from __future__ import annotations

from collections import deque
import gc
import multiprocessing
import os
from pathlib import Path
import stat
import threading
import time

import pytest

import vision_llm


class FakeConnection:
    def __init__(self, *, on_send=None):
        self.inbox = deque()
        self.on_send = on_send
        self.closed = False
        self.sent = []
        self._condition = threading.Condition()

    def push(self, message):
        with self._condition:
            self.inbox.append(message)
            self._condition.notify_all()

    def poll(self, timeout=0.0):
        deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
        with self._condition:
            while not self.inbox and not self.closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return bool(self.inbox)

    def recv(self):
        with self._condition:
            return self.inbox.popleft()

    def send(self, message):
        self.sent.append(message)
        if self.on_send:
            self.on_send(self, message)

    def close(self):
        with self._condition:
            self.closed = True
            self._condition.notify_all()


class FakeProcess:
    def __init__(self, *, stays_alive_after_kill=False):
        self.pid = 1234
        self.started = False
        self.alive = False
        self.terminate_calls = 0
        self.kill_calls = 0
        self.join_calls = []
        self.close_calls = 0
        self.stays_alive_after_kill = stays_alive_after_kill

    def start(self):
        self.started = True
        self.alive = True

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1
        if not self.stays_alive_after_kill:
            self.alive = False

    def join(self, timeout=None):
        self.join_calls.append(timeout)

    def close(self):
        self.close_calls += 1


class FakeContext:
    def __init__(self, specs):
        self.specs = deque(specs)
        self.processes = []
        self.parents = []

    def Pipe(self, duplex=True):
        assert duplex is True
        spec = self.specs[0]
        parent = FakeConnection(on_send=spec.get("on_send"))
        child = FakeConnection()
        self.parents.append(parent)
        return parent, child

    def Process(self, *, target, args, daemon, name):
        assert callable(target)
        assert daemon is True
        assert name == "vision-mlx-process"
        spec = self.specs.popleft()
        process = FakeProcess(
            stays_alive_after_kill=spec.get("stays_alive_after_kill", False)
        )
        self.processes.append(process)
        if spec.get("ready"):
            generation = args[1]
            self.parents[-1].push(("ready", generation, "fake-model"))
        if spec.get("load_failed"):
            generation = args[1]
            self.parents[-1].push(
                ("load_failed", generation, "local_model_unavailable", "LoadError")
            )
        return process


def _supervisor(
    context,
    *,
    ipc_root=None,
    default_timeout_sec=40.0,
    cleanup_pending_timeout_sec=10.0,
    warmup_timeout_sec=300.0,
):
    return vision_llm._VisionProcessSupervisor(
        context=context,
        terminate_timeout_sec=0.001,
        kill_timeout_sec=0.001,
        poll_slice_sec=0.001,
        default_timeout_sec=default_timeout_sec,
        cleanup_pending_timeout_sec=cleanup_pending_timeout_sec,
        warmup_timeout_sec=warmup_timeout_sec,
        ipc_store=vision_llm._PrivateIpcStore(root=ipc_root) if ipc_root else None,
    )


def test_warming_request_fails_fast_without_killing_safe_loader():
    context = FakeContext([{"ready": False, "on_send": None}])
    supervisor = _supervisor(context)

    with pytest.raises(vision_llm.VisionBusyError, match="warming"):
        supervisor.run("describe", {"image": b"image"}, timeout_sec=0.01)

    process = context.processes[0]
    assert process.terminate_calls == 0
    assert process.kill_calls == 0
    assert process.is_alive()


