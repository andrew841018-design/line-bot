"""Regression tests for Discord/pending jobs under disabled pending reply."""

from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE / "jobs"))


def test_process_pending_media_disabled_noops(monkeypatch, capsys):
    import process_pending_media as ppm

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("should not run pending drain work")

    monkeypatch.setattr(ppm, "pending_reply_enabled", lambda: False)
    monkeypatch.setattr(ppm, "_generate_reply", fail_if_called)
    monkeypatch.setattr(ppm, "_send_to_group", fail_if_called)

    assert ppm.main() == 0
    out = capsys.readouterr().out
    assert "disabled" in out
    assert "processed" in out


def test_preflight_pending_check_skips_when_pending_reply_disabled(monkeypatch):
    import preflight_check as pf

    monkeypatch.setattr(pf, "pending_reply_enabled", lambda: False)

    result = pf.check_11_pending()

    assert result.status == "skip"
    assert "disabled" in result.detail


def test_update_push_success_discord_copy_avoids_pending_word(tmp_path, monkeypatch):
    import line_bot_update_push as lup

    draft = tmp_path / "pending_line_push.txt"
    draft.write_text("更新第一行\n詳細內容")
    monkeypatch.setattr(lup, "PENDING_FILE", draft)
    monkeypatch.setattr(lup, "GROUP_ID", "G1")
    monkeypatch.setattr(lup, "_get_line_token", lambda: "token")

    class Resp:
        status_code = 200
        text = "OK"

    monkeypatch.setattr(lup.requests, "post", lambda *args, **kwargs: Resp())
    sent = []
    monkeypatch.setattr(lup, "_notify_discord", lambda msg: sent.append(msg))

    assert lup.main() == 0
    assert sent
    assert "pending" not in sent[0].lower()
    assert "草稿檔已清空" in sent[0]
