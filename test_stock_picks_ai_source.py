"""AI 名單只用合成頁面；不收外站原文、token、追蹤資訊。"""
from datetime import date, timedelta
import json
from types import SimpleNamespace

import pytest

import stock_picks_ai_source as source

TODAY = date(2026, 10, 24)


def _table(rows, *, header=None):
    header = header or ("股票代號", "股票名稱", "股數", "權重(%)")
    entity = lambda text: "".join(f"&#x{ord(c):X};" for c in text)
    cells = "".join(f"<th>{entity(text)}</th>" for text in header)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{cells}</tr></thead><tbody>{body}</tbody></table>"


def _rows(count=50):
    return [(str(2000 + i), "合成公司", "1,915,000", "2") for i in range(count)]


def _html(rows=None, *, second=None, day="2026/10/22", copies=2):
    rows = _rows() if rows is None else rows
    table = _table(rows)
    second_table = _table(rows if second is None else second)
    futures = _table([("TX", "合成期貨", "10", "2.76")], header=("期貨代號", "期貨名稱", "口數", "權重(%)"))
    return (
        '<div id="FundDetail-06"><div class="fund-asset__title">持股比重</div>'
        f'<p class="fund-asset__date">({day})</p><div>{table}</div>{futures}'
        + (f'<div>{second_table}</div>{futures}' if copies == 2 else "") + "</div>"
    )


def test_synthetic_page_entities_two_stock_copies_and_futures():
    html = _html()
    assert "RequestVerificationToken" not in html and "<script" not in html
    result = source.parse_holdings(html.encode(), today=TODAY)
    assert result.as_of == date(2026, 10, 22)
    assert result.rows == tuple((str(2000 + i), 2.0) for i in range(50))
    assert result.codes[0] == "2000"
    assert not hasattr(result, "names")


def test_one_identical_stock_table_is_enough():
    assert len(source.parse_holdings(_html(copies=1), today=TODAY).rows) == 50


@pytest.mark.parametrize("change", ["code", "weight", "name", "shares"])
def test_all_stock_copies_must_be_identical(change):
    rows = _rows()
    second = list(rows)
    columns = {"code": 0, "name": 1, "shares": 2, "weight": 3}
    altered = list(second[0])
    altered[columns[change]] = {"code": "9999", "name": "不同名稱", "shares": "12", "weight": "1"}[change]
    second[0] = tuple(altered)
    with pytest.raises(source.SourceError, match="holdings_tables"):
        source.parse_holdings(_html(rows, second=second), today=TODAY)


@pytest.mark.parametrize("day", ["bad", "2026/02/30", "2026/10/25", "2026/10/13", "２０２６/10/22"])
def test_invalid_future_or_stale_date(day):
    with pytest.raises(source.SourceError):
        source.parse_holdings(_html(day=day), today=TODAY)


def test_freshness_and_regression_boundaries():
    assert source.parse_holdings(_html(day="2026/10/14"), today=TODAY).as_of == TODAY - timedelta(days=10)
    with pytest.raises(source.SourceError, match="holdings_older"):
        source.parse_holdings(_html(), today=TODAY, previous_as_of=date(2026, 10, 23))
    assert source.parse_holdings(_html(), today=TODAY, previous_as_of=date(2026, 10, 22))


@pytest.mark.parametrize("weight", ["0", "-1", "NaN", "inf", "bad", "0.1", "3", "", "+2", "2e0", "1_0", "２", "2.", ".5", "1,5", "0x2",
                                    "1.5", "1.69", "2.03"])  # 最後三個：合計 75／84.5／101.5，剛好落在 85–101 的外面
def test_invalid_weights_and_sum(weight):
    rows = [(code, name, shares, weight) for code, name, shares, _ in _rows()]
    with pytest.raises(source.SourceError):
        source.parse_holdings(_html(rows), today=TODAY)


