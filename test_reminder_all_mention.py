"""2026-11-07: a reminder rings every phone only when someone asked for that.

`calendar_db._raw_text_reminder_aliases` read 「all」 inside 「@allen」, 「@mall」, an address or a link, and
took any 全家／大家／所有人／全部／全員／everyone in the message, so 「全部買完」 and 「全家便利商店」 pinged
the whole group when the reminder went out.  Andrew's calls: in the message only an @ counts, LINE's
「@All」 too; an event whose title says 全家 is for the whole family, as a corrected title is, unless
that 全家 is the shop (or a clinic or a pharmacy of that name), while a place next to it (全家門口) still
reads as the family; an @ inside a link or an address tags nobody.  The reply text is
test_all_mention_boundary.py.  All text is made up.
"""

from __future__ import annotations

import importlib
import json
from datetime import date, timedelta

import pytest

import line_mentions
import main

ALIASES = {"U_A": "成員甲", "U_B": "成員乙"}


def _half(text: str) -> str:
    """Addresses are written with 「＠」: the privacy audit refuses a literal one anywhere in the tree."""
    return text.replace("＠", "@")


@pytest.fixture(autouse=True)
def _synthetic_aliases(monkeypatch):
    monkeypatch.setattr(line_mentions, "load_user_aliases", lambda: dict(ALIASES))


@pytest.fixture
def allen_is_a_member(monkeypatch):
    monkeypatch.setattr(line_mentions, "load_user_aliases", lambda: {**ALIASES, "U_ALLEN": "allen"})


@pytest.fixture
def cd(tmp_path, monkeypatch):
    db_file = tmp_path / "test_cal.db"
    monkeypatch.setenv("SQLITE_PATH", str(db_file))
    import config

    importlib.reload(config)
    config.settings.sqlite_path = str(db_file)
    import calendar_db
    import memory

    importlib.reload(calendar_db)
    memory._DB_PATH = calendar_db._DB_PATH
    memory._init_db()
    return calendar_db


def _aliases(text: str | None) -> list[str]:
    import calendar_db

    return calendar_db._raw_text_reminder_aliases(text)


