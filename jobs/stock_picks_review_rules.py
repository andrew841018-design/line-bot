"""股票推薦回測 C3 精簡版（C3-lite）：背景規則評估。只算、只列，給週報用；不存表、不寫檔、不加階段。

規格：ops/state/line_bot_stock_backtest_20261028/PLAN_C3.md（v2）；細節出處 reviews/c3_plan_r1..r4*.md，主 Claude 的核實在
PHASE4_NOTES.md 的「C3 計畫審查」節。每週從頭重算「選單的變體（stock_picks_review_variants）vs 現行規則」，週報只列描述性的
數字：不判斷門檻、不寫建議句（等 Andrew 12 月回答 PLAN_C3 §11）。

輸入（全部唯讀，§1）
- 兩族：台股 AI 相關股（tw_ai，底規則 AI_BASE＝ai_v1）、美股（us，US_BASE＝us_v1）。成員＝pool_rows 裡 status 是 member
  的列（冷凍股是 frozen，不解析；r1 nit 11）。
- 選版一律用 returns 的公開函式（r1 I5）：family_versions → 只留 rule＝底規則的版本 → perf.close_deadline（日曆和週報的
  trading_days 同一份）→ select_version(picks＝None：E 收盤前最後一版）→ version_returns → pool_p（h＝20）→ pick_return。
  底規則的版本被過濾掉、但 E 收盤前有別的規則的版本，那天記 rule_mismatch；最新的一版不是底規則時，週報寫「現行規則已換成
  別的版本」（DB 的字串不進週報，§5）。
- derive_version：走法讀這一版最大的 derive_version 的列（只看 pool_rows，和 member_returns 無關：§3 的「不偷看」）；
  r 用 pool_p 回傳的 dv。pool_p 還沒有齊全的 dv，或它和走法讀的不同時，那一期先等（pending，不作廢；r3 n-4、r4 對 n-4
  的回應）。所以算進數字的每一期，列和 r 都是同一個 dv。
- 起算日 d0：各市場日曆上 variants.CREATED（2026-10-29）之後的第一個交易日（只用樣本外資料）。日曆守門（r4 I-A）：
  close_deadline 對早於日曆第一天的 t 會回日曆第一天的收盤時間，所以日曆上要有 CREATED 當天或更早的一天，才確定 d0 在
  日曆上；沒有就整段只寫一句「起算日已超出價格庫範圍，背景規則要改成存樣本日」。價格庫留 PRICE_KEEP_DAYS 天：日曆上
  最後一個不晚於 CREATED 的日子再過 PRICE_KEEP_DAYS＋1 天就被刪掉，前 WARN_DAYS 天起資料行先預告（r2 條件 2）。

選股引擎（純函式，§2）
- 解析（parse）：每一列各自 try、接住所有例外（r2 I-1、r3 I-5）；傳 {**values, "n": ""} 給 bot 的 _facts_from（名稱和規則
  無關，bot 之後收緊名稱規則時舊資料才不會整批作廢）。任何一列失敗或沒過一致性檢查 → 整版不可判定（bad_values），不跳過
  那一列（不然候選會和 bot 不同）。一致性：values 的代號＝列的 code；台股 values 的 d＝t；走勢標記是 pool／ok 時 values 的
  tr＝trend_json 的 tr，其他標記兩邊都沒有 tr；標記本身要是 pool 匯入 spool 時收的那幾種。美股的 t 也在逐列的 try 裡
  轉成日期（壞了只讓這一版不可判定，r3 n-4）。例外訊息不記：parse 回例外類別（type(exc).__name__），一致性檢查沒過回
  INCONSISTENT；判定不了的天按原因（bad_values、unknown_trend）與例外類別分開數（r2 I-2）。
- 參數（params_ok）：鍵要和底規則完全一樣、型別對；above_year 只認 True。不合格 → 那條規則 bad_params（fail closed）。
  真正的保護是 test_every_menu_variant_has_valid_params_and_bad_params_fail_closed（選單逐條驗過）；這裡只是便宜的保險（r4 N-2）。
- 台股 AI：條件 1 bool(ai_source)、普通股、上市（空字串不合格，同 bot）；條件 2 _tenths(單月年增) > gt、_tenths(累計) ≥ min；
  條件 3（use_pe 才看）本益比與中位數都是正數、_tenths(pe) ≥ 下限、_tenths(pe)×100 ≤ _tenths(中位數)×pct（整數）；
  排序 cum_yoy_over_pe 用 bot 的 ai_sort_key，cum_yoy 用 (−累計年增, 代號)。條件 4 照 _maybe_fill_ai 的走法一檔一檔看：
  pool／ok 用 _facts_from 驗過的走勢（走勢是 None 就不合格，同 bot）；no:*、skipped:* 確定不選（和規則無關）；
  unknown、unchecked、null 是未知，還沒湊滿 max_picks 就遇到 → 不可判定（unknown_trend）。
- 美股：普通股、fund＝OK、有走勢；長期向上直接用 _facts_from 回的 UsTrend（美分精度）；|p60| ≤ band 直接用 p60
  （us_checks_of 的 MID 寫死 ±5%）；use_growth／use_profit 時用 us_checks_of 依 GROWTH／PROFIT 取狀態；排序同 choose_us_picks。
- bot 的符號只用白名單（PLAN §4.1），一律在函式裡 import：stock_picks 的 _facts_from、_tenths、ai_sort_key、STOCK、TWSE、
  MAX_PICKS、NEAR_PERMILLE；stock_picks_us 的 _facts_from、us_checks_of、FUND_OK、STOCK、GROWTH、PROFIT、MAX_PICKS。

對帳（判斷 C3 可不可信的主要驗收，§2）
- 每一天選到的版本用底規則跑一次：選出的集合＝那一版 picked 的集合；整串候選（照排序）的代號與名次＝rank 不是 null 的
  那幾列（名次 1、2、3…，任何 status 的列有名次都算）；美股另外要 meta 的 picks_match 是真。不一致的那天整族都不取樣。
- 美股 excluded:no_close 的列（沒有 c0）照樣帶名次（derive_us 對整個池排名），引擎只解析 member，所以那天會記成對帳
  不一致（fail closed，r1 N-4、r3 n-2）。parity_mismatch > 0 時，先查那天美股有沒有沒收盤的候選，再懷疑引擎。
- 執行時比對 bot 的常數：兩個 MAX_PICKS 要等於選單的 max_picks、NEAR_PERMILLE 要等於 US_BASE 的 band；不一致整族不算
  （bot_rule_changed）。

取樣：逐變體走法（§3）
- 每個變體各自從 d0 起，取第一個「有底規則的版本、底規則可判定而且對帳一致、這個變體可判定、兩邊 picks 集合不同」的 t
  當一個樣本；下一個從這一期的 th（E 之後第 20 個交易日，同 perf.evaluate 的 T_h，用日曆算、不看報酬）起再找（t ≥ th）。
  th 超出日曆就不再往後找。
- 不偷看（r4 I-B）：取樣只看 t 當下就知道的事（上面四條全來自 pool_rows）。事後才知道的事只讓那一期作廢、不改走法；
  作廢了照樣跳到 th。判斷順序（第一個成立的算）：任一邊 0 檔（zero_picks，t 當下就知道）→ 等成績（pending，不算作廢）→
  股票池有值的成員不到 8 成（pool_missing，p 不是 ok）→ 任一邊的入選股都沒有 20 日報酬（no_returns）→ 兩邊有報酬的代號
  其實一樣（same_valued，r3 I-1）。
- 0 檔為什麼是取樣再作廢、不是跳過這天（r4 N-5）：一邊 0 檔時兩邊選的確實不一樣，四條在 t 當下都成立，所以照樣是一期；
  這一期怎麼算是 PLAN_C3 §11 Q3 的事（現在先作廢、另列期數）。Andrew 回答 Q3 時（例如算 0，或和股票池打平）只要改
  outcome 裡這一期怎麼算，走法與樣本日都不用變。
- 每週從 d0 重算、不存：引擎修正或 spool 晚到，過去的樣本可能跟著變；C3 只報告，可以接受。

數字（§4，只描述、不判斷）
- 每個變體：可比樣本 n、平均差（R(變體) − R(現行)，R＝入選股 r 的 math.fsum 平均，只算有 r 的；p 對兩邊相同所以和 p 無關）、
  較好的期數（差 > 0）、作廢與等成績的期數、分歧比例 k/N（底規則和這個變體都可判定的天裡，選得不一樣的天數）。
- 每族一行的「N 天裡有 k 天和現行選得不一樣」：N＝底規則可判定而且對帳一致的天，k＝其中至少一個可判定的變體選得不一樣
  的天（判定不了的變體不算分歧，資料行有空間時寫明，r1 N-6）。週報的作廢、等成績都是全部變體加起來的期數。

週報【背景規則】（§5）
- 週報一律把變體叫「試驗版本」、單位用「期」；k/N 寫成「N 天裡有 k 天…」，不寫成像日期的 k/N（r4 I-2）。
- 硬上限 MAX_CHARS 字；放不下就一層一層退（r4 I-3、r3 n-3）：每族只留第 1 行細節 → 每族一行 → 只留資料行 → 一句
  「這週的數字太長放不下」。只放選單的 id 與 desc、常數字串、數字、日期（r2 n-1）。
- 自己看 time.monotonic，超過 TIME_BUDGET_S 秒就只寫「這週算不完」（實測全部約 0.14 秒，r3）。
- 回傳 (行, 數字)：數字只有數字（例外類別的計數是 {類別: 天數}）；數字放在週報資料行，以及組週報那一輪的 last_run
  （weekly 階段的輸出）。任一族沒有正常算（bot 常數不同、沒有日曆、掉出日曆）時數字裡沒有 ready，檢查點寫「這週算不出來」。

頂層只 import 標準庫、pool、variants；returns、db 與 bot 的模組都在函式裡 import。這個模組 import 時不做任何 I/O。
"""

