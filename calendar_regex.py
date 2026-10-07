"""Regex-based family event extractor — pure rule, no Gemini dependency.

Called by `calendar_extractor.extract()` when both Gemini models fail
(quota exhausted / network down). Covers the most common explicit family
event phrasings:

  - "YYYY-MM-DD HH:MM <title>"  (e.g. "2026-05-22 14:00 拿喜來登蛋糕")
  - "N月N日 HH:MM <title>"      (year inferred; past → +1)
  - "今天/明天/後天 HH:MM <title>"

Whitelist (precise, codex/GP1 反饋) prevents false positives on work events
('明天 14:00 開週會') and common verb fragments ('我拿到票了').
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, timedelta

import reminder_intent

logger = logging.getLogger(__name__)


# 家族 keyword whitelist —— 動詞要配名詞，避免「拿」「接」「陪」單字濫觸
_FAMILY_KW = re.compile(
    r"(?:聚餐|生日|出遊|看醫生|蛋糕|爺爺|奶奶|爸爸|媽媽|姊姊|妹妹|弟弟|全家|"
    r"牙醫|東海|校友|美僑|六福萬怡|"
    r"拿(?:蛋糕|藥|包裹|貨|餐|禮物|花)|"
    r"接(?:爸|媽|妹|弟|姊|爺爺|奶奶|小孩|小朋友)|"
    r"接送|接機|機場接送|機場|搭機|打球|羽球|壁球|洗牙|預約|"
    r"陪(?:爸|媽|妹|弟|姊|爺爺|奶奶|看醫生)|"
    r"喜來登|"
    r"回(?:家|老家)|"
    r"婚禮|喜宴|滿月|彌月)"
)

_FAMILY_ACTION_HINT = re.compile(
    r"(?:聚餐|聚會|活動|看醫生|看病|洗牙|預約|領|拿|接送|接機|機場|"
    r"打球|羽球|壁球|"
    r"到|回台北|回台中|回高雄|回老家|去|帶|送|領藥|參加|"
    r"全家|爸爸|媽媽|姊姊|妹妹|弟弟|蛋糕|生日)"
)

_MEDICAL_DEPARTMENT = (
    r"(?:胸腔外科|胸腔內科|心臟內科|心臟血管外科|神經內科|神經外科|"
    r"肝膽腸胃科|腸胃內科|胃腸科|消化內科|泌尿科|骨科|皮膚科|眼科|"
    r"耳鼻喉科|婦產科|小兒科|兒科|精神科|復健科|腫瘤科|血液科|"
    r"感染科|家醫科|家庭醫學科|新陳代謝科|內分泌科|乳房外科|"
    r"大腸直腸外科|整形外科|一般外科|腎臟科|風濕免疫科|牙科)"
)

# Plan C：三類 event_type 分類規則（醫療 > 個人旅程 > 家族聚會，medical 最先因更具體）
# 注意：personal_trip「回 X」明列地名不含「家」單字，避免「回家吃飯」誤分類為 trip
_TYPE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    (
        "medical",
        re.compile(
            r"做(?:胃鏡|大腸鏡|健康檢查|體檢|手術|健檢)|"
            rf"看(?:醫生|牙醫|{_MEDICAL_DEPARTMENT})|"
            r"看.{0,12}(?:牙醫|醫師|醫生)|"
            r"牙醫|"
            r"陪(?:.{0,5})(?:就醫|看醫生|看病|拿藥)|"
            r"打疫苗|抽血|健檢|回診|"
            r"(?:做|照|安排|接受)\s*(?<![A-Za-z])(?:MRI|PET(?:-CT)?)(?![A-Za-z])|"
            r"(?<![A-Za-z])MRI(?![A-Za-z]).{0,8}(?:掃描|檢查|結果|影像)|"
            r"(?<![A-Za-z])PET(?![A-Za-z]).{0,4}(?:掃描|檢查|結果|影像)|"
            r"(?<![A-Za-z])PET-CT(?![A-Za-z])|"
            r"核磁共振|正子斷層"
        ),
    ),
    (
        "personal_trip",
        re.compile(
            r"回(?:台北|新北|台中|台南|高雄|花蓮|宜蘭|新竹|苗栗|嘉義|屏東|台東|老家)|"
            r"北上|南下|出差|"
            r"搭(?:高鐵|火車|客運|台鐵)"
        ),
    ),
)


def _normalize_event_text(text: str) -> str:
    """Normalize only unambiguous medical scan spellings."""

    normalized = re.sub(
        r"(?<![A-Za-z])[Mm]\s*[Rr]\s*[Ii](?![A-Za-z])"
        r"(?=.{0,8}(?:掃描|檢查|結果|影像))",
        "MRI",
        text,
    )
    normalized = re.sub(
        r"(?<![A-Za-z])[Pp][Ee][Tt](?=\s*(?:掃描|檢查|正子))",
        "PET",
        normalized,
    )
    normalized = re.sub(
        r"(?<![A-Za-z])[Pp][Ee][Tt]-?[Cc][Tt](?![A-Za-z])",
        "PET-CT",
        normalized,
    )
    normalized = re.sub(
        r"((?:做|照|安排|接受)\s*)[Mm]\s*[Rr]\s*[Ii](?![A-Za-z])",
        r"\1MRI",
        normalized,
    )
    normalized = re.sub(
        r"((?:做|照|安排|接受)\s*)[Pp][Ee][Tt](?![A-Za-z])",
        r"\1PET",
        normalized,
    )
    return normalized


def _classify_type(title: str) -> str | None:
    """回 'medical' / 'personal_trip' / 'family_gathering' / None(不認)。

    GP2 反饋：classify=None 不要 silently default 成 family_gathering，
    寧可 reject 也別 mis-tag。
    """
    for name, pat in _TYPE_PATTERNS:
        if pat.search(title):
            return name
    if _FAMILY_KW.search(title):
        return "family_gathering"
    return None

# HH:MM 或 HHMM — validated 00:00 to 23:59（支援「1430」、「08:30」）
_TIME_COLON = r"(?:[01]?\d|2[0-3])[:：][0-5]\d"
_TIME_COMPACT = r"(?<!\d)(?:2[0-3]|[01]?\d)[0-5]\d(?!\d)"
_TIME_COMPACT_4 = r"(?<!\d)(?:2[0-3]|[01]\d)[0-5]\d(?!\d)"
_TIME = rf"(?:{_TIME_COLON}|{_TIME_COMPACT})"
_CHINESE_NUM = r"[零〇一二兩三四五六七八九十]{1,3}"
_CHINESE_MINUTE = rf"(?:(?<!\d)\d{{1,2}}(?!\d)|{_CHINESE_NUM})"
_CHINESE_TIME = (
    rf"(?:(?:(?<!\d)\d{{1,2}}(?!\d)|{_CHINESE_NUM})\s*點"
    rf"(?:\s*(?:半|{_CHINESE_MINUTE}\s*分?))?)"
)
_DAYPART = r"(?:早上|上午|中午|下午|傍晚|晚上|凌晨|半夜)"
_DAYPART_DEFAULTS = {
    "早上": "09:00",
    "上午": "09:00",
    "中午": "12:00",
    "下午": "15:00",
    "傍晚": "18:00",
    "晚上": "19:00",
}
_REL_DAY = r"(?:今天|明天|後天|大後天)"
_WEEKDAY = r"(?:(?:星期|週|周|禮拜)[一二三四五六日天])"
_REL_OR_WEEKDAY = rf"(?:{_REL_DAY}|{_WEEKDAY})"

_DATE_TIME_TITLE = re.compile(
    rf"(\d{{4}})-(\d{{1,2}})-(\d{{1,2}})\s+({_TIME})\s+([^\n\r]{{2,40}})"
)

# An explicit year: 2026年, 115年 or 民國99年 (the 民國 year +1911), spaces
# allowed.  Any other number before 年 (「25 年」) is a year we cannot read:
# the date makes no event rather than being read as yearless.
_YEAR_NUM = r"民國\s*\d{1,3}|\d{1,4}"
_NO_YEAR = r"(?<!\d)(?<!\d年)(?<!\d年\s)(?<!\d\s年)"
_CHINESE_DATE_TIME_TITLE = re.compile(
    rf"(?:({_YEAR_NUM})\s*年\s*|{_NO_YEAR})(\d{{1,2}})月(\d{{1,2}})日\s*({_TIME})\s+([^\n\r]{{2,40}})"
)

_RELATIVE_DATE_TIME_TITLE = re.compile(
    rf"(今天|明天|後天)\s*({_TIME})\s+([^\n\r]{{2,40}})"
)

_RELATIVE_CHINESE_TIME_TITLE = re.compile(
    rf"({_REL_OR_WEEKDAY})\s*({_DAYPART})?\s*({_TIME}|{_CHINESE_TIME})\s*([^\n\r]{{2,40}})"
)

_DATE_TOKEN = re.compile(
    rf"(?:\d{{4}}-\d{{1,2}}-\d{{1,2}}|(?:(?:{_YEAR_NUM})\s*年\s*|{_NO_YEAR})\d{{1,2}}月\d{{1,2}}日"
    r"|\d{1,2}/\d{1,2})"
)
_DATE_PREFIX = re.compile(
    rf"^(?:(\d{{4}})-(\d{{1,2}})-(\d{{1,2}})|(?:({_YEAR_NUM})\s*年\s*|{_NO_YEAR})(\d{{1,2}})月(\d{{1,2}})日"
    r"|(\d{1,2})/(\d{1,2}))"
)
# 「2028年1月5日、1月6日」「2025年9月24日到9月26日」: a date in a list or range
# shares the year written on the date before it.
# Between them only a weekday mark or a time: 「1月5日（三）晚上7點、1月6日」.  A bare
# 「，」 starts a new sentence: 「生日是1940年10月10日，10月10日聚餐」.
_LIST_OR_RANGE_GAP_RE = re.compile(
    rf"\s*(?:[（(][一二三四五六日天][)）])?\s*(?:{_DAYPART})?\s*(?:{_TIME}|{_CHINESE_TIME})?\s*"
    r"(?:、|和|跟|及|與|到|至|~|～|-|－|—)\s*"
)
_TIME_IN_TEXT = re.compile(
    rf"(?:(?P<compact_daypart>{_DAYPART})\s*"
    rf"(?P<compact_time>{_TIME_COMPACT})|"
    rf"(?P<regular_daypart>{_DAYPART})?\s*"
    rf"(?P<regular_time>{_TIME_COLON}|{_CHINESE_TIME}|"
    rf"(?<![\s\S]){_TIME_COMPACT}))"
)
_DAYPART_IN_TEXT = re.compile(_DAYPART)
_TIME_RANGE_SEPARATOR = r"(?:-|－|—|–|~|～|到|至)"
_TIME_RANGE_IN_TEXT = re.compile(
    rf"(?:(?P<range_compact_daypart>{_DAYPART})\s*"
    rf"(?P<range_daypart_compact_start>{_TIME_COMPACT})|"
    rf"(?<![\s\S])(?P<range_leading_compact_start>{_TIME_COMPACT_4})|"
    rf"(?:(?P<range_regular_daypart>{_DAYPART})\s*)?"
    rf"(?P<range_regular_start>{_TIME_COLON}|{_CHINESE_TIME}))"
    rf"\s*{_TIME_RANGE_SEPARATOR}\s*"
    rf"(?:(?P<range_end_daypart>{_DAYPART})\s*)?"
    rf"(?P<range_end>{_TIME_COLON}|{_CHINESE_TIME}|{_TIME_COMPACT})"
)
_BROAD_TIME_RANGE_CANDIDATE_RE = re.compile(
    rf"(?<!\S)(?:(?:{_DAYPART}\s*)"
    rf"(?:\d{{3,5}}|\d{{1,2}}[:：]\d{{2,3}}|{_CHINESE_TIME})|"
    rf"(?:\d{{4,5}}|\d{{1,2}}[:：]\d{{2,3}}|{_CHINESE_TIME}))"
    rf"\s*{_TIME_RANGE_SEPARATOR}\s*"
    rf"(?:{_DAYPART}\s*)?"
    rf"(?:\d{{3,5}}|\d{{1,2}}[:：]\d{{2,3}}|{_CHINESE_TIME})(?!\d)"
)
_UNQUALIFIED_THREE_DIGIT_RANGE_RE = re.compile(
    rf"^\d{{3}}\s*{_TIME_RANGE_SEPARATOR}\s*\d{{4,5}}(?!\d)"
)
_THREE_DIGIT_RANGE_AT_START_RE = re.compile(
    r"^\d{3}\s*(?:-|－|—|–|~|～|到|至)\s*\d{3}(?!\d)"
)
_THREE_DIGIT_RANGE_RE = re.compile(
    r"(?<!\d)\d{3}\s*(?:-|－|—|–|~|～|到|至)\s*\d{3}(?!\d)"
)


def _make_fail() -> dict:
    return {
        "has_event": False,
        "is_cancellation": False,
        "title": None,
        "date": None,
        "time": None,
        "location": None,
        "participants": [],
        "cancel_target_keyword": None,
        "event_type": "family_gathering",
    }


def _make_event(
    title: str, date_iso: str, time_str: str, event_type: str
) -> dict:
    return {
        "has_event": True,
        "is_cancellation": False,
        "title": title,
        "date": date_iso,
        "time": time_str,
        "location": None,
        "participants": [],
        "cancel_target_keyword": None,
        "event_type": event_type,
    }


def _accepted_event(source_text: str, event: dict) -> dict:
    if reminder_intent.should_reject_reminder_candidate(
        source_text,
        event.get("title"),
    ):
        return _make_fail()
    return event


def _dedupe_events(events: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str, str]] = set()
    out: list[dict] = []
    for ev in events:
        key = (
            str(ev.get("date") or ""),
            str(ev.get("time") or ""),
            str(ev.get("title") or ""),
            str(ev.get("location") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def _sanitize_title(raw: str) -> str:
    """Strip control chars + trim length."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", raw).strip()
    return cleaned[:30]


