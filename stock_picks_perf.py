"""推薦成績（2026-10-28 /股票 第 3 段；2026-10-29 改成 v2）：台股、美股推薦「隔天收盤」進場，到之後第 5、20、60、240
個交易日的實際漲跌，和 0050／SPY 比。

Andrew 10/28 選了「查詢＋推薦＋推薦成績」；同一晚回測的討論定了 v2（回測 PLAN 2026-10-29 §0 第 1、2 題）：持有 5／20／60／240
個交易日（週／月／季／年）、進場價＝推薦隔天的收盤。家人的成績卡只顯示 5、20 日（CARD_HORIZONS）；60、240 日寫進同一本帳，
給回測週報用（週報數字和家人成績卡同一本帳）。爸媽會把成績卡上的數字當成事實看，所以這裡的原則是
「每個輸入都確定了才算，算了就不改」（美股 PLAN v5 §6）：

- 一筆成績＝（市場、規則、推薦日 t、rev、代號、h）。E＝t 之後第一個交易日（家人最早能照推薦買進的那一天，用它的收盤當進場價），
  T_h＝E 之後第 h 個交易日。日曆確定、除權息資料涵蓋到、區間內每個交易日都有收盤，才算出來寫進成績帳；還不確定就「等」
  （不寫帳，下一輪再看）；確定是壞資料（t 的收盤和紀錄對不上、公司行動、分割、隔離一直沒解除、價格已經拿不到、報酬不合理，
  或日曆／除權息過了 10 個交易日還是不確定）就「凍結成跳過」：寫一行 skip，之後不再算，卡上寫出跳過幾次。帳的每一列也存
  算式的輸入（c0＝t 的收盤（只拿來核對）、ce、ch、除權息因子 cf，基準 be、bh、bf），日後公式改版可以核對、從帳推回來。
- 成績帳 state/stock_picks_perf_ledger.jsonl 只增不改：寫入拿 stock_picks._state_file_lock()，鎖內重讀帳、
  去掉已經有的鍵才 append（部署時新舊程序會短暫重疊）；讀不拿鎖，跳過寫到一半或看不懂的行，同一個鍵只算第一次寫的。
  彙總與卡片只看帳、而且只看目前版本（LEDGER_VERSION＝2）的列：v1（推薦當天收盤進場）的列留著備查。價格庫刪掉、紀錄檔被截斷，
  已經算出來的成績都不變；（市場、t）在帳裡一有 v2 的列，就釘住那一組（rule、rev），之後紀錄檔才多出來的 rev 不會把已經上卡的
  成績換掉（P3 #2）。
- 紀錄要在「E 收盤」之前算出來才算數（台股台北 13:30、美股美東 13:00：半日市 13:00 就收，一律保守用 13:00；
  close_deadline）：晚算出來的那一行，家人看到時已經過了進場的那一刻，算成績等於偷看答案。E 還不在日曆上就先等。
- 台股價格要趁還在的時候收：DB 的 tw_all 每檔只留最近 10 個交易日（market_pipeline_tw.KEEP_ALL_SESSIONS），還沒凍結、
  DB tw（0050 成分，約 300 個交易日的長歷史）又沒有的收盤，存進 v2 自己的價格庫 state/stock_picks_perf_prices.json
  （v1 的程式不碰它，r3 #6），那一檔 4 個天數都凍結才刪。部署時用 backfill_prices() 從回測 job 的 review.sqlite（唯讀）
  補一次。收集自己查、自己 try（P3 #5）：紀錄檔讀不了、算成績的某個查詢壞了，照樣延長價格庫（沿用上一輪記下的 needed）。
  除權息不存，算的時候直接查 DB exrights（不刪，還拿得到盤勢 job 事後修正的值）。美股 DB 每檔都有一年多，不存。
- 共用的狀態檔 state/stock_picks_perf_state.json 格式不變（STATE_SCHEMA 1）：v2 照樣讀寫 ai_flag_by_t、ai_first_t；
  裡面 v1 的價格庫（prices、needed）v2 只拿來當補充來源、原樣保留，回滾到 v1 時照樣讀得懂。
- 台股紀錄有兩份（舊規則 stock_picks_history.jsonl、AI 相關股 stock_picks_tw_ai_history.jsonl）：凍結時才決定
  每個 t 用哪一份（decide_tw）；家人那天沒看到的「影子推薦」不算。規則看紀錄自己的 rule 欄位：美股 us_v1、us_v2 都算
  （冷凍名單上線時美股升 us_v2），台股 AI 看 ai_ 開頭。
- 美股推薦的 worker（stock_picks_us）每 30 分鐘呼叫一次 update()，美股開關關著也照呼叫（價格收集不能停）；
  這裡不 import stock_picks_us。DB 只透過 stock_db_ro 唯讀；回覆路徑只讀結果檔（load_result），不碰 DB。

算法：
- 報酬 r＝T_h 的收盤 × 除權息因子 ÷ E 的收盤 − 1。台股含股利：(E, T_h] 裡每對相鄰交易日 (前一天, 這一天] 之間的除權息
  （含落在休市日的）用「除權息前收盤 ÷ 參考價」還原在這一天；E 當天（和 t、E 之間）的除權息不算：E 的收盤已經是除權息後的。
  美股只算股價。基準：台股 0050（同樣還原）、美股 SPY。
- r 的合理範圍依 h：5、20 日是 (−90%, +300%)；60、240 日是 (−99%, +4900%)（DB 實測一年真的有 +1064% 的，r1 #4）。
- 日曆：t 本身要在日曆上（r3 #4：t 比日曆的起點早、或 t 那天有洞時，E、T_h 會默默錯位）；台股 TAIEX 的日期，[t, T_h]
  裡每個平日都要是 TAIEX 有收盤的日子或確定休市（證交所休市表、颱風臨時休市 confirmed 或已過 10 天），T_h 之後還要有
  2 個交易日，除權息資料要涵蓋到 T_h 的隔天以後（盤勢 job 下次會重讀最後一天；P3 #4）；美股 SPY／VOO／QQQ 的日期聯集，
  T_h 之後還要有 4 個交易日，而且這一檔和 SPY 自己都有第 4 天的收盤（盤勢 job 每次把每檔最近 4 個交易日整段重抓、差 ≤ 0.5%
  直接覆蓋：PLAN 的「至少 2 個」之外再等這段改寫結束，T_h 的收盤才是定案）。
- 已知限制（規則檔註明）：8% 這種小比例減資擋不住（11% 防護只擋大比例）；同一檔連續被推薦會算好幾次；台股紀錄
  多半在隔天早上 05:20–09:00 才算出來，E 收盤（13:30）以後才算出來的那一天不算成績。
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
_ET = ZoneInfo("America/New_York")

# ── 檔名與格式 ─────────────────────────────────────────────────────────────
US_HISTORY_NAME = "stock_picks_us_history.jsonl"  # 美股推薦紀錄（stock_picks_us 寫；檔名以這裡為準）
RESULT_NAME = "stock_picks_perf.json"  # 彙總結果（小檔；卡片只讀這份）
LEDGER_NAME = "stock_picks_perf_ledger.jsonl"  # 只增不改的成績帳
STATE_NAME = "stock_picks_perf_state.json"  # 共用狀態檔：ai_flag_by_t＋ai_first_t（＋v1 的價格庫與 needed：v2 只讀、原樣保留）
PRICES_NAME = "stock_picks_perf_prices.json"  # v2 的台股價格庫＋needed（獨立的檔：v1 的程式不碰它；r3 #6）
REVIEW_DB = Path("stock_picks_review") / "review.sqlite"  # 回測 job 的庫（STATE_DIR 底下）；只有 backfill_prices 唯讀它
LEDGER_VERSION = 2  # 算法版本（帳的 v）：公式改了就升版，彙總只看目前版本，舊列留著備查（v1＝推薦當天收盤進場、5／20 日）
STATE_SCHEMA = 1  # 不升：v2 不改狀態檔的格式，回滾到 v1 時照樣讀得懂
PRICES_SCHEMA = 1
RESULT_SCHEMA = 2  # v1 讀到 schema 2 會當成不合格、自己重算（反過來 v2 讀到 v1 的結果檔也一樣）
LEDGER_MAX_BYTES = 10 * 1024 * 1024  # 一天幾十行、一行約 300 字，夠用很多年；滿了記 error、summary 報
HISTORY_MAX_BYTES = 6 * 1024 * 1024  # 紀錄檔自己的上限是 5 MB（stock_picks.HISTORY_MAX_BYTES）：留一點餘裕
TMP_MAX_AGE_S = 3600.0  # 當機留下的暫存檔超過 1 小時清掉

# ── 算法（美股 PLAN v5 §6；回測 PLAN 2026-10-29 §5.1 的 v2） ─────────────────────
HORIZONS = (5, 20, 60, 240)  # 寫進帳的天數：E 之後第幾個交易日（週、月、季、年）
CARD_HORIZONS = (5, 20)  # 家人的成績卡只顯示這兩個（60、240 日給回測週報）
MIN_N = 5  # 次數少於這個，卡上寫「紀錄還不夠」
TW, US = "tw", "us"
TW_OLD_RULE = "v1"  # 舊規則紀錄沒有 rule 欄位
US_RULE = "us_v1"  # 美股紀錄沒有 rule 欄位時的預設（有就照紀錄的：us_v1、us_v2 都算）
TW_BENCHMARK, US_BENCHMARK = "0050", "SPY"
MARKET_CLOSE = {TW: (_TPE, 13, 30), US: (_ET, 13, 0)}  # 紀錄要在 E（t 之後第一個交易日）的這個時間之前算出來才算數
C0_TOLERANCE = 0.005  # 推薦當天的 DB 收盤和紀錄上的收盤差 ≤ 0.5%（不然就是對不上：mismatch）
TW_JUMP_LIMIT = 0.11  # 台股漲跌幅 10%：還原後相鄰兩個交易日漲跌超過 11%＝大比例減資、分割這類公司行動（jump）
EXRIGHT_RATIO = (1.0, 3.0)  # 除權息前收盤 ÷ 參考價只收 1–3（< 1 的現金增資除權、> 3 都當公司行動：jump）
R_RANGE_SHORT = (-0.9, 3.0)  # h ≤ 20：r ≤ −90% 或 ≥ +300% 當資料錯（range；家人卡片上的天數照舊）
R_RANGE_LONG = (-0.99, 49.0)  # h ≥ 60：一季、一年真的會漲好幾倍（DB 實測 240 日有 +1064%、60 日有 +368%；r1 #4）
TW_BUFFER_SESSIONS = 2  # T_h 之後日曆上還要有幾個交易日（後面先寫、前面後補的洞多半隔天就補上）
US_BUFFER_SESSIONS = 4  # 美股：盤勢 job 每天整段重抓最近 4 個交易日（market_pipeline_us 的 3＋1），過了才定案
SETTLE_SESSIONS = 10  # T_h 之後過了 10 個交易日：價格、隔離、日曆、除權息都不再等，凍結成跳過
SKIP_REASONS = ("mismatch", "jump", "split", "quarantine", "missing", "range", "calendar", "exrights")
# 帳裡存的算式輸入：t 的收盤（核對用）；這一檔、基準各（E 收盤、T_h 收盤、除權息因子）
INPUT_KEYS = ("c0", "ce", "ch", "cf", "be", "bh", "bf")
_LEDGER_INPUT_KEYS = ("c0", "ce", "ch", "cf", "b0", "be", "bh", "bf")  # 讀帳時認得的輸入欄位（含 v1 列的 b0）
COVERAGE_SOURCE = "twt49u"  # 除權息（證交所 TWT49U）已經查到哪一天：盤勢 job 的 coverage 表
WAIT, FREEZE = "wait", "freeze"
SOURCE_WAIT, SOURCE_SKIP = "source_wait", "source_skip"  # decide_*：還判不了／這個 t 不算

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


def r_range(h: int) -> tuple[float, float]:
    """報酬（這一檔和基準都是）的合理範圍，開區間：5、20 日 (−90%, +300%)；60、240 日 (−99%, +4900%)。"""
    return R_RANGE_SHORT if h <= 20 else R_RANGE_LONG


# ── 紀錄檔（三份；讀不拿鎖，壞行跳過） ─────────────────────────────────────────
@dataclass(frozen=True)
class Pick:
    code: str
    close: Optional[float]  # 紀錄上的收盤；壞掉是 None（凍結時當 mismatch：沒辦法確認家人看到的價格）


@dataclass(frozen=True)
class Record:
    """紀錄檔的一行。"""

    t: str
    rev: int
    rule: str
    picks: tuple
    computed_at: float  # 算出來的時間（epoch 秒）：家人最早什麼時候看得到這組推薦


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
    computed_at = _finite(obj.get("computed_at"), 0.0, 1e11)
    if computed_at is None:  # 不知道什麼時候算出來的：判斷不了家人看到時過了進場的那一刻沒有，不算
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
    return Record(t, rev, rule, tuple(picks), computed_at)


def parse_rows(
    objs: Iterable, *, market: str, fixed_rule: Optional[str] = None, default_rule: str, today: str
) -> dict[str, list[Record]]:
    """紀錄檔的每一行（已解析；壞行是 None）→ {t: [這個 t 的每一行，照檔案順序]}。fixed_rule 給了就固定用它
    （舊檔沒有 rule 欄位），不然用行裡的 rule（沒有就 default_rule）。挑哪一行由 decide_* 決定（先看算出來的時間）。"""
    out: dict[str, list[Record]] = {}
    for obj in objs:
        rec = _record_from(obj, market=market, fixed_rule=fixed_rule, default_rule=default_rule, today=today)
        if rec is not None:
            out.setdefault(rec.t, []).append(rec)
    return out


def parse_records(
    objs: Iterable, *, market: str, fixed_rule: Optional[str] = None, default_rule: str, today: str
) -> dict[str, Record]:
    """{t: rev 最大的那一行}：只解析、不看算出來的時間（讀的這一邊的契約；test_stock_picks_us 用它核對寫入端的格式）。
    成績本身用 parse_rows＋decide_*（先排除 E 收盤以後才算出來的行）。"""
    rows = parse_rows(objs, market=market, fixed_rule=fixed_rule, default_rule=default_rule, today=today)
    return {t: latest(recs) for t, recs in rows.items()}


def close_deadline(market: str, t: str, calendar: Sequence[str]) -> Optional[float]:
    """E（t 之後第一個交易日）收盤的時間（epoch 秒；台股台北 13:30、美股美東 13:00，夏令時間照當地）：紀錄要在這之前
    算出來才算數，家人才來得及照推薦用 E 的收盤買進。美股半日市 13:00 就收，一律用 13:00（寧可少算幾天，不偷看）。
    日曆上還沒有 E → None（還判不了，先等）。選來源、ai_first_t 都只用這一個期限；回測 job 選版也用它。"""
    i = bisect.bisect_right(calendar, t)
    if i >= len(calendar):
        return None
    day = date.fromisoformat(calendar[i])
    tz, hour, minute = MARKET_CLOSE[market]
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).timestamp()


def _eligible(rows: Sequence[Record], deadline: Optional[float]) -> list[Record]:
    """E 收盤之前就算出來的那幾行（P3 #1：晚算出來的那一行，家人看到時已經過了進場的那一刻，算成績就是偷看答案）。"""
    return [] if deadline is None else [r for r in rows if r.computed_at < deadline]


