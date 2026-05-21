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
    r"拿(?:蛋糕|藥|包裹|貨|餐|禮物|花)|"
    r"接(?:爸|媽|妹|弟|姊|爺爺|奶奶|小孩|小朋友)|"
    r"陪(?:爸|媽|妹|弟|姊|爺爺|奶奶|看醫生)|"
    r"喜來登|"
    r"回(?:家|老家)|"
    r"婚禮|喜宴|滿月|彌月)"
)

# Plan C：三類 event_type 分類規則（醫療 > 個人旅程 > 家族聚會，medical 最先因更具體）
# 注意：personal_trip「回 X」明列地名不含「家」單字，避免「回家吃飯」誤分類為 trip
_TYPE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    (
        "medical",
        re.compile(
            r"做(?:胃鏡|大腸鏡|健康檢查|體檢|手術|健檢)|"
            r"看(?:醫生|牙醫|皮膚科|眼科|耳鼻喉科)|"
            r"陪(?:.{0,5})(?:就醫|看醫生|看病|拿藥)|"
            r"打疫苗|抽血|健檢|回診"
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

# HH:MM — validated 00:00 to 23:59
_TIME = r"(?:[01]?\d|2[0-3]):[0-5]\d"

_DATE_TIME_TITLE = re.compile(
    rf"(\d{{4}})-(\d{{1,2}})-(\d{{1,2}})\s+({_TIME})\s+([^\n\r]{{2,40}})"
)

_CHINESE_DATE_TIME_TITLE = re.compile(
    rf"(\d{{1,2}})月(\d{{1,2}})日\s*({_TIME})\s+([^\n\r]{{2,40}})"
)

_RELATIVE_DATE_TIME_TITLE = re.compile(
    rf"(今天|明天|後天)\s*({_TIME})\s+([^\n\r]{{2,40}})"
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


def _sanitize_title(raw: str) -> str:
    """Strip control chars + trim length."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", raw).strip()
    return cleaned[:30]


def _validate_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def extract_regex_only(combined_text: str, today_tw: date) -> dict:
    """Pure-regex family event extractor.

    Returns dict matching calendar_extractor.extract() schema.
    All title hits must pass _FAMILY_KW.search() to filter out work events
    and irrelevant date-time strings.
    """
    if not combined_text or not combined_text.strip():
        return _make_fail()

    # 1. YYYY-MM-DD HH:MM title
    m = _DATE_TIME_TITLE.search(combined_text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        target = _validate_date(year, month, day)
        time_str = m.group(4)
        title = _sanitize_title(m.group(5))
        et = _classify_type(title) if len(title) >= 2 else None
        if target and et:
            return _make_event(title, target.isoformat(), time_str, et)

    # 2. N月N日 HH:MM title — year inference: past → +1
    m = _CHINESE_DATE_TIME_TITLE.search(combined_text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        time_str = m.group(3)
        title = _sanitize_title(m.group(4))
        et = _classify_type(title) if len(title) >= 2 else None
        if et:
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
        time_str = m.group(2)
        title = _sanitize_title(m.group(3))
        et = _classify_type(title) if len(title) >= 2 else None
        if et:
            return _make_event(title, target.isoformat(), time_str, et)

    return _make_fail()
