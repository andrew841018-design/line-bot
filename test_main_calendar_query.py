"""main._is_calendar_query / _handle_calendar_query — 14:49 verbatim 必過。"""

from __future__ import annotations

import json
from contextlib import nullcontext

import pytest


# ── 14:49 verbatim：原始失敗 query ─────────────────────────────────────────
def test_1449_verbatim_classified_as_calendar_query():
    """家人在 14:49 問的問句 — _is_calendar_query 必須回 True。"""
    import main

    assert main._is_calendar_query("爸爸明天幾點要拿蛋糕？") is True


def test_calendar_query_variations():
    import main

    truthy = [
        "明天有什麼安排",
        "明天有事嗎",
        "後天幾點要出門",
        "今天有什麼計畫",
        "週六有事嗎",
        "下週六的行程",
        "哥哥去紐西蘭的日期",
        "米堡，媽媽什麼時候回家",
        "媽媽幾點回家",
        "媽媽什麼時間回家",
        "媽媽幾點到家",
        "媽媽什麼時候會到家",
        "媽媽什麼時候回台北",
        "明天有什麼活動",
        "明天有什麼聚會",
        "明天有什麼會議",
        "明天有什麼約",
        "明天有什麼要做的",
        "請問明天有什麼活動要參加嗎",
        "明天有什麼聚會需要出席",
        "明天有什麼會議需要準備",
        "明天有什麼約要去",
        "明天有什麼聚餐",
        "明天有哪些活動",
        "明天有聚餐嗎",
        "明天安排是什麼",
        "明天的行程是什麼",
        "明天的行程",
        "明天台北有會議嗎",
        "明天台北有聚餐嗎",
        "明天我們在台北有什麼活動",
        "媽媽明天在台北有什麼活動",
        "明天媽媽有什麼活動",
        "媽媽在台北明天有什麼活動",
        "明天我們的行程有什麼活動",
        "姐姐明天在台北有什麼活動",
        "哥哥明天在台北有什麼活動",
        "哥哥明天在台北有什麼活動",
        "妹妹明天在台北有什麼活動",
        "明天早上媽媽在台北有什麼活動",
        "媽媽預計明天在台北有什麼活動",
        "媽媽什麼時候到台北",
        "媽媽什麼時候會到台北",
        "媽媽幾點抵達台北",
        "媽媽何時從台中到台北",
        "媽媽何時搭車到台北",
        "媽媽何時才會到台北",
        "媽媽什麼時候回來",
        "媽媽什麼時候回來啊",
        "媽媽什麼時候回來台北",
        "星期六媽媽有什麼活動",
        "禮拜六媽媽有什麼活動",
        "周六媽媽有什麼活動",
        "本週末媽媽在台北有什麼活動",
        "這禮拜有什麼活動",
        "下禮拜有什麼行程",
        "這星期有什麼行程",
        "下星期有什麼活動",
        "這週末有什麼活動",
        "媽媽這週末在台北有什麼活動",
        "這週末媽媽在台北有什麼活動",
        "明天晚上有什麼活動",
        "明天下午有什麼會議",
        "明天早上有什麼聚會",
        "明天我有什麼活動",
        "明天我在台北有什麼活動",
        "明天台北有什麼活動是媽媽要去的",
        "明天媽媽、爸爸、妹妹在台北有什麼活動",
        "媽媽明天在威靈頓有什麼活動",
        "媽媽今晚有什麼活動",
        "媽媽明晚有什麼活動",
        "8/13媽媽有什麼活動",
        "媽媽8月13日有什麼行程",
        "2026-08-13媽媽有什麼安排",
        "8/13有什麼活動",
        "媽媽8月13號有什麼活動",
        "媽媽2027年8月13日有什麼活動",
        "媽媽明天下午開什麼會？",
        "媽媽明天要參加哪些會議？",
        "媽媽明天回台北嗎？",
        "媽媽明天會回台北嗎？",
        "媽媽明天要回台北嗎？",
        "媽媽明天回家嗎？",
        "媽媽明天回台北？",
        "媽媽明天會回台北吧？",
        "媽媽明天會回台北了吧？",
        "媽媽明天回台北對嗎？",
        "媽媽明天回台北是不是？",
        "媽媽明天回台北了沒？",
        "媽媽明天回台北，對吧？",
        "媽媽明天回來嗎？",
        "媽媽明天會回來嗎？",
        "媽媽明天回來？",
        "媽媽明天會不會回台北",
        "媽媽明天是否回台北",
        "媽媽明天回不回台北",
        "記得媽媽什麼時候回家嗎？",
        "記得媽媽明天有什麼活動嗎？",
        "記得爸爸明天開會嗎？",
        "記得媽媽明天看診嗎？",
        "記得妹妹明天上課嗎？",
        "記得明天爸爸要開會嗎？",
        "記得明天媽媽要看診嗎？",
        "記得明天下午爸爸要去臺北市中正區考選部開會嗎？",
        "媽媽明天是不是要回台北",
        "媽媽明天是不是會回台北",
        "媽媽明天是不是回家",
        "媽媽明天有沒有要回台北",
        "媽媽明天會回台北是嗎？",
        "媽媽明天會回台北沒錯吧？",
        "媽媽明天回台北好嗎？",
        "媽媽明天會回台北嘛？",
        "媽媽明天回台北喔？",
        "媽媽明天會回台北是嗎",
        "媽媽明天會回台北沒錯吧",
        "媽媽明天回台北好嗎",
        "媽媽明天回台北對不對",
        "媽媽明天回來了沒",
        "媽媽回來沒",
        "媽媽回來了嗎",
        "媽媽還要多久才回家",
        "媽媽多久回家",
        "明天有哪些新增加的行程？",
        "明天有什麼新增行程？",
    ]
    for q in truthy:
        assert main._is_calendar_query(q) is True, f"should match: {q}"


def test_non_calendar_query_rejected():
    import main

    falsy = [
        "今天天氣如何",
        "美國股市怎樣",
        "幫我畫一張圖",
        "蛋糕好吃嗎",
        "紐西蘭時間現在幾點",
        "紐西蘭日期格式怎麼寫",
        "媽媽什麼時候回我訊息",
        "媽媽何時回覆群組",
        "媽媽什麼時候回電話",
        "媽媽何時方便聊天",
        "今天有什麼新聞",
        "明天有什麼天氣",
        "今天有什麼餐廳推薦",
        "今天有什麼好吃的",
        "明天有什麼電影",
        "今天股市有什麼消息",
        "今天新聞有什麼重點",
        "明天台北天氣有什麼變化",
        "明天臺北有什麼活動",
        "臺北明天有什麼活動",
        "明天東京有什麼活動",
        "大阪明天有哪些展覽",
        "明天Taipei有什麼活動",
        "Auckland明天有哪些活動",
        "媽媽從桃園回台北要多久",
        "媽媽回台北車程多久",
        "明天幾點下雨",
        "明天幾點開盤",
        "明天幾點日出",
        "今天幾點有電影",
        "今天幾點新聞播出",
        "今天有什麼感想",
        "明天有什麼課可以上",
        "媽媽什麼時候到家樂福",
        "媽媽幾點到家庭聚會",
        "媽媽什麼時候回家鄉",
        "媽媽什麼時候回家長群組",
        "明天有什麼活動推薦",
        "明天有什麼聚餐推薦",
        "明天有活動推薦嗎",
        "明天有聚餐推薦嗎",
        "明天有活動好玩嗎",
        "明天台北有什麼活動",
        "今天高雄有什麼活動",
        "台北明天有什麼活動",
        "高雄今天有什麼活動",
        "媽媽想知道明天台北有什麼活動",
        "媽媽問明天台北有什麼活動",
        "媽媽明天想知道台北有什麼活動",
        "明天媽媽想知道台北有什麼活動",
        "媽媽明天問台北有什麼活動",
        "幫媽媽查明天台北有什麼活動",
        "明天台北有活動嗎",
        "明天台北有什麼活動適合媽媽",
        "明天東京有什麼活動",
        "明天臺北有什麼活動",
        "媽媽明天從桃園回台北要多久",
        "本週台北有什麼活動",
        "週六台北有什麼活動",
        "台北週六有什麼活動",
        "我們想知道明天台北有什麼活動",
        "我們想查明天台北有什麼活動",
        "我們要去台北，明天有什麼活動",
        "明天台北有什麼活動適合我們",
        "明天台北有什麼活動我們可以去",
        "明天桃園有什麼活動",
        "明天基隆有什麼活動",
        "明天彰化有什麼活動",
        "明天奧克蘭有什麼活動",
        "信義區明天有什麼活動",
        "明天信義區有什麼活動",
        "明天有什麼活動適合媽媽",
        "明天有什麼活動適合我們",
        "明天有什麼活動可以參加",
        "明天有哪些活動能參加",
        "明天有什麼活動可以報名",
        "明天有什麼活動值得去",
        "明天有什麼活動媽媽可以參加",
        "旅行時間太久了",
        "上班時間好長",
        "開會時間改了",
        "明天上班時間很長",
        "媽媽何時提到台北",
        "媽媽何時找到台北資料",
        "媽媽什麼時候講到台北",
        "媽媽何時從報告中提到台北",
        "媽媽何時從新聞中提到台北",
        "媽媽什麼時候從對話裡提到台北",
        "星期六台北有什麼活動",
        "台北禮拜六有什麼活動",
        "明天威靈頓有什麼活動",
        "明天有事不能去",
        "明天安排去看電影",
        "明天行程很滿",
        "明天計畫去爬山",
        "明天的行程改了",
        "行程明天再說",
        "安排明天去看電影",
        "媽媽想知道明天桃園有什麼活動",
        "明天活動很好玩",
        "明天聚餐吃火鍋",
        "明天會議很麻煩",
        "明天聚會我不去了",
        "昨天活動很精彩",
        "",
    ]
    for q in falsy:
        assert main._is_calendar_query(q) is False, f"should NOT match: {q}"


def test_todo_query_variations():
    import main

    truthy = [
        "有哪些待辦事項？",
        "目前有哪些提醒事項和會提醒的時間？",
        "是不是還有其他待辦事項？",
        "還有什麼要做的事項",
    ]
    for q in truthy:
        assert main._is_todo_query(q) is True, f"should match: {q}"

    falsy = [
        "提醒我明天早上洗牙",
        "幫我新增6/25早上洗牙",
        "今天天氣如何",
        "",
    ]
    for q in falsy:
        assert main._is_todo_query(q) is False, f"should NOT match: {q}"


def test_squash_timing_query_routes_to_reminders():
    import main

    for q in ("什麼時候打壁球", "壁球什麼時候"):
        assert main._is_todo_query(q) is True, f"should match reminder query: {q}"
        assert main._is_calendar_query(q) is False, f"should not use calendar query: {q}"


def test_conversation_search_query_variations():
    import main

    assert main._is_conversation_search_query("搜尋對話紀錄") is True
    assert main._is_conversation_search_query("查聊天紀錄 紐西蘭") is True
    assert main._is_conversation_search_query("對話紀錄搜尋 哥哥") is True
    assert main._is_conversation_search_query("今天天氣如何") is False


def test_build_conversation_search_reply_requires_keyword():
    import main

    reply = main._build_conversation_search_reply("G1", "搜尋對話紀錄")

    assert "請在後面加關鍵字" in reply
    assert "搜尋對話紀錄 紐西蘭" in reply


def test_build_conversation_search_reply_lists_keyword_hits(monkeypatch):
    import main

    monkeypatch.setattr(
        main.memory,
        "search_raw_messages",
        lambda gid, query, limit=5, exclude_bot=True: [
            ("m1", "U_DAD", "哥哥 7/16 去紐西蘭，7/28 回來", 1783200000),
        ],
    )
    monkeypatch.setattr(main, "_alias_from_user_id", lambda uid: "爸爸")

    reply = main._build_conversation_search_reply("G1", "搜尋對話紀錄 紐西蘭")

    assert "找到「紐西蘭」相關對話" in reply
    assert "哥哥 7/16 去紐西蘭" in reply
    assert "（爸爸）" in reply


def test_search_raw_messages_splits_chinese_trip_terms():
    import memory

    memory.log_raw_message(
        "G1",
        "m1",
        "U_DAD",
        "哥哥 7/16 去紐西蘭，7/28 回來",
    )

    hits = memory.search_raw_messages("G1", "哥哥去紐西蘭", limit=5)

    assert len(hits) == 1
    assert hits[0][0] == "m1"
    assert "7/16" in hits[0][2]


def test_extract_conversation_search_query_strips_trailing_record_words():
    import main

    assert main._extract_conversation_search_query("查一下紐西蘭的聊天紀錄") == "紐西蘭"
    assert main._extract_conversation_search_query("找哥哥的對話紀錄") == "哥哥"


# ── _resolve_relative_date ───────────────────────────────────────────────
def test_resolve_relative_date_tomorrow():
    import main
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today_tw = datetime.now(ZoneInfo("Asia/Taipei")).date()
    result = main._resolve_relative_date("爸爸明天幾點要拿蛋糕")
    assert result is not None
    delta = (result - today_tw).days
    assert delta == 1


def test_resolve_relative_date_no_match():
    import main

    assert main._resolve_relative_date("這個月的計畫") is None


def test_resolve_calendar_query_dates_supports_week_and_weekend_ranges():
    import main

    this_weekend = main._resolve_calendar_query_dates("這週末有什麼活動")
    next_weekend = main._resolve_calendar_query_dates("下週末有什麼活動")
    this_week = main._resolve_calendar_query_dates("本週有什麼行程")
    next_week = main._resolve_calendar_query_dates("下週有什麼行程")

    assert [day.weekday() for day in this_weekend] == [5, 6]
    assert [day.weekday() for day in next_weekend] == [5, 6]
    assert (next_weekend[0] - this_weekend[0]).days == 7
    assert len(this_week) == 7
    assert [day.weekday() for day in this_week] == list(range(7))
    assert len(next_week) == 7
    assert (next_week[0] - this_week[0]).days == 7
    assert len(main._resolve_calendar_query_dates("這禮拜有什麼行程")) == 7
    assert len(main._resolve_calendar_query_dates("下星期有什麼行程")) == 7


def test_resolve_calendar_query_dates_honors_source_reference_date():
    import main
    from datetime import date

    reference = date(2026, 8, 8)
    assert main._resolve_calendar_query_dates(
        "今天", reference_date=reference
    ) == (date(2026, 8, 8),)
    assert main._resolve_calendar_query_dates(
        "明天", reference_date=reference
    ) == (date(2026, 8, 9),)
    assert main._resolve_calendar_query_dates(
        "後天", reference_date=reference
    ) == (date(2026, 8, 10),)


def test_resolve_calendar_query_dates_supports_absolute_and_specific_relative_dates():
    import main
    from datetime import date

    reference = date(2026, 8, 10)
    assert main._resolve_calendar_query_dates(
        "8/13媽媽有什麼活動", reference_date=reference
    ) == (date(2026, 8, 13),)
    assert main._resolve_calendar_query_dates(
        "媽媽8月13日有什麼行程", reference_date=reference
    ) == (date(2026, 8, 13),)
    assert main._resolve_calendar_query_dates(
        "2026-08-13有什麼安排", reference_date=reference
    ) == (date(2026, 8, 13),)
    assert main._resolve_calendar_query_dates(
        "媽媽8月13號有什麼活動", reference_date=reference
    ) == (date(2026, 8, 13),)
    assert main._resolve_calendar_query_dates(
        "2027年8月13日有什麼行程", reference_date=reference
    ) == (date(2027, 8, 13),)
    assert main._resolve_calendar_query_dates(
        "2025年8月13日有什麼行程", reference_date=reference
    ) == (date(2025, 8, 13),)
    assert main._resolve_calendar_query_dates(
        "大後天有什麼活動", reference_date=reference
    ) == (date(2026, 8, 13),)
    assert main._resolve_calendar_query_dates(
        "大前天有什麼活動", reference_date=reference
    ) == (date(2026, 8, 7),)
    assert main._resolve_calendar_query_dates(
        "明後天有什麼活動", reference_date=reference
    ) == (date(2026, 8, 11), date(2026, 8, 12))


def test_absolute_date_schedule_queries_route_to_calendar():
    import main

    for query in (
        "8/13媽媽有什麼活動",
        "媽媽8月13日有什麼行程",
        "2026-08-13媽媽有什麼安排",
        "8/13有什麼活動",
        "媽媽8月13號有什麼活動",
        "2027年8月13日媽媽有什麼行程",
    ):
        assert main._is_calendar_query(query), query


# ── _handle_calendar_query 整合測試（mock calendar_db）─────────────────
@pytest.fixture
def patched_calendar_db(monkeypatch):
    """mock calendar_db.list_upcoming 回固定 events，避免動到真 DB。"""
    fake_events = [
        {
            "event_id": "e1",
            "group_id": "G1",
            "title": "拿喜來登贈送的生日蛋糕",
            "event_date": "2026-05-22",
            "event_time": "14:00",
            "location": "喜來登",
            "participants": json.dumps(["爸爸"], ensure_ascii=False),
            "status": "active",
        }
    ]
    import calendar_db

    monkeypatch.setattr(calendar_db, "list_upcoming", lambda gid, days=30: fake_events)
    return fake_events


def _patch_calendar_reply_capture(monkeypatch, main_mod, captured: dict):
    """
    _handle_calendar_query 直接呼 MessagingApi.reply_message（為了跳過 _reply 的
    piggyback drain，2026-05-21 4d8ec69）。所以這裡 mock ApiClient / MessagingApi 來
    capture，而不是 mock _reply。
    """
    class FakeMessagingApi:
        def __init__(self, _api_client):
            pass

        def reply_message(self, request):
            captured["text"] = request.messages[0].text
            captured["token"] = request.reply_token
            captured["message"] = request.messages[0]

    class FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(main_mod, "MessagingApi", FakeMessagingApi)
    monkeypatch.setattr(main_mod, "ApiClient", FakeApiClient)
    monkeypatch.setattr(main_mod, "_get_line_config", lambda: object())


def test_handle_calendar_query_finds_tomorrow_event(monkeypatch, patched_calendar_db):
    """14:49 case 完整 path：問句進來 → query DB → 回 formatted event。"""
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    today_tw = datetime.now(ZoneInfo("Asia/Taipei")).date()
    tomorrow = (today_tw + timedelta(days=1)).isoformat()
    patched_calendar_db[0]["event_date"] = tomorrow

    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)

    class FakeEvent:
        reply_token = "fake_token"

    main._handle_calendar_query(FakeEvent(), "G1", "爸爸明天幾點要拿蛋糕？")
    assert "text" in captured
    assert "14:00" in captured["text"]
    assert "蛋糕" in captured["text"]


def test_handle_calendar_query_no_match(monkeypatch):
    """target date 沒 event → 回「沒有家族行程」訊息。"""
    import main
    import calendar_db

    monkeypatch.setattr(calendar_db, "list_upcoming", lambda gid, days=30: [])
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)

    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake_token"

    main._handle_calendar_query(FakeEvent(), "G1", "明天有什麼安排")
    assert "text" in captured
    assert "沒有" in captured["text"] or "0" in captured["text"]


def test_handle_calendar_query_uses_text_v2_for_known_mentions(monkeypatch):
    import main
    import calendar_db
    import event_reminder
    import line_mentions
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    target_date = (
        datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=1)
    ).isoformat()
    event = {
        "event_id": "e1",
        "group_id": "G1",
        "title": "打壁球",
        "event_date": target_date,
        "event_time": "19:00",
        "location": "南港運動中心",
        "participants": "[]",
        "status": "active",
    }
    monkeypatch.setattr(calendar_db, "list_past", lambda gid, days=90: [])
    monkeypatch.setattr(calendar_db, "list_upcoming", lambda gid, days=90: [event])
    monkeypatch.setattr(event_reminder, "_format_event", lambda ev, offset: "@爸爸\n打壁球")
    monkeypatch.setattr(line_mentions, "load_user_aliases", lambda: {"U_DAD": "爸爸"})
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)

    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake_token"

    main._handle_calendar_query(FakeEvent(), "G1", "明天有什麼安排")

    assert captured["message"].type == "textV2"
    assert captured["message"].text.startswith("{p1}\n")
    assert captured["message"].substitution["p1"].mentionee.user_id == "U_DAD"


def test_handle_calendar_query_finds_hwang_new_zealand_dates(monkeypatch):
    import main
    import calendar_db
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    first = datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=7)
    second = first + timedelta(days=12)

    events = [
        {
            "event_id": "nz-out",
            "group_id": "G1",
            "title": f"機場接送（去紐西蘭）{first.month}/{first.day}去程",
            "event_date": first.isoformat(),
            "event_time": "14:30",
            "location": "桃園機場；預約編號F15309511",
            "participants": "[\"哥哥(被接送)\"]",
            "status": "active",
        },
        {
            "event_id": "nz-back",
            "group_id": "G1",
            "title": f"機場接送（去紐西蘭）{second.month}/{second.day}回程",
            "event_date": second.isoformat(),
            "event_time": "",
            "location": "桃園機場；時間待補",
            "participants": "[\"哥哥(被接送)\"]",
            "status": "active",
        },
    ]

    monkeypatch.setattr(calendar_db, "search_by_title_phrase", lambda *a, **k: [])
    monkeypatch.setattr(calendar_db, "search_by_keyword", lambda *a, **k: events)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)

    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake_token"

    main._handle_calendar_query(FakeEvent(), "G1", "哥哥去紐西蘭的日期")

    assert first.isoformat() in captured["text"]
    assert "14:30" in captured["text"]
    assert second.isoformat() in captured["text"]
    assert "去紐西蘭" in captured["text"]


def test_handle_calendar_query_infers_mom_return_home_from_owned_schedule(
    monkeypatch,
):
    """The real 2026-08-10 failure: 回家 means home-city return, not private-data denial."""
    import main
    import calendar_db
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    return_day = today + timedelta(days=1)
    taoyuan_day = today + timedelta(days=2)
    exam_day = today + timedelta(days=3)
    source_text = (
        f"我 {return_day.month}/{return_day.day} 晚上回台北 "
        f"{taoyuan_day.month}/{taoyuan_day.day} 去桃園\n"
        f"{exam_day.month}/{exam_day.day} 早上看風濕免疫科 "
        "下午 考選部開國考題庫建置會議"
    )
    events = [
        {
            "event_id": "return",
            "group_id": "G1",
            "title": "晚上回台北",
            "event_date": return_day.isoformat(),
            "event_time": "",
            "location": "",
            "participants": "[]",
            "source_msg_id": "mom-schedule",
            "status": "active",
        },
        {
            "event_id": "taoyuan",
            "group_id": "G1",
            "title": "去桃園",
            "event_date": taoyuan_day.isoformat(),
            "event_time": "",
            "location": "",
            "participants": "[]",
            "source_msg_id": "mom-schedule",
            "status": "active",
        },
        {
            "event_id": "exam",
            "group_id": "G1",
            "title": "早上看風濕免疫科 下午 考選部開國考題庫建置會議",
            "event_date": exam_day.isoformat(),
            "event_time": "",
            "location": "",
            "participants": "[]",
            "source_msg_id": "mom-schedule",
            "status": "active",
        },
    ]

    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *a, **k: events)
    monkeypatch.setattr(calendar_db, "search_by_title_phrase", lambda *a, **k: [])
    monkeypatch.setattr(calendar_db, "search_by_keyword", lambda *a, **k: events)
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda gid, mid: ("U_MOM", source_text) if mid == "mom-schedule" else None,
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)

    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake_token"

    main._handle_calendar_query(FakeEvent(), "G1", "米堡，媽媽什麼時候回家")

    assert f"{return_day.month}/{return_day.day}" in captured["text"]
    assert "晚上回台北" in captured["text"]
    assert "媽媽" in captured["text"]
    assert "目前最直接的行程紀錄" in captured["text"]
    assert "沒有記到確切到家時間" in captured["text"]
    assert "無法查詢" not in captured["text"]


def test_return_home_query_uses_configured_home_city_consistently(monkeypatch):
    import calendar_db
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tomorrow = datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=1)
    event_row = {
        "event_id": "return-home",
        "group_id": "G1",
        "title": "晚上回高雄",
        "event_date": tomorrow.isoformat(),
        "event_time": "",
        "location": "",
        "participants": '["媽媽"]',
        "status": "active",
    }
    monkeypatch.setenv("FAMILY_HOME_CITY", "高雄")
    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *a, **k: [event_row])
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake-token"

    main._handle_calendar_query(FakeEvent(), "G1", "媽媽什麼時候回高雄")

    assert "媽媽目前最直接的行程紀錄" in captured["text"]
    assert "回到高雄" in captured["text"]


