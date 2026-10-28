"""2026-10-24: 沒點名咪寶的行事曆查詢誤判（Bug 1，PLAN B 的 B5）。

10/13–10/17 有 6 則陳述句或長文（25／31／127／214／430／3866 字，2 則含 @）被當成行事曆
查詢：路由在擷取與抽取之前就回了「沒有家族行程」，還把那句話寫進對話記憶。根因是
`_CALENDAR_TIMING_QUERY_RE` 把「之前／上次」當疑問詞（別的地方也用它，所以不改它）。

做法：沒點名的路徑多一道形狀閘門 `_calendar_query_passive_ok`——短（≤60 字）、沒有 @、整句本身
就是行事曆問句；不合格的訊息走一般的擷取→抽取→burst。點名咪寶的查詢不受影響。
All sentences are synthetic.
"""
from __future__ import annotations

import re
import signal
import time

import pytest

import main

# 10/17 報告的三句合成句，加上同結構的陳述句、長文、@、混合訊息。
NOT_QUERIES = [
    "我之前去北海道的時候，把媽媽的舊外套都整理掉了",
    "上次去花蓮我把爸爸的相機借走了",
    "下週二我會回老家拿東西，有多的紙箱嗎？之前買的那幾個還在嗎",
    # structure of the real misfires: long forwarded text, @other member / @All, narrative
    "這是一篇很長的轉貼文章" + "，內容講的是以前的人怎麼安排旅行" * 8 + "，之前也有人去過台北",
    "@All 我下週三要回台北，之前買的那箱東西還在嗎",
    "@小明 媽媽之前去高雄的時候買了一堆鳳梨酥",
    "媽媽之前去台中的時候買了一堆太陽餅",
    "今天上班之前先去醫院看診，下午再回公司開會",
    "我10/8要去面試和拿證件。10/7晚上回台北，家裡還有雨傘嗎？",
    "爸爸下週三要回台北，家裡的鑰匙放哪裡嗎？",
]

QUERIES = [
    "明天有什麼行程？",
    "爸爸明天幾點要拿蛋糕",
    "媽媽什麼時候回台北",
    "米堡，媽媽什麼時候回家",
    "上次做胃鏡是什麼時候？",
    "媽媽上次什麼時候回台北",
    "明天，有什麼行程？",
    "媽媽明天回台北嗎？",
    "記得明天爸爸要開會嗎？",
    "後天有什麼活動",
]


@pytest.mark.parametrize("text", NOT_QUERIES)
def test_the_base_predicate_is_unchanged_for_the_statements_the_gate_catches(text):
    # The gate, not _is_calendar_query / _CALENDAR_TIMING_QUERY_RE, is what blocks these:
    # other callers still see 「之前／上次」 as a question word.
    if text.startswith("@") or len(text) > main._CAL_PASSIVE_MAX_CHARS:
        pytest.skip("rejected by the length/@ check before the predicate matters")
    assert main._CALENDAR_TIMING_QUERY_RE.search(text)


@pytest.mark.parametrize("text", NOT_QUERIES)
def test_passive_gate_rejects_statements(text):
    assert not main._calendar_query_passive_ok(text)


@pytest.mark.parametrize("text", QUERIES)
def test_passive_gate_keeps_real_questions(text):
    assert main._is_calendar_query(text), "precondition: the base predicate says query"
    assert main._calendar_query_passive_ok(text)


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        # 「之前／上次」 alone is narrative; it needs a time question word or a yes/no particle
        ("上次去花蓮把相機借走了", False),
        ("上次什麼時候去花蓮的", True),
        ("上次去花蓮是什麼時候", True),
        # a statement clause cannot borrow a question word from a neighbour, and the other way round
        ("明天要開會，媽媽幾點出門", False),
        ("爸爸明天要拿蛋糕，幾點？", True),
        ("明天，有什麼行程？", True),
        # a question that also STATES a schedule is a mixed message
        ("後天我要看診，明天有什麼行程？", False),
        # length: 60 characters is the limit, after NFKC
        ("明天有什麼行程？", True),
        ("", False),
        ("   ", False),
        (None, False),
        # a mention is never a question to the bot
        ("@爸爸 明天有什麼行程？", False),
        ("＠all 明天有什麼行程？", False),  # full-width @ folds to @ under NFKC
    ],
)
def test_passive_gate_shape_rules(text, ok):
    assert main._calendar_query_passive_ok(text) is ok


def test_the_length_limit_counts_characters_after_nfkc():
    base = "明天有什麼行程？"
    assert main._calendar_query_passive_ok(base)
    padded_ok = base + "。" * (main._CAL_PASSIVE_MAX_CHARS - len(base))
    padded_long = base + "。" * (main._CAL_PASSIVE_MAX_CHARS - len(base) + 1)
    assert len(padded_ok) == main._CAL_PASSIVE_MAX_CHARS
    assert main._calendar_query_passive_ok(padded_ok)
    assert not main._calendar_query_passive_ok(padded_long)


