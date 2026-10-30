"""grounding_evidence: the quantities a cited page holds, read safely (PLAN §10, 2026-10-29).

Synthetic pages only, served by a fake ``safe_fetch.fetch``; nothing reaches the network.
"""
import gzip
import threading
import time
import zlib

import pytest
import requests

import grounding_evidence as ge
import reply_guards
import safe_fetch

URL = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/SYNTH1"
URL2 = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/SYNTH2"


def _page(body, *, content_type="text/html; charset=utf-8", status=200, final="https://pages.example/a"):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body if isinstance(body, bytes) else body.encode("utf-8")
    if content_type is not None:
        resp.headers["Content-Type"] = content_type
    resp.url = final
    return resp


@pytest.fixture
def served(monkeypatch):
    """A fake fetch: ``pages[url]`` is a Response, an exception to raise, or a callable."""
    pages, calls = {}, []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        page = pages.get(url)
        if callable(page):
            page = page()
        if isinstance(page, BaseException):
            raise page
        if page is None:
            raise safe_fetch.BlockedURL("unknown")
        return page

    monkeypatch.setattr(safe_fetch, "fetch", fetch)
    return pages, calls


def _q(kind, value):
    return reply_guards.Quantity(kind, value)


def _backing(urls, needed, seconds=5.0):
    return ge.page_backing(urls, set(needed), deadline=time.monotonic() + seconds)


# ── what may back a number ───────────────────────────────────────────────────

def test_every_canonical_kind_renders_back_to_itself():
    # A contract with reply_guards: a new unit there must round-trip here, or the page
    # step quietly stops rescuing it (REVIEW_gp2_plan_s10 F11).
    kinds = {kind for kind, _factor in reply_guards._UNITS.values()}
    assert kinds
    for kind in kinds:
        for value in ("1", "90", "1.5", "20000"):
            assert ge.render(_q(kind, value)), (kind, value)


def test_years_never_need_a_page_and_never_come_from_one(served):
    pages, _calls = served
    assert ge.needed_quantities("自2024年起，免簽停留期限為90天。") == {_q("天", "90")}
    pages[URL] = _page("<p>免簽停留期限為90天（民國112年修正，2019年公告）。</p>")
    backing, stats = _backing([URL], {_q("天", "90")})
    assert backing == "90天" and stats["kept"] == 1
    assert all(q.kind != "年份" for q in reply_guards.quantities(backing))


def test_the_backing_is_exactly_the_needed_quantities_the_pages_hold(served):
    pages, _calls = served
    pages[URL] = _page("<p>酒類1公升，捲菸200支，最長可停留九十日，規費新臺幣1,600元。</p>")
    needed = {_q("ml", "1000"), _q("天", "90"), _q("元", "500")}
    backing, _stats = _backing([URL], needed)
    assert set(reply_guards.quantities(backing)) == {_q("ml", "1000"), _q("天", "90")}


def test_an_inline_number_stays_whole_and_cells_stay_apart(served):
    pages, _calls = served
    pages[URL] = _page("<p>限量1<span>5公升</span></p>")
    assert _backing([URL], {_q("ml", "15000"), _q("ml", "5000")})[0] == "15000ml"
    pages[URL] = _page("<table><tr><td>1</td><td>5公升</td></tr></table>")
    assert _backing([URL], {_q("ml", "15000"), _q("ml", "5000")})[0] == "5000ml"


def test_scripts_styles_and_the_head_back_nothing(served):
    pages, _calls = served
    pages[URL] = _page(
        "<html><head><title>90天</title></head><body><script>var d='90天';</script>"
        "<style>.x{}</style><noscript>90天</noscript><p>另見說明</p></body></html>"
    )
    backing, stats = _backing([URL], {_q("天", "90")})
    assert backing == "" and stats["nomatch"] == 1


def test_a_number_at_the_40k_cut_of_a_long_line_is_not_made_up(served):
    pages, _calls = served
    pages[URL] = _page("。" * 39996 + "NT$20000", content_type="text/plain; charset=utf-8")
    backing, _stats = _backing([URL], {_q("元", "2"), _q("元", "20000")})
    assert backing == "20000元"


def test_a_comma_inside_a_number_is_no_place_to_cut(served):
    # REVIEW_gp1_plan_s10 Phase 6: the window's end must not hide 「1,|000元」's digits.
    pages, _calls = served
    pages[URL] = _page("合" * 29998 + "1,000元", content_type="text/plain; charset=utf-8")
    backing, _stats = _backing([URL], {_q("元", "0"), _q("元", "1000")})
    assert "0元" not in backing.split("\n")


def test_the_scan_budget_never_cuts_a_number(served):
    pages, _calls = served
    pages[URL] = _page("。" * 299_996 + "NT$20000", content_type="text/plain; charset=utf-8")
    backing, _stats = _backing([URL], {_q("元", "2"), _q("元", "20000")})
    assert "2元" not in backing.split("\n")


