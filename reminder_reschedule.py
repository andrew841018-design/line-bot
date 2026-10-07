"""Pure parsing for quoted reminder reschedules (2026-10-03 proposal).

A family member quotes one bot reminder push or receipt and replies with only a
new date and/or clock time, optionally one place.  This module performs no
database writes: ``main`` resolves the quoted identity and ``memory`` performs
the compare-and-set update.  Every ambiguity becomes a reason code so the
caller answers 「尚未更新提醒」 instead of guessing.

Three outcomes for the current text:

- ``not_reschedule``: not ours; later routes (cancel, chat, creation) decide.
- ``invalid``: clearly an attempt to move the reminder that cannot be applied
  safely; the caller refuses and changes nothing.
- ``candidate``: parsed pieces that ``resolve_new_schedule`` turns into one
  timestamp once the quoted reminder's current time is known.
"""

from __future__ import annotations

import re
import unicodedata
import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")

NOT_RESCHEDULE = "not_reschedule"
INVALID = "invalid"
CANDIDATE = "candidate"

QUOTED_NONE = "none"
QUOTED_ONE = "one"
QUOTED_MULTIPLE = "multiple"

_MAX_TEXT_LENGTH = 120


# ── Quoted bot message ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class QuotedReminder:
    """One reminder shown in a bot push or receipt, or why there is not one."""

    status: str
    action: str = ""
    remind_at: int | None = None


# Receipts written by reminder creation, restatement and this feature.  The
# 已更新提醒 prefix covers both 「已更新提醒」 and 「已更新提醒（A → B）」.
_RECEIPT_HEADERS = (
    "已新增提醒",
    "提醒已存在，未重複新增",
    "已更新既有提醒，未重複新增",
    "已更新提醒",
    "提醒本來就是這個時間",
    "這則更正先前已處理",
)
_MULTI_RECEIPT_RE = re.compile(r"已新增\s*\d+\s*筆提醒|\d+\s*筆提醒皆已存在")
_PUSH_HEADER_RE = re.compile(r"^⏰\s*提醒")
_TIME_LINE_RE = re.compile(
    r"^時間\s*[:：]\s*(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})"
    r"\s*(?:[（(][^）)\n]*[）)])?\s*$"
)
_ACTION_LINE_RE = re.compile(r"^事項\s*[:：]\s*(\S.*?)\s*$")
_PUSH_LINE_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})\s+(\S.*?)\s*$"
)
_PEOPLE_LINE_RE = re.compile(r"^(?:對象|參加人)\s*[:：]")
# fixR5a: pushes and receipts may show a fuller absorbed wording on this line
# (reminder_push.fuller_detail_line); it never names another reminder.
_DETAIL_LINE_RE = re.compile(r"^(?:細節|细节)\s*[:：]")
_PLACEHOLDER_RE = re.compile(r"\{p\d{1,3}\}")
_MAX_QUOTED_LENGTH = 2000


def _is_mention_line(line: str) -> bool:
    """A line made only of @mentions or text_v2 placeholders ({p1}).

    Checked token by token; a single regex with nested repeats here
    backtracks exponentially on a long run of @.
    """
    tokens = line.split()
    return bool(tokens) and all(
        (token[0] in "@＠" and len(token.lstrip("@＠")) > 0)
        or _PLACEHOLDER_RE.fullmatch(token) is not None
        for token in tokens
    )


def _content_lines(text: str) -> list[str]:
    """Drop blank lines and the leading mention line of a bot message."""

    lines: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if not lines and line.startswith(("@", "＠", "{p")) and "提醒" not in line:
            continue
        lines.append(line)
    return lines


def _epoch(year: str, month: str, day: str, hour: str, minute: str) -> int | None:
    try:
        value = datetime(
            int(year), int(month), int(day), int(hour), int(minute), tzinfo=TAIPEI
        )
    except ValueError:
        return None
    return int(value.timestamp())


