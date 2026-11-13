"""2026-11-07 (Andrew): a reply the model wrote never pings the whole group, whatever it printed.

Only what the program wrote itself does: a poll, a reminder receipt, a due reminder.  A page, a video's
text or an article in the prompt can tell the model to print 「@all」, and `_md_to_line` can put one
together after any filter, so the rule is read off the message at the last step.  A blocked 「@all」
stays in the words.  What counts as an @all: test_all_mention_boundary.py.  All text is made up, and
nothing here reaches the network.

Still open, its own task (Andrew 2026-11-12): a model reply that @s every member by name pings each of them.
"""

from __future__ import annotations

import ast
import ipaddress
import json
import os
import socket
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import calendar_db
import family_poll
import gemini_client
import line_mentions
import main
import memory
import pending_store
import reminder_push
import test_schedule_capture as schedule
from test_poll_flow import G, _install_noop_side_modules, _make_text_event, poll_db  # noqa: F401
from test_research_verify_claims import ANSWER, ROW, research_env  # noqa: F401
from test_support_fake_line import BAD_TOKEN, GROUP, install_fake_line

REAL_REPLY = main._reply  # `research_env` puts a recorder in its place

# A clean one, and what comes out as one: `_md_to_line` takes the marks off, and
# `_strip_user_visible_mode_labels` joins the two ends of the last one.
SPELLINGS = [
    "@all 開會",
    "@**all** 開會",
    "@*all* 開會",
    "@_all_ 開會",
    "@__all__ 開會",
    "@al_l_ 開會",
    "@`all` 開會",
    "@（lite mode）all 開會",
]
SPELLING_IDS = ["clean", "bold", "star", "underscore", "double-underscore", "inner-underscore", "code", "mode-label"]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No name lookup and no TCP connection off this machine (UDP sends and subprocesses are not
    covered): these tests drive the bot's widest entry points."""
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo, real_gethostbyname = socket.getaddrinfo, socket.gethostbyname
    real_gethostbyname_ex, real_gethostbyaddr = socket.gethostbyname_ex, socket.gethostbyaddr

    def local(host) -> bool:
        try:
            return ipaddress.ip_address(str(host)).is_loopback
        except ValueError:
            return str(host) == "localhost"

    def connect(sock, address, *args, **kwargs):
        if isinstance(address, tuple) and not local(address[0]):
            raise OSError("no network in these tests")
        return real_connect(sock, address, *args, **kwargs)

    def connect_ex(sock, address, *args, **kwargs):
        if isinstance(address, tuple) and not local(address[0]):
            raise OSError("no network in these tests")
        return real_connect_ex(sock, address, *args, **kwargs)

    def getaddrinfo(host, *args, **kwargs):
        if host and not local(host):
            raise socket.gaierror("no network in these tests")
        return real_getaddrinfo(host, *args, **kwargs)

    def gethostbyname(host):
        if host and not local(host):
            raise socket.gaierror("no network in these tests")
        return real_gethostbyname(host)

    def gethostbyname_ex(host):
        if host and not local(host):
            raise socket.gaierror("no network in these tests")
        return real_gethostbyname_ex(host)

    def gethostbyaddr(host):
        if host and not local(host):
            raise socket.herror("no network in these tests")
        return real_gethostbyaddr(host)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", gethostbyname)
    monkeypatch.setattr(socket, "gethostbyname_ex", gethostbyname_ex)
    monkeypatch.setattr(socket, "gethostbyaddr", gethostbyaddr)


@pytest.fixture(autouse=True)
def _synthetic_aliases(tmp_path, monkeypatch):
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"U_A": "成員甲"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(tmp_path / "no_roles.json"))


