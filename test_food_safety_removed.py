"""2026-09-28 Andrew：「你把食安查詢那個功能拿掉」。

咪寶原本在晚餐推薦之前先跑食安查詢：攔下店名／商品／「有沒有食安問題」，回官方紀錄，
查不到資料時回「官方資料暫時無法取得」。下面的例句都曾被它攔下（2026-09-28 用移除前
的程式跑過這些測試，每一句都被攔下），現在照一般路由往下走。
"""
from __future__ import annotations

import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import burst_filter
import main


def test_food_safety_lookup_is_gone():
    assert not hasattr(main, "food_safety_client")
    assert not hasattr(main, "_handle_restaurant_food_safety")
    assert not pathlib.Path(main.__file__).with_name("food_safety_client.py").exists()


def _route(monkeypatch, text, *, fast_path=False, quoted_media=False):
    """Run _handle_text_message with the real dinner/research predicates; return the exits taken."""
    from linebot.v3.webhooks import MessageEvent, TextMessageContent

    msg = MagicMock(spec=TextMessageContent)
    msg.id, msg.text, msg.mention, msg.quote_token, msg.type = "MSG928", text, None, "qt", "text"
    msg.quoted_message_id = "Q928" if quoted_media else None
    evt = MagicMock(spec=MessageEvent)
    evt.message = msg
    evt.source = SimpleNamespace(type="group", group_id="GRP_F", user_id="U_TEST")
    evt.reply_token = "TOKEN928"
    seen = []
    stubs = {
        "_try_handle_reminder_cancellation": lambda *_a: False,
        "_try_handle_quoted_calendar_correction": lambda *_a: False,
        "_try_handle_creation_followup": lambda *_a: False,
        "_try_handle_missed_reminder_repair": lambda *_a: False,
        "_try_handle_contextual_date_reminder": lambda *_a: False,
        "_explicit_range_reminder_result": lambda *_a: None,
        "_explicit_month_reminder_result": lambda *_a: None,
        "_explicit_single_reminder_result": lambda *_a: None,
        "_text_with_quote_context": lambda _m, _g, t: t,
        "_try_one_shot_reply": lambda *_a: False,
        "_try_handle_calendar_correction": lambda *_a: False,
        "_handle_command": lambda *_a: None,
        "_handle_explicit_poll_text": lambda *_a: None,
        "_is_todo_query": lambda *_a: False,
        "_is_calendar_query": lambda *_a: False,
        "_is_public_event_discovery_query": lambda *_a: False,
        "_is_travel_duration_question": lambda *_a: False,
        "_detect_user_correction": lambda *_a: None,
        "_auto_capture_text_if_important": lambda *_a: False,
        "_maybe_extract_reminder": lambda *_a, **_k: None,
        "_extract_gemini_trigger": lambda t, _m: t.removeprefix("咪寶").strip() if t.startswith("咪寶") else None,
        "_handle_dinner_recommendation": lambda *_a: seen.append("dinner"),
        "_handle_web_research_question": lambda *_a, **_k: seen.append("research") or True,
        "_handle_media_via_quote": lambda *_a: seen.append("quoted_media"),
        "_handle_explicit_text": lambda _e, _g, clean, **_k: seen.append(("explicit", clean)),
        "_try_piggyback_reminders_fast_path": lambda *_a: seen.append("fast_path") or fast_path,
        "_reply": lambda *_a, **_k: seen.append("reply"),
    }
    for name, value in stubs.items():
        monkeypatch.setattr(main, name, value)
    monkeypatch.setattr(main.memory, "get_raw_message", lambda _g, _q: ("U2", "[圖片]"))
    monkeypatch.setattr(main.feedback_collector, "in_feedback_window", lambda: False)
    monkeypatch.setattr(burst_filter, "cancel_burst", lambda _g: seen.append("cancel") or [])
    monkeypatch.setattr(burst_filter, "add_to_burst", lambda *_a: seen.append("burst"))
    import food_signals
    import knowledge_graph
    import message_classifier
    import reminder_restatement

    monkeypatch.setattr(reminder_restatement, "correction", lambda *_a: None)
    monkeypatch.setattr(knowledge_graph, "auto_extract_kg_async", lambda *_a: None)
    monkeypatch.setattr(food_signals, "extract_and_store_async", lambda *_a: None)
    monkeypatch.setattr(message_classifier, "classify_rule", lambda _t: "other")
    monkeypatch.setattr(message_classifier, "update_category", lambda *_a: None)
    main._handle_text_message(evt, "GRP_F")
    return seen


@pytest.mark.parametrize(
    "text",
    ["鬍鬚張", "乳香世家牛奶", "乳香世家牛奶可以吃嗎", "今晚訂鬍鬚張", "鬍鬚張有沒有食安問題"],
)
def test_former_lookups_wait_in_the_burst(monkeypatch, text):
    assert _route(monkeypatch, text) == ["fast_path", "burst"]


def test_a_due_reminder_still_takes_the_reply_token_first(monkeypatch):
    assert _route(monkeypatch, "鬍鬚張", fast_path=True) == ["fast_path"]


def test_restaurant_question_goes_to_research(monkeypatch):
    assert _route(monkeypatch, "這家餐廳有食安問題嗎") == ["research", "cancel"]


def test_dinner_question_gets_the_dinner_recommendation(monkeypatch):
    assert _route(monkeypatch, "今晚吃什麼？鬍鬚張有食安問題嗎") == ["cancel", "dinner"]


def test_question_on_a_quoted_photo_reads_the_photo(monkeypatch):
    assert _route(monkeypatch, "這個有食安問題嗎", quoted_media=True) == ["cancel", "quoted_media"]


def test_mention_gets_the_normal_reply(monkeypatch):
    assert _route(monkeypatch, "咪寶 鬍鬚張有沒有食安問題") == ["cancel", ("explicit", "鬍鬚張有沒有食安問題")]
