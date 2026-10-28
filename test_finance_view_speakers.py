"""財經觀點：2026-10-24 停用，抽取器的發言人邏輯保留。

10/07 Andrew：「自己看多」誰知道自己是誰 → 每則訊息的發話者跟著 burst 一起交給抽取器，
存的人名由程式決定，不收「自己／我／家人」。

10/24 財經觀點拿掉（Andrew：「財經觀點沒更新⇒看你要拿掉，還是要更新」）：
  - /觀點 只回一句固定句，不再讀 finance_views；
  - burst 的三個出口（研究、快取命中、一般回覆）不再啟動抽取；
  - finance_view_*.py、finance_views 表和週日驗證器保留，所以抽取器的發言人邏輯照舊直接測
    （現在沒有任何程式路徑會呼叫它；要恢復時這些測試就是驗收）。
All content is made up.
"""
from __future__ import annotations

import json
import threading
from contextlib import nullcontext
from unittest.mock import MagicMock

import pytest

import finance_view_db
import finance_view_extractor as fve
import main
import memory

G = "G_FIN_SPEAKERS"
NAMES = {"U_MOM": "成員甲", "U_DAD": "成員乙"}


@pytest.fixture
def run_inline(monkeypatch):
    """Run the extractor thread inline and stub Gemini with ``views``."""
    prompts: list[str] = []

    class InlineThread:
        def __init__(self, target, **_kw):
            self.target = target

        def start(self):
            self.target()

    def stub(views):
        def generate(model, contents, config):
            prompts.append(contents)
            return MagicMock(text=json.dumps(views, ensure_ascii=False))

        client = MagicMock()
        client.models.generate_content.side_effect = generate
        monkeypatch.setattr(fve.gemini_client, "_client", client)

    monkeypatch.setattr(fve.threading, "Thread", InlineThread)
    return stub, prompts


def _view(**overrides):
    view = {"symbol_type": "ticker", "ticker": "0050.TW", "direction": "bull", "raw_quote": ""}
    view.update(overrides)
    return view


def _stored():
    return [(v["display_name"], v["user_id"], v["source_msg_id"], v["ticker"]) for v in finance_view_db.list_recent(G)]


def _extract(combined_text: str, message_ids: list[str]) -> None:
    """What the (retired) burst hook used to hand the extractor: each message with its sender."""
    speakers = []
    for message_id in message_ids:
        raw = memory.get_raw_message(G, str(message_id))
        if raw and raw[1]:
            speakers.append((str(message_id), raw[0] or "", raw[1]))
    fve.maybe_extract_and_save_async(
        G,
        combined_text,
        speakers=speakers,
        resolve_speaker=lambda user_id: NAMES.get(user_id or "", ""),
    )


# ── 抽取器本身（保留，直接測）────────────────────────────────────────────────


def test_each_view_is_stored_under_whoever_said_it(run_inline):
    stub, prompts = run_inline
    stub([
        _view(ticker="0050.TW", speaker_hint="自己", msg_index=1, raw_quote="我覺得 0050 會漲到 180"),
        _view(ticker="2330.TW", direction="bear", speaker_hint=None, raw_quote="我看空台積電"),
    ])
    memory.log_raw_message(G, "m1", "U_MOM", "我覺得 0050 會漲到 180")
    memory.log_raw_message(G, "m2", "U_DAD", "我看空台積電")

    _extract("我覺得 0050 會漲到 180\n我看空台積電", ["m1", "m2"])

    assert "［1］成員甲：我覺得 0050 會漲到 180" in prompts[0]
    assert "［2］成員乙：我看空台積電" in prompts[0]
    assert sorted(_stored()) == [
        ("成員乙", "U_DAD", "m2", "2330.TW"),
        ("成員甲", "U_MOM", "m1", "0050.TW"),
    ]


def test_a_single_speaker_burst_needs_no_hint(run_inline):
    stub, _prompts = run_inline
    stub([_view(ticker="NVDA", speaker_hint=None, raw_quote="輝達會再漲")])
    memory.log_raw_message(G, "m1", "U_DAD", "我覺得 NVDA 會再漲")
    memory.log_raw_message(G, "m2", "U_DAD", "目標 200")

    _extract("我覺得 NVDA 會再漲\n目標 200", ["m1", "m2"])

    assert [row[:2] for row in _stored()] == [("成員乙", "U_DAD")]


