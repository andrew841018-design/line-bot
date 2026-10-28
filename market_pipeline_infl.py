"""市場盤勢（/盤勢）通膨的 collect 任務（第 9 輪批 2 C 軌，2026-10-24）：5 年／10 年損益兩平通膨、BLS 的 CPI 與核心 CPI。

Andrew 2026-10-23 第 4 項「通膨指標＋目前通膨狀況」。PLAN_r9_andrew9_v2 A6、A22：通膨的 collect 程式放在自己的模組
（market_sources_infl／這個檔），由 jobs/market_snapshot.py 的 PIPELINES 載入；market_tasks.load_modules 讓「一個 pipeline 模組
import 壞掉只停它自己的任務」，美股其他任務不受影響。排程用的小工具（_run_batch、_batch_due、current_d…）借 market_pipeline_us
的；那邊壞了這邊也會一起不載入（沒有它們任務也跑不了），反過來這裡壞了不影響美股。

損益兩平（us.infl.be）：
- 自己抓財政部的名目與實質兩份 CSV（主機 home.treasury.gov，已在白名單，只換 type 參數），存**獨立的 obs**
  `us.infl.be`：{"rows": [{"date", "n5", "n10", "r5", "r10"}, …]}（具名鍵：之後加欄不會錯位；舊到新、最近 40 列、% 兩位小數；
  某檔還沒公布的格子是 None）。
  `us.ust`／`parse_treasury_csv` 一個位元組都不動，所以實質來源壞了，10 年債列與 GEX 用的 3 個月利率照常。
- 兩個項目（nominal、real）各自一個 batch 項目：一個失敗不擋另一個；每個 D（美東最近收完的交易日）一個期間，沒有 D 那一天
  → "not_yet"（每小時再看，最多 4 次），和 us.ust 同一套；債市休市的日子（10/12、11/11）本來就沒有列，D 沒有列只是 not_yet。
- 同日才算、5 日變化在「名目與實質共同的日期」上數：這些在 market_calc.breakeven_values（publish 時算）；collect 只負責把兩份
  CSV 的格子對到同一個日期的列裡。
- 月初：新月份的檔可能只有表頭（→ 沒有列）或還沒建好（→ 非 200，前 10 號當成沒有列）；這個檔已存的列太少、或最新一列還在上個月
  時，另外把上個月抓回來補齊（冷啟動、換月）。
- 排在 us.ust 之後（同一個財政部主機：429／403 的 2 小時冷卻是整個主機，實質檔出狀況時 us.ust 當輪已經先抓完）。

CPI 與核心 CPI（us.infl.cpi；2026-10-24 Andrew 同意新增 api.bls.gov，v1 免註冊、每天 25 次，帳本每台北日 12 次、間隔 5 秒）：
- 四個序列存在一個 obs `us.infl.cpi`：{"series": {序列代號: {"YYYY-MM": 指數}}, "retrieved": 取得日（台北）}；先問 CUUR0000SA0
  一個請求，月份前進才抓另外三個，四個一起存（半組不寫）；四個序列月份對不上也存，但不算做完（publish 的計算會標 aligned=False、
  不算動能）。比已存的還舊 → conflict，什麼都不寫。
- 期間＝「現在應該已公布的最新一月」（比照 us.cftc 的 _cftc_expected）：已存的比它舊就到期。輪詢規則在 market_calc.cpi_poll（job 的
  outcome 用同一個函式）：每月 8–20 號、平日美東 08:30 後每 2 小時問一次直到最新月份前進（公布時刻是 08:30；每天 ≤6 次）；冷啟動或
  落後超過一個月不受日期與時段限制、立刻補抓；窗口過了還沒更新（延後公布）每個平日問一次。排程只用時間（task_state 的 last_probe），
  不用 _run_batch 的次數上限：延後公布不會把期間鎖死。
- 未來月份（2026-10-28 審查 gp2 C1）：BLS 回的月份晚於「應已公布最新月＋1」（market_calc.cpi_month_ceiling）→ 整個回應拒收
  （ParseError("date", "future")，什麼都不寫，不論探針或另外三個序列）；已存的序列若帶著這種月份（舊程式或壞回應存進去的）→ 排程
  當成冷啟動、下一輪重抓四個序列並把它洗掉（讀已存資料時一律先去掉未來月份，所以 held／落後／倒退的判斷都不看它）。
- 一個開關：api.bls.gov 不在 market_http.ALLOWED_HOSTS 時 due 一律 False、run 不送請求（要停用只要把主機拿掉）。
- 200＋REQUEST_NOT_PROCESSED 或 Results 是空的 → 和 429 一樣冷卻主機 2 小時＋任務退避（FetchError rate_limited）；HTTP 429／403
  由 market_http 處理。沒有 max_fails 可以把一個月鎖死。

這個模組 import 時只把任務登記進框架的記憶體表，不做任何 I/O。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import market_calc
import market_http
import market_pipeline_us as us_pipe
import market_sources_infl as src
from market_tasks import Task, register

MARKET = "us"

# ── obs 鍵與 payload（market_publish 讀；要改兩邊一起改，test_market_publish_infl 比對） ──────────
# 損益兩平：{"rows": [{"date": ISO 日期, "n5", "n10"（名目 5／10 年）, "r5", "r10"（實質）}, …]}（舊到新）；data_date＝最新一列的日期
OBS_INFL_BE = "us.infl.be"

# CPI：{"series": {序列代號: {"YYYY-MM": 指數}}, "retrieved": ISO 日期（台北）}；data_date＝四個序列都有的最新月份的月底、period＝那個月
OBS_INFL_CPI = "us.infl.cpi"
CPI_KEY = "us.infl.cpi"  # 排程紀錄（task_state "sched:us.infl.cpi"）：{"last_probe": 上一次問了沒進展的 epoch 秒}
_TPE = ZoneInfo("Asia/Taipei")
_MONTH_KEY_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])", re.ASCII)

BE_KEY = "us.infl.be"  # 排程紀錄（task_state "sched:us.infl.be"）的鍵；也是任務名
BE_ITEMS = ("nominal", "real")
BE_COLUMNS = {"nominal": ("n5", "n10"), "real": ("r5", "r10")}  # 這個檔的 5 年、10 年在列裡的鍵
BE_ROWS = 40  # 和 us.ust 的 UST_ROWS 一樣：最近 40 列
BE_MIN_ROWS = 10  # 這個檔已存的列少於這麼多（冷啟動）就把上個月也抓回來
MONTH_START_DAYS = 10  # D 在每月前幾號時，新月份的檔可能還沒建（非 200）→ 當成沒有列


# ── 損益兩平 ─────────────────────────────────────────────────────────────────


def _empty_row(day: str) -> dict:
    return {"date": day, "n5": None, "n10": None, "r5": None, "r10": None}


def _be_rows(ctx: Any) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for item in us_pipe._obs_payload(ctx, OBS_INFL_BE).get("rows") or []:
        if isinstance(item, dict) and isinstance(item.get("date"), str):
            rows[item["date"]] = {**_empty_row(item["date"]),
                                  **{key: us_pipe._finite(item.get(key)) for key in ("n5", "n10", "r5", "r10")}}
    return rows


def _kind_dates(rows: dict[str, dict], kind: str) -> list[str]:
    first, second = BE_COLUMNS[kind]
    return sorted(day for day, row in rows.items() if row[first] is not None or row[second] is not None)


def _merge_points(rows: dict[str, dict], kind: str, points: Any, last_day: date) -> None:
    """把一份 CSV 的格子放進同日期的列（晚於 D 的不收）；整列都是空的（天期還沒公布）不建列、也不蓋掉已存的值。"""
    first, second = BE_COLUMNS[kind]
    for point in points or ():
        if not isinstance(point.date, date) or point.date > last_day:
            continue
        values = (us_pipe._finite(point.y5), us_pipe._finite(point.y10))
        if values == (None, None):
            continue
        key = point.date.isoformat()
        row = rows.setdefault(key, _empty_row(key))
        row[first], row[second] = values


def _fetch_month(ctx: Any, kind: str, month: date, last_day: date) -> Any:
    try:
        return src.fetch_treasury_yields(kind, month, http=ctx.http, ledger=ctx.ledger) or ()
    except market_http.FetchError as exc:
        # 月初新檔可能還沒建（非 200；財政部第一個交易日的資料公布前沒有該月的檔，目前沒有樣本）：D 在前 10 號、抓的是 D 那個月
        # 時當成沒有列，上個月的資料另外補。429／403／逾時等其他錯誤照丟。
        if exc.kind == "status" and month == last_day.replace(day=1) and last_day.day <= MONTH_START_DAYS:
            return ()
        raise


def _due_be(ctx: Any) -> bool:
    return us_pipe._batch_due(ctx, BE_KEY, us_pipe._iso(us_pipe.current_d(ctx)), BE_ITEMS)


def _run_be(ctx: Any) -> Optional[str]:
    day = us_pipe.current_d(ctx)
    if day is None:
        return None

    def one(kind: str) -> str:
        rows = _be_rows(ctx)
        before = {key: dict(row) for key, row in rows.items()}
        current = day.replace(day=1)
        dates = _kind_dates(rows, kind)
        months = [current]
        if len(dates) < BE_MIN_ROWS or dates[-1] < current.isoformat():
            months.append(us_pipe._prev_month(day))  # 冷啟動或剛換月：上個月的最後幾列才接得起來
        problem: Optional[BaseException] = None
        for month in months:
            try:
                _merge_points(rows, kind, _fetch_month(ctx, kind, month, day), day)
            except Exception as exc:  # noqa: BLE001 - 這個月失敗不丟掉另一個月已經拿到的列
                problem = problem or exc
                if us_pipe._kind(exc) in us_pipe._STOP_KINDS:
                    break  # 主機被擋或帳本不夠：不再打同一個主機
        changed = rows != before
        if changed:
            ordered = [rows[key] for key in sorted(rows)[-BE_ROWS:]]
            ctx.store.put_obs(OBS_INFL_BE, {"rows": ordered}, data_date=ordered[-1]["date"])
        if problem is not None:
            raise problem
        if not rows:
            raise market_calc.ParseError("empty")
        if not changed:
            ctx.store.obs_not_yet(OBS_INFL_BE)
        dates = _kind_dates(rows, kind)
        # 債市休市的日子（哥倫布日、退伍軍人節）D 沒有列不算錯，稍後再看一次（和 us.ust 一樣）
        return "done" if dates and dates[-1] >= day.isoformat() else "not_yet"

    return us_pipe._run_batch(ctx, BE_KEY, day.isoformat(), BE_ITEMS, one, obs_for=lambda _item: OBS_INFL_BE,
                              max_not_yet=4, not_yet_retry_s=3600.0)


# ── CPI 與核心 CPI（BLS） ─────────────────────────────────────────────────────


def bls_enabled() -> bool:
    """一個開關：api.bls.gov 在 market_http.ALLOWED_HOSTS 才抓。之後要停用只要把主機拿掉；每次呼叫才讀白名單（測試 monkeypatch）。"""
    return market_calc.CPI_HOST in market_http.ALLOWED_HOSTS


def _stored_series(ctx: Any) -> dict[str, dict[str, float]]:
    raw = us_pipe._obs_payload(ctx, OBS_INFL_CPI).get("series")
    out: dict[str, dict[str, float]] = {}
    if isinstance(raw, dict):
        for series_id in src.BLS_SERIES:
            points = raw.get(series_id)
            if isinstance(points, dict):
                clean = {key: float(value) for key, value in points.items()
                         if isinstance(key, str) and _MONTH_KEY_RE.fullmatch(key) and us_pipe._finite(value) is not None}
                if clean:
                    out[series_id] = clean
    return out


def _cpi_plan(ctx: Any) -> tuple[market_calc.CpiPoll, dict[str, dict[str, float]], dict, bool]:
    """(輪詢計畫, 已存的序列, 排程紀錄, 已存的序列是否帶著未來月份)。已存的序列先去掉晚於上限的月份；有去掉東西（毒化）時，排程
    當成冷啟動（stored＝None）：不然四個序列都帶 2099-12 時 cpi_stored_month ≥ expected → done，之後完全不再問。"""
    now_et = us_pipe._et_now(ctx)
    ceiling = market_calc.cpi_month_ceiling(market_calc.cpi_expected_month(now_et.date()))
    trimmed, poisoned = market_calc.cpi_trim_future(_stored_series(ctx), ceiling)
    stored = {series_id: points for series_id, points in trimmed.items() if points}  # 只剩未來月份的序列整個當沒有
    state = us_pipe._state(ctx, CPI_KEY)
    plan = market_calc.cpi_poll(now_et, None if poisoned else market_calc.cpi_stored_month(stored),
                                us_pipe._finite(state.get("last_probe")), enabled=bls_enabled())
    return plan, stored, state, poisoned


def _due_cpi(ctx: Any) -> bool:
    return _cpi_plan(ctx)[0].due


def _fetch_series(ctx: Any, series_id: str, ceiling: str) -> src.BlsSeries:
    if ctx.should_stop():
        raise market_http.FetchError("budget")  # 這個任務的時間到了：不再送下一個請求，也不算失敗
    try:
        series = src.fetch_bls_series(series_id, http=ctx.http, ledger=ctx.ledger)
    except market_calc.ParseError as exc:
        if exc.kind == "vendor" and exc.detail == "limit":
            # 200＋REQUEST_NOT_PROCESSED 或 Results 是空的：BLS 沒處理這個請求。和真的 429 一樣冷卻主機 2 小時、任務退避
            # （15→30→60→120 分鐘）；不是「沒有資料」，這個任務也沒有 max_fails 可以把一個月鎖死。
            if ctx.ledger is not None:
                ctx.ledger.cooldown(src.BLS_HOST, market_http.COOLDOWN_SECONDS)
            raise market_http.FetchError("rate_limited") from None
        raise
    if series.latest is not None and series.latest > ceiling:
        # 晚於「應已公布最新月＋1」：來源出狀況（2026-10-28 審查 gp2 C1），整個回應不收、這一輪什麼都不寫
        raise market_calc.ParseError("date", "future")
    return series


def _poll_cpi(ctx: Any, plan: market_calc.CpiPoll, stored: dict[str, dict[str, float]], state: dict,
              poisoned: bool = False) -> Optional[str]:
    """先問 CUUR0000SA0（一個請求）；月份前進才抓另外三個，沒前進但已存的序列有落後探針的只補抓落後的；四個序列合在一起存
    （半組不寫），月份對不上也存，但不算做完。poisoned（已存的序列帶過未來月份，stored 已經去掉）一律當成前進：重抓、重寫，
    才把壞月份從 obs 裡洗掉。"""
    probe = _fetch_series(ctx, src.BLS_PROBE, plan.ceiling)
    held = max(stored[src.BLS_PROBE]) if stored.get(src.BLS_PROBE) else None
    advanced = held is None or probe.latest > held or poisoned
    target = probe.latest if held is None else max(probe.latest, held)
    behind = [series_id for series_id in src.BLS_SERIES
              if series_id != src.BLS_PROBE and (series_id not in stored or max(stored[series_id]) < target)]
    if not advanced and not behind:
        state["last_probe"] = float(ctx.now)  # 沒有進展：兩小時後（窗口外是隔天）再問
        us_pipe._save_state(ctx, CPI_KEY, state)
        return "not_yet"
    fetched = {src.BLS_PROBE: probe.values}
    for series_id in src.BLS_SERIES:
        if series_id != src.BLS_PROBE and (advanced or series_id in behind):
            fetched[series_id] = _fetch_series(ctx, series_id, plan.ceiling).values
    for series_id, values in fetched.items():
        if stored.get(series_id) and max(values) < max(stored[series_id]):
            raise market_calc.ParseError("conflict", "regress")  # 比已存的還舊：來源出狀況，這一輪什麼都不寫
    series = {**stored, **fetched}  # 沒重抓的序列沿用已存的
    month = min(max(values) for values in series.values())  # 四個序列都有的最新月份
    payload = {
        "series": {series_id: dict(sorted(values.items())) for series_id, values in series.items()},
        # 取得日（BLS 條款：應註明取得日期）＝這一輪抓的台北日期，卡片寫「取得日」
        "retrieved": datetime.fromtimestamp(float(ctx.now), _TPE).date().isoformat(),
    }
    ctx.store.put_obs(OBS_INFL_CPI, payload, data_date=us_pipe._month_end(month).isoformat(), period=month)
    finished = month >= plan.expected
    state["last_probe"] = None if finished else float(ctx.now)
    us_pipe._save_state(ctx, CPI_KEY, state)
    return None if finished else "not_yet"


def _run_cpi(ctx: Any) -> Optional[str]:
    plan, stored, state, poisoned = _cpi_plan(ctx)
    if not plan.due:
        return None  # 白名單關了或還沒到時間（due 和 run 之間不會變，保險）：什麼請求都不送
    try:
        return _poll_cpi(ctx, plan, stored, state, poisoned)
    except Exception as exc:  # noqa: BLE001 - 失敗記在 obs 上（框架另外記任務的 fail_streak 與退避）
        kind = us_pipe._kind(exc)
        if kind not in us_pipe._NON_FAILURE:
            ctx.store.obs_checked(OBS_INFL_CPI, error_kind=kind)
        raise


# ── 任務表（同一群組內依登記順序：load_modules 的順序讓這個模組排在 market_pipeline_us 之後，所以在 us.ust 後面） ──

TASK_TABLE: tuple[tuple[str, str, Callable[[Any], bool], Callable[[Any], Optional[str]], float], ...] = (
    ("us.infl.be", "core", _due_be, _run_be, 35.0),  # 名目＋實質各最多 2 個請求（含上個月）
    # BLS：探針＋另外三個＝4 個請求，帳本間隔 5 秒（約 15 秒）；放 core 而不是 low：low 排在最後，重的一輪會被 STOPPED
    ("us.infl.cpi", "core", _due_cpi, _run_cpi, 35.0),
)


def build_tasks() -> list[Task]:
    return [Task(name=name, group=group, due=due, run=run, budget_s=budget, market=MARKET)
            for name, group, due, run, budget in TASK_TABLE]


TASKS: list[Task] = build_tasks()


def register_tasks(register_fn: Callable[[Task], None] = register) -> None:
    for task in TASKS:
        register_fn(task)


register_tasks()  # import 時登記進框架的記憶體表（不做 I/O）