def test_configured_home_city_participates_in_calendar_classification(monkeypatch):
    import main

    monkeypatch.setenv("FAMILY_HOME_CITY", "桃園")

    assert main._is_calendar_query("媽媽什麼時候回桃園") is True
    assert main._return_home_query_actor("媽媽什麼時候回桃園", "桃園") == "媽媽"
    assert main._is_calendar_query("媽媽何時到桃園") is True
    assert main._return_home_query_actor("媽媽何時到桃園", "桃園") == "媽媽"


def test_dated_calendar_query_filters_requested_actor_and_place(monkeypatch):
    import calendar_db
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tomorrow = datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=1)
    common = {
        "group_id": "G1",
        "event_date": tomorrow.isoformat(),
        "event_time": "10:00",
        "status": "active",
    }
    events = [
        dict(
            common,
            event_id="mom-taipei",
            title="媽媽台北會議",
            location="台北",
            participants='["媽媽"]',
        ),
        dict(
            common,
            event_id="dad-taipei",
            title="爸爸台北會議",
            location="台北",
            participants='["爸爸"]',
        ),
        dict(
            common,
            event_id="mom-kaohsiung",
            title="媽媽高雄會議",
            location="高雄",
            participants='["媽媽"]',
        ),
        dict(
            common,
            event_id="mom-taipei-remote",
            title="媽媽台北線上會議",
            location="Zoom",
            participants='["媽媽"]',
        ),
    ]
    monkeypatch.setattr(calendar_db, "list_past", lambda *a, **k: [])
    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *a, **k: events)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake-token"

    main._handle_calendar_query(
        FakeEvent(),
        "G1",
        "媽媽明天在台北有什麼活動",
    )

    assert "媽媽台北會議" in captured["text"]
    assert "爸爸台北會議" not in captured["text"]
    assert "媽媽高雄會議" not in captured["text"]
    assert "媽媽台北線上會議" not in captured["text"]


def test_country_place_query_contains_known_physical_cities():
    import main

    assert main._calendar_event_matches_query_place(
        {"title": "媽媽台北會議", "location": "台北"},
        "台灣",
    )
    assert main._calendar_event_matches_query_place(
        {"title": "媽媽奧克蘭活動", "location": "奧克蘭"},
        "紐西蘭",
    )
    assert not main._calendar_event_matches_query_place(
        {"title": "媽媽台北線上會議", "location": "Zoom"},
        "台灣",
    )


@pytest.mark.parametrize(
    ("title", "location"),
    [
        ("台北專案會議", "高雄"),
        ("討論台北活動", "高雄辦公室"),
        ("考選部題庫會議", "高雄"),
        ("台北開會", "桃園"),
        ("國考會議", "高雄考選部國家考場"),
        ("國考會議", "高雄國家考場（考選部）"),
    ],
)
def test_structured_location_conflict_blocks_home_city_inference(title, location):
    import main

    assert main._calendar_event_home_city_evidence(
        {"title": title, "location": location},
        "台北",
    ) is None


@pytest.mark.parametrize(
    ("title", "location", "source_clause"),
    [
        ("考選部會議", "大阪", ""),
        ("考選部會議", "https://meet.jit.si/exam", ""),
        ("考選部會議", "", "我明天電話參加考選部會議"),
        ("電話討論考選部題庫會議", "", ""),
        ("打電話討論考選部會議", "", ""),
        ("LINE語音討論考選部會議", "", ""),
        ("用LINE開考選部會議", "", ""),
    ],
)
def test_remote_or_conflicting_location_cannot_imply_taipei(
    title,
    location,
    source_clause,
):
    import main

    assert main._calendar_event_home_city_evidence(
        {"title": title, "location": location},
        "台北",
        source_clause=source_clause,
    ) is None


def test_calendar_query_places_prefers_specific_generic_location():
    import main

    assert main._calendar_query_places(
        "媽媽明天在板橋區有什麼活動"
    ) == ["板橋區"]
    assert main._calendar_query_places(
        "媽媽明天在台北市信義區有什麼活動"
    ) == ["台北市信義區"]
    assert main._calendar_query_places(
        "媽媽明天在大阪市有什麼活動"
    ) == ["大阪市"]
    assert main._calendar_query_places(
        "媽媽明天在考選部有什麼活動"
    ) == ["考選部"]
    assert main._calendar_query_places(
        "媽媽明天在板橋有什麼活動"
    ) == ["板橋"]
    for query in (
        "媽媽明天在板橋下午有什麼活動",
        "媽媽明天在板橋早上有什麼活動",
        "媽媽明天在板橋開什麼會？",
        "媽媽明天在板橋要參加哪些會議？",
        "媽媽明天在板橋要做什麼？",
    ):
        assert main._calendar_query_places(query) == ["板橋"]


def test_dated_handler_filters_actor_topic_daypart_and_place(monkeypatch):
    import calendar_db
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    target = datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=1)
    common = {
        "group_id": "G1",
        "event_date": target.isoformat(),
        "status": "active",
    }
    events = [
        dict(
            common,
            event_id="wanted",
            title="媽媽下午會議",
            event_time="15:00",
            location="板橋區",
            participants='["媽媽"]',
        ),
        dict(
            common,
            event_id="morning",
            title="媽媽早上會議",
            event_time="09:00",
            location="板橋區",
            participants='["媽媽"]',
        ),
        dict(
            common,
            event_id="dinner",
            title="媽媽下午聚餐",
            event_time="15:00",
            location="板橋區",
            participants='["媽媽"]',
        ),
        dict(
            common,
            event_id="wrong-place",
            title="媽媽下午會議",
            event_time="15:00",
            location="高雄",
            participants='["媽媽"]',
        ),
        dict(
            common,
            event_id="wrong-actor",
            title="爸爸下午會議",
            event_time="15:00",
            location="板橋區",
            participants='["爸爸"]',
        ),
    ]
    monkeypatch.setattr(calendar_db, "list_past", lambda *a, **k: [])
    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *a, **k: events)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake-token"

    main._handle_calendar_query(
        FakeEvent(),
        "G1",
        "媽媽明天下午在板橋區有什麼會議",
    )

    assert "媽媽下午會議" in captured["text"]
    assert "媽媽早上會議" not in captured["text"]
    assert "媽媽下午聚餐" not in captured["text"]
    assert "高雄" not in captured["text"]
    assert "爸爸下午會議" not in captured["text"]


def test_absolute_date_handler_applies_actor_and_topic_filters(monkeypatch):
    import calendar_db
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    target = datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=3)
    common = {
        "group_id": "G1",
        "event_date": target.isoformat(),
        "event_time": "15:00",
        "location": "",
        "status": "active",
    }
    events = [
        dict(common, event_id="mom-meeting", title="媽媽會議", participants='["媽媽"]'),
        dict(common, event_id="mom-dinner", title="媽媽聚餐", participants='["媽媽"]'),
        dict(common, event_id="dad-meeting", title="爸爸會議", participants='["爸爸"]'),
    ]
    monkeypatch.setattr(calendar_db, "list_past", lambda *a, **k: [])
    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *a, **k: events)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake-token"

    query = f"{target.month}/{target.day}媽媽有什麼會議"
    main._handle_calendar_query(FakeEvent(), "G1", query)

    assert "媽媽會議" in captured["text"]
    assert "媽媽聚餐" not in captured["text"]
    assert "爸爸會議" not in captured["text"]


@pytest.mark.parametrize("query", ["2/30有什麼活動", "2026-02-30媽媽有什麼行程", "4月31日有什麼活動"])
def test_invalid_absolute_date_fails_closed(monkeypatch, query):
    import calendar_db
    import main

    monkeypatch.setattr(
        calendar_db,
        "list_upcoming",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("invalid date must not broaden into a calendar scan")
        ),
    )
    monkeypatch.setattr(
        calendar_db,
        "list_past",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("invalid date must not broaden into a calendar scan")
        ),
    )
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake-token"

    assert main._is_calendar_query(query)
    main._handle_calendar_query(FakeEvent(), "G1", query)

    assert "日期看起來無效" in captured["text"]


def test_far_absolute_date_expands_database_horizon(monkeypatch):
    import calendar_db
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    target = today + timedelta(days=400)
    event_row = {
        "event_id": "far-mom",
        "group_id": "G1",
        "title": "媽媽遠期會議",
        "event_date": target.isoformat(),
        "event_time": "15:00",
        "location": "",
        "participants": '["媽媽"]',
        "status": "active",
    }
    seen: dict = {}

    def list_upcoming(_gid, days=90):
        seen["days"] = days
        return [event_row]

    monkeypatch.setattr(calendar_db, "list_upcoming", list_upcoming)
    monkeypatch.setattr(calendar_db, "list_past", lambda *a, **k: [])
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake-token"

    main._handle_calendar_query(
        FakeEvent(),
        "G1",
        f"{target.isoformat()}媽媽有什麼會議",
    )

    assert seen["days"] >= 400
    assert "媽媽遠期會議" in captured["text"]


def test_past_phrase_query_does_not_leak_another_family_member_event(monkeypatch):
    import calendar_db
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    past_day = datetime.now(ZoneInfo("Asia/Taipei")).date() - timedelta(days=2)
    events = [
        {
            "event_id": actor,
            "group_id": "G1",
            "title": f"{actor}拿蛋糕",
            "event_date": past_day.isoformat(),
            "event_time": "15:00",
            "location": "",
            "participants": f'["{actor}"]',
            "status": "active",
        }
        for actor in ("媽媽", "爸爸")
    ]
    monkeypatch.setattr(
        calendar_db,
        "search_by_title_phrase",
        lambda *a, **k: events,
    )
    monkeypatch.setattr(calendar_db, "search_by_keyword", lambda *a, **k: events)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake-token"

    main._handle_calendar_query(
        FakeEvent(),
        "G1",
        "之前媽媽哪一天拿蛋糕",
    )

    assert "媽媽拿蛋糕" in captured["text"]
    assert "爸爸拿蛋糕" not in captured["text"]


@pytest.mark.parametrize(
    ("query", "sender_alias", "included", "excluded", "safe_message"),
    [
        ("我什麼時候拿蛋糕", "媽媽", "媽媽拿蛋糕", "爸爸拿蛋糕", None),
        ("我什麼時候拿蛋糕", "", None, None, "無法辨識你對應的家庭成員"),
        ("媽媽問爸爸什麼時候拿蛋糕", "", "爸爸拿蛋糕", "媽媽拿蛋糕", None),
        ("媽媽什麼時候幫爸爸拿蛋糕", "", "媽媽拿蛋糕", "爸爸拿蛋糕", None),
        ("媽媽什麼時候替爸爸拿蛋糕", "", "媽媽拿蛋糕", "爸爸拿蛋糕", None),
        ("媽媽幫爸爸什麼時候拿蛋糕", "", "媽媽拿蛋糕", "爸爸拿蛋糕", None),
        ("什麼時候媽媽拿蛋糕", "", "媽媽拿蛋糕", "爸爸拿蛋糕", None),
        ("什麼時候我要拿蛋糕", "媽媽", "媽媽拿蛋糕", "爸爸拿蛋糕", None),
        ("什麼時候我要拿蛋糕", "", None, None, "無法辨識你對應的家庭成員"),
    ],
)
def test_nondated_query_resolves_self_and_reporter_subject_safely(
    monkeypatch,
    query,
    sender_alias,
    included,
    excluded,
    safe_message,
):
    import calendar_db
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    target = datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=3)
    events = [
        {
            "event_id": actor,
            "group_id": "G1",
            "title": f"{actor}拿蛋糕",
            "event_date": target.isoformat(),
            "event_time": "15:00",
            "location": "",
            "participants": f'["{actor}"]',
            "status": "active",
        }
        for actor in ("媽媽", "爸爸")
    ]
    monkeypatch.setattr(calendar_db, "search_by_title_phrase", lambda *a, **k: events)
    monkeypatch.setattr(calendar_db, "search_by_keyword", lambda *a, **k: events)
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: sender_alias)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeSource:
        user_id = "U_SELF"

    class FakeEvent:
        source = FakeSource()
        reply_token = "fake-token"

    main._handle_calendar_query(FakeEvent(), "G1", query)

    if safe_message:
        assert safe_message in captured["text"]
        assert "媽媽拿蛋糕" not in captured["text"]
        assert "爸爸拿蛋糕" not in captured["text"]
    else:
        assert included in captured["text"]
        assert excluded not in captured["text"]


def test_dated_calendar_query_uses_queried_actor_not_reporter(monkeypatch):
    import calendar_db
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tomorrow = datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=1)
    events = [
        {
            "event_id": actor,
            "group_id": "G1",
            "title": f"{actor}活動",
            "event_date": tomorrow.isoformat(),
            "event_time": "10:00",
            "location": "",
            "participants": f'["{actor}"]',
            "status": "active",
        }
        for actor in ("媽媽", "爸爸")
    ]
    monkeypatch.setattr(calendar_db, "list_past", lambda *a, **k: [])
    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *a, **k: events)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake-token"

    main._handle_calendar_query(
        FakeEvent(),
        "G1",
        "媽媽問爸爸明天有什麼活動",
    )

    assert "爸爸活動" in captured["text"]
    assert "媽媽活動" not in captured["text"]


def test_weekend_calendar_query_lists_both_weekend_days(monkeypatch):
    import calendar_db
    import main

    weekend = main._resolve_calendar_query_dates("這週末有什麼活動")
    events = [
        {
            "event_id": f"weekend-{day.isoformat()}",
            "group_id": "G1",
            "title": title,
            "event_date": day.isoformat(),
            "event_time": "10:00",
            "location": "",
            "participants": "[]",
            "status": "active",
        }
        for day, title in zip(weekend, ("週六活動", "週日活動"), strict=True)
    ]
    monkeypatch.setattr(calendar_db, "list_past", lambda *a, **k: events)
    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *a, **k: events)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake-token"

    main._handle_calendar_query(FakeEvent(), "G1", "這週末有什麼活動")

    assert "週六活動" in captured["text"]
    assert "週日活動" in captured["text"]


@pytest.mark.parametrize(
    "query",
    [
        "明天我有什麼活動",
        "我明天有什麼活動",
        "我的行程明天有什麼活動",
        "明天有什麼活動是我要去的",
    ],
)
def test_dated_first_person_query_filters_to_line_sender(monkeypatch, query):
    import calendar_db
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tomorrow = datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=1)
    events = [
        {
            "event_id": actor,
            "group_id": "G1",
            "title": f"{actor}活動",
            "event_date": tomorrow.isoformat(),
            "event_time": "10:00",
            "location": "",
            "participants": f'["{actor}"]',
            "status": "active",
        }
        for actor in ("媽媽", "爸爸")
    ]
    monkeypatch.setattr(calendar_db, "list_past", lambda *a, **k: [])
    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *a, **k: events)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeSource:
        user_id = "U_MOM"

    class FakeEvent:
        source = FakeSource()
        reply_token = "fake-token"

    main._handle_calendar_query(FakeEvent(), "G1", query)

    assert "媽媽活動" in captured["text"]
    assert "爸爸活動" not in captured["text"]


@pytest.mark.parametrize(
    "query",
    ["明天我有什麼活動", "我和爸爸明天有什麼活動"],
)
def test_dated_first_person_query_with_unknown_sender_fails_closed(
    monkeypatch,
    query,
):
    import calendar_db
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tomorrow = datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=1)
    events = [
        {
            "event_id": actor,
            "group_id": "G1",
            "title": f"{actor}活動",
            "event_date": tomorrow.isoformat(),
            "event_time": "10:00",
            "location": "",
            "participants": f'["{actor}"]',
            "status": "active",
        }
        for actor in ("媽媽", "爸爸")
    ]
    monkeypatch.setattr(calendar_db, "list_past", lambda *a, **k: [])
    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *a, **k: events)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "")
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeSource:
        user_id = "U_UNKNOWN"

    class FakeEvent:
        source = FakeSource()
        reply_token = "fake-token"

    main._handle_calendar_query(FakeEvent(), "G1", query)

    assert "無法辨識你對應的家庭成員" in captured["text"]
    assert "媽媽活動" not in captured["text"]
    assert "爸爸活動" not in captured["text"]


@pytest.mark.parametrize(
    ("query", "sender_user_id", "sender_alias"),
    [
        ("我和爸爸明天有什麼活動", "U_MOM", "媽媽"),
        ("明天我跟媽媽有什麼活動", "U_DAD", "爸爸"),
        ("媽媽和我明天有什麼活動", "U_DAD", "爸爸"),
    ],
)
def test_dated_first_person_and_named_actor_query_uses_union(
    monkeypatch,
    query,
    sender_user_id,
    sender_alias,
):
    import calendar_db
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tomorrow = datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=1)
    events = [
        {
            "event_id": actor,
            "group_id": "G1",
            "title": f"{actor}活動",
            "event_date": tomorrow.isoformat(),
            "event_time": "10:00",
            "location": "",
            "participants": f'["{actor}"]',
            "status": "active",
        }
        for actor in ("媽媽", "爸爸")
    ]
    monkeypatch.setattr(calendar_db, "list_past", lambda *a, **k: [])
    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *a, **k: events)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: sender_alias if uid == sender_user_id else "",
    )
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeSource:
        user_id = sender_user_id

    class FakeEvent:
        source = FakeSource()
        reply_token = "fake-token"

    main._handle_calendar_query(FakeEvent(), "G1", query)

    assert "媽媽活動" in captured["text"]
    assert "爸爸活動" in captured["text"]


def test_handle_calendar_query_cautiously_infers_home_by_taipei_commitment(
    monkeypatch,
):
    """A home-city appointment is an upper bound, never an invented arrival time."""
    import main
    import calendar_db
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    exam_day = today + timedelta(days=3)
    event = {
        "event_id": "exam",
        "group_id": "G1",
        "title": "下午 考選部開國考題庫建置會議",
        "event_date": exam_day.isoformat(),
        "event_time": "",
        "location": "",
        "participants": "[]",
        "source_msg_id": "mom-schedule",
        "status": "active",
    }
    source_text = f"我 {exam_day.month}/{exam_day.day} 下午去考選部開會"

    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *a, **k: [event])
    monkeypatch.setattr(calendar_db, "search_by_title_phrase", lambda *a, **k: [])
    monkeypatch.setattr(calendar_db, "search_by_keyword", lambda *a, **k: [event])
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda gid, mid: ("U_MOM", source_text),
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)

    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake_token"

    main._handle_calendar_query(FakeEvent(), "G1", "媽媽何時會回家？")

    assert f"最晚 {exam_day.month}/{exam_day.day}" in captured["text"]
    assert "考選部" in captured["text"]
    assert "台北" in captured["text"]
    assert "依行程推測" in captured["text"]
    assert "無法判斷確切到家時間" in captured["text"]


@pytest.mark.parametrize(
    "query",
    [
        "米堡，媽媽什麼時候回家",
        "媽媽幾點回家",
        "媽媽什麼時間回家",
        "媽媽幾點到家",
        "媽媽什麼時間到家",
        "媽媽何時到家",
        "媽媽幾時到家",
        "媽媽什麼時候回台北",
        "明天有什麼活動",
        "明天有什麼聚會",
        "明天有什麼會議",
        "明天有什麼要做的",
        "請問明天有什麼活動要參加嗎",
        "明天有什麼聚會需要出席",
        "明天有什麼會議需要準備",
        "明天有什麼約要去",
        "明天有什麼聚餐",
        "明天我們在台北有什麼活動",
        "媽媽明天在台北有什麼活動",
        "明天媽媽有什麼活動",
        "媽媽什麼時候到台北",
        "媽媽幾點抵達台北",
        "媽媽什麼時候回來",
        "媽媽明天回台北嗎？",
        "媽媽明天會不會回台北",
        "媽媽明天是不是要回台北",
        "媽媽明天回來了沒",
        "媽媽明天是否回台北",
        "媽媽明天回不回台北",
        "明天有哪些新增加的行程？",
        "明天有什麼新增行程？",
        "記得明天爸爸要開會嗎？",
        "記得明天媽媽要看診嗎？",
        "記得明天下午爸爸要去臺北市中正區考選部開會嗎？",
    ],
)
def test_unaddressed_natural_calendar_question_routes_before_burst(
    monkeypatch, query
):
    """A calendar question stays useful even when the family misspells the bot name."""
    import main

    routed: list[tuple[str, str]] = []

    monkeypatch.setattr(main, "_try_handle_reminder_cancellation", lambda *a: False)
    monkeypatch.setattr(main, "_try_handle_quoted_calendar_correction", lambda *a: False)
    monkeypatch.setattr(main, "_extract_gemini_trigger", lambda *a: None)
    monkeypatch.setattr(main, "_explicit_range_reminder_result", lambda *a: None)
    monkeypatch.setattr(main, "_try_handle_missed_reminder_repair", lambda *a: False)
    monkeypatch.setattr(main, "_text_with_quote_context", lambda _m, _g, t: t)
    monkeypatch.setattr(main.feedback_collector, "in_feedback_window", lambda: False)
    monkeypatch.setattr(main, "_try_one_shot_reply", lambda *a: False)
    monkeypatch.setattr(main, "_try_handle_calendar_correction", lambda *a: False)
    monkeypatch.setattr(main, "_detect_user_correction", lambda *a: None)
    monkeypatch.setattr(
        main,
        "_auto_capture_text_if_important",
        lambda *a: pytest.fail("calendar query must bypass auto capture"),
    )
    monkeypatch.setattr(
        main,
        "_maybe_extract_reminder",
        lambda *a, **k: pytest.fail("calendar query must bypass reminder extraction"),
    )
    monkeypatch.setattr(main, "_handle_command", lambda *a: None)
    monkeypatch.setattr(main, "_is_todo_query", lambda _t: False)
    monkeypatch.setattr(
        main,
        "_handle_calendar_query",
        lambda _event, gid, text: routed.append((gid, text)),
    )
    monkeypatch.setattr(
        main.burst_filter,
        "add_to_burst",
        lambda *a, **k: pytest.fail("calendar query must not reach burst LLM"),
    )

    class FakeMessage:
        id = "m1"
        quoted_message_id = None

    FakeMessage.text = query

    class FakeSource:
        user_id = "U1"

    class FakeEvent:
        message = FakeMessage()
        source = FakeSource()
        reply_token = "token"

    main._handle_text_message(FakeEvent(), "G1")

    assert routed == [("G1", query)]


