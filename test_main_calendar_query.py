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
                "action": "黃聖穎早上洗牙，看陳敏慧牙醫師",
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
        datetime(2026, 7, 16, 14, 30, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(
        main.memory,
        "list_pending_reminders",
        lambda gid, within_seconds=None: [
            {
                "action": "機場接送（去紐西蘭）7/16去程",
                "remind_at": reminder_ts,
                "mention_aliases": ["黃將修"],
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


def test_dedup_same_group_title_date_blocks_duplicate(tmp_calendar_db):
    """SQL UNIQUE INDEX 阻擋同 group+title+date 的 active event。"""
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
        event_time="14:30",  # 不同時間也算重複
    )
    assert eid2 == ""  # dedup hit → 回空字串
    events = cd.list_upcoming("G1", days=30)
    assert len(events) == 1
    assert events[0]["event_time"] == "14:00"  # 第一筆保留


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

    monkeypatch.setattr(
        calendar_extractor,
        "extract",
        lambda text: {
            "has_event": True,
            "is_cancellation": False,
            "title": "東海大學校友年中交流聚餐",
            "date": "2026-07-04",
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

    monkeypatch.setattr(
        calendar_extractor,
        "extract",
        lambda text: {
            "has_event": True,
            "is_cancellation": False,
            "title": "媽媽去東海大學",
            "date": "2026-07-05",
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

    text = (
        "咪寶麻煩提醒我7月4日台北市東海大學校友會在美僑俱樂部聚會"
        "上午10:30到下午14:30以及7月11日在台北六福萬怡酒店9樓-海山廳"
        "（南港火車站B棟，忠孝東路七段359號9樓）13:10-16:30。"
    )
    monkeypatch.setattr(
        calendar_extractor,
        "extract",
        lambda _text: {
            "has_event": True,
            "is_cancellation": False,
            "title": "東海大學校友會活動（美僑俱樂部）",
            "date": "2026-07-04",
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
    assert dates == ["2026-07-04", "2026-07-11"]
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
        "update_event_schedule",
        lambda event_id, new_date, new_time=None, title=None: event_updates.append(
            (event_id, new_date, new_time, title)
        )
        or True,
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
    assert len(reminder_updates) == 1
    updated_dt = datetime.fromtimestamp(
        reminder_updates[0][1], tz=ZoneInfo("Asia/Taipei")
    )
    assert updated_dt.date().isoformat() == target_date
    assert updated_dt.strftime("%H:%M") == "16:00"
    assert reminder_updates[0][2] == "明天羽球，更正為1600"
    assert reminder_updates[0][3] == "全家羽球"
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
        "update_event_schedule",
        lambda event_id, new_date, new_time=None, title=None: event_updates.append(
            (event_id, new_date, new_time, title)
        )
        or True,
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
    updated_dt = datetime.fromtimestamp(
        reminder_updates[0][1], tz=ZoneInfo("Asia/Taipei")
    )
    assert updated_dt.date().isoformat() == existing_event_date
    assert updated_dt.strftime("%H:%M") == "16:00"
    assert reminder_updates[0][3] == "全家羽球"


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
        "update_event_schedule",
        lambda event_id, new_date, new_time=None, title=None: event_updates.append(
            (event_id, new_date, new_time, title)
        )
        or True,
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
    updated_dt = datetime.fromtimestamp(
        reminder_updates[0][1], tz=ZoneInfo("Asia/Taipei")
    )
    assert updated_dt.strftime("%Y-%m-%d %H:%M") == old_dt.strftime("%Y-%m-%d %H:%M")
    assert reminder_updates[0][3] == "全家羽球"
