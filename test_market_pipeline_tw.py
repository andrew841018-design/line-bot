"""market_pipeline_tw（/盤勢 台股 collect 任務，2026-10-17）的測試：假的 http、暫存的 Store、固定的休市表、
注入的時間。測的是排程：目標日、最早時間、每天次數、颱風判斷與撤銷、BFI82U 補洞、TWT49U 涵蓋接續、
FinMind 接入前的 5 天比對、期交所 15:00 起每 30 分鐘。解析本身在 test_market_sources_tw。"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

import market_calendar
import market_http
import market_paths
import market_pipeline_tw as tw
import market_store
import market_tasks

TPE = ZoneInfo("Asia/Taipei")
NO_DATA = {"stat": "很抱歉，沒有符合條件的資料!"}
INDEX_UP = "<p style ='color:red'>+</p>"
UP = "<p style= color:red>+</p>"
CLOSE_FIELDS = [
    "證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價",
    "漲跌(+/-)", "漲跌價差", "最後揭示買價", "最後揭示買量", "最後揭示賣價", "最後揭示賣量", "本益比",
]
TWT49U_FIELDS = [
    "資料日期", "股票代號", "股票名稱", "除權息前收盤價", "除權息參考價", "權值+息值", "權/息", "漲停價格", "跌停價格",
    "開盤競價基準", "減除股利參考價", "詳細資料", "最近一次申報資料 季別/日期", "最近一次申報每股 (單位)淨值",
    "最近一次申報每股 (單位)盈餘",
]
# 2026 年休市表裡 10 月的兩天（其他月份照真實表的分類方式）＋開始／最後交易日（交易日）
HOLIDAYS_2026 = [
    {"date": "2026-01-01", "name": "開國紀念日", "note": "依規定放假1日。", "kind": "closed"},
    {"date": "2026-01-02", "name": "國曆新年開始交易日", "note": "國曆新年開始交易。", "kind": "trading"},
    {"date": "2026-10-09", "name": "國慶日", "note": "補假。", "kind": "closed"},
    {"date": "2026-10-26", "name": "光復節", "note": "補假。", "kind": "closed"},
]


# ── 小工具 ──────────────────────────────────────────────────────────────────────
def at(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> float:
    """台北牆上時間 → epoch 秒。"""
    return datetime(y, m, d, hh, mm, tzinfo=TPE).timestamp()


def roc(day: date) -> str:
    return f"{day.year - 1911}年{day.month:02d}月{day.day:02d}日"


def ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


def day_of(url: str) -> date:
    params = query(url)
    raw = params.get("date") or params.get("dayDate")
    return datetime.strptime(raw, "%Y%m%d").date()


def close_row(code: str, close: str, volume: str = "1,000") -> list[str]:
    sign, diff = (UP, "1.00") if close != "--" else ("<p> </p>", "0.00")
    return [code, "名稱", volume, "1", "1", close, close, close, close, sign, diff, close, "1", close, "1", "0.00"]


def allbut_payload(day: date, closes: dict[str, str], *, taiex: str = "47,111.26", turnover: str = "993,814,830,090") -> dict:
    return {
        "stat": "OK",
        "date": ymd(day),
        "type": "ALLBUT0999",
        "tables": [
            {
                "title": f"{roc(day)} 價格指數(臺灣證券交易所)",
                "fields": ["指數", "收盤指數", "漲跌(+/-)", "漲跌點數", "漲跌百分比(%)", "特殊處理註記"],
                "data": [["發行量加權股價指數", taiex, INDEX_UP, "141.39", "0.30", ""]],
            },
            {
                "title": f"{roc(day)} 大盤統計資訊",
                "fields": ["成交統計", "成交金額(元)", "成交股數(股)", "成交筆數"],
                "data": [["總計(1~15)", turnover, "11,820,630,612", "4,986,590"]],
            },
            {
                "title": "漲跌證券數合計",
                "fields": ["類型", "整體市場", "股票"],
                "data": [
                    ["上漲(漲停)", "9,016(68)", "684(19)"],
                    ["下跌(跌停)", "6,541(84)", "286(0)"],
                    ["持平", "1,190", "106"],
                    ["未成交", "17,678", "5"],
                    ["無比價", "3,528", "1"],
                ],
            },
            {
                "title": f"{roc(day)} 每日收盤行情(全部(不含權證、牛熊證、可展延牛熊證))",
                "fields": CLOSE_FIELDS,
                "data": [close_row(code, close) for code, close in closes.items()],
            },
            {},
        ],
    }


def bfi_payload(day: date) -> dict:
    return {
        "stat": "OK",
        "date": ymd(day),
        "title": f"{roc(day)} 三大法人買賣金額統計表",
        "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"],
        "data": [
            ["自營商(自行買賣)", "8,381,436,969", "18,560,032,105", "-10,178,595,136"],
            ["自營商(避險)", "28,653,271,647", "51,888,684,916", "-23,235,413,269"],
            ["投信", "43,848,499,145", "32,800,992,175", "11,047,506,970"],
            ["外資及陸資(不含外資自營商)", "356,324,360,692", "402,471,523,529", "-46,147,162,837"],
            ["外資自營商", "0", "0", "0"],
            ["合計", "437,207,568,453", "505,721,232,725", "-68,513,664,272"],
        ],
    }


def twt49u_payload(start: date, end: date, rows: list[list[str]]) -> dict:
    if not rows:
        return NO_DATA
    return {"stat": "OK", "title": "除權除息計算結果表", "fields": TWT49U_FIELDS, "data": rows,
            "strDate": ymd(start), "endDate": ymd(end)}


def ex_row(day: date, code: str, pre: str, ref: str) -> list[str]:
    return [roc(day), code, "名稱", pre, ref, "1.000000", "息", "1", "1", ref, ref, f"{code},x", "", "", ""]


def holidays_payload(year: int, rows: list[list[str]]) -> dict:
    return {"stat": "ok", "date": f"{year}0101", "fields": ["日期", "名稱", "說明"], "data": rows,
            "queryYear": year, "total": len(rows)}


def finmind_payload(code: str, rows: list[tuple[date, float, int, int]]) -> dict:
    data = [
        {"date": day.isoformat(), "stock_id": code, "Trading_Volume": volume, "Trading_money": money,
         "open": close, "max": close, "min": close, "close": close, "spread": 0.0, "Trading_turnover": 1}
        for day, close, volume, money in rows
    ]
    return {"msg": "success", "status": 200, "data": data}


def futures_rows(day: date) -> list[dict]:
    def row(contract: str, long_: int, short_: int) -> dict:
        return {"Date": ymd(day), "ContractCode": contract, "Item": "外資及陸資", "TradingVolume(Net)": "120",
                "OpenInterest(Long)": str(long_), "OpenInterest(Short)": str(short_),
                "OpenInterest(Net)": str(long_ - short_)}

    return [row("臺股期貨", 20000, 117582), row("小型臺指期貨", 1000, 4928), row("微型臺指期貨", 500, 12915)]


class Clock:
    """帳本與 Store 用的假時鐘；帳本等間隔時呼叫 sleep，時間往前走、測試不真的等。"""

    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += float(seconds)


class FakeHttp:
    """照 market_http.fetch 的樣子：先在帳本預扣，再依網址片段回假的內容（dict→JSON）或丟 FetchError。"""

    def __init__(self) -> None:
        self.routes: list[tuple[str, object]] = []
        self.calls: list[str] = []

    def route(self, needle: str, handler) -> None:
        self.routes.insert(0, (needle, handler))  # 後加的優先（測試中途換回應）

    def fetch(self, url, *, max_bytes, timeout=15.0, accept=("json",), headers=None, ledger=None):
        if ledger is not None:
            ledger.reserve(urlsplit(url).hostname)
        self.calls.append(url)
        for needle, handler in self.routes:
            if needle in url:
                result = handler(url) if callable(handler) else handler
                if isinstance(result, BaseException):
                    raise result
                body = result if isinstance(result, bytes) else json.dumps(result, ensure_ascii=False).encode()
                return market_http.Response(200, body, {"content-type": "application/json"}, url)
        raise market_http.FetchError("status")

    def count(self, needle: str) -> int:
        return sum(needle in url for url in self.calls)

    def days(self, needle: str) -> list[date]:
        return [day_of(url) for url in self.calls if needle in url]


def make_env(tmp_path: Path, *, holidays: bool = True) -> SimpleNamespace:
    root = tmp_path / ("plain" if holidays else "bare")
    root.mkdir()
    state = root / "state"
    state.mkdir(mode=0o700)
    clock = Clock(at(2026, 10, 1))
    store = market_store.Store(state / "market_history.sqlite", now_fn=clock)
    if holidays:
        store.put_obs("tw.holidays.2026", {"year": 2026, "rows": HOLIDAYS_2026, "unknown": []}, data_date=None)
        store.put_obs("tw.holidays.2025", {"year": 2025, "rows": [], "unknown": []}, data_date=None)
    return SimpleNamespace(store=store, http=FakeHttp(), clock=clock, paths=market_paths.Paths.under(root))


@pytest.fixture
def env(tmp_path):
    environment = make_env(tmp_path)
    yield environment
    environment.store.close()


@pytest.fixture
def bare_env(tmp_path):
    environment = make_env(tmp_path, holidays=False)
    yield environment
    environment.store.close()


def make_ctx(env: SimpleNamespace, now: float) -> market_tasks.Context:
    """一輪 collect 的 Context：日曆每輪從 store 重建（覆寫、實際成交日、休市表）。"""
    env.clock.value = max(env.clock.value, now)
    return market_tasks.Context(
        store=env.store,
        http=env.http,
        ledger=env.store.ledger(now_fn=env.clock, sleep_fn=env.clock.sleep),
        now=now,
        paths=env.paths,
        deadline=1e9,
        us_cal=market_calendar.us_calendar(env.store),
        tw_cal=market_calendar.tw_calendar(env.store),
        clock=lambda: 0.0,
    )


TASKS = {task.name: task for task in tw.tasks()}


def run(ctx: market_tasks.Context, name: str) -> str:
    return market_tasks.run_task(ctx, TASKS[name])


def seed_days(env: SimpleNamespace, obs_id: str, days: list[date]) -> None:
    payload = {"days": {day.isoformat(): {"seeded": True} for day in days}, "latest": max(days).isoformat()}
    env.store.put_obs(obs_id, payload, data_date=max(days).isoformat())


def sessions_before(day: date, count: int) -> list[date]:
    out, cursor = [], day - timedelta(days=1)
    while len(out) < count:
        if cursor.weekday() < 5 and cursor.isoformat() not in {row["date"] for row in HOLIDAYS_2026}:
            out.append(cursor)
        cursor -= timedelta(days=1)
    return out


def sched(env: SimpleNamespace, name: str) -> dict:
    return env.store.task_state(tw.STATE_PREFIX + name)


# ── 登錄 ──────────────────────────────────────────────────────────────────────────
def test_tasks_are_registered_in_the_framework_groups():
    with market_tasks.isolated_registry():
        tw.register_all()
        names = {task.name: (task.group, task.market) for task in market_tasks.registered()}
    assert names["tw.allbut"] == ("core", "tw")
    assert names["tw.holidays"] == ("core", "tw")
    assert names["tw.allbut.backfill"] == ("backfill", "tw")
    assert names["tw.finmind"] == ("backfill", "tw")
    assert names["tw.twt49u.bootstrap"] == ("backfill", "tw")
    assert {f"tw.taifex.{x}" for x in ("futures", "options_inst", "pcr", "txo", "fx")} <= set(names)
    assert names["tw.pe"] == ("low", "tw")


# ── 目標日 ────────────────────────────────────────────────────────────────────────
def test_target_day_when_the_mac_slept_through_the_evening(env):
    friday = date(2026, 10, 16)
    env.http.route("MI_INDEX", lambda url: allbut_payload(day_of(url), {"0050": "109.00", "2330": "2,420.00"}))
    # 週五晚上整個沒跑，週六早上 9 點才醒：每個端點的目標仍是週五
    ctx = make_ctx(env, at(2026, 10, 17, 9, 0))
    assert tw.allbut_target(ctx) == friday
    assert tw.bfi_target(ctx) == friday
    assert tw.margin_target(ctx) == friday
    assert run(ctx, "tw.allbut") == "ok"
    assert env.http.days("MI_INDEX") == [friday]
    # 週一 14:30 前目標還是週五（已經有了）；14:30 後 ALLBUT 換週一，三大法人等 15:30、融資等 21:30
    assert tw.allbut_target(make_ctx(env, at(2026, 10, 19, 14, 29))) is None
    ctx = make_ctx(env, at(2026, 10, 19, 14, 31))
    assert tw.allbut_target(ctx) == date(2026, 10, 19)
    assert tw.bfi_target(ctx) == friday
    assert tw.margin_target(ctx) == friday
    assert tw.margin_target(make_ctx(env, at(2026, 10, 19, 21, 30))) == date(2026, 10, 19)


def test_holiday_and_unknown_calendar_days(env, bare_env):
    # 10/9 是休市表上的補假：10/9 下午的目標是前一個交易日 10/8
    assert tw.target_session(make_ctx(env, at(2026, 10, 9, 15, 0)).tw_cal, datetime(2026, 10, 9, 15, 0, tzinfo=TPE),
                             tw._rule("twse_allbut0999")[0]) == date(2026, 10, 8)
    # 休市表還沒抓到（bootstrap）：平日先當開市
    cal = make_ctx(bare_env, at(2026, 10, 9, 15, 0)).tw_cal
    assert cal.is_session(date(2026, 10, 9)) is None
    assert tw.expected_session(cal, date(2026, 10, 9)) is True
    assert tw.expected_session(cal, date(2026, 10, 10)) is False  # 週六


# ── ALLBUT0999：最早時間、每天上限、颱風 ─────────────────────────────────────────────────
def test_allbut_earliest_time_daily_cap_and_last_try_kept_for_20_00(env):
    wednesday = date(2026, 10, 14)
    seed_days(env, tw.OBS_ALLBUT, sessions_before(wednesday, 10))
    env.http.route("MI_INDEX", NO_DATA)  # 證交所一直說「沒有資料」
    # FinMind 有這天的加權指數：市場有開，只是我們沒抓到（來源漏日），不是休市
    env.http.route("finmindtrade", finmind_payload("TAIEX", [(wednesday, 47000.0, 1, 2)]))
    assert tw.allbut_target(make_ctx(env, at(2026, 10, 14, 14, 29))) is None
    results = {}
    for hh, mm in ((14, 30), (14, 45), (15, 0), (15, 30), (16, 0), (19, 59), (20, 0), (20, 30)):
        results[(hh, mm)] = run(make_ctx(env, at(2026, 10, 14, hh, mm)), "tw.allbut")
    assert results == {
        (14, 30): "not_yet",
        (14, 45): "not_due",  # 30 分鐘內不重打
        (15, 0): "not_yet",
        (15, 30): "not_yet",
        (16, 0): "not_due",  # 第 4 次留到 20:00
        (19, 59): "not_due",
        (20, 0): "not_yet",
        (20, 30): "not_due",  # 每天 4 次用完
    }
    assert env.http.count("MI_INDEX") == 4
    assert env.http.count("finmindtrade") == 1  # 只有 20:00 那次才問 FinMind
    assert env.store.calendar_overrides("tw") == {}
    assert env.store.get_obs(tw.OBS_ALLBUT)["error"] == "source_gap"
    # 台北日期換了，次數重算：隔天凌晨還能再試（目標仍是 10/14）
    assert tw.allbut_target(make_ctx(env, at(2026, 10, 15, 0, 30))) == wednesday


@pytest.mark.parametrize("mac_slept", [False, True])
def test_typhoon_closure_needs_20_00_and_both_sources_empty_and_is_revoked_on_data(env, mac_slept):
    wednesday = date(2026, 10, 14)
    seed_days(env, tw.OBS_ALLBUT, sessions_before(wednesday, 10))
    published = {"yes": False}
    closes = {"0050": "109.00", "2330": "2,420.00"}
    env.http.route("MI_INDEX", lambda url: allbut_payload(wednesday, closes) if published["yes"] else NO_DATA)
    env.http.route("finmindtrade", {"msg": "success", "status": 200, "data": []})
    if not mac_slept:
        assert run(make_ctx(env, at(2026, 10, 14, 15, 0)), "tw.allbut") == "not_yet"
        assert env.store.calendar_overrides("tw") == {}  # 20:00 前只算「還沒公布」
        assert env.http.count("finmindtrade") == 0
        decide_at = at(2026, 10, 14, 20, 5)
    else:
        decide_at = at(2026, 10, 15, 8, 0)  # 那晚 Mac 在睡：隔天早上第一次跑就判斷
    assert run(make_ctx(env, decide_at), "tw.allbut") == "not_yet"
    override = env.store.calendar_overrides("tw")[wednesday]
    assert override["kind"] == "closed" and override["confirmed"] is False
    assert override["evidence"] == tw.TYPHOON_EVIDENCE
    # 下一輪的日曆把那天當休市：主要任務不再打它
    assert market_calendar.tw_calendar(env.store).is_session(wednesday) is False
    assert tw.allbut_target(make_ctx(env, decide_at + 1800)) is None
    # 隔天補洞時再看一次；證交所這次有資料 → 撤銷（revoked，紀錄留著）、日曆恢復
    published["yes"] = True
    ctx = make_ctx(env, at(2026, 10, 15, 9, 30))
    assert tw.allbut_backfill(ctx) == ("recheck", wednesday)
    assert run(ctx, "tw.allbut.backfill") == "ok"
    assert env.store.calendar_overrides("tw")[wednesday]["kind"] == "revoked"
    assert market_calendar.tw_calendar(env.store).is_session(wednesday) is True
    assert wednesday.isoformat() in env.store.get_obs(tw.OBS_ALLBUT)["payload"]["days"]
    assert tw.closures(env.store) == {}


def test_closure_is_also_revoked_when_finmind_later_has_the_day(env):
    wednesday = date(2026, 10, 14)
    env.store.set_task_state(tw.STATE_PREFIX + tw.TASK_ALLBUT, {"closures": {wednesday.isoformat(): {"at": 1.0}}})
    env.store.set_calendar_override("tw", wednesday, "closed", tw.TYPHOON_EVIDENCE)
    ctx = make_ctx(env, at(2026, 10, 16, 10, 0))
    tw._revoke_seen_closures(ctx, [SimpleNamespace(date=wednesday)])
    assert env.store.calendar_overrides("tw")[wednesday]["kind"] == "revoked"
    assert tw.closures(env.store) == {}


def test_allbut_keeps_all_common_stocks_short_and_tracked_long(env):
    env.store.put_obs(tw.OBS_HOLDINGS, {"rows": [["2330", "台積電", 60.0]]}, data_date="2026-10-15")
    days = sorted(sessions_before(date(2026, 10, 17), 12))
    for index, day in enumerate(days):
        closes = {"0050": f"{100 + index}.00", "2330": f"{2000 + index}.00", "1101": f"{30 + index}.00", "1216": "--"}
        tw.store_allbut(env.store, tw._tw().parse_twse_allbut0999(allbut_payload(day, closes), day))
    # tw_all：全部普通股＋0050＋加權指數，每檔只留最近 10 列；「--」沒有收盤不存
    assert len(env.store.closes(tw.MARKET_ALL, "1101")) == 10
    assert env.store.closes(tw.MARKET_ALL, "1216") == []
    assert env.store.closes(tw.MARKET_ALL, "1101")[0].date == days[2]
    # tw：只有追蹤的（0050 成分、0050、加權指數），長歷史不被 prune 吃掉
    assert len(env.store.closes(tw.MARKET, "2330")) == 12
    assert len(env.store.closes(tw.MARKET, "0050")) == 12
    assert env.store.closes(tw.MARKET, "1101") == []
    taiex = env.store.closes(tw.MARKET, "TAIEX")[-1]
    assert taiex.close == 47111.26 and taiex.volume == 993_814_830_090  # 加權指數的「量」＝成交金額
    summary = env.store.get_obs(tw.OBS_ALLBUT)["payload"]
    assert len(summary["days"]) == 10 and summary["latest"] == days[-1].isoformat()
    assert summary["days"][days[-1].isoformat()]["turnover_twd"] == 993_814_830_090
    assert summary["days"][days[-1].isoformat()]["adv_limit"] == 19


def test_old_tw_gaps_are_backfilled_one_per_day(env):
    """2026-10-23（tw_history (a)）：2026-07-10 那種休市表沒寫的臨時休市是 FinMind bootstrap 帶進來的洞，補洞只看最近
    10 個交易日、永遠輪不到，跨過它的 120／240 日窗口全部無效。歷史窗口裡更早的洞（加權指數沒有收盤）每天補一個：
    證交所與 FinMind 都沒有 → 暫定休市；證交所有 → 存起來。舊日期看收盤（ALLBUT 摘要只留 10 天），不會整年重抓。"""
    sessions = sessions_before(date(2026, 10, 17), 100)  # 10/16 往前 100 個預期交易日（新到舊）
    gap_new, gap_old = sessions[72], sessions[80]  # 72、80 個交易日前，休市表沒寫
    env.store.upsert_closes(tw.MARKET, tw.SYM_TAIEX, [(day, 47000.0, 1) for day in sessions if day not in (gap_new, gap_old)],
                            tw.SOURCE_FINMIND)
    seed_days(env, tw.OBS_ALLBUT, sessions[:10])  # 最近 10 個交易日都有了：近期的補洞沒事做
    closes = {"0050": "109.00", "2330": "2,420.00"}
    env.http.route("MI_INDEX", lambda url: allbut_payload(gap_old, closes) if day_of(url) == gap_old else NO_DATA)
    env.http.route("finmindtrade", {"msg": "success", "status": 200, "data": []})
    ctx = make_ctx(env, at(2026, 10, 17, 10, 0))  # 週六上午
    assert ctx.tw_cal.missing_bars(sessions[-1], sessions[10]) == [gap_old, gap_new]  # 前提：只有這兩個洞
    assert tw.allbut_backfill(ctx) == ("history", gap_new)  # 新的先
    assert run(ctx, "tw.allbut.backfill") == "not_yet"
    assert env.http.days("MI_INDEX") == [gap_new] and env.http.count("finmindtrade") == 1  # 這一輪只試一個
    override = env.store.calendar_overrides("tw")[gap_new]
    assert override["kind"] == "closed" and override["confirmed"] is False and override["evidence"] == tw.TYPHOON_EVIDENCE
    assert market_calendar.tw_calendar(env.store).is_session(gap_new) is False
    for minutes in (15, 60, 300):  # 同一天不再試別的舊洞
        assert tw.allbut_backfill(make_ctx(env, at(2026, 10, 17, 10, 0) + minutes * 60)) is None
    assert env.http.count("MI_INDEX") == 1
    # 隔天輪到更舊的那個：證交所有那天的資料 → 存起來，日曆就看得到那天有開
    ctx = make_ctx(env, at(2026, 10, 18, 10, 0))
    assert tw.allbut_backfill(ctx) == ("history", gap_old)
    assert run(ctx, "tw.allbut.backfill") == "ok"
    assert gap_old in {row.date for row in env.store.closes(tw.MARKET, tw.SYM_TAIEX)}
    assert market_calendar.tw_calendar(env.store).missing_bars(sessions[-1], sessions[10]) == []
    assert tw.allbut_backfill(make_ctx(env, at(2026, 10, 19, 10, 0))) is None  # 沒有洞了
    assert env.http.days("MI_INDEX") == [gap_new, gap_old]


def test_an_old_gap_that_stays_is_left_after_two_tries(env):
    """證交所沒有、FinMind 卻有那天（來源漏日）：不寫休市；隔天再試一次，試了兩次還在就不再試，留給每日維護
    （outcome 的 calendar_missing_bars）。舊日期的試過次數另外記：一般的 tries 只留最近 40 個日期。"""
    sessions = sessions_before(date(2026, 10, 17), 40)
    gap = sessions[30]
    env.store.upsert_closes(tw.MARKET, tw.SYM_TAIEX, [(day, 47000.0, 1) for day in sessions if day != gap], tw.SOURCE_FINMIND)
    seed_days(env, tw.OBS_ALLBUT, sessions[:10])
    env.http.route("MI_INDEX", NO_DATA)
    env.http.route("finmindtrade", finmind_payload("TAIEX", [(gap, 47000.0, 1, 2)]))
    for day in (17, 18):
        ctx = make_ctx(env, at(2026, 10, day, 10, 0))
        assert tw.allbut_backfill(ctx) == ("history", gap)
        assert run(ctx, "tw.allbut.backfill") == "not_yet"
    assert tw.allbut_backfill(make_ctx(env, at(2026, 10, 19, 10, 0))) is None
    assert env.http.days("MI_INDEX") == [gap, gap] and env.store.calendar_overrides("tw") == {}


def test_an_old_gap_is_not_given_up_on_transient_errors(env):
    """2026-10-23 審查 r9 gp1 N1：證交所連兩天出錯、或 FinMind 問不到，都不算「試過」——不然來源好了也不再補，
    7/10 那種洞就一直留著。證交所說沒有資料、而且 FinMind 答得出來（或存到資料）才算一次。"""
    sessions = sessions_before(date(2026, 10, 17), 40)
    gap = sessions[30]
    env.store.upsert_closes(tw.MARKET, tw.SYM_TAIEX, [(day, 47000.0, 1) for day in sessions if day != gap], tw.SOURCE_FINMIND)
    seed_days(env, tw.OBS_ALLBUT, sessions_before(date(2026, 10, 22), 15))  # 近期都有了（含當天）：只剩歷史洞
    env.http.route("MI_INDEX", market_http.FetchError("status"))
    env.http.route("finmindtrade", {"msg": "success", "status": 200, "data": []})
    for day in (17, 18):  # 證交所連兩天出錯
        ctx = make_ctx(env, at(2026, 10, day, 10, 0))
        assert tw.allbut_backfill(ctx) == ("history", gap)
        run(ctx, "tw.allbut.backfill")
    env.http.route("MI_INDEX", NO_DATA)
    env.http.route("finmindtrade", market_http.FetchError("status"))
    for day in (19, 20):  # 證交所說沒有資料，FinMind 卻問不到
        ctx = make_ctx(env, at(2026, 10, day, 10, 0))
        assert tw.allbut_backfill(ctx) == ("history", gap)
        run(ctx, "tw.allbut.backfill")
    assert env.store.calendar_overrides("tw") == {}
    env.http.route("finmindtrade", {"msg": "success", "status": 200, "data": []})
    ctx = make_ctx(env, at(2026, 10, 21, 10, 0))  # 兩邊都好了：照樣補，寫成暫定休市
    assert tw.allbut_backfill(ctx) == ("history", gap)
    run(ctx, "tw.allbut.backfill")
    assert env.store.calendar_overrides("tw")[gap]["kind"] == "closed"


def test_an_old_gap_waits_for_the_days_own_allbut(env):
    """2026-10-23 審查 r9 gp1：交易日當天的主要 ALLBUT 還沒存，歷史洞不搶額度（15:00 那次重試要留著）。"""
    sessions = sessions_before(date(2026, 10, 20), 40)
    gap = sessions[30]
    env.store.upsert_closes(tw.MARKET, tw.SYM_TAIEX, [(day, 47000.0, 1) for day in sessions if day != gap], tw.SOURCE_FINMIND)
    seed_days(env, tw.OBS_ALLBUT, sessions[:10])  # 到 10/19 都有了，10/20（週二，當天）還沒
    assert tw.allbut_backfill(make_ctx(env, at(2026, 10, 20, 10, 0))) is None
    seed_days(env, tw.OBS_ALLBUT, [date(2026, 10, 20)] + sessions[:10])
    assert tw.allbut_backfill(make_ctx(env, at(2026, 10, 20, 16, 0))) == ("history", gap)


def test_ledger_refusal_is_not_counted_as_a_try(env):
    env.http.route("MI_INDEX", lambda url: allbut_payload(day_of(url), {"0050": "109.00", "2330": "2,420.00"}))
    ledger = env.store.ledger(now_fn=env.clock, sleep_fn=env.clock.sleep)
    env.clock.value = at(2026, 10, 14, 9, 0)
    for _ in range(12):  # 證交所 www 今天的 12 次已經用完（例如 bootstrap）
        ledger.reserve("www.twse.com.tw")
        env.clock.sleep(11)
    ctx = make_ctx(env, at(2026, 10, 14, 15, 0))
    assert tw.allbut_target(ctx) == date(2026, 10, 14)
    assert run(ctx, "tw.allbut") == "not_yet"
    assert env.http.calls == []
    assert sched(env, tw.TASK_ALLBUT).get("count", 0) == 0


# ── BFI82U：漏掉的日子在每天上限內補 ─────────────────────────────────────────────────────
def test_bfi82u_backfills_missed_days_newest_first_within_the_daily_cap(env):
    friday = date(2026, 10, 16)
    seed_days(env, tw.OBS_INST3, [date(2026, 10, 12)])  # 10/13–10/15 漏了（Mac 關了三天）
    env.http.route("BFI82U", lambda url: bfi_payload(day_of(url)))
    ctx = make_ctx(env, at(2026, 10, 16, 16, 0))
    assert run(ctx, "tw.bfi82u") == "ok"  # 目標日先
    assert run(ctx, "tw.bfi82u.backfill") == "ok"
    assert run(make_ctx(env, at(2026, 10, 16, 16, 15)), "tw.bfi82u.backfill") == "ok"
    assert run(make_ctx(env, at(2026, 10, 16, 16, 30)), "tw.bfi82u.backfill") == "not_due"  # 每天 3 次
    assert env.http.days("BFI82U") == [friday, date(2026, 10, 15), date(2026, 10, 14)]
    assert run(make_ctx(env, at(2026, 10, 17, 10, 0)), "tw.bfi82u.backfill") == "ok"  # 隔天補最後一天
    days = env.store.get_obs(tw.OBS_INST3)["payload"]["days"]
    assert sorted(days) == [f"2026-10-{d}" for d in ("12", "13", "14", "15", "16")]
    assert days["2026-10-16"]["foreign"] == -46_147_162_837
    assert run(make_ctx(env, at(2026, 10, 17, 11, 0)), "tw.bfi82u.backfill") == "not_due"  # 5 日齊了


def test_bfi82u_backfill_cannot_use_the_tries_kept_for_todays_data(env):
    seed_days(env, tw.OBS_INST3, [date(2026, 10, 12), date(2026, 10, 15)])
    env.http.route("BFI82U", lambda url: bfi_payload(day_of(url)))
    # 週五早上：今天 15:30 的資料還沒到手，三次裡要留兩次給它，補洞只能用一次
    assert run(make_ctx(env, at(2026, 10, 16, 10, 0)), "tw.bfi82u.backfill") == "ok"
    assert run(make_ctx(env, at(2026, 10, 16, 11, 0)), "tw.bfi82u.backfill") == "not_due"
    assert run(make_ctx(env, at(2026, 10, 16, 15, 30)), "tw.bfi82u") == "ok"
    assert run(make_ctx(env, at(2026, 10, 16, 15, 45)), "tw.bfi82u.backfill") == "ok"  # 今天的到手了才放行
    assert env.http.days("BFI82U") == [date(2026, 10, 14), date(2026, 10, 16), date(2026, 10, 13)]


def test_backfill_leaves_room_for_the_evening_margin_request(env):
    friday = date(2026, 10, 16)
    ctx = make_ctx(env, at(2026, 10, 16, 9, 0))
    # 證交所 www 今天已經打了 5 次；今天的 ALLBUT、BFI82U、TWT49U、融資都還沒到手 → 預留 2+2+1+2
    env.store.set_task_state(tw.STATE_PREFIX + tw.TASK_ALLBUT, {"day": friday.isoformat(), "count": 1})
    env.store.set_task_state(tw.STATE_PREFIX + tw.TASK_HOLIDAYS, {"day": friday.isoformat(), "count": 4})
    assert tw.reserves(ctx, friday) == {"tw.allbut": 2, "tw.bfi82u": 2, "tw.twt49u": 1, "tw.margin": 2}
    assert tw.www_used(ctx, friday) == 5
    state = tw._sched(env.store, tw.TASK_ALLBUT, friday)
    assert not tw.spare_ok(ctx, tw.TASK_ALLBUT, state, friday, 4)  # 5＋7＝12：補洞不能再打
    assert tw.allbut_backfill(ctx) is None
    env.store.set_task_state(tw.STATE_PREFIX + tw.TASK_ALLBUT, {"day": friday.isoformat(), "count": 0})
    state = tw._sched(env.store, tw.TASK_ALLBUT, friday)
    assert tw.spare_ok(ctx, tw.TASK_ALLBUT, state, friday, 4)  # 4＋7＝11：還有一次
    # 昨天的次數不算今天的
    env.store.set_task_state(tw.STATE_PREFIX + tw.TASK_HOLIDAYS, {"day": "2026-10-15", "count": 12})
    assert tw.www_used(ctx, friday) == 0


# ── 融資 ──────────────────────────────────────────────────────────────────────────
def test_margin_waits_for_21_30_and_marks_not_yet_without_failing(env):
    friday = date(2026, 10, 16)
    payload = {
        "stat": "OK",
        "date": ymd(friday),
        "tables": [{
            "title": f"{roc(friday)} 信用交易統計",
            "fields": ["項目", "買進", "賣出", "現金(券)償還", "前日餘額", "今日餘額"],
            "data": [
                ["融資(交易單位)", "343,600", "318,542", "7,624", "9,521,230", "9,538,664"],
                ["融券(交易單位)", "20,982", "30,004", "4,570", "229,150", "233,602"],
                ["融資金額(仟元)", "28,656,955", "28,942,556", "536,222", "646,415,871", "645,594,048"],
            ],
        }],
    }
    published = {"yes": False}
    env.http.route("MI_MARGN", lambda url: payload if published["yes"] else NO_DATA)
    env.store.put_obs(tw.OBS_MARGIN, {"date": "2026-10-15"}, data_date="2026-10-15")
    assert tw.margin_target(make_ctx(env, at(2026, 10, 16, 21, 29))) is None
    assert run(make_ctx(env, at(2026, 10, 16, 21, 30)), "tw.margin") == "not_yet"
    assert env.store.get_obs(tw.OBS_MARGIN)["fail_streak"] == 0
    published["yes"] = True
    assert run(make_ctx(env, at(2026, 10, 16, 22, 0)), "tw.margin") == "ok"
    obs = env.store.get_obs(tw.OBS_MARGIN)
    assert obs["data_date"] == "2026-10-16" and obs["payload"]["margin_balance_k"] == 645_594_048


# ── TWT49U：bootstrap 分段、之後從上次檢查日接續 ───────────────────────────────────────────
def test_twt49u_bootstrap_in_ranges_then_daily_from_the_last_checked_date(env):
    events = {date(2026, 9, 16): ex_row(date(2026, 9, 16), "2330", "2,385.00", "2,377.99")}

    def answer(url):
        params = query(url)
        start = datetime.strptime(params["startDate"], "%Y%m%d").date()
        end = datetime.strptime(params["endDate"], "%Y%m%d").date()
        return twt49u_payload(start, end, [row for day, row in events.items() if start <= day <= end])

    env.http.route("TWT49U", answer)
    saturday = at(2026, 10, 17, 10, 0)
    ctx = make_ctx(env, saturday)
    assert tw.twt49u_core(ctx) is None  # 16:00 前不抓「今天」
    first = tw.twt49u_bootstrap(ctx)
    assert first == (date(2026, 10, 16) - timedelta(days=tw.TWT49U_CHUNK_DAYS - 1), date(2026, 10, 16))
    for minutes in (0, 15, 30, 45):
        run(make_ctx(env, saturday + minutes * 60), "tw.twt49u.bootstrap")
    assert env.http.count("TWT49U") == tw.TWT49U_BOOTSTRAP_PER_DAY  # bootstrap 每天最多 3 段
    spans = env.store.coverage(tw.COVERAGE_TWT49U)
    assert spans == [(date(2026, 10, 16) - timedelta(days=3 * tw.TWT49U_CHUNK_DAYS - 1), date(2026, 10, 16))]
    assert [row.code for row in env.store.exrights("2330")] == ["2330"]
    # 週六 16:00 後：目標日仍是週五（已涵蓋），週末不必再打
    assert tw.twt49u_core(make_ctx(env, at(2026, 10, 17, 16, 30))) is None
    # 週日補最舊那段；沒有事件的區間（證交所回「沒有符合條件的資料」）也算涵蓋
    assert run(make_ctx(env, at(2026, 10, 18, 10, 0)), "tw.twt49u.bootstrap") == "ok"
    assert env.store.coverage(tw.COVERAGE_TWT49U)[0][0] == date(2026, 10, 18) - timedelta(days=tw.HISTORY_DAYS)
    assert tw.twt49u_bootstrap(make_ctx(env, at(2026, 10, 18, 11, 0))) is None  # 歷史段補完
    # 週一 16:00 前沒有事；16:00 後從上次檢查日（10/16）抓到今天，每天 1 次
    assert tw.twt49u_core(make_ctx(env, at(2026, 10, 19, 15, 59))) is None
    ctx = make_ctx(env, at(2026, 10, 19, 16, 0))
    assert tw.twt49u_core(ctx) == (date(2026, 10, 16), date(2026, 10, 19))
    assert run(ctx, "tw.twt49u") == "ok"
    assert env.store.coverage(tw.COVERAGE_TWT49U)[-1][1] == date(2026, 10, 19)
    assert tw.twt49u_core(make_ctx(env, at(2026, 10, 20, 15, 0))) is None
    assert tw.twt49u_core(make_ctx(env, at(2026, 10, 20, 16, 0))) == (date(2026, 10, 19), date(2026, 10, 20))
    obs = env.store.get_obs(tw.OBS_TWT49U)
    assert obs["payload"]["covered_to"] == "2026-10-19"


def test_twt49u_chunks_continue_after_the_mac_was_off(env):
    # 週五 16:00 那次沒跑到（Mac 在睡）：週六早上就補「週四～週五」，不必等到 16:00
    env.store.add_coverage(tw.COVERAGE_TWT49U, date(2025, 8, 1), date(2026, 10, 15))
    assert tw.twt49u_core(make_ctx(env, at(2026, 10, 17, 9, 0))) == (date(2026, 10, 15), date(2026, 10, 16))
    covered = [(date(2025, 8, 1), date(2026, 10, 2))]
    chunks = tw.twt49u_chunks(covered, date(2025, 8, 20), date(2026, 10, 16))
    assert chunks == [(date(2026, 10, 2), date(2026, 10, 16))]  # 一段、從上次檢查日開始
    holes = tw.twt49u_chunks([(date(2026, 1, 1), date(2026, 3, 1)), (date(2026, 3, 10), date(2026, 10, 1))],
                             date(2026, 1, 1), date(2026, 10, 5))
    assert holes == [(date(2026, 10, 1), date(2026, 10, 5)), (date(2026, 3, 1), date(2026, 3, 9))]


def test_twt49u_runs_at_16_00_even_after_the_midnight_catch_up(env):
    """2026-10-23（tw_history (c) Step 1）：次數以台北日期計，凌晨 00:0x 補昨天那次用掉當天唯一的名額，16:00 那次
    從來沒跑過，0050 細節頁每個交易日都要等到隔天凌晨才換到 T。每天上限 1→2：16:30 照跑、涵蓋到 T；隔天就不必再打。"""
    env.store.add_coverage(tw.COVERAGE_TWT49U, date(2025, 8, 1), date(2026, 10, 16))  # 涵蓋到上週五
    env.http.route("TWT49U", NO_DATA)
    assert run(make_ctx(env, at(2026, 10, 20, 0, 5)), "tw.twt49u") == "ok"  # 週二凌晨補到週一
    assert env.store.coverage(tw.COVERAGE_TWT49U)[-1][1] == date(2026, 10, 19)
    assert sched(env, tw.TASK_TWT49U)["count"] == 1
    assert tw.twt49u_core(make_ctx(env, at(2026, 10, 20, 15, 59))) is None
    ctx = make_ctx(env, at(2026, 10, 20, 16, 30))
    assert tw.twt49u_core(ctx) == (date(2026, 10, 19), date(2026, 10, 20))
    assert run(ctx, "tw.twt49u") == "ok"
    assert env.store.coverage(tw.COVERAGE_TWT49U)[-1][1] == date(2026, 10, 20)
    asked = [(query(url)["startDate"], query(url)["endDate"]) for url in env.http.calls if "TWT49U" in url]
    assert asked == [("20261016", "20261019"), ("20261019", "20261020")]
    for hh, mm in ((0, 5), (8, 0), (15, 59)):  # 隔天：涵蓋已經到週二，不必再打
        assert tw.twt49u_core(make_ctx(env, at(2026, 10, 21, hh, mm))) is None


# ── 休市表 ────────────────────────────────────────────────────────────────────────
def test_holidays_bootstrap_this_and_previous_year_then_next_year_weekly(bare_env):
    env = bare_env
    tables = {
        2026: holidays_payload(2026, [["2026-10-09", "國慶日", "補假。"], ["2026-10-26", "光復節", "補假。"]]),
        2025: holidays_payload(2025, [["2025-10-10", "國慶日", "依規定放假1日。"]]),
    }
    env.http.route("holidaySchedule", lambda url: tables.get(int(query(url)["date"][:4]), NO_DATA))
    monday = at(2026, 11, 2, 9, 0)
    assert run(make_ctx(env, monday), "tw.holidays") == "ok"
    assert run(make_ctx(env, monday + 900), "tw.holidays") == "ok"
    assert run(make_ctx(env, monday + 1800), "tw.holidays") == "not_due"  # bootstrap 當天最多 2 次
    assert [query(url)["date"] for url in env.http.calls] == ["20260101", "20250101"]
    rows = env.store.get_obs("tw.holidays.2026")["payload"]["rows"]
    assert rows[0] == {"date": "2026-10-09", "name": "國慶日", "note": "補假。", "kind": "closed"}
    assert market_calendar.tw_calendar(env.store).is_session(date(2026, 10, 26)) is False
    # 下一年的表 11 月起試抓；還沒公布不算失敗，一週後再試
    tuesday = at(2026, 11, 3, 9, 0)
    assert run(make_ctx(env, tuesday), "tw.holidays") == "not_yet"
    assert query(env.http.calls[-1])["date"] == "20270101"
    assert tw.holiday_year(make_ctx(env, tuesday + 86400)) is None
    tables[2027] = holidays_payload(2027, [["2027-01-01", "開國紀念日", "依規定放假1日。"]])
    assert tw.holiday_year(make_ctx(env, tuesday + 7 * 86400)) == 2027


# ── 期交所：目標日（今天要過 15:00 才算）、07:00 起每 2 小時、每天 9 次（匯率 5 次），資料日以回應為準 ──────
def test_taifex_polls_every_30_minutes_and_keeps_the_payload_date(env):
    lag = {"day": date(2026, 10, 15)}
    env.http.route("MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContracts", lambda url: futures_rows(lag["day"]))
    name = "tw.taifex.futures"
    # 什麼都還沒存：15:00 前也先補前一個交易日（2026-10-18 拿掉 15:00 硬閘）
    assert run(make_ctx(env, at(2026, 10, 16, 14, 59)), name) == "ok"
    obs = env.store.get_obs(tw.OBS_FUTURES)
    assert obs["data_date"] == "2026-10-15"  # 期交所晚一天：資料日照回應
    assert obs["payload"]["foreign"]["TX"]["oi_net"] == -97582
    assert obs["payload"]["foreign"]["MTX"]["oi_net"] == -3928 and obs["payload"]["foreign"]["TMF"]["oi_net"] == -12415
    assert run(make_ctx(env, at(2026, 10, 16, 15, 10)), name) == "not_due"  # 過了 15:00 目標變 10/16，但不到 2 小時
    assert run(make_ctx(env, at(2026, 10, 16, 18, 0)), name) == "not_yet"  # 還是 10/15：不算失敗
    assert run(make_ctx(env, at(2026, 10, 16, 21, 0)), name) == "not_yet"
    assert run(make_ctx(env, at(2026, 10, 16, 22, 30)), name) == "not_due"  # 2 小時間隔
    for hour in (0, 3, 6):  # 2026-10-23：07:00 前不打（期交所約 06:36 才放上前一個交易日）
        assert run(make_ctx(env, at(2026, 10, 17, hour, 0)), name) == "not_due"
    for hour in (7, 9, 11):  # 隔天（週六）07:00 起每 2 小時
        assert run(make_ctx(env, at(2026, 10, 17, hour, 0)), name) == "not_yet"  # 期交所還沒更新
    assert env.store.get_obs(tw.OBS_FUTURES)["fail_streak"] == 0
    lag["day"] = date(2026, 10, 16)
    assert run(make_ctx(env, at(2026, 10, 17, 13, 0)), name) == "ok"  # 週六：目標仍是週五
    obs = env.store.get_obs(tw.OBS_FUTURES)
    assert obs["data_date"] == "2026-10-16" and obs["payload"]["date"] == "2026-10-16"
    # 期交所只回最新一天：自己留前幾天，publish 才算得出外資淨口數的日變化
    assert sorted(obs["payload"]["days"]) == ["2026-10-15", "2026-10-16"] and obs["payload"]["latest"] == "2026-10-16"
    assert obs["payload"]["days"]["2026-10-15"]["TX"]["oi_net"] == -97582
    assert run(make_ctx(env, at(2026, 10, 17, 15, 30)), name) == "not_due"  # 到手了就不再打
    old_format = {"date": "2026-10-15", "foreign": {"TX": {"oi_net": -1}}}  # 只有最新一天的舊格式也接得上
    merged = tw._merge_futures(old_format, {"date": "2026-10-16", "foreign": {"TX": {"oi_net": -2}}})
    assert merged["days"] == {"2026-10-15": {"TX": {"oi_net": -1}}, "2026-10-16": {"TX": {"oi_net": -2}}}


def test_taifex_morning_rules(env):
    """平日早上：手上已有前一個交易日就不打；週日早上手上只有週四 → 補週五（不用等到 15:00）。"""
    env.http.route("MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContracts",
                   lambda url: futures_rows(date(2026, 10, 16)))
    name = "tw.taifex.futures"
    env.store.put_obs(tw.OBS_FUTURES, {"date": "2026-10-15", "foreign": {"TX": {"oi_net": -1}}}, data_date="2026-10-15")
    assert run(make_ctx(env, at(2026, 10, 16, 9, 0)), name) == "not_due"  # 週五早上：目標是週四，已經有了
    assert run(make_ctx(env, at(2026, 10, 18, 9, 0)), name) == "ok"  # 週日早上：目標是週五，手上只有週四
    assert env.store.get_obs(tw.OBS_FUTURES)["data_date"] == "2026-10-16"
    assert run(make_ctx(env, at(2026, 10, 19, 9, 0)), name) == "not_due"  # 週一早上：目標仍是週五


def test_taifex_tries_start_at_07_00_and_stay_2_hours_apart(env):
    """2026-10-23：期交所的檔案約 06:36 才放上前一個交易日。以前每 3 小時、每天 6 次：00／03／06 點那三次一定白打，
    10/23 剩下的 09／12／15 點又碰上 CDN 給 CSV 的時段，要到隔天 00:09 才再試；匯率每天 2 次都燒在 00:11、03:12。
    現在 07:00 前不打，之後每 2 小時一次、每天最多 9 次（07、09…23 點）；匯率 5 次（07、09…15 點；審查 r9 gp1：
    匯率也會碰到 CSV 的時段，2 次太少）。"""
    env.http.route("MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContracts",
                   lambda url: futures_rows(date(2026, 10, 15)))  # 期交所一直沒更新
    env.http.route("DailyForeignExchangeRates", [{"Date": "20261015", "USD/NTD": "32.300"}])
    env.store.put_obs(tw.OBS_FUTURES, {"date": "2026-10-15", "foreign": {"TX": {"oi_net": -1}}}, data_date="2026-10-15")
    env.store.put_obs(tw.OBS_FX, {"date": "2026-10-15", "usd_ntd": 32.3}, data_date="2026-10-15")
    tried: dict[str, list[str]] = {"tw.taifex.futures": [], "tw.taifex.fx": []}
    for step in range(24 * 4):  # 週二 10/20 一整天、每 15 分鐘一輪（和 launchd 一樣）
        now = at(2026, 10, 20) + step * 900
        for name, seen in tried.items():
            before = len(env.http.calls)
            result = run(make_ctx(env, now), name)
            if len(env.http.calls) > before:
                assert result == "not_yet", name
                seen.append(datetime.fromtimestamp(now, TPE).strftime("%H:%M"))
            else:
                assert result == "not_due", name
    assert tried["tw.taifex.futures"] == ["07:00", "09:00", "11:00", "13:00", "15:00", "17:00", "19:00", "21:00", "23:00"]
    assert tried["tw.taifex.fx"] == ["07:00", "09:00", "11:00", "13:00", "15:00"]  # 匯率 5 次（審查 r9 gp1）
    for name in tried:  # 隔天：次數歸零、離上次也超過 2 小時，但還沒 07:00 就不打
        assert run(make_ctx(env, at(2026, 10, 21, 1, 0)), name) == "not_due", name
        assert run(make_ctx(env, at(2026, 10, 21, 6, 59)), name) == "not_due", name
        assert run(make_ctx(env, at(2026, 10, 21, 7, 0)), name) == "not_yet", name
    for spec in tw.TAIFEX_SPECS:  # 五個期交所任務同一套規則
        assert tw._rule(spec.endpoint) == (time(7, 0), 5 if spec.task == "tw.taifex.fx" else 9), spec.task
    # 主機合計 4×9＋5＝41 次／天；期交所不在 HOST_RULES 裡，用帳本的預設上限
    cap = market_store.HOST_RULES.get("openapi.taifex.com.tw", market_store.DEFAULT_HOST_RULE).daily_cap
    assert sum(tw._rule(spec.endpoint)[1] for spec in tw.TAIFEX_SPECS) == 41 <= cap


def test_taifex_txo_csv_body_is_stored_without_backing_off(env):
    """2026-10-23：CDN 給 DailyMarketReportOpt 的 CSV 時照樣入庫（以前 format、退避，台指選擇權卡停在前一天）。"""
    header = ("日期,契約,到期月份(週別),履約價,買賣權,開盤價,最高價,最低價,最後成交價,成交量,結算價,未沖銷契約量,"
              "最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段")
    lines = [
        header,
        "20261015,TXO,202610,54000,買權,-,-,-,-,0,500,9876,-,-,-,-,,一般",
        "20261015,TXO,202610,46000,賣權,-,-,-,-,0,500,5432,-,-,-,-,,一般",
        "20261015,TXO,202610,54000,買權,-,-,-,-,0,-,-,-,-,-,-,,盤後",  # 盤後盤不收
        "20261015,TXO,202610W4,47000,買權,-,-,-,-,0,500,100,-,-,-,-,,一般",  # 週選不併進月選
    ]
    env.http.route("DailyMarketReportOpt", ("\ufeff" + "\r\n".join(lines) + "\r\n").encode("utf-8"))
    assert run(make_ctx(env, at(2026, 10, 16, 15, 0)), "tw.taifex.txo") == "ok"
    obs = env.store.get_obs(tw.OBS_TXO)
    assert obs["data_date"] == "2026-10-15" and obs["fail_streak"] == 0
    assert sorted(map(tuple, obs["payload"]["rows"])) == [("202610", 46000.0, "P", 5432, 500.0),
                                                          ("202610", 54000.0, "C", 9876, 500.0)]
    state = env.store.task_state("tw.taifex.txo")
    assert state["last_kind"] == "ok" and state["fail_streak"] == 0 and state["next_try"] is None


# ── FinMind：接入前 5 天比對、停這輪、修訂 ──────────────────────────────────────────────
def _seed_five_sessions(env: SimpleNamespace, closes_by_day: dict[date, dict[str, str]]) -> None:
    for day, closes in sorted(closes_by_day.items()):
        tw.store_allbut(env.store, tw._tw().parse_twse_allbut0999(allbut_payload(day, closes), day))


FIVE = [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)]


def _finmind_router(env: SimpleNamespace, series: dict[str, list[tuple[date, float, int, int]]]) -> None:
    def answer(url):
        params = query(url)
        start, end = date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])
        rows = [row for row in series.get(params["data_id"], []) if start <= row[0] <= end]
        return finmind_payload(params["data_id"], rows)

    env.http.route("finmindtrade", answer)


def test_finmind_accepts_a_symbol_only_when_its_last_5_sessions_match_allbut(env):
    env.store.put_obs(
        tw.OBS_HOLDINGS,
        {"rows": [["2330", "台積電", 60.0], ["2317", "鴻海", 5.0], ["2454", "聯發科", 4.0]]},
        data_date="2026-10-16",
    )
    closes = {day: {"0050": f"{105 + i}.00", "2330": f"{2400 + i}.00", "2317": f"{240 + i}.00"} for i, day in enumerate(FIVE)}
    for day in FIVE[2:]:  # 2454 只有 3 天的證交所收盤：還不能比
        closes[day]["2454"] = "4,250.00"
    _seed_five_sessions(env, closes)
    older = [date(2026, 10, 7), date(2026, 10, 8)]
    series = {
        "TAIEX": [(day, 47111.26, 1, 993_814_830_090) for day in older + FIVE],
        "0050": [(day, 104.0, 10, 1000) for day in older] + [(day, 105.0 + i, 10, 1000) for i, day in enumerate(FIVE)],
        "2330": [(day, 2399.0, 7, 70) for day in older] + [(day, 2400.0 + i, 7, 70) for i, day in enumerate(FIVE)],
        # 鴻海最後一天對不上（244.5 vs 244.00）：整檔不接
        "2317": [(day, 240.0 + i, 3, 30) for i, day in enumerate(FIVE[:-1])] + [(FIVE[-1], 244.5, 3, 30)],
    }
    _finmind_router(env, series)
    ctx = make_ctx(env, at(2026, 10, 17, 10, 0))
    assert run(ctx, "tw.finmind") == "partial"
    asked = [query(url)["data_id"] for url in env.http.calls if "finmindtrade" in url]
    assert asked == ["TAIEX", "0050", "2330", "2317"]  # 2454 沒打（不浪費 FinMind 次數）
    rows_2330 = env.store.closes(tw.MARKET, "2330")
    assert [(row.date, row.source) for row in rows_2330[:2]] == [(older[0], "finmind"), (older[1], "finmind")]
    assert {row.source for row in rows_2330[2:]} == {"twse"}  # 證交所的列不被 FinMind 蓋掉
    assert {row.source for row in env.store.closes(tw.MARKET, "2317")} == {"twse"}
    assert env.store.closes(tw.MARKET, "TAIEX")[0].volume == 993_814_830_090
    progress = env.store.get_obs(tw.OBS_FINMIND)["payload"]
    assert progress == {"total": 5, "accepted": 3, "rejected": {"2317": "mismatch"}}
    state = sched(env, tw.TASK_FINMIND)
    assert state["symbols"]["2317"]["reason"] == "mismatch"
    # 拒收的一週內不再打；接入的沒有缺洞也不再打
    assert run(make_ctx(env, at(2026, 10, 17, 11, 0)), "tw.finmind") == "not_due"


def test_finmind_stops_the_round_when_it_says_no(env):
    env.store.put_obs(tw.OBS_HOLDINGS, {"rows": [["2330", "台積電", 60.0]]}, data_date="2026-10-16")
    _seed_five_sessions(env, {day: {"0050": "105.00", "2330": "2,400.00"} for day in FIVE})
    env.http.route("finmindtrade", {"msg": "Requests reach the upper limit.", "status": 402, "data": []})
    ctx = make_ctx(env, at(2026, 10, 17, 10, 0))
    assert run(ctx, "tw.finmind") == "status"  # msg≠success：停這輪，框架記 status
    assert env.http.count("finmindtrade") == 1
    assert tw._finmind_due(make_ctx(env, at(2026, 10, 17, 10, 40))) is False  # 先停一小時
    assert env.store.get_obs(tw.OBS_FINMIND)["payload"]["stopped"] == "status"
    env.http.route("finmindtrade", market_http.FetchError("status"))  # 非 200 也一樣
    assert tw._finmind_due(make_ctx(env, at(2026, 10, 17, 11, 1))) is True
    assert run(make_ctx(env, at(2026, 10, 17, 11, 1)), "tw.finmind") == "status"
    assert env.http.count("finmindtrade") == 2


def test_finmind_gap_fill_and_revision_replaces_the_whole_symbol(env):
    _seed_five_sessions(env, {day: {"0050": "105.00", "2330": "2,400.00"} for day in FIVE})
    older = [date(2026, 10, 7), date(2026, 10, 8)]
    later = [date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22),
             date(2026, 10, 23), date(2026, 10, 27), date(2026, 10, 28)]  # 10/26 補假
    series = {code: [(day, 105.0 if code == "0050" else 47111.26, 10, 1000) for day in older + FIVE + later]
              for code in ("0050", "TAIEX")}
    _finmind_router(env, series)
    # 1) bootstrap：0050 與加權指數比對過最近 5 天後接入
    run(make_ctx(env, at(2026, 10, 17, 10, 0)), "tw.finmind")
    info = sched(env, tw.TASK_FINMIND)["symbols"]["0050"]
    assert info["accepted_on"] == "2026-10-17" and info["checked_to"] == "2026-10-16"

    def allbut_only(*days):
        for day in days:
            tw.store_allbut(env.store, tw._tw().parse_twse_allbut0999(allbut_payload(day, {"0050": "105.00", "2330": "2,400.00"}), day))

    # 2) Mac 關機：ALLBUT 只有 10/21、10/22；10/19–10/20 的洞用 FinMind 補（從洞前 5 個交易日開始比）
    allbut_only(date(2026, 10, 21), date(2026, 10, 22))
    ctx = make_ctx(env, at(2026, 10, 23, 10, 0))
    job = tw.finmind_next(ctx, tw._sched(env.store, tw.TASK_FINMIND, date(2026, 10, 23)))
    assert job[0] == "TAIEX" and job[3] == "gap" and job[1] == date(2026, 10, 12)
    run(ctx, "tw.finmind")
    rows = {row.date: row for row in env.store.closes(tw.MARKET, "0050")}
    assert rows[date(2026, 10, 19)].source == "finmind" and rows[date(2026, 10, 21)].source == "twse"
    # 3) 又一個洞（10/23、10/27），FinMind 這次把已存的 10/19 改了（來源修訂）：只拿到一段 → 不寫、標整檔重抓
    allbut_only(date(2026, 10, 28))
    series["0050"] = [(day, 99.0 if day == date(2026, 10, 19) else close, vol, money)
                      for day, close, vol, money in series["0050"]]
    ctx = make_ctx(env, at(2026, 10, 29, 10, 0))
    assert run(ctx, "tw.finmind") == "partial"
    assert sched(env, tw.TASK_FINMIND)["symbols"]["0050"]["needs_full"] is True
    assert {row.date: row.close for row in env.store.closes(tw.MARKET, "0050")}[date(2026, 10, 19)] == 105.0
    # 4) 下一輪整檔重抓、一個交易內替換：修訂後的值生效、洞補上、證交所的列原樣保留
    run(make_ctx(env, at(2026, 10, 29, 10, 15)), "tw.finmind")
    rows = {row.date: row for row in env.store.closes(tw.MARKET, "0050")}
    assert rows[date(2026, 10, 19)].close == 99.0 and rows[date(2026, 10, 19)].source == "finmind"
    assert rows[date(2026, 10, 23)].source == "finmind" and rows[date(2026, 10, 27)].source == "finmind"
    assert {rows[day].source for day in FIVE + [date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 28)]} == {"twse"}
    assert "needs_full" not in sched(env, tw.TASK_FINMIND)["symbols"]["0050"]


def test_overlap_check_rules():
    bars = [SimpleNamespace(date=day, close=100.0 + i) for i, day in enumerate(FIVE)]
    twse = {day: 100.0 + i for i, day in enumerate(FIVE)}
    assert tw.overlap_check(twse, bars, 5) == "ok"
    assert tw.overlap_check({**twse, FIVE[2]: 102.004}, bars, 5) == "ok"  # 小數 2 位的誤差內
    assert tw.overlap_check({**twse, FIVE[2]: 102.01}, bars, 5) == "mismatch"
    assert tw.overlap_check(dict(list(twse.items())[:4]), bars, 5) == "defer"
    assert tw.overlap_check(twse, bars[:2] + bars[3:], 5) == "mismatch"  # FinMind 缺了中間一天
    assert tw.overlap_check(twse, [], 5) == "defer"


# ── 外部字串與每天一次的來源 ─────────────────────────────────────────────────────────
def test_holdings_names_are_cleaned_and_codes_checked(env, monkeypatch):
    holdings = SimpleNamespace(
        as_of=date(2026, 10, 16),
        rows=[
            SimpleNamespace(symbol="2330", name="台積電\u202e", weight=60.5),
            SimpleNamespace(symbol="2317", name="鴻海 https://x.example", weight=5.0),
            SimpleNamespace(symbol="23X0", name="壞代號", weight=1.0),
        ],
        dropped_count=2,
        unmapped_weight=0.0,
    )
    monkeypatch.setattr(tw._tw(), "fetch_moneydj_0050", lambda **kwargs: holdings)
    assert run(make_ctx(env, at(2026, 10, 17, 9, 0)), "tw.holdings") == "ok"
    payload = env.store.get_obs(tw.OBS_HOLDINGS)["payload"]
    assert payload["rows"] == [["2330", "台積電", 60.5], ["2317", None, 5.0]]
    assert tw.tracked_codes(env.store) == ["TAIEX", "0050", "2330", "2317"]
    assert run(make_ctx(env, at(2026, 10, 17, 10, 0)), "tw.holdings") == "not_due"  # 每天一次


def test_every_taifex_endpoint_is_stored_with_its_payload_date(env):
    day = "20261015"
    env.http.route("PutCallRatio", (
        "\ufeff日期,賣權成交量,買權成交量,買賣權成交量比率%,賣權未平倉量,買權未平倉量,買賣權未平倉量比率%\n"
        "20261015,191704,255629,74.99,77287,127036,60.84\n"
        "20261014,337868,326539,103.47,50695,68105,74.44\n"
    ).encode("utf-8"))

    def opt(side: str, long_: int, short_: int) -> dict:
        return {"Date": day, "ContractCode": "臺指選擇權", "CallPut": side, "Item": "外資及陸資",
                "OpenInterest(Long)": str(long_), "OpenInterest(Short)": str(short_),
                "OpenInterest(Net)": str(long_ - short_), "ContractValueofOpenInterest(Net)(Thousands)": "-1000"}

    env.http.route("CallsAndPuts", [opt("CALL", 30000, 20000), opt("PUT", 15000, 40000)])

    def txo(month: str, strike: str, side: str, oi: str, session: str = "一般") -> dict:
        return {"Date": day, "Contract": "TXO", "ContractMonth(Week)": month, "StrikePrice": strike, "CallPut": side,
                "OpenInterest": oi, "SettlementPrice": "500", "TradingSession": session}

    env.http.route("DailyMarketReportOpt", [
        txo("202610", "54000", "買權", "9876"), txo("202610", "46000", "賣權", "5432"),
        txo("202610", "54000", "買權", "-", "盤後"),  # 盤後盤不收
        txo("202610W4", "47000", "買權", "100"),  # 週選不併進月選
        txo("202611", "48000", "賣權", "-"),  # 「-」＝沒有未平倉
    ])
    env.http.route("DailyForeignExchangeRates", [{"Date": "20261014", "USD/NTD": "32.300"}, {"Date": day, "USD/NTD": "32.338"}])
    now = at(2026, 10, 16, 15, 0)
    for name in ("tw.taifex.options_inst", "tw.taifex.pcr", "tw.taifex.txo", "tw.taifex.fx"):
        assert run(make_ctx(env, now), name) == "ok", name
    pcr = env.store.get_obs(tw.OBS_PCR)
    assert pcr["data_date"] == "2026-10-15" and [row["date"] for row in pcr["payload"]["rows"]] == ["2026-10-14", "2026-10-15"]
    assert pcr["payload"]["rows"][-1]["oi_ratio_pct"] == 60.84
    options = env.store.get_obs(tw.OBS_OPTIONS_INST)["payload"]
    assert options["foreign_call"]["oi_net"] == 10000 and options["foreign_put"]["oi_net"] == -25000
    txo_rows = env.store.get_obs(tw.OBS_TXO)["payload"]["rows"]
    assert sorted(map(tuple, txo_rows)) == [("202610", 46000.0, "P", 5432, 500.0), ("202610", 54000.0, "C", 9876, 500.0),
                                            ("202611", 48000.0, "P", None, 500.0)]
    fx = env.store.get_obs(tw.OBS_FX)
    assert fx["data_date"] == "2026-10-15" and fx["payload"]["usd_ntd"] == 32.338 and fx["payload"]["prev_date"] == "2026-10-14"


def test_daily_openapi_pe_and_revenue(env):
    env.http.route("BWIBBU_ALL", [{"Date": "1151016", "Code": "2330", "PEratio": "25.10"},
                                  {"Date": "1151016", "Code": "2603", "PEratio": "-"}])
    env.http.route("t187ap05_L", [
        {"資料年月": "11509", "公司代號": "2330", "營業收入-去年同月增減(%)": "30.5"},
        {"資料年月": "11508", "公司代號": "2330", "營業收入-去年同月增減(%)": "20.0"},
        {"資料年月": "11509", "公司代號": "2603", "營業收入-去年同月增減(%)": ""},
    ])
    assert run(make_ctx(env, at(2026, 10, 17, 7, 59)), "tw.pe") == "not_due"
    assert run(make_ctx(env, at(2026, 10, 17, 8, 0)), "tw.pe") == "ok"
    assert run(make_ctx(env, at(2026, 10, 17, 8, 0)), "tw.revenue") == "ok"
    pe = env.store.get_obs(tw.OBS_PE)
    assert pe["data_date"] == "2026-10-16" and pe["payload"]["pe"] == {"2330": 25.1, "2603": None}
    revenue = env.store.get_obs(tw.OBS_REVENUE)
    assert revenue["period"] == "2026-09" and revenue["data_date"] == "2026-09-30"
    assert revenue["payload"]["rows"] == {"2330": [2026, 9, 30.5], "2603": [2026, 9, None]}
    assert run(make_ctx(env, at(2026, 10, 17, 12, 0)), "tw.pe") == "not_due"  # 每天一次


def test_finmind_does_not_keep_asking_for_a_day_with_no_close(env):
    env.store.put_obs(tw.OBS_HOLDINGS, {"rows": [["2330", "台積電", 60.0]]}, data_date="2026-10-16")
    _seed_five_sessions(env, {day: {"0050": "105.00", "2330": "2,400.00"} for day in FIVE})
    series = {code: [(day, value, 10, 1000) for day in FIVE] for code, value in
              (("TAIEX", 47111.26), ("0050", 105.0), ("2330", 2400.0))}
    _finmind_router(env, series)
    run(make_ctx(env, at(2026, 10, 17, 10, 0)), "tw.finmind")
    assert env.store.get_obs(tw.OBS_FINMIND)["payload"]["accepted"] == 3
    # 10/19 台積電停牌（「--」沒有收盤）：FinMind 也沒有那天 → 問一次就好，不能每 15 分鐘再問
    monday = date(2026, 10, 19)
    tw.store_allbut(env.store, tw._tw().parse_twse_allbut0999(
        allbut_payload(monday, {"0050": "105.00", "2330": "--", "1101": "30.00"}), monday))
    asked_before = env.http.count("finmindtrade")
    assert run(make_ctx(env, at(2026, 10, 20, 10, 0)), "tw.finmind") is not None
    assert [query(url)["data_id"] for url in env.http.calls[asked_before:]] == ["2330"]
    assert run(make_ctx(env, at(2026, 10, 20, 10, 15)), "tw.finmind") == "not_due"
    assert sched(env, tw.TASK_FINMIND)["symbols"]["2330"]["checked_to"] == "2026-10-19"


def test_finmind_round_stops_before_a_request_that_could_overrun_the_budget(env):
    _seed_five_sessions(env, {day: {"0050": "105.00", "2330": "2,400.00"} for day in FIVE})
    _finmind_router(env, {"TAIEX": [(day, 47111.26, 1, 9) for day in FIVE]})
    ctx = make_ctx(env, at(2026, 10, 17, 10, 0))
    ctx.task_deadline = 20.0  # 這輪只剩 20 秒：bootstrap 請求最多跑 30 秒，不開始
    assert tw._finmind_run(ctx) == "not_yet"
    assert env.http.count("finmindtrade") == 0


def test_no_typhoon_override_while_the_calendar_cannot_confirm_the_day(bare_env):
    env = bare_env
    wednesday = date(2026, 10, 14)
    env.http.route("MI_INDEX", NO_DATA)
    env.http.route("finmindtrade", {"msg": "success", "status": 200, "data": []})
    ctx = make_ctx(env, at(2026, 10, 14, 20, 30))
    assert ctx.tw_cal.is_session(wednesday) is None  # 休市表還沒抓到
    assert run(ctx, "tw.allbut") == "not_yet"
    assert env.store.calendar_overrides("tw") == {}
    assert env.http.count("finmindtrade") == 0


def test_twt49u_bootstrap_keeps_one_try_for_the_16_00_continuation(env):
    env.store.add_coverage(tw.COVERAGE_TWT49U, date(2026, 6, 19), date(2026, 10, 15))  # 只有最近那段
    env.http.route("TWT49U", NO_DATA)
    friday = date(2026, 10, 16)
    assert run(make_ctx(env, at(2026, 10, 16, 9, 0)), "tw.twt49u.bootstrap") == "ok"
    assert run(make_ctx(env, at(2026, 10, 16, 9, 15)), "tw.twt49u.bootstrap") == "ok"
    assert run(make_ctx(env, at(2026, 10, 16, 9, 30)), "tw.twt49u.bootstrap") == "not_due"  # 第 3 次留給 16:00
    assert tw.twt49u_core(make_ctx(env, at(2026, 10, 16, 15, 59))) is None
    assert run(make_ctx(env, at(2026, 10, 16, 16, 0)), "tw.twt49u") == "ok"
    assert max(end for _, end in env.store.coverage(tw.COVERAGE_TWT49U)) == friday


def test_endpoint_rules_come_from_the_source_table_not_the_fallback():
    """接縫（2026-10-18）：_rule／_finmind_timeout 的預設表只是保險；實際要用 market_sources_tw 的設定，
    以後端點改名時這裡先失敗，不會默默換成預設值。"""
    import market_sources_tw as src

    for name in tw._DEFAULT_RULES:
        item = src.ENDPOINTS[name]
        assert tw._rule(name) == (item.earliest_tpe, int(item.daily_max)), name
        assert tw._DEFAULT_RULES[name] == (item.earliest_tpe, int(item.daily_max)), name  # 備援表也要跟著改
    short, long_range = date(2026, 10, 1), date(2026, 12, 31)
    assert tw._finmind_timeout(short, short) == float(src.ENDPOINTS["finmind_prices"].timeout_s)
    assert tw._finmind_timeout(short, long_range) == float(src.FINMIND_BOOTSTRAP_TIMEOUT_S)


def _taiex_sessions(store, days):
    """日曆要知道哪幾天開市：休市表以外的日子看加權指數的實際成交日（market_calendar.tw_calendar）。"""
    store.upsert_closes(tw.MARKET, tw.SYM_TAIEX, [(day, 20000.0, 1) for day in days], tw.SOURCE_TWSE)


def test_tw_jump_recompute_is_one_transaction(env, monkeypatch):
    """2026-10-23 審查 r7 #3：解除與加回在同一個交易；加回失敗時隔離維持原樣，不會變成這檔沒有隔離。"""
    store = env.store
    days = [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)]
    _taiex_sessions(store, days)
    store.add_coverage(tw.COVERAGE_TWT49U, days[0], days[-1])
    store.upsert_closes(tw.MARKET, "2330", [(d, c, 1) for d, c in zip(days, [100.0, 200.0, 201.0, 402.0, 403.0])],
                        tw.SOURCE_TWSE)
    tw.check_tw_jumps(store, ["2330"])
    before = store.quarantine(tw.MARKET)["2330"]
    store.upsert_closes(tw.MARKET, "2330", [(days[1], 101.0, 1), (days[2], 102.0, 1)], tw.SOURCE_TWSE)

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "add_quarantine", boom)
    with pytest.raises(RuntimeError):
        tw.check_tw_jumps(store, ["2330"])
    assert store.quarantine(tw.MARKET)["2330"] == before


