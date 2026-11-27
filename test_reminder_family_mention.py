"""Stage 2 of the reminder-path @all work: who hears a reminder (Andrew 2026-11-13/14, D1, D2, D5).

- D1: a family member's own message that @-calls everyone (``line_mentions.whole_family_mentions``)
  makes a reminder that rings everyone when it falls due.  全家 joins its aliases at each write point
  (live, the queue's drain, a schedule list, a creation follow-up), read off the message only, never
  off the words a model or a parser made of it.
- The stored words lose that @ (``strip_whole_family_mentions``), so a receipt or a list that reads
  them back never rings everyone (D2).  Whether a receipt rings: test_model_all_mention.py.
- D5: a reminder for everyone that folds into another reminder, or into an event's mirror, keeps
  ringing everyone.  Only 全家 joins, never the newcomer's people (7c6a395), and 全家 is never a
  person when the matcher asks whether two reminders are one event.
- Rounds 2 and 3 (2026-11-20/21): an @全家 that is FamilyMart (「取件@全家」, 「@全家取貨」) calls nobody,
  every other one does; a capture for everyone with no message of its own makes the event it lands on
  everyone's, or, when that event cannot take it, is an event of its own as before (one with the same title,
  date and time is still dropped by the unique index, as before); two reminders for
  everyone of one short-titled event (「全家聚餐」) stay one; the strip goes on until no call is left.
- Round 4 (2026-11-27, F1-F4): two reminders for everyone of one short-titled event stay one when the strip
  took 全家 out of their words too (a reminder for everyone next to one that is not pairs as before); two
  events that both ring everyone are never apart for the other people they name, and a capture that an
  event would have taken before stage 2 is no event of its own; a burst for everyone that the unique index
  drops goes into that event; an @全家 that is FamilyMart is the shop to names_the_family as well.

A fold is checked by the pushes due at the moment and whether one of them is for everyone: an outcome
alone lets a broken fold pass.  Members are conftest's synthetic ones (測試成員甲／乙), every date is
in the future, and nothing reaches the network.

Plan and reviews: ops/state/line_bot_reminder_extract_mention_20261113/.
"""

from __future__ import annotations

import itertools
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import calendar_db
import gemini_client
import line_mentions
import main
import memory
import reminder_overview
import reminder_push
import test_schedule_capture as schedule
from test_calendar_last_tier import inline_spawn
from test_gemini_last_tier import LITE, TIER, resp, tier  # noqa: F401  tier is a fixture
from test_reminder_extract_claude import _answer as claude_answer, api as claude_api, claude  # noqa: F401  claude is a fixture

TW = ZoneInfo("Asia/Taipei")
GROUP, USER = "G_FAMILY_AT", "U_TEST_A"


def _half(text: str) -> str:
    """Addresses are written with 「＠」: the privacy audit refuses a literal one anywhere in the tree."""
    return text.replace("＠", "@")


# ── the stored words ──────────────────────────────────────────────────────────────────────────

# Seat 3's sentences (stage 2 pre-review) and the glued table of PLAN_STAGE2_v2 §4.1.
STRIP_INPUTS = [
    "@全家人一起吃飯",
    "@大家好 明天開會",
    "@全家福拍照",
    "@全部的人都要到",
    "@全家8點吃飯",
    "@All.",
    "提醒 @All.",
    "@all, 明天開會",
    "@All，明天開會",
    "＠全家 聚餐",
    "https://example.invalid/?x=@all 開會",
    "https://example.invalid/@all 開會",
    _half("寄到 me＠all.example 開會"),
    "@allen 開會",
    "@全家",
    "@all",
    "@全家 @all",
    "@全家@all",
    "@all.",
    "@全家：明天10點",
    "@全家，明天10點",
    "@all吃飯",
    "@everyone開會",
    "@ALL開會",
    "@全家便利商店取貨",
    "**@all** 開會",
    "@全家\n聚餐",
    "開會　@全家　帶便當",
    "大家@all吃飯",
    "@全員集合",
    "通知@全家：明天10點",
    "@all開會 @測試成員甲",
    # R4 (seats 1, 2, 3): a call glued to the one before it comes into view once that one is out
    "@ALL@大家好 開會",
    "@everyone@全員 集合",
    "@all＠全家 聚餐",
    # R1: FamilyMart's @全家 stays in the words
    "取件@全家",
    "@all 取件@全家",
    "@大家 去@全家取貨",
]
GLUED = [
    ("@全家人一起吃飯", "全家人一起吃飯"),
    ("@大家好 明天開會", "大家好 明天開會"),
    ("@全家8點吃飯", "全家8點吃飯"),
    ("@全家 聚餐", "聚餐"),
    ("@all 開會", "開會"),
    ("@all吃飯", "吃飯"),
    ("＠全家 聚餐", "聚餐"),
    ("@全家", "全家"),
    ("@全家 @all", "全家"),
    ("@all.", "全家"),
    # R4: two calls glued together both go
    ("@all@全家 聚餐", "聚餐"),
    ("@All@大家好", "大家好"),
    ("@everyone@所有人 開會", "開會"),
    ("@all＠全家", "全家"),
    ("＠All＠全家人一起吃飯", "全家人一起吃飯"),
    ("提醒@all@全家人吃飯", "提醒全家人吃飯"),
]
UNTOUCHED = ["@allen 開會", _half("寄到 me＠all.example"), "https://example.invalid/@all"]


@pytest.mark.parametrize("text", STRIP_INPUTS + [text for text, _kept in GLUED] + UNTOUCHED)
def test_stored_words_never_call_everyone(text):
    out = line_mentions.strip_whole_family_mentions(text)

    assert not line_mentions.whole_family_mentions(out)
    assert not line_mentions.has_all_mention(out)
    assert out.strip()
    assert line_mentions.strip_whole_family_mentions(out) == out
    if not line_mentions.whole_family_mentions(text):
        assert out == text


@pytest.mark.parametrize(("text", "kept"), GLUED)
def test_a_glued_chinese_word_keeps_its_letters_and_an_english_one_goes_whole(text, kept):
    # 「@全家人一起吃飯」 means 全家人: only the @ goes.  An English word has a right edge, so it goes whole.
    assert line_mentions.strip_whole_family_mentions(text) == kept


@pytest.mark.parametrize("text", UNTOUCHED)
def test_words_that_call_nobody_are_left_alone(text):
    assert line_mentions.strip_whole_family_mentions(text) == text


def test_taking_a_call_out_makes_every_run_of_spaces_one():
    # R5: what the strip does to the spaces, as its docstring says (seat 4 N3).
    assert line_mentions.strip_whole_family_mentions("開會　@全家　帶便當") == "開會 帶便當"


# Seat 3's blocks with 「取件」 and two whole calls, every string of two or three of them (R4).
_BLOCKS = [
    "@", "＠", "all", "All", "everyone", "全家", "大家", "所有人", "人", "好", " ", "　", ".", "，", "x", "1",
    "/", "-", "=", "開會", "​", "取件", "@all", "@全家",
]


def test_the_strip_keeps_its_promises_on_every_short_string():
    for size in (2, 3):
        for blocks in itertools.product(_BLOCKS, repeat=size):
            text = "".join(blocks)
            out = line_mentions.strip_whole_family_mentions(text)

            assert not line_mentions.whole_family_mentions(out), (text, out)
            assert line_mentions.strip_whole_family_mentions(out) == out, (text, out)
            if line_mentions.whole_family_mentions(text):
                assert out.strip(), text
            else:
                assert out == text, text


# ── an @全家 that is FamilyMart (R1, 2026-11-20) ──────────────────────────────────────────────

