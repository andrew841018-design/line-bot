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
from datetime import date, timedelta

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
_TIME_COLON = r"(?:[01]?\d|2[0-3]):[0-5]\d"
_TIME_COMPACT = r"(?:[01]?\d|2[0-3])[0-5]\d"
_TIME = rf"(?:{_TIME_COLON}|{_TIME_COMPACT})"
_CHINESE_NUM = r"[零〇一二兩三四五六七八九十]{1,3}"
_CHINESE_TIME = (
    rf"(?:(?:\d{{1,2}}|{_CHINESE_NUM})\s*點"
    rf"(?:\s*(?:半|(?:\d{{1,2}}|{_CHINESE_NUM})\s*分?))?)"
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

_CHINESE_DATE_TIME_TITLE = re.compile(
    rf"(\d{{1,2}})月(\d{{1,2}})日\s*({_TIME})\s+([^\n\r]{{2,40}})"
)

_RELATIVE_DATE_TIME_TITLE = re.compile(
    rf"(今天|明天|後天)\s*({_TIME})\s+([^\n\r]{{2,40}})"
)

_RELATIVE_CHINESE_TIME_TITLE = re.compile(
    rf"({_REL_OR_WEEKDAY})\s*({_DAYPART})?\s*({_TIME}|{_CHINESE_TIME})\s*([^\n\r]{{2,40}})"
)

_DATE_TOKEN = re.compile(
    r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日|\d{1,2}/\d{1,2})"
)
_DATE_PREFIX = re.compile(
    r"^(?:(\d{4})-(\d{1,2})-(\d{1,2})|(\d{1,2})月(\d{1,2})日|(\d{1,2})/(\d{1,2}))"
)
_TIME_IN_TEXT = re.compile(
    rf"(?:(?P<compact_daypart>{_DAYPART})\s*"
    rf"(?P<compact_time>{_TIME_COMPACT})|"
    rf"(?P<regular_daypart>{_DAYPART})?\s*"
    rf"(?P<regular_time>{_TIME_COLON}|{_CHINESE_TIME}|"
    rf"(?<![\s\S]){_TIME_COMPACT}))"
)
_DAYPART_IN_TEXT = re.compile(_DAYPART)
_TIME_RANGE_IN_TEXT = re.compile(
    rf"(?:(?P<range_compact_daypart>{_DAYPART})\s*"
    rf"(?P<range_daypart_compact_start>{_TIME_COMPACT})|"
    rf"(?<![\s\S])(?P<range_leading_compact_start>{_TIME_COMPACT})|"
    rf"(?:(?P<range_regular_daypart>{_DAYPART})\s*)?"
    rf"(?P<range_regular_start>{_TIME_COLON}|{_CHINESE_TIME}))"
    rf"\s*(?:-|~|～|到|至)\s*"
    rf"(?:(?P<range_end_daypart>{_DAYPART})\s*)?"
    rf"(?P<range_end>{_TIME_COLON}|{_CHINESE_TIME}|{_TIME_COMPACT})"
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


def _parse_date_token(token: str, today_tw: date) -> date | None:
    m = _DATE_PREFIX.match(token)
    if not m:
        return None
    if m.group(1):
        return _validate_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if m.group(4):
        month, day = int(m.group(4)), int(m.group(5))
    else:
        month, day = int(m.group(6)), int(m.group(7))
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
    range_match = _TIME_RANGE_IN_TEXT.search(text)
    if range_match:
        return _parse_time(
            range_match.group("range_daypart_compact_start")
            or range_match.group("range_leading_compact_start")
            or range_match.group("range_regular_start"),
            range_match.group("range_compact_daypart")
            or range_match.group("range_regular_daypart"),
        )
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

    # 1. YYYY-MM-DD HH:MM title
    m = _DATE_TIME_TITLE.search(combined_text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        target = _validate_date(year, month, day)
        time_str = _parse_time(m.group(4))
        title = _sanitize_title(m.group(5))
        et = _classify_type(title) if len(title) >= 2 else None
        if target and time_str and et:
            return _make_event(title, target.isoformat(), time_str, et)

    # 2. N月N日 HH:MM title — year inference: past → +1
    m = _CHINESE_DATE_TIME_TITLE.search(combined_text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        time_str = _parse_time(m.group(3))
        title = _sanitize_title(m.group(4))
        et = _classify_type(title) if len(title) >= 2 else None
        if time_str and et:
            target = _validate_date(today_tw.year, month, day)
            if target:
                if target < today_tw:
                    target = _validate_date(today_tw.year + 1, month, day)
                if target:
                    return _make_event(title, target.isoformat(), time_str, et)

    # 3. 今天/明天/後天 HH:MM title
    m = _RELATIVE_DATE_TIME_TITLE.search(combined_text)
    if m:
        offset = {"今天": 0, "明天": 1, "後天": 2}[m.group(1)]
        target = today_tw + timedelta(days=offset)
        time_str = _parse_time(m.group(2))
        title = _sanitize_title(m.group(3))
        et = _classify_type(title) if len(title) >= 2 else None
        if time_str and et:
            return _make_event(title, target.isoformat(), time_str, et)

    # 4. 今天/星期四 + 中文時間 title（例：星期四早上十點半看牙醫）
    m = _RELATIVE_CHINESE_TIME_TITLE.search(combined_text)
    if m:
        target = _target_for_relative_or_weekday(m.group(1), today_tw)
        time_str = _parse_time(m.group(3), m.group(2))
        title = _sanitize_title(m.group(4))
        et = _classify_type(title) if len(title) >= 2 else None
        if target and time_str and et:
            return _make_event(title, target.isoformat(), time_str, et)

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


def _title_for_fragment(segment_body: str, location: str | None, combined_text: str) -> str:
    haystack = f"{segment_body} {combined_text}"
    if "東海" in haystack and "校友" in haystack:
        if location and "六福" in location:
            return "東海大學校友會活動（六福萬怡酒店）"
        if location and "美僑" in location:
            return "東海大學校友會活動（美僑俱樂部）"
        return "東海大學校友會活動"
    if location:
        action_body = re.sub(_TIME_RANGE_IN_TEXT, "", segment_body)
        action_body = re.sub(_TIME_IN_TEXT, "", action_body)
        action_body = re.sub(_DAYPART_IN_TEXT, "", action_body)
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
        return f"{location}活動"[:30]
    raw = re.sub(_TIME_RANGE_IN_TEXT, "", segment_body)
    raw = re.sub(_TIME_IN_TEXT, "", raw)
    raw = re.sub(_DAYPART_IN_TEXT, "", raw)
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
) -> dict | None:
    token_match = _DATE_PREFIX.match(segment.strip())
    if not token_match:
        return None
    target = _parse_date_token(token_match.group(0), today_tw)
    if not target:
        return None
    body = _strip_date_prefix(segment)
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
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(combined_text)
        segment = combined_text[match.start():end]
        ev = _fragment_event(
            segment,
            today_tw,
            combined_text,
            require_time=require_time,
            fallback_title=fallback_title,
        )
        if ev:
            events.append(ev)

    return _dedupe_events(events)
