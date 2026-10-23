"""市場盤勢（/盤勢，2026-10-17）：bot 端只讀「顯示用快照」，組 4 張摘要卡、細節卡與期權圖回覆。

Andrew 2026-10-17：「我想要知道目前市場的狀況」——美股／台股的技術面、寬度（站上／跌破均線的家數、
漲跌家數）、量能（「不能只有量，我要知道當前量是多還是少」）、籌碼、期權、13F、槓桿、外資與消息面；
決定 1A「按了才回」（只用 reply token、不主動推）；10/17「字卡字太小」→ 每張卡另有「🔍 看大字版」
（main 經 card_pages 加，這裡不加）。

- 只讀 launchd job 寫好的 state/market_dashboard.json（market_paths.read_private_file：O_NOFOLLOW、
  一般檔、自己的、≤2 MB），依 (st_ino, st_mtime_ns, st_size) 快取解析結果；不連網、不叫模型、
  不帶交易日曆。新鮮度只比 now > stale_after（job 依市場日曆算好）。列 id 與 values 欄位照
  market_calc.ROW_IDS（job 與 bot 同一份）；某個欄位型別不對只讓那格暫缺。
- 快照不存在、讀不了、格式不對 → 「盤勢：資料準備中，請過幾分鐘再按一次」，不丟例外。
- 停用＝旗標檔 state/market_dashboard.disabled（每次都看；看不到也當停用）→「盤勢：暫停使用」。
- 卡上只有固定句型、程式算的數字與日期、官方代號。外部字串（新聞標題、川普原文、持股名稱、事件名稱）
  只進 Flex 的 text 節點，每則都再過 market_calc.clean_external 與 output_validator（ok 且原樣），
  過不了就丟那則並記數（一則標題不能讓整張卡失效）。快照裡的字 job 已經清洗過：還帶著 @、控制字元、
  零寬字元、雙向控制或孤立代理字元的，代表沒洗或被改過，整則丟掉（PLAN §19.1 的測試案例）。
  job 用固定句型寫的註記（成分清單、申報期間…）一樣要過這些檢查，另外不能有禁用字；原樣顯示，
  不再 NFKC（不然全形括號、逗號會被換成半形）。
- Reply.text／alt_text／image_alt 只由固定句型、程式算的數字與日期組成：文字回覆路徑會把「@all」
  「@家人」變成真的提及，_prepare_outbound_text 也可能整則換掉。矛盾列用代碼查 market_calc 的固定句，
  一句話要完全符合固定句型才放進 altText。
- 版面（PLAN §6）：摘要 4 張同一個 size、每張 ≤6 列、每列 ≤24 個中文字寬（全形算 1、半形算 0.5）、
  先中文後括號英文、標題帶張數與資料日；按鈕是可點的 box＋xxl 字＋message action；顏色只用藍
  #1E5AA8、橘 #B7791F、灰 #555555／#666666（台股紅漲綠跌，不用紅綠）。只描述狀態，不是買賣建議。
- 不 import main、card_pages、stock_picks、market_sources_*、numpy、pandas、matplotlib（market_calc
  只在算 GEX 時才載 numpy，這裡不呼叫）；import 時不做 I/O。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional, Sequence
from zoneinfo import ZoneInfo

import market_calc
import market_paths
import output_validator

logger = logging.getLogger("market_dashboard")
_TPE = ZoneInfo("Asia/Taipei")
_ET = ZoneInfo("America/New_York")

# ── 常數 ─────────────────────────────────────────────────────────────────────
ALT_PREFIX = "盤勢："
CHART_ALT_PREFIX = "盤勢圖："
COMMAND = "/盤勢"

MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_OUTCOME_BYTES = 1024 * 1024
SUMMARY_BUBBLE_MAX_BYTES = 6656  # 6.5 KB
# card_pages 對整張卡只給 30 KB、超過就悄悄不加大字版按鈕；每張 🔍 約 0.4–0.6 KB，所以卡片自己 ≤28 KB
CARD_MAX_BYTES = 28 * 1024
MAX_SUMMARY_ROWS = 6
MAX_ROW_WIDTH = 24
MAX_ALT_CHARS = 400
MAX_BUBBLES = 12
MAX_CHART_FILES = 5  # 一個 reply 最多 5 則
MAX_CHART_BYTES = 10 * 1024 * 1024  # LINE 圖片上限

BLUE, ORANGE = market_calc.COLOR_BLUE, market_calc.COLOR_ORANGE
GRAY = market_calc.COLOR_GREY

# 外部字串長度上限（PLAN §10：標題 40、名稱 24、川普摘錄 100）
TITLE_MAX = market_calc.MAX_TITLE
NAME_MAX = market_calc.MAX_NAME
EXCERPT_MAX = market_calc.MAX_TRUMP_EXCERPT
NOTE_MAX = 60  # job 用固定句型寫的註記（成分清單、覆蓋率、申報期間）

# 每類最多列幾則：快照裡的清單再長也只看前面這些（大小、時間都有上限）
NEWS_PER_KIND = 5
IRAN_NEWS_MAX = 4
TRUMP_POSTS_MAX = 4
EVENTS_MAX = 6
HOLDINGS_MAX = 10
CROSSINGS_MAX = 2
TOPICS_MAX = 3
QUOTES_MAX = 4
NEWS_MAX_AGE_S = market_calc.NEWS_TTL.total_seconds()
TRUMP_MAX_AGE_S = market_calc.TRUMP_TTL.total_seconds()

# VIX 的白話狀態（一般慣用的 20／30 分界；只描述，不判斷多空）
VIX_CALM_BELOW = 20.0
VIX_FEAR_FROM = 30.0

ELECTION_DAY = date(2026, 11, 3)  # 美東日期
ELECTION_LAST_DAY = date(2027, 2, 1)  # Kalshi 結算上限；過了一律當本屆結束

# 固定句：和 job 共用的放在 market_calc；這裡只有 bot 自己用的
TEXT_NOT_READY = market_calc.TEXT_PREPARING
TEXT_DISABLED = market_calc.TEXT_DISABLED
TEXT_DISCLAIMER = market_calc.TEXT_DISCLAIMER
TEXT_MISSING = market_calc.TEXT_MISSING
TEXT_INSUFFICIENT = market_calc.TEXT_INSUFFICIENT
TEXT_PERIOD_UNKNOWN = market_calc.TEXT_UNKNOWN_PERIOD
TEXT_MM = market_calc.TEXT_MM_TRUMP
TEXT_US_BASIS = market_calc.TEXT_US_NO_DIVIDEND
TEXT_TW_BASIS = market_calc.TEXT_TW_ADJUSTED
TEXT_CARD_FAILED = "盤勢：卡片暫時出不來，請過幾分鐘再按一次"
TEXT_CHART_UNAVAILABLE = "盤勢圖：這張圖還沒畫好或已經過期，請過幾分鐘再按一次"
TEXT_NO_13F_0050 = "盤勢：0050 是台股 ETF，沒有 13F 資料；台股細節請打 /盤勢 0050"
TEXT_ELECTION_ENDED = "盤勢：本屆期中選舉已結束"
TEXT_SWIPE = "往左滑還有 3 張"
TEXT_NEWS_NOTE = "新聞標題是第三方原文，只照錄、不代表程式判斷"
TEXT_GAMMA_NOTE = "假設部位下的 gamma 是估計：假設造市商持有買權、賣出賣權"
TEXT_SHARE_CLASS = "同一公司的不同股別合併成一家（例 GOOGL＋GOOG），權重相加"
TEXT_FLOW_NOTE = "FINRA 是月資料、落後 3–4 週"
TEXT_TAIPEI_TIME = "時間為台北時間"  # 細節卡印了時刻、但沒有逐列寫「（台北）」的：footer 補一句
TEXT_PROXY_NO_CHANGE = "暫無漲跌幅"  # ETF 代理沒有日漲跌幅：不拿 ETF 的股價頂替
TEXT_MID_MISSING = "買賣價中間值暫缺"
TEXT_SETTLED_YES = "已結算，民主黨拿下"  # 預測市場的結算（Yes＝民主黨）
TEXT_SETTLED_NO = "已結算，民主黨未拿下"
TEXT_SEASON_SOURCE = "來源：datahub（Shiller 月均價）、FinMind（加權指數）"
TEXT_USAGE = "\n".join(
    (
        "盤勢：指令用法",
        "/盤勢 → 4 張摘要卡（美股、台股、期權與資金、宏觀與事件）",
        "/盤勢 VOO、/盤勢 QQQ、/盤勢 SOXX、/盤勢 0050 → 細節",
        "/盤勢 期權 → 期權與資金細節",
        "/盤勢 QQQ 圖（也可以 SPY、SOXX、台指）→ 期權圖",
        "/盤勢 13F QQQ（也可以 VOO、SOXX）→ 前十大的機構持股",
        "/盤勢 新聞、/盤勢 季節性、/盤勢 選舉",
        TEXT_DISCLAIMER,
    )
)

# 程式自己的句子不能有的字（PLAN §6）：market_calc 的表再加幾個同義的；固定免責句是精確例外，
# 13F 的「加碼／減碼」描述機構、允許。「買進／賣出」不列：描述部位要用（「假設造市商…賣出賣權」）。
FORBIDDEN_WORDS = tuple(
    dict.fromkeys((*market_calc.FORBIDDEN_WORDS, "停利", "逢高", "佈局", "可以賣", "追高", "殺低"))
)

# ── 指令 ─────────────────────────────────────────────────────────────────────
SUMMARY, ETF, TW, OPTIONS, NEWS, SEASON, ELECTION, F13, NO_13F, CHART = (
    "summary", "etf", "tw", "options", "news", "season", "election", "13f", "no_13f", "chart",
)
US_ETFS = ("VOO", "QQQ", "SOXX")
CHART_SYMBOLS = ("QQQ", "SPY", "SOXX", "台指")
# 指令上的標的 → 快照 charts 的鍵（market_calc.CHART_KEYS）
CHART_KEYS = {"QQQ": "QQQ", "SPY": "SPY", "SOXX": "SOXX", "台指": "TXO"}

# 卡片按鈕會送出的所有字：舊卡片一直留在聊天紀錄裡可以點，只能加不能刪（test_market_routing 同一份）。
SHIPPED_MARKET_TEXTS: tuple[str, ...] = (
    "/盤勢",
    "/盤勢 VOO",
    "/盤勢 QQQ",
    "/盤勢 SOXX",
    "/盤勢 0050",
    "/盤勢 期權",
    "/盤勢 新聞",
    "/盤勢 季節性",
    "/盤勢 選舉",
    "/盤勢 QQQ 圖",
    "/盤勢 SPY 圖",
    "/盤勢 SOXX 圖",
    "/盤勢 台指 圖",
    "/盤勢 13F QQQ",
    "/盤勢 13F VOO",
    "/盤勢 13F SOXX",
)

_BODY_MAX = 40
_TRAILING = "?？!！。.,，~～ "
_WORD_COMMANDS = {"期權": OPTIONS, "新聞": NEWS, "季節性": SEASON, "選舉": ELECTION}
_13F_RE = re.compile(r"13F ?(VOO|QQQ|SOXX|0050)")
_CHART_RE = re.compile(r"(QQQ|SPY|SOXX|台指) ?圖")


def parse_command(body: Optional[str]) -> Optional[tuple[str, Optional[str]]]:
    """「/盤勢」後面的字 → (指令, 標的)；不認得回 None（回用法說明）。

    先刪 Cf（零寬字元等）再 NFKC、壓空白、去頭尾標點、英文轉大寫：「/盤勢　qqq圖？」也接得到。
    """
    raw = "".join(ch for ch in (body or "")[: _BODY_MAX * 4] if unicodedata.category(ch) != "Cf")
    text = " ".join(unicodedata.normalize("NFKC", raw).split()).strip(_TRAILING).upper()
    if len(text) > _BODY_MAX:
        return None
    if not text:
        return SUMMARY, None
    if text in US_ETFS:
        return ETF, text
    if text == "0050":
        return TW, text
    if text in _WORD_COMMANDS:
        return _WORD_COMMANDS[text], None
    match = _13F_RE.fullmatch(text)
    if match:
        return (NO_13F, "0050") if match.group(1) == "0050" else (F13, match.group(1))
    match = _CHART_RE.fullmatch(text)
    if match:
        return CHART, match.group(1)
    return None


# ── 回覆 ─────────────────────────────────────────────────────────────────────
@dataclass
class Reply:
    text: Optional[str] = None  # 純文字回覆（用法說明、資料準備中…）
    flex: Optional[dict] = None  # Flex（carousel 或 bubble）
    alt_text: Optional[str] = None
    fallback_text: Optional[str] = None
    images: Optional[list[str]] = None  # 圖檔名（32hex.png），main 負責組網址與送出
    image_alt: Optional[str] = None  # 寫進 raw_messages 的「盤勢圖：…」


# ── 讀快照 ───────────────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_cache: dict[str, tuple[tuple[int, int, int], Optional[dict]]] = {}
_CACHE_MAX = 4


def clear_cache() -> None:
    """測試用：清掉快照的解析快取。"""
    with _cache_lock:
        _cache.clear()


def _parse_snapshot(raw: bytes) -> Optional[dict]:
    try:
        # NaN／Infinity 一律當 null：now > NaN 永遠是 False，過期的格子會一直顯示成新的
        data = json.loads(raw.decode("utf-8"), parse_constant=lambda _name: None)
    except Exception:  # 壞 UTF-8、壞 JSON、巢狀太深（RecursionError）都只是「讀不了」
        return None
    schema = data.get("schema") if isinstance(data, dict) else None
    if type(schema) is not int or schema != market_calc.SNAPSHOT_SCHEMA or not isinstance(data.get("rows"), dict):
        return None
    return data


def _load_snapshot(paths: Any) -> Optional[dict]:
    found = market_paths.read_private_file(paths.snapshot, MAX_SNAPSHOT_BYTES)
    if found is None:
        return None
    raw, key = found
    name = str(paths.snapshot)
    with _cache_lock:
        hit = _cache.get(name)
        if hit is not None and hit[0] == key:
            return hit[1]
    parsed = _parse_snapshot(raw)
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[name] = (key, parsed)
    return parsed


# ── 值的型別檢查與格式 ────────────────────────────────────────────────────────
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONTH_RE = re.compile(r"(\d{4})-?(\d{2})")
_SYMBOL_RE = re.compile(r"[A-Z]{1,5}(?:\.[A-Z])?")
_CHART_NAME_RE = re.compile(r"[0-9a-f]{32}\.png")
_ROW_ID_RE = re.compile(r"[a-z0-9]+(?:\.[A-Za-z0-9_]+){0,3}")
_KIND_RE = re.compile(r"[A-Za-z0-9_.:-]{1,48}")
_PROXY_RE = re.compile(r"ETF 代理：([A-Z]{1,5})")
_SUSPICIOUS_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_BIG = 1e15


def _num(value: Any, lo: float = -_BIG, hi: float = _BIG) -> Optional[float]:
    """有限的數字（不收 bool），範圍外當缺值：1e308 印出來會撐爆版面。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and lo <= number <= hi else None


