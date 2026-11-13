"""Feature E tests — TODO extractor + reminder (Andrew 2026-05-25)."""

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")


@pytest.fixture
def temp_todo_db(monkeypatch):
    import todo
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    monkeypatch.setattr(todo, "_DB_PATH", tmp_path)
    todo.init_db()
    yield tmp_path
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass


def test_init_db_creates_table(temp_todo_db):
    import sqlite3
    with sqlite3.connect(temp_todo_db) as c:
        tables = [
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
    assert "todos" in tables


def test_insert_and_list_due_today(temp_todo_db):
    import todo
    today_iso = datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()
    tid = todo.insert_todo(
        group_id="GRP", task="交報告", sender_user_id="U1", due_date=today_iso,
    )
    assert tid
    due = todo.list_due_today("GRP")
    assert len(due) == 1
    assert due[0]["task"] == "交報告"


def test_list_pending_orders_due_dates_before_undated(temp_todo_db):
    import todo
    todo.insert_todo("GRP", "無日期事項", "U1", None)
    todo.insert_todo("GRP", "明確日期事項", "U1", "2026-06-25")

    pending = todo.list_pending("GRP")

    assert [p["task"] for p in pending] == ["明確日期事項", "無日期事項"]


def test_list_pending_can_filter_due_date(temp_todo_db):
    import todo
    todo.insert_todo("GRP", "6月25事項", "U1", "2026-06-25")
    todo.insert_todo("GRP", "6月26事項", "U1", "2026-06-26")

    pending = todo.list_pending("GRP", due_date="2026-06-25")

    assert len(pending) == 1
    assert pending[0]["task"] == "6月25事項"


def test_insert_empty_task_returns_empty():
    import todo
    assert not todo.insert_todo("GRP", "")
    assert not todo.insert_todo("GRP", "   ")


def test_insert_dedup(temp_todo_db):
    import todo
    a = todo.insert_todo("GRP", "task1", "U1", "2026-06-04")
    b = todo.insert_todo("GRP", "task1", "U1", "2026-06-04")
    assert a
    assert not b


def test_complete_todo(temp_todo_db):
    import todo
    tid = todo.insert_todo("GRP", "task", "U1", "2026-06-04")
    assert todo.complete_todo(tid) is True
    # 已 completed 再 complete → False
    assert todo.complete_todo(tid) is False


def test_cancel_todo(temp_todo_db):
    import todo
    tid = todo.insert_todo("GRP", "task", "U1", "2026-06-04")
    assert todo.cancel_todo(tid) is True
    assert todo.cancel_todo(tid) is False


def test_list_overdue(temp_todo_db):
    import todo
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    future = (today + timedelta(days=1)).isoformat()
    todo.insert_todo("GRP", "overdue1", "U1", yesterday)
    todo.insert_todo("GRP", "future1", "U1", future)
    overdue = todo.list_overdue("GRP")
    assert len(overdue) == 1
    assert overdue[0]["task"] == "overdue1"


def test_list_overdue_skips_very_old(temp_todo_db):
    """Overdue 超過 MAX_OVERDUE_DAYS_TO_REMIND 不再 reminder."""
    import todo
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    very_old = (today - timedelta(days=14)).isoformat()
    todo.insert_todo("GRP", "very_old", "U1", very_old)
    overdue = todo.list_overdue("GRP")
    assert not any(t["task"] == "very_old" for t in overdue)


def test_extract_from_text_empty():
    import todo
    assert todo.extract_from_text("") == []


def test_push_daily_reminders_skips_when_no_todos(temp_todo_db):
    import todo
    with patch.object(todo, "_push") as mock_push:
        n = todo.push_daily_reminders("GRP_EMPTY")
    assert n == 0
    assert not mock_push.called


def test_push_daily_reminders_sends_when_due(temp_todo_db):
    import todo
    today_iso = datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()
    tid = todo.insert_todo("GRP", "today task", "U1", today_iso)
    assert tid

    # 2026-05-27 multi-group: GROUP_ID module-global 已移除，改 patch helper（從 env 讀）。
    # `_push(group_id, text, targets, *, retry_key)`（2026-11-12）；patch return_value 不受影響。
    with patch.object(todo, "_get_target_group_ids", return_value=["GRP"]), \
         patch.object(todo, "_push", return_value=True) as mock_push:
        n = todo.push_daily_reminders("GRP")
    assert n >= 1
    assert mock_push.called


def test_main_reminder_requires_group_id():
    import todo
    with patch.object(todo, "_get_target_group_ids", return_value=[]):
        assert todo.main_reminder() == 1


def test_main_extractor_requires_group_id():
    import todo
    with patch.object(todo, "_get_target_group_ids", return_value=[]):
        assert todo.main_extractor_sweep() == 1


# ── 2026-11-12 「提到人就真 @」: the daily TODO push @s the owners whose names it prints ─────────────
# Synthetic ids and names only; the push goes to conftest's recorder, the token is a stand-in.


@pytest.fixture
def named(tmp_path, monkeypatch):
    import json

    import line_push_client

    aliases = tmp_path / "user_aliases.json"
    aliases.write_text(json.dumps({"U_A": "成員甲", "U_B": "成員乙"}, ensure_ascii=False), encoding="utf-8")
    roles = tmp_path / "family_roles.local.json"
    roles.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(roles))
    monkeypatch.setattr(line_push_client, "line_access_token", lambda fallback_token=None: "token")
    monkeypatch.setenv("LINE_BOT_PUSH_MENTIONS", "")
    return line_push_client.push_messages.calls