def _validate_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _explicit_year(token: str) -> int | None:
    """「2026年」, a Republic-of-China 「115年」「民國99年」 (+1911); None for 「25年」."""
    digits = re.sub(r"\D", "", token)
    if token.lstrip().startswith("民國") or (len(digits) == 3 and digits.startswith("1")):
        return int(digits) + 1911
    return int(digits) if len(digits) == 4 else None


def _month_day(token: str) -> tuple[int, int] | None:
    m = _DATE_PREFIX.match(token)
    if not m:
        return None
    if m.group(1):
        return int(m.group(2)), int(m.group(3))
    if m.group(5):
        return int(m.group(5)), int(m.group(6))
    return int(m.group(7)), int(m.group(8))


def _shared_years(text: str) -> dict[int, int]:
    """Start of each date token → the year it shares with the date before it.

    「2026年12月31日到1月2日」: a later date that comes earlier in the calendar is
    in the next year.
    """
    hints: dict[int, int] = {}
    matches = list(_DATE_TOKEN.finditer(text))
    for match, following in zip(matches, matches[1:]):
        prefix = _DATE_PREFIX.match(match.group(0))
        if prefix and prefix.group(1):
            year = int(prefix.group(1))
        elif prefix and prefix.group(4):
            year = _explicit_year(prefix.group(4)) or 0  # unreadable: shares "no event"
        else:
            year = hints.get(match.start())
        if year is not None and _LIST_OR_RANGE_GAP_RE.fullmatch(text[match.end():following.start()]):
            here, there = _month_day(match.group(0)), _month_day(following.group(0))
            hints[following.start()] = year + 1 if year and here and there and there < here else year
    return hints