# ── what a family message asks for ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "明天問 @allen 報價",
        "問 @mall 客服",
        "問 @small 客服",
        "問 @Sally 客服",
        "問 @Marshall 客服",
        "＠allen 看一下",
        "看 https://www.tiktok.com/@all/video/1",
        "看 https://www.tiktok.com/@allen/video/1",
        "看 https://www.tiktok.com/＠all/video/1",
        _half("寄到 x＠alliance.example"),
        _half("寄到 x＠hallmark.example"),
        _half("x＠all.example"),
        "@all.example 的客服",
        "@allen.example 的客服",
        "@All.example 的客服",
        "@Allen 報價",
        "@Alliance 的客服",
        "x@All",
        "@all/package",
        "@@all",
    ],
)
def test_all_inside_something_else_asks_for_nobody(text):
    assert _aliases(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "全部買完",
        "全家便利商店買牛奶",
        "週六去全家買牛奶",
        "全家聚餐",  # the event's title is where this one counts
        "大家好，我週五回診",
        "所有人的東西收好",
        "全員到齊再出發",
        "everyone 吃飯",
        "今天全家去機場集合",
    ],
)
def test_a_family_word_in_the_message_asks_for_nobody(text):
    assert _aliases(text) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@all 吃飯", ["all"]),
        ("＠all 吃飯", ["all"]),
        ("@all吃飯", ["all"]),
        ("大家@all", ["all"]),
        ("@all，吃飯", ["all"]),
        ("@allen 和 @all 吃飯", ["all"]),
        (_half("寄到 x＠alliance.example，@all 看一下"), ["all"]),
        ("@全家 週六吃飯", ["全家"]),
        ("＠全家 週六吃飯", ["全家"]),
        ("@全家週六吃飯", ["全家"]),
        ("@全家人週六吃飯", ["全家"]),
        ("@全家，週六吃飯", ["全家"]),
        ("@大家 吃飯", ["大家"]),
        ("@所有人 吃飯", ["所有人"]),
        ("@全部 吃飯", ["全部"]),
        ("@全員 吃飯", ["全員"]),
        ("@everyone 吃飯", ["everyone"]),
        ("@everyone.", ["everyone"]),
        ("@everyone，吃飯", ["everyone"]),
        ("@全家 @all 吃飯", ["全家", "all"]),
        ("@all @all 吃飯", ["all"]),
        # a Chinese word with a number behind it is no handle
        ("@全家8點吃飯", ["全家"]),
        ("@全家12/25聚餐", ["全家"]),
        ("@大家7點集合", ["大家"]),
        ("@所有人9點到", ["所有人"]),
        ("@全部3個人", ["全部"]),
        ("@全員2點集合", ["全員"]),
        # LINE's own mention menu writes 「@All」: any case counts, stored in lower case
        ("@All 集合", ["all"]),
        ("@ALL，集合", ["all"]),
        ("@All吃飯", ["all"]),
        ("@Everyone 吃飯", ["everyone"]),
        ("@All @all 吃飯", ["all"]),
    ],
)
def test_an_at_on_the_whole_family_counts(text, expected):
    assert _aliases(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "x@全家",
        "https://x.example/@全家/1",
        "@everyones 吃飯",
        "@everyone.example 吃飯",
        "@everyone8點",
        "@我們全家 吃飯",
        # Chosen, as for 「@all」 in a reply: glued to a letter, a digit or a "/" it is not counted in the
        # message (a title that says 全家 still counts: test_what_the_local_regex_files).
        "18:00@全家吃飯",
        "OK@全家",
        "/@全家",
        "@all8點吃飯",
        "@all.5點集合",
        "@all@allen 集合",
    ],
)
def test_a_family_word_after_an_at_needs_the_edges_of_a_mention(text):
    assert _aliases(text) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@成員甲 回診", ["成員甲"]),
        ("＠成員甲 回診", ["成員甲"]),
        ("@成員甲明天回診", ["成員甲"]),  # no space: the name is found inside the token
        ("＠成員甲明天回診", ["成員甲"]),
        ("@成員甲 @成員乙 回診", ["成員甲", "成員乙"]),
        ("@成員甲@成員乙 回診", ["成員甲", "成員乙"]),
        ("@成員甲/@成員乙 回診", ["成員甲", "成員乙"]),
        ("@成員甲 @allen 回診", ["成員甲"]),
        ("@all，@成員甲 回診", ["成員甲", "all"]),
        ("@成員甲/@全家 回診", ["成員甲"]),  # the "/" keeps 全家 out, as it keeps 「@成員甲/@all」 out of a reply
        ("@成員甲 全部買完", ["成員甲"]),
        ("成員甲體檢", []),  # a name can be the subject: no @, no tag
        ("看 https://x.example/分類/@成員甲", ["成員甲"]),  # chosen: after a Chinese word a "/" is no link
    ],
)
def test_a_family_member_stays_a_family_member(text, expected):
    assert _aliases(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "看 https://ig.example/@成員甲/1",
        "看 https://ig.example/＠成員甲",
        "看 ig.example/@成員甲",
        "x@成員甲",
        "@@成員甲",
        "18:00@成員甲回診",  # chosen: the price of leaving an address alone
    ],
)
def test_a_member_in_a_link_or_an_address_is_not_tagged(text):
    assert _aliases(text) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("明天問 @allen 報價", ["allen"]),  # before: ['allen', 'all'], the whole group
        ("＠allen 看一下", ["allen"]),
        (_half("寄到 x＠allen.example"), []),
        ("看 https://www.tiktok.com/@allen/video/1", []),
        ("@allen@成員甲 回診", ["allen"]),  # chosen: glued to a letter, the second one is not read
    ],
)
def test_a_member_called_allen_is_tagged_alone(allen_is_a_member, text, expected):
    assert _aliases(text) == expected


@pytest.mark.parametrize("text", ["", None])
def test_no_text_no_aliases(text):
    assert _aliases(text) == []


@pytest.mark.parametrize("left", ["", " ", "x", "X", "1", "_", "/", "@", ".", "-", "，", "家"])
@pytest.mark.parametrize("right", ["", " ", "x", "1", "_", ".", ".x", "@x", "/x", "-x", "-", "吃", "。"])
def test_the_edges_of_all_are_those_of_a_reply(left, right):
    text = f"{left}@all{right}"

    assert ("all" in line_mentions.whole_family_mentions(text)) == line_mentions.has_all_mention(text)
    # 「@All」 has the same edges; only the reply's own check keeps to lower case
    capital = f"{left}@All{right}"
    assert ("all" in line_mentions.whole_family_mentions(capital)) == line_mentions.has_all_mention(text)
    assert not line_mentions.has_all_mention(capital)


