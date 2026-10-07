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
_CLAUSE_SPLIT_RE = re.compile(r"[\n\r，,。；;！!?？]+")
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
_REMINDER_DAYPART_RE = re.compile(
    r"明晚|今晚|凌晨|半夜|早上|上午|中午|下午|傍晚|晚上"
)
_REMINDER_EXPLICIT_CLOCK_RE = re.compile(
    r"(?<!\d)(?:[01]?\d|2[0-3])[:：][0-5]\d(?!\d)|"
    r"(?<!\d)(?:\d{1,2}|[零〇一二兩三四五六七八九十]{1,3})\s*點"
    r"(?:\s*(?:半|\d{1,2}\s*分?))?"
)
_REMINDER_DEFAULTS = {
    "凌晨": (0, 0, "daypart:凌晨"),
    "半夜": (0, 0, "daypart:半夜"),
    "早上": (9, 0, "morning"),
    "上午": (9, 0, "morning"),
    "中午": (12, 0, "daypart:中午"),
    "下午": (15, 0, "daypart:下午"),
    "傍晚": (18, 0, "daypart:傍晚"),
    "晚上": (19, 0, "evening"),
    "今晚": (19, 0, "evening"),
    "明晚": (19, 0, "evening"),
}


def normalize_text(text: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = re.sub(r"[\x00-\x1f\x7f]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def reminder_default_time(
    daypart: str | None = None,
) -> tuple[int, int, str] | None:
    """Return the single source of truth for a reminder with no clock.

    A completely unspecified daypart is noon.  A named daypart keeps the
    established deterministic defaults, including Andrew's explicit morning
    and evening policy.  Unknown dayparts fail closed.
    """

    if daypart is None:
        return (12, 0, "no_daypart")
    return _REMINDER_DEFAULTS.get(normalize_text(daypart))


def has_explicit_reminder_clock(source_text: object) -> bool:
    """Return whether the source contains an unambiguous colon/point clock."""

    return bool(_REMINDER_EXPLICIT_CLOCK_RE.search(normalize_text(source_text)))


def resolve_reminder_default_time(
    source_text: object,
) -> tuple[int, int, str] | None:
    """Resolve a missing-clock default, or ``None`` when no default is safe.

    Explicit clocks are never overridden. Multiple distinct dayparts are
    ambiguous and remain fail-closed instead of letting a model choose.
    """

    source = normalize_text(source_text)
    if has_explicit_reminder_clock(source):
        return None
    dayparts = list(dict.fromkeys(_REMINDER_DAYPART_RE.findall(source)))
    if len(dayparts) > 1:
        return None
    return reminder_default_time(dayparts[0] if dayparts else None)


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
            and not _AVAILABILITY_ONLY_RE.search(fragment)
            and not is_weak_reminder_action(fragment)
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


# ── Same event, mentioned again ──────────────────────────────────────────────
# 2026-09-28 Andrew：同一事件反覆提到時不要重複新增提醒，把新的細節（含後來才
# 補上的時間）併進原本那一筆。Pure and deterministic; any doubt means "not the
# same event": a wrong merge can make the family miss an appointment, while a
# duplicate only adds noise.

_SAME_EVENT_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_SAME_EVENT_DATE_RE = re.compile(
    r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*[日號]?|\d{1,2}\s*[/／]\s*\d{1,2}|"
    r"\d{1,2}\s*月\s*\d{1,2}\s*[日號]?|(?:星期|週|周|禮拜)[一二三四五六日天]|"
    r"今天|今晚|明天|明晚|明日|大後天|後天|下下週|下週|下周|這週|本週|下個月|月底|月初"
)
_SAME_EVENT_CLOCK_RE = re.compile(
    r"(?:[01]?\d|2[0-3])\s*[:：]\s*[0-5]\d|"
    r"(?:\d{1,2}|[零〇一二兩三四五六七八九十]{1,3})\s*點(?:\s*(?:半|\d{1,2}\s*分?))?"
)
_SAME_EVENT_OFFSET_RE = re.compile(
    r"[（(]?\s*(?:前一天|前\d+天|前[一二三四五六七]天|當天|提前)\s*提醒\s*[）)]?"
)
_SAME_EVENT_NUMBER = r"(?:\d{1,2}|[一二兩三四五六七八九十]{1,3})"
_SAME_EVENT_QUALIFIER_RE = re.compile(
    r"第\s*" + _SAME_EVENT_NUMBER + r"\s*(?:劑|次|場|堂|期|梯|輪|回)|"
    + _SAME_EVENT_NUMBER
    + r"\s*劑|初診|複診|上午場|下午場|晚場|早場"
)
_SAME_EVENT_FILLER_RE = re.compile(
    r"提醒我|提醒|記得|幫我|麻煩|謝謝|一下|一起|我們|大家|要去|要回|參加|出席|前往|"
    r"辦理|手續|施打|接種|進行|"
    r"的|和|跟|與|及|我|去|要|在"
)
# Family members as the code already names them as actors (main.py
# _LOCAL_REMINDER_POSSESSIVE_SUBJECT_RE) plus common kinship terms.
_SAME_EVENT_KIN = (
    "爸爸", "媽媽", "阿公", "阿嬤", "外公", "外婆", "爺爺", "奶奶", "哥哥", "姊姊",
    "姐姐", "弟弟", "妹妹", "老公", "老婆", "兒子", "女兒", "小阿姨", "大阿姨", "阿姨",
    "叔叔", "伯伯", "伯父", "伯母", "姑姑", "姑丈", "嬸嬸", "舅舅", "舅媽", "姨丈",
    "表哥", "表姊", "表姐", "表弟", "表妹", "堂哥", "堂姊", "堂姐", "堂弟", "堂妹",
    "外甥", "姪子", "姪女", "孫子", "孫女", "媳婦", "女婿", "岳父", "岳母", "公公",
    "婆婆", "乾爹", "乾媽",
)
_SAME_EVENT_SURNAMES = (
    "王李張劉陳楊黃趙吳周徐孫馬朱胡郭何林羅高鄭梁謝宋唐許韓馮鄧曹彭曾蕭田董袁潘蔣"
    "蔡余杜葉程蘇魏呂丁任沈姚盧姜崔鍾譚陸汪范金石廖賈夏韋付方白鄒孟熊秦邱江尹薛閻"
    "段雷侯龍史陶黎賀顧毛郝龔邵萬錢嚴覃武戴莫孔向湯"
)
# A name is a surname plus one or two characters right before a personal event.
_SAME_EVENT_NAME_RE = re.compile(
    rf"(?<![一-鿿])[{_SAME_EVENT_SURNAMES}][一-鿿]{{1,2}}"
    r"(?=結婚|喜宴|婚禮|生日|告別式|滿月|畢業|的)"
)
_SAME_EVENT_ROLE_VERBS = ("接送", "陪", "帶", "載", "接", "送")
_SAME_EVENT_PLACE_SUFFIX = (
    "醫院|診所|學校|公司|餐廳|教會|車站|機場|公園|銀行|郵局|超商|市場|大樓|國小|國中|高中|大學"
)
# A place starts a sentence or follows 在/到/去/回/於/往 or punctuation, so it
# never swallows the activity in front of it (「家族聚餐在合成餐廳」).
_SAME_EVENT_PLACE_RE = re.compile(
    rf"(?:^|(?<=[在到去回於往\s，,、。]))[一-鿿]{{2,4}}(?:{_SAME_EVENT_PLACE_SUFFIX})"
)
# 在/到/去/回/於/往 in front of a place or road is grammar, not part of its name.
_SAME_EVENT_PREPOSITION = r"(?:前往|在|到|去|回|於|往)"
_SAME_EVENT_ADDRESS_NUMBER = r"(?:\d{1,4}|[一二三四五六七八九十]{1,4})"
_SAME_EVENT_HOUSE_NUMBER = (
    rf"{_SAME_EVENT_ADDRESS_NUMBER}(?:\s*之\s*{_SAME_EVENT_ADDRESS_NUMBER})?"
)
# A road name is its last two characters plus a direction (中山北路、忠孝東路);
# longer names (羅斯福路) keep their last two on both sides, so a preposition
# or an activity in front (「簽租約中山路1號」) is never taken into the name.
_SAME_EVENT_ROAD_ADDRESS = (
    r"(?P<name>[一-鿿]{1,2}[東西南北]?)(?P<type>大道|路|街)"
    rf"(?:\s*(?P<section>{_SAME_EVENT_ADDRESS_NUMBER})\s*段)?"
    rf"(?:\s*(?P<lane>{_SAME_EVENT_ADDRESS_NUMBER})\s*巷)?"
    rf"(?:\s*(?P<alley>{_SAME_EVENT_ADDRESS_NUMBER})\s*弄)?"
    rf"(?:\s*(?P<number>{_SAME_EVENT_HOUSE_NUMBER})\s*號)?"
    rf"(?:\s*(?P<floor>{_SAME_EVENT_ADDRESS_NUMBER})\s*樓)?"
)
# 10巷1號 without a road name (a lane or an alley is required; 「8號」 is a date).
_SAME_EVENT_LANE_ADDRESS = (
    rf"(?:(?P<lane>{_SAME_EVENT_ADDRESS_NUMBER})\s*巷)?"
    rf"(?:\s*(?P<alley>{_SAME_EVENT_ADDRESS_NUMBER})\s*弄)?"
    rf"(?:\s*(?P<number>{_SAME_EVENT_HOUSE_NUMBER})\s*號)?"
    rf"(?:\s*(?P<floor>{_SAME_EVENT_ADDRESS_NUMBER})\s*樓)?"
)
_SAME_EVENT_ROAD_ADDRESS_RE = re.compile(_SAME_EVENT_ROAD_ADDRESS)
_SAME_EVENT_LANE_ADDRESS_RE = re.compile(_SAME_EVENT_LANE_ADDRESS)
# The core also loses the preposition in front (「到中山路1號簽租約」 → 簽租約).
_SAME_EVENT_ROAD_ADDRESS_STRIP_RE = re.compile(
    rf"{_SAME_EVENT_PREPOSITION}?{_SAME_EVENT_ROAD_ADDRESS}"
)
_SAME_EVENT_LANE_ADDRESS_STRIP_RE = re.compile(
    rf"{_SAME_EVENT_PREPOSITION}?{_SAME_EVENT_LANE_ADDRESS}"
)
_SAME_EVENT_ADDRESS_CHAIN = ("section", "lane", "alley", "number")
_SAME_EVENT_ADDRESS_PARTS = (*_SAME_EVENT_ADDRESS_CHAIN, "floor")
# Words ending in 路/街 that are not a road when nothing like a number follows
# (「家裡網路繳費」, 「修理網路設備」, 「高速公路」).
_SAME_EVENT_NOT_ROADS = frozenset({
    "網路", "走路", "馬路", "迷路", "順路", "繞路", "問路", "帶路", "趕路",
    "半路", "沿路", "鐵路", "公路", "道路", "線路", "電路", "思路", "出路", "退路",
    "水路", "陸路", "逛街", "上街", "過街", "掃街",
})
# Parts of an event that are events of their own (a rehearsal is not the ceremony).
_SAME_EVENT_SUB_EVENTS = (
    "彩排", "說明會", "報名", "繳費", "截止", "報告", "預演", "試穿", "領取", "訂位",
    "預約", "取消", "改期",
)
_SAME_EVENT_TRAVEL_RE = re.compile(
    r"(?:回|去|到|前往|搭|坐)?(?:台北|新北|桃園|新竹|台中|台南|高雄|基隆|宜蘭|花蓮|"
    r"台東|屏東|嘉義|彰化|雲林|南投|苗栗|老家|家)|^(?:回|去|到|搭|坐)$"
)
_SAME_EVENT_KEEP_RE = re.compile(r"[^一-鿿0-9a-z]+")
_SAME_EVENT_RELATIONAL_RE = re.compile(r"^(?:前|之前|以前|後|之後|以後)")
_SAME_EVENT_DAYPART_WINDOWS = {
    "凌晨": (0, 6), "半夜": (0, 6),
    "早上": (5, 12), "上午": (5, 12),
    "中午": (11, 14),
    "下午": (13, 18),
    "傍晚": (16, 20),
    "晚上": (17, 24), "今晚": (17, 24), "明晚": (17, 24),
}


def _same_event_people_pattern(names: tuple[str, ...]) -> str:
    people = sorted(set(_SAME_EVENT_KIN) | {n for n in names if n}, key=len, reverse=True)
    return "|".join(re.escape(person) for person in people)


def same_event_people(text: object, names: tuple[str, ...] = ()) -> set[str]:
    """Family members, configured aliases and personal names in ``text``."""

    value = normalize_text(text)
    found: set[str] = set()
    for person in sorted(set(_SAME_EVENT_KIN) | {n for n in names if n}, key=len, reverse=True):
        if person in value:
            found.add(person)
            value = value.replace(person, " ")
    found.update(_SAME_EVENT_NAME_RE.findall(normalize_text(text)))
    return found


def _same_event_role_signature(text: object, names: tuple[str, ...]) -> tuple[str, ...] | None:
    value = normalize_text(text)
    people = _same_event_people_pattern(names)
    verbs = "|".join(_SAME_EVENT_ROLE_VERBS)
    if not re.search(rf"(?:{verbs})\s*(?:{people})", value):
        return None
    tokens = re.findall(rf"(?:{verbs})\s*(?:{people})|(?:{people})", value)
    return tuple(re.sub(r"\s+", "", token) for token in tokens)


_CHINESE_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}


