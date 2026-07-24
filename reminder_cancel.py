"""Pure parser and resolver for natural-language reminder cancellation.

The module deliberately performs no database writes.  A caller should:

1. call :func:`parse_cancel_request` with the current LINE text and, when the
   message is a quote reply, the separately recovered quoted text;
2. load pending cancellation candidates for the current group;
3. call :func:`resolve_cancel_request`; and
4. mutate state only when the returned status is ``MATCHED``.

Matching is deterministic: Unicode/whitespace-normalized actions must be equal
and scheduled timestamps must identify the same minute in ``Asia/Taipei``.
There is no fuzzy, newest-row, or LLM fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
import unicodedata
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")


class ReferenceSource(str, Enum):
    """Where an exact reminder reference was recovered."""

    CURRENT = "current"
    QUOTED = "quoted"


class CancelParseStatus(str, Enum):
    """Result of recognizing and parsing the user's cancellation text."""

    NOT_CANCEL = "not_cancel"
    AMBIGUOUS = "ambiguous"
    READY = "ready"


class CancelResolutionStatus(str, Enum):
    """Result of matching a parsed request against stored candidates."""

    NOT_CANCEL = "not_cancel"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    MATCHED = "matched"


@dataclass(frozen=True)
class ReminderReference:
    """Exact action and Taipei-local scheduled minute found in user-visible text.

    ``remind_at`` is an epoch timestamp at second zero of the referenced minute.
    """

    action: str
    remind_at: int
    source: ReferenceSource
    action_variants: tuple[str, ...] = ()
    reference_kind: str = "scheduled_reminder"
    event_time_specified: bool = False
    location_hint: str = ""


@dataclass(frozen=True)
class CancelRequest:
    """Cancellation parse outcome with current and quoted evidence kept apart."""

    status: CancelParseStatus
    current_reference: ReminderReference | None = None
    quoted_reference: ReminderReference | None = None
    reason: str = ""

    @property
    def reference(self) -> ReminderReference | None:
        """Return the usable reference only when parsing was unambiguous."""

        if self.status is not CancelParseStatus.READY:
            return None
        if (
            self.quoted_reference is not None
            and self.quoted_reference.reference_kind == "calendar_event"
        ):
            return self.quoted_reference
        return self.current_reference or self.quoted_reference


@dataclass(frozen=True)
class CancelResolution:
    """Safe match outcome for a main-handler compare-and-set cancellation."""

    status: CancelResolutionStatus
    reminder_id: int | None = None
    action: str | None = None
    remind_at: int | None = None
    reference: ReminderReference | None = None
    reason: str = ""


_DATE = r"(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})"
_TIME = r"(?P<hour>\d{1,2}):(?P<minute>\d{2})"

_CREATION_RE = re.compile(
    rf"^[ \t]*(?:時間|时间)\s*[：:]\s*{_DATE}[ \t]+{_TIME}[ \t]*\r?\n"
    r"[ \t]*(?:事項|事项)\s*[：:]\s*(?P<action>[^\r\n]+?)[ \t]*$",
    re.MULTILINE,
)
_TODO_RE = re.compile(
    r"^[ \t]*(?:\d+\.\s*)?(?P<todo_month>\d{1,2})/"
    r"(?P<todo_day>\d{1,2})[（(][一二三四五六日天][）)]\s*"
    r"(?P<todo_hour>\d{1,2}):(?P<todo_minute>\d{2})[ \t]*\r?\n"
    r"[ \t]*(?:事項|事项)\s*[：:]\s*(?P<todo_action>[^\r\n]+?)[ \t]*$"
    r"(?:\r?\n[ \t]*(?:細節|细节)\s*[：:]\s*"
    r"(?P<todo_detail>[^\r\n]+?)[ \t]*$)?",
    re.MULTILINE,
)
_EVENT_RE = re.compile(
    rf"^[ \t]*📅\s*{_DATE}(?:\s+{_TIME})?[ \t]*\r?\n"
    r"[ \t]*🎯\s*(?P<action>[^\r\n]+?)[ \t]*"
    r"(?:\r?\n[ \t]*📍\s*(?P<location>[^\r\n]+?)[ \t]*)?$",
    re.MULTILINE,
)
_DATETIME_ACTION_RE = re.compile(
    rf"(?<!\d){_DATE}[ \t]+{_TIME}[ \t]+(?P<action>[^\r\n]+?)[ \t]*$",
    re.MULTILINE,
)
_CANCEL_VERB_PATTERN = r"(?:取消(?:掉)?|刪除|刪掉|移除)"
_CANCEL_VERB_RE = re.compile(_CANCEL_VERB_PATTERN)
_DEICTIC_TARGET_RE = re.compile(
    r"(?:(?:這|該)(?:一)?(?:個|則)(?:提醒)?|(?:這|該)提醒)"
)
_PRE_CANCEL_NEGATION_RE = re.compile(
    r"(?:不要|不用|先不要|先別|別|不想|不必|不需要|無須|"
    r"沒有要|沒要|不是(?:要)?|並非(?:要)?|"
    r"沒有說要|沒說要|不准|不能|不可以)"
)
_FULL_DATETIME_HINT_RE = re.compile(
    r"(?<!\d)\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}\s+\S"
)
_DEICTIC_TARGET_SUFFIX_RE = re.compile(
    r"(?:"
    r"[\s，,；;]+(?:這|該)(?:(?:一)?個提醒|(?:一)?則(?:提醒)?)"
    r"|[\s，,；;]+(?:這|該)(?:一)?(?:個提醒|則提醒?|個|則)"
    r"\s*(?:取消|刪除|移除|不用了|不要了)"
    r")"
    r"\s*[。.!！]?$"
)

