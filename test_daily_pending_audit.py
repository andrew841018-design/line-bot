"""Unit tests for jobs/daily_pending_audit.py.

Pure logic only — patches pending_store.PENDING_PATH to tmp_path and stubs
notify_discord.send_dm; never touches production pending JSON or Discord.
"""
from __future__ import annotations

import json
import pytest
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE / "jobs"))

import daily_pending_audit as dpa  # noqa: E402


@pytest.fixture(autouse=True)
def _legacy_pending_audit_enabled(monkeypatch):
    monkeypatch.setattr(dpa, "pending_reply_enabled", lambda: True)


# ---------- build_report ----------

def test_build_report_empty():
    r = dpa.build_report({}, load_status="empty", file_size=0, file_present=True)
    assert r.total_unanswered_media == 0
    assert r.load_status == "empty"
    assert r.audio_excluded_count == 0


def test_build_report_filters_audio_text_other():
    data = {
        "G1": [
            {"type": "image", "message_id": "m1", "user_id": "u1", "timestamp": 100.0},
            {"type": "audio", "message_id": "m2", "user_id": "u2", "timestamp": 200.0},
            {"type": "text", "message_id": "m3", "user_id": "u3", "timestamp": 300.0, "text": "hi"},
            {"type": "sticker", "message_id": "m4", "user_id": "u4", "timestamp": 400.0},
            {"type": "video", "message_id": "m5", "user_id": "u5", "timestamp": 500.0},
            {"type": "file", "message_id": "m6", "user_id": "u6", "timestamp": 600.0, "file_name": "a.pdf"},
        ]
    }
    r = dpa.build_report(data)
    assert r.total_unanswered_media == 3
    assert r.type_counts == {"image": 1, "video": 1, "file": 1}
    assert r.audio_excluded_count == 1
    assert r.text_excluded_count == 1
    assert r.other_excluded_count == 1


def test_build_report_manual_recovery_and_download_failed():
    data = {
        "G1": [
            {
                "type": "image",
                "message_id": "manual_recovery_abc",
                "user_id": "U_papa_manual",
                "timestamp": 100.0,
                "manual_recovery_reason": "webhook fail",
            },
            {
                "type": "video",
                "message_id": "m2",
                "user_id": "u2",
                "timestamp": 200.0,
                "download_failed": True,
            },
        ]
    }
    r = dpa.build_report(data)
    assert r.manual_recovery_count == 1
    assert r.download_failed_count == 1
    assert r.rows[0].manual_recovery is True
    assert r.rows[1].download_failed is True


def test_build_report_robust_to_bad_shapes():
    data = {
        "G1": "not-a-list",
        "G2": [None, "x", {"no_type": True}, {"type": "image", "message_id": "m1"}],
    }
    r = dpa.build_report(data)
    assert r.total_unanswered_media == 1
    assert r.other_excluded_count == 1


def test_build_report_corrupt_status_preserved():
    r = dpa.build_report({}, load_status="corrupt", file_size=123, file_present=True)
    assert r.load_status == "corrupt"
    assert r.pending_file_size == 123


def test_build_report_keeps_pending_reminder_rows():
    rows = [
        dpa.PendingReminderRow(
            pending_id=4,
            group_id="G1",
            message_id="m1",
            created_at=1000,
            retries=2,
            text="明天晚上7:15去教會4樓參加嗎哪小組查經",
        )
    ]
    r = dpa.build_report({}, pending_reminders=rows)
    assert r.total_pending_reminders == 1
    assert r.pending_reminders[0].pending_id == 4


# ---------- format_message ----------

def test_format_message_corrupt_shows_alarm():
    r = dpa.build_report({}, load_status="corrupt", file_size=999, file_present=True)
    msg = dpa.format_message(r)
    assert "🚨" in msg
    assert "毀損" in msg or "corrupt" in msg.lower()