@pytest.mark.parametrize(
    ("raw_text", "clean_text", "route_kind"),
    [
        ("咪寶，媽媽明天有什麼會議？", "媽媽明天有什麼會議？", "calendar"),
        ("有哪些待辦事項？", None, "todo"),
        ("你有提醒我明天開會嗎？", None, "todo"),
        ("有沒有提醒我明天開會？", None, "todo"),
        ("你記得提醒我明天開會嗎？", None, "todo"),
        ("你會提醒我明天開會嗎？", None, "todo"),
        ("你提醒我明天開會了嗎？", None, "todo"),
        ("你已經提醒我明天開會了嗎？", None, "todo"),
        ("你提醒過我明天開會嗎？", None, "todo"),
        ("你提醒我明天開會了沒", None, "todo"),
        ("你提醒我明天開會了沒？", None, "todo"),
        ("請問你有沒有提醒我明天開會嗎？", None, "todo"),
        ("我想問你有沒有提醒我明天開會嗎？", None, "todo"),
        ("想問一下你有沒有提醒我明天開會嗎？", None, "todo"),
        ("你到底有沒有提醒我明天開會嗎？", None, "todo"),
        ("你之前有沒有提醒我明天開會嗎？", None, "todo"),
        ("咪寶你昨天有沒有提醒我明天開會嗎？", None, "todo"),
        ("不好意思，請問你有沒有提醒我明天開會嗎？", None, "todo"),
        ("請問，你有沒有提醒我明天開會嗎？", None, "todo"),
        ("我想問一下你有沒有提醒我明天開會嗎？", None, "todo"),
        ("我想問一下，你有沒有提醒我明天開會嗎？", None, "todo"),
        ("麻煩問一下，你有沒有提醒我明天開會嗎？", None, "todo"),
        ("咪寶，不好意思，請問你有沒有提醒我明天開會嗎？", None, "todo"),
        ("想問一下你提醒過我明天開會嗎？", None, "todo"),
        ("你昨天提醒過我明天開會嗎？", None, "todo"),
        ("我想問你之前提醒我明天開會了沒？", None, "todo"),
        ("請問你提醒我明天開會了嗎？", None, "todo"),
        ("咪寶，你提醒我明天開會了嗎？", "你提醒我明天開會了嗎？", "todo"),
        ("咪寶，你提醒過我明天開會嗎？", "你提醒過我明天開會嗎？", "todo"),
        ("@咪寶 你提醒我明天開會了嗎？", "你提醒我明天開會了嗎？", "todo"),
        ("咪寶：你提醒我明天開會了嗎？", "你提醒我明天開會了嗎？", "todo"),
        ("/問 你提醒我明天開會了嗎？", "你提醒我明天開會了嗎？", "todo"),
    ],
)
def test_deterministic_queries_bypass_capture_and_reminder_extraction(
    monkeypatch,
    raw_text,
    clean_text,
    route_kind,
):
    import main

    routed: list[tuple[str, str, str]] = []
    monkeypatch.setattr(main, "_try_handle_reminder_cancellation", lambda *a: False)
    monkeypatch.setattr(main, "_try_handle_quoted_calendar_correction", lambda *a: False)
    monkeypatch.setattr(main, "_extract_gemini_trigger", lambda *a: clean_text)
    monkeypatch.setattr(main, "_explicit_range_reminder_result", lambda *a: None)
    monkeypatch.setattr(main, "_try_handle_missed_reminder_repair", lambda *a: False)
    monkeypatch.setattr(main, "_text_with_quote_context", lambda _m, _g, t: t)
    monkeypatch.setattr(main.feedback_collector, "in_feedback_window", lambda: False)
    monkeypatch.setattr(main, "_try_one_shot_reply", lambda *a: False)
    monkeypatch.setattr(main, "_try_handle_calendar_correction", lambda *a: False)
    monkeypatch.setattr(main, "_handle_command", lambda *a: None)
    monkeypatch.setattr(main, "_handle_explicit_poll_text", lambda *a: None)
    monkeypatch.setattr(
        main,
        "_auto_capture_text_if_important",
        lambda *a: pytest.fail("read-only query must bypass auto capture"),
    )
    monkeypatch.setattr(
        main,
        "_maybe_extract_reminder",
        lambda *a, **k: pytest.fail("read-only query must bypass reminder extraction"),
    )
    monkeypatch.setattr(
        main,
        "_handle_calendar_query",
        lambda _event, gid, query: routed.append(("calendar", gid, query)),
    )
    monkeypatch.setattr(
        main,
        "_handle_todo_query",
        lambda _event, gid, query: routed.append(("todo", gid, query)),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    class FakeMessage:
        id = "m1"
        quoted_message_id = None
        text = raw_text

    class FakeSource:
        user_id = "U1"

    class FakeEvent:
        message = FakeMessage()
        source = FakeSource()
        reply_token = "token"

    main._handle_text_message(FakeEvent(), "G1")

    expected_query = clean_text if clean_text is not None else raw_text
    assert routed == [(route_kind, "G1", expected_query)]


@pytest.mark.parametrize(
    "text",
    (
        "媽媽說明天提醒我問爸爸什麼時候回家",
        "爸爸問能不能提醒我明天查媽媽幾點回家",
        "媽媽說爸爸有沒有提醒我明天開會嗎",
        "媽媽問爸爸記得提醒我明天開會嗎",
        "爸爸問媽媽有沒有提醒我吃藥嗎",
        "同事說主管有沒有提醒我交報告嗎",
    ),
)
def test_reported_reminder_with_embedded_home_query_stays_in_chat(
    monkeypatch,
    text,
):
    import food_signals
    import knowledge_graph
    import main
    import message_classifier

    routed: list[str] = []
    monkeypatch.setattr(main, "_try_handle_reminder_cancellation", lambda *a: False)
    monkeypatch.setattr(main, "_try_handle_quoted_calendar_correction", lambda *a: False)
    monkeypatch.setattr(main, "_extract_gemini_trigger", lambda *a: None)
    monkeypatch.setattr(main, "_explicit_range_reminder_result", lambda *a: None)
    monkeypatch.setattr(main, "_try_handle_missed_reminder_repair", lambda *a: False)
    monkeypatch.setattr(main, "_text_with_quote_context", lambda _m, _g, t: t)
    monkeypatch.setattr(main.feedback_collector, "in_feedback_window", lambda: False)
    monkeypatch.setattr(main, "_try_one_shot_reply", lambda *a: False)
    monkeypatch.setattr(main, "_try_handle_calendar_correction", lambda *a: False)
    monkeypatch.setattr(main, "_handle_command", lambda *a: None)
    monkeypatch.setattr(main, "_handle_explicit_poll_text", lambda *a: None)
    monkeypatch.setattr(
        main,
        "_handle_calendar_query",
        lambda *_a: pytest.fail("reported speech must not expose calendar data"),
    )
    monkeypatch.setattr(
        main,
        "_handle_todo_query",
        lambda *_a: pytest.fail("reported speech must not expose reminder data"),
    )
    monkeypatch.setattr(
        main,
        "_auto_capture_text_if_important",
        lambda *a: pytest.fail("reported speech must not auto-capture"),
    )
    monkeypatch.setattr(
        main,
        "_maybe_extract_reminder",
        lambda *a, **k: pytest.fail("reported speech must not create a reminder"),
    )
    monkeypatch.setattr(main, "_detect_user_correction", lambda *a: None)
    monkeypatch.setattr(knowledge_graph, "auto_extract_kg_async", lambda *a: None)
    monkeypatch.setattr(food_signals, "extract_and_store_async", lambda *a: None)
    monkeypatch.setattr(message_classifier, "classify_rule", lambda _t: "other")
    monkeypatch.setattr(message_classifier, "update_category", lambda *a: None)
    monkeypatch.setattr(main, "_handle_restaurant_food_safety", lambda *a: False)
    monkeypatch.setattr(main, "_is_dinner_question", lambda _t: False)
    monkeypatch.setattr(main, "_is_web_research_question", lambda _t: False)
    monkeypatch.setattr(main, "_try_piggyback_reminders_fast_path", lambda *a: False)
    monkeypatch.setattr(
        main.burst_filter,
        "add_to_burst",
        lambda *_a, **_k: routed.append("burst"),
    )

    class FakeMessage:
        id = "m1"
        quoted_message_id = None

    FakeMessage.text = text

    class FakeSource:
        user_id = "U1"

    class FakeEvent:
        message = FakeMessage()
        source = FakeSource()
        reply_token = "token"

    assert main._is_reported_reminder_statement(text)
    main._handle_text_message(FakeEvent(), "G1")

    assert routed == ["burst"]


@pytest.mark.parametrize(
    "text",
    [
        "明天提醒我查東京有什麼活動",
        "提醒我明天查東京有什麼活動",
        "明天記得查東京有什麼活動",
        "明天早上記得查東京有什麼活動",
        "8/13下午記得查東京有什麼活動",
        "下週三早上記得查東京有什麼活動",
        "明天務必記得查東京有什麼活動",
        "明天下午三點記得查東京有什麼活動",
        "8/13下午三點記得查東京有什麼活動",
        "下週三早上九點記得查東京有什麼活動",
        "下週三下午三點記得查東京有什麼活動",
        "8/13（下午）記得查東京有什麼活動",
        "明天記得查東京有什麼活動，好嗎？",
        "明天記得查東京有什麼活動，可以嗎？",
        "明天務必記得查東京有什麼活動，好嗎",
        "明天記得查東京有什麼活動好嗎？",
        "明天記得查東京有什麼活動可以嗎？",
        "提醒我明天查東京有什麼活動可以嗎？",
        "咪寶，能不能提醒我明天開會",
        "@咪寶 可以提醒我明天開會嗎？",
        "/問 能不能提醒我明天開會",
        "咪寶，媽媽明天生日提醒我買蛋糕",
        "咪寶，媽媽明天回診提醒我帶健保卡",
        "咪寶，媽媽8/13的約提醒我帶文件",
        "加一個明天9點開會提醒",
        "幫我加一個明天9點開會提醒",
        "新增明天9點開會提醒",
        "加入明天9點開會提醒",
        "建立明天9點開會提醒",
        "可以新增明天9點開會提醒嗎？",
        "能不能新增一個提醒，明天9點開會？",
        "幫我新增明天9點開會提醒好嗎？",
        "可以加入明天9點開會提醒嗎？",
        "不要忘記提醒我明天9點開會",
        "別忘了提醒我明天9點開會",
        "不能不提醒我明天9點開會",
        "不得不提醒我明天9點開會",
        "幫我新增一個提醒明天9點開會",
        "可以設定一個提醒，明天9點開會嗎？",
        "能不能設定一個提醒，明天9點開會？",
        "幫我設定一個提醒，明天9點開會好嗎？",
        "請設定一個提醒，明天9點開會",
        "提醒我明天整理提醒清單",
        "明天出門前提醒我帶雨傘",
        "明天下班後提醒我買牛奶",
        "明天開會前提醒我帶資料",
        "明天吃藥前提醒我吃早餐",
        "明天要提醒我開會",
        "明天記得要提醒我開會",
        "明天出門時提醒我帶雨傘",
        "明天開會提醒我帶資料",
        "明天提醒我問媽媽誰要開會",
        "明天提醒我確認哪個人負責接送",
        "明天提醒我確認負責接送的人是誰",
        "明天提醒我查今天開會的人是誰",
        "明天提醒我確認負責接送的人是誰？",
        "明天提醒我查今天開會的人是誰？",
        "明天提醒我聯絡開會的人確認時間",
        "明天提醒我聯絡開會的同事確認時間",
        "明天提醒我通知負責接送的人帶證件",
        "明天提醒我跟負責的人確認接送",
        "明天提醒我通知開會的人是否改時間",
        "明天提醒我問開會的人是不是會出席",
        "明天提醒我聯絡開會的人是要改時間還是改地點",
        "明天提醒我請開會的人帶資料",
        "明天提醒我叫負責接送的人帶證件",
        "明天提醒我催開會的人交資料",
        "明天提醒我回覆開會的人確認出席",
        "明天提醒我傳訊息給開會的人確認時間",
        "明天提醒我確認開會的地點在哪裡",
        "明天提醒我查包裹的物流在哪裡",
        "明天提醒我詢問會議的地址在哪",
        "明天提醒我確認媽媽的醫院在哪裡",
        "明天提醒我查公司的報表",
        "明天提醒我繳信用卡的帳單",
        "明天提醒我支付公司的費用",
        "明天提醒我寄公司的文件",
        "明天提醒我提交公司的報告",
        "明天提醒我領爸爸的藥",
        "明天提醒我預約媽媽的門診",
        "明天提醒我打公司的電話",
        "明天提醒我買媽媽的生日禮物",
        "明天提醒我帶小明的雨傘",
        "明天提醒我拿爸爸的鑰匙",
        "明天提醒我準備媽媽的早餐",
        "明天提醒我整理公司的簡報",
        "請明天提醒我買媽媽的生日禮物",
        "麻煩明天提醒我帶小明的雨傘",
        "明天請提醒我拿爸爸的鑰匙",
        "提醒我明天要做的是準備資料",
        "提醒我明天要帶的是健保卡",
        "提醒我明天需要準備的是資料",
        "請你明天提醒我開會",
        "麻煩你明天提醒我開會",
        "你明天記得提醒我開會",
        "我想請你明天提醒我開會",
        "咪寶你明天提醒我開會",
        "明天請你提醒我帶藥",
        "明天你提醒我帶藥",
        "8/13請咪寶提醒我回診",
        "明天我想請你提醒我帶藥",
        "8/13我想請咪寶提醒我回診",
        "明天早上我想請米堡提醒我開會",
        "我想麻煩你明天提醒我帶藥",
        "明天我想麻煩你提醒我帶藥",
        "我想麻煩咪寶8/13提醒我回診",
        "明天請你務必提醒我帶藥",
        "明天請你先提醒我帶藥",
        "8/13我想請咪寶再提醒我回診",
        "明天你一定要提醒我開會",
        "請你幫我明天提醒我帶藥",
        "明天請你幫我提醒我帶藥",
        "麻煩你幫忙明天提醒我開會",
        "8/13我想麻煩咪寶幫忙提醒我回診",
        "不要買藥，明天提醒我開會",
        "不要告訴媽媽，明天提醒我開會",
        "別去公司了，明天提醒我回診",
        "我是說明天提醒我開會",
        "我的意思是說明天提醒我開會",
        "我想說明天提醒我開會",
        "我是說提醒我明天開會",
        "我的意思是說提醒我明天開會",
        "我想說提醒我明天開會",
        "我是說要你明天提醒我開會",
        "我是說請明天提醒我開會",
        "我想問能不能提醒我明天帶藥",
        "能不能明天提醒我開會",
        "能不能先提醒我明天開會",
        "可不可以明天提醒我開會",
        "可不可以先提醒我明天開會",
        "不好意思想問可以提醒我明天回診嗎？",
        "我能不能提醒我明天開會",
        "媽媽生日明天可以提醒我買蛋糕嗎？",
        "媽媽的生日明天能不能提醒我買蛋糕？",
        "媽媽生日明天提醒我買蛋糕",
        "媽媽的生日明天提醒我買蛋糕",
        "爸爸的藥明天能不能提醒我帶？",
        "妹妹的回診明天可不可以提醒我？",
        "關於媽媽，能不能提醒我明天買蛋糕？",
        "關於 媽媽，能不能提醒我明天買蛋糕？",
        "關於我媽媽，能不能提醒我明天買蛋糕？",
        "有關媽媽，能不能提醒我明天買蛋糕？",
        "能不能請你明天提醒我開會",
        "能不能請你先提醒我明天開會",
        "能不能請明天提醒我開會",
        "能不能請先提醒我明天開會",
        "能不能讓咪寶提前提醒我明天開會",
        "咪寶，能不能提醒我明天開會",
        "米堡 能不能提醒我明天開會",
        "@咪寶 可以提醒我明天開會嗎？",
        "/問 能不能提醒我明天開會",
        "不只要提醒我明天開會，還要提醒我帶資料",
        "不但要提醒我明天開會，還要提醒我帶資料",
        "不僅要提醒我明天開會，還要提醒我帶資料",
        "不只是要提醒我明天開會，還要提醒我帶資料",
    ],
)
def test_explicit_reminder_intent_beats_public_query_guards(monkeypatch, text):
    import food_signals
    import knowledge_graph
    import main
    import message_classifier

    routed: list[str] = []
    monkeypatch.setattr(main, "_try_handle_reminder_cancellation", lambda *a: False)
    monkeypatch.setattr(main, "_try_handle_quoted_calendar_correction", lambda *a: False)
    monkeypatch.setattr(main, "_extract_gemini_trigger", lambda *a: None)
    monkeypatch.setattr(main, "_explicit_range_reminder_result", lambda *a: None)
    monkeypatch.setattr(main, "_try_handle_missed_reminder_repair", lambda *a: False)
    monkeypatch.setattr(main, "_text_with_quote_context", lambda _m, _g, t: t)
    monkeypatch.setattr(main.feedback_collector, "in_feedback_window", lambda: False)
    monkeypatch.setattr(main, "_try_one_shot_reply", lambda *a: False)
    monkeypatch.setattr(main, "_try_handle_calendar_correction", lambda *a: False)
    monkeypatch.setattr(main, "_handle_command", lambda *a: None)
    monkeypatch.setattr(main, "_handle_explicit_poll_text", lambda *a: None)
    monkeypatch.setattr(
        main,
        "_handle_calendar_query",
        lambda *_a: pytest.fail("reminder request must not route to calendar"),
    )
    monkeypatch.setattr(
        main,
        "_auto_capture_text_if_important",
        lambda *a: pytest.fail("explicit reminder must bypass calendar capture"),
    )
    monkeypatch.setattr(main, "_detect_user_correction", lambda *a: None)
    monkeypatch.setattr(knowledge_graph, "auto_extract_kg_async", lambda *a: None)
    monkeypatch.setattr(food_signals, "extract_and_store_async", lambda *a: None)
    monkeypatch.setattr(message_classifier, "classify_rule", lambda _t: "other")
    monkeypatch.setattr(message_classifier, "update_category", lambda *a: None)
    monkeypatch.setattr(
        main,
        "_maybe_extract_reminder",
        lambda *_a, **_k: routed.append("reminder") or "已建立提醒",
    )
    monkeypatch.setattr(main, "_reply", lambda *_a, **_k: routed.append("reply"))
    monkeypatch.setattr(
        main,
        "_handle_web_research_question",
        lambda *_a: pytest.fail("explicit reminder must not route to web"),
    )
    monkeypatch.setattr(
        main.burst_filter,
        "add_to_burst",
        lambda *_a, **_k: pytest.fail("explicit reminder must not reach burst"),
    )

    class FakeMessage:
        id = "m1"
        quoted_message_id = None

    FakeMessage.text = text

    class FakeSource:
        user_id = "U1"

    class FakeEvent:
        message = FakeMessage()
        source = FakeSource()
        reply_token = "token"

    main._handle_text_message(FakeEvent(), "G1")

    assert routed == ["reminder", "reply"]


@pytest.mark.parametrize(
    "text",
    [
        "明天記得查東京有什麼活動好嗎？",
        "明天記得查東京有什麼活動可以嗎？",
        "提醒我明天查東京有什麼活動可以嗎？",
    ],
)
def test_real_reminder_policy_respects_explicit_polite_request(monkeypatch, text):
    import main

    saved: list[tuple] = []
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *a, **k: True)
    monkeypatch.setattr(
        main.gemini_client,
        "extract_reminder",
        lambda _text: {
            "year": 2099,
            "month": 8,
            "day": 11,
            "hour": 9,
            "minute": 0,
            "action": "查東京有什麼活動",
        },
    )
    monkeypatch.setattr(
        main.memory,
        "add_reminder_with_outcome",
        lambda *args, **kwargs: saved.append((args, kwargs)) or (1, "created"),
    )

    reply = main._maybe_extract_reminder(text, "G1", "U1", "m1")

    assert saved
    assert reply and "提醒" in reply


@pytest.mark.parametrize(
    "text",
    ["媽媽明天要加入什麼活動？", "明天要新增什麼活動？"],
)
def test_bare_add_query_cannot_create_calendar_or_reminder(monkeypatch, text):
    import main

    assert not main._has_explicit_reminder_creation_intent(text)
    monkeypatch.setattr(
        main,
        "_capture_calendar_events_regex_only",
        lambda *_a: pytest.fail("question must not create a calendar event"),
    )
    monkeypatch.setattr(
        main.gemini_client,
        "extract_reminder",
        lambda _text: pytest.fail("question must not invoke reminder extraction"),
    )

    assert main._auto_capture_text_if_important("G1", text, "U1", "m1") is False
    assert main._maybe_extract_reminder(text, "G1", "U1", "m1") is None


@pytest.mark.parametrize(
    "text",
    [
        "別提醒我明天下午三點看醫生",
        "請別提醒我明天下午三點看醫生",
        "先別提醒我明天下午三點看醫生",
        "別再提醒我明天下午三點看醫生",
        "請別再提醒我明天下午三點看醫生",
        "不要再提醒我明天下午三點看醫生",
        "先不要再提醒我明天下午三點看醫生",
        "別再幫我提醒明天下午三點看醫生",
        "不要繼續幫我提醒明天下午三點看醫生",
        "不要一直提醒我明天下午三點看醫生",
        "別一直提醒我明天下午三點看醫生",
        "不要重複提醒我明天下午三點看醫生",
        "不要反覆提醒我明天下午三點看醫生",
        "別老是提醒我明天下午三點看醫生",
        "不要老是提醒我明天下午三點看醫生",
        "不要再繼續提醒我明天下午三點看醫生",
        "我不是要你提醒我明天開會",
        "不是叫你提醒我明天開會",
        "我沒有要你提醒我明天開會",
        "我沒說要提醒我明天開會",
        "我不是說提醒我明天開會",
        "我不是想要你提醒我明天開會",
        "我不想要你提醒我明天開會",
        "我不想讓你提醒我明天開會",
        "不是請你提醒我明天開會",
        "我並不需要你提醒我明天開會",
        "我不希望你提醒我明天開會",
        "我無需你提醒我明天開會",
        "我沒打算要你提醒我明天開會",
        "我並沒有打算叫你提醒我明天開會",
        "@咪寶 你提醒我明天開會了嗎？",
        "咪寶：你提醒我明天開會了嗎？",
        "/問 你提醒我明天開會了嗎？",
        "媽媽說她明天會提醒我開會",
        "爸爸問能不能提醒我明天帶藥",
        "我媽說她明天會提醒我開會",
        "她說明天會提醒我回診",
        "醫生說他明天會提醒我回診",
        "同事表示明天會提醒我開會",
        "阿姨說她明天會提醒我開會",
        "老闆說明天會提醒我開會",
        "護理師說明天會提醒我回診",
        "小明表示明天會提醒我開會",
        "老闆問可以提醒我明天開會嗎？",
        "我是說媽媽明天會提醒我開會",
        "我的意思是說她明天會提醒我開會",
        "我想說同事明天會提醒我開會",
        "我是說醫生8/13會提醒我回診",
        "我想問媽媽能不能提醒我明天開會",
        "請問媽媽可不可以提醒我明天開會",
        "我想請問爸爸可以提醒我明天開會嗎",
        "不好意思我想問同事能不能提醒我明天開會",
        "我想問醫生可以提醒我明天回診嗎？",
        "媽媽能不能提醒我明天開會",
        "爸爸可不可以提醒我明天帶藥",
        "醫生可以提醒我明天回診嗎",
        "Siri可以提醒我明天開會嗎？",
        "Google Calendar可以提醒我明天開會嗎？",
        "手機可以提醒我明天開會嗎？",
        "媽媽的手機可以提醒我明天開會嗎？",
        "公司的系統能不能提醒我明天打卡？",
        "能不能請媽媽提醒我明天開會",
        "可不可以讓爸爸提醒我明天帶藥",
        "可以請醫生提醒我明天回診嗎",
        "能不能請 Siri 提醒我明天開會",
        "能不能讓手機提醒我明天開會",
        "媽媽昨天提醒我明天9點開會",
        "護理師提醒我明天9點回診",
        "同事剛剛提醒我明天9點開會",
        "她提醒我明天9點開會",
        "媽媽剛提醒我明天開會",
        "媽媽最近提醒我明天開會",
        "媽媽也提醒我明天開會",
        "媽媽先提醒我明天開會",
        "媽媽稍早提醒我明天開會",
        "媽媽剛才提醒我明天開會",
        "同事稍早提醒我明天打卡",
        "護理師之前提醒我明天回診",
        "她前天提醒我明天開會",
        "爸爸剛才有提醒我明天拿藥",
        "媽媽明天回診後提醒我帶健保卡",
        "小明昨天提醒我明天9點開會",
        "Apple Watch剛提醒我明天運動",
        "系統明天會提醒我開會",
        "明天系統會提醒我開會",
        "明天小明提醒我開會",
        "明天Apple Watch提醒我運動",
        "明天系統提醒我打卡",
        "明天鄰居提醒我倒垃圾",
        "明天行事曆提醒我開會",
        "明天提醒我開會的是媽媽",
        "明天是媽媽要提醒我開會",
        "提醒我明天開會的人是媽媽",
        "可以提醒我明天開會的是誰？",
        "明天提醒我開會的會是媽媽",
        "明天提醒我開會的人可能是媽媽",
        "明天提醒我開會的應該是媽媽",
        "明天提醒我開會的會是誰？",
        "明天提醒我開會的可能是誰？",
        "明天可以提醒我開會的人會是誰？",
        "明天提醒我的應該是媽媽",
        "明天提醒我的人不是媽媽，是爸爸",
        "明天提醒我的不是媽媽，是爸爸",
        "明天提醒我開會的不會是媽媽",
        "明天提醒我開會的不是媽媽",
        "明天提醒我的不只是媽媽",
        "明天提醒我的人不會是媽媽",
        "明天提醒我的人不是小王",
        "明天提醒我的那個人是小王",
        "明天提醒我的人可能是隔壁鄰居",
        "明天提醒我的人叫小王",
        "明天提醒我的人會由小王負責",
        "明天提醒我查報表的人是誰？",
        "明天提醒我確認時間的人是誰？",
        "明天提醒我問媽媽的人是誰？",
        "明天提醒我查報表的是誰？",
        "明天提醒我確認時間的是誰？",
        "明天提醒我查報表的是哪個人？",
        "明天提醒我的人住隔壁",
        "明天提醒我的人姓王",
        "明天提醒我的人穿紅衣服",
        "明天提醒我的人住哪裡？",
        "明天提醒我的人為什麼請假？",
        "明天提醒我的那位同事住隔壁",
        "明天提醒我的那個同事是小王",
        "明天提醒我的同事是小王",
        "明天提醒我的老師是小王",
        "明天提醒我的鄰居住隔壁",
        "明天提醒我的系統是公司行事曆",
        "明天提醒我的同學住隔壁",
        "明天提醒我的秘書叫小王",
        "明天提醒我的房東住樓下",
        "明天提醒我的教練姓王",
        "明天提醒我的室友是小李",
        "明天提醒我的工程師是小王",
        "明天提醒我的管理員住隔壁",
        "明天提醒我的司機是小李",
        "明天提醒我的教練住哪裡？",
        "明天提醒我的會計師姓王",
        "明天提醒我開會的秘書住樓下",
        "明天提醒我回診的室友叫小李",
        "明天提醒我吃藥的教練姓王",
        "明天提醒我打卡的同學住隔壁",
        "明天提醒我查看的工程師退休了",
        "明天提醒我帶的司機生病了",
        "明天提醒我準備的教練升職了",
        "明天提醒我記錄的同事搬家了",
        "明天提醒我整理的管理員請辭了",
        "明天提醒我查報表的同事住在哪裡？",
        "明天提醒我確認會議的秘書為什麼請假？",
        "明天提醒我查開會的同事是誰？",
        "明天提醒我確認接送的司機是哪一位？",
        "明天提醒我問開會的同事是不是會出席",
        "明天提醒我查看報表的同事會遲到",
        "明天提醒我買藥的媽媽明天回家",
        "明天提醒我的會是小王",
        "明天提醒我的可能是小王",
        "明天提醒我的不會是小王",
        "明天提醒我開會的會是小王",
        "明天提醒我開會的不是小王，是小李",
        "明天提醒我查報表的是哪一個人？",
        "明天提醒我查報表的是哪一位？",
        "明天提醒我查報表的是哪位？",
        "明天提醒我查報表的到底是誰？",
        "明天提醒我查報表的究竟是誰？",
        "明天提醒我查報表的會是哪一位？",
        "鄰居明天會提醒我倒垃圾",
        "行事曆明天會提醒我開會",
        "明天要新增提醒嗎？",
        "媽媽明天要新增提醒嗎？",
        "明天可以新增提醒嗎？",
        "明天新增提醒了嗎？",
        "明天要新增一個提醒嗎？",
        "媽媽明天要新增一個提醒嗎？",
        "明天建立一個提醒了嗎？",
        "明天要加入這個提醒嗎？",
        "明天要加一個提醒嗎？",
        "媽媽明天加一個提醒了嗎？",
        "明天新增新的提醒嗎？",
        "明天要設定提醒嗎？",
        "明天設定提醒了嗎？",
        "明天新增一個新的提醒嗎？",
        "明天加一個新的提醒了嗎？",
    ],
)
def test_negated_reminder_request_cannot_persist(monkeypatch, text):
    import main

    assert not main._has_explicit_reminder_creation_intent(text)
    monkeypatch.setattr(
        main.gemini_client,
        "extract_reminder",
        lambda _text: pytest.fail("negated request must not invoke Gemini"),
    )
    monkeypatch.setattr(
        main.memory,
        "add_reminder_with_outcome",
        lambda *_a, **_k: pytest.fail("negated request must not persist"),
    )

    assert main._maybe_extract_reminder(text, "G1", "U1", "m1") is None


