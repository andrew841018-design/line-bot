"""
test_drain_quota_exhaustion.py — Gemini quota 爆時用 reply_token piggyback bundle
drain pending 的測試。對應 main._try_piggyback_drain_with_reply_token +
_commit_pending_removal + _peek_text_pending_for_drain。

Covers (§3 codex + GP1 abbreviated chain findings):
  - Skip cases (no token / muted / no pending / drain lock contention)
  - Happy path: peek → render local LLM → bundle → reply_message → commit
  - Failure path: reply_message raise → pending 完整保留 (peek-then-confirm)
  - Deadline: 超時時不繼續 pop 新 item
  - _commit_pending_removal correctness (idempotent + atomic)
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "false")  # 必須 false 才會 send reply

import main  # noqa: E402

PASS = 0
FAIL = 0


@pytest.fixture(autouse=True)
def _enable_legacy_pending_reply(monkeypatch):
    monkeypatch.setattr(main, "_PENDING_REPLY_ENABLED", True)


def check(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}")


def _fake_pending(items_by_group: dict) -> None:
    """安裝 _load_pending_explicit / _save_pending_explicit_raw 的 in-memory fake。"""
    state = {"data": dict(items_by_group)}

    def fake_load():
        import copy
        return copy.deepcopy(state["data"])

    def fake_save(d):
        state["data"] = dict(d)

    return state, fake_load, fake_save


# ═══════════════════════════════════════════════════════════════════════════════
# Test A: skip cases
# ═══════════════════════════════════════════════════════════════════════════════

def test_skip_no_reply_token():
    print("\n── Test A1: skip when reply_token is None ──")
    with patch.object(main, "_load_pending_explicit") as mock_load:
        main._try_piggyback_drain_with_reply_token(None, "G1")
        check("沒 reply_token → 不呼叫 _load_pending_explicit", not mock_load.called)


def test_skip_no_group_id():
    print("\n── Test A2: skip when group_id empty ──")
    with patch.object(main, "_load_pending_explicit") as mock_load:
        main._try_piggyback_drain_with_reply_token("tok", "")
        check("沒 group_id → 不呼叫 _load_pending_explicit", not mock_load.called)


def test_skip_when_bot_muted():
    print("\n── Test A3: skip when bot_muted=True ──")
    with patch.object(main.settings, "bot_muted", True), \
         patch.object(main, "_load_pending_explicit") as mock_load:
        main._try_piggyback_drain_with_reply_token("tok", "G1")
        check("bot_muted=True → 不 load", not mock_load.called)


def test_skip_when_no_pending():
    print("\n── Test A4: skip when pending empty ──")
    state, fake_load, fake_save = _fake_pending({})
    with patch.object(main, "_load_pending_explicit", fake_load), \
         patch.object(main, "_save_pending_explicit_raw", fake_save), \
         patch.object(main, "_try_acquire_drain_slot") as mock_slot:
        main._try_piggyback_drain_with_reply_token("tok", "G1")
        check("pending 空 → 不嘗試 acquire drain slot", not mock_slot.called)


def test_skip_when_drain_lock_held():
    print("\n── Test A5: skip when drain lock contention ──")
    state, fake_load, fake_save = _fake_pending({
        "G1": [
            {"type": "text", "text": "hello", "message_id": "M1", "timestamp": time.time()},
        ]
    })
    with patch.object(main, "_load_pending_explicit", fake_load), \
         patch.object(main, "_save_pending_explicit_raw", fake_save), \
         patch.object(main, "_try_acquire_drain_slot", return_value=None), \
         patch.object(main, "_peek_text_pending_for_drain") as mock_peek:
        main._try_piggyback_drain_with_reply_token("tok", "G1")
        check("drain lock 拿不到 → 不 peek", not mock_peek.called)


# ═══════════════════════════════════════════════════════════════════════════════
# Test B: happy path — peek → reply → commit
# ═══════════════════════════════════════════════════════════════════════════════

def test_happy_path_drains_and_commits():
    print("\n── Test B: happy path drain + commit ──")
    state, fake_load, fake_save = _fake_pending({
        "G1": [
            {"type": "text", "text": "old msg 1", "message_id": "M1", "timestamp": time.time() - 3600},
            {"type": "text", "text": "old msg 2", "message_id": "M2", "timestamp": time.time() - 1800},
            {"type": "text", "text": "old msg 3", "message_id": "M3", "timestamp": time.time() - 900},
        ]
    })

    fake_slot = MagicMock()
    fake_slot.release = MagicMock()
    fake_api = MagicMock()
    fake_api_class = MagicMock(return_value=fake_api)

    with patch.object(main.settings, "bot_muted", False), \
         patch.object(main, "_load_pending_explicit", fake_load), \
         patch.object(main, "_save_pending_explicit_raw", fake_save), \
         patch.object(main, "_try_acquire_drain_slot", return_value=fake_slot), \
         patch.object(main, "_llm_chat", side_effect=["reply for M1", "reply for M2"]), \
         patch.object(main, "_get_persona_notes", return_value=[]), \
         patch("main.MessagingApi", fake_api_class), \
         patch("main.ApiClient"), \
         patch.object(main, "_get_line_config", return_value=MagicMock()), \
         patch.object(main.memory, "log_raw_message"):
        main._try_piggyback_drain_with_reply_token("tok-good", "G1")

    # reply_message 該被呼叫一次
    check("reply_message 呼叫一次", fake_api.reply_message.called)
    call_args = fake_api.reply_message.call_args
    req = call_args[0][0]
    n_msgs = len(req.messages)
    check(
        f"bundle 訊息數 = 2 drain only, no system notice (got {n_msgs})",
        n_msgs == 2,
    )
    # pending 該剩下 M3 (M1+M2 已 commit removed)
    remaining = state["data"].get("G1", [])
    remaining_ids = [it["message_id"] for it in remaining]
    check(f"pending 剩 1 條 M3 (got ids={remaining_ids})", remaining_ids == ["M3"])
    # drain slot 該被 release
    check("drain slot release 被呼叫", fake_slot.release.called)


# ═══════════════════════════════════════════════════════════════════════════════
# Test C: failure path — reply_message raise, pending preserved (peek-then-confirm)
# ═══════════════════════════════════════════════════════════════════════════════

def test_reply_api_failure_preserves_pending():
    print("\n── Test C: reply_message API fail → pending 完整保留 ──")
    state, fake_load, fake_save = _fake_pending({
        "G1": [
            {"type": "text", "text": "old 1", "message_id": "M1", "timestamp": time.time()},
            {"type": "text", "text": "old 2", "message_id": "M2", "timestamp": time.time()},
        ]
    })
    original_ids = [it["message_id"] for it in state["data"]["G1"]]

    fake_slot = MagicMock()
    fake_api = MagicMock()
    fake_api.reply_message.side_effect = Exception("reply_token expired")
    fake_api_class = MagicMock(return_value=fake_api)

    with patch.object(main.settings, "bot_muted", False), \
         patch.object(main, "_load_pending_explicit", fake_load), \
         patch.object(main, "_save_pending_explicit_raw", fake_save), \
         patch.object(main, "_try_acquire_drain_slot", return_value=fake_slot), \
         patch.object(main, "_llm_chat", return_value="rendered reply"), \
         patch.object(main, "_get_persona_notes", return_value=[]), \
         patch("main.MessagingApi", fake_api_class), \
         patch("main.ApiClient"), \
         patch.object(main, "_get_line_config", return_value=MagicMock()):
        main._try_piggyback_drain_with_reply_token("tok-expired", "G1")

    # pending 應該完全沒動
    final_ids = [it["message_id"] for it in state["data"].get("G1", [])]
    check(
        f"reply 失敗後 pending 完整保留 (got ids={final_ids}, expected={original_ids})",
        final_ids == original_ids,
    )
    check("drain slot 仍 release", fake_slot.release.called)


# ═══════════════════════════════════════════════════════════════════════════════
# Test D: deadline 強制中斷
# ═══════════════════════════════════════════════════════════════════════════════

def test_deadline_breaks_loop_early():
    print("\n── Test D: deadline 強制中斷 peek loop ──")
    state, fake_load, fake_save = _fake_pending({
        "G1": [
            {"type": "text", "text": f"old {i}", "message_id": f"M{i}", "timestamp": time.time()}
            for i in range(5)
        ]
    })

    def slow_llm(*args, **kwargs):
        time.sleep(0.5)  # 每次 0.5s
        return "ok"

    with patch.object(main, "_load_pending_explicit", fake_load), \
         patch.object(main, "_llm_chat", side_effect=slow_llm), \
         patch.object(main, "_get_persona_notes", return_value=[]):
        # deadline 0.6s → 應該只跑 1 個 (第 2 個前就超 deadline)
        results = main._peek_text_pending_for_drain("G1", max_count=5, deadline_sec=0.6)

    check(
        f"deadline 0.6s + LLM 0.5s/call → results ≤ 2 (got {len(results)})",
        len(results) <= 2,
    )


def test_deadline_with_fast_llm_returns_max_count():
    print("\n── Test D2: 快 LLM → 達到 max_count ──")
    state, fake_load, fake_save = _fake_pending({
        "G1": [
            {"type": "text", "text": f"old {i}", "message_id": f"M{i}", "timestamp": time.time()}
            for i in range(5)
        ]
    })
    with patch.object(main, "_load_pending_explicit", fake_load), \
         patch.object(main, "_llm_chat", return_value="fast ok"), \
         patch.object(main, "_get_persona_notes", return_value=[]):
        results = main._peek_text_pending_for_drain("G1", max_count=2, deadline_sec=30.0)
    check(f"max_count=2 達標 (got {len(results)})", len(results) == 2)


# ═══════════════════════════════════════════════════════════════════════════════
# Test E: _commit_pending_removal correctness
# ═══════════════════════════════════════════════════════════════════════════════

def test_commit_removal_removes_matching_ids():
    print("\n── Test E1: commit_removal 砍指定 msg_id ──")
    state, fake_load, fake_save = _fake_pending({
        "G1": [
            {"message_id": "A"},
            {"message_id": "B"},
            {"message_id": "C"},
        ]
    })
    with patch.object(main, "_load_pending_explicit", fake_load), \
         patch.object(main, "_save_pending_explicit_raw", fake_save):
        n = main._commit_pending_removal("G1", ["A", "C"])
    check(f"removed 2 (got {n})", n == 2)
    remaining = [it["message_id"] for it in state["data"]["G1"]]
    check(f"剩 [B] (got {remaining})", remaining == ["B"])


def test_commit_removal_empty_ids_noop():
    print("\n── Test E2: empty msg_ids → noop ──")
    with patch.object(main, "_load_pending_explicit") as mock_load:
        n = main._commit_pending_removal("G1", [])
    check("empty ids → 不 load", n == 0 and not mock_load.called)


def test_commit_removal_deletes_group_when_empty():
    print("\n── Test E3: 移除全部後 group key 也清掉 ──")
    state, fake_load, fake_save = _fake_pending({
        "G1": [{"message_id": "X"}],
        "G2": [{"message_id": "Y"}],
    })
    with patch.object(main, "_load_pending_explicit", fake_load), \
         patch.object(main, "_save_pending_explicit_raw", fake_save):
        main._commit_pending_removal("G1", ["X"])
    check("G1 整 key 被刪", "G1" not in state["data"])
    check("G2 不受影響", "G2" in state["data"])


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    test_skip_no_reply_token()
    test_skip_no_group_id()
    test_skip_when_bot_muted()
    test_skip_when_no_pending()
    test_skip_when_drain_lock_held()
    test_happy_path_drains_and_commits()
    test_reply_api_failure_preserves_pending()
    test_deadline_breaks_loop_early()
    test_deadline_with_fast_llm_returns_max_count()
    test_commit_removal_removes_matching_ids()
    test_commit_removal_empty_ids_noop()
    test_commit_removal_deletes_group_when_empty()

    print(f"\n{'=' * 50}")
    print(f"TOTAL: {PASS} passed, {FAIL} failed")
    print("=" * 50)
    if FAIL:
        print("Some tests FAILED.")
        sys.exit(1)
    else:
        print("All tests passed!")
