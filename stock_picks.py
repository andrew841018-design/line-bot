"""股票推薦（台股，2026-10-10）：家人按「📈 股票推薦」或打 /股票，回一組可以左右滑的卡片。

Andrew 2026-10-09／10-10：「股票部分要新增建議買哪一檔＆建議的理由」「推薦理由只用公開數據算」
「股票推薦我要怎麼使用沒講清楚」「命令需要同時同步到大字版和非大字版」；10/10 看過真實卡片後選了
✅／⚠️／⏸️ 的燈號（台股紅漲綠跌，不用紅綠燈）、每天記下推薦了哪幾檔、每日維護回報這裡的狀態。

推薦只看五個公開條件（全部由程式算，不經過模型、不搜尋；都用卡片上顯示的精度判斷）：
- 長期向上：收盤 ≥ 年線（240 個交易日均價）。
- 靠近季線：收盤離季線（60 個交易日均價）在 ±5% 以內。
- 有在成長：最新一個月營收年增 > 0，而且 1 月到那個月的累計年增 > 0。
- 不算貴：本益比 ≤ 同產業上市公司本益比中位數（至少 5 家）。
- 有人在買：外資＋投信近 10 個交易日（含資料日）合計買超（10 天要齊）。
燈號只描述狀態，不寫買、賣、等、停損。單檔查詢與常問股用這五條；舊推薦（上市市值前 50 大、走勢兩條都要成立、
總共至少 4 條）照樣每天算、存進快照當影子紀錄，不再顯示。ETF、上櫃只看走勢，不進推薦。

2026-10-24 起 /股票 的推薦區改成「AI 相關股推薦」（Andrew：「股票推薦，請推，股價相對便宜，成長潛力高，與AI掛勾的
公司」「取代現在的推薦」）：名單＝00952 凱基台灣AI50 每天公布的持股＋程式裡每檔附公開來源的核心名單
（stock_picks_ai_core.json，每年 5、11 月檢查），再過「目前上市普通股」資格；AI 相關、營收成長（月增 > 0 且累計 ≥ 15.0%）、
本益比不高於同產業中位數（≥ 5.0 倍）、走勢（年線之上、比季線高不超過 10.0%）四條都成立才推，最多 3 檔，一樣用卡片上
顯示的精度判斷。名單解析與核心名單載入在葉節點模組 stock_picks_ai_source.py（缺檔時只有 AI 段失敗，/股票 照常）。
`LINE_BOT_STOCK_PICKS_AI=0`（模組載入時讀，改了要重啟）退回舊推薦卡、AI 段完全不跑；Yahoo 日曆抽查與主流程／AI 段共用的
429 冷卻也一起關掉（行為回到 2eb85e1）。單檔查詢任何時候都不看、也不設這個共用冷卻。

資料：證交所 OpenAPI（STOCK_DAY_ALL、BWIBBU_ALL、t187ap03_L、t187ap05_L）、證交所 T86
（外資、投信買賣超）、櫃買中心 OpenAPI（上櫃每日收盤）、Yahoo 日線（2 年；加權指數 ^TWII 當
交易日曆）。背景執行緒每 30 分鐘看一次證交所日資料的日期，有新的交易日才整輪重算，寫成快照；
家人按的時候只讀快照（股票池以外的單檔才即時抓一次 Yahoo，有限流）。抓資料一律走 `_get_json`
（https、不轉址、限大小、驗 Content-Type）。

不 import main 或 fcn_eval（停用 FCN 不能連帶弄壞 /股票）。卡片上只有固定句型、程式算的數字、
官方名稱與代號，沒有使用者原文；AI 相關股推薦卡的名單來源只有「凱基台灣AI50 持股」或程式內固定清單的核心分類
（網頁與核心檔裡的其他文字都不上卡片）。
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import stat
import statistics
import sys
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger("stock_picks")
_TPE = ZoneInfo("Asia/Taipei")

# ── 常數（門檻都是起始值，跑一陣子再調） ──────────────────────────────────────
PREFIX = "股票推薦："
EVAL_PREFIX = "股票評估："
TOP_N = 50
MAX_PICKS = 3
MIN_SCORE = 4
YEAR_BARS = 240
QUARTER_BARS = 60
MONTH_BARS = 20
NEAR_PERMILLE = 50  # 季線 ±5.0%
HOT_B20, HOT_B60 = 60, 120  # 大盤：比月線高 6%、比季線高 12% → 偏熱（千分位）
COLD_B20, COLD_B60 = -60, -100
MIN_INDUSTRY_PE = 5
FAQ_CODES = ("0050", "2330")

STOCK, ETF, OTC = "stock", "etf", "otc"
TWSE, TPEX = "twse", "tpex"  # 上市（證交所）／上櫃（櫃買中心）
# 燈號等級（Andrew 2026-10-10：台股看盤習慣紅漲綠跌，燈號不用紅綠燈，字色也避開紅綠）。
MET, CAUTION, BELOW_YEAR, NO_DATA = "met", "caution", "below_year", "no_data"
_EMOJI = {MET: "✅", CAUTION: "⚠️", BELOW_YEAR: "⏸️", NO_DATA: "⚪"}
_COLOR = {MET: "#1E5AA8", CAUTION: "#B7791F", BELOW_YEAR: "#555555", NO_DATA: "#666666"}
_KIND_ONLY_TREND = {ETF: "ETF 只看走勢", OTC: "上櫃只看走勢"}

# 條件代號（順序就是卡片上的順序）
LONG, MID, GROWTH, CHEAP, INST = "long", "mid", "growth", "cheap", "inst"
CONDITION_ORDER = (LONG, MID, GROWTH, CHEAP, INST)
CONDITION_NAME = {
    LONG: "長期向上",
    MID: "靠近季線",
    GROWTH: "有在成長",
    CHEAP: "不算貴",
    INST: "有人在買",
}


# ── 數字與日期 ─────────────────────────────────────────────────────────────
def _num(value) -> Optional[float]:
    """證交所的字串數字：去逗號與空白；空白、「--」「-」「N/A」與非有限值 → None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:  # JSON 的超大整數（2026-10-17 審查）
            return None
    elif isinstance(value, str):
        text = value.replace(",", "").strip()
        if text in ("", "--", "-", "N/A", "X"):
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _roc_date(text) -> Optional[date]:
    """「1151008」→ 2026-10-08；格式不對回 None。"""
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not (6 <= len(raw) <= 7 and raw.isascii() and raw.isdigit()):
        return None
    try:
        return date(int(raw[:-4]) + 1911, int(raw[-4:-2]), int(raw[-2:]))
    except ValueError:
        return None


def _roc_month(text) -> Optional[tuple[int, int]]:
    """營收的資料年月「11508」→ (2026, 8)。"""
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not (4 <= len(raw) <= 5 and raw.isascii() and raw.isdigit()):
        return None
    year, month = int(raw[:-2]) + 1911, int(raw[-2:])
    return (year, month) if 1 <= month <= 12 else None


def _round_half_up(x: float) -> int:
    """正負對稱的四捨五入（0.5 進位；加一點容差，免得 4.95 被浮點數算成 4.9499…）。"""
    magnitude = int(math.floor(abs(x) + 0.5 + 1e-9))
    return -magnitude if x < 0 else magnitude


def _permille(ratio: float) -> int:
    """比例 → 千分位整數：0.0523 → 52（顯示成 5.2%）。判斷和顯示都用這個整數。"""
    return _round_half_up(ratio * 1000)


def _fmt_tenths(value: int) -> str:
    """千分位（或「十分之一個百分點」）整數 → 「5.2%」的數字部分「5.2」，不帶正負號。"""
    magnitude = abs(value)
    return f"{magnitude // 10}.{magnitude % 10}"


def _tenths(value: float) -> int:
    """百分比數字（證交所營收年增率，例如 53.32）→ 四捨五入到 0.1 的整數（533）。"""
    return _round_half_up(value * 10)


def _display_value(price: float) -> float:
    """卡片上顯示的價格數值：1000 元以上到元、其餘到 0.01 元（四捨五入）。年線判斷也用這個。"""
    if price >= 1000:
        return float(_round_half_up(price))
    return _round_half_up(price * 100) / 100


def _fmt_price(price: float) -> str:
    """價格（收盤、均價都一樣）：1000 元以上不帶小數，其餘最多兩位小數；去掉多餘的 0。"""
    value = _display_value(price)
    if value >= 1000:
        return f"{value:,.0f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _fmt_lots(shares: int) -> str:
    """股數 → 張：一萬張以上寫「1.2 萬張」，其餘「3,456 張」。"""
    lots = _round_half_up(abs(shares) / 1000)
    if lots >= 10000:
        tenths = _round_half_up(lots / 1000)  # 萬張的十分位
        return f"{tenths // 10}.{tenths % 10} 萬張"
    return f"{lots:,} 張"


def _fmt_date(day: date) -> str:
    return f"{day.month}/{day.day:02d}"


# ── 走勢 ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Trend:
    close: float
    ma60: float
    ma240: float
    p60: int  # 收盤比季線，千分位（52 ＝ 高 5.2%）

    @property
    def above_year(self) -> bool:
        """長期向上與 ⏸️ 共用這一個判斷：顯示出來的收盤 ≥ 顯示出來的年線。"""
        return _display_value(self.close) >= _display_value(self.ma240)

    @property
    def near_quarter(self) -> bool:
        """靠近季線：±5.0% 以內（顯示「高 5.0%」的一定算在以內）。"""
        return abs(self.p60) <= NEAR_PERMILLE


MAX_MISSING_YEAR = 5  # 年線窗（240 個交易日）最多缺 5 天
MAX_MISSING_QUARTER = 2  # 季線窗（60 個交易日）最多缺 2 天


def trend_of(year: Sequence[float], quarter: Sequence[float]) -> Optional[Trend]:
    """year／quarter：年線窗、季線窗裡的收盤（舊到新，最後一個都是資料日收盤；缺的日子不補）。
    年線窗至少 235 個、季線窗至少 58 個，都要是正的有限值，否則 None（資料不足）。"""
    y = [float(v) for v in year][-YEAR_BARS:]
    q = [float(v) for v in quarter][-QUARTER_BARS:]
    if len(y) < YEAR_BARS - MAX_MISSING_YEAR or len(q) < QUARTER_BARS - MAX_MISSING_QUARTER:
        return None
    if not all(math.isfinite(v) and v > 0 for v in y + q) or q[-1] != y[-1]:
        return None
    close = y[-1]
    ma60 = math.fsum(q) / len(q)
    ma240 = math.fsum(y) / len(y)
    return Trend(close=close, ma60=ma60, ma240=ma240, p60=_permille(close / ma60 - 1.0))


# ── 大盤 ───────────────────────────────────────────────────────────────────
HOT, COLD, NEUTRAL = "hot", "cold", "neutral"


@dataclass(frozen=True)
class Market:
    level: str
    b20: int  # 加權指數比月線，千分位
    b60: int  # 比季線，千分位


def market_of(closes: Sequence[float]) -> Optional[Market]:
    """先判偏熱再判偏冷（兩個都成立時算偏熱）。"""
    values = [float(v) for v in closes[-QUARTER_BARS:]]
    if len(values) < QUARTER_BARS or not all(math.isfinite(v) and v > 0 for v in values):
        return None
    close = values[-1]
    b20 = _permille(close / (math.fsum(values[-MONTH_BARS:]) / MONTH_BARS) - 1.0)
    b60 = _permille(close / (math.fsum(values) / QUARTER_BARS) - 1.0)
    if b20 > HOT_B20 or b60 > HOT_B60:
        level = HOT
    elif b20 < COLD_B20 or b60 < COLD_B60:
        level = COLD
    else:
        level = NEUTRAL
    return Market(level=level, b20=b20, b60=b60)


def _market_gap_words(market: Market) -> str:
    """偏熱／偏冷是哪一條觸發的就寫哪一條；兩條都觸發寫月線。"""
    if market.level == HOT:
        use_month = market.b20 > HOT_B20
    else:
        use_month = market.b20 < COLD_B20
    value, line = (market.b20, "月線") if use_month else (market.b60, "季線")
    return f"比{line}{'高' if value >= 0 else '低'} {_fmt_tenths(value)}%"


def market_line(market: Optional[Market]) -> str:
    """大盤那一行（推薦卡標題下、單檔卡頁尾）。"""
    if market is None:
        return "大盤：資料不足"
    if market.level == HOT:
        return f"大盤：偏熱（{_market_gap_words(market)}），追高風險較大"
    if market.level == COLD:
        return f"大盤：偏冷（{_market_gap_words(market)}）"
    return "大盤：中性"


# ── 個股條件與燈號 ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Revenue:
    year: int
    month: int
    yoy: Optional[float]  # 單月年增率（%）
    cum_yoy: Optional[float]  # 1 月到這個月的累計年增率（%）


@dataclass(frozen=True)
class Facts:
    """一檔股票算燈號需要的全部數字（快照裡存的就是這些）。"""

    code: str
    name: str
    kind: str  # STOCK／ETF／OTC
    data_date: date
    trend: Optional[Trend]
    revenue: Optional[Revenue] = None
    pe: Optional[float] = None
    pe_median: Optional[float] = None
    inst_net: Optional[int] = None  # 外資＋投信 10 個交易日合計（股）
    cap: Optional[float] = None  # 市值（元），只有上市普通股有
    market: str = TWSE  # 收盤從哪裡來（卡片「依據」那行）


@dataclass(frozen=True)
class Check:
    key: str
    state: Optional[bool]  # True 成立、False 不成立、None 資料不足
    text: str


def _more_less(value: int) -> str:
    """十分位整數 → 「多 5.3%」或「少 1.2%」。"""
    return f"{'多' if value >= 0 else '少'} {_fmt_tenths(value)}%"


def _growth_check(revenue: Optional[Revenue]) -> Check:
    if revenue is None or revenue.yoy is None or revenue.cum_yoy is None:
        return Check(GROWTH, None, "營收資料不足")
    month_t, cum_t = _tenths(revenue.yoy), _tenths(revenue.cum_yoy)
    text = f"{revenue.month} 月營收比去年 {revenue.month} 月{_more_less(month_t)}"
    if revenue.month >= 2:  # 1 月的累計就是單月，不重複寫
        text += f"，1–{revenue.month} 月累計比去年同期{_more_less(cum_t)}"
    return Check(GROWTH, month_t > 0 and cum_t > 0, text)


def _cheap_check(pe: Optional[float], median: Optional[float]) -> Check:
    if pe is None or pe <= 0:
        return Check(CHEAP, None, "本益比不適用（虧損或沒有資料）")
    pe_t = _tenths(pe)
    if median is None or median <= 0:
        return Check(CHEAP, None, f"本益比 {_fmt_tenths(pe_t)} 倍，同產業資料不足")
    med_t = _tenths(median)
    if pe_t == med_t:
        text = f"本益比 {_fmt_tenths(pe_t)} 倍，和同產業中位數一樣"
    else:
        text = (
            f"本益比 {_fmt_tenths(pe_t)} 倍，比同產業中位數 {_fmt_tenths(med_t)} 倍"
            f"{'低' if pe_t < med_t else '高'}"
        )
    return Check(CHEAP, pe_t <= med_t, text)


def _inst_check(net: Optional[int]) -> Check:
    if net is None:
        return Check(INST, None, "法人買賣資料不足")
    lots = _round_half_up(net / 1000)
    if lots == 0:
        return Check(INST, False, "外資和投信近 10 個交易日合計買賣超不到 1 張")
    word = "買超" if lots > 0 else "賣超"
    return Check(INST, lots > 0, f"外資和投信近 10 個交易日合計{word} {_fmt_lots(net)}")


def checks_of(facts: Facts) -> list[Check]:
    """五個條件（ETF、上櫃只有走勢兩條）。"""
    trend = facts.trend
    if trend is None:
        result = [Check(LONG, None, "日線資料不足"), Check(MID, None, "日線資料不足")]
    else:
        result = [
            Check(LONG, trend.above_year, "在年線之上" if trend.above_year else "在年線之下"),
            Check(
                MID,
                trend.near_quarter,
                f"比季線{'高' if trend.p60 >= 0 else '低'} {_fmt_tenths(trend.p60)}%"
                f"（{'在 ±5% 以內' if trend.near_quarter else '超過 5%'}）",
            ),
        ]
    if facts.kind != STOCK:
        return result
    return result + [
        _growth_check(facts.revenue),
        _cheap_check(facts.pe, facts.pe_median),
        _inst_check(facts.inst_net),
    ]


def score_of(facts: Facts) -> int:
    """成立的條數（資料不足不算）。"""
    return sum(1 for check in checks_of(facts) if check.state is True)


@dataclass(frozen=True)
class Light:
    level: str
    text: str
    score_in_text: Optional[int] = None  # 燈號文字裡已經寫了「符合幾條」時才有（單檔卡就不再補一次）

    @property
    def emoji(self) -> str:
        return _EMOJI[self.level]

    @property
    def color(self) -> str:
        return _COLOR[self.level]


def light_of(facts: Facts) -> Light:
    """唯一的燈號函式（推薦卡、常問股、單檔卡共用），依序判斷、互斥、涵蓋全部輸入。"""
    trend = facts.trend
    if trend is None:
        return Light(NO_DATA, "資料不足")
    if not trend.above_year:
        return Light(BELOW_YEAR, "長期偏弱（在年線之下）")
    if trend.p60 > NEAR_PERMILLE:
        return Light(CAUTION, f"漲多了（比季線高 {_fmt_tenths(trend.p60)}%）")
    if trend.p60 < -NEAR_PERMILLE:
        return Light(CAUTION, f"比季線低 {_fmt_tenths(trend.p60)}%")
    if facts.kind != STOCK:
        return Light(MET, f"走勢 2 條都符合（{_KIND_ONLY_TREND[facts.kind]}）")
    score = score_of(facts)
    if score >= MIN_SCORE:
        return Light(MET, _score_words(score), score)
    return Light(CAUTION, f"走勢可以，其他條件不足（符合 {score} 條，共 5 條）", score)


def _score_words(score: int) -> str:
    """「符合 4 條（共 5 條）」：「4／5」和旁邊的日期（10/08）放在一起會被看成 4 月 5 日。"""
    return f"符合 {score} 條（共 5 條）"


def is_pick_candidate(facts: Facts) -> bool:
    """上市普通股、市值有算到、燈號 ✅（走勢兩條＋總共 ≥ 4 條）。"""
    return facts.kind == STOCK and facts.cap is not None and light_of(facts).level == MET


def pick_sort_key(facts: Facts) -> tuple:
    """(分數 降冪, |季線乖離| 升冪, 市值 降冪, 代號 升冪)。"""
    trend = facts.trend
    return (
        -score_of(facts),
        abs(trend.p60) if trend is not None else 10**9,
        -(facts.cap or 0.0),
        facts.code,
    )


def choose_picks(universe: Iterable[Facts], limit: int = MAX_PICKS) -> list[Facts]:
    candidates = [f for f in universe if is_pick_candidate(f)]
    return sorted(candidates, key=pick_sort_key)[:limit]


def most_missed(universe: Iterable[Facts]) -> Optional[tuple[str, int, int]]:
    """沒有推薦時說明最常差哪一條：(條件代號, 沒過的檔數, 總檔數)；同數照卡片順序。"""
    rows = [f for f in universe if f.kind == STOCK]
    if not rows:
        return None
    misses = {key: 0 for key in CONDITION_ORDER}
    for facts in rows:
        for check in checks_of(facts):
            if check.state is not True:
                misses[check.key] += 1
    key = max(CONDITION_ORDER, key=lambda k: (misses[k], -CONDITION_ORDER.index(k)))
    return key, misses[key], len(rows)


# ── 卡片（給爸媽：字大、燈號＋一句話；只有固定句型、數字、官方簡稱與代號） ─────────
TEXT_DISCLAIMER = "只供參考，不是買賣指示"
TEXT_POOL_NOTE = "推薦卡只從上市市值前 50 大挑，最多 3 檔"
TEXT_REFERENCE_PREFIX = "季線（近 3 個月均價）"
TEXT_NOT_UPDATED = "資料暫時沒有更新，先不推薦"
TEXT_LEAD_NOTE = "用過去的公開數字篩選，不保證會漲，買賣自己決定"


def _text(text: str, *, size: str = "lg", weight: str | None = None, color: str | None = None) -> dict:
    node = {"type": "text", "text": text, "size": size, "wrap": True}
    if weight:
        node["weight"] = weight
    if color:
        node["color"] = color
    return node


def _button(label: str, command: str) -> dict:
    return {
        "type": "button",
        "style": "primary",
        "height": "md",
        "action": {"type": "message", "label": label, "text": command},
    }


def _separator() -> dict:
    return {"type": "separator", "margin": "md"}


def _bubble(title: str, subtitle: str | None, body: list[dict], footer: list[dict]) -> dict:
    header = [_text(title, size="xl", weight="bold")]
    if subtitle:
        header.append(_text(subtitle, size="md", color="#555555"))
    return {
        "type": "bubble",
        "size": "giga",
        "header": {"type": "box", "layout": "vertical", "contents": header},
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": footer},
    }


def _label(facts: Facts) -> str:
    return f"{facts.name} {facts.code}" if facts.name else facts.code


def _close_line(facts: Facts) -> str:
    if facts.trend is None:
        return f"資料到 {_fmt_date(facts.data_date)}"
    return f"收盤 {_fmt_price(facts.trend.close)} 元（{_fmt_date(facts.data_date)}）"


def _reference_line(trend: Trend) -> str:
    return f"{TEXT_REFERENCE_PREFIX}{_fmt_price(trend.ma60)} 元｜年線（近 1 年均價）{_fmt_price(trend.ma240)} 元"


def _check_line(check: Check) -> str:
    mark = "✅" if check.state is True else "⬜"
    return f"{mark} {CONDITION_NAME[check.key]}：{check.text}"


def _source_line(data_date: date, market: str = TWSE) -> str:
    who = "櫃買中心" if market == TPEX else "證交所"
    return f"依據：{who}、Yahoo｜資料到 {_fmt_date(data_date)}"


def _detail_command(code: str) -> str:
    return f"/股票 {code}"


def _lead(lead_note: bool) -> list[dict]:
    return [_text(TEXT_LEAD_NOTE, size="md", weight="bold", color="#555555")] if lead_note else []


def pick_bubble(facts: Facts, index: int, total: int, market: Optional[Market], *, lead_note: bool = False) -> dict:
    """推薦卡：大盤行＋燈號＋收盤＋理由（成立的條件）＋沒過的條件＋均價參考。"""
    light = light_of(facts)
    checks = checks_of(facts)
    body: list[dict] = _lead(lead_note) + [
        _text(f"{light.emoji} {_label(facts)}", size="xl", weight="bold", color=light.color),
        _text(f"{light.text}｜{_close_line(facts)}"),
        _separator(),
        _text("推薦理由", size="md", weight="bold"),
    ]
    body += [_text(_check_line(c), size="md") for c in checks if c.state is True]
    missed = [c for c in checks if c.state is not True]
    if missed:
        body.append(_text("沒過的條件", size="md", weight="bold"))
        body += [_text(_check_line(c), size="md", color="#555555") for c in missed]
    if facts.trend is not None:
        body += [_separator(), _text(_reference_line(facts.trend), size="md", color="#555555")]
    footer = [
        _text(_source_line(facts.data_date), size="sm", color="#555555"),
        _text(f"{TEXT_POOL_NOTE}；{TEXT_DISCLAIMER}", size="sm", color="#555555"),
    ]
    return _bubble(f"股票推薦：第 {index} 檔（共 {total} 檔）", market_line(market), body, footer)


