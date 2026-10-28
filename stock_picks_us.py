"""美股單檔查詢＋美股推薦（/股票 第 3 段，2026-10-28）。

Andrew 10/28：「line bot股票查詢部分，新增美股」；範圍＝查詢＋推薦＋推薦成績（成績的計算與卡片在 stock_picks_perf）。
條件只有四條（程式算，不經模型、不搜尋；都用卡片上顯示的精度判斷）：
- 長期向上：收盤 ≥ 年線（240 個交易日均價；兩邊都先四捨五入到 0.01 美元再比）。
- 靠近季線：收盤離季線（60 個交易日均價）±5.0% 以內。
- 有在成長：最近一季營收比去年同季高（一位小數 > 0.0%）。
- 有在賺錢：近四季每季淨利都 > 0。
本益比只列、不算條件；ETF 只看走勢兩條。燈號只描述狀態，不寫買、賣、等、停損。

資料（全部是公開市場資料）：
- 推薦池：盤勢 job 的 DB（state/market_history.sqlite，唯讀，只經 stock_db_ro）：SSGA 的 SPY 持股依權重前 50 大
  （同公司不同股別只留權重大的）、Nasdaq 正式收盤；財報是 Yahoo fundamentals-timeseries（背景每檔 3 天抓一次，
  每輪最多 25 次、每天最多 40 次，送出前先記帳）。
- 單檔查詢：池裡、推薦池快照有更新、而且不是 ⚪ → 直接用快照；其他即時抓 Yahoo（日線＋財報，不開執行緒、依序、
  只打 query1，總預算 min(6 秒, 剩下時間 − 3 秒)），收盤和快照帶的 Nasdaq 最新收盤核對（回覆路徑不碰 sqlite）。

背景執行緒 stock-picks-us 每 30 分鐘一輪：讀 D → 需要時重算美股推薦 → 補寫推薦紀錄 → 更新推薦成績。
美股開關（LINE_BOT_STOCK_PICKS_US，模組載入時讀，改了要重啟）關閉時只做推薦紀錄與推薦成績（只讀本機），
不讀推薦池、不打 Yahoo；回覆退回舊句。

規格：ops/state/line_bot_stock_us_20261028/PLAN.md（v5）與 IMPL_SPEC.md。不 import main、fcn_eval、market_*；
stock_picks 的東西一律在呼叫時用 stock_picks.X（conftest 的擋網、tmp state、關背景才會生效）；
stock_picks_perf 只在函式裡 import（成績模組壞了，美股照樣能用）。卡片上只有固定句型、程式算的數字、
代號與交易所登記的公司名，沒有使用者原文。
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Mapping, Optional, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

import stock_card_check
import stock_db_ro
import stock_picks

logger = logging.getLogger("stock_picks_us")
_NY = ZoneInfo("America/New_York")
_TPE = ZoneInfo("Asia/Taipei")

# ── 常數（門檻都是起始值；改了 RULE 要跟著升，同一個 D 才會用新規則重算） ─────────────────
RULE = "us_v1"
FLAG_ENV = "LINE_BOT_STOCK_PICKS_US"
ENABLED = stock_picks._ai_flag(os.environ.get(FLAG_ENV))  # 載入模組時讀（同 AI_ENABLED）；reset_for_tests 重讀
SNAPSHOT_NAME = "stock_picks_us.json"
WORKER_NAME = "stock_picks_us_worker.json"
HISTORY_NAME = "stock_picks_us_history.jsonl"  # ＝stock_picks_perf.US_HISTORY_NAME（測試釘住兩邊一樣）
SNAPSHOT_SCHEMA = 1
WORKER_SCHEMA = 1
POOL_N, MAX_PICKS, MIN_SCORE = 50, 3, 4
TOTAL_CONDITIONS = 4
STOCK, ETF = "stock", "etf"  # 和 stock_picks.STOCK／ETF 同字

# 推薦池（obs us.holdings.SPY：SSGA 每天的 SPY 持股，rows＝[代號, 名稱, 權重%, 市值]）
HOLDINGS_OBS = "us.holdings.SPY"
POOL_MIN_ROWS = 400
POOL_WEIGHT_SUM = (90.0, 101.0)
POOL_AS_OF_DAYS = (-10, 3)  # 持股日期要在 D−10 天到 D+3 天
POOL_MAX_AGE_DAYS = 45  # 新的持股不合格時，沿用上一份池的期限
SPY = "SPY"

# 資料日 D 與到齊（PLAN §2.2）
CALENDAR_LOOKBACK_DAYS = 420  # 日曆與收盤都從 D−420 天起查（240 個交易日約 350 天）
COVER_DAYS = 5  # 只看日曆最後 5 天的到齊數
STRAGGLER_WAIT_S = 10 * 3600  # 最新一天看到 10 小時後，缺 ≤ 2 檔也照算（停牌、下市）
STRAGGLER_MAX_MISSING = 2
SPY_DATES_KEEP = 260
MAX_GRAY_RATIO = 0.2  # ⚪＋財報資料不足超過兩成 → 整輪失敗（保留舊快照）

# 沒更新（PLAN §4）
BEHIND_GRACE_S = 12 * 3600
STALE_DAYS = 7
CHECK_STALE_S = 36 * 3600

# 背景 worker
POLL_S = 30 * 60
STARTUP_DELAY_S = 150.0
REFRESH_BUDGET_S = 150.0  # 一次重算最多花的時間（含財報）
NOT_YET_RETRY_S = 15 * 60  # 還沒好（DB 忙碌、財報還在等、池還沒到齊）：不算失敗
DB_RETRY_S = 20 * 60  # DB 讀不到：算失敗，20 分鐘
STUCK_FAIL_COUNT = 3  # 沒有快照又連續失敗這麼多次：卡片改寫「暫時算不出來」

# 財報（Yahoo fundamentals-timeseries；PLAN §2.3）
FUND_REFRESH_S = 3 * 86400  # 每檔固定 3 天抓一次
FUND_PER_ROUND = 25
FUND_PER_DAY = 40  # 台北日期
FUND_CACHE_MAX = 150
FUND_LOOKBACK_DAYS = 800
FUND_TIMEOUT_S = 10.0
FUND_SPACING_S = 1.0  # 背景依序抓，兩次之間停 1 秒（staging 也是這個間隔）
FUND_MAX_BYTES = 256 * 1024
FUND_MAX_ROWS = 12
FUND_VALUE_MAX = 1e15
FUND_MAX_AGE_DAYS = 200  # 最近一季離資料日超過 200 天 → 資料不足
YOY_LAG_DAYS = (350, 380)  # 去年同季：往前 350–380 天
QUARTER_GAP_DAYS = (70, 110)  # 相鄰兩季
SHARES_MAX_RATIO = 1.3  # 淨利 ÷ EPS 的最大／最小（擋沒還原的分割）
SHARES_MIN_QUARTERS = 3
PE_MAX = 1000.0
_FUND_TYPES = (("rev", "quarterlyTotalRevenue"), ("ni", "quarterlyNetIncome"), ("eps", "quarterlyDilutedEPS"))
_YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")  # 和 FCN、台股單檔同樣的主機
_YAHOO_MAX_BYTES = 256 * 1024

# 單檔即時查（PLAN §5.2）
LOOKUP_TOTAL_S = 6.0
LOOKUP_RESERVE_S = 3.0  # 留給回覆本身
LOOKUP_FETCH_S = 3.0  # 單次請求最多
LOOKUP_FUND_MIN_S = 1.5  # 抓完日線還剩這麼多才抓財報
LOOKUP_MIN_REMAINING_S = 5.0
LOOKUP_LIMIT = 4
LOOKUP_WINDOW_S = 600.0
LOOKUP_FAIL_TTL_S = 600.0
LOOKUP_CACHE_MAX = 128
LOOKUP_BLOCK_S = 20 * 60  # 單檔自己的 429／403 冷卻（不看、也不設計畫 C 的共用冷卻）
LOOKUP_CLOSE_HOUR = 17  # 美東 17:00 前，今天那根還沒收完
LOOKUP_STALE_DAYS = 5
JUMP_RATIO = 1.45  # 只用在 Yahoo 路徑（DB 的列已經過盤勢 job 的 15% 兩次確認＋quarantine）
VERIFY_TOL = 0.01  # Yahoo 收盤和 Nasdaq 同一天的收盤差 > 1% → ⚪
_EXCHANGES = frozenset({"NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS"})
_INSTRUMENT_KIND = {"EQUITY": STOCK, "ETF": ETF}

# 卡片
MAX_CARD_BYTES = 25 * 1024  # 加上美股後整組（大字版按鈕每張約 402 B，card_pages 上限 30 KB）
MAX_BUBBLES = 12  # LINE carousel 上限
_MAX_PRICE = 1e6

# 條件（順序就是卡片上的順序）
LONG, MID, GROWTH, PROFIT = "long", "mid", "growth", "profit"
US_CONDITIONS = (LONG, MID, GROWTH, PROFIT)
US_CONDITION_NAME = {LONG: "長期向上", MID: "靠近季線", GROWTH: "有在成長", PROFIT: "有在賺錢"}
PE_OK, PE_OVER, PE_LOSS, PE_NOT_USD, PE_NONE = "ok", "over", "loss", "not_usd", "none"
_PE_KINDS = (PE_OK, PE_OVER, PE_LOSS, PE_NOT_USD, PE_NONE)
FUND_OK, FUND_UNAVAILABLE, FUND_NA = "ok", "unavailable", "na"  # na：ETF 不看財報
_FUND_STATES = (FUND_OK, FUND_UNAVAILABLE, FUND_NA)

# ── 固定句（hook 也會用到 TEXT_RESERVED／TEXT_US_NOT_FOUND） ────────────────────────────
TEXT_RESERVED = "股票評估：美股請打代號，例如 /股票 NVDA；看推薦請打 /股票"
TEXT_US_NOT_FOUND = "股票評估：認不出這個美股代號，請確認代號，例如 /股票 NVDA"
TEXT_PHASE3_OFF = "股票推薦：美股推薦和推薦成績暫時關閉"
TEXT_US_UNSUPPORTED = "股票評估：美股目前只看普通股和 ETF"
TEXT_US_EXCHANGE = "股票評估：美股目前只看在美國交易所上市的普通股和 ETF"
TEXT_US_NO_TRADE = "股票評估：這一檔最近沒有成交"
TEXT_US_COMPUTING = "美股推薦計算中，請稍後再按一次"
TEXT_US_STUCK = "美股推薦暫時算不出來，請稍後再按一次"
TEXT_US_NOT_UPDATED = "美股資料暫時沒有更新，先不推薦"
TEXT_US_POOL_LEAD = "美股從標普 500 市值前 50 大挑，4 條都符合才推，最多 3 檔"
TEXT_US_POOL_NOTE = "美股推薦只從標普 500 市值前 50 大挑"
TEXT_US_DETAIL_POOL_NOTE = "美股推薦卡只從標普 500 市值前 50 大挑"
TEXT_ETF_NA = "營收、獲利：ETF 只看走勢，不適用"
TEXT_FUND_UNAVAILABLE = "財報暫時抓不到"
_GRAY = "#555555"

# ── 代號（PLAN §5.1；stock_picks.resolve_query／_us_command／_us_only_symbol 會呼叫） ──────────
RESERVED = frozenset({"AI", "ETF", "US", "USA", "OK", "HELP", "BUY", "SELL"})  # HI、TW 是真的代號，不列
REJECTED = "<rejected>"  # 像美股但不收（S&P、J&J…）：不會符合代號格式，誤傳進 handle_symbol 也只會回認不出
_TOKEN_RE = re.compile(r"([A-Za-z]{1,5})(?:[./\-]([A-Za-z]))?")  # 只收 ASCII 字母（不用 IGNORECASE：K、ſ 會混進來）
_AMP_RE = re.compile(r"[A-Za-z]+(?:[&+][A-Za-z]+)+")  # 字母之間有 & 或 +：S&P、AT&T、AT+T
_AMP_SPACES_RE = re.compile(r"\s*([&+])\s*")  # 「S & P」先併成「S&P」，才不會變成 S（SentinelOne）
_SYMBOL_RE = re.compile(r"[A-Z]{1,5}(?:\.[A-Z])?")  # 正規寫法（DB、顯示）：BRK.B；Yahoo 用 BRK-B
_TAIL_PUNCT = tuple("?？!！。.,，~～(（")
# 美股只用空白和中文標點切段（&、+、/、.、- 不切：S&P、BRK/B、BRK.B）。NFKC 會把全形標點變成半形
# （「，」→「,」、「？」→「?」、「｜」→「|」），所以半形的那幾個也算。
_SPLIT_CHARS = frozenset("、。「」『』【】《》〈〉…‥・·—|,;:!?()~")
_MAX_QUERY_CHARS = 200


def _clean_query(text) -> str:
    """刪 Cf（零寬空白等）→ NFKC（全形變半形）→ 再刪一次；最多 200 字。"""
    raw = "".join(ch for ch in str(text or "")[: _MAX_QUERY_CHARS * 4] if unicodedata.category(ch) != "Cf")
    normalized = unicodedata.normalize("NFKC", raw)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Cf")[:_MAX_QUERY_CHARS].strip()


def _question_tails() -> tuple[str, ...]:
    """代號後面可以接的問句字：台股名稱用的那一組裡非 ASCII 的（「NVDA嗎」「NVDA現在」）；數字不收
    （「A1」「NVDA2330」不會變成 A、NVDA）。"""
    return tuple(t for t in stock_picks._NAME_TAILS if not t.isascii())


def parse_us_token(token: str):
    """一段字 → 正規代號（"NVDA"、"BRK.B"）、REJECTED（S&P 這類）或 None（不像美股代號）。
    先確定是 ASCII 字母才 upper（「ß」upper 會變「SS」）。"""
    seg = _clean_query(token)
    if not seg:
        return None
    if _AMP_RE.match(seg):
        return REJECTED
    match = _TOKEN_RE.match(seg)
    if match is None:
        return None
    rest = seg[match.end():]
    if rest and not (rest.startswith(_TAIL_PUNCT) or rest.startswith(_question_tails())):
        return None
    base, share_class = match.group(1).upper(), match.group(2)
    return f"{base}.{share_class.upper()}" if share_class else base


def find_us_symbol(text: str):
    """整段字裡第一個認得的美股代號（或 REJECTED）；沒有回 None。保留字（AI 等）照樣回代號，由呼叫端決定。"""
    cleaned = _AMP_SPACES_RE.sub(r"\1", _clean_query(text))
    for seg in "".join(" " if ch in _SPLIT_CHARS else ch for ch in cleaned).split():
        found = parse_us_token(seg)
        if found is not None:
            return found
    return None


def _yahoo_symbol(symbol: str) -> str:
    return symbol.replace(".", "-")


# ── 數字、日期、時間 ───────────────────────────────────────────────────────────────
def _wall() -> float:
    """真實時間（測試換掉這個就能固定「現在」）。"""
    return time.time()


def _cents(value: float) -> int:
    return stock_picks._round_half_up(value * 100)


def _fmt_usd(value: float) -> str:
    """美元兩位小數、千分位：1281.905 → 「1,281.91」（判斷與顯示用同一個四捨五入）。"""
    cents = _cents(value)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100:,}.{cents % 100:02d}"


def _us_today(now: float) -> date:
    return datetime.fromtimestamp(now, _NY).date()


def _tw_day(now: float) -> str:
    return datetime.fromtimestamp(now, _TPE).date().isoformat()


def _iso_ok(value) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _price_ok(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and 0 < value < _MAX_PRICE
    except OverflowError:
        return False


def _next_us_close(now: float) -> float:
    """下一個美東 17:00（真實時間）：單檔成功結果快取到這時（之後就有新的一根）。"""
    local = datetime.fromtimestamp(now, _NY)
    day = local.date() if local.hour < LOOKUP_CLOSE_HOUR else local.date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, LOOKUP_CLOSE_HOUR, tzinfo=_NY).timestamp()


# ── 走勢 ───────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class UsTrend(stock_picks.Trend):
    """台股的 Trend，只有「長期向上」改用美元的顯示精度（0.01 美元）比。"""

    @property
    def above_year(self) -> bool:
        return _cents(self.close) >= _cents(self.ma240)


def us_trend_of(year: Sequence[float], quarter: Sequence[float]) -> Optional[UsTrend]:
    """沿用台股的 trend_of（年線窗 ≥ 235、季線窗 ≥ 58、最後一根相同），再擋超出範圍的價格。"""
    trend = stock_picks.trend_of(year, quarter)
    if trend is None or not all(_price_ok(v) for v in (trend.close, trend.ma60, trend.ma240)):
        return None
    return UsTrend(close=trend.close, ma60=trend.ma60, ma240=trend.ma240, p60=trend.p60)


def _jumpy(values: Sequence[float]) -> bool:
    """相鄰兩根比值超出 1/1.45–1.45（Yahoo 沒有別的確認：沒還原的分割會長這樣）。"""
    return any(not (1 / JUMP_RATIO <= b / a <= JUMP_RATIO) for a, b in zip(values, values[1:]))


# ── 財報（每個序列各自驗；PLAN §3） ──────────────────────────────────────────────────
FundRow = tuple  # (asOfDate: date, raw: float, currencyCode: str)


def _fund_row(row) -> Optional[FundRow]:
    """一列要有 asOfDate、periodType == "3M"、有限數 reportedValue.raw（字串數字不收）、currencyCode。"""
    if not isinstance(row, dict) or row.get("periodType") != "3M":
        return None
    when, cur, reported = row.get("asOfDate"), row.get("currencyCode"), row.get("reportedValue")
    if not _iso_ok(when):
        return None
    if not (isinstance(cur, str) and 1 <= len(cur) <= 5 and cur.isascii() and cur.isalpha() and cur.isupper()):
        return None
    raw = reported.get("raw") if isinstance(reported, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        number = float(raw)
    except OverflowError:
        return None
    if not math.isfinite(number) or abs(number) >= FUND_VALUE_MAX:
        return None
    return (date.fromisoformat(when), number, cur)


def parse_fundamentals(payload) -> Optional[dict]:
    """Yahoo fundamentals-timeseries → {"rev": 列, "ni": 列, "eps": 列}（舊到新、同日期取最後一筆、最多 12 列）；
    形狀不對回 None（當作沒抓到）。"""
    try:
        results = payload["timeseries"]["result"]
    except (KeyError, TypeError):
        return None
    if not isinstance(results, list):
        return None
    out: dict[str, dict] = {key: {} for key, _ in _FUND_TYPES}
    for item in results:
        if not isinstance(item, dict):
            continue
        for key, type_name in _FUND_TYPES:
            rows = item.get(type_name)
            if not isinstance(rows, list):
                continue
            for row in rows:
                parsed = _fund_row(row)
                if parsed is not None:
                    out[key][parsed[0]] = parsed
    return {key: tuple(sorted(rows.values()))[-FUND_MAX_ROWS:] for key, rows in out.items()}


@dataclass(frozen=True)
class Growth:
    q: date  # 最近一季（asOfDate）
    yoy: int  # 年增，千分位（52 ＝ 5.2%）


@dataclass(frozen=True)
class Profit:
    q: date
    losses: int  # 近四季淨利 < 0 的季數
    zeros: int  # 剛好是 0 的季數


@dataclass(frozen=True)
class Pe:
    kind: str
    tenths: Optional[int] = None  # 只有 PE_OK 有：本益比 ×10（273 ＝ 27.3 倍）


def _recent(day: date, data_date: date) -> bool:
    return day <= data_date and (data_date - day).days <= FUND_MAX_AGE_DAYS


def growth_of(rev: Sequence[FundRow], data_date: date) -> Optional[Growth]:
    """最新一季營收 L（> 0）和往前 350–380 天那一季 P（> 0、同幣別）→ 年增；L 太舊、P 找不到或不只一筆 → None。"""
    if not rev:
        return None
    q, latest, cur = rev[-1]
    if latest <= 0 or not _recent(q, data_date):
        return None
    prior = [r for r in rev if YOY_LAG_DAYS[0] <= (q - r[0]).days <= YOY_LAG_DAYS[1]]
    if len(prior) != 1 or prior[0][1] <= 0 or prior[0][2] != cur:
        return None
    return Growth(q=q, yoy=stock_picks._permille(latest / prior[0][1] - 1.0))


def _last_quarters(series: Sequence[FundRow], data_date: date, n: int = 4) -> Optional[tuple]:
    """最新 n 季：相鄰差 70–110 天、最新一季離資料日 ≤ 200 天、幣別相同；不符合回 None。"""
    if len(series) < n:
        return None
    rows = tuple(series[-n:])
    if not _recent(rows[-1][0], data_date):
        return None
    if any(not QUARTER_GAP_DAYS[0] <= (b[0] - a[0]).days <= QUARTER_GAP_DAYS[1] for a, b in zip(rows, rows[1:])):
        return None
    if len({r[2] for r in rows}) != 1:
        return None
    return rows


def profit_of(ni: Sequence[FundRow], data_date: date) -> Optional[Profit]:
    rows = _last_quarters(ni, data_date)
    if rows is None:
        return None
    return Profit(q=rows[-1][0], losses=sum(1 for r in rows if r[1] < 0), zeros=sum(1 for r in rows if r[1] == 0))


def pe_of(eps: Sequence[FundRow], ni: Sequence[FundRow], close: float, data_date: date) -> Pe:
    """本益比（只列）：EPS 要是美元（Yahoo 的 ADR EPS 是美元就是每一 ADS；TSM 的是台幣、每股普通股 → 不適用）；
    股數檢查：淨利 > 0 且 EPS > 0 的季算「淨利 ÷ EPS」，至少 3 季、最大／最小 ≤ 1.3（擋沒還原的分割）。"""
    if not eps:
        return Pe(PE_NONE)
    if eps[-1][2] != "USD":
        return Pe(PE_NOT_USD)
    rows = _last_quarters(eps, data_date)
    if rows is None:
        return Pe(PE_NONE)
    income = {d: (v, c) for d, v, c in ni}
    shares = [income[d][0] / e for d, e, _ in rows if d in income and income[d][1] == "USD" and income[d][0] > 0 and e > 0]
    if len(shares) < SHARES_MIN_QUARTERS or max(shares) / min(shares) > SHARES_MAX_RATIO:
        return Pe(PE_NONE)
    total = math.fsum(e for _, e, _ in rows)
    if total <= 0:
        return Pe(PE_LOSS)
    pe = close / total
    if not math.isfinite(pe) or pe <= 0:
        return Pe(PE_NONE)
    if pe > PE_MAX:
        return Pe(PE_OVER)
    return Pe(PE_OK, stock_picks._tenths(pe))


@dataclass(frozen=True)
class FundEntry:
    """一檔的財報（背景快取或單檔查詢自己的快取；整個換掉、不改內容）。"""

    at: float  # 抓到的時間（真實時間）
    rev: tuple = ()
    ni: tuple = ()
    eps: tuple = ()

    @property
    def latest_q(self) -> Optional[date]:
        days = [rows[-1][0] for rows in (self.rev, self.ni, self.eps) if rows]
        return max(days) if days else None


def _entry_from_parsed(parsed: dict, at: float) -> FundEntry:
    return FundEntry(at=at, rev=parsed["rev"], ni=parsed["ni"], eps=parsed["eps"])


# ── 一檔的數字、條件、燈號 ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class UsFacts:
    """一檔算燈號需要的全部數字（快照裡存的就是這些）。trend 是 None ＝ ⚪（gray 記原因）。"""

    symbol: str  # 正規寫法 BRK.B
    name: str  # 卡片上的公司名（清理過；空字串＝只顯示代號）
    kind: str  # STOCK／ETF
    data_date: date  # 美東日期
    trend: Optional[UsTrend]
    growth: Optional[Growth] = None
    profit: Optional[Profit] = None
    pe: Pe = Pe(PE_NONE)
    fund: str = FUND_OK
    weight: Optional[float] = None  # 只有推薦池有（SPY 權重 %）
    gray: str = ""  # ⚪ 的原因：missing／quarantine／short／jump／mismatch／error
    source: str = "db"  # db（Nasdaq 收盤）／yahoo
    verified: bool = False  # Yahoo 收盤和 Nasdaq 同一天的收盤核對過


@dataclass(frozen=True)
class UsCheck:
    key: str
    state: Optional[bool]  # True 成立、False 不成立、None 資料不足
    text: str


def _growth_check(growth: Optional[Growth]) -> UsCheck:
    if growth is None:
        return UsCheck(GROWTH, None, "營收資料不足")
    head = f"最近一季（到 {stock_picks._fmt_date(growth.q)}）營收"
    if growth.yoy == 0:
        return UsCheck(GROWTH, False, f"{head}和去年同季一樣（0.0%）")
    word = "多" if growth.yoy > 0 else "少"
    return UsCheck(GROWTH, growth.yoy > 0, f"{head}比去年同季{word} {stock_picks._fmt_tenths(growth.yoy)}%")


def _profit_check(profit: Optional[Profit]) -> UsCheck:
    if profit is None:
        return UsCheck(PROFIT, None, "獲利資料不足")
    if profit.losses == 0 and profit.zeros == 0:
        return UsCheck(PROFIT, True, "近四季每季都有賺錢")
    if profit.zeros == 0:
        return UsCheck(PROFIT, False, f"近四季有 {profit.losses} 季虧損")
    return UsCheck(PROFIT, False, f"近四季有 {profit.losses + profit.zeros} 季沒有賺錢")


def us_checks_of(facts: UsFacts) -> list[UsCheck]:
    """四個條件（ETF 只有走勢兩條）。"""
    trend = facts.trend
    if trend is None:
        result = [UsCheck(LONG, None, "日線資料不足"), UsCheck(MID, None, "日線資料不足")]
    else:
        result = [
            UsCheck(LONG, trend.above_year, "在年線之上" if trend.above_year else "在年線之下"),
            UsCheck(
                MID,
                trend.near_quarter,
                f"比季線{'高' if trend.p60 >= 0 else '低'} {stock_picks._fmt_tenths(trend.p60)}%"
                f"（{'在 ±5% 以內' if trend.near_quarter else '超過 5%'}）",
            ),
        ]
    if facts.kind != STOCK:
        return result
    if facts.fund == FUND_UNAVAILABLE:
        return result + [UsCheck(GROWTH, None, TEXT_FUND_UNAVAILABLE), UsCheck(PROFIT, None, TEXT_FUND_UNAVAILABLE)]
    return result + [_growth_check(facts.growth), _profit_check(facts.profit)]


def us_score_of(facts: UsFacts) -> int:
    return sum(1 for check in us_checks_of(facts) if check.state is True)


@dataclass(frozen=True)
class UsLight:
    level: str
    text: str
    show_score: bool = False  # 單檔卡第二行要不要補「符合 X 條（共 4 條）」

    @property
    def emoji(self) -> str:
        return stock_picks._EMOJI[self.level]

    @property
    def color(self) -> str:
        return stock_picks._COLOR[self.level]


def us_light_of(facts: UsFacts) -> UsLight:
    """燈號（同台股的順序、互斥）：⚪ → ⏸️ → ⚠️ 漲多／比季線低 → ETF ✅ → 4 條 ✅ → 財報抓不到 ⚠️ → 其他 ⚠️。"""
    trend = facts.trend
    if trend is None:
        return UsLight(stock_picks.NO_DATA, "資料不足")
    if not trend.above_year:
        return UsLight(stock_picks.BELOW_YEAR, "長期偏弱（在年線之下）", True)
    if trend.p60 > stock_picks.NEAR_PERMILLE:
        return UsLight(stock_picks.CAUTION, f"漲多了（比季線高 {stock_picks._fmt_tenths(trend.p60)}%）", True)
    if trend.p60 < -stock_picks.NEAR_PERMILLE:
        return UsLight(stock_picks.CAUTION, f"比季線低 {stock_picks._fmt_tenths(trend.p60)}%", True)
    if facts.kind != STOCK:
        return UsLight(stock_picks.MET, "走勢 2 條都符合（ETF 只看走勢）")
    if facts.fund == FUND_UNAVAILABLE:  # 不寫「其他條件不足」：其實可能 4 條都符合（PLAN v4 #9）
        return UsLight(stock_picks.CAUTION, f"走勢可以，{TEXT_FUND_UNAVAILABLE}")
    score = us_score_of(facts)
    if score >= MIN_SCORE:
        return UsLight(stock_picks.MET, "4 條都符合")
    return UsLight(stock_picks.CAUTION, f"走勢可以，其他條件不足（符合 {score} 條，共 {TOTAL_CONDITIONS} 條）")


def _pe_text(facts: UsFacts) -> str:
    if facts.fund == FUND_UNAVAILABLE:
        return f"本益比：{TEXT_FUND_UNAVAILABLE}"
    pe = facts.pe
    if pe.kind == PE_OK and pe.tenths is not None:
        return f"本益比 {stock_picks._fmt_tenths(pe.tenths)} 倍（參考，不算條件）"
    return {
        PE_OVER: "本益比超過 1000 倍（參考，不算條件）",
        PE_LOSS: "近四季合計虧損，本益比不適用",
        PE_NOT_USD: "財報不是美元，本益比不適用",
    }.get(pe.kind, "本益比資料不足")


def is_us_pick_candidate(facts: UsFacts) -> bool:
    return facts.kind == STOCK and facts.fund == FUND_OK and us_light_of(facts).level == stock_picks.MET


def choose_us_picks(pool: Sequence[UsFacts], limit: int = MAX_PICKS) -> list[UsFacts]:
    """池裡普通股、燈號 ✅ → 依（|季線乖離|、權重大到小、代號）取最多 3 檔。"""
    candidates = [f for f in pool if is_us_pick_candidate(f)]
    return sorted(candidates, key=lambda f: (abs(f.trend.p60), -(f.weight or 0.0), f.symbol))[:limit]


def us_most_missed(pool: Sequence[UsFacts]) -> Optional[tuple[str, int, int]]:
    """沒有推薦時：最常差哪一條 (條件, 沒過的檔數, 總檔數)；同數照卡片順序。資料不足也算沒過（同台股）。"""
    rows = [f for f in pool if f.kind == STOCK]
    if not rows:
        return None
    misses = {key: 0 for key in US_CONDITIONS}
    for facts in rows:
        for check in us_checks_of(facts):
            if check.state is not True:
                misses[check.key] += 1
    key = max(US_CONDITIONS, key=lambda k: (misses[k], -US_CONDITIONS.index(k)))
    return key, misses[key], len(rows)


# ── 名稱（外部字串：白名單字元＋長度，不合格只顯示代號） ─────────────────────────────────
NAME_MAX = 60
_NAME_PUNCT = frozenset(" &.,'-()/")


def clean_us_name(value, *, pool: bool = False) -> str:
    """Yahoo longName／shortName 或 SSGA 名稱 → 卡片名稱；不合格回 ""。SSGA 的「+」是「&」、「/THE」去掉、
    截斷留下的「...」去掉；「*」去掉（同一行兩個會被 main 的 Markdown 轉換吃掉）。"""
    if not isinstance(value, str):
        return ""
    name = unicodedata.normalize("NFKC", value[: NAME_MAX * 4]).replace("*", "").strip()
    if pool:
        name = name.replace("+", "&")
        if name.upper().endswith("/THE"):
            name = name[:-4]
    while name.endswith("...") or name.endswith("…"):
        name = (name[:-3] if name.endswith("...") else name[:-1]).rstrip()
    name = " ".join(name.split()).rstrip(" &,-/")
    if not (1 <= len(name) <= NAME_MAX) or not all((ch.isascii() and ch.isalnum()) or ch in _NAME_PUNCT for ch in name):
        return ""
    return name


# ── 卡片（只有固定句型、數字、代號、交易所登記的公司名） ──────────────────────────────────
def us_market_line(market: Optional[stock_picks.Market]) -> str:
    """美股大盤行：SPY 先照用台股的偏熱／偏冷門檻。"""
    if market is None:
        return "美股大盤（SPY）：資料不足"
    if market.level == stock_picks.HOT:
        return f"美股大盤（SPY）：偏熱（{stock_picks._market_gap_words(market)}），追高風險較大"
    if market.level == stock_picks.COLD:
        return f"美股大盤（SPY）：偏冷（{stock_picks._market_gap_words(market)}）"
    return "美股大盤（SPY）：中性"


def _label(facts: UsFacts) -> str:
    return f"{facts.symbol}（{facts.name}）" if facts.name else facts.symbol


def _us_day(day: date) -> str:
    return f"{stock_picks._fmt_date(day)}（美國時間）"


def _close_line(facts: UsFacts) -> str:
    if facts.trend is None:
        return f"資料到 {_us_day(facts.data_date)}"
    return f"收盤 {_fmt_usd(facts.trend.close)} 美元（{stock_picks._fmt_date(facts.data_date)} 美國時間）"


def _reference_line(trend: UsTrend) -> str:
    return f"季線（近 3 個月均價）{_fmt_usd(trend.ma60)} 美元｜年線（近 1 年均價）{_fmt_usd(trend.ma240)} 美元"


def _check_line(check: UsCheck) -> str:
    return f"{'✅' if check.state is True else '⬜'} {US_CONDITION_NAME[check.key]}：{check.text}"


def _source_line(facts: UsFacts) -> str:
    price = "Nasdaq 收盤" if facts.source == "db" else "Yahoo 收盤"
    what = price if facts.kind != STOCK else f"{price}、Yahoo 財報"
    verified = "｜收盤和 Nasdaq 核對過" if facts.verified else ""
    return f"依據：{what}{verified}｜資料到 {_us_day(facts.data_date)}"


def _pool_source_line(day: date) -> str:
    return f"依據：Nasdaq 收盤、Yahoo 財報｜資料到 {_us_day(day)}"


def _small(text: str) -> dict:
    return stock_picks._text(text, size="sm", color=_GRAY)


def _lead() -> list[dict]:
    return [
        stock_picks._text(stock_picks.TEXT_LEAD_NOTE, size="md", weight="bold", color=_GRAY),
        stock_picks._text(TEXT_US_POOL_LEAD, size="md"),
    ]


def _pool_footer(day: Optional[date]) -> list[dict]:
    lines = [_small(_pool_source_line(day))] if day is not None else []
    return lines + [_small(f"{TEXT_US_POOL_NOTE}；{stock_picks.TEXT_DISCLAIMER}")]


def us_pick_bubble(facts: UsFacts, index: int, total: int, market: Optional[stock_picks.Market], *,
                   lead_note: bool = False) -> dict:
    """美股推薦卡：燈號＋收盤＋推薦理由（成立的條件）＋本益比（參考）＋均價。"""
    light = us_light_of(facts)
    checks = us_checks_of(facts)
    body: list[dict] = (_lead() if lead_note else []) + [
        stock_picks._text(f"{light.emoji} {_label(facts)}", size="xl", weight="bold", color=light.color),
        stock_picks._text(f"{light.text}｜{_close_line(facts)}"),
        stock_picks._separator(),
        stock_picks._text("推薦理由", size="md", weight="bold"),
    ]
    body += [stock_picks._text(_check_line(c), size="md") for c in checks if c.state is True]
    missed = [c for c in checks if c.state is not True]
    if missed:  # 推薦一定 4 條都符合；留著是防呆（不會出現空的區塊）
        body.append(stock_picks._text("沒過的條件", size="md", weight="bold"))
        body += [stock_picks._text(_check_line(c), size="md", color=_GRAY) for c in missed]
    body.append(stock_picks._text(_pe_text(facts), size="md", color=_GRAY))
    if facts.trend is not None:
        body += [stock_picks._separator(), stock_picks._text(_reference_line(facts.trend), size="md", color=_GRAY)]
    return stock_picks._bubble(f"{stock_picks.PREFIX}美股第 {index} 檔（共 {total} 檔）", us_market_line(market), body,
                               _pool_footer(facts.data_date))


def us_no_pick_bubble(day: date, market: Optional[stock_picks.Market], missed: Optional[tuple[str, int, int]]) -> dict:
    body = _lead() + [stock_picks._text(f"這次（資料到 {stock_picks._fmt_date(day)} 美國時間）美股沒有 4 條都符合的",
                                        size="xl", weight="bold")]
    if missed is not None:
        key, count, total = missed
        body.append(stock_picks._text(f"最常差的條件：{US_CONDITION_NAME[key]}（{total} 檔裡有 {count} 檔沒過）"))
    return stock_picks._bubble(f"{stock_picks.PREFIX}美股", us_market_line(market), body, _pool_footer(day))


def us_not_updated_bubble(day: date) -> dict:
    body = _lead() + [
        stock_picks._text(TEXT_US_NOT_UPDATED, size="xl", weight="bold"),
        stock_picks._text(f"最後一次的資料到 {_us_day(day)}"),
    ]
    return stock_picks._bubble(f"{stock_picks.PREFIX}美股", None, body, _pool_footer(day))


def us_status_bubble(text: str) -> dict:
    """還沒有快照（計算中、暫時算不出來）。"""
    body = _lead() + [stock_picks._text(text, size="xl", weight="bold")]
    return stock_picks._bubble(f"{stock_picks.PREFIX}美股", None, body, _pool_footer(None))


def us_detail_bubble(facts: UsFacts, *, market: Optional[stock_picks.Market] = None,
                     market_date: Optional[date] = None) -> dict:
    """單檔卡：燈號＋收盤＋每個條件一列（✅／⬜）＋本益比＋均價；頁尾：美股大盤（有才寫）、依據、池說明、免責。"""
    light = us_light_of(facts)
    second = _close_line(facts)
    if facts.kind == STOCK and light.show_score and facts.fund == FUND_OK:
        second += f"｜符合 {us_score_of(facts)} 條（共 {TOTAL_CONDITIONS} 條）"
    body: list[dict] = [
        stock_picks._text(f"{light.emoji} {light.text}", size="xl", weight="bold", color=light.color),
        stock_picks._text(second),
        stock_picks._separator(),
    ]
    body += [stock_picks._text(_check_line(c), size="md") for c in us_checks_of(facts)]
    if facts.kind == STOCK:
        body.append(stock_picks._text(_pe_text(facts), size="md", color=_GRAY))
    else:
        body.append(stock_picks._text(TEXT_ETF_NA, size="md", color=_GRAY))
    if facts.trend is not None:
        body += [stock_picks._separator(), stock_picks._text(_reference_line(facts.trend), size="md", color=_GRAY)]
    footer: list[dict] = []
    if market is not None:
        suffix = (f"（{stock_picks._fmt_date(market_date)} 美國時間）"
                  if market_date is not None and market_date != facts.data_date else "")
        footer.append(_small(us_market_line(market) + suffix))
    footer.append(_small(_source_line(facts)))
    if facts.kind == STOCK:
        footer.append(_small(TEXT_US_DETAIL_POOL_NOTE))
    footer.append(_small(stock_picks.TEXT_DISCLAIMER))
    return stock_picks._bubble(f"{stock_picks.EVAL_PREFIX}{_label(facts)}", None, body, footer)


def us_detail_alt_text(facts: UsFacts) -> str:
    light = us_light_of(facts)
    return f"{stock_picks.EVAL_PREFIX}只供參考｜{_label(facts)}｜{light.emoji} {light.text}"


_cards_rejected = 0  # 自己驗沒過的卡片數（status_summary 回報；不默默拿掉）


def _card_ok(card, alt_text: Optional[str] = None) -> bool:
    global _cards_rejected
    if stock_card_check.card_ok(card, alt_text):
        return True
    _cards_rejected += 1
    return False


def _card_bytes(card: dict) -> int:
    return len(json.dumps(card, ensure_ascii=False).encode("utf-8"))


# ── 推薦池（DB；PLAN §3、§2.2） ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class PoolMember:
    symbol: str
    name: str
    weight: float


@dataclass(frozen=True)
class PoolList:
    as_of: Optional[date]
    members: tuple[PoolMember, ...]

    @property
    def symbols(self) -> list[str]:
        return [m.symbol for m in self.members]


_CLASS_SUFFIX_RE = re.compile(r"\s+CL(?:ASS)?\s+[A-Z](?:\s+SHARES)?\s*$")


def _company_key(name: str) -> str:
    """同公司不同股別：名稱去掉「...」與結尾「CL A／CL C／CLASS A（SHARES）」後相同（前 50 只有 GOOGL／GOOG）。"""
    return _CLASS_SUFFIX_RE.sub("", name.replace("...", "").strip().upper()).strip()


def parse_pool(obj) -> Optional[PoolList]:
    """obs us.holdings.SPY → 依權重大到小、股別去重的前 50 檔；不合格（列數 < 400、權重合計不在 90–101、
    沒有持股日期）回 None。權重不是有限正數、代號不合白名單的列不算。"""
    if not isinstance(obj, dict) or not isinstance(obj.get("rows"), list):
        return None
    as_of = obj.get("as_of")
    if not _iso_ok(as_of):
        return None
    rows: list[tuple[float, str, str]] = []
    for row in obj["rows"]:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        symbol, raw_name, weight = row[0], row[1], row[2]
        if not (isinstance(symbol, str) and _SYMBOL_RE.fullmatch(symbol)):
            continue
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            continue
        try:
            if not math.isfinite(weight) or not 0 < weight <= 100:
                continue
        except OverflowError:
            continue
        rows.append((float(weight), symbol, raw_name if isinstance(raw_name, str) else ""))
    if len(rows) < POOL_MIN_ROWS:
        return None
    total = math.fsum(w for w, _, _ in rows)
    if not POOL_WEIGHT_SUM[0] <= total <= POOL_WEIGHT_SUM[1]:
        return None
    members: list[PoolMember] = []
    seen_symbols: set[str] = set()
    seen_companies: set[str] = set()
    for weight, symbol, raw_name in sorted(rows, key=lambda r: (-r[0], r[1])):
        key = _company_key(raw_name) if raw_name.strip() else ""
        if symbol in seen_symbols or (key and key in seen_companies):
            continue
        seen_symbols.add(symbol)
        if key:
            seen_companies.add(key)
        members.append(PoolMember(symbol, clean_us_name(raw_name, pool=True), weight))
        if len(members) == POOL_N:
            break
    return PoolList(as_of=date.fromisoformat(as_of), members=tuple(members))


def select_d(calendar: Sequence[str], coverage: Mapping[str, int], pool_size: int, *,
             seen_latest: Optional[date], seen_latest_at: Optional[float], now: float) -> Optional[str]:
    """D＝日曆最後 5 天裡最新一個「推薦池每檔都有那一根」的日期；例外：日曆最新那天看到 ≥ 10 小時、
    只缺 ≤ 2 檔 → 用那天（缺的那幾檔 ⚪）。都沒有回 None。"""
    recent = list(calendar[-COVER_DAYS:])
    complete = [d for d in recent if coverage.get(d, 0) >= pool_size]
    best = complete[-1] if complete else None
    if recent:
        latest = recent[-1]
        if (best != latest and seen_latest is not None and seen_latest.isoformat() == latest
                and seen_latest_at is not None and now - seen_latest_at >= STRAGGLER_WAIT_S
                and coverage.get(latest, 0) >= pool_size - STRAGGLER_MAX_MISSING):
            best = latest
    return best


def db_trend(series: Mapping[str, float], cal_d: Sequence[str], d: str,
             quarantine: Sequence[tuple[str, str]]) -> tuple[Optional[UsTrend], str]:
    """推薦池一檔的走勢：窗＝日曆（到 D）最後 240／60 天裡這檔有的收盤；D 那根一定要有；窗內有未解除的
    quarantine → ⚪。不套 1.45 跳動閘門（DB 的列已經過盤勢 job 的確認）。"""
    if not _price_ok(series.get(d)):
        return None, "missing"
    year_dates, quarter_dates = cal_d[-stock_picks.YEAR_BARS:], cal_d[-stock_picks.QUARTER_BARS:]
    start = year_dates[0] if year_dates else d
    for low, high in quarantine:
        if str(low) <= d and str(high) >= start:
            return None, "quarantine"
    year = [series[x] for x in year_dates if _price_ok(series.get(x))]
    quarter = [series[x] for x in quarter_dates if _price_ok(series.get(x))]
    trend = us_trend_of(year, quarter)
    return (trend, "") if trend is not None else (None, "short")


def pool_member_facts(member: PoolMember, day: date, trend: Optional[UsTrend], gray: str,
                      entry: Optional[FundEntry]) -> UsFacts:
    """推薦池一檔：走勢（DB）＋財報（背景快取）。沒有財報 → 「財報暫時抓不到」。"""
    base = UsFacts(member.symbol, member.name, STOCK, day, trend, weight=member.weight, source="db",
                   fund=FUND_OK if entry is not None else FUND_UNAVAILABLE)
    if trend is None:
        return replace(base, gray=gray or "short")
    if entry is None:
        return base
    return replace(base, growth=growth_of(entry.rev, day), profit=profit_of(entry.ni, day),
                   pe=pe_of(entry.eps, entry.ni, trend.close, day))


def _spy_market(series: Mapping[str, float], cal_d: Sequence[str], d: str) -> Optional[stock_picks.Market]:
    if not _price_ok(series.get(d)):
        return None
    return stock_picks.market_of([series[x] for x in cal_d if _price_ok(series.get(x))])


# ── 快照與工作狀態（state/ 底下兩個 JSON；讀檔一律驗證，不合格當作沒有） ──────────────────────
@dataclass(frozen=True)
class UsSnapshot:
    d: date
    rule: str
    covered: int
    computed_at: float
    universe_as_of: Optional[date]
    spy: Optional[stock_picks.Market]
    spy_dates: tuple[date, ...]  # 日曆最後 260 個交易日（單檔查詢的缺日檢查用，回覆路徑不查 DB）
    pool: tuple[UsFacts, ...]
    picks: tuple[str, ...]
    missed: Optional[tuple[str, int, int]]
    latest_closes: Mapping[str, tuple[date, float]] = field(default_factory=dict)  # 全部美股最新一根正式收盤（單檔核對）

    def facts_of(self, symbol: str) -> Optional[UsFacts]:
        for facts in self.pool:
            if facts.symbol == symbol:
                return facts
        return None


def _facts_json(f: UsFacts) -> dict:
    t = f.trend
    return {
        "s": f.symbol, "n": f.name, "k": f.kind, "w": f.weight,
        "tr": None if t is None else [t.close, t.ma60, t.ma240, t.p60],
        "g": None if f.growth is None else [f.growth.q.isoformat(), f.growth.yoy],
        "p": None if f.profit is None else [f.profit.q.isoformat(), f.profit.losses, f.profit.zeros],
        "pe": [f.pe.kind, f.pe.tenths], "f": f.fund, "x": f.gray,
    }


def _v_symbol(value) -> str:
    stock_picks._require(isinstance(value, str) and bool(_SYMBOL_RE.fullmatch(value)))
    return value


def _v_choice(value, allowed: Sequence[str]) -> str:
    stock_picks._require(isinstance(value, str) and value in allowed)
    return value


def _facts_from(value, day: date) -> UsFacts:
    v = stock_picks
    v._require(isinstance(value, dict))
    name = v._v_str(value.get("n"), NAME_MAX, allow_empty=True)
    v._require(name == "" or clean_us_name(name) == name)
    trend = None
    if value.get("tr") is not None:
        raw = value["tr"]
        v._require(isinstance(raw, list) and len(raw) == 4)
        trend = UsTrend(close=v._v_num(raw[0], 1e-6, _MAX_PRICE), ma60=v._v_num(raw[1], 1e-6, _MAX_PRICE),
                        ma240=v._v_num(raw[2], 1e-6, _MAX_PRICE), p60=v._v_int(raw[3], -10**6, 10**6))
    growth = profit = None
    if value.get("g") is not None:
        raw = value["g"]
        v._require(isinstance(raw, list) and len(raw) == 2)
        growth = Growth(q=v._v_date(raw[0]), yoy=v._v_int(raw[1], -10**7, 10**7))
    if value.get("p") is not None:
        raw = value["p"]
        v._require(isinstance(raw, list) and len(raw) == 3)
        profit = Profit(q=v._v_date(raw[0]), losses=v._v_int(raw[1], 0, 4), zeros=v._v_int(raw[2], 0, 4))
    pe_raw = value.get("pe")
    v._require(isinstance(pe_raw, list) and len(pe_raw) == 2)
    pe = Pe(_v_choice(pe_raw[0], _PE_KINDS), v._v_int(pe_raw[1], 0, 10**5, allow_none=True))
    v._require((pe.kind == PE_OK) == (pe.tenths is not None))
    gray = v._v_str(value.get("x"), 20, allow_empty=True)
    return UsFacts(
        symbol=_v_symbol(value.get("s")), name=name, kind=_v_choice(value.get("k"), (STOCK, ETF)), data_date=day,
        trend=trend, growth=growth, profit=profit, pe=pe, fund=_v_choice(value.get("f"), _FUND_STATES),
        weight=v._v_num(value.get("w"), 1e-9, 100.0, allow_none=True), gray=gray if trend is None else "", source="db",
    )


def snapshot_to_json(s: UsSnapshot) -> dict:
    return {
        "schema": SNAPSHOT_SCHEMA,
        "rule": s.rule,
        "d": s.d.isoformat(),
        "covered": s.covered,
        "computed_at": s.computed_at,
        "universe_as_of": None if s.universe_as_of is None else s.universe_as_of.isoformat(),
        "spy": None if s.spy is None else {"level": s.spy.level, "b20": s.spy.b20, "b60": s.spy.b60},
        "spy_dates": [d.isoformat() for d in s.spy_dates],
        "pool": [_facts_json(f) for f in s.pool],
        "picks": list(s.picks),
        "missed": None if s.missed is None else list(s.missed),
        "latest_closes": {sym: [d.isoformat(), c] for sym, (d, c) in sorted(s.latest_closes.items())},
    }


def snapshot_from_json(obj, now: float) -> UsSnapshot:
    v = stock_picks
    v._require(isinstance(obj, dict) and obj.get("schema") == SNAPSHOT_SCHEMA)
    day = v._v_date(obj.get("d"))
    v._require(day <= _us_today(now))
    spy = None
    if obj.get("spy") is not None:
        raw = obj["spy"]
        v._require(isinstance(raw, dict))
        spy = stock_picks.Market(level=_v_choice(raw.get("level"), (stock_picks.HOT, stock_picks.COLD, stock_picks.NEUTRAL)),
                                 b20=v._v_int(raw.get("b20"), -10**6, 10**6), b60=v._v_int(raw.get("b60"), -10**6, 10**6))
    dates_raw, pool_raw, picks_raw = obj.get("spy_dates"), obj.get("pool"), obj.get("picks")
    v._require(isinstance(dates_raw, list) and len(dates_raw) <= 400)
    spy_dates = tuple(v._v_date(x) for x in dates_raw)
    v._require(all(a < b for a, b in zip(spy_dates, spy_dates[1:])))
    v._require(isinstance(pool_raw, list) and 1 <= len(pool_raw) <= POOL_N * 2)
    pool = tuple(_facts_from(x, day) for x in pool_raw)
    symbols = [f.symbol for f in pool]
    v._require(len(set(symbols)) == len(symbols))
    v._require(isinstance(picks_raw, list) and len(picks_raw) <= MAX_PICKS)
    picks = tuple(_v_symbol(x) for x in picks_raw)
    v._require(all(p in symbols for p in picks) and len(set(picks)) == len(picks))
    missed = None
    if obj.get("missed") is not None:
        raw = obj["missed"]
        v._require(isinstance(raw, list) and len(raw) == 3)
        missed = (_v_choice(raw[0], US_CONDITIONS), v._v_int(raw[1], 0, 10**4), v._v_int(raw[2], 0, 10**4))
    latest_raw = obj.get("latest_closes")
    v._require(isinstance(latest_raw, dict) and len(latest_raw) <= 5000)
    latest = {}
    for sym, pair in latest_raw.items():
        v._require(isinstance(pair, list) and len(pair) == 2)
        latest[_v_symbol(sym)] = (v._v_date(pair[0]), v._v_num(pair[1], 1e-6, _MAX_PRICE))
    as_of = obj.get("universe_as_of")
    return UsSnapshot(
        d=day, rule=v._v_str(obj.get("rule"), 20), covered=v._v_int(obj.get("covered"), 0, 10**4),
        computed_at=stock_picks._at_most(v._v_num(obj.get("computed_at"), 0.0, 1e11), now + stock_picks._CLOCK_SKEW_S),
        universe_as_of=None if as_of is None else v._v_date(as_of), spy=spy, spy_dates=spy_dates, pool=pool,
        picks=picks, missed=missed, latest_closes=latest,
    )


@dataclass(frozen=True)
class UsWorker:
    """背景工作的狀態（整個換掉、不改內容；每次變化都原子寫入，重啟後沿用）。"""

    last_ok_check: Optional[float] = None
    fail_count: int = 0
    next_try: Optional[float] = None
    last_fail: str = ""
    last_wait: str = ""  # 在等什麼（不是失敗）
    seen_latest: Optional[date] = None  # DB 日曆最新的日期
    seen_latest_at: Optional[float] = None  # 第一次看到它的時間
    behind_since: Optional[float] = None  # 第一次發現快照落後的時間（之後又有更新的日期不重設，快照追上才清掉）
    bg_yahoo_until: Optional[float] = None  # 背景自己的 Yahoo 429／403 冷卻
    fund_day: Optional[str] = None  # 財報請求帳的日期（台北）
    fund_count: int = 0  # 那一天已經記的財報請求數（送出前先記）
    fund: Mapping[str, FundEntry] = field(default_factory=dict)
    writer: str = ""


_MAX_BACKOFF_S = max(*stock_picks.BACKOFF_S, stock_picks.BLOCKED_BACKOFF_S, stock_picks.STATE_CORRUPT_BACKOFF_S,
                     NOT_YET_RETRY_S, DB_RETRY_S)


def _fund_rows_json(rows: Sequence[FundRow]) -> list:
    return [[d.isoformat(), v, c] for d, v, c in rows]


def worker_to_json(w: UsWorker) -> dict:
    return {
        "schema": WORKER_SCHEMA,
        "last_ok_check": w.last_ok_check,
        "fail_count": w.fail_count,
        "next_try": w.next_try,
        "last_fail": w.last_fail,
        "last_wait": w.last_wait,
        "seen_latest": None if w.seen_latest is None else w.seen_latest.isoformat(),
        "seen_latest_at": w.seen_latest_at,
        "behind_since": w.behind_since,
        "bg_yahoo_until": w.bg_yahoo_until,
        "fund_day": w.fund_day,
        "fund_count": w.fund_count,
        "fund": {
            sym: {"at": e.at, "latest_q": None if e.latest_q is None else e.latest_q.isoformat(),
                  "rev": _fund_rows_json(e.rev), "ni": _fund_rows_json(e.ni), "eps": _fund_rows_json(e.eps)}
            for sym, e in sorted(w.fund.items())
        },
        "writer": w.writer,
    }


def _fund_rows_from(value) -> tuple:
    v = stock_picks
    v._require(isinstance(value, list) and len(value) <= FUND_MAX_ROWS)
    rows = []
    for item in value:
        v._require(isinstance(item, list) and len(item) == 3)
        cur = v._v_str(item[2], 5)
        v._require(cur.isascii() and cur.isalpha() and cur.isupper())
        rows.append((v._v_date(item[0]), v._v_num(item[1], -FUND_VALUE_MAX, FUND_VALUE_MAX), cur))
    v._require(all(a[0] < b[0] for a, b in zip(rows, rows[1:])))
    return tuple(rows)


def _fund_entry_from(value, now: float) -> FundEntry:
    v = stock_picks
    v._require(isinstance(value, dict))
    at = min(v._v_num(value.get("at"), 0.0, 1e11), now)  # 未來時間夾回（不然永遠不會被當成要更新）
    return FundEntry(at=at, rev=_fund_rows_from(value.get("rev")), ni=_fund_rows_from(value.get("ni")),
                     eps=_fund_rows_from(value.get("eps")))


def _lenient_text(value) -> str:
    return value[:40] if isinstance(value, str) and value.isprintable() else ""


def worker_from_json(obj, now: float) -> UsWorker:
    """嚴格驗證退避與請求帳（型別不對整個檔當壞檔）；財報快取一檔不合格只丟那一檔（帳不能因為一檔壞掉就歸零）。
    缺的欄位用預設值：部署的種子檔只帶 {"schema": 1, "fund": {...}}（PLAN §10.3）。"""
    v = stock_picks
    v._require(isinstance(obj, dict) and obj.get("schema") == WORKER_SCHEMA)
    soon = now + stock_picks._CLOCK_SKEW_S
    fund_raw = obj.get("fund", {})
    v._require(isinstance(fund_raw, dict) and len(fund_raw) <= FUND_CACHE_MAX * 2)
    fund = {}
    for sym, raw in fund_raw.items():
        try:
            fund[_v_symbol(sym)] = _fund_entry_from(raw, now)
        except (ValueError, TypeError, OverflowError):
            logger.warning("stock picks us fund cache entry dropped")
    seen = obj.get("seen_latest")
    seen_day = None if seen is None else v._v_date(seen)
    if seen_day is not None and seen_day > _us_today(now):
        seen_day = None
    fund_day = obj.get("fund_day")
    if fund_day is not None:
        v._v_date(fund_day)
        fund_day = min(fund_day, _tw_day(now))  # 時鐘倒退：帳照算在今天（寧可多記）
    return UsWorker(
        last_ok_check=stock_picks._at_most(v._v_num(obj.get("last_ok_check"), 0.0, 1e11, allow_none=True), soon),
        fail_count=v._v_int(obj.get("fail_count", 0), 0, 10**6),
        next_try=stock_picks._at_most(v._v_num(obj.get("next_try"), 0.0, 1e11, allow_none=True), now + _MAX_BACKOFF_S + stock_picks._CLOCK_SKEW_S),
        last_fail=_lenient_text(obj.get("last_fail")),
        last_wait=_lenient_text(obj.get("last_wait")),
        seen_latest=seen_day,
        seen_latest_at=None if seen_day is None else stock_picks._at_most(v._v_num(obj.get("seen_latest_at"), 0.0, 1e11, allow_none=True), now),
        behind_since=stock_picks._at_most(v._v_num(obj.get("behind_since"), 0.0, 1e11, allow_none=True), now),
        bg_yahoo_until=stock_picks._at_most(v._v_num(obj.get("bg_yahoo_until"), 0.0, 1e11, allow_none=True), now + stock_picks.BLOCKED_BACKOFF_S + stock_picks._CLOCK_SKEW_S),
        fund_day=fund_day,
        fund_count=v._v_int(obj.get("fund_count", 0), 0, 10**5),
        fund=fund,
        writer=v._v_str(obj.get("writer") or "", 64, allow_empty=True),
    )


def _snapshot_path():
    return stock_picks.STATE_DIR / SNAPSHOT_NAME


def _worker_path():
    return stock_picks.STATE_DIR / WORKER_NAME


def _load_snapshot_file(now: float) -> Optional[UsSnapshot]:
    try:
        obj = stock_picks._read_json_file(_snapshot_path())
        return None if obj is None else snapshot_from_json(obj, now)
    except Exception as exc:
        logger.error("stock picks us snapshot unreadable error_type=%s", type(exc).__name__)
        return None


def _corrupt_worker(now: float) -> UsWorker:
    """工作狀態檔壞掉：退避 1 小時，而且當天的財報額度當作用完（帳讀不回來，寧可少抓）。"""
    return UsWorker(next_try=now + stock_picks.STATE_CORRUPT_BACKOFF_S, last_fail="state_corrupt",
                    fund_day=_tw_day(now), fund_count=FUND_PER_DAY)


def _load_worker_file(now: float) -> UsWorker:
    try:
        obj = stock_picks._read_json_file(_worker_path())
        return UsWorker() if obj is None else worker_from_json(obj, now)
    except Exception as exc:
        logger.error("stock picks us worker state unreadable error_type=%s", type(exc).__name__)
        return _corrupt_worker(now)


# ── 執行期狀態（快照、工作狀態都是整個換掉的不可變物件；回覆只讀，不拿鎖、不等檔案鎖） ──────────────
_mem_lock = threading.Lock()  # 只保護幾個參照的換手；不包任何 I/O、不和檔案鎖一起拿
_snapshot: Optional[UsSnapshot] = None
_worker = UsWorker()
_loaded = False
_worker_side_ready = False
_perf_result: Optional[dict] = None
_WRITER_ID = f"{os.getpid()}-{time.time_ns()}"


def _perf_module():
    """成績模組（PERF 子模組）；載不到回 None（美股照樣能用，只是沒有成績卡）。"""
    try:
        import stock_picks_perf
    except Exception as exc:
        logger.warning("stock picks perf import failed error_type=%s", type(exc).__name__)
        return None
    return stock_picks_perf


def _ensure_loaded() -> None:
    """第一次用到時讀狀態檔（回覆路徑也可能先到：只讀檔，不拿檔案鎖、不寫檔）。"""
    global _snapshot, _worker, _loaded, _perf_result
    if _loaded:
        return
    now = _wall()
    snapshot, worker = _load_snapshot_file(now), _load_worker_file(now)
    perf = None
    module = _perf_module()
    if module is not None:
        try:
            perf = module.load_result()
        except Exception as exc:
            logger.warning("stock picks perf load failed error_type=%s", type(exc).__name__)
    with _mem_lock:
        if _loaded:
            return
        _snapshot, _worker, _perf_result, _loaded = snapshot, worker, perf, True


def _set_worker(worker: UsWorker) -> None:
    global _worker
    with _mem_lock:
        _worker = worker


def _set_snapshot(snapshot: Optional[UsSnapshot]) -> None:
    global _snapshot
    with _mem_lock:
        _snapshot = snapshot


def _sweep_stale_tmp(now: float, max_age_s: float = 3600.0) -> None:
    """寫檔途中被殺留下的暫存檔：超過 1 小時的清掉（只動美股與成績的檔名）。"""
    try:
        for pattern in (".stock_picks_us*.tmp", ".stock_picks_perf*.tmp"):
            for path in stock_picks.STATE_DIR.glob(pattern):
                with contextlib.suppress(FileNotFoundError):
                    if path.is_file() and not path.is_symlink() and path.stat().st_mtime < now - max_age_s:
                        path.unlink()
    except OSError as exc:
        logger.info("stock picks us tmp sweep skipped error_type=%s", type(exc).__name__)


def _prepare_worker_side(now: float) -> None:
    """背景第一次跑：清暫存檔、壞掉的工作狀態檔順手修好（退避一起存）。"""
    global _worker_side_ready
    if _worker_side_ready:
        return
    _worker_side_ready = True
    _sweep_stale_tmp(now)
    if _worker.last_fail == "state_corrupt":
        _save_worker(now)


def _merge_worker(mem: UsWorker, disk: Optional[UsWorker], now: float) -> UsWorker:
    """部署時新舊程序可能重疊：財報請求帳取大的（同一天）或較新的那天、財報快取以 at 新的為準；
    別的程序寫的退避、冷卻取晚的、最後成功檢查取晚的、看到的最新日期取新的。自己上次寫的退避不併。"""
    if disk is None:
        return mem
    fund = dict(disk.fund)
    for sym, entry in mem.fund.items():
        if sym not in fund or entry.at >= fund[sym].at:
            fund[sym] = entry
    if disk.fund_day == mem.fund_day:
        day, count = mem.fund_day, max(mem.fund_count, disk.fund_count)
    elif disk.fund_day is not None and (mem.fund_day is None or disk.fund_day > mem.fund_day):
        day, count = disk.fund_day, disk.fund_count
    else:
        day, count = mem.fund_day, mem.fund_count
    merged = replace(mem, fund=_trim_fund(fund, keep=set(mem.fund)), fund_day=day, fund_count=count)
    if disk.writer and disk.writer != _WRITER_ID:
        if disk.next_try is not None and disk.next_try > now:
            merged = replace(merged, next_try=max(merged.next_try or 0.0, disk.next_try))
        if disk.bg_yahoo_until is not None and disk.bg_yahoo_until > now:
            merged = replace(merged, bg_yahoo_until=max(merged.bg_yahoo_until or 0.0, disk.bg_yahoo_until))
        if disk.last_ok_check is not None:
            merged = replace(merged, last_ok_check=max(merged.last_ok_check or 0.0, disk.last_ok_check))
        if disk.seen_latest is not None and (merged.seen_latest is None or disk.seen_latest > merged.seen_latest):
            merged = replace(merged, seen_latest=disk.seen_latest, seen_latest_at=disk.seen_latest_at)
        elif disk.seen_latest is not None and disk.seen_latest == merged.seen_latest and disk.seen_latest_at is not None:
            merged = replace(merged, seen_latest_at=min(merged.seen_latest_at or disk.seen_latest_at, disk.seen_latest_at))
    return merged


def _trim_fund(fund: Mapping[str, FundEntry], keep: set) -> dict:
    """最多 150 檔：先丟不在 keep（目前用得到的）裡、最久沒更新的。"""
    out = dict(fund)
    if len(out) > FUND_CACHE_MAX:
        victims = sorted(out, key=lambda s: (s in keep, out[s].at))
        for sym in victims[: len(out) - FUND_CACHE_MAX]:
            del out[sym]
    return out


def _save_worker(now: float) -> bool:
    """檔案鎖裡先併磁碟上的狀態再寫；成功回 True（失敗只記種類，狀態留在記憶體，下次再寫）。"""
    path = _worker_path()
    try:
        with stock_picks._state_file_lock():
            try:
                obj = stock_picks._read_json_file(path)
                disk = None if obj is None else worker_from_json(obj, now)
            except Exception:  # 磁碟上的壞了：用記憶體的蓋過去（帳在記憶體裡）
                disk = None
            merged = replace(_merge_worker(_worker, disk, now), writer=_WRITER_ID)
            stock_picks._write_json_file(path, worker_to_json(merged))
    except Exception as exc:
        logger.error("stock picks us worker state write failed error_type=%s", type(exc).__name__)
        return False
    _set_worker(merged)
    return True


COMMIT_WRITTEN, COMMIT_DISK, COMMIT_FAILED = "written", "disk", "failed"


def _commit_snapshot(new: UsSnapshot, now: float, *, refresh_only: bool = False) -> str:
    """寫快照再換記憶體（兩邊一致）。鎖內重讀磁碟：磁碟上的 D 比較新 → 不寫、改用磁碟上的；重算時磁碟上已經
    有同一個 D、同一個 rule（別的程序先算了）→ 也用磁碟上的（每個 D 只算一次）。refresh_only（只換最新收盤與
    日曆）時同一個 D 照寫。寫不進去回 COMMIT_FAILED、記憶體不換。"""
    try:
        payload = snapshot_to_json(new)
        snapshot_from_json(json.loads(json.dumps(payload)), now)  # 寫得出去的就讀得回來（同一套驗證）
        with stock_picks._state_file_lock():
            disk = _load_snapshot_file(now)
            if disk is not None and (disk.d > new.d or (not refresh_only and disk.d == new.d and disk.rule == new.rule)):
                adopted = disk
            else:
                stock_picks._write_json_file(_snapshot_path(), payload)
                adopted = new
    except Exception as exc:
        logger.error("stock picks us snapshot write failed error_type=%s", type(exc).__name__)
        return COMMIT_FAILED
    _set_snapshot(adopted)
    return COMMIT_WRITTEN if adopted is new else COMMIT_DISK


def _fail(kind: str, now: float, *, blocked: bool = False, not_yet: bool = False, retry_s: Optional[float] = None) -> float:
    """記下這一輪沒算成、決定下次什麼時候再試；回要等幾秒。not_yet 不加失敗次數；期限只延長不縮短。"""
    retry = stock_picks._valid_retry(retry_s)
    w = _worker
    if not_yet and not blocked:
        delay = retry or NOT_YET_RETRY_S
        w = replace(w, last_wait=kind[:40])
    else:
        count = w.fail_count + 1
        if retry is not None:
            delay = retry
        elif blocked:
            delay = stock_picks.BLOCKED_BACKOFF_S
        else:
            delay = stock_picks.BACKOFF_S[min(count - 1, len(stock_picks.BACKOFF_S) - 1)]
        w = replace(w, fail_count=count, last_fail=kind[:40], last_wait="")
    delay = min(float(delay), float(_MAX_BACKOFF_S))
    _set_worker(replace(w, next_try=max(now + delay, w.next_try or 0.0)))
    _save_worker(now)  # 存檔時會併進別的程序更晚的期限：回傳的等待以存完的為準
    wait = max(0.0, (_worker.next_try or now) - now)
    if not_yet and not blocked:
        logger.info("stock picks us waiting kind=%s retry_in_s=%d", kind, int(wait))
    else:
        logger.warning("stock picks us round failed kind=%s count=%d", kind, _worker.fail_count)
    return wait


def _not_updated(snapshot: UsSnapshot, worker: UsWorker, now: float) -> bool:
    """DB 日曆已經有比快照新的日期、而且落後 ≥ 12 小時；或快照的 D 比美東今天早 > 7 天；或 36 小時沒成功檢查。"""
    if (worker.seen_latest is not None and worker.seen_latest > snapshot.d and worker.behind_since is not None
            and now - worker.behind_since >= BEHIND_GRACE_S):
        return True
    if (_us_today(now) - snapshot.d).days > STALE_DAYS:
        return True
    return now - max(worker.last_ok_check or 0.0, snapshot.computed_at) >= CHECK_STALE_S


# ── 一輪（背景；PLAN §4） ─────────────────────────────────────────────────────────
def _calendar(today: date) -> list[str]:
    """美股日曆（≤ 美東今天）；日期格式不對當讀不到（欄位改了）。"""
    days = stock_db_ro.us_calendar((today - timedelta(days=CALENDAR_LOOKBACK_DAYS)).isoformat())
    if not all(_iso_ok(d) for d in days):
        raise stock_db_ro.DbUnavailable("bad_date")
    return [d for d in days if d <= today.isoformat()]


def _latest_closes() -> dict:
    """全部美股代號最新一根正式收盤（約 530 檔，一個查詢）；格式不對的列不收。"""
    out = {}
    for symbol, (day, close) in stock_db_ro.latest_closes("us").items():
        if isinstance(symbol, str) and _SYMBOL_RE.fullmatch(symbol) and _iso_ok(day) and _price_ok(close):
            out[symbol] = (date.fromisoformat(day), float(close))
    return out


def _note_latest(latest: str, now: float) -> None:
    """記下日曆最新的日期第一次看到的時間；快照落後的話記 behind_since（只在第一次記，追上才清）。"""
    w = _worker
    seen, seen_at = w.seen_latest, w.seen_latest_at
    if seen is None or latest > seen.isoformat():
        seen, seen_at = date.fromisoformat(latest), now
    snapshot = _snapshot
    behind = w.behind_since
    if snapshot is not None and snapshot.d.isoformat() < latest:
        behind = behind if behind is not None else now
    else:
        behind = None
    _set_worker(replace(w, seen_latest=seen, seen_latest_at=seen_at, behind_since=behind, last_ok_check=now))


def _pool_of_snapshot(snapshot: Optional[UsSnapshot]) -> Optional[PoolList]:
    if snapshot is None or snapshot.universe_as_of is None:
        return None
    members = tuple(PoolMember(f.symbol, f.name, f.weight) for f in snapshot.pool if f.weight is not None)
    return PoolList(as_of=snapshot.universe_as_of, members=members) if members else None


def _choose_pool(snapshot: Optional[UsSnapshot], calendar: Sequence[str], now: float) -> tuple[PoolList, str, int]:
    """新的持股（合格而且持股日期在 D−10～D+3）→ 否則沿用上一份池（≤ 45 天）→ 都沒有就失敗；
    池裡最近 5 天都沒到齊 → 還沒好（not_yet）。回 (池, D, D 那天到齊的檔數)。"""
    try:
        obj = stock_db_ro.obs_json(HOLDINGS_OBS)
    except stock_db_ro.DbUnavailable as exc:
        if exc.kind != "bad_json":
            raise
        obj = None
    candidates = [("fresh", parse_pool(obj)), ("previous", _pool_of_snapshot(snapshot))]
    w = _worker
    incomplete = False
    for kind, pool in candidates:
        if pool is None or not pool.members:
            continue
        coverage = stock_db_ro.coverage_by_date("us", pool.symbols, calendar[-COVER_DAYS:][0])
        d = select_d(calendar, coverage, len(pool.members), seen_latest=w.seen_latest,
                     seen_latest_at=w.seen_latest_at, now=now)
        if d is None:
            incomplete = True
            continue
        age = (date.fromisoformat(d) - pool.as_of).days
        if kind == "fresh" and not -POOL_AS_OF_DAYS[1] <= age <= -POOL_AS_OF_DAYS[0]:
            continue
        if kind == "previous" and not -POOL_AS_OF_DAYS[1] <= age <= POOL_MAX_AGE_DAYS:
            continue
        if kind == "previous":
            logger.warning("stock picks us using previous pool as_of=%s", pool.as_of.isoformat())
        return pool, d, int(coverage.get(d, 0))
    if incomplete:
        raise stock_picks.RoundFailed("pool_incomplete", not_yet=True)
    raise stock_picks.RoundFailed("pool_invalid")


def _yahoo_cooling(now: float) -> bool:
    """背景：計畫 C 的共用冷卻（AI 關閉時不作用）或美股自己的冷卻還沒過。"""
    return stock_picks._yahoo_blocked() or (_worker.bg_yahoo_until or 0.0) > now


def _charge_requests(n: int, now: float) -> bool:
    """財報請求帳：送出前先記 n 次並存檔（bot 常被自動重啟，寧可多記不少記）；超過當天上限或存不進去回 False。"""
    w = _worker
    today = _tw_day(now)
    count = w.fund_count if w.fund_day == today else 0
    if n <= 0 or count + n > FUND_PER_DAY:
        return False
    _set_worker(replace(w, fund_day=today, fund_count=count + n))
    return _save_worker(now)


def _fund_params(ysym: str, now: float) -> dict:
    end = int(now)
    return {"symbol": ysym, "type": ",".join(name for _, name in _FUND_TYPES),
            "period1": end - FUND_LOOKBACK_DAYS * 86400, "period2": end}


def _fetch_fund_background(symbol: str, stop: threading.Event, give_up: float, now: float) -> Optional[dict]:
    """背景抓一檔財報：先 query1 再 query2（第二次也記帳）；429／403／stopped 往上丟，其他失敗回 None。"""
    ysym = _yahoo_symbol(symbol)
    path = f"/ws/fundamentals-timeseries/v1/finance/timeseries/{quote(ysym, safe='')}"
    for i, host in enumerate(_YAHOO_HOSTS):
        if i and not _charge_requests(1, now):
            return None
        budget = stock_picks._budget_left(give_up, FUND_TIMEOUT_S)
        try:
            status, data, _ = stock_picks._get_json(host, path, params=_fund_params(ysym, now), max_bytes=FUND_MAX_BYTES,
                                                    timeout=budget, total_s=budget, stop=stop)
        except stock_picks.FetchError as exc:
            if exc.kind in ("rate_limited", "blocked", "stopped"):
                raise
            logger.info("stock picks us fund fetch failed host=%s kind=%s", host, exc.kind)
            if exc.kind == "not_found":  # 代號 Yahoo 不認得：換主機也一樣
                return None
            continue
        if status == 200 and isinstance(data, dict):
            return data
    return None


def _refresh_fundamentals(wanted: Sequence[str], stop: threading.Event, now: float, give_up: float) -> set:
    """照節奏補抓財報（每檔 3 天一次；沒抓過的先、再來最久沒更新的；每輪 25、每天 40；一次一檔、依序）。
    回「這輪抓過但失敗、手上也沒有財報」的代號。429／403 → 共用冷卻＋自己的冷卻、整輪失敗（已抓的留在快取）。"""
    fund = _worker.fund
    due = [s for s in wanted if s not in fund or now - fund[s].at >= FUND_REFRESH_S]
    due.sort(key=lambda s: (s in fund, fund[s].at if s in fund else 0.0))  # sort 穩定：沒抓過的照池的順序
    if not due or _yahoo_cooling(now):
        return set()
    w = _worker
    used = w.fund_count if w.fund_day == _tw_day(now) else 0
    batch = due[: max(0, min(FUND_PER_ROUND, FUND_PER_DAY - used))]
    if not batch or not _charge_requests(len(batch), now):
        return set()
    failed: set = set()
    keep = set(wanted)
    for i, symbol in enumerate(batch):
        if stop.is_set():
            raise stock_picks.RoundFailed("stopped")
        if time.monotonic() > give_up or _yahoo_cooling(now):
            break
        if i and FUND_SPACING_S > 0 and stop.wait(FUND_SPACING_S):
            raise stock_picks.RoundFailed("stopped")
        try:
            payload = _fetch_fund_background(symbol, stop, give_up, now)
        except stock_picks.FetchError as exc:
            if exc.kind == "stopped":
                raise stock_picks.RoundFailed("stopped") from None
            stock_picks._block_yahoo()  # 台股的 AI 段也跟著等（AI 關閉時這個不作用）
            _set_worker(replace(_worker, bg_yahoo_until=max(_worker.bg_yahoo_until or 0.0, now + stock_picks.BLOCKED_BACKOFF_S)))
            raise stock_picks.RoundFailed("yahoo_rate_limited", blocked=True) from None
        parsed = parse_fundamentals(payload) if payload is not None else None
        if parsed is None:
            if symbol not in _worker.fund:
                failed.add(symbol)
            continue
        fresh = dict(_worker.fund)
        fresh[symbol] = _entry_from_parsed(parsed, now)
        _set_worker(replace(_worker, fund=_trim_fund(fresh, keep)))
    return failed


def _recompute(stop: threading.Event, now: float, pool: PoolList, d: str, covered: int, calendar: Sequence[str],
               latest: Mapping[str, tuple[date, float]], spy_dates: tuple[date, ...]) -> UsSnapshot:
    """重算一個 D（150 秒為限）：池的收盤（一個 IN 查詢）、quarantine → 走勢 → 財報（照節奏）→ facts → 閘門 → 推薦。"""
    give_up = time.monotonic() + REFRESH_BUDGET_S
    day = date.fromisoformat(d)
    cal_d = [x for x in calendar if x <= d]
    symbols = pool.symbols
    closes = stock_db_ro.closes("us", [*symbols, SPY], (day - timedelta(days=CALENDAR_LOOKBACK_DAYS)).isoformat())
    quarantine = stock_db_ro.active_quarantine("us", symbols)
    limit = len(pool.members) * MAX_GRAY_RATIO
    trends: dict[str, tuple[Optional[UsTrend], str]] = {}
    for m in pool.members:
        try:  # 一檔的資料怪到丟例外：只有那一檔 ⚪（算進閘門），不讓整輪一直失敗
            trends[m.symbol] = db_trend(closes.get(m.symbol, {}), cal_d, d, quarantine.get(m.symbol, ()))
        except Exception as exc:
            logger.warning("stock picks us trend failed error_type=%s", type(exc).__name__)
            trends[m.symbol] = (None, "error")
    gray = sum(1 for t, _ in trends.values() if t is None)
    if gray > limit:  # 走勢就已經過不了閘門：不浪費 Yahoo 請求
        raise stock_picks.RoundFailed("too_many_gray")
    wanted = [m.symbol for m in pool.members if trends[m.symbol][0] is not None]
    failed = _refresh_fundamentals(wanted, stop, now, give_up)
    if stop.is_set():
        raise stock_picks.RoundFailed("stopped")
    fund = _worker.fund
    waiting = [s for s in wanted if s not in fund and s not in failed]
    if len(waiting) > limit:  # 被每輪／每天上限擋下、還沒抓過的太多：這輪先不寫快照
        raise stock_picks.RoundFailed("fund_waiting", not_yet=True)
    facts: list[UsFacts] = []
    bad = gray
    for m in pool.members:
        trend, reason = trends[m.symbol]
        entry = fund.get(m.symbol)
        try:
            f = pool_member_facts(m, day, trend, reason, entry)
        except Exception as exc:
            logger.warning("stock picks us facts failed error_type=%s", type(exc).__name__)
            f = UsFacts(m.symbol, m.name, STOCK, day, None, weight=m.weight, gray="error", fund=FUND_UNAVAILABLE)
        if trend is not None and (f.trend is None or (entry is not None and (f.growth is None or f.profit is None))
                                  or (entry is None and m.symbol in failed)):
            bad += 1  # ⚪（計算出錯）或財報資料不足（抓到了但不夠、或這輪抓失敗）
        facts.append(f)
    if bad > limit:
        raise stock_picks.RoundFailed("too_many_gray")
    picks = choose_us_picks(facts)
    return UsSnapshot(
        d=day, rule=RULE, covered=covered, computed_at=now, universe_as_of=pool.as_of,
        spy=_spy_market(closes.get(SPY, {}), cal_d, d), spy_dates=spy_dates, pool=tuple(facts),
        picks=tuple(p.symbol for p in picks), missed=None if picks else us_most_missed(facts), latest_closes=dict(latest),
    )


def _caught_up(now: float, snapshot: UsSnapshot) -> None:
    w = _worker
    behind = None if w.seen_latest is None or snapshot.d >= w.seen_latest else (w.behind_since or now)
    _set_worker(replace(w, fail_count=0, last_fail="", last_wait="", next_try=None, behind_since=behind))


def _us_round(stop: threading.Event, now: float) -> float:
    """第 1、2 步：讀 D；需要時重算。DB 忙碌 → not_yet；讀不到 → 失敗 20 分。"""
    w = _worker
    if w.next_try is not None and now < w.next_try:
        return w.next_try - now
    try:
        calendar = _calendar(_us_today(now))
        if not calendar:
            raise stock_db_ro.DbUnavailable("calendar_empty")
        _note_latest(calendar[-1], now)
        snapshot = _snapshot
        pool, d, covered = _choose_pool(snapshot, calendar, now)
        latest = _latest_closes()
        spy_dates = tuple(date.fromisoformat(x) for x in calendar[-SPY_DATES_KEEP:])
        current = snapshot.d.isoformat() if snapshot is not None else ""
        # 這個 D 算過了（每個 D 只算一次；rule 換了才重算同一個 D；DB 退回較舊的日期不重算）：只換最新收盤與日曆
        if snapshot is not None and (current > d or (current == d and snapshot.rule == RULE)):
            _caught_up(now, snapshot)
            if dict(snapshot.latest_closes) != latest or snapshot.spy_dates != spy_dates:
                _commit_snapshot(replace(snapshot, latest_closes=latest, spy_dates=spy_dates), now, refresh_only=True)
            _save_worker(now)
            return float(POLL_S)
        new = _recompute(stop, now, pool, d, covered, calendar, latest, spy_dates)
    except stock_db_ro.DbBusy:
        return _fail("db_busy", now, not_yet=True)
    except stock_db_ro.DbUnavailable as exc:
        logger.warning("stock picks us db unavailable kind=%s", exc.kind)
        return _fail("db_unavailable", now, retry_s=DB_RETRY_S)
    except stock_picks.RoundFailed as exc:
        if exc.kind == "stopped" or stop.is_set():
            _save_worker(now)  # 已抓的財報與請求帳留著
            return float(POLL_S)
        return _fail(exc.kind, now, blocked=exc.blocked, not_yet=exc.not_yet, retry_s=exc.retry_s)
    if stop.is_set():
        _save_worker(now)
        return float(POLL_S)
    if _commit_snapshot(new, now) == COMMIT_FAILED:
        return _fail("snapshot_write", now)
    _caught_up(now, _snapshot or new)
    _save_worker(now)
    logger.info("stock picks us refreshed d=%s picks=%d", new.d.isoformat(), len(new.picks))
    return float(POLL_S)


def _history_record(snapshot: UsSnapshot) -> dict:
    picks = [snapshot.facts_of(s) for s in snapshot.picks]
    return {
        "t": snapshot.d.isoformat(),
        "market": None if snapshot.spy is None else snapshot.spy.level,
        "picks": [{"code": f.symbol, "name": f.name, "close": f.trend.close if f.trend else None,
                   "score": us_score_of(f), "p60": f.trend.p60 if f.trend else None} for f in picks if f is not None],
        "computed_at": round(snapshot.computed_at),
        "rule": snapshot.rule,
    }


def _check_history() -> None:
    """第 3 步：快照的 D 比紀錄最後一行新就補寫（日期只往前、5 MB、0600 都在 _append_history 裡；鎖外呼叫）。"""
    snapshot = _snapshot
    if snapshot is None:
        return
    try:
        stock_picks._append_history(None, name=HISTORY_NAME, record=_history_record(snapshot))
    except Exception as exc:
        logger.error("stock picks us history failed error_type=%s", type(exc).__name__)


def _update_perf(now: float) -> None:
    """第 4 步：推薦成績每輪都更新（不管美股這輪成功與否）；結果放記憶體，回覆只讀記憶體。"""
    global _perf_result
    module = _perf_module()
    if module is None:
        return
    try:
        module.update(ai_enabled=stock_picks.AI_ENABLED, now=now)
    except Exception as exc:
        logger.error("stock picks perf update failed error_type=%s", type(exc).__name__)
    try:
        result = module.load_result()
    except Exception as exc:
        logger.error("stock picks perf load failed error_type=%s", type(exc).__name__)
        return
    with _mem_lock:
        _perf_result = result


def _tick(stop: threading.Event, now: float) -> float:
    """同步跑一輪、回下一輪要等幾秒（測試直接呼叫）。美股開關關閉時只做推薦紀錄與推薦成績。"""
    _ensure_loaded()
    _prepare_worker_side(now)
    delay = float(POLL_S)
    if ENABLED:
        try:
            delay = _us_round(stop, now)
        except Exception as exc:  # 最外層接住：記成失敗並退避，不讓執行緒停掉
            logger.error("stock picks us round crashed error_type=%s", type(exc).__name__)
            try:
                delay = _fail(f"crash_{type(exc).__name__}"[:40], now)
            except Exception:
                delay = float(stock_picks.BACKOFF_S[-1])
    if stop.is_set():
        return float(POLL_S)
    _check_history()
    _update_perf(now)
    return delay


# ── 背景執行緒 ─────────────────────────────────────────────────────────────────────
_thread: Optional[threading.Thread] = None
_stop: Optional[threading.Event] = None
_thread_lock = threading.Lock()


def _run(stop: threading.Event) -> None:
    if stop.wait(STARTUP_DELAY_S):  # 重啟時不要一起來就讀 DB、打 Yahoo
        return
    while not stop.is_set():
        try:
            delay = _tick(stop, _wall())
        except Exception as exc:  # _tick 自己會接；這裡只是最後一道
            logger.error("stock picks us tick crashed error_type=%s", type(exc).__name__)
            delay = float(stock_picks.BACKOFF_S[-1])
        # 最多等一輪（30 分鐘）：美股在長退避（壞檔 1 小時）時 _tick 會馬上返回，推薦紀錄與推薦成績照樣每輪更新
        if stop.wait(max(1.0, min(delay, float(POLL_S)))):
            return


def start_background() -> None:
    """lifespan 啟動時（經 stock_picks.start_background）呼叫；不受美股開關控制（推薦成績要照收）。"""
    global _thread, _stop
    if not stock_picks._BACKGROUND_ENABLED:
        return
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop = threading.Event()
        _thread = threading.Thread(target=_run, args=(_stop,), daemon=True, name="stock-picks-us")
        _thread.start()


def signal_stop() -> None:
    stop = _stop
    if stop is not None:
        stop.set()


def join_background(timeout: float = 2.0) -> None:
    thread = _thread
    if thread is not None:
        thread.join(timeout=timeout)


# ── 單檔即時查（回覆路徑：不開執行緒、依序、只打 query1；不碰 sqlite、不拿檔案鎖） ─────────────────
_lookup_lock = threading.Lock()  # 只保護下面的限流、快取、冷卻（記憶體）；網路請求不在鎖裡
_lookup_times: deque = deque()
_lookup_cache: "OrderedDict[str, tuple[float, object]]" = OrderedDict()
_lookup_yahoo_until = 0.0


def parse_us_chart(payload, ysym: str) -> tuple[str, Optional[dict]]:
    """Yahoo 日線 → ("ok", {kind, name, bars: {美東日期: 收盤}}) 或 (not_found／unsupported／exchange／bad, None)。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("chart"), dict):
        return "bad", None
    chart = payload["chart"]
    error = chart.get("error")
    if isinstance(error, dict) and error.get("code") == "Not Found":
        return "not_found", None
    results = chart.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return "bad", None
    result = results[0]
    meta = result.get("meta")
    if not isinstance(meta, dict) or meta.get("symbol") != ysym:
        return "bad", None
    instrument, currency, exchange = meta.get("instrumentType"), meta.get("currency"), meta.get("exchangeName")
    kind = _INSTRUMENT_KIND.get(instrument) if isinstance(instrument, str) else None
    if kind is None or currency != "USD":
        return "unsupported", None
    if not isinstance(exchange, str) or exchange not in _EXCHANGES:
        return "exchange", None
    stamps = result.get("timestamp")
    try:
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return "bad", None
    if not isinstance(stamps, list) or not isinstance(closes, list):
        return "bad", None
    bars: dict[date, float] = {}
    for stamp, close in zip(stamps, closes):
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not _price_ok(close):
            continue
        try:
            if not math.isfinite(stamp):
                continue
            day = datetime.fromtimestamp(stamp, _NY).date()
        except (OverflowError, OSError, ValueError):
            continue
        bars[day] = float(close)
    name = clean_us_name(meta.get("longName")) or clean_us_name(meta.get("shortName"))
    return "ok", {"kind": kind, "name": name, "bars": dict(sorted(bars.items()))}


