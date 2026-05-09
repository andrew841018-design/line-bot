"""tests/test_inference_accel.py — pure-mock unit tests.

不真的下載 14B / 3B model；mlx_lm.load + mlx_lm.generate 全 mock。
驗證 speculative path、draft 載入、KV cache 命中、fallback 邏輯。
"""

from __future__ import annotations

import importlib
import inspect
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# fake mlx-lm
# ─────────────────────────────────────────────────────────────────────────────


class _FakeTokenizer:
    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
        # Simple deterministic prompt build
        return "|".join(f"{m['role']}:{m['content']}" for m in messages)

    def encode(self, text, add_special_tokens=False):
        # 1 token per char (deterministic, easy to assert)
        return list(range(len(text)))


class _FakeModel:
    def __init__(self, name: str = "fake"):
        self.name = name


def _fake_make_prompt_cache(model, max_kv_size=None):
    """Returns a sentinel list — used as identity check in test."""
    return [f"cache_for_{getattr(model, 'name', 'unknown')}"]


def _install_fake_mlx_lm(
    monkeypatch,
    *,
    generate_return: str = "fake response",
    generate_supports_draft: bool = True,
    target_load_ok: bool = True,
    draft_load_ok: bool = True,
    raise_on_generate: bool = False,
):
    """Build & inject fake mlx_lm modules into sys.modules.

    Returns dict of MagicMocks so tests can assert call args / counts.
    """
    fake_mlx_lm = types.ModuleType("mlx_lm")
    fake_models = types.ModuleType("mlx_lm.models")
    fake_cache = types.ModuleType("mlx_lm.models.cache")

    target_tok = _FakeTokenizer()
    target_mdl = _FakeModel("target")
    draft_tok = _FakeTokenizer()
    draft_mdl = _FakeModel("draft")

    load_calls: list[str] = []

    def fake_load(name, *args, **kwargs):
        load_calls.append(name)
        if "14B" in name or name.endswith("target"):
            if not target_load_ok:
                raise RuntimeError("synthetic target load fail")
            return target_mdl, target_tok
        if "3B" in name or name.endswith("draft"):
            if not draft_load_ok:
                raise RuntimeError("synthetic draft load fail")
            return draft_mdl, draft_tok
        # default → treat as target
        if not target_load_ok:
            raise RuntimeError("synthetic load fail")
        return target_mdl, target_tok

    generate_mock = MagicMock()

    def fake_generate(model, tokenizer, prompt, **kwargs):
        generate_mock(model=model, tokenizer=tokenizer, prompt=prompt, **kwargs)
        if raise_on_generate:
            raise RuntimeError("synthetic generate fail")
        if "draft_model" in kwargs and not generate_supports_draft:
            raise TypeError("unexpected keyword 'draft_model'")
        return generate_return

    # stream_generate signature must declare draft_model param if supported
    if generate_supports_draft:
        def fake_stream_generate(model, tokenizer, prompt, draft_model=None, **kwargs):
            yield from ()
    else:
        def fake_stream_generate(model, tokenizer, prompt, **kwargs):
            yield from ()

    fake_mlx_lm.load = fake_load
    fake_mlx_lm.generate = fake_generate
    fake_mlx_lm.stream_generate = fake_stream_generate
    fake_cache.make_prompt_cache = _fake_make_prompt_cache

    monkeypatch.setitem(sys.modules, "mlx_lm", fake_mlx_lm)
    monkeypatch.setitem(sys.modules, "mlx_lm.models", fake_models)
    monkeypatch.setitem(sys.modules, "mlx_lm.models.cache", fake_cache)

    return {
        "load_calls": load_calls,
        "generate_mock": generate_mock,
        "target_mdl": target_mdl,
        "draft_mdl": draft_mdl,
    }


def _reload_inference_accel(monkeypatch, env_flag: str | None = None):
    if env_flag is None:
        monkeypatch.delenv("INFERENCE_SPECULATION", raising=False)
    else:
        monkeypatch.setenv("INFERENCE_SPECULATION", env_flag)
    sys.modules.pop("inference_accel", None)
    import inference_accel

    importlib.reload(inference_accel)
    inference_accel._reset_module_state_for_test()
    return inference_accel


# ─────────────────────────────────────────────────────────────────────────────
# tests
# ─────────────────────────────────────────────────────────────────────────────