def _parse_date_token(token: str, today_tw: date, year_hint: int | None = None) -> date | None:
    m = _DATE_PREFIX.match(token)
    if not m:
        return None
    if m.group(1):
        return _validate_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if m.group(4):
        # An explicit year is the year meant; a date already past is no event.
        year = _explicit_year(m.group(4))
        target = _validate_date(year, int(m.group(5)), int(m.group(6))) if year else None
        return target if target and target >= today_tw else None
    if m.group(5):
        month, day = int(m.group(5)), int(m.group(6))
    else:
        month, day = int(m.group(7)), int(m.group(8))
    if year_hint is not None:
        target = _validate_date(year_hint, month, day)
        return target if target and target >= today_tw else None
    target = _validate_date(today_tw.year, month, day)
    if target and target < today_tw:
        target = _validate_date(today_tw.year + 1, month, day)
    return target


_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _parse_chinese_int(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    if "十" in raw:
        left, _, right = raw.partition("十")
        tens = 1 if left == "" else _CN_DIGITS.get(left)
        ones = 0 if right == "" else _CN_DIGITS.get(right)
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    if len(raw) == 1:
        return _CN_DIGITS.get(raw)
    return None


def _parse_time(raw_time: str, daypart: str | None = None) -> str | None:
    raw_time = re.sub(r"\s+", "", raw_time)
    raw_time = raw_time.replace("：", ":")
    m = re.fullmatch(_TIME, raw_time)
    if m:
        if ":" in raw_time:
            hour_s, minute_s = raw_time.split(":", 1)
        elif len(raw_time) == 3:
            hour_s, minute_s = raw_time[:-2], raw_time[-2:]
        elif len(raw_time) == 4:
            hour_s, minute_s = raw_time[:2], raw_time[2:4]
        else:
            return None
        hour, minute = int(hour_s), int(minute_s)
    else:
        m = re.fullmatch(
            rf"(\d{{1,2}}|{_CHINESE_NUM})點(?:(半)|(\d{{1,2}}|{_CHINESE_NUM})分?)?",
            raw_time,
        )
        if not m:
            return None
        hour = _parse_chinese_int(m.group(1))
        if hour is None:
            return None
        if m.group(2):
            minute = 30
        elif m.group(3):
            minute = _parse_chinese_int(m.group(3))
            if minute is None:
                return None
        else:
            minute = 0

    if minute < 0 or minute > 59:
        return None
    if daypart == "晚上" and hour == 12:
        return None
    if daypart in ("下午", "傍晚", "晚上") and 1 <= hour < 12:
        hour += 12
    elif daypart == "中午" and hour < 11:
        hour += 12
    elif daypart in ("凌晨", "半夜") and hour == 12:
        hour = 0
    if hour < 0 or hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def _first_time_in_text(text: str) -> str | None:
    masked = _THREE_DIGIT_RANGE_AT_START_RE.sub(" ", text, count=1)
    if masked != text:
        after_room = re.match(
            r"^\s*(?:號?房|房間|室|樓)\s*",
            masked,
        )
        text = masked[after_room.end():] if after_room else masked
    range_match = _TIME_RANGE_IN_TEXT.search(text)
    if range_match:
        if (
            range_match.group("range_leading_compact_start")
            and len(re.sub(r"\D", "", range_match.group("range_end"))) != 4
        ):
            return None
        start = _parse_time(
            range_match.group("range_daypart_compact_start")
            or range_match.group("range_leading_compact_start")
            or range_match.group("range_regular_start"),
            range_match.group("range_compact_daypart")
            or range_match.group("range_regular_daypart"),
        )
        end = _parse_time(
            range_match.group("range_end"),
            range_match.group("range_end_daypart"),
        )
        return start if start is not None and end is not None else None
    m = _TIME_IN_TEXT.search(text)
    if m:
        return _parse_time(
            m.group("compact_time") or m.group("regular_time"),
            m.group("compact_daypart") or m.group("regular_daypart"),
        )
    daypart = _DAYPART_IN_TEXT.search(text)
    if daypart:
        return _DAYPART_DEFAULTS.get(daypart.group(0))
    return None


def _has_malformed_compact_range(text: str) -> bool:
    if _UNQUALIFIED_THREE_DIGIT_RANGE_RE.search(text):
        return True
    for match in _BROAD_TIME_RANGE_CANDIDATE_RE.finditer(text):
        strict = _TIME_RANGE_IN_TEXT.fullmatch(match.group(0))
        if strict is None:
            return True
        if _first_time_in_text(strict.group(0)) is None:
            return True
    return False


_WEEKDAY_TO_INDEX = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}


