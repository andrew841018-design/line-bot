"""市場盤勢（/盤勢，2026-10-17）的 publish 步驟：只讀 SQLite → 計算 → 畫圖 → 原子發布顯示用快照。

Andrew 2026-10-17 要一張「目前市場狀況」的盤勢卡（美股／台股的技術面、寬度、量能、期權、籌碼、
槓桿、13F、宏觀、事件），按了才回、只描述狀態、不寫買賣。bot 端（market_dashboard）不抓網路、
不帶交易日曆，只比 `now > stale_after`；所以「什麼時候算過期」全部在這裡依市場日曆算好寫進快照。

規則（PLAN §4、§5、§7、§18、§19；INTERFACES §5、§9.4）：
- 列 id 與 values 欄位照 `market_calc.ROW_IDS`；每列寫進快照前用 `market_calc.row_ok` 自我驗，
  驗不過那列改 never。每一列、每張圖各自 `except Exception`：壞的那列 state=never，outcome 只記
  `unexpected:<型別名>`（不記例外訊息，outcome 會進每日維護）；一列壞掉不影響其他列。
- `stale_after`：原始列照 `market_calc.stale_after` 的類型表；衍生列（寬度、量能、牆、GEX、一句話、
  矛盾…）＝所有輸入的最小值，過期的輸入不拿來算。寬度只發布覆蓋率 ≥80% 的最新那一天（台股另要
  除權息資料涵蓋到那天），當天還在抓時顯示前一個完整交易日（寫日期）。
- 交易日曆：`market_calendar` 從 store 建（SPY／加權指數實際成交日＋休市表＋覆寫），再轉成
  market_calc 的 TradingCalendar，collect 與 publish 用同一份輸入。
- 圖：QQQ／SPY／SOXX 各兩張（未平倉、GEX），台指只有未平倉；輸入雜湊沒變而且檔案還在就沿用。
  圖目錄要先過 `market_paths.private_dir_ok`，不合格就整份快照不帶圖（快照照寫）。檔名 uuid4 hex，
  先登記再畫（market_charts 自己用同目錄暫存檔＋fsync＋rename 寫）。清理只刪自己登記、退役超過
  72 小時、lstat 是自己的一般檔的那些；被 kill 留下的 `.market-*.tmp` 超過 1 小時也清掉。
- 快照：寫之前自我驗 schema、每列試編碼（ensure_ascii=False、不允許 NaN），編不過的那列改 never；
  `state/` 不合格（`state_dir_ok`）就什麼都不寫、回 kind=state_dir；不建立、不 chmod `state/`。
- 外部字串（新聞標題、川普摘錄、持股名稱、事件名稱）組列前再過一次 `market_calc.clean_external`。
- 所有時間由呼叫端的 `now` 決定，不呼叫 time.time()／date.today()。

這個模組 import 時不做任何 I/O；畫圖模組（matplotlib）與 pipeline 模組在真的要用時才載入。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import numbers
import os
import re
import stat
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

import market_calc
import market_calendar
import market_paths

_ET = market_calc.ET
_TPE = market_calc.TPE
_UTC = timezone.utc

# ── 常數 ─────────────────────────────────────────────────────────────────────
SCHEMA_VERSION = market_calc.SNAPSHOT_SCHEMA
STATES = frozenset(market_calc.ROW_STATES)
CHART_KEYS = market_calc.CHART_KEYS  # ("QQQ", "SPY", "SOXX", "TXO")；bot 的「台指」對應 TXO
CHART_DELETE_AFTER_S = 72 * 3600.0
TMP_SWEEP_AFTER_S = 3600.0  # 被 kill 留下的暫存檔：超過 1 小時才清（不會碰到正在寫的）
CHART_MAX_BYTES = 1_000_000  # 一張圖 <1 MB（LINE 預覽圖上限）
# bot 讀快照的上限是 2 MB（market_dashboard）；超過就整份讀不到，這裡留一半當餘裕。
SNAPSHOT_MAX_BYTES = 1_000_000
BREADTH_LOOKBACK = 5  # 往前最多找幾個交易日的「完整日」，再舊就當暫缺
HISTORY_DAYS = 420  # 讀收盤的範圍（日曆天）：寬度 240＋5＋5 個交易日、量能 250 日都夠
CHART_INPUT_VERSION = 1  # 畫圖輸入的格式版本；改畫法時加 1 讓舊圖全部重畫
OPTION_CHART_DAYS = market_calc.OPTION_WINDOW_DAYS
EVENT_HORIZON_DAYS = 45  # FOMC、財報列多遠以內的（經濟日曆本來就只抓 7 天）
EVENTS_KEEP = 12
NEWS_KEEP = 10
TRUMP_KEEP = 10
POSTS_LOOKBACK_S = 30 * 86400.0  # 讀累積貼文的範圍（只拿來找最近一次抓到的時間）
ELECTION_RESULT_DAYS = 7  # 結果出來後顯示幾天
ELECTION_STATE_TASK = "publish.election"  # store.task_state：第一次看到選舉結果的時間
# 來源多久沒成功就不再保證列表是「最新的」（之後那列算過期）：經濟日曆每天抓，FOMC、財報日每週
EVENT_SOURCE_MAX_AGE_S = {"econ": 36 * 3600.0, "fomc": 8 * 86400.0, "earnings": 8 * 86400.0}
FEED_LAG_AT_LEAST_S = 1.5 * 3600.0  # 川普存檔超過這麼久沒抓到：計數標「（至少）」

US, TW = "us", "tw"
TAIEX, TW_ETF = "TAIEX", "0050"
US_ETFS = ("VOO", "QQQ", "SOXX", "SPY")
BREADTH_ETFS = ("VOO", "QQQ", "SOXX")
MA_WINDOWS = market_calc.MA_WINDOWS
_US_SYMBOL_RE = re.compile(r"[A-Z]{1,5}(?:\.[A-Z])?")
_TW_CODE_RE = re.compile(r"[0-9A-Z]{4,6}")
_CHART_NAME_RE = re.compile(r"[0-9a-f]{32}\.png")
_TMP_NAME_RE = re.compile(r"\.market-[A-Za-z0-9_]+\.tmp")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONTH_RE = re.compile(r"\d{4}-\d{2}")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# ── 讀的 obs／貼文鍵（和 market_pipeline_us／_tw 同一份；test_market_publish 有漂移測試） ─────────
OBS_US_HOLDINGS = {"VOO": "us.holdings.SPY", "QQQ": "us.holdings.NDX", "SOXX": "us.holdings.SOXX"}
OBS_US_TOP10 = "us.top10.{}"
OBS_US_13F = "us.13f.sym.{}"
OBS_US_FUND = "us.fund.sym.{}"
OBS_US_OPTIONS = "us.options.{}"
OBS_US_QUOTE = "us.quote.{}"  # VIX、VIX3M
OBS_US_UST = "us.ust"
OBS_US_CFTC = "us.cftc.{}"
OBS_US_MARGIN = "us.margin"
OBS_US_FOMC = "us.events.fomc"
OBS_US_ECON = "us.events.econ"
OBS_US_EARNINGS = "us.events.earnings"
OBS_TW_ALLBUT = "tw.allbut"
OBS_TW_INST3 = "tw.inst3"
OBS_TW_MARGIN = "tw.margin"
OBS_TW_PE = "tw.pe"
OBS_TW_REVENUE = "tw.revenue"
OBS_TW_FUTURES = "tw.futures"
OBS_TW_OPTIONS_INST = "tw.options_inst"
OBS_TW_PCR = "tw.pcr"
OBS_TW_TXO = "tw.txo"
OBS_TW_FX = "tw.fx"
OBS_TW_HOLDINGS = "tw.holdings.0050"
COVERAGE_TWT49U = "twt49u"
# market_pipeline_misc（宏觀 Yahoo／代理、BTC、新聞、川普、預測市場）寫這些
OBS_MACRO = "macro.{}"  # {"value","chg","chg_pct","date","prev_date","source","symbol","bars": [[日期, 收盤], …]}
OBS_BTC = "macro.BTC"  # Coinbase 每 2 小時：{"value","source","fetched_utc"}
OBS_BTC_YAHOO = "macro.BTC.yahoo"  # Yahoo 的 BTC 日線（只當備援），格式同其他宏觀
OBS_NEWS = "news.{}"  # {"items": [{"title","source","published_utc"}], "fetched_utc"}
OBS_TRUMP_SUMMARY = "trump.summary"  # {"topics","labels","at_least",…}：這裡只拿 at_least（缺口）與主題名
POSTS_TRUMP = "trump"  # store.upsert_posts 的 kind：{original_id, published_utc, own_text, quoted_author, removed, topics}
# 預測市場（PredictionQuote 欄位＋mid、fetched_utc）：眾院在前（bot 摘要卡取 Polymarket 眾院那一則），參院在後
OBS_ELECTION = {
    "house": ("election.polymarket.house", "election.kalshi.house"),
    "senate": ("election.polymarket.senate", "election.kalshi.senate"),
}

# CFTC 合約的固定標籤（卡上只放程式的字，不放 CFTC 原文市場名）
_CFTC_NAMES = {"es": "S&P 500", "nq": "Nasdaq-100"}
_CFTC_BASIS = {"consolidated": "Consolidated", "e-mini": "E-mini"}
_VOTE_DAY = date(2026, 11, 3)  # 美東日期
_ELECTION_LAST_DAY = date(2027, 2, 1)  # Kalshi 結算上限；過了一律當本屆結束

_LABELS = {
    "us.etf.": "美股 ETF",
    "us.breadth.": "成分股寬度",
    "us.options.": "期權",
    "us.cftc.": "CFTC 期貨部位",
    "us.margin": "美股融資（FINRA）",
    "us.13f.": "13F 機構持股",
    "us.fund.": "前十大本益比",
    "tw.taiex": "加權指數",
    "tw.etf.": "台股 ETF",
    "tw.breadth.": "0050 成分寬度",
    "tw.ad": "上市漲跌家數",
    "tw.turnover": "成交金額",
    "tw.inst3": "三大法人",
    "tw.margin": "台股融資",
    "tw.futures": "外資台指期",
    "tw.options": "台指選擇權",
    "tw.fund.": "0050 基本面",
    "macro.": "宏觀",
    "events.next": "接下來的事件",
    "season.now": "季節性",
    "election": "期中選舉",
    "trump.summary": "川普貼文主題",
    "trump.posts": "川普原文",
    "news.": "新聞",
    "oneliner.": "一句話",
    "contra.": "矛盾",
}


class _Unavailable(Exception):
    """這一列現在沒有資料可算（還沒抓到、日曆算不出來…）：state=never，不算 unexpected。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class _BadValue(ValueError):
    """快照裡出現不能發布的值（孤立代理字元、不認得的型別、驗不過 ROW_IDS）：那一列改 never。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


@dataclass
class ChartJob:
    """一個圖鍵（QQQ／SPY／SOXX／TXO）這一輪要畫的東西；`parts` 依序是 (畫圖函式名, 參數)。"""

    key: str
    parts: list[tuple[str, dict]]
    inputs_date: Optional[date]
    stale_after: float

    def part_hashes(self) -> list[str]:
        """每張圖自己的輸入雜湊（含版本、圖鍵、資料日、第幾張）：Store 依它找可沿用的舊檔。"""
        out = []
        for index, (func_name, kwargs) in enumerate(self.parts):
            blob = json.dumps(
                {"v": CHART_INPUT_VERSION, "key": self.key, "index": index, "func": func_name, "args": kwargs,
                 "date": self.inputs_date.isoformat() if self.inputs_date else None},
                sort_keys=True, ensure_ascii=True, default=_hash_default, allow_nan=True,
            )
            out.append(f"{hashlib.sha256(blob.encode('ascii')).hexdigest()}:{index}")
        return out


@dataclass
class RowResult:
    """builder 的輸出；`_finish_row` 轉成快照列。facts＝給一句話與矛盾用的面向（鍵見
    market_calc.contradictions）；chart＝這一列的圖（列本身驗過才畫）。"""

    values: dict
    data_date: Optional[date] = None
    stale_after: Optional[float] = None
    source: str = ""
    basis: str = ""
    period: Optional[str] = None
    state: str = "ok"
    facts: dict[str, Any] = field(default_factory=dict)
    chart: Optional[ChartJob] = None


def _hash_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):  # numpy 純量
        return value.item()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(type(value).__name__)


@dataclass(frozen=True)
class _Failed:
    kind: str


class _Ctx:
    """一輪 publish 的共用狀態：store、時間、日曆與讀過的資料（快取在這裡，失敗也快取）。"""

    def __init__(self, store: Any, paths: Any, now: float):
        self.store = store
        self.paths = paths
        self.now = float(now)
        self.now_utc = datetime.fromtimestamp(self.now, _UTC)
        self.rows: dict[str, dict] = {}
        self.row_errors: dict[str, str] = {}
        self.never_reasons: Counter = Counter()
        self.shared_errors: dict[str, str] = {}
        self.chart_jobs: dict[str, ChartJob] = {}
        self.facts: dict[str, market_calc.Fact] = {}
        self.fact_stale: dict[str, float] = {}
        self.progress: dict[str, Any] = {"us_members": None, "us_last_complete_day": None, "tw_last_complete_day": None}
        self.nan_dropped = 0
        self._cache: dict[str, Any] = {}

    def shared(self, key: str, loader: Callable[[], Any]) -> Any:
        """幾列共用的資料（日曆、成分股收盤）只載一次；載入失敗也記住，之後的列直接 _Unavailable，
        outcome 只記一次 `unexpected:<型別名>`。"""
        if key in self._cache:
            value = self._cache[key]
            if isinstance(value, _Failed):
                raise _Unavailable(value.kind)
            return value
        try:
            value = loader()
        except _Unavailable as exc:
            self._cache[key] = _Failed(exc.kind)
            raise
        except Exception as exc:
            self.shared_errors[key] = f"unexpected:{type(exc).__name__}"
            self._cache[key] = _Failed("shared")
            raise _Unavailable("shared") from None
        self._cache[key] = value
        return value

    # ── 日曆 ──
    def calendar(self, market: str) -> tuple[Any, market_calc.TradingCalendar]:
        """(market_calendar.Calendar, market_calc.TradingCalendar)；同一份輸入。"""

        def load() -> tuple[Any, market_calc.TradingCalendar]:
            cal = market_calendar.us_calendar(self.store) if market == US else market_calendar.tw_calendar(self.store)
            return cal, cal.to_trading_calendar()

        return self.shared(f"calendar.{market}", load)

    def trading(self, market: str) -> market_calc.TradingCalendar:
        return self.calendar(market)[1]

    def us_day(self) -> date:
        """D：最近一個美東已過收盤＋30 分鐘的預期交易日。"""

        def load() -> date:
            day = market_calendar.latest_closed_us_session(self.calendar(US)[0], self.now)
            if day is None:
                raise _Unavailable("no_session_us")
            return day

        return self.shared("day.us", load)

    def tw_day(self) -> date:
        """T：最近一個台北已過 14:30 的預期交易日。"""

        def load() -> date:
            day = market_calendar.latest_tw_session(self.calendar(TW)[0], self.now)
            if day is None:
                raise _Unavailable("no_session_tw")
            return day

        return self.shared("day.tw", load)

    def stale(self, kind: str, data_date: Any) -> float:
        """market_calc.stale_after；交易日類型的日曆算不出來 → 這列暫缺（不猜）。"""
        calendars: dict[str, market_calc.TradingCalendar] = {}
        needs = {
            market_calc.KIND_US_DAILY: US, market_calc.KIND_HOLDINGS_SPY: US, market_calc.KIND_HOLDINGS_NDX: US,
            market_calc.KIND_OPTIONS: US, market_calc.KIND_TW_DAILY: TW,
        }.get(kind)
        if needs == US:
            calendars["nyse"] = self.trading(US)
        elif needs == TW:
            calendars["twse"] = self.trading(TW)
        return float(market_calc.stale_after(kind, data_date, calendars, self.now))

    # ── 資料 ──
    def obs(self, obs_id: str) -> Optional[dict]:
        """成功過的 obs（payload 是 dict）；沒有或只失敗過回 None。"""

        def load() -> Optional[dict]:
            found = self.store.get_obs(obs_id)
            if not isinstance(found, dict) or not isinstance(found.get("payload"), dict):
                return None
            return found

        return self.shared(f"obs:{obs_id}", load)

    def payload(self, obs_id: str) -> dict:
        found = self.obs(obs_id)
        if found is None:
            raise _Unavailable("missing")
        return found["payload"]

    def closes(self, market: str, symbol: str) -> list[Any]:
        since = self.now_utc.date() - timedelta(days=HISTORY_DAYS)
        return self.shared(f"closes:{market}:{symbol}", lambda: list(self.store.closes(market, symbol, since=since)))

    def quarantine(self, market: str) -> dict[str, list[tuple[date, date]]]:
        """{代號: [(起, 迄), …]}：market_calc 只要區間，原因不用。"""

        def load() -> dict[str, list[tuple[date, date]]]:
            raw = self.store.quarantine(market) or {}
            return {symbol: [(item[0], item[1]) for item in items] for symbol, items in raw.items()}

        return self.shared(f"quarantine:{market}", load)

    def module(self, name: str) -> Any:
        return self.shared(f"module:{name}", lambda: importlib.import_module(name))

    def record_facts(self, facts: Mapping[str, Any], data_date: Optional[date], stale_after: float) -> None:
        ok = self.now <= stale_after
        for key, value in facts.items():
            self.facts[key] = market_calc.Fact(value, data_date, ok)
            self.fact_stale[key] = stale_after


# ── 進入點 ──────────────────────────────────────────────────────────────────


def publish(store: Any, paths: Any, *, now: float, charts: Any = None) -> dict:
    """算完全部列、畫圖、寫快照、清舊圖；回 outcome 片段（只有列舉值、次數、時間）。

    `charts` 預設是 market_charts（真的要畫才 import，import 本模組不會載入 matplotlib）；
    測試注入假的模組物件（有 render_oi_chart／render_gex_chart 即可）。
    """
    now = float(now)
    outcome: dict[str, Any] = {"kind": "ok", "generated_at": now}
    # 先看 state/：不合格就什麼都不做（連圖目錄都不建），也不碰 store。
    if not market_paths.state_dir_ok(Path(paths.state_dir)):
        outcome["kind"] = "state_dir"
        return outcome
    ctx = _Ctx(store, paths, now)
    ids = row_ids()
    derived = [row_id for row_id in ids if row_id.startswith(("oneliner.", "contra."))]
    for row_id in ids:
        if row_id not in derived:
            ctx.rows[row_id] = _build_row(ctx, row_id)
    for row_id in derived:  # 一句話與矛盾要等其他列算完
        ctx.rows[row_id] = _build_row(ctx, row_id)
    ctx.rows = {row_id: ctx.rows[row_id] for row_id in ids}
    _build_progress(ctx)

    chart_entries, chart_outcome = _publish_charts(ctx, charts)
    snapshot = {
        "schema": SCHEMA_VERSION,
        "generated_at": now,
        "run_id": uuid.uuid4().hex,
        "rows": ctx.rows,
        "charts": chart_entries,
        "progress": ctx.progress,
    }
    outcome.update(_row_summary(ctx))
    outcome["charts"] = chart_outcome
    problems = validate_snapshot(snapshot)
    if problems:
        outcome["kind"] = "schema"
        outcome["schema_problems"] = problems[:10]
        return outcome
    data = encode_snapshot(snapshot)
    outcome["snapshot_bytes"] = len(data)
    if len(data) > SNAPSHOT_MAX_BYTES:
        outcome["kind"] = "too_big"
        return outcome
    try:
        market_paths.write_private_atomic(Path(paths.snapshot), data)
    except OSError as exc:
        outcome["kind"] = f"write_failed:{type(exc).__name__}"
        return outcome
    # 快照寫成功之後才退役舊圖、刪舊檔：寫失敗時舊快照還引用著舊圖。
    _after_publish_charts(ctx, chart_entries, chart_outcome)
    return outcome


def row_ids() -> list[str]:
    """`market_calc.ROW_IDS` 的列 id（兩端 import 同一份）。"""
    return [str(key) for key in market_calc.ROW_IDS]


# ── 一列 ────────────────────────────────────────────────────────────────────

_EXACT_BUILDERS: dict[str, Callable[[_Ctx, str], Optional[RowResult]]] = {}
_PREFIX_BUILDERS: list[tuple[str, Callable[[_Ctx, str], Optional[RowResult]]]] = []


def _rows(*keys: str) -> Callable:
    """登記 builder：以「.」結尾的是前綴（`us.etf.` 接 `us.etf.QQQ`），其他是完整列 id。"""

    def deco(fn: Callable[[_Ctx, str], Optional[RowResult]]) -> Callable:
        for key in keys:
            if key.endswith("."):
                _PREFIX_BUILDERS.append((key, fn))
            else:
                _EXACT_BUILDERS[key] = fn
        return fn

    return deco


def _builder_for(row_id: str) -> Optional[Callable[[_Ctx, str], Optional[RowResult]]]:
    fn = _EXACT_BUILDERS.get(row_id)
    if fn is not None:
        return fn
    best: Optional[tuple[str, Callable]] = None
    for prefix, candidate in _PREFIX_BUILDERS:
        if row_id.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, candidate)
    return best[1] if best else None


def _label_for(row_id: str) -> str:
    for prefix, label in _LABELS.items():
        if row_id == prefix or (prefix.endswith(".") and row_id.startswith(prefix)):
            return label
    return ""


def never_row(label: str = "") -> dict:
    return {
        "label": label,
        "values": {},
        "data_date": None,
        "period": None,
        "source": "",
        "basis": "",
        "stale_after": 0.0,
        "state": "never",
    }


def _build_row(ctx: _Ctx, row_id: str) -> dict:
    label = _label_for(row_id)
    builder = _builder_for(row_id)
    if builder is None:
        ctx.never_reasons["no_builder"] += 1
        return never_row(label)
    try:
        result = builder(ctx, row_id)
        if result is None:
            raise _Unavailable("missing")
        row = _finish_row(ctx, row_id, result, label)
    except _Unavailable as exc:
        ctx.never_reasons[exc.kind] += 1
        return never_row(label)
    except _BadValue as exc:  # 自己組錯（型別、編碼、ROW_IDS）：記種類，這列暫缺
        ctx.row_errors[row_id] = f"bad:{exc.kind}"
        return never_row(label)
    except Exception as exc:  # 一列壞掉不影響其他列（PLAN §19.4）
        ctx.row_errors[row_id] = f"unexpected:{type(exc).__name__}"
        return never_row(label)
    if result.facts:
        ctx.record_facts(result.facts, result.data_date, row["stale_after"])
    if result.chart is not None:
        ctx.chart_jobs[result.chart.key] = result.chart
    return row


def _finish_row(ctx: _Ctx, row_id: str, result: RowResult, label: str) -> dict:
    if result.state not in STATES:
        raise _BadValue("state")
    if result.state == "never":
        raise _Unavailable("missing")
    if result.stale_after is None or not math.isfinite(float(result.stale_after)):
        raise _BadValue("stale_after")
    counter = [0]
    values = _sanitize(result.values, counter)
    if not isinstance(values, dict):
        raise _BadValue("values")
    row = {
        "label": label,
        "values": values,
        "data_date": result.data_date.isoformat() if result.data_date else None,
        "period": _sanitize(result.period, counter),
        "source": _sanitize(result.source or "", counter),
        "basis": _sanitize(result.basis or "", counter),
        "stale_after": float(result.stale_after),
        "state": result.state,
    }
    if not market_calc.row_ok(row_id, row):
        raise _BadValue("row_ids")
    # 每列先自己試編碼：孤立代理字元、NaN 在這裡就讓這一列失敗，不會拖垮整份快照。
    try:
        json.dumps(row, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (UnicodeEncodeError, ValueError, TypeError):
        raise _BadValue("encoding") from None
    ctx.nan_dropped += counter[0]
    return row


def _sanitize(value: Any, counter: list[int]) -> Any:
    """轉成可發布的 JSON 值：非有限浮點數→None（記數）、date→ISO 日期、aware datetime→epoch 秒、
    tuple→list、dict 鍵→字串；字串要能編成 UTF-8（孤立代理字元 → 整列失敗）。"""
    if value is None or isinstance(value, bool):
        return value
    if type(value).__module__ == "numpy" and hasattr(value, "item"):
        value = value.item()
        if value is None or isinstance(value, bool):
            return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        number = float(value)
        if not math.isfinite(number):
            counter[0] += 1
            return None
        return number
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise _BadValue("encoding") from None
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise _BadValue("naive_datetime")
        return value.timestamp()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(key, bool) or not isinstance(key, (str, int)):
                raise _BadValue("key")
            out[_sanitize(str(key), counter)] = _sanitize(item, counter)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, counter) for item in value]
    raise _BadValue("type")


def _row_summary(ctx: _Ctx) -> dict:
    states = Counter(row.get("state") for row in ctx.rows.values())
    summary: dict[str, Any] = {"rows": {state: int(states.get(state, 0)) for state in sorted(STATES)}}
    if ctx.row_errors:
        summary["row_errors"] = dict(sorted(ctx.row_errors.items()))
    if ctx.never_reasons:
        summary["never_reasons"] = dict(sorted(ctx.never_reasons.items()))
    if ctx.shared_errors:
        summary["shared_errors"] = dict(sorted(ctx.shared_errors.items()))
    if ctx.nan_dropped:
        summary["nan_dropped"] = ctx.nan_dropped
    return summary


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _md(day: date) -> str:
    return f"{day.month}/{day.day}"


def _iso_day(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10 and _DATE_RE.fullmatch(value[:10]):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _epoch(value: Any) -> Optional[float]:
    """epoch 秒、aware datetime 或帶時區的 ISO 字串 → epoch 秒；沒有時區的不收。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.timestamp()
    return None


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _clean(text: Any, max_len: int) -> Optional[str]:
    """外部字串組列前再洗一次（入庫時已經洗過，這裡是第二道）。"""
    try:
        return market_calc.clean_external(text, max_len)
    except Exception:
        return None


