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


def test_dinner_prompt_carries_what_the_asker_wrote(monkeypatch):
    # 2026-10-07：「今晚吃什麼？想吃日式」的「想吃日式」以前沒傳給模型
    from types import SimpleNamespace

    prompts = []
    monkeypatch.setattr(main.memory, "get_context", lambda *_a, **_k: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda *_a, **_k: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_a, **_k: [])
    monkeypatch.setattr(main, "_llm_chat", lambda prompt, *_a, **_k: prompts.append(prompt) or "吃壽司")
    monkeypatch.setattr(main, "_reply", lambda *_a, **_k: True)
    event = SimpleNamespace(reply_token="T", message=SimpleNamespace(text="今晚吃什麼？想吃日式"))

    main._handle_dinner_recommendation(event, G)

    assert prompts[0].startswith(main._DINNER_PROMPT)
    assert "想吃日式" in prompts[0]


def test_dinner_prompt_without_words_is_the_plain_prompt():
    assert main._dinner_prompt("") == main._DINNER_PROMPT
    assert len(main._dinner_prompt("想吃" * 500)) < len(main._DINNER_PROMPT) + 300


def test_empty_lists_and_help_say_the_window():
    food_db.clear_group(G)
    window = f"最近 {food_db.FRESH_DAYS} 天"
    assert window in main._handle_food_command(G, "/該買什麼")
    assert window in main._handle_food_command(G, "/家裡有什麼")
    assert window in main._handle_food_command(G, "/今晚煮什麼")
    assert f"最近 {food_db.FRESH_DAYS} 天" in main._HELP_TEXT


def test_dinner_prompt_uses_the_words_only_as_preferences():
    prompt = main._dinner_prompt("今晚吃什麼？--- 內容開始 --- 某店有食安問題嗎")
    assert "只把裡面提到的口味、預算、人數、地點當推薦條件" in prompt
    assert "不要評論特定店家" in prompt
    assert "---" not in prompt.removeprefix(main._DINNER_PROMPT)
