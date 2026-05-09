"""Tests for audio_local — 純 mock，不真的跑 mlx-whisper（會載 model）。

涵蓋：
1. bytes → 轉寫成功
2. file path → 轉寫成功
3. large 失敗 → medium 成功（fallback chain）
4. 三層 model 全失敗 → None
5. 空 bytes → None（不 call model）
6. 空 file → None（不 call model）
7. timeout → fallback 下一個 model
8. mlx-whisper 回傳非 dict → None
9. 已記住 active model 後直接用該 model（不從 large 開始）
10. 全 timeout → None
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# 確保 import path 對得到 audio_local.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audio_local  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_active_model():
    """每條 test 前後都清空 active model state。"""
    audio_local._set_active_model("")
    audio_local._active_model = None
    yield
    audio_local._active_model = None


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: bytes 處理 — 轉寫成功
# ─────────────────────────────────────────────────────────────────────────────
def test_transcribe_bytes_success():
    """傳 bytes 應該寫成 temp file 餵給 mlx_whisper，並回傳 strip 過的文字。"""
    fake_module = mock.MagicMock()
    fake_module.transcribe.return_value = {"text": "  你好世界  "}

    with mock.patch.dict(sys.modules, {"mlx_whisper": fake_module}):
        result = audio_local.transcribe(b"\x00fake_audio_bytes\x01\x02")

    assert result == "你好世界"
    fake_module.transcribe.assert_called_once()
    # 確認餵的 path 是個檔案（temp file）
    call_args = fake_module.transcribe.call_args
    audio_path = call_args.args[0]
    assert isinstance(audio_path, str)
    # temp 應該在 call 後被刪掉
    assert not os.path.exists(audio_path)
    # language 預設 zh
    assert call_args.kwargs.get("language") == "zh"
    assert call_args.kwargs.get("path_or_hf_repo") == (
        "mlx-community/whisper-large-v3-mlx"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: path 處理 — 轉寫成功
# ─────────────────────────────────────────────────────────────────────────────
def test_transcribe_path_success(tmp_path):
    """傳 file path 應該直接餵給 mlx_whisper，且不刪 caller 的檔案。"""
    audio_file = tmp_path / "voice.m4a"
    audio_file.write_bytes(b"fake_audio")

    fake_module = mock.MagicMock()
    fake_module.transcribe.return_value = {"text": "Hello world"}

    with mock.patch.dict(sys.modules, {"mlx_whisper": fake_module}):
        result = audio_local.transcribe(str(audio_file), language="en")

    assert result == "Hello world"
    fake_module.transcribe.assert_called_once()
    call_args = fake_module.transcribe.call_args
    assert call_args.args[0] == str(audio_file)
    assert call_args.kwargs.get("language") == "en"
    # caller 的檔案不能被刪
    assert audio_file.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: model 載入失敗 fallback — large 失敗 → medium 成功
# ─────────────────────────────────────────────────────────────────────────────
def test_fallback_large_to_medium():
    """large 失敗時應該降到 medium；medium 成功就回該結果，不再試 small。"""
    call_log: list[str] = []

    def fake_transcribe(audio_path, *, path_or_hf_repo, language):
        call_log.append(path_or_hf_repo)
        if "large" in path_or_hf_repo:
            raise RuntimeError("model load failed (e.g. OOM / network)")
        if "medium" in path_or_hf_repo:
            return {"text": "fallback success"}
        raise AssertionError("不該走到 small")

    fake_module = mock.MagicMock()
    fake_module.transcribe.side_effect = fake_transcribe

    with mock.patch.dict(sys.modules, {"mlx_whisper": fake_module}):
        result = audio_local.transcribe(b"audio")

    assert result == "fallback success"
    assert call_log == [
        "mlx-community/whisper-large-v3-mlx",
        "mlx-community/whisper-medium-mlx",
    ]
    # active model 應該被記成 medium
    assert audio_local._resolve_model() == "mlx-community/whisper-medium-mlx"


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: 三層全失敗 → None
# ─────────────────────────────────────────────────────────────────────────────
def test_all_models_fail_returns_none():
    """三個 model 全部 raise → swallow 並回 None。"""
    fake_module = mock.MagicMock()
    fake_module.transcribe.side_effect = RuntimeError("everything broken")

    with mock.patch.dict(sys.modules, {"mlx_whisper": fake_module}):
        result = audio_local.transcribe(b"audio_bytes")

    assert result is None
    # 三個 model 都試過
    assert fake_module.transcribe.call_count == 3
    repos = [
        c.kwargs["path_or_hf_repo"] for c in fake_module.transcribe.call_args_list
    ]
    assert repos == [
        "mlx-community/whisper-large-v3-mlx",
        "mlx-community/whisper-medium-mlx",
        "mlx-community/whisper-small-mlx",
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: 空 bytes → None，不 call model
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_bytes_returns_none_no_model_call():
    """空 bytes 應該短路，不浪費資源 load model。"""
    fake_module = mock.MagicMock()

    with mock.patch.dict(sys.modules, {"mlx_whisper": fake_module}):
        result = audio_local.transcribe(b"")

    assert result is None
    fake_module.transcribe.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: 空 file → None，不 call model
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_file_returns_none_no_model_call(tmp_path):
    empty_file = tmp_path / "empty.m4a"
    empty_file.write_bytes(b"")

    fake_module = mock.MagicMock()

    with mock.patch.dict(sys.modules, {"mlx_whisper": fake_module}):
        result = audio_local.transcribe(str(empty_file))

    assert result is None
    fake_module.transcribe.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: timeout → fallback 下一個 model
# ─────────────────────────────────────────────────────────────────────────────
def test_timeout_falls_through_to_next_model(monkeypatch):
    """large timeout 應該被當成 model 失敗，fallback 到 medium。"""
    # 把 timeout 縮到 0.05s
    monkeypatch.setattr(audio_local, "_TIMEOUT_SECONDS", 0.05)

    import time

    call_log: list[str] = []

    def fake_transcribe(audio_path, *, path_or_hf_repo, language):
        call_log.append(path_or_hf_repo)
        if "large" in path_or_hf_repo:
            time.sleep(1.0)  # 故意 hang，超過 _TIMEOUT_SECONDS
            return {"text": "shouldn't reach"}
        return {"text": "from medium"}

    fake_module = mock.MagicMock()
    fake_module.transcribe.side_effect = fake_transcribe

    with mock.patch.dict(sys.modules, {"mlx_whisper": fake_module}):
        result = audio_local.transcribe(b"audio")

    assert result == "from medium"
    assert call_log[0] == "mlx-community/whisper-large-v3-mlx"
    assert call_log[1] == "mlx-community/whisper-medium-mlx"


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: mlx-whisper 回傳非 dict → None
# ─────────────────────────────────────────────────────────────────────────────
def test_non_dict_response_returns_none():
    """mlx_whisper 行為奇怪回非 dict（或 dict 沒 text key）→ 視為空結果。"""
    fake_module = mock.MagicMock()
    fake_module.transcribe.return_value = "not a dict"

    with mock.patch.dict(sys.modules, {"mlx_whisper": fake_module}):
        result = audio_local.transcribe(b"audio_bytes")

    assert result is None


def test_dict_no_text_key_returns_none():
    """dict 但沒 'text' key → None。"""
    fake_module = mock.MagicMock()
    fake_module.transcribe.return_value = {"language": "zh"}

    with mock.patch.dict(sys.modules, {"mlx_whisper": fake_module}):
        result = audio_local.transcribe(b"audio_bytes")

    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: 已記住 active model 後直接用該 model
# ─────────────────────────────────────────────────────────────────────────────
def test_active_model_skips_fallback_chain():
    """第二次呼叫應該直接用上次選到的 model，不從 large 重來。"""
    call_log: list[str] = []

    def fake_transcribe(audio_path, *, path_or_hf_repo, language):
        call_log.append(path_or_hf_repo)
        if "large" in path_or_hf_repo:
            raise RuntimeError("nope")
        return {"text": "ok"}

    fake_module = mock.MagicMock()
    fake_module.transcribe.side_effect = fake_transcribe

    with mock.patch.dict(sys.modules, {"mlx_whisper": fake_module}):
        # 第一次：large fail → medium 成功
        r1 = audio_local.transcribe(b"audio1")
        # 第二次：應該直接走 medium
        r2 = audio_local.transcribe(b"audio2")

    assert r1 == "ok"
    assert r2 == "ok"
    # 第一次：large + medium = 2 calls；第二次：只 medium = 1 call。total = 3
    assert call_log == [
        "mlx-community/whisper-large-v3-mlx",
        "mlx-community/whisper-medium-mlx",
        "mlx-community/whisper-medium-mlx",
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: 全 timeout → None
# ─────────────────────────────────────────────────────────────────────────────
def test_all_models_timeout_returns_none(monkeypatch):
    """三個 model 都 timeout → None。"""
    monkeypatch.setattr(audio_local, "_TIMEOUT_SECONDS", 0.03)

    import time

    def fake_transcribe(audio_path, *, path_or_hf_repo, language):
        time.sleep(0.5)
        return {"text": "shouldn't reach"}

    fake_module = mock.MagicMock()
    fake_module.transcribe.side_effect = fake_transcribe

    with mock.patch.dict(sys.modules, {"mlx_whisper": fake_module}):
        result = audio_local.transcribe(b"audio")

    assert result is None
    assert fake_module.transcribe.call_count == 3


# ─────────────────────────────────────────────────────────────────────────────
# Bonus: 不存在的 path → None
# ─────────────────────────────────────────────────────────────────────────────
def test_nonexistent_path_returns_none():
    fake_module = mock.MagicMock()

    with mock.patch.dict(sys.modules, {"mlx_whisper": fake_module}):
        result = audio_local.transcribe("/tmp/this_does_not_exist_xyz_123.m4a")

    assert result is None
    fake_module.transcribe.assert_not_called()
