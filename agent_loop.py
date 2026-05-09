"""ReAct-style agent loop — 讓 local LLM (Qwen 等) 也能 call tools。

設計理由：
- mlx-lm 的 Qwen 對 OpenAI native tool-calling format 支援未必完整
- 改用 prompt-based ReAct：LLM 每輪固定回 JSON
    - {"action": "TOOL_NAME", "args": {...}, "thought": "..."}  → 要呼叫工具
    - {"final": "答案"}                                          → 終止 loop
- 路由層 parse JSON → call tool → observation 餵回 LLM 第二輪 → 直到 final
  或達到 _MAX_ITERATIONS（防 infinite loop）

Robustness：
- LLM 可能回非 JSON / JSON 包夾在 markdown / 自由文字 → _safe_parse_json 多重 fallback
- 解析失敗 → 視為「LLM 已給最終答案」，整段 raw 當 final return（degrade gracefully）
- tool 失敗仍回字串 observation（在 agent_tools.call_tool 那層保證）

主要對外介面：
    Agent(llm_chat=local_llm.chat).run(user_msg, context=None) -> str

LLM signature 對齊 local_llm.chat（user_input, context, system_prompt, max_tokens）。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Optional

from agent_tools import call_tool, list_tools_for_prompt

logger = logging.getLogger("agent_loop")

_MAX_ITERATIONS = 5
_MAX_OBSERVATION_LEN = 1500  # 餵回 LLM 的 observation 上限


# ReAct system prompt — 給 local LLM 的嚴格格式引導
_SYSTEM_PROMPT_TEMPLATE = """你是個有工具的 LINE 對話助理。今天是 LINE 群組的對話場景，使用者用繁體中文。

可用工具：
{tools}

每輪你**必須**只回**單一 JSON 物件**（不要其他文字、不要 markdown code block、不要解釋）：

格式 A — 需要呼叫工具：
{{"action": "TOOL_NAME", "args": {{...}}, "thought": "為什麼要叫這個工具"}}

格式 B — 已經能回答：
{{"final": "給使用者的最終答案（繁體中文）"}}

範例：

User: 台積電現在多少
You: {{"action": "get_stock_price", "args": {{"text": "台積電"}}, "thought": "查即時報價"}}
Tool result (get_stock_price): 2330.TW (台積電): 1245.0
You: {{"final": "台積電現在 1245 元（即時）"}}

User: 你好
You: {{"final": "嗨！有什麼能幫你的？"}}

User: 100 美金換台幣
You: {{"action": "get_forex", "args": {{"text": "100 美金換台幣"}}, "thought": "查匯率"}}
Tool result (get_forex): 100 USD = 3,200 TWD
You: {{"final": "100 美金約 3,200 台幣（即時匯率）"}}

