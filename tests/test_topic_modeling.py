"""
test_topic_modeling — 對 topic_modeling.py 的單元測試。

涵蓋 13 條：
1. 空輸入 → []
2. 單一文件 → []
3. 兩個明顯不同主題群（股票 vs 旅遊）→ LDA 抽到合理 top_words
4. n_topics > 文件數 → 自動 clamp
5. assign_topic 正確分到對的 topic
6. assign_topic 未知句 → -1
7. assign_topic 空字串 → -1
8. assign_topic None model → -1
9. extract_topics 回 dict 結構正確
10. doc_indices 加總 == 文件數
11. top_words 不為空且皆為 str
12. 長語料（30 條）穩定跑出指定數量 topics
13. model 屬性 transform 可用
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from topic_modeling import TopicModel, assign_topic, extract_topics  # noqa: E402


# ── 假對話：兩個明顯主題（投資 vs 旅遊）──────────────────────────────────
INVEST_MSGS = [
    "今天台積電股價漲了三十元",
    "台積電的營收很好",
    "我覺得台積電還會繼續漲",
    "美股費半大漲輝達領頭",
    "輝達的 GPU 賣得超好",
    "輝達跟台積電都是半導體龍頭",
]
TRAVEL_MSGS = [
    "下週要去日本京都旅遊",
    "京都的清水寺一定要去",
    "日本的拉麵真的好吃",
    "京都的櫻花季是最美的",
    "日本旅遊要安排五天比較剛好",
    "京都跟大阪都很值得玩",
]
MIXED = INVEST_MSGS + TRAVEL_MSGS


# ─────────────────────────────────────────────────────────────────────────────
# extract_topics
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_returns_empty_list():
    assert extract_topics([]) == []


def test_single_doc_returns_empty():
    assert extract_topics(["只有一條"]) == []


def test_two_topics_separable():
    topics = extract_topics(MIXED, n_topics=2)
    assert len(topics) == 2
    all_top_words: list[str] = []
    for t in topics:
        all_top_words.extend(t["top_words"])
    # 兩個主題裡至少有一個會抓到「台積電」或「輝達」（投資組）
    assert any(w in ("台積電", "輝達", "半導體") for w in all_top_words)
    # 另一個會抓到旅遊類詞
    assert any(w in ("京都", "日本", "旅遊") for w in all_top_words)


def test_n_topics_clamped_to_doc_count():
    msgs = ["蘋果好吃", "香蕉也不錯", "西瓜很甜"]
    topics = extract_topics(msgs, n_topics=10)
    assert 1 <= len(topics) <= 3


def test_topics_dict_structure():
    topics = extract_topics(MIXED, n_topics=2)
    for t in topics:
        assert "topic_id" in t
        assert "top_words" in t
        assert "doc_indices" in t
        assert "model" in t
        assert isinstance(t["topic_id"], int)
        assert isinstance(t["top_words"], list)
        assert isinstance(t["doc_indices"], list)


def test_doc_indices_partition():
    topics = extract_topics(MIXED, n_topics=2)
    all_idx: list[int] = []
    for t in topics:
        all_idx.extend(t["doc_indices"])
    assert sorted(all_idx) == list(range(len(MIXED)))


def test_top_words_not_empty_and_str():
    topics = extract_topics(MIXED, n_topics=2)
    for t in topics:
        assert len(t["top_words"]) > 0
        for w in t["top_words"]:
            assert isinstance(w, str)
            assert len(w) >= 2


def test_long_corpus_stable_topic_count():
    msgs = (INVEST_MSGS + TRAVEL_MSGS) * 3
    topics = extract_topics(msgs, n_topics=3)
    assert len(topics) == 3


# ─────────────────────────────────────────────────────────────────────────────
# assign_topic
# ─────────────────────────────────────────────────────────────────────────────
def test_assign_topic_routes_to_right_cluster():
    topics = extract_topics(MIXED, n_topics=2)
    model = topics[0]["model"]

    invest_q = "我看好台積電的股價"
    travel_q = "京都櫻花真的很美"

    invest_topic = assign_topic(invest_q, model)
    travel_topic = assign_topic(travel_q, model)

    # 兩句應該分到不同 topic
    assert invest_topic != -1
    assert travel_topic != -1
    assert invest_topic != travel_topic


def test_assign_topic_empty_string():
    topics = extract_topics(MIXED, n_topics=2)
    model = topics[0]["model"]
    assert assign_topic("", model) == -1
    assert assign_topic("   ", model) == -1


def test_assign_topic_none_model():
    assert assign_topic("任意句子", None) == -1  # type: ignore[arg-type]


def test_assign_topic_unknown_words_returns_minus_one():
    """完全不在 vocab 內的字串 → 回 -1。"""
    topics = extract_topics(MIXED, n_topics=2)
    model = topics[0]["model"]
    # 用很冷僻的字組（語料裡都沒）
    out = assign_topic("zzzzz qqqqqq vvvvvv", model)
    assert out == -1


def test_topic_model_transform_works():
    topics = extract_topics(MIXED, n_topics=2)
    model = topics[0]["model"]
    assert isinstance(model, TopicModel)
    arr = model.transform(["台積電大漲"])
    assert isinstance(arr, np.ndarray)
    assert arr.shape == (1, 2)
