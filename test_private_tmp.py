"""2026-11-06: private_tmp_dir must only hand out a folder that is really ours.

Everything runs under tmp_path; the real temp folder is never touched.
"""

from __future__ import annotations

import logging
import os
import stat
from types import SimpleNamespace

import pytest

import private_tmp


@pytest.fixture
def temp_root(monkeypatch, tmp_path):
    monkeypatch.setattr(private_tmp.tempfile, "tempdir", str(tmp_path))
    return tmp_path


def _own_path(root):
    return root / f"synthetic-{os.getuid()}"


def test_folder_is_made_for_this_user_alone(temp_root):
    folder = private_tmp.private_tmp_dir("synthetic")
    assert folder == _own_path(temp_root)
    assert stat.S_IMODE(folder.lstat().st_mode) == 0o700


def test_folder_is_reused_with_what_it_holds(temp_root):
    kept = private_tmp.private_tmp_dir("synthetic") / "kept.txt"
    kept.write_text("合成內容", encoding="utf-8")
    assert private_tmp.private_tmp_dir("synthetic") == kept.parent
    assert kept.exists()


def test_a_folder_removed_in_between_comes_back(temp_root):
    folder = private_tmp.private_tmp_dir("synthetic")
    folder.rmdir()
    assert private_tmp.private_tmp_dir("synthetic").is_dir()


def test_loose_permissions_are_tightened(temp_root):
    _own_path(temp_root).mkdir()
    os.chmod(_own_path(temp_root), 0o755)
    assert stat.S_IMODE(private_tmp.private_tmp_dir("synthetic").lstat().st_mode) == 0o700


def test_a_symlink_is_refused_and_its_target_left_alone(temp_root, caplog):
    target = temp_root / "elsewhere"
    target.mkdir()
    os.chmod(target, 0o755)
    _own_path(temp_root).symlink_to(target)
    with caplog.at_level(logging.WARNING, logger="private_tmp"), pytest.raises(OSError):
        private_tmp.private_tmp_dir("synthetic")
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert "failed ownership validation name=synthetic" in caplog.text


def test_a_plain_file_is_refused(temp_root):
    _own_path(temp_root).write_text("", encoding="utf-8")
    with pytest.raises(OSError):
        private_tmp.private_tmp_dir("synthetic")


def test_someone_elses_folder_is_refused(temp_root, monkeypatch):
    foreign = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=os.getuid() + 1)
    # Path.lstat is global and conftest's teardown reads it: the patch must end here.
    with monkeypatch.context() as patch:
        patch.setattr(private_tmp.Path, "lstat", lambda _self: foreign)
        with pytest.raises(OSError):
            private_tmp.private_tmp_dir("synthetic")
