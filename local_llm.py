"""Local LLM 推理 — Gemini quota 爆時的生成式 fallback。
不像 lite_reply 寫死規則式，這個是真實 LLM，能自主對話。
"""
from __future__ import annotations
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("local_llm")
from local_llm_config import LOCAL_LLM_MODEL, LOCAL_LLM_FALLBACKS

try:  # ACTIVE_ADAPTER 可能在舊 config 裡缺失
    from local_llm_config import ACTIVE_ADAPTER  # type: ignore[attr-defined]
except Exception:
    ACTIVE_ADAPTER = None  # type: ignore[assignment]
_MODEL_NAME = LOCAL_LLM_MODEL
_model = None
_tokenizer = None
_loaded_name: Optional[str] = None
_loaded_adapter: Optional[str] = None
_cache_unavailable_logged = False


def _huggingface_cache_root() -> Path:
    """Return the HF cache root used before mlx_lm starts loading models."""
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]).expanduser()
    if os.environ.get("HUGGINGFACE_HUB_CACHE"):
        return Path(os.environ["HUGGINGFACE_HUB_CACHE"]).expanduser().parent
    return Path.home() / ".cache" / "huggingface"


def _huggingface_cache_available() -> bool:
    """Avoid slow model-load attempts when the HF cache symlink is broken."""
    global _cache_unavailable_logged
    root = _huggingface_cache_root()
    try:
        if root.is_symlink() and not root.exists():
            if not _cache_unavailable_logged:
                target = os.readlink(root)
                logger.warning(
                    "huggingface cache symlink target missing: %s -> %s",
                    root,
                    target,
                )
                _cache_unavailable_logged = True
            return False
    except OSError as e:
        logger.warning("huggingface cache status check failed: %s", e)
    return True


def _ensure_loaded() -> bool:
    """Lazy load model 一次。失敗回 False。
    依序嘗試 _MODEL_NAME → LOCAL_LLM_FALLBACKS（去重後高 → 低）。
    若 local_llm_config.ACTIVE_ADAPTER 有值 → 載 base + adapter（mlx-lm 支援
    `adapter_path=` kwarg）。adapter 沒成功不致命，會 fallback 到純 base。
    """
    global _model, _tokenizer, _loaded_name, _loaded_adapter
    if _model is not None:
        return True
    if not _huggingface_cache_available():
        return False
    try:
        from mlx_lm import load
    except Exception as e:
        logger.warning("mlx_lm import failed: %s", e)
        return False
    # 組裝候選清單：主 model 優先，再追加 fallback（去重保序）
    candidates = []
    seen = set()
    for name in [_MODEL_NAME, *LOCAL_LLM_FALLBACKS]:
        if name and name not in seen:
            candidates.append(name)
            seen.add(name)
    for name in candidates:
        # 先試 base + adapter（若 ACTIVE_ADAPTER 有設）
        if ACTIVE_ADAPTER:
            try:
                _model, _tokenizer = load(name, adapter_path=ACTIVE_ADAPTER)
                _loaded_name = name
                _loaded_adapter = ACTIVE_ADAPTER
                logger.info(
                    "local LLM loaded: %s + adapter %s", name, ACTIVE_ADAPTER
                )
                return True
            except Exception as e:
                logger.warning(
                    "local LLM load with adapter failed for %s + %s: %s",
                    name, ACTIVE_ADAPTER, e,
                )
                _model, _tokenizer = None, None
        # 純 base 後備
        try:
            _model, _tokenizer = load(name)
            _loaded_name = name
            _loaded_adapter = None
            logger.info("local LLM loaded: %s (no adapter)", name)
            return True
        except Exception as e:
            logger.warning("local LLM load failed for %s: %s", name, e)
            _model, _tokenizer = None, None
    return False


def loaded_model_name() -> Optional[str]:
    """回傳實際載入的模型名稱（首選 or fallback）。未載入回 None。"""
    return _loaded_name


def loaded_adapter_path() -> Optional[str]:
    """回傳實際載入的 LoRA adapter 路徑。未載入或沒帶 adapter 回 None。"""
    return _loaded_adapter


def chat(
    user_input: str,
    context: list[tuple[str, str]] | None = None,
    system_prompt: str = "你是個友善的 LINE 群組對話助理咪寶，用繁體中文簡短回覆。",
    max_tokens: int = 400,
) -> Optional[str]:
    """跟 Gemini 那條 chat 介面相容。失敗回 None。

    2026-05-09 整合：優先試 inference_accel.generate_with_speculation
    （speculative decoding 14B + 3B draft，速度 +30-50%）；失敗 graceful 退原 generate()。
    """
    if not _ensure_loaded():
        return None
    try:
        from mlx_lm import generate
        messages = [{"role": "system", "content": system_prompt}]
        if context:
            for role, text in context[-6:]:  # 最近 3 輪
                role_map = "user" if role == "user" else "assistant"
                messages.append({"role": role_map, "content": text})
        messages.append({"role": "user", "content": user_input})
        prompt = _tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
        )
        # 優先 speculative decoding（純本機加速）
        import os as _os
        if _os.environ.get("INFERENCE_SPECULATION", "1") != "0":
            try:
                import inference_accel
                if inference_accel.speculation_enabled():
                    out = inference_accel.generate_with_speculation(
                        prompt, _model, _tokenizer, max_tokens=max_tokens,
                    )
                    if out:
                        return out.strip()
            except (ImportError, Exception) as e:
                logger.debug("speculative decoding skipped: %s", e)
        response = generate(
            _model, _tokenizer, prompt=prompt,
            max_tokens=max_tokens, verbose=False,
        )
        return response.strip()
    except Exception as e:
        logger.warning("local_llm.chat failed: %s", e)
        return None


if __name__ == "__main__":
    import time
    try:
        import psutil
        _proc = psutil.Process()

        def _rss_mb() -> float:
            return _proc.memory_info().rss / (1024 * 1024)
    except Exception:
        def _rss_mb() -> float:
            return -1.0

    print(f"[start] RSS={_rss_mb():.1f} MB")
    t_load0 = time.time()
    ok = _ensure_loaded()
    print(f"[load] ok={ok} model={loaded_model_name()} took={time.time()-t_load0:.1f}s RSS={_rss_mb():.1f} MB")

    tests = [
        "你好嗎？",
        "你最喜歡什麼食物？",
        "幫我寫一個 Python 找質數的函式",
        "請分析一下 SOXL 在 2027 年的可能走勢",
        "為什麼天空是藍的",
    ]
    peak_rss = _rss_mb()
    for q in tests:
        t0 = time.time()
        r = chat(q)
        dt = time.time() - t0
        rss = _rss_mb()
        peak_rss = max(peak_rss, rss)
        chars = len(r) if r else 0
        print(f"\n>>> {q}\n[{dt:.1f}s | {chars} chars | RSS={rss:.1f} MB] {r}")
    print(f"\n[peak RSS] {peak_rss:.1f} MB")