_NEGATED_CANCEL_PATTERNS = (
    re.compile(
        r"(?:不要|不用|先別|別|不想|不必|不需要|無須|沒有要|沒要|不是要|並非要)"
        r"\s*(?:再\s*)?(?:把|將)?\s*(?:這|該)?(?:個|則)?\s*"
        r"提醒\s*(?:取消|刪除|移除)"
    ),
    re.compile(
        r"(?:不要|不用|先別|別|不想|不必|不需要|無須|沒有要|沒要|不是要|並非要)"
        r"\s*(?:再\s*)?(?:取消|刪除|移除)\s*(?:這|該)?(?:個|則)?\s*提醒"
    ),
    re.compile(r"(?:這|該)(?:個|則)?\s*提醒\s*(?:不要|先別|別)\s*取消"),
)
_CREATION_PATTERNS = (
    re.compile(r"(?<!已)(?:新增|建立|設定|設置|加一個)\s*(?:一個)?\s*提醒"),
    re.compile(r"(?:請\s*)?提醒\s*(?:我|我們|一下).{0,40}取消"),
)
_HELP_PATTERNS = (
    re.compile(r"(?:怎麼|如何|怎樣).{0,16}取消\s*提醒"),
    re.compile(r"取消\s*提醒.{0,16}(?:怎麼|如何|方法|功能|指令)"),
    re.compile(
        r"(?:可以|能不能|能否|可不可以|是否可以)\s*取消\s*提醒\s*[嗎呢?？]"
    ),
    re.compile(r"請問.{0,20}取消\s*提醒.{0,12}(?:嗎|呢|怎麼|如何|[?？])"),
)
_INVOCATION_PATTERN = (
    r"(?:(?:[@＠]\s*)?(?:line\s*bot|咪寶)\s*[:：，,]?\s*)?"
)
_DEICTIC_PATTERN = r"(?:這|該)(?:一)?(?:個|則)"
_TARGET_PATTERN = rf"(?:(?:{_DEICTIC_PATTERN}\s*)?提醒|{_DEICTIC_PATTERN})"
_POLITE_MODAL_PREFIX_RE = re.compile(
    rf"^{_INVOCATION_PATTERN}(?:能不能|可不可以)\s*(?:請\s*)?"
    rf"(?:幫我|替我)(?:\s*(?:把|將)\s*{_TARGET_PATTERN})?\s*$",
    re.IGNORECASE,
)
_TEMPORAL_STAGE_PATTERN = (
    r"(?:今天|明天|後天|"
    r"(?:一|\d+)\s*(?:天|週|周|個月|小時|分鐘)\s*後|"
    r"(?:\*\*)?現在\s*/\s*即將到時(?:\*\*)?)"
)
# Only bot-rendered temporal labels are valid here. Accepting arbitrary
# parenthetical text can turn ``取消提醒（先不要）`` into a positive command.
_STAGE_PATTERN = (
    rf"(?:\s*[（(]\s*{_TEMPORAL_STAGE_PATTERN}\s*[）)])?"
)
_COMMAND_END_PATTERN = (
    r"(?:\s*(?:一下|吧))?(?:\s*(?:嗎|好嗎))?\s*[。.!！?？]?"
)
_POSITIVE_CANCEL_COMMANDS = (
    re.compile(
        rf"^{_INVOCATION_PATTERN}"
        r"(?:(?:我要|我想(?:要)?|我決定(?:要)?)\s*)?"
        r"(?:(?:請|麻煩)\s*)?(?:(?:幫我|替我|給我)\s*)?"
        rf"{_CANCEL_VERB_PATTERN}\s*(?:一下\s*)?"
        rf"{_TARGET_PATTERN}{_STAGE_PATTERN}{_COMMAND_END_PATTERN}$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^{_INVOCATION_PATTERN}(?:可以|能不能|可不可以)\s*(?:請\s*)?"
        rf"(?:幫我|替我)\s*{_CANCEL_VERB_PATTERN}\s*(?:一下\s*)?"
        rf"{_TARGET_PATTERN}{_STAGE_PATTERN}{_COMMAND_END_PATTERN}$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^{_INVOCATION_PATTERN}(?:可以|能不能|可不可以)\s*(?:請\s*)?"
        rf"(?:幫我|替我)\s*(?:把|將)\s*{_TARGET_PATTERN}\s*"
        rf"{_CANCEL_VERB_PATTERN}{_STAGE_PATTERN}{_COMMAND_END_PATTERN}$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^{_INVOCATION_PATTERN}(?:(?:請|麻煩)\s*)?"
        r"(?:(?:幫我|替我)\s*)?(?:把|將)?\s*"
        rf"{_TARGET_PATTERN}\s*(?:(?:幫我|給我)\s*)?"
        rf"{_CANCEL_VERB_PATTERN}{_STAGE_PATTERN}{_COMMAND_END_PATTERN}$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^{_INVOCATION_PATTERN}{_TARGET_PATTERN}\s*"
        rf"(?:不用了|不要了|不用再提醒(?:我)?(?:了)?)"
        rf"{_COMMAND_END_PATTERN}$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^{_INVOCATION_PATTERN}(?:不用|不要)\s*再\s*提醒(?:我)?(?:了)?"
        rf"{_COMMAND_END_PATTERN}$",
        re.IGNORECASE,
    ),
)
_INLINE_TARGET_CANCEL_RE = re.compile(
    rf"(?:^|(?<=[\s，,；;]))"
    rf"(?P<intent>{_DEICTIC_PATTERN}\s*(?:提醒\s*)?"
    rf"(?:{_CANCEL_VERB_PATTERN}|不用了|不要了))\s*[。.!！]?$"
)
_BARE_QUOTED_CANCEL_RE = re.compile(
    rf"^{_INVOCATION_PATTERN}(?:請\s*)?(?:幫我\s*)?"
    rf"{_CANCEL_VERB_PATTERN}\s*[。.!！]?$",
    re.IGNORECASE,
)
_BOT_EVENT_HEADER_RE = re.compile(
    r"^(?:🔔\s*)?(?:\*\*)?\s*"
    r"(?:(?:今天|明天|後天|"
    r"(?:一|\d+)\s*(?:天|週|周|個月|小時|分鐘)\s*後|"
    r"現在\s*/\s*即將到時)\s*)?"
    r"活動提醒\s*(?:\*\*)?$"
)
_EVENT_LOCATION_TERMINAL_INTENT_RE = re.compile(
    r"^📍\s*(?P<payload>[^。.!！\r\n]+)[。.!！]\s*"
    r"(?P<intent>[^。.!！\r\n]+(?:[。.!！?？])?)\s*$"
)
_UNSAFE_EVENT_LOCATION_COMMAND_CONTEXT_RE = re.compile(
    r"(?:不要|不用|先別|先别|別再|别再|別|别|不想|不必|"
    r"不需要|無須|无须|无需|請勿|请勿|禁止|切勿|勿|莫|休要|"
    r"不得|不可|不應|不应|不該|不该|沒有要|没有要|没要|沒要|不是要|"
    r"並非要|并非要|不准|不能|不可以|"
    r"如果|假如|假設|假设|要是|萬一|万一|若是|"
    r"只是|測試|测试|"
    r"取消|刪|删|移除|"
    r"怎麼|怎么|如何|是否|已經|已经|早就|剛剛|刚刚|剛才|刚才|"
    r"之前|原本)"
)
_CJK_GUARD_CHAR_RE = re.compile(
    r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF"
    r"\U00020000-\U0002FA1F\U00030000-\U000323AF]"
)


