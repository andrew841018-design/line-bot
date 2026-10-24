"""市場盤勢（/盤勢，2026-10-17）台股的 collect 任務：證交所、證交所 OpenAPI、期交所、MoneyDJ 0050、FinMind。

Andrew 2026-10-17 要台股的技術面、0050 成分站上／跌破均線的家數、成交金額量能（「我要知道當前量是多還是少」）、
漲跌家數、外資動向、融資、台指期權（DECISIONS.md）。這個模組只負責「到期就抓、寫進 Store」：每個端點一個
任務，自己判斷目標日、最早時間、每天次數；計算與顯示在 market_publish／market_dashboard。

規則（PLAN §3.2、§9、§18.4、§18.5、§18.10–18.12、§19.1、§19.2；INTERFACES §3 B、§9）：
- 目標日＝「發布時間已過的最近一個預期交易日」：Mac 睡過整個晚上、隔天早上醒來，目標仍是前一個交易日。
  日曆說「不知道」（休市表還沒抓到的年份）時先當平日開市：抓了才知道，抓不到就是「還沒公布」。
- 證交所 www 每天合計 12 次、間隔 ≥10 秒由 Store 的帳本管（帳本自己等間隔）；各端點的最早時間與每天
  次數照 `market_sources_tw.ENDPOINTS`，以台北日期計。補洞（漏掉的日期、FinMind 比對要的過去幾天）只用
  「當天主要資料（ALLBUT、BFI82U、TWT49U、融資）預留的次數」以外剩下的，晚上 21:30 的融資才不會被補洞吃掉。
- 帳本說不行：budget（間隔、每日上限、這輪要停）＝沒送出請求，不算次數、不算失敗；cooldown 照框架記失敗。
  「還沒公布」回 "not_yet"（不算失敗）。
- ALLBUT0999：全部普通股＋0050 的收盤存 `tw_all`（每檔只留最近 10 列：新成分不必再打請求，FinMind 比對
  也用它）；追蹤的 0050 成分、0050、加權指數另存 `tw`（長歷史）。加權指數那一列的「量」放成交金額
  「總計(1~15)」（元），和 FinMind TAIEX 的 Trading_money 同口徑：大盤量能看的就是成交金額。
- 颱風（§18.4）：T 當天 20:00 前沒資料只算「還沒公布」；T 的 20:00 之後（含 Mac 隔天早上才醒）ALLBUT 與
  FinMind 都沒有 T，才寫「暫定休市」覆寫；之後任一來源看到 T 的資料就撤銷（覆寫改 revoked，紀錄留著）。
- TWT49U：bootstrap 把整個歷史窗口分段抓（最新那段先抓），之後每天 16:00 後從上次檢查日抓到今天；
  沒有事件的區間也算涵蓋，否則台股寬度永遠等不到「除權息資料涵蓋到 T」（§18.11）。
- FinMind：0050 成分＋0050＋加權指數的 bootstrap／補洞；間隔 ≥3 秒、每天 ≤60 次（帳本）；非 200 或
  msg≠success 就停這輪補洞（再停一小時）；第一次接入某檔前，和已存的 ALLBUT 收盤比最近 5 個交易日（全部
  51 檔共用同一組 5 天的證交所請求），對不上就不接；同一天出現新值（來源修訂）→ 整檔重抓、一個交易內替換。
- 外部字串（名稱、說明）一律先過 `market_calc.clean_external` 才存；代號另驗格式。

obs 鍵（market_publish 讀；payload 全是 JSON 值、日期一律 ISO 字串）見下方 `OBS_*` 常數。
這個模組 import 時只把任務登記進框架的記憶體表，不做任何 I/O；sources 到真的要抓時才載入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

import market_calendar
import market_tasks

TPE = ZoneInfo("Asia/Taipei")
UTC = timezone.utc

# ── 存放位置（market_publish 讀這些名字；要改兩邊一起改） ─────────────────────────────────
MARKET = "tw"  # 追蹤的標的：0050 成分＋0050＋加權指數，長歷史（market_calendar 也用這裡的加權指數當實際成交日）
MARKET_ALL = "tw_all"  # ALLBUT0999 全部普通股＋0050＋加權指數，每檔只留最近 KEEP_ALL_SESSIONS 列
SYM_TAIEX = "TAIEX"
SYM_0050 = "0050"
SOURCE_TWSE = "twse"
SOURCE_FINMIND = "finmind"
CAL_MARKET = "tw"  # 日曆覆寫的市場鍵

OBS_ALLBUT = "tw.allbut"  # {"days": {iso: 大盤摘要（加權指數、成交金額、漲跌家數…）}, "latest": iso}
OBS_INST3 = "tw.inst3"  # {"days": {iso: {"foreign","trust","dealer_self","dealer_hedge"}（元）}, "latest": iso}
OBS_MARGIN = "tw.margin"  # {"date","margin_balance_k","margin_prev_k","short_units","short_prev_units"}
OBS_TWT49U = "tw.twt49u"  # {"covered_from","covered_to","pieces"}（事件在 store.exrights、區間在 coverage）
OBS_PE = "tw.pe"  # {"date", "pe": {代號: 本益比|None}}
OBS_REVENUE = "tw.revenue"  # {"period": "YYYY-MM", "rows": {代號: [年, 月, 年增%|None]}}
OBS_FUTURES = "tw.futures"  # {"date", "foreign": {"TX"(大台)|"MTX"(小台)|"TMF"(微台): {oi_long, oi_short, oi_net, trade_net}},
#                              "days": {iso: 那天的 foreign（同上）}（最近 10 個資料日，日變化用）, "latest": iso}
OBS_OPTIONS_INST = "tw.options_inst"  # {"date", "foreign_call": {...}, "foreign_put": {...}}
OBS_PCR = "tw.pcr"  # {"date", "rows": [{date, put_volume, call_volume, volume_ratio_pct, put_oi, call_oi, oi_ratio_pct}]}
OBS_TXO = "tw.txo"  # {"date", "fields": [...], "rows": [[month, strike, kind, oi|None, settle|None], …]}（月選一般盤）
OBS_FX = "tw.fx"  # {"date","usd_ntd","prev_date","prev_usd_ntd"}（market_pipeline_misc 的台幣代理也讀這個）
OBS_HOLDINGS = "tw.holdings.0050"  # {"source","as_of","rows": [[代號, 名稱|None, 權重%], …],"count",…}（追蹤名單）
OBS_FINMIND = "tw.finmind"  # {"total","accepted","rejected": {代號: 原因},"stopped"?}（「歷史資料補到 X/Y 檔」）
OBS_HOLIDAYS_PREFIX = "tw.holidays."  # ＋西元年：{"year","rows": [{date,name,note,kind}],"unknown": [iso…]}
COVERAGE_TWT49U = "twt49u"

OVERRIDE_CLOSED = "closed"  # 颱風等臨時休市（暫定，confirmed=False，每日維護確認）
OVERRIDE_REVOKED = "revoked"  # 撤銷暫定休市：之後看到了那天的資料（日曆當作沒有覆寫，紀錄留著）
TYPHOON_EVIDENCE = "allbut0999_empty+finmind_empty"
REVOKE_EVIDENCE = "data_seen"

# ── 任務名稱；自己的排程紀錄放在 STATE_PREFIX＋名稱（和框架的 task_state(任務名) 分開） ─────────────
TASK_ALLBUT = "tw.allbut"
TASK_BFI = "tw.bfi82u"
TASK_TWT49U = "tw.twt49u"
TASK_MARGIN = "tw.margin"
TASK_HOLIDAYS = "tw.holidays"
TASK_PE = "tw.pe"
TASK_REVENUE = "tw.revenue"
TASK_HOLDINGS = "tw.holdings"
TASK_FINMIND = "tw.finmind"
STATE_PREFIX = "tw.pipe:"
WWW_TASKS = (TASK_ALLBUT, TASK_BFI, TASK_TWT49U, TASK_MARGIN, TASK_HOLIDAYS)
NOT_YET, PARTIAL = market_tasks.NOT_YET, market_tasks.PARTIAL

# ── 排程常數 ──────────────────────────────────────────────────────────────────────
WWW_DAILY_CAP = 12  # 證交所 www 每天合計（Andrew 接受和 /股票 各自限速；帳本另外硬擋）
# 今天是交易日、主要資料還沒到手前，各端點保留的次數（補洞不能動）：發布時間一次＋重試一次
CORE_RESERVE = {TASK_ALLBUT: 2, TASK_BFI: 2, TASK_TWT49U: 1, TASK_MARGIN: 2}
RETRY_NOT_YET_S = 1800.0  # 還沒公布：30 分鐘後再試
TYPHOON_DECIDE_AT = time(20, 0)
KEEP_ALL_SESSIONS = 10
KEEP_TRACKED_SESSIONS = 300  # PLAN §2：closes 只留近 300 個交易日
HISTORY_DAYS = 430  # 歷史窗口（日曆天）≈ 290 個交易日：240 日均線＋5、250 日量能百分位都夠
INST3_WINDOW = 5  # 5 日合計要齊
EMPTY_GIVE_UP = 2  # 過去的日期回「沒有資料」幾次就不再補（休市表沒寫的特別休市）
CLOSURE_RECHECK_DAYS = 10  # 暫定休市之後幾天內，每天再看一次有沒有資料（撤銷用）
TWT49U_CHUNK_DAYS = 120  # 一次請求的區間（實測 6/1–10/16 一次 1,023 筆）
TWT49U_BOOTSTRAP_PER_DAY = 3  # bootstrap 期間每天最多（正常是 ENDPOINTS 的 2 次）
HOLIDAY_BOOTSTRAP_PER_DAY = 2  # 今年＋前一年兩張都還沒有時，當天可以抓兩次
HOLIDAY_RETRY_S = 6 * 3600.0
HOLIDAY_REFRESH_S = 30 * 86400.0  # 今年的表每 30 天重抓一次（證交所可能補公告特別開休市）
HOLIDAY_NEXT_YEAR_MONTH = 11  # 下一年的表 11 月起每週試抓
HOLIDAY_NEXT_YEAR_EVERY_S = 7 * 86400.0
TAIFEX_EARLIEST = time(15, 0)  # 過了這個時間「今天」才算目標日；幾點起才抓看 ENDPOINTS 的 earliest_tpe（07:00）
# 2026-10-18：期交所常晚一天，30 分鐘一次會在半夜就把每天的次數用完 → 3 小時。2026-10-23：07:00 起才抓（ENDPOINTS），
# 改 2 小時、每天 9 次：CDN 給 CSV 的那幾個小時過去後當天還有機會（07、09…23 點）
TAIFEX_EVERY_S = 2 * 3600.0
DAILY_FROM = time(8, 0)  # 本益比、月營收、MoneyDJ 持股：每天一次的最早時間
DAILY_RETRY_S = 7200.0
FINMIND_OVERLAP = 5
FINMIND_TOLERANCE = 0.005  # 證交所收盤到小數 2 位、FinMind 給浮點：差 ≤0.005 算相同
FINMIND_STOP_S = 3600.0  # 非 200／msg≠success 之後先停一小時（帳本每天 60 次，不要每 15 分鐘撞一次）
FINMIND_REJECT_RETRY_DAYS = 7
MIN_TIME_LEFT_S = 8.0  # 補洞迴圈：剩不到這麼多秒就不再開新的請求

_TW_CODE_TAIL = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_NO_DATA_KINDS = frozenset({"empty", "date"})  # 證交所「沒有符合條件的資料」或回別天的資料＝還沒有這天

# 端點不在 market_sources_tw.ENDPOINTS 時的預設（PLAN §3.2 的表）
_DEFAULT_RULES: dict[str, tuple[Optional[time], int]] = {
    "twse_allbut0999": (time(14, 30), 4),
    "twse_bfi82u": (time(15, 30), 3),
    "twse_twt49u": (time(16, 0), 2),  # 2026-10-23 1→2：凌晨補昨天那次不再吃掉 16:00 那次
    "twse_margin_ms": (time(21, 30), 3),
    "twse_holidays": (None, 1),
    "openapi_bwibbu": (None, 2),
    "openapi_revenue": (None, 2),
    "taifex_futures_inst": (time(7, 0), 9),  # 2026-10-23：期交所約 06:36 才放上前一個交易日，07:00 起才抓
    "taifex_calls_puts_inst": (time(7, 0), 9),
    "taifex_pcr": (time(7, 0), 9),
    "taifex_daily_options": (time(7, 0), 9),
    "taifex_fx": (time(7, 0), 2),
    "finmind_prices": (None, 60),
    "moneydj_0050": (None, 2),
}
_ENDPOINT_OF = {
    TASK_ALLBUT: "twse_allbut0999",
    TASK_BFI: "twse_bfi82u",
    TASK_TWT49U: "twse_twt49u",
    TASK_MARGIN: "twse_margin_ms",
    TASK_HOLIDAYS: "twse_holidays",
}


# ── 共用：延後載入、錯誤分類、清洗 ───────────────────────────────────────────────────
def _tw() -> Any:
    import market_sources_tw  # noqa: PLC0415 - 真的要抓時才載入

    return market_sources_tw


def _fetch_error() -> type:
    import market_http  # noqa: PLC0415

    return market_http.FetchError


class _NoRequest(Exception):
    """帳本擋下：沒有送出請求（budget＝間隔或每日上限、這輪要停；cooldown＝429／403 之後的冷卻）。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def _kind(exc: BaseException) -> str:
    kind = getattr(exc, "kind", None)
    return kind if isinstance(kind, str) and kind else f"unexpected:{type(exc).__name__}"