@pytest.mark.parametrize("html", [
    "<p>酒類<!-- 註解 -->90天</p>",
    "<!--StartFragment--><p>停留90天</p><!--EndFragment-->",
    "<p>停留<?php echo 1; ?>90天</p>",
])
def test_text_after_a_comment_or_instruction_is_read(served, html):
    pages, _calls = served
    pages[URL] = _page(html)
    assert _backing([URL], {_q("天", "90")})[0] == "90天"


def test_a_long_line_with_no_safe_cut_backs_nothing(served):
    pages, _calls = served
    pages[URL] = _page("合" * 35000 + "90天", content_type="text/plain; charset=utf-8")
    assert _backing([URL], {_q("天", "90")})[0] == ""


# ── reading a page safely ─────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [
    gzip.compress(("<p>90天</p>" * 200_000).encode()),  # a 2 MB page, gzip
    zlib.compress(("<p>90天</p>" * 200_000).encode()),  # zlib
    b"\x28\xb5\x2f\xfd" + b"\x00" * 64,  # zstd's magic
    b"PK\x03\x04" + b"\x00" * 64,  # zip
    b"%PDF-1.7\n90\xe5\xa4\xa9",  # a PDF that happens to hold 90天
])
def test_compressed_and_pdf_bodies_are_never_unpacked(served, monkeypatch, body):
    pages, _calls = served
    pages[URL] = _page(body, content_type="text/html")
    seen = []
    monkeypatch.setattr(ge, "_html_text", lambda text: seen.append(text) or "")
    backing, stats = _backing([URL], {_q("天", "90")})
    assert backing == "" and stats["magic"] == 1 and seen == []


def test_deep_nesting_is_read_fast_and_fails_closed(served):
    pages, _calls = served
    pages[URL] = _page("<div>" * 100_000 + "90天" + "</div>" * 100_000)
    started = time.process_time()  # CPU, not the wall clock: steady on a loaded machine
    backing, _stats = _backing([URL], {_q("天", "90")}, seconds=30)
    assert time.process_time() - started < 5
    assert backing in ("", "90天")


@pytest.mark.parametrize("content_type, body", [
    ("text/html", "<p>停留期間最長90天</p>".encode("utf-8")),  # no charset: not ISO-8859-1
    ("text/html", b'<meta charset="big5"><p>' + "停留期間最長90天".encode("big5") + b"</p>"),
    ("application/xhtml+xml",
     '<?xml version="1.0" encoding="UTF-8"?><html><body><p>停留期間最長90天</p></body></html>'.encode()),
    ("text/plain", "停留期間最長90天".encode("utf-8")),
])
def test_pages_are_decoded_here(served, content_type, body):
    pages, _calls = served
    pages[URL] = _page(body, content_type=content_type)
    assert _backing([URL], {_q("天", "90")})[0] == "90天"


@pytest.mark.parametrize("content_type, body", [
    # a declared charset holds past one bad byte (REVIEW_gp1_plan_s10 Phase 6 P6-2)
    ("text/html; charset=utf-8", "<p>停留90天</p>".encode() + "停".encode()[:2]),  # the 1 MB cut
    ("text/html; charset=big5", "<p>碁停留90天</p>".encode("cp950")),  # a cp950-only character
    ("text/html; charset=utf-8", b"<p>\xff" + "停留90天".encode() + b"</p>"),  # one stray byte
    ("text/html", "<p>停留90天</p>".encode() + "停".encode()[:2]),  # no charset, cut at the end
])
def test_one_bad_byte_does_not_lose_the_page(served, content_type, body):
    pages, _calls = served
    pages[URL] = _page(body, content_type=content_type)
    assert _backing([URL], {_q("天", "90")})[0] == "90天"


def test_only_web_encodings_are_used(served):
    # REVIEW_gp2_plan_s10 Phase 6 P6-1: a declared punycode decodes in quadratic time.
    assert ge._codec("punycode") == "" and ge._codec("utf-7") == ""
    assert ge._codec("big5") == "cp950" and ge._codec("UTF-8") == "utf-8" and ge._codec("gb2312") == "gbk"
    pages, _calls = served
    pages[URL] = _page(("a" * 99 + "\n") * 2000 + "停留90天", content_type="text/plain; charset=punycode")
    started = time.process_time()
    assert _backing([URL], {_q("天", "90")}, seconds=30)[0] == "90天"
    assert time.process_time() - started < 3


def test_a_page_that_breaks_the_reader_is_counted(served, monkeypatch):
    pages, _calls = served
    pages[URL] = _page("<p>90天</p>")
    monkeypatch.setattr(ge, "_decode", lambda *_a: 1 / 0)
    backing, stats = _backing([URL], {_q("天", "90")})
    assert backing == "" and stats["error"] == 1


