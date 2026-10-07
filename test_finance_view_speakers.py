"""財經觀點記下的是說話那個人的名字（Andrew 2026-10-07）。

「自己看多」誰知道自己是誰：每則訊息的發話者跟著 burst 一起交給抽取器，
存的人名由程式決定，不收「自己／我／家人」。All content is made up.
"""
from __future__ import annotations

import json
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
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(main, "_get_member_display_name", lambda _g, uid: NAMES.get(uid, "群組成員"))
    return stub, prompts


def _view(**overrides):
    view = {"symbol_type": "ticker", "ticker": "0050.TW", "direction": "bull", "raw_quote": ""}
    view.update(overrides)
    return view


def _stored():
    return [(v["display_name"], v["user_id"], v["source_msg_id"], v["ticker"]) for v in finance_view_db.list_recent(G)]


def test_each_view_is_stored_under_whoever_said_it(run_inline):
    stub, prompts = run_inline
    stub([
        _view(ticker="0050.TW", speaker_hint="自己", msg_index=1, raw_quote="我覺得 0050 會漲到 180"),
        _view(ticker="2330.TW", direction="bear", speaker_hint=None, raw_quote="我看空台積電"),
    ])
    memory.log_raw_message(G, "m1", "U_MOM", "我覺得 0050 會漲到 180")
    memory.log_raw_message(G, "m2", "U_DAD", "我看空台積電")

    main._start_burst_finance_extraction(G, "我覺得 0050 會漲到 180\n我看空台積電", ["m1", "m2"])

    assert "［1］成員甲：我覺得 0050 會漲到 180" in prompts[0]
    assert "［2］成員乙：我看空台積電" in prompts[0]
    assert sorted(_stored()) == [
        ("成員乙", "U_DAD", "m2", "2330.TW"),
        ("成員甲", "U_MOM", "m1", "0050.TW"),
    ]
    reply = main._handle_finance_view_command(G, "/觀點")
    assert "成員甲 0050.TW 看多" in reply and "成員乙 2330.TW 看空" in reply
    assert "自己" not in reply


def test_a_single_speaker_burst_needs_no_hint(run_inline):
    stub, _prompts = run_inline
    stub([_view(ticker="NVDA", speaker_hint=None, raw_quote="輝達會再漲")])
    memory.log_raw_message(G, "m1", "U_DAD", "我覺得 NVDA 會再漲")
    memory.log_raw_message(G, "m2", "U_DAD", "目標 200")

    main._start_burst_finance_extraction(G, "我覺得 NVDA 會再漲\n目標 200", ["m1", "m2"])

    assert [row[:2] for row in _stored()] == [("成員乙", "U_DAD")]


def test_reported_speech_keeps_the_person_named_in_the_message(run_inline):
    stub, _prompts = run_inline
    stub([_view(speaker_hint="成員甲", msg_index=1, raw_quote="成員甲說 0050 會漲")])
    memory.log_raw_message(G, "m1", "U_DAD", "成員甲說 0050 會漲")

    main._start_burst_finance_extraction(G, "成員甲說 0050 會漲", ["m1"])

    assert [row[:2] for row in _stored()] == [("成員甲", "")]


def test_a_reported_role_is_filed_under_the_family_name(run_inline, monkeypatch):
    import line_mentions

    monkeypatch.setattr(line_mentions, "configured_family_alias_mapping", lambda **_kw: {"媽媽": "成員甲"})
    stub, _prompts = run_inline
    stub([_view(speaker_hint="媽媽", msg_index=1, raw_quote="媽媽說 0050 會漲")])
    memory.log_raw_message(G, "m1", "U_DAD", "媽媽說 0050 會漲")

    main._start_burst_finance_extraction(G, "媽媽說 0050 會漲", ["m1"])

    assert [row[0] for row in _stored()] == ["成員甲"]
    assert "成員甲 0050.TW 看多" in main._handle_finance_view_command(G, "/觀點 媽媽")


def test_a_hint_the_message_does_not_support_is_not_trusted(run_inline):
    stub, _prompts = run_inline
    stub([_view(speaker_hint="成員甲", msg_index=1, raw_quote="0050 會漲")])
    memory.log_raw_message(G, "m1", "U_DAD", "我覺得 0050 會漲")

    main._start_burst_finance_extraction(G, "我覺得 0050 會漲", ["m1"])

    assert [row[:2] for row in _stored()] == [("成員乙", "U_DAD")]


def test_without_senders_a_pronoun_is_never_stored(run_inline):
    stub, _prompts = run_inline
    stub([_view(speaker_hint="自己")])

    fve.maybe_extract_and_save_async(G, "我覺得 0050 會漲到 180")

    assert [row[0] for row in _stored()] == ["家人"]


def test_the_prompt_no_longer_offers_self_as_a_speaker():
    assert "自己）" not in fve._PROMPT
    assert "msg_index" in fve._PROMPT


def _legacy_view(display_name: str, raw_text: str) -> str:
    return finance_view_db.insert_view(
        group_id=G, source_msg_id=None, user_id="", display_name=display_name, raw_text=raw_text,
        symbol_type="ticker", ticker="0056.TW", macro_topic=None, direction="bull",
        time_frame=None, horizon_days=None, target_price=None, target_pct=None,
        confidence=None, condition_text=None, expires_at=None,
    )


def test_an_old_self_row_shows_and_keeps_the_real_name(monkeypatch):
    monkeypatch.setattr(main, "_get_member_display_name", lambda _g, uid: NAMES.get(uid, "群組成員"))
    memory.log_raw_message(G, "m1", "U_MOM", "我覺得 0056 會漲")
    memory.log_raw_message(G, "m2", "U_DAD", "晚餐吃什麼")
    _legacy_view("自己", "我覺得 0056 會漲")

    reply = main._handle_finance_view_command(G, "/觀點")

    assert "成員甲 0056.TW 看多" in reply
    assert [row[:2] for row in _stored()] == [("成員甲", "U_MOM")]  # saved, not re-guessed


def test_an_old_row_nobody_can_place_says_family(monkeypatch):
    monkeypatch.setattr(main, "_get_member_display_name", lambda _g, uid: NAMES.get(uid, "群組成員"))
    memory.log_raw_message(G, "m1", "U_MOM", "我覺得 0056 會漲")
    memory.log_raw_message(G, "m2", "U_DAD", "我也覺得")
    _legacy_view("自己", "大概會漲吧")

    reply = main._handle_finance_view_command(G, "/觀點")

    assert "家人 0056.TW 看多" in reply
    assert "自己" not in reply


def test_person_query_accepts_a_family_role(monkeypatch):
    import line_mentions

    monkeypatch.setattr(line_mentions, "configured_family_alias_mapping", lambda **_kw: {"媽": "成員甲"})
    finance_view_db.insert_view(
        group_id=G, source_msg_id="m1", user_id="U_MOM", display_name="成員甲", raw_text="0050 會漲",
        symbol_type="ticker", ticker="0050.TW", macro_topic=None, direction="bull",
        time_frame=None, horizon_days=None, target_price=None, target_pct=None,
        confidence=None, condition_text=None, expires_at=None,
    )

    reply = main._handle_finance_view_command(G, "/觀點 媽")

    assert reply.startswith("📈 成員甲 的財經觀點")
    assert "成員甲 0050.TW 看多" in reply