def _is_parse_error(exc: BaseException) -> bool:
    return isinstance(exc, ValueError) and isinstance(getattr(exc, "kind", None), str)


def _is_source_error(exc: BaseException) -> bool:
    """來源的錯（抓取或解析），不是程式的錯；程式的錯（TypeError…）原樣往上丟給框架記 unexpected。"""
    return isinstance(exc, _fetch_error()) or _is_parse_error(exc)


def _call(ctx: Any, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    fetch_error = _fetch_error()
    try:
        return fn(*args, http=ctx.http, ledger=ctx.ledger, **kwargs)
    except fetch_error as exc:
        if getattr(exc, "kind", None) in ("budget", "cooldown"):
            raise _NoRequest(exc.kind) from None
        raise


def _skipped(exc: _NoRequest) -> str:
    """帳本擋下時的回傳：budget＝不算失敗（這輪不打）；cooldown 丟回框架記失敗（主機被限流中）。"""
    if exc.kind == "cooldown":
        raise _fetch_error()("cooldown")
    return NOT_YET


def _for_framework(exc: BaseException) -> BaseException:
    """框架只認 FetchError／ParseError 的 kind；FinMind「msg≠success」那種另外的型別換成 FetchError("status")。"""
    if isinstance(exc, _fetch_error()) or type(exc).__name__ == "ParseError":
        return exc
    if _is_parse_error(exc) and _kind(exc) == "status":
        return _fetch_error()("status")
    return exc


def clean(text: Any, max_len: int) -> Optional[str]:
    """PLAN §19.1 的共用清洗（market_calc.clean_external）；回 None＝整則丟掉。"""
    import market_calc  # noqa: PLC0415

    return market_calc.clean_external(text, max_len)


def valid_code(code: Any) -> bool:
    """台股代號：4 碼數字開頭、最多再兩個數字或大寫字母（和 market_sources_tw 同規則）。"""
    return (
        isinstance(code, str)
        and 4 <= len(code) <= 6
        and code[:4].isascii()
        and code[:4].isdigit()
        and all(ch in _TW_CODE_TAIL for ch in code[4:])
    )


# ── 日期與日曆 ─────────────────────────────────────────────────────────────────────
def _now_tpe(ctx: Any) -> datetime:
    return datetime.fromtimestamp(float(ctx.now), TPE)


def _at(day: date, at: time) -> datetime:
    return datetime.combine(day, at, TPE)


def expected_session(cal: Any, day: date) -> bool:
    """是不是預期交易日；日曆說不知道（None）時先當平日開市。"""
    try:
        value = cal.is_session(day)
    except Exception:  # noqa: BLE001 - 日曆壞掉不能讓台股整組停擺
        value = None
    if value is None:
        return day.weekday() < 5
    return bool(value)


def recent_sessions(cal: Any, end: date, count: int) -> list[date]:
    """從 end（含）往前的 count 個預期交易日，新到舊。"""
    out: list[date] = []
    day = end
    for _ in range(count * 2 + 30):
        if len(out) >= count:
            break
        if expected_session(cal, day):
            out.append(day)
        day -= timedelta(days=1)
    return out


def target_session(cal: Any, now_tpe: datetime, earliest: Optional[time]) -> Optional[date]:
    """發布時間已過的最近一個預期交易日（今天要過了 earliest 才算今天）。"""
    today = now_tpe.date()
    if expected_session(cal, today) and (earliest is None or now_tpe.time() >= earliest):
        return today
    back = recent_sessions(cal, today - timedelta(days=1), 1)
    return back[0] if back else None


def _rule(endpoint: str) -> tuple[Optional[time], int]:
    """(最早時間, 每天最多次)：以 market_sources_tw.ENDPOINTS 為準，沒有就用 PLAN 的表。"""
    try:
        item = _tw().ENDPOINTS[endpoint]
        return item.earliest_tpe, int(item.daily_max)
    except (ImportError, AttributeError, KeyError, TypeError, ValueError):
        return _DEFAULT_RULES[endpoint]


# ── 自己的排程紀錄（以台北日期計每天次數） ──────────────────────────────────────────────
def _sched(store: Any, name: str, today: date) -> dict:
    state = dict(store.task_state(STATE_PREFIX + name) or {})
    if state.get("day") != today.isoformat():
        state["day"] = today.isoformat()
        state["count"] = 0
    state.setdefault("count", 0)
    return state


def _save(store: Any, name: str, state: dict) -> None:
    tries = state.get("tries")
    if isinstance(tries, dict) and len(tries) > 40:  # 只留最近的日期紀錄
        for key in sorted(tries)[:-40]:
            tries.pop(key, None)
    store.set_task_state(STATE_PREFIX + name, state)


def _note_try(state: dict, iso: Optional[str], now: float) -> None:
    """記一次真的送出的請求（含「還沒公布」與失敗）。"""
    state["count"] = int(state.get("count", 0)) + 1
    state["last_try"] = float(now)
    if iso is not None:
        record = state.setdefault("tries", {}).setdefault(iso, {})
        record["n"] = int(record.get("n", 0)) + 1
        record["last"] = float(now)


def _tried_within(state: dict, iso: str, now: float, seconds: float) -> bool:
    record = (state.get("tries") or {}).get(iso) or {}
    last = record.get("last")
    return isinstance(last, (int, float)) and now - float(last) < seconds


def _empty_count(state: dict, iso: str) -> int:
    return int(((state.get("tries") or {}).get(iso) or {}).get("empty", 0))


def _note_empty(state: dict, iso: str) -> None:
    record = state.setdefault("tries", {}).setdefault(iso, {})
    record["empty"] = int(record.get("empty", 0)) + 1


# ── 讀 Store 的小工具 ────────────────────────────────────────────────────────────────
def _payload(store: Any, obs_id: str) -> dict:
    obs = store.get_obs(obs_id)
    payload = obs.get("payload") if isinstance(obs, dict) else None
    return payload if isinstance(payload, dict) else {}


def _obs_date(store: Any, obs_id: str) -> Optional[str]:
    obs = store.get_obs(obs_id)
    value = obs.get("data_date") if isinstance(obs, dict) and obs.get("payload") is not None else None
    return value if isinstance(value, str) else None


def _days(store: Any, obs_id: str) -> dict:
    days = _payload(store, obs_id).get("days")
    return days if isinstance(days, dict) else {}


def _merge_days(store: Any, obs_id: str, day: date, summary: dict, keep: int) -> None:
    days = dict(_days(store, obs_id))
    days[day.isoformat()] = summary
    kept = sorted(days)[-keep:]
    store.put_obs(obs_id, {"days": {key: days[key] for key in kept}, "latest": kept[-1]}, data_date=kept[-1])


def closures(store: Any) -> dict:
    """暫定休市（ALLBUT 的排程紀錄裡）：{iso: {"at", "rechecked"?}}。"""
    return dict(dict(store.task_state(STATE_PREFIX + TASK_ALLBUT) or {}).get("closures") or {})


def tracked_codes(store: Any) -> list[str]:
    """追蹤名單：加權指數、0050，再加 0050 成分（權重大的先）。"""
    rows = _payload(store, OBS_HOLDINGS).get("rows")
    weighted: list[tuple[float, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        code, weight = row[0], row[2]
        if valid_code(code) and code not in (SYM_0050, SYM_TAIEX):
            weighted.append((-float(weight) if isinstance(weight, (int, float)) else 0.0, code))
    members = [code for _, code in sorted(set(weighted))]
    ordered: list[str] = []
    for code in (SYM_TAIEX, SYM_0050, *members):
        if code not in ordered:
            ordered.append(code)
    return ordered


def _coverage(store: Any) -> list[tuple[date, date]]:
    return sorted((a, b) for a, b in store.coverage(COVERAGE_TWT49U) or [])


def _coverage_end(store: Any) -> Optional[date]:
    covered = _coverage(store)
    return max(end for _, end in covered) if covered else None


def _today_done(ctx: Any, name: str, today: date) -> bool:
    iso = today.isoformat()
    if name == TASK_ALLBUT:
        return iso in _days(ctx.store, OBS_ALLBUT)
    if name == TASK_BFI:
        return iso in _days(ctx.store, OBS_INST3)
    if name == TASK_TWT49U:
        end = _coverage_end(ctx.store)
        return end is not None and end >= today
    if name == TASK_MARGIN:
        return _obs_date(ctx.store, OBS_MARGIN) == iso
    return True


def reserves(ctx: Any, today: date) -> dict[str, int]:
    """今天是交易日時，主要資料還沒到手的端點各保留幾次（不超過它今天剩下的次數）。"""
    if not expected_session(ctx.tw_cal, today) or today.isoformat() in closures(ctx.store):
        return {}
    out: dict[str, int] = {}
    for name, keep in CORE_RESERVE.items():
        if _today_done(ctx, name, today):
            continue
        _, cap = _rule(_ENDPOINT_OF[name])
        if name == TASK_TWT49U and _twt49u_bootstrapping(ctx):
            cap = max(cap, TWT49U_BOOTSTRAP_PER_DAY)
        used = int(_sched(ctx.store, name, today)["count"])
        out[name] = max(0, min(keep, cap - used))
    return out


def _twt49u_bootstrapping(ctx: Any) -> bool:
    _, historical, _, _ = twt49u_parts(ctx)
    return bool(historical)


def www_used(ctx: Any, today: date) -> int:
    return sum(int(_sched(ctx.store, name, today)["count"]) for name in WWW_TASKS)


def spare_ok(ctx: Any, name: str, state: dict, today: date, cap: int) -> bool:
    """補洞能不能用這一次：自己的上限扣掉當天預留、證交所總量扣掉全部預留，都還有剩。"""
    held = reserves(ctx, today)
    if int(state["count"]) + held.get(name, 0) >= cap:
        return False
    return www_used(ctx, today) + sum(held.values()) < WWW_DAILY_CAP


# ── ALLBUT0999（加權指數、成交金額、漲跌家數、全部收盤） ────────────────────────────────────
def allbut_target(ctx: Any) -> Optional[date]:
    """主要任務這一輪要抓的目標日；不用抓回 None。"""
    now_tpe = _now_tpe(ctx)
    today = now_tpe.date()
    earliest, cap = _rule("twse_allbut0999")
    state = _sched(ctx.store, TASK_ALLBUT, today)
    if state["count"] >= cap:
        return None
    target = target_session(ctx.tw_cal, now_tpe, earliest)
    if target is None:
        return None
    iso = target.isoformat()
    if iso in _days(ctx.store, OBS_ALLBUT) or iso in (state.get("closures") or {}):
        return None
    if _tried_within(state, iso, ctx.now, RETRY_NOT_YET_S):
        return None
    # 最後一次留到 T 的 20:00 之後：那時兩個來源都沒有才算颱風休市
    if now_tpe < _at(target, TYPHOON_DECIDE_AT) and state["count"] >= cap - 1:
        return None
    return target


def allbut_backfill(ctx: Any) -> Optional[tuple[str, date]]:
    """補洞：最近 10 個交易日裡漏掉的（新到舊），再來是暫定休市的撤銷檢查（每天一次），最後是歷史窗口裡
    更早的洞（每天一個，`_history_gap`）。"""
    now_tpe = _now_tpe(ctx)
    today = now_tpe.date()
    earliest, cap = _rule("twse_allbut0999")
    state = _sched(ctx.store, TASK_ALLBUT, today)
    if state["count"] >= cap or not spare_ok(ctx, TASK_ALLBUT, state, today, cap):
        return None
    target = target_session(ctx.tw_cal, now_tpe, earliest)
    if target is None:
        return None
    days = _days(ctx.store, OBS_ALLBUT)
    closed = state.get("closures") or {}
    recent = recent_sessions(ctx.tw_cal, target, KEEP_ALL_SESSIONS)
    for day in recent:
        iso = day.isoformat()
        if day == target or iso in days or iso in closed:
            continue
        if _empty_count(state, iso) >= EMPTY_GIVE_UP or _tried_within(state, iso, ctx.now, RETRY_NOT_YET_S):
            continue
        return ("backfill", day)
    for iso in sorted(closed, reverse=True):
        day = date.fromisoformat(iso)
        if day >= today or (today - day).days > CLOSURE_RECHECK_DAYS:
            continue
        if (closed[iso] or {}).get("rechecked") == today.isoformat():
            continue
        return ("recheck", day)
    gap = _history_gap(ctx, state, today, recent)
    return None if gap is None else ("history", gap)


HISTORY_GAPS = "history_gaps"  # ALLBUT 排程紀錄裡的歷史洞紀錄：{"day": 最後一次試的台北日期, "tried": {iso: 次數}}


def _history_gap(ctx: Any, state: dict, today: date, recent: list[date]) -> Optional[date]:
    """歷史窗口裡更早的洞：表上是交易日、tw 的加權指數卻沒有收盤（新到舊），每天最多試一個。

    2026-10-23：2026-07-10 是休市表沒寫的臨時休市（推測颱風），是 FinMind 歷史 bootstrap 帶進來的洞。live 路徑只在
    T 當天 20:00 後判斷、上面的補洞只看最近 10 個交易日，永遠輪不到它；跨過它的 120／240 日窗口全部無效（0050 半年線、
    年線暫缺，成分寬度資料不足）。交給 _run_allbut／_allbut_no_data 照原路處理：證交所有資料就存，證交所與 FinMind
    都沒有就寫暫定休市。舊日期的 ALLBUT 摘要早就不在 obs 裡（只留 10 天），所以看收盤、不看 obs，否則整年都會重抓；
    只看已存歷史之內（加權指數第一筆之後），更早的是還沒 bootstrap、不是洞。試過 EMPTY_GIVE_UP 次還在就不再試，
    留給每日維護（outcome 的 calendar_missing_bars）。"""
    record = state.get(HISTORY_GAPS) or {}
    if not recent or record.get("day") == today.isoformat():
        return None
    rows = ctx.store.closes(MARKET, SYM_TAIEX, since=today - timedelta(days=HISTORY_DAYS))
    if not rows:
        return None
    try:
        gaps = ctx.tw_cal.missing_bars(rows[0][0], min(recent) - timedelta(days=1))
    except Exception:  # noqa: BLE001 - 日曆壞掉不能讓台股整組停擺
        return None
    tried = record.get("tried") or {}
    closed = state.get("closures") or {}
    for day in reversed(gaps):
        iso = day.isoformat()
        if iso not in closed and int(tried.get(iso) or 0) < EMPTY_GIVE_UP:
            return day
    return None


def _note_history(state: dict, iso: str, today: date) -> None:
    """歷史洞真的送出了一次請求：記今天用掉了、這個洞試過幾次。另外記，因為 `tries` 只留最近 40 個日期，
    舊日期的紀錄一存就被修掉，「試過幾次」永遠是 0。"""
    start = (today - timedelta(days=HISTORY_DAYS)).isoformat()
    tried = {
        key: int(value)
        for key, value in ((state.get(HISTORY_GAPS) or {}).get("tried") or {}).items()
        if key >= start and isinstance(value, int)
    }
    tried[iso] = tried.get(iso, 0) + 1
    state[HISTORY_GAPS] = {"day": today.isoformat(), "tried": tried}


def _run_allbut(ctx: Any, day: date, mode: str) -> Optional[str]:
    today = _now_tpe(ctx).date()
    state = _sched(ctx.store, TASK_ALLBUT, today)
    iso = day.isoformat()
    try:
        daily = _call(ctx, _tw().fetch_twse_allbut0999, day, extra_codes=(SYM_0050,))
    except _NoRequest as exc:
        return _skipped(exc)
    except Exception as exc:
        if not _is_source_error(exc):
            raise
        _note_try(state, iso, ctx.now)
        if mode == "history":
            _note_history(state, iso, today)
        if _is_parse_error(exc) and _kind(exc) in _NO_DATA_KINDS:
            status = _allbut_no_data(ctx, state, day, mode)
            _save(ctx.store, TASK_ALLBUT, state)
            return status
        _save(ctx.store, TASK_ALLBUT, state)
        if mode == "target":
            ctx.store.obs_checked(OBS_ALLBUT, error_kind=_kind(exc))
        raise _for_framework(exc) from None
    _note_try(state, iso, ctx.now)
    if mode == "history":
        _note_history(state, iso, today)
    store_allbut(ctx.store, daily, ctx.tw_cal)
    closed = state.get("closures") or {}
    if iso in closed:
        ctx.store.set_calendar_override(CAL_MARKET, day, OVERRIDE_REVOKED, REVOKE_EVIDENCE, confirmed=False)
        closed.pop(iso, None)
        state["closures"] = closed
    (state.get("source_gaps") or {}).pop(iso, None)
    _save(ctx.store, TASK_ALLBUT, state)
    return None


def _allbut_no_data(ctx: Any, state: dict, day: date, mode: str) -> Optional[str]:
    """ALLBUT 說這天沒有資料：20:00 前＝還沒公布；20:00 後再問 FinMind，兩邊都沒有才寫暫定休市。"""
    iso = day.isoformat()
    _note_empty(state, iso)
    closed = state.setdefault("closures", {})
    if mode == "recheck":
        if iso in closed:
            closed[iso]["rechecked"] = _now_tpe(ctx).date().isoformat()
        return NOT_YET
    if _now_tpe(ctx) < _at(day, TYPHOON_DECIDE_AT):
        if mode == "target":
            ctx.store.obs_not_yet(OBS_ALLBUT)
        return NOT_YET
    try:
        expected = ctx.tw_cal.is_session(day)
    except Exception:  # noqa: BLE001
        expected = None
    if expected is not True:  # 休市表還沒抓到（bootstrap）：不知道那天本來開不開，不寫覆寫
        if mode == "target":
            ctx.store.obs_not_yet(OBS_ALLBUT)
        return NOT_YET
    seen = finmind_has_taiex(ctx, day)
    if seen is None:  # FinMind 這次問不到：先不判斷，下一次再看
        if mode == "target":
            ctx.store.obs_not_yet(OBS_ALLBUT)
        return NOT_YET
    if seen:  # 市場有開、只是證交所這次沒給（來源漏日）：不是休市，30 分鐘後再試
        state.setdefault("source_gaps", {})[iso] = float(ctx.now)
        if mode == "target":
            ctx.store.obs_checked(OBS_ALLBUT, error_kind="source_gap")
        return NOT_YET
    ctx.store.set_calendar_override(CAL_MARKET, day, OVERRIDE_CLOSED, TYPHOON_EVIDENCE, confirmed=False)
    closed[iso] = {"at": float(ctx.now)}
    if mode == "target":
        ctx.store.obs_not_yet(OBS_ALLBUT)
    return NOT_YET


def finmind_has_taiex(ctx: Any, day: date) -> Optional[bool]:
    """FinMind 有沒有這天的加權指數：已存就不打請求；問不到（帳本、錯誤）回 None。"""
    if any(row[0] == day for row in ctx.store.closes(MARKET, SYM_TAIEX, since=day)):
        return True
    try:
        bars = _call(ctx, _tw().fetch_finmind_prices, SYM_TAIEX, day, day)
    except _NoRequest:
        return None
    except Exception as exc:
        if not _is_source_error(exc):
            raise
        if _is_parse_error(exc) and _kind(exc) == "empty":
            return False
        return None
    return any(getattr(bar, "date", None) == day for bar in bars)


TW_JUMP_LIMIT = 0.105  # 台股漲跌幅 ±10%（PLAN §18.2）：超過又沒有除權息事件＝減資／分割（TWT49U 表註明不含）
QUARANTINE_JUMP = "jump"


def check_tw_jumps(store: Any, codes: Optional[Iterable[str]] = None, cal: Any = None) -> None:
    """長歷史（tw）相鄰兩日收盤 |r−1| > 10.5%，而除權息表涵蓋那天卻沒有事件 → 隔離那一段（publish 把
    store.quarantine("tw") 交給 breadth，只讓碰到的均線窗口無效）。涵蓋不到的日子不判斷（可能是還沒補的大額除息）；
    之後補到那天的事件就解除重算。2026-10-18 審查 r6c I3：PLAN 寫了「台股 ±10% 隔離」，原本沒做。

    每次都照現在的收盤重算這檔該隔離的段落、和現有的比：補到事件或來源改正了價格，已經不是跳空的那段就解除
    （2026-10-23 審查 r7 #3：原本只有補到事件才解除，價格改正後隔離永遠留著、寬度窗口一直排除這檔）。

    只比「相鄰兩個交易日」：中間缺一天（ALLBUT 漏抓、FinMind 還沒補）時，兩天各自正常的漲跌加起來就會超過
    10.5%，不能當跳空；等補齊再判（2026-10-23 審查 r7a N2）。日曆不知道的日子也不判斷。"""
    spans = _coverage(store)
    if cal is None:
        cal = market_calendar.tw_calendar(store)
    current = store.quarantine(MARKET)
    # 去重：current 是開頭讀的，同一檔第二次會拿舊值再清一次、多留一筆歷史（2026-10-23 審查 r8 Codex nit）
    for code in dict.fromkeys(codes) if codes is not None else tracked_codes(store):
        if code == SYM_TAIEX or not valid_code(code):
            continue
        events = {row.date for row in store.exrights(code)}
        rows = sorted((day, value[0]) for day, value in _closes_map(store, MARKET, code).items())
        want = {
            (d0, d1)
            for (d0, c0), (d1, c1) in zip(rows, rows[1:])
            if c0 > 0
            and abs(c1 / c0 - 1.0) > TW_JUMP_LIMIT
            and d1 not in events
            and any(a <= d1 <= b for a, b in spans)
            and cal.prev_session(d1) == d0
        }
        have = {(a, b) for a, b, reason in current.get(code, []) if reason == QUARANTINE_JUMP}
        if want == have:
            continue
        with store.transaction():
            if have - want:  # 有一段已經不成立：這檔的跳空隔離全部解除、再照現在的加回去
                store.clear_quarantine(MARKET, code, QUARANTINE_JUMP)
                have = set()
            for d0, d1 in sorted(want - have):
                store.add_quarantine(MARKET, code, d0, d1, QUARANTINE_JUMP)


def store_allbut(store: Any, daily: Any, cal: Any = None) -> None:
    """一天的 ALLBUT0999（一個交易）：全部收盤進 tw_all、追蹤的進 tw、大盤摘要進 obs。"""
    day = daily.date
    tracked = set(tracked_codes(store))
    summary = {
        "taiex_close": float(daily.taiex_close),
        "taiex_change": float(daily.taiex_change),
        "turnover_twd": int(daily.turnover_twd),
        "adv": int(daily.adv),
        "dec": int(daily.dec),
        "flat": int(daily.flat),
        "adv_limit": daily.adv_limit,
        "dec_limit": daily.dec_limit,
        "untraded": getattr(daily, "untraded", None),
        "no_compare": int(getattr(daily, "no_compare", 0) or 0),
        "bad_rows": int(getattr(daily, "bad_rows", 0) or 0),
        "closes": sum(1 for item in daily.closes.values() if item.close is not None),
    }
    with store.transaction():
        for code, item in sorted(daily.closes.items()):
            if not valid_code(code) or item.close is None:  # 「--」＝當天無成交：沒有收盤可存
                continue
            row = [(day, float(item.close), int(item.volume))]
            store.upsert_closes(MARKET_ALL, code, row, SOURCE_TWSE)
            if code in tracked:
                store.upsert_closes(MARKET, code, row, SOURCE_TWSE)
        taiex = [(day, float(daily.taiex_close), int(daily.turnover_twd))]
        store.upsert_closes(MARKET_ALL, SYM_TAIEX, taiex, SOURCE_TWSE)
        store.upsert_closes(MARKET, SYM_TAIEX, taiex, SOURCE_TWSE)
        store.prune(MARKET_ALL, KEEP_ALL_SESSIONS)
        _merge_days(store, OBS_ALLBUT, day, summary, KEEP_ALL_SESSIONS)
    check_tw_jumps(store, sorted(tracked & set(daily.closes)), cal)


def _allbut_due(ctx: Any) -> bool:
    return allbut_target(ctx) is not None


def _allbut_main(ctx: Any) -> Optional[str]:
    day = allbut_target(ctx)
    return NOT_YET if day is None else _run_allbut(ctx, day, "target")


def _allbut_backfill_due(ctx: Any) -> bool:
    return allbut_backfill(ctx) is not None


def _allbut_backfill_run(ctx: Any) -> Optional[str]:
    plan = allbut_backfill(ctx)
    return NOT_YET if plan is None else _run_allbut(ctx, plan[1], plan[0])


# ── BFI82U（三大法人；5 日合計要齊，漏的在每天上限內補） ───────────────────────────────────
def bfi_target(ctx: Any) -> Optional[date]:
    now_tpe = _now_tpe(ctx)
    today = now_tpe.date()
    earliest, cap = _rule("twse_bfi82u")
    state = _sched(ctx.store, TASK_BFI, today)
    if state["count"] >= cap:
        return None
    target = target_session(ctx.tw_cal, now_tpe, earliest)
    if target is None:
        return None
    iso = target.isoformat()
    if iso in _days(ctx.store, OBS_INST3) or iso in closures(ctx.store):
        return None
    if _tried_within(state, iso, ctx.now, RETRY_NOT_YET_S):
        return None
    return target


def bfi_backfill(ctx: Any) -> Optional[date]:
    now_tpe = _now_tpe(ctx)
    today = now_tpe.date()
    earliest, cap = _rule("twse_bfi82u")
    state = _sched(ctx.store, TASK_BFI, today)
    if state["count"] >= cap or not spare_ok(ctx, TASK_BFI, state, today, cap):
        return None
    target = target_session(ctx.tw_cal, now_tpe, earliest)
    if target is None:
        return None
    days = _days(ctx.store, OBS_INST3)
    closed = closures(ctx.store)
    for day in recent_sessions(ctx.tw_cal, target, INST3_WINDOW):
        iso = day.isoformat()
        if day == target or iso in days or iso in closed:
            continue
        if _empty_count(state, iso) >= EMPTY_GIVE_UP or _tried_within(state, iso, ctx.now, RETRY_NOT_YET_S):
            continue
        return day
    return None


def _run_bfi(ctx: Any, day: date, mode: str) -> Optional[str]:
    today = _now_tpe(ctx).date()
    state = _sched(ctx.store, TASK_BFI, today)
    iso = day.isoformat()
    try:
        inst = _call(ctx, _tw().fetch_twse_bfi82u, day)
    except _NoRequest as exc:
        return _skipped(exc)
    except Exception as exc:
        if not _is_source_error(exc):
            raise
        _note_try(state, iso, ctx.now)
        if _is_parse_error(exc) and _kind(exc) in _NO_DATA_KINDS:
            _note_empty(state, iso)
            _save(ctx.store, TASK_BFI, state)
            if mode == "target":
                ctx.store.obs_not_yet(OBS_INST3)
            return NOT_YET
        _save(ctx.store, TASK_BFI, state)
        if mode == "target":
            ctx.store.obs_checked(OBS_INST3, error_kind=_kind(exc))
        raise _for_framework(exc) from None
    _note_try(state, iso, ctx.now)
    _save(ctx.store, TASK_BFI, state)
    summary = {
        "foreign": int(inst.foreign),
        "trust": int(inst.trust),
        "dealer_self": int(inst.dealer_self),
        "dealer_hedge": int(inst.dealer_hedge),
    }
    _merge_days(ctx.store, OBS_INST3, inst.date, summary, KEEP_ALL_SESSIONS)
    return None


def _bfi_due(ctx: Any) -> bool:
    return bfi_target(ctx) is not None


def _bfi_main(ctx: Any) -> Optional[str]:
    day = bfi_target(ctx)
    return NOT_YET if day is None else _run_bfi(ctx, day, "target")


def _bfi_backfill_due(ctx: Any) -> bool:
    return bfi_backfill(ctx) is not None


def _bfi_backfill_run(ctx: Any) -> Optional[str]:
    day = bfi_backfill(ctx)
    return NOT_YET if day is None else _run_bfi(ctx, day, "backfill")


# ── MI_MARGN（融資；21:30 後，只要最新一天） ─────────────────────────────────────────────
def margin_target(ctx: Any) -> Optional[date]:
    now_tpe = _now_tpe(ctx)
    today = now_tpe.date()
    earliest, cap = _rule("twse_margin_ms")
    state = _sched(ctx.store, TASK_MARGIN, today)
    if state["count"] >= cap:
        return None
    target = target_session(ctx.tw_cal, now_tpe, earliest)
    if target is None:
        return None
    iso = target.isoformat()
    stored = _obs_date(ctx.store, OBS_MARGIN)
    if (stored is not None and stored >= iso) or iso in closures(ctx.store):
        return None
    if _tried_within(state, iso, ctx.now, RETRY_NOT_YET_S):
        return None
    return target


def _margin_run(ctx: Any) -> Optional[str]:
    day = margin_target(ctx)
    if day is None:
        return NOT_YET
    today = _now_tpe(ctx).date()
    state = _sched(ctx.store, TASK_MARGIN, today)
    iso = day.isoformat()
    try:
        margin = _call(ctx, _tw().fetch_twse_margin_ms, day)
    except _NoRequest as exc:
        return _skipped(exc)
    except Exception as exc:
        if not _is_source_error(exc):
            raise
        _note_try(state, iso, ctx.now)
        _save(ctx.store, TASK_MARGIN, state)
        if _is_parse_error(exc) and _kind(exc) in _NO_DATA_KINDS:
            ctx.store.obs_not_yet(OBS_MARGIN)
            return NOT_YET
        ctx.store.obs_checked(OBS_MARGIN, error_kind=_kind(exc))
        raise _for_framework(exc) from None
    _note_try(state, iso, ctx.now)
    _save(ctx.store, TASK_MARGIN, state)
    payload = {
        "date": margin.date.isoformat(),
        "margin_balance_k": int(margin.margin_balance_k),
        "margin_prev_k": int(margin.margin_prev_k),
        "short_units": int(margin.short_units),
        "short_prev_units": int(margin.short_prev_units),
    }
    ctx.store.put_obs(OBS_MARGIN, payload, data_date=margin.date.isoformat())
    return None


def _margin_due(ctx: Any) -> bool:
    return margin_target(ctx) is not None


# ── TWT49U（除權息；bootstrap 分段、之後每天從上次檢查日接續） ─────────────────────────────
def _merge(intervals: list[tuple[date, date]]) -> list[tuple[date, date]]:
    merged: list[tuple[date, date]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def twt49u_chunks(covered: list[tuple[date, date]], start: date, end: date) -> list[tuple[date, date]]:
    """[start, end] 裡還沒涵蓋的部分切成請求區間，新到舊；接在已涵蓋區間後面的那段從上次檢查日開始（多看一天）。"""
    if start > end:
        return []
    merged = _merge([(a, b) for a, b in covered if b >= start - timedelta(days=1) and a <= end])
    gaps: list[tuple[date, date]] = []
    cursor = start
    for a, b in merged:
        if a > cursor:
            gaps.append((cursor, a - timedelta(days=1)))
        cursor = max(cursor, b + timedelta(days=1))
    if cursor <= end:
        gaps.append((cursor, end))
    ends = {b for _, b in merged}
    chunks: list[tuple[date, date]] = []
    for gap_start, gap_end in reversed(gaps):
        if gap_start - timedelta(days=1) in ends:
            gap_start -= timedelta(days=1)
        cur_end = gap_end
        while cur_end >= gap_start:
            cur_start = max(gap_start, cur_end - timedelta(days=TWT49U_CHUNK_DAYS - 1))
            chunks.append((cur_start, cur_end))
            cur_end = cur_start - timedelta(days=1)
    return chunks


def twt49u_parts(ctx: Any) -> tuple[Optional[tuple[date, date]], list[tuple[date, date]], Optional[date], Optional[date]]:
    """(接續段, 歷史段（新到舊）, 目標日, 已涵蓋到哪天)。

    接續段＝從上次檢查日接到「今天（16:00 後）／昨天」的那段（每天的主要請求）；其他沒涵蓋的都是歷史段
    （bootstrap、或很久以前的洞），由 bootstrap 任務用剩下的次數補。目標日＝16:00 已過的最近一個交易日。
    """
    now_tpe = _now_tpe(ctx)
    today = now_tpe.date()
    earliest, _ = _rule("twse_twt49u")
    after = earliest is None or now_tpe.time() >= earliest
    end = today if after else today - timedelta(days=1)
    covered = _coverage(ctx.store)
    chunks = twt49u_chunks(covered, today - timedelta(days=HISTORY_DAYS), end)
    last_end = max((b for _, b in covered), default=None)
    if last_end is not None and last_end < end and chunks and chunks[0][1] == end:
        continuation, historical = chunks[0], chunks[1:]
    else:
        continuation, historical = None, chunks
    return continuation, historical, target_session(ctx.tw_cal, now_tpe, earliest), last_end


def twt49u_core(ctx: Any) -> Optional[tuple[date, date]]:
    """接續段：涵蓋還沒到目標日才打（週末不必再打一次）；正常每天 1 次，還有歷史段時共用 bootstrap 的上限。

    2026-10-23：每天上限 1→2（ENDPOINTS）。次數以台北日期計，凌晨 00:0x 補昨天的那一次會把當天唯一的名額用掉，
    16:00 那次從來沒跑過，0050 細節頁與成分寬度每個交易日都要等到隔天凌晨才換到 T。穩態仍是每天 1 次（16:00 那次
    涵蓋到 T，隔天凌晨就不必再打）；凌晨那次留著當安全網（16:00 沒跑到或失敗時才會到期）。"""
    continuation, historical, target, last_end = twt49u_parts(ctx)
    if continuation is None or target is None or last_end is None or last_end >= target:
        return None
    today = _now_tpe(ctx).date()
    _, cap = _rule("twse_twt49u")
    if historical:
        cap = max(cap, TWT49U_BOOTSTRAP_PER_DAY)
    if _sched(ctx.store, TASK_TWT49U, today)["count"] >= cap:
        return None
    return continuation


def twt49u_bootstrap(ctx: Any) -> Optional[tuple[date, date]]:
    """歷史段（最新的先）：只用當天主要資料預留之外剩下的次數。"""
    _, historical, _, _ = twt49u_parts(ctx)
    if not historical:
        return None
    today = _now_tpe(ctx).date()
    _, cap = _rule("twse_twt49u")
    cap = max(cap, TWT49U_BOOTSTRAP_PER_DAY)
    state = _sched(ctx.store, TASK_TWT49U, today)
    if state["count"] >= cap or not spare_ok(ctx, TASK_TWT49U, state, today, cap):
        return None
    return historical[0]


def _run_twt49u(ctx: Any, chunk: tuple[date, date]) -> Optional[str]:
    start, end = chunk
    today = _now_tpe(ctx).date()
    state = _sched(ctx.store, TASK_TWT49U, today)
    try:
        events = _call(ctx, _tw().fetch_twse_twt49u, start, end)
    except _NoRequest as exc:
        return _skipped(exc)
    except Exception as exc:
        if not _is_source_error(exc):
            raise
        _note_try(state, None, ctx.now)
        _save(ctx.store, TASK_TWT49U, state)
        if not (_is_parse_error(exc) and _kind(exc) == "empty"):  # 這段沒有事件也是「查過了」
            ctx.store.obs_checked(OBS_TWT49U, error_kind=_kind(exc))
            raise _for_framework(exc) from None
        events = []
    else:
        _note_try(state, None, ctx.now)
        _save(ctx.store, TASK_TWT49U, state)
    rows = [
        (item.date, item.code, float(item.pre_close), float(item.ref_price), clean(item.kind, 8) or "權息")
        for item in events
        if valid_code(item.code)
    ]
    with ctx.store.transaction():
        if rows:
            ctx.store.upsert_exrights(rows)
        ctx.store.add_coverage(COVERAGE_TWT49U, start, end)
    check_tw_jumps(ctx.store, cal=ctx.tw_cal)  # 新涵蓋的日子：判斷跳空、補到事件的解除
    latest = _merge(_coverage(ctx.store))[-1]
    ctx.store.put_obs(
        OBS_TWT49U,
        {
            "covered_from": latest[0].isoformat(),
            "covered_to": latest[1].isoformat(),
            "pieces": len(_merge(_coverage(ctx.store))),
        },
        data_date=latest[1].isoformat(),
    )
    return None


def _twt49u_due(ctx: Any) -> bool:
    return twt49u_core(ctx) is not None


def _twt49u_main(ctx: Any) -> Optional[str]:
    chunk = twt49u_core(ctx)
    return NOT_YET if chunk is None else _run_twt49u(ctx, chunk)


def _twt49u_bootstrap_due(ctx: Any) -> bool:
    return twt49u_bootstrap(ctx) is not None


def _twt49u_bootstrap_run(ctx: Any) -> Optional[str]:
    chunk = twt49u_bootstrap(ctx)
    return NOT_YET if chunk is None else _run_twt49u(ctx, chunk)


# ── 休市表（今年＋前一年；下一年 11 月起每週試抓） ───────────────────────────────────────
def holiday_year(ctx: Any) -> Optional[int]:
    now_tpe = _now_tpe(ctx)
    today = now_tpe.date()
    state = _sched(ctx.store, TASK_HOLIDAYS, today)
    years = [today.year, today.year - 1]
    if today.month >= HOLIDAY_NEXT_YEAR_MONTH:
        years.append(today.year + 1)
    have = {year: _payload(ctx.store, f"{OBS_HOLIDAYS_PREFIX}{year}").get("rows") is not None for year in years}
    _, cap = _rule("twse_holidays")
    if not have[today.year] or not have[today.year - 1]:
        cap = max(cap, HOLIDAY_BOOTSTRAP_PER_DAY)
    if state["count"] >= cap or not spare_ok(ctx, TASK_HOLIDAYS, state, today, cap):
        return None
    tried = state.get("years") or {}
    for year in years:
        last = (tried.get(str(year)) or {}).get("last")
        recent = isinstance(last, (int, float))
        if not have[year]:
            wait = HOLIDAY_NEXT_YEAR_EVERY_S if year == today.year + 1 else HOLIDAY_RETRY_S
            if recent and ctx.now - float(last) < wait:
                continue
            return year
        if year == today.year:
            obs = ctx.store.get_obs(f"{OBS_HOLIDAYS_PREFIX}{year}") or {}
            success = obs.get("last_success_at")
            fresh = isinstance(success, (int, float)) and ctx.now - float(success) < HOLIDAY_REFRESH_S
            if not fresh and not (recent and ctx.now - float(last) < HOLIDAY_RETRY_S):
                return year
    return None


def _holidays_run(ctx: Any) -> Optional[str]:
    year = holiday_year(ctx)
    if year is None:
        return NOT_YET
    today = _now_tpe(ctx).date()
    state = _sched(ctx.store, TASK_HOLIDAYS, today)
    obs_id = f"{OBS_HOLIDAYS_PREFIX}{year}"
    try:
        rows = _call(ctx, _tw().fetch_twse_holidays, year)
    except _NoRequest as exc:
        return _skipped(exc)
    except Exception as exc:
        if not _is_source_error(exc):
            raise
        _note_try(state, None, ctx.now)
        state.setdefault("years", {})[str(year)] = {"last": float(ctx.now)}
        _save(ctx.store, TASK_HOLIDAYS, state)
        if year == today.year + 1 and _is_parse_error(exc) and _kind(exc) in _NO_DATA_KINDS:
            return NOT_YET  # 下一年的表還沒公布：不算失敗，一週後再試
        if _payload(ctx.store, obs_id).get("rows") is None:
            ctx.store.obs_checked(obs_id, error_kind=_kind(exc))
        raise _for_framework(exc) from None
    _note_try(state, None, ctx.now)
    state.setdefault("years", {})[str(year)] = {"last": float(ctx.now)}
    _save(ctx.store, TASK_HOLIDAYS, state)
    items = []
    unknown = []
    for row in rows:
        kind = str(getattr(row, "kind", "unknown") or "unknown")
        items.append(
            {
                "date": row.date.isoformat(),
                "name": clean(row.name, 24) or "",
                "note": clean(row.note, 80) or "",
                "kind": kind,
            }
        )
        if kind == "unknown":
            unknown.append(row.date.isoformat())
    ctx.store.put_obs(obs_id, {"year": year, "rows": items, "unknown": unknown}, data_date=None, period=str(year))
    return None


def _holidays_due(ctx: Any) -> bool:
    return holiday_year(ctx) is not None


# ── 每天一次的 OpenAPI／MoneyDJ（本益比、月營收、0050 持股） ─────────────────────────────
def daily_due(ctx: Any, name: str, endpoint: str) -> bool:
    now_tpe = _now_tpe(ctx)
    today = now_tpe.date()
    if now_tpe.time() < DAILY_FROM:
        return False
    state = _sched(ctx.store, name, today)
    _, cap = _rule(endpoint)
    if state.get("ok_day") == today.isoformat() or state["count"] >= cap:
        return False
    last = state.get("last_try")
    return not (isinstance(last, (int, float)) and ctx.now - float(last) < DAILY_RETRY_S)


def _daily_fetch(ctx: Any, name: str, obs_id: str, fn: Callable[..., Any]) -> tuple[Any, Optional[str]]:
    """共用：抓一次、記次數；成功回 (結果, None)，帳本擋下回 (None, 狀態)，來源錯誤記 obs 後往上丟。"""
    today = _now_tpe(ctx).date()
    state = _sched(ctx.store, name, today)
    try:
        result = _call(ctx, fn)
    except _NoRequest as exc:
        return None, _skipped(exc)
    except Exception as exc:
        if not _is_source_error(exc):
            raise
        _note_try(state, None, ctx.now)
        _save(ctx.store, name, state)
        ctx.store.obs_checked(obs_id, error_kind=_kind(exc))
        raise _for_framework(exc) from None
    _note_try(state, None, ctx.now)
    state["ok_day"] = today.isoformat()
    _save(ctx.store, name, state)
    return result, None


def _pe_run(ctx: Any) -> Optional[str]:
    result, status = _daily_fetch(ctx, TASK_PE, OBS_PE, _tw().fetch_openapi_bwibbu)
    if result is None:
        return status
    day, table = result
    pe = {code: (float(value) if value is not None else None) for code, value in table.items() if valid_code(code)}
    ctx.store.put_obs(OBS_PE, {"date": day.isoformat(), "pe": pe}, data_date=day.isoformat())
    return None


def _month_end(year: int, month: int) -> date:
    first_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return first_next - timedelta(days=1)


def _revenue_run(ctx: Any) -> Optional[str]:
    table, status = _daily_fetch(ctx, TASK_REVENUE, OBS_REVENUE, _tw().fetch_openapi_revenue)
    if table is None:
        return status
    rows = {
        code: [int(item.year), int(item.month), (float(item.yoy) if item.yoy is not None else None)]
        for code, item in table.items()
        if valid_code(code)
    }
    if not rows:
        ctx.store.obs_checked(OBS_REVENUE, error_kind="empty")
        return PARTIAL
    year, month = max((value[0], value[1]) for value in rows.values())
    period = f"{year:04d}-{month:02d}"
    ctx.store.put_obs(
        OBS_REVENUE, {"period": period, "rows": rows}, data_date=_month_end(year, month).isoformat(), period=period
    )
    return None


def _holdings_run(ctx: Any) -> Optional[str]:
    holdings, status = _daily_fetch(ctx, TASK_HOLDINGS, OBS_HOLDINGS, _tw().fetch_moneydj_0050)
    if holdings is None:
        return status
    rows = []
    for item in holdings.rows:
        weight = getattr(item, "weight", None)
        if not valid_code(item.symbol) or isinstance(weight, bool) or not isinstance(weight, (int, float)):
            continue
        rows.append([item.symbol, clean(item.name, 24), float(weight)])
    if not rows:
        ctx.store.obs_checked(OBS_HOLDINGS, error_kind="too_few")
        return PARTIAL
    as_of = holdings.as_of.isoformat() if getattr(holdings, "as_of", None) else None
    payload = {
        "source": "moneydj",
        "as_of": as_of,
        "rows": rows,
        "count": len(rows),
        "dropped_count": int(getattr(holdings, "dropped_count", 0) or 0),
        "unmapped_weight": float(getattr(holdings, "unmapped_weight", 0.0) or 0.0),
    }
    ctx.store.put_obs(OBS_HOLDINGS, payload, data_date=as_of or _now_tpe(ctx).date().isoformat())
    return None


# ── 期交所（07:00 起、間隔 2 小時、每天 9 次、匯率 2 次；資料日以回應為準，常晚一天） ─────────────────
@dataclass(frozen=True)
class TaifexSpec:
    task: str
    endpoint: str  # market_sources_tw.ENDPOINTS 的鍵
    fetch_name: str
    obs_id: str
    build: Callable[[Any], tuple[dict, date]]
    merge: Optional[Callable[[dict, dict], dict]] = None  # (舊 payload, 新 payload) → 要存的 payload（留歷史）


def _position(value: Any) -> dict:
    return {key: int(getattr(value, key)) for key in value._fields}


def _build_futures(result: Any) -> tuple[dict, date]:
    foreign = {code: _position(pos) for code, pos in sorted(result.foreign.items())}
    return {"date": result.date.isoformat(), "foreign": foreign}, result.date


def _merge_futures(old: dict, new: dict) -> dict:
    """外資台指期只回最新一天：自己留最近 10 個資料日，publish 才算得出日變化（矛盾「指數漲、外資期貨空單增加」）。"""
    days = dict(old.get("days") or {}) if isinstance(old.get("days"), dict) else {}
    if not days and isinstance(old.get("date"), str) and isinstance(old.get("foreign"), dict):
        days[old["date"]] = old["foreign"]  # 舊格式（只有最新一天）也接得上
    days[new["date"]] = new["foreign"]
    kept = sorted(days)[-KEEP_ALL_SESSIONS:]
    return {**new, "days": {key: days[key] for key in kept}, "latest": kept[-1]}


def _build_options_inst(result: Any) -> tuple[dict, date]:
    payload = {
        "date": result.date.isoformat(),
        "foreign_call": _position(result.foreign_call),
        "foreign_put": _position(result.foreign_put),
    }
    return payload, result.date


def _build_pcr(rows: Any) -> tuple[dict, date]:
    ordered = sorted(rows, key=lambda row: row.date)[-KEEP_ALL_SESSIONS:]
    items = [
        {
            "date": row.date.isoformat(),
            "put_volume": int(row.put_volume),
            "call_volume": int(row.call_volume),
            "volume_ratio_pct": float(row.volume_ratio_pct),
            "put_oi": int(row.put_oi),
            "call_oi": int(row.call_oi),
            "oi_ratio_pct": float(row.oi_ratio_pct),
        }
        for row in ordered
    ]
    return {"date": items[-1]["date"], "rows": items}, ordered[-1].date


def _build_txo(rows: Any) -> tuple[dict, date]:
    days = {row.date for row in rows if getattr(row, "date", None) is not None}
    if len(days) != 1:
        raise TypeError("txo rows without a single data date")
    day = days.pop()
    items = [[row.month, float(row.strike), row.kind, row.oi, row.settle] for row in rows]
    return {"date": day.isoformat(), "fields": ["month", "strike", "kind", "oi", "settle"], "rows": items}, day


def _build_fx(row: Any) -> tuple[dict, date]:
    payload = {
        "date": row.date.isoformat(),
        "usd_ntd": float(row.usd_ntd),
        "prev_date": row.prev_date.isoformat() if row.prev_date else None,
        "prev_usd_ntd": float(row.prev_usd_ntd) if row.prev_usd_ntd is not None else None,
    }
    return payload, row.date


TAIFEX_SPECS: tuple[TaifexSpec, ...] = (
    TaifexSpec(
        "tw.taifex.futures", "taifex_futures_inst", "fetch_taifex_futures_inst", OBS_FUTURES, _build_futures,
        merge=_merge_futures,
    ),
    TaifexSpec(
        "tw.taifex.options_inst",
        "taifex_calls_puts_inst",
        "fetch_taifex_calls_puts_inst",
        OBS_OPTIONS_INST,
        _build_options_inst,
    ),
    TaifexSpec("tw.taifex.pcr", "taifex_pcr", "fetch_taifex_pcr", OBS_PCR, _build_pcr),
    TaifexSpec("tw.taifex.txo", "taifex_daily_options", "fetch_taifex_daily_options", OBS_TXO, _build_txo),
    TaifexSpec("tw.taifex.fx", "taifex_fx", "fetch_taifex_fx", OBS_FX, _build_fx),
)


def taifex_due(ctx: Any, spec: TaifexSpec) -> bool:
    """手上的資料日比目標日舊、過了端點的最早時間、離上次 ≥2 小時、今天還有次數。

    目標日＝已過 15:00 的最近一個交易日（target_session）：今天要過 15:00 才算今天，所以平日早上手上已有
    前一個交易日就不打。2026-10-18 拿掉「15:00 前一律不打」：剛部署、Mac 睡著、前一天沒抓到時，早上就補
    前一個交易日（期交所還沒更新就回 NOT_YET，照樣受每天次數與間隔限制）。

    2026-10-23：端點的最早時間（07:00）前不打。期交所約 06:36 才放上前一個交易日的資料，以前 00／03／06 點那三次
    一定拿到舊的，剩下的三次又剛好碰上 CDN 給 CSV 的時段就整天沒資料；現在間隔 2 小時、每天最多 9 次
    （07、09…23 點；匯率每天 2 次：07、09 點）。"""
    now_tpe = _now_tpe(ctx)
    today = now_tpe.date()
    earliest, cap = _rule(spec.endpoint)
    if earliest is not None and now_tpe.time() < earliest:
        return False
    state = _sched(ctx.store, spec.task, today)
    if state["count"] >= cap:
        return False
    target = target_session(ctx.tw_cal, now_tpe, TAIFEX_EARLIEST)
    stored = _obs_date(ctx.store, spec.obs_id)
    if target is not None and stored is not None and stored >= target.isoformat():
        return False
    last = state.get("last_try")
    return not (isinstance(last, (int, float)) and ctx.now - float(last) < TAIFEX_EVERY_S)


def _taifex_run(ctx: Any, spec: TaifexSpec) -> Optional[str]:
    if not taifex_due(ctx, spec):
        return NOT_YET
    today = _now_tpe(ctx).date()
    state = _sched(ctx.store, spec.task, today)
    try:
        result = _call(ctx, getattr(_tw(), spec.fetch_name))
    except _NoRequest as exc:
        return _skipped(exc)
    except Exception as exc:
        if not _is_source_error(exc):
            raise
        _note_try(state, None, ctx.now)
        _save(ctx.store, spec.task, state)
        if _is_parse_error(exc) and _kind(exc) == "empty":
            ctx.store.obs_not_yet(spec.obs_id)
            return NOT_YET
        ctx.store.obs_checked(spec.obs_id, error_kind=_kind(exc))
        raise _for_framework(exc) from None
    _note_try(state, None, ctx.now)
    _save(ctx.store, spec.task, state)
    payload, day = spec.build(result)
    stored = _obs_date(ctx.store, spec.obs_id)
    if stored is not None and day.isoformat() <= stored:
        ctx.store.obs_not_yet(spec.obs_id)  # 還是舊的那天：期交所常晚一天，不算失敗
        return NOT_YET
    if spec.merge is not None:
        payload = spec.merge(_payload(ctx.store, spec.obs_id), payload)
    ctx.store.put_obs(spec.obs_id, payload, data_date=day.isoformat())
    return None


# ── FinMind（0050 成分＋0050＋加權指數的 bootstrap 與補洞） ───────────────────────────────
def _closes_map(store: Any, market: str, code: str) -> dict[date, tuple[float, Optional[int], str]]:
    return {row[0]: (float(row[1]), row[2], str(row[3])) for row in store.closes(market, code) or []}


def twse_closes(store: Any, code: str) -> dict[date, float]:
    """已存的證交所收盤（tw_all 最近 10 天＋tw 裡來源是證交所的）。"""
    out = {day: value[0] for day, value in _closes_map(store, MARKET, code).items() if value[2] == SOURCE_TWSE}
    out.update({day: value[0] for day, value in _closes_map(store, MARKET_ALL, code).items()})
    return out


def overlap_check(twse: dict[date, float], bars: list, need: int) -> str:
    """FinMind 和證交所比最近 need 個交易日（need=0 ＝範圍內全部）：ok／mismatch／defer（證交所天數不夠）。

    只比 FinMind 這次涵蓋的日期範圍內、證交所有收盤的日子；FinMind 缺了其中一天也算對不上。
    """
    if not bars:
        return "defer"
    fin = {bar.date: float(bar.close) for bar in bars}
    first, last = min(fin), max(fin)
    common = sorted(day for day in twse if first <= day <= last)
    recent = common[-need:] if need > 0 else common
    if need > 0 and len(recent) < need:
        return "defer"
    for day in recent:
        if day not in fin or abs(fin[day] - twse[day]) > FINMIND_TOLERANCE + 1e-9:
            return "mismatch"
    return "ok"


def _volume_of(code: str, bar: Any) -> int:
    # 加權指數那一列的量＝成交金額（元），和 ALLBUT 的「總計(1~15)」同口徑
    return int(bar.money) if code == SYM_TAIEX else int(bar.volume)


def _finmind_info(state: dict, code: str) -> dict:
    return dict((state.get("symbols") or {}).get(code) or {})


def finmind_next(ctx: Any, state: dict, *, skip: Optional[set] = None) -> Optional[tuple[str, date, date, str]]:
    """下一個要打的 FinMind 請求：(代號, 起, 迄, 模式)；模式 bootstrap／full／gap。沒事回 None。"""
    today = _now_tpe(ctx).date()
    window_start = today - timedelta(days=HISTORY_DAYS)
    latest_allbut = max(_days(ctx.store, OBS_ALLBUT), default=None)
    window: Optional[list[date]] = None
    for code in tracked_codes(ctx.store):
        if skip and code in skip:
            continue
        info = _finmind_info(state, code)
        rejected = info.get("rejected_on")
        if rejected and (today - date.fromisoformat(rejected)).days < FINMIND_REJECT_RETRY_DAYS:
            continue
        if info.get("tried_on") == today.isoformat() and info.get("last") in ("empty", "format", "defer"):
            continue
        if not info.get("accepted_on"):
            # 第一次接入要先有 5 天證交所收盤可比：還不夠就先不打（不浪費 FinMind 次數）
            if len(twse_closes(ctx.store, code)) < FINMIND_OVERLAP:
                continue
            return (code, window_start, today, "bootstrap")
        if info.get("needs_full"):
            return (code, window_start, today, "full")
        if latest_allbut is None:
            continue
        end = date.fromisoformat(latest_allbut)
        checked = info.get("checked_to")
        checked_day = date.fromisoformat(checked) if checked else window_start
        if window is None:
            window = [day for day in recent_sessions(ctx.tw_cal, end, KEEP_TRACKED_SESSIONS) if day >= window_start]
        have = {row[0] for row in ctx.store.closes(MARKET, code)}
        missing = [day for day in window if checked_day < day <= end and day not in have]
        if missing:
            back = recent_sessions(ctx.tw_cal, min(missing), FINMIND_OVERLAP + 1)
            start = max(window_start, back[-1] if back else min(missing))
            return (code, start, today, "gap")
    return None


def _write_finmind(store: Any, code: str, bars: list, *, replace: bool, cal: Any = None) -> None:
    """FinMind 寫進長歷史（一個交易）：證交所的列優先，不被 FinMind 蓋掉；replace＝整檔先刪後寫。"""
    with store.transaction():
        existing = _closes_map(store, MARKET, code)
        twse_rows = [(day, value[0], value[1]) for day, value in sorted(existing.items()) if value[2] == SOURCE_TWSE]
        twse_days = {row[0] for row in twse_rows}
        fin_rows = [(bar.date, float(bar.close), _volume_of(code, bar)) for bar in bars if bar.date not in twse_days]
        if replace and fin_rows:
            store.replace_symbol(MARKET, code, fin_rows, SOURCE_FINMIND)
            if twse_rows:
                store.upsert_closes(MARKET, code, twse_rows, SOURCE_TWSE)
        elif fin_rows:
            store.upsert_closes(MARKET, code, fin_rows, SOURCE_FINMIND)
        # 新成分：tw_all 裡最近 10 天的證交所收盤一起放進長歷史
        recent = [
            (day, value[0], value[1])
            for day, value in sorted(_closes_map(store, MARKET_ALL, code).items())
            if day not in twse_days
        ]
        if recent:
            store.upsert_closes(MARKET, code, recent, SOURCE_TWSE)
        store.prune(MARKET, KEEP_TRACKED_SESSIONS)
    check_tw_jumps(store, [code], cal)


def _finmind_revised(store: Any, code: str, bars: list) -> bool:
    """同一天已存的 FinMind 值變了（來源修訂）。"""
    stored = {day: value for day, value in _closes_map(store, MARKET, code).items() if value[2] == SOURCE_FINMIND}
    for bar in bars:
        old = stored.get(bar.date)
        if old is not None and (abs(old[0] - float(bar.close)) > 1e-9 or old[1] != _volume_of(code, bar)):
            return True
    return False


def _revoke_seen_closures(ctx: Any, bars: list) -> None:
    """加權指數有某個暫定休市日的資料：撤銷（之後看到資料就撤銷，§18.4）。"""
    state = dict(ctx.store.task_state(STATE_PREFIX + TASK_ALLBUT) or {})
    closed = dict(state.get("closures") or {})
    seen = {bar.date.isoformat() for bar in bars}
    hit = [iso for iso in closed if iso in seen]
    if not hit:
        return
    for iso in hit:
        ctx.store.set_calendar_override(
            CAL_MARKET, date.fromisoformat(iso), OVERRIDE_REVOKED, REVOKE_EVIDENCE, confirmed=False
        )
        closed.pop(iso, None)
    state["closures"] = closed
    ctx.store.set_task_state(STATE_PREFIX + TASK_ALLBUT, state)


def _finmind_timeout(start: date, end: date) -> float:
    """這個請求最多跑多久（和 market_sources_tw.fetch_finmind_prices 的逾時一樣：超過 31 天用 bootstrap 的）。"""
    try:
        tw = _tw()
        endpoint = tw.ENDPOINTS["finmind_prices"].timeout_s
        long_range = tw.FINMIND_BOOTSTRAP_TIMEOUT_S
    except (ImportError, AttributeError, KeyError):
        endpoint, long_range = 15.0, 30.0
    return float(long_range if (end - start).days > 31 else endpoint)


def _finmind_due(ctx: Any) -> bool:
    today = _now_tpe(ctx).date()
    state = _sched(ctx.store, TASK_FINMIND, today)
    stop_until = state.get("stop_until")
    if isinstance(stop_until, (int, float)) and ctx.now < float(stop_until):
        return False
    return finmind_next(ctx, state) is not None


def _finmind_run(ctx: Any) -> Optional[str]:
    if not _finmind_due(ctx):
        return NOT_YET
    tw = _tw()
    today = _now_tpe(ctx).date()
    state = _sched(ctx.store, TASK_FINMIND, today)
    symbols = dict(state.get("symbols") or {})
    state["symbols"] = symbols
    done: set[str] = set()
    stop_error: Optional[BaseException] = None
    status: Optional[str] = None
    try:
        while not ctx.should_stop() and ctx.time_left() > MIN_TIME_LEFT_S:
            job = finmind_next(ctx, state, skip=done)
            if job is None:
                break
            code, start, end, mode = job
            if ctx.time_left() < _finmind_timeout(start, end) + MIN_TIME_LEFT_S:
                status = status or NOT_YET  # 這個請求可能跑到逾時：留到下一輪，不讓 collect 超時
                break
            done.add(code)
            info = _finmind_info(state, code)
            try:
                bars = _call(ctx, tw.fetch_finmind_prices, code, start, end)
            except _NoRequest as exc:
                if exc.kind == "cooldown":
                    stop_error = _fetch_error()("cooldown")
                status = status or NOT_YET
                break
            except Exception as exc:
                if not _is_source_error(exc):
                    raise
                _note_try(state, None, ctx.now)
                kind = _kind(exc)
                if not _is_parse_error(exc) or kind == "status":
                    # 非 200、msg≠success（含次數用完的訊息）：停這輪補洞，先停一小時
                    state["stop_until"] = float(ctx.now) + FINMIND_STOP_S
                    state["stop_kind"] = kind
                    stop_error = _for_framework(exc)
                    break
                info.update({"tried_on": today.isoformat(), "last": kind})
                if kind == "conflict" or mode == "bootstrap":  # 同一天兩個值：這檔這次整檔拒收
                    info.update({"rejected_on": today.isoformat(), "reason": kind})
                symbols[code] = info
                status = PARTIAL
                continue
            _note_try(state, None, ctx.now)
            symbols[code] = finmind_accept(ctx, code, bars, mode, info, today)
            if symbols[code].get("last") != "ok":
                status = PARTIAL
            if code == SYM_TAIEX:
                _revoke_seen_closures(ctx, bars)
    finally:
        _save(ctx.store, TASK_FINMIND, state)
        _finmind_progress(ctx, state)
    if stop_error is not None:
        raise stop_error
    return status


def finmind_accept(ctx: Any, code: str, bars: list, mode: str, info: dict, today: date) -> dict:
    """比對、接入或拒收一檔；回更新後的這檔狀態。"""
    info = dict(info)
    info["tried_on"] = today.isoformat()
    verdict = overlap_check(twse_closes(ctx.store, code), bars, FINMIND_OVERLAP if mode == "bootstrap" else 0)
    if verdict == "defer":
        info["last"] = "defer"
        return info
    if verdict == "mismatch":
        info.update({"last": "mismatch", "rejected_on": today.isoformat(), "reason": "mismatch"})
        return info
    if mode == "gap" and _finmind_revised(ctx.store, code, bars):
        info.update({"last": "revised", "needs_full": True})  # 只拿到一段：下一次整檔重抓、一次替換
        return info
    replace = mode == "full" or (mode == "bootstrap" and _finmind_revised(ctx.store, code, bars))
    _write_finmind(ctx.store, code, bars, replace=replace, cal=ctx.tw_cal)
    # 這次請求涵蓋到今天：最後一個 ALLBUT 日以前都查過了。FinMind 也沒有的那天（停牌、「--」無成交）
    # 不能每 15 分鐘再問一次，所以檢查日至少推到那天
    checked = max(bar.date for bar in bars)
    latest_allbut = max(_days(ctx.store, OBS_ALLBUT), default=None)
    if latest_allbut is not None:
        checked = max(checked, min(date.fromisoformat(latest_allbut), today))
    info.update({"last": "ok", "checked_to": checked.isoformat()})
    for key in ("needs_full", "rejected_on", "reason"):
        info.pop(key, None)
    info.setdefault("accepted_on", today.isoformat())
    return info


def _finmind_progress(ctx: Any, state: dict) -> None:
    codes = tracked_codes(ctx.store)
    symbols = state.get("symbols") or {}
    accepted = [code for code in codes if (symbols.get(code) or {}).get("accepted_on")]
    rejected = {
        code: str((symbols.get(code) or {}).get("reason") or "")
        for code in codes
        if (symbols.get(code) or {}).get("rejected_on")
    }
    payload: dict[str, Any] = {"total": len(codes), "accepted": len(accepted), "rejected": rejected}
    stop_until = state.get("stop_until")
    if state.get("stop_kind") and isinstance(stop_until, (int, float)) and ctx.now < stop_until:
        payload["stopped"] = state["stop_kind"]
    ctx.store.put_obs(OBS_FINMIND, payload, data_date=_now_tpe(ctx).date().isoformat())


# ── 任務清單與註冊 ───────────────────────────────────────────────────────────────────
def _taifex_fns(spec: TaifexSpec) -> tuple[Callable[[Any], bool], Callable[[Any], Optional[str]]]:
    def due(ctx: Any) -> bool:
        return taifex_due(ctx, spec)

    def run(ctx: Any) -> Optional[str]:
        return _taifex_run(ctx, spec)

    return due, run


def _daily_due_fn(name: str, endpoint: str) -> Callable[[Any], bool]:
    def due(ctx: Any) -> bool:
        return daily_due(ctx, name, endpoint)

    return due


def tasks() -> list[market_tasks.Task]:
    """這個模組的全部 collect 任務。休市表放 core 的最前面：bootstrap 第一輪就抓，下一輪的日曆才對。"""
    out = [
        market_tasks.Task(TASK_HOLIDAYS, "core", _holidays_due, _holidays_run, 25.0),
        market_tasks.Task(TASK_ALLBUT, "core", _allbut_due, _allbut_main, 40.0),
        market_tasks.Task(TASK_BFI, "core", _bfi_due, _bfi_main, 25.0),
        market_tasks.Task(TASK_TWT49U, "core", _twt49u_due, _twt49u_main, 25.0),
        market_tasks.Task(TASK_MARGIN, "core", _margin_due, _margin_run, 25.0),
    ]
    for spec in TAIFEX_SPECS:
        due, run = _taifex_fns(spec)
        out.append(market_tasks.Task(spec.task, "core", due, run, 40.0 if spec.task == "tw.taifex.txo" else 25.0))
    out += [
        market_tasks.Task(TASK_ALLBUT + ".backfill", "backfill", _allbut_backfill_due, _allbut_backfill_run, 40.0),
        market_tasks.Task(TASK_BFI + ".backfill", "backfill", _bfi_backfill_due, _bfi_backfill_run, 25.0),
        market_tasks.Task(TASK_TWT49U + ".bootstrap", "backfill", _twt49u_bootstrap_due, _twt49u_bootstrap_run, 25.0),
        market_tasks.Task(TASK_FINMIND, "backfill", _finmind_due, _finmind_run, 150.0),
        market_tasks.Task(TASK_PE, "low", _daily_due_fn(TASK_PE, "openapi_bwibbu"), _pe_run, 25.0),
        market_tasks.Task(TASK_REVENUE, "low", _daily_due_fn(TASK_REVENUE, "openapi_revenue"), _revenue_run, 25.0),
        market_tasks.Task(TASK_HOLDINGS, "low", _daily_due_fn(TASK_HOLDINGS, "moneydj_0050"), _holdings_run, 25.0),
    ]
    return out


def register_all() -> None:
    for task in tasks():
        market_tasks.register(task)


register_all()
