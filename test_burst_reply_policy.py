import burst_filter
import main
import pytest
import sqlite3

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent


@pytest.fixture(autouse=True)
def _reset_burst_runtime_state():
    with burst_filter._lock:
        burst_filter._pending.clear()
        burst_filter._timers.clear()
        burst_filter._last_reply_tokens.clear()
        burst_filter._waiting_groups.clear()
        burst_filter._generations.clear()
        burst_filter._cancelled_generations.clear()
        burst_filter._claimed_generations.clear()
        burst_filter._retry_attempts.clear()
    yield
    with burst_filter._lock:
        for timer in burst_filter._timers.values():
            timer.cancel()
        burst_filter._pending.clear()
        burst_filter._timers.clear()
        burst_filter._last_reply_tokens.clear()
        burst_filter._waiting_groups.clear()
        burst_filter._generations.clear()
        burst_filter._cancelled_generations.clear()
        burst_filter._claimed_generations.clear()
        burst_filter._retry_attempts.clear()


def test_short_substantive_text_should_trigger_reply():
    assert burst_filter._heuristic_decision("我買好了") == "respond"


def test_exact_chitchat_still_skips_reply():
    assert burst_filter._heuristic_decision("好") == "skip"
    assert burst_filter._heuristic_decision("哈哈") == "skip"


def test_intentional_burst_skip_is_durably_completed(monkeypatch):
    completed: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(burst_filter.memory, "list_filter_rules", lambda _gid: [])
    monkeypatch.setattr(
        burst_filter.memory,
        "mark_inbound_events_completed_no_reply",
        lambda gid, ids: completed.append((gid, ids)),
    )
    monkeypatch.setattr(
        burst_filter,
        "_invoke_flush",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("chitchat skip must not invoke the reply path")
        ),
    )

    burst_filter._classify_and_maybe_respond(
        "GRP001", [("MSG001", "好", "USR001", 1.0)], "TOKEN001"
    )

    assert completed == [("GRP001", ["MSG001"])]


def test_cancelled_burst_is_durably_completed(monkeypatch):
    completed: list[tuple[str, list[str]]] = []
    timer = MagicMock()
    monkeypatch.setattr(
        burst_filter.memory,
        "mark_inbound_events_completed_no_reply",
        lambda gid, ids: completed.append((gid, ids)),
    )

    with burst_filter._lock:
        burst_filter._pending["GRP001"] = [
            ("MSG001", "第一段資料", "USR001", 1.0),
            ("MSG002", "第二段資料", "USR001", 2.0),
        ]
        burst_filter._timers["GRP001"] = timer
        burst_filter._last_reply_tokens["GRP001"] = "TOKEN002"
        burst_filter._waiting_groups.add("GRP001")

    burst_filter.cancel_burst("GRP001")

    timer.cancel.assert_called_once_with()
    assert completed == [("GRP001", ["MSG001", "MSG002"])]
    assert "GRP001" not in burst_filter._pending
    assert "GRP001" not in burst_filter._timers
    assert "GRP001" not in burst_filter._last_reply_tokens
    assert "GRP001" not in burst_filter._waiting_groups


def test_failed_burst_flush_keeps_batch_recoverable(monkeypatch):
    pending = [("MSG001", "synthetic payload", "USR001", 1.0)]
    timer = MagicMock()

    def fail_once(*_args, **_kwargs):
        raise burst_filter.RetryableBurstError("pre-delivery failure")

    monkeypatch.setattr(burst_filter, "_classify_and_maybe_respond", fail_once)
    monkeypatch.setattr(burst_filter.threading, "Timer", lambda *_a, **_kw: timer)
    with burst_filter._lock:
        burst_filter._pending["GRP001"] = list(pending)
        burst_filter._last_reply_tokens["GRP001"] = "TOKEN001"
        burst_filter._generations["GRP001"] = 1

    burst_filter._flush_burst("GRP001", generation=1)

    with burst_filter._lock:
        assert burst_filter._pending["GRP001"] == pending
        assert burst_filter._last_reply_tokens["GRP001"] == "TOKEN001"
        assert burst_filter._timers["GRP001"] is timer
        assert burst_filter._retry_attempts["GRP001"] == (2, 1)
    timer.start.assert_called_once_with()