def test_b2_runs_before_b1_as_the_second_pass_assumes():
    # main._page_backed_guards rebuilds B1's own output as apply_reply_guards does: B2, then B1.
    for reply, evidence in [
        ("免簽最多可停留90天。", ""),
        ("免簽最多可停留90天。", "90天"),
        ("搜尋結果都沒有提到其他限制。入境菸酒免稅額度是酒類1公升。", ""),
        ("台灣護照赴日免簽，護照效期需6個月以上。\n免簽最多可停留90天。", "6個月"),
    ]:
        whole = reply_guards.apply_reply_guards(reply, request_text="幾天？", evidence_text=evidence,
                                                search_only=True)
        b2, _negative = reply_guards.strip_negative_coverage(reply, False, True)
        b1 = reply_guards.strip_unbacked_regulatory_numbers(
            b2, lambda: reply_guards.regulation_backing("", "幾天？", "", evidence))
        assert whole.numbers_dropped == b1.dropped, reply


@pytest.mark.parametrize("text", ["辦理簽證需90天", "x 90天", "老舊規定90天"])
def test_plain_text_is_not_taken_for_zlib(served, text):
    pages, _calls = served
    pages[URL] = _page(text, content_type="text/plain; charset=utf-8")
    assert _backing([URL], {_q("天", "90")})[0] == "90天"


@pytest.mark.parametrize("page, counter", [
    (_page("<p>90天</p>", status=404), "non200"),
    (safe_fetch.Unsupported("application/pdf"), "unsupported"),
    (safe_fetch.BlockedURL("non-public address"), "blocked"),
    (safe_fetch.FetchTimeout("deadline"), "timeout"),
    (requests.ConnectionError("synthetic"), "error"),
    (_page("<p>請稍候</p>", final="https://vertexaisearch.cloud.google.com/interstitial"), "unresolved"),
])
def test_a_page_that_cannot_back_anything_is_counted(served, page, counter):
    pages, _calls = served
    pages[URL] = page
    backing, stats = _backing([URL], {_q("天", "90")})
    assert backing == "" and stats[counter] == 1 and stats["tried"] == 1


def test_only_the_cited_urls_go_out_with_two_fixed_headers(served):
    pages, calls = served
    urls = [URL, URL, URL2, "http://pages.example/c", "https://pages.example/d", "https://pages.example/e",
            "ftp://pages.example/f"]
    for url in urls:
        pages[url] = _page("<p>90天</p>", final=url)
    backing, stats = _backing(urls, {_q("天", "90")})
    assert backing == "90天"
    assert [url for url, _kw in calls] == [URL, URL2, "http://pages.example/c", "https://pages.example/d"]
    assert stats["tried"] == 4
    outcomes = sum(stats[key] for key in ge.STATS_KEYS if key not in ("tried", "kept"))
    assert outcomes <= stats["tried"]
    for _url, kwargs in calls:
        assert set(kwargs["headers"]) == {"User-Agent", "Accept-Language"}
        assert "params" not in kwargs and kwargs.get("method", "GET") == "GET"
        assert kwargs["max_bytes"] <= 1_000_000 and kwargs["truncate"] is True
        assert set(kwargs["accept_types"]) == {"text/html", "application/xhtml+xml", "text/plain"}


def test_two_citations_of_one_page_read_it_once(served, monkeypatch):
    pages, _calls = served
    pages[URL] = _page("<p>90天</p>", final="https://pages.example/same")
    pages[URL2] = _page("<p>90天</p>", final="https://pages.example/same")
    reads = []
    real = ge._html_text
    monkeypatch.setattr(ge, "_html_text", lambda text: reads.append(1) or real(text))
    assert _backing([URL, URL2], {_q("天", "90")})[0] == "90天"
    assert len(reads) == 1


def test_a_slow_page_never_holds_the_caller_past_the_deadline(served):
    pages, _calls = served
    release = threading.Event()

    def slow():
        release.wait(10)
        return _page("<p>90天</p>")

    pages[URL] = slow
    started = time.monotonic()
    backing, stats = _backing([URL], {_q("天", "90")}, seconds=0.6)
    assert time.monotonic() - started < 5  # the page holds 10 s; the caller must not wait for it
    assert backing == "" and stats["timeout"] == 1
    workers = [t for t in threading.enumerate() if t.name == "grounding-page"]
    assert workers and all(t.daemon for t in workers)
    release.set()


def test_nothing_is_read_without_urls_or_needed_quantities(served):
    _pages, calls = served
    assert _backing([], {_q("天", "90")}) == ("", {key: 0 for key in ge.STATS_KEYS})
    assert _backing([URL], set())[0] == ""
    assert calls == []


def test_the_kill_switch(monkeypatch):
    assert ge.enabled()
    monkeypatch.setenv("LINE_BOT_GROUNDING_BACKING", "0")
    assert not ge.enabled()


def test_counts_name_no_url():
    text = ge.format_stats({"tried": 2, "ok": 1, "kept": 1})
    assert text.startswith("tried=2 ok=1") and "http" not in text
