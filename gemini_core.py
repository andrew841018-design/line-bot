"""gemini_core — shared constants for gemini_client + rag_graph.

Phase 2B.2.1: extracted to break circular import between gemini_client and
rag_graph (which both need NEWS_CASE regex / rule text). API stable;
gemini_client.py re-exports for backwards compat with 15 existing importers.

DO NOT add stateful objects here (e.g. genai.Client). Module-level state
belongs in gemini_client until Phase 2B.2.3.
"""
from __future__ import annotations

import re
from typing import TypedDict

__all__ = [
    "_RULE_NEWS_CASE",
    "_NEWS_CASE_RE",
    # Phase 2B.2.2 — text utilities (pure, no module state)
    "_CITE_RE",
    "_URL_IN_TEXT_RE",
    "_clean_reply",
    "_extract_grounding_urls",
    "_append_sources",
    "_is_chinese_majority",
    "_count_zh_chars",
    # Phase 2B.5 — shared constants + typed kwargs for gemini_client._run
    "MIN_RECALL_LEN",
    "_RunKwargs",
]


# Phase 2B.5: minimum user_text strip-length to fire semantic recall +
# case-pair retrieval. Shared between inline gemini_client.chat() and
# rag_graph nodes (_node_semantic_retrieve, _node_case_retrieve,
# _route_after_semantic) — single source of truth prevents future
# re-divergence between the two paths.
MIN_RECALL_LEN: int = 4


class _RunKwargs(TypedDict):
    """Phase 2B.5: typed kwargs contract for `gemini_client._run`.

    Mirrors `_run`'s signature exactly (7 keyword-only args after `model`).
    Update both definitions atomically — this dict IS the contract surface.

    total=True (default): every call site must populate all 7 keys.
    Substitutes for the previous `dict(...)` builder pattern that mypy
    could not narrow at `**kwargs` call sites.
    """
    user_input: object  # MessageInput = str | types.Part | list (multi-modal)
    context: list[tuple[str, str]]
    facts: list[str]
    persona_notes: list[dict] | None
    recall_hits: list[dict] | None
    case_hits: list[dict] | None
    group_id: str | None


_RULE_NEWS_CASE = """【新聞／案例／研究／專業議題】
先查證實際主張，第一句直接給具體判斷，補充必要事實、風險或可執行建議。
不要重述貼文、空泛附和、編造數字；保留關鍵條件及不確定性。
不強制論點數、正反方、來源清單或最低字數。使用者明確要求才展開正反方或列來源。
"""


# 新版輸出規格會在 gemini_client 組裝 system instruction 時最後追加，
# 這裡保留既有常數名稱與偵測 regex，避免破壞 import 相容性。
# 新聞 / 案例 / 研究 / 專業議題（覆蓋政治以外的「需要觀點」場景）
_NEWS_CASE_RE = re.compile(
    r"新聞|報導|案例|個案|研究|論文|文章|貼文|"
    r"保險|保單|投保|理賠|"
    r"投資|股市|理財|"
    r"醫療|醫生|醫院|疫苗|藥物|手術|症狀|病|健康|養生|"
    r"教育|升學|考試|教改|"
    r"法律|法案|法條|判決|訴訟|"
    r"消費|商品|品牌|"
    r"房地產|房市|房價|租屋|"
    r"AI|人工智慧|詐騙|騙局|"
    r"分析|評論|心得|解析"
)


# ════════════════════════════════════════════════════════════════════════════
# Phase 2B.2.2 — pure text helpers (extracted 2026-05-19)
# ════════════════════════════════════════════════════════════════════════════

# Gemini grounding citation tags 對 LINE 使用者沒意義要清掉
_CITE_RE = re.compile(r"\[cite:\w+\]|\[BROWSING_TOOL_\d+\]")

_URL_IN_TEXT_RE = re.compile(r"https?://\S+")


def _clean_reply(text: str) -> str:
    """清除 Gemini 回覆中的 citation 標籤。"""
    text = _CITE_RE.sub("", text)
    text = re.sub(r"  +", " ", text)
    return text.strip()


def _extract_grounding_urls(response) -> list[tuple[str, str]]:
    """從 response.candidates[0].grounding_metadata 抽出 (uri, title) 清單。"""
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return []
        meta = getattr(candidates[0], "grounding_metadata", None)
        if meta is None:
            return []
        chunks = getattr(meta, "grounding_chunks", None) or []
        seen: set[str] = set()
        result = []
        for chunk in chunks:
            web = getattr(chunk, "web", None)
            if web is None:
                continue
            uri = (getattr(web, "uri", None) or "").strip()
            title = (getattr(web, "title", None) or "").strip()
            if uri and uri not in seen:
                seen.add(uri)
                result.append((uri, title))
        return result
    except Exception:
        return []


def _append_sources(
    text: str, urls: list[tuple[str, str]], *, user_text: str = ""
) -> str:
    """Append available grounding links only for an explicit source request."""
    if not text.strip() or not urls:
        return text
    # Evaluate request clauses separately so a negated format preference does
    # not cancel an explicit source request in a later clause.
    wants_sources = False
    for clause in re.split(r"[，,。；;!?\n]|\bbut\b", user_text, flags=re.IGNORECASE):
        if re.search(
            r"(?:不用|不必|不要|不需|無需)[^，,。；;]{0,8}(?:來源|出處|連結|網址)"
            r"|\b(?:no|without|omit|exclude|do not|don't|dont)\b.{0,25}\b(?:sources?|citations?|references?|links?)\b",
            clause, re.IGNORECASE,
        ):
            wants_sources = False
        elif re.search(
            r"(?:請|給我|提供|列出|附上|附|查看|想看).{0,8}(?:來源|出處|參考資料|網址|連結)"
            r"|(?:來源|出處)(?:呢|在哪|是什麼|是甚麼)"
            r"|^\s*(?:來源|出處|參考資料)[？?]?\s*$"
            r"|\b(?:include|provide|show|list|cite|give|add|attach)\b.{0,20}\b(?:sources?|citations?|references?|links?)\b"
            r"|\b(?:what|where)\b.{0,8}\b(?:are|is)\b.{0,10}\b(?:sources?|references?)\b"
            r"|^\s*(?:sources?|citations?|references?)(?:\s+please)?\s*$",
            clause, re.IGNORECASE,
        ):
            wants_sources = True
    if not wants_sources or _URL_IN_TEXT_RE.search(text):
        return text
    links = []
    seen = set()
    for url, title in urls:
        if not url.startswith(("https://", "http://")) or url in seen:
            continue
        seen.add(url)
        links.append(f"{title.strip() or '來源'} {url}")
        if len(links) == 3:
            break
    return text + "\n\n來源：\n" + "\n".join(links) if links else text


def _is_chinese_majority(text: str) -> bool:
    """中文字元數 >= 英文字母數才算中文為主。"""
    cn = len(re.findall(r"[一-鿿]", text))
    en = len(re.findall(r"[a-zA-Z]", text))
    return cn >= en


def _count_zh_chars(s: str) -> int:
    """數中文 CJK Unified Ideographs 字數（不含標點英數）。"""
    return sum(1 for c in s if "一" <= c <= "鿿")
