"""機構動向 第一期 T 軌（2026-10-31）：證交所 T86（0050 成分股的三大法人買賣超股數）的解析、抓取與排程。

規格：ops/state/line_bot_inst_flow_20261028/IMPL_SPEC_p1.md §3、PLAN_v2.md §4，另加協調者 10/31 轉達的 /股票 實測：
- /股票 每天台北 05:36–06:03 打一次 T86（每月第一個交易日延到 08:00–10:00），兩邊的帳本互相看不到、又是同一個主機：
  /盤勢 的 T86（含補抓）05:30 起、06:15 前不抓；每月第一個交易日 10:00 前也不抓。
- 403、429、非 JSON、轉址：T86 整個停 2 小時（≥ /股票 要求的 1 小時），這一輪停手、不逐日補抓一直打。
全部用假的 http、暫存的 Store、合成的最小 T86 JSON（只留解析需要的結構），不連網。"""

from __future__ import annotations

import ast
import dataclasses
import json
import logging
import subprocess
import sys
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

import market_calendar
import market_http
import market_paths
import market_pipeline_tw as tw
import market_sources_tw as src
import market_store
import market_tasks

TPE = ZoneInfo("Asia/Taipei")
HERE = Path(__file__).resolve().parent
TUE, WED, THU, FRI = date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30)
LAST_FRI = date(2026, 10, 23)  # 10/26（週一）是休市表上的補假

# ── 合成的 T86（selectType=ALLBUT0999）：19 欄；解析只用代號與四欄買賣超股數 ─────────────────────────
T86_FIELDS = [
    "證券代號", "證券名稱",
    "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)", "外陸資買賣超股數(不含外資自營商)",
    "外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數",
    "投信買進股數", "投信賣出股數", "投信買賣超股數",
    "自營商買賣超股數",
    "自營商買進股數(自行買賣)", "自營商賣出股數(自行買賣)", "自營商買賣超股數(自行買賣)",
    "自營商買進股數(避險)", "自營商賣出股數(避險)", "自營商買賣超股數(避險)",
    "三大法人買賣超股數",
]
NO_DATA = {"stat": "很抱歉，沒有符合條件的資料!"}  # 非交易日
TOO_EARLY = {"stat": "查詢日期大於可查詢最大日期，請重新查詢!"}  # 還沒到的日子


def num(value: int) -> str:
    return f"{value:,}"


def t86_row(code: str, foreign: int, trust: int, dealer_self: int, dealer_hedge: int) -> list[str]:
    """一列（代號照證交所補空白）。外資自營商、自營商合計、三大法人三欄刻意填別的數：解析不能用它們。"""

    def trio(net: int) -> list[str]:
        buy = max(net, 0) + 100
        return [num(buy), num(buy - net), num(net)]

    return [f"{code}  ", "名稱", *trio(foreign), *trio(777), *trio(trust), num(888), *trio(dealer_self),
            *trio(dealer_hedge), num(999_999)]


def roc(day: date) -> str:
    return f"{day.year - 1911}年{day.month:02d}月{day.day:02d}日"


def t86_payload(day: date, rows: list, *, fields: list | None = None) -> dict:
    return {"stat": "OK", "date": day.strftime("%Y%m%d"), "title": f"{roc(day)} 三大法人買賣超日報",
            "fields": list(fields or T86_FIELDS), "data": rows, "selectType": "ALLBUT0999"}


ROWS = [
    t86_row("2330", 1_234_000, -56_000, 1_000, -2_000),
    t86_row("2454", -300, 0, 0, 5),
    t86_row("1101", 7, 7, 7, 7),  # 不是 0050 成分：不列
]


def parse_error(payload, **kwargs) -> tuple[str, str]:
    with pytest.raises(src.ParseError) as info:
        src.parse_twse_t86(payload, **{"expect_date": WED, **kwargs})
    return info.value.kind, info.value.detail


# ── 解析 ──────────────────────────────────────────────────────────────────────────
def test_parse_t86_keeps_four_signed_columns_for_the_requested_codes():
    rows = [*ROWS, [*t86_row("2317", 0, 0, 0, 0)[:4], "+1,000", *t86_row("2317", 0, 0, 0, 0)[5:]]]
    day = src.parse_twse_t86(t86_payload(WED, rows), expect_date=WED, codes=("2330", "2454", "2317", "2412"))
    assert day.date == WED
    # 2412 不在 T86：不列；負數、「+1,000」照收；外資＝外陸資（不含外資自營商）
    assert day.rows == {"2330": (1_234_000, -56_000, 1_000, -2_000), "2454": (-300, 0, 0, 5), "2317": (1_000, 0, 0, 0)}
    assert (day.rows["2330"].foreign, day.rows["2330"].trust) == (1_234_000, -56_000)
    assert (day.rows["2330"].dealer_self, day.rows["2330"].dealer_hedge) == (1_000, -2_000)
    assert set(src.parse_twse_t86(t86_payload(WED, ROWS)).rows) == {"2330", "2454", "1101"}  # 沒給代號＝全部


@pytest.mark.parametrize("payload", [NO_DATA, TOO_EARLY, {"stat": " 很抱歉，沒有符合條件的資料 ! "}])
def test_parse_t86_not_yet_stats_are_empty(payload):
    assert parse_error(payload) == ("empty", "")  # job 當「還沒公布」：不算失敗


def test_parse_t86_format_errors():
    good = t86_payload(WED, ROWS)
    assert parse_error([])[0] == "format"
    assert parse_error({"stat": "系統忙碌中，請稍後再試"})[0] == "format"
    assert parse_error({**good, "stat": None})[0] == "format"
    assert parse_error({**good, "fields": "證券代號"})[0] == "format"
    assert parse_error({**good, "data": {}})[0] == "format"
    assert parse_error({**good, "date": "2026-10-28"})[0] == "format"
    assert parse_error({**good, "date": "20261027"})[0] == "date"  # 回別天＝還沒有這天
    assert parse_error({**good, "title": "115年10月27日 三大法人買賣超日報"})[0] == "date"
    position = T86_FIELDS.index("投信買賣超股數")
    for bad in ("--", "1.5", "abc", "", "1,23", None, True, 1.5):
        row = t86_row("2330", 1, 2, 3, 4)
        row[position] = bad
        assert parse_error(t86_payload(WED, [row]), codes=("2330",))[0] == "format", bad
    assert parse_error(t86_payload(WED, [t86_row("2330", 1, 2, 3, 4)[:12]]), codes=("2330",))[0] == "format"  # 列太短


