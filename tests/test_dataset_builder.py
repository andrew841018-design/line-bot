"""Tests for finetune/extract_line_history.py + finetune/dataset_builder.py.

Pure mock SQLite — does NOT touch real line_bot.db.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

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

from finetune import dataset_builder, extract_line_history  # noqa: E402


# ─── fixtures ──────────────────────────────────────────────────────────────
def _make_db(rows_context: list[tuple], rows_persona: list[tuple] | None = None,
             rows_raw: list[tuple] | None = None) -> Path:
    """Create a throwaway sqlite db populated with the given rows."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = Path(tmp.name)
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE context (group_id TEXT NOT NULL, seq INTEGER NOT NULL, "
        "role TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY (group_id, seq))"
    )
    con.executemany("INSERT INTO context VALUES (?,?,?,?)", rows_context)
    con.execute(
        "CREATE TABLE persona_notes (group_id TEXT NOT NULL, "
        "note_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
        "scenario TEXT NOT NULL, content TEXT NOT NULL, "
        "created_at INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'rule_violation')"
    )
    if rows_persona:
        con.executemany(
            "INSERT INTO persona_notes (group_id, kind, scenario, content, "
            "created_at, source) VALUES (?,?,?,?,?,?)",
            rows_persona,
        )
    con.execute(
        "CREATE TABLE raw_messages (group_id TEXT NOT NULL, message_id TEXT NOT NULL, "
        "user_id TEXT, text TEXT NOT NULL, created_at INTEGER NOT NULL, "
        "PRIMARY KEY (group_id, message_id))"
    )
    if rows_raw:
        con.executemany("INSERT INTO raw_messages VALUES (?,?,?,?,?)", rows_raw)
    con.commit()
    con.close()
    return path


@pytest.fixture
def cleanup_paths():
    paths: list[Path] = []
    yield paths
    for p in paths:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# extract_all_pairs
# ═══════════════════════════════════════════════════════════════════════════
def test_extract_all_pairs_basic(cleanup_paths):
    rows = [
        ("g1", 1, "user", "你好啊咪寶"),
        ("g1", 2, "bot", "嗨今天天氣不錯欸要不要出去走走呢"),
        ("g1", 3, "user", "中午吃什麼好"),
        ("g1", 4, "bot", "附近那家麵店看起來不錯欸要不要試試看"),
    ]
    db = _make_db(rows)
    cleanup_paths.append(db)
    pairs = extract_line_history.extract_all_pairs(db)
    assert len(pairs) == 2
    assert pairs[0]["prompt"] == "你好啊咪寶"
    assert pairs[0]["source"] == "organic"
    assert "pair_hash" in pairs[0]


def test_extract_skips_non_adjacent_pairs(cleanup_paths):
    rows = [
        ("g1", 1, "user", "問題一這是測試"),
        ("g1", 3, "bot", "回答有 seq gap 不該被抓出來"),  # seq 2 缺 → 非連續
        ("g1", 4, "user", "問題二這也是測試"),
        ("g1", 5, "bot", "正常回答這是一段比較長的測試內容"),
    ]
    db = _make_db(rows)
    cleanup_paths.append(db)
    pairs = extract_line_history.extract_all_pairs(db)
    assert len(pairs) == 1
    assert pairs[0]["prompt"] == "問題二這也是測試"


def test_extract_skips_short_user_msg(cleanup_paths):
    rows = [
        ("g1", 1, "user", "嗨"),  # 只有 2 字 → MIN_USER_LEN=3 跳掉
        ("g1", 2, "bot", "你好啊我是咪寶請問需要什麼協助嗎"),
        ("g1", 3, "user", "今天天氣如何呢"),
        ("g1", 4, "bot", "今天台北看起來晴朗大概 25 度欸"),
    ]
    db = _make_db(rows)
    cleanup_paths.append(db)
    pairs = extract_line_history.extract_all_pairs(db)
    assert len(pairs) == 1
    assert pairs[0]["prompt"] == "今天天氣如何呢"


def test_extract_skips_pure_emoji(cleanup_paths):
    rows = [
        ("g1", 1, "user", "🎉🎊🎈"),  # 純 emoji
        ("g1", 2, "bot", "好像在慶祝什麼欸發生什麼好事了嗎"),
        ("g1", 3, "user", "今天心情很好欸"),
        ("g1", 4, "bot", "什麼事讓你這麼開心呢可以分享一下嗎"),
    ]
    db = _make_db(rows)
    cleanup_paths.append(db)
    pairs = extract_line_history.extract_all_pairs(db)
    assert len(pairs) == 1


