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
import time
from types import SimpleNamespace

import pytest
import requests

import fulltext_fetcher
import grounding_evidence
import line_mentions
import main
import public_research
import reply_guards
import reply_provenance
import safe_fetch
import source_aggregator
import web_scraper

QUESTION = "電動機車和油車的保養有什麼不同？"
ANSWER = "電動機車不用換機油和火星塞，平常多注意電池、煞車和輪胎；油車要定期換機油、齒輪油和空氣濾網。"
CLAIM = "今年東南亞榴槤生產過剩，所以比較便宜。"
QUOTA_429 = "429 RESOURCE_EXHAUSTED: GenerateRequestsPerDayPerProjectPerModel-FreeTier (synthetic)"


def _no_model(*_a, **_k):
    pytest.fail("no model may answer here")


def _no_page(*_a, **_k):
    raise safe_fetch.BlockedURL("these tests read no page")


@pytest.fixture(autouse=True)
def no_real_models(monkeypatch):
    """Every model a test needs is stubbed by that test; nothing else may run."""
    monkeypatch.setattr(main.gemini_client, "chat", _no_model)
    monkeypatch.setattr(main, "_gemini_llm_chat", _no_model)
    monkeypatch.setattr(main, "_llm_chat", _no_model)
    # 2026-10-29 (PLAN §10): a cited page is read only where a test serves one.
    monkeypatch.setattr(safe_fetch, "fetch", _no_page)
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


# ── names stay home; regulation numbers are judged after the search ──────────

@pytest.mark.parametrize("question, name", [
    ("測試成員甲推薦的拉麵好吃嗎？", "測試成員甲"),  # an alias (conftest's alias file)
    ("合成成員甲推薦的拉麵好吃嗎？", "合成成員甲"),  # a label in front of the group's turns
    ("@小明 台南有什麼好玩的景點？", "小明"),  # anyone tagged
    ("＠小明 台南有什麼好玩的景點？", "小明"),
    ("﹫小明 台南有什麼好玩的景點？", "小明"),
    ("媽去日本要簽證嗎？", "媽去"),  # one person's mother
    ("老媽祖先是哪裡人？", "老媽"),  # 老媽 is a deny term of its own, whatever 媽's mask does
    ("阿媽祖厝在哪裡？", "阿媽"),
])
def test_a_question_naming_someone_stays_home(research_env, nothing_crawled, caplog, question, name):
    assert public_research.is_plain_question(question)
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", question)
    assert research_env.sent == [public_research.NO_SOURCE_QUESTION]
    assert "gemini=named" in caplog.text and name not in caplog.text
    assert main._plain_question_searches["count"] == 0  # nothing went out, nothing counted


@pytest.mark.parametrize("question", [
    "日本人過年吃什麼？",  # 日本人 is masked before 本人 is looked for
    "大甲媽祖遶境有什麼好玩的？",  # nor is 媽祖 anyone's mother
    "澳門媽閣廟有什麼好玩的？",
    "請問，去日本要簽證嗎？",  # search_query keeps 「去日本要簽證嗎」: only courtesy was cut
])
def test_words_that_name_nobody_are_searched(monkeypatch, research_env, nothing_crawled, question):
    calls = []
    monkeypatch.setattr(main.gemini_client, "chat", _grounded(calls))
    main._handle_web_research_question(research_env.event, "G_PQ", question)
    assert research_env.sent == [ANSWER] and len(calls) == 1


@pytest.mark.parametrize("common_on", [False, True])
def test_unreadable_names_send_nothing(monkeypatch, research_env, nothing_crawled, caplog, tmp_path,
                                       common_on):
    monkeypatch.setattr(main, "_RESEARCH_COMMON_KNOWLEDGE", common_on)  # _llm_chat stays _no_model
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(tmp_path / "missing.json"))
    line_mentions.clear_alias_cache()
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    _ends_in_b(research_env, caplog, "names_unavailable")
    assert f"common={'skipped' if common_on else 'off'}" in caplog.text
    assert main._plain_question_searches["count"] == 0


