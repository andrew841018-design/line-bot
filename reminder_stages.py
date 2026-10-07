"""The 5/8 reminder ladder as pure functions (2026-10-04).

``reminder_push`` decides what to push with :func:`open_stage`; ``memory``
uses :func:`stages_opening_within` to mark the stages a creation receipt has
already announced.  Keeping both on one source of truth means the windows
can never drift apart, and this module imports neither of them (no cycle).

Ladder (Andrew 2026-05-08, unchanged): weekly for 7–30 days out, then 3 days,
1 day, 4 hours, 2 hours, 1 hour and at the time.  Windows never overlap, so a
missed stage is never replayed later: once its window has closed it is gone.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

TW = ZoneInfo("Asia/Taipei")

FLAG_COLUMNS: dict[str, str] = {
    "3d": "pushed_3d",
    "1d": "pushed_1d",
    "4hr": "pushed_4hr",
    "2hr": "pushed_2hr",
    "1hr": "pushed_1hr",
    "now": "pushed_now",
}
# A receipt may stand in for these; "now" is the reminder itself and always goes out.
CONSUMABLE_STAGES: tuple[str, ...] = ("weekly", "3d", "1d", "4hr", "2hr", "1hr")
WEEKLY_REPEAT_DAYS = 6.5

# A timed calendar mirror carries its clock in the source text.
_MIRROR_CLOCK_RE = re.compile(r"(?:活動)?時間：(?:[01]\d|2[0-3]):[0-5]\d(?:\b|$)")


def is_calendar_mirror(source_kind: object, source_ref: object) -> bool:
    return str(source_kind or "") == "calendar_event" and bool(str(source_ref or ""))


def window_stage(
    remind_at: int,
    now: int,
    *,
    source_kind: object = "",
    source_ref: object = "",
    source_text: object = "",
) -> str | None:
    """The stage whose window contains ``now``, ignoring what was already sent."""

    remind_at = int(remind_at)
    now = int(now)
    if str(source_kind or "") == "contextual_date_once":
        # A user-requested 前一天／當天 reminder fires once on its own date.
        if (
            now >= remind_at
            and datetime.fromtimestamp(now, TW).date()
            == datetime.fromtimestamp(remind_at, TW).date()
        ):
            return "now"
        return None
    mirror = is_calendar_mirror(source_kind, source_ref)
    # Calendar events split delivery ownership by granularity: the calendar
    # sender owns day-level stages, a timed mirror keeps only 4/2/1-hour and
    # at-time delivery, and an all-day mirror has no trustworthy clock.
    if mirror and not _MIRROR_CLOCK_RE.search(str(source_text or "")):
        return None
    delta = remind_at - now
    hours = delta / 3600
    days = delta / 86400
    if -0.25 <= hours <= 0.25:
        return "now"
    if 0.5 < hours <= 1.5:
        return "1hr"
    if 1.5 < hours <= 2.5:
        return "2hr"
    if 3.5 < hours <= 4.5:
        return "4hr"
    if mirror:
        return None
    if hours > 4.5 and 0.5 < days <= 2:
        return "1d"
    if 2 < days <= 4:
        return "3d"
    if 4 < days < 7:
        return None  # dead zone
    if 7 <= days <= 30:
        return "weekly"
    return None  # more than 30 days out: too early to say anything


def weekly_owed(last_weekly_at: object, now: int) -> bool:
    last = int(last_weekly_at or 0)
    days_since = (int(now) - last) / 86400 if last else 999
    return days_since >= WEEKLY_REPEAT_DAYS


def open_stage(row: Mapping, now: int) -> str | None:
    """The stage to push for ``row`` at ``now``; None means nothing is due.

    ``row`` needs remind_at, the pushed_* flags and last_weekly_at, plus
    source_kind/source_ref/source_text when present.
    """

    stage = window_stage(
        row["remind_at"],
        now,
        source_kind=row.get("source_kind"),
        source_ref=row.get("source_ref"),
        source_text=row.get("source_text"),
    )
    if stage is None:
        return None
    if stage == "weekly":
        return "weekly" if weekly_owed(row.get("last_weekly_at"), now) else None
    return None if int(row.get(FLAG_COLUMNS[stage]) or 0) else stage


def stages_opening_within(
    row: Mapping,
    now: int,
    horizon_seconds: int,
    *,
    step_seconds: int = 30,
) -> list[str]:
    """Window stages open at some moment in [now, now + horizon], in order.

    Flags are ignored here; the caller decides what is still owed.  Every
    window is at least 30 minutes wide, so sampling every 30 seconds plus the
    end point cannot step over one.
    """

    now = int(now)
    horizon = max(0, int(horizon_seconds))
    found: list[str] = []
    moments = list(range(now, now + horizon + 1, max(1, int(step_seconds))))
    if moments[-1] != now + horizon:
        moments.append(now + horizon)
    for moment in moments:
        stage = window_stage(
            row["remind_at"],
            moment,
            source_kind=row.get("source_kind"),
            source_ref=row.get("source_ref"),
            source_text=row.get("source_text"),
        )
        if stage is not None and stage not in found:
            found.append(stage)
    return found
