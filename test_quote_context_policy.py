from contextlib import nullcontext
from types import SimpleNamespace

import pytest
import main
import memory
import burst_filter
from unittest.mock import MagicMock


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(main, "_get_member_display_name", lambda *_a: "synthetic sender")
    monkeypatch.setattr(main, "_thinking_indicator", lambda *_a: nullcontext())
    monkeypatch.setattr(memory, "get_context", lambda *_a: [])
    monkeypatch.setattr(memory, "top_facts", lambda *_a, **_k: [])
    monkeypatch.setattr(memory, "append_turn", lambda *_a: None)
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_a: [])
    monkeypatch.setattr(main, "_reply", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "_maybe_extract_facts", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "_maybe_capture_calendar_event", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a: False)


def test_missing_quote_never_substitutes_recent_chat(monkeypatch, isolated):
    monkeypatch.setattr(memory, "get_raw_message", lambda *_a: None)
    monkeypatch.setattr(memory, "get_recent_raw_messages", lambda *_a, **_k: [("other", "user", "UNRELATED_SENTINEL", 1)])
    block = main._build_quoted_block(SimpleNamespace(quoted_message_id="missing"), "group")
    assert "UNRELATED_SENTINEL" not in block
    assert "推斷使用者引用" not in block
    assert "不要跟使用者說你找不到" not in block
    assert "未取得" in block


def test_pending_keeps_complete_exact_quote(isolated):
    source = "合成原文" * 40 + "DECISIVE_DETAIL_AT_END"
    parts = main._build_group_parts([{"type": "text", "text": "竟然", "quoted_original": source}], "group")
    assert source in "\n".join(p for p in parts if isinstance(p, str))


def test_research_prompt_keeps_quote_without_searching_private_source(monkeypatch, isolated):
    original = "PRIVATE_SOURCE_SENTINEL，這只是單一案例。"
    monkeypatch.setattr(memory, "get_raw_message", lambda *_a: ("user", original))
    monkeypatch.setattr(memory, "get_raw_message_meta", lambda *_a: {})
    queries, prompts = [], []
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda query: queries.append(query) or [{"url": "https://example.com/report", "full_text": "合成查證資料足夠長，顯示資料範圍只涵蓋其中一個地區。"}])
    monkeypatch.setattr(main, "_llm_chat", lambda prompt, *_a: prompts.append(prompt) or "資料只涵蓋局部，不能推論整體。")
    event = SimpleNamespace(source=SimpleNamespace(user_id="user"), reply_token="token", message=SimpleNamespace(quoted_message_id="source"))
    assert main._handle_web_research_question(event, "group", "今年產量增加嗎？")
    assert original in prompts[0]
    assert all(original not in query and "PRIVATE_SOURCE" not in query for query in queries)


def test_burst_keeps_each_reply_boundary(monkeypatch):
    captured = []
    monkeypatch.setattr(memory, "list_filter_rules", lambda *_a: [])
    monkeypatch.setattr(
        burst_filter,
        "_invoke_flush",
        lambda _g, text, *_a, **_kw: captured.append(text),
    )
    source = "--- 原始訊息 開始 ---\nSOURCE_A\n--- 原始訊息 結束 ---\n--- 目前回覆 開始 ---\n竟然\n--- 目前回覆 結束 ---"
    burst_filter._classify_and_maybe_respond("group", [("m1", source, "u1", 1), ("m2", "誰讓你發現的", "u2", 2)], "token", True)
    assert "群組訊息 1" in captured[0]
    assert "群組訊息 2" in captured[0]
    assert source in captured[0]


def test_quoted_burst_bypasses_unbound_fact_cache(monkeypatch, isolated):
    monkeypatch.setattr(memory, "check_fact_cache", lambda *_a: pytest.fail("quoted cache must not override source"))
    monkeypatch.setattr(memory, "store_fact_cache", lambda *_a: pytest.fail("quoted answers are contextual"))
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: text)
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: "只適用這個合成案例。")
    main._handle_burst_flush("group", "--- 原始訊息 開始 ---\nSOURCE\n--- 原始訊息 結束 ---\n竟然", "token")