# 2026-10-29 privacy review (REVIEW_gp1_plan_s9.md, REVIEW_gp2_plan_s9.md): the contract for the
# masks in front of 本人 and 媽.  The shared reply_guards version (TODO R2) must keep every verdict.
@pytest.mark.parametrize("question, named", [
    ("舅媽祖厝在哪裡？", True),  # 舅媽's ancestral house: the 媽 stays
    ("姑媽祖產怎麼分？", True),
    ("姑媽祖婆的房子過戶要繳多少贈與稅？", True),  # 祖婆 is a great-grandmother, not the goddess
    ("舅媽祖廟在哪裡？", True),  # after a kinship prefix it is 舅媽's temple
    ("姨媽祖宮在哪裡？", True),
    ("乾媽祖廟在哪裡？", True),
    ("契媽祖廟在哪裡？", True),
    ("\uf909媽祖廟在哪裡？", True),  # a compatibility 契
    ("義媽祖廟在哪裡？", True),
    ("姑媽祖廟在哪裡？", True),  # every prefix in the lookbehind is pinned (Phase 6)
    ("養媽祖廟在哪裡？", True),
    ("親媽祖廟在哪裡？", True),
    ("奶媽祖廟在哪裡？", True),
    ("乳媽祖廟在哪裡？", True),
    ("舅\u0301媽祖廟在哪裡？", True),  # a combining mark
    ("媽 祖廟在哪裡？", True),  # 媽 apart from 祖: the stripped view must not join them
    ("媽、祖廟在哪裡？", True),
    ("媽～祖廟在哪裡？", True),
    ("媽\u3000祖宮在哪裡？", True),
    ("王媽 祖廟在哪裡？", True),
    ("王媽、祖廟在哪裡？", True),
    ("王小明\U0001f240人入境可以帶幾瓶酒？", True),  # 〔本〕 folds into 本
    ("繼媽祖廟在哪裡？", True),
    ("伯媽祖廟在哪裡？", True),
    ("岳媽祖廟在哪裡？", True),
    ("舅媽祖 厝在哪裡？", True),  # a space is no clause end: _bare joins the words again
    ("舅媽祖，厝在哪裡？", True),
    ("舅 媽祖廟在哪裡？", True),
    ("舅\u200b媽祖廟在哪裡？", True),
    ("舅媽祖\u3000廟在哪裡？", True),
    ("舅媽祖\xa0厝在哪裡？", True),
    ("舅媽\ufa50廟在哪裡？", True),  # a compatibility 祖
    ("舅媽閣樓在哪裡？", True),
    ("王小明本人入境可以帶幾瓶酒？", True),  # 本人 after a name nobody configured
    ("王小明本\u2f08入境可以帶幾瓶酒？", True),  # characters that fold into 人
    ("王小明本\u319f入境可以帶幾瓶酒？", True),
    ("王旭日、本人入境可以帶幾瓶酒？", True),  # 本人 is read unstripped: 日、本人 is no 日本人
    ("王旭日「本人」入境可以帶幾瓶酒？", True),
    ("王旭日 本人入境可以帶幾瓶酒？", True),
    ("基本人權有哪些？", True),  # the cost of keeping 本人: fails closed
    ("日本人過年吃什麼？", False),
    ("大甲媽祖遶境有什麼好玩的？", False),
    ("大甲媽祖 遶境有什麼好玩的？", False),
    ("媽祖誕辰是哪一天？", False),
    ("澳門媽閣廟有什麼好玩的？", False),
    ("北港媽祖宮怎麼去？", False),
    ("白沙屯媽祖進香路線？", False),
    ("大甲媽祖文化節在哪裡？", False),
    ("大甲媽祖？", False),
    ("大甲媽祖", False),  # the end of the text
    ("大甲媽祖\ufe16", False),  # a vertical-form question mark folds into ?
    ("「大甲媽祖」", False),
    ("大甲媽\ufa50遶境有什麼好玩的？", False),
    ("全台最大媽祖廟在哪？", False),  # 大, 小 and 後 are no kinship prefix
    ("附近有小媽祖廟嗎？", False),
    ("之後媽祖誕辰怎麼慶祝？", False),
    ("大甲媽祖繞境幾天？", False),
    ("媽祖信仰從哪裡來？", False),
    ("大甲媽祖，好玩嗎？", False),  # the clause marks after NFKC: , ! 。
    ("大甲媽祖！", False),
    ("大甲媽祖。", False),
])
def test_the_masks_hide_only_the_goddess_and_japanese(question, named):
    assert main._plain_question_names(question, None) == ("named" if named else "")


