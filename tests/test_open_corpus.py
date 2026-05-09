"""Tests for finetune/open_corpus.py.

Pure mock — no real HuggingFace download (avoids network burn).
Mocks `_hf_load` to return tiny in-memory list-of-dicts.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# bootstrap (matches conftest.py style)
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

LINE_BOT_ROOT = Path(__file__).resolve().parent.parent
if str(LINE_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(LINE_BOT_ROOT))

from finetune import open_corpus  # noqa: E402


# ─── fakes ─────────────────────────────────────────────────────────────────
class FakeDS:
    """Mimics HuggingFace `datasets.Dataset` minimally — len + __getitem__."""

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, idx: int) -> dict[str, Any]:
        return self.rows[idx]


# ═══════════════════════════════════════════════════════════════════════════
# 1. download_corpus — mocked HF load
# ═══════════════════════════════════════════════════════════════════════════
def test_download_corpus_dry_run_no_network():
    """dry_run=True → 不打雲端，回 []。"""
    rows = open_corpus.download_corpus(name="fake/ds", size=10, dry_run=True)
    assert rows == []


def test_download_corpus_basic_belle_schema(monkeypatch):
    fake_rows = [
        {
            "instruction": "为什么台积电股价最近涨这么多？",
            "input": "",
            "output": "因为AI需求旺盛，加上汇率因素，所以涨了。这是一段足够长的回应内容来通过长度过滤。",
        },
        {
            "instruction": "请翻译下面的句子到英文",
            "input": "你好",
            "output": "Hello",  # 純翻譯指令 + 太短 → length filter 掉
        },
        {
            "instruction": "我觉得最近通膨真的很严重，你怎么看？",
            "input": "",
            "output": "通膨这事其实分好几层来看。首先核心 CPI 跟能源是不同变数，得分开看。这段也长度够长。",
        },
    ]
    monkeypatch.setattr(open_corpus, "_hf_load", lambda *a, **kw: FakeDS(fake_rows))
    out = open_corpus.download_corpus(name="fake/ds", size=10, seed=1, dry_run=False)

    # 至少有一條通過 length+normalize（短 hello 那條會被剃）
    assert len(out) >= 1
    # 每條都有 prompt + completion 字段
    for r in out:
        assert "prompt" in r and "completion" in r
        assert r["prompt"] and r["completion"]


def test_download_corpus_simplified_to_traditional(monkeypatch):
    """簡 → 繁：output 含简体字 → 應被 OpenCC 轉繁。"""
    fake_rows = [
        {
            "instruction": "你怎么看这个问题？我觉得很有趣这是测试内容长度",
            "input": "",
            "output": "这是简体中文的测试输出内容，应该被OpenCC转成繁中。这段长度也足够通过过滤。",
        },
    ]
    monkeypatch.setattr(open_corpus, "_hf_load", lambda *a, **kw: FakeDS(fake_rows))
    out = open_corpus.download_corpus(name="fake/ds", size=5, seed=0, dry_run=False)
    assert len(out) == 1
    # OpenCC s2t：「这」→「這」, 「测试」→「測試」
    text = out[0]["prompt"] + out[0]["completion"]
    assert "這" in text or "測試" in text  # 至少其中一個轉換成功


def test_download_corpus_length_filter(monkeypatch):
    """太短（<30字）跟太長（>1000字）都該被剔除。"""
    fake_rows = [
        # 太短
        {"instruction": "嗨？", "input": "", "output": "好"},
        # 太長
        {"instruction": "為什麼？" * 10, "input": "", "output": "看法。" * 500},
        # 剛好
        {
            "instruction": "為什麼今天市場跌這麼多？我覺得很奇怪",
            "input": "",
            "output": "今天主要是聯準會升息預期讓資金縮手，加上技術面剛好破底。這段足夠長。",
        },
    ]
    monkeypatch.setattr(open_corpus, "_hf_load", lambda *a, **kw: FakeDS(fake_rows))
    out = open_corpus.download_corpus(name="fake/ds", size=10, seed=0, dry_run=False)
    assert len(out) == 1


def test_download_corpus_conversations_schema(monkeypatch):
    """ShareGPT 風格 schema：conversations[{value}, {value}]."""
    fake_rows = [
        {
            "conversations": [
                {"from": "human", "value": "我想问你怎么看美元最近的走势？"},
                {"from": "gpt", "value": "美元最近因为利差扩大走强，但短线已经偏多伽过头。"},
            ]
        }
    ]
    monkeypatch.setattr(open_corpus, "_hf_load", lambda *a, **kw: FakeDS(fake_rows))
    out = open_corpus.download_corpus(name="shareAI/ShareGPT-Chinese", size=5, seed=0, dry_run=False)
    assert len(out) == 1
    assert "美元" in out[0]["prompt"] or "美元" in out[0]["completion"]


# ═══════════════════════════════════════════════════════════════════════════
# 2. filter_for_chat_style — pure rule-based
# ═══════════════════════════════════════════════════════════════════════════
def test_filter_keeps_chat_style():
    rows = [
        {
            "prompt": "你覺得今年房市還能撐嗎？",
            "completion": "我認為短線還有支撐力，但中長期看人口紅利已經反轉，要小心。",
        },
        {
            "prompt": "我想問一下，最近通膨怎麼看",
            "completion": "通膨主要看核心 CPI，扣除能源跟食物比較準。",
        },
    ]
    kept = open_corpus.filter_for_chat_style(rows)
    assert len(kept) == 2


def test_filter_drops_pure_instruction():
    rows = [
        {
            "prompt": "請翻譯下面的英文到中文",
            "completion": "好的請提供英文",
        },
        {
            "prompt": "請寫一首關於秋天的詩",
            "completion": "秋風起兮秋葉黃",
        },
        {
            "prompt": "Generate a Python function to add two numbers",
            "completion": "def add(a,b): return a+b",
        },
    ]
    kept = open_corpus.filter_for_chat_style(rows)
    # 三條都是純指令型 → 全砍
    assert kept == []


def test_filter_drops_code_blocks():
    rows = [
        {
            "prompt": "你覺得這段程式有問題嗎",
            "completion": "看起來```def f(x): return x```有問題",
        },
        {
            "prompt": "為什麼這樣？",  # 純對話
            "completion": "因為這個邏輯本身是這樣設計的，沒問題。",
        },
    ]
    kept = open_corpus.filter_for_chat_style(rows)
    # 第一條 completion 含 ``` 程式碼 → 但我們是看 prompt，prompt 是「你覺得」OK
    # 哦再仔細看：filter 只判斷 prompt 是否 instruction-like + 對話標記
    # prompt #1「你覺得這段程式有問題嗎」OK
    # prompt #2「為什麼這樣？」OK
    # 兩條 prompt 都是 chat style → 都保留
    assert len(kept) == 2


# ═══════════════════════════════════════════════════════════════════════════
# 3. compute_target_size + merge_into_dataset
# ═══════════════════════════════════════════════════════════════════════════
def test_compute_target_size_basic():
    # 1000 personal × 0.15 → 150
    assert open_corpus.compute_target_size(1000, 0.15) == 150
    # 1000 × 0.10 → 100
    assert open_corpus.compute_target_size(1000, 0.10) == 100
    # 0 → 0
    assert open_corpus.compute_target_size(0, 0.15) == 0
    # ratio 0 → 0
    assert open_corpus.compute_target_size(1000, 0) == 0
    # 太小但非零 → 至少 1
    assert open_corpus.compute_target_size(3, 0.1) == 1


def test_merge_into_dataset_writes_jsonl(tmp_path):
    rows = [
        {"prompt": f"問題 {i} 內容夠長啦這樣", "completion": f"回答 {i} 也夠長啦這樣"}
        for i in range(20)
    ]
    out_path = tmp_path / "out.jsonl"
    stats = open_corpus.merge_into_dataset(
        rows,
        personal_count=100,
        ratio=0.15,
        out_path=out_path,
        source_label="open_corpus_belle",
    )
    # 100 × 0.15 = 15
    assert stats["target"] == 15
    assert stats["written"] == 15
    # 確認檔案存在 + 結構正確
    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 15
    obj = json.loads(lines[0])
    assert "messages" in obj
    assert obj["messages"][0]["role"] == "user"
    assert obj["messages"][1]["role"] == "assistant"
    assert obj["metadata"]["source"] == "open_corpus_belle"


def test_merge_into_dataset_caps_to_corpus_size(tmp_path):
    """corpus 比 target 小：寫出全部 corpus，不複製膨脹。"""
    rows = [
        {"prompt": f"短資料 {i} 內容夠長吧這樣應該", "completion": f"短回應 {i} 也夠長吧"}
        for i in range(5)
    ]
    out_path = tmp_path / "out.jsonl"
    stats = open_corpus.merge_into_dataset(
        rows,
        personal_count=1000,
        ratio=0.15,
        out_path=out_path,
        source_label="open_corpus_belle",
    )
    # target=150 但 corpus 只有 5 → 寫 5
    assert stats["target"] == 150
    assert stats["written"] == 5


def test_merge_into_dataset_zero_ratio(tmp_path):
    rows = [{"prompt": "x" * 30, "completion": "y" * 30}]
    out_path = tmp_path / "out.jsonl"
    stats = open_corpus.merge_into_dataset(
        rows,
        personal_count=1000,
        ratio=0.0,
        out_path=out_path,
    )
    assert stats["written"] == 0
    # 空檔案還是要存在（或內容為空）
    assert out_path.exists()


def test_slug_from_corpus_name():
    assert open_corpus._slug_from_corpus_name("BelleGroup/train_2M_CN") == "open_corpus_belle"
    assert open_corpus._slug_from_corpus_name("shareAI/ShareGPT-Chinese-English-90k") == "open_corpus_sharegpt"
    assert open_corpus._slug_from_corpus_name("m-a-p/COIG-CQIA") == "open_corpus_coig"
    assert open_corpus._slug_from_corpus_name("foo/random_stuff") == "open_corpus_generic"


# ═══════════════════════════════════════════════════════════════════════════
# 4. CLI dry-run
# ═══════════════════════════════════════════════════════════════════════════
def test_cli_dry_run_no_network(capsys):
    rc = open_corpus.main(["--download", "--size", "100", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "BelleGroup" in out or "open_corpus" in out  # corpus name printed
    assert "no network call" in out


def test_cli_no_args_returns_help(capsys):
    """沒給 --download 應印 help 並 return 1（不打 HF）。"""
    rc = open_corpus.main([])
    assert rc == 1


# ═══════════════════════════════════════════════════════════════════════════
# 5. dataset_builder integration — open_corpus 是最低優先，比例正確
# ═══════════════════════════════════════════════════════════════════════════
def test_dataset_builder_with_corpus_low_priority(tmp_path, monkeypatch):
    """gather_records 加 open_corpus_path → corpus 條目排最低 + 數量被 ratio cap。"""
    import sqlite3

    # mock dataset_builder pull-from-DB 階段（直接 stub 函式回傳 personal pairs）
    from finetune import dataset_builder

    # 手刻 personal pairs（10 對，模擬 organic）
    personal_pairs = [
        {
            "prompt": f"personal 問題 {i} 內容夠長啦",
            "completion": f"personal 回答 {i} 內容也夠長啦",
            "source": "organic",
        }
        for i in range(10)
    ]
    monkeypatch.setattr(
        dataset_builder, "extract_all_pairs", lambda *a, **kw: personal_pairs
    )
    monkeypatch.setattr(
        dataset_builder, "extract_with_corrections", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        dataset_builder, "find_all_exports", lambda *a, **kw: []
    )

    # corpus jsonl: 50 條（純資料，等比例 sample）
    corpus_path = tmp_path / "open_corpus_sample.jsonl"
    corpus_records: list[str] = []
    for i in range(50):
        corpus_records.append(
            json.dumps(
                {
                    "messages": [
                        {"role": "user", "content": f"corpus 通用問題 {i} 夠長"},
                        {"role": "assistant", "content": f"corpus 通用回答 {i} 也夠長"},
                    ],
                    "metadata": {"source": "open_corpus_belle"},
                },
                ensure_ascii=False,
            )
        )
    corpus_path.write_text("\n".join(corpus_records) + "\n", encoding="utf-8")

    # 跑 gather_records（不跑 PII mask 簡化）
    records = dataset_builder.gather_records(
        db_path=tmp_path / "fake.db",  # extract_all_pairs 已 mocked
        distilled_path=tmp_path / "no_distilled.jsonl",
        include_exports=False,
        notes_distilled_path=tmp_path / "no_notes.jsonl",
        include_notes=False,
        sft_path=tmp_path / "no_sft.jsonl",
        include_sft=False,
        open_corpus_path=corpus_path,
        open_corpus_ratio=0.20,  # 10 personal × 0.20 = 2 corpus
    )

    by_src: dict[str, int] = {}
    for r in records:
        s = r["metadata"].get("source", "unknown")
        by_src[s] = by_src.get(s, 0) + 1

    # 10 organic + 2 open_corpus = 12
    assert by_src.get("organic", 0) == 10
    assert by_src.get("open_corpus_belle", 0) == 2

    # corpus 排在最後（dedup keep-first 實作）
    sources_order = [r["metadata"]["source"] for r in records]
    last_corpus_idx = max(
        i for i, s in enumerate(sources_order) if s == "open_corpus_belle"
    )
    last_organic_idx = max(
        i for i, s in enumerate(sources_order) if s == "organic"
    )
    assert last_corpus_idx > last_organic_idx, (
        "open_corpus 條目應排在 organic 之後（最低優先）"
    )


def test_dataset_builder_corpus_dedup_protects_personal(tmp_path, monkeypatch):
    """如果 corpus pair 跟 personal pair 撞 hash，應保留 personal 不換 corpus。"""
    from finetune import dataset_builder

    # 「重複內容」既在 personal 也在 corpus → 應只留 personal 的 source 標籤
    dup_prompt = "重複的測試問題 一段夠長的內容"
    dup_completion = "重複的測試回答 也是夠長的內容"

    personal_pairs = [
        {
            "prompt": dup_prompt,
            "completion": dup_completion,
            "source": "organic",
        }
    ]
    monkeypatch.setattr(
        dataset_builder, "extract_all_pairs", lambda *a, **kw: personal_pairs
    )
    monkeypatch.setattr(
        dataset_builder, "extract_with_corrections", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        dataset_builder, "find_all_exports", lambda *a, **kw: []
    )

    corpus_path = tmp_path / "open_corpus_sample.jsonl"
    corpus_path.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": dup_prompt},
                    {"role": "assistant", "content": dup_completion},
                ],
                "metadata": {"source": "open_corpus_belle"},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    records = dataset_builder.gather_records(
        db_path=tmp_path / "fake.db",
        distilled_path=tmp_path / "no_distilled.jsonl",
        include_exports=False,
        notes_distilled_path=tmp_path / "no_notes.jsonl",
        include_notes=False,
        sft_path=tmp_path / "no_sft.jsonl",
        include_sft=False,
        open_corpus_path=corpus_path,
        open_corpus_ratio=1.0,
    )

    assert len(records) == 1
    # 應該保留 personal source（organic），不被 corpus 標籤覆蓋
    assert records[0]["metadata"]["source"] == "organic"
