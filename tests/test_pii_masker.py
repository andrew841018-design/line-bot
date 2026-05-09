"""Tests for finetune/pii_masker.py + finetune/pii_storage.py + the
build_dataset PII integration. 100% offline / no jieba network.
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

from finetune import dataset_builder, pii_masker, pii_storage  # noqa: E402


# ─── 1. Per-category mask coverage ─────────────────────────────────────────
def test_mask_chinese_name_relationship_form():
    text = "黃媽媽今天去市場了"
    masked, mapping = pii_masker.mask(text)
    assert "黃媽媽" not in masked
    assert "[NAME_1]" in masked
    assert mapping["[NAME_1]"] == "黃媽媽"


def test_mask_chinese_full_name_jieba():
    # jieba.posseg should tag 「王小明」as nr
    text = "王小明今天請客"
    masked, mapping = pii_masker.mask(text)
    assert "王小明" not in masked
    assert any(k.startswith("[NAME_") for k in mapping)


def test_mask_amount_with_currency_suffix():
    text = "他匯了 50萬元 過來，或 1000 美元"
    masked, mapping = pii_masker.mask(text)
    assert "50萬元" not in masked
    assert "1000 美元" not in masked or "1000美元" not in masked
    counts = pii_masker.count_pii(mapping)
    assert counts.get("AMOUNT", 0) >= 2


def test_mask_amount_simple():
    text = "成本 5000元 賣 8000元"
    masked, mapping = pii_masker.mask(text)
    assert "5000元" not in masked
    assert "8000元" not in masked


def test_mask_address_with_city():
    text = "地址在 台北市信義區信義路五段100號5樓"
    masked, mapping = pii_masker.mask(text)
    assert "台北市" not in masked
    assert "信義路" not in masked
    assert any(k.startswith("[ADDRESS_") for k in mapping)


def test_mask_address_local_form():
    text = "我家在中央路100號"
    masked, mapping = pii_masker.mask(text)
    assert "中央路100號" not in masked
    assert any(k.startswith("[ADDRESS_") for k in mapping)


def test_mask_phone_mobile():
    text = "我手機 0912-345-678 可以打"
    masked, mapping = pii_masker.mask(text)
    assert "0912-345-678" not in masked
    assert any(k.startswith("[PHONE_") for k in mapping)


def test_mask_phone_landline():
    text = "公司 02-2345-6789 請打這支"
    masked, mapping = pii_masker.mask(text)
    assert "02-2345-6789" not in masked


def test_mask_credit_card_dashed():
    text = "卡號 4321-1234-5678-9012 過期了"
    masked, mapping = pii_masker.mask(text)
    assert "4321-1234-5678-9012" not in masked
    assert any(k.startswith("[ACCOUNT_") for k in mapping)


def test_mask_bank_account_10digit():
    text = "合作金庫帳號1234567890"
    masked, mapping = pii_masker.mask(text)
    assert "1234567890" not in masked
    assert any(k.startswith("[ACCOUNT_") for k in mapping)


def test_mask_taiwan_id():
    text = "身分證 A123456789 別亂貼"
    masked, mapping = pii_masker.mask(text)
    assert "A123456789" not in masked
    assert any(k.startswith("[ID_") for k in mapping)


def test_mask_email():
    text = "聯絡 andrew@example.com 即可"
    masked, mapping = pii_masker.mask(text)
    assert "andrew@example.com" not in masked
    assert any(k.startswith("[ID_") for k in mapping)


def test_mask_url():
    text = "網址 https://malicious.example.com/abc 不要點"
    masked, mapping = pii_masker.mask(text)
    assert "https://malicious.example.com/abc" not in masked


def test_mask_org_with_suffix():
    text = "富邦金控股份有限公司 公布財報"
    masked, mapping = pii_masker.mask(text)
    assert "富邦金控股份有限公司" not in masked
    assert any(k.startswith("[ORG_") for k in mapping)


# ─── 2. Preservation rules ──────────────────────────────────────────────────
def test_preserve_pure_relationship_words():
    text = "我老婆說媽媽今天回家做飯，朋友也來"
    masked, _ = pii_masker.mask(text)
    # 老婆 / 媽媽 / 朋友 都應保留 (沒姓氏前綴)
    assert "老婆" in masked
    assert "媽媽" in masked
    assert "朋友" in masked


def test_preserve_percentage():
    text = "利率現在是 3.5% 真的不錯"
    masked, mapping = pii_masker.mask(text)
    # 百分比不應被 mask 為 amount
    assert "3.5%" in masked
    assert not any(k.startswith("[AMOUNT_") for k in mapping)


def test_preserve_date_yyyy_mm_dd():
    text = "2024/07/10 開盤大跌"
    masked, mapping = pii_masker.mask(text)
    assert "2024/07/10" in masked
    assert not any(k.startswith("[AMOUNT_") for k in mapping)


# ─── 3. Same-name-same-placeholder ──────────────────────────────────────────
def test_same_name_same_placeholder_within_text():
    text = "黃媽媽說好，黃媽媽又說壞"
    masked, mapping = pii_masker.mask(text)
    # 兩次「黃媽媽」應 mask 成同一個 [NAME_X]
    assert masked.count("[NAME_1]") == 2
    assert mapping["[NAME_1]"] == "黃媽媽"


def test_same_name_consistent_across_calls():
    """Reusing mapping in 2nd call → 同 raw 同 placeholder。"""
    a, mp = pii_masker.mask("黃媽媽今天好開心")
    b, mp = pii_masker.mask("剛剛黃媽媽走過來", mapping=mp)
    assert "[NAME_1]" in a
    assert "[NAME_1]" in b
    assert mp["[NAME_1]"] == "黃媽媽"


# ─── 4. round-trip ──────────────────────────────────────────────────────────
def test_unmask_roundtrip():
    text = "黃媽媽匯了 5000元 給 王小明"
    masked, mapping = pii_masker.mask(text)
    # may not be byte-for-byte identical (e.g. compound boundaries) but
    # all placeholders should restore. Check all originals appear back.
    restored = pii_masker.unmask(masked, mapping)
    assert "黃媽媽" in restored
    assert "5000元" in restored


# ─── 5. pii_storage cache ───────────────────────────────────────────────────
def test_storage_save_load_roundtrip(tmp_path):
    db = tmp_path / "pii.db"
    h = pii_storage.record_hash("hello", "world")
    pii_storage.save_mapping(db, h, {"[NAME_1]": "黃媽媽"})
    out = pii_storage.load_mapping(db, h)
    assert out == {"[NAME_1]": "黃媽媽"}


def test_storage_cache_size(tmp_path):
    db = tmp_path / "pii.db"
    assert pii_storage.cache_size(db) == 0
    pii_storage.save_mapping(db, "h1", {"[NAME_1]": "x"})
    pii_storage.save_mapping(db, "h2", {"[NAME_1]": "y"})
    assert pii_storage.cache_size(db) == 2


def test_mask_with_cache_idempotent(tmp_path):
    db = tmp_path / "pii.db"
    p = "黃媽媽匯款給王小明"
    c = "好的我幫黃媽媽轉帳"
    mp1, mc1, m1 = pii_storage.mask_with_cache(p, c, db_path=db)
    mp2, mc2, m2 = pii_storage.mask_with_cache(p, c, db_path=db)
    assert mp1 == mp2
    assert mc1 == mc2
    assert m1 == m2
    # cache populated exactly one entry
    assert pii_storage.cache_size(db) == 1


def test_mask_with_cache_shares_mapping_across_prompt_completion(tmp_path):
    db = tmp_path / "pii.db"
    p = "黃媽媽今天匯款"
    c = "我幫黃媽媽轉了"
    mp, mc, m = pii_storage.mask_with_cache(p, c, db_path=db)
    # Same name in both prompt + completion → same placeholder
    assert "[NAME_1]" in mp
    assert "[NAME_1]" in mc
    assert m["[NAME_1]"] == "黃媽媽"


# ─── 6. dataset_builder integration ─────────────────────────────────────────
def _make_db(rows_context: list[tuple]) -> Path:
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
    con.execute(
        "CREATE TABLE raw_messages (group_id TEXT NOT NULL, message_id TEXT NOT NULL, "
        "user_id TEXT, text TEXT NOT NULL, created_at INTEGER NOT NULL, "
        "PRIMARY KEY (group_id, message_id))"
    )
    con.commit()
    con.close()
    return path


def test_build_dataset_masks_pii_by_default(tmp_path):
    """built train.jsonl should NOT contain raw names / amounts."""
    rows = [
        ("g1", 1, "user", "黃媽媽問我家附近富邦金控股份有限公司怎麼走"),
        ("g1", 2, "bot", "黃媽媽要找的話可以從台北市信義區信義路五段100號出發"),
    ]
    db = _make_db(rows)
    pii_db = tmp_path / "pii.db"
    out = tmp_path / "out"
    stats = dataset_builder.build_dataset(
        db_path=db,
        out_dir=out,
        distilled_path=tmp_path / "no_distilled.jsonl",
        notes_distilled_path=tmp_path / "no_notes.jsonl",
        sft_path=tmp_path / "no_sft.jsonl",
        include_exports=False,
        pii_db_path=pii_db,
    )
    assert stats["pii_masked"] is True
    assert stats["pii_counts"], "expected non-empty pii_counts"

    # check actual files don't contain leaked PII
    all_text = ""
    for fn in ("train.jsonl", "val.jsonl", "test.jsonl"):
        all_text += (out / fn).read_text(encoding="utf-8")
    assert "黃媽媽" not in all_text
    assert "富邦金控" not in all_text
    assert "台北市信義區" not in all_text
    # pii_masked metadata flag
    for fn in ("train.jsonl", "val.jsonl", "test.jsonl"):
        for line in (out / fn).read_text(encoding="utf-8").splitlines():
            obj = json.loads(line)
            assert obj["metadata"].get("pii_masked") is True

    # mapping cached in pii_db
    assert pii_db.exists()
    assert pii_storage.cache_size(pii_db) >= 1


def test_build_dataset_no_mask_flag_keeps_raw(tmp_path):
    rows = [
        ("g1", 1, "user", "黃媽媽問我富邦金控好嗎"),
        ("g1", 2, "bot", "黃媽媽我覺得富邦金控目前 PB 不算便宜"),
    ]
    db = _make_db(rows)
    out = tmp_path / "out"
    stats = dataset_builder.build_dataset(
        db_path=db,
        out_dir=out,
        distilled_path=tmp_path / "no_distilled.jsonl",
        notes_distilled_path=tmp_path / "no_notes.jsonl",
        sft_path=tmp_path / "no_sft.jsonl",
        include_exports=False,
        mask_pii=False,
        pii_db_path=tmp_path / "should_not_be_created.db",
    )
    assert stats["pii_masked"] is False
    all_text = ""
    for fn in ("train.jsonl", "val.jsonl", "test.jsonl"):
        all_text += (out / fn).read_text(encoding="utf-8")
    assert "黃媽媽" in all_text