def test_retryable_burst_failure_preserves_newer_batch_token_and_timer(monkeypatch):
    old = [("MSG001", "old", "USR001", 1.0)]
    newer_timer = MagicMock()

    def fail_after_new_message(*_args, **_kwargs):
        burst_filter.add_to_burst(
            "GRP001", "MSG002", "new", "USR002", "TOKEN002"
        )
        raise burst_filter.RetryableBurstError("pre-delivery failure")

    monkeypatch.setattr(
        burst_filter, "_classify_and_maybe_respond", fail_after_new_message
    )
    timer_factory = MagicMock(return_value=newer_timer)
    monkeypatch.setattr(burst_filter.threading, "Timer", timer_factory)
    with burst_filter._lock:
        burst_filter._pending["GRP001"] = list(old)
        burst_filter._last_reply_tokens["GRP001"] = "TOKEN001"
        burst_filter._waiting_groups.add("GRP001")
        burst_filter._generations["GRP001"] = 1

    burst_filter._flush_burst("GRP001", force_respond=True, generation=1)

    with burst_filter._lock:
        assert [item[:3] for item in burst_filter._pending["GRP001"]] == [
            old[0][:3],
            ("MSG002", "new", "USR002"),
        ]
        assert burst_filter._last_reply_tokens["GRP001"] == "TOKEN002"
        assert burst_filter._timers["GRP001"] is newer_timer
        assert "GRP001" in burst_filter._waiting_groups
    timer_call = timer_factory.call_args
    assert timer_call.args[:2] == (
        burst_filter.BURST_WINDOW_SECONDS,
        burst_filter._flush_burst,
    )
    assert timer_call.kwargs["args"] == ["GRP001", True, 2]
    newer_timer.start.assert_called_once_with()


def test_stale_timer_cannot_consume_newer_batch(monkeypatch):
    timers = [MagicMock(), MagicMock()]
    timer_factory = MagicMock(side_effect=timers)
    monkeypatch.setattr(burst_filter.threading, "Timer", timer_factory)

    burst_filter.add_to_burst("GRP001", "MSG001", "old", "USR001", "TOKEN001")
    burst_filter.add_to_burst("GRP001", "MSG002", "new", "USR002", "TOKEN002")

    burst_filter._flush_burst("GRP001", generation=1)

    with burst_filter._lock:
        assert [item[0] for item in burst_filter._pending["GRP001"]] == [
            "MSG001",
            "MSG002",
        ]
        assert burst_filter._last_reply_tokens["GRP001"] == "TOKEN002"
        assert burst_filter._timers["GRP001"] is timers[1]


def test_new_message_during_respond_merges_old_batch_into_new_timer(monkeypatch):
    timer = MagicMock()
    delivered = MagicMock()
    completed = MagicMock()

    def add_then_respond(group_id, pending, reply_token, _force, generation):
        burst_filter.add_to_burst(
            group_id, "MSG002", "new", "USR002", "TOKEN002"
        )
        burst_filter._invoke_flush(
            group_id,
            "old",
            reply_token,
            pending,
            generation=generation,
        )

    monkeypatch.setattr(
        burst_filter, "_classify_and_maybe_respond", add_then_respond
    )
    monkeypatch.setattr(burst_filter, "_on_flush", delivered)
    monkeypatch.setattr(
        burst_filter.memory, "mark_inbound_events_completed_no_reply", completed
    )
    monkeypatch.setattr(
        burst_filter.threading, "Timer", lambda *_a, **_kw: timer
    )
    with burst_filter._lock:
        burst_filter._pending["GRP001"] = [
            ("MSG001", "old", "USR001", 1.0)
        ]
        burst_filter._last_reply_tokens["GRP001"] = "TOKEN001"
        burst_filter._generations["GRP001"] = 1

    burst_filter._flush_burst("GRP001", generation=1)

    with burst_filter._lock:
        assert [item[0] for item in burst_filter._pending["GRP001"]] == [
            "MSG001",
            "MSG002",
        ]
        assert burst_filter._last_reply_tokens["GRP001"] == "TOKEN002"
        assert burst_filter._timers["GRP001"] is timer
    delivered.assert_not_called()
    completed.assert_not_called()


