"""v4 pipeline Step 7 — self-critique + refine over an existing reply.

對既有 reply 跑兩階段：

1. critique_reply(reply, sources_full_text) → dict
   - 用 Gemini light model 跑 critique prompt：列出 reply 中的 factual claim、
     對每 claim 標 supported / contradicted / unsupported，找 sources 矛盾、
     列出 missing facts（sources 有提但 reply 沒寫）。
   - 失敗（429 / quota 爆 / parse 失敗）→ fallback 到本機 14B
     （local_llm.chat），prompt 同；JSON parse 仍失敗 → 回 graceful empty schema。

2. refine_reply(reply, critique, sources_full_text) → str
   - 根據 critique 重寫 reply：
       a. 砍掉 contradicted / unsupported claims
       b. 補上 missing_facts
       c. 標出 contradictions 並表態
       d. 保留原結構（正反方 + 整合 + actionable + 來源 URL）
       e. 比原 reply 豐富 1.5x
   - Gemini → 14B fallback；兩條都爆 → 直接回原 reply（不阻塞）。

主流程整合（main.py 之後可以這樣串）：

    from self_critique import critique_reply, refine_reply
    c = critique_reply(reply, sources_full_text)
    refined = refine_reply(reply, c, sources_full_text)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ── JSON schemas (回 caller 看的契約) ────────────────────────────────────────
# critique_reply 輸出：
#   {
#     "claims": [
#       {"claim": "...", "verdict": "supported"|"contradicted"|"unsupported",
#        "evidence": "<source url or quote, 可選>"}
#     ],
#     "contradictions": [
#       {"summary": "source A 說 X，source B 說 Y",
#        "sources": ["url_a", "url_b"]}
#     ],
#     "missing_facts": [
#       {"fact": "...", "source": "<url>"}
#     ]
#   }
EMPTY_CRITIQUE: dict[str, list[Any]] = {
    "claims": [],
    "contradictions": [],
    "missing_facts": [],
}


# ── Prompts ────────────────────────────────────────────────────────────────
_CRITIQUE_PROMPT = """下面是一段 LINE 群 bot 對某張圖的回覆，以及我搜到的 N 條 sources（含 full text）。
請逐 claim 檢查：

【Reply】
{reply}

【Sources（含完整內容）】
{full_text_block}

任務：
1. 列出 reply 中的所有具體 factual claim（人名/數字/日期/事件）
2. 對每 claim 標記是否被 sources 真實支持：「supported」/「contradicted」/「unsupported」（sources 沒提）
3. 找出 sources 之間的矛盾點（如有）
4. 找出 reply 缺少的重要 fact（sources 有提但 reply 沒寫）

輸出 JSON：{{"claims": [{{"claim": "...", "verdict": "supported|contradicted|unsupported", "evidence": "..."}}], "contradictions": [{{"summary": "...", "sources": ["url_a", "url_b"]}}], "missing_facts": [{{"fact": "...", "source": "..."}}]}}
只回 JSON，不要 markdown / code fence。
"""

_REFINE_PROMPT = """根據下面 critique，重寫上面 reply：
1. 砍掉 contradicted / unsupported claims
2. 補上 missing_facts
3. 標出 contradictions 並表態
4. 保持原結構（正反方+整合+actionable+來源 URL）
5. 輸出比原 reply 豐富 1.5x（更多具體 fact / 數字 / 人名）

⚠️ 硬性規則 — 不可違反：
- **必須在最後保留來源 URL 段**：列至少 5 條 URL，格式 `機構名 https://URL`，每條一行
- URL 從 Full sources 直接複製（不編造、不短化、不省略）
- 即使原 reply 沒列來源段，refined 版本也必須加上
- 來源段是分開段落，不要嵌進敘述體裡

【原 reply】
{reply}

【Critique】
{critique_json}

【Full sources】
{full_text_block}

