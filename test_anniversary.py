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

    def fake_push(group_id, text, targets=(), *, retry_key=None):
        push_count["n"] += 1
        return True

    # 2026-05-27 multi-group: GROUP_ID module-global 已移除；2026-11-12 `_push` 多了 targets 與 retry_key
    with patch.object(anniversary, "_get_target_group_ids", return_value=["GRP_TEST"]), \
         patch.object(anniversary, "_push", side_effect=fake_push):
        n1 = anniversary.push_today_reminders("GRP_TEST")
        n2 = anniversary.push_today_reminders("GRP_TEST")

    assert n1 == 1, f"first run should push once (got {n1})"
    assert n2 == 0, f"second run dedup should push 0 (got {n2})"


def test_main_reminder_requires_group_id():
    import anniversary
    with patch.object(anniversary, "_get_target_group_ids", return_value=[]):
        assert anniversary.main_reminder() == 1


def test_main_extractor_requires_group_id():
    import anniversary
    with patch.object(anniversary, "_get_target_group_ids", return_value=[]):
        assert anniversary.main_extractor() == 1


def test_extract_from_text_empty():
    import anniversary
    assert anniversary.extract_from_text("") == []
    assert anniversary.extract_from_text("   ") == []


# ── 2026-11-12 「提到人就真 @」: 紀念日 @ 全家（Andrew 選的），只在家族主群 ─────────────────────────
# Synthetic ids and names only; the push goes to conftest's recorder, the token is a stand-in.


def _today_row(anniversary, group_id="GRP_FAMILY"):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    return anniversary.insert_anniversary(group_id, "成員甲", "birthday", today.month, today.day)


@pytest.fixture
def pushed(monkeypatch):
    import line_push_client

    monkeypatch.setattr(line_push_client, "line_access_token", lambda fallback_token=None: "token")
    monkeypatch.setenv("LINE_BOT_PUSH_MENTIONS", "")
    return line_push_client.push_messages.calls


def test_the_family_group_gets_the_anniversary_with_an_all_mention(temp_anniv_db, monkeypatch, pushed):
    import uuid
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import anniversary

    monkeypatch.setattr(anniversary.settings, "family_group_id", "GRP_FAMILY")
    aid = _today_row(anniversary)
    assert anniversary.push_today_reminders("GRP_FAMILY") == 1
    [call] = pushed
    message = call["messages"][0]
    assert message["type"] == "textV2" and message["text"].startswith("{all}\n🔔 今天是 成員甲 的生日")
    assert message["substitution"] == {"all": {"type": "mention", "mentionee": {"type": "all"}}}
    year = datetime.now(ZoneInfo("Asia/Taipei")).year
    assert call["retry_key"] == str(uuid.uuid5(uuid.NAMESPACE_URL, f"anniversary:{aid}:{year}:0"))
    assert anniversary.already_reminded(aid, year, 0)


def test_another_group_gets_the_anniversary_without_a_mention(temp_anniv_db, monkeypatch, pushed):
    import anniversary

    monkeypatch.setattr(anniversary.settings, "family_group_id", "GRP_FAMILY")
    _today_row(anniversary, "GRP_OTHER")
    assert anniversary.push_today_reminders("GRP_OTHER") == 1
    assert [call["messages"][0]["type"] for call in pushed] == ["text"]


def test_a_failed_anniversary_push_is_not_marked_and_the_next_row_still_goes(temp_anniv_db, monkeypatch):
    import line_push_client

    import anniversary

    monkeypatch.setattr(line_push_client, "line_access_token", lambda fallback_token=None: "token")
    monkeypatch.setattr(anniversary.settings, "family_group_id", "GRP_FAMILY")
    first = _today_row(anniversary)
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    second = anniversary.insert_anniversary("GRP_FAMILY", "成員乙", "birthday", today.month, today.day)
    answers = [429, 200]
    calls: list[tuple[str, str]] = []

    def push(to, messages, **kwargs):
        calls.append((kwargs["retry_key"], messages[0]["type"]))
        status = answers.pop(0)
        if status != 200:
            raise line_push_client.LinePushError("LINE push HTTP 429", status_code=status)
        return True

    monkeypatch.setattr(line_push_client, "push_messages", push)
    assert anniversary.push_today_reminders("GRP_FAMILY") == 1
    assert [kind for _key, kind in calls] == ["textV2", "textV2"]  # one try per row: a 429 is not resent as plain
    assert len({key for key, _kind in calls}) == 2
    year = today.year
    assert [anniversary.already_reminded(aid, year, 0) for aid in (first, second)].count(True) == 1


def test_the_sweep_skips_the_bots_own_replies(temp_anniv_db, monkeypatch):
    import anniversary
    import memory

    memory.log_raw_message("GRP_FAMILY", "m_member", "U_A", "成員甲 3/5 生日")
    memory.log_raw_message("GRP_FAMILY", "m_bot", "__bot__", "咪寶：成員乙 4/6 生日")
    seen: list[str] = []

    def extract(text):
        seen.append(text)
        person = "成員乙" if "成員乙" in text else "成員甲"
        return [{"person": person, "anniversary_type": "birthday", "month": 3, "day": 5}]

    monkeypatch.setattr(anniversary, "extract_from_text", extract)
    assert anniversary.run_sweep("GRP_FAMILY", since_days=0) == 1
    assert seen == ["成員甲 3/5 生日"]


def test_a_mention_that_cannot_be_built_never_stops_the_anniversary_push(temp_anniv_db, monkeypatch, pushed):
    import line_mentions

    import anniversary

    def boom(*_a, **_kw):
        raise RuntimeError("broken")

    monkeypatch.setattr(anniversary.settings, "family_group_id", "GRP_FAMILY")
    monkeypatch.setattr(line_mentions, "MentionTarget", boom)
    _today_row(anniversary)
    assert anniversary.push_today_reminders("GRP_FAMILY") == 1
    assert [call["messages"][0]["type"] for call in pushed] == ["text"]


def test_the_sweep_still_reads_rows_without_a_sender(temp_anniv_db, monkeypatch):
    import anniversary
    import memory

    with memory._conn() as c:
        c.execute(
            "INSERT INTO raw_messages (group_id, message_id, user_id, text, created_at) "
            "VALUES (?, ?, NULL, ?, strftime('%s','now'))",
            ("GRP_FAMILY", "m_null", "成員甲 3/5 生日"),
        )
    monkeypatch.setattr(
        anniversary, "extract_from_text",
        lambda text: [{"person": "成員甲", "anniversary_type": "birthday", "month": 3, "day": 5}],
    )
    assert anniversary.run_sweep("GRP_FAMILY", since_days=0) == 1
