"""finetune_query_expansion.py — v4 pipeline Step 2

從圖片描述（+ optional OCR）生 6-8 個多樣搜尋 query，給後續事實查證 retrieve 用。
100% 本機：local_llm（Qwen2.5-14B + LoRA）為主、純規則式 fallback 兜底。

設計理由
========
v4 pipeline 的 fact-check 階段需要對單一描述做多 angle 搜尋（事件本身 / 機構 /
歷史背景 / 反對意見 / 一手 source / fact-check）。單一 query 容易 echo
描述本身，蓋不到 contrarian / 一手英文 source，所以這層用 LLM 做「query 多樣化」。

對外 API
========
    expand_queries(desc, ocr_text="", n=6) -> list[str]
        主路徑：local_llm.chat 給 prompt 拿 JSON list；失敗退 _fallback_queries。

    _fallback_queries(desc, n) -> list[str]
        純規則式：jieba.posseg 抓人名 / 地名 / 機構名，配模板生成。
        無 LLM 也跑得動，CI / mock 環境友善。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger("finetune_query_expansion")


# ─────────────────────────────────────────────────────────────────────────────
# Prompt
# ─────────────────────────────────────────────────────────────────────────────
_PROMPT_TEMPLATE = """下面是 LINE 群組一張圖的描述。請為「事實查證」生成 {n} 個多樣搜尋 query：
- 從不同 angle 切入（事件本身 / 機構 / 歷史背景 / 反對意見 / 一手 source / fact-check）
- 中英文混合（一手 source 用英文，二手中文）
- 包含具體名詞（人名 / 機構 / 日期 / 數字）
- 每個 query 5-12 字

描述：{desc}
OCR：{ocr_text}

