"""Shared fakes of the reply-path tests (2026-10-28, review R2 GP2 #6).  Synthetic data only.

`test_reply_400_recovery.py` and `test_exercise_nudge.py` both need a scripted LINE, LINE's error
bodies and a due reminder.  The nudge tests used to import them from the recovery tests, so a
rename or an ImportError there silently dropped the nudge's privacy invariants from the run; both
import from here now.

This file holds no tests.  It is named `test_*` on purpose: the monitor's code-drift check treats
every other top-level `.py` as production code (`ops/monitors/line_bot.py`, `_iter_app_python_files`),
and a helper that only tests use must not restart the bot when it changes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from linebot.v3.messaging.exceptions import ApiException

import main
import memory
import reminder_push

GROUP = "GRP-400"
TZ = ZoneInfo("Asia/Taipei")


# ── fake LINE ───────────────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status, reason, data, headers):
        self.status, self.reason, self.data, self._headers = status, reason, data, headers

    def getheaders(self):
        return self._headers


def api_error(status, body=None, reason="Bad Request"):
    payload = json.dumps(body if body is not None else {"message": "x"}).encode()
    return ApiException(http_resp=_Resp(status, reason, payload, {"x-line-request-id": "req-400"}))


def refused(*indexes, prop="text"):
    """LINE's 400 body naming the refused message(s)."""
    return {
        "message": f"The request body has {len(indexes)} error(s)",
        "details": [{"message": "Invalid", "property": f"messages[{i}].{prop}"} for i in indexes],
    }


def bad(*indexes, prop="text"):
    return api_error(400, refused(*indexes, prop=prop))


BAD_UNNAMED = api_error(400, {"message": "The request body could not be understood"})
BAD_TOKEN = api_error(400, {"message": "Invalid reply token"})
MONTHLY = {"message": "You have reached your monthly limit."}


class FakeLine:
    """reply_message / push_message of a scripted LINE.  ``script`` holds the outcome of each
    reply_message call in turn (an exception to raise; None accepts); ``push_script`` the same for
    push_message.  Every call is recorded."""

    def __init__(self):
        self.script: list = []
        self.push_script: list = []
        self.replies: list[list] = []
        self.pushes: list[tuple[list, str | None]] = []
        self._ids = iter(range(1, 10**6))

    def reply_message(self, request):
        self.replies.append(list(request.messages))
        outcome = self.script.pop(0) if self.script else None
        if outcome is not None:
            raise outcome
        return SimpleNamespace(sent_messages=[SimpleNamespace(id=f"sent-{next(self._ids)}") for _ in request.messages])

    def push_message(self, request, x_line_retry_key=None):
        self.pushes.append((list(request.messages), x_line_retry_key))
        outcome = self.push_script.pop(0) if self.push_script else None
        if outcome is not None:
            raise outcome
        return SimpleNamespace(sent_messages=[SimpleNamespace(id=f"push-{next(self._ids)}") for _ in request.messages])


class FakeApiClient:
    def __init__(self, _config):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def install_fake_line(monkeypatch) -> FakeLine:
    """Point `main._reply` at a scripted LINE: not muted, no legacy pending branch, no reminder
    piggyback (a test turns that on itself), a fresh token -> inbound table.  Returns the fake."""
    fake = FakeLine()
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: False)
    monkeypatch.setattr(main, "ApiClient", FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", lambda _client: fake)
    monkeypatch.setattr(main, "_get_line_config", lambda: object())
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    return fake


def kinds(batch):
    return [type(message).__name__ for message in batch]


def texts(batch):
    return [str(getattr(message, "text", "") or "") for message in batch]


# ── reminders and what they leave in the database ───────────────────────────────────────────


def _day(offset):
    return (datetime.now(TZ) + timedelta(days=offset)).date()


def _at(day, hour, minute=0):
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).timestamp())


def due_natural(monkeypatch, action="繳房屋稅", *, user="U_A", day_offset=3, group=GROUP):
    """A natural reminder due for its 1 hour notice right now.  Returns its id."""
    day = _day(day_offset)
    rid, outcome = memory.add_reminder_with_outcome(
        group, user, action, _at(day, 14), source_text=f"{day.month}/{day.day} 14:00 {action}", time_kind="clock"
    )
    assert outcome == "created"
    monkeypatch.setattr(reminder_push, "_now_ts", lambda: _at(day, 13))
    return rid


def claims():
    """(subject_ref, state) of every delivery claim row."""
    with memory._conn() as c:
        return sorted(c.execute("SELECT subject_ref, state FROM reminder_delivery_claims").fetchall())


def pushed_1hr(rid):
    with memory._conn() as c:
        return int(c.execute("SELECT pushed_1hr FROM reminders WHERE reminder_id=?", (rid,)).fetchone()[0])


def raw_bot_texts():
    with memory._conn() as c:
        return [row[0] for row in c.execute("SELECT text FROM raw_messages WHERE user_id='__bot__' ORDER BY rowid").fetchall()]
