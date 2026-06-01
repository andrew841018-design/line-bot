"""test_food_db.py — family_food 表 CRUD + 「最新事件定狀態」+ dedup + 群組隔離。

對應 §3 review chain 決議：GP1 Critical 2（時序）、C5c（dedup）、GP2 A（家庭層級）/C（truncate）。
測試用獨立 group_id，每例先 clear。
"""
import time

import food_db as fdb

G = "C_food_db_test"
G2 = "C_food_db_test_2"


def _setup():
    fdb.clear_group(G)
    fdb.clear_group(G2)


def test_insert_and_inventory_basic():
    _setup()
    assert fdb.insert_signal(G, "has_food", "蛋", source_msg_id="m1") is True
    assert "蛋" in fdb.query_inventory(G)


def test_invalid_kind_rejected():
    _setup()
    assert fdb.insert_signal(G, "not_a_kind", "蛋", source_msg_id="m1") is False
    assert fdb.query_inventory(G) == []


def test_inventory_latest_event_wins():
    # GP1 Critical 2：買→吃完→又買，最新是 has → 在庫
    _setup()
    fdb.insert_signal(G, "has_food", "蛋", source_msg_id="m1", created_at_ms=1000)
    fdb.insert_signal(G, "finished_food", "蛋", source_msg_id="m2", created_at_ms=2000)
    assert "蛋" not in fdb.query_inventory(G)   # 最新是 finished
    fdb.insert_signal(G, "has_food", "蛋", source_msg_id="m3", created_at_ms=3000)
    assert "蛋" in fdb.query_inventory(G)        # 最新又是 has


def test_shopping_latest_event_wins():
    # GP1 Critical 2：上週買過蛋，本週又要買 → 本週 wants 不該被舊 bought 消掉
    _setup()
    fdb.insert_signal(G, "wants_bought", "蛋", source_msg_id="m1", created_at_ms=1000)
    fdb.insert_signal(G, "bought", "蛋", source_msg_id="m2", created_at_ms=2000)
    assert "蛋" not in fdb.query_shopping(G)     # 已買
    fdb.insert_signal(G, "wants_bought", "蛋", source_msg_id="m3", created_at_ms=3000)
    assert "蛋" in fdb.query_shopping(G)          # 又要買（最新 wants）


def test_dedup_same_msg():
    # GP1 C5c：同 msg_id+kind+food 重送 → 第二次 dedup；不同 msg_id 同 food 保留
    _setup()
    assert fdb.insert_signal(G, "has_food", "蛋", source_msg_id="m1") is True
    assert fdb.insert_signal(G, "has_food", "蛋", source_msg_id="m1") is False
    assert fdb.insert_signal(G, "has_food", "蛋", source_msg_id="m2") is True


def test_group_isolation():
    _setup()
    fdb.insert_signal(G, "has_food", "蛋", source_msg_id="m1")
    assert "蛋" in fdb.query_inventory(G)
    assert fdb.query_inventory(G2) == []


def test_prefs_latest_wins():
    _setup()
    fdb.insert_signal(G, "dislikes_food", "苦瓜", source_msg_id="m1", created_at_ms=1000)
    fdb.insert_signal(G, "likes_food", "苦瓜", source_msg_id="m2", created_at_ms=2000)
    prefs = fdb.query_prefs(G)
    assert "苦瓜" in prefs["likes"]
    assert "苦瓜" not in prefs["dislikes"]


def test_subject_always_household():
    # GP2 blocker A：v1 不存個人 subject
    _setup()
    fdb.insert_signal(G, "has_food", "蛋", source_msg_id="m1")
    with fdb._conn() as c:
        row = c.execute(
            "SELECT subject FROM family_food WHERE group_id=? AND source_msg_id='m1'",
            (G,),
        ).fetchone()
    assert row[0] == fdb.HOUSEHOLD_SUBJECT == "家庭"


def test_source_text_truncated():
    # GP2 C：source_text 截 500
    _setup()
    long = "蛋" * 600
    fdb.insert_signal(G, "has_food", "蛋", source_msg_id="m1", source_text=long)
    with fdb._conn() as c:
        row = c.execute(
            "SELECT source_text FROM family_food WHERE group_id=? AND source_msg_id='m1'",
            (G,),
        ).fetchone()
    assert row is not None and len(row[0]) == 500


def test_purge_older_than():
    _setup()
    old_ts = int(time.time() * 1000) - 100 * 86400 * 1000  # 100 天前
    fdb.insert_signal(G, "has_food", "蛋", source_msg_id="m1", created_at_ms=old_ts)
    fdb.insert_signal(G, "has_food", "魚", source_msg_id="m2")  # now
    deleted = fdb.purge_older_than(90)
    assert deleted >= 1
    inv = fdb.query_inventory(G)
    assert "魚" in inv and "蛋" not in inv


def test_wants_recent():
    _setup()
    fdb.insert_signal(G, "wants_to_eat", "滷肉飯", source_msg_id="m1", created_at_ms=1000)
    fdb.insert_signal(G, "wants_to_eat", "牛肉麵", source_msg_id="m2", created_at_ms=2000)
    wants = fdb.query_wants(G)
    assert "牛肉麵" in wants and "滷肉飯" in wants
    assert wants[0] == "牛肉麵"  # 最近優先