def test_format_message_missing_shows_alarm():
    r = dpa.build_report({}, load_status="missing", file_size=0, file_present=False)
    msg = dpa.format_message(r)
    assert "🚨" in msg
    assert "不存在" in msg


def test_format_message_zero_leftover():
    r = dpa.build_report({}, load_status="empty")
    msg = dpa.format_message(r)
    assert "✅" in msg
    assert "0 則" in msg


def test_format_message_zero_with_excluded_extras():
    data = {"G1": [{"type": "audio", "message_id": "m"}, {"type": "text", "message_id": "m2"}]}
    r = dpa.build_report(data)
    msg = dpa.format_message(r)
    assert "✅" in msg
    assert "audio=1" in msg
    assert "text=1" in msg


def test_format_message_zero_media_warns_pending_reminders():
    r = dpa.build_report(
        {},
        pending_reminders=[
            dpa.PendingReminderRow(
                pending_id=4,
                group_id="G1",
                message_id="616958066751701691",
                created_at=1700000000,
                retries=0,
                text="以下是正確的：6月6日星期六下午三點要出發到台大醫院新醫院1樓做MRI胸椎檢查",
            )
        ],
    )
    msg = dpa.format_message(r)
    assert "⚠️" in msg
    assert "reminder extract pending 1 筆" in msg
    assert "pid=4" in msg
    assert "MRI" in msg


def test_format_message_with_leftover_lists_top10_and_flags():
    items = []
    for i in range(15):
        items.append(
            {
                "type": "image",
                "message_id": f"m{i:02d}",
                "user_id": f"U{i:040d}",
                "timestamp": 1000.0 + i,
            }
        )
    items[0]["manual_recovery_reason"] = "x"
    items[1]["download_failed"] = True
    items[2]["file_name"] = "report_2026_q1_long.pdf"
    items[2]["type"] = "file"
    data = {"G1": items}
    r = dpa.build_report(data)
    msg = dpa.format_message(r)
    assert "⚠️" in msg
    assert "15 則" in msg
    assert "Top 10" in msg
    assert msg.count("\n  ") >= 10
    assert "[補]" in msg
    assert "DL✗" in msg
    assert "name=" in msg


def test_format_message_truncated_strictly_under_limit(monkeypatch):
    """PII-redacted rows are short, so truncation rarely fires in practice.
    Force a tiny limit to verify the truncation math: must NEVER exceed limit.
    Regression guard for: msg[:LIMIT] + "\\n…(truncated)" → 1815 > 1800 bug.
    """
    monkeypatch.setattr(dpa, "DISCORD_MSG_MAX", 200)
    items = [
        {"type": "image", "message_id": f"m{i}", "user_id": f"U{i}", "timestamp": 1000.0 + i}
        for i in range(20)
    ]
    r = dpa.build_report({"G1": items})
    msg = dpa.format_message(r)
    assert len(msg) <= 200, f"msg len {len(msg)} > 200 — truncation overflow"
    assert msg.endswith("(truncated)")


# ---------- helpers ----------

def test_hash_user_short_preserves_manual_label():
    assert dpa._hash_user_short("U_papa_manual") == "U_papa_manual"
    assert dpa._hash_user_short("U9fde03d0fe1e0669eccc8b9b4ecc28a6").startswith("U9fde03")
    assert dpa._hash_user_short(None) == "?"


def test_short_file_name_hashes_and_keeps_extension():
    assert dpa._short_file_name(None) == ""
    short_pdf = dpa._short_file_name("a.pdf")
    assert short_pdf.endswith(".pdf") and len(short_pdf) == len("xxxxxx.pdf")
    long = dpa._short_file_name("114年度-曾美惠-非常長的-報表.pdf")
    assert long.endswith(".pdf")
    # Hash must NOT leak any Chinese / context from the original name
    assert "曾" not in long
    assert "年度" not in long
    assert "報表" not in long
    # Same input → same hash (deterministic, useful for dedup)
    assert dpa._short_file_name("a.pdf") == dpa._short_file_name("a.pdf")
    # No extension → just hash, no trailing dot
    no_ext = dpa._short_file_name("noext_filename")
    assert "." not in no_ext
    assert len(no_ext) == 6


