"""Local vision LLM — 圖片理解 fallback。

Lazy load Qwen2.5-VL-7B-Instruct-4bit (mlx-vlm)。
跟 gemini_client.chat 介面相容，但接受圖片 bytes / path。

回覆風格對齊 gemini_client._CORE_PROMPT 的「咪寶」人設 + 規則 0
（first-sentence-take）+ 黑名單 post-check（共用 _ECHO_OPENERS / _EMPTY_PHRASES）。

System prompt / post-check / compose_prompt 抽到 vision_common.py，
給本機 vision_llm 跟雲端 vision_cloud（Together AI）共用。
"""
from __future__ import annotations
import logging
import sys
from pathlib import Path
from typing import Optional

# 規則 0 / 咪寶人設 / 黑名單 post-check 共用模組
from vision_common import (
    compose_prompt as _compose_prompt,  # re-export with old name
    post_check as _post_check,  # re-export with old name
)

logger = logging.getLogger("vision_llm")

_MODEL_NAME = "mlx-community/Qwen2.5-VL-7B-Instruct-4bit"
_FALLBACKS = [
    "mlx-community/Qwen2.5-VL-7B-Instruct-4bit",
    "mlx-community/Qwen2-VL-2B-Instruct-4bit",
]
_model = None
_processor = None
_loaded_name = None


def _ensure_loaded() -> bool:
    global _model, _processor, _loaded_name
    if _model is not None:
        return True
    for name in _FALLBACKS:
        try:
            from mlx_vlm import load
            _model, _processor = load(name)
            _loaded_name = name
            logger.info("vision_llm loaded: %s", name)
            return True
        except Exception as e:
            logger.warning("load %s failed: %s", name, e)
    return False


def describe_image(
    image_path: str | Path | bytes,
    prompt: Optional[str] = None,
    max_tokens: int = 600,
) -> Optional[str]:
    """單張圖片 → 描述。失敗回 None。

    image_path 可以是檔案路徑或 bytes（自動寫 temp file）。
    prompt 為 None 時使用預設「請描述圖中的重點」+ 咪寶人設。
    回傳前會跑 _post_check 對齊規則 0。
    """
    if not _ensure_loaded():
        return None
    try:
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        # bytes → temp file
        if isinstance(image_path, bytes):
            import tempfile
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            tmp.write(image_path)
            tmp.close()
            image_path = tmp.name

        composed = _compose_prompt(prompt or "")
        formatted = apply_chat_template(
            _processor, _model.config, composed, num_images=1
        )
        response = generate(
            _model, _processor,
            image=str(image_path), prompt=formatted,
            max_tokens=max_tokens, verbose=False,
        )
        # mlx-vlm >= 0.5 returns a GenerationResult with .text; older returns str
        text = getattr(response, "text", response)
        if not text:
            return None
        return _post_check(text.strip())
    except Exception as e:
        logger.warning("describe_image failed: %s", e)
        return None


def chat_with_images(
    user_text: str,
    image_paths: list,
    max_tokens: int = 600,
) -> Optional[str]:
    """多圖 + 文字。回 LLM 回應。回傳前會跑 _post_check。"""
    if not _ensure_loaded():
        return None
    if not image_paths:
        return None
    try:
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        composed = _compose_prompt(user_text)
        prompt = apply_chat_template(
            _processor, _model.config, composed, num_images=len(image_paths)
        )
        response = generate(
            _model, _processor,
            image=[str(p) for p in image_paths],
            prompt=prompt, max_tokens=max_tokens, verbose=False,
        )
        text = getattr(response, "text", response)
        if not text:
            return None
        return _post_check(text.strip())
    except Exception as e:
        logger.warning("chat_with_images failed: %s", e)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test = sys.argv[1] if len(sys.argv) > 1 else "/path/to/test.jpg"
    print(describe_image(test))