def test_new_message_during_wait_merges_old_batch_into_new_timer(monkeypatch):
    timers = [MagicMock(), MagicMock()]
    timer_factory = MagicMock(side_effect=timers)
    completed = MagicMock()

    def add_then_wait(*_args, **_kwargs):
        burst_filter.add_to_burst(
            "GRP001", "MSG002", "new", "USR002", "TOKEN002"
        )
        return "wait", "more input expected"

    monkeypatch.setattr(burst_filter.memory, "list_filter_rules", lambda _gid: [])
    monkeypatch.setattr(burst_filter, "_heuristic_decision", lambda _text: None)
    monkeypatch.setattr(
        burst_filter.gemini_client, "classify_burst", add_then_wait
    )
    monkeypatch.setattr(
        burst_filter.memory, "mark_inbound_events_completed_no_reply", completed
    )
    monkeypatch.setattr(burst_filter.threading, "Timer", timer_factory)
    with burst_filter._lock:
        burst_filter._pending["GRP001"] = [
            ("MSG001", "old", "USR001", 1.0)
        ]
        burst_filter._last_reply_tokens["GRP001"] = "TOKEN001"
        burst_filter._generations["GRP001"] = 1

    burst_filter._flush_burst("GRP001", generation=1)

    with burst_filter._lock:
        assert [item[0] for item in burst_filter._pending["GRP001"]] == [
            "MSG001",
            "MSG002",
        ]
        assert burst_filter._last_reply_tokens["GRP001"] == "TOKEN002"
        assert burst_filter._timers["GRP001"] is timers[1]
        assert "GRP001" in burst_filter._waiting_groups
    timers[0].cancel.assert_called_once_with()
    retry_call = timer_factory.call_args_list[1]
    assert retry_call.args[:2] == (
        burst_filter.BURST_WINDOW_SECONDS,
        burst_filter._flush_burst,
    )
    assert retry_call.kwargs["args"] == ["GRP001", True, 3]
    completed.assert_not_called()


def test_cancel_during_flush_prevents_retry_resurrection(monkeypatch):
    def cancel_then_fail(*_args, **_kwargs):
        burst_filter.cancel_burst("GRP001")
        raise burst_filter.RetryableBurstError("pre-delivery failure")

    monkeypatch.setattr(
        burst_filter, "_classify_and_maybe_respond", cancel_then_fail
    )
    with burst_filter._lock:
        burst_filter._pending["GRP001"] = [
            ("MSG001", "synthetic payload", "USR001", 1.0)
        ]
        burst_filter._last_reply_tokens["GRP001"] = "TOKEN001"
        burst_filter._generations["GRP001"] = 1

    burst_filter._flush_burst("GRP001", generation=1)

    with burst_filter._lock:
        assert "GRP001" not in burst_filter._pending
        assert "GRP001" not in burst_filter._timers
        assert "GRP001" not in burst_filter._last_reply_tokens


def test_cancel_during_classification_prevents_callback_handoff(monkeypatch):
    delivered = MagicMock()
    completed: list[tuple[str, list[str]]] = []

    def cancel_then_respond(group_id, pending, reply_token, _force, generation):
        burst_filter.cancel_burst(group_id)
        burst_filter._invoke_flush(
            group_id,
            "synthetic payload",
            reply_token,
            pending,
            generation=generation,
        )

    monkeypatch.setattr(
        burst_filter, "_classify_and_maybe_respond", cancel_then_respond
    )
    monkeypatch.setattr(burst_filter, "_on_flush", delivered)
    monkeypatch.setattr(
        burst_filter.memory,
        "mark_inbound_events_completed_no_reply",
        lambda gid, ids: completed.append((gid, ids)),
    )
    with burst_filter._lock:
        burst_filter._pending["GRP001"] = [
            ("MSG001", "synthetic payload", "USR001", 1.0)
        ]
        burst_filter._last_reply_tokens["GRP001"] = "TOKEN001"
        burst_filter._generations["GRP001"] = 1

    burst_filter._flush_burst("GRP001", generation=1)

    delivered.assert_not_called()
    assert completed == [("GRP001", ["MSG001"])]


def test_claimed_handoff_can_recover_after_later_cancel(monkeypatch):
    retry_timer = MagicMock()

    def claimed_callback(group_id, *_args):
        burst_filter.cancel_burst(group_id)
        raise burst_filter.RetryableBurstError("pre-delivery failure")

    monkeypatch.setattr(burst_filter, "_on_flush", claimed_callback)
    monkeypatch.setattr(
        burst_filter.threading, "Timer", lambda *_a, **_kw: retry_timer
    )
    monkeypatch.setattr(burst_filter.memory, "list_filter_rules", lambda _gid: [])
    monkeypatch.setattr(burst_filter, "_heuristic_decision", lambda _text: "respond")
    with burst_filter._lock:
        burst_filter._pending["GRP001"] = [
            ("MSG001", "synthetic payload", "USR001", 1.0)
        ]
        burst_filter._last_reply_tokens["GRP001"] = "TOKEN001"
        burst_filter._generations["GRP001"] = 1

    burst_filter._flush_burst("GRP001", generation=1)

    with burst_filter._lock:
        assert [item[0] for item in burst_filter._pending["GRP001"]] == [
            "MSG001"
        ]
        assert burst_filter._last_reply_tokens["GRP001"] == "TOKEN001"
        assert burst_filter._timers["GRP001"] is retry_timer
        assert burst_filter._retry_attempts["GRP001"] == (3, 1)
    retry_timer.start.assert_called_once_with()