def test_extract_skips_pure_url_user(cleanup_paths):
    rows = [
        ("g1", 1, "user", "https://example.com/abc"),  # 純 URL
        ("g1", 2, "bot", "這個網站看起來蠻有趣的對吧要看看嗎"),
        ("g1", 3, "user", "推薦一些好看的小說吧"),
        ("g1", 4, "bot", "最近滿值得看的有那本原子習慣的續作欸"),
    ]
    db = _make_db(rows)
    cleanup_paths.append(db)
    pairs = extract_line_history.extract_all_pairs(db)
    assert len(pairs) == 1
    assert pairs[0]["prompt"] == "推薦一些好看的小說吧"


def test_extract_filters_violates_quality(cleanup_paths, monkeypatch):
    """Mock _violates_quality 讓某條 bot reply 違規 → 該 pair 被丟掉。"""
    def fake_violates(reply, user_text=""):
        return ("FAKE_BAD_REPLY" in reply, "test reason")

    monkeypatch.setattr(
        "gemini_client._violates_quality", fake_violates, raising=False
    )

    rows = [
        ("g1", 1, "user", "請問問題甲"),
        ("g1", 2, "bot", "FAKE_BAD_REPLY 這條應該被 violates_quality 擋掉"),
        ("g1", 3, "user", "請問問題乙"),
        ("g1", 4, "bot", "正常合格的回答看起來蠻不錯的"),
    ]
    db = _make_db(rows)
    cleanup_paths.append(db)
    pairs = extract_line_history.extract_all_pairs(db)
    assert len(pairs) == 1
    assert "FAKE_BAD_REPLY" not in pairs[0]["completion"]


def test_extract_dedup_sha256(cleanup_paths):
    rows = [
        # 同樣 (user, bot) 出現兩次（不同 seq / group）→ 應只留一筆
        ("g1", 1, "user", "重複問題的測試內容"),
        ("g1", 2, "bot", "重複的標準測試回覆內容"),
        ("g2", 1, "user", "重複問題的測試內容"),
        ("g2", 2, "bot", "重複的標準測試回覆內容"),
    ]
    db = _make_db(rows)
    cleanup_paths.append(db)
    pairs = extract_line_history.extract_all_pairs(db)
    assert len(pairs) == 1


def test_group_id_is_hashed(cleanup_paths):
    raw_gid = "C83c5609ada4df93fa7f3239c24685133"
    rows = [
        (raw_gid, 1, "user", "測試問題長度夠的內容"),
        (raw_gid, 2, "bot", "測試回答這也是合理長度的內容"),
    ]
    db = _make_db(rows)
    cleanup_paths.append(db)
    pairs = extract_line_history.extract_all_pairs(db)
    assert len(pairs) == 1
    assert pairs[0]["group_id"] != raw_gid
    assert len(pairs[0]["group_id"]) == 16  # truncated hex


# ═══════════════════════════════════════════════════════════════════════════
# extract_with_corrections + augment_with_negatives
# ═══════════════════════════════════════════════════════════════════════════
def test_extract_with_corrections(cleanup_paths):
    persona_rows = [
        (
            "g1", "correction",
            "投資建議",
            "user 訊息：請問現在台積電該買嗎？\n"
            "bot 原回覆：我覺得可以買\n"
            "user 糾正：應該說明各家投行目標價並列出 https://example.com/a 跟 https://example.com/b 兩條來源以及自己看法",
            1700000000, "organic",
        ),
        # 非 organic source 應排除
        (
            "g1", "correction",
            "規則 0 違規",
            "違規原因：echo opener\n違規回覆（前 200 字）：咪寶看到...",
            1700000000, "rule_violation",
        ),
    ]
    db = _make_db([], rows_persona=persona_rows)
    cleanup_paths.append(db)

    pairs = extract_line_history.extract_with_corrections(db)
    assert len(pairs) == 1
    assert pairs[0]["source"] == "correction"
    assert pairs[0]["priority"] == "high"
    assert "現在台積電" in pairs[0]["prompt"]
    assert "example.com" in pairs[0]["completion"]


