"""Feature D tests — anniversary extractor + reminder (Andrew 2026-05-25)."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")


@pytest.fixture
def temp_anniv_db(monkeypatch):
    import anniversary
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    monkeypatch.setattr(anniversary, "_DB_PATH", tmp_path)
    anniversary.init_db()
    yield tmp_path
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass


def test_init_db_creates_tables(temp_anniv_db):
    import sqlite3
    with sqlite3.connect(temp_anniv_db) as c:
        tables = [
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
    assert "anniversaries" in tables
    assert "anniversary_reminders_sent" in tables


def test_insert_and_get_anniversary(temp_anniv_db):
    import anniversary
    aid = anniversary.insert_anniversary(
        group_id="GRP", person="媽媽", anniversary_type="birthday",
        month=6, day=4,
    )
    assert aid
    anns = anniversary.get_anniversaries_on("GRP", 6, 4)
    assert len(anns) == 1
    assert anns[0]["person"] == "媽媽"


def test_insert_dedup(temp_anniv_db):
    """同 group+person+type+month+day 第二次 insert → 回空 string."""
    import anniversary
    a1 = anniversary.insert_anniversary("GRP", "媽媽", "birthday", 6, 4)
    a2 = anniversary.insert_anniversary("GRP", "媽媽", "birthday", 6, 4)
    assert a1
    assert not a2


def test_insert_invalid_month_or_day(temp_anniv_db):
    import anniversary
    assert not anniversary.insert_anniversary("GRP", "X", "birthday", 13, 1)
    assert not anniversary.insert_anniversary("GRP", "X", "birthday", 6, 32)
    assert not anniversary.insert_anniversary("GRP", "X", "birthday", 0, 1)


def test_invalid_type_normalized_to_other(temp_anniv_db):
    """anniversary_type 不在 whitelist → 變 'other'."""
    import anniversary
    aid = anniversary.insert_anniversary(
        "GRP", "媽媽", "bogus_type", 6, 4,
    )
    assert aid
    anns = anniversary.get_anniversaries_on("GRP", 6, 4)
    assert anns[0]["anniversary_type"] == "other"


def test_mark_and_check_reminded(temp_anniv_db):
    import anniversary
    aid = anniversary.insert_anniversary("GRP", "媽媽", "birthday", 6, 4)
    assert not anniversary.already_reminded(aid, 2026, 0)
    anniversary.mark_reminded(aid, 2026, 0)
    assert anniversary.already_reminded(aid, 2026, 0)
    # 不同 year / offset 還沒 mark
    assert not anniversary.already_reminded(aid, 2027, 0)
    assert not anniversary.already_reminded(aid, 2026, 1)


def test_push_today_reminders_dedup(temp_anniv_db):
    """跑兩次同年同天 → 第二次 _push call count = 0."""
    import anniversary
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    anniversary.insert_anniversary(
        "GRP_TEST", "媽媽", "birthday", today.month, today.day,
    )

    push_count = {"n": 0}

    def fake_push(text):
        push_count["n"] += 1
        return True

    with patch.object(anniversary, "GROUP_ID", "GRP_TEST"), \
         patch.object(anniversary, "_push", side_effect=fake_push):
        n1 = anniversary.push_today_reminders("GRP_TEST")
        n2 = anniversary.push_today_reminders("GRP_TEST")

    assert n1 == 1, f"first run should push once (got {n1})"
    assert n2 == 0, f"second run dedup should push 0 (got {n2})"


def test_main_reminder_requires_group_id():
    import anniversary
    with patch.object(anniversary, "GROUP_ID", ""):
        assert anniversary.main_reminder() == 1


def test_main_extractor_requires_group_id():
    import anniversary
    with patch.object(anniversary, "GROUP_ID", ""):
        assert anniversary.main_extractor() == 1


def test_extract_from_text_empty():
    import anniversary
    assert anniversary.extract_from_text("") == []
    assert anniversary.extract_from_text("   ") == []
