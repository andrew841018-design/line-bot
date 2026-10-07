from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gemini_client  # noqa: E402
import gemini_core  # noqa: E402
import output_validator  # noqa: E402
import pytest  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from reply_policy import NO_REPEAT_CONTRACT  # noqa: E402


@pytest.mark.parametrize("text", [
    "請將手機設成靜默模式。",
    "保持沉默是這篇小說主角的選擇。",
    "思考實驗可以用來檢驗假設。",
    "推理小說適合喜歡解謎的讀者。",
    "我需要判斷電阻大小，才能選對保險絲。",
    "請把回覆結構改成兩段。",
    "Analysis tools help inspect logs.",
])
def test_ordinary_topic_words_are_not_internal_narration(text):
    assert output_validator.validate_outbound_text(text).text == text
    assert not gemini_client._looks_like_internal_trace(text)


@pytest.mark.parametrize("text", [
    "我決定不回覆這則訊息。", "這只是閒聊，不用回覆。", "決定不回覆。",
])
def test_actual_no_reply_narration_is_suppressed(text):
    assert output_validator.validate_outbound_text(text).text == ""


def test_explicit_source_request_gets_available_grounding_links():
    text = gemini_client._quality_gate(
        None, "這個方法的問題是樣本太小。",
        [("https://example.com/study", "Example study")],
        "請附上來源", None,
    )
    assert "https://example.com/study" in text


def test_empty_answer_does_not_turn_into_source_list():
    assert gemini_client._quality_gate(
        None, "", [("https://example.com/study", "Example")], "請附來源", None
    ) == ""


@pytest.mark.parametrize("user_text", [
    "不用附來源", "這個來源可信嗎？", "能源來源有哪些？",
    "No sources please.", "Do not include sources.",
    "What does citations mean?", "Explain how renewable energy sources work.",
    "Explain Python source references.",
])
def test_incidental_or_negated_sources_do_not_opt_in(user_text):
    assert gemini_core._append_sources(
        "先核對原始內容。", [("https://example.com", "Example")], user_text=user_text
    ) == "先核對原始內容。"


def test_source_request_is_independent_of_negated_format_request():
    assert "https://example.com" in gemini_core._append_sources(
        "先核對原始內容。", [("https://example.com", "Example")],
        user_text="請不要列正反方，但附上來源",
    )


def test_retry_preserves_source_request_without_forcing_sections(monkeypatch):
    calls = []
    response = SimpleNamespace(text="這項研究的樣本太小，不能直接推廣。")
    def send(prompt):
        calls.append(prompt)
        return response
    monkeypatch.setattr(gemini_client, "_track_usage", lambda *_: None)
    monkeypatch.setattr(gemini_client, "_extract_grounding_urls", lambda *_: [])
    result = gemini_client._quality_gate(
        SimpleNamespace(send_message=send), "思緒：我需要判斷使用者。",
        [("https://example.com/study", "Study")], "請列出來源", None,
    )
    assert len(calls) == 1
    assert "只有使用者要求才列正反方或來源" in calls[0]
    assert "https://example.com/study" in result


def test_latest_output_contract_requires_concise_user_visible_text():
    prompt = gemini_client._build_system_instruction(
        facts=[], user_input="這篇投資文章怎麼看？"
    )

    assert "只輸出要給 LINE 使用者看的正式繁體中文" in prompt
    assert "不強制正方／反方／同意／反對段落" in prompt
    assert "保持沉默" in prompt
    assert NO_REPEAT_CONTRACT in prompt


def test_url_cannot_fall_back_to_unsolicited_summary(monkeypatch):
    import lite_reply

    monkeypatch.setattr(lite_reply, "_STAGE1_HANDLERS", ())
    monkeypatch.setattr(lite_reply, "_try_local_llm", lambda *_a, **_kw: None)
    monkeypatch.setattr(lite_reply, "_summarize_url", lambda *_a: pytest.fail("unsolicited summary"))
    monkeypatch.setattr(lite_reply, "_weather_taiwan", lambda *_a: None)
    monkeypatch.setattr(lite_reply, "_google_search_snippet", lambda *_a: None)
    assert lite_reply.lite_reply("https://example.com/article") is None