from __future__ import annotations

import bisect
import importlib
import json
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Mapping, NamedTuple, Optional, Sequence

from jobs import stock_picks_review_pool as pool
from jobs import stock_picks_review_variants as variants

HOLD_H = 20  # 出場日 th＝E 之後第 20 個交易日（perf 的 T_h），比的是 20 日報酬
MIN_SAMPLES = 6  # 週報「要 6 個」：之後的門檻（PLAN_C3 §11 Q2）用得到的最少樣本數
MAX_CHARS = 550  # 這一段的硬上限（r4 S5、r3 I-4：整份週報 5,000 字以內）
MAX_DETAIL = 2  # 樣本夠的變體，每族最多另列幾行
TIME_BUDGET_S = 30.0
WARN_DAYS = 60  # 起算日掉出價格庫前幾天開始預告
FAMILIES = (pool.FAMILY_AI, pool.FAMILY_US)
BASES = {pool.FAMILY_AI: variants.AI_BASE, pool.FAMILY_US: variants.US_BASE}
TITLE = "【背景規則】"
HEAD = TITLE + "只算，還沒有換任何規則"
INDENT = "　"  # 變體那幾行用全形空白縮排（Discord 會吃掉半形的行首空白）
OUT_OF_RANGE_TEXT = "・起算日已超出價格庫範圍，背景規則要改成存樣本日"
TOO_LONG_TEXT = "・這週的數字太長放不下"  # 最後一層（實際上走不到；不能只剩標題，看起來像沒東西要報）
UNDECIDABLE_NOTE = "；判定不了的試驗版本不算分歧"  # 資料行有空間才加（r1 N-6）

