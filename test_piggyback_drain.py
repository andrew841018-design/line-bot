"""Unit tests for piggyback pending drain triggered by webhook messages.

Targets _piggyback_drain_pending / _spawn_piggyback_drain in main.py.
External dependencies (LINE API, Gemini, pending store mutation) are mocked —
only throttle + gate + drain-lock flow under test.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import main
import pending_store


def _seed_pending(group_id: str) -> None:
    pending_store.save_full(
        {
            group_id: [
                {
                    "type": "text",
                    "message_id": "m1",
                    "user_id": "u1",
                    "timestamp": time.time(),
                    "text": "hi",
                }
            ]
        }
    )


def _reset_piggyback_state() -> None:
    with main._piggyback_drain_lock:
        main._last_piggyback_drain_ts.clear()
    with main._drain_lock_factory_lock:
        main._drain_locks.clear()
    with main._global_gate_cache_lock:
        main._global_gate_cache = None


def test_piggyback_skips_when_no_pending_for_group(monkeypatch):
    _reset_piggyback_state()
    monkeypatch.setattr(main.settings, "bot_muted", False)
    with patch.object(main, "_drain_pending_for_group", return_value=True) as drain, \
         patch.object(main, "_global_pending_drain_ready", return_value=True), \
         patch.object(main, "_has_enough_quota_for_retry", return_value=True):
        main._piggyback_drain_pending("G_no_pending")
    drain.assert_not_called()
    assert "G_no_pending" not in main._last_piggyback_drain_ts, \
        "empty pending 不該寫 throttle ts (GP1 critical #1)"


def test_piggyback_throttles_within_window_after_successful_drain(monkeypatch):
    _reset_piggyback_state()
    monkeypatch.setattr(main.settings, "bot_muted", False)
    _seed_pending("G1")
    with patch.object(main, "_drain_pending_for_group", return_value=True) as drain, \
         patch.object(main, "_global_pending_drain_ready", return_value=True), \
         patch.object(main, "_has_enough_quota_for_retry", return_value=True):
        main._piggyback_drain_pending("G1")
        main._piggyback_drain_pending("G1")
        main._piggyback_drain_pending("G1")
    assert drain.call_count == 1
    drain.assert_called_with("G1", source="piggyback")


def test_piggyback_runs_again_after_throttle_window(monkeypatch):
    _reset_piggyback_state()
    monkeypatch.setattr(main.settings, "bot_muted", False)
    _seed_pending("G2")
    with patch.object(main, "_drain_pending_for_group", return_value=True) as drain, \
         patch.object(main, "_global_pending_drain_ready", return_value=True), \
         patch.object(main, "_has_enough_quota_for_retry", return_value=True):
        main._piggyback_drain_pending("G2")
        with main._piggyback_drain_lock:
            main._last_piggyback_drain_ts["G2"] = (
                time.time() - main._PIGGYBACK_DRAIN_THROTTLE_SEC - 10
            )
        main._piggyback_drain_pending("G2")
    assert drain.call_count == 2


def test_piggyback_skips_when_global_gate_blocks(monkeypatch):
    _reset_piggyback_state()
    monkeypatch.setattr(main.settings, "bot_muted", False)
    _seed_pending("G3")
    with patch.object(main, "_drain_pending_for_group", return_value=True) as drain, \
         patch.object(main, "_global_pending_drain_ready", return_value=False), \
         patch.object(main, "_has_enough_quota_for_retry", return_value=True):
        main._piggyback_drain_pending("G3")
    drain.assert_not_called()
    assert "G3" not in main._last_piggyback_drain_ts, \
        "global gate fail 不該寫 throttle ts (GP1 critical #1)"


def test_piggyback_skips_when_retry_quota_low(monkeypatch):
    _reset_piggyback_state()
    monkeypatch.setattr(main.settings, "bot_muted", False)
    _seed_pending("G4")
    with patch.object(main, "_drain_pending_for_group", return_value=True) as drain, \
         patch.object(main, "_global_pending_drain_ready", return_value=True), \
         patch.object(main, "_has_enough_quota_for_retry", return_value=False):
        main._piggyback_drain_pending("G4")
    drain.assert_not_called()
    assert "G4" not in main._last_piggyback_drain_ts, \
        "retry quota gate fail 不該寫 throttle ts"


def test_piggyback_per_group_throttle_is_independent(monkeypatch):
    _reset_piggyback_state()
    monkeypatch.setattr(main.settings, "bot_muted", False)
    pending_store.save_full(
        {
            "G_A": [{"type": "text", "message_id": "a", "user_id": "u",
                     "timestamp": time.time(), "text": "x"}],
            "G_B": [{"type": "text", "message_id": "b", "user_id": "u",
                     "timestamp": time.time(), "text": "y"}],
        }
    )
    with patch.object(main, "_drain_pending_for_group", return_value=True) as drain, \
         patch.object(main, "_global_pending_drain_ready", return_value=True), \
         patch.object(main, "_has_enough_quota_for_retry", return_value=True):
        main._piggyback_drain_pending("G_A")
        main._piggyback_drain_pending("G_B")
        main._piggyback_drain_pending("G_A")
    assert drain.call_count == 2
    called_groups = {c.args[0] for c in drain.call_args_list}
    assert called_groups == {"G_A", "G_B"}


def test_piggyback_skips_when_drain_lock_busy(monkeypatch):
    """另一 caller（retry worker / startup）正在 drain 同 group → piggyback 跳過 + 不寫 ts."""
    _reset_piggyback_state()
    monkeypatch.setattr(main.settings, "bot_muted", False)
    _seed_pending("G_busy")
    # 預先把該 group 的 drain slot 拿走，模擬 retry worker 在跑
    busy_slot = main._try_acquire_drain_slot("G_busy")
    assert busy_slot is not None
    try:
        with patch.object(main, "_global_pending_drain_ready", return_value=True), \
             patch.object(main, "_has_enough_quota_for_retry", return_value=True):
            # piggyback 拿不到 slot → 內部 _drain_pending_for_group 回 False → ts 不寫
            main._piggyback_drain_pending("G_busy")
    finally:
        busy_slot.release()
    assert "G_busy" not in main._last_piggyback_drain_ts, \
        "drain lock conflict 不該寫 throttle ts (GP1 important #3)"


def test_drain_pending_for_group_returns_false_when_locked():
    """_drain_pending_for_group 同 group 第二 caller 拿不到 lock 應該回 False."""
    _reset_piggyback_state()
    pending_store.save_full({})  # 沒 pending 也測得到 lock 行為
    busy_slot = main._try_acquire_drain_slot("G_lock")
    assert busy_slot is not None
    try:
        result = main._drain_pending_for_group("G_lock", source="piggyback")
        assert result is False
    finally:
        busy_slot.release()


def test_drain_pending_for_group_releases_lock_after_run(monkeypatch):
    """drain 完 lock 必須 release，下次 caller 拿得到."""
    _reset_piggyback_state()
    monkeypatch.setattr(main.settings, "bot_muted", False)
    pending_store.save_full({})  # 空 pending → drain 內部走 `if not items: return True`
    result1 = main._drain_pending_for_group("G_release", source="startup")
    assert result1 is True
    # 同一 group 再 acquire 應該成功
    slot = main._try_acquire_drain_slot("G_release")
    assert slot is not None, "lock 應該已 release"
    slot.release()


def test_spawn_piggyback_drain_uses_executor_not_blocking(monkeypatch):
    """_spawn_piggyback_drain 不該阻塞 caller — 透過 executor submit."""
    _reset_piggyback_state()
    monkeypatch.setattr(main.settings, "bot_muted", False)
    _seed_pending("G_spawn")
    done = threading.Event()

    def _slow_drain(*args, **kwargs):
        done.wait(timeout=2.0)
        return True

    with patch.object(main, "_drain_pending_for_group", side_effect=_slow_drain), \
         patch.object(main, "_global_pending_drain_ready", return_value=True), \
         patch.object(main, "_has_enough_quota_for_retry", return_value=True):
        t0 = time.time()
        main._spawn_piggyback_drain("G_spawn")
        elapsed = time.time() - t0
        assert elapsed < 0.5, f"_spawn_piggyback_drain blocked for {elapsed:.2f}s"
        done.set()


def test_spawn_piggyback_drain_skips_empty_group_id():
    _reset_piggyback_state()
    with patch.object(main._PIGGYBACK_EXECUTOR, "submit") as submit:
        main._spawn_piggyback_drain("")
        main._spawn_piggyback_drain(None)  # type: ignore[arg-type]
    submit.assert_not_called()


def test_spawn_piggyback_fast_path_bails_when_throttled():
    """30 分鐘冷卻內 _spawn_piggyback_drain 不該 submit 到 executor."""
    _reset_piggyback_state()
    with main._piggyback_drain_lock:
        main._last_piggyback_drain_ts["G_cool"] = time.time()
    with patch.object(main._PIGGYBACK_EXECUTOR, "submit") as submit:
        main._spawn_piggyback_drain("G_cool")
    submit.assert_not_called()


def test_global_gate_cache_returns_cached_result():
    """同 60s 視窗內第二次呼叫應該直接拿 cache，不重打 LINE API."""
    _reset_piggyback_state()
    # 預先把 cache 設為 (now, True)，模擬上次有 check 過
    with main._global_gate_cache_lock:
        main._global_gate_cache = (time.time(), True)
    with patch("line_token_refresh.get_line_token") as get_tok:
        result = main._global_pending_drain_ready()
    assert result is True
    get_tok.assert_not_called()
