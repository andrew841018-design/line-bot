"""咪寶選單（Quick Reply 按鈕）測試。2026-10-05 加，10-07 由 Flex 卡片改成 Quick Reply。"""

from __future__ import annotations

import os
import socket
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import calendar_db  # noqa: E402
import flex_menu  # noqa: E402
import main  # noqa: E402
import memory  # noqa: E402
import reminder_push  # noqa: E402
from linebot.v3.messaging import TextMessage  # noqa: E402
from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent  # noqa: E402

# LINE Quick Reply 限制：最多 13 顆、action label 20 字、message action text 300 字；
# 文字訊息 5000 字。
QUICK_REPLY_MAX_ITEMS = 13
LABEL_MAX = 20
ACTION_TEXT_MAX = 300
TEXT_MAX = 5000

# 已經發出去的 Flex 卡片會永遠留在聊天紀錄裡可以點，所以這份清單只能加、不能刪，
# 而且每一個都要永遠接得到原本的處理路徑。刻意寫死，不要改成從 flex_menu 匯入。
SHIPPED_BUTTON_TEXTS = (
    "/提醒清單",
    "/行事曆",
    "今晚吃什麼？",
    "/觀點",
    "/help",
)

# 指令型按鈕 → 應該接手的既有處理函式。/help 與晚餐推薦另有專門測試。
_COMMAND_ROUTES = {
    "/提醒清單": "_build_todo_status_reply",
    "/行事曆": "_format_calendar",
    "/觀點": "_handle_finance_view_command",
}


def _message(text: str, *, mention=None, msg_id: str = "MSG_MENU") -> MagicMock:
    message = MagicMock(spec=TextMessageContent)
    message.id = msg_id
    message.text = text
    message.mention = mention
    message.quoted_message_id = None
    return message


def _event(message, *, user_id: str = "U_MEMBER") -> MagicMock:
    source = MagicMock(spec=GroupSource)
    source.group_id = "G_TEST"
    source.user_id = user_id
    event = MagicMock(spec=MessageEvent)
    event.source = source
    event.message = message
    event.reply_token = "TOKEN_MENU"
    return event


def _handle_as_member(event) -> None:
    """走完整的 _handle_event（一般成員、群組白名單關閉）。"""
    with (
        patch("main.line_mentions.user_id_for_family_role", return_value="U_SISTER"),
        patch("main.memory.begin_inbound_event", return_value="new"),
        patch("main._spawn_piggyback_drain"),
        patch.object(main.settings, "allowed_group_ids_raw", ""),
        patch.object(main.settings, "allowed_group_id", ""),
        patch("main.feedback_collector.in_feedback_window", return_value=False),
    ):
        main._handle_event(event)


def _row_counts() -> tuple[int, int]:
    with memory._conn() as c:
        reminders = c.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        events = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return reminders, events


@pytest.fixture
def no_network(monkeypatch):
    def _refuse(*_args, **_kwargs):
        raise OSError("network disabled in test_flex_menu")

    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)


def _fake_line_api(*, reply_side_effect=None) -> MagicMock:
    api = MagicMock()
    if reply_side_effect is not None:
        api.reply_message.side_effect = reply_side_effect
    else:
        api.reply_message.return_value = SimpleNamespace(
            sent_messages=[SimpleNamespace(id="SENT_1", quote_token=None)]
        )
    return api


# ── 選單內容 ──────────────────────────────────────────────────────────────


def test_every_button_text_is_recorded_as_shipped():
    for _label, text in flex_menu.BUTTONS:
        assert text in SHIPPED_BUTTON_TEXTS


def test_every_shipped_text_has_a_routing_test():
    assert set(SHIPPED_BUTTON_TEXTS) == set(_COMMAND_ROUTES) | {"/help", "今晚吃什麼？"}


def test_menu_respects_line_quick_reply_limits():
    items = flex_menu.menu_message().to_dict()["quickReply"]["items"]
    assert len(items) == len(flex_menu.BUTTONS) == 5
    assert len(items) <= QUICK_REPLY_MAX_ITEMS
    for item in items:
        action = item["action"]
        assert action["type"] == "message"
        assert 0 < len(action["label"]) <= LABEL_MAX
        assert 0 < len(action["text"]) <= ACTION_TEXT_MAX
    assert 0 < len(flex_menu.PROMPT_TEXT) <= TEXT_MAX