def test_an_alias_that_is_a_common_word_still_counts(monkeypatch, tmp_path):
    assert main._plain_question_names("對方違規要怎麼檢舉", None) == ""
    aliases = tmp_path / "user_aliases.json"
    aliases.write_text('{"U_SYNTH": "對方"}', encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    line_mentions.clear_alias_cache()
    assert main._plain_question_names("對方違規要怎麼檢舉", None) == "named"


def test_the_common_layer_never_sees_a_named_question(monkeypatch, research_env, nothing_crawled, caplog):
    monkeypatch.setattr(main, "_RESEARCH_COMMON_KNOWLEDGE", True)  # _llm_chat stays _no_model
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", "測試成員甲推薦的拉麵好吃嗎？")
    assert research_env.sent == [public_research.NO_SOURCE_QUESTION]
    assert "gemini=named" in caplog.text and "common=skipped" in caplog.text


VISA_YES = "台灣護照去日本觀光不用辦簽證。"
VISA_DAYS = "免簽最多可停留90天。"


def test_a_regulation_number_nothing_gathered_backs_goes_but_the_answer_stays(monkeypatch, research_env,
                                                                              nothing_crawled):
    calls = []
    monkeypatch.setattr(main.gemini_client, "chat", _grounded(calls, answer=f"{VISA_YES}\n{VISA_DAYS}"))
    main._handle_web_research_question(research_env.event, "G_PQ", "去日本要簽證嗎？")
    assert research_env.sent == [VISA_YES]
    assert "數字另外寫成一句" in calls[0].prompt


@pytest.mark.parametrize("question, answer", [
    # one sentence: nothing is left, and reply_guards answers with its own fixed line
    ("去日本要簽證嗎？", "台灣護照去日本免簽，最多可停留90天。"),
    # what is left does not say how long: this path replaces it
    ("日本免簽可以待幾天？", f"{VISA_YES}\n{VISA_DAYS}"),
    ("日本免簽可以待幾天？", f"台灣護照自2005年起赴日免簽。\n{VISA_DAYS}"),  # a year is not how long
])
def test_an_asked_number_the_guard_drops_gets_the_fixed_line(monkeypatch, research_env, nothing_crawled,
                                                             question, answer):
    monkeypatch.setattr(main.gemini_client, "chat", _grounded([], answer=answer))
    main._handle_web_research_question(research_env.event, "G_PQ", question)
    assert research_env.sent == [reply_guards.REGULATORY_UNCONFIRMED_REPLY]


def test_an_asked_number_that_stays_keeps_the_answer(monkeypatch, research_env, nothing_crawled):
    # The question itself backs 90天; only the fine nobody asked about is dropped.
    stays = "是，台灣護照赴日免簽最多可停留90天。"
    answer = f"{stays}\n逾期停留最高罰款30萬日圓。"
    monkeypatch.setattr(main.gemini_client, "chat", _grounded([], answer=answer))
    main._handle_web_research_question(research_env.event, "G_PQ", "免簽最多可以待90天嗎？")
    assert research_env.sent == [stays]


def test_the_fixed_line_swap_is_for_plain_questions_only(monkeypatch, research_env):
    # The crawler found a usable row: batch 2's own handling of the dropped number stands.
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [{
        "url": "https://example.com/visa", "title": "合成來源",
        "full_text": "合成的來源內容，談到日本觀光免簽的規定與入境時要準備的文件。"}])
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: f"{VISA_YES}\n{VISA_DAYS}")
    main._handle_web_research_question(research_env.event, "G_PQ", "日本免簽可以待幾天？")
    assert research_env.sent == [VISA_YES]


def test_an_answer_the_guards_empty_is_b_not_silence(monkeypatch, research_env, nothing_crawled, caplog):
    monkeypatch.setattr(main.gemini_client, "chat", _grounded([]))
    monkeypatch.setattr(main, "_enforce_new_value_reply", lambda *_a, **_k: "")
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    assert research_env.sent == [public_research.NO_SOURCE_QUESTION]
    assert research_env.finished == []
    assert "plain question answer emptied by the reply guards" in caplog.text


# ── 2026-10-29 (PLAN §10): a number the cited pages hold may stay ────────────

SOURCE = "https://example.com/source"  # what _grounded cites by default


@pytest.fixture
def cited(monkeypatch):
    """safe_fetch.fetch serving synthetic cited pages; every call is recorded."""
    pages, calls = {}, []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        page = pages.get(url)
        if page is None:
            raise safe_fetch.BlockedURL("not served")
        return page

    monkeypatch.setattr(safe_fetch, "fetch", fetch)
    return SimpleNamespace(pages=pages, calls=calls)


