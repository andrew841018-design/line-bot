"""Routing and metadata tests for Mibao's canonical visual identity."""

from __future__ import annotations

from types import SimpleNamespace

import main
import mibao_identity
import pytest
import vision_common


def _plain_message():
    return SimpleNamespace(mention=None, text="")


def _mentioned_message(text: str):
    mentionee = SimpleNamespace(is_self=True, index=0, length=3)
    mention = SimpleNamespace(mentionees=[mentionee])
    return SimpleNamespace(mention=mention, text=text)


def test_identity_metadata_contract():
    assert mibao_identity.NAME == "咪寶"
    assert mibao_identity.ALIASES == ("咪寶", "Mibao")
    assert mibao_identity.REFERENCE_IMAGE_PATH.name == "mibao_reference.jpg"
    assert len(mibao_identity.REFERENCE_IMAGE_SHA256) == 64
    assert mibao_identity.GENERATION_FIDELITY == "text_prompt_approximation"


def test_deployed_private_reference_integrity():
    if not mibao_identity.REFERENCE_IMAGE_PATH.exists():
        pytest.skip("private reference image is deployed outside Git")
    valid, reason = mibao_identity.verify_reference_image()
    assert valid, reason


def test_all_runtime_persona_prompts_use_canonical_identity():
    assert mibao_identity.CHAT_IDENTITY_ZH in main.gemini_client._CORE_PROMPT
    assert mibao_identity.VISION_IDENTITY_ZH in vision_common._VISION_SYSTEM_PROMPT
    assert "小女生" not in main.gemini_client._CORE_PROMPT
    assert "小女生" not in vision_common._VISION_SYSTEM_PROMPT
    assert "小女生" not in main.gemini_client._PERSONA_REVIEW_PROMPT


def test_image_subject_mibao_is_not_removed_as_bot_vocative():
    text = "畫一張咪寶坐在毯子上"
    assert main._extract_gemini_trigger(text, _plain_message()) == text
    assert main._detect_image_gen_request(text) == "咪寶坐在毯子上"


@pytest.mark.parametrize("text,message", [
    ("/ai 畫一張：咪寶", _plain_message()),
    ("生成圖片：咪寶", _plain_message()),
])
def test_exact_mibao_subject_after_colon_is_not_removed(text, message):
    clean = main._extract_gemini_trigger(text, message)
    subject = main._detect_image_gen_request(clean)
    assert subject == "咪寶"
    assert mibao_identity.is_exact_mibao_alias(subject)


def test_leading_bot_vocative_is_removed_for_unrelated_subject():
    text = "咪寶，畫一張普通虎斑貓"
    assert main._extract_gemini_trigger(text, _plain_message()) == "畫一張普通虎斑貓"


def test_leading_bot_vocative_preserves_second_mibao_as_subject():
    text = "咪寶，畫一張咪寶坐在窗邊"
    clean = main._extract_gemini_trigger(text, _plain_message())
    assert clean == "畫一張咪寶坐在窗邊"
    assert main._detect_image_gen_request(clean) == "咪寶坐在窗邊"


def test_addressed_self_portrait_resolves_to_mibao_subject():
    text = "咪寶，畫一張你自己"
    clean = main._extract_gemini_trigger(text, _plain_message())
    assert clean == "畫一張咪寶"
    assert main._detect_image_gen_request(clean) == "咪寶"


def test_addressed_self_portrait_with_pose_resolves_to_mibao_subject():
    text = "咪寶，畫一張你自己坐在窗邊"
    clean = main._extract_gemini_trigger(text, _plain_message())
    assert clean == "畫一張咪寶坐在窗邊"
    assert main._detect_image_gen_request(clean) == "咪寶坐在窗邊"


def test_addressed_self_portrait_accepts_unlisted_action():
    text = "咪寶，畫一張你自己跳舞"
    clean = main._extract_gemini_trigger(text, _plain_message())
    assert clean == "畫一張咪寶跳舞"
    assert main._detect_image_gen_request(clean) == "咪寶跳舞"


@pytest.mark.parametrize("text", [
    "咪寶，畫一張你自己的房間",
    "/ai 畫一張你自己的狗",
])
def test_addressed_possessive_subject_is_not_rewritten_as_mibao(text):
    clean = main._extract_gemini_trigger(text, _plain_message())
    subject = main._detect_image_gen_request(clean)
    assert subject in {"你自己的房間", "你自己的狗"}
    assert not mibao_identity.is_mibao_subject(subject)


@pytest.mark.parametrize("text,expected", [
    ("咪寶，畫一張你自己的模樣坐在窗邊", "咪寶坐在窗邊"),
    ("/ai 畫一張你自己的樣子跳舞", "咪寶跳舞"),
])
def test_addressed_own_appearance_with_suffix_resolves_to_mibao(text, expected):
    clean = main._extract_gemini_trigger(text, _plain_message())
    subject = main._detect_image_gen_request(clean)
    assert subject == expected
    assert mibao_identity.is_mibao_subject(subject)


@pytest.mark.parametrize("text,message", [
    ("@咪寶，畫一張你自己坐在窗邊", _mentioned_message("@咪寶，畫一張你自己坐在窗邊")),
    ("@咪寶，畫一張你自己坐在窗邊", _plain_message()),
    ("/ai 畫一張你自己坐在窗邊", _plain_message()),
])
def test_all_explicit_trigger_routes_normalize_addressed_self(text, message):
    clean = main._extract_gemini_trigger(text, message)
    assert clean == "畫一張咪寶坐在窗邊"
    assert main._detect_image_gen_request(clean) == "咪寶坐在窗邊"


def test_trailing_bot_vocative_is_not_the_image_subject():
    text = "畫一張普通虎斑貓給我看，咪寶"
    clean = main._extract_gemini_trigger(text, _plain_message())
    assert clean == "畫一張普通虎斑貓給我看"
    subject = main._detect_image_gen_request(clean)
    assert subject == "普通虎斑貓給我看"
    assert not mibao_identity.is_mibao_subject(subject)


def test_english_alias_routes_to_image_generation():
    text = "draw Mibao by a window"
    clean = main._extract_gemini_trigger(text, _plain_message())
    assert clean == text
    assert main._detect_image_gen_request(clean) == "Mibao by a window"


@pytest.mark.parametrize("text", [
    "畫一張台北夜景給咪寶看",
    "畫一張台北夜景給咪寶看看",
    "畫一張台北夜景讓咪寶看",
    "畫一張生日卡片給咪寶",
    "畫一張生日卡片送給咪寶",
])
def test_recipient_name_does_not_contaminate_image_subject(text):
    clean = main._extract_gemini_trigger(text, _plain_message())
    subject = main._detect_image_gen_request(clean)
    assert subject in {"台北夜景", "生日卡片"}
    assert not mibao_identity.is_mibao_subject(subject)


@pytest.mark.parametrize("text,message", [
    ("/ai 畫一張生日卡片給咪寶", _plain_message()),
    ("@咪寶，畫一張生日卡片給咪寶", _plain_message()),
    (
        "@咪寶，畫一張生日卡片給咪寶",
        _mentioned_message("@咪寶，畫一張生日卡片給咪寶"),
    ),
])
def test_explicit_routes_remove_recipient_name(text, message):
    clean = main._extract_gemini_trigger(text, message)
    assert clean == "畫一張生日卡片"
    subject = main._detect_image_gen_request(clean)
    assert subject == "生日卡片"
    assert not mibao_identity.is_mibao_subject(subject)
