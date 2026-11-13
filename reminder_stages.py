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

# 2026-11-12 家人可要求提醒只推一次: a generic reminder may keep one stage
# (reminders.push_keep_stage; '' keeps the whole ladder).
KEEPABLE_STAGES: tuple[str, ...] = ("now", "1hr", "2hr", "4hr")
# (lower exclusive, upper inclusive) seconds before remind_at, as window_stage.
KEEP_WINDOW_SECONDS: dict[str, tuple[int, int]] = {
    "1hr": (1800, 5400),
    "2hr": (5400, 9000),
    "4hr": (12600, 16200),
}
_NOW_HALF_WINDOW_SECONDS = 900

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


def keep_stage_of(row: Mapping) -> str:
    """The one stage a family member asked to keep, or ''.

    Only generic reminders (no source_kind, no source_ref) honour it: a
    calendar mirror, a 前一天／當天 row or any value outside KEEPABLE_STAGES
    reads as no limit, so a stray value never stops a push run.
    """

    if str(row.get("source_kind") or "") or str(row.get("source_ref") or ""):
        return ""
    keep = str(row.get("push_keep_stage") or "")
    return keep if keep in KEEPABLE_STAGES else ""


def kept_stage_delivered(row: Mapping, keep: str) -> bool:
    """Whether the kept 1/2/4-hour stage really went out.

    A creation receipt (consume_open_stages) or a fold can set the flag
    without a push, so the flag alone is not proof.  A real push writes
    last_pushed_at; the limited row's other stages are held back and the
    windows do not overlap, so a push at or after the kept window's upper
    edge was the kept stage.
    """

    bounds = KEEP_WINDOW_SECONDS.get(keep)
    if bounds is None:
        return False
    if not int(row.get(FLAG_COLUMNS[keep]) or 0):
        return False
    last = int(row.get("last_pushed_at") or 0)
    return last > 0 and last >= int(row["remind_at"]) - bounds[1]


def keep_allows(row: Mapping, stage: str) -> bool:
    """Whether a push limit lets ``stage`` go out (shared by open_stage and the claim).

    The kept stage goes out as usual; the at-time push is the safety net when
    the kept stage never really went out (push failures, a later time
    change); every other stage, weekly included, is held back.
    """

    keep = keep_stage_of(row)
    if not keep or stage == keep:
        return True
    return stage == "now" and not kept_stage_delivered(row, keep)


def keep_window_state(remind_at: int, keep: str, now: int) -> str:
    """'ahead', 'open' or 'passed' for the kept stage's window at ``now``."""

    delta = int(remind_at) - int(now)
    if keep == "now":
        if delta > _NOW_HALF_WINDOW_SECONDS:
            return "ahead"
        return "open" if delta >= -_NOW_HALF_WINDOW_SECONDS else "passed"
    low, high = KEEP_WINDOW_SECONDS[keep]
    if delta > high:
        return "ahead"
    return "open" if delta > low else "passed"


def merged_keep_stage(primary_keep: object, peer_keeps) -> str:
    """The limit a merged row keeps: its own, else the first peer's that has one."""

    for value in (primary_keep, *(peer_keeps or ())):
        keep = str(value or "")
        if keep in KEEPABLE_STAGES:
            return keep
    return ""


def open_stage(row: Mapping, now: int) -> str | None:
    """The stage to push for ``row`` at ``now``; None means nothing is due.

    ``row`` needs remind_at, the pushed_* flags and last_weekly_at, plus
    source_kind/source_ref/source_text, push_keep_stage and last_pushed_at
    when present.
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
    if not keep_allows(row, stage):
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
