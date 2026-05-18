"""finance_view_db CRUD 測試。

conftest.py 已 isolate finance_view_db._DB_PATH，test 自動跑在 tmp DB。
"""

from __future__ import annotations

import finance_view_db


def test_insert_and_list_recent():
    vid = finance_view_db.insert_view(
        group_id="G1",
        source_msg_id="M1",
        user_id="U1",
        display_name="媽媽",
        raw_text="0050 會漲到 180",
        symbol_type="ticker",
        ticker="0050.TW",
        macro_topic=None,
        direction="bull",
        time_frame="mid",
        horizon_days=90,
        target_price=180.0,
        target_pct=None,
        confidence="high",
        condition_text=None,
        expires_at="2026-08-15",
    )
    assert isinstance(vid, str) and len(vid) > 0

    rows = finance_view_db.list_recent("G1", limit=10)
    found = next((r for r in rows if r["view_id"] == vid), None)
    assert found is not None
    assert found["ticker"] == "0050.TW"
    assert found["direction"] == "bull"
    assert found["target_price"] == 180.0
    assert found["validation_result"] == "pending"
    assert found["status"] == "active"


def test_list_by_person_filter():
    finance_view_db.insert_view(
        group_id="G2", source_msg_id=None, user_id="U_M",
        display_name="媽媽", raw_text="0050 漲",
        symbol_type="ticker", ticker="0050.TW", macro_topic=None,
        direction="bull", time_frame="short", horizon_days=30,
        target_price=None, target_pct=None, confidence=None,
        condition_text=None, expires_at=None,
    )
    finance_view_db.insert_view(
        group_id="G2", source_msg_id=None, user_id="U_P",
        display_name="爸爸", raw_text="2330 跌",
        symbol_type="ticker", ticker="2330.TW", macro_topic=None,
        direction="bear", time_frame="short", horizon_days=30,
        target_price=None, target_pct=None, confidence=None,
        condition_text=None, expires_at=None,
    )
    mama = finance_view_db.list_by_person("G2", "媽媽")
    papa = finance_view_db.list_by_person("G2", "爸爸")
    assert mama and all(r["display_name"] == "媽媽" for r in mama)
    assert papa and all(r["display_name"] == "爸爸" for r in papa)


def test_list_by_ticker_filter():
    finance_view_db.insert_view(
        group_id="G3", source_msg_id=None, user_id="U",
        display_name="媽媽", raw_text="A",
        symbol_type="ticker", ticker="0050.TW", macro_topic=None,
        direction="bull", time_frame="short", horizon_days=30,
        target_price=None, target_pct=None, confidence=None,
        condition_text=None, expires_at=None,
    )
    finance_view_db.insert_view(
        group_id="G3", source_msg_id=None, user_id="U",
        display_name="媽媽", raw_text="B",
        symbol_type="ticker", ticker="2330.TW", macro_topic=None,
        direction="bear", time_frame="short", horizon_days=30,
        target_price=None, target_pct=None, confidence=None,
        condition_text=None, expires_at=None,
    )
    rows = finance_view_db.list_by_ticker("G3", "0050.TW")
    assert rows and all(r["ticker"] == "0050.TW" for r in rows)


def test_update_validation():
    vid = finance_view_db.insert_view(
        group_id="G4", source_msg_id=None, user_id="U",
        display_name="媽媽", raw_text="X",
        symbol_type="ticker", ticker="0050.TW", macro_topic=None,
        direction="bull", time_frame="short", horizon_days=30,
        target_price=180.0, target_pct=None, confidence=None,
        condition_text=None, expires_at="2026-08-15",
    )
    finance_view_db.update_validation(
        vid, "hit", "目標達成", price_start=170.0, price_end=185.0
    )
    rows = finance_view_db.list_recent("G4", limit=1)
    assert rows[0]["validation_result"] == "hit"
    assert rows[0]["validation_detail"] == "目標達成"
    assert rows[0]["validated_price_start"] == 170.0
    assert rows[0]["validated_price_end"] == 185.0


def test_count_by_result_aggregation():
    # 3 個觀點：1 hit / 1 miss / 1 pending
    for direction, result in [("bull", "hit"), ("bear", "miss"), ("neutral", None)]:
        vid = finance_view_db.insert_view(
            group_id="G5", source_msg_id=None, user_id="U",
            display_name="爸爸", raw_text="x",
            symbol_type="ticker", ticker="0050.TW", macro_topic=None,
            direction=direction, time_frame="short", horizon_days=30,
            target_price=None, target_pct=None, confidence=None,
            condition_text=None, expires_at=None,
        )
        if result:
            finance_view_db.update_validation(vid, result, "y")
    counts = finance_view_db.count_by_result("G5")
    assert counts["hit"] == 1
    assert counts["miss"] == 1
    assert counts["pending"] == 1


def test_list_pending_validation_filters_expired_only():
    # active+pending+expired → 取
    finance_view_db.insert_view(
        group_id="G6", source_msg_id=None, user_id="U",
        display_name="媽媽", raw_text="past",
        symbol_type="ticker", ticker="0050.TW", macro_topic=None,
        direction="bull", time_frame="short", horizon_days=30,
        target_price=None, target_pct=None, confidence=None,
        condition_text=None, expires_at="2025-01-01",  # 過期
    )
    # active+pending+future → 不取
    finance_view_db.insert_view(
        group_id="G6", source_msg_id=None, user_id="U",
        display_name="媽媽", raw_text="future",
        symbol_type="ticker", ticker="0050.TW", macro_topic=None,
        direction="bull", time_frame="long", horizon_days=180,
        target_price=None, target_pct=None, confidence=None,
        condition_text=None, expires_at="2099-01-01",
    )
    pending = finance_view_db.list_pending_validation("2026-05-18")
    raws = {r["raw_text"] for r in pending}
    assert "past" in raws
    assert "future" not in raws