def test_menu_is_one_text_message_with_buttons_on_its_quick_reply():
    # 收得起來的關鍵：按鈕掛在 Quick Reply，不是會一直留在聊天畫面的卡片。
    message = flex_menu.menu_message()
    assert isinstance(message, TextMessage)
    assert message.to_dict() == {
        "type": "text",
        "text": flex_menu.PROMPT_TEXT,
        "quickReply": {
            "items": [
                {"type": "action", "action": {"type": "message", "label": label, "text": text}}
                for label, text in flex_menu.BUTTONS
            ]
        },
    }


def test_static_strings_survive_the_outbound_text_pipeline():
    strings = [flex_menu.PROMPT_TEXT] + [label for label, _ in flex_menu.BUTTONS]
    for s in strings:
        assert main._md_to_line(s) == s
    # 只有選單文字會走 _reply 的檢查（_prepare_outbound_text、系統狀態抑制）。
    assert (
        main._prepare_outbound_text(flex_menu.PROMPT_TEXT, source="reply")
        == flex_menu.PROMPT_TEXT
    )
    assert not main._is_system_status_outbound(flex_menu.PROMPT_TEXT)


# ── 觸發詞 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["選單", "選單\n", " 選單 ", "/選單", "／選單", "選單？", "選單?", "選單！"],
)
def test_plain_trigger_forms_are_accepted(text):
    assert flex_menu.is_menu_request(text, main._extract_gemini_trigger(text, _message(text)))


@pytest.mark.parametrize(
    "text",
    ["咪寶選單", "咪寶 選單", "＠咪寶 選單", "@咪寶 選單", "/ai 選單", "選單 咪寶"],
)
def test_addressed_trigger_forms_are_accepted(text):
    assert flex_menu.is_menu_request(text, main._extract_gemini_trigger(text, _message(text)))


def test_mobile_mention_structure_is_what_makes_an_unknown_label_count():
    # 「家庭小幫手」不是 bot 別名，沒有 mention 結構時不算；手機版 @ 到 bot 才算。
    text = "@家庭小幫手 選單"
    plain = _message(text)
    mentioned = _message(
        text,
        mention=SimpleNamespace(
            mentionees=[SimpleNamespace(is_self=True, index=0, length=len("@家庭小幫手"))]
        ),
    )
    assert not flex_menu.is_menu_request(text, main._extract_gemini_trigger(text, plain))
    assert flex_menu.is_menu_request(text, main._extract_gemini_trigger(text, mentioned))


@pytest.mark.parametrize(
    "text",
    ["這個選單", "選單呢", "選單🙏", "@爸爸 選單", "咪寶", "", "選單。", "看選單"],
)
def test_non_trigger_text_is_rejected(text):
    assert not flex_menu.is_menu_request(text, main._extract_gemini_trigger(text, _message(text)))


# ── _handle_text_message 的選單分支 ────────────────────────────────────────


def test_menu_trigger_replies_with_menu_before_any_reminder_route():
    event = _event(_message("選單"))
    with (
        patch("main._reply") as reply,
        patch("main.burst_filter.cancel_burst") as cancel_burst,
        patch("main._try_handle_reminder_cancellation") as cancellation,
    ):
        main._handle_text_message(event, "G_TEST")

    reply.assert_called_once_with(
        "TOKEN_MENU", flex_menu.PROMPT_TEXT, group_id="G_TEST", menu_card=True
    )
    cancel_burst.assert_not_called()
    cancellation.assert_not_called()


def test_menu_detection_failure_falls_through_to_normal_routing():
    event = _event(_message("選單"))
    with (
        patch("flex_menu.is_menu_request", side_effect=RuntimeError("boom")),
        patch("main._reply") as reply,
        patch("main._try_handle_reminder_cancellation", return_value=True) as cancellation,
    ):
        main._handle_text_message(event, "G_TEST")

    cancellation.assert_called_once()
    reply.assert_not_called()


def test_ordinary_text_skips_menu_detection_entirely():
    event = _event(_message("今天天氣如何"))
    with (
        patch("main._extract_gemini_trigger") as extract,
        patch("main._try_handle_reminder_cancellation", return_value=True),
    ):
        main._handle_text_message(event, "G_TEST")

    extract.assert_not_called()