# The main session's 26 sentences (POST_DECISIONS_STAGE2 R1), the reviewers' calls (round 3) and a few more.
# FamilyMart is two ways only (R6): an errand at the shop glued to the @, or, with the @ out, a word of the
# shop's right after 全家 (the first of names_the_family's shop rules, _FAMILYMART_RE).  Every other @全家 is
# a call, 去 or 到 in front included.
AT_THE_SHOP = [
    ("取件@全家", []),
    ("取貨@全家便利商店", []),
    ("寄包裹＠全家", []),
    ("去@全家領包裹", []),
    ("去@全家取貨", []),
    ("@全家便利商店 取貨", []),
    ("拿包裹@全家", []),
    ("取貨@全家", []),
    ("繳費@全家", []),
    ("@全家門口集合", ["全家"]),  # 全家門口 is the family (Andrew 11/12)
    ("到@全家門口集合", ["全家"]),  # 去 or 到 with other words: a call (a known leftover, R6)
    ("@全家 明天聚餐", ["全家"]),
    ("明天聚餐 @全家", ["全家"]),
    ("提醒@全家 明天帶傘", ["全家"]),
    ("去@全家人的聚會", ["全家"]),
    ("輪到@全家打疫苗", ["全家"]),
    ("@全家8點吃飯", ["全家"]),
    ("@全家人一起吃飯", ["全家"]),
    ("全家一起去@全家取貨", []),
    ("@全家 去全家取貨", ["全家"]),
    ("@大家 去@全家取貨", ["大家"]),
    ("@all 取件@全家", ["all"]),
    ("買咖啡@全家", ["全家"]),  # no word of the shop's on either side: still a call (a known leftover)
    ("見面@全家", ["全家"]),
    ("@全家藥局 拿藥", []),
    ("@全家 藥局拿藥", ["全家"]),
    # A space or a new line between the errand and the @: the @ calls everyone, as the end of a sentence
    # does (「記得繳費 @全家」); only an @ glued to the errand is 「at」 the shop.
    ("取件 ＠全家", ["全家"]),
    ("取件\n@全家", ["全家"]),
    ("明天下午3點去郵局寄包裹 @全家", ["全家"]),
    # A word of the shop's right after 全家 is the shop whatever comes before the @ (seat 4 I-2).
    ("明天 @全家取貨", []),
    # Glued to an errand, but 全家人, 全家福, 全家聚... are the family (R6).
    ("取件@全家人", ["全家"]),
    ("繳費@全家人", ["全家"]),
    ("取件@全家福", ["全家"]),  # seat 4 round 3 n-a: more than 人 is pinned
    ("取件@全家一起", ["全家"]),
    # Only the Chinese 全家 can be the shop (seat 4 n2).
    ("取件@all", ["all"]),
    ("繳費@大家", ["大家"]),
    # 去 or 到 in front: a call (seat 1 G1, seat 3 I-a, the main session's four; R6).
    ("聚餐不要遲到@全家", ["全家"]),
    ("婚禮大家都要到@全家", ["全家"]),
    ("大家都要去@全家！", ["全家"]),
    ("記得準時到@全家喔", ["全家"]),
    ("不要遲到@全家 明天見", ["全家"]),
    ("阿嬤生日大家都要去@全家", ["全家"]),
    ("一起去@全家", ["全家"]),
    ("記得去@全家", ["全家"]),
    ("明天下午去一趟@全家", ["全家"]),
    ("明天8點出發不要遲到@全家", ["全家"]),
    ("明天上午9點記得報到@全家", ["全家"]),
    ("明天9點準時到@全家", ["全家"]),
    ("明天9點簽到@全家", ["全家"]),
    ("晚上7點聚餐大家一定要到@全家", ["全家"]),
    ("晚上7點聚餐大家都要到@全家", ["全家"]),
    ("明天下午3點開會要通知到@全家", ["全家"]),
    ("明天下午3點開會要提醒到@全家", ["全家"]),
    ("週六下午3點一起去@全家", ["全家"]),
    ("明天我不去@全家", ["全家"]),
    ("晚上回去@全家 一起吃飯", ["全家"]),
    ("明天8點出發不要遲到@全家喔", ["全家"]),
    ("大家一定要到@全家哦", ["全家"]),
    ("週六一起去@全家吧", ["全家"]),
    ("8點到@全家囉", ["全家"]),
    ("記得報到@全家啦", ["全家"]),
    ("明天8點出發不要遲到 @全家", ["全家"]),
    ("大家都要去@全家參加婚禮", ["全家"]),
    ("記得到@全家吃飯", ["全家"]),
    ("晚上回去@全家吃飯", ["全家"]),
    ("明天到@全家集合拍照", ["全家"]),
    # ... and with other words after 全家 too (known leftovers, like 「買咖啡@全家」): read as a call.
    ("去@全家買牛奶", ["全家"]),
    ("到@全家集合", ["全家"]),
]


@pytest.mark.parametrize(("text", "calls"), AT_THE_SHOP)
def test_an_at_that_means_familymart_calls_nobody(text, calls):
    assert line_mentions.whole_family_mentions(text) == calls


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("取件@全家", "取件@全家"),
        ("@全家便利商店取貨", "@全家便利商店取貨"),
        ("@all 取件@全家", "取件@全家"),
        ("@大家 去@全家取貨", "去@全家取貨"),
    ],
)
def test_familymarts_at_stays_in_the_words(text, kept):
    assert line_mentions.strip_whole_family_mentions(text) == kept


# F4 (2026-11-27, seat 1 G2): names_the_family, which the medical actor and a title's 全家 read, takes an @全家
# that is FamilyMart (_at_the_shop) for the shop as well, as D1 does; every other @全家 is still the family.
@pytest.mark.parametrize(
    ("text", "family"),
    [
        ("取件@全家", False),
        ("取件＠全家", False),
        ("寄包裹@全家 明天看牙醫", False),
        ("去@全家取貨", False),
        ("@全家 聚餐", True),
        ("取件@全家人", True),
        ("不要遲到@全家", True),
        ("取件@全家 全家聚餐", True),  # the other 全家 is still the family
    ],
)
def test_names_the_family_reads_familymarts_at_as_the_shop(text, family):
    assert line_mentions.names_the_family(text) is family


@pytest.mark.parametrize(
    ("text", "patient"),
    [
        ("明天下午3點取件@全家 順便看牙醫", "測試成員甲"),
        ("取件＠全家 明天看牙醫", "測試成員甲"),
        ("@全家 明天看牙醫", "全家"),
        ("取件@全家人 明天看牙醫", "全家"),
    ],
)
def test_familymarts_at_leaves_the_visit_with_the_sender(monkeypatch, text, patient):
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "測試成員甲")

    assert main._infer_medical_actor(text, "U_TEST_A") == patient


# Cutting the shop's @ out only takes a 全家 away: on every string of up to three of these blocks the family is
# found only where the reading before F4 found it, and exactly there when no @ is written.
FAMILY_BLOCKS = ["取件", "@全家", "＠全家", "全家", "人", "便利商店", "去", "到", " ", "看牙醫"]


def test_names_the_family_never_finds_more_family_than_before():
    for size in (1, 2, 3):
        for parts in itertools.product(FAMILY_BLOCKS, repeat=size):
            text = "".join(parts)
            before = "全家" in line_mentions._FAMILYMART_RE.sub(" ", text)
            now = line_mentions.names_the_family(text)
            assert before or not now, text
            if "@" not in text and "＠" not in text:
                assert now == before, text


# ── the write points ──────────────────────────────────────────────────────────────────────────


def _day(days: int = 12):
    return (datetime.now(TW) + timedelta(days=days)).date()


def _when(day, clock: str = "下午3點") -> str:
    return f"{day.month}月{day.day}日{clock}"


def _six(day, *, hour: int = 15, minute: int = 0, action: str = "去郵局寄包裹") -> dict:
    return {"action": action, "year": day.year, "month": day.month, "day": day.day, "hour": hour, "minute": minute}


def _answer(six: dict) -> str:
    return json.dumps(six, ensure_ascii=False)


def _rows(group: str = GROUP) -> list[dict]:
    with memory._conn() as c:
        rows = c.execute(
            "SELECT reminder_id, user_id, action, mention_aliases, COALESCE(source_kind, ''), "
            "COALESCE(source_ref, '') FROM reminders WHERE group_id=? AND status='pending' "
            "ORDER BY remind_at, reminder_id",
            (group,),
        ).fetchall()
    return [
        {
            "reminder_id": row[0],
            "user_id": row[1],
            "action": row[2],
            "mention_aliases": json.loads(row[3] or "[]"),
            "source_kind": row[4],
            "source_ref": row[5],
        }
        for row in rows
    ]


def _row(group: str = GROUP) -> dict:
    rows = _rows(group)
    assert len(rows) == 1, f"expected one reminder, found {len(rows)}"
    return rows[0]


def _rings_everyone(row: dict) -> bool:
    """What every push of this row does when it falls due (reminder_push builds it this way)."""
    targets = line_mentions.reminder_actor_targets(row["user_id"], row["mention_aliases"], row["action"])
    return any(target.kind == "all" for target in targets)


def _calls_nobody(words: str) -> bool:
    return not line_mentions.whole_family_mentions(words) and not line_mentions.has_all_mention(words)


# W1: live, through the model.  No 咪寶, no 提醒我, nothing medical, so only the model reads it, and the
# model's words carry no @: the call is read off the message.
@pytest.mark.parametrize("call", ["@all", "@All", "＠all", "@全家", "＠全家", "@大家", "@everyone", "@大家好"])
def test_a_message_that_calls_everyone_makes_a_reminder_for_everyone(tier, call):
    day = _day()
    fake = tier(generate={LITE: resp(_answer(_six(day)))})

    receipt = main._maybe_extract_reminder(f"{call} {_when(day)}去郵局寄包裹", GROUP, USER, message_id="M_W1")

    assert fake.generated == [LITE]
    row = _row()
    assert row["mention_aliases"] == ["全家"]
    assert row["action"] == "去郵局寄包裹"
    assert _rings_everyone(row)
    # The receipt reads the stored words back: no @all line, 全家 in front of the 事項.
    assert "@all" not in receipt
    assert "事項：全家 去郵局寄包裹" in receipt.splitlines()


