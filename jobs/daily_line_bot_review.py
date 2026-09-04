"""Daily LINE bot lifecycle review and feature suggestion -> Discord.

This job intentionally sends only sanitized summaries to Discord. It must not
push to LINE, and it must not expose raw chat text, agent stdout/stderr, or
full LINE identifiers in job state or stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence


BASE = Path(__file__).resolve().parent.parent
ROOT = BASE.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE / ".env")


STATE_DIR = BASE / "state"
STATE_PATH = STATE_DIR / "last_run_daily-line-bot-review.json"
DISCORD_MSG_MAX = 1800
LIFECYCLE_TASK_FILE = ROOT / "ops" / "state" / "daily_line_bot_review_task.md"

LINE_ID_RE = re.compile(r"\b([UGR])[0-9A-Fa-f]{24,}\b")
SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|authorization)(\s*[:=]\s*)([^\s'\"`]+)"
)
SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(key=)[^&\s]+", re.IGNORECASE), r"\1REDACTED"),
    (re.compile(r"([?&]key=)[^&\s]+"), r"\1REDACTED"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]+"), r"\1REDACTED"),
    (re.compile(r"(X-Goog-Api-Key:\s*)[^\s]+"), r"\1REDACTED"),
    (re.compile(r"(Authorization:\s*Bot\s+)[^\s]+"), r"\1REDACTED"),
    (re.compile(r"(https?://discord(?:app)?\.com/api/webhooks/\d+/)[A-Za-z0-9_\-]+"), r"\1REDACTED"),
    (re.compile(r"(postgres(?:ql)?://[^:]+:)[^@\s]+"), r"\1REDACTED"),
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
    (re.compile(r"AIza[0-9A-Za-z_-]{20,}"), "[REDACTED]"),
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str = ""
    returncode: int | None = None


@dataclass(frozen=True)
class LifecycleResult:
    enabled: bool
    exit_code: int
    agents: list[dict]
    error: str = ""

    @property
    def ok(self) -> bool:
        return (not self.enabled) or self.exit_code == 0


def _sanitize(text: object, *, limit: int | None = None) -> str:
    clean = str(text or "")
    clean = LINE_ID_RE.sub(lambda m: f"{m.group(1)}***", clean)
    for pattern, replacement in SECRET_PATTERNS:
        clean = pattern.sub(replacement, clean)
    clean = SECRET_RE.sub(r"\1\2[REDACTED]", clean)
    clean = re.sub(r"[\r\t]+", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    clean = _compact_repeated_prefix(clean)
    if limit is not None and len(clean) > limit:
        clean = clean[: max(0, limit - 1)].rstrip() + "…"
    return clean


def _compact_repeated_prefix(text: str) -> str:
    """Collapse accidental repeated private-message bodies before truncation."""
    if len(text) < 48:
        return text
    max_unit = min(120, len(text) // 2)
    for size in range(12, max_unit + 1):
        unit = text[:size]
        if unit and text.startswith(unit * 2):
            return unit.rstrip() + "…"
    return text


def _run_command(name: str, command: list[str], *, cwd: Path, timeout_s: int) -> CheckResult:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            check=False,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(name, "timeout", f"timed out after {timeout_s}s")
    except OSError as exc:
        return CheckResult(name, "error", _sanitize(exc, limit=160))

    detail = (completed.stdout or completed.stderr or "").strip()
    status = "passed" if completed.returncode == 0 else "failed"
    return CheckResult(name, status, _sanitize(detail, limit=180), completed.returncode)


def run_local_checks() -> list[CheckResult]:
    py_compile_targets = [
        "main.py",
        "gemini_client.py",
        "daily_briefing_discord.py",
        "memory.py",
        "media_pipeline.py",
        "vision_common.py",
        "output_validator.py",
        "jobs/daily_line_bot_review.py",
    ]
    existing_targets = [target for target in py_compile_targets if (BASE / target).exists()]
    return [
        _run_command(
            "GitHub privacy audit",
            [
                sys.executable,
                "jobs/git_privacy_audit.py",
                "--repo",
                ".",
                "--scope",
                "index",
                "--scope",
                "worktree",
                "--scope",
                "remote",
            ],
            cwd=BASE,
            timeout_s=120,
        ),
        _run_command(
            "git diff --check",
            ["git", "diff", "--check"],
            cwd=BASE,
            timeout_s=30,
        ),
        _run_command(
            "py_compile core",
            [sys.executable, "-m", "py_compile", *existing_targets],
            cwd=BASE,
            timeout_s=60,
        ),
    ]


def _ensure_lifecycle_task_file() -> None:
    if LIFECYCLE_TASK_FILE.exists():
        return
    LIFECYCLE_TASK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LIFECYCLE_TASK_FILE.write_text(
        "\n".join(
            [
                "Daily task: review the local line_bot project.",
                "",
                "Focus:",
                "- correctness regressions in current dirty worktree",
                "- privacy and outbound messaging safety",
                "- scheduled job reliability",
                "- tests that should be added or run",
                "",
                "Do not ask to push LINE messages. Return findings first.",
            ]
        ),
        encoding="utf-8",
    )


def run_lifecycle_sidecar(timeout_s: int = 180) -> LifecycleResult:
    _ensure_lifecycle_task_file()
    command = [
        sys.executable,
        str(ROOT / "ops" / "lifecycle_runner.py"),
        "run",
        "--stage",
        "review",
        "--task-file",
        str(LIFECYCLE_TASK_FILE.relative_to(ROOT)),
        "--send-external",
        "--timeout-s",
        str(timeout_s),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            check=False,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_s + 30,
        )
    except subprocess.TimeoutExpired:
        return LifecycleResult(True, 1, [], f"lifecycle runner timed out after {timeout_s + 30}s")
    except OSError as exc:
        return LifecycleResult(True, 1, [], _sanitize(exc, limit=180))

    try:
        parsed = json.loads(completed.stdout or "[]")
        agents = parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        agents = []
    sanitized_agents = [
        {
            "name": _sanitize(row.get("name", "?"), limit=24),
            "status": _sanitize(row.get("status", "unknown"), limit=48),
            "returncode": row.get("returncode"),
            "duration_s": row.get("duration_s"),
            "stdout_len": row.get("stdout_len", 0),
            "stderr_len": row.get("stderr_len", 0),
            "error": _sanitize(row.get("error", ""), limit=120),
        }
        for row in agents
        if isinstance(row, dict)
    ]
    error = ""
    if completed.returncode != 0 and not sanitized_agents:
        error = _sanitize(completed.stderr or completed.stdout, limit=180)
    return LifecycleResult(True, completed.returncode, sanitized_agents, error)


def _extract_suggestion(rendered: str) -> dict[str, str]:
    text = _sanitize(rendered, limit=600)
    marker = "LINE bot 每日推薦"
    if marker not in text:
        return {"title": "LINE bot 每日推薦", "reason": text[:160]}
    _, tail = text.split(marker, 1)
    tail = tail.lstrip("*：: -")
    if "—" in tail:
        title, reason = tail.split("—", 1)
    elif "-" in tail:
        title, reason = tail.split("-", 1)
    else:
        title, reason = tail, ""
    return {
        "title": _sanitize(title.strip(" ：:*"), limit=48),
        "reason": _sanitize(reason.strip(), limit=160),
    }


def build_feature_suggestion(*, record_history: bool = True) -> dict[str, str]:
    import daily_briefing_discord as dbd

    if record_history:
        rendered = dbd.line_bot_suggestions(use_ai=False)
    else:
        with tempfile.TemporaryDirectory(prefix="daily_line_bot_review_") as tmp:
            rendered = dbd.line_bot_suggestions(
                history_path=Path(tmp) / "suggestion_history.json",
                use_ai=False,
            )
    suggestion = _extract_suggestion(rendered)
    if not suggestion["title"]:
        suggestion["title"] = "群聊需求雷達"
    if not suggestion["reason"]:
        suggestion["reason"] = "根據近期群聊訊號，每天抽一個最值得做的 LINE bot 功能缺口。"
    return suggestion


def _agent_status_line(result: LifecycleResult) -> str:
    if not result.enabled:
        return "skipped"
    if not result.agents:
        return f"runner_failed rc={result.exit_code}"
    parts = [
        f"{_sanitize(row.get('name', '?'), limit=24)}="
        f"{_sanitize(row.get('status', 'unknown'), limit=48)}"
        for row in result.agents
    ]
    return ", ".join(parts)


def _checks_line(checks: Sequence[CheckResult]) -> str:
    if not checks:
        return "none"
    return ", ".join(f"{check.name}={check.status}" for check in checks)


def format_discord_message(
    *,
    now: datetime,
    local_checks: Sequence[CheckResult],
    lifecycle: LifecycleResult,
    suggestion: dict[str, str],
) -> str:
    local_ok = all(check.status == "passed" for check in local_checks)
    review_status = "PASS" if local_ok and lifecycle.ok else "ATTENTION"
    lines = [
        f"🧪 LINE Bot Daily Review {now.strftime('%Y-%m-%d %H:%M')}",
        f"狀態：{review_status}",
        f"本地檢查：{_checks_line(local_checks)}",
        f"Agents：{_agent_status_line(lifecycle)}",
    ]
    failed_checks = [check for check in local_checks if check.status != "passed"]
    if failed_checks:
        detail = "; ".join(
            f"{check.name}: {check.status} {check.detail}".strip()
            for check in failed_checks[:3]
        )
        lines.append(f"需注意：{_sanitize(detail, limit=240)}")
    if lifecycle.error:
        lines.append(f"Agent 錯誤：{_sanitize(lifecycle.error, limit=160)}")

    title = _sanitize(suggestion.get("title", ""), limit=48)
    reason = _sanitize(suggestion.get("reason", ""), limit=180)
    lines.extend(
        [
            "",
            f"功能建議：{title}",
            f"理由：{reason}",
            "你覺得值得做再叫我實作；此 job 不會自動改程式或推 LINE。",
        ]
    )
    msg = "\n".join(lines)
    if len(msg) > DISCORD_MSG_MAX:
        msg = msg[: DISCORD_MSG_MAX - 1].rstrip() + "…"
    return _sanitize(msg)


def _state_summary(
    *,
    local_checks: Sequence[CheckResult],
    lifecycle: LifecycleResult,
    suggestion: dict[str, str],
    discord_sent: bool,
    discord_skipped: bool,
) -> dict:
    return {
        "local_checks": [
            {
                "name": _sanitize(check.name, limit=80),
                "status": check.status,
                "returncode": check.returncode,
                "detail": _sanitize(check.detail, limit=120),
            }
            for check in local_checks
        ],
        "lifecycle": {
            "enabled": lifecycle.enabled,
            "exit_code": lifecycle.exit_code,
            "agents": [
                {
                    "name": _sanitize(row.get("name", "?"), limit=24),
                    "status": _sanitize(row.get("status", "unknown"), limit=48),
                    "returncode": row.get("returncode"),
                    "duration_s": row.get("duration_s"),
                    "stdout_len": row.get("stdout_len", 0),
                    "stderr_len": row.get("stderr_len", 0),
                    "error": _sanitize(row.get("error", ""), limit=120),
                }
                for row in lifecycle.agents
                if isinstance(row, dict)
            ],
            "error": _sanitize(lifecycle.error, limit=120),
        },
        "suggestion": {
            "title": _sanitize(suggestion.get("title", ""), limit=48),
            "reason": _sanitize(suggestion.get("reason", ""), limit=160),
        },
        "discord_sent": discord_sent,
        "discord_skipped": discord_skipped,
    }


def _write_state(record: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(f"{STATE_PATH.name}.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _send_discord(message: str) -> bool:
    from notify_discord import send_dm

    return bool(send_dm(message))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-external", action="store_true")
    parser.add_argument("--external-timeout-s", type=int, default=180)
    args = parser.parse_args(argv)

    started = time.time()
    now = datetime.now()
    local_checks = run_local_checks()
    lifecycle = (
        LifecycleResult(False, 0, [])
        if args.skip_external or args.dry_run
        else run_lifecycle_sidecar(timeout_s=args.external_timeout_s)
    )
    suggestion = build_feature_suggestion(record_history=not args.dry_run)
    message = format_discord_message(
        now=now,
        local_checks=local_checks,
        lifecycle=lifecycle,
        suggestion=suggestion,
    )

    discord_sent = False
    status = "dry_run" if args.dry_run else "completed"
    ok = all(check.status == "passed" for check in local_checks) and lifecycle.ok
    if args.dry_run:
        print(message)
    else:
        discord_sent = _send_discord(message)
        if not discord_sent:
            status = "discord_send_failed"
            ok = False
        elif not ok:
            status = "review_attention"

    finished = time.time()
    _write_state(
        {
            "ok": bool(ok),
            "status": status,
            "started_at": started,
            "finished_at": finished,
            "duration_s": round(finished - started, 3),
            "summary": _state_summary(
                local_checks=local_checks,
                lifecycle=lifecycle,
                suggestion=suggestion,
                discord_sent=discord_sent,
                discord_skipped=args.dry_run,
            ),
        }
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
