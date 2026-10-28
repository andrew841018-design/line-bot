"""市場盤勢（/盤勢，2026-10-24）的川普壓力指數（TACO 指數）collect 任務：`macro.taco`。

Andrew 2026-10-17 要卡上有「川普壓力指數」的數字和歷史位階；MM 的頁面程式抓不到（Cloudflare，不繞過），2026-10-24 改選
MacroMargin（ocmacro.com）公開的 TACO 指數頁（market_sources_taco）。這個模組只負責「到期就抓、寫進 Store」，位階的計算在
market_publish、顯示在 market_dashboard。

規則（PLAN §9、§19.4；INTERFACES §3 C、§9；Andrew 的指定）：
- 站上「應該已經有」的最新美股交易日＝美東收盤＋30 分鐘（D，`market_calendar.latest_closed_us_session`）再過 4 小時
  （D_DELAY_S；來源幾點更新不明：2026-10-23 台北 18:35 看到的已是 10/22 的值，只知道上限）。已存的資料日 ≥ 它就不抓
  （成功之後，到下一個交易日收盤＋4.5 小時之前都不再打這個主機；週末、休市日也不打）。
- 比它舊（第一次、或站上還沒更新）：每 2 小時再看一次（RETRY_S，`due` 自己擋：框架的第一次失敗退避只有 15 分鐘），
  每個台北日最多 6 次（MAX_TRIES_PER_DAY；只算真的送出請求的：帳本的 budget／cooldown 擋下、白名單不過都不算）。
  「還沒更新」（抓到的資料日 ≤ 已存的）不算失敗（任務回 "not_yet"、obs 只更新 checked_at）；抓不到、解析不了記
  `obs_checked(kind)`，把例外丟回框架（框架記 kind、退避）。抓到比已存的新、但還是比 D 舊，照存（任務算成功），之後
  照 2 小時再看。
- 429／403：market_http 已把 ocmacro.com 放進帳本冷卻 2 小時（之後帳本回 FetchError("cooldown")，一個請求都不送）；
  這裡另外在自己的狀態記 cooldown_until，冷卻中任務不到期。被擋的次數往上疊（2026-10-28 審查 gp2 N1；以前永遠是 2 小時，
  被擋的站每個台北日仍敲 6 次）：第 1 次 2 小時（可能是一時的）；連續第 2 次起冷卻到下一個台北午夜（至少 2 小時）；連續 3 個
  台北日都被擋就停 7 天（之後再被擋還是停 7 天）。「連續」＝期間沒有一次「站方有回應、而且不是擋我們」（成功、還沒更新、
  500、解析失敗…；逾時、連不上、帳本擋下這類沒有答案的不算，也不清）。state 的 block_streak 是連續被擋的次數，job 的 outcome
  與 status_summary 帶 taco_block_streak（只有整數），每日維護看得到。
- 資料日晚於「最近一個收完的美股交易日」＝來源給了還沒收盤的值或時間不對 → ParseError("date", "future")，不存。
- 站上的歷史只有「共同交易日」：債市休市但股市開的日子（哥倫布日、退伍軍人節；保存的回應裡缺 2025-10-13、2025-11-11、
  2026-10-12）站上沒有那一列，但 D 是股市交易日——那幾天（到下一個交易日 D 換日為止）任務會用掉當天的 6 次都是
  not_yet。這是刻意的取捨：多幾個對個人網站很輕的請求（每台北日 ≤6 次、主機帳本另有 8 次上限），換不用維護債市休市表。
- 一個開關（照 BLS）：ocmacro.com 不在 market_http.ALLOWED_HOSTS 時 due 一律 False（market_sources_taco.enabled），要停用只要把主機拿掉
  （2026-10-28 審查 gp2 N3：以前拿掉白名單任務照跑、每次 host 失敗進 outcome 的 failing）。
- 時間只用 `ctx.now`；只透過 Context／Store 存取資料。不 import 其他 market_pipeline／market_sources 模組
  （一個模組 import 壞了不能拖垮別的任務；這個模組壞了也只停用自己）。

obs（market_publish 讀；全是 JSON 值，日期一律 ISO 字串；data_date＝as_of）：
  `macro.TACO`＝{"as_of", "value", "change20d"|None, "status_key"|None, "method", "history": [[日期, 值], …]}
  history 是來源完整的日資料（嚴格遞增、最後一筆＝as_of，約 400 筆、每年 +250）。來源的文字（標籤、方法說明、因子名稱）
  一個字都不存；status_key 只存 market_calc.TACO_STATUS_WORDS 認得的鍵（其他是 None）、method 只存 "v1"（來源的方法版本等於
  TACO_METHOD_V1）或 "other"（2026-10-28 審查 gp2 N10：以前原樣存來源給的兩個短字串，站方能控制 DB 裡的這 56 個字元）。
自己的排程紀錄存在 task_state `taco.pipe:macro.taco`：{"day", "tries", "last_try", "last_kind", "cooldown_until",
"block_streak", "block_days", "block_day"}（後三個是被擋的紀錄，見上）。

這個模組 import 時只把任務登記進框架的記憶體表，不做任何 I/O。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import market_calc
import market_calendar
import market_sources_taco as src
import market_tasks
from market_calc import ParseError
from market_tasks import NOT_YET, error_kind

_TPE = ZoneInfo("Asia/Taipei")

TASK_NAME = "macro.taco"
OBS_TACO = "macro.TACO"  # 要和 market_publish.OBS_TACO 一樣（test_market_taco_publish 檢查）
STATE_NAME = "taco.pipe:" + TASK_NAME

D_DELAY_S = 4 * 3600.0  # D（美東收盤＋30 分鐘）之後再等 4 小時，站上才算「應該已經有」
RETRY_S = 2 * 3600.0  # 還沒有／抓不到：兩次嘗試至少隔 2 小時
MAX_TRIES_PER_DAY = 6  # 每個台北日最多嘗試幾次（真的送出請求的）；要和 market_sources_taco.ENDPOINTS 的 per_day 一樣
COOLDOWN_S = 7200.0  # 429／403 之後，和 market_http 的主機冷卻一樣長
TASK_BUDGET_S = 45.0  # 抓取最多 30 秒（ENDPOINTS）＋解析 <0.1 秒
# 沒有送出請求、也不算一次嘗試的失敗種類：白名單不過（host）。帳本擋下的 budget／cooldown 在 run 裡更早就處理掉了
_NO_REQUEST_KINDS = frozenset({"host"})
_RATE_LIMIT_KINDS = frozenset({"rate_limited", "blocked"})
BLOCK_PAUSE_S = 7 * 86400.0  # 連續 3 個台北日都被擋：停 7 天
BLOCK_DAYS_TO_PAUSE = 3
COOLDOWN_AHEAD_S = BLOCK_PAUSE_S + 86400.0  # 狀態裡的 cooldown_until 比「現在＋這麼久」還遠＝時鐘有問題，不信
# 站方有回應、而且不是擋我們（這些失敗種類和所有 ParseError 一樣）：清掉被擋的紀錄。逾時、連不上、帳本擋下這類沒有答案的不算
_ANSWERED_KINDS = frozenset({"moved", "status", "content_type", "too_big", "not_json"})


# ── 共用小工具 ───────────────────────────────────────────────────────────────


def _tpe_day(now: float) -> str:
    return datetime.fromtimestamp(float(now), _TPE).date().isoformat()


def _state(store: Any) -> dict:
    return dict(store.task_state(STATE_NAME) or {})


def _save(store: Any, state: dict) -> None:
    store.set_task_state(STATE_NAME, state)


def _stamp(value: Any, now: float, *, ahead: float = 0.0) -> Optional[float]:
    """狀態裡記的時間：有限數字、而且不比「現在＋ahead」晚（時鐘被調回去時，記在未來的時間不能讓任務卡住）；否則當沒有。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    stamp = float(value)
    return stamp if stamp == stamp and stamp <= now + ahead else None