def test_reported_speech_keeps_the_person_named_in_the_message(run_inline):
    stub, _prompts = run_inline
    stub([_view(speaker_hint="成員甲", msg_index=1, raw_quote="成員甲說 0050 會漲")])
    memory.log_raw_message(G, "m1", "U_DAD", "成員甲說 0050 會漲")

    _extract("成員甲說 0050 會漲", ["m1"])

    assert [row[:2] for row in _stored()] == [("成員甲", "")]


def test_a_reported_role_is_filed_under_the_family_name(run_inline, monkeypatch):
    import line_mentions

    monkeypatch.setattr(line_mentions, "configured_family_alias_mapping", lambda **_kw: {"媽媽": "成員甲"})
    stub, _prompts = run_inline
    stub([_view(speaker_hint="媽媽", msg_index=1, raw_quote="媽媽說 0050 會漲")])
    memory.log_raw_message(G, "m1", "U_DAD", "媽媽說 0050 會漲")

    _extract("媽媽說 0050 會漲", ["m1"])

    assert [row[0] for row in _stored()] == ["成員甲"]


def test_a_hint_the_message_does_not_support_is_not_trusted(run_inline):
    stub, _prompts = run_inline
    stub([_view(speaker_hint="成員甲", msg_index=1, raw_quote="0050 會漲")])
    memory.log_raw_message(G, "m1", "U_DAD", "我覺得 0050 會漲")

    _extract("我覺得 0050 會漲", ["m1"])

    assert [row[:2] for row in _stored()] == [("成員乙", "U_DAD")]


def test_without_senders_a_pronoun_is_never_stored(run_inline):
    stub, _prompts = run_inline
    stub([_view(speaker_hint="自己")])

    fve.maybe_extract_and_save_async(G, "我覺得 0050 會漲到 180")

    assert [row[0] for row in _stored()] == [""]  # 2026-10-09: never stored as 「家人」


def test_the_prompt_no_longer_offers_self_as_a_speaker():
    assert "自己）" not in fve._PROMPT
    assert "msg_index" in fve._PROMPT


# ── /觀點：固定句，不再讀 finance_views（2026-10-24）──────────────────────────


def _legacy_view(display_name: str, raw_text: str) -> str:
    return finance_view_db.insert_view(
        group_id=G, source_msg_id=None, user_id="", display_name=display_name, raw_text=raw_text,
        symbol_type="ticker", ticker="0056.TW", macro_topic=None, direction="bull",
        time_frame=None, horizon_days=None, target_price=None, target_pct=None,
        confidence=None, condition_text=None, expires_at=None,
    )


@pytest.mark.parametrize("text", ["/觀點", "/觀點 媽媽", "/觀點 0050", "/觀點 媽"])
def test_the_command_answers_the_fixed_line_even_when_views_are_stored(text):
    _legacy_view("成員甲", "我覺得 0056 會漲")

    reply = main._handle_finance_view_command(G, text)

    assert reply == main._FINANCE_VIEW_RETIRED_REPLY
    assert reply == "財經觀點已經停用了。看行情可以點「🧭 市場盤勢」，看台股推薦可以點「📈 股票推薦」。"
    assert "0056" not in reply and "成員甲" not in reply  # nothing is read from the table


def test_the_command_does_not_read_finance_views_or_resolve_tickers(monkeypatch):
    import stock_quote

    def boom(*_a, **_k):
        raise AssertionError("the retired command must not touch finance_views or stock_quote")

    for name in ("list_recent", "list_by_ticker", "list_by_person", "set_speaker"):
        monkeypatch.setattr(finance_view_db, name, boom)
    monkeypatch.setattr(stock_quote, "detect_symbols", boom)

    assert main._handle_finance_view_command(G, "/觀點 2330") == main._FINANCE_VIEW_RETIRED_REPLY
    assert main._handle_finance_view_command(G, "/觀點") == main._FINANCE_VIEW_RETIRED_REPLY