def latest(rows: Sequence[Record]) -> Optional[Record]:
    """rev 最大的那一行（一樣大取後面那行：家人最後看到的版本）。"""
    best: Optional[Record] = None
    for rec in rows:
        if best is None or rec.rev >= best.rev:
            best = rec
    return best


def decide_tw(t: str, old_rows: Sequence[Record], ai_rows: Sequence[Record], deadline: Optional[float],
              flags: dict[str, bool], ai_first_t: Optional[str]):
    """凍結前才決定台股的 t 用哪一份紀錄（PLAN v5 §6；r4 #1）。先只留 E 收盤前算出來的行（P3 #1），再：
    ① AI 檔有 t → 用它（rev 最大的）；
    ② 記到 t 那天 AI 開著 → 不算：家人看到的是上一份 AI 推薦或「計算中」，舊檔那行是沒人看過的影子推薦；
    ③ 記到關著 → 舊檔；
    ④ 沒記到（本功能上線前、worker 沒在跑的日子）→ t 早於看過的第一個 AI 紀錄（或從來沒有 AI 檔）才用舊檔，否則不猜。
    回 Record、SOURCE_SKIP（這一輪不算：不凍結，下一輪重判）或 SOURCE_WAIT（E 還不在日曆上）。"""
    if deadline is None:
        return SOURCE_WAIT
    ai = latest(_eligible(ai_rows, deadline))
    if ai is not None:
        return ai
    flag = flags.get(t)
    if flag is True:
        return SOURCE_SKIP
    if flag is False or ai_first_t is None or t < ai_first_t:
        old = latest(_eligible(old_rows, deadline))
        return old if old is not None else SOURCE_SKIP
    return SOURCE_SKIP


