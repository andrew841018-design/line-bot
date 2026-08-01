"""Tests for `image_gen_local.generate`.

驗收項目：
  (a) prompt 翻譯流程：中文 prompt 會調 local_llm.chat
  (b) bytes 回傳：generate 成功時回 PNG bytes（前 8 byte = PNG signature）
  (c) load 失敗 → None：所有 backend 都載失敗時 generate 回 None
  (d) style 詞拼接：'photo' / 'anime' / 'art' style 詞會出現在 final prompt
  (e) 純英文 prompt 不調翻譯
  (f) 空 prompt 短路 → None
  (g) backend 強制切換（IMAGE_GEN_BACKEND env var）
  (h) load 失敗後標記 _load_failed → 後續 generate 立刻 None 不再重試
  (i) FLUX backend 也走 PIL → bytes
  (j) generate 過程爆例外 → 回 None（不 crash）
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image


# ── 確保 line_bot/ root 在 sys.path ─────────────────────────────────────────
LINE_BOT_ROOT = Path(__file__).resolve().parent.parent
if str(LINE_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(LINE_BOT_ROOT))


# ── Helpers ─────────────────────────────────────────────────────────────────

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _fake_pil(color=(120, 200, 80)) -> Image.Image:
    """造一張 32x32 純色 PIL 圖，給 mock 圖生 model 回傳用。"""
    return Image.new("RGB", (32, 32), color)


@pytest.fixture(autouse=True)
def _reset_module():
    """每條 test 前後重設 image_gen_local module state。"""
    import image_gen_local as ig
    ig.reset_state()
    yield
    ig.reset_state()


# ── (a) 中文 prompt → local_llm.chat 翻譯 ───────────────────────────────────


def test_chinese_prompt_calls_translator():
    import image_gen_local as ig

    fake_pipe = MagicMock()
    fake_pipe.return_value = MagicMock(images=[_fake_pil()])

    with patch("image_gen_local._try_load_sdxl_turbo", return_value=True), \
         patch.object(ig, "_pipe", fake_pipe, create=False), \
         patch.object(ig, "_active_backend", "sdxl_turbo"), \
         patch("local_llm.chat", return_value="a cute cat in a garden") as mock_chat:
        out = ig.generate("一隻可愛的貓在花園裡", style="photo")

    assert out is not None
    assert mock_chat.called, "中文 prompt 必須走翻譯"
    # full prompt 必須含翻譯後英文
    final_prompt = fake_pipe.call_args.kwargs["prompt"]
    assert "cute cat" in final_prompt


# ── (b) PNG bytes 回傳 ──────────────────────────────────────────────────────


def test_generate_returns_png_bytes():
    import image_gen_local as ig

    fake_pipe = MagicMock()
    fake_pipe.return_value = MagicMock(images=[_fake_pil()])

    with patch("image_gen_local._try_load_sdxl_turbo", return_value=True):
        ig._pipe = fake_pipe
        ig._active_backend = "sdxl_turbo"
        out = ig.generate("a sunset")

    assert isinstance(out, bytes)
    assert out.startswith(PNG_SIGNATURE), "必須是 PNG header"


# ── (c) 全部 backend 載失敗 → None ──────────────────────────────────────────


def test_all_backends_fail_returns_none():
    import image_gen_local as ig

    with patch("image_gen_local._try_load_sdxl_turbo", return_value=False), \
         patch("image_gen_local._try_load_flux_schnell", return_value=False), \
         patch("image_gen_local._try_load_sdxl_base", return_value=False):
        out = ig.generate("anything")

    assert out is None
    assert ig._load_failed is True


# ── (d) style 詞拼接 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("style,kw", [
    ("photo", "photorealistic"),
    ("anime", "anime style"),
    ("art", "concept art"),
    ("sketch", "pencil sketch"),
])
def test_style_keywords_appended(style, kw):
    import image_gen_local as ig

    fake_pipe = MagicMock()
    fake_pipe.return_value = MagicMock(images=[_fake_pil()])

    with patch("image_gen_local._try_load_sdxl_turbo", return_value=True):
        ig._pipe = fake_pipe
        ig._active_backend = "sdxl_turbo"
        ig.generate("dragon", style=style)

    final_prompt = fake_pipe.call_args.kwargs["prompt"]
    assert kw in final_prompt, f"style={style} 必須含 keyword '{kw}'，實際: {final_prompt}"


# ── (e) 純英文 prompt 不調翻譯 ──────────────────────────────────────────────


def test_english_prompt_skips_translator():
    import image_gen_local as ig

    fake_pipe = MagicMock()
    fake_pipe.return_value = MagicMock(images=[_fake_pil()])

    with patch("image_gen_local._try_load_sdxl_turbo", return_value=True), \
         patch("local_llm.chat", return_value="should not be called") as mock_chat:
        ig._pipe = fake_pipe
        ig._active_backend = "sdxl_turbo"
        ig.generate("a red apple on the table", style="photo")

    assert not mock_chat.called, "純英文 prompt 不該叫翻譯"


# ── (f) 空 prompt → None ────────────────────────────────────────────────────


def test_empty_prompt_returns_none():
    import image_gen_local as ig

    assert ig.generate("") is None
    assert ig.generate("   ") is None


# ── (g) IMAGE_GEN_BACKEND env var 強制 ──────────────────────────────────────


def test_env_var_forces_backend(monkeypatch):
    import image_gen_local as ig
    monkeypatch.setenv("IMAGE_GEN_BACKEND", "flux_schnell")

    flux_called = {"yes": False}

    def fake_flux_loader():
        flux_called["yes"] = True
        ig._pipe = MagicMock()
        ig._active_backend = "flux_schnell"
        return True

    with patch("image_gen_local._try_load_flux_schnell", side_effect=fake_flux_loader), \
         patch("image_gen_local._try_load_sdxl_turbo", return_value=True) as mock_turbo:
        ig._ensure_loaded()

    assert flux_called["yes"] is True
    assert not mock_turbo.called, "強制 flux 時不該嘗試 SDXL Turbo"


# ── (h) load 失敗後不重試 ──────────────────────────────────────────────────


def test_load_failed_short_circuits():
    import image_gen_local as ig

    with patch("image_gen_local._try_load_sdxl_turbo", return_value=False) as t, \
         patch("image_gen_local._try_load_flux_schnell", return_value=False) as f, \
         patch("image_gen_local._try_load_sdxl_base", return_value=False) as s:
        ig.generate("first try")  # 第一次：跑全部 fallback
        first_call_count = t.call_count + f.call_count + s.call_count
        ig.generate("second try")  # 第二次：應該短路
        second_call_count = t.call_count + f.call_count + s.call_count

    assert first_call_count == 3, "首次必須試完三個 backend"
    assert second_call_count == first_call_count, "第二次應該短路、不再 retry load"


# ── (i) FLUX backend 路徑也輸出 PNG bytes ───────────────────────────────────


def test_flux_backend_outputs_png():
    import image_gen_local as ig

    fake_flux = MagicMock()
    fake_flux.generate_image.return_value = MagicMock(image=_fake_pil(color=(50, 80, 200)))

    with patch("image_gen_local._try_load_flux_schnell", return_value=True):
        ig._pipe = fake_flux
        ig._active_backend = "flux_schnell"
        out = ig.generate("a blue ocean", style="photo")

    assert isinstance(out, bytes)
    assert out.startswith(PNG_SIGNATURE)
    # 驗證 generate_image 真的被叫到（mflux API）
    assert fake_flux.generate_image.called
    kwargs = fake_flux.generate_image.call_args.kwargs
    assert "a blue ocean" in kwargs["prompt"]
    assert "photorealistic" in kwargs["prompt"]


# ── (j) generate 過程爆例外 → None ──────────────────────────────────────────


def test_generate_exception_returns_none():
    import image_gen_local as ig

    fake_pipe = MagicMock(side_effect=RuntimeError("MPS OOM 模擬"))

    with patch("image_gen_local._try_load_sdxl_turbo", return_value=True):
        ig._pipe = fake_pipe
        ig._active_backend = "sdxl_turbo"
        out = ig.generate("anything", style="photo")

    assert out is None, "model 爆例外時必須 graceful 回 None"


# ── (k) bonus：active_backend() reflect lazy state ──────────────────────────


def test_active_backend_reflects_state():
    import image_gen_local as ig
    assert ig.active_backend() is None  # reset 過

    with patch("image_gen_local._try_load_sdxl_turbo", return_value=True):
        ig._pipe = MagicMock(return_value=MagicMock(images=[_fake_pil()]))
        ig._active_backend = "sdxl_turbo"
        ig.generate("hi")

    assert ig.active_backend() == "sdxl_turbo"


# ── (l) 咪寶 canonical visual identity ─────────────────────────────────────


@pytest.mark.parametrize("subject", [
    "咪寶坐在窗邊",
    "小貓咪寶坐在窗邊",
    "Mibao by a window",
    "mibao sleeping on a blanket",
])
def test_mibao_alias_adds_canonical_identity(subject):
    import image_gen_local as ig
    import mibao_identity

    with patch("image_gen_local._translate_to_english", return_value="scene translation"):
        final_prompt = ig._build_full_prompt(subject, "photo")

    assert mibao_identity.IMAGE_PROMPT_EN in final_prompt
    assert final_prompt.count(mibao_identity.IMAGE_PROMPT_EN) == 1
    assert "scene translation" in final_prompt
    assert "photorealistic" in final_prompt


@pytest.mark.parametrize("subject", [
    "一隻虎斑貓坐在窗邊",
    "米寶在睡覺",
    "貓咪寶寶在睡覺",
    "mibaox by a window",
])
def test_unrelated_or_near_match_does_not_add_mibao_identity(subject):
    import image_gen_local as ig
    import mibao_identity

    with patch("image_gen_local._translate_to_english", return_value="scene translation"):
        final_prompt = ig._build_full_prompt(subject, "photo")

    assert final_prompt == "scene translation, photorealistic, detailed, soft lighting, sharp focus, 4k"
    assert mibao_identity.IMAGE_PROMPT_EN not in final_prompt


def test_translator_cannot_invent_mibao_identity():
    import image_gen_local as ig
    import mibao_identity

    with patch("image_gen_local._translate_to_english", return_value="Mibao in a garden"):
        final_prompt = ig._build_full_prompt("一隻普通貓在花園", "photo")

    assert mibao_identity.IMAGE_PROMPT_EN not in final_prompt


def test_repeated_mibao_alias_adds_identity_once():
    import image_gen_local as ig
    import mibao_identity

    final_prompt = ig._build_full_prompt("Mibao and Mibao", "photo")
    assert final_prompt.count(mibao_identity.IMAGE_PROMPT_EN) == 1