def test_format_timestamp_defensive():
    assert dpa._format_timestamp(None) == "未知時間"
    assert dpa._format_timestamp("not-a-number") == "未知時間"
    assert dpa._format_timestamp(-1) == "未知時間"
    assert dpa._format_timestamp(0) == "未知時間"
    assert "/" in dpa._format_timestamp(1700000000.5)
    assert "/" in dpa._format_timestamp("1700000000")


def test_load_pending_reminder_rows_reads_sqlite(tmp_path):
    db = tmp_path / "line_bot.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE pending_reminder_extract ("
            "pending_id INTEGER PRIMARY KEY, group_id TEXT, user_id TEXT, "
            "message_id TEXT, text TEXT, created_at INTEGER, retries INTEGER, "
            "claimed_at INTEGER, status TEXT)"
        )
        conn.execute(
            "INSERT INTO pending_reminder_extract "
            "(pending_id, group_id, user_id, message_id, text, created_at, retries, claimed_at, status) "
            "VALUES (4, 'G1', 'U1', 'm1', '明天晚上7:15去教會4樓參加嗎哪小組查經', 1000, 1, 0, 'pending')"
        )
        conn.execute(
            "INSERT INTO pending_reminder_extract "
            "(pending_id, group_id, user_id, message_id, text, created_at, retries, claimed_at, status) "
            "VALUES (5, 'G1', 'U1', 'm2', 'done row', 1001, 0, 0, 'done')"
        )

    rows = dpa.load_pending_reminder_rows(db)

    assert len(rows) == 1
    assert rows[0].pending_id == 4
    assert rows[0].retries == 1
    assert "嗎哪小組" in rows[0].text


# ---------- _safe_load_pending ----------

def test_safe_load_pending_missing(tmp_path, monkeypatch):
    import pending_store
    fake = tmp_path / "nope.json"
    monkeypatch.setattr(pending_store, "PENDING_PATH", fake)
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / ".lock")
    data, status, size, present = dpa._safe_load_pending()
    assert status == "missing"
    assert present is False
    assert data == {}


def test_safe_load_pending_empty_file(tmp_path, monkeypatch):
    import pending_store
    fake = tmp_path / "p.json"
    fake.write_text("")
    monkeypatch.setattr(pending_store, "PENDING_PATH", fake)
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / ".lock")
    data, status, size, present = dpa._safe_load_pending()
    assert status == "empty"
    assert size == 0
    assert present is True


def test_safe_load_pending_corrupt(tmp_path, monkeypatch):
    import pending_store
    fake = tmp_path / "p.json"
    fake.write_text("{not valid json at all")
    monkeypatch.setattr(pending_store, "PENDING_PATH", fake)
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / ".lock")
    data, status, size, present = dpa._safe_load_pending()
    assert status == "corrupt"
    assert data == {}


def test_safe_load_pending_ok(tmp_path, monkeypatch):
    import pending_store
    fake = tmp_path / "p.json"
    payload = {"G1": [{"type": "image", "message_id": "m1", "timestamp": 1.0}]}
    fake.write_text(json.dumps(payload))
    monkeypatch.setattr(pending_store, "PENDING_PATH", fake)
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / ".lock")
    data, status, size, present = dpa._safe_load_pending()
    assert status == "ok"
    assert data == payload


# ---------- main() integration ----------

class _DummySendDm:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[str] = []

    def __call__(self, msg: str) -> bool:
        self.calls.append(msg)
        return self.ok