def no_pick_bubble(
    data_date: date,
    market: Optional[Market],
    missed: Optional[tuple[str, int, int]],
    *,
    not_updated: bool,
    lead_note: bool = False,
) -> dict:
    """沒有推薦（這次沒有符合的，或資料沒有更新先不推薦）。"""
    if not_updated:
        body = _lead(lead_note) + [
            _text(TEXT_NOT_UPDATED, size="xl", weight="bold"),
            _text(f"最後一次的資料到 {_fmt_date(data_date)}"),
        ]
    else:
        body = _lead(lead_note) + [
            _text(f"這次（資料到 {_fmt_date(data_date)}）台股沒有符合條件的", size="xl", weight="bold")
        ]
        if missed is not None:
            key, count, total = missed
            body.append(_text(f"最常差的條件：{CONDITION_NAME[key]}（{total} 檔裡有 {count} 檔沒過）"))
    footer = [
        _text(_source_line(data_date), size="sm", color="#555555"),
        _text(f"{TEXT_POOL_NOTE}；{TEXT_DISCLAIMER}", size="sm", color="#555555"),
    ]
    return _bubble("股票推薦：台股", market_line(market), body, footer)


def faq_bubble(rows: Sequence[Facts], data_date: date) -> dict:
    """常問股：每檔一列燈號＋收盤＋一句話，各一顆「看細節」按鈕。"""
    body: list[dict] = []
    footer: list[dict] = []
    for i, facts in enumerate(rows):
        light = light_of(facts)
        if i:
            body.append(_separator())
        body.append(_text(f"{light.emoji} {_label(facts)}", size="xl", weight="bold", color=light.color))
        body.append(_text(light.text))
        body.append(_text(_close_line(facts), size="md", color="#555555"))
        footer.append(_button(f"看{facts.name}細節", _detail_command(facts.code)))
    footer = [
        _text(_source_line(data_date), size="sm", color="#555555"),
        _text(f"想看別檔：打 /股票 代號或名稱；{TEXT_DISCLAIMER}", size="sm", color="#555555"),
        *footer,
    ]
    return _bubble(f"{EVAL_PREFIX}常問股", None, body, footer)  # 常問股不是推薦，標題不寫「股票推薦」


def detail_bubble(facts: Facts, *, market: Optional[Market]) -> dict:
    """單檔卡：燈號＋收盤＋每個條件一列（✅／⬜＋數字）＋均價參考。"""
    light = light_of(facts)
    checks = checks_of(facts)
    second = _close_line(facts)
    if facts.kind == STOCK and light.score_in_text is None and light.level != NO_DATA:  # 燈號沒寫分數、也不是 ⚪ 才補
        second += f"｜{_score_words(score_of(facts))}"
    body: list[dict] = [
        _text(f"{light.emoji} {light.text}", size="xl", weight="bold", color=light.color),
        _text(second),
        _separator(),
    ]
    body += [_text(_check_line(c), size="md") for c in checks]
    if facts.kind != STOCK:
        body.append(_text(f"營收、本益比、法人：{_KIND_ONLY_TREND[facts.kind]}，不適用", size="md", color="#555555"))
    if facts.trend is not None:
        body += [_separator(), _text(_reference_line(facts.trend), size="md", color="#555555")]
    footer = [
        _text(market_line(market), size="sm", color="#555555"),
        _text(_source_line(facts.data_date, facts.market), size="sm", color="#555555"),
    ]
    if facts.kind == STOCK:
        footer.append(_text(TEXT_AI_POOL_NOTE if AI_ENABLED else TEXT_POOL_NOTE, size="sm", color="#555555"))
    footer.append(_text(TEXT_DISCLAIMER, size="sm", color="#555555"))
    return _bubble(f"股票評估：{_label(facts)}", None, body, footer)


def carousel(bubbles: Sequence[dict]) -> dict:
    return {"type": "carousel", "contents": list(bubbles)}


def picks_alt_text(picks: Sequence[Facts], data_date: date, *, not_updated: bool) -> str:
    """聊天列表與通知只看得到這行，所以「只供參考」放最前面。"""
    if not_updated:
        what = TEXT_NOT_UPDATED
    elif not picks:
        what = "這次台股沒有符合條件的"
    else:
        what = "、".join(p.name or p.code for p in picks)
    return f"{PREFIX}只供參考｜{what}｜資料到 {_fmt_date(data_date)}"


def detail_alt_text(facts: Facts) -> str:
    """通知與聊天列表只看得到這行：「只供參考」放最前面（前綴不變，引用守門照樣認得）。"""
    light = light_of(facts)
    return f"{EVAL_PREFIX}只供參考｜{_label(facts)}｜{light.emoji} {light.text}"


# ── 解析證交所／Yahoo 的資料（純函式；外部資料一律當不可信：型別、長度、數值都檢查） ─────
_CODE_MAX = 6
_NAME_MAX = 16
_FULL_NAME_MAX = 24
# 組網址與「/股票 代號」只收：上市／上櫃普通股 4 碼、ETF（00 開頭，可帶一個大寫字母）。[0-9] 不收其他文字的數字。
_FETCH_CODE_RE = re.compile(r"[0-9]{4}|00[0-9]{2,4}[A-Z]?")


def _clean_code(value) -> Optional[str]:
    """證券代號：4–6 位數字，可帶一個大寫字母（特別股、ETF 的 B／L／R／U）。"""
    if not isinstance(value, str):
        return None
    code = value.strip()
    if not (4 <= len(code) <= _CODE_MAX + 1 and code.isascii()):
        return None
    digits, tail = (code[:-1], code[-1]) if code[-1].isalpha() else (code, "")
    if not (4 <= len(digits) <= _CODE_MAX and digits.isdigit()) or (tail and not tail.isupper()):
        return None
    return code


_NAME_PUNCT = frozenset(" -+&.()")


def _name_char_ok(ch: str) -> bool:
    if ch.isascii():
        return ch.isalnum() or ch in _NAME_PUNCT
    code = ord(ch)
    return 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF


def _clean_name(value, max_len: int = _NAME_MAX) -> Optional[str]:
    """官方簡稱 → 卡片上的名稱：NFKC、去掉「*」（同一行兩個「*」會被 main 的 Markdown 轉換
    當斜體吃掉）、去頭尾空白、最多 16 字、只收中文、英數、空白與 -+&.()；不合格回 None
    （卡片只顯示代號）。"""
    if not isinstance(value, str):
        return None
    name = unicodedata.normalize("NFKC", value).replace("*", "").strip()
    if not (1 <= len(name) <= max_len) or not all(_name_char_ok(ch) for ch in name):
        return None
    if "  " in name:
        return None
    return name


def _clean_full_name(value) -> Optional[str]:
    """公司全名（只拿來比對查詢，不上卡片）：去掉「股份有限公司」「有限公司」，其他同簡稱的規則。
    「聯華電子」「中華航空」才不會被最長前綴對到「聯華」「中華」。"""
    if not isinstance(value, str):
        return None
    name = unicodedata.normalize("NFKC", value).strip()
    for suffix in ("股份有限公司", "有限公司"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return _clean_name(name, _FULL_NAME_MAX)


@dataclass(frozen=True)
class DayRow:
    name: str
    close: Optional[float]
    change: Optional[float]


def _keep_unique(table: dict, conflicts: set, code: str, value) -> None:
    """同一份資料裡同代號出現兩次：值一樣就算了，不一樣就整個代號不收（不採最後一筆）。"""
    if code in table and table[code] != value:
        conflicts.add(code)
    table[code] = value


def parse_day_all(rows) -> tuple[Optional[date], dict[str, DayRow]]:
    """STOCK_DAY_ALL：回 (資料日, {代號: DayRow})；全部列的日期必須一樣，否則日期回 None。"""
    if not isinstance(rows, list):
        return None, {}
    dates: set[date] = set()
    table: dict[str, DayRow] = {}
    conflicts: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        code, name, day = _clean_code(row.get("Code")), _clean_name(row.get("Name")), _roc_date(row.get("Date"))
        if code is None or day is None:
            continue
        dates.add(day)
        close = _num(row.get("ClosingPrice"))
        _keep_unique(table, conflicts, code, DayRow(
            name=name or "", close=close if close and close > 0 else None, change=_num(row.get("Change"))
        ))
    for code in conflicts:
        del table[code]
    return (dates.pop() if len(dates) == 1 else None), table


@dataclass(frozen=True)
class Company:
    name: str
    industry: str
    shares: Optional[float]
    full_name: str = ""


def parse_companies(rows) -> dict[str, Company]:
    """t187ap03_L：只留代號、簡稱、全名、產業別、已發行股數（董事長、電話等個資一律不讀）。"""
    table: dict[str, Company] = {}
    conflicts: set[str] = set()
    if not isinstance(rows, list):
        return table
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = _clean_code(row.get("公司代號"))
        industry = row.get("產業別")
        if code is None or not isinstance(industry, str) or len(industry.strip()) > 4:
            continue
        shares = _num(row.get("已發行普通股數或TDR原股發行股數"))
        _keep_unique(table, conflicts, code, Company(
            name=_clean_name(row.get("公司簡稱")) or "",
            industry=industry.strip(),
            shares=shares if shares and shares > 0 else None,
            full_name=_clean_full_name(row.get("公司名稱")) or "",
        ))
    for code in conflicts:
        del table[code]
    return table


def parse_pe(rows) -> tuple[Optional[date], dict[str, float]]:
    """BWIBBU_ALL：回 (資料日, {代號: 本益比})；本益比 ≤ 0 或空白的不收。"""
    if not isinstance(rows, list):
        return None, {}
    dates: set[date] = set()
    table: dict[str, Optional[float]] = {}
    conflicts: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        code, day = _clean_code(row.get("Code")), _roc_date(row.get("Date"))
        if code is None or day is None:
            continue
        dates.add(day)
        pe = _num(row.get("PEratio"))
        _keep_unique(table, conflicts, code, pe if pe is not None and 0 < pe < 10000 else None)
    result = {code: pe for code, pe in table.items() if code not in conflicts and pe is not None}
    return (dates.pop() if len(dates) == 1 else None), result


def parse_revenue(rows) -> dict[str, Revenue]:
    """t187ap05_L：{代號: Revenue}；同代號取資料年月最新的，同一個月兩筆不一樣就不收。"""
    table: dict[str, Revenue] = {}
    conflicts: set[tuple[str, int, int]] = set()
    if not isinstance(rows, list):
        return table
    for row in rows:
        if not isinstance(row, dict):
            continue
        code, ym = _clean_code(row.get("公司代號")), _roc_month(row.get("資料年月"))
        if code is None or ym is None:
            continue
        yoy, cum = _num(row.get("營業收入-去年同月增減(%)")), _num(row.get("累計營業收入-前期比較增減(%)"))
        revenue = Revenue(year=ym[0], month=ym[1], yoy=yoy, cum_yoy=cum)
        old = table.get(code)
        if old is None or (old.year, old.month) < ym:
            table[code] = revenue
        elif (old.year, old.month) == ym and old != revenue:
            conflicts.add((code, ym[0], ym[1]))
    for code, year, month in conflicts:
        current = table.get(code)
        if current is not None and (current.year, current.month) == (year, month):
            del table[code]
    return table


T86_OK, T86_NOT_YET, T86_BAD = "ok", "not_yet", "bad"
_T86_CODE = "證券代號"
_T86_FOREIGN = "外陸資買賣超股數(不含外資自營商)"
_T86_TRUST = "投信買賣超股數"
# 證交所「這天還沒有資料」的兩種說法（2026-10-17 實測）：查還沒到的日子回「查詢日期大於可查詢最大日期，
# 請重新查詢!」，查非交易日回「很抱歉，沒有符合條件的資料!」。用子字串比對，標點微調不會變成 BAD。
_T86_NOT_YET_STATS = ("沒有符合條件", "大於可查詢最大日期")


def parse_t86(payload, day: date) -> tuple[str, dict[str, int]]:
    """T86 一天：回 (狀態, {代號: 外資＋投信買賣超股數})。

    stat 是上面兩種「還沒有資料」的說法 → T86_NOT_YET（只有 day 是 T 才算還沒發布，見 refresh）。
    其他不是 OK 的 stat、不是 dict、日期不對、欄位找不到 → T86_BAD（stat 原文是遠端文字：只記前 40 字）。
    """
    if not isinstance(payload, dict):
        return T86_BAD, {}
    stat = payload.get("stat")
    if stat != "OK":
        if isinstance(stat, str) and any(word in stat for word in _T86_NOT_YET_STATS):
            return T86_NOT_YET, {}
        logger.info("stock picks t86 unexpected stat=%r", str(stat)[:40])
        return T86_BAD, {}
    if payload.get("date") != day.strftime("%Y%m%d"):
        return T86_BAD, {}
    fields, data = payload.get("fields"), payload.get("data")
    if not isinstance(fields, list) or not isinstance(data, list):
        return T86_BAD, {}
    try:
        i_code, i_foreign, i_trust = fields.index(_T86_CODE), fields.index(_T86_FOREIGN), fields.index(_T86_TRUST)
    except ValueError:
        return T86_BAD, {}
    table: dict[str, int] = {}
    conflicts: set[str] = set()
    width = max(i_code, i_foreign, i_trust)
    for row in data:
        if not isinstance(row, list) or len(row) <= width:
            continue
        code = _clean_code(row[i_code])
        foreign, trust = _num(row[i_foreign]), _num(row[i_trust])
        if code is None or foreign is None or trust is None:
            continue
        _keep_unique(table, conflicts, code, int(foreign) + int(trust))
    for code in conflicts:
        del table[code]
    return (T86_OK, table) if table else (T86_BAD, {})


def parse_chart(payload) -> dict[date, float]:
    """Yahoo chart → {台北日期: 原始收盤}。同一天重複取最後一個非空值；null、非正數丟掉。時區寫死台北。"""
    try:
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return {}
    if not isinstance(result, dict):
        return {}
    stamps = result.get("timestamp") or []
    try:
        closes = result["indicators"]["quote"][0]["close"] or []
    except (KeyError, IndexError, TypeError):
        return {}
    if not isinstance(stamps, list) or not isinstance(closes, list):
        return {}
    bars: dict[date, float] = {}
    for stamp, close in zip(stamps, closes):
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            continue
        if isinstance(close, bool) or not isinstance(close, (int, float)):
            continue
        try:  # 超大整數 math.isfinite 也會丟 OverflowError（2026-10-17 審查）
            if not math.isfinite(stamp) or not math.isfinite(close) or close <= 0:
                continue
            day = datetime.fromtimestamp(stamp, _TPE).date()
        except (OverflowError, OSError, ValueError):
            continue
        bars[day] = float(close)
    return dict(sorted(bars.items()))


# ── Yahoo 還沒有 T 那根時，用證交所的大盤收盤補（2026-10-17） ───────────────────────
# 證交所 OpenAPI 每天 05:20–05:25 就有前一個交易日的資料；Yahoo ^TWII 要到隔天 07:50–09:50 才有 T 那根。
SPLICE_INDEX_TOL = 0.05  # Yahoo 前一根收盤 vs 證交所 T 收盤−漲跌（實測差 ≤ 0.002，是 float 表示法）
SPLICE_INDEX_MAX_MOVE = 0.12  # 漲跌超過前收 12%（台股漲跌幅上限 10%）就不收


def parse_fmtqik(rows) -> dict[date, tuple[float, float]]:
    """OpenAPI FMTQIK（本月每個交易日一列）→ {日期: (加權指數收盤, 漲跌點數)}。

    日期、數字格式不對的列丟掉；同一天出現兩次而且數字不同 → 那天整個不收（不採最後一筆）。
    """
    if not isinstance(rows, list):
        return {}
    table: dict[date, tuple[float, float]] = {}
    conflicts: set[date] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        day, close, change = _roc_date(row.get("Date")), _num(row.get("TAIEX")), _num(row.get("Change"))
        if day is None or close is None or change is None or close <= 0:
            continue
        _keep_unique(table, conflicts, day, (close, change))
    return {day: value for day, value in table.items() if day not in conflicts}


def splice_t_close(
    bars: dict[date, float], index_days: dict[date, tuple[float, float]], t: date
) -> tuple[Optional[float], str, str]:
    """Yahoo 還沒有 T 時，能不能用證交所的 T 收盤補上：回 (T 收盤, "", 細節) 或 (None, 原因, 細節)。

    全部成立才收：
    - 證交所有 T 那一列（index_not_t）；
    - Yahoo 在 T 那個月（同年同月）早於 T 的日子和證交所一模一樣，月初兩邊都是空的（index_gap）；
      近 12 個月實測兩邊的交易日每個月都完全一樣；
    - 不是月初：證交所本月在 T 之前要有交易日（index_month_start）。月初沒有獨立的證據證明 Yahoo 停在前一個
      交易日（只靠數值擋不住「連兩天收盤一樣又漏一天」，上一份快照也可能落後），所以月初照舊等 Yahoo；
    - Yahoo 早於 T 的最後一根收盤 ≈ 證交所 T 收盤−漲跌（差 ≤ SPLICE_INDEX_TOL；index_mismatch）；
    - |漲跌| ≤ 前收 × SPLICE_INDEX_MAX_MOVE（index_jump）。
    細節只有日期與公開的指數數字。
    """
    row = index_days.get(t)
    if row is None:
        return None, "index_not_t", f"t={t.isoformat()}"
    close, change = row
    before = [d for d in bars if d < t]
    yahoo_month = {d for d in before if (d.year, d.month) == (t.year, t.month)}
    twse_month = {d for d in index_days if d < t and (d.year, d.month) == (t.year, t.month)}
    if yahoo_month != twse_month:
        missing = ",".join(d.isoformat() for d in sorted(twse_month - yahoo_month)[:5])
        extra = ",".join(d.isoformat() for d in sorted(yahoo_month - twse_month)[:5])
        return None, "index_gap", f"t={t.isoformat()} yahoo_missing={missing or '-'} yahoo_extra={extra or '-'}"
    if not twse_month:
        return None, "index_month_start", f"t={t.isoformat()}"
    prev = bars[max(before)]
    detail = f"t={t.isoformat()} yahoo_prev={prev:.2f} twse_prev={close - change:.2f}"
    if abs(prev - (close - change)) > SPLICE_INDEX_TOL:
        return None, "index_mismatch", detail
    if abs(change) > prev * SPLICE_INDEX_MAX_MOVE:
        return None, "index_jump", detail
    return close, "", detail


@dataclass(frozen=True)
class OtcRow:
    name: str
    close: float
    change: Optional[float]


OTC_MIN_ROWS = 300  # 櫃買清單少於這個數就當作不完整（實測 4 碼普通股 886 檔）


def parse_otc(rows, t: date) -> Optional[tuple[dict[str, OtcRow], dict[str, str]]]:
    """櫃買每日收盤清單 → ({代號: OtcRow}, {這天沒有收盤的代號: 名稱})；收 4 碼普通股與 00 開頭的 ETF。
    日期要全部等於 T、而且至少 300 檔，否則回 None（不算整輪失敗，之後輪詢補抓）。"""
    if not isinstance(rows, list):
        return None
    table: dict[str, Optional[OtcRow]] = {}
    names: dict[str, str] = {}
    conflicts: set[str] = set()
    dates: set[date] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = _clean_code(row.get("SecuritiesCompanyCode"))
        day = _roc_date(row.get("Date"))
        if code is None or day is None or not _FETCH_CODE_RE.fullmatch(code):
            continue
        dates.add(day)
        name = _clean_name(row.get("CompanyName")) or ""
        close = _num(row.get("Close"))
        value = None if close is None or close <= 0 else OtcRow(name=name, close=close, change=_num(row.get("Change")))
        _keep_unique(table, conflicts, code, value)
        names[code] = name
    if dates != {t}:
        return None
    rows_ok = {code: row for code, row in table.items() if row is not None and code not in conflicts}
    if len(rows_ok) < OTC_MIN_ROWS:
        return None
    no_trade = {code: names[code] for code, row in table.items() if row is None and code not in conflicts}
    return rows_ok, no_trade


def market_value_rank(companies: dict[str, Company], day_rows: dict[str, DayRow]) -> list[tuple[str, float]]:
    """上市普通股市值排序：4 位數、不是 00 開頭、產業別不是空白或 91（存託憑證）、股數為正、
    有收盤。回 [(代號, 市值)]，市值大到小、同值照代號。"""
    rows = []
    for code, company in companies.items():
        if not (len(code) == 4 and code.isdigit()) or code.startswith("00"):
            continue
        if company.industry in ("", "91") or company.shares is None:
            continue
        day = day_rows.get(code)
        if day is None or day.close is None:
            continue
        rows.append((code, day.close * company.shares))
    rows.sort(key=lambda item: (-item[1], item[0]))
    return rows


def industry_medians(companies: dict[str, Company], pe: dict[str, float]) -> dict[str, float]:
    """同產業有效本益比（> 0）至少 5 家才算中位數；產業別空白與 91 不算。"""
    groups: dict[str, list[float]] = {}
    for code, company in companies.items():
        value = pe.get(code)
        if company.industry in ("", "91") or value is None or value <= 0:
            continue
        groups.setdefault(company.industry, []).append(value)
    return {k: statistics.median(v) for k, v in groups.items() if len(v) >= MIN_INDUSTRY_PE}


SPLICE_PREV_GAP = 0.15  # 證交所前一日參考價 vs Yahoo 前一天收盤
SPLICE_T_GAP = 0.01  # Yahoo 也有 T 那根時和證交所收盤的差
SPLICE_JUMP = 0.25  # 窗內相鄰兩天（台股漲跌幅上限 10%）


def lines_up_before_t(bars: dict[date, float], prev_day: date, t: date) -> bool:
    """Yahoo 個股日線在 T 前面接得上日曆：要有 T 的前一個交易日（prev_day）那根；夾在 prev_day 和 T 之間的 Yahoo 日子
    日曆上一定沒有（prev_day 就是日曆裡 T 的前一天），只有「照抄 prev_day 收盤的平盤」可以放過（math.isclose，rel 1e-9）：
    臨時休市日（2026-10-23）^TWII 那天沒有收盤，個股 Yahoo 卻補了一根成交量 0 的平盤。收盤不一樣＝那天真的有成交，
    是日曆少了一個真的交易日。window_closes（逐檔窗口）、_window_unavailable_reason（⚪ 診斷）、_calendar_canary
    （2330 抽查）共用這一條，免得一處放過休市日、另一處又把它當成日曆的洞。"""
    prev_close = bars.get(prev_day)
    if prev_close is None:
        return False
    return all(math.isclose(value, prev_close, rel_tol=1e-9) for day, value in bars.items() if prev_day < day < t)


def window_closes(
    bars: dict[date, float], calendar: Sequence[date], t: date, close: float, prev_reference: Optional[float]
) -> Optional[tuple[list[float], list[float]]]:
    """年線窗、季線窗的收盤：^TWII 日曆上含 T 的最後 240／60 個交易日裡 Yahoo 有的收盤，
    最後接官方（證交所或櫃買）的 T 收盤。接不上、缺太多天 → None（⚪）。

    - Yahoo 要有 T 的前一個交易日那根（停牌、缺資料就不接）；夾在它和 T 之間、日曆上沒有的 Yahoo 日子，
      只有「照抄前一個交易日收盤」可以放過（lines_up_before_t）：臨時休市日（2026-10-23）^TWII 那天沒有收盤、
      不在日曆上，個股 Yahoo 卻補了一根成交量 0 的平盤。日曆真的少了一個交易日時，那天個股不會整批平盤
      → None（整輪 too_many_gray）。
    - Yahoo 也有 T 而且和官方收盤差 > 1%；官方前一日參考價（收盤−漲跌）和 Yahoo 前一天收盤
      差 > 15%（漲跌缺值就不比，只靠下一條）；窗內相鄰兩根差 > 25%（分割、減資沒對齊）→ None。
    - 不在日曆上的 Yahoo 日子不用；缺的日子不拿更早的補。
    """
    days = [d for d in calendar if d <= t][-YEAR_BARS:]
    if len(days) < YEAR_BARS or days[-1] != t or not (math.isfinite(close) and close > 0):
        return None
    prev_day = days[-2]
    if not lines_up_before_t(bars, prev_day, t):
        return None
    on_day = bars.get(t)
    if on_day is not None and abs(on_day / close - 1.0) > SPLICE_T_GAP:
        return None
    if prev_reference is not None and prev_reference > 0 and abs(prev_reference / bars[prev_day] - 1.0) > SPLICE_PREV_GAP:
        return None
    quarter_days = set(days[-QUARTER_BARS:])
    year: list[float] = []
    quarter: list[float] = []
    for day in days[:-1]:
        value = bars.get(day)
        if value is None:
            continue
        year.append(value)
        if day in quarter_days:
            quarter.append(value)
    year.append(close)
    quarter.append(close)
    if len(year) < YEAR_BARS - MAX_MISSING_YEAR or len(quarter) < QUARTER_BARS - MAX_MISSING_QUARTER:
        return None
    if any(abs(b / a - 1.0) > SPLICE_JUMP for a, b in zip(year, year[1:])):
        return None
    return year, quarter


def _window_unavailable_reason(
    bars: dict[date, float], calendar: Sequence[date], t: date, close: float, prev_reference: Optional[float]
) -> str:
    """只供 ⚪ 診斷，不參與窗口計算或改變 window_closes 的回傳。檢查順序照 window_closes；前一個交易日那條用同一個
    lines_up_before_t（休市日的平盤不算前一天缺——AI 段把 previous_day_missing 當「Yahoo 還沒補」等最多 3 小時）。"""
    days = [d for d in calendar if d <= t][-YEAR_BARS:]
    if len(days) < YEAR_BARS or not days or days[-1] != t:
        return "calendar_short"
    if not math.isfinite(close) or close <= 0:
        return "close_invalid"
    if not lines_up_before_t(bars, days[-2], t):
        return "previous_day_missing"
    if t in bars and abs(bars[t] / close - 1.0) > SPLICE_T_GAP:
        return "close_mismatch"
    if prev_reference is not None and prev_reference > 0 and abs(prev_reference / bars[days[-2]] - 1.0) > SPLICE_PREV_GAP:
        return "previous_close_mismatch"
    year = [bars[d] for d in days[:-1] if d in bars] + [close]
    quarter = [bars[d] for d in days[-QUARTER_BARS:-1] if d in bars] + [close]
    if len(year) < YEAR_BARS - MAX_MISSING_YEAR or len(quarter) < QUARTER_BARS - MAX_MISSING_QUARTER:
        return "bars_short"
    if any(abs(b / a - 1.0) > SPLICE_JUMP for a, b in zip(year, year[1:])):
        return "price_jump"
    return "unknown"


# ── 查詢：代號、官方簡稱、別名 ──────────────────────────────────────────────
# 官方簡稱以外的常用叫法（官方簡稱都從證交所資料來，不用手抄；「-KY」可以不打）。
_ALIASES = {"台積": "2330", "台達": "2308", "中華電信": "2412", "日月光": "3711"}
_QUERY_MAX_CHARS = 200
_SPLIT_CHARS = " \t\n,，、/／;；|｜+＋&"
US_SYMBOL, NOT_FOUND, UNSUPPORTED = "us", "not_found", "unsupported"
_LEADING_CODE_RE = re.compile(r"[0-9]{4,6}[A-Z]?")


_NAME_SUFFIXES = ("-KY", "-創")
_QUERY_TRAILING_PUNCT = "?？!！。.,，~～ 　"


def name_index(names: dict[str, str]) -> dict[str, str]:
    """{卡片名稱: 代號} → 查詢用索引：加上去掉「-KY」「-創」的寫法與別名（官方名稱優先）。"""
    index: dict[str, str] = {}
    for name, code in names.items():
        index.setdefault(name.casefold(), code)
    for name, code in names.items():
        for suffix in _NAME_SUFFIXES:
            if name.upper().endswith(suffix) and len(name) > len(suffix):
                index.setdefault(name[: -len(suffix)].casefold(), code)
    for alias, code in _ALIASES.items():
        index.setdefault(alias.casefold(), code)
    return index


def _leading_code(token: str) -> Optional[str]:
    """一段字開頭的代號（「2330」「2330.TW」「2330台積電」）；後面緊接英數字的不算（「12345678」）。"""
    upper = token.upper()
    for suffix in (".TWO", ".TW"):
        if upper.endswith(suffix):
            upper = upper[: -len(suffix)]
            break
    match = _LEADING_CODE_RE.match(upper)
    if match is None:
        return None
    rest = upper[match.end():]
    if rest and rest[0].isascii() and rest[0].isalnum():
        return None
    return match.group(0)


# 名稱後面可以接的字（問句、助詞）：「台積電現在怎樣」「台積電可以買嗎」「台積電的股價」。其他字不接受
# 開頭吻合：「中華精測」的「中華」是中華汽車、「長華科技」的「長華」是別家，爸媽會把別家公司的卡片當成答案。
_NAME_TAILS = (
    "現在", "目前", "今天", "最近", "怎樣", "怎麼", "如何", "可以", "能買", "能不能", "會", "要", "該", "好", "值得", "適合",
    "嗎", "呢", "吧", "喔", "啊", "還", "的", "是", "有", "跟", "和", "與", "這", "股票", "股價", "價", "行情", "表現",
    "狀況", "走勢", "漲", "跌", "買", "賣", "多少", "(", "~", ".", "?", "!", "。",
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",  # 「台積電2330」
)
# 上面每一個字都用 10/08 的上市、上櫃、ETF 約 5,000 個簡稱與全名驗過：沒有任何名稱是「別家名稱＋這些字開頭」。
# 單字「能」不收（「世紀能源」會對到「世紀」）。


def _match_name(folded: str, index: dict[str, str]) -> Optional[str]:
    """整段就是認得的名稱 → 代號；或開頭是名稱、後面接的是問句字 → 代號（最長的名稱先試）；否則 None。
    句尾的標點先去掉（「台積電？」「台積電～」）。"""
    folded = folded.rstrip(_QUERY_TRAILING_PUNCT) or folded
    hit = index.get(folded)
    if hit is not None:
        return hit
    for size in range(min(len(folded) - 1, _FULL_NAME_MAX), 1, -1):
        hit = index.get(folded[:size])
        if hit is not None and folded[size:].startswith(_NAME_TAILS):
            return hit
    return None


def resolve_query(body: str, index: dict[str, str]) -> tuple[str, Optional[str]]:
    """回 (種類, 代號)：種類是 "code"、UNSUPPORTED（像代號但不是普通股或 ETF）、US_SYMBOL、NOT_FOUND。

    先看整段（名稱本身可能有空白：「元大MSCI A股」），再依序看每一段（空白或標點分開），只取第一個
    認得的：開頭是代號 → 用它；是認得的名稱（dict 比對，不用名稱組 regex；見 `_match_name`）→ 用它；
    都不是台股才看美股代號（2026-10-28：`stock_picks_us.find_us_symbol`，回 (US_SYMBOL, "BRK.B") 這種正規寫法；
    美股的切段只用空白與中文標點，「S&P」不會變成 S）。認不得就回 NOT_FOUND，不猜。
    """
    text = unicodedata.normalize("NFKC", body or "")[:_QUERY_MAX_CHARS].strip()
    hit = _match_name(text.casefold(), index)
    if hit is not None:
        return "code", hit
    tokens = [t for t in "".join(" " if ch in _SPLIT_CHARS else ch for ch in text).split(" ") if t]
    saw_us = False
    for token in tokens:
        code = _leading_code(token)
        if code is not None:
            return ("code", code) if _FETCH_CODE_RE.fullmatch(code) else (UNSUPPORTED, None)
        hit = _match_name(token.casefold(), index)
        if hit is not None:
            return "code", hit
        if 1 <= len(token) <= 5 and token.isascii() and token.isalpha():
            saw_us = True
    try:
        import stock_picks_us

        symbol = stock_picks_us.find_us_symbol(text)
    except Exception as exc:  # 美股模組壞了：照舊只認出「是美股」，handle 回舊句
        logger.error("stock picks us symbol parse failed error_type=%s", type(exc).__name__)
        return (US_SYMBOL, None) if saw_us else (NOT_FOUND, None)
    return (US_SYMBOL, symbol) if symbol is not None else (NOT_FOUND, None)


# ── 抓資料（只用 https、固定主機、不轉址、限制解壓後大小、驗 Content-Type、不關 TLS 驗證） ──
_OPENAPI = "openapi.twse.com.tw"
_TWSE = "www.twse.com.tw"
_TPEX = "www.tpex.org.tw"
_YAHOO = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
OPENAPI_MAX_BYTES = 4 * 1024 * 1024
TPEX_MAX_BYTES = 8 * 1024 * 1024
T86_MAX_BYTES = 1024 * 1024
YAHOO_MAX_BYTES = 256 * 1024
BATCH_TIMEOUT_S = 25.0
BATCH_TOTAL_S = 60.0
T86_TIMEOUT_S = 10.0
YAHOO_TIMEOUT_S = 3.0
_YAHOO_SYMBOL_RE = re.compile(r"\^TWII|[0-9]{4}\.TWO?|00[0-9]{2,4}[A-Z]?\.TWO?")
_LAST_MODIFIED_RE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4} \d{2}:\d{2}:\d{2} GMT"
)


