"""市場盤勢（/盤勢，2026-10-17）的純計算：job 與 bot 共用的公式、門檻、交易日曆、新鮮度、固定句與快照欄位表。

Andrew 2026-10-17 原話：「美股/台股的籌碼/基本面/技術面/消息面/期權市場的下注（最大未平倉價位等等，
gamma squzze…)/持股跌破20MA,60MA,120MA,240MA的家數/成交量(微縮or爆大量…我要知道，不能只有量，
我要知道當前量是多還是少）/持股上漲家數與下跌家數比例/13F相關資訊/市場上槓桿的程度/外資動向」
「我想要知道目前市場的狀況」。10/17 的選擇：按了才回、一次全做、只描述狀態不寫買賣、顏色只用藍橘灰、
每一面各自標狀態、不算加權總分。

這裡只放「程式算」的部分（規格：設計文件 PLAN.md v6.2 的 §4、§5、
§18、§19；介面：INTERFACES.md §4、§5、§8）：
- 均線、距離、RSI14（Wilder）、240 日高點回撤；寬度（N+5 個預期交易日都有收盤、未碰到隔離區間才算有效；
  5 日前用兩天都有效的同一批、交集 <80% 不比；狀態由覆蓋率決定）；漲跌家數；240 日新高新低。
- 量能（倍數＝當天量／前 20 個交易日平均、近 250 日百分位、五級標籤、價量四種組合、結算日／早收日旗標）。
- 期權（OI 有效／gamma 有效兩個集合、IV=0 的價內合約改用同履約價價外 IV、月選規則、牆、最大痛點、
  PCR、假設部位的 GEX(S) 曲線與變號點、覆蓋率）；台指選擇權的牆與最大痛點。
- 台股還原除權息（只乘在除權息日之前）、本益比（市值 ÷ 近四季普通股淨利）、股別合併、13F 摘要。
- 矛盾（7 條、同資料日才比）與一句話（固定句型）。
- 交易日曆（休市表由呼叫端傳入）與 `stale_after`（§5 類型表＋§18.1、§18.9 的修正）。
- 外部字串清洗 `clean_external`（§19.1）、`ParseError`（sources 共用）、快照列 id 與 values 欄位表 `ROW_IDS`。

約定：
- 純函式：不讀寫檔案、不連網、不 import 專案模組；所有跟時間有關的函式都要呼叫端傳 `now`。
- numpy 只在算 GEX 時才 import（§19.6：bot 端 market_dashboard 會 import 本模組拿 ROW_IDS，
  它不可以載入 numpy／pandas／matplotlib）。
- sources 的 dataclass（OptionContract、TxoRow、TwBar、ExRight、Quarter、Holding…）用屬性名讀
  （鴨子型別），本模組不 import sources，避免循環相依。
"""

from __future__ import annotations

import calendar as _calendar
import html
import math
import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, is_dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from functools import cached_property
from typing import Any, Callable, NamedTuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
TPE = ZoneInfo("Asia/Taipei")
UTC = timezone.utc

# ── 門檻（都是起始值，跑一陣子再調） ───────────────────────────────────────────
MA_WINDOWS = (20, 60, 120, 240)
BREADTH_EXTRA = 5  # 寬度的窗口＝N+5 個預期交易日（含 5 日前那天的均線）
COVERAGE_OK = 0.80  # 覆蓋率 ≥80% 才算正常、才比 5 日前
RSI_PERIOD = 14
DRAWDOWN_WINDOW = 240
HIGHLOW_WINDOW = 240
VOL_AVG_DAYS = 20
VOL_PCT_DAYS = 250
VOL_STRONG_SHRINK = 0.5
VOL_SHRINK = 0.8
VOL_EXPAND = 1.5  # 沿用出場警示的放量門檻
VOL_SURGE = 2.5
PV_SPLIT = 1.0  # 價量組合：倍數 ≥1.0 算量增

OPTION_ROOTS = ("QQQ", "SPY", "SOXX")
OPTION_SYMBOL_RE = re.compile(r"^(QQQ|SPY|SOXX)(\d{6})([CP])(\d{8})$")
OPTION_WINDOW_DAYS = 45  # 牆、PCR、GEX 只看 D 之後 45 天內到期
WALL_MIN_CONTRACTS = 100
NEAR_BAND = 0.10  # 「現價 ±10% 兩邊都有」與覆蓋率的範圍
WALL_BAND = 0.15  # 買權牆 [S, 1.15S]、賣權牆 [0.85S, S]（§18.13）
MONTHLY_MIN_CONTRACTS = 40
MONTHLY_MIN_STRIKES = 15
GEX_GRID_LOW, GEX_GRID_HIGH, GEX_GRID_POINTS = 0.85, 1.15, 61  # 每 0.5% 一點
GEX_NEAR_ZERO = 0.02
GEX_COVERAGE_OK = 0.80
DEFAULT_RATE = 0.04  # 3M 公債抓不到時的假設利率（卡上要標「假設利率」）
CONTRACT_MULTIPLIER = 100

STALE_SESSIONS = 4
OPTIONS_GRACE = timedelta(hours=3)
CBOE_MAX_AGE = timedelta(hours=12)
BTC_TTL = timedelta(hours=6)
PREDICTION_TTL = timedelta(hours=6)
NEWS_TTL = timedelta(hours=36)
TRUMP_TTL = timedelta(hours=48)
EVENT_GRACE = timedelta(hours=2)
# 事件清單一定要有的種類，依序保證（2026-10-18：財報季時二十幾檔前十大的財報日會把 FOMC 擠出清單）
EVENT_MUST_KEEP = ("fomc", "cpi", "nfp")
# 同一時刻的事件：必留種類排前面（順序同 EVENT_MUST_KEEP），其他種類排後；publish 與 bot 的排序共用這份
EVENT_PRIORITY = {kind: index for index, kind in enumerate(EVENT_MUST_KEEP)}
EVENT_PRIORITY_OTHER = len(EVENT_MUST_KEEP)


def keep_events(items: Sequence[Any], limit: int, kind_of: Callable[[Any], str | None]) -> list[Any]:
    """items 已依時間排：取前 limit 筆，但 EVENT_MUST_KEEP 各種類最近的一筆一定在內——放不下就擠掉最晚的
    一筆其他事件；回傳仍依原本的時間順序。"""
    if limit <= 0:
        return []
    chosen = list(range(min(limit, len(items))))
    for kind in EVENT_MUST_KEEP:
        if any(kind_of(items[i]) == kind for i in chosen):
            continue
        extra = next((i for i in range(limit, len(items)) if kind_of(items[i]) == kind), None)
        victims = [i for i in chosen if kind_of(items[i]) not in EVENT_MUST_KEEP]
        if extra is not None and victims:
            chosen.remove(max(victims))
            chosen.append(extra)
    return [items[i] for i in sorted(chosen)]
CFTC_DAYS = 16
MONTH_END_DAYS = 62  # FINRA、台股月營收：以月底為錨
MONEYDJ_DAYS = 62
F13_DAYS = 155
FUND_DAYS = 160
FUND_NOT_UPDATED_DAYS = 151  # 季度結束＋下一季（約 91 天）＋申報期限（10-K 60 天）
CALENDAR_FALLBACK_DAYS = 6  # 日曆表外的退路（§18.3）
US_CLOSE_DELAY = timedelta(minutes=30)  # D＝美東已過收盤＋30 分鐘的預期交易日
TW_CLOSE_DELAY = timedelta(hours=1)  # T＝已過 14:30 的預期交易日

# 外部字串長度上限（§10）
MAX_TITLE, MAX_NAME, MAX_TRUMP_EXCERPT = 40, 24, 100

# 顏色只用藍、橘、灰（/股票 的紅漲綠跌顧慮）
COLOR_BLUE, COLOR_ORANGE, COLOR_GREY, COLOR_GREY_LIGHT = "#1E5AA8", "#B7791F", "#555555", "#666666"
COLORS = (COLOR_BLUE, COLOR_ORANGE, COLOR_GREY, COLOR_GREY_LIGHT)

# 量能標籤與旗標
VOL_LABEL_STRONG_SHRINK, VOL_LABEL_SHRINK, VOL_LABEL_NORMAL = "明顯量縮", "量縮", "正常"
VOL_LABEL_EXPAND, VOL_LABEL_SURGE = "放量", "爆量"
VOL_LABELS = (VOL_LABEL_STRONG_SHRINK, VOL_LABEL_SHRINK, VOL_LABEL_NORMAL, VOL_LABEL_EXPAND, VOL_LABEL_SURGE)
PV_LABELS = ("價漲量增", "價漲量縮", "價跌量增", "價跌量縮")
FLAG_SETTLEMENT, FLAG_EARLY_CLOSE = "結算日", "早收日"

# 快照列的狀態與期權狀態
STATUS_OK, STATUS_LOW_COVERAGE, STATUS_INSUFFICIENT = "ok", "low_coverage", "insufficient"
ROW_STATES = ("ok", "unknown_period", "never")
SNAPSHOT_SCHEMA = 1

# ── 固定句（卡片與 altText 只用這些＋程式算的數字與日期；不寫年份，避開 validator 的年份規則） ─────
TEXT_DISCLAIMER = "程式用公開資料算的，只描述狀態，不是買賣建議"  # 禁用字檢查的精確例外
TEXT_PREPARING = "盤勢：資料準備中，請過幾分鐘再按一次"
TEXT_BOOTSTRAP = "歷史資料補到 {done}/{total} 檔"
TEXT_DISABLED = "盤勢：暫停使用"
TEXT_STALE = "過期（資料日 {md}）"
TEXT_MISSING = "暫缺"
TEXT_UNKNOWN_PERIOD = "期間未標"
TEXT_INSUFFICIENT = "資料不足"
TEXT_ACCUMULATING = "資料累積中（{n}/5）"
TEXT_CALENDAR_PENDING = "日曆待更新"
TEXT_NOT_UPDATED = "未更新"
TEXT_MEMBER_LIST_DATE = "成分清單 {md}"
TEXT_MM_TRUMP = "MM 川普壓力指數：抓不到（要登入）"
TEXT_US_NO_DIVIDEND = "美股收盤已調整分割、配息不還原"
TEXT_TW_ADJUSTED = "台股用還原除權息收盤"
TEXT_VOL_SOURCE_DIFF = "來源不同，暫不判斷"
TEXT_OI_DATE = "未平倉推定前一交易日結算（來源未標日期）"
TEXT_CHAIN_RANGE = "收到的有效合約範圍"
TEXT_WALL_RANGE = "買權牆看現價到 +15%、賣權牆看 −15% 到現價"
TEXT_WALL_DEF = "牆＝未平倉合約最集中的履約價"
TEXT_MAX_PAIN = "最大痛點＝讓選擇權買方整體虧損最多的結算價（不是目標價）"
TEXT_RATE_ASSUMED = "假設利率 4%"
TEXT_GEX_NEGATIVE = "假設部位下，現價附近 gamma 為負（漲跌較容易被放大的組合）"
TEXT_GEX_POSITIVE = "假設部位下，現價附近 gamma 為正（漲跌較容易被緩和的組合）"
TEXT_GEX_NEAR_ZERO = "假設部位下，現價附近 gamma 接近 0、不確定"
TEXT_GEX_NO_CROSSING = "±15% 內沒有變號"
TEXT_GEX_CROSSINGS = "變號點（價格由低往高）"
TEXT_COVERAGE_LOWER = "（下限）"
TEXT_SEASON_NOTE = "歷史統計，不是今年的預測；S&P 的統計不能直接當 SOXX 的勝率"
TEXT_SEASON_SP_BASIS = "月均價口徑"
TEXT_SEASON_TW_BASIS = "月底收盤口徑"
TEXT_ELECTION_NOTE = "兩平台結算規則不同，不是民調、不是保證"
TEXT_POLL_NONE = "民調：未提供"
TEXT_KALSHI_TIME = "成交時間未知"
TEXT_TRUMP_NOTE = "第三方存檔，未經核實是否已發生、不代表政策已執行"
TEXT_TRUMP_NONE = "近 48 小時沒有符合主題的貼文"
TEXT_AT_LEAST = "（至少）"
TEXT_13F_LAG = "13F 季末後 45 天才申報完"
TEXT_13F_PERIOD = "彙總期間來源未標；前十大持有人申報期間以 {md} 為主（混合）"
TEXT_13F_PERIOD_SINGLE = "彙總期間來源未標；前十大持有人申報期間 {md}"
TEXT_13F_SUMMARY = "前十大成分股中，{more} 檔的加碼機構多於減碼機構（{missing} 檔資料暫缺）"
TEXT_CFTC_NOTE = "CFTC 是週資料、槓桿基金不是「外資」"
TEXT_PE_NONPOSITIVE = "近四季淨利合計 ≤0，不計本益比"
TEXT_PE_MISSING = "近四季資料不齊，不計本益比"
TEXT_PE_NO_CAP = "市值暫缺，不計本益比"
TEXT_PE_STALE = "資料過期，不計本益比"
TEXT_SPY_WEIGHT = "依 S&P 500 權重（SPY 每日持股 {md}）"
TEXT_NO_US_SYMBOL = "另有 {n} 檔無美國代號（合計 {pct}%）"

# 程式自己句子的禁用字（§6）；免責句是精確例外；13F 的「加碼／減碼」描述機構，允許。
FORBIDDEN_WORDS = ("建議", "進場", "出場", "停損", "抄底", "逢低", "布局", "可以買", "該賣", "該買", "買點", "賣點")

# 量能的白話（一句話、卡片列都用這一份；VOL_LABELS 是資料值，這裡是顯示的字）
VOL_PHRASE = {
    VOL_LABEL_STRONG_SHRINK: "量明顯偏少",
    VOL_LABEL_SHRINK: "量偏少",
    VOL_LABEL_NORMAL: "量正常",
    VOL_LABEL_EXPAND: "量偏多",
    VOL_LABEL_SURGE: "爆量",
}


