"""家人可要求提醒只推一次 (2026-11-12 proposal): synthetic data only."""

from __future__ import annotations

import itertools
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent

import line_mentions
import main
import memory
import reminder_push
import reminder_push_limit as rpl
import reminder_reschedule as rr
import reminder_stages

TW = rr.TAIPEI
_IDS = itertools.count(1)
HOUR = 3600
DAY = 86400
# The appointment every timeline test is about (a future date, never the real clock).
T = int(datetime(2030, 6, 12, 14, 0, tzinfo=TW).timestamp())


def _seed(action: str, remind_at: int, *, user_id: str = "U1", group_id: str = "G1") -> int:
    reminder_id = memory.add_reminder(group_id, user_id, action, remind_at, source_text=action)
    assert reminder_id is not None
    return int(reminder_id)


def _sql(statement: str, params: tuple = ()) -> None:
    with memory._conn() as conn:
        conn.execute(statement, params)


def _state(reminder_id: int, group_id: str = "G1") -> dict:
    state = memory.get_reminder_push_state(group_id, reminder_id)
    assert state is not None
    return state


def _limit(reminder_id: int, keep: str, *, now: int, group_id: str = "G1") -> dict:
    state = _state(reminder_id, group_id)
    return memory.set_reminder_push_keep_stage(
        group_id,
        reminder_id,
        keep_stage=keep,
        expected_action=state["action"],
        expected_remind_at=state["remind_at"],
        now=now,
    )


class _Ladder:
    """Runs the real 15-minute push job on a fake clock and records what LINE accepted."""

    def __init__(self, monkeypatch):
        self.clock = 0
        self.claimed: list[tuple[int, str]] = []
        self.sent: list[tuple[int, str]] = []
        self.fail: set[str] = set()
        monkeypatch.setattr(time, "time", lambda: float(self.clock))
        original_claim = reminder_push._claim_due_item

        def recording_claim(item):
            claim = original_claim(item)
            if claim is not None:
                self.claimed.append((int(item["reminder_id"]), str(item["stage"])))
            return claim

        def fake_push(group_id, text, *args, **kwargs):
            reminder_id, stage = self.claimed[-1]
            if stage in self.fail:
                return False
            self.sent.append((reminder_id, stage))
            return True

        monkeypatch.setattr(reminder_push, "_claim_due_item", recording_claim)
        monkeypatch.setattr(reminder_push, "_push_to_group", fake_push)

    def run(self, start: int, end: int, step: int = 900) -> None:
        for moment in range(int(start), int(end) + 1, step):
            self.clock = moment
            reminder_push.push_reminders()

    def stages(self, reminder_id: int) -> list[str]:
        return [stage for rid, stage in self.sent if rid == reminder_id]


@pytest.fixture
def ladder(monkeypatch):
    return _Ladder(monkeypatch)


# ── Classifier ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, keep",
    [
        ("只要一小時前提醒", "1hr"),
        ("1小時前提醒我就好", "1hr"),
        ("我只要一小時前提醒就好", "1hr"),
        ("改成一小時前提醒", "1hr"),
        ("提前一小時提醒就好", "1hr"),
        ("一個小時前提醒一次就好", "1hr"),
        ("只要一小時前提醒，其他不用", "1hr"),
        ("不用一直提醒，一小時前提醒就好", "1hr"),
        ("咪寶 只要兩小時前提醒就好", "2hr"),
        ("只要4小時前提醒", "4hr"),
        ("提醒一次就好", "now"),
        ("只要提醒一次", "now"),
        ("提醒１次就好", "now"),
        ("幫我改成只提醒一次", "now"),
        ("到時再提醒就好", "now"),
        ("時間到再提醒就好", "now"),
        ("可以只提醒一次嗎", "now"),
        ("提醒一次就好囉🙏", "now"),
        ("前一小時提醒就好", "1hr"),
        ("能不能只提醒一次", "now"),
        ("可以只提醒一次就好", "now"),
        ("只要提醒一次嘛", "now"),
        ("提醒我一次就好", "now"),
        ("提醒一次就好哈哈", "now"),
    ],
)
def test_keep_phrases(text, keep):
    assert rpl.classify(text) == rpl.PushLimitRequest(rpl.KEEP, keep=keep)