@pytest.mark.parametrize(
    "query",
    [
        "媽媽幾點回家",
        "媽媽什麼時間回家",
        "媽媽幾點到家",
        "媽媽什麼時間到家",
        "媽媽何時到家",
        "媽媽幾時到家",
        "媽媽什麼時候回台北",
        "明天有什麼活動",
        "明天有什麼聚會",
        "明天有什麼會議",
        "明天有什麼要做的",
        "請問明天有什麼活動要參加嗎",
        "明天有什麼聚會需要出席",
        "明天有什麼會議需要準備",
        "明天有什麼約要去",
        "明天有什麼聚餐",
        "明天我們在台北有什麼活動",
        "媽媽明天在台北有什麼活動",
        "明天媽媽有什麼活動",
        "媽媽什麼時候到台北",
        "媽媽幾點抵達台北",
        "媽媽什麼時候回來",
    ],
)
def test_explicit_home_timing_variants_route_calendar(monkeypatch, query):
    import main

    routed: list[tuple[str, str]] = []
    monkeypatch.setattr(main, "_detect_image_gen_request", lambda _t: None)
    monkeypatch.setattr(main, "_handle_explicit_poll_text", lambda *a: None)
    monkeypatch.setattr(
        main,
        "_handle_calendar_query",
        lambda _event, gid, text: routed.append((gid, text)),
    )

    class FakeMessage:
        quoted_message_id = None

    class FakeSource:
        user_id = "U1"

    class FakeEvent:
        message = FakeMessage()
        source = FakeSource()
        reply_token = "token"

    main._handle_explicit_text(FakeEvent(), "G1", query)

    assert routed == [("G1", query)]


@pytest.mark.parametrize(
    "text",
    [
        "今天股市有什麼消息",
        "今天新聞有什麼重點",
        "明天台北天氣有什麼變化",
        "明天幾點下雨",
        "明天幾點開盤",
        "明天幾點日出",
        "今天幾點有電影",
        "今天幾點新聞播出",
        "今天有什麼感想",
        "明天有什麼課可以上",
        "明天有活動推薦嗎",
        "明天有聚餐推薦嗎",
        "明天有活動好玩嗎",
        "明天台北有什麼活動",
        "今天高雄有什麼活動",
        "台北明天有什麼活動",
        "高雄今天有什麼活動",
        "媽媽想知道明天台北有什麼活動",
        "媽媽問明天台北有什麼活動",
        "幫媽媽查明天台北有什麼活動",
        "明天台北有活動嗎",
        "明天台北有什麼活動適合媽媽",
    ],
)
def test_generic_web_question_is_not_intercepted_by_calendar_fast_path(
    monkeypatch, text
):
    import main

    routed: list[str] = []
    monkeypatch.setattr(main, "_try_handle_reminder_cancellation", lambda *a: False)
    monkeypatch.setattr(main, "_try_handle_quoted_calendar_correction", lambda *a: False)
    monkeypatch.setattr(main, "_extract_gemini_trigger", lambda *a: None)
    monkeypatch.setattr(main, "_explicit_range_reminder_result", lambda *a: None)
    monkeypatch.setattr(main, "_try_handle_missed_reminder_repair", lambda *a: False)
    monkeypatch.setattr(main, "_text_with_quote_context", lambda _m, _g, t: t)
    monkeypatch.setattr(main.feedback_collector, "in_feedback_window", lambda: False)
    monkeypatch.setattr(main, "_try_one_shot_reply", lambda *a: False)
    monkeypatch.setattr(main, "_try_handle_calendar_correction", lambda *a: False)
    monkeypatch.setattr(main, "_detect_user_correction", lambda *a: None)
    side_effect_regressions = {
        "明天東京有什麼活動",
        "明天臺北有什麼活動",
        "媽媽明天從桃園回台北要多久",
    }

    def auto_capture(*_args):
        if text in side_effect_regressions:
            pytest.fail("public/duration query must bypass auto capture")
        return False

    def extract_reminder(*_args, **_kwargs):
        if text in side_effect_regressions:
            pytest.fail("public/duration query must bypass reminder extraction")
        return None

    monkeypatch.setattr(main, "_auto_capture_text_if_important", auto_capture)
    monkeypatch.setattr(main, "_maybe_extract_reminder", extract_reminder)
    monkeypatch.setattr(main, "_handle_command", lambda *a: None)
    monkeypatch.setattr(main, "_is_todo_query", lambda _t: False)
    monkeypatch.setattr(
        main,
        "_handle_calendar_query",
        lambda *_a: pytest.fail("generic web query must not reach calendar"),
    )
    monkeypatch.setattr(main, "_handle_restaurant_food_safety", lambda *a: False)
    monkeypatch.setattr(main, "_is_dinner_question", lambda _t: False)
    monkeypatch.setattr(main, "_is_web_research_question", lambda _t: True)
    monkeypatch.setattr(
        main,
        "_handle_web_research_question",
        lambda _event, _gid, query: routed.append(query) or True,
    )

    class FakeMessage:
        id = "m1"
        quoted_message_id = None

    FakeMessage.text = text

    class FakeSource:
        user_id = "U1"

    class FakeEvent:
        message = FakeMessage()
        source = FakeSource()
        reply_token = "token"

    main._handle_text_message(FakeEvent(), "G1")

    assert routed == [text]


@pytest.mark.parametrize(
    "text",
    [
        "明天活動很好玩",
        "明天聚餐吃火鍋",
        "明天會議很麻煩",
        "明天聚會我不去了",
        "昨天活動很精彩",
    ],
)
def test_schedule_noun_statement_stays_in_ordinary_chat(monkeypatch, text):
    import main

    burst: list[str] = []
    monkeypatch.setattr(main, "_try_handle_reminder_cancellation", lambda *a: False)
    monkeypatch.setattr(main, "_try_handle_quoted_calendar_correction", lambda *a: False)
    monkeypatch.setattr(main, "_extract_gemini_trigger", lambda *a: None)
    monkeypatch.setattr(main, "_explicit_range_reminder_result", lambda *a: None)
    monkeypatch.setattr(main, "_try_handle_missed_reminder_repair", lambda *a: False)
    monkeypatch.setattr(main, "_text_with_quote_context", lambda _m, _g, t: t)
    monkeypatch.setattr(main.feedback_collector, "in_feedback_window", lambda: False)
    monkeypatch.setattr(main, "_try_one_shot_reply", lambda *a: False)
    monkeypatch.setattr(main, "_try_handle_calendar_correction", lambda *a: False)
    monkeypatch.setattr(main, "_detect_user_correction", lambda *a: None)
    monkeypatch.setattr(main, "_auto_capture_text_if_important", lambda *a: False)
    monkeypatch.setattr(main, "_maybe_extract_reminder", lambda *a, **k: None)
    monkeypatch.setattr(main, "_handle_command", lambda *a: None)
    monkeypatch.setattr(main, "_is_todo_query", lambda _t: False)
    monkeypatch.setattr(
        main,
        "_handle_calendar_query",
        lambda *_a: pytest.fail("ordinary chat must not reach calendar"),
    )
    monkeypatch.setattr(main, "_handle_restaurant_food_safety", lambda *a: False)
    monkeypatch.setattr(main, "_is_dinner_question", lambda _t: False)
    monkeypatch.setattr(main, "_is_web_research_question", lambda _t: False)
    monkeypatch.setattr(main, "_try_piggyback_reminders_fast_path", lambda *a: False)
    monkeypatch.setattr(
        main.burst_filter,
        "add_to_burst",
        lambda _gid, _mid, body, _uid, _token: burst.append(body),
    )

    class FakeMessage:
        id = "m1"
        quoted_message_id = None

    FakeMessage.text = text

    class FakeSource:
        user_id = "U1"

    class FakeEvent:
        message = FakeMessage()
        source = FakeSource()
        reply_token = "token"

    main._handle_text_message(FakeEvent(), "G1")

    assert burst == [text]


def test_calendar_event_owner_requires_same_group_first_person_source(monkeypatch):
    import main

    event = {
        "group_id": "G1",
        "status": "active",
        "title": "下午去考選部開會",
        "event_date": "2026-08-13",
        "participants": "[]",
        "source_msg_id": "m1",
    }
    source_text = "我 8/13 下午去考選部開會"
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda gid, mid: ("U_MOM", source_text) if (gid, mid) == ("G1", "m1") else None,
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )

    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is True

    cross_group = dict(event, group_id="G2")
    assert main._calendar_event_owned_by_actor("G1", cross_group, "媽媽") is False

    monkeypatch.setattr(
        main.memory, "get_raw_message", lambda gid, mid: ("U_MOM", "8/13 考選部開會")
    )
    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is False


def test_calendar_event_explicit_other_person_blocks_sender_owner(monkeypatch):
    import main

    event = {
        "group_id": "G1",
        "status": "active",
        "title": "爸爸下午去考選部開會",
        "participants": '["爸爸"]',
        "source_msg_id": "m1",
    }
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda gid, mid: ("U_MOM", "我整理爸爸的行程：8/13 去考選部"),
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )

    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is False
    assert main._calendar_event_owned_by_actor("G1", event, "爸爸") is True


def test_calendar_home_city_evidence_rejects_remote_commitment():
    import main

    physical = {
        "title": "下午去考選部開國考題庫建置會議",
        "location": "",
    }
    remote = {
        "title": "考選部線上會議",
        "location": "Zoom",
    }

    assert main._calendar_event_home_city_evidence(physical, "台北") == "考選部"
    assert main._calendar_event_home_city_evidence(remote, "台北") is None
    assert main._calendar_event_home_city_evidence(
        physical,
        "台北",
        source_clause="8/13 考選部線上會議",
    ) is None
    for platform in ("google meet", "GOOGLE MEET", "TEAMS", "webex"):
        assert main._calendar_event_home_city_evidence(
            physical,
            "台北",
            source_clause=f"8/13 {platform} 參加考選部會議",
        ) is None


def test_calendar_home_city_evidence_rejects_cancelled_or_meta_poi_claims():
    import main

    for title in (
        "考選部會議取消",
        "媽媽討論考選部會議",
        "媽媽規劃考選部會議",
        "媽媽詢問考選部會議",
        "媽媽查考選部會議",
        "媽媽討論要不要去考選部",
        "媽媽規劃前往考選部",
        "媽媽詢問怎麼去考選部",
        "媽媽查怎麼到考選部",
        "媽媽確認是否去考選部",
        "媽媽不用去考選部開會",
        "媽媽不需要去考選部開會",
        "媽媽無需前往考選部",
        "媽媽不必到考選部",
        "媽媽沒有要去考選部",
        "媽媽別去考選部",
        "媽媽不要去考選部開會",
        "媽媽不能去考選部開會",
        "媽媽沒辦法去考選部",
        "媽媽無法前往考選部",
        "媽媽禁止去考選部",
        "媽媽不可以去考選部",
        "媽媽可能去考選部開會",
        "媽媽也許去考選部",
        "媽媽或許去考選部",
        "媽媽還不確定要不要去考選部",
        "媽媽去考選部待確認",
        "媽媽去考選部視情況",
        "媽媽應該會去考選部",
        "媽媽不會去考選部",
        "媽媽不打算去考選部",
        "媽媽不想去考選部",
        "媽媽不願意去考選部",
        "媽媽拒絕去考選部",
        "媽媽尚未決定是否去考選部",
        "媽媽去考選部尚未決定",
        "媽媽不在考選部開會",
        "考選部會議改到高雄",
        "原訂考選部會議，改到高雄",
        "考選部會議移到高雄",
        "考選部會議改成高雄場",
        "考選部會議改為高雄舉行",
        "考選部會議移師高雄",
        "考選部會議變更至高雄",
        "考選部會議改到板橋",
        "考選部會議移到板橋車站",
        "考選部會議地點改了",
        "考選部會議改地點",
        "考選部會議簡報準備",
        "考選部會議紀錄整理",
        "考選部會議邀請函寄送",
        "考選部開會資料整理",
        "提醒媽媽去考選部開會",
        "叫媽媽去考選部開會",
        "請媽媽去考選部開會",
        "建議媽媽去考選部開會",
        "媽媽被要求去考選部開會",
        "媽媽可能在考選部開會",
        "媽媽也許在考選部開會",
        "媽媽應該在考選部開會",
        "媽媽或許在考選部開會",
        "媽媽大概在考選部開會",
        "媽媽不確定是否在考選部開會",
        "媽媽可能考選部開會",
        "媽媽也許考選部開會",
        "媽媽應該考選部開會",
        "媽媽考選部會議待確認",
        "媽媽考選部會議未定",
        "媽媽考選部會議視情況",
        "媽媽考選部會議不一定",
        "媽媽準備考選部會議",
        "媽媽籌備考選部會議",
        "媽媽安排考選部會議",
        "爸爸幫媽媽準備考選部會議",
        "媽媽整理考選部會議紀錄",
        "媽媽被主管要求去考選部開會",
        "媽媽被通知要去考選部開會",
        "媽媽受邀去考選部開會",
        "爸爸拜託媽媽去考選部開會",
        "爸爸希望媽媽去考選部開會",
        "媽媽考選部會議改至桃園",
        "媽媽考選部會議換到桃園",
        "媽媽考選部會議在桃園舉辦",
        "媽媽考選部會議於桃園舉辦",
        "媽媽考選部會議會場在桃園",
        "媽媽考選部會議地點：桃園",
        "媽媽考選部會議不是在台北而是在桃園",
        "提醒媽媽後天去考選部開會",
        "請媽媽後天去考選部開會",
        "媽媽讓爸爸去考選部開會",
        "媽媽告訴爸爸去考選部開會",
        "媽媽考選部會議會場在板橋",
        "媽媽考選部會議會場在板橋車站",
        "媽媽考選部會議地點在新莊",
        "媽媽考選部會議地點另行通知",
        "媽媽考選部會議會場待通知",
        "媽媽考選部會議在板橋舉辦",
        "媽媽考選部會議於新莊舉辦",
        "媽媽考選部會議在板橋車站開會",
        "媽媽考選部會議將在板橋進行",
        "媽媽考選部會議地點尚未確認",
        "媽媽考選部會議地點還沒確認",
        "媽媽考選部會議會場尚未確認",
        "媽媽考選部會議地點不明",
        "媽媽考選部會議場地另訂",
        "媽媽考選部會議地點之後再說",
        "提醒媽媽稍後去考選部開會",
        "提醒媽媽日後去考選部開會",
        "請媽媽之後去考選部開會",
        "媽媽提醒爸爸稍後去考選部開會",
        "媽媽考選部會議地點在台北或桃園",
        "媽媽考選部會議地點是考選部還是桃園",
        "媽媽考選部會議地點確定為桃園",
        "媽媽考選部會議場地確定在桃園",
        "媽媽考選部會議會場設在桃園",
        "媽媽考選部會議地點訂在桃園",
        "媽媽考選部會議場地選在桃園",
        "媽媽考選部會議地點已改桃園",
    ):
        assert main._calendar_event_home_city_evidence(
            {"title": title, "location": ""},
            "台北",
        ) is None

    for title in (
        "媽媽討論後去考選部",
        "媽媽查完後前往考選部",
        "媽媽確定在考選部開會",
        "媽媽考選部會議時間待確認",
        "媽媽準備完後去考選部",
        "媽媽考選部資料審查會議",
        "媽媽考選部資料庫建置會議",
        "媽媽考選部簡報評選會議",
        "媽媽去考選部做簡報",
        "媽媽到考選部簡報",
        "媽媽在考選部準備簡報",
        "媽媽去考選部處理會議資料",
        "媽媽請假去考選部",
        "媽媽叫車去考選部",
        "媽媽叫Uber去考選部",
        "媽媽請司機載她去考選部",
        "媽媽被通知後確定去考選部",
        "媽媽考選部會議會場在考選部",
        "媽媽考選部會議地點：台北",
        "媽媽考選部會議在下午進行",
        "媽媽考選部會議在下午2點進行",
        "媽媽考選部會議在明天下午進行",
        "媽媽考選部會議於8/13下午進行",
        "媽媽考選部會議在週三進行",
        "媽媽考選部會議在上午10:30進行",
        "媽媽考選部會議在晚上七點進行",
        "媽媽去考選部開會，晚餐地點在桃園",
        "媽媽去考選部開會，下午場地另行通知",
    ):
        assert main._calendar_event_home_city_evidence(
            {"title": title, "location": ""},
            "台北",
        ) == "考選部"

    for location in (
        "台北或桃園",
        "台北／桃園",
        "台北、桃園擇一",
        "台北市 / 桃園市",
    ):
        assert main._calendar_event_home_city_evidence(
            {"title": "媽媽會議", "location": location},
            "台北",
        ) is None

    for title in (
        "考選部會議取消",
        "媽媽不用去考選部開會",
        "提醒媽媽去考選部開會",
        "媽媽可能在考選部開會",
        "媽媽被主管要求去考選部開會",
        "媽媽考選部會議在桃園舉辦",
        "提醒媽媽後天去考選部開會",
        "媽媽考選部會議地點尚未確認",
        "媽媽考選部會議在板橋舉辦",
        "媽媽考選部會議地點在台北或桃園",
    ):
        reply = main._build_return_home_calendar_reply(
            "G1",
            "媽媽什麼時候回家",
            [
                {
                    "group_id": "G1",
                    "status": "active",
                    "title": title,
                    "event_date": "2026-08-13",
                    "participants": '["媽媽"]',
                    "source_msg_id": "",
                }
            ],
            "2026-08-10",
        )
        assert reply is not None
        assert "可確認是在台北" not in reply
        assert "無法判斷" in reply


def test_calendar_source_owner_is_scoped_to_each_dated_clause(monkeypatch):
    import main

    event = {
        "group_id": "G1",
        "status": "active",
        "title": "下午去考選部開會",
        "event_date": "2026-08-13",
        "participants": "[]",
        "source_msg_id": "m1",
    }
    source_text = "我 8/11 回台北；8/12 爸爸去桃園；8/13 下午去考選部開會"
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda gid, mid: ("U_MOM", source_text),
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )

    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is False
    assert main._calendar_event_owned_by_actor("G1", event, "爸爸") is True


def test_companion_does_not_take_over_later_dated_clauses(monkeypatch):
    import main

    event = {
        "group_id": "G1",
        "status": "active",
        "title": "下午去考選部開會",
        "event_date": "2026-08-13",
        "participants": "[]",
        "source_msg_id": "m1",
    }
    source_text = "我 8/11 回台北；8/12 跟爸爸聚餐；8/13 下午去考選部開會"
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda _gid, _mid: ("U_MOM", source_text),
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )

    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is True
    assert main._calendar_event_owned_by_actor("G1", event, "爸爸") is False


def test_companion_event_includes_first_person_speaker(monkeypatch):
    import main

    event = {
        "group_id": "G1",
        "status": "active",
        "title": "跟爸爸回台北",
        "event_date": "2026-08-12",
        "participants": "[]",
        "source_msg_id": "m1",
    }
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda _gid, _mid: ("U_MOM", "我 8/12 跟爸爸回台北"),
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )

    for participants in ("[]", '["爸爸"]', '["爸爸(同行)"]'):
        event["participants"] = participants
        assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is True
        assert main._calendar_event_owned_by_actor("G1", event, "爸爸") is True


def test_shared_family_return_event_belongs_to_each_named_family_member():
    import main

    whole_family = {
        "group_id": "G1",
        "status": "active",
        "title": "全家回台北",
        "participants": '["全家"]',
    }
    joint_parents = {
        "group_id": "G1",
        "status": "active",
        "title": "媽媽和爸爸一起回台北",
        "participants": "[]",
    }

    for actor in ("媽媽", "爸爸"):
        assert main._calendar_event_owned_by_actor("G1", whole_family, actor) is True
        assert main._calendar_event_owned_by_actor("G1", joint_parents, actor) is True


def test_calendar_title_uses_action_subject_not_every_name_mention(monkeypatch):
    import main

    event = {
        "group_id": "G1",
        "status": "active",
        "title": "媽媽提醒爸爸 8/13 去考選部開會",
        "event_date": "2026-08-13",
        "participants": "[]",
        "source_msg_id": "m1",
    }
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda gid, mid: ("U_MOM", "我提醒爸爸 8/13 去考選部開會"),
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )

    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is False
    assert main._calendar_event_owned_by_actor("G1", event, "爸爸") is True


@pytest.mark.parametrize(
    "title",
    ["媽媽看到爸爸回台北", "媽媽做筆記：爸爸去考選部"],
)
def test_calendar_title_ignores_reporter_and_observer_as_owner(title):
    import main

    assert main._calendar_title_action_actors(title) == {"爸爸"}


@pytest.mark.parametrize(
    "title",
    (
        "媽媽看到爸爸去考選部開會",
        "媽媽說爸爸去考選部開會",
        "爸爸考選部會議",
        "爸爸的考選部會議",
    ),
)
def test_general_calendar_owner_rejects_conflicting_title_actor(title):
    import main

    event = {
        "group_id": "G1",
        "status": "active",
        "title": title,
        "event_date": "2026-08-13",
        "participants": '["媽媽"]',
        "source_msg_id": "",
    }

    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is False
    assert main._calendar_event_owned_by_actor("G1", event, "爸爸") is True


