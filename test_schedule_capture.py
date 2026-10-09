"""P1 (2026-10-04): reminders are added directly, never "會再確認".

Covers the deterministic schedule-list parser, local creation when the model
is unavailable, quoted 「咪寶」 capture, the silent queue / silent drops, the
daily audit of dropped rows and the burst calendar skip.  Synthetic data only.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent

import calendar_regex
import gemini_client
import main
import memory

sys.path.insert(0, str(Path(__file__).parent / "jobs"))
import daily_pending_audit as dpa  # noqa: E402

TW = ZoneInfo("Asia/Taipei")
G = "G_SCHED"
U_SENDER = "U_SENDER"
U_OTHER = "U_OTHER"
TODAY = date(2026, 10, 2)


# ── helpers ───────────────────────────────────────────────────────────────────


def _today() -> date:
    return datetime.now(TW).date()


def _md(day: date) -> str:
    return f"{day.month}/{day.day}"


def _cn(number: int) -> str:
    digits = "一二三四五六七八九"
    if number < 10:
        return digits[number - 1]
    tens, ones = divmod(number, 10)
    head = "" if tens == 1 else digits[tens - 1]
    return f"{head}十{digits[ones - 1] if ones else ''}"


def _base_day() -> date:
    """Day 10 of next month, so a day list up to +3 stays in that month."""
    first = _today().replace(day=1)
    return (first + timedelta(days=40)).replace(day=10)


def _trip_text(base: date) -> str:
    return (
        f"{_md(base)}去海邊一日遊\n"
        f"{base.day + 1}、{base.day + 3} 市區自由行\n"
        f"{_md(base + timedelta(days=2))}跟團一日遊"
    )


def _pending_rows(group_id: str = G) -> list[tuple]:
    with memory._conn() as c:
        return c.execute(
            "SELECT action, remind_at, user_id, source_text, source_kind, source_ref, "
            "reminder_id FROM reminders WHERE group_id=? AND status='pending' "
            "ORDER BY remind_at, reminder_id",
            (group_id,),
        ).fetchall()


def _queue_rows(group_id: str = G) -> list[tuple]:
    with memory._conn() as c:
        return c.execute(
            "SELECT pending_id, status FROM pending_reminder_extract WHERE group_id=?",
            (group_id,),
        ).fetchall()


def _queue_state(pending_id: int) -> tuple:
    with memory._conn() as c:
        return c.execute(
            "SELECT status, dropped_at, drop_reason FROM pending_reminder_extract "
            "WHERE pending_id=?",
            (pending_id,),
        ).fetchone()


def _outbox_count() -> int:
    with memory._conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM reminder_confirmation_outbox"
        ).fetchone()[0]


def _day_of(epoch: int) -> date:
    return datetime.fromtimestamp(int(epoch), TW).date()


def _clock_of(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), TW).strftime("%H:%M")


def _no_model(monkeypatch) -> None:
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: False)
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        MagicMock(side_effect=AssertionError("model must not be called")),
    )


def _model_unavailable(monkeypatch) -> MagicMock:
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: True)
    stub = MagicMock(side_effect=RuntimeError("503 UNAVAILABLE"))
    monkeypatch.setattr(gemini_client, "extract_reminder", stub)
    return stub


def _text_event(
    text: str,
    *,
    message_id: str = "m-evt",
    user_id: str = U_SENDER,
    quoted: str | None = None,
):
    message = MagicMock(spec=TextMessageContent)
    message.id = message_id
    message.text = text
    message.mention = None
    message.quoted_message_id = quoted
    message.quote_token = "qt"
    message.type = "text"
    source = MagicMock(spec=GroupSource)
    source.group_id = G
    source.user_id = user_id
    event = MagicMock(spec=MessageEvent)
    event.message = message
    event.source = source
    event.reply_token = "TOKEN-SCHED"
    event.timestamp = int(time.time() * 1000)
    return event


def _quiet_routing(monkeypatch) -> None:
    monkeypatch.setattr(main.feedback_collector, "in_feedback_window", lambda: False)
    monkeypatch.setattr(main.burst_filter, "add_to_burst", MagicMock())
    monkeypatch.setattr(main.burst_filter, "cancel_burst", MagicMock(return_value=[]))
    monkeypatch.setattr(
        main, "_try_piggyback_reminders_fast_path", lambda *_a, **_k: False
    )


def _seed_raw(
    text: str,
    *,
    message_id: str = "m-src",
    user_id: str = U_SENDER,
    age_sec: int = 3600,
) -> None:
    with memory._conn() as c:
        c.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (G, message_id, user_id, text, int(time.time()) - age_sec),
        )


# ── calendar_regex.extract_schedule_lines ─────────────────────────────────────


def test_parser_itinerary_with_day_list():
    items = calendar_regex.extract_schedule_lines(
        "10/23去海邊一日遊\n24、26 市區自由行\n10/25跟團一日遊", TODAY
    )

    assert [(item["date"], item["title"]) for item in items] == [
        ("2026-10-23", "去海邊一日遊"),
        ("2026-10-24", "市區自由行"),
        ("2026-10-25", "跟團一日遊"),
        ("2026-10-26", "市區自由行"),
    ]
    assert all(item["time"] is None for item in items)


@pytest.mark.parametrize(
    "text",
    [
        "24、26 市區自由行",
        "9/28去海邊一日遊",
        "10/23去海邊一日遊？",
        "10/23去海邊一日遊嗎",
        "上次10/23去海邊一日遊",
        "1/2 杯牛奶\n1/4 匙鹽\n2/3 杯麵粉",
        "1/3 的價格就買到",
        "日本買才1/3 或1/2 的價格，又可以退稅。",
        "10/5 晴 25-31度\n10/6 雨 22-27度",
        "10/15 除息\n10/20 發放股利",
        "10/23 要下雨",
        "東北五日遊\n10/23 出發\n團費 25900",
        "10/23-10/26 日本東北自由行",
        "10/23~26 日本自由行",
        "10/23到10/26 去日本",
        "10月23日至26日 去日本",
        "10/23去海邊 10/24回來",
        "10/23去海邊一日遊\n10/24 好熱",
        "5/20 去日本玩",
        "2027/6/1 出國",
        "13/40 去海邊",
        "10/23去海邊\n3、4個人去吃飯",
    ],
)
def test_parser_rejects_non_schedules(text):
    assert calendar_regex.extract_schedule_lines(text, TODAY) == []


def test_parser_day_list_crosses_month_and_year():
    items = calendar_regex.extract_schedule_lines(
        "12/30 出發去海邊\n31、1、2 市區自由行", date(2026, 12, 1)
    )
    assert [item["date"] for item in items] == [
        "2026-12-30",
        "2026-12-31",
        "2027-01-01",
        "2027-01-02",
    ]

    items = calendar_regex.extract_schedule_lines(
        "9/30 出發去海邊\n1、2 市區自由行", date(2026, 9, 1)
    )
    assert [item["date"] for item in items] == [
        "2026-09-30",
        "2026-10-01",
        "2026-10-02",
    ]


@pytest.mark.parametrize("text", ["十月22日去日本要買青汁", "十月二十二日去日本要買青汁"])
def test_parser_reads_chinese_month(text):
    items = calendar_regex.extract_schedule_lines(text, TODAY)
    assert [(item["date"], item["title"]) for item in items] == [
        ("2026-10-22", "去日本要買青汁")
    ]


def test_parser_keeps_clock_daypart_and_weekday_annotation():
    items = calendar_regex.extract_schedule_lines(
        "10/23（五）14:30 去看診\n10/24 晚上 聚餐吃飯\n10/25一日遊去海邊", TODAY  # privacy-safe-fixture
    )

    assert [
        (item["date"], item["title"], item["time"], item.get("daypart"))
        for item in items
    ] == [
        ("2026-10-23", "去看診", "14:30", None),  # privacy-safe-fixture
        ("2026-10-24", "聚餐吃飯", None, "晚上"),
        ("2026-10-25", "一日遊去海邊", None, None),
    ]


def test_parser_caps_items_per_message():
    text = "\n".join(f"10/{day}去海邊" for day in range(10, 17))
    assert calendar_regex.extract_schedule_lines(text, TODAY) == []


# GP1 r2 (2026-10-05): every one of these used to become 「已新增 2 筆提醒」
# plus a full push ladder.  A negated / called-off line, or a forecast or
# price line, is not a plan, and one such line rejects the whole message.
_NOT_A_PLAN_PROBES = [
    "10/23 不去了\n10/24 也不回去",
    "10/23 取消去台中\n10/26 改成不回台北",
    "10/23 沒辦法去\n10/24 不能去",
    "10/7 晴到多雲\n10/8 多雲到陰",
    "10/7 晴 22到30度\n10/8 陰 21到27度",
    "10/6 油價漲到32元\n10/13 降到31元",
]


@pytest.mark.parametrize(
    "text",
    _NOT_A_PLAN_PROBES
    + [
        "10/23 去台中\n10/24 不回台北",
        "10/23 不去海邊了",
        "10/23 去台中\n10/24 不陪媽媽去了",
        "10/23 去台中\n10/24 沒有要回去",
        "10/23 不用去上課\n10/24 去台中",
        "10/23 不參加聚餐\n10/24 去台中",
        "10/23 去台中聚餐作罷\n10/24 回台北",
        "10/23 去台中 延期\n10/24 回台北",
        "10/23 去不了台中\n10/24 回台北",
        "10/7 多雲時晴 去爬山\n10/8 回公司",
        "10/7 降雨機率高 去爬山\n10/8 回公司",
        "10/6 股價回升\n10/7 回檔",
        "10/7 回溫\n10/8 回暖",
        "10/6 金價漲到2700\n10/7 跌到2650",
        "10/7 回到1450\n10/8 回到1500",
    ],
)
def test_parser_rejects_negated_cancelled_weather_and_price_lines(text):
    assert calendar_regex.extract_schedule_lines(text, TODAY) == []


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "10/23 到台中找阿姨\n10/24 回台北",
            [("2026-10-23", "到台中找阿姨"), ("2026-10-24", "回台北")],
        ),
        (
            "10/23 去台中\n10/26 回台北",
            [("2026-10-23", "去台中"), ("2026-10-26", "回台北")],
        ),
        (
            "10/7 早上到九份\n10/8 下午到十分放天燈",
            [("2026-10-07", "到九份"), ("2026-10-08", "到十分放天燈")],
        ),
        (
            "10/23 帶雨傘去台中\n10/24 回台北",
            [("2026-10-23", "帶雨傘去台中"), ("2026-10-24", "回台北")],
        ),
        (
            "10/23 去看醫生 不用空腹\n10/30 回診",
            [("2026-10-23", "去看醫生 不用空腹"), ("2026-10-30", "回診")],
        ),
        (
            "10/23 去晴光市場買菜\n10/24 回台北",
            [("2026-10-23", "去晴光市場買菜"), ("2026-10-24", "回台北")],
        ),
    ],
)
def test_parser_keeps_genuine_plans_with_single_char_verbs(text, expected):
    items = calendar_regex.extract_schedule_lines(text, TODAY)

    assert [(item["date"], item["title"]) for item in items] == expected


# ── date hint (fractions are not dates) ───────────────────────────────────────


def test_reminder_date_hint_ignores_fraction_prices():
    for text in (
        "日本買才1/3 或1/2 的價格，又可以退稅。",
        "加 2/3 杯麵粉",
        "超過1/2要先處理",
        "13/40 這樣",
    ):
        assert main._has_reminder_date_hint(text) is False, text
    for text in (
        "1/3 要去看牙醫",
        "10/23量血壓",
        "約10/23出發",
        "10/23左右回來",
        "10/23多帶外套",
        "明天要開會",
    ):
        assert main._has_reminder_date_hint(text) is True, text


def test_fraction_price_text_is_neither_queued_nor_created(monkeypatch):
    _no_model(monkeypatch)
    text = "日本買才1/3 或1/2 的價格，又可以退稅。"

    assert main._maybe_extract_reminder(text, G, U_SENDER, "m-frac") is None
    assert main._enqueue_reminder_if_candidate(text, G, U_SENDER, "m-frac-2") is None
    assert _queue_rows() == []
    assert _pending_rows() == []


def _small_slash_date() -> date:
    """A fraction-looking date (small numbers) comfortably in the future."""
    today = _today()
    for month, day in ((1, 3), (3, 1), (5, 2), (7, 1), (9, 2), (11, 3)):
        for year in (today.year, today.year + 1):
            candidate = date(year, month, day)
            if 7 <= (candidate - today).days <= 180:
                return candidate
    raise AssertionError("no candidate date")


def test_slash_date_with_action_is_still_a_reminder(monkeypatch):
    _no_model(monkeypatch)
    target = _small_slash_date()

    receipt = main._maybe_extract_reminder(
        f"{_md(target)} 要去看牙醫", G, U_SENDER, "m-dentist"
    )

    rows = _pending_rows()
    assert receipt and receipt.startswith("已新增提醒")
    assert [_day_of(row[1]) for row in rows] == [target]


def test_auto_capture_ignores_fraction_only_dates(monkeypatch):
    monkeypatch.setattr(
        main,
        "_capture_calendar_events_regex_only",
        MagicMock(side_effect=AssertionError("fraction is not a date")),
    )

    assert (
        main._auto_capture_text_if_important(
            G, "生日蛋糕食譜 1/2 杯牛奶", U_SENDER, "m-recipe"
        )
        is False
    )


# ── local creation ────────────────────────────────────────────────────────────


def test_schedule_list_creates_one_reminder_per_date_without_model(monkeypatch):
    _no_model(monkeypatch)
    base = _base_day()

    receipt = main._maybe_extract_reminder(_trip_text(base), G, U_SENDER, "m-trip")

    rows = _pending_rows()
    assert [(_day_of(row[1]), row[0]) for row in rows] == [
        (base, "去海邊一日遊"),
        (base + timedelta(days=1), "市區自由行"),
        (base + timedelta(days=2), "跟團一日遊"),
        (base + timedelta(days=3), "市區自由行"),
    ]
    assert {_clock_of(row[1]) for row in rows} == {"12:00"}
    assert {row[2] for row in rows} == {U_SENDER}
    assert [row[4] for row in rows] == ["schedule_line"] * 4
    assert sorted(row[5] for row in rows) == [f"m-trip:{index}" for index in range(4)]
    assert isinstance(receipt, main.ReminderReceipt)
    lines = receipt.splitlines()
    assert lines[0] == "已新增 4 筆提醒"
    assert f"{base:%Y-%m-%d} 12:00 去海邊一日遊" in lines
    assert lines[-1] == "未指定時間，均預設 12:00。"
    assert sorted(receipt.reminder_ids) == sorted(row[6] for row in rows)
    assert _queue_rows() == []


def test_schedule_list_resend_reports_existing_without_duplicates(monkeypatch):
    _no_model(monkeypatch)
    text = _trip_text(_base_day())

    main._maybe_extract_reminder(text, G, U_SENDER, "m-trip")
    again = main._maybe_extract_reminder(text, G, U_SENDER, "m-trip")
    copy = main._maybe_extract_reminder(text, G, U_SENDER, "m-trip-copy")

    assert len(_pending_rows()) == 4
    assert again.splitlines()[0] == "4 筆提醒皆已存在，未重複新增"
    assert copy.splitlines()[0] == "4 筆提醒皆已存在，未重複新增"
    assert again.reminder_ids == () and copy.reminder_ids == ()


def test_cancelled_schedule_item_is_not_recreated(monkeypatch):
    _no_model(monkeypatch)
    text = _trip_text(_base_day())
    main._maybe_extract_reminder(text, G, U_SENDER, "m-trip")
    with memory._conn() as c:
        c.execute(
            "UPDATE reminders SET status='cancelled' WHERE source_ref='m-trip:0'"
        )

    receipt = main._maybe_extract_reminder(text, G, U_SENDER, "m-trip")

    assert len(_pending_rows()) == 3
    assert "先前已取消，未重新建立" in receipt


def test_command_line_above_schedule_is_not_blocked_by_single_reminder_gate(
    monkeypatch,
):
    _no_model(monkeypatch)
    base = _base_day()
    text = (
        "咪寶 記一下\n"
        f"{_md(base)}去海邊一日遊\n"
        f"{_md(base + timedelta(days=2))}跟團一日遊"
    )

    receipt = main._maybe_extract_reminder(text, G, U_SENDER, "m-cmd-list")

    assert [row[0] for row in _pending_rows()] == ["去海邊一日遊", "跟團一日遊"]
    assert receipt.startswith("已新增 2 筆提醒")


@pytest.mark.parametrize("text", _NOT_A_PLAN_PROBES)
def test_local_schedule_list_ignores_negated_weather_and_price_lists(text):
    assert main._local_schedule_list_items(text, TODAY) == []


def test_local_schedule_list_keeps_a_genuine_trip_with_single_char_verbs():
    items = main._local_schedule_list_items("10/23 到台中找阿姨\n10/24 回台北", TODAY)

    assert [(item["date"], item["title"]) for item in items] == [
        ("2026-10-23", "到台中找阿姨"),
        ("2026-10-24", "回台北"),
    ]


_NOT_A_PLAN_TEMPLATES = [
    "{d0} 不去了\n{d1} 也不回去",
    "{d0} 取消去台中\n{d3} 改成不回台北",
    "{d0} 沒辦法去\n{d1} 不能去",
    "{d0} 晴到多雲\n{d1} 多雲到陰",
    "{d0} 晴 22到30度\n{d1} 陰 21到27度",
    "{d0} 油價漲到32元\n{d7} 降到31元",
]


@pytest.mark.parametrize("template", _NOT_A_PLAN_TEMPLATES)
def test_not_a_plan_list_message_writes_no_reminder_and_sends_no_receipt(
    monkeypatch, template
):
    _no_model(monkeypatch)
    _quiet_routing(monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(
        main, "_reply", lambda _token, text, **_kw: sent.append(text) or True
    )
    base = _base_day()
    text = template.format(
        **{f"d{offset}": _md(base + timedelta(days=offset)) for offset in range(8)}
    )

    main._handle_text_message(_text_event(text, message_id="m-not-a-plan"), G)

    assert _pending_rows() == []
    assert _queue_rows() == []
    assert not any("已新增" in reply or "筆提醒" in reply for reply in sent), sent


def test_negated_schedule_request_creates_nothing(monkeypatch):
    _no_model(monkeypatch)
    base = _base_day()
    text = (
        "不要提醒我\n"
        f"{_md(base)}去海邊一日遊\n"
        f"{_md(base + timedelta(days=2))}跟團一日遊"
    )

    assert main._maybe_extract_reminder(text, G, U_SENDER, "m-negated") is None
    assert _pending_rows() == []


@pytest.mark.parametrize("kind", ["day_list", "recent_past", "question"])
def test_unanchored_past_or_question_schedules_create_nothing(monkeypatch, kind):
    _no_model(monkeypatch)
    text = {
        "day_list": "24、26 市區自由行",
        "recent_past": f"{_md(_today() - timedelta(days=5))}去海邊一日遊",
        "question": f"{_md(_base_day())}去海邊一日遊？",
    }[kind]

    main._maybe_extract_reminder(text, G, U_SENDER, f"m-{kind}")

    assert _pending_rows() == []


def test_chinese_month_line_is_created_locally_when_model_unavailable(monkeypatch):
    stub = _model_unavailable(monkeypatch)
    target = _base_day() + timedelta(days=12)
    text = f"{_cn(target.month)}月{target.day}日去日本要買青汁"

    receipt = main._maybe_extract_reminder(text, G, U_SENDER, "m-cn")

    rows = _pending_rows()
    assert stub.call_count == 1
    assert [(_day_of(row[1]), row[0]) for row in rows] == [(target, "去日本要買青汁")]
    assert receipt.startswith("已新增提醒")
    assert "未指定時間，預設 12:00" in receipt
    assert _queue_rows() == []


@pytest.mark.parametrize("unavailable", ["not_allowed", "raises"])
def test_single_trip_line_is_local_only_when_model_unavailable(monkeypatch, unavailable):
    base = _base_day()
    text = f"{_md(base)}去海邊一日遊"
    if unavailable == "not_allowed":
        _no_model(monkeypatch)
    else:
        _model_unavailable(monkeypatch)

    receipt = main._maybe_extract_reminder(text, G, U_SENDER, "m-one")

    assert receipt.startswith("已新增提醒")
    assert [(_day_of(row[1]), row[0]) for row in _pending_rows()] == [
        (base, "去海邊一日遊")
    ]


def test_single_line_model_null_is_not_overridden(monkeypatch):
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *_a, **_k: None)

    result = main._maybe_extract_reminder(
        f"{_md(_base_day())}去海邊一日遊", G, U_SENDER, "m-null"
    )

    assert result is None
    assert _pending_rows() == []
    assert _queue_rows() == []


# ── explicit vs passive when nothing parses ──────────────────────────────────


def _local_parsers_fail(monkeypatch) -> None:
    for name in (
        "_explicit_single_reminder_result",
        "_explicit_range_reminder_result",
        "_explicit_month_reminder_result",
        "_calendar_regex_to_reminder_result",
    ):
        monkeypatch.setattr(main, name, lambda *_a, **_k: None)


def test_explicit_request_unparsed_while_model_down_is_queued_silently(monkeypatch):
    _model_unavailable(monkeypatch)
    _local_parsers_fail(monkeypatch)
    text = f"咪寶 提醒我{_md(_base_day())}處理報名表"

    result = main._maybe_extract_reminder(text, G, U_SENDER, "m-explicit")

    assert result is main._REMINDER_QUEUED_SILENTLY
    assert not result
    assert [row[1] for row in _queue_rows()] == ["pending"]


def test_explicit_silent_queue_closes_inbound_without_chat_or_reply(monkeypatch):
    _model_unavailable(monkeypatch)
    _local_parsers_fail(monkeypatch)
    _quiet_routing(monkeypatch)
    replies = MagicMock(return_value=True)
    explicit = MagicMock()
    completed = MagicMock(return_value=True)
    monkeypatch.setattr(main, "_reply", replies)
    monkeypatch.setattr(main, "_handle_explicit_text", explicit)
    monkeypatch.setattr(main, "_mark_inbound_reply_completed_no_reply", completed)

    main._handle_text_message(
        _text_event(
            f"咪寶 提醒我{_md(_base_day())}處理報名表", message_id="m-explicit-evt"
        ),
        G,
    )

    replies.assert_not_called()
    explicit.assert_not_called()
    main.burst_filter.add_to_burst.assert_not_called()
    assert completed.call_args.kwargs["message_ids"] == ["m-explicit-evt"]
    assert [row[1] for row in _queue_rows()] == ["pending"]


def test_passive_candidate_is_queued_silently_and_keeps_routing(monkeypatch):
    _model_unavailable(monkeypatch)
    _quiet_routing(monkeypatch)
    replies = MagicMock(return_value=True)
    monkeypatch.setattr(main, "_reply", replies)
    monkeypatch.setattr(main, "_auto_capture_text_if_important", lambda *_a, **_k: False)

    assert main._maybe_extract_reminder("下週三下午8點開會", G, U_SENDER, "m-p1") is None
    main._handle_text_message(_text_event("下週三晚上要聚餐", message_id="m-p2"), G)

    replies.assert_not_called()
    main.burst_filter.add_to_burst.assert_called_once()
    assert sorted(row[1] for row in _queue_rows()) == ["pending", "pending"]


def test_explicit_request_without_any_date_gets_an_honest_reply(monkeypatch):
    _no_model(monkeypatch)

    assert (
        main._maybe_extract_reminder("咪寶 提醒我買牛奶", G, U_SENDER, "m-nodate")
        == "尚未新增：請補上日期與事項。"
    )
    # GP1 r3 #1: not said to 咪寶, so production's None (routing goes on).
    assert main._maybe_extract_reminder("提醒我買牛奶", G, U_SENDER, "m-nodate-2") is None
    assert main._maybe_extract_reminder("記得帶傘", G, U_SENDER, "m-casual") is None
    assert _queue_rows() == []
    assert _pending_rows() == []


def _model_answers_null(monkeypatch) -> MagicMock:
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: True)
    stub = MagicMock(return_value=None)
    monkeypatch.setattr(gemini_client, "extract_reminder", stub)
    return stub


@pytest.mark.parametrize(
    "template, parsers_fail",
    [
        ("咪寶 提醒我下下週找時間剪頭髮", False),
        ("咪寶 提醒我{day}處理報名表", True),
    ],
)
def test_explicit_request_model_null_gets_the_date_format_hint(
    monkeypatch, template, parsers_fail
):
    """GP1 r2: an explicit request the model will not read is never chat's.

    GP1 r3 nit: the text has a date the bot could not read, so the reply
    shows the format instead of asking for a date the user already gave.
    """
    stub = _model_answers_null(monkeypatch)
    if parsers_fail:
        _local_parsers_fail(monkeypatch)
    text = template.format(day=_md(_base_day()))

    result = main._maybe_extract_reminder(text, G, U_SENDER, "m-explicit-null")

    assert stub.call_count == 1
    assert result == main._REMINDER_DATE_FORMAT_REPLY == _DATE_FORMAT
    assert _pending_rows() == []
    assert _queue_rows() == []


def test_passive_candidate_model_null_keeps_routing(monkeypatch):
    stub = _model_answers_null(monkeypatch)

    assert main._maybe_extract_reminder("下週三下午8點開會", G, U_SENDER, "m-p-null") is None
    assert stub.call_count == 1
    assert _pending_rows() == []
    assert _queue_rows() == []


def test_explicit_model_null_replies_honestly_without_a_chat_call(monkeypatch):
    _model_answers_null(monkeypatch)
    _quiet_routing(monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(
        main, "_reply", lambda _token, text, **_kw: sent.append(text) or True
    )
    chat = MagicMock(side_effect=AssertionError("must not reach chat"))
    monkeypatch.setattr(main, "_handle_explicit_text", chat)
    monkeypatch.setattr(
        main, "_llm_chat", MagicMock(side_effect=AssertionError("no chat model"))
    )

    main._handle_text_message(
        _text_event("咪寶 提醒我下下週找時間剪頭髮", message_id="m-haircut"), G
    )

    assert sent == [_DATE_FORMAT]
    chat.assert_not_called()
    main.burst_filter.add_to_burst.assert_not_called()
    assert _pending_rows() == []
    assert _queue_rows() == []


# ── explicit requests: the remaining exits never reach chat (GP1 r2, S2) ─────

_ONE_DATE = "尚未新增：一次請寫一個日期，或分行列出每個日期與事項。"
_ONE_TIME = "尚未新增：一次請寫一個時間與事項。"
_RESEND = "尚未新增：請傳送「提醒我＋完整日期＋事項」。"
_NEEDS_DATE = "尚未新增：請補上日期與事項。"
_PAST = "提醒時間已經過了，尚未新增。請傳送新的完整日期與事項。"
_TRY_AGAIN = "這次沒有新增提醒，請稍後再傳一次。"
_DATE_FORMAT = "尚未新增：看不懂這個日期，請寫成「10/23 下午3點 看牙醫」這樣的格式。"
_UNCONFIRMED = "這次無法確認提醒是否建立，請稍後查詢提醒清單或重試原本的要求。"
_EXPLICIT_TEXT = "咪寶 提醒我{day}處理報名表"
_PASSIVE_TEXT = "下週三下午8點開會"


def _model_answers(monkeypatch, result: dict) -> MagicMock:
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: True)
    stub = MagicMock(side_effect=lambda *_a, **_k: dict(result))
    monkeypatch.setattr(gemini_client, "extract_reminder", stub)
    return stub


def _fields(moment: datetime) -> dict:
    return {
        "action": "處理報名表",
        "year": moment.year,
        "month": moment.month,
        "day": moment.day,
        "hour": moment.hour,
        "minute": moment.minute,
    }


def _explicit_or_passive(kind: str) -> str:
    return (
        _EXPLICIT_TEXT.format(day=_md(_base_day()))
        if kind == "explicit"
        else _PASSIVE_TEXT
    )


@pytest.mark.parametrize(
    "text",
    [
        "咪寶 提醒我10/23和10/25去看牙醫",
        "咪寶 記得提醒我10/23跟10/25看牙醫",
        "咪寶 提醒我明天買牛奶，提醒我後天繳費",
    ],
)
def test_explicit_request_with_several_dates_gets_the_one_date_reply(monkeypatch, text):
    """Reviewer probe: the write gate stops it, and chat promised both days."""
    _no_model(monkeypatch)

    result = main._maybe_extract_reminder(text, G, U_SENDER, "m-two-dates")

    assert result == main._REMINDER_ONE_DATE_REPLY == _ONE_DATE
    assert _pending_rows() == []
    assert _queue_rows() == []


@pytest.mark.parametrize(
    "text", ["咪寶 提醒我明天早上和晚上吃藥", "咪寶 提醒我明天9點和10點開會"]
)
def test_explicit_request_with_several_times_gets_the_one_time_reply(monkeypatch, text):
    _no_model(monkeypatch)

    result = main._maybe_extract_reminder(text, G, U_SENDER, "m-two-times")

    assert result == main._REMINDER_ONE_TIME_REPLY == _ONE_TIME
    assert _pending_rows() == []
    assert _queue_rows() == []


@pytest.mark.parametrize(
    "text",
    [
        "咪寶 提醒我每天早上吃藥",
        "咪寶 提醒我",
        "咪寶 提醒我明天",
        "咪寶 提醒我2/30繳費",
        "咪寶 提醒我10/23出門前帶傘",
        "咪寶 幫我新增提醒 10/23 看牙醫",
    ],
)
def test_explicit_request_the_write_gate_refuses_gets_the_resend_reply(
    monkeypatch, text
):
    _no_model(monkeypatch)

    result = main._maybe_extract_reminder(text, G, U_SENDER, "m-gate")

    assert result == main._REMINDER_RESEND_FORMAT_REPLY == _RESEND
    assert _pending_rows() == []
    assert _queue_rows() == []


@pytest.mark.parametrize(
    "text",
    [
        "咪寶 提醒我明天開會 翻譯成英文",
        "咪寶 提醒我明天去看牙醫還是後天？",
        "請勿新增，明天提醒我領米",
        "明天提醒我領米，不用新增",
        "這不是命令，明天提醒我領米",
        "咪寶 提醒我10/23和10/25去看牙醫，只是舉例，不要新增",
    ],
)
def test_text_the_gate_reads_as_no_request_keeps_routing(monkeypatch, text):
    """A wording question or a revoked add is no request, whatever its shape."""
    _no_model(monkeypatch)

    assert main._maybe_extract_reminder(text, G, U_SENDER, "m-meta") is None
    assert _pending_rows() == []
    assert _queue_rows() == []


def test_explicit_request_with_a_date_but_no_action_hint_gets_the_needs_date_reply(
    monkeypatch,
):
    """Every explicit form names 提醒／記得／別忘 today; this pins the exit."""
    _no_model(monkeypatch)
    _local_parsers_fail(monkeypatch)
    monkeypatch.setattr(main, "_has_explicit_reminder_creation_intent", lambda *_a: True)
    text = f"咪寶 {_md(_base_day())} 牙醫"
    assert not main._REMINDER_TIME_OR_ACTION_HINT.search(text)

    result = main._maybe_extract_reminder(text, G, U_SENDER, "m-no-action")

    assert result == main._REMINDER_NEEDS_DATE_REPLY
    assert _pending_rows() == []
    assert _queue_rows() == []


@pytest.mark.parametrize("kind, expected", [("explicit", _PAST), ("passive", None)])
def test_model_time_already_past(monkeypatch, kind, expected):
    _local_parsers_fail(monkeypatch)
    stub = _model_answers(
        monkeypatch, _fields(datetime.now(TW).replace(second=0, microsecond=0) - timedelta(days=1))
    )

    result = main._maybe_extract_reminder(
        _explicit_or_passive(kind), G, U_SENDER, f"m-past-{kind}"
    )

    assert stub.call_count == 1
    assert result == expected
    if kind == "explicit":
        assert result == main._REMINDER_PAST_TIME_REPLY
    assert _pending_rows() == []
    assert _queue_rows() == []


@pytest.mark.parametrize("kind, expected", [("explicit", _DATE_FORMAT), ("passive", None)])
@pytest.mark.parametrize("broken", ["month_13", "no_hour"])
def test_model_result_with_unparseable_fields(monkeypatch, kind, expected, broken):
    _local_parsers_fail(monkeypatch)
    fields = _fields(datetime.combine(_base_day(), datetime.min.time()).replace(hour=9))
    if broken == "month_13":
        fields["month"] = 13
    else:
        fields.pop("hour")
    _model_answers(monkeypatch, fields)

    result = main._maybe_extract_reminder(
        _explicit_or_passive(kind), G, U_SENDER, f"m-bad-{kind}"
    )

    assert result == expected
    assert _pending_rows() == []
    assert _queue_rows() == []


@pytest.mark.parametrize("kind, expected", [("explicit", _DATE_FORMAT), ("passive", None)])
def test_queue_refusal_while_model_is_down(monkeypatch, kind, expected):
    _model_unavailable(monkeypatch)
    _local_parsers_fail(monkeypatch)
    monkeypatch.setattr(
        memory,
        "enqueue_pending_reminder",
        MagicMock(side_effect=sqlite3.OperationalError("database is locked")),
    )

    result = main._maybe_extract_reminder(
        _explicit_or_passive(kind), G, U_SENDER, f"m-noqueue-{kind}"
    )

    assert result == expected
    if kind == "explicit":
        # GP1 r3 nit: resending the same text cannot succeed; show the format.
        assert result == main._REMINDER_DATE_FORMAT_REPLY
    assert _pending_rows() == []
    assert _queue_rows() == []


@pytest.mark.parametrize("kind, expected", [("explicit", _TRY_AGAIN), ("passive", None)])
def test_internal_error_before_any_write(monkeypatch, kind, expected):
    import reminder_restatement

    _local_parsers_fail(monkeypatch)
    _model_answers(
        monkeypatch, _fields(datetime.combine(_base_day(), datetime.min.time()).replace(hour=9))
    )
    monkeypatch.setattr(
        reminder_restatement,
        "preserve_transit_details",
        MagicMock(side_effect=RuntimeError("boom")),
    )

    result = main._maybe_extract_reminder(
        _explicit_or_passive(kind), G, U_SENDER, f"m-boom-{kind}"
    )

    assert result == expected
    assert _pending_rows() == []


def test_internal_error_after_the_write_does_not_deny_the_reminder(monkeypatch):
    _local_parsers_fail(monkeypatch)
    _model_answers(
        monkeypatch, _fields(datetime.combine(_base_day(), datetime.min.time()).replace(hour=9))
    )
    monkeypatch.setattr(
        main,
        "_format_persisted_reminder_confirmation",
        MagicMock(side_effect=RuntimeError("boom")),
    )

    result = main._maybe_extract_reminder(
        _explicit_or_passive("explicit"), G, U_SENDER, "m-boom-after"
    )

    assert result == main._REMINDER_UNCONFIRMED_REPLY == _UNCONFIRMED
    assert [row[0] for row in _pending_rows()] == ["處理報名表"]


def _two_line_items(day: date, clock: str) -> list[dict]:
    return [
        {"date": day.isoformat(), "time": clock, "title": "去海邊一日遊", "daypart": None},
        {"date": day.isoformat(), "time": clock, "title": "市區自由行", "daypart": None},
    ]


@pytest.mark.parametrize(
    "case, expected",
    [("past_explicit", _PAST), ("write_fails_explicit", _TRY_AGAIN), ("past_passive", None)],
)
def test_schedule_list_that_writes_nothing(monkeypatch, case, expected):
    _no_model(monkeypatch)
    day = _base_day() if case == "write_fails_explicit" else _today() - timedelta(days=1)
    head = "咪寶 提醒我\n" if case.endswith("explicit") else ""
    text = f"{head}{_md(day)} 去海邊一日遊\n{_md(day)} 市區自由行"
    items = _two_line_items(day, "09:00")
    if case == "write_fails_explicit":
        monkeypatch.setattr(
            memory,
            "add_reminder_with_outcome",
            MagicMock(side_effect=sqlite3.OperationalError("database is locked")),
        )

    result = main._maybe_extract_reminder(
        text, G, U_SENDER, f"m-list-{case}", schedule_items=items
    )

    assert result == expected
    assert _pending_rows() == []


def test_explicit_two_date_request_replies_without_a_chat_call(monkeypatch):
    _no_model(monkeypatch)
    _quiet_routing(monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(
        main, "_reply", lambda _token, text, **_kw: sent.append(text) or True
    )
    chat = MagicMock(side_effect=AssertionError("must not reach chat"))
    monkeypatch.setattr(main, "_handle_explicit_text", chat)
    monkeypatch.setattr(
        main, "_llm_chat", MagicMock(side_effect=AssertionError("no chat model"))
    )

    main._handle_text_message(
        _text_event("咪寶 提醒我10/23和10/25去看牙醫", message_id="m-dentist"), G
    )

    assert sent == [_ONE_DATE]
    chat.assert_not_called()
    main.burst_filter.add_to_burst.assert_not_called()
    assert _pending_rows() == []
    assert _queue_rows() == []


# ── GP1 r3 #1: only a request made to 咪寶 gets a 「尚未新增」 reply ─────────
#
# A 「尚未新增…」 reply goes out only when the text is said to 咪寶 (named,
# @-mentioned or /ai), is not a question and is not said to someone else
# (「哥，…」).  Anything else keeps production's None and its routing.

# Reviewer probes: production (b18cf96) returned None for each and routed it
# as listed (probed with the same handler stubs as below).
_LOOKALIKE_ROUTES = [
    ("咪寶 提醒我一下那家店叫什麼", [("explicit", "提醒我一下那家店叫什麼")]),
    ("提醒我一下那家店叫什麼？", ["burst"]),
    ("提醒我一下，餐廳是哪一家", [("research", "提醒我一下，餐廳是哪一家")]),
    ("哥，提醒我等一下要打電話給阿姨", ["burst"]),
    ("記得提醒我喔", ["burst"]),
    ("提醒我們不要再吵了", ["burst"]),
]


def _record_routes(monkeypatch) -> list:
    """Where the handler sends a text: reply, chat, research, burst, closed."""
    _quiet_routing(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        main.burst_filter, "add_to_burst", lambda *_a, **_k: calls.append("burst")
    )
    monkeypatch.setattr(
        main, "_reply", lambda _token, text, **_kw: calls.append(("reply", text)) or True
    )
    monkeypatch.setattr(
        main, "_handle_explicit_text", lambda _e, _g, text: calls.append(("explicit", text))
    )
    monkeypatch.setattr(
        main,
        "_handle_web_research_question",
        lambda _e, _g, text, **_kw: calls.append(("research", text)) or True,
    )
    monkeypatch.setattr(main, "_handle_todo_query", lambda *_a, **_k: calls.append("todo"))
    monkeypatch.setattr(
        main, "_handle_calendar_query", lambda *_a, **_k: calls.append("calendar")
    )
    monkeypatch.setattr(
        main,
        "_mark_inbound_reply_completed_no_reply",
        lambda *_a, **_k: calls.append("closed") or True,
    )
    monkeypatch.setattr(main, "_auto_capture_text_if_important", lambda *_a, **_k: False)
    return calls


@pytest.mark.parametrize("model", ["null", "down"])
@pytest.mark.parametrize(
    "text",
    [
        *(text for text, _route in _LOOKALIKE_ROUTES),
        # said to 咪寶 but asked as a question, or said to someone else
        # (GP1 r4 #4: 「咪寶 可以提醒我買牛奶嗎」 is a polite request, see below)
        "咪寶 提醒我誰要來",
        "咪寶 提醒我幾點出門比較好？",
        "咪寶 提醒我幾點出門比較好",
        "咪寶 可以提醒我那家店叫什麼嗎",
        "咪寶 可以提醒我幾點出門嗎",
        "咪寶 提醒我幾號繳學費",
        "咪寶 提醒我幾月要換駕照",
        "咪寶 提醒我一下呢",
        "咪寶 提醒我月底繳房租，你覺得呢",
        "咪寶 哥，提醒我等一下要打電話給阿姨",
        "哥，咪寶提醒我買牛奶",
        # not said to 咪寶 at all
        "提醒我買牛奶",
        "提醒我月底繳房租",
        "提醒我10分鐘後關火",
    ],
)
def test_lookalike_request_gets_no_honest_reply(monkeypatch, text, model):
    if model == "null":
        _model_answers_null(monkeypatch)
    else:
        _model_unavailable(monkeypatch)
    assert main._has_explicit_reminder_creation_intent(text)

    assert main._maybe_extract_reminder(text, G, U_SENDER, "m-lookalike") is None
    assert _pending_rows() == []
    assert _queue_rows() == []


@pytest.mark.parametrize("model", ["null", "down"])
@pytest.mark.parametrize(
    "text", ["提醒我每個月5號繳卡費", "提醒我明天跟後天都要澆花", "哥，提醒我明天跟後天都要澆花"]
)
def test_lookalike_request_with_a_date_hint_is_handled_like_production(
    monkeypatch, text, model
):
    """Not said to 咪寶: None, so routing goes on (the queue keeps a passive copy)."""
    if model == "null":
        _model_answers_null(monkeypatch)
    else:
        _model_unavailable(monkeypatch)

    result = main._maybe_extract_reminder(text, G, U_SENDER, "m-lookalike-date")

    assert result is None and result is not main._REMINDER_QUEUED_SILENTLY
    assert _pending_rows() == []
    expected_queue = ["pending"] if model == "down" else []
    assert [row[1] for row in _queue_rows()] == expected_queue


@pytest.mark.parametrize("text, route", _LOOKALIKE_ROUTES)
def test_lookalike_request_routes_as_production(monkeypatch, text, route):
    _model_answers_null(monkeypatch)
    calls = _record_routes(monkeypatch)

    main._handle_text_message(_text_event(text, message_id="m-lookalike-evt"), G)

    assert calls == route
    assert not any("尚未新增" in str(call) for call in calls)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("咪寶 提醒我買牛奶", _NEEDS_DATE),
        ("咪寶，提醒我買牛奶", _NEEDS_DATE),
        ("@咪寶 提醒我買牛奶", _NEEDS_DATE),
        ("提醒我買牛奶，咪寶", _NEEDS_DATE),
        ("咪寶 提醒我下下週找時間剪頭髮", _DATE_FORMAT),
    ],
)
def test_request_said_to_mibao_keeps_its_honest_reply(monkeypatch, text, expected):
    _model_answers_null(monkeypatch)

    assert main._maybe_extract_reminder(text, G, U_SENDER, "m-to-mibao") == expected
    assert _pending_rows() == []
    assert _queue_rows() == []


def _mentioned_event(text: str, name: str = "@家庭幫手"):
    """A LINE mention of the bot under a display name that is not 咪寶."""
    event = _text_event(text, message_id="m-mention")
    start = text.index(name)
    event.message.mention = SimpleNamespace(
        mentionees=[SimpleNamespace(is_self=True, index=start, length=len(name))]
    )
    return event


@pytest.mark.parametrize(
    "text, route",
    [
        # The handler's own address test (the mention data) decides, not the text.
        ("提醒我買牛奶 @家庭幫手", [("reply", _NEEDS_DATE)]),
        # Mentioned, but said to 哥: production's routing (chat with the text).
        ("哥，提醒我買牛奶 @家庭幫手", [("explicit", "哥，提醒我買牛奶")]),
    ],
)
def test_a_line_mention_of_the_bot_counts_as_said_to_mibao(monkeypatch, text, route):
    _model_answers_null(monkeypatch)
    calls = _record_routes(monkeypatch)

    main._handle_text_message(_mentioned_event(text), G)

    assert calls == route


def test_the_same_words_without_the_mention_route_on(monkeypatch):
    _model_answers_null(monkeypatch)
    calls = _record_routes(monkeypatch)

    main._handle_text_message(_text_event("提醒我買牛奶 @家庭幫手", message_id="m-nomention"), G)

    assert calls == ["burst"]


@pytest.mark.parametrize(
    "text", ["提醒我{day}下午3點看牙醫", "咪寶 提醒我{day}下午3點看牙醫"]
)
def test_a_dated_request_is_created_whoever_it_is_said_to(monkeypatch, text):
    _model_answers_null(monkeypatch)
    day = _base_day()

    receipt = main._maybe_extract_reminder(
        text.format(day=_md(day)), G, U_SENDER, "m-dated"
    )

    assert str(receipt).startswith("已新增提醒")
    assert [(row[0], _day_of(row[1]), _clock_of(row[1])) for row in _pending_rows()] == [
        ("看牙醫", day, "15:00")
    ]


# ── GP1 r3 nits: a date the bot cannot read gets the format, not 「補上日期」 ──


@pytest.mark.parametrize(
    "text",
    [
        "咪寶 提醒我月底繳房租",
        "咪寶 提醒我每個月5號繳卡費",
        "咪寶 提醒我明天跟後天都要澆花",
        "咪寶 提醒我10分鐘後關火",
        "咪寶 提醒我等一下要打電話給阿姨",
        "咪寶 提醒我中秋節前訂月餅",
    ],
)
def test_a_date_the_bot_cannot_read_gets_the_format_hint(monkeypatch, text):
    _model_answers_null(monkeypatch)

    result = main._maybe_extract_reminder(text, G, U_SENDER, "m-unreadable")

    assert result == main._REMINDER_DATE_FORMAT_REPLY == _DATE_FORMAT
    assert "補上日期" not in result
    assert _pending_rows() == []
    assert _queue_rows() == []


@pytest.mark.parametrize("text", ["咪寶 提醒我買中秋禮盒", "咪寶 提醒我打電話給清明叔叔"])
def test_a_request_with_no_date_still_asks_for_one(monkeypatch, text):
    """A holiday-looking word that is no date keeps the ask for a date."""
    _model_answers_null(monkeypatch)

    result = main._maybe_extract_reminder(text, G, U_SENDER, "m-no-date")

    assert result == main._REMINDER_NEEDS_DATE_REPLY == _NEEDS_DATE


def test_a_request_the_queue_will_not_take_gets_the_format_hint(monkeypatch):
    """Model down and the queue refuses it: resending cannot help, the format can."""
    _model_unavailable(monkeypatch)
    _local_parsers_fail(monkeypatch)
    monkeypatch.setattr(main, "_enqueue_reminder_if_candidate", lambda *_a, **_k: None)

    result = main._maybe_extract_reminder(
        _EXPLICIT_TEXT.format(day=_md(_base_day())), G, U_SENDER, "m-not-queued"
    )

    assert result == _DATE_FORMAT
    assert "稍後再傳" not in result
    assert _pending_rows() == []


# ── GP1 r4 #4: a polite ask, a vague date or a soft 呢 is still a request ───
#
# 「可以提醒我…嗎」 asks 咪寶 to do it, 幾 in 十幾號／幾天後 is a number and a
# 呢 after 提醒我 and its task only softens it.  These went to chat, where a
# reply such as 「好的！月底會提醒你繳房租喔～」 promised a reminder that does
# not exist.  Any other question word still makes it a question, and so does
# a 呢 after something to recall rather than a dated task (「提醒我密碼呢」
# 「提醒我今天的行程呢」).

_POLITE_OR_VAGUE_REQUESTS = [
    ("咪寶 可以提醒我月底繳房租嗎", _DATE_FORMAT),
    ("咪寶 可以提醒我月底繳房租嗎？", _DATE_FORMAT),
    ("咪寶 能不能提醒我月底繳房租？", _DATE_FORMAT),
    ("咪寶 麻煩提醒我月底繳房租好嗎", _DATE_FORMAT),
    ("咪寶 提醒我月底繳房租好嗎？", _DATE_FORMAT),
    ("可以提醒我月底繳房租嗎，咪寶", _DATE_FORMAT),
    ("咪寶 可以提醒我嗎？我月底要繳房租", _DATE_FORMAT),
    ("咪寶 提醒我幾天後回診", _DATE_FORMAT),
    ("咪寶 可以提醒我幾天後回診嗎", _DATE_FORMAT),
    ("咪寶 提醒我十幾號繳學費", _DATE_FORMAT),
    ("咪寶 提醒我二十幾號繳卡費", _DATE_FORMAT),
    ("咪寶 提醒我過幾天回診", _DATE_FORMAT),
    ("咪寶 提醒我幾個月後換濾心", _DATE_FORMAT),
    ("咪寶 提醒我下下週找時間剪頭髮呢", _DATE_FORMAT),
    ("咪寶 提醒我月底繳房租呢～", _DATE_FORMAT),
    ("咪寶 提醒我三點開會呢", _NEEDS_DATE),
    ("咪寶 提醒我十幾號繳學費呢", _DATE_FORMAT),
    ("咪寶 提醒我幾天後回診呢", _DATE_FORMAT),
    ("咪寶 可以提醒我買牛奶嗎", _NEEDS_DATE),
]


@pytest.mark.parametrize("text", [text for text, _reply in _POLITE_OR_VAGUE_REQUESTS])
def test_polite_asks_vague_dates_and_a_soft_ne_are_requests_to_mibao(text):
    assert main._is_reminder_request_to_bot(text)


@pytest.mark.parametrize(
    "text",
    [
        "咪寶 提醒我一下那家店叫什麼",
        "咪寶 你會提醒我嗎？",
        "咪寶 提醒我幾點出門比較好？",
        "咪寶 提醒我幾點出門比較好",
        "咪寶 提醒我誰要來",
        "咪寶 可以提醒我那家店叫什麼嗎",
        "咪寶 可以提醒我幾點出門嗎",
        "咪寶 提醒我幾號繳學費",
        "咪寶 提醒我幾月要換駕照",
        "咪寶 那提醒我呢",
        "咪寶 提醒我的事呢",
        "咪寶 提醒我一下呢",
        "咪寶 提醒我月底繳房租，你覺得呢",
        "咪寶 提醒我那件事呢",
        "咪寶 提醒我密碼呢",
        "咪寶 提醒我待辦呢",
        "咪寶 提醒我今天的行程呢",
        "咪寶 提醒我月底的行程呢",
        "咪寶 提醒我月底要繳的費用呢",
        "咪寶 請問提醒我的時間是？",
        "咪寶 哥，可以提醒我月底繳房租嗎",
        "提醒我一下，餐廳是哪一家",
        "可以提醒我月底繳房租嗎",
    ],
)
def test_questions_and_words_to_someone_else_stay_as_they_were(text):
    assert not main._is_reminder_request_to_bot(text)


@pytest.mark.parametrize("model", ["null", "down"])
@pytest.mark.parametrize("text, expected", _POLITE_OR_VAGUE_REQUESTS)
def test_polite_or_vague_request_to_mibao_gets_an_honest_reply(
    monkeypatch, text, expected, model
):
    if model == "null":
        _model_answers_null(monkeypatch)
    else:
        _model_unavailable(monkeypatch)
    assert main._has_explicit_reminder_creation_intent(text)

    assert main._maybe_extract_reminder(text, G, U_SENDER, "m-polite") == expected
    assert _pending_rows() == []
    assert _queue_rows() == []


@pytest.mark.parametrize(
    "text",
    [
        "咪寶 可以提醒我月底繳房租嗎",
        "咪寶 提醒我幾天後回診",
        "咪寶 提醒我十幾號繳學費",
        "咪寶 提醒我下下週找時間剪頭髮呢",
    ],
)
def test_the_reviewed_requests_reply_honestly_without_a_chat_call(monkeypatch, text):
    _model_answers_null(monkeypatch)
    calls = _record_routes(monkeypatch)
    monkeypatch.setattr(
        main, "_llm_chat", MagicMock(side_effect=AssertionError("no chat model"))
    )

    main._handle_text_message(_text_event(text, message_id="m-polite-evt"), G)

    assert calls == [("reply", _DATE_FORMAT)]
    assert _pending_rows() == []


@pytest.mark.parametrize(
    "text, route",
    [
        ("咪寶 提醒我幾點出門比較好？", [("explicit", "提醒我幾點出門比較好？")]),
        ("咪寶 你會提醒我嗎？", ["todo"]),
        ("咪寶 可以提醒我那家店叫什麼嗎", [("explicit", "可以提醒我那家店叫什麼嗎")]),
        ("咪寶 提醒我幾號繳學費", [("explicit", "提醒我幾號繳學費")]),
        ("咪寶 提醒我密碼呢", [("explicit", "提醒我密碼呢")]),
        ("咪寶 提醒我待辦呢", [("explicit", "提醒我待辦呢")]),
        ("咪寶 提醒我月底的行程呢", [("explicit", "提醒我月底的行程呢")]),
    ],
)
def test_a_question_to_mibao_keeps_its_routing(monkeypatch, text, route):
    _model_answers_null(monkeypatch)
    calls = _record_routes(monkeypatch)

    main._handle_text_message(_text_event(text, message_id="m-question-evt"), G)

    assert calls == route


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("咪寶 " + "可以提醒我" * 99, id="ask-run"),
        pytest.param("咪寶 可以" + "提醒" * 240 + "嗎？", id="remind-run"),
        pytest.param("咪寶 " + "提醒我" * 165 + "呢", id="remind-me-ne"),
        pytest.param("咪寶 " + "提醒我的" * 124 + "呢", id="remind-me-de-ne"),
        pytest.param("咪寶 " + "十幾" * 248, id="teens-run"),
        pytest.param("咪寶 " + "幾" * 480 + "天後", id="ji-run"),
        pytest.param("咪寶 " + "幾 " * 245 + "天", id="ji-space-run"),
        pytest.param("咪寶 " + "麻煩" * 120 + "提醒我" + "嗎" * 100, id="mafan-ma-run"),
        pytest.param("咪寶 可以提醒我" + " " * 480 + "呢", id="space-ne"),
        pytest.param("咪寶 提醒我" + "十" * 480 + "呢", id="numeral-ne"),
        pytest.param("咪寶 提醒我" + "1 " * 240 + "呢", id="digit-space-ne"),
        pytest.param("咪寶 提醒我" + "下" * 480 + "呢", id="xia-ne"),
    ],
)
def test_the_question_test_stays_fast_on_long_repetitive_text(text):
    assert len(text) <= 500
    start = time.perf_counter()
    main._is_reminder_request_to_bot(text, addressed=True)
    assert time.perf_counter() - start < 0.05


def test_queued_and_failure_receipt_helpers_are_gone():
    assert not hasattr(main, "_format_queued_reminder_confirmation")
    assert not hasattr(main, "_PENDING_REMINDER_FAILURE_CONFIRMATION")
    assert not hasattr(main, "_drop_pending_reminder_with_terminal_receipt")
    assert not hasattr(memory, "drop_pending_reminder_with_confirmation")


def test_reminder_receipt_is_a_plain_string_with_ids():
    receipt = main.ReminderReceipt("已新增提醒", [3, 5])

    assert isinstance(receipt, str)
    assert receipt == "已新增提醒"
    assert receipt.strip() == "已新增提醒"
    assert receipt.reminder_ids == (3, 5)


# ── receipt delivery and stage consumption ───────────────────────────────────


@pytest.mark.parametrize("delivered", [True, False])
def test_itinerary_message_skips_calendar_and_consumes_only_after_delivery(
    monkeypatch, delivered
):
    _no_model(monkeypatch)
    _quiet_routing(monkeypatch)
    monkeypatch.setattr(
        main,
        "_auto_capture_text_if_important",
        MagicMock(side_effect=AssertionError("calendar capture must be skipped")),
    )
    monkeypatch.setattr(
        main,
        "_handle_explicit_text",
        MagicMock(side_effect=AssertionError("must not reach chat")),
    )
    sent: list[str] = []
    monkeypatch.setattr(
        main, "_reply", lambda _token, text, **_kw: sent.append(text) or delivered
    )
    consumed: list[tuple[list[int], int]] = []
    monkeypatch.setattr(
        memory,
        "consume_open_stages",
        lambda ids, now: consumed.append((list(ids), now)),
        raising=False,
    )

    main._handle_text_message(
        _text_event(_trip_text(_base_day()), message_id="m-trip-evt"), G
    )

    rows = _pending_rows()
    assert len(rows) == 4
    assert len(sent) == 1 and sent[0].startswith("已新增 4 筆提醒")
    main.burst_filter.add_to_burst.assert_not_called()
    if delivered:
        assert len(consumed) == 1
        assert sorted(consumed[0][0]) == sorted(row[6] for row in rows)
        assert abs(consumed[0][1] - time.time()) < 60
    else:
        assert consumed == []


# ── quoted capture ────────────────────────────────────────────────────────────


def _capture_setup(monkeypatch):
    _no_model(monkeypatch)
    _quiet_routing(monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(main, "_reply", lambda _token, text, **_kw: sent.append(text) or True)
    explicit = MagicMock()
    monkeypatch.setattr(main, "_handle_explicit_text", explicit)
    monkeypatch.setattr(memory, "consume_open_stages", lambda *_a, **_k: None, raising=False)
    return sent, explicit


@pytest.mark.parametrize("command", ["咪寶", "@咪寶 記一下", "幫我加提醒", "咪寶 新增提醒"])
def test_quote_plus_name_or_record_verb_captures_for_original_sender(
    monkeypatch, command
):
    sent, explicit = _capture_setup(monkeypatch)
    source_text = _trip_text(_base_day())
    _seed_raw(source_text)

    main._handle_text_message(
        _text_event(command, message_id="m-cmd", user_id=U_OTHER, quoted="m-src"), G
    )

    rows = _pending_rows()
    assert len(rows) == 4
    assert {row[2] for row in rows} == {U_SENDER}
    assert {row[3] for row in rows} == {memory._normalize_reminder_text(source_text)}
    assert sorted(row[5] for row in rows) == [f"m-src:{index}" for index in range(4)]
    assert len(sent) == 1 and sent[0].startswith("已新增 4 筆提醒")
    explicit.assert_not_called()


def test_quote_of_non_schedule_text_falls_through_to_explicit(monkeypatch):
    sent, explicit = _capture_setup(monkeypatch)
    _seed_raw("因為港式很油")

    main._handle_text_message(
        _text_event("咪寶", message_id="m-cmd", user_id=U_OTHER, quoted="m-src"), G
    )

    assert _pending_rows() == []
    explicit.assert_called_once()


def test_quote_capture_works_for_the_sister_too(monkeypatch):
    """2026-10-09：取消妹妹零回覆後，引用她的行程＋「咪寶」也照樣幫她記。"""
    sent, explicit = _capture_setup(monkeypatch)
    monkeypatch.setattr(main.line_mentions, "user_id_for_family_role", lambda _role: "U_SISTER")
    _seed_raw(_trip_text(_base_day()), user_id="U_SISTER")

    main._handle_text_message(
        _text_event("咪寶", message_id="m-cmd", user_id=U_OTHER, quoted="m-src"), G
    )

    rows = _pending_rows()
    assert len(rows) == 4 and {row[2] for row in rows} == {"U_SISTER"}
    assert len(sent) == 1 and sent[0].startswith("已新增 4 筆提醒")
    explicit.assert_not_called()


@pytest.mark.parametrize("variant", ["bot", "old", "media"])
def test_quote_capture_refuses_bot_old_or_media_sources(monkeypatch, variant):
    sent, explicit = _capture_setup(monkeypatch)
    text = _trip_text(_base_day())
    if variant == "bot":
        _seed_raw(text, user_id="__bot__")
    elif variant == "old":
        _seed_raw(text, age_sec=15 * 86400)
    else:
        _seed_raw("[圖片]")

    main._handle_text_message(
        _text_event("咪寶", message_id="m-cmd", user_id=U_OTHER, quoted="m-src"), G
    )

    assert _pending_rows() == []
    assert not any(text_sent.startswith("已新增") for text_sent in sent)


def test_requote_reports_existing_without_duplicates(monkeypatch):
    sent, _explicit = _capture_setup(monkeypatch)
    _seed_raw(_trip_text(_base_day()))

    main._handle_text_message(
        _text_event("咪寶", message_id="m-cmd-1", user_id=U_OTHER, quoted="m-src"), G
    )
    main._handle_text_message(
        _text_event("咪寶 記一下", message_id="m-cmd-2", user_id=U_OTHER, quoted="m-src"),
        G,
    )

    assert len(_pending_rows()) == 4
    assert sent[1].startswith("4 筆提醒皆已存在")


def test_quote_uses_source_day_and_skips_items_already_past(monkeypatch):
    sent, _explicit = _capture_setup(monkeypatch)
    yesterday = _today() - timedelta(days=1)
    base = _base_day()
    _seed_raw(
        f"{_md(yesterday)}去海邊一日遊\n{_md(base)}跟團一日遊",
        age_sec=3 * 86400,
    )

    main._handle_text_message(
        _text_event("咪寶", message_id="m-cmd", user_id=U_OTHER, quoted="m-src"), G
    )

    assert [(_day_of(row[1]), row[0]) for row in _pending_rows()] == [
        (base, "跟團一日遊")
    ]
    assert sent[0].startswith("已新增提醒")


# ── silent pending drops ──────────────────────────────────────────────────────


def _drain_ready(monkeypatch) -> None:
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)


def test_drop_pending_reminder_requires_claim_and_known_reason():
    pending_id = memory.enqueue_pending_reminder(G, U_SENDER, "下週三開會", "m-drop")
    claim_token = memory.claim_pending_reminder(pending_id)

    with pytest.raises(ValueError):
        memory.drop_pending_reminder(pending_id, claim_token, G, "because")
    assert memory.drop_pending_reminder(pending_id, "wrong-token", G, "no_date") is False
    assert memory.drop_pending_reminder(pending_id, claim_token, G, "no_date") is True

    status, dropped_at, reason = _queue_state(pending_id)
    assert (status, reason) == ("dropped", "no_date")
    assert dropped_at > 0
    assert _outbox_count() == 0


def test_drain_model_null_drops_silently_with_reason(monkeypatch):
    _drain_ready(monkeypatch)
    pending_id = memory.enqueue_pending_reminder(G, U_SENDER, "下週三下午3點開會", "m-null")
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "_calendar_regex_to_reminder_result", lambda *_a, **_k: None)

    main._drain_pending_reminders(G)

    status, dropped_at, reason = _queue_state(pending_id)
    assert (status, reason) == ("dropped", "model_null") and dropped_at > 0
    assert _outbox_count() == 0


def test_drain_expired_result_drops_silently(monkeypatch):
    _drain_ready(monkeypatch)
    pending_id = memory.enqueue_pending_reminder(G, U_SENDER, "下週三下午3點開會", "m-exp")
    past = datetime.now(TW) - timedelta(days=2)
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *_a, **_k: {
            "action": "開會",
            "year": past.year,
            "month": past.month,
            "day": past.day,
            "hour": 15,
            "minute": 0,
        },
    )

    main._drain_pending_reminders(G)

    status, _dropped_at, reason = _queue_state(pending_id)
    assert (status, reason) == ("dropped", "expired")
    assert _outbox_count() == 0


def test_stale_cleanup_drops_silently():
    pending_id = memory.enqueue_pending_reminder(G, U_SENDER, "下週三下午3點開會", "m-stale")
    with memory._conn() as c:
        c.execute(
            "UPDATE pending_reminder_extract SET created_at=? WHERE pending_id=?",
            (int(time.time()) - main._PENDING_MAX_AGE_SEC - 60, pending_id),
        )

    assert memory.drop_stale_pending_reminders(main._PENDING_MAX_AGE_SEC, G) == 1

    status, dropped_at, reason = _queue_state(pending_id)
    assert (status, reason) == ("dropped", "stale") and dropped_at > 0
    assert _outbox_count() == 0


def test_drain_keeps_rows_queued_while_model_is_unavailable(monkeypatch):
    _drain_ready(monkeypatch)
    first = memory.enqueue_pending_reminder(G, U_SENDER, "下週三下午3點開會", "m-u1")
    second = memory.enqueue_pending_reminder(G, U_SENDER, "下週四下午3點開會", "m-u2")
    stub = MagicMock(side_effect=RuntimeError("503 UNAVAILABLE"))
    monkeypatch.setattr(gemini_client, "extract_reminder", stub)

    main._drain_pending_reminders(G)

    assert stub.call_count == 1
    assert _queue_state(first)[0] == "pending"
    assert _queue_state(second)[0] == "pending"
    assert _outbox_count() == 0


def test_drain_success_sends_no_receipt_and_consumes_nothing(monkeypatch):
    _drain_ready(monkeypatch)
    pending_id = memory.enqueue_pending_reminder(G, U_SENDER, "下週三下午3點開會", "m-ok")
    future = datetime.now(TW) + timedelta(days=5)
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *_a, **_k: {
            "action": "開會",
            "year": future.year,
            "month": future.month,
            "day": future.day,
            "hour": 15,
            "minute": 0,
        },
    )
    consume = MagicMock()
    monkeypatch.setattr(memory, "consume_open_stages", consume, raising=False)

    main._drain_pending_reminders(G)

    assert [row[0] for row in _pending_rows()] == ["開會"]
    assert _queue_state(pending_id)[0] == "done"
    assert _outbox_count() == 0
    consume.assert_not_called()


# ── follow-up 「加到提醒」 on a queued source ─────────────────────────────────


def _seed_followup(queue_status: str) -> int:
    now = int(time.time())
    with memory._conn() as c:
        c.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, 'm-fsrc', ?, '明天下午3點看牙醫', ?)",
            (G, U_SENDER, now - 30),
        )
        c.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, 'm-fcmd', ?, '加到提醒', ?)",
            (G, U_SENDER, now),
        )
    pending_id = memory.enqueue_pending_reminder(G, U_SENDER, "明天下午3點看牙醫", "m-fsrc")
    if queue_status == "processing":
        assert memory.claim_pending_reminder(pending_id)
    return pending_id


def test_followup_on_queued_source_creates_and_closes_queue_row(monkeypatch):
    pending_id = _seed_followup("pending")

    reply = main._creation_followup_reply(
        _text_event("加到提醒", message_id="m-fcmd", quoted="m-fsrc"), G, "加到提醒"
    )

    assert reply.startswith("已新增提醒")
    assert isinstance(reply, main.ReminderReceipt) and len(reply.reminder_ids) == 1
    assert [row[0] for row in _pending_rows()] == ["看牙醫"]
    assert _queue_state(pending_id)[0] == "done"


def test_followup_on_processing_queue_row_gets_neutral_reply(monkeypatch):
    _seed_followup("processing")

    reply = main._creation_followup_reply(
        _text_event("加到提醒", message_id="m-fcmd", quoted="m-fsrc"), G, "加到提醒"
    )

    assert reply == "這則提醒正在處理中，請稍後查看提醒清單。"
    assert _pending_rows() == []


def test_followup_receipt_consumes_open_stages_after_delivery(monkeypatch):
    _seed_followup("pending")
    consumed: list[list[int]] = []
    monkeypatch.setattr(
        memory,
        "consume_open_stages",
        lambda ids, now: consumed.append(list(ids)),
        raising=False,
    )
    monkeypatch.setattr(main, "_reply", lambda *_a, **_k: True)
    monkeypatch.setattr(main.burst_filter, "cancel_burst", MagicMock(return_value=[]))

    handled = main._try_handle_creation_followup(
        _text_event("加到提醒", message_id="m-fcmd", quoted="m-fsrc"), G, "加到提醒"
    )

    assert handled is True
    assert consumed == [[_pending_rows()[0][6]]]


# ── daily audit of dropped rows ──────────────────────────────────────────────


def _audit_db(path: Path, rows: list[tuple]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE pending_reminder_extract ("
            "pending_id INTEGER PRIMARY KEY, group_id TEXT, user_id TEXT, "
            "message_id TEXT, text TEXT, created_at INTEGER, retries INTEGER, "
            "claimed_at INTEGER, claim_token TEXT, status TEXT, "
            "dropped_at INTEGER NOT NULL DEFAULT 0, drop_reason TEXT NOT NULL DEFAULT '')"
        )
        conn.executemany(
            "INSERT INTO pending_reminder_extract(pending_id, group_id, user_id, "
            "message_id, text, created_at, retries, claimed_at, claim_token, status, "
            "dropped_at, drop_reason) VALUES (?, 'G-AUDIT', 'U-AUDIT', ?, ?, 1, 0, 0, '', ?, ?, ?)",
            rows,
        )


def test_daily_audit_lists_recent_dropped_ids_without_text(tmp_path):
    now = 1_800_000_000
    db = tmp_path / "audit.db"
    _audit_db(
        db,
        [
            (1, "m-a", "SECRET-TEXT-A", "dropped", now - 3600, "expired"),
            (2, "m-b", "SECRET-TEXT-B", "dropped", now - 3 * 86400, "stale"),
            (3, "m-c", "SECRET-TEXT-C", "pending", 0, ""),
            (4, "m-d", "SECRET-TEXT-D", "dropped", now - 60, "model_null"),
        ],
    )

    rows = dpa.load_dropped_reminder_rows(db, now=now)
    message = dpa.format_dropped_reminders(rows, now=now)

    assert [row.pending_id for row in rows] == [1, 4]
    assert "pid=1" in message and "pid=4" in message
    assert "expired" in message and "model_null" in message
    assert "SECRET" not in message
    assert "G-AUDIT" not in message and "m-a" not in message


def test_daily_audit_reports_dropped_rows_even_when_pending_reply_disabled(
    tmp_path, monkeypatch
):
    pending_id = memory.enqueue_pending_reminder(G, U_SENDER, "SECRET 下週三開會", "m-audit")
    claim_token = memory.claim_pending_reminder(pending_id)
    assert memory.drop_pending_reminder(pending_id, claim_token, G, "no_date")
    sent: list[str] = []
    monkeypatch.setattr(dpa, "pending_reply_enabled", lambda: False)
    monkeypatch.setattr(dpa, "_send_discord", lambda message: sent.append(message) or True)
    monkeypatch.setattr(dpa, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(dpa, "STATE_PATH", tmp_path / "state" / "x.json")

    assert dpa.main([]) == 0

    assert len(sent) == 1
    assert f"pid={pending_id}" in sent[0] and "no_date" in sent[0]
    assert "SECRET" not in sent[0] and G not in sent[0]


# ── burst calendar capture skips reminder-owned messages ─────────────────────


def _burst_patches(monkeypatch, llm_reply: str) -> MagicMock:
    from contextlib import nullcontext

    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: False)
    monkeypatch.setattr(main.memory, "get_context", lambda _gid: [])
    monkeypatch.setattr(main.memory, "check_fact_cache", lambda *_args: None)
    monkeypatch.setattr(main.memory, "top_facts", lambda _gid: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda _gid: [])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: text)
    monkeypatch.setattr(main, "_is_market_quote_request", lambda *_a, **_kw: False)
    monkeypatch.setattr(main, "_requires_public_research", lambda *_a, **_kw: False)
    monkeypatch.setattr(main, "_thinking_indicator", lambda _gid: nullcontext())
    monkeypatch.setattr(main, "_llm_chat", lambda *_a, **_kw: llm_reply)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_enforce_new_value_reply", lambda text, **_kw: text)
    monkeypatch.setattr(main.memory, "store_fact_cache", lambda *_a, **_kw: None)
    monkeypatch.setattr(main.memory, "append_turn", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "_maybe_extract_facts", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "_start_burst_finance_extraction", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "_reply", lambda *_a, **_kw: True)
    capture = MagicMock()
    monkeypatch.setattr(main, "_maybe_capture_calendar_event", capture)
    return capture


@pytest.mark.parametrize("llm_reply", ["new value", ""])
def test_burst_calendar_capture_skips_queued_reminder_message(monkeypatch, llm_reply):
    capture = _burst_patches(monkeypatch, llm_reply)
    memory.enqueue_pending_reminder(G, U_SENDER, "下週三晚上要聚餐", "m-burst")

    main._handle_burst_flush(G, "下週三晚上要聚餐", "TOKEN-B", ["m-burst"])

    capture.assert_not_called()


@pytest.mark.parametrize("llm_reply", ["new value", ""])
def test_burst_calendar_capture_still_runs_for_other_messages(monkeypatch, llm_reply):
    capture = _burst_patches(monkeypatch, llm_reply)

    main._handle_burst_flush(G, "下週三晚上要聚餐", "TOKEN-B", ["m-burst-other"])

    capture.assert_called_once()


# ── a receipt never carries its own reminders' due stage (Andrew 2026-10-04) ──


def test_receipt_reply_ref_lists_ids_without_binding_one_reminder():
    assert main._receipt_reply_ref("已新增提醒") is None
    assert main._receipt_reply_ref(main.ReminderReceipt("已新增提醒", ())) is None
    ref = main._receipt_reply_ref(main.ReminderReceipt("已新增 2 筆提醒", (7, 9)))
    assert ref == {"reminder_ids": [7, 9]}


def _ref_capture(monkeypatch) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        main, "_reply", lambda _token, text, **kw: calls.append((text, kw)) or True
    )
    monkeypatch.setattr(memory, "consume_open_stages", lambda *_a, **_k: 0, raising=False)
    return calls


def test_list_receipt_excludes_its_new_reminders_from_the_same_reply(monkeypatch):
    _no_model(monkeypatch)
    _quiet_routing(monkeypatch)
    monkeypatch.setattr(
        main, "_handle_explicit_text",
        MagicMock(side_effect=AssertionError("must not reach chat")),
    )
    calls = _ref_capture(monkeypatch)

    main._handle_text_message(
        _text_event(_trip_text(_base_day()), message_id="m-trip-ref"), G
    )

    rows = _pending_rows()
    assert len(calls) == 1 and calls[0][0].startswith("已新增 4 筆提醒")
    ref = calls[0][1]["primary_reminder_ref"]
    assert set(ref) == {"reminder_ids"}
    assert sorted(ref["reminder_ids"]) == sorted(row[6] for row in rows)


def test_quoted_capture_receipt_excludes_its_new_reminders(monkeypatch):
    _no_model(monkeypatch)
    _quiet_routing(monkeypatch)
    monkeypatch.setattr(main, "_handle_explicit_text", MagicMock())
    calls = _ref_capture(monkeypatch)
    _seed_raw(_trip_text(_base_day()))

    main._handle_text_message(
        _text_event("咪寶", message_id="m-cmd-ref", user_id=U_OTHER, quoted="m-src"), G
    )

    rows = _pending_rows()
    assert len(calls) == 1 and calls[0][0].startswith("已新增 4 筆提醒")
    assert sorted(calls[0][1]["primary_reminder_ref"]["reminder_ids"]) == sorted(
        row[6] for row in rows
    )


def test_followup_receipt_excludes_its_new_reminder(monkeypatch):
    _seed_followup("pending")
    calls = _ref_capture(monkeypatch)
    monkeypatch.setattr(main.burst_filter, "cancel_burst", MagicMock(return_value=[]))

    assert main._try_handle_creation_followup(
        _text_event("加到提醒", message_id="m-fcmd", quoted="m-fsrc"), G, "加到提醒"
    )

    assert calls[0][1]["primary_reminder_ref"] == {"reminder_ids": [_pending_rows()[0][6]]}


def test_later_mention_of_a_listed_trip_day_joins_the_schedule_row():
    """A trip day created from a list is still the one row for that event."""
    day = _base_day()
    noon = int(datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo("Asia/Taipei")).timestamp())
    first, outcome = memory.add_reminder_with_outcome(
        G, U_SENDER, "去海邊一日遊", noon,
        source_text=f"{_md(day)}去海邊一日遊\n{_md(day + timedelta(days=2))}跟團一日遊",
        time_kind="none", source_kind="schedule_line", source_ref="m-list:0",
    )
    assert outcome == "created"

    again, outcome = memory.add_reminder_with_outcome(
        G, U_SENDER, "海邊一日遊行程", noon,
        source_text=f"{_md(day)}的海邊一日遊行程", time_kind="none",
    )

    assert again == first and outcome in {"duplicate", "merged"}
    assert [row[6] for row in _pending_rows()] == [first]


def test_unread_date_words_check_stays_fast_on_digit_and_space_runs():
    """GP2 r4: a run of digits then spaces must not make the check crawl."""
    text = "咪寶提醒我" + "1" * 150 + " " * 344
    started = time.perf_counter()
    main._REMINDER_UNREAD_DATE_RE.search(text)
    assert time.perf_counter() - started < 0.05
    for phrase in ("10分鐘後", "3 個月後", "3個 月後", "兩 個 禮拜 之後", "半小時內"):
        assert main._REMINDER_UNREAD_DATE_RE.search(phrase), phrase