@pytest.mark.parametrize(
    "text", ["照原本提醒就好", "照原本的提醒就好", "照舊提醒就好", "改回原本的提醒", "照原本的就好", "照舊就好", "改回原本就好"]
)
def test_restore_phrases(text):
    assert rpl.classify(text).status == rpl.RESTORE


@pytest.mark.parametrize(
    "text, reason",
    [
        ("四十分鐘前提醒就好", "unsupported_stage"),
        ("前一天提醒就好", "unsupported_stage"),
        ("出門前提醒就好", "unsupported_stage"),
        ("24小時前提醒就好", "unsupported_stage"),
        ("12小時前提醒就好", "unsupported_stage"),
        ("1個半小時前提醒就好", "unsupported_stage"),
        ("提醒三遍就好", "unsupported_count"),
        ("前一天跟一小時前提醒就好", "multiple_stages"),
        ("以後都只要提醒一次", "global"),
        ("太多提醒了", "complaint"),
        ("提醒太多次了", "complaint"),
        ("不用推那麼多次", "complaint"),
    ],
)
def test_reply_only_phrases(text, reason):
    assert rpl.classify(text) == rpl.PushLimitRequest(rpl.REPLY, reason=reason)


@pytest.mark.parametrize(
    "text",
    [
        "一次就好", "再提醒一次", "晚點再提醒我一次", "多提醒一次", "提醒他一次就好",
        "會只提醒一次嗎", "不要只提醒一次", "不要一小時前提醒就好", "不用提醒了",
        "只要提前一小時", "1點前提醒就好", "晚上提醒我", "晚上提醒我吃藥", "睡前提醒我刷牙",
        "每天早上提醒我量血壓", "前一天提醒我繳費", "也提醒我早上帶健保卡",
        "明天早上提醒我一次就好", "下午三點提醒一次就好", "提醒一次就好改到三點",
        "好", "收到", "這則取消", "改到10/8", "不用恢復",
        # 「恢復這則提醒」 is what the cancel flow tells the family to say.
        "恢復這則提醒", "恢復提醒",
        # Statements and the speaker doing it themselves.
        "只提醒了一次", "只通知了一次", "推了一次就好", "我提醒一次就好", "只提醒一次了",
        # Dayparts belong to reschedule or creation.
        "改成早上提醒", "晚上提醒我就好", "中午提醒就好",
    ],
)
def test_not_ours(text):
    assert rpl.classify(text).status == rpl.NOT_OURS


# ── Stage helpers ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("keep", ["now", "1hr", "2hr", "4hr"])
def test_keep_window_state_matches_window_stage(keep):
    for delta in range(-3 * HOUR, 6 * HOUR, 60):
        now = T - delta
        open_ = reminder_stages.keep_window_state(T, keep, now) == "open"
        assert open_ == (reminder_stages.window_stage(T, now) == keep), (keep, delta)


def test_limit_counts_only_on_generic_rows_and_known_values():
    row = {"push_keep_stage": "1hr", "source_kind": "", "source_ref": ""}
    assert reminder_stages.keep_stage_of(row) == "1hr"
    assert reminder_stages.keep_stage_of({**row, "source_kind": "calendar_event", "source_ref": "E1"}) == ""
    for bad in ("weekly", "1d", "3d", "garbage", None):
        assert reminder_stages.keep_stage_of({**row, "push_keep_stage": bad}) == ""


# ── Acceptance through the real push job ───────────────────────────────────


def test_only_one_hour_before_pushes_once_then_done(ladder):
    rid = _seed("看牙醫", T)
    result = _limit(rid, "1hr", now=T - 2 * DAY)
    assert result["status"] == "updated" and not result["kept_already_sent"]
    ladder.run(T - 2 * DAY, T + 2 * HOUR)
    assert ladder.stages(rid) == ["1hr"]
    assert _state(rid)["status"] == "done"


