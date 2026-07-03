from __future__ import annotations
from dataclasses import dataclass
import os
from pathlib import Path
import sys

@dataclass(frozen=True)
class JobSpec:
    command: list[str]
    cwd: str | None
    env: dict[str, str]
    timeout: int
    description: str = ""

_BASE_PATH = os.environ.get("LINE_BOT_JOB_PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
_HERE = Path(__file__).resolve().parent
_USER_SCRIPTS_DIR = Path(os.environ.get("LINE_BOT_USER_SCRIPTS_DIR", "/Users/andrew/scripts"))
_LB_PYTHON = os.environ.get("LINE_BOT_PYTHON", sys.executable)
_HOME = os.environ.get("HOME", "/tmp")
_USER = os.environ.get("USER", os.environ.get("LOGNAME", "line-bot"))


def _base_env() -> dict[str, str]:
    return {"PATH": _BASE_PATH, "HOME": _HOME, "USER": _USER, "LOGNAME": _USER}


def _lb_script(name: str) -> str:
    return str(_HERE / name)


def _user_script(name: str) -> str:
    return str(_USER_SCRIPTS_DIR / name)

JOB_REGISTRY: dict[str, JobSpec] = {}

JOB_REGISTRY["line-bot-event-reminder"] = JobSpec(
    command=[_LB_PYTHON, _lb_script("event_reminder.py")],
    cwd=str(_HERE),
    env=_base_env(),
    timeout=30,
    description="Daily 07:00 event reminder",
)

JOB_REGISTRY["line-bot-update-push"] = JobSpec(
    command=[_LB_PYTHON, _lb_script("line_bot_update_push.py")],
    cwd=str(_HERE),
    env=_base_env(),
    timeout=30,
    description="Daily 09:00 update notify",
)

JOB_REGISTRY["line-bot-feedback-push"] = JobSpec(
    command=["/bin/bash", _user_script("run_feedback_push.sh")],
    cwd=None,
    env=_base_env(),
    timeout=30,
    description="Sunday 20:00 feedback notify",
)

JOB_REGISTRY["line-bot-weekly-summary"] = JobSpec(
    command=[_LB_PYTHON, _lb_script("weekly_summary.py")],
    cwd=str(_HERE),
    env=_base_env(),
    timeout=60,
    description="Sunday 20:00 weekly summary",
)

JOB_REGISTRY["line-bot-feedback-process"] = JobSpec(
    command=["/bin/bash", _user_script("run_feedback_process.sh")],
    cwd=None,
    env=_base_env(),
    timeout=60,
    description="Tuesday 02:00 feedback process",
)

JOB_REGISTRY["line-bot-health"] = JobSpec(
    command=["/bin/bash", _user_script("line_bot_health_check.sh")],
    cwd=None,
    env=_base_env(),
    timeout=60,
    description="Daily 15:00 bot health check",
)

JOB_REGISTRY["monthly-cold-backup-reminder"] = JobSpec(
    command=[_user_script("monthly_cold_backup_reminder.sh")],
    cwd=None,
    env=_base_env(),
    timeout=180,
    description="Day 1 of month 09:00 cold backup reminder",
)

JOB_REGISTRY["process-pending-media"] = JobSpec(
    command=[_LB_PYTHON, str(_HERE / "jobs" / "process_pending_media.py")],
    cwd=str(_HERE),
    env=_base_env(),
    timeout=600,
    description="Legacy pending reply drain; no-op while pending reply is disabled",
)


JOB_REGISTRY["daily-pending-audit"] = JobSpec(
    command=[_LB_PYTHON, str(_HERE / "jobs" / "daily_pending_audit.py")],
    cwd=str(_HERE),
    env=_base_env(),
    timeout=60,
    description="Legacy pending reply audit; no Discord while pending reply is disabled",
)


JOB_REGISTRY["daily-line-bot-review"] = JobSpec(
    command=[_LB_PYTHON, str(_HERE / "jobs" / "daily_line_bot_review.py")],
    cwd=str(_HERE),
    env=_base_env(),
    timeout=420,
    description="Daily LINE bot lifecycle review and feature suggestion → Discord",
)


JOB_REGISTRY["weekly-hook-block-report"] = JobSpec(
    command=[_LB_PYTHON, _user_script("weekly_hook_block_report.py")],
    cwd=None,
    env=_base_env(),
    timeout=60,
    description="Sunday 09:00 weekly ironrule hook block report → Discord",
)
