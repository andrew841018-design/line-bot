from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import main
import memory


TW = ZoneInfo("Asia/Taipei")


def seed(text="我週末在整理書櫃內的東西", *, user="U_TEST", age=30):
    now = int(datetime.now(TW).timestamp())
    with memory._conn() as c:
        c.executemany(
            "INSERT INTO raw_messages(group_id,message_id,user_id,text,created_at) VALUES (?,?,?,?,?)",
            [
                ("G_TEST", "source", user, text, now - age),
                ("G_TEST", "bot", "__bot__", "這是閒聊。", now - 10),
                ("G_TEST", "request", "U_TEST", "不是閒聊，加入提醒事項", now),
            ],
        )


def event(text="不是閒聊，加入提醒事項", quote="bot", mid="request"):
    return SimpleNamespace(
        message=SimpleNamespace(
            id=mid, text=text, quoted_message_id=quote, mention=None
        ),
        source=SimpleNamespace(user_id="U_TEST"),
        reply_token="synthetic-token",
    )


def forbid(*a, **kw):
    raise AssertionError("must not reach model or later chat routing")


def test_original_weekend_requests_date_before_chat(monkeypatch):
    replies = []
    monkeypatch.setattr(main, "_reply", lambda token, text, **kw: replies.append(text))
    monkeypatch.setattr(main, "_try_one_shot_reply", forbid)
    monkeypatch.setattr(main, "_auto_capture_text_if_important", forbid)
    main._handle_text_message(
        event("我週末在整理書櫃內的東西", None, "original"), "G_TEST"
    )
    assert len(replies) == 1
    assert "尚未新增" in replies[0] and "日期" in replies[0]
    assert "整理書櫃" in replies[0]
    assert memory.list_pending_reminders_full("G_TEST") == []


@pytest.mark.parametrize("quote", ["bot", "source", None])
def test_explicit_correction_recovers_source_and_clarifies(monkeypatch, quote):
    seed()
    replies = []
    monkeypatch.setattr(main, "_reply", lambda token, text, **kw: replies.append(text))
    monkeypatch.setattr(main, "_try_one_shot_reply", forbid)
    monkeypatch.setattr(main.gemini_client, "extract_reminder", forbid)
    main._handle_text_message(event(quote=quote), "G_TEST")
    assert "整理書櫃" in replies[0] and "尚未新增" in replies[0]
    assert memory.list_pending_reminders_full("G_TEST") == []
    assert memory.get_pending_reminder_extract_by_message("G_TEST", "request") is None


def test_exact_date_source_persists_once_with_original_provenance(monkeypatch):
    day = datetime.now(TW).date() + timedelta(days=3)
    source = f"{day.month}月{day.day}日整理書櫃"
    seed(source)
    replies = []
    monkeypatch.setattr(main, "_reply", lambda token, text, **kw: replies.append(text))
    monkeypatch.setattr(main, "_try_one_shot_reply", forbid)
    main._handle_text_message(event(), "G_TEST")
    main._handle_text_message(event(), "G_TEST")
    rows = memory.list_pending_reminders_full("G_TEST")
    assert len(rows) == 1
    row = memory.get_reminder(rows[0]["reminder_id"])
    assert row["source_ref"] == "source"
    assert row["source_text"] == source
    assert row["action"] == "整理書櫃"
    assert datetime.fromtimestamp(row["remind_at"], TW).hour == 12
    assert all("提醒" in r for r in replies)


@pytest.mark.parametrize(
    "user,age,quote",
    [("U_OTHER", 30, "bot"), ("U_TEST", 600, "bot"), ("U_TEST", 30, "missing")],
)
def test_unsafe_source_gets_clarification_without_chat(monkeypatch, user, age, quote):
    seed(user=user, age=age)
    replies = []
    monkeypatch.setattr(main, "_reply", lambda token, text, **kw: replies.append(text))
    monkeypatch.setattr(main, "_try_one_shot_reply", forbid)
    main._handle_text_message(event(quote=quote), "G_TEST")
    assert "尚未新增" in replies[0]
    assert "整理書櫃" not in replies[0]
    assert memory.list_pending_reminders_full("G_TEST") == []


@pytest.mark.parametrize(
    "text",
    [
        "我上週末整理書櫃",
        "我週末不整理書櫃",
        "我週末可能整理書櫃",
        "週末要不要整理書櫃？",
        "他說週末整理書櫃",
        "不要加入提醒事項",
        "我週末已經整理完書櫃",
        "週末整理書櫃的方法是什麼？",
    ],
)
def test_non_requests_not_intercepted(monkeypatch, text):
    monkeypatch.setattr(main, "_reply", forbid)
    assert main._try_handle_creation_followup(event(text), "G_TEST", text) is False