def decide_us(rows: Sequence[Record], deadline: Optional[float]):
    """美股只有一份紀錄：E 收盤前算出來的、rev 最大的那一行。"""
    if deadline is None:
        return SOURCE_WAIT
    rec = latest(_eligible(rows, deadline))
    return rec if rec is not None else SOURCE_SKIP


def pinned_line(rows: Sequence[Record], pin: tuple) -> Optional[Record]:
    """帳裡已經釘住（rule、rev）的 t：找回那一行（同一組有好幾行取後面那行）；紀錄被截斷了回 None。"""
    match = [r for r in rows if (r.rule, r.rev) == pin]
    return match[-1] if match else None


def pins_of(rows: Iterable[dict]) -> dict[tuple, tuple]:
    """帳裡每個（市場、t）第一次寫的那一組（rule、rev）：一有列就釘住（P3 #2：凍結、上卡之後才出現的 rev 不換掉它）。"""
    pins: dict[tuple, tuple] = {}
    for row in rows:
        if row["v"] == LEDGER_VERSION:
            pins.setdefault((row["m"], row["t"]), (row["rule"], row["rev"]))
    return pins


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


def candidates_of(lines: dict[tuple, Record], frozen: set) -> list[Candidate]:
    """這一輪用的紀錄（選定的或已經釘住的）→ 還沒凍結的成績（每檔推薦 × 每個 h）。"""
    out: list[Candidate] = []
    for (market, t), rec in sorted(lines.items()):
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
    """除權息資料涵蓋 (t, T_h]（比要用的 (E, T_h] 多看 t 到 E 那段，保守），而且一直到 T_h 的隔天以後（r3 #1：收盤和
    除權息不是同時進 DB；P3 #4：盤勢 job 下一次會從「上次涵蓋的最後一天」重讀，最後一天還可能補進事件）。
    重疊或相鄰的區間盤勢 job 會合併成一段。"""
    start = (date.fromisoformat(t) + timedelta(days=1)).isoformat()
    return any(a <= start and th < z for a, z in spans)


def _c0_ok(c: Candidate, c0: float, data: MarketData) -> bool:
    """推薦當天（t）的 DB 收盤要和家人看到的一樣（差 ≤ 0.5%）。美股在 t 之後任何時候有分割就不比：
    盤勢 job 會整段重抓、改成調整後的價格（PLAN v5 #12）；落在 (E, T_h] 的分割前面已經凍結成跳過，
    落在 E 當天的不影響報酬（E、T_h 都是分割後的價格）。"""
    if c.market == US and any(d > c.t for d in data.splits.get(c.code, ())):
        return True
    return c.close is not None and abs(c0 / c.close - 1.0) <= C0_TOLERANCE + 1e-9


def _adjust(market: str, series: dict, days: Sequence[str], events: Sequence) -> tuple[Optional[float], bool]:
    """(除權息因子 Π, 有沒有公司行動)。days＝E 到 T_h 的交易日，所以只看 (E, T_h]：E 當天和更早的除權息不算（E 的收盤
    已經是除權息後的）。台股：每對相鄰交易日 (前一天, 這一天] 的除權息（含落在休市日的，r3 #14）還原在這一天；還原後漲跌
    超過 11%、或比值不在 1–3（因子回 None）→ 公司行動（jump）。美股只算股價、不套跳動閘門（真的大跌要算進去，r3 #3）：因子 1。"""
    if market == US:
        return 1.0, False
    factor, jumped = 1.0, False
    for prev, cur in zip(days, days[1:]):
        step = 1.0
        for day, pre, ref in events:
            if prev < day <= cur:
                ratio = pre / ref if ref > 0 else math.inf
                if not EXRIGHT_RATIO[0] <= ratio <= EXRIGHT_RATIO[1]:
                    return None, True
                step *= ratio
        if abs(series[cur] * step / series[prev] - 1.0) > TW_JUMP_LIMIT:
            jumped = True
        factor *= step
    return factor, jumped