def _target_for_relative_or_weekday(token: str, today_tw: date) -> date | None:
    if token in ("今天", "明天", "後天", "大後天"):
        return today_tw + timedelta(days={"今天": 0, "明天": 1, "後天": 2, "大後天": 3}[token])
    m = re.fullmatch(r"(?:星期|週|周|禮拜)([一二三四五六日天])", token)
    if not m:
        return None
    target_idx = _WEEKDAY_TO_INDEX[m.group(1)]
    days = (target_idx - today_tw.weekday()) % 7
    return today_tw + timedelta(days=days)


def extract_regex_only(combined_text: str, today_tw: date) -> dict:
    """Pure-regex family event extractor.

    Returns dict matching calendar_extractor.extract() schema.
    All title hits must pass _FAMILY_KW.search() to filter out work events
    and irrelevant date-time strings.
    """
    if not combined_text or not combined_text.strip():
        return _make_fail()
    combined_text = _normalize_event_text(combined_text)
    if _has_malformed_compact_range(combined_text):
        return _make_fail()

    # 1. YYYY-MM-DD HH:MM title
    # TODO(2026-10-03 review): unlike 「YYYY年M月D日」, a past ISO date still makes an
    # event; test_calendar_regex pins the 5/22 case, so settle the ISO rule first.
    m = _DATE_TIME_TITLE.search(combined_text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        target = _validate_date(year, month, day)
        time_str = _parse_time(m.group(4))
        title = _sanitize_title(m.group(5))
        et = _classify_type(title) if len(title) >= 2 else None
        if target and time_str and et:
            return _accepted_event(
                combined_text,
                _make_event(title, target.isoformat(), time_str, et),
            )

    # 2. (YYYY年)N月N日 HH:MM title — no year: past → +1; explicit past year: no event
    m = _CHINESE_DATE_TIME_TITLE.search(combined_text)
    if m:
        month, day = int(m.group(2)), int(m.group(3))
        time_str = _parse_time(m.group(4))
        title = _sanitize_title(m.group(5))
        et = _classify_type(title) if len(title) >= 2 else None
        if time_str and et:
            explicit = bool(m.group(1))
            year = _explicit_year(m.group(1)) if explicit else _shared_years(combined_text).get(m.start(2))
            if explicit or year is not None:
                target = _validate_date(year, month, day) if year else None
                if target and target < today_tw:
                    target = None
            else:
                target = _validate_date(today_tw.year, month, day)
                if target and target < today_tw:
                    target = _validate_date(today_tw.year + 1, month, day)
            if target:
                return _accepted_event(
                    combined_text,
                    _make_event(title, target.isoformat(), time_str, et),
                )

    # 3. 今天/明天/後天 HH:MM title
    m = _RELATIVE_DATE_TIME_TITLE.search(combined_text)
    if m:
        offset = {"今天": 0, "明天": 1, "後天": 2}[m.group(1)]
        target = today_tw + timedelta(days=offset)
        time_str = _parse_time(m.group(2))
        title = _sanitize_title(m.group(3))
        et = _classify_type(title) if len(title) >= 2 else None
        if time_str and et:
            return _accepted_event(
                combined_text,
                _make_event(title, target.isoformat(), time_str, et),
            )

    # 4. 今天/星期四 + 中文時間 title（例：星期四早上十點半看牙醫）
    m = _RELATIVE_CHINESE_TIME_TITLE.search(combined_text)
    if m:
        target = _target_for_relative_or_weekday(m.group(1), today_tw)
        time_str = _parse_time(m.group(3), m.group(2))
        title = _sanitize_title(m.group(4))
        et = _classify_type(title) if len(title) >= 2 else None
        if target and time_str and et:
            return _accepted_event(
                combined_text,
                _make_event(title, target.isoformat(), time_str, et),
            )

    return _make_fail()


def _strip_date_prefix(segment: str) -> str:
    segment = _DATE_PREFIX.sub("", segment.strip(), count=1)
    return re.sub(r"^[（(]?(?:星期|週|周|禮拜)?[一二三四五六日天][）)]?", "", segment).strip()


def _extract_location(segment_body: str) -> str | None:
    # 避免在「現在大漲」這類句子誤命中「在」作為關鍵字。
    # 僅接受句首或「空白/標點」前綴後的「在/地點」。
    m = re.search(
        rf"(?:^|[\s，、。；；：()（）])(?:在|地點[:：])\s*(.+?)"
        rf"(?={_DAYPART}|{_TIME_COLON}|{_CHINESE_TIME}|做|看|回診|抽血|"
        rf"打疫苗|MRI|PET|核磁|正子|全家|聚餐|打羽球|打壁球|$|。|，|；|;)",
        segment_body,
    )
    if not m:
        # 「校友會在美僑俱樂部」前面不是標點，但後面明顯是場地；
        # 保守開 fallback，避免把「現在大漲」這類一般文字當 location。
        m = re.search(
            rf"在\s*(.+?)(?={_DAYPART}|{_TIME_COLON}|{_CHINESE_TIME}|做|看|"
            rf"回診|抽血|打疫苗|MRI|PET|核磁|正子|全家|聚餐|打羽球|"
            rf"打壁球|$|。|，|；|;)",
            segment_body,
        )
        if m and not re.search(
            r"美僑|六福萬怡|俱樂部|酒店|飯店|餐廳|醫院|診所|會館|會議室|"
            r"廳|樓|館|中心|機場|車站|火車站|捷運站",
            m.group(1),
        ):
            m = None
    if not m:
        return None
    location = re.sub(r"[（(][^）)]*$", "", m.group(1)).strip(" ，。；;、")
    location = re.sub(r"(?:聚會|活動|集合)$", "", location).strip(" ，。；;、")
    return location[:120] if location else None


def _strip_fragment_time_tokens(text: str) -> str:
    protected: list[str] = []

    def protect(match: re.Match) -> str:
        protected.append(match.group(0))
        return f"ROOMRANGEPLACEHOLDER{len(protected) - 1}"

    stripped = _THREE_DIGIT_RANGE_RE.sub(protect, text)
    stripped = re.sub(_TIME_RANGE_IN_TEXT, "", stripped)
    stripped = re.sub(_TIME_IN_TEXT, "", stripped)
    stripped = re.sub(_DAYPART_IN_TEXT, "", stripped)
    for index, original in enumerate(protected):
        stripped = stripped.replace(
            f"ROOMRANGEPLACEHOLDER{index}",
            original,
        )
    return stripped


def _title_for_fragment(segment_body: str, location: str | None, combined_text: str) -> str:
    haystack = f"{segment_body} {combined_text}"
    if "東海" in haystack and "校友" in haystack:
        if location and "六福" in location:
            return "東海大學校友會活動（六福萬怡酒店）"
        if location and "美僑" in location:
            return "東海大學校友會活動（美僑俱樂部）"
        return "東海大學校友會活動"
    if location:
        action_body = _strip_fragment_time_tokens(segment_body)
        action_body = re.sub(
            rf"(?:^|[\s，、。；；：()（）])(?:在|地點[:：])\s*{re.escape(location)}",
            " ",
            action_body,
            count=1,
        )
        action_body = action_body.strip(" ，。；;、")
        if (
            action_body
            and _classify_type(f"{action_body} {combined_text}") == "medical"
        ):
            return _sanitize_title(action_body)
        if re.search(r"(?:需要|必須|記得|要|需)帶", action_body):
            return f"{location}，{action_body}"
        return f"{location}活動"[:30]
    raw = _strip_fragment_time_tokens(segment_body)
    raw = re.sub(r"^(?:以及|和|並且|、|，|,|的)", "", raw).strip()
    return _sanitize_title(raw or "行程")


def _is_weak_fragment_title(title: str | None) -> bool:
    """Return True when a fragment only contains a connector/noise word.

    Example: "7/5 1600和7/12 1800 全家打羽球" gives the first fragment title
    "和"; the actual shared title lives after the final date/time fragment.
    """
    cleaned = re.sub(r"[\s，,、。；;：:（）()]+", "", title or "")
    return cleaned in {"", "行程", "和", "以及", "並且", "還有", "及", "與", "跟", "同"}


def _shared_tail_title(combined_text: str, today_tw: date) -> str | None:
    matches = list(_DATE_TOKEN.finditer(combined_text))
    if len(matches) < 2:
        return None
    tail = combined_text[matches[-1].start() :]
    token_match = _DATE_PREFIX.match(tail.strip())
    if not token_match:
        return None
    if not _parse_date_token(token_match.group(0), today_tw):
        return None
    body = _strip_date_prefix(tail)
    location = _extract_location(body)
    title = _title_for_fragment(body, location, combined_text)
    if _is_weak_fragment_title(title):
        return None
    if not _classify_type(f"{title} {body} {combined_text}"):
        return None
    return title


def _fragment_event(
    segment: str,
    today_tw: date,
    combined_text: str,
    *,
    require_time: bool = True,
    fallback_title: str | None = None,
    year_hint: int | None = None,
) -> dict | None:
    token_match = _DATE_PREFIX.match(segment.strip())
    if not token_match:
        return None
    target = _parse_date_token(token_match.group(0), today_tw, year_hint)
    if not target:
        return None
    body = _strip_date_prefix(segment)
    if _has_malformed_compact_range(body):
        return None
    time_str = _first_time_in_text(body)
    if not time_str:
        if require_time:
            return None
        time_str = None
    location = _extract_location(body)
    title = _title_for_fragment(body, location, combined_text)
    if fallback_title and _is_weak_fragment_title(title):
        title = fallback_title
    et = _classify_type(f"{title} {body}")
    if not et:
        return None
    if et == "family_gathering" and not location and not _FAMILY_ACTION_HINT.search(title):
        return None
    ev = _make_event(title, target.isoformat(), time_str, et)
    ev["location"] = location
    if reminder_intent.should_reject_reminder_candidate(combined_text, title):
        return None
    return ev


def extract_many_regex_only(
    combined_text: str,
    today_tw: date,
    require_time: bool = True,
) -> list[dict]:
    """Extract multiple explicit date/time events from one message.

    This is intentionally conservative:
    - default: every fragment must have its own date and time.
    - require_time=False: allow date-only fragments and keep time as None,
      used by reminder backfill/event sync workflows where missing time should
      still become a valid midnight reminder/event.

    It is used as a backstop when model extraction persists only the first
    event in a multi-event LINE message.
    """
    if not combined_text or not combined_text.strip():
        return []
    combined_text = _normalize_event_text(combined_text)
    events: list[dict] = []
    first = extract_regex_only(combined_text, today_tw)
    if first.get("has_event"):
        events.append(first)

    matches = list(_DATE_TOKEN.finditer(combined_text))
    fallback_title = _shared_tail_title(combined_text, today_tw)
    shared_years = _shared_years(combined_text)
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(combined_text)
        segment = combined_text[match.start():end]
        ev = _fragment_event(
            segment,
            today_tw,
            combined_text,
            require_time=require_time,
            fallback_title=fallback_title,
            year_hint=shared_years.get(match.start()),
        )
        if ev:
            events.append(ev)

    return _dedupe_events(events)


# ── Schedule lists (2026-10-04) ───────────────────────────────────────────────
# A message whose every line starts with a date is a schedule the family wants
# kept, e.g. a trip: "10/23去海邊一日遊\n24、26 市區自由行\n10/25跟團一日遊".
# Strict on purpose (GP1 C1): one non-schedule line, a range, a fraction, a
# recap word or a question rejects the whole message.

_SCHEDULE_MAX_ITEMS = 6
_SCHEDULE_PAST_WINDOW_DAYS = 120
_SCHEDULE_FUTURE_LIMIT_DAYS = 200
_SCHEDULE_ROLL_GAP_DAYS = 10
_SCHEDULE_TITLE_LIMIT = 40
_SCHEDULE_NUM = r"(?:\d{1,2}|[零〇一二兩三四五六七八九十]{1,3})"
_SCHEDULE_WEEKDAY = (
    r"(?:\s*[（(]\s*(?:星期|週|周|禮拜)?\s*[一二三四五六日天]\s*[）)]"
    r"|\s*(?:星期|週|周|禮拜)[一二三四五六日天])"
)
_SCHEDULE_DAY_SEPARATOR = r"\s*(?:[、,，]|及|和|與)\s*"
# A bare day number followed by a counter is a quantity ("3、4個人"), not a day.
_SCHEDULE_NOT_A_DAY = (
    r"(?!\s*(?:個|人|位|次|天|小時|點|分|歲|元|塊|樓|%|成|折|倍|顆|杯|份|張|本|件|號樓))"
)
_SCHEDULE_DATE_RE = re.compile(
    rf"(?:(?P<year>\d{{4}})\s*(?:年|/|-)\s*)?"
    rf"(?P<month>{_SCHEDULE_NUM})\s*(?P<sep>月|/)\s*"
    rf"(?P<day>{_SCHEDULE_NUM})(?!\d)\s*(?P<suffix>日|號)?"
    rf"(?:{_SCHEDULE_WEEKDAY})?"
)
_SCHEDULE_EXTRA_DAY_RE = re.compile(
    rf"{_SCHEDULE_DAY_SEPARATOR}(?P<day>\d{{1,2}})(?!\d){_SCHEDULE_NOT_A_DAY}"
    rf"\s*(?:日|號)?(?:{_SCHEDULE_WEEKDAY})?"
)
_SCHEDULE_LEADING_DAY_RE = re.compile(
    rf"(?P<day>\d{{1,2}})(?!\d){_SCHEDULE_NOT_A_DAY}\s*(?P<suffix>日|號)?"
    rf"(?:{_SCHEDULE_WEEKDAY})?"
)
_SCHEDULE_RANGE_TAIL_RE = re.compile(
    rf"\s*(?:-|－|—|–|~|～|到|至)\s*(?:\d{{4}}\s*(?:年|/|-)\s*)?"
    rf"(?:{_SCHEDULE_NUM}\s*(?:月|/)\s*)?{_SCHEDULE_NUM}(?!\d)"
)
_SCHEDULE_BODY_DATE_RE = re.compile(rf"{_SCHEDULE_NUM}\s*(?:月|/)\s*{_SCHEDULE_NUM}")
# 1/2 杯, 1/3 的價格: a fraction, not a date (N1 keeps only measure/price cues).
_SCHEDULE_FRACTION_TAIL_RE = re.compile(
    r"\s*(?:杯|匙|碗|瓶|罐|包|顆|片|塊|公克|公斤|克|斤|兩|"
    r"(?:ml|cc|g)(?![a-z])|的?價(?:格|錢)?|折|倍)",
    re.IGNORECASE,
)
_SCHEDULE_REJECT_RE = re.compile(r"上次|昨天|之前|去年|已經|曾經|[?？]|嗎(?!哪)")
_SCHEDULE_ACTIVITY_RE = re.compile(
    r"去|回|到|出發|搭|飛|入住|退房|看醫生|看診|回診|打疫苗|接種|開會|上課|"
    r"聚餐|吃飯|參加|報到|預約|接送|拿|領|買|繳|辦|考試|面試|旅遊|旅行|"
    r"一日遊|自由行|跟團|出國|出遊|行程"
)
# GP1 r2 (2026-10-05): a called-off line still names its trip (不去了,
# 取消去台中, 沒辦法去), and forecasts / prices carry 到 / 回 / 去 too
# (晴到多雲, 漲到32元, 回檔).  One such line rejects the whole message.
_SCHEDULE_CALLED_OFF_RE = re.compile(
    r"取消|作罷|延期|改期|改天|順延|延後|喊停|停辦|停課|停班|算了|免了|"
    r"無法|不能|不行|不克|不便|不方便|不一定|不確定|來不及|趕不上|"
    r"沒辦法|沒空|沒法|沒有要|改(?:成|為)不|別去|甭去|"
    r"(?:去|回|來|到|走|參加|出發)不(?:了|成)|"
    r"(?:不|沒有?)(?:要|用|必|會|想|打算|準備|再)?"
    r"(?:(?:跟|陪|帶|載|和|與|同)[^\s，,。；;!！]{1,4}?)?"
    r"(?:去|回|來|過去|出發|出國|出門|出遊|參加|跟團|赴|報到)"
)
_SCHEDULE_WEATHER_WORD = r"(?:多雲|雷陣雨|陣雨|雷雨|豪雨|大雨|小雨|晴|陰|雨|雪|霧)"
_SCHEDULE_FORECAST_OR_PRICE_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:度|°|℃)|"
    r"天氣|氣溫|溫度|低溫|高溫|體感|濕度|降雨|雨量|紫外線|空氣品質|"
    r"晴天|晴朗|陰天|雨天|多雲|陣雨|雷雨|豪雨|大雨|小雨|下雨|颱風|寒流|冷氣團|"
    r"鋒面|東北季風|梅雨|降溫|回溫|回暖|轉涼|轉冷|下雪|降雪|"
    r"(?<![一-鿿])[晴陰雨](?![一-鿿])|[晴陰雨](?=\s*(?:到|轉|時))|"
    r"(?<=[到轉時有])[晴陰雨](?![一-鿿])|"
    r"油價|股價|房價|金價|價格|價錢|漲價|降價|漲停|跌停|漲幅|跌幅|"
    r"大盤|台股|美股|加權|指數|收盤|開盤|除息|除權|股利|配息|殖利率|匯率|利率|"
    r"升息|降息|回檔|回升|回落|回跌|回彈|回穩|反彈|"
    r"(?:漲|跌|降)(?:了|到|至|破|\s*\d)"
)
# (回)到 before a number is a price, a temperature or a range, and between
# two weather words a forecast: neither counts as the line's activity.
_SCHEDULE_NOT_AN_ACTIVITY_RE = re.compile(
    rf"{_SCHEDULE_WEATHER_WORD}\s*(?:到|轉|時)\s*{_SCHEDULE_WEATHER_WORD}|"
    r"[回去]?到(?=\s*(?:\d|[零〇一二兩三四五六七八九十百千]+\s*(?:度|元|塊|%|成|倍)))"
)
_SCHEDULE_BULLET_RE = re.compile(r"^[-*•・●◆▪]\s*")
_SCHEDULE_TITLE_COMMAND_RE = re.compile(r"^提醒(?:我們|我|大家)?\s*")