def test_exact_source_and_quote_edge_are_group_scoped(isolated):
    memory.log_raw_message("group-a", "source", "user", "EXACT_ORIGINAL", index_for_recall=False)
    memory.log_raw_message("group-a", "reply", "user", "竟然", quoted_message_id="source", index_for_recall=False)
    assert memory.get_quoted_message_id("group-a", "reply") == "source"
    assert memory.get_quoted_message_id("group-b", "reply") is None
    assert memory.get_raw_message("group-b", "source") is None
    block = main._build_quoted_block(SimpleNamespace(id="reply"), "group-a")
    assert "EXACT_ORIGINAL" in block
    assert "未取得" in main._build_quoted_block(SimpleNamespace(quoted_message_id="source"), "group-b")


def test_sister_text_is_quotable_routed_and_indexed(monkeypatch):
    """2026-10-09：取消妹妹零回覆。設定裡「妹妹」傳的文字照常路由、建回想索引，也照樣能被引用。"""
    from linebot.v3.webhooks import MessageEvent, TextMessageContent, GroupSource
    event = MagicMock(spec=MessageEvent)
    event.source = MagicMock(spec=GroupSource)
    event.source.group_id, event.source.user_id = "group", "sister-user"
    event.message = MagicMock(spec=TextMessageContent)
    event.message.id, event.message.text = "sister-source", "SISTER_SOURCE_TEXT"
    event.message.quoted_message_id = "older"
    event.reply_token = "token"
    event.delivery_context = None
    monkeypatch.setattr(main.settings, "allowed_group_ids_raw", "")
    monkeypatch.setattr(main.settings, "allowed_group_id", "")
    monkeypatch.setattr(main.line_mentions, "user_id_for_family_role", lambda _role: "sister-user")
    routed = []
    monkeypatch.setattr(main, "_handle_text_message", lambda *a: routed.append(a))
    indexed = []

    def _submit(fn, *_a, **_k):
        indexed.append(fn)
        memory._EMBED_INFLIGHT.release()

    monkeypatch.setattr(memory._EMBED_EXECUTOR, "submit", _submit)
    main._handle_event(event)
    assert routed == [(event, "group")]
    assert len(indexed) == 1
    assert memory.get_raw_message("group", "sister-source")[1] == "SISTER_SOURCE_TEXT"
    assert memory.get_quoted_message_id("group", "sister-source") == "older"
    assert memory.get_inbound_event_status("group", "sister-source") != "completed_no_reply"


def test_explicit_retry_keeps_original_even_with_nonempty_current_text(monkeypatch, isolated):
    saved = []
    monkeypatch.setattr(memory, "get_raw_message", lambda *_a: ("user", "EXACT_ORIGINAL"))
    monkeypatch.setattr(memory, "get_raw_message_meta", lambda *_a: {})
    monkeypatch.setattr(main, "_handle_explicit_poll_text", lambda *_a: None)
    monkeypatch.setattr(main, "_get_explicit_market_quote_reply", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: text)
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: "")
    monkeypatch.setattr(main, "_quota_exhausted", lambda: True)
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: True)
    monkeypatch.setattr(main, "_save_pending_burst_text", lambda _g, text: saved.append(text))
    event = SimpleNamespace(source=SimpleNamespace(user_id="user"), message=SimpleNamespace(id="reply", quoted_message_id="source"), reply_token="token")
    main._handle_explicit_text(event, "group", "這代表什麼？")
    assert len(saved) == 1 and "EXACT_ORIGINAL" in saved[0] and "這代表什麼？" in saved[0]


def test_archive_uses_accepted_line_id_and_complete_sent_text(monkeypatch):
    sent_text = "合成送出內容" * 120 + "IMPORTANT_END"
    monkeypatch.setattr(memory._EMBED_EXECUTOR, "submit", lambda *_a: None)
    main._archive_sent_texts("group", SimpleNamespace(sent_messages=[SimpleNamespace(id="actual-line-id")]), [sent_text])
    assert memory.get_raw_message("group", "actual-line-id") == ("__bot__", sent_text)
    assert memory.get_raw_message("another-group", "actual-line-id") is None