# W10 (2026-11-27): since reminders from chat ask Claude first (77d68c1), Claude's answer takes the same last
# step: the call is read off the message, never off Claude's words, and Gemini is not asked.
def test_claudes_answer_rings_everyone_for_a_message_that_calls_everyone(claude, tier):
    day = _day()
    fake = tier(generate={LITE: resp(_answer(_six(day)))})
    claude(claude_api(claude_answer(action="去郵局寄包裹", day=day)))

    receipt = main._maybe_extract_reminder(f"@全家 {_when(day)}去郵局寄包裹", GROUP, USER, message_id="M_W10")

    assert fake.generated == []
    row = _row()
    assert row["mention_aliases"] == ["全家"]
    assert row["action"] == "去郵局寄包裹"
    assert _rings_everyone(row)
    assert "@all" not in receipt
    assert "事項：全家 去郵局寄包裹" in receipt.splitlines()


def test_the_models_words_never_decide_who_hears_it(monkeypatch):
    # The model's 事項 carries 「@all」 and the message has no call: the stored words lose it all the same,
    # and nobody else is added.
    day = _day()
    asked: list[str] = []

    def model(text, *_a, **_k):
        asked.append(text)
        return _six(day, action="@all 開會")

    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(gemini_client, "extract_reminder", model)

    receipt = main._maybe_extract_reminder(f"{_when(day)}開會", GROUP, USER, message_id="M_W1R")

    assert len(asked) == 1
    row = _row()
    assert row["mention_aliases"] == []
    assert row["action"] == "開會"
    assert not _rings_everyone(row)
    assert "@all" not in receipt


@pytest.mark.parametrize(
    "words",
    ["@allen", _half("x＠all.example"), "https://example.invalid/@all", "全家"],
)
def test_these_call_nobody(tier, words):
    day = _day()
    tier(generate={LITE: resp(_answer(_six(day)))})

    main._maybe_extract_reminder(f"{_when(day)}去郵局寄包裹 {words}", GROUP, USER, message_id="M_W2")

    row = _row()
    assert row["mention_aliases"] == []
    assert not _rings_everyone(row)


def test_an_all_in_a_link_query_still_calls_everyone(tier):
    # As decided 11/12: nothing glued on its left, so 「?x=@all」 reads as a call.
    day = _day()
    tier(generate={LITE: resp(_answer(_six(day)))})

    main._maybe_extract_reminder(
        f"{_when(day)}去郵局寄包裹 https://example.invalid/?x=@all", GROUP, USER, message_id="M_W2Q"
    )

    assert _row()["mention_aliases"] == ["全家"]


def test_a_member_whose_name_starts_with_a_family_word_reads_as_everyone(tier):
    # A known limit, gated before deploy (PLAN_STAGE2_v2 §5, C1): 「@大家好」 reads as 大家 whatever a
    # member's display name is.  If C1 finds such a name, a known-name mask goes in and this flips.
    Path(os.environ["LINE_MEMBER_DISPLAY_NAMES_PATH"]).write_text(
        json.dumps({"U_TEST_B": "大家好"}, ensure_ascii=False), encoding="utf-8"
    )
    day = _day(1)
    tier(generate={LITE: resp(_answer(_six(day, hour=10, action="看牙醫")))})

    main._maybe_extract_reminder("@大家好 明天10點看牙醫", GROUP, USER, message_id="M_W2N")

    row = _row()
    assert line_mentions.is_all_participants(row["mention_aliases"])
    assert _rings_everyone(row)


# R1 (seat 1 F1, seat 3 I1): 「@」 for "at" FamilyMart.  The model kept the @ in its 事項: it stays there.
@pytest.mark.parametrize("words", ["取件@全家", "取貨@全家便利商店", "寄包裹＠全家", "去@全家領包裹"])
def test_familymarts_at_makes_no_reminder_for_everyone(tier, words):
    day = _day()
    tier(generate={LITE: resp(_answer(_six(day, action=words)))})

    receipt = main._maybe_extract_reminder(f"{_when(day)}{words}", GROUP, USER, message_id="M_SHOP")

    row = _row()
    assert row["mention_aliases"] == []
    assert row["action"] == words
    assert not _rings_everyone(row)
    assert "事項：全家" not in str(receipt)


@pytest.mark.parametrize(("words", "action"), [("@全家門口集合", "門口集合"), ("@全家 去全家取貨", "去全家取貨")])
def test_a_call_next_to_the_shops_words_still_calls_everyone(tier, words, action):
    day = _day()
    tier(generate={LITE: resp(_answer(_six(day, action=action)))})

    main._maybe_extract_reminder(f"{_when(day)}{words}", GROUP, USER, message_id="M_SHOP2")

    row = _row()
    assert row["mention_aliases"] == ["全家"]
    assert _rings_everyone(row)


@pytest.mark.parametrize(
    ("text", "title", "aliases"),
    [("明天下午3點取件@全家", "取件", []), ("明天下午3點@全家 門口集合", "門口集合", ["全家"])],
)
def test_an_events_mirror_reads_familymarts_at_the_same_way(text, title, aliases):
    # The mirror reads its own message's @ through whole_family_mentions too (since 11/07), so it rings
    # less as well (seat 1 c5).
    _at_ts, day = _at(hh=15)
    memory.log_raw_message(GROUP, "m-shop", "U_TEST_A", text, index_for_recall=False)

    calendar_db.insert_event_with_outcome(
        group_id=GROUP, title=title, event_date=day, event_time="15:00", location=None,
        participants=[], event_type="family_gathering", source_msg_id="m-shop",
    )

    assert _mirror_aliases(GROUP) == aliases


# W4: the queue's drain writes through memory.complete_pending_reminder.
def test_a_queued_message_that_calls_everyone_rings_everyone(tier, monkeypatch):
    day = _day()
    fake = tier(generate={LITE: resp(_answer(_six(day)))})
    assert memory.enqueue_pending_reminder(GROUP, USER, f"@all {_when(day)}去郵局寄包裹", "M_W4")
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)

    main._drain_pending_reminders(GROUP)

    assert fake.generated == [LITE]
    row = _row()
    assert row["mention_aliases"] == ["全家"]
    assert row["action"] == "去郵局寄包裹"
    assert _rings_everyone(row)


def test_a_queued_request_the_local_parser_reads_rings_everyone(monkeypatch):
    # The local parser keeps the @ in its 事項 (「開會 @all」) and gives no aliases.
    schedule._no_model(monkeypatch)
    assert memory.enqueue_pending_reminder(GROUP, USER, "提醒我明天10:00開會 @all", "M_W4L")

    main._drain_pending_reminders(GROUP)

    row = _row()
    assert row["mention_aliases"] == ["全家"]
    assert row["action"] == "開會"
    assert _rings_everyone(row)


# W5: a schedule list.  An @全家 on any dated line is the message's: every line rings everyone (Q5).
def _list_text() -> tuple[date, date, str]:
    base = schedule._base_day()
    second = base + timedelta(days=1)
    return base, second, f"{schedule._md(base)} @全家 去海邊一日遊\n{schedule._md(second)} 市區自由行"


def test_a_schedule_list_that_calls_everyone_rings_everyone_on_every_line(monkeypatch):
    schedule._no_model(monkeypatch)
    base, second, text = _list_text()

    receipt = main._maybe_extract_reminder(text, schedule.G, schedule.U_SENDER, "m-trip")

    rows = _rows(schedule.G)
    assert [(row["action"], row["mention_aliases"]) for row in rows] == [
        ("去海邊一日遊", ["全家"]),
        ("市區自由行", ["全家"]),
    ]
    assert all(_rings_everyone(row) for row in rows)
    lines = receipt.splitlines()
    assert lines[0] == "已新增 2 筆提醒"
    assert f"{base:%Y-%m-%d} 12:00 全家 去海邊一日遊" in lines
    assert f"{second:%Y-%m-%d} 12:00 全家 市區自由行" in lines
    assert "@all" not in receipt and "@全家" not in receipt


def test_a_quoted_schedule_list_that_calls_everyone_rings_everyone(monkeypatch):
    sent, _explicit = schedule._capture_setup(monkeypatch)
    _base, _second, text = _list_text()
    schedule._seed_raw(text)

    main._handle_text_message(
        schedule._text_event("咪寶", message_id="m-cmd", user_id=schedule.U_OTHER, quoted="m-src"), schedule.G
    )

    rows = _rows(schedule.G)
    assert [(row["action"], row["mention_aliases"]) for row in rows] == [
        ("去海邊一日遊", ["全家"]),
        ("市區自由行", ["全家"]),
    ]
    assert len(sent) == 1 and sent[0].startswith("已新增 2 筆提醒")


def test_a_follow_up_on_a_dated_list_that_calls_everyone_rings_everyone():
    # 「加到提醒」 on a message with two dated and timed lines that the calendar regex reads as events:
    # _followup_multi_event_receipt writes them.
    base = schedule._base_day()
    text = f"{schedule._md(base)} 18:00 @全家 聚餐\n{schedule._md(base + timedelta(days=1))} 10:00 回老家"
    now = int(datetime.now(TW).timestamp())
    with memory._conn() as c:
        c.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) VALUES (?, 'm-fsrc', ?, ?, ?)",
            (schedule.G, schedule.U_SENDER, text, now - 30),
        )
        c.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, 'm-fcmd', ?, '加到提醒', ?)",
            (schedule.G, schedule.U_SENDER, now),
        )

    reply = main._creation_followup_reply(
        schedule._text_event("加到提醒", message_id="m-fcmd", quoted="m-fsrc"), schedule.G, "加到提醒"
    )

    assert isinstance(reply, main.ReminderReceipt)
    rows = _rows(schedule.G)
    assert [(row["action"], row["mention_aliases"]) for row in rows] == [
        ("聚餐", ["全家"]),
        ("回老家", ["全家"]),
    ]