def _count(value: Any, hi: int = 10**8) -> Optional[int]:
    number = _num(value, 0, hi)
    return int(number) if number is not None and number == int(number) else None


def _signed_int(value: Any, lim: int = 10**9) -> Optional[int]:
    number = _num(value, -lim, lim)
    return int(number) if number is not None and number == int(number) else None


def _flag(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any, limit: int) -> list:
    return list(value[:limit]) if isinstance(value, list) else []


def _day(value: Any) -> Optional[date]:
    if not isinstance(value, str) or not _DAY_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _month_of(value: Any) -> Optional[int]:
    """「2026-09」「202609」→ 9。"""
    match = _MONTH_RE.fullmatch(value) if isinstance(value, str) else None
    month = int(match.group(2)) if match else 0
    return month if 1 <= month <= 12 else None


def _moment(value: Any) -> Optional[datetime]:
    """UTC 時刻：epoch 秒（ROW_IDS 的 num）；ISO 字串要帶時區，沒有的不收（不猜）。"""
    seconds = _num(value, 0, 10**11)
    if seconds is not None:
        return datetime.fromtimestamp(seconds, timezone.utc)
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _md(day: date) -> str:
    return market_calc.fmt_md(day)


def _md_hm(moment: datetime) -> str:
    local = moment.astimezone(_TPE)
    return f"{_md(local.date())} {local:%H:%M}"


def _price(value: float) -> str:
    """價格：1000 以上不帶小數，其餘最多兩位小數（同 /股票）。"""
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _signed(value: float, digits: int = 2, unit: str = "") -> str:
    rounded = round(value, digits)
    if rounded == 0:
        return f"{0:.{digits}f}{unit}"
    return f"{rounded:+,.{digits}f}{unit}"


def _pct(value: float, digits: int = 1) -> str:
    return _signed(value, digits, "%")


def _plain_pct(value: float, digits: int = 0) -> str:
    return f"{value:.{digits}f}%"


def _amount_yi(yi: float) -> str:
    """億元（不帶正負號）：1000 億以上不帶小數。"""
    return f"{abs(yi):,.0f}" if abs(yi) >= 1000 else f"{abs(yi):,.1f}"


def _contracts(value: int) -> str:
    return f"{abs(value):,}"


def row_width(text: str) -> float:
    """一列的寬：全形（W／F）算 1 個中文字，其他算 0.5。"""
    return sum(1.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 0.5 for ch in text)


def _clip(text: str, width: float = MAX_ROW_WIDTH) -> str:
    """最後的保險：超寬就從尾巴切掉、補「…」（外部名稱特別長時才會用到）。

    切點落在數字中間就整個數字拿掉，不印半個數字（2026-10-23 審查 r7 #2：「+12.34%」被切成「+12.…」）。"""
    if row_width(text) <= width:
        return text
    keep = 0
    used = 0.0
    for ch in text:
        used += row_width(ch)
        if used > width - 1:
            break
        keep += 1
    return text[: market_calc.number_safe_cut(text, keep)].rstrip() + "…"


def _fit(*variants: Optional[str]) -> str:
    """第一個不超過 24 個中文字寬的寫法（變體由長到短排）；都太寬就切最短的那個。"""
    candidates = [v for v in variants if v]
    for variant in candidates:
        if row_width(variant) <= MAX_ROW_WIDTH:
            return variant
    return _clip(candidates[-1])


def has_forbidden(text: str) -> bool:
    """程式自己的句子有沒有禁用字；固定免責句整句扣掉再查。"""
    plain = text.replace(TEXT_DISCLAIMER, "")
    return any(word in plain for word in FORBIDDEN_WORDS)


def _suspicious(text: str) -> bool:
    return any(ch in "@＠" or unicodedata.category(ch) in _SUSPICIOUS_CATEGORIES for ch in text)


# ── 每一列的狀態 ─────────────────────────────────────────────────────────────
FRESH, STALE, MISSING = "fresh", "stale", "missing"


@dataclass(frozen=True)
class _View:
    status: str
    values: dict
    data_date: Optional[date] = None
    period_unknown: bool = False

    @property
    def fresh(self) -> bool:
        return self.status == FRESH


class _Ctx:
    """一次回覆用的狀態：快照、now、丟掉幾則外部字串。"""

    def __init__(self, snapshot: dict, now: float):
        self.snapshot = snapshot
        self.rows: dict = snapshot["rows"]
        self.now = now
        self.now_dt = datetime.fromtimestamp(now, _TPE)
        self.today = self.now_dt.date()
        self.dropped = 0

    def row(self, row_id: str) -> _View:
        raw = self.rows.get(row_id)
        if not isinstance(raw, dict):
            return _View(MISSING, {})
        day = _day(raw.get("data_date"))
        values = raw.get("values")
        stale_after = _num(raw.get("stale_after"))
        state = raw.get("state")
        if state not in ("ok", "unknown_period") or not isinstance(values, dict) or stale_after is None:
            return _View(MISSING, {}, day)  # state=never、壞列：暫缺
        if self.now > stale_after:
            return _View(STALE, {}, day)
        return _View(FRESH, values, day, state == "unknown_period")

    def gap(self, view: _View, *, short: bool = False) -> str:
        """不顯示數字時的那格：過期（資料日 X/X）或暫缺。"""
        if view.status == STALE:
            if short or view.data_date is None:
                return "過期"
            return market_calc.TEXT_STALE.format(md=_md(view.data_date))
        return TEXT_MISSING

    def _passes(self, text: str) -> bool:
        result = output_validator.validate_outbound_text(text, now=self.now_dt)
        return result.ok and result.text == text

    def external(self, raw: Any, max_len: int) -> Optional[str]:
        """第三方字串（標題、名稱、摘錄）進卡片前的最後一關；回清洗後的字，過不了回 None 並記數。"""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None  # 沒給（或空字串，例如一句話沒有主指數時）不算丟
        if not isinstance(raw, str) or len(raw) > max_len * 8 or _suspicious(raw):
            self.dropped += 1
            return None
        try:
            cleaned = market_calc.clean_external(raw, max_len)
        except Exception:
            cleaned = None
        if not isinstance(cleaned, str) or not cleaned.strip() or _suspicious(cleaned) or not self._passes(cleaned):
            self.dropped += 1
            return None
        return cleaned

    def note(self, raw: Any, max_len: int = NOTE_MAX) -> Optional[str]:
        """job 用固定句型寫的註記：同樣的檢查，另外不能有禁用字、HTML；原樣顯示（保留全形標點）。"""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        if (
            not isinstance(raw, str)
            or len(raw) > max_len
            or _suspicious(raw)
            or "<" in raw
            or ">" in raw
            or has_forbidden(raw)
        ):
            self.dropped += 1
            return None
        try:
            ok = market_calc.clean_external(raw, max_len) is not None  # 網址、email、手機號
        except Exception:
            ok = False
        if not ok or not self._passes(raw):
            self.dropped += 1
            return None
        return raw

    def any_data(self) -> bool:
        return any(isinstance(r, dict) and r.get("state") in ("ok", "unknown_period") for r in self.rows.values())

    def progress(self) -> Optional[tuple[int, int]]:
        members = _dict(self.snapshot.get("progress")).get("us_members")
        if not isinstance(members, list) or len(members) != 2:
            return None
        done, total = _count(members[0], 10**5), _count(members[1], 10**5)
        if done is None or total is None or total == 0 or done >= total:
            return None
        return done, total

    def generated(self) -> Optional[datetime]:
        stamp = _num(self.snapshot.get("generated_at"), 0, 10**11)
        return datetime.fromtimestamp(stamp, _TPE) if stamp is not None else None


# ── Flex 元件 ────────────────────────────────────────────────────────────────
def _text(text: str, *, size: str = "lg", color: Optional[str] = None, weight: Optional[str] = None) -> dict:
    node: dict = {"type": "text", "text": text, "size": size, "wrap": True}
    if color:
        node["color"] = color
    if weight:
        node["weight"] = weight
    return node


def _sep() -> dict:
    return {"type": "separator", "margin": "md"}


def _button(label: str, command: str) -> dict:
    """可以點的 box（Flex 的 button 不能調字級；作法同 flex_menu.build_large_menu）；外框與字用藍。"""
    return {
        "type": "box",
        "layout": "vertical",
        "margin": "md",
        "borderWidth": "medium",
        "borderColor": BLUE,
        "cornerRadius": "lg",
        "paddingAll": "md",
        "action": {"type": "message", "label": label, "text": command},
        "contents": [
            {"type": "text", "text": label, "size": "xxl", "weight": "bold", "color": BLUE, "align": "center",
             "wrap": True}
        ],
    }


def _bubble(
    title: str,
    body: list[dict],
    *,
    subtitle: Optional[str] = None,
    notes: Sequence[str] = (),
    buttons: Sequence[tuple[str, str]] = (),
) -> dict:
    header = [_text(title, size="xl", weight="bold")]
    if subtitle:
        header.append(_text(subtitle, size="md", color=GRAY))
    footer = [_text(note, size="md", color=GRAY) for note in notes]
    footer.append(_text(TEXT_DISCLAIMER, size="md", color=GRAY))
    footer += [_button(label, command) for label, command in buttons]
    return {
        "type": "bubble",
        "size": "giga",
        "header": {"type": "box", "layout": "vertical", "contents": header},
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": body or [_text(TEXT_MISSING)]},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": footer},
    }


def _carousel(bubbles: Sequence[dict]) -> dict:
    return {"type": "carousel", "contents": list(bubbles)}


def card_bytes(card: dict) -> int:
    """卡片大小：和 card_pages.fits_line_limits 同一種量法（預設分隔符、UTF-8）。"""
    return len(json.dumps(card, ensure_ascii=False).encode("utf-8"))


def _flex_reply(card: dict, alt_text: str, *, summary: bool = False) -> Reply:
    """送出前自己再量一次：超過上限就回固定句（LINE 回 400 整張卡就沒了）。"""
    bubbles = card["contents"] if card.get("type") == "carousel" else [card]
    if (
        card_bytes(card) > CARD_MAX_BYTES
        or len(bubbles) > MAX_BUBBLES
        or len(alt_text) > MAX_ALT_CHARS
        or (summary and any(card_bytes(b) > SUMMARY_BUBBLE_MAX_BYTES for b in bubbles))
    ):
        logger.error("market dashboard card over budget bytes=%d bubbles=%d", card_bytes(card), len(bubbles))
        return Reply(text=TEXT_CARD_FAILED)
    return Reply(flex=card, alt_text=alt_text, fallback_text=TEXT_CARD_FAILED)


# ── 共用的列 ─────────────────────────────────────────────────────────────────
_US_NAMES = {"VOO": "標普500（VOO）", "QQQ": "那斯達克100（QQQ）", "SOXX": "半導體（SOXX）", "SPY": "標普500（SPY）"}
_US_SHORT = {"VOO": "標普500", "QQQ": "那斯達克", "SOXX": "半導體", "SPY": "標普500"}
_VOL_FLAGS = (market_calc.FLAG_SETTLEMENT, market_calc.FLAG_EARLY_CLOSE)
_MA_NAMES = (("20", "月線"), ("60", "季線"), ("120", "半年線"), ("240", "年線"))
_SUMMARY_JARGON = ("GEX", "PCR", "IV30", "最大痛點", "CFTC", "gamma")


def _word(value: Any, allowed: Sequence[str]) -> Optional[str]:
    """job 給的狀態詞只收固定詞表裡的（等於用我們自己的字），其他當沒有。"""
    return value if isinstance(value, str) and value in allowed else None


def _above_word(above: Optional[bool]) -> Optional[str]:
    if above is None:
        return None
    return "在月線上" if above else "在月線下"


def _above_color(above: Optional[bool]) -> str:
    return GRAY if above is None else BLUE if above else ORANGE


def _vol_phrase(value: Any) -> Optional[str]:
    """資料值（明顯量縮…爆量）→ 顯示的白話（量明顯偏少…爆量）：和一句話同一份表 market_calc.VOL_PHRASE。"""
    label = _word(value, market_calc.VOL_LABELS)
    return market_calc.VOL_PHRASE[label] if label else None


def _vol_word(values: dict) -> Optional[str]:
    """量能的白話詞；來源不同（CBOE 備援）那天寫「暫不判斷」。"""
    if values.get("vol_note") == market_calc.TEXT_VOL_SOURCE_DIFF:
        return "暫不判斷"
    return _vol_phrase(values.get("vol_label"))


def _vol_short(word: str) -> str:
    """列放不下時拿掉開頭的「量」（量偏少 → 偏少）：那一列已經寫了「量能：」。"""
    return word[1:] if word.startswith("量") and len(word) > 1 else word


def _date_suffix(day: Optional[date], main_day: Optional[date]) -> str:
    return f"（{_md(day)}）" if day is not None and main_day is not None and day != main_day else ""


def _latest_day(views: Sequence[_View]) -> Optional[date]:
    days = [v.data_date for v in views if v.fresh and v.data_date is not None]
    return max(days) if days else None


def _etf_line(ctx: _Ctx, view: _View, name: str, main_day: Optional[date]) -> tuple[str, str]:
    if not view.fresh:
        return _fit(f"{name}{ctx.gap(view)}", f"{name}{ctx.gap(view, short=True)}"), GRAY
    values = view.values
    close = _num(values.get("close"), 0)
    change = _num(values.get("chg_pct"), -100, 1000)
    above = _flag(values.get("above_ma20"))
    status = _above_word(above)
    close_text = _price(close) if close is not None else "收盤暫缺"
    change_text = f" {_pct(change, 2)}" if change is not None else ""
    status_text = f" {status}" if status else ""
    suffix = _date_suffix(view.data_date, main_day)
    text = _fit(
        f"{name}{close_text}{change_text}{status_text}{suffix}",
        f"{name}{close_text}{change_text}{status_text}",
        f"{name}{close_text}{status_text}",
    )
    return text, _above_color(above)