# ── 共用錯誤 ────────────────────────────────────────────────────────────────
# vendor（2026-10-23 審查 r7b F2）：Nasdaq 回 data: null 而且 status.rCode 不是 200＝來源自己報錯，和「沒有資料」分開記
PARSE_ERROR_KINDS = ("format", "missing_field", "empty", "too_few", "date", "conflict", "vendor")


class ParseError(ValueError):
    """sources 的解析錯誤；`kind` 是列舉值（outcome 只記 kind，detail 只放程式自己的字，不放來源內容）。"""

    def __init__(self, kind: str, detail: str = "") -> None:
        if kind not in PARSE_ERROR_KINDS:
            raise ValueError(f"unknown ParseError kind: {kind!r}")
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}" if detail else kind)

    def __reduce__(self):  # 讓子程序間傳遞時 kind 不會遺失
        return (type(self), (self.kind, self.detail))


# ── 小工具 ──────────────────────────────────────────────────────────────────
def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _pos(value: Any) -> bool:
    return _is_num(value) and value > 0


def _nonneg_int(value: Any) -> int | None:
    """OI 這類「≥0 的整數」：int，或剛好是整數的 float（CBOE 的 88.0）；bool、負數、小數 → None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """dataclass／namedtuple 用屬性、dict 用鍵（sources 的型別在這裡只認欄位名）。"""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _plain_date(value: Any) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError("expected a datetime.date (not datetime)")
    return value


def _epoch(value: Any) -> float:
    """aware datetime 或 epoch 秒 → epoch 秒；naive datetime 不收（時區不明）。"""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime")
        return value.timestamp()
    if _is_num(value):
        return float(value)
    raise TypeError("expected an aware datetime or epoch seconds")


def _midnight_tpe(day: date) -> float:
    return datetime.combine(day, time(0, 0), tzinfo=TPE).timestamp()


def _month_end(day: date) -> date:
    return date(day.year, day.month, _calendar.monthrange(day.year, day.month)[1])


def fmt_md(day: date) -> str:
    """卡上的日期寫法「10/16」（固定句不寫年份，避開 validator 的年份規則）。"""
    return f"{day.month}/{day.day}"


# ── 外部字串清洗（§19.1） ─────────────────────────────────────────────────────
_STRIP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# 換行、tab、U+2028／2029 換成空白再刪，免得前後兩個字黏在一起
_SPACE_LIKE = frozenset("\t\n\r\x0b\x0c\x1c\x1d\x1e\x1f\x85\u2028\u2029")
_TAG_RE = re.compile(r"</?[A-Za-z!][^<>]*>")
_URL_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:[a-z][a-z0-9+.\-]*://|www\.|[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,}/)"
)
# 沒有斜線的裸網域（「AMAZON.COM INC」「請上 example.com」）刻意不擋：持股名稱要留著；Flex 的 text 與
# card_pages（html.escape、不加連結）都不會把它變成可點的連結（2026-10-18 核對）。
_EMAIL_RE = re.compile(r"(?i)[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)09\d{2}[-\s.]?\d{3}[-\s.]?\d{3}(?!\d)")
_FULLWIDTH_SPACE = "　"


def _drop_bad_chars(text: str) -> str:
    out: list[str] = []
    for ch in text:
        if ch in _SPACE_LIKE:
            out.append(" ")
        elif unicodedata.category(ch) not in _STRIP_CATEGORIES:
            out.append(ch)
    return "".join(out)


def clean_external(text: Any, max_len: int) -> str | None:
    """外部字串（新聞標題、川普摘錄、持股名稱、預測市場標題）入庫前與組卡前都用這個；None＝整則丟掉。

    順序照 §19.1：NFKC → 刪 Cc／Cf／Cs／Co／Cn／Zl／Zp → 去 HTML → 含網址／email／09 手機號整則丟掉
    → 「@」「＠」換全形空白 → 截長度。email 要在換掉「@」之前查才查得到，所以檢查放在換字之前（結果相同）；
    HTML 實體還原後可能又冒出控制字元（例 &#x202E;），所以還原後再洗一次。
    """
    if isinstance(max_len, bool) or not isinstance(max_len, int) or max_len <= 0:
        raise ValueError("max_len must be a positive int")
    if not isinstance(text, str):
        return None
    # 先截長（2026-10-18 安全審查 I1）：網址／email 的正規式最壞是 O(n²)，一個幾十萬字的欄位就能讓 collect 卡到被 kill；
    # 輸出只取前 max_len 字，往後看 8 倍已足夠判斷（NFKC 可能變長，所以之後再截一次）
    window = max(8 * max_len, 512)
    cleaned = _drop_bad_chars(unicodedata.normalize("NFKC", text[:window]))[:window]
    cleaned = html.unescape(_TAG_RE.sub(" ", cleaned))
    cleaned = _drop_bad_chars(unicodedata.normalize("NFKC", cleaned))[:window]
    # 編碼過的標籤（&lt;b&gt;、&#xff1c;b&#xff1e;）還原、NFKC 之後才變成 <b>：再去一次（2026-10-18）
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned).strip()
    if not cleaned:
        return None
    # 便宜的預檢：網址一定有「/」或「www.」，email 一定有「@」（NFKC 後全形＠已是 @）
    has_url = ("/" in cleaned or "www." in cleaned.lower()) and _URL_RE.search(cleaned)
    if has_url or ("@" in cleaned and _EMAIL_RE.search(cleaned)) or _PHONE_RE.search(cleaned):
        return None
    cleaned = cleaned.replace("@", _FULLWIDTH_SPACE).replace("＠", _FULLWIDTH_SPACE).strip()
    if len(cleaned) > max_len:
        cleaned = _truncate(cleaned, max_len)
    return cleaned.strip() or None


_ELLIPSIS = "..."  # ASCII：「…」經 NFKC 會變成「...」，入庫、publish、卡片各洗一次長度會再被切
_TRAILING_PUNCT = " ,.;:!?、，。；：！？+-−–—|｜"


def _run_char(ch: str) -> bool:
    """數字、英文、小數點、千分位、%：切在這種字的中間會把「1144點」切成「11」、把名字切一半。"""
    return ch.isascii() and (ch.isalnum() or ch in ".,%")


# 一個數字（正負號、千分位、小數、%）：截斷時整個留或整個丟，切一半就變成另一個數字（2026-10-23 審查 r7 #1、#2）。
# 千分位一定是逗號＋3 位數：「1,234,567」是一個數字，「2330,2317」是兩個
_NUMBER_RE = re.compile(r"[+\-−]?(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.\d+)?%?")


def number_safe_cut(text: str, cut: int) -> int:
    """切點落在某個數字中間就退到那個數字前面；沒切到數字就原樣回傳。"""
    for match in _NUMBER_RE.finditer(text):
        if match.start() >= cut:
            break
        if cut < match.end():
            return match.start()
    return cut


def _truncate(text: str, max_len: int) -> str:
    """截到 max_len 以內並補「...」（2026-10-18 審查 r6c C1：硬切會把數字切成另一個數字、沒有標記）。

    切點落在數字／英文的中間就往回退到那一串的前面。整段都是英數、退光了的話，英文可以從中間切，數字不行
    （2026-10-23 審查 r7 #1：原本退光就硬切，「2891」變「28」、長串數字切出像手機號碼的一段，再洗一次就整則不見）。
    只剩一個切不開的數字 → 回空字串（整則丟掉）。結果長度 ≤ max_len，再洗一次不會變（冪等）。"""
    if max_len <= len(_ELLIPSIS):
        return text[: number_safe_cut(text, max_len)]
    keep = max_len - len(_ELLIPSIS)
    cut = keep
    if _run_char(text[keep]) and _run_char(text[keep - 1]):
        while cut and _run_char(text[cut - 1]):
            cut -= 1
        if not cut:
            cut = number_safe_cut(text, keep)
    head = text[:cut].rstrip(_TRAILING_PUNCT)
    return head + _ELLIPSIS if head else ""


# ── 交易日曆 ────────────────────────────────────────────────────────────────
# NYSE 官方表：https://www.nyse.com/markets/hours-calendars（2026-10-17 台北時間抄寫；
# 原文另存在開發紀錄）。
NYSE_HOLIDAYS = frozenset(
    {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
        date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
        date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
    }
)
NYSE_EARLY_CLOSES = frozenset({date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)})
NYSE_TABLE_FROM, NYSE_TABLE_TO = date(2026, 1, 1), date(2027, 12, 31)
_MAX_GAP_DAYS = 15  # 連假最長也不會超過；找不到就當日曆未知


@dataclass(frozen=True)
class TradingCalendar:
    """一個市場的預期交易日。表內：平日且不在休市表；表之前：用實際有成交的日期（observed）；其他＝未知。"""

    market: str  # "us" / "tw"
    tz: ZoneInfo
    close: time
    holidays: frozenset[date]
    table_from: date
    table_to: date
    early_closes: frozenset[date] = frozenset()
    early_close: time | None = None
    extra_sessions: frozenset[date] = frozenset()  # 週末照常開市（表內）
    observed: frozenset[date] = frozenset()  # 表開始之前，實際有成交的日期（SPY／FinMind TAIEX）
    closed_overrides: frozenset[date] = frozenset()  # 臨時休市（有獨立證據才寫入，§18.3）

    @cached_property
    def _observed_range(self) -> tuple[date, date] | None:
        return (min(self.observed), max(self.observed)) if self.observed else None

    def is_session(self, day: date) -> bool | None:
        """True／False；表外又沒有觀察資料 → None（未知，不猜）。"""
        if day in self.closed_overrides:
            return False
        if self.table_from <= day <= self.table_to:
            if day in self.extra_sessions:
                return True
            return day.weekday() < 5 and day not in self.holidays
        span = self._observed_range
        if day < self.table_from and span is not None and span[0] <= day <= span[1]:
            return day in self.observed
        return None

    def _step(self, day: date, direction: int) -> date | None:
        for offset in range(1, _MAX_GAP_DAYS + 1):
            candidate = day + timedelta(days=direction * offset)
            state = self.is_session(candidate)
            if state is None:
                return None
            if state:
                return candidate
        return None

    def next_session(self, day: date) -> date | None:
        return self._step(day, 1)

    def prev_session(self, day: date) -> date | None:
        return self._step(day, -1)

    def add_sessions(self, day: date, count: int) -> date | None:
        """往後（count>0）或往前（count<0）數第 |count| 個交易日；碰到未知 → None。"""
        current: date | None = day
        for _ in range(abs(count)):
            current = self._step(current, 1 if count > 0 else -1)
            if current is None:
                return None
        return current

    def sessions_ending(self, day: date, count: int) -> list[date]:
        """≤ day 的最近 count 個交易日（由舊到新）；碰到未知就停，所以可能不足 count 個。"""
        state = self.is_session(day)
        if state is None:
            return []
        out: list[date] = []
        current: date | None = day if state else self.prev_session(day)
        while current is not None and len(out) < count:
            out.append(current)
            current = self.prev_session(current)
        out.reverse()
        return out

    def sessions_between(self, start: date, end: date) -> list[date] | None:
        """[start, end] 內的交易日；中間有未知 → None。"""
        out: list[date] = []
        day = start
        while day <= end:
            state = self.is_session(day)
            if state is None:
                return None
            if state:
                out.append(day)
            day += timedelta(days=1)
        return out

    def close_at(self, day: date) -> datetime:
        local = self.early_close if (day in self.early_closes and self.early_close) else self.close
        return datetime.combine(day, local, tzinfo=self.tz)

    def last_complete_session(self, now: datetime | float, delay: timedelta) -> date | None:
        """最近一個「當地已過收盤＋delay」的預期交易日（美股 D、台股 T）；日曆未知 → None。"""
        local_now = datetime.fromtimestamp(_epoch(now), tz=self.tz)
        today = local_now.date()
        state = self.is_session(today)
        if state is None:
            return None
        if state and local_now >= self.close_at(today) + delay:
            return today
        return self.prev_session(today)


def nyse_calendar(
    *,
    holidays: Collection[date] = NYSE_HOLIDAYS,
    early_closes: Collection[date] = NYSE_EARLY_CLOSES,
    table_from: date = NYSE_TABLE_FROM,
    table_to: date = NYSE_TABLE_TO,
    extra_sessions: Iterable[date] = (),
    observed: Iterable[date] = (),
    closed_overrides: Iterable[date] = (),
) -> TradingCalendar:
    return TradingCalendar(
        market="us",
        tz=ET,
        close=time(16, 0),
        early_close=time(13, 0),
        holidays=frozenset(holidays),
        early_closes=frozenset(early_closes),
        table_from=table_from,
        table_to=table_to,
        extra_sessions=frozenset(extra_sessions),
        observed=frozenset(observed),
        closed_overrides=frozenset(closed_overrides),
    )


def twse_calendar(
    *,
    holidays: Collection[date],
    table_from: date,
    table_to: date,
    extra_sessions: Iterable[date] = (),
    observed: Iterable[date] = (),
    closed_overrides: Iterable[date] = (),
) -> TradingCalendar:
    """holidays＝休市表裡「休市」與「市場無交易，僅辦理結算交割」的平日（§18.4 的分類由 sources 做）。"""
    return TradingCalendar(
        market="tw",
        tz=TPE,
        close=time(13, 30),
        holidays=frozenset(holidays),
        table_from=table_from,
        table_to=table_to,
        extra_sessions=frozenset(extra_sessions),
        observed=frozenset(observed),
        closed_overrides=frozenset(closed_overrides),
    )


@dataclass(frozen=True)
class Calendars:
    nyse: TradingCalendar
    twse: TradingCalendar


def _calendar_of(calendars: Any, name: str) -> TradingCalendar:
    cal = calendars.get(name) if isinstance(calendars, Mapping) else getattr(calendars, name, None)
    if not isinstance(cal, TradingCalendar):
        raise ValueError(f"calendar {name!r} missing")
    return cal


def _holiday_set(holidays: Any) -> frozenset[date]:
    if isinstance(holidays, TradingCalendar):
        return holidays.holidays | holidays.closed_overrides
    return frozenset(holidays or ())


def _third_weekday(year: int, month: int, weekday: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 14)


def us_monthly_expiry_day(year: int, month: int, holidays: Any) -> date:
    """美股月選到期日：第三個週五；那天休市就提前到週四。"""
    friday = _third_weekday(year, month, 4)
    return friday - timedelta(days=1) if friday in _holiday_set(holidays) else friday


def tw_settlement_day(year: int, month: int, calendar: TradingCalendar | None = None) -> date:
    """台指月結算日：第三個週三；那天休市就順延到下一個交易日（期交所契約規格；日曆未知就用原日）。"""
    wednesday = _third_weekday(year, month, 2)
    if calendar is None or calendar.is_session(wednesday) is not False:
        return wednesday
    return calendar.next_session(wednesday) or wednesday


def is_monthly_expiry(day: date, holidays: Any) -> bool:
    """§4：週五且 15–21 日，或該週五休市時的週四 14–20 日。"""
    if day.weekday() == 4 and 15 <= day.day <= 21:
        return True
    return day.weekday() == 3 and 14 <= day.day <= 20 and (day + timedelta(days=1)) in _holiday_set(holidays)


def monthly_expiry(expiries: Iterable[date], holidays: Any, after: date | None = None) -> date | None:
    """從收到的到期日挑 `after`（資料日 D）之後第一個月選；找不到 → None（卡上標未知）。"""
    for expiry in sorted(set(expiries)):
        if after is not None and expiry <= after:
            continue
        if is_monthly_expiry(expiry, holidays):
            return expiry
    return None


def volume_day_flag(day: date, calendar: TradingCalendar) -> str | None:
    """量能的「結算日／早收日」（§18.15：美股月選結算的第三個週五、台指第三個週三、NYSE 早收）。"""
    if calendar.market == "us":
        if day == us_monthly_expiry_day(day.year, day.month, calendar):
            return FLAG_SETTLEMENT
        if day in calendar.early_closes:
            return FLAG_EARLY_CLOSE
        return None
    if day == tw_settlement_day(day.year, day.month, calendar):
        return FLAG_SETTLEMENT
    return None


def txo_front_month(data_date: date, calendar: TradingCalendar | None = None) -> str:
    """台指最近月選：結算日 > 資料日的第一個月份（結算日當天期交所仍列當月，要跳過，§18.13）。"""
    year, month = data_date.year, data_date.month
    if tw_settlement_day(year, month, calendar) <= data_date:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{year:04d}{month:02d}"


# ── 新鮮度（§5 類型表＋§18.1、§18.9） ─────────────────────────────────────────
KIND_US_DAILY = "us_daily"  # 美股日線、ETF、VIX、美債、宏觀（Yahoo／代理，每個代號一筆）
KIND_OPTIONS = "options"
KIND_TW_DAILY = "tw_daily"  # ALLBUT0999、BFI82U、融資、期交所、本益比
KIND_BTC = "btc"
KIND_PREDICTION = "prediction"
KIND_NEWS = "news"
KIND_TRUMP = "trump"
KIND_EVENT = "event"
KIND_CFTC = "cftc"
KIND_FINRA = "finra"
KIND_TW_MONTHLY = "tw_monthly"  # 月營收（表上沒有，比照 FINRA 以月底為錨）
KIND_13F = "13f"
KIND_US_FUND = "us_fund"
KIND_HOLDINGS_SPY = "holdings_spy"
KIND_HOLDINGS_NDX = "holdings_ndx"
KIND_HOLDINGS_MONEYDJ = "holdings_moneydj"
KIND_SEASON = "season"

_SESSION_KINDS = {
    KIND_US_DAILY: "nyse",
    KIND_HOLDINGS_SPY: "nyse",
    KIND_HOLDINGS_NDX: "nyse",
    KIND_TW_DAILY: "twse",
}
_INSTANT_KINDS = {
    KIND_BTC: BTC_TTL,
    KIND_PREDICTION: PREDICTION_TTL,
    KIND_NEWS: NEWS_TTL,
    KIND_TRUMP: TRUMP_TTL,
    KIND_EVENT: EVENT_GRACE,
}
_DAY_KINDS = {KIND_CFTC: CFTC_DAYS, KIND_13F: F13_DAYS, KIND_US_FUND: FUND_DAYS, KIND_HOLDINGS_MONEYDJ: MONEYDJ_DAYS}
_MONTH_END_KINDS = {KIND_FINRA: MONTH_END_DAYS, KIND_TW_MONTHLY: MONTH_END_DAYS}
STALE_KINDS = tuple(_SESSION_KINDS) + (KIND_OPTIONS,) + tuple(_INSTANT_KINDS) + tuple(_DAY_KINDS) + tuple(
    _MONTH_END_KINDS
) + (KIND_SEASON,)


@dataclass(frozen=True)
class Staleness:
    at: float  # epoch 秒；bot 只比 now > at
    calendar_pending: bool = False  # 表外，用了 +6 個日曆日的退路（卡上標「日曆待更新」）
    not_updated: bool = False  # 美股基本面：已經過了「最新應有季度」的申報期限（卡上標「未更新」）


def stale_after_info(kind: str, data_date: Any, calendars: Any, now: datetime | float) -> Staleness:
    """依資料類型算過期時間點。

    - 交易日類（美股日線／ETF／VIX／美債／宏觀、持股清單 SPY／Nasdaq-100、台股日資料）：資料日之後第 4 個
      預期交易日的收盤時刻。
    - 期權：資料日（`last_trade_time` 的美東日期）之後下一個預期 NYSE 交易日收盤＋3 小時（§18.1）。
    - 日曆表外：資料日＋6 個日曆日的收盤時刻（期權再＋3 小時）並標 calendar_pending（§18.3）。
    - 時刻類（BTC、預測市場＝抓取時間；新聞／川普＝pubDate；事件＝UTC 時刻）：data_date 傳 aware datetime。
    - 日期類：CFTC 報告日＋16 天、13F 申報期間眾數＋155 天、美股基本面季度結束＋160 天、MoneyDJ 清單日＋62 天、
      FINRA／月營收以月底為錨＋62 天；都取台北時間該日 0 點。
    - 季節性：靜態表（年度維護），樣本截止年＋2 年的 4/1 才算過期（data_date 傳截止年或該年任一天）。
    """
    if kind in _SESSION_KINDS or kind == KIND_OPTIONS:
        cal = _calendar_of(calendars, _SESSION_KINDS.get(kind, "nyse"))
        day = _plain_date(data_date)
        target = cal.add_sessions(day, STALE_SESSIONS) if kind in _SESSION_KINDS else cal.next_session(day)
        grace = OPTIONS_GRACE if kind == KIND_OPTIONS else timedelta(0)
        if target is None:
            fallback = datetime.combine(day + timedelta(days=CALENDAR_FALLBACK_DAYS), cal.close, tzinfo=cal.tz)
            return Staleness((fallback + grace).timestamp(), calendar_pending=True)
        return Staleness((cal.close_at(target) + grace).timestamp())
    if kind in _INSTANT_KINDS:
        return Staleness(_epoch(data_date) + _INSTANT_KINDS[kind].total_seconds())
    if kind in _DAY_KINDS:
        day = _plain_date(data_date)
        not_updated = False
        if kind == KIND_US_FUND:
            not_updated = _epoch(now) > _midnight_tpe(day + timedelta(days=FUND_NOT_UPDATED_DAYS))
        return Staleness(_midnight_tpe(day + timedelta(days=_DAY_KINDS[kind])), not_updated=not_updated)
    if kind in _MONTH_END_KINDS:
        day = _month_end(_plain_date(data_date))
        return Staleness(_midnight_tpe(day + timedelta(days=_MONTH_END_KINDS[kind])))
    if kind == KIND_SEASON:
        year = data_date if isinstance(data_date, int) and not isinstance(data_date, bool) else None
        if year is None:
            year = _plain_date(data_date).year
        return Staleness(_midnight_tpe(date(year + 2, 4, 1)))
    raise ValueError(f"unknown stale kind: {kind!r}")


def stale_after(kind: str, data_date: Any, calendars: Any, now: datetime | float) -> float:
    """INTERFACES 的介面：只回 epoch 秒（旗標要用 `stale_after_info`）。"""
    return stale_after_info(kind, data_date, calendars, now).at


def stale_min(values: Iterable[float | None]) -> float | None:
    """衍生列（寬度、量能、牆、GEX、一句話、矛盾…）：所有輸入的最小值；有一個輸入沒有 → None。"""
    out: float | None = None
    for value in values:
        if value is None:
            return None
        out = value if out is None else min(out, value)
    return out


def common_date(dates: Iterable[date | None]) -> date | None:
    """衍生列的資料日＝輸入的共同日期；不一致或有缺 → None。"""
    unique = set(dates)
    if len(unique) != 1:
        return None
    (only,) = unique
    return only


def cboe_snapshot_ok(fetched_at: datetime | float, cboe_timestamp: datetime | float) -> bool:
    """§18.1 拒收：抓取時間 − CBOE `timestamp` > 12 小時就不收，保留上一份。"""
    return _epoch(fetched_at) - _epoch(cboe_timestamp) <= CBOE_MAX_AGE.total_seconds()


def et_date(moment: datetime) -> date:
    """CBOE `last_trade_time` 等美東時間的日期（期權、VIX 的資料日，§18.1）。"""
    if moment.tzinfo is None:
        return moment.date()  # sources 已註明是美東當地時間
    return moment.astimezone(ET).date()


def fomc_decision_at(second_day: date) -> datetime:
    """FOMC 的事件時刻＝第二天 14:00 美東（§18.9）。"""
    return datetime.combine(second_day, time(14, 0), tzinfo=ET)


# ── 均線、RSI、回撤 ───────────────────────────────────────────────────────────
def sma(values: Sequence[float], n: int) -> float | None:
    if n <= 0 or len(values) < n:
        return None
    window = values[-n:]
    if not all(_is_num(v) for v in window):
        return None
    return math.fsum(window) / n


def ma_distance_pct(close: float | None, ma: float | None) -> float | None:
    if not _pos(close) or not _pos(ma):
        return None
    return (close / ma - 1.0) * 100.0


def rsi_wilder(closes: Sequence[float], period: int = RSI_PERIOD) -> float | None:
    """Wilder RSI：前 period 個變化取簡單平均，之後 (前值×(period−1)＋當期)/period。"""
    if period <= 0 or len(closes) < period + 1 or not all(_is_num(c) for c in closes):
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    avg_gain = math.fsum(gains[:period]) / period
    avg_loss = math.fsum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return None if avg_gain == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def drawdown_pct(closes: Sequence[float], window: int = DRAWDOWN_WINDOW) -> float | None:
    """收盤相對近 window 根最高收盤的回撤（%，≤0）。"""
    if len(closes) < window or not all(_pos(c) for c in closes[-window:]):
        return None
    tail = closes[-window:]
    return (tail[-1] / max(tail) - 1.0) * 100.0


def _sessions_upto(calendar: Any, day: date, count: int) -> list[date]:
    """呼叫端可以給 TradingCalendar，或已排好的預期交易日清單。"""
    if isinstance(calendar, TradingCalendar):
        return calendar.sessions_ending(day, count)
    days = sorted({d for d in calendar if d <= day})
    return days[-count:]


@dataclass(frozen=True)
class Technicals:
    close: float | None
    prev_close: float | None
    chg: float | None
    chg_pct: float | None
    ma: dict[int, float | None]
    ma_dist: dict[int, float | None]
    above: dict[int, bool | None]
    dd240_pct: float | None
    rsi14: float | None


def technicals(
    closes: Mapping[date, float],
    calendar: Any,
    day: date,
    quarantine: Sequence[tuple[date, date | None]] | None = None,
) -> Technicals:
    """單一 ETF／指數在 D 的技術面；均線要窗口內每個預期交易日都有收盤，日變化要前一筆＝prev_session(D)。

    quarantine＝這檔的隔離區間（分割沒對上、跳空）：碰到的窗口（日變化、均線、240 日回檔、RSI 往回算的那段）
    一律不算，和成分寬度同一個規則（2026-10-23 審查 r7 #5：0050 被隔離了，技術面照樣發布 -50%）。"""
    sessions = _sessions_upto(calendar, day, DRAWDOWN_WINDOW + 20)
    empty = {n: None for n in MA_WINDOWS}
    if not sessions or sessions[-1] != day or not _pos(closes.get(day)):
        return Technicals(None, None, None, None, dict(empty), dict(empty), dict(empty), None, None)
    series = [closes.get(s) for s in sessions]

    def clean(span: int) -> bool:  # 以 D 結尾、span 個交易日的窗口沒碰到隔離區間
        return not _touches_quarantine(quarantine, sessions[-span], day)

    close = float(series[-1])
    prev = series[-2] if len(series) >= 2 and _pos(series[-2]) and clean(2) else None
    chg = close - prev if prev is not None else None
    chg_pct = (close / prev - 1.0) * 100.0 if prev is not None else None
    ma: dict[int, float | None] = {}
    for n in MA_WINDOWS:
        window = series[-n:]
        ma[n] = sma(window, n) if len(window) == n and all(_pos(v) for v in window) and clean(n) else None
    ma_dist = {n: ma_distance_pct(close, ma[n]) for n in MA_WINDOWS}
    above = {n: (None if ma[n] is None else _position(close, ma[n]) > 0) for n in MA_WINDOWS}
    tail: list[float] = []
    for session, value in zip(reversed(sessions), reversed(series)):
        if not _pos(value) or _touches_quarantine(quarantine, session, session):
            break
        tail.append(float(value))
    tail.reverse()
    return Technicals(
        close=close,
        prev_close=prev,
        chg=chg,
        chg_pct=chg_pct,
        ma=ma,
        ma_dist=ma_dist,
        above=above,
        dd240_pct=(
            drawdown_pct(series, DRAWDOWN_WINDOW)
            if len(series) >= DRAWDOWN_WINDOW and clean(DRAWDOWN_WINDOW)
            else None
        ),
        rsi14=rsi_wilder(tail, RSI_PERIOD),
    )