def test_a_call_alone_on_the_first_line_is_no_schedule_list(tier):
    # Seat 1 I6: 「@全家」 on a line of its own leaves no schedule list; the model makes one reminder.
    base = schedule._base_day()
    text = f"@全家\n{schedule._md(base)}去海邊一日遊\n{schedule._md(base + timedelta(days=1))} 市區自由行"
    assert main._local_schedule_list_items(text) == []
    fake = tier(generate={LITE: resp(_answer(_six(base, hour=12, action="去海邊一日遊")))})

    main._maybe_extract_reminder(text, GROUP, USER, message_id="M_W5B")

    assert fake.generated == [LITE]
    row = _row()
    assert row["mention_aliases"] == ["全家"]
    assert row["action"] == "去海邊一日遊"


@pytest.mark.parametrize("read_back", [True, False], ids=["row-read-back", "row-gone"])
def test_a_one_line_schedule_that_calls_everyone_says_so_in_its_receipt(monkeypatch, read_back):
    # Seat 1 F6: with the model unavailable, one dated line goes out as a single receipt.  It names its
    # people from the row it reads back, or from the aliases it wrote when that row cannot be read.
    schedule._no_model(monkeypatch)
    if not read_back:
        monkeypatch.setattr(memory, "get_reminder", lambda *_a, **_k: None)
    base = schedule._base_day()

    receipt = main._maybe_extract_reminder(f"{schedule._md(base)} @全家 去海邊一日遊", schedule.G, schedule.U_SENDER, "m-one")

    assert _row(schedule.G)["mention_aliases"] == ["全家"]
    assert "事項：全家 去海邊一日遊" in str(receipt).splitlines()


# W6: the creation follow-up (「加到提醒」 on a message with no weekend in it).
def test_a_creation_follow_up_on_a_message_that_called_everyone_rings_everyone():
    now = int(datetime.now(TW).timestamp())
    with memory._conn() as c:
        c.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, 'm-fsrc', ?, '明天下午3點去郵局寄包裹 @全家', ?)",
            (schedule.G, schedule.U_SENDER, now - 30),
        )
        c.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, 'm-fcmd', ?, '加到提醒', ?)",
            (schedule.G, schedule.U_SENDER, now),
        )
    memory.enqueue_pending_reminder(schedule.G, schedule.U_SENDER, "明天下午3點去郵局寄包裹 @全家", "m-fsrc")

    reply = main._creation_followup_reply(
        schedule._text_event("加到提醒", message_id="m-fcmd", quoted="m-fsrc"), schedule.G, "加到提醒"
    )

    row = _row(schedule.G)
    assert row["mention_aliases"] == ["全家"]
    assert row["action"] == "去郵局寄包裹"
    assert _rings_everyone(row)
    assert "@" not in reply
    assert "事項：全家 去郵局寄包裹" in reply.splitlines()


def test_a_creation_follow_up_next_to_another_messages_event_rings_once_for_everyone():
    # Seat 3 I3, seat 1 F7, pinned as it is: another message already made the same visit an event for a
    # member.  The follow-up writes a row of its own (reminder_followup.persist_source) and never reaches
    # D5, so the event keeps its participants and its offset pushes do not ring everyone (stage 2b).  At
    # the moment it is one push, for everyone: the event's mirror rides on the follow-up's row.
    group = schedule.G
    day = _day(1)
    at = int(datetime(day.year, day.month, day.day, 15, 0, tzinfo=TW).timestamp())
    memory.log_raw_message(group, "m-ev", "U_TEST_A", f"{day.isoformat()} 15:00 測試成員甲看牙醫", index_for_recall=False)
    calendar_db.insert_event_with_outcome(
        group_id=group, title="看牙醫", event_date=day.isoformat(), event_time="15:00", location=None,
        participants=["測試成員甲"], event_type="medical", source_msg_id="m-ev",
    )
    now = int(datetime.now(TW).timestamp())
    with memory._conn() as c:
        c.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, 'm-fsrc', ?, '明天下午3點看牙醫 @全家', ?)",
            (group, schedule.U_SENDER, now - 30),
        )
        c.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, 'm-fcmd', ?, '加到提醒', ?)",
            (group, schedule.U_SENDER, now),
        )

    reply = main._creation_followup_reply(
        schedule._text_event("加到提醒", message_id="m-fcmd", quoted="m-fsrc"), group, "加到提醒"
    )

    assert isinstance(reply, main.ReminderReceipt)
    assert _participants(group) == [["測試成員甲"]]
    assert _pushes(group, at) == [True]


# W7: the medical step replaces the aliases with the patient; the call to everyone is added after it.
@pytest.mark.parametrize(
    ("text", "aliases"),
    [
        ("@全家 明天10點看牙醫", ["全家"]),  # the patient is 全家: once, not twice
        ("@全家 明天10點媽媽看牙醫", ["全家", "媽媽"]),
    ],
)
def test_the_medical_step_keeps_a_call_to_everyone(tier, text, aliases):
    day = _day(1)
    tier(generate={LITE: resp(_answer(_six(day, hour=10, action="看牙醫")))})

    main._maybe_extract_reminder(text, GROUP, USER, message_id="M_W7")

    row = _row()
    assert row["mention_aliases"] == aliases
    assert _rings_everyone(row)


# W8: preserve_transit_details puts the whole message, @all and all, back into the words.
_TRANSIT = (
    "我明天早上08:10從測試站出發 等72號公車\n\n"
    "因周末班次少。間隔15-25分\n\n需08:10出門 接09:40的X02A國道快速線"
)


def test_a_transit_message_keeps_its_lines_and_loses_the_call(monkeypatch):
    day = _day(1)
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *_a, **_k: _six(day, hour=8, minute=10, action="從測試站出發等72號公車"),
    )

    main._maybe_extract_reminder("@all " + _TRANSIT, GROUP, USER, message_id="M_W8")

    row = _row()
    assert row["action"] == _TRANSIT
    assert _calls_nobody(row["action"])
    assert row["mention_aliases"] == ["全家"]


# W7 and W8 in the queue's drain (seat 1 F2): there too the call comes last, after the medical step and
# the transit step.
@pytest.mark.parametrize(
    ("text", "answer", "aliases", "words"),
    [
        ("@全家 明天10點媽媽看牙醫", {"action": "看牙醫", "hour": 10, "minute": 0}, ["全家", "媽媽"], None),
        ("@all " + _TRANSIT, {"action": "從測試站出發等72號公車", "hour": 8, "minute": 10}, ["全家"], _TRANSIT),
    ],
    ids=["medical", "transit"],
)
def test_the_drain_adds_the_call_after_the_medical_and_transit_steps(monkeypatch, text, answer, aliases, words):
    day = _day(1)
    monkeypatch.setattr(
        gemini_client, "extract_reminder", lambda *_a, **_k: dict(answer, year=day.year, month=day.month, day=day.day)
    )
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    assert memory.enqueue_pending_reminder(GROUP, USER, text, "M_W4X")

    main._drain_pending_reminders(GROUP)

    row = _row()
    assert row["mention_aliases"] == aliases
    assert _calls_nobody(row["action"])
    assert words is None or row["action"] == words


# ── folding (D5), the pushes due at the moment ───────────────────────────────────────────────


def _at(days: int = 4, hh: int = 18, mm: int = 0) -> tuple[int, str]:
    day = datetime.now(TW).date() + timedelta(days=days)
    return int(datetime(day.year, day.month, day.day, hh, mm, tzinfo=TW).timestamp()), day.isoformat()


def _pushes(group: str, at: int) -> list[bool]:
    """[whether that push is for everyone, ...] for the pushes due at ``at`` (a mirror riding on a
    reminder is one push)."""
    items = reminder_push._due_reminder_items(group, now=at)
    riders = reminder_push._mirror_riders(items)
    return [
        "all:" in reminder_push._reminder_audience(item)
        for index, item in enumerate(items)
        if index not in riders
    ]


def _participants(group: str) -> list[list[str]]:
    with memory._conn() as c:
        return [
            json.loads(row[0] or "[]")
            for row in c.execute(
                "SELECT participants FROM events WHERE group_id=? AND status='active' ORDER BY created_at",
                (group,),
            )
        ]


def _mirror_aliases(group: str) -> list[str]:
    (row,) = [row for row in _rows(group) if row["source_kind"] == calendar_db.EVENT_REMINDER_SOURCE_KIND]
    return row["mention_aliases"]


def _sync_all(group: str) -> None:
    with memory._conn() as c:
        ids = [
            row[0]
            for row in c.execute("SELECT event_id FROM events WHERE group_id=? AND status='active'", (group,))
        ]
    for event_id in ids:
        event = calendar_db.get_active_event_by_id(group, event_id)
        if event:
            calendar_db.synchronize_pending_event_reminder_mirror(event)


