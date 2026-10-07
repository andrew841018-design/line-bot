import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from restart_lock_holder import _write_status


HERE = Path(__file__).resolve().parent
HELPER = HERE / "restart_lock.sh"
HOLDER = HERE / "restart_lock_holder.py"
SHELL_CALLERS = (
    HERE / "health_check.sh",
    HERE / "morning_restart.sh",
    Path("/Users/andrew/scripts/line_bot_health_check.sh"),
    Path("/Users/andrew/scripts/morning_restart_line_bot.sh"),
    Path("/Users/andrew/scripts/line_bot_auto_iterate.sh"),
)


def _private_lock_path(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(mode=0o700, parents=True)
    state.chmod(0o700)
    return state / "uvicorn_restart.lock"


def _bash(lock_file: Path, body: str, *, timeout: int = 10):
    env = os.environ.copy()
    env.update(
        {
            "BOT_DIR": str(lock_file.parent.parent),
            "LINE_BOT_RESTART_LOCK_FILE": str(lock_file),
            "LINE_BOT_RESTART_LOCK_PYTHON": sys.executable,
            "LINE_BOT_RESTART_LOCK_HOLDER": str(HOLDER),
            "RESTART_LOCK_TIMEOUT_SEC": "0",
        }
    )
    return subprocess.run(
        ["/bin/bash", "-c", f'source "{HELPER}"\n{body}'],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_persistent_lock_file_and_legacy_lockdir_do_not_mean_busy(tmp_path):
    lock_file = _private_lock_path(tmp_path)
    lock_file.touch(mode=0o600)
    (tmp_path / "line_bot_restart.lockdir").mkdir()

    result = _bash(
        lock_file,
        "acquire_restart_lock; rc=$?; printf 'rc=%s kind=%s\\n' \"$rc\" \"$RESTART_LOCK_ERROR_KIND\"; release_restart_lock; exit \"$rc\"",
    )

    assert result.returncode == 0, result.stderr
    assert "rc=0 kind=" in result.stdout
    assert lock_file.exists()
    assert oct(lock_file.stat().st_mode & 0o777) == "0o600"


def test_shell_holder_does_not_leak_lock_to_child_and_sigkill_releases(tmp_path):
    lock_file = _private_lock_path(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "BOT_DIR": str(tmp_path),
            "LINE_BOT_RESTART_LOCK_FILE": str(lock_file),
            "LINE_BOT_RESTART_LOCK_PYTHON": sys.executable,
            "LINE_BOT_RESTART_LOCK_HOLDER": str(HOLDER),
            "RESTART_LOCK_TIMEOUT_SEC": "0",
        }
    )
    holder = subprocess.Popen(
        [
            "/bin/bash",
            "-c",
            f'source "{HELPER}"; acquire_restart_lock || exit $?; sleep 30 & child=$!; echo "acquired $child"; while :; do :; done',
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_pid = None
    try:
        assert holder.stdout is not None
        ready = holder.stdout.readline().strip().split()
        assert ready[0] == "acquired"
        child_pid = int(ready[1])
        contender = _bash(
            lock_file,
            "acquire_restart_lock; rc=$?; printf 'kind=%s\\n' \"$RESTART_LOCK_ERROR_KIND\"; exit \"$rc\"",
        )
        assert contender.returncode != 0
        assert "kind=busy" in contender.stdout

        fd = os.open(lock_file, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError("Python unexpectedly acquired the shell-held lock")
        finally:
            os.close(fd)

        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=5)
        deadline = time.monotonic() + 2
        while True:
            recovered = _bash(lock_file, "acquire_restart_lock; rc=$?; release_restart_lock; exit \"$rc\"")
            if recovered.returncode == 0 or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        assert recovered.returncode == 0, recovered.stderr
        os.kill(child_pid, 0)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_python_holder_blocks_shell_then_close_releases(tmp_path):
    lock_file = _private_lock_path(tmp_path)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        contender = _bash(lock_file, "acquire_restart_lock; exit $?")
        assert contender.returncode != 0
    finally:
        os.close(fd)

    recovered = _bash(lock_file, "acquire_restart_lock; rc=$?; release_restart_lock; exit \"$rc\"")
    assert recovered.returncode == 0, recovered.stderr


def test_helper_rejects_unsafe_parent_and_symlink_without_touching_target(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    unsafe.chmod(0o755)
    bad_parent = _bash(unsafe / "restart.lock", "acquire_restart_lock; exit $?")
    assert bad_parent.returncode == 2

    lock_file = _private_lock_path(tmp_path / "safe_case")
    target = lock_file.parent / "target"
    target.write_text("unchanged")
    lock_file.symlink_to(target)
    symlink = _bash(lock_file, "acquire_restart_lock; exit $?")
    assert symlink.returncode == 2
    assert target.read_text() == "unchanged"


def test_non_contention_holder_failure_is_a_setup_error(tmp_path):
    lock_file = _private_lock_path(tmp_path)
    fake_python = tmp_path / "fake_python"
    fake_python.write_text("#!/bin/sh\nexit 70\n")
    fake_python.chmod(0o700)

    env = os.environ.copy()
    env.update(
        {
            "BOT_DIR": str(tmp_path),
            "LINE_BOT_RESTART_LOCK_FILE": str(lock_file),
            "LINE_BOT_RESTART_LOCK_PYTHON": str(fake_python),
            "LINE_BOT_RESTART_LOCK_HOLDER": str(HOLDER),
            "RESTART_LOCK_TIMEOUT_SEC": "0",
        }
    )
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f'source "{HELPER}"; acquire_restart_lock; rc=$?; '
            'printf "rc=%s kind=%s\\n" "$rc" "$RESTART_LOCK_ERROR_KIND"; exit "$rc"',
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "kind=error" in result.stdout


def test_immediate_busy_holder_exit_is_classified_as_contention(tmp_path):
    lock_file = _private_lock_path(tmp_path)
    fake_python = tmp_path / "fake_busy_python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "shift\n"
        "while [ $# -gt 0 ]; do\n"
        "  if [ \"$1\" = --status-file ]; then printf 'busy\\n' > \"$2\"; exit 75; fi\n"
        "  shift\n"
        "done\n"
        "exit 70\n"
    )
    fake_python.chmod(0o700)
    env = os.environ.copy()
    env.update(
        {
            "BOT_DIR": str(tmp_path),
            "LINE_BOT_RESTART_LOCK_FILE": str(lock_file),
            "LINE_BOT_RESTART_LOCK_PYTHON": str(fake_python),
            "LINE_BOT_RESTART_LOCK_HOLDER": str(HOLDER),
            "RESTART_LOCK_TIMEOUT_SEC": "0",
        }
    )

    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f'source "{HELPER}"; acquire_restart_lock; rc=$?; '
            'printf "rc=%s kind=%s\\n" "$rc" "$RESTART_LOCK_ERROR_KIND"; exit "$rc"',
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "kind=busy" in result.stdout


def test_helper_rejects_hardlinked_lock_file(tmp_path):
    lock_file = _private_lock_path(tmp_path)
    lock_file.touch(mode=0o600)
    os.link(lock_file, lock_file.parent / "second-link")

    result = _bash(lock_file, "acquire_restart_lock; exit $?")

    assert result.returncode == 2


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "wrong_mode"])
def test_status_writer_validates_before_truncating(tmp_path, unsafe_kind):
    target = tmp_path / "target"
    target.write_bytes(b"must stay unchanged")
    status = tmp_path / "status"
    if unsafe_kind == "symlink":
        status.symlink_to(target)
    elif unsafe_kind == "hardlink":
        os.link(target, status)
    else:
        status.write_bytes(b"must stay unchanged")
        status.chmod(0o644)

    with pytest.raises(OSError):
        _write_status(status, "acquired")

    assert target.read_bytes() == b"must stay unchanged"
    if unsafe_kind == "wrong_mode":
        assert status.read_bytes() == b"must stay unchanged"


def test_every_live_shell_caller_uses_shared_advisory_helper():
    for caller in SHELL_CALLERS:
        text = caller.read_text()
        assert "restart_lock.sh" in text, caller
        assert "RESTART_LOCK_DIR" not in text, caller
        assert 'mkdir "$RESTART_LOCK_DIR"' not in text, caller
        assert 'rmdir "$RESTART_LOCK_DIR"' not in text, caller

    auto_iterate = SHELL_CALLERS[-1].read_text()
    assert auto_iterate.index('BOT_DIR="$PROJECT"') < auto_iterate.index('source "$RESTART_LOCK_LIB"')
