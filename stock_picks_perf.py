"""推薦成績（2026-10-28，/股票 第 3 段）：台股、美股推薦之後第 5、20 個交易日的實際漲跌，和 0050／SPY 比。

Andrew 10/28 選了「查詢＋推薦＋推薦成績」。爸媽會把成績卡上的數字當成事實看，所以這裡的原則是
「每個輸入都確定了才算，算了就不改」（PLAN v5 §6）：

- 一筆成績＝（市場、規則、推薦日 t、rev、代號、h）。日曆確定、除權息資料涵蓋到、區間內每個交易日都有收盤，
  才算出來寫進成績帳；還不確定就「等」（不寫帳，下一輪再看）；確定是壞資料（收盤和紀錄對不上、公司行動、分割、
  隔離一直沒解除、價格已經拿不到、報酬不合理）就「凍結成跳過」：寫一行 skip，之後不再算，卡上寫出跳過幾次。
- 成績帳 state/stock_picks_perf_ledger.jsonl 只增不改：寫入拿 stock_picks._state_file_lock()，鎖內重讀帳、
  去掉已經有的鍵才 append（部署時新舊程序會短暫重疊）；讀不拿鎖，跳過寫到一半或看不懂的行，同一個鍵只算第一次寫的。
  彙總與卡片只看帳：價格庫刪掉、紀錄檔被截斷，已經算出來的成績都不變。
- 台股價格要趁還在的時候收：DB 的 tw_all 每檔只留最近 10 個交易日（market_pipeline_tw.KEEP_ALL_SESSIONS），
  還沒凍結、DB tw（0050 成分，約 300 個交易日的長歷史）又沒有的收盤，存進 state/stock_picks_perf_state.json 的
  價格庫，凍結後就刪。除權息不存，算的時候直接查 DB exrights（不會刪，還拿得到盤勢 job 事後修正的值）。
  美股 DB 每檔都有一年多，不存。
- 台股紀錄有兩份（舊規則 stock_picks_history.jsonl、AI 相關股 stock_picks_tw_ai_history.jsonl）：凍結時才決定
  每個 t 用哪一份（select_tw）；家人那天沒看到的「影子推薦」不算。
- 美股推薦的 worker（stock_picks_us）每 30 分鐘呼叫一次 update()，美股開關關著也照呼叫（價格收集不能停）；
  這裡不 import stock_picks_us。DB 只透過 stock_db_ro 唯讀；回覆路徑只讀結果檔（load_result），不碰 DB。

算法：
- 報酬 r＝第 h 個交易日的收盤 ÷ 推薦當天收盤 − 1。台股含股利：每對相鄰交易日 (前一天, 這一天] 之間的除權息
  （含落在休市日的）用「除權息前收盤 ÷ 參考價」還原在這一天；美股只算股價。基準：台股 0050（同樣還原）、美股 SPY。
- 日曆：台股 TAIEX 的日期，(t, T_h] 裡每個平日都要是 TAIEX 有收盤的日子或確定休市（證交所休市表、颱風臨時休市
  confirmed 或已過 10 天），T_h 之後還要有 2 個交易日；美股 SPY／VOO／QQQ 的日期聯集，T_h 之後還要有 4 個交易日，
  而且這一檔和 SPY 自己都有第 4 天的收盤（盤勢 job 每次把每檔最近 4 個交易日整段重抓、差 ≤ 0.5% 直接覆蓋：
  PLAN 的「至少 2 個」之外再等這段改寫結束，T_h 的收盤才是定案）。
- 已知限制（規則檔註明）：8% 這種小比例減資擋不住（11% 防護只擋大比例）；同一檔連續被推薦會算好幾次。
"""

from __future__ import annotations

import bisect
import contextlib
import json
import logging
import math
import os
import re
import stat
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import cached_property
from pathlib import Path
from typing import Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

import stock_db_ro
import stock_picks

logger = logging.getLogger("stock_picks_perf")
_TPE = ZoneInfo("Asia/Taipei")

# ── 檔名與格式 ─────────────────────────────────────────────────────────────
US_HISTORY_NAME = "stock_picks_us_history.jsonl"  # 美股推薦紀錄（stock_picks_us 寫；檔名以這裡為準）
RESULT_NAME = "stock_picks_perf.json"  # 彙總結果（小檔；卡片只讀這份）
LEDGER_NAME = "stock_picks_perf_ledger.jsonl"  # 只增不改的成績帳
STATE_NAME = "stock_picks_perf_state.json"  # 台股價格庫＋ai_flag_by_t＋ai_first_t
LEDGER_VERSION = 1  # 算法版本（帳的 v）：公式改了就升版，彙總只看目前版本，舊列留著備查
STATE_SCHEMA = 1
RESULT_SCHEMA = 1
LEDGER_MAX_BYTES = 10 * 1024 * 1024  # 一天十幾行、一行約 150 字，夠用幾十年；滿了記 error、summary 報
HISTORY_MAX_BYTES = 6 * 1024 * 1024  # 紀錄檔自己的上限是 5 MB（stock_picks.HISTORY_MAX_BYTES）：留一點餘裕
TMP_MAX_AGE_S = 3600.0  # 當機留下的暫存檔超過 1 小時清掉