def test_ready_worker_is_one_active_zero_queue_and_checks_generation():
    release = threading.Event()

    def respond(connection, request):
        kind, generation, request_id, operation, _payload = request
        assert kind == "request"
        assert operation == "describe"

        def deliver():
            assert release.wait(timeout=1)
            connection.push(("result", generation - 1, request_id, "stale"))
            connection.push(("result", generation, request_id, "fresh"))

        threading.Thread(target=deliver, daemon=True).start()

    context = FakeContext([{"ready": True, "on_send": respond}])
    supervisor = _supervisor(context)
    results = []
    first = threading.Thread(
        target=lambda: results.append(
            supervisor.run("describe", {"image": b"one"}, timeout_sec=1)
        )
    )
    first.start()
    deadline = time.monotonic() + 1
    while not context.parents or not context.parents[0].sent:
        assert time.monotonic() < deadline
        time.sleep(0.001)

    with pytest.raises(vision_llm.VisionBusyError, match="busy"):
        supervisor.run("describe", {"image": b"two"}, timeout_sec=0.1)

    release.set()
    first.join(timeout=1)
    assert results == ["fresh"]


def test_timeout_invalidates_generation_and_kills_late_native_worker():
    context = FakeContext(
        [
            {"ready": True, "on_send": lambda *_args: None},
            {"ready": False, "on_send": None},
        ]
    )
    supervisor = _supervisor(context)

    with pytest.raises(vision_llm.VisionTimeoutError):
        supervisor.run("describe", {"image": b"slow"}, timeout_sec=0.005)

    old_process = context.processes[0]
    assert old_process.terminate_calls == 1
    assert old_process.kill_calls == 1
    assert not old_process.is_alive()
    assert context.parents[0].closed

    with pytest.raises(vision_llm.VisionBusyError, match="warming"):
        supervisor.run("describe", {"image": b"next"}, timeout_sec=0.1)
    assert len(context.processes) == 2


def test_cleanup_pending_is_fail_closed_and_never_spawns_second_child():
    context = FakeContext(
        [
            {
                "ready": True,
                "on_send": lambda *_args: None,
                "stays_alive_after_kill": True,
            },
            {"ready": True, "on_send": None},
        ]
    )
    supervisor = _supervisor(context)

    with pytest.raises(vision_llm.VisionTimeoutError):
        supervisor.run("describe", {"image": b"slow"}, timeout_sec=0.005)

    with pytest.raises(vision_llm.VisionUnavailableError, match="cleanup pending"):
        supervisor.run("describe", {"image": b"next"}, timeout_sec=0.1)
    assert len(context.processes) == 1


def test_delayed_kill_exit_is_reaped_and_maintenance_rewarms_once():
    context = FakeContext(
        [
            {
                "ready": True,
                "on_send": lambda *_args: None,
                "stays_alive_after_kill": True,
            },
            {"ready": False, "on_send": None},
        ]
    )
    supervisor = _supervisor(context)

    with pytest.raises(vision_llm.VisionTimeoutError):
        supervisor.run("describe", {"image": b"slow"}, timeout_sec=0.005)

    old_process = context.processes[0]
    assert len(context.processes) == 1
    old_process.alive = False  # SIGKILL completed shortly after the fast budget.

    assert supervisor.maintenance_tick() == "warming"
    assert old_process.join_calls[-1] == 0
    assert old_process.close_calls == 1
    assert len(context.processes) == 2

    # Repeated ticks keep the exact replacement; they never create a second one.
    assert supervisor.maintenance_tick() == "warming"
    assert len(context.processes) == 2


def test_cleanup_expiry_stays_fenced_until_late_exit_then_recovers():
    context = FakeContext(
        [
            {
                "ready": True,
                "on_send": lambda *_args: None,
                "stays_alive_after_kill": True,
            },
            {"ready": True, "on_send": None},
        ]
    )
    supervisor = _supervisor(context, cleanup_pending_timeout_sec=0.0)

    with pytest.raises(vision_llm.VisionTimeoutError):
        supervisor.run("describe", {"image": b"slow"}, timeout_sec=0.005)

    assert supervisor.maintenance_tick() == "failed"
    assert "cleanup failed" in supervisor._failure_reason
    assert len(context.processes) == 1

    # The timeout is categorical only while the exact old child still exists.
    # Once it eventually exits, a later tick must reap it and recover.
    old_process = context.processes[0]
    old_process.alive = False
    assert supervisor.maintenance_tick() == "ready"
    assert old_process.close_calls == 1
    assert len(context.processes) == 2