# ── 路由：沒點名的陳述句走一般的擷取→抽取→burst；真問句照舊在擷取之前就答 ────────────────────


def _route(monkeypatch, text, *, addressed=False):
    """Run _handle_text_message with the heavy collaborators stubbed; return what was called.

    Same stub set as test_main_calendar_query.test_unaddressed_natural_calendar_question_routes_before_burst,
    plus the stubs the statement path needs (capture, extraction, burst)."""
    calls = {"calendar": [], "capture": [], "extract": [], "burst": []}
    monkeypatch.setattr(main, "_try_handle_reminder_cancellation", lambda *a: False)
    monkeypatch.setattr(main, "_try_handle_quoted_calendar_correction", lambda *a: False)
    monkeypatch.setattr(main, "_extract_gemini_trigger", lambda *a: text if addressed else None)
    monkeypatch.setattr(main, "_explicit_range_reminder_result", lambda *a: None)
    monkeypatch.setattr(main, "_try_handle_missed_reminder_repair", lambda *a: False)
    monkeypatch.setattr(main, "_text_with_quote_context", lambda _m, _g, t: t)
    monkeypatch.setattr(main.feedback_collector, "in_feedback_window", lambda: False)
    monkeypatch.setattr(main, "_try_one_shot_reply", lambda *a: False)
    monkeypatch.setattr(main, "_try_handle_calendar_correction", lambda *a: False)
    monkeypatch.setattr(main, "_detect_user_correction", lambda *a: None)
    monkeypatch.setattr(main, "_handle_command", lambda *a: None)
    monkeypatch.setattr(main, "_is_todo_query", lambda _t: False)
    monkeypatch.setattr(main, "_handle_calendar_query", lambda _e, gid, t: calls["calendar"].append(t))
    monkeypatch.setattr(
        main, "_auto_capture_text_if_important", lambda *a: calls["capture"].append(a[1]) or False
    )
    monkeypatch.setattr(
        main, "_maybe_extract_reminder", lambda *a, **k: calls["extract"].append(a[0]) or None
    )
    monkeypatch.setattr(main, "_is_dinner_question", lambda _t: False)
    monkeypatch.setattr(main, "_is_web_research_question", lambda _t: False)
    monkeypatch.setattr(main, "_requires_public_research", lambda _t: False)
    monkeypatch.setattr(main, "_try_piggyback_reminders_fast_path", lambda *a: False)
    monkeypatch.setattr(main, "_handle_explicit_text", lambda *a, **k: None)
    monkeypatch.setattr(main.burst_filter, "add_to_burst", lambda *a, **k: calls["burst"].append(a[2]))
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda *a, **k: [])
    import food_signals
    import knowledge_graph

    monkeypatch.setattr(food_signals, "extract_and_store_async", lambda *a, **k: None)
    monkeypatch.setattr(knowledge_graph, "auto_extract_kg_async", lambda *a, **k: None)

    class FakeMessage:
        id = "m1"
        quoted_message_id = None
        mention = None

    FakeMessage.text = text

    class FakeSource:
        user_id = "U1"

    class FakeEvent:
        message = FakeMessage()
        source = FakeSource()
        reply_token = "token"

    main._handle_text_message(FakeEvent(), "G1")
    return calls


@pytest.mark.parametrize("text", NOT_QUERIES)
def test_route_statement_skips_the_query_but_is_still_captured_and_extracted(monkeypatch, text):
    calls = _route(monkeypatch, text)
    assert calls["calendar"] == []                 # no 「沒有家族行程」 reply
    assert calls["capture"] == [text]              # auto capture ran
    assert calls["extract"] == [text]              # reminder extraction ran
    assert calls["burst"] == [text]                # and the message reached the normal burst path


@pytest.mark.parametrize("text", QUERIES)
def test_route_real_question_still_answers_from_the_calendar_before_capture(monkeypatch, text):
    calls = _route(monkeypatch, text)
    assert calls["calendar"] == [text]
    assert calls["capture"] == [] and calls["extract"] == [] and calls["burst"] == []


@pytest.mark.parametrize(
    "text",
    [
        "媽媽明天有什麼會議？",
        # An addressed question is the user asking the bot: the shape gate does not apply, so a
        # long one or one with a mention is still answered from the calendar.
        "請問一下，我想確認家裡接下來這一個月的行程安排，媽媽明天有什麼會議？爸爸後天有什麼行程？",
        "@爸爸 明天有什麼行程？",
    ],
)
def test_route_addressed_question_is_not_gated(monkeypatch, text):
    assert main._is_calendar_query(text)
    calls = _route(monkeypatch, text, addressed=True)
    assert calls["calendar"] == [text]
    assert calls["capture"] == [] and calls["extract"] == [] and calls["burst"] == []