def test_long_text_mentioning_menu_skips_detection():
    event = _event(_message("選單" + "，" * 80))
    with (
        patch("main._extract_gemini_trigger") as extract,
        patch("main._try_handle_reminder_cancellation", return_value=True),
    ):
        main._handle_text_message(event, "G_TEST")

    extract.assert_not_called()


def test_menu_card_has_exactly_one_call_site():
    source = Path(main.__file__).read_text(encoding="utf-8")
    assert source.count("menu_card=True") == 1


# ── 按鈕送出的文字都接得到原本的處理（走完整 _handle_event） ───────────────


@pytest.mark.parametrize("text", sorted(_COMMAND_ROUTES))
def test_shipped_command_buttons_route_to_their_handlers(text, no_network):
    # 包住真的處理函式：/觀點 的比對寫在處理函式裡面，換成假函式會讓任何指令都通過。
    real = getattr(main, _COMMAND_ROUTES[text])
    results = []

    def spy(*args, **kwargs):
        result = real(*args, **kwargs)
        results.append(result)
        return result

    before = _row_counts()
    with (
        patch(f"main.{_COMMAND_ROUTES[text]}", side_effect=spy),
        patch("main._reply") as reply,
    ):
        _handle_as_member(_event(_message(text)))

    handled = [r for r in results if r is not None]
    assert len(handled) == 1
    reply.assert_called_once()
    args, kwargs = reply.call_args
    assert args[1] == handled[0]
    # 按鈕回覆是一般文字回覆：照常可以搭到期提醒、不再掛選單。
    assert kwargs.get("menu_card", False) is False
    assert kwargs.get("include_auxiliary", True) is True
    assert _row_counts() == before


def test_shipped_help_button_returns_help_text(no_network):
    before = _row_counts()
    with patch("main._reply") as reply:
        _handle_as_member(_event(_message("/help")))

    reply.assert_called_once()
    assert reply.call_args[0][1] == main._HELP_TEXT
    assert _row_counts() == before


def test_shipped_dinner_button_reaches_dinner_recommendation(no_network):
    before = _row_counts()
    with (
        patch("main._handle_dinner_recommendation") as dinner,
        patch("main._reply") as reply,
        patch("knowledge_graph.auto_extract_kg_async"),
        patch("food_signals.extract_and_store_async"),
    ):
        _handle_as_member(_event(_message("今晚吃什麼？")))

    dinner.assert_called_once()
    reply.assert_not_called()
    assert _row_counts() == before


def test_reminder_list_button_does_not_revive_or_reset_sent_reminders(no_network):
    """不替換任何清單函式：點「提醒清單」不能把已推送／已完成的提醒改回待推送。"""
    soon = (date.today() + timedelta(days=10)).isoformat()
    later = (date.today() + timedelta(days=20)).isoformat()
    done_event = calendar_db.insert_event("G_TEST", "家族聚餐", soon, "18:00", participants=["爸爸"])
    pending_event = calendar_db.insert_event("G_TEST", "回診", later, "09:00", participants=["全家"])
    assert done_event and pending_event
    calendar_db.ensure_active_event_reminder_mirrors("G_TEST")
    ids = {}
    with memory._conn() as c:
        for key, event_id in (("done", done_event), ("pending", pending_event)):
            row = c.execute(
                "SELECT reminder_id FROM reminders WHERE source_kind = ? AND source_ref = ?",
                (calendar_db.EVENT_REMINDER_SOURCE_KIND, str(event_id)),
            ).fetchone()
            assert row is not None
            ids[key] = row[0]
        # 已全部推完而結案的一筆、還在待推送但早期階段已推過的一筆。
        c.execute(
            "UPDATE reminders SET status = 'done', last_pushed_at = 111, weekly_count = 2, "
            "pushed_3d = 1, pushed_1d = 1, pushed_4hr = 1, pushed_2hr = 1, "
            "pushed_1hr = 1, pushed_now = 1 WHERE reminder_id = ?",
            (ids["done"],),
        )
        c.execute(
            "UPDATE reminders SET status = 'pending', last_pushed_at = 222, weekly_count = 1, "
            "pushed_3d = 1 WHERE reminder_id = ?",
            (ids["pending"],),
        )
    columns = (
        "reminder_id, status, last_pushed_at, weekly_count, pushed_3d, pushed_1d, "
        "pushed_4hr, pushed_2hr, pushed_1hr, pushed_now"
    )

    def snapshot():
        with memory._conn() as c:
            return [
                tuple(c.execute(f"SELECT {columns} FROM reminders WHERE reminder_id = ?", (rid,)).fetchone())
                for rid in (ids["done"], ids["pending"])
            ]

    def mirror_count():
        with memory._conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM reminders WHERE source_kind = ?",
                (calendar_db.EVENT_REMINDER_SOURCE_KIND,),
            ).fetchone()[0]

    before, count_before = snapshot(), mirror_count()
    with patch("main._reply") as reply:
        _handle_as_member(_event(_message("/提醒清單")))
    reply.assert_called_once()  # 清單真的有跑到
    assert snapshot() == before
    assert mirror_count() == count_before  # 也沒有偷插一筆新的待推送副本


