"""gemini_core — shared constants for gemini_client + rag_graph.

Phase 2B.2.1: extracted to break circular import between gemini_client and
rag_graph (which both need NEWS_CASE regex / rule text). API stable;
gemini_client.py re-exports for backwards compat with 15 existing importers.

DO NOT add stateful objects here (e.g. genai.Client). Module-level state
belongs in gemini_client until Phase 2B.2.3.
"""
from __future__ import annotations

import re

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
]


_RULE_NEWS_CASE = """【新聞 / 案例 / 研究 / 專業議題分享 = 必給觀點 + 多源】（2026-05-04 加，補 23f 在非政治話題的覆蓋）
觸發：群組分享新聞、案例、研究、報導、文章，或聊到保險、投資、醫療、教育、健康、法律、消費、房地產、AI、詐騙等專業議題；影片標題含「分析 / 評論 / 心得 / 解析」。

bot 必須走「**有想法的查核員**」模式，禁止走「**附和的鄰居**」模式。

❌ 禁止：
- 「咪寶看到 X」「咪寶覺得這個 X 很 Y」「我看到您分享了 Z」（echo 重述）
- 「保險選擇要看個人需求」「醫療要看個人狀況」「需要諮詢專業人士」（純附和廢話）
- 「真的很心疼」「真的很重要」「值得我們深思」「需要重視」（空附和）
- 只附 1 條來源（user 貼的那條本身不算）

✅ 必含 4 樣（不照順序，但都要有）：
(i) 第一句直接表態你的 take（具體判斷，不是描述、不是評論）
    例：「我這邊覺得這份保單條款設計有問題」「這個方法的問題在於樣本太小」
(ii) 具體事實 ≥ 3 個（法條 / 統計 / 國外做法 / 類似案例 / 量化數字）
(iii) 至少 3 條來源 URL，不同網域，**不算 user 貼的那條**
(iv) 結論一句：你權衡完支持 / 反對什麼具體做法

範例（user 貼某保險新聞，bot 應該）：
「我這邊覺得問題不在『保戶沒看清楚』而在『條款設計刻意模糊』。
- 金管會 2024 統計：壽險爭議件數中 68% 涉及條款解釋
- 對比日本：金融廳要求保單條款須通過『一般民眾理解度測試』
- 台灣金融消費評議中心：去年條款爭議補償率 < 30%
結論：應推動條款『可讀性審查』機制，光要消費者自己讀不夠。
來源：
• 金管會 2024 保險業務統計：https://...
• 金融消費評議中心年報：https://...
• 日本金融廳保單揭露指引：https://...」
"""


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


def _append_sources(text: str, urls: list[tuple[str, str]]) -> str:
    """若回覆裡還沒有來源網址，就把 grounding URLs 補在結尾。最多附 3 條。"""
    if not urls:
        return text
    if _URL_IN_TEXT_RE.search(text):
        return text
    lines = ["來源："]
    for uri, title in urls[:3]:
        lines.append(f"• {title}\n  {uri}" if title else f"• {uri}")
    return text + "\n\n" + "\n".join(lines)


def _is_chinese_majority(text: str) -> bool:
    """中文字元數 >= 英文字母數才算中文為主。"""
    cn = len(re.findall(r"[一-鿿]", text))
    en = len(re.findall(r"[a-zA-Z]", text))
    return cn >= en


def _count_zh_chars(s: str) -> int:
    """數中文 CJK Unified Ideographs 字數（不含標點英數）。"""
    return sum(1 for c in s if "一" <= c <= "鿿")