# ── 算法（PLAN v5 §6） ──────────────────────────────────────────────────────
HORIZONS = (5, 20)  # 推薦後第幾個交易日
MIN_N = 5  # 次數少於這個，卡上寫「紀錄還不夠」
TW, US = "tw", "us"
TW_OLD_RULE = "v1"  # 舊規則紀錄沒有 rule 欄位
US_RULE = "us_v1"
TW_BENCHMARK, US_BENCHMARK = "0050", "SPY"
C0_TOLERANCE = 0.005  # 推薦當天的 DB 收盤和紀錄上的收盤差 ≤ 0.5%（不然就是對不上：mismatch）
TW_JUMP_LIMIT = 0.11  # 台股漲跌幅 10%：還原後相鄰兩個交易日漲跌超過 11%＝大比例減資、分割這類公司行動（jump）
EXRIGHT_RATIO = (1.0, 3.0)  # 除權息前收盤 ÷ 參考價只收 1–3（< 1 的現金增資除權、> 3 都當公司行動：jump）
R_RANGE = (-0.9, 3.0)  # r ≤ −90% 或 ≥ +300% 當資料錯（range）
TW_BUFFER_SESSIONS = 2  # T_h 之後日曆上還要有幾個交易日（後面先寫、前面後補的洞多半隔天就補上）
US_BUFFER_SESSIONS = 4  # 美股：盤勢 job 每天整段重抓最近 4 個交易日（market_pipeline_us 的 3＋1），過了才定案
SETTLE_SESSIONS = 10  # T_h 之後過了 10 個交易日：tw_all 已經滾掉、quarantine 也不再等，凍結成跳過
SKIP_REASONS = ("mismatch", "jump", "split", "quarantine", "missing", "range")
COVERAGE_SOURCE = "twt49u"  # 除權息（證交所 TWT49U）已經查到哪一天：盤勢 job 的 coverage 表
WAIT, FREEZE = "wait", "freeze"

_ISO_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TW_CODE_RE = re.compile(r"[0-9]{4,6}[A-Z]?")
_US_SYMBOL_RE = re.compile(r"[A-Z]{1,5}(?:\.[A-Z])?")  # DB 與紀錄的正規寫法（BRK.B）
_RULE_RE = re.compile(r"[a-z][a-z0-9_]{0,15}")
_MAX_PRICE = 1e6
_MAX_PICKS = 10  # 推薦最多 3 檔；一行超過 10 檔當壞行


# ── 小工具 ─────────────────────────────────────────────────────────────────
def _require(cond: bool) -> None:
    if not cond:
        raise ValueError("invalid perf value")


