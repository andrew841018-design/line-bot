"""Research path: plain questions whose search found nothing usable (2026-10-28).

Family members asked ordinary questions without @咪寶; the no-mention route sent
them to the research path.  DuckDuckGo answered 202 (a bot check), Google News
RSS rows carried titles only and the full-text fetch of their news.google.com
links came back empty, so no row counted as evidence and the bot answered
「…還無法核實這個說法。」 as if a forwarded claim had been checked.  Andrew:
find it (「打通他」); 「這次沒查到…」 (B) only when every layer fails.  The
search that works is Google's, through Gemini's grounding; Bing's results page
was tried and served unrelated results to the bot.
Synthetic questions only: topic, wording and speaker all differ from the chat.
Nothing here reaches the network or a real model.
"""
from contextlib import nullcontext
import logging
from types import SimpleNamespace

import pytest

import fulltext_fetcher
import main
import public_research
import reply_provenance
import source_aggregator
import web_scraper

QUESTION = "電動機車和油車的保養有什麼不同？"
ANSWER = "電動機車不用換機油和火星塞，平常多注意電池、煞車和輪胎；油車要定期換機油、齒輪油和空氣濾網。"
CLAIM = "今年東南亞榴槤生產過剩，所以比較便宜。"
QUOTA_429 = "429 RESOURCE_EXHAUSTED: GenerateRequestsPerDayPerProjectPerModel-FreeTier (synthetic)"


def _no_model(*_a, **_k):
    pytest.fail("no model may answer here")


@pytest.fixture(autouse=True)
def no_real_models(monkeypatch):
    """Every model a test needs is stubbed by that test; nothing else may run."""
    monkeypatch.setattr(main.gemini_client, "chat", _no_model)
    monkeypatch.setattr(main, "_gemini_llm_chat", _no_model)
    monkeypatch.setattr(main, "_llm_chat", _no_model)
    # raising=False: on the code before 2026-10-28 there is no counter, and the
    # repro below must fail on its assertion there, not here.
    monkeypatch.setattr(main, "_plain_question_searches", {"date": "", "count": 0}, raising=False)


@pytest.fixture
def research_env(monkeypatch):
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    monkeypatch.setattr(main, "_thinking_indicator", lambda *_: nullcontext())
    # what the group said lately and what the bot remembers about the speaker
    monkeypatch.setattr(main.memory, "get_context", lambda *_: [("user", "合成成員甲：合成的家庭對話")])
    monkeypatch.setattr(main.memory, "top_facts", lambda *a, **k: ["合成事實：合成成員甲的私事"])
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_: [{"note": "合成筆記"}])
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_: False)
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    finished = []
    monkeypatch.setattr(main, "_finish_research_without_reply", lambda *a, **k: finished.append(True))
    sent = []
    monkeypatch.setattr(main, "_reply", lambda _tok, text, **_kw: sent.append(text))
    event = SimpleNamespace(source=SimpleNamespace(user_id="U_TEST"), reply_token="T_PQ", message=None)
    return SimpleNamespace(event=event, sent=sent, finished=finished)


def _grounded(calls, answer=ANSWER, urls=("https://example.com/source",)):
    """gemini_client.chat as it returns a Google-grounded answer."""
    def chat(prompt, context, facts, pnotes):
        calls.append(SimpleNamespace(prompt=prompt, context=context, facts=facts, pnotes=pnotes))
        reply_provenance.record_grounding(
            {"model": "synthetic", "urls": list(urls), "supported_segments": [], "queries": []})
        if urls:
            reply_provenance.mark_searched()
        return answer
    return chat


# ── the crawler as it stood on 2026-10-28, then Google's search ──────────────

@pytest.fixture
def todays_crawler(monkeypatch):
    """DDG blocked, news rows with titles only, no wiki hit, no full text."""
    monkeypatch.setattr(source_aggregator, "_fetch_ddg", lambda *_a: [])
    monkeypatch.setattr(source_aggregator, "_fetch_wiki", lambda *_a: None)
    monkeypatch.setattr(source_aggregator, "_fetch_gnews", lambda *_a: [
        {"title": f"合成新聞標題{i}", "url": f"https://news.google.com/rss/articles/SYNTH{i}",
         "snippet": "", "source_query": "q", "engine": "gnews"}
        for i in range(4)
    ])
    monkeypatch.setattr(fulltext_fetcher, "_fetch_one", lambda *_a: None)