def _breadth_stats(view: _View, n: str = "20") -> Optional[tuple[int, int, int, float]]:
    """(站上, 有效, 總數, 比例)；覆蓋率不夠（status 不是 ok）或數字對不上回 None（＝資料不足）。"""
    stats = _dict(_dict(view.values.get("per_n")).get(n))
    above, valid, total = _count(stats.get("above")), _count(stats.get("valid")), _count(stats.get("total"))
    if stats.get("status") != market_calc.STATUS_OK or above is None or not valid or total is None:
        return None
    if valid > total or above > valid:
        return None
    return above, valid, total, above * 100.0 / valid


# 矛盾句的主詞用中文（先中文後英文）；句型本身是 market_calc.CONTRA_TEXT 的固定句，主詞和 job 共用同一份
CONTRA_SUBJECTS = market_calc.SUBJECT_NAME


def _contras(ctx: _Ctx, row_id: str) -> list[tuple[Optional[str], str]]:
    """矛盾代碼 →（主詞代號, 固定句）；代碼或主詞不認得的跳過：卡上只放固定句，不放快照裡的字。"""
    view = ctx.row(row_id)
    found: list[tuple[Optional[str], str]] = []
    if not view.fresh:
        return found
    for item in _list(view.values.get("codes"), 10):
        if not isinstance(item, str):
            continue
        code, _colon, subject = item.partition(":")
        template = market_calc.CONTRA_TEXT.get(code)
        if template is None or (subject and subject not in CONTRA_SUBJECTS):
            continue
        found.append((subject or None, template.format(subject=CONTRA_SUBJECTS.get(subject, ""))))
    return found


def _contra_sentences(ctx: _Ctx, row_id: str) -> list[str]:
    return [sentence for _subject, sentence in _contras(ctx, row_id)]


def _note_line(ctx: _Ctx, contra_id: str, oneliner_id: str) -> Optional[tuple[str, str]]:
    """矛盾優先（橘），否則一句話（灰）；每張卡最多 1 列，太寬或有術語的不放摘要卡。"""
    for sentence in _contra_sentences(ctx, contra_id):
        if row_width(sentence) <= MAX_ROW_WIDTH and not any(word in sentence for word in _SUMMARY_JARGON):
            return sentence, ORANGE
    one = ctx.row(oneliner_id)
    if one.fresh:
        text = ctx.note(one.values.get("text"), 40)
        if text and row_width(text) <= MAX_ROW_WIDTH:
            return text, GRAY
    return None


def _summary_bubble(
    title: str,
    rows: Sequence[tuple[str, str]],
    *,
    subtitle: Optional[str] = None,
    notes: Sequence[str] = (),
    buttons: Sequence[tuple[str, str]] = (),
) -> dict:
    body = [_text(text, color=color) for text, color in rows[:MAX_SUMMARY_ROWS]]
    return _bubble(title, body, subtitle=subtitle, notes=notes, buttons=buttons)


def _title(name: str, day: Optional[date], suffix: str = "收盤", views: Sequence[_View] = ()) -> str:
    """標題帶資料日；沒有資料日時：這張卡用到的列全都是過期 → 「資料過期」（列上也寫過期），否則「資料暫缺」。"""
    if day is not None:
        return f"{name}｜{_md(day)} {suffix}"
    stale = bool(views) and all(view.status == STALE for view in views)
    return f"{name}｜資料過期" if stale else f"{name}｜資料暫缺"


# ── ① 美股 ──────────────────────────────────────────────────────────────────
def _card_us(ctx: _Ctx) -> dict:
    etfs = {sym: ctx.row(f"us.etf.{sym}") for sym in US_ETFS}
    main_day = _latest_day(list(etfs.values()))
    rows = [_etf_line(ctx, etfs[sym], _US_NAMES[sym], main_day) for sym in US_ETFS]

    parts, days = [], []
    for sym in US_ETFS:
        view = ctx.row(f"us.breadth.{sym}")
        stats = _breadth_stats(view) if view.fresh else None
        if stats is not None:
            parts.append(f"{sym} {_plain_pct(stats[3])}")
            if view.data_date is not None:
                days.append(view.data_date)
        else:
            parts.append(f"{sym} {TEXT_INSUFFICIENT if view.fresh else ctx.gap(view, short=True)}")
    lag = min(days) if days else None
    label = "成分股站上月線" if lag is None or lag == main_day else f"站上月線（{_md(lag)}）"
    joined = "、".join(parts)
    # 三檔都寫「資料不足」會超寬被切成「SOXX 資…」（2026-10-23 審查 r7a N3）：全部不足就合寫一次，否則改短字
    if all(part.endswith(TEXT_INSUFFICIENT) for part in parts):
        rows.append((f"{label}：{TEXT_INSUFFICIENT}", GRAY))
    else:
        short_joined = joined.replace(TEXT_INSUFFICIENT, "不足")
        rows.append((_fit(f"{label}：{joined}", f"站上月線：{joined}", f"站上月線：{short_joined}"), GRAY))

    words, short_words, flags = [], [], set()
    for sym in US_ETFS:
        view = etfs[sym]
        word = _vol_word(view.values) if view.fresh else None
        flag = _word(view.values.get("vol_flag"), _VOL_FLAGS) if view.fresh else None
        if flag:
            flags.add(flag)
        gap = ctx.gap(view, short=True)
        words.append(f"{sym} {word or gap}")
        short_words.append(f"{sym} {_vol_short(word) if word else gap}")
    joined, short_joined = "、".join(words), "、".join(short_words)
    flag_text = f"（{'、'.join(sorted(flags))}）" if flags else ""
    # 放不下先拿掉每個詞開頭的「量」（三檔都寫「量明顯偏少」會超寬），最後才拿掉結算日旗標
    rows.append((_fit(f"量能{flag_text}：{joined}", f"量能{flag_text}：{short_joined}", f"量能：{joined}",
                      f"量能：{short_joined}"), GRAY))

    extra = _note_line(ctx, "contra.us", "oneliner.us")
    if extra:
        rows.append(extra)
    notes = []
    progress = ctx.progress()
    if progress:
        notes.append("美股" + market_calc.TEXT_BOOTSTRAP.format(done=progress[0], total=progress[1]))
    return _summary_bubble(
        _title("美股（1/4）", main_day, views=list(etfs.values())),
        rows,
        subtitle=TEXT_SWIPE,
        notes=notes,
        buttons=(("標普500 細節", "/盤勢 VOO"), ("那斯達克 細節", "/盤勢 QQQ"), ("半導體 細節", "/盤勢 SOXX")),
    )


# ── ② 台股 ──────────────────────────────────────────────────────────────────
def _taiex_line(ctx: _Ctx, view: _View) -> tuple[str, str]:
    name = "加權指數"
    if not view.fresh:
        return f"{name} {ctx.gap(view)}", GRAY
    close = _num(view.values.get("close"), 0)
    change = _num(view.values.get("chg_pct"), -100, 1000)
    above = _flag(view.values.get("above_ma20"))
    close_text = _price(close) if close is not None else "收盤暫缺"
    change_text = f"（{_pct(change, 2)}）" if change is not None else ""
    status = _above_word(above)
    text = _fit(f"{name} {close_text}{change_text}{' ' + status if status else ''}", f"{name} {close_text}{change_text}")
    return text, _above_color(above)


def _turnover_line(ctx: _Ctx, view: _View, main_day: Optional[date]) -> str:
    if not view.fresh:
        return f"成交金額 {ctx.gap(view)}"
    value = _num(view.values.get("value_twd"), 0)
    ratio = _num(view.values.get("vol_ratio"), 0, 1000)
    word = _vol_phrase(view.values.get("vol_label"))
    flag = _word(view.values.get("vol_flag"), _VOL_FLAGS)
    amount = f"成交金額 {_amount_yi(value / 1e8)} 億" if value is not None else "成交金額暫缺"
    state = f"，{word}" if word else ""
    times = f"（{ratio:.2f} 倍）" if ratio is not None else ""
    flag_text = f"，{flag}" if flag else ""
    suffix = _date_suffix(view.data_date, main_day)
    return _fit(amount + state + times + flag_text + suffix, amount + state + times + flag_text, amount + state + flag_text)


def _inst_part(name: str, yi: Optional[float]) -> str:
    """外資／投信／自營（億元）：買超、賣超或持平。"""
    if yi is None:
        return f"{name}暫缺"
    if round(abs(yi), 1) == 0:
        return f"{name}持平"
    return f"{name}{'買超' if yi > 0 else '賣超'} {_amount_yi(yi)} 億"


def _inst_line(ctx: _Ctx, view: _View, main_day: Optional[date]) -> str:
    if not view.fresh:
        return f"外資與投信 {ctx.gap(view)}"
    base = (
        f"{_inst_part('外資', _num(view.values.get('foreign_yi'), -1e7, 1e7))}、"
        f"{_inst_part('投信', _num(view.values.get('trust_yi'), -1e7, 1e7))}"
    )
    return _fit(base + _date_suffix(view.data_date, main_day), base)


def _card_tw(ctx: _Ctx) -> dict:
    taiex = ctx.row("tw.taiex")
    etf = ctx.row("tw.etf.0050")
    main_day = taiex.data_date if taiex.fresh and taiex.data_date else _latest_day([etf])
    rows = [_taiex_line(ctx, taiex), _etf_line(ctx, etf, "台灣50（0050）", main_day)]
    breadth = ctx.row("tw.breadth.0050")
    if breadth.fresh:
        stats = _breadth_stats(breadth)
        if stats is None:
            text = f"0050 成分站上月線：{TEXT_INSUFFICIENT}"
        else:
            above, valid, _total, pct = stats
            text = _fit(
                f"0050 成分站上月線：{_plain_pct(pct)}（{above}/{valid} 檔）{_date_suffix(breadth.data_date, main_day)}",
                f"0050 成分站上月線：{_plain_pct(pct)}（{above}/{valid} 檔）",
                f"0050 成分站上月線：{_plain_pct(pct)}",
            )
    else:
        text = f"0050 成分站上月線：{ctx.gap(breadth)}"
    rows.append((text, GRAY))
    rows.append((_turnover_line(ctx, ctx.row("tw.turnover"), main_day), GRAY))
    rows.append((_inst_line(ctx, ctx.row("tw.inst3"), main_day), GRAY))
    extra = _note_line(ctx, "contra.tw", "oneliner.tw")
    if extra:
        rows.append(extra)
    return _summary_bubble(
        _title("台股（2/4）", main_day, views=[taiex, etf]),
        rows,
        buttons=(("台股細節", "/盤勢 0050"), ("台指期權圖", "/盤勢 台指 圖")),
    )


# ── ③ 期權與資金 ─────────────────────────────────────────────────────────────
def _vix_word(value: float) -> str:
    if value < VIX_CALM_BELOW:
        return "平穩"
    return "偏緊張" if value < VIX_FEAR_FROM else "恐慌"


def _vix_line(ctx: _Ctx, view: _View) -> tuple[str, str]:
    name = "恐慌指數（VIX）"
    if not view.fresh:
        return f"{name}{ctx.gap(view)}", GRAY
    value = _num(view.values.get("value"), 0, 1000)
    change = _num(view.values.get("chg"), -1000, 1000)
    if value is None:
        return f"{name}{TEXT_MISSING}", GRAY
    change_text = f"（{_signed(change)}）" if change is not None else ""
    word = _vix_word(value)
    return _fit(f"{name}{value:.2f}{change_text}，{word}", f"{name}{value:.2f}，{word}"), (
        BLUE if value < VIX_CALM_BELOW else ORANGE
    )


def _wall_line(ctx: _Ctx, view: _View, kind: str) -> tuple[str, str]:
    name = f"那斯達克（QQQ）{'買權牆' if kind == 'call' else '賣權牆'}"
    if not view.fresh:
        return f"{name}{ctx.gap(view)}", GRAY
    wall = _num(view.values.get(f"{kind}_wall"), 0)
    if view.values.get("wall_status") != market_calc.STATUS_OK or wall is None:
        return f"{name}：{TEXT_INSUFFICIENT}", GRAY
    dist = _num(view.values.get(f"{kind}_wall_dist_pct"), -100, 1000)
    dist_text = f"（距現價 {_pct(dist)}）" if dist is not None else ""
    return _fit(f"{name} {_price(wall)}{dist_text}", f"{name} {_price(wall)}"), GRAY


def _txo_line(ctx: _Ctx, view: _View, main_day: Optional[date]) -> tuple[str, str]:
    """期交所常晚一天（PLAN §18.13）：資料日和卡片標題不同就標出來（2026-10-18 H 的端對端測試）。"""
    name = "台指選擇權"
    if not view.fresh:
        return f"{name}：{ctx.gap(view)}", GRAY
    call, put = _num(view.values.get("call_wall"), 0), _num(view.values.get("put_wall"), 0)
    if view.values.get("status") != market_calc.STATUS_OK or (call is None and put is None):
        return f"{name}：{TEXT_INSUFFICIENT}", GRAY
    call_text = f"買權牆 {_price(call)}" if call is not None else "買權牆暫缺"
    put_text = f"賣權牆 {_price(put)}" if put is not None else "賣權牆暫缺"
    suffix = _date_suffix(view.data_date, main_day)
    if not suffix:
        return _fit(f"{name}：{call_text}、{put_text}"), GRAY
    return _fit(f"{name}：{call_text}、{put_text}{suffix}", f"{name}：{_md(view.data_date)} {call_text}、{put_text}"), GRAY


def _net_word(net: int) -> str:
    return "淨多" if net > 0 else "淨空" if net < 0 else "持平"


def _futures_line(ctx: _Ctx, view: _View, main_day: Optional[date]) -> tuple[str, str]:
    name = "外資台指期（大台）"
    if not view.fresh:
        return f"{name}{ctx.gap(view)}", GRAY
    net = _signed_int(view.values.get("foreign_net_big"), 10**8)
    if net is None:
        return f"{name}{TEXT_MISSING}", GRAY
    suffix = _date_suffix(view.data_date, main_day)
    return _fit(f"{name}{_net_word(net)} {_contracts(net)} 口{suffix}"), ORANGE if net < 0 else BLUE


def _margin_line(ctx: _Ctx, main_day: Optional[date]) -> tuple[str, str]:
    """台股融資常和卡片標題（美股資料日）差一天：不同天就在台股那段標日期（太寬改用「台股 10/17 +12.3 億」）。"""
    tw, us = ctx.row("tw.margin"), ctx.row("us.margin")
    dated: Optional[str] = None
    suffix = ""
    if tw.fresh:
        change = _num(tw.values.get("change_yi"), -1e6, 1e6)
        tw_text = f"台股 {_signed(change, 1)} 億" if change is not None else "台股暫缺"
        suffix = _date_suffix(tw.data_date, main_day)
        if suffix and change is not None:
            dated = f"台股 {_md(tw.data_date)} {_signed(change, 1)} 億"
    else:
        tw_text = f"台股{ctx.gap(tw, short=True)}"
    if us.fresh:
        yoy = _num(us.values.get("yoy_pct"), -100, 10000)
        us_text = f"美股年增 {_pct(yoy, 0)}" if yoy is not None else "美股暫缺"
    else:
        us_text = f"美股{ctx.gap(us, short=True)}"
    return _fit(
        f"融資：{tw_text}{suffix}、{us_text}", f"融資：{dated}、{us_text}" if dated else None, f"融資：{tw_text}、{us_text}"
    ), GRAY


