"""Tests for finetune/back_translate.py — pure mock，不真跑 14B。

mock 策略：
  把 sys.modules['local_llm'].chat 換成 deterministic stub，依 prompt 內容
  判斷是 zh→en or en→zh，回 hard-coded 譯文（含「不同 sampling 變不同」變體）。
"""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

# bootstrap env
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

LINE_BOT_ROOT = Path(__file__).resolve().parent.parent
if str(LINE_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(LINE_BOT_ROOT))

from finetune import back_translate as bt  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────
def _install_fake_local_llm(monkeypatch, zh_outputs, en_outputs):
    """zh->en 用 zh_outputs（list），en->zh 用 en_outputs（list）。

    第 i 次 zh->en 回 zh_outputs[i]，第 i 次 en->zh 回 en_outputs[i]。
    list 用完後回最後一個（不 IndexError）。回 None 模擬翻譯失敗。
    """
    counts = {"zh2en": 0, "en2zh": 0}

    def fake_chat(prompt, **kwargs):
        if "Translate the following Chinese to natural English" in prompt:
            i = counts["zh2en"]
            counts["zh2en"] += 1
            arr = zh_outputs
            return arr[i] if i < len(arr) else (arr[-1] if arr else None)
        if "將下面英文翻譯成自然的繁體中文" in prompt:
            i = counts["en2zh"]
            counts["en2zh"] += 1
            arr = en_outputs
            return arr[i] if i < len(arr) else (arr[-1] if arr else None)
        return None

    fake = types.ModuleType("local_llm")
    fake.chat = fake_chat
    monkeypatch.setitem(sys.modules, "local_llm", fake)
    return counts


# ═══════════════════════════════════════════════════════════════════════════
# 1. translate_zh_to_en / translate_en_to_zh
# ═══════════════════════════════════════════════════════════════════════════
def test_translate_zh_to_en_basic(monkeypatch):
    _install_fake_local_llm(
        monkeypatch,
        zh_outputs=["What's the weather today?"],
        en_outputs=[],
    )
    out = bt.translate_zh_to_en("今天天氣如何")
    assert out == "What's the weather today?"


def test_translate_zh_to_en_strips_label(monkeypatch):
    """LLM 偶爾仍輸出 'English: ...' → 應剝掉 prefix。"""
    _install_fake_local_llm(
        monkeypatch,
        zh_outputs=["English: What is going on here?"],
        en_outputs=[],
    )
    out = bt.translate_zh_to_en("這是什麼狀況")
    assert out == "What is going on here?"


def test_translate_en_to_zh_basic(monkeypatch):
    _install_fake_local_llm(
        monkeypatch,
        zh_outputs=[],
        en_outputs=["今天的天氣怎麼樣"],
    )
    out = bt.translate_en_to_zh("How's the weather today?")
    assert out == "今天的天氣怎麼樣"


def test_translate_returns_none_on_empty(monkeypatch):
    _install_fake_local_llm(monkeypatch, zh_outputs=[""], en_outputs=[""])
    assert bt.translate_zh_to_en("") is None
    assert bt.translate_zh_to_en("一些測試") is None  # LLM 回空字串
    assert bt.translate_en_to_zh("") is None


def test_translate_returns_none_on_local_llm_unavailable(monkeypatch):
    """模擬 local_llm import 失敗 — 注入會 raise ImportError 的假 module。"""
    # remove already-cached if any
    monkeypatch.delitem(sys.modules, "local_llm", raising=False)

    class _Bomb:
        def __getattr__(self, name):
            raise ImportError("simulated mlx_lm missing")

    # use import hook style: just inject a module whose chat raises
    fake = types.ModuleType("local_llm")
    def _bad_chat(*a, **k):
        raise RuntimeError("model not loaded")
    fake.chat = _bad_chat
    monkeypatch.setitem(sys.modules, "local_llm", fake)
    assert bt.translate_zh_to_en("測試") is None
    assert bt.translate_en_to_zh("test") is None