def _iso_day(value) -> Optional[str]:
    """YYYY-MM-DD 的合法日期原樣回傳，其他回 None（3.11 起 fromisoformat 也收「2026-W41-4」，所以先比格式）。"""
    if not isinstance(value, str) or not _ISO_RE.fullmatch(value):
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _is_int(value, lo: int, hi: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _finite(value, lo: float, hi: float) -> Optional[float]:
    """lo < x < hi 的有限數（bool 不算），其他回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and lo < number < hi else None


def _min_day(*days: Optional[str]) -> Optional[str]:
    present = [d for d in days if d is not None]
    return min(present) if present else None


def _code_re(market: str) -> re.Pattern:
    return _TW_CODE_RE if market == TW else _US_SYMBOL_RE


# ── 紀錄檔（三份；讀不拿鎖，壞行跳過） ─────────────────────────────────────────
@dataclass(frozen=True)
class Pick:
    code: str
    close: Optional[float]  # 紀錄上的收盤；壞掉是 None（凍結時當 mismatch：沒辦法確認家人看到的價格）


@dataclass(frozen=True)
class Record:
    """紀錄檔裡一個 t 的那一行（rev 最大的；一樣大取後面那行）。"""

    t: str
    rev: int
    rule: str
    picks: tuple


def _pick_from(obj, code_re: re.Pattern) -> Optional[Pick]:
    if not isinstance(obj, dict):
        return None
    code = obj.get("code")
    if not isinstance(code, str) or not code_re.fullmatch(code):
        return None
    return Pick(code, _finite(obj.get("close"), 0.0, _MAX_PRICE))


def _record_from(obj, *, market: str, fixed_rule: Optional[str], default_rule: str, today: str) -> Optional[Record]:
    if not isinstance(obj, dict):
        return None
    t = _iso_day(obj.get("t"))
    if t is None or t > today:  # 被改成未來日期的行不算（和 stock_picks._history_tail 一樣）
        return None
    rev = obj.get("rev")
    rev = 1 if rev is None else rev  # 計畫 C 之前的行沒有 rev：當 1
    if not _is_int(rev, 1, 10**6):
        return None
    raw_rule = obj.get("rule")
    rule = fixed_rule or (raw_rule if isinstance(raw_rule, str) and _RULE_RE.fullmatch(raw_rule) else default_rule)
    raw_picks = obj.get("picks")
    if not isinstance(raw_picks, list) or len(raw_picks) > _MAX_PICKS:
        return None
    picks: list[Pick] = []
    for item in raw_picks:
        pick = _pick_from(item, _code_re(market))
        if pick is not None and all(p.code != pick.code for p in picks):
            picks.append(pick)
    return Record(t, rev, rule, tuple(picks))


def parse_records(
    objs: Iterable, *, market: str, fixed_rule: Optional[str] = None, default_rule: str, today: str
) -> dict[str, Record]:
    """紀錄檔的每一行（已解析；壞行是 None）→ {t: Record}。fixed_rule 給了就固定用它（舊檔沒有 rule 欄位），
    不然用行裡的 rule（沒有就 default_rule）。同一個 t 取 rev 最大的那一行（家人最後看到的版本）、一樣大取後面那行。"""
    out: dict[str, Record] = {}
    for obj in objs:
        rec = _record_from(obj, market=market, fixed_rule=fixed_rule, default_rule=default_rule, today=today)
        if rec is None:
            continue
        old = out.get(rec.t)
        if old is None or rec.rev >= old.rev:
            out[rec.t] = rec
    return out


def select_tw(
    t: str, old: dict[str, Record], ai: dict[str, Record], flags: dict[str, bool], ai_first_t: Optional[str]
) -> Optional[Record]:
    """凍結時才決定台股的 t 用哪一份紀錄（PLAN v5 §6；r4 #1：第一次看到 t 就決定，會和 AI 紀錄晚寫的時序競態）：
    ① AI 檔有 t → 用它（已經取 rev 最大）；凍結時已經過了 T_h＋2 個交易日，AI 只重算最新的 t，這時已經定了。
    ② 記到 t 那天 AI 開著 → 不算：家人看到的是上一份 AI 推薦或「計算中」，舊檔那行是沒人看過的影子推薦。
    ③ 記到關著 → 舊檔。
    ④ 沒記到（本功能上線前、worker 沒在跑的日子）→ t 早於看過的第一個 AI 紀錄（或從來沒有 AI 檔）才用舊檔，否則不猜。
    回 None＝這一輪不算：不凍結，下一輪重判。"""
    if t in ai:
        return ai[t]
    flag = flags.get(t)
    if flag is True:
        return None
    if flag is False:
        return old.get(t)
    if ai_first_t is None or t < ai_first_t:
        return old.get(t)
    return None


# ── 一筆成績的計算（純函式） ─────────────────────────────────────────────────
@dataclass(frozen=True)
class Candidate:
    """還沒凍結的一筆成績。"""

    market: str
    rule: str
    t: str
    rev: int
    code: str
    close: Optional[float]
    h: int

    @property
    def key(self) -> tuple:
        return (LEDGER_VERSION, self.market, self.rule, self.t, self.rev, self.code, self.h)


def _row_key(row: dict) -> tuple:
    return (row["v"], row["m"], row["rule"], row["t"], row["rev"], row["code"], row["h"])


def candidates_of(selected: dict[tuple, Record], frozen: set) -> list[Candidate]:
    """選定的紀錄 → 還沒凍結的成績（每檔推薦 × 每個 h）。"""
    out: list[Candidate] = []
    for (market, t), rec in sorted(selected.items()):
        for pick in rec.picks:
            for h in HORIZONS:
                cand = Candidate(market, rec.rule, t, rec.rev, pick.code, pick.close, h)
                if cand.key not in frozen:
                    out.append(cand)
    return out


@dataclass(frozen=True, eq=False)
class MarketData:
    """一個市場這一輪從 DB（台股再加價格庫）讀到的東西；evaluate 只看這個。日期都是 ISO 字串。"""

    market: str
    calendar: tuple  # 交易日（舊到新）
    prices: dict  # {代號: {日期: 收盤}}（台股：tw 優先，其次 tw_all，最後價格庫）
    quarantine: dict  # {代號: [(from, to)]}：盤勢 job 判定有問題、還沒解除的區間
    closed_by_year: dict = field(default_factory=dict)  # 台股：{年: 確定沒交易的日子 | None（休市表讀不到）}
    coverage: tuple = ()  # 台股：twt49u 涵蓋的區間 [(from, to)]
    exrights: dict = field(default_factory=dict)  # 台股：{代號: [(日期, 除權息前收盤, 參考價)]}
    splits: dict = field(default_factory=dict)  # 美股：{代號: [分割生效日]}

    @cached_property
    def sessions(self) -> frozenset:
        return frozenset(self.calendar)


def _tw_calendar_certain(t: str, th: str, data: MarketData) -> bool:
    """[t, T_h] 的每個平日都是 TAIEX 有收盤的日子，或確定休市（r4 #2：TAIEX 會後面先寫、前面後補，
    只看「T_h 之後還有幾天」擋不住連續好幾天的洞）。用不到休市表的區間就不用等休市表。"""
    day, end = date.fromisoformat(t), date.fromisoformat(th)
    while day <= end:
        if day.weekday() < 5:
            iso = day.isoformat()
            if iso not in data.sessions:
                closed = data.closed_by_year.get(day.year)
                if closed is None or iso not in closed:
                    return False
        day += timedelta(days=1)
    return True


def _exrights_covered(spans: Sequence, t: str, th: str) -> bool:
    """除權息資料涵蓋 (t, T_h]（r3 #1：收盤和除權息不是同時進 DB，4552 晚 80 分鐘、0056 晚 5 天）。
    盤勢 job 會把重疊或相鄰的區間合併，所以要有一段從 t 的隔天以前一直涵蓋到 T_h。"""
    start = (date.fromisoformat(t) + timedelta(days=1)).isoformat()
    return any(a <= start and th <= z for a, z in spans)


def _c0_ok(c: Candidate, c0: float, data: MarketData) -> bool:
    """推薦當天的 DB 收盤要和家人看到的一樣（差 ≤ 0.5%）。美股在 t 之後任何時候有分割就不比：
    盤勢 job 會整段重抓、改成調整後的價格（PLAN v5 #12）；落在 (t, T_h] 的分割前面已經凍結成跳過。"""
    if c.market == US and any(d > c.t for d in data.splits.get(c.code, ())):
        return True
    return c.close is not None and abs(c0 / c.close - 1.0) <= C0_TOLERANCE + 1e-9


def _growth(market: str, series: dict, days: Sequence[str], events: Sequence) -> Optional[float]:
    """days[0]（推薦日）到 days[-1]（T_h）的報酬；台股含除權息還原，任一對相鄰交易日還原後漲跌超過 11%、
    或除權息比值不在 1–3 → None（公司行動，凍結成 jump）。美股只算股價、不套跳動閘門（真的大跌要算進去，r3 #3）。"""
    c0, ch = series[days[0]], series[days[-1]]
    if market == US:
        return ch / c0 - 1.0
    total = 1.0
    for prev, cur in zip(days, days[1:]):
        step = 1.0
        for day, pre, ref in events:
            if prev < day <= cur:  # 落在休市日的除權息也算在下一個交易日（r3 #14）
                ratio = pre / ref if ref > 0 else math.inf
                if not EXRIGHT_RATIO[0] <= ratio <= EXRIGHT_RATIO[1]:
                    return None
                step *= ratio
        if abs(series[cur] * step / series[prev] - 1.0) > TW_JUMP_LIMIT:
            return None
        total *= step
    return ch * total / c0 - 1.0


def evaluate(c: Candidate, data: MarketData) -> tuple[str, object]:
    """一筆成績這一輪的結果：(WAIT, 原因) 或 (FREEZE, 帳的一行)。順序：到期 → 日曆 → 除權息涵蓋 → 分割 →
    隔離 → 缺價 → 收盤對不對得上 → 公司行動 → 報酬範圍。"""
    cal = data.calendar
    first = bisect.bisect_right(cal, c.t)
    last = first + c.h - 1
    buffer = TW_BUFFER_SESSIONS if c.market == TW else US_BUFFER_SESSIONS
    if last + buffer >= len(cal):
        return WAIT, "not_due"
    th = cal[last]
    settled = len(cal) - 1 - last >= SETTLE_SESSIONS
    codes = (c.code, TW_BENCHMARK if c.market == TW else US_BENCHMARK)
    if c.market == TW:
        if not _tw_calendar_certain(c.t, th, data):
            return WAIT, "calendar"
        if not _exrights_covered(data.coverage, c.t, th):
            return WAIT, "exrights"
    elif not settled and any(cal[last + buffer] not in data.prices.get(code, {}) for code in codes):
        # 盤勢 job 照字母分批寫：日曆（SPY）已經有那一天，這一檔的批次可能還沒跑；它跑的那一次會把 T_h 再整段
        # 重抓一次（[前一次有效日 − 3 個交易日, D]），之後才不會再改。過了 10 個交易日就不再看這一條。
        return WAIT, "not_due"

    def frozen(skip: Optional[str] = None, r: Optional[float] = None, b: Optional[float] = None) -> tuple:
        return FREEZE, {"v": LEDGER_VERSION, "m": c.market, "rule": c.rule, "t": c.t, "rev": c.rev,
                        "code": c.code, "h": c.h, "th": th, "r": r, "b": b, "skip": skip}

    if c.market == US and any(c.t < d <= th for code in codes for d in data.splits.get(code, ())):
        return frozen("split")
    if any(z > c.t and a <= th for code in codes for a, z in data.quarantine.get(code, ())):
        return frozen("quarantine") if settled else (WAIT, "quarantine")
    days = (c.t, *cal[first:last + 1])
    series = [data.prices.get(code, {}) for code in codes]
    if any(day not in s for s in series for day in days):
        return frozen("missing") if settled else (WAIT, "price")
    if not _c0_ok(c, series[0][c.t], data):
        return frozen("mismatch")
    r, b = (_growth(c.market, s, days, data.exrights.get(code, ())) for code, s in zip(codes, series))
    if r is None or b is None:
        return frozen("jump")
    if not (R_RANGE[0] < r < R_RANGE[1] and R_RANGE[0] < b < R_RANGE[1]):
        return frozen("range")
    return frozen(r=round(r, 6), b=round(b, 6))


# ── 台股價格庫（純函式） ─────────────────────────────────────────────────────
def needed_prices(pending: Iterable[Candidate]) -> dict[str, str]:
    """還沒凍結的台股要留哪些價格：{代號: 最早的 t}；基準 0050 從最早那一筆開始。"""
    out: dict[str, str] = {}
    for c in pending:
        if c.market == TW:
            out[c.code] = min(out.get(c.code, c.t), c.t)
    if out:
        earliest = min(out.values())
        out[TW_BENCHMARK] = min(out.get(TW_BENCHMARK, earliest), earliest)
    return out


def next_library(old: dict, long: dict, recent: dict, needed: dict[str, str]) -> dict:
    """新的價格庫：只留還沒凍結的代號、從它最早的 t 起、DB tw 沒有的日期；tw_all 有的以 DB 為準（r4 #3）。"""
    out: dict[str, dict] = {}
    for code, since in needed.items():
        have = long.get(code, {})
        series = {d: v for d, v in old.get(code, {}).items() if d >= since and d not in have}
        series.update({d: v for d, v in recent.get(code, {}).items() if d >= since and d not in have})
        if series:
            out[code] = dict(sorted(series.items()))
    return out


def _merge_prices(a: dict, b: dict) -> dict:
    return {code: {**a.get(code, {}), **b.get(code, {})} for code in set(a) | set(b)}


# ── 彙總（純函式） ───────────────────────────────────────────────────────────
def aggregate(
    rows: Sequence[dict],
    selections: dict[tuple, Record],
    pending: Sequence[Candidate],
    first_t: dict[str, Optional[str]],
    *,
    now: float,
    ledger_rows: int,
    ledger_full: bool,
) -> dict:
    """帳（已依鍵去重、先寫的算）→ 結果。每個（市場、t、h）只算一組（rule、rev）：紀錄檔現在選得出來源 →
    只算那一組、而且代號還在那一行裡（r3 #5：紀錄被改寫後舊代號的列不算）；選不出來（紀錄被截斷、這個 t 不算）
    → 帳裡最後寫的那一組。這樣不會同一天算兩套，價格庫或紀錄檔不見了，已經算出來的也不會消失。"""
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        if row["v"] == LEDGER_VERSION and row["h"] in HORIZONS:
            groups.setdefault((row["m"], row["t"], row["h"]), []).append(row)
    chosen: list[dict] = []
    for (market, t, _h), group in groups.items():
        rec = selections.get((market, t))
        if rec is not None:
            codes = {p.code for p in rec.picks}
            chosen += [r for r in group if r["rule"] == rec.rule and r["rev"] == rec.rev and r["code"] in codes]
        else:
            last = group[-1]
            chosen += [r for r in group if r["rule"] == last["rule"] and r["rev"] == last["rev"]]
    markets: dict[str, dict] = {}
    skipped_total = 0
    for market in (TW, US):
        mine = [r for r in chosen if r["m"] == market]
        counted = [r for r in mine if r["skip"] is None]
        ai_days = [r["t"] for r in counted if r["rule"].startswith("ai_")]
        mixed = bool(ai_days) and any(not r["rule"].startswith("ai_") for r in counted)
        by_h: dict[str, dict] = {}
        for h in HORIZONS:
            ok = [r for r in counted if r["h"] == h]
            n = len(ok)
            skipped = sum(1 for r in mine if r["h"] == h and r["skip"] is not None)
            skipped_total += skipped
            by_h[str(h)] = {
                "n": n,
                # 平均先算、最後才四捨五入到 0.1%（千分位整數；卡片和判斷都用這個整數）
                "r_pm": stock_picks._permille(math.fsum(r["r"] for r in ok) / n) if n else None,
                "b_pm": stock_picks._permille(math.fsum(r["b"] for r in ok) / n) if n else None,
                "wins": sum(1 for r in ok if r["r"] > r["b"]),
                "skipped": skipped,
                "pending": sum(1 for c in pending if c.market == market and c.h == h),
            }
        markets[market] = {
            "first_t": first_t.get(market),
            "from": min((r["t"] for r in counted), default=None),  # 卡上的「起」：實際算進去的最早一筆（r3 #6）
            "to": max((r["th"] for r in counted), default=None),
            "ai_from": min(ai_days) if mixed else None,
            "h": by_h,
        }
    return {
        "schema": RESULT_SCHEMA,
        "computed_at": int(now),
        "ledger_rows": ledger_rows,
        "ledger_full": ledger_full,
        "pending": len(pending),
        "skipped": skipped_total,
        "markets": markets,
    }


def _check_result(obj) -> dict:
    """結果檔（或 update 的回傳值）的格式；不對丟 ValueError。"""
    _require(isinstance(obj, dict) and obj.get("schema") == RESULT_SCHEMA)
    _require(_is_int(obj.get("computed_at"), 0, 10**11) and isinstance(obj.get("ledger_full"), bool))
    _require(all(_is_int(obj.get(k), 0, 10**9) for k in ("ledger_rows", "pending", "skipped")))
    markets = obj.get("markets")
    _require(isinstance(markets, dict))
    for market in (TW, US):
        info = markets.get(market)
        _require(isinstance(info, dict) and isinstance(info.get("h"), dict))
        _require(all(info.get(k) is None or _iso_day(info.get(k)) for k in ("first_t", "from", "to", "ai_from")))
        _require((info.get("from") is None) == (info.get("to") is None))
        for h in HORIZONS:
            s = info["h"].get(str(h))
            _require(isinstance(s, dict) and _is_int(s.get("n"), 0, 10**9))
            n = s["n"]
            _require(_is_int(s.get("wins"), 0, n) and _is_int(s.get("skipped"), 0, 10**9))
            _require(_is_int(s.get("pending"), 0, 10**9))
            for k in ("r_pm", "b_pm"):
                _require((s.get(k) is None) == (n == 0) and (s.get(k) is None or _is_int(s.get(k), -1000, 10**6)))
    return obj


def _checked_or_none(result) -> Optional[dict]:
    if result is None:
        return None
    try:
        return _check_result(result)
    except ValueError:
        logger.warning("stock picks perf result invalid; showing computing card")
        return None


# ── 檔案（成績帳、狀態檔、紀錄檔） ───────────────────────────────────────────
def _read_jsonl(path: Path, max_bytes: int) -> tuple[list, int, bool]:
    """(每行解析後的物件（壞行是 None）, 檔案大小, 檔尾是不是換行)。檔案不存在回 ([], 0, True)。
    不跟隨 symlink、只讀一般檔、限大小；其他讀取問題丟例外（呼叫端決定這一輪怎麼辦）。"""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return [], 0, True
    with os.fdopen(fd, "rb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError("not a small regular file")
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("file too large")
    objs: list = []
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            objs.append(json.loads(raw))
        except (ValueError, RecursionError):  # 寫到一半的最後一行、被改壞的行（UnicodeDecodeError 也是 ValueError）
            objs.append(None)
    return objs, len(data), not data or data.endswith(b"\n")


def _read_records(path: Path, market: str, *, fixed_rule: Optional[str], default_rule: str,
                  today: str) -> Optional[dict[str, Record]]:
    """讀不了（權限、I/O、太大）回 None：那個市場這一輪不凍結（r4 #1），不是當成沒有紀錄。"""
    try:
        objs, _, _ = _read_jsonl(path, HISTORY_MAX_BYTES)
    except Exception as exc:
        logger.warning("stock picks perf history unreadable name=%s error_type=%s", path.name, type(exc).__name__)
        return None
    return parse_records(objs, market=market, fixed_rule=fixed_rule, default_rule=default_rule, today=today)


@dataclass
class _Ledger:
    rows: list  # 讀得懂的列（同一個鍵只留第一次寫的）
    keys: set
    size: int
    ends_with_newline: bool


def _ledger_row(obj) -> Optional[dict]:
    if not isinstance(obj, dict):
        return None
    m, rule, code, skip = obj.get("m"), obj.get("rule"), obj.get("code"), obj.get("skip")
    ok = (
        _is_int(obj.get("v"), 1, 1000) and m in (TW, US)
        and isinstance(rule, str) and _RULE_RE.fullmatch(rule) is not None
        and _iso_day(obj.get("t")) is not None and _is_int(obj.get("rev"), 1, 10**6)
        and isinstance(code, str) and _code_re(m).fullmatch(code) is not None
        and _is_int(obj.get("h"), 1, 1000) and _iso_day(obj.get("th")) is not None
    )
    if not ok:
        return None
    if skip is None:
        r, b = _finite(obj.get("r"), -1.0, 100.0), _finite(obj.get("b"), -1.0, 100.0)
        if r is None or b is None:
            return None
    elif skip in SKIP_REASONS:
        r = b = None
    else:
        return None
    return {"v": obj["v"], "m": m, "rule": rule, "t": obj["t"], "rev": obj["rev"], "code": code, "h": obj["h"],
            "th": obj["th"], "r": r, "b": b, "skip": skip}


def _read_ledger(path: Path) -> _Ledger:
    objs, size, ends_with_newline = _read_jsonl(path, LEDGER_MAX_BYTES + 64 * 1024)
    rows: list[dict] = []
    keys: set = set()
    for obj in objs:
        row = _ledger_row(obj)
        if row is None:
            continue
        key = _row_key(row)
        if key in keys:  # 同一個鍵只算第一次寫的
            continue
        keys.add(key)
        rows.append(row)
    return _Ledger(rows, keys, size, ends_with_newline)


def _append_rows(path: Path, ledger: _Ledger, new_rows: Sequence[dict]) -> tuple[list[dict], bool]:
    """鎖內呼叫，ledger 是鎖內剛重讀的帳：去掉已經有的鍵（別的程序剛寫的）才 append。
    回 (真的寫進去的列, 帳滿了沒有)；滿了整批不寫（下一輪照樣是「等」，價格庫也還留著）。"""
    fresh: list[dict] = []
    seen = set(ledger.keys)
    for row in new_rows:
        key = _row_key(row)
        if key not in seen:
            seen.add(key)
            fresh.append(row)
    if not fresh:
        return [], ledger.size >= LEDGER_MAX_BYTES
    block = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in fresh).encode("utf-8")
    if not ledger.ends_with_newline:  # 上一行寫到一半就當機：先補換行，不黏在後面
        block = b"\n" + block
    if ledger.size + len(block) > LEDGER_MAX_BYTES:
        logger.error("stock picks perf ledger is full; rows not appended n=%d", len(fresh))
        return [], True
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "ab") as fh:
        fh.write(block)
        fh.flush()
        os.fsync(fh.fileno())
    return fresh, ledger.size + len(block) >= LEDGER_MAX_BYTES