def _event(group: str, title: str, day: str, hh: int, participants: list[str], source: str = ""):
    if source:
        memory.log_raw_message(group, source, "U_TEST_A", f"{day} {hh}:00 {title}", index_for_recall=False)
    return calendar_db.insert_event_with_outcome(
        group_id=group, title=title, event_date=day, event_time=f"{hh}:00", location=None,
        participants=participants, event_type="family_gathering", source_msg_id=source,
    )


def _offsets(group: str) -> list[bool]:
    """Whether each active event's offset pushes (T-30/7/3/2/1 days) ring everyone."""
    with memory._conn() as c:
        ids = [
            row[0]
            for row in c.execute(
                "SELECT event_id FROM events WHERE group_id=? AND status='active' ORDER BY created_at", (group,)
            )
        ]
    return [
        any(target.kind == "all" for target in line_mentions.event_mention_targets(calendar_db.get_active_event_by_id(group, i)))
        for i in ids
    ]


def _calendar_answer(title: str, participants: list[str], day: str) -> str:
    """The calendar model's answer: an event at 18:00 on ``day``."""
    return json.dumps(
        {
            "has_event": True, "is_cancellation": False, "title": title, "event_type": "family_gathering",
            "date": day, "time": "18:00", "location": None, "participants": participants,
            "cancel_target_keyword": None,
        },
        ensure_ascii=False,
    )


# A2 #1 (seat 2 C1 and its reverse; the glued word of seat 3 I1)
@pytest.mark.parametrize(
    ("first", "second"),
    [
        (("測試成員甲看牙醫", ["測試成員甲"]), ("看牙醫", ["全家"])),
        (("看牙醫", ["全家"]), ("測試成員甲看牙醫", ["測試成員甲"])),
        (("全家8點吃飯", ["全家"]), ("測試成員甲吃飯", ["測試成員甲"])),
    ],
)
def test_a_members_reminder_and_one_for_everyone_are_one_push_for_everyone(first, second):
    at, _day_iso = _at(hh=10)
    memory.add_reminder_with_outcome(GROUP, "U_TEST_A", first[0], at, source_text="x", mention_aliases=first[1], time_kind="clock")

    _rid, outcome = memory.add_reminder_with_outcome(
        GROUP, "U_TEST_B", second[0], at, source_text="y", mention_aliases=second[1], time_kind="clock"
    )

    assert outcome in ("merged", "duplicate")
    assert _pushes(GROUP, at) == [True]


def test_a_glued_family_word_still_reads_as_the_same_meal():
    # 「全家8點吃飯」 keeps its letters after the strip; 全家 still drops out of the words compared.
    at, _day_iso = _at(hh=20)
    memory.add_reminder_with_outcome(GROUP, "U_TEST_A", "吃飯", at, source_text="x", mention_aliases=[], time_kind="clock")
    memory.add_reminder_with_outcome(GROUP, "U_TEST_B", "全家8點吃飯", at, source_text="y", mention_aliases=["全家"], time_kind="clock")

    assert _pushes(GROUP, at) == [True]


# Seat 1 p3c: whose event it is decides whether two reminders are one.  全家 and a member are one; 「全家
# 看電影」 and 「測試成員甲看牙醫」 are two; 測試成員乙 and 測試成員甲 are two, 全家 on one of them or not.
@pytest.mark.parametrize(
    ("first", "second", "pushes"),
    [
        (("大家吃飯", ["全家"]), ("測試成員甲吃飯", ["測試成員甲"]), [True]),
        (("看牙醫", ["大家"]), ("測試成員甲看牙醫", ["測試成員甲"]), [True]),
        (("看牙醫", ["all"]), ("測試成員甲看牙醫", ["測試成員甲"]), [True]),
        (("全家看電影", ["全家"]), ("測試成員甲看牙醫", ["測試成員甲"]), [False, True]),
        (("測試成員乙看牙醫", ["全家", "測試成員乙"]), ("測試成員甲看牙醫", ["測試成員甲"]), [False, True]),
    ],
)
def test_whose_event_it_is_decides_whether_two_reminders_are_one(first, second, pushes):
    at, _day_iso = _at(hh=18)
    memory.add_reminder_with_outcome(GROUP, "U_TEST_A", first[0], at, source_text="x", mention_aliases=first[1], time_kind="clock")

    memory.add_reminder_with_outcome(GROUP, "U_TEST_B", second[0], at, source_text="y", mention_aliases=second[1], time_kind="clock")

    assert sorted(_pushes(GROUP, at)) == pushes


# A2 #10 (seat 4 I1, R3): two people each write a reminder for everyone of one short-titled event, one
# with a clock and one with a daypart or no time.  The words compared lose 全家 (「全家聚餐」 is 「聚餐」),
# so a short core is one event when both texts hold the same names, 全家 counted: one reminder, as before.
@pytest.mark.parametrize("aliases", [[], ["全家"]], ids=["no-alias", "family-alias"])
@pytest.mark.parametrize(
    ("first", "second"),
    [
        (("clock", 18), ("daypart:晚上", 19)),
        (("daypart:晚上", 19), ("daypart:晚上", 19)),
        (("clock", 18), ("none", 12)),
        (("clock", 18), ("clock", 18)),
    ],
    ids=["clock+daypart", "daypart+daypart", "clock+none", "same-clock"],
)
@pytest.mark.parametrize("words", ["全家聚餐", "全家吃飯", "全家出遊"])
def test_two_reminders_for_everyone_of_one_short_event_stay_one(words, first, second, aliases):
    at, _day_iso = _at(hh=first[1])
    memory.add_reminder_with_outcome(GROUP, "U_TEST_A", words, at, source_text="x", mention_aliases=aliases, time_kind=first[0])

    _rid, outcome = memory.add_reminder_with_outcome(
        GROUP, "U_TEST_B", words, _at(hh=second[1])[0], source_text="y", mention_aliases=aliases, time_kind=second[0]
    )

    assert outcome in ("merged", "duplicate")
    assert len(_rows()) == 1
    assert len(_pushes(GROUP, at)) == 1


# A2 #10b (R3, the move): the reminder with a daypart or no time comes first; the one with a clock moves
# it there, as before.
@pytest.mark.parametrize("earlier", [("daypart:晚上", 19), ("none", 12)], ids=["daypart", "none"])
@pytest.mark.parametrize("words", ["全家聚餐", "全家吃飯"])
def test_a_clock_moves_the_reminder_for_everyone_of_one_short_event(words, earlier):
    memory.add_reminder_with_outcome(
        GROUP, "U_TEST_A", words, _at(hh=earlier[1])[0], source_text="x", mention_aliases=["全家"], time_kind=earlier[0]
    )
    at, _day_iso = _at(hh=18)

    _rid, outcome = memory.add_reminder_with_outcome(
        GROUP, "U_TEST_B", words, at, source_text="y", mention_aliases=["全家"], time_kind="clock"
    )

    assert outcome == "merged"
    assert len(_rows()) == 1
    assert _pushes(GROUP, at) == [True]


# F1 (2026-11-27, seat 3 I-b): D1's strip takes the call out of the words, so two people who each @全家 the
# same dinner are stored as 「聚餐」 with 全家 among the aliases.  When both reminders are for everyone, 全家
# counts among both sides' names (reminder_intent._named_alike): one reminder and one push for everyone, the
# words kept 全家 or not, and the clock still moves the other.  Seat 3's grid: four pairs of words, a clock with
# a daypart or no time, both moves, the same person or another; the row that stays reminds at 18:00.
SHORT_FOR_EVERYONE = {
    "stripped-both": (("聚餐", ["全家"]), ("聚餐", ["全家"])),
    "stripped-then-word": (("聚餐", ["全家"]), ("全家聚餐", ["全家"])),
    "word-then-stripped": (("全家聚餐", ["全家"]), ("聚餐", ["全家"])),
    "all-both": (("聚餐", ["all"]), ("聚餐", ["all"])),
}
TWO_TIMES = {
    "clock+daypart": (("clock", 18), ("daypart:晚上", 19)),
    "clock+none": (("clock", 18), ("none", 12)),
    "daypart-moved": (("daypart:晚上", 19), ("clock", 18)),
    "none-moved": (("none", 12), ("clock", 18)),
}


def _two_reminders(first, second, times: str, author: str) -> str:
    (kind1, hh1), (kind2, hh2) = TWO_TIMES[times]
    memory.add_reminder_with_outcome(
        GROUP, "U_TEST_A", first[0], _at(hh=hh1)[0], source_text="x", mention_aliases=first[1], time_kind=kind1
    )
    _rid, outcome = memory.add_reminder_with_outcome(
        GROUP, "U_TEST_A" if author == "same" else "U_TEST_B", second[0], _at(hh=hh2)[0], source_text="y",
        mention_aliases=second[1], time_kind=kind2,
    )
    return outcome


@pytest.mark.parametrize("author", ["same", "other"])
@pytest.mark.parametrize("times", list(TWO_TIMES))
@pytest.mark.parametrize("pair", list(SHORT_FOR_EVERYONE))
def test_two_calls_to_everyone_of_one_short_event_stay_one(pair, times, author):
    outcome = _two_reminders(*SHORT_FOR_EVERYONE[pair], times, author)

    assert outcome in ("merged", "duplicate")
    assert len(_rows()) == 1
    assert _pushes(GROUP, _at(hh=18)[0]) == [True]