def test_aliases_in_text_are_only_aliases():
    # 「全家」 and 「all」 are no family member: whole_family_mentions answers for those.
    assert line_mentions.aliases_mentioned_in_text("@all @全家 全部 @成員甲") == ["成員甲"]


def test_a_row_stored_with_a_family_word_still_reads_as_the_family():
    # Rows written before the change: is_all_participants keeps its list.
    for word in ("全部", "大家", "全家", "所有人", "全員", "everyone", "all", "@all"):
        assert line_mentions.is_all_participants([word])


# ── what reaches the reminder ─────────────────────────────────────────────────────────────────────


def _write_event(cd, text: str, title: str = "機場接機", **event) -> tuple[str, dict]:
    import memory

    memory.log_raw_message("G1", "msg-1", "U_A", text)
    event_id = cd.insert_event(
        group_id="G1",
        title=title,
        event_date=(date.today() + timedelta(days=4)).isoformat(),
        event_time="10:30",
        source_msg_id="msg-1",
        **event,
    )
    assert event_id
    return event_id, _mirror(event_id)


def _mirror(event_id: str) -> dict:
    import memory

    (row,) = [r for r in memory.list_pending_reminders_full("G1") if r["source_ref"] == event_id]
    return row


def _kinds(row: dict) -> list[str]:
    return [t.kind for t in line_mentions.reminder_actor_targets(row["user_id"], row["mention_aliases"], row["action"])]


@pytest.mark.parametrize("text", ["明天問 @allen 報價", "全家便利商店買牛奶", "全部買完", "今天全家去機場集合"])
def test_a_false_ping_tags_the_sender_and_nobody_else(cd, text):
    _event_id, row = _write_event(cd, text)

    assert row["mention_aliases"] == []
    assert _kinds(row) == ["user"]


@pytest.mark.parametrize("text", ["@all 集合", "@全家 集合"])
def test_a_real_one_reaches_the_reminder(cd, text):
    _event_id, row = _write_event(cd, text)

    assert _kinds(row) == ["all"]


def test_participants_say_the_whole_family(cd):
    _event_id, row = _write_event(cd, "全部買完", participants=["全家"])

    assert row["mention_aliases"] == ["全家"]
    assert _kinds(row) == ["all"]


@pytest.mark.parametrize("title", ["全家聚餐", "全家去餐廳聚餐", "我們全家回台北", "@全家 聚餐"])
def test_a_title_says_the_whole_family(cd, title):
    # The local regex files 「明天下午2點全家聚餐」 with no participants and no model ever sees it.
    _event_id, row = _write_event(cd, "週六見", title=title)

    assert row["mention_aliases"] == ["全家"]
    assert _kinds(row) == ["all"]


@pytest.mark.parametrize("title", ["去全家買牛奶", "到全家取貨", "全家便利商店取貨", "全家取件"])
def test_a_title_about_the_shop_does_not(cd, title):
    _event_id, row = _write_event(cd, f"週六{title}", title=title)

    assert row["mention_aliases"] == []
    assert _kinds(row) == ["user"]


def test_a_title_adds_to_the_people_already_there(cd):
    _event_id, row = _write_event(cd, "@成員乙 週六見", title="全家聚餐", participants=["成員甲(主辦)"])

    assert row["mention_aliases"] == ["成員甲", "成員乙", "全家"]


@pytest.mark.parametrize(
    ("text", "aliases", "kinds"),
    [
        ("明天下午2點全家聚餐", ["全家"], ["all"]),
        ("明天晚上6點全家去餐廳聚餐", ["全家"], ["all"]),
        ("明天下午2點 @全家 聚餐", ["全家"], ["all"]),
        ("明天早上10點全家打疫苗", ["全家"], ["all"]),
        ("明天下午2點去全家買牛奶", [], ["user"]),
        ("明天下午3點到全家取貨", [], ["user"]),
        ("明天早上10點去全家對面看牙醫", ["成員甲"], ["user"]),
    ],
)
def test_what_the_local_regex_files(cd, monkeypatch, text, aliases, kinds):
    import memory

    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "成員甲")
    memory.log_raw_message("G1", "msg-1", "U_A", text)

    assert main._capture_calendar_events_regex_only("G1", text, "U_A", "msg-1") == 1

    (row,) = memory.list_pending_reminders_full("G1")
    assert row["mention_aliases"] == aliases
    assert _kinds(row) == kinds


