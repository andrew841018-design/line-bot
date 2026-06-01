"""test_food_command.py — _handle_food_command 三指令 + _handle_command route + GP1 C4 regression。"""
import food_db
import food_recipes
import main

G = "C_food_cmd_test"


def test_no_match_returns_none():
    # GP1 C4：非 food 指令必回 None，否則短路後面所有既有指令
    assert main._handle_food_command(G, "/help") is None
    assert main._handle_food_command(G, "/記住 abc") is None
    assert main._handle_food_command(G, "隨便聊天") is None
    assert main._handle_food_command(G, "/今天吃什麼") is None  # 不撞既有晚餐問句


def test_cook_command_empty_graceful():
    food_db.clear_group(G)
    out = main._handle_food_command(G, "/今晚煮什麼")
    assert out is not None and "還沒記錄" in out   # graceful-empty，不是 None/空


def test_cook_command_with_inventory():
    food_db.clear_group(G)
    food_db.insert_signal(G, "has_food", "虱目魚肚", source_msg_id="m1")
    out = main._handle_food_command(G, "/今晚煮什麼")
    assert "煎虱目魚肚" in out
    assert food_recipes.DISCLAIMER in out
    food_db.clear_group(G)


def test_shopping_command():
    food_db.clear_group(G)
    food_db.insert_signal(G, "wants_bought", "牛奶", source_msg_id="m1")
    out = main._handle_food_command(G, "/該買什麼")
    assert "牛奶" in out
    food_db.clear_group(G)


def test_inventory_command():
    food_db.clear_group(G)
    food_db.insert_signal(G, "has_food", "蛋", source_msg_id="m1")
    out = main._handle_food_command(G, "/家裡有什麼")
    assert "蛋" in out
    food_db.clear_group(G)


def test_handle_command_routes_to_food():
    # 整合：_handle_command 確實 route 到 food handler
    food_db.clear_group(G)
    food_db.insert_signal(G, "wants_bought", "蛋", source_msg_id="m1")
    out = main._handle_command(G, "/該買什麼")
    assert out is not None and "蛋" in out
    food_db.clear_group(G)


def test_existing_command_not_shadowed():
    # GP1 C4 regression：/help 仍走既有指令，沒被 food 委派短路
    out = main._handle_command(G, "/help")
    assert out is not None and "可用指令" in out
    assert "【飲食】" in out  # HELP 也更新了