def parse_quoted_reminder(text: str) -> QuotedReminder:
    """Read the single reminder a bot push or receipt shows.

    Only whitelisted bot layouts count, so an unrelated bot answer that happens
    to contain 「時間：」 and 「事項：」 lines never identifies a reminder.
    """

    if len(str(text or "")) > _MAX_QUOTED_LENGTH:
        return QuotedReminder(QUOTED_NONE)
    lines = _content_lines(text)
    if not lines:
        return QuotedReminder(QUOTED_NONE)
    head = lines[0]
    if _MULTI_RECEIPT_RE.search(head):
        return QuotedReminder(QUOTED_MULTIPLE)
    found: set[tuple[str, int]] = set()
    body = lines[1:]
    if head.startswith(_RECEIPT_HEADERS):
        # Every line must belong to the receipt layout; an LLM chat reply that
        # merely starts like a receipt is also archived as __bot__.
        index = 0
        while index < len(body):
            line = body[index]
            time_match = _TIME_LINE_RE.match(line)
            if time_match is not None and index + 1 < len(body):
                action_match = _ACTION_LINE_RE.match(body[index + 1])
                remind_at = _epoch(*time_match.groups())
                if action_match is None or remind_at is None:
                    return QuotedReminder(QUOTED_NONE)
                found.add((action_match.group(1), remind_at))
                index += 2
                continue
            if (
                _PEOPLE_LINE_RE.match(line)
                or _DETAIL_LINE_RE.match(line)
                or _is_mention_line(line)
            ):
                index += 1
                continue
            return QuotedReminder(QUOTED_NONE)
    elif _PUSH_HEADER_RE.match(head):
        for line in body:
            push_match = _PUSH_LINE_RE.match(line)
            if push_match is not None:
                remind_at = _epoch(*push_match.groups()[:5])
                if remind_at is None:
                    return QuotedReminder(QUOTED_NONE)
                found.add((push_match.group(6), remind_at))
            elif not (
                _PEOPLE_LINE_RE.match(line)
                or _DETAIL_LINE_RE.match(line)
                or _is_mention_line(line)
            ):
                return QuotedReminder(QUOTED_NONE)
    else:
        return QuotedReminder(QUOTED_NONE)
    if not found:
        return QuotedReminder(QUOTED_NONE)
    if len(found) > 1:
        return QuotedReminder(QUOTED_MULTIPLE)
    action, remind_at = found.pop()
    return QuotedReminder(QUOTED_ONE, action=action, remind_at=remind_at)


# ── Current message ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Clock:
    hour: int
    minute: int


@dataclass(frozen=True)
class DateSpec:
    kind: str  # absolute | month_day | relative | weekday
    year: int | None = None
    month: int | None = None
    day: int | None = None
    offset_days: int | None = None
    weekday: int | None = None
    week_prefix: str = ""


@dataclass(frozen=True)
class RescheduleRequest:
    status: str
    reason: str = ""
    date: DateSpec | None = None
    weekday_check: int | None = None
    clock: Clock | None = None
    location: str | None = None