@dataclass(frozen=True)
class _State:
    prices: dict  # 台股價格庫 {代號: {日期: 收盤}}
    flags: dict  # ai_flag_by_t {T: 那時 AI 開著沒有}：只在 T 是舊檔最新一行時記，記過不改
    ai_first_t: Optional[str]  # 看過的 AI 紀錄最小的 t（AI 檔被刪或截斷時，AI 的日子不會被改判成舊規則）


_EMPTY_STATE = _State({}, {}, None)


def _state_from_json(obj) -> _State:
    _require(isinstance(obj, dict) and obj.get("schema") == STATE_SCHEMA)
    raw_prices, raw_flags, first = obj.get("prices"), obj.get("ai_flag_by_t"), obj.get("ai_first_t")
    _require(isinstance(raw_prices, dict) and isinstance(raw_flags, dict))
    prices: dict[str, dict] = {}
    for code, series in raw_prices.items():
        _require(isinstance(code, str) and _TW_CODE_RE.fullmatch(code) is not None and isinstance(series, dict))
        clean = {}
        for day, close in series.items():
            value = _finite(close, 0.0, _MAX_PRICE)
            _require(_iso_day(day) is not None and value is not None)
            clean[day] = value
        prices[code] = clean
    flags = {}
    for day, flag in raw_flags.items():
        _require(_iso_day(day) is not None and isinstance(flag, bool))
        flags[day] = flag
    _require(first is None or _iso_day(first) is not None)
    return _State(prices, flags, first)


