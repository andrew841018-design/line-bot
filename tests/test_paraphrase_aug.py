"""Tests for finetune/paraphrase_aug.py.

Mock local_llm.chat → 不真的載 Qwen2.5-14B（測試環境沒有 GPU/RAM 跑 14B）。
覆蓋：
  (a) parse N 個變體（含編號/引號髒污）
  (b) char-overlap < 0.3 過濾（hallucination guard）
  (c) chat 失敗 → 回 [] 不阻塞
  (d) augment_pair 結構（保留原 pair 在第 0 個）
  (e) max_pairs cap
  (f) augment_dataset jsonl 讀寫
  (g) estimate_runtime 數學
  (h) CLI dry-run 印格式
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

LINE_BOT_ROOT = Path(__file__).resolve().parent.parent
if str(LINE_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(LINE_BOT_ROOT))

from finetune import paraphrase_aug  # noqa: E402


# ─── helpers ─────────────────────────────────────────────────────────────────
def _make_chat_fn(reply: str | None):
    """Make a fake chat function returning fixed reply (or None to simulate failure)."""
    def _chat(user_input, context=None, system_prompt="", max_tokens=400):
        return reply
    return _chat


def _make_chat_fn_raising(exc: Exception):
    def _chat(user_input, context=None, system_prompt="", max_tokens=400):
        raise exc
    return _chat


# ═════════════════════════════════════════════════════════════════════════════
# (a) parse N variants
# ═════════════════════════════════════════════════════════════════════════════
def test_parse_variants_clean_lines():
    """乾淨換行格式 → 抽 N 個變體。"""
    raw = "今天天氣真好啊\n今天天氣不錯\n今天天氣很棒呢\n今天天氣晴朗\n今天天氣超讚"
    out = paraphrase_aug._parse_variants(raw, n=5)
    assert len(out) == 5
    assert all("天氣" in v for v in out)


def test_parse_variants_strips_numbered_prefix():
    """1. / 2) / 3、 編號前綴要剝掉。"""
    raw = (
        "1. 今天天氣很好\n"
        "2) 今天天氣超棒\n"
        "3、 今天天氣不錯\n"
    )
    out = paraphrase_aug._parse_variants(raw, n=5)
    assert len(out) == 3
    for v in out:
        assert not v.startswith(("1", "2", "3"))
        assert "天氣" in v


def test_parse_variants_strips_quote_marks():
    """中文引號 / 英文引號要剝。"""
    raw = "「今天天氣真好」\n\"今天天氣超棒\"\n"
    out = paraphrase_aug._parse_variants(raw, n=5)
    assert len(out) == 2
    assert all(not v.startswith(("「", '"')) for v in out)


def test_parse_variants_skips_empty_lines():
    raw = "改寫一句話啊\n\n\n改寫第二句話\n   \n改寫第三句話"
    out = paraphrase_aug._parse_variants(raw, n=5)
    assert len(out) == 3


def test_parse_variants_caps_at_n():
    """超過 N 個只取前 N 個。"""
    raw = "改一\n改二\n改三\n改四\n改五\n改六\n改七"
    out = paraphrase_aug._parse_variants(raw, n=3)
    assert len(out) == 3


# ═════════════════════════════════════════════════════════════════════════════
# (b) char-overlap filter
# ═════════════════════════════════════════════════════════════════════════════
def test_char_overlap_identical():
    assert paraphrase_aug._char_overlap("abc", "abc") == 1.0


def test_char_overlap_disjoint():
    assert paraphrase_aug._char_overlap("abc", "xyz") == 0.0


def test_char_overlap_partial():
    """ '今天天氣' vs '今晚天氣' → 共 3/5 chars。"""
    score = paraphrase_aug._char_overlap("今天天氣", "今晚天氣")
    assert 0.4 < score < 0.8


def test_char_overlap_empty():
    assert paraphrase_aug._char_overlap("", "abc") == 0.0
    assert paraphrase_aug._char_overlap("abc", "") == 0.0


def test_paraphrase_filters_hallucination():
    """LLM 跑掉，吐了完全不相關的句子 → 該句要被過濾掉。"""
    # 原句「今天台北的天氣如何呢」 → LLM 同時吐一句相關 + 一句完全跑題的
    # 「明天倫敦會下雨嗎」（跟原句字符重疊極低）
    raw = (
        "今天台北天氣怎麼樣\n"
        "明天倫敦會不會下雨\n"  # ← 應被 hallucination filter 擋掉
        "請問今天台北的天氣\n"
    )
    chat_fn = _make_chat_fn(raw)
    out = paraphrase_aug.paraphrase(
        "今天台北的天氣如何呢", n=5, chat_fn=chat_fn,
    )
    # 「明天倫敦會不會下雨」跟「今天台北的天氣如何呢」共集很小 → 應被剔除
    assert all("倫敦" not in v for v in out)
    # 至少抽到 1 個合格變體（含「天氣」「台北」）
    assert len(out) >= 1


# ═════════════════════════════════════════════════════════════════════════════
# (c) graceful failure
# ═════════════════════════════════════════════════════════════════════════════
def test_paraphrase_returns_empty_on_chat_none():
    """chat_fn 回 None（LLM 載入失敗）→ paraphrase 回 []，不 raise。"""
    chat_fn = _make_chat_fn(None)
    out = paraphrase_aug.paraphrase("測試輸入", n=5, chat_fn=chat_fn)
    assert out == []


def test_paraphrase_returns_empty_on_chat_exception():
    """chat_fn raise → paraphrase 吞掉回 []。"""
    chat_fn = _make_chat_fn_raising(RuntimeError("模型壞了"))
    out = paraphrase_aug.paraphrase("測試輸入", n=5, chat_fn=chat_fn)
    assert out == []


def test_paraphrase_returns_empty_on_blank_input():
    """空字串 / 純空白 → 直接回 []，不 call LLM。"""
    called = {"n": 0}

    def _chat(*a, **kw):
        called["n"] += 1
        return "x"

    assert paraphrase_aug.paraphrase("", n=5, chat_fn=_chat) == []
    assert paraphrase_aug.paraphrase("   ", n=5, chat_fn=_chat) == []
    assert paraphrase_aug.paraphrase("test", n=0, chat_fn=_chat) == []
    assert called["n"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# (d) augment_pair structure
# ═════════════════════════════════════════════════════════════════════════════
def test_augment_pair_keeps_original_first():
    """augment_pair 第 0 個必須是原 pair。"""
    raw = "今天天氣怎樣呢\n今天的氣候如何呢\n今天天氣狀況如何"
    chat_fn = _make_chat_fn(raw)
    pairs = paraphrase_aug.augment_pair(
        "今天天氣如何", "晴朗 25 度", n=3, chat_fn=chat_fn,
    )
    # 至少 1 (原) + 一些變體
    assert len(pairs) >= 1
    assert pairs[0] == ("今天天氣如何", "晴朗 25 度")
    # 所有 completion 都是同一個（不改 completion）
    for p, c in pairs:
        assert c == "晴朗 25 度"


def test_augment_pair_no_variants_still_returns_original():
    """LLM 回空 → 至少還是要有原 pair。"""
    chat_fn = _make_chat_fn(None)
    pairs = paraphrase_aug.augment_pair(
        "測試問題", "測試回答", n=5, chat_fn=chat_fn,
    )
    assert len(pairs) == 1
    assert pairs[0] == ("測試問題", "測試回答")


# ═════════════════════════════════════════════════════════════════════════════
# (e) max_pairs cap + (f) augment_dataset
# ═════════════════════════════════════════════════════════════════════════════
def _write_jsonl(path: Path, recs: list[dict]):
    with path.open("w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _make_train_jsonl(path: Path, n: int):
    recs = []
    for i in range(n):
        recs.append({
            "messages": [
                {"role": "user", "content": f"問題 {i} 內容夠長啦"},
                {"role": "assistant", "content": f"回答 {i} 內容夠長啦"},
            ],
            "metadata": {"source": "organic", "pair_hash": f"hash_{i}"},
        })
    _write_jsonl(path, recs)


def test_augment_dataset_max_pairs_cap(tmp_path):
    """max_pairs=2 → 只處理前 2 對，雖然 input 有 5 對。"""
    in_path = tmp_path / "train.jsonl"
    out_path = tmp_path / "train_aug.jsonl"
    _make_train_jsonl(in_path, n=5)

    raw = "改寫一句話呀\n改寫第二種說法\n改寫第三種說法"
    chat_fn = _make_chat_fn(raw)

    stats = paraphrase_aug.augment_dataset(
        in_path, out_path, n=3, max_pairs=2,
        chat_fn=chat_fn, progress=False,
    )
    assert stats["input"] == 5
    assert stats["processed"] == 2
    # 每對寫 1 + (≤3) 變體 → output ≤ 8，至少 2（原 pair 一定有）
    assert 2 <= stats["output"] <= 8


def test_augment_dataset_writes_valid_jsonl(tmp_path):
    """寫出的 jsonl 每行都是合法 record，第一行是原 pair（augmented=False）。"""
    in_path = tmp_path / "train.jsonl"
    out_path = tmp_path / "train_aug.jsonl"
    _make_train_jsonl(in_path, n=2)

    raw = "改一啊\n改二呀\n改三呢"
    chat_fn = _make_chat_fn(raw)

    paraphrase_aug.augment_dataset(
        in_path, out_path, n=3, chat_fn=chat_fn, progress=False,
    )

    lines = out_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 2  # 至少每對的原 pair 各一行
    for line in lines:
        obj = json.loads(line)
        assert obj["messages"][0]["role"] == "user"
        assert obj["messages"][1]["role"] == "assistant"
        assert "metadata" in obj

    # 第一行該是 input 第 0 對的原 pair（augmented 應為 False/缺）
    first = json.loads(lines[0])
    assert first["metadata"].get("augmented") is not True
    assert "問題 0" in first["messages"][0]["content"]


def test_augment_dataset_marks_augmented_metadata(tmp_path):
    """變體 record 在 metadata.augmented=True 標記。"""
    in_path = tmp_path / "train.jsonl"
    out_path = tmp_path / "train_aug.jsonl"
    _make_train_jsonl(in_path, n=1)

    # 變體要跟原句「問題 0 內容夠長啦」共字夠多才能過 hallucination guard
    raw = "問題 0 的內容夠長啦呀\n第 0 個問題內容也夠長啦"
    chat_fn = _make_chat_fn(raw)
    paraphrase_aug.augment_dataset(
        in_path, out_path, n=2, chat_fn=chat_fn, progress=False,
    )

    lines = out_path.read_text(encoding="utf-8").strip().splitlines()
    aug_count = 0
    orig_count = 0
    for line in lines:
        obj = json.loads(line)
        if obj["metadata"].get("augmented"):
            aug_count += 1
            assert obj["metadata"].get("augment_method") == "paraphrase_local"
        else:
            orig_count += 1

    assert orig_count == 1  # 一對原始
    assert aug_count >= 1  # 至少 1 變體（過濾後可能少於 N）


# ═════════════════════════════════════════════════════════════════════════════
# (g) estimate_runtime
# ═════════════════════════════════════════════════════════════════════════════
def test_estimate_runtime_math(tmp_path):
    in_path = tmp_path / "train.jsonl"
    _make_train_jsonl(in_path, n=11)
    est = paraphrase_aug.estimate_runtime(in_path, n=5)
    assert est["input_pairs"] == 11
    assert est["processed_pairs"] == 11
    assert est["n"] == 5
    assert est["n_variants"] == 55  # 11 × 5
    assert est["expected_output"] == 11 * 6  # 11 × (5+1)
    assert est["total_seconds"] == 11 * paraphrase_aug.SECONDS_PER_PAIR_EST


def test_estimate_runtime_max_pairs(tmp_path):
    in_path = tmp_path / "train.jsonl"
    _make_train_jsonl(in_path, n=100)
    est = paraphrase_aug.estimate_runtime(in_path, n=3, max_pairs=10)
    assert est["input_pairs"] == 100
    assert est["processed_pairs"] == 10
    assert est["n_variants"] == 30


# ═════════════════════════════════════════════════════════════════════════════
# (h) CLI dry-run prints expected format
# ═════════════════════════════════════════════════════════════════════════════
def test_cli_dry_run_format(tmp_path, capsys):
    in_path = tmp_path / "train.jsonl"
    out_path = tmp_path / "train_aug.jsonl"
    _make_train_jsonl(in_path, n=11)

    rc = paraphrase_aug.main([
        "--in", str(in_path), "--out", str(out_path),
        "--n", "5", "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # 對應 spec 驗收：「11 對 → 5x = 55 對，... ~Z 分鐘」
    assert "11 對" in out
    assert "5x" in out
    assert "55 對" in out
    assert "分鐘" in out
