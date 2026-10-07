from contextlib import nullcontext
from types import SimpleNamespace
import threading
import time

import pytest

import main
import public_research as research

FIXTURE = "今年東南亞榴槤生產過剩，所以比較便宜。"
SOURCES = [{"title": "Synthetic market report", "url": "https://example.com/report",
            "published": "2026-09-18", "full_text": "測試報導：本期供應量增加，而需求增幅較小，部分品種售價下降；各產區與等級並不完全相同。"}]


@pytest.mark.parametrize("text", [FIXTURE, "今年咖啡產量下降，價格上漲。", "/問 今年咖啡產量下降，價格上漲。"])
def test_current_public_claims_need_research(text):
    assert research.requires_current_research(text)


@pytest.mark.parametrize("text", ["我今年想去旅行", "媽媽今年買的榴槤比較便宜", "今年產量增加\n我家地址在這裡", "提醒我今年繳費", "好", "榴槤是什麼", "今年帳戶支出增加"])
def test_private_or_noncurrent_text_does_not_trigger(text):
    assert not research.requires_current_research(text)


def test_no_title_or_publisher_only_evidence():
    assert research.collect(lambda _: [{"url": "https://example.com", "snippet": "News"}], FIXTURE) == []


def test_deadline_returns_without_waiting_for_worker():
    done = threading.Event()
    start = time.monotonic()
    try:
        assert research.collect(lambda _: done.wait(1), FIXTURE, timeout=.02) == []
        assert time.monotonic() - start < .5
    finally:
        done.set()


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(main, "_thinking_indicator", lambda *_: nullcontext())
    monkeypatch.setattr(main.memory, "get_context", lambda *_: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda *a, **k: [])
    monkeypatch.setattr(main.memory, "append_turn", lambda *a: None)
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_: [])
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_: False)
    replies = []
    monkeypatch.setattr(main, "_reply", lambda _, text, **kw: replies.append(text))
    return replies


def test_burst_collects_before_generation_and_bypasses_old_cache(monkeypatch, isolated):
    calls = []
    monkeypatch.setattr(main.memory, "check_fact_cache", lambda *a: pytest.fail("stale cache bypass required"))
    monkeypatch.setattr(main.memory, "store_fact_cache", lambda *a: pytest.fail("no undated research answer cache"))
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda text: calls.append(("search", text)) or SOURCES)
    def generate(prompt, *args):
        calls.append(("generate", prompt))
        assert "example.com/report" in prompt
        return "供應增加可能壓低價格，但不同品種仍有差異。"
    monkeypatch.setattr(main, "_llm_chat", generate)
    main._handle_burst_flush("G_TEST", FIXTURE, "T_TEST")
    assert calls[0] == ("search", FIXTURE)
    assert [x[0] for x in calls] == ["search", "generate"]
    assert isolated == ["供應增加可能壓低價格，但不同品種仍有差異。"]


def test_promise_repaired_using_same_evidence(monkeypatch, isolated):
    searches = []
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda text: searches.append(text) or SOURCES)
    answers = iter(["我會搜尋看看近期報導。", "供應增加使部分品種價格下跌。"])
    monkeypatch.setattr(main, "_llm_chat", lambda *a: next(answers))
    event = SimpleNamespace(source=SimpleNamespace(user_id="U_TEST"), reply_token="T_TEST")
    assert main._handle_web_research_question(event, "G_TEST", FIXTURE)
    assert searches == [FIXTURE]
    assert isolated == ["供應增加使部分品種價格下跌。"]


def test_empty_search_does_not_generate_unverified_answer(monkeypatch, isolated):
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _: [])
    monkeypatch.setattr(main, "_llm_chat", lambda *a: pytest.fail("no evidence"))
    event = SimpleNamespace(source=SimpleNamespace(user_id="U_TEST"), reply_token="T_TEST")
    main._handle_web_research_question(event, "G_TEST", FIXTURE)
    assert isolated == [research.NO_EVIDENCE]


@pytest.mark.parametrize("text", [
    "公司今年未公開的出口訂單減少，內部報價是每箱200元。",
    "今年公司的API key是sk-synthetic-secret，產量增加。",
    "今年價格下降，聯絡電話是測試號碼。",
    "今年產量增加\n媽媽的地址在測試路。",
    "今年榴槤價格下降，請聯絡0900000000。",
    "今年測試市合成路123號的測試人買榴槤比較便宜。",
])
def test_confidential_mixed_text_never_reaches_collector(text):
    assert not research.requires_current_research(text)
    assert research.collect(lambda _: pytest.fail("private lookup"), text) == []


@pytest.mark.parametrize("text", [
    "我先查一下最新資料，再回覆你。",
    "我可以幫你查近期報導。",
    "我目前沒有最新資料，等我搜尋一下。",
    "我可以幫你查查最新報導。",
    "", "  \n ",
])
def test_deferred_search_variants_are_retried_then_rejected(monkeypatch, isolated, text):
    attempts = []
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _: SOURCES)
    monkeypatch.setattr(main, "_llm_chat", lambda *a: attempts.append(a) or text)
    event = SimpleNamespace(source=SimpleNamespace(user_id="U_TEST"), reply_token="T_TEST")
    main._handle_web_research_question(event, "G_TEST", FIXTURE)
    assert len(attempts) == 2
    assert isolated == [research.NO_ANSWER]