def test_a_plain_question_is_searched_for_not_called_a_claim(monkeypatch, research_env, todays_crawler,
                                                             caplog):
    # The no-mention route sends this question here, as a question (addressed).
    assert main._is_web_research_question(QUESTION)
    calls = []
    monkeypatch.setattr(main.gemini_client, "chat", _grounded(calls))
    with caplog.at_level(logging.INFO, logger="line_bot"):
        assert main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    assert research_env.sent == [ANSWER]
    assert "Google 搜尋" in calls[0].prompt and public_research.search_query(QUESTION) in calls[0].prompt
    # The model writes its own search queries: no chat history, facts or notes go with it.
    assert (calls[0].context, calls[0].facts, calls[0].pnotes) == ([], [], None)
    assert "合成" not in calls[0].prompt.replace(QUESTION, "")
    # which layer came back empty, and which answered, in counts only
    assert "web research sources rows=4 with_text=0 full_text=0" in caplog.text
    assert "plain question answered layer=gemini" in caplog.text
    assert "電動機車" not in caplog.text


def test_a_grounded_answer_may_say_it_searched(monkeypatch, research_env, todays_crawler):
    # 「我查了一下」 is dropped as a pretend search unless the guard hears a search ran.
    said = "我查了一下：電動機車主要檢查電池與煞車，油車要定期換機油。"
    monkeypatch.setattr(main.gemini_client, "chat", _grounded([], answer=said))
    main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    assert research_env.sent == [said]


def test_a_blocked_ddg_leaves_a_trace_but_not_the_question(monkeypatch, caplog):
    monkeypatch.setattr(web_scraper.requests, "post",
                        lambda *_a, **_k: SimpleNamespace(status_code=202, text="bot check"))
    monkeypatch.setattr(web_scraper, "_polite_sleep", lambda: None)
    with caplog.at_level(logging.INFO, logger="web_scraper"):
        assert web_scraper.search_duckduckgo("合成的秘密查詢") == []
    assert "DDG search status=202" in caplog.text
    assert "秘密" not in caplog.text


# ── the layers after the crawler (plain questions only) ──────────────────────

@pytest.fixture
def nothing_crawled(monkeypatch):
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [])


def _ends_in_b(research_env, caplog, reason):
    assert research_env.sent == [public_research.NO_SOURCE_QUESTION]
    assert "說法" not in research_env.sent[0]
    assert f"gemini={reason}" in caplog.text
    assert "電動機車" not in caplog.text  # reasons only, never the question
    assert research_env.finished == []


@pytest.mark.parametrize("chat, reason", [
    (_grounded([], urls=()), "ungrounded"),  # answered from memory: not a search
    (lambda *_a: reply_provenance.mark_searched() or ANSWER, "ungrounded"),  # marked, but no sources
    (_grounded([], answer=""), "empty"),
    (_grounded([], answer="我會再搜尋看看最新資料。"), "promise"),
])
def test_b_only_after_every_layer_failed(monkeypatch, research_env, nothing_crawled, caplog, chat, reason):
    monkeypatch.setattr(main.gemini_client, "chat", chat)
    # B is a fixed line: the guard would drop 「沒查到…資料」 as a search nobody ran
    monkeypatch.setattr(main, "_enforce_new_value_reply", _no_model)
    with caplog.at_level(logging.INFO, logger="line_bot"):
        assert main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    _ends_in_b(research_env, caplog, reason)
    assert "crawler=no_usable" in caplog.text


def test_b_survives_the_outbound_checks():
    line = public_research.NO_SOURCE_QUESTION
    assert main._prepare_outbound_text(line) == line
    assert not main._is_system_status_outbound(line)


def test_no_gemini_call_past_the_quota(monkeypatch, research_env, nothing_crawled, caplog):
    monkeypatch.setattr(main, "_quota_exhausted", lambda: True)
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    _ends_in_b(research_env, caplog, "quota")