def yahoo_trend(bars: Mapping[date, float], now: float, spy_dates: Sequence[date]) -> tuple[str, Optional[date], Optional[UsTrend], str]:
    """Yahoo 路徑的走勢：丟掉還沒收完的那根（美東今天 17:00 前）；最後一根比美東今天早 > 5 天 → no_trade；
    快照的日曆涵蓋到資料日時用日曆窗檢查缺日，不然只看根數；相鄰比值超出 1.45 → ⚪（jump）。
    回 (狀態, 資料日, 走勢, ⚪ 原因)。"""
    local = datetime.fromtimestamp(now, _NY)
    today = local.date()
    days = [d for d in bars if d < today or (d == today and local.hour >= LOOKUP_CLOSE_HOUR)]
    if not days:
        return "bad", None, None, ""
    data_date = max(days)
    if (today - data_date).days > LOOKUP_STALE_DAYS:
        return "no_trade", data_date, None, ""
    calendar = [d for d in spy_dates if d <= data_date]
    if calendar and calendar[-1] == data_date:
        year = [bars[d] for d in calendar[-stock_picks.YEAR_BARS:] if d in bars]
        quarter = [bars[d] for d in calendar[-stock_picks.QUARTER_BARS:] if d in bars]
    else:
        values = [bars[d] for d in sorted(days)]
        year, quarter = values[-stock_picks.YEAR_BARS:], values[-stock_picks.QUARTER_BARS:]
    if _jumpy(year):
        return "ok", data_date, None, "jump"
    trend = us_trend_of(year, quarter)
    return "ok", data_date, trend, "" if trend is not None else "short"


