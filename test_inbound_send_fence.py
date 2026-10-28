"""Durable send fence (LINE bot fixes batch 1, 2026-10-24).  Synthetic ids and texts only.

Before: a re-run of an inbound event (LINE redelivered it after the 30 s lease) that reached
`_reply` after the first run's reply was accepted failed with "Invalid reply token" and then
*pushed* the answer a second time; only the monthly push quota (HTTP 429 since 10/8) kept it
out of the group.  Now `_reply` moves the event's rows to `reply_sending` in the database
before anything leaves for LINE:

* LINE accepts            -> replied
* LINE refuses (4xx)      -> the fence lifts (payload) or the event closes (token dead)
* timeout / 5xx / killed  -> the fence stays: no re-run, no push, however many redeliveries
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from linebot.v3.messaging.exceptions import ApiException
from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent

import burst_filter
import main
import memory

GROUP = "GRP-FENCE"
TOKEN = "TOKEN-SYNTHETIC-0001"
HERE = Path(__file__).resolve().parent


class _Resp:
    def __init__(self, status, reason, data, headers):
        self.status, self.reason, self.data, self._headers = status, reason, data, headers

    def getheaders(self):
        return self._headers


def api_error(status, body=None, reason="Bad Request"):
    payload = json.dumps(body if body is not None else {"message": "x"}).encode()
    return ApiException(http_resp=_Resp(status, reason, payload, {"x-line-request-id": "req-123"}))


BAD_TOKEN = {"message": "Invalid reply token"}
BAD_PAYLOAD = {
    "message": "The request body has 1 error(s)",
    "details": [{"message": "May not be empty", "property": "messages[0].text"}],
}
MONTHLY = {"message": "You have reached your monthly limit."}


@pytest.fixture()
def line(monkeypatch):
    """A fake LINE API; the registry is private to the test."""
    api = MagicMock()
    api.__enter__ = MagicMock(return_value=api)
    api.__exit__ = MagicMock(return_value=False)
    messaging = MagicMock()
    messaging.reply_message.return_value = SimpleNamespace(sent_messages=[])
    messaging.push_message.return_value = SimpleNamespace(sent_messages=[])
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main, "_inbound_ids_by_token", {})
    monkeypatch.setattr(main, "_INBOUND_FENCE_CLAIM_RETRY_SLEEP_SECONDS", (0, 0))
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: False)
    with (
        patch("main.ApiClient", return_value=api),
        patch("main.MessagingApi", return_value=messaging),
        patch("main._load_pending_explicit", return_value={}),
    ):
        yield messaging


@pytest.fixture()
def clock(monkeypatch):
    state = {"t": 1_800_000_000.0}
    monkeypatch.setattr(memory._time, "time", lambda: state["t"])
    return state


def open_event(message_id="M1", token=TOKEN, group=GROUP, *, expect="new"):
    """What `_handle_event` does at the start of a run."""
    assert memory.begin_inbound_event(group, message_id) == expect
    main._register_inbound_reply_token(token, group, message_id)
    return message_id


def reopen(message_id="M1", token=TOKEN, group=GROUP, *, expect="retry"):
    """A redelivery that starts a second run of the same event."""
    return open_event(message_id, token, group, expect=expect)


def status(message_id="M1", group=GROUP):
    return memory.get_inbound_event_status(group, message_id)


def say(text="好的", *, token=TOKEN, group=GROUP, **kwargs):
    kwargs.setdefault("include_auxiliary", False)
    return main._reply(token, text, group_id=group, **kwargs)


# ── memory.claim_inbound_reply / release_inbound_reply ────────────────────────────────────


def test_claim_moves_the_active_rows_to_reply_sending(clock):
    memory.begin_inbound_event(GROUP, "a")
    memory.begin_inbound_event(GROUP, "b")
    memory.mark_inbound_event_media_processing(GROUP, "b")

    assert memory.claim_inbound_reply(GROUP, ["a", "b"]) == ("claimed", False)
    assert status("a") == status("b") == "reply_sending"


def test_claim_reports_a_lease_retry(monkeypatch, clock):
    memory.begin_inbound_event(GROUP, "a")
    clock["t"] += 200
    assert memory.begin_inbound_event(GROUP, "a") == "retry"

    assert memory.claim_inbound_reply(GROUP, ["a"]) == ("claimed", True)


def test_claim_is_blocked_by_a_fence_or_an_accepted_reply(clock):
    memory.begin_inbound_event(GROUP, "fenced")
    memory.claim_inbound_reply(GROUP, ["fenced"])
    memory.begin_inbound_event(GROUP, "done")
    memory.mark_inbound_event_replied(GROUP, "done")

    assert memory.claim_inbound_reply(GROUP, ["fenced"]) == ("blocked", False)
    assert memory.claim_inbound_reply(GROUP, ["done"]) == ("blocked", False)
    assert status("fenced") == "reply_sending" and status("done") == "replied"


def test_one_fenced_message_blocks_the_whole_batch_without_touching_the_others(clock):
    memory.begin_inbound_event(GROUP, "a")
    memory.begin_inbound_event(GROUP, "b")
    memory.claim_inbound_reply(GROUP, ["a"])

    assert memory.claim_inbound_reply(GROUP, ["a", "b"]) == ("blocked", False)
    assert status("b") == "processing"


def test_rows_closed_silently_or_missing_leave_nothing_to_fence(clock):
    memory.begin_inbound_event(GROUP, "quiet")
    memory.mark_inbound_events_completed_no_reply(GROUP, ["quiet"])

    assert memory.claim_inbound_reply(GROUP, ["quiet"]) == ("untracked", False)
    assert memory.claim_inbound_reply(GROUP, ["never-seen"]) == ("untracked", False)
    assert memory.claim_inbound_reply(GROUP, []) == ("untracked", False)
    assert memory.claim_inbound_reply("", ["quiet"]) == ("untracked", False)
    assert status("quiet") == "completed_no_reply"


def test_a_silently_closed_message_does_not_stop_the_rest_of_its_batch(clock):
    memory.begin_inbound_event(GROUP, "quiet")
    memory.mark_inbound_events_completed_no_reply(GROUP, ["quiet"])
    memory.begin_inbound_event(GROUP, "live")

    assert memory.claim_inbound_reply(GROUP, ["quiet", "live"]) == ("claimed", False)
    assert status("quiet") == "completed_no_reply" and status("live") == "reply_sending"


def test_release_lifts_only_the_fence(clock):
    memory.begin_inbound_event(GROUP, "a")
    memory.begin_inbound_event(GROUP, "b")
    memory.mark_inbound_event_replied(GROUP, "b")
    memory.claim_inbound_reply(GROUP, ["a"])

    assert memory.release_inbound_reply(GROUP, ["a", "b"]) == 1
    assert status("a") == "processing" and status("b") == "replied"
    assert memory.release_inbound_reply(GROUP, ["a"]) == 0


def test_a_terminal_mark_replaces_the_fence(clock):
    memory.begin_inbound_event(GROUP, "a")
    memory.claim_inbound_reply(GROUP, ["a"])
    memory.mark_inbound_events_completed_no_reply(GROUP, ["a"])
    assert status("a") == "completed_no_reply"

    memory.begin_inbound_event(GROUP, "b")
    memory.claim_inbound_reply(GROUP, ["b"])
    assert memory.mark_inbound_events_replied(GROUP, ["b"]) == 1
    assert status("b") == "replied"


def test_the_drains_close_changes_only_the_events_that_are_still_being_handled(clock):
    """(review R2 GP2 #1) the exit drain must not turn a fence or an accepted reply into
    `completed_no_reply`: that would record a delivered reply as "no reply" and let a late run of
    the same event send unfenced."""
    for message_id in ("active", "media", "fenced", "done", "quiet"):
        memory.begin_inbound_event(GROUP, message_id)
    memory.mark_inbound_event_media_processing(GROUP, "media")
    memory.claim_inbound_reply(GROUP, ["fenced"])
    memory.mark_inbound_event_replied(GROUP, "done")
    memory.mark_inbound_events_completed_no_reply(GROUP, ["quiet"])
    everything = ["active", "media", "fenced", "done", "quiet", "never-seen"]

    assert memory.mark_active_inbound_events_completed_no_reply(GROUP, everything) == 2

    assert [status(m) for m in ("active", "media", "fenced", "done", "quiet")] == [
        "completed_no_reply", "completed_no_reply", "reply_sending", "replied", "completed_no_reply",
    ]
    assert memory.claim_inbound_reply(GROUP, ["fenced"]) == ("blocked", False)  # still fenced
    assert memory.mark_active_inbound_events_completed_no_reply(GROUP, everything) == 0  # nothing left to change
    assert memory.mark_active_inbound_events_completed_no_reply("", everything) == 0
    assert memory.mark_active_inbound_events_completed_no_reply(GROUP, []) == 0


def test_the_drains_close_counts_rows_across_chunks(clock):
    ids = [f"bulk-{n}" for n in range(memory._COMPLETE_BATCH + 7)]
    for message_id in ids:
        memory.begin_inbound_event(GROUP, message_id)
    memory.claim_inbound_reply(GROUP, [ids[3]])

    assert memory.mark_active_inbound_events_completed_no_reply(GROUP, ids) == len(ids) - 1
    assert status(ids[3]) == "reply_sending" and status(ids[-1]) == "completed_no_reply"


def test_only_one_of_many_threads_gets_the_fence(clock):
    memory.begin_inbound_event(GROUP, "race")
    barrier = threading.Barrier(8)
    outcomes: list[str] = []

    def contender():
        barrier.wait()
        outcomes.append(memory.claim_inbound_reply(GROUP, ["race"])[0])

    threads = [threading.Thread(target=contender) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == ["blocked"] * 7 + ["claimed"]


def test_only_one_of_many_processes_gets_the_fence(tmp_path):
    """The fence is a write transaction on the file, so it holds across processes."""
    db = tmp_path / "shared.db"
    env = os.environ.copy()
    env.update({"SQLITE_PATH": str(db), "BOT_MUTED": "true", "CLAUDE_CODE_CMD": "/nonexistent/claude"})
    seed = (
        "import memory; assert memory.begin_inbound_event('G','race')=='new'; print('seeded')"
    )
    subprocess.run([sys.executable, "-c", seed], env=env, cwd=HERE, check=True, capture_output=True, timeout=120)
    go_at = time.time() + 6
    code = (
        "import time, memory\n"
        f"time.sleep(max(0, {go_at!r} - time.time()))\n"
        "print(memory.claim_inbound_reply('G', ['race'])[0])\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", code], env=env, cwd=HERE, stdout=subprocess.PIPE, text=True)
        for _ in range(4)
    ]
    outcomes = [p.communicate(timeout=120)[0].strip() for p in procs]

    assert sorted(outcomes) == ["blocked"] * 3 + ["claimed"]


# ── _reply: accepted ─────────────────────────────────────────────────────────────────────


def test_an_accepted_reply_marks_the_event_replied(line):
    open_event()

    assert say() is True

    assert status() == "replied"
    line.reply_message.assert_called_once()
    line.push_message.assert_not_called()


def test_a_second_run_that_finishes_after_the_first_reply_sends_nothing(line, clock):
    """The 10/20 shape: the original run's reply was accepted; a re-run (lease ran out while
    the original was still working) reaches `_reply` with the same, now spent, token."""
    open_event()
    clock["t"] += 200
    reopen()  # the lease ran out: LINE's redelivery starts a second run
    assert say("原本那一輪的回答") is True
    assert status() == "replied"

    line.reply_message.side_effect = api_error(400, BAD_TOKEN)  # what LINE would answer now
    line.reply_message.reset_mock()
    line.push_message.reset_mock()
    main._register_inbound_reply_token(TOKEN, GROUP, "M1")  # the second run registered it again
    main._inbound_reply_by_token.clear()  # ...and the first run's success had already popped it

    assert say("重跑那一輪的回答") is False

    line.reply_message.assert_not_called()  # blocked before LINE is asked
    line.push_message.assert_not_called()
    assert status() == "replied"


def test_the_slower_run_is_blocked_whichever_run_finishes_first(line, clock):
    open_event()
    clock["t"] += 200
    reopen()
    main._inbound_reply_by_token.clear()  # only the lookup kept for later runs remains

    assert say("第二輪先回") is True
    line.reply_message.reset_mock()

    assert say("第一輪後回") is False
    line.reply_message.assert_not_called()
    line.push_message.assert_not_called()


# ── _reply: ambiguous outcomes ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("read timed out"), ConnectionError("reset by peer"), api_error(500, {"message": "oops"}, "Internal Server Error"), api_error(503, {"message": "busy"}, "Service Unavailable")],
    ids=["timeout", "connection", "http-500", "http-503"],
)
def test_an_ambiguous_failure_keeps_the_fence_and_never_pushes(line, failure):
    open_event()
    line.reply_message.side_effect = failure

    assert say() is False

    assert status() == "reply_sending"
    line.push_message.assert_not_called()


def test_redeliveries_after_an_ambiguous_failure_neither_rerun_nor_push(line, clock):
    open_event()
    line.reply_message.side_effect = TimeoutError("read timed out")
    assert say() is False

    for _ in range(5):  # LINE redelivers about every minute
        clock["t"] += 62
        assert memory.begin_inbound_event(GROUP, "M1") == "reply_sending"
    clock["t"] += 100000
    assert memory.begin_inbound_event(GROUP, "M1") == "reply_sending"

    # And a run that is already under way cannot send either, with the very same token.
    main._register_inbound_reply_token(TOKEN, GROUP, "M1")
    line.reply_message.reset_mock()
    line.reply_message.side_effect = None
    assert say("另一輪的回答") is False
    line.reply_message.assert_not_called()
    line.push_message.assert_not_called()


def test_a_killed_process_leaves_the_fence_and_the_next_instance_sends_nothing(line, monkeypatch, clock):
    """SIGTERM/SIGKILL between LINE accepting the reply and the database mark."""

    class Killed(BaseException):
        pass

    monkeypatch.setattr(memory, "_INSTANCE_ID", "instance-A")
    open_event()
    line.reply_message.side_effect = Killed()
    with pytest.raises(Killed):
        say()
    assert status() == "reply_sending"

    # The bot restarts: new instance, empty registry, LINE redelivers.
    monkeypatch.setattr(memory, "_INSTANCE_ID", "instance-B")
    main._inbound_reply_by_token.clear()
    main._inbound_ids_by_token.clear()
    clock["t"] += 5
    assert memory.begin_inbound_event(GROUP, "M1") == "reply_sending"  # skipped, not retried at once
    line.reply_message.reset_mock()
    line.reply_message.side_effect = None
    main._register_inbound_reply_token(TOKEN, GROUP, "M1")
    assert say("重啟後的另一個回答") is False
    line.reply_message.assert_not_called()
    line.push_message.assert_not_called()


def test_a_restart_before_the_send_lets_the_new_instance_answer(line, monkeypatch, clock):
    monkeypatch.setattr(memory, "_INSTANCE_ID", "instance-A")
    open_event()  # the model call was under way when the bot restarted

    monkeypatch.setattr(memory, "_INSTANCE_ID", "instance-B")
    main._inbound_reply_by_token.clear()
    main._inbound_ids_by_token.clear()
    clock["t"] += 5
    reopen()  # retried at once, no 150 s wait
    assert say("重啟後補上的回答") is True

    assert status() == "replied"
    line.reply_message.assert_called_once()


# ── _reply: LINE refused ──────────────────────────────────────────────────────────────────


def test_a_payload_rejection_lifts_the_fence_without_pushing(line, clock):
    """Batch 2 (B6.3) changed this contract for a *text* reply: LINE's 400 is answered by one plain
    text resend (test_reply_400_recovery.py) and, if that is refused too, by a push where a push
    is allowed.  What stays: where no push is allowed (reply-only flows), and for cards (always),
    a refused reply lifts the fence and pushes nothing."""
    open_event()
    line.reply_message.side_effect = api_error(400, BAD_PAYLOAD)

    assert say(allow_push_fallback=False) is False

    assert status() == "processing"  # nothing was sent: the event may be handled again
    line.push_message.assert_not_called()
    clock["t"] += 200
    assert memory.begin_inbound_event(GROUP, "M1") == "retry"


def test_a_card_payload_rejection_lifts_the_fence_without_pushing(line, clock):
    """A menu / large-menu / Flex command card is never sent again as plain text and never pushed
    (its altText is not a message to push, line_bot.md 10/05)."""
    open_event()
    line.reply_message.side_effect = api_error(400, BAD_PAYLOAD)

    assert say("選單", large_menu_card=True) is False

    line.reply_message.assert_called_once()  # no second attempt
    assert status() == "processing"
    line.push_message.assert_not_called()


@pytest.mark.parametrize("code", [401, 403, 404, 413, 429])
def test_any_other_4xx_also_lifts_the_fence(line, code):
    open_event()
    line.reply_message.side_effect = api_error(code, {"message": "no"}, "No")

    assert say() is False
    assert status() == "processing"
    line.push_message.assert_not_called()


def test_the_event_can_be_answered_after_the_fence_was_lifted(line):
    open_event()
    line.reply_message.side_effect = api_error(400, BAD_PAYLOAD)
    assert say(allow_push_fallback=False) is False  # (batch 2: with a push allowed it would push)
    line.reply_message.side_effect = None
    main._register_inbound_reply_token(TOKEN, GROUP, "M1")

    assert say("第二次") is True
    assert status() == "replied"


# ── _reply: token dead -> push fallback ───────────────────────────────────────────────────


def test_a_dead_token_on_the_first_run_pushes_with_a_fixed_retry_key(line):
    open_event()
    line.reply_message.side_effect = api_error(400, BAD_TOKEN)

    assert say("慢了才好的回答") is True

    line.push_message.assert_called_once()
    key = line.push_message.call_args.kwargs["x_line_retry_key"]
    assert key == main._reply_push_retry_key(
        GROUP, main._InboundSendFence(GROUP, ("M1",), False), TOKEN
    )
    assert status() == "replied"


def test_the_retry_key_depends_on_the_event_not_on_the_words(line):
    fence = main._InboundSendFence(GROUP, ("M1", "M2"), False)
    key = main._reply_push_retry_key(GROUP, fence, "any-token")
    assert key == main._reply_push_retry_key(GROUP, fence, "another-token")
    assert key != main._reply_push_retry_key(GROUP, main._InboundSendFence(GROUP, ("M1",), False), "t")
    assert key != main._reply_push_retry_key("OTHER-GROUP", fence, "t")
    assert len(key) == 36 and key.count("-") == 4  # a UUID, as LINE wants


def test_a_409_on_the_push_means_the_group_already_has_the_answer(line):
    open_event()
    line.reply_message.side_effect = api_error(400, BAD_TOKEN)
    line.push_message.side_effect = api_error(409, {"message": "accepted"}, "Conflict")

    with patch("main.memory.log_raw_message") as archive:
        assert say("同一件事的回答") is True

    assert status() == "replied"
    archive.assert_not_called()  # this run's wording is not what the group saw


def test_a_dead_token_on_a_lease_retry_run_never_pushes_and_closes_the_event(line, clock):
    open_event()
    clock["t"] += 200
    reopen()
    line.reply_message.side_effect = api_error(400, BAD_TOKEN)

    assert say() is False

    line.push_message.assert_not_called()
    assert status() == "completed_no_reply"


def test_a_run_started_by_a_restart_orphan_retry_also_never_pushes(line, monkeypatch, clock):
    monkeypatch.setattr(memory, "_INSTANCE_ID", "instance-A")
    open_event()
    monkeypatch.setattr(memory, "_INSTANCE_ID", "instance-B")
    clock["t"] += 5
    reopen()
    line.reply_message.side_effect = api_error(400, BAD_TOKEN)

    assert say() is False

    line.push_message.assert_not_called()
    assert status() == "completed_no_reply"


def test_a_refused_push_closes_the_event(line):
    open_event()
    line.reply_message.side_effect = api_error(400, BAD_TOKEN)
    line.push_message.side_effect = api_error(429, MONTHLY, "Too Many Requests")

    assert say() is False

    assert status() == "completed_no_reply"
    assert memory.begin_inbound_event(GROUP, "M1") == "completed_no_reply"


def test_an_ambiguous_push_keeps_the_fence(line):
    open_event()
    line.reply_message.side_effect = api_error(400, BAD_TOKEN)
    line.push_message.side_effect = TimeoutError("read timed out")

    assert say() is False

    assert status() == "reply_sending"


def test_a_push_that_could_not_even_be_built_closes_the_event(line, monkeypatch):
    open_event()
    line.reply_message.side_effect = api_error(400, BAD_TOKEN)
    real_builder = main._text_message_with_mentions

    def build(*args, **kwargs):
        if kwargs.get("validation_source") == "reply_push_fallback_mentions":
            raise ValueError("bad")  # only the push fallback's own message fails to build
        return real_builder(*args, **kwargs)

    monkeypatch.setattr(main, "_text_message_with_mentions", build)

    assert say() is False

    line.push_message.assert_not_called()
    assert status() == "completed_no_reply"


@pytest.mark.parametrize("kwargs", [{"allow_push_fallback": False}], ids=["reply-only"])
def test_a_reply_only_flow_with_a_dead_token_closes_the_event(line, kwargs):
    open_event()
    line.reply_message.side_effect = api_error(400, BAD_TOKEN)

    assert say(**kwargs) is False

    line.push_message.assert_not_called()
    assert status() == "completed_no_reply"


def test_a_market_quote_with_a_dead_token_is_not_pushed_and_closes_the_event(line):
    open_event()
    line.reply_message.side_effect = api_error(400, BAD_TOKEN)

    assert say("【市場報價】合成行情") is False
    line.push_message.assert_not_called()
    assert status() == "completed_no_reply"


# ── _reply: settling by the fence's own identity ──────────────────────────────────────────


def test_an_accepted_reply_settles_the_event_even_when_the_registry_forgot_the_token(line):
    """Another run of the same event had already popped the registry entry; the lookup that
    outlives it found the rows, so the same identity must also settle them."""
    open_event()
    main._inbound_reply_by_token.clear()

    assert say() is True

    assert status() == "replied"


def test_a_push_that_succeeds_settles_the_event_without_the_registry(line):
    open_event()
    main._inbound_reply_by_token.clear()
    line.reply_message.side_effect = api_error(400, BAD_TOKEN)

    assert say() is True

    line.push_message.assert_called_once()
    assert status() == "replied"


def test_a_409_settles_the_event_without_the_registry(line):
    open_event()
    main._inbound_reply_by_token.clear()
    line.reply_message.side_effect = api_error(400, BAD_TOKEN)
    line.push_message.side_effect = api_error(409, {"message": "accepted"}, "Conflict")

    assert say() is True
    assert status() == "replied"


def test_a_dead_token_without_a_push_closes_the_event_without_the_registry(line):
    open_event()
    main._inbound_reply_by_token.clear()
    line.reply_message.side_effect = api_error(400, BAD_TOKEN)

    assert say(allow_push_fallback=False) is False

    assert status() == "completed_no_reply"


def test_a_refused_push_closes_the_event_without_the_registry(line):
    open_event()
    main._inbound_reply_by_token.clear()
    line.reply_message.side_effect = api_error(400, BAD_TOKEN)
    line.push_message.side_effect = api_error(400, BAD_PAYLOAD)

    assert say() is False

    assert status() == "completed_no_reply"


def test_the_settled_reply_is_logged_once_with_the_real_latency(line, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    open_event()
    seen_at = main._inbound_reply_by_token[TOKEN][2]
    main._inbound_reply_by_token.clear()
    monkeypatch.setattr(main.time, "time", lambda: seen_at + 7.0)

    assert say() is True

    (line_text,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("reply accepted")]
    assert "handler_to_accept_s=7.00" in line_text


# ── _reply: where the fence does not apply ────────────────────────────────────────────────


def test_a_token_that_is_not_an_inbound_event_is_sent_as_before(line):
    line.reply_message.side_effect = api_error(400, BAD_TOKEN)

    assert say(token="UNREGISTERED") is True  # token dead -> pushed, as it always was

    line.push_message.assert_called_once()
    assert line.push_message.call_args.kwargs["x_line_retry_key"]


def test_rows_closed_silently_do_not_block_a_reply(line):
    open_event()
    memory.mark_inbound_events_completed_no_reply(GROUP, ["M1"])

    assert say("事後補一句") is True
    line.reply_message.assert_called_once()


def test_a_muted_bot_takes_no_fence(line, monkeypatch):
    monkeypatch.setattr(main.settings, "bot_muted", True)
    open_event()

    assert say() is False

    assert status() == "processing"
    line.reply_message.assert_not_called()


def test_a_fence_that_cannot_be_written_stops_the_send(line, monkeypatch):
    open_event()

    def broken(*_a, **_k):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(memory, "claim_inbound_reply", broken)

    assert say() is False

    line.reply_message.assert_not_called()
    line.push_message.assert_not_called()
    assert status() == "processing"  # the lease and the attempts cap decide about a re-run


def test_a_burst_reply_fences_every_message_of_the_batch(line):
    for message_id in ("B1", "B2", "B3"):
        assert memory.begin_inbound_event(GROUP, message_id) == "new"
    main._register_inbound_reply_batch(TOKEN, GROUP, ["B1", "B2", "B3"])

    assert say("整批的回答") is True

    assert [status(m) for m in ("B1", "B2", "B3")] == ["replied"] * 3


def test_a_burst_reply_that_times_out_fences_every_message(line):
    for message_id in ("B1", "B2"):
        memory.begin_inbound_event(GROUP, message_id)
    main._register_inbound_reply_batch(TOKEN, GROUP, ["B1", "B2"])
    line.reply_message.side_effect = TimeoutError("read timed out")

    assert say() is False

    assert [status(m) for m in ("B1", "B2")] == ["reply_sending"] * 2


def test_a_suppressed_reply_takes_no_fence(line):
    open_event()

    assert say("") is False

    assert status() == "processing"
    line.reply_message.assert_not_called()


def _confirmation_states() -> list[str]:
    with memory._lock, memory._conn() as c:
        return [row[0] for row in c.execute("SELECT status FROM reminder_confirmation_outbox").fetchall()]


def test_a_blocked_reply_gives_the_piggybacked_confirmations_back(line):
    """The fence returns before the delivery bookkeeping starts, so the `finally` hands the
    claimed reminder receipts back instead of stranding them as 'sending'."""
    memory.enqueue_reminder_confirmation(GROUP, "src-1", "已新增提醒：合成事項")
    open_event()
    memory.claim_inbound_reply(GROUP, ["M1"])  # an earlier run is already sending

    assert say(include_auxiliary=True) is False

    line.reply_message.assert_not_called()
    assert _confirmation_states() == ["pending"]


def test_an_accepted_reply_still_carries_and_clears_the_confirmations(line):
    memory.enqueue_reminder_confirmation(GROUP, "src-1", "已新增提醒：合成事項")
    open_event()

    assert say(include_auxiliary=True) is True

    assert len(line.reply_message.call_args.args[0].messages) == 2
    assert _confirmation_states() == []


def test_an_ambiguous_failure_gives_the_confirmations_back_too(line):
    memory.enqueue_reminder_confirmation(GROUP, "src-1", "已新增提醒：合成事項")
    open_event()
    line.reply_message.side_effect = TimeoutError("read timed out")

    assert say(include_auxiliary=True) is False

    assert _confirmation_states() == ["pending"]
    assert status() == "reply_sending"


# ── _handle_event: what a redelivery does ─────────────────────────────────────────────────


def _redelivered_event(message_id="M1", token=TOKEN, group=GROUP):
    msg = MagicMock(spec=TextMessageContent)
    msg.id = message_id
    msg.text = "媽媽明天幾點到家，要不要去接"
    msg.mention = None
    msg.quoted_message_id = None
    src = MagicMock(spec=GroupSource)
    src.group_id, src.user_id, src.type = group, "U1", "group"
    evt = MagicMock(spec=MessageEvent)
    evt.message, evt.source, evt.reply_token = msg, src, token
    evt.timestamp = int(time.time() * 1000)
    evt.delivery_context = MagicMock(is_redelivery=True)
    return evt


def _handle(evt):
    handled: list[int] = []
    with (
        patch("main._handle_text_message", side_effect=lambda *a, **k: handled.append(1)),
        patch("main.memory.log_raw_message"),
        patch("main._observe_parent_health"),
        patch("main._spawn_piggyback_drain"),
        patch("main.settings.allowed_group_ids_raw", ""),
        patch("main.settings.allowed_group_id", ""),
    ):
        main._handle_event(evt)
    return handled


def test_a_redelivery_of_a_fenced_event_is_skipped(clock):
    memory.begin_inbound_event(GROUP, "M1")
    memory.claim_inbound_reply(GROUP, ["M1"])
    clock["t"] += 400  # long past any lease

    assert _handle(_redelivered_event()) == []
    assert status() == "reply_sending"


def test_a_redelivery_of_a_restart_orphan_runs_at_once(monkeypatch, clock):
    monkeypatch.setattr(memory, "_INSTANCE_ID", "instance-A")
    memory.begin_inbound_event(GROUP, "M1")
    monkeypatch.setattr(memory, "_INSTANCE_ID", "instance-B")
    clock["t"] += 3

    assert _handle(_redelivered_event()) == [1]


def test_a_redelivery_inside_the_lease_is_skipped(clock):
    memory.begin_inbound_event(GROUP, "M1")
    clock["t"] += 62

    assert _handle(_redelivered_event()) == []


def test_a_redelivery_of_a_message_the_burst_buffer_holds_is_skipped_and_leaves_the_lease_alone(clock):
    evt = _redelivered_event("OWNED-1")
    with burst_filter._lock:
        burst_filter._pending[GROUP] = [("OWNED-1", "媽媽明天幾點到家，要不要去接", "U1", time.time())]
    try:
        assert _handle(evt) == []
        assert status("OWNED-1") is None  # begin_inbound_event was never reached
    finally:
        with burst_filter._lock:
            burst_filter._pending.clear()
    assert _handle(evt) == [1]  # no longer held: handled (a new row, 'new')


# ── invariants the post-implementation review (2026-10-25) found unpinned ─────────────────


@pytest.fixture()
def fresh_registries(monkeypatch):
    """Both token registries private to the test (`line` resets only one of them)."""
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main, "_inbound_ids_by_token", {})


def _updated_at(message_id, group=GROUP):
    with memory._lock, memory._conn() as c:
        return c.execute(
            "SELECT updated_at FROM inbound_events WHERE group_id = ? AND message_id = ?",
            (group, message_id),
        ).fetchone()[0]


def test_the_registry_outlives_the_longest_processing_lease(fresh_registries, clock):
    """A media handler may run 300 s before it reaches `_reply`.  If its token had been
    forgotten, the claim would say `untracked` and the reply (and its push fallback) would go
    out unfenced -- the 10/20 duplicate shape.  Entries are pruned only when another token is
    registered, so the test registers one after the clock moved."""
    assert main._INBOUND_REPLY_REGISTRY_TTL_SECONDS > memory._INBOUND_MEDIA_PROCESSING_LEASE_SECONDS
    open_event("SLOW", "TOKEN-SLOW")
    clock["t"] += 400  # past the media lease, inside the registry's time to live
    main._register_inbound_reply_token("TOKEN-OTHER", GROUP, "OTHER")  # runs the pruning

    outcome, fence = main._claim_inbound_send("TOKEN-SLOW")

    assert outcome == "claimed"
    assert fence.message_ids == ("SLOW",)


def test_the_registry_forgets_a_token_after_its_time_to_live(fresh_registries, clock):
    open_event("OLD", "TOKEN-OLD")
    clock["t"] += main._INBOUND_REPLY_REGISTRY_TTL_SECONDS + 1
    main._register_inbound_reply_token("TOKEN-NEW", GROUP, "NEW")  # runs the pruning

    assert "TOKEN-OLD" not in main._inbound_reply_by_token
    assert "TOKEN-OLD" not in main._inbound_ids_by_token
    assert main._claim_inbound_send("TOKEN-OLD") == ("untracked", None)


def test_the_kept_token_registry_is_bounded(fresh_registries):
    for i in range(main._INBOUND_IDS_BY_TOKEN_MAX + 1):
        main._register_inbound_reply_token(f"T{i}", GROUP, f"M{i}")

    assert len(main._inbound_ids_by_token) == main._INBOUND_IDS_BY_TOKEN_MAX
    assert "T0" not in main._inbound_ids_by_token  # the oldest went first
    assert f"T{main._INBOUND_IDS_BY_TOKEN_MAX}" in main._inbound_ids_by_token


def test_the_claim_refreshes_updated_at_so_a_slow_handler_is_not_cut_off_mid_send(clock):
    """The restart wait only counts rows younger than 90 s: a handler that spent 100 s on its
    models must look fresh once it is sending, or a restart could cut the send off."""
    memory.begin_inbound_event(GROUP, "a")
    clock["t"] += 100

    assert memory.claim_inbound_reply(GROUP, ["a"]) == ("claimed", False)

    assert _updated_at("a") == int(clock["t"])


def test_a_batch_is_a_lease_retry_when_any_of_its_messages_is_a_retry(clock):
    """One burst with a first start and a retried message must not be allowed to push."""
    memory.begin_inbound_event(GROUP, "a")
    memory.begin_inbound_event(GROUP, "b")
    clock["t"] += 200
    assert memory.begin_inbound_event(GROUP, "b") == "retry"  # attempts: a=1, b=2

    assert memory.claim_inbound_reply(GROUP, ["a", "b"]) == ("claimed", True)


# ── the request is built before the fence is taken (review GP1 #2) ────────────────────────


@pytest.mark.parametrize("broken", ["request", "config"])
def test_a_failure_before_any_request_leaves_no_fence(line, monkeypatch, broken):
    open_event()

    def boom(*_a, **_k):
        raise ValueError("malformed rider")

    monkeypatch.setattr(main, "ReplyMessageRequest" if broken == "request" else "_get_line_config", boom)

    assert say() is False

    assert status() == "processing"  # not reply_sending: nothing left for LINE, the lease may re-run it
    line.reply_message.assert_not_called()
    line.push_message.assert_not_called()


def test_a_failure_before_any_request_lets_a_later_run_answer(line, monkeypatch):
    open_event()
    real_request = main.ReplyMessageRequest
    monkeypatch.setattr(main, "ReplyMessageRequest", MagicMock(side_effect=ValueError("malformed rider")))
    assert say() is False

    monkeypatch.setattr(main, "ReplyMessageRequest", real_request)
    main._register_inbound_reply_token(TOKEN, GROUP, "M1")
    assert say("第二次") is True
    assert status() == "replied"


# ── the claim is tried more than once (review GP1 #4) ─────────────────────────────────────


def test_a_locked_database_on_the_first_claim_does_not_drop_the_reply(line, monkeypatch):
    import sqlite3

    open_event()
    real_claim = memory.claim_inbound_reply
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(memory, "claim_inbound_reply", flaky)

    assert say() is True

    assert len(calls) == 2
    line.reply_message.assert_called_once()
    assert status() == "replied"


def test_the_claim_gives_up_after_three_attempts(line, monkeypatch):
    open_event()
    calls = []

    def broken(*_a, **_k):
        calls.append(1)
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(memory, "claim_inbound_reply", broken)

    assert say() is False

    assert len(calls) == main._INBOUND_FENCE_CLAIM_ATTEMPTS == 3
    line.reply_message.assert_not_called()
    line.push_message.assert_not_called()
    assert status() == "processing"


# ── a rejected degraded reply must not overwrite the fence (review GP1 #8) ────────────────

DEGRADED = "這個圖片我這次沒分析成功，請稍後再傳一次🙏"  # one of Andrew's rejected receipts


@pytest.fixture()
def rejected_text(monkeypatch):
    """The receipt is judged before the quota footer is appended; the status filter judges it
    afterwards.  With a footer the filter no longer sees the exact shape, the receipt is sent
    and `user_rejected_primary` is still set -- the only way such a text reaches the fence."""
    monkeypatch.setattr(main, "_get_quota_footer", lambda: "\n— 小提醒")
    assert main._is_user_rejected_degraded_outbound(DEGRADED)
    return DEGRADED


def test_an_ambiguous_send_of_a_rejected_text_keeps_the_fence(line, rejected_text):
    open_event()
    line.reply_message.side_effect = TimeoutError("read timed out")

    assert say(rejected_text) is False

    line.reply_message.assert_called_once()  # it did reach LINE: the fence was taken first
    assert status() == "reply_sending"  # not completed_no_reply
    assert memory.claim_inbound_reply(GROUP, ["M1"]) == ("blocked", False)  # a late second run is stopped


def test_a_refused_send_of_a_rejected_text_still_closes_the_event(line, rejected_text):
    open_event()
    line.reply_message.side_effect = api_error(400, BAD_PAYLOAD)

    assert say(rejected_text) is False

    line.reply_message.assert_called_once()
    assert status() == "completed_no_reply"  # nothing was sent and a re-run would only repeat it