def _cited_page(text, url="https://pages.example/visa"):
    resp = requests.Response()
    resp.status_code = 200
    resp._content = f"<html><body><p>{text}</p></body></html>".encode("utf-8")
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.url = url
    return resp


def test_a_number_the_cited_page_holds_is_said(monkeypatch, research_env, nothing_crawled, cited, caplog):
    cited.pages[SOURCE] = _cited_page("外籍旅客免簽入境，停留期間最長90天。")
    monkeypatch.setattr(main.gemini_client, "chat", _grounded([], answer=f"{VISA_YES}\n{VISA_DAYS}"))
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", "日本免簽可以待幾天？")
    assert research_env.sent == [f"{VISA_YES}\n{VISA_DAYS}"]
    assert [url for url, _kw in cited.calls] == [SOURCE]
    assert "grounding backing pass2=used" in caplog.text and "kept=1" in caplog.text
    assert "example.com" not in caplog.text and "停留期間" not in caplog.text  # counts only


def test_a_number_no_cited_page_holds_still_gets_the_fixed_line(monkeypatch, research_env, nothing_crawled,
                                                                cited):
    cited.pages[SOURCE] = _cited_page("外籍旅客免簽入境，請備妥回程機票。")
    monkeypatch.setattr(main.gemini_client, "chat", _grounded([], answer=f"{VISA_YES}\n{VISA_DAYS}"))
    main._handle_web_research_question(research_env.event, "G_PQ", "日本免簽可以待幾天？")
    assert research_env.sent == [reply_guards.REGULATORY_UNCONFIRMED_REPLY]
    assert cited.calls  # the page was read and did not hold 90天


def test_part_of_the_asked_numbers_backed_is_the_fixed_line(monkeypatch, research_env, nothing_crawled, cited):
    # REVIEW_gp1_plan_s10 #11: 「幾天」 must not be answered with the 6個月 the page backs.
    answer = "台灣護照赴日免簽，護照效期需6個月以上。\n免簽最多可停留90天。"
    cited.pages[SOURCE] = _cited_page("申請人護照效期需6個月以上。")
    monkeypatch.setattr(main.gemini_client, "chat", _grounded([], answer=answer))
    main._handle_web_research_question(research_env.event, "G_PQ", "日本免簽可以待幾天？")
    assert research_env.sent == [reply_guards.REGULATORY_UNCONFIRMED_REPLY]
    assert cited.calls


def test_a_yes_no_answer_keeps_what_the_page_backs(monkeypatch, research_env, nothing_crawled, cited):
    answer = f"{VISA_YES}\n{VISA_DAYS}\n逾期停留最高罰款30萬日圓。"
    cited.pages[SOURCE] = _cited_page("外籍旅客免簽入境，停留期間最長90天。")
    monkeypatch.setattr(main.gemini_client, "chat", _grounded([], answer=answer))
    main._handle_web_research_question(research_env.event, "G_PQ", "去日本要簽證嗎？")
    assert research_env.sent == [f"{VISA_YES}\n{VISA_DAYS}"]


def test_no_page_is_read_for_an_answer_without_a_dropped_number(monkeypatch, research_env, nothing_crawled,
                                                                cited):
    monkeypatch.setattr(main.gemini_client, "chat", _grounded([]))
    main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    assert research_env.sent == [ANSWER] and cited.calls == []


def test_no_page_is_read_late_in_the_reply(monkeypatch, research_env, nothing_crawled, cited, caplog):
    cited.pages[SOURCE] = _cited_page("停留期間最長90天。")
    monkeypatch.setattr(main.gemini_client, "chat", _grounded([], answer=f"{VISA_YES}\n{VISA_DAYS}"))
    monkeypatch.setattr(main, "_PAGE_BACKING_MIN_LEFT_SEC", 1000.0)
    with caplog.at_level(logging.INFO, logger="line_bot"):
        main._handle_web_research_question(research_env.event, "G_PQ", "日本免簽可以待幾天？")
    assert research_env.sent == [reply_guards.REGULATORY_UNCONFIRMED_REPLY] and cited.calls == []
    assert "grounding pages skipped reason=late" in caplog.text