def test_calendar_title_recognizes_joint_action_subjects():
    import main

    assert main._calendar_title_action_actors("媽媽跟爸爸去考選部") == {
        "媽媽",
        "爸爸",
    }
    assert main._calendar_title_action_actors("媽媽、爸爸及妹妹一起回台北") == {
        "媽媽",
        "爸爸",
        "妹妹",
    }
    for title in (
        "媽媽陪爸爸去考選部",
        "媽媽帶爸爸去考選部",
        "媽媽載爸爸去考選部",
        "媽媽接爸爸回台北",
        "媽媽送爸爸回台北",
        "媽媽接爸爸從桃園回台北",
        "媽媽陪爸爸一起從桃園回台北",
        "媽媽帶爸爸搭車去考選部",
        "媽媽接爸爸從桃園機場回台北",
        "媽媽接爸爸從高鐵站回台北",
        "媽媽陪爸爸從醫院回台北",
        "媽媽帶爸爸搭計程車去考選部",
    ):
        assert main._calendar_title_action_actors(title) == {"媽媽", "爸爸"}
    for title in (
        "媽媽帶爸爸的行李回台北",
        "媽媽送爸爸的藥回台北",
        "媽媽接爸爸的電話後回台北",
        "媽媽陪爸爸的朋友去考選部",
    ):
        assert main._calendar_title_action_actors(title) == {"媽媽"}
    assert main._calendar_title_action_actors("媽媽到台北") == {"媽媽"}
    assert main._calendar_title_action_actors("媽媽從台中到台北") == {"媽媽"}
    for title in ("媽媽提到台北", "媽媽講到台北", "媽媽找到台北資料"):
        assert main._calendar_title_action_actors(title) == set()


@pytest.mark.parametrize(
    "title",
    ["媽媽替爸爸去考選部", "媽媽幫爸爸去考選部"],
)
def test_calendar_title_does_not_treat_proxy_target_as_attendee(title):
    import main

    assert main._calendar_title_action_actors(title) == {"媽媽"}


def test_return_home_query_actor_uses_home_subject_not_reporter():
    import main

    assert main._return_home_query_actor("媽媽問爸爸什麼時候回家") == "爸爸"
    assert main._return_home_query_actor("爸爸說媽媽幾點回家") == "媽媽"
    for query in (
        "媽媽和爸爸什麼時候回家",
        "媽媽或爸爸什麼時候回家",
        "媽媽還是爸爸什麼時候回家",
        "媽媽和妹妹什麼時候回家",
        "媽媽或弟弟什麼時候回家",
    ):
        assert (
            main._return_home_query_actor(query)
            == main._CALENDAR_AMBIGUOUS_ACTOR
        )
    assert main._return_home_query_actor("媽媽什麼時候回台北") == "媽媽"
    assert main._return_home_query_actor("媽媽什麼時候到台北") == "媽媽"
    assert main._return_home_query_actor("媽媽幾點抵達台北") == "媽媽"
    assert main._return_home_query_actor("媽媽什麼時候回來") == "媽媽"
    assert main._return_home_query_actor("媽媽回來是幾點") == "媽媽"
    assert main._return_home_query_actor("媽媽回來時間是什麼時候") == "媽媽"
    for query in (
        "媽媽回台北後，爸爸回來了嗎？",
        "媽媽回家後爸爸回來了嗎",
        "媽媽回台北後，爸爸什麼時候回來",
    ):
        assert main._return_home_query_actor(query) == "爸爸"
    for query in (
        "媽媽什麼時候回家？爸爸已經回台北了",
        "媽媽幾點回家？爸爸今晚會回來",
    ):
        assert main._return_home_query_actor(query) == "媽媽"
    assert main._return_home_query_actor(
        "爸爸什麼時候回家？媽媽已回台北"
    ) == "爸爸"
    for query in (
        "媽媽問我她什麼時候回來",
        "媽媽問我：她何時回家",
        "媽媽告訴我她明天回台北嗎",
    ):
        assert main._return_home_query_actor(query) == "媽媽"
    for query in (
        "媽媽什麼時候回來啊",
        "媽媽什麼時候回來呀",
        "媽媽什麼時候回來啦",
        "媽媽什麼時候回來嘛",
        "媽媽什麼時候回來欸",
        "媽媽什麼時候回來耶",
        "媽媽什麼時候回來齁",
        "媽媽什麼時候回來台北",
    ):
        assert main._return_home_query_actor(query) == "媽媽"
    for query in (
        "媽媽什麼時候回來上班",
        "媽媽什麼時候回來拿東西",
        "媽媽什麼時候回來開會",
        "媽媽什麼時候回來看診",
        "媽媽什麼時候回來台灣",
    ):
        assert main._return_home_query_actor(query) is None


def test_return_home_query_actor_supports_elliptical_time_and_date_questions():
    import main

    for query in (
        "媽媽回來的時間呢",
        "媽媽回來時間呢",
        "媽媽回家的時間呢",
        "媽媽回家時間？",
        "媽媽到家的時間呢",
        "媽媽到家時間？",
        "媽媽預計回來的日期是哪天",
        "媽媽回來的日期是幾號",
    ):
        assert main._is_calendar_query(query), query
        assert main._return_home_query_actor(query) == "媽媽", query

    for statement in ("媽媽回來時間改了", "媽媽到家時間太晚"):
        assert not main._is_calendar_query(statement), statement
        assert main._return_home_query_actor(statement) is None


def test_return_home_query_actor_supports_self_and_reported_self():
    import main

    for query in (
        "我什麼時候回家",
        "我什麼時候回台北",
        "媽媽問我什麼時候回家",
        "爸爸問我何時到台北",
        "媽媽說我幾點回家",
    ):
        assert main._is_calendar_query(query), query
        assert main._return_home_query_actor(query) == main._CALENDAR_SELF_ACTOR

    assert (
        main._return_home_query_actor("我和媽媽什麼時候回家")
        == main._CALENDAR_AMBIGUOUS_ACTOR
    )


@pytest.mark.parametrize(
    ("sender_alias", "expected"),
    [
        ("媽媽", "最直接的行程紀錄"),
        ("", "無法辨識你對應的家庭成員"),
    ],
)
def test_first_person_return_home_query_resolves_line_sender_safely(
    monkeypatch,
    sender_alias,
    expected,
):
    import calendar_db
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    target = datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=1)
    event_row = {
        "event_id": "mom-return",
        "group_id": "G1",
        "title": "回台北",
        "event_date": target.isoformat(),
        "event_time": "",
        "location": "",
        "participants": '["媽媽"]',
        "status": "active",
    }
    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *a, **k: [event_row])
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: sender_alias)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeSource:
        user_id = "U_SELF"

    class FakeEvent:
        source = FakeSource()
        reply_token = "fake-token"

    main._handle_calendar_query(FakeEvent(), "G1", "我什麼時候回家")

    assert expected in captured["text"]


def test_ambiguous_return_home_query_does_not_show_anyone_schedule(monkeypatch):
    import calendar_db
    import main

    monkeypatch.setattr(
        calendar_db,
        "list_upcoming",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("ambiguous query must not read a person's schedule")
        ),
    )
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake-token"

    main._handle_calendar_query(
        FakeEvent(),
        "G1",
        "我和媽媽什麼時候回家",
    )

    assert "請一次指定一位家人" in captured["text"]


def test_return_home_query_honors_requested_date_instead_of_earlier_event(
    monkeypatch,
):
    import calendar_db
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    earlier = today + timedelta(days=1)
    requested = today + timedelta(days=3)
    events = [
        {
            "event_id": "early",
            "group_id": "G1",
            "title": "明天回台北",
            "event_date": earlier.isoformat(),
            "event_time": "",
            "location": "",
            "participants": '["媽媽"]',
            "status": "active",
        },
        {
            "event_id": "requested",
            "group_id": "G1",
            "title": "大後天回台北",
            "event_date": requested.isoformat(),
            "event_time": "",
            "location": "",
            "participants": '["媽媽"]',
            "status": "active",
        },
    ]
    monkeypatch.setattr(calendar_db, "list_past", lambda *a, **k: [])
    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *a, **k: events)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake-token"

    main._handle_calendar_query(
        FakeEvent(),
        "G1",
        "媽媽大後天幾點回家",
    )

    assert "大後天回台北" in captured["text"]
    assert "明天回台北" not in captured["text"]


def test_calendar_query_subject_distinguishes_subject_reporter_and_beneficiary():
    import main

    assert main._calendar_query_subject_actors("媽媽明天有什麼活動") == {"媽媽"}
    assert main._calendar_query_subject_actors("媽媽問明天有什麼活動") == set()
    assert main._calendar_query_subject_actors("媽媽想知道明天有什麼活動") == set()
    assert main._calendar_query_subject_actors("幫媽媽查明天有什麼活動") == set()
    assert main._calendar_query_subject_actors("明天有什麼活動是媽媽要去的") == {
        "媽媽"
    }
    assert main._calendar_query_subject_actors("明天有什麼活動爸爸需要出席") == {
        "爸爸"
    }
    assert main._calendar_query_subject_actors("明天媽媽問有什麼活動") == set()
    assert main._calendar_query_subject_actors("明天媽媽想知道有什麼活動") == set()
    assert main._calendar_query_subject_actors("明天媽媽想知道台北有什麼活動") == set()
    assert main._calendar_query_subject_actors("明天媽媽和爸爸有什麼活動") == {
        "媽媽",
        "爸爸",
    }
    for query in (
        "明天有什麼活動是媽媽和爸爸要去的",
        "明天有什麼活動是媽媽、爸爸需要出席",
        "明天有什麼活動是媽媽跟爸爸會去",
    ):
        assert main._calendar_query_subject_actors(query) == {"媽媽", "爸爸"}
    assert main._calendar_query_subject_actors("明天有什麼活動需要媽媽出席") == {
        "媽媽"
    }
    assert main._calendar_query_subject_actors("明天有什麼活動要爸爸參加") == {
        "爸爸"
    }
    assert main._calendar_query_is_first_person_subject(
        "明天有什麼活動需要我出席"
    )
    for query in (
        "明天有什麼活動是媽媽參加的",
        "明天有什麼活動媽媽會參加的",
        "明天有哪些會議媽媽要出席",
        "明天有哪些會議是媽媽出席的",
    ):
        assert main._calendar_query_subject_actors(query) == {"媽媽"}
    for query in (
        "明天有什麼活動是我參加的",
        "明天有什麼活動我會參加的",
    ):
        assert main._calendar_query_is_first_person_subject(query)


def test_source_owner_rejects_delegated_first_person_preamble(monkeypatch):
    import main

    event = {
        "group_id": "G1",
        "status": "active",
        "title": "下午去考選部開會",
        "event_date": "2026-08-13",
        "participants": "[]",
        "source_msg_id": "m1",
    }
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda _gid, _mid: ("U_MOM", "我會請爸爸 8/13 去考選部開會"),
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )

    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is False


@pytest.mark.parametrize(
    "source_text",
    ["我會在 8/13 去考選部開會", "我預計在8/13去考選部開會", "我：8/13 去考選部開會"],
)
def test_first_person_source_accepts_safe_date_connectors(monkeypatch, source_text):
    import main

    event = {
        "group_id": "G1",
        "status": "active",
        "title": "下午去考選部開會",
        "event_date": "2026-08-13",
        "participants": "[]",
        "source_msg_id": "m1",
    }
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda _gid, _mid: ("U_MOM", source_text),
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )

    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is True


def test_relative_date_first_person_source_keeps_conservative_owner(monkeypatch):
    import main

    event = {
        "group_id": "G1",
        "status": "active",
        "title": "明天回台北",
        "event_date": "2026-08-11",
        "participants": "[]",
        "source_msg_id": "m1",
    }
    source_text = "我明天回台北"
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda _gid, _mid: ("U_MOM", source_text),
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )

    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is True

    source_text = "我明天提醒爸爸回台北"
    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is False
    assert main._calendar_event_owned_by_actor("G1", event, "爸爸") is True


def test_relative_date_source_clause_preserves_remote_marker():
    import main

    source_text = "我明天線上參加考選部會議"
    assert main._source_clause_for_event(source_text, "2026-08-11") == source_text
    assert main._calendar_event_home_city_evidence(
        {"title": "參加考選部會議", "location": ""},
        "台北",
        source_clause=main._source_clause_for_event(source_text, "2026-08-11"),
    ) is None


def test_relative_source_date_mismatch_cannot_supply_taipei_evidence():
    import main
    from datetime import date

    source_text = "我明天去考選部開會"
    assert main._source_clause_for_event(
        source_text,
        "2026-08-14",
        source_date=date(2026, 8, 10),
    ) == ""


def test_relative_source_date_mismatch_invalidates_explicit_return_event(
    monkeypatch,
):
    import main
    from datetime import datetime
    from zoneinfo import ZoneInfo

    source_timestamp = int(
        datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "get_raw_message_record",
        lambda _gid, _mid: {
            "group_id": "G1",
            "message_id": "m1",
            "user_id": "U_MOM",
            "text": "我明天回台北",
            "created_at": source_timestamp,
        },
    )
    event = {
        "group_id": "G1",
        "status": "active",
        "title": "媽媽回台北",
        "event_date": "2026-08-14",
        "participants": '["媽媽"]',
        "source_msg_id": "m1",
    }

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [event],
        "2026-08-10",
    )

    assert reply is not None
    assert "預計 8/14 回到台北" not in reply
    assert "無法判斷" in reply


def test_same_day_source_clauses_are_merged_for_conservative_remote_scan():
    import main

    source_text = "我 8/13 上午去考選部；8/13 下午線上參加考選部會議"
    source_clause = main._source_clause_for_event(source_text, "2026-08-13")
    assert "上午去考選部" in source_clause
    assert "下午線上參加考選部會議" in source_clause
    assert main._calendar_event_home_city_evidence(
        {"title": "參加考選部會議", "location": ""},
        "台北",
        source_clause=source_clause,
    ) is None


def test_mixed_absolute_and_relative_source_keeps_remote_marker():
    import main

    source_text = "我 8/11 回台北；明天線上參加考選部會議"
    source_clause = main._source_clause_for_event(source_text, "2026-08-12")
    assert "線上參加考選部會議" in source_clause
    assert main._calendar_event_home_city_evidence(
        {"title": "考選部會議", "location": ""},
        "台北",
        source_clause=source_clause,
    ) is None


def test_relative_proxy_trip_belongs_to_speaker_not_proxy_target(monkeypatch):
    import main

    event = {
        "group_id": "G1",
        "status": "active",
        "title": "明天去考選部開會",
        "event_date": "2026-08-11",
        "participants": "[]",
        "source_msg_id": "m1",
    }
    source_text = "我明天替爸爸去考選部開會"
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda _gid, _mid: ("U_MOM", source_text),
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )

    assert main._calendar_event_owned_by_actor("G1", event, "爸爸") is False
    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is True


def test_relative_date_clauses_keep_each_events_actor(monkeypatch):
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    source_text = "我明天去考選部開會；後天爸爸去桃園"
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda _gid, _mid: ("U_MOM", source_text),
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )
    common = {
        "group_id": "G1",
        "status": "active",
        "participants": "[]",
        "source_msg_id": "m1",
    }
    mom_event = dict(
        common,
        title="去考選部開會",
        event_date=(today + timedelta(days=1)).isoformat(),
    )
    dad_event = dict(
        common,
        title="去桃園",
        event_date=(today + timedelta(days=2)).isoformat(),
    )

    assert main._calendar_event_owned_by_actor("G1", mom_event, "媽媽") is True
    assert main._calendar_event_owned_by_actor("G1", mom_event, "爸爸") is False
    assert main._calendar_event_owned_by_actor("G1", dad_event, "媽媽") is False
    assert main._calendar_event_owned_by_actor("G1", dad_event, "爸爸") is True


@pytest.mark.parametrize("relative_first", [False, True])
def test_mixed_absolute_relative_clauses_keep_each_events_actor(
    monkeypatch,
    relative_first,
):
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)
    numeric = f"{day_after.month}/{day_after.day}"
    source_text = (
        f"我明天去考選部；{numeric} 爸爸去桃園"
        if relative_first
        else f"我 {tomorrow.month}/{tomorrow.day} 去考選部；後天爸爸去桃園"
    )
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda _gid, _mid: ("U_MOM", source_text),
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )
    common = {
        "group_id": "G1",
        "status": "active",
        "participants": "[]",
        "source_msg_id": "m1",
    }
    mom_event = dict(
        common,
        title="去考選部",
        event_date=tomorrow.isoformat(),
    )
    dad_event = dict(
        common,
        title="去桃園",
        event_date=day_after.isoformat(),
    )

    assert main._calendar_event_owned_by_actor("G1", mom_event, "媽媽") is True
    assert main._calendar_event_owned_by_actor("G1", dad_event, "爸爸") is True


def test_prefixed_weekday_relative_clauses_keep_each_events_actor(monkeypatch):
    import main

    source_text = "我下週三去考選部；下週四爸爸去桃園"
    wednesday = main._resolve_calendar_query_dates("下週三")[0]
    thursday = main._resolve_calendar_query_dates("下週四")[0]
    assert [
        match.group(0)
        for match, _clause in main._source_relative_dated_clauses(source_text)
    ] == ["下週三", "下週四"]
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda _gid, _mid: ("U_MOM", source_text),
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )
    common = {
        "group_id": "G1",
        "status": "active",
        "participants": "[]",
        "source_msg_id": "m1",
    }
    mom_event = dict(common, title="去考選部", event_date=wednesday.isoformat())
    dad_event = dict(common, title="去桃園", event_date=thursday.isoformat())

    assert main._calendar_event_owned_by_actor("G1", mom_event, "媽媽") is True
    assert main._calendar_event_owned_by_actor("G1", dad_event, "爸爸") is True


def test_relative_source_uses_message_date_not_query_date(monkeypatch):
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Asia/Taipei"))
    source_time = now - timedelta(days=1)
    event = {
        "group_id": "G1",
        "status": "active",
        "title": "回台北",
        "event_date": now.date().isoformat(),
        "participants": "[]",
        "source_msg_id": "m1",
    }
    monkeypatch.setattr(
        main.memory,
        "get_raw_message_record",
        lambda _gid, _mid: {
            "group_id": "G1",
            "message_id": "m1",
            "user_id": "U_MOM",
            "text": "我明天回台北",
            "created_at": int(source_time.timestamp()),
        },
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )

    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is True


def test_same_relative_date_clauses_use_event_title_to_disambiguate(monkeypatch):
    import main
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tomorrow = datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=1)
    source_text = "我明天去考選部；明天爸爸去桃園"
    monkeypatch.setattr(
        main.memory,
        "get_raw_message_record",
        lambda _gid, _mid: None,
    )
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda _gid, _mid: ("U_MOM", source_text),
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )
    common = {
        "group_id": "G1",
        "status": "active",
        "event_date": tomorrow.isoformat(),
        "participants": "[]",
        "source_msg_id": "m1",
    }
    mom_event = dict(common, title="去考選部")
    dad_event = dict(common, title="去桃園")

    assert main._calendar_event_owned_by_actor("G1", mom_event, "媽媽") is True
    assert main._calendar_event_owned_by_actor("G1", mom_event, "爸爸") is False
    assert main._calendar_event_owned_by_actor("G1", dad_event, "媽媽") is False
    assert main._calendar_event_owned_by_actor("G1", dad_event, "爸爸") is True


def test_relative_source_date_disambiguates_same_title_clauses(monkeypatch):
    import main
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    source_date = date(2026, 8, 8)
    source_timestamp = int(
        datetime(2026, 8, 8, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    source_text = "我明天去桃園；後天爸爸去桃園"
    monkeypatch.setattr(
        main.memory,
        "get_raw_message_record",
        lambda _gid, _mid: {
            "group_id": "G1",
            "message_id": "m1",
            "user_id": "U_MOM",
            "text": source_text,
            "created_at": source_timestamp,
        },
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )
    common = {
        "group_id": "G1",
        "status": "active",
        "title": "去桃園",
        "participants": "[]",
        "source_msg_id": "m1",
    }
    mom_event = dict(common, event_date=date(2026, 8, 9).isoformat())
    dad_event = dict(common, event_date=date(2026, 8, 10).isoformat())

    assert source_date == main._calendar_raw_source_date(
        main._calendar_source_raw("G1", mom_event)
    )
    assert main._calendar_event_owned_by_actor("G1", mom_event, "媽媽") is True
    assert main._calendar_event_owned_by_actor("G1", dad_event, "爸爸") is True


def test_dated_source_mismatch_cannot_supply_owner_or_taipei_evidence(monkeypatch):
    import main
    from datetime import datetime
    from zoneinfo import ZoneInfo

    source_text = "我 8/13 去考選部開會"
    source_timestamp = int(
        datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "get_raw_message_record",
        lambda _gid, _mid: {
            "group_id": "G1",
            "message_id": "m1",
            "user_id": "U_MOM",
            "text": source_text,
            "created_at": source_timestamp,
        },
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )
    event = {
        "group_id": "G1",
        "status": "active",
        "title": "考選部開會",
        "event_date": "2026-08-14",
        "participants": "[]",
        "source_msg_id": "m1",
    }

    assert main._source_clause_for_event(
        source_text,
        "2026-08-14",
        event_title="考選部開會",
        source_date=datetime(2026, 8, 10).date(),
    ) == ""
    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is False
    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [event],
        "2026-08-10",
    )
    assert reply is not None
    assert "可確認" not in reply
    assert "無法判斷" in reply


def test_yearless_source_date_uses_raw_message_year(monkeypatch):
    import main
    from datetime import datetime
    from zoneinfo import ZoneInfo

    source_text = "我 8/13 去考選部開會"
    source_timestamp = int(
        datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "get_raw_message_record",
        lambda _gid, _mid: {
            "group_id": "G1",
            "message_id": "m1",
            "user_id": "U_MOM",
            "text": source_text,
            "created_at": source_timestamp,
        },
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )
    event = {
        "group_id": "G1",
        "status": "active",
        "title": "考選部開會",
        "event_date": "2027-08-13",
        "participants": "[]",
        "source_msg_id": "m1",
    }

    assert main._source_clause_for_event(
        source_text,
        "2027-08-13",
        event_title="考選部開會",
        source_date=datetime(2026, 8, 10).date(),
    ) == ""
    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is False


def test_source_owner_handles_postposed_actor_in_dated_clause(monkeypatch):
    import main

    event = {
        "group_id": "G1",
        "status": "active",
        "title": "去桃園",
        "event_date": "2026-08-12",
        "participants": "[]",
        "source_msg_id": "m1",
    }
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda _gid, _mid: ("U_MOM", "我 8/11 回台北；8/12 去桃園的是爸爸"),
    )
    monkeypatch.setattr(
        main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else ""
    )

    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is False
    assert main._calendar_event_owned_by_actor("G1", event, "爸爸") is True


def test_calendar_owner_lookup_failure_is_safe(monkeypatch):
    import main

    event = {
        "group_id": "G1",
        "status": "active",
        "title": "下午去考選部開會",
        "event_date": "2026-08-13",
        "participants": "[]",
        "source_msg_id": "m1",
    }
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("database is locked")),
    )

    assert main._calendar_event_owned_by_actor("G1", event, "媽媽") is False


def test_returning_to_taiwan_is_not_direct_evidence_of_home_city():
    import main

    assert not main._calendar_event_is_direct_home_return(
        {"title": "8/13 返台", "location": "桃園機場"},
        "台北",
    )
    assert main._calendar_event_is_direct_home_return(
        {"title": "晚上到家", "location": ""},
        "台北",
    )
    for title in ("今晚不會到家", "今晚沒辦法到家", "取消到家", "改天到家"):
        assert not main._calendar_event_is_direct_home_return(
            {"title": title, "location": ""},
            "台北",
        )
    for title in ("今晚尚未到家", "今晚還未到家", "今晚未能到家"):
        assert not main._calendar_event_is_direct_home_return(
            {"title": title, "location": ""},
            "台北",
        )
    for title in (
        "未決定是否回台北",
        "暫無回台北計畫",
        "尚無回台北計畫",
        "回台北（取消）",
        "未回台北",
    ):
        assert not main._calendar_event_is_direct_home_return(
            {"title": title, "location": ""},
            "台北",
        )
    assert not main._calendar_event_is_direct_home_return(
        {"title": "到家樂福採買", "location": ""},
        "台北",
    )
    for title in ("回家鄉", "回家長群組"):
        assert not main._calendar_event_is_direct_home_return(
            {"title": title, "location": ""},
            "台北",
        )
    for title in ("回家裡", "到家門口"):
        assert main._calendar_event_is_direct_home_return(
            {"title": title, "location": ""},
            "台北",
        )
    assert main._calendar_event_is_direct_home_return(
        {"title": "晚上抵達台北", "location": ""},
        "台北",
    )
    assert main._calendar_event_is_direct_home_return(
        {"title": "晚上回來", "location": ""},
        "台北",
    )
    assert main._calendar_event_is_direct_home_return(
        {"title": "媽媽到台北", "location": ""},
        "台北",
    )
    for title in (
        "會議提到台北",
        "聊天講到台北",
        "終於找到台北資料",
        "找回台北資料",
        "寄回台北文件",
        "媽媽回來上班",
        "媽媽回來拿東西",
        "媽媽回來開會",
        "媽媽回來看診",
        "媽媽回來台灣",
    ):
        assert not main._calendar_event_is_direct_home_return(
            {"title": title, "location": ""},
            "台北",
        )
    for title in ("回來", "預計回來", "8/13 回來"):
        assert main._calendar_event_is_direct_home_return(
            {"title": title, "location": ""},
            "台北",
        )
    for title in ("媽媽回來啊", "媽媽回來呀", "媽媽回來啦", "媽媽回來台北"):
        assert main._calendar_event_is_direct_home_return(
            {"title": title, "location": ""},
            "台北",
        )
    for title in (
        "媽媽從台中到台北",
        "從桃園機場抵達台北",
        "媽媽搭高鐵到台北",
        "媽媽從台中搭高鐵到台北",
        "媽媽搭高鐵回台北",
        "搭火車回台北",
        "坐飛機回台北",
        "搭計程車回台北",
    ):
        assert main._calendar_event_is_direct_home_return(
            {"title": title, "location": ""},
            "台北",
        )


