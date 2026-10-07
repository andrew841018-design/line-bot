import os
import subprocess
import sys

import family_interest


def test_family_interest_uses_configured_sqlite_path(tmp_path):
    configured = tmp_path / "configured.db"
    env = os.environ.copy()
    env["SQLITE_PATH"] = str(configured)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import config, family_interest; "
            "db=Path(config.settings.sqlite_path); "
            "result=family_interest.detect_per_member_topics('group'); "
            "print(int(family_interest.DB_PATH == db), result, int(db.exists()), "
            "int(Path(str(db) + '-wal').exists()), "
            "int(Path(str(db) + '-shm').exists()), sep='|')",
        ],
        cwd=family_interest.BASE,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "1|{}|0|0|0"


def test_family_interest_missing_database_is_read_only_and_returns_empty(
    tmp_path, monkeypatch
):
    missing = tmp_path / "missing.db"
    monkeypatch.setattr(family_interest, "DB_PATH", missing)
    monkeypatch.setattr(family_interest, "_load_aliases", lambda: {"member": "alias"})

    assert family_interest.detect_per_member_topics("group") == {}
    assert not missing.exists()
