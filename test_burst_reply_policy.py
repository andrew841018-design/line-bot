import burst_filter
import main

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent


def test_short_substantive_text_should_trigger_reply():
    assert burst_filter._heuristic_decision("我買好了") == "respond"


def test_exact_chitchat_still_skips_reply():
    assert burst_filter._heuristic_decision("好") == "skip"
    assert burst_filter._heuristic_decision("哈哈") == "skip"


def _make_text_event(text: str):
    msg = MagicMock(spec=TextMessageContent)
    msg.id = "MSG001"
    msg.text = text
    msg.type = "text"
    msg.mention = None
    msg.quoted_message_id = None

    src = MagicMock(spec=GroupSource)
    src.group_id = "GRP001"
    src.user_id = "USR001"

    event = MagicMock(spec=MessageEvent)
    event.message = msg
    event.source = src
    event.reply_token = "TOKEN001"
    event.delivery_context = MagicMock(is_redelivery=False)
    return event


def test_web_research_question_detector_covers_public_info_questions():
    assert main._is_web_research_question("美股最近怎樣")
    assert main._is_web_research_question("紐西蘭氣候如何")
    assert main._is_web_research_question("WezTerm 可以支援 M1 晶片嗎？")
    assert main._is_web_research_question("日本哪裡好玩")


def test_web_research_question_detector_ignores_plain_chat():
    assert not main._is_web_research_question("我買好了")
    assert not main._is_web_research_question("好")
    assert not main._is_web_research_question("媽媽最近怎樣")
    assert not main._is_web_research_question("你推薦哪個")
    assert not main._is_web_research_question("媽媽推薦哪個")


def test_plain_web_research_question_bypasses_burst():
    event = _make_text_event("美股最近怎樣")

    with patch("main.feedback_collector.in_feedback_window", return_value=False), \
         patch("main._try_one_shot_reply", return_value=False), \
         patch("main._try_handle_calendar_correction", return_value=False), \
         patch("main._detect_user_correction"), \
         patch("main._auto_capture_text_if_important"), \
         patch("main._maybe_extract_reminder"), \
         patch("main._handle_command", return_value=None), \
         patch("main._is_todo_query", return_value=False), \
         patch("main._is_dinner_question", return_value=False), \
         patch("main._extract_gemini_trigger", return_value=None), \
         patch("main._handle_web_research_question", return_value=True) as mock_web, \
         patch("main.burst_filter.add_to_burst") as mock_burst:
        main._handle_text_message(event, "GRP001")

    mock_web.assert_called_once_with(event, "GRP001", "美股最近怎樣")
    mock_burst.assert_not_called()


def test_web_research_handler_injects_crawled_sources(monkeypatch):
    event = _make_text_event("紐西蘭氣候如何")
    captured: dict[str, str] = {}
    replies: list[str] = []

    monkeypatch.setattr(main, "_thinking_indicator", lambda _gid: nullcontext())
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [
        {
            "title": "New Zealand climate overview",
            "url": "https://www.metservice.com/example",
            "domain": "metservice.com",
            "full_text": "紐西蘭氣候受海洋影響，北島較溫暖，南島較涼。",
        }
    ])
    monkeypatch.setattr(main.memory, "get_context", lambda _gid: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda *_a, **_k: [])
    monkeypatch.setattr(main.memory, "append_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "_get_persona_notes", lambda _gid: [])

    def fake_llm(prompt, *_args):
        captured["prompt"] = prompt
        return "紐西蘭氣候要分北島和南島看。"

    monkeypatch.setattr(main, "_llm_chat", fake_llm)
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, group_id=None, **_kw: replies.append(text),
    )

    assert main._handle_web_research_question(event, "GRP001", "紐西蘭氣候如何")
    assert "紐西蘭氣候如何" in captured["prompt"]
    assert "metservice.com" in captured["prompt"]
    assert "本機爬蟲資料" in captured["prompt"]
    assert replies == ["紐西蘭氣候要分北島和南島看。"]


def test_quota_exhausted_text_still_enters_text_handler():
    event = _make_text_event("我買好了")

    main.settings.allowed_group_id = "GRP001"
    main.settings.allowed_group_ids_raw = ""

    with (
        patch("main._quota_exhausted", return_value=True),
        patch("main._handle_text_message") as mock_text_handler,
        patch("main._save_pending_any") as mock_save_pending,
        patch("main._try_piggyback_drain_with_reply_token") as mock_piggyback,
        patch("main._spawn_piggyback_drain") as mock_spawn,
    ):
        main._handle_event(event)

    mock_text_handler.assert_called_once_with(event, "GRP001")
    mock_spawn.assert_called_once_with("GRP001")
    mock_save_pending.assert_not_called()
    mock_piggyback.assert_not_called()