def _canonical_number(token: str) -> str:
    """「第一劑」and「第1劑」are the same dose; 十一/二十 as well."""

    token = token.strip()
    if token.isdigit():
        return str(int(token))
    if "十" in token:
        tens, _, ones = token.partition("十")
        return str(
            (_CHINESE_DIGITS.get(tens, 1) if tens else 1) * 10
            + (_CHINESE_DIGITS.get(ones, 0) if ones else 0)
        )
    if token in _CHINESE_DIGITS:
        return str(_CHINESE_DIGITS[token])
    return token


def _same_event_qualifiers(text: object) -> set[str]:
    return {
        re.sub(_SAME_EVENT_NUMBER, lambda m: _canonical_number(m.group()), re.sub(r"\s+", "", value))
        for value in _SAME_EVENT_QUALIFIER_RE.findall(normalize_text(text))
    }


def _same_event_places(text: object) -> set[str]:
    """Named places without the preposition in front (在台大醫院 = 去台大醫院)."""

    return {
        re.sub(rf"^{_SAME_EVENT_PREPOSITION}+", "", place)
        for place in _SAME_EVENT_PLACE_RE.findall(normalize_text(text))
    }


def _same_place(left: str, right: str) -> bool:
    """A place captured with words in front (媽媽台大醫院) is still that place."""

    return left.endswith(right) or right.endswith(left)