def _tries_today(state: dict, now: float) -> int:
    if state.get("day") != _tpe_day(now):
        return 0
    tries = state.get("tries")
    return tries if isinstance(tries, int) and not isinstance(tries, bool) and tries > 0 else 0


def _count_try(state: dict, now: float, kind: str) -> None:
    state["tries"] = _tries_today(state, now) + 1
    state["day"] = _tpe_day(now)
    state["last_try"] = float(now)
    state["last_kind"] = kind


def _positive_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _next_tpe_midnight(now: float) -> float:
    local = datetime.fromtimestamp(float(now), _TPE)
    return (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()


def _note_block(state: dict, now: float) -> float:
    """這次被擋了（429／403）：更新 state 裡被擋的紀錄，回這次的冷卻到什麼時候（epoch 秒）。
    第 1 次 2 小時；連續第 2 次起到下一個台北午夜（但至少 2 小時）；連續 3 個台北日都被擋就停 7 天（見模組說明）。"""
    today = _tpe_day(now)
    state["block_streak"] = _positive_int(state.get("block_streak")) + 1
    if state.get("block_day") == today:
        state["block_days"] = max(1, _positive_int(state.get("block_days")))
    else:
        state["block_days"] = _positive_int(state.get("block_days")) + 1
        state["block_day"] = today
    if state["block_days"] >= BLOCK_DAYS_TO_PAUSE:
        return now + BLOCK_PAUSE_S
    if state["block_streak"] >= 2:
        return max(_next_tpe_midnight(now), now + COOLDOWN_S)
    return now + COOLDOWN_S


def _clear_blocks(state: dict) -> None:
    for key in ("block_streak", "block_days", "block_day", "cooldown_until"):
        state.pop(key, None)


def expected_d(ctx: Any) -> Optional[date]:
    """站上應該已經有的最新美股交易日（見模組說明）；日曆不知道 → None，任務就不跑。"""
    try:
        return market_calendar.latest_closed_us_session(ctx.us_cal, float(ctx.now) - D_DELAY_S)
    except Exception:  # noqa: BLE001 - 日曆壞掉：這輪不抓
        return None


def stored_as_of(store: Any) -> Optional[date]:
    """已存的資料日；沒有、或只失敗過、或內容壞了 → None。"""
    found = store.get_obs(OBS_TACO)
    payload = found.get("payload") if isinstance(found, dict) else None
    if not isinstance(payload, dict):
        return None
    value = payload.get("as_of")
    try:
        return date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        return None


def payload_of(series: src.TacoSeries) -> dict:
    return {
        "as_of": series.as_of.isoformat(),
        "value": series.value,
        "change20d": series.change20d,
        # 只存列舉（2026-10-28 審查 gp2 N10）：不認得的狀態鍵、方法版本都不留來源給的字
        "status_key": series.status_key if series.status_key in market_calc.TACO_STATUS_WORDS else None,
        "method": "v1" if series.method == market_calc.TACO_METHOD_V1 else "other",
        "history": [[day.isoformat(), value] for day, value in series.history],
    }


# ── 任務 ─────────────────────────────────────────────────────────────────────


def due(ctx: Any) -> bool:
    if not src.enabled():
        return False  # 白名單拿掉＝停用：什麼都不做、也不記失敗
    d = expected_d(ctx)
    if d is None:
        return False
    stored = stored_as_of(ctx.store)
    if stored is not None and stored >= d:
        return False  # 已是最新：不抓
    now = float(ctx.now)
    state = _state(ctx.store)
    cooldown = _stamp(state.get("cooldown_until"), now, ahead=COOLDOWN_AHEAD_S)  # 比最長的冷卻（7 天）還遠的未來＝時鐘有問題，不信
    if cooldown is not None and now < cooldown:
        return False
    last = _stamp(state.get("last_try"), now)
    if last is not None and now - last < RETRY_S:
        return False
    return _tries_today(state, now) < MAX_TRIES_PER_DAY


def _check_not_future(ctx: Any, series: src.TacoSeries) -> None:
    latest = market_calendar.latest_closed_us_session(ctx.us_cal, ctx.now)
    if latest is not None and series.as_of > latest:
        raise ParseError("date", "future")


def run(ctx: Any) -> Optional[str]:
    d = expected_d(ctx)
    if d is None:
        return NOT_YET
    stored = stored_as_of(ctx.store)
    if stored is not None and stored >= d:
        return NOT_YET
    now = float(ctx.now)
    state = _state(ctx.store)
    try:
        series = src.fetch_taco(http=ctx.http, ledger=ctx.ledger)
        _check_not_future(ctx, series)
    except Exception as exc:
        kind = error_kind(exc)
        if kind == "budget":  # 帳本說這一刻不行（間隔、每日上限、這輪要停了）：沒送請求，不算一次、也不算失敗
            return NOT_YET
        if kind == "cooldown":  # 主機冷卻中：沒送請求；記下來，冷卻結束前任務不到期（已經記著更長的冷卻就不縮短）
            state["cooldown_until"] = max(now + COOLDOWN_S, _stamp(state.get("cooldown_until"), now, ahead=COOLDOWN_AHEAD_S) or 0.0)
            _save(ctx.store, state)
            raise
        if kind not in _NO_REQUEST_KINDS:
            _count_try(state, now, kind)
        if kind in _RATE_LIMIT_KINDS:  # market_http 已經把主機放進帳本冷卻 2 小時；這裡依連續被擋的次數拉長
            state["cooldown_until"] = _note_block(state, now)
        elif isinstance(exc, ParseError) or kind in _ANSWERED_KINDS:  # 站方有回應、不是擋我們：被擋的紀錄清掉
            _clear_blocks(state)
        _save(ctx.store, state)
        ctx.store.obs_checked(OBS_TACO, error_kind=kind)
        raise
    _clear_blocks(state)  # 抓到了（站方沒擋）：被擋的紀錄與冷卻都清掉
    if stored is not None and series.as_of <= stored:  # 站上還沒更新：不算失敗，2 小時後再看
        _count_try(state, now, NOT_YET)
        _save(ctx.store, state)
        ctx.store.obs_not_yet(OBS_TACO)
        return NOT_YET
    ctx.store.put_obs(OBS_TACO, payload_of(series), data_date=series.as_of.isoformat())
    _count_try(state, now, "ok")
    _save(ctx.store, state)
    return None


# ── 任務清單與註冊 ───────────────────────────────────────────────────────────


def tasks() -> list[market_tasks.Task]:
    # medium 組（2 小時來源）：每天最多 6 次、每次 <45 秒，排在 core 的日資料之前
    return [market_tasks.Task(TASK_NAME, "medium", due, run, TASK_BUDGET_S)]


def register_all() -> None:
    for task in tasks():
        market_tasks.register(task)


register_all()
