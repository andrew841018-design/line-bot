"""純本機文生圖（image generation）—— 給 LINE bot 用。

100% 本機，不打雲端。Apple Silicon GPU（mps / mlx）跑 Stable Diffusion 系列。

Backend 順序（速度優先）：
  1. SDXL Turbo via `diffusers` + torch mps —— 1 step, ~4s on M2 Pro（預設）
  2. FLUX.1-schnell via `mflux`（mlx 原生）—— 4 steps, ~20s（高品質後備）
  3. SDXL base via `diffusers` + torch mps —— 30 steps, 慢但相容性最廣

Memory（32GB Mac）：
  - SDXL Turbo ≈ 6.5GB FP16
  - FLUX schnell ≈ 12GB FP16 / 6GB 4-bit
  - SDXL base ≈ 6.5GB

主要 entrypoint：`generate(prompt, style='photo', steps=4) -> Optional[bytes]`
中文 prompt 會先用 `local_llm.chat` 翻成英文。失敗回 None。
"""

from __future__ import annotations

import io
import logging
import os
import random
from typing import Optional

logger = logging.getLogger("image_gen_local")

# ── Backend 與 model lazy state ─────────────────────────────────────────────
# 0 = 還沒嘗試載入；其它值 = 實際 active 的 backend（"sdxl_turbo" / "flux_schnell" / "sdxl_base"）
_active_backend: Optional[str] = None
_pipe: object = None  # diffusers Pipeline 或 mflux Flux1
_load_failed: bool = False  # 全部 fallback 試過都失敗 → 永久 give up，不再重試


# ── Style preset ────────────────────────────────────────────────────────────

_STYLE_KEYWORDS = {
    "photo": "photorealistic, detailed, soft lighting, sharp focus, 4k",
    "anime": "anime style, vibrant colors, cel shading, manga illustration",
    "art": "digital painting, concept art, dramatic lighting, trending on artstation",
    "sketch": "pencil sketch, monochrome, hand-drawn, line art",
}


def _has_chinese(text: str) -> bool:
    """偵測字串是否含 CJK 字元（粗略：U+4E00–U+9FFF）。"""
    return any("一" <= ch <= "鿿" for ch in text or "")


def _translate_to_english(prompt: str) -> str:
    """中文 prompt → 英文 image generation prompt（透過本機 LLM）。

    走 `local_llm.chat`。失敗（model 沒載 / 翻譯爆）→ 直接回原字串，
    讓圖生模型自己處理（FLUX/SDXL 對英文最佳但能吃多語）。
    """
    if not _has_chinese(prompt):
        return prompt
    try:
        from local_llm import chat as _local_chat
    except Exception as e:
        logger.warning("local_llm import 失敗，跳過翻譯: %s", e)
        return prompt
    sys_prompt = (
        "Translate the user's text to a concise English image generation "
        "prompt. Add 2-3 quality keywords (e.g. 'detailed', 'high quality'). "
        "Reply with the English prompt only, no explanation, no quotes."
    )
    try:
        en = _local_chat(prompt, system_prompt=sys_prompt, max_tokens=120)
    except Exception as e:
        logger.warning("local_llm.chat 翻譯爆: %s", e)
        return prompt
    if not en or not en.strip():
        return prompt
    # 安全：local LLM 偶爾會 echo 中文 → 沒過 ASCII 比例就不採用
    en = en.strip().strip('"').strip("'")
    if _has_chinese(en):
        logger.warning("翻譯結果仍含中文，回退原文")
        return prompt
    return en


def _build_full_prompt(prompt: str, style: str) -> str:
    """組裝最終 prompt：英文化 + style 詞。"""
    base = _translate_to_english(prompt)
    style_kw = _STYLE_KEYWORDS.get(style, _STYLE_KEYWORDS["photo"])
    return f"{base}, {style_kw}"


# ── Backend loaders ─────────────────────────────────────────────────────────


def _try_load_sdxl_turbo() -> bool:
    """SDXL Turbo（diffusers + mps）。最快 ~4s/張，1 step。"""
    global _pipe, _active_backend
    try:
        import torch
        from diffusers import AutoPipelineForText2Image
    except Exception as e:
        logger.info("diffusers/torch 載入失敗: %s", e)
        return False
    try:
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        pipe = AutoPipelineForText2Image.from_pretrained(
            "stabilityai/sdxl-turbo",
            torch_dtype=torch.float16,
            variant="fp16",
        )
        pipe = pipe.to(device)
        _pipe = pipe
        _active_backend = "sdxl_turbo"
        logger.info("SDXL Turbo loaded on %s", device)
        return True
    except Exception as e:
        logger.warning("SDXL Turbo load 失敗: %s", e)
        return False