def _state_to_json(state: _State) -> dict:
    return {
        "schema": STATE_SCHEMA,
        "prices": {code: dict(sorted(series.items())) for code, series in sorted(state.prices.items())},
        "ai_flag_by_t": dict(sorted(state.flags.items())),
        "ai_first_t": state.ai_first_t,
    }


def _load_state(path: Path) -> _State:
    """壞檔當作空的重來（記 error）：價格庫可以從 tw_all 補回最近 10 天；ai_first_t 另外從帳裡的 AI 列補下限。"""
    try:
        obj = stock_picks._read_json_file(path)
        return _EMPTY_STATE if obj is None else _state_from_json(obj)
    except Exception as exc:
        logger.error("stock picks perf state unreadable; starting empty error_type=%s", type(exc).__name__)
        return _EMPTY_STATE


def _sweep_stale_tmp(state_dir: Path) -> None:
    """_write_json_file 當機留下的暫存檔（.stock_picks_perf*.tmp）超過 1 小時就清掉（看檔案的真實時間）。"""
    with contextlib.suppress(OSError):
        for tmp in state_dir.glob(".stock_picks_perf*.tmp"):
            with contextlib.suppress(OSError):
                if time.time() - tmp.lstat().st_mtime > TMP_MAX_AGE_S:
                    tmp.unlink()