def test_claimed_failure_merges_into_live_newer_batch(monkeypatch):
    newer_timer = MagicMock()

    def add_newer_then_fail(group_id, *_args):
        burst_filter.add_to_burst(
            group_id, "MSG002", "new", "USR002", "TOKEN002"
        )
        raise burst_filter.RetryableBurstError("pre-delivery failure")

    monkeypatch.setattr(burst_filter, "_on_flush", add_newer_then_fail)
    monkeypatch.setattr(
        burst_filter.threading, "Timer", lambda *_a, **_kw: newer_timer
    )
    monkeypatch.setattr(burst_filter.memory, "list_filter_rules", lambda _gid: [])
    monkeypatch.setattr(burst_filter, "_heuristic_decision", lambda _text: "respond")
    with burst_filter._lock:
        burst_filter._pending["GRP001"] = [
            ("MSG001", "old", "USR001", 1.0)
        ]
        burst_filter._last_reply_tokens["GRP001"] = "TOKEN001"
        burst_filter._generations["GRP001"] = 1

    burst_filter._flush_burst("GRP001", generation=1)

    with burst_filter._lock:
        assert [item[0] for item in burst_filter._pending["GRP001"]] == [
            "MSG001",
            "MSG002",
        ]
        assert burst_filter._last_reply_tokens["GRP001"] == "TOKEN002"
        assert burst_filter._timers["GRP001"] is newer_timer
        assert "GRP001" not in burst_filter._retry_attempts


def test_overlapping_claims_are_tracked_per_generation(monkeypatch):
    retry_timer = MagicMock()
    observed_claims: list[set[tuple[str, int]]] = []

    def callback(group_id, _text, reply_token, _message_ids):
        if reply_token == "TOKEN001":
            burst_filter.add_to_burst(
                group_id, "MSG002", "new", "USR002", "TOKEN002"
            )
            burst_filter._flush_burst(group_id, generation=2)
            with burst_filter._lock:
                assert (group_id, 1) in burst_filter._claimed_generations
            raise burst_filter.RetryableBurstError("pre-delivery failure")
        with burst_filter._lock:
            observed_claims.append(set(burst_filter._claimed_generations))

    monkeypatch.setattr(burst_filter, "_on_flush", callback)
    monkeypatch.setattr(
        burst_filter.threading, "Timer", lambda *_a, **_kw: retry_timer
    )
    monkeypatch.setattr(burst_filter.memory, "list_filter_rules", lambda _gid: [])
    monkeypatch.setattr(burst_filter, "_heuristic_decision", lambda _text: "respond")
    with burst_filter._lock:
        burst_filter._pending["GRP001"] = [
            ("MSG001", "old", "USR001", 1.0)
        ]
        burst_filter._last_reply_tokens["GRP001"] = "TOKEN001"
        burst_filter._generations["GRP001"] = 1

    burst_filter._flush_burst("GRP001", generation=1)

    assert observed_claims == [{("GRP001", 1), ("GRP001", 2)}]
    with burst_filter._lock:
        assert burst_filter._claimed_generations == set()
        assert [item[0] for item in burst_filter._pending["GRP001"]] == [
            "MSG001"
        ]
        assert burst_filter._retry_attempts["GRP001"] == (3, 1)


def test_retryable_burst_failure_stops_after_one_automatic_retry(monkeypatch):
    timer_factory = MagicMock()
    monkeypatch.setattr(
        burst_filter,
        "_classify_and_maybe_respond",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            burst_filter.RetryableBurstError("pre-delivery failure")
        ),
    )
    monkeypatch.setattr(burst_filter.threading, "Timer", timer_factory)
    with burst_filter._lock:
        burst_filter._pending["GRP001"] = [
            ("MSG001", "synthetic payload", "USR001", 1.0)
        ]
        burst_filter._last_reply_tokens["GRP001"] = "TOKEN001"
        burst_filter._generations["GRP001"] = 1

    burst_filter._flush_burst("GRP001", generation=1)
    burst_filter._flush_burst("GRP001", generation=2)

    with burst_filter._lock:
        assert [item[0] for item in burst_filter._pending["GRP001"]] == [
            "MSG001"
        ]
        assert "GRP001" not in burst_filter._timers
        assert "GRP001" not in burst_filter._retry_attempts
        assert burst_filter._last_reply_tokens["GRP001"] == "TOKEN001"
    timer_factory.assert_called_once()