class FetchError(Exception):
    """抓資料失敗；只帶種類（timeout／network／status／moved／too_big／not_json／blocked／rate_limited／stopped）。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def _valid_retry(retry_s) -> Optional[float]:
    """retry_s 只收有限的正數秒；其他（NaN、負數、布林、字串）當作沒給。RoundFailed 與 _fail 共用。"""
    if isinstance(retry_s, bool) or not isinstance(retry_s, (int, float)):
        return None
    try:
        value = float(retry_s)
    except OverflowError:
        return None
    return value if math.isfinite(value) and value > 0 else None


class RoundFailed(Exception):
    """這一輪重算失敗（保留舊快照）。重試策略在丟出的地方決定（2026-10-17）：

    - not_yet：資料源還沒有 T（不算失敗、fail_count 不加，固定 NOT_YET_RETRY_S 或 retry_s 後再試）；
    - blocked：疑似被擋（算失敗；Yahoo 429 用 BLOCKED_BACKOFF_S，證交所的另外帶 retry_s）；
    - retry_s：這種情況固定等多久（T86 一家、證交所被擋），不照 BACKOFF_S 的順序。
    預設是真的錯誤；blocked 和 not_yet 都設時當錯誤（blocked 勝）；retry_s 不是有限正數就不用。
    """

    def __init__(self, kind: str, *, blocked: bool = False, not_yet: bool = False, retry_s: Optional[float] = None):
        super().__init__(kind)
        self.kind = kind
        self.blocked = blocked
        self.not_yet = not_yet and not blocked
        self.retry_s = _valid_retry(retry_s)


def _get_json(
    host: str,
    path: str,
    *,
    params: Optional[dict] = None,
    max_bytes: int,
    timeout: float,
    total_s: float,
    if_modified_since: Optional[str] = None,
    stop: Optional[threading.Event] = None,
):
    """回 (狀態碼 200／304, JSON, Last-Modified)；其他情況丟 FetchError。

    TODO（Phase 6 p3 #4，deferred）：總時間與 stop 是收到一段才檢查；帶 Content-Length 的回應若一直
    慢慢滴，最多拖到 requests 的單次讀取 timeout×段數。目前證交所、櫃買、Yahoo 都是 chunked（實測），
    所以先不改；要改就把單檔查詢的抓取放進 daemon 執行緒、用 join(剩下的時間) 等。
    """
    headers = dict(_HEADERS)
    if if_modified_since:
        headers["If-Modified-Since"] = if_modified_since
    give_up = time.monotonic() + total_s
    buf = bytearray()
    try:
        with requests.get(
            f"https://{host}{path}",
            params=params,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        ) as resp:
            status = resp.status_code
            last_modified = resp.headers.get("Last-Modified")
            if status == 304:
                return 304, None, last_modified
            if status == 429:
                raise FetchError("rate_limited")
            if status == 403:
                raise FetchError("blocked")
            if 300 <= status < 400:
                raise FetchError("moved")
            if status == 404:  # 美股單檔用來分出「認不出代號」（2026-10-28）；其他呼叫端照舊當一般失敗
                raise FetchError("not_found")
            if status != 200:
                raise FetchError("status")
            if "json" not in str(resp.headers.get("Content-Type") or "").lower():
                raise FetchError("not_json")
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                buf += chunk
                if len(buf) > max_bytes:
                    raise FetchError("too_big")
                if time.monotonic() > give_up:
                    raise FetchError("timeout")
                if stop is not None and stop.is_set():
                    raise FetchError("stopped")
    except requests.Timeout:
        raise FetchError("timeout") from None
    except requests.RequestException:
        raise FetchError("network") from None
    try:
        return 200, json.loads(bytes(buf)), last_modified
    except (ValueError, RecursionError):  # 巢狀太深的 JSON 會丟 RecursionError（2026-10-17 審查）
        raise FetchError("not_json") from None


def _valid_last_modified(value) -> Optional[str]:
    """只存伺服器給的標準格式（不含 CR／LF），否則下一輪 requests 會一直丟 InvalidHeader。"""
    return value if isinstance(value, str) and _LAST_MODIFIED_RE.fullmatch(value) else None


def _budget_left(give_up: Optional[float], cap: float) -> float:
    """這次請求最多能花幾秒（單次讀取與總時間都用它）：不超過 cap，也不超過整輪剩下的時間（至少 1 秒）。"""
    if give_up is None:
        return cap
    return max(1.0, min(cap, give_up - time.monotonic()))


def _fetch_openapi(
    path: str,
    *,
    stop: Optional[threading.Event],
    if_modified_since: Optional[str] = None,
    give_up: Optional[float] = None,
):
    return _get_json(
        _OPENAPI, path, max_bytes=OPENAPI_MAX_BYTES, timeout=_budget_left(give_up, BATCH_TIMEOUT_S),
        total_s=_budget_left(give_up, BATCH_TOTAL_S), if_modified_since=if_modified_since, stop=stop,
    )


def _fetch_chart(
    symbol: str,
    *,
    hosts: Sequence[str] = _YAHOO,
    timeout: float = YAHOO_TIMEOUT_S,
    stop: Optional[threading.Event] = None,
    shared_block: bool = True,
) -> Optional[dict]:
    """Yahoo 日線 2 年；代號不在白名單、兩個主機都失敗回 None；429 往上丟（整輪中止）。

    shared_block：主流程與 AI 段共用的 429 冷卻（先看有沒有在冷卻、撞到 429 就設）。單檔查詢（_lookup）傳 False：
    它和 2eb85e1 一樣不看也不設這個冷卻（AI 段的封鎖不能讓爸媽查不到單檔，單檔的 429 也不該讓背景多等）。"""
    if not _YAHOO_SYMBOL_RE.fullmatch(symbol):
        return None
    for host in hosts:
        if shared_block and _yahoo_blocked():  # 共用冷卻中（含 AI 段撞到的 429 與重啟前主流程的）：不送請求
            raise FetchError("cooldown")
        try:
            status, data, _ = _get_json(
                host,
                f"/v8/finance/chart/{quote(symbol, safe='')}",
                params={"range": "2y", "interval": "1d", "includePrePost": "false"},
                max_bytes=YAHOO_MAX_BYTES,
                timeout=timeout,
                total_s=timeout * 2,
                stop=stop,
            )
        except FetchError as exc:
            if exc.kind == "rate_limited" and shared_block:
                _block_yahoo()
            if exc.kind in ("rate_limited", "stopped"):
                raise
            logger.info("stock picks yahoo fetch failed host=%s kind=%s", host, exc.kind)
            continue
        if status == 200 and isinstance(data, dict):
            return data
    return None


# ── 快照與工作狀態（state/ 底下兩個 JSON；讀檔一律驗證，不合格當作沒有） ─────────────
SNAPSHOT_SCHEMA = 3  # 快照格式（改了就升；舊快照讀不進來＝當作沒有，背景重算）
WORKER_SCHEMA = 2  # 工作狀態格式（分開升，免得改快照就丟掉 T86 日快取與請求帳）
STATE_DIR = Path(__file__).resolve().parent / "state"
SNAPSHOT_NAME = "stock_picks_tw.json"
WORKER_NAME = "stock_picks_tw_worker.json"
LOCK_NAME = "stock_picks_tw.lock"
STATE_MAX_BYTES = 2 * 1024 * 1024
_MAX_PRICE = 1e6


@dataclass(frozen=True)
class ListedRow:
    """上市股票與 ETF 的全市場資料（單檔查詢股票池以外的上市股時用）。"""

    name: str
    kind: str
    industry: str
    close: float
    change: Optional[float]
    pe: Optional[float]
    revenue: Optional[Revenue]
    inst: Optional[int]


@dataclass(frozen=True)
class Snapshot:
    t: date
    calendar: tuple[date, ...]  # ^TWII 日曆上含 T 的最後 240 個交易日（單檔即時查詢也用）
    computed_at: float
    market: Optional[Market]
    universe: tuple[str, ...]
    picks: tuple[str, ...]
    pool: dict[str, Facts]
    listed: dict[str, ListedRow]
    medians: dict[str, float]
    otc: Optional[dict[str, OtcRow]]
    missed: Optional[tuple[str, int, int]]
    no_trade: dict[str, str] = field(default_factory=dict)  # 這天沒有收盤的上市／上櫃代號 → 名稱
    full_names: dict[str, str] = field(default_factory=dict)  # 上市公司全名（去「股份有限公司」）→ 只拿來比對查詢
    index: dict[str, str] = field(default_factory=dict, compare=False)


def _with_index(snapshot: Snapshot) -> Snapshot:
    """查詢索引：上市簡稱 → 上櫃簡稱 → 沒成交的 → 全名（先放的優先；全名讓「聯華電子」不會對到「聯華」）。"""
    names = {row.name: code for code, row in snapshot.listed.items() if row.name}
    for code, row in (snapshot.otc or {}).items():
        if row.name:
            names.setdefault(row.name, code)
    for code, name in snapshot.no_trade.items():
        if name:
            names.setdefault(name, code)
    for code, full_name in snapshot.full_names.items():  # 含這天沒成交的公司
        if full_name:
            names.setdefault(full_name, code)
    return replace(snapshot, index=name_index(names))


def _require(cond: bool) -> None:  # 狀態檔驗證（和選股的 checks_of 無關）
    if not cond:
        raise ValueError("invalid state value")


def _v_num(value, lo: float, hi: float, *, allow_none: bool = False) -> Optional[float]:
    if value is None and allow_none:
        return None
    _require(isinstance(value, (int, float)) and not isinstance(value, bool))
    number = float(value)
    _require(math.isfinite(number) and lo <= number <= hi)
    return number


def _v_int(value, lo: int, hi: int, *, allow_none: bool = False) -> Optional[int]:
    if value is None and allow_none:
        return None
    _require(isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi)
    return value


def _v_str(value, max_len: int, *, allow_empty: bool = False) -> str:
    _require(isinstance(value, str) and len(value) <= max_len and (allow_empty or value != ""))
    _require(all(ch.isprintable() for ch in value))
    return value


def _v_date(value) -> date:
    _require(isinstance(value, str) and len(value) == 10)
    return date.fromisoformat(value)


def _v_code(value) -> str:
    code = _v_str(value, _CODE_MAX + 1)
    _require(_clean_code(code) == code)
    return code


def _v_name(value, max_len: int = _NAME_MAX) -> str:
    name = _v_str(value, max_len, allow_empty=True)
    _require(name == "" or _clean_name(name, max_len) == name)
    return name


def _revenue_json(rev: Optional[Revenue]):
    return None if rev is None else [rev.year, rev.month, rev.yoy, rev.cum_yoy]


def _revenue_from(value) -> Optional[Revenue]:
    if value is None:
        return None
    _require(isinstance(value, list) and len(value) == 4)
    return Revenue(
        year=_v_int(value[0], 1990, 2200),
        month=_v_int(value[1], 1, 12),
        yoy=_v_num(value[2], -1e7, 1e7, allow_none=True),
        cum_yoy=_v_num(value[3], -1e7, 1e7, allow_none=True),
    )


def _facts_json(f: Facts) -> dict:
    trend = None if f.trend is None else [f.trend.close, f.trend.ma60, f.trend.ma240, f.trend.p60]
    return {
        "c": f.code, "n": f.name, "k": f.kind, "d": f.data_date.isoformat(), "tr": trend,
        "rv": _revenue_json(f.revenue), "pe": f.pe, "pm": f.pe_median, "in": f.inst_net, "cap": f.cap,
        "m": f.market,
    }


def _facts_from(value) -> Facts:
    _require(isinstance(value, dict))
    kind = value.get("k")
    _require(kind in (STOCK, ETF, OTC))
    trend_raw = value.get("tr")
    trend = None
    if trend_raw is not None:
        _require(isinstance(trend_raw, list) and len(trend_raw) == 4)
        trend = Trend(
            close=_v_num(trend_raw[0], 1e-6, _MAX_PRICE),
            ma60=_v_num(trend_raw[1], 1e-6, _MAX_PRICE),
            ma240=_v_num(trend_raw[2], 1e-6, _MAX_PRICE),
            p60=_v_int(trend_raw[3], -10**6, 10**6),
        )
    return Facts(
        code=_v_code(value.get("c")),
        name=_v_name(value.get("n")),
        kind=kind,
        data_date=_v_date(value.get("d")),
        trend=trend,
        revenue=_revenue_from(value.get("rv")),
        pe=_v_num(value.get("pe"), 1e-6, 1e4, allow_none=True),
        pe_median=_v_num(value.get("pm"), 1e-6, 1e4, allow_none=True),
        inst_net=_v_int(value.get("in"), -10**13, 10**13, allow_none=True),
        cap=_v_num(value.get("cap"), 1.0, 1e16, allow_none=True),
        market=_require_in(value.get("m"), (TWSE, TPEX)),
    )


def _require_in(value, allowed: tuple):
    _require(value in allowed)
    return value


def snapshot_to_json(s: Snapshot) -> dict:
    return {
        "schema": SNAPSHOT_SCHEMA,
        "t": s.t.isoformat(),
        "calendar": [d.isoformat() for d in s.calendar],
        "computed_at": s.computed_at,
        "market": None if s.market is None else [s.market.level, s.market.b20, s.market.b60],
        "universe": list(s.universe),
        "picks": list(s.picks),
        "pool": [_facts_json(f) for f in s.pool.values()],
        "listed": {
            code: [r.name, r.kind, r.industry, r.close, r.change, r.pe, _revenue_json(r.revenue), r.inst]
            for code, r in s.listed.items()
        },
        "medians": s.medians,
        "otc": None if s.otc is None else {code: [r.name, r.close, r.change] for code, r in s.otc.items()},
        "missed": None if s.missed is None else list(s.missed),
        "no_trade": s.no_trade,
        "full_names": s.full_names,
    }


def snapshot_from_json(obj) -> Snapshot:
    """不合格就丟 ValueError（呼叫端當作沒有快照）。"""
    _require(isinstance(obj, dict) and obj.get("schema") == SNAPSHOT_SCHEMA)
    market = None
    if obj.get("market") is not None:
        raw = obj["market"]
        _require(isinstance(raw, list) and len(raw) == 3 and raw[0] in (HOT, COLD, NEUTRAL))
        market = Market(level=raw[0], b20=_v_int(raw[1], -10**6, 10**6), b60=_v_int(raw[2], -10**6, 10**6))
    universe, picks = obj.get("universe"), obj.get("picks")
    _require(isinstance(universe, list) and len(universe) <= TOP_N and isinstance(picks, list) and len(picks) <= MAX_PICKS)
    pool_raw, listed_raw, medians_raw = obj.get("pool"), obj.get("listed"), obj.get("medians")
    _require(isinstance(pool_raw, list) and len(pool_raw) <= TOP_N + len(FAQ_CODES))
    _require(isinstance(listed_raw, dict) and len(listed_raw) <= 5000 and isinstance(medians_raw, dict))
    pool = {}
    for item in pool_raw:
        facts = _facts_from(item)
        pool[facts.code] = facts
    listed = {}
    for code, row in listed_raw.items():
        _require(isinstance(row, list) and len(row) == 8 and row[1] in (STOCK, ETF))
        listed[_v_code(code)] = ListedRow(
            name=_v_name(row[0]), kind=row[1], industry=_v_str(row[2], 4, allow_empty=True),
            close=_v_num(row[3], 1e-6, _MAX_PRICE), change=_v_num(row[4], -_MAX_PRICE, _MAX_PRICE, allow_none=True),
            pe=_v_num(row[5], 1e-6, 1e4, allow_none=True), revenue=_revenue_from(row[6]),
            inst=_v_int(row[7], -10**13, 10**13, allow_none=True),
        )
    medians = {_v_str(k, 4): _v_num(v, 1e-6, 1e4) for k, v in medians_raw.items()}
    otc = None
    if obj.get("otc") is not None:
        _require(isinstance(obj["otc"], dict) and len(obj["otc"]) <= 5000)
        otc = {}
        for code, row in obj["otc"].items():
            _require(isinstance(row, list) and len(row) == 3)
            otc[_v_code(code)] = OtcRow(
                name=_v_name(row[0]), close=_v_num(row[1], 1e-6, _MAX_PRICE),
                change=_v_num(row[2], -_MAX_PRICE, _MAX_PRICE, allow_none=True),
            )
    missed = None
    if obj.get("missed") is not None:
        raw = obj["missed"]
        _require(isinstance(raw, list) and len(raw) == 3 and raw[0] in CONDITION_ORDER)
        missed = (raw[0], _v_int(raw[1], 0, 10**4), _v_int(raw[2], 0, 10**4))
    no_trade_raw = obj.get("no_trade")
    _require(isinstance(no_trade_raw, dict) and len(no_trade_raw) <= 5000)
    no_trade = {_v_code(code): _v_name(name) for code, name in no_trade_raw.items()}
    full_names_raw = obj.get("full_names")
    _require(isinstance(full_names_raw, dict) and len(full_names_raw) <= 5000)
    full_names = {_v_code(code): _v_name(name, _FULL_NAME_MAX) for code, name in full_names_raw.items()}
    calendar_raw = obj.get("calendar")
    _require(isinstance(calendar_raw, list) and len(calendar_raw) == YEAR_BARS)
    calendar = tuple(_v_date(d) for d in calendar_raw)
    _require(all(a < b for a, b in zip(calendar, calendar[1:])))
    snapshot = Snapshot(
        t=_v_date(obj.get("t")),
        calendar=calendar,
        computed_at=_v_num(obj.get("computed_at"), 0.0, 1e11),
        market=market,
        universe=tuple(_v_code(c) for c in universe),
        picks=tuple(_v_code(c) for c in picks),
        pool=pool,
        listed=listed,
        medians=medians,
        otc=otc,
        missed=missed,
        no_trade=no_trade,
        full_names=full_names,
    )
    _require(snapshot.calendar[-1] == snapshot.t and all(c in pool for c in snapshot.picks))
    return _with_index(snapshot)


@dataclass
class WorkerState:
    """背景工作的狀態（跟快照分開存；每次變化都原子寫入，重啟後沿用）。"""

    last_ok_check: Optional[float] = None
    seen_t: Optional[date] = None
    seen_at: Optional[float] = None
    last_modified: Optional[str] = None
    fail_count: int = 0
    next_try: Optional[float] = None
    last_fail: str = ""
    # 沒有快照時回「暫時抓不到」還是「準備中」：重啟後快照檔不見、工作狀態還留著舊的 fail_count 時，
    # fail_count > 0 不代表這次的第一輪失敗過，所以另外記。
    first_round_failed: bool = False
    t86: dict = field(default_factory=dict)  # {"YYYY-MM-DD": {代號: 外資＋投信買賣超股數}}，只存 stat=OK 的日子
    t86_requests: list = field(default_factory=list)  # 送出 T86 請求的時間（跨重啟記帳）
    writer: str = ""  # 最後寫這個檔的程序（合併時分得出是不是別的程序的冷卻）
    # 正在等哪個資料源（「還沒出來」的種類，2026-10-17）；不是失敗，成功、304、追上、真的失敗時清掉。
    last_wait: str = ""
    # 主流程撞到 Yahoo 429 的冷卻結束時間（真實時間，2026-10-28 計畫 C）。None＝舊程式寫的檔（沒有這個欄位）。
    # AI 段的 429 只放記憶體，不寫這裡（AI 失敗不改主 worker）。
    yahoo_blocked_until: Optional[float] = None


def _lenient_wait(value) -> str:
    """讀檔時的 last_wait：格式不對就當沒有，絕不丟例外（丟了整個檔案會被當壞檔，T86 請求帳就歸零）。"""
    return value[:40] if isinstance(value, str) and value.isprintable() else ""


def _lenient_until(value, limit: float) -> Optional[float]:
    """讀檔時的 yahoo_blocked_until：不是有限的非負數字就當沒有（絕不丟例外，理由同上）；
    比現在晚太多的夾回 limit，不然合併取最大值後會一直黏住。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return min(float(value), limit)


