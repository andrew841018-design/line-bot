"""市場盤勢（/盤勢，2026-10-17）的路由與回覆測試。

走完整的 _handle_event：/盤勢 在提醒路徑之前被接走、不進 burst、不呼叫模型、不取消別人正在累積的
burst、只用 reply token、不進 recall；卡片走 _reply_command_card(page=True)；圖走
_reply_market_chart（送圖前先 durable 結案、LINE 接受後用記下的 identity 升成 replied、
結果不明不再送）；引用盤勢卡／盤勢圖回固定句；週報與每晚記憶不吃盤勢的存檔字串。
market_dashboard 本身的卡片測試在 test_market_dashboard.py；這裡用假的模組，main 的接線不依賴它的內容。
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
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
from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent  # noqa: E402

# 卡片上的按鈕會一直留在聊天紀錄裡可以點：這份只能加不能刪，每個都要接得到 /盤勢 路由。
SHIPPED_MARKET_TEXTS = (
    "/盤勢",
    "/盤勢 VOO",
    "/盤勢 QQQ",
    "/盤勢 SOXX",
    "/盤勢 0050",
    "/盤勢 期權",
    "/盤勢 新聞",
    "/盤勢 季節性",
    "/盤勢 選舉",
    "/盤勢 QQQ 圖",
    "/盤勢 SPY 圖",
    "/盤勢 SOXX 圖",
    "/盤勢 台指 圖",
    "/盤勢 13F QQQ",
    "/盤勢 13F VOO",
    "/盤勢 13F SOXX",
)

_CARD = {
    "type": "carousel",
    "contents": [
        {"type": "bubble", "size": "mega", "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "美股（1/4）｜10/16 收盤", "wrap": True}]}},
    ],
}


def _message(text: str, *, msg_id: str = "MSG_MARKET", quoted: str | None = None) -> MagicMock:
    message = MagicMock(spec=TextMessageContent)
    message.id = msg_id
    message.text = text
    message.mention = None
    message.quoted_message_id = quoted
    return message


def _event(message, token: str = "TOKEN_MARKET") -> MagicMock:
    source = MagicMock(spec=GroupSource)
    source.group_id = "G_TEST"
    source.user_id = "U_MEMBER"
    event = MagicMock(spec=MessageEvent)
    event.source = source
    event.message = message
    event.reply_token = token
    return event


def _handle_as_member(event) -> None:
    with (
        patch("main.line_mentions.user_id_for_family_role", return_value="U_SISTER"),
        patch("main.memory.begin_inbound_event", return_value="new"),
        patch("main._spawn_piggyback_drain"),
        patch.object(main.settings, "allowed_group_ids_raw", ""),
        patch.object(main.settings, "allowed_group_id", ""),
        patch("main.feedback_collector.in_feedback_window", return_value=False),
    ):
        main._handle_event(event)


def _fake_module(handle) -> types.ModuleType:
    module = types.ModuleType("market_dashboard")
    module.handle = handle
    return module


def _routed(text: str, monkeypatch, *, reply_obj=None, raw_row=None, quoted: str | None = None, handle=None):
    handle = handle or MagicMock(return_value=reply_obj if reply_obj is not None else SimpleNamespace(
        flex=_CARD, alt_text="盤勢：美股收在月線上｜台股外資賣超", text=None, fallback_text=None, images=None,
        image_alt=None))
    monkeypatch.setitem(sys.modules, "market_dashboard", _fake_module(handle))
    with (
        patch("main._reply") as reply,
        patch("main.burst_filter.cancel_burst") as cancel,
        patch("main.burst_filter.add_to_burst") as add,
        patch("main.memory.get_raw_message", return_value=raw_row),
        patch("main.memory.get_quoted_message_id", return_value=None),
        patch("main._llm_chat", side_effect=AssertionError("/盤勢 must not call a model")),
    ):
        _handle_as_member(_event(_message(text, quoted=quoted)))
    return reply, cancel, add, handle


# ── 路由 ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["/盤勢", "／盤勢", "  /盤勢  ", "咪寶 /盤勢", "/ 盤勢", "/\u200b盤勢"])
def test_bare_command_replies_with_the_card(text, monkeypatch):
    reply, cancel, add, handle = _routed(text, monkeypatch)
    handle.assert_called_once()
    assert handle.call_args.args[0].strip() == ""
    reply.assert_called_once()
    args, kwargs = reply.call_args
    assert args[1].startswith("盤勢：")
    assert kwargs["flex_card"]["type"] == "carousel"
    assert kwargs["index_for_recall"] is False
    cancel.assert_not_called()
    add.assert_not_called()


def test_menu_button_is_the_market_command(monkeypatch):
    labels = dict(flex_menu.BUTTONS)
    assert labels.get("🧭 市場盤勢") == "/盤勢"
    assert "🚦 過濾規則" not in labels
    assert len(flex_menu.BUTTONS) == 13
    reply, _cancel, _add, _handle = _routed("/盤勢", monkeypatch)
    assert reply.call_args.kwargs["menu_buttons"] is True


def test_retired_rules_button_still_routes_to_the_rules_command():
    assert flex_menu.is_button_text("/規則")
    with patch.object(main, "_handle_command", return_value="目前沒有過濾規則。") as handled:
        with patch("main._reply"):
            _handle_as_member(_event(_message("/規則", msg_id="MSG_RULES"), token="TOKEN_RULES"))
    handled.assert_called()


@pytest.mark.parametrize("text", SHIPPED_MARKET_TEXTS)
def test_every_shipped_button_text_reaches_the_market_command(text, monkeypatch):
    _reply, _cancel, _add, handle = _routed(text, monkeypatch)
    handle.assert_called_once()
    assert handle.call_args.args[0].strip() == text[len("/盤勢"):].strip()


@pytest.mark.parametrize("text", ["盤勢", "今天盤勢如何", "/盤勢圖", "我的 /盤勢", "/股票", "/觀點"])
def test_other_text_is_not_a_market_command(text):
    body = main._market_command_body(text, _message(text))
    if text == "/盤勢圖":  # 「/盤勢圖」也算 /盤勢，後面的字交給 market_dashboard 解讀
        assert body == "圖"
    else:
        assert body is None


def test_card_reply_asks_for_the_large_text_page(monkeypatch):
    with patch.object(main, "_reply_command_card") as card_reply:
        monkeypatch.setitem(sys.modules, "market_dashboard", _fake_module(MagicMock(return_value=SimpleNamespace(
            flex=_CARD, alt_text="盤勢：x", text=None, fallback_text=None, images=None, image_alt=None))))
        main._reply_market(_event(_message("/盤勢")), "G_TEST", "/盤勢", "")
    assert card_reply.call_args.kwargs["page"] is True
    assert "盤勢：" in main._CARD_PAGE_ALT_PREFIXES


def test_text_reply_never_pushes(monkeypatch):
    reply_obj = SimpleNamespace(flex=None, alt_text=None, text="盤勢：資料準備中，請過幾分鐘再按一次",
                                fallback_text=None, images=None, image_alt=None)
    reply, _cancel, _add, _handle = _routed("/盤勢 abc @all", monkeypatch, reply_obj=reply_obj)
    args, kwargs = reply.call_args
    assert args[1] == "盤勢：資料準備中，請過幾分鐘再按一次"
    assert kwargs["allow_push_fallback"] is False and kwargs["index_for_recall"] is False


def test_import_failure_still_answers(monkeypatch):
    monkeypatch.setitem(sys.modules, "market_dashboard", None)  # import 會丟 ImportError
    with patch("main._reply") as reply, patch("main._llm_chat", side_effect=AssertionError):
        _handle_as_member(_event(_message("/盤勢")))
    assert reply.call_args.args[1] == main._MARKET_FAILURE_TEXT


def test_handle_crash_still_answers(monkeypatch):
    reply, _cancel, _add, _handle = _routed("/盤勢", monkeypatch, handle=MagicMock(side_effect=RuntimeError("x")))
    assert reply.call_args.args[1] == main._MARKET_FAILURE_TEXT


def test_inbound_market_command_is_not_indexed_for_recall(monkeypatch):
    calls = []
    with patch("main.memory.log_raw_message", side_effect=lambda *a, **k: calls.append((a, k))):
        _routed("/盤勢 QQQ", monkeypatch)
    inbound = [kw for args, kw in calls if len(args) >= 4 and args[1] == "MSG_MARKET"]
    assert inbound and inbound[0]["index_for_recall"] is False


def test_investment_command_detection_includes_market():
    assert main._investment_command("/盤勢 QQQ", _message("/盤勢 QQQ")) == ("market", " QQQ")
    assert main._investment_command("今天盤勢", _message("今天盤勢")) is None


def test_reply_only_guard_uses_the_prefix_with_colon():
    assert main._is_market_quote_outbound("盤勢：美股收在月線上")
    assert main._is_market_quote_outbound("盤勢圖：QQQ 期權未平倉")
    assert not main._is_market_quote_outbound("盤勢不錯，可以再觀察")


def test_help_explains_the_market_commands_without_mentions():
    help_text = main._HELP_TEXT
    for needed in ("/盤勢", "🧭 市場盤勢", "/盤勢 QQQ", "/盤勢 QQQ 圖", "/盤勢 13F QQQ", "不是買賣建議"):
        assert needed in help_text, needed
    assert "@" not in help_text


# ── 引用盤勢卡／盤勢圖 ───────────────────────────────────────────────────────
@pytest.mark.parametrize("quoted_text", ["盤勢：美股收在月線上｜台股外資賣超", "盤勢圖：QQQ 期權未平倉與 gamma（假設部位）｜資料 10/16"])
def test_quoting_a_market_card_gets_the_fixed_sentence(quoted_text, monkeypatch):
    reply, cancel, add, handle = _routed(
        "現在可以買嗎？", monkeypatch, raw_row=("__bot__", quoted_text), quoted="MSG_CARD")
    handle.assert_not_called()
    assert reply.call_args.args[1] == main._MARKET_QUOTE_TEXT
    assert reply.call_args.kwargs["allow_push_fallback"] is False
    add.assert_not_called()


# ── 圖片回覆 ────────────────────────────────────────────────────────────────
@pytest.fixture
def chart_files(tmp_path, monkeypatch):
    names = ["a" * 32 + ".png", "b" * 32 + ".png"]
    for name in names:
        (tmp_path / name).write_bytes(b"\x89PNG fake")
    monkeypatch.setattr(main, "_GENIMG_DIR", str(tmp_path))
    fake_pages = types.ModuleType("card_pages")
    fake_pages.public_base_url = lambda: "https://abc-def.trycloudflare.com"
    monkeypatch.setitem(sys.modules, "card_pages", fake_pages)
    return names


def _line_api(response=None, error=None):
    api = MagicMock()
    if error is not None:
        api.return_value.reply_message.side_effect = error
    else:
        api.return_value.reply_message.return_value = response or SimpleNamespace(
            sent_messages=[SimpleNamespace(id="SENT_IMG_1"), SimpleNamespace(id="SENT_IMG_2")])
    return api


def _chart(files, *, api, muted=False, register=True, pre_mark=True, token="TOKEN_CHART"):
    if register:
        main._register_inbound_reply_token(token, "G_TEST", "MSG_CHART")
    with (
        patch.object(main.settings, "bot_muted", muted),
        patch.object(main, "MessagingApi", api),
        patch.object(main, "ApiClient", MagicMock()),
        patch.object(main, "_get_line_config", return_value=None),
        patch("main.memory.mark_inbound_events_completed_no_reply", return_value=1 if pre_mark else 0) as pre,
        patch("main.memory.mark_inbound_event_replied") as replied,
        patch("main.memory.log_raw_message") as archived,
        patch("main._reply", return_value=True) as text_reply,
    ):
        main._reply_market_chart(_event(_message("/盤勢 QQQ 圖"), token=token), "G_TEST", files,
                                 "盤勢圖：QQQ 期權未平倉與 gamma（假設部位）｜資料 10/16",
                                 fallback_text=main._MARKET_CHART_FAILURE_TEXT)
    return pre, replied, archived, text_reply


def test_chart_is_sent_after_a_durable_mark_and_upgraded_to_replied(chart_files):
    api = _line_api()
    pre, replied, archived, text_reply = _chart(chart_files, api=api)
    pre.assert_called_once()
    request = api.return_value.reply_message.call_args.args[0]
    assert request.reply_token == "TOKEN_CHART"
    assert [m.original_content_url for m in request.messages] == [
        f"https://abc-def.trycloudflare.com/static/img/{name}" for name in chart_files]
    replied.assert_called_once_with("G_TEST", "MSG_CHART")
    assert archived.call_count == 2
    for call in archived.call_args_list:
        assert call.args[2] == "__bot__" and call.args[3].startswith("盤勢圖：")
        assert call.kwargs["index_for_recall"] is False
    text_reply.assert_not_called()


def test_muted_bot_sends_nothing_but_closes_the_event(chart_files):
    api = _line_api()
    pre, replied, _archived, text_reply = _chart(chart_files, api=api, muted=True)
    api.return_value.reply_message.assert_not_called()
    text_reply.assert_not_called()
    pre.assert_called_once()
    replied.assert_not_called()


def test_missing_public_url_sends_the_fixed_sentence(chart_files, monkeypatch):
    sys.modules["card_pages"].public_base_url = lambda: None
    api = _line_api()
    _pre, _replied, _archived, text_reply = _chart(chart_files, api=api)
    api.return_value.reply_message.assert_not_called()
    kwargs = text_reply.call_args.kwargs
    assert text_reply.call_args.args[1] == main._MARKET_CHART_FAILURE_TEXT
    assert kwargs["allow_push_fallback"] is False and kwargs["include_auxiliary"] is False
    assert kwargs["index_for_recall"] is False


@pytest.mark.parametrize("bad", [["../etc/passwd"], ["A" * 32 + ".png"], ["c" * 32 + ".png"], []])
def test_bad_or_missing_chart_files_send_the_fixed_sentence(bad, chart_files):
    api = _line_api()
    _pre, _replied, _archived, text_reply = _chart(bad, api=api)
    api.return_value.reply_message.assert_not_called()
    assert text_reply.call_args.args[1] == main._MARKET_CHART_FAILURE_TEXT


def test_symlinked_chart_is_refused(chart_files, tmp_path):
    target = tmp_path / ("d" * 32 + ".png")
    os.symlink("/etc/hosts", target)
    api = _line_api()
    _chart([target.name], api=api)
    api.return_value.reply_message.assert_not_called()


def test_failed_durable_mark_sends_no_image(chart_files):
    api = _line_api()
    _pre, replied, _archived, text_reply = _chart(chart_files, api=api, pre_mark=False)
    api.return_value.reply_message.assert_not_called()
    assert text_reply.call_args.args[1] == main._MARKET_CHART_FAILURE_TEXT
    replied.assert_not_called()


def test_line_rejecting_the_images_gets_the_fixed_sentence(chart_files):
    error = Exception("(400) Bad Request: invalid image url")
    error.status = 400
    api = _line_api(error=error)
    _pre, replied, _archived, text_reply = _chart(chart_files, api=api)
    assert text_reply.call_args.args[1] == main._MARKET_CHART_FAILURE_TEXT
    replied.assert_called_once_with("G_TEST", "MSG_CHART")


@pytest.mark.parametrize("status, body", [(500, "server error"), (None, "timed out"),
                                          (400, "Invalid reply token")])
def test_ambiguous_or_token_failures_send_nothing_more(chart_files, status, body):
    error = Exception(body)
    if status is not None:
        error.status = status
    api = _line_api(error=error)
    _pre, replied, archived, text_reply = _chart(chart_files, api=api)
    text_reply.assert_not_called()
    replied.assert_not_called()
    archived.assert_not_called()


def test_redelivery_after_an_ambiguous_failure_is_already_terminal(chart_files):
    """真的 sqlite：送圖前已經 durable 結案，所以 lease 過期後 LINE 再送同一則訊息，
    begin_inbound_event 回的是終態，main 不會再處理一次（不會送第二張圖）。"""
    assert memory.begin_inbound_event("G_TEST", "MSG_REDELIVER") == "new"
    main._register_inbound_reply_token("TOKEN_RD", "G_TEST", "MSG_REDELIVER")
    error = Exception("timed out")
    with (
        patch.object(main.settings, "bot_muted", False),
        patch.object(main, "MessagingApi", _line_api(error=error)),
        patch.object(main, "ApiClient", MagicMock()),
        patch.object(main, "_get_line_config", return_value=None),
        patch("main._reply", return_value=True),
    ):
        main._reply_market_chart(_event(_message("/盤勢 QQQ 圖"), token="TOKEN_RD"), "G_TEST", chart_files,
                                 "盤勢圖：x", fallback_text=main._MARKET_CHART_FAILURE_TEXT)
    assert memory.begin_inbound_event("G_TEST", "MSG_REDELIVER") == "completed_no_reply"


def test_accepted_chart_ends_as_replied_in_the_real_store(chart_files):
    assert memory.begin_inbound_event("G_TEST", "MSG_OK") == "new"
    main._register_inbound_reply_token("TOKEN_OK", "G_TEST", "MSG_OK")
    with (
        patch.object(main.settings, "bot_muted", False),
        patch.object(main, "MessagingApi", _line_api()),
        patch.object(main, "ApiClient", MagicMock()),
        patch.object(main, "_get_line_config", return_value=None),
        patch("main.memory.log_raw_message"),
    ):
        main._reply_market_chart(_event(_message("/盤勢 QQQ 圖"), token="TOKEN_OK"), "G_TEST", chart_files,
                                 "盤勢圖：x", fallback_text=main._MARKET_CHART_FAILURE_TEXT)
    assert memory.begin_inbound_event("G_TEST", "MSG_OK") == "replied"


def test_chart_reply_from_the_router(chart_files, monkeypatch):
    reply_obj = SimpleNamespace(flex=None, alt_text=None, text=None, fallback_text=None,
                                images=chart_files, image_alt="盤勢圖：QQQ 期權未平倉與 gamma（假設部位）｜資料 10/16")
    monkeypatch.setitem(sys.modules, "market_dashboard", _fake_module(MagicMock(return_value=reply_obj)))
    with patch.object(main, "_reply_market_chart") as chart:
        main._reply_market(_event(_message("/盤勢 QQQ 圖")), "G_TEST", "/盤勢 QQQ 圖", " QQQ 圖")
    args, kwargs = chart.call_args
    assert args[2] == chart_files and args[3].startswith("盤勢圖：")
    assert kwargs["fallback_text"] == main._MARKET_CHART_FAILURE_TEXT


# ── 模型的間接路徑：週報與每晚記憶不吃盤勢的存檔字串（審查 GP2 I2） ─────────────
def test_weekly_summary_excludes_market_rows():
    import weekly_summary

    assert "盤勢：" in weekly_summary._MARKET_PREFIXES and "盤勢圖：" in weekly_summary._MARKET_PREFIXES
    source = Path(weekly_summary.__file__).read_text(encoding="utf-8")
    assert "*_MARKET_PREFIXES" in source


def test_daily_memory_excludes_market_rows():
    sys.path.insert(0, str(Path(main.__file__).resolve().parent / "jobs"))
    try:
        import daily_memory
    finally:
        sys.path.pop(0)
    import member_labels

    assert daily_memory._private_bot_reply(member_labels.BOT_USER_ID, "盤勢：美股收在月線上")
    assert daily_memory._private_bot_reply(member_labels.BOT_USER_ID, "盤勢圖：QQQ 期權未平倉")
    assert not daily_memory._private_bot_reply(member_labels.BOT_USER_ID, "今天天氣很好")
