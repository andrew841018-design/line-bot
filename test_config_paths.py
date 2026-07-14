from pathlib import Path

import config


def test_relative_sqlite_path_is_anchored_to_bot_directory():
    resolved = config.Settings._resolve_sqlite_path("line_bot.db")
    assert Path(resolved) == Path(config.__file__).resolve().parent / "line_bot.db"


def test_absolute_sqlite_path_is_preserved(tmp_path):
    absolute = tmp_path / "isolated.db"
    assert config.Settings._resolve_sqlite_path(str(absolute)) == str(absolute)