# ═══════════════════════════════════════════════════════════════════════════
# 2. back_translate (round-trip)
# ═══════════════════════════════════════════════════════════════════════════
def test_back_translate_one_pass_returns_variant(monkeypatch):
    _install_fake_local_llm(
        monkeypatch,
        zh_outputs=["What's the weather like today?"],
        en_outputs=["今天天氣怎麼樣呢"],   # 跟原句「今天天氣如何」字集 overlap > 0.3
    )
    variants = bt.back_translate("今天天氣如何", n_passes=1)
    assert variants == ["今天天氣怎麼樣呢"]


def test_back_translate_filters_identical(monkeypatch):
    """back-translated 跟原句完全一樣 → 過濾掉。"""
    _install_fake_local_llm(
        monkeypatch,
        zh_outputs=["How is today's weather"],
        en_outputs=["今天天氣如何"],   # 跟原句一字不差
    )
    variants = bt.back_translate("今天天氣如何", n_passes=1)
    assert variants == []


def test_back_translate_filters_low_overlap(monkeypatch):
    """back-translated 跟原句字集 overlap < 0.3 → 視為語意飄，過濾。"""
    _install_fake_local_llm(
        monkeypatch,
        zh_outputs=["What is the weather"],
        # 完全不同字集 — 無一個字重疊
        en_outputs=["全新無關內容ABCDEF測試亂碼XYZ"],
    )
    variants = bt.back_translate("今天天氣如何", n_passes=1)
    assert variants == []


def test_back_translate_multi_pass_dedup(monkeypatch):
    """3 輪 — 前兩輪一樣（dedup），第三輪不同 → 最後 2 個變體。"""
    _install_fake_local_llm(
        monkeypatch,
        zh_outputs=["Q1", "Q2", "Q3"],
        en_outputs=["今天天氣怎樣呢", "今天天氣怎樣呢", "今天的天氣狀況如何"],
    )
    variants = bt.back_translate("今天天氣如何", n_passes=3)
    assert len(variants) == 2
    assert "今天天氣怎樣呢" in variants
    assert "今天的天氣狀況如何" in variants


def test_back_translate_skips_failed_translation(monkeypatch):
    """中→英 fail → 該輪整個跳過，不往下走 en→zh。"""
    _install_fake_local_llm(
        monkeypatch,
        zh_outputs=[None, "How's the weather today?"],
        en_outputs=["今天天氣怎麼樣呢"],
    )
    variants = bt.back_translate("今天天氣如何", n_passes=2)
    # 第一輪 zh2en None → skip；第二輪成功
    assert variants == ["今天天氣怎麼樣呢"]


def test_back_translate_zero_passes(monkeypatch):
    _install_fake_local_llm(monkeypatch, zh_outputs=["x"], en_outputs=["y"])
    assert bt.back_translate("一些測試文字", n_passes=0) == []


# ═══════════════════════════════════════════════════════════════════════════
# 3. augment_pair_bt + augment_dataset_bt
# ═══════════════════════════════════════════════════════════════════════════
def test_augment_pair_bt_returns_original_plus_variants(monkeypatch):
    _install_fake_local_llm(
        monkeypatch,
        zh_outputs=["E1", "E2"],
        en_outputs=["今天天氣怎樣呢", "今天的天氣狀況如何"],
    )
    pairs = bt.augment_pair_bt("今天天氣如何", "天氣晴朗喔", n=2)
    # [(原, 原), (var1, 原), (var2, 原)]
    assert len(pairs) == 3
    assert pairs[0] == ("今天天氣如何", "天氣晴朗喔")
    assert pairs[1][1] == "天氣晴朗喔"  # completion 不動
    assert pairs[2][1] == "天氣晴朗喔"
    assert pairs[1][0] != "今天天氣如何"


def test_augment_pair_bt_all_failed_returns_only_original(monkeypatch):
    """back-translate 全失敗 → 至少回原 pair。"""
    _install_fake_local_llm(
        monkeypatch,
        zh_outputs=[None, None],
        en_outputs=[],
    )
    pairs = bt.augment_pair_bt("今天天氣如何", "天氣晴朗喔", n=2)
    assert pairs == [("今天天氣如何", "天氣晴朗喔")]


