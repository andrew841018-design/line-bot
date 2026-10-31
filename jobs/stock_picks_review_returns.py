"""股票推薦回測（計畫 B）C1：股票池成員的報酬（member_returns）、股票池基準 p、週報的推薦成績。

規格：ops/state/line_bot_stock_backtest_20261028/PLAN.md（v5）§3（member_returns；p 出報表時才算；derive_version
齊全才用）、§6（C1）、§9（C1 測試）。算報酬一律用 stock_picks_perf 的公開函式（Candidate、evaluate、load_tw、load_us、
close_deadline、read_ledger_rows、parse_rows、pins_of、pinned_line）：窗口、日曆、除權息、期限都不在這裡重寫。

版本（都在 member_returns 的 PK 裡；查詢一律只看目前的版本，審查 C1 r2#1、r4#3、r4#6）
- SUPPORTED_LEDGER_VERSION：只在 perf 帳的版本（stock_picks_perf.LEDGER_VERSION）是它時才算（不是就回
  skipped:ledger_version）。perf 帳升版時：改這個常數，並確認 perf.INPUT_KEYS（inputs_json 照它存）。舊列留著備查，
  新版本在 700 天價格窗內分好幾輪重算（每輪有預算），算完之前 p 是 pending。
- CALC_VERSION：job 端算法的版本（MarketData 的疊價順序、missing 的規則、成員的定義）。改了就升，做法同上。
- 池子的推導版本是 derive_version（池子表本來就有，見 p）。

returns 階段（價格之後、週報之前；和其他階段一樣各自 try，壞掉不擋週報）
- 對象：近 700 天的每個池子版本（pool_days 一列）。只算 E（t 之後第一個交易日）收盤前算出來的版本
  （computed_at < perf.close_deadline，和 perf 挑紀錄同一個期限、同樣嚴格小於：E 收盤以後才算出來的版本永遠不算，
  記 late）；日曆上還沒有 E 的先等（wait_e）。
- 成員：pool_rows 裡 status 不是 frozen 的每一列（member 與 excluded:*；C2 之前沒有 frozen）。每個 h（perf.HORIZONS）
  還沒有目前版本的 member_returns 的，用 perf.evaluate(Candidate(m, rule, t, ver_s, code, close=c0, h), data)：
  FREEZE 就寫入（r、b、skip、算式輸入），WAIT 留到下一輪。凍結後不改（ON CONFLICT DO NOTHING）。
- 一定還沒到期的先略過、不載入（審查 C1 r2#4、r4#4）：T_h 還不在日曆上（_sessions_to > 0）時，evaluate 的第一個檢查
  一定回 not_due。版本層看最快到期的那個 h（not_due_versions 數版本），成員層看自己的 h（not_due_members 數
  (成員, h)）；waiting 只剩真的在等價格、除權息、日曆的列。
- c0 是 NULL 的成員（池子紀錄的 excluded:no_close，例如美股 ⚪ 拿不到 D 收盤）：照樣交給 evaluate（close＝None），
  只要它不再回 not_due，就直接記 skip＝missing，不等 settle（r、b、算式輸入都不記，審查 C1 r1#3）；perf 先凍結的也一律記
  missing（不編數字、不拿後來補到的收盤回填，impl r3#6）。所以它算在 p 的分母裡（有值的成員要 ≥ 80%），不是默默從
  股票池拿掉。
- MarketData（有要算的列才讀盤勢 DB）：
  - 台股：perf.load_tw(…, prices, today, db_path)。prices＝job 的價格庫（700 天，DB tw 沒有的成員也在）← DB tw_all
    ← DB tw：同一天以 DB 為準，順序和 perf 自己組的一樣（tw 優先、再來 tw_all、最後才是自己存的）；DB 的收盤照 perf
    的規則清（有限、0 < 收盤 < 1e6）。日曆＝DB 的 TAIEX ∪ 價格庫的 TAIEX。
  - 美股：perf.load_us(…, prices＝價格庫 ← DB us)（價格庫每 25 分鐘才同步一次，疊上 DB 的才和 perf 看到的一樣；
    load_us 自己會清）。日曆＝SPY／VOO／QQQ 的日期聯集（DB ∪ 價格庫）。
  - 基準 0050、SPY 由 load_tw／load_us 自己加進代號。
- 兩個市場各自隔離（審查 C1 r3#4）：盤勢 DB 忙碌（DbBusy、hot journal）→ 那個市場這一輪不算，markets＝busy（不算
  失敗）；其他讀不到的 → markets＝error:<例外類別>，也只跳過那個市場。另一個市場照算、照寫。
- perf 回了看不懂的一列（member_row 丟 ValueError）：那一列不寫、記 unexpected，其他列照寫（審查 C1 r1#4）。
- 階段狀態：dry-run 一律 dry_run；否則 error（error＝第一個出錯的市場的例外類別，沒有才是 UnexpectedPerfRow）> busy
  > ok。error 照樣算失敗、走連續失敗短訊。
- alarm 預算：這一階段最多用 BUDGET_S 秒（每個版本之間、每評 CHECK_EVERY 筆檢查一次），到了就停（out_of_time）；一輪最多
  載入 MAX_CANDIDATES 筆（記憶體上限，capped；升 calc_version、帳升版要整批重算時才會碰到）。剩下的下一輪再來。
  版本照「最快到期的那個 h 的 T_h 在日曆上的位置」排、成員列照自己的 T_h 排：快到期的先算。凍結的列每滿 INSERT_CHUNK
  就寫一次（alarm 在評估途中觸發，前面算好的也不白費，審查 C1 r2#5）。
- last_run 只放數字與列舉值：evaluated／frozen／waiting（總數與各族）、各市場 ok｜busy｜error:<類別>、wait 原因的次數、
  略過與預算的計數。runner 另記 returns 最後一次 ok 的時間（returns_last_ok_at：busy 不算失敗，一直 busy 靠它看得到）。

p（股票池等權平均；出報表時才算、不另存）
- 對 (m, 族, rule, t, ver_s, h)：取「每個成員都有 member_returns 列」的最大 derive_version（新的 derive_version 重算到
  一半時退回上一個齊全的版本，辯論 r3）；都不齊＝pending。齊全時有值的成員 ≥ 80% → p＝平均 r；不到 → pool_missing。

週報的推薦成績（report_lines；週報階段呼叫，失敗只讓這一段變成一行說明）
- 家人看到的推薦：r、b 取 perf 帳（read_ledger_rows，v2 列），挑列和 perf.aggregate 一樣：每個 (m, t) 釘住帳裡第一次寫的
  (rule, rev)、紀錄檔找得到那一行就只算代號還在那一行裡的、skip 是 None 才算進次數。和家人成績卡同一本帳：台股 AI 加上
  家人看到的舊五條件，次數＝成績卡台股的次數（有對帳測試）。帳只在 stock_picks.STATE_DIR 和 job 的 state/ 是同一個目錄
  時才讀（正式環境本來就是；測試、staging 用別的 bot 目錄時不讀別人的帳，週報寫一行說明）。
- 台股 AI、美股和股票池比。選版（拿哪一版的 p 來比）：同一個 (m, 族, t)，取 E 收盤前算出來、picks 和帳釘住那一行相同的
  最後一版；找不到就用 E 收盤前的最後一版（version_fallback）；一版都沒有是 version_missing（例如池子紀錄 10/27 才開始記）。
- 台股舊五條件（對照組）只用背景日（PLAN §6，審查 C1 r1#2、r3#2）：家人那天沒看到舊五條件（帳裡那個 t 沒有釘住 v1）、
  而且有 E 收盤前的 v1 池子版本的 t，用最後一版的入選列的 member_returns。
- 家人看到的舊五條件（帳裡釘住 v1 的 t，10/08–10/27）另成一段，放最後：不選版、沒有 p，只和 0050 比。一個 t 有沒有
  釘住 v1 二選一，所以和對照組不重疊。
- 每段每個 h：次數、平均報酬、0050／SPY 同期都算全部次數；和股票池比只用有 p 的那幾次（同一批推薦）：可比的次數、這幾次
  的推薦平均、股票池平均、贏股票池的次數（r > p）。次數 < 5 寫「紀錄還不夠（目前 n 次）」，可比的次數 < 5 寫「股票池
  可比的紀錄還不夠（目前 k 次）」（審查 C1 r1#1、r3#1）。
- 自我檢查：同一筆推薦 (m, t, 代號, h)，perf 帳的 r 和 member_returns 的 r 差超過 0.1 個百分點的筆數（另列一邊有值、
  一邊跳過的筆數）；只比台股 AI、美股（家人看到的舊五條件不選版）。

這個模組 import 時不做任何 I/O；bot 的模組（stock_picks_perf、stock_picks、stock_db_ro）在函式裡才 import。
"""