# F1's edge (variant B, 2026-11-27): a reminder for everyone next to one that is not pairs as it did.  A member
# named in both words, 全家 among one side's aliases, stays one push for everyone (D5); a 「全家聚餐」 written
# without a call stays its own reminder next to a 「聚餐」 for everyone.
@pytest.mark.parametrize("times", list(TWO_TIMES))
@pytest.mark.parametrize("everyone_first", [True, False], ids=["everyone-first", "member-first"])
def test_a_member_named_on_both_with_everyone_on_one_stays_one_push_for_everyone(everyone_first, times):
    pair = [("測試成員甲吃飯", ["全家", "測試成員甲"]), ("測試成員甲吃飯", ["測試成員甲"])]
    _two_reminders(*(pair if everyone_first else pair[::-1]), times, "other")

    assert len(_rows()) == 1
    assert _pushes(GROUP, _at(hh=18)[0]) == [True]


@pytest.mark.parametrize("author", ["same", "other"])
@pytest.mark.parametrize("times", list(TWO_TIMES))
@pytest.mark.parametrize("call_first", [True, False], ids=["call-first", "words-first"])
def test_a_family_dinner_written_without_a_call_stays_its_own_reminder(call_first, times, author):
    pair = [("聚餐", ["全家"]), ("全家聚餐", [])]
    _two_reminders(*(pair if call_first else pair[::-1]), times, author)

    rows = _rows()
    assert len(rows) == 2
    assert sorted(_rings_everyone(row) for row in rows) == [False, True]


# A2 #2 (seat 2 q2, seat 3 M4b): a mirror naming a member, then a reminder for everyone of the same visit
@pytest.mark.parametrize("family", ["全家", "all"])
def test_a_reminder_for_everyone_on_a_members_mirror_makes_the_event_everyones(family):
    at, day = _at(hh=10)
    calendar_db.insert_event_with_outcome(
        group_id=GROUP, title="看牙醫", event_date=day, event_time="10:00", location=None,
        participants=["測試成員甲(就醫)"], event_type="medical", source_msg_id="",
    )
    before = _mirror_aliases(GROUP)

    _rid, outcome = memory.add_reminder_with_outcome(
        GROUP, "U_TEST_B", "去看牙醫", at, source_text="z", mention_aliases=[family], time_kind="clock",
    )

    assert outcome == "duplicate"
    assert _mirror_aliases(GROUP) == [*before, "全家"]  # the word 全家, whichever word asked for everyone
    (participants,) = _participants(GROUP)
    assert line_mentions.is_all_participants(participants)
    assert "測試成員甲(就醫)" in participants
    _sync_all(GROUP)
    assert _pushes(GROUP, at) == [True]


# seat 3 M5: the exact branch lands on a mirror; the next sync rebuilds the mirror from the event
def test_the_same_words_on_a_mirror_survive_the_sync():
    at, day = _at(hh=10)
    _event(GROUP, "看牙醫", day, 10, ["測試成員甲"], source="msg-e")
    memory.add_reminder_with_outcome(GROUP, "U_TEST_B", "看牙醫", at, source_text="z", mention_aliases=["全家"], time_kind="clock")

    _sync_all(GROUP)

    assert _pushes(GROUP, at) == [True]


# A2 #3 (seat 3 C2): the drain writes through complete_pending_reminder, not add_reminder_with_outcome
def test_the_drain_reaches_the_same_exit():
    at, day = _at(hh=10)
    _event(GROUP, "看牙醫", day, 10, ["測試成員甲"], source="msg-e")
    pending_id = memory.enqueue_pending_reminder(GROUP, "U_TEST_B", "q", "msg-q")
    token = memory.claim_pending_reminder(pending_id)

    memory.complete_pending_reminder(
        pending_id=pending_id, pending_claim_token=token, group_id=GROUP, user_id="U_TEST_B", action="看牙醫",
        remind_at=at, source_text="q", mention_aliases=["全家"], time_kind="clock",
    )
    _sync_all(GROUP)

    assert _pushes(GROUP, at) == [True]


# A2 #4 (seat 3 I5): the nearby branch answers merged, and its row can be a mirror.  The words are the
# fixed topic memory._reminder_topic knows; nothing else here relies on that topic.
def test_a_merge_into_a_mirror_also_makes_the_event_everyones():
    at, day = _at(hh=19)
    _event(GROUP, "嗎哪小組查經", day, 19, [], source="msg-m")

    _rid, outcome = memory.add_reminder_with_outcome(
        GROUP, "U_TEST_B", "嗎哪小組查經（教會4樓）", at, source_text="y", mention_aliases=["全家"], time_kind="clock"
    )

    assert outcome == "merged"
    _sync_all(GROUP)
    assert line_mentions.is_all_participants(_participants(GROUP)[0])
    assert _pushes(GROUP, at) == [True]


# A2 #5 (seat 1 I1-2): weak -> strong answers duplicate; the newcomer's audience is kept
def test_a_weak_mention_on_a_members_row_keeps_its_audience():
    at, _day_iso = _at(hh=10)
    memory.add_reminder_with_outcome(
        GROUP, "U_TEST_A", "測試成員甲看牙醫", at, source_text="x", mention_aliases=["測試成員甲"], time_kind="clock"
    )

    _rid, outcome = memory.add_reminder_with_outcome(
        GROUP, "U_TEST_B", "明天", at, source_text="y", mention_aliases=["全家"], time_kind="clock"
    )

    assert outcome == "duplicate"
    assert _pushes(GROUP, at) == [True]


# A2 #6 (seat 3 I2, seat 2 I1 the other way round): the reminder first, then S1 captures the same thing
@pytest.mark.parametrize("title", ["聚餐", "週六聚餐"])
def test_a_later_event_keeps_the_reminders_audience(title):
    at, day = _at()
    memory.add_reminder_with_outcome(GROUP, "U_TEST_B", "聚餐", at, source_text="w", mention_aliases=["全家"], time_kind="clock")

    _event(GROUP, title, day, 18, [], source="msg-s1")
    _sync_all(GROUP)

    assert _pushes(GROUP, at) == [True]


# A2 #7 (seat 4 I2): the flush reads only the rows it folds in.  An event whose own mirror rings
# everyone (a 全家 title) keeps its participants when a plain duplicate folds into it.
def test_the_flush_does_not_make_a_title_only_family_event_everyones():
    at, day = _at()
    memory.add_reminder_with_outcome(GROUP, "U_TEST_B", "全家聚餐", at, source_text="w", mention_aliases=[], time_kind="clock")

    _event(GROUP, "全家聚餐", day, 18, [], source="msg-s1")
    _sync_all(GROUP)

    assert _participants(GROUP) == [[]]
    assert _pushes(GROUP, at) == [True]


# A2 #8 (seat 3 C1, M2b/M2c): D5 put 全家 on an event without a message; later S1 captures the same evening
@pytest.mark.parametrize("participants", [["測試成員甲"], []])
@pytest.mark.parametrize("title2", ["晚上聚餐", "聚餐"])
def test_the_event_stays_everyones_through_a_semantic_merge(participants, title2):
    at, day = _at()
    calendar_db.insert_event_with_outcome(
        group_id=GROUP, title="聚餐", event_date=day, event_time="18:00", location=None,
        participants=[], event_type="family_gathering", source_msg_id="",
    )
    _rid, outcome = memory.add_reminder_with_outcome(
        GROUP, "U_TEST_B", "聚餐", at, source_text="w", mention_aliases=["全家"], time_kind="clock"
    )
    assert outcome == "duplicate"
    memory.log_raw_message(GROUP, "msg-y", "U_TEST_A", f"{day} 晚上6點 {title2}", index_for_recall=False)

    calendar_db.insert_event_with_outcome(
        group_id=GROUP, title=title2, event_date=day, event_time="18:00", location=None,
        participants=participants, event_type="family_gathering", source_msg_id="msg-y",
    )
    _sync_all(GROUP)

    assert len(_participants(GROUP)) == 1
    assert line_mentions.is_all_participants(_participants(GROUP)[0])
    assert set(participants) <= set(_participants(GROUP)[0])  # the message's own people stay (seat 4 N12)
    assert _pushes(GROUP, at) == [True]


