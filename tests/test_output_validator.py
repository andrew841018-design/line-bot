from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

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


def test_blocks_chinese_internal_trace_leak():
    result = output_validator.validate_outbound_text(
        "[[思考]]\n使用者貼了一個 Threads 連結，並沒有明確提問。\n"
        "我需要判斷使用者可能的意圖。\n\n"
        "正式回覆：這個連結需要先查內容。",
        now=_NOW,
    )

    assert not result.ok
    assert result.reason == "internal_trace_leak"
    assert result.text == ""


def test_blocks_single_strong_internal_trace_marker():
    result = output_validator.validate_outbound_text(
        "我需要判斷使用者可能的意圖。正式回覆：這個連結要先查內容。",
        now=_NOW,
    )

    assert not result.ok
    assert result.reason == "internal_trace_leak"


def test_blocks_english_internal_trace_prose():
    result = output_validator.validate_outbound_text(
        "The user posted a Threads link and did not ask a clear question. "
        "Final answer: please provide the post text.",
        now=_NOW,
    )

    assert not result.ok
    assert result.reason == "internal_trace_leak"


def test_allows_user_facing_analysis_heading():
    result = output_validator.validate_outbound_text(
        "分析：這張圖的重點是會議時間改到 7/20。",
        now=_NOW,
    )

    assert result.ok
    assert result.text.startswith("分析：")


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


def test_blocks_low_value_checkin_commitment_reply():
    examples = [
        "好喔，我們會好好打卡的。",
        "收到，我們會記得打卡。",
        "好，我會準時簽到。",
        "我們會按時報到",
        "好的，我會打卡。",
        "嗯，我們會簽到。",
        "OK，我會記得打卡。",
        "好喔，我們會好好打卡，請放心。",
    ]

    for text in examples:
        result = output_validator.validate_outbound_text(text, now=_NOW)
        assert not result.ok, text
        assert result.reason == "low_value_checkin_commitment", text
        assert result.text == ""


def test_allows_useful_checkin_reminder_or_advice():
    examples = [
        "已設定提醒：2026-07-20 08:00 打卡。",
        "建議 08:30 前完成打卡，入口在一樓櫃台。",
        "請問打卡時間是什麼時候？",
        "好的，我知道了。",
        "我會幫你設定提醒。",
    ]

    for text in examples:
        result = output_validator.validate_outbound_text(text, now=_NOW)
        assert result.ok, text
        assert result.text == text


def test_blocks_low_value_helpless_reply():
    examples = [
        "這個說法有一定道理，但需要進一步查證和具體分析。",
        "這個議題需要多方面考量，建議大家再確認。",
        "長期來看要保持健康的生活方式，均衡飲食、適度運動。",
        "這件事視情況而定，建議諮詢專業人士。",
    ]

    for text in examples:
        result = output_validator.validate_outbound_text(text, now=_NOW)
        assert not result.ok, text
        assert result.reason == "low_value_helpless_reply", text
        assert result.text == ""


def test_allows_helpful_reply_with_concrete_value():
    examples = [
        "這個說法要小心：衛福部資料顯示慢性發炎和癌症風險相關，但不是單一食物直接致癌。來源：https://www.hpa.gov.tw/",
        "處理方式：第一步先截圖保存紀錄，第二步打給銀行客服，第三步停止入金直到 IBKR 審核完成。",
        "建議 08:30 前完成打卡，入口在一樓櫃台。",
        "已設定提醒：2026-07-20 08:00 打卡。",
        "建議你先確認預約是否成功，必要時到櫃台補登。",
        "建議先確認資料欄位是否一致，必要時補一個 migration。",
        "建議先確認 maps.app.goo.gl/abc 的集合地點。",
        "第一點先確認原始訊息，第二點再回覆具體處理方式。",
        "⏰ 提醒（明天）\n2026-07-20 08:00 我們會打卡",
    ]

    for text in examples:
        result = output_validator.validate_outbound_text(text, now=_NOW)
        assert result.ok, text
        assert result.text == text


def test_blocks_low_value_reply_with_fake_numeric_specificity():
    result = output_validator.validate_outbound_text(
        "這個議題需要多方面考量，建議大家再確認 1 下。",
        now=_NOW,
    )

    assert not result.ok
    assert result.reason == "low_value_helpless_reply"


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


def test_line_push_client_rejects_all_suppressed_empty_text_message():
    class Session:
        def post(self, *args, **kwargs):  # pragma: no cover - should not be called
            raise AssertionError("suppressed push should not call LINE API")

    with pytest.raises(line_push_client.LinePushError) as exc:
        line_push_client.push_messages(
            "GROUP",
            [{"type": "text", "text": "好喔，我們會好好打卡的。"}],
            session=Session(),
            fallback_token="token",
        )

    assert exc.value.status_code == 400


def test_line_push_client_rejects_all_suppressed_low_value_helpless_text():
    class Session:
        def post(self, *args, **kwargs):  # pragma: no cover - should not be called
            raise AssertionError("suppressed push should not call LINE API")

    with pytest.raises(line_push_client.LinePushError) as exc:
        line_push_client.push_messages(
            "GROUP",
            [{"type": "text", "text": "這個議題需要多方面考量，建議大家再確認。"}],
            session=Session(),
            fallback_token="token",
        )

    assert exc.value.status_code == 400


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
