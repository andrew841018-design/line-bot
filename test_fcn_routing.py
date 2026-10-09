"""FCN 評估的路由與回覆測試（2026-10-09）。

走完整的 _handle_event：/FCN 在提醒路徑之前被接走、不進 burst、不呼叫模型、不取消
別人正在累積的 burst、只用 reply token、不進 recall；_reply(flex_card=…) 的不變式。
fcn_eval 本身的解析／模擬／卡片測試在 test_fcn_eval.py。
"""

from __future__ import annotations

import math
import os
import re
import socket
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import fcn_eval  # noqa: E402
import flex_menu  # noqa: E402
import main  # noqa: E402
import memory  # noqa: E402
from linebot.v3.messaging import FlexMessage  # noqa: E402
from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent  # noqa: E402


# ── 測試工具（刻意不 import test_flex_menu：那支檔案別的對話也在改） ─────────
def _message(text: str, *, msg_id: str = "MSG_FCN", quoted: str | None = None) -> MagicMock:
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
    event.reply_token = "TOKEN_FCN"
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


def _fake_line_api(*, reply_side_effect=None) -> MagicMock:
    api = MagicMock()
    if reply_side_effect is not None:
        api.reply_message.side_effect = reply_side_effect
    else:
        api.reply_message.return_value = SimpleNamespace(
            sent_messages=[SimpleNamespace(id="SENT_FCN", quote_token=None)]
        )
    return api


def _chart(symbol: str, *, sigma: float = 0.4, currency: str = "USD", tz: str = "America/New_York",
           itype: str = "EQUITY", seed: int = 1) -> dict:
    """合成的 Yahoo chart JSON：約 260 個交易日、最後一天是今天。"""
    import numpy as np

    zone = ZoneInfo(tz)
    rng = np.random.default_rng(seed)
    days = []
    d = datetime.now(zone).date()
    while len(days) < 260:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    price = 100.0
    closes = []
    for _ in days:
        price *= math.exp(rng.normal(0, sigma / math.sqrt(252)))
        closes.append(round(price, 4))
    stamps = [int(datetime(x.year, x.month, x.day, 16, 0, tzinfo=zone).timestamp()) for x in days]
    return {
        "chart": {
            "result": [
                {
                    "meta": {"instrumentType": itype, "currency": currency, "exchangeTimezoneName": tz},
                    "timestamp": stamps,
                    "indicators": {"quote": [{"close": closes}], "adjclose": [{"adjclose": closes}]},
                    "events": {},
                }
            ]
        }
    }


@pytest.fixture(autouse=True)
def _fcn_offline(monkeypatch):
    """不連網：fcn_eval 的所有抓資料都換成合成資料；任何漏掉的網路呼叫直接失敗。"""

    def _refuse(*_args, **_kwargs):
        raise AssertionError("network disabled in test_fcn_routing")

    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(fcn_eval, "_http_get_json", _refuse)
    monkeypatch.setattr(fcn_eval, "_fetch_chart", lambda symbol, range_value="1y": _chart(symbol, seed=len(symbol)))
    monkeypatch.setattr(fcn_eval, "_fetch_net_income", lambda symbol: (1.0, 2.0, 3.0, 4.0))
    monkeypatch.setattr(fcn_eval, "_fetch_usd_rf", lambda: 0.04)
    monkeypatch.setattr(fcn_eval, "N_PATHS", 3000)
    fcn_eval.clear_caches()
    yield
    fcn_eval.clear_caches()


def _routed(text: str, *, quoted: str | None = None, raw_row=None):
    """走完整 _handle_event，回傳 (_reply 的 mock, cancel_burst mock, add_to_burst mock)。"""
    with (
        patch("main._reply") as reply,
        patch("main.burst_filter.cancel_burst") as cancel,
        patch("main.burst_filter.add_to_burst") as add,
        patch("main.memory.get_raw_message", return_value=raw_row),
        patch("main._llm_chat", side_effect=AssertionError("FCN must not call a model")),
    ):
        _handle_as_member(_event(_message(text, quoted=quoted)))
    return reply, cancel, add


# ── 路由 ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["/FCN", "／ＦＣＮ", "  /fcn  ", "/FCN評估", "咪寶 /FCN"])
def test_bare_fcn_replies_with_usage_text_and_menu_buttons(text):
    before = _row_counts()
    reply, cancel, add = _routed(text)
    reply.assert_called_once()
    args, kwargs = reply.call_args
    assert args[1] == fcn_eval.USAGE_TEXT
    assert args[1].startswith("FCN 評估：")
    assert kwargs["allow_push_fallback"] is False
    assert kwargs["index_for_recall"] is False
    assert kwargs.get("include_auxiliary", True) is True
    assert kwargs.get("flex_card") is None
    cancel.assert_not_called()
    add.assert_not_called()
    assert _row_counts() == before


def test_menu_button_text_brings_the_buttons_back():
    reply, _cancel, _add = _routed("/FCN")
    assert reply.call_args.kwargs["menu_buttons"] is True
    assert flex_menu.is_button_text("/FCN")