_DAYPARTS = ("凌晨", "半夜", "清晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "晚間")
_DAYPART = "|".join(_DAYPARTS)
_ZH_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_NUM = r"(?:\d{1,2}|[零〇一二兩三四五六七八九十]{1,4})"
_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_INVOCATION_RE = re.compile(r"^(?:@\s*)?(?:line\s*bot|咪寶)\s*[:,]?\s*", re.IGNORECASE)
_QUESTION_RE = re.compile(
    r"[?]|嗎|呢|要不要|可不可以|能不能|是不是|對不對|好不好|會不會|有沒有|行不行"
)
_NEGATION_RE = re.compile(r"不|沒|別|勿|免")
_INTENT_RE = re.compile(r"提醒(?:我|一下|大家|你|妳|他|她)|記得|叫我|通知")
# Only a message that is nothing but a shift request counts (「延後一天」,
# 「提醒提前兩小時吧」); chat such as 「我會晚10分鐘到」 is not ours.
_OFFSET_ONLY_RE = re.compile(
    r"^(?:(?:請|幫我|麻煩|把|將|這則|這個|這筆|提醒的?|時間)\s*)*(?:再)?"
    r"(?:延後|延期|延|往後|順延|提前|提早|晚|早)\s*"
    r"(?:\d+|[一二兩三四五六七八九十半]+)\s*(?:個)?\s*"
    r"(?:天|日|小時|鐘頭|分鐘|分|週|周|禮拜|星期)"
    r"[\s,.!~。、]*(?:吧|好了|喔|囉|啦|了)?[\s,.!~。、]*$"
)
# 「改成明天早上9點提醒我」: after a change verb a trailing reminder phrase
# is filler, not a new reminder request.
_TRAILING_REMIND_RE = re.compile(
    r"(?:再)?(?:提醒|通知)(?:我們|我|大家|你們|一下)+[\s,.!~。、]*(?:喔|囉|啦|吧)?[\s,.!~。、]*$"
)

_DATE_FULL_RE = re.compile(
    r"(?P<y>\d{4})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})\s*[日號]?"
)
_DATE_MD_RE = re.compile(rf"(?P<m>{_NUM})\s*月\s*(?P<d>{_NUM})\s*[日號]?")
_DATE_SLASH_RE = re.compile(r"(?P<m>\d{1,2})\s*/\s*(?P<d>\d{1,2})(?![\d/])")
_RELATIVE_DAYS = (
    ("大後天", 3, None),
    ("後天", 2, None),
    ("明天", 1, None),
    ("明日", 1, None),
    ("今天", 0, None),
    ("明早", 1, "早上"),
    ("今早", 0, "早上"),
    ("明晚", 1, "晚上"),
    ("今晚", 0, "晚上"),
)
_WEEKDAY_RE = re.compile(
    r"(?P<pre>下下|下|這|本)?\s*(?:個)?\s*(?:星期|週|周|禮拜)(?P<wd>[一二三四五六日天])"
)
_WEEKDAY_PAREN_RE = re.compile(
    r"\(\s*(?:星期|週|周)?(?P<wd>[一二三四五六日天])\s*\)"
)
_CLOCK_COMPACT_DP_RE = re.compile(rf"(?P<dp>{_DAYPART})\s*(?P<c>\d{{3,4}})(?!\d)")
_CLOCK_COLON_RE = re.compile(
    rf"(?:(?P<dp>{_DAYPART})\s*)?(?P<h>\d{{1,2}})\s*:\s*(?P<mi>\d{{2}})(?!\d)"
    r"(?:\s*(?P<ap>[ap]\.?m\.?)(?![a-z]))?",
    re.IGNORECASE,
)
_CLOCK_AMPM_RE = re.compile(r"(?P<h>\d{1,2})\s*(?P<ap>[ap]\.?m\.?)(?![a-z])", re.IGNORECASE)
_CLOCK_ZH_RE = re.compile(
    rf"(?:(?P<dp>{_DAYPART})\s*)?(?P<h>{_NUM})\s*(?:點|時)(?!間)(?:鐘)?"
    # Minutes follow 點 directly (「9點10」) or carry 分 (「9點 10分」); a
    # number that starts a date (「早上9點 10/8」) is never taken as minutes.
    rf"(?:\s*(?P<half>半)|(?P<mi>{_NUM})(?![\d零〇一二兩三四五六七八九十]|\s*[/月.])\s*分?"
    rf"|\s+(?P<mi2>{_NUM})\s*分)?"
)
_CLOCK_COMPACT_RE = re.compile(r"(?P<c>\d{4})(?!\d)")
_DAYPART_RE = re.compile(_DAYPART)
_RANGE_SEPARATOR_RE = re.compile(r"\s*(?:-|~|到|至)\s*")
_LABEL_RE = re.compile(r"(?:提醒)?(?P<field>時間|日期)")
_CHANGE_WORDS = (
    "更改為", "更改成", "更改到", "更正為", "更正成", "修正為", "修正成",
    "變更為", "變更成", "調整為", "調整成", "調整到", "延後到", "延期到",
    "提前到", "提早到", "改期到", "改期為", "改成", "改為", "改到", "改在",
    "改至", "換成", "換到", "延到", "挪到", "移到", "調到", "設定成", "設定為",
    "設定到", "設成", "設為", "應該是", "正確是",
    "其實是", "才是", "改期", "更改", "更正", "修正", "改",
)
_OBJECT_WORDS = (
    "這一則", "這則", "這個", "這筆", "那則", "那個", "提醒的", "提醒", "把", "將", "的",
)
_POLITE_WORDS = (
    "謝謝", "感謝", "麻煩了", "麻煩", "拜託", "請", "幫我", "幫忙",
    "喔", "哦", "噢", "囉", "啰", "唷", "呦", "啦", "了", "吧", "嗯", "耶", "呀",
)
_OPENING_WORDS_RE = re.compile(r"(?:好的|好|收到|ok)(?=[\s,、。.!~])", re.IGNORECASE)
_FILLER_RE = re.compile(r"[\s,、。.!~;:]+")

_LOCATION_LEADS = ("地點:", "地點", "地址:", "地址", "可以到", "可到", "改在", "改到", "在", "到")
_PLACE_CHAR_RE = re.compile(r"[一-鿿A-Za-z0-9\-之]")
_PLACE_END_RE = re.compile(
    r"(?:路|街|巷|弄|號|樓|段|區|市|縣|鄉|鎮|村|里|場|棟|室|醫院|診所|衛生所|"
    r"中心|車站|站|館|店|公司|學校|大學|公園|機場|飯店|酒店|餐廳|市場|銀行|郵局|"
    r"大樓|廣場|寺|廟|教會|教堂|藥局|門口|B\d+|\d+[Ff])$"
)
_PLACE_CHAIN_RE = re.compile(
    r"(?:再|順便|然後|之後|和|跟|與|及|或|、)\s*"
    r"(?:去|拿|買|繳|領|看|吃|做|帶|接|送|提醒|找|辦)"
)
_PLACE_HINT_RE = re.compile(_PLACE_END_RE.pattern.rstrip("$") + r"|家")
_LOCATION_LEAD_RE = re.compile(r"(?:地點|地址|可以到|可到|在|到)")
_RESIDUE_RE = re.compile(r"^[\d\s:.,~\-/apmAPM]+$")


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).strip()


def _zh_int(raw: str | None) -> int | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    stripped = value.lstrip("零〇")
    if not stripped:
        return 0 if value else None
    if "十" in stripped:
        tens_raw, _, ones_raw = stripped.partition("十")
        tens = 1 if not tens_raw else _ZH_DIGITS.get(tens_raw)
        ones = 0 if not ones_raw else _ZH_DIGITS.get(ones_raw)
        if tens is None or ones is None or len(tens_raw) > 1 or len(ones_raw) > 1:
            return None
        return tens * 10 + ones
    if len(stripped) == 1:
        return _ZH_DIGITS.get(stripped)
    return None


@dataclass(frozen=True)
class _RawClock:
    hour: int
    minute: int
    daypart: str
    final: bool  # hour already on the 24-hour clock (am/pm, padded, >= 12)
    ampm: bool = False  # written with am/pm; a daypart word contradicts it