# ── 寬度、漲跌家數、新高新低 ──────────────────────────────────────────────────
QuarantineMap = Mapping[str, Sequence[tuple[date, date | None]]]


def _position(close: float, ma: float) -> int:
    if math.isclose(close, ma, rel_tol=1e-9, abs_tol=1e-12):
        return 0
    return 1 if close > ma else -1


def _touches_quarantine(intervals: Sequence[tuple[date, date | None]] | None, start: date, end: date) -> bool:
    """隔離區間 (from, to)；to 為 None＝還沒解除。窗口 [start, end] 碰到任何一段就無效。"""
    for interval in intervals or ():
        begin, finish = interval[0], interval[1]
        if begin <= end and (finish is None or finish >= start):
            return True
    return False


def _window_valid(
    series: Sequence[float | None],
    sessions: Sequence[date],
    intervals: Sequence[tuple[date, date | None]] | None,
    end: int,
    span: int,
) -> bool:
    """以 end 結尾、span 個交易日的窗口：每天都有收盤，而且沒碰到隔離區間。"""
    start = end - span + 1
    return all(_pos(v) for v in series[start : end + 1]) and not _touches_quarantine(
        intervals, sessions[start], sessions[end]
    )


def _universe(closes_by_symbol: Mapping[str, Mapping[date, float]], members: Iterable[str] | None) -> list[str]:
    source = closes_by_symbol.keys() if members is None else members
    return list(dict.fromkeys(source))


