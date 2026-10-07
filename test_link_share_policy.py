"""2026-09-26 Andrew: shared links of any site get the video treatment — no
summary of the page, and no reply (nor canned 「查不到」) when the bot could
not read anything from the link.  The research path now shows the reviewer
the linked material and gives it room in the prompt.

All fixtures are synthetic; no real chat content.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import main
import public_research
import restatement_judge

NEWS = "https://news.example.com/article/1?utm_source=lineshare"
PAGE_TEXT = "合成新聞內文：市府宣布每月補助兩千元，明年一月上路。" * 8  # > _PREFETCH_MIN_CHARS


# ── classification: any site, routing vs. silence eligibility ────────────────

@pytest.mark.parametrize("text", [
    NEWS,
    "https://www.instagram.com/p/DEMOPOST/",
    "https://liff.line.me/1234567890-DEMO/v3/article/DEMO?utm_source=lineshare",
    f"{NEWS}\nhttps://youtu.be/NOSUBS00000",
])
def test_bare_share_of_any_site(text):
    assert main._bare_link_share_urls(text)
    assert main._is_web_research_question(text) is False


def test_three_links_are_still_a_share():
    # Only two get fetched; the model cannot see the rest either, so a share of
    # three links with nothing readable is as silent as a share of one.
    text = f"{NEWS}\nhttps://news.example.com/b\nhttps://news.example.com/c"
    assert len(main._bare_link_share_urls(text)) == 3
    assert main._is_web_research_question(text) is False


def test_headline_share_is_not_bare():
    text = f"合成標題：市府補助上路 | 合成新聞 | LINE TODAY\n\n{NEWS}"
    assert main._bare_link_share_urls(text) == []


# ── content recorder: real page text only ────────────────────────────────────

def _html(body):
    return SimpleNamespace(status_code=200, text=f"<html><body><p>{body}</p></body></html>",
                           raise_for_status=lambda: None)


def _prefetch_found(monkeypatch, text, response):
    monkeypatch.setattr(main._requests, "get", lambda *_a, **_k: response)
    with main._recording_link_content() as found:
        block = main._prefetch_urls(text)
    return block, bool(found)


def test_fetched_page_text_is_content(monkeypatch):
    block, found = _prefetch_found(monkeypatch, NEWS, _html(PAGE_TEXT))
    assert found and "補助兩千元" in block


def test_empty_or_failed_page_is_not_content(monkeypatch):
    _block, found = _prefetch_found(monkeypatch, NEWS, _html("短"))
    assert not found

    def boom(*_a, **_k):
        raise OSError("synthetic network failure")

    monkeypatch.setattr(main._requests, "get", boom)
    with main._recording_link_content() as found:
        main._prefetch_urls(NEWS)
    assert not found


def _reddit(post, comments=()):
    payload = [
        {"data": {"children": [{"data": post}]}},
        {"data": {"children": [{"data": c} for c in comments]}},
    ]
    return SimpleNamespace(status_code=200, json=lambda: payload)


REDDIT = "https://www.reddit.com/r/demo/comments/abc123/synthetic_post/"


@pytest.mark.parametrize("post,comments,expected", [
    ({"title": "合成標題", "selftext": "合成內文：補助每月兩千元。"}, [], True),
    ({"title": "合成標題", "selftext": ""}, [{"body": "合成留言：申請要帶身分證。", "author": "a"}], True),
    ({"title": "合成標題", "selftext": ""}, [], False),
    ({"title": "合成標題", "selftext": "[removed]"}, [{"body": "[deleted]"}], False),
])
def test_reddit_needs_body_or_comments(monkeypatch, post, comments, expected):
    monkeypatch.setattr(main._requests, "get", lambda *_a, **_k: _reddit(post, comments))
    with main._recording_link_content() as found:
        main._fetch_reddit_meta(REDDIT)
    assert bool(found) is expected


# ── burst and explicit gates ─────────────────────────────────────────────────

@pytest.fixture
def burst_env(monkeypatch, tmp_path):
    monkeypatch.setattr(main.memory, "_DB_PATH", tmp_path / "memory.sqlite3")
    main.memory._init_db()
    assert main.memory.begin_inbound_event("GRP001", "MSG001") == "new"
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda _name: False)
    monkeypatch.setattr(main.memory, "get_context", lambda _gid: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda _gid: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda _gid: [])
    monkeypatch.setattr(main, "_is_market_quote_request", lambda *_a, **_kw: False)
    monkeypatch.setattr(main, "_thinking_indicator", lambda _gid: nullcontext())
    monkeypatch.setattr(main, "_maybe_capture_calendar_event", lambda *_a, **_kw: None)
    monkeypatch.setattr(main.memory, "check_fact_cache", lambda *_a: pytest.fail("stale cache must be bypassed"))
    prompts = []
    monkeypatch.setattr(main, "_llm_chat", lambda prompt, *_a: prompts.append(prompt) or "")
    monkeypatch.setattr(main, "_reply", lambda *_a, **_kw: False)
    return prompts


def test_burst_bare_news_link_without_content_is_silent(monkeypatch, burst_env):
    monkeypatch.setattr(main._requests, "get", lambda *_a, **_k: _html("短"))
    main._handle_burst_flush("GRP001", NEWS, "TOKEN001", ["MSG001"])
    assert burst_env == []
    assert main.memory.get_inbound_event_status("GRP001", "MSG001") == "completed_no_reply"


def test_burst_three_unreadable_links_are_silent(monkeypatch, burst_env):
    monkeypatch.setattr(main._requests, "get", lambda *_a, **_k: _html("短"))
    three = f"{NEWS}\nhttps://news.example.com/b\nhttps://news.example.com/c"
    main._handle_burst_flush("GRP001", three, "TOKEN001", ["MSG001"])
    assert burst_env == []
    assert main.memory.get_inbound_event_status("GRP001", "MSG001") == "completed_no_reply"


def test_burst_bare_news_link_with_page_asks_the_model(monkeypatch, burst_env):
    monkeypatch.setattr(main._requests, "get", lambda *_a, **_k: _html(PAGE_TEXT))
    main._handle_burst_flush("GRP001", NEWS, "TOKEN001", ["MSG001"])
    assert len(burst_env) == 1 and "補助兩千元" in burst_env[0]


def test_explicit_bare_news_link_without_content_is_silent(monkeypatch):
    from linebot.v3.webhooks import MessageEvent, TextMessageContent

    monkeypatch.setattr(main._requests, "get", lambda *_a, **_k: _html("短"))
    evt = MagicMock(spec=MessageEvent)
    evt.message = TextMessageContent(id="MSG903", text="咪寶 " + NEWS, quoteToken="qt")
    evt.source = SimpleNamespace(type="group", group_id="GRP001", user_id="U_TEST")
    evt.reply_token = "TOKEN903"
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._build_quoted_block", return_value=""),
        patch("main._requires_public_research", return_value=False),
        patch("main._llm_chat", side_effect=AssertionError("nothing fetched → silent")),
        patch("main.memory.append_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._maybe_capture_calendar_event"),
        patch("main._mark_inbound_reply_completed_no_reply") as mark_silent,
        patch("main._reply") as mock_reply,
    ):
        main._handle_explicit_text(evt, "GRP001", NEWS)
    mock_reply.assert_not_called()
    assert mark_silent.call_args.args[0] == "TOKEN903"


# ── research path ────────────────────────────────────────────────────────────

@pytest.fixture
def research_env(monkeypatch):
    monkeypatch.setattr(main, "_thinking_indicator", lambda *_: nullcontext())
    monkeypatch.setattr(main.memory, "get_context", lambda *_: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda *a, **k: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_: [])
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_: False)
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    assert main.memory.begin_inbound_event("G_LINK", "M_LINK") == "new"
    main._register_inbound_reply_batch("T_LINK", "G_LINK", ["M_LINK"])
    sent = []
    monkeypatch.setattr(main, "_reply", lambda _tok, text, **_kw: sent.append(text))
    monkeypatch.setattr(main.memory, "append_turn", lambda *a: None)
    event = SimpleNamespace(source=SimpleNamespace(user_id="U_TEST"), reply_token="T_LINK", message=None)
    return SimpleNamespace(event=event, sent=sent)


def _status():
    return main.memory.get_inbound_event_status("G_LINK", "M_LINK")


@pytest.mark.parametrize("url", [
    "https://news.example.com/新聞/1?share=synthetic-private-id",
    "https://新聞.example/報導?share=synthetic-private-id",
    "youtu.be/NOSUBS00000?si=synthetic-private-id",
    "m.youtu.be/NOSUBS00000?si=synthetic-private-id",
    "https://news.example.com/ＡＢＣ２０２６?share=synthetic-private-id",
    "https://news.example.com/a「b」?share=synthetic-private-id",
])
def test_search_query_never_contains_a_url_fragment(monkeypatch, research_env, url):
    searched = []
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda text: searched.append(text) or [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: text)
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: "")
    main._handle_web_research_question(research_env.event, "G_LINK", f"這篇怎麼看 {url}")
    assert searched and all("synthetic-private-id" not in q and "example" not in q for q in searched)


def test_link_only_research_text_still_prefetches(monkeypatch, research_env):
    # Nothing is left to search once the link is removed; the link is still read.
    searched, fetched = [], []
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda text: searched.append(text) or [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: fetched.append(text) or text)
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: "")
    main._handle_web_research_question(research_env.event, "G_LINK", NEWS)
    assert searched == []
    assert fetched == [NEWS]


def _page_row(text):
    return {"url": NEWS, "full_text": text, "evidence_kind": "linked_context"}


def test_linked_material_gets_room_and_reaches_the_judge(monkeypatch, research_env):
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "1")
    late_fact = "（第七百字之後的合成事實：受理窗口在三樓。）"
    material = "合成新聞內文。" * 150 + late_fact  # > 700 characters
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: material + "\n\n" + text)
    prompts, judged = [], []
    monkeypatch.setattr(main, "_llm_chat", lambda prompt, *_a: prompts.append(prompt) or "受理窗口改到一樓了，公告已更新。")
    monkeypatch.setattr(restatement_judge, "_call_light_model",
                        lambda p: judged.append(p) or json.dumps({"labels": [{"i": 1, "label": "correction"}]}))
    share = f"合成標題：補助上路 | 合成新聞 | LINE TODAY\n\n{NEWS}"
    main._handle_web_research_question(research_env.event, "G_LINK", share)
    assert late_fact in prompts[0]
    assert judged and late_fact in judged[0]
    assert research_env.sent == ["受理窗口改到一樓了，公告已更新。"]


def test_share_summary_is_dropped(monkeypatch, research_env):
    page = "合成新聞內文：市府宣布每月補助兩千元，明年一月上路，受理窗口在三樓。"
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: page + "\n\n" + text)
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: "市府宣布每月補助兩千元，明年一月上路，受理窗口在三樓。")
    share = f"合成標題：補助上路 | 合成新聞 | LINE TODAY\n\n{NEWS}"
    main._handle_web_research_question(research_env.event, "G_LINK", share)
    assert research_env.sent == []
    assert _status() == "completed_no_reply"


def test_direct_answer_quoting_the_page_is_kept(monkeypatch, research_env):
    page = "合成公告：申請人必須在九月底以前繳交身分證明文件。"
    answer = "申請人必須在九月底以前繳交身分證明文件。"
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: page + "\n\n" + text)
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: answer)
    main._handle_web_research_question(research_env.event, "G_LINK", f"申請要繳交什麼文件 {NEWS}")
    assert research_env.sent == [answer]


@pytest.mark.parametrize("canned", [public_research.NO_EVIDENCE, public_research.NO_ANSWER])
def test_link_question_never_sends_a_canned_sentence(monkeypatch, research_env, canned):
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: PAGE_TEXT + "\n\n" + text)
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: canned)
    main._handle_web_research_question(research_env.event, "G_LINK", f"這是真的嗎？ {NEWS}")
    assert research_env.sent == []
    assert _status() == "completed_no_reply"


def test_url_characters_never_make_a_public_claim(monkeypatch, research_env):
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: text)
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: pytest.fail("no material"))
    main._handle_web_research_question(
        research_env.event, "G_LINK", "這篇？ https://example.test/今年價格上漲?share=synthetic-id")
    assert research_env.sent == []
    assert _status() == "completed_no_reply"


@pytest.mark.parametrize("url", [
    "https://example.test/a「今年價格」?share=synthetic-id",
    "https://www.google.com/search?q=「今年價格上漲」",   # bracketed query value, last in the link
])
def test_url_with_cjk_punctuation_is_still_not_a_claim(monkeypatch, research_env, url):
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: text)
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: pytest.fail("no material"))
    main._handle_web_research_question(research_env.event, "G_LINK", f"這篇？ {url}")
    assert research_env.sent == []
    assert _status() == "completed_no_reply"


def test_claim_typed_after_a_link_still_reports_missing_evidence(monkeypatch, research_env):
    # A later 「#」 is the user's hashtag, not the link going on.
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: text)
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: pytest.fail("no evidence"))
    main._handle_web_research_question(
        research_env.event, "G_LINK", "https://example.test/a，今年東南亞榴槤生產過剩，所以比較便宜。#新聞")
    assert research_env.sent == [public_research.NO_EVIDENCE]


def test_burst_keeps_the_answer_to_a_question_about_a_link(monkeypatch, burst_env):
    # Words besides the link: not a bare share, so the fact cache is consulted.
    monkeypatch.setattr(main.memory, "check_fact_cache", lambda *_a: None)
    page = "合成公告：申請人必須在九月底以前繳交身分證明文件。" * 3
    monkeypatch.setattr(main._requests, "get", lambda *_a, **_k: _html(page))
    answer = "申請人必須在九月底以前繳交身分證明文件。"
    monkeypatch.setattr(main, "_llm_chat", lambda prompt, *_a: burst_env.append(prompt) or answer)
    sent = []
    monkeypatch.setattr(main, "_reply", lambda _tok, text, **_kw: sent.append(text))
    monkeypatch.setattr(main.memory, "store_fact_cache", lambda *a: None)
    main._handle_burst_flush("GRP001", f"{NEWS}\n申請要繳交什麼文件？", "TOKEN001", ["MSG001"])
    assert sent == [answer]


def test_burst_drops_a_summary_of_a_shared_page(monkeypatch, burst_env):
    # Words besides the link: not a bare share, so the fact cache is consulted.
    monkeypatch.setattr(main.memory, "check_fact_cache", lambda *_a: None)
    page = "合成新聞內文：市府宣布每月補助兩千元，明年一月上路，受理窗口在三樓。" * 3
    monkeypatch.setattr(main._requests, "get", lambda *_a, **_k: _html(page))
    monkeypatch.setattr(main, "_llm_chat", lambda prompt, *_a: burst_env.append(prompt)
                        or "市府宣布每月補助兩千元，明年一月上路，受理窗口在三樓。")
    sent = []
    monkeypatch.setattr(main, "_reply", lambda _tok, text, **_kw: sent.append(text))
    main._handle_burst_flush("GRP001", f"合成群友：看看這個\n{NEWS}", "TOKEN001", ["MSG001"])
    assert sent == []
    assert main.memory.get_inbound_event_status("GRP001", "MSG001") == "completed_no_reply"


def test_command_form_request_keeps_its_direct_answer(monkeypatch, research_env):
    page = "合成公告：申請截止日期是十月三十一日。"
    answer = "申請截止日期是十月三十一日。"
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: page + "\n\n" + text)
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: answer)
    main._handle_web_research_question(research_env.event, "G_LINK", f"請查詢申請截止日期 {NEWS}")
    assert research_env.sent == [answer]


def test_question_right_after_a_link_keeps_its_answer(monkeypatch, research_env):
    page = "合成公告：申請人必須在九月底以前繳交身分證明文件。"
    answer = "申請人必須在九月底以前繳交身分證明文件。"
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: page + "\n\n" + text)
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: answer)
    main._handle_web_research_question(research_env.event, "G_LINK", f"{NEWS}，申請要繳交什麼文件?")
    assert research_env.sent == [answer]


def test_search_rows_keep_their_700_character_budget():
    long_row = {"url": "https://search.example.com/x", "full_text": "搜" * 2000}
    linked = _page_row("連" * 3000)
    text = main._format_web_research_sources([linked, long_row])
    assert "連" * 3000 in text
    assert "搜" * 701 not in text