def test_intervening_human_prevents_bot_quote_inference(monkeypatch):
    seed()
    with memory._conn() as c:
        now = c.execute(
            "SELECT created_at FROM raw_messages WHERE message_id='request'"
        ).fetchone()[0]
        c.execute(
            "INSERT INTO raw_messages VALUES (?,?,?,?,?,?)",
            ("G_TEST", "interloper", "U_OTHER", "無關訊息", now - 5, None),
        )
    import reminder_followup

    assert (
        reminder_followup.resolve_source("G_TEST", "U_TEST", "request", "bot") is None
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        ("我週末在整理書櫃", None),
        # An explicit request to 咪寶 never routes on to chat (GP1 r2, S2);
        # the handler's weekend clarification normally answers it first.
        ("咪寶 提醒我這週末整理書櫃", main._REMINDER_RESEND_FORMAT_REPLY),
        # Not said to 咪寶: production's None (GP1 r3 #1).
        ("提醒我這週末整理書櫃", None),
    ],
)
def test_ambiguous_weekend_cannot_be_queued_or_model_extracted(
    monkeypatch, text, expected
):
    monkeypatch.setattr(main.gemini_client, "extract_reminder", forbid)
    assert main._maybe_extract_reminder(text, "G_TEST", "U_TEST", "source") == expected
    assert (
        main._enqueue_reminder_if_candidate(text, "G_TEST", "U_TEST", "source") is None
    )
    assert (
        main._auto_capture_text_if_important("G_TEST", text, "U_TEST", "source")
        is False
    )


def test_cancelled_source_is_not_revived(monkeypatch):
    day = datetime.now(TW).date() + timedelta(days=3)
    seed(f"{day.month}月{day.day}日整理書櫃")
    monkeypatch.setattr(main, "_reply", lambda *a, **kw: None)
    main._handle_text_message(event(), "G_TEST")
    with memory._conn() as c:
        c.execute("UPDATE reminders SET status='cancelled' WHERE group_id='G_TEST'")
    main._handle_text_message(event(), "G_TEST")
    assert memory.list_pending_reminders_full("G_TEST") == []
    with memory._conn() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM reminders WHERE group_id='G_TEST'"
            ).fetchone()[0]
            == 1
        )


def test_write_failure_returns_truthful_reply(monkeypatch):
    import reminder_followup

    day = datetime.now(TW).date() + timedelta(days=3)
    seed(f"{day.month}月{day.day}日整理書櫃")
    replies = []
    monkeypatch.setattr(main, "_reply", lambda token, text, **kw: replies.append(text))
    monkeypatch.setattr(reminder_followup, "persist_source", forbid)
    main._handle_text_message(event(), "G_TEST")
    assert "無法確認" in replies[0] and "已新增" not in replies[0]


@pytest.mark.parametrize(
    "text",
    [
        "提醒我明天中午確認週末旅行清單",
        "提醒我明天上午9點整理週末旅行行李",
    ],
)
def test_weekend_in_payload_does_not_block_exact_schedule(monkeypatch, text):
    monkeypatch.setattr(main, "_reply", forbid)
    assert main._try_handle_creation_followup(event(text), "G_TEST", text) is False
    assert not main._should_suppress_reminder_write(text)


def test_same_relative_text_on_another_date_is_not_same_source():
    import reminder_followup

    now = int(datetime.now(TW).timestamp())
    result = {"action": "整理書櫃"}
    first = {"message_id": "first", "text": "明天整理書櫃", "user_id": "U_TEST"}
    second = dict(first, message_id="second")
    rid, outcome = reminder_followup.persist_source(
        "G_TEST", first, result, now + 86400
    )
    assert outcome == "created"
    with memory._conn() as c:
        c.execute("UPDATE reminders SET status='cancelled' WHERE reminder_id=?", (rid,))
    other, outcome = reminder_followup.persist_source(
        "G_TEST", second, result, now + 172800
    )
    assert outcome == "created" and other != rid


@pytest.mark.parametrize("status", ["active", "cancelled"])
def test_calendar_source_without_mirror_never_creates_parallel_reminder(status):
    import reminder_followup

    now = int(datetime.now(TW).timestamp())
    with memory._conn() as c:
        c.execute(
            "INSERT INTO events(event_id,group_id,title,event_date,source_msg_id,status,created_at) VALUES (?,?,?,?,?,?,?)",
            ("evt", "G_TEST", "整理書櫃", "2099-01-01", "source", status, now),
        )
    source = {"message_id": "source", "text": "明天整理書櫃", "user_id": "U_TEST"}
    assert reminder_followup.persist_source(
        "G_TEST", source, {"action": "整理書櫃"}, now + 86400
    ) == (None, "inactive")


@pytest.mark.parametrize("quote", ["bot", None, "source"])
def test_multi_message_burst_requires_exact_human_quote(quote):
    import reminder_followup

    seed()
    with memory._conn() as c:
        source_at = c.execute(
            "SELECT created_at FROM raw_messages WHERE message_id='source'"
        ).fetchone()[0]
        c.execute(
            "INSERT INTO raw_messages(group_id,message_id,user_id,text,created_at) VALUES (?,?,?,?,?)",
            ("G_TEST", "another-source", "U_TEST", "明天寄包裹", source_at - 1),
        )
    result = reminder_followup.resolve_source(
        "G_TEST", "U_TEST", "request", quote or ""
    )
    if quote == "source":
        assert result and result["message_id"] == "source"
    else:
        assert result is None


def test_expired_due_time_never_inserted():
    import reminder_followup

    source = {"message_id": "source", "text": "今天整理書櫃", "user_id": "U_TEST"}
    assert reminder_followup.persist_source(
        "G_TEST",
        source,
        {"action": "整理書櫃"},
        int(datetime.now(TW).timestamp()) - 1,
    ) == (None, "expired")
