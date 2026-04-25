#!/usr/bin/env python3
"""
test_bot_flow.py — LINE bot 核心流程離線測試

涵蓋：
  1. bot_stats：訊息分類 + 計數器
  2. Pending：存入 / 讀取 / __bot__ 過濾
  3. Piggyback：格式 + pending 正確移除
  4. _llm_chat：Gemini→Grok waterfall
  5. Grok grouping：fallback 格式驗證
  6. Quota state：load/save 往返一致

用法：
  python test_bot_flow.py        # 全部（離線，不呼叫 LLM API）
"""

import sys, os, json, tempfile, types, unittest.mock as mock

sys.path.insert(0, os.path.dirname(__file__))

PASS = FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


# ══════════════════════════════════════════════════════════════════════════════
# Test 1: bot_stats — classify_message + increment
# ══════════════════════════════════════════════════════════════════════════════


def test_bot_stats():
    print("\n── Test 1: bot_stats 訊息分類 + 計數器 ──")
    import bot_stats

    cases = [
        ("https://youtube.com/shorts/abc", "url"),
        ("真的假的？這是謠言嗎", "fact_check"),
        ("台積電今天漲停", "finance"),
        ("我頭痛要看醫生", "health"),
        ("民進黨選舉最新消息", "political"),
        ("記者報導指出", "news"),
        ("你覺得這樣對嗎？", "question"),
        ("哈哈", "casual"),
        ("[圖片]", "media"),
    ]
    for text, expected in cases:
        got = bot_stats.classify_message(text)
        check(f"classify '{text[:20]}' → {expected}", got == expected, f"got={got}")

    # increment + query_range 往返
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_db = f.name
    orig = bot_stats._DB_PATH
    bot_stats._DB_PATH = tmp_db
    try:
        bot_stats.increment("msg_received", 3, date="2099-01-01")
        bot_stats.increment("msg_received", 2, date="2099-01-01")
        rows = bot_stats.query_range(30)
        day = next((r for r in rows if r["date"] == "2099-01-01"), None)
        check(
            "increment 累加正確",
            day is not None and day.get("msg_received") == 5,
            f"got={day}",
        )
    finally:
        bot_stats._DB_PATH = orig
        os.unlink(tmp_db)


# ══════════════════════════════════════════════════════════════════════════════
# Test 2: pending JSON — 存入 / 讀取 / __bot__ 過濾
# ══════════════════════════════════════════════════════════════════════════════


def test_pending_flow():
    print("\n── Test 2: Pending JSON 基本流程 ──")
    import main

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump({}, f)
        tmp_path = f.name

    orig = main._PENDING_EXPLICIT_PATH
    main._PENDING_EXPLICIT_PATH = tmp_path
    try:
        gid = "G_TEST"

        # 寫入 3 則 user + 1 則 bot
        data = {
            gid: [
                {
                    "user_id": "U1",
                    "message_id": "m1",
                    "type": "text",
                    "text": "hello",
                    "timestamp": 1.0,
                },
                {
                    "user_id": "U2",
                    "message_id": "m2",
                    "type": "text",
                    "text": "world",
                    "timestamp": 2.0,
                },
                {
                    "user_id": "__bot__",
                    "message_id": "m3",
                    "type": "text",
                    "text": "hi",
                    "timestamp": 3.0,
                },
                {
                    "user_id": "U3",
                    "message_id": "m4",
                    "type": "text",
                    "text": "bye",
                    "timestamp": 4.0,
                },
            ]
        }
        main._save_pending_explicit_raw(data)

        loaded = main._load_pending_explicit()
        items = loaded.get(gid, [])
        check("pending 讀取 4 則", len(items) == 4)

        # 過濾 __bot__（模擬啟動時的 filter）
        filtered = [it for it in items if it.get("user_id") != "__bot__"]
        check("__bot__ 過濾後剩 3 則", len(filtered) == 3)
        check(
            "__bot__ 條目不在 filtered",
            all(it["user_id"] != "__bot__" for it in filtered),
        )
    finally:
        main._PENDING_EXPLICIT_PATH = orig
        os.unlink(tmp_path)


