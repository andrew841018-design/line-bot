"""2026-11-12 Andrew (bug C)：同一人、同一天、同一個明確鐘點、推給同一群人的提醒，
第一次該推時併成一筆（另一筆的文字放進「細節」），之後每段只推一則。

The push-time fold in reminder_push pairs one person's rows at one explicit
clock whatever their wording, and the identity check does not keep them
apart.  Everything else (other people, another minute, a vague or legacy
time, another audience, calendar mirrors, 前一天／當天 rows, offset labels)
folds only as before.  All content is made up; dates are relative to today.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent

import main
import memory
import reminder_push
import reminder_stages

TZ = ZoneInfo("Asia/Taipei")
G = "G_SAME_MINUTE"

# One person's two wordings of one phone call: the matcher cannot read either
# into the other and the identity check sees two different places.
CALL = "打電話到星光診所預約"
CALL_AGAIN = "打給海景醫院掛號"


@pytest.fixture(autouse=True)
def _synthetic_aliases(tmp_path, monkeypatch):
    aliases = tmp_path / "aliases.json"
    aliases.write_text(
        json.dumps({"U_A": "成員甲", "U_B": "成員乙", "U_C": "成員丙"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(tmp_path / "no_roles.json"))


def _day(offset: int):
    return (datetime.now(TZ) + timedelta(days=offset)).date()


def _at(day, hour: int, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).timestamp())


def _insert(
    action: str,
    remind_at: int,
    *,
    user: str = "U_A",
    kind: str | None = "clock",
    source_kind: str = "",
    source_ref: str = "",
    mentions: list[str] | None = None,
    details: list[str] | None = None,
    **flags,
) -> int:
    fragments = [memory._merged_detail_fragment(text, text) for text in details or []]
    with memory._conn() as c:
        cur = c.execute(
            "INSERT INTO reminders(group_id, user_id, action, remind_at, created_at, status, "
            "source_kind, source_ref, source_text, mention_aliases, time_kind, merged_details) "
            "VALUES (?, ?, ?, ?, 0, 'pending', ?, ?, ?, ?, ?, ?)",
            (G, user, action, remind_at, source_kind, source_ref, action,
             json.dumps(mentions or [], ensure_ascii=False), kind,
             json.dumps(fragments, ensure_ascii=False)),
        )
        rid = int(cur.lastrowid)
        for column, value in flags.items():
            c.execute(f"UPDATE reminders SET {column}=? WHERE reminder_id=?", (value, rid))
    return rid


def _row(rid: int) -> dict:
    with memory._conn() as c:
        c.row_factory = __import__("sqlite3").Row
        row = c.execute("SELECT * FROM reminders WHERE reminder_id=?", (rid,)).fetchone()
    assert row is not None
    return dict(row)


def _status(rid: int) -> str:
    return str(_row(rid)["status"])


def _details(rid: int) -> str:
    return json.dumps(json.loads(_row(rid)["merged_details"] or "[]"), ensure_ascii=False)


def _pending(rid: int) -> dict:
    rows = {
        int(r["reminder_id"]): r
        for r in memory.list_pending_reminders_full(G, dedupe=False)
    }
    return rows[rid]


def _set_now(monkeypatch, moment: int) -> None:
    monkeypatch.setattr(reminder_push, "_now_ts", lambda: int(moment))


def _record_pushes(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_push(group_id, text, **kwargs):
        calls.append({"group_id": group_id, "text": text, **kwargs})
        return True

    monkeypatch.setattr(reminder_push, "_push_to_group", fake_push)
    return calls


def _assert_not_one_event(first: int, second: int) -> None:
    """The matcher reads neither row into the other: only the clock rule can pair them."""
    left, right = _pending(first), _pending(second)
    assert not reminder_push._merges_into(left, right)
    assert not reminder_push._merges_into(right, left)


# ── one person, one clock: one push ──────────────────────────────────────────


def test_two_wordings_of_one_call_at_one_clock_push_once(monkeypatch):
    at = _at(_day(3), 8, 30)
    first = _insert(CALL, at)
    second = _insert(CALL_AGAIN, at)
    _assert_not_one_event(first, second)
    left, right = _pending(first), _pending(second)
    assert reminder_push._identity_conflict(right, reminder_push._identity_texts(left), ())
    calls = _record_pushes(monkeypatch)

    _set_now(monkeypatch, at - 4 * 3600 + 60)
    assert reminder_push.push_reminders() == 1

    assert len(calls) == 1 and calls[0]["reminder_id"] == first
    assert _status(first) == "pending" and _status(second) == "cancelled"
    assert CALL_AGAIN in _details(first)
    assert "細節：" + CALL_AGAIN in calls[0]["text"] and CALL in calls[0]["text"]
    assert _row(first)["pushed_4hr"] == 1


def test_every_later_stage_sends_one_message(monkeypatch):
    at = _at(_day(3), 8, 30)
    first = _insert(CALL, at)
    second = _insert(CALL_AGAIN, at)
    calls = _record_pushes(monkeypatch)

    for ahead in (4 * 3600, 2 * 3600, 3600, 0):
        before = len(calls)
        _set_now(monkeypatch, at - ahead + 60)
        assert reminder_push.push_reminders() == 1
        assert len(calls) == before + 1 and calls[-1]["reminder_id"] == first
        assert reminder_push.push_reminders() == 0      # nothing twice in one moment

    assert len(calls) == 4
    assert _status(first) == "done" and _status(second) == "cancelled"


def test_a_typo_and_its_fix_at_one_clock_push_once(monkeypatch):
    at = _at(_day(2), 16)
    typo = _insert("看牙衣", at)
    fixed = _insert("看牙醫", at)
    _assert_not_one_event(typo, fixed)
    calls = _record_pushes(monkeypatch)

    _set_now(monkeypatch, at - 3600 + 60)
    assert reminder_push.push_reminders() == 1

    assert {_status(typo), _status(fixed)} == {"pending", "cancelled"}
    assert "看牙衣" in calls[0]["text"] and "看牙醫" in calls[0]["text"]


def test_three_rows_at_one_clock_become_one_push(monkeypatch):
    at = _at(_day(3), 14)
    texts = ("繳停車費", "去郵局寄包裹", "打第二劑疫苗")
    rids = [_insert(text, at) for text in texts]
    calls = _record_pushes(monkeypatch)

    _set_now(monkeypatch, at - 3600 + 60)
    assert reminder_push.push_reminders() == 1

    survivors = [rid for rid in rids if _status(rid) == "pending"]
    assert len(survivors) == 1 and sum(_status(r) == "cancelled" for r in rids) == 2
    for text in texts:
        assert text in calls[0]["text"]


def test_naming_another_member_in_the_text_is_another_audience(monkeypatch):
    # The push for 「…給成員乙」 also pings 成員乙, so it is not "the same people".
    at = _at(_day(3), 14)
    first = _insert("繳停車費", at)
    second = _insert("去郵局寄包裹給成員乙", at)
    _set_now(monkeypatch, at - 3600 + 60)
    assert reminder_push.fold_due_duplicates() == 0
    assert _status(first) == _status(second) == "pending"


def test_the_twin_does_not_redefine_the_event_for_later_rows(monkeypatch):
    # The vague row (no time given) is the primary's own appointment; the 14:00
    # twin (another errand) only rides along.  The twin's words must not keep
    # the vague row out, although the fold looks at the twin first (it sorts
    # before 18:00).
    d = _day(3)
    primary = _insert("施打流感疫苗和新冠疫苗", _at(d, 14))
    twin = _insert("去台新銀行繳費", _at(d, 14))
    vague = _insert("打流感疫苗", _at(d, 18), kind="none")
    p, t, v = _pending(primary), _pending(twin), _pending(vague)
    assert reminder_push._merges_into(v, p) or reminder_push._merges_into(p, v)
    assert not reminder_push._identity_conflict(v, reminder_push._identity_texts(p), ())
    assert reminder_push._identity_conflict(v, reminder_push._identity_texts(p) +
                                            reminder_push._identity_texts(t), ())
    calls = _record_pushes(monkeypatch)

    _set_now(monkeypatch, _at(d, 14) - 3600 + 60)
    assert reminder_push.push_reminders() == 1

    assert len(calls) == 1 and calls[0]["reminder_id"] == primary
    assert _status(twin) == _status(vague) == "cancelled"


@pytest.mark.parametrize(
    "details",
    [
        ["看牙醫帶健保卡", "看牙醫順便洗牙", "看牙醫要先掛號"],          # the 3-item cap
        ["看牙醫" + "，記得帶健保卡和上次的藥袋" * 9],                  # the 120-character cap
    ],
    ids=["three-items", "long-detail"],
)
def test_a_twin_that_would_not_show_is_not_folded(monkeypatch, details):
    # Another errand must never disappear: when the 細節 line is already full,
    # the two rows are pushed as before (two at one moment, never none).
    at = _at(_day(3), 14)
    dentist = _insert("看牙醫", at, details=details)
    parking = _insert("繳停車費", at)
    calls = _record_pushes(monkeypatch)

    _set_now(monkeypatch, at - 3600 + 60)
    assert reminder_push.push_reminders() == 2

    assert len(calls) == 2 and _status(dentist) == _status(parking) == "pending"


def test_only_the_twin_that_still_fits_on_the_detail_line_folds(monkeypatch):
    # Room for one more item: one twin folds, the other is pushed on its own
    # (were the check made only at the end, neither would fold).
    at = _at(_day(3), 14)
    dentist = _insert("看牙醫", at, details=["看牙醫帶健保卡", "看牙醫順便洗牙"])
    parking = _insert("繳停車費", at)
    parcel = _insert("去郵局寄包裹", at)
    _record_pushes(monkeypatch)

    _set_now(monkeypatch, at - 3600 + 60)
    assert reminder_push.push_reminders() == 2

    assert _status(dentist) == "pending"
    assert sorted([_status(parking), _status(parcel)]) == ["cancelled", "pending"]


def test_an_errand_with_no_words_that_could_show_is_not_folded(monkeypatch):
    at = _at(_day(3), 14)
    dentist = _insert("看牙醫", at)
    emoji = _insert("🦷🦷", at)
    _set_now(monkeypatch, at - 3600 + 60)
    assert reminder_push.fold_due_duplicates() == 0
    assert _status(dentist) == _status(emoji) == "pending"


def test_a_matched_pair_still_folds_when_no_twin_can_ride(monkeypatch):
    # The fullest row (no room left on its 細節 line) cannot take a rider; the
    # two wordings of the visit are still one push, as before the rider rule.
    at = _at(_day(3), 14)
    full = _insert("看牙醫", at, mentions=["全家"],
                   details=["看牙醫帶健保卡", "看牙醫順便洗牙", "看牙醫要先掛號"])
    restated = _insert("海景醫院看外婆", at, mentions=["全家"])
    visit = _insert("去海景醫院看外婆", at, user="U_B", mentions=["全家"])
    rows = memory.list_pending_reminders_full(G, dedupe=False)
    plans = reminder_push.plan_folds(rows, at - 3600 + 60)
    assert [(p["reminder_id"], [x["reminder_id"] for x in peers]) for p, peers in plans] == [
        (visit, [restated])
    ]
    assert full not in {x["reminder_id"] for _p, peers in plans for x in peers}


def test_a_riders_restatement_that_cannot_join_keeps_the_old_plan():
    # The visit's own restatement rides on the dentist, but the visit itself
    # asked to be reminded once: splitting them would push the visit twice.
    at = _at(_day(3), 14)
    _insert("看牙醫", at, mentions=["全家"], details=["看牙醫帶健保卡"])
    restated = _insert("海景醫院看外婆", at, mentions=["全家"])
    visit = _insert("去海景醫院看外婆", at, user="U_B", mentions=["全家"])
    rows = [
        {**row, "push_keep_stage": "now"} if row["reminder_id"] == visit else row
        for row in memory.list_pending_reminders_full(G, dedupe=False)
    ]
    plans = reminder_push.plan_folds(rows, at - 3600 + 60)
    assert [(p["reminder_id"], [x["reminder_id"] for x in peers]) for p, peers in plans] == [
        (visit, [restated])
    ]


def test_a_riders_vague_restatement_that_cannot_join_keeps_the_old_plan():
    # Same as above with the visit given no time (18:00 default): it still
    # fits the dentist's time, so it must not be split from its restatement.
    d = _day(3)
    _insert("看牙醫", _at(d, 14), mentions=["全家"], details=["看牙醫帶健保卡"])
    restated = _insert("海景醫院看外婆", _at(d, 14), mentions=["全家"])
    visit = _insert("去海景醫院看外婆", _at(d, 18), user="U_B", mentions=["全家"], kind="none")
    rows = [
        {**row, "push_keep_stage": "now"} if row["reminder_id"] == visit else row
        for row in memory.list_pending_reminders_full(G, dedupe=False)
    ]
    plans = reminder_push.plan_folds(rows, _at(d, 14) - 3600 + 60)
    assert [(p["reminder_id"], [x["reminder_id"] for x in peers]) for p, peers in plans] == [
        (restated, [visit])
    ]


@pytest.mark.parametrize("order", ["flu,first,second", "second,first,flu", "first,second,flu"])
def test_a_same_clock_restatement_extends_its_errand(order):
    # 打流感疫苗 and its 第1劑 wording both ride at 14:00 as one errand, so the
    # vague 第2劑 is still kept out (as before the rider rule), in any order.
    d = _day(3)
    _insert("去郵局寄包裹給外婆再去銀行", _at(d, 14))       # the longest: the primary
    specs = {
        "flu": ("打流感疫苗", _at(d, 14), "clock"),
        "first": ("打流感疫苗第1劑", _at(d, 14), "clock"),
        "second": ("打流感疫苗第2劑", _at(d, 18), "none"),
    }
    ids = {key: _insert(specs[key][0], specs[key][1], kind=specs[key][2])
           for key in order.split(",")}
    flu, first, second = ids["flu"], ids["first"], ids["second"]
    f, s = _pending(flu), _pending(second)
    assert reminder_push._merges_into(s, f) or reminder_push._merges_into(f, s)
    rows = memory.list_pending_reminders_full(G, dedupe=False)
    plans = reminder_push.plan_folds(rows, _at(d, 14) - 3600 + 60)
    folded = {x["reminder_id"] for _p, peers in plans for x in peers}
    assert {flu, first} <= folded and second not in folded


@pytest.mark.parametrize("order", ["E1,E2,R,R2", "R2,R,E2,E1", "E2,R2,E1,R", "R,R2,E1,E2"])
def test_a_generic_restatement_does_not_bridge_two_doses(order):
    # Both doses ride at 14:00 (the accepted trade-off); 「打流感疫苗」 restates
    # both, 「打流感疫苗第1劑」 (no time) restates the first.  Neither may be left
    # out: every errand goes out exactly once, whatever the order of the rows.
    d = _day(3)
    _insert("去郵局寄包裹給外婆再去銀行", _at(d, 14))       # the longest: the primary
    specs = {
        "E1": ("打流感疫苗第1劑", _at(d, 14), "clock"),
        "E2": ("打流感疫苗第2劑", _at(d, 14), "clock"),
        "R": ("打流感疫苗第1劑", _at(d, 18), "none"),
        "R2": ("打流感疫苗", _at(d, 18), "none"),
    }
    ids = {key: _insert(specs[key][0], specs[key][1], kind=specs[key][2])
           for key in order.split(",")}
    rows = {rid: _pending(rid) for rid in ids.values()}
    for left, right in (("R", "E1"), ("R2", "E1"), ("R2", "E2")):
        a, b = rows[ids[left]], rows[ids[right]]
        assert reminder_push._merges_into(a, b) or reminder_push._merges_into(b, a)
    plans = reminder_push.plan_folds(
        memory.list_pending_reminders_full(G, dedupe=False), _at(d, 14) - 3600 + 60
    )
    assert len(plans) == 1
    assert {peer["reminder_id"] for peer in plans[0][1]} == set(ids.values())


@pytest.mark.parametrize("order", ["E1,E2,F,R", "R,F,E2,E1", "F,R,E1,E2", "E2,R,F,E1"])
@pytest.mark.parametrize(
    "words",
    [
        ("打流感疫苗第1劑", "打流感疫苗第2劑", "打流感疫苗"),
        ("去星光診所看牙醫", "去海景醫院看牙醫", "看牙醫"),
    ],
    ids=["doses", "places"],
)
def test_a_restatement_of_one_side_of_a_bridged_errand_stays_with_it(order, words):
    # E1, E2 and the generic F all ride at 14:00, so one errand holds both
    # sides; R (no time) restates E1.  It must not be pushed apart from E1:
    # the start row plans as before the rider rule, in any order.
    d = _day(3)
    _insert("去郵局寄包裹給外婆再去銀行", _at(d, 14))       # the longest: the primary
    specs = {
        "E1": (words[0], _at(d, 14), "clock"),
        "E2": (words[1], _at(d, 14), "clock"),
        "F": (words[2], _at(d, 14), "clock"),
        "R": (words[0], _at(d, 18), "none"),
    }
    ids = {key: _insert(specs[key][0], specs[key][1], kind=specs[key][2])
           for key in order.split(",")}
    e1, r = _pending(ids["E1"]), _pending(ids["R"])
    assert reminder_push._merges_into(r, e1) or reminder_push._merges_into(e1, r)
    plans = reminder_push.plan_folds(
        memory.list_pending_reminders_full(G, dedupe=False), _at(d, 14) - 3600 + 60
    )
    together = [
        {p["reminder_id"], *(x["reminder_id"] for x in peers)} for p, peers in plans
    ]
    assert any({ids["E1"], ids["R"]} <= members for members in together)


def test_a_later_same_clock_row_that_contradicts_a_restatement_plans_as_before():
    # 成員乙's 第2劑 restates the flu rider and joins; then 成員乙's own 第1劑 at
    # the same clock would join too.  Two doses must never become one push:
    # the whole start row plans as before the rider rule.
    at = _at(_day(3), 14)
    _insert("去郵局寄包裹給外婆再去銀行", at, mentions=["全家"])   # the primary
    flu = _insert("打流感疫苗", at, mentions=["全家"])
    second = _insert("打流感疫苗第2劑", at, user="U_B", mentions=["全家"])
    first = _insert("打流感疫苗第1劑", at, user="U_B", mentions=["全家"])
    f = _pending(flu)
    for rid in (second, first):
        r = _pending(rid)
        assert reminder_push._merges_into(r, f) or reminder_push._merges_into(f, r)
    rows = memory.list_pending_reminders_full(G, dedupe=False)
    plans = reminder_push.plan_folds(rows, at - 3600 + 60)
    for primary, peers in plans:
        members = {primary["reminder_id"], *(peer["reminder_id"] for peer in peers)}
        assert not {first, second} <= members


def test_a_restatement_is_checked_against_its_own_errand_only(monkeypatch):
    # Two errands ride on the primary; another member's restatement of the
    # dentist must join the dentist even though it clashes with the other errand.
    at = _at(_day(3), 14)
    _insert("去郵局寄包裹給外婆再去銀行", at, mentions=["全家"])    # the primary
    dentist = _insert("看牙醫", at, mentions=["全家"])
    _insert("帶外婆買菜", at, mentions=["全家"])
    restated = _insert("下午看牙醫", at, user="U_B", mentions=["全家"])
    d, r = _pending(dentist), _pending(restated)
    assert reminder_push._merges_into(r, d) or reminder_push._merges_into(d, r)
    calls = _record_pushes(monkeypatch)

    _set_now(monkeypatch, at - 3600 + 60)
    reminder_push.push_reminders()

    assert _status(restated) == "cancelled"
    assert sum("看牙醫" in call["text"] for call in calls) == 1


def test_a_riders_restatements_still_meet_the_identity_check():
    d = _day(3)
    _insert("去郵局寄包裹給外婆再去銀行", _at(d, 14))       # the longest: the primary
    flu = _insert("打流感疫苗", _at(d, 14))                   # a rider at the same clock
    first = _insert("打流感疫苗第1劑", _at(d, 18), kind="none")
    second = _insert("打流感疫苗第2劑", _at(d, 18), kind="none")
    f = _pending(flu)
    for rid in (first, second):
        r = _pending(rid)
        assert reminder_push._merges_into(r, f) or reminder_push._merges_into(f, r)
    rows = memory.list_pending_reminders_full(G, dedupe=False)
    plans = reminder_push.plan_folds(rows, _at(d, 14) - 3600 + 60)
    folded = {x["reminder_id"] for _p, peers in plans for x in peers}
    assert flu in folded and len(folded & {first, second}) == 1


def test_a_failing_rider_check_leaves_the_old_folds_alone(monkeypatch):
    def boom(*_args):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(reminder_push, "_twin_shows", boom)
    at = _at(_day(3), 14)
    restated = _insert("海景醫院看外婆", at, mentions=["全家"])
    visit = _insert("去海景醫院看外婆", at, user="U_B", mentions=["全家"])
    _insert("繳停車費", at, mentions=["全家"])
    rows = memory.list_pending_reminders_full(G, dedupe=False)
    plans = reminder_push.plan_folds(rows, at - 3600 + 60)
    assert [(p["reminder_id"], [x["reminder_id"] for x in peers]) for p, peers in plans] == [
        (visit, [restated])
    ]


def test_a_riders_restatement_rides_and_the_primarys_own_row_still_folds():
    d = _day(3)
    flu = _insert("施打流感疫苗和新冠疫苗", _at(d, 14))
    parking = _insert("繳停車費", _at(d, 14))
    parking_again = _insert("繳停車費第2次", _at(d, 14))
    vague = _insert("打流感疫苗第1劑", _at(d, 18), kind="none")
    p, v = _pending(flu), _pending(vague)
    assert reminder_push._merges_into(v, p) or reminder_push._merges_into(p, v)
    rows = memory.list_pending_reminders_full(G, dedupe=False)
    plans = reminder_push.plan_folds(rows, _at(d, 14) - 3600 + 60)

    assert len(plans) == 1 and plans[0][0]["reminder_id"] == flu
    marks = {peer["reminder_id"]: peer.get("fold_twin", False) for peer in plans[0][1]}
    assert marks == {vague: False, parking: True, parking_again: True}


@pytest.mark.parametrize("order", ["PTYV", "TYPV"])
def test_riders_across_the_family_do_not_depend_on_insertion_order(monkeypatch, order):
    at = _at(_day(3), 14)
    specs = {
        "P": ("打流感疫苗", "U_A", ["打流感疫苗記得帶健保卡"]),
        "T": ("海景醫院看外婆", "U_A", None),
        "Y": ("去海景醫院看外婆", "U_B", None),
        "V": ("去星光診所打流感疫苗", "U_C", None),
    }
    ids = {
        key: _insert(specs[key][0], at, user=specs[key][1], mentions=["全家"], details=specs[key][2])
        for key in order
    }
    calls = _record_pushes(monkeypatch)

    _set_now(monkeypatch, at - 3600 + 60)
    assert reminder_push.push_reminders() == 1

    assert calls[0]["reminder_id"] == ids["P"]
    assert {_status(ids[k]) for k in "TYV"} == {"cancelled"}
    assert "看外婆" in calls[0]["text"] and "星光診所" in calls[0]["text"]


def test_the_simulated_details_match_what_the_fold_stores():
    # reminder_push mirrors memory.fold_same_event_reminders to decide whether a
    # rider still shows; this pins the mirror to what memory really writes.
    at = _at(_day(3), 14)
    primary = _insert("看牙醫", at, details=["看牙醫帶健保卡"])
    peer_ids = [_insert("繳停車費", at), _insert("去郵局寄包裹", at, details=["去郵局寄包裹給外婆"])]
    rows = {r["reminder_id"]: r for r in memory.list_pending_reminders_full(G, dedupe=False)}
    peers = [rows[rid] for rid in peer_ids]
    expected = reminder_push._details_after_fold(rows[primary], peers)

    assert memory.fold_same_event_reminders(rows[primary], peers) == peer_ids
    stored = json.loads(_row(primary)["merged_details"])
    assert [item["key"] for item in stored] == [item["key"] for item in expected]


def test_a_rider_crowded_off_after_all_folds_as_before(monkeypatch):
    # Should a rider pass the check while folding and still end up off the
    # 細節 line, the start row plans as it did before the rider rule.
    monkeypatch.setattr(reminder_push, "_twin_shows", lambda *_args: True)
    at = _at(_day(3), 14)
    _insert("看牙醫", at, details=["看牙醫帶健保卡", "看牙醫順便洗牙", "看牙醫要先掛號"])
    _insert("繳停車費", at)
    rows = memory.list_pending_reminders_full(G, dedupe=False)
    assert reminder_push.plan_folds(rows, at - 3600 + 60) == []


@pytest.mark.parametrize("limited", ["first", "second"])
def test_a_push_limit_on_either_side_keeps_twins_apart(limited):
    # 「只推一次」(push_keep_stage, another change) on either errand: folding
    # would push the other one less, or this one more, than asked.
    at = _at(_day(3), 8, 30)
    first = _insert(CALL, at)
    second = _insert(CALL_AGAIN, at)
    limited_id = first if limited == "first" else second
    rows = [
        {**row, "push_keep_stage": "now"} if row["reminder_id"] == limited_id else row
        for row in memory.list_pending_reminders_full(G, dedupe=False)
    ]
    assert reminder_push.plan_folds(rows, at - 60) == []


def test_a_twin_of_a_row_folded_into_someone_elses_reminder_folds_too(monkeypatch):
    # All for the whole family at 14:00: 成員乙's visit absorbs 成員甲's restatement
    # of it, and 成員甲's other errand at that same clock rides along as well.
    at = _at(_day(3), 14)
    visit = _insert("去海景醫院看外婆", at, user="U_B", mentions=["全家"])
    restated = _insert("海景醫院看外婆", at, user="U_A", mentions=["全家"])
    errand = _insert("去星光診所拿藥", at, user="U_A", mentions=["全家"])
    v, r, e = _pending(visit), _pending(restated), _pending(errand)
    assert reminder_push._merges_into(r, v) or reminder_push._merges_into(v, r)
    assert reminder_push._primary_rank(v) < reminder_push._primary_rank(r)
    assert reminder_push._identity_conflict(e, reminder_push._identity_texts(v), ())
    calls = _record_pushes(monkeypatch)

    _set_now(monkeypatch, at - 3600 + 60)
    assert reminder_push.push_reminders() == 1

    assert calls[0]["reminder_id"] == visit
    assert _status(restated) == _status(errand) == "cancelled"
    assert "去星光診所拿藥" in calls[0]["text"]


def test_the_plan_marks_twins_for_the_fold():
    at = _at(_day(3), 14)
    clock = _insert("施打流感疫苗和新冠疫苗", at)
    restated = _insert("施打流感疫苗", at)
    twin = _insert("去台新銀行繳費", at)
    rows = memory.list_pending_reminders_full(G, dedupe=False)
    plans = reminder_push.plan_folds(rows, at - 3600 + 60)

    assert len(plans) == 1 and plans[0][0]["reminder_id"] == clock
    marks = {peer["reminder_id"]: peer.get("fold_twin", False) for peer in plans[0][1]}
    assert marks == {restated: False, twin: True}
    assert all("fold_twin" not in row for row in rows)        # the input rows stay untouched


def test_rows_that_are_not_twins_still_meet_the_identity_check(monkeypatch):
    # Unchanged guard (only twins skip it): two rows with no time given both
    # read as the 14:00 appointment, but once 第一劑 is absorbed 第二劑 stays.
    d = _day(3)
    clock = _insert("打新冠疫苗", _at(d, 14))
    first_dose = _insert("打新冠疫苗第一劑", _at(d, 12), kind="none")
    second_dose = _insert("打新冠疫苗第二劑", _at(d, 12), kind="none")
    c, f, s = _pending(clock), _pending(first_dose), _pending(second_dose)
    for vague in (f, s):
        assert reminder_push._merges_into(vague, c) or reminder_push._merges_into(c, vague)
    assert reminder_push._identity_conflict(
        s, reminder_push._identity_texts(c) + reminder_push._identity_texts(f), ()
    )
    _record_pushes(monkeypatch)

    _set_now(monkeypatch, _at(d, 14) - 3600 + 60)
    reminder_push.push_reminders()

    assert _status(clock) == "pending" and _status(first_dose) == "cancelled"
    assert _status(second_dose) == "pending"


def test_a_stage_the_twin_already_sent_is_not_sent_again(monkeypatch):
    # Before this rule each row pushed on its own: the 4-hour notice of one is
    # already out, so folding at that moment sends nothing more.
    at = _at(_day(3), 8, 30)
    first = _insert(CALL, at)
    second = _insert(CALL_AGAIN, at, pushed_4hr=1)
    calls = _record_pushes(monkeypatch)

    _set_now(monkeypatch, at - 4 * 3600 + 60)
    assert reminder_push.push_reminders() == 0

    assert calls == []
    assert _status(second) == "cancelled" and _row(first)["pushed_4hr"] == 1


def test_both_already_sent_then_the_next_stage_is_one_message(monkeypatch):
    at = _at(_day(3), 8, 30)
    first = _insert(CALL, at, pushed_4hr=1)
    second = _insert(CALL_AGAIN, at, pushed_4hr=1)
    calls = _record_pushes(monkeypatch)

    _set_now(monkeypatch, at - 2 * 3600 + 60)
    assert reminder_push.push_reminders() == 1

    assert len(calls) == 1 and _status(second) == "cancelled"
    assert _row(first)["pushed_2hr"] == 1


# ── the reply paths fold the same way ────────────────────────────────────────


class _FakeApiClient:
    def __init__(self, _cfg):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _fake_line(monkeypatch) -> list[list]:
    batches: list[list] = []

    class FakeMessagingApi:
        def __init__(self, _client):
            pass

        def reply_message(self, request):
            batches.append(list(request.messages))
            return SimpleNamespace(sent_messages=[])

        def push_message(self, request, **_kwargs):
            raise AssertionError("no push fallback expected")

    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: True)
    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: object())
    return batches


def _texts(batch: list) -> list[str]:
    return [str(getattr(message, "text", "") or "") for message in batch]


def test_the_reply_piggyback_carries_one(monkeypatch):
    now = int(time.time()) // 60 * 60
    first = _insert(CALL, now + 3600)
    second = _insert(CALL_AGAIN, now + 3600)
    batches = _fake_line(monkeypatch)
    _set_now(monkeypatch, now)

    assert main._reply("reply-token", "嗨", group_id=G)

    texts = _texts(batches[0])
    assert texts[0] == "嗨"
    assert sum("⏰" in t for t in texts) == 1
    assert {_status(first), _status(second)} == {"pending", "cancelled"}


def test_the_fast_path_carries_one(monkeypatch):
    now = int(time.time()) // 60 * 60
    first = _insert(CALL, now + 3600)
    second = _insert(CALL_AGAIN, now + 3600)
    batches = _fake_line(monkeypatch)
    _set_now(monkeypatch, now)

    assert main._try_piggyback_reminders_fast_path("reply-token", G)

    assert sum("⏰" in t for t in _texts(batches[0])) == 1
    assert {_status(first), _status(second)} == {"pending", "cancelled"}


def test_a_receipt_keeps_its_twin_off_the_receipt():
    at = _at(_day(3), 8, 30)
    first = _insert(CALL, at)
    second = _insert(CALL_AGAIN, at)
    assert reminder_push.same_event_ids(G, [second]) == {first}
    assert reminder_push.same_event_ids(G, [first]) == {second}


# ── the cost Andrew accepted: cancelling either afterwards cancels both ──────


def _cancel_event(text: str, quoted_message_id: str) -> MessageEvent:
    message = MagicMock(spec=TextMessageContent)
    message.id = "incoming-cancel"
    message.text = text
    message.mention = None
    message.quoted_message_id = quoted_message_id
    message.quote_token = "quote-token"
    message.type = "text"
    source = MagicMock(spec=GroupSource)
    source.group_id = G
    source.user_id = "U_A"
    event = MagicMock(spec=MessageEvent)
    event.message = message
    event.source = source
    event.reply_token = "reply-token"
    event.delivery_context = SimpleNamespace(is_redelivery=False)
    return event


def _quote_cancel(monkeypatch, quoted_message_id: str) -> list[str]:
    replies: list[str] = []

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("cancellation must stop all later routing")

    for name in ("_try_one_shot_reply", "_try_handle_calendar_correction",
                 "_auto_capture_text_if_important", "_maybe_extract_reminder"):
        monkeypatch.setattr(main, name, must_not_run)
    monkeypatch.setattr(main, "_reply", lambda _token, text, **_kw: replies.append(text))
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)
    main._handle_text_message(_cancel_event("這則取消", quoted_message_id), G)
    return replies


def test_quoting_the_twins_older_push_cancels_the_whole_event(monkeypatch):
    at = _at(_day(3), 8, 30)
    first = _insert(CALL, at)
    second = _insert(CALL_AGAIN, at)
    sent = iter(["old-twin-push"])

    class FakeMessagingApi:
        def __init__(self, _client):
            pass

        def push_message(self, _request, x_line_retry_key=None):
            return SimpleNamespace(sent_messages=[SimpleNamespace(id=next(sent))])

    monkeypatch.setattr(reminder_push, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(reminder_push, "MessagingApi", FakeMessagingApi)
    monkeypatch.setattr(reminder_push, "_line_access_token", lambda: "token")
    # The twin's own notice went out before the two were folded.
    assert reminder_push._push_to_group(G, f"⏰ 提醒\n08:30 {CALL_AGAIN}", reminder_id=second)
    _record_pushes(monkeypatch)
    _set_now(monkeypatch, at - 2 * 3600 + 60)
    reminder_push.push_reminders()
    assert _status(second) == "cancelled" and _status(first) == "pending"

    replies = _quote_cancel(monkeypatch, "old-twin-push")

    assert len(replies) == 1 and "已取消提醒" in replies[0]
    assert _status(first) == "cancelled"


# ── everything else folds only as before ─────────────────────────────────────


def test_different_people_for_the_whole_family_at_one_clock_stay_apart(monkeypatch):
    at = _at(_day(3), 14)
    first = _insert("繳停車費", at, user="U_A", mentions=["全家"])
    second = _insert("去郵局寄包裹", at, user="U_C", mentions=["全家"])
    assert reminder_push._reminder_audience(_pending(first)) == reminder_push._reminder_audience(
        _pending(second)
    )
    calls = _record_pushes(monkeypatch)

    _set_now(monkeypatch, at - 3600 + 60)
    assert reminder_push.push_reminders() == 2

    assert len(calls) == 2 and _status(first) == _status(second) == "pending"
    assert reminder_push.same_event_ids(G, [first]) == set()


def test_rows_without_an_author_are_left_to_the_matcher(monkeypatch):
    at = _at(_day(3), 14)
    first = _insert("繳停車費", at, user="")
    second = _insert("去郵局寄包裹", at, user="")
    _set_now(monkeypatch, at - 3600 + 60)
    assert reminder_push.fold_due_duplicates() == 0
    assert _status(first) == _status(second) == "pending"


def test_one_minute_apart_stays_apart(monkeypatch):
    d = _day(3)
    first = _insert(CALL, _at(d, 8, 30))
    second = _insert(CALL_AGAIN, _at(d, 8, 31))
    calls = _record_pushes(monkeypatch)

    _set_now(monkeypatch, _at(d, 8, 31) - 3600 + 60)
    assert reminder_push.push_reminders() == 2

    assert len(calls) == 2 and _status(first) == _status(second) == "pending"
    assert reminder_push.same_event_ids(G, [first]) == set()


@pytest.mark.parametrize(
    "kinds",
    [("clock", None), ("clock", "none"), ("clock", "daypart:早上"), ("none", "none"), (None, None)],
)
def test_a_vague_or_legacy_time_is_not_a_clock(monkeypatch, kinds):
    at = _at(_day(3), 9)
    first = _insert("繳停車費", at, kind=kinds[0])
    second = _insert("去郵局寄包裹", at, kind=kinds[1])
    now = at - 3600 + 60
    assert any(reminder_stages.open_stage(_pending(rid), now) for rid in (first, second))
    _set_now(monkeypatch, now)

    assert reminder_push.fold_due_duplicates() == 0
    assert _status(first) == _status(second) == "pending"
    assert reminder_push.same_event_ids(G, [first]) == set()


def test_a_different_audience_at_one_clock_stays_apart(monkeypatch):
    at = _at(_day(3), 14)
    first = _insert("繳停車費", at)
    second = _insert("去郵局寄包裹", at, mentions=["成員乙"])
    _set_now(monkeypatch, at - 3600 + 60)
    assert reminder_push.fold_due_duplicates() == 0
    assert _status(first) == _status(second) == "pending"


@pytest.mark.parametrize(
    "never_folded",
    [
        {"action": "成員甲 看牙醫", "source_kind": "calendar_event", "source_ref": "E-1"},
        {"action": "成員甲 看牙醫（當天提醒）", "source_kind": "contextual_date_once",
         "source_ref": "M1:same:0"},
        {"action": "繳停車費（前一天提醒）"},
    ],
    ids=["calendar-mirror", "contextual-same-day", "offset-label"],
)
def test_rows_the_fold_never_touches_stay_apart_at_one_clock(monkeypatch, never_folded):
    at = _at(_day(3), 14)
    natural = _insert("去郵局寄包裹", at)
    special = _insert(
        never_folded["action"],
        at,
        source_kind=never_folded.get("source_kind", ""),
        source_ref=never_folded.get("source_ref", ""),
    )
    _set_now(monkeypatch, at - 3600 + 60)
    assert reminder_push.fold_due_duplicates() == 0
    assert _status(natural) == _status(special) == "pending"
