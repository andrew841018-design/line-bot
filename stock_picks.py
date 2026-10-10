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
燈號只描述狀態，不寫買、賣、等、停損。推薦只從上市市值前 50 大挑，走勢兩條都要成立、
總共至少 4 條，最多 3 檔。ETF、上櫃只看走勢，不進推薦。

資料：證交所 OpenAPI（STOCK_DAY_ALL、BWIBBU_ALL、t187ap03_L、t187ap05_L）、證交所 T86
（外資、投信買賣超）、櫃買中心 OpenAPI（上櫃每日收盤）、Yahoo 日線（2 年；加權指數 ^TWII 當
交易日曆）。背景執行緒每 30 分鐘看一次證交所日資料的日期，有新的交易日才整輪重算，寫成快照；
家人按的時候只讀快照（股票池以外的單檔才即時抓一次 Yahoo，有限流）。抓資料一律走 `_get_json`
（https、不轉址、限大小、驗 Content-Type）。

不 import main 或 fcn_eval（停用 FCN 不能連帶弄壞 /股票）。卡片上只有固定句型、程式算的數字、
官方名稱與代號，沒有使用者原文。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import math
import os
import re
import stat
import statistics
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
        number = float(value)
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
        footer.append(_text(TEXT_POOL_NOTE, size="sm", color="#555555"))
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


def parse_t86(payload, day: date) -> tuple[str, dict[str, int]]:
    """T86 一天：回 (狀態, {代號: 外資＋投信買賣超股數})。

    stat 不是 OK（例如「很抱歉，沒有符合條件的資料!」）→ T86_NOT_YET：^TWII 說是交易日，
    所以是還沒發布，整輪失敗重試，不往前補。不是 dict、日期不對、欄位找不到 → T86_BAD。
    """
    if not isinstance(payload, dict):
        return T86_BAD, {}
    if payload.get("stat") != "OK":
        return T86_NOT_YET, {}
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
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            continue
        if isinstance(close, bool) or not isinstance(close, (int, float)) or not math.isfinite(close) or close <= 0:
            continue
        try:
            day = datetime.fromtimestamp(stamp, _TPE).date()
        except (OverflowError, OSError, ValueError):
            continue
        bars[day] = float(close)
    return dict(sorted(bars.items()))


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


def window_closes(
    bars: dict[date, float], calendar: Sequence[date], t: date, close: float, prev_reference: Optional[float]
) -> Optional[tuple[list[float], list[float]]]:
    """年線窗、季線窗的收盤：^TWII 日曆上含 T 的最後 240／60 個交易日裡 Yahoo 有的收盤，
    最後接官方（證交所或櫃買）的 T 收盤。接不上、缺太多天 → None（⚪）。

    - Yahoo 早於 T 的最後一根必須是 T 的前一個交易日（停牌、缺資料就不接）。
    - Yahoo 也有 T 而且和官方收盤差 > 1%；官方前一日參考價（收盤−漲跌）和 Yahoo 前一天收盤
      差 > 15%（漲跌缺值就不比，只靠下一條）；窗內相鄰兩根差 > 25%（分割、減資沒對齊）→ None。
    - 不在日曆上的 Yahoo 日子不用；缺的日子不拿更早的補。
    """
    days = [d for d in calendar if d <= t][-YEAR_BARS:]
    if len(days) < YEAR_BARS or days[-1] != t or not (math.isfinite(close) and close > 0):
        return None
    prev_day = days[-2]
    before = [d for d in bars if d < t]
    if not before or max(before) != prev_day:
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
    全是英文字母 1–5 個 → 美股（後面有台股就用台股）。認不得就回 NOT_FOUND，不猜。
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
    return (US_SYMBOL, None) if saw_us else (NOT_FOUND, None)


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


