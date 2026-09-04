"""
test_food_extractor — 對 food_extractor.py 的單元測試。

涵蓋：
  - 各 kind 的 regex 萃取（正/反例，基於真實 raw_messages 樣本）
  - 食物 white-list + canonical 同義詞
  - 餐廳/品牌 black-list
  - sentence-level cancel cue
  - 疑問句 modal → confidence='low'
  - 第三人稱 subject 偵測 (「爸爸喜歡 X」)
  - store_and_query roundtrip via kg_triples (PK dedupe)
  - alias_from_user_id (user_aliases.json)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import food_extractor as fx  # noqa: E402
import knowledge_graph as kg  # noqa: E402


@pytest.fixture
def food_test_group():
    gid = f"FOOD_TEST_{int(time.time() * 1000)}"
    yield gid
    kg.clear_triples(gid)


# ── FOOD_RELATIONS exposed ──────────────────────────────────────────────────

def test_food_relations_constant():
    assert "wants_to_eat" in fx.FOOD_RELATIONS
    assert "wants_bought" in fx.FOOD_RELATIONS
    assert "bought" in fx.FOOD_RELATIONS
    assert "has_food" in fx.FOOD_RELATIONS
    assert "likes_food" in fx.FOOD_RELATIONS
    assert "dislikes_food" in fx.FOOD_RELATIONS
    assert "finished_food" in fx.FOOD_RELATIONS


# ── canonicalization ────────────────────────────────────────────────────────

def test_canonical_魚肉_to_魚():
    assert fx.canonical("魚肉") == "魚"


def test_canonical_雞肉_to_雞():
    assert fx.canonical("雞肉") == "雞"


def test_canonical_passthrough():
    assert fx.canonical("蘿蔔糕") == "蘿蔔糕"


def test_is_food_whitelist():
    assert fx.is_food("蘿蔔糕")
    assert fx.is_food("虱目魚丸")
    assert fx.is_food("粉腸")
    assert fx.is_food("粽子")
    assert fx.is_food("魚肉")  # canonical mapped to 魚
    assert fx.is_food("雞")
    assert fx.is_food("便當")
    assert fx.is_food("蛋")


def test_is_food_negative():
    assert not fx.is_food("和園")  # restaurant
    assert not fx.is_food("鬍鬚張")
    assert not fx.is_food("全聯")
    assert not fx.is_food("中華民國")
    assert not fx.is_food("")


# ── wants_to_eat ────────────────────────────────────────────────────────────

def test_wants_to_eat_basic():
    signals = fx.extract("我想吃蘿蔔糕", sender_alias="媽媽")
    kinds = [(s["kind"], s["food"]) for s in signals]
    assert ("wants_to_eat", "蘿蔔糕") in kinds


def test_wants_to_eat_real_sample_restaurant_not_extracted():
    """「想吃和園什麼菜？」— 和園是餐廳不算 food。"""
    signals = fx.extract("穎： 想吃和園什麼菜？", sender_alias="爸爸")
    foods = [s["food"] for s in signals]
    assert "和園" not in foods


# ── has_food ────────────────────────────────────────────────────────────────

def test_has_food_basic():
    signals = fx.extract("冰箱裡有蘿蔔糕", sender_alias="媽媽")
    assert any(s["kind"] == "has_food" and s["food"] == "蘿蔔糕" for s in signals)


def test_has_food_real_sample_粉腸():
    """「裡面還有粉腸」— 'X 還有 Y' pattern。"""
    signals = fx.extract("裡面還有粉腸", sender_alias="媽媽")
    assert any(s["kind"] == "has_food" and s["food"] == "粉腸" for s in signals)


# ── wants_bought ────────────────────────────────────────────────────────────

def test_wants_bought_basic():
    """「幫我再買上次一樣的正記消痔丸二瓶」(real sample) — 包含非食物採購項目。"""
    signals = fx.extract(
        "幫我再買上次一樣的正記消痔丸二瓶", sender_alias="爸爸"
    )
    assert any(s["kind"] == "wants_bought" and "消痔丸" in s["food"] for s in signals)


def test_wants_bought_question_modal_low_confidence():
    """「以後可以買虱目魚丸嗎」— 疑問 + 未來 → confidence='low'。"""
    signals = fx.extract("以後可以買虱目魚丸嗎", sender_alias="妹妹")
    wb = [s for s in signals if s["kind"] == "wants_bought"]
    assert wb, f"應該抽 wants_bought (low confidence)，got: {signals}"
    assert all(s["confidence"] == "low" for s in wb)


def test_wants_bought_multiple_items():
    """「這次回來可買虱目魚肚、好吃的粽子以及耐放的水果」— list-style 多 item。"""
    signals = fx.extract(
        "這次回來可買虱目魚肚、好吃的粽子以及耐放的水果", sender_alias="媽媽"
    )
    foods = {s["food"] for s in signals if s["kind"] == "wants_bought"}
    assert "虱目魚肚" in foods
    assert "粽子" in foods


# ── bought ──────────────────────────────────────────────────────────────────

def test_bought_basic():
    signals = fx.extract("今天買了蘿蔔糕", sender_alias="媽媽")
    assert any(s["kind"] == "bought" and s["food"] == "蘿蔔糕" for s in signals)


def test_bought_real_sample_秋刀魚():
    """「我在高雄買的秋刀魚，七條魚$100」"""
    signals = fx.extract(
        "我在高雄買的秋刀魚，七條魚$100", sender_alias="媽媽"
    )
    assert any(s["kind"] == "bought" and "秋刀魚" in s["food"] for s in signals)


# ── likes_food ──────────────────────────────────────────────────────────────

def test_likes_food_third_person_subject():
    """「爸爸喜歡買澱粉類的食物」— subject 應該是 爸爸（非 sender）。"""
    signals = fx.extract("爸爸喜歡買澱粉類的食物", sender_alias="媽媽")
    likes = [s for s in signals if s["kind"] == "likes_food"]
    assert any(s["subject"] == "爸爸" for s in likes), (
        f"expected subject='爸爸', got: {likes}"
    )


def test_likes_food_first_person_default_sender():
    signals = fx.extract("我喜歡吃蘿蔔糕", sender_alias="爸爸")
    assert any(
        s["kind"] == "likes_food" and s["food"] == "蘿蔔糕" and s["subject"] == "爸爸"
        for s in signals
    )


# ── dislikes_food ───────────────────────────────────────────────────────────

def test_dislikes_food_explicit():
    signals = fx.extract("我不喜歡吃虱目魚丸", sender_alias="妹妹")
    assert any(
        s["kind"] == "dislikes_food" and s["food"] == "虱目魚丸"
        for s in signals
    )


def test_dislikes_food_reason_only_does_not_flip_to_likes():
    """「虱目魚肚的魚刺好多」— 抱怨刺，不該誤判 likes。"""
    signals = fx.extract("虱目魚肚的魚刺好多", sender_alias="妹妹")
    assert all(s["kind"] != "likes_food" for s in signals)


# ── finished_food ───────────────────────────────────────────────────────────

def test_finished_food_basic():
    """「今晚虱目魚肚全光」"""
    signals = fx.extract("今晚虱目魚肚全光", sender_alias="爸爸")
    assert any(
        s["kind"] == "finished_food" and "虱目魚" in s["food"]
        for s in signals
    )


# ── 餐廳 / 品牌 black-list ──────────────────────────────────────────────────

def test_restaurant_not_extracted_as_want():
    """「今晚吃和園？還是鬍鬚張？」— 餐廳問句，不抽 wants_to_eat。"""
    signals = fx.extract("今晚吃和園？還是鬍鬚張？", sender_alias="爸爸")
    foods = [s["food"] for s in signals]
    assert "和園" not in foods
    assert "鬍鬚張" not in foods


def test_brand_not_extracted_as_food():
    signals = fx.extract("我去全聯買菜", sender_alias="媽媽")
    foods = [s["food"] for s in signals]
    assert "全聯" not in foods


# ── cancel cue (sentence-level) ─────────────────────────────────────────────

def test_cancel_cue_kills_wants_bought():
    """「本來我要再去買蘿蔔糕，可是穎說...就沒有再出去買了」— 取消 cue 跨字距 > 10。"""
    signals = fx.extract(
        "本來我要再去買蘿蔔糕，可是穎說妳食量很小，所以就沒有再出去買了",
        sender_alias="爸爸",
    )
    wb = [s for s in signals if s["kind"] == "wants_bought"]
    assert not wb, f"取消 cue 應該扣掉 wants_bought, got: {wb}"


def test_explicit_negation_kills_signal():
    signals = fx.extract("我不想吃蘿蔔糕", sender_alias="媽媽")
    wte = [s for s in signals if s["kind"] == "wants_to_eat"]
    assert not wte


# ── empty / edge cases ──────────────────────────────────────────────────────

def test_empty_input_returns_empty():
    assert fx.extract("", sender_alias="媽媽") == []
    assert fx.extract("  ", sender_alias="媽媽") == []


def test_pure_chitchat_no_food_returns_empty():
    """「天氣真好啊」— 無食物字。"""
    signals = fx.extract("天氣真好啊", sender_alias="媽媽")
    assert signals == []


def test_long_input_does_not_explode():
    """500 字以上應被截斷不爆 (catastrophic backtracking 防護)。"""
    long_text = "我想吃蘿蔔糕" + "啦啦啦" * 300
    signals = fx.extract(long_text, sender_alias="媽媽")
    assert isinstance(signals, list)


def test_url_not_treated_as_food():
    """訊息含 URL 不該被誤抓。"""
    signals = fx.extract(
        "https://example.com/recipe/luobogao 看這個", sender_alias="媽媽"
    )
    foods = [s["food"] for s in signals]
    # 不該因為 URL 含「luobogao」就抽
    assert all("://" not in f for f in foods)


# ── store + query roundtrip ─────────────────────────────────────────────────

def test_extract_and_store_writes_to_kg(food_test_group):
    n = fx.extract_and_store(
        food_test_group,
        sender_alias="媽媽",
        text="我想吃蘿蔔糕",
    )
    assert n >= 1
    rows = kg.query_triples(food_test_group, relation="wants_to_eat")
    assert any(r["object"] == "蘿蔔糕" for r in rows)


def test_extract_and_store_dedupes_via_pk(food_test_group):
    fx.extract_and_store(food_test_group, "媽媽", "我想吃蘿蔔糕")
    n2 = fx.extract_and_store(food_test_group, "媽媽", "我想吃蘿蔔糕")
    # PK = (group_id, subject, relation, object) 衝突 → IGNORE
    assert n2 == 0


def test_extract_and_store_records_source_text(food_test_group):
    fx.extract_and_store(food_test_group, "媽媽", "我想吃蘿蔔糕")
    rows = kg.query_triples(food_test_group, relation="wants_to_eat")
    assert rows
    assert "我想吃蘿蔔糕" in rows[0]["source_text"]


# ── alias_from_user_id ──────────────────────────────────────────────────────

def test_alias_lookup_from_user_id(tmp_path, monkeypatch):
    """Local aliases are read without committing real LINE identifiers."""
    user_id = "U" + "1" * 32
    aliases_path = tmp_path / "user_aliases.json"
    aliases_path.write_text('{"' + user_id + '": "妹妹"}\n', encoding="utf-8")
    monkeypatch.setattr(fx, "_ALIASES_PATH", aliases_path)
    monkeypatch.setattr(fx, "_ALIASES_CACHE", None)

    assert fx.alias_from_user_id(user_id) == "妹妹"


def test_alias_lookup_unknown_returns_empty():
    assert fx.alias_from_user_id("unknown_id_xxx") == ""
    assert fx.alias_from_user_id("") == ""


# ── extract_async fire-and-forget ───────────────────────────────────────────

def test_extract_async_does_not_raise(food_test_group):
    """async 介面失敗 silent。"""
    # 不該爆，即使 text 是 None
    fx.extract_async(food_test_group, "媽媽", "我想吃蘿蔔糕")
    # 給 thread 一點時間
    time.sleep(0.5)
    rows = kg.query_triples(food_test_group, relation="wants_to_eat")
    assert any(r["object"] == "蘿蔔糕" for r in rows)


def test_extract_async_empty_text_noop(food_test_group):
    fx.extract_async(food_test_group, "媽媽", "")
    time.sleep(0.1)
    rows = kg.query_triples(food_test_group)
    assert rows == []