# ── a row written before the change ───────────────────────────────────────────────────────────────


def _stored(reminder_id: int) -> tuple:
    import memory

    with memory._conn() as c:
        return c.execute(
            "SELECT mention_aliases, status, pushed_3d, user_id, action, remind_at FROM reminders WHERE reminder_id=?",
            (reminder_id,),
        ).fetchone()


def _as_written_before(reminder_id: int, status: str = "pending") -> None:
    import memory

    with memory._conn() as c:
        c.execute(
            "UPDATE reminders SET mention_aliases=?, status=?, pushed_3d=1 WHERE reminder_id=?",
            (json.dumps(["all"]), status, reminder_id),
        )


def test_a_sync_takes_the_old_ping_off_a_pending_row(cd):
    event_id, row = _write_event(cd, "明天問 @allen 報價")
    _as_written_before(row["reminder_id"])

    assert cd.synchronize_pending_event_reminder_mirror(cd.get_active_event_by_id("G1", event_id))

    aliases, status, pushed_3d, user_id, action, remind_at = _stored(row["reminder_id"])
    assert json.loads(aliases) == []
    assert (status, pushed_3d, user_id) == ("pending", 1, "U_A")  # it stays the same reminder
    assert (action, remind_at) == (row["action"], row["remind_at"])


def test_a_sync_keeps_a_ping_the_title_asks_for(cd):
    event_id, row = _write_event(cd, "週六見", title="全家聚餐")
    _as_written_before(row["reminder_id"])

    assert cd.synchronize_pending_event_reminder_mirror(cd.get_active_event_by_id("G1", event_id))

    assert json.loads(_stored(row["reminder_id"])[0]) == ["全家"]


@pytest.mark.parametrize("status", ["done", "expired", "cancelled"])
def test_a_sync_leaves_a_finished_row_alone(cd, status):
    event_id, row = _write_event(cd, "明天問 @allen 報價")
    _as_written_before(row["reminder_id"], status)
    before = _stored(row["reminder_id"])

    cd.synchronize_pending_event_reminder_mirror(cd.get_active_event_by_id("G1", event_id))

    assert _stored(row["reminder_id"]) == before


def test_moving_the_event_rewrites_the_ping_too(cd):
    # update_event_schedule has no caller in the bot today: this pins what it would do, not a path that runs.
    event_id, row = _write_event(cd, "明天問 @allen 報價")
    _as_written_before(row["reminder_id"])

    assert cd.update_event_date(event_id, (date.today() + timedelta(days=6)).isoformat())

    aliases, status, *_rest = _stored(row["reminder_id"])
    assert (json.loads(aliases), status) == ([], "pending")


def test_a_repair_reads_the_title_the_way_the_mirror_was_written(cd):
    # repair_legacy_reminders_as_calendar_events compares the mirror it wrote with what it would write now:
    # the title has to count on both sides, or a finished repair reads as drift.
    import memory

    with memory._conn() as conn:
        conn.execute(
            "INSERT INTO raw_messages(group_id,message_id,user_id,text,created_at) VALUES ('G1','M-1','U1','週六上課',1)"
        )
        legacy = conn.execute(
            "INSERT INTO reminders(group_id,user_id,action,remind_at,created_at,status,source_kind,source_ref,"
            "source_text,mention_aliases) VALUES ('G1','U1','皮拉提斯',100,1,'pending','','','原始','[]')"
        ).lastrowid
    expectations = [
        {"reminder_id": legacy, "action": "皮拉提斯", "remind_at": 100, "status": "pending", "source_kind": "", "source_ref": ""}
    ]
    specs = [
        {
            "title": "全家皮拉提斯",
            "event_date": "2099-08-30",
            "event_time": "11:00",
            "participants": [],
            "source_msg_id": "M-1",
            "legacy_reminder_ids": [legacy],
        }
    ]

    event_ids, outcome = cd.repair_legacy_reminders_as_calendar_events("G1", reminder_expectations=expectations, event_specs=specs)
    again = cd.repair_legacy_reminders_as_calendar_events("G1", reminder_expectations=expectations, event_specs=specs)

    assert outcome == "created"
    assert _mirror(event_ids[0])["mention_aliases"] == ["全家"]
    assert again == (event_ids, "already_applied")