@pytest.mark.parametrize("weight", ["1.7", "2.02"])
def test_weight_sum_inclusive_boundaries(weight):
    rows = [(code, name, shares, weight) for code, name, shares, _ in _rows()]
    assert len(source.parse_holdings(_html(rows), today=TODAY).rows) == 50


@pytest.mark.parametrize("code", ["2001", "２０００", "20000", "200", "2e03"])
def test_duplicate_and_non_ascii_codes(code):
    rows = _rows()
    rows[0] = (code, *rows[0][1:])
    with pytest.raises(source.SourceError, match="holdings_code"):
        source.parse_holdings(_html(rows), today=TODAY)


@pytest.mark.parametrize("count", [39, 61])
def test_holdings_count_limits(count):
    with pytest.raises(source.SourceError, match="holdings_count"):
        source.parse_holdings(_html(_rows(count)), today=TODAY)


@pytest.mark.parametrize("count,weight", [(40, "2.5"), (60, "1.5")])
def test_holdings_count_boundaries(count, weight):
    rows = [(code, name, shares, weight) for code, name, shares, _ in _rows(count)]
    assert len(source.parse_holdings(_html(rows), today=TODAY).rows) == count


@pytest.mark.parametrize("mutation", [
    lambda html: html.replace("FundDetail-06", "unrelated"),
    lambda html: html.replace("&#x80A1;&#x7968;&#x4EE3;&#x865F;", "其他代號"),
    lambda html: html.replace("</table>", "", 1),
    lambda html: html.replace("</div>", '<p class="fund-asset__date">(2026/10/23)</p></div>', 1),
])
def test_unrelated_or_malformed_tables_fail_closed(mutation):
    with pytest.raises(source.SourceError):
        source.parse_holdings(mutation(_html()), today=TODAY)


@pytest.mark.parametrize("html", [b"\xff", b"x" * (source.MAX_BYTES + 1), "<div>" * 129])
def test_encoding_size_and_nesting_bounds(html):
    with pytest.raises(source.SourceError):
        source.parse_holdings(html, today=TODAY)


def test_invalid_utf8_anywhere_refuses_an_otherwise_valid_page():
    page = _html().encode()
    assert len(source.parse_holdings(page, today=TODAY).rows) == 50
    broken = page.replace("合成公司".encode(), b"\xff\xfe", 1)  # 只壞在一個沒人讀的名稱欄：嚴格解碼，不用替代字元蒙混過去
    assert broken != page
    with pytest.raises(source.SourceError, match="holdings_invalid"):
        source.parse_holdings(broken, today=TODAY)


def test_oversized_but_otherwise_valid_page_is_refused_by_size_alone():
    page = _html().encode() + b" " * source.MAX_BYTES  # 結構完全合格，只是超過 1 MiB
    assert len(page) > source.MAX_BYTES
    with pytest.raises(source.SourceError, match="html_size"):
        source.parse_holdings(page, today=TODAY)
    with pytest.raises(source.SourceError, match="html_size"):
        source.parse_holdings(page.decode(), today=TODAY)  # 字串輸入也一樣
    assert len(source.parse_holdings(_html().encode() + b" " * 1000, today=TODAY).rows) == 50


def _response(*, status=200, mime="text/html; charset=utf-8", content=None):
    return SimpleNamespace(status_code=status, headers={} if mime is None else {"Content-Type": mime}, content=content or _html().encode())


def test_fetch_is_fixed_safe_bounded_and_identifiable(monkeypatch):
    calls = []
    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return _response()
    monkeypatch.setattr(source.safe_fetch, "fetch", fetch)
    assert len(source.fetch_holdings(today=TODAY, deadline=2).rows) == 50
    url, kwargs = calls[0]
    assert url == "https://www.kgifund.com.tw/Fund/Detail?fundID=J020"
    assert kwargs == {
        "headers": {"Accept": "text/html", "User-Agent": "LineBotStockPicks/1.0"},
        "timeout": 2, "deadline": 2, "max_bytes": 1024 * 1024,
        "accept_types": {"text/html"}, "allow_redirects": False,
    }