@pytest.mark.parametrize("text", [FIXTURE.replace("，", "\n"),
    "今年咖啡產量下降，價格上漲，請給我來源", "幫我查今年榴槤產量",
    "今年消費者物價指數上漲。", "今年黃金產量下降，價格上漲。",
    "目前黃金價格上漲，因為央行需求增加。", "目前台積電價格上漲，因為需求增加。"])
def test_public_request_boilerplate_and_linebreaks(text):
    assert main._requires_public_research(text)


def test_explicit_current_claim_does_not_become_stock_quote(monkeypatch, isolated):
    event = SimpleNamespace(source=SimpleNamespace(user_id="U_TEST"), reply_token="T_TEST",
                            message=SimpleNamespace(quoted_message_id=None))
    monkeypatch.setattr(main, "_get_explicit_market_quote_reply", lambda *a, **k: pytest.fail("not a stock quote"))
    monkeypatch.setattr(main, "_handle_explicit_poll_text", lambda *a: None)
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _: SOURCES)
    monkeypatch.setattr(main, "_llm_chat", lambda *a: "供應增加使部分品種價格下跌。")
    main._handle_explicit_text(event, "G_TEST", FIXTURE)
    assert isolated == ["供應增加使部分品種價格下跌。"]


def test_stock_quote_keeps_dedicated_reply_only_route(monkeypatch, isolated):
    event = SimpleNamespace(source=SimpleNamespace(user_id="U_TEST"), reply_token="T_TEST",
                            message=SimpleNamespace(quoted_message_id=None))
    reply_options = []
    monkeypatch.setattr(main, "_handle_explicit_poll_text", lambda *a: None)
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _: pytest.fail("stock must use quote route"))
    monkeypatch.setattr(main, "_get_explicit_market_quote_reply", lambda *a, **k: "合成市場報價")
    monkeypatch.setattr(main, "_reply", lambda *a, **k: reply_options.append(k))
    main._handle_explicit_text(event, "G_TEST", "現在台積電價格多少")
    assert reply_options[0]["allow_push_fallback"] is False


def test_failed_fulltext_fetch_stays_a_search_summary(monkeypatch, tmp_path):
    import fulltext_fetcher
    monkeypatch.setattr(fulltext_fetcher, "_fetch_one", lambda *a: None)
    rows = fulltext_fetcher.fetch_top_sources(
        [{"url": "https://example.com/report", "snippet": "合成搜尋摘要內容。"}],
        cache_db=tmp_path / "cache.db",
    )
    prompt = main._format_web_research_sources(rows)
    assert "搜尋摘要（未確認全文）" in prompt
    assert "正文摘錄" not in prompt


def test_publisher_only_news_is_not_evidence(monkeypatch):
    import source_aggregator
    monkeypatch.setattr(source_aggregator.web_scraper, "search_google_news", lambda *a, **k: [
        {"url": "https://example.com/report", "source": "Synthetic International Broadcasting Corporation"}
    ])
    assert research.collect(lambda _: source_aggregator._fetch_gnews("query", 1), FIXTURE) == []


def test_research_skips_unbounded_news_decoder(monkeypatch):
    import source_aggregator
    calls = []
    monkeypatch.setattr(source_aggregator, "_fetch_ddg", lambda *a: [])
    monkeypatch.setattr(source_aggregator, "_fetch_wiki", lambda *a: None)
    monkeypatch.setattr(source_aggregator.web_scraper, "search_google_news",
                        lambda *a, **k: calls.append(k) or [])
    main._collect_web_research_sources(FIXTURE)
    assert calls and all(c["resolve_urls"] is False for c in calls)


def test_worker_slots_recover_after_timeout():
    done = threading.Event()
    finished = threading.Event()
    def delayed(_):
        done.wait(1)
        finished.set()
        return SOURCES
    try:
        assert research.collect(delayed, FIXTURE, timeout=.01) == []
    finally:
        done.set()
    assert finished.wait(1)
    assert research.collect(lambda _: SOURCES, FIXTURE) == SOURCES


def test_unavailable_video_status_is_not_evidence(monkeypatch, isolated):
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _: [])
    for reader in ("_fetch_video_ytdlp", "_fetch_youtube_meta", "_fetch_youtube_html_meta"):
        monkeypatch.setattr(main, reader, lambda *a: None)
    monkeypatch.setattr(main, "_llm_chat", lambda *a: pytest.fail("no usable video evidence"))
    event = SimpleNamespace(source=SimpleNamespace(user_id="U_TEST"), reply_token="T_TEST")
    main._handle_web_research_question(event, "G_TEST", "這直播在講什麼 https://www.youtube.com/live/DEMO1234567")
    # 2026-09-26: a video-link question with nothing retrieved stays silent
    # (9/19 link-failure rule) rather than sending NO_EVIDENCE.
    assert isolated == []
