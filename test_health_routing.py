"""爸媽健康的路由與整合測試（2026-10-11）。

走完整的 _handle_event：/健康 在提醒路徑之前被接走、不進 burst、不呼叫模型、只用 reply token、
不進 recall；引用健康卡不讓模型判讀；緊急字詞的 119 提醒只用這則的 reply token、不搭到期提醒，
送出結果交回 parent_health；每則文字交給背景抽取；收回訊息刪紀錄；週報段落出錯時週報逐字不變。
parent_health 本身（抽取、卡片、同意）的測試在 test_parent_health*.py。這裡大多換上假的
parent_health 模組，只測 main／weekly_summary 怎麼接；最後幾個測試用真的模組。
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import flex_menu  # noqa: E402
import main  # noqa: E402
import memory  # noqa: E402
from linebot.v3.webhooks import (  # noqa: E402
    GroupSource,
    MessageEvent,
    TextMessageContent,
    UnsendDetail,
    UnsendEvent,
)

SAFETY = "爸媽健康：⚠️ 如果現在有胸痛胸悶、呼吸困難、嘴歪或說話不清、單側手腳無力，或跌倒後不舒服，請馬上就醫或打 119。"
NOTICE = "爸媽健康：卡片上是從群組留言整理的紀錄，咪寶不會判讀或診斷。"


# ── 測試工具（刻意不 import 別的測試檔：那些檔案別的對話也在改） ─────────────
def _message(text: str, *, msg_id: str = "MSG_HEALTH", quoted: str | None = None) -> MagicMock:
    message = MagicMock(spec=TextMessageContent)
    message.id = msg_id
    message.text = text
    message.mention = None
    message.quoted_message_id = quoted
    return message


def _event(message, *, user_id: str = "U_MEMBER", timestamp: int = 1_791_700_000_000) -> MagicMock:
    source = MagicMock(spec=GroupSource)
    source.group_id = "G_TEST"
    source.user_id = user_id
    event = MagicMock(spec=MessageEvent)
    event.source = source
    event.message = message
    event.reply_token = "TOKEN_HEALTH"
    event.timestamp = timestamp
    return event


def _handle(event) -> None:
    with (
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
def fake_health(monkeypatch):
    """換上假的 parent_health：記下 main 怎麼呼叫它。"""
    calls: dict[str, list] = {
        "handle": [],
        "observe": [],
        "decision": [],
        "outcome": [],
        "forget": [],
    }
    module = SimpleNamespace(
        SAFETY_TEXT=SAFETY,
        QUOTE_NOTICE=NOTICE,
        CARD_PREFIX="爸媽健康卡：",
        REPLY_PREFIX="爸媽健康：",
        BOT_PREFIXES=("爸媽健康：", "爸媽健康卡："),
        reply=SimpleNamespace(text="爸媽健康：說明", card=None, alt_text=None, fallback_text="爸媽健康：暫時無法回覆，請稍後再試"),
        decision=None,
    )

    def handle_command(group_id, user_id, body, *, message_id, quoted_message_id=None, now=None):
        calls["handle"].append(
            {"group_id": group_id, "user_id": user_id, "body": body, "message_id": message_id, "quoted": quoted_message_id}
        )
        return module.reply

    def observe_async(group_id, message_id, user_id, text, *, event_ts_ms):
        calls["observe"].append((group_id, message_id, user_id, text, event_ts_ms))

    def red_flag_decision(group_id, user_id, text, *, message_id, event_ts_ms, now=None):
        calls["decision"].append((group_id, user_id, text, message_id, event_ts_ms))
        return module.decision

    def red_flag_outcome(group_id, decision, *, message_id, sent, suppressed, muted, now=None):
        calls["outcome"].append(
            {"decision": decision, "message_id": message_id, "sent": sent, "suppressed": suppressed, "muted": muted}
        )

    def forget_message(group_id, message_id, *, db_path=None):
        calls["forget"].append((group_id, message_id))
        return 1

    module.handle_command = handle_command
    module.observe_async = observe_async
    module.red_flag_decision = red_flag_decision
    module.red_flag_outcome = red_flag_outcome
    module.forget_message = forget_message
    module.is_health_card_text = lambda text: str(text).startswith("爸媽健康卡：")
    module.start_background = lambda: None
    module.stop_background = lambda timeout=2.0: None
    module.calls = calls
    monkeypatch.setitem(sys.modules, "parent_health", module)
    return module


def _routed(text: str, *, quoted: str | None = None, raw_row=None, reply_return=True, user_id="U_MEMBER",
            delivery_suppressed=False):
    """走完整 _handle_event，回傳 (_reply mock, cancel_burst mock, add_to_burst mock)。"""

    def fake_reply(*args, **kwargs):
        delivery = kwargs.get("primary_delivery")
        if isinstance(delivery, dict):
            delivery["suppressed"] = delivery_suppressed
        return reply_return

    with (
        patch("main._reply", side_effect=fake_reply) as reply,
        patch("main.burst_filter.cancel_burst") as cancel,
        patch("main.burst_filter.add_to_burst") as add,
        patch("main.memory.get_raw_message", return_value=raw_row),
        patch("main._llm_chat", side_effect=AssertionError("health routes must not call a model")),
    ):
        _handle(_event(_message(text, quoted=quoted), user_id=user_id))
    return reply, cancel, add


# ── /健康 指令路由 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "body"),
    [
        ("/健康", ""),
        ("／健康", ""),
        ("  /健康  ", ""),
        ("/爸媽健康", ""),
        ("咪寶 /健康", ""),
        ("/健康 爸爸", "爸爸"),
        ("/健康 130/85", "130/85"),  # 不會被提醒路徑當成日期
        ("/健康 明天回診", "明天回診"),
        ("/健康 同意", "同意"),
        ("/健康 恢復", "恢復"),
        ("/健康 恢復 爸爸", "恢復 爸爸"),  # 10/17：家人恢復家人暫停的
        ("/健康\u3000恢復\u3000媽媽", "恢復 媽媽"),
        # GP1 post-review：子指令前面沒打空格也要接得到（不然會掉進一般路由交給模型）。
        ("/健康同意", "同意"),
        ("/健康恢復", "恢復"),
        ("/健康恢復爸爸", "恢復爸爸"),
        ("/健康爸爸", "爸爸"),
        ("/健康暫停爸爸", "暫停爸爸"),
        ("/健康吃藥了", "吃藥了"),
        ("/健康刪除我的紀錄", "刪除我的紀錄"),
        ("/爸媽健康說明", "說明"),
    ],
)
def test_health_command_is_answered_before_the_reminder_paths(fake_health, text, body):
    before = _row_counts()
    reply, cancel, add = _routed(text)
    assert [c["body"] for c in fake_health.calls["handle"]] == [body]
    reply.assert_called_once()
    args, kwargs = reply.call_args
    assert args[1] == "爸媽健康：說明"
    assert kwargs["allow_push_fallback"] is False
    assert kwargs["index_for_recall"] is False
    cancel.assert_not_called()
    add.assert_not_called()
    assert _row_counts() == before
    assert fake_health.calls["decision"] == []  # 指令不會再被當成緊急字詞


@pytest.mark.parametrize("text", ["/今日健康", "/健康檢查報告出來了", "健康最重要", "/健康管理"])
def test_other_texts_with_health_in_them_are_not_the_command(fake_health, text):
    _routed(text)
    assert fake_health.calls["handle"] == []


def test_card_reply_goes_through_the_single_command_card_path(fake_health):
    card = {
        "type": "bubble",
        "body": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "爸媽健康"}]},
    }
    fake_health.reply = SimpleNamespace(text=None, card=card, alt_text="爸媽健康卡：近 7 天紀錄（點開看）", fallback_text="爸媽健康：暫時無法回覆，請稍後再試")
    reply, _cancel, _add = _routed("/健康")
    args, kwargs = reply.call_args
    assert args[1] == "爸媽健康卡：近 7 天紀錄（點開看）"
    assert kwargs["flex_card"] == card
    assert kwargs["index_for_recall"] is False
    assert kwargs["menu_buttons"] is True


def test_a_failing_health_module_answers_with_a_fixed_line(fake_health):
    def boom(*_args, **_kwargs):
        raise RuntimeError("synthetic failure")

    fake_health.handle_command = boom
    reply, _cancel, add = _routed("/健康")
    assert reply.call_args.args[1] == main._HEALTH_FAILURE_TEXT
    add.assert_not_called()


def test_a_missing_health_module_answers_with_a_fixed_line(monkeypatch):
    monkeypatch.setitem(sys.modules, "parent_health", None)  # import 會丟 ImportError
    reply, _cancel, add = _routed("/健康 同意")
    assert reply.call_args.args[1] == main._HEALTH_FAILURE_TEXT
    add.assert_not_called()


def test_quoted_message_id_is_passed_for_delete(fake_health):
    _routed("/健康 刪除", quoted="MSG_ORIGINAL", raw_row=("U_KID", "爸爸剛吃完藥"))
    assert fake_health.calls["handle"][0]["quoted"] == "MSG_ORIGINAL"


def test_health_command_text_is_not_indexed_for_recall(fake_health):
    with patch("main.memory.log_raw_message") as log_raw:
        _routed("/健康 爸爸")
    assert log_raw.call_args.kwargs.get("index_for_recall") is False


def test_ordinary_text_is_still_indexed_for_recall(fake_health):
    with patch("main.memory.log_raw_message") as log_raw:
        _routed("今天天氣很好")
    assert "index_for_recall" not in log_raw.call_args.kwargs


# ── 每則文字都交給背景抽取 ───────────────────────────────────────────────────
def test_every_text_message_is_handed_to_the_extractor(fake_health):
    _routed("我今天有去散步", user_id="U_DAD")
    assert fake_health.calls["observe"] == [("G_TEST", "MSG_HEALTH", "U_DAD", "我今天有去散步", 1_791_700_000_000)]


def test_an_extractor_failure_does_not_stop_routing(fake_health):
    def boom(*_args, **_kwargs):
        raise RuntimeError("synthetic failure")

    fake_health.observe_async = boom
    _reply, _cancel, add = _routed("今天天氣很好")
    add.assert_called_once()


# ── 收回訊息 ─────────────────────────────────────────────────────────────────
def _unsend_event(group_id: str = "G_TEST", message_id: str = "MSG_GONE") -> UnsendEvent:
    source = GroupSource(type="group", groupId=group_id, userId="U_DAD")
    return UnsendEvent(
        type="unsend",
        source=source,
        timestamp=1_791_700_000_000,
        mode="active",
        webhookEventId="WEBHOOK_TEST",
        deliveryContext={"isRedelivery": False},
        unsend=UnsendDetail(messageId=message_id),
    )


def test_unsending_a_message_forgets_its_health_records(fake_health):
    _handle(_unsend_event())
    assert fake_health.calls["forget"] == [("G_TEST", "MSG_GONE")]


def test_unsend_from_a_group_that_is_not_allowed_is_ignored(fake_health):
    with (
        patch.object(main.settings, "allowed_group_ids_raw", "G_OTHER"),
        patch.object(main.settings, "allowed_group_id", ""),
    ):
        main._handle_event(_unsend_event())
    assert fake_health.calls["forget"] == []


# ── 引用健康卡 ───────────────────────────────────────────────────────────────
def test_quoting_a_health_card_gets_a_fixed_notice_not_a_model(fake_health):
    reply, _cancel, add = _routed(
        "@咪寶 這樣正常嗎", quoted="MSG_CARD", raw_row=("__bot__", "爸媽健康卡：近 7 天紀錄（點開看）")
    )
    args, kwargs = reply.call_args
    assert args[1] == NOTICE
    assert kwargs["allow_push_fallback"] is False
    assert kwargs["index_for_recall"] is False
    add.assert_not_called()


def test_quoting_the_safety_line_is_answered_as_usual(fake_health):
    with patch("main._handle_explicit_text") as explicit:
        _routed("@咪寶 要掛哪一科", quoted="MSG_SAFETY", raw_row=("__bot__", SAFETY))
    assert explicit.called  # 沒被健康卡的引用檢查攔下


def test_quoting_a_family_message_is_not_intercepted(fake_health):
    _reply, _cancel, add = _routed("哈哈", quoted="MSG_KID", raw_row=("U_KID", "爸媽健康卡：這是家人打的字"))
    add.assert_called_once()


# ── 緊急字詞 ─────────────────────────────────────────────────────────────────
def _decision(*, reply=True, allow_push=True, dm=True, reason="send"):
    return SimpleNamespace(role="爸爸", labels=("跌倒",), reply=reply, allow_push=allow_push, dm=dm, reason=reason)


def test_red_flag_reply_uses_only_this_reply_token(fake_health):
    fake_health.decision = _decision()
    before = _row_counts()
    reply, cancel, add = _routed("我剛剛跌倒了", user_id="U_DAD")
    reply.assert_called_once()
    args, kwargs = reply.call_args
    assert args[0] == "TOKEN_HEALTH"
    assert args[1] == SAFETY
    assert kwargs["include_auxiliary"] is False
    assert kwargs["allow_push_fallback"] is True
    assert kwargs["index_for_recall"] is False
    assert isinstance(kwargs["primary_delivery"], dict)
    add.assert_not_called()
    cancel.assert_not_called()
    assert _row_counts() == before
    outcome = fake_health.calls["outcome"][0]
    assert outcome["sent"] is True and outcome["suppressed"] is False
    assert outcome["message_id"] == "MSG_HEALTH"
    assert fake_health.calls["decision"][0][4] == 1_791_700_000_000


def test_red_flag_push_fallback_follows_the_daily_cap(fake_health):
    fake_health.decision = _decision(allow_push=False)
    reply, _cancel, _add = _routed("我剛剛跌倒了", user_id="U_DAD")
    assert reply.call_args.kwargs["allow_push_fallback"] is False


def test_red_flag_outcome_reports_a_failed_or_suppressed_reply(fake_health):
    fake_health.decision = _decision()
    _routed("我剛剛跌倒了", user_id="U_DAD", reply_return=False, delivery_suppressed=True)
    outcome = fake_health.calls["outcome"][0]
    assert outcome["sent"] is False
    assert outcome["suppressed"] is True


def test_dm_only_decisions_keep_normal_routing(fake_health):
    fake_health.decision = _decision(reply=False, reason="in_care")
    reply, _cancel, add = _routed("媽媽在急診", user_id="U_KID")
    for call in reply.call_args_list:
        assert call.args[1] != SAFETY
    add.assert_called_once()
    assert fake_health.calls["outcome"][0]["sent"] is False


def test_no_red_flag_keeps_normal_routing(fake_health):
    _reply, _cancel, add = _routed("今天天氣很好")
    add.assert_called_once()
    assert fake_health.calls["outcome"] == []


def test_a_failing_red_flag_check_keeps_normal_routing(fake_health):
    def boom(*_args, **_kwargs):
        raise RuntimeError("synthetic failure")

    fake_health.red_flag_decision = boom
    _reply, _cancel, add = _routed("我剛剛跌倒了")
    add.assert_called_once()


@pytest.mark.parametrize("text", ["/記住 爸爸剛剛跌倒了", "／記住 爸爸剛剛跌倒了", "咪寶 /記住 爸爸剛剛跌倒了"])
def test_other_slash_commands_are_not_red_flag_checked(fake_health, text):
    fake_health.decision = _decision()
    reply, _cancel, _add = _routed(text)
    assert fake_health.calls["decision"] == []
    assert all(call.args[1] != SAFETY for call in reply.call_args_list)


# ── 選單、說明 ───────────────────────────────────────────────────────────────
def test_menu_button_replaced_and_old_card_button_still_brings_the_menu_back():
    assert ("🩺 爸媽健康", "/健康") in flex_menu.BUTTONS
    assert all(text != "/今晚煮什麼" for _label, text in flex_menu.BUTTONS)
    assert len(flex_menu.BUTTONS) == 13
    assert flex_menu.is_button_text("/今晚煮什麼")
    assert flex_menu.is_button_text("/健康")


def test_help_lists_the_health_commands_without_mentions():
    text = main._HELP_TEXT
    assert "【爸媽健康】" in text
    assert "/健康 恢復" in text and "/健康 暫停" in text and "/健康 刪除" in text
    assert "/健康 同意" not in text  # 2026-10-17 起家族主群預設記錄，說明不再叫人按同意
    assert "/今晚煮什麼" in text
    assert "@" not in text and "＠" not in text


# ── 週報 ─────────────────────────────────────────────────────────────────────
def test_weekly_section_is_appended_but_not_archived(monkeypatch, fake_health):
    import weekly_summary

    fake_health.weekly_section = lambda group_id, now=None: "🩺 爸媽這週\n爸爸：吃藥 5/7 天"
    family = "👨‍👩‍👧‍👦 家族熱話週報（本週）\n成員甲：散步"
    combined = weekly_summary._with_parent_health("G1", family)
    assert combined == family + "\n\n🩺 爸媽這週\n爸爸：吃藥 5/7 天"


@pytest.mark.parametrize(
    "section",
    [
        "",
        "最新數據 3 筆",  # 推播驗證會擋
        "🩺 爸媽這週\n" + "爸" * 5000,  # 合併後超過 4900
    ],
)
def test_weekly_report_is_unchanged_when_the_section_cannot_be_used(fake_health, section):
    import weekly_summary

    fake_health.weekly_section = lambda group_id, now=None: section
    family = "👨‍👩‍👧‍👦 家族熱話週報（本週）\n成員甲：散步"
    assert weekly_summary._with_parent_health("G1", family) == family


def test_weekly_report_is_unchanged_when_the_section_raises(fake_health):
    import weekly_summary

    def boom(*_args, **_kwargs):
        raise RuntimeError("synthetic failure")

    fake_health.weekly_section = boom
    family = "👨‍👩‍👧‍👦 家族熱話週報（本週）\n成員甲：散步"
    assert weekly_summary._with_parent_health("G1", family) == family


def test_weekly_report_is_unchanged_when_the_module_is_missing(monkeypatch):
    import weekly_summary

    monkeypatch.setitem(sys.modules, "parent_health", None)
    family = "👨‍👩‍👧‍👦 家族熱話週報（本週）\n成員甲：散步"
    assert weekly_summary._with_parent_health("G1", family) == family


def test_weekly_push_archives_the_report_without_the_health_section(monkeypatch, fake_health):
    import weekly_summary

    family = "👨‍👩‍👧‍👦 家族熱話週報（本週）\n成員甲：散步"
    fake_health.weekly_section = lambda group_id, now=None: "🩺 爸媽這週\n爸爸：吃藥 5/7 天"
    pushed: list[tuple[str, str | None]] = []

    def fake_push(text, archive_text=None):
        pushed.append((text, archive_text))
        return True

    monkeypatch.setattr(weekly_summary, "GROUP_ID", "G1")
    monkeypatch.setattr(weekly_summary, "line_access_token", lambda: "token")
    monkeypatch.setattr(weekly_summary.memory, "get_messages_since", lambda *a, **kw: [("m1", "__bot__", "bot reply", 0)])
    monkeypatch.setattr(weekly_summary.gemini_client, "chat", lambda *a, **kw: "")
    monkeypatch.setattr(weekly_summary, "_push", fake_push)
    monkeypatch.setattr(weekly_summary, "_start_family_workers", lambda *a, **kw: None)
    monkeypatch.setattr(weekly_summary, "_collect_family_inputs", lambda group_id: ({}, {}, {}))
    monkeypatch.setattr(weekly_summary, "_render_family_text", lambda *a, **kw: family)
    monkeypatch.setattr(weekly_summary, "_start_worker", lambda name, fn: None)
    monkeypatch.setattr(weekly_summary, "_wait_worker", lambda name, deadline: (False, None))
    weekly_summary.main()
    assert pushed == [(family + "\n\n🩺 爸媽這週\n爸爸：吃藥 5/7 天", family)]


def test_health_replies_never_feed_the_weekly_model_summary(monkeypatch, fake_health):
    import weekly_summary

    requests: list = []
    rows = [
        ("m1", "__bot__", "爸媽健康卡：近 7 天紀錄（點開看）", 0),
        ("m2", "__bot__", "爸媽健康：記下了", 0),
        ("m3", "__bot__", "一般回覆", 0),
    ]
    monkeypatch.setattr(weekly_summary, "GROUP_ID", "G1")
    monkeypatch.setattr(weekly_summary, "line_access_token", lambda: "token")
    monkeypatch.setattr(weekly_summary.memory, "get_messages_since", lambda *a, **kw: rows)
    monkeypatch.setattr(
        weekly_summary, "_build_summary_request", lambda replies: requests.append(list(replies)) or ("p", [], [], 1)
    )
    monkeypatch.setattr(weekly_summary, "_start_worker", lambda name, fn: None)
    monkeypatch.setattr(weekly_summary, "_wait_worker", lambda name, deadline: (False, None))
    monkeypatch.setattr(weekly_summary, "_start_family_workers", lambda *a, **kw: None)
    monkeypatch.setattr(weekly_summary, "_collect_family_inputs", lambda group_id: ({}, {}, {}))
    monkeypatch.setattr(weekly_summary, "_render_family_text", lambda *a, **kw: "")
    weekly_summary.main()
    assert requests == [["一般回覆"]]


# ── 真的 parent_health 模組 ─────────────────────────────────────────────────
def test_real_overview_card_goes_out_through_the_validated_card_path():
    reply, _cancel, add = _routed("/健康")
    args, kwargs = reply.call_args
    assert args[1].startswith("爸媽健康卡：")
    assert kwargs["flex_card"] is not None  # 沒被 _reply_command_card 的驗證換成固定句
    assert kwargs["menu_buttons"] is True
    add.assert_not_called()


def test_health_cards_never_ask_for_a_large_print_page(monkeypatch):
    """2026-10-17 字卡大字版只給股票、FCN：健康卡是家人健康紀錄，不能變成拿到連結就看得到的網頁。"""
    page = MagicMock(side_effect=AssertionError("health cards must not become web pages"))
    monkeypatch.setattr(main, "_with_card_page", page)
    reply, _cancel, _add = _routed("/健康")
    args, kwargs = reply.call_args
    assert args[1].startswith("爸媽健康卡：") and kwargs["flex_card"] is not None
    page.assert_not_called()


def test_every_health_card_button_routes_to_the_health_command():
    import parent_health

    assert parent_health.SHIPPED_HEALTH_BUTTON_TEXTS
    for text in parent_health.SHIPPED_HEALTH_BUTTON_TEXTS:
        reply, _cancel, add = _routed(text)
        reply.assert_called_once()
        assert reply.call_args.args[1] != main._HEALTH_FAILURE_TEXT, text
        add.assert_not_called()


def test_shared_constants_match_the_module():
    import parent_health
    import weekly_summary

    assert weekly_summary._HEALTH_PREFIXES == parent_health.BOT_PREFIXES
    assert main._HEALTH_CARD_PREFIX == parent_health.CARD_PREFIX
    assert main._HEALTH_FAILURE_TEXT == parent_health.FAILURE_TEXT
    assert parent_health.SAFETY_TEXT == SAFETY
    assert parent_health.QUOTE_NOTICE == NOTICE


def test_disabled_stub_answers_old_buttons_with_a_fixed_line(monkeypatch):
    import parent_health_disabled

    monkeypatch.setitem(sys.modules, "parent_health", parent_health_disabled)
    for text in ("/健康", "/健康 同意", "/健康 恢復", "/健康 吃藥了"):
        reply, _cancel, add = _routed(text)
        assert reply.call_args.args[1] == "爸媽健康：暫停使用中"
        add.assert_not_called()
    reply, _cancel, add = _routed("我剛剛跌倒了", user_id="U_DAD")
    assert all(call.args[1] != SAFETY for call in reply.call_args_list)
    add.assert_called_once()



@pytest.mark.parametrize(
    "quoted_text",
    ["爸媽健康卡：點開看爸媽近 7 天的整理", "股票推薦：只供參考｜示意", "FCN 評估：示意"],
)
def test_quoting_a_card_does_not_block_the_safety_line(fake_health, quoted_text):
    # GP1 post-review：引用卡片時的固定句不能搶在 119 前面。
    fake_health.decision = _decision()
    reply, _cancel, add = _routed("我剛剛跌倒了", quoted="MSG_CARD", raw_row=("__bot__", quoted_text), user_id="U_DAD")
    reply.assert_called_once()
    assert reply.call_args.args[1] == SAFETY
    add.assert_not_called()


def test_quoting_a_card_without_a_red_flag_still_gets_the_notice(fake_health):
    fake_health.decision = None
    reply, _cancel, _add = _routed("這樣正常嗎", quoted="MSG_CARD", raw_row=("__bot__", "爸媽健康卡：點開看爸媽近 7 天的整理"))
    assert reply.call_args.args[1] == NOTICE



# ── 點名咪寶的指令只由 main 判斷（GP2 R3 #1） ──────────────────────────────
@pytest.mark.parametrize("text", ["咪寶 /記住 爸爸剛剛跌倒了", "/記住 爸爸剛剛跌倒了", "／記住 爸爸剛剛跌倒了"])
def test_addressed_commands_skip_both_the_extractor_and_the_safety_check(fake_health, text):
    fake_health.decision = _decision()
    _routed(text, user_id="U_DAD")
    assert fake_health.calls["observe"] == []
    assert fake_health.calls["decision"] == []


@pytest.mark.parametrize("text", ["@成員甲/成員乙 爸爸剛剛跌倒了", "米寶 /記住 我剛剛跌倒了"])
def test_slash_text_main_does_not_treat_as_a_command_still_reaches_health(fake_health, text):
    # main 不認為是指令的，一定交給 parent_health（緊急字詞與抽取都不能被默默略過）。
    _routed(text, user_id="U_DAD")
    assert len(fake_health.calls["observe"]) == 1
    assert len(fake_health.calls["decision"]) == 1