def test_delayed_load_failure_reap_preserves_sticky_failure():
    context = FakeContext(
        [
            {
                "load_failed": True,
                "on_send": None,
                "stays_alive_after_kill": True,
            },
            {"ready": True, "on_send": None},
        ]
    )
    supervisor = _supervisor(context)

    assert supervisor.start_background() is False
    old_process = context.processes[0]
    assert supervisor._state == "cleanup_pending"
    old_process.alive = False

    assert supervisor.maintenance_tick() == "failed"
    assert "local_model_unavailable" in supervisor._failure_reason
    assert old_process.close_calls == 1
    assert len(context.processes) == 1


def test_maintenance_load_failure_never_spends_terminate_budget():
    context = FakeContext([{"load_failed": True, "on_send": None}])
    supervisor = _supervisor(context)
    with supervisor._lock:
        supervisor._start_locked()

    assert supervisor.maintenance_tick() == "failed"
    process = context.processes[0]
    assert process.terminate_calls == 0
    assert process.kill_calls == 0
    assert "local_model_unavailable" in supervisor._failure_reason


def test_maintenance_warmup_timeout_is_bounded_and_categorical():
    context = FakeContext([{"ready": False, "on_send": None}])
    supervisor = _supervisor(context, warmup_timeout_sec=0.0)

    assert supervisor.start_background() is False
    assert supervisor.maintenance_tick() == "failed"

    process = context.processes[0]
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert not process.is_alive()
    assert supervisor._failure_code == "warmup_timeout"
    assert len(context.processes) == 1


def test_default_warmup_timeout_has_measured_cold_start_headroom():
    supervisor = vision_llm._VisionProcessSupervisor(
        context=FakeContext([]),
    )

    assert 240.0 <= supervisor._warmup_timeout_sec <= 600.0


def test_maintenance_reaps_idle_dead_worker_and_rewarms_once():
    context = FakeContext(
        [
            {"ready": True, "on_send": None},
            {"ready": False, "on_send": None},
        ]
    )
    supervisor = _supervisor(context)
    assert supervisor.start_background() is True
    old_process = context.processes[0]
    old_process.alive = False

    assert supervisor.maintenance_tick() == "warming"
    assert old_process.join_calls[-1] == 0
    assert old_process.close_calls == 1
    assert len(context.processes) == 2


def test_shutdown_fence_prevents_late_maintenance_respawn():
    context = FakeContext(
        [
            {"ready": True, "on_send": None},
            {"ready": True, "on_send": None},
        ]
    )
    supervisor = _supervisor(context)
    assert supervisor.start_background() is True

    supervisor.shutdown()

    assert supervisor.maintenance_tick() == "stopped"
    assert len(context.processes) == 1
    with pytest.raises(vision_llm.VisionUnavailableError) as exc_info:
        supervisor.start_background()
    assert exc_info.value.code == "worker_shutdown"


def test_byte_image_uses_private_parent_file_and_cleans_after_success(tmp_path):
    observed = {}

    def respond(connection, request):
        _, generation, request_id, _operation, payload = request
        image_path = Path(payload["image"])
        metadata = image_path.lstat()
        observed.update(
            path=image_path,
            content=image_path.read_bytes(),
            mode=stat.S_IMODE(metadata.st_mode),
            regular=stat.S_ISREG(metadata.st_mode),
            nlink=metadata.st_nlink,
            owner=metadata.st_uid,
        )
        connection.push(("result", generation, request_id, "ok"))

    context = FakeContext([{"ready": True, "on_send": respond}])
    supervisor = _supervisor(context, ipc_root=tmp_path / "ipc")

    assert supervisor.run("describe", {"image": b"private"}, timeout_sec=1) == "ok"
    assert observed["content"] == b"private"
    assert observed["mode"] == 0o600
    assert observed["regular"] is True
    assert observed["nlink"] == 1
    assert observed["owner"] == os.getuid()
    assert not observed["path"].exists()
    assert stat.S_IMODE((tmp_path / "ipc").stat().st_mode) == 0o700