@pytest.mark.parametrize("keep", ["2hr", "4hr"])
def test_two_and_four_hours_before(ladder, keep):
    rid = _seed("看牙醫", T)
    _limit(rid, keep, now=T - DAY)
    ladder.run(T - DAY, T + HOUR)
    assert ladder.stages(rid) == [keep]
    assert _state(rid)["status"] == "done"


def test_limit_survives_a_time_change(ladder):
    rid = _seed("看牙醫", T)
    _limit(rid, "1hr", now=T - 2 * DAY)
    assert memory.update_reminder_schedule(rid, T + HOUR)
    ladder.run(T - DAY, T + 2 * HOUR)
    assert ladder.stages(rid) == ["1hr"]


def test_stray_day_value_never_completes_the_reminder_early(ladder):
    rid = _seed("看牙醫", T)
    _sql("UPDATE reminders SET push_keep_stage='1d' WHERE reminder_id=?", (rid,))
    ladder.run(T - 2 * DAY, T + HOUR)
    assert ladder.stages(rid) == ["1d", "4hr", "2hr", "1hr", "now"]


def test_once_keeps_only_the_at_time_push_even_weeks_ahead(ladder):
    rid = _seed("繳費", T)
    assert _limit(rid, "now", now=T - 20 * DAY)["status"] == "updated"
    ladder.run(T - 20 * DAY, T - 5 * HOUR, step=6 * HOUR)
    ladder.run(T - 5 * HOUR, T + HOUR)
    assert ladder.stages(rid) == ["now"]


def test_other_reminders_keep_the_whole_ladder(ladder):
    limited = _seed("看牙醫", T)
    other = _seed("領包裹", T + 600, user_id="U2")
    _limit(limited, "1hr", now=T - 2 * DAY)
    ladder.run(T - 2 * DAY, T + 2 * HOUR)
    assert ladder.stages(limited) == ["1hr"]
    # 2 days and 10 minutes ahead at the start: the whole ladder, 3 days included.
    assert ladder.stages(other) == ["3d", "1d", "4hr", "2hr", "1hr", "now"]


def test_stage_eaten_by_the_creation_receipt_comes_back(ladder):
    created = T - (HOUR + 48 * 60)
    rid = _seed("看牙醫", T)
    memory.consume_open_stages([rid], now=created)
    assert _state(rid)["pushed_1hr"] == 1  # the receipt stood in for it
    result = _limit(rid, "1hr", now=created + 60)
    assert result["status"] == "updated" and not result["kept_already_sent"]
    assert _state(rid)["pushed_1hr"] == 0
    ladder.run(created + 60, T + HOUR)
    assert ladder.stages(rid) == ["1hr"]


def test_receipt_never_eats_the_kept_stage(ladder):
    rid = _seed("看牙醫", T)
    _limit(rid, "1hr", now=T - DAY)
    memory.consume_open_stages([rid], now=T - HOUR - 25 * 60)
    assert _state(rid)["pushed_1hr"] == 0


def test_at_time_push_is_the_safety_net_when_the_kept_stage_fails(ladder):
    rid = _seed("看牙醫", T)
    _limit(rid, "1hr", now=T - DAY)
    ladder.fail = {"1hr"}
    ladder.run(T - DAY, T + HOUR)
    assert ladder.stages(rid) == ["now"]


def test_limit_after_the_kept_stage_went_out_completes_the_reminder(ladder):
    rid = _seed("看牙醫", T)
    ladder.run(T - DAY, T - HOUR - 10 * 60)
    assert ladder.stages(rid)[-1] == "1hr"
    result = _limit(rid, "1hr", now=T - HOUR)
    assert result["status"] == "updated" and result["kept_already_sent"]
    assert _state(rid)["status"] == "done"
    ladder.run(T - HOUR + 900, T + HOUR)
    assert ladder.stages(rid)[-1] == "1hr"