def test_parse_t86_header_must_name_each_used_column_exactly_once():
    good = t86_payload(WED, ROWS)
    used = ("證券代號", "外陸資買賣超股數(不含外資自營商)", "投信買賣超股數", "自營商買賣超股數(自行買賣)", "自營商買賣超股數(避險)")
    for name in used:
        missing = [field for field in T86_FIELDS if field != name]
        renamed = ["外資" + field if field == name else field for field in T86_FIELDS]
        twice = [*T86_FIELDS, name]
        for fields in (missing, renamed, twice):
            assert parse_error({**good, "fields": fields}) == ("format", "header"), (name, fields)
    # 全形括號、多空白照樣認得（NFKC＋去空白）
    wide = [field.replace("(", "（").replace(")", "） ") for field in T86_FIELDS]
    assert src.parse_twse_t86({**good, "fields": wide}, expect_date=WED, codes=("2330",)).rows["2330"].trust == -56_000


def test_parse_t86_checks_only_the_rows_it_keeps():
    rows = [["9999", "名稱", "--", "x"], "2330", [], t86_row("2330", 1, 2, 3, 4), t86_row("2330", 1, 2, 3, 4)]
    assert src.parse_twse_t86(t86_payload(WED, rows), expect_date=WED, codes=("2330",)).rows == {"2330": (1, 2, 3, 4)}
    conflict = [t86_row("2330", 1, 2, 3, 4), t86_row("2330", 1, 2, 3, 5)]
    assert parse_error(t86_payload(WED, conflict), codes=("2330",))[0] == "conflict"
    assert parse_error(t86_payload(WED, [t86_row("1101", 1, 2, 3, 4)]), codes=("2330",))[0] == "too_few"
    assert parse_error(t86_payload(WED, []))[0] == "too_few"


# ── 抓取與端點 ──────────────────────────────────────────────────────────────────────
class OneShotHttp:
    def __init__(self, body) -> None:
        self.body = body
        self.calls: list[tuple[str, dict]] = []

    def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.body, Exception):
            raise self.body
        return SimpleNamespace(status=200, body=self.body, headers={}, url=url)


