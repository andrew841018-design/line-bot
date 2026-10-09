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


def test_lists_say_how_far_back_they_look():
    # 2026-10-07：沒人再提的東西會自己消失，清單要講清楚只算最近幾天
    food_db.clear_group(G)
    food_db.insert_signal(G, "wants_bought", "牛奶", source_msg_id="m1")
    food_db.insert_signal(G, "has_food", "蛋", source_msg_id="m2")
    window = f"最近 {food_db.FRESH_DAYS} 天"
    assert window in main._handle_food_command(G, "/該買什麼")
    assert window in main._handle_food_command(G, "/家裡有什麼")
    food_db.clear_group(G)


def test_dinner_never_asks_a_model(monkeypatch, tmp_path):
    # 2026-10-09：晚餐推薦改成只從查證清單挑店，模型不再寫店名和地址。
    import json
    from types import SimpleNamespace

    import dinner_places

    data = tmp_path / "places.json"
    data.write_text(json.dumps({"places": [{
        "name": "測試麵店", "address": "測試市測試區測試路1段1號", "cuisine": "麵食",
        "tags": ["麵食"], "verified_on": "2099-01-01",
        "sources": ["https://example.com/a", "https://example.org/b"],
    }]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(dinner_places, "DATA_PATH", data)
    monkeypatch.setattr(dinner_places, "_today", lambda: __import__("datetime").date(2099, 1, 2))
    monkeypatch.setattr(main, "_llm_chat", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("model")))
    sent = []
    monkeypatch.setattr(main, "_reply", lambda _tok, text, **_k: sent.append(text) or True)
    event = SimpleNamespace(reply_token="T", message=SimpleNamespace(text="今晚吃什麼？想吃麵"))

    main._handle_dinner_recommendation(event, G)

    assert len(sent) == 1 and "🍽 測試麵店\n📍 測試市測試區測試路1段1號" in sent[0]


def test_empty_lists_and_help_say_the_window():
    food_db.clear_group(G)
    window = f"最近 {food_db.FRESH_DAYS} 天"
    assert window in main._handle_food_command(G, "/該買什麼")
    assert window in main._handle_food_command(G, "/家裡有什麼")
    assert window in main._handle_food_command(G, "/今晚煮什麼")
    assert f"最近 {food_db.FRESH_DAYS} 天" in main._HELP_TEXT