def worker_state_to_json(w: WorkerState) -> dict:
    return {
        "schema": WORKER_SCHEMA,
        "last_ok_check": w.last_ok_check,
        "seen_t": None if w.seen_t is None else w.seen_t.isoformat(),
        "seen_at": w.seen_at,
        "last_modified": w.last_modified,
        "fail_count": w.fail_count,
        "next_try": w.next_try,
        "last_fail": w.last_fail,
        "first_round_failed": w.first_round_failed,
        "t86": w.t86,
        "t86_requests": w.t86_requests,
        "writer": w.writer,
        "last_wait": w.last_wait,
        "yahoo_blocked_until": w.yahoo_blocked_until,
    }


def worker_state_from_json(obj) -> WorkerState:
    _require(isinstance(obj, dict) and obj.get("schema") == WORKER_SCHEMA)
    t86_raw, requests_raw = obj.get("t86"), obj.get("t86_requests")
    _require(isinstance(t86_raw, dict) and len(t86_raw) <= T86_KEEP_DAYS and isinstance(requests_raw, list))
    t86 = {}
    for day, table in t86_raw.items():
        _v_date(day)
        _require(isinstance(table, dict) and len(table) <= 5000)
        t86[day] = {_v_code(code): _v_int(net, -10**13, 10**13) for code, net in table.items()}
    _require(len(requests_raw) <= 1000)
    seen_t = obj.get("seen_t")
    last_fail = obj.get("last_fail")
    # 時鐘跳快過、或檔案被寫進未來的時間：夾回合理範圍（不然合併取最大值後會一直黏住）。
    now = time.time()
    soon = now + _CLOCK_SKEW_S
    seen_day = None if seen_t is None else _v_date(seen_t)
    if seen_day is not None and seen_day > _taipei_today():
        seen_day = None
    return WorkerState(
        last_ok_check=_at_most(_v_num(obj.get("last_ok_check"), 0.0, 1e11, allow_none=True), soon),
        seen_t=seen_day,
        seen_at=None if seen_day is None else _at_most(_v_num(obj.get("seen_at"), 0.0, 1e11, allow_none=True), soon),
        last_modified=_valid_last_modified(obj.get("last_modified")),
        fail_count=_v_int(obj.get("fail_count"), 0, 10**6),
        next_try=_at_most(_v_num(obj.get("next_try"), 0.0, 1e11, allow_none=True), now + _MAX_BACKOFF_S + _CLOCK_SKEW_S),
        last_fail=_v_str(last_fail, 40, allow_empty=True) if last_fail is not None else "",
        first_round_failed=obj.get("first_round_failed") is True,
        t86=t86,
        t86_requests=[min(x, now) for x in (_v_num(x, 0.0, 1e11) for x in requests_raw)],  # 夾回、不丟（帳不能變少）
        writer=_v_str(obj.get("writer") or "", 64, allow_empty=True),
        last_wait=_lenient_wait(obj.get("last_wait")),
        yahoo_blocked_until=_lenient_until(obj.get("yahoo_blocked_until"), now + BLOCKED_BACKOFF_S + _CLOCK_SKEW_S),
    )


def _at_most(value: Optional[float], limit: float) -> Optional[float]:
    return None if value is None else min(value, limit)


def _taipei_today() -> date:
    return datetime.now(_TPE).date()


def _read_json_file(path: Path):
    """不跟隨 symlink、只讀一般檔、限大小；檔案不存在回 None，其他問題丟例外。"""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > STATE_MAX_BYTES:
            raise ValueError("state file is not a small regular file")
        data = fh.read(STATE_MAX_BYTES + 1)
    if len(data) > STATE_MAX_BYTES:
        raise ValueError("state file too large")
    return json.loads(data)