def _canonical_house_number(token: str | None) -> str | None:
    if token is None:
        return None
    return "之".join(_canonical_number(part) for part in re.split(r"\s*之\s*", token.strip()))


def _road_name(match: re.Match) -> str:
    """The road's name without a preposition read into it (「去逛街」 is 逛街)."""

    name = match.group("name")
    return name[1:] if len(name) >= 2 and name[0] in "在到去回於往" else name


def _real_road_address(match: re.Match) -> bool:
    """網路、走路、逛街 end in 路/街 without being an address."""

    if any(match.group(part) for part in _SAME_EVENT_ADDRESS_PARTS):
        return True
    name = _road_name(match)
    return len(name) >= 2 and name[-1] + match.group("type") not in _SAME_EVENT_NOT_ROADS


def _same_event_addresses(text: object) -> list[tuple[str | None, ...]]:
    """(road name, road type, section, lane, alley, number, floor) of each address."""

    value = normalize_text(text)
    found: list[tuple[str | None, ...]] = []
    for match in _SAME_EVENT_ROAD_ADDRESS_RE.finditer(value):
        if _real_road_address(match):
            found.append(
                (_road_name(match), match.group("type"))
                + tuple(_canonical_house_number(match.group(p)) for p in _SAME_EVENT_ADDRESS_PARTS)
            )
    rest = _SAME_EVENT_ROAD_ADDRESS_RE.sub(lambda m: " " * len(m.group()), value)
    for match in _SAME_EVENT_LANE_ADDRESS_RE.finditer(rest):
        if match.group("lane") or match.group("alley"):
            found.append(
                (None, None, None)
                + tuple(
                    _canonical_house_number(match.group(p))
                    for p in ("lane", "alley", "number", "floor")
                )
            )
    return found


