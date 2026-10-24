"""`burst_filter.owns_message` (LINE bot fixes batch 1, 2026-10-24).  Synthetic data only.

A LINE redelivery of a message this process still holds -- queued in the debounce window,
waiting for the one-minute follow-up, or inside a flush thread -- must be skipped: a second run
would answer it twice.  The registration in `_flush_burst` happens in the same lock hold that
takes the batch out of `_pending`, so a message is always either queued or in flight.
"""

from __future__ import annotations

import threading
import time

import pytest

import burst_filter
import memory

GROUP = "GRP-OWNS"
OTHER = "GRP-OTHER"


@pytest.fixture(autouse=True)
def _clean_burst_state():
    def wipe():
        with burst_filter._lock:
            for timer in list(burst_filter._timers.values()):
                timer.cancel()
            burst_filter._pending.clear()
            burst_filter._timers.clear()
            burst_filter._last_reply_tokens.clear()
            burst_filter._waiting_groups.clear()
            burst_filter._inflight.clear()

    wipe()
    old_callback = burst_filter._on_flush
    yield
    burst_filter._on_flush = old_callback
    wipe()


def _queue(*pairs):
    for message_id, text in pairs:
        assert memory.begin_inbound_event(GROUP, message_id) == "new"
        burst_filter.add_to_burst(GROUP, message_id, text, "U1", "tok-" + message_id)


def test_a_queued_message_is_owned_and_strangers_are_not():
    _queue(("Q1", "媽媽明天幾點到家，要不要去接"))

    assert burst_filter.owns_message(GROUP, "Q1") is True
    assert burst_filter.owns_message(GROUP, "other-message") is False
    assert burst_filter.owns_message(OTHER, "Q1") is False
    assert burst_filter.owns_message(GROUP, "") is False
    assert burst_filter.owns_message("", "Q1") is False


def test_a_cancelled_burst_is_no_longer_owned():
    _queue(("Q1", "媽媽明天幾點到家，要不要去接"), ("Q2", "順便把上次的相簿帶過去"))

    burst_filter.cancel_burst(GROUP)

    assert not burst_filter.owns_message(GROUP, "Q1")
    assert not burst_filter.owns_message(GROUP, "Q2")


def test_a_flush_thread_owns_its_batch_until_it_is_done(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow_callback(group_id, combined, token, ids):
        started.set()
        release.wait(5)

    burst_filter._on_flush = slow_callback
    monkeypatch.setattr(burst_filter, "SHORT_WINDOW_SECONDS", 0.05)
    monkeypatch.setattr(burst_filter, "_heuristic_decision", lambda _t: "respond")
    _queue(("F1", "媽媽明天幾點到家，要不要去接"), ("F2", "順便把上次的相簿帶過去"))
    assert started.wait(3)

    assert burst_filter._pending == {}  # the batch left the queue...
    assert burst_filter.owns_message(GROUP, "F1") and burst_filter.owns_message(GROUP, "F2")  # ...but is held

    release.set()
    deadline = time.time() + 3
    while burst_filter.owns_message(GROUP, "F1") and time.time() < deadline:
        time.sleep(0.01)
    assert not burst_filter.owns_message(GROUP, "F1")
    assert burst_filter._inflight == {}


def test_there_is_never_a_moment_when_the_message_is_neither_queued_nor_in_flight(monkeypatch):
    """The window between 'taken out of _pending' and 'registered as in flight' must not exist:
    poll from before the timer fires until well after the callback returns."""
    done = threading.Event()

    def callback(group_id, combined, token, ids):
        time.sleep(0.3)
        done.set()

    burst_filter._on_flush = callback
    monkeypatch.setattr(burst_filter, "SHORT_WINDOW_SECONDS", 0.05)
    monkeypatch.setattr(burst_filter, "_heuristic_decision", lambda _t: "respond")
    _queue(("P1", "媽媽明天幾點到家，要不要去接"))

    misses = 0
    polls = 0
    while not done.is_set():
        polls += 1
        # (A miss after the callback has signalled is the flush thread tidying up, not a gap.)
        if not burst_filter.owns_message(GROUP, "P1") and not done.is_set():
            misses += 1
    assert polls > 100
    assert misses == 0


def test_the_batch_is_registered_in_the_same_lock_hold_that_takes_it_from_the_queue(monkeypatch):
    """Deterministic version of the test above: look at the state every time the lock is let
    go.  Whenever the message is not queued any more it must already be in flight."""

    class SpyLock:
        def __init__(self):
            self._lock = threading.RLock()
            self.snapshots: list[tuple[bool, bool]] = []

        def __enter__(self):
            self._lock.acquire()
            return self

        def __exit__(self, *_exc):
            queued = any(item[0] == "S1" for item in burst_filter._pending.get(GROUP, ()))
            in_flight = any("S1" in ids for _gid, ids in burst_filter._inflight.values())
            self.snapshots.append((queued, in_flight))
            self._lock.release()

    spy = SpyLock()
    monkeypatch.setattr(burst_filter, "_lock", spy)
    end = {}

    def callback(group_id, combined, token, ids):
        end["index"] = len(spy.snapshots)

    burst_filter._on_flush = callback
    monkeypatch.setattr(burst_filter, "SHORT_WINDOW_SECONDS", 0.05)
    monkeypatch.setattr(burst_filter, "_heuristic_decision", lambda _t: "respond")
    _queue(("S1", "媽媽明天幾點到家，要不要去接"))
    deadline = time.time() + 3
    while "index" not in end and time.time() < deadline:
        time.sleep(0.01)

    assert "index" in end
    seen = spy.snapshots[: end["index"]]
    assert (True, False) in seen and (False, True) in seen  # it did change hands
    assert (False, False) not in seen  # ...without a gap


def test_a_flush_that_raises_still_releases_the_message(monkeypatch):
    def broken(group_id, combined, token, ids):
        raise RuntimeError("model exploded")

    burst_filter._on_flush = broken
    monkeypatch.setattr(burst_filter, "SHORT_WINDOW_SECONDS", 0.05)
    monkeypatch.setattr(burst_filter, "_heuristic_decision", lambda _t: "respond")
    _queue(("E1", "媽媽明天幾點到家，要不要去接"))

    deadline = time.time() + 3
    while time.time() < deadline and (burst_filter._pending or burst_filter._timers):
        time.sleep(0.01)
    deadline = time.time() + 3
    while time.time() < deadline and burst_filter._inflight:
        time.sleep(0.01)

    assert burst_filter._inflight == {}
    assert not burst_filter.owns_message(GROUP, "E1")


def test_a_batch_without_a_reply_token_is_not_registered():
    with burst_filter._lock:
        burst_filter._pending[GROUP] = [("N1", "沒有 token 的一則", "U1", time.time())]
        burst_filter._generations[GROUP] = 1
    burst_filter._flush_burst(GROUP, False, 1)

    assert burst_filter._inflight == {}


def test_the_in_flight_registry_is_bounded():
    with burst_filter._lock:
        for index in range(burst_filter._INFLIGHT_MAX * 3):
            burst_filter._begin_inflight_locked(GROUP, [(f"m{index}", "t", "U1", 0.0)])
        size = len(burst_filter._inflight)
    assert size == burst_filter._INFLIGHT_MAX
    assert burst_filter.owns_message(GROUP, f"m{burst_filter._INFLIGHT_MAX * 3 - 1}")
    assert not burst_filter.owns_message(GROUP, "m0")  # the oldest entries went first
