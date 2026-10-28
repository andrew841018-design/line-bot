"""B7.1 (LINE bot fixes batch 2, 2026-10-24): the exit drain.  Synthetic data only.

The burst state lives in memory.  A restart used to lose what sat in the 8 s debounce window or in a
flush thread: unanswered, unremembered, the inbound event stuck in `processing` until the monitor's
reply-gap alert.  `burst_filter.drain_for_shutdown` -- called first in the lifespan exit, on a
daemon thread the exit stops waiting for after a time limit, never fatal -- writes queued batches to
the conversation memory (`[burst]` turn) and closes them (`completed_no_reply`), gives running
flushes a few seconds (looking at the queue again meanwhile: a batch a flush puts back is drained
too), and closes what is still running the same way.  "Remembered and closed, not answered."

Review R2 (2026-10-28): only events still being handled are closed -- a `reply_sending` fence or a
`replied` event is never overwritten and its batch is not remembered as unanswered; no wall-clock
bounds on a loaded machine (the drain's own ``waited_s`` and events instead).

(The ordering "SIGTERM while a request is in flight -> the request finishes, then the lifespan exit
runs" is a uvicorn property; it was checked once against a throwaway uvicorn on a local port, see the
batch report, not here.)
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

import burst_filter
import main
import memory
import stock_picks

GROUP = "GRP-DRAIN"
OTHER = "GRP-DRAIN-2"


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
            burst_filter._inflight_batches.clear()

    wipe()
    old_callback = burst_filter._on_flush
    yield
    burst_filter._on_flush = old_callback
    wipe()


def _queue(group, *pairs, user="U1"):
    for message_id, text in pairs:
        assert memory.begin_inbound_event(group, message_id) == "new"
        burst_filter.add_to_burst(group, message_id, text, user, "tok-" + message_id)


def _bursts(group):
    return [text for role, text in memory.get_context(group) if role == "user" and text.startswith("[burst]")]


def _wait(predicate, seconds=3.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ── queued batches ──────────────────────────────────────────────────────────────────────────


def test_queued_batches_are_remembered_and_closed():
    _queue(GROUP, ("Q1", "媽媽明天幾點到家，要不要去接"), ("Q2", "順便把上次的相簿帶過去"))
    _queue(OTHER, ("Q3", "週末要不要一起吃飯"))

    result = burst_filter.drain_for_shutdown(0.5)

    assert result["queued_messages"] == 3 and result["inflight_batches"] == 0
    assert burst_filter._pending == {} and burst_filter._timers == {}  # no timer can fire after it
    for group, message_id in ((GROUP, "Q1"), (GROUP, "Q2"), (OTHER, "Q3")):
        assert memory.get_inbound_event_status(group, message_id) == "completed_no_reply"
    first = _bursts(GROUP)
    assert len(first) == 1 and "媽媽明天幾點到家" in first[0] and "順便把上次的相簿帶過去" in first[0]
    assert len(_bursts(OTHER)) == 1 and "週末要不要一起吃飯" in _bursts(OTHER)[0]


def test_chit_chat_is_closed_but_not_remembered():
    _queue(GROUP, ("C1", "哈哈"))

    burst_filter.drain_for_shutdown(0.2)

    assert memory.get_inbound_event_status(GROUP, "C1") == "completed_no_reply"
    assert _bursts(GROUP) == []  # the same rule a cancelled burst follows


def test_a_batch_waiting_for_its_one_minute_follow_up_is_drained_too():
    _queue(GROUP, ("W1", "對方還沒說完的一句話"))
    with burst_filter._lock:
        burst_filter._waiting_groups.add(GROUP)

    result = burst_filter.drain_for_shutdown(0.2)

    assert result["queued_messages"] == 1 and burst_filter._waiting_groups == set()
    assert memory.get_inbound_event_status(GROUP, "W1") == "completed_no_reply"


def test_an_empty_drain_does_nothing_and_returns_at_once():
    result = burst_filter.drain_for_shutdown(60.0)

    # (R2 GP2 #6: no wall-clock bound on a loaded machine; ``waited_s`` is the drain's own clock.)
    # Nothing in flight: it does not sit out the 60 seconds.
    assert result["waited_s"] < 30.0
    assert result["queued_messages"] == 0 and result["inflight_batches"] == 0


def test_a_message_that_was_already_answered_is_not_reopened_or_remembered_again():
    _queue(GROUP, ("A1", "已經回過的一句話"))
    memory.mark_inbound_event_replied(GROUP, "A1")

    result = burst_filter.drain_for_shutdown(0.2)

    assert memory.get_inbound_event_status(GROUP, "A1") == "replied"
    assert _bursts(GROUP) == []  # (R2 GP2 #1) the reply's own turn is the memory of it
    assert result["queued_messages"] == 1 and result["left_as_is"] == 1


def test_a_reply_that_is_on_its_way_keeps_its_fence_and_is_not_remembered_as_unanswered():
    """(R2 GP2 #1) `reply_sending` is "a reply is on its way, or may have been delivered": the drain
    must neither overwrite it with `completed_no_reply` nor write a `[burst]` turn for an answered
    batch."""
    _queue(GROUP, ("Y1", "回覆正在送出的訊息"), ("Y2", "同一批的第二句"))
    assert memory.claim_inbound_reply(GROUP, ["Y1", "Y2"]) == ("claimed", False)

    result = burst_filter.drain_for_shutdown(0.2)

    assert memory.get_inbound_event_status(GROUP, "Y1") == "reply_sending"
    assert memory.get_inbound_event_status(GROUP, "Y2") == "reply_sending"
    assert _bursts(GROUP) == [] and result["left_as_is"] == 2
    assert memory.claim_inbound_reply(GROUP, ["Y1"]) == ("blocked", False)  # a late run still finds the fence
    assert burst_filter._pending == {} and burst_filter._timers == {}  # (it is still taken out of the queue)


def test_a_batch_with_one_open_event_is_closed_and_remembered_once():
    _queue(GROUP, ("H1", "已經在送出的那句"), ("H2", "還沒處理的那句"))
    assert memory.claim_inbound_reply(GROUP, ["H1"]) == ("claimed", False)

    result = burst_filter.drain_for_shutdown(0.2)

    assert memory.get_inbound_event_status(GROUP, "H1") == "reply_sending"
    assert memory.get_inbound_event_status(GROUP, "H2") == "completed_no_reply"
    assert len(_bursts(GROUP)) == 1 and result["left_as_is"] == 1


# ── flushes already running ─────────────────────────────────────────────────────────────────


def _slow_flush(monkeypatch, seconds):
    started, release = threading.Event(), threading.Event()

    def callback(group_id, combined, token, ids):
        started.set()
        release.wait(seconds)

    burst_filter._on_flush = callback
    monkeypatch.setattr(burst_filter, "SHORT_WINDOW_SECONDS", 0.05)
    monkeypatch.setattr(burst_filter, "_heuristic_decision", lambda _t: "respond")
    return started, release


def test_a_flush_that_finishes_inside_the_wait_is_left_alone(monkeypatch):
    started, release = _slow_flush(monkeypatch, 5)
    _queue(GROUP, ("F1", "還在處理中的訊息，會在寬限期內處理完"))
    assert started.wait(3)
    threading.Timer(0.2, release.set).start()

    result = burst_filter.drain_for_shutdown(30.0)  # generous: it returns as soon as the flush ends

    assert result["inflight_batches"] == 0 and result["inflight_messages_closed"] == 0
    assert memory.get_inbound_event_status(GROUP, "F1") == "processing"  # the flush owns its outcome
    assert _bursts(GROUP) == []


def test_a_flush_that_does_not_finish_is_remembered_and_closed_after_the_wait(monkeypatch):
    started, release = _slow_flush(monkeypatch, 10)
    _queue(GROUP, ("S1", "處理很久的訊息，重啟時還沒好"), ("S2", "同一批的第二句"))
    assert started.wait(3)

    try:
        result = burst_filter.drain_for_shutdown(0.3)
    finally:
        release.set()

    # waited its time, not the 10 seconds the flush would have taken (the drain's own clock, so a
    # loaded machine cannot fail it)
    assert 0.2 <= result["waited_s"] < 5.0
    assert result["inflight_batches"] == 1 and result["inflight_messages_closed"] == 2
    assert memory.get_inbound_event_status(GROUP, "S1") == "completed_no_reply"
    assert memory.get_inbound_event_status(GROUP, "S2") == "completed_no_reply"
    burst = _bursts(GROUP)
    assert len(burst) == 1 and "處理很久的訊息" in burst[0] and "同一批的第二句" in burst[0]
    assert _wait(lambda: burst_filter._inflight == {} and burst_filter._inflight_batches == {})  # tidied up when it ends


def test_a_flush_that_is_sending_its_reply_keeps_its_fence_and_is_not_remembered_as_unanswered(monkeypatch):
    """(R2 GP2 #1) what `_reply` does right before `reply_message`, then the restart arrives."""
    claimed: list = []
    started, release = threading.Event(), threading.Event()

    def callback(group_id, combined, token, ids):
        claimed.append(memory.claim_inbound_reply(group_id, ids))
        started.set()
        release.wait(10)

    burst_filter._on_flush = callback
    monkeypatch.setattr(burst_filter, "SHORT_WINDOW_SECONDS", 0.05)
    monkeypatch.setattr(burst_filter, "_heuristic_decision", lambda _t: "respond")
    _queue(GROUP, ("X1", "回覆正在送出的訊息"))
    assert started.wait(5)

    try:
        result = burst_filter.drain_for_shutdown(0.2)
    finally:
        release.set()

    assert claimed == [("claimed", False)]
    assert result["inflight_batches"] == 1 and result["inflight_messages_closed"] == 0 and result["left_as_is"] == 1
    assert memory.get_inbound_event_status(GROUP, "X1") == "reply_sending"
    assert _bursts(GROUP) == []
    assert memory.claim_inbound_reply(GROUP, ["X1"]) == ("blocked", False)


def _arm_batch_that_comes_back(monkeypatch, how):
    """A flush that is running when the drain starts and, once ``proceed`` is set, puts its batch
    back into the queue ("retry": it failed before delivery; "wait": the classifier says the
    sender is not done).  The queue window is long, so only the drain can take the batch out."""
    started, proceed = threading.Event(), threading.Event()

    def gate():
        started.set()
        proceed.wait(30)
        monkeypatch.setattr(burst_filter, "SHORT_WINDOW_SECONDS", 60.0)
        monkeypatch.setattr(burst_filter, "BURST_WINDOW_SECONDS", 60.0)

    monkeypatch.setattr(burst_filter, "SHORT_WINDOW_SECONDS", 0.05)
    if how == "retry":
        def callback(_group, _combined, _token, _ids):
            gate()
            raise burst_filter.RetryableBurstError("database busy before delivery")

        burst_filter._on_flush = callback
        monkeypatch.setattr(burst_filter, "_heuristic_decision", lambda _t: "respond")
    else:
        monkeypatch.setattr(burst_filter, "_heuristic_decision", lambda _t: None)

        def classify(_text, _rules):
            gate()
            return "wait", "sender is still typing"

        monkeypatch.setattr(burst_filter.gemini_client, "classify_burst", classify)
    return started, proceed


@pytest.mark.parametrize("how", ["retry", "wait"])
def test_a_batch_that_is_put_back_while_the_drain_waits_is_drained_too(monkeypatch, how):
    """(R2 GP1 #2) the queue used to be looked at once, before the wait: a flush that failed before
    delivery (or decided to wait) while the drain waited put its batch back, and it died with the
    process, unremembered, its event stuck in `processing`."""
    started, proceed = _arm_batch_that_comes_back(monkeypatch, how)
    _queue(GROUP, ("B1", "放回佇列的訊息，不能跟著程序一起消失"))
    assert started.wait(5)
    threading.Timer(0.3, proceed.set).start()

    result = burst_filter.drain_for_shutdown(30.0)  # generous: it returns as soon as nothing is left

    assert burst_filter._pending == {} and burst_filter._timers == {} and burst_filter._waiting_groups == set()
    assert memory.get_inbound_event_status(GROUP, "B1") == "completed_no_reply"
    burst = _bursts(GROUP)
    assert len(burst) == 1 and "放回佇列的訊息" in burst[0]
    assert result["queued_messages"] == 1 and result["inflight_messages_closed"] == 0
    assert _wait(lambda: burst_filter._inflight == {} and burst_filter._inflight_batches == {})


def test_a_batch_in_the_queue_and_in_the_in_flight_table_is_remembered_once():
    """A flush that ends just before the drain closes what is left has put its batch back into the
    queue and has not yet left the in-flight table: the batch is found twice.  Closing it the second
    time changes no row, so it is not remembered twice."""
    _queue(GROUP, ("D1", "同時在佇列和飛行中表裡的訊息"))
    with burst_filter._lock:
        burst_filter._begin_inflight_locked(GROUP, list(burst_filter._pending[GROUP]))

    result = burst_filter.drain_for_shutdown(0.0)  # no time to wait at all

    assert memory.get_inbound_event_status(GROUP, "D1") == "completed_no_reply"
    assert len(_bursts(GROUP)) == 1
    assert (result["queued_messages"], result["inflight_batches"], result["inflight_messages_closed"]) == (1, 1, 0)


def test_queued_and_running_batches_are_both_handled(monkeypatch):
    started, release = _slow_flush(monkeypatch, 10)
    _queue(GROUP, ("R1", "正在處理的訊息"))
    assert started.wait(3)
    _queue(OTHER, ("R2", "還在排隊的另一個群組的訊息"))

    try:
        result = burst_filter.drain_for_shutdown(0.2)
    finally:
        release.set()

    assert (result["queued_messages"], result["inflight_batches"]) == (1, 1)
    assert memory.get_inbound_event_status(GROUP, "R1") == "completed_no_reply"
    assert memory.get_inbound_event_status(OTHER, "R2") == "completed_no_reply"


def test_the_in_flight_table_and_the_batch_table_stay_in_step(monkeypatch):
    started, release = _slow_flush(monkeypatch, 5)
    _queue(GROUP, ("T1", "追蹤用的訊息"))
    assert started.wait(3)
    with burst_filter._lock:
        assert set(burst_filter._inflight) == set(burst_filter._inflight_batches) != set()
    release.set()
    assert _wait(lambda: not burst_filter._inflight and not burst_filter._inflight_batches)


def test_the_batch_table_is_trimmed_with_the_in_flight_table():
    with burst_filter._lock:
        for index in range(burst_filter._INFLIGHT_MAX + 6):
            burst_filter._begin_inflight_locked(GROUP, [(f"m{index}", "t", "U1", 0.0)])
        assert len(burst_filter._inflight) == len(burst_filter._inflight_batches) == burst_filter._INFLIGHT_MAX
        assert set(burst_filter._inflight) == set(burst_filter._inflight_batches)


# ── never fatal, counts only ─────────────────────────────────────────────────────────────────


def test_a_failing_database_never_stops_the_drain(monkeypatch, caplog):
    _queue(GROUP, ("E1", "資料庫壞掉時的訊息"))
    remembered: list = []

    def broken(*_args, **_kwargs):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(memory, "mark_active_inbound_events_completed_no_reply", broken)
    monkeypatch.setattr(memory, "append_turn", lambda *args, **kwargs: remembered.append(args))
    caplog.set_level(logging.INFO)

    result = burst_filter.drain_for_shutdown(0.1)

    assert result["queued_messages"] == 1  # it carried on
    assert remembered == []  # (whether the event was still open is unknown: nothing is written on a guess)
    drain_lines = [r.getMessage() for r in caplog.records if "burst drain" in r.getMessage()]
    assert any("close failed error_type=RuntimeError" in line for line in drain_lines)
    assert all("資料庫壞掉時的訊息" not in line for line in drain_lines)


def test_the_log_carries_counts_only(monkeypatch, caplog):
    started, release = _slow_flush(monkeypatch, 10)
    _queue(GROUP, ("L1", "不該出現在 log 的內容甲"))
    assert started.wait(3)
    _queue(OTHER, ("L2", "不該出現在 log 的內容乙"))
    caplog.set_level(logging.INFO)

    try:
        burst_filter.drain_for_shutdown(0.1)
    finally:
        release.set()

    drain_lines = [r.getMessage() for r in caplog.records if "burst drain" in r.getMessage()]
    assert len(drain_lines) == 1 and "queued_messages" in drain_lines[0]
    # (the flush path's own "burst respond by heuristic" line is older and not part of the drain)
    assert "不該出現在 log 的內容" not in drain_lines[0] and GROUP not in drain_lines[0] and OTHER not in drain_lines[0]


# ── the lifespan exit ────────────────────────────────────────────────────────────────────────


def _lifespan_stubs(monkeypatch, order):
    monkeypatch.delenv("JOBS_ROUTES_ENABLED", raising=False)
    monkeypatch.setattr(main, "_configure_local_text_llm_runtime", lambda: None)
    monkeypatch.setattr(main, "_start_local_vision_worker", lambda: None)
    fake_vision = types.ModuleType("vision_llm")
    fake_vision.shutdown_background_worker = lambda: order.append("vision_stop")
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)
    fake_pending = types.ModuleType("pending_store")
    fake_pending.harden_media_permissions = lambda: 0
    fake_pending.sweep_orphan_media = lambda: 0
    fake_pending.sweep_delivery_lock_files = lambda: 0
    monkeypatch.setitem(sys.modules, "pending_store", fake_pending)
    monkeypatch.setattr(main, "_process_pending_on_startup", lambda: None)
    monkeypatch.setattr(main, "_init_on_startup", lambda: order.append("init"))
    monkeypatch.setattr(stock_picks, "start_background", lambda: order.append("stock_start"))
    monkeypatch.setattr(stock_picks, "stop_background", lambda timeout=2.0: order.append("stock_stop"))


def _run_lifespan():
    app = SimpleNamespace(state=SimpleNamespace())

    async def run():
        async with main._app_lifespan(app):
            pass

    asyncio.run(run())


def test_the_drain_is_the_first_thing_the_lifespan_exit_does(monkeypatch):
    order: list = []
    _lifespan_stubs(monkeypatch, order)
    monkeypatch.setattr(burst_filter, "drain_for_shutdown", lambda seconds: order.append(("drain", seconds)))

    _run_lifespan()

    exit_steps = order[order.index("stock_start") + 1 :]
    assert exit_steps[0] == ("drain", main._EXIT_DRAIN_SECONDS) and "stock_stop" in exit_steps and "vision_stop" in exit_steps
    # (R2 GP2 #7) launchd ends the process about 20 s after SIGTERM; the drain shares that with two
    # 2 s thread stops and the vision worker
    assert main._EXIT_DRAIN_SECONDS + main._EXIT_DRAIN_MARGIN_SECONDS <= 10.0


def test_a_drain_that_fails_never_blocks_the_exit(monkeypatch, caplog):
    order: list = []
    _lifespan_stubs(monkeypatch, order)

    def broken(seconds):
        raise RuntimeError("drain exploded")

    monkeypatch.setattr(burst_filter, "drain_for_shutdown", broken)
    caplog.set_level(logging.WARNING)

    _run_lifespan()

    assert "stock_stop" in order and "vision_stop" in order
    assert "burst drain on exit failed error_type=RuntimeError" in caplog.text


def test_a_drain_that_hangs_runs_on_a_daemon_thread_and_does_not_hold_the_exit_up(monkeypatch, caplog):
    """(R2 GP1 #5, GP2 #7) `wait_for(to_thread(...))` only stopped waiting: the work stayed on an
    executor thread, which `asyncio.run` and the interpreter join when they exit, so a drain stuck on
    the database delayed the whole exit until launchd's kill.  Here the drain blocks on an event the
    test sets only after the lifespan has returned: if the exit had waited for it, it would return
    after the drain finished (here: after 20 s) and ``still_running`` would be False."""
    order: list = []
    _lifespan_stubs(monkeypatch, order)
    monkeypatch.setattr(main, "_EXIT_DRAIN_SECONDS", 0.05)
    monkeypatch.setattr(main, "_EXIT_DRAIN_MARGIN_SECONDS", 0.1)
    release, finished, seen = threading.Event(), threading.Event(), {}

    def hangs(seconds):
        seen["thread"] = threading.current_thread()
        release.wait(20)
        finished.set()

    monkeypatch.setattr(burst_filter, "drain_for_shutdown", hangs)
    caplog.set_level(logging.WARNING)

    try:
        _run_lifespan()
        still_running = not finished.is_set()
    finally:
        release.set()
        if "thread" in seen:
            seen["thread"].join(5)

    assert still_running, "the exit waited for the drain to finish"
    assert "stock_stop" in order and "vision_stop" in order  # the exit carried on
    assert "burst drain on exit failed error_type=TimeoutError" in caplog.text
    assert seen["thread"].daemon and seen["thread"].name == "burst-exit-drain"


def test_a_drain_that_finishes_inside_its_time_is_waited_for_and_not_reported(monkeypatch, caplog):
    order: list = []
    _lifespan_stubs(monkeypatch, order)
    done = threading.Event()

    def quick(seconds):
        time.sleep(0.2)
        done.set()

    monkeypatch.setattr(burst_filter, "drain_for_shutdown", quick)
    caplog.set_level(logging.WARNING)

    _run_lifespan()

    assert done.is_set()  # the exit waited for the work it started
    assert "burst drain on exit failed" not in caplog.text