def _closes_upto(rows: Iterable[Any], day: date) -> list[Any]:
    """≤ day 的列（PLAN §18.8：一律丟掉日期晚於資料日的列）。"""
    return [row for row in rows if row[0] <= day]


def _next_midnight(now: float, tz: Any) -> float:
    local = datetime.fromtimestamp(now, tz)
    return datetime.combine(local.date() + timedelta(days=1), time(0, 0), tzinfo=tz).timestamp()


def _covered(spans: Iterable[tuple[date, date]], day: date) -> bool:
    return any(start <= day <= end for start, end in spans)


def _complete_day(
    closes: Mapping[str, Mapping[date, float]],
    members: list[str],
    cal: market_calc.TradingCalendar,
    latest: date,
    extra_ok: Optional[Callable[[date], bool]] = None,
) -> Optional[date]:
    """覆蓋率 ≥80% 的最新那一天（當天還在抓時就是前一個完整交易日；最多往前找 5 個交易日）。"""
    for day in reversed(cal.sessions_ending(latest, BREADTH_LOOKBACK)):
        if extra_ok is not None and not extra_ok(day):
            continue
        have, total = market_calc.day_coverage(closes, members, day)
        if total and have >= market_calc.COVERAGE_OK * total:
            return day
    return None


def _usable(counts: Any) -> bool:
    """漲跌家數、新高新低：有效家數 ≥80% 才發布（不足時 0 家看起來像「沒有」）。"""
    valid, total = getattr(counts, "valid", 0), getattr(counts, "total", 0)
    return bool(total) and valid >= market_calc.COVERAGE_OK * total