@dataclass(frozen=True)
class Breadth:
    n: int
    above: int
    below: int
    equal: int
    new_below: int
    new_above: int
    valid: int
    total: int
    prev_pct: float | None  # 5 個交易日前（兩天都有效的同一批）
    prev_valid: int  # 那一批的家數
    same_set_pct: float | None  # 同一批在 D 的比例（和 prev_pct 對照）
    status: str  # "ok"（有效 ≥80%）／"low_coverage"

    @property
    def pct(self) -> float | None:
        return 100.0 * self.above / self.valid if self.valid else None

    @property
    def coverage(self) -> float:
        return self.valid / self.total if self.total else 0.0


def breadth(
    closes_by_symbol: Mapping[str, Mapping[date, float]],
    calendar: Any,
    D: date,
    n: int,
    *,
    members: Iterable[str] | None = None,
    quarantine: QuarantineMap | None = None,
) -> Breadth:
    """成分股站上／跌破 N 日線的家數（§4、§18.2）。

    有效＝D 往前 N+5 個預期交易日每天都有收盤、且窗口沒碰到該檔的隔離區間（只影響碰到的那條均線）。
    今天新跌破／新站上用 D−1 的均線比；5 個交易日前用「D 與 D−5 都有效」的同一批，交集 <80% 不比。
    """
    universe = _universe(closes_by_symbol, members)
    total = len(universe)
    span = n + BREADTH_EXTRA
    sessions = _sessions_upto(calendar, D, span + BREADTH_EXTRA)
    if n <= 0 or len(sessions) < span or sessions[-1] != D:
        return Breadth(n, 0, 0, 0, 0, 0, 0, total, None, 0, None, STATUS_LOW_COVERAGE)
    last = len(sessions) - 1
    has_prev_window = len(sessions) >= span + BREADTH_EXTRA
    counts = Counter()
    same_now = same_prev = same_total = 0
    for symbol in universe:
        closes = closes_by_symbol.get(symbol) or {}
        series = [closes.get(s) for s in sessions]
        intervals = (quarantine or {}).get(symbol)
        if not _window_valid(series, sessions, intervals, last, span):
            continue
        counts["valid"] += 1
        pos_now = _position(series[last], math.fsum(series[last - n + 1 : last + 1]) / n)
        pos_prev_day = _position(series[last - 1], math.fsum(series[last - n : last]) / n)
        counts[{1: "above", 0: "equal", -1: "below"}[pos_now]] += 1
        if pos_now < 0 <= pos_prev_day:
            counts["new_below"] += 1
        if pos_now > 0 >= pos_prev_day:
            counts["new_above"] += 1
        back = last - BREADTH_EXTRA
        if has_prev_window and _window_valid(series, sessions, intervals, back, span):
            same_total += 1
            same_now += pos_now > 0
            same_prev += _position(series[back], math.fsum(series[back - n + 1 : back + 1]) / n) > 0
    comparable = same_total > 0 and same_total >= COVERAGE_OK * total
    valid = counts["valid"]
    return Breadth(
        n=n,
        above=counts["above"],
        below=counts["below"],
        equal=counts["equal"],
        new_below=counts["new_below"],
        new_above=counts["new_above"],
        valid=valid,
        total=total,
        prev_pct=100.0 * same_prev / same_total if comparable else None,
        prev_valid=same_total,
        same_set_pct=100.0 * same_now / same_total if comparable else None,
        status=STATUS_OK if total and valid >= COVERAGE_OK * total else STATUS_LOW_COVERAGE,
    )


@dataclass(frozen=True)
class AdCounts:
    adv: int
    dec: int
    flat: int
    valid: int
    total: int


def ad_counts(
    closes_by_symbol: Mapping[str, Mapping[date, float]],
    calendar: Any,
    D: date,
    *,
    members: Iterable[str] | None = None,
    quarantine: QuarantineMap | None = None,
) -> AdCounts:
    """美股漲跌家數：D 對 prev_session(D)，兩天都有收盤、沒碰隔離區間才算。"""
    universe = _universe(closes_by_symbol, members)
    sessions = _sessions_upto(calendar, D, 2)
    if len(sessions) < 2 or sessions[-1] != D:
        return AdCounts(0, 0, 0, 0, len(universe))
    prev_day = sessions[0]
    counts = Counter()
    for symbol in universe:
        closes = closes_by_symbol.get(symbol) or {}
        now_close, prev_close = closes.get(D), closes.get(prev_day)
        if not (_pos(now_close) and _pos(prev_close)):
            continue
        if _touches_quarantine((quarantine or {}).get(symbol), prev_day, D):
            continue
        counts["valid"] += 1
        counts[{1: "adv", 0: "flat", -1: "dec"}[_position(now_close, prev_close)]] += 1
    return AdCounts(counts["adv"], counts["dec"], counts["flat"], counts["valid"], len(universe))


@dataclass(frozen=True)
class HighsLows:
    new_high: int
    new_low: int
    valid: int
    total: int


def highs_lows_240(
    closes_by_symbol: Mapping[str, Mapping[date, float]],
    calendar: Any,
    D: date,
    *,
    members: Iterable[str] | None = None,
    quarantine: QuarantineMap | None = None,
) -> HighsLows:
    """240 日新高／新低：245 根都有效者，D 收盤嚴格高於（低於）前 239 根。"""
    universe = _universe(closes_by_symbol, members)
    span = HIGHLOW_WINDOW + BREADTH_EXTRA
    sessions = _sessions_upto(calendar, D, span)
    if len(sessions) < span or sessions[-1] != D:
        return HighsLows(0, 0, 0, len(universe))
    counts = Counter()
    for symbol in universe:
        closes = closes_by_symbol.get(symbol) or {}
        series = [closes.get(s) for s in sessions]
        if not all(_pos(v) for v in series):
            continue
        if _touches_quarantine((quarantine or {}).get(symbol), sessions[0], sessions[-1]):
            continue
        counts["valid"] += 1
        before = series[-HIGHLOW_WINDOW:-1]
        counts["new_high"] += series[-1] > max(before)
        counts["new_low"] += series[-1] < min(before)
    return HighsLows(counts["new_high"], counts["new_low"], counts["valid"], len(universe))


def day_coverage(
    closes_by_symbol: Mapping[str, Mapping[date, float]], members: Iterable[str] | None, day: date
) -> tuple[int, int]:
    universe = _universe(closes_by_symbol, members)
    have = sum(1 for symbol in universe if _pos((closes_by_symbol.get(symbol) or {}).get(day)))
    return have, len(universe)


def latest_complete_day(
    closes_by_symbol: Mapping[str, Mapping[date, float]],
    members: Iterable[str] | None,
    days: Iterable[date],
    threshold: float = COVERAGE_OK,
) -> date | None:
    """只發布覆蓋率達 80% 的最新那一天（當天還在抓時就是前一個完整交易日）。"""
    member_list = list(members) if members is not None else None
    for day in sorted(set(days), reverse=True):
        have, total = day_coverage(closes_by_symbol, member_list, day)
        if total and have >= threshold * total:
            return day
    return None


# ── 量能 ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class VolumeState:
    ratio: float | None
    pct250: float | None
    label: str | None
    pv_label: str | None
    day_flag: str | None = None  # 結算日／早收日：照樣顯示，但不觸發量能類矛盾
    note: str | None = None  # 「來源不同，暫不判斷」


def volume_label(ratio: float | None) -> str | None:
    if ratio is None:
        return None
    if ratio <= VOL_STRONG_SHRINK:
        return VOL_LABEL_STRONG_SHRINK
    if ratio <= VOL_SHRINK:
        return VOL_LABEL_SHRINK
    if ratio < VOL_EXPAND:
        return VOL_LABEL_NORMAL
    if ratio < VOL_SURGE:
        return VOL_LABEL_EXPAND
    return VOL_LABEL_SURGE


def _volume_ok(value: Any) -> bool:
    return _is_num(value) and value >= 0


def volume_state(
    volumes: Mapping[date, float],
    D: date,
    *,
    calendar: Any = None,
    closes: Mapping[date, float] | None = None,
    day_flag: str | None = None,
    excluded: Collection[date] = (),
    quarantine: Sequence[tuple[date, date | None]] | None = None,
) -> VolumeState:
    """倍數＝D 量／前 20 個交易日平均（不含 D）；百分位＝近 250 個交易日（含 D）量 ≤ 當天的比例。

    calendar 沒給就用 volumes 的日期當交易日；excluded＝來源不同的日子（CBOE 備援量，§18.7）：
    D 本身是 → 不判斷；窗口裡有 → 當缺值。價量組合需要 closes（D 與前一個交易日都要有）。
    quarantine＝這檔的隔離區間：分割前後的量不同口徑，碰到的窗口不算倍數／百分位（2026-10-23 審查 r7 #5）。
    """
    sessions = _sessions_upto(calendar if calendar is not None else volumes.keys(), D, VOL_PCT_DAYS)
    if not sessions or sessions[-1] != D or not _volume_ok(volumes.get(D)):
        return VolumeState(None, None, None, None, day_flag)
    if D in excluded:
        return VolumeState(None, None, None, None, day_flag, TEXT_VOL_SOURCE_DIFF)

    def usable(day: date) -> bool:
        return day not in excluded and _volume_ok(volumes.get(day))

    today = float(volumes[D])
    prior = sessions[-(VOL_AVG_DAYS + 1) : -1]
    ratio = None
    if (
        len(prior) == VOL_AVG_DAYS
        and all(usable(day) for day in prior)
        and not _touches_quarantine(quarantine, prior[0], D)
    ):
        average = math.fsum(float(volumes[day]) for day in prior) / VOL_AVG_DAYS
        ratio = today / average if average > 0 else None
    pct250 = None
    if (
        len(sessions) == VOL_PCT_DAYS
        and all(usable(day) for day in sessions)
        and not _touches_quarantine(quarantine, sessions[0], D)
    ):
        pct250 = 100.0 * sum(1 for day in sessions if float(volumes[day]) <= today) / VOL_PCT_DAYS
    pv_label = None
    if closes is not None and ratio is not None and len(sessions) >= 2:
        now_close, prev_close = closes.get(D), closes.get(sessions[-2])
        if _pos(now_close) and _pos(prev_close) and _position(now_close, prev_close) != 0:
            up = now_close > prev_close
            more = ratio >= PV_SPLIT
            pv_label = ("價漲" if up else "價跌") + ("量增" if more else "量縮")
    return VolumeState(ratio, pct250, volume_label(ratio), pv_label, day_flag)


# ── 期權（CBOE） ────────────────────────────────────────────────────────────
_KIND_ALIASES = {"C": "C", "CALL": "C", "買權": "C", "P": "P", "PUT": "P", "賣權": "P"}


def _norm_kind(value: Any) -> str | None:
    return _KIND_ALIASES.get(value.strip().upper()) if isinstance(value, str) else None


def parse_option_symbol(symbol: Any) -> tuple[str, date, str, float] | None:
    """「QQQ261016C00688000」→ ("QQQ", 2026-10-16, "C", 688.0)；不是標準 root 的（調整後合約）→ None。"""
    if not isinstance(symbol, str):
        return None
    match = OPTION_SYMBOL_RE.fullmatch(symbol)
    if match is None:
        return None
    root, ymd, kind, strike = match.groups()
    try:
        expiry = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    except ValueError:
        return None
    return root, expiry, kind, int(strike) / 1000.0


@dataclass(frozen=True)
class OptionLeg:
    symbol: str
    root: str
    expiry: date
    kind: str  # "C" / "P"
    strike: float
    oi: int
    days: int  # 到期日 − D（日曆天）
    volume: float | None
    iv: float | None  # 已換成價外 IV 的話是換過的值
    gamma: float | None  # CBOE 自帶的 gamma（只拿來核對）
    iv_replaced: bool = False


@dataclass(frozen=True)
class ConflictLeg:
    """被排除的衝突合約：oi 已知（各筆一致）或未知（取各筆最大值放進分母，覆蓋率因此是下限）。"""

    expiry: date | None
    strike: float | None
    days: int | None  # 到期日 − D
    oi: int | None
    oi_max: int | None


@dataclass(frozen=True)
class OptionStats:
    raw: int
    bad: int
    conflict: int
    oi_valid: int
    gamma_valid: int
    iv_replaced: int
    expired: int
    conflict_legs: tuple[ConflictLeg, ...] = ()
    unattributed_conflicts: int = 0  # 來源只給筆數、沒給是哪幾筆
    unattributed_oi: int = 0  # 來源給了那些筆數的 OI 合計時，放進分母


def _contract_fields(contract: Any) -> tuple:
    """比對同代號的重複列用；NaN 彼此不相等，先換成 None，免得完全相同的兩列被當成衝突。"""
    values = (_get(contract, name) for name in ("oi", "volume", "iv", "gamma", "delta", "strike", "expiry", "kind"))
    return tuple(None if isinstance(v, float) and not math.isfinite(v) else v for v in values)