def test_deadline_starts_before_blocking_send_and_payload_is_cleaned(tmp_path):
    observed_paths = []

    def slow_send(_connection, request):
        observed_paths.append(Path(request[4]["image"]))
        time.sleep(0.02)

    context = FakeContext([{"ready": True, "on_send": slow_send}])
    supervisor = _supervisor(context, ipc_root=tmp_path / "ipc")

    with pytest.raises(vision_llm.VisionTimeoutError):
        supervisor.run("describe", {"image": b"private"}, timeout_sec=0.005)
    assert observed_paths and not observed_paths[0].exists()


def test_none_timeout_uses_bounded_default(tmp_path):
    context = FakeContext([{"ready": True, "on_send": lambda *_args: None}])
    supervisor = _supervisor(
        context,
        ipc_root=tmp_path / "ipc",
        default_timeout_sec=0.005,
    )

    with pytest.raises(vision_llm.VisionTimeoutError):
        supervisor.run("describe", {"image": b"slow"}, timeout_sec=None)


def test_send_failure_cleans_payload_and_is_not_sticky(tmp_path):
    sent_paths = []

    def fail_send(_connection, request):
        sent_paths.append(Path(request[4]["image"]))
        raise BrokenPipeError("sensitive path must not be retained")

    context = FakeContext(
        [
            {"ready": True, "on_send": fail_send},
            {"ready": False, "on_send": None},
        ]
    )
    supervisor = _supervisor(context, ipc_root=tmp_path / "ipc")

    with pytest.raises(vision_llm.VisionUnavailableError, match="send failed"):
        supervisor.run("describe", {"image": b"private"}, timeout_sec=1)
    assert sent_paths and not sent_paths[0].exists()
    with pytest.raises(vision_llm.VisionBusyError, match="warming"):
        supervisor.run("describe", {"image": b"next"}, timeout_sec=1)
    assert len(context.processes) == 2


def test_send_failure_cleanup_pending_is_categorical_then_recovers(tmp_path):
    def fail_send(_connection, _request):
        raise BrokenPipeError("private")

    context = FakeContext(
        [
            {
                "ready": True,
                "on_send": fail_send,
                "stays_alive_after_kill": True,
            },
            {"ready": True, "on_send": None},
        ]
    )
    supervisor = _supervisor(context, ipc_root=tmp_path / "ipc")

    with pytest.raises(
        vision_llm.VisionUnavailableError, match="cleanup pending"
    ):
        supervisor.run("describe", {"image": b"private"}, timeout_sec=1)

    old_process = context.processes[0]
    old_process.alive = False
    assert supervisor.maintenance_tick() == "ready"
    assert supervisor._failure_reason == ""
    assert len(context.processes) == 2


def test_unlink_failure_is_fail_closed_without_exposing_path(monkeypatch, tmp_path):
    context = FakeContext(
        [
            {
                "ready": True,
                "on_send": lambda connection, request: connection.push(
                    ("result", request[1], request[2], "ok")
                ),
            },
            {"ready": True, "on_send": None},
        ]
    )
    store = vision_llm._PrivateIpcStore(root=tmp_path / "private-ipc")
    supervisor = vision_llm._VisionProcessSupervisor(
        context=context,
        ipc_store=store,
        terminate_timeout_sec=0.001,
        kill_timeout_sec=0.001,
        poll_slice_sec=0.001,
    )
    monkeypatch.setattr(store, "_safe_unlink_name", lambda _name: False)

    with pytest.raises(vision_llm.VisionUnavailableError) as failure:
        supervisor.run("describe", {"image": b"private"}, timeout_sec=1)

    assert str(tmp_path) not in str(failure.value)
    with pytest.raises(vision_llm.VisionUnavailableError, match="cleanup failed"):
        supervisor.run("describe", {"image": b"next"}, timeout_sec=1)
    assert len(context.processes) == 1