def _apply_daypart(raw: _RawClock) -> tuple[str, int | None]:
    """Return ("ok", hour) or (reason, None) for one written clock."""

    hour, daypart = raw.hour, raw.daypart
    if not 0 <= raw.minute <= 59 or not 0 <= hour <= 23:
        return "bad_time", None
    if not daypart:
        if raw.final or hour == 0 or hour >= 12:
            return "ok", hour
        return "ambiguous_hour", None
    rules: dict[str, tuple[tuple[range, int], ...]] = {
        "凌晨": ((range(0, 6), 0),),
        "半夜": ((range(0, 6), 0),),
        "清晨": ((range(4, 8), 0),),
        "早上": ((range(4, 12), 0),),
        "上午": ((range(4, 12), 0),),
        "中午": ((range(11, 15), 0), (range(1, 3), 12)),
        "下午": ((range(1, 8), 12), (range(12, 20), 0)),
        "傍晚": ((range(4, 8), 12), (range(16, 20), 0)),
        "晚上": ((range(6, 12), 12), (range(18, 24), 0)),
        "晚間": ((range(6, 12), 12), (range(18, 24), 0)),
    }
    if daypart in {"凌晨", "半夜"} and hour == 12:
        return "ok", 0
    for allowed, shift in rules.get(daypart, ()):
        if hour in allowed:
            return "ok", hour + shift
    return "bad_time", None


def _compact_clock(digits: str, daypart: str) -> _RawClock | None:
    if len(digits) == 3:
        hour, minute = int(digits[0]), int(digits[1:])
    elif len(digits) == 4:
        hour, minute = int(digits[:2]), int(digits[2:])
    else:
        return None
    return _RawClock(hour, minute, daypart, final=len(digits) == 4)


def _ampm_hour(hour: int, marker: str) -> int | None:
    if not 1 <= hour <= 12:
        return None
    is_pm = marker.lower().startswith("p")
    if hour == 12:
        return 12 if is_pm else 0
    return hour + 12 if is_pm else hour


def _match_clock(
    s: str, pos: int, previous: str, *, bare_chinese: bool = False
) -> tuple[_RawClock | None, int] | None:
    """Match one clock at ``pos``; a None clock means it was malformed.

    A bare Chinese-numeral hour (「一點」「兩點」「一時」, no daypart, minutes,
    半 or 鐘) is an idiom far more often than a time, so it only counts when
    ``bare_chinese`` says a change verb or label came first (「改成九點」).
    """

    match = _CLOCK_COMPACT_DP_RE.match(s, pos)
    if match:
        return _compact_clock(match.group("c"), match.group("dp")), match.end()
    match = _CLOCK_COLON_RE.match(s, pos)
    if match:
        hour, minute = int(match.group("h")), int(match.group("mi"))
        if match.group("ap"):
            if match.group("dp"):
                return None, match.end()
            converted = _ampm_hour(hour, match.group("ap"))
            if converted is None:
                return None, match.end()
            return _RawClock(converted, minute, "", final=True, ampm=True), match.end()
        padded = len(match.group("h")) == 2
        return _RawClock(hour, minute, match.group("dp") or "", final=padded), match.end()
    match = _CLOCK_AMPM_RE.match(s, pos)
    if match:
        converted = _ampm_hour(int(match.group("h")), match.group("ap"))
        if converted is None:
            return None, match.end()
        return _RawClock(converted, 0, "", final=True, ampm=True), match.end()
    match = _CLOCK_ZH_RE.match(s, pos)
    if match and not (
        bare_chinese
        or match.group("dp")
        or match.group("h").isdigit()
        or match.group("half")
        or match.group("mi")
        or match.group("mi2")
        or "鐘" in match.group(0)
    ):
        match = None
    if match:
        hour = _zh_int(match.group("h"))
        if match.group("half"):
            minute: int | None = 30
        elif match.group("mi") or match.group("mi2"):
            minute = _zh_int(match.group("mi") or match.group("mi2"))
        else:
            minute = 0
        if hour is None or minute is None:
            return None, match.end()
        padded = match.group("h").isdigit() and len(match.group("h")) == 2
        return _RawClock(hour, minute, match.group("dp") or "", final=padded), match.end()
    if previous in {"label_time", "date"}:
        match = _CLOCK_COMPACT_RE.match(s, pos)
        if match:
            return _compact_clock(match.group("c"), ""), match.end()
    return None


def _match_date(s: str, pos: int) -> tuple[DateSpec, str, int] | None:
    match = _DATE_FULL_RE.match(s, pos)
    if match:
        spec = DateSpec(
            "absolute",
            year=int(match.group("y")),
            month=int(match.group("m")),
            day=int(match.group("d")),
        )
        return spec, "", match.end()
    for pattern in (_DATE_MD_RE, _DATE_SLASH_RE):
        match = pattern.match(s, pos)
        if match:
            month, day = _zh_int(match.group("m")), _zh_int(match.group("d"))
            if month is None or day is None:
                return None
            return DateSpec("month_day", month=month, day=day), "", match.end()
    for word, offset, daypart in _RELATIVE_DAYS:
        if s.startswith(word, pos):
            return DateSpec("relative", offset_days=offset), daypart or "", pos + len(word)
    return None