# 選股不可判定的原因（Selection.reason）；bad_values 的細項是例外類別，或一致性檢查沒過的 INCONSISTENT
BAD_PARAMS, BAD_VALUES, UNKNOWN_TREND = "bad_params", "bad_values", "unknown_trend"
INCONSISTENT = "inconsistent"
# 一天（族, t）可以取樣（USABLE），或不能取樣的原因
USABLE, WAIT_E, NO_VERSION, RULE_MISMATCH, UNDECIDED, PARITY = (
    "usable", "wait_e", "no_version", "rule_mismatch", "undecided", "parity")
# 一族的狀態
OK, BOT_RULE_CHANGED, NO_CALENDAR, OUT_OF_RANGE = "ok", "bot_rule_changed", "no_calendar", "out_of_range"
# 一期的結果：可比、等成績、作廢（照判斷順序）
COMPARABLE, PENDING = "comparable", "pending"
ZERO_PICKS, POOL_MISSING, NO_RETURNS, SAME_VALUED = "zero_picks", "pool_missing", "no_returns", "same_valued"
VOID_ORDER = (ZERO_PICKS, POOL_MISSING, NO_RETURNS, SAME_VALUED)
_VOID_WORDS = {ZERO_PICKS: "任一邊 0 檔", POOL_MISSING: "股票池有值的不到 8 成", NO_RETURNS: "入選股沒有 20 日報酬",
               SAME_VALUED: "有報酬的入選股兩邊一樣"}

_AI_SORTS = ("cum_yoy_over_pe", "cum_yoy")
_US_SORTS = ("abs_p60_weight_symbol",)
_KNOWN_TRENDS = ("pool", "ok")
_SKIP_PREFIXES = ("no:", "skipped:")
_INT_KEYS = {
    pool.FAMILY_AI: ("growth_month_yoy_tenths_gt", "growth_cum_yoy_tenths_min", "pe_tenths_min", "pe_vs_median_pct_max",
                     "p60_permille_max", "max_picks"),
    pool.FAMILY_US: ("band_permille", "max_picks"),
}
_BOOL_KEYS = {pool.FAMILY_AI: ("use_pe",), pool.FAMILY_US: ("use_growth", "use_profit")}


class _OutOfTime(Exception):
    """這一段超過 TIME_BUDGET_S 秒（只在這個模組裡丟、接）。"""


class Member(NamedTuple):
    """pool_rows 的一列（引擎只用這幾欄）。JSON 欄位是 DB 裡的原字串：解析在 parse 裡逐列 try。"""

    code: str
    status: str
    picked: bool
    rank: Any  # 名次（1 起）或 None；DB 的值原樣放，對帳時再驗型別
    trend_json: Optional[str]
    values_json: Optional[str]


class Parsed(NamedTuple):
    """解析過的一個成員：bot 的 Facts／UsFacts、走勢標記、AI 來源（美股是 None）。"""

    code: str
    facts: Any
    label: Optional[str]
    ai_source: Any


class Selection(NamedTuple):
    picks: Optional[tuple]  # None＝不可判定
    candidates: tuple  # 整串候選的代號（照排序；台股 AI 是條件 1–3，美股是全部條件）
    reason: Optional[str]


class Undecided(NamedTuple):
    """底規則判定不了的一天：原因（BAD_VALUES／UNKNOWN_TREND）與 bad_values 的細項（例外類別或 INCONSISTENT）。"""

    reason: str
    error: Optional[str]


# ── 引擎（純函式） ─────────────────────────────────────────────────────────────
def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def params_ok(family: str, params) -> bool:
    """參數的鍵要和底規則完全一樣、型別對；above_year 只認 True（False 沒有定義，r1 nit 10）。"""
    base = BASES.get(family)
    if base is None or not isinstance(params, Mapping) or set(params) != set(base):
        return False
    if params["above_year"] is not True or not isinstance(params["rule"], str):
        return False
    if not all(_is_int(params[key]) for key in _INT_KEYS[family]) or params["max_picks"] < 1:
        return False
    if not all(isinstance(params[key], bool) for key in _BOOL_KEYS[family]):
        return False
    return params["sort"] in (_AI_SORTS if family == pool.FAMILY_AI else _US_SORTS)


def _label_kind(label) -> str:
    """走勢標記的三類：known（pool、ok）｜no（no:*、skipped:*：確定不選，和規則無關）｜unknown（其他）。"""
    if label in _KNOWN_TRENDS:
        return "known"
    if isinstance(label, str) and label.startswith(_SKIP_PREFIXES):
        return "no"
    return "unknown"


