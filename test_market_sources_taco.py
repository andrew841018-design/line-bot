"""market_sources_taco（川普壓力指數，MacroMargin，2026-10-24）的測試：解析要嚴格、來源的任何文字都不能漏出來。

全部離線、不連網：頁面是 test_market_taco_fixture 組的（格式照保存的真實回應；fixture 的來源與限制寫在那個檔案），
傳輸層是假的或 market_http.fetch＋換掉的 safe_fetch（測 403／429、型別、大小上限）。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import market_calc
import market_http
import market_sources_taco as taco
import market_store
from test_market_taco_fixture import (
    CLOUDFLARE, HEAD, HISTORY, ROWS_AFTER, ROWS_BEFORE, assert_no_source_text, data_row, flight_of, float_edge_case,
    imported_modules, make_bytes, make_data, make_page, page_of, push,
)

HERE = Path(__file__).resolve().parent
MIB = 1024 * 1024


def parse(page) -> taco.TacoSeries:
    """dict（data 物件）→ 組成頁面再解析；bytes／str 直接解析；其他東西原樣交給 parse_taco。"""
    return taco.parse_taco(make_bytes(page) if isinstance(page, dict) else page)


def parse_error(page) -> tuple[str, str]:
    with pytest.raises(taco.ParseError) as caught:
        parse(page)
    error = caught.value
    assert re.fullmatch(r"[A-Za-z_]{1,24}", error.detail), error.detail  # 框架的 log 只收這種
    return error.kind, error.detail


# ── 解析：保存的頁面長相 ────────────────────────────────────────────────────────


def test_fixture_is_the_saved_series():
    """名字是歷史遺留：fixture 的數列現在是合成的（2026-10-28 F1-11，審查 gp2 Q1），這裡釘住其他測試依賴的形狀。"""
    assert len(HISTORY) == 394 and HISTORY[0] == (date(2025, 3, 26), 2.7065) and HISTORY[-1] == (date(2026, 10, 22), 9.0005)
    values = [v for _day, v in HISTORY]
    assert min(values) == -4.9868 and HISTORY[values.index(min(values))][0] == date(2025, 5, 20)
    assert max(values) == 17.3261 and HISTORY[values.index(max(values))][0] == date(2026, 3, 27)
    assert all(a[0] < b[0] for a, b in zip(HISTORY, HISTORY[1:]))


def test_parses_the_numbers_of_the_saved_page():
    series = parse(make_bytes())
    assert series.method == market_calc.TACO_METHOD_V1 == "persistent-pressure-new4-v1"
    assert series.value == 9.0005 and series.as_of == date(2026, 10, 22)
    assert series.change20d == -3.6 and series.status_key == "high"
    assert series.history == HISTORY
    assert_no_source_text(series)


def test_accepts_text_as_well_as_bytes_and_a_bom():
    assert parse(make_page()) == parse(make_bytes()) == parse(b"\xef\xbb\xbf" + make_bytes())


@pytest.mark.parametrize("size", [997, 4096, 20000])
def test_a_row_cut_into_several_pushes_parses_the_same(size):
    """合成：同一份 flight 文字在任意位置切開（含 JSON 字串或數字的中間）。"""
    page = make_page(split=size)
    assert page.count("self.__next_f.push([1,") >= 3  # 整份 flight 文字約 45 KB
    assert parse(page) == parse(make_page())


def test_a_cut_between_a_backslash_and_its_quote_inside_the_row_parses_the_same():
    text = flight_of(make_data())
    cut = text.index('\\"') + 1  # flight 層的跳脫 \" 的反斜線和引號之間
    assert text[cut - 1] == "\\" and text[cut] == '"'
    assert parse(page_of([text[:cut], text[cut:]])) == parse(make_page())
    # 切在數字中間、行首 id 和冒號之間
    cut = text.index("9.0005") + 3
    assert parse(page_of([text[:cut], text[cut:]])) == parse(make_page())
    cut = text.index("a:[") + 1
    assert parse(page_of([text[:cut], text[cut:]])) == parse(make_page())


def test_pushes_that_are_not_data_chunks_are_skipped():
    noise = ('<script>self.__next_f.push([0])</script><script>self.__next_f.push([3,"binary"])</script>'
             '<script>self.__next_f.push(not json)</script><script>self.__next_f.push([1,5])</script>'
             '<script>self.__next_f.push([true,"x"])</script>')
    page = make_page().replace("</body>", noise + "</body>")
    assert parse(page) == parse(make_page())


def test_only_a_push_typed_with_the_integer_1_carries_flight_data():
    """React flight 的 push 型別：0＝啟動、1＝資料、2／3＝其他。`true == 1` 在 Python 成立，但不是資料 chunk。"""
    row = data_row(make_data())
    for kind in ("true", "0", "2", "3", "1.0", '"1"'):
        quoted = json.dumps(row, ensure_ascii=False)
        page = HEAD + push(ROWS_BEFORE[0]) + f"<script>self.__next_f.push([{kind},{quoted}])</script></body></html>"
        assert parse_error(page) == ("format", "no_line"), kind
    good = HEAD + push(ROWS_BEFORE[0]) + f"<script>self.__next_f.push([1,{json.dumps(row, ensure_ascii=False)}])</script></body></html>"
    assert parse(good) == parse(make_page())


def test_identical_duplicate_rows_are_one_series_but_different_ones_conflict():
    data = make_data()
    twice = page_of((*ROWS_BEFORE, data_row(data, "a"), data_row(data, "b"), *ROWS_AFTER))
    assert parse(twice) == parse(make_page())
    other = make_data(index={"value": 9.0105})
    other["history"][-1]["value"] = 9.0105
    kind, detail = parse_error(page_of((*ROWS_BEFORE, data_row(data, "a"), data_row(other, "b"), *ROWS_AFTER)))
    assert (kind, detail) == ("conflict", "dup_line")


def test_the_only_strings_in_the_result_are_the_version_and_the_status_key():
    series = parse(make_bytes())
    strings = [series.method, series.status_key]
    assert all(re.fullmatch(r"[A-Za-z0-9._-]{1,40}", s) for s in strings)
    assert {type(v).__name__ for v in (series.value, series.change20d)} == {"float"}
    assert_no_source_text(series, strings)


# ── 解析：頁面不是預期的樣子 ────────────────────────────────────────────────────

def test_a_cloudflare_challenge_page_is_a_format_error():
    assert parse_error(CLOUDFLARE) == ("format", "no_push")
    assert parse_error(b"") == ("format", "no_push")
    assert parse_error("<html></html>") == ("format", "no_push")


def test_pushes_without_the_data_row_are_a_format_error():
    page = page_of((*ROWS_BEFORE, *ROWS_AFTER))
    assert parse_error(page) == ("format", "no_line")
    only_bootstrap = HEAD + "</body></html>"
    assert parse_error(only_bootstrap) == ("format", "no_push")


def test_the_marker_line_must_be_a_json_row_with_index_and_history():
    broken = flight_of(make_data())
    cut = broken.index("loadError")
    assert parse_error(page_of([broken[:cut]])) == ("format", "json")  # 被截斷
    no_data = page_of((*ROWS_BEFORE, 'a:["$","div",null,{"methodologyVersion":"x"}]\n', *ROWS_AFTER))
    assert parse_error(no_data) == ("format", "no_data")
    not_a_row = page_of((*ROWS_BEFORE, '"methodologyVersion" is mentioned here\n', *ROWS_AFTER))
    assert parse_error(not_a_row) == ("format", "no_row")
    not_hex = page_of((*ROWS_BEFORE, 'xyz:["methodologyVersion"]\n', *ROWS_AFTER))  # flight 的 row id 是十六進位
    assert parse_error(not_hex) == ("format", "no_row")
    tree = ["$", "x", None, {"methodologyVersion": "x", "first": {"index": {}, "history": []},
                            "second": {"index": {}, "history": []}}]
    row = "a:" + json.dumps(tree) + "\n"  # 同一行有兩個「有 index 與 history 的 dict」
    assert parse_error(page_of((*ROWS_BEFORE, row, *ROWS_AFTER))) == ("format", "dup_data")


def test_a_decoy_line_does_not_hide_the_real_data_row():
    """別的行也提到 methodologyVersion（例如 T 文字行）：只要有一行解得出資料就用它。"""
    page = page_of((*ROWS_BEFORE, '5:T2a,"methodologyVersion" appears in a text row\n', data_row(make_data()), *ROWS_AFTER))
    assert parse(page) == parse(make_page())


def test_too_many_pushes_and_oversized_pages_are_refused():
    junk = "<script>self.__next_f.push([0])</script>" * (taco.MAX_PUSHES + 1)
    assert parse_error(HEAD + junk + "</body>") == ("format", "too_many_push")
    assert parse_error(b"x" * (taco.MAX_BYTES + 1)) == ("format", "body_size")
    assert parse_error("x" * (taco.MAX_BYTES + 1)) == ("format", "body_size")
    assert parse_error(b"\xff\xfe\xfa") == ("format", "encoding")
    assert parse_error(12345) == ("format", "body")
    assert parse_error(None) == ("format", "body")


def test_a_row_with_too_many_json_nodes_is_refused_quickly():
    """找資料時最多走 MAX_NODES 個 JSON 節點（4 MiB 的頁面也不能讓任務跑太久）。"""
    import time

    body = json.dumps({"methodologyVersion": "x", "x": [[1] * 10 for _ in range(taco.MAX_NODES // 10 + 10_000)]})
    page = page_of([("a:" + body + "\n")])
    assert len(page) < taco.MAX_BYTES
    started = time.perf_counter()
    assert parse_error(page) == ("format", "too_big_json")
    assert time.perf_counter() - started < 5.0


def test_deeply_nested_json_is_a_format_error_not_a_crash():
    row = "a:" + "[" * 60_000 + '"methodologyVersion"' + "]" * 60_000 + "\n"  # 3.13 的 JSON 解析器在 5 萬層會丟 RecursionError
    assert parse_error(page_of((*ROWS_BEFORE, row, *ROWS_AFTER))) == ("format", "json")  # RecursionError 也接住
    push_depth = HEAD + "<script>self.__next_f.push(" + "[" * 60_000 + "]" * 60_000 + ")</script></body></html>"
    assert parse_error(push_depth) == ("format", "no_push")
    deep = "a:" + "[" * 100 + '{"methodologyVersion":"x","index":{},"history":[]}' + "]" * 100 + "\n"
    assert parse_error(page_of((*ROWS_BEFORE, deep, *ROWS_AFTER))) == ("format", "no_data")  # 超過深度上限：找不到
    shallow = "a:" + "[" * 10 + '{"methodologyVersion":"x","index":{},"history":[]}' + "]" * 10 + "\n"
    assert parse_error(page_of((*ROWS_BEFORE, shallow, *ROWS_AFTER))) == ("format", "value")  # 找得到（淺層），但 index 是空的


# ── 解析：每個欄位的反例 ────────────────────────────────────────────────────────


def with_index(**changes) -> dict:
    return make_data(index=changes)


def with_history(rows) -> dict:
    data = make_data()
    data["history"] = rows
    return data


def history_rows(count=394, *, start=0):
    return [{"date": day.isoformat(), "value": value} for day, value in HISTORY[start:start + count]]


BAD_PAGES = {
    # 方法版本
    "method_chars": (lambda: make_data(version="v1; drop table"), ("format", "method")),
    "method_empty": (lambda: make_data(version=""), ("format", "method")),
    "method_long": (lambda: make_data(version="a" * 41), ("format", "method")),
    "method_not_str": (lambda: make_data(version=1), ("format", "method")),
    "method_cjk": (lambda: make_data(version="持续压力新4"), ("format", "method")),
    # index
    "index_not_dict": (lambda: make_data(index=None) | {"index": "x"}, ("format", "index")),
    "value_missing": (lambda: with_index(value=None), ("format", "value")),
    "value_string": (lambda: with_index(value="9.0005"), ("format", "value")),
    "value_bool": (lambda: with_index(value=True), ("format", "value")),
    "value_too_big": (lambda: with_index(value=100.5), ("format", "value")),
    "value_too_small": (lambda: with_index(value=-100.5), ("format", "value")),
    "value_huge_int": (lambda: with_index(value=10 ** 400), ("format", "value")),
    "as_of_missing": (lambda: with_index(asOfDate=None), ("format", "as_of")),
    "as_of_number": (lambda: with_index(asOfDate=20261022), ("format", "as_of")),
    "as_of_compact": (lambda: with_index(asOfDate="20261022"), ("format", "as_of")),
    "as_of_not_a_day": (lambda: with_index(asOfDate="2026-02-30"), ("format", "as_of")),
    "as_of_with_time": (lambda: with_index(asOfDate="2026-10-22T00:00:00"), ("format", "as_of")),
    "as_of_other_digits": (lambda: with_index(asOfDate="٢٠٢٦-١٠-٢٢"), ("format", "as_of")),
    "as_of_out_of_range": (lambda: with_index(asOfDate="1999-10-22"), ("format", "as_of")),
    "change_string": (lambda: with_index(change20d="-3.6"), ("format", "change")),
    "change_bool": (lambda: with_index(change20d=False), ("format", "change")),
    "change_too_big": (lambda: with_index(change20d=200.5), ("format", "change")),
    # status.key 只收 [a-z_]{1,16}
    "status_upper": (lambda: with_index(status={"key": "High"}), ("format", "status")),
    "status_space": (lambda: with_index(status={"key": "high pressure"}), ("format", "status")),
    "status_cjk": (lambda: with_index(status={"key": "高压"}), ("format", "status")),
    "status_long": (lambda: with_index(status={"key": "a" * 17}), ("format", "status")),
    "status_empty": (lambda: with_index(status={"key": ""}), ("format", "status")),
    "status_digit": (lambda: with_index(status={"key": "level2"}), ("format", "status")),
    "status_injection": (lambda: with_index(status={"key": "high\n5:evil"}), ("format", "status")),
    "status_number": (lambda: with_index(status={"key": 5}), ("format", "status")),
    "status_not_dict": (lambda: with_index(status=["high"]), ("format", "status")),
    # history
    "history_not_list": (lambda: with_history({"2026-10-22": 9.0005}), ("format", "history_count")),
    "history_empty": (lambda: with_history([]), ("format", "history_count")),
    "history_59": (lambda: with_history(history_rows(59, start=335)), ("format", "history_count")),
    "history_too_long": (lambda: with_history([{"date": date.fromordinal(740000 + i).isoformat(), "value": 1.0}
                                               for i in range(taco.MAX_HISTORY + 1)]), ("format", "history_count")),
    "history_item_not_dict": (lambda: with_history(history_rows()[:-1] + [["2026-10-22", 9.0005]]), ("format", "history_row")),
    "history_value_missing": (lambda: with_history(history_rows()[:-1] + [{"date": "2026-10-22"}]), ("format", "history_row")),
    "history_value_string": (lambda: with_history(history_rows()[:100] + [{"date": "2026-10-22", "value": "9"}]), ("format", "history_row")),
    "history_date_bad": (lambda: with_history(history_rows()[:-1] + [{"date": "2026-13-01", "value": 9.0}]), ("format", "history_row")),
    "history_date_missing": (lambda: with_history(history_rows()[:-1] + [{"value": 9.0}]), ("format", "history_row")),
    "history_value_big": (lambda: with_history(history_rows()[:200] + [{"date": "2026-10-22", "value": 150.0}]), ("format", "history_row")),
    "history_duplicate_date": (lambda: with_history(history_rows() + [{"date": "2026-10-22", "value": 9.0005}]), ("format", "history_order")),
    "history_goes_back": (lambda: with_history(history_rows() + [{"date": "2026-10-21", "value": 9.0005}]), ("format", "history_order")),
    "history_reversed": (lambda: with_history(list(reversed(history_rows()))), ("format", "history_order")),
    # 最後一筆要對得上 index
    "last_date_differs": (lambda: with_index(asOfDate="2026-10-21"), ("conflict", "last_row")),
    "last_date_later": (lambda: with_index(asOfDate="2026-10-23"), ("conflict", "last_row")),
    "last_value_differs": (lambda: with_index(value=9.0206), ("conflict", "last_row")),
    "last_value_slightly_more": (lambda: with_index(value=9.0106), ("conflict", "last_row")),
}


@pytest.mark.parametrize("name", list(BAD_PAGES))
def test_bad_fields_are_refused_with_a_listed_kind(name):
    build, expected = BAD_PAGES[name]
    assert parse_error(make_bytes(build())) == expected


def test_a_missing_version_is_a_format_error():
    data = make_data()
    del data["methodologyVersion"]
    # 沒有 methodologyVersion 這個鍵＝連那一行都找不到
    assert parse_error(make_bytes(data)) == ("format", "no_line")


def test_non_finite_numbers_are_refused_everywhere():
    for name, replace, expected in (
        ("nan_value", ("9.0005", "NaN"), "json"),  # NaN／Infinity 這種字面值連 JSON 都不收
        ("inf_value", ("9.0005", "Infinity"), "json"),
        ("neg_inf_change", ("-3.6", "-Infinity"), "json"),
        ("nan_history", ('"value":4.4609', '"value":NaN'), "json"),
        ("overflow_value", ("9.0005", "1e999"), "value"),  # 1e999 解成 inf：有限檢查擋下
        ("overflow_history", ('"value":2.7065', '"value":1e999'), "history_row"),
        ("overflow_change", ("-3.6", "-1e999"), "change"),
    ):
        text = flight_of(make_data()).replace(*replace, 1)
        assert parse_error(page_of([text])) == ("format", expected), name


def test_values_at_the_edges_are_accepted():
    # change20d 要和歷史對得上（2026-10-28 F1-7）：極端的 +200 需要倒數第 21 筆是 −100
    edge = make_data(index={"value": 100.0, "change20d": 200.0, "status": None})
    edge["history"][-1]["value"] = 100.0
    edge["history"][-21]["value"] = -100.0
    series = parse(make_bytes(edge))
    assert series.value == 100.0 and series.change20d == 200.0 and series.status_key is None
    assert parse(make_bytes(with_history(history_rows(60, start=334)))).history == HISTORY[334:394]  # 剛好 60 筆
    # last row 與 index.value 差 0.01 以內
    close = make_data(index={"value": 9.0105})
    assert parse(make_bytes(close)).value == 9.0105


def test_a_difference_of_exactly_one_hundredth_is_accepted_despite_float_error():
    count, value = float_edge_case()
    rows = HISTORY[:count]
    assert abs(value - rows[-1][1]) > 0.01  # 這組數字的浮點差確實略大於 0.01
    series = parse(make_data(index={"asOfDate": rows[-1][0].isoformat(), "value": value}, history=rows))
    assert series.value == value and series.history == rows


def test_optional_fields_may_be_missing():
    data = make_data()
    del data["index"]["change20d"]
    del data["index"]["status"]
    series = parse(make_bytes(data))
    assert series.change20d is None and series.status_key is None
    assert parse(make_bytes(make_data(index={"change20d": None, "status": {"label": "高压"}}))).status_key is None


def test_a_changed_but_well_formed_method_version_is_kept_as_is():
    series = parse(make_bytes(make_data(version="persistent-pressure-new5-v2")))
    assert series.method == "persistent-pressure-new5-v2" != market_calc.TACO_METHOD_V1


def test_errors_never_carry_source_text():
    for name, (build, expected) in BAD_PAGES.items():
        page = make_bytes(build())
        try:
            parse(page)
        except taco.ParseError as error:
            assert_no_source_text(str(error), repr(error), error.kind, error.detail, error.args)
        except Exception as error:  # noqa: BLE001
            raise AssertionError(f"{name}: {type(error).__name__}") from None
    assert_no_source_text(str(taco.ParseError("format", "no_line")))


# ── 抓取 ────────────────────────────────────────────────────────────────────────


class FakeHttp:
    """照 market_http.fetch 的簽名；記下每次的參數。"""

    def __init__(self, body=None):
        self.body = make_bytes() if body is None else body
        self.calls = []

    def fetch(self, url, *, max_bytes, timeout=15.0, accept=("json",), headers=None, ledger=None):
        self.calls.append(SimpleNamespace(url=url, max_bytes=max_bytes, timeout=timeout, accept=accept, headers=headers,
                                          ledger=ledger))
        return SimpleNamespace(status=200, body=self.body, headers={}, url=url)


def test_fetch_uses_the_one_fixed_url_with_the_budget_of_the_endpoint():
    http, ledger = FakeHttp(), object()
    series = taco.fetch_taco(http=http, ledger=ledger)
    (call,) = http.calls
    assert call.url == taco.URL == "https://ocmacro.com/dashboard/trump"
    assert call.max_bytes == 4 * MIB and call.accept == ("text/html",) and call.timeout == 30.0
    assert call.ledger is ledger and call.headers is None
    assert series == parse(make_bytes())


def test_the_endpoint_table_matches_the_host_rules():
    endpoint = taco.ENDPOINTS["taco"]
    assert endpoint.host == taco.HOST == "ocmacro.com" and endpoint.host in market_http.ALLOWED_HOSTS
    assert market_http.host_of(taco.URL) == "ocmacro.com"
    rule = market_store.HOST_RULES["ocmacro.com"]
    assert (rule.min_interval_s, rule.daily_cap, rule.bucket) == (10.0, 8, "")
    assert endpoint.per_day <= rule.daily_cap == 8  # 帳本的每日上限留 2 次餘裕
    from urllib.parse import urlsplit

    parts = urlsplit(taco.URL)
    assert (parts.scheme, parts.path, parts.query, parts.fragment) == ("https", "/dashboard/trump", "", "")
    assert "/api/" not in taco.URL and "/data/" not in taco.URL  # robots.txt 只 Allow /dashboard/trump


def test_a_body_that_is_not_bytes_or_is_over_the_limit_is_a_format_error():
    for body in ("text", None, 5):
        http = FakeHttp()
        http.body = body
        with pytest.raises(taco.ParseError) as caught:
            taco.fetch_taco(http=http)
        assert (caught.value.kind, caught.value.detail) == ("format", "body")
    http = FakeHttp(b"x" * (taco.MAX_BYTES + 1))
    with pytest.raises(taco.ParseError) as caught:
        taco.fetch_taco(http=http)
    assert (caught.value.kind, caught.value.detail) == ("format", "body_size")


class Ledger:
    def __init__(self):
        self.reserved, self.cooled = [], []

    def reserve(self, host):
        self.reserved.append(host)

    def cooldown(self, host, seconds):
        self.cooled.append((host, seconds))


def fake_safe_fetch(monkeypatch, *, status=200, content_type="text/html; charset=utf-8", body=None, error=None):
    import safe_fetch

    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if error is not None:
            raise error
        return SimpleNamespace(status_code=status, headers={"Content-Type": content_type},
                               content=make_bytes() if body is None else body)

    monkeypatch.setattr(safe_fetch, "fetch", fetch)
    return calls


def test_the_real_transport_gets_the_page_and_passes_the_byte_limit(monkeypatch):
    calls = fake_safe_fetch(monkeypatch)
    ledger = Ledger()
    series = taco.fetch_taco(http=market_http, ledger=ledger)
    assert series == parse(make_bytes())
    ((url, kwargs),) = calls
    assert url == taco.URL and kwargs["max_bytes"] == 4 * MIB and kwargs["allow_redirects"] is False
    assert ledger.reserved == ["ocmacro.com"] and ledger.cooled == []


@pytest.mark.parametrize("status, kind, cooled", [
    (429, "rate_limited", True), (403, "blocked", True), (503, "status", False), (500, "status", False),
    (404, "status", False), (301, "moved", False),
])
def test_the_real_transport_turns_statuses_into_listed_kinds(monkeypatch, status, kind, cooled):
    fake_safe_fetch(monkeypatch, status=status, body=CLOUDFLARE)
    ledger = Ledger()
    with pytest.raises(market_http.FetchError) as caught:
        taco.fetch_taco(http=market_http, ledger=ledger)
    assert caught.value.kind == kind
    assert ledger.cooled == ([("ocmacro.com", market_http.COOLDOWN_SECONDS)] if cooled else [])  # 429／403 才冷卻


def test_the_real_transport_refuses_other_content_types_and_oversized_bodies(monkeypatch):
    import safe_fetch

    fake_safe_fetch(monkeypatch, content_type="application/json", body=b"{}")
    with pytest.raises(market_http.FetchError) as caught:
        taco.fetch_taco(http=market_http)
    assert caught.value.kind == "content_type"
    fake_safe_fetch(monkeypatch, error=safe_fetch.TooLarge("too big"))
    with pytest.raises(market_http.FetchError) as caught:
        taco.fetch_taco(http=market_http)
    assert caught.value.kind == "too_big"
    # Cloudflare 的驗證頁（200 或 403 都可能）：200 → 解析失敗（沒有 push）
    fake_safe_fetch(monkeypatch, body=CLOUDFLARE)
    with pytest.raises(taco.ParseError) as error:
        taco.fetch_taco(http=market_http)
    assert (error.value.kind, error.value.detail) == ("format", "no_push")


def test_outside_the_whitelist_not_a_single_request_is_sent(monkeypatch):
    calls = fake_safe_fetch(monkeypatch)
    monkeypatch.setattr(market_http, "ALLOWED_HOSTS", market_http.ALLOWED_HOSTS - {"ocmacro.com"})
    ledger = Ledger()
    with pytest.raises(market_http.FetchError) as caught:
        taco.fetch_taco(http=market_http, ledger=ledger)
    assert caught.value.kind == "host"
    assert calls == [] and ledger.reserved == []  # 連帳本都沒碰


# ── 模組衛生 ────────────────────────────────────────────────────────────────────


def test_the_source_module_only_imports_market_calc_and_loads_the_transport_lazily():
    imports = imported_modules(HERE / "market_sources_taco.py")
    assert {name for name in imports if name.startswith("market_")} == {"market_calc", "market_http"}
    tree_text = (HERE / "market_sources_taco.py").read_text(encoding="utf-8")
    top_level = [line for line in tree_text.splitlines() if line.startswith(("import ", "from "))]
    assert not any("market_http" in line or "requests" in line or "safe_fetch" in line for line in top_level)
    assert not imports & {"main", "stock_picks", "fcn_eval", "card_pages", "config", "requests", "safe_fetch"}


def test_importing_the_source_module_does_no_io():
    import subprocess

    code = (f"import sys; sys.path.insert(0, {str(HERE)!r}); import market_sources_taco; "
            "print(sorted(m for m in ('requests', 'safe_fetch', 'market_http', 'sqlite3', 'socket') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60, check=True)
    assert out.stdout.strip() == "[]"