# ── 美股：ETF 技術面與量能 ──────────────────────────────────────────────────


def _etf_parts(
    rows: list[Any], cal: market_calc.TradingCalendar, day: date
) -> tuple[market_calc.Technicals, market_calc.VolumeState]:
    closes = {row[0]: row[1] for row in rows}
    volumes = {row[0]: row[2] for row in rows if row[2] is not None}
    # CBOE 備援的量和 Nasdaq 不同口徑（PLAN §18.7）：不參加量能倍數
    provisional = {row[0] for row in rows if len(row) > 4 and row[4]}
    tech = market_calc.technicals(closes, cal, day)
    volume = market_calc.volume_state(
        volumes, day, calendar=cal, closes=closes, day_flag=market_calc.volume_day_flag(day, cal),
        excluded=provisional,
    )
    return tech, volume


@_rows("us.etf.")
def _us_etf(ctx: _Ctx, row_id: str) -> RowResult:
    symbol = row_id.rsplit(".", 1)[1]
    cal = ctx.trading(US)
    rows = _closes_upto(ctx.closes(US, symbol), ctx.us_day())
    if not rows:
        raise _Unavailable("missing")
    day = rows[-1][0]
    tech, volume = _etf_parts(rows, cal, day)
    if tech.close is None:
        raise _Unavailable("calendar")
    return RowResult(
        market_calc.etf_values(tech, volume),
        data_date=day,
        stale_after=ctx.stale(market_calc.KIND_US_DAILY, day),
        source="CBOE（備援）" if rows[-1][4] else "Nasdaq",
        basis=market_calc.TEXT_US_NO_DIVIDEND,
        facts={
            f"{symbol}.chg_pct": tech.chg_pct,
            f"{symbol}.above_ma20": tech.above.get(20),
            f"{symbol}.vol_label": volume.label,
            f"{symbol}.vol_flag": volume.day_flag,
        },
    )


# ── 美股：寬度 ──────────────────────────────────────────────────────────────


@dataclass
class _HoldingInfo:
    members: list[str]
    as_of: Optional[date]
    payload: dict


def _us_holdings(ctx: _Ctx, etf: str) -> _HoldingInfo:
    def load() -> _HoldingInfo:
        found = ctx.obs(OBS_US_HOLDINGS[etf])
        if found is None:
            raise _Unavailable("missing")
        payload = found["payload"]
        members = sorted(
            {
                row[0]
                for row in payload.get("rows") or []
                if isinstance(row, list) and row and isinstance(row[0], str) and _US_SYMBOL_RE.fullmatch(row[0])
                and row[0] not in US_ETFS
            }
        )
        if not members:
            raise _Unavailable("missing")
        return _HoldingInfo(members, _iso_day(payload.get("as_of")) or _iso_day(found.get("data_date")), payload)

    return ctx.shared(f"holdings.us.{etf}", load)


def _us_member_closes(ctx: _Ctx) -> dict[str, dict[date, float]]:
    """三份寬度成分的聯集（只用正式列）：{代號: {日期: 收盤}}。"""

    def load() -> dict[str, dict[date, float]]:
        union: set[str] = set()
        for etf in BREADTH_ETFS:
            try:
                union.update(_us_holdings(ctx, etf).members)
            except _Unavailable:
                continue
        if not union:
            raise _Unavailable("missing")
        return {symbol: {row[0]: row[1] for row in ctx.closes(US, symbol) if not row[4]} for symbol in sorted(union)}

    return ctx.shared("closes.us.members", load)


_HOLDINGS_KIND = {
    "VOO": market_calc.KIND_HOLDINGS_SPY,
    "QQQ": market_calc.KIND_HOLDINGS_NDX,
    "SOXX": market_calc.KIND_HOLDINGS_MONEYDJ,
}


def _us_universe_label(etf: str, info: _HoldingInfo) -> str:
    day = f" {_md(info.as_of)}" if info.as_of else ""
    if etf == "VOO":
        return f"S&P 500 成分（SPY 每日持股{day}）"
    if etf == "QQQ":
        return f"Nasdaq-100 成分（Nasdaq 名單{day}）"
    return f"SOXX 持股（MoneyDJ{day}）"


def _us_coverage_note(ctx: _Ctx, etf: str, info: _HoldingInfo) -> Optional[str]:
    """持股清單過期時寬度照算、加註「成分清單 X/X」（§18.9）；SOXX 另註沒有美國代號的外國股。"""
    notes = []
    if info.as_of is not None and ctx.stale(_HOLDINGS_KIND[etf], info.as_of) < ctx.now:
        notes.append(market_calc.TEXT_MEMBER_LIST_DATE.format(md=_md(info.as_of)))
    unmapped_n = _int(info.payload.get("unmapped_count"))
    unmapped_w = _num(info.payload.get("unmapped_weight"))
    if unmapped_n and unmapped_w is not None and unmapped_w > 0:
        notes.append(market_calc.TEXT_NO_US_SYMBOL.format(n=unmapped_n, pct=f"{unmapped_w:.1f}"))
    return "；".join(notes) or None


@_rows("us.breadth.")
def _us_breadth(ctx: _Ctx, row_id: str) -> RowResult:
    etf = row_id.rsplit(".", 1)[1]
    cal = ctx.trading(US)
    latest = ctx.us_day()
    info = _us_holdings(ctx, etf)
    closes = _us_member_closes(ctx)
    quarantine = ctx.quarantine(US)
    day = _complete_day(closes, info.members, cal, latest)
    if day is None:
        raise _Unavailable("coverage")
    per_n = {
        n: market_calc.breadth(closes, cal, day, n, members=info.members, quarantine=quarantine) for n in MA_WINDOWS
    }
    ad = market_calc.ad_counts(closes, cal, day, members=info.members, quarantine=quarantine)
    highs = market_calc.highs_lows_240(closes, cal, day, members=info.members, quarantine=quarantine)
    values = market_calc.breadth_values(
        per_n,
        ad if _usable(ad) else None,
        highs if _usable(highs) else None,
        universe_label=_us_universe_label(etf, info),
        coverage_note=_us_coverage_note(ctx, etf, info),
    )
    b20 = per_n[20]
    return RowResult(
        values,
        data_date=day,
        stale_after=ctx.stale(market_calc.KIND_US_DAILY, day),
        source="Nasdaq",
        basis=market_calc.TEXT_US_NO_DIVIDEND,
        facts={f"{etf}.breadth20_pct": b20.pct if b20.status == market_calc.STATUS_OK else None},
    )


# ── 美股：期權 ──────────────────────────────────────────────────────────────


def _us_rate(ctx: _Ctx, day: date) -> tuple[Optional[float], Optional[float]]:
    """GEX 的 r：資料日（含）以前最近一列 3M 公債殖利率（%→小數）與那一列的過期時間。

    那一列已經過期（衍生列不拿過期的輸入）或沒有 → (None, None)：market_calc 改用 4% 並標「假設利率」。"""
    try:
        rows = ctx.payload(OBS_US_UST).get("rows") or []
    except _Unavailable:
        return None, None
    best: Optional[tuple[date, float]] = None
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        row_day, m3 = _iso_day(row[0]), _num(row[1])
        if row_day is None or m3 is None or row_day > day:
            continue
        if best is None or row_day > best[0]:
            best = (row_day, m3)
    if best is None:
        return None, None
    expires = ctx.stale(market_calc.KIND_US_DAILY, best[0])
    return (best[1] / 100.0, expires) if expires >= ctx.now else (None, None)


def _oi_by_strike(legs: Iterable[Any], low: float, high: float) -> tuple[list[float], list[int], list[int]]:
    calls: dict[float, int] = {}
    puts: dict[float, int] = {}
    for leg in legs:
        if not low <= leg.strike <= high:
            continue
        target = calls if leg.kind == "C" else puts
        target[leg.strike] = target.get(leg.strike, 0) + int(leg.oi)
    strikes = sorted(set(calls) | set(puts))
    return strikes, [calls.get(k, 0) for k in strikes], [puts.get(k, 0) for k in strikes]


@_rows("us.options.")
def _us_options(ctx: _Ctx, row_id: str) -> RowResult:
    symbol = row_id.rsplit(".", 1)[1]
    payload = ctx.payload(OBS_US_OPTIONS.format(symbol))
    chain = ctx.module("market_pipeline_us").chain_from_payload(payload)
    last = getattr(chain, "quote_last_trade_et", None)
    spot = _num(getattr(chain, "spot", None))
    if not isinstance(last, datetime) or spot is None or spot <= 0:
        raise _Unavailable("missing")
    day = market_calc.et_date(last)  # 資料日＝報價最後成交的美東日期（PLAN §18.1）
    cal = ctx.trading(US)
    rate, rate_stale = _us_rate(ctx, day)
    values = market_calc.options_values(chain, day, spot=spot, rate=rate, holidays=cal, iv30=getattr(chain, "iv30", None))
    # GEX 是衍生值：過期＝期權鏈與 3M 公債（有用到的話）兩者較早的那個（§18.9）
    stale_after = min(value for value in (ctx.stale(market_calc.KIND_OPTIONS, day), rate_stale) if value is not None)
    gex = values.get("gex") or {}
    facts: dict[str, Any] = {}
    if gex.get("status") == market_calc.STATUS_OK:
        facts = {f"{symbol}.gex_sign": gex.get("sign"), f"{symbol}.gex_near_zero": gex.get("near_zero")}
    return RowResult(
        values,
        data_date=day,
        stale_after=stale_after,
        source="CBOE（延遲報價）",
        basis=market_calc.TEXT_CHAIN_RANGE,
        facts=facts,
        chart=_option_chart(symbol, chain, day, spot, rate, values, stale_after),
    )


