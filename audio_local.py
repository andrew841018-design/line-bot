"""本機 audio ASR — 用 mlx-whisper（Apple Silicon GPU 加速）。

取代 audio_asr.py（Groq Whisper 雲端，被砍）。100% 本機，不打雲。

策略：
- 主 model：mlx-community/whisper-large-v3-mlx（~3GB，最強）
- 退：mlx-community/whisper-medium-mlx（~1.5GB）
- 退：mlx-community/whisper-small-mlx（~500MB）
- lazy load（第一次呼叫才下載 model）+ 失敗自動降級
- LINE audio 通常 m4a / mp4 / opus → 寫進 temp file（mlx-whisper 吃 file path，內部會用 ffmpeg decode）
- timeout 60s（避免主執行緒 webhook timeout）

對外 API：
    transcribe(audio_bytes_or_path, language="zh") -> Optional[str]
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from typing import Optional, Union

logger = logging.getLogger("audio_local")

# 三層 fallback model，由強→弱
_MODEL_CHAIN = [
    "mlx-community/whisper-large-v3-mlx",   # ~3GB
    "mlx-community/whisper-medium-mlx",     # ~1.5GB
    "mlx-community/whisper-small-mlx",      # ~500MB
]

_TIMEOUT_SECONDS = 60.0

# lazy state：記住「目前可用的 model」（避免每次 call 都重試 large）
_active_model: Optional[str] = None
_active_model_lock = threading.Lock()


def _resolve_model() -> Optional[str]:
    """回傳目前可用 model 名稱；尚未決定時回 None（讓 caller 走 fallback chain）。"""
    with _active_model_lock:
        return _active_model


def _set_active_model(name: str) -> None:
    global _active_model
    with _active_model_lock:
        _active_model = name


def _run_with_timeout(target, timeout: float):
    """跑 target() 在 thread 內，回 (result, error, timed_out)。

    mlx-whisper 沒原生 timeout，自己包一層 thread + join。timeout 後 thread 不能殺，
    但會被 GC（process 結束自動清），對 webhook 來說 caller 已經 return 了。
    """
    result_box: list = [None]
    error_box: list = [None]

    def _runner() -> None:
        try:
            result_box[0] = target()
        except Exception as e:  # noqa: BLE001
            error_box[0] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        return None, None, True
    return result_box[0], error_box[0], False


def _transcribe_with_model(
    audio_path: str,
    model_name: str,
    language: str,
) -> Optional[str]:
    """單一 model 嘗試。失敗 raise，timeout 也 raise。"""
    import mlx_whisper  # lazy import，避免測試環境硬要求

    def _call():
        return mlx_whisper.transcribe(
            audio_path,
            path_or_hf_repo=model_name,
            language=language,
        )

    result, error, timed_out = _run_with_timeout(_call, _TIMEOUT_SECONDS)
    if timed_out:
        raise TimeoutError(
            f"mlx_whisper.transcribe timed out after {_TIMEOUT_SECONDS}s "
            f"(model={model_name})"
        )
    if error is not None:
        raise error

    if not isinstance(result, dict):
        return None
    text = result.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    return text or None


def transcribe(
    audio_bytes_or_path: Union[bytes, str, os.PathLike],
    language: str = "zh",
) -> Optional[str]:
    """轉寫音訊 → 純文字。

    參數：
        audio_bytes_or_path: bytes（LINE webhook 下載結果）或檔案路徑
        language: ISO-639-1 代碼，預設 "zh"（中文）

    回傳：
        - 成功：轉寫文字（已 strip）
        - 失敗（model 全爆 / timeout / 空音訊）：None

    語意：永遠不 raise，全部失敗 swallow 後 log。caller 拿到 None 就是「無轉寫結果」。
    """
    # 1. bytes → temp file
    tmp_path: Optional[str] = None
    cleanup_tmp = False
    try:
        if isinstance(audio_bytes_or_path, (bytes, bytearray)):
            data = bytes(audio_bytes_or_path)
            if not data:
                logger.info("audio_local: empty audio bytes, skip")
                return None
            tmp = tempfile.NamedTemporaryFile(
                prefix="audio_local_", suffix=".m4a", delete=False
            )
            tmp.write(data)
            tmp.close()
            tmp_path = tmp.name
            cleanup_tmp = True
            audio_path = tmp_path
        else:
            audio_path = os.fspath(audio_bytes_or_path)
            if not os.path.exists(audio_path):
                logger.warning("audio_local: path not found: %s", audio_path)
                return None
            if os.path.getsize(audio_path) == 0:
                logger.info("audio_local: empty audio file, skip")
                return None

        # 2. fallback chain：先試已記住的 active model；沒有則從 large 開始試
        active = _resolve_model()
        if active is not None:
            try:
                text = _transcribe_with_model(audio_path, active, language)
                if text:
                    return text
                logger.info("audio_local: active model %s returned empty", active)
                return None
            except TimeoutError as e:
                logger.warning("audio_local: %s", e)
                return None
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "audio_local: active model %s failed (%s); "
                    "re-running fallback chain",
                    active,
                    e,
                )
                # active 突然壞了 → 重跑 fallback chain
                _set_active_model("")  # 清空，下面再決定
                with _active_model_lock:
                    pass

        # 從 large → medium → small 試
        last_error: Optional[Exception] = None
        for model_name in _MODEL_CHAIN:
            try:
                logger.info("audio_local: trying model %s", model_name)
                text = _transcribe_with_model(audio_path, model_name, language)
                _set_active_model(model_name)
                if text:
                    return text
                logger.info(
                    "audio_local: model %s returned empty result", model_name
                )
                return None
            except TimeoutError as e:
                last_error = e
                logger.warning(
                    "audio_local: %s timed out, trying next fallback", model_name
                )
                continue
            except Exception as e:  # noqa: BLE001
                last_error = e
                logger.warning(
                    "audio_local: model %s failed (%s), trying next fallback",
                    model_name,
                    e,
                )
                continue

        logger.error(
            "audio_local: all models failed; last_error=%s", last_error
        )
        return None

    finally:
        if cleanup_tmp and tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


__all__ = ["transcribe"]