def test_full_command_replies_with_evaluation_card_and_creates_nothing():
    before = _row_counts()
    reply, cancel, add = _routed("/FCN NVDA AMD 年利率12% 12個月")
    reply.assert_called_once()
    args, kwargs = reply.call_args
    card = kwargs["flex_card"]
    assert isinstance(card, dict) and card["type"] == "bubble"
    assert args[1].startswith("FCN 評估：NVDA、AMD｜")
    assert kwargs["index_for_recall"] is False
    assert "menu_buttons" not in kwargs
    cancel.assert_not_called()
    add.assert_not_called()
    assert _row_counts() == before  # 「12個月」沒有被當成日期建提醒


def test_twelve_months_is_not_read_as_a_reminder_date():
    with patch("main._try_handle_contextual_date_reminder", side_effect=AssertionError("too late")):
        reply, _cancel, _add = _routed("/FCN NVDA 年利率12% 12個月")
    assert "flex_card" in reply.call_args.kwargs


def test_quoting_a_banker_message_returns_the_confirm_card():
    banker = "連結標的：輝達 NVDA、超微 AMD｜Tenor 6M｜KI 60%｜年化票息 17.89% p.a.｜最低申購 USD 50,000"
    reply, _cancel, _add = _routed("/FCN 幫我看", quoted="Q1", raw_row=("U_OTHER", banker))
    card = reply.call_args.kwargs["flex_card"]
    header = card["header"]["contents"][0]["text"]
    assert header == "FCN 評估：我讀到這些條件"
    button = card["footer"]["contents"][0]["action"]
    assert button["type"] == "message" and button["text"].startswith("/FCN NVDA AMD 年利率17.89%")


def test_quoting_a_bot_message_returns_the_fixed_sentence():
    reply, _cancel, _add = _routed("/FCN", quoted="Q1", raw_row=("__bot__", fcn_eval.USAGE_TEXT))
    assert reply.call_args.args[1] == fcn_eval.TEXT_QUOTED_BOT


def test_quoted_message_not_found_returns_the_missing_sentence():
    reply, _cancel, _add = _routed("/FCN", quoted="Q_GONE", raw_row=None)
    assert reply.call_args.args[1] == fcn_eval.TEXT_QUOTED_MISSING


def test_mentions_never_survive_into_the_reply():
    reply, _cancel, _add = _routed("/FCN 輝達 @all 幫我看")
    text = reply.call_args.args[1]
    assert "@" not in text and "＠" not in text
    assert text.startswith("FCN 評估：")


def test_import_failure_still_answers_with_a_fixed_sentence(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "fcn_eval", None)  # import fcn_eval → ImportError
    reply, cancel, add = _routed("/FCN NVDA 年利率12%")
    assert reply.call_args.args[1] == main._FCN_FAILURE_TEXT
    cancel.assert_not_called()
    add.assert_not_called()


def test_evaluation_crash_still_answers_with_a_fixed_sentence(monkeypatch):
    monkeypatch.setattr(fcn_eval, "handle", MagicMock(side_effect=RuntimeError("boom")))
    reply, _cancel, add = _routed("/FCN NVDA 年利率12%")
    assert reply.call_args.args[1] == main._FCN_FAILURE_TEXT
    add.assert_not_called()


def test_detection_failure_falls_through_to_normal_routing(monkeypatch):
    monkeypatch.setattr(main, "_extract_gemini_trigger", MagicMock(side_effect=RuntimeError("x")))
    assert main._fcn_command_body("咪寶 /FCN", _message("咪寶 /FCN")) is None


def test_non_fcn_text_is_not_detected():
    for text in ("今天FCN好像很多人買", "/fcntl", "fcn", "/觀點", "我覺得 /FCN 很難懂"):
        assert main._fcn_command_body(text, _message(text)) is None, text


def test_inbound_fcn_message_is_not_indexed_for_recall():
    calls = []

    def _capture(*args, **kwargs):
        calls.append((args, kwargs))

    with patch("main.memory.log_raw_message", side_effect=_capture):
        _routed("/FCN NVDA 年利率12%")
    inbound = [kw for args, kw in calls if len(args) >= 4 and args[1] == "MSG_FCN"]
    assert inbound and inbound[0]["index_for_recall"] is False


def test_market_quote_guard_covers_fcn_text():
    assert main._is_market_quote_outbound("FCN 評估：暫時拿不到股價資料，請稍後再試")
    assert main._is_market_quote_outbound(fcn_eval.USAGE_TEXT)


def test_help_lists_the_fcn_steps():
    help_text = main._HELP_TEXT
    assert "【投資】" in help_text
    assert "/FCN 股票… 年利率X%" in help_text
    assert "🧾 FCN評估" in help_text
    assert "引用理專" in help_text
    assert "@" not in help_text


def test_flex_card_has_exactly_one_call_site():
    source = Path(main.__file__).read_text(encoding="utf-8")
    assert len(re.findall(r"\bflex_card=(?!None)", source)) == 1