def test_module_imports_clean(monkeypatch):
    """(import 不爆) 即使沒裝 mlx_lm 也能 import."""
    # ensure fresh import even without mlx_lm in sys
    sys.modules.pop("inference_accel", None)
    sys.modules.pop("mlx_lm", None)
    sys.modules.pop("mlx_lm.models", None)
    sys.modules.pop("mlx_lm.models.cache", None)
    import inference_accel  # noqa: F401

    assert hasattr(inference_accel, "generate_with_speculation")
    assert hasattr(inference_accel, "CachedSession")


def test_speculation_disabled_by_default(monkeypatch):
    """env 沒設 → speculation_enabled() == False。"""
    ia = _reload_inference_accel(monkeypatch, env_flag=None)
    assert ia.speculation_enabled() is False


def test_speculation_enabled_via_env(monkeypatch):
    """env=true → enabled。也支援 1/yes/on。"""
    ia = _reload_inference_accel(monkeypatch, env_flag="true")
    assert ia.speculation_enabled() is True
    ia = _reload_inference_accel(monkeypatch, env_flag="1")
    assert ia.speculation_enabled() is True
    ia = _reload_inference_accel(monkeypatch, env_flag="off")
    assert ia.speculation_enabled() is False


def test_speculative_path_triggered_when_enabled(monkeypatch):
    """(a) speculative path 觸發 → generate 被呼叫且帶 draft_model kwarg。"""
    fakes = _install_fake_mlx_lm(monkeypatch)
    ia = _reload_inference_accel(monkeypatch, env_flag="true")

    out = ia.generate_with_speculation(
        "hello", target_model="model-14B", draft_model="model-3B", max_tokens=42
    )
    assert out == "fake response"
    # generate 一定有被叫，且帶 draft_model
    fakes["generate_mock"].assert_called()
    last_kwargs = fakes["generate_mock"].call_args.kwargs
    assert "draft_model" in last_kwargs
    assert last_kwargs["draft_model"] is fakes["draft_mdl"]
    assert last_kwargs["num_draft_tokens"] == ia.DEFAULT_NUM_DRAFT_TOKENS
    assert last_kwargs["max_tokens"] == 42


def test_speculation_off_skips_draft_load(monkeypatch):
    """env OFF → draft 不載入，generate 不帶 draft_model kwarg。"""
    fakes = _install_fake_mlx_lm(monkeypatch)
    ia = _reload_inference_accel(monkeypatch, env_flag=None)

    out = ia.generate_with_speculation("hello", target_model="model-14B")
    assert out == "fake response"
    # 載入清單只有 target，沒有 draft
    assert any("14B" in n for n in fakes["load_calls"])
    assert not any("3B" in n for n in fakes["load_calls"])
    last_kwargs = fakes["generate_mock"].call_args.kwargs
    assert "draft_model" not in last_kwargs


def test_draft_model_loaded_lazy_once(monkeypatch):
    """(b) draft 第一次呼叫才載；第二次不重載。"""
    fakes = _install_fake_mlx_lm(monkeypatch)
    ia = _reload_inference_accel(monkeypatch, env_flag="true")

    ia.generate_with_speculation("p1", target_model="model-14B", draft_model="model-3B")
    ia.generate_with_speculation("p2", target_model="model-14B", draft_model="model-3B")

    target_loads = sum(1 for n in fakes["load_calls"] if "14B" in n)
    draft_loads = sum(1 for n in fakes["load_calls"] if "3B" in n)
    assert target_loads == 1
    assert draft_loads == 1


def test_draft_load_failure_falls_back_to_plain(monkeypatch):
    """(d) draft 載失敗 → fallback 純 14B（不帶 draft_model kwarg、不 raise）。"""
    fakes = _install_fake_mlx_lm(monkeypatch, draft_load_ok=False)
    ia = _reload_inference_accel(monkeypatch, env_flag="true")

    out = ia.generate_with_speculation(
        "hello", target_model="model-14B", draft_model="model-3B"
    )
    assert out == "fake response"
    last_kwargs = fakes["generate_mock"].call_args.kwargs
    assert "draft_model" not in last_kwargs


def test_unsupported_speculative_kwarg_falls_back(monkeypatch):
    """mlx-lm 太舊不認 draft_model kwarg → TypeError → 自動 retry plain。"""
    fakes = _install_fake_mlx_lm(monkeypatch, generate_supports_draft=False)
    ia = _reload_inference_accel(monkeypatch, env_flag="true")

    out = ia.generate_with_speculation(
        "hello", target_model="model-14B", draft_model="model-3B"
    )
    assert out == "fake response"
    # 至少被叫過 2 次：第一次 spec（TypeError）+ 第二次 plain
    assert fakes["generate_mock"].call_count >= 2
    final_kwargs = fakes["generate_mock"].call_args.kwargs
    assert "draft_model" not in final_kwargs


