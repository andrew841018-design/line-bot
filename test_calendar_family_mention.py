"""Stage 2 of the reminder-path @all work: the calendar model's 全家 (Andrew 2026-11-13/14, D3, D4).

- D3: a 全家, 大家, all… that the calendar model writes among an event's participants counts only with
  the family's own words behind it: an @ that calls everyone, or a title and a message that both say
  全家.  Without them it is dropped, and neither the event's offset pushes nor its mirror ring
  everyone.  The medical 全家 the program works out itself still counts.
- D4: a 全家 in a model's title rings everyone only when the family's message says 全家 too.  The
  event's own message decides when there is one; a burst event, which has none, keeps what its capture
  decided; an event that never had a message keeps its title (11/07).
- 咪寶's words never stand for the family's: a burst is read without any quoted message, and the
  missed-reminder repair builds nothing from a message of 咪寶's.

Members are conftest's synthetic ones, every date is in the future, and nothing reaches the network.

Plan and reviews: ops/state/line_bot_reminder_extract_mention_20261113/.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import burst_filter
import calendar_db
import calendar_extractor
import calendar_regex
import event_reminder
import line_mentions
import main
import memory
from quote_context import missing_block, original_block, recent_block, with_current_reply
from test_calendar_last_tier import TIER, answer, inline_spawn, tier  # noqa: F401  tier is a fixture

TW = ZoneInfo("Asia/Taipei")
GROUP = "G_CAL_FAMILY"


def _today():
    return datetime.now(TW).date()


def _future(days: int = 5) -> str:
    return (_today() + timedelta(days=days)).isoformat()


def _model_event(title, participants, *, day=None, event_type="family_gathering", time="18:00") -> dict:
    return {
        "has_event": True, "is_cancellation": False, "title": title, "event_type": event_type,
        "date": day or _future(), "time": time, "location": None,
        "participants": list(participants), "cancel_target_keyword": None,
    }


def _events() -> list[dict]:
    with memory._conn() as c:
        rows = c.execute(
            "SELECT event_id, title, participants, COALESCE(source_msg_id, '') FROM events "
            "WHERE group_id=? AND status='active' ORDER BY created_at",
            (GROUP,),
        ).fetchall()
    return [
        {"event_id": row[0], "title": row[1], "participants": json.loads(row[2] or "[]"), "source_msg_id": row[3]}
        for row in rows
    ]


def _mirror(event_id: str) -> list[str]:
    with memory._conn() as c:
        (row,) = c.execute(
            "SELECT mention_aliases FROM reminders WHERE group_id=? AND source_kind=? AND source_ref=? "
            "AND status='pending'",
            (GROUP, calendar_db.EVENT_REMINDER_SOURCE_KIND, event_id),
        ).fetchall()
    return json.loads(row[0] or "[]")


def _offset_rings_everyone(event_id: str) -> bool:
    event = calendar_db.get_active_event_by_id(GROUP, event_id)
    return any(target.kind == "all" for target in line_mentions.event_mention_targets(event))


# ── one reading of a participant for everyone ─────────────────────────────────────────────────

EVERYONE = ["全家", "大家", "all", "ALL", "@all", " 全家 ", "全家(主辦)", "全家（主辦）", "all（全家）", "Everyone（大家）", "所有人"]
NOT_EVERYONE = ["全家人", "大家長", "＠全家", "ＡＬＬ", "所有人🙂"]


@pytest.mark.parametrize("name", EVERYONE + NOT_EVERYONE)
def test_a_participant_is_read_the_way_the_mirror_reads_it(name):
    mirror = line_mentions.is_all_participants(calendar_db._event_participants({"participants": [name]}))
    offset = line_mentions.is_all_participants([name])  # the offset push cuts no bracket

    assert line_mentions.is_whole_family_participant(name) == mirror == (name in EVERYONE)
    assert mirror or not offset  # the offset push reads fewer as everyone, never more


@pytest.mark.parametrize("name", EVERYONE + NOT_EVERYONE)
def test_a_models_participant_for_everyone_needs_the_familys_words(name):
    out = calendar_extractor._normalize(_model_event("聚餐", [name]), family_text="週六聚餐")

    assert out["participants"] == ([] if name in EVERYONE else [name])


def test_only_the_participant_for_everyone_goes():
    out = calendar_extractor._normalize(_model_event("聚餐", ["測試成員甲", "全家"]), family_text="週六聚餐")

    assert out["participants"] == ["測試成員甲"]


@pytest.mark.parametrize(
    ("title", "text", "kept", "title_backed"),
    [
        ("聚餐", "@all 週六聚餐", ["全家"], False),  # an @ that calls everyone
        ("聚餐", "@全家 週六聚餐", ["全家"], False),  # every way of writing it (seat 1 F3, seat 4 I5)
        ("聚餐", "@All 週六聚餐", ["全家"], False),  # LINE's own mention menu
        ("聚餐", "＠全家 週六聚餐", ["全家"], False),
        ("全家聚餐", "週六全家聚餐", ["全家"], True),  # a title and a message that both say 全家
        ("全家聚餐", "週六聚餐", [], False),  # the title alone
        ("看電影", "週六全家一起看電影", [], False),  # the message alone (seat 1 I4): it no longer rings
    ],
)
def test_what_backs_a_models_participant_for_everyone(title, text, kept, title_backed):
    out = calendar_extractor._normalize(_model_event(title, ["全家"]), family_text=text)

    assert out["participants"] == kept
    assert out["_title_names_family"] is title_backed


def test_the_rules_alone_are_read_as_before():
    # Only the model's answer is checked: _normalize without the family's words leaves a regex event alone.
    out = calendar_extractor._normalize(_model_event("聚餐", ["全家"]))

    assert out["participants"] == ["全家"] and "_title_names_family" not in out


# ── through the capture ──────────────────────────────────────────────────────────────────────


def test_a_models_participant_for_everyone_without_the_familys_words_rings_nobody(tier, monkeypatch):
    text = "週六下午6點聚餐"
    fake = tier(answer(_model_event("聚餐", ["全家"])))
    inline_spawn(monkeypatch)
    memory.log_raw_message(GROUP, "m-k4", "U_TEST_A", text, index_for_recall=False)

    main._maybe_capture_calendar_event(GROUP, text, "U_TEST_A", "m-k4")

    assert fake.generated == [TIER]
    (event,) = _events()
    assert event["participants"] == []
    assert _mirror(event["event_id"]) == []
    assert not _offset_rings_everyone(event["event_id"])
    spec = event_reminder.build_reminder_message_spec(calendar_db.get_active_event_by_id(GROUP, event["event_id"]), 7)
    assert spec is not None and "all" not in json.dumps(spec.get("message") or {})


@pytest.mark.parametrize(("text", "mirror"), [("週六下午6點聚餐", []), ("週六下午6點全家聚餐", ["全家"])])
def test_a_burst_events_title_needs_the_familys_words(tier, monkeypatch, text, mirror):
    # A burst event has no message of its own (message_id ""): its mirror keeps what the capture decided.
    tier(answer(_model_event("全家聚餐", [])))
    inline_spawn(monkeypatch)

    main._burst_capture_calendar_event(GROUP, text, ["m-b1"])

    (event,) = _events()
    assert event["source_msg_id"] == ""
    assert _mirror(event["event_id"]) == mirror


def test_the_medical_family_the_program_works_out_still_counts(tier, monkeypatch):
    text = "明天早上10點全家打疫苗"
    tier(answer(_model_event("打疫苗", ["大家"], day=_future(1), event_type="medical", time="10:00")))
    inline_spawn(monkeypatch)
    memory.log_raw_message(GROUP, "m-k7", "U_TEST_A", text, index_for_recall=False)

    main._maybe_capture_calendar_event(GROUP, text, "U_TEST_A", "m-k7")

    (event,) = _events()
    assert event["participants"] == ["全家"]  # the model's 大家 went; the program's 全家 came after it
    assert _offset_rings_everyone(event["event_id"])


# ── the event's own message, and what the capture decided ─────────────────────────────────────


def _insert(title: str, *, source: str = "", raw: str | None = None, title_names_family: bool | None = None) -> str:
    if raw is not None:
        memory.log_raw_message(GROUP, source, "U_TEST_A", raw, index_for_recall=False)
    event_id, outcome = calendar_db.insert_event_with_outcome(
        group_id=GROUP, title=title, event_date=_future(), event_time="18:00", location=None,
        participants=[], event_type="family_gathering", source_msg_id=source,
        title_names_family=title_names_family,
    )
    assert outcome == "created"
    return event_id


def _resync(event_id: str, stale: list[str]) -> list[str]:
    """The mirror after a sync, from aliases an older run left on it."""
    with memory._conn() as c:
        c.execute(
            "UPDATE reminders SET mention_aliases=? WHERE source_kind=? AND source_ref=?",
            (json.dumps(stale, ensure_ascii=False), calendar_db.EVENT_REMINDER_SOURCE_KIND, event_id),
        )
    assert calendar_db.synchronize_pending_event_reminder_mirror(calendar_db.get_active_event_by_id(GROUP, event_id))
    return _mirror(event_id)


@pytest.mark.parametrize(("raw", "mirror"), [("週六聚餐", []), ("週六全家聚餐", ["全家"])])
def test_a_mirror_reads_the_events_own_message(raw, mirror):
    event_id = _insert("全家聚餐", source="m-k8", raw=raw)

    assert _mirror(event_id) == mirror
    assert _resync(event_id, [] if mirror else ["全家"]) == mirror


def test_an_event_that_never_had_a_message_keeps_its_title():
    # 「Never had one」: a message pruned away later reads the same way, a known leftover (PLAN_STAGE2_v2 §6.5).
    event_id = _insert("全家聚餐")

    assert _mirror(event_id) == ["全家"]
    assert _resync(event_id, []) == ["全家"]


def test_an_event_with_no_message_and_no_family_title_rings_nobody():
    # Seat 4 N21: with no message and no capture's decision, only a title that says 全家 can ring
    # everyone; 「看牙醫」 does not, now or after a sync.
    event_id = _insert("看牙醫")

    assert _mirror(event_id) == []
    assert _resync(event_id, ["全家"]) == []


def test_the_events_own_message_wins_over_what_the_capture_decided():
    # Seat 2 I6: the capture's decision is only for an event without a message.
    event_id = _insert("全家聚餐", source="m-k8b", raw="週六聚餐", title_names_family=True)

    assert _mirror(event_id) == []


# ── a rule-built event's title comes from the family's words (seat 1 r1c, PLAN_STAGE2_v1 §2.5) ──

_BEFORE = ["", "去", "到", "去一趟", "回去", "輪到", "確保", "保", "我們", "帶", "跟", "交通安", "在"]
_AFTER = [
    "聚餐", "買牛奶", "取貨", "門口集合", "人的聚會", "長會", "裡檢查", "便利商店取件", "一起吃飯",
    "團圓", "福拍照", "到齊吃飯", "用餐", "去看電影", "診所回診",
]


def _grid_texts() -> list[str]:
    day = _today() + timedelta(days=9)
    times = ["明天下午2點", "後天18:00", "週六晚上6點", f"{day.month}/{day.day} 10:00"]
    texts = [f"{when}{before}全家{after}" for when in times for before in _BEFORE for after in _AFTER]
    md = f"{day.month}/{day.day} 18:00"
    return texts + [
        f"{md} 到全家門口集合",
        f"{md} 去全家便利商店後全家聚餐",
        f"{md} 全家去全家拿包裹",
        f"{md} 帶全家人去吃飯",
        f"{md} 交通安全家長會",
        f"{md} 去 全家 聚餐",
        "下週六晚上6點去全家",
    ]


def test_a_rule_built_titles_family_is_in_the_message_too():
    # The rules' titles are cut from the message, so D4 changes nothing for them: a title that names the
    # family never comes from a message that does not.
    today = _today()
    wrong, titles = [], 0
    for text in _grid_texts():
        events = [calendar_regex.extract_regex_only(text, today)]
        events += calendar_regex.extract_many_regex_only(text, today, require_time=False)
        for event in events:
            title = event.get("title")
            if not title:
                continue
            titles += 1
            if line_mentions.names_the_family(title) and not line_mentions.names_the_family(text):
                wrong.append((text, title))

    assert titles > 500  # the grid really reaches the rules
    assert wrong == []


# ── a burst is read without its quoted messages ───────────────────────────────────────────────


def _burst(*texts: str) -> str:
    return burst_filter._combine([(f"m{index}", text, "U_TEST_A", 0.0) for index, text in enumerate(texts, 1)])


def _bot_push(what: str) -> str:
    return original_block(f"@all\n全家 {what}\n時間：{_future()} 18:00", "我 (bot)")


def test_a_burst_that_quotes_a_message_writes_no_event_today(tier, monkeypatch):
    # Today three fail-closed gates stop a burst that carries a quote before any event is written: the
    # shell reads as prompt text (reminder_intent._INTERNAL_PROMPT_MARKERS) to the write gate, the
    # noncommittal check and the candidate check.  So 咪寶's quoted push cannot back an event now; the
    # tests below lift those gates and check the second line, calendar_extractor.family_words.
    tier(answer(_model_event("全家聚餐", ["全家"])))
    inline_spawn(monkeypatch)

    main._burst_capture_calendar_event(GROUP, _burst(with_current_reply(_bot_push("聚餐"), "週六全家聚餐，要帶什麼")), ["m-b1"])

    assert _events() == []


@pytest.fixture
def quote_gates_lifted(monkeypatch):
    """The three gates that stop a quoted burst today, lifted: as if one of them ever let it through."""
    monkeypatch.setattr(main, "_should_suppress_reminder_write", lambda *_a, **_k: False)
    monkeypatch.setattr(main.reminder_intent, "is_obvious_noncommittal_source", lambda *_a, **_k: False)
    monkeypatch.setattr(main.reminder_intent, "should_reject_reminder_candidate", lambda *_a, **_k: False)


def _model_events() -> list[dict]:
    # The rules may file the quoted push as an event of its own: only the model's event is looked at.
    return [event for event in _events() if event["title"] == "全家聚餐" or "健康檢查" in event["title"]]


@pytest.mark.parametrize(
    ("quoted", "reply", "participants"),
    [
        (_bot_push("聚餐"), "要帶什麼", []),  # 咪寶's own push for everyone, quoted back
        (original_block("週六全家聚餐", "測試成員乙"), "要帶什麼", []),  # a family message: it made its own event
        (_bot_push("聚餐"), "週六全家聚餐，要帶什麼", ["全家"]),  # the reply itself says it
    ],
    ids=["mibao-push", "family-message", "the-reply-says-it"],
)
def test_a_quoted_message_never_backs_a_burst_event(tier, monkeypatch, quote_gates_lifted, quoted, reply, participants):
    tier(answer(_model_event("全家聚餐", ["全家"])))
    inline_spawn(monkeypatch)

    main._burst_capture_calendar_event(GROUP, _burst(with_current_reply(quoted, reply)), ["m-b1"])

    (event,) = _model_events()
    assert event["participants"] == participants
    assert _mirror(event["event_id"]) == participants
    assert _offset_rings_everyone(event["event_id"]) is bool(participants)


def test_a_quoted_push_never_names_the_patient(tier, monkeypatch, quote_gates_lifted):
    tier(answer(_model_event("做健康檢查", [], event_type="medical")))
    inline_spawn(monkeypatch)

    main._burst_capture_calendar_event(GROUP, _burst(with_current_reply(_bot_push("做健康檢查"), "明天幾點出門")), ["m-b1"])

    (event,) = _model_events()
    assert event["participants"] == []
    assert not _offset_rings_everyone(event["event_id"])


def test_a_quoted_block_cut_short_is_left_out_to_the_end(tier, quote_gates_lifted):
    cut = _burst(with_current_reply(_bot_push("聚餐"), "要帶什麼")).split("--- 原始訊息 結束 ---")[0]
    assert "--- 原始訊息 開始 ---" in cut
    tier(answer(_model_event("全家聚餐", ["全家"])))

    words = calendar_extractor.family_words(cut)
    data = calendar_extractor.extract(cut)

    assert "全家" not in words and "@all" not in words
    assert data["has_event"] and data["participants"] == [] and data["_title_names_family"] is False


_SHELLS = {
    "original": lambda reply: with_current_reply(original_block("好", "我 (bot)"), reply),
    "recent": lambda reply: with_current_reply(recent_block("好"), reply),
    "missing": lambda reply: with_current_reply(missing_block(), reply),
    "burst": lambda reply: _burst(with_current_reply(original_block("好"), "好"), reply),
}


@pytest.mark.parametrize("shell", list(_SHELLS))
def test_the_quote_shell_says_nothing_about_the_family(shell):
    # If 大家 or 全家 is ever written into a shell's own lines, this says so (the quote rule itself goes
    # out whole, so a word written there never gets this far).
    words = calendar_extractor.family_words(_SHELLS[shell]("好"))

    assert line_mentions.whole_family_mentions(words) == []
    assert not line_mentions.names_the_family(words)
    # With a medical reply in the shell, nobody in the shell's own lines becomes the patient (seat 4 N7).
    assert main._infer_medical_actor(calendar_extractor.family_words(_SHELLS[shell]("明天看牙醫")), "") is None


# ── the missed-reminder repair ────────────────────────────────────────────────────────────────


def _mibao_says(shape: str) -> str:
    """A message of 咪寶's with a date, a clock and a family event: the rules can read each one."""
    day = _today() + timedelta(days=3)
    return {
        "rules": f"@all\n{day.month}/{day.day} 18:00 全家聚餐",
        # the shapes 咪寶 really sends (seat 4 N13): a due push and a receipt
        "push": f"@all\n⏰ 提醒（**現在 / 即將到時**）\n{day.isoformat()} 18:00 全家 聚餐",
        "receipt": f"已新增提醒\n時間：{day.isoformat()} 18:00\n事項：全家 聚餐",
    }[shape]


@pytest.mark.parametrize("shape", ["rules", "push", "receipt"])
def test_a_missed_reminder_repair_on_a_message_of_mibaos_builds_nothing(monkeypatch, shape):
    # Before stage 2 the repair built an event, and its mirror, from 咪寶's own words.
    memory.log_raw_message(GROUP, "m-bot", "__bot__", _mibao_says(shape), index_for_recall=False)
    sent: list[str] = []
    monkeypatch.setattr(main, "_reply", lambda _token, text, **_kw: sent.append(text) or True)
    event = SimpleNamespace(reply_token="TOKEN-REPAIR", message=SimpleNamespace(id="m-ask", quoted_message_id="m-bot"))

    assert main._try_handle_missed_reminder_repair(event, GROUP, "這個漏掉了") is True

    assert sent == ["引用的是咪寶自己的訊息，先沒有建立提醒。請直接回覆家人原本那則訊息，再說「這個漏掉了」。"]
    assert _events() == []
    with memory._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM reminders WHERE group_id=?", (GROUP,)).fetchone()[0] == 0
