"""Tests for the lite-mode helpfulness gate.

Bug: lite/local-LLM fallback emitted vague platitudes for substantive questions
(e.g. 購屋款 + 阿婆出資 → "應該明確確認並妥善處理"). Rule
(feedback_bot_reply_helpful_or_defer): substantive 題寧可不回也別回空泛 lecture；
gate 抑制空話 → fall through 到網搜 → 仍不行 return None → 上層存 pending。

Gate is suppression-biased: false positive (defer a borderline reply) is cheap;
false negative (leak a platitude) is the cardinal sin. Chitchat must NOT be nuked.
"""
from __future__ import annotations

import sys
import types

import lite_reply as lr

PLATITUDE = "關於購屋款的問題，如果有阿婆的部分出資，應該明確確認並妥善處理。"


def _force_skip_nlp(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "chinese_nlp",
        types.SimpleNamespace(classify_intent=lambda t: {"intent": "general"}),
    )


# ── _passes_helpfulness_gate (pure) ─────────────────────────────────────────


def test_gate_suppresses_substantive_platitude():
    # substantive (購屋/出資) + advice modal + no specifics + short → suppress
    assert lr._passes_helpfulness_gate(PLATITUDE, "買房子的錢阿婆有出一部分要怎麼弄") is False


def test_gate_passes_chitchat_even_with_advice_word():
    # non-substantive → pass; must NOT nuke warm chitchat (含「注意」也放行)
    assert lr._passes_helpfulness_gate("早點休息喔，明天還要上班呢", "晚安") is True
    assert lr._passes_helpfulness_gate("天氣冷要注意保暖喔", "今天好冷") is True


def test_gate_passes_substantive_with_specifics():
    # substantive but concrete (有數字) → genuinely helpful, must pass
    reply = "贈與稅每年免稅額 244 萬，阿婆出資超過要申報，可分年贈與分攤。"
    assert lr._passes_helpfulness_gate(reply, "阿婆出錢買房贈與稅怎麼算") is True


def test_gate_rejects_empty():
    assert lr._passes_helpfulness_gate("", "買房") is False
    assert lr._passes_helpfulness_gate("   ", "買房") is False


# ── lite_reply integration ──────────────────────────────────────────────────


def test_lite_reply_substantive_platitude_returns_none(monkeypatch):
    _force_skip_nlp(monkeypatch)
    monkeypatch.setattr(lr, "_STAGE1_HANDLERS", ())
    monkeypatch.setattr(
        lr, "_try_local_llm",
        lambda text, context=None: PLATITUDE,
    )
    monkeypatch.setattr(lr, "_try_google_snippet", lambda text: None)
    monkeypatch.setattr(lr, "_STAGE3_HANDLERS", (lr._try_google_snippet,))

    # platitude suppressed → snippet None → return None (→ caller defers to pending)
    assert lr.lite_reply("買房子的錢阿婆有出一部分要怎麼處理") is None


def test_lite_reply_chitchat_still_replies(monkeypatch):
    _force_skip_nlp(monkeypatch)
    monkeypatch.setattr(lr, "_STAGE1_HANDLERS", ())
    monkeypatch.setattr(
        lr, "_try_local_llm",
        lambda text, context=None: "早點睡喔，晚安～",
    )
    out = lr.lite_reply("晚安")
    assert out is not None and "早點睡" in out  # chitchat preserved


def test_lite_reply_opinion_share_requires_sections(monkeypatch):
    _force_skip_nlp(monkeypatch)
    monkeypatch.setattr(lr, "_STAGE1_HANDLERS", ())
    monkeypatch.setattr(lr, "_STAGE3_HANDLERS", ())
    shallow = (
        "這支影片分享了如何在家做手工皂，看起來很有趣也很實用，"
        "自己動手做可以避免商業皂的化學成分，對皮膚更友善。"
    )
    monkeypatch.setattr(lr, "_try_local_llm", lambda text, context=None: shallow)

    assert lr.lite_reply("這支影片分享了如何在家做手工皂，看起來很實用") is None


def test_lite_reply_opinion_share_allows_structured_reply(monkeypatch):
    _force_skip_nlp(monkeypatch)
    monkeypatch.setattr(lr, "_STAGE1_HANDLERS", ())
    structured = (
        "我覺得這支影片可以當入門參考，但不能把手工皂直接等於更安全。\n"
        "正方：自己做能控制香精、色料與油脂來源，也比較知道成分。\n"
        "反方：手工皂仍然有氫氧化鈉皂化與保存問題，配方錯反而刺激皮膚。\n"
        "整合：可以做興趣或禮物，但敏感肌要先小範圍測試。"
    )
    monkeypatch.setattr(lr, "_try_local_llm", lambda text, context=None: structured)

    out = lr.lite_reply("這支影片分享了如何在家做手工皂，看起來很實用")

    assert out is not None
    assert "正方" in out
    assert "反方" in out
    assert "整合" in out