def test_button_reply_still_piggybacks_due_reminders_with_mentions(monkeypatch):
    """硬性條件：按鈕回覆搭車送出的到期提醒，提及照舊（爸爸→本人、全家→全體）。"""
    now = int(time.time())
    monkeypatch.setattr(reminder_push.line_mentions, "load_user_aliases", lambda: {"U_DAD": "爸爸"})
    base_row = {
        "group_id": "G_TEST",
        "user_id": "U_DAD",
        "remind_at": now + 86400,
        "created_at": now - 3600,
        "source_text": "",
        "last_pushed_at": 0,
        "weekly_count": 0,
        "last_weekly_at": 0,
        "pushed_3d": 0,
        "pushed_1d": 0,
        "pushed_4hr": 0,
        "pushed_2hr": 0,
        "pushed_1hr": 0,
        "pushed_now": 0,
    }
    rows = [
        dict(base_row, reminder_id=41, action="回診", mention_aliases=["爸爸"]),
        dict(base_row, reminder_id=42, action="家族聚餐", mention_aliases=["全家"]),
    ]
    items = []
    for r in rows:
        text, message = reminder_push._build_push_text_and_message(r, "1d", now=now)
        items.append(
            {
                "reminder_id": r["reminder_id"],
                "group_id": "G_TEST",
                "stage": "1d",
                "text": text,
                "message": message,
                "action": r["action"],
                "remind_at": r["remind_at"],
                "weekly_count": 0,
            }
        )
    monkeypatch.setattr(reminder_push, "due_reminders_for_reply", lambda *a, **kw: items)
    monkeypatch.setattr(calendar_db, "list_due_for_reminder", lambda *a, **kw: [])
    monkeypatch.setattr(main.memory, "is_reminder_pending", lambda *a, **kw: True)
    claims = iter(({"reminder_id": 41, "claim_token": "c41"}, {"reminder_id": 42, "claim_token": "c42"}))
    monkeypatch.setattr(main.memory, "claim_natural_reminder_delivery", lambda *a, **kw: next(claims))
    monkeypatch.setattr(main.memory, "finalize_natural_reminder_delivery", lambda claim: True)
    monkeypatch.setattr(main.memory, "log_raw_message", lambda *a, **kw: None)

    api = _fake_line_api()
    with (
        patch.object(main.settings, "bot_muted", False),
        patch("main.MessagingApi", return_value=api),
        patch("main.ApiClient"),
        patch.object(main, "_get_line_config", return_value=MagicMock()),
    ):
        main._reply("TOKEN_MENU", "清單", group_id="G_TEST")

    sent = api.reply_message.call_args[0][0].messages
    assert getattr(sent[0], "text", None) == "清單"
    reminder_messages = sent[1:]
    assert len(reminder_messages) == 2
    user_mentions = [
        target.mentionee.user_id
        for target in reminder_messages[0].substitution.values()
        if getattr(target.mentionee, "user_id", None)
    ]
    assert "U_DAD" in user_mentions
    assert reminder_messages[1].substitution.get("all") is not None


# ── 說明文字 ──────────────────────────────────────────────────────────────


def test_help_text_has_no_mention_and_lists_the_menu():
    assert "@" not in main._HELP_TEXT
    assert "＠" not in main._HELP_TEXT
    assert "選單" in main._HELP_TEXT
    for removed in ("/清除記憶", "/清除規則", "/忘記", "/採用"):
        assert removed not in main._HELP_TEXT


