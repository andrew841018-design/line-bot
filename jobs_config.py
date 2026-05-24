from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class JobSpec:
    command: list[str]
    cwd: str | None
    env: dict[str, str]
    timeout: int
    description: str = ""

_BASE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
_PROJECT_VENV_PATH = "/Users/andrew/Desktop/andrew/Data_engineer/project/.venv/bin:" + _BASE_PATH
_NVM_PATH = "/Users/andrew/.nvm/versions/node/v18.20.8/bin:" + _BASE_PATH + ":/usr/sbin:/sbin"
_DATA_ENG = "/Users/andrew/Desktop/andrew/Data_engineer"
_LB_VENV = f"{_DATA_ENG}/line_bot/.venv/bin/python"
_PROJ_VENV = f"{_DATA_ENG}/project/.venv/bin/python"

JOB_REGISTRY: dict[str, JobSpec] = {}

JOB_REGISTRY["daily-briefing-discord"] = JobSpec(
    command=[_PROJ_VENV, f"{_DATA_ENG}/line_bot/daily_briefing_discord.py"],
    cwd=f"{_DATA_ENG}/project/dependent_code",
    env={"PATH": _PROJECT_VENV_PATH, "HOME": "/Users/andrew", "USER": "andrew", "LOGNAME": "andrew"},
    timeout=120,
    description="Daily 10:00 Discord briefing",
)

JOB_REGISTRY["line-bot-event-reminder"] = JobSpec(
    command=[_LB_VENV, f"{_DATA_ENG}/line_bot/event_reminder.py"],
    cwd=f"{_DATA_ENG}/line_bot",
    env={"PATH": _BASE_PATH, "HOME": "/Users/andrew", "USER": "andrew", "LOGNAME": "andrew"},
    timeout=30,
    description="Daily 07:00 event reminder",
)

JOB_REGISTRY["line-bot-update-push"] = JobSpec(
    command=[_LB_VENV, f"{_DATA_ENG}/line_bot/line_bot_update_push.py"],
    cwd=f"{_DATA_ENG}/line_bot",
    env={"PATH": _BASE_PATH, "HOME": "/Users/andrew", "USER": "andrew", "LOGNAME": "andrew"},
    timeout=30,
    description="Daily 09:00 update notify",
)

JOB_REGISTRY["line-bot-feedback-push"] = JobSpec(
    command=["/bin/bash", "/Users/andrew/scripts/run_feedback_push.sh"],
    cwd=None,
    env={"PATH": _BASE_PATH, "HOME": "/Users/andrew", "USER": "andrew", "LOGNAME": "andrew"},
    timeout=30,
    description="Sunday 20:00 feedback notify",
)

JOB_REGISTRY["line-bot-weekly-summary"] = JobSpec(
    command=[_LB_VENV, f"{_DATA_ENG}/line_bot/weekly_summary.py"],
    cwd=f"{_DATA_ENG}/line_bot",
    env={"PATH": _BASE_PATH, "HOME": "/Users/andrew", "USER": "andrew", "LOGNAME": "andrew"},
    timeout=60,
    description="Sunday 20:00 weekly summary",
)

JOB_REGISTRY["line-bot-feedback-process"] = JobSpec(
    command=["/bin/bash", "/Users/andrew/scripts/run_feedback_process.sh"],
    cwd=None,
    env={"PATH": _BASE_PATH, "HOME": "/Users/andrew", "USER": "andrew", "LOGNAME": "andrew"},
    timeout=60,
    description="Tuesday 02:00 feedback process",
)

JOB_REGISTRY["line-bot-health"] = JobSpec(
    command=["/bin/bash", "/Users/andrew/scripts/line_bot_health_check.sh"],
    cwd=None,
    env={"PATH": _BASE_PATH, "HOME": "/Users/andrew", "USER": "andrew", "LOGNAME": "andrew"},
    timeout=60,
    description="Daily 15:00 bot health check",
)

JOB_REGISTRY["monthly-cold-backup-reminder"] = JobSpec(
    command=["/Users/andrew/scripts/monthly_cold_backup_reminder.sh"],
    cwd=None,
    env={"PATH": _BASE_PATH, "HOME": "/Users/andrew", "USER": "andrew", "LOGNAME": "andrew"},
    timeout=30,
    description="Day 1 of month 09:00 cold backup reminder",
)

JOB_REGISTRY["soxl-monitor-code-review"] = JobSpec(
    command=["/bin/bash", "/Users/andrew/scripts/soxl_monitor_code_review.sh"],
    cwd=None,
    env={"PATH": _BASE_PATH, "HOME": "/Users/andrew", "USER": "andrew", "LOGNAME": "andrew"},
    timeout=600,
    description="Daily 09:00 soxl code review",
)

JOB_REGISTRY["soxl-monitor-health-check"] = JobSpec(
    command=[_LB_VENV, f"{_DATA_ENG}/soxx_tracker/scripts/soxl_monitor_health_check.py"],
    cwd=f"{_DATA_ENG}/soxx_tracker",
    env={"PATH": _BASE_PATH, "HOME": "/Users/andrew", "USER": "andrew", "LOGNAME": "andrew"},
    timeout=60,
    description="Daily 20:00 soxl health check",
)


JOB_REGISTRY["process-pending-media"] = JobSpec(
    command=[_LB_VENV, f"{_DATA_ENG}/line_bot/jobs/process_pending_media.py"],
    cwd=f"{_DATA_ENG}/line_bot",
    env={"PATH": _BASE_PATH, "HOME": "/Users/andrew", "USER": "andrew", "LOGNAME": "andrew"},
    timeout=600,
    description="6-hourly retry: drain pending (text/image/video/audio/file) via local LLM",
)


JOB_REGISTRY["daily-pending-audit"] = JobSpec(
    command=[_LB_VENV, f"{_DATA_ENG}/line_bot/jobs/daily_pending_audit.py"],
    cwd=f"{_DATA_ENG}/line_bot",
    env={"PATH": _BASE_PATH, "HOME": "/Users/andrew", "USER": "andrew", "LOGNAME": "andrew"},
    timeout=60,
    description="Daily 21:00 audit: scan pending media leftovers, push Discord DM",
)


JOB_REGISTRY["weekly-hook-block-report"] = JobSpec(
    command=[_LB_VENV, "/Users/andrew/scripts/weekly_hook_block_report.py"],
    cwd=None,
    env={"PATH": _BASE_PATH, "HOME": "/Users/andrew", "USER": "andrew", "LOGNAME": "andrew"},
    timeout=60,
    description="Sunday 09:00 weekly ironrule hook block report → Discord",
)