def evaluate(c: Candidate, data: MarketData) -> tuple[str, object]:
    """一筆成績這一輪的結果：(WAIT, 原因) 或 (FREEZE, 帳的一行)。E＝t 之後第一個交易日，T_h＝E 之後第 h 個交易日；
    r＝T_h 收盤 × (E, T_h] 的除權息因子 ÷ E 收盤 − 1（基準同算），t 的收盤只拿來核對家人看到的價格。
    順序：到期 → t 在日曆上 → 日曆 → 除權息涵蓋 → 分割 → 隔離 → 缺價 → 收盤對不對得上 → 公司行動 → 報酬範圍。
    T_h 之後過了 10 個交易日，「等」的都凍結成跳過。回測週報的 job 也用這個函式算股票池成員的報酬（close＝池子紀錄的 c0）。"""
    cal = data.calendar
    first = bisect.bisect_right(cal, c.t)  # E 的位置
    last = first + c.h  # T_h 的位置
    buffer = TW_BUFFER_SESSIONS if c.market == TW else US_BUFFER_SESSIONS
    if last + buffer >= len(cal):
        return WAIT, "not_due"
    th = cal[last]
    settled = len(cal) - 1 - last >= SETTLE_SESSIONS
    codes = (c.code, TW_BENCHMARK if c.market == TW else US_BENCHMARK)

    def frozen(skip: Optional[str] = None, inputs: Optional[dict] = None, r: Optional[float] = None,
               b: Optional[float] = None) -> tuple:
        row = {"v": LEDGER_VERSION, "m": c.market, "rule": c.rule, "t": c.t, "rev": c.rev, "code": c.code,
               "h": c.h, "th": th, "r": r, "b": b, "skip": skip}
        row.update({k: (inputs or {}).get(k) for k in INPUT_KEYS})
        return FREEZE, row

    # t 本身要在日曆上（r3 #4）：t 比日曆的起點早（bisect 回 0）或 t 那天有洞時，E、T_h 會默默錯位。
    if not (first > 0 and cal[first - 1] == c.t):
        return frozen("calendar") if settled else (WAIT, "calendar")
    if c.market == TW:
        if not _tw_calendar_certain(c.t, th, data):  # P3 #10：等也要有出口
            return frozen("calendar") if settled else (WAIT, "calendar")
        if not _exrights_covered(data.coverage, c.t, th):
            return frozen("exrights") if settled else (WAIT, "exrights")
    elif not settled and any(cal[last + buffer] not in data.prices.get(code, {}) for code in codes):
        # 盤勢 job 照字母分批寫：日曆（SPY）已經有那一天，這一檔的批次可能還沒跑；它跑的那一次會把 T_h 再整段
        # 重抓一次（[前一次有效日 − 3 個交易日, D]），之後才不會再改。過了 10 個交易日就不再看這一條。
        return WAIT, "not_due"
    window = cal[first:last + 1]  # E … T_h
    entry = window[0]
    # 美股分割：(E, T_h] 裡有就跳過（E 和 T_h 一個調整前、一個調整後）；E 當天或更早的分割不影響 E 到 T_h 的報酬
    if c.market == US and any(entry < d <= th for code in codes for d in data.splits.get(code, ())):
        return frozen("split")
    # 隔離看 [t, T_h]：t 的收盤還要拿來核對（P3 #3：隔離區間結尾剛好是 t 也算，盤勢 job 對 t 那根還有爭議時不改 DB）
    if any(z >= c.t and a <= th for code in codes for a, z in data.quarantine.get(code, ())):
        return frozen("quarantine") if settled else (WAIT, "quarantine")
    stock, bench = (data.prices.get(code, {}) for code in codes)
    if c.t not in stock or any(day not in s for s in (stock, bench) for day in window):
        return frozen("missing") if settled else (WAIT, "price")
    c0 = stock[c.t]
    (ce, ch), (be, bh) = ((s[entry], s[th]) for s in (stock, bench))
    (cf, c_jump), (bf, b_jump) = (_adjust(c.market, s, window, data.exrights.get(code, ()))
                                  for code, s in zip(codes, (stock, bench)))
    inputs = {"c0": c0, "ce": ce, "ch": ch, "cf": cf, "be": be, "bh": bh, "bf": bf}
    if not _c0_ok(c, c0, data):
        return frozen("mismatch", inputs)
    if c_jump or b_jump:
        return frozen("jump", inputs)
    r, b = ch * cf / ce - 1.0, bh * bf / be - 1.0
    lo, hi = r_range(c.h)
    if not (lo < r < hi and lo < b < hi):
        return frozen("range", inputs)
    return frozen(None, inputs, round(r, 6), round(b, 6))


# ── 台股價格庫（純函式） ─────────────────────────────────────────────────────
def needed_prices(pending: Iterable[Candidate], undecided: dict[str, list[Record]]) -> dict[str, str]:
    """台股要留哪些價格：{代號: 最早的 t}。還沒凍結的成績，加上還判不了來源的 t 的每一行（先收，判了再丟）；
    基準 0050 從最早那一筆開始。"""
    out: dict[str, str] = {}
    for c in pending:
        if c.market == TW:
            out[c.code] = min(out.get(c.code, c.t), c.t)
    for t, rows in undecided.items():
        for rec in rows:
            for pick in rec.picks:
                out[pick.code] = min(out.get(pick.code, t), t)
    return _with_benchmark(out)


def _with_benchmark(needs: dict[str, str]) -> dict[str, str]:
    out = dict(needs)
    if out:
        earliest = min(out.values())
        out[TW_BENCHMARK] = min(out.get(TW_BENCHMARK, earliest), earliest)
    return out


def next_library(old: dict, long: dict, recent: dict, needed: dict[str, str]) -> dict:
    """新的價格庫：只留還要用的代號、從它最早的 t 起、DB tw 沒有的日期；tw_all 有的以 DB 為準（r4 #3）。"""
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
    lines: dict[tuple, Record],
    pending: Sequence[Candidate],
    first_t: dict[str, Optional[str]],
    *,
    now: float,
    ledger_rows: int,
    ledger_full: bool,
) -> dict:
    """帳（已依鍵去重、先寫的算）→ 結果。只看目前版本（LEDGER_VERSION）的列；每個（市場、t）只算帳裡第一次寫的那一組
    （rule、rev）（P3 #2）；紀錄檔裡還找得到那一行時，只算代號還在那一行裡的列（r3 #5：紀錄被改寫後舊代號的列不算）。
    這樣不會同一天算兩套，價格庫或紀錄檔不見了、紀錄多出新的 rev，已經算出來的都不會消失或被換掉。
    4 個天數都有統計（回測週報、每日維護看）；卡上的「起、算到、起含 AI」只看卡片顯示的天數（CARD_HORIZONS）。"""
    pins = pins_of(rows)
    chosen: list[dict] = []
    for row in rows:
        if row["v"] != LEDGER_VERSION or row["h"] not in HORIZONS:
            continue
        pin = pins[(row["m"], row["t"])]
        if (row["rule"], row["rev"]) != pin:
            continue
        rec = lines.get((row["m"], row["t"]))
        if rec is not None and (rec.rule, rec.rev) == pin and all(p.code != row["code"] for p in rec.picks):
            continue
        chosen.append(row)
    markets: dict[str, dict] = {}
    skipped_total = 0
    for market in (TW, US):
        mine = [r for r in chosen if r["m"] == market]
        counted = [r for r in mine if r["skip"] is None]
        shown = [r for r in counted if r["h"] in CARD_HORIZONS]
        ai_days = [r["t"] for r in shown if r["rule"].startswith("ai_")]
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
            "from": min((r["t"] for r in shown), default=None),  # 卡上的「起」：實際算進去的最早一筆（r3 #6）
            "to": max((r["th"] for r in shown), default=None),
            "ai_from": min(ai_days, default=None),  # 算進去的有 AI 相關股推薦：最早那一天（P3 #7）
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