def test_tw_jumps_only_compare_adjacent_sessions(env):
    """2026-10-23 審查 r7a N2：中間缺一天時，兩天各自正常的漲跌（+8%、+6.5%）加起來超過 10.5%，不能當跳空；
    補齊後也不能留下假隔離。除權息事件在缺的那天時也一樣。"""
    store = env.store
    fri, mon, tue = date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 20)
    _taiex_sessions(store, [fri, mon, tue])
    store.add_coverage(tw.COVERAGE_TWT49U, fri, tue)
    store.upsert_closes(tw.MARKET, "2330", [(fri, 100.0, 1), (tue, 115.0, 1)], tw.SOURCE_TWSE)  # 週一還沒存到
    tw.check_tw_jumps(store, ["2330"])
    assert store.quarantine(tw.MARKET).get("2330", []) == []
    store.upsert_closes(tw.MARKET, "2330", [(mon, 108.0, 1)], tw.SOURCE_TWSE)
    tw.check_tw_jumps(store, ["2330"])
    assert store.quarantine(tw.MARKET).get("2330", []) == []
    # 除權息在週一、週一那列還沒存：週五 100 → 週二 89（-11%）不是跳空
    store.upsert_exrights([(mon, "2317", 100.0, 90.0, "權息")])
    store.upsert_closes(tw.MARKET, "2317", [(fri, 100.0, 1), (tue, 89.0, 1)], tw.SOURCE_TWSE)
    tw.check_tw_jumps(store, ["2317"])
    assert store.quarantine(tw.MARKET).get("2317", []) == []


