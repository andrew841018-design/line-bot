"""Daily 21:00 audit: scan pending_explicit_reply.json for unanswered 圖文
(image/video/file) and push a Discord DM with daily status.

Pure `build_report(data) -> AuditReport` + thin `main()` I/O driver. Audio
intentionally excluded per user spec but surfaced as a separate line for
transparency. Distinguishes corrupt JSON from empty pending so a silently
broken file does not get reported as "全部已回應".
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("daily_pending_audit")

# Per user spec: 圖文 = image/video/file. Audio counted separately for
# transparency. Adding a new audited type is a one-liner.
AUDITED_TYPES: frozenset[str] = frozenset({"image", "video", "file"})

DISCORD_MSG_MAX = 1800  # Discord hard limit is 2000; leave headroom

STATE_DIR = BASE / "state"
STATE_PATH = STATE_DIR / "last_run_daily-pending-audit.json"

# SUPPRESS_OK_DM=1 silences daily-OK pings. Default OFF: user asked
# "有的話，也推到 discord 讓我知道每日狀況".
SUPPRESS_OK_DM = os.environ.get("SUPPRESS_OK_DM", "").lower() in {"1", "true", "yes"}


@dataclass
class AuditRow:
    group_id: str
    message_id: str
    user_id: str | None
    type: str
    timestamp: float | int | str | None
    file_name: str | None = None
    download_failed: bool = False
    manual_recovery: bool = False


@dataclass
class AuditReport:
    rows: list[AuditRow] = field(default_factory=list)
    type_counts: dict[str, int] = field(
        default_factory=lambda: {"image": 0, "video": 0, "file": 0}
    )
    manual_recovery_count: int = 0
    download_failed_count: int = 0
    audio_excluded_count: int = 0
    text_excluded_count: int = 0
    other_excluded_count: int = 0
    load_status: str = "ok"  # ok | empty | corrupt | missing
    pending_file_size: int = 0
    pending_file_present: bool = False

    @property
    def total_unanswered_media(self) -> int:
        return len(self.rows)


def _safe_load_pending() -> tuple[dict, str, int, bool]:
    """Return (data, status, file_size, present).

    pending_store.load() swallows JSONDecodeError and returns {}. We detect
    corruption by re-parsing when load() returns empty for a non-trivial file.
    """
    import pending_store

    pending_path = pending_store.PENDING_PATH
    present = pending_path.exists()
    file_size = pending_path.stat().st_size if present else 0

    if not present:
        return {}, "missing", 0, False

    if file_size == 0:
        return {}, "empty", 0, True

    try:
        data = pending_store.load()
    except Exception as e:
        log.warning("pending_store.load raised: %s", str(e)[:200])
        data = {}

    if not data and file_size > 5:
        try:
            with open(pending_path) as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                return raw, "ok", file_size, True
            return {}, "corrupt", file_size, True
        except (json.JSONDecodeError, OSError) as e:
            log.warning("direct parse failed: %s", str(e)[:200])
            return {}, "corrupt", file_size, True

    return data, "ok", file_size, True


def _hash_user_short(user_id: str | None) -> str:
    if not user_id:
        return "?"
    if user_id.startswith("U_") and user_id.endswith("_manual"):
        return user_id
    if len(user_id) > 7:
        return user_id[:7] + "…"
    return user_id


def _short_message_id(message_id: str | None) -> str:
    if not message_id:
        return "?"
    if message_id.startswith("manual_recovery_"):
        return message_id[:24]
    if len(message_id) > 10:
        return message_id[:10] + "…"
    return message_id


def _short_file_name(file_name: str | None) -> str:
    """PII-conscious: hash user-supplied file name, keep extension.

    Truncated prefix leaks Chinese context ("114年度-曾美…"); full hash + ext
    keeps file identity (audit row uniqueness) without exposing content.
    """
    if not file_name:
        return ""
    dot = file_name.rfind(".")
    ext = file_name[dot:] if 0 < dot and len(file_name) - dot <= 6 else ""
    h = hashlib.sha256(file_name.encode("utf-8", "replace")).hexdigest()[:6]
    return f"{h}{ext}" if ext else h


def _format_timestamp(ts) -> str:
    try:
        ts_float = float(ts)
        if ts_float <= 0:
            return "未知時間"
        return datetime.fromtimestamp(ts_float).strftime("%m/%d %H:%M")
    except (TypeError, ValueError):
        return "未知時間"


def build_report(
    data: dict,
    *,
    load_status: str = "ok",
    file_size: int = 0,
    file_present: bool = True,
) -> AuditReport:
    report = AuditReport(
        load_status=load_status,
        pending_file_size=file_size,
        pending_file_present=file_present,
    )

    if not isinstance(data, dict):
        return report

    for group_id, items in data.items():
        if not isinstance(items, list):
            continue
        for entry in items:
            if not isinstance(entry, dict):
                continue
            t = entry.get("type")
            if t == "audio":
                report.audio_excluded_count += 1
                continue
            if t == "text":
                report.text_excluded_count += 1
                continue
            if t not in AUDITED_TYPES:
                report.other_excluded_count += 1
                continue

            row = AuditRow(
                group_id=group_id,
                message_id=entry.get("message_id") or "?",
                user_id=entry.get("user_id"),
                type=t,
                timestamp=entry.get("timestamp"),
                file_name=entry.get("file_name"),
                download_failed=bool(entry.get("download_failed")),
                manual_recovery="manual_recovery_reason" in entry,
            )
            report.rows.append(row)
            report.type_counts[t] = report.type_counts.get(t, 0) + 1
            if row.manual_recovery:
                report.manual_recovery_count += 1
            if row.download_failed:
                report.download_failed_count += 1

    return report


def format_message(report: AuditReport) -> str:
    today = datetime.now().strftime("%Y-%m-%d")

    if report.load_status == "corrupt":
        return (
            f"🚨 [{today}] daily-pending-audit FAILED: pending JSON 可能毀損 "
            f"(size={report.pending_file_size}B, 解析失敗或回傳空 dict)。"
            f"請檢查 line_bot/pending_explicit_reply.json"
        )

    if report.load_status == "missing":
        return f"🚨 [{today}] daily-pending-audit: pending_explicit_reply.json 不存在"

    n = report.total_unanswered_media

    if n == 0:
        msg = f"✅ [{today}] pending 圖文 0 則，全部已回應"
        extras = []
        if report.audio_excluded_count > 0:
            extras.append(f"audio={report.audio_excluded_count}")
        if report.text_excluded_count > 0:
            extras.append(f"text={report.text_excluded_count}")
        if extras:
            msg += "\n  其他類型 pending（已排除）: " + ", ".join(extras)
        return msg

    lines = [f"⚠️ [{today}] pending 還有 {n} 則圖文未回"]
    lines.append(
        f"  image={report.type_counts.get('image', 0)} "
        f"video={report.type_counts.get('video', 0)} "
        f"file={report.type_counts.get('file', 0)}"
    )
    if report.manual_recovery_count > 0:
        lines.append(f"  其中 {report.manual_recovery_count} 則為手動補回")
    if report.download_failed_count > 0:
        lines.append(
            f"  ⚠️ {report.download_failed_count} 則 download_failed "
            f"(無法自動補回，建議手動處理)"
        )
    if report.audio_excluded_count > 0:
        lines.append(f"  audio leftover {report.audio_excluded_count} 則（已排除，非圖文）")

    lines.append("")
    lines.append("Top 10（最舊優先）:")

    def _sort_key(r: AuditRow) -> float:
        try:
            v = float(r.timestamp) if r.timestamp is not None else float("inf")
            return v if v > 0 else float("inf")
        except (TypeError, ValueError):
            return float("inf")

    sorted_rows = sorted(report.rows, key=_sort_key)

    for i, row in enumerate(sorted_rows[:10], 1):
        ts = _format_timestamp(row.timestamp)
        user = _hash_user_short(row.user_id)
        msg_id = _short_message_id(row.message_id)
        flags = []
        if row.manual_recovery:
            flags.append("補")
        if row.download_failed:
            flags.append("DL✗")
        flag_str = f" [{','.join(flags)}]" if flags else ""
        fname = (
            f" name={_short_file_name(row.file_name)}"
            if row.file_name
            else ""
        )
        lines.append(
            f"  {i}. {row.type} {ts} u={user} m={msg_id}{fname}{flag_str}"
        )

    msg = "\n".join(lines)
    if len(msg) > DISCORD_MSG_MAX:
        suffix = "\n…(truncated)"
        msg = msg[: DISCORD_MSG_MAX - len(suffix)] + suffix
    return msg


def _write_state(*, ok: bool, status: str, summary: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now().isoformat(timespec="seconds")
    state = {
        "ok": ok,
        "status": status,
        "started_at": started,
        "started_at_epoch": time.time(),
        "finished_at": started,
        "summary": summary,
    }
    tmp = STATE_PATH.with_name(f".{STATE_PATH.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log.warning("write state failed: %s", e)
        try:
            tmp.unlink()
        except OSError:
            pass


def _send_discord(msg: str) -> bool:
    # TODO(notify_discord): suppress r.text print on error to reduce token-echo
    # risk in launchd logs. Out of scope for this change.
    try:
        from notify_discord import send_dm

        return bool(send_dm(msg))
    except Exception as e:
        log.warning("discord send raised: %s", str(e)[:200])
        return False


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    dry_run = "--dry-run" in argv

    data, status, file_size, present = _safe_load_pending()
    report = build_report(
        data,
        load_status=status,
        file_size=file_size,
        file_present=present,
    )
    msg = format_message(report)
    print(msg)

    summary = {
        "load_status": status,
        "total_unanswered_media": report.total_unanswered_media,
        "type_counts": dict(report.type_counts),
        "manual_recovery_count": report.manual_recovery_count,
        "download_failed_count": report.download_failed_count,
        "audio_excluded_count": report.audio_excluded_count,
        "text_excluded_count": report.text_excluded_count,
        "msg_len": len(msg),
        "suppress_ok_dm": SUPPRESS_OK_DM,
    }

    if dry_run:
        summary["dry_run"] = True
        _write_state(ok=True, status="dry_run", summary=summary)
        return 0

    should_push = True
    if SUPPRESS_OK_DM and status == "ok" and report.total_unanswered_media == 0:
        should_push = False
        log.info("SUPPRESS_OK_DM on + 0 leftover → skip Discord")

    sent = False
    if should_push:
        sent = _send_discord(msg)
    summary["discord_sent"] = sent
    summary["discord_skipped"] = not should_push

    overall_ok = (status in {"ok", "empty"}) and (sent or not should_push)
    _write_state(
        ok=overall_ok,
        status="completed" if overall_ok else (
            "corrupt_pending" if status == "corrupt"
            else "discord_send_failed"
        ),
        summary=summary,
    )

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
