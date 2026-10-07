from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest


_NOW_TW = datetime(2026, 8, 28, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("咪寶明天提醒我領米", (12, 0)),
        ("咪寶明天早上提醒我領米", (9, 0)),
        ("咪寶明天晚上提醒我領米", (19, 0)),
    ],
)
def test_single_reminder_uses_uniform_unspecified_time_policy(text, expected):
    import main

    result = main._explicit_single_reminder_result(text, "U1", now_tw=_NOW_TW)

    assert result is not None
    assert (result["hour"], result["minute"]) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("咪寶明天早上10點提醒我領米", (10, 0)),
        ("咪寶明天晚上8點提醒我領米", (20, 0)),
        ("咪寶明天19:30提醒我領米", (19, 30)),
    ],
)
def test_explicit_clock_always_wins_over_daypart_defaults(text, expected):
    import main

    result = main._explicit_single_reminder_result(text, "U1", now_tw=_NOW_TW)

    assert result is not None
    assert (result["hour"], result["minute"]) == expected
    assert result["_time_was_defaulted"] is False


@pytest.mark.parametrize(
    ("daypart", "expected"),
    [
        ("", (12, 0)),
        ("早上", (9, 0)),
        ("晚上", (19, 0)),
    ],
)
def test_range_reminder_uses_same_unspecified_time_policy(daypart, expected):
    import main

    text = f"提醒我9月16到9月28日{daypart}在紐西蘭期間要買：按摩油"
    result = main._explicit_range_reminder_result(text, "U1", now_tw=_NOW_TW)

    assert result is not None
    assert (result["hour"], result["minute"]) == expected


@pytest.mark.parametrize(
    ("text", "parser"),
    [
        ("咪寶明天早上晚上提醒我領米", "single"),
        (
            "提醒我9月16到9月28日早上晚上在紐西蘭期間要買：按摩油",
            "range",
        ),
    ],
)
def test_multiple_dayparts_without_a_clock_fail_closed(text, parser):
    import main
    import reminder_intent

    assert reminder_intent.resolve_reminder_default_time(text) is None
    parse = (
        main._explicit_single_reminder_result
        if parser == "single"
        else main._explicit_range_reminder_result
    )
    assert parse(text, "U1", now_tw=_NOW_TW) is None


def test_model_extractor_cannot_reintroduce_the_old_0900_default(monkeypatch):
    import gemini_client

    response = SimpleNamespace(
        text=(
            '{"action":"領米","year":2026,"month":8,"day":29,'
            '"hour":9,"minute":0}'
        )
    )
    monkeypatch.setattr(
        gemini_client,
        "_client",
        SimpleNamespace(
            models=SimpleNamespace(generate_content=lambda **_kwargs: response)
        ),
    )
    monkeypatch.setattr(gemini_client, "_track_usage", lambda _response: None)

    result = gemini_client.extract_reminder(
        "明天提醒我領米",
        today_iso="2026-08-28 Friday",
    )

    assert result is not None
    assert (result["hour"], result["minute"]) == (12, 0)
    assert result["_time_default_kind"] == "no_daypart"


def test_model_extractor_preserves_an_explicit_clock(monkeypatch):
    import gemini_client

    response = SimpleNamespace(
        text=(
            '{"action":"領米","year":2026,"month":8,"day":29,'
            '"hour":20,"minute":15}'
        )
    )
    monkeypatch.setattr(
        gemini_client,
        "_client",
        SimpleNamespace(
            models=SimpleNamespace(generate_content=lambda **_kwargs: response)
        ),
    )
    monkeypatch.setattr(gemini_client, "_track_usage", lambda _response: None)

    result = gemini_client.extract_reminder(
        "明天晚上8點15分提醒我領米",
        today_iso="2026-08-28 Friday",
    )

    assert result is not None
    assert (result["hour"], result["minute"]) == (20, 15)
    assert "_time_default_kind" not in result


def test_regex_fallback_uses_noon_when_the_event_has_no_time(monkeypatch):
    import calendar_regex
    import main

    monkeypatch.setattr(
        calendar_regex,
        "extract_many_regex_only",
        lambda *_args, **_kwargs: [
            {
                "has_event": True,
                "date": "2026-09-16",
                "time": None,
                "title": "領米",
            }
        ],
    )

    result = main._calendar_regex_to_reminder_result(
        "9月16日領米",
        date(2026, 8, 28),
        "U1",
    )

    assert result is not None
    assert (result["hour"], result["minute"]) == (12, 0)
    assert result["_time_default_kind"] == "no_daypart"


@pytest.mark.parametrize(
    ("kind", "clock", "fragment"),
    [
        ("morning", (9, 0), "依「早上」預設 09:00"),
        ("evening", (19, 0), "依「晚上」預設 19:00"),
        ("no_daypart", (12, 0), "未指定時間，預設 12:00"),
    ],
)
def test_confirmation_discloses_which_default_was_used(kind, clock, fragment):
    import main

    confirmation = main._format_reminder_write_confirmation(
        "created",
        "領米",
        datetime(2026, 8, 29, *clock, tzinfo=ZoneInfo("Asia/Taipei")),
        [],
        kind,
    )

    assert fragment in confirmation