# ══════════════════════════════════════════════════════════════════════════════
# Test 3: Piggyback — 格式 + pending 正確移除
# ══════════════════════════════════════════════════════════════════════════════


def test_piggyback():
    print("\n── Test 3: Piggyback 格式與 pending 移除 ──")
    import main

    gid = "G_PIG"
    pending_data = {
        gid: [
            {
                "user_id": "U1",
                "message_id": "p1",
                "type": "text",
                "text": "第一則測試訊息",
                "timestamp": 1.0,
            },
            {
                "user_id": "U2",
                "message_id": "p2",
                "type": "text",
                "text": "第二則測試訊息",
                "timestamp": 2.0,
            },
            {
                "user_id": "U3",
                "message_id": "p3",
                "type": "text",
                "text": "第三則測試訊息",
                "timestamp": 3.0,
            },
        ]
    }

    saved = {}

    def fake_save(data):
        saved.update(data)

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump(pending_data, f, ensure_ascii=False)
        tmp_path = f.name

    orig_path = main._PENDING_EXPLICIT_PATH
    main._PENDING_EXPLICIT_PATH = tmp_path
    try:
        with (
            mock.patch("main._llm_chat", return_value="這是測試回覆內容"),
            mock.patch("main._get_persona_notes", return_value=[]),
            mock.patch("main.memory") as mock_mem,
        ):
            mock_mem.top_facts.return_value = []
            mock_mem.get_context.return_value = []
            result = main._pop_pending_for_piggyback(gid)

        check("piggyback 回傳非 None", result is not None)
        check("格式含「📬」", result is not None and "📬" in result)
        check("格式含「原文：」", result is not None and "原文：" in result)
        check("格式含「回應：」", result is not None and "回應：" in result)
        check("原文含第一則內容", result is not None and "第一則測試訊息" in result)
        check("回應含 LLM 輸出", result is not None and "這是測試回覆內容" in result)

        # pending 應該減少 3 則（全部被處理）
        remaining = main._load_pending_explicit()
        leftover = remaining.get(gid, [])
        check("pending 3 則已移除", len(leftover) == 0, f"remaining={len(leftover)}")
    finally:
        main._PENDING_EXPLICIT_PATH = orig_path
        os.unlink(tmp_path)

    # LLM 失敗時 pending 不動
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump(pending_data, f, ensure_ascii=False)
        tmp_path2 = f.name
    main._PENDING_EXPLICIT_PATH = tmp_path2
    try:
        with (
            mock.patch("main._llm_chat", return_value=""),
            mock.patch("main._get_persona_notes", return_value=[]),
            mock.patch("main.memory") as mock_mem,
        ):
            mock_mem.top_facts.return_value = []
            mock_mem.get_context.return_value = []
            result2 = main._pop_pending_for_piggyback(gid)

        check("LLM 失敗時回 None", result2 is None)
        remaining2 = main._load_pending_explicit()
        check("LLM 失敗時 pending 不動", len(remaining2.get(gid, [])) == 3)
    finally:
        main._PENDING_EXPLICIT_PATH = orig_path
        os.unlink(tmp_path2)


# ══════════════════════════════════════════════════════════════════════════════
# Test 4: _llm_chat waterfall — Gemini → Grok
# ══════════════════════════════════════════════════════════════════════════════


