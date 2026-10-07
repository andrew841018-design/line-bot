from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import calendar_regex
import main
import memory
import reminder_restatement as rr

TW = ZoneInfo("Asia/Taipei")


def snapshot(rid):
    with memory._conn() as c:
        names = [r[1] for r in c.execute("pragma table_info(reminders)")]
        return dict(
            zip(
                names,
                c.execute(
                    "select * from reminders where reminder_id=?", (rid,)
                ).fetchone(),
            )
        )


def fixture():
    now = datetime.now(TW)
    day = now.date() + timedelta(days=3)
    oldday = day + timedelta(days=4)
    stamp = int(
        datetime.combine(day, datetime.min.time(), TW)
        .replace(hour=13, minute=30)
        .timestamp()
    )
    source = f"{oldday.month}月{oldday.day}日下午1:30在測試飯店正門口接我"
    text = f"我是{day.month}月{day.day}日下午1:30在測試飯店正門口，需要帶西裝外套、領帶正裝拍照"
    rid = memory.add_reminder(
        "G_TEST", "U_TEST", "測試飯店正門口接人", stamp + 4 * 86400, source_text=source
    )
    with memory._conn() as c:
        c.executemany(
            "insert into raw_messages(group_id,user_id,message_id,text,created_at) values(?,?,?,?,?)",
            [
                ("G_TEST", "U_TEST", "source", source, int(now.timestamp()) - 1747),
                ("G_TEST", "U_TEST", "current", text, int(now.timestamp())),
            ],
        )
    return rid, text, stamp


def test_weekday_overrides_model_wrong_friday():
    result = rr.complete_result(
        "下星期一下午1:30在測試飯店接我",
        {"year": 2026, "month": 9, "day": 18, "action": "接人"},
        date(2026, 9, 11),
    )
    assert (result["year"], result["month"], result["day"]) == (2026, 9, 14)


def test_preparation_clause_survives_model_and_regex():
    text = "我是2月14日下午1:30在六福萬怡酒店，需要帶雨傘、證件進場拍照"
    result = rr.complete_result(text, {"action": "拍照"}, date(2031, 2, 11))
    events = calendar_regex.extract_many_regex_only(text, date(2031, 2, 11))
    assert events
    for word in ("雨傘", "證件", "進場拍照"):
        assert word in result["action"] and word in events[0]["title"]


def test_correction_persisted_before_reply_without_later_routing(monkeypatch):
    rid, text, stamp = fixture()
    replies = []

    def reply(token, body, **kw):
        assert snapshot(rid)["remind_at"] == stamp
        assert "西裝外套、領帶正裝拍照" in snapshot(rid)["action"]
        replies.append(body)

    monkeypatch.setattr(main, "_reply", reply)

    def forbid(*a, **kw):
        raise AssertionError("later routing")

    monkeypatch.setattr(main, "_try_handle_quoted_calendar_correction", forbid)
    event = SimpleNamespace(
        source=SimpleNamespace(user_id="U_TEST"),
        message=SimpleNamespace(text=text, id="current", quoted_message_id=None),
        reply_token="test",
    )
    main._handle_text_message(event, "G_TEST")
    main._handle_text_message(event, "G_TEST")
    assert len(replies) == 2
    assert len(memory.list_pending_reminders_full("G_TEST")) == 1
    assert all("已更新" in reply for reply in replies)


def test_reconcile_exact_repair_and_source_replay():
    rid, text, stamp = fixture()
    old = snapshot(rid)
    right = memory.add_reminder("G_TEST", "U_TEST", "拍照", stamp, source_text=text)
    with memory._conn() as c:
        c.execute("update reminders set pushed_3d=1 where reminder_id=?", (right,))
    target = snapshot(right)
    assert (
        rr.reconcile(target, [old], action=text, remind_at=stamp, source_text=text)
        == "updated"
    )
    assert snapshot(right)["pushed_3d"] == 1
    assert snapshot(rid)["status"] == "cancelled"
    _, outcome = memory.add_reminder_with_outcome(
        "G_TEST",
        "U_TEST",
        old["action"],
        old["remind_at"],
        source_text=old["source_text"],
    )
    assert outcome == "inactive"
    assert len(memory.list_pending_reminders_full("G_TEST")) == 1
    assert (
        rr.reconcile(
            snapshot(right), [], action=text, remind_at=stamp, source_text=text
        )
        == "unchanged"
    )


def test_stale_snapshot_changes_nothing():
    rid, text, stamp = fixture()
    old = snapshot(rid)
    with memory._conn() as c:
        c.execute("update reminders set action=? where reminder_id=?", ("changed", rid))
    assert (
        rr.reconcile(old, [], action=text, remind_at=stamp, source_text=text)
        == "conflict"
    )
    assert snapshot(rid)["action"] == "changed"