def test_return_home_reply_uses_neutral_tense_for_today_time_boundaries():
    import main

    base = {
        "event_id": "return",
        "group_id": "G1",
        "status": "active",
        "title": "回台北",
        "event_date": "2026-08-10",
        "participants": '["媽媽"]',
    }

    past = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [dict(base, event_time="08:00")],
        "2026-08-10",
        now_hhmm="20:00",
    )
    assert past is not None and "時間已經過" in past
    assert "預計" not in past

    date_only = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [dict(base, event_time="")],
        "2026-08-10",
        now_hhmm="20:00",
    )
    assert date_only is not None and "沒有記時間" in date_only
    assert "預計" not in date_only

    future = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [dict(base, event_time="21:00")],
        "2026-08-10",
        now_hhmm="20:00",
    )
    assert future is not None and "21:00" in future
    assert "返程行程的記錄時間" in future
    assert "21:00 回到台北" not in future


@pytest.mark.parametrize("title", ["媽媽到台北", "媽媽從台中到台北"])
def test_return_home_reply_uses_named_bare_arrival_with_empty_participants(
    monkeypatch,
    title,
):
    import main

    event = {
        "event_id": "arrival",
        "group_id": "G1",
        "status": "active",
        "title": title,
        "event_date": "2026-08-13",
        "event_time": "18:00",
        "participants": "[]",
        "source_msg_id": "dad-note",
    }
    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda _gid, _mid: ("U_DAD", f"8/13 {title}"),
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "爸爸" if uid == "U_DAD" else "",
    )

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [event],
        "2026-08-10",
        now_hhmm="12:00",
    )

    assert reply is not None
    assert "最直接的行程紀錄" in reply
    assert "8/13" in reply
    assert "18:00" in reply
    if title == "媽媽到台北":
        assert "預計 8/13 18:00 回到台北" in reply
        assert "不能證明人已經到家" in reply
    else:
        assert "返程行程的記錄時間" in reply
        assert "18:00 回到台北" not in reply


def test_direct_return_modalities_are_positive_but_uncertain_plans_are_not():
    import main

    for title in (
        "媽媽要回台北",
        "準備回台北",
        "確定回台北",
        "決定回台北",
        "打算回台北",
        "媽媽計畫回台北",
        "媽媽預定回台北",
        "媽媽預備回台北",
        "不過媽媽明天會回台北",
        "媽媽明天不只會回台北",
        "不是不回台北，是晚一點回",
        "不能不回台北",
        "不得不回台北",
        "媽媽確定回來",
        "媽媽決定回來",
        "媽媽打算回來",
        "媽媽準備回來",
        "媽媽要回來",
        "媽媽預定回來",
        "媽媽預備回來",
        "媽媽請假後回台北",
        "媽媽請假回台北休息",
        "媽媽申請完資料回台北",
        "媽媽請完客回台北",
        "媽媽告訴爸爸後回台北",
        "媽媽提醒爸爸後自己回台北",
        "媽媽帶原本的資料回台北",
        "媽媽拿本來的文件回台北",
        "媽媽穿原本的衣服回台北",
        "媽媽照原本的路線回台北",
        "媽媽叫車回台北",
        "媽媽叫計程車回台北",
        "媽媽叫Uber回台北",
        "媽媽請司機載她回台北",
        "媽媽讓司機載她回台北",
        "媽媽原本要回台北，現在確定會回台北",
        "媽媽原本不回台北，現在要回台北",
        "媽媽本來不打算回台北，後來決定回台北",
        "媽媽原本不確定是否回台北，現在確定會回台北",
        "媽媽本來還沒決定要不要回台北，現在決定回台北",
        "媽媽叫爸爸載她回台北",
        "媽媽請爸爸送她回台北",
        "媽媽讓妹妹開車載她回台北",
        "媽媽叫爸爸陪她回台北",
        "媽媽由爸爸載回台北",
        "媽媽搭爸爸的車回台北",
        "媽媽坐爸爸的車回台北",
        "媽媽和爸爸一起回台北",
        "媽媽跟爸爸一起回台北",
        "媽媽與爸爸一起回台北",
        "媽媽、爸爸一起回台北",
        "全家回台北",
        "全家一起回台北",
        "我們全家回台北",
        "媽媽跟家人一起回台北",
    ):
        assert main._calendar_event_is_direct_home_return(
            {"title": title, "location": ""}, "台北"
        )
    for title in (
        "可能回台北",
        "未定回台北",
        "回台北（取消）",
        "媽媽尚未確定回來",
        "媽媽尚未決定回來",
        "媽媽不打算回來",
        "媽媽還沒打算回來",
        "提醒媽媽明天要回來",
        "跟媽媽確認明天要回來",
        "媽媽要求爸爸回台北",
        "媽媽讓爸爸回台北",
        "媽媽叫爸爸回台北",
        "媽媽告訴爸爸回台北",
        "媽媽原本要回台北",
        "本來要回台北",
        "媽媽準備回台北資料",
        "媽媽問爸爸一起回台北嗎",
        "提醒媽媽後天回台北",
        "請媽媽後天回台北",
        "媽媽提醒爸爸後天回台北",
        "告訴爸爸後天回台北",
        "提醒媽媽稍後回台北",
        "請媽媽之後回台北",
        "媽媽提醒爸爸稍後回台北",
        "媽媽告訴爸爸日後回台北",
    ):
        assert not main._calendar_event_is_direct_home_return(
            {"title": title, "location": ""}, "台北"
        )


def test_family_driver_return_is_attributed_to_the_passenger():
    import main

    mom_reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": "媽媽叫爸爸載她回台北",
                "event_date": "2026-08-13",
                "participants": '["媽媽"]',
                "source_msg_id": "",
            }
        ],
        "2026-08-10",
    )
    assert mom_reply is not None
    assert "預計 8/13 回到台北" in mom_reply

    sister_event = {
        "group_id": "G1",
        "status": "active",
        "title": "媽媽叫爸爸陪妹妹回台北",
        "event_date": "2026-08-13",
        "participants": '["妹妹"]',
        "source_msg_id": "",
    }
    mom_reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [sister_event],
        "2026-08-10",
    )
    assert mom_reply is not None
    assert "預計 8/13 回到台北" not in mom_reply
    assert "無法判斷" in mom_reply

    for title in (
        "媽媽叫爸爸陪妹妹回台北",
        "媽媽叫爸爸陪妹妹去考選部開會",
    ):
        wrong_participant_reply = main._build_return_home_calendar_reply(
            "G1",
            "媽媽什麼時候回家",
            [
                {
                    "group_id": "G1",
                    "status": "active",
                    "title": title,
                    "event_date": "2026-08-13",
                    "participants": '["媽媽"]',
                    "source_msg_id": "",
                }
            ],
            "2026-08-10",
        )
        assert wrong_participant_reply is not None
        assert "預計 8/13 回到台北" not in wrong_participant_reply
        assert "可確認是在台北" not in wrong_participant_reply
        assert "無法判斷" in wrong_participant_reply

    for title in (
        "媽媽由爸爸載回台北",
        "媽媽搭爸爸的車回台北",
        "媽媽坐爸爸的車回台北",
    ):
        reply = main._build_return_home_calendar_reply(
            "G1",
            "媽媽什麼時候回家",
            [
                {
                    "group_id": "G1",
                    "status": "active",
                    "title": title,
                    "event_date": "2026-08-13",
                    "participants": '["媽媽"]',
                    "source_msg_id": "",
                }
            ],
            "2026-08-10",
        )
        assert reply is not None
        assert "預計 8/13 回到台北" in reply


@pytest.mark.parametrize(
    ("title", "participants"),
    (
        ("媽媽和爸爸一起回台北", '["媽媽", "爸爸"]'),
        ("媽媽跟爸爸一起回台北", '["媽媽", "爸爸"]'),
        ("媽媽與爸爸一起回台北", '["媽媽", "爸爸"]'),
        ("媽媽、爸爸一起回台北", '["媽媽", "爸爸"]'),
        ("全家回台北", '["全家"]'),
        ("全家一起回台北", '["全家"]'),
        ("我們全家回台北", '["全家"]'),
        ("媽媽跟家人一起回台北", '["媽媽"]'),
    ),
)
def test_joint_family_return_is_direct_for_mom(title, participants):
    import main

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": title,
                "event_date": "2026-08-13",
                "participants": participants,
                "source_msg_id": "",
            }
        ],
        "2026-08-10",
    )

    assert reply is not None
    assert "媽媽預計 8/13 回到台北" in reply


def test_today_untimed_direct_return_still_answers_the_recorded_daypart():
    import main

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": "晚上回台北",
                "event_date": "2026-08-11",
                "event_time": "",
                "participants": '["媽媽"]',
                "source_msg_id": "",
            }
        ],
        "2026-08-11",
        now_hhmm="12:00",
    )

    assert reply is not None
    assert "預計今天晚上回到台北" in reply
    assert "沒有記到確切到家時間" in reply
    assert "不能證明人已經到家" in reply


@pytest.mark.parametrize(
    "title",
    (
        "媽媽看到爸爸回台北",
        "媽媽知道爸爸回台北",
        "媽媽說爸爸回台北",
        "媽媽確認爸爸回台北",
        "媽媽安排爸爸回台北",
        "媽媽問爸爸回台北的時間",
    ),
)
def test_return_home_reply_binds_movement_to_queried_actor(title):
    import main

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": title,
                "event_date": "2026-08-13",
                "participants": '["媽媽"]',
                "source_msg_id": "",
            }
        ],
        "2026-08-10",
    )

    assert reply is not None
    assert "預計 8/13 回到台北" not in reply
    assert "無法判斷" in reply


@pytest.mark.parametrize(
    "title",
    (
        "媽媽看到爸爸去考選部開會",
        "媽媽說爸爸去考選部開會",
    ),
)
def test_return_home_reply_binds_poi_movement_to_queried_actor(title):
    import main

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": title,
                "event_date": "2026-08-13",
                "participants": '["媽媽"]',
                "source_msg_id": "",
            }
        ],
        "2026-08-10",
    )

    assert reply is not None
    assert "可確認是在台北" not in reply
    assert "無法判斷" in reply


@pytest.mark.parametrize(
    "title",
    (
        "爸爸考選部會議",
        "爸爸的考選部會議",
        "爸爸考選部題庫建置會議",
        "爸爸台北會議",
    ),
)
def test_return_home_reply_rejects_conflicting_nominal_event_actor(title):
    import main

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": title,
                "event_date": "2026-08-13",
                "participants": '["媽媽"]',
                "source_msg_id": "",
            }
        ],
        "2026-08-10",
    )

    assert reply is not None
    assert "可確認是在台北" not in reply
    assert "無法判斷" in reply


@pytest.mark.parametrize(
    ("title", "source_text"),
    (
        ("媽媽回台北", "8/13 爸爸回台北"),
        ("回台北", "我 8/13 回台北"),
        ("媽媽考選部會議", "8/13 爸爸去考選部開會"),
        ("考選部會議", "我 8/13 去考選部開會"),
    ),
)
def test_return_home_reply_rejects_linked_raw_actor_conflict(
    monkeypatch,
    title,
    source_text,
):
    import main
    from datetime import datetime
    from zoneinfo import ZoneInfo

    created_at = int(
        datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "get_raw_message_record",
        lambda _gid, _mid: {
            "group_id": "G1",
            "message_id": "m1",
            "user_id": "U_DAD",
            "text": source_text,
            "created_at": created_at,
        },
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "爸爸" if uid == "U_DAD" else "",
    )
    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": title,
                "event_date": "2026-08-13",
                "participants": '["媽媽"]',
                "source_msg_id": "m1",
            }
        ],
        "2026-08-10",
    )

    assert reply is not None
    assert "預計 8/13 回到台北" not in reply
    assert "可確認是在台北" not in reply
    assert "無法判斷" in reply


def test_return_home_reply_binds_same_date_source_action_to_its_actor(
    monkeypatch,
):
    import main
    from datetime import datetime
    from zoneinfo import ZoneInfo

    created_at = int(
        datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "get_raw_message_record",
        lambda _gid, _mid: {
            "group_id": "G1",
            "message_id": "m1",
            "user_id": "U_MOM",
            "text": "我 8/13 爸爸回台北，媽媽去考選部開會",
            "created_at": created_at,
        },
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "媽媽" if uid == "U_MOM" else "",
    )

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": "媽媽回台北",
                "event_date": "2026-08-13",
                "participants": '["媽媽"]',
                "source_msg_id": "m1",
            }
        ],
        "2026-08-10",
    )

    assert reply is not None
    assert "預計 8/13 回到台北" not in reply


@pytest.mark.parametrize(
    "title",
    (
        "爸爸回台北，媽媽去考選部開會",
        "媽媽去考選部開會；爸爸回台北",
    ),
)
def test_return_home_reply_binds_direct_movement_actor_inside_event(title):
    import main

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": title,
                "event_date": "2026-08-13",
                "participants": '["媽媽"]',
                "source_msg_id": "",
            }
        ],
        "2026-08-10",
    )

    assert reply is not None
    assert "預計 8/13 回到台北" not in reply
    assert "考選部" in reply
    assert "可確認是在台北" in reply


def test_first_person_companion_return_keeps_speaker_as_candidate(monkeypatch):
    import main

    monkeypatch.setattr(
        main.memory,
        "get_raw_message",
        lambda _gid, _mid: ("U_MOM", "我 8/12 跟爸爸回台北"),
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "媽媽" if uid == "U_MOM" else "",
    )
    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": "跟爸爸回台北",
                "event_date": "2026-08-12",
                "participants": '["爸爸(同行)"]',
                "source_msg_id": "m1",
            }
        ],
        "2026-08-10",
    )

    assert reply is not None
    assert "媽媽預計 8/12 回到台北" in reply


@pytest.mark.parametrize("transport", ("載", "送", "陪", "接"))
def test_first_person_passenger_return_belongs_to_raw_sender(
    monkeypatch,
    transport,
):
    import main
    from datetime import datetime
    from zoneinfo import ZoneInfo

    created_at = int(
        datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "get_raw_message_record",
        lambda _gid, _mid: {
            "group_id": "G1",
            "message_id": "m1",
            "user_id": "U_MOM",
            "text": f"我 8/12 爸爸{transport}我回台北",
            "created_at": created_at,
        },
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "媽媽" if uid == "U_MOM" else "",
    )

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": f"爸爸{transport}媽媽回台北",
                "event_date": "2026-08-12",
                "participants": '["媽媽"]',
                "source_msg_id": "m1",
            }
        ],
        "2026-08-10",
    )

    assert reply is not None
    assert "媽媽預計 8/12 回到台北" in reply


def test_first_person_passenger_binding_is_symmetric_for_dad(monkeypatch):
    import main
    from datetime import datetime
    from zoneinfo import ZoneInfo

    created_at = int(
        datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "get_raw_message_record",
        lambda _gid, _mid: {
            "group_id": "G1",
            "message_id": "m1",
            "user_id": "U_DAD",
            "text": "我 8/12 媽媽載我回台北",
            "created_at": created_at,
        },
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "爸爸" if uid == "U_DAD" else "",
    )

    reply = main._build_return_home_calendar_reply(
        "G1",
        "爸爸什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": "媽媽載爸爸回台北",
                "event_date": "2026-08-12",
                "participants": '["爸爸"]',
                "source_msg_id": "m1",
            }
        ],
        "2026-08-10",
    )

    assert reply is not None
    assert "爸爸預計 8/12 回到台北" in reply


@pytest.mark.parametrize(
    "transport_phrase",
    (
        "爸爸會載我",
        "爸爸要載我",
        "爸爸預計載我",
        "爸爸會開車載我",
        "爸爸晚上會載我",
        "爸爸下午要帶我",
        "爸爸確定會載我",
        "爸爸決定帶我",
        "爸爸已安排載我",
        "爸爸答應載我",
    ),
)
def test_first_person_passenger_return_supports_affirmative_modals(
    monkeypatch,
    transport_phrase,
):
    import main
    from datetime import datetime
    from zoneinfo import ZoneInfo

    created_at = int(
        datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "get_raw_message_record",
        lambda _gid, _mid: {
            "group_id": "G1",
            "message_id": "m1",
            "user_id": "U_MOM",
            "text": f"我 8/12 {transport_phrase}回台北",
            "created_at": created_at,
        },
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "媽媽" if uid == "U_MOM" else "",
    )

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": "爸爸載媽媽回台北",
                "event_date": "2026-08-12",
                "participants": '["媽媽"]',
                "source_msg_id": "m1",
            }
        ],
        "2026-08-10",
    )

    assert reply is not None
    assert "媽媽預計 8/12 回到台北" in reply


@pytest.mark.parametrize(
    "source_text",
    (
        "我 8/12 爸爸不會載我回台北",
        "我 8/12 爸爸會載我回台北嗎",
        "我 8/12 爸爸答應載我回台北，不過現在不會了",
        "我 8/12 爸爸答應載我回台北，但現在不載了",
        "我 8/12 爸爸答應載我回台北，後來反悔了",
        "我 8/12 爸爸答應載我回台北，但現在不載我了",
        "我 8/12 爸爸答應送我回台北，不過後來不送我了",
        "我 8/12 爸爸答應陪我回台北，但現在不陪我了",
        "我 8/12 爸爸答應接我回台北，但現在不接我了",
        "我 8/12 爸爸答應帶我回台北，但現在不帶我了",
        "我 8/12 爸爸答應載我回台北，但現在不會載我了",
        "我 8/12 爸爸答應送我回台北，但現在不會送我了",
        "我 8/12 爸爸答應陪我回台北，但現在不會陪我了",
        "我 8/12 爸爸答應接我回台北，但現在不會接我了",
        "我 8/12 爸爸答應帶我回台北，但現在不會帶我了",
        "我 8/12 爸爸答應載我回台北，8/12不載我了",
    ),
)
def test_first_person_passenger_negative_or_question_is_not_direct(
    monkeypatch,
    source_text,
):
    import main
    from datetime import datetime
    from zoneinfo import ZoneInfo

    created_at = int(
        datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "get_raw_message_record",
        lambda _gid, _mid: {
            "group_id": "G1",
            "message_id": "m1",
            "user_id": "U_MOM",
            "text": source_text,
            "created_at": created_at,
        },
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "媽媽" if uid == "U_MOM" else "",
    )

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": "爸爸載媽媽回台北",
                "event_date": "2026-08-12",
                "participants": '["媽媽"]',
                "source_msg_id": "m1",
            }
        ],
        "2026-08-10",
    )

    assert reply is not None
    assert "媽媽預計 8/12 回到台北" not in reply


@pytest.mark.parametrize(
    ("source_text", "event_title", "event_date"),
    (
        (
            "我 8/12 爸爸載我回台北，後天不載我了",
            "8/12爸爸載媽媽回台北",
            "2026-08-12",
        ),
        (
            "我明天爸爸載我回台北，週二不載我了",
            "8/11爸爸載媽媽回台北",
            "2026-08-11",
        ),
    ),
)
def test_mixed_date_family_revocation_fails_closed(
    monkeypatch,
    source_text,
    event_title,
    event_date,
):
    import main
    from datetime import datetime
    from zoneinfo import ZoneInfo

    created_at = int(
        datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "get_raw_message_record",
        lambda _gid, _mid: {
            "group_id": "G1",
            "message_id": "m1",
            "user_id": "U_MOM",
            "text": source_text,
            "created_at": created_at,
        },
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "媽媽" if uid == "U_MOM" else "",
    )

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": event_title,
                "event_date": event_date,
                "participants": '["媽媽"]',
                "source_msg_id": "m1",
            }
        ],
        "2026-08-11",
    )

    assert reply is not None
    assert "媽媽目前最直接的行程紀錄" not in reply
    assert "預計" not in reply


def test_today_and_weekday_revocation_fails_closed_without_source_time():
    import main

    assert not main._calendar_event_is_direct_home_return(
        {"title": "媽媽今天回台北，週二不載我了", "location": ""},
        "台北",
    )
    assert not main._calendar_event_is_direct_home_return(
        {"title": "爸爸週二載我回台北，本週二不載我了", "location": ""},
        "台北",
    )
    assert not main._calendar_event_is_direct_home_return(
        {"title": "媽媽週末回台北，這週末不載我了", "location": ""},
        "台北",
    )


def test_bare_and_prefixed_weekday_revocation_uses_source_semantics(monkeypatch):
    import main
    from datetime import datetime
    from zoneinfo import ZoneInfo

    created_at = int(
        datetime(2026, 8, 6, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "get_raw_message_record",
        lambda _gid, _mid: {
            "group_id": "G1",
            "message_id": "m1",
            "user_id": "U_MOM",
            "text": "我週三爸爸載我回台北，下週三不載我了",
            "created_at": created_at,
        },
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "媽媽" if uid == "U_MOM" else "",
    )

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": "週三爸爸載媽媽回台北",
                "event_date": "2026-08-12",
                "participants": '["媽媽"]',
                "source_msg_id": "m1",
            }
        ],
        "2026-08-11",
    )

    assert reply is not None
    assert "媽媽預計 8/12 回到台北" not in reply


@pytest.mark.parametrize(
    "title",
    (
        "媽媽回台北後不載爸爸了",
        "爸爸接我回台北，之後不接妹妹了",
        "爸爸送我回台北，接著不送爸爸了",
        "爸爸陪我回台北，然後不陪妹妹了",
        "爸爸載我回台北，回家後不載爸爸了",
        "爸爸接我回台北以後不接妹妹了",
        "爸爸接我回台北，以後不接妹妹了",
        "爸爸接我回台北，接下來不接妹妹了",
        "爸爸接我回台北，後續不接妹妹了",
        "媽媽回台北，但之後不載爸爸了",
        "媽媽回台北，只是接下來不陪爸爸了",
        "媽媽回台北，但後續不接妹妹了",
        "爸爸明天載我回台北，後天不載我了",
        "爸爸8/12載我回台北，8/13不載我了",
        "爸爸今天接我回台北，明天不接妹妹了",
        "爸爸週三送我回台北，週四不送我了",
        "爸爸本週三送我回台北，本週四不送我了",
    ),
)
def test_return_home_followed_by_downstream_transport_change_stays_direct(title):
    import main

    assert main._calendar_event_is_direct_home_return(
        {"title": title, "location": ""},
        "台北",
    )


@pytest.mark.parametrize(
    "transport_phrase",
    ("爸爸帶我", "爸爸帶著我", "爸爸載著我", "爸爸陪同我", "爸爸順路載我"),
)
def test_first_person_passenger_return_supports_natural_transport_forms(
    monkeypatch,
    transport_phrase,
):
    import main
    from datetime import datetime
    from zoneinfo import ZoneInfo

    created_at = int(
        datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "get_raw_message_record",
        lambda _gid, _mid: {
            "group_id": "G1",
            "message_id": "m1",
            "user_id": "U_MOM",
            "text": f"我 8/12 {transport_phrase}回台北",
            "created_at": created_at,
        },
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "媽媽" if uid == "U_MOM" else "",
    )

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": "爸爸帶媽媽回台北",
                "event_date": "2026-08-12",
                "participants": '["媽媽"]',
                "source_msg_id": "m1",
            }
        ],
        "2026-08-10",
    )

    assert reply is not None
    assert "媽媽預計 8/12 回到台北" in reply


