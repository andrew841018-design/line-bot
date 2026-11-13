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
from concurrent.futures import ThreadPoolExecutor

import pytest


def cpu_seconds(check, text, deadline=60):
    """CPU time this thread spends in ``check(text)``; the test fails if it still runs after ``deadline`` wall seconds.

    Waiting for a CPU, sleeping and other threads do not count, and ``timeit`` pauses GC (a collection of the full
    run's heap is not ``check``'s cost).  2026-11-06, load about 250: a 30 ms helper measured 0.14 s of wall time, and
    in a full run a call hit the old 5 s deadline.  Real slowness burns CPU on this thread, so the thresholds still
    catch it.  The deadline only has to stop a hang, and one false failure costs a rerun of more than three hours, so
    it is generous.  A wait, or work moved to another thread, is invisible here, so use this on pure computation only
    (no I/O, locks or pools).  Where the code can reach a fetch or a pool, make the test fail on it
    (``forbid_other_threads`` below, ``_no_fetch`` in test_stock_chitchat_quote.py), and with pytest.fail: the code's
    own ``except Exception`` eats an AssertionError.

    ``text`` can be any object.  This takes over ITIMER_REAL: an outer timer does not survive it.
    """
    label = str(text)[:12]  # not in the handler: an Exception there could be eaten, and the timer fires once

    def expire(*_):
        pytest.fail(f"still running after {deadline} s: {label!r}")

    previous = signal.signal(signal.SIGALRM, expire)
    try:
        signal.setitimer(signal.ITIMER_REAL, deadline)
        return timeit.timeit(lambda: check(text), timer=time.thread_time, number=1)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def forbid_other_threads(monkeypatch):
    """Fail when the main thread starts a thread or submits to a pool: ``cpu_seconds`` would read that work as free.

    For timed code with threads or pools within reach.  Moving a slow check to a pool with a timeout looks like a fix
    and is none: a running regex keeps the other threads waiting, and with ``\\d+\\s*%`` on 20000 digits
    ``result(timeout=0.05)`` came back after 3.1 s (2026-11-07).  Threads left over from earlier tests pass: they are
    not the timed code.
    """

    def refuse(real):
        def guard(self, *args, **kwargs):
            if threading.current_thread() is threading.main_thread():
                pytest.fail("the timed code left this thread (thread or thread pool): thread_time would not see it")
            return real(self, *args, **kwargs)

        return guard

    monkeypatch.setattr(threading.Thread, "start", refuse(threading.Thread.start))
    monkeypatch.setattr(ThreadPoolExecutor, "submit", refuse(ThreadPoolExecutor.submit))


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


def test_a_hang_fails_the_same_way_whatever_the_input_is():
    """Some tests pass a number or a dict.  An ``Exception`` from the handler could be eaten by the timed code, and the
    timer fires once, so the hang would have no deadline left."""
    with pytest.raises(pytest.fail.Exception):
        cpu_seconds(lambda _: time.sleep(5), 20000, deadline=0.05)


def test_forbid_other_threads_stops_the_main_thread_only(monkeypatch):
    go, ran = threading.Event(), []

    def leftover():
        go.wait(5)
        inner = threading.Thread(target=ran.append, args=("thread",))
        inner.start()
        inner.join()
        with ThreadPoolExecutor(1) as pool:
            pool.submit(ran.append, "pool")

    earlier = threading.Thread(target=leftover)
    earlier.start()
    with ThreadPoolExecutor(1) as warm:
        warm.submit(int).result()  # its worker is up: submit no longer goes through Thread.start
        forbid_other_threads(monkeypatch)
        with pytest.raises(pytest.fail.Exception):
            warm.submit(print)
    with pytest.raises(pytest.fail.Exception):
        threading.Thread(target=print).start()
    go.set()
    earlier.join()
    assert ran == ["thread", "pool"]