@pytest.fixture(autouse=True)
def _no_extraction_model(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise RuntimeError("no model in these tests")

    monkeypatch.setattr(gemini_client, "extract_reminder", refuse)


@pytest.fixture()
def line(monkeypatch):
    return install_fake_line(monkeypatch)


@pytest.fixture()
def poll(poll_db):  # noqa: F811
    return family_poll.create_poll(G, "今天晚上可以吃飯嗎")


def say(text, **kwargs):
    return main._reply("TOKEN-ALL", text, group_id=GROUP, **kwargs)


def pings_everyone(message) -> bool:
    return "all" in (getattr(message, "substitution", None) or {})


def through_the_text_handler(text: str, message_id: str) -> None:
    with (
        patch("main.feedback_collector.in_feedback_window", return_value=False),
        patch("main._try_handle_calendar_correction", return_value=False),
        patch("main._detect_user_correction"),
        patch("main._auto_capture_text_if_important"),
        patch("main.burst_filter.add_to_burst"),
    ):
        main._handle_text_message(_make_text_event(text, msg_id=message_id), G)


# ── what the model wrote ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", SPELLINGS, ids=SPELLING_IDS)
def test_each_spelling_really_comes_out_as_an_all(text):
    # Without this the tests below that take SPELLINGS could pass for nothing.
    assert line_mentions.has_all_mention(main._prepare_outbound_text(text, source="reply"))


@pytest.mark.parametrize("text", SPELLINGS, ids=SPELLING_IDS)
def test_a_model_reply_does_not_ping_the_group(line, text):
    assert say(text)

    [message] = line.replies[0]
    assert not pings_everyone(message)
    assert message.text == "@all 開會"


@pytest.mark.parametrize("text", SPELLINGS, ids=SPELLING_IDS)
def test_a_model_reply_sent_by_push_does_not_ping_the_group(line, text):
    line.script = [BAD_TOKEN]

    assert say(text)

    [(batch, _key)] = line.pushes
    assert not pings_everyone(batch[0])
    assert batch[0].text == "@all 開會"


@pytest.mark.parametrize("text", SPELLINGS, ids=SPELLING_IDS)
def test_an_article_the_research_path_read_cannot_ping_the_group(line, research_env, monkeypatch, text):  # noqa: F811
    # The case this was decided for: pages nobody here picked, and a model told to copy them.
    monkeypatch.setattr(main, "_reply", REAL_REPLY)
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [dict(ROW)])
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: f"{ANSWER}\n{text}")

    main._handle_web_research_question(research_env.event, GROUP, "輝達暴跌10 %多", addressed=False)

    [message] = line.replies[0]
    assert message.text == f"{ANSWER}\n@all 開會"
    assert not pings_everyone(message)


def test_a_member_on_the_same_line_is_still_pinged(line):
    assert say("@all @成員甲\n明天開會")

    [message] = line.replies[0]
    assert message.text == "{p1} @all\n明天開會"
    assert set(message.substitution) == {"p1"}
    assert message.substitution["p1"].mentionee.user_id == "U_A"


@pytest.mark.parametrize(
    ("command", "patched", "answer"),
    [
        ("/檢討", "main.review.run_weekly_review", ("@all 這週要檢討", [])),
        ("/閉嘴 不用回", "main._handle_layer2_correction", "@all 這週要檢討"),
    ],
)
def test_a_command_that_answers_in_the_models_words_does_not_ping_the_group(
    line, monkeypatch, command, patched, answer
):
    # The same call site sends a poll, so nothing about the call can tell them apart.
    _install_noop_side_modules(monkeypatch)

    with patch(patched, return_value=answer):
        through_the_text_handler(command, "M-model-command")

    assert "@all" in line.replies[0][0].text
    assert not pings_everyone(line.replies[0][0])


def test_a_stored_title_in_the_todo_list_does_not_ping_the_group(line, monkeypatch):
    monkeypatch.setattr(main, "_build_todo_status_reply", lambda *_args: "目前待辦/提醒：\n1. @all 開會")

    main._handle_todo_query(_make_text_event("/提醒清單"), GROUP, "/提醒清單")

    assert not pings_everyone(line.replies[0][0])


@pytest.mark.parametrize(
    "first",
    [
        "一般的回覆",
        line_mentions.AllowsAllMention("@all 民調開好了：今晚可以嗎"),
        main.ReminderReceipt("@all\n已新增提醒\n事項：繳管理費"),
    ],
    ids=["after-a-model-reply", "after-a-poll", "after-a-receipt"],
)
def test_a_queued_answer_riding_on_a_reply_does_not_ping_the_group(line, monkeypatch, tmp_path, first):
    # The legacy pending queue is off; a rollback turns it on.  What it holds is a model's answers,
    # whatever the message it rides behind may do.
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: True)
    monkeypatch.setattr(main, "_DRAIN_LOCK_DIR", str(tmp_path / "drain_locks"))
    monkeypatch.setattr(
        main,
        "_load_pending_explicit",
        lambda *_a, **_k: {GROUP: [{"type": "text", "message_id": "m1", "user_id": "U_A", "text": "早安"}]},
    )
    monkeypatch.setattr(main, "_quota_exhausted", lambda *_a, **_k: False)
    monkeypatch.setattr(pending_store, "ensure_message_ids", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "_peek_text_pending_for_drain", lambda *_a, **_k: [("@all 排隊的回覆 @成員甲", "m1")])

    assert say(first)

    _first, queued = line.replies[0]
    assert "@all" in queued.text
    assert set(queued.substitution) == {"p1"}