from __future__ import annotations

import bisect
import importlib
import json
import logging
import math
import os
import re
import time
import traceback
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from jobs import stock_picks_review_pool as pool

log = logging.getLogger("stock_picks_review")

SUPPORTED_LEDGER_VERSION = 2  # 只在 perf 帳是這個版本時才算（PLAN §3；perf 帳升版時怎麼改見模組說明）
# job 端算法的版本：MarketData 的疊價順序、missing 的規則、成員的定義改了就升。舊列留著備查，新的版本在 700 天價格窗內
# 分好幾輪重算，算完之前 p 是 pending。
CALC_VERSION = 1
# TODO（審查 C1 r2#3，延後）：預算改看整輪還剩多少（Context 帶整輪的 deadline，取 min(BUDGET_S, 剩下的秒數)）。
#   r2 實測冷啟動 15.3 秒，對 240 秒的 alarm 餘裕很大，先不做。
BUDGET_S = 60.0  # 這一階段最多用幾秒（整輪 alarm 240 秒，週報還要跑）
CHECK_EVERY = 500  # 每評幾筆看一次時間
MAX_CANDIDATES = 200_000  # 一輪最多載入幾筆要評的（記憶體上限；略過 not_due 之後平常遠低於這個數）
INSERT_CHUNK = 2000
MIN_N = 5  # 次數少於這個寫「紀錄還不夠」（和 perf.MIN_N 一樣）
P_NUM, P_DEN = 4, 5  # 有值的成員 ≥ 4/5（80%）才算 p
SELF_CHECK_TOLERANCE = 0.001  # 0.1 個百分點
MAX_PRICE = 1e6  # DB 收盤只收 0 < 收盤 < 1e6 的有限數（和 perf 清 DB 收盤的規則一樣）
MISSING = "missing"
NOT_DUE = "not_due"  # perf.evaluate 的 WAIT 原因：T_h（加緩衝）還不在日曆上（perf 沒有這個常數）
FAMILY_MARKET = {family.key: family.market for family in pool.FAMILIES}
SEEN_V1 = "tw_v1_seen"  # 家人看到的舊五條件（帳裡釘住 v1 的 t）：週報另成一段，不是池子的族
NOT_COMPARED = "not_compared"  # SEEN_V1 的 p_status：不選版、不和股票池比
_CURRENT_MEMBER = f"status <> '{pool.FROZEN}'"  # 目前的成員（C2 的冷凍股不算）：成員報酬、p 的三條查詢共用
INSERT_SQL = (  # 凍結後不改：同一個鍵已經有就不動。不用 INSERT OR IGNORE：那個連 CHECK 不合格的列都會默默丟掉
    "INSERT INTO member_returns (m, family, rule, t, ver_s, derive_version, code, h, ledger_version, calc_version,"
    " r, b, skip, inputs_json, frozen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"
)