# ── DB（唯讀；每個查詢都是 stock_db_ro 的一個短 SELECT） ───────────────────────
def _clean_series(table: dict) -> dict:
    """DB 的收盤只收有限正數（欄位型別變了就當沒有這一根）。"""
    return {code: {d: float(v) for d, v in series.items() if _iso_day(d) and _finite(v, 0.0, _MAX_PRICE)}
            for code, series in table.items()}


def _load_tw(cands: Sequence[Candidate], library: dict, today: str, db_path: Optional[Path]) -> tuple:
    """台股這一輪要用的 DB 資料：(MarketData, tw 收盤, tw_all 收盤)。後兩個給價格庫更新用。"""
    since = min(c.t for c in cands)
    codes = sorted({c.code for c in cands} | {TW_BENCHMARK})
    calendar = tuple(d for d in stock_db_ro.tw_calendar(since, path=db_path) if _iso_day(d))
    last_year = int((calendar[-1] if calendar else today)[:4])
    closed_by_year = {}
    for year in range(int(since[:4]), last_year + 1):  # 一年一年查：明年的表還沒公布，不擋今年的區間
        closed = stock_db_ro.tw_closed_days([year], today=today, path=db_path)
        closed_by_year[year] = None if closed is None else frozenset(closed)
    spans = tuple(
        (a, z) for a, z in stock_db_ro.rows(
            "SELECT from_date, to_date FROM coverage WHERE source = ?", (COVERAGE_SOURCE,), path=db_path)
        if _iso_day(a) and _iso_day(z)
    )
    long = _clean_series(stock_db_ro.closes("tw", codes, since, path=db_path))
    recent = _clean_series(stock_db_ro.closes("tw_all", codes, since, path=db_path))
    events = stock_db_ro.exrights(codes, since, path=db_path)
    quarantine: dict[str, list] = {}
    for market in ("tw", "tw_all"):
        for code, spans_q in stock_db_ro.active_quarantine(market, codes, path=db_path).items():
            quarantine.setdefault(code, []).extend(spans_q)
    prices = {code: {**library.get(code, {}), **recent.get(code, {}), **long.get(code, {})} for code in codes}
    data = MarketData(TW, calendar, prices, quarantine, closed_by_year, spans, events)
    return data, long, recent