def _schedule_int(raw: str | None) -> int | None:
    if not raw:
        return None
    return _parse_chinese_int(raw)


def _resolve_schedule_date(
    year: int | None, month: int, day: int, today_tw: date
) -> date | None:
    """Pick the year for a written date; None when it reads as past or far off."""
    if year is not None:
        target = _validate_date(year, month, day)
        if target is None or target < today_tw:
            return None
    else:
        target = _validate_date(today_tw.year, month, day)
        if target is not None and target < today_tw:
            if (today_tw - target).days <= _SCHEDULE_PAST_WINDOW_DAYS:
                return None  # a recap of something recent, not a plan
            target = _validate_date(today_tw.year + 1, month, day)
        elif target is None:
            target = _validate_date(today_tw.year + 1, month, day)
        if target is None:
            return None
    if (target - today_tw).days > _SCHEDULE_FUTURE_LIMIT_DAYS:
        return None
    return target


def _continue_schedule_day(previous: date, day: int, today_tw: date) -> date | None:
    """A bare day after a dated one keeps its month; a smaller day starts the next."""
    target = _validate_date(previous.year, previous.month, day)
    if target is None or day < previous.day:
        year, month = (
            (previous.year + 1, 1)
            if previous.month == 12
            else (previous.year, previous.month + 1)
        )
        target = _validate_date(year, month, day)
        if target is None or (target - previous).days > _SCHEDULE_ROLL_GAP_DAYS:
            return None
    if target < today_tw or (target - today_tw).days > _SCHEDULE_FUTURE_LIMIT_DAYS:
        return None
    return target