def _same_address(left: tuple[str | None, ...], right: tuple[str | None, ...]) -> bool:
    """Same road and type, and the same section/lane/alley/number as far as
    both go; a part only one side names is fine only where the other stops
    (中山路10巷 = 中山路10巷1號, but 中山路1號 ≠ 中山路一段1號).  Two floors
    must agree (3樓 ≠ 5樓); a floor only one side names is fine."""

    if left[0] and right[0] and left[:2] != right[:2]:
        return False
    if left[6] and right[6] and left[6] != right[6]:
        return False
    chain_left, chain_right = left[2:6], right[2:6]

    def depth(parts: tuple[str | None, ...]) -> int:
        return max((i for i, part in enumerate(parts) if part), default=-1)

    shared = min(depth(chain_left), depth(chain_right)) + 1
    return chain_left[:shared] == chain_right[:shared]


def _same_event_sub_events(text: object) -> set[str]:
    value = normalize_text(text)
    return {word for word in _SAME_EVENT_SUB_EVENTS if word in value}


def same_event_core(text: object, names: tuple[str, ...] = ()) -> str:
    """The activity words of a reminder, without dates, times, people and places."""

    value = normalize_text(text).casefold()
    for pattern in (
        _SAME_EVENT_URL_RE,
        _SAME_EVENT_OFFSET_RE,
        _SAME_EVENT_DATE_RE,
        _SAME_EVENT_CLOCK_RE,
        _REMINDER_DAYPART_RE,
        _SAME_EVENT_QUALIFIER_RE,
    ):
        value = pattern.sub(" ", value)
    people = _same_event_people_pattern(names)
    value = re.sub(rf"(?:{'|'.join(_SAME_EVENT_ROLE_VERBS)})\s*(?:{people})", " ", value)
    for person in sorted(set(_SAME_EVENT_KIN) | {n for n in names if n}, key=len, reverse=True):
        value = value.replace(person.casefold(), " ")
    value = _SAME_EVENT_NAME_RE.sub(" ", value)
    value = _SAME_EVENT_PLACE_RE.sub(" ", value)
    value = _SAME_EVENT_ROAD_ADDRESS_STRIP_RE.sub(
        lambda match: " " if _real_road_address(match) else match.group(), value
    )
    value = _SAME_EVENT_LANE_ADDRESS_STRIP_RE.sub(
        lambda match: " " if match.group("lane") or match.group("alley") else match.group(),
        value,
    )
    value = _SAME_EVENT_FILLER_RE.sub(" ", value)
    value = re.sub(r"[-－—–~～]", " ", value)
    return _SAME_EVENT_KEEP_RE.sub("", value)