規則：
1. 不要重複呼叫同一個工具（如果上一輪已 call 且拿到 result，就該給 final）
2. 只能查事實 / 即時資料用工具；閒聊 / 解釋類直接給 final
3. 工具回 「(...)」開頭的錯誤訊息 → 換策略或直接 final 告訴使用者查不到
4. 第 5 輪一定要 final，不再 call tool
"""


class Agent:
    """ReAct agent。state 不持久化，每次 run() 都是獨立 loop。"""

    def __init__(
        self,
        llm_chat: Optional[Callable] = None,
        max_iterations: int = _MAX_ITERATIONS,
    ):
        """
        Args:
            llm_chat: 真正的 LLM chat callable，signature 比照
                local_llm.chat(user_input, context, system_prompt, max_tokens)。
                None → lazy load local_llm.chat。
            max_iterations: ReAct loop 最多幾輪（含最後一輪 final）。
        """
        self._llm_chat = llm_chat  # None 時 _ensure_llm 才 lazy load
        self.max_iterations = max(1, int(max_iterations))

    # ── public ──────────────────────────────────────────────────────────────

    def run(
        self,
        user_msg: str,
        context: list | None = None,
    ) -> str | None:
        """跑 ReAct loop，回最終 string answer。失敗回 None。"""
        if not user_msg or not user_msg.strip():
            return None
        chat = self._ensure_llm()
        if chat is None:
            return None

        system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(tools=list_tools_for_prompt())
        # ReAct 會把上下文 user/assistant 傳給 LLM；我們從外層 context 起步，
        # 每輪 append (assistant 的 JSON) + (user 的 tool result)
        history: list[tuple[str, str]] = list(context or [])
        history.append(("user", user_msg))

        last_action_signature: str | None = None
        for step in range(self.max_iterations):
            is_last = (step == self.max_iterations - 1)

            llm_user_msg = self._build_loop_user_msg(user_msg, history, is_last)
            try:
                raw = chat(
                    llm_user_msg,
                    context=history[:-1],  # 不含本次 user msg（因為包進 llm_user_msg）
                    system_prompt=system_prompt,
                    max_tokens=400,
                )
            except TypeError:
                # 某些 stub 不接 system_prompt / max_tokens
                try:
                    raw = chat(llm_user_msg, context=history[:-1])
                except Exception as e:
                    logger.warning("llm_chat call failed: %s", e)
                    return None
            except Exception as e:
                logger.warning("llm_chat raised: %s", e)
                return None

            if not raw:
                logger.info("agent step %d: LLM 回空", step)
                return None

            parsed = self._safe_parse_json(raw)

            # final → 終止
            if isinstance(parsed, dict) and "final" in parsed:
                final = str(parsed["final"]).strip()
                if not final:
                    return None
                # 2026-05-09 整合：local_nli + grounding_local 對 final 做 fact-check
                # 純本機，分數低時加警示但不阻塞
                if os.environ.get("AGENT_GROUNDING_DISABLED") != "1":
                    sources_text = [
                        t for r, t in history if r == "user" and t.startswith("[")
                    ]
                    if sources_text:
                        try:
                            import grounding_local
                            score = grounding_local.score_response(final, sources_text)
                            avg = score.get("score_avg", 1.0) if score else 1.0
                            if avg < 0.3:
                                logger.warning(
                                    "agent grounding low: avg=%.2f", avg
                                )
                                final += "\n\n（部分敘述可能與來源不一致，請查證）"
                        except (ImportError, Exception) as e:
                            logger.debug("grounding_local skipped: %s", e)
                return final

            # action → call tool
            if isinstance(parsed, dict) and "action" in parsed:
                if is_last:
                    # 最後一輪不能再 call tool；當作 final 把 raw 拋出（含 thought 也算交代）
                    fallback = parsed.get("thought") or parsed.get("final")
                    return str(fallback) if fallback else None

                tool_name = str(parsed.get("action", "")).strip()
                tool_args = parsed.get("args") or {}
                if not isinstance(tool_args, dict):
                    tool_args = {}

                # 防卡死：偵測重複 call 同 tool 同 args
                sig = f"{tool_name}|{json.dumps(tool_args, sort_keys=True, ensure_ascii=False)}"
                if sig == last_action_signature:
                    logger.info("agent: 重複 action %s，提早終止", sig)
                    # 把累積的 observation 當 fallback final（其實已沒了；給 None 讓 router 走下個 tier）
                    return None
                last_action_signature = sig

                observation = call_tool(tool_name, tool_args)
                if len(observation) > _MAX_OBSERVATION_LEN:
                    observation = observation[:_MAX_OBSERVATION_LEN] + "…（截斷）"

                # 餵回對話：assistant = 剛才那段 JSON（純 JSON，不重 wrap），user = tool result
                history.append(("assistant", json.dumps(parsed, ensure_ascii=False)))
                history.append((
                    "user",
                    f"Tool result ({tool_name}): {observation}",
                ))
                continue

            # 既非 final 也非 action → LLM 沒遵守格式；當作 final 直接 return raw
            logger.info("agent step %d: LLM 不遵守 JSON 格式，直接 return raw", step)
            stripped = raw.strip()
            return stripped or None

        return None

    # ── internals ───────────────────────────────────────────────────────────

    def _ensure_llm(self) -> Callable | None:
        if self._llm_chat is not None:
            return self._llm_chat
        try:
            from local_llm import chat as local_chat  # lazy
            self._llm_chat = local_chat
            return local_chat
        except ImportError:
            logger.info("local_llm 不可用，agent 無法跑")
            return None
        except Exception as e:
            logger.warning("local_llm import failed: %s", e)
            return None

    @staticmethod
    def _build_loop_user_msg(
        user_msg: str,
        history: list[tuple[str, str]],
        is_last: bool,
    ) -> str:
        """組第 N 輪要傳給 LLM 的 user content。

        第 0 輪（history 只有原始 user_msg）→ 直接給 user_msg。
        第 N>0 輪（history 有 tool round-trip）→ 提示「依上面 tool 結果回 JSON」。
        is_last → 強調必須給 final。
        """
        # history 結尾應為最近一筆 user (Tool result) 或 user_msg 本身
        # 第一輪：history 末筆 = user_msg；後續輪：末筆 = Tool result
        if len(history) <= 1:
            return user_msg
        last_role, last_text = history[-1]
        if is_last:
            return (
                f"{last_text}\n\n"
                "（這是最後一輪，請直接回 {\"final\": \"...\"} 給使用者答案，"
                "不要再呼叫工具）"
            )
        return (
            f"{last_text}\n\n"
            "請依上述 tool 結果回 JSON：能回答就 {\"final\": \"...\"}，"
            "需要再查就 {\"action\": \"...\", \"args\": {...}}。"
        )

    @staticmethod
    def _safe_parse_json(raw: str) -> Any:
        """寬鬆 JSON parse — 處理 LLM 常見不乾淨輸出。

        嘗試順序：
          1. 整段 strip 後直接 json.loads
          2. 抽出第一個 {...} 區塊（greedy 配對 brace）後 json.loads
          3. 移除 markdown ```json fence
          4. 失敗 → 回原 raw（caller 視為非 JSON，當 final）
        """
        if not raw:
            return None
        s = raw.strip()

        # 1. 直接試
        try:
            return json.loads(s)
        except Exception:
            pass

        # 2. markdown code fence
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
        if fence:
            try:
                return json.loads(fence.group(1))
            except Exception:
                pass

        # 3. 第一個 balanced {...}
        start = s.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(s)):
                ch = s[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = s[start : i + 1]
                        try:
                            return json.loads(candidate)
                        except Exception:
                            break

        # 4. 給回原 raw 字串（caller 視為非 JSON）
        return raw


__all__ = ["Agent"]