def _unsafe_event_location_command_context(value: str) -> bool:
    """Scan a Chinese-only skeleton so invisible separators cannot hide intent."""

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    cjk_skeleton = "".join(
        char
        for char in _CJK_GUARD_CHAR_RE.findall(normalized)
        if unicodedata.category(char) == "Lo"
    )
    return bool(
        _UNSAFE_EVENT_LOCATION_COMMAND_CONTEXT_RE.search(cjk_skeleton)
    )


def normalize_action(value: object) -> str:
    """Normalize only presentation differences; do not perform fuzzy matching."""

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.split()).casefold()


def _extracted_action_variants(value: object) -> tuple[str, ...]:
    """Return raw and possible deictic-suffix interpretations.

    Users often paste a rendered reminder and append ``這個提醒`` or
    ``這一則取消`` on the same line. Those words may identify the pasted target,
    but can also legitimately end an action. Keep both interpretations so the
    resolver can refuse mutation if they point at different reminders.
    """

    action = str(value or "").strip()
    stripped = _DEICTIC_TARGET_SUFFIX_RE.sub("", action).strip()
    if stripped and stripped != action:
        return stripped, action
    return (action,) if action else ()


def _reference_action_variants(
    value: object,
    source: ReferenceSource,
) -> tuple[str, ...]:
    """Keep bot-authored quoted actions exact.

    A deictic suffix can only be user-appended in the current pasted message.
    In quoted bot output, words such as ``這個`` are part of the stored action;
    stripping them could target a different reminder from the one quoted.
    """

    action = str(value or "").strip()
    if source is ReferenceSource.QUOTED:
        return (action,) if action else ()
    return _extracted_action_variants(action)


