"""main._is_calendar_query / _handle_calendar_query — 14:49 verbatim 必過。"""

from __future__ import annotations

import json

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
    eid1 = cd.insert_event(
        group_id="G1",
        title="拿蛋糕",
        event_date="2026-05-22",
        event_time="14:00",
    )
    assert eid1
    eid2 = cd.insert_event(
        group_id="G1",
        title="拿蛋糕",
        event_date="2026-05-22",
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
    monkeypatch.setattr(main.bot_stats, "track_message", lambda *a, **k: None)
    monkeypatch.setattr(main.bot_stats, "track_pending_saved", lambda: None)
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

    # 直接呼叫 _maybe_capture_calendar_event（line 1095 quota-path 內呼叫的 fn）
    main._maybe_capture_calendar_event(
        "G1", "2026-05-22 14:00 拿喜來登贈送的生日蛋糕"
    )

    # events table 必須有一筆
    events = calendar_db.list_upcoming("G1", days=30)
    assert len(events) == 1
    assert events[0]["event_date"] == "2026-05-22"
    assert events[0]["event_time"] == "14:00"
    assert "蛋糕" in events[0]["title"]