def _card_flow(ctx: _Ctx) -> dict:
    vix, qqq = ctx.row("macro.VIX"), ctx.row("us.options.QQQ")
    txo, fut = ctx.row("tw.options"), ctx.row("tw.futures")
    # 美股兩列都沒有日期（過期或暫缺）時，標題改用期交所兩列的日期：每一列都有日期可以對
    main_day = _latest_day([vix, qqq]) or _latest_day([txo, fut])
    rows = [
        _vix_line(ctx, vix),
        _wall_line(ctx, qqq, "call"),
        _wall_line(ctx, qqq, "put"),
        _txo_line(ctx, txo, main_day),
        _futures_line(ctx, fut, main_day),
        _margin_line(ctx, main_day),
    ]
    return _summary_bubble(
        _title("期權與資金（3/4）", main_day, views=[vix, qqq, txo, fut]),
        rows,
        buttons=(("期權與資金細節", "/盤勢 期權"), ("那斯達克期權圖", "/盤勢 QQQ 圖"), ("台指期權圖", "/盤勢 台指 圖")),
    )


# ── ④ 宏觀與事件 ─────────────────────────────────────────────────────────────
_MACRO = {
    "UST3M": ("美債 3 個月", "UST3M"),
    "UST10Y": ("美債 10 年", "UST10Y"),
    "DXY": ("美元", "DXY"),
    "WTI": ("油價", "WTI"),
    "BRENT": ("布蘭特油價", "Brent"),
    "GOLD": ("黃金", "Gold"),
    "SILVER": ("白銀", "Silver"),
    "BTC": ("比特幣", "BTC"),
    "USDTWD": ("台幣匯率", "USD/TWD"),
    "VIX": ("恐慌指數", "VIX"),
    "VIX3M": ("3 個月恐慌指數", "VIX3M"),
    "MOVE": ("美債波動指數", "MOVE"),
    "RUT": ("羅素 2000", "RUT"),
    "DJI": ("道瓊", "DJI"),
    "IXIC": ("那斯達克綜合", "IXIC"),
    "SPY_DD": ("標普500 距高點", "SPY 價格代理"),
}
_YIELDS = ("UST3M", "UST10Y")


def _etf_proxy(key: str, values: dict) -> Optional[str]:
    """來源是「ETF 代理：UUP」（Yahoo 失敗改抓相近的 ETF）→ ETF 代號，否則 None。

    ETF 的收盤是股價（UUP 約 28.9、美元指數約 98），不是指數／現貨價：只能拿它當天的漲跌幅，價位一律不印。
    期交所的台幣匯率（「代理：期交所匯率」）和 Yahoo 的 TWD=X 是同一個量，不在這裡；SPY_DD 的「SPY 價格代理」也不是。
    """
    source = values.get("source")
    if key == "SPY_DD" or not isinstance(source, str):
        return None
    match = _PROXY_RE.fullmatch(source)
    return match.group(1) if match else None


def _macro_label(key: str, values: dict) -> str:
    """「美元（DXY）」；來源是 ETF 代理時改寫成「美元（UUP 代理）」，期交所匯率寫「台幣匯率（期交所代理）」。"""
    zh, en = _MACRO[key]
    source = values.get("source")
    if isinstance(source, str) and key != "SPY_DD":
        etf = _etf_proxy(key, values)
        if etf:
            return f"{zh}（{etf} 代理）"
        if "代理" in source and "期交所" in source:
            return f"{zh}（期交所代理）"
        if "代理" in source:
            return f"{zh}（代理）"
    return f"{zh}（{en}）"


def _macro_value(key: str, values: dict, *, terse: bool = False) -> Optional[str]:
    """terse＝最短寫法：ETF 代理不寫「單日」（2026-10-23 審查 r7 #2）。"""
    if _etf_proxy(key, values):  # 只寫日漲跌幅；沒有就明說沒有，不拿 ETF 股價頂替
        change = _num(values.get("chg_pct"), -100, 1000)
        if change is None:
            return TEXT_PROXY_NO_CHANGE
        return _pct(change, 2) if terse else f"單日 {_pct(change, 2)}"
    value = _num(values.get("value"), -1e9, 1e9)
    if value is None:
        return None
    if key in _YIELDS:
        return f"{value:.2f}%"
    if key == "SPY_DD":
        return _pct(value)
    return _price(value)


def _macro_change(key: str, values: dict, *, spelled: bool = False) -> Optional[str]:
    """殖利率寫百分點的變化（spelled＝細節卡寫出「個百分點」；摘要列寬度不夠、不加），其他優先寫漲跌幅。
    ETF 代理沒有變化量：它的 chg 是 ETF 股價的變動，不是指數的。"""
    if key == "SPY_DD" or _etf_proxy(key, values):
        return None
    change_pct = _num(values.get("chg_pct"), -100, 1000)
    if key not in _YIELDS and change_pct is not None:
        return _pct(change_pct, 2)
    change = _num(values.get("chg"), -1e9, 1e9)
    if change is None:
        return None
    return f"{_signed(change)} 個百分點" if spelled and key in _YIELDS else _signed(change)


def _macro_part(ctx: _Ctx, key: str) -> tuple[str, str, str, str]:
    """(完整名稱, 短名稱, 值, 最短寫法)：過期或暫缺時值是狀態字。代理的短名稱留著「（代理）」，不讓人當成指數本身。

    最短寫法（「油價代理 +12.34%」）給兩列都是代理、短寫法也放不下時用：數字一定完整（2026-10-23 審查 r7 #2）。"""
    zh, en = _MACRO[key]
    view = ctx.row(f"macro.{key}")
    if not view.fresh:
        gap = ctx.gap(view, short=True)
        return f"{zh}（{en}）", f"{zh} ", gap, f"{zh} {gap}"
    label = _macro_label(key, view.values)
    value = _macro_value(key, view.values) or TEXT_MISSING
    if label == f"{zh}（{en}）":
        return label, f"{zh} ", value, f"{zh} {value}"
    return label, f"{zh}（代理）", value, f"{zh}代理 {_macro_value(key, view.values, terse=True) or TEXT_MISSING}"


def _macro_pair_line(ctx: _Ctx, first: str, second: str) -> tuple[str, str]:
    full, short, shortest = [], [], []
    for key in (first, second):
        label, short_label, value, terse = _macro_part(ctx, key)
        full.append(f"{label}{value}")
        short.append(f"{short_label}{value}")
        shortest.append(terse)
    return _fit("、".join(full), "、".join(short), "、".join(shortest)), GRAY


def _yield_line(ctx: _Ctx) -> tuple[str, str]:
    view = ctx.row("macro.UST10Y")
    label, _short, value, _terse = _macro_part(ctx, "UST10Y")
    change = _macro_change("UST10Y", view.values) if view.fresh else None
    return _fit(f"{label}{value}（{change}）" if change else f"{label}{value}", f"{label}{value}"), GRAY


# 種類 → 卡上的中文名：除了 FOMC，都和 market_calc.US_EVENT_LABELS 同一份（不各存一份）。
# FOMC 倒數的是利率決議公布的時刻（market_publish 放 decision_utc），不是開會第一天。
_EVENT_KINDS = {
    "fomc": "聯準會利率決議（FOMC）",
    "cpi": market_calc.US_EVENT_LABELS["CPI"],
    "ppi": market_calc.US_EVENT_LABELS["PPI"],
    "nfp": market_calc.US_EVENT_LABELS["Nonfarm Payrolls"],
    "pce": market_calc.US_EVENT_LABELS["PCE Price Index"],
    "gdp": market_calc.US_EVENT_LABELS["GDP"],
}


def _event_name(ctx: _Ctx, item: dict) -> Optional[str]:
    kind = item.get("kind")
    if isinstance(kind, str) and kind.lower() in _EVENT_KINDS:
        return _EVENT_KINDS[kind.lower()]
    name = item.get("name")
    if kind == "earnings" and isinstance(name, str) and _SYMBOL_RE.fullmatch(name):
        # 財報日多半是預估（公司還沒公布）：標出來；公司已公布的（estimated=False）照舊
        return f"財報（{name}，預估）" if item.get("estimated") is True else f"財報（{name}）"
    # 其他美國經濟事件：名稱是 market_sources_us 白名單的英文名 → 用 market_calc 的固定中文標籤（PLAN §6）；
    # 表裡沒有的才當外部字串（2026-10-18：摘要卡原本顯示英文「Initial Jobless Claims」）
    if isinstance(name, str) and name in market_calc.US_EVENT_LABELS:
        return market_calc.US_EVENT_LABELS[name]
    return ctx.external(name, NAME_MAX)


def _events(ctx: _Ctx) -> list[tuple[str, datetime, bool]]:
    """(名稱, 時刻, 時間是否已知)；過了的不列、依時間排。

    財報日只有日期（market_publish 放美東當天 16:00、標 time_known=False）：卡上只寫美東日期＋「時間未公布」，
    不把猜的 16:00 換成台北時間寫出來（2026-10-18 F4 回報）。
    """
    view = ctx.row("events.next")
    if not view.fresh:
        return []
    found = []
    for item in _list(view.values.get("items"), EVENTS_MAX * 3):
        if not isinstance(item, dict):
            continue
        moment = _moment(item.get("at"))
        if moment is None or moment.timestamp() < ctx.now:
            continue
        name = _event_name(ctx, item)
        if name:
            kind = item.get("kind") if isinstance(item.get("kind"), str) else None
            found.append((name, moment, item.get("time_known") is not False, kind))
    # 同一時刻：FOMC／CPI／非農排前面（失業率和非農同時公布，「下一個事件」要先寫非農）
    found.sort(key=lambda entry: (entry[1], market_calc.EVENT_PRIORITY.get(entry[3], market_calc.EVENT_PRIORITY_OTHER)))
    kept = market_calc.keep_events(found, EVENTS_MAX, lambda entry: entry[3])  # FOMC／CPI／非農一定列
    return [(name, moment, known) for name, moment, known, _kind in kept]


def _countdown(ctx: _Ctx, moment: datetime, time_known: bool = True) -> str:
    if not time_known:  # 只知道美東日期：用美東的今天算，不寫時刻
        days = (moment.astimezone(_ET).date() - datetime.fromtimestamp(ctx.now, _ET).date()).days
        if days <= 0:
            return "美東今天（時間未公布）"
        if days == 1:
            return "美東明天（時間未公布）"
        return f"還有 {days} 天（美東日期）"
    local = moment.astimezone(_TPE)
    days = (local.date() - ctx.today).days
    if days <= 0:
        return f"今天 {local:%H:%M}"
    if days == 1:
        return f"明天 {local:%H:%M}"
    return f"還有 {days} 天"


def _event_line(ctx: _Ctx) -> tuple[str, str]:
    view = ctx.row("events.next")
    if not view.fresh:
        return f"下一個事件：{ctx.gap(view)}", GRAY
    events = _events(ctx)
    if not events:
        return "下一個事件：近期沒有列入的事件", GRAY
    name, moment, time_known = events[0]
    when = _countdown(ctx, moment, time_known)
    when_short = when.split("（", 1)[0]  # 「還有 3 天（美東日期）」→「還有 3 天」：細節卡有寫日期與「時間未公布」
    short = name.split("（", 1)[0] if "（" in name and not name.startswith("財報") else name  # 寬度不夠先拿掉括號裡的英文
    return _fit(
        f"下一個事件：{name}，{when}",
        f"下一個事件：{short}，{when}",
        f"下一個事件：{name}，{when_short}",
        f"下一個事件：{short}，{when_short}",
        f"{name}，{when}",
    ), GRAY


def _election_phase(ctx: _Ctx, view: _View) -> str:
    """before／voting／waiting／result／ended；投票日與期限照固定日期算，結果狀態照快照。"""
    today_et = datetime.fromtimestamp(ctx.now, _ET).date()
    state = view.values.get("state") if view.fresh else None
    state = state.lower() if isinstance(state, str) else ""
    if today_et > ELECTION_LAST_DAY or state == "ended":
        return "ended"
    if today_et < ELECTION_DAY:
        return "before"
    if today_et == ELECTION_DAY:
        return "voting"
    return "result" if state.startswith("result") else "waiting"


_CHAMBERS = {"house": "眾院", "senate": "參院"}
_SETTLED = {"yes": TEXT_SETTLED_YES, "no": TEXT_SETTLED_NO}
_PLATFORMS = {"polymarket": "Polymarket", "kalshi": "Kalshi"}


@dataclass(frozen=True)
class _Quote:
    platform: str
    chamber: str
    mid: Optional[float]
    last: Optional[float]
    updated: Optional[datetime]
    result: Optional[str] = None  # "yes"／"no"：市場已結算（Yes＝民主黨）；其他值當沒有


def _quotes(view: _View) -> list[_Quote]:
    found: list[_Quote] = []
    if not view.fresh:
        return found
    for item in _list(view.values.get("quotes"), QUOTES_MAX):
        if not isinstance(item, dict):
            continue
        platform, chamber = item.get("platform"), item.get("chamber")
        platform = platform.lower() if isinstance(platform, str) else ""
        chamber = chamber.lower() if isinstance(chamber, str) else ""
        if platform not in _PLATFORMS or chamber not in _CHAMBERS:
            continue
        result = item.get("result")
        result = result.lower() if isinstance(result, str) and result.lower() in _SETTLED else None
        found.append(
            _Quote(platform, chamber, _num(item.get("mid"), 0, 1), _num(item.get("last"), 0, 1),
                   _moment(item.get("updated_utc")), result)
        )
    return found


def _house_result(view: _View) -> Optional[str]:
    """眾院已結算的結果（yes／no）；兩個平台寫的不一樣（結算規則不同）或都沒有 → None。"""
    results = {q.result for q in _quotes(view) if q.chamber == "house" and q.result}
    return results.pop() if len(results) == 1 else None


def _election_days(ctx: _Ctx) -> int:
    return (ELECTION_DAY - datetime.fromtimestamp(ctx.now, _ET).date()).days