def _parse_row(family: str, bot, row: Member, t: str) -> Optional[Parsed]:
    """一列 → Parsed；沒過一致性檢查回 None。例外（含美股 t 不是日期，r3 n-4）由 parse 接住。"""
    values = json.loads(row.values_json) if row.values_json is not None else None
    trend = json.loads(row.trend_json) if row.trend_json is not None else {}
    if not isinstance(values, dict) or not isinstance(trend, dict):
        return None
    label = trend.get("trend")
    if not pool._trend_label_ok(label):  # pool 匯入 spool 時收的那幾種（簡版、美股本來就在裡面）
        return None
    blank = {**values, "n": ""}  # 名稱和規則無關（r3 I-5）
    if family == pool.FAMILY_AI:
        facts = bot._facts_from(blank)
        if facts.code != row.code or facts.data_date.isoformat() != t:
            return None
    else:
        facts = bot._facts_from(blank, date.fromisoformat(t))
        if facts.symbol != row.code:
            return None
    tr_values, tr_trend = values.get("tr"), trend.get("tr")
    if label in _KNOWN_TRENDS:
        if tr_values != tr_trend:
            return None
    elif tr_values is not None or tr_trend is not None:
        return None
    return Parsed(row.code, facts, label, values.get("ai_source") if family == pool.FAMILY_AI else None)


def parse(family: str, members: Sequence[Member], t: str) -> tuple[Optional[list[Parsed]], Optional[str]]:
    """status＝member 的列 → ([Parsed], None)。任何一列解析失敗或沒過一致性檢查 → (None, 細項)：整版不可判定
    （bad_values）；細項是例外類別（type(exc).__name__，訊息不記），或一致性檢查沒過的 INCONSISTENT（r2 I-2）。"""
    bot = importlib.import_module("stock_picks" if family == pool.FAMILY_AI else "stock_picks_us")
    out: list[Parsed] = []
    for row in members:
        if row.status != pool.MEMBER:
            continue
        try:
            parsed = _parse_row(family, bot, row, t)
        except Exception as exc:  # noqa: BLE001 - 一列壞了只讓這一版不可判定（含 OverflowError、RecursionError）
            return None, type(exc).__name__
        if parsed is None:
            return None, INCONSISTENT
        out.append(parsed)
    return out, None


def _cum_key(row: Parsed) -> tuple:
    cum = row.facts.revenue.cum_yoy if row.facts.revenue is not None else None
    return (-cum if _finite(cum) else math.inf, row.code)


def _choose_ai(params: Mapping, parsed: Sequence[Parsed]) -> Selection:
    sp = importlib.import_module("stock_picks")
    candidates = []
    for row in parsed:
        f = row.facts
        if not (bool(row.ai_source) and f.kind == sp.STOCK and f.market == sp.TWSE):  # 條件 1（同 bot：空字串不合格）
            continue
        rev = f.revenue
        if rev is None or not _finite(rev.yoy) or not _finite(rev.cum_yoy):
            continue
        if not (sp._tenths(rev.yoy) > params["growth_month_yoy_tenths_gt"]
                and sp._tenths(rev.cum_yoy) >= params["growth_cum_yoy_tenths_min"]):
            continue
        if params["use_pe"]:
            pe, median = f.pe, f.pe_median
            if not (_finite(pe) and pe > 0 and _finite(median) and median > 0):
                continue
            pe_t, median_t = sp._tenths(pe), sp._tenths(median)
            if not (pe_t >= params["pe_tenths_min"] and pe_t * 100 <= median_t * params["pe_vs_median_pct_max"]):
                continue
        candidates.append(row)
    if params["sort"] == "cum_yoy_over_pe":
        candidates.sort(key=lambda r: sp.ai_sort_key(r.facts))
    else:
        candidates.sort(key=_cum_key)
    codes = tuple(r.code for r in candidates)
    picks: list[str] = []
    for row in candidates:  # 條件 4：照 _maybe_fill_ai 的走法
        kind = _label_kind(row.label)
        if kind == "no":
            continue
        if kind == "unknown":
            return Selection(None, codes, UNKNOWN_TREND)
        tr = row.facts.trend
        if tr is not None and tr.above_year and tr.p60 <= params["p60_permille_max"]:
            picks.append(row.code)
            if len(picks) >= params["max_picks"]:
                break
    return Selection(tuple(picks), codes, None)


def _choose_us(params: Mapping, parsed: Sequence[Parsed]) -> Selection:
    us = importlib.import_module("stock_picks_us")
    candidates = []
    for row in parsed:
        f, tr = row.facts, row.facts.trend
        if f.kind != us.STOCK or f.fund != us.FUND_OK or tr is None:
            continue
        if not tr.above_year:  # _facts_from 回的是 UsTrend：美分精度（r1 nit 2；test_trend_precision_… 釘住）
            continue
        if abs(tr.p60) > params["band_permille"]:
            continue
        states = {check.key: check.state for check in us.us_checks_of(f)}
        if params["use_growth"] and states.get(us.GROWTH) is not True:
            continue
        if params["use_profit"] and states.get(us.PROFIT) is not True:
            continue
        candidates.append(row)
    candidates.sort(key=lambda r: (abs(r.facts.trend.p60), -(r.facts.weight or 0.0), r.code))
    codes = tuple(r.code for r in candidates)
    return Selection(codes[:params["max_picks"]], codes, None)