def test_switching_and_restoring(ladder):
    rid = _seed("看牙醫", T)
    assert _limit(rid, "1hr", now=T - DAY)["status"] == "updated"
    assert _limit(rid, "now", now=T - DAY)["status"] == "updated"
    assert _limit(rid, "now", now=T - DAY)["status"] == "unchanged"
    ladder.run(T - DAY, T - 3 * HOUR)
    assert _limit(rid, "", now=T - 3 * HOUR)["status"] == "updated"
    assert _limit(rid, "", now=T - 3 * HOUR)["status"] == "unchanged"
    ladder.run(T - 3 * HOUR + 900, T + HOUR)
    assert ladder.stages(rid) == ["2hr", "1hr", "now"]


def test_stage_collected_before_the_limit_is_held_back_at_claim(monkeypatch):
    rid = _seed("看牙醫", T)
    monkeypatch.setattr(time, "time", lambda: float(T - 4 * HOUR))
    items = reminder_push._due_reminder_items()
    assert [(item["reminder_id"], item["stage"]) for item in items] == [(rid, "4hr")]
    _limit(rid, "1hr", now=T - 4 * HOUR)
    assert reminder_push._claim_due_item(items[0]) is None


def test_bad_stored_value_reads_as_no_limit(ladder):
    rid = _seed("看牙醫", T)
    _sql("UPDATE reminders SET push_keep_stage='weekly' WHERE reminder_id=?", (rid,))
    ladder.run(T - 5 * HOUR, T + HOUR)
    assert ladder.stages(rid) == ["4hr", "2hr", "1hr", "now"]


# ── The setter ──────────────────────────────────────────────────────────────


def test_setter_refusals():
    rid = _seed("看牙醫", T)
    state = _state(rid)
    common = dict(expected_action=state["action"], expected_remind_at=state["remind_at"])
    assert memory.set_reminder_push_keep_stage("G1", rid, keep_stage="weekly", **common)["status"] == "invalid"
    assert memory.set_reminder_push_keep_stage(
        "G1", rid, keep_stage="1hr", expected_action="別的事", expected_remind_at=T
    )["status"] == "conflict"
    assert memory.set_reminder_push_keep_stage("G2", rid, keep_stage="1hr", **common)["status"] == "not_found"
    assert _limit(rid, "4hr", now=T - HOUR)["status"] == "past"
    _sql("UPDATE reminders SET time_kind='none' WHERE reminder_id=?", (rid,))
    assert _limit(rid, "1hr", now=T - DAY)["status"] == "no_clock"
    assert _limit(rid, "now", now=T - DAY)["status"] == "updated"
    _sql("UPDATE reminders SET source_kind='calendar_event', source_ref='E1' WHERE reminder_id=?", (rid,))
    assert _limit(rid, "now", now=T - DAY)["status"] == "not_generic"
    done = _seed("繳費", T + 600)
    _sql("UPDATE reminders SET status='done' WHERE reminder_id=?", (done,))
    assert _limit(done, "now", now=T - DAY)["status"] == "terminal"


def test_fold_and_duplicate_cleanup_carry_the_limit():
    primary = _seed("看牙醫", T)
    peer = _seed("看牙醫診所", T, user_id="U2")
    _limit(peer, "1hr", now=T - DAY)
    rows = {row["reminder_id"]: row for row in memory.list_pending_reminders_full("G1", dedupe=False)}
    assert memory.fold_same_event_reminders(rows[primary], [rows[peer]]) == [peer]
    assert _state(primary)["push_keep_stage"] == "1hr"

    other = _seed("開會", T + 3600)
    twin = _seed("上課", T + 3600)
    _limit(twin, "now", now=T - DAY)
    rows = {row["reminder_id"]: row for row in memory.list_pending_reminders_full("G1", dedupe=False)}
    marked = {**rows[twin], "fold_twin": True}  # another event, folded only by the same-minute rule
    assert memory.fold_same_event_reminders(rows[other], [marked]) == [twin]
    assert _state(other)["push_keep_stage"] == ""

    kept = _seed("領包裹", T + 7200)
    twin = _seed("寄包裹", T + 7200)  # the write path refuses an exact twin; make one by hand
    _sql("UPDATE reminders SET action='領包裹', source_text='領包裹' WHERE reminder_id=?", (twin,))
    _limit(twin, "now", now=T - DAY)
    memory.delete_duplicate_pending_reminders("G1")
    survivors = [rid for rid in (kept, twin) if memory.get_reminder_push_state("G1", rid)]
    assert len(survivors) == 1
    assert _state(survivors[0])["push_keep_stage"] == "now"