def _longest_common_run(left: str, right: str) -> tuple[int, int, int]:
    """Length and end offsets (in left, right) of the longest common substring."""

    best = (0, 0, 0)
    previous = [0] * (len(right) + 1)
    for i in range(1, len(left) + 1):
        current = [0] * (len(right) + 1)
        for j in range(1, len(right) + 1):
            if left[i - 1] == right[j - 1]:
                current[j] = previous[j - 1] + 1
                if current[j] > best[0]:
                    best = (current[j], i, j)
        previous = current
    return best


def same_event_identity_conflict(
    incoming: object,
    identity: list[object] | tuple[object, ...],
    names: tuple[str, ...] = (),
) -> bool:
    """Whether ``incoming`` contradicts an event described by ``identity``.

    ``identity`` is the kept reminder's action plus every description it has
    absorbed, so an absorbed vague phrase cannot bridge 第一劑 to 第二劑, and
    an absorbed 「信用卡繳費台新」 keeps 「信用卡繳費國泰」 out.
    """

    known_qualifiers: set[str] = set()
    known_roles: set[tuple[str, ...]] = set()
    known_people: set[str] = set()
    known_places: set[str] = set()
    known_addresses: list[list[tuple[str | None, ...]]] = []
    known_sub_events: set[str] = set()
    for text in identity:
        known_qualifiers |= _same_event_qualifiers(text)
        role = _same_event_role_signature(text, names)
        if role is not None:
            known_roles.add(role)
        known_people |= same_event_people(text, names)
        known_places |= _same_event_places(text)
        text_addresses = _same_event_addresses(text)
        if text_addresses:
            known_addresses.append(text_addresses)
        known_sub_events |= _same_event_sub_events(text)
    qualifiers = _same_event_qualifiers(incoming)
    if qualifiers and known_qualifiers and qualifiers != known_qualifiers:
        return True
    role = _same_event_role_signature(incoming, names)
    if ({role} if role is not None else set()) != known_roles:
        return True
    people = same_event_people(incoming, names)
    if people and known_people and not (people <= known_people or known_people <= people):
        return True
    places = _same_event_places(incoming)
    if places and known_places and not any(
        _same_place(place, known) for place in places for known in known_places
    ):
        return True
    # each description that names an address must name one compatible with
    # the new one: an absorbed 中山路1號 keeps 中山路2號 out even when the
    # title only says 中山路
    addresses = _same_event_addresses(incoming)
    if addresses and any(
        not any(_same_address(address, known) for address in addresses for known in text_addresses)
        for text_addresses in known_addresses
    ):
        return True
    if _same_event_sub_events(incoming) != known_sub_events:
        return True
    core = same_event_core(incoming, names)
    for text in identity:
        known = same_event_core(text, names)
        if not core or not known or core == known:
            continue
        size = _longest_common_run(core, known)[0]
        if size >= 0.5 * min(len(core), len(known)) and _distinct_variants(core, known):
            return True
    return False


