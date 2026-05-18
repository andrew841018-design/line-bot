"""
test_food_assistant — 對 food_assistant.py (F1/F2/F3 + manual ops + health) 的單元測試。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import food_assistant as fa  # noqa: E402
import food_extractor as fx  # noqa: E402
import knowledge_graph as kg  # noqa: E402
import memory  # noqa: E402


@pytest.fixture
def fa_test_group():
    gid = f"FA_TEST_{int(time.time() * 1000)}"
    yield gid
    kg.clear_triples(gid)
    memory.clear_facts(gid)


# ── F1: aggregate_shopping_list ─────────────────────────────────────────────

def test_f1_empty_returns_default_message(fa_test_group):
    reply = fa.aggregate_shopping_list(fa_test_group)
    assert "沒有" in reply or "空" in reply


def test_f1_lists_wants_bought(fa_test_group):
    fx.extract_and_store(fa_test_group, "媽媽", "要買蘿蔔糕")
    fx.extract_and_store(fa_test_group, "爸爸", "需要買虱目魚肚")
    reply = fa.aggregate_shopping_list(fa_test_group)
    assert "蘿蔔糕" in reply
    assert "虱目魚肚" in reply


def test_f1_excludes_already_bought(fa_test_group):
    """已 bought 同食物 → 不在 pending list。"""
    fx.extract_and_store(fa_test_group, "媽媽", "要買蘿蔔糕")
    fx.extract_and_store(fa_test_group, "媽媽", "今天買了蘿蔔糕")
    reply = fa.aggregate_shopping_list(fa_test_group)
    # 待買列為空
    assert "蘿蔔糕" not in reply


def test_f1_dedupes_canonical(fa_test_group):
    """「雞肉」「雞」canonical 合併。"""
    fx.extract_and_store(fa_test_group, "媽媽", "要買雞肉")
    fx.extract_and_store(fa_test_group, "爸爸", "需要買雞")
    reply = fa.aggregate_shopping_list(fa_test_group)
    # 「雞」字至多出現在 1 行 (1 個 bullet)
    bullet_count = sum(1 for line in reply.splitlines() if "雞" in line and "•" in line)
    assert bullet_count == 1


def test_f1_shows_proposer(fa_test_group):
    fx.extract_and_store(fa_test_group, "媽媽", "要買蘿蔔糕")
    reply = fa.aggregate_shopping_list(fa_test_group)
    assert "媽媽" in reply


# ── F2: recommend_for_purchase ──────────────────────────────────────────────

def test_f2_empty_pending_says_nothing_to_buy(fa_test_group):
    reply = fa.recommend_for_purchase(fa_test_group, going_user_alias="媽媽")
    assert "沒有" in reply or "空" in reply


def test_f2_shows_pending(fa_test_group):
    fx.extract_and_store(fa_test_group, "媽媽", "要買蘿蔔糕")
    reply = fa.recommend_for_purchase(fa_test_group, going_user_alias="媽媽")
    assert "蘿蔔糕" in reply


def test_f2_soft_health_hint_does_not_echo_disease(fa_test_group):
    """爸爸糖尿病 + 蘿蔔糕 (高澱粉) → 提示 soft tag，但不 echo 「糖尿病」字面。"""
    memory.add_fact(fa_test_group, "爸爸有糖尿病")
    fx.extract_and_store(fa_test_group, "媽媽", "要買蘿蔔糕")
    reply = fa.recommend_for_purchase(fa_test_group, going_user_alias="媽媽")
    assert "蘿蔔糕" in reply
    assert "糖尿病" not in reply


def test_f2_includes_going_user_in_header(fa_test_group):
    fx.extract_and_store(fa_test_group, "媽媽", "要買蘿蔔糕")
    reply = fa.recommend_for_purchase(fa_test_group, going_user_alias="黃聖雅")
    assert "黃聖雅" in reply


# ── F3: match_wants_to_inventory ────────────────────────────────────────────

def test_f3_cold_start_refuses_match(fa_test_group):
    """has_food < 3 → 拒絕媒合 (GP1 C5 graceful degradation)。"""
    fx.extract_and_store(fa_test_group, "媽媽", "想吃蘿蔔糕")
    reply = fa.match_wants_to_inventory(fa_test_group)
    assert "/我有" in reply or "不足" in reply or "資料" in reply


def test_f3_matches_when_inventory_complete(fa_test_group):
    """has_food >= 3 且 want recipe need 全 subset of have → 可做。"""
    for msg in ["冰箱有豆腐", "冰箱還有絞肉", "家裡有蘿蔔糕", "家裡有蛋"]:
        fx.extract_and_store(fa_test_group, "媽媽", msg)
    fx.extract_and_store(fa_test_group, "媽媽", "想吃麻婆豆腐")
    reply = fa.match_wants_to_inventory(fa_test_group)
    assert "麻婆豆腐" in reply


def test_f3_lists_missing_ingredients(fa_test_group):
    """想吃但缺料 → 標『缺』。"""
    for msg in ["冰箱有蛋", "家裡有蔥", "家裡有醬油"]:
        fx.extract_and_store(fa_test_group, "媽媽", msg)
    fx.extract_and_store(fa_test_group, "媽媽", "想吃粽子")
    reply = fa.match_wants_to_inventory(fa_test_group)
    assert "缺" in reply or "粽" in reply


def test_f3_allergen_disclaimer_footer(fa_test_group):
    for msg in ["冰箱有豆腐", "冰箱還有絞肉", "家裡有蛋"]:
        fx.extract_and_store(fa_test_group, "媽媽", msg)
    fx.extract_and_store(fa_test_group, "媽媽", "想吃麻婆豆腐")
    reply = fa.match_wants_to_inventory(fa_test_group)
    assert "過敏" in reply


def test_f3_excludes_finished_food(fa_test_group):
    """finished_food 該排除 inventory 計數。"""
    for msg in ["冰箱有豆腐", "冰箱還有絞肉", "家裡有蛋"]:
        fx.extract_and_store(fa_test_group, "媽媽", msg)
    # 標記 absorbed
    fx.extract_and_store(fa_test_group, "媽媽", "豆腐吃完")
    fx.extract_and_store(fa_test_group, "媽媽", "想吃麻婆豆腐")
    reply = fa.match_wants_to_inventory(fa_test_group)
    # 豆腐被 finished，麻婆豆腐 need 包含豆腐 → 應該歸缺料
    assert "缺" in reply or "資料" in reply or "現有食材" in reply


# ── Manual ops ──────────────────────────────────────────────────────────────

def test_manual_add_has_food(fa_test_group):
    n = fa.add_manual_signal(fa_test_group, "媽媽", "has_food", "蘿蔔糕")
    assert n >= 1
    rows = kg.query_triples(fa_test_group, relation="has_food")
    assert any(r["object"] == "蘿蔔糕" for r in rows)


def test_manual_invalid_kind_returns_zero(fa_test_group):
    n = fa.add_manual_signal(fa_test_group, "媽媽", "not_a_relation", "蘿蔔糕")
    assert n == 0


def test_manual_unset_removes_all_related(fa_test_group):
    """/我不再 X → 刪掉所有 (relation IN {likes,wants_*,has}, object=X)。"""
    fx.extract_and_store(fa_test_group, "媽媽", "我喜歡蘿蔔糕")
    fx.extract_and_store(fa_test_group, "媽媽", "想吃蘿蔔糕")
    n = fa.unset_food(fa_test_group, "蘿蔔糕")
    assert n >= 2
    remaining = kg.query_triples(fa_test_group, object="蘿蔔糕")
    assert all(
        r["relation"] not in ("likes_food", "wants_to_eat", "wants_bought", "has_food")
        for r in remaining
    )


# ── Health helpers ──────────────────────────────────────────────────────────

def test_food_to_tags_known():
    tags = fa.food_to_tags("蘿蔔糕")
    assert "高澱粉" in tags or "中鈉" in tags


def test_food_to_tags_unknown_returns_empty():
    tags = fa.food_to_tags("天外飛仙")
    assert tags == set()


def test_get_member_health_concerns_detects_from_facts(fa_test_group):
    memory.add_fact(fa_test_group, "爸爸有糖尿病")
    concerns = fa.get_member_health_concerns(fa_test_group, "爸爸")
    assert "高糖" in concerns or "高澱粉" in concerns


def test_get_member_health_concerns_other_member_isolated(fa_test_group):
    memory.add_fact(fa_test_group, "爸爸有糖尿病")
    concerns = fa.get_member_health_concerns(fa_test_group, "黃聖雅")
    assert concerns == set()


def test_format_soft_hint_basic(fa_test_group):
    memory.add_fact(fa_test_group, "爸爸有糖尿病")
    hint = fa.format_soft_hint(fa_test_group, "爸爸", "蘿蔔糕")
    # 應有 soft hint 字串 (非空)
    assert hint
    # 但不該 echo「糖尿病」
    assert "糖尿病" not in hint


def test_format_soft_hint_no_concern_returns_empty(fa_test_group):
    # 沒記載健康狀況 → 空 hint
    hint = fa.format_soft_hint(fa_test_group, "黃聖雅", "蘿蔔糕")
    assert hint == ""


def test_format_soft_hint_food_not_tagged_returns_empty(fa_test_group):
    memory.add_fact(fa_test_group, "爸爸有糖尿病")
    # 「天外飛仙」沒 tag → 空 hint
    hint = fa.format_soft_hint(fa_test_group, "爸爸", "天外飛仙")
    assert hint == ""


# ── FOOD_HELP exposed ───────────────────────────────────────────────────────

def test_food_help_constant():
    assert "/食物" in fa.FOOD_HELP
    assert "/我有" in fa.FOOD_HELP