def _read_rows(path: Path, market: str, *, fixed_rule: Optional[str], default_rule: str,
               today: str) -> Optional[dict[str, list[Record]]]:
    """讀不了（權限、I/O、太大）回 None：那個市場這一輪不選來源、不凍結（r4 #1），不是當成沒有紀錄。"""
    try:
        objs, _, _ = _read_jsonl(path, HISTORY_MAX_BYTES)
    except Exception as exc:
        logger.warning("stock picks perf history unreadable name=%s error_type=%s", path.name, type(exc).__name__)
        return None
    return parse_rows(objs, market=market, fixed_rule=fixed_rule, default_rule=default_rule, today=today)


@dataclass
class _Ledger:
    rows: list  # 讀得懂的列（同一個鍵只留第一次寫的）
    keys: set
    size: int
    ends_with_newline: bool


def _ledger_row(obj) -> Optional[dict]:
    """帳的一行；看不懂回 None。算式輸入是後來才加的：沒有這幾個欄位的舊列照樣讀。v1 列的輸入是 c0、ch、cf、b0、bh、bf，
    v2 列是 INPUT_KEYS（c0、ce、ch、cf、be、bh、bf）；讀出來的列每個輸入欄位都有（沒有的是 None）。"""
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
    inputs = {}
    for key in _LEDGER_INPUT_KEYS:
        value = obj.get(key)
        inputs[key] = None if value is None else _finite(value, 0.0, _MAX_PRICE)
        if value is not None and inputs[key] is None:
            return None
    return {"v": obj["v"], "m": m, "rule": rule, "t": obj["t"], "rev": obj["rev"], "code": code, "h": obj["h"],
            "th": obj["th"], "r": r, "b": b, "skip": skip, **inputs}


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
    """共用狀態檔（v1 的格式，STATE_SCHEMA 1）。v2 讀寫 flags、ai_first_t；prices、needed 是 v1 的價格庫，v2 只讀、原樣寫回。"""

    prices: dict  # v1 的台股價格庫 {代號: {日期: 收盤}}（v2 只拿來當補充來源）
    flags: dict  # ai_flag_by_t {T: 那時 AI 開著沒有}：只在 T 是舊檔最新一行時記，記過不改
    ai_first_t: Optional[str]  # 看過的（E 收盤前算出來的）AI 紀錄最小的 t（AI 檔被刪或截斷時，AI 的日子不會被改判成舊規則）
    needed: dict = field(default_factory=dict)  # v1 上一輪算出來要收的 {代號: 從哪一天}（v2 原樣保留）


_EMPTY_STATE = _State({}, {}, None)


@dataclass(frozen=True)
class _Library:
    """v2 的台股價格庫（stock_picks_perf_prices.json；v1 的程式不碰這個檔，r3 #6）。"""

    prices: dict  # {代號: {日期: 收盤}}：還沒凍結（4 個天數）、DB tw 又沒有的收盤
    needed: dict = field(default_factory=dict)  # 上一輪算出來要收的 {代號: 從哪一天}：這一輪判不了時照這份收（P3 #5）


_EMPTY_LIBRARY = _Library({}, {})


def _prices_from(raw) -> dict[str, dict]:
    _require(isinstance(raw, dict))
    prices: dict[str, dict] = {}
    for code, series in raw.items():
        _require(isinstance(code, str) and _TW_CODE_RE.fullmatch(code) is not None and isinstance(series, dict))
        clean = {}
        for day, close in series.items():
            value = _finite(close, 0.0, _MAX_PRICE)
            _require(_iso_day(day) is not None and value is not None)
            clean[day] = value
        prices[code] = clean
    return prices


def _needed_from(raw) -> dict[str, str]:
    _require(isinstance(raw, dict))
    needed = {}
    for code, since in raw.items():
        _require(isinstance(code, str) and _TW_CODE_RE.fullmatch(code) is not None and _iso_day(since) is not None)
        needed[code] = since
    return needed


def _prices_json(prices: dict) -> dict:
    return {code: dict(sorted(series.items())) for code, series in sorted(prices.items())}


def _state_from_json(obj) -> _State:
    _require(isinstance(obj, dict) and obj.get("schema") == STATE_SCHEMA)
    raw_flags, first = obj.get("ai_flag_by_t"), obj.get("ai_first_t")
    prices = _prices_from(obj.get("prices"))
    needed = _needed_from(obj.get("needed", {}))  # 後來才加的欄位：舊狀態檔沒有
    _require(isinstance(raw_flags, dict))
    flags = {}
    for day, flag in raw_flags.items():
        _require(_iso_day(day) is not None and isinstance(flag, bool))
        flags[day] = flag
    _require(first is None or _iso_day(first) is not None)
    return _State(prices, flags, first, needed)


def _state_to_json(state: _State) -> dict:
    return {
        "schema": STATE_SCHEMA,
        "prices": _prices_json(state.prices),
        "needed": dict(sorted(state.needed.items())),
        "ai_flag_by_t": dict(sorted(state.flags.items())),
        "ai_first_t": state.ai_first_t,
    }


def _load_state(path: Path) -> _State:
    """壞檔當作空的重來（記 error）：ai_first_t 另外從帳裡的 AI 列補下限。"""
    try:
        obj = stock_picks._read_json_file(path)
        return _EMPTY_STATE if obj is None else _state_from_json(obj)
    except Exception as exc:
        logger.error("stock picks perf state unreadable; starting empty error_type=%s", type(exc).__name__)
        return _EMPTY_STATE


def _library_from_json(obj) -> _Library:
    _require(isinstance(obj, dict) and obj.get("schema") == PRICES_SCHEMA)
    return _Library(_prices_from(obj.get("prices")), _needed_from(obj.get("needed", {})))


def _library_to_json(library: _Library) -> dict:
    return {"schema": PRICES_SCHEMA, "prices": _prices_json(library.prices),
            "needed": dict(sorted(library.needed.items()))}


def _load_library(path: Path) -> _Library:
    """壞檔當作空的重來（記 error）：最近 10 天可以從 tw_all 補回，更早的用 backfill_prices 從 review.sqlite 補。"""
    try:
        obj = stock_picks._read_json_file(path)
        return _EMPTY_LIBRARY if obj is None else _library_from_json(obj)
    except Exception as exc:
        logger.error("stock picks perf price library unreadable; starting empty error_type=%s", type(exc).__name__)
        return _EMPTY_LIBRARY


def _write_library(path: Path, library: _Library, disk: _Library) -> bool:
    """內容變了才寫（原子寫入、0600）。超過 2 MiB（stock_picks.STATE_MAX_BYTES）不寫、記 error：價格庫不再長，但帳、
    結果檔照常更新（不能讓 update() 每輪都失敗）。回 True＝寫了。"""
    payload = _library_to_json(library)
    if payload == _library_to_json(disk):
        return False
    try:
        stock_picks._write_json_file(path, payload)
    except ValueError:
        logger.error("stock picks perf price library too large; not written codes=%d", len(library.prices))
        return False
    return True


