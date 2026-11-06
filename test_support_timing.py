"""CPU-time measurement for the linear-time tests (2026-11-07).  Synthetic data only.

Named `test_*` so that changing it does not restart the bot (`test_support_fake_line.py` explains how).  The tests
below check the measurement itself, so run this file when you change it: the tests that use it will not.
"""

from __future__ import annotations

import gc
import re
import signal
import threading
import time
import timeit

import pytest


def cpu_seconds(check, text, deadline=60):
    """CPU time this thread spends in ``check(text)``; the test fails if it still runs after ``deadline`` wall seconds.

    Waiting for a CPU, sleeping and other threads do not count, and ``timeit`` pauses GC (a collection of the full
    run's heap is not ``check``'s cost).  2026-11-06, load about 250: a 30 ms helper measured 0.14 s of wall time, and
    in a full run a call hit the old 5 s deadline.  Real slowness burns CPU, so the thresholds still catch it.  The
    deadline only has to stop a hang, and one false failure costs a rerun of more than three hours, so it is generous.
    A wait added to the timed code is invisible here, so use this on pure computation only (no I/O, locks or pools).
    It takes over ITIMER_REAL: an outer timer does not survive it.
    """

    def expire(*_):
        pytest.fail(f"still running after {deadline} s: {text[:12]!r}")

    previous = signal.signal(signal.SIGALRM, expire)
    try:
        signal.setitimer(signal.ITIMER_REAL, deadline)
        return timeit.timeit(lambda: check(text), timer=time.thread_time, number=1)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_quadratic_work_counts_but_sleeping_does_not():
    """The measurement itself: backtracking has to show, waiting (queueing for a CPU, sleeping) must not.

    ``\\d+\\s*%`` is quadratic: about 390 ms of CPU on 7000 digits (P core; about 2.6 times that on an E core), against
    the 0.1 s the linear-time tests allow.  If this fails, check first that it is still quadratic.
    """
    quadratic = re.compile(r"\d+\s*%")
    digits = "1" * 7000
    assert min(cpu_seconds(lambda s: list(quadratic.finditer(s)), digits) for _ in range(3)) >= 0.1
    assert cpu_seconds(lambda _: time.sleep(0.2), "x") < 0.1


def test_other_threads_do_not_count():
    """A leftover thread busy during the full run must not be charged to this measurement (``process_time`` would)."""

    def wait_for_another_thread(_):
        def burn():
            end = time.thread_time() + 0.2
            while time.thread_time() < end:
                pass

        worker = threading.Thread(target=burn)
        worker.start()
        worker.join()

    assert cpu_seconds(wait_for_another_thread, "x") < 0.1


def test_gc_pause_and_timer_are_undone_and_a_hang_still_fails():
    seen = []
    handler = signal.getsignal(signal.SIGALRM)
    cpu_seconds(lambda _: seen.append(gc.isenabled()), "x")
    assert seen == [False] and gc.isenabled()
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0) and signal.getsignal(signal.SIGALRM) is handler
    with pytest.raises(pytest.fail.Exception):
        cpu_seconds(lambda _: time.sleep(5), "x", deadline=0.05)
    assert gc.isenabled() and signal.getsignal(signal.SIGALRM) is handler
