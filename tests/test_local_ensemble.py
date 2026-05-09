"""Tests for local_ensemble — pure-local ensemble + self-consistency + ToT.

Mock local_llm.chat 回固定不同回答，驗證：
  (a) ensemble 用 LLM judge 挑 best
  (b) self_consistency 用 first-sentence cluster
  (c) ToT 多路徑展開 + judge
  (d) judge 失敗 → 退 majority vote
  (e) 觸發條件 helper 行為
  (f) 全失敗 / 空輸入的 graceful return
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

# bootstrap env（對齊 conftest.py，避免 main.py import 時噴錯）
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import local_ensemble  # noqa: E402


# ─── helpers：把 local_llm.chat mock 成可控的多輪回答 ────────────────────────


def _install_fake_local_llm(monkeypatch, responses, judge_response=None):
    """把 sys.modules['local_llm'].chat 換成 deterministic stub。

    responses: list[str]，主 chat 第 i 次呼叫回 responses[i]。
    judge_response: 偵測到 judge prompt（含「最佳 index =」）時固定回此值。
    用完 responses 之後就回最後一個值（不會 IndexError）。
    """
    calls = {"i": 0, "args": []}

    def fake_chat(query, **kwargs):
        calls["args"].append({"query": query, "kwargs": kwargs})
        # judge prompt 偵測
        if judge_response is not None and "最佳 index" in (query or ""):
            return judge_response
        i = calls["i"]
        calls["i"] += 1
        if i < len(responses):
            return responses[i]
        return responses[-1] if responses else None

    fake = types.ModuleType("local_llm")
    fake.chat = fake_chat
    monkeypatch.setitem(sys.modules, "local_llm", fake)
    return calls


# ─── 1. ensemble：judge 挑 best ─────────────────────────────────────────────


def test_ensemble_picks_judge_chosen_index(monkeypatch):
    """judge 回 '1' → 第二個候選被挑中。"""
    calls = _install_fake_local_llm(
        monkeypatch,
        responses=["回答 A", "回答 B 比較好", "回答 C"],
        judge_response="1",
    )
    out = local_ensemble.local_ensemble_chat("測試", n=3)
    assert out == "回答 B 比較好"
    # 4 calls：3 個候選 + 1 judge
    assert calls["i"] >= 3


def test_ensemble_judge_failure_falls_back_majority(monkeypatch):
    """judge 回非數字 → 退 majority cluster。"""
    # 三個候選裡兩個高度相似，cluster 大者勝
    _install_fake_local_llm(
        monkeypatch,
        responses=[
            "今天天氣好。",
            "今天天氣很好。",
            "完全無關的回答內容。",
        ],
        judge_response="完全沒有數字的東西",
    )
    out = local_ensemble.local_ensemble_chat("天氣？", n=3)
    # 前兩個高度相似 → 應該回 idx 0 或 1（cluster size=2）
    assert out in ("今天天氣好。", "今天天氣很好。")


def test_ensemble_temperature_padding(monkeypatch):
    """temperatures 短於 n → 自動補最後值。"""
    calls = _install_fake_local_llm(
        monkeypatch,
        responses=["a", "b", "c"],
        judge_response="0",
    )
    local_ensemble.local_ensemble_chat("Q", n=3, temperatures=[0.5])
    # 前 3 個 call（候選）的 temperature 應該都是 0.5（padding）
    sample_calls = [
        c for c in calls["args"] if "最佳 index" not in (c["query"] or "")
    ]
    temps = [c["kwargs"].get("temperature") for c in sample_calls[:3]]
    assert temps == [0.5, 0.5, 0.5]


def test_ensemble_all_chat_failures_returns_empty(monkeypatch):
    """所有 chat 都回 None → 回 ""。"""
    fake = types.ModuleType("local_llm")
    fake.chat = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "local_llm", fake)
    assert local_ensemble.local_ensemble_chat("Q", n=3) == ""


def test_ensemble_single_candidate_skips_judge(monkeypatch):
    """只有 1 個有效候選 → 直接回，不調 judge。"""
    fake = types.ModuleType("local_llm")
    seq = [None, "唯一有效", None]
    state = {"i": 0}

    def chat(*a, **kw):
        i = state["i"]
        state["i"] += 1
        return seq[i] if i < len(seq) else None

    fake.chat = chat
    monkeypatch.setitem(sys.modules, "local_llm", fake)
    out = local_ensemble.local_ensemble_chat("Q", n=3)
    assert out == "唯一有效"


# ─── 2. self_consistency：first-sentence cluster ────────────────────────────


def test_self_consistency_picks_majority_cluster(monkeypatch):
    """5 個候選裡 4 個 first-sentence 一致 → 回該 cluster 代表。"""
    _install_fake_local_llm(
        monkeypatch,
        responses=[
            "答案是 A。詳細解釋一。",
            "答案是 A。詳細解釋二。",
            "答案是 A。詳細解釋三。",
            "答案是 A。詳細解釋四。",
            "完全不同的離群答案內容。",
        ],
    )
    out = local_ensemble.self_consistency("Q", n=5)
    assert out.startswith("答案是 A")


def test_self_consistency_first_sentence_extraction(monkeypatch):
    """first-sentence 抽取：中文句號 / 沒標點 都要 work。"""
    # 一個有句號、其他沒句號 → 沒句號的會 cluster 在一起
    _install_fake_local_llm(
        monkeypatch,
        responses=[
            "短句一致 短句一致 短句一致",
            "短句一致 短句一致 不同尾巴",
            "完全 不一樣 的 內容",
        ],
    )
    out = local_ensemble.self_consistency("Q", n=3)
    # 前兩個前綴一致 → 應該選到其中一個
    assert "短句一致" in out


def test_self_consistency_empty_query_returns_empty():
    assert local_ensemble.self_consistency("", n=3) == ""


# ─── 3. Tree-of-Thoughts ────────────────────────────────────────────────────


def test_tot_multi_branch_with_judge(monkeypatch):
    """ToT branches=3 depth=2 → 6 calls + 1 judge → 回 judge 選擇的路徑。"""
    # depth=2：第 1 層 3 個分支 + 第 2 層 3 個展開 + 1 judge call
    _install_fake_local_llm(
        monkeypatch,
        responses=[
            "L1 正面：A",
            "L1 反面：B",
            "L1 中性：C",
            "L2 展開 A 結論",
            "L2 展開 B 結論",
            "L2 展開 C 結論",
        ],
        judge_response="2",
    )
    out = local_ensemble.tree_of_thoughts("Q", branches=3, depth=2)
    assert out == "L2 展開 C 結論"


def test_tot_depth_one_no_expansion(monkeypatch):
    """depth=1 → 不展開，第 1 層直接 judge。"""
    _install_fake_local_llm(
        monkeypatch,
        responses=["L1 a", "L1 b"],
        judge_response="0",
    )
    out = local_ensemble.tree_of_thoughts("Q", branches=2, depth=1)
    assert out == "L1 a"


def test_tot_branches_capped_to_prompt_pool(monkeypatch):
    """branches 超過 prompt pool → 自動 cap。"""
    _install_fake_local_llm(
        monkeypatch,
        responses=["x"] * 20,
        judge_response="0",
    )
    out = local_ensemble.tree_of_thoughts("Q", branches=99, depth=1)
    # 不應 raise，回某個非空字串
    assert out == "x"


def test_tot_all_failures_returns_empty(monkeypatch):
    fake = types.ModuleType("local_llm")
    fake.chat = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "local_llm", fake)
    assert local_ensemble.tree_of_thoughts("Q") == ""


# ─── 4. 觸發條件 helper ─────────────────────────────────────────────────────


def test_should_ensemble_keyword_and_length():
    long_query = "這個問題很重要" + "x" * 100
    assert local_ensemble.should_ensemble(long_query) is True


def test_should_ensemble_short_keyword_rejected():
    """有關鍵字但字數 <= 100 → False。"""
    assert local_ensemble.should_ensemble("這很重要") is False


def test_should_ensemble_no_keyword_rejected():
    long_no_kw = "x" * 200
    assert local_ensemble.should_ensemble(long_no_kw) is False


def test_should_self_consistency_keyword_hit():
    assert local_ensemble.should_self_consistency("這個答案要精準") is True
    assert local_ensemble.should_self_consistency("不能錯，請仔細想") is True


def test_should_self_consistency_no_keyword():
    assert local_ensemble.should_self_consistency("隨便聊聊") is False


def test_should_tot_keyword_and_length():
    q = "我該不該買這檔股票，這個決定很關鍵需要慎重考慮所有因素 " * 2
    assert len(q) > 50
    assert local_ensemble.should_tot(q) is True


def test_should_tot_short_rejected():
    assert local_ensemble.should_tot("該不該買") is False


def test_should_helpers_empty_query():
    assert local_ensemble.should_ensemble("") is False
    assert local_ensemble.should_self_consistency("") is False
    assert local_ensemble.should_tot("") is False


# ─── 5. 內部小工具 sanity ───────────────────────────────────────────────────


def test_majority_cluster_picks_largest():
    texts = ["蘋果好吃", "蘋果好吃喔", "蘋果好吃啦", "完全不一樣"]
    idx = local_ensemble._majority_cluster(texts)
    # 前 3 個高度相似 → idx 應該是 0 / 1 / 2 之一
    assert idx in (0, 1, 2)


def test_first_sentence_chinese_punct():
    assert local_ensemble._first_sentence("今天天氣好。後面還有更多。") == "今天天氣好。"


def test_first_sentence_no_punct_truncates():
    long = "x" * 100
    out = local_ensemble._first_sentence(long)
    assert len(out) <= 40


def test_safe_chat_swallows_exceptions(monkeypatch):
    """_safe_chat 在 chat 拋例外時回 None。"""
    fake = types.ModuleType("local_llm")

    def boom(*a, **kw):
        raise RuntimeError("boom")

    fake.chat = boom
    monkeypatch.setitem(sys.modules, "local_llm", fake)
    assert local_ensemble._safe_chat("Q") is None
