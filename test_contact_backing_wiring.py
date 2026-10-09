"""地址／電話查證關卡的「依據」接線（2026-10-10 review）。

審查員把 main._contact_backing 改成不看 source_text／request_text／material_text／
evidence_text，原有測試仍全過：@咪寶、burst、研究這幾條路徑的依據沒有被守住。
這裡每一種依據各有一個案例：回覆重述同一個門牌或電話，只有那一種依據寫了它。
拿掉任何一種依據，對應的案例就會失敗。
"""
from __future__ import annotations

import pytest

import main
import reply_policy
import reply_provenance

_ADDRESS_REPLY = "青島東路3-2號附近停車要先找路邊格，晚上六點後比較好停。"
_ADDRESS_GIVEN = "我們約在測試市中正區青島東路3之2號"
_PHONE_REPLY = "訂位打02-2345-6789，週一公休。"
_PHONE_GIVEN = "那家店電話是 02 2345 6789"
_QUESTION = "那裡好停車嗎？"


@pytest.fixture(autouse=True)
def _no_judge(monkeypatch):
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    reply_provenance.reset()


def _enforce(reply, **kwargs):
    kwargs.setdefault("source_text", _QUESTION)
    kwargs.setdefault("request_text", _QUESTION)
    outcome: dict = {}
    out = main._enforce_new_value_reply(reply, outcome=outcome, **kwargs)
    return out, outcome


def _kept(out, outcome, reply):
    return out == reply and not outcome.get("contact_details_dropped")


def test_nothing_backs_it_so_it_is_dropped():
    # The baseline the other cases differ from by one source each.
    out, outcome = _enforce(_ADDRESS_REPLY, addressed=False)
    assert out == "" and outcome.get("contact_details_dropped") is True


@pytest.mark.parametrize(
    "source",
    ["source_text", "request_text", "material_text", "evidence_text"],
)
def test_each_text_source_backs_an_address(source):
    kwargs = {source: f"{_ADDRESS_GIVEN}。{_QUESTION}"}
    if source == "material_text":
        kwargs["has_material"] = True
    out, outcome = _enforce(_ADDRESS_REPLY, addressed=False, **kwargs)
    assert _kept(out, outcome, _ADDRESS_REPLY), (source, out, outcome)


def test_an_earlier_family_turn_backs_an_address():
    context = [("user", main.memory.speaker_turn("成員甲", _ADDRESS_GIVEN)), ("bot", "好的。")]
    out, outcome = _enforce(_ADDRESS_REPLY, addressed=False, context=context)
    assert _kept(out, outcome, _ADDRESS_REPLY)


def test_a_bot_turn_does_not_back_an_address():
    # What the bot said before is not evidence for what it says now.
    context = [("bot", _ADDRESS_GIVEN)]
    out, outcome = _enforce(_ADDRESS_REPLY, addressed=False, context=context)
    assert out == "" and outcome.get("contact_details_dropped") is True


def test_a_remembered_fact_backs_an_address():
    out, outcome = _enforce(_ADDRESS_REPLY, addressed=False, facts=[f"成員甲：{_ADDRESS_GIVEN}"])
    assert _kept(out, outcome, _ADDRESS_REPLY)


@pytest.mark.parametrize("source", ["source_text", "request_text", "material_text", "evidence_text"])
def test_each_text_source_backs_a_phone(source):
    kwargs = {source: f"{_PHONE_GIVEN}。{_QUESTION}"}
    if source == "material_text":
        kwargs["has_material"] = True
    out, outcome = _enforce(_PHONE_REPLY, addressed=False, **kwargs)
    assert _kept(out, outcome, _PHONE_REPLY), (source, out, outcome)


def test_generated_replies_use_the_conversation_and_facts():
    context = [("user", main.memory.speaker_turn("成員甲", _ADDRESS_GIVEN))]
    assert main._guard_generated_reply(_ADDRESS_REPLY, _QUESTION, context=context) == _ADDRESS_REPLY
    reply_provenance.reset()
    facts = [f"成員甲：{_ADDRESS_GIVEN}"]
    assert main._guard_generated_reply(_ADDRESS_REPLY, _QUESTION, facts=facts) == _ADDRESS_REPLY
    reply_provenance.reset()
    assert not main._guard_generated_reply(_ADDRESS_REPLY, _QUESTION)


def test_the_local_fallback_keeps_the_conversation_as_backing(monkeypatch):
    # 2026-10-10 review: the local model's reply was checked without the
    # conversation, so an address the family gave earlier dropped it.
    import sys
    import types

    fake = types.ModuleType("local_llm")
    fake.chat = lambda *_a, **_k: _ADDRESS_REPLY
    monkeypatch.setitem(sys.modules, "local_llm", fake)
    context = [("user", main.memory.speaker_turn("成員甲", _ADDRESS_GIVEN))]
    assert main._local_text_llm_fallback(_QUESTION, context) == _ADDRESS_REPLY
    reply_provenance.reset()
    assert main._local_text_llm_fallback(_QUESTION, []) == ""


def test_the_guard_reads_the_same_keys_as_the_backing():
    # Sanity: the reply's address and the given one compare equal.
    assert reply_policy.unbacked_contact_details(_ADDRESS_REPLY, [_ADDRESS_GIVEN]) == []
    assert reply_policy.unbacked_contact_details(_PHONE_REPLY, [_PHONE_GIVEN]) == []
