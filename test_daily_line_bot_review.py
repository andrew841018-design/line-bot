"""Unit tests for jobs/daily_line_bot_review.py."""
from __future__ import annotations

import json
import multiprocessing
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE / "jobs"))

import daily_line_bot_review as dlbr  # noqa: E402


def _patch_state(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    state_path = state_dir / "daily_line_bot_review_outcome.json"
    monkeypatch.setattr(dlbr, "STATE_DIR", state_dir)
    monkeypatch.setattr(dlbr, "OUTCOME_PATH", state_path, raising=False)
    monkeypatch.setattr(
        dlbr,
        "PROPOSAL_LEDGER_PATH",
        state_dir / "daily_line_bot_proposal_delivery.json",
        raising=False,
    )
    monkeypatch.setattr(
        dlbr,
        "PROPOSAL_LOCK_PATH",
        state_dir / "daily_line_bot_proposal_delivery.lock",
        raising=False,
    )
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


def test_local_checks_include_bounded_github_privacy_audit(monkeypatch):
    calls = []

    def fake_run(name, command, *, cwd, timeout_s):
        calls.append((name, command, cwd, timeout_s))
        return dlbr.CheckResult(name, "passed", "privacy_audit=passed findings=0", 0)

    monkeypatch.setattr(dlbr, "_run_command", fake_run)

    results = dlbr.run_local_checks()

    privacy = next(call for call in calls if call[0] == "GitHub privacy audit")
    assert privacy[1].count("--scope") == 3
    assert privacy[1][-1] == "remote"
    assert privacy[3] == 120
    assert any(result.name == "GitHub privacy audit" for result in results)


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
    assert "圖片收據到期提醒" not in stdout
    assert "功能建議" not in stdout
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
    assert "功能建議" not in sent[0]
    assert "圖片收據到期提醒" not in sent[0]
    state = json.loads(state_path.read_text())
    assert state["status"] == "completed"
    assert state["summary"]["discord_sent"] is True


def test_scheduled_review_never_builds_a_feature_proposal(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    _patch_success(monkeypatch)
    monkeypatch.setattr(
        dlbr,
        "build_feature_suggestion",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("proposal builder ran")),
    )
    sent = []
    monkeypatch.setattr(dlbr, "_send_discord", lambda msg: sent.append(msg) or True)

    rc = dlbr.main([])

    assert rc == 0
    assert len(sent) == 1
    assert "功能建議" not in sent[0]


def test_attention_delivery_is_successful_execution_with_truthful_outcome(
    tmp_path, monkeypatch
):
    state_path = _patch_state(monkeypatch, tmp_path)
    monkeypatch.setattr(
        dlbr,
        "run_local_checks",
        lambda: [dlbr.CheckResult("focused tests", "failed", "1 failed", 1)],
    )
    monkeypatch.setattr(
        dlbr,
        "run_lifecycle_sidecar",
        lambda timeout_s=180: dlbr.LifecycleResult(True, 0, []),
    )
    monkeypatch.setattr(
        dlbr,
        "build_feature_suggestion",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("proposal builder ran")),
    )
    sent = []
    monkeypatch.setattr(dlbr, "_send_discord", lambda msg: sent.append(msg) or True)

    rc = dlbr.main([])

    assert rc == 0
    assert "狀態：ATTENTION" in sent[0]
    state = json.loads(state_path.read_text())
    assert state["ok"] is False
    assert state["status"] == "review_attention"
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


def test_main_returns_1_when_discord_delivery_is_unknown(tmp_path, monkeypatch):
    state_path = _patch_state(monkeypatch, tmp_path)
    _patch_success(monkeypatch)
    monkeypatch.setattr(
        dlbr,
        "_send_discord",
        lambda msg: (_ for _ in ()).throw(TimeoutError("unknown delivery")),
    )

    rc = dlbr.main([])

    assert rc == 0
    state = json.loads(state_path.read_text())
    assert state["status"] == "discord_delivery_unknown"
    assert state["ok"] is False
    assert state["summary"]["discord_delivery_status"] == "pending_unknown"


def test_successful_review_delivery_is_not_retried_when_outcome_write_fails(
    tmp_path, monkeypatch
):
    state_path = _patch_state(monkeypatch, tmp_path)
    _patch_success(monkeypatch)
    monkeypatch.setattr(dlbr, "_send_discord", lambda msg: True)
    original_write = dlbr._write_state
    writes = 0

    def fail_final_write(record):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("disk full")
        return original_write(record)

    monkeypatch.setattr(dlbr, "_write_state", fail_final_write)

    assert dlbr.main([]) == 0
    state = json.loads(state_path.read_text())
    assert state["status"] == "discord_delivery_pending"