# R2 (seat 2 C1), the other way round: a capture for everyone with no message of its own (a burst) lands
# on an event another message made for a member.  The semantic merge keeps that event and makes it
# everyone's, still one push (D5).  58d5d5c kept two events; stage 2's first round dropped the 全家.
@pytest.mark.parametrize("call", ["@全家", "@all"])
def test_a_burst_that_calls_everyone_makes_a_members_event_everyones(tier, monkeypatch, call):
    inline_spawn(monkeypatch)
    at, day = _at()
    tier(generate={TIER: resp(_calendar_answer("聚餐", ["測試成員甲"], day))})
    first = "@測試成員甲 週六下午6點聚餐"
    memory.log_raw_message(GROUP, "m-s1", "U_TEST_B", first, index_for_recall=False)
    main._maybe_capture_calendar_event(GROUP, first, "U_TEST_B", "m-s1")
    # The burst's two messages go through the reminder path first, and the model reads no reminder in
    # either (a model that cannot be asked would queue the first one, and the burst would not be captured).
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *_a, **_k: None)
    burst = ["週六晚上6點", f"{call} 聚餐喔"]
    ids = ["m-b1", "m-b2"]
    for message_id, text in zip(ids, burst):
        memory.log_raw_message(GROUP, message_id, "U_TEST_A", text, index_for_recall=False)
        assert not main._maybe_extract_reminder(text, GROUP, "U_TEST_A", message_id)
    assert not main._burst_message_owned_by_reminder(GROUP, ids)
    tier(generate={TIER: resp(_calendar_answer("晚上聚餐", ["全家"], day))})

    main._burst_capture_calendar_event(GROUP, "\n".join(burst), ids)

    assert _participants(GROUP) == [["全家", "測試成員甲"]]
    assert _offsets(GROUP) == [True]
    assert _pushes(GROUP, at) == [True]


@pytest.mark.parametrize("title", ["晚上聚餐", "聚餐"])
def test_a_capture_for_everyone_without_a_message_makes_a_members_event_everyones(title):
    # The same, straight into the calendar.  With the same title 58d5d5c's unique index dropped the 全家,
    # so this one now rings everyone where it did not (D5).
    at, day = _at()
    _event(GROUP, "聚餐", day, 18, ["測試成員甲"], source="msg-a")

    _eid, outcome = calendar_db.insert_event_with_outcome(
        group_id=GROUP, title=title, event_date=day, event_time="18:00", location=None,
        participants=["全家"], event_type="family_gathering", source_msg_id="",
    )

    assert outcome == "merged"
    assert _participants(GROUP) == [["全家", "測試成員甲"]]
    assert _offsets(GROUP) == [True]
    assert _pushes(GROUP, at) == [True]


# R2 when it could be more than one event: 全家 counts as a person again, as before stage 2.  The events
# that name only other people drop out; none left, it is an event of its own; one left, it joins that one.
@pytest.mark.parametrize("title", ["傍晚聚餐", "晚上 聚餐", "聚餐 晚上"])
def test_a_capture_for_everyone_that_two_members_events_could_be_is_its_own(title):
    at, day = _at()
    _event(GROUP, "聚餐", day, 18, ["測試成員甲"], source="msg-a")
    _event(GROUP, "晚上聚餐", day, 18, ["測試成員乙"], source="msg-b")

    _eid, outcome = calendar_db.insert_event_with_outcome(
        group_id=GROUP, title=title, event_date=day, event_time="18:00", location=None,
        participants=["全家"], event_type="family_gathering", source_msg_id="",
    )

    assert outcome == "created"
    assert _offsets(GROUP) == [False, False, True]
    assert sorted(_pushes(GROUP, at)) == [False, False, True]


def test_a_capture_for_everyone_joins_the_one_event_that_names_nobody_else():
    at, day = _at()
    _event(GROUP, "聚餐", day, 18, ["測試成員甲"], source="msg-a")
    _event(GROUP, "晚上聚餐", day, 18, [], source="msg-b")

    _eid, outcome = calendar_db.insert_event_with_outcome(
        group_id=GROUP, title="傍晚聚餐", event_date=day, event_time="18:00", location=None,
        participants=["全家"], event_type="family_gathering", source_msg_id="",
    )

    assert outcome == "merged"
    assert _participants(GROUP) == [["測試成員甲"], ["全家"]]
    assert _offsets(GROUP) == [False, True]
    assert sorted(_pushes(GROUP, at)) == [False, True]


# R8 (seat 2 R2-C1): the one event a capture for everyone lands on cannot take it, because that event's
# mirror is no longer pending (the push loop's own clean-up, a push that went out) or is going out now.
# Before stage 2 the two were never one event (both name people, none shared): as then, the capture is
# an event of its own, rung for everyone.
def _a_members_badminton(day: str, participants: list[str]) -> str:
    # A badminton event's mirror is due 7 days before it (calendar_db), so inside that lead it is overdue.
    memory.log_raw_message(GROUP, "msg-bm", "U_TEST_A", "@測試成員甲 打羽球", index_for_recall=False)
    event_id, outcome = calendar_db.insert_event_with_outcome(
        group_id=GROUP, title="打羽球", event_date=day, event_time="10:00", location=None,
        participants=participants, event_type="family_gathering", source_msg_id="msg-bm",
    )
    assert outcome == "created"
    return event_id


def _take_the_mirror_away(event_id: str, how: str) -> None:
    with memory._conn() as c:
        (mirror_id,) = c.execute(
            "SELECT reminder_id FROM reminders WHERE group_id=? AND source_ref=?", (GROUP, event_id)
        ).fetchone()
    if how == "cleaned-up":
        memory.delete_stale_pending_reminders(grace_seconds=3600)  # reminder_push runs this before each round
    elif how == "pushed":
        assert memory.mark_reminder_pushed(mirror_id, "now")
    else:
        with memory._conn() as c:
            c.execute(
                "INSERT INTO reminder_delivery_claims(group_id, delivery_kind, subject_ref, occurrence, transport, "
                "state, claim_token, retry_key, claimed_at) VALUES (?, 'calendar', ?, 'now', 'push', 'sending', "
                "'t', 'r', ?)",
                (GROUP, event_id, int(time.time())),
            )
    with memory._conn() as c:
        status = c.execute("SELECT status FROM reminders WHERE reminder_id=?", (mirror_id,)).fetchone()[0]
    assert (status != "pending") is (how != "going-out")


@pytest.mark.parametrize("how", ["cleaned-up", "pushed", "going-out"])
def test_a_capture_for_everyone_the_members_event_cannot_take_is_an_event_of_its_own(how):
    day = (datetime.now(TW).date() + timedelta(days=3)).isoformat()
    _take_the_mirror_away(_a_members_badminton(day, ["測試成員甲"]), how)

    event_id, outcome = calendar_db.insert_event_with_outcome(
        group_id=GROUP, title="羽球", event_date=day, event_time="10:00", location=None,
        participants=["全家"], event_type="family_gathering", source_msg_id="",
    )

    assert outcome == "created"
    assert _participants(GROUP) == [["測試成員甲"], ["全家"]]
    assert _offsets(GROUP) == [False, True]
    assert [row["mention_aliases"] for row in _rows() if row["source_ref"] == event_id] == [["全家"]]


@pytest.mark.parametrize("how", ["pushed", "going-out"])
def test_a_capture_for_everyone_stays_out_of_an_event_that_names_nobody_when_it_cannot_merge(how):
    # The counter-example (advisor): an event that names nobody was one event with this capture before stage
    # 2 as well, so a merge that cannot write adds nothing, as then.
    day = (datetime.now(TW).date() + timedelta(days=3)).isoformat()
    _take_the_mirror_away(_a_members_badminton(day, []), how)

    _eid, outcome = calendar_db.insert_event_with_outcome(
        group_id=GROUP, title="羽球", event_date=day, event_time="10:00", location=None,
        participants=["全家"], event_type="family_gathering", source_msg_id="",
    )

    assert outcome in ("blocked", "busy")
    assert _participants(GROUP) == [[]]


def _capture(
    title: str,
    day: str,
    clock: str | None,
    participants: list[str],
    *,
    source: str = "",
    location: str | None = None,
    event_type: str = "family_gathering",
) -> tuple[str, str]:
    if source:
        memory.log_raw_message(GROUP, source, "U_TEST_A", f"{day} {title}", index_for_recall=False)
    return calendar_db.insert_event_with_outcome(
        group_id=GROUP, title=title, event_date=day, event_time=clock, location=location,
        participants=participants, event_type=event_type, source_msg_id=source,
    )


# F2 (2026-11-27, seat 2 R3-N1, seat 3 N-c): two events that both ring everyone, read as their offset pushes read
# them (no bracket cut), are never apart for the other people they name, as two that both said 全家 were not
# before stage 2.  The search keeps such an event (a), and so does the filter among several (b).  When the one
# event a capture lands on cannot take it, the capture is an event of its own only if 58d5d5c's reading finds no
# candidate at all on that day (c, seat 2's fix).
def test_two_events_for_everyone_that_name_different_members_are_one():
    at, day = _at()
    _capture("晚上聚餐", day, "18:00", ["全家", "測試成員乙"], source="msg-z")

    _eid, outcome = _capture("傍晚聚餐", day, "18:00", ["全家", "測試成員甲"])

    assert outcome == "duplicate"
    assert _participants(GROUP) == [["全家", "測試成員乙"]]
    assert _offsets(GROUP) == [True]
    assert _pushes(GROUP, at) == [True]


@pytest.mark.parametrize("everyone", ["大家", "all"])
def test_a_capture_for_everyone_joins_the_event_for_everyone_among_several(everyone):
    at, day = _at()
    _capture("聚餐", day, "18:00", ["測試成員甲"], source="msg-a")
    _capture("晚上聚餐", day, "18:00", [everyone], source="msg-b")

    _eid, outcome = _capture("傍晚聚餐", day, "18:00", ["全家"])

    assert outcome == "duplicate"
    assert _participants(GROUP) == [["測試成員甲"], [everyone]]
    assert _offsets(GROUP) == [False, True]
    assert sorted(_pushes(GROUP, at)) == [False, True]