def _yahoo_once(path: str, params: dict, timeout: float):
    """回覆路徑的一次請求：只打 query1、單次 ≤ 3 秒；只接 FetchError（其他例外往上丟）。"""
    status, data, _ = stock_picks._get_json(_YAHOO_HOSTS[0], path, params=params, max_bytes=_YAHOO_MAX_BYTES,
                                            timeout=timeout, total_s=timeout)
    return data if status == 200 else None


def _lookup_block(now: float) -> None:
    global _lookup_yahoo_until
    with _lookup_lock:
        _lookup_yahoo_until = max(_lookup_yahoo_until, now + LOOKUP_BLOCK_S)


def _lookup_fetch(symbol: str, snapshot: Optional[UsSnapshot], give_up: float, now: float) -> tuple[object, bool]:
    """抓一檔（日線＋必要時財報）→ (UsFacts 或固定句, 可不可以快取到下一個美東 17:00)。"""
    ysym = _yahoo_symbol(symbol)
    try:
        payload = _yahoo_once(f"/v8/finance/chart/{quote(ysym, safe='')}",
                              {"range": "2y", "interval": "1d", "includePrePost": "false"},
                              max(0.5, min(LOOKUP_FETCH_S, give_up - time.monotonic())))
    except stock_picks.FetchError as exc:
        if exc.kind in ("rate_limited", "blocked"):
            _lookup_block(now)
        if exc.kind == "not_found":  # 只有 404（或 chart.error Not Found）才說認不出代號
            return TEXT_US_NOT_FOUND, False
        logger.info("stock picks us lookup failed kind=%s", exc.kind)
        return stock_picks.TEXT_LOOKUP_FAILED, False
    status, info = parse_us_chart(payload, ysym)
    if status != "ok":
        return {"not_found": TEXT_US_NOT_FOUND, "unsupported": TEXT_US_UNSUPPORTED,
                "exchange": TEXT_US_EXCHANGE}.get(status, stock_picks.TEXT_LOOKUP_FAILED), False
    trend_status, data_date, trend, gray = yahoo_trend(info["bars"], now, snapshot.spy_dates if snapshot else ())
    if trend_status == "no_trade":
        return TEXT_US_NO_TRADE, False
    if trend_status != "ok" or data_date is None:
        return stock_picks.TEXT_LOOKUP_FAILED, False
    verified = False
    if trend is not None and snapshot is not None:
        hit = snapshot.latest_closes.get(symbol)
        if hit is not None and hit[0] == data_date:
            if abs(trend.close - hit[1]) > VERIFY_TOL * hit[1]:
                trend, gray = None, "mismatch"
            else:
                verified = True
    facts = UsFacts(symbol, info["name"], info["kind"], data_date, trend, gray=gray if trend is None else "",
                    source="yahoo", verified=verified, fund=FUND_NA if info["kind"] == ETF else FUND_OK)
    if facts.kind == ETF or trend is None:
        return facts, trend is not None
    entry = _lookup_fund(symbol, give_up, now)
    if entry is None:
        return replace(facts, fund=FUND_UNAVAILABLE), False  # 只快取 10 分鐘
    return replace(facts, growth=growth_of(entry.rev, data_date), profit=profit_of(entry.ni, data_date),
                   pe=pe_of(entry.eps, entry.ni, trend.close, data_date)), True


