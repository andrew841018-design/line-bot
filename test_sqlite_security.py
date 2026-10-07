import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import sqlite_security


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_prepare_private_sqlite_creates_private_db_and_wal_under_open_umask(
    tmp_path,
):
    db_path = tmp_path / "private.db"
    previous_umask = os.umask(0o022)
    try:
        sqlite_security.prepare_private_sqlite(db_path)
        connection = sqlite3.connect(db_path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE item(value TEXT)")
        connection.execute("INSERT INTO item VALUES ('ok')")
        connection.commit()
        sqlite_security.harden_private_sqlite_sidecars(db_path)

        assert _mode(db_path) == 0o600
        assert _mode(Path(f"{db_path}-wal")) == 0o600
        assert _mode(Path(f"{db_path}-shm")) == 0o600
        connection.close()
    finally:
        os.umask(previous_umask)


def test_prepare_private_sqlite_hardens_exact_family_only(tmp_path):
    db_path = tmp_path / "private.db"
    neighbors = [
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
    ]
    for path in neighbors:
        path.write_bytes(b"")
        path.chmod(0o644)
    unrelated = tmp_path / "private.db-backup"
    unrelated.write_bytes(b"keep")
    unrelated.chmod(0o644)
    parent_mode = _mode(tmp_path)

    sqlite_security.prepare_private_sqlite(db_path)

    assert [_mode(path) for path in neighbors] == [0o600, 0o600, 0o600]
    assert _mode(unrelated) == 0o644
    assert _mode(tmp_path) == parent_mode


def test_prepare_private_sqlite_rejects_symlink_without_chmod_target(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"private")
    target.chmod(0o644)
    link = tmp_path / "private.db"
    link.symlink_to(target)

    with pytest.raises((OSError, RuntimeError)):
        sqlite_security.prepare_private_sqlite(link)

    assert _mode(target) == 0o644


def test_missing_sidecars_are_not_created_by_hardening(tmp_path):
    db_path = tmp_path / "private.db"
    sqlite_security.prepare_private_sqlite(db_path)

    sqlite_security.harden_private_sqlite_sidecars(db_path)

    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()


def test_private_connect_recreates_deleted_database_with_private_mode(tmp_path):
    db_path = tmp_path / "private.db"
    first = sqlite_security.connect_private_sqlite(db_path)
    first.close()
    db_path.unlink()

    previous_umask = os.umask(0o022)
    try:
        second = sqlite_security.connect_private_sqlite(db_path)
        second.execute("PRAGMA journal_mode=WAL")
        second.execute("CREATE TABLE item(value TEXT)")
        second.execute("INSERT INTO item VALUES ('ok')")
        second.commit()
        sqlite_security.harden_private_sqlite_sidecars(db_path)
        assert _mode(db_path) == 0o600
        assert _mode(Path(f"{db_path}-wal")) == 0o600
        assert _mode(Path(f"{db_path}-shm")) == 0o600
        second.close()
    finally:
        os.umask(previous_umask)


def test_prepare_private_sqlite_rejects_world_writable_parent(tmp_path):
    unsafe_parent = tmp_path / "shared"
    unsafe_parent.mkdir(mode=0o777)
    unsafe_parent.chmod(0o777)
    db_path = unsafe_parent / "private.db"

    with pytest.raises(RuntimeError, match="parent"):
        sqlite_security.prepare_private_sqlite(db_path)

    assert not db_path.exists()


def test_prepare_private_sqlite_creates_missing_owner_only_parent(tmp_path):
    private_parent = tmp_path / "line_bot-private"
    db_path = private_parent / "line_bot.db"

    sqlite_security.prepare_private_sqlite(db_path)

    assert _mode(private_parent) == 0o700
    assert _mode(db_path) == 0o600


def test_memory_startup_creates_private_cloud_parent_and_database(tmp_path):
    private_parent = tmp_path / "line_bot-private"
    db_path = private_parent / "line_bot.db"
    env = os.environ.copy()
    env["SQLITE_PATH"] = str(db_path)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import stat; from pathlib import Path; import config; "
            "db=Path(config.settings.sqlite_path); "
            "before=int(db.exists() or db.parent.exists()); import memory; "
            "print(before, oct(stat.S_IMODE(db.parent.stat().st_mode)), "
            "oct(stat.S_IMODE(db.stat().st_mode)), sep='|')",
        ],
        cwd=Path(__file__).resolve().parent,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "0|0o700|0o600"