@pytest.mark.parametrize(
    ("text", "explicit_only"),
    [("@all 開會", True), ("@all 開會", False), ("全家 明天吃飯", False)],
)
def test_the_builder_says_no_unless_it_is_told(text, explicit_only):
    assert not pings_everyone(main._text_message_with_mentions(text, explicit_only=explicit_only)[1])
    assert pings_everyone(
        main._text_message_with_mentions(text, explicit_only=explicit_only, allow_all_mention=True)[1]
    )


# ── what the program wrote ───────────────────────────────────────────────────────────────────

POLL_FORMATTERS = [family_poll.format_created_reply, family_poll.format_nudge_reply]


@pytest.mark.parametrize("formatter", POLL_FORMATTERS)
def test_a_poll_pings_the_group(line, poll, formatter):
    assert say(formatter(poll))

    assert pings_everyone(line.replies[0][0])


@pytest.mark.parametrize("formatter", POLL_FORMATTERS)
def test_a_poll_sent_by_push_pings_the_group(line, poll, formatter):
    line.script = [BAD_TOKEN]

    assert say(formatter(poll))

    assert pings_everyone(line.pushes[0][0][0])


def test_a_receipt_for_everyone_pings_the_group_and_the_same_words_as_a_plain_str_do_not(line):
    words = main._format_reminder_write_confirmation(
        "created", "繳管理費", main.datetime(2099, 10, 1, 12, 0), ["all"]
    )

    assert say(main.ReminderReceipt(words))
    assert say(str(words))

    assert pings_everyone(line.replies[0][0])
    assert not pings_everyone(line.replies[1][0])


@pytest.mark.parametrize(
    ("text", "message_id"),
    [
        ("/民調 今晚可以嗎", "M-poll-command"),  # `_handle_command`, the call site model words share
        ("咪寶 幫我做民調：今晚可以嗎", "M-poll-explicit"),
        ("提醒我明天10:00開會 @all", "M-receipt"),
    ],
)
def test_the_text_handler_hands_the_mark_on(line, poll_db, monkeypatch, text, message_id):  # noqa: F811
    # A `.strip()` or an f-string on the way would make these quiet, and nothing else would notice.
    _install_noop_side_modules(monkeypatch)

    through_the_text_handler(text, message_id)

    assert pings_everyone(line.replies[0][0])


@pytest.mark.parametrize("nudge", ["/催民調", "/提醒民調"])
def test_a_nudge_through_the_text_handler_pings_the_group(line, poll_db, monkeypatch, nudge):  # noqa: F811
    _install_noop_side_modules(monkeypatch)

    through_the_text_handler("/民調 今晚可以嗎", "M-poll-open")
    through_the_text_handler(nudge, "M-poll-nudge")

    texts = [message.text for batch in line.replies for message in batch]
    assert len(line.replies) == 2 and "民調更新" in texts[1], texts
    assert pings_everyone(line.replies[1][0])


def test_the_second_poll_site_hands_the_mark_on(line, poll_db):  # noqa: F811
    text = "幫我做民調：今晚可以嗎"

    main._handle_explicit_text(_make_text_event(text, msg_id="M-poll-second"), G, text)

    assert pings_everyone(line.replies[0][0])


def test_a_quoted_schedule_receipt_reaches_reply_with_its_mark(monkeypatch):
    sent, _explicit = schedule._capture_setup(monkeypatch)
    schedule._seed_raw(schedule._trip_text(schedule._base_day()))

    main._handle_text_message(
        schedule._text_event("咪寶", message_id="m-cmd", user_id=schedule.U_OTHER, quoted="m-src"), schedule.G
    )

    assert len(sent) == 1 and isinstance(sent[0], line_mentions.AllowsAllMention)


def test_a_followup_receipt_reaches_reply_with_its_mark(monkeypatch):
    schedule._seed_followup("pending")
    sent: list[str] = []
    monkeypatch.setattr(main, "_reply", lambda _token, text, **_kw: sent.append(text) or True)
    monkeypatch.setattr(main.burst_filter, "cancel_burst", MagicMock(return_value=[]))
    monkeypatch.setattr(memory, "consume_open_stages", lambda *_a, **_k: None, raising=False)

    assert main._try_handle_creation_followup(
        schedule._text_event("加到提醒", message_id="m-fcmd", quoted="m-fsrc"), schedule.G, "加到提醒"
    )

    assert len(sent) == 1 and isinstance(sent[0], line_mentions.AllowsAllMention)