def _lookup_fund(symbol: str, give_up: float, now: float) -> Optional[FundEntry]:
    """財報先看背景快取（新鮮就不抓）；剩下的預算 ≥ 1.5 秒才抓；抓不到時有舊的快取就用舊的。不寫進背景的快取。"""
    cached = _worker.fund.get(symbol)
    if cached is not None and now - cached.at < FUND_REFRESH_S:
        return cached
    left = give_up - time.monotonic()
    if left < LOOKUP_FUND_MIN_S:
        return cached
    ysym = _yahoo_symbol(symbol)
    try:
        payload = _yahoo_once(f"/ws/fundamentals-timeseries/v1/finance/timeseries/{quote(ysym, safe='')}",
                              _fund_params(ysym, now), min(LOOKUP_FETCH_S, left))
    except stock_picks.FetchError as exc:
        if exc.kind in ("rate_limited", "blocked"):
            _lookup_block(now)
        logger.info("stock picks us lookup fund failed kind=%s", exc.kind)
        return cached
    parsed = parse_fundamentals(payload) if payload is not None else None
    return _entry_from_parsed(parsed, now) if parsed is not None else cached


def _lookup(symbol: str, snapshot: Optional[UsSnapshot], deadline: Optional[float]):
    """即時查一檔：回 UsFacts 或固定句。快取：成功到下一個美東 17:00、其他 10 分鐘、最多 128 筆；
    全域每 10 分鐘 4 次；美股自己的 429 冷卻中 → 查詢有點多。"""
    now_m, now = time.monotonic(), _wall()
    with _lookup_lock:
        hit = _lookup_cache.get(symbol)
        if hit is not None and hit[0] > now:
            _lookup_cache.move_to_end(symbol)
            return hit[1]
        remaining = (deadline - now_m) if deadline is not None else 30.0
        if remaining < LOOKUP_MIN_REMAINING_S:
            return stock_picks.TEXT_NO_TIME
        if _lookup_yahoo_until > now:
            return stock_picks.TEXT_BUSY
        while _lookup_times and now_m - _lookup_times[0] > LOOKUP_WINDOW_S:
            _lookup_times.popleft()
        if len(_lookup_times) >= LOOKUP_LIMIT:
            return stock_picks.TEXT_BUSY
        _lookup_times.append(now_m)
    give_up = now_m + min(LOOKUP_TOTAL_S, remaining - LOOKUP_RESERVE_S)
    result, long_ok = _lookup_fetch(symbol, snapshot, give_up, now)
    with _lookup_lock:
        _lookup_cache[symbol] = (_next_us_close(now) if long_ok else now + LOOKUP_FAIL_TTL_S, result)
        _lookup_cache.move_to_end(symbol)
        while len(_lookup_cache) > LOOKUP_CACHE_MAX:
            _lookup_cache.popitem(last=False)
    return result