def _load_us(cands: Sequence[Candidate], db_path: Optional[Path]) -> MarketData:
    since = min(c.t for c in cands)
    codes = sorted({c.code for c in cands} | {US_BENCHMARK})
    calendar = tuple(d for d in stock_db_ro.us_calendar(since, path=db_path) if _iso_day(d))
    prices = _clean_series(stock_db_ro.closes("us", codes, since, path=db_path))
    splits = stock_db_ro.splits_after(codes, since, path=db_path)
    quarantine = stock_db_ro.active_quarantine("us", codes, path=db_path)
    return MarketData(US, calendar, prices, quarantine, splits=splits)


# ── 對外：背景每輪更新、回覆讀結果 ───────────────────────────────────────────
def update(*, ai_enabled: bool, now: float, db_path: Optional[Path] = None) -> Optional[dict]:
    """美股 worker 每輪呼叫（美股開關關著也呼叫）：讀三份紀錄 → 記 ai_flag_by_t／ai_first_t → 凍結到期的成績 →
    append 帳 → 更新台股價格庫 → 彙總 → 寫結果檔；回結果。ai_enabled 是 stock_picks.AI_ENABLED（家人此刻看到哪一種
    台股推薦）。任何失敗只記 log、回 None，不丟例外（美股推薦照常）。"""
    try:
        return _update(bool(ai_enabled), float(now), db_path)
    except TimeoutError:
        logger.warning("stock picks perf update skipped reason=lock_busy")
        return None
    except Exception as exc:
        logger.error("stock picks perf update failed error_type=%s", type(exc).__name__)
        return None


def _update(ai_enabled: bool, now: float, db_path: Optional[Path]) -> dict:
    state_dir = stock_picks.STATE_DIR
    today = datetime.fromtimestamp(now, _TPE).date().isoformat()
    _sweep_stale_tmp(state_dir)
    tw_old = _read_records(state_dir / stock_picks.HISTORY_NAME, TW, fixed_rule=TW_OLD_RULE,
                           default_rule=TW_OLD_RULE, today=today)
    tw_ai = _read_records(state_dir / stock_picks.AI_HISTORY_NAME, TW, fixed_rule=None,
                          default_rule=stock_picks.AI_RULE, today=today)
    us_hist = _read_records(state_dir / US_HISTORY_NAME, US, fixed_rule=None, default_rule=US_RULE, today=today)
    ledger_path, state_path = state_dir / LEDGER_NAME, state_dir / STATE_NAME
    ledger = _read_ledger(ledger_path)  # 讀不了就丟出去：沒有帳就不能去重，這一輪整個不做
    start = _load_state(state_path)

    # 1. 選來源（凍結時才決定；這裡只記一個布林和一個日期）
    flags, ai_first = dict(start.flags), start.ai_first_t
    selections: dict[tuple, Record] = {}
    tw_readable = tw_old is not None and tw_ai is not None
    if tw_readable:
        if tw_old:
            flags.setdefault(max(tw_old), ai_enabled)  # 只記家人此刻看到的那個 T；晚才看到的舊 T 不補記
        ledger_ai = [r["t"] for r in ledger.rows if r["m"] == TW and r["rule"].startswith("ai_")]
        ai_first = _min_day(ai_first, min(tw_ai, default=None), min(ledger_ai, default=None))
        for t in set(tw_old) | set(tw_ai):
            rec = select_tw(t, tw_old, tw_ai, flags, ai_first)
            if rec is not None:
                selections[(TW, t)] = rec
    if us_hist is not None:
        selections.update({(US, t): rec for t, rec in us_hist.items()})
    cands = candidates_of(selections, ledger.keys)

    # 2. 算到期的（DB 查詢都在鎖外）
    new_rows: list[dict] = []
    waits: Counter = Counter()
    tw_long: dict = {}
    tw_recent: dict = {}
    for market in (TW, US):
        mine = [c for c in cands if c.market == market]
        if not mine:
            continue
        try:
            if market == TW:
                data, tw_long, tw_recent = _load_tw(mine, start.prices, today, db_path)
            else:
                data = _load_us(mine, db_path)
        except stock_db_ro.DbBusy:  # 盤勢 job 正在 commit：等下一輪，不算失敗
            waits[f"{market}:db_busy"] += len(mine)
            continue
        except stock_db_ro.DbUnavailable as exc:
            logger.warning("stock picks perf db unavailable market=%s kind=%s", market, exc.kind)
            waits[f"{market}:db_unavailable"] += len(mine)
            continue
        for cand in mine:
            kind, value = evaluate(cand, data)
            if kind == FREEZE:
                new_rows.append(value)
            else:
                waits[f"{market}:{value}"] += 1

    # 3. 寫檔（同一把跨程序鎖，鎖內只讀檔、寫檔）
    with stock_picks._state_file_lock():
        disk = _read_ledger(ledger_path)
        appended, full = _append_rows(ledger_path, disk, new_rows)
        rows = disk.rows + appended
        frozen = disk.keys | {_row_key(r) for r in appended}
        pending = [c for c in cands if c.key not in frozen]
        disk_state = _load_state(state_path)
        library = _merge_prices(start.prices, disk_state.prices)
        if tw_readable:  # 台股紀錄這輪讀不了：價格庫原樣留著，不能因為「看不到待算的」就刪光
            library = next_library(library, tw_long, tw_recent, needed_prices(pending))
        merged = _State(library, {**flags, **disk_state.flags}, _min_day(ai_first, disk_state.ai_first_t))
        payload = _state_to_json(merged)
        if payload != _state_to_json(disk_state):  # 內容沒變就不寫
            stock_picks._write_json_file(state_path, payload)
        first_t = {TW: min(set(tw_old or {}) | set(tw_ai or {}), default=None), US: min(us_hist or {}, default=None)}
        result = aggregate(rows, selections, pending, first_t, now=now, ledger_rows=len(rows), ledger_full=full)
        stock_picks._write_json_file(state_dir / RESULT_NAME, result)
    logger.info("stock picks perf updated appended=%d pending=%d waits=%s",
                len(appended), len(pending), ",".join(f"{k}:{v}" for k, v in sorted(waits.items())) or "-")
    return result


