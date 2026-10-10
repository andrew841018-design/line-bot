"""股票推薦的路由與回覆測試（2026-10-10）。

走完整的 _handle_event：/股票 在提醒路徑之前被接走、不進 burst、不呼叫模型、不取消別人正在
累積的 burst、只用 reply token、不進 recall；FCN 與股票推薦共用的 _reply_command_card（送卡
前先驗、驗不過用同一個 token 回固定句）；引用 bot 投資卡片的固定句守門；lifespan 起停背景工作。
stock_picks 本身的資料、快照、背景工作測試在 test_stock_picks.py、test_stock_picks_data.py。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import types
from datetime import date
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
import stock_picks  # noqa: E402
from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent  # noqa: E402

T = date(2026, 10, 8)


# ── 測試工具（刻意不 import test_flex_menu／test_fcn_routing：別的對話也在改） ───
def _message(text: str, *, msg_id: str = "MSG_STOCK", quoted: str | None = None) -> MagicMock:
    message = MagicMock(spec=TextMessageContent)
    message.id = msg_id
    message.text = text
    message.mention = None
    message.quoted_message_id = quoted
    return message


def _event(message) -> MagicMock:
    source = MagicMock(spec=GroupSource)
    source.group_id = "G_TEST"
    source.user_id = "U_MEMBER"
    event = MagicMock(spec=MessageEvent)
    event.source = source
    event.message = message
    event.reply_token = "TOKEN_STOCK"
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


def _row_counts() -> tuple[int, int]:
    with memory._conn() as c:
        reminders = c.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        events = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return reminders, events


def _facts(code: str, name: str, *, p60: int = 2, kind: str = stock_picks.STOCK) -> stock_picks.Facts:
    return stock_picks.Facts(
        code=code, name=name, kind=kind, data_date=T,
        trend=stock_picks.Trend(close=100.0, ma60=99.8, ma240=90.0, p60=p60),
        revenue=stock_picks.Revenue(2026, 8, 10.0, 5.0) if kind == stock_picks.STOCK else None,
        pe=10.0 if kind == stock_picks.STOCK else None,
        pe_median=20.0 if kind == stock_picks.STOCK else None,
        inst_net=1_000_000 if kind == stock_picks.STOCK else None,
        cap=1e12 if kind == stock_picks.STOCK else None,
    )


def _picks_reply(names=(("2344", "華邦電"), ("3231", "緯創"))) -> stock_picks.Reply:
    picks = [_facts(code, name) for code, name in names]
    bubbles = [stock_picks.pick_bubble(p, i + 1, len(picks), None, lead_note=(i == 0)) for i, p in enumerate(picks)]
    bubbles.append(stock_picks.faq_bubble([_facts("0050", "元大台灣50", kind=stock_picks.ETF), _facts("2330", "台積電")], T))
    return stock_picks.Reply(
        flex=stock_picks.carousel(bubbles), alt_text=stock_picks.picks_alt_text(picks, T, not_updated=False)
    )


def _routed(text: str, *, quoted: str | None = None, raw_row=None, reply_obj=None, quoted_edge=None):
    """走完整 _handle_event，回傳 (_reply mock, cancel_burst mock, add_to_burst mock, handle mock)。"""
    handle = MagicMock(return_value=reply_obj if reply_obj is not None else _picks_reply())
    with (
        patch("main._reply") as reply,
        patch("main.burst_filter.cancel_burst") as cancel,
        patch("main.burst_filter.add_to_burst") as add,
        patch("main.memory.get_raw_message", return_value=raw_row),
        patch("main.memory.get_quoted_message_id", return_value=quoted_edge),
        patch("main._llm_chat", side_effect=AssertionError("stock picks must not call a model")),
        patch.object(stock_picks, "handle", handle),
    ):
        _handle_as_member(_event(_message(text, quoted=quoted)))
    return reply, cancel, add, handle


# ── 路由 ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["/股票", "／股票", "  /股票  ", "/股票推薦", "咪寶 /股票", "/ 股票"])
def test_bare_command_replies_with_the_picks_carousel(text):
    before = _row_counts()
    reply, cancel, add, handle = _routed(text)
    handle.assert_called_once()
    assert handle.call_args.args[0].strip() == ""
    assert isinstance(handle.call_args.kwargs["deadline"], float)
    reply.assert_called_once()
    args, kwargs = reply.call_args
    assert kwargs["flex_card"]["type"] == "carousel"
    assert args[1].startswith("股票推薦：只供參考｜華邦電、緯創")
    assert kwargs["index_for_recall"] is False
    cancel.assert_not_called()
    add.assert_not_called()
    assert _row_counts() == before


def test_menu_button_brings_the_buttons_back_on_the_card():
    reply, _cancel, _add, _handle = _routed("/股票")
    assert reply.call_args.kwargs["menu_buttons"] is True
    assert flex_menu.is_button_text("/股票")


def test_single_stock_command_passes_the_body_and_has_no_menu_buttons():
    detail = _facts("2330", "台積電")
    reply_obj = stock_picks.Reply(
        flex=stock_picks.detail_bubble(detail, market=None), alt_text=stock_picks.detail_alt_text(detail)
    )
    reply, _cancel, _add, handle = _routed("/股票 2330 台積電", reply_obj=reply_obj)
    assert handle.call_args.args[0].strip() == "2330 台積電"
    kwargs = reply.call_args.kwargs
    assert kwargs["flex_card"]["type"] == "bubble" and kwargs["menu_buttons"] is False
    assert reply.call_args.args[1].startswith("股票評估：只供參考｜台積電 2330｜")


def test_detail_buttons_on_cards_route_back_to_the_stock_command():
    """常問股卡上的「看…細節」會一直留在聊天紀錄裡可以點，送出的字要永遠接得到。"""
    reply_obj = _picks_reply()
    commands = [
        node["action"]["text"]
        for bubble in reply_obj.flex["contents"]
        for node in bubble["footer"]["contents"]
        if isinstance(node.get("action"), dict)
    ]
    assert commands and all(re.fullmatch(r"/股票 \d{4}", c) for c in commands)
    for command in commands:
        assert main._stock_command_body(command, _message(command)).strip() == command.split()[1]


def test_text_reply_never_pushes_and_never_echoes_input():
    reply, _cancel, _add, _handle = _routed(
        "/股票 NVDA @all", reply_obj=stock_picks.Reply(text=stock_picks.TEXT_US)
    )
    args, kwargs = reply.call_args
    assert args[1] == stock_picks.TEXT_US and "@" not in args[1]
    assert kwargs["allow_push_fallback"] is False and kwargs["index_for_recall"] is False
    assert kwargs.get("flex_card") is None


@pytest.mark.parametrize("text", ["股票", "/股市", "今天股票漲很多", "我的 /股票", "/FCN", "/觀點", "股票推薦"])
def test_other_text_is_not_a_stock_command(text):
    assert main._stock_command_body(text, _message(text)) is None


def test_detection_failure_falls_through(monkeypatch):
    monkeypatch.setattr(main, "_extract_gemini_trigger", MagicMock(side_effect=RuntimeError("x")))
    assert main._stock_command_body("咪寶 /股票", _message("咪寶 /股票")) is None


def test_import_failure_still_answers(monkeypatch):
    monkeypatch.setitem(sys.modules, "stock_picks", None)
    with (
        patch("main._reply") as reply,
        patch("main.burst_filter.add_to_burst") as add,
        patch("main._llm_chat", side_effect=AssertionError("no model")),
    ):
        _handle_as_member(_event(_message("/股票")))
    assert reply.call_args.args[1] == main._STOCK_FAILURE_TEXT
    add.assert_not_called()


def test_handle_crash_still_answers(monkeypatch):
    with (
        patch("main._reply") as reply,
        patch.object(stock_picks, "handle", MagicMock(side_effect=RuntimeError("boom"))),
        patch("main._llm_chat", side_effect=AssertionError("no model")),
    ):
        _handle_as_member(_event(_message("/股票 2330")))
    assert reply.call_args.args[1] == main._STOCK_FAILURE_TEXT


def test_real_handle_without_snapshot_says_preparing():
    with patch("main._reply") as reply, patch("main._llm_chat", side_effect=AssertionError("no model")):
        _handle_as_member(_event(_message("/股票")))
    assert reply.call_args.args[1] == stock_picks.TEXT_NOT_READY


# ── 共用的卡片回覆：送前先驗，驗不過用同一個 token 回固定句 ─────────────────
def test_card_failing_validation_gets_the_fallback_sentence(monkeypatch):
    monkeypatch.setattr(main, "_validated_flex_card_message", MagicMock(side_effect=ValueError("bad card")))
    reply, _cancel, _add, _handle = _routed("/股票")
    reply.assert_called_once()
    args, kwargs = reply.call_args
    assert args[0] == "TOKEN_STOCK" and args[1] == stock_picks.TEXT_CARD_FAILED
    assert kwargs.get("flex_card") is None and kwargs["allow_push_fallback"] is False


def test_alt_text_that_markdown_would_rewrite_is_never_sent():
    """兩個帶「*」的名字在同一行，`_md_to_line` 會當斜體吃掉（審查 r4 #1）。"""
    reply_obj = stock_picks.Reply(flex=_picks_reply().flex, alt_text="股票推薦：國巨*、愛普*｜資料到 10/08")
    reply, _cancel, _add, _handle = _routed("/股票", reply_obj=reply_obj)
    assert reply.call_args.args[1] == stock_picks.TEXT_CARD_FAILED