def _place_end(s: str, start: int) -> int:
    """End of a place fragment: stop at spaces, punctuation or a date/clock."""

    end = start
    while end < len(s) and _PLACE_CHAR_RE.match(s, end):
        if end > start and (
            _match_date(s, end) is not None
            or _match_clock(s, end, "") is not None
            or _DAYPART_RE.match(s, end)
        ):
            break
        end += 1
    return end


def _valid_place(place: str) -> bool:
    """One place fragment, kept verbatim in the action.

    「銀行和郵局」 stays as written: without a gazetteer 「台北市和平東路」 cannot
    be told apart from two places, and the user's own words lose nothing.
    Chained activities (「到銀行順便去郵局」) are refused.
    """
    return (
        2 <= len(place) <= 40
        and bool(_PLACE_END_RE.search(place))
        and not _PLACE_CHAIN_RE.search(place)
        and "提醒" not in place
    )


def _match_location(s: str, pos: int) -> tuple[str, int] | None:
    for lead in _LOCATION_LEADS:
        if not s.startswith(lead, pos):
            continue
        start = pos + len(lead)
        while start < len(s) and s[start] in " :":
            start += 1
        if start >= len(s):
            return None
        if _match_date(s, start) is not None or _match_clock(s, start, "") is not None:
            return None  # 改到10/8: a verb, not a place
        end = _place_end(s, start)
        place = s[start:end]
        if _valid_place(place):
            return place, end
        return None
    return None


def _match_word(s: str, pos: int, words: tuple[str, ...]) -> int | None:
    for word in words:
        if s.startswith(word, pos):
            return pos + len(word)
    return None


def mentions_change_word(text: str) -> bool:
    """True when the text names a reschedule verb (used for honest refusals)."""

    s = _normalize(text)
    return any(word in s for word in _CHANGE_WORDS if len(word) > 1)


def has_schedule_hint(text: str) -> bool:
    """True when the text holds a real date, clock or weekday token
    (「我換成搭8號公車」 has a digit but none of these)."""

    s = _normalize(text)[: _MAX_TEXT_LENGTH * 2]
    return any(
        _match_date(s, pos) is not None
        or _match_clock(s, pos, "") is not None
        or _WEEKDAY_RE.match(s, pos) is not None
        for pos in range(len(s))
    )


def is_question(text: str) -> bool:
    return bool(_QUESTION_RE.search(_normalize(text)))


def is_negated(text: str) -> bool:
    return bool(_NEGATION_RE.search(_normalize(text)))