# ── 回覆（hook 呼叫；只讀記憶體） ───────────────────────────────────────────────────────
def handle_symbol(symbol, *, deadline: Optional[float] = None) -> stock_picks.Reply:
    """美股單檔：開關關 → 舊句；池裡、快照有更新、不是 ⚪ → 直接出卡；其他即時查。卡片沒過自我檢查 → 固定句。"""
    if not ENABLED:
        return stock_picks.Reply(text=stock_picks.TEXT_US)
    if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
        return stock_picks.Reply(text=TEXT_US_NOT_FOUND)
    _ensure_loaded()
    snapshot, worker, now = _snapshot, _worker, _wall()
    fresh = snapshot is not None and not _not_updated(snapshot, worker, now)
    facts = snapshot.facts_of(symbol) if fresh else None
    if facts is None or facts.trend is None:  # 池裡是 ⚪ 的也改走即時查
        found = _lookup(symbol, snapshot, deadline)
        if isinstance(found, str):
            return stock_picks.Reply(text=found)
        facts = found
    market, market_date = (snapshot.spy, snapshot.d) if fresh else (None, None)
    bubble = us_detail_bubble(facts, market=market, market_date=market_date)
    alt = us_detail_alt_text(facts)
    if not _card_ok(bubble, alt):
        return stock_picks.Reply(text=stock_picks.TEXT_EVAL_CARD_FAILED)
    return stock_picks.Reply(flex=bubble, alt_text=alt, fallback_text=stock_picks.TEXT_EVAL_CARD_FAILED)