def _election_line(ctx: _Ctx) -> Optional[tuple[str, str]]:
    view = ctx.row("election")
    phase = _election_phase(ctx, view)
    if phase == "ended":
        return None  # 結果顯示期過了就不佔摘要卡；/盤勢 選舉 照樣接得到
    if phase == "voting":
        return f"期中選舉：美東 {_md(ELECTION_DAY)} 投票中", GRAY
    if phase == "waiting":
        return "期中選舉：已投票，等待結果", GRAY
    if phase == "result":
        result = _house_result(view)
        if result:
            return f"期中選舉：已投票，眾院{_SETTLED[result]}", GRAY
        return "期中選舉：已投票，結果看 /盤勢 選舉", GRAY
    days = _election_days(ctx)
    head = f"期中選舉還有 {days} 天"
    house = next((q for q in _quotes(view) if q.platform == "polymarket" and q.chamber == "house"), None)
    if house is None or house.mid is None:
        return _fit(f"{head}，預測市場暫缺"), GRAY
    pct = _plain_pct(house.mid * 100)
    # 摘要只放 Polymarket 一家（細節卡兩家都列）：放得下就點名平台，放不下退回不寫平台的舊寫法
    return _fit(
        f"距期中選舉 {days} 天，{_PLATFORMS[house.platform]} 民主黨拿下眾院 {pct}",
        f"{head}，預測市場：民主黨拿下眾院 {pct}",
        head,
    ), GRAY


def _topic_counts(ctx: _Ctx, values: dict) -> list[tuple[str, int]]:
    found = []
    for name, count in list(_dict(values.get("topics")).items())[:20]:
        number = _count(count, 10**4)
        label = ctx.external(name, 8) if number else None
        if label and number:
            found.append((label, number))
    rank = {label: position for position, label in enumerate(market_calc.TRUMP_TOPIC_ORDER)}
    found.sort(key=lambda entry: (-entry[1], rank.get(entry[0], len(rank)), entry[0]))  # 同票照主題表的先後
    return found


def _trump_line(ctx: _Ctx) -> tuple[str, str]:
    view = ctx.row("trump.summary")
    head = "川普近 48 小時"
    if not view.fresh:
        return f"{head}：{ctx.gap(view)}", GRAY
    topics = _topic_counts(ctx, view.values)
    at_least = market_calc.TEXT_AT_LEAST if _flag(view.values.get("at_least")) else ""
    if not topics:
        return _fit(f"{head}{at_least}：沒有符合主題的貼文", f"{head}：沒有符合主題的貼文"), GRAY
    variants = [
        f"{head}{at_least}：" + "、".join(f"{name} {count} 則" for name, count in topics[:k])
        for k in range(min(TOPICS_MAX, len(topics)), 0, -1)
    ]
    return _fit(*variants), GRAY


def _card_macro(ctx: _Ctx) -> dict:
    rows = [
        _yield_line(ctx),
        _macro_pair_line(ctx, "DXY", "WTI"),
        _macro_pair_line(ctx, "GOLD", "BTC"),
        _event_line(ctx),
    ]
    election = _election_line(ctx)
    if election:
        rows.append(election)
    rows.append(_trump_line(ctx))
    made = ctx.generated()
    title = f"宏觀與事件（4/4）｜{_md(made.date())} {made:%H:%M} 更新" if made else "宏觀與事件（4/4）｜資料暫缺"
    return _summary_bubble(
        title,
        rows,
        notes=(TEXT_MM,),
        buttons=(("新聞與宏觀", "/盤勢 新聞"), ("季節性", "/盤勢 季節性"), ("期中選舉", "/盤勢 選舉")),
    )


# 一句話（market_calc.one_liner）只有完全符合固定句型時才放進 altText：altText 只能有固定句與數字
_VOL_PHRASES = "|".join(re.escape(phrase) for phrase in market_calc.VOL_PHRASE.values())  # 同一份：量明顯偏少…爆量
_ONELINER_US_RE = re.compile(rf"美股收在月線[上下](?:、成分股站上月線 \d{{1,3}}%)?(?:、(?:{_VOL_PHRASES}))?")
_ONELINER_TW_RE = re.compile(rf"台股收在月線[上下](?:、外資[買賣]超 [0-9,]+(?:\.\d)? 億)?(?:、(?:{_VOL_PHRASES}))?")


def _oneliner_alt(ctx: _Ctx, row_id: str, pattern: re.Pattern[str]) -> Optional[str]:
    view = ctx.row(row_id)
    text = view.values.get("text") if view.fresh else None
    return text if isinstance(text, str) and pattern.fullmatch(text) else None


def _summary_alt(ctx: _Ctx) -> str:
    """altText：開頭直接寫狀態（只有固定句型、程式算的數字與日期）。"""
    voo, taiex = ctx.row("us.etf.VOO"), ctx.row("tw.taiex")
    us = _oneliner_alt(ctx, "oneliner.us", _ONELINER_US_RE)
    if us is None:
        status = _above_word(_flag(voo.values.get("above_ma20"))) if voo.fresh else None
        us = f"美股 VOO {status}" if status else "美股資料" + ("過期" if voo.status == STALE else "暫缺")
    tw = _oneliner_alt(ctx, "oneliner.tw", _ONELINER_TW_RE)
    if tw is None:
        change = _num(taiex.values.get("chg_pct"), -100, 1000) if taiex.fresh else None
        tw = f"台股加權指數 {_pct(change, 2)}" if change is not None else "台股資料" + (
            "過期" if taiex.status == STALE else "暫缺")
    # 美股與台股的資料日常差一天（台北下午台股已收、美股還是前一天）：不同天就各寫各的，同一天才合寫一個日期
    us_day, tw_day = _latest_day([voo]), _latest_day([taiex])
    if us_day is not None and tw_day is not None and us_day != tw_day:
        stamp = f"｜美股資料 {_md(us_day)}、台股資料 {_md(tw_day)}"
    else:
        day = us_day or tw_day
        stamp = f"｜資料 {_md(day)}" if day else ""
    return f"{ALT_PREFIX}{us}｜{tw}{stamp}"


def _summary_reply(ctx: _Ctx) -> Reply:
    card = _carousel([_card_us(ctx), _card_tw(ctx), _card_flow(ctx), _card_macro(ctx)])
    return _flex_reply(card, _summary_alt(ctx), summary=True)


# ── 細節：美股 ETF ───────────────────────────────────────────────────────────
def _tech_rows(ctx: _Ctx, view: _View, label: str, *, named: bool = False) -> list[dict]:
    """收盤、月線、均線距離、240 日高點、RSI、量能、價量（美股 ETF、0050 共用）。

    named＝收盤與月線那兩列開頭寫標的名稱：0050 細節的第一列是加權指數，不寫的話下面幾列會被讀成指數。"""
    if not view.fresh:
        return [_text(f"{label}：{ctx.gap(view)}", size="xl", color=GRAY)]
    subject = label if named else ""
    values = view.values
    close = _num(values.get("close"), 0)
    change = _num(values.get("chg_pct"), -100, 1000)
    above = _flag(values.get("above_ma20"))
    head = f"{subject}收盤 {_price(close)}" if close is not None else f"{subject}收盤暫缺"
    if change is not None:
        head += f"（{_pct(change, 2)}）"
    rows = [_text(head, size="xl", weight="bold")]
    status = _above_word(above)
    if status:
        rows.append(_text(f"{subject}{status}", color=_above_color(above)))
    dists = _dict(values.get("ma_dist"))
    parts = []
    for n, name in _MA_NAMES:
        dist = _num(dists.get(n), -100, 10000)
        parts.append(f"離{name} {_pct(dist)}" if dist is not None else f"離{name}暫缺")
    rows.append(_text("、".join(parts[:2])))
    rows.append(_text("、".join(parts[2:])))
    extra = []
    dd = _num(values.get("dd240_pct"), -100, 0.0001)
    rsi = _num(values.get("rsi14"), 0, 100)
    if dd is not None:
        extra.append(f"離 240 日高點 {_pct(dd)}")
    if rsi is not None:
        extra.append(f"RSI14 {rsi:.0f}")
    if extra:
        rows.append(_text("、".join(extra)))
    ratio = _num(values.get("vol_ratio"), 0, 1000)
    rank = _num(values.get("vol_pct250"), 0, 100)
    word = _vol_word(values)
    flag = _word(values.get("vol_flag"), _VOL_FLAGS)
    if ratio is not None or word:
        line = "量能："
        if ratio is not None:
            line += f"{ratio:.2f} 倍（前 20 日平均）"
        if rank is not None:
            line += f"，近 250 日第 {rank:.0f} 百分位"
        if word:
            line += f"，{word}"
        if flag:
            line += f"（{flag}）"
        rows.append(_text(line))
    pv = _word(values.get("pv_label"), market_calc.PV_LABELS)
    if pv and word != "暫不判斷":
        rows.append(_text(f"價量：{pv}"))
    return rows


def _breadth_rows(ctx: _Ctx, view: _View, label: str) -> list[dict]:
    if not view.fresh:
        return [_text(f"{label}寬度：{ctx.gap(view)}", color=GRAY)]
    values = view.values
    day = f"（{_md(view.data_date)}）" if view.data_date else ""
    rows = [_text(f"{label}寬度{day}", weight="bold")]
    universe = ctx.note(values.get("universe_label"))
    if universe:
        rows.append(_text(universe, size="md", color=GRAY))
    per_n = _dict(values.get("per_n"))
    for n, name in _MA_NAMES:
        found = _breadth_stats(view, n)
        if found is None:
            rows.append(_text(f"站上{name}：{TEXT_INSUFFICIENT}", size="md", color=GRAY))
            continue
        above, valid, total, pct = found
        stats = _dict(per_n.get(n))
        line = f"站上{name}：{above} 檔（{_plain_pct(pct)}，有效 {valid}/{total}）"
        new_below, new_above = _count(stats.get("new_below")), _count(stats.get("new_above"))
        if new_below is not None and new_above is not None:
            line += f"，新跌破 {new_below}、新站上 {new_above}"
        prev = _num(stats.get("prev_pct"), 0, 100)
        if prev is not None:
            line += f"；5 個交易日前 {_plain_pct(prev)}"
        rows.append(_text(line, size="md"))
    adv, dec, flat = _count(values.get("adv")), _count(values.get("dec")), _count(values.get("flat"))
    if adv is not None and dec is not None:
        rows.append(_text(f"漲跌家數：上漲 {adv}、下跌 {dec}" + (f"、平盤 {flat}" if flat is not None else ""), size="md"))
    high, low = _count(values.get("new_high")), _count(values.get("new_low"))
    if high is not None and low is not None:
        rows.append(_text(f"240 日新高 {high} 檔、新低 {low} 檔", size="md"))
    coverage = ctx.note(values.get("coverage_note"))
    if coverage:
        rows.append(_text(coverage, size="md", color=GRAY))
    return rows


def _symbols(item: dict) -> list[str]:
    return [s for s in _list(item.get("symbols"), 4) if isinstance(s, str) and _SYMBOL_RE.fullmatch(s)]


# 本益比算不出來的原因（publish 的 us.fund 列每家一個 status；其他值＝近四季資料不齊）
_PE_REASONS = {
    "nonpositive": market_calc.TEXT_PE_NONPOSITIVE,
    "no_market_cap": market_calc.TEXT_PE_NO_CAP,
    "stale": market_calc.TEXT_PE_STALE,
}


def _fund_rows(ctx: _Ctx, view: _View) -> list[dict]:
    """前十大本益比（市值 ÷ 近四季普通股淨利）：中位數與逐家。"""
    if not view.fresh:
        return [_text(f"前十大本益比：{ctx.gap(view)}", size="md", color=GRAY)]
    values = view.values
    median = _num(values.get("pe_median"), 0, 100000)
    valid, total = _count(values.get("pe_valid")), _count(values.get("total"))
    count = f"（{valid}/{total} 檔有效）" if valid is not None and total is not None else ""
    rows = [_text(f"前十大本益比中位數 {median:.1f}{count}" if median is not None else f"前十大本益比：{TEXT_INSUFFICIENT}",
                  weight="bold")]
    for item in _list(values.get("rows"), HOLDINGS_MAX):
        if not isinstance(item, dict) or not _symbols(item):
            continue
        name = ctx.external(item.get("name"), NAME_MAX)
        who = f"{name}（{'／'.join(_symbols(item))}）" if name else "／".join(_symbols(item))
        pe = _num(item.get("pe"), -100000, 100000)
        status = item.get("status")
        if pe is not None and status == market_calc.STATUS_OK:
            rows.append(_text(f"{who} {pe:.1f}", size="md"))
        else:
            reason = _PE_REASONS.get(status, market_calc.TEXT_PE_MISSING) if isinstance(status, str) else market_calc.TEXT_PE_MISSING
            rows.append(_text(f"{who}：{reason}", size="md", color=GRAY))
    return rows


_CHART_FOR_ETF = {"VOO": "SPY", "QQQ": "QQQ", "SOXX": "SOXX"}


def _etf_alt(sym: str, view: _View) -> str:
    if not view.fresh:
        return f"{ALT_PREFIX}{sym} 細節｜資料{'過期' if view.status == STALE else '暫缺'}"
    close = _num(view.values.get("close"), 0)
    change = _num(view.values.get("chg_pct"), -100, 1000)
    parts = []
    if close is not None:
        parts.append(f"收盤 {_price(close)}" + (f"（{_pct(change, 2)}）" if change is not None else ""))
    status = _above_word(_flag(view.values.get("above_ma20")))
    if status:
        parts.append(status)
    day = f"｜資料 {_md(view.data_date)}" if view.data_date else ""
    return f"{ALT_PREFIX}{sym} 細節｜{'、'.join(parts) or '數字暫缺'}{day}"


def _subject_contras(ctx: _Ctx, row_id: str, subject: str) -> list[dict]:
    return [_text(sentence, size="md", color=ORANGE) for who, sentence in _contras(ctx, row_id) if who == subject]


def _etf_detail(ctx: _Ctx, sym: str) -> Reply:
    etf = ctx.row(f"us.etf.{sym}")
    body = _tech_rows(ctx, etf, _US_NAMES[sym]) + _subject_contras(ctx, "contra.us", sym)
    body.append(_sep())
    body += _breadth_rows(ctx, ctx.row(f"us.breadth.{sym}"), "成分股")
    body.append(_sep())
    body += _fund_rows(ctx, ctx.row(f"us.fund.{sym}"))
    chart = _CHART_FOR_ETF[sym]
    bubble = _bubble(
        _title(_US_NAMES[sym], etf.data_date if etf.fresh else None, views=[etf]),
        body,
        notes=(TEXT_US_BASIS,),
        buttons=(("機構持股（13F）", f"/盤勢 13F {sym}"), (f"期權圖（{chart}）", f"/盤勢 {chart} 圖"), ("回摘要", COMMAND)),
    )
    return _flex_reply(bubble, _etf_alt(sym, etf))