def test_the_backfill_does_not_touch_a_row_that_exists(cd):
    # Why an old row does not heal by itself: this is all the event sender calls.
    event_id, row = _write_event(cd, "明天問 @allen 報價")
    _as_written_before(row["reminder_id"])
    before = _stored(row["reminder_id"])

    assert cd.ensure_event_reminder_mirror(cd.get_active_event_by_id("G1", event_id))

    assert _stored(row["reminder_id"]) == before


# ── the shop and the family ───────────────────────────────────────────────────────────────────────

FAMILY = [
    "全家聚餐",
    "全家出遊",
    "全家打疫苗",
    "全家福拍照",
    "帶全家去花蓮",
    "我們全家回台北",
    "全家買年貨",
    "全家買牛奶",  # the same shape: it stays the family
    "在全家群組公布",
    "現在全家打疫苗",
    # a place next to it reads as the family (Andrew, 11/12)
    "全家門口集合",
    "全家前面集合",
    "全家後面停車場",
    "全家對面的餐廳",
    "全家旁邊的診所",
    "全家附近",
    "全家隔壁",
    "全家樓上",
    "全家樓下集合",
    # 去 and 到 that go nowhere
    "過去全家出遊",
    "輪到全家打疫苗",
    "看到全家出遊",
    "想到全家要回診",
    "等到全家到齊",
    "遇到全家出遊",
    "回到全家身邊",
    "直到全家到齊",
    "得到全家支持",
    "收到全家的祝福",
    "提到全家出遊",
    "說到全家出遊",
    "找到全家要住的飯店",
    "聽到全家要出遊",
    "碰到全家出遊",
    "感到全家的愛",
    # going to the family, not to the shop
    "去全家人的聚會",
    "去全家福拍照",
    "去全家聚餐",
    "到全家都喜歡的餐廳",
    "回去全家團圓",
    "回去全家圍爐",
    "回去全家一起吃飯",
    "去全家過年",
    "去一下全家人那邊",
    # 全家 in a longer word that is still the family
    "平安全家福",
    "成全家人",
    "祝大家平安全家幸福",
    # the week or 確保 before it, 長輩 after it: the family again
    "上周全家園遊會",
    "下周全家園遊會",
    "這周全家教會聚會",
    "本周全家園遊會",
    "每周全家園遊會",
    "確保全家長輩都打疫苗",
    "確保全家用藥",
    "安全家長輩",
    "隔周全家用餐",
    "這周全家居家防疫",
    "吃完全家用餐",
    # the shop and the family in one line
    "去全家便利商店買菜，全家聚餐",
]

SHOP = [
    "全家便利商店買牛奶",
    "全家便利店",
    "全家便利超商取貨",
    "全家超商繳費",
    "全家門市",
    "全家分店",
    "全家店到店",
    "全家取貨",
    "全家繳費",
    "全家取件",
    "全家寄件",
    "全家寄貨",
    "全家領貨",
    "全家領包裹",
    "全家寄包裹",
    "全家取包裹",
    "全家拿包裹",
    "全家退貨",
    # a clinic or a pharmacy of that name (Andrew, 11/10)
    "全家診所回診",
    "全家聯合診所",
    "全家藥局領藥",
    "全家藥房",
    # someone goes there
    "去全家買牛奶",
    "去全家",
    "週六去全家",
    "回去全家拿東西",
    "到全家拿包裹",
    "去全家幫媽媽買牛奶",
    "去趟全家",
    "去一趟全家",
    "去一下全家",
    "過去一趟全家",
    # half of another word
    "交通安全家長說明會",
    "安全家居檢查",
    "健全家庭講座",
    "保全家裡裝設備",
    "齊全家具店",
    "週六安全家訪",
    "安全家電",
    "安全家用梯",
    "健全家教",
    "健全家園",
    "成全家屬",
    "安全家事",
    "安全家政課",
    "安全家務",
]


@pytest.mark.parametrize("text", FAMILY)
def test_this_one_is_the_family(text):
    assert line_mentions.names_the_family(text)


@pytest.mark.parametrize("text", SHOP)
def test_this_one_is_the_shop(text):
    assert not line_mentions.names_the_family(text)


