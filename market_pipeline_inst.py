"""機構動向（/盤勢 13F，第一期，2026-10-31）資金流的 collect 任務：us.flows（SOXX、SPY）、tw.flows（0050）。

Andrew 2026-10-31 定案（PLAN_v2 §1、§3.1、§6；IMPL_SPEC_p1 §1、§2）：SOXX、0050 的申購贖回（在外股數的變化）與估算金額上卡，
SPY 只給戰情室。抓與解析在 market_sources_inst；這個模組只負責「到期就抓、和前一份比、寫進 Store」，卡片在 market_publish／
market_dashboard。照 market_pipeline_infl 的寫法借 market_pipeline_us 的 `_run_batch`、`_batch_due`、`current_d`：那邊 import
壞了這邊也一起不載入（沒有它們任務也跑不了），反過來這裡壞了不影響美股（market_tasks.load_modules）。

obs（market_publish 與戰情室讀；IMPL_SPEC_p1 §1 的契約，要改兩邊一起改）：`us.flows.etf.SOXX`、`us.flows.etf.SPY`、
`tw.flows.etf.0050`；data_date＝發行商印的日期（SOXX：iShares 的 as of；SPY：SSGA 那一列的日期；0050：元大的交易日期），
period＝None。payload：
  {"v": 1, "symbol", "source", "source_url", "unit"（USD／TWD）, "shares_out", "shares_out_as_of", "prev_shares_out",
   "prev_as_of", "change", "official_prev_session", "nav", "nav_as_of", "flow", "method": "Δshares×NAV", "derived": true,
   "flag", "change_5d": {"from_as_of", "to_as_of", "change"}|null, "days": [{"as_of", "shares_out", "nav", "flag"}, …]}
- 數字一律存數值、缺值 null，不用 0（股數真的沒變的那天 change＝0、flow＝0.0 是真的值）。資金流天生落後一個 session。
- days：新到舊、最多 10 筆、同一個 as_of 只留一筆（同日期的新數字蓋掉舊的）。SOXX、0050 的頁面只有當天一筆，和已存的
  days 合併；SPY 的檔案本身有每天的歷史，檔案裡的列優先。每一筆帶自己的 flag（2026-10-31 Phase 6 I-1，只增欄位）：最新那筆＝
  payload 的 flag；較舊的那筆和它的前一筆差超過門檻、或以前存的時候已經是 "check" → "check"，否則 null。
- prev＝days 裡比最新那筆舊的第一筆（前一份資料；SOXX、0050 第一次是 null，SPY 是檔案的前一列）。中間漏抓的日子照實
  留著兩個日期（不算成「一天」）。
- change：SOXX、SPY＝shares_out − prev_shares_out；0050＝元大頁上的官方差異數。official_prev_session＝官方差異數的起點：
  元大交易日期的前一個台股交易日（Phase 6 I-2；卡片第一天沒有 prev 時就用它；SOXX、SPY 沒有官方差異數，是 null）。
- nav／nav_as_of：同一份文件、同一天的每單位淨值（日期對不上或拿不到 → 兩個都是 null）；flow＝change × nav（估算，
  四捨五入到小數 2 位），任一為 null → null。同一天重抓時頁面沒給 NAV、已存的有 → 沿用已存的（Phase 6 I-3）。NAV 和前一份
  差超過 NAV_JUMP_LIMIT（50%）→ 不當 NAV（寫 log）：同一天已存的 NAV 還過得了就沿用（複審 R2-1-2），否則 null。
- flag：null｜"check"：|change|／前值 > 門檻（CHECK_THRESHOLD，起始值 20%，在 staging 核對；分割、反分割、抓錯數字都會落在
  這裡；0050 的分母是官方的前一日單位數 shares_out − change）。0050 另外自己算 shares_out − prev_shares_out 和官方差異數比，
  但只在已存的前一份正好是 official_prev_session 時才比（Phase 6 I-2：漏抓一天時中間不只一天，不能比、也不標 check）。
  "check" 照樣寫（存值），卡片不顯示金額與股數、每日維護標出來（jobs/market_snapshot._inst_status）。
- "not_updated"（股數與 NAV 都和 days[0] 一模一樣、但日期較新＝發行商還沒更新，不是「在外股數不變」）：不覆寫 days、
  不寫 obs，只 obs_not_yet，這一項回 not_yet（2 小時後再看）。所以寫進去的 flag 只會是 null 或 "check"。
- change_5d：最新 − 5 個交易日前（days[5]）的股數，印兩個日期（Phase 6 I-1）：days[5] 要正好是 shares_out_as_of 往前第 5 個
  交易日（SOXX、SPY 用美股日曆，0050 用台股日曆），窗口裡 5 段相鄰的變化都沒超過門檻、也沒有哪一天標了 check，
  最新與 days[5] 的差也沒超過門檻；任一不合（分割、反分割、抓錯一天、漏抓好幾天、日曆不知道）→ null。這裡的門檻分母都用
  兩筆裡較小的那筆（market_calc.inst_5d_over，publish 同一條；複審 R2-1-5：往上讀錯 +20～25% 的那天當起點時回程不到 20%）；
  卡上的 flag 分母還是較舊那筆。

任務（market_tasks 的 low 群組：週／月來源之後，重的一輪被 STOPPED 不算失敗，下一輪接著做）：
- us.flows：期間＝current_d（美東收盤＋30 分鐘之後才換日）；項目 ("SOXX", "SPY")——卡片要的 SOXX 先抓，SSGA 主機被擋
  （us.holdings.spy 也打它）不會拖住 SOXX；反過來 iShares 被擋或帳本說不行時這批停下，SPY 留到下一輪（SSGA 的檔有完整
  歷史，晚抓不會少資料）。
- tw.flows：期間＝台北的目標交易日（market_pipeline_tw.target_session 的同一套：日曆說不知道時當平日開市、往前找 32 天），
  最早時間 17:00（元大 16:30 左右上傳；10/28 的樣本上傳時間 16:33）；項目 ("0050",)。不 import market_pipeline_tw
  （它 import 時會登記任務）。
- 每一項：抓到的日期 ≥ 期間 → 寫（有變才寫）、回 "done"；比期間舊 → 比已存的新（或同一天但數字改了）才寫，回 "not_yet"
  （每 2 小時再試、每期最多 6 次）；比已存的還舊 → 不寫。股數到了、NAV 還沒到 → 照寫股數、回 "not_yet"（在 6 次內等 NAV，
  用完才接受沒有金額；Phase 6 I-3）。日期晚於來源時區的今天 → ParseError("date", "future")，不寫。
  失敗（格式、日期、HTTP）由 _run_batch 記在該項的 obs、每期最多 3 次；429／403／冷卻／帳本不夠讓這批停下、不算該項的失敗。
- 開關：來源的主機不在 market_http.ALLOWED_HOSTS 就不抓那一項（兩項都不在＝不到期；比照 TACO）；旗標檔
  `state/market_inst.disabled` 存在時兩個任務都不到期、也不送請求（卡片那邊另外讀同一個旗標檔）。

時間只用 ctx.now；只透過 Context／Store 存取資料。這個模組 import 時只把任務登記進框架的記憶體表，不做任何 I/O。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import market_paths
import market_pipeline_us as us_pipe
import market_sources_inst as src
from market_calc import ParseError, inst_5d_over
from market_tasks import NOT_YET, Task, register

log = logging.getLogger("market_pipeline_inst")

_ET = ZoneInfo("America/New_York")
_TPE = ZoneInfo("Asia/Taipei")

# ── obs 鍵與 payload（market_publish 讀；要改兩邊一起改） ─────────────────────────
OBS_FLOWS = {"SOXX": "us.flows.etf.SOXX", "SPY": "us.flows.etf.SPY", "0050": "tw.flows.etf.0050"}
PAYLOAD_VERSION = 1
METHOD = "Δshares×NAV"
DAYS_KEEP = 10
CHANGE_5D_BACK = 5  # 近 5 日＝最新 − 5 個交易日前（兩天都要在 days 裡、中間沒漏）

# |change|／前值超過就標 "check"（起始值，在 staging 核對；日後加槓桿 ETF 要另訂，大波動期單日超過 20% 不罕見）
CHECK_THRESHOLD = {"SOXX": 0.20, "SPY": 0.20, "0050": 0.20}
# NAV 和前一份差超過這麼多就不當 NAV（Phase 6 審查 R2-2：版面改了取到別的數字；分割那天也會到這裡，那天本來就是 check）
NAV_JUMP_LIMIT = 0.50
_MARKETS = {"SOXX": "us", "SPY": "us", "0050": "tw"}  # 交易日用哪一個市場的日曆

# ── 任務與排程 ──────────────────────────────────────────────────────────────
US_KEY = "us.flows"  # 任務名，也是排程紀錄 task_state "sched:us.flows" 的鍵
TW_KEY = "tw.flows"
US_ITEMS = ("SOXX", "SPY")  # 卡片要的 SOXX 先抓
TW_ITEMS = ("0050",)
NOT_YET_RETRY_S = 7200.0  # 發行商還沒換到這個期間：2 小時後再看
MAX_NOT_YET = 6  # 每期最多看幾次「還沒」
DISABLED_FLAG = "market_inst.disabled"  # state/ 底下的旗標檔：存在就不抓（卡片那邊也讀同一個名字）
TW_EARLIEST = time(17, 0)  # 元大的申購買回清單約 16:30 上傳：17:00 之後「今天」才算目標日
_TW_LOOKBACK_DAYS = 32  # 和 market_pipeline_tw.recent_sessions(…, 1) 一樣往前找 1×2＋30 天
_ZONES = {"SOXX": _ET, "SPY": _ET, "0050": _TPE}  # 「今天」用來源那邊的日期（擋未來的日期）


# ── 和前一份比 ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FlowResult:
    """flow_payload 的結果。payload＝要寫的（None＝不寫）；result：write｜same｜not_updated｜older；nav_missing＝頁面沒給
    同一天的 NAV、已存的同一天也沒有（2026-10-31 Phase 6 I-3：這一項回 not_yet，在「還沒」的次數內再等 NAV）。"""

    payload: Optional[dict]
    result: str
    nav_missing: bool = False


def _iso_date(value: Any) -> Optional[date]:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _stored_items(payload: Any) -> list[dict]:
    items = payload.get("days") if isinstance(payload, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def stored_days(payload: Any) -> list[src.FlowDay]:
    """已存 payload 的 days（新到舊、同日期只留第一筆）；壞掉的筆直接略過（舊版或被改過的 DB 不能讓任務停住）。"""
    out: dict[date, src.FlowDay] = {}
    for item in _stored_items(payload):
        day, shares = _iso_date(item.get("as_of")), item.get("shares_out")
        if day is None or isinstance(shares, bool) or not isinstance(shares, int) or shares <= 0:
            continue
        nav = us_pipe._finite(item.get("nav"))
        out.setdefault(day, src.FlowDay(day, shares, nav if nav is not None and nav > 0 else None))
    return sorted(out.values(), key=lambda item: item.as_of, reverse=True)


def stored_flags(payload: Any) -> set[date]:
    """已存 days 裡自己標了 "check" 的日期（Phase 6 I-1 起每一筆都帶 flag；以前的資料沒有這個欄位＝沒標）。"""
    return {day for day in (_iso_date(item.get("as_of")) for item in _stored_items(payload) if item.get("flag") == "check")
            if day is not None}


def _jump(symbol: str, newer: int, older: int) -> bool:
    """相鄰兩筆的變化超過這檔的門檻（分割、反分割、抓錯數字都會落在這裡）。"""
    return older <= 0 or abs(newer - older) / older > CHECK_THRESHOLD[symbol]


def _nav_jumped(nav: Optional[float], prev: Optional[src.FlowDay]) -> bool:
    """NAV 和前一份差超過 NAV_JUMP_LIMIT；任一邊沒有 NAV 就不比（分割隔天要靠這點放行）。"""
    return nav is not None and prev is not None and prev.nav is not None and abs(nav / prev.nav - 1) > NAV_JUMP_LIMIT


def _flag(symbol: str, latest: src.FlowDay, prev: Optional[src.FlowDay], change: Optional[int],
          official: Optional[int], official_prev: Optional[date]) -> Optional[str]:
    if official is not None:  # 0050：官方差異數過同一條門檻（分母＝官方的前一日單位數），另外和自己算的差額比
        base = latest.shares_out - official
        if base <= 0 or abs(official) / base > CHECK_THRESHOLD[symbol]:
            return "check"
        # 只有已存的前一份正好是官方差異數的起點（前一個台股交易日）才比：漏抓一天時兩個日期中間不只一天，不能比（Phase 6 I-2）
        if (prev is not None and official_prev is not None and prev.as_of == official_prev
                and latest.shares_out - prev.shares_out != official):
            return "check"
        return None
    if change is None or prev is None:
        return None
    return "check" if _jump(symbol, latest.shares_out, prev.shares_out) else None


def _day_json(day: src.FlowDay, flag: Optional[str]) -> dict:
    return {"as_of": day.as_of.isoformat(), "shares_out": day.shares_out, "nav": day.nav, "flag": flag}


def _change_5d(symbol: str, days: list[src.FlowDay], flags: list[Optional[str]],
               sessions: Optional[list[date]]) -> Optional[dict]:
    """近 5 日（Phase 6 I-1）：days[0]、days[5] 要正好是 sessions[0]、sessions[5]（相隔 5 個交易日，中間沒漏），窗口裡 5 段相鄰的
    變化都沒超過門檻、也沒有哪一天自己標了 check，最新與 days[5] 的差也沒超過門檻（inst_5d_over：分母用較小的那筆）；否則 null。"""
    back, limit = CHANGE_5D_BACK, CHECK_THRESHOLD[symbol]
    if len(days) <= back or sessions is None or len(sessions) <= back:
        return None
    if days[0].as_of != sessions[0] or days[back].as_of != sessions[back]:
        return None  # 漏抓過：days[5] 不是 5 個交易日前，跨的天數不對
    if any(flags[index] is not None or inst_5d_over(days[index + 1].shares_out, days[index].shares_out, limit)
           for index in range(back)):
        return None  # 窗口裡有分割、反分割或抓錯的一天
    if inst_5d_over(days[back].shares_out, days[0].shares_out, limit):
        return None
    delta = days[0].shares_out - days[back].shares_out
    return {"from_as_of": days[back].as_of.isoformat(), "to_as_of": days[0].as_of.isoformat(), "change": delta}


def flow_payload(symbol: str, page: src.FlowPage, stored: Any, *, sessions: Optional[list[date]] = None) -> FlowResult:
    """這次抓到的頁面＋已存的 payload → FlowResult。sessions＝最新那天（page.days[0].as_of）往前的交易日（新到舊、含當天，
    sessions_ending 算）；日曆不知道時 None，change_5d 與 official_prev_session 就是 null。規則見模組說明。"""
    endpoint = src.ENDPOINTS[symbol]
    latest = page.days[0]
    kept = stored_days(stored)
    newest = kept[0] if kept else None
    if newest is not None and latest.as_of < newest.as_of:
        return FlowResult(None, "older")
    if (newest is not None and latest.as_of > newest.as_of
            and (latest.shares_out, latest.nav) == (newest.shares_out, newest.nav)):
        return FlowResult(None, "not_updated")
    same_day = next((item for item in kept if item.as_of == latest.as_of), None)
    if latest.nav is None and same_day is not None and same_day.nav is not None:
        latest = src.FlowDay(latest.as_of, latest.shares_out, same_day.nav)  # 同一天重抓沒拿到 NAV：沿用已存的（I-3）
    merged = {item.as_of: item for item in kept if item.as_of < latest.as_of}
    merged.update({item.as_of: item for item in page.days[1:]})  # 檔案裡的歷史列優先（SPY）
    merged[latest.as_of] = latest
    days = sorted(merged.values(), key=lambda item: item.as_of, reverse=True)[:DAYS_KEEP]
    prev = days[1] if len(days) > 1 else None
    nav_missing = latest.nav is None
    if _nav_jumped(latest.nav, prev) or _nav_jumped(latest.nav, same_day):  # 沒有前一份 NAV 時也要和同一天已存的比（R3-1）
        log.warning("market inst nav jump symbol=%s", symbol)  # 只記代號（列舉值），不記來源的數字
        # 不再等 NAV。同一天已存的 NAV 還過得了就沿用（複審 R2-1-2：和頁面沒給 NAV 時一樣），否則不算金額
        kept_nav = same_day.nav if same_day is not None and not _nav_jumped(same_day.nav, prev) else None
        latest = days[0] = src.FlowDay(latest.as_of, latest.shares_out, kept_nav)
    official_prev = sessions[1] if page.official_change is not None and sessions is not None and len(sessions) > 1 else None
    if page.official_change is not None:
        change: Optional[int] = page.official_change
    else:
        change = latest.shares_out - prev.shares_out if prev is not None else None
    flag = _flag(symbol, latest, prev, change, page.official_change, official_prev)
    carried = stored_flags(stored)
    flags = [flag] + ["check" if item.as_of in carried or (index + 1 < len(days) and _jump(
        symbol, item.shares_out, days[index + 1].shares_out)) else None for index, item in enumerate(days[1:], start=1)]
    nav = latest.nav
    payload = {
        "v": PAYLOAD_VERSION,
        "symbol": symbol,
        "source": endpoint.source,
        "source_url": endpoint.url,
        "unit": endpoint.unit,
        "shares_out": latest.shares_out,
        "shares_out_as_of": latest.as_of.isoformat(),
        "prev_shares_out": prev.shares_out if prev is not None else None,
        "prev_as_of": prev.as_of.isoformat() if prev is not None else None,
        "change": change,
        "official_prev_session": official_prev.isoformat() if official_prev is not None else None,
        "nav": nav,
        "nav_as_of": latest.as_of.isoformat() if nav is not None else None,
        "flow": round(change * nav, 2) if change is not None and nav is not None else None,
        "method": METHOD,
        "derived": True,
        "flag": flag,
        "change_5d": _change_5d(symbol, days, flags, sessions),
        "days": [_day_json(item, flags[index]) for index, item in enumerate(days)],
    }
    if payload == stored:
        return FlowResult(None, "same", nav_missing)
    return FlowResult(payload, "write", nav_missing)


def sessions_ending(ctx: Any, symbol: str, day: date, count: int) -> Optional[list[date]]:
    """day（含）往前 count 個交易日，新到舊。SOXX、SPY 用美股日曆（ctx.us_cal）；0050 用台股日曆（ctx.tw_cal，照
    market_pipeline_tw 的同一套：日曆說不知道時當平日開市）。day 本身不是交易日、或美股日曆說不知道 → None。"""
    if _MARKETS[symbol] == "tw":
        if not _tw_session(ctx.tw_cal, day):
            return None
        out, current = [day], day
        for _ in range(count * 2 + 30):
            if len(out) >= count:
                break
            current -= timedelta(days=1)
            if _tw_session(ctx.tw_cal, current):
                out.append(current)
        return out if len(out) == count else None
    try:
        back = ctx.us_cal.sessions_back(day, count)
    except Exception:  # noqa: BLE001 - 日曆壞掉：這一輪不算近 5 日
        return None
    return list(reversed(back)) if len(back) == count and back[-1] == day else None


def _check_not_future(ctx: Any, symbol: str, page: src.FlowPage) -> None:
    today = datetime.fromtimestamp(float(ctx.now), _ZONES[symbol]).date()
    if page.days[0].as_of > today:
        raise ParseError("date", "future")  # 來源給了還沒到的日期（時間不對）：不存


def _collect(ctx: Any, symbol: str, target: date) -> str:
    """一個項目：抓、比、寫；回 "done"（已經有 ≥ target 的資料）或 "not_yet"，失敗丟例外（_run_batch 記在該項的 obs）。"""
    page = src.fetch_flow(symbol, http=ctx.http, ledger=ctx.ledger)
    _check_not_future(ctx, symbol, page)
    obs_id = OBS_FLOWS[symbol]
    stored = us_pipe._obs_payload(ctx, obs_id)
    outcome = flow_payload(symbol, page, stored,
                           sessions=sessions_ending(ctx, symbol, page.days[0].as_of, CHANGE_5D_BACK + 1))
    if outcome.payload is None:
        ctx.store.obs_not_yet(obs_id)  # 只更新 checked_at、不算失敗
    else:
        ctx.store.put_obs(obs_id, outcome.payload, data_date=outcome.payload["shares_out_as_of"])
    if outcome.result == "not_updated" or outcome.nav_missing:
        return "not_yet"  # 發行商還沒更新；或股數到了、NAV 還沒到（I-3）：在「還沒」的次數內再看
    have = page.days[0].as_of
    if outcome.result == "older":
        have = stored_days(stored)[0].as_of
    return "done" if have >= target else "not_yet"


# ── 期間與開關 ───────────────────────────────────────────────────────────────


def inst_disabled(ctx: Any) -> bool:
    """旗標檔 state/market_inst.disabled 存在（或看不了）＝停用。"""
    return market_paths.flag_set(ctx.paths.state_dir / DISABLED_FLAG)


def _tw_session(cal: Any, day: date) -> bool:
    """market_pipeline_tw.expected_session 的同一套：日曆說不知道（None）時先當平日開市；日曆壞掉也一樣。"""
    try:
        value = cal.is_session(day)
    except Exception:  # noqa: BLE001 - 日曆壞掉不能讓這個任務停擺
        value = None
    if value is None:
        return day.weekday() < 5
    return bool(value)


def tw_target(ctx: Any) -> Optional[date]:
    """tw.flows 的期間：17:00（TW_EARLIEST）已過的最近一個預期交易日（market_pipeline_tw.target_session 的同一套）。"""
    now_tpe = datetime.fromtimestamp(float(ctx.now), _TPE)
    today = now_tpe.date()
    if _tw_session(ctx.tw_cal, today) and now_tpe.time() >= TW_EARLIEST:
        return today
    day = today - timedelta(days=1)
    for _ in range(_TW_LOOKBACK_DAYS):
        if _tw_session(ctx.tw_cal, day):
            return day
        day -= timedelta(days=1)
    return None


def _enabled(items: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(symbol for symbol in items if src.enabled(symbol))


def _due(ctx: Any, key: str, period: Optional[date], items: tuple[str, ...]) -> bool:
    if inst_disabled(ctx):
        return False
    return us_pipe._batch_due(ctx, key, us_pipe._iso(period), _enabled(items), max_not_yet=MAX_NOT_YET)


def _run(ctx: Any, key: str, period: Optional[date], items: tuple[str, ...]) -> Optional[str]:
    enabled = _enabled(items)
    if period is None or not enabled or inst_disabled(ctx):
        return NOT_YET  # due 和 run 之間不會變（保險）：什麼請求都不送，不算成功也不算失敗
    return us_pipe._run_batch(ctx, key, period.isoformat(), enabled, lambda symbol: _collect(ctx, symbol, period),
                              obs_for=OBS_FLOWS.get, max_not_yet=MAX_NOT_YET, not_yet_retry_s=NOT_YET_RETRY_S)


def _due_us(ctx: Any) -> bool:
    return _due(ctx, US_KEY, us_pipe.current_d(ctx), US_ITEMS)


def _run_us(ctx: Any) -> Optional[str]:
    return _run(ctx, US_KEY, us_pipe.current_d(ctx), US_ITEMS)


def _due_tw(ctx: Any) -> bool:
    return _due(ctx, TW_KEY, tw_target(ctx), TW_ITEMS)


def _run_tw(ctx: Any) -> Optional[str]:
    return _run(ctx, TW_KEY, tw_target(ctx), TW_ITEMS)


# ── 任務表（low 群組：同一群組內依登記順序，load_modules 讓這個模組排在 market_pipeline_us 之後） ──

TASK_TABLE: tuple[tuple[str, str, str, Callable[[Any], bool], Callable[[Any], Optional[str]], float], ...] = (
    (US_KEY, "low", "us", _due_us, _run_us, 65.0),  # iShares（30 秒上限）＋SSGA（30 秒上限）
    (TW_KEY, "low", "tw", _due_tw, _run_tw, 35.0),  # 元大（30 秒上限）
)


def build_tasks() -> list[Task]:
    return [Task(name=name, group=group, due=due, run=run, budget_s=budget, market=market)
            for name, group, market, due, run, budget in TASK_TABLE]


TASKS: list[Task] = build_tasks()


def register_tasks(register_fn: Callable[[Task], None] = register) -> None:
    for task in TASKS:
        register_fn(task)


register_tasks()  # import 時登記進框架的記憶體表（不做 I/O）