def _write_json_file(path: Path, payload: dict) -> None:
    """同目錄唯一暫存檔（O_EXCL｜O_NOFOLLOW、0600）→ fsync → os.replace。"""
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > STATE_MAX_BYTES:
        raise ValueError("state payload too large")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _state_file_lock(timeout_s: float = 5.0):
    """跨程序鎖（部署時新舊程序可能重疊）：拿不到就丟 TimeoutError，這次不寫。"""
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(STATE_DIR / LOCK_NAME, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        give_up = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > give_up:
                    raise TimeoutError("state lock busy") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def load_snapshot_file() -> Optional[Snapshot]:
    try:
        obj = _read_json_file(STATE_DIR / SNAPSHOT_NAME)
        return None if obj is None else snapshot_from_json(obj)
    except Exception as exc:
        logger.error("stock picks snapshot unreadable error_type=%s", type(exc).__name__)
        return None


def _read_worker_quiet() -> Optional[WorkerState]:
    try:
        obj = _read_json_file(STATE_DIR / WORKER_NAME)
        return None if obj is None else worker_state_from_json(obj)
    except Exception:
        return None


def load_worker_file() -> WorkerState:
    """工作狀態壞掉時重來，但先退避 1 小時（T86 請求帳不能因為壞檔就恢復滿額）。"""
    try:
        obj = _read_json_file(STATE_DIR / WORKER_NAME)
        return WorkerState() if obj is None else worker_state_from_json(obj)
    except Exception as exc:
        logger.error("stock picks worker state unreadable error_type=%s", type(exc).__name__)
        return WorkerState(next_try=time.time() + STATE_CORRUPT_BACKOFF_S, last_fail="state_corrupt")


# ── 執行期狀態（記憶體裡的快照是不可變物件，整個換掉） ──────────────────────────
_lock = threading.RLock()
_snapshot: Optional[Snapshot] = None
_worker = WorkerState()
_loaded = False
_thread: Optional[threading.Thread] = None
_stop: Optional[threading.Event] = None
_wake: Optional[threading.Event] = None
_BACKGROUND_ENABLED = True  # conftest 會關掉（測試不能起真的背景工作）
_yahoo_blocked_until = 0.0  # 真實時間；主流程與 AI 段共用，重啟時也看持久化的工作狀態
_yahoo_block_lock = threading.Lock()  # 只保護上面這個數字，不和別的鎖或網路請求同時持有（回覆也會走到 429 的路徑）


def _yahoo_block_end() -> float:
    """共用冷卻結束的時間（真實時間）。來源：記憶體（這個程序撞到的 429，含 AI 段）、worker 檔的
    yahoo_blocked_until（主流程的 429，重啟後還在）。舊程式寫的檔沒有這個欄位（None）：只有「最後失敗是 429、
    而且現在不是在等資料源」時才用 next_try 推——等資料源（not_yet）也會把 next_try 往後推，不是 429 的冷卻。"""
    w = _worker
    end = max(_yahoo_blocked_until, w.yahoo_blocked_until or 0.0)
    if w.yahoo_blocked_until is None and w.last_fail == "yahoo_rate_limited" and not w.last_wait:
        end = max(end, w.next_try or 0.0)
    return end


def _yahoo_blocked(now: Optional[float] = None) -> bool:
    """共用冷卻中嗎。LINE_BOT_STOCK_PICKS_AI=0 時一律否（回到 2eb85e1：沒有共用冷卻）。"""
    if not AI_ENABLED:
        return False
    now = time.time() if now is None else now
    return _yahoo_block_end() > now


def _block_yahoo() -> None:
    """任何 Yahoo 429（主流程、AI 段）都先封鎖共用入口；AI 失敗不改主 worker 的失敗次數。單檔查詢不呼叫這個。"""
    global _yahoo_blocked_until
    if not AI_ENABLED:
        return
    until = time.time() + BLOCKED_BACKOFF_S
    with _yahoo_block_lock:  # 不拿 _lock：單檔查詢的回覆撞到 429 時不能被背景拿著 _lock 等檔案鎖卡住
        _yahoo_blocked_until = max(_yahoo_blocked_until, until)


def _ensure_loaded() -> None:
    """第一次用到時才讀狀態檔。讀過就不再拿鎖：背景拿著鎖等檔案鎖時，回覆不能被卡住。"""
    global _snapshot, _worker, _loaded, _ai_state, _ai_next_try, _ai_holdings, _ai_fetch_budget
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        _snapshot = load_snapshot_file()
        _worker = load_worker_file()
        _ai_state = load_ai_file() if AI_ENABLED else None
        if _ai_state:
            _ai_next_try = _ai_state["last_try"] + AI_RETRY_S if _ai_state["fail_count"] else 0.0
            _ai_holdings = _ai_state.get("holdings")
            _ai_fetch_budget = dict(_ai_state.get("fetch_budget", {}))
        if _worker.last_fail == "state_corrupt":
            _save_worker()  # 壞檔順手修好（退避一起存）；不然到期後又讀到壞檔、再退避一次
        _sweep_stale_tmp()
        _loaded = True


def _sweep_stale_tmp(max_age_s: float = 3600.0) -> None:
    """寫檔途中程序被殺會留下暫存檔；超過 1 小時的清掉（只動自己的檔名）。"""
    try:
        cutoff = time.time() - max_age_s
        for path in STATE_DIR.glob(".stock_picks_tw*.tmp"):
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                path.unlink()
    except OSError as exc:
        logger.info("stock picks tmp sweep skipped error_type=%s", type(exc).__name__)


_WRITER_ID = f"{os.getpid()}-{time.time_ns()}"


def _merge_worker_from_disk() -> bool:  # 回 True＝磁碟上的檔壞了
    """呼叫端持有 _lock 與檔案鎖：把磁碟上（可能是別的程序寫的）狀態併進來。

    T86 請求時間與日快取取聯集（只留 24 小時與 12 天）；別的程序寫的退避期限、最後成功檢查
    時間取較晚的；看到的新日期取較新的（同一天取較早看到的時間）。自己上次寫的不併（不然清掉
    的退避又會被讀回來）。檔案在但壞掉 → 先退避 1 小時、回 True（呼叫端不能預扣 T86）。"""
    path = STATE_DIR / WORKER_NAME
    try:
        obj = _read_json_file(path)
        disk = None if obj is None else worker_state_from_json(obj)
    except Exception:
        _worker.next_try = max(_worker.next_try or 0.0, time.time() + STATE_CORRUPT_BACKOFF_S)
        _worker.last_fail = "state_corrupt"
        return True
    if disk is None:
        return False
    now = time.time()
    _worker.t86_requests = sorted({x for x in _worker.t86_requests + disk.t86_requests if now - x < 86400})
    merged = dict(disk.t86)
    merged.update(_worker.t86)
    _worker.t86 = {day: merged[day] for day in sorted(merged)[-T86_KEEP_DAYS:]}
    if disk.writer and disk.writer != _WRITER_ID:
        if disk.next_try is not None and disk.next_try > now:
            _worker.next_try = max(_worker.next_try or 0.0, disk.next_try)
        if disk.yahoo_blocked_until is not None and disk.yahoo_blocked_until > now:
            _worker.yahoo_blocked_until = max(_worker.yahoo_blocked_until or 0.0, disk.yahoo_blocked_until)
        if disk.last_ok_check is not None:
            _worker.last_ok_check = max(_worker.last_ok_check or 0.0, disk.last_ok_check)
        if disk.seen_t is not None and (_worker.seen_t is None or disk.seen_t > _worker.seen_t):
            _worker.seen_t, _worker.seen_at = disk.seen_t, disk.seen_at
        elif disk.seen_t is not None and disk.seen_t == _worker.seen_t and disk.seen_at is not None:
            _worker.seen_at = min(_worker.seen_at or disk.seen_at, disk.seen_at)
    return False


def _write_worker_locked() -> None:
    """呼叫端持有 _lock 與檔案鎖：標上這個程序再寫（合併時才分得出是不是別的程序的冷卻）。"""
    _worker.writer = _WRITER_ID
    _write_json_file(STATE_DIR / WORKER_NAME, worker_state_to_json(_worker))


def _save_worker() -> None:
    """呼叫端持有 _lock。在檔案鎖裡先合併磁碟上的狀態再寫（壞掉的檔會被修好，退避一起存）；
    失敗只記種類（狀態留在記憶體，下次再寫）。"""
    try:
        with _state_file_lock():
            _merge_worker_from_disk()
            _write_worker_locked()
    except Exception as exc:
        logger.error("stock picks worker state write failed error_type=%s", type(exc).__name__)


COMMIT_WRITTEN, COMMIT_DISK_NEWER, COMMIT_FAILED = "written", "disk_newer", "failed"


def _commit_snapshot(new: Snapshot) -> str:
    """寫快照再換記憶體（兩邊一致）。磁碟上的 T 比新的還新 → 不寫、改用磁碟上的；T 相同照寫；
    寫不進去 → 不換、回 COMMIT_FAILED（呼叫端當這輪失敗、退避後重來）。"""
    global _snapshot
    with _lock:
        try:
            with _state_file_lock():
                disk = load_snapshot_file()
                if disk is not None and disk.t > new.t:
                    logger.warning("stock picks kept newer snapshot from disk")
                    _snapshot = disk
                    return COMMIT_DISK_NEWER
                payload = snapshot_to_json(new)
                snapshot_from_json(json.loads(json.dumps(payload)))  # 寫得出去的就讀得回來（同一套驗證）
                _write_json_file(STATE_DIR / SNAPSHOT_NAME, payload)
        except Exception as exc:
            logger.error("stock picks snapshot write failed error_type=%s", type(exc).__name__)
            return COMMIT_FAILED
        _snapshot = new
        return COMMIT_WRITTEN


# ── 推薦紀錄（Andrew 2026-10-10：從上線那天開始記，第 3 段算推薦成績用；只存公開數字） ────────
HISTORY_NAME = "stock_picks_history.jsonl"
HISTORY_MAX_BYTES = 5 * 1024 * 1024  # 一天一行約 300 字（AI 紀錄同一天重算換推薦時多幾行），夠用幾十年


def _history_last(path: Path) -> tuple[Optional[dict], bool]:
    """(最後一筆讀得懂的紀錄, 檔尾是不是換行)。只讀最後 4 KB；最後一行寫到一半就往前看；
    t 要是合法日期（被改壞的 "9999" 不算，免得 `>=` 從此擋住所有寫入）。沒有檔回 (None, True)。"""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None, True
    with os.fdopen(fd, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        fh.seek(max(0, size - 4096))
        tail = fh.read()
    ends_with_newline = not tail or tail.endswith(b"\n")
    for raw in reversed(tail.decode("utf-8", "replace").strip().splitlines()):
        try:
            record = json.loads(raw)
            day = date.fromisoformat(record.get("t"))
        except (ValueError, AttributeError, TypeError):
            continue
        if day > _taipei_today():  # 被改成未來日期（"9999-12-31"）也不算，免得從此不再記
            continue
        return record, ends_with_newline
    return None, ends_with_newline


def _history_rev(record: dict) -> int:
    """一行紀錄的版次：沒有 rev（rev 出現之前寫的）或不是正整數都算第 1 版。"""
    rev = record.get("rev")
    return rev if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 1 else 1


def _append_history(
    snapshot: Optional[Snapshot], *, name: str = HISTORY_NAME, record: Optional[dict] = None, revise: bool = False
) -> None:
    """一個交易日一行：T、大盤、推薦的代號／名稱／收盤／分數／季線乖離（沒有推薦也記一行）。
    日期只往前：最後一行的 T 已經 ≥ 這個 T 就不寫（重啟、同 T 重寫快照、別的程序先寫了）；
    revise（只有 AI 推薦紀錄用）：同一個 T 重算（00952 晚到、補上跳過的候選）而且 picks 和該 T 最後一行不同時，
    再追加一行、rev 加 1（第一行 rev=1，沒有 rev 的舊行算 1）；picks 一樣就不寫。讀取端同一個 t 取 rev 最大的那行。
    寫不進去只記錯誤種類。"""
    path = STATE_DIR / name
    record = record if record is not None else {
        "t": snapshot.t.isoformat(),
        "market": None if snapshot.market is None else snapshot.market.level,
        "picks": [
            {
                "code": f.code,
                "name": f.name,
                "close": f.trend.close if f.trend else None,
                "score": score_of(f),
                "p60": f.trend.p60 if f.trend else None,
            }
            for f in (snapshot.pool[c] for c in snapshot.picks if c in snapshot.pool)
        ],
        "computed_at": round(snapshot.computed_at),
    }
    try:
        with _state_file_lock():
            last, ends_with_newline = _history_last(path)
            last_t = None if last is None else last["t"]
            if revise:
                if last_t is not None and last_t > record["t"]:
                    return
                rev = 1
                if last_t == record["t"]:
                    if last.get("picks") == json.loads(json.dumps(record["picks"])):  # 和讀回來的同一種型別才比
                        return
                    rev = _history_rev(last) + 1
                record = {"t": record["t"], "rev": rev, **{k: v for k, v in record.items() if k != "t"}}
            elif last_t is not None and last_t >= record["t"]:
                return
            line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            try:
                if os.lstat(path).st_size + len(line) > HISTORY_MAX_BYTES:
                    logger.warning("stock picks history is full; not appended")
                    return
            except FileNotFoundError:
                pass
            if not ends_with_newline:  # 上一行寫到一半就當機：先補換行，不黏在後面
                line = b"\n" + line
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "ab") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
    except Exception as exc:
        logger.error("stock picks history append failed error_type=%s", type(exc).__name__)


# ── 重算一輪 ───────────────────────────────────────────────────────────────
REFRESH_BUDGET_S = 180.0  # T86 每天間隔 3 秒，首輪 10 天就要 30 秒以上
TPEX_MIN_LEFT_S = 20.0  # 整輪剩不到 20 秒就不抓櫃買（之後輪詢補抓）
YAHOO_WORKERS = 4
MAX_GRAY_RATIO = 0.2
T86_DAYS = 10
T86_KEEP_DAYS = 12
T86_DAILY_CAP = 15
T86_SPACING_S = 3.0
_MIN_ROWS = 500


def _reserve_t86_request(stop: threading.Event, give_up: float) -> None:
    """送出 T86 前：在跨程序鎖裡讀磁碟→合併→檢查退避、24 小時 15 次與 3 秒間隔→預扣並寫回。
    寫不進去、狀態檔壞掉、要等的時間超過整輪期限 → 不送（記不了帳的請求不能打）。"""
    while True:
        with _lock:
            try:
                with _state_file_lock():
                    corrupt = _merge_worker_from_disk()
                    now = time.time()
                    if corrupt:
                        _write_worker_locked()  # 修好壞檔，1 小時退避一起存
                        raise RoundFailed("state_corrupt")
                    if _worker.next_try is not None and _worker.next_try > now:
                        raise RoundFailed("t86_cooldown", retry_s=T86_RETRY_S)  # 別的程序還在退避（併進來的期限）
                    recent = [x for x in _worker.t86_requests if now - x < 86400]
                    if len(recent) >= T86_DAILY_CAP:
                        raise RoundFailed("t86_budget", retry_s=T86_RETRY_S)
                    wait = (max(recent) + T86_SPACING_S - now) if recent else 0.0
                    if wait <= 0:
                        _worker.t86_requests = recent + [now]
                        _write_worker_locked()
                        return
            except RoundFailed:
                raise
            except Exception as exc:
                raise RoundFailed(f"state_{type(exc).__name__}"[:40]) from None
        if time.monotonic() + wait > give_up:
            raise RoundFailed("budget_t86", retry_s=T86_RETRY_S)
        if stop.wait(wait):
            raise RoundFailed("stopped")


def _t86_day(day: date, stop: threading.Event, give_up: float) -> dict[str, int]:
    """抓一天 T86（日快取有就用）。

    T86 每 24 小時最多 15 次、送出前就記帳，所以不管還沒出來、錯誤還是被擋（非 JSON、403、429、轉址），
    一律每小時再試（T86_RETRY_S；Andrew 2026-10-17 選「改每小時」）。還沒出來只有 day 是 T 才算，見 refresh。"""
    key = day.isoformat()
    with _lock:
        cached = _worker.t86.get(key)
    if cached is not None:
        return cached
    _reserve_t86_request(stop, give_up)
    try:
        status, payload, _ = _get_json(
            _TWSE, "/rwd/zh/fund/T86",
            params={"date": day.strftime("%Y%m%d"), "selectType": "ALLBUT0999", "response": "json"},
            max_bytes=T86_MAX_BYTES, timeout=_budget_left(give_up, T86_TIMEOUT_S),
            total_s=_budget_left(give_up, T86_TIMEOUT_S * 2), stop=stop,
        )
    except FetchError as exc:
        if exc.kind in ("not_json", "blocked", "rate_limited", "moved"):
            raise RoundFailed(f"t86_{exc.kind}", blocked=True, retry_s=T86_RETRY_S) from None
        raise RoundFailed(f"t86_{exc.kind}", retry_s=T86_RETRY_S) from None
    kind, table = parse_t86(payload, day) if status == 200 else (T86_BAD, {})
    if kind == T86_NOT_YET:
        raise RoundFailed("t86_not_yet", not_yet=True, retry_s=T86_RETRY_S)
    if kind != T86_OK:
        raise RoundFailed(f"t86_{kind}", retry_s=T86_RETRY_S)
    with _lock:
        _worker.t86[key] = table
        for old in sorted(_worker.t86)[:-T86_KEEP_DAYS]:
            del _worker.t86[old]
        _save_worker()
    return table


def _fetch_charts(symbols: Sequence[str], stop: threading.Event, give_up: float) -> dict[str, Optional[dict]]:
    """背景用 4 條 daemon 執行緒抓日線（不用 ThreadPoolExecutor：程式結束時會等它跑完）。"""
    pending = list(symbols)
    results: dict[str, Optional[dict]] = {}
    errors: list[str] = []
    guard = threading.Lock()

    def run() -> None:
        while True:
            with guard:
                if not pending or errors:
                    return
                symbol = pending.pop(0)
            if stop.is_set() or time.monotonic() > give_up:
                return
            try:
                data = _fetch_chart(symbol, stop=stop)
            except FetchError as exc:
                with guard:
                    errors.append(exc.kind)
                return
            with guard:
                results[symbol] = data

    workers = [threading.Thread(target=run, daemon=True, name=f"stock-picks-yahoo-{i}") for i in range(YAHOO_WORKERS)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=max(0.0, give_up - time.monotonic()) + 1.0)
    with guard:
        if errors:
            # 同一批其他執行緒可能先看見冷卻；真正的 429 仍按原本的 20 分鐘退避。旗標關掉時沒有冷卻，照 2eb85e1 取第一個。
            kind = "rate_limited" if AI_ENABLED and "rate_limited" in errors else errors[0]
            raise RoundFailed(f"yahoo_{kind}", blocked=kind == "rate_limited", not_yet=kind == "cooldown")
        return dict(results)


def _symbol(code: str, market: str) -> str:
    return f"{code}.TWO" if market == TPEX else f"{code}.TW"


def _listed_kind(code: str) -> str:
    return ETF if code.startswith("00") else STOCK


def _prev_reference(close: float, change: Optional[float]) -> Optional[float]:
    if change is None:
        return None
    ref = close - change
    return ref if ref > 0 else None


def _fetch_otc(
    t: date, stop: Optional[threading.Event], give_up: Optional[float] = None
) -> Optional[tuple[dict[str, OtcRow], dict[str, str]]]:
    """抓櫃買每日收盤清單再交給 parse_otc；抓不到回 None（不算整輪失敗）。"""
    try:
        status, rows, _ = _get_json(
            _TPEX, "/openapi/v1/tpex_mainboard_daily_close_quotes",
            max_bytes=TPEX_MAX_BYTES, timeout=_budget_left(give_up, BATCH_TIMEOUT_S),
            total_s=_budget_left(give_up, BATCH_TOTAL_S), stop=stop,
        )
    except FetchError as exc:
        logger.info("stock picks tpex fetch failed kind=%s", exc.kind)
        return None
    parsed = parse_otc(rows, t) if status == 200 else None
    if parsed is None:
        logger.info("stock picks tpex list not usable for t=%s", t.isoformat())
    return parsed


FMTQIK_MAX_BYTES = 64 * 1024  # 本月每個交易日一列，實際約 2 KB
FMTQIK_TIMEOUT_S = 10.0


def _twse_t_close(bars: dict[date, float], t: date, stop: threading.Event, give_up: float) -> float:
    """Yahoo ^TWII 還沒有 T 時，用證交所 OpenAPI FMTQIK 的 T 收盤補上（2026-10-17）。

    證交所只是加速：抓不到、資料對不上都算「還沒出來」（照舊等 Yahoo），不能比沒有它更糟；被擋 → 等 1 小時。
    每次接受、拒絕都記一行 info（只有日期與公開的指數數字），之後查得到加速有沒有作用。"""
    try:
        _, rows, _ = _get_json(
            _OPENAPI, "/v1/exchangeReport/FMTQIK", max_bytes=FMTQIK_MAX_BYTES,
            timeout=_budget_left(give_up, FMTQIK_TIMEOUT_S), total_s=_budget_left(give_up, FMTQIK_TIMEOUT_S), stop=stop,
        )
    except FetchError as exc:
        if exc.kind == "stopped":
            raise RoundFailed("stopped") from None
        if exc.kind in ("blocked", "rate_limited"):
            raise RoundFailed(f"index_{exc.kind}", blocked=True, retry_s=TWSE_BLOCKED_RETRY_S) from None
        logger.info("stock picks twse index unavailable kind=%s", exc.kind)
        raise RoundFailed(f"index_{exc.kind}", not_yet=True) from None
    close, why, detail = splice_t_close(bars, parse_fmtqik(rows), t)
    if close is None:
        logger.info("stock picks twse index not used reason=%s %s", why, detail)
        raise RoundFailed(why, not_yet=True)
    logger.info("stock picks used twse index %s", detail)
    return close


def _calendar_canary(twii: dict, calendar: Sequence[date], t: date, stop: threading.Event) -> dict:
    """抽查 2330 的日線：要接得上日曆的前一個交易日（Yahoo 內部日線不一致時，窗口會悄悄少一根）。規則和逐檔窗口
    window_closes 同一條（lines_up_before_t）：有前一個交易日那根；夾在它和 T 之間、日曆沒有的日子只能是休市日照抄的
    平盤（臨時休市後第一個交易日不能卡在這裡），收盤不一樣＝日曆少了真的交易日。
    抽查沒做成（抓不到、429、冷卻、停止）或日曆有洞都是「還沒出來」（not_yet），不算主流程失敗；成功回 2330 的日線，
    主池抓取直接重用。"""
    try:
        canary = _fetch_chart("2330.TW", stop=stop)
    except FetchError as exc:
        if exc.kind == "stopped":
            raise RoundFailed("stopped") from None
        if exc.kind == "rate_limited":
            _block_yahoo()
        logger.info("stock picks calendar canary reason=%s", exc.kind)
        raise RoundFailed(
            f"canary_{exc.kind}", not_yet=True,
            retry_s=BLOCKED_BACKOFF_S if exc.kind == "rate_limited" else None,
        ) from None
    if canary is None:
        logger.info("stock picks calendar canary reason=unavailable")
        raise RoundFailed("canary_unavailable", not_yet=True)
    bars = parse_chart(canary)
    before = [d for d in bars if d < t]
    canary_prev = max(before) if before else None  # 只進 log：休市日的平盤放過時，這天會比 expected_prev 晚
    aligned = lines_up_before_t(bars, calendar[-2], t)
    logger.info(
        "stock picks calendar canary index_last=%s expected_prev=%s canary_prev=%s result=%s",
        max(parse_chart(twii), default=None), calendar[-2], canary_prev, "ok" if aligned else "calendar_hole",
    )
    if not aligned:
        raise RoundFailed("calendar_hole", not_yet=True)
    return canary


def refresh(t: date, day_rows: dict[str, DayRow], stop: threading.Event, *, now: Optional[float] = None) -> Snapshot:
    """用資料日 T 整輪重算；任何來源不齊、超過整輪期限就丟 RoundFailed（保留舊快照）。"""
    give_up = time.monotonic() + REFRESH_BUDGET_S

    def check_budget(stage: str, retry_s: Optional[float] = None) -> None:
        if stop.is_set():
            raise RoundFailed("stopped")
        if time.monotonic() > give_up:
            raise RoundFailed(f"budget_{stage}", retry_s=retry_s)

    if _yahoo_blocked():
        raise RoundFailed("yahoo_cooldown", not_yet=True)
    # 1) 交易日曆＝加權指數日線（先截到 ≤ T，最後一根必須是 T）
    try:
        twii = _fetch_chart("^TWII", stop=stop)
    except FetchError as exc:
        raise RoundFailed(
            f"yahoo_{exc.kind}", blocked=exc.kind == "rate_limited", not_yet=exc.kind == "cooldown"
        ) from None
    if twii is None:
        raise RoundFailed("twii_unavailable")  # 兩個主機都失敗：真的錯誤，不是「還沒出來」
    twii_bars = {d: v for d, v in parse_chart(twii).items() if d <= t}
    calendar = sorted(twii_bars)
    if not calendar:
        raise RoundFailed("twii_bad")
    if calendar[-1] != t:
        # Yahoo 還沒有 T 那根（要到隔天 07:50–09:50）：用證交所的 T 收盤補上（2026-10-17；補不了就是「還沒出來」）
        if not any((d.year, d.month) == (t.year, t.month) for d in twii_bars):
            # 月初（Yahoo 在 T 這個月還沒有日線）：splice_t_close 一定不收，不用每 15 分鐘去抓 FMTQIK
            logger.info("stock picks twse index not used reason=index_month_start t=%s", t.isoformat())
            raise RoundFailed("index_month_start", not_yet=True)
        check_budget("index")
        twii_bars[t] = _twse_t_close(twii_bars, t, stop, give_up)
        calendar.append(t)
    if len(calendar) < YEAR_BARS:
        raise RoundFailed("twii_short")
    # Yahoo 內部抽查：只比 T 前最後一根，不加抓 FMTQIK，也不改窗口的接法。LINE_BOT_STOCK_PICKS_AI=0 時整段不做（回到 2eb85e1）。
    canary = None
    if AI_ENABLED:
        check_budget("canary")
        canary = _calendar_canary(twii, calendar, t, stop)
    # 2) 證交所批次
    try:
        check_budget("batch")
        _, company_rows, _ = _fetch_openapi("/v1/opendata/t187ap03_L", stop=stop, give_up=give_up)
        check_budget("batch")
        _, pe_rows, _ = _fetch_openapi("/v1/exchangeReport/BWIBBU_ALL", stop=stop, give_up=give_up)
        check_budget("batch")
        _, revenue_rows, _ = _fetch_openapi("/v1/opendata/t187ap05_L", stop=stop, give_up=give_up)
    except FetchError as exc:
        blocked = exc.kind in ("blocked", "rate_limited")
        raise RoundFailed(
            f"batch_{exc.kind}", blocked=blocked, retry_s=TWSE_BLOCKED_RETRY_S if blocked else None
        ) from None
    companies = parse_companies(company_rows)
    pe_date, pe = parse_pe(pe_rows)
    revenue = parse_revenue(revenue_rows)
    if len(companies) < _MIN_ROWS or len(revenue) < _MIN_ROWS:
        raise RoundFailed("batch_short")
    if pe_date != t:
        if pe_date is not None and pe_date < t:
            raise RoundFailed("pe_not_t", not_yet=True)  # 本益比還停在前一天：還沒出來
        raise RoundFailed("pe_bad")  # 日期不唯一、或比 T 還新：真的錯誤
    # 3) 外資＋投信：^TWII 日曆上含 T 的最後 10 個交易日，每天都要有；某一檔缺任何一天 → 那檔未知
    inst: dict[str, int] = {}
    days_seen: dict[str, int] = {}
    for day in calendar[-T86_DAYS:]:
        check_budget("t86", retry_s=T86_RETRY_S)  # T86 階段的失敗一律每小時
        try:
            table = _t86_day(day, stop, give_up)
        except RoundFailed as exc:
            if exc.not_yet and day != t:  # 早於 T 的日子沒有 T86 不會自己好（多半是日曆多一天）：真的錯誤
                raise RoundFailed("t86_gap", retry_s=T86_RETRY_S) from None
            raise
        for code, net in table.items():
            inst[code] = inst.get(code, 0) + net
            days_seen[code] = days_seen.get(code, 0) + 1
    inst = {code: net for code, net in inst.items() if days_seen[code] == T86_DAYS}
    # 4) 股票池與日線
    ranked = market_value_rank(companies, day_rows)
    if len(ranked) < TOP_N:
        raise RoundFailed("universe_short")
    universe = [code for code, _ in ranked[:TOP_N]]
    caps = dict(ranked[:TOP_N])
    pool_codes = list(dict.fromkeys(universe + [c for c in FAQ_CODES if c in day_rows]))
    check_budget("yahoo")
    charts = _fetch_charts([_symbol(c, TWSE) for c in pool_codes if canary is None or c != "2330"], stop, give_up)
    if canary is not None:
        charts["2330.TW"] = canary  # 抽查與主池共用同一份，不重抓
    check_budget("yahoo")
    medians = industry_medians(companies, pe)
    listed: dict[str, ListedRow] = {}
    no_trade: dict[str, str] = {}
    for code, row in day_rows.items():
        if not _FETCH_CODE_RE.fullmatch(code):
            continue
        company = companies.get(code)
        name = (company.name if company else row.name) or ""
        if row.close is None:
            no_trade[code] = name
            continue
        listed[code] = ListedRow(
            name=name,
            kind=_listed_kind(code),
            industry=company.industry if company else "",
            close=row.close,
            change=row.change,
            pe=pe.get(code),
            revenue=revenue.get(code),
            inst=inst.get(code),
        )
    pool: dict[str, Facts] = {}
    for code in pool_codes:
        row = listed.get(code)
        if row is None:
            continue
        bars = parse_chart(charts.get(_symbol(code, TWSE)))
        windows = window_closes(bars, calendar, t, row.close, _prev_reference(row.close, row.change))
        if windows is None:
            logger.info(
                "stock picks gray code=%s reason=%s", code,
                _window_unavailable_reason(bars, calendar, t, row.close, _prev_reference(row.close, row.change)),
            )
        pool[code] = _facts_for(code, row, t, windows, medians, cap=caps.get(code))
    gray = sum(1 for code in universe if pool.get(code) is None or pool[code].trend is None)
    logger.info("stock picks gray count=%d total=%d", gray, len(universe))
    if gray > TOP_N * MAX_GRAY_RATIO:
        # TODO(2026-10-17 延後)：這類後段的真錯誤每 20 分鐘重試一次，每次會重抓約 51 檔 Yahoo 日線。
        raise RoundFailed("too_many_gray")
    universe_facts = [pool[c] for c in universe if c in pool]
    picks = choose_picks(universe_facts)
    market = market_of([twii_bars[d] for d in calendar])
    otc = _fetch_otc(t, stop, give_up) if give_up - time.monotonic() >= TPEX_MIN_LEFT_S else None
    snapshot = Snapshot(
        t=t,
        calendar=tuple(calendar[-YEAR_BARS:]),
        computed_at=time.time() if now is None else now,
        market=market,
        universe=tuple(universe),
        picks=tuple(p.code for p in picks),
        pool=pool,
        listed=listed,
        medians=medians,
        otc=None if otc is None else otc[0],
        missed=None if picks else most_missed(universe_facts),
        no_trade={**(otc[1] if otc else {}), **no_trade},
        full_names={code: c.full_name for code, c in companies.items() if c.full_name and _FETCH_CODE_RE.fullmatch(code)},
    )
    return _with_index(snapshot)


def _facts_for(
    code: str,
    row,
    t: date,
    windows: Optional[tuple[list[float], list[float]]],
    medians: dict[str, float],
    *,
    cap: Optional[float] = None,
) -> Facts:
    trend = trend_of(*windows) if windows else None
    if isinstance(row, OtcRow):  # 上櫃：普通股或 ETF，都只看走勢
        return Facts(code=code, name=row.name, kind=ETF if code.startswith("00") else OTC,
                     data_date=t, trend=trend, market=TPEX)
    return Facts(
        code=code,
        name=row.name,
        kind=row.kind,
        data_date=t,
        trend=trend,
        revenue=row.revenue if row.kind == STOCK else None,
        pe=row.pe if row.kind == STOCK else None,
        pe_median=medians.get(row.industry) if row.kind == STOCK else None,
        inst_net=row.inst if row.kind == STOCK else None,
        cap=cap if row.kind == STOCK else None,
        market=TWSE,
    )


# ── 背景工作 ───────────────────────────────────────────────────────────────
POLL_S = 30 * 60
STARTUP_DELAY_S = 90.0
# 重試的等待（Andrew 2026-10-17：「還沒出來」每 15 分鐘、真的錯誤最長等 20 分鐘；T86 改每小時；證交所擋住等 1 小時）
NOT_YET_RETRY_S = 15 * 60  # 資料源還沒有 T：不算失敗
T86_RETRY_S = 60 * 60  # T86 一家（還沒出來、錯誤、被擋、額度用完）：24 小時最多 15 次，送出前就記帳
TWSE_BLOCKED_RETRY_S = 60 * 60  # 證交所（openapi、www）回 403／429：等 1 小時，一直打只會被擋更久
BACKOFF_S = (600, 1200)  # 其他真的錯誤：第一次 10 分鐘，之後都 20 分鐘
BLOCKED_BACKOFF_S = 20 * 60  # Yahoo 429：一輪最多幾個請求，20 分鐘再試
STATE_CORRUPT_BACKOFF_S = 60 * 60  # 工作狀態檔壞掉：1 小時（T86 請求帳不能因為壞檔就恢復滿額）
# 讀工作狀態時把 next_try 夾在這個範圍內：新增更長的等待要一起加進來，不然重啟後會被夾短。
_MAX_BACKOFF_S = max(
    *BACKOFF_S, BLOCKED_BACKOFF_S, NOT_YET_RETRY_S, T86_RETRY_S, TWSE_BLOCKED_RETRY_S, STATE_CORRUPT_BACKOFF_S
)
_CLOCK_SKEW_S = 600  # 允許的時鐘誤差（工作狀態裡比現在晚超過這麼多的時間一律夾回）
_YAHOO_429_KINDS = ("yahoo_rate_limited", "canary_rate_limited")  # 主流程自己撞到 429 的失敗／等待種類
_YAHOO_COOLDOWN_KINDS = ("yahoo_cooldown", "canary_cooldown")  # 別人（AI 段、前一輪）設的共用冷卻還沒過：只等，不算失敗
# TODO(2026-10-17 延後)：月初（約 12 天／年）仍要等 Yahoo，08:40 左右可能短暫顯示沒更新；量過再決定要不要放寬。
NEW_DATE_GRACE_S = 3 * 3600
CHECK_STALE_S = 36 * 3600
MIN_WAIT_S = 1.0
# TODO(2026-10-17 延後)：05:40 就算出快照時櫃買可能還沒有 T（上櫃單檔最多約 2.5 小時查不到）；
# 10/20 起看 log 有沒有 tpex list not usable，再決定新快照要不要更常補抓（每次 4.8 MB）。
OTC_RETRY_S = 2 * 3600  # 櫃買清單沒抓到（或日期還不是 T）時，補抓的間隔
_otc_next_try = 0.0


def _fail(kind: str, *, blocked: bool = False, not_yet: bool = False, retry_s: Optional[float] = None) -> float:
    """記下這一輪沒算成並決定下次什麼時候再試；回傳要等幾秒。

    還沒出來（not_yet）：不算失敗——fail_count、last_fail 不動，記 last_wait，等 retry_s（沒給就 15 分鐘）。
    真的錯誤：fail_count +1、記 last_fail、清 last_wait，等 retry_s，否則 Yahoo 被擋 20 分鐘，否則照 BACKOFF_S。
    已經有更晚的期限（壞檔 1 小時、別的程序的冷卻）就留著，不縮短。"""
    if not_yet and blocked:
        not_yet = False
    retry_s = _valid_retry(retry_s)
    now = time.time()
    with _lock:
        if AI_ENABLED and kind in _YAHOO_429_KINDS:  # 主流程自己撞到 429：冷卻結束時間跟著 worker 存（重啟後還在）
            _block_yahoo()
            _worker.yahoo_blocked_until = max(_worker.yahoo_blocked_until or 0.0, _yahoo_blocked_until)
        if not_yet:
            delay = retry_s or NOT_YET_RETRY_S
            _worker.last_wait = kind[:40]
        else:
            _worker.fail_count += 1
            if retry_s is not None:
                delay = retry_s
            elif blocked:
                delay = BLOCKED_BACKOFF_S
            else:
                delay = BACKOFF_S[min(_worker.fail_count - 1, len(BACKOFF_S) - 1)]
            _worker.last_fail = kind[:40]
            _worker.last_wait = ""
            if _snapshot is None:
                _worker.first_round_failed = True
        delay = min(float(delay), float(_MAX_BACKOFF_S))
        _worker.next_try = max(now + delay, _worker.next_try or 0.0)
        if AI_ENABLED and kind in _YAHOO_429_KINDS + _YAHOO_COOLDOWN_KINDS:  # 等共用冷卻的，不早於冷卻結束
            _worker.next_try = max(_worker.next_try, _yahoo_block_end())
        _save_worker()  # 存檔時會併進別的程序更晚的期限：回傳的等待以存完的為準
        delay = max(0.0, (_worker.next_try or now) - now)
        count = _worker.fail_count
    if not_yet:
        logger.info("stock picks waiting for data kind=%s retry_in_s=%d", kind, int(delay))
    else:
        logger.warning("stock picks refresh failed kind=%s count=%d", kind, count)
    return float(delay)


def _maybe_fill_otc(stop: threading.Event) -> None:
    """上櫃清單沒抓到的快照，每 2 小時補抓一次（日期要等於快照的 T；4.8 MB，不要每 30 分鐘抓）。"""
    global _otc_next_try
    snapshot = _snapshot
    if snapshot is None or snapshot.otc is not None or time.monotonic() < _otc_next_try:
        return
    _otc_next_try = time.monotonic() + OTC_RETRY_S
    otc = _fetch_otc(snapshot.t, stop)
    if otc is not None:
        merged = replace(snapshot, otc=otc[0], no_trade={**otc[1], **snapshot.no_trade})
        _commit_snapshot(_with_index(merged))


def _tick(stop: threading.Event) -> float:
    """看一次證交所日資料的日期；有新交易日就整輪重算。回傳下一次要等幾秒。
    收到 stop 中斷的那一輪不算失敗（重啟、部署不應該留下退避）。"""
    now = time.time()
    with _lock:
        snapshot = _snapshot
        if _worker.next_try is not None and now < _worker.next_try:
            return _worker.next_try - now
        up_to_date = snapshot is not None and _worker.seen_t is not None and snapshot.t >= _worker.seen_t
        since = _worker.last_modified if up_to_date else None
    try:
        status, rows, last_modified = _fetch_openapi("/v1/exchangeReport/STOCK_DAY_ALL", stop=stop, if_modified_since=since)
    except FetchError as exc:
        if exc.kind == "stopped" or stop.is_set():
            return POLL_S
        blocked = exc.kind in ("blocked", "rate_limited")
        return _fail(f"day_{exc.kind}", blocked=blocked, retry_s=TWSE_BLOCKED_RETRY_S if blocked else None)
    if status == 304:
        with _lock:
            _worker.last_ok_check = now
            _worker.fail_count, _worker.last_fail = 0, ""  # 已經追上：之前檢查失敗的次數不再累計
            _worker.last_wait = ""
            _save_worker()
        _maybe_fill_otc(stop)
        return POLL_S
    t, day_rows = parse_day_all(rows)
    if t is None or len(day_rows) < _MIN_ROWS or t > _taipei_today():  # 資料日不能晚於台北今天
        return _fail("day_bad")
    with _lock:
        _worker.last_ok_check = now
        if _worker.seen_t is None or t >= _worker.seen_t:  # 舊節點回舊日期時，不拿它的 Last-Modified
            _worker.last_modified = _valid_last_modified(last_modified)
        if _worker.seen_t is None or t > _worker.seen_t:  # 只往前；日期回跳不重設首次看到的時間
            _worker.seen_t, _worker.seen_at = t, now
            logger.info("stock picks saw new trading date t=%s", t.isoformat())
        if snapshot is not None and t <= snapshot.t and _worker.seen_t <= snapshot.t:
            _worker.fail_count, _worker.last_fail = 0, ""  # 真的追上了（沒有待重算的日期）才歸零
            _worker.last_wait = ""
        _save_worker()
    if snapshot is not None and t <= snapshot.t:
        _maybe_fill_otc(stop)
        return POLL_S
    with _lock:  # 存檔時併進來的退避（壞檔、別的程序的冷卻）還沒到期：這輪先不重算
        wait = (_worker.next_try or 0.0) - time.time()
    if wait > 0:
        return wait
    try:
        new = refresh(t, day_rows, stop)
    except RoundFailed as exc:
        if exc.kind == "stopped" or stop.is_set():
            return POLL_S
        return _fail(exc.kind, blocked=exc.blocked, not_yet=exc.not_yet, retry_s=exc.retry_s)
    if stop.is_set():
        return POLL_S
    result = _commit_snapshot(new)
    if result == COMMIT_FAILED:
        return _fail("snapshot_write")
    with _lock:
        _worker.fail_count, _worker.next_try, _worker.last_fail = 0, None, ""
        _worker.first_round_failed = False
        _worker.last_wait = ""
        _save_worker()
    if result == COMMIT_WRITTEN:
        _append_history(new)
    logger.info("stock picks refreshed t=%s picks=%d", new.t.isoformat(), len(new.picks))
    return POLL_S


def _run(stop: threading.Event, wake: threading.Event) -> None:
    _ensure_loaded()
    if stop.wait(STARTUP_DELAY_S):  # 重啟迴圈時不要一起來就打證交所；叫醒也等到這時
        return
    wake.clear()  # 啟動期間的叫醒已經由第一輪處理，不要第一輪一結束又多跑一輪
    if _snapshot is not None:
        _append_history(_snapshot)  # 部署時放進去的快照（上線那天）也記一行；同一個 T 不重複
    if AI_ENABLED and _ai_state:
        _append_ai_history(_ai_state)
    main_due = 0.0  # 主流程下次該跑的時間（monotonic，和 wake.wait 的計時同一種時鐘，睡眠或調時間不會影響）；0＝馬上
    while not stop.is_set():
        if time.monotonic() >= main_due:  # 到期（或被叫醒）才跑 _tick；只是 AI 的 20 分鐘計時器醒來時，只跑 AI 段（不多打證交所）
            try:
                delay = _tick(stop)
            except Exception as exc:  # 最外層接住，不讓執行緒無聲停掉；記成失敗並退避（叫醒也要等）
                logger.error("stock picks tick crashed error_type=%s", type(exc).__name__)
                try:
                    delay = _fail(f"crash_{type(exc).__name__}"[:40])
                except Exception:
                    delay = float(BACKOFF_S[-1])
            main_due = time.monotonic() + delay
        else:
            delay = main_due - time.monotonic()
        try:  # AI 段的例外只記 AI 自己的失敗與節流，不進 _fail、不動主 worker 的失敗次數與退避
            _maybe_fill_ai(stop)
        except Exception as exc:
            logger.warning("stock picks ai reason=round_failed error_type=%s", type(exc).__name__)
            with contextlib.suppress(Exception):  # 記失敗本身也不能讓背景執行緒停掉
                _ai_failure()
        if AI_ENABLED and _AI_BACKGROUND_ENABLED and _ai_next_try > time.time():
            delay = min(delay, _ai_next_try - time.time())
        if wake.wait(max(MIN_WAIT_S, delay)):  # 被叫醒（有人要結果、要停）：主流程也馬上跑一輪
            main_due = 0.0
        wake.clear()


def start_background() -> None:
    """lifespan 啟動時呼叫；執行緒還活著就不再起（每次起都用新的 Event）。"""
    global _thread, _stop, _wake
    if not _BACKGROUND_ENABLED:
        return
    try:  # 美股與推薦成績（2026-10-28）：放在台股提早 return 之前；起不來只記種類，不影響台股
        import stock_picks_us

        stock_picks_us.start_background()
    except Exception as exc:
        logger.error("stock picks us background start failed error_type=%s", type(exc).__name__)
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop, _wake = threading.Event(), threading.Event()
        _thread = threading.Thread(target=_run, args=(_stop, _wake), daemon=True, name="stock-picks")
        _thread.start()


def stop_background(timeout: float = 2.0) -> None:
    us = sys.modules.get("stock_picks_us")  # 沒載入過就不 import（停止時不該多做事）
    if us is not None:
        try:  # 先把兩邊的停止旗標都設好，再各自 join
            us.signal_stop()
        except Exception as exc:
            logger.error("stock picks us background stop failed error_type=%s", type(exc).__name__)
    with _lock:
        thread, stop, wake = _thread, _stop, _wake
    if stop is not None:
        stop.set()
    if wake is not None:
        wake.set()
    if thread is not None:
        thread.join(timeout=timeout)
    if us is not None:
        try:
            us.join_background(timeout=timeout)
        except Exception as exc:
            logger.error("stock picks us background join failed error_type=%s", type(exc).__name__)


def _wake_worker() -> None:
    """不拿 _lock（回覆不能被背景拿著鎖等檔案鎖卡住）；只讀 Event 的參照。"""
    wake = _wake
    if wake is not None:
        wake.set()


def _not_updated(snapshot: Snapshot, now: Optional[float] = None, worker: Optional[WorkerState] = None) -> bool:
    """看到新日期 ≥ 3 小時還沒算成，或 36 小時沒有成功檢查 → 推薦區先不推薦。
    不拿 _lock（只讀幾個欄位；背景拿著鎖等檔案鎖時，回覆不能被卡住）。"""
    now = time.time() if now is None else now
    worker = _worker if worker is None else worker
    seen_t, seen_at, last_ok = worker.seen_t, worker.seen_at, worker.last_ok_check
    if seen_t is not None and seen_t > snapshot.t and seen_at is not None and now - seen_at >= NEW_DATE_GRACE_S:
        return True
    baseline = max(last_ok or 0.0, snapshot.computed_at)
    return now - baseline >= CHECK_STALE_S


def _lag_minutes(snapshot: Optional[Snapshot], w: WorkerState) -> Optional[int]:
    """快照的 T 就是最近看到的新日期時：從看到到算好花了幾分鐘（其他情況 None）。"""
    if snapshot is None or w.seen_t is None or w.seen_at is None or snapshot.t != w.seen_t:
        return None
    lag = snapshot.computed_at - w.seen_at
    return round(lag / 60) if lag >= 0 else None


def status_summary(now: Optional[float] = None) -> dict:
    """給每日維護與部署檢查讀的狀態（不含任何使用者資料）。

    這個程序還沒載入過狀態（例如每日維護、部署腳本另外開的程序）就只讀檔：不清暫存檔、不改執行期狀態。"""
    now = time.time() if now is None else now
    if _loaded:
        snapshot, w = _snapshot, _worker
    else:
        snapshot, w = load_snapshot_file(), load_worker_file()
    info = {
        "data_date": None if snapshot is None else snapshot.t.isoformat(),
        "seen_date": None if w.seen_t is None else w.seen_t.isoformat(),
        "fail_count": w.fail_count,
        "last_fail": w.last_fail,
        "hours_since_check": None if w.last_ok_check is None else round((now - w.last_ok_check) / 3600, 1),
        "backoff_minutes": None if w.next_try is None or w.next_try <= now else round((w.next_try - now) / 60),
        # 在等哪個資料源（不是失敗，2026-10-17）
        "waiting": w.last_wait or None,
        # 看到新日期到算好花了幾分鐘：證交所補 T 有作用時通常在半小時內；Yahoo 那條路約 3 小時
        "lag_minutes": _lag_minutes(snapshot, w),
    }
    if snapshot is None:
        info["status"] = "no_snapshot"
    elif _not_updated(snapshot, now, w):
        info["status"] = "not_updated"
    else:
        info["status"] = "ok"
    info.update(_ai_status_fields(_ai_state if _loaded else load_ai_file()))
    return info


# ── AI 相關股推薦（獨立規則，不改單檔與常問股的五條件） ──────────────────────
# Andrew 2026-10-23／24：推薦區改成「AI 相關＋便宜＋成長」（外加走勢）：名單＝00952 凱基台灣AI50 每天公布的
# 持股＋程式裡每檔附公開來源的核心名單（stock_picks_ai_core.json，每年 5、11 月檢查）；卡片與說明一律寫
# 「AI 相關股推薦」（爸媽可能以為是 AI 模型挑的）。計畫與審查在 ops/state/line_bot_fixes_20261023/PLAN_C。
# 鎖的規則：不巢狀 _state_file_lock；拿著 _lock 或檔案鎖時不做任何網路請求；回覆（handle）只讀記憶體。
AI_ENV = "LINE_BOT_STOCK_PICKS_AI"
_AI_OFF = frozenset({"0", "false", "no", "off"})  # 和 card_pages._OFF 同一組（測試釘住）


def _ai_flag(value: Optional[str]) -> bool:
    """沒設＝開；0／false／no／off＝關（退回舊推薦卡，AI 段完全不跑）。"""
    return (value if value is not None else "1").strip().lower() not in _AI_OFF


AI_ENABLED = _ai_flag(os.environ.get(AI_ENV))  # 載入模組時讀，改了要重啟
AI_RULE = "ai_v1"
AI_NAME = "stock_picks_tw_ai.json"  # stock_picks_tw 前綴：暫存檔清理與每日維護的 glob 都涵蓋
AI_HISTORY_NAME = "stock_picks_tw_ai_history.jsonl"
AI_RETRY_S = 1200.0  # 沒算完或失敗：AI 自己 20 分鐘後再試（不動主 worker 的退避）
AI_BUDGET_S = 60.0  # 一輪 AI 最多花的時間（含抓名單）
AI_FETCH_CAP = 20  # 一輪最多抓幾檔日線
AI_NEGATIVE_TTL_S = 900.0  # 日線有洞：這段時間內不重抓
AI_HOLE_GIVE_UP_S = 3 * 3600.0  # 同一檔、同一個 T 的日線一直有洞（前一天那根 Yahoo 還沒補）：從第一次發現算起 3 小時（真實時間）才當作不合格
AI_FAIL_MAX_TRIES = 3  # 同一檔、同一個 T 回不出日線（例如 404）：Yahoo 當輪有在回資料時，累計 3 次就當作不合格
AI_SKIP_RECHECK_S = 3600.0  # 因為「資料拿不到」被跳過的候選：同一個 T 每小時再看一次
AI_SKIP_RECHECK_MAX = 6  # 每個 T 最多重看幾次；其中一檔算得出來了就重算一次
ENV_FILE = Path(__file__).resolve().parent / ".env"  # main.py 載入的同一個檔；狀態檢查另開的程序沒載入它，所以只為這個旗標另外讀
AI_FETCH_PER_DAY = 3  # 00952 一天最多抓幾次
AI_FETCH_RETRY_S = 7200.0  # 抓名單失敗後多久再試
AI_HOLDINGS_MAX_AGE_DAYS = 45  # 抓不到新的時，沿用上一份持股的期限
AI_STALE_DAYS = 7  # 上一份推薦最多顯示幾個日曆日（Andrew 10/24：「照樣顯示上一份」）
TEXT_AI_POOL_NOTE = "推薦卡從 AI 相關股挑，條件和這張卡不同"
_AI_BACKGROUND_ENABLED = True  # conftest 會關掉（測試不能讓 _run 順手去抓名單）
_ai_state: Optional[dict] = None  # 記憶體裡驗證過的 AI 狀態；回覆只讀這個
_ai_next_try = 0.0  # AI 自己的節流（真實時間）；失敗後 20 分鐘
_ai_cache_t: Optional[date] = None  # 日線快取是哪個 T 的
_ai_chart_cache: dict = {}  # 代號 → ("ok", 窗口) | ("no", 原因) | ("hole", 到期的 monotonic)
_ai_hole_first: dict = {}  # 代號 → 這個 T 第一次發現日線有洞的真實時間（超過 AI_HOLE_GIVE_UP_S 就放棄）
_ai_fail_tries: dict = {}  # 代號 → 這個 T 回不出日線、而且當輪 Yahoo 有在回資料的累計次數
_ai_skip_check: dict = {"n": 0, "next": 0.0}  # 跳過的候選這個 T 已重看幾次、下次最早什麼時候（真實時間）
_ai_holdings: Optional[dict] = None  # {"codes", "as_of", "fetched_day"}：最近一份驗證過的 00952 持股
_ai_fetch_budget: dict = {}  # {"day", "attempts", "next_try"}：今天抓 00952 的帳

_AI_OK, _AI_NO, _AI_HOLE, _AI_FAIL, _AI_LIMIT = "ok", "no", "hole", "fail", "limit"
# 日線有洞（前一天那根不見）可能只是 Yahoo 還沒補，先當未知；其他算不出窗口的原因（根數不夠、相鄰兩根跳超過
# 25%、官方收盤對不上）這個 T 不會自己好，直接算不合格。
_AI_HOLE_REASONS = frozenset({"previous_day_missing", "unknown"})
# 「資料拿不到」而不是「條件不符」的放棄原因：記進 AI 狀態的 skipped，同一個 T 之後還會重看。
_AI_SKIP_REASONS = _AI_HOLE_REASONS | {"chart_unavailable"}


def ai_checks_of(facts: Facts, ai_source: str = "00952") -> list[Check]:
    """四條件用顯示精度判斷；來源只能是已驗證的 ETF 標記或核心分類。"""
    rev, pe, med, tr = facts.revenue, facts.pe, facts.pe_median, facts.trend
    growth = rev is not None and _num(rev.yoy) is not None and _num(rev.cum_yoy) is not None
    growth_text = "營收資料不足"
    if growth:
        growth_text = f"{rev.month} 月營收比去年同期{_more_less(_tenths(rev.yoy))}"
        if rev.month >= 2:  # 1 月的累計就是單月，不重複寫
            growth_text += f"，1–{rev.month} 月累計{_more_less(_tenths(rev.cum_yoy))}"
    cheap = _num(pe) is not None and pe > 0 and _num(med) is not None and med > 0
    cheap_text = (f"本益比 {_fmt_tenths(_tenths(pe))} 倍，同產業中位數 {_fmt_tenths(_tenths(med))} 倍"
                  if cheap else "本益比或同產業資料不足")
    trend_text = (f"在年線之{'上' if tr.above_year else '下'}，比季線{'高' if tr.p60 >= 0 else '低'} "
                  f"{_fmt_tenths(tr.p60)}%" if tr else "日線資料不足")
    return [Check("ai", bool(ai_source) and facts.kind == STOCK and facts.market == TWSE, "AI 相關"),
            Check(GROWTH, bool(growth and _tenths(rev.yoy) > 0 and _tenths(rev.cum_yoy) >= 150), growth_text),
            Check(CHEAP, bool(cheap and 50 <= _tenths(pe) <= _tenths(med)), cheap_text),
            Check("trend", tr is not None and tr.above_year and tr.p60 <= 100, trend_text)]


def ai_eligible(facts: Facts, ai_source: str = "00952") -> bool:
    return all(c.state is True for c in ai_checks_of(facts, ai_source))


def ai_sort_key(facts: Facts) -> tuple:
    """原始累計營收年增／正有限本益比降冪，同分按代號（快照沒有發行股數，不用市值）。"""
    rev, pe = facts.revenue, _num(facts.pe)
    ratio = rev.cum_yoy / pe if rev and _num(rev.cum_yoy) is not None and pe and pe > 0 else -math.inf
    return -ratio, facts.code


def ai_candidates(snapshot: Snapshot, codes: Sequence[str], sources: dict) -> tuple[list[Facts], int]:
    """條件 1–3（AI 相關、營收成長、本益比不高於同業）只靠主快照就算得出來：
    回 (候選：照排序鍵排好, 營收成長的檔數)。走勢（條件 4）最後才一檔一檔查。"""
    candidates: list[Facts] = []
    growth_count = 0
    for code in codes:
        row = snapshot.listed.get(code)
        if row is None:
            continue
        f = _facts_for(code, row, snapshot.t, None, snapshot.medians)
        is_ai, grows, cheap = (c.state is True for c in ai_checks_of(f, sources[code])[:3])
        if is_ai and grows:
            growth_count += 1
            if cheap:
                candidates.append(f)
    candidates.sort(key=ai_sort_key)
    return candidates, growth_count


def _empty_ai_state() -> dict:
    return {"schema": 1, "rule": AI_RULE, "t": None, "universe": {"as_of": None, "source": "core_only",
            "codes": [], "etf_codes": [], "fetched_day": None}, "picks": [],
            "funnel": dict.fromkeys(("universe", "growth", "cheap", "trend"), 0), "computed_at": 0.0,
            "last_try": 0.0, "fail_count": 0, "core_as_of": None, "core_version": ""}


def ai_state_from_json(obj) -> dict:
    """讀取與寫入共用驗證；沒有結果（t=None）和有效的零檔不能混淆。"""
    from stock_picks_ai_source import SEGMENTS
    _require(isinstance(obj, dict) and obj.get("schema") == 1)
    out = dict(obj)
    today, now = _taipei_today(), time.time()
    _v_str(obj.get("rule"), 40)
    t = None if obj.get("t") is None else _v_date(obj["t"])
    _require(t is None or t <= today)  # 未來的 T 整份不收（不夾回）
    for key in ("computed_at", "last_try"):
        out[key] = min(_v_num(obj.get(key), 0, 1e12), now)
    _v_int(obj.get("fail_count"), 0, 10**6)
    _v_str(obj.get("core_version", ""), 64, allow_empty=True)
    if obj.get("core_as_of") is not None:
        _require(_v_date(obj["core_as_of"]) <= today)
    u = obj.get("universe")
    _require(isinstance(u, dict) and u.get("source") in ("00952", "cached", "core_only"))
    for key, limit in (("codes", 120), ("etf_codes", 60)):
        codes = u.get(key, [])
        _require(isinstance(codes, list) and len(codes) <= limit)
        _require(all(isinstance(c, str) and re.fullmatch(r"[0-9]{4}", c) for c in codes))
        _require(len(set(codes)) == len(codes))
    for key in ("as_of", "fetched_day"):
        if u.get(key) is not None:
            _require(_v_date(u[key]) <= today)
    picks, funnel = obj.get("picks"), obj.get("funnel")
    _require(isinstance(picks, list) and len(picks) <= MAX_PICKS)
    _require(isinstance(funnel, dict))
    counts = [_v_int(funnel.get(k), 0, 120) for k in ("universe", "growth", "cheap", "trend")]
    _require(counts == sorted(counts, reverse=True) and counts[0] == len(u["codes"]))
    codes = []
    for raw in picks:
        _require(isinstance(raw, dict) and raw.get("ai_source") in SEGMENTS | {"00952"})  # 卡上的來源文字只能是固定清單
        f = _facts_from(raw)
        # 規則換了（門檻調整）以後，舊檔的入選者是當時合格的，不用新門檻重驗：不然整份舊推薦被丟掉、卡片變成「計算中」。
        _require(f.data_date == t and f.code in u["codes"] and (obj.get("rule") != AI_RULE or ai_eligible(f, raw["ai_source"])))
        _require(not f.code.startswith(("00", "91")) and not f.name.endswith(("-DR", "-創")))
        if raw["ai_source"] == "00952":
            _require(f.code in u.get("etf_codes", []) and u.get("as_of") is not None)
        codes.append(f.code)
    _require(len(set(codes)) == len(codes) and len(picks) == counts[-1])
    _require(t is not None or (not picks and not any(counts)))
    skipped = obj.get("skipped", {})  # 因為「資料拿不到」被跳過的候選（舊檔沒有這個鍵）：代號 → 固定原因
    _require(isinstance(skipped, dict) and len(skipped) <= 60)
    _require(all(isinstance(c, str) and c in u["codes"] and r in _AI_SKIP_REASONS for c, r in skipped.items()))
    if obj.get("holdings") is not None:
        h = obj["holdings"]
        _require(isinstance(h, dict) and 40 <= len(h.get("codes", [])) <= 60)
        _require(len(set(h["codes"])) == len(h["codes"]))
        _require(all(isinstance(c, str) and re.fullmatch(r"[0-9]{4}", c) for c in h["codes"]))
        _require(_v_date(h["as_of"]) <= today and _v_date(h["fetched_day"]) <= today)
    if obj.get("fetch_budget"):
        b = obj["fetch_budget"]
        _require(isinstance(b, dict) and _v_date(b["day"]) <= today)
        _v_int(b["attempts"], 0, AI_FETCH_PER_DAY)
        out["fetch_budget"] = {**b, "next_try": min(_v_num(b["next_try"], 0, 1e12), now + 86400)}
    return out


def load_ai_file() -> Optional[dict]:
    """壞檔、缺檔、葉節點模組不見都當作沒有（回 None），不丟例外。"""
    try:
        obj = _read_json_file(STATE_DIR / AI_NAME)
        return None if obj is None else ai_state_from_json(obj)
    except Exception:
        logger.warning("stock picks ai reason=state_invalid")
        return None


def _commit_ai(payload: dict) -> bool:
    """寫 AI 狀態再換記憶體。不退回較舊的 T（磁碟上的比較新就改用磁碟上的）；同一個 T 可以覆寫
    （規則或名單換了、失敗次數更新）。沒有結果的狀態（t=None）不會蓋掉有結果的。"""
    global _ai_state
    with _lock:
        try:
            new = ai_state_from_json(payload)
            with _state_file_lock():
                disk = load_ai_file()
                if disk and disk.get("t") and (not new.get("t") or disk["t"] > new["t"]):
                    _ai_state = disk
                    return False
                _write_json_file(STATE_DIR / AI_NAME, new)
            _ai_state = new
            return True
        except Exception:
            logger.warning("stock picks ai reason=state_write")
            return False


def _append_ai_history(state: dict) -> None:
    """每個交易日至少一行（rev=1），帶 rule；同一個 T 重算（00952 晚到、補上跳過的候選）而且 picks 和該 T 最後一行
    不同時，再追加一行、rev 加 1，picks 一樣就不寫；讀取端同一個 t 取 rev 最大的那行。
    欄位：t、rev、rule、computed_at、source、core_version、picks[code, name, close, ai_source, p60]。
    舊的 stock_picks_history.jsonl 照舊寫（一個 T 一行）、不再顯示。"""
    if state.get("t") is None:
        return
    record = {"t": state["t"], "rule": state["rule"], "computed_at": state["computed_at"],
              "source": state["universe"]["source"], "core_version": state.get("core_version", ""),
              "picks": [{"code": p["c"], "name": p["n"], "close": p["tr"][0],
                         "ai_source": p["ai_source"], "p60": p["tr"][3]} for p in state["picks"]]}
    _append_history(None, name=AI_HISTORY_NAME, record=record, revise=True)


def _ai_record(*, failed: bool, reason: str = "exception") -> None:
    """這輪沒有可寫的結果：保留上一份，記下試過的時間與抓名單的帳，AI 自己 20 分鐘後再試（不動主 worker）。
    failed：真的失敗（抓不到日線、名單不能用）才加失敗次數；只是還沒查完（額度、時間、Yahoo 冷卻、
    日線有洞）不加——不然部署當天多跑幾輪就會被當成「暫時算不出來」。reason 只是 log 用的固定原因碼。"""
    global _ai_next_try
    now = time.time()
    _ai_next_try = now + AI_RETRY_S
    state = dict(_ai_state or _empty_ai_state())
    state.update(last_try=now, fail_count=min(state["fail_count"] + (1 if failed else 0), 10**6),
                 holdings=_ai_holdings, fetch_budget=dict(_ai_fetch_budget))
    _commit_ai(state)
    (logger.warning if failed else logger.info)(
        "stock picks ai %s reason=%s fail_count=%d retry_in_s=%d",
        "failed" if failed else "waiting", reason, state["fail_count"], int(AI_RETRY_S),
    )


def _ai_failure() -> None:
    _ai_record(failed=True)


def _ai_core():
    """核心檔整份不合法就不用（回 (None, "invalid")）；版本含內容的雜湊，換名單可以同一個 T 重算。"""
    from stock_picks_ai_source import load_core
    try:
        core = load_core(today=_taipei_today())
    except Exception:
        logger.warning("stock picks ai reason=core_invalid")
        return None, "invalid"
    data = [(code, entry.segment, entry.source_url, str(entry.source_date)) for code, entry in sorted(core.entries.items())]
    version = hashlib.sha256(json.dumps([str(core.as_of), data], ensure_ascii=False).encode()).hexdigest()
    return core, version


def _ai_fetch_due(today: date, now: float) -> bool:
    """今天還沒抓到 00952，而且額度（一天 3 次）與間隔（失敗後 2 小時）都允許。"""
    h, b = _ai_holdings, _ai_fetch_budget
    if h and h.get("fetched_day") == today.isoformat():
        return False
    if b.get("day") != today.isoformat():
        return True
    return b["attempts"] < AI_FETCH_PER_DAY and now >= b["next_try"]


def _ai_holdings_usable(h: Optional[dict], today: date) -> bool:
    """這份 00952 持股能不能用：有，而且名單日期不在未來、最多舊 AI_HOLDINGS_MAX_AGE_DAYS 天。
    _ai_universe 和 _ai_fetched_today 共用這一條（2026-10-29）。"""
    return h is not None and 0 <= (today - date.fromisoformat(h["as_of"])).days <= AI_HOLDINGS_MAX_AGE_DAYS


def _ai_fetched_today(today: date) -> bool:
    """記憶體裡的 00952 是今天抓的、而且照 _ai_holdings_usable 能用：拿它重算，名單來源一定是 00952（2026-10-29）。"""
    h = _ai_holdings
    return _ai_holdings_usable(h, today) and h["fetched_day"] == today.isoformat()


def _ai_universe(snapshot: Snapshot, give_up: float) -> tuple:
    """名單＝（00952 持股 ∪ 核心名單）過「目前上市普通股」資格；回 (名單資訊, {代號: 來源}, 核心日期, 核心版本)。
    00952 一天最多抓 3 次、失敗 2 小時後再試、被擋（403／429／5xx）當天不再抓；抓不到就沿用上一份（45 天內），
    再不行只用核心名單。網路請求都在鎖外。"""
    global _ai_holdings, _ai_fetch_budget
    from stock_picks_ai_source import SourceError, eligible_codes, fetch_holdings
    today, now = _taipei_today(), time.time()
    core, version = _ai_core()
    if _ai_fetch_budget.get("day") != today.isoformat():
        _ai_fetch_budget = {"day": today.isoformat(), "attempts": 0, "next_try": 0.0}
    b, h = _ai_fetch_budget, _ai_holdings
    if _ai_fetch_due(today, now):
        b["attempts"] += 1
        b["next_try"] = now + AI_FETCH_RETRY_S
        try:
            result = fetch_holdings(today=today, previous_as_of=date.fromisoformat(h["as_of"]) if h else None,
                                    deadline=min(5.0, max(0.01, give_up - time.monotonic())))
            h = _ai_holdings = {"codes": [c for c, _ in result.rows], "as_of": result.as_of.isoformat(),
                                "fetched_day": today.isoformat()}
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status in (403, 429) or (isinstance(status, int) and 500 <= status <= 599):
                b["attempts"] = AI_FETCH_PER_DAY  # 被擋或對方壞了：今天不再打
            logger.info("stock picks ai holdings unavailable reason=%s", exc.reason if isinstance(exc, SourceError) else "error")
    usable = _ai_holdings_usable(h, today)
    etf = set(h["codes"]) if usable else set()
    entries = core.entries if core else {}
    codes = eligible_codes(etf | set(entries), snapshot.listed)
    if not codes:
        raise ValueError("universe_empty")
    if usable:
        source = "00952" if h["fetched_day"] == today.isoformat() else "cached"
    else:
        source = "core_only"
        logger.info("stock picks ai reason=core_only")
    sources = {c: "00952" if c in etf else entries[c].segment for c in codes}
    u = {"as_of": h["as_of"] if usable else (core.as_of.isoformat() if core else None), "source": source,
         "codes": list(codes), "etf_codes": sorted(etf), "fetched_day": h["fetched_day"] if usable else None}
    return u, sources, core.as_of.isoformat() if core else None, version


@dataclass
class _AiRound:
    """一輪 AI 計算的帳（只在這一輪內用）：fetches＝送出的日線請求數（上限 AI_FETCH_CAP）、served＝有回資料的請求數、
    unfetched＝回不出資料的代號（單檔壞掉或整個 Yahoo 壞掉，要等這一輪結束時確認 Yahoo 是活的才分得出來）。"""

    fetches: int = 0
    served: int = 0
    unfetched: list = field(default_factory=list)


def _ai_windows(snapshot: Snapshot, code: str, row: ListedRow, stop: threading.Event, give_up: float,
                rnd: _AiRound) -> tuple[str, Optional[tuple]]:
    """主池沒有的候選：查這一檔的年線、季線窗口。回 (結果, 窗口)：
    ok／no 是有結果（no＝這個 T 確定算不出來，或日線有洞／抓不到拖太久而放棄，當作不合格）；hole／fail 是還沒結果
    （日線有洞還沒到期限／抓取失敗）；limit 是額度、時間或 Yahoo 冷卻用完，這一輪不能再查。rnd 是這一輪的帳（共用）。"""
    cached = _ai_chart_cache.get(code)
    now = time.monotonic()
    if cached:
        if cached[0] == _AI_OK:
            return _AI_OK, cached[1]
        if cached[0] == _AI_NO:
            return _AI_NO, None
        if cached[0] == _AI_HOLE and cached[1] > now:
            return _AI_HOLE, None
    if rnd.fetches >= AI_FETCH_CAP or now >= give_up or _yahoo_blocked():
        return _AI_LIMIT, None
    rnd.fetches += 1
    try:
        data = _fetch_chart(_symbol(code, TWSE), stop=stop, timeout=min(YAHOO_TIMEOUT_S, max(0.01, (give_up - now) / 4)))
    except FetchError as exc:  # 429 已經由 _fetch_chart 設了共用冷卻
        if exc.kind in ("cooldown", "stopped"):
            return _AI_LIMIT, None
        if exc.kind == "rate_limited":
            return _AI_FAIL, None  # 429 不算「這一檔回不出日線」
        data = None  # 其他種類（實際上 _fetch_chart 已經自己吞掉了）當作抓不到
    if data is None:
        rnd.unfetched.append(code)
        return _AI_FAIL, None
    rnd.served += 1
    bars = parse_chart(data)
    prev = _prev_reference(row.close, row.change)
    windows = window_closes(bars, snapshot.calendar, snapshot.t, row.close, prev)
    if windows is not None:
        _ai_chart_cache[code] = (_AI_OK, windows)
        return _AI_OK, windows
    reason = _window_unavailable_reason(bars, snapshot.calendar, snapshot.t, row.close, prev)
    if reason in _AI_HOLE_REASONS:
        wall = time.time()
        first = _ai_hole_first.setdefault(code, wall)
        if wall - first < AI_HOLE_GIVE_UP_S:
            _ai_chart_cache[code] = (_AI_HOLE, now + AI_NEGATIVE_TTL_S)
            logger.info("stock picks ai code=%s hole reason=%s waited_s=%d", code, reason, int(wall - first))
            return _AI_HOLE, None
        logger.warning("stock picks ai code=%s gave up reason=%s waited_s=%d", code, reason, int(wall - first))
    _ai_chart_cache[code] = (_AI_NO, reason)
    logger.info("stock picks ai code=%s not eligible reason=%s", code, reason)
    return _AI_NO, None


def _ai_yahoo_alive(stop: threading.Event, give_up: float) -> bool:
    """抽查一檔一定有的（2330）：Yahoo 現在有沒有在回日線。額度、時間、冷卻不允許抽查就當作不確定（否）。"""
    now = time.monotonic()
    if stop.is_set() or now >= give_up or _yahoo_blocked():
        return False
    try:
        data = _fetch_chart("2330.TW", stop=stop, timeout=min(YAHOO_TIMEOUT_S, max(0.01, (give_up - now) / 4)))
    except FetchError:
        return False
    return data is not None


def _ai_settle_chart_failures(rnd: _AiRound, stop: threading.Event, give_up: float) -> None:
    """這一輪有代號回不出日線：只有「Yahoo 當輪確實有在回資料」才算那一檔自己的失敗，整個 Yahoo 壞掉時不能把排名靠前的
    候選悄悄換掉。這一輪沒有任何請求成功（例如其他檔的結果都已在快取裡，只剩壞掉的那一檔要抓）就抽查 2330 當證據。
    累計 AI_FAIL_MAX_TRIES 次就當作這個 T 不合格（chart_unavailable）；之後寫進 skipped，同一個 T 還會重看。"""
    if rnd.served == 0 and not _ai_yahoo_alive(stop, give_up):
        logger.info("stock picks ai reason=yahoo_not_confirmed unfetched=%d", len(rnd.unfetched))
        return
    for code in rnd.unfetched:
        tries = _ai_fail_tries[code] = _ai_fail_tries.get(code, 0) + 1
        if tries >= AI_FAIL_MAX_TRIES:
            _ai_chart_cache[code] = (_AI_NO, "chart_unavailable")
            logger.warning("stock picks ai code=%s not eligible reason=chart_unavailable tries=%d", code, tries)
        else:
            logger.info("stock picks ai code=%s chart unavailable tries=%d", code, tries)


def _ai_skipped_resolved(snapshot: Snapshot, state: dict, stop: threading.Event) -> bool:
    """同一個 T、已經寫好的推薦裡有候選因為「資料拿不到」被跳過：每小時重看一次、每個 T 最多 6 次。
    其中一檔算得出窗口就回 True（呼叫端重算一次）；還是不行就維持原本的跳過判定。"""
    check, now = _ai_skip_check, time.time()
    if check["n"] >= AI_SKIP_RECHECK_MAX or now < check["next"] or _yahoo_blocked():
        return False
    check["n"] += 1
    check["next"] = now + AI_SKIP_RECHECK_S
    give_up, rnd, resolved = time.monotonic() + AI_BUDGET_S, _AiRound(), False
    for code, reason in state["skipped"].items():
        row = snapshot.listed.get(code)
        if row is None or stop.is_set():
            continue
        saved = _ai_chart_cache.pop(code, None)
        status, _ = _ai_windows(snapshot, code, row, stop, give_up, rnd)
        if status == _AI_OK:
            resolved = True
        elif status != _AI_NO:  # 還是拿不到（洞、抓不到、額度）：維持原本的跳過
            _ai_chart_cache[code] = saved or (_AI_NO, reason)
    logger.info("stock picks ai skipped recheck n=%d resolved=%s", check["n"], resolved)
    return resolved


def _maybe_fill_ai(stop: threading.Event) -> None:
    """AI 相關股推薦的背景計算；`_run` 在每輪 `_tick` 之後呼叫一次（不管 `_tick` 是否提早返回）。

    條件 1–3 用主快照算完排好，條件 4（走勢）照最後排名一檔一檔查：主池有的直接用，沒有的才抓 Yahoo，
    滿 3 檔就停。只有「最後一檔入選者之前排名的候選都有結果」才寫；有候選還沒結果就保留上一份、之後再試，
    不讓排名較後的候選偷偷補位。候選的日線一直拿不到（有洞超過 3 小時、或 Yahoo 在回別檔卻累計 3 次回不出這一檔）
    才放棄，並記在狀態的 skipped；同一個 T 之後每小時重看，算得出來就重算一次。
    失敗（例外由 `_run` 接住）只記 AI 自己的失敗與節流，不動主 worker。"""
    global _ai_cache_t, _ai_next_try
    if not AI_ENABLED or not _AI_BACKGROUND_ENABLED or stop.is_set():
        return
    snapshot = _snapshot
    now = time.time()
    if snapshot is None or now < _ai_next_try:
        return
    _, version = _ai_core()
    old, t_iso = _ai_state, snapshot.t.isoformat()
    if old and old.get("t") and old["t"] > t_iso:
        return  # 已經比主快照還新（不該發生）：不退回去
    if _ai_cache_t != snapshot.t:  # 換了一個 T：這一天的日線快取、洞與失敗的帳都重來
        _ai_chart_cache.clear()
        _ai_hole_first.clear()
        _ai_fail_tries.clear()
        _ai_skip_check.update(n=0, next=0.0)
        _ai_cache_t = snapshot.t
    current = bool(old and old.get("t") == t_iso and old["rule"] == AI_RULE and old.get("core_version") == version)
    # 同一個 T 通常算過就不再算；例外：① 當天沒抓到 00952（用的是上一份或只有核心名單），而且現在又可以抓了，
    # 或今天已經抓到、只是那一輪沒算完（2026-10-29：直接用記憶體那份重算、不再抓；照 _ai_universe 的條件能用才算，
    # 重算出來才一定是 00952、寫了就不再進來，不然寫完 _ai_next_try 歸零、每一輪都會重算）；
    # ② 有候選因為「資料拿不到」被跳過，而且這次重看其中一檔算得出來了。
    today = _taipei_today()
    holdings_due = current and old["universe"]["source"] != "00952" and (_ai_fetch_due(today, now) or _ai_fetched_today(today))
    if current and not holdings_due and not (old.get("skipped") and _ai_skipped_resolved(snapshot, old, stop)):
        return
    give_up = time.monotonic() + AI_BUDGET_S
    u, sources, core_as_of, version = _ai_universe(snapshot, give_up)
    if stop.is_set():
        return
    candidates, growth_count = ai_candidates(snapshot, u["codes"], sources)
    picks: list[dict] = []
    skipped: dict = {}
    rnd = _AiRound()
    unresolved, failed, holes = False, False, False
    for f in candidates:
        if stop.is_set():
            return
        row = snapshot.listed[f.code]
        pool = snapshot.pool.get(f.code)
        if pool is not None and pool.trend is not None:
            f = replace(f, trend=pool.trend)
        else:
            status, windows = _ai_windows(snapshot, f.code, row, stop, give_up, rnd)
            if status == _AI_LIMIT:
                unresolved = True
                break
            if status in (_AI_HOLE, _AI_FAIL):
                unresolved, failed, holes = True, failed or status == _AI_FAIL, holes or status == _AI_HOLE
                continue
            if status == _AI_NO:
                reason = _ai_chart_cache.get(f.code, (_AI_NO, ""))[1]
                if reason in _AI_SKIP_REASONS:
                    skipped[f.code] = reason
                continue
            f = _facts_for(f.code, row, snapshot.t, windows, snapshot.medians)
        if ai_eligible(f, sources[f.code]):
            picks.append({**_facts_json(f), "ai_source": sources[f.code]})
            if len(picks) == MAX_PICKS:
                break
    if unresolved:
        if rnd.unfetched:
            _ai_settle_chart_failures(rnd, stop, give_up)
        # current＝這個 T 已經有好結果（只是 00952 晚到或跳過的候選要重算）：這次沒算完不能把它標成失敗
        _ai_record(failed=failed and not current, reason="fetch_failed" if failed else "line_hole" if holes else "limit")
        return
    done = time.time()
    state = {"schema": 1, "rule": AI_RULE, "t": t_iso, "universe": u, "picks": picks,
             "funnel": {"universe": len(u["codes"]), "growth": growth_count, "cheap": len(candidates), "trend": len(picks)},
             "computed_at": done, "last_try": done, "fail_count": 0,
             "core_as_of": core_as_of, "core_version": version, "holdings": _ai_holdings,
             "fetch_budget": dict(_ai_fetch_budget), "skipped": skipped}
    if _commit_ai(state):
        _ai_next_try = 0.0
        if skipped:  # 剛放棄就馬上重看沒有意義：第一次重看在一小時後
            _ai_skip_check["next"] = max(_ai_skip_check["next"], done + AI_SKIP_RECHECK_S)
        _append_ai_history(state)
        try:  # 回測週報的池子紀錄（2026-10-29 回測 PLAN §5.2）：只寫自己的 spool 檔；任何問題都不能變成 AI 段的失敗
            import stock_picks_review_hook
            stock_picks_review_hook.write_ai_spool(state, snapshot, candidates, sources,
                                                   chart_cache=_ai_chart_cache, fail_tries=_ai_fail_tries)
        except Exception as exc:
            logger.warning("stock picks ai review spool failed error_type=%s", type(exc).__name__)
        logger.info("stock picks ai computed t=%s source=%s universe=%d growth=%d cheap=%d picks=%d skipped=%d fetched=%d",
                    t_iso, u["source"], len(u["codes"]), growth_count, len(candidates), len(picks), len(skipped), rnd.fetches)
    else:
        _ai_next_try = time.time() + AI_RETRY_S


def _ai_stale(state: dict, snapshot: Snapshot, now: Optional[float] = None) -> bool:
    """上一份推薦還能不能顯示：AI 狀態的 T 距主快照或最近看到的新日期超過 7 個日曆日，
    或一個星期沒有成功檢查，才改回「資料暫時沒有更新」。"""
    now = time.time() if now is None else now
    t = date.fromisoformat(state["t"])
    latest = max(snapshot.t, _worker.seen_t or snapshot.t)
    baseline = max(_worker.last_ok_check or 0.0, snapshot.computed_at, state["computed_at"])
    return (latest - t).days > AI_STALE_DAYS or now - baseline > AI_STALE_DAYS * 86400


def _ai_scope_line(universe: dict) -> str:
    """第一張卡的說明：名單從哪裡來（日期是名單的日期，不是推薦的資料日）。"""
    as_of = universe.get("as_of")
    day = f"（名單 {_fmt_date(date.fromisoformat(as_of))}）" if as_of else ""
    if universe.get("source") == "core_only":
        return f"從 AI 相關股挑：核心名單{day}，最多 3 檔"
    return f"從 AI 相關股挑：凱基台灣AI50 持股＋核心名單{day}，最多 3 檔"


def ai_pick_bubble(facts: Facts, index: int, total: int, market: Optional[Market], *, ai_source: str = "00952",
                   as_of: Optional[date] = None, market_date: Optional[date] = None, note: str = "",
                   lead_note: bool = False, scope: str = "") -> dict:
    """AI 相關股推薦卡：收盤、名單來源，成長、便宜、走勢三行（專屬句型：只有程式算的數字與固定分類）。
    note＝推薦資料比主快照舊時的「推薦資料到 M/D」；scope＝第一張卡的名單說明。"""
    if ai_source == "00952":
        source = f"凱基台灣AI50 持股（名單 {_fmt_date(as_of)}）" if as_of else "凱基台灣AI50 持股"
    else:
        source = f"核心名單（{ai_source}）"
    body = _lead(lead_note)
    if lead_note:
        body.append(_text(scope or "從 AI 相關股挑：凱基台灣AI50 持股＋核心名單，最多 3 檔", size="md"))
    if note:
        body.append(_text(note, size="md", weight="bold"))
    body += [_text(_label(facts), size="xl", weight="bold"), _text("✅ 4 條都符合"), _text(_close_line(facts)),
             _text(f"AI 相關：{source}", size="md")]
    labels = {GROWTH: "成長", CHEAP: "便宜", "trend": "走勢"}
    body += [_text(f"{labels[c.key]}：{c.text}", size="md") for c in ai_checks_of(facts, ai_source)[1:]]
    subtitle = market_line(market) + (f"（{_fmt_date(market_date)}）" if market_date else "")
    return _bubble(f"AI 相關股推薦：第 {index} 檔（共 {total} 檔）", subtitle, body,
                   [_text(_source_line(facts.data_date), size="sm"), _text(TEXT_DISCLAIMER, size="sm")])


def _ai_picks_reply(snapshot: Snapshot) -> Reply:
    """/股票 的推薦區：只讀記憶體裡的 AI 狀態（不讀檔、不抓資料）。大盤那行是主快照的 T；推薦比較舊時
    第一張卡寫「推薦資料到 M/D」，兩個日期都看得出來。"""
    state = _ai_state
    picks: list[Facts] = []
    note, stale, t = "", False, snapshot.t
    if state and state.get("t"):
        t = date.fromisoformat(state["t"])
        stale = _ai_stale(state, snapshot)
        if max(snapshot.t, _worker.seen_t or snapshot.t) > t:
            note = f"推薦資料到 {_fmt_date(t)}"
            if not state["fail_count"] and not stale:  # 算失敗過就不說「計算中」，免得一直掛著
                note += "，新資料計算中"
        if not stale:
            picks = [_facts_from(p) for p in state["picks"]]
    if picks:
        scope = _ai_scope_line(state["universe"])
        as_of = date.fromisoformat(state["universe"]["as_of"]) if state["universe"]["as_of"] else None
        bubbles = [
            ai_pick_bubble(f, i + 1, len(picks), snapshot.market, ai_source=state["picks"][i]["ai_source"], as_of=as_of,
                           market_date=snapshot.t, note=note if i == 0 else "", lead_note=i == 0, scope=scope)
            for i, f in enumerate(picks)
        ]
        alt = picks_alt_text(picks, t, not_updated=False)
    else:
        if not state or not state.get("t"):  # 還沒有結果：和「有結果但零檔」是不同的狀態
            text = "AI 相關股推薦暫時算不出來" if state and state["fail_count"] >= 3 else "AI 相關股推薦計算中，請稍後再按一次"
        elif stale:
            text = TEXT_NOT_UPDATED
        else:
            f = state["funnel"]
            text = (f"AI 名單上市 {f['universe']} 檔 → 營收成長 {f['growth']} 檔 → "
                    f"本益比不高於同業 {f['cheap']} 檔 → 走勢符合 {f['trend']} 檔")
        body = _lead(True) + ([_text(note, size="md", weight="bold")] if note else []) + [_text(text)]
        bubbles = [_bubble("AI 相關股推薦", market_line(snapshot.market) + f"（{_fmt_date(snapshot.t)}）", body,
                           [_text(_source_line(t), size="sm"), _text(TEXT_DISCLAIMER, size="sm")])]
        alt = f"{PREFIX}只供參考｜{text}｜資料到 {_fmt_date(t)}"
    faq = [snapshot.pool[c] for c in FAQ_CODES if c in snapshot.pool]
    if faq:
        bubbles.append(faq_bubble(faq, snapshot.t))
    return Reply(flex=carousel(bubbles), alt_text=alt)


def _env_file_value(name: str) -> Optional[str]:
    """只讀 .env 裡這一個鍵的值（最後一次設定算數；不丟例外：沒有檔、讀不了、太大、不是 UTF-8 都當沒有）。"""
    try:
        if ENV_FILE.stat().st_size > 256 * 1024:
            return None
        text = ENV_FILE.read_text(encoding="utf-8")
    except Exception:
        return None
    value: Optional[str] = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, rest = line.partition("=")
        if not sep or key.strip() != name:
            continue
        rest = rest.strip()
        if rest[:1] in ("'", '"'):  # 有引號：到下一個同樣的引號為止
            end = rest.find(rest[0], 1)
            value = rest[1:end] if end > 0 else rest[1:]
        else:  # 沒引號：空白加 # 之後是註解
            value = rest.split(" #", 1)[0].strip()
    return value


def _ai_flag_now() -> bool:
    """狀態用的開關：環境變數（有設就用它，和 main.py 的 load_dotenv(override=False) 一致）→ .env → 預設開。
    每日維護與部署 smoke 另開的程序沒有載入 .env，只看環境變數的話永遠讀到「開」，狀態不會寫 disabled。"""
    value = os.environ.get(AI_ENV)
    return _ai_flag(value if value is not None else _env_file_value(AI_ENV))


def _ai_import_ok() -> bool:
    """葉節點模組（stock_picks_ai_source）在不在：不在時沒有任何狀態寫得進去（連失敗次數都記不了），卡片會一直寫「計算中」。"""
    try:
        import stock_picks_ai_source  # noqa: F401
    except Exception:
        return False
    return True


def _ai_status_fields(state: Optional[dict]) -> dict:
    """給每日維護讀（舊程式沒有這些欄位，維護文字要容許缺欄位）。只讀記憶體或檔案，不改狀態。
    ai_status：disabled（旗標關：模組載入時是關的，或環境變數／.env 現在是關的）→ failed（失敗次數 > 0）→ ok／pending。"""
    u = state["universe"] if state else {}
    if not (AI_ENABLED and _ai_flag_now()):
        status = "disabled"
    elif state and state["fail_count"]:
        status = "failed"
    else:
        status = "ok" if state and state.get("t") else "pending"
    core_status = None
    if state and state.get("core_version"):
        core_status = "invalid" if state["core_version"] == "invalid" else "ok"
    return {"ai_t": state.get("t") if state else None, "ai_status": status,
            "ai_fail_count": state["fail_count"] if state else 0,
            "ai_core_as_of": state.get("core_as_of") if state else None, "ai_core_status": core_status,
            "ai_universe_source": u.get("source"), "ai_universe_as_of": u.get("as_of"),
            "ai_skipped": sorted(state.get("skipped", {})) if state else [], "ai_import_ok": _ai_import_ok()}


# ── 回覆（main 只呼叫 handle；只讀快照，單檔查詢有限流的即時抓） ──────────────────
TEXT_NOT_READY = "股票推薦：資料準備中，請過幾分鐘再按一次"
TEXT_UNAVAILABLE = "股票推薦：暫時抓不到資料，請稍後再試"
TEXT_CARD_FAILED = "股票推薦：卡片暫時出不來，請稍後再試"
TEXT_EVAL_CARD_FAILED = "股票評估：卡片暫時出不來，請稍後再試"
TEXT_TOO_LONG = "股票評估：請只打一檔的代號或名稱，例如 /股票 2330"
# 名稱比對刻意收緊（不猜），所以不說「找不到這一檔」（爸媽會以為下市），而是請他只打代號或名稱。
TEXT_NOT_FOUND = "股票評估：認不出是哪一檔，請只打代號或名稱，例如 /股票 2330、/股票 台積電；美股請打英文代號，例如 /股票 NVDA"
TEXT_US = "股票評估：美股還不能查，目前只有台股"  # 2026-10-28 起只在美股開關關閉、或美股模組載不進來時用
TEXT_US_FAILED = "股票評估：美股暫時查不到，請稍後再試"
TEXT_US_CARDS_FAILED = "股票推薦：美股推薦和推薦成績暫時出不來，請稍後再試"
TEXT_OTC_UNAVAILABLE = "股票評估：上櫃資料暫時抓不到，請稍後再試"
TEXT_UNSUPPORTED = "股票評估：目前只看普通股和 ETF"
TEXT_BUSY = "股票評估：查詢有點多，請過幾分鐘再試"
TEXT_NO_TIME = "股票評估：這次來不及查，請再傳一次"
TEXT_LOOKUP_FAILED = "股票評估：暫時抓不到這一檔的股價，請稍後再試"


def no_trade_text(day: date) -> str:
    return f"股票評估：這一檔 {_fmt_date(day)} 沒有成交，查不到收盤"


BODY_MAX_CHARS = 40
BODY_MAX_TOKENS = 5
LOOKUP_LIMIT = 4
LOOKUP_WINDOW_S = 600.0
LOOKUP_MIN_REMAINING_S = 5.0
LOOKUP_FAIL_TTL_S = 600.0
LOOKUP_CACHE_MAX = 128
_lookup_lock = threading.Lock()
_lookup_times: deque = deque()
_lookup_cache: "OrderedDict[tuple[str, str], tuple[float, Optional[Facts], bool]]" = OrderedDict()


@dataclass(frozen=True)
class Reply:
    text: Optional[str] = None
    flex: Optional[dict] = None
    alt_text: Optional[str] = None
    fallback_text: str = TEXT_CARD_FAILED


def _normalize_body(body: Optional[str]) -> Optional[str]:
    """先刪 Cf 類字元（零寬空白等）再 NFKC；太長或太多段回 None。"""
    raw = "".join(ch for ch in (body or "")[: BODY_MAX_CHARS * 4] if unicodedata.category(ch) != "Cf")
    text = unicodedata.normalize("NFKC", raw).strip()
    if len(text) > BODY_MAX_CHARS or len(text.split()) > BODY_MAX_TOKENS:
        return None
    return text


def clear_caches() -> None:
    """測試用：清掉單檔查詢的限流與快取、櫃買補抓的計時。"""
    global _otc_next_try, _yahoo_blocked_until, _ai_state, _ai_next_try, _ai_cache_t, _ai_holdings
    _otc_next_try = 0.0
    _yahoo_blocked_until = 0.0
    _ai_state, _ai_next_try, _ai_cache_t, _ai_holdings = None, 0.0, None, None
    _ai_chart_cache.clear()
    _ai_hole_first.clear()
    _ai_fail_tries.clear()
    _ai_skip_check.update(n=0, next=0.0)
    _ai_fetch_budget.clear()
    with _lookup_lock:
        _lookup_times.clear()
        _lookup_cache.clear()


def _lookup(snapshot: Snapshot, code: str, deadline: Optional[float]):
    """股票池以外的單檔：回 Facts 或固定句。只打一個主機、3 秒；全域每 10 分鐘 4 次；同一個 T
    查過、走勢算得出來的快取到換日；抓不到或資料不足（⚪）只記 10 分鐘，之後再抓。"""
    listed = snapshot.listed.get(code)
    otc = (snapshot.otc or {}).get(code)
    if listed is None and otc is None:
        if code in snapshot.no_trade:
            return no_trade_text(snapshot.t)
        if snapshot.otc is None and _FETCH_CODE_RE.fullmatch(code):
            return TEXT_OTC_UNAVAILABLE
        return TEXT_NOT_FOUND
    if not _FETCH_CODE_RE.fullmatch(code):
        return TEXT_UNSUPPORTED
    key = (snapshot.t.isoformat(), code)
    now = time.monotonic()
    with _lookup_lock:
        hit = _lookup_cache.get(key)
        if hit is not None and (hit[2] or now - hit[0] < LOOKUP_FAIL_TTL_S):
            _lookup_cache.move_to_end(key)
            return hit[1] if hit[1] is not None else TEXT_LOOKUP_FAILED
        remaining = (deadline - now) if deadline is not None else 30.0
        if remaining < LOOKUP_MIN_REMAINING_S:
            return TEXT_NO_TIME
        while _lookup_times and now - _lookup_times[0] > LOOKUP_WINDOW_S:
            _lookup_times.popleft()
        if len(_lookup_times) >= LOOKUP_LIMIT:
            return TEXT_BUSY
        _lookup_times.append(now)
    market = TPEX if listed is None else TWSE
    row = otc if listed is None else listed
    try:
        data = _fetch_chart(
            _symbol(code, market), hosts=_YAHOO[:1], timeout=min(YAHOO_TIMEOUT_S, max(1.0, (remaining - 2.0) / 2)),
            shared_block=False,  # 和 2eb85e1 一樣：不看、也不設主流程與 AI 段共用的 429 冷卻
        )
    except FetchError:
        data = None
    facts: Optional[Facts] = None
    if data is not None:
        bars = parse_chart(data)
        windows = window_closes(bars, snapshot.calendar, snapshot.t, row.close, _prev_reference(row.close, row.change))
        facts = _facts_for(code, row, snapshot.t, windows, snapshot.medians)
    with _lookup_lock:
        _lookup_cache[key] = (time.monotonic(), facts, facts is not None and facts.trend is not None)
        _lookup_cache.move_to_end(key)
        while len(_lookup_cache) > LOOKUP_CACHE_MAX:
            _lookup_cache.popitem(last=False)
    return facts if facts is not None else TEXT_LOOKUP_FAILED


def _picks_reply(snapshot: Snapshot) -> Reply:
    if AI_ENABLED:
        return _ai_picks_reply(snapshot)
    not_updated = _not_updated(snapshot)
    picks = [] if not_updated else [snapshot.pool[c] for c in snapshot.picks if c in snapshot.pool]
    if picks:
        bubbles = [
            pick_bubble(p, i + 1, len(picks), snapshot.market, lead_note=(i == 0)) for i, p in enumerate(picks)
        ]
    else:
        bubbles = [no_pick_bubble(snapshot.t, snapshot.market, snapshot.missed, not_updated=not_updated, lead_note=True)]
    faq = [snapshot.pool[c] for c in FAQ_CODES if c in snapshot.pool]
    if faq:
        bubbles.append(faq_bubble(faq, snapshot.t))
    return Reply(flex=carousel(bubbles), alt_text=picks_alt_text(picks, snapshot.t, not_updated=not_updated))


_PICKS_WORDS = ("", "推薦")
_TRAILING_PUNCT = "?？!！。.,，~～ "


def handle(body: str, *, deadline: Optional[float] = None) -> Reply:
    """`/股票` 後面的字 → 回覆。deadline 是 time.monotonic() 的絕對期限（reply token）。
    只讀快照與工作狀態的幾個欄位，不拿 _lock（背景拿著鎖等檔案鎖時，回覆不能被卡住）。"""
    _ensure_loaded()
    text = _normalize_body(body)
    if text is None:
        return Reply(text=TEXT_TOO_LONG)
    us_reply = _us_command(text, deadline)  # 「美股」「成績」「美股 代號」：不需要台股快照
    if us_reply is not None:
        return us_reply
    snapshot, first_failed = _snapshot, _worker.first_round_failed
    if snapshot is None:
        symbol = _us_only_symbol(text)
        if symbol is not None:
            return _us_detail(symbol, deadline)
        _wake_worker()
        return Reply(text=TEXT_UNAVAILABLE if first_failed else TEXT_NOT_READY)
    if text.strip(_TRAILING_PUNCT) in _PICKS_WORDS:
        return _with_us(_picks_reply(snapshot))
    kind, code = resolve_query(text, snapshot.index)
    if kind == US_SYMBOL:
        return _us_detail(code, deadline)
    if kind == UNSUPPORTED:
        return Reply(text=TEXT_UNSUPPORTED)
    if code is None:
        return Reply(text=TEXT_NOT_FOUND)
    facts = snapshot.pool.get(code)
    if facts is None:
        found = _lookup(snapshot, code, deadline)
        if isinstance(found, str):
            return Reply(text=found)
        facts = found
    bubble = detail_bubble(facts, market=snapshot.market)
    return Reply(flex=bubble, alt_text=detail_alt_text(facts), fallback_text=TEXT_EVAL_CARD_FAILED)


# ── 美股與推薦成績的轉接（2026-10-28，第 3 段；邏輯都在 stock_picks_us／stock_picks_perf） ─────────
# 這裡只決定「交給美股」：模組載不進來或出錯，台股照舊，美股回固定句，不讓整個 /股票 掛掉。
_US_PICKS_WORDS = ("美股", "美股推薦")
_PERF_WORDS = ("成績", "推薦成績")
_US_FORCE_PREFIX = "美股"
# 台股快照還沒有時認不得台股名稱：這幾個純英文的台股簡稱（不分大小寫、去掉 -KY）不當成美股代號（10/22 的名稱索引）。
_TW_ASCII_NAMES = frozenset({"abc", "aes", "amax", "gis", "iet", "ikka", "jpp", "linepay", "tpk"})


def _us_command(text: str, deadline: Optional[float]) -> Optional[Reply]:
    """「美股」「美股推薦」→ 美股推薦卡＋成績卡；「成績」「推薦成績」→ 成績卡；「美股 代號」→ 強制查美股
    （台股英文名和美股代號撞名時用，例如 AES、GIS）。都不是回 None。中間有空白也算（「美股 推薦」）。"""
    compact = "".join(text.split()).strip(_TRAILING_PUNCT)
    try:
        if compact in _US_PICKS_WORDS:
            import stock_picks_us

            return stock_picks_us.us_reply()
        if compact in _PERF_WORDS:
            import stock_picks_us

            return stock_picks_us.perf_reply()
        stripped = text.strip()
        if stripped.startswith(_US_FORCE_PREFIX) and len(compact) > len(_US_FORCE_PREFIX):
            import stock_picks_us

            # 後面的字照原樣交給美股切段（不壓掉空白：「美股 BRK B」不會變成 BRKB）
            symbol = stock_picks_us.find_us_symbol(stripped[len(_US_FORCE_PREFIX):])
            if symbol is not None and not stock_picks_us.ENABLED:  # 美股開關關閉：一律舊句
                return Reply(text=TEXT_US)
            if symbol == stock_picks_us.REJECTED:
                return Reply(text=stock_picks_us.TEXT_US_NOT_FOUND)
            if symbol is not None:  # 強制美股：保留字（AI 等）也照查
                return stock_picks_us.handle_symbol(symbol, deadline=deadline)
    except AssertionError:  # 測試的擋網守門（conftest）要讓測試失敗，不能被吞成固定句；正式環境由 main 接住
        raise
    except Exception as exc:
        logger.error("stock picks us command failed error_type=%s", type(exc).__name__)
        return Reply(text=TEXT_US_CARDS_FAILED if compact in _US_PICKS_WORDS + _PERF_WORDS else TEXT_US_FAILED)
    return None


def _us_only_symbol(text: str) -> Optional[str]:
    """台股快照還沒有時：整段只有一個美股代號、又不是台股的純英文簡稱 → 代號（或 REJECTED）；其他 None（照舊回準備中）。"""
    tokens = text.split()
    if len(tokens) != 1 or tokens[0].casefold().removesuffix("-ky") in _TW_ASCII_NAMES:
        return None
    try:
        import stock_picks_us

        symbol = stock_picks_us.find_us_symbol(tokens[0])
        if symbol is not None and symbol.casefold() in _TW_ASCII_NAMES:  # 先解析再比（「gis?」「GIS嗎」也擋）
            return None
        return symbol
    except AssertionError:
        raise
    except Exception as exc:
        logger.error("stock picks us symbol parse failed error_type=%s", type(exc).__name__)
        return None


def _us_detail(symbol: Optional[str], deadline: Optional[float]) -> Reply:
    """美股單檔：保留字回提示；模組載不進來回舊句；出錯回固定句。"""
    try:
        import stock_picks_us
    except Exception as exc:
        logger.error("stock picks us import failed error_type=%s", type(exc).__name__)
        return Reply(text=TEXT_US)
    if symbol is None:
        return Reply(text=TEXT_NOT_FOUND)
    if not stock_picks_us.ENABLED:  # 美股開關關閉：保留字、S&P 這類也一律舊句（ROLLBACK.md §1）
        return Reply(text=TEXT_US)
    if symbol == stock_picks_us.REJECTED:  # 像美股但不收（S&P、J&J 這類）
        return Reply(text=stock_picks_us.TEXT_US_NOT_FOUND)
    if symbol in stock_picks_us.RESERVED:
        return Reply(text=stock_picks_us.TEXT_RESERVED)
    try:
        return stock_picks_us.handle_symbol(symbol, deadline=deadline)
    except AssertionError:  # 測試的擋網守門（conftest）要讓測試失敗，不能被吞成固定句；正式環境由 main 接住
        raise
    except Exception as exc:
        logger.error("stock picks us lookup failed error_type=%s", type(exc).__name__)
        return Reply(text=TEXT_US_FAILED)


def _with_us(reply: Reply) -> Reply:
    """台股推薦卡組後面接美股推薦卡＋成績卡；美股有任何問題就原樣回台股（extend_picks_reply 自己也不丟例外）。"""
    try:
        import stock_picks_us

        return stock_picks_us.extend_picks_reply(reply)
    except AssertionError:  # 測試的擋網守門（conftest）要讓測試失敗，不能被吞成固定句；正式環境由 main 接住
        raise
    except Exception as exc:
        logger.error("stock picks us cards failed error_type=%s", type(exc).__name__)
        return reply
