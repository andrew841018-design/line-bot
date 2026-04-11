"""
Gemini client wrapper.

兩個功能：
1. chat()：帶著 system prompt + facts + context 呼叫 Gemini，回字串
2. extract_facts()：從最近對話抽取「關於使用者的事實」，回 list[str]
"""
from __future__ import annotations

import json
import logging

import google.generativeai as genai

from config import settings

logger = logging.getLogger(__name__)

genai.configure(api_key=settings.gemini_api_key)

_SYSTEM_PROMPT = """你是一個 LINE 群組助手。請嚴格遵守：
1. 用繁體中文回覆
2. 回覆簡短務實，不要長篇大論（群組氣氛，一兩段就好）
3. 不要裝腔作勢，不要用過多 emoji
4. 如果使用者在閒聊，你也可以閒聊
5. 如果使用者問技術問題，給出具體可操作的答案
6. 如果不知道答案，就說不知道，不要亂編
"""


def _build_system_instruction(facts: list[str]) -> str:
    base = _SYSTEM_PROMPT.strip()
    if not facts:
        return base
    facts_block = "\n".join(f"- {f}" for f in facts)
    return (
        f"{base}\n\n"
        f"你已經知道以下關於使用者的事實（自動從過往對話抽出，請善加利用）：\n"
        f"{facts_block}"
    )


def _to_gemini_history(context: list[tuple[str, str]]) -> list[dict]:
    """把 [(role, text), ...] 轉成 Gemini SDK 吃的格式。
    Gemini SDK 只認 'user' / 'model' 兩種 role。"""
    history = []
    for role, text in context:
        g_role = "user" if role == "user" else "model"
        history.append({"role": g_role, "parts": [text]})
    return history


def chat(user_text: str, context: list[tuple[str, str]], facts: list[str]) -> str:
    """
    主對話入口。
    - user_text：使用者這次的新訊息
    - context：舊的對話歷史（舊→新），**不含** user_text
    - facts：已知的長期事實（會注進 system instruction）
    """
    model = genai.GenerativeModel(
        model_name=settings.gemini_model,
        system_instruction=_build_system_instruction(facts),
    )
    chat_session = model.start_chat(history=_to_gemini_history(context))
    response = chat_session.send_message(user_text)
    return (response.text or "").strip() or "（抱歉我這次沒生出東西，再問一次試試）"


_FACT_EXTRACT_PROMPT = """下面是一段 LINE 群組對話，請從中抽出「關於使用者的長期事實」，
例如：偏好、身份、正在做的專案、技術棧、個人習慣、稱呼……

規則：
1. 只抽「跨對話都會成立」的事實，不要抽「這次對話的即時內容」
2. 每條事實一行、繁體中文、盡量簡短具體
3. 沒抽到就回空陣列 []
4. 嚴格用 JSON 陣列格式回答，不要加任何說明文字、不要 markdown code block

對話：
{dialogue}

只輸出 JSON 陣列，例如：["使用者是 data engineer", "使用者偏好簡短回覆"]"""


def extract_facts(context: list[tuple[str, str]]) -> list[str]:
    """從最近對話抽長期事實，失敗就回空 list（不要 raise）。"""
    if not context:
        return []
    dialogue = "\n".join(
        f"{'使用者' if role == 'user' else '助手'}：{text}" for role, text in context
    )
    prompt = _FACT_EXTRACT_PROMPT.format(dialogue=dialogue)
    try:
        model = genai.GenerativeModel(model_name=settings.gemini_model)
        response = model.generate_content(prompt)
        text = (response.text or "").strip()
        # 去掉可能的 markdown fence
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        facts = json.loads(text)
        if isinstance(facts, list):
            return [str(f).strip() for f in facts if str(f).strip()]
        return []
    except Exception as e:
        logger.warning("extract_facts failed: %s", e)
        return []
