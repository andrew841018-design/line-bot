from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import pytest

import cloud_run_plan as crp


def test_job_token_matches_hmac_sha256():
    expected = hmac.new(
        b"test-master",
        b"line-bot-event-reminder",
        hashlib.sha256,
    ).hexdigest()

    assert crp.job_token("test-master", "line-bot-event-reminder") == expected


def test_job_token_rejects_invalid_job_name():
    with pytest.raises(ValueError):
        crp.job_token("test-master", "../bad")


def test_default_deploy_plan_is_muted_and_small():
    plan = crp.build_plan(project="demo-project", region="asia-east1", service="line-bot")
    joined = "\n".join(plan.commands)

    assert "gcloud run deploy line-bot" in joined
    assert "--max-instances 1" in joined
    assert "--min-instances 0" in joined
    assert "BOT_MUTED=true" in joined
    assert "JOBS_ROUTES_ENABLED=0" in joined
    assert "LINE_CHANNEL_ACCESS_TOKEN" not in joined
    assert "GEMINI_API_KEY" not in joined


def test_scheduler_command_contains_token_header_without_master_secret():
    cmd = crp.scheduler_command(
        service_url="https://line-bot.example.run.app/",
        job_name="line-bot-event-reminder",
        schedule="0 7 * * *",
        timezone="Asia/Taipei",
        region="asia-east1",
        token="per-job-token",
    )

    assert "https://line-bot.example.run.app/jobs/line-bot-event-reminder" in cmd
    assert "X-Job-Token=per-job-token" in cmd
    assert "JOBS_MASTER_TOKEN" not in cmd


def test_scheduler_command_preserves_service_url_shell_variable():
    cmd = crp.scheduler_command(
        service_url="$SERVICE_URL",
        job_name="line-bot-event-reminder",
        schedule="0 7 * * *",
        timezone="Asia/Taipei",
        region="asia-east1",
        token="per-job-token",
    )

    assert '--uri "$SERVICE_URL/jobs/line-bot-event-reminder"' in cmd


def test_secret_commands_use_secret_names_not_values():
    commands = "\n".join(crp.secret_commands(service="line-bot", region="asia-east1"))

    assert "line-bot-line-channel-secret" in commands
    assert "LINE_CHANNEL_SECRET=line-bot-line-channel-secret:latest" in commands
    assert "LINE_CHANNEL_ACCESS_TOKEN=" in commands
    assert "LINE_CHANNEL_ID=" not in commands
    assert "ALLOWED_GROUP_IDS=" not in commands
    assert "GROQ_API_KEY=" not in commands
    assert "<SECRET_VALUE>" in commands
    assert "dummy" not in commands


def test_secret_commands_can_include_optional_groq_key():
    commands = "\n".join(
        crp.secret_commands(service="line-bot", region="asia-east1", include_optional=True)
    )

    assert "GROQ_API_KEY=line-bot-groq-api-key:latest" in commands


def test_require_gcloudignore_passes_current_file():
    assert crp.require_gcloudignore(Path(__file__).resolve().parent) == []


def test_require_gcloudignore_reports_missing_runtime_patterns(tmp_path):
    (tmp_path / ".gcloudignore").write_text(".env\n")

    findings = crp.require_gcloudignore(tmp_path)

    assert any("line_bot.db" in finding for finding in findings)