def test_detail_fallback_uses_the_evaluation_prefix():
    detail = _facts("2330", "台積電")
    reply_obj = stock_picks.Reply(flex={"type": "bubble", "body": {"type": "span", "text": "可以買進"}},
                                  alt_text=stock_picks.detail_alt_text(detail),
                                  fallback_text="股票評估：卡片暫時出不來，請稍後再試")
    reply, _cancel, _add, _handle = _routed("/股票 2330", reply_obj=reply_obj)
    assert reply.call_args.args[1] == "股票評估：卡片暫時出不來，請稍後再試"


def test_mention_targets_are_cleared_before_replying():
    with patch.object(main, "_clear_reply_mention_targets") as clear:
        _routed("/股票")
    clear.assert_called_with("TOKEN_STOCK")


def test_flex_card_still_has_exactly_one_call_site():
    source = Path(main.__file__).read_text(encoding="utf-8")
    assert len(re.findall(r"\bflex_card=(?!None)", source)) == 1


@pytest.mark.parametrize(
    "card",
    [
        {"type": "bubble", "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "看這裡", "contents": [{"type": "span", "text": "可以買進"}]}]}},
        {"type": "bubble", "body": {"type": "box", "layout": "vertical", "contents": [{"type": "image", "url": "x"}]}},
        {"type": "bubble", "body": {"type": "box", "layout": "vertical", "contents": [{"type": "video"}]}},
    ],
)
def test_flex_whitelist_rejects_unknown_node_types(card):
    with pytest.raises(ValueError):
        main._validated_flex_card_message("股票推薦：x", card)


def test_flex_whitelist_accepts_the_real_cards():
    reply_obj = _picks_reply()
    main._validated_flex_card_message(reply_obj.alt_text, reply_obj.flex)


# ── recall、推播、/help ─────────────────────────────────────────────────────
def test_inbound_stock_command_is_not_indexed_for_recall():
    calls = []
    with patch("main.memory.log_raw_message", side_effect=lambda *a, **k: calls.append((a, k))):
        _routed("/股票 2330")
    inbound = [kw for args, kw in calls if len(args) >= 4 and args[1] == "MSG_STOCK"]
    assert inbound and inbound[0]["index_for_recall"] is False


def test_investment_command_detection_is_shared():
    assert main._investment_command("/股票 2330", _message("/股票 2330")) == ("stock", " 2330")
    assert main._investment_command("/FCN", _message("/FCN")) == ("fcn", "")
    assert main._investment_command("今天股票漲", _message("今天股票漲")) is None


def test_market_quote_guard_covers_stock_texts():
    for text in (stock_picks.TEXT_NOT_READY, stock_picks.TEXT_US, "股票推薦：只供參考｜華邦電｜資料到 10/08"):
        assert main._is_market_quote_outbound(text)


def test_help_explains_the_steps_without_mentions():
    help_text = main._HELP_TEXT
    for needed in ("/股票", "📈 股票推薦", "往左滑", "/股票 2330", "細節", "資料日期", "只供參考"):
        assert needed in help_text, needed
    assert "@" not in help_text


# ── 引用 bot 的投資卡片：固定句，不叫模型、不進 burst、不建提醒 ───────────────
@pytest.mark.parametrize(
    "quoted_text, kind",
    [("股票推薦：只供參考｜華邦電、緯創｜資料到 10/08", "stock"),
     ("股票評估：只供參考｜台積電 2330｜⚠️ 漲多了（比季線高 8.0%）", "stock"),
     ("FCN 評估：NVDA、AMD｜CP值普通", "fcn")],
)
def test_quoting_an_investment_card_gets_the_fixed_sentence(quoted_text, kind):
    before = _row_counts()
    reply, cancel, add, handle = _routed("咪寶 這支明天提醒我買", quoted="Q1", raw_row=("__bot__", quoted_text))
    handle.assert_not_called()
    reply.assert_called_once()
    args, kwargs = reply.call_args
    assert args[1] == (main._FCN_QUOTE_TEXT if kind == "fcn" else main._INVESTMENT_QUOTE_TEXT)
    assert args[1].startswith("FCN 評估：" if kind == "fcn" else "股票評估：")
    assert kwargs["allow_push_fallback"] is False and kwargs["index_for_recall"] is False
    cancel.assert_not_called()
    add.assert_not_called()
    assert _row_counts() == before


def test_persisted_quote_edge_is_also_checked():
    reply, _cancel, _add, _handle = _routed(
        "這支可以買嗎", quoted=None, raw_row=("__bot__", "股票推薦：只供參考｜華邦電｜資料到 10/08"), quoted_edge="Q9"
    )
    assert reply.call_args.args[1] == main._INVESTMENT_QUOTE_TEXT


@pytest.mark.parametrize(
    "raw_row",
    [("U_OTHER", "股票推薦：我自己打的"), ("__bot__", "明天記得帶傘"), None],
)
def test_other_quotes_are_not_guarded(raw_row):
    with patch("main.memory.get_raw_message", return_value=raw_row), patch(
        "main.memory.get_quoted_message_id", return_value=None
    ):
        assert main._quoted_bot_investment_kind(_event(_message("x", quoted="Q1")), "G_TEST") is None


def test_stock_command_while_quoting_a_card_still_runs_the_command():
    reply, _cancel, _add, handle = _routed("/股票 2330", quoted="Q1", raw_row=("__bot__", "股票推薦：只供參考｜華邦電"))
    handle.assert_called_once()


# ── lifespan ───────────────────────────────────────────────────────────────
def _lifespan_stubs(monkeypatch, order):
    monkeypatch.delenv("JOBS_ROUTES_ENABLED", raising=False)
    monkeypatch.setattr(main, "_configure_local_text_llm_runtime", lambda: None)
    monkeypatch.setattr(main, "_start_local_vision_worker", lambda: None)
    fake_vision = types.ModuleType("vision_llm")
    fake_vision.shutdown_background_worker = lambda: order.append("vision_stop")
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)
    fake_pending = types.ModuleType("pending_store")
    fake_pending.harden_media_permissions = lambda: 0
    fake_pending.sweep_orphan_media = lambda: 0
    fake_pending.sweep_delivery_lock_files = lambda: 0
    monkeypatch.setitem(sys.modules, "pending_store", fake_pending)
    monkeypatch.setattr(main, "_process_pending_on_startup", lambda: None)
    monkeypatch.setattr(main, "_init_on_startup", lambda: order.append("init"))