def test_daily_proposal_definite_failure_is_not_retried_same_day(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    now = datetime(2026, 8, 18, 9, 15, tzinfo=ZoneInfo("Asia/Taipei"))
    calls = []

    failed = dlbr.send_daily_proposal_once(
        "sanitized proposal",
        title="daily proposal",
        now=now,
        sender=lambda message: calls.append(message) or False,
    )
    skipped = dlbr.send_daily_proposal_once(
        "sanitized proposal",
        title="daily proposal",
        now=now,
        sender=lambda message: (_ for _ in ()).throw(AssertionError("resent")),
    )

    assert failed.status == "definite_failed"
    assert skipped.status == "definite_failed"
    assert len(calls) == 1


def test_daily_proposal_unknown_exception_leaves_pending_and_fails_closed(
    tmp_path, monkeypatch
):
    _patch_state(monkeypatch, tmp_path)
    now = datetime(2026, 8, 18, 9, 15, tzinfo=ZoneInfo("Asia/Taipei"))

    first = dlbr.send_daily_proposal_once(
        "sanitized proposal",
        title="daily proposal",
        now=now,
        sender=lambda message: (_ for _ in ()).throw(TimeoutError("unknown delivery")),
    )
    second = dlbr.send_daily_proposal_once(
        "sanitized proposal",
        title="daily proposal",
        now=now,
        sender=lambda message: (_ for _ in ()).throw(AssertionError("resent")),
    )

    assert first.status == "pending_unknown"
    assert second.status == "pending_unknown"


def test_daily_proposal_unknown_sender_status_fails_closed(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    now = datetime(2026, 8, 18, 9, 15, tzinfo=ZoneInfo("Asia/Taipei"))

    first = dlbr.send_daily_proposal_once(
        "sanitized proposal",
        title="daily proposal",
        now=now,
        sender=lambda message: "unexpected_status",
    )
    second = dlbr.send_daily_proposal_once(
        "sanitized proposal",
        title="daily proposal",
        now=now,
        sender=lambda message: (_ for _ in ()).throw(AssertionError("resent")),
    )

    assert first.status == "pending_unknown"
    assert second.status == "pending_unknown"


def test_daily_proposal_corrupt_ledger_fails_closed(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    dlbr.PROPOSAL_LEDGER_PATH.parent.mkdir(parents=True)
    dlbr.PROPOSAL_LEDGER_PATH.write_text("not-json", encoding="utf-8")

    result = dlbr.send_daily_proposal_once(
        "sanitized proposal",
        title="daily proposal",
        now=datetime(2026, 8, 18, 9, 15, tzinfo=ZoneInfo("Asia/Taipei")),
        sender=lambda message: (_ for _ in ()).throw(AssertionError("sent")),
    )

    assert result.status == "ledger_corrupt"


def test_daily_proposal_schema_mismatch_fails_closed(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    dlbr.PROPOSAL_LEDGER_PATH.parent.mkdir(parents=True)
    dlbr.PROPOSAL_LEDGER_PATH.write_text(
        json.dumps(
            {
                "schema": 999,
                "purpose": dlbr.PROPOSAL_PURPOSE,
                "date": "2026-08-18",
                "status": "sent",
                "title_hash": "0" * 64,
                "started_at": "2026-08-18T09:15:00+08:00",
            }
        ),
        encoding="utf-8",
    )

    result = dlbr.send_daily_proposal_once(
        "sanitized proposal",
        title="daily proposal",
        now=datetime(2026, 8, 18, 9, 15, tzinfo=ZoneInfo("Asia/Taipei")),
        sender=lambda message: (_ for _ in ()).throw(AssertionError("sent")),
    )

    assert result.status == "ledger_corrupt"


def test_daily_proposal_malformed_valid_shape_fails_closed(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    dlbr.PROPOSAL_LEDGER_PATH.parent.mkdir(parents=True)
    dlbr.PROPOSAL_LEDGER_PATH.write_text(
        json.dumps(
            {
                "schema": dlbr.DELIVERY_SCHEMA,
                "purpose": dlbr.PROPOSAL_PURPOSE,
                "date": "not-a-date",
                "status": "sent",
                "title_hash": "not-a-hash",
                "started_at": "not-a-timestamp",
                "finished_at": "not-a-timestamp",
            }
        ),
        encoding="utf-8",
    )

    result = dlbr.send_daily_proposal_once(
        "sanitized proposal",
        title="daily proposal",
        now=datetime(2026, 8, 18, 9, 15, tzinfo=ZoneInfo("Asia/Taipei")),
        sender=lambda message: (_ for _ in ()).throw(AssertionError("sent")),
    )

    assert result.status == "ledger_corrupt"


def test_daily_proposal_malformed_field_types_fail_closed(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    now = datetime(2026, 8, 18, 9, 15, tzinfo=ZoneInfo("Asia/Taipei"))
    base = dlbr._delivery_record(
        purpose=dlbr.PROPOSAL_PURPOSE,
        day="2026-08-18",
        status="sent",
        title="daily proposal",
        now=now,
    )
    base["finished_at"] = now.isoformat()

    for field, invalid in (
        ("schema", True),
        ("schema", 1.0),
        ("status", ["sent"]),
        ("finished_at", {"timestamp": now.isoformat()}),
    ):
        record = dict(base)
        record[field] = invalid
        dlbr._atomic_write_private_json(dlbr.PROPOSAL_LEDGER_PATH, record)
        result = dlbr.send_daily_proposal_once(
            "sanitized proposal",
            title="daily proposal",
            now=now,
            sender=lambda message: (_ for _ in ()).throw(AssertionError("sent")),
        )
        assert result.status == "ledger_corrupt"


def test_daily_proposal_future_ledger_fails_closed(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    future = datetime(2026, 8, 19, 9, 15, tzinfo=ZoneInfo("Asia/Taipei"))
    record = dlbr._delivery_record(
        purpose=dlbr.PROPOSAL_PURPOSE,
        day="2026-08-19",
        status="sent",
        title="future",
        now=future,
    )
    record["finished_at"] = future.isoformat()
    dlbr._atomic_write_private_json(dlbr.PROPOSAL_LEDGER_PATH, record)

    result = dlbr.send_daily_proposal_once(
        "sanitized proposal",
        title="daily proposal",
        now=datetime(2026, 8, 18, 9, 15, tzinfo=ZoneInfo("Asia/Taipei")),
        sender=lambda message: (_ for _ in ()).throw(AssertionError("sent")),
    )

    assert result.status == "ledger_corrupt"


def test_daily_proposal_state_files_are_private(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)

    result = dlbr.send_daily_proposal_once(
        "sanitized proposal",
        title="daily proposal",
        now=datetime(2026, 8, 18, 9, 15, tzinfo=ZoneInfo("Asia/Taipei")),
        sender=lambda message: True,
    )

    assert result.status == "sent"
    assert dlbr.PROPOSAL_LEDGER_PATH.stat().st_mode & 0o777 == 0o600
    assert dlbr.PROPOSAL_LOCK_PATH.stat().st_mode & 0o777 == 0o600
    assert dlbr.PROPOSAL_LEDGER_PATH.parent.stat().st_mode & 0o777 == 0o700


def test_daily_proposal_sanitizes_outbound_message(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    delivered = []

    result = dlbr.send_daily_proposal_once(
        "C" + "a" * 32 + " token=secret-value",
        title="daily proposal",
        now=datetime(2026, 8, 18, 9, 15, tzinfo=ZoneInfo("Asia/Taipei")),
        sender=lambda message: delivered.append(message) or True,
    )

    assert result.status == "sent"
    assert delivered == ["C*** token=[REDACTED]"]


def test_daily_proposal_final_write_failure_keeps_pending(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    original_write = dlbr._atomic_write_private_json
    writes = 0

    def fail_second_write(path, record):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("disk full")
        return original_write(path, record)

    monkeypatch.setattr(dlbr, "_atomic_write_private_json", fail_second_write)
    now = datetime(2026, 8, 18, 9, 15, tzinfo=ZoneInfo("Asia/Taipei"))
    first = dlbr.send_daily_proposal_once(
        "sanitized proposal", title="daily proposal", now=now, sender=lambda msg: True
    )
    second = dlbr.send_daily_proposal_once(
        "sanitized proposal",
        title="daily proposal",
        now=now,
        sender=lambda msg: (_ for _ in ()).throw(AssertionError("resent")),
    )

    assert first.status == "pending_unknown"
    assert second.status == "pending_unknown"
    assert json.loads(dlbr.PROPOSAL_LEDGER_PATH.read_text())["status"] == "pending"


def test_daily_proposal_uses_taipei_calendar_day(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    calls = []
    before_midnight_utc = datetime(2026, 8, 18, 15, 59, tzinfo=ZoneInfo("UTC"))
    after_midnight_utc = datetime(2026, 8, 18, 16, 1, tzinfo=ZoneInfo("UTC"))

    first = dlbr.send_daily_proposal_once(
        "proposal one",
        title="one",
        now=before_midnight_utc,
        sender=lambda message: calls.append(message) or True,
    )
    second = dlbr.send_daily_proposal_once(
        "proposal two",
        title="two",
        now=after_midnight_utc,
        sender=lambda message: calls.append(message) or True,
    )

    assert first.status == "sent"
    assert second.status == "sent"
    assert len(calls) == 2


def test_daily_proposal_multiprocess_race_sends_once(tmp_path, monkeypatch):
    if "fork" not in multiprocessing.get_all_start_methods():
        return
    _patch_state(monkeypatch, tmp_path)
    marker = tmp_path / "send_calls"
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()
    statuses = ctx.Queue()

    def sender(_message):
        with marker.open("a", encoding="utf-8") as fh:
            fh.write("sent\n")
            fh.flush()
            os.fsync(fh.fileno())
        time.sleep(0.15)
        return True

    def worker():
        start.wait()
        result = dlbr.send_daily_proposal_once(
            "sanitized proposal",
            title="daily proposal",
            now=datetime(2026, 8, 18, 9, 15, tzinfo=ZoneInfo("Asia/Taipei")),
            sender=sender,
        )
        statuses.put(result.status)

    processes = [ctx.Process(target=worker) for _ in range(2)]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(5)
        assert process.exitcode == 0

    assert sorted(statuses.get(timeout=1) for _ in processes) == ["already_sent", "sent"]
    assert marker.read_text(encoding="utf-8").splitlines() == ["sent"]


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
    assert dlbr._sanitize("C" + "a" * 32) == "C***"


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
    assert "suggestion" not in spec.description.lower()


def test_proposal_cli_is_the_deduplicated_production_entry(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    payload = tmp_path / "proposal.json"
    payload.write_text(
        json.dumps({"title": "daily proposal", "message": "sanitized proposal"}),
        encoding="utf-8",
    )
    payload.chmod(0o600)
    delivered = []
    monkeypatch.setattr(
        dlbr,
        "_send_discord_proposal",
        lambda message: delivered.append(message) or "sent",
    )

    assert dlbr.main(["--send-proposal-json", str(payload)]) == 0
    assert dlbr.main(["--send-proposal-json", str(payload)]) == 0
    assert delivered == ["sanitized proposal"]


def test_proposal_cli_rejects_dry_run_without_sending(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    payload = tmp_path / "proposal.json"
    payload.write_text(
        json.dumps({"title": "daily proposal", "message": "sanitized proposal"}),
        encoding="utf-8",
    )
    payload.chmod(0o600)
    monkeypatch.setattr(
        dlbr,
        "_send_discord_proposal",
        lambda message: (_ for _ in ()).throw(AssertionError("sent")),
    )

    assert dlbr.main(["--dry-run", "--send-proposal-json", str(payload)]) == 1
    assert not dlbr.PROPOSAL_LEDGER_PATH.exists()


def test_proposal_cli_rejects_non_private_or_symlink_payload(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    payload = tmp_path / "proposal.json"
    payload.write_text(
        json.dumps({"title": "daily proposal", "message": "sanitized proposal"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        dlbr,
        "_send_discord_proposal",
        lambda message: (_ for _ in ()).throw(AssertionError("sent")),
    )

    payload.chmod(0o644)
    assert dlbr.main(["--send-proposal-json", str(payload)]) == 1

    payload.chmod(0o600)
    symlink = tmp_path / "proposal-link.json"
    symlink.symlink_to(payload)
    assert dlbr.main(["--send-proposal-json", str(symlink)]) == 1
    assert not dlbr.PROPOSAL_LEDGER_PATH.exists()


def test_proposal_cli_rejects_oversized_private_payload(tmp_path, monkeypatch):
    _patch_state(monkeypatch, tmp_path)
    payload = tmp_path / "proposal.json"
    payload.write_text(
        json.dumps({"title": "daily proposal", "message": "x" * (33 * 1024)}),
        encoding="utf-8",
    )
    payload.chmod(0o600)
    monkeypatch.setattr(
        dlbr,
        "_send_discord_proposal",
        lambda message: (_ for _ in ()).throw(AssertionError("sent")),
    )

    assert dlbr.main(["--send-proposal-json", str(payload)]) == 1
    assert not dlbr.PROPOSAL_LEDGER_PATH.exists()