def _option_chart(
    symbol: str, chain: Any, day: date, spot: float, rate: Optional[float], values: dict, stale_after: float
) -> ChartJob:
    """圖 1＝45 天內到期各履約價的買權／賣權未平倉（±15%）；圖 2＝假設部位的 GEX 曲線（算得出來才畫）。"""
    oi_valid, gamma_valid, stats = market_calc.option_sets(chain, day, spot=spot)
    window = [leg for leg in oi_valid if 0 < leg.days <= OPTION_CHART_DAYS]
    low, high = spot * (1 - market_calc.WALL_BAND), spot * (1 + market_calc.WALL_BAND)
    strikes, call_oi, put_oi = _oi_by_strike(window, low, high)
    monthly = values.get("monthly") if isinstance(values.get("monthly"), dict) else {}
    parts: list[tuple[str, dict]] = [
        (
            "render_oi_chart",
            {
                "title": f"{symbol} 期權未平倉（{OPTION_CHART_DAYS} 天內到期）｜{_md(day)}",
                "strikes": strikes,
                "call_oi": call_oi,
                "put_oi": put_oi,
                "spot": spot,
                "call_wall": values.get("call_wall"),
                "put_wall": values.get("put_wall"),
                "max_pain": monthly.get("max_pain"),
                "range_note": "現價 ±15%；" + market_calc.TEXT_OI_DATE,
            },
        )
    ]
    gex = market_calc.gex_from_sets(oi_valid, gamma_valid, stats, spot, market_calc.DEFAULT_RATE if rate is None else rate)
    if gex.status == market_calc.STATUS_OK and gex.grid:
        note = "假設造市商持有買權、賣出賣權；看不到實際部位"
        if rate is None:
            note += "；" + market_calc.TEXT_RATE_ASSUMED
        parts.append(
            (
                "render_gex_chart",
                {
                    "title": f"{symbol} 假設部位的 gamma｜{_md(day)}",
                    "prices": list(gex.grid),
                    "gex": list(gex.values),
                    "spot": spot,
                    "crossings": [(c.price, market_calc.GEX_DIRECTION_TEXT.get(c.direction, "")) for c in gex.crossings],
                    "note": note,
                },
            )
        )
    return ChartJob(symbol, parts, day, stale_after)


# ── 美股：CFTC、FINRA、13F、基本面 ───────────────────────────────────────────


@_rows("us.cftc.")
def _us_cftc(ctx: _Ctx, row_id: str) -> RowResult:
    key = row_id.rsplit(".", 1)[1]
    found = ctx.obs(OBS_US_CFTC.format(key))
    if found is None:
        raise _Unavailable("missing")
    payload = found["payload"]
    day = _iso_day(payload.get("report_date")) or _iso_day(found.get("data_date"))
    lev_l, lev_s, am_l, am_s = (_int(payload.get(name)) for name in ("lev_l", "lev_s", "am_l", "am_s"))
    if day is None or None in (lev_l, lev_s, am_l, am_s):
        raise _Unavailable("missing")

    def change(long_key: str, short_key: str) -> Optional[int]:
        long_chg, short_chg = _int(payload.get(long_key)), _int(payload.get(short_key))
        return None if long_chg is None or short_chg is None else long_chg - short_chg

    values = {
        "contract": f"{_CFTC_NAMES[key]} {_CFTC_BASIS.get(str(payload.get('basis')), '')}".strip(),
        "lev_net": lev_l - lev_s,
        "lev_net_chg": change("chg_lev_l", "chg_lev_s"),
        "am_net": am_l - am_s,
        "am_net_chg": change("chg_am_l", "chg_am_s"),
        "oi": _int(payload.get("oi")),
    }
    return RowResult(values, data_date=day, stale_after=ctx.stale(market_calc.KIND_CFTC, day), source="CFTC",
                     basis=market_calc.TEXT_CFTC_NOTE)


@_rows("us.margin")
def _us_margin(ctx: _Ctx, row_id: str) -> RowResult:
    months: dict[str, float] = {}
    for row in ctx.payload(OBS_US_MARGIN).get("rows") or []:
        if isinstance(row, list) and len(row) == 2 and isinstance(row[0], str) and _MONTH_RE.fullmatch(row[0]):
            value = _num(row[1])
            if value is not None and value > 0:
                months[row[0]] = value
    if not months:
        raise _Unavailable("missing")
    latest = max(months)
    year, month = int(latest[:4]), int(latest[5:])
    last_year = months.get(f"{year - 1:04d}-{month:02d}")
    debit = months[latest]
    month_end = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    values = {"month": latest, "debit_musd": debit, "yoy_pct": (debit / last_year - 1.0) * 100.0 if last_year else None}
    # FINRA 以該月月底為錨＋62 天（§18.9）
    return RowResult(values, data_date=month_end, stale_after=ctx.stale(market_calc.KIND_FINRA, month_end),
                     source="FINRA", basis="融資餘額（百萬美元）", period=latest)


def _top10(ctx: _Ctx, etf: str) -> tuple[list[dict], dict]:
    found = ctx.obs(OBS_US_TOP10.format(etf))
    if found is None:
        raise _Unavailable("missing")
    items = []
    for item in found["payload"].get("rows") or []:
        if not isinstance(item, dict):
            continue
        pick = item.get("pick")
        symbols = [s for s in item.get("symbols") or [] if isinstance(s, str) and _US_SYMBOL_RE.fullmatch(s)]
        if not isinstance(pick, str) or not _US_SYMBOL_RE.fullmatch(pick) or not symbols:
            continue
        items.append(
            {"pick": pick, "symbols": sorted(symbols), "weight": _num(item.get("weight")),
             "name": _clean(item.get("name"), market_calc.MAX_NAME) or pick}
        )
    if not items:
        raise _Unavailable("missing")
    return items, found


def _list_day(found: dict) -> Optional[date]:
    return _iso_day(found["payload"].get("list_as_of")) or _iso_day(found.get("data_date"))


@_rows("us.13f.")
def _us_13f(ctx: _Ctx, row_id: str) -> RowResult:
    etf = row_id.rsplit(".", 1)[1]
    items, top_found = _top10(ctx, etf)
    rows = []
    periods: list[date] = []
    for item in items:
        found = ctx.obs(OBS_US_13F.format(item["pick"]))
        data = found["payload"] if found else {}
        rows.append(
            {
                "name": item["name"],
                "symbols": item["symbols"],
                "weight": item["weight"],
                "increased": _int(data.get("increased")),
                "decreased": _int(data.get("decreased")),
                "new": _int(data.get("new")),
                "soldout": _int(data.get("soldout")),
                "own_pct": _num(data.get("own_pct")),
            }
        )
        periods.extend(day for day in (_iso_day(p) for p in data.get("holder_periods") or []) if day is not None)
    mode, mixed = market_calc.holder_period_mode(periods)
    if mode is None:
        raise _Unavailable("missing")
    template = market_calc.TEXT_13F_PERIOD if mixed else market_calc.TEXT_13F_PERIOD_SINGLE
    list_day = _list_day(top_found)
    md = _md(list_day) if list_day else "日期未標"
    basis = market_calc.TEXT_SPY_WEIGHT.format(md=md) if etf == "VOO" else f"依 {etf} 持股權重（MoneyDJ {md}）"
    values = {
        "rows": rows,
        "summary": market_calc.inst13f_summary(rows),
        "holder_period_note": template.format(md=_md(mode)),
        "weight_basis": basis,
    }
    # 13F 的彙總期間來源未標（state=unknown_period）；過期＝前十大持有人申報期間的眾數＋155 天（§18.9）
    return RowResult(values, data_date=None, stale_after=ctx.stale(market_calc.KIND_13F, mode), source="Nasdaq 13F",
                     basis=market_calc.TEXT_13F_LAG, state="unknown_period")


@_rows("us.fund.")
def _us_fund(ctx: _Ctx, row_id: str) -> RowResult:
    etf = row_id.rsplit(".", 1)[1]
    items, _found = _top10(ctx, etf)
    rows = []
    pes: list[Optional[float]] = []
    used_stale: list[float] = []
    for item in items:
        found = ctx.obs(OBS_US_FUND.format(item["pick"]))
        pe: Optional[float] = None
        status = "missing"
        if found is not None:
            quarters = []
            for entry in found["payload"].get("quarters") or []:
                if isinstance(entry, list) and len(entry) == 2 and _iso_day(entry[0]) is not None:
                    quarters.append((_iso_day(entry[0]), _num(entry[1])))
            latest = max((end for end, _ in quarters), default=None)
            company_stale = ctx.stale(market_calc.KIND_US_FUND, latest) if latest else None
            if company_stale is not None and company_stale >= ctx.now:
                result = market_calc.eps_metrics(quarters, _num(found["payload"].get("market_cap_usd")))
                pe, status = result.pe, result.status
                used_stale.append(company_stale)
            elif company_stale is not None:
                status = "stale"  # 過了季度結束＋160 天：這家不算（衍生列不拿過期的輸入）
        pes.append(pe)
        rows.append({"name": item["name"], "symbols": item["symbols"], "pe": pe, "status": status})
    if not used_stale:
        raise _Unavailable("missing")
    median, valid = market_calc.median_with_count(pes)
    values = {"rows": rows, "pe_median": median, "pe_valid": valid, "total": len(rows)}
    return RowResult(values, data_date=None, stale_after=min(used_stale), source="Nasdaq financials",
                     basis="本益比＝市值 ÷ 近四季普通股淨利")


# ── 台股 ────────────────────────────────────────────────────────────────────


def _tw_exright_spans(ctx: _Ctx) -> list[tuple[date, date]]:
    return ctx.shared("coverage.twt49u", lambda: list(ctx.store.coverage(COVERAGE_TWT49U)))


def _tw_adjusted(ctx: _Ctx, code: str, day: date) -> list[Any]:
    """≤day 的收盤，還原除權息（只乘除權息日之前，PLAN §3.2）。"""
    rows = _closes_upto(ctx.closes(TW, code), day)
    exrights = ctx.shared(f"exrights:{code}", lambda: list(ctx.store.exrights(code)))
    return market_calc.adjust_tw_closes(rows, exrights, code=code)


def _tw_ready_day(ctx: _Ctx) -> date:
    """用還原價的台股列（0050、成分寬度）只發布「除權息資料涵蓋到」的那天（§18.11），否則往前一天。"""
    cal = ctx.trading(TW)
    spans = _tw_exright_spans(ctx)
    for day in reversed(cal.sessions_ending(ctx.tw_day(), BREADTH_LOOKBACK)):
        if _covered(spans, day):
            return day
    raise _Unavailable("exrights")


@_rows("tw.taiex")
def _tw_taiex(ctx: _Ctx, row_id: str) -> RowResult:
    cal = ctx.trading(TW)
    rows = _closes_upto(ctx.closes(TW, TAIEX), ctx.tw_day())
    if not rows:
        raise _Unavailable("missing")
    day = rows[-1][0]
    tech = market_calc.technicals({row[0]: row[1] for row in rows}, cal, day)
    if tech.close is None:
        raise _Unavailable("calendar")
    values = {
        "close": tech.close,
        "chg": tech.chg,
        "chg_pct": tech.chg_pct,
        "ma_dist": {str(n): tech.ma_dist.get(n) for n in MA_WINDOWS},
        "above_ma20": tech.above.get(20),
        "dd240_pct": tech.dd240_pct,
    }
    return RowResult(values, data_date=day, stale_after=ctx.stale(market_calc.KIND_TW_DAILY, day),
                     source="FinMind" if rows[-1][3] == "finmind" else "證交所", basis="加權指數收盤",
                     facts={"TAIEX.chg_pct": tech.chg_pct, "TAIEX.above_ma20": tech.above.get(20)})


@_rows("tw.etf.")
def _tw_etf(ctx: _Ctx, row_id: str) -> RowResult:
    code = row_id.rsplit(".", 1)[1]
    cal = ctx.trading(TW)
    rows = _tw_adjusted(ctx, code, _tw_ready_day(ctx))
    if not rows:
        raise _Unavailable("missing")
    day = rows[-1][0]
    tech, volume = _etf_parts(rows, cal, day)
    if tech.close is None:
        raise _Unavailable("calendar")
    return RowResult(
        market_calc.etf_values(tech, volume),
        data_date=day,
        stale_after=ctx.stale(market_calc.KIND_TW_DAILY, day),
        source="證交所",
        basis=market_calc.TEXT_TW_ADJUSTED,
        facts={f"{code}.chg_pct": tech.chg_pct, f"{code}.above_ma20": tech.above.get(20)},
    )


