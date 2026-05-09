"""Inference acceleration — speculative decoding + KV cache 持久化（純本機 mlx-lm）。

針對 14B target model 跑得慢（~30s for 200 tokens），用 3B draft model 預測 next k tokens、
14B 一次驗證 → 預期 +30~50% throughput。對同 group 連續對話，再用 KV cache 持久化讓
後續 turn 只需 encode 增量 → 對話 5+ 輪預期 2-3x 加速。

Public API：
    - generate_with_speculation(prompt, target_model, draft_model, ...) -> str
    - CachedSession(group_id, target_model, draft_model=None)
    - get_session(group_id, target_model, draft_model=None) -> CachedSession

開關：env `INFERENCE_SPECULATION` = "true" 才走 speculative path（預設 OFF，先驗證穩定）。
fallback：mlx-lm 不支援 draft_model kwarg / 載 draft 失敗 → 純 14B 跑（不會 raise）。
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger("inference_accel")

# ----- env flags -----
ENV_SPEC_FLAG = "INFERENCE_SPECULATION"
DEFAULT_DRAFT_MODEL = "mlx-community/Qwen2.5-3B-Instruct-4bit"
DEFAULT_TARGET_MODEL = "mlx-community/Qwen2.5-14B-Instruct-4bit"
DEFAULT_NUM_DRAFT_TOKENS = 4  # 3B 一次預測 4 tokens 給 14B 驗

# ----- module-level state（lazy load + reuse）-----
_draft_model: Any = None
_draft_tokenizer: Any = None
_draft_loaded_name: Optional[str] = None
_target_model: Any = None
_target_tokenizer: Any = None
_target_loaded_name: Optional[str] = None
_load_lock = threading.Lock()

# group_id -> CachedSession
_sessions: dict[str, "CachedSession"] = {}
_session_lock = threading.Lock()


def speculation_enabled() -> bool:
    """讀 env flag。預設 OFF（穩定後再開）。"""
    return os.environ.get(ENV_SPEC_FLAG, "").lower() in ("1", "true", "yes", "on")


def _mlx_supports_speculative() -> bool:
    """Probe mlx-lm 是否支援 draft_model kwarg。0.21+ 應該都有。"""
    try:
        import inspect

        from mlx_lm import stream_generate

        sig = inspect.signature(stream_generate)
        return "draft_model" in sig.parameters
    except Exception as e:
        logger.warning("mlx-lm speculative probe failed: %s", e)
        return False


def _load_target(target_model_name: str = DEFAULT_TARGET_MODEL) -> bool:
    """Lazy load target (14B). 失敗回 False。"""
    global _target_model, _target_tokenizer, _target_loaded_name
    with _load_lock:
        if _target_model is not None and _target_loaded_name == target_model_name:
            return True
        try:
            from mlx_lm import load
        except Exception as e:
            logger.warning("mlx_lm import failed: %s", e)
            return False
        try:
            _target_model, _target_tokenizer = load(target_model_name)
            _target_loaded_name = target_model_name
            logger.info("target model loaded: %s", target_model_name)
            return True
        except Exception as e:
            logger.warning("target load failed (%s): %s", target_model_name, e)
            _target_model, _target_tokenizer = None, None
            _target_loaded_name = None
            return False


def _load_draft(draft_model_name: str = DEFAULT_DRAFT_MODEL) -> bool:
    """Lazy load draft (3B)，跟 target 共存（14B + 3B ~= 12GB on 32GB Mac）。"""
    global _draft_model, _draft_tokenizer, _draft_loaded_name
    with _load_lock:
        if _draft_model is not None and _draft_loaded_name == draft_model_name:
            return True
        try:
            from mlx_lm import load
        except Exception as e:
            logger.warning("mlx_lm import failed: %s", e)
            return False
        try:
            _draft_model, _draft_tokenizer = load(draft_model_name)
            _draft_loaded_name = draft_model_name
            logger.info("draft model loaded: %s", draft_model_name)
            return True
        except Exception as e:
            logger.warning("draft load failed (%s): %s", draft_model_name, e)
            _draft_model, _draft_tokenizer = None, None
            _draft_loaded_name = None
            return False


def _build_prompt(
    tokenizer: Any,
    user_input: str,
    context: Optional[list[tuple[str, str]]] = None,
    system_prompt: str = "你是個友善的 LINE 群組對話助理咪寶，用繁體中文簡短回覆。",
) -> str:
    """組 chat template prompt。模仿 local_llm.chat 的 schema，保證互換。"""
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    if context:
        for role, text in context[-6:]:  # 最近 3 輪
            messages.append(
                {
                    "role": "user" if role == "user" else "assistant",
                    "content": text,
                }
            )
    messages.append({"role": "user", "content": user_input})
    return tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )


def _generate_plain(
    prompt: str,
    target_model: Any,
    target_tokenizer: Any,
    max_tokens: int = 200,
    prompt_cache: Any = None,
) -> str:
    """純 target 跑 (no draft)。fallback path。"""
    from mlx_lm import generate

    kwargs: dict[str, Any] = {"max_tokens": max_tokens, "verbose": False}
    if prompt_cache is not None:
        kwargs["prompt_cache"] = prompt_cache
    return generate(target_model, target_tokenizer, prompt=prompt, **kwargs)


def generate_with_speculation(
    prompt: str,
    target_model: Optional[str] = None,
    draft_model: Optional[str] = None,
    max_tokens: int = 200,
    num_draft_tokens: int = DEFAULT_NUM_DRAFT_TOKENS,
    prompt_cache: Any = None,
) -> str:
    """Speculative decoding 主入口。

    target_model / draft_model 是「模型名稱字串」（mlx-community 的 repo id）。
    若 env flag OFF / mlx-lm 不支援 / draft 載失敗 → 自動 fallback 純 target。
    回傳 generated text，已 strip。失敗回 ""。
    """
    target_name = target_model or DEFAULT_TARGET_MODEL
    draft_name = draft_model or DEFAULT_DRAFT_MODEL

    # 1. 先確保 target 一定載得起來（沒它就什麼都跑不動）
    if not _load_target(target_name):
        return ""

    # 2. 決定是否走 speculative
    # 注意：probe (_mlx_supports_speculative) 只當 hint，不阻擋實際嘗試。
    # 即使 probe 說不支援，也樂觀地試一次 spec call，靠 TypeError → retry plain，
    # 這樣 mlx-lm 簽名介於新舊版的 corner case 也能正確 fallback。
    use_spec = speculation_enabled()
    if use_spec:
        if not _load_draft(draft_name):
            logger.info("draft load failed, fallback to plain target")
            use_spec = False

    try:
        if use_spec:
            from mlx_lm import generate

            try:
                kwargs: dict[str, Any] = {
                    "max_tokens": max_tokens,
                    "verbose": False,
                    "draft_model": _draft_model,
                    "num_draft_tokens": num_draft_tokens,
                }
                if prompt_cache is not None:
                    kwargs["prompt_cache"] = prompt_cache
                out = generate(
                    _target_model, _target_tokenizer, prompt=prompt, **kwargs
                )
                return (out or "").strip()
            except TypeError as e:
                # mlx-lm 過舊不認 draft_model kwarg
                logger.warning(
                    "speculative kwarg unsupported (%s), fallback plain", e
                )
            except Exception as e:
                logger.warning("speculative generate failed (%s), fallback plain", e)

        # plain fallback
        out = _generate_plain(
            prompt,
            _target_model,
            _target_tokenizer,
            max_tokens=max_tokens,
            prompt_cache=prompt_cache,
        )
        return (out or "").strip()
    except Exception as e:
        logger.warning("generate_with_speculation outer fail: %s", e)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# KV cache 持久化（同 group 連續對話加速）
# ─────────────────────────────────────────────────────────────────────────────


class CachedSession:
    """同 group_id 共用 KV cache，後續 turn 只 encode 增量。

    設計：
    - 第一次 chat()：full prompt → 建 prompt_cache → 走 generate（傳 prompt_cache）
    - 後續 turn：只把「新 user message + assistant 回覆」append 進去（mlx-lm 內部
      在 generate_step 會 reuse cache，自動只前向跑沒看過的 tokens）
    - hit_count 是「第二次以上的 turn」次數，給 test/觀測用
    """

    def __init__(
        self,
        group_id: str,
        target_model: str = DEFAULT_TARGET_MODEL,
        draft_model: Optional[str] = None,
    ) -> None:
        self.group_id = group_id
        self.target_model_name = target_model
        self.draft_model_name = draft_model or DEFAULT_DRAFT_MODEL
        self.prompt_cache: Any = None
        self.turns: int = 0
        self.hit_count: int = 0
        # 上一輪喂進去的「token 數」，給觀測：第二輪以後增量大小應該遠小於 first turn
        self.last_prompt_tokens: int = 0

    def _ensure_cache(self) -> None:
        """第一次用時建 cache。需要 target 已載入。"""
        if self.prompt_cache is not None:
            return
        if _target_model is None:
            return
        try:
            from mlx_lm.models.cache import make_prompt_cache

            self.prompt_cache = make_prompt_cache(_target_model)
        except Exception as e:
            logger.warning("make_prompt_cache failed: %s", e)
            self.prompt_cache = None

    def chat(
        self,
        user_input: str,
        context: Optional[list[tuple[str, str]]] = None,
        system_prompt: str = "你是個友善的 LINE 群組對話助理咪寶，用繁體中文簡短回覆。",
        max_tokens: int = 200,
    ) -> str:
        """跑一次對話。第二輪起 reuse KV cache。"""
        if not _load_target(self.target_model_name):
            return ""
        self._ensure_cache()

        prompt = _build_prompt(
            _target_tokenizer, user_input, context=context, system_prompt=system_prompt
        )
        try:
            self.last_prompt_tokens = len(_target_tokenizer.encode(prompt))
        except Exception:
            self.last_prompt_tokens = -1

        out = generate_with_speculation(
            prompt,
            target_model=self.target_model_name,
            draft_model=self.draft_model_name,
            max_tokens=max_tokens,
            prompt_cache=self.prompt_cache,
        )
        self.turns += 1
        if self.turns >= 2:
            self.hit_count += 1
        return out

    def reset(self) -> None:
        """清掉 cache（換話題 / 太長等情境用）。"""
        self.prompt_cache = None
        self.turns = 0
        self.hit_count = 0
        self.last_prompt_tokens = 0


def get_session(
    group_id: str,
    target_model: str = DEFAULT_TARGET_MODEL,
    draft_model: Optional[str] = None,
) -> CachedSession:
    """取 / 建 group 的 CachedSession（thread-safe）。"""
    with _session_lock:
        sess = _sessions.get(group_id)
        if sess is None or sess.target_model_name != target_model:
            sess = CachedSession(group_id, target_model, draft_model)
            _sessions[group_id] = sess
        return sess


def reset_all_sessions() -> None:
    """主要給 test 用。"""
    with _session_lock:
        _sessions.clear()


def _reset_module_state_for_test() -> None:
    """test only. 把 lazy load 的 model 全部歸零，避免 test 之間污染。"""
    global _draft_model, _draft_tokenizer, _draft_loaded_name
    global _target_model, _target_tokenizer, _target_loaded_name
    _draft_model = None
    _draft_tokenizer = None
    _draft_loaded_name = None
    _target_model = None
    _target_tokenizer = None
    _target_loaded_name = None
    reset_all_sessions()