def test_ambiguous_burst_failure_is_not_restored_or_retried(monkeypatch):
    timer_factory = MagicMock()
    monkeypatch.setattr(
        burst_filter,
        "_classify_and_maybe_respond",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("ambiguous")),
    )
    monkeypatch.setattr(burst_filter.threading, "Timer", timer_factory)
    with burst_filter._lock:
        burst_filter._pending["GRP001"] = [
            ("MSG001", "synthetic payload", "USR001", 1.0)
        ]
        burst_filter._last_reply_tokens["GRP001"] = "TOKEN001"
        burst_filter._generations["GRP001"] = 1
        burst_filter._retry_attempts["GRP001"] = (1, 1)

    burst_filter._flush_burst("GRP001", generation=1)

    with burst_filter._lock:
        assert "GRP001" not in burst_filter._pending
        assert "GRP001" not in burst_filter._last_reply_tokens
        assert "GRP001" not in burst_filter._timers
        assert "GRP001" not in burst_filter._retry_attempts
    timer_factory.assert_not_called()


def test_burst_store_failure_is_typed_before_any_reply(monkeypatch):
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda _name: False)
    monkeypatch.setattr(main.memory, "get_context", lambda _gid: [])
    monkeypatch.setattr(main.memory, "check_fact_cache", lambda *_args: None)
    monkeypatch.setattr(main.memory, "top_facts", lambda _gid: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda _gid: [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: text)
    monkeypatch.setattr(main, "_is_market_quote_request", lambda *_a, **_kw: False)
    monkeypatch.setattr(main, "_thinking_indicator", lambda _gid: nullcontext())
    monkeypatch.setattr(main, "_llm_chat", lambda *_a, **_kw: "new value")
    monkeypatch.setattr(main, "_enforce_new_value_reply", lambda text, **_kw: text)
    monkeypatch.setattr(main.memory, "store_fact_cache", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        main.memory,
        "append_turn",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            sqlite3.OperationalError("unable to open database file")
        ),
    )
    reply = MagicMock()
    finance_start = MagicMock()
    monkeypatch.setattr(main, "_reply", reply)
    monkeypatch.setattr(main, "_start_burst_finance_extraction", finance_start)

    with pytest.raises(burst_filter.RetryableBurstError):
        main._handle_burst_flush(
            "GRP001", "synthetic payload", "TOKEN001", ["MSG001"]
        )

    reply.assert_not_called()
    finance_start.assert_not_called()


def test_burst_cache_store_failure_does_not_start_finance_side_task(monkeypatch):
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main.memory, "get_context", lambda _gid: [])
    monkeypatch.setattr(main.memory, "check_fact_cache", lambda *_args: None)
    monkeypatch.setattr(main.memory, "top_facts", lambda _gid: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda _gid: [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: text)
    monkeypatch.setattr(main, "_is_market_quote_request", lambda *_a, **_kw: False)
    monkeypatch.setattr(main, "_thinking_indicator", lambda _gid: nullcontext())
    monkeypatch.setattr(main, "_llm_chat", lambda *_a, **_kw: "new value")
    monkeypatch.setattr(main, "_enforce_new_value_reply", lambda text, **_kw: text)
    monkeypatch.setattr(
        main.memory,
        "store_fact_cache",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            sqlite3.OperationalError("unable to open database file")
        ),
    )
    finance_start = MagicMock()
    reply = MagicMock()
    monkeypatch.setattr(main, "_start_burst_finance_extraction", finance_start)
    monkeypatch.setattr(main, "_reply", reply)

    with pytest.raises(burst_filter.RetryableBurstError):
        main._handle_burst_flush(
            "GRP001", "synthetic payload", "TOKEN001", ["MSG001"]
        )

    finance_start.assert_not_called()
    reply.assert_not_called()


def test_burst_context_read_failure_is_typed_before_side_work_or_reply(monkeypatch):
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(
        main.memory,
        "get_context",
        lambda _gid: (_ for _ in ()).throw(
            sqlite3.OperationalError("unable to open database file")
        ),
    )
    side_task = MagicMock()
    reply = MagicMock()
    monkeypatch.setattr(main, "_gemini_side_task_allowed", side_task)
    monkeypatch.setattr(main, "_reply", reply)

    with pytest.raises(burst_filter.RetryableBurstError):
        main._handle_burst_flush(
            "GRP001", "synthetic payload", "TOKEN001", ["MSG001"]
        )

    side_task.assert_not_called()
    reply.assert_not_called()


