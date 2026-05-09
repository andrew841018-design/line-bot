"""Local-only ensemble + self-consistency + Tree-of-Thoughts on Qwen2.5 (mlx).

純本機推理：用 `local_llm.chat()` 多次採樣 + LLM-as-judge / cluster vote。
完全不打雲端 API（Groq / Cerebras / DeepSeek / Gemini 都不碰）。

三個主要函式：
- `local_ensemble_chat(query, n=3, temperatures=[0.3, 0.6, 0.9])`：
    多 temperature 採樣 → LLM-as-judge 挑最佳；judge 失敗退 majority vote。
- `self_consistency(query, n=5, temperature=0.7)`：
    同 temperature 跑 n 次 → first-sentence cluster → 取最大 cluster 代表。
- `tree_of_thoughts(query, branches=3, depth=2)`：
    第 1 層分 N 個推理 prompt → 第 2 層各自展開 → judge 挑最佳。

觸發條件 helper：
- `should_ensemble(query)` / `should_self_consistency(query)` / `should_tot(query)`

效能（Apple M-series 32GB）：
- n=3 用 3B：~6s
- n=3 用 14B：~20s
- n=5 用 14B：~30s
LINE webhook 30s timeout 內可承受 default n=3 + 14B。
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Callable, Optional

logger = logging.getLogger("local_ensemble")

# ─── 觸發關鍵字（保留為 module 常數方便測試 / 調整）──────────────────────────

_ENSEMBLE_KEYWORDS = ("重要", "嚴謹", "精準", "慎重")
_SELF_CONSISTENCY_KEYWORDS = ("精準", "準確", "不能錯")
_TOT_KEYWORDS = ("決定", "選擇", "該不該", "怎麼決定")

# 字串相似度 cluster 門檻（first-sentence 等價時）
_CLUSTER_SIM_THRESHOLD = 0.55


# ─── 內部小工具 ─────────────────────────────────────────────────────────────


def _local_chat() -> Callable:
    """延後 import：方便 monkeypatch sys.modules['local_llm']。"""
    from local_llm import chat  # noqa: WPS433
    return chat


def _safe_chat(query: str, **kwargs) -> Optional[str]:
    """呼叫 local_llm.chat()，吃掉任何例外回 None。

    這層 wrapper 讓 ensemble / self_consistency 能在單次採樣失敗時繼續，
    不要因為一發 generate failure 整個 pipeline 倒掉。
    """
    try:
        chat = _local_chat()
    except Exception as e:
        logger.warning("local_llm import failed: %s", e)
        return None
    try:
        return chat(query, **kwargs)
    except TypeError:
        # local_llm.chat 介面只支援部分 kwargs，退回最寬鬆呼叫
        try:
            return chat(query)
        except Exception as e:
            logger.warning("local_llm.chat (loose) failed: %s", e)
            return None
    except Exception as e:
        logger.warning("local_llm.chat failed: %s", e)
        return None


def _first_sentence(text: str) -> str:
    """抽第一句。中文標點優先，再 fallback 英文標點。"""
    if not text:
        return ""
    text = text.strip()
    # 中文 / 英文句末標點
    m = re.search(r"[。！？!?\.]", text)
    if m:
        return text[: m.end()].strip()
    # 沒標點 → 取前 40 字
    return text[:40].strip()


def _similarity(a: str, b: str) -> float:
    """SequenceMatcher 相似度（純 Python，不需 embedding model）。"""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _majority_cluster(texts: list[str], threshold: float = _CLUSTER_SIM_THRESHOLD) -> int:
    """把 texts 用相似度 cluster，回最大 cluster 的代表 index。

    O(n²) 比對，n 很小（3~5）所以 OK。
    """
    if not texts:
        return 0
    n = len(texts)
    # 對每個 i，算它和其他文字相似的數量（含自己 = 1）
    scores = [0] * n
    for i in range(n):
        for j in range(n):
            if _similarity(texts[i], texts[j]) >= threshold:
                scores[i] += 1
    # 取 cluster size 最大者；同分取 index 較小
    best = max(range(n), key=lambda i: (scores[i], -i))
    return best


def _judge_best(query: str, candidates: list[str]) -> Optional[int]:
    """用本機 LLM 當 judge，回最佳候選 index。失敗回 None。"""
    if len(candidates) == 1:
        return 0
    numbered = "\n".join(
        f"[{i}] {c}" for i, c in enumerate(candidates)
    )
    judge_prompt = (
        "以下 N 個候選回答 user 問題，按咪寶人設規則 0 first-sentence-take + "
        "中文流暢 + 結構完整 挑最佳。"
        "只輸出單一數字 index，不要任何說明。\n\n"
        f"使用者問題：{query}\n\n候選：\n{numbered}\n\n"
        "最佳 index ="
    )
    raw = _safe_chat(judge_prompt, max_tokens=10)
    if not raw:
        return None
    m = re.search(r"\d+", raw)
    if not m:
        return None
    idx = int(m.group(0))
    if 0 <= idx < len(candidates):
        return idx
    return None


# ─── 1. ensemble：多 temperature + LLM judge ────────────────────────────────


def local_ensemble_chat(
    query: str,
    n: int = 3,
    temperatures: Optional[list[float]] = None,
) -> str:
    """多次採樣（不同 temperature）→ judge 挑最佳。

    Args:
        query: 使用者輸入。
        n: 採樣次數。
        temperatures: 與 n 對應的 temperature；長度不對會 truncate / pad。

    Returns:
        最佳候選字串。全失敗回 ""。
    """
    if not query:
        return ""
    if temperatures is None:
        temperatures = [0.3, 0.6, 0.9]
    # 對齊長度：不足補最後一個值，過多 truncate
    if len(temperatures) < n:
        last = temperatures[-1] if temperatures else 0.7
        temperatures = list(temperatures) + [last] * (n - len(temperatures))
    else:
        temperatures = list(temperatures[:n])

    candidates: list[str] = []
    for t in temperatures:
        out = _safe_chat(query, temperature=t)
        if out and out.strip():
            candidates.append(out.strip())

    if not candidates:
        return ""
    if len(candidates) == 1:
        return candidates[0]

    idx = _judge_best(query, candidates)
    if idx is None:
        # judge 失敗 → majority vote（cluster 最大者）
        idx = _majority_cluster(candidates)
        logger.info("judge failed, fall back to majority cluster (idx=%d)", idx)
    return candidates[idx]


# ─── 2. self-consistency：first-sentence cluster ────────────────────────────


def self_consistency(
    query: str,
    n: int = 5,
    temperature: float = 0.7,
) -> str:
    """同 temperature 跑 n 次 → first-sentence cluster → 取最大者代表。

    思路：first-sentence-take 是咪寶人設核心；若 n 次採樣都收斂到同一個
    判斷句，這個答案最可信。
    """
    if not query:
        return ""
    candidates: list[str] = []
    for _ in range(n):
        out = _safe_chat(query, temperature=temperature)
        if out and out.strip():
            candidates.append(out.strip())

    if not candidates:
        return ""
    if len(candidates) == 1:
        return candidates[0]

    first_sentences = [_first_sentence(c) for c in candidates]
    idx = _majority_cluster(first_sentences)
    return candidates[idx]


# ─── 3. Tree-of-Thoughts：分支推理 + judge ─────────────────────────────────

_BRANCH_PROMPTS = [
    "請從正面 / 樂觀角度分析：",
    "請從反面 / 風險角度分析：",
    "請從第三方 / 中性角度分析：",
    "請從歷史 / 過往經驗角度分析：",
]


def tree_of_thoughts(
    query: str,
    branches: int = 3,
    depth: int = 2,
) -> str:
    """ToT：第 1 層 N 個推理路徑 → 第 2 層各路徑展開 → judge 挑最佳。

    Args:
        query: 使用者輸入。
        branches: 第 1 層分支數（從 _BRANCH_PROMPTS 取前 N 個 prompt）。
        depth: 展開層數，每層遞迴一次。depth=1 等同 ensemble（沒展開）。

    Returns:
        最佳路徑的最終答案字串。失敗回 ""。
    """
    if not query:
        return ""
    branches = max(1, min(branches, len(_BRANCH_PROMPTS)))
    # 第 1 層：分支推理
    layer1: list[str] = []
    for i in range(branches):
        prompt = f"{_BRANCH_PROMPTS[i]}{query}"
        out = _safe_chat(prompt, temperature=0.7)
        if out and out.strip():
            layer1.append(out.strip())
    if not layer1:
        return ""

    # 第 2 ~ depth 層：每路徑各自再展開
    current = layer1
    for _layer in range(max(0, depth - 1)):
        next_layer: list[str] = []
        for thought in current:
            expand_prompt = (
                f"基於以下分析展開更深入的判斷：\n{thought}\n\n"
                f"原始問題：{query}\n請直接給結論。"
            )
            out = _safe_chat(expand_prompt, temperature=0.5)
            if out and out.strip():
                next_layer.append(out.strip())
        if next_layer:
            current = next_layer
        else:
            break  # 這層全失敗 → 用上一層的結果

    if not current:
        return ""
    if len(current) == 1:
        return current[0]
    idx = _judge_best(query, current)
    if idx is None:
        idx = _majority_cluster(current)
    return current[idx]


# ─── 觸發條件 helper（讓 caller 判斷要不要跑 ensemble）─────────────────────


def should_ensemble(query: str) -> bool:
    """含「重要 / 嚴謹 / 精準 / 慎重」+ 字數 > 100 → True。"""
    if not query:
        return False
    if len(query) <= 100:
        return False
    return any(kw in query for kw in _ENSEMBLE_KEYWORDS)


def should_self_consistency(query: str) -> bool:
    """含「精準 / 準確 / 不能錯」 → True。"""
    if not query:
        return False
    return any(kw in query for kw in _SELF_CONSISTENCY_KEYWORDS)


def should_tot(query: str) -> bool:
    """含「決定 / 選擇 / 該不該 / 怎麼決定」+ 字數 > 50 → True。"""
    if not query:
        return False
    if len(query) <= 50:
        return False
    return any(kw in query for kw in _TOT_KEYWORDS)


__all__ = [
    "local_ensemble_chat",
    "self_consistency",
    "tree_of_thoughts",
    "should_ensemble",
    "should_self_consistency",
    "should_tot",
]
