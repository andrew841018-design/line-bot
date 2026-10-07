"""2026-09-28 Andrew：同一事件被反覆提到時，不要重複新增提醒，
而是把新的細節（含後來才補上的時間）併進同一筆。

原則：能照顧才合併——只有保留的那一筆事後仍會在新說法要求的時間提醒，才不新增；
否則照舊新增（寧可重複，不可漏掉）。全部是虛構內容；日期取今天往後，避免日後過期。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import main
import memory
import reminder_intent as ri

TZ = ZoneInfo("Asia/Taipei")
G = "G_SAME_EVENT"


def _day(offset: int = 40):
    return (datetime.now(TZ) + timedelta(days=offset)).date()


def _at(day, hour: int, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).timestamp())


def _md(day) -> str:
    return f"{day.month}/{day.day}"


def _add(day, hour, action, source, kind, minute=0, user="U1", mentions=None):
    return memory.add_reminder_with_outcome(
        G, user, action, _at(day, hour, minute), source_text=source,
        mention_aliases=mentions or [], time_kind=kind,
    )


def _pending_on(day) -> list[dict]:
    start = _at(day, 0)
    return [
        row for row in memory.list_pending_reminders(G, within_seconds=400 * 86400)
        if start <= int(row["remind_at"]) < start + 86400
    ]


def _clock(row) -> str:
    return datetime.fromtimestamp(int(row["remind_at"]), TZ).strftime("%H:%M")


def _details(row) -> str:
    return "\n".join(f"{f['action']}|{f['text']}" for f in row["merged_details"])


def _set(rid, **columns):
    assignments = ", ".join(f"{name}=?" for name in columns)
    with memory._conn() as c:
        c.execute(f"UPDATE reminders SET {assignments} WHERE reminder_id=?", (*columns.values(), rid))


def _claim(rid):
    with memory._conn() as c:
        c.execute(
            "INSERT INTO reminder_delivery_claims(group_id, delivery_kind, subject_ref, occurrence, "
            "source_kind, source_ref, transport, state, claim_token, retry_key, "
            "fallback_retry_key, claimed_at) VALUES (?, 'natural', ?, 'now', '', '', 'push', "
            "'sending', 'T', 'R', '', ?)",
            (G, str(rid), int(datetime.now(TZ).timestamp())),
        )


# ── the list Andrew saw, replayed with fictional content ──────────────────────

def test_three_mentions_of_one_event_leave_one_reminder_at_the_explicit_time():
    d = _day()
    first, created = _add(d, 14, "參加社區健檢", f"社區健檢：{_md(d)} 下午兩點到四點，在合成路1號3樓；攜帶身分證", "clock")
    second, vague = _add(d, 12, "回台北參加社區健檢", f"{_md(d)} 要回台北參加社區健檢", "none")
    third, same_time = _add(d, 14, "去社區健檢做檢查", f"{_md(d)} 14:00 去社區健檢做檢查", "clock")

    rows = _pending_on(d)
    assert (created, vague, same_time) == ("created", "merged", "merged")
    assert first == second == third and len(rows) == 1
    assert _clock(rows[0]) == "14:00" and rows[0]["action"] == "參加社區健檢"
    assert "回台北" in _details(rows[0]) and "去社區健檢做檢查" in _details(rows[0])
    assert "合成路1號3樓" in rows[0]["source_text"]


def test_a_later_mention_fills_in_the_missing_time():
    d = _day()
    rid, _ = _add(d, 12, "回台北參加社區健檢", f"{_md(d)} 要回台北參加社區健檢", "none")
    same, outcome = _add(d, 14, "回台北參加社區健檢", f"{_md(d)} 14:00 回台北參加社區健檢，在合成路1號3樓", "clock")

    rows = _pending_on(d)
    assert outcome == "merged" and same == rid and len(rows) == 1
    assert _clock(rows[0]) == "14:00" and rows[0]["time_kind"] == "clock"
    assert rows[0]["source_text"] == f"{_md(d)} 要回台北參加社區健檢"  # identity unchanged
    assert "合成路1號3樓" in _details(rows[0])


def test_the_time_is_filled_in_after_a_weekly_notice_went_out():
    d = _day(10)
    rid, _ = _add(d, 12, "社區健檢", f"{_md(d)} 社區健檢", "none")
    assert memory.mark_reminder_pushed(rid, "weekly")
    same, outcome = _add(d, 14, "社區健檢", f"{_md(d)} 14:00 社區健檢", "clock")

    assert (same, outcome) == (rid, "merged") and _clock(_pending_on(d)[0]) == "14:00"


def test_a_later_vague_mention_keeps_the_explicit_time():
    d = _day()
    rid, _ = _add(d, 14, "參加社區健檢", f"社區健檢：{_md(d)} 14:00，在合成路1號3樓", "clock")
    same, outcome = _add(d, 12, "回台北參加社區健檢", f"{_md(d)} 要回台北參加社區健檢", "none")

    rows = _pending_on(d)
    assert (same, outcome) == (rid, "merged") and len(rows) == 1 and _clock(rows[0]) == "14:00"


def test_the_list_shows_absorbed_details_and_the_same_day_label():
    d = _day()
    _add(d, 14, "參加社區健檢", f"社區健檢：{_md(d)} 14:00", "clock")
    _add(d, 12, "回台北參加社區健檢", f"{_md(d)} 要回台北參加社區健檢，攜帶身分證", "none")
    lines = main._format_reminder_report_item(_pending_on(d)[0], 1)
    assert any("攜帶身分證" in line for line in lines), lines

    item = {
        "action": f"成員甲 {_md(d)} 10:30 看診（當天提醒）",
        "remind_at": _at(d, 9),
        "source_text": f"成員甲 {_md(d)} 看診，麻煩前一天以及當天提醒，謝謝！",
        "mention_aliases": [],
    }
    lines = main._format_reminder_report_item(item, 1)
    assert any("當天提醒" in line and "麻煩" not in line for line in lines), lines


def test_the_chat_path_records_whether_the_time_was_said():
    tomorrow = _day(1)
    main._maybe_extract_reminder("咪寶明天提醒我領米", G, "U1", "M-chat-1")
    first = _pending_on(tomorrow)
    assert len(first) == 1 and first[0]["time_kind"] == "none"

    main._maybe_extract_reminder("咪寶明天下午3點提醒我領米", G, "U1", "M-chat-2")
    rows = _pending_on(tomorrow)
    assert len(rows) == 1 and rows[0]["reminder_id"] == first[0]["reminder_id"]
    assert _clock(rows[0]) == "15:00" and rows[0]["time_kind"] == "clock"


def test_the_quota_drain_path_merges_too(monkeypatch):
    import gemini_client

    d = _day(3)
    rid, _ = _add(d, 12, "開會", f"{_md(d)} 開會", "none")
    memory.enqueue_pending_reminder(G, "U1", f"{_md(d)} 晚上8點開會", "M-drain")
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: {
        "action": "開會", "year": d.year, "month": d.month, "day": d.day, "hour": 20, "minute": 0,
    })
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    main._drain_pending_reminders(G)

    rows = _pending_on(d)
    assert len(rows) == 1 and rows[0]["reminder_id"] == rid and _clock(rows[0]) == "20:00"
    with memory._conn() as c:
        confirmation = c.execute(
            "SELECT text FROM reminder_confirmation_outbox WHERE group_id=?", (G,)
        ).fetchone()
    assert confirmation is None  # late extraction: merged silently (2026-10-04)


# ── a confirmed time must never be moved later (R4-1) ─────────────────────────

def test_repeating_the_same_action_with_its_time_fixes_that_time():
    d = _day()
    rid, _ = _add(d, 12, "社區健檢", f"{_md(d)} 社區健檢", "none")
    same, outcome = _add(d, 12, "社區健檢", f"{_md(d)} 12:00 社區健檢", "clock")
    assert (same, outcome) == (rid, "duplicate")
    assert _pending_on(d)[0]["time_kind"] == "clock"

    other, inserted = _add(d, 14, "社區健檢報到", f"{_md(d)} 14:00 社區健檢報到", "clock")
    assert inserted == "created" and other != rid
    assert sorted(_clock(r) for r in _pending_on(d)) == ["12:00", "14:00"]


def test_a_weak_mention_upgraded_by_a_strong_one_keeps_the_stated_clock():
    d = _day()
    weak, _ = _add(d, 9, "8/8", f"{_md(d)}早上", "daypart:早上")
    strong, merged = _add(d, 9, "上皮拉提斯課", f"{_md(d)} 九點上皮拉提斯課", "clock")
    assert (strong, merged) == (weak, "merged")
    assert _pending_on(d)[0]["time_kind"] == "clock"

    other, inserted = _add(d, 11, "上皮拉提斯課", f"{_md(d)} 11:00 上皮拉提斯課", "clock")
    assert inserted == "created" and other != weak


# ── honor-or-insert: when the kept row cannot take the new time, insert ──────

@pytest.mark.parametrize("stage", ["pushed_4hr", "pushed_2hr", "pushed_1hr", "pushed_now"])
def test_a_same_day_notice_already_sent_blocks_moving_the_time(stage):
    d = _day()
    rid, _ = _add(d, 12, "社區健檢", f"{_md(d)} 社區健檢", "none")
    _set(rid, **{stage: 1})
    other, outcome = _add(d, 14, "社區健檢", f"{_md(d)} 14:00 社區健檢", "clock")
    assert outcome == "created" and other != rid and len(_pending_on(d)) == 2


def test_a_reminder_being_delivered_is_not_rewritten():
    d = _day()
    rid, _ = _add(d, 14, "參加社區健檢", f"{_md(d)} 14:00 參加社區健檢", "clock")
    _claim(rid)
    same, outcome = _add(d, 12, "回台北參加社區健檢", f"{_md(d)} 要回台北參加社區健檢", "none")
    assert (same, outcome) == (rid, "duplicate") and _pending_on(d)[0]["merged_details"] == []

    d2 = _day(41)
    moving, _ = _add(d2, 12, "社區健檢", f"{_md(d2)} 社區健檢", "none")
    _claim(moving)
    other, inserted = _add(d2, 14, "社區健檢", f"{_md(d2)} 14:00 社區健檢", "clock")
    assert inserted == "created" and other != moving


def test_a_legacy_row_is_not_moved_to_a_new_time():
    d = _day()
    rid, _ = _add(d, 12, "社區健檢", f"{_md(d)} 社區健檢", "none")
    _set(rid, time_kind=None)
    other, outcome = _add(d, 14, "社區健檢", f"{_md(d)} 14:00 社區健檢", "clock")
    assert outcome == "created" and other != rid
    assert sorted(_clock(r) for r in _pending_on(d)) == ["12:00", "14:00"]


def test_a_legacy_row_still_absorbs_a_mention_without_a_time():
    d = _day()
    rid, _ = _add(d, 12, "社區健檢", f"{_md(d)} 社區健檢", "none")
    _set(rid, time_kind=None)
    same, outcome = _add(d, 12, "社區健檢集合", f"{_md(d)} 社區健檢集合，攜帶身分證", "none")
    row = _pending_on(d)[0]
    assert (same, outcome) == (rid, "merged") and len(_pending_on(d)) == 1
    assert _clock(row) == "12:00" and row["time_kind"] is None


def test_an_unknown_time_kind_behaves_as_before():
    d = _day()
    rid, _ = memory.add_reminder_with_outcome(G, "U1", "社區健檢", _at(d, 12), source_text="a")
    other, outcome = memory.add_reminder_with_outcome(G, "U2", "參加社區健檢", _at(d, 14), source_text="b")
    assert outcome == "created" and other != rid


# ── different events stay separate, through the write path ──────────────────

@pytest.mark.parametrize(
    ("first", "second"),
    [
        (("流感疫苗第一劑", "none"), ("流感疫苗第二劑", "none")),
        (("台北國泰醫院回診", "none"), ("馬偕醫院回診", "none")),
        (("畢業典禮", "clock"), ("畢業典禮彩排", "none")),
        (("王小明結婚喜宴", "clock"), ("李大華結婚喜宴", "none")),
        (("聚餐", "clock"), ("聚餐前買蛋糕", "none")),
        (("陪媽媽看牙醫", "none"), ("媽媽陪爸爸看牙醫", "none")),
        (("帶狗去打預防針", "clock"), ("帶貓去打預防針", "none")),
    ],
)
def test_different_events_on_the_same_day_are_not_merged(first, second):
    d = _day()
    rid, _ = _add(d, 14 if first[1] == "clock" else 12, first[0], f"{_md(d)} {first[0]}", first[1])
    other, outcome = _add(d, 12, second[0], f"{_md(d)} {second[0]}", second[1])
    assert outcome == "created" and other != rid


def test_an_absorbed_dose_keeps_the_other_dose_out():
    d = _day()
    # The kept title names no dose; the first dose only arrives as an absorbed
    # mention, and must still keep the second dose out.
    rid, _ = _add(d, 12, "打流感疫苗", f"{_md(d)} 打流感疫苗", "none")
    _, absorbed = _add(d, 12, "流感疫苗第一劑", f"{_md(d)} 流感疫苗第一劑", "none")
    assert absorbed == "merged"
    other, outcome = _add(d, 12, "流感疫苗第二劑", f"{_md(d)} 流感疫苗第二劑", "none")
    assert outcome == "created" and other != rid


def test_people_named_by_mention_must_agree():
    d = _day()
    rid, _ = _add(d, 12, "繳費", f"{_md(d)} 繳費", "none", mentions=["媽媽"])
    other, outcome = _add(d, 20, "繳費", f"{_md(d)} 晚上8點繳費", "clock", mentions=["爸爸"])
    assert outcome == "created" and other != rid


def _insert(day, hour, action, kind):
    with memory._conn() as c:
        c.execute(
            "INSERT INTO reminders(group_id, user_id, action, remind_at, created_at, status, "
            "source_kind, source_ref, source_text, mention_aliases, time_kind) "
            "VALUES (?, 'U1', ?, ?, 0, 'pending', '', '', ?, '[]', ?)",
            (G, action, _at(day, hour), action, kind),
        )


def test_two_possible_reminders_mean_no_merge():
    d = _day()
    # Inserted directly: through the write path the second would already have
    # been folded into the first.
    _insert(d, 12, "參加社區健檢", "none")
    _insert(d, 16, "去社區健檢做檢查", "clock")
    _, outcome = _add(d, 12, "回台北參加社區健檢做檢查", f"{_md(d)} 回台北參加社區健檢做檢查", "none")
    assert outcome == "created" and len(_pending_on(d)) == 3


def test_other_days_and_offset_sets_are_left_alone():
    d = _day()
    rid, _ = _add(d, 14, "社區健檢", f"{_md(d)} 14:00 社區健檢", "clock")
    other_day, outcome = _add(_day(41), 14, "社區健檢", "隔天社區健檢", "clock")
    assert outcome == "created" and other_day != rid

    with memory._conn() as c:
        c.execute(
            "INSERT INTO reminders(group_id, user_id, action, remind_at, created_at, status, "
            "source_kind, source_ref, source_text, mention_aliases, time_kind) VALUES "
            "(?, 'U1', ?, ?, 0, 'pending', 'contextual_date_once', 'M9:same:0', 'x', '[]', 'clock')",
            (G, f"成員甲 {_md(_day(42))} 看診（當天提醒）", _at(_day(42), 9)),
        )
    _, set_outcome = _add(_day(42), 12, "成員甲看診", f"{_md(_day(42))} 成員甲看診", "none")
    assert set_outcome == "created"


# ── a refused move, 嗎哪, names and addresses ─────────────────────────────────

@pytest.mark.parametrize("stage", ["pushed_4hr", "pushed_1hr"])
def test_repeating_a_time_after_a_refused_move_adds_nothing(stage):
    d = _day()
    rid, _ = _add(d, 12, "社區健檢", f"{_md(d)} 社區健檢", "none")
    _set(rid, **{stage: 1})
    added, created = _add(d, 12, "社區健檢", f"{_md(d)} 12:30 社區健檢", "clock", minute=30)
    again = _add(d, 12, "社區健檢", f"{_md(d)} 12:30 社區健檢", "clock", minute=30)
    reworded = _add(d, 12, "社區健檢", f"{_md(d)} 12點半社區健檢", "clock", minute=30)
    assert created == "created" and added != rid
    assert again == (added, "duplicate") and reworded == (added, "duplicate")
    assert sorted(_clock(r) for r in _pending_on(d)) == ["12:00", "12:30"]


def test_the_chat_path_does_not_announce_a_repeated_time_as_new():
    tomorrow = _day(1)
    main._maybe_extract_reminder("咪寶明天早上提醒我回診", G, "U1", "M-rep-1")
    _set(_pending_on(tomorrow)[0]["reminder_id"], pushed_4hr=1)
    replies = [
        main._maybe_extract_reminder("咪寶明天早上9點50分提醒我回診", G, "U1", f"M-rep-{n}")
        for n in (2, 3, 4)
    ]
    assert sorted(_clock(r) for r in _pending_on(tomorrow)) == ["09:00", "09:50"]
    assert replies[0].startswith("已新增提醒")
    assert not any(reply.startswith("已新增提醒") for reply in replies[1:]), replies


def test_a_reworded_mention_joins_the_row_that_already_has_its_time():
    d = _day()
    rid, _ = _add(d, 12, "社區健檢", f"{_md(d)} 社區健檢", "none")
    _set(rid, pushed_4hr=1)
    added, created = _add(d, 14, "社區健檢", f"{_md(d)} 14:00 社區健檢", "clock")
    same, outcome = _add(d, 14, "去社區健檢", f"{_md(d)} 14:00 去社區健檢，攜帶身分證", "clock")
    assert created == "created" and (same, outcome) == (added, "merged")
    assert sorted(_clock(r) for r in _pending_on(d)) == ["12:00", "14:00"]


@pytest.mark.parametrize(
    ("default", "new", "expected"),
    [((12, "none"), (12, 15), "12:15"), ((12, "none"), (11, 45), "11:45"),
     ((19, "daypart:晚上"), (19, 20), "19:20")],
)
def test_a_clock_for_a_manna_reminder_moves_it(default, new, expected):
    d = _day()
    rid, _ = _add(d, default[0], "嗎哪小組查經", f"{_md(d)} 嗎哪小組查經", default[1])
    same, outcome = _add(
        d, new[0], "嗎哪小組查經", f"{_md(d)} {expected} 嗎哪小組查經", "clock", minute=new[1]
    )
    rows = _pending_on(d)
    assert (same, outcome) == (rid, "merged") and len(rows) == 1
    assert _clock(rows[0]) == expected and rows[0]["time_kind"] == "clock"


def test_an_announced_manna_reminder_gets_the_new_time_as_its_own():
    d = _day()
    rid, _ = _add(d, 12, "嗎哪小組查經", f"{_md(d)} 嗎哪小組查經", "none")
    _set(rid, pushed_4hr=1)
    added, created = _add(d, 12, "嗎哪小組查經", f"{_md(d)} 12:15 嗎哪小組查經", "clock", minute=15)
    again = _add(d, 12, "嗎哪小組查經", f"{_md(d)} 12:15 嗎哪小組查經", "clock", minute=15)
    assert created == "created" and added != rid and again == (added, "duplicate")
    assert sorted(_clock(r) for r in _pending_on(d)) == ["12:00", "12:15"]


@pytest.mark.parametrize(
    ("first", "second"), [("台新信用卡繳費", "信用卡繳費國泰"), ("咪咪寵物美容", "寵物美容旺旺")]
)
def test_reordered_names_at_the_same_clock_are_different_events(first, second):
    d = _day()
    rid, _ = _add(d, 14, first, f"{_md(d)} 14:00 {first}", "clock")
    other, outcome = _add(d, 14, second, f"{_md(d)} 14:00 {second}", "clock")
    assert outcome == "created" and other != rid


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("帶手機去更換電池", "帶手錶去更換電池"),
        ("空腹抽血檢查", "飯後抽血檢查"),
        ("台北市中山路1號簽租約", "新北市中山路1號簽租約"),
        ("去仁愛社區中山路看牙醫", "去和平社區中山路看牙醫"),
        ("帶便當給弟弟", "帶外套給弟弟"),
    ],
)
def test_a_different_object_condition_or_place_never_moves_the_reminder(first, second):
    d = _day()
    rid, _ = _add(d, 12, first, f"{_md(d)} {first}", "none")
    other, outcome = _add(d, 15, second, f"{_md(d)} 15:00 {second}", "clock")
    assert outcome == "created" and other != rid
    assert sorted(_clock(r) for r in _pending_on(d)) == ["12:00", "15:00"]


def test_an_absorbed_address_keeps_a_different_number_out():
    d = _day()
    rid, _ = _add(d, 12, "中山路簽租約", f"{_md(d)} 中山路簽租約", "none")
    same, merged = _add(d, 12, "中山路1號簽租約", f"{_md(d)} 中山路1號簽租約", "none")
    other, outcome = _add(d, 15, "中山路2號簽租約", f"{_md(d)} 下午3點中山路2號簽租約", "clock")
    assert (same, merged) == (rid, "merged") and outcome == "created" and other != rid
    assert sorted(_clock(r) for r in _pending_on(d)) == ["12:00", "15:00"]


def test_a_different_floor_is_a_different_appointment():
    d = _day()
    rid, _ = _add(d, 12, "中山路1號5樓簽租約", f"{_md(d)} 中山路1號5樓簽租約", "none")
    other, outcome = _add(d, 15, "中山路1號6樓簽租約", f"{_md(d)} 下午3點中山路1號6樓簽租約", "clock")
    assert outcome == "created" and other != rid


@pytest.mark.parametrize(
    ("first", "second"), [("媽媽嗎哪小組查經", "嗎哪小組查經"), ("嗎哪小組查經", "媽媽嗎哪小組查經")]
)
def test_manna_mentions_with_and_without_mum_share_the_new_time(first, second):
    d = _day()
    rid, _ = _add(d, 19, first, f"{_md(d)} 晚上{first}", "daypart:晚上")
    same, outcome = _add(d, 19, second, f"{_md(d)} 19:20 {second}", "clock", minute=20)
    rows = _pending_on(d)
    assert (same, outcome) == (rid, "merged") and len(rows) == 1
    assert _clock(rows[0]) == "19:20" and rows[0]["time_kind"] == "clock"


def test_an_absorbed_name_keeps_a_different_name_out():
    d = _day()
    rid, _ = _add(d, 14, "信用卡繳費", f"{_md(d)} 14:00 信用卡繳費", "clock")
    same, merged = _add(d, 14, "信用卡繳費台新", f"{_md(d)} 14:00 信用卡繳費台新", "clock")
    other, outcome = _add(d, 14, "信用卡繳費國泰", f"{_md(d)} 14:00 信用卡繳費國泰", "clock")
    assert (same, merged) == (rid, "merged") and outcome == "created" and other != rid


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("中山路10巷1號簽租約", "中山路20巷1號簽租約"),
        ("中山路一段1號簽租約", "中山路二段1號簽租約"),
        ("中山路1號簽租約", "中山街1號簽租約"),
    ],
)
def test_a_different_lane_section_or_street_is_a_different_appointment(first, second):
    d = _day()
    rid, _ = _add(d, 12, first, f"{_md(d)} {first}", "none")
    other, outcome = _add(d, 15, second, f"{_md(d)} 下午3點{second}", "clock")
    assert outcome == "created" and other != rid
    assert sorted(_clock(r) for r in _pending_on(d)) == ["12:00", "15:00"]


@pytest.mark.parametrize(
    ("first", "second", "hour"),
    [
        ("中山路1號簽租約", "到中山路1號簽租約", 15),
        ("簽租約中山路1號", "中山路1號簽租約", 15),
        ("在台大醫院看皮膚科", "去台大醫院看皮膚科", 10),
        ("中山路1號簽租約", "中山路1號3樓簽租約", 15),
        ("繳網路費", "繳網路費帳單", 15),
    ],
)
def test_the_same_address_or_place_worded_differently_fills_in_the_time(first, second, hour):
    d = _day()
    rid, _ = _add(d, 12, first, f"{_md(d)} {first}", "none")
    same, outcome = _add(d, hour, second, f"{_md(d)} {hour}:00 {second}", "clock")
    assert (same, outcome) == (rid, "merged") and _clock(_pending_on(d)[0]) == f"{hour}:00"


# ── bookkeeping ───────────────────────────────────────────────────────────────

def test_replaying_the_same_message_adds_nothing():
    d = _day()
    rid, _ = _add(d, 14, "參加社區健檢", f"{_md(d)} 14:00 參加社區健檢", "clock")
    first = _add(d, 12, "回台北參加社區健檢", f"{_md(d)} 要回台北參加社區健檢", "none")
    again = _add(d, 12, "回台北參加社區健檢", f"{_md(d)} 要回台北參加社區健檢", "none")
    assert first == (rid, "merged") and again == (rid, "duplicate")
    assert len(_pending_on(d)[0]["merged_details"]) == 1


def test_an_explicit_reschedule_fixes_the_time():
    d = _day()
    rid, _ = _add(d, 12, "社區健檢", f"{_md(d)} 社區健檢", "none")
    assert memory.update_reminder_schedule(rid, _at(d, 10))
    assert _pending_on(d)[0]["time_kind"] == "clock"


def test_list_cleanup_keeps_the_most_specific_kind_and_every_detail():
    d = _day()
    with memory._conn() as c:
        for kind, details in (("none", '[{"key": "a", "action": "甲", "text": "甲"}]'),
                              ("clock", '[{"key": "b", "action": "乙", "text": "乙"}]')):
            c.execute(
                "INSERT INTO reminders(group_id, user_id, action, remind_at, created_at, status, "
                "source_kind, source_ref, source_text, mention_aliases, time_kind, merged_details) "
                "VALUES (?, 'U1', '社區健檢', ?, 0, 'pending', '', '', 'x', '[]', ?, ?)",
                (G, _at(d, 14), kind, details),
            )
    rows = _pending_on(d)
    assert len(rows) == 1 and rows[0]["time_kind"] == "clock"
    assert sorted(f["key"] for f in rows[0]["merged_details"]) == ["a", "b"]


def test_different_addresses_are_different_appointments():
    d = _day()
    rid, _ = _add(d, 12, "到中山路1號簽租約", f"{_md(d)} 到中山路1號簽租約", "none")
    other, outcome = _add(d, 15, "到中山路2號簽租約", f"{_md(d)} 下午3點到中山路2號簽租約", "clock")
    assert outcome == "created" and other != rid
    assert sorted(_clock(r) for r in _pending_on(d)) == ["12:00", "15:00"]


def test_the_same_dose_written_two_ways_is_one_event():
    d = _day()
    rid, _ = _add(d, 12, "流感疫苗第一劑", f"{_md(d)} 流感疫苗第一劑", "none")
    same, outcome = _add(d, 14, "流感疫苗第1劑", f"{_md(d)} 14:00 流感疫苗第1劑", "clock")
    assert (same, outcome) == (rid, "merged") and _clock(_pending_on(d)[0]) == "14:00"


def test_a_weak_mention_keeps_its_details_on_the_stronger_reminder():
    d = _day()
    rid, _ = _add(d, 9, "上皮拉提斯課", f"{_md(d)} 九點上皮拉提斯課", "clock")
    same, outcome = _add(d, 9, "8/8", f"{_md(d)} 記得帶水壺", "clock")
    assert (same, outcome) == (rid, "duplicate")
    assert "帶水壺" in _details(_pending_on(d)[0])


def test_a_weak_reminder_upgraded_by_a_strong_one_keeps_its_own_words():
    d = _day()
    rid, _ = _add(d, 9, "8/8", f"{_md(d)}早上 記得帶水壺", "daypart:早上")
    same, outcome = _add(d, 9, "上皮拉提斯課", f"{_md(d)} 九點上皮拉提斯課", "clock")
    row = _pending_on(d)[0]
    assert (same, outcome) == (rid, "merged") and row["action"] == "上皮拉提斯課"
    assert "帶水壺" in _details(row)


def test_an_offset_labelled_reminder_is_never_absorbed_into():
    d = _day()
    _insert(d, 9, "成員甲 看診（當天提醒）", "clock")
    _, outcome = _add(d, 12, "成員甲看診", f"{_md(d)} 成員甲看診", "none")
    assert outcome == "created" and len(_pending_on(d)) == 2


def test_list_cleanup_keeps_every_detail_beyond_the_per_row_limit():
    d = _day()
    with memory._conn() as c:
        for prefix in ("a", "b"):
            details = json.dumps(
                [{"key": f"{prefix}{i}", "action": f"說法{prefix}{i}", "text": ""} for i in range(15)],
                ensure_ascii=False,
            )
            c.execute(
                "INSERT INTO reminders(group_id, user_id, action, remind_at, created_at, status, "
                "source_kind, source_ref, source_text, mention_aliases, time_kind, merged_details) "
                "VALUES (?, 'U1', '社區健檢', ?, 0, 'pending', '', '', 'x', '[]', 'clock', ?)",
                (G, _at(d, 14), details),
            )
    rows = _pending_on(d)
    assert len(rows) == 1 and len(rows[0]["merged_details"]) == 30



def test_a_time_in_the_past_never_moves_a_live_reminder():
    # The quota drain accepts times up to an hour old; moving a live reminder
    # there would silence it. "Now" is pinned at 11:00 on the event day.
    d = _day()
    rid, _ = _add(d, 12, "去郵局寄包裹", f"{_md(d)} 去郵局寄包裹", "none")
    with memory._lock, memory._conn() as c:
        c.execute("BEGIN IMMEDIATE")
        other, outcome = memory._add_reminder_with_outcome_conn(
            c, G, "U1", "去郵局寄包裹", _at(d, 10, 30), f"{_md(d)} 10:30 去郵局寄包裹",
            [], _at(d, 11), "clock",
        )
    assert outcome == "created" and other != rid
    assert sorted(_clock(r) for r in _pending_on(d)) == ["10:30", "12:00"]


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("咪寶明天提醒我回診", "咪寶明天11點15分提醒我回診", "11:15"),
        ("咪寶明天早上提醒我回診", "咪寶明天早上8點20分提醒我回診", "08:20"),
        ("咪寶明天晚上提醒我回診", "咪寶明天晚上6點半提醒我回診", "18:30"),
    ],
)
def test_a_clock_close_to_the_default_still_fills_it_in(first, second, expected):
    tomorrow = _day(1)
    main._maybe_extract_reminder(first, G, "U1", "M-near-1")
    rid = _pending_on(tomorrow)[0]["reminder_id"]
    reply = main._maybe_extract_reminder(second, G, "U1", "M-near-2")
    rows = _pending_on(tomorrow)
    assert len(rows) == 1 and rows[0]["reminder_id"] == rid and _clock(rows[0]) == expected
    assert reply.startswith("已更新既有提醒")


def test_a_repeat_with_a_different_clock_does_not_keep_its_stale_time():
    d = _day()
    rid, _ = _add(d, 15, "開會", f"{_md(d)} 下午3點開會", "clock")
    same, outcome = _add(d, 15, "開會", f"{_md(d)} 下午3點40分開會", "clock", minute=40)
    assert (same, outcome) == (rid, "duplicate") and _pending_on(d)[0]["merged_details"] == []


def test_an_explicit_reschedule_clears_absorbed_details():
    d = _day()
    rid, _ = _add(d, 14, "參加社區健檢", f"{_md(d)} 14:00 參加社區健檢", "clock")
    _add(d, 12, "回台北參加社區健檢", f"{_md(d)} 要回台北參加社區健檢", "none")
    assert memory.update_reminder_schedule(rid, _at(d, 17))
    assert _pending_on(d)[0]["merged_details"] == []


def test_the_confirmation_says_when_the_kept_time_is_still_a_default():
    d = _day()
    rid, _ = _add(d, 12, "社區健檢", f"{_md(d)} 社區健檢", "none")
    same, outcome = _add(d, 12, "社區健檢集合", f"{_md(d)} 社區健檢集合", "none")
    text = main._format_persisted_reminder_confirmation(
        outcome, same, "社區健檢集合", datetime.fromtimestamp(_at(d, 12), TZ)
    )
    assert outcome == "merged" and "未指定時間" in text

def test_the_migration_can_run_again_and_old_rows_have_no_kind():
    d = _day()
    with memory._conn() as c:
        c.execute(
            "INSERT INTO reminders(group_id, user_id, action, remind_at, created_at, status) "
            "VALUES (?, 'U1', '舊資料', ?, 0, 'pending')",
            (G, _at(d, 12)),
        )
    memory._init_db()
    memory._init_db()
    row = _pending_on(d)[0]
    assert row["time_kind"] is None and row["merged_details"] == []


# ── the matcher on its own ────────────────────────────────────────────────────

NAMES = ("成員甲", "成員乙", "成員丙")


def _absorbs(new, kept, same_clock=False):
    return not ri.same_event_identity_conflict(new, [kept], NAMES) and ri.same_event_text(
        new, kept, same_clock=same_clock, names=NAMES
    )


@pytest.mark.parametrize(
    ("new", "kept"),
    [
        ("參加社區健檢", "回台北參加社區健檢"),
        ("參加社區健檢", "去社區健檢做檢查"),
        ("看牙醫", "成員甲看牙醫"),
        ("家族聚餐", "家族聚餐在合成餐廳"),
        ("繳房屋稅", "繳房屋稅最後一天"),
        ("成員甲回診", "成員甲 10:30 回診"),
        ("社區健檢", "社區健檢（攜帶身分證）"),
        ("回台北打流感疫苗", "施打流感疫苗和新冠疫苗"),
        ("流感疫苗第一劑", "打流感疫苗第一劑"),
        ("陪媽媽看牙醫", "陪媽媽去看牙醫"),
        ("打流感疫苗第一劑", "打流感疫苗第1劑"),
        ("中山路1號簽租約", "到中山路1號簽租約"),
        ("去民生路看牙醫", "到民生路看牙醫"),
        ("在台大醫院看皮膚科", "去台大醫院看皮膚科"),
        ("回中山路老家吃飯", "去中山路老家吃飯"),
        ("中山路10巷簽租約", "中山路10巷1號簽租約"),
        ("簽租約中山路1號", "中山路1號簽租約"),
        ("臺北市中正區忠孝東路一段108號打流感疫苗", "打流感疫苗"),
        ("辦理護照換發", "護照換發手續"),
        ("施打流感疫苗", "流感疫苗接種"),
    ],
)
def test_matcher_recognises_the_same_event(new, kept):
    assert _absorbs(new, kept)


@pytest.mark.parametrize(
    ("new", "kept"),
    [
        ("聚餐", "聚餐前買蛋糕"), ("繳房屋稅", "繳地價稅"), ("開會", "家長會"),
        ("看診", "看診後拿藥"), ("社區健檢", "健檢前一天禁食"), ("去銀行", "去郵局"),
        ("接小孩", "送小孩"), ("台北醫院看牙", "台北醫院復健"),
        ("成員甲和成員乙看牙醫", "成員乙和成員丙復健"), ("媽媽看牙醫", "爸爸看牙醫"),
        ("合成路1號3樓開會", "合成路1號3樓上課"), ("送洗衣服", "拿洗衣服"),
        ("流感疫苗第一劑", "流感疫苗第二劑"), ("陪媽媽看牙醫", "媽媽陪爸爸看牙醫"),
        ("牙科初診", "牙科複診"), ("社區健檢上午場", "社區健檢下午場"), ("預約看牙", "取消看牙"),
        ("台北國泰醫院回診", "馬偕醫院回診"), ("王小明結婚喜宴", "李大華結婚喜宴"),
        ("小阿姨生日聚餐", "舅舅生日聚餐"), ("帶阿姨去台大醫院看皮膚科", "舅舅去台大醫院看皮膚科"),
        ("畢業典禮", "畢業典禮彩排"), ("社區健檢", "社區健檢說明會"), ("媽媽繳費", "爸爸繳費"),
        ("明天提醒我到中山路1號簽租約", "明天下午3點提醒我到中山路2號簽租約"),
        ("合成路1號家庭會議", "合成路2號家庭會議"), ("到中山路1號簽約", "到民生路1號簽約"),
        ("媽媽回診看眼科", "媽媽回診看牙科"), ("寵物美容接咪咪", "寵物美容接旺旺"),
        ("信用卡繳費台新", "信用卡繳費國泰"),
        ("中山路10巷1號簽租約", "中山路20巷1號簽租約"), ("中山路1巷2號簽租約", "中山路3巷2號簽租約"),
        ("中山路一段1號簽租約", "中山路二段1號簽租約"), ("中山路1號簽租約", "中山街1號簽租約"),
        ("中山北路1號簽約", "中山路1號簽約"), ("台新信用卡繳費", "信用卡繳費國泰"),
        ("咪咪寵物美容", "寵物美容旺旺"), ("中山路1號5樓簽租約", "中山路1號6樓簽租約"),
        ("看牙醫帶健保卡", "看眼科帶健保卡"), ("全聯買菜", "全聯買藥"),
        ("帶狗去打預防針", "帶貓去打預防針"), ("帶咪咪看醫生", "帶旺旺看醫生"),
        ("帶小孩看牙醫", "帶小明看牙醫"), ("去全聯超市中山路店買菜", "去家樂福中山路店買菜"),
        ("去中正一路看牙醫", "去中正二路看牙醫"),
        ("帶手機去更換電池", "帶手錶去更換電池"), ("空腹抽血檢查", "飯後抽血檢查"),
        ("買媽媽生日蛋糕", "拿媽媽生日蛋糕"), ("台北市中山路1號簽租約", "新北市中山路1號簽租約"),
        ("帶狗領藥看醫生", "帶貓領藥看醫生"), ("買二手腳踏車", "賣二手腳踏車"),
        ("買兒童腳踏車", "修兒童腳踏車"), ("帶便當給爸爸", "帶藥給爸爸"),
        ("買狗飼料送收容所", "買貓飼料送收容所"), ("去仁愛社區中山路看牙醫", "去和平社區中山路看牙醫"),
        ("帶便當給弟弟", "帶外套給弟弟"), ("帶小卡去打預防針", "帶旺旺去打預防針"),
    ],
)
def test_matcher_keeps_different_events_apart(new, kept):
    assert not _absorbs(new, kept) and not _absorbs(new, kept, same_clock=True)


def test_absorbed_descriptions_count_as_identity():
    assert ri.same_event_identity_conflict("信用卡繳費國泰", ["信用卡繳費", "信用卡繳費台新"], NAMES)
    assert ri.same_event_identity_conflict("眼科回診", ["回診", "牙科回診"], NAMES)
    assert not ri.same_event_identity_conflict("信用卡繳費", ["信用卡繳費", "信用卡繳費台新"], NAMES)
    assert not ri.same_event_identity_conflict(
        "去社區健檢做檢查", ["參加社區健檢", "回台北參加社區健檢"], NAMES
    )


def test_addresses_are_read_part_by_part():
    assert ri._same_event_addresses("到忠孝東路四段10巷2弄3之1號5樓、12巷8號") == [
        ("忠孝東", "路", "4", "10", "2", "3之1", "5"),
        (None, None, None, "12", None, "8", None),
    ]
    assert ri._same_event_addresses("網路報名、走路去逛街、10月8號") == []
    assert ri._same_event_addresses("家裡網路繳費、修理網路設備、繳網路費、走高速公路") == []
    assert ri._same_event_addresses("向上路一段100號") == [("向上", "路", "1", None, None, "100", None)]


def test_a_short_action_counts_only_when_the_same_person_repeats_it():
    assert ri.same_event_text("領米", "領米", same_author=True)
    assert ri.same_event_move_match("領米", "領米", ["領米"], NAMES, same_author=True)
    assert not ri.same_event_text("領米", "領米")
    assert not ri.same_event_move_match("領米", "領米", ["領米"], NAMES)


def test_the_same_event_at_the_same_clock_may_add_words():
    assert _absorbs("打流感疫苗更新的新冠疫苗", "施打流感疫苗和新冠疫苗", same_clock=True)


def test_moving_a_time_needs_a_stronger_match():
    assert ri.same_event_move_match("社區健檢", "社區健檢", ["社區健檢"], NAMES)
    assert ri.same_event_move_match("打流感疫苗", "回台北打流感疫苗", ["回台北打流感疫苗"], NAMES)
    assert not ri.same_event_move_match("看牙醫", "成員甲看牙醫", ["成員甲看牙醫"], NAMES)
    assert not ri.same_event_move_match(
        "回台北打流感疫苗", "施打流感疫苗和新冠疫苗", ["施打流感疫苗和新冠疫苗"], NAMES
    )


def test_time_kinds():
    assert ri.time_kind_from_default(None) == "clock"
    assert ri.time_kind_from_default(True) == "none"
    assert ri.time_kind_from_default("morning") == "daypart:早上"
    assert ri.times_compatible(None, "12:00", "clock", "14:00") is False
    assert ri.times_compatible("none", "12:00", "clock", "14:00") is True
    assert ri.times_compatible("daypart:下午", "15:00", "clock", "14:00") is True
    assert ri.time_is_confirmed_by("12:00", "daypart:下午", "15:00") is False