def test_staging_cleanup_failure_is_categorical(monkeypatch, tmp_path):
    store = vision_llm._PrivateIpcStore(root=tmp_path / "private-ipc")
    monkeypatch.setattr(store, "_safe_metadata", lambda _metadata: False)

    with pytest.raises(
        vision_llm.VisionUnavailableError,
        match="staging cleanup failed",
    ) as failure:
        store.stage(b"private", 1)

    assert str(tmp_path) not in str(failure.value)


def test_load_failure_reaps_child_but_keeps_fixed_sticky_reason(tmp_path):
    context = FakeContext([{"load_failed": True, "on_send": None}])
    supervisor = _supervisor(context, ipc_root=tmp_path / "ipc")

    with pytest.raises(
        vision_llm.VisionUnavailableError,
        match="local_model_unavailable.*LoadError",
    ):
        supervisor.run("describe", {"image": b"private"}, timeout_sec=1)
    process = context.processes[0]
    assert process.join_calls
    assert process.close_calls == 1
    assert supervisor._process is None
    with pytest.raises(vision_llm.VisionUnavailableError):
        supervisor.run("describe", {"image": b"next"}, timeout_sec=1)
    assert len(context.processes) == 1


def test_load_failure_message_is_drained_before_dead_child_replacement(tmp_path):
    context = FakeContext(
        [
            {"ready": False, "on_send": None},
            {"ready": True, "on_send": None},
        ]
    )
    supervisor = _supervisor(context, ipc_root=tmp_path / "ipc")

    assert supervisor.start_background() is False
    generation = supervisor._generation
    context.parents[0].push(
        ("load_failed", generation, "local_model_unavailable", "LoadError")
    )
    context.processes[0].alive = False

    with pytest.raises(
        vision_llm.VisionUnavailableError,
        match="local_model_unavailable.*LoadError",
    ):
        supervisor.start_background()
    assert len(context.processes) == 1


def test_startup_sweep_only_removes_safe_dead_owner_orphan(tmp_path):
    root = tmp_path / "ipc"
    store = vision_llm._PrivateIpcStore(root=root)
    store.ensure_private_root()
    dead_pid = 999_999_999
    dead = root / f"vision-ipc-p{dead_pid}-r1-0000000000000001.bin"
    live = root / f"vision-ipc-p{os.getpid()}-r1-0000000000000002.bin"
    linked = root / f"vision-ipc-p{dead_pid}-r1-0000000000000003.bin"
    linked_peer = root / "unrelated-hardlink.bin"
    symlink = root / f"vision-ipc-p{dead_pid}-r1-0000000000000004.bin"
    unrelated = root / "unrelated.bin"
    for path in (dead, live, linked, unrelated):
        path.write_bytes(b"x")
        path.chmod(0o600)
    os.link(linked, linked_peer)
    symlink.symlink_to(dead)

    store.sweep_orphans()

    assert not dead.exists()
    assert live.exists()
    assert linked.exists() and linked_peer.exists()
    assert symlink.is_symlink()
    assert unrelated.exists()


def test_local_model_load_uses_offline_existing_absolute_snapshot(
    monkeypatch, tmp_path
):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    calls = []

    def fake_snapshot_download(**kwargs):
        calls.append(("snapshot", kwargs))
        return str(snapshot)

    def fake_load(path):
        calls.append(("load", path))
        return object(), object()

    monkeypatch.setattr(vision_llm, "_model", None)
    monkeypatch.setattr(vision_llm, "_processor", None)
    monkeypatch.setattr(vision_llm, "_loaded_name", None)
    monkeypatch.setattr(
        "huggingface_hub.snapshot_download", fake_snapshot_download
    )
    fake_mlx = type(os)("mlx_vlm")
    fake_mlx.__file__ = "/test/fake_mlx_vlm.py"
    fake_mlx.load = fake_load
    monkeypatch.setitem(__import__("sys").modules, "mlx_vlm", fake_mlx)

    assert vision_llm._ensure_loaded() is True
    assert calls[0] == (
        "snapshot",
        {
            "repo_id": vision_llm._FALLBACKS[0],
            "local_files_only": True,
        },
    )
    assert calls[1] == ("load", str(snapshot.resolve()))