def classify_reschedule_text(text: str) -> RescheduleRequest:
    """Decide whether ``text`` only names a new date/time (and maybe a place)."""

    raw = str(text or "")
    if len(raw) > _MAX_TEXT_LENGTH * 2:
        return RescheduleRequest(NOT_RESCHEDULE)
    s = _INVOCATION_RE.sub("", _normalize(raw), count=1)
    # Cap after NFKC and the bot mention, before any other regex runs.
    if not s or len(s) > _MAX_TEXT_LENGTH:
        return RescheduleRequest(NOT_RESCHEDULE)
    if _QUESTION_RE.search(s) or _NEGATION_RE.search(s):
        return RescheduleRequest(NOT_RESCHEDULE)
    if any(word in s for word in _CHANGE_WORDS if len(word) > 1):
        s = _TRAILING_REMIND_RE.sub("", s).strip()
    if _INTENT_RE.search(s):
        return RescheduleRequest(NOT_RESCHEDULE)
    if _OFFSET_ONLY_RE.match(s):
        return RescheduleRequest(INVALID, reason="offset_unsupported")

    dates: list[tuple[DateSpec, str]] = []
    weekdays: list[tuple[int, str]] = []
    clocks: list[_RawClock | None] = []
    dayparts: list[str] = []
    places: list[str] = []
    has_range = False
    has_change = False
    previous = ""
    pos = 0
    opening = _OPENING_WORDS_RE.match(s)
    if opening:
        pos = opening.end()
    leftover = ""
    while pos < len(s):
        filler = _FILLER_RE.match(s, pos)
        if filler:
            pos = filler.end()
            continue
        # Order matters: a place is tried before verbs (「改到中山路」 is a
        # place, 「改到10/8」 a verb) and dates before clocks.
        location = _match_location(s, pos)
        if location is not None:
            places.append(location[0])
            pos = location[1]
            previous = "location"
            continue
        date_match = _match_date(s, pos)
        if date_match is not None:
            spec, daypart, pos = date_match
            dates.append((spec, daypart))
            previous = "date"
            continue
        weekday_match = _WEEKDAY_RE.match(s, pos) or _WEEKDAY_PAREN_RE.match(s, pos)
        if weekday_match is not None:
            prefix = weekday_match.groupdict().get("pre") or ""
            weekdays.append((_WEEKDAY_INDEX[weekday_match.group("wd")], prefix))
            pos = weekday_match.end()
            previous = "weekday"
            continue
        clock_match = _match_clock(s, pos, previous, bare_chinese=has_change)
        if clock_match is not None:
            clock, end = clock_match
            separator = _RANGE_SEPARATOR_RE.match(s, end)
            range_end = (
                _match_clock(s, separator.end(), "") if separator else None
            )
            if range_end is not None:
                has_range = True
                pos = range_end[1]
            else:
                clocks.append(clock)
                pos = end
            previous = "clock"
            continue
        daypart_match = _DAYPART_RE.match(s, pos)
        if daypart_match is not None:
            dayparts.append(daypart_match.group(0))
            pos = daypart_match.end()
            previous = "daypart"
            continue
        label = _LABEL_RE.match(s, pos)
        if label is not None:
            has_change = True
            previous = "label_time" if label.group("field") == "時間" else "label_date"
            pos = label.end()
            continue
        end = _match_word(s, pos, _CHANGE_WORDS)
        if end is not None:
            has_change = True
            previous = "verb"
            pos = end
            continue
        end = _match_word(s, pos, _OBJECT_WORDS) or _match_word(s, pos, _POLITE_WORDS)
        if end is not None:
            pos = end
            continue
        leftover = s[pos:]
        break

    has_schedule = bool(dates or weekdays or clocks or has_range)
    if leftover:
        if not (has_schedule or has_change):
            return RescheduleRequest(NOT_RESCHEDULE)
        if has_schedule and (
            re.match(r"(?:地點|地址)\s*:", leftover)
            or (
                (places or _LOCATION_LEAD_RE.match(leftover))
                and _PLACE_HINT_RE.search(leftover)
            )
        ):
            # 「10/8 地點：台北101」「10/8 早上9點在家」, or a second place.
            return RescheduleRequest(INVALID, reason="unsupported_location")
        if _RESIDUE_RE.match(leftover):
            return RescheduleRequest(INVALID, reason="unparsed")
        return RescheduleRequest(NOT_RESCHEDULE)
    if has_range:
        return RescheduleRequest(INVALID, reason="range")
    if not has_schedule:
        if dayparts and has_change:
            return RescheduleRequest(INVALID, reason="daypart_only")
        return RescheduleRequest(NOT_RESCHEDULE)
    if len(places) > 1:
        return RescheduleRequest(INVALID, reason="unsupported_location")
    if len(dates) > 1 or len(weekdays) > 1:
        return RescheduleRequest(INVALID, reason="multiple_dates")
    if len(clocks) > 1:
        return RescheduleRequest(INVALID, reason="multiple_times")
    if len(dayparts) > 1:
        return RescheduleRequest(INVALID, reason="bad_time")

    date_spec: DateSpec | None = None
    date_daypart = ""
    weekday_check: int | None = None
    if dates:
        date_spec, date_daypart = dates[0]
        if weekdays:
            weekday_check = weekdays[0][0]
    elif weekdays:
        weekday, prefix = weekdays[0]
        date_spec = DateSpec("weekday", weekday=weekday, week_prefix=prefix)

    standalone = dayparts[0] if dayparts else ""
    hints = [part for part in (date_daypart, standalone) if part]
    clock: Clock | None = None
    if clocks:
        raw = clocks[0]
        if raw is None:
            return RescheduleRequest(INVALID, reason="bad_time")
        daypart = raw.daypart
        if raw.ampm and hints:
            # 「晚上8am」「今晚8:30 am」 contradict themselves.
            return RescheduleRequest(INVALID, reason="bad_time")
        for hint in hints:
            if daypart and daypart != hint:
                return RescheduleRequest(INVALID, reason="bad_time")
            daypart = daypart or hint
        raw = dataclasses.replace(raw, daypart=daypart)
        status, hour = _apply_daypart(raw)
        if status != "ok" or hour is None:
            return RescheduleRequest(INVALID, reason=status)
        if daypart == "半夜" or (daypart == "凌晨" and raw.hour == 12):
            # 「10/8半夜12點」 usually means the night after 10/8.
            return RescheduleRequest(INVALID, reason="ambiguous_midnight")
        clock = Clock(hour, raw.minute)
    elif hints:
        return RescheduleRequest(INVALID, reason="daypart_only")

    return RescheduleRequest(
        CANDIDATE,
        date=date_spec,
        weekday_check=weekday_check,
        clock=clock,
        location=places[0] if places else None,
    )


# ── Resolution against the quoted reminder ─────────────────────────────────


def _nearest_future_month_day(
    month: int, day: int, current: date, clock: Clock, now: datetime
) -> tuple[str, date | None]:
    """Same nearest-date rule as calendar_db._resolve_corrected_date, but a
    past candidate is dropped first (2026-10-08 + 「4/10」 → 2027-04-10)."""

    valid: list[date] = []
    for year in range(current.year - 1, current.year + 3):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        valid.append(candidate)
    if not valid:
        return "bad_date", None

    def moment(candidate: date) -> datetime:
        return datetime(
            candidate.year, candidate.month, candidate.day,
            clock.hour, clock.minute, tzinfo=TAIPEI,
        )

    def nearest(candidates: list[date]) -> date:
        return min(
            candidates,
            key=lambda candidate: (abs((candidate - current).days), candidate < current),
        )

    closest = nearest(valid)
    age = (now - moment(closest)).total_seconds()
    if 0 <= age <= 60 * 86400:
        # 「10/3」 on 10/5, or 「10/4早上9點」 at 13:00 on 10/4: the date nearest
        # the reminder just passed; refuse instead of moving a year ahead.
        return "past", None
    future = [candidate for candidate in valid if moment(candidate) > now]
    chosen = nearest(future) if future else None
    if chosen is None or abs((chosen - current).days) > 366:
        return "bad_date", None  # e.g. 「2/29」 with no leap day within a year
    return "ok", chosen