def test_augment_with_negatives(cleanup_paths):
    persona_rows = [
        (
            "g1", "correction",
            "scenario A",
            "user 訊息：問題一這是長一點的問題\n"
            "bot 原回覆：這是被糾正的舊版本回答太短\n"
            "user 糾正：這是糾正後比較完整的版本應該要這樣回覆 https://a.test https://b.test 兩條",
            1700000000, "organic",
        ),
    ]
    db = _make_db([], rows_persona=persona_rows)
    cleanup_paths.append(db)

    dpo = extract_line_history.augment_with_negatives(db)
    assert len(dpo) == 1
    rec = dpo[0]
    assert rec["source"] == "dpo_correction"
    assert "問題一" in rec["prompt"]
    assert "糾正後" in rec["chosen"]
    assert "舊版本" in rec["rejected"]


# ═══════════════════════════════════════════════════════════════════════════
# dataset_builder
# ═══════════════════════════════════════════════════════════════════════════
def test_build_dataset_full_pipeline(cleanup_paths, tmp_path):
    # 每個 group 兩 pair（seq 1-4），共 10 group → 20 pairs
    interleaved: list[tuple] = []
    for g in range(10):
        gid = f"g{g}"
        interleaved.extend([
            (gid, 1, "user", f"群 {g} 的第一個測試問題內容"),
            (gid, 2, "bot", f"群 {g} 的第一個測試回答內容也很完整"),
            (gid, 3, "user", f"群 {g} 的第二個測試問題內容"),
            (gid, 4, "bot", f"群 {g} 的第二個測試回答內容也很完整"),
        ])
    db = _make_db(interleaved)
    cleanup_paths.append(db)

    out_dir = tmp_path / "out"
    distilled = tmp_path / "distilled.jsonl"
    distilled.write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "蒸餾用測試 user 訊息"},
                {"role": "assistant", "content": "蒸餾的 ideal 測試回應內容"},
            ],
            "metadata": {"source": "distilled"},
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    stats = dataset_builder.build_dataset(
        db_path=db, out_dir=out_dir, distilled_path=distilled,
        include_exports=False,
        notes_distilled_path=tmp_path / "no_notes.jsonl",
        sft_path=tmp_path / "no_sft.jsonl",
    )

    # 80/10/10 分配 — 21 筆（20 organic + 1 distilled）
    assert stats["total"] == 21
    assert stats["train"] + stats["val"] + stats["test"] == 21
    # train 應該佔大宗
    assert stats["train"] >= stats["val"]
    assert stats["train"] >= stats["test"]

    # 檔案實際寫出
    assert (out_dir / "train.jsonl").exists()
    assert (out_dir / "val.jsonl").exists()
    assert (out_dir / "test.jsonl").exists()

    # 內容是合法 jsonl，每行 messages 結構
    for fname in ("train.jsonl", "val.jsonl", "test.jsonl"):
        for line in (out_dir / fname).read_text(encoding="utf-8").splitlines():
            obj = json.loads(line)
            assert "messages" in obj
            assert obj["messages"][0]["role"] == "user"
            assert obj["messages"][1]["role"] == "assistant"


def test_build_dataset_dedup_across_sources(cleanup_paths, tmp_path):
    """同一 (prompt, completion) 若同時出現在 context 跟 distilled → 只留一筆。"""
    rows_context = [
        ("g1", 1, "user", "重複的測試內容問題啦"),
        ("g1", 2, "bot", "重複的測試回答也很完整啦"),
    ]
    db = _make_db(rows_context)
    cleanup_paths.append(db)

    distilled = tmp_path / "distilled.jsonl"
    distilled.write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "重複的測試內容問題啦"},
                {"role": "assistant", "content": "重複的測試回答也很完整啦"},
            ],
            "metadata": {"source": "distilled"},
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    stats = dataset_builder.build_dataset(
        db_path=db, out_dir=tmp_path / "out", distilled_path=distilled,
        include_exports=False,
        notes_distilled_path=tmp_path / "no_notes.jsonl",
        sft_path=tmp_path / "no_sft.jsonl",
    )
    assert stats["total"] == 1


