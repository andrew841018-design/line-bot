"""/盤勢 整條管線的離線端到端測試（2026-10-18）：collect（三個 pipeline）→ publish → bot 組卡。

Andrew 2026-10-17 要 /盤勢 一次看美股／台股的技術面、寬度、量能、期權、籌碼、槓桿、13F、宏觀與事件，
按了才回、只描述狀態。各模組都有自己的單元測試；這裡把它們全部接起來跑一遍，抓的是接縫：欄位名、
簽名、obs 鍵、publish 寫了 bot 讀不懂的列、卡片超過 LINE 的大小上限、外部字串漏過清洗。

做法（全部離線、全部決定性）：
- 全新的 `Paths.under(tmp)`：state/ 先建好 0700（job 不建它），季節性表照部署那樣放在根目錄。
- 假的 http 照 `market_http.fetch` 的樣子：白名單、先在帳本預扣、大小上限、各主機實測的 Content-Type；
  依主機與路徑回「照真實格式合成」的回應。內容全部寫在這個檔裡（repo 外的 fixtures_raw 不讀，也不放
  任何家人資料）。價格是（代號, 交易日序）的固定函式：Nasdaq、證交所 ALLBUT、FinMind 用同一份，才過得了
  覆寫守門（差 >0.5%）與 FinMind 接入比對（差 >0.005）。不認得的網址記下來，由
  `test_the_router_only_saw_known_requests` 印出網址並失敗；真的 market_http.fetch 被換成只記錄、不連網。
- 真的 collect 路徑：三個 pipeline 的任務、真的 Store＋StoreLedger、job 自己的日曆建法與時間預算
  （COLLECT_TIMEOUT_S − COLLECT_MARGIN_S）；時鐘是假的（帳本等間隔時時間往前走，測試不真的等）。
- 時間線兩段。台股 bootstrap 本來就要跨天（PLAN §18.12：ALLBUT 每天最多 4 次，FinMind 接入前要有 5 天
  證交所收盤可比），所以一個晚上跑不完：
  ① 台北 10/16（五）18:00 起每 15 分鐘一輪：美東週五清晨，D＝10/15、T＝10/16；
  ② Mac 睡過一夜，台北 10/17（六）08:30 起每 15 分鐘一輪：美東週五 20:30，D＝10/16、T＝10/16。
  每段跑到「除了定時來源（新聞、川普）之外沒有任何請求」為止（上限 MAX_ROUNDS 輪），
  每段結束照 job 的做法另開一個 Store 連線跑一次真的 publish（真的畫圖）。
- 刻意放進來的狀況：一檔 10/16 生效的 1:10 反分割（Nasdaq 收盤後才調整歷史 → 覆寫守門＋整段替換）、一檔
  6 月已調整的 4:1 分割、一檔 10/16 財報跳空 +22%（待確認→兩次一致接受）、一檔 10/14 起停牌（沒有 D 列）、
  Yahoo 第二天 429（宏觀改代理）、美債哥倫布日休市、台股除權息、期交所選擇權晚一天、新聞標題帶網址／@／
  雙向控制字元、川普貼文被移除／只有圖片／引用。

斷言：沒有 unexpected:*；快照 0600、每列過 `market_calc.row_ok`；餵過資料的列不是 never；
SHIPPED_MARKET_TEXTS 每個指令都有回覆、卡片大小照 `market_dashboard.card_bytes`／`card_pages` 的量法、
altText 前綴、圖檔在圖目錄且是 PNG、文字沒有網址／電話／@／隱形字元；選舉細節有眾院與參院。
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import stat
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

import openpyxl
import pytest

import card_pages
import market_calc
import market_calendar
import market_dashboard
import market_http
import market_paths
import market_pipeline_misc
import market_pipeline_tw
import market_pipeline_us
import market_publish
import market_store
import market_tasks
from jobs import market_snapshot as job

TPE = ZoneInfo("Asia/Taipei")
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
WORK = Path(__file__).resolve().parent

# ── 時間線 ────────────────────────────────────────────────────────────────────
PHASES = (
    ("fri_evening", datetime(2026, 10, 16, 18, 0, tzinfo=TPE)),  # 美東 10/16 06:00：D＝10/15、T＝10/16
    ("sat_morning", datetime(2026, 10, 17, 8, 30, tzinfo=TPE)),  # 美東 10/16 20:30：D＝10/16、T＝10/16
)
ROUND_S = 900.0  # launchd StartInterval
MAX_ROUNDS = 16  # 每段的上限；PLAN §9 的「9 輪抓齊」只是估計，實際輪數印在報表
REQUEST_S = 0.2  # 假的網路延遲：每個請求讓時鐘走這麼多秒
COLLECT_BUDGET_S = job.COLLECT_TIMEOUT_S - min(job.COLLECT_MARGIN_S, job.COLLECT_TIMEOUT_S * 0.25)
# 每 15–120 分鐘固定會打的來源：它們有請求不代表 collect 還沒做完
PERIODIC_HOSTS = frozenset({"news.google.com", "www.trumpstruth.org"})
D1, D2 = date(2026, 10, 15), date(2026, 10, 16)  # 兩段的美股 D
T = date(2026, 10, 16)  # 兩段的台股 T

# ── 交易日曆（合成資料用；不靠被測的日曆） ─────────────────────────────────────────
US_HOLIDAYS = frozenset(market_calendar.NYSE_HOLIDAYS) | {
    date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
}
US_BOND_HOLIDAYS = frozenset({date(2026, 10, 12), date(2026, 11, 11)})  # 哥倫布日、退伍軍人節：債市休、股市開

# 證交所休市表：2026 年照真實的 27 列（公開的交易所公告）；2025 年照同樣的說法合成
TW_HOLIDAYS = {
    2025: [
        ["2025-01-01", "中華民國開國紀念日", "依規定放假1日。"],
        ["2025-01-22", "農曆春節前最後交易日", "農曆春節前最後交易。"],
        ["2025-01-23", "市場無交易，僅辦理結算交割作業", ""],
        ["2025-01-24", "市場無交易，僅辦理結算交割作業", ""],
        ["2025-01-27", "農曆除夕及春節", "依規定放假。"],
        ["2025-01-28", "農曆除夕及春節", "依規定放假。"],
        ["2025-01-29", "農曆除夕及春節", "依規定放假。"],
        ["2025-01-30", "農曆除夕及春節", "依規定放假。"],
        ["2025-01-31", "農曆除夕及春節", "依規定放假。"],
        ["2025-02-03", "農曆春節後開始交易日", "農曆春節後開始交易。"],
        ["2025-02-28", "和平紀念日", "依規定放假1日。"],
        ["2025-04-03", "兒童節及民族掃墓節", "兒童節補假。"],
        ["2025-04-04", "兒童節及民族掃墓節", "依規定放假1日。"],
        ["2025-05-01", "勞動節", "依規定放假1日。"],
        ["2025-05-30", "端午節", "端午節為5月31日適逢星期六，於5月30日（星期五）補假。"],
        ["2025-09-29", "孔子誕辰紀念日/ 教師節", "教師節為9月28日適逢星期日，於9月29日（星期一）補假。"],
        ["2025-10-06", "中秋節", "依規定放假1日。"],
        ["2025-10-10", "國慶日", "依規定放假1日。"],
        ["2025-10-24", "臺灣光復暨金門古寧頭大捷紀念日", "10月25日適逢星期六，於10月24日（星期五）補假。"],
        ["2025-12-25", "行憲紀念日", "依規定放假1日。"],
    ],
    2026: [
        ["2026-01-01", "中華民國開國紀念日", "依規定放假1日。"],
        ["2026-01-02", "國曆新年開始交易日", "國曆新年開始交易。"],
        ["2026-02-11", "農曆春節前最後交易日", "農曆春節前最後交易。\r\n"],
        ["2026-02-12", "市場無交易，僅辦理結算交割作業", ""],
        ["2026-02-13", "市場無交易，僅辦理結算交割作業", ""],
        *[
            [f"2026-02-{day}", "農曆除夕及春節",
             "依規定於2月15日至2月19日放假5日。2月15日適逢星期日，於2月20日（星期五）補假。"]
            for day in (15, 16, 17, 18, 19, 20)
        ],
        ["2026-02-23", "農曆春節後開始交易日", "農曆春節後開始交易。"],
        ["2026-02-27", "和平紀念日", "和平紀念日為2月28日適逢星期六，於2月27日（星期五）補假。"],
        ["2026-02-28", "和平紀念日", "依規定放假1日。"],
        ["2026-04-03", "兒童節及民族掃墓節", "兒童節為4月4日適逢星期六，於4月3日（星期五）補假。"],
        ["2026-04-04", "兒童節及民族掃墓節", "依規定放假1日。"],
        ["2026-04-05", "兒童節及民族掃墓節", "依規定放假1日。"],
        ["2026-04-06", "兒童節及民族掃墓節", "民族掃墓節為4月5日適逢星期日，於4月6日（星期一）補假。"],
        ["2026-05-01", "勞動節", "依規定放假1日。"],
        ["2026-06-19", "端午節", "依規定放假1日。"],
        ["2026-09-25", "中秋節", "依規定放假1日。"],
        ["2026-09-28", "孔子誕辰紀念日/ 教師節", "依規定放假1日。"],
        ["2026-10-09", "國慶日", "國慶日為10月10日適逢星期六，於10月9日（星期五）補假。"],
        ["2026-10-10", "國慶日", "依規定放假1日。"],
        ["2026-10-25", "臺灣光復暨金門古寧頭大捷紀念日", "依規定放假1日。"],
        ["2026-10-26", "臺灣光復暨金門古寧頭大捷紀念日",
         "臺灣光復暨金門古寧頭大捷紀念日為10月25日適逢星期日，於10月26日（星期一）補假。"],
        ["2026-12-25", "行憲紀念日", "依規定放假1日。"],
    ],
}
_TW_OPEN_WORDS = ("開始交易", "最後交易")
TW_CLOSED = frozenset(
    date.fromisoformat(row[0])
    for rows in TW_HOLIDAYS.values()
    for row in rows
    if not any(word in row[1] + row[2] for word in _TW_OPEN_WORDS)
)


def _sessions(start: date, end: date, closed: frozenset) -> list[date]:
    out, day = [], start
    while day <= end:
        if day.weekday() < 5 and day not in closed:
            out.append(day)
        day += timedelta(days=1)
    return out


US_SESSIONS = _sessions(date(2025, 6, 2), date(2026, 12, 31), US_HOLIDAYS)
TW_SESSIONS = _sessions(date(2025, 6, 2), date(2026, 12, 31), TW_CLOSED)
US_INDEX = {day: i for i, day in enumerate(US_SESSIONS)}
TW_INDEX = {day: i for i, day in enumerate(TW_SESSIONS)}


def _prev_in(sessions_index: dict[date, int], sessions: list[date], day: date) -> Optional[date]:
    i = sessions_index.get(day)
    return sessions[i - 1] if i else None


def us_prev(day: date) -> Optional[date]:
    return _prev_in(US_INDEX, US_SESSIONS, day)


def tw_prev(day: date) -> Optional[date]:
    return _prev_in(TW_INDEX, TW_SESSIONS, day)


def et(now: float) -> datetime:
    return datetime.fromtimestamp(now, ET)


def tpe(now: float) -> datetime:
    return datetime.fromtimestamp(now, TPE)


def us_closed_through(now: float, after: tuple[int, int] = (16, 20)) -> date:
    """美東最近一個「收盤後 after 時刻已過」的交易日（假來源公布日線的時間）。"""
    moment = et(now)
    day = moment.date()
    while True:
        if day in US_INDEX and (day < moment.date() or (moment.hour, moment.minute) >= after):
            return day
        day -= timedelta(days=1)


def tw_published_through(now: float, at: tuple[int, int]) -> date:
    """台北最近一個「當天 at 時刻已過」的交易日（證交所／期交所各端點公布的時間）。"""
    moment = tpe(now)
    day = moment.date()
    while True:
        if day in TW_INDEX and (day < moment.date() or (moment.hour, moment.minute) >= at):
            return day
        day -= timedelta(days=1)


# ── 價格模型：（市場, 代號, 交易日序）的固定函式 ─────────────────────────────────────


def _h(*parts: object) -> int:
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).digest()[:8], "big")


def _u(*parts: object) -> float:
    """[0, 1) 的固定亂數。"""
    return _h(*parts) / 2.0**64


@dataclass(frozen=True)
class Walk:
    base: float
    drift: float
    a1: float
    p1: float
    f1: float
    a2: float
    p2: float
    f2: float
    volume: int

    def level(self, t: int) -> float:
        return self.base * math.exp(
            self.drift * t + self.a1 * math.sin(2 * math.pi * t / self.p1 + self.f1)
            + self.a2 * math.sin(2 * math.pi * t / self.p2 + self.f2)
        )

    def shares(self, t: int) -> int:
        return max(1000, int(self.volume * (1.0 + 0.55 * math.sin(2 * math.pi * t / 17.0 + self.f2))))


US_ETF_BASE = {
    "SPY": 660.0, "VOO": 606.0, "QQQ": 712.0, "SOXX": 282.0,
    "UUP": 27.4, "USO": 74.0, "BNO": 30.5, "GLD": 381.0, "SLV": 45.2, "IBIT": 46.0, "IWM": 246.0, "DIA": 462.0,
    "ONEQ": 88.0,
}


def walk(market: str, symbol: str) -> Walk:
    """每檔自己的走勢：漂移＋兩個週期，每個交易日最多變動約 3%（不會碰到 ±10%／±15% 的守門）。"""
    if market == "us" and symbol in US_ETF_BASE:
        base = US_ETF_BASE[symbol]
    elif market == "tw" and symbol in TW_BASE:
        base = TW_BASE[symbol]
    else:
        base = 15.0 + 485.0 * _u(market, symbol, "base")
    return Walk(
        base=base,
        drift=(_u(market, symbol, "drift") - 0.45) * 0.0016,
        a1=0.03 + 0.09 * _u(market, symbol, "a1"),
        p1=70.0 + 120.0 * _u(market, symbol, "p1"),
        f1=2 * math.pi * _u(market, symbol, "f1"),
        a2=0.005 + 0.02 * _u(market, symbol, "a2"),
        p2=12.0 + 18.0 * _u(market, symbol, "p2"),
        f2=2 * math.pi * _u(market, symbol, "f2"),
        volume=int(2e5 + 5e7 * _u(market, symbol, "vol")),
    )


# 特殊的美股成分（都是合成代號，放在 SPY 名單裡）
SPLIT_NOW = "YKND"  # 10/16 生效 1:10 反分割：Nasdaq 在 10/16 收盤後才把歷史乘 10（之前的請求拿到未調整的價）
SPLIT_NOW_DAY = date(2026, 10, 16)
SPLIT_NOW_ADJUSTED_FROM = datetime(2026, 10, 16, 16, 0, tzinfo=ET).timestamp()
SPLIT_OLD = "YOLD"  # 6/15 生效 4:1 分割：Nasdaq 早就調整好了（生效日前後連續）
SPLIT_OLD_DAY = date(2026, 6, 15)
JUMP = "YJMP"  # 10/16 財報跳空 +22%、沒有分割：待確認，下一輪同樣的值 → 接受
JUMP_DAY = date(2026, 10, 16)
HALTED = "YHLT"  # 10/14 之後停牌：沒有 D 列（重排 4 次後放棄）
HALTED_LAST = date(2026, 10, 14)


def us_close(symbol: str, day: date, now: float) -> float:
    t = US_INDEX[day]
    value = walk("us", symbol).level(t)
    if symbol == SPLIT_NOW:
        value = 4.0 * value / walk("us", symbol).base  # 調整後約 4 美元
        if day < SPLIT_NOW_DAY and now < SPLIT_NOW_ADJUSTED_FROM:
            value /= 10.0  # 還沒調整：生效前是 0.4 美元左右
    if symbol == JUMP and day >= JUMP_DAY:
        value *= 1.22
    return round(value, 2)


def us_volume(symbol: str, day: date) -> int:
    return walk("us", symbol).shares(US_INDEX[day])


# ── 美股名單（真實的公開代號與名稱只用在前幾大；其他都是合成的 Y／W／V 開頭代號） ────────────────


def _letters(i: int, width: int = 3) -> str:
    out = ""
    for _ in range(width):
        out = chr(65 + i % 26) + out
        i //= 26
    return out


SPY_REAL = [
    ("NVIDIA CORP", "NVDA", 7.88), ("APPLE INC", "AAPL", 6.95), ("MICROSOFT CORP", "MSFT", 6.40),
    ("AMAZON.COM INC", "AMZN", 3.95), ("META PLATFORMS INC CLASS A", "META", 2.90), ("BROADCOM INC", "AVGO", 2.55),
    ("ALPHABET INC CL A", "GOOGL", 2.40), ("TESLA INC", "TSLA", 2.05), ("ALPHABET INC CL C", "GOOG", 1.95),
    ("BERKSHIRE HATHAWAY INC CL B", "BRK.B", 1.47), ("JPMORGAN CHASE + CO", "JPM", 1.40),
    ("ELI LILLY + CO", "LLY", 1.20), ("VISA INC CLASS A SHARES", "V", 1.00), ("NETFLIX INC", "NFLX", 0.90),
    ("COSTCO WHOLESALE CORP", "COST", 0.85), ("ADVANCED MICRO DEVICES", "AMD", 0.80),
    ("MICRON TECHNOLOGY INC", "MU", 0.45), ("QUALCOMM INC", "QCOM", 0.40), ("APPLIED MATERIALS INC", "AMAT", 0.35),
    ("TEXAS INSTRUMENTS INC", "TXN", 0.35), ("LAM RESEARCH CORP", "LRCX", 0.33), ("INTEL CORP", "INTC", 0.30),
    ("ANALOG DEVICES INC", "ADI", 0.30), ("KLA CORP", "KLAC", 0.30), ("NXP SEMICONDUCTORS NV", "NXPI", 0.12),
    ("MICROCHIP TECHNOLOGY INC", "MCHP", 0.10), ("MONOLITHIC POWER SYSTEMS INC", "MPWR", 0.08),
    ("ON SEMICONDUCTOR", "ON", 0.06), ("TERADYNE INC", "TER", 0.05), ("BROWN FORMAN CORP CLASS B", "BF.B", 0.0076),
]
SPY_SPECIAL = [SPLIT_NOW, SPLIT_OLD, JUMP, HALTED]
SPY_FILLERS = SPY_SPECIAL + [f"Y{_letters(i)}" for i in range(469)]
SPY_MEMBERS = [symbol for _, symbol, _ in SPY_REAL] + SPY_FILLERS  # 503 檔
NDX_REAL = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "AVGO", "GOOGL", "GOOG", "TSLA", "NFLX", "COST", "AMD", "MU",
            "QCOM", "AMAT", "TXN", "LRCX", "INTC", "ADI", "KLAC", "NXPI", "MCHP", "MPWR", "ON", "ASML", "ARM"]
NDX_ONLY_FILLERS = [f"W{_letters(i)}" for i in range(15)]
NDX_MEMBERS = NDX_REAL + SPY_FILLERS[4:64] + NDX_ONLY_FILLERS  # 101 檔（GOOGL／GOOG 各一）
SOXX_LINKED = ["NVDA", "AVGO", "AMD", "INTC", "MU", "QCOM", "TXN", "ADI", "AMAT", "LRCX", "KLAC", "NXPI", "MCHP",
               "ON", "MPWR", "TER"] + [f"V{_letters(i)}" for i in range(6)]
# MoneyDJ 沒有代號連結的外國股（名稱→代號表；SK Hynix 對不到美國代號）
SOXX_UNLINKED = [
    ("TAIWAN SEMICONDUCTOR MANUFACTURING", "TSM", 7.10), ("ASML HOLDING ADR REPRESENTING", "ASML", 1.46),
    ("TOWER SEMICONDUCTOR", "TSEM", 1.18), ("ASE TECHNOLOGY HOLDING ADR REPRESE", "ASX", 0.89),
    ("UNITED MICRO ELECTRONICS ADR REP", "UMC", 0.68), ("ARM HOLDINGS AMERICAN DEPOSITARY S", "ARM", 0.50),
    ("STMICROELECTRONICS ADR", "STM", 0.42), ("SK HYNIX AMERICAN DEPOSITARY SHARE", None, 3.06),
]
REAL_NAMES = {symbol: name for name, symbol, _ in SPY_REAL}
REAL_NAMES.update({"ASML": "ASML HOLDING NV", "ARM": "ARM HOLDINGS PLC", "TSM": "TAIWAN SEMICONDUCTOR"})
UNIVERSE = sorted(set(SPY_MEMBERS) | set(NDX_MEMBERS) | set(SOXX_LINKED) | {s for _, s, _ in SOXX_UNLINKED if s})


def us_name(symbol: str) -> str:
    return REAL_NAMES.get(symbol) or f"EXAMPLE CO {symbol}"


def spy_weight(symbol: str) -> float:
    for _, sym, weight in SPY_REAL:
        if sym == symbol:
            return weight
    return round(0.04 + 0.11 * _u("spyw", symbol), 6)


def ndx_weight(symbol: str) -> float:
    top = {"NVDA": 9.1, "AAPL": 8.4, "MSFT": 7.9, "AMZN": 5.6, "AVGO": 5.1, "META": 3.9, "GOOGL": 2.9, "TSLA": 2.8,
           "GOOG": 2.7, "NFLX": 2.4, "COST": 2.3, "AMD": 1.9}
    return top.get(symbol, round(0.1 + 0.5 * _u("ndxw", symbol), 4))


# ── 台股名單 ──────────────────────────────────────────────────────────────────
TW_REAL = [("2330", "台積電", 56.66), ("2317", "鴻海", 5.02), ("2454", "聯發科", 4.48), ("2308", "台達電", 2.51),
           ("2382", "廣達", 1.83), ("2891", "中信金", 1.52), ("2881", "富邦金", 1.41), ("2882", "國泰金", 1.30),
           ("2412", "中華電", 1.21), ("3711", "日月光投控", 1.10)]
TW_MEMBERS = [code for code, _, _ in TW_REAL] + [str(6100 + i) for i in range(40)]  # 50 檔
TW_OTHERS = [str(1200 + i) for i in range(20)]  # 不在 0050 的普通股（ALLBUT 一樣有收盤，留在 tw_all）
TW_NO_TRADE = "1299"  # 當天「--」無成交
TW_BASE = {"TAIEX": 47111.26, "0050": 109.0, "2330": 2400.0, "2317": 244.0, "2454": 4250.0, "2308": 1635.0}
TW_EXRIGHTS = [  # (除權息日, 代號, 因子＝參考價／前收)：真實的價格在除權息日跳一次，還原後連續
    (date(2026, 9, 17), "2330", 0.9975),
    (date(2026, 7, 16), "0050", 0.985),
    (date(2026, 8, 20), "6105", 0.97),
]
TW_TOP_WEIGHT = {code: weight for code, _, weight in TW_REAL}


def tw_name(code: str) -> str:
    for real, name, _ in TW_REAL:
        if real == code:
            return name
    return f"示例{code}"


def tw_adjusted(code: str, day: date) -> float:
    return walk("tw", code).level(TW_INDEX[day])


def tw_close(code: str, day: date) -> float:
    """證交所／FinMind 的原始（未還原）收盤：除權息日之前除以因子。"""
    value = tw_adjusted(code, day)
    for ex_day, ex_code, factor in TW_EXRIGHTS:
        if ex_code == code and day < ex_day:
            value /= factor
    return round(value, 2)


def tw_volume(code: str, day: date) -> int:
    return walk("tw", code).shares(TW_INDEX[day])


def tw_turnover(day: date) -> int:
    t = TW_INDEX[day]
    return int(9.9e11 * (1.0 + 0.3 * math.sin(2 * math.pi * t / 19.0)))


# ── 格式小工具 ────────────────────────────────────────────────────────────────


def _comma(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}"


def _mdy(day: date) -> str:
    return day.strftime("%m/%d/%Y")


def _roc(day: date) -> str:
    return f"{day.year - 1911}年{day.month:02d}月{day.day:02d}日"


def _roc_compact(day: date) -> str:
    return f"{day.year - 1911}{day.month:02d}{day.day:02d}"


def _ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def _nasdaq(data: Any) -> dict:
    return {"data": data, "message": None, "status": {"rCode": 200, "bCodeMessage": None, "developerMessage": None}}


def cusip(base8: str) -> str:
    values = []
    for index, ch in enumerate(base8):
        value = int(ch) if ch.isdigit() else ord(ch) - 55
        values.append(value * 2 if index % 2 else value)
    total = sum(v // 10 + v % 10 for v in values)
    return base8 + str((10 - total % 10) % 10)


def _xlsx(sheet_title: str, rows: list[list]) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = sheet_title
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _rfc822(moment: datetime) -> str:
    return format_datetime(moment.astimezone(UTC), usegmt=True)


# ── 美股來源（照 fixtures_raw 的真實格式合成） ───────────────────────────────────


def nasdaq_historical(symbol: str, etf: bool, start: date, end: date, now: float) -> dict:
    through = min(end, us_closed_through(now))
    days = [day for day in US_SESSIONS if start <= day <= through]
    if symbol == HALTED:
        days = [day for day in days if day <= HALTED_LAST]
    days = days[-284:]  # Nasdaq 一次最多回約 13 個月
    money = "" if etf else "$"
    rows = []
    for day in reversed(days):
        close = us_close(symbol, day, now)
        rows.append({"date": _mdy(day), "close": f"{money}{close:.2f}", "volume": f"{us_volume(symbol, day):,}",
                     "open": f"{money}{close:.2f}", "high": f"{money}{close * 1.01:.2f}",
                     "low": f"{money}{close * 0.99:.2f}"})
    headers = {"date": "Date", "close": "Close/Last", "volume": "Volume", "open": "Open", "high": "High", "low": "Low"}
    return _nasdaq({"symbol": symbol, "totalRecords": len(rows),
                    "tradesTable": {"asOf": None, "headers": headers, "rows": rows or None}})


def nasdaq_market_info(now: float) -> dict:
    today = et(now).date()
    prev = max(day for day in US_SESSIONS if day < today)
    following = min(day for day in US_SESSIONS if day > today)
    fmt = "%b %d, %Y"
    return _nasdaq({
        "country": "U.S.", "marketIndicator": "Market Closed",
        "marketOpeningTime": f"{today.strftime(fmt)} 09:30 AM ET",
        "marketClosingTime": f"{today.strftime(fmt)} 04:00 PM ET",
        "previousTradeDate": prev.strftime(fmt), "nextTradeDate": following.strftime(fmt),
        "isBusinessDay": today in US_INDEX, "mrktStatus": "Closed",
    })


SPLIT_EVENTS = [  # 成分裡的兩個＋不在成分裡的（只當雜訊）＋還沒生效的 ETF 分割
    {"symbol": SPLIT_NOW, "name": "Example Reverse Split Co", "ratio": "1:10", "executionDate": _mdy(SPLIT_NOW_DAY)},
    {"symbol": SPLIT_OLD, "name": "Example Forward Split Co", "ratio": "4 : 1", "executionDate": _mdy(SPLIT_OLD_DAY)},
    {"symbol": "KNDI", "name": "Kandi Technologies Group, Inc.", "ratio": "1:10", "executionDate": "10/16/2026"},
    {"symbol": "BTOC", "name": "Armlogi Holding Corp.", "ratio": "1:16", "executionDate": "10/15/2026"},
    {"symbol": "IBO", "name": "Example Odd Ratio Co", "ratio": "1 : 12.62", "executionDate": "09/23/2026"},
    {"symbol": "MTUM", "name": "iShares MSCI USA Momentum Factor ETF", "ratio": "4 : 1", "executionDate": "11/05/2026"},
    {"symbol": "ITA", "name": "iShares U.S. Aerospace & Defense ETF", "ratio": "3 : 1", "executionDate": "11/05/2026"},
]


def nasdaq_splits(since: date, now: float) -> dict:
    rows = [row for row in SPLIT_EVENTS if datetime.strptime(row["executionDate"], "%m/%d/%Y").date() >= since]
    headers = {"symbol": "SYMBOL", "name": "COMPANY", "ratio": "RATIO", "executionDate": "EFFECTIVE DATE"}
    return _nasdaq({"asOf": et(now).strftime("%a, %b %d, %Y"), "headers": headers, "rows": rows})


def nasdaq_ndx(now: float) -> dict:
    day = us_closed_through(now)
    rows = []
    for symbol in NDX_MEMBERS:
        close = us_close(symbol, day, now)
        cap = int(5e9 + 4.5e12 * _u("cap", symbol) ** 3)
        rows.append({"symbol": symbol, "sector": "", "companyName": f"{us_name(symbol)} Common Stock",
                     "marketCap": f"{cap:,}", "lastSalePrice": f"${close:.2f}", "netChange": "$0.10",
                     "percentageChange": "+0.10%", "deltaIndicator": "up"})
    headers = {"symbol": "Symbol", "companyName": "Name", "marketCap": "Market Cap", "lastSalePrice": "Last Sale"}
    return _nasdaq({"totalrecords": len(rows), "limit": 101, "offset": 0,
                    "date": et(now).strftime("%b %d, %Y %I:%M %p"),
                    "data": {"asOf": None, "headers": headers, "rows": rows}})


def nasdaq_13f(symbol: str) -> dict:
    up = 900 + int(3000 * _u("13f+", symbol))
    down = 700 + int(3000 * _u("13f-", symbol))
    return _nasdaq({
        "ownershipSummary": {"SharesOutstandingPCT": {"label": "Institutional Ownership",
                                                      "value": f"{40 + 45 * _u('own', symbol):.2f}%"}},
        "activePositions": {"rows": [
            {"positions": "Increased Positions", "holders": f"{up:,}", "shares": "653,445,644"},
            {"positions": "Decreased Positions", "holders": f"{down:,}", "shares": "419,330,425"},
            {"positions": "Held Positions", "holders": "348", "shares": "18,093,073,185"},
            {"positions": "Total Institutional Shares", "holders": f"{up + down + 348:,}", "shares": "19,165,849,254"},
        ]},
        "newSoldOutPositions": {"rows": [
            {"positions": "New Positions", "holders": f"{100 + int(300 * _u('new', symbol))}", "shares": "1"},
            {"positions": "Sold Out Positions", "holders": f"{50 + int(150 * _u('sold', symbol))}", "shares": "1"},
        ]},
        "holdingsTransactions": {"table": {"rows": [
            {"ownerName": "Example Index Fund Manager", "date": "6/30/2026", "marketValue": "$492,391,608"},
            {"ownerName": "Example Asset Management", "date": "6/30/2026", "marketValue": "$421,842,931"},
            {"ownerName": "Example Pension Plan", "date": "3/31/2026", "marketValue": "$222,889,178"},
        ]}},
    })


def nasdaq_financials(symbol: str) -> dict:
    if symbol == "TSM":  # 實測：ADR 回 data: null
        return _nasdaq(None)
    headers = {"value1": "Quarterly Ending:", "value2": "6/30/2026", "value3": "3/31/2026", "value4": "12/31/2025",
               "value5": "9/30/2025"}
    base = 1_000_000 + int(20_000_000 * _u("ni", symbol))
    values = {f"value{i}": f"${int(base * (1.0 + 0.05 * (5 - i))):,}" for i in range(2, 6)}
    row = {"value1": "Net Income Applicable to Common Shareholders", **values}
    revenue = {"value1": "Total Revenue", **{key: "$1" for key in values}}
    return _nasdaq({"symbol": symbol, "incomeStatementTable": {"asOf": None, "headers": headers,
                                                               "rows": [revenue, row]}})


def nasdaq_summary(symbol: str) -> dict:
    cap = int(2e11 + 8e11 * _u("cap2", symbol))
    return _nasdaq({"symbol": symbol.replace(".", "/"), "summaryData": {
        "Exchange": {"label": "Exchange", "value": "NYSE"},
        "MarketCap": {"label": "Market Cap", "value": f"{cap:,}"},
    }, "assetClass": "STOCKS"})


ECON_EVENTS = {  # 美東的事件日 → [(美東時刻, 名稱, 國家)]
    date(2026, 10, 20): [("08:30", "Initial Jobless Claims", "United States")],
    date(2026, 10, 21): [("07:00", "MBA Mortgage Applications", "United States")],  # 週三才有（A 會核對）
    date(2026, 10, 22): [("08:30", "CPI", "United States"), ("08:30", "Core CPI", "United States"),
                         ("03:00", "Core CPI", "Spain")],
    date(2026, 10, 23): [("10:00", "Michigan Consumer Sentiment", "United States"),
                         ("All Day", "GDP", "United States")],
}


def nasdaq_econ(requested: date) -> dict:
    """實測（主 Claude 10/18）：`?date=X` 回的是 X 前一天的事件，所以依「事件日＝X−1」回。"""
    day = requested - timedelta(days=1)
    rows = [{"gmt": clock, "country": country, "eventName": name, "actual": "&nbsp;", "consensus": " ",
             "previous": "&nbsp;", "description": ""} for clock, name, country in ECON_EVENTS.get(day, [])]
    return _nasdaq({"asOf": requested.strftime("%a, %b %d, %Y"), "headers": {"gmt": "Time"}, "rows": rows or None})


# 前十大的財報日（照一般的財報季分布：FOMC 10/28 之前只有少數幾家）；其他代號落在 11 月
EARNINGS_DAYS = {
    "NFLX": date(2026, 10, 20), "TSLA": date(2026, 10, 21), "TXN": date(2026, 10, 21), "INTC": date(2026, 10, 22),
    "META": date(2026, 10, 28), "MSFT": date(2026, 10, 28), "GOOGL": date(2026, 10, 28), "AAPL": date(2026, 10, 29),
    "AMZN": date(2026, 10, 29), "BRK.B": date(2026, 11, 2), "AMD": date(2026, 11, 3), "QCOM": date(2026, 11, 4),
    "AMAT": date(2026, 11, 12), "NVDA": date(2026, 11, 18), "ADI": date(2026, 11, 24), "AVGO": date(2026, 12, 10),
    "COST": date(2026, 12, 10), "MU": date(2026, 12, 17), "JPM": date(2027, 1, 13), "TSM": date(2027, 1, 14),
}


def nasdaq_earnings(symbol: str) -> dict:
    day = EARNINGS_DAYS.get(symbol) or date(2026, 11, 5) + timedelta(days=int(20 * _u("earn", symbol)))
    return _nasdaq({"reportText": f"{us_name(symbol)} is estimated to report earnings on  {_mdy(day)}. The upcoming "
                                  "earnings date is derived from an algorithm based on historical reporting dates.",
                    "heading": f"{symbol} Earnings Date",
                    "announcement": f"Earnings announcement* for {symbol}: {day.strftime('%b %d, %Y')}"})


def ssga_xlsx(now: float) -> bytes:
    """SSGA 在 D 晚上掛的是 D 前一個交易日的持股（實測 10/16 晚上是 As of 15-Oct）。"""
    as_of = us_prev(us_closed_through(now))
    header = ["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"]
    rows = [[us_name(symbol), symbol, cusip(f"{_h('cusip', symbol) % 10**8:08d}"), "-", spy_weight(symbol), "-",
             float(1e6 + _h("shares", symbol) % 10**8), "USD"] for symbol in SPY_MEMBERS]
    rows += [["US DOLLAR", "-", "999USDZ92", "-", 0.286774, "-", 2347837191.76, "USD"],
             ["TPG INC", "2602335D", "436CVR021", "-", 3e-06, "-", 2578626.0, "USD"]]
    sheet = [["Fund Name:", "SPDR S&P 500 ETF Trust"], ["Ticker Symbol:", "SPY"],
             ["Holdings:", f"As of {as_of.strftime('%d-%b-%Y')}"], [], header, *rows, [], [],
             ["Past performance is not a reliable indicator of future performance."]]
    return _xlsx("holdings", sheet)


def _mdj_link(code: str, name: str, back: str) -> str:
    return f"<a href='/etf/ea/ETZCW.djhtm?c=1&etfid={code}&back={back}'>{name}({code})</a>"


def moneydj_html(etfid: str, now: float) -> str:
    if etfid == "SOXX":
        label, title, as_of = "SOXX", "iShares 半導體 ETF", "2026/09/30"
        rows = [(_mdj_link(f"{sym}.US", us_name(sym).title(), "SOXX"), f"{9.5 - i * 0.35:.2f}")
                for i, sym in enumerate(SOXX_LINKED)]
        rows += [(name, f"{weight:.2f}") for name, _, weight in SOXX_UNLINKED]
        rows += [("BLK CSH FND TREASURY SL AGENCY", "0.12"), ("USD CASH", "0.03")]
    elif etfid == "QQQ":
        label, title, as_of = "QQQ", "Invesco QQQ", "2026/09/30"
        rows = [(_mdj_link(f"{sym}.US", us_name(sym).title(), "QQQ"), f"{ndx_weight(sym):.2f}") for sym in NDX_MEMBERS]
        rows += [("E-MINI NASDAQ 100 DEC26", "0.20"), ("USD CASH", "0.03")]
    elif etfid == "0050.TW":
        label, title = "0050.TW", "元大台灣50"
        as_of = tw_published_through(now, (18, 0)).strftime("%Y/%m/%d")
        rest = (99.7 - sum(TW_TOP_WEIGHT.values())) / (len(TW_MEMBERS) - len(TW_TOP_WEIGHT))
        rows = [(_mdj_link(f"{code}.TW", tw_name(code), "0050.TW"), f"{TW_TOP_WEIGHT.get(code, rest):.2f}")
                for code in TW_MEMBERS]
        rows += [(_mdj_link("FITXN*1.TF", "臺股期貨 202610", "0050.TW"), "0.21"), ("台灣50ETF股票期貨 202610", "0.03")]
    else:
        raise KeyError(etfid)
    body = "".join(
        f'<tr class="even"><td class="col05">{name}</td><td class="col06">{weight}</td><td class="col07">1,000</td></tr>'
        for name, weight in rows
    )
    return ('<html><head><meta charset="UTF-8"></head><body><table><tr><td>layout</td></tr></table>'
            f'<div class="eTitle">{title}({label})-全部持股'
            f'<div id="x_sdate3" align="right">資料日期：{as_of}</div></div>'
            '<table class="datalist" id="x_stable3"><thead><tr><th scope="col">個股名稱</th>'
            '<th scope="col">投資比例(%)</th><th scope="col">持有股數 / 口數</th></tr></thead>'
            f"<tbody>{body}</tbody><tfoot></tfoot></table></body></html>")


def _cboe_data_day(now: float) -> date:
    """延遲報價：收盤＋15 分之後到下一個交易日開盤，報價停在那天的收盤。"""
    return us_closed_through(now, after=(16, 15))


def _cboe_stamp(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d %H:%M:%S")


EXPIRIES = (date(2026, 10, 23), date(2026, 10, 30), date(2026, 11, 20), date(2026, 12, 18))


def cboe_options(symbol: str, now: float) -> dict:
    """每個到期日 ±15% 現價、1% 一格的買權與賣權；買權 OI 集中在 +5%、賣權在 −5%（牆、月選、GEX 都算得出來）。"""
    day = _cboe_data_day(now)
    spot = us_close(symbol, day, now)
    options = []
    for expiry in EXPIRIES:
        days = (expiry - day).days
        for step in range(-15, 16):
            strike = round(spot * (1 + step / 100.0))
            for kind in ("C", "P"):
                center = 5 if kind == "C" else -5
                oi = int(400 + 9000 * math.exp(-((step - center) / 4.0) ** 2) + 50 * _u(symbol, expiry, strike, kind))
                itm = (kind == "C" and strike < spot) or (kind == "P" and strike > spot)
                iv = 0.0 if itm and step in (-15, 15) else round(0.18 + 0.002 * abs(step) + 0.01 * (days < 10), 4)
                options.append({
                    "option": f"{symbol}{expiry.strftime('%y%m%d')}{kind}{int(strike * 1000):08d}",
                    "bid": 1.0, "ask": 1.1, "iv": iv, "open_interest": float(oi),
                    "volume": float(int(oi * 0.1)), "delta": 0.5 if kind == "C" else -0.5,
                    "gamma": round(0.02 * math.exp(-(step / 6.0) ** 2), 6), "vega": 0.1, "theta": -0.1, "rho": 0.0,
                    "last_trade_time": f"{day.isoformat()}T15:59:00",
                })
    return {"timestamp": _cboe_stamp(now), "symbol": symbol,
            "data": {"symbol": symbol, "security_type": "stock", "current_price": spot, "close": spot,
                     "prev_day_close": us_close(symbol, us_prev(day), now), "volume": us_volume(symbol, day),
                     "iv30": 20.8, "last_trade_time": f"{day.isoformat()}T16:14:57", "options": options}}


def vix_close(symbol: str, day: date) -> float:
    t = US_INDEX[day]
    base = 18.0 if symbol == "VIX" else 20.5
    return round(base + 2.5 * math.sin(2 * math.pi * t / 23.0), 2)


def cboe_quote(symbol: str, now: float) -> dict:
    day = _cboe_data_day(now)
    close, prev = vix_close(symbol, day), vix_close(symbol, us_prev(day))
    change = round(close - prev, 2)
    return {"timestamp": _cboe_stamp(now), "symbol": f"_{symbol}",
            "data": {"symbol": f"^{symbol}", "security_type": "index", "current_price": close, "close": close,
                     "price_change": change, "price_change_percent": round(change / prev * 100, 4),
                     "prev_day_close": close, "last_trade_time": f"{day.isoformat()}T16:15:01"}}


TREASURY_HEADER = 'Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"'


def treasury_csv(ym: str, now: float) -> str:
    year, month = int(ym[:4]), int(ym[4:])
    through = us_closed_through(now, after=(17, 0))
    lines = [TREASURY_HEADER]
    for day in reversed(US_SESSIONS):
        if (day.year, day.month) != (year, month) or day > through or day in US_BOND_HOLIDAYS:
            continue
        t = US_INDEX[day]
        m3, y10 = 4.20 + 0.05 * math.sin(t / 9.0), 5.05 + 0.08 * math.sin(t / 13.0)
        values = [m3 + 0.02, m3 + 0.01, m3, m3, m3 - 0.01, m3 - 0.03, m3 - 0.05, 4.0, 4.1, 4.3, 4.6, y10, 5.3, 5.25]
        lines.append(_mdy(day) + "," + ",".join(f"{value:.2f}" for value in values))
    return "\n".join(lines) + "\n"


# CFTC TFF（無表頭、87 欄）：真實公開的四列，報告日依時間換成「已公布的最近一個週二」
CFTC_ROWS = (
    '"S&P 500 Consolidated - CHICAGO MERCANTILE EXCHANGE",261013,2026-10-13,13874+,CME ,00,138 , 1948056,  201288,  804452,   49131, 1112656,  194559,   96050,  121198,  531716,   53147,   62418,   73606,     302, 1696190, 1802963,  251866,  145094,   -1721,    -142,    1122,    6830,    3253,   -5543,   -3331,    6539,    1147,    -722,  -10369,    -982,     -28,    2029,   -1508,   -3750,    -213,  100.0,   10.3,   41.3,    2.5,   57.1,   10.0,    4.9,    6.2,   27.3,    2.7,    3.2,    3.8,    0.0,   87.1,   92.6,   12.9,    7.4,    453,     40,     32,     15,    165,     42,     79,     66,     79,     58,     17,     12,.,    345,    263,     9.6,    27.9,    16.8,    39.5,     9.2,    26.5,    15.4,    37.9,"(S&P 500 INDEX X $50.00)","13874+","CME ","138 ","F20","FutOnly"',
    '"E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",261013,2026-10-13,13874A,CME ,00,138 , 1931607,  200960,  803318,   49131, 1110384,  194549,   96043,  121404,  530130,   46956,   62418,   73497,     302, 1687598, 1793926,  244009,  137681,    -620,    -205,    1312,    7002,    3667,   -5553,   -3280,    8181,    1820,   -1480,  -10369,    -982,     -28,    3488,   -1189,   -4108,     569,  100.0,   10.4,   41.6,    2.5,   57.5,   10.1,    5.0,    6.3,   27.4,    2.4,    3.2,    3.8,    0.0,   87.4,   92.9,   12.6,    7.1,    444,     39,     31,     15,    164,     41,     79,     62,     79,     55,     17,     11,.,    339,    257,     9.7,    28.1,    16.9,    39.8,     9.3,    26.7,    15.5,    38.2,"($50 X S&P 500 INDEX)","13874A","CME ","138 ","F20","FutOnly"',
    '"NASDAQ-100 Consolidated - CHICAGO MERCANTILE EXCHANGE",261013,2026-10-13,20974+,CME ,00,209 ,  294776,   49082,  122889,    7515,  113679,   37277,    5269,   43778,   65669,   14043,   10909,    8044,       5,  244280,  260711,   50496,   34065,   -2879,    -328,    4698,    1057,     858,    -688,     671,   -4361,   -9209,    2311,   -4592,     124,       5,   -4379,   -1031,    1500,   -1848,  100.0,   16.7,   41.7,    2.5,   38.6,   12.6,    1.8,   14.9,   22.3,    4.8,    3.7,    2.7,    0.0,   82.9,   88.4,   17.1,   11.6,    326,     34,     26,     14,     90,     28,     34,     53,     59,     30,     21,     15,.,    240,    165,    20.8,    31.3,    30.1,    44.0,    20.5,    29.2,    29.7,    41.7,"(NASDAQ 100 INDEX X $20)","20974+","CME ","209 ","F20","FutOnly"',
    '"NASDAQ MINI - CHICAGO MERCANTILE EXCHANGE",261013,2026-10-13,209742,CME ,00,209 ,  279515,   48575,  126507,    3012,  112604,   37281,    5238,   48106,   59699,    8647,   10809,    7911,       5,  236996,  248300,   42519,   31215,   -4071,    -256,    5114,     136,     877,    -648,     702,   -3243,  -10862,    1838,   -4618,     137,       5,   -4559,   -3578,     488,    -493,  100.0,   17.4,   45.3,    1.1,   40.3,   13.3,    1.9,   17.2,   21.4,    3.1,    3.9,    2.8,    0.0,   84.8,   88.8,   15.2,   11.2,    283,     31,     21,      9,     84,     27,     30,     45,     48,     22,     16,     11,.,    205,    140,    21.9,    31.4,    31.7,    44.7,    21.6,    30.8,    31.4,    43.4,"(NASDAQ 100 STOCK INDEX X $20)","209742","CME ","209 ","F20","FutOnly"',
)


def cftc_text(now: float) -> str:
    moment = et(now)
    tuesday = moment.date() - timedelta(days=(moment.weekday() - 1) % 7)
    release = datetime.combine(tuesday + timedelta(days=3), datetime.min.time(), ET) + timedelta(hours=15, minutes=30)
    report = tuesday if moment >= release else tuesday - timedelta(days=7)
    stamp = f"{report.strftime('%y%m%d')},{report.isoformat()}"
    return "\n".join(row.replace("261013,2026-10-13", stamp) for row in CFTC_ROWS) + "\n"


def finra_xlsx() -> bytes:
    header = ["Year-Month", "Debit Balances in Customers' Securities Margin Accounts",
              "Free Credit Balances in Customers' Cash Accounts",
              "Free Credit Balances in Customers' Securities Margin Accounts"]
    rows = []
    month = date(2026, 9, 1)
    for i in range(25):
        rows.append([month.strftime("%Y-%m"), 1480054 - 11000 * i, 215328, 218581])
        month = (month - timedelta(days=1)).replace(day=1)
    return _xlsx("Customer Margin Balances", [header, *rows])


def fomc_html() -> str:
    def panel(year: int, meetings: list[tuple[str, str]]) -> str:
        items = "".join(
            f'<div class="row fomc-meeting" "><div class="fomc-meeting__month col-xs-5"><strong>{month}</strong></div>'
            f'<div class="fomc-meeting__date col-xs-4">{days}</div><div class="col-xs-12"><a href="/x">PDF</a></div></div>'
            for month, days in meetings
        )
        return (f'<div class="panel panel-default"><div class="panel-heading"><h4><a id="{year}">{year} FOMC Meetings'
                f"</a></h4></div>{items}</div>")

    return ("\ufeff<html><body><h5>FOMC Search</h5>"
            + panel(2026, [("January", "27-28"), ("March", "17-18*"), ("April", "28-29"), ("June", "16-17*"),
                           ("July", "28-29"), ("September", "15-16*"), ("October", "27-28"), ("December", "8-9*")])
            + panel(2027, [("January", "26-27"), ("March", "16-17*")])
            + "</body></html>")


# ── 宏觀、新聞、川普、預測市場 ──────────────────────────────────────────────────
YAHOO_ZONES = {"TWD=X": "Europe/London", "BTC-USD": "UTC"}
YAHOO_BASE = {"DX-Y.NYB": 98.6, "CL=F": 74.1, "BZ=F": 78.0, "GC=F": 4010.0, "SI=F": 48.3, "BTC-USD": 81471.0,
              "TWD=X": 32.34, "^RUT": 2460.0, "^DJI": 46200.0, "^IXIC": 22600.0, "^MOVE": 88.0}
YAHOO_RATE_LIMITED_FROM = datetime(2026, 10, 17, 0, 0, tzinfo=TPE).timestamp()  # 第二天 Yahoo 回 429


def yahoo_spark(symbols: list[str], now: float) -> dict:
    through = us_closed_through(now)
    results = []
    for symbol in symbols:
        zone = ZoneInfo(YAHOO_ZONES.get(symbol, "America/New_York"))
        if symbol == "BTC-USD":
            days = [through - timedelta(days=i) for i in range(21, -1, -1)]
        else:
            days = [day for day in US_SESSIONS if through - timedelta(days=31) < day <= through]
        stamps = [int(datetime(d.year, d.month, d.day, 9, 30, tzinfo=zone).timestamp()) for d in days]
        closes = [round(YAHOO_BASE[symbol] * (1 + 0.01 * math.sin(i / 3.0 + _u("y", symbol))), 4) for i, _ in enumerate(days)]
        meta = {"symbol": symbol, "currency": "USD", "exchangeTimezoneName": str(zone)}
        results.append({"symbol": symbol, "response": [{"meta": meta, "timestamp": stamps,
                                                        "indicators": {"quote": [{"close": closes}]}}]})
    return {"spark": {"result": results, "error": None}}


NEWS = {  # 主題 → [(標題, 來源, 幾小時前)]；有幾則刻意帶網址、@、雙向控制字元或太舊
    "美股": [
        ("美股收高 科技股領漲", "經濟日報", 2.0), ("聯準會官員談利率路徑", "中央社", 5.0),
        ("詳見 https://example.com 的盤後整理", "鉅亨網", 6.0),
        ("x @all 盤後焦點", "鉅亨網", 7.0),
        ("費半指數\u202e大漲", "工商時報", 8.0),
        ("三天前的舊聞", "經濟日報", 50.0),
    ],
    "台股": [("台股站穩月線 電子權值股撐盤", "中央社", 3.0), ("外資連三日調節", "經濟日報", 9.0),
             ("零寬\u200b標題測試", "鉅亨網", 10.0)],
    "伊朗": [("伊朗局勢 油價波動加大", "中央社", 4.0), ("國際能源署更新油市報告", "經濟日報", 20.0)],
}


def gnews_rss(query: str, now: float) -> str:
    moment = datetime.fromtimestamp(now, UTC)
    items = "".join(
        f"<item><title>{title} - {source}</title><link>https://news.google.com/rss/articles/{_h(query, i) % 10**9}</link>"
        f'<guid isPermaLink="false">g{_h(query, i) % 10**12}</guid><pubDate>{_rfc822(moment - timedelta(hours=hours))}</pubDate>'
        f'<description>d</description><source url="https://example.com">{source}</source></item>'
        for i, (title, source, hours) in enumerate(NEWS[query])
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{query}</title>{items}</channel></rss>'


TRUMP_NS = ('xmlns:truth="https://truthsocial.com/ns" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:archive="https://www.trumpstruth.org/rss-extensions"')
TRUMP_POSTS = [  # (originalId, 幾小時前, 內文 HTML, 移除, postType)
    ("115300000000000001", 3.0, "<p>We will put new tariffs on imported chips, and Iran must stop now.</p>", False,
     "original"),
    ("115300000000000002", 6.0, "<p>The Federal Reserve should cut interest rates.</p>", False, "original"),
    ("115300000000000003", 9.0, "<p>Tariffs are working.</p>", True, "original"),
    ("115300000000000004", 12.0, '<p><a href="https://truthsocial.com/x">View image</a></p>', False, "original"),
    ("115300000000000005", 20.0, "<p>Great deal on oil prices.</p><blockquote>Quoted Example Author: Iran talks"
                                 "</blockquote>", False, "original"),
    ("115300000000000006", 60.0, "<p>Old post about Taiwan semiconductors.</p>", False, "original"),
]


def trump_rss(now: float) -> str:
    moment = datetime.fromtimestamp(now, UTC)
    items = "".join(
        "<item><title>Post</title><link>https://www.trumpstruth.org/statuses/1</link>"
        f"<description><![CDATA[{text}]]></description><dc:creator>Donald J. Trump</dc:creator>"
        f"<pubDate>{_rfc822(moment - timedelta(hours=hours))}</pubDate>"
        f"<truth:originalUrl>https://truthsocial.com/@realDonaldTrump/{oid}</truth:originalUrl>"
        f"<truth:originalId>{oid}</truth:originalId><archive:postType>{kind}</archive:postType>"
        f"<archive:removed>{'true' if removed else 'false'}</archive:removed></item>"
        for oid, hours, text, removed, kind in TRUMP_POSTS
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" {TRUMP_NS}><channel><title>t</title>{items}</channel></rss>'


# ── 台股來源 ──────────────────────────────────────────────────────────────────
TW_NO_DATA = {"stat": "很抱歉，沒有符合條件的資料!"}
_UP, _DOWN, _FLAT = "<p style= color:red>+</p>", "<p style= color:green>-</p>", "<p> </p>"
CLOSE_FIELDS = ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價",
                "漲跌(+/-)", "漲跌價差", "最後揭示買價", "最後揭示買量", "最後揭示賣價", "最後揭示賣量", "本益比"]
TWT49U_FIELDS = ["資料日期", "股票代號", "股票名稱", "除權息前收盤價", "除權息參考價", "權值+息值", "權/息", "漲停價格",
                 "跌停價格", "開盤競價基準", "減除股利參考價", "詳細資料", "最近一次申報資料 季別/日期",
                 "最近一次申報每股 (單位)淨值", "最近一次申報每股 (單位)盈餘"]


def _sign_cells(close: float, prev: Optional[float]) -> tuple[str, str]:
    if prev is None or close == prev:
        return _FLAT, "0.00"
    return (_UP if close > prev else _DOWN), f"{abs(close - prev):.2f}"


def twse_allbut(day: date, now: float) -> dict:
    if day not in TW_INDEX or day > tw_published_through(now, (14, 0)):
        return TW_NO_DATA
    prev_day = tw_prev(day)
    closes = []
    for code in ["0050", *TW_MEMBERS, *TW_OTHERS, TW_NO_TRADE]:
        if code == TW_NO_TRADE:
            closes.append([code, tw_name(code), "0", "0", "0", "--", "--", "--", "--", _FLAT, "0.00", "--", "0",
                           "--", "0", "0.00"])
            continue
        close = tw_close(code, day)
        sign, diff = _sign_cells(close, tw_close(code, prev_day) if prev_day else None)
        text = _comma(close)
        closes.append([code, tw_name(code), f"{tw_volume(code, day):,}", "1,000", "1,000,000", text, text, text, text,
                       sign, diff, text, "1", text, "1", "0.00"])
    taiex = tw_close("TAIEX", day)
    taiex_prev = tw_close("TAIEX", prev_day) if prev_day else taiex
    index_sign = "<p style ='color:red'>+</p>" if taiex >= taiex_prev else "<p style ='color:green'>-</p>"
    adv = 600 + int(200 * _u("adv", day))
    return {
        "stat": "OK", "date": _ymd(day), "type": "ALLBUT0999",
        "tables": [
            {"title": f"{_roc(day)} 價格指數(臺灣證券交易所)",
             "fields": ["指數", "收盤指數", "漲跌(+/-)", "漲跌點數", "漲跌百分比(%)", "特殊處理註記"],
             "data": [["寶島股價指數", "52,176.05", index_sign, "175.67", "0.34", ""],
                      ["發行量加權股價指數", _comma(taiex), index_sign, f"{abs(taiex - taiex_prev):.2f}",
                       f"{(taiex / taiex_prev - 1) * 100:.2f}", ""]]},
            {"title": f"{_roc(day)} 大盤統計資訊", "fields": ["成交統計", "成交金額(元)", "成交股數(股)", "成交筆數"],
             "data": [["1.一般股票", "909,178,480,941", "4,589,501,506", "3,796,071"],
                      ["總計(1~15)", f"{tw_turnover(day):,}", "11,820,630,612", "4,986,590"]]},
            {"title": "漲跌證券數合計", "fields": ["類型", "整體市場", "股票"],
             "data": [["上漲(漲停)", "9,016(68)", f"{adv}(19)"], ["下跌(跌停)", "6,541(84)", f"{1000 - adv}(0)"],
                      ["持平", "1,190", "106"], ["未成交", "17,678", "5"], ["無比價", "3,528", "1"]]},
            {"title": f"{_roc(day)} 每日收盤行情(全部(不含權證、牛熊證、可展延牛熊證))", "fields": CLOSE_FIELDS,
             "data": closes},
            {},
        ],
    }


def twse_bfi82u(day: date, now: float) -> dict:
    if day not in TW_INDEX or day > tw_published_through(now, (15, 0)):
        return TW_NO_DATA
    rows, total_buy, total_sell = [], 0, 0
    for label in ("自營商(自行買賣)", "自營商(避險)", "投信", "外資及陸資(不含外資自營商)"):
        buy = 10_000_000_000 + _h("buy", label, day) % 300_000_000_000
        sell = 10_000_000_000 + _h("sell", label, day) % 300_000_000_000
        rows.append([label, f"{buy:,}", f"{sell:,}", f"{buy - sell:,}"])
        total_buy, total_sell = total_buy + buy, total_sell + sell
    rows.append(["外資自營商", "0", "0", "0"])
    rows.append(["合計", f"{total_buy:,}", f"{total_sell:,}", f"{total_buy - total_sell:,}"])
    return {"stat": "OK", "date": _ymd(day), "title": f"{_roc(day)} 三大法人買賣金額統計表",
            "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"], "data": rows, "hints": "單位：元"}


def twse_margin(day: date, now: float) -> dict:
    if day not in TW_INDEX or day > tw_published_through(now, (21, 0)):
        return TW_NO_DATA
    today_k = 645_000_000 + _h("margin", day) % 5_000_000
    prev = tw_prev(day)
    prev_k = 645_000_000 + _h("margin", prev) % 5_000_000
    return {"stat": "OK", "date": _ymd(day), "tables": [{
        "title": f"{_roc(day)} 信用交易統計", "fields": ["項目", "買進", "賣出", "現金(券)償還", "前日餘額", "今日餘額"],
        "data": [["融資(交易單位)", "343,600", "318,542", "7,624", "9,521,230", "9,538,664"],
                 ["融券(交易單位)", "20,982", "30,004", "4,570", "229,150", "233,602"],
                 ["融資金額(仟元)", "28,656,955", "28,942,556", "536,222", f"{prev_k:,}", f"{today_k:,}"]],
    }, {}]}


def twse_twt49u(start: date, end: date) -> dict:
    rows = []
    for ex_day, code, factor in TW_EXRIGHTS:
        if start <= ex_day <= end:
            pre = tw_close(code, tw_prev(ex_day))
            ref = round(pre * factor, 2)
            rows.append([_roc(ex_day), code, tw_name(code), f"{pre:.2f}", f"{ref:.2f}", f"{pre - ref:.6f}", "息",
                         "1", "1", f"{ref:.2f}", f"{ref:.2f}", f"{code},x", "", "", ""])
    if not rows:
        return TW_NO_DATA
    return {"stat": "OK", "title": "除權除息計算結果表", "fields": TWT49U_FIELDS, "data": rows,
            "strDate": _ymd(start), "endDate": _ymd(end)}


def twse_holidays(year: int) -> dict:
    rows = TW_HOLIDAYS.get(year)
    if not rows:
        return TW_NO_DATA
    return {"stat": "ok", "date": f"{year}0101", "title": f"{year - 1911} 年市場開休市日期",
            "fields": ["日期", "名稱", "說明"], "data": rows, "queryYear": year, "total": len(rows)}


def openapi_bwibbu(now: float) -> list:
    day = tw_published_through(now, (17, 0))
    return [{"Date": _roc_compact(day), "Code": code, "Name": tw_name(code),
             "PEratio": "-" if i % 9 == 4 else f"{10 + 25 * _u('pe', code):.2f}", "DividendYield": "1.50", "PBratio": "2.10"}
            for i, code in enumerate(TW_MEMBERS + TW_OTHERS)]


def openapi_revenue() -> list:
    rows = []
    for i, code in enumerate(TW_MEMBERS):
        yoy = "" if i % 11 == 3 else f"{-20 + 60 * _u('rev', code):.2f}"
        rows.append({"資料年月": "11509", "公司代號": code, "公司名稱": tw_name(code), "營業收入-去年同月增減(%)": yoy})
        rows.append({"資料年月": "11508", "公司代號": code, "公司名稱": tw_name(code), "營業收入-去年同月增減(%)": "1.00"})
    return rows


def _taifex_day(now: float, lag: bool) -> date:
    """期交所 OpenAPI：期貨／買賣權法人／PCR 當天 16:00 後就有；選擇權行情與匯率晚一天（隔天 16:00 才有）。"""
    if not lag:
        return tw_published_through(now, (16, 0))
    moment = tpe(now)
    day = moment.date()
    while day not in TW_INDEX or moment < datetime.combine(day + timedelta(days=1), dtime(16, 0), TPE):
        day -= timedelta(days=1)
    return day


def taifex_futures(now: float) -> list:
    day = _taifex_day(now, lag=False)

    def row(contract: str, long_: int, short_: int) -> dict:
        return {"Date": _ymd(day), "ContractCode": contract, "Item": "外資及陸資", "TradingVolume(Net)": "-120",
                "OpenInterest(Long)": str(long_), "OpenInterest(Short)": str(short_),
                "OpenInterest(Net)": str(long_ - short_)}

    shift = int(2000 * _u("fut", day))
    return [row("臺股期貨", 9171 + shift, 106753), row("小型臺指期貨", 1000, 4928), row("微型臺指期貨", 500, 12915),
            {**row("臺股期貨", 10619, 3725), "Item": "自營商"}]


def taifex_calls_puts(now: float) -> list:
    day = _taifex_day(now, lag=False)

    def opt(side: str, long_: int, short_: int) -> dict:
        return {"Date": _ymd(day), "ContractCode": "臺指選擇權", "CallPut": side, "Item": "外資及陸資",
                "OpenInterest(Long)": str(long_), "OpenInterest(Short)": str(short_),
                "OpenInterest(Net)": str(long_ - short_), "ContractValueofOpenInterest(Net)(Thousands)": "-1000"}

    return [opt("CALL", 18797, 18966), opt("PUT", 15000, 40000)]


def taifex_pcr(now: float) -> bytes:
    through = _taifex_day(now, lag=False)
    days = [day for day in TW_SESSIONS if day <= through][-10:]
    lines = ["\ufeff日期,賣權成交量,買權成交量,買賣權成交量比率%,賣權未平倉量,買權未平倉量,買賣權未平倉量比率%"]
    for day in reversed(days):
        put_oi, call_oi = 70000 + _h("pcr", day) % 15000, 127036
        lines.append(f"{_ymd(day)},191704,255629,74.99,{put_oi},{call_oi},{put_oi / call_oi * 100:.2f}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def taifex_txo(now: float) -> list:
    day = _taifex_day(now, lag=True)
    spot = tw_close("TAIEX", day)
    rows = []
    for month, scale in (("202610", 1.0), ("202611", 0.4), ("202610W4", 0.2)):
        for step in range(-15, 16):
            strike = int(round(spot * (1 + step / 100.0) / 100.0) * 100)
            for side, kind in (("買權", "C"), ("賣權", "P")):
                center = 5 if kind == "C" else -5
                oi = int(scale * (300 + 9000 * math.exp(-((step - center) / 3.5) ** 2)))
                rows.append({"Date": _ymd(day), "Contract": "TXO", "ContractMonth(Week)": month,
                             "StrikePrice": str(strike), "CallPut": side, "OpenInterest": str(oi),
                             "SettlementPrice": "100", "TradingSession": "一般"})
                rows.append({"Date": _ymd(day), "Contract": "TXO", "ContractMonth(Week)": month,
                             "StrikePrice": str(strike), "CallPut": side, "OpenInterest": "-",
                             "SettlementPrice": "-", "TradingSession": "盤後"})
    return rows


def taifex_fx(now: float) -> list:
    through = _taifex_day(now, lag=True)
    days = [day for day in TW_SESSIONS if day <= through][-5:]
    return [{"Date": _ymd(day), "USD/NTD": f"{32.30 + 0.01 * i:.3f}", "EUR/USD": "1.1589"} for i, day in enumerate(days)]


def finmind_prices(code: str, start: date, end: date, now: float) -> dict:
    through = min(end, tw_published_through(now, (14, 30)))
    rows = []
    for day in TW_SESSIONS:
        if not start <= day <= through:
            continue
        close = tw_close(code, day)
        if code == "TAIEX":
            volume, money = 11_820_630_612, tw_turnover(day)
        else:
            volume = tw_volume(code, day)
            money = int(volume * close)
        rows.append({"date": day.isoformat(), "stock_id": code, "Trading_Volume": volume, "Trading_money": money,
                     "open": close, "max": close, "min": close, "close": close, "spread": 0.0, "Trading_turnover": 1})
    return {"msg": "success", "status": 200, "data": rows}



# ── 假的 http：照 market_http.fetch 的樣子 ──────────────────────────────────────
CONTENT_TYPES = {  # 主 Claude 10/17 實測的型別
    "api.nasdaq.com": "application/json; charset=utf-8",
    "www.ssga.com": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "www.moneydj.com": "text/html; charset=utf-8",
    "cdn-api.cboe.com": "application/json",
    "home.treasury.gov": "text/csv; charset=utf-8",
    "www.cftc.gov": "text/plain",
    "www.finra.org": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "www.federalreserve.gov": "text/html; charset=utf-8",
    "query1.finance.yahoo.com": "application/json;charset=utf-8",
    "www.twse.com.tw": "application/json;charset=UTF-8",
    "openapi.twse.com.tw": "application/json; charset=utf-8",
    "openapi.taifex.com.tw": "application/octet-stream",
    "api.finmindtrade.com": "application/json",
    "news.google.com": "application/xml; charset=utf-8",
    "www.trumpstruth.org": "application/rss+xml; charset=UTF-8",
}


class SimClock:
    """一個假時鐘給全部人用：monotonic（框架的預算）與牆上時間（Store、帳本、來源的「現在」）一起走。"""

    def __init__(self, wall: float) -> None:
        self.mono = 1000.0
        self._wall0 = float(wall)
        self._mono0 = self.mono

    def monotonic(self) -> float:
        return self.mono

    def wall(self) -> float:
        return self._wall0 + (self.mono - self._mono0)

    def sleep(self, seconds: float) -> None:
        self.mono += max(0.0, float(seconds))

    def start_round(self, wall: float) -> None:
        if wall < self.wall() - 1e-6:
            raise AssertionError("a round started before the previous one ended")
        self._wall0, self._mono0 = float(wall), self.mono


@dataclass
class Call:
    host: str
    path: str
    query: dict
    at: float  # 牆上時間


class FakeHttp:
    """依主機與路徑回合成的內容；先在帳本預扣（和真的 market_http 一樣），再量大小與 Content-Type。"""

    FetchError = market_http.FetchError

    def __init__(self, clock: SimClock) -> None:
        self.clock = clock
        self.ledger: Any = None  # 這一輪的 ctx.ledger
        self.calls: list[Call] = []
        self.unknown: list[str] = []
        self.no_ledger: list[str] = []
        self.too_big: list[str] = []

    def fetch(self, url, *, max_bytes, timeout=15.0, accept=("json",), headers=None, ledger=None):
        host = market_http.host_of(url)
        if host is None:
            self.unknown.append(url)
            raise market_http.FetchError("host")
        if ledger is None or ledger is not self.ledger:
            self.no_ledger.append(url)  # 繞過帳本的請求：預算與冷卻都失效
        if ledger is not None:
            ledger.reserve(host)
        parts = urlsplit(url)
        query = {key: values[0] for key, values in parse_qs(parts.query, keep_blank_values=True).items()}
        now = self.clock.wall()
        self.calls.append(Call(host, parts.path, query, now))
        self.clock.sleep(REQUEST_S)
        try:
            status, body = self._route(host, parts.path, query, now)
        except KeyError:
            status, body = None, None
        if status is None:
            self.unknown.append(url)
            raise market_http.FetchError("status")
        if status == 429:
            if ledger is not None:
                ledger.cooldown(host, market_http.COOLDOWN_SECONDS)
            raise market_http.FetchError("rate_limited")
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        if len(body) > max_bytes:
            self.too_big.append(f"{url} ({len(body)} > {max_bytes})")
            raise market_http.FetchError("too_big")
        content_type = CONTENT_TYPES[host]
        if accept and not any(token in content_type for token in accept):
            raise market_http.FetchError("content_type")
        return market_http.Response(200, body, {"content-type": content_type}, url)

    # 依主機分派；回 (status, body)；不認得回 (None, None)
    def _route(self, host: str, path: str, q: dict, now: float) -> tuple[Optional[int], Any]:
        handler = getattr(self, "_" + host.replace(".", "_").replace("-", "_"), None)
        return handler(path, q, now) if handler else (None, None)

    def _api_nasdaq_com(self, path: str, q: dict, now: float):
        parts = path.split("/")
        if path.startswith("/api/quote/") and path.endswith("/historical"):
            symbol = unquote(parts[3])
            return 200, nasdaq_historical(symbol, q["assetclass"] == "etf", date.fromisoformat(q["fromdate"]),
                                          date.fromisoformat(q["todate"]), now)
        if path == "/api/market-info":
            return 200, nasdaq_market_info(now)
        if path == "/api/calendar/splits":
            return 200, nasdaq_splits(date.fromisoformat(q["date"]), now)
        if path == "/api/quote/list-type/nasdaq100":
            return 200, nasdaq_ndx(now)
        if path.endswith("/institutional-holdings"):
            return 200, nasdaq_13f(unquote(parts[3]))
        if path.endswith("/financials"):
            return 200, nasdaq_financials(unquote(parts[3]))
        if path.startswith("/api/quote/") and path.endswith("/summary"):
            return 200, nasdaq_summary(unquote(parts[3]))
        if path == "/api/calendar/economicevents":
            return 200, nasdaq_econ(date.fromisoformat(q["date"]))
        if path.startswith("/api/analyst/") and path.endswith("/earnings-date"):
            return 200, nasdaq_earnings(unquote(parts[3]))
        return None, None

    def _www_ssga_com(self, path: str, q: dict, now: float):
        if path == "/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx":
            return 200, ssga_xlsx(now)
        return None, None

    def _www_moneydj_com(self, path: str, q: dict, now: float):
        if path == "/ETF/X/Basic/Basic0007B.xdjhtm":
            return 200, moneydj_html(q["etfid"], now)
        return None, None

    def _cdn_api_cboe_com(self, path: str, q: dict, now: float):
        name = path.rsplit("/", 1)[-1].removesuffix(".json")
        if path.startswith("/api/global/delayed_quotes/options/") and name in ("QQQ", "SPY", "SOXX"):
            return 200, cboe_options(name, now)
        if path.startswith("/api/global/delayed_quotes/quotes/_") and name.lstrip("_") in ("VIX", "VIX3M"):
            return 200, cboe_quote(name.lstrip("_"), now)
        return None, None

    def _home_treasury_gov(self, path: str, q: dict, now: float):
        if path.startswith("/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/all/"):
            return 200, treasury_csv(q["field_tdr_date_value_month"], now)
        return None, None

    def _www_cftc_gov(self, path: str, q: dict, now: float):
        return (200, cftc_text(now)) if path == "/dea/newcot/FinFutWk.txt" else (None, None)

    def _www_finra_org(self, path: str, q: dict, now: float):
        return (200, finra_xlsx()) if path == "/sites/default/files/2021-03/margin-statistics.xlsx" else (None, None)

    def _www_federalreserve_gov(self, path: str, q: dict, now: float):
        return (200, fomc_html()) if path == "/monetarypolicy/fomccalendars.htm" else (None, None)

    def _query1_finance_yahoo_com(self, path: str, q: dict, now: float):
        if path != "/v7/finance/spark":
            return None, None
        if now >= YAHOO_RATE_LIMITED_FROM:
            return 429, b"Too Many Requests"
        return 200, yahoo_spark(q["symbols"].split(","), now)

    def _news_google_com(self, path: str, q: dict, now: float):
        if path == "/rss/search" and q.get("q") in NEWS:
            return 200, gnews_rss(q["q"], now)
        return None, None

    def _www_trumpstruth_org(self, path: str, q: dict, now: float):
        return (200, trump_rss(now)) if path == "/feed" else (None, None)

    def _www_twse_com_tw(self, path: str, q: dict, now: float):
        if path == "/rwd/zh/afterTrading/MI_INDEX" and q.get("type") == "ALLBUT0999":
            return 200, twse_allbut(datetime.strptime(q["date"], "%Y%m%d").date(), now)
        if path == "/rwd/zh/fund/BFI82U" and q.get("type") == "day":
            return 200, twse_bfi82u(datetime.strptime(q["dayDate"], "%Y%m%d").date(), now)
        if path == "/rwd/zh/exRight/TWT49U":
            start, end = (datetime.strptime(q[key], "%Y%m%d").date() for key in ("startDate", "endDate"))
            return 200, twse_twt49u(start, end)
        if path == "/rwd/zh/marginTrading/MI_MARGN" and q.get("selectType") == "MS":
            return 200, twse_margin(datetime.strptime(q["date"], "%Y%m%d").date(), now)
        if path == "/rwd/zh/holidaySchedule/holidaySchedule":
            return 200, twse_holidays(int(q["date"][:4]))
        return None, None

    def _openapi_twse_com_tw(self, path: str, q: dict, now: float):
        if path == "/v1/exchangeReport/BWIBBU_ALL":
            return 200, openapi_bwibbu(now)
        if path == "/v1/opendata/t187ap05_L":
            return 200, openapi_revenue()
        return None, None

    def _openapi_taifex_com_tw(self, path: str, q: dict, now: float):
        routes: dict[str, Callable[[float], Any]] = {
            "/v1/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate": taifex_futures,
            "/v1/MarketDataOfMajorInstitutionalTradersDetailsOfCallsAndPutsBytheDate": taifex_calls_puts,
            "/v1/PutCallRatio": taifex_pcr,
            "/v1/DailyMarketReportOpt": taifex_txo,
            "/v1/DailyForeignExchangeRates": taifex_fx,
        }
        handler = routes.get(path)
        return (200, handler(now)) if handler else (None, None)

    def _api_finmindtrade_com(self, path: str, q: dict, now: float):
        if path == "/api/v4/data" and q.get("dataset") == "TaiwanStockPrice":
            return 200, finmind_prices(q["data_id"], date.fromisoformat(q["start_date"]),
                                       date.fromisoformat(q["end_date"]), now)
        return None, None


# ── 整個世界：兩段時間線的 collect＋publish（整個檔案共用一份，只跑一次） ──────────────────────
TASK_LIST = (*market_pipeline_us.TASKS, *market_pipeline_tw.tasks(), *market_pipeline_misc.tasks())
WATCHED_FILES = ("market_pipeline_us.py", "market_pipeline_tw.py", "market_pipeline_misc.py", "market_publish.py",
                 "market_dashboard.py", "market_calc.py", "market_sources_us.py", "market_sources_tw.py",
                 "market_sources_misc.py", "market_store.py", "market_tasks.py", "market_charts.py")


@dataclass
class RoundLog:
    phase: str
    index: int
    now: float
    results: dict[str, str]
    stopped: Optional[str]
    hosts: dict[str, int]
    calendar_problems: dict
    used_s: float  # 這一輪用掉的假時間（秒）
    real_s: float  # 這一輪真的跑了多久


@dataclass
class World:
    root: Path
    paths: market_paths.Paths
    clock: SimClock
    http: FakeHttp
    store: market_store.Store
    rounds: list[RoundLog] = field(default_factory=list)
    publishes: list[dict] = field(default_factory=list)  # 每段結束的 publish outcome 片段
    publish_s: list[float] = field(default_factory=list)  # 每次 publish 真的跑了多久
    real_fetch: list[str] = field(default_factory=list)  # 繞過 ctx.http、打到真的 market_http.fetch 的網址
    hashes: dict[str, str] = field(default_factory=dict)
    final_now: float = 0.0
    snapshot: dict = field(default_factory=dict)
    total_s: float = 0.0
    early_publish: dict = field(default_factory=dict)  # 第一輪之後的 publish（bootstrap 剛開始）
    early_now: float = 0.0
    early_replies: dict = field(default_factory=dict)  # 那時候每個指令的回覆
    early_progress: Any = None

    def phase_rounds(self, phase: str) -> list[RoundLog]:
        return [log for log in self.rounds if log.phase == phase]


def _collect_round(world: World, now: float) -> RoundLog:
    """照 jobs/market_snapshot._collect_worker 的做法跑一輪（只差 http 與時鐘是假的）。"""
    started_real = time.perf_counter()
    clock, store = world.clock, world.store
    clock.start_round(now)
    mono0 = clock.monotonic()
    calls0 = len(world.http.calls)
    supervisor = job.Supervisor(world.paths, job.parse_args(["--root", str(world.root)]), -1, wall=clock.wall)
    us_cal, tw_cal, problems = job._calendars(store)
    ctx = market_tasks.Context(
        store=store, http=world.http, ledger=None, now=now, paths=world.paths,
        deadline=clock.monotonic() + COLLECT_BUDGET_S, us_cal=us_cal, tw_cal=tw_cal, parent_pid=None,
        clock=clock.monotonic,
    )
    ctx.ledger = store.ledger(now_fn=clock.wall, sleep_fn=clock.sleep, stop_fn=ctx.should_stop)
    world.http.ledger = ctx.ledger
    summary = market_tasks.run_collect(ctx, TASK_LIST)
    store.set_task_state(job.COLLECT_STATE, {
        "run_id": supervisor.run_id, "completed_at": clock.wall(), "modules": {}, "calendars": problems,
        "tasks": summary["tasks"], "results": summary["results"], "first": summary["first"],
        "stopped": summary["stopped"],
    })
    hosts: dict[str, int] = {}
    for call in world.http.calls[calls0:]:
        hosts[call.host] = hosts.get(call.host, 0) + 1
    log = RoundLog("", 0, now, dict(summary["results"]), summary["stopped"], hosts, dict(problems),
                   clock.monotonic() - mono0, time.perf_counter() - started_real)
    world._supervisor = supervisor  # type: ignore[attr-defined]  # 這一輪的 run_id（publish、outcome 用）
    return log


def _render_all(world: World, now: float) -> dict:
    market_dashboard.clear_cache()
    return {text: market_dashboard.handle(text[len("/盤勢"):], now=now, paths=world.paths)
            for text in market_dashboard.SHIPPED_MARKET_TEXTS}


def _publish(world: World) -> dict:
    """照 _publish_worker：另開一個 Store 連線、呼叫真的 market_publish.publish（真的畫圖），再照監督程序寫 outcome。"""
    clock = world.clock
    supervisor = world._supervisor  # type: ignore[attr-defined]
    started_real = time.perf_counter()
    store = market_store.Store(world.paths.history_db, now_fn=clock.wall)
    try:
        fragment = market_publish.publish(store, world.paths, now=clock.wall())
        cleaned = job._clean(fragment) if isinstance(fragment, dict) else None
        store.set_task_state(job.PUBLISH_STATE, {
            "run_id": supervisor.run_id, "kind": (cleaned or {}).get("kind", "unknown"), "fragment": cleaned,
            "duration_s": 0.0, "finished_at": clock.wall(),
        })
    finally:
        store.close()
    world.publish_s.append(time.perf_counter() - started_real)
    outcome = supervisor.compose_outcome(code=job.code_status(world.paths), collect="ok", publish="ok",
                                         started_at=clock.wall(), locked_at=time.monotonic())
    data = json.dumps(outcome, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    market_paths.write_private_atomic(world.paths.outcome, data)
    return fragment


def _file_hashes() -> dict[str, str]:
    return {name: hashlib.sha256((WORK / name).read_bytes()).hexdigest()[:12] for name in WATCHED_FILES}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    started = time.perf_counter()
    patch = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("market_e2e")
    (root / "state").mkdir(mode=0o700)
    shutil.copyfile(WORK / "market_seasonality.json", root / "market_seasonality.json")
    paths = market_paths.Paths.under(root)
    clock = SimClock(PHASES[0][1].timestamp())
    store = market_store.Store(paths.history_db, now_fn=clock.wall)
    built = World(root=root, paths=paths, clock=clock, http=FakeHttp(clock), store=store, hashes=_file_hashes())

    def _real_fetch(url, **_kwargs):  # 模組範圍的 fixture 比 conftest 的擋網路早建：自己再擋一次
        built.real_fetch.append(url)
        raise market_http.FetchError("blocked_url")

    patch.setattr(market_http, "fetch", _real_fetch)
    patch.setattr(market_paths.Paths, "default", classmethod(lambda cls: paths))  # 漏傳 paths 也不會碰到正式 state/
    try:
        for phase, start in PHASES:
            for index in range(MAX_ROUNDS):
                log = _collect_round(built, start.timestamp() + index * ROUND_S)
                log.phase, log.index = phase, index
                built.rounds.append(log)
                if (phase, index) == (PHASES[0][0], 0):
                    # 部署後第一輪就發布一次：bootstrap 剛開始（每檔歷史都還不夠）時，家人按下去也要有東西看
                    built.early_publish = _publish(built)
                    built.early_now = clock.wall() + 60.0
                    built.early_replies = _render_all(built, built.early_now)
                    early = market_paths.read_private_file(paths.snapshot, 4 * 1024 * 1024)
                    built.early_progress = json.loads(early[0])["progress"] if early else None
                if not any(host not in PERIODIC_HOSTS for host in log.hosts):
                    break  # 除了定時來源之外沒有任何請求：這段做完了
            built.publishes.append(_publish(built))
        built.final_now = clock.wall() + 300.0  # 發布後 5 分鐘有人按了 /盤勢
        found = market_paths.read_private_file(paths.snapshot, 4 * 1024 * 1024)
        built.snapshot = json.loads(found[0].decode("utf-8")) if found else {}
        built.total_s = time.perf_counter() - started
        _print_rounds(built)
        yield built
    finally:
        store.close()
        patch.undo()


def _print_rounds(world: World) -> None:
    """`pytest -s` 時印出每一輪做了什麼（給報告用）。"""
    print(f"\n[market e2e] files: {world.hashes}")
    for log in world.rounds:
        busy = {name: kind for name, kind in log.results.items() if kind not in ("not_due", "backoff")}
        print(f"[round] {log.phase} #{log.index} {tpe(log.now):%m-%d %H:%M} used={log.used_s:.0f}s real={log.real_s:.2f}s "
              f"stopped={log.stopped} hosts={log.hosts}")
        print(f"        {busy}")
    for index, fragment in enumerate(world.publishes):
        print(f"[publish {index}] {json.dumps(fragment, ensure_ascii=False)[:1500]}")
    print(f"[publish early] {json.dumps(world.early_publish, ensure_ascii=False)[:1500]} progress={world.early_progress}")
    print(f"[publish real seconds] {[round(s, 2) for s in world.publish_s]}")
    print(f"[market e2e] total {world.total_s:.1f}s, rounds per phase: "
          f"{[len(world.phase_rounds(phase)) for phase, _ in PHASES]}")


# ── 斷言用的小工具 ─────────────────────────────────────────────────────────────


def _unexpected(value: Any, where: str = "") -> list[str]:
    """outcome 片段裡所有 unexpected:* 的位置。"""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += _unexpected(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += _unexpected(item, f"{where}[{index}]")
    elif isinstance(value, str) and (value.startswith("unexpected:") or value.startswith("import_error")):
        found.append(f"{where}={value}")
    return found


def _never_reason(world: World, row_id: str) -> str:
    """一列為什麼是 never：在同一個時間點直接叫那一列的 builder（只給失敗訊息用）。"""
    store = market_store.Store(world.paths.history_db, now_fn=world.clock.wall)
    try:
        ctx = market_publish._Ctx(store, world.paths, world.clock.wall())
        builder = market_publish._builder_for(row_id)
        if builder is None:
            return "no_builder"
        try:
            builder(ctx, row_id)
        except Exception as exc:  # noqa: BLE001 - 只是診斷
            return f"{type(exc).__name__}:{getattr(exc, 'kind', exc)}"
        return "builder ok now"
    finally:
        store.close()


def _strings(node: Any) -> list[str]:
    """Flex 卡片裡的每一個字串（text、altText、action 的 label／text…）。"""
    if isinstance(node, dict):
        return [s for value in node.values() for s in _strings(value)]
    if isinstance(node, list):
        return [s for value in node for s in _strings(value)]
    return [node] if isinstance(node, str) else []


_BAD_CATEGORIES = frozenset({"Cf", "Zl", "Zp", "Cs", "Co", "Cn"})


def _hygiene_problems(text: str) -> list[str]:
    problems = []
    if market_calc._URL_RE.search(text) or "http" in text.lower():
        problems.append("url")
    if market_calc._EMAIL_RE.search(text):
        problems.append("email")
    if market_calc._PHONE_RE.search(text):
        problems.append("phone")
    if "@" in text or "＠" in text:
        problems.append("at")
    for ch in text:
        category = unicodedata.category(ch)
        if category in _BAD_CATEGORIES or (category == "Cc" and ch != "\n"):
            problems.append(f"char:U+{ord(ch):04X}")
    return problems


LONG_HOST = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])  # card_pages 接受的最長固定網域
LONG_PAGE_URL = f"https://{LONG_HOST}:65535/card/{'A' * 22}"
SUMMARY_BUBBLE_MAX = 6656  # 6.5 KB（PLAN §6）
CAROUSEL_MAX = 28 * 1024


def _routing_shipped_texts() -> tuple[str, ...]:
    """test_market_routing.SHIPPED_MARKET_TEXTS（用 ast 讀，不 import main）。"""
    import ast

    tree = ast.parse((WORK / "test_market_routing.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "SHIPPED_MARKET_TEXTS" for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("SHIPPED_MARKET_TEXTS not found in test_market_routing.py")


_REPLIES: dict[str, Any] = {}


def _reply(world: World, text: str) -> Any:
    """main._reply_market 的呼叫方式：/盤勢 後面的字（不先 strip）交給 market_dashboard.handle。"""
    if text not in _REPLIES:
        market_dashboard.clear_cache()
        _REPLIES[text] = market_dashboard.handle(text[len("/盤勢"):], now=world.final_now, paths=world.paths)
    return _REPLIES[text]


def _bubbles(card: dict) -> list[dict]:
    return list(card["contents"]) if card.get("type") == "carousel" else [card]


def _is_chart(text: str) -> bool:
    return text.endswith("圖")


# ── 傳輸與 collect ──────────────────────────────────────────────────────────────


def test_the_router_only_saw_known_requests(world):
    assert not world.http.unknown, "假的來源不認得這些網址：\n" + "\n".join(world.http.unknown)
    assert not world.real_fetch, "繞過 ctx.http、打到真的 market_http.fetch：\n" + "\n".join(world.real_fetch)
    assert not world.http.no_ledger, "沒有帶這一輪的 ctx.ledger：\n" + "\n".join(world.http.no_ledger)
    assert not world.http.too_big, "回應超過端點的大小上限：\n" + "\n".join(world.http.too_big)
    hosts = {call.host for call in world.http.calls}
    assert hosts == set(market_http.ALLOWED_HOSTS) - {"query2.finance.yahoo.com"}  # 每個來源都真的打到了


def test_no_task_outcome_is_unexpected_and_calendars_build(world):
    bad = [f"{log.phase}#{log.index} {name}={kind}" for log in world.rounds for name, kind in log.results.items()
           if kind.startswith("unexpected:") or kind.startswith("import_error")]
    assert not bad, bad
    assert all(not log.calendar_problems for log in world.rounds), [log.calendar_problems for log in world.rounds]
    # 每個任務至少真的跑過一次（不是一直 not_due／backoff）。tw.twt49u 是每天 16:00 後的接續段：bootstrap 第一段
    # 已經涵蓋到週五，週六又不是交易日，這個時間窗裡本來就不會到期（下週一 16:00 才會）。
    ran = {name for log in world.rounds for name, kind in log.results.items() if kind in ("ok", "partial", "not_yet")}
    # 預測市場在台灣被封鎖（2026-10-23）：election.* 任務留在清單裡但永遠不到期，整個時間線都不該跑
    elections = {task.name for task in TASK_LIST if task.name.startswith("election.")}
    assert len(elections) == 4 and not elections & ran, sorted(elections & ran)
    assert all(kind == "not_due" for log in world.rounds for name, kind in log.results.items() if name in elections)
    expected = {task.name for task in TASK_LIST} - {market_pipeline_tw.TASK_TWT49U} - elections
    assert expected <= ran, sorted(expected - ran)


def test_each_phase_settles_within_the_round_cap(world):
    """跑到只剩定時來源有請求才算做完；輪數印在報表（-s）。"""
    for phase, _ in PHASES:
        rounds = world.phase_rounds(phase)
        assert rounds and len(rounds) < MAX_ROUNDS, f"{phase}：{len(rounds)} 輪還沒做完"
        assert not any(host not in PERIODIC_HOSTS for host in rounds[-1].hosts)
    assert world.total_s < 120, f"整個世界跑了 {world.total_s:.1f} 秒"


def test_collect_left_the_store_in_the_expected_state(world):
    """刻意放進來的狀況都真的走過：分割整段替換、跳空兩次一致接受、停牌、FinMind 全部接入、Yahoo 改代理。"""
    store, now = world.store, world.clock.wall()
    status = store.get_obs(market_pipeline_us.OBS_HIST_STATUS)["payload"]
    assert status["D"] == D2.isoformat() and status["total"] == len(UNIVERSE) + len(market_pipeline_us.ETFS)
    assert status["missing_d"] == 1 and status["quarantined"] == 0 and status["pending"] == 0, status  # 只有停牌那檔
    closes = {row.date: row for row in store.closes("us", SPLIT_NOW)}
    assert closes[D1].close == us_close(SPLIT_NOW, D1, now) and closes[D2].close == us_close(SPLIT_NOW, D2, now)
    assert closes[D1].close > 2.0  # 10/15 已是調整後的價（生效前未調整的是 0.4 左右）
    assert store.last_full_refetch("us", SPLIT_NOW) == D2
    jump = {row.date: row.close for row in store.closes("us", JUMP)}
    assert jump[JUMP_DAY] == us_close(JUMP, JUMP_DAY, now) and store.pending_jump("us", JUMP) is None
    assert max(row.date for row in store.closes("us", HALTED)) == HALTED_LAST
    assert store.quarantine("us") == {} and store.quarantine("tw") == {}
    assert len(store.closes("us", "SPY")) >= 250  # 240 日均線＋5 日前的比較都夠
    finmind = store.get_obs(market_pipeline_tw.OBS_FINMIND)["payload"]
    assert finmind["total"] == len(TW_MEMBERS) + 2 and finmind["accepted"] == finmind["total"], finmind
    assert not finmind["rejected"]
    assert len(store.closes("tw", "2330")) >= 250 and len(store.closes("tw", "TAIEX")) >= 250
    allbut_days = sorted(store.get_obs(market_pipeline_tw.OBS_ALLBUT)["payload"]["days"])
    assert allbut_days[-1] == T.isoformat() and len(allbut_days) >= 5
    assert {row.code for row in store.exrights("2330")} == {"2330"}
    macro = store.task_state(market_pipeline_misc.STATE_PREFIX + market_pipeline_misc.TASK_MACRO)
    assert macro["d"] == D2.isoformat() and macro["yahoo"] == "rate_limited", macro


# ── publish 與快照 ──────────────────────────────────────────────────────────────


def test_publish_outcomes_are_clean(world):
    assert len(world.publishes) == len(PHASES)
    for index, fragment in enumerate(world.publishes):
        assert fragment.get("kind") == "ok", (index, fragment)
        assert not _unexpected(fragment), (index, _unexpected(fragment))
        assert not fragment.get("row_errors") and not fragment.get("shared_errors"), (index, fragment)
        charts = fragment.get("charts") or {}
        assert charts.get("failed") == 0 and not charts.get("errors") and charts.get("dir") == "ok", (index, charts)
        assert fragment.get("snapshot_bytes", 0) < market_publish.SNAPSHOT_MAX_BYTES
    outcome = json.loads(market_paths.read_private_file(world.paths.outcome, 2_000_000)[0])
    assert outcome["status"] == "ok" and not _unexpected(outcome), _unexpected(outcome)


def test_snapshot_is_private_and_every_row_passes_row_ok(world):
    for path in (world.paths.snapshot, world.paths.history_db, world.paths.outcome):
        info = os.lstat(path)
        assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600, (path, oct(info.st_mode))
    snapshot = world.snapshot
    assert snapshot["schema"] == market_calc.SNAPSHOT_SCHEMA and set(snapshot["rows"]) == set(market_calc.ROW_IDS)
    assert [row_id for row_id, row in snapshot["rows"].items() if not market_calc.row_ok(row_id, row)] == []
    assert market_publish.validate_snapshot(snapshot) == []
    assert snapshot["progress"]["us_members"] == [len(UNIVERSE), len(UNIVERSE)]
    assert snapshot["progress"]["us_last_complete_day"] == D2.isoformat()
    assert snapshot["progress"]["tw_last_complete_day"] == T.isoformat()


def test_every_row_we_fed_is_present_and_fresh(world):
    rows = world.snapshot["rows"]
    never = {row_id: _never_reason(world, row_id) for row_id, row in rows.items() if row["state"] == "never"}
    assert not never, never  # 每一個來源都餵了：沒有一列應該是 never
    stale = [row_id for row_id, row in rows.items() if world.final_now > row["stale_after"]]
    assert not stale, stale
    assert {row_id for row_id, row in rows.items() if row["state"] == "unknown_period"} == {
        "us.13f.VOO", "us.13f.QQQ", "us.13f.SOXX"}


def test_rows_carry_the_scenario_values(world):
    """列是 ok 但值全是 null 也算接縫壞掉：抽幾個每一段都要算得出來的值。"""
    rows = world.snapshot["rows"]

    def values(row_id: str) -> dict:
        return rows[row_id]["values"]

    for etf in ("VOO", "QQQ", "SOXX", "SPY"):
        assert rows[f"us.etf.{etf}"]["data_date"] == D2.isoformat()
        assert None not in values(f"us.etf.{etf}")["ma_dist"].values()
    for etf in ("VOO", "QQQ", "SOXX"):
        per_n = values(f"us.breadth.{etf}")["per_n"]
        assert all(per_n[n]["status"] == "ok" for n in ("20", "60", "120", "240")), (etf, per_n)
    assert all(item["status"] == "ok" for item in values("tw.breadth.0050")["per_n"].values())
    assert None not in values("tw.etf.0050")["ma_dist"].values() and None not in values("tw.taiex")["ma_dist"].values()
    for symbol in ("QQQ", "SPY", "SOXX"):
        option = values(f"us.options.{symbol}")
        assert option["wall_status"] == "ok" and option["call_wall"] and option["put_wall"], option
        assert option["monthly"] and option["monthly"]["status"] == "ok" and option["monthly"]["expiry"] == "2026-11-20"
        assert option["gex"] and option["gex"]["status"] == "ok", option["gex"]
        assert world.snapshot["charts"][symbol]["inputs_date"] == D2.isoformat()
    assert values("tw.options")["status"] == "ok" and rows["tw.options"]["data_date"] == D1.isoformat()  # 期交所晚一天
    assert values("tw.inst3")["days_5d"] == 5 and values("tw.inst3")["foreign_5d_yi"] is not None
    assert values("macro.DXY")["source"] == "ETF 代理：UUP"  # 第二天 Yahoo 429 → 代理
    assert values("macro.MOVE")["source"] == "Yahoo" and rows["macro.MOVE"]["data_date"] == D1.isoformat()
    assert rows["macro.UST10Y"]["data_date"] == D2.isoformat()
    assert values("us.cftc.es")["contract"] == "S&P 500 Consolidated" and rows["us.cftc.es"]["data_date"] == "2026-10-13"
    assert values("us.margin")["month"] == "2026-09"
    assert all(len(values(f"us.13f.{etf}")["rows"]) == 10 for etf in ("VOO", "QQQ", "SOXX"))
    assert values("us.fund.QQQ")["pe_valid"] >= 9 and values("tw.fund.0050")["rev_month"] == "2026-09"
    kinds = {item["kind"] for item in values("events.next")["items"]}
    assert {"fomc", "cpi", "earnings"} <= kinds, kinds
    # 預測市場被封鎖（2026-10-23）：選舉列照樣是 ok、倒數照算，值裡寫 blocked 並且沒有報價
    assert values("election")["blocked"] is True and values("election")["quotes"] == []
    assert values("election")["state"] == "before" and values("election")["days_left"] == 18
    # BTC：Yahoo 第二天 429 → IBIT 代理（只有日漲跌幅可用）
    assert values("macro.BTC")["source"] == "ETF 代理：IBIT" and values("macro.BTC")["chg_pct"] is not None
    titles = [item["title"] for item in values("news.us")["items"]]
    assert "美股收高 科技股領漲" in titles and "三天前的舊聞" not in titles
    assert not any(_hygiene_problems(title) for title in titles), titles
    assert {"關稅", "伊朗"} <= set(values("trump.summary")["topics"])
    assert rows["trump.posts"]["values"]["items"] and rows["season.now"]["values"]["midterm_year"] is True


# ── bot 端：每個送出去的指令 ────────────────────────────────────────────────────


def test_shipped_texts_match_the_routing_test():
    assert market_dashboard.SHIPPED_MARKET_TEXTS == _routing_shipped_texts()
    assert len(market_dashboard.SHIPPED_MARKET_TEXTS) == 16


def _reply_problems(world: World, text: str, reply: Any, *, fresh: bool) -> list[str]:
    """一個回覆的結構、大小、前綴、圖檔與文字衛生。fresh＝資料都在而且沒過期（卡片一定要出來、圖一定要有）。"""
    problems: list[str] = []
    if reply is None:
        return ["no reply"]
    strings = [s for s in (reply.text, reply.alt_text, reply.fallback_text, reply.image_alt) if s]
    for value in (reply.text, reply.alt_text, reply.image_alt):
        if value and not value.startswith((market_dashboard.ALT_PREFIX, market_dashboard.CHART_ALT_PREFIX)):
            problems.append(f"prefix: {value[:40]!r}")
    if reply.images:
        if not reply.image_alt or not reply.image_alt.startswith(market_dashboard.CHART_ALT_PREFIX):
            problems.append("image_alt prefix")
        if not 1 <= len(reply.images) <= 4:  # main 的 _market_chart_urls 只送前 4 張
            problems.append(f"{len(reply.images)} images")
        for name in reply.images:
            path = world.paths.chart_dir / name
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                problems.append(f"missing chart {name}")
                continue
            if not (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and 0 < info.st_size < 1_000_000):
                problems.append(f"bad chart file {name}")
            if stat.S_IMODE(info.st_mode) != 0o600:
                problems.append(f"chart mode {oct(info.st_mode)}")
            with open(path, "rb") as handle:
                if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                    problems.append(f"not a png {name}")
    elif _is_chart(text):
        if fresh or reply.text != market_dashboard.TEXT_CHART_UNAVAILABLE:
            problems.append(f"chart command answered {reply.text!r}")
    elif reply.flex is None:
        if fresh:  # 資料都在：一定要是卡片，不是「資料準備中」之類的固定句
            problems.append(f"text instead of a card: {reply.text!r}")
    else:
        card = reply.flex
        if not reply.alt_text or len(reply.alt_text) > 400:
            problems.append("alt text")
        sizes = [market_dashboard.card_bytes(bubble) for bubble in _bubbles(card)]
        if market_dashboard.card_bytes(card) > CAROUSEL_MAX:
            problems.append(f"card {market_dashboard.card_bytes(card)} B > {CAROUSEL_MAX}")
        if max(sizes) > SUMMARY_BUBBLE_MAX:
            problems.append(f"bubble {max(sizes)} B > {SUMMARY_BUBBLE_MAX}")
        if not card_pages.fits_line_limits(card_pages.with_page_button(card, LONG_PAGE_URL)):
            problems.append("does not fit with the large-text buttons")
        strings += _strings(card)
    for value in strings:
        found = _hygiene_problems(value)
        if found:
            problems.append(f"{found}: {value[:60]!r}")
    return problems


@pytest.mark.parametrize("text", market_dashboard.SHIPPED_MARKET_TEXTS)
def test_every_shipped_command_replies_within_line_limits(world, text):
    assert _reply_problems(world, text, _reply(world, text), fresh=True) == []


@pytest.mark.parametrize("text", market_dashboard.SHIPPED_MARKET_TEXTS)
def test_every_command_renders_from_the_first_bootstrap_snapshot(world, text):
    """部署後第一輪（每檔歷史都還不夠、台股只有一天）：不丟例外、不超過大小、沒有外部字串漏網。"""
    reply = world.early_replies[text]
    assert _reply_problems(world, text, reply, fresh=False) == []
    if text == "/盤勢":
        assert reply.flex is not None or reply.text.startswith(market_calc.TEXT_PREPARING), reply


@pytest.mark.parametrize("text", market_dashboard.SHIPPED_MARKET_TEXTS)
def test_a_week_without_updates_shows_stale_cards_not_old_numbers(world, text):
    """job 停了一週（Mac 沒開）：卡片照樣出來但寫「過期」、圖改回固定句，也不能超過大小。"""
    later = world.final_now + 7 * 86400
    market_dashboard.clear_cache()
    reply = market_dashboard.handle(text[len("/盤勢"):], now=later, paths=world.paths)
    assert _reply_problems(world, text, reply, fresh=False) == []
    if text == "/盤勢":
        assert reply.flex is not None and any("過期" in s for s in _strings(reply.flex))
    if _is_chart(text):
        assert reply.text == market_dashboard.TEXT_CHART_UNAVAILABLE


@pytest.mark.parametrize("text", [t for t in market_dashboard.SHIPPED_MARKET_TEXTS if not t.endswith("圖")])
def test_every_card_passes_mains_real_flex_gate(world, text):
    """main 送卡前的真閘門（節點白名單、message action、每個字原樣過 output_validator、FlexContainer）。"""
    import main

    reply = _reply(world, text)
    main._validated_flex_card_message(reply.alt_text, reply.flex)
    assert main._prepare_outbound_text(reply.alt_text) == reply.alt_text
    if reply.fallback_text:
        assert main._prepare_outbound_text(reply.fallback_text) == reply.fallback_text
    paged = card_pages.with_page_button(reply.flex, LONG_PAGE_URL)
    assert len(json.dumps(paged, ensure_ascii=False).encode("utf-8")) <= card_pages.MAX_CARD_BYTES


@pytest.mark.parametrize("text", [t for t in market_dashboard.SHIPPED_MARKET_TEXTS if t.endswith("圖")])
def test_chart_replies_reach_mains_url_builder(world, text, monkeypatch):
    """bot 回的圖檔名，main 要在它服務的圖目錄找得到（正式環境 job 寫的圖目錄＝main 的 _GENIMG_DIR）。"""
    import main

    assert str(market_paths._CHART_DIR) == main._GENIMG_DIR
    monkeypatch.setattr(main, "_GENIMG_DIR", str(world.paths.chart_dir))
    monkeypatch.setattr(card_pages, "public_base_url", lambda: "https://example-e2e.trycloudflare.com")
    reply = _reply(world, text)
    urls = main._market_chart_urls(list(reply.images))
    assert urls == [f"https://example-e2e.trycloudflare.com/static/img/{name}" for name in reply.images]
    assert main._prepare_outbound_text(reply.image_alt) == reply.image_alt


def test_election_detail_says_the_prediction_markets_are_blocked(world):
    """2026-10-23：Polymarket、Kalshi 在台灣被封鎖，collect 不抓：選舉細節卡寫固定句，不是「暫缺」。"""
    texts = _strings(_reply(world, "/盤勢 選舉").flex)
    assert "預測市場：台灣已封鎖這些網站，暫不提供" in texts, texts
    assert any(s.startswith("投票日 11/3（美東），還有") for s in texts), texts
    assert not any("民主黨（" in s or "暫缺" in s for s in texts), texts


def test_summary_card_keeps_the_election_countdown_and_says_why_there_are_no_odds(world):
    rows = _strings(_reply(world, "/盤勢").flex)
    line = next(s for s in rows if s.startswith("期中選舉還有"))
    assert line.endswith("，預測市場：台灣已封鎖") and market_dashboard.row_width(line) <= market_dashboard.MAX_ROW_WIDTH, line


def test_macro_etf_proxies_are_fetched_exactly_once_each_after_nasdaq_published_d(world):
    """2026-10-23：Yahoo 429 的那天，8 個 ETF 代理加 BTC 的 IBIT 等 SPY 有 D 的正式列才去抓；這裡 Nasdaq 一公布就拿得到 D，
    所以每個代理在那個 D 剛好一個請求（第一天 Yahoo 正常，一個都沒有）。"""
    proxies = ("UUP", "USO", "BNO", "GLD", "SLV", "IBIT", "IWM", "DIA", "ONEQ")
    for etf in proxies:
        calls = [call for call in world.http.calls if call.host == "api.nasdaq.com" and call.path == f"/api/quote/{etf}/historical"]
        assert len(calls) == 1, (etf, [call.query for call in calls])
        assert calls[0].at >= YAHOO_RATE_LIMITED_FROM  # 第二天（Yahoo 429）才用代理
    rows = world.snapshot["rows"]
    for key in ("DXY", "WTI", "BRENT", "GOLD", "SILVER", "BTC", "RUT", "DJI", "IXIC"):
        assert rows[f"macro.{key}"]["data_date"] == D2.isoformat(), key  # 代理列和 ETF 一樣是 D 那天


def test_btc_row_comes_from_the_ibit_proxy_on_the_second_day(world):
    macro = _strings(_reply(world, "/盤勢").flex)
    assert any("比特幣" in s and "代理" in s for s in macro), macro
    detail = _strings(_reply(world, "/盤勢 新聞").flex)
    line = next(s for s in detail if s.startswith("比特幣（IBIT 代理）："))
    etf_close = world.snapshot["rows"]["macro.BTC"]["values"]["value"]
    assert "單日" in line and market_dashboard._price(etf_close) not in line, line  # 只寫漲跌幅，不印 ETF 股價


def test_summary_card_mentions_both_markets_and_their_dates(world):
    reply = _reply(world, "/盤勢")
    titles = [bubble["header"]["contents"][0]["text"] for bubble in _bubbles(reply.flex)]
    assert len(titles) == 4 and titles[0].startswith("美股（1/4）") and titles[1].startswith("台股（2/4）"), titles
    assert "10/16" in titles[0] and "10/16" in titles[1], titles


def test_status_summary_for_the_daily_maintenance_is_clean(world):
    summary = market_dashboard.status_summary(now=world.final_now, paths=world.paths)
    assert summary["snapshot"] == "ok" and not summary["disabled"] and "render_error" not in summary, summary
    assert summary["rows_missing"] == [] and summary["rows_stale"] == [] and summary["rows_unexpected"] == []
    assert summary["charts_stale"] == [] and summary["external_dropped"] == 0, summary
    assert summary["outcome"]["state"] == "ok" and summary["outcome"]["failing"] == [], summary["outcome"]
    assert summary["outcome"]["calendar_mismatch"] == [], summary["outcome"]


def _cjk_first(text: str) -> bool:
    return bool(text) and unicodedata.name(text[0], "").startswith("CJK")


def test_event_names_on_cards_are_chinese_first(world):
    """PLAN §6：先中文、括號英文。A 為此做了 market_sources_us.US_EVENT_LABELS（「初領失業金（Jobless Claims）」…），
    但 publish 只把 nfp／ppi／pce／cpi／gdp 換成種類，其他存英文白名單名、dashboard 照原字印出。"""
    summary = _strings(_reply(world, "/盤勢").flex)
    shown = [s[len("下一個事件："):].split("，")[0] for s in summary if s.startswith("下一個事件：")]
    news = _strings(_reply(world, "/盤勢 新聞").flex)
    start = news.index("接下來的事件")
    shown += [s.split("：")[0] for s in news[start + 1:] if "：" in s and ("（台北）" in s or "時間未公布" in s)]
    assert shown, news
    assert [name for name in shown if not _cjk_first(name)] == [], f"事件名稱沒有中文：{shown}"


def test_lines_from_an_older_data_day_carry_their_date(world):
    """期交所常晚一天（PLAN §3.2、§18.13）：台指牆是 10/15 的資料，卡片標題是「10/16 收盤」。
    dashboard 別的列在資料日和標題不同時會加「（M/D）」（_date_suffix），台指這兩處沒有。"""
    assert world.snapshot["rows"]["tw.options"]["data_date"] == D1.isoformat()  # 前提：這一列真的是前一天的
    card = _bubbles(_reply(world, "/盤勢").flex)[2]
    title = card["header"]["contents"][0]["text"]
    line = next(s for s in _strings(card) if s.startswith("台指選擇權："))
    assert "10/16" in title and "10/15" in line, (title, line)
    detail = _strings(_reply(world, "/盤勢 期權").flex)
    line = next(s for s in detail if s.startswith("台指選擇權（"))
    assert "10/15" in line, line


def test_card_size_table(world):
    """`pytest -s` 印出每個指令的最大 bubble 與整張卡大小（給報告用）；斷言在上面的參數化測試。"""
    lines = []
    for text in market_dashboard.SHIPPED_MARKET_TEXTS:
        reply = _reply(world, text)
        if reply.flex is None:
            lines.append(f"{text:16s} images={len(reply.images or [])}")
            continue
        sizes = [market_dashboard.card_bytes(bubble) for bubble in _bubbles(reply.flex)]
        paged = len(json.dumps(card_pages.with_page_button(reply.flex, LONG_PAGE_URL), ensure_ascii=False).encode())
        lines.append(f"{text:16s} bubbles={len(sizes)} max_bubble={max(sizes)} card={market_dashboard.card_bytes(reply.flex)}"
                     f" with_page_buttons={paged}")
    print("\n[card sizes]\n" + "\n".join(lines))
    assert len(lines) == 16