def _fallback_needs(prices: dict, needed: dict) -> dict[str, str]:
    """這一輪判不了要收哪些（台股紀錄讀不了、日曆查不到）：沿用上一輪記下的；沒有就用價格庫裡已經有的代號。"""
    if needed:
        return dict(needed)
    return _with_benchmark({code: min(series) for code, series in prices.items() if series})


def _sweep_stale_tmp(state_dir: Path) -> None:
    """_write_json_file 當機留下的暫存檔（.stock_picks_perf*.tmp）超過 1 小時就清掉（看檔案的真實時間）。"""
    with contextlib.suppress(OSError):
        for tmp in state_dir.glob(".stock_picks_perf*.tmp"):
            with contextlib.suppress(OSError):
                if time.time() - tmp.lstat().st_mtime > TMP_MAX_AGE_S:
                    tmp.unlink()


# ── DB（唯讀；每個查詢都是 stock_db_ro 的一個短 SELECT；不同用途分開 try） ───────────
def _clean_series(table: dict) -> dict:
    """DB 的收盤只收有限正數（欄位型別變了就當沒有這一根）。"""
    return {code: {d: float(v) for d, v in series.items() if _iso_day(d) and _finite(v, 0.0, _MAX_PRICE)}
            for code, series in table.items()}


def _db_failed(what: str, exc: Exception, waits: Counter) -> None:
    """DB 忙碌（盤勢 job 正在 commit）只記等待；讀不到另外記 warning（只帶種類）。"""
    if isinstance(exc, stock_db_ro.DbUnavailable):
        logger.warning("stock picks perf db unavailable what=%s kind=%s", what, exc.kind)
        waits[f"{what}:db_unavailable"] += 1
    else:
        waits[f"{what}:db_busy"] += 1


def _calendar(market: str, since: str, db_path: Optional[Path], waits: Counter) -> Optional[tuple]:
    """交易日（台股 TAIEX、美股 SPY／VOO／QQQ 聯集）；查不到回 None（這一輪那個市場不選來源、不凍結）。"""
    try:
        found = (stock_db_ro.tw_calendar if market == TW else stock_db_ro.us_calendar)(since, path=db_path)
    except (stock_db_ro.DbBusy, stock_db_ro.DbUnavailable) as exc:
        _db_failed(f"{market}_calendar", exc, waits)
        return None
    return tuple(d for d in found if _iso_day(d))


def _collect_tw(needs: dict[str, str], cands: Sequence[Candidate], db_path: Optional[Path],
                waits: Counter) -> tuple[Optional[dict], Optional[dict]]:
    """台股價格：(tw 收盤, tw_all 收盤)；查不到回 (None, None)。價格收集和算成績共用這兩個查詢，
    但不和日曆、休市表、涵蓋、除權息、隔離的查詢綁在一起（P3 #5）。"""
    codes = set(needs) | {c.code for c in cands}
    if not codes:
        return {}, {}
    since = min([*needs.values(), *(c.t for c in cands)])
    codes = sorted(codes | {TW_BENCHMARK})
    try:
        long = _clean_series(stock_db_ro.closes("tw", codes, since, path=db_path))
        recent = _clean_series(stock_db_ro.closes("tw_all", codes, since, path=db_path))
    except (stock_db_ro.DbBusy, stock_db_ro.DbUnavailable) as exc:
        _db_failed("tw_prices", exc, waits)
        return None, None
    return long, recent


def load_tw(cands: Sequence[Candidate], calendar: tuple, prices: dict, today: str,
            db_path: Optional[Path]) -> MarketData:
    """台股算成績還要的：休市表（一年一年查：明年的表還沒公布，不擋今年的區間）、除權息涵蓋、除權息、隔離。
    公開給回測週報的 job 用（PLAN 2026-10-29 §5.1）：日曆、價格由呼叫端給，其他都查 DB（唯讀）。"""
    since = min(c.t for c in cands)
    codes = sorted({c.code for c in cands} | {TW_BENCHMARK})
    last_year = int((calendar[-1] if calendar else today)[:4])
    closed_by_year = {}
    for year in range(int(since[:4]), last_year + 1):
        closed = stock_db_ro.tw_closed_days([year], today=today, path=db_path)
        closed_by_year[year] = None if closed is None else frozenset(closed)
    spans = tuple(
        (a, z) for a, z in stock_db_ro.rows(
            "SELECT from_date, to_date FROM coverage WHERE source = ?", (COVERAGE_SOURCE,), path=db_path)
        if _iso_day(a) and _iso_day(z)
    )
    events = stock_db_ro.exrights(codes, since, path=db_path)
    quarantine: dict[str, list] = {}
    for market in ("tw", "tw_all"):
        for code, spans_q in stock_db_ro.active_quarantine(market, codes, path=db_path).items():
            quarantine.setdefault(code, []).extend(spans_q)
    return MarketData(TW, calendar, prices, quarantine, closed_by_year, spans, events)


def load_us(cands: Sequence[Candidate], calendar: tuple, db_path: Optional[Path],
            prices: Optional[dict] = None) -> MarketData:
    """美股算成績要的：收盤、分割、隔離。prices 沒給（背景每輪）就查 DB 的 us 收盤；回測週報的 job 給自己價格庫的
    收盤（{代號: {日期: 收盤}}，含 SPY）。分割、隔離一律查 DB（唯讀）。"""
    since = min(c.t for c in cands)
    codes = sorted({c.code for c in cands} | {US_BENCHMARK})
    if prices is None:
        prices = stock_db_ro.closes("us", codes, since, path=db_path)
    prices = _clean_series(prices)
    splits = stock_db_ro.splits_after(codes, since, path=db_path)
    quarantine = stock_db_ro.active_quarantine("us", codes, path=db_path)
    return MarketData(US, calendar, prices, quarantine, splits=splits)


def _evaluate_all(cands: Sequence[Candidate], data: MarketData, new_rows: list, waits: Counter) -> None:
    for cand in cands:
        kind, value = evaluate(cand, data)
        if kind == FREEZE:
            new_rows.append(value)
        else:
            waits[f"{cand.market}:{value}"] += 1


# ── 對外：背景每輪更新、回覆讀結果 ───────────────────────────────────────────
def update(*, ai_enabled: bool, now: float, db_path: Optional[Path] = None) -> Optional[dict]:
    """美股 worker 每輪呼叫（美股開關關著也呼叫）：讀三份紀錄與日曆 → 記 ai_flag_by_t／ai_first_t → 選來源（E 收盤前
    算出來的、帳裡已釘住的）→ 收台股價格 → 凍結到期的成績 → append 帳 → 更新 v2 價格庫 → 彙總 → 寫結果檔；回結果。
    ai_enabled 是 stock_picks.AI_ENABLED（家人此刻看到哪一種台股推薦）。任何失敗只記 log、回 None，不丟例外。"""
    try:
        return _update(bool(ai_enabled), float(now), db_path)
    except TimeoutError:
        logger.warning("stock picks perf update skipped reason=lock_busy")
        return None
    except Exception as exc:
        logger.error("stock picks perf update failed error_type=%s", type(exc).__name__)
        return None