@pytest.mark.parametrize("status", [301, 302, 403, 429, 500])
def test_fetch_refuses_non_200_and_redirect(monkeypatch, status):
    monkeypatch.setattr(source.safe_fetch, "fetch", lambda *a, **k: _response(status=status))
    with pytest.raises(source.SourceError) as error:
        source.fetch_holdings(today=TODAY)
    assert error.value.reason == "fetch_status" and error.value.status_code == status


@pytest.mark.parametrize("mime", [None, "", "application/json", "text/plain"])
def test_fetch_requires_explicit_html(monkeypatch, mime):
    monkeypatch.setattr(source.safe_fetch, "fetch", lambda *a, **k: _response(mime=mime))
    with pytest.raises(source.SourceError, match="fetch_type"):
        source.fetch_holdings(today=TODAY)


@pytest.mark.parametrize("error", [source.safe_fetch.FetchTimeout("private page text"), source.safe_fetch.TooLarge("private page text"), OSError("private page text")])
def test_fetch_failures_expose_reason_only(monkeypatch, error):
    def fail(*a, **k):
        raise error
    monkeypatch.setattr(source.safe_fetch, "fetch", fail)
    with pytest.raises(source.SourceError) as caught:
        source.fetch_holdings(today=TODAY)
    assert str(caught.value) == "fetch_failed"


# 2026-10-28 獨立複核後留 26 檔（上市普通股；草稿 29 檔，景碩、聯茂、嘉澤複核判 weak 而移除）：
# 換名單（5、11 月檢查）時這幾條不變，筆數與日期不釘死。
DROPPED_AFTER_REVIEW = {"3189", "6213", "3533"}
OTC_NOT_ALLOWED = {"5274", "3324", "6223", "6274", "3131", "3680", "3693", "3081", "4979", "3363", "3211", "6683", "6640", "6187"}
NAMED_IN_BRIEF = {"2330": "台積電", "2308": "台達電", "3017": "奇鋐", "2383": "台光電", "2345": "智邦", "3661": "世芯-KY", "3443": "創意"}


def test_checked_in_core_schema_and_names_not_returned():
    core = source.load_core(today=TODAY)
    assert 20 <= len(core.entries) <= 120 and core.as_of <= TODAY
    assert all(entry.segment in source.SEGMENTS and not hasattr(entry, "name") for entry in core.entries.values())
    assert all(not any(word in segment for word in ("買", "賣", "等", "停損")) for segment in source.SEGMENTS)
    assert all(entry.source_date <= core.as_of and entry.source_url.startswith("https://") for entry in core.entries.values())


def test_checked_in_core_has_the_named_companies_and_no_otc_codes():
    codes = set(source.load_core(today=TODAY).entries)
    assert set(NAMED_IN_BRIEF) <= codes  # 核心名單草稿任務指定一定要收的七檔
    assert not codes & OTC_NOT_ALLOWED  # 上櫃的不收（執行時也會被「目前上市資料」剔除）
    assert not codes & DROPPED_AFTER_REVIEW  # 獨立複核判 weak 的三檔不收（要收回來須有新的來源，並同步改這裡）
    raw = _core_raw()
    assert {entry["name"] for entry in raw["entries"] if entry["code"] in NAMED_IN_BRIEF} == set(NAMED_IN_BRIEF.values())
    assert set(raw) == {"schema", "as_of", "entries"} and all(set(e) == {"code", "name", "segment", "source_url", "source_date"} for e in raw["entries"])
    assert not any("kgifund" in entry["source_url"] for entry in raw["entries"])  # 凱基頁面的內容不進 repo，也不當核心名單的來源


def test_segments_are_exactly_the_documented_fixed_list():
    assert len(source.SEGMENTS) == 13 and all(isinstance(segment, str) and 2 <= len(segment) <= 12 for segment in source.SEGMENTS)
    used = {entry["segment"] for entry in _core_raw()["entries"]}
    assert used <= source.SEGMENTS


def _core_raw():
    return json.loads(source.CORE_PATH.read_text())