def test_help_reply_is_plain_text_message():
    _text, message = main._text_message_with_mentions(
        main._HELP_TEXT, prepared=True, limit=5000, explicit_only=True
    )
    assert isinstance(message, TextMessage)


# ── _reply 的選單路徑 ──────────────────────────────────────────────────────


def _reply_with_menu(api, **overrides):
    kwargs = dict(group_id="G_TEST", menu_card=True)
    kwargs.update(overrides)
    with (
        patch("main.MessagingApi", return_value=api),
        patch("main.ApiClient"),
        patch.object(main, "_get_line_config", return_value=MagicMock()),
    ):
        return main._reply("TOKEN_MENU", flex_menu.PROMPT_TEXT, **kwargs)


def test_reply_sends_only_the_menu_and_archives_its_text():
    api = _fake_line_api()
    with (
        patch.object(main.settings, "bot_muted", False),
        patch.object(main.memory, "log_raw_message") as archive,
        patch("reminder_push.due_reminders_for_reply") as due,
    ):
        ok = _reply_with_menu(api, include_auxiliary=True)  # _reply 必須自己關掉搭車

    assert ok is True
    sent = api.reply_message.call_args[0][0].messages
    # 只有一則：LINE 只顯示最後一則訊息的 Quick Reply，搭車的提醒會把按鈕蓋掉。
    assert len(sent) == 1
    assert isinstance(sent[0], TextMessage)
    assert sent[0].to_dict() == flex_menu.menu_message().to_dict()
    due.assert_not_called()
    archive.assert_any_call("G_TEST", "SENT_1", "__bot__", flex_menu.PROMPT_TEXT)


def test_menu_never_push_falls_back_even_if_caller_allows_it():
    api = _fake_line_api(reply_side_effect=Exception("Invalid reply token"))
    assert main._is_definite_reply_token_error(Exception("Invalid reply token"))
    with patch.object(main.settings, "bot_muted", False):
        ok = _reply_with_menu(api, allow_push_fallback=True)

    assert ok is False
    api.push_message.assert_not_called()


def test_menu_build_failure_sends_nothing_and_closes_the_event():
    api = _fake_line_api()
    with (
        patch.object(main.settings, "bot_muted", False),
        patch("flex_menu.menu_message", side_effect=RuntimeError("bad menu")),
        patch.object(main, "_mark_inbound_reply_completed_no_reply") as complete,
    ):
        ok = _reply_with_menu(api)

    assert ok is False
    api.reply_message.assert_not_called()
    complete.assert_called_once_with("TOKEN_MENU")


def test_menu_is_not_sent_when_bot_is_muted():
    api = _fake_line_api()
    with patch.object(main.settings, "bot_muted", True):
        ok = _reply_with_menu(api)

    assert ok is False
    api.reply_message.assert_not_called()


# ── 靜音成員 ──────────────────────────────────────────────────────────────


def _handle_as_silenced(event, *, quotes_bot: bool):
    with (
        patch("main.line_mentions.user_id_for_family_role", return_value="U_SISTER"),
        patch("main.memory.begin_inbound_event", return_value="new"),
        patch("main.memory.mark_inbound_events_completed_no_reply") as complete,
        patch("main._quotes_bot_message", return_value=quotes_bot),
        patch("main._spawn_piggyback_drain"),
        patch("main._reply") as reply,
        patch.object(main.settings, "allowed_group_ids_raw", ""),
        patch.object(main.settings, "allowed_group_id", ""),
    ):
        main._handle_event(event)
    return reply, complete


@pytest.mark.parametrize("text", ["選單", "/提醒清單"])
def test_silenced_member_unquoted_menu_or_button_gets_no_reply(text):
    event = _event(_message(text), user_id="U_SISTER")
    reply, complete = _handle_as_silenced(event, quotes_bot=False)
    reply.assert_not_called()
    complete.assert_called_once_with("G_TEST", ["MSG_MENU"])


def test_silenced_member_quoting_a_bot_message_gets_normal_routing():
    event = _event(_message("選單"), user_id="U_SISTER")
    reply, _complete = _handle_as_silenced(event, quotes_bot=True)
    reply.assert_called_once()
    assert reply.call_args.kwargs["menu_card"] is True
