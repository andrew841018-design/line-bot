"""
Grok (xAI) fallback client — OpenAI-compatible API。

當 Gemini 每日 quota 用完時自動接手。
介面與 gemini_client.chat() 相同，方便 main.py 無縫切換。

免費額度：25 req/天（Grok-3-mini）
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI

from config import settings

logger = logging.getLogger(__name__)

_PT = ZoneInfo("America/Los_Angeles")
_USAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grok_usage.json")
_DAILY_REQUEST_LIMIT = 25

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not settings.grok_api_key:
            raise RuntimeError("GROK_API_KEY not set")
        _client = OpenAI(api_key=settings.grok_api_key, base_url="https://api.x.ai/v1")
    return _client


# ── 用量追蹤 ────────────────────────────────────────────────────────────────

def _today_pt() -> str:
    return datetime.now(tz=_PT).strftime("%Y-%m-%d")


def _load_usage() -> dict:
    try:
        with open(_USAGE_FILE) as f:
            data = json.load(f)
        if data.get("date") == _today_pt():
            return data
    except Exception:
        pass
    return {"date": _today_pt(), "requests": 0}


def _save_usage(data: dict) -> None:
    try:
        with open(_USAGE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def quota_exhausted() -> bool:
    data = _load_usage()
    return data.get("requests", 0) >= _DAILY_REQUEST_LIMIT


def get_quota_info() -> dict:
    data = _load_usage()
    used = data.get("requests", 0)
    return {
        "used_requests": used,
        "limit_requests": _DAILY_REQUEST_LIMIT,
        "remaining": max(0, _DAILY_REQUEST_LIMIT - used),
    }


# ── 對話 ─────────────────────────────────────────────────────────────────────

def chat(
    user_input: str,
    context: list[tuple[str, str]],
    facts: list[str],
    persona_notes: list[dict] | None = None,
) -> str:
    """
    Gemini quota 耗盡後的 fallback 對話入口。
    只接受純文字輸入（不支援圖片/音訊）。
    沒有 Google Search grounding，純語言模型回覆。
    """
    if quota_exhausted():
        return ""

    # 重用 gemini_client 的 system prompt
    from gemini_client import _build_system_instruction
    system_text = _build_system_instruction(facts, persona_notes)

    messages: list[dict] = [{"role": "system", "content": system_text}]

    for role, text in context:
        messages.append({
            "role": "user" if role == "user" else "assistant",
            "content": text,
        })

    if isinstance(user_input, str):
        messages.append({"role": "user", "content": user_input})

    try:
        client = _get_client()
        resp = client.chat.completions.create(
            model=settings.grok_model,
            messages=messages,
            temperature=0.7,
        )
        data = _load_usage()
        data["requests"] = data.get("requests", 0) + 1
        _save_usage(data)

        text = (resp.choices[0].message.content or "").strip()
        logger.info("grok reply ok, used %d/%d today", data["requests"], _DAILY_REQUEST_LIMIT)
        return text

    except Exception as e:
        logger.error("grok chat error: %s", e)
        return ""


def group_messages(items: list[dict]) -> list[dict] | None:
    """用 Grok 把 pending 訊息分組（Gemini 完全掛掉時的 fallback）。
    回傳與 _gemini_group_messages 相同格式，失敗回傳 None。
    不計入 quota（用 json_object 格式，cheap 呼叫）。
    """
    if not items or not settings.grok_api_key:
        return None
    try:
        from datetime import datetime as _dt
        lines = []
        for i, it in enumerate(items):
            ts = it.get("timestamp", 0)
            ts_str = _dt.fromtimestamp(ts).strftime("%H:%M") if ts else "??"
            who = (it.get("user_id") or "?")[:8]
            t = it.get("type", "text")
            content = it.get("text", "") if t == "text" else f"[{t}]"
            lines.append(f"[{i}] {ts_str} ({who}) {content[:200]}")

        prompt = (
            "以下是 LINE 群組積累的訊息（依時間順序，格式：[索引] 時間 (用戶) 內容）。\n"
            "把訊息分組，每組對應「值得單獨回覆一次」的對話段落。\n\n"
            "分組規則：\n"
            "1. 同一人連續分段打同一件事 → 合一組\n"
            "2. 不同人討論同一話題 → 合一組\n"
            "3. 話題明顯轉換 → 新的一組\n"
            "4. 一組不超過 8 則\n\n"
            "reply_to：每組中最具代表性的那則索引。\n\n"
            "訊息：\n"
            + "\n".join(lines)
            + '\n\n只回傳 JSON（每個索引恰好出現一次）：\n{"groups":[{"idxs":[int,...], "reply_to": int}, ...]}'
        )

        client = _get_client()
        resp = client.chat.completions.create(
            model=settings.grok_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        import json as _json
        data = _json.loads(resp.choices[0].message.content or "{}")
        groups_raw = data.get("groups", [])

        seen: set[int] = set()
        clean: list[dict] = []
        for g in groups_raw:
            idxs = g.get("idxs") if isinstance(g, dict) else None
            if not isinstance(idxs, list):
                continue
            ok = [i for i in idxs if isinstance(i, int) and 0 <= i < len(items) and i not in seen]
            if not ok:
                continue
            seen.update(ok)
            reply_to = g.get("reply_to")
            if not isinstance(reply_to, int) or reply_to not in ok:
                reply_to = max(ok, key=lambda i: len(items[i].get("text") or ""))
            clean.append({"idxs": ok, "reply_to": reply_to})
        for i in range(len(items)):
            if i not in seen:
                clean.append({"idxs": [i], "reply_to": i})

        logger.info("grok group: %d items → %d groups", len(items), len(clean))
        return clean
    except Exception as e:
        logger.warning("grok group failed: %s", str(e)[:200])
        return None