def test_tw_jumps_without_exrights_are_quarantined(env):
    """2026-10-18 審查 r6c I3：漲跌超過 10.5% 又沒有除權息事件（減資／分割，TWT49U 不含）→ 隔離那一段；
    涵蓋不到的日子不判斷；之後補到那天的事件就解除。"""
    store = env.store
    days = [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)]
    _taiex_sessions(store, days)
    store.upsert_closes(tw.MARKET, "2330", [(days[0], 100.0, 1), (days[1], 101.0, 1), (days[2], 202.0, 1), (days[3], 203.0, 1)],
                        tw.SOURCE_TWSE)
    tw.check_tw_jumps(store, ["2330"])
    assert store.quarantine(tw.MARKET) == {}  # 除權息表還沒涵蓋那天：不判斷
    store.add_coverage(tw.COVERAGE_TWT49U, days[0], days[3])
    tw.check_tw_jumps(store, ["2330", "TAIEX"])
    assert [(a, b, r) for a, b, r in store.quarantine(tw.MARKET)["2330"]] == [(days[1], days[2], "jump")]
    tw.check_tw_jumps(store, ["2330"])  # 重跑不重複
    assert len(store.quarantine(tw.MARKET)["2330"]) == 1
    store.upsert_exrights([(days[2], "2330", 101.0, 202.0, "權息")])  # 補到事件（示意）：解除
    tw.check_tw_jumps(store, ["2330"])
    assert store.quarantine(tw.MARKET).get("2330", []) == []


