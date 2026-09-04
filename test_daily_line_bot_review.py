"""Unit tests for jobs/daily_line_bot_review.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE / "jobs"))

import daily_line_bot_review as dlbr  # noqa: E402


def _patch_state(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    state_path = state_dir / "last_run_daily-line-bot-review.json"
    monkeypatch.setattr(dlbr, "STATE_DIR", state_dir)
    monkeypatch.setattr(dlbr, "STATE_PATH", state_path)
    return state_path


def _patch_success(monkeypatch):
    monkeypatch.setattr(
        dlbr,
        "run_local_checks",
        lambda: [
            dlbr.CheckResult("git diff --check", "passed"),
            dlbr.CheckResult("py_compile core", "passed"),
        ],
    )
    monkeypatch.setattr(
        dlbr,
        "run_lifecycle_sidecar",
        lambda timeout_s=180: dlbr.LifecycleResult(
            enabled=True,
            exit_code=0,
            agents=[
                {"name": "gemini", "status": "completed-with-output"},
                {"name": "claude", "status": "completed-with-output"},
            ],
        ),
    )
    monkeypatch.setattr(
        dlbr,
        "build_feature_suggestion",
        lambda record_history=True: {
            "title": "圖片收據到期提醒",
            "reason": "近期群聊若貼帳單或收據，bot 可抽出金額、期限與提醒。",
        },
    )


def test_main_dry_run_does_not_send_discord(tmp_path, monkeypatch, capsys):
    state_path = _patch_state(monkeypatch, tmp_path)
    _patch_success(monkeypatch)

    monkeypatch.setattr(
        dlbr,
        "_send_discord",
        lambda msg: (_ for _ in ()).throw(AssertionError("sent")),
    )

    rc = dlbr.main(["--dry-run"])

    assert rc == 0
    stdout = capsys.readouterr().out
    assert "圖片收據到期提醒" in stdout
    state = json.loads(state_path.read_text())
    assert state["status"] == "dry_run"
    assert state["summary"]["discord_sent"] is False


def test_main_dry_run_skips_external_sidecar(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    monkeypatch.setattr(dlbr, "run_local_checks", lambda: [])
    monkeypatch.setattr(
        dlbr,
        "run_lifecycle_sidecar",
        lambda timeout_s=180: (_ for _ in ()).throw(AssertionError("external sidecar ran")),
    )
    monkeypatch.setattr(
        dlbr,
        "build_feature_suggestion",
        lambda record_history=True: {"title": "本地建議", "reason": "dry-run 不外送。"},
    )
    monkeypatch.setattr(
        dlbr,
        "_send_discord",
        lambda msg: (_ for _ in ()).throw(AssertionError("sent")),
    )

    rc = dlbr.main(["--dry-run"])

    assert rc == 0


def test_main_sends_discord_in_normal_mode(tmp_path, monkeypatch):
    state_path = _patch_state(monkeypatch, tmp_path)
    _patch_success(monkeypatch)
    sent = []
    monkeypatch.setattr(dlbr, "_send_discord", lambda msg: sent.append(msg) or True)

    rc = dlbr.main([])

    assert rc == 0
    assert len(sent) == 1
    assert "LINE Bot Daily Review" in sent[0]
    assert "gemini=completed-with-output" in sent[0]
    assert "圖片收據到期提醒" in sent[0]
    state = json.loads(state_path.read_text())
    assert state["status"] == "completed"
    assert state["summary"]["discord_sent"] is True


def test_main_returns_1_when_discord_fails(tmp_path, monkeypatch):
    state_path = _patch_state(monkeypatch, tmp_path)
    _patch_success(monkeypatch)
    monkeypatch.setattr(dlbr, "_send_discord", lambda msg: False)

    rc = dlbr.main([])

    assert rc == 1
    state = json.loads(state_path.read_text())
    assert state["status"] == "discord_send_failed"
    assert state["ok"] is False


def test_report_output_redacts_raw_chat_and_line_user_ids(tmp_path, monkeypatch, capsys):
    state_path = _patch_state(monkeypatch, tmp_path)
    raw_user = "U" + "a" * 32
    raw_chat = "爸爸的完整私密聊天內容不要出現在 Discord 裡面"
    monkeypatch.setattr(
        dlbr,
        "run_local_checks",
        lambda: [dlbr.CheckResult("git diff --check", "passed")],
    )
    monkeypatch.setattr(
        dlbr,
        "run_lifecycle_sidecar",
        lambda timeout_s=180: dlbr.LifecycleResult(
            enabled=True,
            exit_code=0,
            agents=[{"name": raw_user, "status": "completed-with-output"}],
        ),
    )
    monkeypatch.setattr(
        dlbr,
        "build_feature_suggestion",
        lambda record_history=True: {
            "title": f"{raw_user} 文件整理",
            "reason": raw_chat * 8,
        },
    )

    sent = []
    monkeypatch.setattr(dlbr, "_send_discord", lambda msg: sent.append(msg) or True)

    rc = dlbr.main(["--dry-run"])

    assert rc == 0
    stdout = capsys.readouterr().out
    state_text = state_path.read_text()
    for text in [stdout, state_text]:
        assert raw_user not in text
        assert (raw_chat * 2) not in text
    assert "U***" in stdout


def test_build_feature_suggestion_forces_local_only(monkeypatch):
    import daily_briefing_discord as dbd

    calls = []

    def fake_line_bot_suggestions(**kwargs):
        calls.append(kwargs)
        return "💡 **LINE bot 每日推薦**：本地候選 — 不送近期聊天到外部 AI。"

    monkeypatch.setattr(dbd, "line_bot_suggestions", fake_line_bot_suggestions)

    suggestion = dlbr.build_feature_suggestion(record_history=False)

    assert suggestion["title"] == "本地候選"
    assert calls and calls[0]["use_ai"] is False


def test_sanitize_redacts_common_secret_shapes():
    text = (
        "key=abc Bearer token-value X-Goog-Api-Key: " + "AIza" + "1" * 20 + " "
        "Authorization: Bot xyz https://discord.com/api/webhooks/123/secret "
        "postgres://user:pass@example/db " + "sk-" + "1" * 24
    )

    out = dlbr._sanitize(text)

    assert "abc" not in out
    assert "token-value" not in out
    assert "AIza" + "1" * 20 not in out
    assert "Bot xyz" not in out
    assert "/secret" not in out
    assert ":pass@" not in out
    assert "sk-1234567890" not in out


def test_daily_line_bot_review_registered():
    import jobs_config

    spec = jobs_config.JOB_REGISTRY["daily-line-bot-review"]
    command_text = " ".join(spec.command)

    assert "jobs/daily_line_bot_review.py" in command_text
    assert spec.cwd == str(BASE)
    assert spec.timeout == 420
    assert "Discord" in spec.description
