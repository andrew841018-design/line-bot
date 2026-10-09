"""The reminder list as events, one entry each (Andrew 2026-10-07).

「透過命令叫出的提醒事項，我不要重複，你可能把同一件事提醒三次當成三筆，
不對，我只要一筆，我只要知道有哪些代辦事項」.  The reminders table keeps one
row per reminder somebody asked for, so one event can own several rows: a
前一天／當天 pair (contextual_date_once, or actions labelled 「…（前一天提醒）」),
a calendar mirror next to a reminder someone also asked for, same-event rows
the write-time merge missed, and a todo for the same thing.  The list folds
them into one entry per event.  It only reads: every row keeps its own pushes.

Pure: no database, no LINE.  Rows are ``memory.list_pending_reminders`` dicts;
todos are given as row-like dicts (see :func:`todo_item`).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import reminder_intent
import reminder_stages

TW = ZoneInfo("Asia/Taipei")

CONTEXTUAL_SOURCE_KIND = "contextual_date_once"
_EVERYONE = frozenset({"全家", "all", "@all"})
_CLOCK_RE = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)")
_RELATIONAL_RE = re.compile(r"^(?:前|之前|以前|後|之後|以後)")
# contextual_date_once actions: 「{actor} {M/D}[ {HH:MM}] {title}」 before the label.
_CONTEXTUAL_ACTION_RE = re.compile(
    r"^(?P<actor>\S+) \d{1,2}/\d{1,2}(?: (?P<clock>(?:[01]\d|2[0-3]):[0-5]\d))? (?P<title>.+)$"
)


@dataclass
class Entry:
    """One event in the list."""

    event_date: date | None
    clock: str | None  # "14:00", a daypart such as "早上", or None
    action: str  # the representative wording, without an offset label
    people: tuple[str, ...]
    rows: list[dict] = field(default_factory=list)  # reminder rows, primary first
    todos: list[dict] = field(default_factory=list)


@dataclass
class _Item:
    source: dict
    is_todo: bool
    labelled: bool  # one of a deliberate 前一天／當天 set: its clock is not the event's
    action: str
    event_date: date | None
    time_kind: str
    hhmm: str
    clock: str | None
    clocks: frozenset[str]  # event clocks it names, to tell 09:00 from 15:00 apart
    names: tuple[str, ...]
    shown: tuple[str, ...]  # who messages put first: names, or the owner (2026-10-09)
    everyone: bool
    key: str
    match_text: str
    identity: list[str]


def todo_item(task: object, due_date: object, owner: str = "", user_id: str = "") -> dict:
    """A todo in the shape :func:`build_entries` reads."""

    return {
        "task": str(task or ""),
        "due_date": str(due_date or ""),
        "owner": owner,
        "user_id": user_id,
    }


def _hhmm(remind_at: int) -> str:
    return datetime.fromtimestamp(int(remind_at), TW).strftime("%H:%M")


def _names(raw: object) -> tuple[str, ...]:
    """A people field as plain names, without @ or repeats.

    A string is a database field (JSON text such as mention_aliases); one
    person's name goes in as ``[name]``, never parsed (2026-10-10 review).
    """
    if isinstance(raw, str):
        # a row read straight from SQLite keeps mention_aliases as JSON text
        text = raw.strip()
        try:
            raw = json.loads(text) if text.startswith("[") else [raw]
        except ValueError:
            raw = [raw]
    names: list[str] = []
    for value in raw or []:
        name = str(value or "").strip().lstrip("@＠")
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _with_people(text: str, names: tuple[str, ...]) -> str:
    """The text the same-event test reads: participants it does not name go in front,
    so 「回診」 for 媽媽 and 「媽媽 10/12 回診」 read as the same people."""

    missing = [name for name in names if name not in text]
    return f"{' '.join(missing)} {text}".strip() if missing else text


# 2026-10-10 review（第三輪）：顯示名稱是「家人」「我們」「所有人🙂」這種的，也讀起來像全家。
_EVERYONE_WORDS = frozenset({"全家", "全家人", "家人", "家族", "大家", "所有人", "我們", "全部", "全員", "all", "everyone"})


def _is_everyone(name: str) -> bool:
    """all／大家／所有人… : the whole family, which :func:`subject_prefix` writes as 「全家」."""
    import line_mentions

    letters = "".join(
        ch for ch in unicodedata.normalize("NFKC", str(name or "")) if unicodedata.category(ch)[0] in "LN"
    ).lower()
    return name in _EVERYONE or letters in _EVERYONE_WORDS or line_mentions.is_all_participants([name])


def subject_prefix(text: str, people: object) -> list[str]:
    """The names :func:`subject_first` puts in front of ``text``."""
    names = [name for name in _names(people) if name not in text]
    # all／大家／所有人… are the whole family: one 「全家」, as pushes say it
    if any(_is_everyone(name) for name in names):
        return [] if "全家" in text else ["全家"]
    return names


# 2026-10-09：只 @ 建立者的提醒（沒有點名對象）也要有主詞——推播 @ 的就是建立者。
# 事項已經寫到某位家人或全家時不加，「爸爸 回診」不會變成「媽媽 爸爸 回診」。
_PERSON_WORDS = (
    *getattr(reminder_intent, "_SAME_EVENT_KIN", ("爸爸", "媽媽", "姊姊", "哥哥", "弟弟", "妹妹")),
    "全家", "家族", "家人", "大家", "我們",
)


def _names_someone(text: str) -> bool:
    if any(word in text for word in _PERSON_WORDS):
        return True
    import line_mentions

    try:
        names = list(line_mentions.configured_family_alias_mapping(include_short=True))
    except Exception:
        names = []
    # 2026-10-10 review（第三輪）：不看 LINE 顯示名稱——那是家人自己取的，「健康」「平安」
    # 這種一般用字會讓「繳健康保險費」少了主詞；「成員甲 去小陳家」多一個主詞無妨。
    return any(name and name in text for name in names)


def owner_subject(row: Mapping, text: str | None = None) -> tuple[str, ...]:
    """沒有點名對象時放在事項前面的人：設這個提醒的人（推播 @ 的就是他）。

    有點名對象、事項已寫到某位家人或全家、或建立者沒有設定稱呼（或稱呼代表全體）時回 ()。
    """
    if _names(row.get("mention_aliases")):
        return ()
    source_kind = str(row.get("source_kind") or "")
    if source_kind == "calendar_event" or reminder_stages.is_calendar_mirror(
        source_kind, row.get("source_ref")
    ):
        return ()  # 日曆活動的人寫在活動裡，不是建立者
    owner = str(row.get("user_id") or "")
    if not owner or owner == "__bot__":
        return ()
    import line_mentions

    try:
        # 沒有本機別名就用 bot 查到的 LINE 顯示名稱（Andrew 2026-10-09）。
        # 2026-10-10 review（第三輪）：顯示名稱和某人的別名相同（「媽媽」）時不用，
        # 免得讀起來像那個人的事（主程式之後也會把它從檔案移掉）。
        display = line_mentions.display_name_for_user_id(owner) or ""
        if display and display in line_mentions.configured_family_alias_mapping(include_short=True):
            display = ""
        # 2026-10-10 review：名字以 list 傳進 _names，不當 JSON 解析（顯示名稱「[1]」不會變成「1」）。
        alias = _names([line_mentions.alias_for_user_id(owner) or display])
    except Exception:
        return ()
    # 2026-10-10 review：代表全體的名字（ALL、全家…）當作沒有名字，不寫成「全家 繳電話費」
    if not alias or _is_everyone(alias[0]):
        return ()
    wording = str(row.get("action") or "") if text is None else text
    if alias[0] in wording or _names_someone(wording):
        return ()
    return alias


def shown_people(row: Mapping, text: str | None = None) -> tuple[str, ...]:
    """推播、清單、收據放在事項前面的人：點名對象；沒有就是建立者（2026-10-09）。"""
    return _names(row.get("mention_aliases")) or owner_subject(row, text)


def subject_first(text: str, people: object) -> str:
    """「媽媽 家長會」：還沒寫在 text 裡的人放最前面（Andrew 2026-10-07：主詞放前面）。

    全家／all 算一個人，寫「全家」。
    """
    names = subject_prefix(text, people)
    if not names:
        return text
    return f"{'、'.join(names)} {text}" if text else "、".join(names)


def shown_names(shown: str, text: str) -> tuple[str, ...] | None:
    """Undo :func:`subject_first`: the names in front of ``text`` in ``shown``.

    () when ``shown`` is ``text`` itself, None when it is neither.  Normalize
    both the same way first; nothing here checks that the names are people.
    """
    if shown == text:
        return ()
    if not text or not shown.endswith(" " + text):
        return None
    names = tuple(shown[: -len(text) - 1].split("、"))
    return names if all(names) else None


def _make_item(
    source: dict,
    *,
    is_todo: bool,
    labelled: bool,
    action: str,
    event_date: date | None,
    time_kind: str,
    hhmm: str,
    clock: str | None,
    clocks: frozenset[str],
    names: tuple[str, ...],
    identity_extra: Sequence[str] = (),
    shown: tuple[str, ...] | None = None,
) -> _Item:
    people = tuple(name for name in names if name not in _EVERYONE)
    match_text = _with_people(action, people)
    return _Item(
        source=source,
        is_todo=is_todo,
        labelled=labelled,
        action=action,
        event_date=event_date,
        time_kind=time_kind,
        hhmm=hhmm,
        clock=clock,
        clocks=clocks,
        names=names,
        shown=names if shown is None else shown,
        everyone=any(name in _EVERYONE for name in names),
        key=re.sub(r"\s+", "", reminder_intent.normalize_text(action)).casefold(),
        match_text=match_text,
        identity=[match_text, *identity_extra],
    )


def _reminder_item(row: dict) -> _Item:
    raw_action = str(row.get("action") or "")
    action = reminder_intent.strip_reminder_offset_marker(raw_action) or raw_action
    remind_at = int(row.get("remind_at") or 0)
    remind_date = datetime.fromtimestamp(remind_at, TW).date()
    hhmm = _hhmm(remind_at)
    names = _names(row.get("mention_aliases"))
    source_kind = str(row.get("source_kind") or "")
    labelled = source_kind == CONTEXTUAL_SOURCE_KIND or reminder_intent.has_reminder_offset_marker(
        raw_action
    )
    identity_extra = [
        str(item.get("action") or "")
        for item in (row.get("merged_details") or [])
        if isinstance(item, dict) and item.get("action")
    ]
    if labelled:
        # The row fires before or on the event day at a reminder clock; the
        # event's own clock, if any, is in the words.
        clock = None
        contextual = (
            _CONTEXTUAL_ACTION_RE.match(action) if source_kind == CONTEXTUAL_SOURCE_KIND else None
        )
        if contextual:
            clock = contextual.group("clock")
            action = contextual.group("title").strip()
            names = _names([contextual.group("actor"), *names])
        clocks = frozenset(_CLOCK_RE.findall(action)) | (
            frozenset({clock}) if clock else frozenset()
        )
        clock = clock or next(iter(sorted(clocks)), None)
        return _make_item(
            row,
            is_todo=False,
            labelled=True,
            action=action,
            event_date=remind_date + timedelta(days=reminder_intent.reminder_offset_days(raw_action) or 0),
            time_kind="none",
            hhmm=hhmm,
            clock=clock,
            clocks=clocks,
            names=names,
            identity_extra=identity_extra,
            shown=names or owner_subject(row, action),
        )
    time_kind = str(row.get("time_kind") or "")
    mirror = reminder_stages.is_calendar_mirror(source_kind, row.get("source_ref"))
    if hhmm == "00:00" and (mirror or reminder_intent.time_rank(time_kind) == 2):
        # an all-day calendar event, or a legacy row without a clock
        time_kind = "none"
    rank = reminder_intent.time_rank(time_kind)
    clock = hhmm if rank == 2 else time_kind.split(":", 1)[1] if rank == 1 else None
    return _make_item(
        row,
        is_todo=False,
        labelled=False,
        action=action,
        event_date=remind_date,
        time_kind=time_kind,
        hhmm=hhmm,
        clock=clock,
        clocks=frozenset({hhmm}) if rank == 2 else frozenset(),
        names=names,
        identity_extra=identity_extra,
        shown=names or owner_subject(row, action),
    )


def _todo_item(todo: dict) -> _Item:
    try:
        due = date.fromisoformat(str(todo.get("due_date") or ""))
    except ValueError:
        due = None
    action = str(todo.get("task") or "").strip()
    clocks = frozenset(_CLOCK_RE.findall(action))
    return _make_item(
        todo,
        is_todo=True,
        labelled=False,
        action=action,
        event_date=due,
        time_kind="none",
        hhmm="00:00",
        clock=None,
        clocks=clocks,
        names=_names([todo.get("owner")] if todo.get("owner") else []),
    )


def _people_compatible(left: _Item, right: _Item) -> bool:
    if left.everyone or right.everyone:
        return True
    a = set(left.names)
    b = set(right.names)
    return not a or not b or a <= b or b <= a


def _reads_into(incoming: _Item, kept: _Item) -> bool:
    user_id = str(incoming.source.get("user_id") or "")
    return reminder_intent.mention_matches_reminder(
        incoming.match_text,
        kept.match_text,
        time_kind=incoming.time_kind,
        hhmm=incoming.hhmm,
        kept_kind=kept.time_kind,
        kept_hhmm=kept.hhmm,
        kept_identity=kept.identity,
        kept_source=str(kept.source.get("source_text") or ""),
        mentions=[name for name in incoming.names if name not in _EVERYONE],
        kept_mentions=[name for name in kept.names if name not in _EVERYONE],
        same_author=bool(user_id) and user_id == str(kept.source.get("user_id") or ""),
    )


def _same_clock_contained(left: _Item, right: _Item) -> bool:
    """Same day, same exact clock, and one activity inside the other: the
    calendar's 「家族烤肉」 and someone's 「烤肉」 at 18:00."""

    if not left.clocks or left.clocks != right.clocks:
        return False
    names = tuple(dict.fromkeys(n for n in (*left.names, *right.names) if n not in _EVERYONE))
    if reminder_intent.same_event_identity_conflict(
        left.match_text, right.identity, names
    ) or reminder_intent.same_event_identity_conflict(right.match_text, left.identity, names):
        return False
    short, long = sorted(
        (
            reminder_intent.same_event_core(left.match_text, names),
            reminder_intent.same_event_core(right.match_text, names),
        ),
        key=len,
    )
    if len(short) < 2 or short not in long:
        return False
    # 「買菜前先列清單」 is its own task before 「買菜」
    return not _RELATIONAL_RE.match(long[long.index(short) + len(short):])


def _same_event(left: _Item, right: _Item) -> bool:
    if left.event_date is None or left.event_date != right.event_date:
        return False
    if left.clocks and right.clocks and not left.clocks & right.clocks:
        return False  # two occurrences the same day (09:00 and 15:00)
    if not _people_compatible(left, right):
        return False
    if left.key == right.key and reminder_intent.times_compatible(
        left.time_kind, left.hhmm, right.time_kind, right.hhmm
    ):
        return True
    return (
        _reads_into(left, right)
        or _reads_into(right, left)
        or _same_clock_contained(left, right)
    )


def _primary_rank(item: _Item) -> tuple:
    """A row someone asked for as such first; then the most precise time,
    then the earliest, then the most words."""

    return (
        item.labelled,
        -reminder_intent.time_rank(item.time_kind),
        int(item.source.get("remind_at") or 0),
        -len(item.action),
        int(item.source.get("reminder_id") or 0),
    )


def _entry(items: list[_Item]) -> Entry:
    rows = [item for item in items if not item.is_todo]
    todos = [item for item in items if item.is_todo]
    rows.sort(key=_primary_rank)
    primary = rows[0] if rows else todos[0]
    clock = primary.clock or next((item.clock for item in items if item.clock), None)
    # Anyone named wins; only when no row names anyone do the owners go first
    # (2026-10-09), so 「媽媽」 plus another row's owner never reads as two people.
    ordered = [primary, *items]
    named = any(item.names for item in ordered)
    people: list[str] = []
    for item in ordered:
        for name in (item.names if named else item.shown):
            if name not in people:
                people.append(name)
    return Entry(
        event_date=primary.event_date,
        clock=clock,
        action=primary.action,
        people=tuple(people),
        rows=[item.source for item in rows],
        todos=[item.source for item in todos],
    )


def _sort_key(entry: Entry) -> tuple:
    clock = entry.clock if entry.clock and _CLOCK_RE.fullmatch(entry.clock) else ""
    return (entry.event_date or date.max, clock.zfill(5), entry.action)


def _clocks(group: list[_Item]) -> frozenset[str]:
    return frozenset().union(*(item.clocks for item in group))


def _fits(left: list[_Item], right: list[_Item]) -> bool:
    """Two groups can be one event: the clocks they name agree and nobody in
    one is somebody else in the other."""

    a, b = _clocks(left), _clocks(right)
    if a and b and not a & b:
        return False
    return all(_people_compatible(x, y) for x in left for y in right)


def _matches(left: list[_Item], right: list[_Item]) -> bool:
    return any(_same_event(x, y) for x in left for y in right)


def build_entries(rows: Sequence[dict], todos: Sequence[dict] = ()) -> list[Entry]:
    """One entry per event, sorted by date and time."""

    items = [_reminder_item(row) for row in rows] + [_todo_item(todo) for todo in todos]
    # 1. Items that both name a clock, or both name none, fold when they match.
    groups: list[list[_Item]] = []
    for item in items:
        merged = [item]
        for group in list(groups):
            if (
                bool(_clocks(group)) == bool(item.clocks)
                and _fits(group, merged)
                and _matches(group, [item])
            ):
                groups.remove(group)
                merged = group + merged
        groups.append(merged)
    # 2. A group without a clock joins the one clocked group it matches.  If it
    #    matches two (吃藥 next to 吃藥 09:00 and 吃藥 21:00) it stays on its
    #    own: guessing could hide one of them.
    clocked = [group for group in groups if _clocks(group)]
    result = list(clocked)
    for group in groups:
        if _clocks(group):
            continue
        candidates = [
            other for other in clocked if _fits(other, group) and _matches(other, group)
        ]
        if len(candidates) == 1:
            candidates[0].extend(group)
        else:
            result.append(group)
    return sorted((_entry(group) for group in result), key=_sort_key)
