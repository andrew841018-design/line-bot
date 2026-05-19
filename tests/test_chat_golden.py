"""Phase 2B.1 — Golden tests pinning chat() pre-refactor behavior.

These tests are the safety net for the gemini_core.py extraction +
rag_graph.py wrapping refactor (Phase 2B sub-phases 2-6). They mock
external deps (Gemini SDK, embedding_recall, stock_quote) and assert
the exact contracts of chat() under current implementation.

After each Phase 2B sub-phase commit, these tests MUST stay green
byte-identically. Any drift = behavior change → halt + investigate.

Scenarios:
1. _NEWS_CASE_RE pattern pinned (catch accidental regex drift during extraction)
2. semantic_retrieve / retrieve_case_pairs call gates (text length, NEWS_CASE match)
3. `or None` semantics: empty retrieve list → None passed to _build_config
4. Happy path returns string
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Set dummy env BEFORE any line_bot import (pydantic-settings reads .env at import time)
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

# Make line_bot/ importable from tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gemini_client as gc  # noqa: E402


# ---------- 1. Regex drift guard ----------

def test_news_case_regex_pattern_pinned():
    """Pin the exact NEWS_CASE regex pattern (catches accidental edit during extraction)."""
    expected_pattern = (
        r"新聞|報導|案例|個案|研究|論文|文章|貼文|"
        r"保險|保單|投保|理賠|"
        r"投資|股市|理財|"
        r"醫療|醫生|醫院|疫苗|藥物|手術|症狀|病|健康|養生|"
        r"教育|升學|考試|教改|"
        r"法律|法案|法條|判決|訴訟|"
        r"消費|商品|品牌|"
        r"房地產|房市|房價|租屋|"
        r"AI|人工智慧|詐騙|騙局|"
        r"分析|評論|心得|解析"
    )
    assert gc._NEWS_CASE_RE.pattern == expected_pattern


def test_news_case_regex_matches_known_keywords():
    samples = ["新聞報導", "保險理賠", "投資建議", "醫療諮詢", "房價分析", "AI 詐騙"]
    for s in samples:
        assert gc._NEWS_CASE_RE.search(s), f"Should match: {s!r}"


def test_news_case_regex_skips_casual_chat():
    samples = ["你好", "謝謝", "今天天氣不錯", "晚安"]
    for s in samples:
        assert not gc._NEWS_CASE_RE.search(s), f"Should NOT match: {s!r}"


# ---------- Mock fixtures ----------

class _MockResponse:
    """Minimal mock for Gemini response object — mirrors fields chat() reads."""
    def __init__(self, text="這是個測試回覆。"):
        self.text = text
        self.candidates = []
        self.usage_metadata = MagicMock(
            prompt_token_count=10,
            candidates_token_count=20,
            total_token_count=30,
            thoughts_token_count=0,
        )
        self.grounding_metadata = None


def _mock_chat_session(reply_text="這是個測試回覆。"):
    sess = MagicMock()
    sess.send_message.return_value = _MockResponse(text=reply_text)
    return sess


# ---------- 2-5. Retrieve call gates + or-None semantics ----------

@patch("stock_quote.get_quotes_text", return_value="")
@patch("embedding_recall.retrieve_case_pairs")
@patch("embedding_recall.retrieve")
@patch("gemini_client._client")
def test_retrieve_skipped_for_short_text(mock_client, mock_retrieve, mock_case, _stock):
    """user_text strip length < 4 → both retrieves NOT called."""
    mock_client.chats.create.return_value = _mock_chat_session()
    gc.chat(
        user_input="嗨嗨",  # len=2 after strip
        context=[],
        facts=[],
        group_id="C_test_group",
    )
    mock_retrieve.assert_not_called()
    mock_case.assert_not_called()


@patch("stock_quote.get_quotes_text", return_value="")
@patch("embedding_recall.retrieve_case_pairs")
@patch("embedding_recall.retrieve", return_value=[])
@patch("gemini_client._client")
def test_retrieve_skipped_when_no_group_id(mock_client, mock_retrieve, mock_case, _stock):
    """group_id=None → semantic recall entirely skipped."""
    mock_client.chats.create.return_value = _mock_chat_session()
    gc.chat(
        user_input="今天晚餐吃什麼好呢",
        context=[],
        facts=[],
        group_id=None,
    )
    mock_retrieve.assert_not_called()
    mock_case.assert_not_called()


@patch("stock_quote.get_quotes_text", return_value="")
@patch("embedding_recall.retrieve_case_pairs")
@patch("embedding_recall.retrieve", return_value=[])
@patch("gemini_client._client")
def test_retrieve_called_for_normal_non_news_text(mock_client, mock_retrieve, mock_case, _stock):
    """Normal text (len>=4, no NEWS_CASE) → retrieve called, case_pairs NOT."""
    mock_client.chats.create.return_value = _mock_chat_session()
    gc.chat(
        user_input="今天晚餐吃什麼好呢",
        context=[],
        facts=[],
        group_id="C_test_group",
    )
    mock_retrieve.assert_called_once()
    mock_case.assert_not_called()


@patch("stock_quote.get_quotes_text", return_value="")
@patch("embedding_recall.retrieve_case_pairs", return_value=[])
@patch("embedding_recall.retrieve", return_value=[])
@patch("gemini_client._client")
def test_retrieve_case_pairs_called_for_news_case_text(mock_client, mock_retrieve, mock_case, _stock):
    """NEWS_CASE text → BOTH retrieves called."""
    mock_client.chats.create.return_value = _mock_chat_session()
    gc.chat(
        user_input="這個保險方案值得買嗎",
        context=[],
        facts=[],
        group_id="C_test_group",
    )
    mock_retrieve.assert_called_once()
    mock_case.assert_called_once()


@patch("stock_quote.get_quotes_text", return_value="")
@patch("embedding_recall.retrieve_case_pairs", return_value=[])
@patch("embedding_recall.retrieve", return_value=[])
@patch("gemini_client._build_config")
@patch("gemini_client._client")
def test_empty_recall_list_becomes_none(mock_client, mock_build, _ret, _case, _stock):
    """Current `or None` semantics: empty list → None passed to _build_config."""
    mock_client.chats.create.return_value = _mock_chat_session()
    mock_build.return_value = MagicMock()
    gc.chat(
        user_input="今天晚餐吃什麼好呢",
        context=[],
        facts=[],
        group_id="C_test_group",
    )
    assert mock_build.called, "_build_config should be invoked"
    call_kwargs = mock_build.call_args.kwargs
    assert call_kwargs.get("recall_hits") is None, \
        f"Expected None (or-None semantics), got {call_kwargs.get('recall_hits')!r}"
    assert call_kwargs.get("case_hits") is None, \
        f"Expected None (or-None semantics for non-NEWS path), got {call_kwargs.get('case_hits')!r}"


# ---------- 6. Happy path smoke ----------

@patch("stock_quote.get_quotes_text", return_value="")
@patch("embedding_recall.retrieve_case_pairs", return_value=None)
@patch("embedding_recall.retrieve", return_value=None)
@patch("gemini_client._client")
def test_chat_returns_string_on_happy_path(mock_client, _ret, _case, _stock):
    mock_client.chats.create.return_value = _mock_chat_session(reply_text="這是個測試回覆。")
    result = gc.chat(
        user_input="今天晚餐吃什麼好呢",
        context=[],
        facts=[],
        group_id="C_test_group",
    )
    assert isinstance(result, str)
    assert len(result) > 0