def test_the_kill_switch_reads_no_page(monkeypatch, research_env, nothing_crawled, cited):
    monkeypatch.setenv("LINE_BOT_GROUNDING_BACKING", "0")
    cited.pages[SOURCE] = _cited_page("停留期間最長90天。")
    monkeypatch.setattr(main.gemini_client, "chat", _grounded([], answer=f"{VISA_YES}\n{VISA_DAYS}"))
    main._handle_web_research_question(research_env.event, "G_PQ", "日本免簽可以待幾天？")
    assert research_env.sent == [reply_guards.REGULATORY_UNCONFIRMED_REPLY] and cited.calls == []


def test_no_page_is_read_off_the_plain_question_path(monkeypatch, research_env, cited):
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [{
        "url": "https://example.com/visa", "title": "合成來源",
        "full_text": "合成的來源內容，談到日本觀光免簽的規定與入境時要準備的文件。"}])
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: f"{VISA_YES}\n{VISA_DAYS}")
    main._handle_web_research_question(research_env.event, "G_PQ", "日本免簽可以待幾天？")
    assert research_env.sent == [VISA_YES] and cited.calls == []


def _rendered(page_text):
    """A provider that renders what ``page_text`` holds, as the real one does after reading it."""
    def provide(text):
        needed = grounding_evidence.needed_quantities(text)
        found = grounding_evidence._scan(page_text, needed, time.monotonic() + 5)
        return "\n".join(sorted(grounding_evidence.render(q) for q in found))
    return provide


def _enforce(monkeypatch, reply, request, provider, *, all_or_nothing=False):
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    return main._enforce_new_value_reply(
        reply, source_text=request, request_text=request, context=[], addressed=True, searched=True,
        regulation_page_backing=provider, page_backing_all_or_nothing=all_or_nothing,
    )


def test_a_year_on_the_page_never_drops_a_sentence_kept_today(monkeypatch):
    # REVIEW_gp1_plan_s10 probe 5: 「民國112年修正」 must not turn on B1's year rule.
    request = "入境免稅額度還是2萬元嗎？"
    reply = "是的，2025年起入境旅客免稅額度仍是新臺幣2萬元。菸酒免稅額度是酒類1公升。"
    today = _enforce(monkeypatch, reply, request, None)
    paged = _enforce(monkeypatch, reply, request, _rendered("酒類1公升（不限瓶數），捲菸200支（民國112年修正）。"))
    assert "2025年起" in today and "1公升" not in today
    assert "2025年起" in paged and "1公升" in paged


def test_the_page_backs_numbers_and_nothing_else(monkeypatch):
    seen = []
    real_date, real_contact = main._date_claims_unbacked, main._contact_backing
    real_public = main.reply_policy.strip_unbacked_public_claims
    monkeypatch.setattr(main, "_date_claims_unbacked",
                        lambda *a, **k: seen.append(k.get("evidence_text")) or real_date(*a, **k))
    monkeypatch.setattr(main, "_contact_backing", lambda *a, **k: seen.append(a[3]) or real_contact(*a, **k))
    monkeypatch.setattr(main.reply_policy, "strip_unbacked_public_claims",
                        lambda *a, **k: seen.append(k.get("evidence_text")) or real_public(*a, **k))
    out = _enforce(monkeypatch, f"{VISA_YES}\n{VISA_DAYS}", "日本免簽可以待幾天？",
                   _rendered("停留期間最長90天。"), all_or_nothing=True)
    assert VISA_DAYS in out
    assert len(seen) >= 2 and all("90天" not in (ev or "") for ev in seen)


def _first(text="可以。", dropped=1):
    return reply_guards.GuardResult(text, negative_dropped=0, numbers_dropped=dropped)


def _second_pass(first, provider, run_guards, *, all_or_nothing=False):
    return main._page_backed_guards(
        first, "可以。免簽停留90天都可以。", provider, run_guards, lambda evidence: [evidence],
        evidence_text="", partial=False, search_only=True, all_or_nothing=all_or_nothing,
    )


def test_a_second_pass_that_loses_a_short_sentence_is_not_kept(monkeypatch):
    # REVIEW_gp2_plan_s10 F4: 「可以。」 inside 「…都可以。」 is no kept sentence.
    results = iter([reply_guards.RegulatoryResult("可以。", 1, 0),
                    reply_guards.RegulatoryResult("免簽停留90天都可以。", 0, 1)])
    monkeypatch.setattr(reply_guards, "strip_negative_coverage", lambda text, *_a: (text, 0))
    monkeypatch.setattr(reply_guards, "strip_unbacked_regulatory_numbers", lambda *_a: next(results))
    runs = []
    first = _first()
    assert _second_pass(first, lambda _t: "90天", runs.append) is first and runs == []