def _schedule_line_dates(
    line: str, previous: date | None, today_tw: date
) -> tuple[list[date], str] | None:
    """Return (dates, rest of the line) or None when the line is not a schedule."""
    dates: list[date] = []
    match = _SCHEDULE_DATE_RE.match(line)
    if match is not None:
        month = _schedule_int(match.group("month"))
        day = _schedule_int(match.group("day"))
        if month is None or day is None:
            return None
        plain_slash = (
            match.group("sep") == "/"
            and not match.group("year")
            and not match.group("suffix")
        )
        if plain_slash and _SCHEDULE_FRACTION_TAIL_RE.match(line, match.end()):
            return None
        year = int(match.group("year")) if match.group("year") else None
        first = _resolve_schedule_date(year, month, day, today_tw)
        if first is None:
            return None
        dates.append(first)
        position = match.end()
    else:
        if previous is None:
            return None  # 「24、26 X」 needs a dated line above it
        match = _SCHEDULE_LEADING_DAY_RE.match(line)
        if match is None:
            return None
        has_more = _SCHEDULE_EXTRA_DAY_RE.match(line, match.end()) is not None
        if not has_more and not match.group("suffix"):
            return None  # a lone number is not a day
        first = _continue_schedule_day(previous, int(match.group("day")), today_tw)
        if first is None:
            return None
        dates.append(first)
        position = match.end()
    while True:
        extra = _SCHEDULE_EXTRA_DAY_RE.match(line, position)
        if extra is None:
            break
        following = _continue_schedule_day(dates[-1], int(extra.group("day")), today_tw)
        if following is None:
            return None
        dates.append(following)
        position = extra.end()
    rest = line[position:]
    if _SCHEDULE_RANGE_TAIL_RE.match(rest):
        return None  # 10/23-10/26: a range belongs to the range parser
    return dates, rest