def _resolve_weekday(spec: DateSpec, message_day: date, current: date) -> tuple[str, date | None]:
    weekday = int(spec.weekday or 0)
    if not spec.week_prefix:
        delta = (weekday - message_day.weekday()) % 7
        if delta == 0:
            return "ambiguous_weekday", None
        from_today = message_day + timedelta(days=delta)
        if (current - message_day).days >= 7:
            same_week = current - timedelta(days=current.weekday()) + timedelta(days=weekday)
            if same_week != from_today:
                return "ambiguous_weekday", None
        return "ok", from_today
    week_start = message_day - timedelta(days=message_day.weekday())
    offset = {"這": 0, "本": 0, "下": 7, "下下": 14}[spec.week_prefix]
    target = week_start + timedelta(days=offset + weekday)
    if target < message_day:
        return "past", None
    return "ok", target


def resolve_new_schedule(
    request: RescheduleRequest,
    *,
    current_remind_at: int,
    message_time: datetime,
    now: datetime,
) -> tuple[str, int | None]:
    """Turn a candidate into ("ok", epoch) or (reason, None)."""

    if request.status != CANDIDATE:
        return request.reason or "unparsed", None
    current = datetime.fromtimestamp(int(current_remind_at), TAIPEI)
    message_day = message_time.astimezone(TAIPEI).date()
    now_tw = now.astimezone(TAIPEI)
    clock = request.clock or Clock(current.hour, current.minute)
    spec = request.date
    if spec is None:
        target = current.date()
    elif spec.kind == "absolute":
        try:
            target = date(int(spec.year or 0), int(spec.month or 0), int(spec.day or 0))
        except ValueError:
            return "bad_date", None
    elif spec.kind == "month_day":
        status, found = _nearest_future_month_day(
            int(spec.month or 0), int(spec.day or 0), current.date(), clock, now_tw
        )
        if found is None:
            return status, None
        target = found
    elif spec.kind == "relative":
        target = message_day + timedelta(days=int(spec.offset_days or 0))
    else:
        status, found = _resolve_weekday(spec, message_day, current.date())
        if found is None:
            return status, None
        target = found
    if request.weekday_check is not None and target.weekday() != request.weekday_check:
        return "weekday_mismatch", None
    new_dt = datetime(
        target.year, target.month, target.day, clock.hour, clock.minute, tzinfo=TAIPEI
    )
    if new_dt <= now_tw:
        return "past", None
    return "ok", int(new_dt.timestamp())


# ── Place → action ─────────────────────────────────────────────────────────

_MANAGED_PLACE_RE = re.compile(r"，地點：[^，]*$")
# Deliberately broad: a false hit only refuses to add a second place.
_ACTION_PLACE_RE = re.compile(
    r"醫院|診所|衛生所|中心|車站|公司|學校|大學|公園|機場|飯店|酒店|餐廳|市場|銀行|"
    r"郵局|大樓|廣場|教會|教堂|藥局|門口|地點|地址|(?<![A-Za-z])B\d+|"
    r"\d+\s*(?:號|樓|[Ff](?![A-Za-z]))|"
    r"[\u4e00-\u9fffA-Za-z0-9](?:路|街|巷|弄|段|區|市|縣|鄉|鎮|村|里|場|棟|室|站|館|店|寺|廟)"
)


def _compact(text: str) -> str:
    return "".join(_normalize(text).split()).casefold()


def merge_location(action: str, location: str | None) -> tuple[str, str]:
    """Return ("ok", new_action) or ("location_conflict", action)."""

    if not location:
        return "ok", action
    managed = _MANAGED_PLACE_RE.search(action)
    if managed:
        if _compact(managed.group(0)) == _compact(f"，地點：{location}"):
            return "ok", action
        return "ok", f"{action[: managed.start()]}，地點：{location}"
    if _compact(location) in _compact(action):
        return "ok", action
    if _ACTION_PLACE_RE.search(_normalize(action)):
        return "location_conflict", action
    return "ok", f"{action}，地點：{location}"


# ── Reply texts ─────────────────────────────────────────────────────────────


def format_when(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), TAIPEI).strftime("%Y-%m-%d %H:%M")


def updated_receipt(old_at: int, new_at: int, action: str) -> str:
    if int(old_at) == int(new_at):
        head = "已更新提醒（時間不變，地點已更新）"
    else:
        head = f"已更新提醒（{format_when(old_at)} → {format_when(new_at)}）"
    return f"{head}\n時間：{format_when(new_at)}\n事項：{action}"