def _run_lifespan():
    app = SimpleNamespace(state=SimpleNamespace())

    async def run():
        async with main._app_lifespan(app):
            pass

    asyncio.run(run())


def test_lifespan_starts_and_stops_stock_picks(monkeypatch):
    order: list[str] = []
    _lifespan_stubs(monkeypatch, order)
    monkeypatch.setattr(stock_picks, "start_background", lambda: order.append("stock_start"))
    monkeypatch.setattr(stock_picks, "stop_background", lambda timeout=2.0: order.append(f"stock_stop:{timeout}"))
    _run_lifespan()
    assert order[:2] == ["init", "stock_start"] and "stock_stop:2.0" in order


def test_lifespan_survives_stock_picks_failures(monkeypatch):
    order: list[str] = []
    _lifespan_stubs(monkeypatch, order)
    monkeypatch.setattr(stock_picks, "start_background", MagicMock(side_effect=RuntimeError("start")))
    monkeypatch.setattr(stock_picks, "stop_background", MagicMock(side_effect=RuntimeError("stop")))
    _run_lifespan()
    assert "vision_stop" in order
    order.clear()
    monkeypatch.setitem(sys.modules, "stock_picks", None)
    _run_lifespan()
    assert order == ["init", "vision_stop"]


def test_conftest_keeps_the_real_background_off():
    assert stock_picks._BACKGROUND_ENABLED is False
    stock_picks.start_background()
    assert stock_picks._thread is None or not stock_picks._thread.is_alive()