def test_a_429_marks_the_quota_and_never_falls_back(monkeypatch, research_env, nothing_crawled, caplog):
    marked = []

    def quota(*_a):
        raise RuntimeError(QUOTA_429)

    monkeypatch.setattr(main.gemini_client, "chat", quota)
    monkeypatch.setattr(main, "_mark_quota_exhausted", lambda: marked.append(True))
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    _ends_in_b(research_env, caplog, "quota")
    assert marked == [True]


def test_a_per_minute_429_does_not_stop_every_reply(monkeypatch, research_env, nothing_crawled, caplog):
    marked = []

    def per_minute(*_a):
        raise RuntimeError("429 RESOURCE_EXHAUSTED: free_tier_requests PerMinute (synthetic)")

    monkeypatch.setattr(main.gemini_client, "chat", per_minute)
    monkeypatch.setattr(main, "_mark_quota_exhausted", lambda: marked.append(True))
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    _ends_in_b(research_env, caplog, "quota")
    assert marked == []


def test_no_gemini_search_when_the_reply_token_is_nearly_spent(monkeypatch, research_env, nothing_crawled,
                                                               caplog):
    # LINE's event time: the webhook came 30 s ago, the token's budget is 45 s
    research_env.event.timestamp = int((main.time.time() - 30) * 1000)
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    _ends_in_b(research_env, caplog, "late")


def test_a_failing_gemini_still_ends_in_b(monkeypatch, research_env, nothing_crawled, caplog):
    def boom(*_a):
        raise RuntimeError("503 UNAVAILABLE (synthetic)")

    monkeypatch.setattr(main.gemini_client, "chat", boom)
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    _ends_in_b(research_env, caplog, "error")


def test_the_daily_share_of_gemini_is_kept(monkeypatch, research_env, nothing_crawled, caplog):
    calls = []
    monkeypatch.setattr(main.gemini_client, "chat", _grounded(calls))
    monkeypatch.setattr(main, "_PLAIN_QUESTION_SEARCH_DAILY_CAP", 1)
    main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    research_env.sent.clear()
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    assert len(calls) == 1
    _ends_in_b(research_env, caplog, "cap")


def test_no_gemini_search_after_the_crawler_timed_out(monkeypatch, research_env, caplog):
    def timed_out(_collector, _text, *, timeout=16.0, report=None):
        report["reason"] = "timeout"
        return []

    monkeypatch.setattr(public_research, "collect", timed_out)
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    _ends_in_b(research_env, caplog, "late")
    assert "crawler=timeout" in caplog.text


def test_nothing_research_may_not_search_is_sent_to_gemini(monkeypatch, research_env, caplog):
    monkeypatch.setattr(main, "_collect_web_research_sources", _no_model)
    monkeypatch.setattr(public_research, "search_query", lambda _t: "")
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    _ends_in_b(research_env, caplog, "no_search")
    assert "crawler=no_search" in caplog.text


@pytest.mark.parametrize("question, sent_on, never", [
    # search_query keeps only 「物價高嗎」: the place comes back, so it is the same question
    ("去日本旅遊，物價高嗎？", "日本 旅遊 物價高嗎", "去日本"),
    # names and what they did stay home; the city comes back
    ("王大同去高雄開店，房租價格高嗎？", "高雄 房租價格高嗎", "王大同"),
])
def test_a_question_cut_short_gets_its_public_subject_back(monkeypatch, research_env, nothing_crawled,
                                                           caplog, question, sent_on, never):
    assert public_research.is_plain_question(question)
    calls = []
    monkeypatch.setattr(main.gemini_client, "chat", _grounded(calls))
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", question)
    assert research_env.sent == [ANSWER]
    assert sent_on in calls[0].prompt and never not in calls[0].prompt
    assert "restored=True" in caplog.text


