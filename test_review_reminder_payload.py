from datetime import datetime
from zoneinfo import ZoneInfo

import pytest


@pytest.mark.parametrize(("text", "expected"), [
    ('提醒我讀「9/10的報告」，明天', '讀「9/10的報告」'),
    ('提醒我看「8點的新聞」，明天10點', '看「8點的新聞」'),
    ('提醒我讀「早上的筆記」，明天早上', '讀「早上的筆記」'),
    ('提醒我買7/11咖啡，明天', '買7/11咖啡'),
    ('提醒我查看1:20比例，明天10點', '查看1:20比例'),
    ('明晚提醒我讀「9/10的報告」', '讀「9/10的報告」'),
])
def test_schedule_removal_preserves_task_payload(monkeypatch, text, expected):
    import main

    monkeypatch.setattr(main, "_alias_from_user_id", lambda _user: None)
    now = datetime(2026, 9, 6, 8, tzinfo=ZoneInfo("Asia/Taipei"))
    result = main._explicit_single_reminder_result(text, "U_SYNTHETIC", now_tw=now)
    assert result is not None
    assert result["action"] == expected
    assert (result["year"], result["month"], result["day"]) == (2026, 9, 7)


@pytest.mark.parametrize(("now", "text", "expected_date"), [
    ((2026, 9, 6), '提醒我9月6到9月8日要買：筆記本', (2026, 9, 7)),
    ((2026, 9, 30), '提醒我9月30到10月2日要買：筆記本', (2026, 10, 1)),
    ((2026, 12, 31), '提醒我12月31到1月2日要買：筆記本', (2027, 1, 1)),
    ((2026, 9, 6), '提醒我9月6到9月6日要買：筆記本', None),
])
def test_range_five_minute_fallback_keeps_calendar_date(
    monkeypatch, now, text, expected_date,
):
    import main

    monkeypatch.setattr(main, "_alias_from_user_id", lambda _user: None)
    result = main._explicit_range_reminder_result(
        text, "U_SYNTHETIC",
        now_tw=datetime(*now, 23, 58, tzinfo=ZoneInfo("Asia/Taipei")),
    )
    if expected_date is None:
        assert result is None
    else:
        assert result is not None
        assert (result["year"], result["month"], result["day"]) == expected_date
        assert (result["hour"], result["minute"]) == (0, 3)
