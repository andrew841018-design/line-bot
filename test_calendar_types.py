"""Plan C event_type tests — 8 core (advisor 列的 must-have)。"""

from __future__ import annotations

import importlib
import json
from datetime import date, timedelta

import pytest


# ── 共用 fixture：tmp DB ─────────────────────────────────────────────────
@pytest.fixture
def tmp_cal_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_cal_types.db"
    monkeypatch.setenv("SQLITE_PATH", str(db_file))
    import config

    importlib.reload(config)
    config.settings.sqlite_path = str(db_file)
    import calendar_db

    importlib.reload(calendar_db)
    return calendar_db


# ── Test 1: Migration idempotency (init_db twice doesn't crash) ─────────
def test_migration_idempotent(tmp_cal_db):
    """init_db 重複跑不會爆，event_type column 仍然存在。"""
    # init_db 已在 fixture import 時跑過一次
    tmp_cal_db.init_db()  # 第二次
    tmp_cal_db.init_db()  # 第三次
    # PRAGMA 驗欄位
    with tmp_cal_db._conn() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()]
    assert "event_type" in cols


# ── Test 2: 「媽媽什麼時候回台北」no-date keyword search ⭐ ────────────
def test_no_date_search_mom_taipei(tmp_cal_db):
    """Demo query — DB 有 row → search_by_keyword 找到 → reply 包含該 event。"""
    import main

    GID = "G1"
    tmp_cal_db.insert_event(
        group_id=GID,
        title="媽媽回台北",
        event_date="2026-05-18",
        location=None,
        event_type="personal_trip",
    )

    assert main._is_calendar_query("媽媽什麼時候回台北") is True
    assert main._resolve_relative_date("媽媽什麼時候回台北") is None
    hits = tmp_cal_db.search_by_keyword(GID, ["媽媽", "台北"], limit=5)
    assert len(hits) == 1
    assert hits[0]["title"] == "媽媽回台北"
    assert hits[0]["event_type"] == "personal_trip"


# ── Test 3: 「什麼時候做胃鏡」no-date medical search ⭐ ──────────────────
def test_no_date_search_endoscopy(tmp_cal_db):
    import main

    GID = "G1"
    tmp_cal_db.insert_event(
        group_id=GID,
        title="媽媽做胃鏡",
        event_date="2026-05-19",
        event_time="09:00",
        location="好心肝",
        event_type="medical",
    )
    assert main._is_calendar_query("什麼時候做胃鏡") is True
    hits = tmp_cal_db.search_by_keyword(GID, ["胃鏡"], limit=5)
    assert len(hits) == 1
    assert hits[0]["event_type"] == "medical"


# ── Test 4: 「回家吃飯」stays family_gathering (not personal_trip) ──────
def test_classify_huijia_chifan_family():
    """personal_trip 「回X」list 不含 bare「家」→ 回家吃飯保持 family_gathering。"""
    import calendar_regex

    assert calendar_regex._classify_type("回家吃飯") == "family_gathering"
    assert calendar_regex._classify_type("全家回老家") == "personal_trip"
    assert calendar_regex._classify_type("媽媽回台北") == "personal_trip"


# ── Test 5: 「陪媽媽看醫生」 medical wins (priority test) ───────────────
def test_classify_priority_medical_wins():
    """multi-match 字串 medical > personal_trip > family_gathering。"""
    import calendar_regex

    # 「陪媽媽看醫生」既有「媽媽」(family) 又有「看醫生」(medical)
    # medical 優先序高 → 應回 medical
    assert calendar_regex._classify_type("陪媽媽看醫生") == "medical"
    # 「全家做健檢」既有「全家」(family) 又有「做健檢」(medical) → medical
    assert calendar_regex._classify_type("全家做健檢") == "medical"


# ── Test 6: 「你想什麼時候去日本」no-event-stored → safe empty reply ───
def test_empty_search_safe_reply(tmp_cal_db):
    """無 stored event 時 search_by_keyword 回 [] — handler 不 crash。"""
    import main

    GID = "G1"
    hits = tmp_cal_db.search_by_keyword(GID, ["日本"], limit=5)
    assert hits == []