def test_target_load_failure_returns_empty(monkeypatch):
    """target 14B 都載不起來 → 回 "" (caller 自己決定下一招)。"""
    _install_fake_mlx_lm(monkeypatch, target_load_ok=False)
    ia = _reload_inference_accel(monkeypatch, env_flag="true")

    out = ia.generate_with_speculation("hello", target_model="model-14B")
    assert out == ""


def test_cached_session_first_turn_builds_cache(monkeypatch):
    """第一輪：建 prompt_cache 並傳給 generate。"""
    fakes = _install_fake_mlx_lm(monkeypatch)
    ia = _reload_inference_accel(monkeypatch, env_flag=None)

    sess = ia.get_session("group_A", target_model="model-14B")
    assert sess.turns == 0
    assert sess.hit_count == 0

    out = sess.chat("你好", context=[])
    assert out == "fake response"
    assert sess.turns == 1
    assert sess.hit_count == 0  # first turn doesn't count as hit
    assert sess.prompt_cache is not None
    last_kwargs = fakes["generate_mock"].call_args.kwargs
    assert "prompt_cache" in last_kwargs
    assert last_kwargs["prompt_cache"] is sess.prompt_cache


def test_cached_session_reuses_cache_across_turns(monkeypatch):
    """(c) 第 2、3 輪 reuse 同一個 prompt_cache → hit_count 累加。"""
    fakes = _install_fake_mlx_lm(monkeypatch)
    ia = _reload_inference_accel(monkeypatch, env_flag=None)

    sess = ia.get_session("group_A", target_model="model-14B")
    cache_objects: list[Any] = []

    for i in range(3):
        sess.chat(f"訊息 {i}", context=[])
        cache_objects.append(sess.prompt_cache)

    assert sess.turns == 3
    assert sess.hit_count == 2  # turn 2 + turn 3 都是 hit
    # cache identity 維持不變（reuse，非重建）
    assert cache_objects[0] is cache_objects[1] is cache_objects[2]


def test_cached_session_isolated_per_group(monkeypatch):
    """不同 group 各自一份 cache，互不影響。"""
    _install_fake_mlx_lm(monkeypatch)
    ia = _reload_inference_accel(monkeypatch, env_flag=None)

    sess_a = ia.get_session("group_A", target_model="model-14B")
    sess_b = ia.get_session("group_B", target_model="model-14B")
    assert sess_a is not sess_b
    sess_a.chat("a-msg")
    assert sess_a.turns == 1
    assert sess_b.turns == 0
    sess_b.chat("b-msg")
    assert sess_a.prompt_cache is not sess_b.prompt_cache


def test_cached_session_reset_clears_state(monkeypatch):
    """reset() 清空 cache + 計數器。"""
    _install_fake_mlx_lm(monkeypatch)
    ia = _reload_inference_accel(monkeypatch, env_flag=None)

    sess = ia.get_session("group_A", target_model="model-14B")
    sess.chat("first")
    sess.chat("second")
    assert sess.turns == 2
    sess.reset()
    assert sess.turns == 0
    assert sess.hit_count == 0
    assert sess.prompt_cache is None


def test_mlx_supports_speculative_probe(monkeypatch):
    """`_mlx_supports_speculative` 看 stream_generate 簽名有沒有 draft_model。"""
    _install_fake_mlx_lm(monkeypatch, generate_supports_draft=True)
    ia = _reload_inference_accel(monkeypatch, env_flag=None)
    assert ia._mlx_supports_speculative() is True

    _install_fake_mlx_lm(monkeypatch, generate_supports_draft=False)
    ia = _reload_inference_accel(monkeypatch, env_flag=None)
    assert ia._mlx_supports_speculative() is False


def test_generate_exception_returns_empty(monkeypatch):
    """generate 全程 raise → 回 ""，不爆給 caller。"""
    _install_fake_mlx_lm(monkeypatch, raise_on_generate=True)
    ia = _reload_inference_accel(monkeypatch, env_flag=None)

    out = ia.generate_with_speculation("hello", target_model="model-14B")
    assert out == ""