def test_tw_jump_quarantine_follows_corrected_prices(env):
    """2026-10-23 審查 r7 #3：來源改正了錯價，跳空已經不在 → 隔離要解除；還在的那段留著。"""
    store = env.store
    days = [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)]
    _taiex_sessions(store, days)
    store.add_coverage(tw.COVERAGE_TWT49U, days[0], days[-1])
    closes = [100.0, 200.0, 201.0, 402.0, 403.0]
    store.upsert_closes(tw.MARKET, "2330", [(d, c, 1) for d, c in zip(days, closes)], tw.SOURCE_TWSE)
    tw.check_tw_jumps(store, ["2330"])
    assert [(a, b) for a, b, _r in store.quarantine(tw.MARKET)["2330"]] == [(days[0], days[1]), (days[2], days[3])]
    store.upsert_closes(tw.MARKET, "2330", [(days[1], 101.0, 1), (days[2], 102.0, 1)], tw.SOURCE_TWSE)  # 來源改正
    tw.check_tw_jumps(store, ["2330"])
    assert [(a, b) for a, b, _r in store.quarantine(tw.MARKET)["2330"]] == [(days[2], days[3])]
    store.upsert_closes(tw.MARKET, "2330", [(days[3], 103.0, 1), (days[4], 104.0, 1)], tw.SOURCE_TWSE)
    tw.check_tw_jumps(store, ["2330"])
    assert store.quarantine(tw.MARKET).get("2330", []) == []
