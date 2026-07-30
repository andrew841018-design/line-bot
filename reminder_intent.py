"""Deterministic reminder intent and semantic-title policy."""

from __future__ import annotations

import re
import unicodedata


_INTERNAL_PROMPT_MARKERS = (
    "--- 原始訊息 結束 ---",
    "原始訊息 結束",
    "下面是使用者",
    "使用者目標",
)
_NEGATED_CREATE_RE = re.compile(
    r"(?:不要|不用|不必|別)\s*(?:幫我)?\s*"
    r"(?:提醒|新增提醒|建立提醒|加(?:入)?提醒|記(?:下|住))"
)
_EXPLICIT_CREATE_RE = re.compile(
    r"(?:"
    r"(?:請|麻煩|可以|能不能)?\s*幫我\s*(?:提醒|新增提醒|建立提醒|加(?:入)?提醒|記(?:下|住))|"
    r"(?:請|麻煩)\s*提醒我|"
    r"(?:新增|建立|加入)\s*(?:一個|這個|這則)?\s*提醒|"
    r"提醒事項\s*(?:=>|＝>|:|：)"
    r")"
)
_QUESTION_RE = re.compile(
    r"(?:"
    r"要不要|可不可以|能不能|有沒有|有空(?:嗎|呢|[?？])?|"
    r"方不方便|幾點|幾時|何時|什麼時候|"
    r"(?<!嗎)哪(?:一)?(?:家|間|個|天|裡|邊)?|"
    r"是否|嗎(?!哪)|呢(?:[?？！!。]|$)|[?？]"
    r")"
)
_AVAILABILITY_ONLY_RE = re.compile(
    r"(?:也)?(?:可以|有空|方便)\s*(?:啊|呀|喔|哦|的|啦|！|!|。)?\s*$"
)
_DATE_HINT_RE = re.compile(
    r"\d{1,2}/\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*[日號]?|"
    r"今天|明天|後天|大後天|"
    r"(?:星期|週|周|禮拜)[一二三四五六日天]"
)
_TIME_HINT_RE = re.compile(
    r"(?:[01]?\d|2[0-3])[:：][0-5]\d|"
    r"(?<!\d)(?:[01]\d|2[0-3])[0-5]\d(?!\d)|"
    r"(?:\d{1,2}|[一二兩三四五六七八九十]+)\s*點|"
    r"早上|上午|中午|下午|傍晚|晚上"
)
_WEAK_ACTION_RE = re.compile(
    r"(?:"
    r"\d{1,2}/\d{1,2}|"
    r"\d{1,2}\s*月\s*\d{1,2}\s*[日號]?|"
    r"今天|明天|後天|大後天|"
    r"早上|上午|中午|下午|傍晚|晚上|"
    r"晚上嗎|早上嗎|下午嗎|要不要"
    r")"
)
_CLAUSE_SPLIT_RE = re.compile(r"[\n\r。；;！!?？]+")
_QUESTION_BOUNDARY_RE = re.compile(r"嗎(?!哪)|[?？]")
_PUNCT_OR_SPACE_RE = re.compile(r"[\s，,、。；;：:！？!?（）()\[\]【】「」『』]+")
_TIME_RANGE_RE = re.compile(
    r"(?:[01]?\d|2[0-3])(?:[:：]?[0-5]\d)?\s*"
    r"(?:-|－|—|–|~|～|到|至)\s*"
    r"(?:[01]?\d|2[0-3])(?:[:：]?[0-5]\d)?"
)
_CLOCK_RE = re.compile(
    r"(?:[01]?\d|2[0-3])[:：][0-5]\d|"
    r"(?<!\d)(?:[01]\d|2[0-3])[0-5]\d(?!\d)"
)
_DAYPART_RE = re.compile(r"早上|上午|中午|下午|傍晚|晚上|凌晨|半夜")
_DAYPART_DEFAULTS = {
    "早上": "09:00",
    "上午": "09:00",
    "中午": "12:00",
    "下午": "15:00",
    "傍晚": "18:00",
    "晚上": "19:00",
}