def _raise(*_a, **_k):
    raise RuntimeError("synthetic")


def test_errors_in_the_second_pass_keep_the_first(monkeypatch, caplog):
    first = _first()
    with caplog.at_level(logging.INFO, logger="line_bot"):
        assert _second_pass(first, _raise, lambda _e: pytest.fail("no second run")) is first
        assert _second_pass(first, lambda _t: "90天", _raise) is first
    assert caplog.text.count("pass2=error error_type=RuntimeError") == 2


def test_a_second_pass_that_disagrees_with_its_check_is_not_kept(monkeypatch):
    results = iter([reply_guards.RegulatoryResult("可以。", 1, 0), reply_guards.RegulatoryResult("可以。", 0, 1)])
    monkeypatch.setattr(reply_guards, "strip_negative_coverage", lambda text, *_a: (text, 0))
    monkeypatch.setattr(reply_guards, "strip_unbacked_regulatory_numbers", lambda *_a: next(results))
    first = _first()
    assert _second_pass(first, lambda _t: "90天", lambda _e: _first("可以。", dropped=1)) is first


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


@pytest.mark.parametrize("fixed", [
    main.UNVERIFIED_CONTACT_REPLY, main.UNVERIFIED_DATE_REPLY, reply_guards.REGULATORY_UNCONFIRMED_REPLY,
])
def test_the_note_never_follows_a_fixed_guard_line(monkeypatch, research_env, nothing_crawled,
                                                   gemini_from_memory, fixed):
    monkeypatch.setattr(main, "_RESEARCH_COMMON_KNOWLEDGE", True)
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: reply_provenance.reset() or ANSWER)
    monkeypatch.setattr(main, "_enforce_new_value_reply", lambda *_a, **_k: fixed)
    main._handle_web_research_question(research_env.event, "G_PQ", QUESTION)
    assert research_env.sent == [fixed]


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
    ("總統訪問日本？", "總統 訪問 日本"),  # only spaces and punctuation differ
    ("我家附近物價高嗎？", ""),  # private: nothing goes out at all
    # search_query keeps a regulation clause without its 「？」, and drops courtesy around it
    ("去日本要簽證嗎？", "去日本要簽證嗎"),
    ("請問，去日本要簽證嗎？", "去日本要簽證嗎"),
    ("請問一下，去日本要簽證嗎？", "去日本要簽證嗎"),
    ("不好意思，去日本要簽證嗎？", "去日本要簽證嗎"),
    ("哈哈，去日本要簽證嗎？😅", "去日本要簽證嗎"),
    ("去日本要簽證嗎？謝謝！", "去日本要簽證嗎"),
    ("下個月去日本，需要簽證？", "日本 需要簽證"),  # the 「？」 after the kept clause still asks
    ("下個月去日本，需要簽證…？", "日本 需要簽證"),
    ("下個月去日本，需要簽證！？", "日本 需要簽證"),
    ("去帛琉玩，簽證要辦嗎？", ""),  # not a listed place: the subject cannot go
    ("？", ""),
])
def test_complete_search_query(text, expected):
    assert public_research.complete_search_query(text) == expected


def test_a_long_laugh_is_checked_in_linear_time():
    # Nested repeats took seconds at 24 哈 and never finished at 60, holding the GIL.
    started = main.time.monotonic()
    assert public_research._COURTESY.fullmatch("哈" * 60 + "好") is None
    assert public_research.complete_search_query("哈" * 60 + "好好笑，去日本要簽證嗎？") == ""
    assert public_research.complete_search_query("哈" * 60 + "，去日本要簽證嗎？") == "去日本要簽證嗎"
    assert main.time.monotonic() - started < 1.0


def test_names_are_compared_as_the_guards_compare_them():
    assert public_research.mentions_any("ＡＭＹ想問", ["amy"])  # NFKC, case
    assert public_research.mentions_any("王 大同推薦的店", ["王大同"])  # spaces ignored
    assert not public_research.mentions_any("合成的問題", ["王大同"])
    assert public_research.mentions_any("合成的問題", [])  # no list: nobody can say whom it names


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
