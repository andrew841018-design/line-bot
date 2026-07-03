from datetime import datetime
from zoneinfo import ZoneInfo

import line_push_client
import output_validator


_NOW = datetime(2026, 7, 2, 10, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def test_blocks_stale_runtime_year_claim():
    result = output_validator.validate_outbound_text(
        "現在是2024年，2025年的完整數據尚未發布。",
        now=_NOW,
    )

    assert not result.ok
    assert result.reason == "stale_current_year:2024!=2026"
    assert "過期的年份基準" in result.text


def test_blocks_full_width_stale_runtime_year_claim():
    result = output_validator.validate_outbound_text(
        "目前是２０２５年，所以今年資料還不完整。",
        now=_NOW,
    )

    assert not result.ok
    assert result.reason == "stale_current_year:2025!=2026"


def test_blocks_colon_or_iso_runtime_date_claims():
    examples = [
        "今天日期：2024年5月30日。",
        "目前台灣時間 2024-05-30 10:00。",
        "現在已經是 2024 年。",
        "今年 2024，2025 年完整數據還沒出。",
    ]

    for text in examples:
        result = output_validator.validate_outbound_text(text, now=_NOW)
        assert not result.ok, text
        assert result.reason.startswith("stale_current_year:"), text


def test_blocks_internal_knowledge_cutoff_claim():
    result = output_validator.validate_outbound_text(
        "咪寶資料庫只到2024年，所以沒辦法知道現在狀況。",
        now=_NOW,
    )

    assert not result.ok
    assert result.reason == "internal_source_claim"
    assert "內部資料庫" in result.text


def test_blocks_youtube_link_failure_reply_before_line_delivery():
    bad_reply = (
        "這個 YouTube 直播連結本身未提供預覽內容，最直接的方式是點擊觀看以了解即時資訊。\n"
        "直播內容隨時變動，具體涵蓋的主題、發布者及時效性，需透過連結本身才能得知。\n"
        "若您是想查詢特定主題的直播內容，可嘗試提供更多關鍵字。\n"
        "我目前手邊的資料僅包含 YouTube 的一般網域資訊，無法解析此直播的具體內容。"
    )

    result = output_validator.validate_outbound_text(bad_reply, now=_NOW)

    assert not result.ok
    assert result.reason == "youtube_link_failure"
    assert "YouTube 連結解析流程沒有正確啟動" in result.text


def test_blocks_common_youtube_deflection_variants():
    examples = [
        "請直接點擊 YouTube 直播連結觀看。",
        "無法讀取 YouTube 影片內容，請提供更多關鍵字。",
        "我只能提供 YouTube 一般網域資訊。",
        "這個直播點不開，請提供影片標題。",
    ]

    for text in examples:
        result = output_validator.validate_outbound_text(text, now=_NOW)
        assert not result.ok, text
        assert result.reason == "youtube_link_failure", text


def test_allows_honest_youtube_transcript_limit_with_useful_answer():
    result = output_validator.validate_outbound_text(
        "未取得完整逐字稿；根據標題、頻道與描述，這場直播主題是在說央行利率決策。",
        now=_NOW,
    )

    assert result.ok


def test_allows_external_database_cutoff_with_source():
    result = output_validator.validate_outbound_text(
        "World Bank 資料庫截至 2024 年的資料顯示該指標上升。"
        "來源：https://data.worldbank.org/",
        now=_NOW,
    )

    assert result.ok


def test_blocks_latest_official_statistics_without_source_or_date():
    result = output_validator.validate_outbound_text(
        "中國國家移民管理局最新公布的官方統計資料是2023年。",
        now=_NOW,
    )

    assert not result.ok
    assert result.reason == "unverified_current_or_official_data"
    assert "缺少來源或資料日期" in result.text


def test_blocks_chinese_numeral_latest_official_statistics_without_source():
    result = output_validator.validate_outbound_text(
        "官方統計資料顯示最新數字為三百萬人。",
        now=_NOW,
    )

    assert not result.ok
    assert result.reason == "unverified_current_or_official_data"


def test_weak_source_label_does_not_verify_latest_official_statistics():
    result = output_validator.validate_outbound_text(
        "官方統計資料顯示最新數字為300萬人。來源：網路",
        now=_NOW,
    )

    assert not result.ok
    assert result.reason == "unverified_current_or_official_data"


def test_allows_official_statistics_with_source_date():
    result = output_validator.validate_outbound_text(
        "中國國家移民管理局官方統計資料顯示 2023 年為最新已發布年度。"
        "資料日期：2024-01-01。來源：https://example.gov/report",
        now=_NOW,
    )

    assert result.ok
    assert result.text.startswith("中國國家移民管理局官方統計資料")


def test_allows_local_calendar_or_todo_status_without_source():
    result = output_validator.validate_outbound_text(
        "目前沒有查到 pending 待辦或提醒事項。",
        now=_NOW,
    )

    assert result.ok


def test_line_push_client_sanitizes_raw_text_message_before_post():
    captured = {}

    class Resp:
        status_code = 200
        text = ""

    class Session:
        def post(self, url, *, headers, json, timeout):
            captured["json"] = json
            return Resp()

    ok = line_push_client.push_messages(
        "GROUP",
        [{"type": "text", "text": "現在是2024年，2025年的完整數據尚未發布。"}],
        session=Session(),
        fallback_token="token",
    )

    assert ok
    sent_text = captured["json"]["messages"][0]["text"]
    assert "過期的年份基準" in sent_text
    assert "2024年" not in sent_text


def test_line_push_client_downgrades_blocked_textv2_to_plain_text():
    captured = {}

    class Resp:
        status_code = 200
        text = ""

    class Session:
        def post(self, url, *, headers, json, timeout):
            captured["json"] = json
            return Resp()

    ok = line_push_client.push_messages(
        "GROUP",
        [
            {
                "type": "textV2",
                "text": "現在是2024年，{target} 要注意。",
                "substitution": {
                    "target": {
                        "type": "mention",
                        "mentionee": {"type": "user", "userId": "U" + "0" * 32},
                    }
                },
            }
        ],
        session=Session(),
        fallback_token="token",
    )

    assert ok
    message = captured["json"]["messages"][0]
    assert message["type"] == "text"
    assert "substitution" not in message
    assert "過期的年份基準" in message["text"]