# ── 細節：台股 0050 ──────────────────────────────────────────────────────────
def _tw_flow_rows(ctx: _Ctx) -> list[dict]:
    rows: list[dict] = []
    ad = ctx.row("tw.ad")
    if ad.fresh:
        v = ad.values
        adv, dec, flat = _count(v.get("adv")), _count(v.get("dec")), _count(v.get("flat"))
        adv_lim, dec_lim = _count(v.get("adv_limit")), _count(v.get("dec_limit"))
        if adv is not None and dec is not None:
            line = f"上市漲跌家數：上漲 {adv}" + (f"（漲停 {adv_lim}）" if adv_lim is not None else "")
            line += f"、下跌 {dec}" + (f"（跌停 {dec_lim}）" if dec_lim is not None else "")
            if flat is not None:
                line += f"、平盤 {flat}"
            rows.append(_text(line, size="md"))
    else:
        rows.append(_text(f"上市漲跌家數：{ctx.gap(ad)}", size="md", color=GRAY))
    turnover = ctx.row("tw.turnover")
    rank = _num(turnover.values.get("vol_pct250"), 0, 100) if turnover.fresh else None
    rows.append(_text(_turnover_line(ctx, turnover, None) + (f"，近 250 日第 {rank:.0f} 百分位" if rank is not None else ""),
                      size="md"))
    inst = ctx.row("tw.inst3")
    if inst.fresh:
        v = inst.values

        def yi(key: str) -> Optional[float]:
            return _num(v.get(key), -1e7, 1e7)

        rows.append(_text("三大法人（當天）：" + "、".join(
            [_inst_part("外資", yi("foreign_yi")), _inst_part("投信", yi("trust_yi")), _inst_part("自營", yi("dealer_yi"))]
        ), size="md"))
        days = _count(v.get("days_5d"), 5)
        if days == 5 and yi("foreign_5d_yi") is not None:
            rows.append(_text("近 5 日合計：" + "、".join(
                [_inst_part("外資", yi("foreign_5d_yi")), _inst_part("投信", yi("trust_5d_yi")),
                 _inst_part("自營", yi("dealer_5d_yi"))]
            ), size="md"))
        elif days is not None:
            rows.append(_text("近 5 日合計：" + market_calc.TEXT_ACCUMULATING.format(n=days), size="md", color=GRAY))
    else:
        rows.append(_text(f"三大法人：{ctx.gap(inst)}", size="md", color=GRAY))
    margin = ctx.row("tw.margin")
    if margin.fresh:
        balance = _num(margin.values.get("balance_yi"), 0, 1e7)
        change = _num(margin.values.get("change_yi"), -1e6, 1e6)
        if balance is not None:
            line = f"融資餘額 {_amount_yi(balance)} 億" + (f"（{_signed(change, 1)} 億）" if change is not None else "")
            rows.append(_text(line + "，今日（參考）", size="md"))
    else:
        rows.append(_text(f"融資：{ctx.gap(margin)}", size="md", color=GRAY))
    return rows


def _tw_fund_rows(ctx: _Ctx) -> list[dict]:
    fund = ctx.row("tw.fund.0050")
    if not fund.fresh:
        return [_text(f"0050 成分基本面：{ctx.gap(fund)}", size="md", color=GRAY)]
    v = fund.values
    rows = []
    median = _num(v.get("pe_median"), 0, 100000)
    valid, total = _count(v.get("pe_valid")), _count(v.get("total"))
    if median is not None:
        rows.append(_text(f"0050 成分本益比中位數 {median:.1f}" + (f"（{valid}/{total} 檔）" if valid and total else ""),
                          size="md"))
    positive, counted = _count(v.get("rev_pos")), _count(v.get("rev_valid"))
    month = _month_of(v.get("rev_month"))
    if positive is not None and counted and positive <= counted:
        head = f"{month} 月營收" if month else "近一個月營收"
        rows.append(_text(f"{head}年增 >0：{positive}/{counted} 檔（{_plain_pct(positive * 100 / counted)}）", size="md"))
    return rows or [_text(f"0050 成分基本面：{TEXT_INSUFFICIENT}", size="md", color=GRAY)]


def _tw_detail(ctx: _Ctx) -> Reply:
    etf, taiex = ctx.row("tw.etf.0050"), ctx.row("tw.taiex")
    body = [_text(_taiex_line(ctx, taiex)[0], size="xl", weight="bold")]
    body += _tech_rows(ctx, etf, "台灣50（0050）", named=True)
    body += [_text(s, size="md", color=ORANGE) for s in _contra_sentences(ctx, "contra.tw")[:2]]
    body.append(_sep())
    body += _breadth_rows(ctx, ctx.row("tw.breadth.0050"), "0050 成分")
    body.append(_sep())
    body += _tw_flow_rows(ctx)
    body.append(_sep())
    body += _tw_fund_rows(ctx)
    day = etf.data_date if etf.fresh else taiex.data_date if taiex.fresh else None
    bubble = _bubble(
        _title("台灣50（0050）", day, views=[etf, taiex]),
        body,
        notes=(TEXT_TW_BASIS,),
        buttons=(("台指期權圖", "/盤勢 台指 圖"), ("期權與資金細節", "/盤勢 期權"), ("回摘要", COMMAND)),
    )
    return _flex_reply(bubble, _etf_alt("0050", etf))


# ── 細節：期權與資金 ─────────────────────────────────────────────────────────
def _gamma_sentence(gex: dict) -> tuple[str, str]:
    if gex.get("status") != market_calc.STATUS_OK:
        return f"假設部位的 gamma：{TEXT_INSUFFICIENT}", GRAY
    if _flag(gex.get("near_zero")):
        return market_calc.TEXT_GEX_NEAR_ZERO, GRAY
    sign = gex.get("sign")
    if sign == -1:
        return market_calc.TEXT_GEX_NEGATIVE, ORANGE
    if sign == 1:
        return market_calc.TEXT_GEX_POSITIVE, BLUE
    return market_calc.TEXT_GEX_NEAR_ZERO, GRAY


def _crossing_text(gex: dict) -> Optional[str]:
    crossings = gex.get("crossings")
    if not isinstance(crossings, list) or gex.get("status") != market_calc.STATUS_OK:
        return None
    if not crossings:
        return market_calc.TEXT_GEX_NO_CROSSING
    parts = []
    for item in crossings[:CROSSINGS_MAX]:
        price = _num(_dict(item).get("price"), 0)
        direction = _dict(item).get("direction")
        word = market_calc.GEX_DIRECTION_TEXT.get(direction) if isinstance(direction, str) else None  # 和圖同一份
        if price is not None and word:
            parts.append(f"{_price(price)}（{word}）")
    return f"{market_calc.TEXT_GEX_CROSSINGS}：" + "、".join(parts) if parts else None


def _option_rows(ctx: _Ctx, sym: str) -> list[dict]:
    view = ctx.row(f"us.options.{sym}")
    if not view.fresh:
        return [_text(f"{_US_NAMES[sym]}期權：{ctx.gap(view)}", color=GRAY)]
    v = view.values
    spot = _num(v.get("spot"), 0)
    day = f"｜資料 {_md(view.data_date)}" if view.data_date else ""
    rows = [_text(_US_NAMES[sym] + (f" 現價 {_price(spot)}" if spot is not None else "") + day, size="xl", weight="bold")]
    if v.get("wall_status") == market_calc.STATUS_OK:
        walls = []
        for kind, name in (("call", "買權牆"), ("put", "賣權牆")):
            wall = _num(v.get(f"{kind}_wall"), 0)
            dist = _num(v.get(f"{kind}_wall_dist_pct"), -100, 1000)
            if wall is None:
                walls.append(f"{name}暫缺")
            else:
                walls.append(f"{name} {_price(wall)}" + (f"（{_pct(dist)}）" if dist is not None else ""))
        rows.append(_text("45 天內：" + "、".join(walls)))
    else:
        rows.append(_text(f"45 天內的牆：{TEXT_INSUFFICIENT}", color=GRAY))
    monthly = v.get("monthly")
    if isinstance(monthly, dict) and monthly.get("status") == market_calc.STATUS_OK:
        expiry = _day(monthly.get("expiry"))
        parts = []
        for key, name in (("call_wall", "買權牆"), ("put_wall", "賣權牆"), ("max_pain", "最大痛點")):
            value = _num(monthly.get(key), 0)
            if value is not None:
                parts.append(f"{name} {_price(value)}")
        head = f"月選 {_md(expiry)}" if expiry else "月選"
        rows.append(_text(f"{head}：" + ("、".join(parts) if parts else TEXT_INSUFFICIENT)))
    else:
        rows.append(_text(f"月選：{TEXT_INSUFFICIENT}", color=GRAY))
    stats = []
    pcr_oi, pcr_vol = _num(v.get("pcr_oi"), 0, 1000), _num(v.get("pcr_vol"), 0, 1000)
    iv30 = _num(v.get("iv30"), 0, 1000)
    if pcr_oi is not None:
        stats.append(f"賣買權未平倉比（PCR）{pcr_oi:.2f}")
    if pcr_vol is not None:
        stats.append(f"賣買權成交量比 {pcr_vol:.2f}")
    if iv30 is not None:
        stats.append(f"隱含波動率（IV30）{iv30:.1f}%")
    if stats:
        rows.append(_text("、".join(stats), size="md"))
    gex = _dict(v.get("gex"))
    sentence, color = _gamma_sentence(gex)
    rows.append(_text(sentence, size="md", color=color))
    crossing = _crossing_text(gex)
    if crossing:
        rows.append(_text(crossing, size="md"))
    coverage = _num(gex.get("coverage"), 0, 1)
    if coverage is not None:
        lower = market_calc.TEXT_COVERAGE_LOWER if _flag(gex.get("lower_bound")) else ""
        rows.append(_text(f"{market_calc.TEXT_CHAIN_RANGE}：涵蓋 {_plain_pct(coverage * 100)}{lower}", size="md",
                          color=GRAY))
    if _flag(gex.get("rate_assumed")):
        rows.append(_text(market_calc.TEXT_RATE_ASSUMED, size="md", color=GRAY))
    rows += _subject_contras(ctx, "contra.us", sym)
    return rows


def _vol_rows(ctx: _Ctx) -> list[dict]:
    vix, vix3m, move = ctx.row("macro.VIX"), ctx.row("macro.VIX3M"), ctx.row("macro.MOVE")
    v1 = _num(vix.values.get("value"), 0, 1000) if vix.fresh else None
    v3 = _num(vix3m.values.get("value"), 0, 1000) if vix3m.fresh else None
    line = f"恐慌指數（VIX）{v1:.2f}" if v1 is not None else f"恐慌指數（VIX）{ctx.gap(vix)}"
    if v3 is not None:
        line += f"、3 個月（VIX3M）{v3:.2f}"
        if v1 is not None and v3 > 0:
            line += "，短期高於中期" if v1 > v3 else "，短期不高於中期"
    m = _num(move.values.get("value"), 0, 10000) if move.fresh else None
    return [
        _text(line),
        _text(f"美債波動指數（MOVE）{m:.1f}" if m is not None else f"美債波動指數（MOVE）{ctx.gap(move)}", size="md"),
    ]


def _tw_option_rows(ctx: _Ctx) -> list[dict]:
    rows = []
    view = ctx.row("tw.options")
    if view.fresh:
        v = view.values
        month = _month_of(v.get("month"))
        day = f"資料 {_md(view.data_date)}" if view.data_date else ""
        head = "台指選擇權（" + "，".join(x for x in (f"{month} 月" if month else "", day) if x) + "）" if month or day else "台指選擇權"
        parts = []
        if v.get("status") == market_calc.STATUS_OK:
            for key, name in (("call_wall", "買權牆"), ("put_wall", "賣權牆"), ("max_pain", "最大痛點")):
                value = _num(v.get(key), 0)
                if value is not None:
                    parts.append(f"{name} {_price(value)}")
        rows.append(_text(f"{head}：" + ("、".join(parts) if parts else TEXT_INSUFFICIENT), weight="bold"))
        pcr = _num(v.get("pcr_oi_pct"), 0, 10000)
        if pcr is not None:
            rows.append(_text(f"賣買權未平倉比（PCR）{pcr:.1f}%（全部台指選擇權，和月選範圍不同）", size="md"))
        calls, puts = _signed_int(v.get("foreign_call_net"), 10**8), _signed_int(v.get("foreign_put_net"), 10**8)
        if calls is not None and puts is not None:
            rows.append(_text(f"外資買權淨部位 {calls:+,} 口、賣權淨部位 {puts:+,} 口", size="md"))
    else:
        rows.append(_text(f"台指選擇權：{ctx.gap(view)}", color=GRAY))
    fut = ctx.row("tw.futures")
    if fut.fresh:
        parts = []
        for key, name in (("foreign_net_big", "大台"), ("foreign_net_mini", "小台"), ("foreign_net_micro", "微台")):
            net = _signed_int(fut.values.get(key), 10**8)
            if net is not None:
                parts.append(f"{name} {_net_word(net)} {_contracts(net)} 口")
        head = f"外資台指期淨部位（資料 {_md(fut.data_date)}）" if fut.data_date else "外資台指期淨部位"
        line = f"{head}：" + ("、".join(parts) if parts else TEXT_MISSING)
        change = _signed_int(fut.values.get("foreign_net_big_chg"), 10**8)
        if change is not None:
            line += f"（大台單日 {change:+,}）"
        rows.append(_text(line, size="md"))
    else:
        rows.append(_text(f"外資台指期：{ctx.gap(fut)}", size="md", color=GRAY))
    return rows


def _fund_flow_rows(ctx: _Ctx) -> list[dict]:
    rows = []
    for key, name in (("es", "標普 500 期貨"), ("nq", "那斯達克 100 期貨")):
        view = ctx.row(f"us.cftc.{key}")
        net = _signed_int(view.values.get("lev_net"), 10**8) if view.fresh else None
        if net is None:
            gap = ctx.gap(view) if not view.fresh else TEXT_MISSING
            rows.append(_text(f"槓桿基金（CFTC，{name}）：{gap}", size="md", color=GRAY))
            continue
        change = _signed_int(view.values.get("lev_net_chg"), 10**8)
        line = f"槓桿基金淨部位（CFTC，{name}）{net:+,} 口" + (f"，週變化 {change:+,}" if change is not None else "")
        if view.data_date:
            line += f"（{_md(view.data_date)} 週報）"
        rows.append(_text(line, size="md"))
        am = _signed_int(view.values.get("am_net"), 10**8)
        if am is not None:
            rows.append(_text(f"資產管理人淨部位（{name}）{am:+,} 口", size="md", color=GRAY))
    margin = ctx.row("us.margin")
    if margin.fresh:
        debit = _num(margin.values.get("debit_musd"), 0, 1e9)
        yoy = _num(margin.values.get("yoy_pct"), -100, 10000)
        month = _month_of(margin.values.get("month"))
        if debit is not None:
            line = f"美股融資餘額（FINRA）{debit / 1e6:,.2f} 兆美元" + (f"（{month} 月）" if month else "")
            if yoy is not None:
                line += f"，年增 {_pct(yoy)}"
            rows.append(_text(line, size="md"))
    else:
        rows.append(_text(f"美股融資餘額（FINRA）：{ctx.gap(margin)}", size="md", color=GRAY))
    return rows