def test_augment_dataset_bt_dry_run(tmp_path, monkeypatch):
    """dry-run 不呼叫翻譯，純估算。"""
    in_path = tmp_path / "in.jsonl"
    out_path = tmp_path / "out.jsonl"
    in_path.write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "問題一這是測試內容"},
                {"role": "assistant", "content": "回答一也是測試"},
            ],
            "metadata": {"source": "organic"},
        }, ensure_ascii=False) + "\n" +
        json.dumps({
            "messages": [
                {"role": "user", "content": "問題二的測試內容啦"},
                {"role": "assistant", "content": "回答二也夠長啦"},
            ],
            "metadata": {"source": "organic"},
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    stats = bt.augment_dataset_bt(in_path, out_path, n=2, dry_run=True)
    assert stats["input"] == 2
    assert stats["eta_sec"] > 0
    assert stats["added"] == 4   # 2 input * n=2
    assert not out_path.exists()  # dry-run 不寫檔


def test_augment_dataset_bt_writes_file_with_variants(tmp_path, monkeypatch):
    """實跑（mock 後）會寫出 augmented jsonl。"""
    _install_fake_local_llm(
        monkeypatch,
        zh_outputs=["E1", "E2", "E1B", "E2B"],
        en_outputs=[
            "問題一的另一種說法測試",       # vs orig "問題一這是測試內容" → overlap > 0.3
            "問題一的不同表述方式測試",
            "問題二的另類測試表達啦",       # vs orig "問題二的測試內容啦"
            "問題二的不同說法內容啦",
        ],
    )
    in_path = tmp_path / "in.jsonl"
    out_path = tmp_path / "out.jsonl"
    in_path.write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "問題一這是測試內容"},
                {"role": "assistant", "content": "回答一也是測試"},
            ],
            "metadata": {"source": "organic"},
        }, ensure_ascii=False) + "\n" +
        json.dumps({
            "messages": [
                {"role": "user", "content": "問題二的測試內容啦"},
                {"role": "assistant", "content": "回答二也夠長啦"},
            ],
            "metadata": {"source": "organic"},
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    stats = bt.augment_dataset_bt(in_path, out_path, n=2, dry_run=False)
    assert stats["input"] == 2
    assert stats["kept"] == 2
    assert stats["added"] == 4
    assert stats["failed"] == 0

    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6  # 2 originals + 4 augmented
    objs = [json.loads(line) for line in lines]
    aug = [o for o in objs if o["metadata"].get("source") == "back_translate"]
    assert len(aug) == 4
    # completion 不變
    completions = {o["messages"][1]["content"] for o in aug}
    assert completions <= {"回答一也是測試", "回答二也夠長啦"}


def test_augment_dataset_bt_max_pairs(tmp_path, monkeypatch):
    _install_fake_local_llm(
        monkeypatch,
        zh_outputs=["E"],
        en_outputs=["問題一的另一種說法測試"],
    )
    in_path = tmp_path / "in.jsonl"
    out_path = tmp_path / "out.jsonl"
    lines_in = []
    for i in range(5):
        lines_in.append(json.dumps({
            "messages": [
                {"role": "user", "content": f"問題{i}的測試內容啦長度OK"},
                {"role": "assistant", "content": f"回答{i}也夠長啦"},
            ],
            "metadata": {"source": "organic"},
        }, ensure_ascii=False))
    in_path.write_text("\n".join(lines_in) + "\n", encoding="utf-8")

    stats = bt.augment_dataset_bt(in_path, out_path, n=1, max_pairs=2)
    # 只處理前 2 筆（input 統計也只算 2）
    assert stats["input"] == 2


# ═══════════════════════════════════════════════════════════════════════════
# 4. CLI
# ═══════════════════════════════════════════════════════════════════════════
def test_cli_dry_run_prints_eta(tmp_path, capsys):
    in_path = tmp_path / "in.jsonl"
    out_path = tmp_path / "out.jsonl"
    in_path.write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "測試問題啊內容夠長"},
                {"role": "assistant", "content": "測試回答啊也夠長"},
            ],
            "metadata": {},
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    rc = bt.main(["--in", str(in_path), "--out", str(out_path),
                  "--n", "2", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "ETA" in out
    assert "input=1" in out