def _travel_free(words: str) -> str:
    return _SAME_EVENT_TRAVEL_RE.sub("", words)


def _differing_remainders(left: str, right: str) -> bool:
    """Both sides add different words (眼科/牙科、台新/國泰); one merely adding
    to the other (新冠疫苗 ⊂ 更新新冠疫苗) or travel (回台北) is not a difference."""

    left = _travel_free(left)
    right = _travel_free(right)
    return bool(left) and bool(right) and left not in right and right not in left


def _own_words(left: str, right: str) -> tuple[str, str]:
    """What each side says beyond every run of two or more characters they share."""

    while True:
        size, end_left, end_right = _longest_common_run(left, right)
        if size < 2:
            break
        # different separators on each side, so no new shared run forms across a cut
        left = left[: end_left - size] + "\x00" + left[end_left:]
        right = right[: end_right - size] + "\x01" + right[end_right:]
    return (
        "".join(_travel_free(part) for part in left.split("\x00")),
        "".join(_travel_free(part) for part in right.split("\x01")),
    )


def _distinct_variants(left: str, right: str) -> bool:
    """Two cores around one shared activity that each add their own words:
    眼科回診／牙科回診 on the same side, and 台新信用卡繳費／信用卡繳費國泰
    or 咪咪寵物美容／寵物美容旺旺 on opposite sides (two characters or more
    each there, so 施打 vs 更新 around 流感疫苗 is not a difference)."""

    size, end_left, end_right = _longest_common_run(left, right)
    if size < 2:
        return False
    if _differing_remainders(left[: end_left - size], right[: end_right - size]):
        return True
    if _differing_remainders(left[end_left:], right[end_right:]):
        return True
    own_left, own_right = _own_words(left, right)
    return (
        len(own_left) >= 2
        and len(own_right) >= 2
        and own_left not in own_right
        and own_right not in own_left
    )


def _same_words_same_author(incoming: object, kept_action: object, same_author: bool) -> bool:
    """A short action (「領米」) repeated word for word by the same person."""

    return same_author and normalize_text(incoming) == normalize_text(kept_action)


def same_event_text(
    incoming: object,
    kept_action: object,
    *,
    same_clock: bool = False,
    names: tuple[str, ...] = (),
    same_author: bool = False,
) -> bool:
    """Whether a new description names the same activity as the kept reminder.

    A shared run followed by 前/後 is a separate task before/after the event,
    and both sides adding their own words to the shared run (王小明/李大華,
    送/拿, 眼科/牙科, 台新…/…國泰) name different events even at the same
    clock; travel (回台北) or one side only adding words is not a difference.
    """

    left = same_event_core(incoming, names)
    right = same_event_core(kept_action, names)
    if not left or not right:
        return False
    if left == right:
        return (
            len(left) >= 3
            or same_clock
            or bool(same_event_people(incoming, names))
            and same_event_people(incoming, names) == same_event_people(kept_action, names)
            or _same_words_same_author(incoming, kept_action, same_author)
        )
    size, end_left, end_right = _longest_common_run(left, right)
    if _SAME_EVENT_RELATIONAL_RE.match(left[end_left:]) or _SAME_EVENT_RELATIONAL_RE.match(
        right[end_right:]
    ):
        return False
    if _distinct_variants(left, right):
        return False
    return size >= (3 if same_clock else 4) and size >= 0.5 * min(len(left), len(right))


def same_event_move_match(
    incoming: object,
    kept_action: object,
    identity: list[object] | tuple[object, ...],
    names: tuple[str, ...] = (),
    same_author: bool = False,
) -> bool:
    """Stricter match needed before a new mention may change a reminder's time."""

    people = same_event_people(incoming, names)
    if same_event_identity_conflict(incoming, identity, names) or people != same_event_people(
        kept_action, names
    ):
        return False
    left = same_event_core(incoming, names)
    right = same_event_core(kept_action, names)
    if left and left == right and (
        len(left) >= 3 or people or _same_words_same_author(incoming, kept_action, same_author)
    ):
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 4 and shorter in longer