def test_local_and_media_prompt_composers_receive_shared_policy(monkeypatch):
    import main
    import lite_reply
    import vision_common
    import local_llm
    from image_reply import IMAGE_RESPONSE_CONTRACT
    from video_reply import VIDEO_COMMENTARY_CONTRACT

    captured = []
    monkeypatch.setattr(local_llm, "chat", lambda *_a, **kw: captured.append(kw) or "這裡有可核實的新資訊。")
    assert main._local_text_llm_fallback("這個方法有什麼限制？")
    assert NO_REPEAT_CONTRACT in captured[0]["system_prompt"]
    for prompt in (IMAGE_RESPONSE_CONTRACT, VIDEO_COMMENTARY_CONTRACT,
                   lite_reply._LOCAL_LLM_OPINION_SYSTEM_PROMPT,
                   vision_common.compose_prompt(""),
                   vision_common.compose_prompt(IMAGE_RESPONSE_CONTRACT)):
        assert NO_REPEAT_CONTRACT in prompt


@pytest.mark.parametrize("pipeline_name", ["_v4_news_style_pipeline", "_wrap_with_gemini_news_style"])
def test_image_research_wrappers_transport_policy_without_live_calls(monkeypatch, pipeline_name):
    import media_pipeline

    captured = []
    monkeypatch.setitem(sys.modules, "local_llm", SimpleNamespace(
        chat=lambda *_a, **kw: captured.append(kw) or "有限樣本不能推廣到所有情況。"))
    monkeypatch.setitem(sys.modules, "finetune_query_expansion", SimpleNamespace(expand_queries=lambda *_a, **_k: []))
    monkeypatch.setitem(sys.modules, "source_aggregator", SimpleNamespace(aggregate_sources=lambda *_a, **_k: []))
    monkeypatch.setitem(sys.modules, "fulltext_fetcher", SimpleNamespace(fetch_top_sources=lambda *_a, **_k: []))
    monkeypatch.setitem(sys.modules, "grounding_local", SimpleNamespace(score_response=lambda *_a: {}))
    monkeypatch.setitem(sys.modules, "self_critique", SimpleNamespace(
        critique_reply=lambda *_a: {}, refine_reply=lambda *_a, **_k: pytest.fail("unexpected refinement")))
    monkeypatch.setitem(sys.modules, "web_scraper", SimpleNamespace(
        search_duckduckgo=lambda *_a, **_k: [], search_google_news=lambda *_a, **_k: [],
        search_wiki_full=lambda *_a: None))

    assert getattr(media_pipeline, pipeline_name)("合成圖片素材", user_prompt="有哪些限制？")
    assert len(captured) == 1
    assert NO_REPEAT_CONTRACT in captured[0]["system_prompt"]


def test_final_refinement_transports_policy_to_both_providers(monkeypatch):
    import self_critique

    captured = []
    monkeypatch.setattr(self_critique, "_call_gemini", lambda prompt, **_k: captured.append(prompt) or None)
    monkeypatch.setattr(self_critique, "_call_local_14b", lambda prompt, **_k: captured.append(prompt) or "有限樣本不能推廣到所有情況。")
    assert self_critique.refine_reply("合成初稿", {}, [], user_prompt="有哪些限制？") == "有限樣本不能推廣到所有情況。"
    assert len(captured) == 2
    assert all(NO_REPEAT_CONTRACT in prompt and "有哪些限制？" in prompt for prompt in captured)


def test_quality_gate_no_longer_requires_pro_con_sections_or_source_count():
    reply = "這個方法的主要問題是成本太高，先用小額試行比較安全。"

    assert gemini_client._violates_quality(reply, "這個投資方法值得嗎？") == (False, "")


def test_grounding_urls_are_not_appended_as_an_unsolicited_source_list():
    assert gemini_core._append_sources(
        "先看風險再決定。", [("https://example.com", "Example")]
    ) == "先看風險再決定。"


def test_internal_silence_and_reasoning_text_is_blocked_before_line_delivery():
    for text in (
        "思緒：\n1. 判斷這是家常閒聊。\n綜合判斷：保持沉默。",
        "內部判斷：這則訊息不需要回覆。",
        "判斷結果：不產生實際回覆。",
    ):
        result = output_validator.validate_outbound_text(text)
        assert not result.ok
        assert result.reason == "internal_trace_leak"
        assert result.text == ""