@pytest.mark.parametrize(
    "kind", ["wrong_user", "stale", "ambiguous", "question", "terminal"]
)
def test_unsafe_targets_not_changed(kind):
    rid, text, stamp = fixture()
    user = "U_OTHER" if kind == "wrong_user" else "U_TEST"
    with memory._conn() as c:
        if kind == "stale":
            c.execute(
                "update raw_messages set created_at=created_at-7200 where message_id=?",
                ("source",),
            )
        if kind == "terminal":
            c.execute(
                "update reminders set status='cancelled' where reminder_id=?", (rid,)
            )
        if kind == "ambiguous":
            c.execute(
                "insert into reminders(group_id,user_id,action,remind_at,created_at,status,source_text) select group_id,user_id,action,remind_at+86400,created_at,status,source_text from reminders where reminder_id=?",
                (rid,),
            )
    before = snapshot(rid)
    result = rr.correction(
        text + "？" if kind == "question" else text, "G_TEST", user, "current"
    )
    assert result is None or result["status"] == "ambiguous"
    assert snapshot(rid) == before


@pytest.mark.parametrize(
    "text",
    [
        "明天中午提醒我確認下星期一行程",
        "下星期一活動，24號再繳費",
        "下星期一活動，二十四日再繳費",
    ],
)
def test_payload_weekday_does_not_override_other_date(text):
    base = {"year": 2026, "month": 9, "day": 24, "action": "繳費"}
    assert rr.complete_result(text, base, date(2026, 9, 11)) == base


@pytest.mark.parametrize(
    "clause", ["不要帶雨傘", "不需要帶雨傘", "需要帶雨傘，證件進場拍照"]
)
def test_preparation_negation_and_continuation_preserved(clause):
    result = rr.complete_result(
        "明天13:30拍照，" + clause, {"action": "拍照"}, date(2026, 9, 11)
    )
    assert result["action"] == "拍照，" + clause


@pytest.mark.parametrize("state", ["sending", "uncertain", "processing"])
def test_busy_does_not_modify_any_reminder(state):
    rid, text, stamp = fixture()
    old = snapshot(rid)
    with memory._conn() as c:
        if state == "processing":
            c.execute(
                "insert into pending_reminder_extract(group_id,user_id,message_id,text,created_at,status) values(?,?,?,?,?,?)",
                (
                    "G_TEST",
                    "U_TEST",
                    "pending",
                    old["source_text"],
                    int(datetime.now(TW).timestamp()),
                    state,
                ),
            )
        else:
            c.execute(
                "insert into reminder_delivery_claims(group_id,delivery_kind,subject_ref,occurrence,transport,state,claim_token,retry_key,claimed_at) values(?,?,?,?,?,?,?,?,?)",
                (
                    "G_TEST",
                    "natural",
                    str(rid),
                    "test",
                    "push",
                    state,
                    "test",
                    "test",
                    int(datetime.now(TW).timestamp()),
                ),
            )
    assert (
        rr.reconcile(old, [], action=text, remind_at=stamp, source_text=text) == "busy"
    )
    assert snapshot(rid) == old


def test_failed_target_write_rolls_back_cancellation():
    import sqlite3

    rid, text, stamp = fixture()
    old = snapshot(rid)
    right = memory.add_reminder("G_TEST", "U_TEST", "拍照", stamp, source_text=text)
    target = snapshot(right)
    with memory._conn() as c:
        c.execute(
            "CREATE TRIGGER fail_target BEFORE UPDATE OF action ON reminders BEGIN SELECT RAISE(ABORT,'test'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        rr.reconcile(target, [old], action=text, remind_at=stamp, source_text=text)
    assert snapshot(right) == target and snapshot(rid) == old


def test_linked_pending_confirmation_fences_atomic_repair():
    rid, text, stamp = fixture()
    old = snapshot(rid)
    with memory._conn() as c:
        pid = c.execute(
            "insert into pending_reminder_extract(group_id,user_id,message_id,text,created_at,status) values(?,?,?,?,?,?)",
            (
                "G_TEST",
                "U_TEST",
                "pending",
                old["source_text"],
                int(datetime.now(TW).timestamp()),
                "done",
            ),
        ).lastrowid
        c.execute(
            "insert into reminder_confirmation_outbox(group_id,source_ref,text,created_at) values(?,?,?,?)",
            (
                "G_TEST",
                f"pending_reminder:{pid}",
                "old acknowledgement",
                int(datetime.now(TW).timestamp()),
            ),
        )
    assert (
        rr.reconcile(old, [], action=text, remind_at=stamp, source_text=text) == "busy"
    )
    assert snapshot(rid) == old


def test_multiple_schedules_never_mutate_original():
    rid, text, stamp = fixture()
    old = snapshot(rid)
    result = rr.correction(
        text + "，9/18下午2:30還有活動", "G_TEST", "U_TEST", "current"
    )
    assert result is None or result["status"] == "ambiguous"
    assert snapshot(rid) == old