def has_reminder_offset_marker(text: object) -> bool:
    """「…（前一天提醒）」-style actions belong to a deliberate set of reminders."""

    return bool(_SAME_EVENT_OFFSET_RE.search(normalize_text(text)))


_OFFSET_DAYS_RE = re.compile(r"前一天|前(\d+)天|前([一二三四五六七])天|當天")


def reminder_offset_days(text: object) -> int | None:
    """Days from a 「…（前N天提醒）」 reminder to its event; None if unknown (提前) or unlabelled."""

    marker = _SAME_EVENT_OFFSET_RE.search(normalize_text(text))
    words = _OFFSET_DAYS_RE.search(marker.group()) if marker else None
    if words is None:
        return None
    if words.group() == "前一天":
        return 1
    if words.group() == "當天":
        return 0
    if words.group(1):
        return int(words.group(1))
    return _CHINESE_DIGITS[words.group(2)]


def strip_reminder_offset_marker(text: object) -> str:
    """The action without its 「（前一天提醒）」-style label."""

    stripped = _SAME_EVENT_OFFSET_RE.sub(" ", str(text or ""))
    return re.sub(r"\s+", " ", stripped).strip(" ，,、:：")


def time_kind_from_default(default_kind: object) -> str:
    """Stored time kind of a reminder created with ``_time_default_kind``."""

    if default_kind in (None, False, "", "five_minutes"):
        return "clock"
    if default_kind in (True, "no_daypart"):
        return "none"
    if default_kind == "morning":
        return "daypart:早上"
    if default_kind == "evening":
        return "daypart:晚上"
    kind = str(default_kind)
    return kind if kind.startswith("daypart:") else "clock"


def time_rank(kind: object) -> int:
    """0 no time given, 1 only a daypart, 2 a clock; unknown (legacy) counts as fixed."""

    kind = str(kind or "")
    if kind == "none":
        return 0
    if kind.startswith("daypart:"):
        return 1
    return 2


def _daypart_window(kind: object) -> tuple[int, int] | None:
    return _SAME_EVENT_DAYPART_WINDOWS.get(str(kind or "").split(":", 1)[-1])


def times_compatible(
    left_kind: object, left_hhmm: str, right_kind: object, right_hhmm: str
) -> bool:
    """Whether two mentions can be the same occurrence time-wise."""

    left_rank, right_rank = time_rank(left_kind), time_rank(right_kind)
    if left_rank == 0 or right_rank == 0:
        return True
    if left_rank == 2 and right_rank == 2:
        return left_hhmm == right_hhmm
    if left_rank == 1 and right_rank == 1:
        return str(left_kind) == str(right_kind)
    daypart_kind, clock = (left_kind, right_hhmm) if left_rank == 1 else (right_kind, left_hhmm)
    window = _daypart_window(daypart_kind)
    return window is not None and window[0] <= int(clock.split(":", 1)[0]) < window[1]


def time_is_confirmed_by(stored_hhmm: str, incoming_kind: object, incoming_hhmm: str) -> bool:
    """Whether a more specific mention confirms the stored time (promote, don't move)."""

    rank = time_rank(incoming_kind)
    if rank == 2:
        return stored_hhmm == incoming_hhmm
    if rank == 1:
        window = _daypart_window(incoming_kind)
        return window is not None and window[0] <= int(stored_hhmm.split(":", 1)[0]) < window[1]
    return False


# ── 2026-10-04 one event, one reminder: the narrow same-clock loosening ──────
# Andrew 2026-10-04「同一件事只留一筆」: the same person giving the same exact
# clock again, in other words, is almost always the same event.  Only then
# (and never against an identity conflict) two more readings count:
#   (e1) one description is just a meal word that sits inside the other
#        (晚餐 / 在某某飯店晚餐), not 「晚餐前／後…」;
#   (e3) both name the same place and the new one adds at most two characters
#        that the kept reminder's own message already contains.
# COVID(-19) is read as 新冠, and a place right after a colon (「飯店：餐廳」)
# reads as a place.