def _coerce_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(TAIPEI)
    if now.tzinfo is None:
        return now.replace(tzinfo=TAIPEI)
    return now.astimezone(TAIPEI)


def _make_epoch(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
) -> int | None:
    try:
        value = datetime(year, month, day, hour, minute, tzinfo=TAIPEI)
    except ValueError:
        return None
    return int(value.timestamp())


def _reference_from_match(
    match: re.Match[str],
    source: ReferenceSource,
) -> ReminderReference | None:
    remind_at = _make_epoch(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        int(match.group("hour")),
        int(match.group("minute")),
    )
    action_variants = _reference_action_variants(match.group("action"), source)
    action = action_variants[0] if action_variants else ""
    if remind_at is None or not normalize_action(action):
        return None
    return ReminderReference(
        action=action,
        remind_at=remind_at,
        source=source,
        action_variants=action_variants,
    )


def _todo_reference_from_match(
    match: re.Match[str],
    source: ReferenceSource,
    now: datetime,
) -> ReminderReference | None:
    month = int(match.group("todo_month"))
    day = int(match.group("todo_day"))
    hour = int(match.group("todo_hour"))
    minute = int(match.group("todo_minute"))
    action_text = str(match.group("todo_action") or "").strip()
    detail = str(match.group("todo_detail") or "").strip()
    title_variants = _reference_action_variants(action_text, source)
    action_variants = list(title_variants)
    if detail:
        combined = f"{action_text}（{detail}）"
        for variant in _reference_action_variants(combined, source):
            if variant not in action_variants:
                action_variants.append(variant)
    action_variants_tuple = tuple(action_variants)
    action = action_variants_tuple[0] if action_variants_tuple else ""
    year = now.year
    remind_at = _make_epoch(year, month, day, hour, minute)
    if remind_at is None or not normalize_action(action):
        return None
    candidate = datetime.fromtimestamp(remind_at, tz=TAIPEI)
    if candidate.date() < now.date():
        remind_at = _make_epoch(year + 1, month, day, hour, minute)
    if remind_at is None:
        return None
    return ReminderReference(
        action=action,
        remind_at=remind_at,
        source=source,
        action_variants=action_variants_tuple,
    )