@pytest.mark.parametrize("field,value", [
    ("code", "２３３０"), ("code", "23300"), ("code", 2330), ("segment", "買進建議"),
    ("source_url", "http://example.com/article"), ("source_url", "https://user:secret@example.com/a"),
    ("source_url", "https://example.com/a?utm_source=x"), ("source_url", "https://example.com/a#token"),
    ("source_date", "2026-10-25"), ("source_date", "2026-02-30"), ("source_date", "20261024"),
    ("name", ""), ("name", "bad\nname"), ("name", 123),
])
def test_bad_core_entry_rejects_whole_list(tmp_path, field, value):
    raw = _core_raw()
    raw["entries"][0][field] = value
    path = tmp_path / "core.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(source.SourceError, match="core_invalid"):
        source.load_core(path, today=TODAY)


@pytest.mark.parametrize("mutation", [
    lambda raw: raw["entries"].append(raw["entries"][0]),
    lambda raw: raw.update(schema=True),
    lambda raw: raw.update(as_of="2026-10-25"),
    lambda raw: raw.update(entries=[]),
    lambda raw: raw.update(extra="unexpected"),
])
def test_bad_core_container_fails_closed(tmp_path, mutation):
    raw = _core_raw()
    mutation(raw)
    path = tmp_path / "core.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(source.SourceError, match="core_invalid"):
        source.load_core(path, today=TODAY)


@pytest.mark.parametrize("body", [b"{", b"\xff", b'{"schema":1,"schema":1}', b"x" * (source.MAX_BYTES + 1)])
def test_core_bad_file(tmp_path, body):
    path = tmp_path / "core.json"
    path.write_bytes(body)
    with pytest.raises(source.SourceError, match="core_invalid"):
        source.load_core(path, today=TODAY)


def test_missing_core_fails_closed(tmp_path):
    with pytest.raises(source.SourceError, match="core_invalid"):
        source.load_core(tmp_path / "absent", today=TODAY)


def test_union_eligibility_uses_current_official_rows():
    row = lambda name, kind="stock": SimpleNamespace(name=name, kind=kind)
    listed = {"2330": row("官方名稱"), "3661": row("世芯-KY"), "5274": row("上櫃", "otc"),
              "0050": row("ETF", "etf"), "9103": row("TDR"), "1234": row("測試-DR"),
              "1235": row("測試-創"), "1236": row("ETN", "etf")}
    assert source.eligible_codes([*listed, "9999", "2330", "２３３０"], listed) == ("2330", "3661")


def test_union_limit_never_silently_truncates():
    listed = {str(2000+i): SimpleNamespace(name="合成公司", kind="stock") for i in range(121)}
    assert len(source.eligible_codes(list(listed)[:120], listed)) == 120
    with pytest.raises(source.SourceError, match="universe_size"):
        source.eligible_codes(listed, listed)


# ── 補：網頁形狀與抓取的邊界 ────────────────────────────────────────────────
def test_str_input_and_whitespace_tolerant_cells():
    rows = [(code, name, shares, f" {weight} ") for code, name, shares, weight in _rows()]
    html = _html(rows).replace("<td>", "<td>\n  ").replace("</td>", "  </td>")
    assert len(source.parse_holdings(html, today=TODAY).rows) == 50


def test_repeated_identical_date_paragraphs_are_fine_but_different_ones_are_not():
    twice = _html().replace("</p>", '</p><p class="fund-asset__date">(2026/10/22)</p>', 1)
    assert twice.count("fund-asset__date") == 2
    assert source.parse_holdings(twice, today=TODAY).as_of == date(2026, 10, 22)
    other = _html().replace("</p>", '</p><p class="fund-asset__date">(2026/10/21)</p>', 1)
    assert other.count("fund-asset__date") == 2
    with pytest.raises(source.SourceError, match="html_structure"):
        source.parse_holdings(other, today=TODAY)