_SAME_EVENT_MEAL_WORDS = ("用餐", "吃飯", "晚餐", "午餐", "早餐", "聚餐")
_SAME_EVENT_COVID_RE = re.compile(r"covid\s*-?\s*19|covid", re.IGNORECASE)
_SAME_EVENT_COLON_PLACE_RE = re.compile(
    rf"(?<=:)\s*([一-鿿]{{2,4}}(?:{_SAME_EVENT_PLACE_SUFFIX}))"
)


def _loose_normalize(text: object) -> str:
    # normalize_text applies NFKC, so a full-width colon is ":" here.
    value = _SAME_EVENT_COVID_RE.sub("新冠", normalize_text(text))
    return _SAME_EVENT_COLON_PLACE_RE.sub(lambda match: " " + match.group(1), value)


def _meal_word_inside(short: str, long: str) -> bool:
    if short not in _SAME_EVENT_MEAL_WORDS or len(short) >= len(long) or short not in long:
        return False
    start = 0
    while (index := long.find(short, start)) >= 0:
        if long[index + len(short) : index + len(short) + 1] in ("前", "後"):
            return False
        start = index + 1
    return True


def loose_same_event(
    incoming: object,
    kept_action: object,
    kept_source: object = "",
    kept_identity: list[object] | tuple[object, ...] | None = None,
    names: tuple[str, ...] = (),
) -> bool:
    """The e1/e3 loosening; callers guarantee same author and same exact clock."""

    left = _loose_normalize(incoming)
    right = _loose_normalize(kept_action)
    identity = [_loose_normalize(text) for text in (kept_identity or [kept_action])]
    if same_event_identity_conflict(left, identity, names):
        return False
    if same_event_text(left, right, same_clock=True, names=names, same_author=True):
        return True
    core_left = same_event_core(left, names)
    core_right = same_event_core(right, names)
    if not core_left or not core_right:
        return False
    if _SAME_EVENT_RELATIONAL_RE.match(core_left) or _SAME_EVENT_RELATIONAL_RE.match(core_right):
        return False
    short, long = sorted((core_left, core_right), key=len)
    if _meal_word_inside(short, long):
        return True
    places = _same_event_places(left)
    known_places = set().union(*(_same_event_places(text) for text in identity))
    kept_message = re.sub(r"\s+", "", _loose_normalize(kept_source)).casefold()
    return bool(
        places
        and known_places
        and any(_same_place(place, known) for place in places for known in known_places)
        and len(core_left) <= 2
        and core_left in kept_message
    )


def mention_matches_reminder(
    action: object,
    kept_action: object,
    *,
    time_kind: object,
    hhmm: str,
    kept_kind: object,
    kept_hhmm: str,
    kept_identity: list[object] | tuple[object, ...] | None = None,
    kept_source: object = "",
    mentions: list[str] | tuple[str, ...] = (),
    kept_mentions: list[str] | tuple[str, ...] = (),
    names: tuple[str, ...] = (),
    same_author: bool = False,
) -> bool:
    """Whether a mention names the event a kept reminder describes.

    The write-time same-event test of memory._merge_same_event_conn, shared
    with the push-time fold in reminder_push.  The time kinds must allow one
    occurrence, the people named must agree, nothing the kept reminder has
    absorbed may contradict the mention, and the activity must match: as
    before, or through the loosening above when the same person gave the
    same exact clock (time rank 2 on both sides; legacy NULL counts).
    """

    all_names = tuple(dict.fromkeys([*mentions, *kept_mentions, *names]))
    identity = list(kept_identity) if kept_identity else [kept_action]
    if not times_compatible(kept_kind, kept_hhmm, time_kind, hhmm):
        return False
    incoming_people = set(mentions) | same_event_people(action, all_names)
    known_people = set(kept_mentions).union(
        *(same_event_people(text, all_names) for text in identity)
    )
    if (
        incoming_people
        and known_people
        and not (incoming_people <= known_people or known_people <= incoming_people)
    ):
        return False
    if same_event_identity_conflict(action, identity, all_names):
        return False
    same_clock = time_rank(kept_kind) == 2 and time_rank(time_kind) == 2 and kept_hhmm == hhmm
    if same_event_text(
        action, kept_action, same_clock=same_clock, names=all_names, same_author=same_author
    ):
        return True
    return (
        same_clock
        and same_author
        and loose_same_event(action, kept_action, kept_source, identity, all_names)
    )