def test_the_daily_todo_push_pings_each_named_owner_once(temp_todo_db, named):
    import uuid

    import todo

    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    todo.insert_todo("GRP", "繳學費", "U_B", today.isoformat())
    todo.insert_todo("GRP", "買菜", "U_A", today.isoformat())
    todo.insert_todo("GRP", "倒垃圾", "U_A", today.isoformat())
    todo.insert_todo("GRP", "修水管", "U_X", today.isoformat())  # no family name: not printed, not pinged
    expected = todo._format_reminder(todo.list_due_today("GRP"), todo.list_overdue("GRP"))
    assert "- 修水管" in expected  # printed without a name
    assert todo.push_daily_reminders("GRP") == 4
    [call] = named
    message = call["messages"][0]
    assert message["type"] == "textV2"
    head, _, body = message["text"].partition("\n")
    assert head == "{pa} {pb}"
    assert body == expected  # the words are the same as before; only the first line is new
    users = [sub["mentionee"]["userId"] for sub in message["substitution"].values()]
    assert sorted(users) == ["U_A", "U_B"] and len(users) == 2
    assert "U_X" not in str(message)
    assert call["retry_key"] == str(uuid.uuid5(uuid.NAMESPACE_URL, f"todo_daily:GRP:{today.isoformat()}"))


def test_the_daily_todo_text_is_unchanged_and_only_printed_names_are_targets(named):
    import todo

    rows = [{"task": "繳學費", "sender_user_id": "U_B"}, {"task": "買菜", "sender_user_id": "U_X"}]
    text, owners = todo._format_reminder_and_owners(rows, [])
    assert text == todo._format_reminder(rows, []) == "📋 今日 TODO 提醒\n\n🔔 今天到期：\n- 成員乙 繳學費\n- 買菜"
    assert owners == [("U_B", "成員乙")]


def test_a_quota_error_in_one_group_does_not_stop_the_next(temp_todo_db, named, monkeypatch):
    import line_push_client

    import todo

    today = datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()
    todo.insert_todo("GRP1", "繳學費", "U_A", today)
    todo.insert_todo("GRP2", "買菜", "U_B", today)
    sent: list[str] = []

    def push(to, messages, **kwargs):
        if to == "GRP1":
            raise line_push_client.LinePushError("LINE push HTTP 429", status_code=429)
        sent.append(to)
        return True

    monkeypatch.setattr(line_push_client, "push_messages", push)
    with patch.object(todo, "_get_target_group_ids", return_value=["GRP1", "GRP2"]):
        assert todo.main_reminder() == 0
    assert sent == ["GRP2"]


def test_owners_on_overdue_lines_are_pinged_too_in_the_order_the_lines_appear(temp_todo_db, named):
    import todo

    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    todo.insert_todo("GRP", "繳學費", "U_A", (today - timedelta(days=2)).isoformat())  # overdue
    todo.insert_todo("GRP", "買菜", "U_B", today.isoformat())  # due today
    assert todo.push_daily_reminders("GRP") == 2
    [call] = named
    subs = call["messages"][0]["substitution"]
    assert [sub["mentionee"]["userId"] for sub in subs.values()] == ["U_B", "U_A"]  # due lines come first, not id order


def test_a_mention_that_cannot_be_built_never_stops_the_todo_push(temp_todo_db, named, monkeypatch):
    import line_mentions

    import todo

    def boom(*_a, **_kw):
        raise RuntimeError("broken")

    monkeypatch.setattr(line_mentions, "user_mention_targets", boom)
    todo.insert_todo("GRP", "繳學費", "U_A", datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat())
    assert todo.push_daily_reminders("GRP") == 1
    assert [call["messages"][0]["type"] for call in named] == ["text"]


def test_an_owner_alias_with_a_leading_at_is_matched_the_way_it_is_printed(named, monkeypatch):
    import line_mentions

    import todo

    monkeypatch.setattr(line_mentions, "alias_for_user_id", lambda user_id: "@成員丙")
    line, owner = todo._owner_line({"task": "繳學費", "sender_user_id": "U_C"})
    assert owner == ("U_C", "成員丙") and "成員丙" in line