# ── The message handler ─────────────────────────────────────────────────────


def _archive(message_id: str, text: str, *, user_id: str = "__bot__", reminder_id: int | None = None,
             created_at: int = 1, source_kind: str = "", source_ref: str = "") -> None:
    _sql(
        "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) VALUES ('G1', ?, ?, ?, ?)",
        (message_id, user_id, text, created_at),
    )
    if reminder_id is not None or (source_kind and source_ref):
        assert memory.log_sent_reminder_reference(
            "G1", message_id, reminder_id=reminder_id, source_kind=source_kind, source_ref=source_ref
        )


def _event(text: str, *, quoted: str | None = None) -> MessageEvent:
    message = MagicMock(spec=TextMessageContent)
    message.id = f"incoming-{next(_IDS)}"
    message.text = text
    message.mention = None
    message.quoted_message_id = quoted
    message.quote_token = "quote-token"
    message.type = "text"
    source = MagicMock(spec=GroupSource)
    source.group_id = "G1"
    source.user_id = "U2"
    event = MagicMock(spec=MessageEvent)
    event.message = message
    event.source = source
    event.reply_token = "reply-token"
    event.timestamp = None
    event.delivery_context = SimpleNamespace(is_redelivery=False)
    return event


class _Replies(list):
    cancelled: list[str]


@pytest.fixture
def replies(monkeypatch):
    captured = _Replies()

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("a handled push limit must stop all later routing")

    for name in (
        "_try_handle_quoted_reminder_reschedule",
        "_try_handle_quoted_calendar_correction",
        "_try_handle_creation_followup",
        "_try_one_shot_reply",
        "_auto_capture_text_if_important",
        "_maybe_extract_reminder",
    ):
        monkeypatch.setattr(main, name, must_not_run)
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_reply", lambda _token, text, **kwargs: captured.append((text, kwargs)))
    cancelled: list[str] = []
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda gid: cancelled.append(gid) or [])
    captured.cancelled = cancelled
    return captured


FAR = int(datetime(2099, 9, 30, 14, 0, tzinfo=TW).timestamp())


def _push_text(action: str) -> str:
    return f"⏰ 提醒（1 小時後）\n2099-09-30 14:00 {action}"


def test_quoted_push_keeps_one_stage_and_binds_the_receipt(replies):
    rid = _seed("看牙醫", FAR)
    _archive("push-1", _push_text("看牙醫"), reminder_id=rid)
    main._handle_text_message(_event("只要一小時前提醒", quoted="push-1"), "G1")
    (text, kwargs), = replies
    assert text.splitlines()[0] == "已改成只在 1 小時前提醒"
    assert isinstance(text, line_mentions.AllowsMemberMention)  # a receipt keeps its @s (2026-11-13)
    assert "時間：2099-09-30 14:00" in text and "看牙醫" in text
    assert kwargs["primary_reminder_ref"] == {"reminder_id": rid}
    assert kwargs["allow_push_fallback"] is True
    assert _state(rid)["push_keep_stage"] == "1hr"
    assert replies.cancelled == ["G1"]
    assert rr.parse_quoted_reminder(text).status == rr.QUOTED_ONE