@dataclass
class _Selection:
    """這一輪選好的紀錄（_update 與 backfill_prices 共用；只讀檔、查日曆，什麼都不寫）。"""

    today: str
    ledger: _Ledger
    state: _State  # 共用狀態檔（開頭讀的）
    library: _Library  # v2 價格庫（開頭讀的）
    tw_readable: bool
    tw_days: list
    us_days: list
    tw_cal: Optional[tuple]
    us_cal: Optional[tuple]
    flags: dict
    ai_first: Optional[str]
    lines: dict  # {(市場, t): 這一輪用的那一行}
    undecided: dict  # {t: 這個 t 的每一行}：E 還不在日曆上，還判不了用哪一份（價格先收）
    cands: list  # 還沒凍結的成績
    waits: Counter

    @property
    def fresh_needs(self) -> bool:
        """這一輪判得出台股要收哪些價格（紀錄讀得了、日曆查得到）。"""
        return self.tw_readable and self.tw_cal is not None


def _select(now: float, db_path: Optional[Path], *, ai_enabled: Optional[bool]) -> _Selection:
    """讀三份紀錄、帳、狀態檔、價格庫與日曆 → 選來源（帳裡已經有 v2 列的 t 釘住那一組；其他的只收 E 收盤前算出來的行，
    再照 decide_*）→ 還沒凍結的成績。ai_enabled 是 None 時不記 ai_flag_by_t（backfill_prices：部署腳本的環境變數
    不一定和 bot 一樣）。"""
    state_dir = stock_picks.STATE_DIR
    today = datetime.fromtimestamp(now, _TPE).date().isoformat()
    tw_old = _read_rows(state_dir / stock_picks.HISTORY_NAME, TW, fixed_rule=TW_OLD_RULE,
                        default_rule=TW_OLD_RULE, today=today)
    tw_ai = _read_rows(state_dir / stock_picks.AI_HISTORY_NAME, TW, fixed_rule=None,
                       default_rule=stock_picks.AI_RULE, today=today)
    us_hist = _read_rows(state_dir / US_HISTORY_NAME, US, fixed_rule=None, default_rule=US_RULE, today=today)
    ledger = _read_ledger(state_dir / LEDGER_NAME)  # 讀不了就丟出去：沒有帳就不能去重，這一輪整個不做
    start = _load_state(state_dir / STATE_NAME)
    library = _load_library(state_dir / PRICES_NAME)
    pins = pins_of(ledger.rows)
    waits: Counter = Counter()
    tw_readable = tw_old is not None and tw_ai is not None
    tw_days = sorted(set(tw_old or {}) | set(tw_ai or {})) if tw_readable else []
    us_days = sorted(us_hist or {})
    tw_cal = _calendar(TW, tw_days[0], db_path, waits) if tw_days else None
    us_cal = _calendar(US, us_days[0], db_path, waits) if us_days else None

    flags, ai_first = dict(start.flags), start.ai_first_t
    lines: dict[tuple, Record] = {}
    undecided: dict[str, list[Record]] = {}
    if tw_readable:
        if tw_old and ai_enabled is not None:
            flags.setdefault(max(tw_old), ai_enabled)  # 只記家人此刻看到的那個 T；晚才看到的舊 T 不補記
        ledger_ai = [r["t"] for r in ledger.rows if r["m"] == TW and r["rule"].startswith("ai_")]
        seen_ai = [] if tw_cal is None else [
            t for t, rows in tw_ai.items() if _eligible(rows, close_deadline(TW, t, tw_cal))]
        ai_first = _min_day(ai_first, min(seen_ai, default=None), min(ledger_ai, default=None))
        for t in tw_days:
            old_rows, ai_rows = tw_old.get(t, []), tw_ai.get(t, [])
            pin = pins.get((TW, t))
            if pin is not None:
                rec = pinned_line(old_rows + ai_rows, pin)
                if rec is not None:
                    lines[(TW, t)] = rec
            elif tw_cal is not None:
                got = decide_tw(t, old_rows, ai_rows, close_deadline(TW, t, tw_cal), flags, ai_first)
                if got == SOURCE_WAIT:
                    undecided[t] = old_rows + ai_rows
                elif isinstance(got, Record):
                    lines[(TW, t)] = got
    for t in us_days:
        pin = pins.get((US, t))
        if pin is not None:
            rec = pinned_line(us_hist[t], pin)
            if rec is not None:
                lines[(US, t)] = rec
        elif us_cal is not None:
            got = decide_us(us_hist[t], close_deadline(US, t, us_cal))
            if isinstance(got, Record):
                lines[(US, t)] = got
    cands = candidates_of(lines, ledger.keys)
    return _Selection(today, ledger, start, library, tw_readable, tw_days, us_days, tw_cal, us_cal, flags, ai_first,
                      lines, undecided, cands, waits)


def _update(ai_enabled: bool, now: float, db_path: Optional[Path]) -> dict:
    state_dir = stock_picks.STATE_DIR
    _sweep_stale_tmp(state_dir)
    sel = _select(now, db_path, ai_enabled=ai_enabled)
    ledger_path, state_path, prices_path = state_dir / LEDGER_NAME, state_dir / STATE_NAME, state_dir / PRICES_NAME
    waits, cands = sel.waits, sel.cands
    tw_cands = [c for c in cands if c.market == TW]
    us_cands = [c for c in cands if c.market == US]

    # 2. 台股價格收集（自己的查詢、自己的 try；判不了來源時照上一輪記下的收）
    needs = (needed_prices(tw_cands, sel.undecided) if sel.fresh_needs
             else _fallback_needs(sel.library.prices, sel.library.needed))
    long, recent = _collect_tw(needs, tw_cands, db_path, waits)
    seed = _merge_prices(sel.state.prices, sel.library.prices)  # v1 的價格庫只當補充來源（v2 優先；v2 不改它）

    # 3. 算到期的（DB 查詢都在鎖外）
    new_rows: list[dict] = []
    if tw_cands and (sel.tw_cal is None or long is None):
        waits["tw:no_data"] += len(tw_cands)
    elif tw_cands:
        prices = {code: {**seed.get(code, {}), **recent.get(code, {}), **long.get(code, {})}
                  for code in {c.code for c in tw_cands} | {TW_BENCHMARK}}
        try:
            data = load_tw(tw_cands, sel.tw_cal, prices, sel.today, db_path)
        except (stock_db_ro.DbBusy, stock_db_ro.DbUnavailable) as exc:
            _db_failed("tw", exc, waits)
        else:
            _evaluate_all(tw_cands, data, new_rows, waits)
    if us_cands and sel.us_cal is None:
        waits["us:no_data"] += len(us_cands)
    elif us_cands:
        try:
            data = load_us(us_cands, sel.us_cal, db_path)
        except (stock_db_ro.DbBusy, stock_db_ro.DbUnavailable) as exc:
            _db_failed("us", exc, waits)
        else:
            _evaluate_all(us_cands, data, new_rows, waits)

    # 4. 寫檔（同一把跨程序鎖，鎖內只讀檔、寫檔）
    with stock_picks._state_file_lock():
        disk = _read_ledger(ledger_path)
        appended, full = _append_rows(ledger_path, disk, new_rows)
        rows = disk.rows + appended
        frozen = disk.keys | {_row_key(r) for r in appended}
        pending = [c for c in cands if c.key not in frozen]
        if sel.fresh_needs:  # 剛凍結的就不用再收（4 個天數都凍結了才放掉那一檔）
            needs = needed_prices(pending, sel.undecided)
        disk_state, disk_lib = _load_state(state_path), _load_library(prices_path)
        old = _merge_prices(_merge_prices(sel.state.prices, disk_state.prices),  # v1 的（別的程序剛寫的優先）
                            _merge_prices(sel.library.prices, disk_lib.prices))  # v2 的（優先於 v1）
        _write_library(prices_path, _Library(next_library(old, long or {}, recent or {}, needs), needs), disk_lib)
        # 共用狀態檔：v1 的 prices、needed 照磁碟上的原樣寫回（v2 不碰），只合併 ai_flag_by_t、ai_first_t
        merged = _State(disk_state.prices, {**sel.flags, **disk_state.flags},
                        _min_day(sel.ai_first, disk_state.ai_first_t), disk_state.needed)
        payload = _state_to_json(merged)
        if payload != _state_to_json(disk_state):  # 內容沒變就不寫
            stock_picks._write_json_file(state_path, payload)
        first_t = {TW: sel.tw_days[0] if sel.tw_days else None, US: sel.us_days[0] if sel.us_days else None}
        result = aggregate(rows, sel.lines, pending, first_t, now=now, ledger_rows=len(rows), ledger_full=full)
        stock_picks._write_json_file(state_dir / RESULT_NAME, result)
    logger.info("stock picks perf updated appended=%d pending=%d waits=%s",
                len(appended), len(pending), ",".join(f"{k}:{v}" for k, v in sorted(waits.items())) or "-")
    return result