def _us_section(now: float) -> tuple[list[dict], str, Optional[date]]:
    """美股推薦區：(卡片, altText 用的字, 資料日)。"""
    snapshot, worker = _snapshot, _worker
    if snapshot is None:
        stuck = worker.fail_count >= STUCK_FAIL_COUNT
        return [us_status_bubble(TEXT_US_STUCK if stuck else TEXT_US_COMPUTING)], ("暫時算不出來" if stuck else "計算中"), None
    if _not_updated(snapshot, worker, now):
        return [us_not_updated_bubble(snapshot.d)], "資料暫時沒有更新", snapshot.d
    picks = [f for f in (snapshot.facts_of(s) for s in snapshot.picks) if f is not None and f.trend is not None]
    if picks:
        bubbles = [us_pick_bubble(f, i + 1, len(picks), snapshot.spy, lead_note=(i == 0)) for i, f in enumerate(picks)]
        return bubbles, "、".join(f.symbol for f in picks), snapshot.d
    return [us_no_pick_bubble(snapshot.d, snapshot.spy, snapshot.missed)], "這次沒有符合條件的", snapshot.d


def _perf_card() -> Optional[tuple[dict, str]]:
    """成績卡與它的 altText（成績模組載不到或出錯回 None）。只讀記憶體裡的結果。"""
    module = _perf_module()
    if module is None:
        return None
    try:
        result = _perf_result
        return module.perf_bubble(result), module.perf_alt_text(result)
    except Exception as exc:
        logger.error("stock picks perf card failed error_type=%s", type(exc).__name__)
        return None


