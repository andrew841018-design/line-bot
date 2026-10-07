"""Daily LINE bot lifecycle review -> Discord.

This job intentionally sends only sanitized summaries to Discord. It must not
push to LINE, and it must not expose raw chat text, agent stdout/stderr, or
full LINE identifiers in job state or stdout.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Sequence
from zoneinfo import ZoneInfo


BASE = Path(__file__).resolve().parent.parent
ROOT = BASE.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE / ".env")


STATE_DIR = BASE / "state"
OUTCOME_PATH = STATE_DIR / "daily_line_bot_review_outcome.json"
PROPOSAL_LEDGER_PATH = STATE_DIR / "daily_line_bot_proposal_delivery.json"
PROPOSAL_LOCK_PATH = STATE_DIR / "daily_line_bot_proposal_delivery.lock"
DISCORD_MSG_MAX = 1800
LIFECYCLE_TASK_FILE = ROOT / "ops" / "state" / "daily_line_bot_review_task.md"
TAIPEI = ZoneInfo("Asia/Taipei")
DELIVERY_SCHEMA = 1
PROPOSAL_PURPOSE = "line_bot_daily_development_proposal"

LINE_ID_RE = re.compile(r"\b([CUGR])[0-9A-Fa-f]{24,}\b")
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


@dataclass(frozen=True)
class DailyProposalDelivery:
    status: str

    @property
    def sent(self) -> bool:
        return self.status == "sent"

    @property
    def ok(self) -> bool:
        return self.status in {"sent", "already_sent"}


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


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _atomic_write_private_json(path: Path, record: dict) -> None:
    _private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Some filesystems do not support directory fsync. The file itself
            # was already fsynced before the atomic replace.
            pass
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _locked_file(path: Path):
    _private_dir(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)

    class _Lock:
        def __enter__(self):
            fcntl.flock(fd, fcntl.LOCK_EX)
            return self

        def __exit__(self, exc_type, exc, tb):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    return _Lock()


def _taipei_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(TAIPEI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TAIPEI)
    return current.astimezone(TAIPEI)


def _load_delivery_record(path: Path, *, purpose: str) -> dict | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("delivery ledger is not an object")
    required = {"schema", "purpose", "date", "status", "title_hash", "started_at"}
    if not required.issubset(data):
        raise ValueError("delivery ledger schema is incomplete")
    if type(data["schema"]) is not int or data["schema"] != DELIVERY_SCHEMA:
        raise ValueError("delivery ledger schema is invalid")
    string_fields = {"purpose", "date", "status", "title_hash", "started_at"}
    if not all(type(data[field]) is str for field in string_fields):
        raise ValueError("delivery ledger contains invalid field types")
    if data["purpose"] != purpose:
        raise ValueError("delivery ledger purpose/schema mismatch")
    if data["status"] not in {"pending", "sent", "definite_failed"}:
        raise ValueError("delivery ledger status is invalid")
    parsed_date = date.fromisoformat(data["date"])
    if parsed_date.isoformat() != data["date"]:
        raise ValueError("delivery ledger date is not canonical")
    if re.fullmatch(r"[0-9a-f]{64}", data["title_hash"]) is None:
        raise ValueError("delivery ledger title hash is invalid")
    for field in ("started_at", "finished_at"):
        if field not in data:
            if field == "finished_at" and data["status"] == "pending":
                continue
            raise ValueError(f"delivery ledger {field} is missing")
        if type(data[field]) is not str:
            raise ValueError(f"delivery ledger {field} has an invalid type")
        stamp = datetime.fromisoformat(data[field])
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError(f"delivery ledger {field} must be timezone-aware")
    return data


def _delivery_record(
    *, purpose: str, day: str, status: str, title: str, now: datetime
) -> dict:
    safe_title = _sanitize(title, limit=80)
    return {
        "schema": DELIVERY_SCHEMA,
        "purpose": purpose,
        "date": day,
        "status": status,
        "title_hash": hashlib.sha256(safe_title.encode("utf-8")).hexdigest(),
        "started_at": now.isoformat(),
    }


def send_daily_proposal_once(
    message: str,
    *,
    title: str,
    now: datetime | None = None,
    sender: Callable[[str], bool] | None = None,
) -> DailyProposalDelivery:
    """Send at most one development proposal per Taipei calendar day.

    A durable pending reservation is written before network I/O. An unknown
    outcome remains pending and therefore fails closed on every same-day retry.
    The ledger stores no message body, group text, identifiers, or credentials.
    """
    current = _taipei_now(now)
    day = current.date().isoformat()
    delivery_sender = sender or _send_discord_proposal

    with _locked_file(PROPOSAL_LOCK_PATH):
        try:
            previous = _load_delivery_record(
                PROPOSAL_LEDGER_PATH, purpose=PROPOSAL_PURPOSE
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return DailyProposalDelivery("ledger_corrupt")

        if previous and previous["date"] == day:
            if previous["status"] == "sent":
                return DailyProposalDelivery("already_sent")
            if previous["status"] == "pending":
                return DailyProposalDelivery("pending_unknown")
            if previous["status"] == "definite_failed":
                return DailyProposalDelivery("definite_failed")
        if previous and date.fromisoformat(previous["date"]) > current.date():
            return DailyProposalDelivery("ledger_corrupt")

        pending = _delivery_record(
            purpose=PROPOSAL_PURPOSE,
            day=day,
            status="pending",
            title=title,
            now=current,
        )
        try:
            _atomic_write_private_json(PROPOSAL_LEDGER_PATH, pending)
        except OSError:
            return DailyProposalDelivery("ledger_error")

        try:
            raw_result = delivery_sender(_sanitize(message, limit=DISCORD_MSG_MAX))
        except Exception:
            # A timeout/connection error can happen after Discord accepted the
            # message. Keep the pending reservation and do not risk a duplicate.
            return DailyProposalDelivery("pending_unknown")

        if isinstance(raw_result, bool):
            delivery_status = "sent" if raw_result else "definite_failed"
        else:
            delivery_status = str(getattr(raw_result, "status", raw_result))
        if delivery_status == "pending_unknown":
            return DailyProposalDelivery("pending_unknown")
        if delivery_status not in {"sent", "definite_failed"}:
            return DailyProposalDelivery("pending_unknown")
        delivered = delivery_status == "sent"

        if not delivered:
            failed = dict(pending)
            failed["status"] = "definite_failed"
            failed["finished_at"] = _taipei_now().isoformat()
            try:
                _atomic_write_private_json(PROPOSAL_LEDGER_PATH, failed)
            except OSError:
                return DailyProposalDelivery("ledger_error")
            return DailyProposalDelivery("definite_failed")

        sent = dict(pending)
        sent["status"] = "sent"
        sent["finished_at"] = _taipei_now().isoformat()
        try:
            _atomic_write_private_json(PROPOSAL_LEDGER_PATH, sent)
        except OSError:
            # The durable pending reservation is intentionally preserved when
            # finalization fails after a successful network response.
            return DailyProposalDelivery("pending_unknown")
        return DailyProposalDelivery("sent")


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

    msg = "\n".join(lines)
    if len(msg) > DISCORD_MSG_MAX:
        msg = msg[: DISCORD_MSG_MAX - 1].rstrip() + "…"
    return _sanitize(msg)


def _state_summary(
    *,
    local_checks: Sequence[CheckResult],
    lifecycle: LifecycleResult,
    discord_sent: bool,
    discord_skipped: bool,
    discord_delivery_status: str,
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
        "discord_sent": discord_sent,
        "discord_skipped": discord_skipped,
        "discord_delivery_status": discord_delivery_status,
    }


def _write_state(record: dict) -> None:
    _atomic_write_private_json(OUTCOME_PATH, record)


def _send_discord(message: str):
    from notify_discord import send_dm_result

    return send_dm_result(message)


def _send_discord_proposal(message: str):
    from notify_discord import send_dm_result

    return send_dm_result(message)


def _read_private_proposal(path: Path) -> dict:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        proposal_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(proposal_stat.st_mode)
            or proposal_stat.st_uid != os.getuid()
            or stat.S_IMODE(proposal_stat.st_mode) != 0o600
            or proposal_stat.st_size > 32 * 1024
        ):
            raise ValueError(
                "proposal file must be owned, regular, mode 0600, and at most 32 KiB"
            )
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = -1
            proposal = json.load(fh)
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(proposal, dict):
        raise ValueError("proposal payload must be an object")
    return proposal


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-external", action="store_true")
    parser.add_argument("--external-timeout-s", type=int, default=180)
    parser.add_argument(
        "--send-proposal-json",
        type=Path,
        help="Send one sanitized daily proposal from a local JSON file via the dedupe gate",
    )
    args = parser.parse_args(argv)

    if args.send_proposal_json is not None:
        if args.dry_run or args.skip_external:
            print(json.dumps({"status": "conflicting_proposal_mode"}))
            return 1
        try:
            proposal = _read_private_proposal(args.send_proposal_json)
            title = str(proposal["title"]).strip()
            message = str(proposal["message"]).strip()
            if not title or not message:
                raise ValueError("proposal title/message must be non-empty")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            print(json.dumps({"status": "invalid_proposal_file"}))
            return 1
        result = send_daily_proposal_once(message, title=title)
        print(json.dumps({"status": result.status}))
        return 0 if result.ok else 1

    started = time.time()
    now = datetime.now()
    local_checks = run_local_checks()
    lifecycle = (
        LifecycleResult(False, 0, [])
        if args.skip_external or args.dry_run
        else run_lifecycle_sidecar(timeout_s=args.external_timeout_s)
    )
    message = format_discord_message(
        now=now,
        local_checks=local_checks,
        lifecycle=lifecycle,
    )

    discord_sent = False
    discord_delivery_status = "skipped" if args.dry_run else "pending"
    status = "dry_run" if args.dry_run else "completed"
    review_ok = all(check.status == "passed" for check in local_checks) and lifecycle.ok
    if args.dry_run:
        print(message)
    else:
        pending_record = {
            "ok": False,
            "status": "discord_delivery_pending",
            "started_at": started,
            "finished_at": time.time(),
            "duration_s": round(time.time() - started, 3),
            "summary": _state_summary(
                local_checks=local_checks,
                lifecycle=lifecycle,
                discord_sent=False,
                discord_skipped=False,
                discord_delivery_status="pending",
            ),
        }
        try:
            _write_state(pending_record)
        except OSError:
            print("daily review pending outcome write failed", file=sys.stderr)
            return 1

        try:
            raw_delivery = _send_discord(message)
            if isinstance(raw_delivery, bool):
                discord_delivery_status = (
                    "sent" if raw_delivery else "definite_failed"
                )
            else:
                discord_delivery_status = str(
                    getattr(raw_delivery, "status", raw_delivery)
                )
        except Exception:
            discord_delivery_status = "pending_unknown"

        if discord_delivery_status == "sent":
            discord_sent = True
        elif discord_delivery_status == "pending_unknown":
            status = "discord_delivery_unknown"
        else:
            discord_delivery_status = "definite_failed"
            status = "discord_send_failed"
        if discord_sent and not review_ok:
            status = "review_attention"

    ok = bool(review_ok and (args.dry_run or discord_sent))

    finished = time.time()
    try:
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
                discord_sent=discord_sent,
                discord_skipped=args.dry_run,
                discord_delivery_status=discord_delivery_status,
            ),
        }
        )
    except OSError:
        print("daily review outcome write failed", file=sys.stderr)
        if not args.dry_run and discord_delivery_status in {"sent", "pending_unknown"}:
            return 0
        return 1
    if args.dry_run:
        return 0
    return 0 if discord_delivery_status in {"sent", "pending_unknown"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
