"""2026-09-26 Andrew: replies must not summarize what users said.

Only corrections, suggestions and new information may reach LINE.  All
fixtures are synthetic; no real chat content.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import gemini_client  # noqa: E402
import reply_policy  # noqa: E402
import restatement_judge  # noqa: E402

USER_POST = (
    "我發現用甲商店的網站買咖啡豆比較便宜，但在乙平台買反而更貴，"
    "原來乙平台是轉單給甲商店，所以中間多一層手續費，而且出貨也比較慢。"
    "直接在甲商店下單才能直接找到原廠。"
)


# ── deterministic check ──────────────────────────────────────────────────────

@pytest.mark.parametrize("reply", [
    "甲商店和乙平台的價格確實有差異。\n\n如果常買，可以先加入甲商店會員拿首購折扣。",
    "這篇新聞指出，烤肉時生熟食要分開。\n\n另外，豬肉中心溫度要到 71°C 才算熟。",
    "這個影片在討論兩國會談的影響。\n\n會談日期其實延後到下個月。",
    "影片中提到會談地點在海邊。\n\n實際地點已改到首都。",
    "主持人在節目中探討了關稅議題。\n\n關稅生效日是明年一月。",
    # 2026-09-26 shared-video retellings
    "這則影片主要表達了主持人對兩國關係的感嘆。\n\n會談其實延後到下個月。",
    "這支影片的主題是「某地買車很划算」，內容可能著重於電動車等面向。\n\n補助其實只到年底。",
    "這部影片的重點在於「新的補助方案」。\n\n申請期限其實是十月底。",
])
def test_restating_openers_are_detected(reply):
    assert reply_policy.restatement_reason(reply, USER_POST).startswith("restatement")


@pytest.mark.parametrize("reply", [
    "這個說法不對，乙平台其實有自己的倉庫，不是單純轉單。",
    "確實便宜，但甲商店不含運，滿額前反而比較貴。",
    "這個影片是假的，畫面是三年前的舊新聞。",
    "研究顯示，咖啡豆烘焙後兩週內風味最好。",
    "可以改用定期訂購，通常再便宜一成。",
    "先比較含運總價，再決定在哪裡買。",
    "這篇報導提到的價格是去年的，今年已經改成含運價。",
    # Evaluations and advice about the material are new value, not retelling.
    "這部影片呈現倖存者偏差，建議比對完整樣本。",
    "這則報導聚焦單一案例，尚不足以推論全體。",
    "訊息重點是要大家別點連結，這是詐騙。",
    "新聞重點是央行升息半碼，房貸族每月多繳約千元。",
    "內容重點是：補助只限65歲以上。",
    "這部影片的重點在於提醒長輩小心假投資，建議直接打165查證。",
    "這則影片表達的立場過於片面。",
    "這篇文章的分析有漏洞，樣本只有十人。",
    "這支短影音提醒長輩小心假投資，建議直接打165查證。",
    "這支影片的主題是倖存者偏差，樣本只含成功者會高估報酬。",
])
def test_new_value_sentences_pass(reply):
    assert reply_policy.restatement_reason(reply, USER_POST) == ""
    assert reply_policy.strip_restatement(reply, USER_POST) == reply


@pytest.mark.parametrize("question", ["明天會下雨嗎？", "這樣買比較便宜對不對", "is it cheaper?"])
def test_agreeing_answer_to_a_question_is_not_an_echo(question):
    assert reply_policy.restatement_reason("沒錯，明天下午開始會下雨。", question) == ""


def test_verbatim_copy_of_user_sentence_is_detected_and_stripped():
    reply = (
        "原來乙平台是轉單給甲商店，所以中間多一層手續費，而且出貨也比較慢。\n\n"
        "甲商店每月一號有會員日，折扣更多。"
    )
    assert reply_policy.restatement_reason(reply, USER_POST) == "restatement: copied"
    assert reply_policy.strip_restatement(reply, USER_POST) == "甲商店每月一號有會員日，折扣更多。"


def test_quality_gate_does_not_treat_research_material_as_restatement():
    research_prompt = "本機爬蟲資料：甲商店每月一號有會員日，折扣更多，會員另享免運。"
    reply = "甲商店每月一號有會員日，折扣更多，會員另享免運。"
    assert gemini_client._violates_quality(reply, research_prompt) == (False, "")
    assert reply_policy.restatement_reason(reply, research_prompt) == "restatement: copied"


@pytest.mark.parametrize("line", [
    "你問的「乙平台是轉單給甲商店，所以中間多一層手續費」有一半對。",
    "事項：原來乙平台是轉單給甲商店，所以中間多一層手續費",
    "出處：原來乙平台是轉單給甲商店，所以中間多一層手續費",
])
def test_quotes_sources_and_operation_lines_are_not_copies(line):
    assert "copied" not in reply_policy.restatement_reason(line, USER_POST)


def test_reply_with_only_restatement_strips_to_empty():
    reply = "甲商店和乙平台的價格確實有差異。\n\n這篇文章提到乙平台會轉單。"
    assert reply_policy.strip_restatement(reply, USER_POST) == ""


@pytest.mark.parametrize("request_text", [
    "幫我整理重點", "這影片在講什麼？", "請翻譯成英文", "summarize this", "摘要一下",
])
def test_explicit_summary_request_is_exempt(request_text):
    reply = "這篇文章提到乙平台會轉單給甲商店。"
    assert reply_policy.restatement_reason(reply, USER_POST, user_text=request_text) == ""
    assert reply_policy.strip_restatement(reply, USER_POST, user_text=request_text) == reply


def test_contract_asks_for_corrections_suggestions_and_new_information():
    contract = reply_policy.NO_REPEAT_CONTRACT
    for marker in ("糾正", "建議", "新資訊", "確實", "空字串"):
        assert marker in contract


# ── Gemini quality gate ───────────────────────────────────────────────────────

def _gate(monkeypatch, first_reply, retry_texts):
    calls = []
    responses = iter(retry_texts)

    def send(prompt):
        calls.append(prompt)
        return SimpleNamespace(text=next(responses))

    monkeypatch.setattr(gemini_client, "_track_usage", lambda *_: None)
    monkeypatch.setattr(gemini_client, "_extract_grounding_urls", lambda *_: [])
    alerts = []
    monkeypatch.setattr(gemini_client, "_alert_quality_violation", lambda *a: alerts.append(a))
    monkeypatch.setattr(gemini_client, "_log_quality_violation", lambda *a: alerts.append(a))
    out = gemini_client._quality_gate(
        SimpleNamespace(send_message=send), first_reply, [], USER_POST, "GRP001"
    )
    return out, calls, alerts


def test_quality_gate_rewrites_restatement_once(monkeypatch):
    out, calls, _ = _gate(
        monkeypatch,
        "甲商店和乙平台的價格確實有差異。",
        ["乙平台其實有自己的倉庫，不是單純轉單；價差主要是平台抽成。"],
    )
    assert len(calls) == 1
    assert "只留三種內容" in calls[0]
    assert out == "乙平台其實有自己的倉庫，不是單純轉單；價差主要是平台抽成。"


def test_quality_gate_strips_when_rewrite_still_restates(monkeypatch):
    out, calls, alerts = _gate(
        monkeypatch,
        "甲商店和乙平台的價格確實有差異。",
        ["這篇文章提到乙平台會轉單。\n\n甲商店週末有免運。"],
    )
    assert len(calls) == 1  # no extra retries for restatement
    assert out == "甲商店週末有免運。"
    assert alerts == []


def test_quality_gate_goes_silent_when_nothing_new(monkeypatch):
    out, _, _ = _gate(
        monkeypatch,
        "甲商店和乙平台的價格確實有差異。",
        ["兩邊價格確實不同。"],
    )
    assert out == ""


# ── semantic judge ───────────────────────────────────────────────────────────

REPLY = "乙平台會多收手續費，出貨較慢。\n\n甲商店可以用 LINE Pay 回饋 5%。"


@pytest.fixture
def judge_on(monkeypatch):
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "1")


def test_judge_drops_sentences_labelled_restate(monkeypatch, judge_on):
    prompts = []
    monkeypatch.setattr(restatement_judge, "_call_light_model", lambda p: prompts.append(p) or json.dumps(
        {"labels": [{"i": 1, "label": "restate"}, {"i": 2, "label": "suggestion"}]}))
    out = restatement_judge.filter_restatements(REPLY, USER_POST, [("bot", "先前回答")])
    assert out == "甲商店可以用 LINE Pay 回饋 5%。"
    assert "先前回答" in prompts[0] and USER_POST in prompts[0]


def test_judge_all_restate_means_no_reply(monkeypatch, judge_on):
    monkeypatch.setattr(restatement_judge, "_call_light_model", lambda p: json.dumps(
        {"labels": [{"i": 1, "label": "restate"}, {"i": 2, "label": "restate"}]}))
    assert restatement_judge.filter_restatements(REPLY, USER_POST) == ""


@pytest.mark.parametrize("raw", [
    None, "not json", json.dumps({"labels": [{"i": 1, "label": "restate"}]}),
    json.dumps({"labels": [{"i": 1, "label": "restate"}, {"i": 2, "label": "???"}]}),
])
def test_judge_fails_open(monkeypatch, judge_on, raw):
    monkeypatch.setattr(restatement_judge, "_call_light_model", lambda p: raw)
    assert restatement_judge.filter_restatements(REPLY, USER_POST) == REPLY


def test_judge_skips_explicit_request_disabled_flag_and_tiny_source(monkeypatch, judge_on):
    monkeypatch.setattr(restatement_judge, "_call_light_model",
                        lambda p: pytest.fail("judge should not be called"))
    assert restatement_judge.filter_restatements(REPLY, USER_POST, request_text="幫我摘要") == REPLY
    assert restatement_judge.filter_restatements(REPLY, "好") == REPLY
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    assert restatement_judge.filter_restatements(REPLY, USER_POST) == REPLY


# ── main reply paths ─────────────────────────────────────────────────────────

def test_enforce_new_value_reply_combines_regex_and_judge(monkeypatch, judge_on):
    import main

    monkeypatch.setattr(restatement_judge, "_call_light_model", lambda p: json.dumps(
        {"labels": [{"i": 1, "label": "new"}]}))
    out = main._enforce_new_value_reply(
        "甲商店和乙平台的價格確實有差異。\n\n甲商店週末有免運。",
        source_text=USER_POST, request_text=USER_POST, context=[],
    )
    assert out == "甲商店週末有免運。"


def test_enforce_new_value_reply_fails_open_on_error(monkeypatch):
    import main

    monkeypatch.setattr(reply_policy, "strip_restatement",
                        MagicMock(side_effect=RuntimeError("boom")))
    assert main._enforce_new_value_reply(
        "任何回覆。", source_text=USER_POST, request_text="", context=[]
    ) == "任何回覆。"


def test_explicit_reply_that_only_restates_is_not_sent(monkeypatch):
    from linebot.v3.webhooks import MessageEvent, TextMessageContent

    import main

    evt = MagicMock(spec=MessageEvent)
    evt.message = TextMessageContent(id="MSG900", text="咪寶 " + USER_POST, quoteToken="qt")
    evt.source = SimpleNamespace(type="group", group_id="GRP001", user_id="U_TEST")
    evt.reply_token = "TOKEN900"
    turns = []
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._build_quoted_block", return_value=""),
        patch("main._requires_public_research", return_value=False),
        patch("main._get_explicit_market_quote_reply", return_value=None),
        patch("main._prefetch_urls", side_effect=lambda t: t),
        patch("main._llm_chat", return_value="甲商店和乙平台的價格確實有差異。"),
        patch("main.memory.append_turn", side_effect=lambda *a: turns.append(a)),
        patch("main._try_save_correction"),
        patch("main._maybe_capture_calendar_event"),
        patch("main._mark_inbound_reply_completed_no_reply") as mark_silent,
        patch("main._reply") as mock_reply,
    ):
        main._handle_explicit_text(evt, "GRP001", USER_POST)
    mock_reply.assert_not_called()
    assert mark_silent.call_count == 1
    assert mark_silent.call_args.args[0] == "TOKEN900"
    assert [t[1] for t in turns] == ["user"]


def test_judge_uses_its_own_model_a_valid_timeout_and_no_chat_budget(monkeypatch, judge_on):
    from config import settings

    captured = {}

    def generate_content(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(text=json.dumps(
            {"labels": [{"i": 1, "label": "restate"}, {"i": 2, "label": "new"}]}))

    monkeypatch.delenv("LINE_BOT_RESTATEMENT_JUDGE_MODEL", raising=False)
    monkeypatch.setattr(gemini_client, "_client",
                        SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    monkeypatch.setattr(gemini_client, "_track_usage",
                        lambda *_: pytest.fail("judge must not spend the chat models' request counter"))
    out = restatement_judge.filter_restatements(REPLY, USER_POST)
    assert out == "甲商店可以用 LINE Pay 回饋 5%。"
    # Chat replies depend on these two models' 20 free requests/day each.
    assert captured["model"] not in {settings.gemini_model, settings.gemini_light_model}
    assert captured["config"].response_mime_type == "application/json"
    # The API rejects deadlines under 10s (400), which made every call fail open.
    assert captured["config"].http_options.timeout >= 10_000

    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE_MODEL", "judge-model-x")
    restatement_judge.filter_restatements(REPLY, USER_POST)
    assert captured["model"] == "judge-model-x"


def test_web_research_answer_keeps_research_but_drops_agreement_echo():
    import main
    import public_research

    evt = SimpleNamespace(source=SimpleNamespace(user_id="U_TEST"), reply_token="TOKEN901",
                          message=None)
    research = "乙平台自今年起改為自營倉，出貨約兩天。"
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._build_quoted_block", return_value=""),
        patch.object(public_research, "public_query", side_effect=lambda t: t),
        patch.object(public_research, "collect", return_value=[{"title": "t", "snippet": research}]),
        patch("main._llm_chat", return_value="甲商店和乙平台的價格確實有差異。\n\n" + research),
        patch("main.memory.append_turn"),
        patch("main._append_bot_turn"),
        patch("main._reply") as mock_reply,
    ):
        assert main._handle_web_research_question(evt, "GRP001", USER_POST) is True
    assert mock_reply.call_args.args[1] == research


# ── asks_question: decides whether linked material joins the copy check ─────

@pytest.mark.parametrize("text,expected", [
    ("哪天開始申請 https://news.example.com/a", True),
    ("為什麼停業", True),
    ("請查詢申請截止日期 https://news.example.com/a", True),
    ("幫我看這篇 https://news.example.com/a", True),
    ("youtu.be/abcdefghijk?si=a", False),
    ("https://news.example.com/a?utm_source=x", False),
    ("合成標題：補助上路 | 合成新聞 | LINE TODAY https://x.example/a?b=1", False),
    # Two links in one run of text: the question between or after them counts.
    ("https://news.example.com/a，https://news.example.com/b，申請要繳交什麼文件?", True),
    ("https://news.example.com/a，申請要什麼?https://news.example.com/b?share=1", True),
    ("https://news.example.com/a「b」?share=synthetic-id", False),
    # Punctuation later in the words does not take them back.
    ("https://news.example.com/a，申請要繳什麼文件（?）", True),
    ("https://news.example.com/a（?）", True),
    ("https://news.example.com/a?v=1「重要」這是真的嗎？", True),
    # A bracket typed right after a link that asks, or is never closed, is the user's.
    ("https://news.example.com/a（真的嗎）", True),
    ("https://news.example.com/share/v/DEMO01/（真的嗎）", True),
    ("https://news.example.com/a「真的嗎？", True),
    ("https://youtu.be/abcdefghijk（申請要繳交什麼文件）?", True),   # a lone ASCII ? is not URL syntax
    ("https://www.google.com/search?q=「今年價格上漲」", False),
])
def test_asks_question(text, expected):
    assert reply_policy.asks_question(text) is expected


def test_strip_links_keeps_words_but_not_link_parts():
    text = "https://news.example.com/a「b」?share=synthetic-id，申請要什麼?https://news.example.com/c?share=2"
    words = reply_policy.strip_links(text)
    assert "申請要什麼?" in words and "share" not in words and "synthetic" not in words


def test_burst_question_is_the_latest_line():
    assert reply_policy.asks_question("為什麼停業\n好喔", addressed=False) is False
    assert reply_policy.asks_question("好喔\n為什麼停業", addressed=False) is True