def _options_detail(ctx: _Ctx) -> Reply:
    us_body: list[dict] = _vol_rows(ctx)
    for sym in ("QQQ", "SPY", "SOXX"):
        us_body.append(_sep())
        us_body += _option_rows(ctx, sym)
    first = _bubble(
        "美股期權（1/2）",
        us_body,
        subtitle="往左滑還有 1 張",
        notes=(market_calc.TEXT_WALL_RANGE, market_calc.TEXT_WALL_DEF, TEXT_GAMMA_NOTE, market_calc.TEXT_OI_DATE,
               market_calc.TEXT_MAX_PAIN),
        buttons=(("那斯達克期權圖", "/盤勢 QQQ 圖"), ("標普期權圖", "/盤勢 SPY 圖"), ("半導體期權圖", "/盤勢 SOXX 圖")),
    )
    tw_body = _tw_option_rows(ctx)
    tw_body.append(_sep())
    tw_body += _fund_flow_rows(ctx)
    second = _bubble(
        "台指期權與資金（2/2）",
        tw_body,
        notes=(market_calc.TEXT_CFTC_NOTE, TEXT_FLOW_NOTE),
        buttons=(("台指期權圖", "/盤勢 台指 圖"), ("回摘要", COMMAND)),
    )
    day = _latest_day([ctx.row("us.options.QQQ"), ctx.row("macro.VIX")])
    alt = f"{ALT_PREFIX}期權與資金細節" + (f"｜資料 {_md(day)}" if day else "")
    return _flex_reply(_carousel([first, second]), alt)


# ── 細節：13F ────────────────────────────────────────────────────────────────
_13F_BASIS = {
    "VOO": "依 S&P 500 權重（SPY 每日持股）",
    "QQQ": "依 QQQ 持股權重（MoneyDJ 月資料）",
    "SOXX": "依 SOXX 持股權重（MoneyDJ 月資料）",
}


def _holding_rows(ctx: _Ctx, index: int, item: dict) -> list[dict]:
    symbols = _symbols(item)
    name = ctx.external(item.get("name"), NAME_MAX)
    weight = _num(item.get("weight"), 0, 100)
    head = f"{index}. " + (f"{name}（{'／'.join(symbols)}）" if name else "／".join(symbols))
    if weight is not None:
        head += f" 權重 {weight:.1f}%"
    inc, dec = _count(item.get("increased")), _count(item.get("decreased"))
    new, sold = _count(item.get("new")), _count(item.get("soldout"))
    own = _num(item.get("own_pct"), 0, 1000)
    if inc is None or dec is None:
        detail = f"機構持股變化：{TEXT_MISSING}"
    else:
        detail = f"加碼 {inc:,} 家、減碼 {dec:,} 家"
        if new is not None and sold is not None:
            detail += f"；新建 {new:,}、出清 {sold:,}"
        if own is not None:
            detail += f"；機構持股 {own:.0f}%"
    color = BLUE if inc is not None and dec is not None and inc > dec else GRAY
    return [_text(head, weight="bold"), _text(detail, size="md", color=color)]


def _13f_summary(view: _View, shown: list[dict]) -> tuple[int, int]:
    """(加碼家數多於減碼的家數, 暫缺家數)：用 job 算好的；沒有或不合理就從列出的列自己算。"""
    summary = _dict(view.values.get("summary"))
    more, missing, total = (_count(summary.get(k), 100) for k in ("more_increase", "missing", "total"))
    if more is not None and missing is not None and total is not None and more + missing <= total:
        return more, missing
    counted = [(_count(i.get("increased")), _count(i.get("decreased"))) for i in shown]
    more = sum(1 for inc, dec in counted if inc is not None and dec is not None and inc > dec)
    missing = sum(1 for inc, dec in counted if inc is None or dec is None)
    return more, missing


def _13f_detail(ctx: _Ctx, sym: str) -> Reply:
    view = ctx.row(f"us.13f.{sym}")
    body: list[dict] = []
    shown: list[dict] = []
    more = 0
    if view.fresh:
        for item in _list(view.values.get("rows"), HOLDINGS_MAX):
            if isinstance(item, dict) and _symbols(item):
                shown.append(item)
                body += _holding_rows(ctx, len(shown), item)
        if shown:
            more, missing = _13f_summary(view, shown)
            body.insert(0, _text(market_calc.TEXT_13F_SUMMARY.format(more=more, missing=missing), size="xl",
                                 weight="bold"))
        else:
            body.append(_text(f"前十大：{TEXT_INSUFFICIENT}", color=GRAY))
        period = ctx.note(view.values.get("holder_period_note"))
        if period:
            body.append(_text(period, size="md", color=GRAY))
        if view.period_unknown:
            body.append(_text(f"彙總期間：{TEXT_PERIOD_UNKNOWN}", size="md", color=GRAY))
    else:
        body.append(_text(f"機構持股（13F）：{ctx.gap(view)}", color=GRAY))
    basis = (ctx.note(view.values.get("weight_basis")) if view.fresh else None) or _13F_BASIS[sym]
    bubble = _bubble(
        f"機構持股（13F）｜{_US_NAMES[sym]}前十大",
        body,
        subtitle=basis,
        notes=(market_calc.TEXT_13F_LAG, TEXT_SHARE_CLASS),
        buttons=((f"回{_US_SHORT[sym]}細節", f"/盤勢 {sym}"), ("回摘要", COMMAND)),
    )
    alt = f"{ALT_PREFIX}13F 前十大（{sym}）｜" + (f"{more} 檔的加碼機構多於減碼機構" if shown else "資料暫缺")
    return _flex_reply(bubble, alt)


# ── 細節：新聞與宏觀 ─────────────────────────────────────────────────────────
def _fresh_items(ctx: _Ctx, view: _View, limit: int, max_age: float) -> list[tuple[dict, datetime]]:
    found = []
    for item in _list(view.values.get("items"), limit * 3):
        if not isinstance(item, dict):
            continue
        moment = _moment(item.get("published_utc"))
        if moment is not None and 0 <= ctx.now - moment.timestamp() <= max_age:
            found.append((item, moment))
    return found


def _news_rows(ctx: _Ctx, row_id: str, label: str, limit: int) -> tuple[list[dict], int, int]:
    """(列, 列出幾則, 因為格式不符而沒列出幾則)。"""
    view = ctx.row(row_id)
    rows = [_text(label, size="xl", weight="bold")]
    if not view.fresh:
        rows.append(_text(ctx.gap(view), color=GRAY))
        return rows, 0, 0
    shown = skipped = 0
    for item, moment in _fresh_items(ctx, view, limit, NEWS_MAX_AGE_S):
        if shown >= limit:
            break
        title = ctx.external(item.get("title"), TITLE_MAX)
        if title is None:
            skipped += item.get("title") is not None
            continue
        source = ctx.external(item.get("source"), NAME_MAX)
        rows.append(_text(title))
        rows.append(_text(f"{source}｜{_md_hm(moment)}" if source else _md_hm(moment), size="md", color=GRAY))
        shown += 1
    if not shown:
        rows.append(_text("36 小時內沒有列入的標題", color=GRAY))
    return rows, shown, skipped


def _trump_rows(ctx: _Ctx) -> tuple[list[dict], int, int]:
    view = ctx.row("trump.posts")
    rows = [_text("川普原文（Truth Social）", size="xl", weight="bold")]
    if not view.fresh:
        rows.append(_text(ctx.gap(view), color=GRAY))
        return rows, 0, 0
    shown = skipped = 0
    for item, moment in _fresh_items(ctx, view, TRUMP_POSTS_MAX, TRUMP_MAX_AGE_S):
        if shown >= TRUMP_POSTS_MAX:
            break
        excerpt = ctx.external(item.get("excerpt"), EXCERPT_MAX)
        if excerpt is None:
            skipped += item.get("excerpt") is not None
            continue
        rows.append(_text(excerpt, size="md"))
        meta = _md_hm(moment) + "（台北）"
        author = ctx.external(item.get("quoted_author"), NAME_MAX)
        if author:
            meta += f"｜引用（{author}）"
        topics = [t for t in (ctx.external(t, 8) for t in _list(item.get("topics"), 4)) if t]
        if topics:
            meta += "｜主題：" + "、".join(topics)
        rows.append(_text(meta, size="md", color=GRAY))
        shown += 1
    if not shown:
        rows.append(_text(market_calc.TEXT_TRUMP_NONE, color=GRAY))
    return rows, shown, skipped


def _macro_rows(ctx: _Ctx) -> list[dict]:
    rows = [_text("宏觀數字", size="xl", weight="bold")]
    for key in _MACRO:
        view = ctx.row(f"macro.{key}")
        if not view.fresh:
            zh, en = _MACRO[key]
            rows.append(_text(f"{zh}（{en}）：{ctx.gap(view)}", size="md", color=GRAY))
            continue
        value = _macro_value(key, view.values)
        change = _macro_change(key, view.values, spelled=True)
        line = f"{_macro_label(key, view.values)}：{value or TEXT_MISSING}"
        if value and change:
            line += f"（{change}）"
        if view.data_date:
            line += f"｜{_md(view.data_date)}"
        rows.append(_text(line, size="md"))
    rows.append(_sep())
    rows.append(_text("接下來的事件", size="xl", weight="bold"))
    view = ctx.row("events.next")
    events = _events(ctx)
    if not view.fresh:
        rows.append(_text(ctx.gap(view), color=GRAY))
    elif not events:
        rows.append(_text("近期沒有列入的事件", color=GRAY))
    rows += [
        _text(f"{name}：{_md_hm(moment)}（台北）" if known else f"{name}：美東 {_md(moment.astimezone(_ET).date())}（時間未公布）",
              size="md")
        for name, moment, known in events
    ]
    return rows


def _skipped_note(count: int) -> list[str]:
    return [f"另有 {count} 則格式不符，沒有列出"] if count else []


def _news_detail(ctx: _Ctx) -> Reply:
    us_rows, us_n, us_skip = _news_rows(ctx, "news.us", "美股新聞", NEWS_PER_KIND)
    tw_rows, tw_n, tw_skip = _news_rows(ctx, "news.tw", "台股新聞", NEWS_PER_KIND)
    iran_rows, iran_n, iran_skip = _news_rows(ctx, "news.iran", "伊朗新聞", IRAN_NEWS_MAX)
    trump_rows, trump_n, trump_skip = _trump_rows(ctx)
    bubbles = [
        _bubble("新聞（1/3）", us_rows + [_sep()] + tw_rows, subtitle="往左滑還有 2 張",
                notes=[TEXT_NEWS_NOTE, *([TEXT_TAIPEI_TIME] if us_n or tw_n else []), *_skipped_note(us_skip + tw_skip)],
                buttons=(("回摘要", COMMAND),)),
        _bubble("伊朗新聞與川普原文（2/3）", iran_rows + [_sep()] + trump_rows,
                notes=[TEXT_NEWS_NOTE, market_calc.TEXT_TRUMP_NOTE, *([TEXT_TAIPEI_TIME] if iran_n else []),
                       *_skipped_note(iran_skip + trump_skip)],
                buttons=(("回摘要", COMMAND),)),
        _bubble("宏觀與事件（3/3）", _macro_rows(ctx), notes=(TEXT_MM,),
                buttons=(("季節性", "/盤勢 季節性"), ("期中選舉", "/盤勢 選舉"), ("回摘要", COMMAND))),
    ]
    alt = f"{ALT_PREFIX}新聞與宏觀｜美股 {us_n} 則、台股 {tw_n} 則、伊朗 {iran_n} 則、川普 {trump_n} 則"
    return _flex_reply(_carousel(bubbles), alt)


# ── 細節：季節性 ─────────────────────────────────────────────────────────────
def _season_stat(stats: Any) -> Optional[str]:
    stats = _dict(stats)
    up, n = _num(stats.get("up_pct"), 0, 100), _count(stats.get("n"), 1000)
    median, mean = _num(stats.get("median"), -100, 1000), _num(stats.get("mean"), -100, 1000)
    if up is None or not n:
        return None
    text = f"上漲比例 {_plain_pct(up)}（{n} 年）"
    if median is not None and mean is not None:
        text += f"，中位數 {_pct(median)}、平均 {_pct(mean)}"
    return text


def _season_detail(ctx: _Ctx) -> Reply:
    view = ctx.row("season.now")
    body: list[dict] = []
    month = _count(view.values.get("month"), 12) if view.fresh else None
    if view.fresh and month:
        v = view.values
        # 期中選舉年那幾列只在期中選舉年顯示（job 標的＋今天的年份都要是）
        midterm = _flag(v.get("midterm_year")) is True and ctx.today.year % 4 == 2
        sections = [
            (f"標普500（S&P 500）{month} 月，{market_calc.TEXT_SEASON_SP_BASIS}", v.get("sp500")),
            ("第四季（12 月均價比 9 月均價，S&P 500）", v.get("sp500_q4")),
        ]
        if midterm:
            sections += [(f"期中選舉年的 {month} 月（S&P 500）", v.get("sp500_midterm")),
                         ("期中選舉年的第四季（S&P 500）", v.get("sp500_midterm_q4"))]
        if _flag(v.get("taiex_pending")):
            sections.append((f"加權指數 {month} 月", None))
        else:
            sections.append((f"加權指數 {month} 月，{market_calc.TEXT_SEASON_TW_BASIS}", v.get("taiex")))
            sections.append(("加權指數第四季", v.get("taiex_q4")))
            if midterm:
                sections.append((f"期中選舉年的 {month} 月（加權指數）", v.get("taiex_midterm")))
        for label, stats in sections:
            text = _season_stat(stats)
            body.append(_text(label, weight="bold"))
            body.append(_text(text or TEXT_INSUFFICIENT, size="md", color=None if text else GRAY))
    else:
        body.append(_text(f"季節性：{ctx.gap(view)}", color=GRAY))
    bubble = _bubble(
        "季節性" + (f"（{month} 月）" if month else "") + "｜歷史統計",
        body,
        notes=(market_calc.TEXT_SEASON_NOTE, TEXT_SEASON_SOURCE),
        buttons=(("回摘要", COMMAND),),
    )
    alt = f"{ALT_PREFIX}季節性" + (f"（{month} 月）" if month else "") + "｜歷史統計"
    return _flex_reply(bubble, alt)