def _schedule_line_is_plan(body: str) -> bool:
    """A dated line's text names an activity that is still going ahead."""
    if _SCHEDULE_CALLED_OFF_RE.search(body):
        return False
    if _SCHEDULE_FORECAST_OR_PRICE_RE.search(body):
        return False
    return bool(_SCHEDULE_ACTIVITY_RE.search(_SCHEDULE_NOT_AN_ACTIVITY_RE.sub(" ", body)))


def _schedule_line_time(body: str) -> tuple[str | None, str | None] | None:
    """(clock, daypart) of one line; None when its time is malformed."""
    if _has_malformed_compact_range(body):
        return None
    if _TIME_RANGE_IN_TEXT.search(body) or _TIME_IN_TEXT.search(body):
        clock = _first_time_in_text(body)
        return (clock, None) if clock else None
    daypart = _DAYPART_IN_TEXT.search(body)
    return None, daypart.group(0) if daypart else None


def extract_schedule_lines(text: str, today_tw: date) -> list[dict]:
    """Parse a message whose every non-empty line starts with a date.

    Returns ``[{date, title, time, daypart}]`` sorted by date, or ``[]`` when
    any line is not a dated activity, is called off or negated (不去了,
    取消…, 沒辦法去), is a forecast, price or market line (晴到多雲,
    漲到32元), or is a range, any date reads as recent past or more than 200
    days ahead, the message reminisces or asks, or it lists more than six
    items.  ``time`` is an explicit clock ("HH:MM") and ``daypart`` a stated
    daypart; both None means no time was given.
    A line that starts with only day numbers (「24、26 X」) continues the month
    of the line above; a smaller day moves to the next month.
    """
    if not text or not str(text).strip():
        return []
    lines = [
        unicodedata.normalize("NFKC", raw).strip()
        for raw in str(text).splitlines()
    ]
    lines = [_SCHEDULE_BULLET_RE.sub("", line, count=1) for line in lines if line]
    if not lines or _SCHEDULE_REJECT_RE.search("\n".join(lines)):
        return []
    items: list[dict] = []
    previous: date | None = None
    for line in lines:
        parsed = _schedule_line_dates(line, previous, today_tw)
        if parsed is None:
            return []
        dates, rest = parsed
        body = re.sub(r"^[\s:：,，、。\-–—~～]+", "", rest).strip()
        if not body or _SCHEDULE_BODY_DATE_RE.search(body):
            return []
        if not _schedule_line_is_plan(body):
            return []
        line_time = _schedule_line_time(body)
        if line_time is None:
            return []
        clock, daypart = line_time
        title = _strip_fragment_time_tokens(body)
        title = _SCHEDULE_TITLE_COMMAND_RE.sub("", re.sub(r"\s+", " ", title).strip())
        title = re.sub(r"[\x00-\x1f\x7f]", "", title).strip(" ，,。；;、:：-")
        if len(re.findall(r"[一-鿿A-Za-z0-9]", title)) < 2:
            return []
        title = title[:_SCHEDULE_TITLE_LIMIT]
        for target in dates:
            item = {
                "date": target.isoformat(),
                "title": title,
                "time": clock,
                "daypart": daypart,
            }
            if item not in items:
                items.append(item)
        previous = dates[-1]
    if len(items) > _SCHEDULE_MAX_ITEMS:
        return []
    return sorted(items, key=lambda item: (item["date"], item["time"] or ""))