def test_burst_persona_read_failure_is_typed_before_side_work_or_reply(monkeypatch):
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main.memory, "get_context", lambda _gid: [])
    monkeypatch.setattr(main.memory, "check_fact_cache", lambda *_args: None)
    monkeypatch.setattr(main.memory, "top_facts", lambda _gid: [])
    monkeypatch.setattr(
        main,
        "_get_persona_notes",
        lambda _gid: (_ for _ in ()).throw(
            sqlite3.OperationalError("unable to open database file")
        ),
    )
    side_task = MagicMock()
    reply = MagicMock()
    monkeypatch.setattr(main, "_gemini_side_task_allowed", side_task)
    monkeypatch.setattr(main, "_reply", reply)

    with pytest.raises(burst_filter.RetryableBurstError):
        main._handle_burst_flush(
            "GRP001", "synthetic payload", "TOKEN001", ["MSG001"]
        )

    side_task.assert_not_called()
    reply.assert_not_called()


def test_post_handoff_reply_failure_remains_ambiguous(monkeypatch):
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda _name: False)
    monkeypatch.setattr(main.memory, "get_context", lambda _gid: [])
    monkeypatch.setattr(main.memory, "check_fact_cache", lambda *_args: None)
    monkeypatch.setattr(main.memory, "top_facts", lambda _gid: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda _gid: [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: text)
    monkeypatch.setattr(main, "_is_market_quote_request", lambda *_a, **_kw: False)
    monkeypatch.setattr(main, "_thinking_indicator", lambda _gid: nullcontext())
    monkeypatch.setattr(main, "_llm_chat", lambda *_a, **_kw: "new value")
    monkeypatch.setattr(main, "_enforce_new_value_reply", lambda text, **_kw: text)
    monkeypatch.setattr(main.memory, "store_fact_cache", lambda *_a, **_kw: None)
    monkeypatch.setattr(main.memory, "append_turn", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "_maybe_extract_facts", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        main, "_maybe_capture_calendar_event", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        main,
        "_reply",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("handoff unknown")),
    )

    with pytest.raises(RuntimeError, match="handoff unknown") as exc_info:
        main._handle_burst_flush(
            "GRP001", "synthetic payload", "TOKEN001", ["MSG001"]
        )

    assert not isinstance(exc_info.value, burst_filter.RetryableBurstError)


def test_partial_conversation_write_failure_remains_ambiguous(monkeypatch):
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda _name: False)
    monkeypatch.setattr(main.memory, "get_context", lambda _gid: [])
    monkeypatch.setattr(main.memory, "check_fact_cache", lambda *_args: None)
    monkeypatch.setattr(main.memory, "top_facts", lambda _gid: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda _gid: [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: text)
    monkeypatch.setattr(main, "_is_market_quote_request", lambda *_a, **_kw: False)
    monkeypatch.setattr(main, "_thinking_indicator", lambda _gid: nullcontext())
    monkeypatch.setattr(main, "_llm_chat", lambda *_a, **_kw: "new value")
    monkeypatch.setattr(main, "_enforce_new_value_reply", lambda text, **_kw: text)
    monkeypatch.setattr(main.memory, "store_fact_cache", lambda *_a, **_kw: None)
    monkeypatch.setattr(main.memory, "append_turn", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        main,
        "_append_bot_turn",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            sqlite3.OperationalError("unable to open database file")
        ),
    )
    reply = MagicMock()
    monkeypatch.setattr(main, "_reply", reply)

    with pytest.raises(sqlite3.OperationalError) as exc_info:
        main._handle_burst_flush(
            "GRP001", "synthetic payload", "TOKEN001", ["MSG001"]
        )

    assert not isinstance(exc_info.value, burst_filter.RetryableBurstError)
    reply.assert_not_called()


