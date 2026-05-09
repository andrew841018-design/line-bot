"""
test_knowledge_graph — 對 knowledge_graph.py 的單元測試。

涵蓋：
  - extract_triples 規則 patterns
  - store_triples / query_triples SQLite round-trip
  - merge_synonyms 同義詞合併
  - auto_extract_kg_async fire-and-forget
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import knowledge_graph as kg  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# extract_triples — 規則 patterns
# ─────────────────────────────────────────────────────────────────────────────

def test_kin_attribute_birthday():
    """「我老婆生日是 5/14」 → (user, spouse_birthday, 5/14)"""
    triples = kg.extract_triples("我老婆生日是 5/14")
    assert any(
        t[0] == "user" and "spouse" in t[1] and "birthday" in t[1] and "5/14" in t[2]
        for t in triples
    ), f"Expected spouse_birthday triple, got: {triples}"


def test_user_work_at_district():
    """「我在台北市信義區工作」 → (user, work_at, 台北市信義區)"""
    triples = kg.extract_triples("我在台北市信義區工作")
    assert any(
        t[0] == "user" and t[1] == "work_at" and ("信義" in t[2] or "台北" in t[2])
        for t in triples
    ), f"Expected work_at triple, got: {triples}"


def test_user_like_food():
    """「我喜歡吃拉麵」 → (user, like, 吃拉麵) 或 (user, like, 拉麵)"""
    triples = kg.extract_triples("我喜歡吃拉麵")
    assert any(
        t[0] == "user" and t[1] in ("like", "like_food") and "拉麵" in t[2]
        for t in triples
    ), f"Expected like triple, got: {triples}"


def test_user_has_pet():
    """「我有一隻貓」 → (user, has, 貓)"""
    triples = kg.extract_triples("我有一隻貓")
    assert any(
        t[0] == "user" and t[1] == "has" and "貓" in t[2] for t in triples
    ), f"Expected has triple, got: {triples}"


def test_user_birthday():
    """「我生日是 12/25」 → (user, birthday, 12/25)"""
    triples = kg.extract_triples("我生日是 12/25")
    assert any(
        t[0] == "user" and t[1] == "birthday" and "12/25" in t[2] for t in triples
    ), f"Expected user birthday, got: {triples}"


def test_user_live_at():
    """「我住在新北市」 → (user, live_at, 新北市)"""
    triples = kg.extract_triples("我住在新北市")
    assert any(
        t[0] == "user" and t[1] == "live_at" and "新北" in t[2] for t in triples
    ), f"Expected live_at triple, got: {triples}"


def test_kin_child_name():
    """「我兒子的名字叫小明」 → (user, son_name, 小明)"""
    triples = kg.extract_triples("我兒子的名字叫小明")
    assert any(
        t[0] == "user" and "son" in t[1] and "name" in t[1] and "小明" in t[2]
        for t in triples
    ), f"Expected son_name, got: {triples}"


def test_user_dislike():
    """「我討厭下雨」 → (user, dislike, 下雨)"""
    triples = kg.extract_triples("我討厭下雨")
    assert any(
        t[0] == "user" and t[1] == "dislike" and "下雨" in t[2] for t in triples
    ), f"Expected dislike, got: {triples}"


def test_empty_returns_empty_list():
    assert kg.extract_triples("") == []
    assert kg.extract_triples("   ") == []
    assert kg.extract_triples("好") == []


def test_no_match_random_text():
    """無語意句不該硬抽出三元組。"""
    triples = kg.extract_triples("天氣真好啊")
    # 可能 0 條 — 「天氣」不是專名所以「天氣是好」不會中
    assert all(t[0] != "user" for t in triples) or len(triples) == 0


def test_user_phone():
    """「我的電話是 0912345678」 → (user, phone, 0912345678)"""
    triples = kg.extract_triples("我的電話是 0912345678")
    assert any(
        t[0] == "user" and t[1] == "phone" and "0912345678" in t[2] for t in triples
    ), f"Expected phone, got: {triples}"


def test_user_company():
    """「我公司是 Google」 → (user, company, Google)"""
    triples = kg.extract_triples("我公司是 Google")
    assert any(
        t[0] == "user" and t[1] == "company" and "Google" in t[2] for t in triples
    ), f"Expected company, got: {triples}"


def test_dedupe_within_extract():
    """同一句重複觸發同條規則時應 dedupe。"""
    triples = kg.extract_triples("我有一隻貓我有一隻貓")
    # 應該只有 1 條 (user, has, 貓)，不是 2 條
    has_triples = [t for t in triples if t[1] == "has"]
    assert len(has_triples) <= 1


# ─────────────────────────────────────────────────────────────────────────────
# merge_synonyms
# ─────────────────────────────────────────────────────────────────────────────

def test_merge_synonyms_basic():
    assert kg.merge_synonyms("老婆") == "spouse"
    assert kg.merge_synonyms("太太") == "spouse"
    assert kg.merge_synonyms("妻子") == "spouse"


def test_merge_synonyms_passthrough():
    """非同義詞表內的詞原樣回傳。"""
    assert kg.merge_synonyms("Andrew") == "Andrew"
    assert kg.merge_synonyms("拉麵") == "拉麵"


# ─────────────────────────────────────────────────────────────────────────────
# store_triples / query_triples — SQLite round-trip
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def kg_test_group():
    """每個 test 用獨立 group_id；結束清掉。"""
    gid = f"KG_TEST_{int(time.time() * 1000)}"
    yield gid
    kg.clear_triples(gid)


def test_store_and_query_roundtrip(kg_test_group):
    triples = [("user", "birthday", "5/14"), ("user", "spouse_birthday", "1/1")]
    added = kg.store_triples(kg_test_group, triples, source_text="test")
    assert added == 2
    rows = kg.query_triples(kg_test_group)
    assert len(rows) == 2
    keys = {(r["subject"], r["relation"], r["object"]) for r in rows}
    assert ("user", "birthday", "5/14") in keys
    assert ("user", "spouse_birthday", "1/1") in keys


def test_store_dedupe_on_pk_conflict(kg_test_group):
    """同 (group, s, r, o) 二次寫入應該被 IGNORE。"""
    triples = [("user", "name", "Andrew")]
    assert kg.store_triples(kg_test_group, triples) == 1
    assert kg.store_triples(kg_test_group, triples) == 0  # dedupe


def test_query_filter_by_subject(kg_test_group):
    kg.store_triples(kg_test_group, [
        ("user", "name", "Andrew"),
        ("Bob", "is_a", "工程師"),
    ])
    rows = kg.query_triples(kg_test_group, subject="user")
    assert len(rows) == 1
    assert rows[0]["object"] == "Andrew"


def test_query_filter_by_relation(kg_test_group):
    kg.store_triples(kg_test_group, [
        ("user", "name", "Andrew"),
        ("user", "phone", "0912345678"),
    ])
    rows = kg.query_triples(kg_test_group, relation="phone")
    assert len(rows) == 1
    assert rows[0]["object"] == "0912345678"


# ─────────────────────────────────────────────────────────────────────────────
# auto_extract_kg_async — fire-and-forget
# ─────────────────────────────────────────────────────────────────────────────

def test_auto_extract_kg_async(kg_test_group):
    """背景跑完後應該可以從 SQLite 查到三元組。"""
    kg.auto_extract_kg_async(kg_test_group, "我喜歡吃拉麵")
    # 等 thread 跑完
    time.sleep(0.3)
    rows = kg.query_triples(kg_test_group)
    assert len(rows) >= 1, f"async extract didn't store anything: {rows}"
    assert any("拉麵" in r["object"] for r in rows)


def test_auto_extract_async_empty_no_crash():
    """空字串不該爆。"""
    kg.auto_extract_kg_async("any_group", "")
    kg.auto_extract_kg_async("", "我喜歡貓")
    # 不該丟例外即通過


def test_full_flow_with_multiple_patterns(kg_test_group):
    """一句多 pattern：「我老婆生日是 5/14，我喜歡吃拉麵」"""
    text = "我老婆生日是 5/14，我喜歡吃拉麵"
    triples = kg.extract_triples(text)
    assert len(triples) >= 2
    kg.store_triples(kg_test_group, triples, source_text=text)
    rows = kg.query_triples(kg_test_group)
    relations = {r["relation"] for r in rows}
    assert any("birthday" in r for r in relations)
    assert any("like" in r for r in relations)