def _event_reference_from_match(
    match: re.Match[str],
    source: ReferenceSource,
) -> ReminderReference | None:
    hour = int(match.group("hour") or 0)
    minute = int(match.group("minute") or 0)
    remind_at = _make_epoch(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        hour,
        minute,
    )
    action_variants = _reference_action_variants(match.group("action"), source)
    action = action_variants[0] if action_variants else ""
    location_hint = str(match.group("location") or "").strip()
    if source is ReferenceSource.CURRENT and location_hint:
        location_match = _EVENT_LOCATION_TERMINAL_INTENT_RE.fullmatch(
            f"📍 {location_hint}"
        )
        if location_match is not None:
            location_hint = location_match.group("payload").strip()
    if remind_at is None or not normalize_action(action):
        return None
    return ReminderReference(
        action=action,
        remind_at=remind_at,
        source=source,
        action_variants=action_variants,
        reference_kind="calendar_event",
        event_time_specified=match.group("hour") is not None,
        location_hint=location_hint,
    )


def _normalized_action_variants(reference: ReminderReference) -> set[str]:
    values = reference.action_variants or (reference.action,)
    return {normalize_action(value) for value in values if normalize_action(value)}


def _references_equivalent(
    left: ReminderReference,
    right: ReminderReference,
) -> bool:
    left_location = normalize_action(left.location_hint)
    right_location = normalize_action(right.location_hint)
    calendar_locations_match = not (
        left.reference_kind == "calendar_event"
        and right.reference_kind == "calendar_event"
    ) or left_location == right_location
    return (
        left.remind_at // 60 == right.remind_at // 60
        and bool(
            _normalized_action_variants(left)
            & _normalized_action_variants(right)
        )
        and calendar_locations_match
    )


def _unique_references(
    references: Iterable[ReminderReference],
) -> list[ReminderReference]:
    unique: dict[tuple[str, int, str, str], ReminderReference] = {}
    for reference in references:
        variants = reference.action_variants or (reference.action,)
        raw_action = variants[-1]
        location_key = (
            normalize_action(reference.location_hint)
            if reference.reference_kind == "calendar_event"
            else ""
        )
        key = (
            normalize_action(raw_action),
            reference.remind_at // 60,
            reference.reference_kind,
            location_key,
        )
        unique.setdefault(key, reference)
    return list(unique.values())


def _extract_references(
    text: str,
    source: ReferenceSource,
    now: datetime,
    *,
    allow_plain_datetime: bool,
) -> list[ReminderReference]:
    if not text.strip():
        return []

    references: list[ReminderReference] = []
    for match in _CREATION_RE.finditer(text):
        reference = _reference_from_match(match, source)
        if reference is not None:
            references.append(reference)
    for match in _TODO_RE.finditer(text):
        reference = _todo_reference_from_match(match, source, now)
        if reference is not None:
            references.append(reference)
    for match in _EVENT_RE.finditer(text):
        reference = _event_reference_from_match(match, source)
        if reference is not None:
            references.append(reference)

    if allow_plain_datetime or re.search(r"⏰\s*提醒", text):
        for match in _DATETIME_ACTION_RE.finditer(text):
            reference = _reference_from_match(match, source)
            if reference is not None:
                references.append(reference)
    return _unique_references(references)