def test_answered_burst_carries_every_inbound_message_id(monkeypatch):
    captured: list[tuple[str, str, str, list[str]]] = []
    monkeypatch.setattr(burst_filter.memory, "list_filter_rules", lambda _gid: [])
    monkeypatch.setattr(
        burst_filter,
        "_on_flush",
        lambda gid, text, token, message_ids: captured.append(
            (gid, text, token, message_ids)
        ),
    )

    burst_filter._classify_and_maybe_respond(
        "GRP001",
        [
            ("MSG001", "第一段資料", "USR001", 1.0),
            ("MSG002", "第二段資料", "USR001", 2.0),
        ],
        "TOKEN002",
    )

    assert captured == [
        ("GRP001", "第一段資料\n第二段資料", "TOKEN002", ["MSG001", "MSG002"])
    ]


def test_cached_burst_reply_success_marks_the_whole_batch(monkeypatch):
    marked: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda _name: False)
    monkeypatch.setattr(main.memory, "get_context", lambda _gid: [])
    monkeypatch.setattr(main.memory, "check_fact_cache", lambda *_args: "快取回覆")
    monkeypatch.setattr(main, "_is_market_quote_request", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        main.memory,
        "mark_inbound_events_replied",
        lambda group_id, message_ids: marked.append((group_id, message_ids))
        or len(message_ids),
    )

    def accepted_reply(reply_token, *_args, **_kwargs):
        main._mark_inbound_reply_succeeded(reply_token)
        return True

    monkeypatch.setattr(main, "_reply", accepted_reply)

    main._handle_burst_flush(
        "GRP001",
        "第一段資料\n第二段資料",
        "TOKEN002",
        ["MSG001", "MSG002"],
    )

    assert marked == [("GRP001", ["MSG001", "MSG002"])]


def test_burst_quota_miss_does_not_retry_cloud_and_closes_inbound(monkeypatch, tmp_path):
    monkeypatch.setattr(main.memory, "_DB_PATH", tmp_path / "memory.sqlite3")
    main.memory._init_db()
    for message_id in ("MSG001", "MSG002"):
        assert main.memory.begin_inbound_event("GRP001", message_id) == "new"
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda _name: False)
    monkeypatch.setattr(main.memory, "get_context", lambda _gid: [])
    monkeypatch.setattr(main.memory, "check_fact_cache", lambda *_args: None)
    monkeypatch.setattr(main.memory, "top_facts", lambda _gid: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda _gid: [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: text)
    monkeypatch.setattr(main, "_is_market_quote_request", lambda *_a, **_kw: False)
    monkeypatch.setattr(main, "_thinking_indicator", lambda _gid: nullcontext())
    monkeypatch.setattr(main, "_mark_quota_exhausted", lambda: None)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: True)
    monkeypatch.setattr(main, "_quota_recheck_allowed", lambda: False)
    monkeypatch.setattr(main, "_local_text_llm_fallback", lambda *_a, **_kw: "")
    monkeypatch.setattr(main, "_maybe_capture_calendar_event", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: False)
    monkeypatch.setattr(main.gemini_client, "chat", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("unexpected Gemini retry")))
    import lite_reply
    monkeypatch.setattr(lite_reply, "lite_reply", lambda *_a, **_kw: "")

    cloud_attempts = []
    replies = []

    def quota_once(*_args):
        cloud_attempts.append(1)
        if len(cloud_attempts) > 1:
            raise AssertionError("burst retried the cloud route after 429")
        raise RuntimeError("429 RESOURCE_EXHAUSTED PerDay")

    def silent_reply(token, text, **_kwargs):
        replies.append(text)
        assert main._mark_inbound_reply_completed_no_reply(token)
        return False

    monkeypatch.setattr(main, "_llm_chat", quota_once)
    monkeypatch.setattr(main, "_reply", silent_reply)
    main._handle_burst_flush(
        "GRP001", "測試內容", "TOKEN001", ["MSG001", "MSG002"]
    )

    assert len(cloud_attempts) == 1
    assert replies == [main._visible_llm_degraded_reply()]
    assert [main.memory.get_inbound_event_status("GRP001", message_id)
            for message_id in ("MSG001", "MSG002")] == [
        "completed_no_reply", "completed_no_reply"
    ]


def test_mark_quota_exhausted_records_the_fresh_429_as_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_QUOTA_STATE_FILE", str(tmp_path / "quota_state.json"))
    monkeypatch.setattr(main, "_quota_exhausted_until_ts", main._quota_exhausted_until_ts)
    monkeypatch.setattr(main, "_quota_notified_for_ts", main._quota_notified_for_ts)
    monkeypatch.setattr(main, "_quota_last_probe_ts", 0.0)
    monkeypatch.setattr(main.gemini_client, "mark_quota_exhausted_in_usage", lambda: None)

    main._mark_quota_exhausted()

    assert main._quota_last_probe_ts > 0
    assert main._quota_recheck_allowed() is False