def test_main_dry_run_does_not_push(tmp_path, monkeypatch):
    import pending_store
    fake = tmp_path / "p.json"
    fake.write_text(json.dumps({"G1": [{"type": "image", "message_id": "m1", "timestamp": 1.0}]}))
    monkeypatch.setattr(pending_store, "PENDING_PATH", fake)
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(dpa, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(dpa, "STATE_PATH", tmp_path / "state" / "x.json")
    sender = _DummySendDm()
    monkeypatch.setattr(dpa, "_send_discord", lambda m: sender(m) or sender.ok)
    rc = dpa.main(["--dry-run"])
    assert rc == 0
    assert sender.calls == []


def test_main_skips_discord_when_pending_reply_disabled(tmp_path, monkeypatch):
    import pending_store

    fake = tmp_path / "p.json"
    fake.write_text(json.dumps({"G1": [{"type": "image", "message_id": "m1", "timestamp": 1.0}]}))
    monkeypatch.setattr(pending_store, "PENDING_PATH", fake)
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / ".lock")
    state_path = tmp_path / "state" / "x.json"
    monkeypatch.setattr(dpa, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(dpa, "STATE_PATH", state_path)
    monkeypatch.setattr(dpa, "pending_reply_enabled", lambda: False)
    sender = _DummySendDm(ok=True)
    monkeypatch.setattr(dpa, "_send_discord", sender)

    rc = dpa.main([])

    assert rc == 0
    assert sender.calls == []
    state = json.loads(state_path.read_text())
    assert state["ok"] is True
    assert state["status"] == "disabled"
    assert state["summary"]["discord_sent"] is False
    assert state["summary"]["discord_skipped"] is True


def test_main_pushes_when_leftover(tmp_path, monkeypatch):
    import pending_store
    fake = tmp_path / "p.json"
    fake.write_text(json.dumps({"G1": [{"type": "image", "message_id": "m1", "timestamp": 1.0}]}))
    monkeypatch.setattr(pending_store, "PENDING_PATH", fake)
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(dpa, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(dpa, "STATE_PATH", tmp_path / "state" / "x.json")
    monkeypatch.setattr(dpa, "SUPPRESS_OK_DM", False)
    sender = _DummySendDm(ok=True)
    monkeypatch.setattr(dpa, "_send_discord", sender)
    rc = dpa.main([])
    assert rc == 0
    assert len(sender.calls) == 1
    assert "⚠️" in sender.calls[0]


def test_main_pushes_when_zero_leftover_default(tmp_path, monkeypatch):
    """User said default = always push (有的話也推)."""
    import pending_store
    fake = tmp_path / "p.json"
    fake.write_text(json.dumps({}))
    monkeypatch.setattr(pending_store, "PENDING_PATH", fake)
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(dpa, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(dpa, "STATE_PATH", tmp_path / "state" / "x.json")
    monkeypatch.setattr(dpa, "SUPPRESS_OK_DM", False)
    sender = _DummySendDm(ok=True)
    monkeypatch.setattr(dpa, "_send_discord", sender)
    rc = dpa.main([])
    assert rc == 0
    assert len(sender.calls) == 1
    assert "✅" in sender.calls[0]


def test_main_suppresses_ok_when_flag_set(tmp_path, monkeypatch):
    import pending_store
    fake = tmp_path / "p.json"
    fake.write_text(json.dumps({}))
    monkeypatch.setattr(pending_store, "PENDING_PATH", fake)
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(dpa, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(dpa, "STATE_PATH", tmp_path / "state" / "x.json")
    monkeypatch.setattr(dpa, "SUPPRESS_OK_DM", True)
    sender = _DummySendDm(ok=True)
    monkeypatch.setattr(dpa, "_send_discord", sender)
    rc = dpa.main([])
    assert rc == 0
    assert sender.calls == []


def test_main_returns_1_when_discord_fails(tmp_path, monkeypatch):
    import pending_store
    fake = tmp_path / "p.json"
    fake.write_text(json.dumps({}))
    monkeypatch.setattr(pending_store, "PENDING_PATH", fake)
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(dpa, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(dpa, "STATE_PATH", tmp_path / "state" / "x.json")
    monkeypatch.setattr(dpa, "SUPPRESS_OK_DM", False)
    sender = _DummySendDm(ok=False)
    monkeypatch.setattr(dpa, "_send_discord", sender)
    rc = dpa.main([])
    assert rc == 1


def test_main_returns_1_when_corrupt(tmp_path, monkeypatch):
    import pending_store
    fake = tmp_path / "p.json"
    fake.write_text("{not valid")
    monkeypatch.setattr(pending_store, "PENDING_PATH", fake)
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(dpa, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(dpa, "STATE_PATH", tmp_path / "state" / "x.json")
    sender = _DummySendDm(ok=True)
    monkeypatch.setattr(dpa, "_send_discord", sender)
    rc = dpa.main([])
    assert rc == 1
    assert len(sender.calls) == 1
    assert "🚨" in sender.calls[0]


def test_state_file_written_with_summary(tmp_path, monkeypatch):
    """State file must mirror jobs_router shape and include the summary."""
    import pending_store
    fake = tmp_path / "p.json"
    fake.write_text(
        json.dumps(
            {
                "G1": [
                    {"type": "image", "message_id": "m1", "timestamp": 1.0},
                    {"type": "audio", "message_id": "m2", "timestamp": 2.0},
                    {"type": "video", "message_id": "m3", "timestamp": 3.0,
                     "download_failed": True},
                ]
            }
        )
    )
    state_dir = tmp_path / "state"
    state_path = state_dir / "last_run_daily-pending-audit.json"
    monkeypatch.setattr(pending_store, "PENDING_PATH", fake)
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(dpa, "STATE_DIR", state_dir)
    monkeypatch.setattr(dpa, "STATE_PATH", state_path)
    monkeypatch.setattr(dpa, "SUPPRESS_OK_DM", False)
    monkeypatch.setattr(dpa, "_send_discord", _DummySendDm(ok=True))

    rc = dpa.main([])
    assert rc == 0
    assert state_path.exists()

    state = json.loads(state_path.read_text())
    assert state["ok"] is True
    assert state["status"] == "completed"
    assert "started_at" in state and "finished_at" in state
    summary = state["summary"]
    assert summary["load_status"] == "ok"
    assert summary["total_unanswered_media"] == 2  # image + video, audio excluded
    assert summary["audio_excluded_count"] == 1
    assert summary["download_failed_count"] == 1
    assert summary["discord_sent"] is True
    assert summary["discord_skipped"] is False


def test_state_file_records_failure_status_on_corrupt(tmp_path, monkeypatch):
    import pending_store
    fake = tmp_path / "p.json"
    fake.write_text("{garbage")
    state_dir = tmp_path / "state"
    state_path = state_dir / "last_run.json"
    monkeypatch.setattr(pending_store, "PENDING_PATH", fake)
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / ".lock")
    monkeypatch.setattr(dpa, "STATE_DIR", state_dir)
    monkeypatch.setattr(dpa, "STATE_PATH", state_path)
    monkeypatch.setattr(dpa, "_send_discord", _DummySendDm(ok=True))

    dpa.main([])
    state = json.loads(state_path.read_text())
    assert state["ok"] is False
    assert state["status"] == "corrupt_pending"
    assert state["summary"]["load_status"] == "corrupt"


def test_format_message_download_failed_line_rendered():
    """Regression guard: a no-op replacing lines.append would not be caught
    without this assertion (gp1 nit)."""
    items = [
        {"type": "image", "message_id": "m1", "timestamp": 1.0,
         "download_failed": True},
        {"type": "video", "message_id": "m2", "timestamp": 2.0,
         "download_failed": True},
    ]
    r = dpa.build_report({"G1": items})
    msg = dpa.format_message(r)
    assert "2 則 download_failed" in msg
    assert "DL✗" in msg