def load_result() -> Optional[dict]:
    """結果檔（背景啟動時讀一次；之後用 update 的回傳值）。沒有、壞掉都回 None（卡片寫「計算中」）。"""
    try:
        obj = stock_picks._read_json_file(stock_picks.STATE_DIR / RESULT_NAME)
        return None if obj is None else _check_result(obj)
    except Exception as exc:
        logger.warning("stock picks perf result unreadable error_type=%s", type(exc).__name__)
        return None


def summary() -> dict:
    """每日維護用：{perf_date（最後一次算出結果的台北日期）, ledger_rows, ledger_full, pending, skipped}。
    只讀結果檔：不開 DB、不寫檔。"""
    result = load_result()
    if result is None:
        return {"perf_date": None, "ledger_rows": None, "ledger_full": False, "pending": None, "skipped": None}
    return {
        "perf_date": datetime.fromtimestamp(result["computed_at"], _TPE).date().isoformat(),
        "ledger_rows": result["ledger_rows"],
        "ledger_full": result["ledger_full"],
        "pending": result["pending"],
        "skipped": result["skipped"],
    }


# ── 卡片（固定句型＋程式算的數字；沒有使用者原文、不寫買賣建議） ───────────────────
TITLE = f"{stock_picks.PREFIX}推薦成績"
TEXT_COMPUTING = "推薦成績計算中，請稍後再按一次"
TEXT_METHOD = (f"算法：推薦當天收盤，到之後第 {'、'.join(str(h) for h in HORIZONS)} 個交易日的收盤；"
               "同一檔連續被推薦會算好幾次")
TEXT_BASIS = f"台股含股利（除權息還原）、和 {TW_BENCHMARK} 比；美股只算股價、和 {US_BENCHMARK} 比"
TEXT_PAST = f"過去的表現不代表以後；{stock_picks.TEXT_DISCLAIMER}"
_GRAY = "#555555"
_MARKET_NAME = {TW: "台股", US: "美股"}
_BENCHMARK = {TW: TW_BENCHMARK, US: US_BENCHMARK}
_DATE_NOTE = {TW: "", US: "美國時間 "}  # 美股的推薦日是美東日期


def _md(day: str) -> str:
    return stock_picks._fmt_date(date.fromisoformat(day))


def _move(pm: int) -> str:
    """千分位整數 → 「漲 1.8%」「跌 2.8%」「持平」。"""
    if pm > 0:
        return f"漲 {stock_picks._fmt_tenths(pm)}%"
    if pm < 0:
        return f"跌 {stock_picks._fmt_tenths(pm)}%"
    return "持平"


def _skipped_line(n: int) -> str:
    return f"另有 {n} 次因為資料問題沒有算進去"


def _market_lines(market: str, info: dict) -> list[dict]:
    name, bench, note = _MARKET_NAME[market], _BENCHMARK[market], _DATE_NOTE[market]
    stats = [(h, info["h"][str(h)]) for h in HORIZONS]
    if info["from"] is not None:
        head = f"{name}推薦（{note}{_md(info['from'])} 起，算到 {_md(info['to'])}）"
    else:
        head = f"{name}推薦還沒有成績" + (f"（{note}{_md(info['first_t'])} 開始記）" if info["first_t"] else "")
    lines = [stock_picks._text(head, weight="bold")]
    if info["from"] is None and not any(s["skipped"] for _, s in stats):
        return lines
    if info["ai_from"]:
        lines.append(stock_picks._text(f"{_md(info['ai_from'])} 起改成 AI 相關股", size="md", color=_GRAY))
    for h, s in stats:
        if s["n"] < MIN_N:
            lines.append(stock_picks._text(f"推薦後 {h} 個交易日：紀錄還不夠（目前 {s['n']} 次）"))
        else:
            lines.append(stock_picks._text(
                f"推薦後 {h} 個交易日：平均{_move(s['r_pm'])}，{bench} 同期{_move(s['b_pm'])}"))
            lines.append(stock_picks._text(f"{s['n']} 次推薦裡有 {s['wins']} 次比 {bench} 好", size="md"))
        if s["skipped"]:
            lines.append(stock_picks._text(_skipped_line(s["skipped"]), size="md", color=_GRAY))
    return lines


def perf_bubble(result: Optional[dict]) -> dict:
    """成績卡（標題「股票推薦：推薦成績」）；result 是 None 或格式不對 →「推薦成績計算中」。"""
    checked = _checked_or_none(result)
    if checked is None:
        body = [stock_picks._text(TEXT_COMPUTING, size="xl", weight="bold")]
        footer = [stock_picks._text(stock_picks.TEXT_DISCLAIMER, size="sm", color=_GRAY)]
        return stock_picks._bubble(TITLE, None, body, footer)
    body: list[dict] = []
    for market in (TW, US):
        if body:
            body.append(stock_picks._separator())
        body += _market_lines(market, checked["markets"][market])
    footer = [stock_picks._text(text, size="sm", color=_GRAY) for text in (TEXT_METHOD, TEXT_BASIS, TEXT_PAST)]
    return stock_picks._bubble(TITLE, None, body, footer)


def _alt_part(market: str, info: dict) -> str:
    name, bench = _MARKET_NAME[market], _BENCHMARK[market]
    h = HORIZONS[0]
    s = info["h"][str(h)]
    if s["n"] >= MIN_N:
        return f"{name}推薦後 {h} 個交易日平均{_move(s['r_pm'])}（{bench} {_move(s['b_pm'])}）"
    if info["from"] is not None:
        return f"{name}推薦後 {h} 個交易日紀錄還不夠（目前 {s['n']} 次）"
    return f"{name}推薦還沒有成績"


def perf_alt_text(result: Optional[dict]) -> str:
    """通知與聊天列表只看得到這行：「股票推薦：只供參考｜推薦成績｜…」（≤ 400 字：每段長度固定、數字有上限）。"""
    checked = _checked_or_none(result)
    head = f"{stock_picks.PREFIX}只供參考｜推薦成績｜"
    if checked is None:
        return head + "計算中，請稍後再按一次"
    return head + "｜".join(_alt_part(m, checked["markets"][m]) for m in (TW, US))