def test_lite_reply_context_video_followup_requires_structured_reply(monkeypatch):
    _force_skip_nlp(monkeypatch)
    monkeypatch.setattr(lr, "_STAGE1_HANDLERS", ())
    monkeypatch.setattr(lr, "_STAGE3_HANDLERS", ())
    shallow = "這個故事很鼓舞，真的值得學習。"
    monkeypatch.setattr(
        lr, "_try_local_llm", lambda text, context=None: shallow
    )
    context = [
        (
            "user",
            "（以下是影片連結 https://www.youtube.com/watch?v=abc123 的內容，透過 yt-dlp 擷取）\n"
            "--- 影片資訊開始 ---\n"
            "標題：小知足 從貧窮家庭到財富自由的故事\n"
            "上傳者：小知足\n"
            "描述：小知足分享她從貧窮到成功的過程。\n"
            "--- 影片資訊結束 ---",
        ),
    ]
    assert (
        lr.lite_reply("很有感", context=context) is None
    )  # 浅層回覆應被 gate 擋下


def test_lite_reply_substantive_with_specifics_replies(monkeypatch):
    _force_skip_nlp(monkeypatch)
    monkeypatch.setattr(lr, "_STAGE1_HANDLERS", ())
    good = "贈與稅每年免稅額 244 萬，超過要申報，可分年贈與。"
    monkeypatch.setattr(lr, "_try_local_llm", lambda text, context=None: good)
    out = lr.lite_reply("阿婆出錢買房贈與稅怎麼算")
    assert out is not None and "244" in out  # genuinely helpful → sent


def test_lite_stock_technical_preempts_plain_quote(monkeypatch):
    def fail_plain_quote(*_args, **_kwargs):
        raise AssertionError("plain quote should not run for month-line question")

    monkeypatch.setattr(lr.stock_quote, "get_taiex_month_line_text", lambda text: "月線答案")
    monkeypatch.setattr(lr.stock_quote, "get_contextual_quotes_text", fail_plain_quote)

    out = lr._try_stock("咪寶今天台股大盤有跌破月線嗎")

    assert out is not None
    assert out.startswith("月線答案")
    assert "公開報價/日線" in out


def test_lite_reply_suppresses_snippet_platitude(monkeypatch):
    _force_skip_nlp(monkeypatch)
    monkeypatch.setattr(lr, "_STAGE1_HANDLERS", ())
    monkeypatch.setattr(lr, "_try_local_llm", lambda text, context=None: None)
    monkeypatch.setattr(lr, "_try_google_snippet", lambda text: PLATITUDE)
    monkeypatch.setattr(lr, "_STAGE3_HANDLERS", (lr._try_google_snippet,))

    # even the web snippet is a platitude for this substantive Q → suppress → None
    assert lr.lite_reply("買房阿婆出資怎麼處理") is None


# ── C1: explicit-path must defer (not silent-drop) when gate→None during quota outage ──


def test_explicit_path_replies_without_pending_when_lite_misses(monkeypatch):
    """The gate makes lite_reply return None for substantive platitudes; on the explicit
    path (@咪寶 <question>) with Gemini quota already exhausted, `_llm_chat` returns ""
    WITHOUT raising → the except block is skipped. The C1 guard must not restore
    pending reply, but it should send a small visible fallback so the bot does
    not look dead."""
    import main
    from unittest.mock import MagicMock, patch

    evt = MagicMock()
    evt.source = MagicMock()
    evt.source.user_id = "USR001"
    evt.message = MagicMock()
    evt.message.quoted_message_id = None
    evt.reply_token = "TOKEN_C1"

    with patch("main._llm_chat", return_value=""), \
         patch("main._quota_exhausted", return_value=True), \
         patch("main._detect_image_gen_request", return_value=None), \
         patch("main._build_quoted_block", return_value=None), \
         patch("main._prefetch_urls", side_effect=lambda x: x), \
         patch("main._get_persona_notes", return_value=""), \
         patch("main.memory.get_context", return_value=[]), \
         patch("main.memory.top_facts", return_value=[]), \
         patch("main._thinking_indicator"), \
         patch("main._maybe_capture_calendar_event"), \
         patch("main._save_pending_burst_text") as mock_pending, \
         patch("main._reply") as mock_reply:
        main._handle_explicit_text(evt, "GRP001", "買房子的錢阿婆有出一部分要怎麼處理")

    mock_pending.assert_not_called()
    mock_reply.assert_called_once_with(
        "TOKEN_C1",
        main._visible_llm_degraded_reply(),
        group_id="GRP001",
        allow_push_fallback=True,
    )
