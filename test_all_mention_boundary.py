"""2026-11-07: 「@all」 rings every phone in the family, so it has to stand on its own.

`"@all" in reply_text` also took 「@allen」, an address at a domain that opens with "all" and a TikTok
link with such a handle, and, since the CLI path turns 「＠」 back into "@" (e822184), the same words
in full width.
This is the reply text only: a family message that makes a reminder is the next task
(the TODO in line_mentions.aliases_mentioned_in_text).  All text is made up.

These tests ask what counts as an @all, so they build with `allow_all_mention=True`.  Who may ping the
group at all: test_model_all_mention.py.
"""

from __future__ import annotations

import pytest

import claude_client as cc
import family_poll
import line_mentions
import main
import reminder_push


def _half(text: str) -> str:
    """Addresses are written with 「＠」: the privacy audit refuses a literal one anywhere in the tree."""
    return text.replace("＠", "@")


REAL = [
    # the three the fix was asked to keep
    "@all 吃飯",
    "大家@all",
    "@all。",
    # the shape of the bot's own receipts
    "@all\n已新增提醒",
    "@all @成員甲\n已新增提醒",
    # further along the text
    "參加人：@all、@成員甲",
    "參加人：@成員甲、@all",
    "內容\n@all 吃飯",
    # a false one first does not hide the real one
    "@allen 和 @all 吃飯",
    _half("寄到 x＠alliance.example，@all 看一下"),
    # a space or a mark, then ASCII: only ". @ / -" carry a handle on
    "@all 18:00 吃飯",
    "@all\n18:00 吃飯",
    "@all，7點吃飯",
    "@all、Allen",
    # punctuation on either side
    "@all",
    "@all，吃飯",
    "@all、成員甲",
    "@all吃飯",
    "（@all）",
    "(@all)",
    "「@all」",
    "@all!",
    "@all?",
    "@all,",
    "@all:",
    "@all.",
    "@all...",
    "@all...Thanks",
    "@all…",
    "@all.吃飯",
    "@all-大家",
    "明天聚餐...@all 記得",
    "明天聚餐.@all 記得",
    "明天聚餐。@all 記得",
    "-@all 吃飯",
    "- @all 吃飯",
    "要不要吃飯?@all",
    "大家好~@all 吃飯",
    "參加人:@all",
    "@all@成員甲 吃飯",
    "@all/@成員甲",
    "連結 https://x.example/a @all 看一下",
    "連結 https://x.example/a，@all 看一下",
]

NOT_REAL = [
    # the three the fix was asked to stop
    "@allen 你看一下",
    _half("寄到 x＠alliance.example"),
    "https://www.tiktok.com/@allen/video/1",
    # one character on the left
    "信箱 x@all",
    "信箱 X@all",
    "信箱 1@all",
    "信箱 x_@all",
    "tiktok.com/@all",
    "@@all = []",
    # one character on the right
    "@allEN",
    "@all1",
    "@all_star",
    "@all.in",
    "@all.In",
    "@all.1",
    "@all._x",
    "@all-star",
    _half("@all＠mastodon.social"),
    "@all/package",
    # whole addresses and links
    _half("x＠all.com"),
    _half("x.y＠all.co"),
    _half("x-＠all.com"),
    _half("x+tag＠all.com.tw"),
    _half("成員甲＠all.com.tw"),
    "https://www.tiktok.com/@all",
    "https://www.tiktok.com/@all/video/1",
    "https://www.tiktok.com/@all?lang=zh",
    "https://www.tiktok.com/@all.in/video/1",
    "https://www.youtube.com/@allnews",
    # Rang before, and no longer does.  Chosen: one missed ping is a quiet message, a false one rings
    # every phone, and nothing the bot builds puts a word character, "/" or "@" against its @all.
    "hi@all 吃飯",
    "OK@all",
    "18:00@all 吃飯",
    "明天7pm@all",
    "@all18:00 吃飯",
    "@all.Thanks",
    "@all-see below",
    "@all/Member",
    "/@all",
    "@成員甲/@all",
    "@all@all",
    "@all@Member 吃飯",
    "@成員A@all 吃飯",
    # never counted
    "@All 吃飯",
    "@ALL 吃飯",
    "＠all 吃飯",  # off the CLI path nothing turns it back
    "all 吃飯",
    "去 mall 吃飯",
]


@pytest.fixture(autouse=True)
def _synthetic_aliases(monkeypatch):
    monkeypatch.setattr(
        line_mentions,
        "load_user_aliases",
        lambda: {"U_A": "成員甲", "U_B": "成員乙", "U_ALLEN": "allen"},
    )


def _substitution(text: str, **kwargs) -> dict:
    _sent, message = main._text_message_with_mentions(text, allow_all_mention=True, **kwargs)
    return getattr(message, "substitution", None) or {}