def _intent_only_text(text: str) -> str:
    """Remove pasted reminder payload lines before classifying intent.

    An action itself can be a question (for example ``問房東入帳了嗎？``).
    That punctuation must not turn an otherwise explicit ``取消提醒`` command
    into a status question.
    """

    raw_lines = str(text or "").splitlines()
    nonempty_indices = [
        index for index, raw_line in enumerate(raw_lines) if raw_line.strip()
    ]
    last_nonempty_index = nonempty_indices[-1] if nonempty_indices else -1
    has_single_event_envelope = len(list(_EVENT_RE.finditer(text))) == 1

    kept: list[str] = []
    for index, raw_line in enumerate(raw_lines):
        line = raw_line.strip()
        if not line:
            continue
        if (
            has_single_event_envelope
            and index == last_nonempty_index
            and line.startswith("📍")
        ):
            terminal_match = _EVENT_LOCATION_TERMINAL_INTENT_RE.fullmatch(line)
            if (
                terminal_match is not None
                and terminal_match.group("payload").strip()
            ):
                if not _unsafe_event_location_command_context(
                    terminal_match.group("payload")
                ):
                    kept.append(terminal_match.group("intent").strip())
                    continue
        if (
            has_single_event_envelope
            and _BOT_EVENT_HEADER_RE.fullmatch(line) is not None
        ):
            continue
        if re.fullmatch(
            r"(?:(?:[@＠][^\s@＠{}]+|\{[A-Za-z0-9_]+\})"
            r"(?:\s+|$))+",
            line,
        ):
            # Rendered reminders may begin with recipient mentions such as
            # ``@爸爸 @媽媽`` or LINE textV2 placeholders. They are payload,
            # not part of the cancellation command.
            continue
        if re.fullmatch(
            rf"{_DEICTIC_PATTERN}(?:\s*提醒)?",
            line,
        ):
            continue
        if re.fullmatch(
            r"(?:⏰\s*)?提醒(?:\s*[（(][^()（）\r\n]{1,12}[）)])?",
            line,
        ):
            continue
        if re.match(
            r"^(?:⏰\s*提醒|已新增提醒|"
            r"(?:對象|參加人|細節|地點|預約編號|接送網址|"
            r"票券驗證碼|驗證碼)\s*[：:]|"
            r"(?:時間|时间|事項|事项)\s*[：:]|"
            r"\d{1,2}/\d{1,2}[（(][一二三四五六日天][）)]|"
            r"[📅🎯📍👥])",
            line,
        ):
            continue
        line = re.sub(
            r"(?<!\d)\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}\s+\S.*$",
            "",
            line,
        ).strip()
        if line:
            kept.append(line)
    return " ".join(kept)


def _is_non_command_cancel(text: str) -> bool:
    """Conservatively reject negated, hypothetical, queried, or past actions."""

    normalized = unicodedata.normalize("NFKC", text or "").strip()
    verb_match = _CANCEL_VERB_RE.search(normalized)
    if verb_match is not None:
        prefix = normalized[:verb_match.start()]
        if (
            _PRE_CANCEL_NEGATION_RE.search(prefix)
            and _POLITE_MODAL_PREFIX_RE.fullmatch(prefix) is None
        ):
            return True
    has_cancel_subject = bool(
        verb_match is not None
        and ("提醒" in normalized or _DEICTIC_TARGET_RE.search(normalized))
    )
    if not has_cancel_subject:
        return False
    polite_request = bool(
        re.search(
            r"(?:可以|能不能|可不可以|請|麻煩).{0,12}幫我.{0,12}"
            + _CANCEL_VERB_PATTERN,
            normalized,
        )
    )
    if (
        re.search(r"[?？]\s*$|(?:嗎|呢)\s*[。.!！]?$", normalized)
        and not polite_request
    ):
        return True
    if re.search(r"(?:如果|假如|假設|要是|萬一|若是)", normalized):
        return True
    if re.search(
        r"(?:還在考慮|考慮|可能|也許|或許|不確定|還沒決定|尚未決定|猶豫)",
        normalized,
    ):
        return True
    if re.search(r"(?:已經|早就|剛剛|剛才|之前|原本).{0,24}" + _CANCEL_VERB_PATTERN, normalized):
        return True
    if re.search(_CANCEL_VERB_PATTERN + r"(?:了|過)\s*[。.!！]?$", normalized):
        return True
    return False


def _has_explicit_cancel_intent(text: str, *, has_quote: bool) -> bool:
    original = unicodedata.normalize("NFKC", text or "").strip()
    normalized = _intent_only_text(original)
    if not normalized:
        return False
    if _is_non_command_cancel(normalized):
        return False
    if any(pattern.search(normalized) for pattern in _NEGATED_CANCEL_PATTERNS):
        return False
    if any(pattern.search(normalized) for pattern in _CREATION_PATTERNS):
        return False
    if any(pattern.search(normalized) for pattern in _HELP_PATTERNS):
        return False
    positive = any(
        pattern.fullmatch(normalized) for pattern in _POSITIVE_CANCEL_COMMANDS
    )
    if not positive:
        return False
    embedded_reference = bool(
        _FULL_DATETIME_HINT_RE.search(original)
        or _CREATION_RE.search(original)
        or _TODO_RE.search(original)
        or _EVENT_RE.search(original)
    )
    if (
        _DEICTIC_TARGET_RE.search(normalized)
        and "提醒" not in normalized
        and not has_quote
        and not embedded_reference
    ):
        return False
    return True