def choose(family: str, params, parsed: Optional[Sequence[Parsed]]) -> Selection:
    """一條規則在一版上選出來的股票（parsed 是 None＝那一版解析失敗）。"""
    if not params_ok(family, params):
        return Selection(None, (), BAD_PARAMS)
    if parsed is None:
        return Selection(None, (), BAD_VALUES)
    return _choose_ai(params, parsed) if family == pool.FAMILY_AI else _choose_us(params, parsed)


def parse_and_choose(family: str, params, members: Sequence[Member], t: str) -> Selection:
    """解析＋選股（測試與離線對帳用；週報裡同一版只解析一次）。"""
    parsed, _ = parse(family, members, t)
    return choose(family, params, parsed)


def parity_ok(family: str, base: Selection, members: Sequence[Member], meta: Optional[dict]) -> bool:
    """底規則跑出來的和那一版記下的一致嗎：picked 集合、整串候選＝名次 1、2、3…的列（任何 status）、美股的 picks_match。
    base 要可判定（呼叫端先查 picks 不是 None）。"""
    ranks = [(m.rank, m.code) for m in members if m.rank is not None]
    if not all(_is_int(rank) and isinstance(code, str) for rank, code in ranks):  # 代號不是 str 時 sorted 會丟 TypeError
        return False
    if sorted(ranks) != list(enumerate(base.candidates, start=1)):
        return False
    if {m.code for m in members if m.picked} != set(base.picks):
        return False
    return family != pool.FAMILY_US or (isinstance(meta, dict) and meta.get("picks_match") is True)


def bot_rule_changed(family: str) -> bool:
    """bot 的常數和選單不同（PLAN_C3 §2）：MAX_PICKS 要等於 max_picks；美股的 NEAR_PERMILLE 要等於 band。"""
    sp = importlib.import_module("stock_picks")
    if family == pool.FAMILY_AI:
        return getattr(sp, "MAX_PICKS", None) != variants.AI_BASE["max_picks"]
    us = importlib.import_module("stock_picks_us")
    return (getattr(us, "MAX_PICKS", None) != variants.US_BASE["max_picks"]
            or getattr(sp, "NEAR_PERMILLE", None) != variants.US_BASE["band_permille"])


# ── 讀 DB ─────────────────────────────────────────────────────────────────────
_VERSION_WHERE = "m = ? AND rule = ? AND t = ? AND ver_s = ? AND derive_version = ?"


def load_members(conn, version, dv: int) -> list[Member]:
    """一版（某個 derive_version）的全部列，照代號排。"""
    return [Member(code, status, picked == 1, rank, trend_json, values_json)
            for code, status, picked, rank, trend_json, values_json in conn.execute(
                "SELECT code, status, picked, rank, trend_json, values_json FROM pool_rows"
                f" WHERE {_VERSION_WHERE} ORDER BY code", (version.m, version.rule, version.t, version.ver_s, dv))]


def load_meta(conn, version, dv: int) -> Optional[dict]:
    found = conn.execute(f"SELECT meta_json FROM pool_days WHERE {_VERSION_WHERE}",
                         (version.m, version.rule, version.t, version.ver_s, dv)).fetchone()
    try:
        meta = json.loads(found[0]) if found and found[0] else None
    except Exception:  # noqa: BLE001 - 壞的 meta 只讓那天對帳不一致（同 parse）
        return None
    return meta if isinstance(meta, dict) else None


# ── 每一天、每一族 ─────────────────────────────────────────────────────────────
@dataclass
class Day:
    """一族在一個 t 可以取樣的那一版（底規則可判定、對帳一致）。"""

    t: str
    th: Optional[str]  # E 之後第 HOLD_H 個交易日；超出日曆是 None
    base: Selection
    parsed: list
    members: dict  # version_returns 的成員 {dv: 代號}
    returns: dict  # version_returns 的 member_returns {(dv, h, 代號): (r, b, skip)}
    walk_dv: int  # 引擎讀的 derive_version（這一版最大的）
    p_status: str
    p_dv: Optional[int]  # pool_p 回傳的 dv


@dataclass
class VariantStats:
    id: str
    desc: str
    samples: list = field(default_factory=list)  # 樣本日（t）
    diffs: list = field(default_factory=list)  # 可比的那幾期的差
    void: Counter = field(default_factory=Counter)
    pending: int = 0
    k: int = 0  # 選得不一樣的天
    n_days: int = 0  # 底規則和這個變體都可判定的天

    @property
    def n(self) -> int:
        return len(self.diffs)

    @property
    def mean(self) -> Optional[float]:
        return _mean(self.diffs) if self.diffs else None

    @property
    def better(self) -> int:
        return sum(1 for d in self.diffs if d > 0)


@dataclass
class FamilyResult:
    family: str
    state: str = OK
    variants: list = field(default_factory=list)
    days: Counter = field(default_factory=Counter)  # 可以取樣（USABLE）與各種不能取樣的原因的天數
    undecided: Counter = field(default_factory=Counter)  # 底規則判定不了的天，按原因（bad_values、unknown_trend）
    bad_values_types: Counter = field(default_factory=Counter)  # bad_values 的天，按例外類別（或 inconsistent）
    differ_days: int = 0  # 至少一個可判定的變體選得不一樣的天（USABLE 的天裡）
    switched: bool = False  # 最新的一版不是底規則
    drop_day: Optional[date] = None  # 日曆上最後一個不晚於 CREATED 的日子被價格庫刪掉的日子（估）

    @property
    def checked(self) -> int:
        """底規則可判定、拿去對帳的天。"""
        return self.days[USABLE] + self.days[PARITY]