def test_archive_failure_does_not_change_accepted_delivery(monkeypatch):
    monkeypatch.setattr(memory, "log_raw_message", lambda *_a: (_ for _ in ()).throw(RuntimeError("synthetic")))
    main._archive_sent_texts("group", SimpleNamespace(sent_messages=[SimpleNamespace(id="actual-line-id")]), ["sent text"])


def test_one_shot_archives_actual_returned_id(monkeypatch):
    text = "這是合成的已核准回覆。"
    monkeypatch.setattr(main, "_load_one_shot_replies", lambda: {"group": text})
    monkeypatch.setattr(main, "_save_one_shot_replies", lambda *_a: None)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)
    monkeypatch.setattr(main, "ApiClient", lambda *_a: nullcontext(None))
    monkeypatch.setattr(main, "MessagingApi", lambda *_a: SimpleNamespace(reply_message=lambda *_a: SimpleNamespace(sent_messages=[SimpleNamespace(id="line-one-shot")])) )
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *_a: None)
    monkeypatch.setattr(memory._EMBED_EXECUTOR, "submit", lambda *_a: None)
    assert main._try_one_shot_reply(SimpleNamespace(reply_token="token"), "group")
    assert memory.get_raw_message("group", "line-one-shot") == ("__bot__", text)


def test_explicit_market_followup_uses_exact_quote_not_recent_topic(monkeypatch, isolated):
    contexts = []
    monkeypatch.setattr(memory, "get_raw_message", lambda *_a: ("user", "合成來源 SOXX"))
    monkeypatch.setattr(memory, "get_raw_message_meta", lambda *_a: {})
    monkeypatch.setattr(memory, "get_context", lambda *_a: [("user", "無關的 TQQQ")])
    monkeypatch.setattr(main, "_handle_explicit_poll_text", lambda *_a: None)
    monkeypatch.setattr(main, "_requires_public_research", lambda *_a: False)
    monkeypatch.setattr(main, "_get_explicit_market_quote_reply", lambda _text, *, context: contexts.append(context) or "合成查價結果。")
    event = SimpleNamespace(source=SimpleNamespace(user_id="user"), message=SimpleNamespace(quoted_message_id="source"), reply_token="token")
    main._handle_explicit_text(event, "group", "現在價格多少？")
    assert "SOXX" in str(contexts) and "TQQQ" not in str(contexts)


def test_quota_fallback_preserves_quote_without_lite_search(monkeypatch):
    import lite_reply
    captured = []
    text = "--- 原始訊息 開始 ---\nPRIVATE_QUOTE\n--- 原始訊息 結束 ---\n竟然"
    monkeypatch.setattr(main, "_quota_exhausted", lambda: True)
    monkeypatch.setattr(main, "_quota_recheck_allowed", lambda: False)
    monkeypatch.setattr(lite_reply, "lite_reply", lambda *_a, **_k: pytest.fail("private quote must not enter search handlers"))
    monkeypatch.setattr(main, "_local_text_llm_fallback", lambda text, **_k: captured.append(text) or "合成評論")
    assert main._gemini_llm_chat(text, [], []) == "合成評論"
    assert captured == [text]


def test_direct_lite_quote_cannot_trigger_external_handlers(monkeypatch):
    import lite_reply
    monkeypatch.setattr(lite_reply, "_try_local_llm", lambda *_a, **_k: pytest.fail("could collect references"))
    monkeypatch.setattr(lite_reply, "_STAGE1_HANDLERS", (lambda *_a: pytest.fail("wrong target"),))
    assert lite_reply.lite_reply("--- 原始訊息 開始 ---\nPRIVATE_SOURCE\n--- 原始訊息 結束 ---\n是什麼？") is None


def test_pending_missing_snapshot_can_resolve_only_its_bound_id(monkeypatch, isolated):
    from quote_context import missing_block
    monkeypatch.setattr(memory, "get_raw_message", lambda group, mid: ("user", "NOW_AVAILABLE") if (group, mid) == ("group", "source") else None)
    monkeypatch.setattr(memory, "get_raw_message_meta", lambda *_a: {})
    text = main._pending_text_with_quote({"text": "竟然", "quoted_context": missing_block(), "quoted_message_id": "source"}, "group")
    assert "NOW_AVAILABLE" in text
    assert "【引用原文未取得】" not in text