def test_model_failure_log_and_child_failure_omit_exception_text(
    monkeypatch, caplog
):
    sensitive = "/private/sensitive/model/path"

    def fail_snapshot(**_kwargs):
        raise RuntimeError(sensitive)

    monkeypatch.setattr(vision_llm, "_model", None)
    monkeypatch.setattr(vision_llm, "_processor", None)
    monkeypatch.setattr(vision_llm, "_loaded_name", None)
    monkeypatch.setattr("huggingface_hub.snapshot_download", fail_snapshot)

    with caplog.at_level("WARNING"):
        assert vision_llm._ensure_loaded() is False
    logs = caplog.text
    assert sensitive not in logs
    assert "local_snapshot_unavailable" in logs
    assert "RuntimeError" in logs


def test_model_init_failure_is_distinct_from_missing_snapshot(monkeypatch, tmp_path, caplog):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()

    monkeypatch.setattr(vision_llm, "_model", None)
    monkeypatch.setattr(vision_llm, "_processor", None)
    monkeypatch.setattr(vision_llm, "_loaded_name", None)
    monkeypatch.setattr(
        "huggingface_hub.snapshot_download", lambda **_kwargs: str(snapshot)
    )
    fake_mlx = type(os)("mlx_vlm")
    fake_mlx.__file__ = "/test/fake_mlx_vlm.py"
    fake_mlx.load = lambda _path: (_ for _ in ()).throw(RuntimeError("private"))
    monkeypatch.setitem(__import__("sys").modules, "mlx_vlm", fake_mlx)

    with caplog.at_level("WARNING"):
        assert vision_llm._ensure_loaded() is False

    assert "local_model_unavailable" in caplog.text
    assert "private" not in caplog.text