def test_unbound_creation_receipt_and_change_wording(replies):
    rid = _seed("看牙醫", FAR)
    _archive("receipt-1", "已新增提醒\n時間：2099-09-30 14:00\n事項：看牙醫")
    main._handle_text_message(_event("改成只提醒一次", quoted="receipt-1"), "G1")
    (text, _kwargs), = replies
    assert text.startswith("已改成只在時間到時提醒一次")
    assert _state(rid)["push_keep_stage"] == "now"


def test_quoted_refusals_change_nothing(replies):
    rid = _seed("看牙醫", FAR)
    _archive("multi-1", "已新增 2 筆提醒\n時間：2099-09-30 14:00\n事項：看牙醫\n時間：2099-10-01 09:00\n事項：繳費")
    _archive("cal-1", _push_text("家長會"), source_kind="calendar_event", source_ref="E9")
    _archive("push-1", _push_text("看牙醫"), reminder_id=rid)
    main._handle_text_message(_event("只要提醒一次", quoted="multi-1"), "G1")
    main._handle_text_message(_event("只要提醒一次", quoted="cal-1"), "G1")
    main._handle_text_message(_event("前一天提醒就好", quoted="push-1"), "G1")
    texts = [text for text, _ in replies]
    assert texts[0].startswith("尚未更新提醒：引用的訊息裡有多筆提醒")
    assert texts[1].startswith("尚未更新提醒：這則是行事曆活動的提醒")
    assert texts[2].startswith("尚未更新提醒：目前只能留")
    assert all(kwargs["allow_push_fallback"] is False for _, kwargs in replies)
    assert _state(rid)["push_keep_stage"] == ""
    assert replies.cancelled == []


def test_quoting_a_family_message_is_left_to_creation():
    _archive("family-1", "明天兩點看牙醫", user_id="U3")
    assert main._try_handle_reminder_push_limit(_event("只要提醒一次", quoted="family-1"), "G1", "只要提醒一次") is False


def test_unquoted_gets_one_howto_line_only_when_relevant(replies, monkeypatch):
    rid = _seed("看牙醫", FAR)
    assert main._try_handle_reminder_push_limit(_event("提醒一次就好"), "G1", "提醒一次就好") is False
    main._handle_text_message(_event("咪寶 提醒一次就好"), "G1")
    _archive("push-old", _push_text("看牙醫"), reminder_id=rid, created_at=int(time.time()) - 1801)
    assert main._try_handle_reminder_push_limit(_event("提醒太多次了"), "G1", "提醒太多次了") is False
    _archive("push-recent", "@媽媽\n" + _push_text("看牙醫"), reminder_id=rid, created_at=int(time.time()))
    main._handle_text_message(_event("提醒太多次了"), "G1")
    main._handle_text_message(_event("照原本提醒就好"), "G1")
    texts = [text for text, _ in replies]
    assert texts == [rpl.HOWTO_LIMIT, rpl.HOWTO_LIMIT, rpl.HOWTO_RESTORE]
    assert replies.cancelled == []
    assert _state(rid)["push_keep_stage"] == ""


def test_muted_bot_changes_but_does_not_reply(replies, monkeypatch):
    monkeypatch.setattr(main.settings, "bot_muted", True)
    rid = _seed("看牙醫", FAR)
    _archive("push-1", _push_text("看牙醫"), reminder_id=rid)
    main._handle_text_message(_event("提醒一次就好", quoted="push-1"), "G1")
    assert replies == []
    assert _state(rid)["push_keep_stage"] == "now"


def test_receipt_with_calendar_note_still_names_its_reminder():
    text = rpl.receipt(
        "updated", "1hr", remind_at=FAR, action="看牙醫", calendar_title="看牙醫"
    )
    assert text.endswith("行事曆的「看牙醫」照原本的規則提醒，不受影響。")
    parsed = rr.parse_quoted_reminder(text)
    assert parsed.status == rr.QUOTED_ONE and parsed.remind_at == FAR