def test_a_question_without_a_public_subject_to_restore_ends_in_b(research_env, nothing_crawled, caplog):
    # only 「月費價格多少」 may go out, and nothing public in the rest says what it is about
    question = "Gogoro換電方案，月費價格多少？"
    assert public_research.is_plain_question(question)
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", question)
    assert research_env.sent == [public_research.NO_SOURCE_QUESTION]
    assert "gemini=reduced_unrecoverable" in caplog.text


def test_an_answer_the_guards_empty_is_b_not_silence(monkeypatch, research_env, nothing_crawled, caplog):
    monkeypatch.setattr(main.gemini_client, "chat", _grounded([]))
    monkeypatch.setattr(main, "_enforce_new_value_reply", lambda *_a, **_k: "")
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    assert research_env.sent == [public_research.NO_SOURCE_QUESTION]
    assert research_env.finished == []
    assert "plain question answer emptied by the reply guards" in caplog.text


# ── claims keep the fact-check wording; other routes are unchanged ───────────

@pytest.mark.parametrize("text", [
    CLAIM,
    "網傳喝綠茶會傷胃，是真的嗎？",
    "聽說電動機車電池五年就要換？",
    "明年起機車要全面改電動，對嗎？",
    "今年咖啡產量下降，價格上漲了嗎？",
    "今天總統宣布停火了嗎",
    "幫我查證這種保養方式有沒有用？",
])
def test_claims_still_get_no_evidence_and_no_model(research_env, nothing_crawled, text):
    assert main._handle_web_research_question(research_env.event, "G_PQ", text)
    assert research_env.sent == [public_research.NO_EVIDENCE]


def test_quoted_and_unaddressed_questions_are_unchanged(research_env, nothing_crawled):
    main._handle_web_research_question(
        research_env.event, "G_PQ", QUESTION, quoted_context="（引用）合成的前一則訊息")
    assert research_env.sent == [public_research.NO_EVIDENCE]
    research_env.sent.clear()
    main._handle_web_research_question(research_env.event, "G_PQ", QUESTION, addressed=False)
    assert research_env.sent == [] and research_env.finished == [True]


# ── the common-knowledge layer: off until Andrew decides ─────────────────────

@pytest.fixture
def gemini_from_memory(monkeypatch):
    monkeypatch.setattr(main.gemini_client, "chat", _grounded([], urls=()))


def test_common_knowledge_is_off_by_default(research_env, nothing_crawled, gemini_from_memory):
    assert main._RESEARCH_COMMON_KNOWLEDGE is False
    main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    assert research_env.sent == [public_research.NO_SOURCE_QUESTION]


def test_common_knowledge_when_switched_on_is_labelled(monkeypatch, research_env, nothing_crawled,
                                                       gemini_from_memory):
    monkeypatch.setattr(main, "_RESEARCH_COMMON_KNOWLEDGE", True)
    with_digits = "油車大約每 1000 到 3000 公里換一次機油；電動機車不用換機油，主要檢查電池和煞車。"
    calls = []
    monkeypatch.setattr(main, "_llm_chat",
                        lambda *a: calls.append(a) or reply_provenance.reset() or with_digits)
    main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    assert research_env.sent == [f"{with_digits}\n{public_research.COMMON_KNOWLEDGE_NOTE}"]
    assert calls[0][1:] == ([], [], None)  # no chat history here either
    # The label survives the last outbound checks, digits and all.
    assert main._prepare_outbound_text(research_env.sent[0]) == research_env.sent[0]
    assert not main._is_system_status_outbound(research_env.sent[0])


@pytest.mark.parametrize("text, memory_reply", [
    ("台北明天天氣怎麼樣？", ANSWER),  # changes with time: never from memory
    ("什麼是痛風？", ANSWER),  # health: never from memory
    (QUESTION, "我沒辦法確認目前的保養建議，請以原廠公告為準。"),
])
def test_common_knowledge_never_guesses(monkeypatch, research_env, nothing_crawled, gemini_from_memory,
                                        text, memory_reply):
    monkeypatch.setattr(main, "_RESEARCH_COMMON_KNOWLEDGE", True)
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: memory_reply)
    main._handle_web_research_question(research_env.event, "G_PQ", text)
    assert research_env.sent == [public_research.NO_SOURCE_QUESTION]