@pytest.mark.parametrize("text", ["全全家便利商店家", None, ""])
def test_what_is_cut_out_leaves_no_family_behind(text):
    # 「全」 and 「家」 on either side of the shop must not close up into a family that was never written.
    assert not line_mentions.names_the_family(text)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("全家聚餐", ["全家"]),
        ("帶全家去花蓮", ["全家"]),
        ("去全家便利商店買菜，全家聚餐", ["全家"]),
        ("全家便利商店買牛奶", []),
        ("去全家買牛奶", []),
        ("到全家取貨", []),
        ("全家對面的餐廳", ["全家"]),
        ("全家診所回診", []),
        ("去全家幫媽媽買牛奶", ["媽媽"]),
        ("媽媽去全家便利商店", ["媽媽"]),
        (None, []),
    ],
)
def test_a_corrected_title_tells_the_shop_from_the_family(title, expected):
    import calendar_db

    assert calendar_db._corrected_title_participants(title) == expected


@pytest.mark.parametrize(
    "text",
    [
        "明天去全家對面看牙醫",
        "全家診所回診",
        "全家藥局領藥",
        "去全家領藥",
        "到全家便利商店領藥",
    ],
)
def test_the_shop_leaves_the_visit_with_the_sender(text, monkeypatch):
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "成員甲")

    assert main._infer_medical_actor(text, "U_A") == "成員甲"


@pytest.mark.parametrize(
    "text",
    [
        "全家打疫苗",
        "我們全家週六打疫苗",
        "帶全家去打疫苗",
        "現在全家打疫苗",
        "下週輪到全家打疫苗",
        "全家看牙醫",
        "回去全家一起打疫苗",
        # a place next to it is the family, at the doctor too (Andrew, 11/12)
        "全家旁邊的診所回診",
        "明天全家對面的牙醫回診",
    ],
)
def test_the_whole_family_at_the_doctor_is_still_the_whole_family(text, monkeypatch):
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "成員甲")

    assert main._infer_medical_actor(text, "U_A") == "全家"


def test_someone_named_next_to_the_shop_is_the_patient(monkeypatch):
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "成員甲")

    assert main._infer_medical_actor("媽媽明天去全家對面看牙醫", "U_A") == "媽媽"


def test_a_name_that_touches_the_shop_is_still_read_whole(monkeypatch):
    # 「到全家甲那裡」 for a member called 家甲: the shop's 全家 must not take half of the name with it.
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "成員甲")
    monkeypatch.setattr(main, "_FAMILY_ACTOR_TERMS", (*main._FAMILY_ACTOR_TERMS, "家甲"))

    assert main._infer_medical_actor("明天到全家甲那裡回診", "U_A") == "家甲"


# ── the reply path ────────────────────────────────────────────────────────────────────────────────


def _pinged(text: str) -> set[str]:
    _sent, message = main._text_message_with_mentions(text, explicit_only=True)
    return {
        mention.mentionee.user_id
        for mention in (getattr(message, "substitution", None) or {}).values()
        if getattr(mention.mentionee, "user_id", None)
    }


@pytest.mark.parametrize(
    "text",
    [
        _half("寄到 x＠allen.example"),
        "看 https://ig.example/@allen/1",
        "看 https://ig.example/＠allen",
        "看 https://ig.example/@成員甲",
        "看 ig.example/@成員甲/1",
        "x@成員甲",
        "@@成員甲",
    ],
)
def test_a_reply_does_not_ring_a_member_inside_a_link_or_an_address(allen_is_a_member, text):
    assert _pinged(text) == set()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@成員甲 你看一下", {"U_A"}),
        ("＠成員甲 你看一下", {"U_A"}),
        ("大家@成員甲", {"U_A"}),
        ("（@成員甲）", {"U_A"}),
        ("@成員甲/@成員乙", {"U_A", "U_B"}),
        ("@成員甲@成員乙", {"U_A", "U_B"}),
        ("@成員甲、@成員乙", {"U_A", "U_B"}),
        ("@allen 你看一下", {"U_ALLEN"}),
        ("https://ig.example/a @成員甲 看一下", {"U_A"}),
    ],
)
def test_a_reply_still_rings_who_it_names(allen_is_a_member, text, expected):
    assert _pinged(text) == expected