def _tw_members(ctx: _Ctx) -> _HoldingInfo:
    def load() -> _HoldingInfo:
        found = ctx.obs(OBS_TW_HOLDINGS)
        if found is None:
            raise _Unavailable("missing")
        payload = found["payload"]
        codes = set()
        for row in payload.get("rows") or []:  # [代號, 名稱, 權重]（舊格式是 {"symbol": …}，兩種都收）
            code = row[0] if isinstance(row, list) and row else row.get("symbol") if isinstance(row, dict) else None
            if isinstance(code, str) and _TW_CODE_RE.fullmatch(code) and code not in (TW_ETF, TAIEX):
                codes.add(code)
        if not codes:
            raise _Unavailable("missing")
        return _HoldingInfo(sorted(codes), _iso_day(payload.get("as_of")) or _iso_day(found.get("data_date")), payload)

    return ctx.shared("holdings.tw", load)


@_rows("tw.breadth.")
def _tw_breadth(ctx: _Ctx, row_id: str) -> RowResult:
    cal = ctx.trading(TW)
    info = _tw_members(ctx)
    latest = ctx.tw_day()
    spans = _tw_exright_spans(ctx)
    # 還原因子只乘在除權息日之前：同一窗口內整段同乘一個數，站上／跌破的比較不變，所以算到 T 一次就夠
    closes = ctx.shared(
        "closes.tw.members",
        lambda: {code: {row[0]: row[1] for row in _tw_adjusted(ctx, code, latest)} for code in info.members},
    )
    day = _complete_day(closes, info.members, cal, latest, extra_ok=lambda d: _covered(spans, d))
    if day is None:
        raise _Unavailable("coverage")
    quarantine = ctx.quarantine(TW)
    per_n = {n: market_calc.breadth(closes, cal, day, n, members=info.members, quarantine=quarantine) for n in MA_WINDOWS}
    ad = market_calc.ad_counts(closes, cal, day, members=info.members, quarantine=quarantine)
    highs = market_calc.highs_lows_240(closes, cal, day, members=info.members, quarantine=quarantine)
    note = None
    if info.as_of is not None and ctx.stale(market_calc.KIND_HOLDINGS_MONEYDJ, info.as_of) < ctx.now:
        note = market_calc.TEXT_MEMBER_LIST_DATE.format(md=_md(info.as_of))
    label = f"0050 成分（MoneyDJ {_md(info.as_of)}）" if info.as_of else "0050 成分（MoneyDJ）"
    values = market_calc.breadth_values(
        per_n, ad if _usable(ad) else None, highs if _usable(highs) else None, universe_label=label, coverage_note=note
    )
    b20 = per_n[20]
    ctx.progress["tw_last_complete_day"] = day.isoformat()
    return RowResult(
        values,
        data_date=day,
        stale_after=ctx.stale(market_calc.KIND_TW_DAILY, day),
        source="證交所",
        basis=market_calc.TEXT_TW_ADJUSTED,
        facts={"0050.breadth20_pct": b20.pct if b20.status == market_calc.STATUS_OK else None},
    )


def _latest_day_entry(days: Any, upto: date) -> tuple[date, dict]:
    """{"YYYY-MM-DD": {...}} 裡 ≤upto 的最新一天。"""
    best: Optional[tuple[date, dict]] = None
    for key, item in (days or {}).items():
        day = _iso_day(key)
        if day is None or day > upto or not isinstance(item, dict):
            continue
        if best is None or day > best[0]:
            best = (day, item)
    if best is None:
        raise _Unavailable("missing")
    return best


@_rows("tw.ad")
def _tw_ad(ctx: _Ctx, row_id: str) -> RowResult:
    day, item = _latest_day_entry(ctx.payload(OBS_TW_ALLBUT).get("days"), ctx.tw_day())
    adv, dec, flat = _int(item.get("adv")), _int(item.get("dec")), _int(item.get("flat"))
    if None in (adv, dec, flat):
        raise _Unavailable("missing")
    values = {
        "adv": adv,
        "dec": dec,
        "flat": flat,
        "adv_limit": _int(item.get("adv_limit")),
        "dec_limit": _int(item.get("dec_limit")),
        "no_compare": _int(item.get("no_compare")),
    }
    return RowResult(values, data_date=day, stale_after=ctx.stale(market_calc.KIND_TW_DAILY, day), source="證交所",
                     basis="股票欄；括號是漲停／跌停家數")


@_rows("tw.turnover")
def _tw_turnover(ctx: _Ctx, row_id: str) -> RowResult:
    cal = ctx.trading(TW)
    rows = _closes_upto(ctx.closes(TW, TAIEX), ctx.tw_day())
    if not rows or rows[-1][2] is None:
        raise _Unavailable("missing")
    day = rows[-1][0]
    # 加權指數那一列的「量」＝成交金額「總計(1~15)」（元；market_pipeline_tw 的約定，和 FinMind 同口徑）
    volumes = {row[0]: row[2] for row in rows if row[2] is not None}
    closes = {row[0]: row[1] for row in rows}
    volume = market_calc.volume_state(volumes, day, calendar=cal, closes=closes,
                                      day_flag=market_calc.volume_day_flag(day, cal))
    values = {
        "value_twd": float(rows[-1][2]),
        "vol_ratio": volume.ratio,
        "vol_pct250": volume.pct250,
        "vol_label": volume.label,
        "vol_flag": volume.day_flag,
    }
    return RowResult(values, data_date=day, stale_after=ctx.stale(market_calc.KIND_TW_DAILY, day), source="證交所",
                     basis="成交金額總計(1~15)",
                     facts={"TAIEX.vol_label": volume.label, "TAIEX.vol_flag": volume.day_flag})


def _inst_yi(entry: Any) -> Optional[tuple[float, float, float]]:
    """一天的三大法人（元）→ (外資, 投信, 自營＝自行＋避險)（億元）。"""
    if not isinstance(entry, dict):
        return None
    foreign, trust = _num(entry.get("foreign")), _num(entry.get("trust"))
    dealer_self, dealer_hedge = _num(entry.get("dealer_self")), _num(entry.get("dealer_hedge"))
    if None in (foreign, trust, dealer_self, dealer_hedge):
        return None
    return foreign / 1e8, trust / 1e8, (dealer_self + dealer_hedge) / 1e8


@_rows("tw.inst3")
def _tw_inst3(ctx: _Ctx, row_id: str) -> RowResult:
    cal = ctx.trading(TW)
    days = ctx.payload(OBS_TW_INST3).get("days") or {}
    day, item = _latest_day_entry(days, ctx.tw_day())
    today = _inst_yi(item)
    if today is None:
        raise _Unavailable("missing")
    window = cal.sessions_ending(day, market_calc.BREADTH_EXTRA)  # 5 個交易日（含資料日）
    found = [entry for entry in (_inst_yi(days.get(d.isoformat())) for d in window) if entry is not None]
    complete = len(window) == 5 and len(found) == 5  # 5 日要齊（§4）
    sums = [math.fsum(entry[i] for entry in found) for i in range(3)] if complete else [None, None, None]
    values = {
        "foreign_yi": today[0],
        "trust_yi": today[1],
        "dealer_yi": today[2],
        "foreign_5d_yi": sums[0],
        "trust_5d_yi": sums[1],
        "dealer_5d_yi": sums[2],
        "days_5d": len(found),
    }
    return RowResult(
        values, data_date=day, stale_after=ctx.stale(market_calc.KIND_TW_DAILY, day), source="證交所",
        basis="外資＝外資及陸資(不含外資自營商)；億元",
        facts={"tw.foreign": today[0], "tw.foreign_5d": sums[0], "tw.trust_5d": sums[1]},
    )


@_rows("tw.margin")
def _tw_margin(ctx: _Ctx, row_id: str) -> RowResult:
    payload = ctx.payload(OBS_TW_MARGIN)
    day = _iso_day(payload.get("date"))
    balance, previous = _num(payload.get("margin_balance_k")), _num(payload.get("margin_prev_k"))
    if day is None or balance is None or day > ctx.tw_day():
        raise _Unavailable("missing")
    change = (balance - previous) * 1000 / 1e8 if previous is not None else None
    values = {"balance_yi": balance * 1000 / 1e8, "change_yi": change}
    return RowResult(values, data_date=day, stale_after=ctx.stale(market_calc.KIND_TW_DAILY, day), source="證交所",
                     basis="融資金額今日餘額（參考）；億元", facts={"tw.margin_chg": change})


@_rows("tw.futures")
def _tw_futures(ctx: _Ctx, row_id: str) -> RowResult:
    payload = ctx.payload(OBS_TW_FUTURES)
    day = _iso_day(payload.get("date"))
    foreign = payload.get("foreign") if isinstance(payload.get("foreign"), dict) else {}

    def net(code: str) -> Optional[int]:
        entry = foreign.get(code)
        return _int(entry.get("oi_net")) if isinstance(entry, dict) else None

    big = net("TX")
    if day is None or big is None:
        raise _Unavailable("missing")
    # 日變化：F3 在 obs 留最近 10 個資料日（days: {iso: {TX|MTX|TMF: {oi_net, …}}}）。前一筆要剛好是
    # prev_session(資料日) 才算（PLAN §18.8；中間缺一天就不算，免得把兩天的變化當成一天）。矛盾第 6 條靠它。
    change = None
    days = payload.get("days") if isinstance(payload.get("days"), dict) else {}
    earlier = {}
    for key, entry in days.items():
        when = _iso_day(key)
        if when is not None and when < day and isinstance(entry, dict):
            earlier[when] = entry
    if earlier:
        prev_day = max(earlier)
        prev_tx = earlier[prev_day].get("TX")
        prev_big = _int(prev_tx.get("oi_net")) if isinstance(prev_tx, dict) else None
        try:
            adjacent = ctx.trading(TW).prev_session(day) == prev_day
        except _Unavailable:
            adjacent = False
        if prev_big is not None and adjacent:
            change = big - prev_big
    values = {"foreign_net_big": big, "foreign_net_big_chg": change, "foreign_net_mini": net("MTX"),
              "foreign_net_micro": net("TMF")}
    return RowResult(values, data_date=day, stale_after=ctx.stale(market_calc.KIND_TW_DAILY, day), source="期交所",
                     basis="外資未平倉淨口數；大台＝臺股期貨", facts={"tw.fut_net": big, "tw.fut_net_chg": change})


@_rows("tw.options")
def _tw_options(ctx: _Ctx, row_id: str) -> RowResult:
    payload = ctx.payload(OBS_TW_TXO)
    day = _iso_day(payload.get("date"))
    if day is None or day > ctx.tw_day():
        raise _Unavailable("missing")
    cal = ctx.trading(TW)
    fields = list(payload.get("fields") or ["month", "strike", "kind", "oi", "settle"])
    rows = [dict(zip(fields, row)) for row in payload.get("rows") or [] if isinstance(row, list)]
    month = market_calc.txo_front_month(day, cal)
    # 台指「現價」＝期交所資料日那天的加權收盤（期交所常晚一天，§18.13）
    spot = next((row[1] for row in ctx.closes(TW, TAIEX) if row[0] == day), None)
    walls = market_calc.txo_walls(rows, month, spot) if spot else None
    values = {
        "month": month,
        "spot": spot,
        "call_wall": walls.call_wall if walls else None,
        "put_wall": walls.put_wall if walls else None,
        "max_pain": walls.max_pain if walls else None,
        "call_wall_dist_pct": walls.call_wall_dist_pct if walls else None,
        "put_wall_dist_pct": walls.put_wall_dist_pct if walls else None,
        "status": walls.status if walls else market_calc.STATUS_INSUFFICIENT,
        "pcr_oi_pct": None,
        "foreign_call_net": None,
        "foreign_put_net": None,
    }
    pcr = ctx.obs(OBS_TW_PCR)
    for item in (pcr["payload"].get("rows") or []) if pcr else []:
        if isinstance(item, dict) and _iso_day(item.get("date")) == day:  # 同一資料日才放
            values["pcr_oi_pct"] = _num(item.get("oi_ratio_pct"))
    inst = ctx.obs(OBS_TW_OPTIONS_INST)
    if inst and _iso_day(inst["payload"].get("date")) == day:
        values["foreign_call_net"] = _int((inst["payload"].get("foreign_call") or {}).get("oi_net"))
        values["foreign_put_net"] = _int((inst["payload"].get("foreign_put") or {}).get("oi_net"))
    stale_after = ctx.stale(market_calc.KIND_TW_DAILY, day)
    chart = _txo_chart(rows, month, spot, walls, day, stale_after) if walls is not None else None
    return RowResult(values, data_date=day, stale_after=stale_after, source="期交所",
                     basis="最近月選（一般盤）；PCR 是全部台指選擇權", chart=chart)


