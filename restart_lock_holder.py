#!/usr/bin/env python3
"""Hold the shared uvicorn restart flock without leaking it to shell children."""

from __future__ import annotations

import argparse
import fcntl
import os
import stat
import sys
import time
from pathlib import Path


EX_SOFTWARE = 70
EX_TEMPFAIL = 75


def _write_status(path: Path, value: str) -> None:
    flags = os.O_WRONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise OSError("unsafe restart lock status file")
        os.ftruncate(fd, 0)
        os.write(fd, f"{value}\n".encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_lock(path: Path) -> int:
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise OSError("unsafe restart lock parent")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    metadata = os.fstat(fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        os.close(fd)
        raise OSError("unsafe restart lock metadata")
    os.fchmod(fd, 0o600)
    os.set_inheritable(fd, False)
    return fd


def _acquire(fd: int, timeout: int, parent_pid: int) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if os.getppid() != parent_pid:
            raise OSError("restart lock holder parent exited")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--lock-file", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--timeout", type=int, required=True)
    args = parser.parse_args()

    fd: int | None = None
    try:
        if args.timeout < 0 or os.getppid() != args.parent_pid:
            raise OSError("invalid restart lock holder parent or timeout")
        fd = _open_lock(args.lock_file)
        if not _acquire(fd, args.timeout, args.parent_pid):
            _write_status(args.status_file, "busy")
            return EX_TEMPFAIL
        _write_status(args.status_file, "acquired")
        while os.getppid() == args.parent_pid:
            time.sleep(0.01)
        return 0
    except Exception:
        try:
            _write_status(args.status_file, "error")
        except Exception:
            pass
        return EX_SOFTWARE
    finally:
        if fd is not None:
            os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
