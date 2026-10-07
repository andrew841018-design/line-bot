"""Owner-only filesystem policy for local SQLite database families."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path


_PRIVATE_MODE = 0o600
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _validate_private_parent(path: Path) -> None:
    metadata = path.parent.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise RuntimeError("SQLite parent must be owner-controlled and not writable by others")


def _open_and_harden(
    path: Path,
    *,
    create: bool,
    allow_unlinked: bool = False,
) -> None:
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, _PRIVATE_MODE)
    try:
        metadata = os.fstat(fd)
        if allow_unlinked and metadata.st_nlink == 0:
            return
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise RuntimeError("SQLite path is not an owner-controlled regular file")
        os.fchmod(fd, _PRIVATE_MODE)
        if stat.S_IMODE(os.fstat(fd).st_mode) != _PRIVATE_MODE:
            raise RuntimeError("SQLite file mode is not owner-only")
    finally:
        os.close(fd)


def harden_private_sqlite_sidecars(db_path: Path | str) -> None:
    """Harden exact existing SQLite companions without creating or globbing."""
    path = Path(db_path)
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        companion = Path(f"{path}{suffix}")
        try:
            _open_and_harden(
                companion,
                create=False,
                allow_unlinked=True,
            )
        except FileNotFoundError:
            continue


def prepare_private_sqlite(db_path: Path | str) -> Path:
    """Pre-create/harden a configured DB before sqlite3 can create sidecars."""
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _validate_private_parent(path)
    _open_and_harden(path, create=True)
    harden_private_sqlite_sidecars(path)
    return path


def connect_private_sqlite(
    db_path: Path | str,
    *args,
    **kwargs,
) -> sqlite3.Connection:
    """Open SQLite only after enforcing owner-only path and sidecar policy."""
    path = prepare_private_sqlite(db_path)
    connection = sqlite3.connect(path, *args, **kwargs)
    try:
        harden_private_sqlite_sidecars(path)
    except Exception:
        connection.close()
        raise
    return connection