def test_a_hand_written_one_shot_keeps_its_all(line, monkeypatch):
    monkeypatch.setattr(main, "_load_one_shot_replies", lambda: {GROUP: "@all 明天停水"})
    monkeypatch.setattr(main, "_save_one_shot_replies", lambda _data: None)

    assert main._try_one_shot_reply(_make_text_event("早安"), GROUP)

    assert pings_everyone(line.replies[0][0])


# ── riders on a model reply: their mention is their own ──────────────────────────────────────


def _arm_a_due_reminder_for_everyone(monkeypatch, *, built: bool) -> None:
    row = {"action": "繳管理費", "remind_at": 1_800_000_000, "user_id": "", "mention_aliases": ["all"]}
    text, message = reminder_push._build_push_text_and_message(row, "1d", now=1_799_900_000)
    item = {
        "reminder_id": 42,
        "group_id": GROUP,
        "stage": "1d",
        "text": text,
        "action": row["action"],
        "remind_at": row["remind_at"],
        "weekly_count": 0,
    }
    if built:
        item["message"] = message
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: True)
    monkeypatch.setattr(reminder_push, "due_reminders_for_reply", lambda *_a, **_k: [item])
    monkeypatch.setattr(calendar_db, "list_due_for_reminder", lambda *_a, **_k: [])
    monkeypatch.setattr(memory, "is_reminder_pending", lambda *_a, **_k: True)
    monkeypatch.setattr(
        memory,
        "claim_natural_reminder_delivery",
        lambda *_a, **_k: {"reminder_id": 42, "claim_token": "c42"},
    )
    monkeypatch.setattr(memory, "finalize_natural_reminder_delivery", lambda _claim: True)


@pytest.mark.parametrize("built", [True, False], ids=["built", "read-back-as-text"])
def test_a_due_reminder_riding_on_a_model_reply_still_pings_the_group(line, monkeypatch, built):
    _arm_a_due_reminder_for_everyone(monkeypatch, built=built)

    assert say("@all 開會")

    model_reply, reminder = line.replies[0]
    assert not pings_everyone(model_reply)
    assert pings_everyone(reminder)


def test_a_confirmation_riding_on_a_model_reply_still_pings_the_group(line):
    memory.enqueue_reminder_confirmation(GROUP, "pending_reminder:1", "@all\n已新增提醒\n事項：繳管理費")

    assert say("@all 開會")

    model_reply, confirmation = line.replies[0]
    assert not pings_everyone(model_reply)
    assert pings_everyone(confirmation)


# ── every place that can ping the whole group, as one list ──────────────────────────────────

_MARKS = {"AllowsAllMention", "ReminderReceipt"}
# what these store goes out with allow_all_mention=True
_WRITERS = {"queue_one_shot_reply", "_save_one_shot_replies", "enqueue_reminder_confirmation"}
_BUILDER = "_text_message_with_mentions"
# where a mention of the whole group is made without reading any text: the due reminders and events
_ALL_SOURCES = {"text_v2_dict", "sdk_message_from_text_v2_dict", "reminder_actor_targets", "event_mention_targets"}

