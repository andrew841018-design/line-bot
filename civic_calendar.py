"""Verified Taiwan election dates for the reply prompts and guards (2026-10-28).

Twice on 10/28 a fallback model answered the family from stale memory: 11/28
was "not a voting day" and the "latest" presidential election had been on
2025-01-11.  11/28 is the 2026 local-election day (the CEC announced it on
8/20); the last presidential and legislative election was on 2024-01-13.
A model only knows what its training saw, so the prompts now carry the
schedule itself, and ``reply_policy`` takes a sentence that only states these
dates as backed.  Every entry names its source.  Refresh this file when the
CEC announces the next election; past ``VALID_UNTIL`` the prompt line is left
out rather than go stale.
"""

from __future__ import annotations

from datetime import date

# (date, election, source)
ELECTIONS = (
    (date(2024, 1, 13), "總統、副總統及立法委員選舉", "中選會"),
    (date(2026, 11, 28), "地方公職人員選舉（九合一：直轄市長、縣市長、議員等）", "中選會 2026-08-20 公告"),
)
# Every local-election (九合一) day since they were combined, and every
# presidential election day since direct elections began (CEC).
LOCAL = (date(2014, 11, 29), date(2018, 11, 24), date(2022, 11, 26), date(2026, 11, 28))
PRESIDENTIAL = (
    date(1996, 3, 23), date(2000, 3, 18), date(2004, 3, 20), date(2008, 3, 22),
    date(2012, 1, 14), date(2016, 1, 16), date(2020, 1, 11), date(2024, 1, 13),
)
# The next presidential and legislative election falls in this year (the term
# ends 2028-05-20); as of 2026-10 its day was not announced.  The CEC will set
# it during 2027, so the list goes quiet well before then unless refreshed.
NEXT_PRESIDENTIAL_YEAR = 2028
VALID_UNTIL = date(2027, 3, 31)


def active(today: date) -> bool:
    """Past ``VALID_UNTIL`` nothing here is used: stale facts are worse than none."""
    return today <= VALID_UNTIL


def election_context(today: date) -> str:
    """One prompt line with the verified schedule; "" once it may be stale."""
    if not active(today):
        return ""
    weekdays = "一二三四五六日"
    parts = []
    for day, what, source in ELECTIONS:
        when = f"{day.year}年{day.month}月{day.day}日（週{weekdays[day.weekday()]}）"
        tense = "投票" if day >= today else "已舉行"
        parts.append(f"{when}{what}{tense}（{source}）")
    return (
        "【台灣選舉日程｜已查證】" + "；".join(parts)
        + f"；下一次總統、副總統及立委選舉在{NEXT_PRESIDENTIAL_YEAR}年（截至2026年10月日期尚未公告）。"
        "沒有附資料時，這裡列的台灣選舉以這裡為準，不要用記憶推算；"
        "沒列的選舉（補選、罷免、公投、其他國家）沒附資料就不要判斷。"
    )


def verified_election_dates(today: date) -> set[tuple[int, int, int]]:
    """(year, month, day) of every listed election; empty once stale."""
    if not active(today):
        return set()
    return {(day.year, day.month, day.day) for day, _, _ in ELECTIONS}


def next_presidential_year(today: date) -> int | None:
    return NEXT_PRESIDENTIAL_YEAR if active(today) else None


def election_date_known(
    year: int, month: int | None, day: int | None, *, local_too: bool, check_day: bool,
) -> bool | None:
    """Whether a presidential election (``local_too``: or a local one, for a
    bare 大選) was held on that date: True / False, or None when it cannot be
    told — the next presidential day, any later year, or the day itself when
    ``check_day`` is off (the US votes in a Taiwan presidential year, in November)."""
    if year >= NEXT_PRESIDENTIAL_YEAR:
        return None
    # A bare 大選 before the combined local elections: the list does not cover it.
    if local_too and year < LOCAL[0].year:
        return None
    days = PRESIDENTIAL + (LOCAL if local_too else ())
    held = [d for d in days if d.year == year]
    if not held:
        return False
    if month is None or not check_day:
        return True
    if day is None:
        return any(d.month == month for d in held)
    return any((d.month, d.day) == (month, day) for d in held)


def listed_kind(month: int, day: int, today: date) -> str | None:
    """"local" or "presidential" for a listed election day this year or next."""
    if not active(today):
        return None
    for listed, what, _ in ELECTIONS:
        if listed.year >= today.year and (listed.month, listed.day) == (month, day):
            return "local" if "地方" in what else "presidential"
    return None


def prompt_suffix(today: date) -> str:
    """The prompt line with a leading newline, or ""."""
    line = election_context(today)
    return f"\n{line}" if line else ""
