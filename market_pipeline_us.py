"""市場盤勢（/盤勢，2026-10-17）的美股 collect 任務：持股清單、日線、分割、期權、美債、CFTC、FINRA、
13F、基本面、事件、NYSE 日曆核對。

Andrew 2026-10-17 要一張「目前市場狀況」的盤勢卡（VOO／QQQ／SOXX 的技術面、成分股站上均線的家數、
量能、期權的牆與 gamma、13F 前十大、槓桿、事件…），按了才回、只描述狀態。這個模組只負責「到期就抓、
寫進 Store」：每個任務由 market_tasks 的框架在 collect 子程序裡呼叫；計算與發布在 market_publish
（只讀 SQLite）。

規則（PLAN §3.1、§9、§18.1–18.3、§18.5–18.9、§18.13、§19.2–19.4；INTERFACES §3 A、§8、§9）：
- D＝`market_calendar.latest_closed_us_session`（美東收盤＋30 分鐘之後才算當天）。日線只收 ≤D 的列。
- 日線每檔的抓取區間＝[最後一筆正式（非備援）日期往前 3 個交易日, D]；沒有歷史抓一年；回應沒有 D 列
  → 30 分鐘後重排，最多再排 4 次。每輪（core＋backfill 合計）最多 60 檔。主機被擋（403／429／冷卻）或帳本
  說不行就整批停。
- D 有沒有公布只用 SPY 探（2026-10-23）：前 5 次和其他檔一樣每 30 分鐘，之後每小時再探，直到 SPY 有 D 或 D
  換日；SPY 有 D 之前不打其他 ETF 與成分股（含還沒有歷史的），有了同一輪就接著抓。Nasdaq 公布 D 列比收盤後
  2 小時晚（實測 18:46 美東還沒有、隔天 06:22 已有），以前 4 檔 ETF 各 5 次用完就整個 D 不再抓，成分股從
  10/16 起就沒更新過。
- 覆寫守門：重抓的收盤和已存的同來源正式值差 >0.5% → 不覆寫；那之後有生效的分割 → 整段重抓、一個
  交易內先刪後寫；沒有分割（例如配息回溯調整）→ 待確認，下一輪整段重抓，兩次同來源一致就整段替換。
  新列相鄰兩日 |r−1| ≥15% 而且沒有分割（財報跳空）→ 一樣待確認、兩次一致就接受，不隔離（§18.2）。
  只有「同一來源連續兩次對同一天給不同答案」才記隔離區間（只讓碰到區間的均線失效），之後兩次一致就解除。
- 分割事件表：第一次從歷史起點抓、之後每天從 D−7（停機後從上次涵蓋的終點）接續，涵蓋區間記 coverage。
  分割在（最早已存日, D]、而且之後沒整段重抓過 → 整段重抓；替換後生效日前後還差一個分割倍數（來源還沒
  調整）→ 隔離「分割待調整」、2 小時後再抓；生效日還沒有列 → 不記「做過整段重抓」，稍後再抓（§18.6）。
  比例看不懂的分割照樣記（store 的比例欄不收空值，記成 1:1＝倍數未知），一樣觸發整段重抓。
- CBOE：抓取時間−timestamp >12 小時不收（記 stale）；資料日＝`last_trade_time` 的美東日期；收盤（＋15 分
  延遲）之後產生的檔才當那天的資料。Nasdaq 日線還沒有 D 列時（被擋或只是晚公布），SPY／QQQ／SOXX 的收盤
  改用 CBOE 寫成 provisional，VOO 另抓一個小報價（要 last_trade_time 的美東日期＝D、時間在收盤前 60 秒以後：
  CBOE 寫 15:59:59），Nasdaq 正式列到了直接覆蓋（守門只比同一來源，§18.7；2026-10-23 以前要被擋才寫，從沒觸發）。
- 臨時休市要有獨立證據才寫暫定覆寫（§18.3）：market-info 說今天不是交易日或 previous／next 跳過某個預期
  交易日；或 D 晚上 20:00（美東）之後 CBOE 的 SPY、QQQ 最後成交日都還是 D 的前一交易日。只有 Nasdaq 缺列不算。
- 「還沒公布」呼叫 `obs_not_yet`、任務回 "not_yet"（不算失敗）；抓不到、解析不了記 `obs_checked(kind)`，
  全部失敗才把第一個錯誤丟回框架（框架記 kind、退避），部分成功回 "partial"。帳本說額度不夠（budget）
  不算失敗。`ctx.ledger` 交給每一次抓取；每檔、每批之間看 `ctx.should_stop()`（含這個任務的時間預算）。
- 外部字串（持股名稱、經濟事件名稱）入庫前一律過 `market_calc.clean_external`；CFTC 只存固定標籤與代碼。
- 時間只用 `ctx.now`；只透過 Context／Store 存取資料，不自己開 sqlite、不自己算交易日曆。

obs 鍵與 payload（market_publish 讀；全是 JSON 值，日期一律 ISO 字串）見下方 `OBS_*` 常數的註解；期權鏈用
`chain_from_payload` 轉回 `market_sources_us.OptionChain` 再交給 market_calc。
這個模組 import 時只把任務登記進框架的記憶體表，不做任何 I/O。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

import market_calc
import market_calendar
import market_http
import market_sources_us as src
from market_tasks import Task, error_kind, register

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc

# ── 市場、來源、obs 鍵（market_publish 依這些鍵讀；要改兩邊一起改） ──────────────────

MARKET = "us"
SRC_NASDAQ = "nasdaq"
SRC_CBOE = "cboe"
SPLITS_COVERAGE = "nasdaq.splits"
STATE_PREFIX = "sched:"  # 本模組自己的排程紀錄（和 market_pipeline_tw 同一個慣例），不和框架的欄位打架

ETFS: tuple[str, ...] = ("SPY", "VOO", "QQQ", "SOXX")  # SPY 先抓：交易日曆的「實際成交日」也看它
OPTION_SYMBOLS: tuple[str, ...] = tuple(src.CBOE_OPTION_SYMBOLS)  # QQQ、SPY、SOXX
VIX_SYMBOLS: tuple[str, ...] = tuple(src.CBOE_QUOTE_SYMBOLS)  # VIX、VIX3M
# 沒有期權鏈的 ETF（VOO）：us.options 那批的最後一項只抓一個小報價，收盤當 D 的 provisional（2026-10-23）
CLOSE_QUOTE_SYMBOLS: tuple[str, ...] = tuple(src.CBOE_CLOSE_QUOTE_SYMBOLS)

# 持股清單：{"source", "as_of", "rows": [[代號, 名稱|None, 權重%|None, 市值美元|None], …], "count",
#           "dropped_count", "unmapped_weight", "unmapped_count", "covered_weight"}；data_date＝清單日
OBS_HOLDINGS_SPY = "us.holdings.SPY"  # SSGA SPY 每日持股（代表 VOO：寬度與前十大，Andrew 10/17 選的）
OBS_HOLDINGS_NDX = "us.holdings.NDX"  # Nasdaq-100 名單（QQQ 寬度；有市值、沒有權重）
OBS_HOLDINGS_SOXX = "us.holdings.SOXX"  # MoneyDJ SOXX（月資料；寬度與前十大，只含有美國代號的列）
OBS_HOLDINGS_QQQ = "us.holdings.QQQ"  # MoneyDJ QQQ（月資料；只給前十大權重）
# 寬度成分：{"members": {"VOO": [...], "QQQ": [...], "SOXX": [...]}, "lists": {清單 obs: as_of}, "total"}
OBS_UNIVERSE = "us.universe"
# 日線進度（卡上「歷史資料補到 X/Y 檔」、每日維護）：{"D", "total", "have_d", "missing_d", "exhausted",
#   "pending", "quarantined", "blocked", "waiting_for_source", "vendor", "members_last", "outcomes": {結果: 次數}}；
#   data_date＝D。vendor＝這個 D 遇到 Nasdaq 廠商錯誤的次數；members_last＝成分股最後正式日期的中位數（偏舊那個），
#   jobs/market_snapshot.py 拿它和當下的 D 算「落後幾個交易日」寫進 outcome（2026-10-23）
OBS_HIST_STATUS = "us.hist.status"
# 期權鏈（鍵名同 OptionChain 欄位）：{"symbol", "timestamp_utc", "quote_last_trade_et"（美東）, "spot", "close",
#   "volume", "iv30", "raw_count", "bad_count", "conflict_count", "conflict_oi_known",
#   "fields": CHAIN_FIELDS, "contracts": [[…], …], "conflicts": [[代號, OI|None], …]}；data_date＝最後成交的美東日期
OBS_OPTIONS = {sym: f"us.options.{sym}" for sym in OPTION_SYMBOLS}
# VIX／VIX3M：{"close", "price_change", "price_change_pct", "last_trade_et", "timestamp_utc",
#   "history": [[日期, 收盤], …]（自己存的前幾日，核對 price_change 用）}；data_date＝最後成交的美東日期
OBS_QUOTE = {sym: f"us.quote.{sym}" for sym in VIX_SYMBOLS}
# 美債：{"rows": [[日期, m3, y2, y10, y30], …]}（舊到新，最近 40 列；日變化用自己序列的前一列）
OBS_UST = "us.ust"
# CFTC：{"label", "code", "basis": "consolidated"|"e-mini", "report_date", "oi", "dealer_l", …, "chg_*",
#        "lev_net", "am_net", "lev_net_change", "am_net_change"}（不存來源的合約名稱）
OBS_CFTC = {"es": "us.cftc.es", "nq": "us.cftc.nq"}
CFTC_LABELS = {"es": "S&P 500", "nq": "Nasdaq-100"}
# FINRA：{"rows": [["YYYY-MM", 百萬美元], …]}；data_date＝最新月份的月底、period＝"YYYY-MM"
OBS_MARGIN = "us.margin"
# 前十大（股別合併後）：{"source", "list_obs", "list_as_of", "rows": [{"name", "symbols", "weight", "pick"}]}
OBS_TOP10 = {etf: f"us.top10.{etf}" for etf in ("VOO", "QQQ", "SOXX")}
OBS_13F_PREFIX = "us.13f.sym."  # ＋代號：Inst13F 欄位＋"holder_period_mode"；period＝申報期間的眾數
OBS_FUND_PREFIX = "us.fund.sym."  # ＋代號：{"quarters": [[季度結束日, 千美元|None], …], "market_cap_usd", "market_cap_source"}
# market_cap_source 的值（市值都是整家公司的，股別合併後不必相加）
MARKET_CAP_NDX = "nasdaq100_list"  # Nasdaq-100 名單（每天抓的那份）
MARKET_CAP_SUMMARY = "nasdaq_summary"  # /api/quote/{SYM}/summary（不在 Nasdaq-100 的前十大，每週跟著 us.fund 抓）
OBS_EVENTS_FOMC = "us.events.fomc"  # {"meetings": [{"start", "end", "sep", "decision_utc"}]}
OBS_EVENTS_ECON = "us.events.econ"  # {"days": {日期: [[time_utc, 英文名], …]}}（7 天窗口）
OBS_EVENTS_EARNINGS = "us.events.earnings"  # {"symbols": {代號: {"date", "estimated"}}}
OBS_CALENDAR_CHECK = "us.calendar.check"  # {"T", "mismatches", "overrides", "info"}（每日維護看）

# 前十大的來源：(ETF, 清單 obs, 來源標籤)。VOO 用 SPY 每日持股（卡上寫「依 S&P 500 權重（SPY 每日持股）」）。
TOP10_LISTS: tuple[tuple[str, str, str], ...] = (
    ("VOO", OBS_HOLDINGS_SPY, "SPY 每日持股"),
    ("QQQ", OBS_HOLDINGS_QQQ, "MoneyDJ"),
    ("SOXX", OBS_HOLDINGS_SOXX, "MoneyDJ"),
)
# 寬度成分：(ETF, 清單 obs)
BREADTH_LISTS: tuple[tuple[str, str], ...] = (
    ("VOO", OBS_HOLDINGS_SPY),
    ("QQQ", OBS_HOLDINGS_NDX),
    ("SOXX", OBS_HOLDINGS_SOXX),
)
# 同一公司的次要股別 → 公司鍵（PLAN §3.1：13F 顯示時合併成一家，權重相加）
SHARE_CLASSES: dict[str, str] = {"GOOG": "GOOGL", "FOX": "FOXA", "NWS": "NWSA", "BRK.A": "BRK.B"}

# ── 門檻與預算（起始值，跑一陣子再調） ──────────────────────────────────────────

HIST_MAX_SYMBOLS_PER_RUN = 60
HIST_LOOKBACK_SESSIONS = 3
HIST_BOOTSTRAP_DAYS = 365
HIST_FULL_DAYS = 400  # 整段重抓：Nasdaq 一次最多回約 284 列（≈13 個月）
HIST_KEEP_SESSIONS = 280  # closes 保留期 ≤ Nasdaq 一次能回的列數（§18.6）
HIST_RETRY_S = 30 * 60.0
HIST_MAX_RETRIES = 4  # 沒有 D 列：首抓之後最多再排 4 次
# 2026-10-23：D 有沒有公布只探這一檔（也是交易日曆看實際成交日的那檔）；它的 4 次重排用完後不放棄，改每小時再探
HIST_PROBE_SYMBOL = "SPY"
# SPY 探滿 HIST_MAX_RETRIES 次還沒有 D，就每小時也探 VOO：SPY 自己出狀況（廠商錯誤、待確認）時，閘門不能整天跟著關
# （2026-10-23 審查 r9 gp2 I3）。兩檔都不放棄；D 公布了沒看 ETFS 任一檔有沒有正式列
HIST_BACKUP_PROBE = "VOO"
# 備援探針看時間、不看 SPY 的重排次數：SPY 卡在待確認或回格式錯時根本不會記 miss（2026-10-23 審查 r9 Codex 席 I1）。
# 2.5 小時＝SPY 前 5 次每 30 分鐘探完的時間
HIST_BACKUP_AFTER_S = 2.5 * 3600
_HIST_PROBES = (HIST_PROBE_SYMBOL, HIST_BACKUP_PROBE)
HIST_PROBE_S = 3600.0
GUARD_DIFF = 0.005
JUMP_LIMIT = 0.15
SAME_CLOSE_REL = 1e-6  # 「兩次一致」：同一來源同一天的收盤要相同（只容許浮點誤差）
CONFLICT_DAILY_AFTER = 3  # 連續幾次不一致之後改成每天才重抓一次
SPLIT_RETRY_S = 2 * 3600.0
SPLIT_MIN_FACTOR = 1.2  # 倍數太接近 1 的（例 2% 股票股利）看不出有沒有調整，不判斷
CBOE_MAX_AGE_S = 12 * 3600.0
CBOE_DELAY_S = 15 * 60.0  # 延遲報價：收盤後 15 分鐘產生的檔才算收盤後快照
# CBOE 的 last_trade_time 是 15:59:59（10/22 的 SPY／QQQ／SOXX 實測）：收盤前 60 秒內（含）的最後成交也算收盤（2026-10-23）
CBOE_CLOSE_SLACK_S = 60.0
CBOE_RETRY_S = 30 * 60.0
CBOE_MAX_NOT_YET = 10  # 16:30 起每 30 分鐘一次 → 到 21:30；20:00 之後才能當休市證據
CLOSURE_EVIDENCE_ET = dtime(20, 0)
VIX_HISTORY = 30
UST_ROWS = 40
MARGIN_MONTHS = 25
NASDAQ_BLOCK_VALID_S = 3 * 3600.0  # 冷卻 2 小時；超過 3 小時沒再撞到就不當「被擋」
MARKET_INFO_AFTER_ET = dtime(9, 45)  # 開盤後才核對：盤前 Nasdaq 可能還沒換日
# previous／next 一次跳過超過這麼多個預期交易日：比較像回應壞了（覆寫會立刻生效、未來的日子又沒有資料能
# 推翻它），只記不一致、不寫覆寫，交給每日維護。
MARKET_INFO_MAX_SKIP = 3
EVIDENCE_MAX = 200  # store 的 evidence 欄上限

_BLOCK_KINDS = frozenset({"blocked", "rate_limited", "cooldown"})
_STOP_KINDS = _BLOCK_KINDS | {"budget"}
_NON_FAILURE = frozenset({"budget"})  # 帳本說額度不夠：請求沒有送出（框架也不算失敗）
# 日線回應沒有列：empty＝Nasdaq 沒有（空殼、區間內沒有列）、vendor＝Nasdaq 自己報錯（2026-10-23）；都當「還沒有 D」重排
_NO_ROWS_KINDS = frozenset({"empty", "vendor"})
_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
REASON_CONFLICT = "conflict"
REASON_SPLIT = "split_pending"


# ── 共用小工具 ───────────────────────────────────────────────────────────────


def _kind(exc: BaseException) -> str:
    return error_kind(exc)  # 和框架同一套：FetchError／ParseError 用 kind，其他 unexpected:<型別名>


def _et_now(ctx: Any) -> datetime:
    return datetime.fromtimestamp(float(ctx.now), _ET)


def _today_et(ctx: Any) -> date:
    return _et_now(ctx).date()


def _now_utc(ctx: Any) -> datetime:
    return datetime.fromtimestamp(float(ctx.now), _UTC)


def current_d(ctx: Any) -> Optional[date]:
    """這輪的 D（美東收盤＋30 分鐘之後才算當天；日曆不知道 → None，任務就不跑）。"""
    return market_calendar.latest_closed_us_session(ctx.us_cal, ctx.now)


def _iso(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _parse_iso(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _count(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _jsonable(value: Any) -> Any:
    """dataclass／date／datetime／tuple → JSON 值；非有限浮點數 → None；aware datetime → UTC ISO。"""
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, datetime):
        return value.astimezone(_UTC).isoformat() if value.tzinfo is not None else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value) if isinstance(value, (set, frozenset)) else value
        return [_jsonable(item) for item in items]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _clean(text: Any, max_len: int) -> Optional[str]:
    return market_calc.clean_external(text, max_len) if isinstance(text, str) else None


def _valid_symbol(symbol: Any) -> bool:
    return isinstance(symbol, str) and bool(_SYMBOL_RE.match(symbol))


def _iso_week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _state(ctx: Any, key: str) -> dict:
    raw = ctx.store.task_state(STATE_PREFIX + key)
    return dict(raw) if isinstance(raw, dict) else {}


def _save_state(ctx: Any, key: str, state: dict) -> None:
    ctx.store.set_task_state(STATE_PREFIX + key, state)


def _obs(ctx: Any, obs_id: str) -> Optional[dict]:
    got = ctx.store.get_obs(obs_id)
    return got if isinstance(got, dict) else None


def _obs_payload(ctx: Any, obs_id: str) -> dict:
    got = _obs(ctx, obs_id)
    payload = got.get("payload") if got else None
    return payload if isinstance(payload, dict) else {}


def _obs_date(ctx: Any, obs_id: str) -> Optional[date]:
    got = _obs(ctx, obs_id)
    return _parse_iso(got.get("data_date")) if got and got.get("payload") is not None else None


@dataclass
class _Tally:
    """一次 run 的結果：成功、還沒公布、第一個錯誤、讓整批停下的錯誤 → 框架的 run 狀態。"""

    done: int = 0
    not_yet: int = 0
    error: Optional[BaseException] = None
    stop: Optional[BaseException] = None
    stopped_early: bool = False

    def fail(self, exc: BaseException) -> None:
        if _kind(exc) in _STOP_KINDS:
            self.stop = self.stop or exc
        elif self.error is None:
            self.error = exc

    def status(self) -> Optional[str]:
        """全部失敗 → 丟第一個錯誤；部分成功（或做到一半時間到）→ "partial"；只有還沒公布 → "not_yet"；
        全好 → None（ok）。"""
        problem = self.error or self.stop
        if problem is not None:
            if self.done == 0 and self.not_yet == 0:
                raise problem
            return "partial"
        if self.done == 0 and self.not_yet == 0 and self.stopped_early:
            raise market_http.FetchError("budget")  # 一項都還沒做就到時間了：不算成功也不算失敗
        if self.not_yet and self.done == 0:
            return "not_yet"
        if self.not_yet or self.stopped_early:
            return "partial"  # 下一輪接著做剩下的
        return None


# ── 「一個期間做一次」的批次（期間＝D、ISO 週、預期報告日、月…） ─────────────────────


def _batch_state(ctx: Any, key: str, period: str) -> dict:
    state = _state(ctx, key)
    if state.get("period") != period:
        state = {"period": period, "done": [], "fails": {}, "wait": {}}
    for name, default in (("done", []), ("fails", {}), ("wait", {})):
        state.setdefault(name, default)
    return state


def _actionable(state: dict, item: str, now: float, max_fails: int, max_not_yet: int) -> bool:
    if item in state["done"] or int(state["fails"].get(item, 0)) >= max_fails:
        return False
    wait = state["wait"].get(item)
    if wait:
        count, retry_at = int(wait[0]), float(wait[1])
        if count >= max_not_yet or now < retry_at:
            return False
    return True


def _batch_due(ctx: Any, key: str, period: Optional[str], items: Sequence[str], *, max_fails: int = 3,
               max_not_yet: int = 4) -> bool:
    if period is None or not items:
        return False
    state = _batch_state(ctx, key, period)
    return any(_actionable(state, item, float(ctx.now), max_fails, max_not_yet) for item in items)


def _run_batch(
    ctx: Any,
    key: str,
    period: str,
    items: Sequence[str],
    one: Callable[[str], str],
    *,
    obs_for: Callable[[str], Optional[str]] = lambda _item: None,
    max_fails: int = 3,
    max_not_yet: int = 4,
    not_yet_retry_s: float = 3600.0,
) -> Optional[str]:
    """依序處理這個期間還沒完成的項目。`one(item)` 回 "done" 或 "not_yet"，失敗就丟例外。

    主機被擋／帳本不夠（blocked、rate_limited、cooldown、budget）→ 這批停下、不算該項的失敗次數；其他錯誤
    該項失敗數＋1（滿 `max_fails` 這個期間就不再試）。失敗（budget 除外）記在該項的 obs。回框架的狀態。
    """
    state = _batch_state(ctx, key, period)
    tally = _Tally()
    now = float(ctx.now)
    try:
        for item in items:
            if not _actionable(state, item, now, max_fails, max_not_yet):
                continue
            if ctx.should_stop():
                tally.stopped_early = True
                break
            try:
                result = one(item)
            except Exception as exc:  # noqa: BLE001 - 一項壞掉不影響其他項（PLAN §19.4）
                kind = _kind(exc)
                tally.fail(exc)
                obs_id = obs_for(item)
                if obs_id is not None and kind not in _NON_FAILURE:
                    ctx.store.obs_checked(obs_id, error_kind=kind)
                if kind in _STOP_KINDS:
                    break
                state["fails"][item] = int(state["fails"].get(item, 0)) + 1
                continue
            if result == "done":
                tally.done += 1
                state["done"] = sorted(set(state["done"]) | {item})
                state["wait"].pop(item, None)
            else:
                tally.not_yet += 1
                count = int((state["wait"].get(item) or [0, 0.0])[0]) + 1
                state["wait"][item] = [count, now + float(not_yet_retry_s)]
    finally:
        _save_state(ctx, key, state)
    return tally.status()


# ── 持股清單 ─────────────────────────────────────────────────────────────────


def _holding_rows(rows: Iterable[Any]) -> list[list[Any]]:
    out: list[list[Any]] = []
    for row in rows or ():
        symbol = getattr(row, "symbol", None)
        if not _valid_symbol(symbol):
            continue
        out.append([symbol, _clean(getattr(row, "name", None), 24), _finite(getattr(row, "weight", None)),
                    _finite(getattr(row, "market_cap", None))])
    return out


def _holdings_payload(source: str, holding_list: Any, as_of: Optional[date]) -> dict:
    payload: dict[str, Any] = {"source": source, "as_of": _iso(as_of),
                               "rows": _holding_rows(getattr(holding_list, "rows", ()))}
    payload["count"] = len(payload["rows"])
    for name in ("dropped_count", "unmapped_weight", "unmapped_count", "covered_weight"):
        value = getattr(holding_list, name, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            payload[name] = _jsonable(value)
    return payload


def _store_holding_list(ctx: Any, obs_id: str, source: str, holding_list: Any, as_of: Optional[date],
                        expect: Optional[date]) -> str:
    """存一份清單：日期比已存的舊或一樣 → 不覆寫（obs_not_yet）；清單日期還沒到 expect → "not_yet"。"""
    if as_of is None:
        raise market_calc.ParseError("date")
    stored = _obs_date(ctx, obs_id)
    if stored is not None and as_of <= stored:
        ctx.store.obs_not_yet(obs_id)
    else:
        payload = _holdings_payload(source, holding_list, as_of)
        if not payload["rows"]:
            raise market_calc.ParseError("empty")
        ctx.store.put_obs(obs_id, payload, data_date=as_of.isoformat())
    newest = max(as_of, stored) if stored is not None else as_of
    if expect is not None and newest < expect:
        return "not_yet"
    return "done"


def _due_holdings_spy(ctx: Any) -> bool:
    return _batch_due(ctx, "us.holdings.spy", _iso(current_d(ctx)), ("SPY",), max_not_yet=3)


def _run_holdings_spy(ctx: Any) -> Optional[str]:
    day = current_d(ctx)
    if day is None:
        return None
    # SSGA 在 D 晚上掛的是 D 前一個交易日的持股（實測 10/16 晚上是 As of 15-Oct）。
    expect = ctx.us_cal.prev_session(day)

    def one(_: str) -> str:
        holding_list = src.fetch_ssga_spy(http=ctx.http, ledger=ctx.ledger)
        return _store_holding_list(ctx, OBS_HOLDINGS_SPY, "SSGA", holding_list,
                                   _parse_iso(getattr(holding_list, "as_of", None)), expect)

    return _run_batch(ctx, "us.holdings.spy", day.isoformat(), ("SPY",), one, obs_for=lambda _: OBS_HOLDINGS_SPY,
                      max_not_yet=3, not_yet_retry_s=2 * 3600.0)


def _due_holdings_ndx(ctx: Any) -> bool:
    return _batch_due(ctx, "us.holdings.ndx", _iso(current_d(ctx)), ("NDX",))


def _run_holdings_ndx(ctx: Any) -> Optional[str]:
    day = current_d(ctx)
    if day is None:
        return None

    def one(_: str) -> str:
        holding_list = src.fetch_nasdaq100(http=ctx.http, ledger=ctx.ledger)
        # 名單日期是來源標的那一刻（盤中也會有）；沒有就當 D 的名單。
        as_of = _parse_iso(getattr(holding_list, "as_of", None)) or day
        return _store_holding_list(ctx, OBS_HOLDINGS_NDX, "Nasdaq", holding_list, as_of, None)

    return _run_batch(ctx, "us.holdings.ndx", day.isoformat(), ("NDX",), one, obs_for=lambda _: OBS_HOLDINGS_NDX)


def _moneydj_task(etf: str, obs_id: str) -> tuple[Callable[[Any], bool], Callable[[Any], Optional[str]]]:
    key = f"us.holdings.{etf.lower()}"

    def due(ctx: Any) -> bool:
        return _batch_due(ctx, key, _iso_week(_today_et(ctx)), (etf,))

    def run(ctx: Any) -> Optional[str]:
        def one(_: str) -> str:
            holding_list = src.fetch_moneydj_holdings(etf, http=ctx.http, ledger=ctx.ledger)
            return _store_holding_list(ctx, obs_id, "MoneyDJ", holding_list,
                                       _parse_iso(getattr(holding_list, "as_of", None)), None)

        return _run_batch(ctx, key, _iso_week(_today_et(ctx)), (etf,), one, obs_for=lambda _: obs_id)

    return due, run


_due_holdings_soxx, _run_holdings_soxx = _moneydj_task("SOXX", OBS_HOLDINGS_SOXX)
_due_holdings_qqq, _run_holdings_qqq = _moneydj_task("QQQ", OBS_HOLDINGS_QQQ)


def breadth_members(ctx: Any) -> dict[str, list[str]]:
    """寬度成分：{"VOO": SPY 持股, "QQQ": Nasdaq-100, "SOXX": MoneyDJ SOXX（有美國代號的）}。"""
    members: dict[str, list[str]] = {}
    for etf, obs_id in BREADTH_LISTS:
        rows = _obs_payload(ctx, obs_id).get("rows") or []
        members[etf] = sorted({row[0] for row in rows if isinstance(row, list) and row and _valid_symbol(row[0])})
    return members


def _universe(ctx: Any) -> tuple[list[str], dict[str, list[str]]]:
    members = breadth_members(ctx)
    union: set[str] = set()
    for symbols in members.values():
        union.update(symbols)
    return sorted(union - set(ETFS)), members


def _record_universe(ctx: Any, members: dict[str, list[str]], total: int, day: date) -> None:
    payload = {
        "members": members,
        "lists": {obs_id: _iso(_obs_date(ctx, obs_id)) for _, obs_id in BREADTH_LISTS},
        "total": total,
    }
    previous = _obs(ctx, OBS_UNIVERSE)
    if previous and previous.get("payload") == payload and previous.get("data_date") == day.isoformat():
        return
    ctx.store.put_obs(OBS_UNIVERSE, payload, data_date=day.isoformat())


# ── 日線：抓取、守門、分割 ───────────────────────────────────────────────────

Row = tuple[date, float, Optional[int]]
Split3 = tuple[date, float, float]  # (生效日, 新, 舊)


@dataclass
class _Findings:
    diffs: list[date] = field(default_factory=list)  # 和已存同來源正式值差 >0.5%
    jumps: list[date] = field(default_factory=list)  # 新列相鄰兩日 ≥15%、沒有分割
    split_jumps: list[date] = field(default_factory=list)  # 新列在分割日跳動、看起來來源沒調整（或倍數未知）
    values: dict[date, float] = field(default_factory=dict)

    @property
    def suspect(self) -> list[date]:
        return sorted(set(self.diffs) | set(self.jumps))


@dataclass
class _Job:
    symbol: str
    etf: bool
    kind: str  # "split"、"confirm"、"update"、"bootstrap"
    last_valid: Optional[date] = None
    pending: Optional[dict] = None


def _official(ctx: Any, symbol: str, since: Optional[date] = None) -> dict[date, float]:
    """已存的 Nasdaq 正式收盤（不含 CBOE 備援）：守門只比同一來源。"""
    out: dict[date, float] = {}
    for row in ctx.store.closes(MARKET, symbol, since=since) or ():
        if row[3] == SRC_NASDAQ and not row[4]:
            value = _finite(row[1])
            if value is not None and value > 0:
                out[row[0]] = value
    return out


def _last_valid(ctx: Any, symbol: str) -> Optional[date]:
    return ctx.store.last_date(MARKET, symbol, include_provisional=False)


def _has_official_row(ctx: Any, symbol: str, day: date) -> bool:
    return day in _official(ctx, symbol, since=day)


def _splits(ctx: Any, symbol: str, after: date) -> list[Split3]:
    """生效日 > after 的分割。"""
    out = []
    for row in ctx.store.splits_since(symbol, after) or ():
        execution, new, old = row[1], _finite(row[2]), _finite(row[3])
        if isinstance(execution, date) and new and old and new > 0 and old > 0:
            out.append((execution, new, old))
    return sorted(set(out))


def _range_start(cal: Any, last_valid: date) -> date:
    """[最後一筆有效日期往前 3 個交易日, D] 的起點；日曆算不出來就退 7 個日曆日。"""
    back = cal.sessions_back(last_valid, HIST_LOOKBACK_SESSIONS + 1)
    if len(back) == HIST_LOOKBACK_SESSIONS + 1:
        return back[0]
    return last_valid - timedelta(days=7)


def _rows_le(bars: Iterable[Any], day: date) -> list[Row]:
    """Bar → (日期, 收盤, 量)；只留 ≤D、收盤是正的有限數，舊到新。"""
    rows: dict[date, Row] = {}
    for bar in bars or ():
        bar_day = getattr(bar, "date", None)
        close = _finite(getattr(bar, "close", None))
        if not isinstance(bar_day, date) or isinstance(bar_day, datetime) or close is None or close <= 0:
            continue
        if bar_day > day:  # PLAN §18.8：盤中或下一個交易日的列一律丟掉
            continue
        rows[bar_day] = (bar_day, close, _count(getattr(bar, "volume", None)))
    return [rows[key] for key in sorted(rows)]


def _fetch_rows(ctx: Any, job: _Job, start: date, day: date) -> list[Row]:
    bars = src.fetch_nasdaq_historical(job.symbol, start, day, etf=job.etf, http=ctx.http, ledger=ctx.ledger)
    return _rows_le(bars, day)


def _split_between(splits: Sequence[Split3], after: date, through: date) -> list[Split3]:
    return [split for split in splits if after < split[0] <= through]


def _unknown_ratio(split: Split3) -> bool:
    return split[1] == split[2] == 1.0  # 比例看不懂時存成 1:1（見 _run_splits）


def _looks_unadjusted(before: float, after: float, split: Split3) -> bool:
    """after／before 在對數尺度上離「一個分割倍數」比離 1 近 → 來源沒調整這次分割。"""
    _, new, old = split
    factor = old / new  # 沒調整時價格會變成原來的 舊／新 倍（1:10 合股 → ×10）
    if before <= 0 or after <= 0 or abs(math.log(factor)) < math.log(SPLIT_MIN_FACTOR):
        return False
    ratio = math.log(after / before)
    return abs(ratio - math.log(factor)) < abs(ratio)


def _evaluate(rows: Sequence[Row], official: dict[date, float], splits: Sequence[Split3]) -> _Findings:
    findings = _Findings(values={day: close for day, close, _ in rows})
    for day, close, _ in rows:
        old = official.get(day)
        if old is not None and abs(close / old - 1.0) > GUARD_DIFF:
            findings.diffs.append(day)
    new_days = {day for day, _, _ in rows if day not in official}
    merged = dict(official)
    merged.update(findings.values)
    series = sorted(merged.items())
    for index in range(1, len(series)):
        day, close = series[index]
        if day not in new_days:
            continue
        prev_day, prev_close = series[index - 1]
        if abs(close / prev_close - 1.0) < JUMP_LIMIT:
            continue
        explained = _split_between(splits, prev_day, day)
        if not explained:
            findings.jumps.append(day)
        elif any(_unknown_ratio(split) or _looks_unadjusted(prev_close, close, split) for split in explained):
            findings.split_jumps.append(day)
    return findings


def _same_close(left: Any, right: Any) -> bool:
    a, b = _finite(left), _finite(right)
    if a is None or b is None or b == 0:
        return False
    return abs(a / b - 1.0) <= SAME_CLOSE_REL


def _pending_info(ctx: Any, findings: _Findings, *, disagree: int = 0, quarantined: bool = False,
                  disputed: Iterable[date] = ()) -> dict:
    days = findings.suspect
    span = sorted(set(days) | set(disputed))
    if findings.jumps and findings.diffs:
        kind = "mixed"
    else:
        kind = "jump" if findings.jumps else "revision"
    return {
        "kind": kind,
        "dates": [day.isoformat() for day in days],
        "closes": {day.isoformat(): findings.values[day] for day in days if day in findings.values},
        "disputed": [span[0].isoformat(), span[-1].isoformat()] if span else None,
        "seen_at": float(ctx.now),
        "since": _today_et(ctx).isoformat(),
        "disagree": disagree,
        "quarantined": quarantined,
        "next_at": float(ctx.now) + (86400.0 if disagree >= CONFLICT_DAILY_AFTER else 0.0),
    }


def _note_d(hist: dict, symbol: str, day: date, now: float, rows: Sequence[Row], *, vendor: bool = False) -> str:
    """有 D 列 → 清掉重排；沒有 → 30 分鐘後再排（同一個 D 最多再排 HIST_MAX_RETRIES 次；SPY 用完後改每小時探）。

    vendor＝這次是 Nasdaq 的廠商錯誤（2026-10-23 審查 r7b F2）：一樣排重試、算進同一個上限（不然每輪都重打，
    吃掉每輪 60 檔與帳本額度），只是結果記成 vendor，us.hist.status 才看得到。"""
    retries = hist.setdefault("retry", {})
    if rows and rows[-1][0] == day:
        retries.pop(symbol, None)
        return "ok"
    entry = retries.get(symbol)
    misses = int(entry["n"]) + 1 if entry and entry.get("d") == day.isoformat() else 1
    wait = HIST_PROBE_S if symbol in _HIST_PROBES and misses > HIST_MAX_RETRIES else HIST_RETRY_S
    retries[symbol] = {"d": day.isoformat(), "n": misses, "at": now + wait}
    return "vendor" if vendor else "missing_d"


def _after_replace(ctx: Any, symbol: str, rows: Sequence[Row], day: date, hist: dict) -> str:
    """整段替換後：視窗內每個分割的生效日前後要連續、而且生效日已經有列，才記「做過整段重抓」。"""
    closes = {row_day: close for row_day, close, _ in rows}
    ordered = sorted(closes)
    unadjusted: list[tuple[date, date]] = []
    uncovered = False
    for split in (split for split in _splits(ctx, symbol, ordered[0]) if split[0] <= day):
        execution = split[0]
        before = [d for d in ordered if d < execution]
        after = [d for d in ordered if d >= execution]
        if not after:
            uncovered = True
        elif before and _looks_unadjusted(closes[before[-1]], closes[after[0]], split):
            unadjusted.append((before[-1], after[0]))
    retry = hist.setdefault("split_retry", {})
    if unadjusted:
        for start, end in unadjusted:
            ctx.store.add_quarantine(MARKET, symbol, start, end, REASON_SPLIT)
        retry[symbol] = float(ctx.now) + SPLIT_RETRY_S
        return "split_pending"
    ctx.store.clear_quarantine(MARKET, symbol, REASON_SPLIT)
    if uncovered:
        retry[symbol] = float(ctx.now) + SPLIT_RETRY_S
        return "split_wait"
    retry.pop(symbol, None)
    ctx.store.mark_full_refetch(MARKET, symbol, _today_et(ctx))
    return "replaced"


def _full_refetch(ctx: Any, job: _Job, day: date, hist: dict) -> str:
    """分割觸發：整段重抓（約 13 個月）→ 一個交易內先刪後寫 → 檢查生效日前後連續（PLAN §18.6）。"""
    rows = _fetch_rows(ctx, job, day - timedelta(days=HIST_FULL_DAYS), day)
    if not rows:
        raise market_calc.ParseError("empty")
    ctx.store.replace_symbol(MARKET, job.symbol, rows, SRC_NASDAQ)
    ctx.store.set_pending_jump(MARKET, job.symbol, None)  # 整段替換取代待確認
    outcome = _after_replace(ctx, job.symbol, rows, day, hist)
    _note_d(hist, job.symbol, day, float(ctx.now), rows)
    return outcome


def _accept(ctx: Any, job: _Job, rows: Sequence[Row], day: date, hist: dict, pending: dict, outcome: str) -> str:
    ctx.store.replace_symbol(MARKET, job.symbol, rows, SRC_NASDAQ)
    ctx.store.set_pending_jump(MARKET, job.symbol, None)
    if pending.get("quarantined"):
        ctx.store.clear_quarantine(MARKET, job.symbol, REASON_CONFLICT)
    _note_d(hist, job.symbol, day, float(ctx.now), rows)
    return outcome


def _confirm(ctx: Any, job: _Job, day: date, hist: dict) -> str:
    """待確認的檔：整段重抓，和上次記下的值一樣就接受（整段替換）；不一樣才是真的衝突（隔離區間）。"""
    pending = dict(job.pending or {})
    rows = _fetch_rows(ctx, job, day - timedelta(days=HIST_FULL_DAYS), day)
    if not rows:
        raise market_calc.ParseError("empty")
    fetched = {row_day.isoformat(): close for row_day, close, _ in rows}
    wanted = pending.get("closes") or {}
    dates = [item for item in pending.get("dates") or [] if isinstance(item, str)]
    if dates and all(key in fetched and _same_close(fetched[key], wanted.get(key)) for key in dates):
        return _accept(ctx, job, rows, day, hist, pending, "accepted")
    splits = _splits(ctx, job.symbol, rows[0][0] - timedelta(days=1))
    findings = _evaluate(rows, _official(ctx, job.symbol), splits)
    if findings.split_jumps:
        return _full_refetch(ctx, job, day, hist)
    if not findings.suspect:
        # 這次和已存的一致、新列也沒有跳空：上次那份是一時的錯，照這次整段替換。
        return _accept(ctx, job, rows, day, hist, pending, "resolved")
    # 同一來源連續兩次對同一天給不同答案：記隔離區間（只讓碰到它的均線失效），下一輪再抓。
    earlier = [d for d in (_parse_iso(item) for item in dates) if d is not None]
    info = _pending_info(ctx, findings, disagree=int(pending.get("disagree") or 0) + 1, quarantined=True,
                         disputed=earlier)
    if info["disputed"]:
        start, end = (date.fromisoformat(item) for item in info["disputed"])
        if pending.get("quarantined"):
            ctx.store.clear_quarantine(MARKET, job.symbol, REASON_CONFLICT)
        ctx.store.add_quarantine(MARKET, job.symbol, start, end, REASON_CONFLICT)
    ctx.store.set_pending_jump(MARKET, job.symbol, info)
    return "conflict"


def _apply(ctx: Any, job: _Job, rows: list[Row], official: dict[date, float], day: date, hist: dict, *,
           splits: Sequence[Split3], bootstrap: bool) -> str:
    """一般更新與 bootstrap 共用：守門、分割、跳空待確認，都沒問題才寫。"""
    findings = _evaluate(rows, official, splits)
    if findings.split_jumps:
        return _full_refetch(ctx, job, day, hist)
    if findings.diffs and _split_between(splits, min(findings.diffs), _today_et(ctx)):
        return _full_refetch(ctx, job, day, hist)  # 舊值之後有生效的分割：整段重抓、整批替換（§3.1）
    if findings.suspect:
        # 待確認：可疑日期（含）以後先不寫；和已存值不同（diffs）時連新列也不寫，下一輪整段重抓再決定。
        if not findings.diffs:
            first = min(findings.suspect)
            safe = [row for row in rows if row[0] < first and row[0] not in official]
            if safe:
                ctx.store.upsert_closes(MARKET, job.symbol, safe, SRC_NASDAQ)
        ctx.store.set_pending_jump(MARKET, job.symbol, _pending_info(ctx, findings))
        return "pending"
    if bootstrap:
        ctx.store.replace_symbol(MARKET, job.symbol, rows, SRC_NASDAQ)
        outcome = _after_replace(ctx, job.symbol, rows, day, hist)
        if outcome == "split_pending":
            return outcome
    else:
        # 同來源差 ≤0.5% 與 CBOE 備援（provisional）的列直接覆蓋（§18.7：守門只比同一來源）。
        ctx.store.upsert_closes(MARKET, job.symbol, rows, SRC_NASDAQ)
    return _note_d(hist, job.symbol, day, float(ctx.now), rows)


def _update(ctx: Any, job: _Job, day: date, hist: dict) -> str:
    assert job.last_valid is not None
    start = _range_start(ctx.us_cal, job.last_valid)
    vendor = False
    try:
        rows = _fetch_rows(ctx, job, start, day)
    except market_calc.ParseError as exc:
        if exc.kind not in _NO_ROWS_KINDS:
            raise
        rows = []  # 區間內沒有任何列：當成「還沒有 D」；廠商錯誤一樣排重試，但記成 vendor
        vendor = exc.kind == "vendor"
    if not rows:
        return _note_d(hist, job.symbol, day, float(ctx.now), rows, vendor=vendor)
    lookback = start - timedelta(days=10)  # 多拿幾天：區間第一列和它前一列也要比跳空
    return _apply(ctx, job, rows, _official(ctx, job.symbol, since=lookback), day, hist,
                  splits=_splits(ctx, job.symbol, lookback), bootstrap=False)


def _bootstrap(ctx: Any, job: _Job, day: date, hist: dict) -> str:
    start = day - timedelta(days=HIST_BOOTSTRAP_DAYS)
    vendor = False
    try:
        rows = _fetch_rows(ctx, job, start, day)
    except market_calc.ParseError as exc:
        if exc.kind not in _NO_ROWS_KINDS:
            raise
        rows = []  # BF.B 這種空殼（Nasdaq 沒有這檔）也走這裡：每個 D 最多 5 次、不算失敗
        vendor = exc.kind == "vendor"
    if not rows:
        return _note_d(hist, job.symbol, day, float(ctx.now), rows, vendor=vendor)
    return _apply(ctx, job, rows, {}, day, hist, splits=_splits(ctx, job.symbol, start - timedelta(days=1)),
                  bootstrap=True)


def _do_job(ctx: Any, job: _Job, day: date, hist: dict) -> str:
    if job.kind == "split":
        return _full_refetch(ctx, job, day, hist)
    if job.kind == "confirm":
        return _confirm(ctx, job, day, hist)
    if job.kind == "bootstrap":
        return _bootstrap(ctx, job, day, hist)
    return _update(ctx, job, day, hist)


def _split_due(ctx: Any, symbol: str, day: date, hist: dict) -> bool:
    """分割在（最早已存日, D]、而且之後沒做過整段重抓 → 要整段重抓（PLAN §18.6）。"""
    if float(hist.get("split_retry", {}).get(symbol, 0.0)) > float(ctx.now):
        return False
    splits = [split for split in _splits(ctx, symbol, day - timedelta(days=HIST_FULL_DAYS + 30)) if split[0] <= day]
    if not splits:
        return False
    official = _official(ctx, symbol)
    if not official:
        return False
    earliest = min(official)
    last_full = ctx.store.last_full_refetch(MARKET, symbol)
    return any(earliest < split[0] and (last_full is None or last_full < split[0]) for split in splits)


def _retry_allows(hist: dict, symbol: str, day: date, now: float) -> bool:
    entry = hist.get("retry", {}).get(symbol)
    if not entry or entry.get("d") != day.isoformat():
        return True
    if int(entry.get("n") or 0) > HIST_MAX_RETRIES and symbol not in _HIST_PROBES:
        return False  # 探針不放棄：放棄就整個 D 都不會再抓成分股（2026-10-23）
    return float(entry.get("at") or 0.0) <= now


def _job_for(ctx: Any, symbol: str, etf: bool, day: date, hist: dict, *, allow_bootstrap: bool) -> Optional[_Job]:
    now = float(ctx.now)
    last_valid = _last_valid(ctx, symbol)
    if last_valid is None:
        if allow_bootstrap and _retry_allows(hist, symbol, day, now):
            return _Job(symbol, etf, "bootstrap")
        return None
    if _split_due(ctx, symbol, day, hist):
        return _Job(symbol, etf, "split", last_valid)
    pending = ctx.store.pending_jump(MARKET, symbol)
    if pending:
        if float(pending.get("next_at") or 0.0) <= now:
            return _Job(symbol, etf, "confirm", last_valid, dict(pending))
        return None
    if last_valid < day and _retry_allows(hist, symbol, day, now):
        return _Job(symbol, etf, "update", last_valid)
    return None


def _hist_state(ctx: Any, day: date) -> dict:
    state = _state(ctx, "us.hist")
    state["retry"] = {sym: entry for sym, entry in (state.get("retry") or {}).items()
                      if isinstance(entry, dict) and entry.get("d") == day.isoformat()}
    state["split_retry"] = {sym: at for sym, at in (state.get("split_retry") or {}).items()
                            if float(at) > float(ctx.now)}
    marker = [float(ctx.now), float(getattr(ctx, "deadline", 0.0) or 0.0)]
    if (state.get("run") or {}).get("marker") != marker:
        state["run"] = {"marker": marker, "used": 0}  # 每輪 60 檔：core 與 backfill 共用
    return state


def nasdaq_blocked(ctx: Any) -> bool:
    """最近一次打 Nasdaq 日線是不是被擋（403／429／冷卻）：被擋時 backfill 不排。CBOE 備援以前也看它，2026-10-23
    改成只看「還沒有 D 的正式列」（Nasdaq 只是晚公布時也要寫）。"""
    blocked = _state(ctx, "us.hist").get("blocked")
    if not isinstance(blocked, dict):
        return False
    return float(ctx.now) - float(blocked.get("at") or 0.0) <= NASDAQ_BLOCK_VALID_S


def _run_jobs(ctx: Any, day: date, jobs: list[_Job], hist: dict, tally: _Tally, outcomes: Counter) -> bool:
    """跑一串 job；回 True＝撞到主機被擋或帳本不夠（後面的都不用試了）。"""
    run_info = hist["run"]
    for job in jobs:
        if int(run_info["used"]) >= HIST_MAX_SYMBOLS_PER_RUN:
            return False
        if ctx.should_stop():
            tally.stopped_early = True
            return False
        run_info["used"] = int(run_info["used"]) + 1
        try:
            outcome = _do_job(ctx, job, day, hist)
        except Exception as exc:  # noqa: BLE001 - 一檔壞掉不影響其他檔
            kind = _kind(exc)
            tally.fail(exc)
            outcomes["error:" + kind] += 1
            if job.symbol == HIST_PROBE_SYMBOL and kind not in _STOP_KINDS:  # SPY 自己壞了：下一輪起 VOO 一起探
                hist["probe_error"] = {"d": day.isoformat(), "kind": kind}
            if kind in _BLOCK_KINDS:
                hist["blocked"] = {"kind": kind, "at": float(ctx.now)}
            if kind in _STOP_KINDS:
                if kind == "budget":
                    run_info["used"] = int(run_info["used"]) - 1  # 帳本沒放行：請求沒有送出
                return True
            continue
        hist.pop("blocked", None)
        outcomes[outcome] += 1
        if outcome in ("missing_d", "vendor"):  # 廠商錯誤和「還沒有 D」一樣不算失敗；次數進 us.hist.status
            tally.not_yet += 1
        else:
            tally.done += 1
    return False


def _since_d_change(ctx: Any, day: date) -> float:
    """D 換日（美東收盤＋30 分鐘）到現在幾秒；日曆不知道那天的收盤時間 → 0（不提早探備援）。"""
    try:
        close = ctx.us_cal.close_time(day)
    except Exception:  # noqa: BLE001
        return 0.0
    return float(ctx.now) - (close.timestamp() + 1800.0) if close is not None else 0.0


def _d_published(ctx: Any, day: date) -> bool:
    """Nasdaq 公布 D 了沒：任一檔 ETF 有 D 的正式列（2026-10-23；只看 SPY 的話，SPY 單獨出狀況就整天關著）。"""
    return any(_has_official_row(ctx, symbol, day) for symbol in ETFS)


def _open_gate(hist: dict, day: date) -> None:
    """SPY 有 D 之後的第一輪：清掉其他 ETF 這個 D 的重排紀錄（2026-10-23）。

    舊版在 SPY 有 D 之前就 4 檔 ETF 一起探、各 5 次用完，紀錄留到 D 換日；升級那天不清的話 VOO／QQQ／SOXX 整個 D
    都抓不到。新版 SPY 有 D 之前不碰它們，平常這裡是空的；每個 D 只清一次，之後它們缺 D 照一般的 4 次重排。"""
    if hist.get("gate_open") == day.isoformat():
        return
    hist["gate_open"] = day.isoformat()
    for symbol in ETFS:
        if symbol != HIST_PROBE_SYMBOL:
            hist["retry"].pop(symbol, None)


def _members_last(ctx: Any, universe: list[str]) -> Optional[date]:
    """成分股最後一筆正式日期的中位數（偶數檔取偏舊那個）；還沒有歷史的不算（2026-10-23）。"""
    lasts = sorted(last for last in (_last_valid(ctx, symbol) for symbol in universe) if last is not None)
    return lasts[(len(lasts) - 1) // 2] if lasts else None


def _hist_status(ctx: Any, day: date, universe: list[str], hist: dict, outcomes: Counter) -> None:
    """core 與 backfill 都寫。waiting_for_source 一律看 SPY 有沒有 D（2026-10-23：以前 backfill 寫死 False，
    閘門鎖住時這個旗標在 backfill 跑完到下一次 core 之間會翻成 false）。"""
    symbols = list(ETFS) + universe
    have = sum(1 for symbol in symbols if _has_official_row(ctx, symbol, day))
    quarantine = ctx.store.quarantine(MARKET) or {}
    previous = _obs_payload(ctx, OBS_HIST_STATUS)
    merged = Counter(previous.get("outcomes") or {}) if previous.get("D") == day.isoformat() else Counter()
    merged.update(outcomes)
    payload = {
        "D": day.isoformat(),
        "total": len(symbols),
        "have_d": have,
        "missing_d": len(symbols) - have,
        # SPY 不算：它用完 4 次重排後改每小時探，沒有放棄
        "exhausted": sum(1 for symbol, entry in hist.get("retry", {}).items()
                         if symbol not in _HIST_PROBES and int(entry.get("n") or 0) > HIST_MAX_RETRIES),
        "pending": sum(1 for symbol in symbols if ctx.store.pending_jump(MARKET, symbol)),
        "quarantined": sum(1 for symbol in symbols if quarantine.get(symbol)),
        "blocked": isinstance(hist.get("blocked"), dict),
        "waiting_for_source": not _d_published(ctx, day),
        "vendor": int(merged.get("vendor", 0)),
        "members_last": _iso(_members_last(ctx, universe)),
        "outcomes": dict(sorted(merged.items())),
    }
    ctx.store.put_obs(OBS_HIST_STATUS, payload, data_date=day.isoformat())


def _due_hist(ctx: Any) -> bool:
    return current_d(ctx) is not None


def _run_hist_core(ctx: Any) -> Optional[str]:
    """SPY 先探 D；SPY 有 D 之後才抓其他 ETF＋已有歷史的成分股：分割整段重抓、待確認重抓、
    [最後有效−3 交易日, D] 的日更新。"""
    day = current_d(ctx)
    if day is None:
        return None
    hist = _hist_state(ctx, day)
    universe, members = _universe(ctx)
    tally = _Tally()
    outcomes: Counter = Counter()
    try:
        _record_universe(ctx, members, len(universe) + len(ETFS), day)
        # D 公布前每輪最多就 SPY 這 1 個請求（2026-10-23）；有 D 了這裡只剩它自己的分割／待確認重抓
        probes = [HIST_PROBE_SYMBOL]
        spy_broke = (hist.get("probe_error") or {}).get("d") == day.isoformat()
        if spy_broke or _since_d_change(ctx, day) >= HIST_BACKUP_AFTER_S:
            probes.append(HIST_BACKUP_PROBE)
        jobs = [_job_for(ctx, symbol, True, day, hist, allow_bootstrap=True) for symbol in probes]
        stopped = _run_jobs(ctx, day, [job for job in jobs if job], hist, tally, outcomes)
        if not stopped and _d_published(ctx, day):
            _open_gate(hist, day)
            others = [_job_for(ctx, sym, True, day, hist, allow_bootstrap=True)
                      for sym in ETFS if sym != HIST_PROBE_SYMBOL]
            stopped = _run_jobs(ctx, day, [job for job in others if job], hist, tally, outcomes)
            if not stopped:
                jobs = []
                for symbol in universe:
                    if ctx.should_stop():
                        tally.stopped_early = True
                        break
                    job = _job_for(ctx, symbol, False, day, hist, allow_bootstrap=False)
                    if job is not None:
                        jobs.append(job)
                priority = {"split": 0, "confirm": 1, "update": 2}
                jobs.sort(key=lambda job: (priority.get(job.kind, 9), job.last_valid or date.min, job.symbol))
                _run_jobs(ctx, day, jobs, hist, tally, outcomes)
        if hist.get("pruned_for") != day.isoformat():
            ctx.store.prune(MARKET, HIST_KEEP_SESSIONS)
            hist["pruned_for"] = day.isoformat()
    finally:
        _save_state(ctx, "us.hist", hist)
        _hist_status(ctx, day, universe, hist, outcomes)
    status = tally.status()
    # SPY 還沒有 D：Nasdaq 還沒公布（或 D 臨時休市），還沒公布不算失敗
    return "not_yet" if status is None and not _d_published(ctx, day) else status


def _bootstrap_candidates(ctx: Any, day: date, hist: dict, universe: list[str]) -> list[str]:
    now = float(ctx.now)
    return [sym for sym in universe if _last_valid(ctx, sym) is None and _retry_allows(hist, sym, day, now)]


def _due_hist_backfill(ctx: Any) -> bool:
    day = current_d(ctx)
    # 2026-10-23：還沒有歷史的成分股也等 SPY 有 D——D 公布前只探 SPY；公布前補的一年只到 D−1，公布後還得再抓一次
    if day is None or nasdaq_blocked(ctx) or not _d_published(ctx, day):
        return False
    hist = _hist_state(ctx, day)
    if int(hist["run"]["used"]) >= HIST_MAX_SYMBOLS_PER_RUN:
        return False
    return bool(_bootstrap_candidates(ctx, day, hist, _universe(ctx)[0]))


def _run_hist_backfill(ctx: Any) -> Optional[str]:
    """還沒有任何歷史的成分股：一次抓一年（和 core 共用每輪 60 檔的上限）。"""
    day = current_d(ctx)
    if day is None:
        return None
    hist = _hist_state(ctx, day)
    universe, _ = _universe(ctx)
    tally = _Tally()
    outcomes: Counter = Counter()
    try:
        jobs = [_Job(sym, False, "bootstrap") for sym in _bootstrap_candidates(ctx, day, hist, universe)]
        _run_jobs(ctx, day, jobs, hist, tally, outcomes)
    finally:
        _save_state(ctx, "us.hist", hist)
        if outcomes:
            _hist_status(ctx, day, universe, hist, outcomes)  # 不再帶 waiting：閘門狀態由 SPY 的 D 列決定
    return tally.status()


# ── 分割事件表 ───────────────────────────────────────────────────────────────


def _due_splits(ctx: Any) -> bool:
    return _batch_due(ctx, "us.splits", _iso(current_d(ctx)), ("splits",))


def _run_splits(ctx: Any) -> Optional[str]:
    day = current_d(ctx)
    if day is None:
        return None

    def one(_: str) -> str:
        ends = [end for _, end in ctx.store.coverage(SPLITS_COVERAGE) or ()]
        if not ends:
            since = day - timedelta(days=HIST_FULL_DAYS)  # 第一次：從歷史起點一次抓齊
        else:
            since = min(day - timedelta(days=7), max(ends))  # 停機後從上次涵蓋的終點接續
        rows = []
        for split in src.fetch_nasdaq_splits(since, http=ctx.http, ledger=ctx.ledger) or ():
            if not _valid_symbol(split.symbol) or not isinstance(split.execution_date, date):
                continue
            new, old = _finite(split.new), _finite(split.old)
            if not (new and old and new > 0 and old > 0):
                # 比例看不懂：事件照樣記（只用來觸發整段重抓）。store 的比例欄不收空值 → 記成 1:1＝倍數未知。
                new = old = 1.0
            rows.append((split.symbol, split.execution_date, new, old))
        if rows:
            ctx.store.upsert_splits(rows, SRC_NASDAQ)
        ctx.store.add_coverage(SPLITS_COVERAGE, since, day)
        return "done"

    return _run_batch(ctx, "us.splits", day.isoformat(), ("splits",), one)


# ── CBOE：期權鏈、VIX、備援收盤、休市證據 ──────────────────────────────────────

CHAIN_FIELDS: tuple[str, ...] = ("symbol", "expiry", "kind", "strike", "oi", "volume", "iv", "gamma", "delta")
_CHAIN_SCALARS: tuple[str, ...] = ("spot", "close", "volume", "iv30", "raw_count", "bad_count", "conflict_count",
                                   "conflict_oi_known")


def chain_payload(chain: Any) -> dict:
    """OptionChain → JSON。合約用 CHAIN_FIELDS 順序的 list（SPY 一萬多筆，每筆一個 dict 太大）；
    衝突列存 [代號, OI|None]（market_calc 用它算覆蓋率，沒有它覆蓋率會一律標「下限」）。"""
    last = getattr(chain, "quote_last_trade_et", None)
    payload: dict[str, Any] = {
        "symbol": getattr(chain, "symbol", None),
        "timestamp_utc": _jsonable(getattr(chain, "timestamp_utc", None)),
        "quote_last_trade_et": last.astimezone(_ET).isoformat() if isinstance(last, datetime) else None,
        "fields": list(CHAIN_FIELDS),
        "contracts": [[_jsonable(getattr(item, name, None)) for name in CHAIN_FIELDS]
                      for item in getattr(chain, "contracts", ()) or ()],
        "conflicts": [[getattr(item, "symbol", None), _count(getattr(item, "oi", None))]
                      for item in getattr(chain, "conflicts", ()) or ()],
    }
    for name in _CHAIN_SCALARS:
        payload[name] = _jsonable(getattr(chain, name, None))
    return payload


def chain_from_payload(payload: dict) -> Any:
    """`chain_payload` 的反向：回 `market_sources_us.OptionChain`，到期日還原成 date（market_calc 會拿它和
    代號裡的到期日比對）；衝突列還原成 {"symbol", "oi"}（market_calc 只讀這兩個欄位）。"""
    names = payload.get("fields") or CHAIN_FIELDS
    contracts = []
    for row in payload.get("contracts") or []:
        values = dict(zip(names, row))
        values["expiry"] = _parse_iso(values.get("expiry"))
        contracts.append(src.OptionContract(**{name: values.get(name) for name in CHAIN_FIELDS}))
    conflicts = tuple({"symbol": item[0], "oi": item[1]} for item in payload.get("conflicts") or []
                      if isinstance(item, list) and len(item) == 2)

    def moment(name: str) -> Optional[datetime]:
        raw = payload.get(name)
        return datetime.fromisoformat(raw) if isinstance(raw, str) else None

    return src.OptionChain(
        timestamp_utc=moment("timestamp_utc"),
        spot=payload.get("spot"),
        iv30=payload.get("iv30"),
        contracts=contracts,
        raw_count=payload.get("raw_count"),
        bad_count=payload.get("bad_count"),
        conflict_count=payload.get("conflict_count"),
        conflict_oi_known=payload.get("conflict_oi_known"),
        quote_last_trade_et=moment("quote_last_trade_et"),
        symbol=payload.get("symbol"),
        close=payload.get("close"),
        volume=payload.get("volume"),
        conflicts=conflicts,
    )


def _check_fresh(ctx: Any, timestamp: Any) -> datetime:
    """抓取時間−CBOE timestamp >12 小時 → 不收（PLAN §18.1）。"""
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        raise market_calc.ParseError("date")
    if (_now_utc(ctx) - timestamp).total_seconds() > CBOE_MAX_AGE_S:
        raise market_http.FetchError("stale")
    return timestamp


def _post_close(ctx: Any, data_day: date, timestamp: datetime) -> bool:
    """CBOE 檔是不是那個交易日收盤（＋15 分延遲）之後產生的；盤中的快照不當那天的資料。"""
    close = ctx.us_cal.close_time(data_day)
    if close is None:
        return True
    return timestamp >= close + timedelta(seconds=CBOE_DELAY_S)


def _last_trade_et(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise market_calc.ParseError("date")
    return value.replace(tzinfo=_ET) if value.tzinfo is None else value.astimezone(_ET)


def _note_seen(ctx: Any, symbol: str, last: datetime, timestamp: datetime) -> None:
    state = _state(ctx, "us.options.seen")
    state[symbol] = {"last_trade_et": last.isoformat(), "timestamp_utc": timestamp.isoformat(), "at": float(ctx.now)}
    _save_state(ctx, "us.options.seen", state)


def _maybe_provisional(ctx: Any, day: date, chain: Any, last: datetime) -> bool:
    """Nasdaq 日線還沒有 D 列時用 CBOE 的收盤當 D 的 provisional（PLAN §18.7）：last_trade_time 的美東日期＝D、
    時間在收盤前 60 秒以後；已有 Nasdaq 正式列就不寫。量也照寫，但 market_calc 看 provisional／來源，不拿它算
    20 日量能倍數。chain 也可以是 VOO 的報價（Quote，沒有量）。

    2026-10-23（investigations/freshness_20261023.md §3 第 2 點）：以前還要 Nasdaq 被擋、last_trade_time ≥ 收盤，
    可是 Nasdaq 只是晚公布時不算被擋、CBOE 又寫 15:59:59，實際上從來沒寫過；現在 ETF 列收盤後約 45 分鐘就是 D。"""
    symbol = getattr(chain, "symbol", None)
    if symbol not in ETFS or last.date() != day:
        return False
    close_time = ctx.us_cal.close_time(day)
    if close_time is None or last < close_time - timedelta(seconds=CBOE_CLOSE_SLACK_S):
        return False
    if _has_official_row(ctx, symbol, day):
        return False
    close = _finite(getattr(chain, "close", None))  # 不拿 current_price 頂替：盤前盤後的價不是收盤
    if close is None or close <= 0:
        return False
    # 2026-10-23 審查 r9 gp2 C1：和最近一筆正式收盤比，差到跳空門檻（ETF 分割日 CBOE 已是拆後價、檔案給了離譜值）就不寫、
    # 等 Nasdaq；沒有正式收盤可比也不寫。這條路從這輪起每天都會走，卡上又看不出是備援
    earlier = [value for d, value in sorted(_official(ctx, symbol, since=day - timedelta(days=10)).items()) if d < day]
    if not earlier or abs(close / earlier[-1] - 1.0) >= JUMP_LIMIT:
        return False
    ctx.store.upsert_closes(MARKET, symbol, [(day, close, _count(getattr(chain, "volume", None)))], SRC_CBOE,
                            provisional=True)
    return True


def _close_quotes_wanted(ctx: Any, day: date) -> tuple[str, ...]:
    """要抓 CBOE 收盤報價的 ETF（VOO）：已經有歷史、Nasdaq 日線還沒有 D 的正式列（2026-10-23）。不需要時不列進
    這批：不花請求，也不讓「沒事做」算成完成（期權鏈全部失敗時任務照樣記失敗）。"""
    return tuple(symbol for symbol in CLOSE_QUOTE_SYMBOLS
                 if _last_valid(ctx, symbol) is not None and not _has_official_row(ctx, symbol, day))


def _store_close_quote(ctx: Any, day: date, symbol: str) -> str:
    """沒有期權鏈的 ETF（VOO）：抓 CBOE 約 1 KB 的報價，收盤寫成 D 的 provisional（2026-10-23）。檔還沒到收盤後
    （盤前不更新、過舊）→ 等下一次；資料日已經過了 D → 不必再抓。"""
    if _has_official_row(ctx, symbol, day):
        return "done"  # 排進這批之後 Nasdaq 正式列剛好到了
    quote = src.fetch_cboe_etf_quote(symbol, http=ctx.http, ledger=ctx.ledger)
    timestamp = getattr(quote, "timestamp_utc", None)
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        raise market_calc.ParseError("date")
    last = _last_trade_et(getattr(quote, "last_trade_et", None))
    if last.date() > day:
        return "done"  # 檔已經是下一個場次的：D 的收盤從這裡拿不到了，不再抓
    stale = (_now_utc(ctx) - timestamp).total_seconds() > CBOE_MAX_AGE_S
    if stale or last.date() < day or not _post_close(ctx, day, timestamp):
        return "not_yet"  # 只是備援：ETF 報價檔常常晚更新（實測 VOO 收盤後約 4 小時），不算失敗
    _maybe_provisional(ctx, day, quote, last)
    return "done"


def _store_chain(ctx: Any, day: date, symbol: str, chain: Any) -> str:
    obs_id = OBS_OPTIONS[symbol]
    timestamp = _check_fresh(ctx, getattr(chain, "timestamp_utc", None))
    last = _last_trade_et(getattr(chain, "quote_last_trade_et", None))
    _note_seen(ctx, symbol, last, timestamp)
    data_day = last.date()
    if data_day < day or not _post_close(ctx, data_day, timestamp):
        ctx.store.obs_not_yet(obs_id)
        return "not_yet"
    if _obs_date(ctx, obs_id) == data_day:
        ctx.store.obs_not_yet(obs_id)
    else:
        ctx.store.put_obs(obs_id, chain_payload(chain), data_date=data_day.isoformat())
    _maybe_provisional(ctx, day, chain, last)
    return "done"


def _closure_evidence(ctx: Any, day: date) -> Optional[str]:
    """D 晚上 20:00（美東）之後 SPY、QQQ 的最後成交日都還是 D 的前一交易日 → D 臨時休市的證據（PLAN §18.3）。"""
    et_now = _et_now(ctx)
    evening = datetime.combine(day, CLOSURE_EVIDENCE_ET, _ET)
    prev = ctx.us_cal.prev_session(day)
    if et_now < evening or prev is None or day in (ctx.store.calendar_overrides(MARKET) or {}):
        return None
    seen = _state(ctx, "us.options.seen")
    stamps = []
    for symbol in ("SPY", "QQQ"):
        entry = seen.get(symbol)
        if not isinstance(entry, dict) or float(entry.get("at") or 0.0) < float(ctx.now) - 3600.0:
            return None
        last = datetime.fromisoformat(entry["last_trade_et"]).astimezone(_ET)
        if last.date() != prev or datetime.fromisoformat(entry["timestamp_utc"]) < evening:
            return None
        stamps.append(f"{symbol} {last.strftime('%Y-%m-%d %H:%M')}")
    evidence = f"cboe last_trade still {prev.isoformat()} after 20:00 ET ({', '.join(stamps)})"[:EVIDENCE_MAX]
    ctx.store.set_calendar_override(MARKET, day, market_calendar.OVERRIDE_CLOSED, evidence, confirmed=False)
    return evidence


def _options_items(ctx: Any, day: Optional[date]) -> tuple[str, ...]:
    """期權鏈三檔，需要時最後加 VOO 的收盤報價（排最後：期權鏈每檔最多 30 秒，時間不夠時被擠掉的是最不要緊的）。"""
    return OPTION_SYMBOLS + (_close_quotes_wanted(ctx, day) if day is not None else ())


def _due_options(ctx: Any) -> bool:
    day = current_d(ctx)
    return _batch_due(ctx, "us.options", _iso(day), _options_items(ctx, day), max_not_yet=CBOE_MAX_NOT_YET)


def _run_options(ctx: Any) -> Optional[str]:
    day = current_d(ctx)
    if day is None:
        return None

    def one(symbol: str) -> str:
        if symbol in CLOSE_QUOTE_SYMBOLS:
            return _store_close_quote(ctx, day, symbol)
        chain = src.fetch_cboe_options(symbol, http=ctx.http, ledger=ctx.ledger)
        return _store_chain(ctx, day, symbol, chain)

    try:
        return _run_batch(ctx, "us.options", day.isoformat(), _options_items(ctx, day), one, obs_for=OBS_OPTIONS.get,
                          max_not_yet=CBOE_MAX_NOT_YET, not_yet_retry_s=CBOE_RETRY_S)
    finally:
        _closure_evidence(ctx, day)


def _store_quote(ctx: Any, day: date, symbol: str, quote: Any) -> str:
    obs_id = OBS_QUOTE[symbol]
    timestamp = _check_fresh(ctx, getattr(quote, "timestamp_utc", None))
    last = _last_trade_et(getattr(quote, "last_trade_et", None))
    data_day = last.date()
    close = _finite(getattr(quote, "close", None))
    if close is None or close <= 0:
        raise market_calc.ParseError("missing_field")
    if data_day < day or not _post_close(ctx, data_day, timestamp):
        ctx.store.obs_not_yet(obs_id)
        return "not_yet"
    if _obs_date(ctx, obs_id) == data_day:
        ctx.store.obs_not_yet(obs_id)
        return "done"
    history = {item[0]: item[1] for item in (_obs_payload(ctx, obs_id).get("history") or [])
               if isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)}
    history[data_day.isoformat()] = close
    payload = {
        "close": close,
        "price_change": _finite(getattr(quote, "price_change", None)),
        "price_change_pct": _finite(getattr(quote, "price_change_pct", None)),
        "last_trade_et": last.isoformat(),
        "timestamp_utc": timestamp.astimezone(_UTC).isoformat(),
        # 收盤後 prev_day_close 會等於 close：日變化用 price_change，和自己存的前一日互相核對（§3.1、§18.8）
        "history": [[key, history[key]] for key in sorted(history)[-VIX_HISTORY:]],
    }
    ctx.store.put_obs(obs_id, payload, data_date=data_day.isoformat())
    return "done"


def _due_vix(ctx: Any) -> bool:
    return _batch_due(ctx, "us.vix", _iso(current_d(ctx)), VIX_SYMBOLS, max_not_yet=CBOE_MAX_NOT_YET)


def _run_vix(ctx: Any) -> Optional[str]:
    day = current_d(ctx)
    if day is None:
        return None

    def one(symbol: str) -> str:
        return _store_quote(ctx, day, symbol, src.fetch_cboe_quote(symbol, http=ctx.http, ledger=ctx.ledger))

    return _run_batch(ctx, "us.vix", day.isoformat(), VIX_SYMBOLS, one, obs_for=OBS_QUOTE.get,
                      max_not_yet=CBOE_MAX_NOT_YET, not_yet_retry_s=CBOE_RETRY_S)


# ── NYSE 日曆核對（market-info；PLAN §18.3） ─────────────────────────────────


def _calendar_evidence(ctx: Any, today: date, info: Any) -> list[dict]:
    """market-info 說預期交易日 X 不開：isBusinessDay=false，或 previous／next 跳過 X → 暫定覆寫。

    只在回應確定是「美東今天」的資訊時才核對（收盤時刻是今天，或 previous < 今天 < next）；Nasdaq 還沒
    換日時的回應會把昨天當今天，拿來比會把好好的交易日誤判成休市。"""
    cal = ctx.us_cal
    closing = getattr(info, "closing_time", None)
    closing_day = closing.astimezone(_ET).date() if isinstance(closing, datetime) else None
    previous = _parse_iso(getattr(info, "previous_trade_date", None))
    following = _parse_iso(getattr(info, "next_trade_date", None))
    about_today = closing_day == today or (previous is not None and following is not None
                                            and previous < today < following)
    if not about_today:
        return []
    closed: list[tuple[date, str]] = []
    if getattr(info, "is_business_day", None) is False and cal.is_session(today) is True:
        closed.append((today, "market-info isBusinessDay=false"))
    for bound, backward, label in ((previous, True, "previousTradeDate"), (following, False, "nextTradeDate")):
        if bound is None:
            continue
        step = cal.prev_session if backward else cal.next_session
        skipped: list[date] = []
        cursor = step(today)
        while cursor is not None and (cursor > bound if backward else cursor < bound):
            skipped.append(cursor)
            if len(skipped) > MARKET_INFO_MAX_SKIP:
                break
            cursor = step(cursor)
        if len(skipped) <= MARKET_INFO_MAX_SKIP:
            closed += [(day, f"market-info {label}={bound.isoformat()} skips it") for day in skipped]
    existing = ctx.store.calendar_overrides(MARKET) or {}
    written = []
    stamp = _et_now(ctx).strftime("%Y-%m-%d %H:%M ET")
    for day, why in closed:
        if day in existing:
            continue
        evidence = f"{why} (checked {stamp})"[:EVIDENCE_MAX]
        ctx.store.set_calendar_override(MARKET, day, market_calendar.OVERRIDE_CLOSED, evidence, confirmed=False)
        written.append({"date": day.isoformat(), "evidence": evidence})
    return written


def _calendar_period(ctx: Any) -> Optional[str]:
    et_now = _et_now(ctx)
    if et_now.weekday() >= 5 or et_now.time() < MARKET_INFO_AFTER_ET:
        return None  # 週末回應的語意還沒用 fixture 確認：先不核對（PLAN §18.3）
    return et_now.date().isoformat()


def _due_calendar(ctx: Any) -> bool:
    return _batch_due(ctx, "us.calendar", _calendar_period(ctx), ("market-info",))


def _run_calendar(ctx: Any) -> Optional[str]:
    period = _calendar_period(ctx)
    if period is None:
        return None
    today = date.fromisoformat(period)

    def one(_: str) -> str:
        info = src.fetch_nasdaq_market_info(http=ctx.http, ledger=ctx.ledger)
        mismatches = sorted(set(market_calendar.check_market_info(ctx.us_cal, info, ctx.now) or []))
        written = _calendar_evidence(ctx, today, info)
        payload = {"T": period, "mismatches": mismatches, "overrides": written, "info": _jsonable(info)}
        ctx.store.put_obs(OBS_CALENDAR_CHECK, payload, data_date=period)
        return "done"

    return _run_batch(ctx, "us.calendar", period, ("market-info",), one, obs_for=lambda _: OBS_CALENDAR_CHECK)


# ── 美債 ─────────────────────────────────────────────────────────────────────


def _prev_month(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def _due_ust(ctx: Any) -> bool:
    return _batch_due(ctx, "us.ust", _iso(current_d(ctx)), ("ust",))


def _run_ust(ctx: Any) -> Optional[str]:
    day = current_d(ctx)
    if day is None:
        return None

    def one(_: str) -> str:
        rows: dict[str, list] = {item[0]: item for item in _obs_payload(ctx, OBS_UST).get("rows") or []
                                 if isinstance(item, list) and item and isinstance(item[0], str)}
        months = [day.replace(day=1)]
        if len(rows) < 2:
            months.append(_prev_month(day))  # 冷啟動在月初：上個月的最後一列才有「前一列」
        for month in months:
            for row in src.fetch_treasury(month, http=ctx.http, ledger=ctx.ledger) or ():
                if isinstance(row.date, date) and row.date <= day:
                    rows[row.date.isoformat()] = [row.date.isoformat(), _finite(row.m3), _finite(row.y2),
                                                  _finite(row.y10), _finite(row.y30)]
        if not rows:
            raise market_calc.ParseError("empty")
        latest = max(rows)
        stored = _obs_date(ctx, OBS_UST)
        if stored is not None and latest <= stored.isoformat():
            ctx.store.obs_not_yet(OBS_UST)
        else:
            ctx.store.put_obs(OBS_UST, {"rows": [rows[key] for key in sorted(rows)[-UST_ROWS:]]}, data_date=latest)
        # 美債用自己序列的前一列（哥倫布日債市休市、股市照開）：D 沒有列不算錯，稍後再看一次。
        return "done" if latest >= day.isoformat() else "not_yet"

    return _run_batch(ctx, "us.ust", day.isoformat(), ("ust",), one, obs_for=lambda _: OBS_UST, max_not_yet=4,
                      not_yet_retry_s=3600.0)


# ── CFTC（週）、FINRA（月） ───────────────────────────────────────────────────


def _cftc_expected(ctx: Any) -> date:
    """最近一份「應該已公布」的報告日（週二的部位；週五 15:30 美東公布，遇假日延後）。"""
    et_now = _et_now(ctx)
    tuesday = et_now.date() - timedelta(days=(et_now.weekday() - 1) % 7)
    release = datetime.combine(tuesday + timedelta(days=3), dtime(15, 45), _ET)
    return tuesday if et_now >= release else tuesday - timedelta(days=7)


def _due_cftc(ctx: Any) -> bool:
    return _batch_due(ctx, "us.cftc", _cftc_expected(ctx).isoformat(), ("cftc",), max_not_yet=12)


def _cftc_payload(key: str, row: Any, basis: str) -> dict:
    """只存數字、固定標籤與合約代碼（A 從固定代碼表挑的）；來源的合約名稱是外部字串，不存。"""
    payload = {item.name: _jsonable(getattr(row, item.name)) for item in fields(row) if item.name != "market"}
    payload.update(label=CFTC_LABELS[key], basis=basis)
    for name in ("lev_net", "am_net", "lev_net_change", "am_net_change"):
        payload[name] = getattr(row, name, None)
    return payload


def _run_cftc(ctx: Any) -> Optional[str]:
    expected = _cftc_expected(ctx)

    def one(_: str) -> str:
        # 兩個合約同一種口徑：都有 Consolidated 就用它，否則都用 E-mini（PLAN §3.1；A 的 pick_cftc_contracts）
        pair = src.pick_cftc_contracts(src.fetch_cftc_tff(http=ctx.http, ledger=ctx.ledger))
        report = pair.es.report_date
        for key, row in (("es", pair.es), ("nq", pair.nq)):
            obs_id = OBS_CFTC[key]
            stored = _obs_date(ctx, obs_id)
            if stored is not None and report <= stored:
                ctx.store.obs_not_yet(obs_id)
            else:
                ctx.store.put_obs(obs_id, _cftc_payload(key, row, pair.basis), data_date=report.isoformat())
        return "done" if report >= expected else "not_yet"

    return _run_batch(ctx, "us.cftc", expected.isoformat(), ("cftc",), one, obs_for=lambda _: OBS_CFTC["es"],
                      max_not_yet=12, not_yet_retry_s=6 * 3600.0)


def _month_key(value: Any) -> Optional[str]:
    return f"{value.year:04d}-{value.month:02d}" if isinstance(value, date) else None


def _month_end(key: str) -> date:
    year, month = int(key[:4]), int(key[5:7])
    return date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)


def _finra_expected(ctx: Any) -> str:
    """FINRA 在下個月中旬公布上個月（實測 10/17 已有 9 月）：15 日之後期待上個月，之前期待上上個月。"""
    today = _today_et(ctx)
    month = _prev_month(today) if today.day >= 15 else _prev_month(_prev_month(today))
    return f"{month.year:04d}-{month.month:02d}"


def _due_finra(ctx: Any) -> bool:
    return _batch_due(ctx, "us.finra", _finra_expected(ctx), ("finra",), max_not_yet=20)


def _run_finra(ctx: Any) -> Optional[str]:
    expected = _finra_expected(ctx)

    def one(_: str) -> str:
        rows: dict[str, float] = {}
        for row in src.fetch_finra_margin(http=ctx.http, ledger=ctx.ledger) or ():
            key, value = _month_key(row.month), _finite(row.debit_musd)
            if key is not None and value is not None:
                rows[key] = value
        if not rows:
            raise market_calc.ParseError("empty")
        latest = max(rows)
        stored = _obs(ctx, OBS_MARGIN)
        if stored and stored.get("payload") is not None and latest <= str(stored.get("period") or ""):
            ctx.store.obs_not_yet(OBS_MARGIN)
        else:
            ordered = [[key, rows[key]] for key in sorted(rows)[-MARGIN_MONTHS:]]
            # FINRA 的過期以該月月底為錨（§18.9）：data_date＝月底、period＝月份。
            ctx.store.put_obs(OBS_MARGIN, {"rows": ordered}, data_date=_month_end(latest).isoformat(), period=latest)
        return "done" if latest >= expected else "not_yet"

    return _run_batch(ctx, "us.finra", expected, ("finra",), one, obs_for=lambda _: OBS_MARGIN, max_not_yet=20,
                      not_yet_retry_s=12 * 3600.0)


# ── 前十大（股別合併）、13F、基本面、財報日 ─────────────────────────────────────


def merge_top10(rows: Iterable[Sequence[Any]], limit: int = 10) -> list[dict]:
    """同一公司的不同股別合併成一家（權重相加），取權重前 `limit` 家；13F／基本面查權重較大的那個股別
    （ETF 權重∝持有市值，代替「市值較大」）。`rows`＝[[代號, 名稱, 權重, …], …]。"""
    groups: dict[str, dict] = {}
    for row in rows:
        if len(row) < 3 or not _valid_symbol(row[0]):
            continue
        symbol, name, weight = row[0], row[1], _finite(row[2])
        if weight is None:
            continue
        group = groups.setdefault(SHARE_CLASSES.get(symbol, symbol), {"symbols": set(), "weight": 0.0, "best": None})
        group["symbols"].add(symbol)
        group["weight"] += weight
        best = group["best"]
        if best is None or weight > best[1] or (weight == best[1] and symbol < best[0]):
            group["best"] = (symbol, weight, name)
    ranked = sorted(groups.values(), key=lambda group: (-group["weight"], group["best"][0]))[:limit]
    return [{"name": group["best"][2], "symbols": sorted(group["symbols"]), "weight": round(group["weight"], 6),
             "pick": group["best"][0]} for group in ranked]


def _refresh_top10(ctx: Any) -> list[str]:
    """依三份持股清單重算前十大（有變才寫 obs）；回三檔前十大要查的代號（去重、排序）。"""
    picks: set[str] = set()
    for etf, list_obs, source in TOP10_LISTS:
        got = _obs(ctx, list_obs)
        rows = ((got or {}).get("payload") or {}).get("rows")
        if not rows:
            continue
        top = merge_top10(rows)
        payload = {"source": source, "list_obs": list_obs, "list_as_of": got.get("data_date"), "rows": top}
        previous = _obs(ctx, OBS_TOP10[etf])
        if not previous or previous.get("payload") != payload:
            ctx.store.put_obs(OBS_TOP10[etf], payload, data_date=got.get("data_date"))
        picks.update(item["pick"] for item in top)
    return sorted(picks)


def _top10_picks(ctx: Any) -> list[str]:
    """不寫 store 的版本（due 用）：從持股清單直接算。"""
    picks: set[str] = set()
    for _, list_obs, _ in TOP10_LISTS:
        rows = _obs_payload(ctx, list_obs).get("rows")
        if rows:
            picks.update(item["pick"] for item in merge_top10(rows))
    return sorted(picks)


def _filing_season(day: date) -> bool:
    """13F 季末後 45 天截止：2／5／8／11 月的 1–20 日每天抓（PLAN §18.9）。"""
    return day.month in (2, 5, 8, 11) and day.day <= 20


def _13f_period(ctx: Any) -> str:
    today = _today_et(ctx)
    return today.isoformat() if _filing_season(today) else _iso_week(today)


def _mode_date(days: Iterable[Any]) -> Optional[date]:
    counter = Counter(day for day in (_parse_iso(item) for item in days or ()) if day is not None)
    if not counter:
        return None
    best = max(counter.values())
    return max(day for day, count in counter.items() if count == best)  # 並列取較新的


def _due_13f(ctx: Any) -> bool:
    return _batch_due(ctx, "us.13f", _13f_period(ctx), _top10_picks(ctx), max_not_yet=1)


def _run_13f(ctx: Any) -> Optional[str]:
    picks = _refresh_top10(ctx)

    def one(symbol: str) -> str:
        holders = src.fetch_nasdaq_13f(symbol, http=ctx.http, ledger=ctx.ledger)
        mode = _mode_date(holders.holder_periods)
        payload = _jsonable(holders)
        payload["holder_period_mode"] = _iso(mode)
        obs_id = OBS_13F_PREFIX + symbol
        previous = _obs(ctx, obs_id)
        if previous and previous.get("payload") == payload:
            ctx.store.obs_not_yet(obs_id)
        else:
            # 13F 的彙總期間來源未標：data_date 留空，period＝前十大持有人申報期間的眾數（§18.9）。
            ctx.store.put_obs(obs_id, payload, data_date=None, period=_iso(mode))
        return "done"

    return _run_batch(ctx, "us.13f", _13f_period(ctx), picks, one, obs_for=lambda sym: OBS_13F_PREFIX + sym,
                      max_not_yet=1)


def _market_cap(ctx: Any, symbol: str) -> tuple[Optional[float], Optional[str]]:
    """Nasdaq-100 名單裡的市值（已經每天抓）；不在名單的回 (None, None)，由 us.fund 另查 summary。"""
    for row in _obs_payload(ctx, OBS_HOLDINGS_NDX).get("rows") or []:
        if isinstance(row, list) and len(row) >= 4 and row[0] == symbol:
            cap = _finite(row[3])
            if cap is not None and cap > 0:
                return cap, MARKET_CAP_NDX
    return None, None


def _due_fund(ctx: Any) -> bool:
    return _batch_due(ctx, "us.fund", _iso_week(_today_et(ctx)), _top10_picks(ctx), max_not_yet=1)


def _run_fund(ctx: Any) -> Optional[str]:
    picks = _top10_picks(ctx)

    def one(symbol: str) -> str:
        obs_id = OBS_FUND_PREFIX + symbol
        try:
            reported = src.fetch_nasdaq_financials(symbol, http=ctx.http, ledger=ctx.ledger) or []
        except market_calc.ParseError as exc:
            if exc.kind != "empty":
                raise
            reported = []
        quarters = sorted([quarter.end_date.isoformat(), _finite(quarter.net_income_common_kusd)]
                          for quarter in reported)
        if not quarters:
            # 來源沒有這家的季報（ADR 例 TSM 回 data: null）：本益比暫缺，不算失敗，也不必再查市值。
            ctx.store.obs_not_yet(obs_id)
            return "done"
        cap, cap_source = _market_cap(ctx, symbol)
        if cap is None:  # 不在 Nasdaq-100 名單（BRK.B、JPM、LLY…）：另查 summary 的市值（PLAN §19.3）
            cap = src.fetch_nasdaq_summary_market_cap(symbol, http=ctx.http, ledger=ctx.ledger)
            cap_source = MARKET_CAP_SUMMARY if cap is not None else None
        payload = {"quarters": quarters, "market_cap_usd": cap, "market_cap_source": cap_source}
        previous = _obs(ctx, obs_id)
        if previous and previous.get("payload") == payload:
            ctx.store.obs_not_yet(obs_id)
        else:
            ctx.store.put_obs(obs_id, payload, data_date=quarters[-1][0])
        return "done"

    return _run_batch(ctx, "us.fund", _iso_week(_today_et(ctx)), picks, one, obs_for=lambda sym: OBS_FUND_PREFIX + sym,
                      max_not_yet=1)


def _due_earnings(ctx: Any) -> bool:
    return _batch_due(ctx, "us.events.earnings", _iso_week(_today_et(ctx)), _top10_picks(ctx), max_not_yet=1)


def _run_earnings(ctx: Any) -> Optional[str]:
    picks = _top10_picks(ctx)

    def one(symbol: str) -> str:
        earnings = src.fetch_nasdaq_earnings_date(symbol, http=ctx.http, ledger=ctx.ledger)
        symbols = {key: value for key, value in (_obs_payload(ctx, OBS_EVENTS_EARNINGS).get("symbols") or {}).items()
                   if key in picks}
        symbols[symbol] = {"date": _iso(earnings.date), "estimated": bool(earnings.estimated)}
        ctx.store.put_obs(OBS_EVENTS_EARNINGS, {"symbols": dict(sorted(symbols.items()))},
                          data_date=_today_et(ctx).isoformat())
        return "done"

    return _run_batch(ctx, "us.events.earnings", _iso_week(_today_et(ctx)), picks, one,
                      obs_for=lambda _: OBS_EVENTS_EARNINGS, max_not_yet=1)


# ── 事件：FOMC（週）、經濟日曆（每天抓 7 天） ──────────────────────────────────


def _due_fomc(ctx: Any) -> bool:
    return _batch_due(ctx, "us.events.fomc", _iso_week(_today_et(ctx)), ("fomc",))


def _run_fomc(ctx: Any) -> Optional[str]:
    def one(_: str) -> str:
        meetings = [
            # 決議在會期最後一天 14:00 美東公布（§18.9）；卡上換台北時間。
            {"start": meeting.start.isoformat(), "end": meeting.end.isoformat(), "sep": bool(meeting.sep),
             "decision_utc": meeting.decision_utc.isoformat()}
            for meeting in src.fetch_fed_fomc(http=ctx.http, ledger=ctx.ledger) or ()
        ]
        if not meetings:
            raise market_calc.ParseError("empty")
        ctx.store.put_obs(OBS_EVENTS_FOMC, {"meetings": sorted(meetings, key=lambda item: item["start"])},
                          data_date=_today_et(ctx).isoformat())
        return "done"

    return _run_batch(ctx, "us.events.fomc", _iso_week(_today_et(ctx)), ("fomc",), one,
                      obs_for=lambda _: OBS_EVENTS_FOMC)


def _econ_days(ctx: Any) -> list[str]:
    today = _today_et(ctx)
    return [(today + timedelta(days=offset)).isoformat() for offset in range(7)]


def _due_econ(ctx: Any) -> bool:
    return _batch_due(ctx, "us.events.econ", _today_et(ctx).isoformat(), _econ_days(ctx))


def _run_econ(ctx: Any) -> Optional[str]:
    period = _today_et(ctx).isoformat()
    window = _econ_days(ctx)
    # 2026-10-18 審查 r6b I1：先用視窗裡的週三驗 Nasdaq 的日期對應（date=X 回 X−1）。週三有開市就一定要看到 MBA；
    # 對不上就整批不寫（丟例外＝任務失敗、維護看得到），不會讓其他六天整批錯一天。休市的週三不驗。
    wednesday = next(date.fromisoformat(day) for day in window if date.fromisoformat(day).weekday() == 2)
    if ctx.us_cal.is_session(wednesday):
        src.fetch_nasdaq_econ_calendar(wednesday, http=ctx.http, ledger=ctx.ledger, require_sentinel=True)

    def one(day_iso: str) -> str:
        items = []
        # A 的解析器已經把來源的 gmt 欄（其實是美東當地時間）換成 UTC，這裡不再轉。
        for event in src.fetch_nasdaq_econ_calendar(date.fromisoformat(day_iso), http=ctx.http, ledger=ctx.ledger) or ():
            name = _clean(event.name, 40)
            if name is not None and isinstance(event.time_utc, datetime):
                items.append([_jsonable(event.time_utc), name])
        days = {key: value for key, value in (_obs_payload(ctx, OBS_EVENTS_ECON).get("days") or {}).items()
                if key in window}
        days[day_iso] = sorted(items)
        ctx.store.put_obs(OBS_EVENTS_ECON, {"days": dict(sorted(days.items()))}, data_date=period)
        return "done"

    return _run_batch(ctx, "us.events.econ", period, window, one, obs_for=lambda _: OBS_EVENTS_ECON)


# ── 任務表（同一群組內依這個順序執行：分割、持股清單要在日線之前） ─────────────────

TASK_TABLE: tuple[tuple[str, str, Callable[[Any], bool], Callable[[Any], Optional[str]], float], ...] = (
    ("us.calendar", "core", _due_calendar, _run_calendar, 15.0),
    ("us.splits", "core", _due_splits, _run_splits, 20.0),
    ("us.holdings.spy", "core", _due_holdings_spy, _run_holdings_spy, 35.0),  # SSGA xlsx：endpoint 30 秒
    ("us.holdings.ndx", "core", _due_holdings_ndx, _run_holdings_ndx, 20.0),
    ("us.hist", "core", _due_hist, _run_hist_core, 90.0),
    ("us.vix", "core", _due_vix, _run_vix, 35.0),
    ("us.options", "core", _due_options, _run_options, 95.0),  # CBOE 三檔各 30 秒上限
    ("us.ust", "core", _due_ust, _run_ust, 35.0),
    ("us.events.econ", "core", _due_econ, _run_econ, 30.0),
    ("us.hist.backfill", "backfill", _due_hist_backfill, _run_hist_backfill, 90.0),
    ("us.holdings.soxx", "low", _due_holdings_soxx, _run_holdings_soxx, 20.0),
    ("us.holdings.qqq", "low", _due_holdings_qqq, _run_holdings_qqq, 20.0),
    ("us.cftc", "low", _due_cftc, _run_cftc, 20.0),
    ("us.finra", "low", _due_finra, _run_finra, 20.0),
    ("us.13f", "low", _due_13f, _run_13f, 60.0),
    ("us.fund", "low", _due_fund, _run_fund, 60.0),
    ("us.events.earnings", "low", _due_earnings, _run_earnings, 45.0),
    ("us.events.fomc", "low", _due_fomc, _run_fomc, 20.0),
)


def build_tasks() -> list[Task]:
    return [Task(name=name, group=group, due=due, run=run, budget_s=budget, market=MARKET)
            for name, group, due, run, budget in TASK_TABLE]


TASKS: list[Task] = build_tasks()


def register_tasks(register_fn: Callable[[Task], None] = register) -> None:
    for task in TASKS:
        register_fn(task)


register_tasks()  # import 時登記進框架的記憶體表（不做 I/O）