def _txo_chart(rows: list[dict], month: str, spot: float, walls: Any, day: date, stale_after: float) -> ChartJob:
    low, high = spot * (1 - market_calc.WALL_BAND), spot * (1 + market_calc.WALL_BAND)
    calls: dict[float, int] = {}
    puts: dict[float, int] = {}
    for row in rows:
        strike, oi = _num(row.get("strike")), _int(row.get("oi"))
        kind = str(row.get("kind") or "").strip().upper()
        if str(row.get("month") or "").strip() != month or strike is None or oi is None or oi < 0:
            continue
        if not low <= strike <= high or kind not in ("C", "P"):
            continue
        target = calls if kind == "C" else puts
        target[strike] = target.get(strike, 0) + oi
    strikes = sorted(set(calls) | set(puts))
    month_text = f"{int(month[4:])} 月" if len(month) == 6 and month.isdigit() else "最近月"
    parts = [
        (
            "render_oi_chart",
            {
                "title": f"台指選擇權未平倉（{month_text}）｜{_md(day)}",
                "strikes": strikes,
                "call_oi": [calls.get(k, 0) for k in strikes],
                "put_oi": [puts.get(k, 0) for k in strikes],
                "spot": spot,
                "call_wall": walls.call_wall,
                "put_wall": walls.put_wall,
                "max_pain": walls.max_pain,
                "range_note": f"現價＝加權指數 {_md(day)} 收盤，±15%；期交所資料日 {_md(day)}",
            },
        )
    ]
    return ChartJob("TXO", parts, day, stale_after)


@_rows("tw.fund.")
def _tw_fund(ctx: _Ctx, row_id: str) -> RowResult:
    info = _tw_members(ctx)
    stale: list[float] = []
    pe_median, pe_valid = None, 0
    pe_found = ctx.obs(OBS_TW_PE)
    pe_day = _iso_day(pe_found["payload"].get("date")) if pe_found else None
    if pe_found and pe_day is not None:
        pe_stale = ctx.stale(market_calc.KIND_TW_DAILY, pe_day)
        if pe_stale >= ctx.now:  # 過期的輸入不拿來算（衍生列）
            table = pe_found["payload"].get("pe") or {}
            pe_median, pe_valid = market_calc.median_with_count(_num(table.get(code)) for code in info.members)
            stale.append(pe_stale)
    rev_month, rev_pos, rev_valid = None, 0, 0
    rev_found = ctx.obs(OBS_TW_REVENUE)
    if rev_found:
        table = rev_found["payload"].get("rows") or {}
        revenues = {}
        for code in info.members:
            entry = table.get(code)
            if isinstance(entry, list) and len(entry) == 3:
                revenues[code] = {"year": _int(entry[0]), "month": _int(entry[1]), "yoy": _num(entry[2])}
        month, pos, valid = market_calc.revenue_positive_share(revenues)
        if month is not None:
            rev_stale = ctx.stale(market_calc.KIND_TW_MONTHLY, date(month[0], month[1], 1))
            if rev_stale >= ctx.now:
                rev_month, rev_pos, rev_valid = f"{month[0]:04d}-{month[1]:02d}", pos, valid
                stale.append(rev_stale)
    if not stale:
        raise _Unavailable("missing")
    values = {"pe_median": pe_median, "pe_valid": pe_valid, "total": len(info.members), "rev_month": rev_month,
              "rev_pos": rev_pos, "rev_valid": rev_valid}
    return RowResult(values, data_date=pe_day if pe_valid else None, stale_after=min(stale), source="證交所 OpenAPI",
                     basis="本益比中位數；月營收年增 >0 只比同一月份")


# ── 宏觀 ────────────────────────────────────────────────────────────────────


def _series_change(series: list[tuple[date, float]], cal: Optional[market_calc.TradingCalendar]) -> Optional[float]:
    """日變化：前一筆要是 prev_session(資料日)（PLAN §18.8）；沒給日曆就用序列自己的前一列。"""
    if len(series) < 2:
        return None
    (prev_day, prev), (day, value) = series[-2], series[-1]
    if cal is not None and cal.prev_session(day) != prev_day:
        return None
    return value - prev


@_rows("macro.VIX", "macro.VIX3M")
def _macro_cboe(ctx: _Ctx, row_id: str) -> RowResult:
    key = row_id.rsplit(".", 1)[1]
    payload = ctx.payload(OBS_US_QUOTE.format(key))
    value = _num(payload.get("close"))
    last = payload.get("last_trade_et")
    try:
        moment = datetime.fromisoformat(last) if isinstance(last, str) else None
    except ValueError:
        moment = None
    if value is None or moment is None:
        raise _Unavailable("missing")
    day = market_calc.et_date(moment)  # 資料日＝last_trade_time 的美東日期（§18.1、I6）
    cal = ctx.trading(US)
    history = sorted(
        (d, v)
        for d, v in ((_iso_day(item[0]), _num(item[1])) for item in payload.get("history") or []
                     if isinstance(item, list) and len(item) == 2)
        if d is not None and v is not None and d <= day
    )
    stored = _series_change(history, cal) if history and history[-1][0] == day else None
    change = _num(payload.get("price_change"))
    # CBOE 的 price_change 和自存前一日互相核對（§18.8）：兩個都有卻對不上 → 不顯示日變化
    if change is not None and stored is not None and abs(change - stored) > 0.011:
        change = None
    elif change is None:
        change = stored
    values = {"value": value, "chg": change, "chg_pct": _num(payload.get("price_change_pct")), "source": "CBOE"}
    return RowResult(values, data_date=day, stale_after=ctx.stale(market_calc.KIND_US_DAILY, day), source="CBOE",
                     basis="收盤", facts={key: value})


@_rows("macro.UST3M", "macro.UST10Y")
def _macro_ust(ctx: _Ctx, row_id: str) -> RowResult:
    column = 1 if row_id.endswith("UST3M") else 3  # [日期, 3M, 2Y, 10Y, 30Y]
    latest = ctx.us_day()
    series = []
    for row in ctx.payload(OBS_US_UST).get("rows") or []:
        if isinstance(row, list) and len(row) > column:
            day, value = _iso_day(row[0]), _num(row[column])
            if day is not None and value is not None and day <= latest:
                series.append((day, value))
    series.sort()
    if not series:
        raise _Unavailable("missing")
    day, value = series[-1]
    # 美債用自己序列的前一列（債市假日股市照開，§18.8）
    change = value - series[-2][1] if len(series) >= 2 else None
    values = {"value": value, "chg": change, "chg_pct": None, "source": "美國財政部"}
    return RowResult(values, data_date=day, stale_after=ctx.stale(market_calc.KIND_US_DAILY, day),
                     source="美國財政部", basis="殖利率（%）")


@_rows("macro.SPY_DD")
def _macro_spy_dd(ctx: _Ctx, row_id: str) -> RowResult:
    cal = ctx.trading(US)
    rows = _closes_upto(ctx.closes(US, "SPY"), ctx.us_day())
    if not rows:
        raise _Unavailable("missing")
    day = rows[-1][0]
    tech = market_calc.technicals({row[0]: row[1] for row in rows}, cal, day)
    if tech.dd240_pct is None:
        raise _Unavailable("missing")
    values = {"value": tech.dd240_pct, "chg": None, "chg_pct": None, "source": "SPY 價格代理"}
    return RowResult(values, data_date=day, stale_after=ctx.stale(market_calc.KIND_US_DAILY, day),
                     source="SPY 價格代理", basis="離 240 日高點（%）")


_MACRO_KEYS = ("DXY", "WTI", "BRENT", "GOLD", "SILVER", "USDTWD", "RUT", "DJI", "IXIC", "MOVE")


def _macro_daily(ctx: _Ctx, found: dict, latest: date) -> RowResult:
    """market_pipeline_misc 的一格日線：值、日變化（已照 §18.8 只在前一筆＝前一個交易日時給）、資料日、來源。"""
    payload = found["payload"]
    source = payload.get("source") if isinstance(payload.get("source"), str) else ""
    day, value = _iso_day(payload.get("date")), _num(payload.get("value"))
    change, change_pct = _num(payload.get("chg")), _num(payload.get("chg_pct"))
    if day is not None and day > latest:  # 晚於 D 的不要（§18.8）：退回日線裡 ≤D 的最後一根、不給日變化
        bars = sorted((d, v) for d, v in ((_iso_day(item[0]), _num(item[1])) for item in payload.get("bars") or []
                                          if isinstance(item, list) and len(item) >= 2)
                      if d is not None and v is not None and d <= latest)
        day, value, change, change_pct = (bars[-1][0], bars[-1][1], None, None) if bars else (None, None, None, None)
    if day is None or value is None or not source:
        raise _Unavailable("missing")
    values = {"value": value, "chg": change, "chg_pct": change_pct, "source": source}
    return RowResult(values, data_date=day, stale_after=ctx.stale(market_calc.KIND_US_DAILY, day), source=source,
                     basis="日收盤")


@_rows(*(f"macro.{key}" for key in _MACRO_KEYS))
def _macro_misc(ctx: _Ctx, row_id: str) -> RowResult:
    key = row_id.rsplit(".", 1)[1]
    found = ctx.obs(OBS_MACRO.format(key))
    if found is None:
        raise _Unavailable("missing")
    return _macro_daily(ctx, found, ctx.us_day())


@_rows("macro.BTC")
def _macro_btc(ctx: _Ctx, row_id: str) -> RowResult:
    """BTC：Coinbase 現貨（過期＝抓取＋6 小時，§18.9）；它過期或沒有才用 Yahoo 的日線備援。"""
    found = ctx.obs(OBS_BTC)
    if found is not None:
        payload = found["payload"]
        fetched = _epoch(payload.get("fetched_utc")) or _num(found.get("last_success_at"))
        value = _num(payload.get("value"))
        if fetched is not None and value is not None and value > 0:
            stale_after = ctx.stale(market_calc.KIND_BTC, fetched)
            if stale_after >= ctx.now:
                source = payload.get("source") if isinstance(payload.get("source"), str) else "Coinbase"
                values = {"value": value, "chg": None, "chg_pct": None, "source": source}
                return RowResult(values, data_date=datetime.fromtimestamp(fetched, _UTC).date(), stale_after=stale_after,
                                 source=source, basis="現貨價（美元）")
    backup = ctx.obs(OBS_BTC_YAHOO)
    if backup is None:
        raise _Unavailable("missing")
    return _macro_daily(ctx, backup, ctx.us_day())


# ── 事件、季節性、選舉 ───────────────────────────────────────────────────────


# 經濟日曆的英文名稱 → bot 認得的事件種類（卡上用 bot 自己的中文名；其他種類照錄名稱）
_ECON_KINDS = (
    ("nfp", re.compile(r"non[\s-]?farm\s+payrolls?", re.IGNORECASE)),
    ("ppi", re.compile(r"\bPPI\b|producer\s+price", re.IGNORECASE)),
    ("pce", re.compile(r"\bPCE\b|personal\s+consumption\s+expenditures?", re.IGNORECASE)),
    ("cpi", re.compile(r"\bCPI\b|consumer\s+price\s+index", re.IGNORECASE)),
    ("gdp", re.compile(r"\bGDP\b|gross\s+domestic\s+product", re.IGNORECASE)),
)


def _econ_kind(name: Optional[str]) -> str:
    if not name:
        return "econ"
    return next((kind for kind, pattern in _ECON_KINDS if pattern.search(name)), "econ")


