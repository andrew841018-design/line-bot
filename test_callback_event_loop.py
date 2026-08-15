import asyncio
import threading
from unittest.mock import MagicMock

import httpx

import main


def test_callback_keeps_event_loop_responsive_during_blocking_handler(monkeypatch):
    event = MagicMock()
    event.source = MagicMock()
    event.source.group_id = "GRP001"
    entered = threading.Event()
    release = threading.Event()

    def blocking_handler(_event):
        entered.set()
        release.wait()

    monkeypatch.setattr(main._parser, "parse", lambda *_args: [event])
    monkeypatch.setattr(main, "_handle_event", blocking_handler)

    async def exercise_callback():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            watchdog_fired = threading.Event()

            def release_by_watchdog():
                watchdog_fired.set()
                release.set()

            watchdog = threading.Timer(1.5, release_by_watchdog)
            watchdog.start()
            callback_task = asyncio.create_task(
                client.post(
                    "/callback",
                    content=b'{"events":[]}',
                    headers={"x-line-signature": "dummy_sig"},
                )
            )
            health_response = None
            try:
                handler_started = await asyncio.to_thread(entered.wait, 1.0)
                health_response = await client.get("/health")
                assert handler_started
                assert not watchdog_fired.is_set()
                assert not callback_task.done()
            finally:
                release.set()
                watchdog.cancel()
                callback_response = await asyncio.wait_for(callback_task, timeout=1)

        assert entered.is_set()
        assert health_response is not None
        assert health_response.status_code == 200
        assert callback_response.status_code == 200

    asyncio.run(exercise_callback())


def test_concurrent_callbacks_do_not_overlap_handlers(monkeypatch):
    active = 0
    max_active = 0
    calls = []
    state_lock = threading.Lock()
    first_entered = threading.Event()
    release_first = threading.Event()

    def blocking_handler(event):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            calls.append(f"{event}:start")
        try:
            if event == "first":
                first_entered.set()
                release_first.wait(timeout=2.0)
        finally:
            with state_lock:
                calls.append(f"{event}:end")
                active -= 1

    monkeypatch.setattr(main._parser, "parse", lambda body, _signature: [body])
    monkeypatch.setattr(main, "_handle_event", blocking_handler)

    async def exercise_callbacks():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            timer = None
            try:
                first_task = asyncio.create_task(
                    client.post(
                        "/callback",
                        content=b"first",
                        headers={"x-line-signature": "dummy_sig"},
                    )
                )
                assert await asyncio.to_thread(first_entered.wait, 1.0)
                timer = threading.Timer(1.0, release_first.set)
                timer.start()
                second_task = asyncio.create_task(
                    client.post(
                        "/callback",
                        content=b"second",
                        headers={"x-line-signature": "dummy_sig"},
                    )
                )
                responses = await asyncio.gather(first_task, second_task)
            finally:
                release_first.set()
                if timer is not None:
                    timer.cancel()

        assert first_entered.is_set()
        assert [response.status_code for response in responses] == [200, 200]

    asyncio.run(exercise_callbacks())

    assert max_active == 1
    assert calls == ["first:start", "first:end", "second:start", "second:end"]


def test_cancelled_callback_worker_stays_serialized(monkeypatch):
    active = 0
    max_active = 0
    state_lock = threading.Lock()
    first_entered = threading.Event()
    second_offload_started = threading.Event()
    second_handler_entered = threading.Event()
    release_first = threading.Event()

    def blocking_handler(event):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            if event == "first":
                first_entered.set()
                release_first.wait(timeout=2.0)
            else:
                second_handler_entered.set()
        finally:
            with state_lock:
                active -= 1

    real_run_in_threadpool = main.run_in_threadpool

    async def observed_run_in_threadpool(func, event):
        if event == "second":
            second_offload_started.set()
        return await real_run_in_threadpool(func, event)

    monkeypatch.setattr(main._parser, "parse", lambda body, _signature: [body])
    monkeypatch.setattr(main, "_handle_event", blocking_handler)
    monkeypatch.setattr(main, "run_in_threadpool", observed_run_in_threadpool)

    async def exercise_cancellation():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            first_task = asyncio.create_task(
                client.post(
                    "/callback",
                    content=b"first",
                    headers={"x-line-signature": "dummy_sig"},
                )
            )
            assert await asyncio.to_thread(first_entered.wait, 1.0)

            first_task.cancel()
            try:
                await first_task
            except asyncio.CancelledError:
                pass

            watchdog = threading.Timer(1.0, release_first.set)
            watchdog.start()
            try:
                second_task = asyncio.create_task(
                    client.post(
                        "/callback",
                        content=b"second",
                        headers={"x-line-signature": "dummy_sig"},
                    )
                )
                assert await asyncio.to_thread(second_offload_started.wait, 1.0)
                assert not second_handler_entered.is_set()
                release_first.set()
                second_response = await asyncio.wait_for(second_task, timeout=1)
            finally:
                release_first.set()
                watchdog.cancel()

        assert second_response.status_code == 200

    asyncio.run(exercise_cancellation())

    assert second_handler_entered.is_set()
    assert max_active == 1


def test_callback_continues_after_one_handler_fails(monkeypatch):
    calls = []

    def handler(event):
        calls.append(event)
        if event == "first":
            raise RuntimeError("simulated handler failure")

    monkeypatch.setattr(
        main._parser,
        "parse",
        lambda _body, _signature: ["first", "second"],
    )
    monkeypatch.setattr(main, "_handle_event", handler)

    async def exercise_callback():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/callback",
                content=b'{"events":[]}',
                headers={"x-line-signature": "dummy_sig"},
            )
        assert response.status_code == 200

    asyncio.run(exercise_callback())

    assert calls == ["first", "second"]