class RoundFailed(Exception):
    """這一輪重算失敗（保留舊快照、退避）；blocked＝疑似被擋，退避拉長。"""

    def __init__(self, kind: str, *, blocked: bool = False):
        super().__init__(kind)
        self.kind = kind
        self.blocked = blocked


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
    except ValueError:
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
) -> Optional[dict]:
    """Yahoo 日線 2 年；代號不在白名單、兩個主機都失敗回 None；429 往上丟（整輪中止）。"""
    if not _YAHOO_SYMBOL_RE.fullmatch(symbol):
        return None
    for host in hosts:
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
        return WorkerState(next_try=time.time() + BACKOFF_S[-1], last_fail="state_corrupt")


# ── 執行期狀態（記憶體裡的快照是不可變物件，整個換掉） ──────────────────────────
_lock = threading.RLock()
_snapshot: Optional[Snapshot] = None
_worker = WorkerState()
_loaded = False
_thread: Optional[threading.Thread] = None
_stop: Optional[threading.Event] = None
_wake: Optional[threading.Event] = None
_BACKGROUND_ENABLED = True  # conftest 會關掉（測試不能起真的背景工作）


def _ensure_loaded() -> None:
    """第一次用到時才讀狀態檔。讀過就不再拿鎖：背景拿著鎖等檔案鎖時，回覆不能被卡住。"""
    global _snapshot, _worker, _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        _snapshot = load_snapshot_file()
        _worker = load_worker_file()
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
        _worker.next_try = max(_worker.next_try or 0.0, time.time() + BACKOFF_S[-1])
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
HISTORY_MAX_BYTES = 5 * 1024 * 1024  # 一天一行約 300 字，夠用幾十年