def extend_picks_reply(reply: stock_picks.Reply) -> stock_picks.Reply:
    """台股推薦卡組後面接美股推薦卡＋成績卡。不改傳進來的卡組（複製後再接）；每張新卡與新 altText 先自己驗；
    整組 ≤ 25 KB（超過先拿掉成績卡、再超過就不接）；任何問題回原樣的 reply。

    還沒有美股快照（剛上線、第一輪還沒算出來）就不接美股卡，還沒有成績結果就不接成績卡（2026-10-28 整合）：
    台股卡組照原樣，不在後面多一張「計算中」；要看美股的狀態打「/股票 美股」。"""
    if not ENABLED:
        return reply
    try:
        flex = reply.flex
        if not isinstance(flex, dict) or flex.get("type") != "carousel" or not isinstance(flex.get("contents"), list):
            return reply
        _ensure_loaded()
        if _snapshot is None and _perf_result is None:
            return reply
        cards, words = [], None
        if _snapshot is not None:
            cards, words, _ = _us_section(_wall())
            if not all(_card_ok(card) for card in cards):
                return reply
        perf = _perf_card() if _perf_result is not None else None
        extra = [perf[0]] if perf is not None and _card_ok(perf[0]) else []
        if not cards and not extra:
            return reply
        alt = reply.alt_text
        if isinstance(alt, str) and words:
            joined = f"{alt}｜美股：{words}"
            if len(joined) <= stock_card_check.ALT_TEXT_MAX and _card_ok(stock_picks.carousel(cards), joined):
                alt = joined
        for tail in ((cards + extra), cards):
            if len(flex["contents"]) + len(tail) > MAX_BUBBLES:
                continue
            new_flex = copy.deepcopy(flex)
            new_flex["contents"].extend(copy.deepcopy(tail))
            if _card_bytes(new_flex) <= MAX_CARD_BYTES:
                return replace(reply, flex=new_flex, alt_text=alt)
        logger.warning("stock picks us cards skipped reason=too_big")
        return reply
    except Exception as exc:
        logger.error("stock picks us extend failed error_type=%s", type(exc).__name__)
        return reply