def test_llm_chat_waterfall():
    print("\n── Test 4: _llm_chat Gemini→Grok waterfall ──")
    import main

    # Gemini 有量 → 用 Gemini
    with (
        mock.patch("main._quota_exhausted", return_value=False),
        mock.patch("main.gemini_client") as mg,
        mock.patch("main.grok_client") as mk,
    ):
        mg.chat.return_value = "gemini reply"
        mk.chat.return_value = "grok reply"
        mk.quota_exhausted.return_value = False
        result = main._llm_chat("hi", [], [])
    check("Gemini 有量 → 用 Gemini", result == "gemini reply")
    check("Gemini 有量 → Grok 未被呼叫", mk.chat.call_count == 0)

    # Gemini 沒量 → fallback Grok
    with (
        mock.patch("main._quota_exhausted", return_value=True),
        mock.patch("main.gemini_client") as mg2,
        mock.patch("main.grok_client") as mk2,
        mock.patch("main.bot_stats"),
    ):
        mg2.chat.return_value = ""
        mk2.chat.return_value = "grok reply"
        mk2.quota_exhausted.return_value = False
        from config import settings

        orig_key = settings.grok_api_key
        settings.grok_api_key = "fake-key"
        try:
            result2 = main._llm_chat("hi", [], [])
        finally:
            settings.grok_api_key = orig_key
    check("Gemini 沒量 → fallback Grok", result2 == "grok reply")

    # 兩個都沒量 → 回空字串
    with (
        mock.patch("main._quota_exhausted", return_value=True),
        mock.patch("main.grok_client") as mk3,
        mock.patch("main.bot_stats"),
    ):
        mk3.quota_exhausted.return_value = True
        mk3.chat.return_value = ""
        from config import settings

        orig_key = settings.grok_api_key
        settings.grok_api_key = "fake-key"
        try:
            result3 = main._llm_chat("hi", [], [])
        finally:
            settings.grok_api_key = orig_key
    check("兩個都沒量 → 回空字串", result3 == "")


# ══════════════════════════════════════════════════════════════════════════════
# Test 5: Grok group_messages fallback 格式
# ══════════════════════════════════════════════════════════════════════════════


def test_grok_group_format():
    print("\n── Test 5: Grok group_messages fallback 格式 ──")
    import grok_client

    items = [
        {
            "user_id": "U1",
            "message_id": "g1",
            "type": "text",
            "text": "投資問題",
            "timestamp": 1.0,
        },
        {
            "user_id": "U2",
            "message_id": "g2",
            "type": "text",
            "text": "台積電漲了",
            "timestamp": 2.0,
        },
        {
            "user_id": "U1",
            "message_id": "g3",
            "type": "text",
            "text": "今天天氣",
            "timestamp": 100.0,
        },
    ]

    fake_resp = mock.MagicMock()
    fake_resp.choices[0].message.content = json.dumps(
        {
            "groups": [
                {"idxs": [0, 1], "reply_to": 1},
                {"idxs": [2], "reply_to": 2},
            ]
        }
    )

    with (
        mock.patch.object(grok_client, "_get_client") as mc,
        mock.patch.object(grok_client, "settings") as ms,
    ):
        ms.grok_api_key = "fake"
        ms.grok_model = "grok-3-mini"
        mc.return_value.chat.completions.create.return_value = fake_resp

        result = grok_client.group_messages(items)

    check("回傳非 None", result is not None)
    check("分成 2 組", result is not None and len(result) == 2)
    check(
        "每個索引恰好出現一次",
        result is not None
        and sorted(sum([g["idxs"] for g in result], [])) == [0, 1, 2],
    )
    check(
        "reply_to 在各組 idxs 內",
        result is not None and all(g["reply_to"] in g["idxs"] for g in result),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Test 6: quota state load/save 往返
# ══════════════════════════════════════════════════════════════════════════════


def test_quota_state():
    print("\n── Test 6: Quota state load/save 往返 ──")
    import main

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump({}, f)
        tmp = f.name

    orig = main._QUOTA_STATE_FILE
    main._QUOTA_STATE_FILE = tmp
    try:
        main._quota_exhausted_until_ts = 9999999999.0
        main._quota_notified_for_ts = 1234567890.0
        main._save_quota_state()

        main._quota_exhausted_until_ts = 0.0
        main._quota_notified_for_ts = 0.0
        main._load_quota_state()

        check(
            "exhausted_until_ts 往返正確",
            main._quota_exhausted_until_ts == 9999999999.0,
        )
        check("notified_for_ts 往返正確", main._quota_notified_for_ts == 1234567890.0)
    finally:
        main._QUOTA_STATE_FILE = orig
        os.unlink(tmp)


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.WARNING)

    test_bot_stats()
    test_pending_flow()
    test_piggyback()
    test_llm_chat_waterfall()
    test_grok_group_format()
    test_quota_state()

    print(f"\n{'=' * 50}")
    print(f"TOTAL: {PASS} passed, {FAIL} failed")
    print("=" * 50)
    if FAIL == 0:
        print("All tests passed!")
    else:
        print(f"{FAIL} test(s) FAILED.")
    sys.exit(0 if FAIL == 0 else 1)