def _review_closes(path: Path, needs: dict[str, str]) -> dict[str, dict]:
    """回測 job 的 review.sqlite（prices 表：m、code、d、close、src、seen_at）裡、needs 要的台股收盤：{代號: {日期: 收盤}}。
    唯讀（stock_db_ro.rows：mode=ro、query_only、一個 SELECT 就關）；檔案不在丟 DbUnavailable("missing")。"""
    codes = sorted(needs)
    since = min(needs.values())
    out: dict[str, dict] = {}
    for i in range(0, len(codes), 400):
        chunk = codes[i:i + 400]
        for code, day, close in stock_db_ro.rows(
            f"SELECT code, d, close FROM prices WHERE m = 'tw' AND d >= ? AND code IN ({','.join('?' * len(chunk))})",
            (since, *chunk), path=path,
        ):
            value = _finite(close, 0.0, _MAX_PRICE)
            if code in needs and _iso_day(day) and day >= needs[code] and value is not None:
                out.setdefault(code, {})[day] = value
    return out


def backfill_prices(*, now: Optional[float] = None, db_path: Optional[Path] = None,
                    review_db: Optional[Path] = None) -> dict:
    """部署 v2 時跑一次（回測 PLAN §10 B1）：v2 還沒凍結的台股成績要用、但 tw_all 已經滾掉的收盤，從回測 job 的 review.sqlite
    （A 段起每 30 分鐘收全部池子成員的價格；唯讀）補進 v2 價格庫。只補缺的日期（價格庫已經有的不改），之後 update() 照常修剪。
    回 {"status", "codes", "added"}；status：ok、no_review_db（檔案不在：正常，什麼都不做）、review_unavailable、
    review_busy、undecided（紀錄讀不了或日曆查不到：判不了要補哪些）、lock_busy、error。不丟例外、不記 ai_flag_by_t。"""
    try:
        return _backfill(time.time() if now is None else float(now), db_path, review_db)
    except TimeoutError:
        return {"status": "lock_busy", "codes": 0, "added": 0}
    except Exception as exc:
        logger.error("stock picks perf backfill failed error_type=%s", type(exc).__name__)
        return {"status": "error", "codes": 0, "added": 0}


def _backfill(now: float, db_path: Optional[Path], review_db: Optional[Path]) -> dict:
    sel = _select(now, db_path, ai_enabled=None)
    if not sel.fresh_needs:
        return {"status": "undecided", "codes": 0, "added": 0}
    tw_cands = [c for c in sel.cands if c.market == TW]
    needs = needed_prices(tw_cands, sel.undecided)
    if not needs:
        return {"status": "ok", "codes": 0, "added": 0}
    try:
        found = _review_closes(review_db or stock_picks.STATE_DIR / REVIEW_DB, needs)
    except stock_db_ro.DbUnavailable as exc:
        status = "no_review_db" if exc.kind == "missing" else "review_unavailable"
        return {"status": status, "codes": len(needs), "added": 0}
    except stock_db_ro.DbBusy:
        return {"status": "review_busy", "codes": len(needs), "added": 0}
    long, recent = _collect_tw(needs, tw_cands, db_path, sel.waits)  # DB tw 有的日期不必存；查不到就先全收
    prices_path = stock_picks.STATE_DIR / PRICES_NAME
    with stock_picks._state_file_lock():
        disk = _load_library(prices_path)
        mine = _merge_prices(sel.library.prices, disk.prices)
        library = next_library(_merge_prices(found, mine), long or {}, recent or {}, needs)  # 價格庫已經有的優先
        added = sum(1 for code, series in library.items() for day in series if day not in disk.prices.get(code, {}))
        _write_library(prices_path, _Library(library, needs), disk)
    logger.info("stock picks perf backfill codes=%d added=%d", len(needs), added)
    return {"status": "ok", "codes": len(needs), "added": added}


def load_result() -> Optional[dict]:
    """結果檔（背景啟動時讀一次；之後用 update 的回傳值）。沒有、壞掉都回 None（卡片寫「計算中」）。"""
    try:
        obj = stock_picks._read_json_file(stock_picks.STATE_DIR / RESULT_NAME)
        return None if obj is None else _check_result(obj)
    except Exception as exc:
        logger.warning("stock picks perf result unreadable error_type=%s", type(exc).__name__)
        return None


def read_ledger_rows() -> list[dict]:
    """成績帳讀得懂的每一列（同一個鍵只留第一次寫的、壞行跳過：和彙總用的是同一套）；唯讀、不拿鎖、各版本的列都在
    （用 row["v"] 分）。給回測週報的 job 用：週報數字和家人成績卡同一本帳（PLAN 2026-10-29 §6）。
    帳讀不了（不是一般檔、太大、權限）就丟例外，不當成空的帳。"""
    return [dict(row) for row in _read_ledger(stock_picks.STATE_DIR / LEDGER_NAME).rows]


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
TEXT_METHOD = (f"算法：推薦隔天收盤，到之後第 {'、'.join(str(h) for h in CARD_HORIZONS)} 個交易日的收盤；"
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
    stats = [(h, info["h"][str(h)]) for h in CARD_HORIZONS]
    if info["from"] is not None:
        head = f"{name}推薦（{note}{_md(info['from'])} 起，算到 {_md(info['to'])}）"
    else:
        head = f"{name}推薦還沒有成績" + (f"（{note}{_md(info['first_t'])} 開始記）" if info["first_t"] else "")
    lines = [stock_picks._text(head, weight="bold")]
    if info["from"] is None and not any(s["skipped"] for _, s in stats):
        return lines
    if info["ai_from"]:  # 「起含」：AI 關掉、回到舊規則之後這句照樣是真的（P3 #7）
        lines.append(stock_picks._text(f"{_md(info['ai_from'])} 起含 AI 相關股推薦", size="md", color=_GRAY))
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
    h = CARD_HORIZONS[0]
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