# ── 細節：期中選舉 ───────────────────────────────────────────────────────────
def _election_detail(ctx: _Ctx) -> Reply:
    view = ctx.row("election")
    phase = _election_phase(ctx, view)
    if phase == "ended":
        return Reply(text=TEXT_ELECTION_ENDED)
    vote = _md(ELECTION_DAY)
    head = {
        "before": f"投票日 {vote}（美東），還有 {_election_days(ctx)} 天",
        "voting": f"投票日 {vote}（美東），投票中",
        "waiting": "已投票，等待結果",
        "result": "已投票，結果以預測市場的結算為準",
    }[phase]
    body = [_text(head, size="xl", weight="bold")]
    quotes = _quotes(view)
    clock = False  # 有沒有印出「市場更新 …」的時刻（印了 footer 就補「時間為台北時間」）
    for quote in quotes:
        who = f"{_CHAMBERS[quote.chamber]}民主黨（{_PLATFORMS[quote.platform]}）"
        last = _plain_pct(quote.last * 100, 1) if quote.last is not None else None
        extra: list[str] = []  # 第二行（灰）：最後成交與時間
        if quote.result:  # 市場已結算：結果比任何價位都重要（結算後買賣價常只剩一邊，算不出中間值）
            body.append(_text(f"{who}：{_SETTLED[quote.result]}", weight="bold"))
            extra.append(f"最後成交 {last}" if last else "最後成交暫缺")
        elif quote.mid is not None:
            body.append(_text(f"{who}：{_plain_pct(quote.mid * 100, 1)}（買賣價中間值）", weight="bold"))
            extra.append(f"最後成交 {last}" if last else "最後成交暫缺")
        elif last:  # 沒有中間值但有最後成交：照實寫，不寫成「暫缺」
            body.append(_text(f"{who}：最後成交 {last}（{TEXT_MID_MISSING}）"))
        else:
            body.append(_text(f"{who}：{TEXT_MISSING}", color=GRAY))
            continue
        if quote.platform == "kalshi":
            extra.append(market_calc.TEXT_KALSHI_TIME)
        elif quote.updated is not None:
            extra.append(f"市場更新 {_md_hm(quote.updated)}")
            clock = True
        if extra:
            body.append(_text("｜".join(extra), size="md", color=GRAY))
    if not quotes:
        body.append(_text(f"預測市場：{ctx.gap(view)}", color=GRAY))
    body.append(_text(market_calc.TEXT_POLL_NONE, size="md", color=GRAY))
    bubble = _bubble(
        f"美國期中選舉｜{vote} 投票",
        body,
        notes=(market_calc.TEXT_ELECTION_NOTE, *([TEXT_TAIPEI_TIME] if clock else ())),
        buttons=(("回摘要", COMMAND),),
    )
    alt = f"{ALT_PREFIX}期中選舉｜" + (f"還有 {_election_days(ctx)} 天" if phase == "before" else "已投票")
    return _flex_reply(bubble, alt)


# ── 圖 ───────────────────────────────────────────────────────────────────────
_CHART_LABELS = {
    "QQQ": "QQQ 期權未平倉與 gamma（假設部位）",
    "SPY": "SPY 期權未平倉與 gamma（假設部位）",
    "SOXX": "SOXX 期權未平倉與 gamma（假設部位）",
    "台指": "台指選擇權未平倉",
}


def _chart_file_ok(path: Any) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and 0 < info.st_size <= MAX_CHART_BYTES


def _chart_reply(snapshot: dict, sym: str, paths: Any, now: float) -> Reply:
    """只回檔名（main 組網址、送圖）。圖過期、檔名不合、檔案不在 → 固定句；不建目錄（create=False）。"""
    unavailable = Reply(text=TEXT_CHART_UNAVAILABLE)
    entry = _dict(_dict(snapshot.get("charts")).get(CHART_KEYS[sym]))
    stale_after = _num(entry.get("stale_after"))
    files = entry.get("files")
    if stale_after is None or now > stale_after or not isinstance(files, list):
        return unavailable
    if not 1 <= len(files) <= MAX_CHART_FILES or not all(isinstance(n, str) for n in files) or len(set(files)) != len(files):
        return unavailable
    if not market_paths.private_dir_ok(paths.chart_dir, create=False):
        return unavailable
    for name in files:
        if not _CHART_NAME_RE.fullmatch(name) or not _chart_file_ok(paths.chart_dir / name):
            return unavailable
    day = _day(entry.get("inputs_date"))
    alt = f"{CHART_ALT_PREFIX}{_CHART_LABELS[sym]}" + (f"｜資料 {_md(day)}" if day else "")
    return Reply(images=list(files), image_alt=alt)  # 送不出去的句子在 main（_MARKET_CHART_FAILURE_TEXT）


# ── 入口 ─────────────────────────────────────────────────────────────────────
def _now(now: Optional[float]) -> float:
    return time.time() if now is None else float(now)


def _disabled(paths: Any) -> bool:
    try:
        return market_paths.flag_set(paths.dashboard_disabled)
    except Exception:  # 連旗標都看不了：當停用
        return True


def _not_ready(ctx: Optional[_Ctx]) -> Reply:
    progress = ctx.progress() if ctx is not None else None
    if progress:
        note = market_calc.TEXT_BOOTSTRAP.format(done=progress[0], total=progress[1])
        return Reply(text=f"{TEXT_NOT_READY}（{note}）")
    return Reply(text=TEXT_NOT_READY)


_DETAILS: dict[str, Callable[[_Ctx, Optional[str]], Reply]] = {
    SUMMARY: lambda ctx, _sym: _summary_reply(ctx),
    ETF: lambda ctx, sym: _etf_detail(ctx, sym or "VOO"),
    TW: lambda ctx, _sym: _tw_detail(ctx),
    OPTIONS: lambda ctx, _sym: _options_detail(ctx),
    NEWS: lambda ctx, _sym: _news_detail(ctx),
    SEASON: lambda ctx, _sym: _season_detail(ctx),
    ELECTION: lambda ctx, _sym: _election_detail(ctx),
    F13: lambda ctx, sym: _13f_detail(ctx, sym or "QQQ"),
}


def handle(body: str, *, now: Optional[float] = None, paths: Optional[market_paths.Paths] = None) -> Reply:
    """「/盤勢」後面的字 → 回覆；任何意外都回固定句，不丟例外。"""
    try:
        paths = paths or market_paths.Paths.default()
    except Exception as exc:
        logger.error("market dashboard paths failed error_type=%s", type(exc).__name__)
        return Reply(text=TEXT_DISABLED)
    if _disabled(paths):
        return Reply(text=TEXT_DISABLED)
    try:
        moment = _now(now)
        command = parse_command(body)
        if command is None:
            return Reply(text=TEXT_USAGE)
        kind, sym = command
        if kind == NO_13F:
            return Reply(text=TEXT_NO_13F_0050)
        snapshot = _load_snapshot(paths)
        if snapshot is None:
            return _not_ready(None)
        if kind == CHART:
            return _chart_reply(snapshot, sym or "QQQ", paths, moment)
        ctx = _Ctx(snapshot, moment)
        if not ctx.any_data():
            return _not_ready(ctx)
        reply = _DETAILS[kind](ctx, sym)
        if ctx.dropped:
            logger.info("market dashboard dropped external strings count=%d command=%s", ctx.dropped, kind)
        return reply
    except Exception as exc:
        logger.error("market dashboard failed error_type=%s", type(exc).__name__)
        return Reply(text=TEXT_NOT_READY)


# ── 每日維護 ─────────────────────────────────────────────────────────────────
def _safe_kind(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and _KIND_RE.fullmatch(value) else None


def _outcome_summary(paths: Any, now: float) -> dict:
    """outcome 只取列舉值：群組／任務名、fail_streak、時間、kind；不帶例外訊息或回應內容。"""
    found = market_paths.read_private_file(paths.outcome, MAX_OUTCOME_BYTES)
    if found is None:
        return {"state": "missing"}
    try:
        data = json.loads(found[0].decode("utf-8"), parse_constant=lambda _name: None)
    except Exception:
        return {"state": "invalid"}
    if not isinstance(data, dict):
        return {"state": "invalid"}
    failing = []
    for section in ("groups", "tasks"):
        for name, info in list(_dict(data.get(section)).items())[:200]:
            streak = _count(_dict(info).get("fail_streak"), 10**6)
            if _safe_kind(name) and streak is not None and streak >= 3:
                entry: dict = {"name": name, "fail_streak": streak}
                kind = _safe_kind(_dict(info).get("kind"))
                if kind:
                    entry["kind"] = kind
                failing.append(entry)
    out: dict = {"state": "ok", "failing": sorted(failing, key=lambda e: e["name"])}
    # 這一輪的整體狀態（ok／degraded／terminated／code_unverified）與 collect／publish 結果，都是列舉值
    out["status"] = _safe_kind(data.get("status"))
    out["code"] = _safe_kind(data.get("code"))
    out["collect"] = _safe_kind(_dict(data.get("collect")).get("status"))
    out["publish"] = _safe_kind(_dict(data.get("publish")).get("status"))
    for key in ("last_collect_at", "last_publish_at"):
        stamp = _num(data.get(key), 0, 10**11)
        out[key] = stamp
        out[f"{key}_age_s"] = round(now - stamp, 1) if stamp is not None else None
    out["lock_held_s"] = _num(data.get("lock_held_s"), 0, 10**7)
    mismatch = data.get("calendar_mismatch")
    out["calendar_mismatch"] = sorted({k for k in _list(mismatch, 50) if _safe_kind(k)})
    # 日曆還涵蓋幾天、休市表認不出的日數（2026-10-18 審查 r6a I1／I2）：每日維護在 ≤30 天或 >0 時報
    today = datetime.fromtimestamp(now, _TPE).date()
    days_left: dict = {}
    for market, value in _dict(data.get("calendar_until")).items():
        if market in ("us", "tw"):
            try:
                until = date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
            except ValueError:
                until = None
            days_left[market] = (until - today).days if until else None
    out["calendar_days_left"] = days_left
    out["calendar_unclassified"] = {m: n for m, n in _dict(data.get("calendar_unclassified")).items()
                                    if m in ("us", "tw") and _count(n, 10**4) is not None}
    return out


def status_summary(now: Optional[float] = None, paths: Optional[market_paths.Paths] = None) -> dict:
    """給每日維護：停用、快照新鮮度、過期／暫缺的列、圖、被丟掉的外部字串數、outcome 的失敗群組。

    只放列舉值（列 id、kind、次數、時間），不放任何外部字串或例外訊息；自己出錯也不丟例外。
    """
    try:
        return _status_summary(_now(now), paths or market_paths.Paths.default())
    except Exception as exc:
        return {"snapshot": f"unexpected:{type(exc).__name__}"}


def _status_summary(moment: float, paths: Any) -> dict:
    summary: dict = {"disabled": _disabled(paths), "snapshot": "missing", "generated_at": None,
                     "publish_age_s": None, "publish_late": None, "rows_total": 0, "rows_stale": [],
                     "rows_missing": [], "rows_unexpected": [], "charts_stale": [], "external_dropped": 0,
                     "us_members": None}
    try:
        summary["outcome"] = _outcome_summary(paths, moment)
    except Exception as exc:
        summary["outcome"] = {"state": f"unexpected:{type(exc).__name__}"}
    snapshot = _load_snapshot(paths)
    if snapshot is None:
        readable = market_paths.read_private_file(paths.snapshot, MAX_SNAPSHOT_BYTES) is not None
        summary["snapshot"] = "invalid" if readable else "missing"
        return summary
    summary["snapshot"] = "ok"
    ctx = _Ctx(snapshot, moment)
    made = _num(snapshot.get("generated_at"), 0, 10**11)
    summary["generated_at"] = made
    if made is not None:
        summary["publish_age_s"] = round(moment - made, 1)
        summary["publish_late"] = moment - made > 2 * 3600
    known = set(market_calc.ROW_IDS)
    for row_id in sorted(r for r in ctx.rows if isinstance(r, str) and _ROW_ID_RE.fullmatch(r))[:2000]:
        if row_id not in known:
            summary["rows_unexpected"].append(row_id)
            continue
        status = ctx.row(row_id).status
        if status == STALE:
            summary["rows_stale"].append(row_id)
        elif status == MISSING:
            summary["rows_missing"].append(row_id)
    summary["rows_total"] = len(ctx.rows)
    charts = _dict(snapshot.get("charts"))
    for key in market_calc.CHART_KEYS:
        stale_after = _num(_dict(charts.get(key)).get("stale_after"))
        if stale_after is None or moment > stale_after:
            summary["charts_stale"].append(key)
    try:
        for kind, sym in ((SUMMARY, None), (ETF, "VOO"), (ETF, "QQQ"), (ETF, "SOXX"), (TW, None), (OPTIONS, None),
                          (NEWS, None), (SEASON, None), (ELECTION, None), (F13, "VOO"), (F13, "QQQ"), (F13, "SOXX")):
            _DETAILS[kind](ctx, sym)
    except Exception as exc:
        summary["render_error"] = f"unexpected:{type(exc).__name__}"
    summary["external_dropped"] = ctx.dropped
    progress = ctx.progress()
    summary["us_members"] = list(progress) if progress else None
    return summary


# 這個模組會讀的列（job 依 market_calc.ROW_IDS 寫；test_market_dashboard 檢查兩邊一致）
READ_ROWS: tuple[str, ...] = (
    *(f"us.etf.{s}" for s in US_ETFS),
    *(f"us.breadth.{s}" for s in US_ETFS),
    *(f"us.options.{s}" for s in ("QQQ", "SPY", "SOXX")),
    "us.cftc.es", "us.cftc.nq", "us.margin",
    *(f"us.13f.{s}" for s in US_ETFS),
    *(f"us.fund.{s}" for s in US_ETFS),
    "tw.taiex", "tw.etf.0050", "tw.breadth.0050", "tw.ad", "tw.turnover", "tw.inst3", "tw.margin",
    "tw.futures", "tw.options", "tw.fund.0050",
    *(f"macro.{k}" for k in _MACRO),
    "events.next", "season.now", "election", "trump.summary", "trump.posts",
    "news.us", "news.tw", "news.iran", "oneliner.us", "oneliner.tw", "contra.us", "contra.tw",
)