def unchanged_receipt(remind_at: int, action: str) -> str:
    return f"提醒本來就是這個時間，沒有變更。\n時間：{format_when(remind_at)}\n事項：{action}"


def replay_receipt(
    old_at: int,
    new_at: int,
    *,
    current_at: int | None = None,
    current_action: str | None = None,
) -> str:
    head = f"這則更正先前已處理（{format_when(old_at)} → {format_when(new_at)}）"
    if current_at is None or current_action is None:
        return head + "。"
    return (
        f"{head}，之後提醒又被改過。\n"
        f"時間：{format_when(current_at)}\n事項：{current_action}"
    )


_REFUSALS = {
    "unparsed": "看不懂要改成哪一天或幾點，提醒沒有變更。\n請引用提醒，只寫新的日期或時間，例如「改成 10/8」或「時間：早上 9 點」。",
    "offset_unsupported": "目前不能用「延後一天」這種寫法，提醒沒有變更。\n請直接寫新的日期或時間，例如「改成 10/8」或「時間：早上 9 點」。",
    "daypart_only": "只寫了早上／下午，沒有寫幾點，提醒沒有變更。\n請寫明確時間，例如「早上 9 點」或「15:00」。",
    "ambiguous_hour": "沒寫是早上還是下午，提醒沒有變更。\n請寫「早上 9 點」「下午 3 點」或 24 小時制（例如 15:00）。",
    "bad_time": "時間的寫法對不起來，提醒沒有變更。\n請寫成「早上 9 點」「下午 3 點」或 24 小時制（例如 15:00）。",
    "ambiguous_midnight": "「半夜」的時間不確定是哪一天，提醒沒有變更。\n請寫成隔天的日期加時間，例如「10/9 00:30」。",
    "range": "寫了一段時間範圍，提醒只能設一個時間，提醒沒有變更。\n請只寫開始的時間。",
    "multiple_dates": "寫了不只一個日期，提醒沒有變更。\n請只寫要改成的那一天。",
    "multiple_times": "寫了不只一個時間，提醒沒有變更。\n請只寫要改成的那個時間。",
    "bad_date": "日期不存在，提醒沒有變更。",
    "weekday_mismatch": "日期和星期對不起來，提醒沒有變更。\n請確認日期後再寫一次。",
    "ambiguous_weekday": "只寫星期幾，不確定是哪一週，提醒沒有變更。\n請寫日期，例如「10/8」。",
    "past": "新的時間已經過了，提醒沒有變更。",
    "unsupported_location": "地點的寫法看不懂，提醒沒有變更。\n地點請只寫一個地方，例如「地點：中山路1號2樓」。",
    "location_conflict": "原本的提醒已經寫了地點，不能直接換成新地點，提醒沒有變更。\n要換地點請取消後重新建立。",
    "multiple": "引用的訊息裡有多筆提醒，不確定要改哪一筆，提醒沒有變更。\n請引用單一筆提醒的推播再改。",
    "ambiguous": "這則訊息對應到不只一筆提醒，為避免改錯，提醒沒有變更。",
    "duplicate": "這則訊息對應到不只一筆提醒，為避免改錯，提醒沒有變更。",
    "not_found": "這筆提醒已經提醒過、取消或改過了，提醒沒有變更。\n要再提醒，請直接新增一筆；要改最新的提醒，請引用最新的那則訊息。",
    "terminal": "這筆提醒已經提醒過、取消或改過了，提醒沒有變更。\n要再提醒，請直接新增一筆；要改最新的提醒，請引用最新的那則訊息。",
    "stale_quote": "這筆提醒之後已經被改過，提醒沒有變更。\n請引用最新的提醒訊息再改。",
    "conflict": "這筆提醒剛被改過，提醒沒有變更。\n請引用最新的提醒訊息再改一次。",
    "collision": "改完會和另一筆相同的提醒重複，提醒沒有變更。",
    "busy": "這筆提醒正在推送，為避免送出舊內容，提醒沒有變更。\n請稍後再引用提醒改一次。",
    "delivery_uncertain": "這筆提醒上一則推播的送出結果不明，為避免重複或漏送，提醒沒有變更。\n如需改時間，請取消後重新建立。",
    "calendar": "這則是行事曆活動的提醒，要連同活動一起改，提醒沒有變更。\n請引用活動提醒那則，寫「改成 日期 時間」。",
    "not_generic": "這筆提醒和其他紀錄連動，不能單獨改時間，提醒沒有變更。",
    "display_unsafe": "這筆提醒的內容無法完整顯示確認，為避免沒確認就改動，提醒沒有變更。",
    "question": "提醒沒有變更。要改時間請直接寫，例如「改到 10/8」。",
    "unsupported_change": "目前只能改日期或時間，提醒沒有變更。",
    "unavailable": "系統暫時無法更新，提醒沒有變更。\n請稍後再試一次。",
}


def refusal_text(reason: str) -> str:
    """Every refusal starts with 「尚未更新提醒：」 (acceptance ③)."""

    return "尚未更新提醒：" + _REFUSALS.get(reason, _REFUSALS["unparsed"])
