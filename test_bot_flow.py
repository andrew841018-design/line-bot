#!/usr/bin/env python3
"""
test_bot_flow.py — LINE bot 核心流程離線測試

涵蓋：
  1. Pending：存入 / 讀取 / __bot__ 過濾
  2. Piggyback：格式 + pending 正確移除
  3. Quota state：load/save 往返一致

用法：
  python test_bot_flow.py        # 全部（離線，不呼叫 LLM API）
"""

import sys
import os
import json
import tempfile
import unittest.mock as mock
from pathlib import Path

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
# Test 1: pending JSON — 存入 / 讀取 / __bot__ 過濾
# ══════════════════════════════════════════════════════════════════════════════


def test_pending_flow():
    print("\n── Test 1: Pending JSON 基本流程 ──")
    import main
    import pending_store

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump({}, f)
        tmp_path = f.name

    orig_path = pending_store.PENDING_PATH
    orig_lock = pending_store.LOCK_PATH
    pending_store.PENDING_PATH = Path(tmp_path)
    pending_store.LOCK_PATH = Path(tmp_path + ".lock")
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
        pending_store.PENDING_PATH = orig_path
        pending_store.LOCK_PATH = orig_lock
        os.unlink(tmp_path)
        try:
            os.unlink(tmp_path + ".lock")
        except FileNotFoundError:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Test 2: Piggyback — 格式 + pending 正確移除
# ══════════════════════════════════════════════════════════════════════════════


def test_piggyback():
    print("\n── Test 2: Piggyback 格式與 pending 移除 ──")
    import main
    import pending_store

    gid = "G_PIG"
    fresh_ts = 9999999999.0
    pending_data = {
        gid: [
            {
                "user_id": "U1",
                "message_id": "p1",
                "type": "text",
                "text": "第一則測試訊息",
                "timestamp": fresh_ts,
            },
            {
                "user_id": "U2",
                "message_id": "p2",
                "type": "text",
                "text": "第二則測試訊息",
                "timestamp": fresh_ts,
            },
            {
                "user_id": "U3",
                "message_id": "p3",
                "type": "text",
                "text": "第三則測試訊息",
                "timestamp": fresh_ts,
            },
        ]
    }

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump(pending_data, f, ensure_ascii=False)
        tmp_path = f.name

    orig_path = pending_store.PENDING_PATH
    orig_lock = pending_store.LOCK_PATH
    pending_store.PENDING_PATH = Path(tmp_path)
    pending_store.LOCK_PATH = Path(tmp_path + ".lock")
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

        # piggyback 現在每次慢消化 1 則 text pending。
        remaining = main._load_pending_explicit()
        leftover = remaining.get(gid, [])
        check("pending 首則已移除", len(leftover) == 2, f"remaining={len(leftover)}")
    finally:
        pending_store.PENDING_PATH = orig_path
        pending_store.LOCK_PATH = orig_lock
        os.unlink(tmp_path)
        try:
            os.unlink(tmp_path + ".lock")
        except FileNotFoundError:
            pass

    # LLM 失敗時 pending 不動
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump(pending_data, f, ensure_ascii=False)
        tmp_path2 = f.name
    pending_store.PENDING_PATH = Path(tmp_path2)
    pending_store.LOCK_PATH = Path(tmp_path2 + ".lock")
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
        pending_store.PENDING_PATH = orig_path
        pending_store.LOCK_PATH = orig_lock
        os.unlink(tmp_path2)
        try:
            os.unlink(tmp_path2 + ".lock")
        except FileNotFoundError:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Test 4 & 5: Grok waterfall / fallback group format
# ── 已移除（2026-04-26 grok 移除為 stub；保留檔案僅讓 import 不爆）
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# Test 3: quota state load/save 往返
# ══════════════════════════════════════════════════════════════════════════════


def test_quota_state():
    print("\n── Test 3: Quota state load/save 往返 ──")
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

    test_pending_flow()
    test_piggyback()
    test_quota_state()

    print(f"\n{'=' * 50}")
    print(f"TOTAL: {PASS} passed, {FAIL} failed")
    print("=" * 50)
    if FAIL == 0:
        print("All tests passed!")
    else:
        print(f"{FAIL} test(s) FAILED.")
    sys.exit(0 if FAIL == 0 else 1)