def parse_cancel_request(
    current_text: str,
    quoted_text: str | None = None,
    *,
    now: datetime | None = None,
    quoted_identity_bound: bool = False,
) -> CancelRequest:
    """Recognize cancellation intent and parse exact current/quoted references.

    ``quoted_text`` is intentionally tri-state: ``None`` means the LINE message
    had no quote; ``""`` means a quote existed but its text lookup failed.  This
    lets a deictic command such as ``這則取消`` remain handled (ambiguous) when
    the referenced message cannot be recovered. ``quoted_identity_bound`` means
    delivery metadata already identifies the reminder, so nonstandard visible
    text does not need to carry a parseable date/action reference.
    """

    text = str(current_text or "")
    has_quote = quoted_text is not None
    bare_quoted_cancel = bool(
        has_quote
        and _BARE_QUOTED_CANCEL_RE.fullmatch(
            unicodedata.normalize("NFKC", text).strip()
        )
    )
    if (
        not _has_explicit_cancel_intent(text, has_quote=has_quote)
        and not bare_quoted_cancel
    ):
        return CancelRequest(
            status=CancelParseStatus.NOT_CANCEL,
            reason="no_explicit_cancel_intent",
        )

    local_now = _coerce_now(now)
    current_references = _extract_references(
        text,
        ReferenceSource.CURRENT,
        local_now,
        allow_plain_datetime=True,
    )
    quoted_references = _extract_references(
        str(quoted_text or ""),
        ReferenceSource.QUOTED,
        local_now,
        allow_plain_datetime=False,
    )

    current_reference = (
        current_references[0] if len(current_references) == 1 else None
    )
    quoted_reference = (
        quoted_references[0] if len(quoted_references) == 1 else None
    )
    if len(current_references) > 1:
        return CancelRequest(
            status=CancelParseStatus.AMBIGUOUS,
            quoted_reference=quoted_reference,
            reason="multiple_current_references",
        )
    if len(quoted_references) > 1:
        return CancelRequest(
            status=CancelParseStatus.AMBIGUOUS,
            current_reference=current_reference,
            reason="multiple_quoted_references",
        )
    if bare_quoted_cancel and quoted_reference is None:
        if quoted_identity_bound:
            return CancelRequest(status=CancelParseStatus.READY)
        if quoted_text:
            return CancelRequest(
                status=CancelParseStatus.NOT_CANCEL,
                reason="quoted_message_is_not_a_reminder",
            )
        return CancelRequest(
            status=CancelParseStatus.AMBIGUOUS,
            reason="missing_reference",
        )
    if (
        quoted_identity_bound
        and current_reference is not None
        and quoted_reference is None
    ):
        # A durable quote binding identifies one reminder, while the current
        # message explicitly names another target that cannot be compared with
        # the nonstandard quoted text. Never let the hidden binding silently
        # override that explicit target.
        return CancelRequest(
            status=CancelParseStatus.AMBIGUOUS,
            current_reference=current_reference,
            reason="bound_identity_with_unverified_current_reference",
        )
    if (
        current_reference is not None
        and quoted_reference is not None
        and not _references_equivalent(current_reference, quoted_reference)
    ):
        return CancelRequest(
            status=CancelParseStatus.AMBIGUOUS,
            current_reference=current_reference,
            quoted_reference=quoted_reference,
            reason="conflicting_references",
        )
    if current_reference is None and quoted_reference is None:
        if quoted_identity_bound:
            return CancelRequest(status=CancelParseStatus.READY)
        return CancelRequest(
            status=CancelParseStatus.AMBIGUOUS,
            reason="missing_reference",
        )
    return CancelRequest(
        status=CancelParseStatus.READY,
        current_reference=current_reference,
        quoted_reference=quoted_reference,
    )