def _an_evening_for_everyone_that_cannot_merge(how: str) -> tuple[str, str | None]:
    """X of seat 2's R3-N1: 「聚餐」 for everyone (大家) with no message, its mirror no longer pending."""
    if how == "pushed":
        day, clock = (datetime.now(TW).date() + timedelta(days=4)).isoformat(), "18:00"
    else:
        day, clock = datetime.now(TW).date().isoformat(), None  # an all-day event today
    event_id, outcome = _capture("聚餐", day, clock, ["大家"])
    assert outcome == "created"
    if how == "pushed":
        with memory._conn() as c:
            (mirror_id,) = c.execute(
                "SELECT reminder_id FROM reminders WHERE group_id=? AND source_ref=?", (GROUP, event_id)
            ).fetchone()
        assert memory.mark_reminder_pushed(mirror_id, "now")
    else:
        # The push loop's clean-up (it runs with an hour's grace; none here, so the hour of the run does not matter)
        memory.delete_stale_pending_reminders(grace_seconds=0)
    assert [row for row in _rows() if row["source_ref"] == event_id] == []
    return day, clock


@pytest.mark.parametrize("how", ["pushed", "cleaned-up"])
def test_three_captures_for_everyone_of_one_evening_are_two_events_as_before(how):
    # X; then a message's 「晚上聚餐」 for everyone and 測試成員乙 (Z), an event of its own as before stage 2, since
    # 58d5d5c's reading finds nothing on the day; then 「傍晚聚餐」 for everyone and 測試成員甲 with no message, which
    # could be X or Z and is written nowhere.  Two events, as in 58d5d5c, where round 3 made three.
    day, clock = _an_evening_for_everyone_that_cannot_merge(how)
    _z, z_outcome = _capture("晚上聚餐", day, clock, ["全家", "測試成員乙"], source="msg-z")

    _w, w_outcome = _capture("傍晚聚餐", day, clock, ["全家", "測試成員甲"])

    assert (z_outcome, w_outcome) == ("created", "ambiguous")
    assert _participants(GROUP) == [["大家"], ["全家", "測試成員乙"]]
    assert _offsets(GROUP) == [True, True]


def test_a_capture_another_event_took_before_stage_2_is_no_event_of_its_own():
    # c: as above, Z's 全家 bracketed.  Its offset pushes do not read 全家(主辦) as everyone, so the search leaves Z
    # out and the capture lands on X alone, which cannot take it; 58d5d5c read Z as 全家 and merged the capture
    # there.  So it stays out, blocked, rather than being a third event.
    day, clock = _an_evening_for_everyone_that_cannot_merge("pushed")
    _z, z_outcome = _capture("晚上聚餐", day, clock, ["全家(主辦)", "測試成員乙"], source="msg-z")

    _w, w_outcome = _capture("傍晚聚餐", day, clock, ["全家", "測試成員甲"])

    assert (z_outcome, w_outcome) == ("created", "blocked")
    assert _participants(GROUP) == [["大家"], ["全家(主辦)", "測試成員乙"]]


def test_an_event_only_its_mirror_reads_as_everyones_stays_apart_from_a_capture_for_everyone():
    # F2 reads everyone as the offset pushes do: 全家(主辦) with 測試成員乙 is no event for everyone there, so a
    # capture for everyone and 測試成員甲 is an event of its own, as before, and its offset pushes ring everyone.
    _at_18, day = _at()
    _capture("晚上聚餐", day, "18:00", ["全家(主辦)", "測試成員乙"], source="msg-z")

    _eid, outcome = _capture("傍晚聚餐", day, "18:00", ["全家", "測試成員甲"])

    assert outcome == "created"
    assert _participants(GROUP) == [["全家(主辦)", "測試成員乙"], ["全家", "測試成員甲"]]
    assert _offsets(GROUP) == [False, True]


# F3 (2026-11-27, seat 3 N-b, seat 2 R2-N1): a capture for everyone with no message of its own (a burst) that the
# unique index drops (the title, date and time of an event the search left out) goes into that event the
# semantic merge's way: 全家 joins and the event keeps its people (R2 ①); blocked or busy as they are.  Seat 3's
# three ways the search leaves the event out, and seat 2's title of one of two members' events.
@pytest.mark.parametrize(
    ("event", "burst", "participants"),
    [
        (dict(source="msg-a", location="餐廳甲"), dict(participants=["全家"], location="餐廳乙"), ["全家", "測試成員甲"]),
        (dict(source="msg-a"), dict(participants=["全家", "測試成員乙"]), ["全家", "測試成員甲"]),
        (dict(event_type="medical"), dict(participants=["全家"]), ["測試成員甲", "全家"]),
    ],
    ids=["another-place", "another-member", "another-kind-no-messages"],
)
def test_a_burst_for_everyone_the_unique_index_drops_makes_that_event_everyones(event, burst, participants):
    at, day = _at()
    _capture("聚餐", day, "18:00", ["測試成員甲"], **event)

    _eid, outcome = _capture("聚餐", day, "18:00", **burst)

    assert outcome == "merged"
    assert _participants(GROUP) == [participants]
    assert _offsets(GROUP) == [True]
    assert _pushes(GROUP, at) == [True]


def test_a_burst_for_everyone_with_the_title_of_one_of_two_members_events_joins_that_one():
    at, day = _at()
    _capture("聚餐", day, "18:00", ["測試成員甲"], source="msg-a")
    _capture("晚上聚餐", day, "18:00", ["測試成員乙"], source="msg-b")

    _eid, outcome = _capture("聚餐", day, "18:00", ["全家"])

    assert outcome == "merged"
    assert _participants(GROUP) == [["全家", "測試成員甲"], ["測試成員乙"]]
    assert _offsets(GROUP) == [True, False]
    assert sorted(_pushes(GROUP, at)) == [False, True]


@pytest.mark.parametrize(("how", "answer"), [("pushed", "blocked"), ("going-out", "busy")])
def test_a_burst_for_everyone_the_unique_index_drops_writes_nothing_where_it_cannot(how, answer):
    _at_18, day = _at()
    event_id, _outcome = _capture("聚餐", day, "18:00", ["測試成員甲"], source="msg-a", location="餐廳甲")
    _take_the_mirror_away(event_id, how)

    _eid, outcome = _capture("聚餐", day, "18:00", ["全家"], location="餐廳乙")

    assert outcome == answer
    assert _participants(GROUP) == [["測試成員甲"]]
    assert _offsets(GROUP) == [False]


@pytest.mark.parametrize(("how", "answer"), [("cleaned-up", "blocked"), ("pushed", "blocked"), ("going-out", "busy")])
def test_a_burst_for_everyone_with_the_members_own_title_writes_nothing_when_it_cannot_merge(how, answer):
    # Seat 2's R2-C1 rows with the event's own title: R8 lets the capture through to the unique index, which answered
    # duplicate before F3; now the merge answers.  Either way nothing is written and the member's event stays.
    day = (datetime.now(TW).date() + timedelta(days=3)).isoformat()
    _take_the_mirror_away(_a_members_badminton(day, ["測試成員甲"]), how)

    _eid, outcome = _capture("打羽球", day, "10:00", ["全家"])

    assert outcome == answer
    assert _participants(GROUP) == [["測試成員甲"]]
    assert _offsets(GROUP) == [False]


def test_the_same_message_captured_again_for_everyone_stays_a_duplicate():
    # F3 is for a burst only (seat 3 N-b): a capture with a message of its own that the unique index drops answers
    # duplicate, as before, whoever it names.
    _at_18, day = _at()
    _capture("聚餐", day, "18:00", ["測試成員甲"], source="msg-a", location="餐廳甲")

    _eid, outcome = _capture("聚餐", day, "18:00", ["全家"], source="msg-a", location="餐廳乙")

    assert outcome == "duplicate"
    assert _participants(GROUP) == [["測試成員甲"]]
    assert _offsets(GROUP) == [False]


# ── the list (reminder_overview) pairs rows with the same matcher ────────────────────────────


def test_the_list_reads_a_glued_family_word_as_the_same_meal_both_ways():
    at, _day_iso = _at(hh=20)
    rows = [
        {"reminder_id": 1, "group_id": GROUP, "user_id": "U_TEST_A", "action": "吃飯", "remind_at": at,
         "time_kind": "clock", "mention_aliases": [], "source_kind": "", "source_ref": "", "source_text": ""},
        {"reminder_id": 2, "group_id": GROUP, "user_id": "U_TEST_B", "action": "全家8點吃飯", "remind_at": at,
         "time_kind": "clock", "mention_aliases": ["全家"], "source_kind": "", "source_ref": "", "source_text": ""},
    ]
    plain, family = (reminder_overview._reminder_item(row) for row in rows)

    assert reminder_overview._reads_into(family, plain)
    assert reminder_overview._reads_into(plain, family)