def json_bytes(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def test_fetch_twse_t86_url_budget_and_parse():
    fake, ledger = OneShotHttp(json_bytes(t86_payload(WED, ROWS))), object()
    assert src.fetch_twse_t86(WED, http=fake, ledger=ledger, codes=("2330",)).rows == {
        "2330": (1_234_000, -56_000, 1_000, -2_000)}
    assert len(fake.calls) == 1
    url, kwargs = fake.calls[0]
    parts = urlsplit(url)
    assert (parts.scheme, parts.hostname, parts.path) == ("https", "www.twse.com.tw", "/rwd/zh/fund/T86")
    assert {k: v[0] for k, v in parse_qs(parts.query).items()} == {
        "date": "20261028", "selectType": "ALLBUT0999", "response": "json"}
    endpoint = src.SPARE_ENDPOINTS["twse_t86"]
    assert kwargs == {"max_bytes": endpoint.max_bytes, "timeout": endpoint.timeout_s, "accept": ("json",), "ledger": ledger}
    assert url == src.t86_url(WED) and market_http.host_of(url) == "www.twse.com.tw"
    with pytest.raises(src.ParseError) as info:  # HTML 錯誤頁：不是 JSON
        src.fetch_twse_t86(WED, http=OneShotHttp(b"<html>busy</html>"))
    assert (info.value.kind, info.value.detail) == ("format", "json")
    with pytest.raises(market_http.FetchError) as error:  # FetchError 原樣往上丟
        src.fetch_twse_t86(WED, http=OneShotHttp(market_http.FetchError("moved")))
    assert error.value.kind == "moved"


def test_t86_endpoint_rule_is_16_30_twice_a_day_and_read_from_the_source_table(monkeypatch):
    endpoint = src.SPARE_ENDPOINTS["twse_t86"]
    assert endpoint.host == "www.twse.com.tw" and endpoint.host in market_http.ALLOWED_HOSTS
    assert (endpoint.earliest_tpe, endpoint.daily_max) == (time(16, 30), 2)
    assert endpoint.accept == ("json",) and endpoint.max_bytes == 2 * 1024 * 1024 and 0 < endpoint.timeout_s <= 30
    assert "twse_t86" not in src.ENDPOINTS  # 主要資料那張表（證交所各端點上限合計 13）不變
    assert tw._spare_rule("twse_t86") == tw._SPARE_RULES["twse_t86"] == (time(16, 30), 2)  # 備援表跟著來源表
    monkeypatch.setitem(src.SPARE_ENDPOINTS, "twse_t86", dataclasses.replace(endpoint, earliest_tpe=time(17, 0)))
    assert tw._spare_rule("twse_t86") == (time(17, 0), 2)  # 實際用的是來源表，不是備援


def _top_modules(name: str) -> set[str]:
    """頂層 import 的最上層模組名（from a.b import c 算 a；相對 import 算空字串，過不了下面的檢查）。"""
    names: set[str] = set()
    for node in ast.parse((HERE / name).read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    return names


@pytest.mark.parametrize("name", ["market_sources_tw.py", "market_pipeline_tw.py"])
def test_top_level_imports_are_only_the_standard_library_and_market_modules(name):
    """T86 不引入第三方：只看子集合，不鎖整份清單（2026-10-31 審查 R4：別批在這兩個檔加無關的 import 不該變紅）。"""
    names = _top_modules(name)
    assert names  # 抽得到東西（AST 的寫法沒失效）
    assert not {mod for mod in names if mod not in sys.stdlib_module_names and not mod.startswith("market_")}


def test_importing_the_t86_modules_registers_no_other_modules_tasks():
    """market_pipeline_* 一 import 就登記任務，順序靠 job 的 PIPELINES：market_sources_tw 不能帶進任何任務，market_pipeline_tw
    只能有自己的。在乾淨的子程序量實際登記的任務，間接 import 進來的也算。"""
    code = (
        "import json, market_tasks\n"
        "import market_sources_tw\n"
        "sources = [task.name for task in market_tasks.registered()]\n"
        "import market_pipeline_tw\n"
        "print(json.dumps({'sources': sources, 'pipeline': [task.name for task in market_tasks.registered()]}))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"sources": [], "pipeline": [task.name for task in tw.tasks()]}


# ── 排程用的環境（照 test_market_pipeline_tw 的做法：假的 http、暫存 Store、固定休市表、注入時間） ─────────────
HOLIDAYS_2026 = [
    {"date": "2026-01-01", "name": "開國紀念日", "note": "依規定放假1日。", "kind": "closed"},
    {"date": "2026-01-02", "name": "國曆新年開始交易日", "note": "國曆新年開始交易。", "kind": "trading"},
    {"date": "2026-10-09", "name": "國慶日", "note": "補假。", "kind": "closed"},
    {"date": "2026-10-26", "name": "光復節", "note": "補假。", "kind": "closed"},
]
HOLDINGS = {"source": "moneydj", "as_of": "2026-10-27", "count": 3, "dropped_count": 0, "unmapped_weight": 0.0,
            "rows": [["2317", None, 5.0], ["2330", None, 60.0], ["2454", None, 4.0]]}  # 權重大的先：2330、2317、2454
CLOSE_FIELDS = [
    "證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價",
    "漲跌(+/-)", "漲跌價差", "最後揭示買價", "最後揭示買量", "最後揭示賣價", "最後揭示賣量", "本益比",
]
UP = "<p style= color:red>+</p>"


def at(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> float:
    """台北牆上時間 → epoch 秒。"""
    return datetime(y, m, d, hh, mm, ss, tzinfo=TPE).timestamp()


def ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def day_of(url: str) -> date:
    params = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
    return datetime.strptime(params.get("date") or params["dayDate"], "%Y%m%d").date()


def allbut_payload(day: date, closes: dict[str, str]) -> dict:
    def close_row(code: str, close: str) -> list[str]:
        return [code, "名稱", "1,000", "1", "1", close, close, close, close, UP, "1.00", close, "1", close, "1", "0.00"]

    return {
        "stat": "OK", "date": ymd(day), "type": "ALLBUT0999",
        "tables": [
            {"title": f"{roc(day)} 價格指數(臺灣證券交易所)",
             "fields": ["指數", "收盤指數", "漲跌(+/-)", "漲跌點數", "漲跌百分比(%)", "特殊處理註記"],
             "data": [["發行量加權股價指數", "47,111.26", "<p style ='color:red'>+</p>", "141.39", "0.30", ""]]},
            {"title": f"{roc(day)} 大盤統計資訊", "fields": ["成交統計", "成交金額(元)", "成交股數(股)", "成交筆數"],
             "data": [["總計(1~15)", "993,814,830,090", "11,820,630,612", "4,986,590"]]},
            {"title": "漲跌證券數合計", "fields": ["類型", "整體市場", "股票"],
             "data": [["上漲(漲停)", "9,016(68)", "684(19)"], ["下跌(跌停)", "6,541(84)", "286(0)"],
                      ["持平", "1,190", "106"], ["未成交", "17,678", "5"], ["無比價", "3,528", "1"]]},
            {"title": f"{roc(day)} 每日收盤行情(全部(不含權證、牛熊證、可展延牛熊證))", "fields": CLOSE_FIELDS,
             "data": [close_row(code, close) for code, close in closes.items()]},
        ],
    }


def bfi_payload(day: date) -> dict:
    return {
        "stat": "OK", "date": ymd(day), "title": f"{roc(day)} 三大法人買賣金額統計表",
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


class Clock:
    """帳本與 Store 用的假時鐘；帳本等間隔時呼叫 sleep，時間往前走、測試不真的等。"""

    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += float(seconds)


class FakeHttp:
    """照 market_http.fetch 的樣子：先在帳本預扣，再依網址片段回假的內容（dict→JSON、bytes 原樣）或丟 FetchError。"""

    def __init__(self) -> None:
        self.routes: list[tuple[str, object]] = []
        self.calls: list[str] = []

    def route(self, needle: str, handler) -> None:
        self.routes.insert(0, (needle, handler))  # 後加的優先

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

    def days(self, needle: str) -> list[date]:
        return [day_of(url) for url in self.calls if needle in url]


def make_env(tmp_path: Path, *, holdings: bool = True) -> SimpleNamespace:
    root = tmp_path / "t86"
    root.mkdir()
    (root / "state").mkdir(mode=0o700)
    clock = Clock(at(2026, 1, 1))
    store = market_store.Store(root / "state" / "market_history.sqlite", now_fn=clock)
    store.put_obs("tw.holidays.2026", {"year": 2026, "rows": HOLIDAYS_2026, "unknown": []}, data_date=None)
    store.put_obs("tw.holidays.2025", {"year": 2025, "rows": [], "unknown": []}, data_date=None)
    if holdings:
        store.put_obs(tw.OBS_HOLDINGS, dict(HOLDINGS), data_date=HOLDINGS["as_of"])
    return SimpleNamespace(store=store, http=FakeHttp(), clock=clock, paths=market_paths.Paths.under(root))


@pytest.fixture
def env(tmp_path):
    environment = make_env(tmp_path)
    environment.http.route("/fund/T86", lambda url: t86_payload(day_of(url), ROWS))
    yield environment
    environment.store.close()


def make_ctx(env: SimpleNamespace, now: float) -> market_tasks.Context:
    env.clock.value = max(env.clock.value, now)
    return market_tasks.Context(
        store=env.store, http=env.http, ledger=env.store.ledger(now_fn=env.clock, sleep_fn=env.clock.sleep), now=now,
        paths=env.paths, deadline=1e9, us_cal=market_calendar.us_calendar(env.store),
        tw_cal=market_calendar.tw_calendar(env.store), clock=lambda: 0.0,
    )


TASKS = {task.name: task for task in tw.tasks()}


def run(ctx: market_tasks.Context, name: str) -> str:
    return market_tasks.run_task(ctx, TASKS[name])


def sched(env: SimpleNamespace, name: str) -> dict:
    return env.store.task_state(tw.STATE_PREFIX + name)


def set_used(env: SimpleNamespace, day: date, count: int) -> None:
    """證交所 www 今天已用的次數（記在休市表任務的排程紀錄裡；www_used 是 WWW_TASKS 各自紀錄的加總）。"""
    env.store.set_task_state(tw.STATE_PREFIX + tw.TASK_HOLIDAYS, {"day": day.isoformat(), "count": count})


def due(env: SimpleNamespace, when: float) -> bool:
    return tw.t86_plan(make_ctx(env, when)) is not None


# ── 登錄 ──────────────────────────────────────────────────────────────────────────
def test_t86_is_its_own_low_task_counted_in_the_www_budget():
    assert tw.TASK_T86 in tw.WWW_TASKS and tw.TASK_T86 not in tw.CORE_RESERVE
    assert (tw.T86_DAY_CEILING, tw.WWW_DAILY_CAP) == (9, 12)
    assert tw.T86_STOP_S == market_http.COOLDOWN_SECONDS == 7200.0
    with market_tasks.isolated_registry():
        tw.register_all()
        found = [task for task in market_tasks.registered() if task.name == tw.TASK_T86]
    assert len(found) == 1 and (found[0].group, found[0].market) == ("low", "tw")
    bfi = [task for task in tw.tasks() if task.name.startswith(tw.TASK_BFI)]
    assert len(bfi) == 2 and all(task.run is not found[0].run and task.due is not found[0].due for task in bfi)


# ── 目標日、obs 內容 ─────────────────────────────────────────────────────────────────
def test_t86_target_waits_for_16_30_and_the_obs_matches_the_spec(env):
    assert tw.t86_plan(make_ctx(env, at(2026, 10, 28, 16, 29))) == (TUE, "target")  # 16:30 前是前一個交易日
    assert tw.t86_plan(make_ctx(env, at(2026, 10, 28, 16, 30))) == (WED, "target")
    assert run(make_ctx(env, at(2026, 10, 28, 17, 0)), tw.TASK_T86) == "ok"
    obs = env.store.get_obs(tw.OBS_T86)
    assert obs["data_date"] == "2026-10-28" and obs["period"] is None and obs["fail_streak"] == 0
    url = "https://www.twse.com.tw/rwd/zh/fund/T86?date=20261028&selectType=ALLBUT0999&response=json"
    assert obs["payload"] == {
        "v": 1, "source": "TWSE T86", "source_url": url, "unit": "股", "trade_date": "2026-10-28",
        "constituents_as_of": "2026-10-27",
        "rows": [  # 0050 成分、權重大的先；T86 沒有的 2317 不列；total_net＝四欄相加（不用「三大法人買賣超股數」欄）
            {"code": "2330", "foreign_net": 1_234_000, "trust_net": -56_000, "dealer_self_net": 1_000,
             "dealer_hedge_net": -2_000, "total_net": 1_177_000},
            {"code": "2454", "foreign_net": -300, "trust_net": 0, "dealer_self_net": 0, "dealer_hedge_net": 5,
             "total_net": -295},
        ],
        "days": {"2026-10-28": {"2330": [1_234_000, -56_000, 1_000, -2_000], "2454": [-300, 0, 0, 5]}},
    }
    assert env.http.calls == [url]
    # 計數只經過 _sched／_note_try／_save：T86 自己的紀錄＋1，證交所 www 今天的用量跟著算
    assert sched(env, tw.TASK_T86)["count"] == 1
    assert tw.www_used(make_ctx(env, at(2026, 10, 28, 17, 1)), WED) == 1


def test_t86_not_yet_is_not_a_failure_and_the_same_day_waits_an_hour(env):
    published = {"wed": False}
    env.http.route("/fund/T86", lambda url: t86_payload(day_of(url), ROWS)
                   if day_of(url) != WED or published["wed"] else TOO_EARLY)
    assert run(make_ctx(env, at(2026, 10, 28, 16, 30)), tw.TASK_T86) == "not_yet"
    obs = env.store.get_obs(tw.OBS_T86)
    assert obs["payload"] is None and obs["fail_streak"] == 0 and obs["error"] is None
    # 同一天 1 小時內不重打；今天的 T 還沒到手，也不拿剩下那次去補別天（審查 R1-9）
    assert tw.t86_plan(make_ctx(env, at(2026, 10, 28, 16, 45))) is None
    assert run(make_ctx(env, at(2026, 10, 28, 17, 30)), tw.TASK_T86) == "not_yet"  # 1 小時後再試 T
    assert run(make_ctx(env, at(2026, 10, 28, 17, 45)), tw.TASK_T86) == "not_due"  # 每天 2 次
    published["wed"] = True
    assert run(make_ctx(env, at(2026, 10, 29, 0, 30)), tw.TASK_T86) == "ok"  # 隔天 16:30 前目標仍是 10/28
    assert env.http.days("/fund/T86") == [WED, WED, WED]
    assert env.store.get_obs(tw.OBS_T86)["payload"]["trade_date"] == "2026-10-28"


# ── 補抓：最近 5 個交易日、新到舊、跳過休市；days 只留這 5 天 ────────────────────────────────────
def test_t86_backfills_newest_first_skipping_closed_days_over_several_days(env):
    # 10/28 是暫定休市（ALLBUT 的排程紀錄）；10/26 是休市表上的補假
    env.store.set_task_state(tw.STATE_PREFIX + tw.TASK_ALLBUT, {"closures": {"2026-10-28": {"at": 1.0}}})
    assert run(make_ctx(env, at(2026, 10, 30, 17, 0)), tw.TASK_T86) == "ok"
    env.store.put_obs(tw.OBS_HOLDINGS, {**HOLDINGS, "as_of": "2026-10-30"}, data_date="2026-10-30")
    results = [run(make_ctx(env, at(*when)), tw.TASK_T86) for when in (
        (2026, 10, 30, 17, 15), (2026, 10, 30, 17, 30),  # 每天 2 次：剩下的明天補
        (2026, 10, 31, 9, 0), (2026, 10, 31, 9, 15), (2026, 11, 1, 9, 0))]
    assert results == ["ok", "not_due", "ok", "ok", "not_due"]  # 11/1：5 天裡能補的都補了
    assert env.http.days("/fund/T86") == [FRI, THU, TUE, LAST_FRI]
    payload = env.store.get_obs(tw.OBS_T86)["payload"]
    assert list(payload["days"]) == ["2026-10-30", "2026-10-29", "2026-10-27", "2026-10-23"]  # 新的在前
    # 補舊的日子不改最新那天：rows、trade_date、成分的 as_of 都還是 10/30 那次抓的
    assert payload["trade_date"] == "2026-10-30" and payload["constituents_as_of"] == "2026-10-27"
    assert [row["code"] for row in payload["rows"]] == ["2330", "2454"]
    assert payload["source_url"] == src.t86_url(FRI)


def test_t86_days_keep_only_the_last_5_sessions_and_drop_broken_entries(env):
    old = {iso: {"2330": [1, 1, 1, 1]} for iso in ("2026-10-27", "2026-10-23", "2026-10-22", "2026-10-21",
                                                    "2026-10-20", "2026-10-19")}
    old.update({"not-a-day": {"2330": [1, 1, 1, 1]}, "2026-10-16": "壞"})
    old["2026-10-22"] = {"2330": [1, 1, 1, 1], "2454": [1, "壞", 1, 1], "壞": [1, 1, 1, 1]}
    env.store.put_obs(tw.OBS_T86, {"v": 1, "trade_date": "2026-10-27", "rows": [], "days": old}, data_date="2026-10-27")
    assert run(make_ctx(env, at(2026, 10, 28, 17, 0)), tw.TASK_T86) == "ok"
    days = env.store.get_obs(tw.OBS_T86)["payload"]["days"]
    assert list(days) == ["2026-10-28", "2026-10-27", "2026-10-23", "2026-10-22", "2026-10-21"]  # 10/26 補假
    assert days["2026-10-22"] == {"2330": [1, 1, 1, 1]}
    assert tw.t86_plan(make_ctx(env, at(2026, 10, 28, 17, 15))) is None  # 5 天都齊了


def test_t86_needs_the_0050_constituents(tmp_path):
    bare = make_env(tmp_path, holdings=False)
    bare.http.route("/fund/T86", lambda url: t86_payload(day_of(url), ROWS))
    try:
        assert not due(bare, at(2026, 10, 28, 17, 0))
        assert run(make_ctx(bare, at(2026, 10, 28, 17, 0)), tw.TASK_T86) == "not_due" and bare.http.calls == []
        bare.store.put_obs(tw.OBS_HOLDINGS, dict(HOLDINGS), data_date=HOLDINGS["as_of"])
        assert due(bare, at(2026, 10, 28, 17, 15))
    finally:
        bare.store.close()


# ── 額度：只用剩下的、不吃 CORE_RESERVE、當天上限 9 ───────────────────────────────────────────
def test_t86_uses_only_spare_tries_and_leaves_the_core_reserve(env):
    # 週四早上：今天的 ALLBUT、BFI82U、TWT49U、融資都還沒到手 → 預留 2＋2＋1＋2；證交所 www 已經打了 5 次
    set_used(env, THU, 5)
    ctx = make_ctx(env, at(2026, 10, 29, 9, 0))
    held = tw.reserves(ctx, THU)
    assert held == {"tw.allbut": 2, "tw.bfi82u": 2, "tw.twt49u": 1, "tw.margin": 2}
    assert not tw.spare_ok(ctx, tw.TASK_T86, tw._sched(env.store, tw.TASK_T86, THU), THU, 2)  # 5＋7＝12
    assert tw.t86_plan(ctx) is None and run(ctx, tw.TASK_T86) == "not_due" and env.http.calls == []
    set_used(env, THU, 2)  # 2＋7＝9 < 12：spare_ok 為真，但 2＋7＋1＝10 > 9 → 還是不抓
    ctx = make_ctx(env, at(2026, 10, 29, 9, 15))
    assert tw.spare_ok(ctx, tw.TASK_T86, tw._sched(env.store, tw.TASK_T86, THU), THU, 2)
    assert tw.t86_plan(ctx) is None
    set_used(env, THU, 1)
    assert run(make_ctx(env, at(2026, 10, 29, 9, 30)), tw.TASK_T86) == "ok"  # 1＋7＋1＝9
    ctx = make_ctx(env, at(2026, 10, 29, 9, 45))
    assert tw.reserves(ctx, THU) == held  # 主要資料的預留沒被吃掉
    assert tw.www_used(ctx, THU) == 2 and tw.t86_plan(ctx) is None  # 2＋7＋1 > 9：補抓等主要資料到手


def seed_core_done(env: SimpleNamespace, day: date, *, margin: bool) -> None:
    """當天的 ALLBUT、BFI82U、TWT49U 都到手了（reserves 不再替它們保留）；margin＝融資也到手了。"""
    iso = day.isoformat()
    env.store.put_obs(tw.OBS_ALLBUT, {"days": {iso: {}}, "latest": iso}, data_date=iso)
    env.store.put_obs(tw.OBS_INST3, {"days": {iso: {}}, "latest": iso}, data_date=iso)
    env.store.add_coverage(tw.COVERAGE_TWT49U, date(2025, 1, 1), day)
    if margin:
        env.store.put_obs(tw.OBS_MARGIN, {"date": iso}, data_date=iso)


def test_t86_counts_what_the_core_data_still_holds_against_the_9(env):
    """協調者 10/31 的例子：已用 7、融資還沒抓、保留 2 → 7＋2＋1＝10 > 9 → 不抓（spare_ok 只看 12，會放行）。"""
    seed_core_done(env, THU, margin=False)
    set_used(env, THU, 7)
    ctx = make_ctx(env, at(2026, 10, 29, 22, 0))
    assert tw.reserves(ctx, THU) == {"tw.margin": 2}
    assert tw.spare_ok(ctx, tw.TASK_T86, tw._sched(env.store, tw.TASK_T86, THU), THU, 2)  # 7＋2＝9 < 12
    assert not tw.t86_room(ctx, THU) and tw.t86_plan(ctx) is None
    assert run(ctx, tw.TASK_T86) == "not_due" and env.http.calls == []


def test_t86_may_fetch_once_the_margin_is_in(env):
    seed_core_done(env, THU, margin=True)
    set_used(env, THU, 7)
    ctx = make_ctx(env, at(2026, 10, 29, 22, 0))
    assert tw.reserves(ctx, THU) == {} and tw.t86_room(ctx, THU)  # 7＋0＋1＝8
    assert run(ctx, tw.TASK_T86) == "ok" and env.http.days("/fund/T86") == [THU]
    set_used(env, THU, 8)  # 已用 8＋T86 這次 1＝9
    assert not tw.t86_room(make_ctx(env, at(2026, 10, 29, 22, 15)), THU)


# ── 整天跑一次：一般日合計 ≤ 9；bootstrap 日 T86 不抓 ────────────────────────────────────────
DAY_TASKS = (tw.TASK_HOLIDAYS, tw.TASK_ALLBUT, tw.TASK_BFI, tw.TASK_TWT49U, tw.TASK_MARGIN, tw.TASK_ALLBUT + ".backfill",
             tw.TASK_BFI + ".backfill", tw.TASK_TWT49U + ".bootstrap", tw.TASK_T86)  # 證交所 www 的全部任務


def margin_payload(day: date) -> dict:
    return {"stat": "OK", "date": ymd(day), "tables": [{
        "title": f"{roc(day)} 信用交易統計", "fields": ["項目", "買進", "賣出", "現金(券)償還", "前日餘額", "今日餘額"],
        "data": [["融資(交易單位)", "343,600", "318,542", "7,624", "9,521,230", "9,538,664"],
                 ["融券(交易單位)", "20,982", "30,004", "4,570", "229,150", "233,602"],
                 ["融資金額(仟元)", "28,656,955", "28,942,556", "536,222", "646,415,871", "645,594,048"]]}]}


def day_env(env: SimpleNamespace, day: date) -> None:
    """證交所 www 每個端點都有資料；休市表是前一天剛抓的（不會在這天重抓）。"""
    env.http.route("MI_INDEX", lambda url: allbut_payload(day_of(url), {"0050": "109.00", "2330": "2,420.00"}))
    env.http.route("BFI82U", lambda url: bfi_payload(day_of(url)))
    env.http.route("TWT49U", NO_DATA)  # 這段沒有除權息也算涵蓋
    env.http.route("MI_MARGN", lambda url: margin_payload(day_of(url)))
    env.clock.value = max(env.clock.value, at(day.year, day.month, day.day) - 86400)
    env.store.put_obs("tw.holidays.2026", {"year": 2026, "rows": HOLIDAYS_2026, "unknown": []}, data_date=None)
    env.store.put_obs("tw.holidays.2025", {"year": 2025, "rows": [], "unknown": []}, data_date=None)


def run_day(env: SimpleNamespace, day: date) -> dict[str, int]:
    """台北這一天每 15 分鐘跑一輪（照 run_collect 的群組順序），回當天每個證交所端點的請求數。"""
    start = len(env.http.calls)
    tasks = [TASKS[name] for name in DAY_TASKS]
    for quarter in range(96):
        market_tasks.run_collect(make_ctx(env, at(day.year, day.month, day.day) + quarter * 900.0), tasks)
    counts: dict[str, int] = {}
    for url in env.http.calls[start:]:
        name = urlsplit(url).path.rsplit("/", 1)[1]
        counts[name] = counts.get(name, 0) + 1
    return counts


def test_first_days_online_backfill_4_days_and_still_get_each_days_t_after_16_30(env):
    """T86 第一天上線（週四 10/29）：前一天以前的主要資料都齊，T86 的 5 天窗口裡另外 4 天（10/28、10/27、10/23、10/22）要補。
    審查 R1-9：每天先留一次給當天 16:30 後的 T，補抓只用剩下的，所以週四、週五的 T 當天就抓到；舊的日子早上補一天，
    週六補完（10/22 那時已經滑出窗口）。每天證交所合計 ≤ 9。"""
    day_env(env, THU)
    cal = make_ctx(env, at(2026, 10, 29)).tw_cal
    recent = tw.recent_sessions(cal, WED, 10)
    env.store.put_obs(tw.OBS_ALLBUT, {"days": {d.isoformat(): {} for d in recent}, "latest": "2026-10-28"},
                      data_date="2026-10-28")
    env.store.put_obs(tw.OBS_INST3, {"days": {d.isoformat(): {} for d in recent[:5]}, "latest": "2026-10-28"},
                      data_date="2026-10-28")
    env.store.add_coverage(tw.COVERAGE_TWT49U, date(2025, 1, 1), WED)
    env.store.put_obs(tw.OBS_MARGIN, {"date": "2026-10-28"}, data_date="2026-10-28")
    saturday = date(2026, 10, 31)
    fetched: dict[date, list[date]] = {}
    for day in (THU, FRI, saturday):
        start = len(env.http.calls)
        counts = run_day(env, day)
        fetched[day] = [day_of(url) for url in env.http.calls[start:] if "/fund/T86" in url]
        assert sum(counts.values()) <= tw.T86_DAY_CEILING, (day, counts)
        if day == THU:
            assert counts == {"T86": 2, "MI_INDEX": 1, "BFI82U": 1, "TWT49U": 1, "MI_MARGN": 1}
    assert fetched == {THU: [WED, THU], FRI: [TUE, FRI], saturday: [LAST_FRI]}
    payload = env.store.get_obs(tw.OBS_T86)["payload"]
    assert payload["trade_date"] == "2026-10-30"
    assert list(payload["days"]) == ["2026-10-30", "2026-10-29", "2026-10-28", "2026-10-27", "2026-10-23"]


def test_t86_does_not_fetch_on_a_bootstrap_day(env):
    """第一次上線的交易日：主要資料與補洞自己就把當天用到 9 以上，T86 整天都不抓。"""
    day_env(env, THU)
    counts = run_day(env, THU)
    assert "T86" not in counts
    assert sum(counts.values()) > tw.T86_DAY_CEILING  # base 自己就超過 9


def test_the_inst_flag_file_stops_t86(env, monkeypatch):
    flag = env.paths.state_dir / "market_inst.disabled"  # 和 F 軌的資金流、P 軌的卡片同一個旗標檔
    assert tw.INST_DISABLED_FLAG == flag.name
    flag.write_text("", encoding="utf-8")
    ctx = make_ctx(env, at(2026, 10, 28, 17, 0))
    assert tw.t86_disabled(ctx) and tw.t86_plan(ctx) is None
    assert run(ctx, tw.TASK_T86) == "not_due" and env.http.calls == []
    flag.unlink()
    assert due(env, at(2026, 10, 28, 17, 15))

    def broken(path):
        raise RuntimeError("cannot look")

    monkeypatch.setattr(market_paths, "flag_set", broken)  # 連旗標都看不了：當停用
    assert not due(env, at(2026, 10, 28, 17, 30))


@pytest.mark.parametrize("used, expect", [(8, True), (9, False)])
def test_t86_only_while_the_www_day_stays_within_9(env, used, expect):
    saturday = date(2026, 10, 31)  # 非交易日：沒有預留，spare_ok 只看 12
    set_used(env, saturday, used)
    ctx = make_ctx(env, at(2026, 10, 31, 10, 0))
    assert tw.spare_ok(ctx, tw.TASK_T86, tw._sched(env.store, tw.TASK_T86, saturday), saturday, 2)
    assert (tw.t86_plan(ctx) is not None) is expect
    assert run(ctx, tw.TASK_T86) == ("ok" if expect else "not_due")
    # 抓完一次就是 9：補抓也停（T86 自己每天 2 次還沒用完）
    assert tw.t86_plan(make_ctx(env, at(2026, 10, 31, 10, 15))) is None


# ── T86 的紀錄壞掉：當成已用滿，ALLBUT、BFI 照跑 ─────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["壞", None, -1, 1.5, True, [1], {"n": 1}])
def test_a_broken_www_count_reads_as_used_up(env, bad):
    env.store.set_task_state(tw.STATE_PREFIX + tw.TASK_T86, {"day": "2026-10-29", "count": bad})
    ctx = make_ctx(env, at(2026, 10, 29, 10, 0))
    assert tw.www_used(ctx, THU) == tw.WWW_DAILY_CAP
    assert tw.t86_plan(ctx) is None


def warnings_from_tw(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "market_pipeline_tw" and r.levelno == logging.WARNING]


def test_an_unreadable_t86_record_reads_as_used_up(env, monkeypatch, caplog):
    real = env.store.task_state

    def task_state(name):
        if name == tw.STATE_PREFIX + tw.TASK_T86:
            raise ValueError("broken row")
        return real(name)

    monkeypatch.setattr(env.store, "task_state", task_state)
    monkeypatch.setattr(tw, "_WWW_WARNED", set())
    with caplog.at_level(logging.WARNING, logger="market_pipeline_tw"):
        assert tw.www_used(make_ctx(env, at(2026, 10, 29, 10, 0)), THU) == tw.WWW_DAILY_CAP
    assert warnings_from_tw(caplog) == [f"market tw www budget treated as used up task={tw.TASK_T86} reason=unreadable"]
    assert "broken row" not in caplog.text


def test_www_used_logs_one_line_when_it_stops_on_a_bad_record(env, monkeypatch, caplog):
    """審查 R2-8：壞紀錄讓補洞、休市表、T86 一起停時要留一行 log（只帶任務名與原因，不印紀錄內容），同一個任務當天只印一次。"""
    monkeypatch.setattr(tw, "_WWW_WARNED", set())
    env.store.set_task_state(tw.STATE_PREFIX + tw.TASK_T86, {"day": "2026-10-29", "count": "壞"})
    ctx = make_ctx(env, at(2026, 10, 29, 10, 0))
    with caplog.at_level(logging.WARNING, logger="market_pipeline_tw"):
        assert tw.www_used(ctx, THU) == tw.WWW_DAILY_CAP
        assert tw.t86_plan(ctx) is None and tw.allbut_backfill(ctx) is None  # 再問幾次都一樣，不重複印
    assert warnings_from_tw(caplog) == [f"market tw www budget treated as used up task={tw.TASK_T86} reason=bad_count"]
    assert "壞" not in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="market_pipeline_tw"):
        set_used(env, THU, 3)
        env.store.set_task_state(tw.STATE_PREFIX + tw.TASK_T86, {"day": "2026-10-29", "count": 1})
        assert tw.www_used(make_ctx(env, at(2026, 10, 29, 10, 15)), THU) == 4  # 好的紀錄不印
    assert warnings_from_tw(caplog) == []


def test_broken_t86_state_does_not_stop_allbut_and_bfi(env):
    env.store.set_task_state(tw.STATE_PREFIX + tw.TASK_T86,
                             {"day": "2026-10-29", "count": "壞", "tries": "壞", "stop_until": "壞"})
    env.store.put_obs(tw.OBS_T86, {"days": "壞", "rows": "壞"}, data_date="2026-10-28")
    env.http.route("MI_INDEX", lambda url: allbut_payload(day_of(url), {"0050": "109.00", "2330": "2,420.00"}))
    env.http.route("BFI82U", lambda url: bfi_payload(day_of(url)))
    ctx = make_ctx(env, at(2026, 10, 29, 15, 31))
    assert tw.www_used(ctx, THU) == tw.WWW_DAILY_CAP  # 壞值當成已用滿，不丟例外
    assert run(ctx, tw.TASK_ALLBUT) == "ok" and run(ctx, tw.TASK_BFI) == "ok"
    for name in (tw.TASK_ALLBUT + ".backfill", tw.TASK_BFI + ".backfill", tw.TASK_TWT49U + ".bootstrap",
                 tw.TASK_HOLIDAYS, tw.TASK_T86):
        assert run(ctx, name) == "not_due", name  # 補洞與 T86 停下；不是 unexpected
    assert env.http.days("MI_INDEX") == [THU] and env.http.days("BFI82U") == [THU]
    assert not any("/fund/T86" in url for url in env.http.calls)
    assert THU.isoformat() in env.store.get_obs(tw.OBS_INST3)["payload"]["days"]


# ── /股票 打 T86 的時段（協調者 10/31） ───────────────────────────────────────────────────────
@pytest.mark.parametrize("hh, mm, ss, expect", [
    (5, 29, 59, True), (5, 30, 0, False), (5, 50, 0, False), (6, 14, 59, False), (6, 15, 0, True)])
def test_t86_keeps_out_of_the_stock_picks_window(env, hh, mm, ss, expect):
    assert due(env, at(2026, 10, 29, hh, mm, ss)) is expect
    assert tw.t86_quiet(make_ctx(env, at(2026, 10, 29)).tw_cal, datetime(2026, 10, 29, hh, mm, ss, tzinfo=TPE)) is (
        not expect)


@pytest.mark.parametrize("when, expect", [
    ((2026, 11, 2, 0, 30), False),  # 11/2（週一）是 11 月第一個交易日：/股票 那天延到 08:00–10:00
    ((2026, 11, 2, 9, 59), False),
    ((2026, 11, 2, 10, 0), True),
    ((2026, 11, 3, 9, 0), True),  # 第二個交易日照常
    ((2026, 11, 1, 9, 0), True),  # 11/1 週日不是交易日
    ((2026, 1, 2, 9, 0), False),  # 1/1 放假：1/2 才是 1 月第一個交易日
    ((2026, 1, 5, 9, 0), True),
])
def test_t86_waits_until_10_00_on_the_first_session_of_a_month(env, when, expect):
    assert due(env, at(*when)) is expect


# ── 403／429／非 JSON／轉址：停 2 小時，不逐日補抓一直打（協調者 10/31） ────────────────────────────
@pytest.mark.parametrize("answer, kind, framework_kind", [
    pytest.param(market_http.FetchError("blocked"), "blocked", "blocked", id="403"),
    pytest.param(market_http.FetchError("rate_limited"), "rate_limited", "rate_limited", id="429"),
    pytest.param(market_http.FetchError("moved"), "moved", "moved", id="3xx"),
    pytest.param(market_http.FetchError("content_type"), "content_type", "content_type", id="html_content_type"),
    pytest.param(market_http.FetchError("not_json"), "not_json", "not_json", id="not_json"),
    pytest.param(b"<!DOCTYPE html><html><body>busy</body></html>", "not_json", "format", id="html_body"),
])
def test_t86_stops_for_two_hours_on_block_signals(env, answer, kind, framework_kind):
    env.http.route("/fund/T86", answer)
    assert run(make_ctx(env, at(2026, 10, 29, 17, 0)), tw.TASK_T86) == framework_kind
    state = sched(env, tw.TASK_T86)
    assert state["stop_kind"] == kind and state["stop_until"] == at(2026, 10, 29, 19, 0)
    assert env.store.get_obs(tw.OBS_T86)["error"] == framework_kind
    for hh, mm in ((17, 15), (17, 59), (18, 59)):  # 10/28、10/27… 都還沒有，也不改打別天
        ctx = make_ctx(env, at(2026, 10, 29, hh, mm))
        assert tw.t86_plan(ctx) is None
        assert run(ctx, tw.TASK_T86) in ("not_due", "backoff")
    assert len(env.http.calls) == 1
    env.http.route("/fund/T86", lambda url: t86_payload(day_of(url), ROWS))
    assert run(make_ctx(env, at(2026, 10, 29, 19, 0)), tw.TASK_T86) == "ok"
    assert env.http.days("/fund/T86") == [THU, THU]


def test_t86_other_errors_do_not_stop_the_whole_task(env):
    env.http.route("/fund/T86", market_http.FetchError("timeout"))
    assert run(make_ctx(env, at(2026, 10, 29, 17, 0)), tw.TASK_T86) == "timeout"
    assert "stop_until" not in sched(env, tw.TASK_T86)
    # 同一天 1 小時內不重打；今天的 T 還沒到手，也不改去補前一天（R1-9）；1 小時後再試 T
    assert tw.t86_plan(make_ctx(env, at(2026, 10, 29, 17, 15))) is None
    assert tw.t86_plan(make_ctx(env, at(2026, 10, 29, 18, 0))) == (THU, "target")


# ── 審查 R2-3：請求送出後，解析丟出不是 ParseError 的例外，也要記一次 ───────────────────────────────
def deep_json(url: str) -> bytes:
    return b"[" * 200_000 + b"]" * 200_000  # json 丟 RecursionError


def huge_number(url: str) -> dict:
    row = t86_row("2330", 1, 2, 3, 4)
    row[T86_FIELDS.index("投信買賣超股數")] = "1" * 5_000  # 超過 Python 的 4,300 位數上限：int() 丟一般的 ValueError
    return t86_payload(day_of(url), [row])


@pytest.mark.parametrize("answer, kind", [(deep_json, "unexpected:RecursionError"), (huge_number, "unexpected:ValueError")])
def test_t86_counts_a_try_even_when_the_parser_raises_something_else(env, answer, kind):
    env.http.route("/fund/T86", answer)
    assert run(make_ctx(env, at(2026, 10, 29, 17, 0)), tw.TASK_T86) == kind
    state = sched(env, tw.TASK_T86)
    assert state["count"] == 1 and state["tries"]["2026-10-29"]["last"] == at(2026, 10, 29, 17, 0)
    assert tw.www_used(make_ctx(env, at(2026, 10, 29, 17, 1)), THU) == 1  # 證交所的當天用量也算到
    assert tw.t86_plan(make_ctx(env, at(2026, 10, 29, 17, 30))) is None  # 1 小時內不重打
    assert run(make_ctx(env, at(2026, 10, 29, 18, 0)), tw.TASK_T86) == kind
    assert sched(env, tw.TASK_T86)["count"] == 2
    assert run(make_ctx(env, at(2026, 10, 29, 19, 30)), tw.TASK_T86) == "not_due"  # 每天 2 次照算
    assert len(env.http.calls) == 2
