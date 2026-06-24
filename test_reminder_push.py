from unittest.mock import MagicMock, patch

import reminder_push


def _row(now: int, **overrides):
    data = {
        "reminder_id": 7,
        "group_id": "G1",
        "user_id": "U1",
        "action": "看醫生",
        "remind_at": now + 86400,
        "created_at": now - 3600,
        "source_text": "明天看醫生",
        "last_pushed_at": 0,
        "weekly_count": 0,
        "last_weekly_at": 0,
        "pushed_3d": 0,
        "pushed_1d": 0,
        "pushed_4hr": 0,
        "pushed_2hr": 0,
        "pushed_1hr": 0,
        "pushed_now": 0,
    }
    data.update(overrides)
    return data


def test_due_reminders_for_reply_returns_due_items(monkeypatch):
    now = 1_800_000_000
    seen = {}

    def fake_list(group_id=None):
        seen["group_id"] = group_id
        return [_row(now)]

    monkeypatch.setattr(reminder_push.memory, "list_pending_reminders_full", fake_list)

    due = reminder_push.due_reminders_for_reply("G1", limit=2, now=now)
    remind_at = reminder_push.datetime.fromtimestamp(now + 86400).strftime(
        "%Y-%m-%d %H:%M"
    )

    assert seen["group_id"] == "G1"
    assert len(due) == 1
    assert due[0]["reminder_id"] == 7
    assert due[0]["group_id"] == "G1"
    assert due[0]["stage"] == "1d"
    assert due[0]["text"] == f"@當事人\n⏰ 提醒（明天）\n{remind_at} 看醫生"
    assert due[0]["action"] == "看醫生"
    assert due[0]["message"].type == "textV2"
    assert due[0]["message"].text == f"{{target}}\n⏰ 提醒（明天）\n{remind_at} 看醫生"
    assert due[0]["message"].substitution["target"].mentionee.user_id == "U1"


def test_due_reminder_label_uses_calendar_day_not_stage_window(monkeypatch):
    now_dt = reminder_push.datetime(2026, 6, 14, 9, 0)
    target_dt = reminder_push.datetime(2026, 6, 16, 8, 0)
    now = int(now_dt.timestamp())

    def fake_list(group_id=None):
        return [_row(now, remind_at=int(target_dt.timestamp()))]

    monkeypatch.setattr(reminder_push.memory, "list_pending_reminders_full", fake_list)

    due = reminder_push.due_reminders_for_reply("G1", limit=2, now=now)

    assert len(due) == 1
    assert due[0]["stage"] == "1d"
    assert due[0]["text"].startswith("@當事人\n⏰ 提醒（後天）\n")


def test_far_future_reminder_does_not_weekly_push_immediately(monkeypatch):
    """Very distant reminders should stay quiet until they are within 30 days."""
    now = 1_800_000_000

    def fake_list(group_id=None):
        return [_row(now, remind_at=now + 192 * 86400, action="明年初檢查")]

    monkeypatch.setattr(reminder_push.memory, "list_pending_reminders_full", fake_list)

    assert reminder_push.due_reminders_for_reply("G1", limit=2, now=now) == []


def test_weekly_reminder_starts_within_30_days(monkeypatch):
    now = 1_800_000_000

    def fake_list(group_id=None):
        return [_row(now, remind_at=now + 30 * 86400, action="一個月後檢查")]

    monkeypatch.setattr(reminder_push.memory, "list_pending_reminders_full", fake_list)

    due = reminder_push.due_reminders_for_reply("G1", limit=2, now=now)

    assert len(due) == 1
    assert due[0]["stage"] == "weekly"
    assert "（1 個月後）" in due[0]["text"]


def test_due_reminder_mentions_companion_alias_from_action(monkeypatch):
    now = 1_800_000_000
    action = "曾美惠正子斷層掃描當天 08:00 開始禁食 6 小時，只能喝水（黃聖雅陪同）"

    def fake_list(group_id=None):
        return [_row(now, user_id="U_MOM", action=action)]

    monkeypatch.setattr(reminder_push.memory, "list_pending_reminders_full", fake_list)
    monkeypatch.setattr(
        reminder_push.line_mentions,
        "load_user_aliases",
        lambda: {"U_MOM": "媽媽", "U_SIS": "黃聖雅"},
    )

    due = reminder_push.due_reminders_for_reply("G1", limit=2, now=now)

    assert len(due) == 1
    assert due[0]["text"].startswith("@媽媽 @黃聖雅\n⏰ 提醒（明天）\n")
    assert due[0]["message"].text.startswith("{target} {p2}\n⏰ 提醒（明天）\n")
    assert due[0]["message"].substitution["target"].mentionee.user_id == "U_MOM"
    assert due[0]["message"].substitution["p2"].mentionee.user_id == "U_SIS"