直接給 refined reply。不要 markdown code fence、不要前言。
記得：最後必須有「來源：」段含至少 5 條真實 URL。
"""


# ── helpers ───────────────────────────────────────────────────────────────
def _format_sources_block(sources_full_text: list[dict] | None) -> str:
    """把 sources 排成「【n】title (url)\n<full_text>」blocks。空 → 空字串。"""
    if not sources_full_text:
        return "（無 sources）"
    blocks: list[str] = []
    for i, s in enumerate(sources_full_text, 1):
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or "").strip()
        url = str(s.get("url") or "").strip()
        text = str(s.get("text") or s.get("full_text") or s.get("content") or "").strip()
        head = f"【{i}】{title}".rstrip()
        if url:
            head += f" ({url})"
        body = text[:4000]  # 防超長
        blocks.append(f"{head}\n{body}")
    return "\n\n".join(blocks) if blocks else "（無 sources）"


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _strip_code_fence(s: str) -> str:
    """剝掉 ```json ... ``` 圍欄。"""
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _extract_json_blob(s: str) -> str:
    """從文字中盡力抓出第一段 {...} 物件。失敗回原字串。"""
    s = _strip_code_fence(s)
    if not s:
        return s
    # 直接 parse 看看
    try:
        json.loads(s)
        return s
    except Exception:
        pass
    # fallback：找第一個 '{' 跟對應 '}'（粗略 brace match）
    start = s.find("{")
    if start < 0:
        return s
    depth = 0
    for i in range(start, len(s)):
        ch = s[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return s


def _parse_critique_json(text: str) -> dict[str, list[Any]]:
    """robust JSON parse — 失敗回 EMPTY_CRITIQUE 拷貝。"""
    if not text:
        return {**EMPTY_CRITIQUE}
    blob = _extract_json_blob(text)
    try:
        data = json.loads(blob)
    except Exception as e:
        logger.warning("critique JSON parse failed: %s; raw head=%r", e, text[:200])
        return {**EMPTY_CRITIQUE}
    if not isinstance(data, dict):
        return {**EMPTY_CRITIQUE}
    return _normalize_critique(data)


def _normalize_critique(data: dict) -> dict[str, list[Any]]:
    """把外部 JSON 強制套上我們約定的 schema（缺 key 補空 list）。"""
    out: dict[str, list[Any]] = {**EMPTY_CRITIQUE}

    claims_raw = data.get("claims") or []
    if isinstance(claims_raw, list):
        norm_claims: list[dict] = []
        for c in claims_raw:
            if not isinstance(c, dict):
                continue
            claim = str(c.get("claim") or "").strip()
            verdict = str(c.get("verdict") or "").strip().lower()
            if verdict not in ("supported", "contradicted", "unsupported"):
                # 兼容拼字／簡寫
                if "support" in verdict:
                    verdict = "supported"
                elif "contradict" in verdict or "conflict" in verdict:
                    verdict = "contradicted"
                else:
                    verdict = "unsupported"
            evidence = str(c.get("evidence") or "").strip()
            if claim:
                norm_claims.append(
                    {"claim": claim, "verdict": verdict, "evidence": evidence}
                )
        out["claims"] = norm_claims

    contras_raw = data.get("contradictions") or []
    if isinstance(contras_raw, list):
        norm_contras: list[dict] = []
        for ct in contras_raw:
            if not isinstance(ct, dict):
                continue
            summary = str(ct.get("summary") or "").strip()
            srcs = ct.get("sources") or []
            if isinstance(srcs, list):
                srcs = [str(x).strip() for x in srcs if x]
            else:
                srcs = []
            if summary:
                norm_contras.append({"summary": summary, "sources": srcs})
        out["contradictions"] = norm_contras

    missing_raw = data.get("missing_facts") or []
    if isinstance(missing_raw, list):
        norm_missing: list[dict] = []
        for m in missing_raw:
            if not isinstance(m, dict):
                # 也許是純字串
                if isinstance(m, str) and m.strip():
                    norm_missing.append({"fact": m.strip(), "source": ""})
                continue
            fact = str(m.get("fact") or "").strip()
            source = str(m.get("source") or "").strip()
            if fact:
                norm_missing.append({"fact": fact, "source": source})
        out["missing_facts"] = norm_missing

    return out


# ── LLM calls (Gemini → 14B fallback) ────────────────────────────────────
_QUOTA_SIGS = ("429", "RESOURCE_EXHAUSTED", "quota", "exhausted", "PerDay")


def _is_quota_error(err: Exception) -> bool:
    msg = str(err)
    return any(sig.lower() in msg.lower() for sig in _QUOTA_SIGS)


def _call_gemini(prompt: str, *, json_mode: bool) -> str | None:
    """Gemini light model。失敗（含 quota 爆）→ None；caller 再走 14B。

    SELF_CRITIQUE_FORCE_LOCAL=1 → 直接 skip Gemini（圖片 100% 本機 policy）。
    """
    import os as _os
    if _os.environ.get("SELF_CRITIQUE_FORCE_LOCAL") == "1":
        return None  # 強制走 14B
    try:
        import gemini_client
        from google.genai import types  # type: ignore
        from config import settings  # type: ignore
    except Exception as e:
        logger.warning("gemini_client/genai import failed: %s", e)
        return None

    try:
        cfg_kwargs: dict[str, Any] = {"temperature": 0.2}
        if json_mode:
            cfg_kwargs["response_mime_type"] = "application/json"
        # thinking budget=0：快速、便宜
        try:
            cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        except Exception:
            pass
        config = types.GenerateContentConfig(**cfg_kwargs)
        resp = gemini_client._client.models.generate_content(
            model=settings.gemini_light_model,
            contents=prompt,
            config=config,
        )
        text = getattr(resp, "text", None) or ""
        return text.strip() or None
    except Exception as e:
        if _is_quota_error(e):
            logger.info("Gemini quota/429 — fallback to 14B: %s", str(e)[:200])
        else:
            logger.warning("Gemini critique call failed: %s", str(e)[:200])
        return None


def _call_local_14b(prompt: str, *, max_tokens: int = 1200) -> str | None:
    """本機 14B（local_llm.chat）後備。失敗 → None。

    我們把 system_prompt 跟 user_input 對齊：critique / refine prompt 已自帶
    完整 instruction，這邊再強調「直接出輸出」避免 14B 加冗言。
    """
    try:
        import local_llm  # type: ignore
    except Exception as e:
        logger.info("local_llm 不可用：%s", e)
        return None

    sys_prompt = (
        "你是嚴謹的事實檢核 / 改寫助手。"
        "依使用者指示直接輸出結果，不要重述任務、不要加 markdown、不要加前言。"
    )
    try:
        out = local_llm.chat(prompt, system_prompt=sys_prompt, max_tokens=max_tokens)
        if not out:
            return None
        return str(out).strip() or None
    except Exception as e:
        logger.warning("local_llm.chat failed: %s", str(e)[:200])
        return None


# ── public API ────────────────────────────────────────────────────────────
def critique_reply(
    reply: str, sources_full_text: list[dict] | None
) -> dict[str, list[Any]]:
    """跑 critique。Gemini 主路 → 14B fallback。失敗 → graceful empty schema。

    回傳 schema 永遠是 dict[str, list]，含 keys：claims / contradictions / missing_facts。
    每個 claim：{"claim": str, "verdict": "supported|contradicted|unsupported",
                 "evidence": str}
    """
    if not reply or not reply.strip():
        return {**EMPTY_CRITIQUE}

    full_text_block = _format_sources_block(sources_full_text)
    prompt = _CRITIQUE_PROMPT.format(
        reply=reply.strip(),
        full_text_block=full_text_block,
    )

    # Tier 1: Gemini light
    raw = _call_gemini(prompt, json_mode=True)
    if raw:
        parsed = _parse_critique_json(raw)
        # Gemini 給了東西、但 parse 完全失敗（三 list 全空）→ 試 14B
        if any(parsed.get(k) for k in ("claims", "contradictions", "missing_facts")):
            return parsed
        logger.info("Gemini critique 解析後全空，嘗試 14B fallback")

    # Tier 2: 本機 14B
    raw_local = _call_local_14b(prompt, max_tokens=1500)
    if raw_local:
        parsed = _parse_critique_json(raw_local)
        return parsed

    logger.warning("critique_reply: 兩路 LLM 皆失敗，回 empty schema")
    return {**EMPTY_CRITIQUE}


def refine_reply(
    reply: str,
    critique: dict[str, list[Any]] | None,
    sources_full_text: list[dict] | None,
) -> str:
    """根據 critique 改寫 reply。Gemini → 14B fallback。

    全失敗時回原 reply（不阻塞主流程）。
    """
    if not reply or not reply.strip():
        return reply or ""

    crit = critique or {**EMPTY_CRITIQUE}
    # 確保 dump 出來的是合法 JSON
    try:
        critique_json = json.dumps(crit, ensure_ascii=False, indent=2)
    except Exception:
        critique_json = json.dumps(EMPTY_CRITIQUE, ensure_ascii=False)

    full_text_block = _format_sources_block(sources_full_text)
    prompt = _REFINE_PROMPT.format(
        reply=reply.strip(),
        critique_json=critique_json,
        full_text_block=full_text_block,
    )

    # Tier 1: Gemini light
    raw = _call_gemini(prompt, json_mode=False)
    if raw:
        cleaned = _strip_code_fence(raw)
        if cleaned and len(cleaned) > 10:
            return cleaned

    # Tier 2: 本機 14B
    raw_local = _call_local_14b(prompt, max_tokens=1500)
    if raw_local:
        cleaned = _strip_code_fence(raw_local)
        if cleaned and len(cleaned) > 10:
            return cleaned

    logger.warning("refine_reply: 兩路 LLM 皆失敗，回原 reply")
    return reply