# ── 小工具 ─────────────────────────────────────────────────────────────────
def _finite(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


_ISO_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _iso(value) -> bool:
    """YYYY-MM-DD 的合法日期（同 stock_picks_review_pool.iso_day；這裡每個收盤都要看，不每次 import）。"""
    if not isinstance(value, str) or not _ISO_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _where(exc: BaseException) -> Optional[str]:
    """例外最後發生在哪個函式（模組:函式:行號）；只有程式位置，沒有資料（和 runner 的 _where 同一種寫法）。"""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return None
    last = frames[-1]
    return f"{Path(last.filename).stem}:{last.name}:{last.lineno}"


def clean_series(table: dict) -> dict:
    """{代號: {日期: 收盤}} 只留合法日期、0 < 收盤 < 1e6 的有限數（perf 清 DB 收盤的同一條規則）。"""
    out: dict[str, dict] = {}
    for code, series in table.items():
        clean = {}
        for day, value in (series or {}).items():
            number = _finite(value)
            if number is not None and 0 < number < MAX_PRICE and _iso(day):
                clean[day] = number
        out[code] = clean
    return out


def family_of(market: str, rule: str) -> Optional[str]:
    """perf 帳的 (m, rule) → 族：v1 → tw_v1、ai_* → tw_ai、us 的任何 rule → us（和 pool.FAMILIES 的 where 一致，有測試）。"""
    if market == "us":
        return "us"
    if market == "tw" and rule == "v1":
        return "tw_v1"
    if market == "tw" and isinstance(rule, str) and rule.startswith("ai_"):
        return "tw_ai"
    return None


# 交易日的代號：台股看加權指數，美股看三檔 ETF 的聯集。和 stock_db_ro.TW_CALENDAR_SYMBOL／US_CALENDAR_SYMBOLS 同一組
# （有測試對照）；放常數、不 import stock_db_ro：週報的【資料收集】也用這個日曆，不能因為 stock_db_ro 壞了就組不出來。
CALENDAR_SYMBOLS = {"tw": ("TAIEX",), "us": ("SPY", "VOO", "QQQ")}


def store_calendar(conn, market: str, since: Optional[str] = None) -> list[str]:
    """價格庫裡的交易日：台股 TAIEX、美股 SPY／VOO／QQQ 任一檔有收盤的日子（CALENDAR_SYMBOLS）。
    週報（weekly.trading_days，選版用的 E）也用這個。"""
    symbols = CALENDAR_SYMBOLS[market]
    marks = ",".join("?" * len(symbols))
    sql = f"SELECT DISTINCT d FROM prices WHERE m = ? AND code IN ({marks})"
    params: list[Any] = [market, *symbols]
    if since is not None:
        sql += " AND d >= ?"
        params.append(since)
    return [d for (d,) in conn.execute(sql + " ORDER BY d", params) if _iso(d)]


def market_calendar(conn, market: str, since: str, market_db: Path) -> tuple[str, ...]:
    """DB 的日曆（stock_db_ro.tw_calendar／us_calendar）∪ 價格庫的日曆：DB 只留約 290 根，價格庫留 700 天。"""
    import stock_db_ro

    loader = stock_db_ro.tw_calendar if market == "tw" else stock_db_ro.us_calendar
    days = {d for d in loader(since, path=market_db) if _iso(d)} | set(store_calendar(conn, market, since))
    return tuple(sorted(days))


def store_prices(conn, market: str, codes: Iterable[str], since: str) -> dict[str, dict]:
    """價格庫裡這些代號從 since 起的收盤（一次最多 500 個代號）。"""
    out: dict[str, dict] = {}
    symbols = sorted(set(codes))
    for start in range(0, len(symbols), 500):
        chunk = symbols[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for code, day, close in conn.execute(
                f"SELECT code, d, close FROM prices WHERE m = ? AND d >= ? AND code IN ({marks})", (market, since, *chunk)):
            out.setdefault(code, {})[day] = close
    return clean_series(out)


def tw_market_data(perf, conn, cands: Sequence, calendar: tuple, today: str, market_db: Path):
    """台股的 MarketData：價格庫 ← DB tw_all ← DB tw（DB 優先，和 perf 自己的順序一樣），再交給 perf.load_tw。"""
    import stock_db_ro

    codes = sorted({c.code for c in cands} | {perf.TW_BENCHMARK})
    since = min(c.t for c in cands)
    store = store_prices(conn, "tw", codes, since)
    long = clean_series(stock_db_ro.closes("tw", codes, since, path=market_db))
    recent = clean_series(stock_db_ro.closes("tw_all", codes, since, path=market_db))
    prices = {code: {**store.get(code, {}), **recent.get(code, {}), **long.get(code, {})} for code in codes}
    return perf.load_tw(cands, calendar, prices, today, market_db)


def us_market_data(perf, conn, cands: Sequence, calendar: tuple, market_db: Path):
    """美股的 MarketData：價格庫 ← DB us（DB 優先），交給 perf.load_us（它自己清收盤、查分割與隔離）。"""
    import stock_db_ro

    codes = sorted({c.code for c in cands} | {perf.US_BENCHMARK})
    since = min(c.t for c in cands)
    store = store_prices(conn, "us", codes, since)
    db = stock_db_ro.closes("us", codes, since, path=market_db)
    prices = {code: {**store.get(code, {}), **db.get(code, {})} for code in codes}
    return perf.load_us(cands, calendar, market_db, prices=prices)


# ── returns 階段 ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Work:
    """一個還沒算齊的池子版本。"""

    family: str
    m: str
    rule: str
    t: str
    ver_s: int
    derive_version: int
    computed_at: float
    pending_h: tuple  # 還有成員沒凍結的 h（小到大）

    @property
    def key(self) -> tuple:
        return (self.m, self.rule, self.t, self.ver_s, self.derive_version)


def incomplete_versions(conn, family: str, where: str, horizons: Sequence[int], since: str) -> list[Work]:
    """這一族 t ≥ since、還有 (成員, h) 沒有目前版本（同一個 rule、SUPPORTED_LEDGER_VERSION、CALC_VERSION）的
    member_returns 的版本。成員＝status 不是 frozen 的列；只數目前成員的列（spool 取代簡版後成員變了，舊成員的列不算數）。"""
    hs = sorted(int(h) for h in horizons)
    per_h = ", ".join(f"sum(CASE WHEN x.h = {h} THEN 1 ELSE 0 END)" for h in hs)
    sql = (
        f"SELECT r.m, r.rule, r.t, r.ver_s, r.derive_version, d.computed_at, count(DISTINCT r.code), {per_h}"
        f" FROM (SELECT m, rule, t, ver_s, derive_version, code FROM pool_rows"
        f"       WHERE ({where}) AND {_CURRENT_MEMBER} AND t >= ?) r"
        " JOIN pool_days d ON d.m = r.m AND d.rule = r.rule AND d.t = r.t AND d.ver_s = r.ver_s"
        " AND d.derive_version = r.derive_version"
        " LEFT JOIN member_returns x ON x.m = r.m AND x.family = ? AND x.rule = r.rule AND x.t = r.t"
        " AND x.ver_s = r.ver_s AND x.derive_version = r.derive_version AND x.code = r.code"
        " AND x.ledger_version = ? AND x.calc_version = ?"
        " GROUP BY r.m, r.rule, r.t, r.ver_s, r.derive_version"
    )
    out = []
    for m, rule, t, ver_s, dv, computed_at, members, *done in conn.execute(
            sql, (since, family, SUPPORTED_LEDGER_VERSION, CALC_VERSION)):
        pending = tuple(h for h, n in zip(hs, done) if (n or 0) < members)
        if pending:
            out.append(Work(family, m, rule, t, int(ver_s), int(dv), float(computed_at), pending))
    return out


def pending_members(conn, work: Work, horizons: Sequence[int]) -> list[tuple[str, Optional[float], int]]:
    """這一版還沒有目前版本的 member_returns 的 (代號, c0, h)：c0 不是正的有限數就當 None（沒有 t 收盤）。"""
    done: dict[str, set] = {}
    for code, h in conn.execute(
            "SELECT code, h FROM member_returns WHERE m = ? AND family = ? AND rule = ? AND t = ? AND ver_s = ?"
            " AND derive_version = ? AND ledger_version = ? AND calc_version = ?",
            (work.m, work.family, work.rule, work.t, work.ver_s, work.derive_version, SUPPORTED_LEDGER_VERSION,
             CALC_VERSION)):
        done.setdefault(code, set()).add(h)
    out = []
    for code, c0 in conn.execute(
            "SELECT code, c0 FROM pool_rows WHERE m = ? AND rule = ? AND t = ? AND ver_s = ? AND derive_version = ?"
            f" AND {_CURRENT_MEMBER} ORDER BY code", work.key):
        close = _finite(c0)
        close = close if close is not None and close > 0 else None
        out.extend((code, close, h) for h in horizons if h not in done.get(code, ()))
    return out


def _row(family: str, derive_version: int, cand, r: Optional[float], b: Optional[float], skip: Optional[str],
         inputs_json: Optional[str], now: float) -> tuple:
    """member_returns 的一列（INSERT_SQL 的欄位順序）。cand.rev 是池子版本的 ver_s（見 returns_stage）。"""
    return (cand.market, family, cand.rule, cand.t, cand.rev, derive_version, cand.code, cand.h,
            SUPPORTED_LEDGER_VERSION, CALC_VERSION, r, b, skip, inputs_json, float(now))


def member_row(perf, family: str, derive_version: int, cand, value: dict, now: float) -> tuple:
    """evaluate 凍結的一列 → member_returns 的一列。沒有 c0 的成員一律記 missing（r、b 都不記）。看不懂丟 ValueError。"""
    skip = value.get("skip")
    if value.get("v") != SUPPORTED_LEDGER_VERSION or (skip is not None and skip not in perf.SKIP_REASONS):
        raise ValueError("unexpected perf row")
    r, b = _finite(value.get("r")), _finite(value.get("b"))
    if cand.close is None:
        skip = MISSING
    if skip is not None:
        r = b = None
    elif r is None or b is None:
        raise ValueError("unexpected perf row")
    inputs = {key: value.get(key) for key in ("th", *perf.INPUT_KEYS)}
    return _row(family, derive_version, cand, r, b, skip,
                json.dumps(inputs, sort_keys=True, separators=(",", ":"), allow_nan=False), now)


def missing_row(family: str, derive_version: int, cand, now: float) -> tuple:
    """沒有 c0、evaluate 已經不回 not_due 的成員：結果一定是 missing，不等 settle 就先記（算式輸入不記）。"""
    return _row(family, derive_version, cand, None, None, MISSING, None, now)


def _sessions_to(calendar: Sequence[str], t: str, h: int) -> int:
    """T_h（E 之後第 h 個交易日）離日曆最後一天還有幾個交易日（≤ 0＝已經在日曆上）。兩個用途：
    - 排先後（快到期的先算；兩個市場的日曆長度不同也比得了）；
    - 預先略過：> 0 就是 T_h 的位置 ≥ len(calendar)，perf.evaluate 的第一個檢查（T_h 的位置＋緩衝 ≥ len(calendar)）
      一定回 not_due，所以略過不會改變任何結果（有邊界測試）。
    其他到不到期、凍不凍結一律由 perf.evaluate 判斷。"""
    return bisect.bisect_right(calendar, t) + h - (len(calendar) - 1)


def _new_counts() -> dict:
    return {"evaluated": 0, "frozen": 0, "waiting": 0, "unexpected": 0, "versions": 0, "due_versions": 0, "late": 0,
            "wait_e": 0, "not_due_versions": 0, "not_due_members": 0, "capped": False, "out_of_time": False,
            "markets": {}, "waits": Counter(),
            "families": {family: {"evaluated": 0, "frozen": 0, "waiting": 0} for family in FAMILY_MARKET}}


def returns_stage(ctx) -> dict:
    """成員報酬（PLAN §6）：見模組說明。dry-run：照算、不寫。perf 讀不到（import 失敗）照樣丟出去＝這個階段失敗。"""
    perf = importlib.import_module("stock_picks_perf")
    version = getattr(perf, "LEDGER_VERSION", None)
    if version != SUPPORTED_LEDGER_VERSION:
        valid = isinstance(version, int) and not isinstance(version, bool)
        return {"status": "skipped:ledger_version", "ledger_version": version if valid else None}
    conn = ctx.db()
    if conn is None:
        return {"status": "skipped:no_db"}
    from jobs import stock_picks_review_db as rdb

    if not rdb.has_tables(conn, rdb.C1_TABLES):
        if ctx.dry_run:  # 唯讀開的舊庫（還沒升到 C1 的 schema）
            return {"status": "skipped:no_table"}
        raise rdb.SchemaError("missing tables")
    started = time.monotonic()
    horizons = tuple(int(h) for h in perf.HORIZONS)
    since = (ctx.today - timedelta(days=rdb.PRICE_KEEP_DAYS)).isoformat()
    counts = _new_counts()
    failures: list[tuple[str, Optional[str]]] = []  # (例外類別, 程式位置)：第一個就是階段的 error

    def market_failed(market: str, exc: Exception) -> None:
        """忙碌不算失敗；其他讀不到的只讓這個市場這一輪不算（另一個市場照算、照寫），階段最後記 error。"""
        if rdb.is_busy(exc):
            counts["markets"][market] = "busy"
            return
        counts["markets"][market] = "error:" + type(exc).__name__
        failures.append((type(exc).__name__, _where(exc)))
        log.error("stock picks review returns market=%s status=error error_type=%s where=%s", market,
                  type(exc).__name__, failures[-1][1])

    def flush(rows: list) -> None:
        """凍結的列寫進去（一個交易）再清掉；dry-run 只清掉（只回數字）。"""
        if rows and not ctx.dry_run:
            with rdb.transaction(conn):
                conn.executemany(INSERT_SQL, rows)
        rows.clear()

    work = [w for family, where in pool.FAMILY_WHERE for w in incomplete_versions(conn, family, where, horizons, since)]
    counts["versions"] = len(work)

    calendars: dict[str, tuple] = {}
    for market in sorted({FAMILY_MARKET[w.family] for w in work}):
        try:
            calendars[market] = market_calendar(conn, market, since, ctx.paths.market_db)
        except Exception as exc:  # noqa: BLE001 - 一個市場讀不到不擋另一個市場
            market_failed(market, exc)

    due: list[tuple[int, Work]] = []
    for w in work:
        calendar = calendars.get(FAMILY_MARKET[w.family])
        if calendar is None:
            continue
        deadline = perf.close_deadline(w.m, w.t, calendar)
        if deadline is None:
            counts["wait_e"] += 1  # E 還不在日曆上
        elif w.computed_at >= deadline:
            counts["late"] += 1  # E 收盤以後才算出來：永遠不算
        else:
            ahead = _sessions_to(calendar, w.t, w.pending_h[0])
            if ahead > 0:  # 最快到期的那個 h 的 T_h 都還不在日曆上：evaluate 一定回 not_due，不查成員
                counts["not_due_versions"] += 1
            else:
                due.append((ahead, w))
    due.sort(key=lambda item: (item[0], item[1].t, item[1].computed_at, item[1].family, item[1].derive_version))

    pending: list[tuple[int, str, int, Any]] = []  # (離 T_h 還有幾個交易日, 族, derive_version, Candidate)
    for _, w in due:
        # 第一個版本一定載入（每輪至少前進一點）；之後到了上限或預算就停，剩下的下一輪再來
        if counts["due_versions"]:
            if len(pending) >= MAX_CANDIDATES:
                counts["capped"] = True
                break
            if time.monotonic() - started > BUDGET_S:
                counts["out_of_time"] = True
                break
        counts["due_versions"] += 1
        calendar = calendars[FAMILY_MARKET[w.family]]
        for code, c0, h in pending_members(conn, w, horizons):
            ahead = _sessions_to(calendar, w.t, h)
            if ahead > 0:  # T_h 還不在日曆上：一定是 not_due，不載入
                counts["not_due_members"] += 1
                continue
            # Candidate 的 rev 位置放的是池子版本的 ver_s（round(computed_at)），不是歷史檔的版次（1、2、3…）
            pending.append((ahead, w.family, w.derive_version, perf.Candidate(w.m, w.rule, w.t, w.ver_s, code, c0, h)))

    data: dict[str, Any] = {}
    by_market: dict[str, list] = {}
    for _, _, _, cand in pending:
        by_market.setdefault(cand.market, []).append(cand)
    for market, cands in sorted(by_market.items()):
        try:
            if market == "tw":
                data[market] = tw_market_data(perf, conn, cands, calendars["tw"], ctx.today.isoformat(),
                                              ctx.paths.market_db)
            else:
                data[market] = us_market_data(perf, conn, cands, calendars["us"], ctx.paths.market_db)
        except Exception as exc:  # noqa: BLE001 - 一個市場讀不到不擋另一個市場
            market_failed(market, exc)
            continue
        counts["markets"][market] = "ok"

    rows: list[tuple] = []
    pending.sort(key=lambda item: (item[0], item[3].t, item[3].code, item[1]))
    for index, (_, family, derive_version, cand) in enumerate(pending):
        md = data.get(cand.market)
        if md is None:
            continue
        if index and index % CHECK_EVERY == 0 and time.monotonic() - started > BUDGET_S:  # 至少評完前 CHECK_EVERY 筆
            counts["out_of_time"] = True
            break
        kind, value = perf.evaluate(cand, md)
        mine = counts["families"][family]
        counts["evaluated"] += 1
        mine["evaluated"] += 1
        if kind == perf.FREEZE:
            try:
                rows.append(member_row(perf, family, derive_version, cand, value, ctx.now))
            except ValueError:  # 看不懂的一列：不寫、記下來，其他列照寫（階段最後記 error）
                counts["unexpected"] += 1
                continue
        elif cand.close is None and value != NOT_DUE:  # 沒有 c0：結果一定是 missing，不等 settle（審查 C1 r1#3）
            rows.append(missing_row(family, derive_version, cand, ctx.now))
        else:
            counts["waiting"] += 1
            mine["waiting"] += 1
            counts["waits"][str(value)] += 1
            continue
        counts["frozen"] += 1
        mine["frozen"] += 1
        if len(rows) >= INSERT_CHUNK:
            flush(rows)
    flush(rows)

    counts["waits"] = dict(sorted(counts["waits"].items()))
    if counts["unexpected"]:
        log.error("stock picks review returns unexpected perf rows=%d", counts["unexpected"])
        failures.append(("UnexpectedPerfRow", None))
    if ctx.dry_run:
        counts["status"] = "dry_run"
    elif failures:
        counts["status"], counts["error"] = "error", failures[0][0]
        if failures[0][1]:
            counts["where"] = failures[0][1]
    else:
        busy = any(state == "busy" for state in counts["markets"].values())
        counts["status"] = "busy" if busy else "ok"
    return counts


# ── p 與選版（純函式） ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PoolVersion:
    """一個池子版本（同一個 (m, rule, t, ver_s) 的所有 derive_version）。"""

    family: str
    m: str
    rule: str
    t: str
    ver_s: int
    computed_at: float
    picks: frozenset  # 入選的代號（最大的 derive_version）


def select_version(versions: Sequence[PoolVersion], deadline: Optional[float],
                   picks: Optional[frozenset]) -> tuple[Optional[PoolVersion], Optional[str]]:
    """(用哪一版, flag)。E 收盤前算出來的版本裡：picks 和帳釘住那一行相同的最後一版；沒有就 E 收盤前最後一版
    （version_fallback）；一版都沒有 → version_missing；日曆上還沒有 E → pending。picks＝None（舊五條件的背景日）：
    不比對，直接取 E 收盤前最後一版。"""
    if deadline is None:
        return None, "pending"
    before = [v for v in versions if v.computed_at < deadline]
    if not before:
        return None, "version_missing"
    latest = max(before, key=lambda v: (v.computed_at, v.rule))
    if picks is None:
        return latest, None
    same = [v for v in before if v.picks == picks]
    if same:
        return max(same, key=lambda v: (v.computed_at, v.rule)), None
    return latest, "version_fallback"


def pool_p(members: dict[int, frozenset], returns: dict[tuple, tuple], h: int) -> tuple[str, Optional[float], Optional[int]]:
    """股票池等權平均（PLAN §3）。members＝{derive_version: 成員代號}；returns＝{(derive_version, h, 代號): (r, b, skip)}。
    取每個成員都有列的最大 derive_version：有值的成員 ≥ 80% → ("ok", 平均 r, dv)；不到 → ("pool_missing", None, dv)。
    沒有一個 derive_version 齊全 → ("pending", None, None)。"""
    for dv in sorted(members, reverse=True):
        codes = members[dv]
        if not codes or any((dv, h, code) not in returns for code in codes):
            continue
        valued = [returns[(dv, h, code)][0] for code in sorted(codes)
                  if returns[(dv, h, code)][2] is None and _finite(returns[(dv, h, code)][0]) is not None]
        if len(valued) * P_DEN >= len(codes) * P_NUM:
            return "ok", math.fsum(valued) / len(valued), dv
        return "pool_missing", None, dv
    return "pending", None, None


def pick_return(members: dict[int, frozenset], returns: dict[tuple, tuple], h: int, code: str,
                prefer: Optional[int]) -> Optional[tuple]:
    """某個代號、某個 h 的 member_returns (r, b, skip)：先看算 p 用的 derive_version，沒有再從大到小找。"""
    for dv in ([prefer] if prefer is not None else []) + sorted(members, reverse=True):
        found = returns.get((dv, h, code))
        if found is not None:
            return found
    return None


# ── 週報的資料 ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Entry:
    """週報裡的一筆（一檔推薦 × 一個 h）。"""

    family: str  # 三族之一，或 SEEN_V1（家人看到的舊五條件）
    m: str
    t: str
    code: str
    h: int
    r: Optional[float]
    b: Optional[float]
    skip: Optional[str]
    p_status: str  # ok｜pool_missing｜pending｜version_missing｜not_compared（SEEN_V1：不和股票池比）
    p: Optional[float]
    live: bool  # 家人看到的（perf 帳）｜舊五條件的背景日（member_returns）


def family_versions(conn, family: str, where: str) -> dict[str, list[PoolVersion]]:
    """{t: [這一族那天的池子版本]}；picks 取最大的 derive_version 的入選列。"""
    computed: dict[tuple, float] = {}
    top: dict[tuple, int] = {}
    for m, rule, t, ver_s, dv, computed_at in conn.execute(
            f"SELECT m, rule, t, ver_s, derive_version, computed_at FROM pool_days WHERE {where}"):
        key = (m, rule, t, int(ver_s))
        computed[key] = float(computed_at)
        top[key] = max(top.get(key, dv), dv)
    picks: dict[tuple, set] = {}
    for m, rule, t, ver_s, dv, code in conn.execute(
            f"SELECT m, rule, t, ver_s, derive_version, code FROM pool_rows WHERE ({where}) AND picked = 1"):
        picks.setdefault((m, rule, t, int(ver_s), dv), set()).add(code)
    out: dict[str, list[PoolVersion]] = {}
    for key, at in computed.items():
        out.setdefault(key[2], []).append(PoolVersion(family, *key, at, frozenset(picks.get((*key, top[key]), ()))))
    return out


def version_returns(conn, family: str, version: PoolVersion, with_returns: bool) -> tuple[dict, dict]:
    """(成員 {dv: 代號}, member_returns {(dv, h, 代號): (r, b, skip)})：只看目前版本（同一個 rule、
    SUPPORTED_LEDGER_VERSION、CALC_VERSION）的列；with_returns＝False（還沒有 C1 的表）時第二個是空的。"""
    members: dict[int, set] = {}
    for dv, code in conn.execute(
            "SELECT derive_version, code FROM pool_rows WHERE m = ? AND rule = ? AND t = ? AND ver_s = ?"
            f" AND {_CURRENT_MEMBER}", (version.m, version.rule, version.t, version.ver_s)):
        members.setdefault(dv, set()).add(code)
    returns: dict[tuple, tuple] = {}
    if with_returns:
        for dv, code, h, r, b, skip in conn.execute(
                "SELECT derive_version, code, h, r, b, skip FROM member_returns WHERE m = ? AND family = ? AND rule = ?"
                " AND t = ? AND ver_s = ? AND ledger_version = ? AND calc_version = ?",
                (version.m, family, version.rule, version.t, version.ver_s, SUPPORTED_LEDGER_VERSION, CALC_VERSION)):
            returns[(dv, h, code)] = (r, b, skip)
    return {dv: frozenset(codes) for dv, codes in members.items()}, returns


# TODO（審查 C1 r2#6 後半，延後到 B2）：B2 動 perf 時給 read_ledger_rows 加 path 參數，這裡就不用 samefile 閘門。
def _same_dir(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def load_ledger(perf, sp, state_dir: Path) -> tuple[list[dict], str]:
    """perf 帳讀得懂的列（read_ledger_rows）與狀態：ok｜elsewhere（stock_picks.STATE_DIR 不是 job 的 state/：測試、
    staging，不讀別人的帳）｜error:<例外類別>（帳讀不了，不當成空的帳）。"""
    if not _same_dir(Path(sp.STATE_DIR), state_dir):
        return [], "elsewhere"
    try:
        return perf.read_ledger_rows(), "ok"
    except Exception as exc:  # noqa: BLE001 - 帳讀不了：週報寫一行說明
        return [], "error:" + type(exc).__name__


def _jsonl_objects(path: Path, max_bytes: int) -> list:
    """紀錄檔每一行解析後的物件（壞行是 None）；讀不了是空的（aggregate 也是：找不到那一行就不過濾代號）。"""
    try:
        data = pool.read_private_bytes(path, max_bytes)
    except (OSError, ValueError):
        return []
    out: list = []
    for raw in (data or b"").split(b"\n"):
        if not raw.strip():
            continue
        try:
            out.append(json.loads(raw))
        except (ValueError, RecursionError):
            out.append(None)
    return out


def history_rows(perf, sp, state_dir: Path, today: str) -> dict[str, dict]:
    """三份推薦紀錄 → {"tw": {t: [Record…]}, "us": {t: [Record…]}}（perf.parse_rows，參數和 perf 自己讀的時候一樣）。"""
    limit = perf.HISTORY_MAX_BYTES
    old = perf.parse_rows(_jsonl_objects(state_dir / sp.HISTORY_NAME, limit), market="tw",
                          fixed_rule=perf.TW_OLD_RULE, default_rule=perf.TW_OLD_RULE, today=today)
    ai = perf.parse_rows(_jsonl_objects(state_dir / sp.AI_HISTORY_NAME, limit), market="tw",
                         fixed_rule=None, default_rule=sp.AI_RULE, today=today)
    us = perf.parse_rows(_jsonl_objects(state_dir / perf.US_HISTORY_NAME, limit), market="us",
                         fixed_rule=None, default_rule=perf.US_RULE, today=today)
    tw = {t: old.get(t, []) + ai.get(t, []) for t in set(old) | set(ai)}
    return {"tw": tw, "us": us}


# TODO（審查 C1 r4#1 後半，延後到 B2）：B2 下次改 stock_picks_perf 時，在那裡抽出公開的 chosen_rows(rows, lines)，
#   perf.aggregate 和這裡都用它；在那之前靠對帳測試釘住兩邊挑到同樣的列。
def chosen_ledger_rows(perf, rows: Sequence[dict], history: dict) -> tuple[list[dict], dict, dict]:
    """(算進去的帳列, 釘住的 {(m, t): (rule, rev)}, 那一行 {(m, t): Record 或 None})：和 perf.aggregate 挑列的規則一樣
    （目前版本、HORIZONS、釘住的 (rule, rev)、紀錄檔找得到那一行就只算代號還在裡面的）。skip 的列也在：次數只算
    skip 是 None 的，自我檢查兩種都比。目前版本＝SUPPORTED_LEDGER_VERSION（report_lines 只在它等於
    perf.LEDGER_VERSION 時才呼叫，和 aggregate 比的是同一個數）。"""
    pins = perf.pins_of(rows)
    lines = {key: perf.pinned_line(history.get(key[0], {}).get(key[1], []), pin) for key, pin in pins.items()}
    chosen = []
    for row in rows:
        if row["v"] != SUPPORTED_LEDGER_VERSION or row["h"] not in perf.HORIZONS:
            continue
        pin = pins[(row["m"], row["t"])]
        if (row["rule"], row["rev"]) != pin:
            continue
        rec = lines.get((row["m"], row["t"]))
        if rec is not None and (rec.rule, rec.rev) == pin and all(p.code != row["code"] for p in rec.picks):
            continue
        chosen.append(row)
    return chosen, pins, lines


@dataclass
class Performance:
    entries: dict  # {族: [Entry…]}：三族＋SEEN_V1
    flags: Counter  # version_fallback／version_missing 的推薦日數（(族, m, t) 為單位，各族分開數）
    check: Counter  # compared、diff、oneside
    missing_ts: frozenset = frozenset()  # version_missing 的那些 t
    first_pool: Optional[str] = None  # 池子紀錄最早的 t（三族合起來）


def build_performance(perf, conn, versions: dict[str, dict], ledger: Sequence[dict], history: dict,
                      calendars: dict[str, Sequence[str]], *, with_returns: bool) -> Performance:
    """週報的推薦成績資料（見模組說明）。versions＝{族: family_versions(…)}；calendars＝{市場: 交易日}。"""
    chosen, pins, lines = chosen_ledger_rows(perf, ledger, history)
    cache: dict[tuple, tuple] = {}
    selected: dict[tuple, tuple] = {}

    def returns_of(version: PoolVersion) -> tuple[dict, dict]:
        key = (version.family, version.m, version.rule, version.t, version.ver_s)
        if key not in cache:
            cache[key] = version_returns(conn, version.family, version, with_returns)
        return cache[key]

    def select(family: str, m: str, t: str, picks: Optional[frozenset]) -> tuple:
        """(用哪一版, flag)：同一個 (族, m, t) 只選一次。"""
        key = (family, m, t)
        if key not in selected:
            deadline = perf.close_deadline(m, t, tuple(calendars.get(m, ())))
            selected[key] = select_version(versions.get(family, {}).get(t, ()), deadline, picks)
        return selected[key]

    def version_p(family: str, m: str, t: str, picks: Optional[frozenset], h: int,
                  code: str) -> tuple[Optional[str], str, Optional[float], Optional[tuple]]:
        """選版 → version_returns → pool_p → pick_return：(flag, p_status, p, 這一檔的 member_returns (r, b, skip)
        或 None)。家人看到的推薦（picks＝帳釘住那一行）和舊五條件的背景日（picks＝None）走同一條路（C2 加冷凍時只改
        這一條）。"""
        version, flag = select(family, m, t, picks)
        if version is None:
            return flag, ("pending" if flag == "pending" else "version_missing"), None, None
        members, returns = returns_of(version)
        p_status, p, dv = pool_p(members, returns, h)
        return flag, p_status, p, pick_return(members, returns, h, code, dv)

    entries: dict[str, list] = {family: [] for family in (*FAMILY_MARKET, SEEN_V1)}
    flags: Counter = Counter()
    check: Counter = Counter()
    missing_ts: set[str] = set()
    pin_codes: dict[tuple, set] = {}
    for row in chosen:
        pin_codes.setdefault((row["m"], row["t"]), set()).add(row["code"])
    for row in chosen:
        family = family_of(row["m"], row["rule"])
        if family is None:
            continue
        if family == pool.FAMILY_V1:  # 家人看到的舊五條件：另成一段（不選版、沒有 p、不進自我檢查）
            entries[SEEN_V1].append(Entry(SEEN_V1, row["m"], row["t"], row["code"], row["h"], row["r"], row["b"],
                                          row["skip"], NOT_COMPARED, None, True))
            continue
        first = (family, row["m"], row["t"]) not in selected
        rec = lines.get((row["m"], row["t"]))
        picks = frozenset(p.code for p in rec.picks) if rec is not None else frozenset(pin_codes[(row["m"], row["t"])])
        flag, p_status, p, mine = version_p(family, row["m"], row["t"], picks, row["h"], row["code"])
        if first and flag in ("version_fallback", "version_missing"):  # 一個 (族, m, t) 數一次
            flags[flag] += 1
            if flag == "version_missing":
                missing_ts.add(row["t"])
        if mine is not None:
            check["compared"] += 1
            if row["skip"] is None and mine[2] is None:
                check["diff"] += 1 if abs(row["r"] - mine[0]) > SELF_CHECK_TOLERANCE else 0
            elif (row["skip"] is None) != (mine[2] is None):
                check["oneside"] += 1
        entries[family].append(Entry(family, row["m"], row["t"], row["code"], row["h"], row["r"], row["b"], row["skip"],
                                     p_status, p, True))

    # 對照組（舊五條件）只用背景日：家人那天沒看到舊五條件（帳裡那個 t 沒有釘住 v1）。帳讀不到（pins 是空的，週報另有
    # 一行寫帳讀不到），或成員報酬比 perf 帳早凍結的那幾十分鐘（帳還沒釘住那一天）時，家人看過的 v1 日也會暫時算進來；
    # AI 開著的期間家人看不到舊五條件，所以只有 AI 關掉時才會發生（審查 C1 r1#6d）。
    live_v1 = {t for (m, t), pin in pins.items() if m == "tw" and pin[0] == perf.TW_OLD_RULE}
    for t in sorted(versions.get(pool.FAMILY_V1, {})):
        if t in live_v1:
            continue
        version, _ = select(pool.FAMILY_V1, "tw", t, None)
        if version is None:
            continue
        for code in sorted(version.picks):
            for h in perf.HORIZONS:
                _, p_status, p, mine = version_p(pool.FAMILY_V1, "tw", t, None, h, code)
                if mine is not None:  # 還沒凍結的不列
                    entries[pool.FAMILY_V1].append(Entry(pool.FAMILY_V1, "tw", t, code, h, mine[0], mine[1], mine[2],
                                                         p_status, p, False))
    first_pool = min((t for by_t in versions.values() for t in by_t), default=None)
    return Performance(entries, flags, check, frozenset(missing_ts), first_pool)


@dataclass(frozen=True)
class Stats:
    n: int  # 算進去的次數（skip 是 None）
    r: Optional[float]  # 這 n 次的平均報酬
    b: Optional[float]  # 這 n 次的基準（0050／SPY）同期平均
    k: int  # 可和股票池比的次數（p_status 是 ok、p 是有限數）
    r_paired: Optional[float]  # 這 k 次推薦的平均報酬
    p: Optional[float]  # 這 k 次股票池的平均
    wins: int  # 這 k 次裡贏股票池的次數（r > p）
    p_pending: int
    p_missing: int


def stats_of(entries: Iterable[Entry], h: int) -> Stats:
    """平均報酬、基準算全部 n 次；和股票池比只看有 p 的 k 次（同一批推薦，審查 C1 r1#1、r3#1）。"""
    counted = [e for e in entries if e.h == h and e.skip is None and _finite(e.r) is not None]
    benches = [e.b for e in counted if _finite(e.b) is not None]
    paired = [e for e in counted if e.p_status == "ok" and _finite(e.p) is not None]
    return Stats(
        n=len(counted),
        r=math.fsum(e.r for e in counted) / len(counted) if counted else None,
        b=math.fsum(benches) / len(benches) if benches else None,
        k=len(paired),
        r_paired=math.fsum(e.r for e in paired) / len(paired) if paired else None,
        p=math.fsum(e.p for e in paired) / len(paired) if paired else None,
        wins=sum(1 for e in paired if e.r > e.p),
        p_pending=sum(1 for e in counted if e.p_status == "pending"),
        p_missing=sum(1 for e in counted if e.p_status == "pool_missing"),
    )


# ── 週報的文字 ───────────────────────────────────────────────────────────────
HEAD = "【推薦成績】"  # 算法（隔天收盤進場、和股票池等權平均比）寫在週報最後一行（weekly.METHOD），這裡不重複
CONTROL_TAIL = "：背景照算的入選股"  # 對照組那一段的標題（接在族名後面）
_LEDGER_WORDS = {"elsewhere": "成績帳不在這個 bot 目錄（測試或 staging）"}


def _permille(ratio: float) -> int:
    """比例 → 千分位整數，正負對稱四捨五入（和 stock_picks._permille 同一種：週報和家人成績卡的數字一致）。"""
    magnitude = int(math.floor(abs(ratio * 1000) + 0.5 + 1e-9))
    return -magnitude if ratio < 0 else magnitude


def pct(ratio: Optional[float]) -> str:
    if ratio is None:
        return "—"
    pm = _permille(ratio)
    sign = "+" if pm > 0 else "-" if pm < 0 else ""
    return f"{sign}{abs(pm) // 10}.{abs(pm) % 10}%"


def _md(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.month}/{d.day}"


def _h_line(h: int, stats: Stats, bench: str, *, with_pool: bool = True) -> str:
    """一個 h 的一行：平均、基準算全部次數；with_pool 時後半只用可和股票池比的那幾次。"""
    if stats.n < MIN_N:
        return f"・{h} 日：紀錄還不夠（目前 {stats.n} 次）"
    line = f"・{h} 日 {stats.n} 次：平均 {pct(stats.r)}，{bench} {pct(stats.b)}"
    if not with_pool:
        return line
    if stats.k < MIN_N:
        return line + f"；股票池可比的紀錄還不夠（目前 {stats.k} 次）"
    return (line + f"；可和股票池比的 {stats.k} 次：推薦 {pct(stats.r_paired)}、股票池 {pct(stats.p)}"
            f"（贏 {stats.wins}/{stats.k}）")


def format_lines(report: Performance, ledger_state: str, horizons: Sequence[int]) -> list[str]:
    lines = [HEAD]
    pending = missing = 0
    for family in pool.FAMILIES:  # 台股 AI、美股、對照組（和【資料收集】同一張表、同一個順序）
        entries = report.entries.get(family.key, [])
        stats = {h: stats_of(entries, h) for h in horizons}
        pending += sum(s.p_pending for s in stats.values())
        missing += sum(s.p_missing for s in stats.values())
        if not any(s.n for s in stats.values()):
            lines.append(f"・{family.name}：還沒有成績")
            continue
        lines.append(family.name + (CONTROL_TAIL if family.key == pool.FAMILY_V1 else ""))
        lines += [_h_line(h, stats[h], family.bench) for h in horizons]
    seen = report.entries.get(SEEN_V1, [])
    if seen:  # 放最後；如果之後又關掉 AI，這段的日期範圍會跨過中間 AI 開著的那段
        bench = next(family.bench for family in pool.FAMILIES if family.key == pool.FAMILY_V1)
        days = [e.t for e in seen]
        lines.append(f"家人看到的舊五條件（{_md(min(days))}–{_md(max(days))} 推薦，只和 {bench} 比）")
        lines += [_h_line(h, stats_of(seen, h), bench, with_pool=False) for h in horizons]
    notes = []
    if report.flags["version_fallback"]:
        notes.append(f"{report.flags['version_fallback']} 個推薦日（各族分開數）的推薦和池子紀錄的入選對不上"
                     "（改用推薦隔天收盤前最後一版）")
    if report.flags["version_missing"]:
        why = "股票池不比"
        if report.first_pool and report.missing_ts and max(report.missing_ts) < report.first_pool:
            why += f"；池子紀錄 {_md(report.first_pool)} 才開始記"
        notes.append(f"{report.flags['version_missing']} 個推薦日（各族分開數）找不到推薦隔天收盤前的池子紀錄（{why}）")
    if pending:
        notes.append(f"{pending} 筆的股票池成員還沒算完")
    if missing:
        notes.append(f"{missing} 筆的股票池有值的成員不到 8 成（不比）")
    lines += [f"・{text}" for text in notes]
    if ledger_state != "ok":
        reason = _LEDGER_WORDS.get(ledger_state) or f"成績帳讀不到（{ledger_state.split(':', 1)[-1]}）"
        lines.append(f"・{reason}：家人看到的推薦這週沒有算進來")
    check = report.check
    if check["compared"]:
        line = f"・自我檢查：成績帳和股票池算法比對 {check['compared']} 筆，差超過 0.1% 的 {check['diff']} 筆"
        lines.append(line + (f"、一邊有值一邊跳過 {check['oneside']} 筆" if check["oneside"] else ""))
    else:
        lines.append("・自我檢查：還沒有可以比對的推薦（成績帳和股票池都算出來才比）")
    return lines


def report_lines(ctx, conn, calendars: dict[str, Sequence[str]]) -> list[str]:
    """週報的【推薦成績】一段（週報階段在自己的 try 裡呼叫）。calendars＝{市場: 價格庫的交易日}（選版用的 E）。"""
    perf = importlib.import_module("stock_picks_perf")
    if getattr(perf, "LEDGER_VERSION", None) != SUPPORTED_LEDGER_VERSION:
        return [HEAD, "・推薦成績帳的版本不是 v2，這週先不比"]
    sp = importlib.import_module("stock_picks")
    from jobs import stock_picks_review_db as rdb

    # 選版的 E 用價格庫的日曆（weekly.trading_days），returns 階段用 DB ∪ 價格庫（market_calendar）。
    # TODO（審查 C1 r3#5，延後）：共用一個日曆 helper（DB 保留範圍內只用 DB 的日子，價格庫只補 DB 最早一天之前的），
    #   週報和 returns 都用它。週報在週日晚上跑，兩個市場都休市，價格庫和 DB 的日曆是同步的，所以先不做。
    versions = {family: family_versions(conn, family, where) for family, where in pool.FAMILY_WHERE}
    ledger, ledger_state = load_ledger(perf, sp, ctx.paths.state_dir)
    history = history_rows(perf, sp, ctx.paths.state_dir, ctx.today.isoformat()) if ledger else {}
    report = build_performance(perf, conn, versions, ledger, history, calendars,
                               with_returns=rdb.has_tables(conn, rdb.C1_TABLES))
    return format_lines(report, ledger_state, tuple(perf.HORIZONS))