@_rows("events.next")
def _events(ctx: _Ctx, row_id: str) -> RowResult:
    sources = {name: ctx.obs(obs_id) for name, obs_id in
               (("fomc", OBS_US_FOMC), ("econ", OBS_US_ECON), ("earnings", OBS_US_EARNINGS))}
    if not any(sources.values()):
        raise _Unavailable("missing")
    horizon = ctx.now + EVENT_HORIZON_DAYS * 86400
    grace = market_calc.EVENT_GRACE.total_seconds()
    items: list[dict] = []

    def add(at: Optional[float], kind: str, name: Optional[str], **extra: Any) -> None:
        if at is not None and name is not None and ctx.now - grace < at <= horizon:
            items.append({"name": name, "at": at, "kind": kind, **extra})

    for meeting in (sources["fomc"] or {}).get("payload", {}).get("meetings") or []:
        if isinstance(meeting, dict):
            add(_epoch(meeting.get("decision_utc")), "fomc", "FOMC")
    seen_kinds: set[tuple[str, float]] = set()
    for day_items in ((sources["econ"] or {}).get("payload", {}).get("days") or {}).values():
        for entry in day_items if isinstance(day_items, list) else []:
            if not isinstance(entry, list) or len(entry) != 2:
                continue
            at, name = _epoch(entry[0]), _clean(entry[1], market_calc.MAX_TITLE)  # 沒有時間的事件不猜時間、不列
            kind = _econ_kind(name)
            if kind != "econ" and at is not None:
                if (kind, at) in seen_kinds:  # 同一時刻的 CPI 與核心 CPI：卡上只要一個「消費者物價」
                    continue
                seen_kinds.add((kind, at))
            add(at, kind, name)
    for symbol, entry in ((sources["earnings"] or {}).get("payload", {}).get("symbols") or {}).items():
        day = _iso_day(entry.get("date")) if isinstance(entry, dict) else None
        if day is None or not isinstance(symbol, str) or not _US_SYMBOL_RE.fullmatch(symbol):
            continue
        # 財報日只有日期：當成美東當天 16:00（大型股多在收盤後公布），並標 time_known=False；name＝代號
        at = datetime.combine(day, time(16, 0), tzinfo=_ET).timestamp()
        add(at, "earnings", symbol, estimated=bool(entry.get("estimated", True)), time_known=False)
    # 同一時刻：FOMC／CPI／非農排在前面（「下一個事件」才不會被同時刻的失業率等蓋掉）
    items.sort(key=lambda item: (item["at"], market_calc.EVENT_PRIORITY.get(item["kind"], market_calc.EVENT_PRIORITY_OTHER),
                                 item["kind"], item["name"]))
    items = market_calc.keep_events(items, EVENTS_KEEP, lambda item: item["kind"])  # FOMC／CPI／非農一定留
    # 列表的完整性看經濟日曆（每天抓）；它從沒成功過才退而看 FOMC、財報日（每週）
    econ = sources["econ"]
    ages = [("econ", econ)] if econ and isinstance(econ.get("last_success_at"), (int, float)) else [
        (name, found) for name, found in sources.items()
        if found and isinstance(found.get("last_success_at"), (int, float))
    ]
    if not ages:
        raise _Unavailable("missing")
    limits = [float(found["last_success_at"]) + EVENT_SOURCE_MAX_AGE_S[name] for name, found in ages]
    if items:  # 第一個事件過了 2 小時，「下一個事件」就要換（§18.9）
        limits.append(ctx.stale(market_calc.KIND_EVENT, items[0]["at"]))
    return RowResult({"items": items}, data_date=None, stale_after=min(limits),
                     source="聯準會、Nasdaq 經濟日曆、Nasdaq 財報日", basis="財報日只有日期")


@_rows("season.now")
def _season(ctx: _Ctx, row_id: str) -> RowResult:
    def load() -> dict:
        found = market_paths.read_private_file(Path(ctx.paths.seasonality), 1_000_000)
        if found is None:
            raise _Unavailable("missing")
        table = json.loads(found[0].decode("utf-8"))
        if not isinstance(table, dict):
            raise _Unavailable("missing")
        return table

    table = ctx.shared("seasonality", load)
    local = datetime.fromtimestamp(ctx.now, _TPE)
    values = market_calc.season_values(table, local.year, local.month)
    sp = table.get("sp500") if isinstance(table.get("sp500"), dict) else {}
    sample = sp.get("sample")
    end_year = sample[1] if isinstance(sample, list) and len(sample) == 2 and isinstance(sample[1], int) else None
    if end_year is None or values.get("sp500") is None:
        raise _Unavailable("missing")
    # 「這個月」的統計：換月就要換一份（publish 每 15 分鐘會重算；停了就到月底過期）
    next_month = date(local.year + (local.month == 12), local.month % 12 + 1, 1)
    month_end = datetime.combine(next_month, time(0, 0), tzinfo=_TPE).timestamp()
    stale_after = min(ctx.stale(market_calc.KIND_SEASON, end_year), month_end)
    return RowResult(values, data_date=None, stale_after=stale_after, source="datahub（Shiller）、FinMind",
                     basis=market_calc.TEXT_SEASON_SP_BASIS, period=f"{sample[0]}–{end_year}")


def _quote_entry(ctx: _Ctx, obs_id: str, chamber: str) -> Optional[tuple[dict, Optional[float]]]:
    """預測市場一則（PredictionQuote 欄位）：還沒結算的報價過了抓取＋6 小時就不列（不拿舊的報價）；
    已結算的結果不會變，不受 6 小時限制（結算後 collect 一天才看一次）。回 (那則, 過期時間|None)。"""
    found = ctx.obs(obs_id)
    if found is None:
        return None
    payload = found["payload"]
    platform = payload.get("platform").lower() if isinstance(payload.get("platform"), str) else None
    fetched = _epoch(payload.get("fetched_utc")) or _num(found.get("last_success_at"))
    if fetched is None or platform not in ("polymarket", "kalshi"):
        return None
    result = payload.get("result").lower() if isinstance(payload.get("result"), str) else None
    result = result if result in ("yes", "no") and payload.get("closed") else None
    expires = None if result else ctx.stale(market_calc.KIND_PREDICTION, fetched)
    if expires is not None and expires < ctx.now:
        return None
    mid = _num(payload.get("mid"))
    if mid is None or not 0.0 < mid < 1.0:
        bid, ask = _num(payload.get("bid")), _num(payload.get("ask"))
        mid = round((bid + ask) / 2.0, 6) if bid is not None and ask is not None and 0.0 < bid <= ask < 1.0 else None
    entry = {
        "platform": platform,
        "chamber": chamber,
        "outcome": _clean(payload.get("outcome"), market_calc.MAX_NAME) or "",
        "mid": mid,
        "last": _num(payload.get("last")),
        "updated_utc": _epoch(payload.get("updated_utc")),
        "result": result,
    }
    return entry, expires


@_rows("election")
def _election(ctx: _Ctx, row_id: str) -> RowResult:
    today_et = datetime.fromtimestamp(ctx.now, _ET).date()
    quotes: list[dict] = []
    expiries: list[float] = []
    for chamber, obs_ids in OBS_ELECTION.items():
        for obs_id in obs_ids:
            got = _quote_entry(ctx, obs_id, chamber)
            if got is not None:
                quotes.append(got[0])
                if got[1] is not None:
                    expiries.append(got[1])
    if today_et > _ELECTION_LAST_DAY:
        state = "ended"
    elif today_et < _VOTE_DAY:
        state = "before"
    elif today_et == _VOTE_DAY:
        state = "voting"
    elif any(quote["result"] for quote in quotes):
        # 結果出來後顯示 7 天（§18.15）：第一次看到結果的時間記在 task_state
        seen = ctx.store.task_state(ELECTION_STATE_TASK).get("result_seen_at")
        if not isinstance(seen, (int, float)):
            seen = ctx.now
            ctx.store.set_task_state(ELECTION_STATE_TASK, {"result_seen_at": seen})
        state = "ended" if ctx.now > float(seen) + ELECTION_RESULT_DAYS * 86400 else "result"
    else:
        state = "waiting"
    days_left = (_VOTE_DAY - today_et).days if today_et <= _VOTE_DAY else None
    values = {"vote_date": _VOTE_DAY.isoformat(), "days_left": days_left, "state": state, "quotes": quotes}
    # 倒數天數到美東午夜就變：publish 停了的話列到那時過期；報價各自 6 小時
    return RowResult(values, data_date=today_et, stale_after=min([_next_midnight(ctx.now, _ET)] + expiries),
                     source="Polymarket、Kalshi", basis=market_calc.TEXT_ELECTION_NOTE)


# ── 新聞、川普 ──────────────────────────────────────────────────────────────


@_rows("news.")
def _news(ctx: _Ctx, row_id: str) -> RowResult:
    """新聞：只列 36 小時內的（publish 當下再篩一次）；過期＝最後一次抓到這個來源＋36 小時。"""
    key = row_id.rsplit(".", 1)[1]
    found = ctx.obs(OBS_NEWS.format(key))
    if found is None:
        raise _Unavailable("missing")
    payload = found["payload"]
    fetched = _epoch(payload.get("fetched_utc")) or _num(found.get("last_success_at"))
    if fetched is None:
        raise _Unavailable("missing")
    window = market_calc.NEWS_TTL.total_seconds()
    out = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        published = _epoch(item.get("published_utc"))
        title = _clean(item.get("title"), market_calc.MAX_TITLE)
        if published is None or title is None or not ctx.now - window <= published <= ctx.now + 300:
            continue
        out.append({"title": title, "source": _clean(item.get("source"), market_calc.MAX_NAME),
                    "published_utc": published})
    out.sort(key=lambda entry: -entry["published_utc"])
    return RowResult({"items": out[:NEWS_KEEP]}, data_date=None, stale_after=fetched + window,
                     source="Google News", basis="第三方標題，只照錄")


def _trump_feed(ctx: _Ctx) -> tuple[list[dict], float, Optional[dict], dict[str, str]]:
    """(近 48 小時、沒被移除的累積貼文, 最後一次抓到 feed 的時間, market_pipeline_misc 的摘要, 主題名)。

    主題則數用 publish 當下的 now 重算（視窗會滑動）；缺口（「至少」）沿用 collect 記的。"""

    def load() -> tuple[list[dict], float, Optional[dict], dict[str, str]]:
        window = market_calc.TRUMP_TTL.total_seconds()
        stored = list(ctx.store.posts(POSTS_TRUMP, since_utc=ctx.now - POSTS_LOOKBACK_S))
        summary = ctx.obs(OBS_TRUMP_SUMMARY)
        seen = [float(p["last_seen_at"]) for p in stored if isinstance(p.get("last_seen_at"), (int, float))]
        fetched = _num(summary.get("last_success_at")) if summary else None
        fetched = fetched if fetched is not None else (max(seen) if seen else None)
        if fetched is None:
            raise _Unavailable("missing")
        labels: dict[str, str] = {}
        try:
            labels.update(getattr(ctx.module("market_sources_misc"), "TRUMP_TOPIC_LABELS", {}) or {})
        except _Unavailable:
            pass
        if summary and isinstance(summary["payload"].get("labels"), dict):
            labels.update({k: v for k, v in summary["payload"]["labels"].items() if isinstance(v, str)})
        posts = [p for p in stored if not p.get("removed") and (_epoch(p.get("published_utc")) or 0.0) >= ctx.now - window]
        return posts, fetched, summary, labels

    return ctx.shared("trump", load)


def _topic_names(item: dict, labels: dict[str, str]) -> list[str]:
    names = []
    for topic in dict.fromkeys(item.get("topics") or []):
        label = _clean(labels.get(topic), 8) if isinstance(topic, str) else None
        if label:  # 片語表沒有的代號不列（主題名是程式的固定詞）
            names.append(label)
    return names


@_rows("trump.summary")
def _trump_summary(ctx: _Ctx, row_id: str) -> RowResult:
    posts, fetched, summary, labels = _trump_feed(ctx)
    counts: Counter = Counter()
    latest = None
    for post in posts:
        published = _epoch(post.get("published_utc"))
        latest = published if latest is None or (published or 0) > latest else latest
        counts.update(_topic_names(post, labels))
    window = market_calc.TRUMP_TTL.total_seconds()
    gap = bool(summary["payload"].get("at_least")) if summary else True
    # 有缺口、剛上線未滿 48 小時（collect 記的），或存檔太久沒抓到 → 計數標「（至少）」（§18.14）
    at_least = gap or ctx.now - fetched > FEED_LAG_AT_LEAST_S
    values = {"topics": dict(sorted(counts.items())), "latest_utc": latest, "at_least": at_least}
    return RowResult(values, data_date=None, stale_after=fetched + window, source="trumpstruth.org（第三方存檔）",
                     basis=market_calc.TEXT_TRUMP_NOTE)


@_rows("trump.posts")
def _trump_post_rows(ctx: _Ctx, row_id: str) -> RowResult:
    posts, fetched, _summary, labels = _trump_feed(ctx)
    out = []
    for post in posts:
        published = _epoch(post.get("published_utc"))
        excerpt = _clean(post.get("own_text"), market_calc.MAX_TRUMP_EXCERPT)
        topics = _topic_names(post, labels)
        if published is None or excerpt is None or not topics:
            continue  # 只列符合主題、有本人文字的（只有圖片／影片／轉貼的不列）
        out.append({"excerpt": excerpt, "published_utc": published,
                    "quoted_author": _clean(post.get("quoted_author"), market_calc.MAX_NAME), "topics": topics})
    out.sort(key=lambda entry: -entry["published_utc"])
    return RowResult({"items": out[:TRUMP_KEEP]}, data_date=None,
                     stale_after=fetched + market_calc.TRUMP_TTL.total_seconds(),
                     source="trumpstruth.org（第三方存檔）", basis=market_calc.TEXT_TRUMP_NOTE)


# ── 一句話與矛盾（衍生列：資料日相同才比；stale_after＝輸入的最小值） ─────────────────

