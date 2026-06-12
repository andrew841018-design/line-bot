"""Tests for jobs_router (dispatcher correctness + security)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBS_MASTER_TOKEN", "test-master-token-fixture")
    monkeypatch.setenv("JOBS_ROUTES_ENABLED", "1")
    # Redirect state/log dirs to tmp
    import jobs_router as jr
    import jobs_config as jc
    monkeypatch.setattr(jr, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(jr, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(jr, "LOCK_DIR", tmp_path / "locks")
    (tmp_path / "locks").mkdir()
    # TestClient sets client.host = "testclient"; allow it
    monkeypatch.setattr(jr, "_ALLOWED_IPS", {"127.0.0.1", "::1", "::ffff:127.0.0.1", "testclient"})
    # Inject a safe stub for "daily-briefing-discord" — this job was removed
    # from prod registry (commit 7e00e16) but tests still use the name as a
    # stable fixture identity. Stub avoids triggering any real subprocess.
    monkeypatch.setitem(
        jc.JOB_REGISTRY,
        "daily-briefing-discord",
        jc.JobSpec(
            command=["/bin/sh", "-c", "echo stub"],
            cwd=None,
            env={"PATH": "/bin:/usr/bin"},
            timeout=5,
            description="test stub (injected)",
        ),
    )
    yield


@pytest.fixture
def client():
    import jobs_router as jr
    app = FastAPI()
    app.include_router(jr.router)
    return TestClient(app)


def _token_for(job_name: str) -> str:
    return hmac.new(b"test-master-token-fixture", job_name.encode(), hashlib.sha256).hexdigest()


def test_list_jobs_loopback_ok(client):
    r = client.get("/jobs")
    assert r.status_code == 200
    data = r.json()
    assert "jobs" in data
    assert any(j["name"] == "daily-briefing-discord" for j in data["jobs"])


def test_list_jobs_rejects_origin(client):
    r = client.get("/jobs", headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403


def test_trigger_missing_token(client):
    r = client.post("/jobs/daily-briefing-discord")
    assert r.status_code == 401


def test_trigger_wrong_token(client):
    r = client.post(
        "/jobs/daily-briefing-discord",
        headers={"X-Job-Token": "wrong"},
    )
    assert r.status_code == 401


def test_trigger_unknown_job(client):
    job = "nonexistent-job-name"
    r = client.post(
        f"/jobs/{job}",
        headers={"X-Job-Token": _token_for(job)},
    )
    assert r.status_code == 404


def test_removed_soxl_code_review_job_stays_unregistered(client):
    job = "soxl-monitor-code-review"
    r = client.post(
        f"/jobs/{job}",
        headers={"X-Job-Token": _token_for(job)},
    )
    assert r.status_code == 404


def test_removed_soxl_health_check_job_stays_unregistered(client):
    job = "soxl-monitor-health-check"
    r = client.post(
        f"/jobs/{job}",
        headers={"X-Job-Token": _token_for(job)},
    )
    assert r.status_code == 404


def test_trigger_invalid_name_format(client):
    # Uppercase rejected by regex
    r = client.post(
        "/jobs/BadName",
        headers={"X-Job-Token": _token_for("BadName")},
    )
    assert r.status_code == 400


def test_trigger_path_traversal_blocked(client):
    # URL-encoded ../etc should be 400 or 404, never 200
    r = client.post(
        "/jobs/..%2Fetc",
        headers={"X-Job-Token": _token_for("../etc")},
    )
    assert r.status_code in (400, 404)


def test_trigger_origin_header_blocked(client):
    job = "daily-briefing-discord"
    r = client.post(
        f"/jobs/{job}",
        headers={"X-Job-Token": _token_for(job), "Origin": "http://x.com"},
    )
    assert r.status_code == 403


def test_per_job_token_isolation(client):
    """Token valid for job A must NOT authorize job B."""
    job_a_token = _token_for("daily-briefing-discord")
    r = client.post(
        "/jobs/line-bot-event-reminder",
        headers={"X-Job-Token": job_a_token},
    )
    assert r.status_code == 401


def test_discord_alert_route_sends_with_purpose_token(client, monkeypatch):
    import jobs_router as jr

    sent = []
    monkeypatch.setattr(jr, "_deliver_discord_alert", lambda msg: sent.append(msg) or True)

    r = client.post(
        "/jobs/discord-alert",
        headers={"X-Job-Token": _token_for("discord-alert")},
        json={"content": "n8n failed"},
    )
    assert r.status_code == 200
    assert r.json() == {"sent": True}
    assert sent == ["n8n failed"]


def test_discord_alert_route_rejects_job_token(client, monkeypatch):
    import jobs_router as jr

    monkeypatch.setattr(jr, "_deliver_discord_alert", lambda msg: True)
    r = client.post(
        "/jobs/discord-alert",
        headers={"X-Job-Token": _token_for("daily-briefing-discord")},
        json={"content": "n8n failed"},
    )
    assert r.status_code == 401


def test_discord_alert_rejects_wrong_token_before_body_parse(client, monkeypatch):
    import jobs_router as jr

    monkeypatch.setattr(jr, "_deliver_discord_alert", lambda msg: True)
    r = client.post(
        "/jobs/discord-alert",
        headers={"X-Job-Token": _token_for("daily-briefing-discord")},
        content=b"not-json" * 2000,
    )
    assert r.status_code == 401


def test_discord_alert_route_rejects_origin(client, monkeypatch):
    import jobs_router as jr

    monkeypatch.setattr(jr, "_deliver_discord_alert", lambda msg: True)
    r = client.post(
        "/jobs/discord-alert",
        headers={
            "X-Job-Token": _token_for("discord-alert"),
            "Origin": "http://evil.example.com",
        },
        json={"content": "n8n failed"},
    )
    assert r.status_code == 403


def test_discord_alert_route_rejects_non_loopback(client, monkeypatch):
    import jobs_router as jr

    monkeypatch.setattr(jr, "_ALLOWED_IPS", {"127.0.0.1"})
    monkeypatch.setattr(jr, "_deliver_discord_alert", lambda msg: True)
    r = client.post(
        "/jobs/discord-alert",
        headers={"X-Job-Token": _token_for("discord-alert")},
        json={"content": "n8n failed"},
    )
    assert r.status_code == 403


def test_discord_alert_route_rejects_empty_content(client, monkeypatch):
    import jobs_router as jr

    monkeypatch.setattr(jr, "_deliver_discord_alert", lambda msg: True)
    r = client.post(
        "/jobs/discord-alert",
        headers={"X-Job-Token": _token_for("discord-alert")},
        json={"content": "   "},
    )
    assert r.status_code == 400


def test_discord_alert_route_rejects_oversize_payload(client, monkeypatch):
    import jobs_router as jr

    monkeypatch.setattr(jr, "_deliver_discord_alert", lambda msg: True)
    r = client.post(
        "/jobs/discord-alert",
        headers={"X-Job-Token": _token_for("discord-alert")},
        content=b"x" * (jr._DISCORD_ALERT_BODY_LIMIT + 1),
    )
    assert r.status_code == 413


def test_discord_alert_sanitizes_before_send(client, monkeypatch):
    import jobs_router as jr

    sent = []
    monkeypatch.setattr(jr, "_deliver_discord_alert", lambda msg: sent.append(msg) or True)
    r = client.post(
        "/jobs/discord-alert",
        headers={"X-Job-Token": _token_for("discord-alert")},
        json={"content": "Authorization: Bot super-secret-token failed"},
    )
    assert r.status_code == 200
    assert "super-secret-token" not in sent[0]
    assert "REDACTED" in sent[0]


def test_discord_alert_truncates_before_send(client, monkeypatch):
    import jobs_router as jr

    sent = []
    monkeypatch.setattr(jr, "_deliver_discord_alert", lambda msg: sent.append(msg) or True)
    r = client.post(
        "/jobs/discord-alert",
        headers={"X-Job-Token": _token_for("discord-alert")},
        json={"content": "x" * 5000},
    )
    assert r.status_code == 200
    assert len(sent[0]) == jr._DISCORD_ALERT_LIMIT
    assert sent[0].endswith("... (truncated)")


def test_discord_alert_delivery_failure_returns_502(client, monkeypatch):
    import jobs_router as jr

    monkeypatch.setattr(jr, "_deliver_discord_alert", lambda msg: False)
    r = client.post(
        "/jobs/discord-alert",
        headers={"X-Job-Token": _token_for("discord-alert")},
        json={"content": "n8n failed"},
    )
    assert r.status_code == 502


def test_last_run_never_executed(client):
    job = "daily-briefing-discord"
    r = client.get(
        f"/jobs/{job}/last-run",
        headers={"X-Job-Token": _token_for(job)},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "never_run"


def test_rate_limit_after_recent_run(client, tmp_path):
    import jobs_router as jr
    job = "daily-briefing-discord"
    # Inject recent state
    jr.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "ok": True,
        "status": "completed",
        "started_at_epoch": time.time() - 10,  # 10s ago
    }
    (jr.STATE_DIR / f"last_run_{job}.json").write_text(json.dumps(state))
    r = client.post(
        f"/jobs/{job}",
        headers={"X-Job-Token": _token_for(job)},
    )
    assert r.status_code == 429


def test_rate_limit_passes_after_cooldown(client, tmp_path):
    import jobs_router as jr
    job = "daily-briefing-discord"
    jr.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "ok": True,
        "status": "completed",
        "started_at_epoch": time.time() - 120,  # 2 min ago
    }
    (jr.STATE_DIR / f"last_run_{job}.json").write_text(json.dumps(state))
    r = client.post(
        f"/jobs/{job}",
        headers={"X-Job-Token": _token_for(job)},
    )
    assert r.status_code == 202


def test_atomic_write_via_tmp_replace(tmp_path):
    import jobs_router as jr
    target = tmp_path / "state" / "x.json"
    jr._atomic_write(target, {"a": 1})
    assert target.read_text() == '{\n  "a": 1\n}'
    # tmp must not linger
    assert not (tmp_path / "state" / "x.tmp").exists()


def test_sanitize_strips_api_key():
    import jobs_router as jr
    s = "https://api.example.com/v1?key=AIzaSyXXXX&foo=bar"
    out = jr._sanitize(s)
    assert "AIzaSyXXXX" not in out
    assert "REDACTED" in out


def test_sanitize_strips_bearer():
    import jobs_router as jr
    s = "Authorization: Bearer abc123def456 ..."
    out = jr._sanitize(s)
    assert "abc123def456" not in out
    assert "REDACTED" in out


def test_startup_sweep_marks_dead_pid_interrupted(tmp_path):
    import jobs_router as jr
    jr.STATE_DIR.mkdir(parents=True, exist_ok=True)
    # PID 99999999 should not exist
    state = {
        "ok": None,
        "status": "running",
        "pid": 99999999,
        "started_at_epoch": time.time() - 1000,
    }
    (jr.STATE_DIR / "last_run_xxx.json").write_text(json.dumps(state))
    jr.startup_sweep()
    updated = json.loads((jr.STATE_DIR / "last_run_xxx.json").read_text())
    assert updated["status"] == "interrupted"
    assert updated["ok"] is False


def test_startup_sweep_leaves_live_pid_running(tmp_path):
    import jobs_router as jr
    jr.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "ok": None,
        "status": "running",
        "pid": os.getpid(),  # alive
        "started_at_epoch": time.time(),
    }
    (jr.STATE_DIR / "last_run_yyy.json").write_text(json.dumps(state))
    jr.startup_sweep()
    updated = json.loads((jr.STATE_DIR / "last_run_yyy.json").read_text())
    assert updated["status"] == "running"  # not touched


def test_trigger_returns_202_with_valid_token(client, tmp_path, monkeypatch):
    """Use a tiny no-op subprocess to verify the 202 path."""
    import jobs_router as jr
    import jobs_config as jc
    # Override registry entry to a tiny safe command
    monkeypatch.setitem(
        jc.JOB_REGISTRY,
        "daily-briefing-discord",
        jc.JobSpec(
            command=["/bin/sh", "-c", "echo hi"],
            cwd=None,
            env={"PATH": "/bin:/usr/bin"},
            timeout=5,
            description="test stub",
        ),
    )
    job = "daily-briefing-discord"
    r = client.post(f"/jobs/{job}", headers={"X-Job-Token": _token_for(job)})
    assert r.status_code == 202
    # Wait briefly for BG task
    for _ in range(20):
        state_p = jr.STATE_DIR / f"last_run_{job}.json"
        if state_p.exists():
            data = json.loads(state_p.read_text())
            if data.get("status") in ("completed", "failed", "timeout"):
                break
        time.sleep(0.2)
    state = json.loads((jr.STATE_DIR / f"last_run_{job}.json").read_text())
    assert state["status"] == "completed"
    assert state["returncode"] == 0


def test_token_compare_is_constant_time():
    """compare_digest is used (verified by source inspection)."""
    import jobs_router as jr
    import inspect
    src = inspect.getsource(jr._check_token)
    assert "compare_digest" in src


def test_startup_sweep_creates_lock_dir(tmp_path, monkeypatch):
    """Regression: LOCK_DIR must exist after startup_sweep, even on cold install."""
    import jobs_router as jr
    # Point LOCK_DIR to a non-existing path
    target = tmp_path / "fresh_locks"
    monkeypatch.setattr(jr, "LOCK_DIR", target)
    assert not target.exists()
    jr.startup_sweep()
    assert target.exists() and target.is_dir()


def test_sanitize_discord_webhook():
    import jobs_router as jr
    s = "Posted to https://discord.com/api/webhooks/123456/abcdef-secret-XYZ ok"
    out = jr._sanitize(s)
    assert "abcdef-secret-XYZ" not in out
    assert "REDACTED" in out


def test_sanitize_postgres_password():
    import jobs_router as jr
    s = "psycopg2.OperationalError: postgres://user:s3cretpass@localhost/db error"
    out = jr._sanitize(s)
    assert "s3cretpass" not in out
    assert "REDACTED" in out


def test_sanitize_master_token_substring(monkeypatch):
    import jobs_router as jr
    monkeypatch.setenv("JOBS_MASTER_TOKEN", "TESTMASTER123456")
    s = "config dump: JOBS_MASTER_TOKEN=TESTMASTER123456 visible"
    out = jr._sanitize(s)
    assert "TESTMASTER123456" not in out
    assert "[MASTER_TOKEN]" in out