def option_sets(
    chain: Any, D: date, *, spot: float | None = None, root: str | None = None
) -> tuple[list[OptionLeg], list[OptionLeg], OptionStats]:
    """§4 的兩個集合。

    OI 有效：代號符合標準 root、OI ≥0 整數、履約價 >0、剩餘期限 >0（到期日 > D）；同代號值衝突 → 都丟。
    gamma 有效：OI 有效＋CBOE gamma 有限＋IV 有限且 >0；IV=0 的價內合約改用同到期、同履約價另一邊（價外）
    的 IV（記數），找不到就排除。chain 可以是 sources 的 OptionChain（contracts、raw_count、bad_count、
    conflict_count、spot…）或直接一串合約。
    """
    contracts = list(_get(chain, "contracts", chain) if not isinstance(chain, (list, tuple)) else chain)
    spot = spot if spot is not None else _get(chain, "spot")
    if not _pos(spot):
        raise ValueError("spot must be positive")
    raw = _get(chain, "raw_count") if not isinstance(chain, (list, tuple)) else None
    raw = raw if isinstance(raw, int) and not isinstance(raw, bool) else len(contracts)
    bad = _nonneg_int(_get(chain, "bad_count", 0) if not isinstance(chain, (list, tuple)) else 0) or 0
    upstream_conflicts = (
        _nonneg_int(_get(chain, "conflict_count", 0) if not isinstance(chain, (list, tuple)) else 0) or 0
    )
    if root is None and not isinstance(chain, (list, tuple)) and _get(chain, "symbol") in OPTION_ROOTS:
        root = _get(chain, "symbol")
    if root is None:
        roots = Counter(parsed[0] for c in contracts if (parsed := parse_option_symbol(_get(c, "symbol"))))
        root = roots.most_common(1)[0][0] if roots else None

    by_symbol: dict[str, list[Any]] = {}
    for contract in contracts:
        parsed = parse_option_symbol(_get(contract, "symbol"))
        if parsed is None or parsed[0] != root:
            bad += 1
            continue
        by_symbol.setdefault(_get(contract, "symbol"), []).append(contract)

    conflict_legs: list[ConflictLeg] = []
    conflict_rows = 0
    unique: list[tuple[tuple[str, date, str, float], Any]] = []
    for symbol, rows in by_symbol.items():
        parsed = parse_option_symbol(symbol)
        if len({_contract_fields(r) for r in rows}) > 1:
            conflict_rows += len(rows)
            ois = [_nonneg_int(_get(r, "oi")) for r in rows]
            known = ois[0] if ois[0] is not None and all(o == ois[0] for o in ois) else None
            candidates = [o for o in ois if o is not None]
            conflict_legs.append(
                ConflictLeg(parsed[1], parsed[3], (parsed[1] - D).days, known, max(candidates) if candidates else None)
            )
            continue
        unique.append((parsed, rows[0]))

    chain_conflicts = _get(chain, "conflicts") if not isinstance(chain, (list, tuple)) else None
    unattributed = 0
    unattributed_oi = 0
    if chain_conflicts:
        for item in chain_conflicts:
            parsed = parse_option_symbol(_get(item, "symbol"))
            oi = _nonneg_int(_get(item, "oi"))
            if parsed is None:
                conflict_legs.append(ConflictLeg(None, None, None, oi, oi))
            else:
                conflict_legs.append(ConflictLeg(parsed[1], parsed[3], (parsed[1] - D).days, oi, oi))
    elif upstream_conflicts:
        # 來源只給筆數：不知道落在哪裡，覆蓋率只能標下限；若給的是那些列的 OI 合計（數字）就整筆放進分母
        unattributed = upstream_conflicts
        known_total = _get(chain, "conflict_oi_known")
        if not isinstance(known_total, bool):
            unattributed_oi = _nonneg_int(known_total) or 0

    oi_valid: list[OptionLeg] = []
    expired = 0
    parsed_by_key: dict[tuple[date, str, float], Any] = {}
    for parsed, contract in unique:
        _root, expiry, kind, strike = parsed
        given_kind = _get(contract, "kind")
        given_strike = _get(contract, "strike")
        given_expiry = _get(contract, "expiry")
        if (
            (given_kind is not None and _norm_kind(given_kind) != kind)
            or (given_strike is not None and not (_is_num(given_strike) and math.isclose(given_strike, strike)))
            or (given_expiry is not None and given_expiry != expiry)
        ):
            bad += 1
            continue
        parsed_by_key[(expiry, kind, strike)] = contract
        oi = _nonneg_int(_get(contract, "oi"))
        if oi is None or strike <= 0:
            bad += 1
            continue
        days = (expiry - D).days
        if days <= 0:
            expired += 1
            continue
        volume = _get(contract, "volume")
        iv = _get(contract, "iv")
        gamma = _get(contract, "gamma")
        oi_valid.append(
            OptionLeg(
                symbol=_get(contract, "symbol"),
                root=root,
                expiry=expiry,
                kind=kind,
                strike=strike,
                oi=oi,
                days=days,
                volume=float(volume) if _volume_ok(volume) else None,
                iv=float(iv) if _is_num(iv) else None,
                gamma=float(gamma) if _is_num(gamma) else None,
            )
        )

    gamma_valid: list[OptionLeg] = []
    replaced = 0
    for leg in oi_valid:
        if leg.gamma is None or leg.iv is None or leg.iv < 0:
            continue
        if leg.iv > 0:
            gamma_valid.append(leg)
            continue
        in_the_money = leg.strike < spot if leg.kind == "C" else leg.strike > spot
        if not in_the_money:
            continue
        other = parsed_by_key.get((leg.expiry, "P" if leg.kind == "C" else "C", leg.strike))
        other_iv = _get(other, "iv") if other is not None else None
        if _pos(other_iv):
            gamma_valid.append(replace(leg, iv=float(other_iv), iv_replaced=True))
            replaced += 1

    stats = OptionStats(
        raw=raw,
        bad=bad,
        conflict=upstream_conflicts + conflict_rows,
        oi_valid=len(oi_valid),
        gamma_valid=len(gamma_valid),
        iv_replaced=replaced,
        expired=expired,
        conflict_legs=tuple(conflict_legs),
        unattributed_conflicts=unattributed,
        unattributed_oi=unattributed_oi,
    )
    return oi_valid, gamma_valid, stats


def _in_window(leg: OptionLeg, window_days: int = OPTION_WINDOW_DAYS) -> bool:
    return 0 < leg.days <= window_days


def _both_sides(strikes: Iterable[float], spot: float, band: float = NEAR_BAND) -> bool:
    values = list(strikes)
    below = any(spot * (1 - band) <= k < spot for k in values)
    above = any(spot < k <= spot * (1 + band) for k in values)
    return below and above


def _max_oi_strike(oi_by_strike: Mapping[float, int], low: float, high: float, spot: float) -> float | None:
    """[low, high] 內 OI 最大的履約價；並列取離現價近的；全是 0 → None。"""
    best: tuple[int, float, float] | None = None
    for strike, oi in oi_by_strike.items():
        if not (low <= strike <= high) or oi <= 0:
            continue
        key = (-oi, abs(strike - spot), strike)
        if best is None or key < best:
            best = key
    return None if best is None else best[2]


def _aggregate(legs: Iterable[Any]) -> tuple[dict[float, int], dict[float, int]]:
    calls: dict[float, int] = {}
    puts: dict[float, int] = {}
    for leg in legs:
        target = calls if leg.kind == "C" else puts
        target[leg.strike] = target.get(leg.strike, 0) + leg.oi
    return calls, puts


def _dist_pct(strike: float | None, spot: float) -> float | None:
    return None if strike is None else (strike / spot - 1.0) * 100.0


@dataclass(frozen=True)
class Walls:
    call_wall: float | None
    put_wall: float | None
    call_wall_dist_pct: float | None
    put_wall_dist_pct: float | None
    status: str
    n_contracts: int


def _walls_from(legs: Sequence[Any], spot: float, min_contracts: int) -> Walls:
    if len(legs) < min_contracts or not _both_sides((leg.strike for leg in legs), spot):
        return Walls(None, None, None, None, STATUS_INSUFFICIENT, len(legs))
    calls, puts = _aggregate(legs)
    call_wall = _max_oi_strike(calls, spot, spot * (1 + WALL_BAND), spot)
    put_wall = _max_oi_strike(puts, spot * (1 - WALL_BAND), spot, spot)
    return Walls(call_wall, put_wall, _dist_pct(call_wall, spot), _dist_pct(put_wall, spot), STATUS_OK, len(legs))


def walls(
    oi_valid: Sequence[OptionLeg],
    spot: float,
    *,
    window_days: int = OPTION_WINDOW_DAYS,
    min_contracts: int = WALL_MIN_CONTRACTS,
) -> Walls:
    """D 之後 45 天內到期、OI 有效 ≥100 筆且現價 ±10% 兩邊都有才算；按履約價加總 OI。"""
    return _walls_from([leg for leg in oi_valid if _in_window(leg, window_days)], spot, min_contracts)


def max_pain(legs: Sequence[Any], spot: float) -> float | None:
    """到期時買賣方總內含價值最小的履約價（候選＝這些合約的履約價）；並列取離現價近的。"""
    if not legs or sum(leg.oi for leg in legs) <= 0:
        return None
    calls, puts = _aggregate(legs)
    best: tuple[float, float, float] | None = None
    for price in sorted(set(calls) | set(puts)):
        value = math.fsum(oi * (price - k) for k, oi in calls.items() if price > k) + math.fsum(
            oi * (k - price) for k, oi in puts.items() if k > price
        )
        key = (value, abs(price - spot), price)
        if best is None or key < best:
            best = key
    return best[2]


def _complete_month(legs: Sequence[Any], spot: float) -> bool:
    call_strikes = {leg.strike for leg in legs if leg.kind == "C"}
    put_strikes = {leg.strike for leg in legs if leg.kind == "P"}
    return (
        len(legs) >= MONTHLY_MIN_CONTRACTS
        and len(call_strikes) >= MONTHLY_MIN_STRIKES
        and len(put_strikes) >= MONTHLY_MIN_STRIKES
        and _both_sides(call_strikes | put_strikes, spot)
    )


@dataclass(frozen=True)
class MonthlyMetrics:
    expiry: date
    call_wall: float | None
    put_wall: float | None
    max_pain: float | None
    status: str
    n_contracts: int


def monthly_metrics(oi_valid: Sequence[OptionLeg], spot: float, expiry: date) -> MonthlyMetrics:
    """月選自己的 OI 有效 ≥40、買權與賣權各 ≥15 個履約價、現價 ±10% 兩邊都有，才算它的牆與最大痛點。"""
    legs = [leg for leg in oi_valid if leg.expiry == expiry]
    if not _complete_month(legs, spot):
        return MonthlyMetrics(expiry, None, None, None, STATUS_INSUFFICIENT, len(legs))
    calls, puts = _aggregate(legs)
    return MonthlyMetrics(
        expiry=expiry,
        call_wall=_max_oi_strike(calls, spot, spot * (1 + WALL_BAND), spot),
        put_wall=_max_oi_strike(puts, spot * (1 - WALL_BAND), spot, spot),
        max_pain=max_pain(legs, spot),
        status=STATUS_OK,
        n_contracts=len(legs),
    )


@dataclass(frozen=True)
class Pcr:
    oi: float | None
    volume: float | None


def pcr(oi_valid: Sequence[OptionLeg], *, window_days: int = OPTION_WINDOW_DAYS) -> Pcr:
    """賣權／買權（OI、量各一）；45 天視窗內 OI 有效的合約；分母 0 → 缺值。"""
    legs = [leg for leg in oi_valid if _in_window(leg, window_days)]
    call_oi = sum(leg.oi for leg in legs if leg.kind == "C")
    put_oi = sum(leg.oi for leg in legs if leg.kind == "P")
    call_vol = math.fsum(leg.volume for leg in legs if leg.kind == "C" and leg.volume is not None)
    put_vol = math.fsum(leg.volume for leg in legs if leg.kind == "P" and leg.volume is not None)
    return Pcr(put_oi / call_oi if call_oi > 0 else None, put_vol / call_vol if call_vol > 0 else None)


class GexCrossing(NamedTuple):
    price: float
    direction: str  # "up"＝價格往上穿過時由負轉正；"down"＝由正轉負


# 變號方向的中文說明：價格由低往高穿過該點時（圖的標籤與卡片的「變號點」都用這一份）
GEX_DIRECTION_TEXT = {"up": "由負轉正", "down": "由正轉負"}


@dataclass(frozen=True)
class GexResult:
    status: str  # "ok" / "insufficient"（±10% 覆蓋率 <80% 或沒有 gamma 有效合約）
    at_spot: float | None
    sign: int | None  # GEX(S_now) 的實際正負號
    crossings: tuple[GexCrossing, ...]  # 離現價最近的兩個變號點（依價格排序）
    near_zero: bool | None  # |GEX(S_now)| < ±15% 網格上 max|GEX| 的 2%
    coverage: float | None
    coverage_is_lower_bound: bool
    grid: tuple[float, ...] = ()
    values: tuple[float, ...] = ()
    max_abs: float | None = None
    check_at_spot: float | None = None  # 用 CBOE 自帶 gamma 算的現價 GEX（核對重算用）