_DERIVED_INPUTS = {"us": ("VOO.", "QQQ.", "SOXX.", "SPY.", "VIX"), "tw": ("TAIEX.", "0050.", "tw.")}


def _derived_inputs(ctx: _Ctx, market: str) -> tuple[list[str], Optional[date]]:
    keys = [key for key, fact in ctx.facts.items()
            if fact.ok and fact.value is not None and key.startswith(_DERIVED_INPUTS[market])]
    if not keys:
        raise _Unavailable("missing")
    return keys, market_calc.common_date(ctx.facts[key].data_date for key in keys)


@_rows("oneliner.")
def _oneliner(ctx: _Ctx, row_id: str) -> RowResult:
    market = row_id.rsplit(".", 1)[1]
    keys, day = _derived_inputs(ctx, market)
    text = market_calc.one_liner(ctx.facts, market) or None
    return RowResult({"text": text}, data_date=day, stale_after=min(ctx.fact_stale[key] for key in keys),
                     source="程式", basis="只用同一資料日、狀態正常的面向")


@_rows("contra.")
def _contra(ctx: _Ctx, row_id: str) -> RowResult:
    market = row_id.rsplit(".", 1)[1]
    keys, day = _derived_inputs(ctx, market)
    values = market_calc.contra_values(market_calc.contradictions(ctx.facts, market))
    return RowResult(values, data_date=day, stale_after=min(ctx.fact_stale[key] for key in keys), source="程式",
                     basis="7 條規則，只比同一資料日")


# ── 進度 ────────────────────────────────────────────────────────────────────


def _build_progress(ctx: _Ctx) -> None:
    """us_members＝寬度成分聯集裡「已經有歷史收盤」的檔數／總數（bot 卡上寫「歷史資料補到 X/Y 檔」，只在
    bootstrap 沒補完時出現）；每天抓到哪裡看 us_last_complete_day（覆蓋率 ≥80% 的最新那天）。"""
    try:
        closes = _us_member_closes(ctx)
        members = sorted(closes)
        ctx.progress["us_members"] = [sum(1 for symbol in members if closes[symbol]), len(members)]
        complete = _complete_day(closes, members, ctx.trading(US), ctx.us_day())
        ctx.progress["us_last_complete_day"] = complete.isoformat() if complete else None
    except _Unavailable:
        pass
    except Exception as exc:
        ctx.shared_errors["progress"] = f"unexpected:{type(exc).__name__}"


# ── 快照 schema 與編碼 ───────────────────────────────────────────────────────


def encode_snapshot(snapshot: dict) -> bytes:
    """UTF-8 JSON（中文不跳脫、不允許 NaN）；bot 用同一個 json 模組讀回。"""
    return json.dumps(snapshot, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_snapshot(snapshot: Any) -> list[str]:
    """寫快照前的自我檢查（INTERFACES §5＋market_calc.row_ok）；回問題清單（空＝合格）。"""
    problems: list[str] = []
    if not isinstance(snapshot, dict):
        return ["snapshot_type"]
    if snapshot.get("schema") != SCHEMA_VERSION:
        problems.append("schema")
    if not _is_number(snapshot.get("generated_at")):
        problems.append("generated_at")
    rows = snapshot.get("rows")
    if not isinstance(rows, dict):
        problems.append("rows")
    else:
        if set(rows) != set(row_ids()):
            problems.append("row_ids")
        for row_id, row in rows.items():
            if not market_calc.row_ok(row_id, row):
                problems.append(f"row:{row_id}")
    charts = snapshot.get("charts")
    if not isinstance(charts, dict):
        problems.append("charts")
    else:
        for key, entry in charts.items():
            if key not in CHART_KEYS or not isinstance(entry, dict):
                problems.append(f"chart:{key}")
                continue
            files = entry.get("files")
            if not isinstance(files, list) or not 1 <= len(files) <= 5 or len(set(files)) != len(files) or not all(
                isinstance(name, str) and _CHART_NAME_RE.fullmatch(name) for name in files
            ):
                problems.append(f"chart_files:{key}")
            inputs_date = entry.get("inputs_date")
            if inputs_date is not None and not (isinstance(inputs_date, str) and _DATE_RE.fullmatch(inputs_date)):
                problems.append(f"chart_date:{key}")
            if not _is_number(entry.get("stale_after")):
                problems.append(f"chart_stale_after:{key}")
    progress = snapshot.get("progress")
    if not isinstance(progress, dict):
        problems.append("progress")
    else:
        members = progress.get("us_members")
        if members is not None and not (
            isinstance(members, list) and len(members) == 2
            and all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in members)
        ):
            problems.append("progress_us_members")
        for key in ("us_last_complete_day", "tw_last_complete_day"):
            value = progress.get(key)
            if value is not None and not (isinstance(value, str) and _DATE_RE.fullmatch(value)):
                problems.append(f"progress_{key}")
    try:
        encode_snapshot(snapshot)
    except (TypeError, ValueError):
        problems.append("encode")
    return problems


# ── 圖 ──────────────────────────────────────────────────────────────────────


def _default_charts() -> Any:
    import market_charts  # noqa: PLC0415 - matplotlib 很重，真的要畫才載入

    return market_charts


def _own_regular(path: Path) -> Optional[os.stat_result]:
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() else None


def _chart_error(exc: BaseException) -> str:
    """market_charts.ChartError 的 kind（data／dir／too_big）照記；其他只記型別名。"""
    kind = getattr(exc, "kind", None)
    if type(exc).__name__ == "ChartError" and isinstance(kind, str) and re.fullmatch(r"[a-z_]{1,24}", kind):
        return f"chart:{kind}"
    if isinstance(exc, _BadValue):
        return f"bad:{exc.kind}"
    return f"unexpected:{type(exc).__name__}"


def _publish_charts(ctx: _Ctx, charts: Any) -> tuple[dict, dict]:
    """依 ctx.chart_jobs 畫圖（或沿用輸入沒變、檔案還在的舊圖）。回 (快照 charts、outcome)。

    圖目錄不合格 → 不畫、快照不帶圖；畫壞的那個圖鍵不帶圖（不拿比卡片舊的圖充數）。"""
    outcome: dict[str, Any] = {"dir": "ok", "rendered": 0, "reused": 0, "failed": 0, "deleted": 0}
    entries: dict[str, dict] = {}
    if not ctx.chart_jobs:
        outcome["dir"] = "unused"
        return entries, outcome
    chart_dir = Path(ctx.paths.chart_dir)
    if not market_paths.private_dir_ok(chart_dir):  # 不存在就用 0700 建；別人的、權限太寬的不碰
        outcome["dir"] = "chart_dir"
        return entries, outcome
    try:
        registered = ctx.store.chart_files() or {}
    except Exception as exc:
        outcome["state_error"] = f"unexpected:{type(exc).__name__}"
        registered = {}
    by_hash: dict[str, list[tuple[float, str]]] = {}
    for name, info in registered.items():
        if not isinstance(info, dict) or not isinstance(name, str) or not _CHART_NAME_RE.fullmatch(name):
            continue
        digest = info.get("inputs_hash")
        if isinstance(digest, str):
            by_hash.setdefault(digest, []).append((float(info.get("created_at") or 0.0), name))
    charts_mod = charts
    errors: dict[str, str] = {}
    for key in CHART_KEYS:
        job = ctx.chart_jobs.get(key)
        if job is None:
            continue
        fresh: list[str] = []
        try:
            files: list[str] = []
            for (func_name, kwargs), digest in zip(job.parts, job.part_hashes()):
                old = next((name for _, name in sorted(by_hash.get(digest, []), reverse=True)
                            if _own_regular(chart_dir / name)), None)
                if old is not None:
                    files.append(old)
                    continue
                if charts_mod is None:
                    charts_mod = _default_charts()
                name = _render_one(ctx, chart_dir, getattr(charts_mod, func_name), kwargs, digest)
                fresh.append(name)
                files.append(name)
            outcome["rendered" if fresh else "reused"] += 1
            entries[key] = {
                "files": files,
                "inputs_date": job.inputs_date.isoformat() if job.inputs_date else None,
                "stale_after": float(job.stale_after),
            }
        except Exception as exc:  # 一張圖壞掉不影響其他圖與快照（PLAN §19.4）
            _discard(ctx, chart_dir, fresh)  # 不發布半組圖：這一組剛畫好的也刪掉
            outcome["failed"] += 1
            errors[key] = _chart_error(exc)
    if errors:
        outcome["errors"] = errors
    return entries, outcome


def _render_one(ctx: _Ctx, chart_dir: Path, render: Callable, kwargs: dict, digest: str) -> str:
    """畫一張：先登記檔名再畫（被 kill 時只會留下「登記了、沒有檔」的那筆，清理時 forget）。

    market_charts 自己寫同目錄暫存檔＋fsync＋rename；這裡再驗一次是自己的一般檔、PNG、<1 MB。"""
    name = f"{uuid.uuid4().hex}.png"
    ctx.store.register_chart(name, digest, ctx.now)
    try:
        render(chart_dir / name, **kwargs)
        _check_png(chart_dir / name)
    except BaseException:
        _discard(ctx, chart_dir, [name])
        raise
    return name


def _discard(ctx: _Ctx, chart_dir: Path, names: Iterable[str]) -> None:
    for name in names:
        if _own_regular(chart_dir / name):
            try:
                os.unlink(chart_dir / name)
            except OSError:
                pass
        try:
            ctx.store.forget_chart(name)
        except Exception:
            pass


def _check_png(path: Path) -> None:
    """畫出來的檔要是自己的一般檔、PNG、<1 MB；順便 fsync（快照引用它之前內容要落地）。"""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise _BadValue("chart_not_regular")
        if not 0 < info.st_size < CHART_MAX_BYTES:
            raise _BadValue("chart_size")
        if os.pread(fd, len(_PNG_MAGIC), 0) != _PNG_MAGIC:
            raise _BadValue("chart_not_png")
        try:
            os.fsync(fd)
        except OSError:
            pass  # market_charts 寫檔時已經 fsync 過；這裡只是保險
    finally:
        os.close(fd)


def _after_publish_charts(ctx: _Ctx, entries: dict, outcome: dict) -> None:
    """快照已經寫好：退役沒被引用的圖、刪退役超過 72 小時的（只刪自己登記、lstat 是自己的一般檔），
    再清掉被 kill 留下、超過 1 小時的暫存檔。圖目錄不合格時一律不碰裡面的檔。"""
    try:
        keep = {name for entry in entries.values() for name in entry.get("files", [])}
        ctx.store.retire_charts(keep, ctx.now)  # 又被引用的舊圖會取消退役
    except Exception as exc:
        outcome["retire_error"] = f"unexpected:{type(exc).__name__}"
        return
    chart_dir = Path(ctx.paths.chart_dir)
    if outcome.get("dir") == "chart_dir" or not market_paths.private_dir_ok(chart_dir, create=False):
        return
    try:
        names = list(ctx.store.charts_to_delete(ctx.now, CHART_DELETE_AFTER_S))
    except Exception as exc:
        outcome["cleanup_error"] = f"unexpected:{type(exc).__name__}"
        names = []
    skipped = 0
    for name in names:
        try:
            if not isinstance(name, str) or not _CHART_NAME_RE.fullmatch(name):
                skipped += 1
                continue
            target = chart_dir / name
            try:
                info = os.lstat(target)
            except FileNotFoundError:
                ctx.store.forget_chart(name)  # /tmp 被清過：紀錄也不用留
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                skipped += 1  # 不是自己的一般檔（symlink、目錄、別人的）：不刪，紀錄作廢
                ctx.store.forget_chart(name)
                continue
            os.unlink(target)
            ctx.store.forget_chart(name)
            outcome["deleted"] += 1
        except Exception as exc:
            outcome["cleanup_error"] = f"unexpected:{type(exc).__name__}"
    if skipped:
        outcome["skipped"] = skipped
    _sweep_tmp(ctx, chart_dir, outcome)


def _sweep_tmp(ctx: _Ctx, chart_dir: Path, outcome: dict) -> None:
    """market_charts 寫到一半被 kill 留下的 `.market-*.tmp`：自己的一般檔、超過 1 小時才刪。"""
    try:
        names = os.listdir(chart_dir)
    except OSError:
        return
    swept = 0
    for name in names:
        if not _TMP_NAME_RE.fullmatch(name):
            continue
        info = _own_regular(chart_dir / name)
        if info is None or ctx.now - info.st_mtime < TMP_SWEEP_AFTER_S:
            continue
        try:
            os.unlink(chart_dir / name)
            swept += 1
        except OSError:
            pass
    if swept:
        outcome["tmp_swept"] = swept