_CONTEXTUAL_APPOINTMENT_PAIR_RE = re.compile(
    r"^\s*我?\s*"
    r"(?P<month>\d{1,2})月(?P<day1>\d{1,2})(?:日|號)"
    r"(?P<body1>[^，,。；;\n]{2,100})"
    r"\s*[，,；;]\s*"
    r"(?P<day2>\d{1,2})(?:日|號)"
    r"(?P<body2>[^，,。；;\n]{2,100})"
    r"\s*[。！!]?\s*$"
)


def extract_contextual_appointment_pair(
    text: str,
    source_date: date,
) -> list[dict]:
    """Parse one narrow two-appointment source for a later reminder command.

    The second date may inherit the first date's month, but no month/year
    rollover is guessed.  This helper is deliberately not wired into the
    general calendar extractor: it exists only for a same-sender, immediately
    following command that independently proves both lead dates.
    """

    normalized = _normalize_event_text(text or "")
    match = _CONTEXTUAL_APPOINTMENT_PAIR_RE.fullmatch(normalized)
    if match is None:
        return []
    try:
        month = int(match.group("month"))
        first = date(source_date.year, month, int(match.group("day1")))
        if first < source_date:
            first = date(source_date.year + 1, month, int(match.group("day1")))
        second = date(first.year, month, int(match.group("day2")))
    except ValueError:
        return []
    if second < first:
        return []

    parsed: list[dict] = []
    for target, group_name in ((first, "body1"), (second, "body2")):
        body = str(match.group(group_name) or "").strip()
        if _has_malformed_compact_range(body):
            return []
        event_time = _first_time_in_text(body)
        title = _sanitize_title(_strip_fragment_time_tokens(body))
        event_type = _classify_type(f"{title} {body}")
        if event_type is None and re.search(
            r"(?:牙科|植牙|殖牙|回診|拆(?:手術)?線)",
            f"{title} {body}",
        ):
            event_type = "medical"
        if (
            not title
            or event_type != "medical"
            or reminder_intent.should_reject_reminder_candidate(normalized, title)
        ):
            return []
        parsed.append(
            {
                "date": target.isoformat(),
                "time": event_time,
                "title": title,
                "event_type": event_type,
            }
        )
    return parsed