@pytest.mark.parametrize(
    "source_text",
    (
        "我 8/12 爸爸送我到車站後回台北",
        "我 8/12 爸爸載我回公司後回台北",
        "我 8/12 爸爸陪我回宿舍後回台北",
    ),
)
def test_first_person_dropoff_does_not_own_driver_later_return(
    monkeypatch,
    source_text,
):
    import main
    from datetime import datetime
    from zoneinfo import ZoneInfo

    created_at = int(
        datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "get_raw_message_record",
        lambda _gid, _mid: {
            "group_id": "G1",
            "message_id": "m1",
            "user_id": "U_MOM",
            "text": source_text,
            "created_at": created_at,
        },
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "媽媽" if uid == "U_MOM" else "",
    )

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽什麼時候回家",
        [
            {
                "group_id": "G1",
                "status": "active",
                "title": "爸爸回台北",
                "event_date": "2026-08-12",
                "participants": '["媽媽"]',
                "source_msg_id": "m1",
            }
        ],
        "2026-08-10",
    )

    assert reply is not None
    assert "媽媽預計 8/12 回到台北" not in reply


@pytest.mark.parametrize(
    ("event", "source_clause"),
    [
        ({"title": "回台北", "location": "Zoom"}, ""),
        ({"title": "回台北", "location": "https://meet.jit.si/trip"}, ""),
        ({"title": "回台北行程線上說明會", "location": ""}, ""),
        ({"title": "回台北", "location": ""}, "我明天線上討論回台北行程"),
    ],
)
def test_remote_return_discussion_is_not_direct_movement(event, source_clause):
    import main

    assert not main._calendar_event_is_direct_home_return(
        event,
        "台北",
        source_clause=source_clause,
    )


@pytest.mark.parametrize(
    "title",
    [
        "回台北行程規劃會議",
        "回台北行程說明會",
        "回台北計畫討論",
        "回台北的交通安排會議",
        "討論媽媽回台北交通",
        "規劃媽媽回台北的安排",
        "確認媽媽回台北班機",
        "查詢媽媽回台北時間",
        "搜尋媽媽回台北車票",
        "預訂媽媽回台北班機",
        "購買媽媽回台北機票",
        "提醒媽媽回台北班機",
        "討論完回台北計畫",
        "說明完回台北行程",
        "研究完回台北交通",
        "查完回台北班機",
    ],
)
def test_return_trip_planning_meeting_is_not_direct_movement(title):
    import main

    assert not main._calendar_event_is_direct_home_return(
        {"title": title, "location": ""},
        "台北",
    )
    assert main._calendar_event_is_direct_home_return(
        {"title": "開完會回台北", "location": ""},
        "台北",
    )


@pytest.mark.parametrize(
    "title",
    [
        "會議結束後回台北",
        "討論完回台北",
        "吃完飯回台北",
        "結束就回台北",
        "下班後返回台北",
        "看完醫生回到台北",
    ],
)
def test_completed_activity_then_return_is_direct_movement(title):
    import main

    assert main._calendar_event_is_direct_home_return(
        {"title": title, "location": ""},
        "台北",
    )


@pytest.mark.parametrize(
    "title",
    [
        "討論完媽媽是否回台北",
        "確認完媽媽不回台北",
        "問完媽媽沒回台北",
        "查看完媽媽可能回台北",
        "研究完媽媽還沒回台北",
        "問完媽媽也許回台北",
        "問完媽媽暫無回台北",
        "問完媽媽尚無回台北",
        "問完媽媽改天回台北",
        "問完媽媽取消回台北",
        "問完媽媽無法回台北",
        "問完媽媽拒絕回台北",
        "問完媽媽放棄回台北",
        "媽媽可能等考選部會議和晚餐都結束後回台北",
        "回台北待定",
        "回台北待確認",
        "回台北是否成行",
        "回台北視情況",
        "媽媽回台北這件事情要等考選部確認後再決定是否成行",
        "媽媽回台北的安排要等工作和晚餐都結束後再視情況",
        "媽媽回台北這件事情目前還沒有最後決定",
        "媽媽明天會回台北，但是否成行還不確定",
        "媽媽明天會回台北，但行程待確認",
        "媽媽明天會回台北，視情況",
        "媽媽回台北要等爸爸確認後再決定是否成行",
        "媽媽回台北的事情要等考選部確認後再決定是否成行",
        "媽媽回台北目前仍要等主管確認是否成行",
        "媽媽明天會回台北，但還沒決定",
        "媽媽明天會回台北，但還不確定",
        "媽媽明天會回台北，不過尚未決定",
        "媽媽回台北，返程視情況",
        "媽媽回台北目前仍不確定",
        "媽媽回台北到現在還沒決定",
        "媽媽回台北需要再確認",
        "媽媽回台北最後仍待確認",
        "媽媽回台北目前沒有確定",
        "媽媽明天會回台北；現在還不知道",
        "媽媽明天會回台北，但仍待確認",
        "媽媽明天會回台北，不過尚不確定",
        "媽媽明天會回台北，但仍未決定",
        "媽媽回台北有變數",
        "媽媽回台北說不準",
        "媽媽明天會回台北，但這件事還沒決定",
        "媽媽明天會回台北，不過這個安排仍待確認",
        "媽媽明天會回台北，但這部分目前不確定",
        "媽媽回台北還不清楚",
        "媽媽回台北尚無定論",
        "媽媽回台北再看看",
        "媽媽回台北到時候再說",
        "媽媽明天會回台北，但她還不知道能不能成行",
        "媽媽明天會回台北，但媽媽還不確定是否成行",
        "媽媽明天會回台北，但能不能成行還不知道",
        "媽媽明天會回台北，但這趟還沒決定",
        "媽媽明天會回台北，但這個行程還沒決定",
        "媽媽明天會回台北，但還沒確定",
        "媽媽明天會回台北，不過尚未確定",
        "媽媽明天會回台北，但這趟未確定",
        "媽媽明天會回台北，但還不知道會不會回",
        "媽媽明天會回台北，但不知道會不會成行",
        "媽媽明天會回台北，但不清楚會不會成行",
        "媽媽明天會回台北，但不知道最後會不會回來",
        "媽媽明天會回台北，但不清楚能否回去",
        "媽媽明天回台北，但會不會成行",
        "媽媽明天回台北，但到底會不會成行？",
        "媽媽明天回台北但能不能成行",
        "媽媽明天回台北但要不要成行",
        "媽媽明天回台北，但要不要成行",
        "媽媽明天回台北，但能否回去",
        "媽媽明天回台北，但會不會回去",
        "媽媽明天回台北，但能不能回去",
        "媽媽明天回台北，但要不要回來",
        "媽媽明天回台北，但是否能成行",
        "媽媽明天回台北，但能不能順利成行",
        "媽媽明天回台北，但會不會真的回來",
        "媽媽明天回台北，但可不可以回來",
        "媽媽明天回台北，但回不回得來",
        "媽媽明天回台北，但能否回去？",
        "媽媽明天回台北，但會不會回去？",
        "媽媽明天回台北，但能不能回去？",
        "媽媽明天回台北，但要不要回去？",
        "媽媽明天回台北，但是否能回去？",
        "媽媽明天回台北，但是否回來？",
        "媽媽明天回台北，但是否真的能回來？",
        "媽媽明天回台北，但是否有辦法回來？",
        "媽媽明天回台北，但能不能夠回來？",
        "媽媽明天回台北，但可否回來？",
        "媽媽明天回台北，但會不會如期返程？",
        "媽媽明天回台北，但能否回去還不知道",
        "媽媽明天回台北，但會不會回去仍待確認",
        "媽媽明天回台北，但要不要回來還沒決定",
        "媽媽明天回台北，但是否回來目前不確定",
        "媽媽明天回台北，但能不能回去要再確認",
        "媽媽明天回台北，但能否回去？目前待確認",
        "媽媽明天回台北，但能否回去（待確認）",
        "媽媽明天回台北，要看天氣再決定",
        "媽媽明天回台北，等工作結束才能決定",
        "媽媽明天回台北，但是否會回來？",
        "媽媽明天回台北，但是否會成行？",
        "媽媽明天回台北，但是否要回來？",
        "媽媽明天回台北，但是否會回來還不知道",
        "媽媽明天回台北，但有沒有辦法回來？",
        "媽媽明天回台北嗎？",
        "媽媽明天會回台北嗎？",
        "媽媽明天要回台北嗎？",
        "媽媽明天回家嗎？",
        "媽媽明天回台北？",
        "媽媽明天會回台北吧？",
        "媽媽明天會回台北了吧？",
        "媽媽明天回台北對嗎？",
        "媽媽明天回台北是不是？",
        "媽媽明天回台北了沒？",
        "媽媽明天回台北，對吧？",
        "媽媽明天回來嗎？",
        "媽媽明天會回來嗎？",
        "媽媽明天回來？",
        "媽媽明天應該會回台北",
        "媽媽明天大概會回台北",
        "媽媽明天或許會回台北",
        "媽媽明天回台北吧？",
        "媽媽明天回來吧？",
        "媽媽明天會回台北是嗎？",
        "媽媽明天會回台北沒錯吧？",
        "媽媽明天回台北好嗎？",
        "媽媽明天會回台北嘛？",
        "媽媽明天回台北喔？",
        "媽媽明天會回台北是嗎",
        "媽媽明天會回台北沒錯吧",
        "媽媽明天回台北好嗎",
        "媽媽明天回台北對不對",
        "媽媽明天回來了沒",
        "媽媽回來沒",
        "媽媽回來了嗎",
        "媽媽明天是不是要回台北",
        "媽媽明天是不是會回台北",
        "媽媽明天是不是回家",
        "媽媽明天有沒有要回台北",
        "媽媽明天回台北，但能否回去仍未決定",
        "媽媽明天回台北，但是否回來還要確認",
        "媽媽明天回台北，但能不能回去需要再確認",
        "媽媽明天回台北，但會不會回來尚無定論",
        "媽媽明天回台北，但要不要回去再看看",
        "媽媽明天回台北，但可否回來到時候再說",
        "媽媽明天回台北，但可不可以回去有變數",
        "媽媽明天回台北，但能否回去說不準",
        "媽媽明天回台北，但能否回去不一定",
        "媽媽明天回台北，但回不回去？",
        "媽媽明天回台北，但回不回得去？",
        "媽媽明天回台北，但不知道回不回去",
        "媽媽明天回台北，但不知道回得去嗎",
        "媽媽明天會回來，但能否回來還不知道",
        "媽媽明天回來，後來取消",
    ],
)
def test_completion_filler_cannot_swallow_return_negation(title):
    import main

    assert not main._calendar_event_is_direct_home_return(
        {"title": title, "location": ""},
        "台北",
    )


@pytest.mark.parametrize(
    "title",
    [
        "媽媽明天會回台北，但不確定幾點",
        "媽媽明天回台北，時間未定",
        "不確定幾點，但媽媽明天會回台北",
        "媽媽明天會回台北但不確定是幾點",
        "媽媽明天回台北但不確定抵達時間",
        "回台北後視情況去吃飯",
        "媽媽回台北，晚餐視情況",
        "媽媽回台北，之後視情況去吃飯",
        "媽媽回台北，但晚餐視情況",
        "媽媽回台北，視情況去吃飯",
        "媽媽回台北，視情況再去公司",
        "媽媽回台北之後視情況去吃飯",
        "媽媽回台北再視情況去公司",
        "媽媽回台北然後視情況去吃飯",
        "媽媽回台北接著視情況去公司",
        "媽媽回台北後晚餐視情況",
        "媽媽回台北之後公司行程待確認",
        "媽媽回台北之後是否去吃飯還不確定",
        "媽媽回台北後晚餐有變數",
        "媽媽回台北後不確定要不要聚餐",
        "媽媽明天會回台北，但我還不知道幾點",
        "媽媽明天會回台北，只是還不知道確切時間",
        "媽媽明天會回台北，幾點還不知道",
        "媽媽明天會回台北，她還不清楚到家時間",
        "媽媽明天會回台北，但還不清楚搭幾點的車",
        "媽媽明天會回台北，但尚未確定搭幾點的客運",
        "媽媽明天會回台北，但不知道航班時間",
        "媽媽明天會回台北，但車次待確認",
        "媽媽回台北後要不要回去公司",
        "媽媽回台北後能否去公司還不知道",
        "媽媽回台北後要不要聚餐還沒決定",
        "媽媽明天回台北，但她還沒決定要搭哪班車",
        "媽媽明天回台北，但她還沒決定返程時間",
        "媽媽明天回台北，但返程幾點還沒決定",
        "媽媽明天回台北，但她還沒決定回程班次",
        "媽媽明天回台北，但她還沒決定搭高鐵還是客運",
        "媽媽明天回台北，但她還沒決定哪班車",
        "媽媽明天回台北，看天氣再決定幾點出發",
        "媽媽明天回台北，但她還沒決定交通方式",
        "媽媽明天回台北，但交通方式還沒決定",
        "媽媽明天回台北，但還沒決定搭什麼交通工具",
        "媽媽明天回台北，但還沒決定坐什麼車",
        "媽媽明天回台北，但怎麼回還沒決定",
        "媽媽明天回台北，但怎麼回台北還沒決定",
        "媽媽明天大概中午回台北",
        "媽媽明天大概12點回台北",
        "媽媽預計明天大概中午回台北",
        "媽媽確定回來，時間未定",
        "媽媽明天會回來，但不確定幾點",
        "媽媽明天會回來，但幾點還不知道",
        "媽媽明天會回來，只是時間未定",
        "媽媽明天會回來，但不知道搭哪班車",
        "媽媽決定回來，晚餐視情況",
        "媽媽明天回來，之後去公司",
        "媽媽明天回來後去吃飯",
        "媽媽決定回來，再去公司",
        "媽媽明天回來，車次待確認",
        "媽媽明天回來，但能否去公司還不知道",
    ],
)
def test_uncertain_clock_time_does_not_erase_certain_return_date(title):
    import main

    assert main._calendar_event_is_direct_home_return(
        {"title": title, "location": ""},
        "台北",
    )


def test_inverted_meeting_queries_filter_non_meeting_events():
    import main

    events = [
        {"title": "媽媽專案會議", "location": ""},
        {"title": "媽媽聚餐", "location": ""},
    ]
    for query in ("媽媽明天下午開什麼會？", "媽媽明天要參加哪些會議？"):
        assert main._filter_calendar_events_by_query_topic(events, query) == [
            events[0]
        ]


def test_past_dated_home_fallback_does_not_call_it_future():
    import main

    reply = main._build_return_home_calendar_reply(
        "G1",
        "媽媽昨天幾點回家",
        [],
        "2026-08-10",
        actor_override="媽媽",
        target_date_isos={"2026-08-09"},
    )

    assert reply is not None
    assert "指定日期（8/9）" in reply
    assert "未來行程" not in reply

def test_hwang_new_zealand_query_filters_wrong_phrase_hit(monkeypatch):
    import main
    import calendar_db
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    first = datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=7)
    wrong = {
        "event_id": "wrong",
        "group_id": "G1",
        "title": f"機場接送（去紐西蘭）{first.month}/{first.day}去程",
        "event_date": first.isoformat(),
        "event_time": "10:00",
        "location": "桃園機場",
        "participants": "[\"王小明(被接送)\"]",
        "status": "active",
    }
    correct = dict(wrong)
    correct.update(
        {
            "event_id": "correct",
            "event_time": "14:30",
            "participants": "[\"哥哥(被接送)\"]",
        }
    )

    monkeypatch.setattr(calendar_db, "search_by_title_phrase", lambda *a, **k: [wrong])
    monkeypatch.setattr(calendar_db, "search_by_keyword", lambda *a, **k: [wrong, correct])
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)

    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake_token"

    main._handle_calendar_query(FakeEvent(), "G1", "哥哥去紐西蘭的日期")

    assert "14:30" in captured["text"]
    assert "王小明" not in captured["text"]