def normalize_text(text: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = re.sub(r"[\x00-\x1f\x7f]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _normalize_source(text: object) -> str:
    raw = unicodedata.normalize("NFKC", str(text or ""))
    raw = re.sub(r"[\r\n]+", "。", raw)
    return normalize_text(raw)


def has_internal_prompt_artifact(text: object) -> bool:
    normalized = normalize_text(text)
    return any(marker in normalized for marker in _INTERNAL_PROMPT_MARKERS)


def is_weak_reminder_action(action: object) -> bool:
    normalized = _PUNCT_OR_SPACE_RE.sub("", normalize_text(action))
    return not normalized or bool(_WEAK_ACTION_RE.fullmatch(normalized))


def _action_key(action: object) -> str:
    normalized = normalize_text(action)
    normalized = _TIME_RANGE_RE.sub("", normalized)
    normalized = _CLOCK_RE.sub("", normalized)
    normalized = _DAYPART_RE.sub("", normalized)
    return _PUNCT_OR_SPACE_RE.sub("", normalized)


def _has_independent_committed_fragment(source: str, action: str) -> bool:
    key = _action_key(action)
    if len(key) < 2:
        return False

    fragments = [part.strip() for part in _CLAUSE_SPLIT_RE.split(source) if part.strip()]
    question_boundaries = list(_QUESTION_BOUNDARY_RE.finditer(source))
    if question_boundaries:
        suffix = source[question_boundaries[-1].end() :].strip()
        if suffix:
            fragments.append(suffix)

    for fragment in fragments:
        if _QUESTION_RE.search(fragment):
            continue
        fragment_key = _PUNCT_OR_SPACE_RE.sub("", normalize_text(fragment))
        if key not in fragment_key:
            continue
        if _TIME_HINT_RE.search(fragment):
            return True
        if _DATE_HINT_RE.search(fragment) and len(key) >= 2:
            return True
    return False


def should_reject_reminder_candidate(source_text: object, action: object) -> bool:
    """Reject questions/availability without suppressing explicit reminder requests."""

    source = _normalize_source(source_text)
    candidate = normalize_text(action)
    if has_internal_prompt_artifact(source) or has_internal_prompt_artifact(candidate):
        return True
    if _NEGATED_CREATE_RE.search(source):
        return True
    if _EXPLICIT_CREATE_RE.search(source):
        return False
    if is_weak_reminder_action(candidate):
        return True
    if _QUESTION_RE.search(candidate):
        return True
    if _has_independent_committed_fragment(source, candidate):
        return False
    if _QUESTION_RE.search(source) or _AVAILABILITY_ONLY_RE.search(source):
        return True
    return False


def is_obvious_noncommittal_source(source_text: object) -> bool:
    """Cheap pre-model gate for messages that cannot contain a committed event."""

    source = _normalize_source(source_text)
    if not source or has_internal_prompt_artifact(source):
        return True
    if _NEGATED_CREATE_RE.search(source):
        return True
    if _EXPLICIT_CREATE_RE.search(source):
        return False
    if _AVAILABILITY_ONLY_RE.search(source):
        return True

    if not _QUESTION_RE.search(source):
        return False
    question_boundaries = list(_QUESTION_BOUNDARY_RE.finditer(source))
    if question_boundaries:
        suffix = source[question_boundaries[-1].end() :].strip()
        if suffix and _TIME_HINT_RE.search(suffix) and len(_action_key(suffix)) >= 3:
            return False
    for fragment in _CLAUSE_SPLIT_RE.split(source):
        if (
            fragment
            and not _QUESTION_RE.search(fragment)
            and _DATE_HINT_RE.search(fragment)
            and _TIME_HINT_RE.search(fragment)
            and len(_action_key(fragment)) >= 2
        ):
            return False
    return True


def event_semantic_key(title: object) -> str:
    """Conservative exact key for known duplicate calendar renderings."""

    normalized = normalize_text(title)
    if has_internal_prompt_artifact(normalized):
        return ""
    normalized = re.sub(r"[（(]\s*" + _TIME_RANGE_RE.pattern + r"\s*[）)]", "", normalized)
    normalized = _TIME_RANGE_RE.sub("", normalized)
    normalized = _CLOCK_RE.sub("", normalized)
    normalized = _DAYPART_RE.sub("", normalized)
    normalized = normalized.replace("打羽球", "羽球").replace("打壁球", "壁球")
    normalized = re.sub(r"(?:活動|行程)$", "", normalized)
    return _PUNCT_OR_SPACE_RE.sub("", normalized).casefold()


def event_titles_are_semantically_compatible(
    left_title: object,
    right_title: object,
) -> bool:
    left_raw = re.sub(r"\s+", " ", str(left_title or "")).strip()
    right_raw = re.sub(r"\s+", " ", str(right_title or "")).strip()
    if not left_raw or not right_raw:
        return False
    if left_raw == right_raw:
        return True
    known_variant = bool(
        _DAYPART_RE.search(left_raw)
        or _DAYPART_RE.search(right_raw)
        or _TIME_RANGE_RE.search(left_raw)
        or _TIME_RANGE_RE.search(right_raw)
        or re.search(r"打(?:羽球|壁球)", left_raw)
        or re.search(r"打(?:羽球|壁球)", right_raw)
    )
    return known_variant and event_semantic_key(left_raw) == event_semantic_key(
        right_raw
    )


def event_dayparts(title: object) -> set[str]:
    return set(_DAYPART_RE.findall(normalize_text(title)))


def schedules_are_compatible(
    left_time: object,
    left_title: object,
    right_time: object,
    right_title: object,
) -> bool:
    left = normalize_text(left_time)
    right = normalize_text(right_time)
    if left and right:
        return left == right
    if not left and not right:
        return True

    missing_title = left_title if not left else right_title
    explicit_time = right if not left else left
    dayparts = event_dayparts(missing_title)
    if len(dayparts) != 1:
        return False
    return _DAYPART_DEFAULTS.get(next(iter(dayparts))) == explicit_time