def exit_day(calendar: Sequence[str], t: str) -> Optional[str]:
    """th：E（t 之後第一個交易日）之後第 HOLD_H 個交易日（和 perf.evaluate 的 T_h 同一個位置）；超出日曆是 None。
    th 只用來排樣本間隔（成員報酬的 T_h 由 returns 自己算）。日曆是週報的價格庫日曆（weekly.trading_days）；perf 用的是
    價格庫加市場庫的聯集日曆，聯集多出來的日子只會讓 T_h 一樣或更早，所以這裡的 th 只會一樣或更晚：間隔比 perf 保守，
    下一期不會和上一期重疊（r1 N-5）。"""
    i = bisect.bisect_right(calendar, t) + HOLD_H
    return calendar[i] if i < len(calendar) else None


def _mean(values) -> float:
    values = list(values)
    return math.fsum(values) / len(values)


def _valued(returns_mod, day: Day, picks: Sequence[str]) -> dict[str, float]:
    """入選股裡有 20 日報酬的 {代號: r}（用 pool_p 的 dv）。"""
    out = {}
    for code in picks:
        found = returns_mod.pick_return(day.members, day.returns, HOLD_H, code, day.p_dv)
        if found is not None and found[2] is None and _finite(found[0]):
            out[code] = float(found[0])
    return out


def outcome(returns_mod, day: Day, picks: Sequence[str]) -> tuple[str, Optional[float]]:
    """一期樣本的結果：(COMPARABLE, 差) 或 (PENDING／作廢原因, None)。判斷順序見模組說明。"""
    if not day.base.picks or not picks:
        return ZERO_PICKS, None
    if day.p_dv is None or day.p_dv != day.walk_dv:
        return PENDING, None
    if day.p_status != "ok":
        return POOL_MISSING, None
    base, mine = _valued(returns_mod, day, day.base.picks), _valued(returns_mod, day, picks)
    if not base or not mine:
        return NO_RETURNS, None
    if set(base) == set(mine):
        return SAME_VALUED, None
    return COMPARABLE, _mean(mine.values()) - _mean(base.values())


def _day(conn, returns_mod, perf, family: str, calendar: tuple, t: str, versions: Sequence,
         with_returns: bool) -> Day | Undecided | str:
    """一族在 t 的那一版（Day）、底規則判定不了（Undecided），或其他不能取樣的原因。"""
    base = BASES[family]
    deadline = perf.close_deadline(pool.FAMILY_BY_KEY[family].market, t, calendar)
    if deadline is None:
        return WAIT_E
    version, _ = returns_mod.select_version([v for v in versions if v.rule == base["rule"]], deadline, None)
    if version is None:
        other, _ = returns_mod.select_version(versions, deadline, None)
        return RULE_MISMATCH if other is not None else NO_VERSION
    members, rets = returns_mod.version_returns(conn, family, version, with_returns)
    if not members:
        return NO_VERSION
    walk_dv = max(members)
    p_status, _, p_dv = returns_mod.pool_p(members, rets, HOLD_H)
    rows = load_members(conn, version, walk_dv)
    parsed, error = parse(family, rows, t)
    selection = choose(family, base, parsed)
    if selection.picks is None:
        return Undecided(selection.reason, error)
    meta = load_meta(conn, version, walk_dv) if family == pool.FAMILY_US else None
    if not parity_ok(family, selection, rows, meta):
        return PARITY
    return Day(t, exit_day(calendar, t), selection, parsed, members, rets, walk_dv, p_status, p_dv)


# TODO（deferred.md 2026-11-21 C3-3）：記憶體隨天數線性成長（實測 480 天、各族約 20 萬列約 121 MB、2.3 秒），C2b 時重量
def evaluate_family(conn, family: str, calendar: Sequence[str], *, with_returns: bool,
                    check: Callable[[], None] = lambda: None) -> FamilyResult:
    """一族：每一天的版本與對帳、每個變體的 k/N 與走法（見模組說明）。check() 超過時間時丟 _OutOfTime。"""
    from jobs import stock_picks_review_db as rdb
    from jobs import stock_picks_review_returns as returns_mod

    result = FamilyResult(family)
    menu = [v for v in variants.VARIANTS if v["family"] == family]
    result.variants = [VariantStats(v["id"], v["desc"]) for v in menu]
    if bot_rule_changed(family):
        result.state = BOT_RULE_CHANGED
        return result
    cal = tuple(calendar)
    if not cal:
        result.state = NO_CALENDAR
        return result
    start = bisect.bisect_right(cal, variants.CREATED)
    if start == 0:  # 日曆上沒有 CREATED 當天或更早的日子：d0 可能已經掉出日曆
        result.state = OUT_OF_RANGE
        return result
    result.drop_day = date.fromisoformat(cal[start - 1]) + timedelta(days=rdb.PRICE_KEEP_DAYS + 1)
    perf = importlib.import_module("stock_picks_perf")
    versions = returns_mod.family_versions(conn, family, pool.FAMILY_BY_KEY[family].where)
    newest = max((v for by_t in versions.values() for v in by_t), key=lambda v: (v.t, v.computed_at), default=None)
    result.switched = newest is not None and newest.rule != BASES[family]["rule"]
    if start >= len(cal):  # 日曆上還沒有起算日
        return result
    d0 = cal[start]
    days: list[Day] = []
    for t in sorted(t for t in versions if t >= d0):
        check()
        day = _day(conn, returns_mod, perf, family, cal, t, versions[t], with_returns)
        if isinstance(day, Undecided):
            result.days[UNDECIDED] += 1
            result.undecided[day.reason] += 1
            if day.error is not None:
                result.bad_values_types[day.error] += 1
            continue
        if not isinstance(day, Day):
            result.days[day] += 1
            continue
        result.days[USABLE] += 1
        days.append(day)
    choices = {}  # (變體, t) → Selection
    for variant, stats in zip(menu, result.variants):
        params = variants.params_of(variant["id"])
        for day in days:
            check()
            choices[(variant["id"], day.t)] = choose(family, params, day.parsed)
    for day in days:
        picks = [choices[(v["id"], day.t)].picks for v in menu]
        if any(p is not None and set(p) != set(day.base.picks) for p in picks):
            result.differ_days += 1
    for variant, stats in zip(menu, result.variants):
        next_from: Optional[str] = d0
        for day in days:
            picks = choices[(variant["id"], day.t)].picks
            if picks is None:
                continue
            stats.n_days += 1
            differ = set(picks) != set(day.base.picks)
            stats.k += differ
            if next_from is None or day.t < next_from or not differ:
                continue
            stats.samples.append(day.t)
            kind, diff = outcome(returns_mod, day, picks)
            if kind == COMPARABLE:
                stats.diffs.append(diff)
            elif kind == PENDING:
                stats.pending += 1
            else:
                stats.void[kind] += 1
            next_from = day.th  # None：th 超出日曆，不再往後找
    return result


