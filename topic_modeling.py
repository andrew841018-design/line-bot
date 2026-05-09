"""
topic_modeling — 純本機 LDA topic modeling（sklearn）。

- 用 jieba 斷詞 + sklearn CountVectorizer + LatentDirichletAllocation
- 100% 本機，不打雲端 API

公開 API:
    extract_topics(messages, n_topics=5)   -> list[dict]
    assign_topic(message, model)           -> int
"""
from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.decomposition import LatentDirichletAllocation
from sklearn.feature_extraction.text import CountVectorizer

try:
    from chinese_nlp import tokenize  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    import jieba  # type: ignore[import-not-found]

    def tokenize(text: str) -> list[str]:
        if not text or not text.strip():
            return []
        return [t.strip() for t in jieba.lcut(text) if t.strip() and len(t.strip()) >= 2]


def _zh_tokenizer(text: str) -> list[str]:
    """sklearn CountVectorizer 用的 callable tokenizer。
    強制過濾：單字、純空白、純標點。
    """
    if not text:
        return []
    out: list[str] = []
    for tok in tokenize(text):
        tok = tok.strip()
        if len(tok) < 2:
            continue
        if not any(c.isalnum() or "一" <= c <= "鿿" for c in tok):
            continue
        out.append(tok)
    return out


class TopicModel:
    """打包好的 (vectorizer, lda)，方便 assign_topic 直接吃。"""

    __slots__ = ("vectorizer", "lda", "feature_names", "n_topics")

    def __init__(
        self,
        vectorizer: CountVectorizer,
        lda: LatentDirichletAllocation,
    ) -> None:
        self.vectorizer = vectorizer
        self.lda = lda
        self.feature_names = vectorizer.get_feature_names_out()
        self.n_topics = lda.n_components

    def transform(self, messages: list[str]) -> np.ndarray:
        X = self.vectorizer.transform(messages)
        return self.lda.transform(X)


def extract_topics(messages: list[str], n_topics: int = 5, top_n_words: int = 10) -> list[dict[str, Any]]:
    """
    對訊息語料跑 LDA，回傳每個 topic 的 top words + 命中的文件 index。

    Args:
        messages: 文件清單。每條都會被 jieba 斷詞。
        n_topics: LDA 拆幾個 topic。
        top_n_words: 每 topic 印幾個 top word。

    Returns:
        [
          {
            "topic_id": int,
            "top_words": [str, ...],
            "doc_indices": [int, ...],   # argmax 命中此 topic 的文件
            "model": TopicModel,         # 同一個 model object 共享在 list 每筆裡，方便 assign_topic
          },
          ...
        ]

    Notes:
        - 如果文件不足 / 全部太短 → 回 []。
        - n_topics 會自動 clamp 到 1 ~ len(messages)。
    """
    msgs = [m for m in (messages or []) if isinstance(m, str) and m.strip()]
    if len(msgs) < 2:
        return []

    n_topics = max(1, min(int(n_topics), len(msgs)))

    # min_df=1：小語料 / 測試用；max_df=0.95：濾過於通用的詞
    vectorizer = CountVectorizer(
        tokenizer=_zh_tokenizer,
        token_pattern=None,  # 用 callable tokenizer 就要關 token_pattern
        min_df=1,
        max_df=0.95,
        lowercase=False,
    )

    try:
        X = vectorizer.fit_transform(msgs)
    except ValueError:
        # 全部都被過濾光（e.g. 全英文超短句）→ 放寬一次
        vectorizer = CountVectorizer(
            tokenizer=_zh_tokenizer, token_pattern=None, min_df=1, max_df=1.0, lowercase=False
        )
        try:
            X = vectorizer.fit_transform(msgs)
        except ValueError:
            return []

    if X.shape[1] == 0:
        return []

    lda = LatentDirichletAllocation(
        n_components=n_topics,
        max_iter=20,
        learning_method="batch",
        random_state=42,
    )
    lda.fit(X)

    feature_names = vectorizer.get_feature_names_out()
    doc_topic = lda.transform(X)
    doc_assign = doc_topic.argmax(axis=1)

    model = TopicModel(vectorizer, lda)

    topics: list[dict[str, Any]] = []
    for k in range(n_topics):
        top_idx = lda.components_[k].argsort()[::-1][:top_n_words]
        top_words = [str(feature_names[i]) for i in top_idx]
        doc_indices = [int(i) for i, t in enumerate(doc_assign) if t == k]
        topics.append(
            {
                "topic_id": int(k),
                "top_words": top_words,
                "doc_indices": doc_indices,
                "model": model,
            }
        )

    return topics


def assign_topic(message: str, model: TopicModel) -> int:
    """
    用既有 LDA model 預測單句的 topic_id。

    Args:
        message: 單一字串。
        model: extract_topics 回傳裡 topic dict 的 "model" 欄位 (TopicModel)。

    Returns:
        int — argmax topic id；空字串或 model 不正常時回 -1。
    """
    if not isinstance(message, str) or not message.strip():
        return -1
    if model is None or not hasattr(model, "lda") or not hasattr(model, "vectorizer"):
        return -1
    try:
        X = model.vectorizer.transform([message])
        if X.sum() == 0:
            # 整句沒命中任何已知詞 → topic uniform，argmax 會偏移；回 -1 比較誠實
            return -1
        dist = model.lda.transform(X)[0]
        return int(np.argmax(dist))
    except Exception:
        return -1


__all__ = ["extract_topics", "assign_topic", "TopicModel"]