# 2026-11-12.  Before a line here is added or changed: is every word of that text the program's own, or
# typed by a person?  Never a model's reply, never a page it read.
MAY_PING = {
    # text the program put together; an @all in it pings
    "family_poll.format_created_reply marks a text": 1,
    "family_poll.format_nudge_reply marks a text": 1,
    "main.ReminderReceipt is a mark": 1,
    # TODO(2026-11-12 deferred, the reminder-path task): the receipts below can carry a title or 事項 an
    # extraction model or a pasted message wrote, so an @all in those words still pings.
    "main._create_schedule_reminders marks a text": 1,
    "main._creation_followup_reply marks a text": 1,
    "main._format_schedule_receipt marks a text": 2,
    "main._format_source_calendar_capture_confirmation marks a text": 2,
    "main._maybe_extract_reminder marks a text": 1,
    # the one place a text's @all is read
    "main._reply builds with allow_all_mention": 2,  # the reply and its push fallback: by the text's type
    "main._reply builds with True": 2,  # riders the program wrote: a stored confirmation, a due reminder
    "main._try_one_shot_reply builds with True": 1,  # a file a person writes by hand
    "main._text_message_with_mentions makes an all target": 1,
    # never pings: listed so that a change here is seen
    "main._reply builds with False": 1,  # a queued answer riding along: a model wrote it
    "main._drain_pending_for_group builds with False": 1,  # a queued answer: a model wrote it
    "main._exercise_nudge_message builds with nothing": 1,
    "main._handle_calendar_query builds with nothing": 1,
    "main._handle_todo_query builds with nothing": 1,
    "main._try_one_shot_reply calls _save_one_shot_replies": 2,  # takes the text out of the file
    "main.queue_one_shot_reply calls _save_one_shot_replies": 1,  # nothing in the code queues one
    # mentions made from the people a reminder or an event names (mention_aliases, participants)
    "line_mentions.event_mention_targets makes an all target": 1,
    "line_mentions.reminder_actor_targets makes an all target": 1,
    "line_mentions.text_v2_dict writes a type-all mention": 1,
    "line_mentions.sdk_message_from_text_v2_dict makes an SDK all target": 1,
    "main._text_message_with_mentions calls text_v2_dict": 1,
    "main._text_message_with_mentions calls sdk_message_from_text_v2_dict": 1,
    "reminder_push._build_push_text_and_message calls sdk_message_from_text_v2_dict": 1,
    "event_reminder.sdk_message_from_spec calls sdk_message_from_text_v2_dict": 2,  # a stored push, sent again
    "reminder_push._build_push_text_and_message calls reminder_actor_targets": 1,
    "reminder_push._build_push_text_and_message calls text_v2_dict": 1,
    "reminder_push._reminder_audience calls reminder_actor_targets": 1,  # who it pings, to fold duplicates
    "reminder_push._event_audience_covered calls event_mention_targets": 1,  # the same, for an event
    "event_reminder.build_reminder_message_spec calls event_mention_targets": 1,
    "event_reminder.build_reminder_message_spec calls text_v2_dict": 1,
    # 2026-11-12 「提到人就真 @」 (Andrew): a push that names a family member @s them.  The helper only
    # sends what its callers built; the whole-group callers are Andrew's 「@ 全家」 choices.
    "line_push_client.push_text_with_mentions calls text_v2_dict": 1,
    # every word is the program's: a role, a label from a fixed list, a time, a fixed sentence
    "parent_health._red_flag_notice makes an all target": 1,
}

def _called(node) -> str:
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""


class _PingSites(ast.NodeVisitor):
    def __init__(self, module: str):
        self.module, self.scope, self.found = module, [], Counter()

    def _scoped(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _scoped

    def visit_ClassDef(self, node):
        if any(_called(base) in _MARKS for base in node.bases):
            self.found[f"{self.module}.{node.name} is a mark"] += 1
        self._scoped(node)

    def visit_Call(self, node):
        name, where = _called(node.func), ".".join([self.module, *self.scope])
        keywords = {kw.arg: kw.value for kw in node.keywords}
        if name in _MARKS:
            self.found[f"{where} marks a text"] += 1
        elif name == _BUILDER:
            if None in keywords:  # **kwargs could carry the flag: show what is spread in
                how = "**" + ast.unparse(keywords[None])
            elif "allow_all_mention" in keywords:
                how = ast.unparse(keywords["allow_all_mention"])
            else:
                how = "nothing"
            self.found[f"{where} builds with {how}"] += 1
        elif name in _WRITERS or name in _ALL_SOURCES:
            self.found[f"{where} calls {name}"] += 1
        elif name == "MentionTarget" and (
            None in keywords or ast.unparse(keywords.get("kind", ast.Constant(None))) != "'user'"
        ):
            self.found[f"{where} makes an all target"] += 1
        elif name == "AllMentionTarget":
            self.found[f"{where} makes an SDK all target"] += 1
        elif name == "dict" and ast.unparse(keywords.get("type", ast.Constant(None))) == "'all'":
            self.found[f"{where} writes a type-all mention"] += 1
        self.generic_visit(node)

    def visit_Dict(self, node):
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant) and key.value == "type"
                and isinstance(value, ast.Constant) and value.value == "all"
            ):
                self.found[f"{'.'.join([self.module, *self.scope])} writes a type-all mention"] += 1
        self.generic_visit(node)


def _ping_sites(root: Path) -> dict:
    found: Counter = Counter()
    for folder, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if not name.startswith((".", "__"))]  # worktrees and venvs live under dot folders
        for name in files:
            if not name.endswith(".py") or name.startswith("test_") or name == "conftest.py":
                continue
            path = Path(folder) / name
            visitor = _PingSites(path.relative_to(root).with_suffix("").as_posix())
            visitor.visit(ast.parse(path.read_text(encoding="utf-8")))  # every file: a new one may write {'type': 'all'}
            found += visitor.found
    return dict(found)


def test_every_place_that_can_ping_the_whole_group_is_on_the_list():
    # It stops a copied or tidied-up line, not someone set on getting around it (an alias, type(x)(...)).
    assert _ping_sites(Path(main.__file__).resolve().parent) == MAY_PING