輸出 JSON list of {n} strings，不要其他內容。"""


# ─────────────────────────────────────────────────────────────────────────────
# JSON 解析（容忍 markdown fence / 多餘 prose）
# ─────────────────────────────────────────────────────────────────────────────
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_LIST_RE = re.compile(r"\[\s*(?:.|\n)*?\]", re.DOTALL)


def _strip_fence(text: str) -> str:
    """剝除 ```json ... ``` 或 ``` ... ```。沒 fence 直接回原文。"""
    if not text:
        return ""
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _parse_query_list(raw: str) -> list[str]:
    """從 LLM 回應抓 JSON list[str]，盡量寬鬆。

    流程：
      1. 剝 markdown fence
      2. 直接 json.loads
      3. 失敗 → regex 找 [...] 區段再試
      4. 全失敗 → 拋 ValueError 給上層 fallback
    """
    if not raw:
        raise ValueError("empty raw")

    body = _strip_fence(raw)

    # try direct
    try:
        obj = json.loads(body)
        return _coerce_str_list(obj)
    except (json.JSONDecodeError, ValueError):
        pass

    # try regex extract first [...] block
    m = _LIST_RE.search(body)
    if m:
        try:
            obj = json.loads(m.group(0))
            return _coerce_str_list(obj)
        except (json.JSONDecodeError, ValueError):
            pass

    raise ValueError(f"cannot parse query list from: {raw[:120]!r}")


def _coerce_str_list(obj: object) -> list[str]:
    """把 parsed JSON 強制成 list[str]，過濾空字串。"""
    if not isinstance(obj, list):
        raise ValueError(f"expected list, got {type(obj).__name__}")
    out: list[str] = []
    for item in obj:
        if isinstance(item, str):
            s = item.strip()
            if s:
                out.append(s)
    if not out:
        raise ValueError("empty after coerce")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 規則式 fallback（不打 LLM）
# ─────────────────────────────────────────────────────────────────────────────
_QUERY_TEMPLATES: tuple[str, ...] = (
    "{name} 是什麼",
    "{name} 真假",
    "{name} 2026",
    "{name} 反對",
    "{name} fact check",
    "{name} 爭議",
    "{name} 官方聲明",
    "{name} latest news",
)

# 想抓的詞性（人名 / 地名 / 機構名 / 普通名詞 / 英文 / 數字機構代碼）
_INTEREST_FLAGS: frozenset[str] = frozenset({"nr", "ns", "nt", "n", "eng", "nz"})


def _extract_keynames(text: str) -> list[str]:
    """用 jieba.posseg 抓專有名詞，依出現次數排序（穩定）。"""
    if not text or not text.strip():
        return []

    try:
        import jieba.posseg as pseg  # lazy import：避免測試環境 import time 慢
    except Exception as e:
        logger.warning("jieba unavailable for fallback: %s", e)
        return []

    counts: dict[str, int] = {}
    order: list[str] = []
    for pair in pseg.cut(text):
        word = (pair.word or "").strip()
        flag = pair.flag or ""
        if not word or len(word) < 2:
            continue
        if flag not in _INTEREST_FLAGS:
            continue
        if word not in counts:
            order.append(word)
        counts[word] = counts.get(word, 0) + 1

    # 依出現次數降序，同次數保留 jieba 給的原順序（先凍 index，避免 sort key 內查 mutating list）
    original_idx = {w: i for i, w in enumerate(order)}
    order.sort(key=lambda w: (-counts[w], original_idx[w]))
    return order


def _fallback_queries(desc: str, n: int = 6) -> list[str]:
    """純規則式 query 生成；不依賴 LLM。

    流程：
      1. jieba.posseg 抓最高頻 nr / ns / nt 名詞
      2. 配 _QUERY_TEMPLATES 鋪滿到 n 個
      3. 名詞抽不到 → 用 desc 前 50 字當 seed 再變體
    """
    n = max(1, n)
    names = _extract_keynames(desc)

    out: list[str] = []
    seen: set[str] = set()

    if names:
        # 主名詞優先做完一輪模板
        for name in names:
            for tmpl in _QUERY_TEMPLATES:
                if len(out) >= n:
                    break
                q = tmpl.format(name=name)
                if q not in seen:
                    out.append(q)
                    seen.add(q)
            if len(out) >= n:
                break

    if len(out) < n:
        # seed = desc 前 50 字（去頭尾空白）
        seed = (desc or "").strip().replace("\n", " ")
        seed = seed[:50] if seed else "未知主題"
        seed_short = seed[:20] if len(seed) > 20 else seed
        seed_variants = [
            seed_short,
            f"{seed_short} 真假",
            f"{seed_short} 來源",
            f"{seed_short} 爭議",
            f"{seed_short} fact check",
            f"{seed_short} 反對",
            f"{seed_short} 官方",
            f"{seed_short} latest",
        ]
        for v in seed_variants:
            if len(out) >= n:
                break
            if v and v not in seen:
                out.append(v)
                seen.add(v)

    # 仍不足（極端情況）→ 用 index 補
    i = 1
    while len(out) < n:
        q = f"未知主題 {i}"
        if q not in seen:
            out.append(q)
            seen.add(q)
        i += 1

    return out[:n]


# ─────────────────────────────────────────────────────────────────────────────
# 對外 API
# ─────────────────────────────────────────────────────────────────────────────
def expand_queries(desc: str, ocr_text: str = "", n: int = 6) -> list[str]:
    """從圖片描述 + OCR 文字生 n 個多樣搜尋 query。

    Args:
        desc: vision-LLM 給的描述，必填。
        ocr_text: optional OCR 結果，會塞進 prompt 當補充訊號。
        n: 想要的 query 數，預設 6。

    Returns:
        list[str]，長度 == n。LLM 失敗自動退 _fallback_queries。
        desc 為空：直接回 _fallback_queries（會塞「未知主題」變體）。
    """
    if not desc or not desc.strip():
        return _fallback_queries(desc or "", n=n)

    n = max(1, int(n))

    # lazy import：保留測試 monkeypatch sys.modules['local_llm'] 的能力
    chat: Optional[callable] = None
    try:
        import local_llm  # type: ignore
        chat = getattr(local_llm, "chat", None)
    except Exception as e:
        logger.warning("local_llm import failed: %s", e)

    if chat is None:
        return _fallback_queries(desc, n=n)

    prompt = _PROMPT_TEMPLATE.format(
        n=n,
        desc=desc.strip(),
        ocr_text=(ocr_text or "").strip() or "(無)",
    )

    try:
        raw = chat(
            prompt,
            system_prompt=(
                "你是搜尋 query 生成器。只輸出 JSON list of strings，"
                "不要 markdown code fence，不要解釋。"
            ),
            max_tokens=400,
        )
    except Exception as e:
        logger.warning("local_llm.chat raised: %s", e)
        return _fallback_queries(desc, n=n)

    if not raw:
        return _fallback_queries(desc, n=n)

    try:
        queries = _parse_query_list(raw)
    except ValueError as e:
        logger.info("parse failed, falling back: %s", e)
        return _fallback_queries(desc, n=n)

    # 去重 + trim
    deduped: list[str] = []
    seen: set[str] = set()
    for q in queries:
        q = q.strip()
        if q and q not in seen:
            deduped.append(q)
            seen.add(q)

    # 不足 n → 用 fallback 補滿（也避免重複）
    if len(deduped) < n:
        for q in _fallback_queries(desc, n=n):
            if len(deduped) >= n:
                break
            if q not in seen:
                deduped.append(q)
                seen.add(q)

    return deduped[:n]


__all__ = ["expand_queries", "_fallback_queries"]