def test_model_init_failure_wins_over_later_missing_fallback(monkeypatch, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    first_model, second_model = "test/primary", "test/fallback"

    def fake_snapshot_download(*, repo_id, local_files_only):
        assert local_files_only is True
        if repo_id == first_model:
            return str(snapshot)
        raise FileNotFoundError("fallback is not cached")

    monkeypatch.setattr(vision_llm, "_FALLBACKS", [first_model, second_model])
    monkeypatch.setattr(vision_llm, "_model", None)
    monkeypatch.setattr(vision_llm, "_processor", None)
    monkeypatch.setattr(vision_llm, "_loaded_name", None)
    monkeypatch.setattr(
        "huggingface_hub.snapshot_download", fake_snapshot_download
    )
    fake_mlx = type(os)("mlx_vlm")
    fake_mlx.__file__ = "/test/fake_mlx_vlm.py"
    fake_mlx.load = lambda _path: (_ for _ in ()).throw(RuntimeError("init failed"))
    monkeypatch.setitem(__import__("sys").modules, "mlx_vlm", fake_mlx)

    assert vision_llm._ensure_loaded() is False
    assert vision_llm._last_load_failure_code == "local_model_unavailable"


def test_child_sets_offline_flags_before_loading(monkeypatch):
    seen = {}

    class ChildConnection:
        def __init__(self):
            self.sent = []

        def send(self, message):
            self.sent.append(message)

        def recv(self):
            return ("stop",)

        def close(self):
            pass

    def fake_ensure_loaded():
        seen["hf"] = os.environ.get("HF_HUB_OFFLINE")
        seen["transformers"] = os.environ.get("TRANSFORMERS_OFFLINE")
        return True

    connection = ChildConnection()
    monkeypatch.setattr(vision_llm, "_ensure_loaded", fake_ensure_loaded)
    vision_llm._vision_child_main(connection, 7)
    assert seen == {"hf": "1", "transformers": "1"}
    assert connection.sent[0][:2] == ("ready", 7)


def _spawn_smoke_child(connection, generation):
    connection.send(("ready", generation, "test-only"))
    while True:
        message = connection.recv()
        if message[0] == "stop":
            return
        _, request_generation, request_id, operation, payload = message
        if operation == "hang":
            time.sleep(60)
            continue
        if operation == "exit":
            os._exit(17)
        data = Path(payload["image"]).read_bytes()
        connection.send(("result", request_generation, request_id, data))


def _spawn_close_pipe(connection):
    """Prime multiprocessing's process-wide resource-sharer listener."""
    connection.close()


def _fd_count():
    for path in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(path):
            return len(os.listdir(path))
    return None


def _fd_targets():
    targets = []
    for descriptor in range(256):
        try:
            metadata = os.fstat(descriptor)
        except OSError:
            continue
        targets.append(
            (descriptor, metadata.st_mode, metadata.st_dev, metadata.st_ino)
        )
    return targets


def test_real_spawn_ipc_timeout_death_and_no_parent_resource_leak(
    monkeypatch, tmp_path
):
    context = multiprocessing.get_context("spawn")
    # Keep lazy log handlers outside the FD accounting for the process/pipe test.
    monkeypatch.setattr(vision_llm.logger, "warning", lambda *_args, **_kwargs: None)
    # Prime multiprocessing's persistent tracker and resource-sharer listener
    # before the baseline; those process-wide singleton FDs are not a leak from
    # the vision supervisor lifecycle under test.
    left, right = context.Pipe()
    primer = context.Process(target=_spawn_close_pipe, args=(right,))
    primer.start()
    right.close()
    primer.join(timeout=5)
    primer.close()
    left.close()
    baseline_fds = _fd_count()
    baseline_targets = _fd_targets()
    root = tmp_path / "ipc"
    supervisor = vision_llm._VisionProcessSupervisor(
        context=context,
        child_target=_spawn_smoke_child,
        ipc_store=vision_llm._PrivateIpcStore(root=root),
        default_timeout_sec=0.2,
        terminate_timeout_sec=0.1,
        kill_timeout_sec=0.1,
        poll_slice_sec=0.005,
    )
    try:
        ready = supervisor.start_background()
        deadline = time.monotonic() + 5
        while not ready:
            assert time.monotonic() < deadline
            time.sleep(0.01)
            ready = supervisor.start_background()
        assert supervisor.run("describe", {"image": b"spawn"}, timeout_sec=1) == b"spawn"
        with pytest.raises(vision_llm.VisionTimeoutError):
            supervisor.run("hang", {"image": b"timeout"}, timeout_sec=0.05)
        assert supervisor._process is None
        assert list(root.iterdir()) == []
        ready = supervisor.start_background()
        deadline = time.monotonic() + 5
        while not ready:
            assert time.monotonic() < deadline
            time.sleep(0.01)
            ready = supervisor.start_background()
        with pytest.raises(
            vision_llm.VisionUnavailableError,
            match="(?:exited|connection failed)",
        ):
            supervisor.run("exit", {"image": b"crash"}, timeout_sec=1)
        assert supervisor._process is None
        assert list(root.iterdir()) == []
        ready = supervisor.start_background()
        deadline = time.monotonic() + 5
        while not ready:
            assert time.monotonic() < deadline
            time.sleep(0.01)
            ready = supervisor.start_background()
        assert supervisor.run("describe", {"image": b"recovered"}, timeout_sec=1) == b"recovered"
        assert os.getpid() > 0
    finally:
        supervisor.shutdown()
    gc.collect()
    if baseline_fds is not None:
        assert _fd_count() <= baseline_fds, (
            f"before={baseline_targets!r} after={_fd_targets()!r}"
        )


def test_public_shutdown_wrapper_is_idempotent(monkeypatch):
    calls = []

    class FakeSupervisor:
        def shutdown(self):
            calls.append("shutdown")

    monkeypatch.setattr(vision_llm, "_PROCESS_SUPERVISOR", FakeSupervisor())
    vision_llm.shutdown_background_worker()
    vision_llm.shutdown_background_worker()
    assert calls == ["shutdown", "shutdown"]
