"""Focused tests for deterministic natural-language reminder cancellation."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from reminder_cancel import (
    CancelParseStatus,
    CancelResolutionStatus,
    ReferenceSource,
    parse_cancel_request,
    resolve_cancel_request,
)


TAIPEI = ZoneInfo("Asia/Taipei")
NOW = datetime(2026, 7, 24, 12, 0, tzinfo=TAIPEI)
ACTION = "查看251巷租金是否入郵局帳戶，催繳吳秀英"


def _epoch(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=TAIPEI).timestamp())


def test_parses_pasted_scheduled_reminder_with_explicit_cancel_intent():
    request = parse_cancel_request(
        "取消提醒\n"
        "⏰ 提醒（明天）\n"
        f"2026-07-25 04:00 {ACTION}",
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.current_reference is not None
    assert request.current_reference.source is ReferenceSource.CURRENT
    assert request.current_reference.action == ACTION
    assert request.current_reference.remind_at == _epoch(2026, 7, 25, 4, 0)
    assert request.quoted_reference is None
    assert request.reference == request.current_reference


@pytest.mark.parametrize(
    "text",
    [
        "@爸爸\n"
        "⏰ 提醒（明天）\n"
        f"2026-07-25 04:00 {ACTION}\n"
        "這則取消",
        "取消提醒\n"
        "@爸爸 @媽媽\n"
        "⏰ 提醒（明天）\n"
        f"2026-07-25 04:00 {ACTION} 這一則取消",
        "@爸爸\n"
        "⏰ 提醒（明天）\n"
        f"2026-07-25 04:00 {ACTION}\n"
        "參加人：爸爸、媽媽\n"
        "這則取消",
        "{mention0}\n"
        "⏰ 提醒（明天）\n"
        f"2026-07-25 04:00 {ACTION}\n"
        "取消這則提醒",
    ],
)
def test_pasted_rendered_recipient_mentions_are_ignored_for_intent(text):
    request = parse_cancel_request(text, now=NOW)

    assert request.status is CancelParseStatus.READY
    assert request.current_reference is not None
    assert request.current_reference.action == ACTION


def test_parses_quoted_scheduled_reminder_separately_from_current_intent():
    request = parse_cancel_request(
        "這則取消",
        quoted_text=(
            "@爸爸\n"
            "⏰ 提醒（明天）\n"
            f"2026-07-25 04:00 {ACTION}"
        ),
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.current_reference is None
    assert request.quoted_reference is not None
    assert request.quoted_reference.source is ReferenceSource.QUOTED
    assert request.quoted_reference.action == ACTION


def test_parses_creation_acknowledgement():
    request = parse_cancel_request(
        "這個提醒不用了",
        quoted_text=(
            "已新增提醒\n"
            "時間：2026-07-25 04:00\n"
            f"事項：{ACTION}\n"
            "對象：@爸爸"
        ),
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.reference is not None
    assert request.reference.action == ACTION
    assert request.reference.remind_at == _epoch(2026, 7, 25, 4, 0)


def test_parses_pasted_creation_acknowledgement_in_current_text():
    request = parse_cancel_request(
        "取消提醒\n"
        "已新增提醒\n"
        "時間：2026-07-25 04:00\n"
        f"事項：{ACTION}",
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.current_reference is not None
    assert request.current_reference.action == ACTION


def test_parses_todo_report_and_infers_upcoming_year_in_taipei():
    request = parse_cancel_request(
        "這則提醒取消",
        quoted_text=f"1. 7/25（六）04:00\n事項：{ACTION}",
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.reference is not None
    assert request.reference.remind_at == _epoch(2026, 7, 25, 4, 0)
    assert request.reference.action == ACTION


def test_quoted_report_reconstructs_parenthesized_action_detail():
    request = parse_cancel_request(
        "這則取消",
        quoted_text="1. 7/25（六）04:00\n事項：買藥\n細節：先問醫生",
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.reference is not None
    assert request.reference.action == "買藥"
    assert request.reference.action_variants == (
        "買藥",
        "買藥（先問醫生）",
    )

    resolution = resolve_cancel_request(
        request,
        [
            {
                "reminder_id": 24,
                "action": "買藥（先問醫生）",
                "remind_at": _epoch(2026, 7, 25, 4, 0),
            }
        ],
    )

    assert resolution.status is CancelResolutionStatus.MATCHED
    assert resolution.reminder_id == 24

    base_resolution = resolve_cancel_request(
        request,
        [
            {
                "reminder_id": 25,
                "action": "買藥",
                "remind_at": _epoch(2026, 7, 25, 4, 0),
            }
        ],
    )
    assert base_resolution.status is CancelResolutionStatus.MATCHED
    assert base_resolution.reminder_id == 25

    ambiguous = resolve_cancel_request(
        request,
        [
            {
                "reminder_id": 24,
                "action": "買藥（先問醫生）",
                "remind_at": _epoch(2026, 7, 25, 4, 0),
            },
            {
                "reminder_id": 25,
                "action": "買藥",
                "remind_at": _epoch(2026, 7, 25, 4, 0),
            },
        ],
    )
    assert ambiguous.status is CancelResolutionStatus.AMBIGUOUS


def test_parses_quoted_calendar_event_reminder():
    request = parse_cancel_request(
        "這一則取消",
        quoted_text=(
            "@爸爸\n"
            "🔔 **明天活動提醒**\n"
            "📅 2026-07-25 04:00\n"
            f"🎯 {ACTION}\n"
            "📍 郵局"
        ),
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.reference is not None
    assert request.reference.action == ACTION
    assert request.reference.remind_at == _epoch(2026, 7, 25, 4, 0)
    assert request.reference.reference_kind == "calendar_event"
    assert request.reference.event_time_specified is True


@pytest.mark.parametrize(
    "header",
    ["**後天活動提醒**", "🔔 **後天活動提醒**"],
)
def test_pasted_calendar_location_terminal_cancel_command_is_intent(header):
    request = parse_cancel_request(
        f"{header}\n"
        "📅 2026-07-25 01:10\n"
        "🎯 紐西蘭一望無際的公路旅行中活動\n"
        "📍 紐西蘭一望無際的公路旅行中。這個取消",
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.reference is not None
    assert request.reference.action == "紐西蘭一望無際的公路旅行中活動"
    assert request.reference.remind_at == _epoch(2026, 7, 25, 1, 10)
    assert request.reference.reference_kind == "calendar_event"
    assert request.reference.event_time_specified is True
    assert request.reference.location_hint == "紐西蘭一望無際的公路旅行中"


@pytest.mark.parametrize(
    "text",
    [
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 公路旅行中這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 公路旅行中，這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 公路旅行中：這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 公路旅行中；這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 公路旅行中。這個取消方案"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 公路旅行中。不要取消這個提醒"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 公路旅行中。不要取消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 公路旅行中。先不要。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 公路旅行中。我只是測試。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不要取消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 先不要。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 我只是測試。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 如果取消會怎樣。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\u2060要取\u2060消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 先\u2066不要。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 我只是測\uFE0F試。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 如\u034F果取消會怎樣。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\u180E要取消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\u3164要取\u3164消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\u2065要取\u2065消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\u115F要取\u1160消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\uFFF0要取\uFFF0消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\U000E0000要取\U000E0100消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\u0000要取\u0000消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\u007F要取\u007F消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\u009F要取\u009F消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\uFDD0要取\uFDD0消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\uFFFE要取\uFFFE消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\uE000要取\uE000消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\u2800要取\u2800消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\U0002EE5E要取\U0002EE5E消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\U0002F7FF要取\U0002F7FF消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\U0002FA1E要取\U0002FA1E消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不\U0003134B要取\U0003134B消這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 请勿删除这个提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 别删除这个提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 禁止删除这个提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 无需删除这个提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 莫删除这个提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 我在测试。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 这是测试。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 单纯测试。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 测试一下。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 假设这样。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 我没有要这样做。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不刪這個提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 不删这个提醒。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 公路旅行中。如果取消這個提醒會怎樣？"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 公路旅行中。怎麼取消這個提醒？"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 公路旅行中。這個取消了嗎？"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 公路旅行中。這個取消，我再看看"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 公路旅行中。這個取消\n"
            "👥 Andrew"
        ),
        "📍 公路旅行中。這個取消",
        (
            "📅 2026-07-25 01:10\n"
            "🎯 活動一\n"
            "📅 2026-07-25 02:10\n"
            "🎯 活動二\n"
            "📍 公路旅行中。這個取消"
        ),
        (
            "**後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 公路旅行活動\n"
            "📍 通知櫃台這則取消"
        ),
    ],
)
def test_calendar_location_text_without_strict_terminal_command_is_not_cancel(
    text,
):
    request = parse_cancel_request(text, now=NOW)

    assert request.status is CancelParseStatus.NOT_CANCEL


def test_equivalent_current_and_quoted_reference_prefers_calendar_source():
    request = parse_cancel_request(
        f"取消提醒 2026-07-25 04:00 {ACTION}",
        quoted_text=(
            "🔔 **明天活動提醒**\n"
            "📅 2026-07-25 04:00\n"
            f"🎯 {ACTION}"
        ),
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.current_reference is not None
    assert request.quoted_reference is not None
    assert request.reference == request.quoted_reference
    assert request.reference.reference_kind == "calendar_event"


def test_todo_report_rolls_past_month_day_into_next_year():
    request = parse_cancel_request(
        "這則提醒取消",
        quoted_text="1/1（五）00:30\n事項：跨年後關瓦斯",
        now=datetime(2026, 12, 31, 23, 31, tzinfo=TAIPEI),
    )

    assert request.status is CancelParseStatus.READY
    assert request.reference is not None
    assert request.reference.remind_at == _epoch(2027, 1, 1, 0, 30)


def test_parses_single_line_cancellation():
    request = parse_cancel_request(
        f"取消提醒 2026-07-25 04:00 {ACTION}",
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.reference is not None
    assert request.reference.action == ACTION
    assert request.reference.remind_at == _epoch(2026, 7, 25, 4, 0)


@pytest.mark.parametrize("suffix", ["這個提醒", "這一則", "這一則取消"])
def test_pasted_target_suffix_on_same_line_is_not_part_of_action(suffix):
    request = parse_cancel_request(
        "line bot 取消提醒（明天）\n"
        f"2026-07-25 04:00 {ACTION} {suffix}",
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.reference is not None
    assert request.reference.action == ACTION
    assert request.reference.remind_at == _epoch(2026, 7, 25, 4, 0)


@pytest.mark.parametrize(
    "parenthetical",
    ["先不要", "先不要取消", "不要真的做", "我只是測試"],
)
@pytest.mark.parametrize("target_kind", ["explicit", "bound"])
def test_rejects_contradictory_or_meta_parenthetical_cancel_commands(
    parenthetical,
    target_kind,
):
    if target_kind == "explicit":
        request = parse_cancel_request(
            f"取消提醒（{parenthetical}）\n"
            f"2026-07-25 04:00 {ACTION}",
            now=NOW,
        )
    else:
        request = parse_cancel_request(
            f"取消提醒（{parenthetical}）",
            quoted_text="媽媽\n請確認時間",
            quoted_identity_bound=True,
            now=NOW,
        )

    assert request.status is CancelParseStatus.NOT_CANCEL


@pytest.mark.parametrize("target_kind", ["explicit", "bound"])
def test_accepts_temporal_stage_parenthetical_cancel_commands(target_kind):
    if target_kind == "explicit":
        request = parse_cancel_request(
            "取消提醒（明天）\n"
            f"2026-07-25 04:00 {ACTION}",
            now=NOW,
        )
    else:
        request = parse_cancel_request(
            "取消提醒（明天）",
            quoted_text="媽媽\n請確認時間",
            quoted_identity_bound=True,
            now=NOW,
        )

    assert request.status is CancelParseStatus.READY


@pytest.mark.parametrize(
    "command",
    [
        "可以幫我把這則提醒取消嗎？",
        "能不能幫我把這則提醒取消？",
        "可不可以幫我把這個提醒取消",
    ],
)
def test_accepts_polite_modal_ba_order_for_bound_quote(command):
    request = parse_cancel_request(
        command,
        quoted_text="媽媽\n請確認時間",
        quoted_identity_bound=True,
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY


@pytest.mark.parametrize("header", ["提醒（明天）", "⏰ 提醒（明天）"])
@pytest.mark.parametrize("suffix", ["這則取消", "這一則取消"])
def test_same_datetime_line_suffix_alone_is_not_enough_intent(header, suffix):
    request = parse_cancel_request(
        f"{header}\n2026-07-25 04:00 {ACTION} {suffix}",
        now=NOW,
    )

    assert request.status is CancelParseStatus.NOT_CANCEL


def test_same_current_and_quoted_references_are_not_a_conflict():
    rendered = f"⏰ 提醒（明天）\n2026-07-25 04:00 {ACTION}"
    request = parse_cancel_request(
        f"取消提醒\n{rendered}",
        quoted_text=rendered,
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.current_reference is not None
    assert request.quoted_reference is not None


def test_conflicting_current_and_quoted_references_are_ambiguous():
    request = parse_cancel_request(
        f"取消提醒 2026-07-25 04:00 {ACTION}",
        quoted_text="⏰ 提醒（明天）\n2026-07-25 08:00 吃早餐",
        now=NOW,
    )

    assert request.status is CancelParseStatus.AMBIGUOUS
    assert request.reason == "conflicting_references"
    assert request.reference is None


def test_conflicting_calendar_locations_are_ambiguous():
    request = parse_cancel_request(
        "取消提醒\n"
        "📅 2026-07-25 01:10\n"
        "🎯 同名活動\n"
        "📍 地點B",
        quoted_text=(
            "🔔 **後天活動提醒**\n"
            "📅 2026-07-25 01:10\n"
            "🎯 同名活動\n"
            "📍 地點A"
        ),
        now=NOW,
    )

    assert request.status is CancelParseStatus.AMBIGUOUS
    assert request.reason == "conflicting_references"
    assert request.reference is None


@pytest.mark.parametrize(
    ("current_location_line", "quoted_location_line"),
    [
        ("📍 地點A", ""),
        ("", "📍 地點B"),
    ],
)
def test_asymmetric_calendar_location_evidence_is_ambiguous(
    current_location_line,
    quoted_location_line,
):
    current = (
        "取消提醒\n"
        "📅 2026-07-25 01:10\n"
        "🎯 同名活動"
        + (f"\n{current_location_line}" if current_location_line else "")
    )
    quoted = (
        "🔔 **後天活動提醒**\n"
        "📅 2026-07-25 01:10\n"
        "🎯 同名活動"
        + (f"\n{quoted_location_line}" if quoted_location_line else "")
    )

    request = parse_cancel_request(current, quoted_text=quoted, now=NOW)

    assert request.status is CancelParseStatus.AMBIGUOUS
    assert request.reason == "conflicting_references"
    assert request.reference is None


def test_multiple_current_calendar_locations_are_ambiguous():
    request = parse_cancel_request(
        "取消提醒\n"
        "📅 2026-07-25 01:10\n"
        "🎯 同名活動\n"
        "📍 地點A\n"
        "📅 2026-07-25 01:10\n"
        "🎯 同名活動\n"
        "📍 地點B",
        now=NOW,
    )

    assert request.status is CancelParseStatus.AMBIGUOUS
    assert request.reason == "multiple_current_references"
    assert request.reference is None


def test_explicit_cancel_reminder_without_target_is_handled_as_ambiguous():
    request = parse_cancel_request("取消提醒", now=NOW)

    assert request.status is CancelParseStatus.AMBIGUOUS
    assert request.reason == "missing_reference"


def test_quoted_deictic_cancel_with_failed_lookup_is_handled_as_ambiguous():
    # ``None`` means no quote; an empty string means a quoted_message_id existed
    # but its original text could not be recovered.
    request = parse_cancel_request("這則取消", quoted_text="", now=NOW)

    assert request.status is CancelParseStatus.AMBIGUOUS
    assert request.reason == "missing_reference"


@pytest.mark.parametrize("command", ["這則取消", "取消"])
def test_bound_quoted_identity_supports_nonstandard_visible_text(command):
    request = parse_cancel_request(
        command,
        quoted_text="媽媽\n請確認時間",
        quoted_identity_bound=True,
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.reference is None


def test_bound_nonstandard_quote_conflicts_with_explicit_current_target():
    request = parse_cancel_request(
        "取消提醒\n2030-08-04 09:00 繳信用卡",
        quoted_text="媽媽\n請確認時間",
        quoted_identity_bound=True,
        now=NOW,
    )

    assert request.status is CancelParseStatus.AMBIGUOUS
    assert request.reason == "bound_identity_with_unverified_current_reference"
    assert request.current_reference is not None
    assert request.quoted_reference is None


@pytest.mark.parametrize("text", ["取消", "算了", "取消訂房", "這則取消"])
def test_rejects_generic_cancel_without_reminder_or_quote_context(text):
    assert parse_cancel_request(text, now=NOW).status is CancelParseStatus.NOT_CANCEL


@pytest.mark.parametrize(
    "text",
    [
        "這一則取消",
        "這個提醒幫我取消",
        "取消掉這個提醒",
        "刪掉這則提醒",
        "取消",
    ],
)
def test_accepts_common_imperatives_when_replying_to_a_reminder(text):
    request = parse_cancel_request(
        text,
        quoted_text=f"⏰ 提醒（明天）\n2026-07-25 04:00 {ACTION}",
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.reference is not None
    assert request.reference.action == ACTION


def test_accepts_polite_quoted_imperative_with_question_particle():
    request = parse_cancel_request(
        "可以幫我取消這則提醒嗎？",
        quoted_text=f"⏰ 提醒（明天）\n2026-07-25 04:00 {ACTION}",
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY


def test_bare_cancel_reply_to_non_reminder_is_not_intercepted():
    request = parse_cancel_request(
        "取消",
        quoted_text="明天聚餐要不要改到晚上？",
        now=NOW,
    )

    assert request.status is CancelParseStatus.NOT_CANCEL


@pytest.mark.parametrize(
    "text",
    [
        "不要取消提醒",
        "不要把這則提醒取消",
        "不用取消提醒",
        "先別取消提醒",
        "我沒有要取消提醒",
        "不是要取消提醒",
        "不需要取消提醒",
    ],
)
def test_rejects_negated_cancellation(text):
    assert parse_cancel_request(text, now=NOW).status is CancelParseStatus.NOT_CANCEL


@pytest.mark.parametrize(
    "prefix",
    [
        "先不要幫我取消提醒",
        "不要幫忙取消提醒",
        "我沒說要取消提醒",
        "不准取消提醒",
        "這則提醒不要取消",
        "不要取消這則",
        "先別取消這個",
        "不要把這則取消",
    ],
)
def test_rejects_interposed_negation_even_with_an_exact_reference(prefix):
    request = parse_cancel_request(
        f"{prefix}\n2026-07-25 04:00 {ACTION}",
        now=NOW,
    )

    assert request.status is CancelParseStatus.NOT_CANCEL


@pytest.mark.parametrize(
    "intent",
    [
        "我沒有說過要取消提醒",
        "我沒叫你取消提醒",
        "不該取消提醒",
        "不應該取消提醒",
        "不許取消提醒",
        "禁止取消提醒",
        "請勿取消提醒",
        "我已取消提醒",
        "我昨天取消提醒",
        "你覺得要取消這則",
        "你覺得取消這則",
        "是否取消這則",
        "我問一下要取消這則",
        "我想知道誰取消這則",
    ],
)
def test_non_imperative_clause_never_becomes_cancellation(intent):
    request = parse_cancel_request(
        f"{intent}\n2026-07-25 04:00 {ACTION}",
        quoted_text=f"⏰ 提醒（明天）\n2026-07-25 04:00 {ACTION}",
        now=NOW,
    )

    assert request.status is CancelParseStatus.NOT_CANCEL


@pytest.mark.parametrize(
    "action",
    [
        "通知房東這則取消",
        "記錄這個提醒不用了",
        "確認對方說這則刪除",
    ],
)
def test_pasted_reminder_action_suffix_alone_is_not_a_cancel_command(action):
    request = parse_cancel_request(
        f"⏰ 提醒（明天）\n2030-07-25 04:00 {action}",
        now=NOW,
    )

    assert request.status is CancelParseStatus.NOT_CANCEL


@pytest.mark.parametrize(
    "intent",
    [
        "我想取消這則提醒",
        "我決定取消這則提醒",
        "我要取消這則提醒",
    ],
)
def test_anchored_positive_first_person_intent_is_accepted(intent):
    request = parse_cancel_request(
        intent,
        quoted_text=f"⏰ 提醒（明天）\n2026-07-25 04:00 {ACTION}",
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY


@pytest.mark.parametrize(
    "text",
    [
        "這則提醒取消了嗎？",
        "我已經取消這個提醒嗎？",
        "如果取消這則提醒會怎樣？",
        "我已經取消這個提醒了",
        "這則提醒取消了",
        "誰取消這則？",
        "為什麼取消這則？",
        "這則取消了嗎？",
        "我在考慮取消這則提醒",
        "可能要取消這則提醒",
        "也許取消這則提醒",
        "我不確定是否該取消這則提醒",
        "我還沒決定要不要取消這則提醒",
    ],
)
def test_rejects_questions_hypotheticals_and_past_declarations(text):
    request = parse_cancel_request(
        text,
        quoted_text=f"⏰ 提醒（明天）\n2026-07-25 04:00 {ACTION}",
        now=NOW,
    )

    assert request.status is CancelParseStatus.NOT_CANCEL


@pytest.mark.parametrize(
    "text",
    [
        "⏰ 提醒（明天）\n2026-07-25 04:00 取消這個訂房",
        "⏰ 提醒（明天）\n2026-07-25 04:00 刪除這個檔案",
        "已新增提醒\n時間：2026-07-25 04:00\n事項：取消這個訂房",
    ],
)
def test_pasting_reminder_without_separate_cancel_intent_is_not_a_command(text):
    assert parse_cancel_request(text, now=NOW).status is CancelParseStatus.NOT_CANCEL


@pytest.mark.parametrize(
    "text",
    [
        "取消提醒\n2026-07-25 04:00 問房東租金入帳了嗎？",
        "取消提醒\n已新增提醒\n時間：2026-07-25 04:00\n事項：問醫生可以停藥嗎？",
    ],
)
def test_question_mark_inside_pasted_action_does_not_negate_command(text):
    request = parse_cancel_request(text, now=NOW)

    assert request.status is CancelParseStatus.READY


@pytest.mark.parametrize(
    "text",
    [
        "提醒我明天取消訂房",
        "新增提醒：2026-07-25 04:00 取消訂房",
        "請提醒我取消明天的預約",
    ],
)
def test_rejects_reminder_creation_whose_action_contains_cancel(text):
    assert parse_cancel_request(text, now=NOW).status is CancelParseStatus.NOT_CANCEL


@pytest.mark.parametrize(
    "text",
    [
        "怎麼取消提醒？",
        "請問取消提醒要怎麼用",
        "可以取消提醒嗎？",
        "能不能取消提醒？",
    ],
)
def test_rejects_help_and_capability_questions(text):
    assert parse_cancel_request(text, now=NOW).status is CancelParseStatus.NOT_CANCEL


def test_resolver_selects_only_exact_action_and_exact_minute():
    request = parse_cancel_request(
        f"取消提醒 2026-07-25 04:00 {ACTION}",
        now=NOW,
    )
    candidates = [
        {
            "reminder_id": 10,
            "action": ACTION,
            "remind_at": _epoch(2026, 7, 25, 8, 0),
        },
        {
            "reminder_id": 11,
            "action": ACTION,
            "remind_at": _epoch(2026, 7, 25, 4, 0) + 20,
        },
    ]

    resolution = resolve_cancel_request(request, candidates)

    assert resolution.status is CancelResolutionStatus.MATCHED
    assert resolution.reminder_id == 11
    assert resolution.action == ACTION
    assert resolution.remind_at == candidates[1]["remind_at"]


def test_resolver_applies_only_documented_unicode_and_whitespace_normalization():
    request = parse_cancel_request(
        "取消提醒 2026-07-25 04:00 ＡＢＣ　帳單",
        now=NOW,
    )

    resolution = resolve_cancel_request(
        request,
        [
            {
                "reminder_id": 12,
                "action": "abc 帳單",
                "remind_at": _epoch(2026, 7, 25, 4, 0),
            }
        ],
    )

    assert resolution.status is CancelResolutionStatus.MATCHED
    assert resolution.reminder_id == 12


def test_resolver_does_not_fuzzy_match_action_substrings():
    request = parse_cancel_request(
        "取消提醒 2026-07-25 04:00 查看251巷租金",
        now=NOW,
    )

    resolution = resolve_cancel_request(
        request,
        [
            {
                "reminder_id": 13,
                "action": ACTION,
                "remind_at": _epoch(2026, 7, 25, 4, 0),
            }
        ],
    )

    assert resolution.status is CancelResolutionStatus.NOT_FOUND
    assert resolution.reminder_id is None


def test_resolver_returns_ambiguous_for_duplicate_exact_candidates():
    request = parse_cancel_request(
        f"取消提醒 2026-07-25 04:00 {ACTION}",
        now=NOW,
    )
    candidate = {
        "action": ACTION,
        "remind_at": _epoch(2026, 7, 25, 4, 0),
    }

    resolution = resolve_cancel_request(
        request,
        [
            {"reminder_id": 14, **candidate},
            {"reminder_id": 15, **candidate},
        ],
    )

    assert resolution.status is CancelResolutionStatus.AMBIGUOUS
    assert resolution.reason == "multiple_exact_matches"
    assert resolution.reminder_id is None


def test_action_ending_in_ambiguous_object_word_matches_only_exact_row():
    request = parse_cancel_request(
        "取消提醒\n2026-07-25 04:00 買這個",
        now=NOW,
    )

    resolution = resolve_cancel_request(
        request,
        [
            {
                "reminder_id": 20,
                "action": "買",
                "remind_at": _epoch(2026, 7, 25, 4, 0),
            },
            {
                "reminder_id": 21,
                "action": "買這個",
                "remind_at": _epoch(2026, 7, 25, 4, 0),
            },
        ],
    )

    assert resolution.status is CancelResolutionStatus.MATCHED
    assert resolution.reminder_id == 21


def test_ambiguous_object_word_is_not_stripped_from_pasted_action():
    request = parse_cancel_request(
        "取消提醒\n2026-07-25 04:00 買這個",
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.reference is not None
    assert request.reference.action_variants == ("買這個",)

    resolution = resolve_cancel_request(
        request,
        [
            {
                "reminder_id": 23,
                "action": "買",
                "remind_at": _epoch(2026, 7, 25, 4, 0),
            }
        ],
    )

    assert resolution.status is CancelResolutionStatus.NOT_FOUND
    assert resolution.reminder_id is None


def test_quoted_action_deictic_suffix_is_never_stripped():
    request = parse_cancel_request(
        "這則取消",
        quoted_text="⏰ 提醒（明天）\n2026-07-25 04:00 買這個",
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.quoted_reference is not None
    assert request.quoted_reference.action == "買這個"
    assert request.quoted_reference.action_variants == ("買這個",)

    resolution = resolve_cancel_request(
        request,
        [
            {
                "reminder_id": 22,
                "action": "買",
                "remind_at": _epoch(2026, 7, 25, 4, 0),
            }
        ],
    )

    assert resolution.status is CancelResolutionStatus.NOT_FOUND
    assert resolution.reminder_id is None


@pytest.mark.parametrize(
    "command",
    [
        "@咪寶 這則取消",
        "＠咪寶 這則取消",
        "@咪寶 取消這則提醒",
        "@LINE BOT 取消這則提醒",
        "@咪寶 取消",
    ],
)
def test_quoted_cancel_accepts_line_mention_prefix(command):
    request = parse_cancel_request(
        command,
        quoted_text=f"⏰ 提醒（明天）\n2026-07-25 04:00 {ACTION}",
        now=NOW,
    )

    assert request.status is CancelParseStatus.READY
    assert request.quoted_reference is not None
    assert request.quoted_reference.action == ACTION


@pytest.mark.parametrize(
    "lines",
    [
        "2026-07-25 04:00 買\n2026-07-25 04:00 買這個",
        "2026-07-25 04:00 買這個\n2026-07-25 04:00 買",
    ],
)
def test_distinct_raw_reference_lines_are_always_ambiguous(lines):
    request = parse_cancel_request(f"取消提醒\n{lines}", now=NOW)

    assert request.status is CancelParseStatus.AMBIGUOUS
    assert request.reason == "multiple_current_references"


def test_resolver_preserves_parse_ambiguity_without_examining_candidates():
    request = parse_cancel_request("取消提醒", now=NOW)

    resolution = resolve_cancel_request(
        request,
        [
            {
                "reminder_id": 16,
                "action": ACTION,
                "remind_at": _epoch(2026, 7, 25, 4, 0),
            }
        ],
    )

    assert resolution.status is CancelResolutionStatus.AMBIGUOUS
    assert resolution.reason == "missing_reference"


def test_resolver_preserves_not_cancel_status():
    request = parse_cancel_request("怎麼取消提醒？", now=NOW)

    resolution = resolve_cancel_request(request, [])

    assert resolution.status is CancelResolutionStatus.NOT_CANCEL
    assert resolution.reminder_id is None