def bs_gamma(spot: Any, strike: Any, years: Any, sigma: Any, rate: float) -> Any:
    """Black-Scholes gamma（無股息）；參數可以是 numpy 陣列，依廣播規則向量化。"""
    import numpy as np  # 只有算 GEX 才載入（bot 端不能載 numpy）

    spot = np.asarray(spot, dtype=float)
    strike = np.asarray(strike, dtype=float)
    years = np.asarray(years, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    root_t = np.sqrt(years)
    d1 = (np.log(spot / strike) + (rate + 0.5 * sigma**2) * years) / (sigma * root_t)
    return np.exp(-0.5 * d1**2) / (math.sqrt(2.0 * math.pi) * spot * sigma * root_t)


def _gex_values(prices: Any, legs: Sequence[OptionLeg], rate: float) -> Any:
    import numpy as np

    prices = np.asarray(prices, dtype=float)[:, None]
    strikes = np.array([leg.strike for leg in legs], dtype=float)[None, :]
    years = np.array([leg.days / 365.0 for leg in legs], dtype=float)[None, :]
    sigma = np.array([leg.iv for leg in legs], dtype=float)[None, :]
    signed_oi = np.array([leg.oi if leg.kind == "C" else -leg.oi for leg in legs], dtype=float)[None, :]
    gamma = bs_gamma(prices, strikes, years, sigma, rate)
    return (gamma * signed_oi).sum(axis=1) * CONTRACT_MULTIPLIER * prices[:, 0] ** 2 * 0.01


def _crossings(grid: Sequence[float], values: Sequence[float], tolerance: float) -> list[GexCrossing]:
    out: list[GexCrossing] = []
    last_index: int | None = None
    last_value = 0.0
    for index, value in enumerate(values):
        if abs(value) <= tolerance:
            continue
        if last_index is not None and (value > 0) != (last_value > 0):
            if index == last_index + 1:
                price = grid[last_index] + (grid[index] - grid[last_index]) * last_value / (last_value - value)
            else:  # 中間有一段剛好是 0，取那段的中點
                price = (grid[last_index + 1] + grid[index - 1]) / 2.0
            out.append(GexCrossing(float(price), "up" if value > 0 else "down"))
        last_index, last_value = index, value
    return out


def gex_from_sets(
    oi_valid: Sequence[OptionLeg], gamma_valid: Sequence[OptionLeg], stats: OptionStats, spot: float, rate: float
) -> GexResult:
    """GEX(S)＝Σ（買權＋、賣權−）gamma(S)×OI×100×S²×1%（假設造市商持有買權、賣出賣權）。

    計算用 45 天視窗內的 gamma 有效合約；覆蓋率＝其中履約價在現價 ±10% 的 OI ÷ 同範圍 OI 有效的 OI
    （衝突排除的：OI 已知計入分母；未知取最大候選值計入並標下限；來源只給筆數 → 標下限）。
    """
    if not _pos(spot):
        raise ValueError("spot must be positive")
    if not (_is_num(rate) and -0.05 < rate < 0.5):
        raise ValueError("rate must be a decimal (e.g. 0.0426)")
    low, high = spot * (1 - NEAR_BAND), spot * (1 + NEAR_BAND)

    def near(leg: Any) -> bool:
        return low <= leg.strike <= high

    window_oi = [leg for leg in oi_valid if _in_window(leg)]
    legs = [leg for leg in gamma_valid if _in_window(leg)]
    denominator = sum(leg.oi for leg in window_oi if near(leg))
    lower_bound = stats.unattributed_conflicts > 0
    denominator += stats.unattributed_oi
    for conflict in stats.conflict_legs:
        if conflict.days is None or conflict.strike is None:
            lower_bound = True  # 連是哪一檔都不知道，不能確定在不在範圍內
            continue
        if not (0 < conflict.days <= OPTION_WINDOW_DAYS and low <= conflict.strike <= high):
            continue
        if conflict.oi is not None:
            denominator += conflict.oi
        else:
            lower_bound = True
            denominator += conflict.oi_max or 0
    numerator = sum(leg.oi for leg in legs if near(leg))
    coverage = numerator / denominator if denominator > 0 else None
    if not legs or coverage is None or coverage < GEX_COVERAGE_OK:
        return GexResult(STATUS_INSUFFICIENT, None, None, (), None, coverage, lower_bound)

    import numpy as np

    grid = np.linspace(GEX_GRID_LOW, GEX_GRID_HIGH, GEX_GRID_POINTS) * spot
    values = _gex_values(grid, legs, rate)
    at_spot = float(_gex_values([spot], legs, rate)[0])
    max_abs = float(np.max(np.abs(values)))
    near_zero = max_abs == 0.0 or abs(at_spot) < GEX_NEAR_ZERO * max_abs
    found = _crossings(grid.tolist(), values.tolist(), 1e-9 * max_abs)
    nearest = sorted(sorted(found, key=lambda c: abs(c.price - spot))[:2], key=lambda c: c.price)
    check = math.fsum(
        (leg.gamma if leg.kind == "C" else -leg.gamma) * leg.oi * CONTRACT_MULTIPLIER * spot**2 * 0.01 for leg in legs
    )
    return GexResult(
        status=STATUS_OK,
        at_spot=at_spot,
        sign=(at_spot > 0) - (at_spot < 0),
        crossings=tuple(nearest),
        near_zero=near_zero,
        coverage=coverage,
        coverage_is_lower_bound=lower_bound,
        grid=tuple(float(x) for x in grid),
        values=tuple(float(x) for x in values),
        max_abs=max_abs,
        check_at_spot=check,
    )


def gex_curve(chain: Any, spot: float, r: float, *, D: date | None = None) -> GexResult:
    """INTERFACES 的介面：先建 OI／gamma 有效集合再算；r 是小數（3M 公債 4.26% → 0.0426）。

    D 不給就用 chain.quote_last_trade_et 的美東日期（§18.1 的資料日）。
    """
    if D is None:
        moment = _get(chain, "quote_last_trade_et")
        if not isinstance(moment, datetime):
            raise ValueError("D is required when the chain has no quote_last_trade_et")
        D = et_date(moment)
    oi_valid, gamma_valid, stats = option_sets(chain, D, spot=spot)
    return gex_from_sets(oi_valid, gamma_valid, stats, spot, r)


def options_values(
    chain: Any,
    D: date,
    *,
    spot: float,
    rate: float | None,
    holidays: Any,
    iv30: float | None = None,
) -> dict:
    """組 `us.options.<SYM>` 的 values（欄位見 ROW_IDS）；rate=None → 用 4% 並標 rate_assumed。"""
    oi_valid, gamma_valid, stats = option_sets(chain, D, spot=spot)
    rate_assumed = rate is None
    gex = gex_from_sets(oi_valid, gamma_valid, stats, spot, DEFAULT_RATE if rate_assumed else rate)
    wall = walls(oi_valid, spot)
    expiry = monthly_expiry({leg.expiry for leg in oi_valid}, holidays, after=D)
    monthly = None
    if expiry is not None:
        month = monthly_metrics(oi_valid, spot, expiry)
        monthly = {
            "expiry": expiry.isoformat(),
            "call_wall": month.call_wall,
            "put_wall": month.put_wall,
            "max_pain": month.max_pain,
            "status": month.status,
        }
    ratio = pcr(oi_valid)
    iv30 = iv30 if iv30 is not None else _get(chain, "iv30") if not isinstance(chain, (list, tuple)) else None
    return {
        "spot": float(spot),
        "call_wall": wall.call_wall,
        "put_wall": wall.put_wall,
        "call_wall_dist_pct": wall.call_wall_dist_pct,
        "put_wall_dist_pct": wall.put_wall_dist_pct,
        "wall_status": wall.status,
        "monthly": monthly,
        "pcr_oi": ratio.oi,
        "pcr_vol": ratio.volume,
        "iv30": float(iv30) if _is_num(iv30) else None,
        "gex": {
            "status": gex.status,
            "at_spot": gex.at_spot,
            "sign": gex.sign,
            "crossings": [{"price": c.price, "direction": c.direction} for c in gex.crossings],
            "near_zero": gex.near_zero,
            "coverage": gex.coverage,
            "lower_bound": gex.coverage_is_lower_bound,
            "rate_assumed": rate_assumed,
        },
        "counts": {
            "raw": stats.raw,
            "bad": stats.bad,
            "conflict": stats.conflict,
            "oi_valid": stats.oi_valid,
            "gamma_valid": stats.gamma_valid,
            "iv_replaced": stats.iv_replaced,
        },
        "oi_date_note": TEXT_OI_DATE,
    }


# ── 台指選擇權 ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _TxoLeg:
    kind: str
    strike: float
    oi: int


@dataclass(frozen=True)
class TxoWalls:
    month: str
    spot: float
    call_wall: float | None
    put_wall: float | None
    max_pain: float | None
    call_wall_dist_pct: float | None
    put_wall_dist_pct: float | None
    status: str
    n_contracts: int


def txo_walls(rows: Iterable[Any], month: str, spot: float) -> TxoWalls:
    """最近月選（ContractMonth(Week)＝YYYYMM、一般盤）的牆與最大痛點；完整性門檻同美股月選。

    spot＝期交所資料日那天的加權收盤（期交所常晚一天，§18.13）。同月同履約價同買賣權出現兩個不同 OI → 都丟。
    """
    if not _pos(spot):
        raise ValueError("spot must be positive")
    seen: dict[tuple[str, float], set[int]] = {}
    for row in rows:
        if str(_get(row, "month", "")).strip() != month:
            continue
        session = _get(row, "session")
        if session is not None and session not in ("一般", "regular"):
            continue
        kind = _norm_kind(_get(row, "kind"))
        strike = _get(row, "strike")
        oi = _nonneg_int(_get(row, "oi"))
        if kind is None or not _pos(strike) or oi is None:
            continue
        seen.setdefault((kind, float(strike)), set()).add(oi)
    legs = [_TxoLeg(kind, strike, next(iter(ois))) for (kind, strike), ois in seen.items() if len(ois) == 1]
    if not _complete_month(legs, spot):
        return TxoWalls(month, spot, None, None, None, None, None, STATUS_INSUFFICIENT, len(legs))
    calls, puts = _aggregate(legs)
    call_wall = _max_oi_strike(calls, spot, spot * (1 + WALL_BAND), spot)
    put_wall = _max_oi_strike(puts, spot * (1 - WALL_BAND), spot, spot)
    return TxoWalls(
        month=month,
        spot=spot,
        call_wall=call_wall,
        put_wall=put_wall,
        max_pain=max_pain(legs, spot),
        call_wall_dist_pct=_dist_pct(call_wall, spot),
        put_wall_dist_pct=_dist_pct(put_wall, spot),
        status=STATUS_OK,
        n_contracts=len(legs),
    )


# ── 台股還原除權息 ───────────────────────────────────────────────────────────
def _rebuild(bar: Any, close: float) -> Any:
    if is_dataclass(bar) and not isinstance(bar, type):
        return replace(bar, close=close)
    if hasattr(bar, "_replace"):
        return bar._replace(close=close)
    raise TypeError("bar must be a dataclass or namedtuple with a close field")


def adjust_tw_closes(bars: Sequence[Any], exrights: Iterable[Any], *, code: str | None = None) -> list[Any]:
    """因子＝除權息參考價／除權息前收盤，只乘在除權息日之前的收盤（不調量）；多個事件連乘。

    只用已經發生的事件（除權息日 ≤ 最後一根的日期）；同一檔同一天兩筆不同因子 → ParseError("conflict")。
    """
    if not bars:
        return []
    last_day = max(_get(bar, "date") for bar in bars)
    factors: dict[date, float] = {}
    for event in exrights:
        if code is not None and str(_get(event, "code")) != code:
            continue
        day, pre, ref = _get(event, "date"), _get(event, "pre_close"), _get(event, "ref_price")
        if not (_pos(pre) and _pos(ref)) or not isinstance(day, date) or day > last_day:
            continue
        factor = ref / pre
        if day in factors and not math.isclose(factors[day], factor, rel_tol=1e-12):
            raise ParseError("conflict", "two ex-right factors on one day")
        factors[day] = factor
    ordered = sorted(factors.items())
    out = []
    for bar in bars:
        day = _get(bar, "date")
        cumulative = 1.0
        for ex_day, factor in ordered:
            if day < ex_day:
                cumulative *= factor
        close = _get(bar, "close")
        out.append(bar if cumulative == 1.0 or not _is_num(close) else _rebuild(bar, close * cumulative))
    return out


# ── 基本面、股別合併、13F ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class EpsResult:
    pe: float | None
    ttm_net_income_kusd: float | None
    quarter_ends: tuple[date, ...]
    status: str  # ok / missing / not_consecutive / conflict / nonpositive / no_market_cap
    note: str | None


def eps_metrics(quarters: Iterable[Any], market_cap_usd: float | None = None) -> EpsResult:
    """本益比＝市值 ÷ 近四季普通股淨利合計（千美元×1000）；四季要齊、連續（相鄰 75–105 天）、不重複，合計 >0。"""
    by_end: dict[date, set[float]] = {}
    for quarter in quarters:
        if isinstance(quarter, tuple) and not hasattr(quarter, "_fields"):
            end, value = quarter[0], quarter[1]
        else:
            end, value = _get(quarter, "end_date"), _get(quarter, "net_income_common_kusd")
        if not isinstance(end, date):
            continue
        by_end.setdefault(end, set()).add(float(value) if _is_num(value) else math.nan)
    if any(len(values) > 1 for values in by_end.values()):
        return EpsResult(None, None, (), "conflict", TEXT_PE_MISSING)
    latest = sorted(by_end, reverse=True)[:4]
    values = [next(iter(by_end[end])) for end in latest]
    if len(latest) < 4 or not all(math.isfinite(v) for v in values):
        return EpsResult(None, None, tuple(latest), "missing", TEXT_PE_MISSING)
    gaps = [(a - b).days for a, b in zip(latest, latest[1:])]
    if not all(75 <= gap <= 105 for gap in gaps):
        return EpsResult(None, None, tuple(latest), "not_consecutive", TEXT_PE_MISSING)
    ttm = math.fsum(values)
    if ttm <= 0:
        return EpsResult(None, ttm, tuple(latest), "nonpositive", TEXT_PE_NONPOSITIVE)
    if not _pos(market_cap_usd):
        return EpsResult(None, ttm, tuple(latest), "no_market_cap", None)
    return EpsResult(market_cap_usd / (ttm * 1000.0), ttm, tuple(latest), STATUS_OK, None)


def median_with_count(values: Iterable[float | None]) -> tuple[float | None, int]:
    """中位數附有效家數（None 不算）。"""
    usable = [float(v) for v in values if _is_num(v)]
    return (statistics.median(usable) if usable else None), len(usable)


SHARE_CLASS_GROUPS = (("GOOGL", "GOOG"), ("FOXA", "FOX"), ("NWSA", "NWS"), ("BRK.B", "BRK.A"))
_SHARE_CLASS_KEY = {symbol: group[0] for group in SHARE_CLASS_GROUPS for symbol in group}


@dataclass(frozen=True)
class MergedHolding:
    name: str
    symbols: tuple[str, ...]
    weight: float | None
    primary: str  # 13F 數字取這個股別（市值較大：market_values 或持股列的 market_cap；都沒有就看權重）


def merge_share_classes(holdings: Iterable[Any], market_values: Mapping[str, float] | None = None) -> list[MergedHolding]:
    """同一公司的不同股別（GOOGL＋GOOG、FOXA／FOX、NWSA／NWS、BRK.B／BRK.A）合併成一家，權重相加，依權重排序。"""
    groups: dict[str, list[Any]] = {}
    order: list[str] = []
    for holding in holdings:
        symbol = _get(holding, "symbol")
        if not isinstance(symbol, str):
            continue
        key = _SHARE_CLASS_KEY.get(symbol, symbol)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(holding)
    merged: list[MergedHolding] = []
    for key in order:
        members = groups[key]
        weights = [_get(h, "weight") for h in members]
        known = [float(w) for w in weights if _is_num(w)]

        def size(holding: Any) -> float:
            symbol = _get(holding, "symbol")
            if market_values and _is_num(market_values.get(symbol)):
                return float(market_values[symbol])
            if _is_num(_get(holding, "market_cap")):  # Nasdaq-100 名單給的是市值
                return float(_get(holding, "market_cap"))
            weight = _get(holding, "weight")
            return float(weight) if _is_num(weight) else -1.0

        primary = max(members, key=size)
        merged.append(
            MergedHolding(
                name=str(_get(primary, "name") or _get(primary, "symbol")),
                symbols=tuple(_get(h, "symbol") for h in members),
                weight=math.fsum(known) if known else None,
                primary=_get(primary, "symbol"),
            )
        )
    return sorted(merged, key=lambda h: -(h.weight if h.weight is not None else -1.0))


def inst13f_summary(rows: Iterable[Any]) -> dict[str, int]:
    """「前十大成分股中，X 檔的加碼機構多於減碼機構（Y 檔資料暫缺）」的數字；沒資料不算成「沒有加碼」。"""
    more = missing = total = 0
    for row in rows:
        total += 1
        increased, decreased = _get(row, "increased"), _get(row, "decreased")
        if not (_is_num(increased) and _is_num(decreased)):
            missing += 1
        elif increased > decreased:
            more += 1
    return {"more_increase": more, "missing": missing, "total": total}


def holder_period_mode(periods: Iterable[date]) -> tuple[date | None, bool]:
    """前十大持有人申報期間的眾數（並列取較新的，免得永遠過期，§18.9）＋是否混合。"""
    counts = Counter(p for p in periods if isinstance(p, date))
    if not counts:
        return None, False
    mode = max(counts, key=lambda p: (counts[p], p))
    return mode, len(counts) > 1


def revenue_positive_share(revenues: Mapping[str, Any]) -> tuple[tuple[int, int] | None, int, int]:
    """0050 最新月營收年增 >0 的比例：只比同一月份（取最多家公布的那個月，並列取較新）。"""
    by_month: dict[tuple[int, int], list[float]] = {}
    for revenue in revenues.values():
        year, month, yoy = _get(revenue, "year"), _get(revenue, "month"), _get(revenue, "yoy")
        if isinstance(year, int) and isinstance(month, int) and _is_num(yoy):
            by_month.setdefault((year, month), []).append(float(yoy))
    if not by_month:
        return None, 0, 0
    month = max(by_month, key=lambda m: (len(by_month[m]), m))
    values = by_month[month]
    return month, sum(1 for v in values if v > 0), len(values)


# ── 矛盾與一句話 ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Fact:
    """一個面向：值、資料日、狀態正常與否（過期／暫缺 → ok=False，不參與一句話與矛盾）。"""

    value: Any
    data_date: date | None
    ok: bool = True


class Contra(NamedTuple):
    code: str
    subject: str | None


CONTRA_CODES = (
    "above_ma20_weak_breadth",
    "up_low_volume",
    "down_high_volume",
    "foreign_sell_trust_buy",
    "index_down_margin_up",
    "index_up_foreign_short_up",
    "gamma_pos_vix_inverted",
)
CONTRA_TEXT = {
    "above_ma20_weak_breadth": "{subject}在月線上，但成分股站上月線不到一半",
    "up_low_volume": "{subject}上漲但量偏少",
    "down_high_volume": "{subject}下跌但量偏多",
    "foreign_sell_trust_buy": "外資 5 日賣超、投信 5 日買超",
    "index_down_margin_up": "加權指數下跌、融資增加",
    "index_up_foreign_short_up": "加權指數上漲、外資台指期淨空單增加",
    "gamma_pos_vix_inverted": "{subject}假設部位 gamma 正、近月恐慌偏高",  # VIX／VIX3M > 1
}
# 主詞先中文（§6「先中文、括號英文」）；卡上每列 ≤24 個中文字寬，句型配上最長的主詞也不超過
SUBJECT_NAME = {"VOO": "標普500", "QQQ": "那斯達克", "SOXX": "半導體", "SPY": "標普500（SPY）", "0050": "台灣50", "TAIEX": "加權指數"}
US_SUBJECTS = ("VOO", "QQQ", "SOXX")
US_GAMMA_SUBJECTS = ("SPY", "QQQ", "SOXX")
_SHRINK_LABELS = (VOL_LABEL_SHRINK, VOL_LABEL_STRONG_SHRINK)
_EXPAND_LABELS = (VOL_LABEL_EXPAND, VOL_LABEL_SURGE)


def _same_day(state: Mapping[str, Fact], keys: Sequence[str]) -> list[Any] | None:
    """全部都在、狀態正常、資料日相同才回值；否則 None（不比）。"""
    facts = [state.get(key) for key in keys]
    if any(f is None or not f.ok or f.value is None or f.data_date is None for f in facts):
        return None
    if len({f.data_date for f in facts}) != 1:
        return None
    return [f.value for f in facts]


def _flagged(state: Mapping[str, Fact], subject: str) -> bool:
    flag = state.get(f"{subject}.vol_flag")
    return flag is not None and bool(flag.value)


def _volume_rules(state: Mapping[str, Fact], subject: str) -> list[Contra]:
    if _flagged(state, subject):
        return []
    values = _same_day(state, (f"{subject}.chg_pct", f"{subject}.vol_label"))
    if values is None or not _is_num(values[0]):
        return []
    change, label = values
    if change > 0 and label in _SHRINK_LABELS:
        return [Contra("up_low_volume", subject)]
    if change < 0 and label in _EXPAND_LABELS:
        return [Contra("down_high_volume", subject)]
    return []


def _breadth_rule(state: Mapping[str, Fact], subject: str) -> list[Contra]:
    values = _same_day(state, (f"{subject}.above_ma20", f"{subject}.breadth20_pct"))
    if values is not None and values[0] is True and _is_num(values[1]) and values[1] < 50:
        return [Contra("above_ma20_weak_breadth", subject)]
    return []


def contradictions(state: Mapping[str, Fact], market: str | None = None) -> list[Contra]:
    """7 條矛盾規則（§4），只用狀態正常且資料日相同的面向；回傳全部命中的，卡上取第一條。
    market＝"us"／"tw"；不給就兩邊都看（美股在前）。

    state 的鍵：「<代號>.chg_pct／.above_ma20／.breadth20_pct／.vol_label／.vol_flag／.gex_sign／.gex_near_zero」
    （美股 VOO、QQQ、SOXX、SPY；台股 0050 與 TAIEX），「VIX」「VIX3M」，「tw.foreign_5d」「tw.trust_5d」
    「tw.margin_chg」「tw.fut_net」「tw.fut_net_chg」（億元／口）。
    """
    if market is None:
        return contradictions(state, "us") + contradictions(state, "tw")
    found: list[Contra] = []
    if market == "us":
        for subject in US_SUBJECTS:
            found += _breadth_rule(state, subject)
        for subject in US_SUBJECTS:
            found += _volume_rules(state, subject)
        for subject in US_GAMMA_SUBJECTS:
            values = _same_day(state, (f"{subject}.gex_sign", f"{subject}.gex_near_zero", "VIX", "VIX3M"))
            if values is None:
                continue
            sign, near_zero, vix, vix3m = values
            if sign == 1 and near_zero is False and _pos(vix) and _pos(vix3m) and vix / vix3m > 1:
                found.append(Contra("gamma_pos_vix_inverted", subject))
        return found
    if market != "tw":
        raise ValueError("market must be 'us' or 'tw'")
    found += _breadth_rule(state, "0050")
    found += _volume_rules(state, "TAIEX")
    values = _same_day(state, ("tw.foreign_5d", "tw.trust_5d"))
    if values is not None and _is_num(values[0]) and _is_num(values[1]) and values[0] < 0 < values[1]:
        found.append(Contra("foreign_sell_trust_buy", None))
    values = _same_day(state, ("TAIEX.chg_pct", "tw.margin_chg"))
    if values is not None and _is_num(values[0]) and _is_num(values[1]) and values[0] < 0 < values[1]:
        found.append(Contra("index_down_margin_up", None))
    values = _same_day(state, ("TAIEX.chg_pct", "tw.fut_net", "tw.fut_net_chg"))
    if values is not None and all(_is_num(v) for v in values) and values[0] > 0 and values[1] < 0 and values[2] < 0:
        found.append(Contra("index_up_foreign_short_up", None))
    return found


def contra_sentence(contra: Contra) -> str:
    subject = SUBJECT_NAME.get(contra.subject or "", "")
    return CONTRA_TEXT[contra.code].format(subject=subject)


def contra_values(found: Sequence[Contra]) -> dict:
    """`contra.us`／`contra.tw` 的 values：全部命中的代碼＋卡上那一列（每張卡最多 1 列）。"""
    codes = [c.code if c.subject is None else f"{c.code}:{c.subject}" for c in found]
    return {"codes": codes, "text": contra_sentence(found[0]) if found else None}


def _fmt_yi(value: float) -> str:
    amount = abs(value)
    return f"{amount:,.0f}" if amount >= 100 else f"{amount:,.1f}"


def one_liner(state: Mapping[str, Fact], market: str) -> str:
    """固定句型的一句話（altText 開頭、卡上一列）；只放和主指數同一資料日、狀態正常的面向，缺主指數就回空字串。

    美股：「美股收在月線上、成分股站上月線 41%、量偏少」（VOO 與其 S&P 500 成分）；
    台股：「台股收在月線下、外資賣超 120 億、量偏多」（加權指數、三大法人、成交金額）。
    """
    if market == "us":
        anchor_key, prefix, extras = "VOO.above_ma20", "美股", ("VOO.breadth20_pct", "VOO.vol_label")
        flag_subject = "VOO"
    elif market == "tw":
        anchor_key, prefix, extras = "TAIEX.above_ma20", "台股", ("tw.foreign", "TAIEX.vol_label")
        flag_subject = "TAIEX"
    else:
        raise ValueError("market must be 'us' or 'tw'")
    anchor = state.get(anchor_key)
    if anchor is None or not anchor.ok or not isinstance(anchor.value, bool) or anchor.data_date is None:
        return ""
    parts = [f"{prefix}收在月線{'上' if anchor.value else '下'}"]
    for key in extras:
        values = _same_day(state, (anchor_key, key))
        if values is None:
            continue
        value = values[1]
        if key.endswith("breadth20_pct") and _is_num(value):
            parts.append(f"成分股站上月線 {round(value):.0f}%")
        elif key == "tw.foreign" and _is_num(value) and value != 0:
            parts.append(f"外資{'買超' if value > 0 else '賣超'} {_fmt_yi(value)} 億")
        elif key.endswith("vol_label") and value in VOL_PHRASE and not _flagged(state, flag_subject):
            parts.append(VOL_PHRASE[value])
    return "、".join(parts)


# ── 季節性列（讀 market_seasonality.json 的內容，不讀檔） ─────────────────────────
def is_midterm_year(year: int) -> bool:
    return year % 4 == 2


def season_values(table: Mapping[str, Any], year: int, month: int) -> dict:
    """`season.now` 的 values：當月 S&P／加權指數的歷史統計；期中選舉年那幾列只在期中選舉年放。"""

    def stats(section: Any, *path: str) -> dict | None:
        node = section
        for key in path:
            if not isinstance(node, Mapping) or key not in node:
                return None
            node = node[key]
        return dict(node) if isinstance(node, Mapping) else None

    midterm = is_midterm_year(year)
    sp = table.get("sp500") if isinstance(table, Mapping) else None
    tw = table.get("taiex") if isinstance(table, Mapping) else None
    tw_pending = not isinstance(tw, Mapping) or bool(tw.get("pending_full_data"))
    m = str(month)
    return {
        "month": month,
        "midterm_year": midterm,
        "sp500": stats(sp, "months", m),
        "sp500_q4": stats(sp, "q4"),
        "sp500_midterm": stats(sp, "midterm", "months", m) if midterm else None,
        "sp500_midterm_q4": stats(sp, "midterm", "q4") if midterm else None,
        "taiex": None if tw_pending else stats(tw, "months", m),
        "taiex_q4": None if tw_pending else stats(tw, "q4"),
        "taiex_midterm": None if tw_pending or not midterm else stats(tw, "midterm", "months", m),
        "taiex_pending": tw_pending,
    }


# ── 顯示用快照的列 id 與 values 欄位（job 寫、bot 讀，兩端 import 這一份） ────────────
# 型別：num（int 或 float、有限、不是 bool）、int、str、bool、date（"YYYY-MM-DD"）、any；後綴「?」＝可為 null；
# dict＝固定鍵（全部要有）、{"*": spec}＝任意鍵、[spec]＝清單；("?", spec)＝可為 null 的巢狀結構。
MA_DIST_SPEC = {str(n): "num?" for n in MA_WINDOWS}
STATS_SPEC = {"up_pct": "num", "median": "num", "mean": "num", "n": "int"}
PER_N_SPEC = {
    "above": "int",
    "below": "int",
    "equal": "int",
    "new_below": "int",
    "new_above": "int",
    "valid": "int",
    "total": "int",
    "prev_pct": "num?",
    "prev_valid": "int",
    "same_set_pct": "num?",
    "status": "str",
}
ETF_SPEC = {
    "close": "num",
    "chg_pct": "num?",
    "ma_dist": MA_DIST_SPEC,
    "above_ma20": "bool?",
    "dd240_pct": "num?",
    "rsi14": "num?",
    "vol_ratio": "num?",
    "vol_pct250": "num?",
    "vol_label": "str?",
    "pv_label": "str?",
    "vol_flag": "str?",
    "vol_note": "str?",
}
BREADTH_SPEC = {
    "per_n": {str(n): PER_N_SPEC for n in MA_WINDOWS},
    "adv": "int?",
    "dec": "int?",
    "flat": "int?",
    "new_high": "int?",
    "new_low": "int?",
    "universe_label": "str",
    "coverage_note": "str?",
}
GEX_SPEC = {
    "status": "str",
    "at_spot": "num?",
    "sign": "int?",
    "crossings": [{"price": "num", "direction": "str"}],
    "near_zero": "bool?",
    "coverage": "num?",
    "lower_bound": "bool",
    "rate_assumed": "bool",
}
OPTIONS_SPEC = {
    "spot": "num",
    "call_wall": "num?",
    "put_wall": "num?",
    "call_wall_dist_pct": "num?",
    "put_wall_dist_pct": "num?",
    "wall_status": "str",
    "monthly": ("?", {"expiry": "date", "call_wall": "num?", "put_wall": "num?", "max_pain": "num?", "status": "str"}),
    "pcr_oi": "num?",
    "pcr_vol": "num?",
    "iv30": "num?",
    "gex": GEX_SPEC,
    "counts": {"raw": "int", "bad": "int", "conflict": "int", "oi_valid": "int", "gamma_valid": "int", "iv_replaced": "int"},
    "oi_date_note": "str",
}
_ITEMS = "items"
ROW_IDS: dict[str, dict[str, Any]] = {
    **{f"us.etf.{s}": ETF_SPEC for s in ("VOO", "QQQ", "SOXX", "SPY")},
    **{f"us.breadth.{s}": BREADTH_SPEC for s in ("VOO", "QQQ", "SOXX")},
    **{f"us.options.{s}": OPTIONS_SPEC for s in ("QQQ", "SPY", "SOXX")},
    **{
        f"us.cftc.{s}": {
            "contract": "str",
            "lev_net": "int",
            "lev_net_chg": "int?",
            "am_net": "int",
            "am_net_chg": "int?",
            "oi": "int?",
        }
        for s in ("es", "nq")
    },
    "us.margin": {"month": "str", "debit_musd": "num", "yoy_pct": "num?"},
    **{
        f"us.13f.{s}": {
            "rows": [
                {
                    "name": "str",
                    "symbols": ["str"],
                    "weight": "num?",
                    "increased": "int?",
                    "decreased": "int?",
                    "new": "int?",
                    "soldout": "int?",
                    "own_pct": "num?",
                }
            ],
            "summary": {"more_increase": "int", "missing": "int", "total": "int"},
            "holder_period_note": "str?",
            "weight_basis": "str",
        }
        for s in ("VOO", "QQQ", "SOXX")
    },
    **{
        f"us.fund.{s}": {
            "rows": [{"name": "str", "symbols": ["str"], "pe": "num?", "status": "str"}],
            "pe_median": "num?",
            "pe_valid": "int",
            "total": "int",
        }
        for s in ("VOO", "QQQ", "SOXX")
    },
    "tw.taiex": {"close": "num", "chg": "num?", "chg_pct": "num?", "ma_dist": MA_DIST_SPEC, "above_ma20": "bool?", "dd240_pct": "num?"},
    "tw.etf.0050": ETF_SPEC,
    "tw.breadth.0050": BREADTH_SPEC,
    "tw.ad": {"adv": "int", "dec": "int", "flat": "int", "adv_limit": "int?", "dec_limit": "int?", "no_compare": "int?"},
    "tw.turnover": {"value_twd": "num", "vol_ratio": "num?", "vol_pct250": "num?", "vol_label": "str?", "vol_flag": "str?"},
    "tw.inst3": {
        "foreign_yi": "num",
        "trust_yi": "num",
        "dealer_yi": "num",
        "foreign_5d_yi": "num?",
        "trust_5d_yi": "num?",
        "dealer_5d_yi": "num?",
        "days_5d": "int",
    },
    "tw.margin": {"balance_yi": "num", "change_yi": "num?"},
    "tw.futures": {"foreign_net_big": "int", "foreign_net_big_chg": "int?", "foreign_net_mini": "int?", "foreign_net_micro": "int?"},
    "tw.options": {
        "month": "str",
        "spot": "num?",
        "call_wall": "num?",
        "put_wall": "num?",
        "max_pain": "num?",
        "call_wall_dist_pct": "num?",
        "put_wall_dist_pct": "num?",
        "status": "str",
        "pcr_oi_pct": "num?",
        "foreign_call_net": "int?",
        "foreign_put_net": "int?",
    },
    "tw.fund.0050": {"pe_median": "num?", "pe_valid": "int", "total": "int", "rev_month": "str?", "rev_pos": "int", "rev_valid": "int"},
    **{
        f"macro.{s}": {"value": "num", "chg": "num?", "chg_pct": "num?", "source": "str"}
        for s in (
            "VIX", "VIX3M", "UST3M", "UST10Y", "DXY", "WTI", "BRENT", "GOLD", "SILVER", "BTC", "USDTWD",
            "RUT", "DJI", "IXIC", "MOVE", "SPY_DD",
        )
    },
    "events.next": {_ITEMS: [{"name": "str", "at": "num", "kind": "str"}]},
    "season.now": {
        "month": "int",
        "midterm_year": "bool",
        "sp500": ("?", STATS_SPEC),
        "sp500_q4": ("?", STATS_SPEC),
        "sp500_midterm": ("?", STATS_SPEC),
        "sp500_midterm_q4": ("?", STATS_SPEC),
        "taiex": ("?", STATS_SPEC),
        "taiex_q4": ("?", STATS_SPEC),
        "taiex_midterm": ("?", STATS_SPEC),
        "taiex_pending": "bool",
    },
    "election": {
        "vote_date": "date",
        "days_left": "int?",
        "state": "str",
        "quotes": [
            {"platform": "str", "chamber": "str", "outcome": "str", "mid": "num?", "last": "num?", "updated_utc": "num?"}
        ],
    },
    "trump.summary": {"topics": {"*": "int"}, "latest_utc": "num?", "at_least": "bool"},
    **{f"news.{s}": {_ITEMS: [{"title": "str", "source": "str?", "published_utc": "num"}]} for s in ("us", "tw", "iran")},
    "trump.posts": {_ITEMS: [{"excerpt": "str", "published_utc": "num", "quoted_author": "str?", "topics": ["str"]}]},
    "oneliner.us": {"text": "str?"},
    "oneliner.tw": {"text": "str?"},
    "contra.us": {"codes": ["str"], "text": "str?"},
    "contra.tw": {"codes": ["str"], "text": "str?"},
}
CHART_KEYS = ("QQQ", "SPY", "SOXX", "TXO")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _base_ok(base: str, value: Any) -> bool:
    if base == "num":
        return _is_num(value)
    if base == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if base == "str":
        return isinstance(value, str)
    if base == "bool":
        return isinstance(value, bool)
    if base == "date":
        if not (isinstance(value, str) and _ISO_DATE_RE.fullmatch(value)):
            return False
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True
    if base == "any":
        return True
    raise ValueError(f"unknown spec {base!r}")


def spec_ok(spec: Any, value: Any) -> bool:
    """依上面的欄位型別表檢查一個值（bot 讀快照時型別錯 → 那格暫缺；job 寫快照前自我驗）。"""
    if isinstance(spec, str):
        if value is None:
            return spec.endswith("?")
        return _base_ok(spec.rstrip("?"), value)
    if isinstance(spec, tuple) and len(spec) == 2 and spec[0] == "?":
        return value is None or spec_ok(spec[1], value)
    if isinstance(spec, list):
        return isinstance(value, list) and all(spec_ok(spec[0], item) for item in value)
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            return False
        if "*" in spec:
            return all(isinstance(k, str) and spec_ok(spec["*"], v) for k, v in value.items())
        return all(key in value and spec_ok(sub, value[key]) for key, sub in spec.items())
    raise ValueError("bad spec")


def values_ok(row_id: str, values: Any) -> bool:
    spec = ROW_IDS.get(row_id)
    return spec is not None and spec_ok(spec, values)


ROW_ENVELOPE_SPEC = {
    "values": "any",
    "data_date": "date?",
    "period": "str?",
    "source": "str",
    "basis": "str?",
    "stale_after": "num",
    "state": "str",
}


def row_ok(row_id: str, row: Any) -> bool:
    """一整列（values＋資料日、來源、stale_after、state）；state=never 時 values 可以是空的。"""
    if not spec_ok(ROW_ENVELOPE_SPEC, row) or row["state"] not in ROW_STATES:
        return False
    if "label" in row and not isinstance(row["label"], str):
        return False
    if row["state"] == "never":
        return isinstance(row["values"], dict)
    return values_ok(row_id, row["values"])


def etf_values(tech: Technicals, volume: VolumeState) -> dict:
    """組 `us.etf.<SYM>`／`tw.etf.0050` 的 values。"""
    return {
        "close": tech.close,
        "chg_pct": tech.chg_pct,
        "ma_dist": {str(n): tech.ma_dist.get(n) for n in MA_WINDOWS},
        "above_ma20": tech.above.get(20),
        "dd240_pct": tech.dd240_pct,
        "rsi14": tech.rsi14,
        "vol_ratio": volume.ratio,
        "vol_pct250": volume.pct250,
        "vol_label": volume.label,
        "pv_label": volume.pv_label,
        "vol_flag": volume.day_flag,
        "vol_note": volume.note,
    }


def breadth_values(
    per_n: Mapping[int, Breadth],
    ad: AdCounts | None,
    highs_lows: HighsLows | None,
    *,
    universe_label: str,
    coverage_note: str | None = None,
) -> dict:
    """組 `us.breadth.<SYM>`／`tw.breadth.0050` 的 values（台股漲跌家數用證交所漲跌欄，傳進來的 ad 自己組）。"""
    return {
        "per_n": {
            str(n): {
                "above": b.above,
                "below": b.below,
                "equal": b.equal,
                "new_below": b.new_below,
                "new_above": b.new_above,
                "valid": b.valid,
                "total": b.total,
                "prev_pct": b.prev_pct,
                "prev_valid": b.prev_valid,
                "same_set_pct": b.same_set_pct,
                "status": b.status,
            }
            for n, b in ((n, per_n[n]) for n in MA_WINDOWS)
        },
        "adv": ad.adv if ad else None,
        "dec": ad.dec if ad else None,
        "flat": ad.flat if ad else None,
        "new_high": highs_lows.new_high if highs_lows else None,
        "new_low": highs_lows.new_low if highs_lows else None,
        "universe_label": universe_label,
        "coverage_note": coverage_note,
    }


# 美國經濟事件的卡片標籤（PLAN §6：先中文、括號英文；每個都 ≤24 字）。鍵是 market_sources_us 白名單裡的英文名，
# 不是來源原字串；卡片程式（market_dashboard）直接查這份，不過 clean_external（NFKC 會把全形括號變半形）。
US_EVENT_LABELS: dict[str, str] = {
    "CPI": "消費者物價（CPI）",
    "Core CPI": "核心消費者物價（Core CPI）",
    "PPI": "生產者物價（PPI）",
    "Core PPI": "核心生產者物價（Core PPI）",
    "Nonfarm Payrolls": "非農就業（NFP）",
    "Unemployment Rate": "失業率（Unemployment）",
    "GDP": "經濟成長率（GDP）",
    "PCE Price Index": "個人消費支出物價（PCE）",
    "Core PCE Price Index": "核心 PCE 物價（Core PCE）",
    "Retail Sales": "零售銷售（Retail Sales）",
    "Core Retail Sales": "核心零售銷售（Core Retail）",
    "ISM Manufacturing PMI": "製造業採購經理人指數（ISM）",
    "ISM Services PMI": "服務業採購經理人指數（ISM）",
    "Initial Jobless Claims": "初領失業金（Jobless Claims）",
    "JOLTS Job Openings": "職缺數（JOLTS）",
    "Michigan Consumer Sentiment": "密大消費者信心（Michigan）",
    "FOMC Minutes": "聯準會會議紀錄（FOMC）",
    "Fed Chair Speaks": "聯準會主席談話（Fed Chair）",
}


# 川普貼文主題的先後（和 market_sources_misc 的主題表同一順序，test_market_sources_misc 檢查）：卡上同票時照這個排，
# 快照是 sort_keys 寫的、保不住 dict 順序（2026-10-18：照字碼排會讓「關稅」掉出摘要的前 3 名）
TRUMP_TOPIC_ORDER = ("關稅", "伊朗", "聯準會", "利率", "油價", "中國", "以色列", "停火", "台灣", "半導體", "股市", "北約", "俄烏")


# 卡片、altText 會用到的全部固定句（測試用：禁用字、validator、年份）
FIXED_TEXTS = tuple(
    value for name, value in sorted(globals().items()) if name.startswith("TEXT_") and isinstance(value, str)
) + tuple(CONTRA_TEXT.values()) + tuple(VOL_PHRASE.values()) + tuple(GEX_DIRECTION_TEXT.values()) + VOL_LABELS + PV_LABELS + (FLAG_SETTLEMENT, FLAG_EARLY_CLOSE) + tuple(US_EVENT_LABELS.values())
