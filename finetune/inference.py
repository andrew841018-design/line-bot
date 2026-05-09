"""
inference.py — Fine-tuned model inference stub。

目的：
- 提供 `FineTunedAgent.chat(...)`，介面跟 `gemini_client.chat(...)` 相容
- 目前**未真載入 model**，回 placeholder 訊息；訓完之後再實裝 mlx_lm / peft 載入
- 讓 main.py 之後可以做 swap：先試本地 fine-tuned，失敗 fallback Gemini

未來實裝步驟（請看下方 NotImplementedError 註解）：
1. pip install mlx-lm（mac）或 transformers + peft（cuda）
2. 在 _load_model 裡讀 lora_config.yaml 的 adapter path
3. _generate 用 mlx_lm.generate / model.generate
"""

from __future__ import annotations

import os
from typing import Any, Iterable


# 未實裝模型時固定回這個訊息（讓使用者立刻看出 fallback 沒接好）
_NOT_TRAINED_MSG = (
    "fine-tuned model 尚未 train，請先跑 train_lora.sh 並把 adapter 放進 "
    "outputs/。在那之前 main.py 會自動 fallback 回 Gemini。"
)


class FineTunedAgent:
    """
    跟 gemini_client.chat() 介面相容的本地 fine-tuned agent。

    使用：
        agent = FineTunedAgent(config_path="lora_config.yaml")
        reply = agent.chat(
            user_input="今天天氣怎樣？",
            context=[("user", "早安"), ("bot", "早～")],
            facts=["使用者住高雄"],
        )

    fallback 模式：
        - 沒 adapter 檔 → ready=False，chat() 直接回 _NOT_TRAINED_MSG
        - main.py 應檢查 ready 屬性決定要不要 swap
    """

    def __init__(
        self,
        config_path: str | None = None,
        backend: str = "auto",
    ) -> None:
        """
        Args:
            config_path: lora_config.yaml 路徑；None 時用預設位置
            backend: "auto" / "mac_mlx" / "cuda"；auto 看作業系統猜
        """
        self.config_path = config_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "lora_config.yaml"
        )
        self.backend = self._resolve_backend(backend)
        self.ready = False  # 未載入 model 前 always False
        self._model = None
        self._tokenizer = None

        # 嘗試載入 — 失敗就保持 ready=False（不 raise，讓上游 fallback）
        try:
            self._load_model()
        except NotImplementedError:
            # 預期狀態：還沒實裝
            self.ready = False
        except FileNotFoundError:
            # adapter 檔不存在 — 預期狀態之一（還沒 train）
            self.ready = False
        except Exception:
            # 其他例外（讀 config 失敗等）— 同樣 fallback，不擋主流程
            self.ready = False

    def _resolve_backend(self, backend: str) -> str:
        if backend != "auto":
            return backend
        # 簡單偵測：mac → mlx，其他 → cuda
        import platform

        if platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64"):
            return "mac_mlx"
        return "cuda"

    def _load_model(self) -> None:
        """
        載入 model + adapter。

        TODO（實裝時）：
        - mac_mlx：
            from mlx_lm import load
            self._model, self._tokenizer = load(base_model, adapter_path=adapter_path)
        - cuda：
            from transformers import AutoModelForCausalLM, AutoTokenizer
            from peft import PeftModel
            self._tokenizer = AutoTokenizer.from_pretrained(base_model)
            base = AutoModelForCausalLM.from_pretrained(base_model, torch_dtype="bf16", device_map="auto")
            self._model = PeftModel.from_pretrained(base, adapter_path)
        - 兩條 path 都要先 yaml.safe_load(self.config_path) 讀 model / adapter 路徑
        - 確認 adapter 檔存在 → set self.ready = True
        """
        # 目前 stub：直接 raise，讓 __init__ 的 except 把 ready 鎖在 False
        raise NotImplementedError("fine-tune adapter loader 未實裝")

    def chat(
        self,
        user_input: str,
        context: list[tuple[str, str]] | None = None,
        facts: list[str] | None = None,
        persona_notes: list[dict] | None = None,
        group_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        """
        跟 gemini_client.chat() 完全相容的呼叫介面。

        Args:
            user_input: 這次的新訊息
            context: 舊對話歷史 list[(role, text)]，role ∈ {"user", "bot"}
            facts: 長期事實，會 inline 進 system prompt
            persona_notes: 人設範例（目前 stub 不用，未來可以注 system prompt）
            group_id: 選填，未實裝時忽略
            **kwargs: 吞掉未來新增的 kwargs，避免 caller 升級時打到舊 stub

        Returns:
            字串回覆。未 ready 時固定回 _NOT_TRAINED_MSG。
        """
        if not self.ready:
            return _NOT_TRAINED_MSG

        # ── 真載入後的流程（占位）────────────────────────────────────
        prompt = self._build_prompt(user_input, context or [], facts or [])
        return self._generate(prompt)

    def _build_prompt(
        self,
        user_input: str,
        context: list[tuple[str, str]],
        facts: list[str],
    ) -> str:
        """組 chat-template prompt（套 base model 的 tokenizer.apply_chat_template）。"""
        messages: list[dict[str, str]] = []
        if facts:
            sys_text = "你是 LINE 群的助理咪寶。\n背景事實：\n" + "\n".join(
                f"- {x}" for x in facts
            )
            messages.append({"role": "system", "content": sys_text})
        for role, text in context:
            mapped = "assistant" if role == "bot" else "user"
            messages.append({"role": mapped, "content": text})
        messages.append({"role": "user", "content": user_input})

        if self._tokenizer is None:
            # ready=True 時這分支不該走到；當保險
            return user_input
        return self._tokenizer.apply_chat_template(  # type: ignore[no-any-return]
            messages, tokenize=False, add_generation_prompt=True
        )

    def _generate(self, prompt: str) -> str:
        """實際 forward；待實裝。"""
        raise NotImplementedError("generate 未實裝")


# ── 模組層 helper（跟 gemini_client.chat 函式介面一致）─────────────────
_default_agent: FineTunedAgent | None = None


def chat(
    user_input: str,
    context: list[tuple[str, str]] | None = None,
    facts: Iterable[str] | None = None,
    persona_notes: list[dict] | None = None,
    group_id: str | None = None,
) -> str:
    """
    模組層 chat()，跟 gemini_client.chat 介面相同。

    用法：
        from finetune import inference as ft
        reply = ft.chat(user_input, context, facts)
    """
    global _default_agent
    if _default_agent is None:
        _default_agent = FineTunedAgent()
    return _default_agent.chat(
        user_input=user_input,
        context=context,
        facts=list(facts) if facts else None,
        persona_notes=persona_notes,
        group_id=group_id,
    )


if __name__ == "__main__":
    # 簡單 smoke test：import 不爆 + chat 回 placeholder
    agent = FineTunedAgent()
    print(f"backend={agent.backend} ready={agent.ready}")
    print(agent.chat("測試訊息"))