# ── Test 7: SQL injection / LIKE wildcard escape ────────────────────────
def test_search_by_keyword_escape_underscore(tmp_cal_db):
    """keyword 含 `_` 必須被 escape，不會 match 任意一字。"""
    GID = "G1"
    tmp_cal_db.insert_event(group_id=GID, title="蛋糕", event_date="2026-06-01")
    tmp_cal_db.insert_event(group_id=GID, title="蛋_糕", event_date="2026-06-02")

    # 搜「蛋_糕」應該只找「蛋_糕」，不該因 _ 變萬用字元 match「蛋糕」
    hits = tmp_cal_db.search_by_keyword(GID, ["蛋_糕"], limit=5)
    titles = [h["title"] for h in hits]
    assert "蛋_糕" in titles
    # 也驗 % escape：搜「100%」不該爆


def test_search_by_keyword_escape_percent(tmp_cal_db):
    GID = "G1"
    tmp_cal_db.insert_event(group_id=GID, title="100% 達成", event_date="2026-06-01")
    # 不該爆，且能找到
    hits = tmp_cal_db.search_by_keyword(GID, ["100%"], limit=5)
    assert any("100" in h["title"] for h in hits)


# ── Test 8: Cancelled events excluded from search ───────────────────────
def test_cancelled_excluded_from_search(tmp_cal_db):
    GID = "G1"
    eid = tmp_cal_db.insert_event(
        group_id=GID, title="媽媽回台北", event_date="2026-05-18",
        event_type="personal_trip",
    )
    assert eid
    tmp_cal_db.cancel_event(eid)
    hits = tmp_cal_db.search_by_keyword(GID, ["台北"], limit=5)
    assert hits == []


# ── 額外：list_past + search ordering (future ASC + past DESC) ──────────
def test_search_ordering_future_first(tmp_cal_db):
    """search_by_keyword 應該未來 ASC 在前，過去 DESC 在後（GP1 反饋）。"""
    GID = "G1"
    today = date.today()
    future_far = (today + timedelta(days=30)).isoformat()
    future_near = (today + timedelta(days=5)).isoformat()
    past_recent = (today - timedelta(days=5)).isoformat()
    past_old = (today - timedelta(days=30)).isoformat()

    tmp_cal_db.insert_event(group_id=GID, title="未來遠回台北", event_date=future_far, event_type="personal_trip")
    tmp_cal_db.insert_event(group_id=GID, title="未來近回台北", event_date=future_near, event_type="personal_trip")
    tmp_cal_db.insert_event(group_id=GID, title="過去近回台北", event_date=past_recent, event_type="personal_trip")
    tmp_cal_db.insert_event(group_id=GID, title="過去遠回台北", event_date=past_old, event_type="personal_trip")

    hits = tmp_cal_db.search_by_keyword(GID, ["回台北"], limit=10)
    titles = [h["title"] for h in hits]
    # 順序：未來近 → 未來遠 → 過去近 → 過去遠
    assert titles == ["未來近回台北", "未來遠回台北", "過去近回台北", "過去遠回台北"]


# ── 額外：regex extract event_type plumbing ────────────────────────────
def test_regex_extract_sets_event_type():
    """calendar_regex.extract_regex_only 結果必須含正確 event_type。"""
    import calendar_regex
    from datetime import date as _d

    today = _d(2026, 5, 21)
    out = calendar_regex.extract_regex_only(
        "2026-05-22 14:00 拿喜來登贈送的生日蛋糕", today
    )
    assert out["has_event"] is True
    assert out["event_type"] == "family_gathering"  # 蛋糕/喜來登 → family

    out = calendar_regex.extract_regex_only(
        "明天 09:00 媽媽做胃鏡", today
    )
    assert out["has_event"] is True
    assert out["event_type"] == "medical"

    out = calendar_regex.extract_regex_only(
        "後天 14:00 媽媽回台北", today
    )
    assert out["has_event"] is True
    assert out["event_type"] == "personal_trip"

    # 不認三類 → reject
    out = calendar_regex.extract_regex_only(
        "明天 14:00 開週會",
        today,
    )
    assert out["has_event"] is False


# ── 額外：past date keyword resolve ─────────────────────────────────────
def test_resolve_past_date_keywords():
    import main
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today_tw = datetime.now(ZoneInfo("Asia/Taipei")).date()
    assert main._resolve_relative_date("昨天有什麼安排") == today_tw - timedelta(days=1)
    assert main._resolve_relative_date("前天有事嗎") == today_tw - timedelta(days=2)
    # 「5 天前」
    assert main._resolve_relative_date("5天前的行程") == today_tw - timedelta(days=5)
