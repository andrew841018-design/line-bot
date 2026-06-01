"""test_food_push.py — build_message：graceful-empty + 可煮 + 補貨 + 不點名。"""
import food_db
import food_push
import food_recipes

G = "C_food_push_test"


def test_build_message_empty(monkeypatch):
    monkeypatch.setattr(food_push, "GROUP_ID", G)
    food_db.clear_group(G)
    assert food_push.build_message() == ""   # 無料不推（graceful-empty）


def test_build_message_cook(monkeypatch):
    monkeypatch.setattr(food_push, "GROUP_ID", G)
    food_db.clear_group(G)
    food_db.insert_signal(G, "has_food", "蛋", source_msg_id="m1")
    msg = food_push.build_message()
    assert "蛋花湯" in msg
    assert food_recipes.DISCLAIMER in msg
    food_db.clear_group(G)


def test_build_message_shopping(monkeypatch):
    monkeypatch.setattr(food_push, "GROUP_ID", G)
    food_db.clear_group(G)
    food_db.insert_signal(G, "wants_bought", "牛奶", source_msg_id="m1")
    msg = food_push.build_message()
    assert "補貨" in msg and "牛奶" in msg
    food_db.clear_group(G)


def test_no_personal_naming(monkeypatch):
    # GP2 A：推播不得點名任何家庭成員
    monkeypatch.setattr(food_push, "GROUP_ID", G)
    food_db.clear_group(G)
    food_db.insert_signal(G, "has_food", "蛋", source_msg_id="m1")
    food_db.insert_signal(G, "has_food", "虱目魚肚", source_msg_id="m2")
    msg = food_push.build_message()
    for name in ("媽媽", "爸爸", "黃聖雅", "黃聖穎"):
        assert name not in msg
    food_db.clear_group(G)