# ── 週報的文字 ───────────────────────────────────────────────────────────────
def _diff_text(mean: float) -> str:
    from jobs import stock_picks_review_returns as returns_mod

    pm = returns_mod._permille(mean)
    if pm == 0:
        return "和現行差不多（0.0%）"
    return f"比現行{'多' if pm > 0 else '少'} {abs(pm) // 10}.{abs(pm) % 10}%"


def _range(values: Sequence[int]) -> str:
    lo, hi = min(values), max(values)
    return f"{lo}" if lo == hi else f"{lo}–{hi}"


def _family_line(result: FamilyResult) -> str:
    name = pool.FAMILY_BY_KEY[result.family].name
    if result.state == BOT_RULE_CHANGED:
        return f"・{name}：bot 的選股常數（最多幾檔、季線範圍）和選單不一樣，這週不算"
    if result.state == NO_CALENDAR:
        return f"・{name}：價格庫還沒有交易日，這週不算"
    head = f"現行規則已換成別的版本，背景比較停在 {BASES[result.family]['rule']}（要新選單）；" if result.switched else ""
    stats = result.variants
    line = f"・{name}：{head}{len(stats)} 個試驗版本，每個可比 {_range([s.n for s in stats])} 期（要 {MIN_SAMPLES} 期）"
    pending = sum(s.pending for s in stats)
    if pending:
        line += f"，合計另有 {pending} 期等 20 日成績"
    usable = result.days[USABLE]
    return line + (f"；{usable} 天裡有 {result.differ_days} 天和現行選得不一樣" if usable else "；還沒有可以比的天")


def _weight(stats: VariantStats) -> float:
    """排序用：分歧比例 × 平均差的大小（「換上之後平均每期多多少」的估計，§4）。"""
    return abs(stats.k / stats.n_days * stats.mean) if stats.n_days and stats.mean is not None else 0.0


def _detail_lines(result: FamilyResult, limit: Optional[int] = None) -> list[str]:
    """樣本夠的試驗版本最多 limit 行（預設 MAX_DETAIL；照 |k/N × 平均差| 排），其餘收成一行「另外 N 個」：只列一部分時
    也要說還有幾個，不然和檢查點「樣本夠 6 期的試驗版本 n 個」對不起來（C3 第 2 輪審查 正確性 F-3）。"""
    if result.state != OK:
        return []
    limit = MAX_DETAIL if limit is None else limit
    ready = sorted((s for s in result.variants if s.n >= MIN_SAMPLES), key=lambda s: (-_weight(s), s.id))
    lines = [f"{INDENT}{s.id}（{s.desc}）：{s.n} 期平均{_diff_text(s.mean)}，其中 {s.better} 期較好；"
             f"能判定的 {s.n_days} 天裡 {s.k} 天選得不一樣" for s in ready[:limit]]
    if len(ready) > limit:
        lines.append(f"{INDENT}另外 {len(ready) - limit} 個試驗版本也有 {MIN_SAMPLES} 期以上可比樣本")
    return lines


def _data_line(results: Sequence[FamilyResult], today: date) -> str:
    from jobs import stock_picks_review_returns as returns_mod

    checked = sum(r.checked for r in results)
    agreed = sum(r.days[USABLE] for r in results)
    line = (f"・資料：現行規則重算和 bot 記下的，比了 {checked} 天、一致 {agreed} 天" if checked
            else "・資料：還沒有可以對帳的天")
    undecided = sum(r.days[UNDECIDED] for r in results)
    bad = sum(r.undecided[BAD_VALUES] for r in results)
    mismatch = sum(r.days[RULE_MISMATCH] for r in results)
    if undecided:
        line += f"；現行規則判定不了 {undecided} 天" + (f"，其中資料讀不懂 {bad} 天" if bad else "")
    if mismatch:
        line += f"；只有別的規則的紀錄 {mismatch} 天"
    void = Counter()
    for r in results:
        for s in r.variants:
            void.update(s.void)
    total = sum(void.values())
    line += f"；作廢 {total} 期"
    if total:
        line += "（" + "、".join(f"{_VOID_WORDS[k]} {void[k]}" for k in VOID_ORDER if void[k]) + "）"
    drops = [r.drop_day for r in results if r.drop_day is not None]
    if drops and today >= min(drops) - timedelta(days=WARN_DAYS):
        line += f"；約 {returns_mod._md(min(drops).isoformat())} 起要改成存樣本日"
    return line