def _make_text_event(text: str):
    msg = MagicMock(spec=TextMessageContent)
    msg.id = "MSG001"
    msg.text = text
    msg.type = "text"
    msg.mention = None
    msg.quoted_message_id = None

    src = MagicMock(spec=GroupSource)
    src.group_id = "GRP001"
    src.user_id = "USR001"

    event = MagicMock(spec=MessageEvent)
    event.message = msg
    event.source = src
    event.reply_token = "TOKEN001"
    event.delivery_context = MagicMock(is_redelivery=False)
    return event


def test_web_research_question_detector_covers_public_info_questions():
    assert main._is_web_research_question("美股最近怎樣")
    assert main._is_web_research_question("紐西蘭氣候如何")
    assert main._is_web_research_question("WezTerm 可以支援 M1 晶片嗎？")
    assert main._is_web_research_question("日本哪裡好玩")


def test_web_research_question_detector_ignores_plain_chat():
    assert not main._is_web_research_question("我買好了")
    assert not main._is_web_research_question("好")
    assert not main._is_web_research_question("媽媽最近怎樣")
    assert not main._is_web_research_question("你推薦哪個")
    assert not main._is_web_research_question("媽媽推薦哪個")


def test_plain_web_research_question_bypasses_burst():
    event = _make_text_event("美股最近怎樣")

    with patch("main.feedback_collector.in_feedback_window", return_value=False), \
         patch("main._try_one_shot_reply", return_value=False), \
         patch("main._try_handle_calendar_correction", return_value=False), \
         patch("main._detect_user_correction"), \
         patch("main._auto_capture_text_if_important"), \
         patch("main._maybe_extract_reminder"), \
         patch("main._handle_command", return_value=None), \
         patch("main._is_todo_query", return_value=False), \
         patch("main._is_dinner_question", return_value=False), \
         patch("main._extract_gemini_trigger", return_value=None), \
         patch("main._handle_web_research_question", return_value=True) as mock_web, \
         patch("main.burst_filter.add_to_burst") as mock_burst:
        main._handle_text_message(event, "GRP001")

    # A question is addressed to the bot; a bare statement is not (2026-10-04).
    mock_web.assert_called_once_with(
        event, "GRP001", "美股最近怎樣", cancel_pending_burst=True, addressed=True
    )
    mock_burst.assert_not_called()


def test_web_research_handler_injects_crawled_sources(monkeypatch):
    event = _make_text_event("紐西蘭氣候如何")
    captured: dict[str, str] = {}
    replies: list[str] = []

    monkeypatch.setattr(main, "_thinking_indicator", lambda _gid: nullcontext())
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [
        {
            "title": "New Zealand climate overview",
            "url": "https://www.metservice.com/example",
            "domain": "metservice.com",
            "full_text": "紐西蘭氣候受海洋影響，北島較溫暖，南島較涼。",
        }
    ])
    monkeypatch.setattr(main.memory, "get_context", lambda _gid: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda *_a, **_k: [])
    monkeypatch.setattr(main.memory, "append_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "_get_persona_notes", lambda _gid: [])

    def fake_llm(prompt, *_args):
        captured["prompt"] = prompt
        return "紐西蘭氣候要分北島和南島看。"

    monkeypatch.setattr(main, "_llm_chat", fake_llm)
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, group_id=None, **_kw: replies.append(text),
    )

    assert main._handle_web_research_question(event, "GRP001", "紐西蘭氣候如何")
    assert "紐西蘭氣候如何" in captured["prompt"]
    assert "metservice.com" in captured["prompt"]
    assert "本機爬蟲資料" in captured["prompt"]
    assert replies == ["紐西蘭氣候要分北島和南島看。"]


def test_quota_exhausted_text_still_enters_text_handler():
    event = _make_text_event("我買好了")

    main.settings.allowed_group_id = "GRP001"
    main.settings.allowed_group_ids_raw = ""

    with (
        patch("main._quota_exhausted", return_value=True),
        patch("main._handle_text_message") as mock_text_handler,
        patch("main._save_pending_any") as mock_save_pending,
        patch("main._try_piggyback_drain_with_reply_token") as mock_piggyback,
        patch("main._spawn_piggyback_drain") as mock_spawn,
    ):
        main._handle_event(event)

    mock_text_handler.assert_called_once_with(event, "GRP001")
    mock_spawn.assert_called_once_with("GRP001")
    mock_save_pending.assert_not_called()
    mock_piggyback.assert_not_called()