def test_build_dataset_includes_line_export(cleanup_paths, tmp_path):
    """[LINE]*.txt 從 export_search_dirs 自動撈進來，標記 source='line_export'。"""
    rows_context = [
        ("g1", 1, "user", "資料庫裡的測試問題啦"),
        ("g1", 2, "bot", "資料庫裡的測試回覆夠長啦"),
    ]
    db = _make_db(rows_context)
    cleanup_paths.append(db)

    # Fake export 寫到 tmp_path 並指定為唯一搜尋路徑
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    (export_dir / "[LINE]家人群.txt").write_text(
        "[LINE] 聊天紀錄：家人群\n"
        "儲存日期：2026/05/08 12:34\n"
        "\n"
        "2025/12/01（日）\n"
        "10:00\tAndrew\t這是來自 export 檔的測試訊息夠長吧\n"
        "10:01\t咪寶\t收到 export 檔的訊息了喔很開心\n",
        encoding="utf-8",
    )

    out_dir = tmp_path / "out"
    stats = dataset_builder.build_dataset(
        db_path=db,
        out_dir=out_dir,
        distilled_path=tmp_path / "no_distilled.jsonl",
        export_search_dirs=[export_dir],
        notes_distilled_path=tmp_path / "no_notes.jsonl",
        sft_path=tmp_path / "no_sft.jsonl",
    )

    # context: 1 + export: 1 = 2
    assert stats["total"] == 2
    assert stats["by_source"].get("line_export") == 1
    assert stats["by_source"].get("organic") == 1

    # 確認 line_export 的 group_name 已被 hash（meta.group_id 16 hex）
    all_lines: list[str] = []
    for fn in ("train.jsonl", "val.jsonl", "test.jsonl"):
        all_lines.extend((out_dir / fn).read_text(encoding="utf-8").splitlines())
    found = False
    for line in all_lines:
        obj = json.loads(line)
        if obj["metadata"].get("source") == "line_export":
            found = True
            assert "家人群" not in line  # 群名不應 leak
            assert len(obj["metadata"].get("group_id", "")) == 16
    assert found


def test_build_dataset_skip_exports_flag(cleanup_paths, tmp_path):
    """include_exports=False 時不掃 [LINE]*.txt。"""
    rows_context = [
        ("g1", 1, "user", "資料庫裡的測試問題啦"),
        ("g1", 2, "bot", "資料庫裡的測試回覆夠長啦"),
    ]
    db = _make_db(rows_context)
    cleanup_paths.append(db)

    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    (export_dir / "[LINE]X.txt").write_text(
        "[LINE] 聊天紀錄：X\n"
        "儲存日期：2026/05/08 12:34\n"
        "\n"
        "2025/12/01（日）\n"
        "10:00\tAndrew\t這是被忽略的訊息應該不會被收進來\n"
        "10:01\t咪寶\t這條也應該不會被收進去資料集裡\n",
        encoding="utf-8",
    )

    stats = dataset_builder.build_dataset(
        db_path=db,
        out_dir=tmp_path / "out",
        distilled_path=tmp_path / "no_distilled.jsonl",
        include_exports=False,
        export_search_dirs=[export_dir],
        notes_distilled_path=tmp_path / "no_notes.jsonl",
        sft_path=tmp_path / "no_sft.jsonl",
    )
    assert stats["total"] == 1
    assert "line_export" not in stats["by_source"]


def test_compute_stats(cleanup_paths):
    rows = [
        ("g1", 1, "user", "A 測試問題內容夠長"),
        ("g1", 2, "bot", "A 測試回答內容也夠長"),
        ("g1", 3, "user", "B 測試問題啦"),
        ("g1", 4, "bot", "B 測試回答內容也很 OK 啦"),
    ]
    raw_rows = [
        ("g1", "m1", "u1", "msg1", 1700000000),
        ("g1", "m2", "u2", "msg2", 1700099999),
    ]
    db = _make_db(rows, rows_raw=raw_rows)
    cleanup_paths.append(db)
    s = dataset_builder.compute_stats(db)
    assert s["organic_pairs"] == 2
    assert s["groups"] == 1
    assert s["ts_min"] > 0


def test_cli_stats_runs(cleanup_paths, capsys):
    rows = [
        ("g1", 1, "user", "測試問題啊問題啦"),
        ("g1", 2, "bot", "測試回答內容夠長吧啦"),
    ]
    db = _make_db(rows)
    cleanup_paths.append(db)
    rc = dataset_builder.main(["--stats", "--db", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "pairs" in out
    assert "groups" in out