def test_the_note_never_follows_a_fixed_guard_line(monkeypatch, research_env, nothing_crawled,
                                                   gemini_from_memory):
    monkeypatch.setattr(main, "_RESEARCH_COMMON_KNOWLEDGE", True)
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: reply_provenance.reset() or ANSWER)
    monkeypatch.setattr(main, "_enforce_new_value_reply", lambda *_a, **_k: main.UNVERIFIED_CONTACT_REPLY)
    main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    assert research_env.sent == [main.UNVERIFIED_CONTACT_REPLY]


# ── the classifiers ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    QUESTION,
    "多肉植物冬天要怎麼照顧？",
    "日本自由行要注意什麼",
    "什麼是痛風？",
    "台北到台中開車要幾個小時",
    "/問 電動機車和油車差在哪",
    "推薦台南好玩的景點",
    "求推薦適合長輩的運動",
    "介紹一下多肉植物",
])
def test_plain_questions(text):
    assert public_research.is_plain_question(text)


@pytest.mark.parametrize("text", [
    CLAIM,
    "幫我查這件事",
    "最近天氣很冷",
    "這幾個景點比較推薦",
    "這家很推薦",
    "推薦這家，超好吃",
    "推薦大家去看這部電影",
    "這兩家餐廳評價不同，比較推薦第一家",
    "最近幾天天氣很冷",
    "最近天氣都二十幾度",
    "這間店已經開了幾十年",
    "網傳喝綠茶會傷胃，是真的嗎？",
    "明年起機車要全面改電動，對嗎？",
    "Is it true that scooters need no oil?",
    "幫我查證這種保養方式有沒有用？",
    "今天總統宣布停火了嗎",
    "我家的車要怎麼保養？",  # private: never searched at all
    "電動機車" + "的保養" * 30 + "有什麼不同？",
])
def test_not_plain_questions(text):
    assert not public_research.is_plain_question(text)


@pytest.mark.parametrize("text, expected", [
    (QUESTION, True),
    ("多肉植物冬天要怎麼照顧？", True),
    ("什麼是痛風？", False),
    ("定期定額和單筆投資差在哪？", False),
    ("台北明天天氣怎麼樣？", False),
    ("現在高鐵票價多少？", False),
    ("台中有推薦的餐廳嗎？", False),
    ("最近油價會漲嗎？", False),
    ("美股為什麼大跌？", False),
    ("大家晚上要去哪裡？", False),
])
def test_answerable_from_memory(text, expected):
    assert public_research.answerable_from_memory(text) is expected


@pytest.mark.parametrize("text, expected", [
    (QUESTION, QUESTION),  # nothing was cut
    ("去日本旅遊，物價高嗎？", "日本 旅遊 物價高嗎"),
    ("日本自由行，價格大概多少？", "日本 自由行 價格大概多少"),
    ("去沖繩玩，物價高嗎？", "沖繩 物價高嗎"),
    ("王大同去高雄開店，房租價格高嗎？", "高雄 房租價格高嗎"),
    ("張嘉義去日本開會，物價高嗎？", "日本 物價高嗎"),  # 嘉義 here is part of a name
    ("香港腳很難好，藥膏價格多少？", ""),  # not the city
    ("Gogoro換電方案，月費價格多少？", ""),  # no public subject to bring back
    ("總統訪問日本是為了什麼？", ""),  # cut to keywords: the question itself is gone
    ("我家附近物價高嗎？", ""),  # private: nothing goes out at all
])
def test_complete_search_query(text, expected):
    assert public_research.complete_search_query(text) == expected


def test_collect_reports_why_nothing_came_back():
    report = {}
    assert public_research.collect(lambda _q: [{"url": "https://example.com", "snippet": "短"}],
                                   QUESTION, report=report) == []
    assert report == {"reason": "no_usable", "rows": 1}
    report = {}
    assert public_research.collect(lambda _q: [], "我家地址", report=report) == []
    assert report == {"reason": "no_query"}
    report = {}

    def raises(_q):
        raise RuntimeError("synthetic")

    assert public_research.collect(raises, QUESTION, report=report) == []
    assert report == {"reason": "error"}