def _pings_everyone(text: str, **kwargs) -> bool:
    return "all" in _substitution(text, **kwargs)


@pytest.mark.parametrize("text", REAL)
def test_an_all_on_its_own_counts(text):
    assert line_mentions.has_all_mention(text)


@pytest.mark.parametrize("text", NOT_REAL)
def test_an_all_inside_something_else_does_not(text):
    assert not line_mentions.has_all_mention(text)


@pytest.mark.parametrize("text", [None, ""])
def test_no_text_is_no_mention(text):
    assert not line_mentions.has_all_mention(text)


@pytest.mark.parametrize("explicit_only", [True, False])
@pytest.mark.parametrize("text", REAL)
def test_a_real_all_pings_the_group(text, explicit_only):
    assert _pings_everyone(text, explicit_only=explicit_only)


@pytest.mark.parametrize("explicit_only", [True, False])
@pytest.mark.parametrize("text", NOT_REAL)
def test_the_rest_goes_out_without_pinging_the_group(text, explicit_only):
    sent, message = main._text_message_with_mentions(
        text, explicit_only=explicit_only, allow_all_mention=True
    )

    assert sent == text  # the answer below is about this text, not one a guard put in its place
    assert "all" not in (getattr(message, "substitution", None) or {})


def test_the_text_that_counts_is_the_one_that_goes_out():
    # `_md_to_line` drops the 「_」 that kept this one from counting, and the limit cuts this one off.
    assert _pings_everyone("_@all_ 開會", explicit_only=True)
    assert not _pings_everyone("字" * 4900 + " @all", explicit_only=True)


def test_a_member_called_allen_is_pinged_alone():
    # Before: {all} {p2}.  The handle was the whole group and the member too.
    substitution = _substitution("@allen 你看一下", explicit_only=True)

    assert set(substitution) == {"p1"}
    assert substitution["p1"].mentionee.user_id == "U_ALLEN"


def test_the_other_half_of_the_line_did_not_move():
    # 「全家」 counts where it did, and only there: 全家便利商店 in a reply must not ring anyone.
    assert _pings_everyone("全家 明天吃飯", explicit_only=False)
    assert not _pings_everyone("全家 明天吃飯", explicit_only=True)


@pytest.mark.parametrize(
    ("printed", "pings"),
    [
        # Decided 2026-11-06 (test_claude_cli_at_mention.py): a copied 「＠all」 acts like 「@all」.
        ("＠all 開會", True),
        ("＠allen 你看一下", False),
        ("寄到 x＠alliance.example", False),
        ("https://www.tiktok.com/＠allen/video/1", False),
    ],
)
def test_the_cli_path_judges_what_it_turned_back(printed, pings):
    assert _pings_everyone(cc._from_cli(printed), explicit_only=True) is pings


@pytest.fixture()
def poll(tmp_path, monkeypatch):
    import config

    db_path = tmp_path / "poll.db"
    monkeypatch.setattr(config.settings, "sqlite_path", str(db_path))
    monkeypatch.setattr(family_poll, "_DB_PATH", db_path)
    aliases = tmp_path / "user_aliases.json"
    aliases.write_text('{"U_A": "成員甲"}\n', encoding="utf-8")
    monkeypatch.setattr(family_poll, "_ALIASES_PATH", aliases)
    family_poll.init_db()
    return family_poll.create_poll("C_all_mention", "今天晚上可以吃飯嗎")


@pytest.mark.parametrize("formatter", [family_poll.format_created_reply, family_poll.format_nudge_reply])
def test_a_poll_opens_with_an_all_that_counts(poll, formatter):
    text = formatter(poll)

    assert text.startswith("@all 民調")
    assert _pings_everyone(text, explicit_only=True)


@pytest.mark.parametrize(
    ("people", "first_line", "keys"),
    [
        (["all"], "@all", {"all"}),
        (["all", "成員甲"], "@all @成員甲", {"all", "p2"}),
        (["成員甲", "all"], "@成員甲 @all", {"all", "p2"}),
    ],
)
def test_a_receipt_for_everyone_opens_with_an_all_that_counts(people, first_line, keys):
    receipt = main._format_reminder_write_confirmation(
        "created", "繳管理費", main.datetime(2099, 10, 1, 12, 0), people
    )

    assert receipt.startswith(first_line + "\n")
    assert set(_substitution(receipt, explicit_only=True)) == keys


def test_a_push_read_back_as_text_opens_with_an_all_that_counts():
    # main's fallback for a reminder riding on a reply: `item.get("message") or …(item["text"])`.
    row = {"action": "繳管理費", "remind_at": 1_800_000_000, "user_id": "", "mention_aliases": ["all"]}

    text, _message = reminder_push._build_push_text_and_message(row, "1d", now=1_799_900_000)

    assert text.startswith("@all\n")
    assert _pings_everyone(text, explicit_only=True)