def _try_load_flux_schnell() -> bool:
    """FLUX.1-schnell via mflux（mlx 原生，Apple Silicon 最快 mlx-only path）。"""
    global _pipe, _active_backend
    try:
        from mflux.models.common.config.model_config import ModelConfig
        from mflux.models.flux.variants.txt2img.flux import Flux1
    except Exception as e:
        logger.info("mflux 載入失敗: %s", e)
        return False
    try:
        # quantize=4 → 4-bit, ~6GB；quantize=None → FP16, ~12GB
        flux = Flux1(quantize=4, model_config=ModelConfig.schnell())
        _pipe = flux
        _active_backend = "flux_schnell"
        logger.info("FLUX.1-schnell loaded (4-bit quant)")
        return True
    except Exception as e:
        logger.warning("FLUX schnell load 失敗: %s", e)
        return False


def _try_load_sdxl_base() -> bool:
    """SDXL base —— 慢但廣相容；30 steps。"""
    global _pipe, _active_backend
    try:
        import torch
        from diffusers import StableDiffusionXLPipeline
    except Exception as e:
        logger.info("diffusers/torch 載入失敗: %s", e)
        return False
    try:
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        pipe = StableDiffusionXLPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0",
            torch_dtype=torch.float16,
            variant="fp16",
        )
        pipe = pipe.to(device)
        _pipe = pipe
        _active_backend = "sdxl_base"
        logger.info("SDXL base loaded on %s", device)
        return True
    except Exception as e:
        logger.warning("SDXL base load 失敗: %s", e)
        return False


def _ensure_loaded() -> bool:
    """Lazy load — 依序試 SDXL Turbo → FLUX schnell → SDXL base。
    任一成功就停。全失敗 → 標記 _load_failed 不再重試。
    """
    global _load_failed
    if _pipe is not None:
        return True
    if _load_failed:
        return False
    # 優先序由環境變數 IMAGE_GEN_BACKEND 強制（測試 / debug 用）
    forced = os.environ.get("IMAGE_GEN_BACKEND", "").strip().lower()
    if forced == "sdxl_turbo":
        ok = _try_load_sdxl_turbo()
    elif forced == "flux_schnell":
        ok = _try_load_flux_schnell()
    elif forced == "sdxl_base":
        ok = _try_load_sdxl_base()
    else:
        ok = (
            _try_load_sdxl_turbo()
            or _try_load_flux_schnell()
            or _try_load_sdxl_base()
        )
    if not ok:
        _load_failed = True
    return ok


# ── Image → PNG bytes ───────────────────────────────────────────────────────


def _pil_to_png_bytes(pil_image) -> bytes:
    """PIL.Image → PNG bytes."""
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    return buf.getvalue()


# ── Main entrypoint ─────────────────────────────────────────────────────────


def generate(
    prompt: str,
    style: str = "photo",
    steps: int = 4,
) -> Optional[bytes]:
    """產一張圖回 PNG bytes。失敗回 None。

    Args:
        prompt: 自然語言敘述（中英都吃；中文會自動翻譯）。
        style: 'photo' / 'anime' / 'art' / 'sketch' —— 加 quality 詞。
        steps: 取樣步數。SDXL Turbo 用 1，FLUX schnell 用 4，SDXL base 用 30。
               實際會根據 active backend 自動 cap。

    Returns:
        PNG bytes，或 None（model 沒載 / 生成爆）。
    """
    if not prompt or not prompt.strip():
        return None
    if not _ensure_loaded():
        return None
    full_prompt = _build_full_prompt(prompt, style)
    try:
        if _active_backend == "sdxl_turbo":
            # SDXL Turbo: 1 step, guidance_scale=0
            real_steps = max(1, min(steps, 4))
            result = _pipe(
                prompt=full_prompt,
                num_inference_steps=real_steps,
                guidance_scale=0.0,
                height=512,
                width=512,
            )
            pil = result.images[0]
            return _pil_to_png_bytes(pil)
        if _active_backend == "flux_schnell":
            # mflux: generate_image returns GeneratedImage(image=PIL.Image)
            real_steps = max(1, min(steps, 8))
            seed = random.randint(0, 2**31 - 1)
            gen = _pipe.generate_image(
                seed=seed,
                prompt=full_prompt,
                num_inference_steps=real_steps,
                height=512,
                width=512,
                guidance=0.0,
            )
            return _pil_to_png_bytes(gen.image)
        if _active_backend == "sdxl_base":
            real_steps = max(20, min(steps if steps > 4 else 30, 50))
            result = _pipe(
                prompt=full_prompt,
                num_inference_steps=real_steps,
                height=768,
                width=768,
            )
            pil = result.images[0]
            return _pil_to_png_bytes(pil)
    except Exception as e:
        logger.warning("image generate 爆: backend=%s err=%s", _active_backend, e)
        return None
    return None


def active_backend() -> Optional[str]:
    """回傳目前 active 的 backend 名稱。沒 load 過 → None。"""
    return _active_backend


def reset_state() -> None:
    """測試用：清掉 lazy state，下次 generate() 會重新嘗試 load。"""
    global _pipe, _active_backend, _load_failed
    _pipe = None
    _active_backend = None
    _load_failed = False