def test_the_dead_display_helpers_are_gone():
    # Nothing calls them any more; their only job was formatting /觀點 output.
    for name in ("_format_finance_views", "_finance_view_speaker", "_finance_speaker_name", "UNKNOWN_FINANCE_SPEAKER"):
        assert not hasattr(main, name), name


# ── burst 不再啟動抽取（三個出口）────────────────────────────────────────────

_SHARED = "synthetic burst text"


@pytest.fixture
def no_extraction(monkeypatch):
    """Any attempt to start the extraction (or even to ask for the side-task slot) fails the test."""
    started: list[str] = []

    def fail(*_a, **_k):
        started.append("maybe_extract_and_save_async")
        raise AssertionError("finance view extraction must not start")

    monkeypatch.setattr(fve, "maybe_extract_and_save_async", fail)
    real_gate = main._gemini_side_task_allowed

    def watch_gate(name, *a, **k):
        if name == "finance_view_extract":
            started.append("finance_view_extract gate")
            raise AssertionError("the finance_view_extract side-task gate must not be asked")
        return real_gate(name, *a, **k)

    monkeypatch.setattr(main, "_gemini_side_task_allowed", watch_gate)
    return started


def _flush(monkeypatch, *, cached=None, research=False, reply="想確認細節，可以等官方公布的行程表再安排。"):
    """Run _handle_burst_flush with the models and the database stubbed; return what was sent."""
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    monkeypatch.setattr("claude_client.chat", lambda *a, **k: reply)
    monkeypatch.setattr(main.memory, "get_context", lambda *_a: [])
    monkeypatch.setattr(main.memory, "check_fact_cache", lambda *_a: cached)
    monkeypatch.setattr(main.memory, "top_facts", lambda *a, **k: [])
    monkeypatch.setattr(main.memory, "store_fact_cache", lambda *a, **k: None)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_a: [])
    monkeypatch.setattr(main, "_requires_public_research", lambda *_a: research)
    monkeypatch.setattr(main, "_prefetch_urls", lambda t: t)
    monkeypatch.setattr(main, "_thinking_indicator", lambda *_: nullcontext())
    monkeypatch.setattr(main, "_register_inbound_reply_batch", lambda *a, **k: None)
    monkeypatch.setattr(main, "_maybe_extract_facts", lambda *a, **k: None)
    monkeypatch.setattr(main, "_maybe_capture_calendar_event", lambda *a, **k: None)
    sent, researched = [], []
    monkeypatch.setattr(main, "_reply", lambda _tok, text, **_kw: sent.append(text))
    monkeypatch.setattr(main, "_handle_web_research_question", lambda *a, **k: researched.append(True))
    monkeypatch.setattr(main, "_finish_burst_without_reply", lambda *a, **k: None)
    worker = threading.Thread(target=main._handle_burst_flush, args=("G_B", _SHARED, "T_B", ["M_B"]))
    worker.start()
    worker.join(20)
    assert not worker.is_alive()
    return sent, researched


def test_a_plain_burst_reply_no_longer_starts_the_extraction(monkeypatch, no_extraction):
    sent, researched = _flush(monkeypatch)
    assert sent == ["想確認細節，可以等官方公布的行程表再安排。"] and researched == []
    assert no_extraction == []


def test_a_cached_burst_reply_no_longer_starts_the_extraction(monkeypatch, no_extraction):
    cached = "合成快取回覆：這類活動通常要事先報名。"
    sent, researched = _flush(monkeypatch, cached=cached)
    assert sent == [cached] and researched == []
    assert no_extraction == []


def test_a_research_burst_no_longer_starts_the_extraction(monkeypatch, no_extraction):
    sent, researched = _flush(monkeypatch, research=True)
    assert sent == [] and researched == [True]
    assert no_extraction == []


def test_the_retired_hook_starts_nothing_when_called(no_extraction, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger=main.logger.name):
        assert main._start_burst_finance_extraction(G, "我覺得 0050 會漲到 180", ["m1"]) is None
    assert no_extraction == []
    assert any("retired" in record.getMessage() for record in caplog.records)