# ── _reply(flex_card=…) 的不變式 ───────────────────────────────────────────
def _card_reply() -> tuple[dict, str]:
    reply = fcn_eval.handle("NVDA AMD 年利率12%")
    assert reply.flex is not None
    return reply.flex, reply.alt_text


def _reply_with_card(api, card, alt, **kwargs):
    with (
        patch("main.MessagingApi", return_value=api),
        patch("main.ApiClient"),
        patch.object(main, "_get_line_config", return_value=MagicMock()),
    ):
        return main._reply("TOKEN_FCN", alt, group_id="G_TEST", flex_card=card, **kwargs)


def test_reply_sends_only_the_card_and_archives_alt_text_without_recall():
    card, alt = _card_reply()
    api = _fake_line_api()
    with (
        patch.object(main.settings, "bot_muted", False),
        patch.object(main.memory, "log_raw_message") as archive,
        patch("reminder_push.due_reminders_for_reply") as due,
    ):
        ok = _reply_with_card(api, card, alt, include_auxiliary=True, index_for_recall=False)
    assert ok is True
    sent = api.reply_message.call_args[0][0].messages
    assert len(sent) == 1 and isinstance(sent[0], FlexMessage)
    assert sent[0].alt_text == alt
    due.assert_not_called()
    archive.assert_any_call("G_TEST", "SENT_FCN", "__bot__", alt, index_for_recall=False)


def test_card_never_push_falls_back():
    card, alt = _card_reply()
    api = _fake_line_api(reply_side_effect=Exception("Invalid reply token"))
    with patch.object(main.settings, "bot_muted", False):
        ok = _reply_with_card(api, card, alt, allow_push_fallback=True)
    assert ok is False
    api.push_message.assert_not_called()


def test_card_rejected_by_validator_is_not_sent_and_closes_the_event():
    card, alt = _card_reply()
    api = _fake_line_api()
    rejected = SimpleNamespace(ok=False, reason="test", text="（安全替代文字）")
    with (
        patch.object(main.settings, "bot_muted", False),
        patch.object(main.output_validator, "validate_outbound_text", return_value=rejected),
        patch.object(main, "_mark_inbound_reply_completed_no_reply") as complete,
    ):
        ok = _reply_with_card(api, card, alt)
    assert ok is False
    api.reply_message.assert_not_called()
    complete.assert_called_once_with("TOKEN_FCN")


@pytest.mark.parametrize(
    "bad_action",
    [
        {"type": "uri", "label": "看", "uri": "https://example.com"},
        {"type": "postback", "label": "看", "data": "x=1"},
    ],
)
def test_card_with_non_message_action_is_not_sent(bad_action):
    card, alt = _card_reply()
    card["footer"]["contents"].append({"type": "button", "action": bad_action})
    api = _fake_line_api()
    with (
        patch.object(main.settings, "bot_muted", False),
        patch.object(main, "_mark_inbound_reply_completed_no_reply") as complete,
    ):
        ok = _reply_with_card(api, card, alt)
    assert ok is False
    api.reply_message.assert_not_called()
    complete.assert_called_once_with("TOKEN_FCN")


def test_card_cannot_be_combined_with_menu_cards():
    card, alt = _card_reply()
    api = _fake_line_api()
    with patch.object(main.settings, "bot_muted", False):
        ok = _reply_with_card(api, card, alt, menu_card=True)
    assert ok is False
    api.reply_message.assert_not_called()


def test_card_is_not_sent_when_bot_is_muted():
    card, alt = _card_reply()
    api = _fake_line_api()
    with patch.object(main.settings, "bot_muted", True):
        ok = _reply_with_card(api, card, alt)
    assert ok is False
    api.reply_message.assert_not_called()


def test_evaluation_stays_within_the_reply_budget():
    start = time.monotonic()
    reply, _cancel, _add = _routed("/FCN NVDA AMD TSLA AAPL 年利率15% 24個月")
    assert "flex_card" in reply.call_args.kwargs
    assert time.monotonic() - start < 10


def test_fcn_reply_never_mentions_anyone():
    """訊息裡真的 @ 了家人，FCN 回覆也不能把那個人再 @ 一次。"""

    def _register(token, _message):
        with main._reply_mention_targets_lock:
            main._reply_mention_targets_by_token[str(token)] = ["FAKE_TARGET"]

    with patch("main._register_reply_mention_targets", side_effect=_register):
        reply, _cancel, _add = _routed("/FCN @爸爸")
    reply.assert_called_once()
    with main._reply_mention_targets_lock:
        assert "TOKEN_FCN" not in main._reply_mention_targets_by_token


def test_missing_usd_rate_is_not_replaced_by_the_twd_rate(monkeypatch):
    monkeypatch.setattr(fcn_eval, "_fetch_usd_rf", lambda: None)
    reply, _cancel, _add = _routed("/FCN NVDA 年利率12% 台幣")
    assert reply.call_args.args[1] == fcn_eval.TEXT_NO_USD_RF