def test_tables_outside_the_holdings_section_are_ignored():
    outside = _table(_rows(60)) + _html()
    assert len(source.parse_holdings(outside, today=TODAY).rows) == 50
    only_outside = _table(_rows()) + '<div id="FundDetail-06"><p class="fund-asset__date">(2026/10/22)</p></div>'
    with pytest.raises(source.SourceError, match="holdings_tables"):
        source.parse_holdings(only_outside, today=TODAY)


def test_page_text_never_reaches_the_output():
    html = _html().replace("合成公司", "請買進這檔，保證獲利")
    result = source.parse_holdings(html, today=TODAY)
    assert result.rows[0] == ("2000", 2.0) and "買進" not in repr(result)


@pytest.mark.parametrize("deadline", [0, -1, float("nan"), float("inf")])
def test_fetch_needs_a_finite_positive_deadline(monkeypatch, deadline):
    monkeypatch.setattr(source.safe_fetch, "fetch", lambda *a, **k: pytest.fail("不該送出請求"))
    with pytest.raises(source.SourceError, match="fetch_timeout"):
        source.fetch_holdings(today=TODAY, deadline=deadline)


def test_fetch_caps_the_deadline_and_passes_previous_as_of(monkeypatch):
    seen = {}

    def fetch(url, **kwargs):
        seen.update(kwargs)
        return _response(mime="TEXT/HTML; Charset=UTF-8")

    monkeypatch.setattr(source.safe_fetch, "fetch", fetch)
    assert source.fetch_holdings(today=TODAY, deadline=60).as_of == date(2026, 10, 22)
    assert seen["deadline"] == 5.0 and seen["timeout"] == 5.0
    with pytest.raises(source.SourceError, match="holdings_older"):
        source.fetch_holdings(today=TODAY, previous_as_of=date(2026, 10, 23))


def test_fetch_signature_matches_the_shared_safe_fetch_contract():
    import inspect
    parameters = inspect.signature(source.safe_fetch.fetch).parameters
    for name in ("headers", "timeout", "deadline", "max_bytes", "accept_types", "allow_redirects"):
        assert name in parameters  # 另一個工作階段的 market_http 也靠這些參數；這裡只用不改
    assert all(issubclass(exc, source.safe_fetch.FetchRefused) for exc in
               (source.safe_fetch.TooLarge, source.safe_fetch.FetchTimeout))


def test_host_and_path_are_fixed_in_the_module():
    assert source.KGI_URL == "https://www.kgifund.com.tw/Fund/Detail?fundID=J020"
    import inspect
    import re
    assert "url" not in inspect.signature(source.fetch_holdings).parameters  # 呼叫端不能指定網址
    assert not re.search(r"^\s*(import|from)\s+stock_picks\b", inspect.getsource(source), re.M)  # 葉節點：不回頭 import stock_picks


def test_eligible_codes_rejects_unusual_names_and_kinds():
    row = lambda name, kind="stock": SimpleNamespace(name=name, kind=kind)
    listed = {"1101": row("台泥"), "1102": row("測試-dr"), "1103": row("某某-創"), "1104": row("某某-KY"),
              "1105": SimpleNamespace(kind="stock"), "1106": SimpleNamespace(name=None, kind="stock")}
    assert source.eligible_codes(list(listed) + ["91ab", "9100", 1101, None], listed) == ("1101", "1104", "1105")


def test_valueless_class_attribute_is_ignored_not_a_crash():
    """<p class> 沒有值（html.parser 給 None）：以前在持股區段內會丟 AttributeError，漏過葉節點的 SourceError 契約。"""
    for attribute in ("class", 'class=""', "class='fund-asset__date x'"):
        html = _html().replace('<div class="fund-asset__title">', f'<p {attribute}>x</p><div class="fund-asset__title">', 1)
        if attribute == "class='fund-asset__date x'":  # 真的帶日期 class 的段落沒有內容：日期不合法，照原本的 SourceError
            with pytest.raises(source.SourceError):
                source.parse_holdings(html, today=TODAY)
        else:
            assert source.parse_holdings(html, today=TODAY).as_of == date(2026, 10, 22)