def us_reply() -> stock_picks.Reply:
    """「/股票 美股」：美股推薦卡＋成績卡。"""
    if not ENABLED:
        return stock_picks.Reply(text=TEXT_PHASE3_OFF)
    _ensure_loaded()
    cards, words, day = _us_section(_wall())
    if not all(_card_ok(card) for card in cards):
        return stock_picks.Reply(text=stock_picks.TEXT_US_CARDS_FAILED)
    perf = _perf_card()
    extra = [perf[0]] if perf is not None and _card_ok(perf[0]) else []
    alt = f"{stock_picks.PREFIX}只供參考｜美股：{words}" + (f"｜資料到 {_us_day(day)}" if day is not None else "")
    carousel = stock_picks.carousel(cards + extra)
    if _card_bytes(carousel) > MAX_CARD_BYTES:
        carousel = stock_picks.carousel(cards)
    if not _card_ok(carousel, alt):
        return stock_picks.Reply(text=stock_picks.TEXT_US_CARDS_FAILED)
    return stock_picks.Reply(flex=carousel, alt_text=alt, fallback_text=stock_picks.TEXT_US_CARDS_FAILED)


def perf_reply() -> stock_picks.Reply:
    """「/股票 成績」：成績卡（只讀記憶體裡的結果）。"""
    if not ENABLED:
        return stock_picks.Reply(text=TEXT_PHASE3_OFF)
    _ensure_loaded()
    perf = _perf_card()
    if perf is None or not _card_ok(perf[0], perf[1]):
        return stock_picks.Reply(text=stock_picks.TEXT_US_CARDS_FAILED)
    return stock_picks.Reply(flex=perf[0], alt_text=perf[1], fallback_text=stock_picks.TEXT_US_CARDS_FAILED)


# ── 狀態（每日維護、部署檢查） ─────────────────────────────────────────────────────────
def enabled_now() -> bool:
    """狀態用的開關：環境變數（有設就用）→ bot 目錄的 .env 那一行 → 預設開（每日維護的程序不會載入 .env）。"""
    value = os.environ.get(FLAG_ENV)
    return stock_picks._ai_flag(value if value is not None else stock_picks._env_file_value(FLAG_ENV))


def status_summary(now: Optional[float] = None) -> dict:
    """不開 DB、不寫檔（這個程序還沒載入過狀態時只讀檔，不改執行期狀態）。"""
    now = _wall() if now is None else now
    if _loaded:
        snapshot, w = _snapshot, _worker
    else:
        snapshot, w = _load_snapshot_file(now), _load_worker_file(now)
    if not (ENABLED and enabled_now()):
        status = "disabled"
    elif snapshot is None:
        status = "no_snapshot"
    elif _not_updated(snapshot, w, now):
        status = "not_updated"
    else:
        status = "ok"
    perf = None
    module = _perf_module()
    if module is not None:
        try:
            perf = module.summary()
        except Exception as exc:
            logger.warning("stock picks perf summary failed error_type=%s", type(exc).__name__)
    return {
        "status": status,
        "data_date": None if snapshot is None else snapshot.d.isoformat(),
        "seen_date": None if w.seen_latest is None else w.seen_latest.isoformat(),
        "covered": None if snapshot is None else snapshot.covered,
        "fail_count": w.fail_count,
        "last_fail": w.last_fail,
        "waiting": w.last_wait or None,
        "backoff_minutes": None if w.next_try is None or w.next_try <= now else round((w.next_try - now) / 60),
        "hours_since_check": None if w.last_ok_check is None else round((now - w.last_ok_check) / 3600, 1),
        "perf_date": perf.get("perf_date") if isinstance(perf, dict) else None,
        "fund_count_today": w.fund_count if w.fund_day == _tw_day(now) else 0,
        "cards_rejected": _cards_rejected,
        "perf": perf,
    }


def reset_for_tests() -> None:
    """conftest 每個測試呼叫：重讀開關、清掉記憶體狀態、快取、限流、冷卻（狀態檔目錄已經是 tmp）。"""
    global ENABLED, _snapshot, _worker, _loaded, _worker_side_ready, _perf_result, _cards_rejected
    global _thread, _stop, _lookup_yahoo_until
    signal_stop()
    ENABLED = stock_picks._ai_flag(os.environ.get(FLAG_ENV))
    with _mem_lock:
        _snapshot, _worker, _loaded, _perf_result = None, UsWorker(), False, None
    _worker_side_ready = False
    _cards_rejected = 0
    with _thread_lock:
        _thread, _stop = None, None
    with _lookup_lock:
        _lookup_times.clear()
        _lookup_cache.clear()
        _lookup_yahoo_until = 0.0