def _history_tail(path: Path) -> tuple[Optional[str], bool]:
    """(最後一筆讀得懂的紀錄的 T, 檔尾是不是換行)。只讀最後 4 KB；最後一行寫到一半就往前看；
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
            value = json.loads(raw).get("t")
            day = date.fromisoformat(value)
        except (ValueError, AttributeError, TypeError):
            continue
        if day > _taipei_today():  # 被改成未來日期（"9999-12-31"）也不算，免得從此不再記
            continue
        return value, ends_with_newline
    return None, ends_with_newline


def _append_history(snapshot: Snapshot) -> None:
    """一個交易日一行：T、大盤、推薦的代號／名稱／收盤／分數／季線乖離（沒有推薦也記一行）。
    日期只往前：最後一行的 T 已經 ≥ 這個 T 就不寫（重啟、同 T 重寫快照、別的程序先寫了）；
    寫不進去只記錯誤種類。"""
    path = STATE_DIR / HISTORY_NAME
    record = {
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
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        with _state_file_lock():
            last_t, ends_with_newline = _history_tail(path)
            if last_t is not None and last_t >= record["t"]:
                return
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
                        raise RoundFailed("t86_cooldown")  # 別的程序還在退避（併進來的期限）
                    recent = [x for x in _worker.t86_requests if now - x < 86400]
                    if len(recent) >= T86_DAILY_CAP:
                        raise RoundFailed("t86_budget")
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
            raise RoundFailed("budget_t86")
        if stop.wait(wait):
            raise RoundFailed("stopped")


def _t86_day(day: date, stop: threading.Event, give_up: float) -> dict[str, int]:
    """抓一天 T86（日快取有就用）。被擋（非 JSON、403、429、轉址）→ 整輪停、退避 2 小時。"""
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
            raise RoundFailed(f"t86_{exc.kind}", blocked=True) from None
        raise RoundFailed(f"t86_{exc.kind}") from None
    kind, table = parse_t86(payload, day) if status == 200 else (T86_BAD, {})
    if kind != T86_OK:
        raise RoundFailed(f"t86_{kind}")
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
            raise RoundFailed(f"yahoo_{errors[0]}", blocked=errors[0] == "rate_limited")
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


def refresh(t: date, day_rows: dict[str, DayRow], stop: threading.Event, *, now: Optional[float] = None) -> Snapshot:
    """用資料日 T 整輪重算；任何來源不齊、超過整輪期限就丟 RoundFailed（保留舊快照）。"""
    give_up = time.monotonic() + REFRESH_BUDGET_S

    def check_budget(stage: str) -> None:
        if stop.is_set():
            raise RoundFailed("stopped")
        if time.monotonic() > give_up:
            raise RoundFailed(f"budget_{stage}")

    # 1) 交易日曆＝加權指數日線（先截到 ≤ T，最後一根必須是 T）
    try:
        twii = _fetch_chart("^TWII", stop=stop)
    except FetchError as exc:
        raise RoundFailed(f"yahoo_{exc.kind}", blocked=exc.kind == "rate_limited") from None
    twii_bars = {d: v for d, v in parse_chart(twii).items() if d <= t}
    calendar = sorted(twii_bars)
    if not calendar or calendar[-1] != t:
        raise RoundFailed("twii_not_t")
    if len(calendar) < YEAR_BARS:
        raise RoundFailed("twii_short")
    # 2) 證交所批次
    try:
        check_budget("batch")
        _, company_rows, _ = _fetch_openapi("/v1/opendata/t187ap03_L", stop=stop, give_up=give_up)
        check_budget("batch")
        _, pe_rows, _ = _fetch_openapi("/v1/exchangeReport/BWIBBU_ALL", stop=stop, give_up=give_up)
        check_budget("batch")
        _, revenue_rows, _ = _fetch_openapi("/v1/opendata/t187ap05_L", stop=stop, give_up=give_up)
    except FetchError as exc:
        raise RoundFailed(f"batch_{exc.kind}", blocked=exc.kind in ("blocked", "rate_limited")) from None
    companies = parse_companies(company_rows)
    pe_date, pe = parse_pe(pe_rows)
    revenue = parse_revenue(revenue_rows)
    if len(companies) < _MIN_ROWS or len(revenue) < _MIN_ROWS:
        raise RoundFailed("batch_short")
    if pe_date != t:
        raise RoundFailed("pe_not_t")
    # 3) 外資＋投信：^TWII 日曆上含 T 的最後 10 個交易日，每天都要有；某一檔缺任何一天 → 那檔未知
    inst: dict[str, int] = {}
    days_seen: dict[str, int] = {}
    for day in calendar[-T86_DAYS:]:
        check_budget("t86")
        for code, net in _t86_day(day, stop, give_up).items():
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
    charts = _fetch_charts([_symbol(c, TWSE) for c in pool_codes], stop, give_up)
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
        pool[code] = _facts_for(code, row, t, windows, medians, cap=caps.get(code))
    gray = sum(1 for code in universe if pool.get(code) is None or pool[code].trend is None)
    if gray > TOP_N * MAX_GRAY_RATIO:
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
BACKOFF_S = (600, 1200, 2400, 3600)
BLOCKED_BACKOFF_S = 2 * 3600
# 讀工作狀態時把 next_try 夾在這個範圍內：新增更長的退避要一起改，不然重啟後會被夾短。
_MAX_BACKOFF_S = max(max(BACKOFF_S), BLOCKED_BACKOFF_S)
_CLOCK_SKEW_S = 600  # 允許的時鐘誤差（工作狀態裡比現在晚超過這麼多的時間一律夾回）
NEW_DATE_GRACE_S = 3 * 3600
CHECK_STALE_S = 36 * 3600
MIN_WAIT_S = 1.0
OTC_RETRY_S = 2 * 3600  # 櫃買清單沒抓到（或日期還不是 T）時，補抓的間隔
_otc_next_try = 0.0


def _fail(kind: str, *, blocked: bool = False) -> float:
    now = time.time()
    with _lock:
        _worker.fail_count += 1
        delay = BLOCKED_BACKOFF_S if blocked else BACKOFF_S[min(_worker.fail_count - 1, len(BACKOFF_S) - 1)]
        # 已經有更晚的期限（壞檔退避 1 小時、別的程序的冷卻）就留著，不縮短
        _worker.next_try = max(now + delay, _worker.next_try or 0.0)
        delay = _worker.next_try - now
        _worker.last_fail = kind[:40]
        if _snapshot is None:
            _worker.first_round_failed = True
        _save_worker()
        count = _worker.fail_count
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
        return _fail(f"day_{exc.kind}", blocked=exc.kind in ("blocked", "rate_limited"))
    if status == 304:
        with _lock:
            _worker.last_ok_check = now
            _worker.fail_count, _worker.last_fail = 0, ""  # 已經追上：之前檢查失敗的次數不再累計
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
        return _fail(exc.kind, blocked=exc.blocked)
    if stop.is_set():
        return POLL_S
    result = _commit_snapshot(new)
    if result == COMMIT_FAILED:
        return _fail("snapshot_write")
    with _lock:
        _worker.fail_count, _worker.next_try, _worker.last_fail = 0, None, ""
        _worker.first_round_failed = False
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
    while not stop.is_set():
        try:
            delay = _tick(stop)
        except Exception as exc:  # 最外層接住，不讓執行緒無聲停掉；記成失敗並退避（叫醒也要等）
            logger.error("stock picks tick crashed error_type=%s", type(exc).__name__)
            try:
                delay = _fail(f"crash_{type(exc).__name__}"[:40])
            except Exception:
                delay = float(BACKOFF_S[-1])
        wake.wait(max(MIN_WAIT_S, delay))
        wake.clear()


def start_background() -> None:
    """lifespan 啟動時呼叫；執行緒還活著就不再起（每次起都用新的 Event）。"""
    global _thread, _stop, _wake
    if not _BACKGROUND_ENABLED:
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop, _wake = threading.Event(), threading.Event()
        _thread = threading.Thread(target=_run, args=(_stop, _wake), daemon=True, name="stock-picks")
        _thread.start()


def stop_background(timeout: float = 2.0) -> None:
    with _lock:
        thread, stop, wake = _thread, _stop, _wake
    if stop is not None:
        stop.set()
    if wake is not None:
        wake.set()
    if thread is not None:
        thread.join(timeout=timeout)


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
    }
    if snapshot is None:
        info["status"] = "no_snapshot"
    elif _not_updated(snapshot, now, w):
        info["status"] = "not_updated"
    else:
        info["status"] = "ok"
    return info


# ── 回覆（main 只呼叫 handle；只讀快照，單檔查詢有限流的即時抓） ──────────────────
TEXT_NOT_READY = "股票推薦：資料準備中，請過幾分鐘再按一次"
TEXT_UNAVAILABLE = "股票推薦：暫時抓不到資料，請稍後再試"
TEXT_CARD_FAILED = "股票推薦：卡片暫時出不來，請稍後再試"
TEXT_EVAL_CARD_FAILED = "股票評估：卡片暫時出不來，請稍後再試"
TEXT_TOO_LONG = "股票評估：請只打一檔的代號或名稱，例如 /股票 2330"
# 名稱比對刻意收緊（不猜），所以不說「找不到這一檔」（爸媽會以為下市），而是請他只打代號或名稱。
TEXT_NOT_FOUND = "股票評估：認不出是哪一檔，請只打代號或名稱，例如 /股票 2330、/股票 台積電"
TEXT_US = "股票評估：美股還不能查，目前只有台股"
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
    global _otc_next_try
    _otc_next_try = 0.0
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
            _symbol(code, market), hosts=_YAHOO[:1], timeout=min(YAHOO_TIMEOUT_S, max(1.0, (remaining - 2.0) / 2))
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
    snapshot, first_failed = _snapshot, _worker.first_round_failed
    if snapshot is None:
        _wake_worker()
        return Reply(text=TEXT_UNAVAILABLE if first_failed else TEXT_NOT_READY)
    if text.strip(_TRAILING_PUNCT) in _PICKS_WORDS:
        return _picks_reply(snapshot)
    kind, code = resolve_query(text, snapshot.index)
    if kind == US_SYMBOL:
        return Reply(text=TEXT_US)
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