def test_due_reminder_mentions_structured_aliases_without_action_names(monkeypatch):
    now = 1_800_000_000

    def fake_list(group_id=None):
        return [
            _row(
                now,
                user_id="U_MOM",
                action="正子斷層掃描當天 08:00 開始禁食 6 小時，只能喝水",
                mention_aliases=["媽媽", "黃聖雅"],
            )
        ]

    monkeypatch.setattr(reminder_push.memory, "list_pending_reminders_full", fake_list)
    monkeypatch.setattr(
        reminder_push.line_mentions,
        "load_user_aliases",
        lambda: {"U_MOM": "媽媽", "U_SIS": "黃聖雅"},
    )

    due = reminder_push.due_reminders_for_reply("G1", limit=2, now=now)

    assert len(due) == 1
    assert due[0]["text"].startswith("@媽媽 @黃聖雅\n⏰ 提醒（明天）\n")
    assert "參加人：媽媽、黃聖雅" in due[0]["text"]
    assert due[0]["message"].substitution["target"].mentionee.user_id == "U_MOM"
    assert due[0]["message"].substitution["p2"].mentionee.user_id == "U_SIS"


def test_due_reminder_keeps_unmapped_participant_visible(monkeypatch):
    now = 1_800_000_000

    def fake_list(group_id=None):
        return [_row(now, user_id="", mention_aliases=["黃將修"])]

    monkeypatch.setattr(reminder_push.memory, "list_pending_reminders_full", fake_list)
    monkeypatch.setattr(reminder_push.line_mentions, "load_user_aliases", lambda: {})

    due = reminder_push.due_reminders_for_reply("G1", limit=2, now=now)

    assert len(due) == 1
    assert due[0]["text"].startswith("@黃將修\n⏰ 提醒（明天）\n")
    assert "參加人：黃將修" in due[0]["text"]
    assert due[0]["message"].type == "text"


def test_due_reminder_mentions_all_participants(monkeypatch):
    now = 1_800_000_000

    def fake_list(group_id=None):
        return [_row(now, user_id="U_MOM", mention_aliases=["全家"]) ]

    monkeypatch.setattr(reminder_push.memory, "list_pending_reminders_full", fake_list)
    monkeypatch.setattr(reminder_push.line_mentions, "load_user_aliases", lambda: {"U_MOM": "媽媽"})

    due = reminder_push.due_reminders_for_reply("G1", limit=2, now=now)

    assert len(due) == 1
    assert due[0]["text"].startswith("@all\n")
    assert due[0]["message"].substitution.get("all") is not None


def test_mark_reminders_pushed_marks_each_stage(monkeypatch):
    marked = []
    monkeypatch.setattr(
        reminder_push.memory,
        "mark_reminder_pushed",
        lambda reminder_id, stage: marked.append((reminder_id, stage)) or True,
    )

    assert reminder_push.mark_reminders_pushed([(7, "1d"), (8, "now")]) == 2
    assert marked == [(7, "1d"), (8, "now")]


def test_push_to_group_uses_refreshed_line_token(monkeypatch):
    monkeypatch.setattr(reminder_push.settings, "line_channel_access_token", "stale-token")
    monkeypatch.setattr(reminder_push, "line_access_token", lambda: "fresh-token")

    seen = {}

    class FakeApiClient:
        def __init__(self, cfg):
            seen["token"] = cfg.access_token

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch("reminder_push.ApiClient", FakeApiClient), patch(
        "reminder_push.MessagingApi"
    ) as messaging_api:
        messaging_api.return_value.push_message = MagicMock()
        assert reminder_push._push_to_group("G1", "hello")

    assert seen["token"] == "fresh-token"


def test_line_access_token_falls_back_to_env_token(monkeypatch):
    monkeypatch.setattr(reminder_push.settings, "line_channel_access_token", "env-token")
    monkeypatch.setattr(reminder_push, "line_access_token", lambda: "env-token")

    assert reminder_push._line_access_token() == "env-token"