def _candidate_epoch_seconds(value: object) -> int | None:
    if isinstance(value, datetime):
        dt = value.replace(tzinfo=TAIPEI) if value.tzinfo is None else value
        return int(dt.timestamp())
    if isinstance(value, (int, float)):
        return int(value)
    raw = str(value or "").strip()
    if raw.isdigit():
        return int(raw)
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TAIPEI)
    return int(parsed.timestamp())


def resolve_cancel_request(
    request: CancelRequest,
    candidates: Iterable[Mapping[str, object]],
) -> CancelResolution:
    """Select one exact candidate, never guessing on zero or duplicate matches."""

    if request.status is CancelParseStatus.NOT_CANCEL:
        return CancelResolution(
            status=CancelResolutionStatus.NOT_CANCEL,
            reason=request.reason,
        )
    if request.status is CancelParseStatus.AMBIGUOUS:
        return CancelResolution(
            status=CancelResolutionStatus.AMBIGUOUS,
            reason=request.reason,
        )

    reference = request.reference
    if reference is None:
        return CancelResolution(
            status=CancelResolutionStatus.AMBIGUOUS,
            reason="missing_reference",
        )

    reference_actions = _normalized_action_variants(reference)
    reference_minute = reference.remind_at // 60
    matches: list[tuple[int, str, int]] = []
    for candidate in candidates:
        try:
            reminder_id = int(candidate["reminder_id"])
        except (KeyError, TypeError, ValueError):
            continue
        action = str(candidate.get("action") or "")
        remind_at = _candidate_epoch_seconds(candidate.get("remind_at"))
        if remind_at is None:
            continue
        if (
            normalize_action(action) in reference_actions
            and remind_at // 60 == reference_minute
        ):
            matches.append((reminder_id, action, remind_at))

    if not matches:
        return CancelResolution(
            status=CancelResolutionStatus.NOT_FOUND,
            reference=reference,
            reason="no_exact_match",
        )
    if len(matches) > 1:
        return CancelResolution(
            status=CancelResolutionStatus.AMBIGUOUS,
            reference=reference,
            reason="multiple_exact_matches",
        )

    reminder_id, action, remind_at = matches[0]
    return CancelResolution(
        status=CancelResolutionStatus.MATCHED,
        reminder_id=reminder_id,
        action=action,
        remind_at=remind_at,
        reference=reference,
    )


def resolve_cancel_request_by_source(
    request: CancelRequest,
    candidates: Iterable[Mapping[str, object]],
    *,
    source_kind: str,
    source_ref: str,
) -> CancelResolution:
    """Resolve one exact durable source link for a rendered event reminder."""

    if request.status is CancelParseStatus.NOT_CANCEL:
        return CancelResolution(
            status=CancelResolutionStatus.NOT_CANCEL,
            reason=request.reason,
        )
    if request.status is CancelParseStatus.AMBIGUOUS:
        return CancelResolution(
            status=CancelResolutionStatus.AMBIGUOUS,
            reason=request.reason,
        )
    reference = request.reference
    if not source_kind or not source_ref:
        return CancelResolution(
            status=CancelResolutionStatus.NOT_FOUND,
            reference=reference,
            reason="missing_source_reference",
        )

    matches: list[tuple[int, str, int]] = []
    for candidate in candidates:
        if (
            str(candidate.get("source_kind") or "") != source_kind
            or str(candidate.get("source_ref") or "") != source_ref
        ):
            continue
        try:
            reminder_id = int(candidate["reminder_id"])
        except (KeyError, TypeError, ValueError):
            continue
        action = str(candidate.get("action") or "")
        remind_at = _candidate_epoch_seconds(candidate.get("remind_at"))
        if remind_at is None:
            continue
        matches.append((reminder_id, action, remind_at))

    if not matches:
        return CancelResolution(
            status=CancelResolutionStatus.NOT_FOUND,
            reference=reference,
            reason="no_source_match",
        )
    if len(matches) > 1:
        return CancelResolution(
            status=CancelResolutionStatus.AMBIGUOUS,
            reference=reference,
            reason="multiple_source_matches",
        )

    reminder_id, action, remind_at = matches[0]
    return CancelResolution(
        status=CancelResolutionStatus.MATCHED,
        reminder_id=reminder_id,
        action=action,
        remind_at=remind_at,
        reference=reference,
    )