def format_lines(results: Sequence[FamilyResult], today: date) -> list[str]:
    """週報這一段；任一族的起算日掉出日曆就整段只寫一句。放不下 MAX_CHARS 字就一層一層退，不一次退到底（r4 I-3、
    r3 n-3）：每族最多 MAX_DETAIL 行細節（＋「另外 N 個」）→ 每族只留第 1 行細節（其餘一樣收成「另外 N 個」）→ 每族一行
    → 只留資料行 → 一句
    「這週的數字太長放不下」。資料行的「判定不了的試驗版本不算分歧」只在有試驗版本判定不了的天、而且放得下時才寫。"""
    if any(r.state == OUT_OF_RANGE for r in results):
        return [HEAD, OUT_OF_RANGE_TEXT]
    data = _data_line(results, today)
    noted = data + UNDECIDABLE_NOTE if any(s.n_days < r.days[USABLE] for r in results for s in r.variants) else data
    for limit in (MAX_DETAIL, 1, 0):  # 0：每族一行，不列細節
        body = [HEAD]
        for result in results:
            body += [_family_line(result), *(_detail_lines(result, limit) if limit else [])]
        for tail in (noted, data):
            if len("\n".join(body + [tail])) <= MAX_CHARS:
                return body + [tail]
    # 下面兩層實際上走不到（每族一行的最壞寫法有測試）；只是不讓這一段把整份週報擠出 5,000 字
    if len(HEAD + "\n" + data) <= MAX_CHARS:
        return [HEAD, data]
    return [HEAD, TOO_LONG_TEXT]


def numbers_of(results: Sequence[FamilyResult]) -> dict:
    """weekly 階段的輸出：對帳、取樣的計數與樣本夠的變體數（只有數字；bad_values_types 是 {例外類別: 天數}）。
    任一族沒有正常算（state 不是 OK）時不放 ready：檢查點寫「這週算不出來」，不只拿另一族的數字（r3 n-6、r1 N-9）。"""
    stats = [s for r in results for s in r.variants]
    numbers = {
        "computed": 1,
        "checked_days": sum(r.checked for r in results),
        "parity_ok": sum(r.days[USABLE] for r in results),
        "parity_mismatch": sum(r.days[PARITY] for r in results),
        "undecided": sum(r.days[UNDECIDED] for r in results),
        "undecided_bad_values": sum(r.undecided[BAD_VALUES] for r in results),
        "undecided_unknown_trend": sum(r.undecided[UNKNOWN_TREND] for r in results),
        "bad_values_types": dict(sum((r.bad_values_types for r in results), Counter())),
        "rule_mismatch": sum(r.days[RULE_MISMATCH] for r in results),
        "no_version": sum(r.days[NO_VERSION] for r in results),
        "wait_e": sum(r.days[WAIT_E] for r in results),
        "bot_rule_changed": sum(1 for r in results if r.state == BOT_RULE_CHANGED),
        "no_calendar": sum(1 for r in results if r.state == NO_CALENDAR),
        "samples": sum(len(s.samples) for s in stats),
        "comparable": sum(s.n for s in stats),
        "pending": sum(s.pending for s in stats),
        "void": sum(sum(s.void.values()) for s in stats),
    }
    if all(r.state == OK for r in results):
        numbers["ready"] = sum(1 for s in stats if s.n >= MIN_SAMPLES)
    return numbers


def report_lines(ctx, conn, calendars: Mapping[str, Sequence[str]], *, clock: Callable[[], float] = time.monotonic,
                 budget: float = TIME_BUDGET_S) -> tuple[list[str], dict]:
    """週報的【背景規則】一段與數字（weekly.rules_lines 在自己的 try 裡呼叫）。calendars＝{市場: 價格庫的交易日}
    （weekly.trading_days，和選版的 E 同一份）。member_returns 是舊結構時丟 rdb.SchemaError（同 returns.report_data）。"""
    from jobs import stock_picks_review_db as rdb

    started = clock()

    def check() -> None:
        if clock() - started > budget:
            raise _OutOfTime()

    with_returns = rdb.has_tables(conn, rdb.C1_TABLES)
    # TODO（C3 實作後審查 r4 N-3；deferred.md 2026-11-21 C3-4）：和 returns.report_data 同一段結構檢查；下次動 returns.py 時
    # 抽成一個函式共用
    if with_returns and rdb.member_returns_key(conn) != rdb.MEMBER_RETURNS_KEY:
        raise rdb.SchemaError("member_returns columns")
    try:
        results = [evaluate_family(conn, family, calendars.get(pool.FAMILY_BY_KEY[family].market, ()),
                                   with_returns=with_returns, check=check) for family in FAMILIES]
    except _OutOfTime:
        return [f"{TITLE}這週算不完（超過 {budget:g} 秒），其他內容照常"], {"computed": 0, "out_of_time": 1}
    numbers = numbers_of(results)
    if any(r.state == OUT_OF_RANGE for r in results):
        numbers["out_of_range"] = 1
    numbers["seconds"] = round(clock() - started, 3)
    return format_lines(results, ctx.today), numbers
