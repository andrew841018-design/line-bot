"""市場盤勢（/盤勢，2026-10-17）：bot 端只讀「顯示用快照」，組摘要卡（張數見 SUMMARY_CARD_COUNT）、細節卡與期權圖回覆。

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
- 版面（PLAN §6）：摘要卡同一個 size、每張 ≤6 列、每列 ≤24 個中文字寬（全形算 1、半形算 0.5）、
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
from decimal import ROUND_HALF_UP, Decimal
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
# 每個泡泡最多 3 顆按鈕（卡高）；_bubble 自己擋，不只靠測試（2026-10-24 第 9 輪 N7，審查 r9 s1-N7）
MAX_BUTTONS = 3
# 摘要輪播的張數（2026-10-24 第 9 輪 A10／A11）：標題的（i/N）、TEXT_SWIPE 的「還有 N-1 張」、TEXT_USAGE 的「N 張摘要卡」
# 都由它算出。之後加一張摘要卡＝改這個數字、在 SUMMARY_CARDS 插一行、寫新的 builder；別張的頁碼與第一張的「往左滑」
# 都照位置算，不用動（test_market_dashboard 有模擬這件事）。
SUMMARY_CARD_COUNT = 5
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
# events.next 的 items 最多讀幾筆（2026-10-24 第 9 輪 A16）：publish 的 EVENTS_KEEP 從 12 加到 24，清單最後面是 keep_events 換進來
# 的必留事件（FOMC／CPI／非農）與最晚的財報日，只讀前 18 筆（EVENTS_MAX*3）會讀不到它們；留一倍餘裕（test_market_publish 鎖
# EVENTS_KEEP＝24，test_market_dashboard 鎖這個數字 ≥ 它）。事件列表與 ETF 細節卡的財報行共用
EVENTS_SCAN_MAX = 48
EARNINGS_HORIZON_S = 45 * 86400.0  # 財報行的視野（publish 的 EVENT_HORIZON_DAYS）：現在到 45 天後
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

# 摘要輪播的順序：(卡名, builder 函式名)。builder 用名字登記、呼叫時才查（所以測試換掉 builder 也抓得到）；builder 收到的
# 是「卡名＋頁碼」（_page）和副標（只有第一張有「往左滑」），所以插入、調換順序都不用改別張。張數要和 SUMMARY_CARD_COUNT 一致。
SUMMARY_CARDS: tuple[tuple[str, str], ...] = (
    ("重點摘要", "_card_digest"),
    ("美股", "_card_us"),
    ("台股", "_card_tw"),
    ("期權與資金", "_card_flow"),
    ("宏觀與事件", "_card_macro"),
)

# 固定句：和 job 共用的放在 market_calc；這裡只有 bot 自己用的
TEXT_NOT_READY = market_calc.TEXT_PREPARING
TEXT_DISABLED = market_calc.TEXT_DISABLED
TEXT_DISCLAIMER = market_calc.TEXT_DISCLAIMER
TEXT_MISSING = market_calc.TEXT_MISSING
TEXT_DATE_MISSING = market_calc.TEXT_DATE_MISSING  # 融資那一列沒有資料日時代替日期（F2-6）
TEXT_INSUFFICIENT = market_calc.TEXT_INSUFFICIENT
TEXT_PERIOD_UNKNOWN = market_calc.TEXT_UNKNOWN_PERIOD
# 2026-10-24 第 9 輪批 1 B 軌的固定句（都在 market_calc：FIXED_TEXTS 的禁用字、年份、validator 掃描自動涵蓋；這裡只是別名）
TEXT_WALL_DIST_CLOSE = market_calc.TEXT_WALL_DIST_CLOSE
TEXT_WALL_DIST_SPOT = market_calc.TEXT_WALL_DIST_SPOT
TEXT_WALL_DIST_MIXED = market_calc.TEXT_WALL_DIST_MIXED
TEXT_WALL_DIST_NAMED = market_calc.TEXT_WALL_DIST_NAMED
TEXT_CFTC_NO_SEMI = market_calc.TEXT_CFTC_NO_SEMI
TEXT_EARNINGS_HEAD = market_calc.TEXT_EARNINGS_HEAD
TEXT_EARNINGS_ESTIMATED = market_calc.TEXT_EARNINGS_ESTIMATED
TEXT_EARNINGS_OTHERS = market_calc.TEXT_EARNINGS_OTHERS
TEXT_EARNINGS_NONE = market_calc.TEXT_EARNINGS_NONE
TEXT_RSI_NOTE = market_calc.TEXT_RSI_NOTE
TEXT_CLOSE_PENDING = market_calc.TEXT_CLOSE_PENDING  # 美股、台股副標：「{md} 收盤資料尚未取得」
TEXT_TW_CLOSED = market_calc.TEXT_TW_CLOSED  # 台股副標：「{md} 休市」（覆寫已確認；{md}＝快照產生日，F2-3）
TEXT_TW_CLOSED_TENTATIVE = market_calc.TEXT_TW_CLOSED_TENTATIVE  # 「{md} 休市（暫定）」（管線推定）
TEXT_TW_ETF_LAG = market_calc.TEXT_TW_ETF_LAG  # 0050 細節卡註腳：除權息資料還沒到所以 0050 比加權舊一天
TEXT_US_BASIS = market_calc.TEXT_US_NO_DIVIDEND
TEXT_TW_BASIS = market_calc.TEXT_TW_ADJUSTED
TEXT_CARD_FAILED = "盤勢：卡片暫時出不來，請過幾分鐘再按一次"
TEXT_SUMMARY_ALT_FALLBACK = market_calc.TEXT_SUMMARY_ALT_FALLBACK  # 摘要的 altText 出錯時退回的固定句（F2-11）
# 輪播裡某一張的 builder 出錯時，那一張換成只有這一句的短卡（2026-10-24 審查 r9 s4-F9-1）；整組卡太大、無法送出時
# 用的是上面的 TEXT_CARD_FAILED（也是 main 送卡失敗的 fallback），兩句不同、不要合併
TEXT_BUBBLE_FAILED = "這張卡暫時出不來，其他張照常"
TEXT_CHART_UNAVAILABLE = "盤勢圖：這張圖還沒畫好或已經過期，請過幾分鐘再按一次"
TEXT_NO_13F_0050 = "盤勢：0050 是台股 ETF，沒有 13F 資料；台股細節請打 /盤勢 0050"
TEXT_ELECTION_ENDED = "盤勢：本屆期中選舉已結束"
# 2026-10-23：Polymarket、Kalshi 被台灣的 ISP 依法院命令封鎖，collect 不抓；快照的選舉列寫 blocked，卡上用固定句說明
# （不是「暫缺」）。摘要列接在「期中選舉還有 N 天，」後面，放不下全句，用短的
TEXT_PREDICTION_BLOCKED = "預測市場：台灣已封鎖這些網站，暫不提供"
TEXT_PREDICTION_BLOCKED_SHORT = "預測市場：台灣已封鎖"
TEXT_SWIPE = f"往左滑還有 {SUMMARY_CARD_COUNT - 1} 張"
TEXT_NEWS_NOTE = "新聞標題是第三方原文，只照錄、不代表程式判斷"
TEXT_NEWS_SAME_AS_US = "與上面的美股新聞相同，不重複列"  # 半導體新聞的標題全都已經列在美股新聞（2026-10-24 第 9 輪 A17）
TEXT_GAMMA_NOTE = "假設部位下的 gamma 是估計：假設造市商持有買權、賣出賣權"
TEXT_SHARE_CLASS = "同一公司的不同股別合併成一家（例 GOOGL＋GOOG），權重相加"
TEXT_FLOW_NOTE = "FINRA 是月資料、落後 3–4 週"
TEXT_TAIPEI_TIME = "時間為台北時間"  # 細節卡印了時刻、但沒有逐列寫「（台北）」的：footer 補一句
TEXT_PROXY_NO_CHANGE = "暫無漲跌幅"  # ETF 代理沒有日漲跌幅：不拿 ETF 的股價頂替
TEXT_MID_MISSING = "買賣價中間值暫缺"
TEXT_SETTLED_YES = "已結算，民主黨拿下"  # 預測市場的結算（Yes＝民主黨）
TEXT_SETTLED_NO = "已結算，民主黨未拿下"
TEXT_SEASON_SOURCE = "來源：datahub（Shiller 月均價）、Nasdaq（SOXX、費城半導體指數）、FinMind（加權指數）"
# 重點摘要（輪播第一張）用的固定字（2026-10-24 第 9 輪 A9）：整句由這些字加上程式算的數字與日期組成，不放快照裡的字
TEXT_DIGEST_ALL_THREE = "三檔都"  # 第 1 列：三檔同缺（三檔都過期、三檔都暫缺）
TEXT_DIGEST_EACH = "都"  # 第 1 列：三檔同向時點名三檔、接在最後一檔後面（標普、那指、半導體都在月線上；F2-8）
TEXT_DIGEST_STALE = "過期"  # 第 1、2 列：這一檔／這一項資料過期
TEXT_DIGEST_10Y = "10 年債"  # 第 4 列
TEXT_DIGEST_NEXT_EVENT = "下一個事件"  # 第 5 列：事件名稱放不下時的固定寫法（不跳到下下個事件）
TEXT_DIGEST_DATES = "資料日"  # footer：資料日：美股 10/23、台股 10/22、VIX 10/23
TEXT_USAGE = "\n".join(
    (
        "盤勢：指令用法",
        f"/盤勢 → {SUMMARY_CARD_COUNT} 張摘要卡（{'、'.join(name for name, _builder in SUMMARY_CARDS)}）",
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
# 2026-10-24 起新卡片不再有「回摘要」，但聊天紀錄裡舊卡片的「回摘要」仍送 /盤勢，所以 /盤勢 要留著。
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
    # 輪播裡哪幾張出錯、被換成短卡（「builder 名稱:例外型別名」，不放例外訊息）；main 不看這個，每日維護從 status_summary 讀
    render_errors: tuple[str, ...] = ()


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
# re.ASCII：\d 預設連全形、阿拉伯數字都收，_month_of 的 int() 也收，「２０２６-０９」會變成 9 月（第 2 輪複審 gp1 N7）
_MONTH_RE = re.compile(r"(\d{4})-?(\d{2})", re.ASCII)
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


def _half_up(value: float, digits: int) -> Decimal:
    """顯示用的四捨五入進位（ROUND_HALF_UP，平手往遠離 0 的方向）：62.5 → 63、1.45 → 1.5、-0.85 → -0.9。

    Python 的 round() 與 f"{x:.1f}" 對平手是看二進位表示（1.45 → 1.4、62.5 → 62），和 _digest_bp 的作法、建表時寫進 JSON 的
    兩位小數都不一致（2026-10-28 批 1 審查 F2-16）。一定要從 repr 轉成 Decimal：Decimal(0.95) 本身是 0.9499…，進位等於沒改。
    「看顯示值判斷」的地方（摘要第 3 列的『不到 1%』、_signed 的『顯示成 0 就不帶正負號』）也走這一個，判斷與印出來的字不會分家。"""
    number = float(value)
    if not math.isfinite(number):  # 呼叫端都先過 _num；真的來了就退回原本的格式化，不丟例外
        return Decimal(number)
    return Decimal(repr(number)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)


def _signed(value: float, digits: int = 2, unit: str = "") -> str:
    rounded = _half_up(value, digits)
    if rounded == 0:
        return f"{0:.{digits}f}{unit}"
    return f"{rounded:+,.{digits}f}{unit}"


def _pct(value: float, digits: int = 1) -> str:
    return _signed(value, digits, "%")


def _round_text(value: float, digits: int = 0) -> str:
    """四捨五入進位後的數字（不帶單位）：百分位、百分比共用。"""
    return f"{_half_up(value, digits):.{digits}f}"


def _plain_pct(value: float, digits: int = 0) -> str:
    return f"{_round_text(value, digits)}%"


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
    # 收盤是 CBOE 的暫定值：列的 source＝SOURCE_CBOE_FALLBACK（ETF 列），或 values.provisional 為 true（用暫定收盤算的
    # macro.SPY_DD、contra.us；2026-10-24 第 9 輪 A27）。舊快照沒有這個鍵就是 False
    provisional: bool = False

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
        return _View(FRESH, values, day, state == "unknown_period",
                     raw.get("source") == market_calc.SOURCE_CBOE_FALLBACK or values.get("provisional") is True)

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
    # 明確 raise（不用 assert：-O 會拿掉）。不截斷：悄悄少一顆按鈕比出錯更難發現；在輪播裡會被 _guarded_bubbles 接住，
    # 換成固定短卡並記進 render_errors，單張回覆則由 handle() 的最外層接住
    if len(buttons) > MAX_BUTTONS:
        raise ValueError(f"a bubble carries at most {MAX_BUTTONS} buttons, got {len(buttons)}")
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


def _flex_reply(card: dict, alt_text: str, *, summary: bool = False, render_errors: tuple[str, ...] = ()) -> Reply:
    """送出前自己再量一次：超過上限就回固定句（LINE 回 400 整張卡就沒了）。render_errors 兩種結果都帶著，才不會被藏起來。"""
    bubbles = card["contents"] if card.get("type") == "carousel" else [card]
    if (
        card_bytes(card) > CARD_MAX_BYTES
        or len(bubbles) > MAX_BUBBLES
        or len(alt_text) > MAX_ALT_CHARS
        or (summary and any(card_bytes(b) > SUMMARY_BUBBLE_MAX_BYTES for b in bubbles))
    ):
        logger.error("market dashboard card over budget bytes=%d bubbles=%d", card_bytes(card), len(bubbles))
        return Reply(text=TEXT_CARD_FAILED, render_errors=render_errors)
    return Reply(flex=card, alt_text=alt_text, fallback_text=TEXT_CARD_FAILED, render_errors=render_errors)


def _page(index: int, total: int = SUMMARY_CARD_COUNT) -> str:
    """輪播標題上的頁碼：（i/N）；N 預設是摘要輪播的 SUMMARY_CARD_COUNT。"""
    return f"（{index}/{total}）"


def _failed_bubble(head: str, subtitle: Optional[str] = None) -> dict:
    """輪播裡某一張出錯時換上的固定短卡：標題用那一張的標題開頭，內文只有一句固定句（第一張照舊帶「往左滑」）。"""
    return _bubble(head, [_text(TEXT_BUBBLE_FAILED, color=GRAY)], subtitle=subtitle)


def _guarded_bubbles(
    ctx: _Ctx,
    parts: Sequence[tuple[str, Callable[[_Ctx, str, Optional[str]], dict]]],
    swipe: Optional[str] = None,
    *,
    max_bytes: Optional[int] = None,
) -> tuple[list[dict], tuple[str, ...]]:
    """逐泡泡隔離（2026-10-24 審查 r9 s4-F9-1）：每個 builder 各自 try/except，壞的那張換成固定短卡，其他張照送。

    parts＝(標題開頭, builder)；builder(ctx, 標題開頭, 副標) 回一張泡泡，副標只有第一張是 swipe（往左滑還有 N 張），
    其他是 None。回 (泡泡, 出錯的 builder「名稱:例外型別名」)；訊息不進回覆、也不進 log（只記型別）。

    max_bytes（2026-10-28 批 1 審查 F2-12）：給了就也隔離「太大」——某一張泡泡超過它，只把那一張換成固定短卡、記「名稱:TooLarge」，
    其他張照送（以前是 _flex_reply 發現任何一張超過就整組換成 TEXT_CARD_FAILED）。只有摘要輪播傳（SUMMARY_BUBBLE_MAX_BYTES）：
    期權、新聞細節從來沒有單張的上限，最壞值的新聞 1/3 已經 6.3 KB，多加一個上限等於多一種失敗方式。呼叫端傳值、這裡不綁預設，
    所以測試改模組常數看得到。"""
    bubbles: list[dict] = []
    errors: list[str] = []
    for position, (head, build) in enumerate(parts):
        subtitle = swipe if position == 0 else None
        name = getattr(build, "__name__", "builder")
        try:
            bubble = build(ctx, head, subtitle)
        except Exception as exc:
            errors.append(f"{name}:{type(exc).__name__}")
            logger.error("market dashboard bubble failed builder=%s error_type=%s", name, type(exc).__name__)
            bubbles.append(_failed_bubble(head, subtitle))
            continue
        if max_bytes is not None and card_bytes(bubble) > max_bytes:
            errors.append(f"{name}:TooLarge")
            logger.error("market dashboard bubble too large builder=%s bytes=%d", name, card_bytes(bubble))
            bubble = _failed_bubble(head, subtitle)
        bubbles.append(bubble)
    return bubbles, tuple(errors)


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


def _progress_day(ctx: _Ctx, key: str) -> Optional[date]:
    """快照 progress 裡的日期；舊快照沒有這個鍵、型別不對都是 None（＝沒有證據，不加標示）。"""
    return _day(_dict(ctx.snapshot.get("progress")).get(key))


def _progress_flag(ctx: _Ctx, key: str) -> Optional[bool]:
    return _flag(_dict(ctx.snapshot.get("progress")).get(key))


def _with_notice(subtitle: Optional[str], notice: Optional[str]) -> Optional[str]:
    """副標題：骨架傳進來的（第一張的「往左滑還有 N 張」）和落後／休市標示並存、不互相取代（目前美股是第一張，
    重點摘要插到前面之後美股拿到 None，就只剩標示）。"""
    found = [part for part in (subtitle, notice) if part]
    return "｜".join(found) if found else None


def _made_today(ctx: _Ctx) -> bool:
    """快照是不是「今天」（台北）產生的：休市、『尚未取得』都是『現在』的狀態，job 掛了、隔天才看到同一份快照就不能再寫
    （2026-10-28 批 1 審查 F2-5：以前只有休市標示檢查這個）。"""
    made = ctx.generated()
    return made is not None and made.date() == ctx.today


def _waiting_notice(ctx: _Ctx, market: str, shown_day: Optional[date]) -> Optional[str]:
    """「10/23 收盤資料尚未取得」：publish 說預期的最新場次已經收完、來源還沒給（<market>_waiting_for_source 為 true、
    <market>_expected_day 是那一天），而且卡上顯示的資料日還沒到那一天（暫定收盤也算顯示了）。沒有證據不寫；
    快照不是今天做的也不寫（F2-5）。"""
    if _progress_flag(ctx, f"{market}_waiting_for_source") is not True or not _made_today(ctx):
        return None
    expected = _progress_day(ctx, f"{market}_expected_day")
    if expected is None or (shown_day is not None and shown_day >= expected):
        return None
    return TEXT_CLOSE_PENDING.format(md=_md(expected))


def _tw_closed_notice(ctx: _Ctx) -> Optional[str]:
    """「10/23 休市（暫定）」／「10/23 休市」：publish 說今天是預期交易日、日曆卻覆寫成休市（週末與休市表上的假日不會是）。
    日期是快照產生的那一天（不寫「今天」：卡片留在聊天紀錄裡，隔天再看就變成錯的；F2-3）：job 掛了、隔天看到同一份快照就不寫。
    覆寫沒有確認（管線推定）→ 暫定。"""
    if _progress_flag(ctx, "tw_closed_today") is not True or not _made_today(ctx):
        return None
    made = ctx.generated()
    template = TEXT_TW_CLOSED if _progress_flag(ctx, "tw_closed_confirmed") is True else TEXT_TW_CLOSED_TENTATIVE
    return template.format(md=_md(made.date()))


def _us_card_day(ctx: _Ctx) -> Optional[date]:
    """美股卡標題的資料日（三檔 ETF 新鮮的列裡最新的）；重點摘要 footer 的『尚未取得』與美股卡副標用同一個（F2-4）。"""
    return _latest_day([ctx.row(f"us.etf.{sym}") for sym in US_ETFS])


def _tw_card_day(ctx: _Ctx) -> Optional[date]:
    """台股卡標題的資料日：加權新鮮就用它，否則看 0050。"""
    taiex = ctx.row("tw.taiex")
    return taiex.data_date if taiex.fresh and taiex.data_date else _latest_day([ctx.row("tw.etf.0050")])


def _us_notice(ctx: _Ctx) -> Optional[str]:
    return _waiting_notice(ctx, "us", _us_card_day(ctx))


def _tw_notice(ctx: _Ctx) -> Optional[str]:
    """休市（有證據、已確認或暫定）優先於『尚未取得』。"""
    return _tw_closed_notice(ctx) or _waiting_notice(ctx, "tw", _tw_card_day(ctx))


def _rsi_shown(view: _View) -> bool:
    """細節卡（_tech_rows）有沒有印 RSI14 那一列。"""
    return view.fresh and _num(view.values.get("rsi14"), 0, 100) is not None


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
    rsi = _num(values.get("rsi14"), 0, 100)  # 窗口有缺口算不出來就是 None：不寫
    rsi_text = f" RSI {rsi:.0f}" if rsi is not None else ""
    suffix = _date_suffix(view.data_date, main_day)
    tag = market_calc.TEXT_PROVISIONAL if view.provisional else ""  # 暫定的收盤每個寫法都要標，不能被 _fit 拿掉
    # 放不下時先拿掉 RSI（2026-10-24 第 9 輪 A18：RSI 是第一個被拿掉的）、再拿掉漲跌、再拿掉月線狀態，最後才拿掉日期：
    # 這一列比標題舊卻沒寫日期，就看不出落後（審查 r9b gp1：加了暫定標籤後 VOO 第一個寫法就超寬，日期總是先被擠掉）
    text = _fit(
        f"{name}{close_text}{change_text}{status_text}{rsi_text}{tag}{suffix}",
        f"{name}{close_text}{change_text}{status_text}{tag}{suffix}",
        f"{name}{close_text}{status_text}{tag}{suffix}",
        f"{name}{close_text}{tag}{suffix}",
        f"{name}{close_text}{change_text}{status_text}{tag}",
        f"{name}{close_text}{status_text}{tag}",
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
    """矛盾優先（橘），否則一句話（灰）；每張卡最多 1 列，太寬或有術語的不放摘要卡。

    矛盾句是用暫定收盤算的（矛盾列 values.provisional，2026-10-24 第 9 輪 A27）→ 句尾加「（暫定）」；加了超過 24 字寬就
    不加，由呼叫端改在 notes 放固定句（TEXT_PROVISIONAL_CLOSE）。一句話（oneliner）不在這裡標：它只看 VOO，標不標、標在句尾還是
    改放 notes，由美股卡 _card_us 依 VOO 自己的暫定狀態決定（2026-10-28 批 1 審查 F2-9）。"""
    tag = market_calc.TEXT_PROVISIONAL if ctx.row(contra_id).provisional else ""
    for sentence in _contra_sentences(ctx, contra_id):
        if row_width(sentence) <= MAX_ROW_WIDTH and not any(word in sentence for word in _SUMMARY_JARGON):
            return (sentence + tag if row_width(sentence + tag) <= MAX_ROW_WIDTH else sentence), ORANGE
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
def _card_us(ctx: _Ctx, head: str, subtitle: Optional[str] = None) -> dict:
    """head＝卡名＋頁碼（SUMMARY_CARDS 照位置算好），subtitle＝第一張才有的「往左滑還有 N 張」。"""
    etfs = {sym: ctx.row(f"us.etf.{sym}") for sym in US_ETFS}
    main_day = _us_card_day(ctx)
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
    notes = []
    if extra and extra[1] == ORANGE and ctx.row("contra.us").provisional and not extra[0].endswith(market_calc.TEXT_PROVISIONAL):
        notes.append(market_calc.TEXT_PROVISIONAL_CLOSE)  # 矛盾句放不下「（暫定）」：改用固定句說明
    elif extra and extra[1] == GRAY and etfs["VOO"].provisional:
        # 一句話（oneliner.us）只看 VOO：VOO 的收盤是暫定值時它也要標（2026-10-28 批 1 審查 F2-9；以前只有矛盾句會標）。
        # 句尾放得下「（暫定）」就加，放不下改用 footer 的固定句——和矛盾句同一個作法
        tagged = extra[0] + market_calc.TEXT_PROVISIONAL
        if row_width(tagged) <= MAX_ROW_WIDTH:
            extra = (tagged, extra[1])
        else:
            notes.append(market_calc.TEXT_PROVISIONAL_CLOSE)
    if extra:
        rows.append(extra)
    progress = ctx.progress()
    if progress:
        notes.append("美股" + market_calc.TEXT_BOOTSTRAP.format(done=progress[0], total=progress[1]))
    return _summary_bubble(
        _title(head, main_day, views=list(etfs.values())),
        rows,
        # 2026-10-24 第 9 輪 A19：美股預期的最新場次已收完、來源還沒給 → 副標寫「10/23 收盤資料尚未取得」（不塞標題：會換行）
        subtitle=_with_notice(subtitle, _waiting_notice(ctx, "us", main_day)),
        notes=notes,
        buttons=(("標普500 細節", "/盤勢 VOO"), ("那斯達克 細節", "/盤勢 QQQ"), ("半導體 細節", "/盤勢 SOXX")),
    )


# ── ② 台股 ──────────────────────────────────────────────────────────────────
def _taiex_line(ctx: _Ctx, view: _View, main_day: Optional[date] = None, *, with_rsi: bool = True) -> tuple[str, str]:
    """加權指數那一列。main_day＝這張卡標題的資料日：和它不同天就在列尾補日期（0050 細節卡的標題是 0050 的資料日，
    加權比它新時要寫出加權是哪一天；摘要卡的標題就是加權的日期，不傳）。with_rsi＝False：細節卡這一列原本就沒有 RSI。

    放不下時先拿掉 RSI，再拿掉月線狀態，日期最後才拿掉（2026-10-24 第 9 輪 A18、A20）。"""
    name = "加權指數"
    if not view.fresh:
        return f"{name} {ctx.gap(view)}", GRAY
    close = _num(view.values.get("close"), 0)
    change = _num(view.values.get("chg_pct"), -100, 1000)
    above = _flag(view.values.get("above_ma20"))
    rsi = _num(view.values.get("rsi14"), 0, 100) if with_rsi else None
    close_text = _price(close) if close is not None else "收盤暫缺"
    change_text = f"（{_pct(change, 2)}）" if change is not None else ""
    status = _above_word(above)
    status_text = f" {status}" if status else ""
    rsi_text = f" RSI {rsi:.0f}" if rsi is not None else ""
    suffix = _date_suffix(view.data_date, main_day)
    base = f"{name} {close_text}{change_text}"
    text = _fit(
        f"{base}{status_text}{rsi_text}{suffix}",
        f"{base}{status_text}{suffix}",
        f"{base}{suffix}",
        f"{base}{status_text}",
        base,
    )
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


def _card_tw(ctx: _Ctx, head: str, subtitle: Optional[str] = None) -> dict:
    taiex = ctx.row("tw.taiex")
    etf = ctx.row("tw.etf.0050")
    main_day = _tw_card_day(ctx)
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
    # 2026-10-24 第 9 輪 A19：今天是預期交易日卻被覆寫成休市 → 「10/23 休市（暫定）」／「10/23 休市」；否則最新場次已收完、
    # 加權還沒給 → 「10/23 收盤資料尚未取得」。沒有證據（舊快照、旗標不是 true）什麼都不加
    notice = _tw_notice(ctx)
    return _summary_bubble(
        _title(head, main_day, views=[taiex, etf]),
        rows,
        subtitle=_with_notice(subtitle, notice),
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


# 期權牆（摘要列與細節卡共用）。牆距的基準由 job 決定（values 的 dist_basis／ref_close，2026-10-24 第 9 輪 I1）
_WALL_KINDS = (("call", "買權牆"), ("put", "賣權牆"))
_WALL_PRICE_WORD = {market_calc.DIST_CLOSE: "收盤", market_calc.DIST_SPOT: "最新價"}


def _wall_basis(values: dict) -> tuple[str, Optional[float]]:
    """牆距的基準與那個價格：job 寫 close 而且 ref_close 是正數 → (close, 收盤)；其他一律 (spot, 現價)——spot、舊快照沒有這
    兩個欄位（舊 job 的牆距就是對現價算的）、型別不對、說是收盤卻沒有收盤價、不認得的詞。印出來的價格與 footer 的註記都由這裡來，
    不會出現「價格印收盤、註記寫最新價」。"""
    ref = _num(values.get("ref_close"), 0)
    if values.get("dist_basis") == market_calc.DIST_CLOSE and ref is not None and ref > 0:
        return market_calc.DIST_CLOSE, ref
    return market_calc.DIST_SPOT, _num(values.get("spot"), 0)


def _wall_notes(ctx: _Ctx, symbols: Sequence[str], *, named: bool = False) -> list[str]:
    """footer 的牆距註記：列出的各檔（新鮮、牆算得出來）基準全是收盤→「牆距以收盤價計」，全是最新價→「牆距以最新價計」；
    一檔都沒有牆距可看就不寫。基準混著時：期權細節卡（每檔標題列都印了「收盤 X」或「最新價 X」）用合併的一句 MIXED；摘要卡
    （named＝True，牆那幾列不印價格）直接點名各檔，例「牆距：QQQ 以收盤價、SOXX 以最新價計」。"""
    bases: dict[str, str] = {}
    for symbol in symbols:
        view = ctx.row(f"us.options.{symbol}")
        if view.fresh and view.values.get("wall_status") == market_calc.STATUS_OK:
            bases[symbol] = _wall_basis(view.values)[0]
    kinds = set(bases.values())
    if len(kinds) > 1:
        if named:
            pairs = "、".join(f"{symbol} 以{market_calc.WALL_DIST_WORD[basis]}" for symbol, basis in bases.items())
            return [market_calc.TEXT_WALL_DIST_NAMED.format(pairs=pairs)]
        return [TEXT_WALL_DIST_MIXED]
    return [market_calc.WALL_DIST_TEXT[basis] for basis in kinds]


def _walls_line(ctx: _Ctx, view: _View, sym: str, main_day: Optional[date]) -> tuple[str, str]:
    """一檔的買權牆、賣權牆併成一列（2026-10-24 第 9 輪 A15，審查 r9 s2-N11、s3-F12）：用代號開頭——那斯達克100／半導體的
    中文名放不下四位數牆價。變體由長到短：完整（牆價＋距離）→ 拿掉距離 → 只留較近的那道牆（不帶距離）；資料日和標題不同就接
    在後面，只有最後一個變體才拿掉日期。這一列不印基準價，基準寫在 footer（_wall_notes）。"""
    name = f"{sym} 買權牆、賣權牆"
    if not view.fresh:
        return f"{name}{ctx.gap(view)}", GRAY
    values = view.values
    walls = {kind: (_num(values.get(f"{kind}_wall"), 0), _num(values.get(f"{kind}_wall_dist_pct"), -100, 1000))
             for kind, _word in _WALL_KINDS}
    if values.get("wall_status") != market_calc.STATUS_OK or all(wall is None for wall, _dist in walls.values()):
        return f"{name}：{TEXT_INSUFFICIENT}", GRAY
    words = dict(_WALL_KINDS)

    def part(kind: str, with_dist: bool, spaced: bool = True) -> str:
        wall, dist = walls[kind]
        if wall is None:
            return f"{words[kind]}暫缺"
        return f"{words[kind]}{' ' if spaced else ''}{_price(wall)}" + (f"（{_pct(dist)}）" if with_dist and dist is not None else "")

    def both(with_dist: bool, spaced: bool = True) -> str:
        return f"{sym} {part('call', with_dist, spaced)}、{part('put', with_dist, spaced)}"

    # 較近的那道牆：距離（絕對值）小的；沒有距離的排後面；一樣近取買權牆（_WALL_KINDS 的順序）
    present = [kind for kind, _word in _WALL_KINDS if walls[kind][0] is not None]
    nearer = min(present, key=lambda kind: abs(walls[kind][1]) if walls[kind][1] is not None else math.inf)
    only = f"{sym} {part(nearer, False)}"
    suffix = _date_suffix(view.data_date, main_day)
    # 新增的第二個變體（2026-10-28 批 1 審查 F2-10）：牆價前面不留空格（「買權牆262.5（+4.9%）」），每道牆省 0.5 個字寬、兩道省 1.0——
    # SOXX 常見的半美元履約價（262.5／237.5）完整寫法 24.5 放不下、只剩牆價；省掉空格後 23.5 放得下，距離留得住。
    # 不用「買／賣」縮寫：單獨一個「買 262.5」讀起來像買賣建議
    return _fit(both(True) + suffix, both(True, False) + suffix, both(False) + suffix, only + suffix, only), GRAY


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


def _card_flow(ctx: _Ctx, head: str, subtitle: Optional[str] = None) -> dict:
    vix, qqq, soxx = ctx.row("macro.VIX"), ctx.row("us.options.QQQ"), ctx.row("us.options.SOXX")
    txo, fut = ctx.row("tw.options"), ctx.row("tw.futures")
    # 美股兩列都沒有日期（過期或暫缺）時，標題改用期交所兩列的日期：每一列都有日期可以對（SOXX 那一列和標題不同天就自己標日期）
    main_day = _latest_day([vix, qqq]) or _latest_day([txo, fut])
    rows = [
        _vix_line(ctx, vix),
        _walls_line(ctx, qqq, "QQQ", main_day),
        _walls_line(ctx, soxx, "SOXX", main_day),
        _txo_line(ctx, txo, main_day),
        _futures_line(ctx, fut, main_day),
        _margin_line(ctx, main_day),
    ]
    return _summary_bubble(
        _title(head, main_day, views=[vix, qqq, txo, fut]),
        rows,
        subtitle=subtitle,
        notes=_wall_notes(ctx, ("QQQ", "SOXX"), named=True),
        buttons=(("期權與資金細節", "/盤勢 期權"), ("那斯達克期權圖", "/盤勢 QQQ 圖"), ("半導體期權圖", "/盤勢 SOXX 圖")),
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
    "VXTLT": ("TLT 選擇權波動", "VXTLT"),  # 獨立的一列，不是 MOVE 的代理（約 15 對約 100，量綱不同）；MOVE 恢復時並列
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
        return _plain_pct(value, 2)
    if key == "SPY_DD":
        return _pct(value)
    if key == "VXTLT":  # 一位小數（15.09 → 15.1），和旁邊 MOVE 那一行（98.5）同一種寫法
        return f"{value:.1f}"
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
    for item in _list(view.values.get("items"), EVENTS_SCAN_MAX):
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


def _prediction_blocked(view: _View) -> bool:
    """選舉列有沒有寫 blocked（來源在台灣被封鎖）：列過期或暫缺時不知道，當沒有。"""
    return view.fresh and _flag(view.values.get("blocked")) is True


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
    if _prediction_blocked(view):  # 倒數照寫；後面接固定句，說明是被封鎖、不是資料暫缺
        return _fit(f"{head}，{TEXT_PREDICTION_BLOCKED_SHORT}", head), GRAY
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


def _card_macro(ctx: _Ctx, head: str, subtitle: Optional[str] = None) -> dict:
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
    title = f"{head}｜{_md(made.date())} {made:%H:%M} 更新" if made else f"{head}｜資料暫缺"
    return _summary_bubble(
        title,
        rows,
        subtitle=subtitle,
        notes=_taco_summary_notes(ctx),
        buttons=(("新聞與宏觀", "/盤勢 新聞"), ("季節性", "/盤勢 季節性"), ("期中選舉", "/盤勢 選舉")),
    )


# ── ⓪ 重點摘要（輪播第一張） ────────────────────────────────────────────────────
# Andrew 2026-10-24：「回摘要＝/盤勢 就拿掉，我希望看到真正的摘要」（第 9 輪 A9；審查 r9 s2-二-A／N2–N4、s3-F7／F15、s4-F14）。
# 恰好 6 列：美股、台股、風險、利率、事件、川普，每列 ≤24 個中文字寬。只讀其他泡泡已經在用的快照列、不新增資料來源；
# 每列各自降級（fresh／stale／missing），一列沒資料不影響其他列。每一列是獨立的函式 f(ctx) → (文字, 顏色)：第 4 列
# 批 2 會換成通膨狀態句（_digest_rate_row）、第 6 列 TACO 軌會在 _digest_trump_row 加優先的變體。
#
# 日期：列內只在「這一列的資料日比預期的最新場次舊」時才帶日期（放前面，「美股 10/21：…」），放不下先拿掉次要成分、不丟日期；
# 第 1、2 列（A9）、第 3、4、6 列（2026-10-28 批 1 審查 F2-1）都是這個規則，用同一個 _digest_stamp。
# footer 另有一行不受寬度限制的「資料日：…」。bot 不帶交易日曆，所以「預期的最新場次」＝publisher 寫在 progress 的
# us_day／tw_day（第 9 輪 A19 加的欄位；舊快照沒有就不用），沒有就取同一個市場裡其他列已經有的最新資料日（見
# _digest_expected_day）。不從星期幾推：每個國定假日都會變成假的落後。
DIGEST_US_NAMES = (("VOO", "標普"), ("QQQ", "那指"), ("SOXX", "半導體"))  # 摘要列用最短的中文名（細節卡才用全名）
_UP, _DOWN = "up", "down"  # 第 1 列裡一檔 ETF 在月線上／下（過期、暫缺沿用 STALE、MISSING）
_DIGEST_MA = {_UP: "月線上", _DOWN: "月線下"}
# 「預期的最新場次」的證據：同一個市場裡這些列的最新資料日。它們都是收盤後才收進來的（collect 只存已經收盤的場次：
# VIX 與期權鏈要 data_day ≥ D 而且檔案在收盤＋15 分之後，10 年債不晚於 D），資料日不會是盤中的今天
_DIGEST_US_EVIDENCE = (
    *(f"us.etf.{sym}" for sym in US_ETFS),
    *(f"us.options.{sym}" for sym in ("QQQ", "SPY", "SOXX")),
    "macro.VIX",
    "macro.UST10Y",
)
_DIGEST_TW_EVIDENCE = ("tw.taiex", "tw.inst3", "tw.etf.0050", "tw.turnover", "tw.ad")


def _digest_expected_day(ctx: _Ctx, key: str, rows: Sequence[str]) -> Optional[date]:
    """預期的最新場次：progress.<key>（us_expected_day／tw_expected_day，A2 寫的；us_day／tw_day 是「目前資料到哪天」，不能拿來比）
    有合法日期就用它，否則取 rows 裡新鮮的列的最新資料日。

    例：美股 ETF 10/21、VIX 與 10 年債 10/22 → 預期 10/22，ETF 落後一個場次。所有列一樣落後時偵測不到，那種情形只有
    footer 的「資料日」看得出來。"""
    stated = _day(_dict(ctx.snapshot.get("progress")).get(key))
    return stated if stated is not None else _latest_day([ctx.row(row_id) for row_id in rows])


def _digest_stamp(days: Sequence[Optional[date]], expected: Optional[date]) -> str:
    """列內的日期標示 " 10/21"（放在列名後面，沒有就是空字串）：這一列用到的資料裡最舊的那個資料日比預期的最新場次舊才寫。
    寫舊不寫新（一列裡有新有舊時，讀的人看到的日期不會比最舊的資料更新）；沒有日期、不知道預期場次都不寫。"""
    known = [day for day in days if day is not None]
    if not known or expected is None:
        return ""
    oldest = min(known)
    return f" {_md(oldest)}" if oldest < expected else ""


def _digest_etf_state(view: _View) -> str:
    """第 1 列裡一檔 ETF 的狀態：_UP／_DOWN＝收盤在月線上／下，STALE＝過期，MISSING＝沒資料（或有資料但月線狀態缺）。"""
    if view.status == STALE:
        return STALE
    above = _flag(view.values.get("above_ma20")) if view.fresh else None
    return MISSING if above is None else _UP if above else _DOWN


def _digest_us_row(ctx: _Ctx) -> tuple[str, str]:
    """第 1 列：美股。三檔 ETF 依月線分組——「美股：標普在月線上，那指、半導體在月線下」；三檔同向點名三檔，寫「標普、那指、
    半導體都在月線上」（F2-8；以前是「三檔都在月線上」）；過期、暫缺的排最後（「…，半導體暫缺」），三檔都過期寫「三檔都過期
    （資料日 10/16）」、三檔都暫缺寫「三檔都暫缺」。最舊的那檔資料日比預期的最新場次舊時，日期放在「美股」後面。

    寫法的順序（第一個 ≤24 的）：完整 → 「在月線上」縮成「月線上」→ 拿掉過期／暫缺那幾組 → 兩者並用；日期一直留著。
    列上有月線狀態的那幾檔裡有 CBOE 暫定收盤時，選定的寫法放得下（≤24）就在列名（和日期）後面加「（暫定）」——「美股（暫定）：…」
    「美股 10/15（暫定）：…」；放不下不加（footer 有固定句）。帶日期時標籤緊貼那個日期，所以只有那天真的有暫定收盤才加。"""
    views = {sym: ctx.row(f"us.etf.{sym}") for sym in US_ETFS}
    states = {sym: _digest_etf_state(view) for sym, view in views.items()}
    days = [views[sym].data_date for sym, state in states.items() if state in _DIGEST_MA and views[sym].data_date]
    expected = _digest_expected_day(ctx, "us_expected_day", _DIGEST_US_EVIDENCE)
    stamp = _digest_stamp(days, expected)
    groups: list[tuple[str, str]] = []
    for state in (_UP, _DOWN, STALE, MISSING):
        names = [name for sym, name in DIGEST_US_NAMES if states[sym] == state]
        if not names:
            continue
        if len(names) < len(DIGEST_US_NAMES):
            who = "、".join(names)
        elif state in _DIGEST_MA:  # 三檔同向：點名三檔（F2-8；以前寫「三檔都在月線上」，沒說是哪三檔，混合時卻會點名）
            who = "、".join(names) + TEXT_DIGEST_EACH
        else:
            who = TEXT_DIGEST_ALL_THREE
        groups.append((state, who))
    # 三檔都過期：寫最舊的資料日（F2-8；其他列過期都有「資料日 M/D」）。只有部分過期時不寫（那一檔的日期在 footer 看不到，
    # 但寫了會讓這一列太擠；和以前一樣）
    stale_days = [views[sym].data_date for sym in US_ETFS if states[sym] == STALE and views[sym].data_date]
    all_stale = all(state == STALE for state in states.values())
    stale_word = (market_calc.TEXT_STALE.format(md=_md(min(stale_days))) if all_stale and stale_days
                  else TEXT_DIGEST_STALE)

    def clauses(short: bool, gaps: bool) -> str:
        parts = []
        for state, who in groups:
            if state in _DIGEST_MA:
                parts.append(f"{who}{'' if short else '在'}{_DIGEST_MA[state]}")
            elif gaps:
                parts.append(f"{who}{stale_word if state == STALE else TEXT_MISSING}")
        return "，".join(parts)

    label = f"美股{stamp}"
    head = f"{label}："
    bodies = (clauses(False, True), clauses(True, True), clauses(False, False), clauses(True, False))
    everyone = set(states.values())
    color = BLUE if everyone == {_UP} else ORANGE if everyone == {_DOWN} else GRAY
    text = _fit(*(head + body for body in bodies if body))
    # 列上有數字（月線狀態）的那幾檔裡有 CBOE 暫定收盤：放得下就把「（暫定）」放在列名（和日期）後面、冒號前面，放不下由 footer
    # 的固定句說明（F2-8）。結論用到了暫定收盤，所以標整列；以前接在列尾，只有部分暫定時看起來像只屬於最後一組，也可能接在
    # 「暫缺／過期」後面（第 2 輪複審 seat4 R2-I1、gp1 I-2）。字和接在列尾時一樣，放不放得下的判斷不變。
    # 帶日期時「（暫定）」緊貼那個日期，讀起來是「那天的收盤是暫定的」：只有那天（最舊的資料日）真的有暫定收盤才加。每個交易日
    # 收盤後都有一段 VOO 10/15 正式、QQQ／SOXX 10/16 暫定的時間，那時不加，由 footer 的固定句說明（第 3 輪複審 r3-A I-1）
    provisional_days = {views[sym].data_date for sym in US_ETFS if states[sym] in _DIGEST_MA and views[sym].provisional}
    if provisional_days and (not stamp or min(days) in provisional_days) and text.startswith(head):
        tagged = f"{label}{market_calc.TEXT_PROVISIONAL}：{text[len(head):]}"
        if row_width(tagged) <= MAX_ROW_WIDTH:
            text = tagged
    return text, color


def _digest_taiex_facts(view: _View) -> tuple[Optional[float], Optional[bool]]:
    """加權的 (日漲跌幅 %, 收盤在月線上嗎)；過期、暫缺、型別不對都是 None。"""
    if not view.fresh:
        return None, None
    return _num(view.values.get("chg_pct"), -100, 1000), _flag(view.values.get("above_ma20"))


def _digest_tw_row(ctx: _Ctx) -> tuple[str, str]:
    """第 2 列：台股。「台股：加權 -1.81% 月線下，外資賣超 816.6 億」；外資的寫法和金額都用台股卡同一組（_inst_part、
    _amount_yi：持平、≥1000 億不帶小數）。外資只在它和加權是同一個資料日時才放（不把昨天的外資放在今天的加權後面）；
    放不下先丟外資、不丟日期。"""
    taiex, inst = ctx.row("tw.taiex"), ctx.row("tw.inst3")
    change, above = _digest_taiex_facts(taiex)
    day = taiex.data_date if taiex.fresh else inst.data_date if inst.fresh else None
    expected = _digest_expected_day(ctx, "tw_expected_day", _DIGEST_TW_EVIDENCE)
    stamp = _digest_stamp([day], expected)
    if taiex.fresh:
        bits = [_pct(change, 2)] if change is not None else []
        if above is not None:
            bits.append(_DIGEST_MA[_UP if above else _DOWN])
        indexes = [" ".join(("加權", *bits)) if bits else f"加權{TEXT_MISSING}"]
    else:
        indexes = [f"加權{ctx.gap(taiex)}", f"加權{ctx.gap(taiex, short=True)}"]
    foreign: Optional[str] = None
    if not inst.fresh:
        foreign = f"外資{ctx.gap(inst, short=True)}"
    elif not taiex.fresh or inst.data_date == taiex.data_date:
        foreign = _inst_part("外資", _num(inst.values.get("foreign_yi"), -1e7, 1e7))
    head = f"台股{stamp}："
    variants = [f"{head}{index}，{foreign}" for index in indexes] if foreign else []
    variants += [f"{head}{index}" for index in indexes]
    return _fit(*variants), _above_color(above)


def _digest_vix_value(view: _View) -> Optional[float]:
    return _num(view.values.get("value"), 0, 1000) if view.fresh else None


_DIGEST_WALL_SYMBOLS = ("QQQ", "SOXX")  # 第 3 列的牆：兩檔取較近的一道（F2-7；以前只看 QQQ，SOXX 離牆 0.4% 時摘要看不到）


def _digest_wall_part(ctx: _Ctx) -> Optional[tuple[str, str, Optional[date]]]:
    """(「QQQ 賣權牆距 -0.6%」, 代號, 那一檔期權列的資料日)：QQQ、SOXX 的牆離基準價不到 1% 才寫（太遠的牆不是這一列的事），
    兩檔四道牆取最近的一道並帶代號。讀期權列已經發布的 *_wall_dist_pct，不自己用牆價和現價重算（距離的基準由 publisher 決定）；
    「不到 1%」在顯示的一位小數上判（_half_up，和印出來的字同一個進位：0.95 印成 +1.0%，就不算不到 1%）；保留正負，價格已經穿牆時
    看得出來。一樣近：賣權牆先、QQQ 先。"""
    near = []
    for symbol_order, symbol in enumerate(_DIGEST_WALL_SYMBOLS):
        options = ctx.row(f"us.options.{symbol}")
        if not options.fresh or options.values.get("wall_status") != market_calc.STATUS_OK:
            continue
        for order, (kind, name) in enumerate((("put", "賣權牆"), ("call", "買權牆"))):
            wall = _num(options.values.get(f"{kind}_wall"), 0)
            dist = _num(options.values.get(f"{kind}_wall_dist_pct"), -100, 1000)
            if wall is not None and dist is not None and abs(_half_up(dist, 1)) < 1:
                near.append((abs(_half_up(dist, 1)), order, symbol_order, f"{symbol} {name}距 {_pct(dist, 1)}", symbol,
                             options.data_date))
    if not near:
        return None
    _gap, _order, _symbol_order, text, symbol, day = min(near, key=lambda entry: entry[:3])
    return text, symbol, day


def _digest_risk(ctx: _Ctx) -> tuple[str, str, Optional[str]]:
    """第 3 列的 (文字, 顏色, 寫進這一列的牆的代號或 None)；footer 的『期權 M/D』與牆距基準句靠第三個值知道是哪一檔。"""
    vix = ctx.row("macro.VIX")
    wall = _digest_wall_part(ctx)
    value = _digest_vix_value(vix)
    color = GRAY
    if value is not None:
        vixes = [f"VIX {value:.1f} {_vix_word(value)}"]
        color = BLUE if value < VIX_CALM_BELOW else ORANGE
    elif vix.fresh:
        vixes = [f"VIX {TEXT_MISSING}"]
    else:
        vixes = [f"VIX {ctx.gap(vix)}", f"VIX {ctx.gap(vix, short=True)}"]
    # 日期標示（F2-1）：這個寫法裡有數字的資料（VIX、牆）最舊的資料日比預期的最新場次舊就寫在「風險」後面；先丟的是牆
    # （次要成分），日期一直留著。過期的 VIX 自己已經寫了資料日，不另外算
    expected = _digest_expected_day(ctx, "us_expected_day", _DIGEST_US_EVIDENCE)
    vix_day = vix.data_date if value is not None else None
    variants = []
    if wall is not None:
        stamp = _digest_stamp([vix_day, wall[2]], expected)
        variants += [f"風險{stamp}：{text}，{wall[0]}" for text in vixes]
    stamp = _digest_stamp([vix_day], expected)
    variants += [f"風險{stamp}：{text}" for text in vixes]
    text = _fit(*variants)
    return text, color, (wall[1] if wall is not None and wall[0] in text else None)


def _digest_risk_row(ctx: _Ctx) -> tuple[str, str]:
    """第 3 列：風險。「風險：VIX 19.0 平穩，QQQ 賣權牆距 -0.6%」——VIX 一位小數，狀態詞是細節卡同一個 _vix_word（看實際值，
    所以 19.96 會寫成「VIX 20.0 平穩」，和期權卡的「19.96，平穩」一致）；牆距不到 1% 才接在後面（QQQ、SOXX 取較近的一道，
    F2-7）。資料日落後時日期放在「風險」後面（「風險 10/26：…」）。"""
    text, color, _wall = _digest_risk(ctx)
    return text, color


def _digest_bp(change: float) -> int:
    """百分點 → 整數 bp，四捨五入進位（不用銀行家捨入）。殖利率只有兩位小數，所以正常輸入本來就是整數。"""
    return int((Decimal(repr(change)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _digest_ust_value(view: _View) -> Optional[float]:
    return _num(view.values.get("value"), -20, 100) if view.fresh else None


def _digest_rate_row(ctx: _Ctx) -> tuple[str, str]:
    """第 4 列：利率。「利率：10 年債 4.96%（+3bp）」。現在只有 10 年債；批 2 會在這裡換成通膨狀態句（缺資料時退回這一句），
    所以獨立成一個函式、不和別列共用。"""
    view = ctx.row("macro.UST10Y")
    if not view.fresh:
        return _fit(f"利率：{TEXT_DIGEST_10Y}{ctx.gap(view)}", f"利率：{TEXT_DIGEST_10Y}{ctx.gap(view, short=True)}"), GRAY
    value = _digest_ust_value(view)
    if value is None:
        return f"利率：{TEXT_DIGEST_10Y}{TEXT_MISSING}", GRAY
    change = _num(view.values.get("chg"), -50, 50)
    move = ""
    if change is not None:
        bp = _digest_bp(change)
        move = "（持平）" if bp == 0 else f"（{bp:+d}bp）"
    # 資料日比預期的最新場次舊 → 「利率 10/23：…」（F2-1）；放不下先丟變動，日期留著
    stamp = _digest_stamp([view.data_date], _digest_expected_day(ctx, "us_expected_day", _DIGEST_US_EVIDENCE))
    yield_text = _plain_pct(value, 2)
    return _fit(f"利率{stamp}：{TEXT_DIGEST_10Y} {yield_text}{move}", f"利率{stamp}：{TEXT_DIGEST_10Y} {yield_text}"), GRAY


def _digest_event_row(ctx: _Ctx) -> tuple[str, str]:
    """第 5 列：事件。「事件：密大消費者信心，今天 22:00」——下一個事件和時間，名稱與倒數用事件細節同一組（_events、
    _countdown）；名稱先拿掉括號裡的英文，還是放不下（外部來源的名稱可以到 24 字）就寫「下一個事件」，不跳到下下個事件。"""
    view = ctx.row("events.next")
    if not view.fresh:
        return _fit(f"事件：{ctx.gap(view)}", f"事件：{ctx.gap(view, short=True)}"), GRAY
    events = _events(ctx)
    if not events:
        return "事件：近期沒有列入的事件", GRAY
    name, moment, time_known = events[0]
    when = _countdown(ctx, moment, time_known)
    when_short = when.split("（", 1)[0]  # 「還有 3 天（美東日期）」→「還有 3 天」
    short = name.split("（", 1)[0] if "（" in name and not name.startswith("財報") else name
    return _fit(
        f"事件：{short}，{when}",
        f"事件：{short}，{when_short}",
        f"事件：{TEXT_DIGEST_NEXT_EVENT}，{when_short}",
    ), GRAY


def _digest_trump_row(ctx: _Ctx) -> tuple[str, str]:
    """第 6 列：川普。川普壓力指數（MacroMargin）新鮮可用就放它——數字＋任期內位階，和宏觀卡頁尾同一句；否則退回
    「川普近 48 小時：…」那一列的寫法（2026-10-28 整合 A1×TACO）。矛盾句不放這裡（矛盾句是美股、台股的句型）。"""
    _view, values = _taco_view(ctx)
    if values is not None:
        return _taco_line(ctx), GRAY
    return _trump_line(ctx)


def _digest_notes(ctx: _Ctx) -> list[str]:
    """footer 的註記（不受 24 寬限制），由上而下：
    ① 「資料日：美股 10/23、台股 10/22、VIX 10/23、10 年債 10/23、川普壓力指數 10/22」（只列實際有數字的列，照第 1–6 列的順序；
       美股三檔日期不同寫「美股 10/22 至 10/23」；牆距寫進第 3 列時加「期權 …」，F2-1）；
    ② 任一檔 ETF 的收盤是 CBOE 暫定值時一句固定說明（看 _View.provisional：source == SOURCE_CBOE_FALLBACK 的相等比較，不看子字串）；
    ③ 「美股 10/23 收盤資料尚未取得」、「台股 10/23 休市（暫定）」／「台股 10/23 收盤資料尚未取得」——和美股卡、台股卡副標同一套
       證據與判斷（_us_notice／_tw_notice；有證據才寫，F2-4）；
    ④ 第 3 列寫了牆距 → 「牆距以收盤價計」（基準句，_wall_notes；F2-4）；
    ⑤ 第 6 列是川普壓力指數 → 「不是退讓的機率」和來源兩句（F2-2）；第 6 列退回「川普近 48 小時」→ 川普壓力指數的狀態句
       （過期／暫缺，F2-14：不然摘要上任何地方都看不出指數不見了）。"""
    etfs = [ctx.row(f"us.etf.{sym}") for sym in US_ETFS]
    taiex, vix, ust = ctx.row("tw.taiex"), ctx.row("macro.VIX"), ctx.row("macro.UST10Y")
    parts = []
    days = [view.data_date for view in etfs if _digest_etf_state(view) in _DIGEST_MA and view.data_date]
    if days:
        low, high = min(days), max(days)
        parts.append(f"美股 {_md(low)}" if low == high else f"美股 {_md(low)} 至 {_md(high)}")
    if taiex.data_date and any(fact is not None for fact in _digest_taiex_facts(taiex)):
        parts.append(f"台股 {_md(taiex.data_date)}")
    if vix.data_date and _digest_vix_value(vix) is not None:
        parts.append(f"VIX {_md(vix.data_date)}")
    _risk, _color, wall_symbol = _digest_risk(ctx)
    if wall_symbol is not None:
        wall_day = ctx.row(f"us.options.{wall_symbol}").data_date
        if wall_day:
            parts.append(f"期權 {_md(wall_day)}")
    if ust.data_date and _digest_ust_value(ust) is not None:
        parts.append(f"{TEXT_DIGEST_10Y} {_md(ust.data_date)}")
    taco, taco_values = _taco_view(ctx)
    if taco_values is not None and taco.data_date:
        parts.append(f"{market_calc.TEXT_TACO_NAME} {_md(taco.data_date)}")
    notes = [f"{TEXT_DIGEST_DATES}：{'、'.join(parts)}"] if parts else []
    if any(view.fresh and view.provisional for view in etfs):
        notes.append(market_calc.TEXT_PROVISIONAL_CLOSE)
    us_notice, tw_notice = _us_notice(ctx), _tw_notice(ctx)
    if us_notice:
        notes.append(f"美股 {us_notice}")
    if tw_notice:
        notes.append(f"台股 {tw_notice}")
    if wall_symbol is not None:
        notes += _wall_notes(ctx, (wall_symbol,))
    if taco_values is not None:
        extra = (market_calc.TEXT_TACO_MEANING, market_calc.TEXT_TACO_SOURCE)
    else:
        extra = (_taco_line(ctx),)
    notes += [text for text in (ctx.note(note, TACO_NOTE_MAX) for note in extra) if text]
    return notes


def _card_digest(ctx: _Ctx, head: str, subtitle: Optional[str] = None) -> dict:
    """head＝卡名＋頁碼（SUMMARY_CARDS 照位置算好），subtitle＝第一張才有的「往左滑還有 N 張」。"""
    rows = [
        _digest_us_row(ctx),
        _digest_tw_row(ctx),
        _digest_risk_row(ctx),
        _digest_rate_row(ctx),
        _digest_event_row(ctx),
        _digest_trump_row(ctx),
    ]
    made = ctx.generated()
    title = f"{head}｜{_md(made.date())} {made:%H:%M} 更新" if made else f"{head}｜資料暫缺"
    return _summary_bubble(
        title,
        rows,
        subtitle=subtitle,
        notes=_digest_notes(ctx),
        buttons=(("期權與資金細節", "/盤勢 期權"), ("新聞與宏觀", "/盤勢 新聞"), ("台股細節", "/盤勢 0050")),
    )


# 一句話（market_calc.one_liner）只有完全符合固定句型時才放進 altText：altText 只能有固定句與數字
_VOL_PHRASES = "|".join(re.escape(phrase) for phrase in market_calc.VOL_PHRASE.values())  # 同一份：量明顯偏少…爆量
_ONELINER_US_RE = re.compile(rf"標普收在月線[上下](?:、成分股站上月線 \d{{1,3}}%)?(?:、(?:{_VOL_PHRASES}))?")
_ONELINER_TW_RE = re.compile(rf"台股收在月線[上下](?:、外資[買賣]超 [0-9,]+(?:\.\d)? 億)?(?:、(?:{_VOL_PHRASES}))?")


def _oneliner_alt(ctx: _Ctx, row_id: str, pattern: re.Pattern[str]) -> Optional[str]:
    view = ctx.row(row_id)
    text = view.values.get("text") if view.fresh else None
    return text if isinstance(text, str) and pattern.fullmatch(text) else None


def _summary_alt(ctx: _Ctx) -> str:
    """altText：開頭直接寫狀態（只有固定句型、程式算的數字與日期）。"""
    voo, taiex = ctx.row("us.etf.VOO"), ctx.row("tw.taiex")
    us = _oneliner_alt(ctx, "oneliner.us", _ONELINER_US_RE)
    if us is not None:
        used = [ctx.row(f"us.etf.{sym}") for sym in ("VOO", "QQQ", "SOXX")]
    else:
        status = _above_word(_flag(voo.values.get("above_ma20"))) if voo.fresh else None
        used = [voo] if status else []  # 「美股資料過期／暫缺」不接暫定標籤（2026-10-24 審查 r9c gp1 nit 1）
        us = f"美股 VOO {status}" if status else "美股資料" + ("過期" if voo.status == STALE else "暫缺")
    if any(view.fresh and view.provisional for view in used):
        us += market_calc.TEXT_PROVISIONAL  # 通知預覽也看得出收盤是暫定值（2026-10-24 審查 r9b gp1）
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
    """摘要輪播：順序、頁碼、第一張的「往左滑」都來自 SUMMARY_CARDS；每一張各自隔離，壞的換成固定短卡、其他照送。"""
    parts = [
        (f"{name}{_page(position)}", globals()[builder]) for position, (name, builder) in enumerate(SUMMARY_CARDS, 1)
    ]
    bubbles, errors = _guarded_bubbles(ctx, parts, TEXT_SWIPE, max_bytes=SUMMARY_BUBBLE_MAX_BYTES)
    # altText 也各自隔離（2026-10-28 批 1 審查 F2-11）：它壞了只退回固定句、記進 render_errors，輪播照送；
    # 以前它出錯會讓整個 /盤勢 回「資料準備中」，把程式出錯說成資料還沒好
    try:
        alt = _summary_alt(ctx)
    except Exception as exc:
        errors += (f"_summary_alt:{type(exc).__name__}",)
        logger.error("market dashboard alt text failed error_type=%s", type(exc).__name__)
        alt = TEXT_SUMMARY_ALT_FALLBACK
    return _flex_reply(_carousel(bubbles), alt, summary=True, render_errors=errors)


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
    if view.provisional:
        rows.append(_text(market_calc.TEXT_PROVISIONAL_CLOSE, color=GRAY))
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
        parts = []  # 前面沒有字時不能出現「量能：，暫不判斷」（審查 r9 Codex 席）
        if ratio is not None:
            parts.append(f"{ratio:.2f} 倍（前 20 日平均）")
        if rank is not None:
            parts.append(f"近 250 日第 {_round_text(rank)} 百分位")
        if word:
            parts.append(word)
        line = "量能：" + "，".join(parts)
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
        # 暫定與漲跌幅併進同一組括號（「收盤 496.37（暫定，-0.09%）」，不是「（暫定）（-0.09%）」；2026-10-28 批 1 審查 F2-13）
        inside = [market_calc.TEXT_PROVISIONAL.strip("（）")] if view.provisional else []
        if change is not None:
            inside.append(_pct(change, 2))
        parts.append(f"收盤 {_price(close)}" + (f"（{'，'.join(inside)}）" if inside else ""))
    status = _above_word(_flag(view.values.get("above_ma20")))
    if status:
        parts.append(status)
    day = f"｜資料 {_md(view.data_date)}" if view.data_date else ""
    return f"{ALT_PREFIX}{sym} 細節｜{'、'.join(parts) or '數字暫缺'}{day}"


def _subject_contras(ctx: _Ctx, row_id: str, subject: str) -> list[dict]:
    tag = market_calc.TEXT_PROVISIONAL if ctx.row(row_id).provisional else ""  # 用暫定收盤算的矛盾句（A27）
    return [_text(sentence + tag, size="md", color=ORANGE) for who, sentence in _contras(ctx, row_id) if who == subject]


def _top_symbols(ctx: _Ctx, sym: str) -> Optional[set[str]]:
    """這檔 ETF 前十大的代號（同一家公司的不同股別都算）：先看 us.fund 的列，那一列不新鮮再看 us.13f 的列；都沒有回 None。"""
    for row_id in (f"us.fund.{sym}", f"us.13f.{sym}"):
        view = ctx.row(row_id)
        if view.fresh:
            found = {s for item in _list(view.values.get("rows"), HOLDINGS_MAX) if isinstance(item, dict)
                     for s in _symbols(item)}
            if found:
                return found
    return None


def _earnings_rows(ctx: _Ctx, sym: str) -> list[dict]:
    """「45 天內財報（美東日期）」一行（2026-10-24 第 9 輪 A16，審查 r9 s3-F11、s2-7）：只列這檔 ETF 自己前十大的財報日。

    events.next 是三檔前十大的聯集，沒有標哪檔，所以用這檔前十大的代號篩（自己掃清單，上限 EVENTS_SCAN_MAX，讀得到 publish 留下的
    第 19–24 筆）。只列「現在之後、45 天以內」的；日期是美東日期（publish 放美東當天 16:00）；只有 estimated 是 True 才標預估
    （沒有這個欄位的舊快照、1／"yes" 這種都不標）。同一檔出現兩次取最早一筆。日期缺的（來源常是空的，也可能本來就不在 45 天內）
    不點名，行後面接一句固定句；一檔都沒列出時用另一句固定句（不寫「其他」）。"""
    head = TEXT_EARNINGS_HEAD
    events = ctx.row("events.next")
    if not events.fresh:
        return [_text(f"{head}：{ctx.gap(events, short=True)}", size="md", color=GRAY)]
    held = _top_symbols(ctx, sym)
    if held is None:  # 不知道這檔的前十大是誰：不猜
        return [_text(f"{head}：{TEXT_MISSING}", size="md", color=GRAY)]
    found: dict[str, tuple[date, bool]] = {}
    for item in _list(events.values.get("items"), EVENTS_SCAN_MAX):
        if not isinstance(item, dict) or item.get("kind") != "earnings":
            continue
        name, moment = item.get("name"), _moment(item.get("at"))
        if not isinstance(name, str) or name not in held or moment is None:
            continue
        if not ctx.now < moment.timestamp() <= ctx.now + EARNINGS_HORIZON_S:
            continue
        day = moment.astimezone(_ET).date()
        if name not in found or day < found[name][0]:
            found[name] = (day, item.get("estimated") is True)
    entries = [f"{name} {_md(day)}" + (f"（{TEXT_EARNINGS_ESTIMATED}）" if estimated else "")
               for name, (day, estimated) in sorted(found.items(), key=lambda entry: (entry[1][0], entry[0]))]
    if not entries:
        return [_text(f"{head}：{TEXT_EARNINGS_NONE}", size="md", color=GRAY)]
    return [_text(f"{head}：" + "、".join(entries), size="md"), _text(TEXT_EARNINGS_OTHERS, size="md", color=GRAY)]


def _etf_detail(ctx: _Ctx, sym: str) -> Reply:
    etf = ctx.row(f"us.etf.{sym}")
    body = _tech_rows(ctx, etf, _US_NAMES[sym]) + _subject_contras(ctx, "contra.us", sym)
    body.append(_sep())
    body += _breadth_rows(ctx, ctx.row(f"us.breadth.{sym}"), "成分股")
    body.append(_sep())
    body += _fund_rows(ctx, ctx.row(f"us.fund.{sym}"))
    body += _earnings_rows(ctx, sym)
    chart = _CHART_FOR_ETF[sym]
    bubble = _bubble(
        _title(_US_NAMES[sym], etf.data_date if etf.fresh else None, views=[etf]),
        body,
        notes=(TEXT_US_BASIS, *([TEXT_RSI_NOTE] if _rsi_shown(etf) else [])),
        buttons=(("機構持股（13F）", f"/盤勢 13F {sym}"), (f"期權圖（{chart}）", f"/盤勢 {chart} 圖")),
    )
    return _flex_reply(bubble, _etf_alt(sym, etf))


# ── 細節：台股 0050 ──────────────────────────────────────────────────────────
def _tw_flow_rows(ctx: _Ctx, day: Optional[date] = None) -> list[dict]:
    """0050 細節卡下半段（漲跌家數、成交金額、三大法人、融資）。day＝這張卡的標題日（0050 的資料日）：這四列的資料日和它不同天
    （除權息資料還沒到、0050 比加權舊一天）就各自帶日期，不然讀起來都是標題那一天（2026-10-28 批 1 審查 F2-6）。"""
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
            rows.append(_text(line + _date_suffix(ad.data_date, day), size="md"))
    else:
        rows.append(_text(f"上市漲跌家數：{ctx.gap(ad)}", size="md", color=GRAY))
    turnover = ctx.row("tw.turnover")
    rank = _num(turnover.values.get("vol_pct250"), 0, 100) if turnover.fresh else None
    # 日期放句尾（接在百分位後面）：_turnover_line 自己的日期會插在「近 250 日…」前面、變成兩組括號
    rows.append(_text(_turnover_line(ctx, turnover, None) + (f"，近 250 日第 {_round_text(rank)} 百分位" if rank is not None else "")
                      + (_date_suffix(turnover.data_date, day) if turnover.fresh else ""), size="md"))
    inst = ctx.row("tw.inst3")
    if inst.fresh:
        v = inst.values

        def yi(key: str) -> Optional[float]:
            return _num(v.get(key), -1e7, 1e7)

        # 「當天」是和下一行「近 5 日合計」對照的那一天，不是「今天」，所以留著；日期不同天時寫在它前面（三大法人（10/16 當天））
        stamp = f"{_md(inst.data_date)} " if inst.data_date is not None and day is not None and inst.data_date != day else ""
        rows.append(_text(f"三大法人（{stamp}當天）：" + "、".join(
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
            # 以前寫「今日（參考）」：「今日」是相對詞，0050 比加權舊一天時也是錯的（F2-6）；一律改寫資料日
            when = f"{_md(margin.data_date)}（參考）" if margin.data_date is not None else f"{TEXT_DATE_MISSING}（參考）"
            rows.append(_text(f"{line}，{when}", size="md"))
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


def _tw_lag_reason(ctx: _Ctx, etf: _View, taiex: _View) -> Optional[str]:
    """0050 比加權舊的原因句（2026-10-24 第 9 輪 A20）：只在快照有證據時寫——publish 說除權息資料（TWT49U）還沒涵蓋到最新
    場次（progress.tw_exrights_pending 為 true），而且 0050 的資料日真的比加權舊。沒有證據只補日期、不猜原因。"""
    if (
        _progress_flag(ctx, "tw_exrights_pending") is True
        and etf.fresh and taiex.fresh
        and etf.data_date is not None and taiex.data_date is not None
        and etf.data_date < taiex.data_date
    ):
        return TEXT_TW_ETF_LAG.format(md=_md(etf.data_date))
    return None


def _tw_detail(ctx: _Ctx) -> Reply:
    etf, taiex = ctx.row("tw.etf.0050"), ctx.row("tw.taiex")
    day = etf.data_date if etf.fresh else taiex.data_date if taiex.fresh else None
    # 標題是 0050 的資料日：加權和它不同天，加權那一列補自己的日期
    body = [_text(_taiex_line(ctx, taiex, day, with_rsi=False)[0], size="xl", weight="bold")]
    body += _tech_rows(ctx, etf, "台灣50（0050）", named=True)
    body += [_text(s, size="md", color=ORANGE) for s in _contra_sentences(ctx, "contra.tw")[:2]]
    body.append(_sep())
    body += _breadth_rows(ctx, ctx.row("tw.breadth.0050"), "0050 成分")
    body.append(_sep())
    body += _tw_flow_rows(ctx, day)
    body.append(_sep())
    body += _tw_fund_rows(ctx)
    reason = _tw_lag_reason(ctx, etf, taiex)
    bubble = _bubble(
        _title("台灣50（0050）", day, views=[etf, taiex]),
        body,
        notes=(TEXT_TW_BASIS, *([reason] if reason else []), *([TEXT_RSI_NOTE] if _rsi_shown(etf) else [])),
        buttons=(("台指期權圖", "/盤勢 台指 圖"), ("期權與資金細節", "/盤勢 期權")),
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
    basis, price = _wall_basis(v)  # 印出來的價格和牆距的基準同一個（「收盤 X」或「最新價 X」），footer 的註記也由它來
    day = f"｜資料 {_md(view.data_date)}" if view.data_date else ""
    rows = [_text(_US_NAMES[sym] + (f" {_WALL_PRICE_WORD[basis]} {_price(price)}" if price is not None else "") + day,
                  size="xl", weight="bold")]
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
        stats.append(f"隱含波動率（IV30）{_plain_pct(iv30, 1)}")
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
    # VXTLT（2026-10-24 第 9 輪 A12）：MOVE 的下一行，自己的標籤與數字；MOVE 那一行一個字都不動（沒有就照舊寫暫缺），MOVE 恢復時兩行並列
    vxtlt = ctx.row("macro.VXTLT")
    zh, en = _MACRO["VXTLT"]
    shown = _macro_value("VXTLT", vxtlt.values) if vxtlt.fresh else None
    if shown is None:
        vxtlt_line = f"{zh}（{en}）{ctx.gap(vxtlt)}"
    else:
        change = _macro_change("VXTLT", vxtlt.values)
        vxtlt_line = f"{zh}（{en}）{shown}" + (f"（{change}）" if change else "")
    return [
        _text(line),
        _text(f"美債波動指數（MOVE）{m:.1f}" if m is not None else f"美債波動指數（MOVE）{ctx.gap(move)}", size="md"),
        _text(vxtlt_line, size="md"),
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
            rows.append(_text(f"賣買權未平倉比（PCR）{_plain_pct(pcr, 1)}（全部台指選擇權，和月選範圍不同）", size="md"))
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


def _options_us_bubble(ctx: _Ctx, head: str, subtitle: Optional[str]) -> dict:
    body: list[dict] = _vol_rows(ctx)
    for sym in ("QQQ", "SPY", "SOXX"):
        body.append(_sep())
        body += _option_rows(ctx, sym)
    return _bubble(
        head,
        body,
        subtitle=subtitle,
        notes=(market_calc.TEXT_WALL_RANGE, *_wall_notes(ctx, ("QQQ", "SPY", "SOXX")), market_calc.TEXT_WALL_DEF,
               TEXT_GAMMA_NOTE, market_calc.TEXT_OI_DATE, market_calc.TEXT_MAX_PAIN),
        buttons=(("那斯達克期權圖", "/盤勢 QQQ 圖"), ("標普期權圖", "/盤勢 SPY 圖"), ("半導體期權圖", "/盤勢 SOXX 圖")),
    )


def _options_tw_bubble(ctx: _Ctx, head: str, subtitle: Optional[str]) -> dict:
    body = _tw_option_rows(ctx)
    body.append(_sep())
    body += _fund_flow_rows(ctx)
    return _bubble(
        head,
        body,
        subtitle=subtitle,
        notes=(market_calc.TEXT_CFTC_NOTE, TEXT_CFTC_NO_SEMI, TEXT_FLOW_NOTE),
        buttons=(("台指期權圖", "/盤勢 台指 圖"),),
    )


def _options_detail(ctx: _Ctx) -> Reply:
    bubbles, errors = _guarded_bubbles(
        ctx, [("美股期權（1/2）", _options_us_bubble), ("台指期權與資金（2/2）", _options_tw_bubble)], "往左滑還有 1 張")
    day = _latest_day([ctx.row("us.options.QQQ"), ctx.row("macro.VIX")])
    alt = f"{ALT_PREFIX}期權與資金細節" + (f"｜資料 {_md(day)}" if day else "")
    return _flex_reply(_carousel(bubbles), alt, render_errors=errors)


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
        head += f" 權重 {_plain_pct(weight, 1)}"
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
            detail += f"；機構持股 {_plain_pct(own)}"
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
        buttons=((f"回{_US_SHORT[sym]}細節", f"/盤勢 {sym}"),),
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


def _news_rows(
    ctx: _Ctx, row_id: str, label: str, limit: int, *, skip: Sequence[str] = (), listed: Optional[list[str]] = None
) -> tuple[list[dict], int, int]:
    """(列, 列出幾則, 因為格式不符而沒列出幾則)。

    skip＝別段已經列出的標題（半導體新聞不重複列美股新聞已經列的；比的是過了清洗的標題，來源不同也算同一則）；listed 不是
    None 時，這一段列出的標題依序加進去，給後面的段落當 skip。因為重複而略過的不算格式不符、也不占 limit。"""
    view = ctx.row(row_id)
    rows = [_text(label, size="xl", weight="bold")]
    if not view.fresh:
        rows.append(_text(ctx.gap(view), color=GRAY))
        return rows, 0, 0
    shown = skipped = repeated = 0
    for item, moment in _fresh_items(ctx, view, limit, NEWS_MAX_AGE_S):
        if shown >= limit:
            break
        title = ctx.external(item.get("title"), TITLE_MAX)
        if title is None:
            skipped += item.get("title") is not None
            continue
        if title in skip:
            repeated += 1
            continue
        source = ctx.external(item.get("source"), NAME_MAX)
        rows.append(_text(title))
        rows.append(_text(f"{source}｜{_md_hm(moment)}" if source else _md_hm(moment), size="md", color=GRAY))
        if listed is not None:
            listed.append(title)
        shown += 1
    if not shown:
        rows.append(_text(TEXT_NEWS_SAME_AS_US if repeated else "36 小時內沒有列入的標題", color=GRAY))
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


# ── 川普壓力指數（TACO 指數，MacroMargin；2026-10-24） ────────────────────────────────────────────
# 取代「MM 川普壓力指數：抓不到（要登入）」：Andrew 要卡上有數字和歷史位階（DECISIONS.md 2026-10-24：「川普壓力指數要有那個數字啊！
# 也要有他相對于歷史的位階」→ 改用 MacroMargin 的公開頁）。快照的 macro.TACO 列只有數字、日期和兩個短列舉（status_key、method）；
# 卡上的字全是 market_calc.TEXT_TACO_* 的固定句＋這裡算的數字與日期，來源網站的任何文字都沒有存、不會出現。
# 同一行（≤24 字寬）出現在重點摘要（輪播 1/5）的第 6 列和宏觀與事件（5/5）的頁尾；新聞與宏觀（3/3）頁尾的第一行是它不帶日期的寫法。
# 寫法由長到短，放不下才退下一個（_taco_line）：
#   ① 川普壓力指數 +9.0（高壓，任期內第 88 百分位）   ② 川普壓力指數 +9.0（任期內第 88 百分位）（沒有狀態詞時從這裡開始）
#   ③ 川普壓力 +9.0｜第 88 百分位                      ④ 川普壓力指數 +9.0
# 資料日比預期的最新場次舊時，日期放在名稱後面（「川普壓力指數 10/15 +9.0（任期內第 88 百分位）」），每個寫法都帶、日期不丟
# （2026-10-28 批 1 審查 F2-1）。帶日期的①通常是 25.5 寬放不下，所以①和②之間多一個寫法：短名稱＋日期＋數字（狀態詞，位階），
# 「川普壓力 10/15 +9.0（高壓，任期內第 88 百分位）」23.5，來源落後的那幾個小時「高壓」還在；數值兩位數且第 100 百分位、或數值
# 三位數時是 24.5，才退到②（第 2 輪複審 seat4 R2-N1、gp1 N9）。不帶日期時①最壞 24.0，一定放得下。
# 宏觀與事件（5/5）的頁尾再接一句說明句「…不是退讓的機率」（F2-2）。
# 細節卡（新聞與宏觀 3/3）的頁尾放完整說明：第一行是不帶日期的完整寫法（「高壓」永遠在）、這一筆的資料日（TEXT_TACO_DATA_DAY）、
# 20 個交易日變化、位階（起始日、交易日筆數、最高最低與日期）、固定說明、來源、組成。
# 過期／暫缺寫「川普壓力指數：過期（資料日 M/D）」「川普壓力指數：暫缺」；快照裡這一列的型別或範圍不對
# （market_calc.taco_values_ok）也當暫缺；舊快照沒有這一列 → 暫缺。狀態詞只認 market_calc.TACO_STATUS_WORDS 的鍵。
TACO_NOTE_MAX = 80  # 細節卡的「組成」那句比一般註記（NOTE_MAX）長


def _taco_view(ctx: _Ctx) -> tuple[_View, Optional[dict]]:
    """(列, 可用的 values)：新鮮而且通過 market_calc.taco_values_ok 才有 values，否則 None（過期、暫缺、壞掉）。"""
    view = ctx.row("macro.TACO")
    if not view.fresh:
        return view, None
    if not market_calc.taco_values_ok(view.values):
        return _View(MISSING, {}, view.data_date), None
    return view, view.values


def _taco_line(ctx: _Ctx, *, dated: bool = True) -> str:
    """摘要卡頁尾那一行（重點摘要的第 6 列也是它；細節卡的第一行是 dated=False 的它）。

    資料日比預期的最新場次（progress.us_expected_day，沒有就取美股其他列最新的資料日）舊時，日期放在名稱後面（「川普壓力指數
    10/22 +9.0（任期內第 88 百分位）」）：來源是個人網站，常比其他列晚一個場次，過期規則又容許落後最多 3 個場次，不標就看不出來
    （2026-10-28 批 1 審查 F2-1）。寫法由長到短，日期一直留著：① 名稱 日期 數字（狀態詞，位階） ①′ 短名稱 日期 數字（狀態詞，
    位階）——只在帶日期、有狀態詞時才有（第 2 輪複審 seat4 R2-N1、gp1 N9：以前①放不下就直接退到②，落後的那幾個小時三個地方的
    「高壓」一起消失） ② 拿掉狀態詞 ③ 短名稱 日期 數字｜位階 ④ 名稱 日期 數字。

    dated=False：一律不帶日期（新聞與宏觀 3/3 用：那裡第二行另寫「資料日 M/D」，第一行寫完整的寫法，「高壓」永遠在）。"""
    view, values = _taco_view(ctx)
    name = market_calc.TEXT_TACO_NAME
    if values is None:
        return _fit(f"{name}：{ctx.gap(view)}", f"{name}：{ctx.gap(view, short=True)}")
    number = _signed(values["value"], 1)
    word = market_calc.TACO_STATUS_WORDS.get(values["status_key"])
    rank = market_calc.TEXT_TACO_RANK.format(p=values["percentile"])
    expected = _digest_expected_day(ctx, "us_expected_day", _DIGEST_US_EVIDENCE) if dated else None
    stamp = _digest_stamp([view.data_date], expected)
    return _fit(
        f"{name}{stamp} {number}（{word}，{rank}）" if word else None,
        f"{market_calc.TEXT_TACO_NAME_SHORT}{stamp} {number}（{word}，{rank}）" if word and stamp else None,
        f"{name}{stamp} {number}（{rank}）",
        f"{market_calc.TEXT_TACO_NAME_SHORT}{stamp} {number}｜{market_calc.TEXT_TACO_RANK_SHORT.format(p=values['percentile'])}",
        f"{name}{stamp} {number}",
    )


def _taco_position(values: dict) -> str:
    """位階那一行。日期寫完整的年月日：歷史從 2025-03-26 起，只寫月／日的話「3/26 起…最高 3/27」看起來像隔天，
    其實差一年（2026-10-28 整合時改）。年份來自資料，固定句本身不寫年份。"""
    return market_calc.TEXT_TACO_POSITION.format(
        since=date.fromisoformat(values["since"]).isoformat(), n=values["n"], p=values["percentile"],
        hi=_signed(values["max"], 1), hi_md=date.fromisoformat(values["max_date"]).isoformat(),
        lo=_signed(values["min"], 1), lo_md=date.fromisoformat(values["min_date"]).isoformat(),
    )


def _taco_summary_notes(ctx: _Ctx) -> list[str]:
    """宏觀與事件（5/5）的頁尾：那一行；指數有數字時再加一句「不是退讓的機率」（2026-10-28 批 1 審查 F2-2：使用者最容易把百分位
    讀成機率，以前這句只在新聞與宏觀 3/3 的最底下）。"""
    _view, values = _taco_view(ctx)
    notes = [_taco_line(ctx)]
    if values is not None:
        notes.append(market_calc.TEXT_TACO_MEANING)
    return [text for text in (ctx.note(note) for note in notes) if text]


def _taco_detail_notes(ctx: _Ctx) -> list[str]:
    view, values = _taco_view(ctx)
    # 第一行是不帶日期的完整寫法：這裡沒有和摘要一樣的取捨，「高壓」永遠在；日期只寫在下一行（第 2 輪複審 seat4 R2-N1）
    notes = [_taco_line(ctx, dated=False)]
    if values is not None:
        if view.data_date:  # 這一筆是哪一天的（F2-1）
            notes.append(market_calc.TEXT_TACO_DATA_DAY.format(md=_md(view.data_date)))
        change = _num(values.get("chg20"), -market_calc.TACO_CHANGE_MAX, market_calc.TACO_CHANGE_MAX)
        notes.append(market_calc.TEXT_TACO_CHANGE.format(chg=_signed(change, 1) if change is not None else TEXT_MISSING))
        notes += [_taco_position(values), market_calc.TEXT_TACO_MEANING]
    notes.append(market_calc.TEXT_TACO_SOURCE)
    if values is not None:  # 只有來源的方法版本是我們看過的（method＝v1）才寫組成；版本變了不拿舊的組成冒充
        notes.append(market_calc.TEXT_TACO_COMPOSITION if values["method"] == "v1" else market_calc.TEXT_TACO_METHOD_CHANGED)
    return [text for text in (ctx.note(note, TACO_NOTE_MAX) for note in notes) if text]


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
        if view.provisional:  # 用 CBOE 暫定收盤算的（目前只有 SPY 距高點；2026-10-24 第 9 輪 A27）
            line += market_calc.TEXT_PROVISIONAL
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
    shown: dict[str, int] = {}  # 各區列出幾則（altText 用）；有一張出錯就不寫則數，免得把「沒列出」寫成 0 則

    def _news_markets_bubble(ctx: _Ctx, head: str, subtitle: Optional[str]) -> dict:
        # 美股 → 半導體 → 台股；半導體不重複列美股段已經列出的標題（2026-10-24 第 9 輪 A17）
        us_titles: list[str] = []
        us_rows, shown["us"], us_skip = _news_rows(ctx, "news.us", "美股新聞", NEWS_PER_KIND, listed=us_titles)
        semi_rows, shown["semi"], semi_skip = _news_rows(ctx, "news.semi", "半導體新聞", NEWS_PER_KIND, skip=us_titles)
        tw_rows, shown["tw"], tw_skip = _news_rows(ctx, "news.tw", "台股新聞", NEWS_PER_KIND)
        return _bubble(head, us_rows + [_sep()] + semi_rows + [_sep()] + tw_rows, subtitle=subtitle,
                       notes=[TEXT_NEWS_NOTE, *([TEXT_TAIPEI_TIME] if shown["us"] or shown["semi"] or shown["tw"] else []),
                              *_skipped_note(us_skip + semi_skip + tw_skip)])

    def _news_iran_trump_bubble(ctx: _Ctx, head: str, subtitle: Optional[str]) -> dict:
        iran_rows, shown["iran"], iran_skip = _news_rows(ctx, "news.iran", "伊朗新聞", IRAN_NEWS_MAX)
        trump_rows, shown["trump"], trump_skip = _trump_rows(ctx)
        return _bubble(head, iran_rows + [_sep()] + trump_rows, subtitle=subtitle,
                       notes=[TEXT_NEWS_NOTE, market_calc.TEXT_TRUMP_NOTE,
                              *([TEXT_TAIPEI_TIME] if shown["iran"] else []), *_skipped_note(iran_skip + trump_skip)])

    def _news_macro_bubble(ctx: _Ctx, head: str, subtitle: Optional[str]) -> dict:
        return _bubble(head, _macro_rows(ctx), subtitle=subtitle, notes=_taco_detail_notes(ctx),
                       buttons=(("季節性", "/盤勢 季節性"), ("期中選舉", "/盤勢 選舉")))

    bubbles, errors = _guarded_bubbles(
        ctx,
        [("新聞（1/3）", _news_markets_bubble), ("伊朗新聞與川普原文（2/3）", _news_iran_trump_bubble),
         ("宏觀與事件（3/3）", _news_macro_bubble)],
        "往左滑還有 2 張",
    )
    alt = f"{ALT_PREFIX}新聞與宏觀"
    if not errors:
        alt += (f"｜美股 {shown['us']} 則、半導體 {shown['semi']} 則、台股 {shown['tw']} 則、"
                f"伊朗 {shown['iran']} 則、川普 {shown['trump']} 則")
    return _flex_reply(_carousel(bubbles), alt, render_errors=errors)


# ── 細節：季節性 ─────────────────────────────────────────────────────────────
def _season_stat(stats: Any) -> Optional[str]:
    stats = _dict(stats)
    up, n = _num(stats.get("up_pct"), 0, 100), _count(stats.get("n"), 1000)
    median, mean = _num(stats.get("median"), -100, 1000), _num(stats.get("mean"), -100, 1000)
    if up is None or not n:
        return None
    text = f"上漲比例 {_plain_pct(up)}（{n} 年）"
    if median is None or mean is None:
        return text
    # 變體順序（2026-10-24 第 9 輪 A14）：全寫 → 拿掉平均 → 只留比例與年數。現有的真實值最寬剛好 24.0（SOXX 的第四季），
    # 放得下；比例 100% 且漲跌幅到雙位數、或年數三位數才會用到後兩個
    return _fit(f"{text}，中位數 {_pct(median)}、平均 {_pct(mean)}", f"{text}，中位數 {_pct(median)}", text)


_YEAR_MONTH_RE = re.compile(r"(\d{4})-(\d{2})", re.ASCII)  # re.ASCII：全形、阿拉伯數字不收（第 2 輪複審 gp1 N7）


def _year_month_text(value: Any) -> Optional[str]:
    """「2016-11」→「2016-11」；格式不對、月份不在 1–12、年份離譜 → None（卡上只印過了檢查的數字）。"""
    match = _YEAR_MONTH_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    return f"{year}-{month:02d}" if 1900 <= year <= 2100 and 1 <= month <= 12 else None


def _season_range(value: Any) -> str:
    """樣本起迄「2016-11 至 2025-12」（年份從 market_seasonality.json 經快照帶出來，固定句不寫年份）；格式不對印「暫缺」。"""
    node = _dict(value)
    first, last = _year_month_text(node.get("first")), _year_month_text(node.get("last"))
    return f"{first} 至 {last}" if first and last and first <= last else TEXT_MISSING


def _season_nodes(label: str, stats: Any) -> list[dict]:
    text = _season_stat(stats)
    return [_text(label, weight="bold"), _text(text or TEXT_INSUFFICIENT, size="md", color=None if text else GRAY)]


def _season_info(sample: str, data: str, value: Any) -> list[dict]:
    """半導體一段結尾的兩行灰字：樣本起迄、資料來源與調整方式（各自 ≤24 個中文字寬）。"""
    return [_text(sample.format(range=_season_range(value)), size="md", color=GRAY), _text(data, size="md", color=GRAY)]


def _soxx_nodes(v: dict, month: int, midterm: bool) -> list[dict]:
    """SOXX 自身（Nasdaq 只給最近 10 年的 ETF 日線）：月、第四季；期中選舉年樣本夠大（≥5 年，market_calc 檢查過）才有那兩段，
    否則一行說明。"""
    head = market_calc.TEXT_SEASON_SOXX_HEAD.format(month=month)
    if _flag(v.get("soxx_pending")) is not False:  # 舊快照沒有這個鍵：當作還沒有資料
        return _season_nodes(head, None)
    nodes = _season_nodes(head, v.get("soxx")) + _season_nodes(market_calc.TEXT_SEASON_SOXX_Q4, v.get("soxx_q4"))
    if midterm:
        if _dict(v.get("soxx_midterm")) or _dict(v.get("soxx_midterm_q4")):
            nodes += _season_nodes(market_calc.TEXT_SEASON_SOXX_MIDTERM.format(month=month), v.get("soxx_midterm"))
            nodes += _season_nodes(market_calc.TEXT_SEASON_SOXX_MIDTERM_Q4, v.get("soxx_midterm_q4"))
        else:
            nodes.append(_text(market_calc.TEXT_SEASON_SOXX_FEW_MIDTERM, size="md", color=GRAY))
    return nodes + _season_info(market_calc.TEXT_SEASON_SOXX_SAMPLE, market_calc.TEXT_SEASON_SOXX_DATA, v.get("soxx_range"))


def _sox_nodes(v: dict, month: int, midterm: bool) -> list[dict]:
    """SOX（費城半導體指數）長歷史：月、第四季、期中選舉年的月與第四季。"""
    head = market_calc.TEXT_SEASON_SOX_HEAD.format(month=month)
    if _flag(v.get("sox_pending")) is not False:
        return _season_nodes(head, None)
    nodes = _season_nodes(head, v.get("sox")) + _season_nodes(market_calc.TEXT_SEASON_SOX_Q4, v.get("sox_q4"))
    if midterm:
        nodes += _season_nodes(market_calc.TEXT_SEASON_SOX_MIDTERM.format(month=month), v.get("sox_midterm"))
        nodes += _season_nodes(market_calc.TEXT_SEASON_SOX_MIDTERM_Q4, v.get("sox_midterm_q4"))
    return nodes + _season_info(market_calc.TEXT_SEASON_SOX_SAMPLE, market_calc.TEXT_SEASON_SOX_DATA, v.get("sox_range"))


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
        tw_sections = []
        if _flag(v.get("taiex_pending")):
            tw_sections.append((f"加權指數 {month} 月", None))
        else:
            tw_sections.append((f"加權指數 {month} 月，{market_calc.TEXT_SEASON_TW_BASIS}", v.get("taiex")))
            tw_sections.append(("加權指數第四季", v.get("taiex_q4")))
            if midterm:
                tw_sections.append((f"期中選舉年的 {month} 月（加權指數）", v.get("taiex_midterm")))
        # 四組由上而下：SOXX 自身（Andrew 看的標的）、SOX 長歷史、標普500、加權指數；組與組之間一條線
        groups = [
            _soxx_nodes(v, month, midterm),
            _sox_nodes(v, month, midterm),
            [node for label, stats in sections for node in _season_nodes(label, stats)],
            [node for label, stats in tw_sections for node in _season_nodes(label, stats)],
        ]
        for index, group in enumerate(groups):
            if index:
                body.append(_sep())
            body += group
    else:
        body.append(_text(f"季節性：{ctx.gap(view)}", color=GRAY))
    bubble = _bubble(
        "季節性" + (f"（{month} 月）" if month else "") + "｜歷史統計",
        body,
        notes=(market_calc.TEXT_SEASON_NOTE, market_calc.TEXT_SEASON_SEMI_NOTE, market_calc.TEXT_SEASON_BASIS_NOTE,
               TEXT_SEASON_SOURCE),
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
    blocked = _prediction_blocked(view)
    quotes = [] if blocked else _quotes(view)
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
    if blocked:  # 來源被封鎖：固定句說明，不寫成「暫缺」；沒有報價可印，「兩平台結算規則不同」那句也不放
        body.append(_text(TEXT_PREDICTION_BLOCKED, color=GRAY))
    elif not quotes:
        body.append(_text(f"預測市場：{ctx.gap(view)}", color=GRAY))
    body.append(_text(market_calc.TEXT_POLL_NONE, size="md", color=GRAY))
    bubble = _bubble(
        f"美國期中選舉｜{vote} 投票",
        body,
        notes=(*(() if blocked else (market_calc.TEXT_ELECTION_NOTE,)), *([TEXT_TAIPEI_TIME] if clock else ())),
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
    # 日曆還涵蓋幾天、休市表認不出的日數（2026-10-18 審查 r6a I1／I2）：每日維護在 ≤30 天或 >0 時報。
    # 2026-10-23 審查 r7b F7：us／tw 一定有鍵，outcome 缺 key、{"error": …}、舊版 job、壞值都落在 None——以前是 {}，
    # 維護條目的「或是 null」永遠不會觸發
    today = datetime.fromtimestamp(now, _TPE).date()
    until_by_market = _dict(data.get("calendar_until"))
    days_left: dict = {}
    for market in ("us", "tw"):
        value = until_by_market.get(market)
        try:
            until = date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
        except ValueError:
            until = None
        days_left[market] = (until - today).days if until else None
    out["calendar_days_left"] = days_left
    unclassified = _dict(data.get("calendar_unclassified"))
    out["calendar_unclassified"] = {market: _count(unclassified.get(market), 10**4) for market in ("us", "tw")}
    # 休市表上是交易日卻沒有收盤的天數（2026-07-10 那種）；每日維護從這裡讀（2026-10-23 審查 r9 gp1 N2）
    missing = _dict(data.get("calendar_missing_bars"))
    out["calendar_missing_bars"] = {market: _count(missing.get(market), 10**4) for market in ("us", "tw")}
    # 美股資料落後多少（2026-10-23，investigations/format_20261023.md §2、freshness_20261023.md §5 第 5 點、審查 r7b
    # F2）：us.etf 列最舊的資料日落後最新場次幾個交易日、落後幾秒；成分股中位數落後幾個交易日；這個 D 的 Nasdaq
    # 廠商錯誤次數。這次落後 5 天時 us.hist 只是 not_yet，failing 看不到；缺值或壞值 → None
    out["us_etf_lag_sessions"] = _count(data.get("us_etf_lag_sessions"), 10**4)
    out["us_etf_lag_s"] = _count(data.get("us_etf_lag_s"), 10**9)
    out["us_members_lag_sessions"] = _count(data.get("us_members_lag_sessions"), 10**4)
    out["us_hist_vendor"] = _count(data.get("us_hist_vendor"), 10**6)
    # 通膨（BLS CPI；2026-10-24 審查 r9 s4-F15）：BLS 一個月才抓幾次，failing 要連續 3 次失敗才列，一個月內永遠到不了；已存最新月份、
    # 落後幾個月、輪詢狀態（pending／done／exhausted／disabled）給每日維護看，缺值或壞值 → None
    month = data.get("infl_cpi_month")
    out["infl_cpi_month"] = month if isinstance(month, str) and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month, re.ASCII) else None
    out["infl_cpi_lag_months"] = _count(data.get("infl_cpi_lag_months"), 1200)
    poll = data.get("infl_bls_poll")
    out["infl_bls_poll"] = poll if isinstance(poll, str) and poll in market_calc.CPI_POLL_STATES else None
    out["taco_block_streak"] = _count(data.get("taco_block_streak"), 10**6)  # ocmacro.com 連續被擋的次數（2026-10-28 審查 gp2 N1）
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
    # render_error 只放程式自己的列舉字串：整個回覆出錯＝「unexpected:例外型別名」；輪播裡某一張出錯（已換成短卡、其他張照送）
    # ＝「bubble:builder 名稱:例外型別名」，多個用逗號接起來（2026-10-24 審查 r9 s4-F9-1）
    problems: list[str] = []
    bubble_errors: set[str] = set()
    try:
        for kind, sym in ((SUMMARY, None), (ETF, "VOO"), (ETF, "QQQ"), (ETF, "SOXX"), (TW, None), (OPTIONS, None),
                          (NEWS, None), (SEASON, None), (ELECTION, None), (F13, "VOO"), (F13, "QQQ"), (F13, "SOXX")):
            bubble_errors.update(_DETAILS[kind](ctx, sym).render_errors)
    except Exception as exc:
        problems.append(f"unexpected:{type(exc).__name__}")
    if bubble_errors:
        problems.append("bubble:" + ",".join(sorted(bubble_errors)))
    if problems:
        summary["render_error"] = ";".join(problems)
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
    "news.semi",
    "macro.TACO",
)