# ── 零寬字元（實作複核 Codex p1 #4） ────────────────────────────────────────
@pytest.mark.parametrize("text", ["/股​票 2330", "​/股票", "/⁠股票推薦", "咪寶 /股‍票"])
def test_zero_width_characters_do_not_hide_the_command(text):
    reply, cancel, add, handle = _routed(text)
    handle.assert_called_once()
    cancel.assert_not_called()
    add.assert_not_called()


def test_zero_width_fcn_is_also_detected():
    assert main._fcn_command_body("/F​CN", _message("/F​CN")) == ""
    assert main._investment_command("/股​票", _message("/股​票")) == ("stock", "")


def test_inbound_zero_width_command_is_not_indexed_for_recall():
    calls = []
    with patch("main.memory.log_raw_message", side_effect=lambda *a, **k: calls.append((a, k))):
        _routed("/股​票 2330")
    inbound = [kw for args, kw in calls if len(args) >= 4 and args[1] == "MSG_STOCK"]
    assert inbound and inbound[0]["index_for_recall"] is False



def test_zero_width_inside_the_bot_name_is_still_addressed():
    reply, _cancel, _add, handle = _routed("咪\u200b寶 /股票 2330")
    handle.assert_called_once()
    assert handle.call_args.args[0].strip() == "2330"



def test_zero_width_bot_name_with_another_mention_later_in_the_text():
    text = "咪\u200b寶 /股票 2330，是咪寶推薦的嗎"
    body = main._stock_command_body(text, _message(text))
    assert body is not None and body.strip().startswith("2330")