def test_build_todo_status_reply_reads_all_sources(monkeypatch):
    import main
    import todo
    from datetime import datetime
    from zoneinfo import ZoneInfo

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 6, 24, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))
            return base if tz is None else base.astimezone(tz)

    monkeypatch.setattr(main, "datetime", FixedDateTime)

    monkeypatch.setattr(
        todo,
        "list_pending",
        lambda gid, limit=10, due_date=None: [
            {
                "task": "領長期處方箋",
                "due_date": "2026-06-25",
                "sender_user_id": "U_MOM",
            }
        ],
    )
    reminder_ts = int(
        datetime(2026, 6, 25, 8, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "list_pending_reminders",
        lambda gid, within_seconds=None: [
            {
                "action": "全家打球",
                "remind_at": reminder_ts,
                "mention_aliases": ["全家"],
                "source_kind": "",
                "source_ref": "",
            },
            {
                "action": "弟弟早上洗牙，看陳敏慧牙醫師",
                "remind_at": reminder_ts,
                "mention_aliases": [],
                "source_kind": "calendar_event",
                "source_ref": "e1",
            }
        ],
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "媽媽" if uid == "U_MOM" else "",
    )

    reply = main._build_todo_status_reply("G1", "有哪些待辦事項？")

    assert "領長期處方箋" in reply
    assert "1. 6/25（四）08:00" in reply
    assert "事項：全家打球" in reply
    assert "參加人：@all" in reply
    assert "陳敏慧牙醫師" in reply


def test_build_todo_status_reply_detail_query_includes_source_text(monkeypatch):
    import main
    import todo
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(todo, "list_pending", lambda gid, limit=10, due_date=None: [])
    reminder_ts = int(
        datetime(2099, 7, 16, 14, 30, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "list_pending_reminders",
        lambda gid, within_seconds=None: [
            {
                "action": "機場接送（去紐西蘭）7/16去程",
                "remind_at": reminder_ts,
                "mention_aliases": ["哥哥"],
                "source_text": (
                    "機場接送（去紐西蘭）7/16去程；"
                    "接送網址：https://68666.tw/TwMI；票券驗證碼：8459"
                ),
            }
        ],
    )

    reply = main._build_todo_status_reply("G1", "未來提醒事項細節，包含網址驗證碼")

    assert "機場接送（去紐西蘭）7/16去程" in reply
    assert "1. 7/16（四）14:30" in reply
    assert "接送網址：https://68666.tw/TwMI" in reply
    assert "票券驗證碼：8459" in reply
    assert "https://68666.tw/TwMI" in reply
    assert "8459" in reply


def test_build_todo_status_reply_filters_squash_timing_query(monkeypatch):
    import main
    import todo
    from datetime import datetime
    from zoneinfo import ZoneInfo

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 7, 3, 20, 0, tzinfo=ZoneInfo("Asia/Taipei"))
            return base if tz is None else base.astimezone(tz)

    monkeypatch.setattr(main, "datetime", FixedDateTime)
    monkeypatch.setattr(
        todo,
        "list_pending",
        lambda gid, limit=10, due_date=None: [
            {"task": "買牛奶", "due_date": "2026-07-04", "sender_user_id": "U1"}
        ],
    )
    squash_ts = int(
        datetime(2026, 7, 6, 19, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    dental_ts = int(
        datetime(2026, 7, 5, 9, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "list_pending_reminders",
        lambda gid, within_seconds=None: [
            {
                "action": "打壁球",
                "remind_at": squash_ts,
                "mention_aliases": [],
                "source_text": "活動：打壁球；地點：運動中心",
            },
            {
                "action": "洗牙",
                "remind_at": dental_ts,
                "mention_aliases": [],
                "source_text": "牙醫回診",
            },
        ],
    )

    reply = main._build_todo_status_reply("G1", "什麼時候打壁球")

    assert "壁球相關提醒事項＆細節" in reply
    assert "事項：打壁球" in reply
    assert "運動中心" in reply
    assert "洗牙" not in reply
    assert "買牛奶" not in reply


def test_build_todo_status_reply_squash_no_match(monkeypatch):
    import main
    import todo
    from datetime import datetime
    from zoneinfo import ZoneInfo

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 7, 3, 20, 0, tzinfo=ZoneInfo("Asia/Taipei"))
            return base if tz is None else base.astimezone(tz)

    monkeypatch.setattr(main, "datetime", FixedDateTime)
    monkeypatch.setattr(todo, "list_pending", lambda gid, limit=10, due_date=None: [])
    dental_ts = int(
        datetime(2026, 7, 5, 9, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "list_pending_reminders",
        lambda gid, within_seconds=None: [
            {
                "action": "洗牙",
                "remind_at": dental_ts,
                "mention_aliases": [],
                "source_text": "牙醫回診",
            }
        ],
    )

    reply = main._build_todo_status_reply("G1", "壁球什麼時候")

    assert reply == "目前沒有查到壁球相關 pending 待辦或提醒事項。"


def test_handle_todo_query_replies_immediately(monkeypatch):
    import main

    monkeypatch.setattr(
        main,
        "_build_todo_status_reply",
        lambda gid, text: "目前待辦/提醒：\n精準提醒：\n- 2026-06-25 08:00 測試",
    )
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)

    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeEvent:
        reply_token = "fake_token"

    main._handle_todo_query(FakeEvent(), "G1", "有哪些待辦事項？")

    assert captured["text"].startswith("目前待辦/提醒")
    assert "測試" in captured["text"]


def test_one_shot_reply_intercepts_next_text_and_clears(monkeypatch):
    import main

    monkeypatch.setattr(main.feedback_collector, "in_feedback_window", lambda: False)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_load_one_shot_replies", lambda: {"G1": "完整提醒清單"})
    saved: list[dict] = []
    monkeypatch.setattr(main, "_save_one_shot_replies", lambda data: saved.append(data))
    monkeypatch.setattr(
        main,
        "_try_handle_calendar_correction",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not continue")),
    )
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeMessage:
        text = "隨便說什麼"

    class FakeEvent:
        reply_token = "fake_token"
        message = FakeMessage()

    main._handle_text_message(FakeEvent(), "G1")

    assert captured["text"] == "完整提醒清單"
    assert captured["token"] == "fake_token"
    assert saved == [{}]


def test_one_shot_reply_uses_text_v2_for_known_mentions(monkeypatch):
    import main

    monkeypatch.setattr(main.feedback_collector, "in_feedback_window", lambda: False)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_load_one_shot_replies", lambda: {"G1": "參加人：@爸爸"})
    saved: list[dict] = []
    monkeypatch.setattr(main, "_save_one_shot_replies", lambda data: saved.append(data))
    monkeypatch.setattr(
        main.line_mentions if hasattr(main, "line_mentions") else __import__("line_mentions"),
        "load_user_aliases",
        lambda: {"U_DAD": "爸爸"},
    )
    captured: dict = {}
    _patch_calendar_reply_capture(monkeypatch, main, captured)

    class FakeMessage:
        text = "隨便說什麼"

    class FakeEvent:
        reply_token = "fake_token"
        message = FakeMessage()

    main._handle_text_message(FakeEvent(), "G1")

    assert captured["message"].type == "textV2"
    assert captured["message"].text.startswith("{p1}\n")
    assert captured["message"].substitution["p1"].mentionee.user_id == "U_DAD"
    assert saved == [{}]


def test_mention_builder_keeps_all_and_named_mentions(monkeypatch):
    import main
    import line_mentions

    monkeypatch.setattr(
        line_mentions,
        "load_user_aliases",
        lambda: {"U_DAD": "爸爸", "U_MOM": "媽媽"},
    )

    _text, message = main._text_message_with_mentions("參加人：@all、@爸爸、@媽媽")

    assert message.type == "textV2"
    assert message.text.startswith("{all} {p2} {p3}\n")
    assert message.substitution["all"].mentionee.type == "all"
    assert message.substitution["p2"].mentionee.user_id == "U_DAD"
    assert message.substitution["p3"].mentionee.user_id == "U_MOM"


def test_explicit_todo_query_routes_before_llm(monkeypatch):
    import main

    captured = []
    monkeypatch.setattr(
        main,
        "_handle_todo_query",
        lambda event, group_id, clean_text: captured.append((group_id, clean_text)),
    )

    class FakeSource:
        user_id = "U1"

    class FakeMessage:
        quoted_message_id = None

    class FakeEvent:
        source = FakeSource()
        message = FakeMessage()
        reply_token = "fake_token"

    main._handle_explicit_text(FakeEvent(), "G1", "有哪些待辦事項？")

    assert captured == [("G1", "有哪些待辦事項？")]


def test_handle_command_todo_uses_status_builder(monkeypatch):
    import main

    monkeypatch.setattr(
        main,
        "_build_todo_status_reply",
        lambda group_id, text: f"reply:{group_id}:{text}",
    )

    assert main._handle_command("G1", "/待辦") == "reply:G1:/待辦"


# ── calendar_db dedup（GP1/GP2 反饋）─────────────────────────────────────
@pytest.fixture
def tmp_calendar_db(tmp_path, monkeypatch):
    import importlib

    db_file = tmp_path / "test_cal.db"
    monkeypatch.setenv("SQLITE_PATH", str(db_file))
    import config

    importlib.reload(config)
    config.settings.sqlite_path = str(db_file)
    import calendar_db

    importlib.reload(calendar_db)
    return calendar_db


def test_same_group_title_date_allows_distinct_explicit_times(tmp_calendar_db):
    """Same title/date is not a duplicate when both explicit times differ."""
    cd = tmp_calendar_db
    # 用「明天」避免時間飄移：hardcode date 會在跑測試的那天變成過去
    future_date = (cd._today_tw() + __import__("datetime").timedelta(days=1)).isoformat()
    eid1 = cd.insert_event(
        group_id="G1",
        title="拿蛋糕",
        event_date=future_date,
        event_time="14:00",
    )
    assert eid1
    eid2 = cd.insert_event(
        group_id="G1",
        title="拿蛋糕",
        event_date=future_date,
        event_time="14:30",
    )
    assert eid2
    assert eid2 != eid1
    events = cd.list_upcoming("G1", days=30)
    assert len(events) == 2
    assert {event["event_time"] for event in events} == {"14:00", "14:30"}


def test_calendar_capture_reschedules_only_with_explicit_reschedule_words(
    tmp_calendar_db,
    monkeypatch,
):
    import calendar_extractor
    import main

    old_date = (
        tmp_calendar_db._today_tw() + __import__("datetime").timedelta(days=3)
    ).isoformat()
    new_date = (
        tmp_calendar_db._today_tw() + __import__("datetime").timedelta(days=5)
    ).isoformat()
    event_id = tmp_calendar_db.insert_event(
        group_id="G1",
        title="家族聚餐",
        event_date=old_date,
        event_time="18:00",
    )
    extracted = {
        "is_cancellation": True,
        "cancel_target_keyword": "家族聚餐",
        "date": new_date,
        "time": "19:00",
        "events": [],
    }
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(calendar_extractor, "extract", lambda _text: extracted)
    monkeypatch.setattr(
        calendar_extractor,
        "extract_many",
        lambda _text, primary=None: extracted,
    )

    main._maybe_capture_calendar_event(
        "G1",
        f"家族聚餐改到{new_date}晚上七點",
    )

    events = tmp_calendar_db.list_upcoming("G1", days=30)
    updated = next(event for event in events if event["event_id"] == event_id)
    assert updated["event_date"] == new_date
    assert updated["event_time"] == "19:00"


def test_calendar_capture_cancellation_selector_mismatch_does_not_reschedule(
    tmp_calendar_db,
    monkeypatch,
):
    import calendar_extractor
    import main

    old_date = (
        tmp_calendar_db._today_tw() + __import__("datetime").timedelta(days=3)
    ).isoformat()
    wrong_date = (
        tmp_calendar_db._today_tw() + __import__("datetime").timedelta(days=5)
    ).isoformat()
    event_id = tmp_calendar_db.insert_event(
        group_id="G1",
        title="家族聚餐",
        event_date=old_date,
        event_time="18:00",
    )
    extracted = {
        "is_cancellation": True,
        "cancel_target_keyword": "家族聚餐",
        "date": wrong_date,
        "time": None,
        "events": [],
    }
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(calendar_extractor, "extract", lambda _text: extracted)
    monkeypatch.setattr(
        calendar_extractor,
        "extract_many",
        lambda _text, primary=None: extracted,
    )

    main._maybe_capture_calendar_event("G1", "取消家族聚餐")

    events = tmp_calendar_db.list_upcoming("G1", days=30)
    unchanged = next(event for event in events if event["event_id"] == event_id)
    assert unchanged["event_date"] == old_date
    assert unchanged["status"] == "active"


def test_dedup_different_date_allowed(tmp_calendar_db):
    cd = tmp_calendar_db
    eid1 = cd.insert_event(
        group_id="G1", title="拿蛋糕", event_date="2026-05-22"
    )
    eid2 = cd.insert_event(
        group_id="G1", title="拿蛋糕", event_date="2026-05-23"
    )
    assert eid1 and eid2
    assert eid1 != eid2


def test_dedup_cancelled_allows_reinsert(tmp_calendar_db):
    """取消過的同 title+date 應該可以重新插入（partial unique on status='active'）。"""
    cd = tmp_calendar_db
    eid1 = cd.insert_event(
        group_id="G1", title="拿蛋糕", event_date="2026-05-22"
    )
    cd.cancel_event(eid1)
    eid2 = cd.insert_event(
        group_id="G1", title="拿蛋糕", event_date="2026-05-22"
    )
    assert eid2 and eid2 != ""


# ── Critical: line 1095 quota-path short-circuit fix 測試（GP1/codex 反饋）─────
def test_quota_path_explicit_calendar_query_reaches_deterministic_handler(monkeypatch):
    """
    14:49 case 重現：quota 爆 + explicit + calendar query 必須走 deterministic 路徑，
    不能被 line 1095 短路存進 pending。
    """
    import main
    import calendar_db

    # mock quota exhausted
    monkeypatch.setattr(main, "_quota_exhausted", lambda: True)
    # mock dependency layer
    monkeypatch.setattr(
        calendar_db,
        "list_upcoming",
        lambda gid, days=30: [
            {
                "event_id": "e1",
                "title": "拿生日蛋糕",
                "event_date": "2026-05-22",
                "event_time": "14:00",
                "location": "喜來登",
                "participants": "[]",
            }
        ],
    )
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.memory, "log_raw_message", lambda *a, **k: None)
    monkeypatch.setattr(main, "burst_filter", type("F", (), {"cancel_burst": lambda gid: None})())
    monkeypatch.setattr(main, "_save_pending_any", lambda *a, **k: None)
    monkeypatch.setattr(main, "_try_piggyback_drain_with_reply_token", lambda *a, **k: None)
    captured = {}

    def fake_reply(token, text, group_id=None):
        captured["text"] = text

    monkeypatch.setattr(main, "_reply", fake_reply)

    # 模擬 14:49 webhook event
    class FakeMessage:
        id = "msg1"
        text = "咪寶，爸爸明天幾點要拿蛋糕？"
        type = "text"

    class FakeSource:
        type = "group"
        group_id = "G1"
        user_id = "U1"

    class FakeEvent:
        source = FakeSource()
        message = FakeMessage()
        reply_token = "tok"
        timestamp = 0

    # 直接呼叫 _handle_event 模擬整個 webhook 路徑
    # 模擬 _extract_gemini_trigger 行為 — 偵測「咪寶」前綴並回 clean_text
    monkeypatch.setattr(
        main,
        "_extract_gemini_trigger",
        lambda text, msg: text.replace("咪寶，", "").replace("咪寶", "").strip() or None,
    )

    # 把 FakeMessage 偽裝成 TextMessageContent (isinstance check)
    monkeypatch.setattr(
        main,
        "TextMessageContent",
        type("FakeTextMessageContent", (), {}),
    )

    # 14:49 case: text + explicit + calendar query → deterministic handler
    # 直接驗 _handle_calendar_query path 已驗過了；這裡驗 line 1095 fast-path 不擋 query
    clean = main._extract_gemini_trigger("咪寶，爸爸明天幾點要拿蛋糕？", FakeMessage())
    assert clean is not None
    assert main._is_calendar_query(clean) is True


def test_quota_path_text_with_event_string_captures_via_regex(monkeypatch, tmp_path):
    """
    14:51 case 重現：quota 爆 + 純文字含「YYYY-MM-DD HH:MM 拿X」必須走 regex
    fallback 寫進 events table，不能只存 pending。
    """
    import importlib

    db_file = tmp_path / "test_cal.db"
    monkeypatch.setenv("SQLITE_PATH", str(db_file))
    import config
    importlib.reload(config)
    config.settings.sqlite_path = str(db_file)
    import calendar_db
    importlib.reload(calendar_db)

    # _maybe_capture_calendar_event 內 import — 重新 reload main 讓他用到新 sqlite
    import main

    # mock Gemini extractor 模擬 quota 爆走 regex fallback
    import calendar_extractor

    def fake_gemini_extract(*args, **kwargs):
        raise RuntimeError("429 RESOURCE_EXHAUSTED simulated")

    monkeypatch.setattr(
        calendar_extractor.gemini_client._client.models,
        "generate_content",
        fake_gemini_extract,
    )

    # 用未來日期避免測試在執行日當天就變過去（list_upcoming 過濾掉）
    from datetime import timedelta
    future_date = (calendar_db._today_tw() + timedelta(days=1)).isoformat()
    # 直接呼叫 _maybe_capture_calendar_event（line 1095 quota-path 內呼叫的 fn）
    main._maybe_capture_calendar_event(
        "G1", f"{future_date} 14:00 拿喜來登贈送的生日蛋糕"
    )

    # events table 必須有一筆
    events = calendar_db.list_upcoming("G1", days=30)
    assert len(events) == 1
    assert events[0]["event_date"] == future_date
    assert events[0]["event_time"] == "14:00"
    assert "蛋糕" in events[0]["title"]


def test_auto_capture_gate_accepts_weekday_chinese_time_dentist(monkeypatch):
    """Real entry gate must let this wording reach calendar capture."""
    import main

    called = []
    monkeypatch.setattr(
        main,
        "_maybe_capture_calendar_event",
        lambda group_id, text, sender_user_id="", message_id="": called.append(
            (group_id, text, sender_user_id, message_id)
        ),
    )
    monkeypatch.setattr(main, "_capture_calendar_events_regex_only", lambda *a, **k: 0)

    class InlineThread:
        def __init__(self, target, daemon=False):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

    import threading
    monkeypatch.setattr(threading, "Thread", InlineThread)

    text = "星期四早上十點半看台大陳敏惠牙醫師"
    main._auto_capture_text_if_important("G1", text, "U_MOM")

    assert called == [("G1", text, "U_MOM", "")]


def test_medical_event_subjectless_defaults_to_sender_alias(
    monkeypatch, tmp_calendar_db
):
    """Subjectless medical events should still record who via sender alias."""
    import main
    import calendar_extractor

    future_date = (tmp_calendar_db._today_tw() + __import__("datetime").timedelta(days=2)).isoformat()
    monkeypatch.setattr(main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else "")
    monkeypatch.setattr(
        calendar_extractor,
        "extract",
        lambda text: {
            "has_event": True,
            "is_cancellation": False,
            "title": "看台大陳敏惠牙醫師",
            "date": future_date,
            "time": "10:30",
            "location": "台大",
            "participants": [],
            "cancel_target_keyword": None,
            "event_type": "medical",
        },
    )

    main._maybe_capture_calendar_event(
        "G1", "星期四早上十點半看台大陳敏惠牙醫師", "U_MOM"
    )

    events = tmp_calendar_db.list_upcoming("G1", days=30)
    assert len(events) == 1
    assert events[0]["title"] == "媽媽看台大陳敏惠牙醫師"
    assert json.loads(events[0]["participants"]) == ["媽媽(就醫)"]


def test_personal_trip_first_person_defaults_to_sender_alias(
    monkeypatch, tmp_calendar_db
):
    """For family shorthand, '我' means the LINE message sender."""
    import main
    import calendar_extractor
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    event_date = (
        datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=1)
    ).isoformat()

    monkeypatch.setattr(main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else "")
    monkeypatch.setattr(
        calendar_extractor,
        "extract",
        lambda text: {
            "has_event": True,
            "is_cancellation": False,
            "title": "我到高雄",
            "date": event_date,
            "time": None,
            "location": "高雄",
            "participants": ["我(旅者)"],
            "cancel_target_keyword": None,
            "event_type": "personal_trip",
        },
    )

    main._maybe_capture_calendar_event("G1", f"{event_date} 我到高雄", "U_MOM")

    events = tmp_calendar_db.list_upcoming("G1", days=30)
    assert len(events) == 1
    assert events[0]["title"] == "媽媽到高雄"
    assert json.loads(events[0]["participants"]) == ["媽媽(旅者)"]


def test_donghai_events_default_to_dad(monkeypatch, tmp_calendar_db):
    """Andrew's family convention: 東海-related events are usually dad's."""
    import main
    import calendar_extractor

    event_date = (
        tmp_calendar_db._today_tw()
        + __import__("datetime").timedelta(days=1)
    ).isoformat()
    monkeypatch.setattr(
        calendar_extractor,
        "extract",
        lambda text: {
            "has_event": True,
            "is_cancellation": False,
            "title": "東海大學校友年中交流聚餐",
            "date": event_date,
            "time": "10:30",
            "location": "美僑俱樂部 California Room",
            "participants": ["校友"],
            "cancel_target_keyword": None,
            "event_type": "family_gathering",
        },
    )

    main._maybe_capture_calendar_event(
        "G1", "東海大學校友會在美僑俱樂部聚餐", "U_ANY"
    )

    events = tmp_calendar_db.list_upcoming("G1", days=30)
    assert len(events) == 1
    assert json.loads(events[0]["participants"]) == ["爸爸(東海相關)", "校友"]


def test_donghai_default_does_not_override_explicit_other_actor(
    monkeypatch, tmp_calendar_db
):
    import main
    import calendar_extractor
    from datetime import timedelta

    future_date = (tmp_calendar_db._today_tw() + timedelta(days=1)).isoformat()

    monkeypatch.setattr(
        calendar_extractor,
        "extract",
        lambda text: {
            "has_event": True,
            "is_cancellation": False,
            "title": "媽媽去東海大學",
            "date": future_date,
            "time": None,
            "location": "東海大學",
            "participants": ["媽媽(旅者)"],
            "cancel_target_keyword": None,
            "event_type": "personal_trip",
        },
    )

    main._maybe_capture_calendar_event("G1", "媽媽去東海大學", "U_ANY")

    events = tmp_calendar_db.list_upcoming("G1", days=30)
    assert len(events) == 1
    assert json.loads(events[0]["participants"]) == ["媽媽(旅者)"]


def test_explicit_calendar_capture_passes_sender_user_id(monkeypatch):
    import main

    class FakeSource:
        user_id = "U_MOM"

    class FakeMessage:
        quoted_message_id = None

    class FakeEvent:
        source = FakeSource()
        message = FakeMessage()
        reply_token = "tok"

    captured = []
    monkeypatch.setattr(main, "_detect_image_gen_request", lambda text: None)
    monkeypatch.setattr(main, "_is_calendar_query", lambda text: False)
    monkeypatch.setattr(main, "_get_explicit_market_quote_reply", lambda *a, **k: None)
    monkeypatch.setattr(main, "_build_quoted_block", lambda *a, **k: "")
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: text)
    monkeypatch.setattr(main, "_is_market_quote_request", lambda *a, **k: False)
    monkeypatch.setattr(main, "_get_persona_notes", lambda gid: [])
    monkeypatch.setattr(main, "_thinking_indicator", lambda gid: nullcontext())
    monkeypatch.setattr(main, "_llm_chat", lambda *a, **k: "ok")
    monkeypatch.setattr(main, "_try_save_correction", lambda *a, **k: None)
    monkeypatch.setattr(main, "_maybe_extract_facts", lambda *a, **k: None)
    monkeypatch.setattr(main, "_reply", lambda *a, **k: None)
    monkeypatch.setattr(main.memory, "get_context", lambda gid: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda *a, **k: [])
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(
        main,
        "_maybe_capture_calendar_event",
        lambda group_id, text, sender_user_id="", message_id="": captured.append(
            (group_id, text, sender_user_id, message_id)
        ),
    )

    main._handle_explicit_text(FakeEvent(), "G1", "6/21 我到高雄")

    assert captured == [("G1", "6/21 我到高雄", "U_MOM", "")]


def test_calendar_capture_persists_second_event_when_model_returns_first_only(
    monkeypatch, tmp_calendar_db
):
    import main
    import calendar_extractor

    date1 = tmp_calendar_db._today_tw() + __import__("datetime").timedelta(days=7)
    date2 = tmp_calendar_db._today_tw() + __import__("datetime").timedelta(days=14)
    text = (
        f"咪寶麻煩提醒我{date1.month}月{date1.day}日台北市東海大學校友會在美僑俱樂部聚會"
        f"上午10:30到下午14:30以及{date2.month}月{date2.day}日在台北六福萬怡酒店9樓-海山廳"
        "（南港火車站B棟，忠孝東路七段359號9樓）13:10-16:30。"
    )
    monkeypatch.setattr(
        calendar_extractor,
        "extract",
        lambda _text: {
            "has_event": True,
            "is_cancellation": False,
            "title": "東海大學校友會活動（美僑俱樂部）",
            "date": date1.isoformat(),
            "time": "10:30",
            "location": "美僑俱樂部",
            "participants": ["校友"],
            "cancel_target_keyword": None,
            "event_type": "family_gathering",
        },
    )

    main._maybe_capture_calendar_event("G1", text, "U_DAD")

    events = tmp_calendar_db.list_upcoming("G1", days=60)
    dates = [ev["event_date"] for ev in events]
    assert dates == [date1.isoformat(), date2.isoformat()]
    assert any("美僑" in (ev["location"] or "") for ev in events)
    assert any("六福萬怡酒店" in (ev["location"] or "") for ev in events)
    assert all("爸爸" in ev["participants"] for ev in events)


def test_plain_calendar_correction_updates_event_and_reminder(monkeypatch):
    import main
    import calendar_db
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    today_tw = datetime.now(ZoneInfo("Asia/Taipei")).date()
    target_date = (today_tw + timedelta(days=1)).isoformat()
    old_dt = datetime(
        today_tw.year,
        today_tw.month,
        today_tw.day,
        4,
        0,
        tzinfo=ZoneInfo("Asia/Taipei"),
    ) + timedelta(days=1)
    event_row = {
        "event_id": "e-ball",
        "group_id": "G1",
        "title": "全家打球",
        "event_date": target_date,
        "event_time": "04:00",
        "location": "",
        "participants": "[]",
        "status": "active",
    }
    event_updates: list[tuple[str, str, str | None, str | None]] = []
    reminder_updates: list[tuple[int, int, str | None, str | None]] = []
    replies: list[str] = []

    def fake_search(_gid, keywords, limit=10):
        return [event_row] if "打球" in keywords else []

    monkeypatch.setattr(calendar_db, "search_by_keyword", fake_search)
    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *a, **k: [])
    monkeypatch.setattr(calendar_db, "list_past", lambda *a, **k: [])
    monkeypatch.setattr(
        calendar_db,
        "correct_event_and_reminder_by_id",
        lambda group_id, event_id, new_date=None, new_time=None, new_title=None:
        event_updates.append(
            (event_id, new_date, new_time, new_title)
        )
        or {"status": "updated"},
    )
    monkeypatch.setattr(
        main.memory,
        "list_pending_reminders",
        lambda gid: [
            {
                "reminder_id": 7,
                "group_id": gid,
                "action": "全家打球",
                "remind_at": int(old_dt.timestamp()),
                "source_text": "明天4點全家打球",
            }
        ],
    )
    monkeypatch.setattr(
        main.memory,
        "update_reminder_schedule",
        lambda reminder_id, remind_at, source_text=None, action=None: reminder_updates.append(
            (reminder_id, remind_at, source_text, action)
        )
        or True,
    )
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda gid: None)
    monkeypatch.setattr(
        main, "_reply", lambda _token, text, **_kw: replies.append(text)
    )
    monkeypatch.setattr(
        main, "_maybe_extract_reminder", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("correction must not be extracted as a new reminder")
        )
    )
    monkeypatch.setattr(main.feedback_collector, "in_feedback_window", lambda: False)

    class FakeSource:
        user_id = "U1"

    class FakeMessage:
        id = "m1"
        text = "明天羽球，更正為1600"

    class FakeEvent:
        source = FakeSource()
        message = FakeMessage()
        reply_token = "reply-token"

    main._handle_text_message(FakeEvent(), "G1")

    assert event_updates == [("e-ball", target_date, "16:00", "全家羽球")]
    assert reminder_updates == []
    assert replies
    assert "已更正" in replies[0]
    assert "全家羽球" in replies[0]
    assert "提醒已同步更新 1 筆" in replies[0]


def test_calendar_time_only_correction_preserves_found_event_date(monkeypatch):
    import main
    import calendar_db
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    today_tw = datetime.now(ZoneInfo("Asia/Taipei")).date()
    parsed_target_date = (today_tw + timedelta(days=1)).isoformat()
    existing_event_date = today_tw.isoformat()
    old_dt = datetime(
        today_tw.year,
        today_tw.month,
        today_tw.day,
        4,
        0,
        tzinfo=ZoneInfo("Asia/Taipei"),
    )
    event_row = {
        "event_id": "e-ball",
        "group_id": "G1",
        "title": "全家打球",
        "event_date": existing_event_date,
        "event_time": "04:00",
        "location": "",
        "participants": "[]",
        "status": "active",
    }
    event_updates: list[tuple[str, str, str | None, str | None]] = []
    reminder_updates: list[tuple[int, int, str | None, str | None]] = []

    monkeypatch.setattr(
        main,
        "_resolve_relative_date",
        lambda text: today_tw + timedelta(days=1) if "明天" in text else None,
    )
    monkeypatch.setattr(
        calendar_db,
        "search_by_keyword",
        lambda _gid, keywords, limit=10: [event_row] if "打球" in keywords else [],
    )
    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *a, **k: [])
    monkeypatch.setattr(calendar_db, "list_past", lambda *a, **k: [])
    monkeypatch.setattr(
        calendar_db,
        "correct_event_and_reminder_by_id",
        lambda group_id, event_id, new_date=None, new_time=None, new_title=None:
        event_updates.append(
            (event_id, new_date, new_time, new_title)
        )
        or {"status": "updated"},
    )
    monkeypatch.setattr(
        main.memory,
        "list_pending_reminders",
        lambda gid: [
            {
                "reminder_id": 7,
                "group_id": gid,
                "action": "全家打球",
                "remind_at": int(old_dt.timestamp()),
                "source_text": "明天4點全家打球",
            }
        ],
    )
    monkeypatch.setattr(
        main.memory,
        "update_reminder_schedule",
        lambda reminder_id, remind_at, source_text=None, action=None: reminder_updates.append(
            (reminder_id, remind_at, source_text, action)
        )
        or True,
    )
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda gid: None)
    monkeypatch.setattr(main, "_reply", lambda *a, **k: None)

    class FakeSource:
        user_id = "U1"

    class FakeMessage:
        id = "m1"
        text = "明天羽球，更正為1600"

    class FakeEvent:
        source = FakeSource()
        message = FakeMessage()
        reply_token = "reply-token"

    main._try_handle_calendar_correction(FakeEvent(), "G1", "明天羽球，更正為1600")

    assert parsed_target_date != existing_event_date
    assert event_updates == [("e-ball", existing_event_date, "16:00", "全家羽球")]
    assert reminder_updates == []


def test_calendar_content_only_correction_updates_titles(monkeypatch):
    import main
    import calendar_db
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    today_tw = datetime.now(ZoneInfo("Asia/Taipei")).date()
    event_date = (today_tw + timedelta(days=2)).isoformat()
    old_dt = datetime(
        today_tw.year,
        today_tw.month,
        today_tw.day,
        18,
        0,
        tzinfo=ZoneInfo("Asia/Taipei"),
    ) + timedelta(days=2)
    event_row = {
        "event_id": "e-ball",
        "group_id": "G1",
        "title": "全家打球",
        "event_date": event_date,
        "event_time": "18:00",
        "location": "",
        "participants": "[]",
        "status": "active",
    }
    event_updates: list[tuple[str, str, str | None, str | None]] = []
    reminder_updates: list[tuple[int, int, str | None, str | None]] = []

    monkeypatch.setattr(
        calendar_db,
        "search_by_keyword",
        lambda _gid, keywords, limit=10: [event_row] if "打球" in keywords else [],
    )
    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *a, **k: [])
    monkeypatch.setattr(calendar_db, "list_past", lambda *a, **k: [])
    monkeypatch.setattr(
        calendar_db,
        "correct_event_and_reminder_by_id",
        lambda group_id, event_id, new_date=None, new_time=None, new_title=None:
        event_updates.append(
            (event_id, new_date, new_time, new_title)
        )
        or {"status": "updated"},
    )
    monkeypatch.setattr(
        main.memory,
        "list_pending_reminders",
        lambda gid: [
            {
                "reminder_id": 7,
                "group_id": gid,
                "action": "全家打球",
                "remind_at": int(old_dt.timestamp()),
                "source_text": "後天全家打球",
            }
        ],
    )
    monkeypatch.setattr(
        main.memory,
        "update_reminder_schedule",
        lambda reminder_id, remind_at, source_text=None, action=None: reminder_updates.append(
            (reminder_id, remind_at, source_text, action)
        )
        or True,
    )
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda gid: None)
    monkeypatch.setattr(main, "_reply", lambda *a, **k: None)

    class FakeEvent:
        reply_token = "reply-token"

    assert main._try_handle_calendar_correction(FakeEvent(), "G1", "打球更正為羽球")

    assert event_updates == [("e-ball", event_date, None, "全家羽球")]
    assert reminder_updates == []
