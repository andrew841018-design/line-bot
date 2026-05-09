"""Tests for finetune/self_distill_notes.py + dataset_builder integration.

新版 (2026-05-08)：Q / A 分離 pipeline
- Q 從 chunk 抽（用本機 14B / mock）
- A 透過 Gemini 生成（mock gemini_client.chat 回 JSON list）
- 測 batching、progress 持久化、quota 上限、dry-run 估算
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# bootstrap (matches conftest style — main.py needs these envs to import)
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

LINE_BOT_ROOT = Path(__file__).resolve().parent.parent
if str(LINE_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(LINE_BOT_ROOT))

from finetune import dataset_builder, self_distill_notes  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────
def _mk_md(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# 14B mock：抽 N 條問題（plain-text，每行一條）
def _mock_local_qs_3lines(*args, **kwargs) -> str:
    return "Q1 是什麼？\nQ2 為什麼這樣？\nQ3 怎麼做？\n"


def _mock_local_qs_5lines(*args, **kwargs) -> str:
    return "\n".join([f"問題 {i}？" for i in range(1, 6)])


def _mock_local_qs_with_numbering(*args, **kwargs) -> str:
    return (
        "1. 第一個問題是什麼？\n"
        "2) 第二個問題為何？\n"
        "- 第三個怎麼做？\n"
        "* 第四個有什麼風險？\n"
    )


def _mock_local_qs_empty(*args, **kwargs) -> str:
    return ""


def _mock_local_qs_garbage(*args, **kwargs) -> str:
    return "嗨"  # too-short → filtered out


def _mock_local_qs_raises(*args, **kwargs):
    raise RuntimeError("local LLM 沒載入")


# Gemini mock：回 JSON list（簽名同 gemini_client.chat）
def _mock_gemini_chat_3pairs(user_input, context, facts, **kwargs) -> str:
    return json.dumps([
        {"q": "Q1 是什麼？", "a": "咪寶覺得 A1 內容（具體判斷句）"},
        {"q": "Q2 為什麼這樣？", "a": "咪寶覺得 A2 內容（具體判斷句）"},
        {"q": "Q3 怎麼做？", "a": "咪寶覺得 A3 內容（具體判斷句）"},
    ], ensure_ascii=False)


def _mock_gemini_chat_with_fences(user_input, context, facts, **kwargs) -> str:
    return (
        "好的，這是回應：\n"
        "```json\n"
        + json.dumps([
            {"q": "包 fence 的問題", "a": "包 fence 的回答"},
        ], ensure_ascii=False)
        + "\n```\n"
    )


def _mock_gemini_chat_garbage(user_input, context, facts, **kwargs) -> str:
    return "我覺得這段不錯但不會給你 JSON 哈"


def _mock_gemini_chat_none(user_input, context, facts, **kwargs):
    return None


def _mock_gemini_chat_raises(*args, **kwargs):
    raise RuntimeError("Gemini quota 爆了")


class _GeminiCallCounter:
    """記 Gemini call 次數，每次回 1 對 pair。"""

    def __init__(self):
        self.count = 0

    def __call__(self, user_input, context, facts, **kwargs):
        self.count += 1
        return json.dumps([
            {"q": f"問題 {self.count}", "a": f"咪寶答 {self.count}"},
        ], ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════
# find_note_files
# ═══════════════════════════════════════════════════════════════════════════
def test_find_note_files_basic(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    _mk_md(root_a / "x.md", "# X\nhi")
    _mk_md(root_a / "y.md", "# Y\nbye")
    _mk_md(root_b / "z.md", "# Z\n")
    (root_a / "ignored.txt").write_text("nope")

    found = self_distill_notes.find_note_files([root_a, root_b])
    names = sorted(p.name for p in found)
    assert names == ["x.md", "y.md", "z.md"]


def test_find_note_files_skips_excluded_dirs(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    keep = root / "kept.md"
    _mk_md(keep, "# kept")
    venv = root / ".venv"
    venv.mkdir()
    _mk_md(venv / "skip.md", "should not be picked up")
    git = root / ".git"
    git.mkdir()
    _mk_md(git / "logs.md", "git internal")

    found = self_distill_notes.find_note_files([root])
    assert len(found) == 1
    assert found[0].name == "kept.md"


def test_find_note_files_missing_root_ok(tmp_path):
    found = self_distill_notes.find_note_files(
        [tmp_path / "ghost", tmp_path / "alsoghost"]
    )
    assert found == []


# ═══════════════════════════════════════════════════════════════════════════
# extract_chunks
# ═══════════════════════════════════════════════════════════════════════════
def test_extract_chunks_splits_on_h2(tmp_path):
    body = (
        "# Title\n\n"
        "## Section 1\n\n"
        + ("這是第一段內容。" * 20) + "\n\n"
        "## Section 2\n\n"
        + ("這是第二段內容。" * 20) + "\n"
    )
    f = _mk_md(tmp_path / "n.md", body)
    chunks = self_distill_notes.extract_chunks(f, chunk_size=500)
    assert len(chunks) >= 2
    assert any("Section 1" in c for c in chunks)
    assert any("Section 2" in c for c in chunks)


def test_extract_chunks_filters_too_short(tmp_path):
    f = _mk_md(tmp_path / "tiny.md", "# X\n太短")
    chunks = self_distill_notes.extract_chunks(f)
    assert chunks == []


def test_extract_chunks_paragraph_split_on_oversized_section(tmp_path):
    para = "這是一個段落這是一個段落這是一個段落這是一個段落這是一個段落。"
    body = "## 大塊\n\n" + (para + "\n\n") * 12
    f = _mk_md(tmp_path / "big.md", body)
    chunks = self_distill_notes.extract_chunks(f, chunk_size=300)
    assert len(chunks) >= 2
    for c in chunks:
        assert (
            self_distill_notes.MIN_CHUNK_LEN
            <= len(c)
            <= self_distill_notes.MAX_CHUNK_LEN
        )


def test_extract_chunks_handles_bad_encoding(tmp_path):
    f = tmp_path / "bad.md"
    f.write_bytes(b"\xff\xfe broken bytes \x00")
    chunks = self_distill_notes.extract_chunks(f)
    assert chunks == []


# ═══════════════════════════════════════════════════════════════════════════
# extract_questions (Step A — local 14B)
# ═══════════════════════════════════════════════════════════════════════════
def test_extract_questions_basic():
    qs = self_distill_notes.extract_questions(
        "一段測試內容 " * 20, n=3, chat_fn=_mock_local_qs_3lines,
    )
    assert qs == ["Q1 是什麼？", "Q2 為什麼這樣？", "Q3 怎麼做？"]


def test_extract_questions_strips_numbering_and_bullets():
    qs = self_distill_notes.extract_questions(
        "x" * 200, n=4, chat_fn=_mock_local_qs_with_numbering,
    )
    assert qs == [
        "第一個問題是什麼？",
        "第二個問題為何？",
        "第三個怎麼做？",
        "第四個有什麼風險？",
    ]


def test_extract_questions_caps_at_n():
    qs = self_distill_notes.extract_questions(
        "x" * 200, n=3, chat_fn=_mock_local_qs_5lines,
    )
    assert len(qs) == 3


def test_extract_questions_empty_response_returns_empty():
    qs = self_distill_notes.extract_questions(
        "x" * 200, n=3, chat_fn=_mock_local_qs_empty,
    )
    assert qs == []


def test_extract_questions_garbage_returns_empty():
    qs = self_distill_notes.extract_questions(
        "x" * 200, n=3, chat_fn=_mock_local_qs_garbage,
    )
    assert qs == []  # 太短被過濾


def test_extract_questions_chat_raises_graceful():
    qs = self_distill_notes.extract_questions(
        "x" * 200, n=3, chat_fn=_mock_local_qs_raises,
    )
    assert qs == []


def test_extract_questions_empty_chunk():
    qs = self_distill_notes.extract_questions("", n=3, chat_fn=_mock_local_qs_3lines)
    assert qs == []


# ═══════════════════════════════════════════════════════════════════════════
# generate_answers_via_gemini (Step B — Gemini batched)
# ═══════════════════════════════════════════════════════════════════════════
def test_generate_answers_via_gemini_batched():
    qs = ["Q1 是什麼？", "Q2 為什麼這樣？", "Q3 怎麼做？"]
    pairs = self_distill_notes.generate_answers_via_gemini(
        qs, "context chunk", gemini_chat=_mock_gemini_chat_3pairs,
    )
    assert len(pairs) == 3
    # Q 對齊原始 list（不被 Gemini 改寫）
    assert [q for q, _ in pairs] == qs
    assert pairs[0][1].startswith("咪寶覺得 A1")


def test_generate_answers_via_gemini_one_call_for_batch():
    """確認 Gemini 只被 call 一次（batching 省 quota 的核心）。"""
    counter = _GeminiCallCounter()
    qs = ["Q1", "Q2", "Q3", "Q4", "Q5"]
    self_distill_notes.generate_answers_via_gemini(qs, "chunk", gemini_chat=counter)
    assert counter.count == 1


def test_generate_answers_via_gemini_strips_code_fences():
    qs = ["Q only"]
    pairs = self_distill_notes.generate_answers_via_gemini(
        qs, "chunk", gemini_chat=_mock_gemini_chat_with_fences,
    )
    assert len(pairs) == 1
    # Q 來自原 list（對齊保留）
    assert pairs[0][0] == "Q only"
    assert pairs[0][1] == "包 fence 的回答"


def test_generate_answers_via_gemini_garbage_returns_empty():
    pairs = self_distill_notes.generate_answers_via_gemini(
        ["Q?"], "chunk", gemini_chat=_mock_gemini_chat_garbage,
    )
    assert pairs == []


def test_generate_answers_via_gemini_none_response():
    pairs = self_distill_notes.generate_answers_via_gemini(
        ["Q?"], "chunk", gemini_chat=_mock_gemini_chat_none,
    )
    assert pairs == []


def test_generate_answers_via_gemini_raises_graceful():
    pairs = self_distill_notes.generate_answers_via_gemini(
        ["Q?"], "chunk", gemini_chat=_mock_gemini_chat_raises,
    )
    assert pairs == []


def test_generate_answers_via_gemini_empty_questions():
    pairs = self_distill_notes.generate_answers_via_gemini(
        [], "chunk", gemini_chat=_mock_gemini_chat_3pairs,
    )
    assert pairs == []


# ═══════════════════════════════════════════════════════════════════════════
# generate_qa_pair (整合：14B → Gemini)
# ═══════════════════════════════════════════════════════════════════════════
def test_generate_qa_pair_full_pipeline():
    pairs = self_distill_notes.generate_qa_pair(
        "一段測試內容 " * 20,
        questions_chat_fn=_mock_local_qs_3lines,
        gemini_chat_fn=_mock_gemini_chat_3pairs,
        n=3,
    )
    assert len(pairs) == 3
    # 第一個 Q 應對齊到 14B 抽出的 Q
    assert pairs[0][0] == "Q1 是什麼？"


def test_generate_qa_pair_no_questions_skips_gemini():
    """14B 抽不到 Q → 不該 call Gemini。"""
    counter = _GeminiCallCounter()
    pairs = self_distill_notes.generate_qa_pair(
        "x" * 200,
        questions_chat_fn=_mock_local_qs_empty,
        gemini_chat_fn=counter,
    )
    assert pairs == []
    assert counter.count == 0


def test_generate_qa_pair_empty_chunk():
    pairs = self_distill_notes.generate_qa_pair(
        "", questions_chat_fn=_mock_local_qs_3lines,
        gemini_chat_fn=_mock_gemini_chat_3pairs,
    )
    assert pairs == []


# ═══════════════════════════════════════════════════════════════════════════
# build_distill_jsonl + progress + quota
# ═══════════════════════════════════════════════════════════════════════════
def _mk_two_chunk_note(note_dir: Path) -> Path:
    body = (
        "# Topic\n\n"
        "## Section 1\n\n"
        + "這是 section 一的測試內容寫得長一些至少超過一百個字才會被當成有效 chunk。" * 5
        + "\n\n"
        "## Section 2\n\n"
        + "這是 section 二的測試內容寫得長一些至少超過一百個字才會被當成有效 chunk。" * 5
    )
    return _mk_md(note_dir / "a.md", body)


def test_build_distill_jsonl_writes_pairs(tmp_path):
    note_dir = tmp_path / "notes"
    note_dir.mkdir()
    _mk_two_chunk_note(note_dir)

    out = tmp_path / "out.jsonl"
    progress = tmp_path / "progress.json"
    stats = self_distill_notes.build_distill_jsonl(
        out_path=out,
        note_roots=[note_dir],
        questions_chat_fn=_mock_local_qs_3lines,
        gemini_chat_fn=_mock_gemini_chat_3pairs,
        batch_size=3,
        max_gemini_calls=10,
        progress_path=progress,
    )
    # 2 chunk × 3 pair / chunk = 6 pair；2 Gemini call
    assert stats["files"] == 1
    assert stats["chunks"] >= 2
    assert stats["pairs"] >= 6
    assert stats["gemini_calls"] == 2
    assert stats["gemini_calls_remaining"] == 8

    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == stats["pairs"]
    rec = json.loads(lines[0])
    assert rec["messages"][0]["role"] == "user"
    assert rec["messages"][1]["role"] == "assistant"
    assert rec["metadata"]["source"] == "notes_distilled"
    assert "source_file" in rec["metadata"]


def test_build_distill_jsonl_max_gemini_calls_caps(tmp_path):
    """跑到 --max-gemini-calls 即停（quota 保護）。"""
    note_dir = tmp_path / "notes"
    note_dir.mkdir()
    # 5 個不同 H2 section（避免 hash 撞 → dedup 提前停）
    body = "# T\n\n" + "".join(
        f"## Section {i}\n\n"
        + f"這是 section 第 {i} 個測試內容寫得長至少超過一百字才會通過過濾。"
        * 5
        + "\n\n"
        for i in range(5)
    )
    _mk_md(note_dir / "a.md", body)
    out = tmp_path / "out.jsonl"
    progress = tmp_path / "progress.json"

    counter = _GeminiCallCounter()
    stats = self_distill_notes.build_distill_jsonl(
        out_path=out, note_roots=[note_dir],
        questions_chat_fn=_mock_local_qs_3lines,
        gemini_chat_fn=counter,
        max_gemini_calls=2,
        progress_path=progress,
    )
    # 即便有 5 chunk，最多只 call Gemini 2 次
    assert stats["chunks"] == 5
    assert stats["gemini_calls"] == 2
    assert counter.count == 2
    assert stats["gemini_calls_remaining"] == 0


def test_build_distill_jsonl_progress_resume(tmp_path):
    """第一次跑完後第二次再跑，已處理的 chunk 應跳過。"""
    note_dir = tmp_path / "notes"
    note_dir.mkdir()
    _mk_two_chunk_note(note_dir)

    out = tmp_path / "out.jsonl"
    progress = tmp_path / "progress.json"

    # 第一次：處理 1 chunk
    stats1 = self_distill_notes.build_distill_jsonl(
        out_path=out, note_roots=[note_dir],
        questions_chat_fn=_mock_local_qs_3lines,
        gemini_chat_fn=_mock_gemini_chat_3pairs,
        batch_size=3, max_gemini_calls=1,
        progress_path=progress,
    )
    assert stats1["gemini_calls"] == 1
    assert stats1["processed"] == 1
    pairs_after_run1 = stats1["pairs"]

    # progress.json 應寫入 1 個 hash
    pdata = json.loads(progress.read_text(encoding="utf-8"))
    assert len(pdata["processed_hashes"]) == 1
    assert pdata["pairs_total"] == pairs_after_run1
    assert pdata["gemini_calls_total"] == 1

    # 第二次：上次處理過的應 skip，剩下的繼續處理
    stats2 = self_distill_notes.build_distill_jsonl(
        out_path=out, note_roots=[note_dir],
        questions_chat_fn=_mock_local_qs_3lines,
        gemini_chat_fn=_mock_gemini_chat_3pairs,
        batch_size=3, max_gemini_calls=10,
        progress_path=progress,
    )
    assert stats2["skipped_resume"] >= 1  # 第一次的 chunk 被 skip
    assert stats2["gemini_calls"] == 1   # 只剩 1 chunk 沒做
    # jsonl 應 append（總 pair = 6）
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 6


def test_build_distill_jsonl_no_resume_starts_fresh(tmp_path):
    note_dir = tmp_path / "notes"
    note_dir.mkdir()
    _mk_two_chunk_note(note_dir)

    out = tmp_path / "out.jsonl"
    progress = tmp_path / "progress.json"
    progress.write_text(json.dumps({
        "processed_hashes": ["fakehash1", "fakehash2"],
        "gemini_calls_total": 50, "pairs_total": 150, "last_run_at": 0,
    }), encoding="utf-8")

    stats = self_distill_notes.build_distill_jsonl(
        out_path=out, note_roots=[note_dir],
        questions_chat_fn=_mock_local_qs_3lines,
        gemini_chat_fn=_mock_gemini_chat_3pairs,
        batch_size=3, max_gemini_calls=10,
        progress_path=progress,
        resume=False,
    )
    # resume=False → 即便 progress 有舊 hash 也忽略
    assert stats["skipped_resume"] == 0
    assert stats["processed"] >= 2


def test_build_distill_jsonl_handles_local_q_failure(tmp_path):
    """14B 抽 Q 失敗 → errors 增、不 call Gemini。"""
    note_dir = tmp_path / "notes"
    note_dir.mkdir()
    body = "## H\n\n" + "這是測試內容寫得長至少超過一百字才會通過過濾門檻足夠嗎。" * 5
    _mk_md(note_dir / "a.md", body)

    out = tmp_path / "out.jsonl"
    progress = tmp_path / "progress.json"
    counter = _GeminiCallCounter()
    stats = self_distill_notes.build_distill_jsonl(
        out_path=out, note_roots=[note_dir],
        questions_chat_fn=_mock_local_qs_empty,  # 抽不到 Q
        gemini_chat_fn=counter,
        max_gemini_calls=10,
        progress_path=progress,
    )
    assert stats["pairs"] == 0
    assert stats["errors"] >= 1
    assert counter.count == 0  # Q 抽不到 → Gemini 一次都沒 call


def test_build_distill_jsonl_handles_gemini_failure(tmp_path):
    """Gemini 解析失敗 → errors 增；該 chunk 仍記入 progress 避免下次重 call。"""
    note_dir = tmp_path / "notes"
    note_dir.mkdir()
    body = "## H\n\n" + "這是測試內容寫得長至少超過一百字才會通過過濾門檻足夠嗎。" * 5
    _mk_md(note_dir / "a.md", body)

    out = tmp_path / "out.jsonl"
    progress = tmp_path / "progress.json"
    stats = self_distill_notes.build_distill_jsonl(
        out_path=out, note_roots=[note_dir],
        questions_chat_fn=_mock_local_qs_3lines,
        gemini_chat_fn=_mock_gemini_chat_garbage,
        max_gemini_calls=10,
        progress_path=progress,
    )
    assert stats["pairs"] == 0
    assert stats["errors"] >= 1
    # 進度檔仍應記入該 chunk hash（避免下次重 call Gemini）
    pdata = json.loads(progress.read_text(encoding="utf-8"))
    assert len(pdata["processed_hashes"]) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# Progress file load/save
# ═══════════════════════════════════════════════════════════════════════════
def test_progress_load_missing_file_returns_default(tmp_path):
    p = tmp_path / "no_such.json"
    data = self_distill_notes._load_progress(p)
    assert data["processed_hashes"] == []
    assert data["gemini_calls_total"] == 0
    assert data["pairs_total"] == 0


def test_progress_load_corrupt_file_returns_default(tmp_path):
    p = tmp_path / "corrupt.json"
    p.write_text("{not valid json", encoding="utf-8")
    data = self_distill_notes._load_progress(p)
    assert data["processed_hashes"] == []


def test_progress_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "ok.json"
    self_distill_notes._save_progress(p, {
        "processed_hashes": ["a", "b", "c"],
        "gemini_calls_total": 5,
        "pairs_total": 25,
        "last_run_at": 12345,
    })
    data = self_distill_notes._load_progress(p)
    assert data["processed_hashes"] == ["a", "b", "c"]
    assert data["gemini_calls_total"] == 5
    assert data["pairs_total"] == 25


def test_chunk_hash_stable():
    h1 = self_distill_notes._chunk_hash("hello world")
    h2 = self_distill_notes._chunk_hash("hello world")
    h3 = self_distill_notes._chunk_hash("hello world!")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 16


# ═══════════════════════════════════════════════════════════════════════════
# estimate_dry_run / CLI
# ═══════════════════════════════════════════════════════════════════════════
def test_estimate_dry_run_includes_quota_estimate(tmp_path):
    note_dir = tmp_path / "notes"
    note_dir.mkdir()
    body = ("## H\n\n" + "這是測試內容寫得長至少超過一百字才會通過過濾門檻喔。" * 5
            + "\n\n") * 3
    _mk_md(note_dir / "a.md", body)

    s = self_distill_notes.estimate_dry_run(
        [note_dir], batch_size=5, max_gemini_calls=10,
    )
    assert s["total_files"] == 1
    assert s["total_chunks"] >= 1
    assert s["batch_size"] == 5
    assert s["max_gemini_calls_per_day"] == 10
    assert s["pairs_per_day"] == 50
    assert s["estimated_pairs_total"] == s["total_chunks"] * 5
    # days_needed = ceil(chunks / max_gemini_calls)
    assert s["days_needed"] >= 1


def test_cli_dry_run_prints_quota_estimate(monkeypatch, tmp_path, capsys):
    note_dir = tmp_path / "notes"
    note_dir.mkdir()
    body = ("## H\n\n" + "測試內容寫得長至少超過一百字才會被當成有效 chunk 通過過濾門檻。" * 5)
    _mk_md(note_dir / "a.md", body)
    monkeypatch.setattr(self_distill_notes, "NOTE_ROOTS", [note_dir])
    rc = self_distill_notes.main(["--dry-run", "--batch-size", "5",
                                  "--max-gemini-calls", "10"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "找到" in out
    assert "切出" in out
    assert "calls" in out and "對 pair" in out
    assert "天" in out  # 預計 X 天


def test_cli_default_max_gemini_calls_is_10():
    """確保 default 是 10（user 留另外 10 給主對話）。"""
    assert self_distill_notes.DEFAULT_MAX_GEMINI_CALLS == 10


# ═══════════════════════════════════════════════════════════════════════════
# dataset_builder integration: notes_distilled merges + correct priority
# ═══════════════════════════════════════════════════════════════════════════
def _make_minimal_db(tmp_path: Path) -> Path:
    import sqlite3
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE context (group_id TEXT NOT NULL, seq INTEGER NOT NULL, "
        "role TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY (group_id, seq))"
    )
    con.execute(
        "CREATE TABLE persona_notes (group_id TEXT NOT NULL, "
        "note_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
        "scenario TEXT NOT NULL, content TEXT NOT NULL, "
        "created_at INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'rule_violation')"
    )
    con.execute(
        "CREATE TABLE raw_messages (group_id TEXT NOT NULL, message_id TEXT NOT NULL, "
        "user_id TEXT, text TEXT NOT NULL, created_at INTEGER NOT NULL, "
        "PRIMARY KEY (group_id, message_id))"
    )
    con.commit()
    con.close()
    return db


def test_dataset_builder_merges_notes_distilled(tmp_path):
    db = _make_minimal_db(tmp_path)
    notes = tmp_path / "notes_distilled.jsonl"
    notes.write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "Andrew 的筆記產生的 Q"},
                {"role": "assistant", "content": "Andrew 的筆記產生的 A"},
            ],
            "metadata": {"source": "notes_distilled"},
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    stats = dataset_builder.build_dataset(
        db_path=db,
        out_dir=tmp_path / "out",
        distilled_path=tmp_path / "no_distilled.jsonl",
        notes_distilled_path=notes,
        sft_path=tmp_path / "no_sft.jsonl",
        include_exports=False,
    )
    assert stats["total"] == 1
    assert stats["by_source"].get("notes_distilled") == 1


def test_dataset_builder_priority_notes_over_distilled(tmp_path):
    db = _make_minimal_db(tmp_path)

    pair = {
        "messages": [
            {"role": "user", "content": "重複出現的 question 內容"},
            {"role": "assistant", "content": "重複出現的 answer 內容"},
        ],
    }

    notes = tmp_path / "notes.jsonl"
    notes.write_text(
        json.dumps({**pair, "metadata": {"source": "notes_distilled"}},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    distilled = tmp_path / "distilled.jsonl"
    distilled.write_text(
        json.dumps({**pair, "metadata": {"source": "distilled"}},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    stats = dataset_builder.build_dataset(
        db_path=db,
        out_dir=tmp_path / "out",
        distilled_path=distilled,
        notes_distilled_path=notes,
        sft_path=tmp_path / "no_sft.jsonl",
        include_exports=False,
    )
    assert stats["total"] == 1
    assert stats["by_source"].get("notes_distilled") == 1
    assert "distilled" not in stats["by_source"] or stats["by_source"].get("distilled", 0) == 0


def test_dataset_builder_no_notes_flag(tmp_path):
    db = _make_minimal_db(tmp_path)
    notes = tmp_path / "notes.jsonl"
    notes.write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "should be ignored 不該出現的問題"},
                {"role": "assistant", "content": "should be ignored 不該出現的回覆"},
            ],
            "metadata": {"source": "notes_distilled"},
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    stats = dataset_builder.build_dataset(
        db_path=db,
        out_dir=tmp_path / "out",
        distilled_path=tmp_path / "nope.jsonl",
        notes_distilled_path=notes,
        include_notes=False,
        sft_path=tmp_path / "no_sft.jsonl",
        include_exports=False,
    )
    assert stats["total"] == 0