# ── H5：新的 pattern 都是單純的詞表，不在重複之下；5k／20k 字都要線性 ────────────────────────────

_NEW_PATTERNS = {
    "_CAL_PAST_MARKER_RE": lambda text: main._CAL_PAST_MARKER_RE.search(text),
    "_CAL_TIME_QUESTION_RE": lambda text: main._CAL_TIME_QUESTION_RE.search(text),
    "_CAL_YESNO_RE": lambda text: main._CAL_YESNO_RE.search(text),
    "_CAL_ACTIVITY_WORD_RE": lambda text: main._CAL_ACTIVITY_WORD_RE.search(text),
    "_CAL_SCHEDULE_WORD_RE": lambda text: main._CAL_SCHEDULE_WORD_RE.search(text),
    "_CAL_CLAUSE_SPLIT_RE": lambda text: main._CAL_CLAUSE_SPLIT_RE.split(text),
}

# The gate stops at the length check, but the compiled patterns are also what a later edit might
# point at longer text, so they are timed directly on adversarial strings (near-misses of every
# alternative, runs of separators, digits and slashes).
_SHAPES = {
    "word soup": lambda n: ("之前上次什麼時候嗎吧呢" * (n // 10 + 1))[:n],
    "near-miss date prefixes": lambda n: ("下個週" * (n // 3 + 1))[:n],
    "週 run": lambda n: "週" * n,
    "digits and slashes": lambda n: ("12/" * (n // 3 + 1))[:n],
    "years and months": lambda n: ("2026年1月" * (n // 7 + 1))[:n],
    "回 near-misses": lambda n: ("回台" * (n // 2 + 1))[:n],
    "拿 near-misses": lambda n: ("拿蛋" * (n // 2 + 1))[:n],
    "clause marks": lambda n: "，" * n,
    "question marks": lambda n: "？" * n,
    "newlines": lambda n: "\n" * n,
    "spaces": lambda n: " " * n,
    "ideographic spaces": lambda n: "　" * n,
    "plain": lambda n: "字" * n,
    "date then nothing": lambda n: ("明天" * (n // 2 + 1))[:n],
}


class _Deadline(Exception):
    pass


def _seconds(check, text):
    """How long ``check(text)`` takes; a hang fails after 5 s instead of stalling the run."""

    def expire(*_):
        raise _Deadline(text[:12])

    previous = signal.signal(signal.SIGALRM, expire)
    outer = signal.setitimer(signal.ITIMER_REAL, 5)
    started = time.perf_counter()
    try:
        check(text)
        return time.perf_counter() - started
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if outer[0]:  # someone else's timer: give back what is left of it
            left = max(outer[0] - (time.perf_counter() - started), 0.001)
            signal.setitimer(signal.ITIMER_REAL, left, outer[1])


@pytest.mark.parametrize("name", sorted(_NEW_PATTERNS))
def test_new_calendar_patterns_run_in_linear_time(name):
    check = _NEW_PATTERNS[name]
    for shape, make in _SHAPES.items():
        small = min(_seconds(check, make(5000)) for _ in range(3))
        large = min(_seconds(check, make(20000)) for _ in range(3))
        assert large < 1.0, (name, shape)
        assert large < 10 * max(small, 0.002), (name, shape, small, large)  # 4× the text, not 16× the time


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_the_gate_itself_is_cheap_on_long_text(shape):
    make = _SHAPES[shape]
    small = min(_seconds(main._calendar_query_passive_ok, make(5000)) for _ in range(3))
    large = min(_seconds(main._calendar_query_passive_ok, make(20000)) for _ in range(3))
    assert large < 0.5, shape
    assert large < 10 * max(small, 0.002), (shape, small, large)
    assert main._calendar_query_passive_ok(make(20000)) is False


@pytest.mark.parametrize("name", ["_CAL_PAST_MARKER_RE", "_CAL_TIME_QUESTION_RE", "_CAL_YESNO_RE",
                                  "_CAL_ACTIVITY_WORD_RE", "_CAL_SCHEDULE_WORD_RE", "_CAL_CLAUSE_SPLIT_RE"])
def test_new_calendar_patterns_have_no_repeated_group(name):
    # H5 (line_bot.md 2026-10-05): a repeated alternation must split every string one way only.
    # These patterns are plain word lists, so the simplest way to keep that true is to have no
    # repeated group at all: no ``)*``, ``)+`` or ``){n,m}``.
    source = getattr(main, name).pattern
    assert not re.search(r"\)[*+]|\)\{", source), source
