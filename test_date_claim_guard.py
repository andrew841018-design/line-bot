"""2026-10-28 Andrew：bot 說 11/28「並非台灣的投票日」，還說「下一次」總統大選在 2025 年。

11/28 是 2026 年地方選舉（九合一）投票日；「下一次」也不可能落在過去。當時 Claude CLI
已登出、Gemini 2.5 flash 額度用完，回覆來自 2.5-flash-lite，提示詞裡的今天日期和「沒有
資料不糾正近期公開事實」都沒擋住，所以改成每則生成的回覆都經過決定性檢查。同日 15:03
又回「總統及立委選舉已於2025年1月11日舉行完畢，因此『台灣大選已近』的說法不符現況」
（使用者沒寫日期），所以也檢查「否認選舉時間」，並把查證過的選舉日程放進提示詞。
All fixtures are synthetic or public-schedule phrasing; no real chat content.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from pathlib import Path

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import civic_calendar  # noqa: E402
import gemini_client  # noqa: E402
import main  # noqa: E402
import reply_policy  # noqa: E402
import reply_provenance  # noqa: E402

TODAY = date(2026, 10, 28)
USER = "補助到月底，投票日11月28日那週人比較多。"
WRONG = (
    "2026年11月28日並不是台灣的投票日。\n"
    "台灣下一次的總統大選預計在2025年1月11日舉行。\n"
    "因此把補助期限和投票日連在一起，可能有所誤解。"
)
RIGHT = "2026年11月28日將舉行地方公職人員選舉，也就是九合一選舉。"


_ORIGINAL = reply_policy.date_claim_findings


def _findings(reply, **kwargs):
    kwargs.setdefault("today", TODAY)
    return _ORIGINAL(reply, **kwargs)


def _at_today(monkeypatch):
    """main computes today itself; pin it to TODAY."""
    monkeypatch.setattr(
        main.reply_policy, "date_claim_findings",
        lambda reply, *, today, **kw: _ORIGINAL(reply, today=TODAY, **kw),
    )


# ── 1. the reply that went out ───────────────────────────────────────────────

def test_the_wrong_correction_is_caught_twice():
    assert sorted(_findings(WRONG, user_text=USER)) == ["denied_date", "past_upcoming", "presidential_date"]
    # each sentence alone is enough
    assert sorted(_findings("台灣下一次的總統大選預計在2025年1月11日舉行。")) == [
        "past_upcoming", "presidential_date",
    ]
    assert _findings("2026年11月28日並不是台灣的投票日。", user_text=USER) == ["denied_date"]


def test_the_right_answer_still_goes_out():
    # the follow-up quoted the wrong reply, so 11/28 is a stated date there too
    assert _findings(RIGHT, user_text=USER + "\n" + WRONG) == []
    assert _findings("對，11月28日是投票日，記得帶身分證和印章。", user_text=USER) == []


# ── 2. a past date called upcoming ───────────────────────────────────────────

@pytest.mark.parametrize("reply", [
    "下一屆選舉預計在民國114年1月11日。",
    "下一次投票預計在2025/1/11。",
    "2025年1月11日將舉行大選。",
    "這項指南還沒發布，預計會在2025年底公布。",
    "影城預計於 2025 年第二季正式營運。",
    "新館預計於2026年9月開幕。",  # month precision: September 2026 is past
    "下次總統大選在2025年。",
    "The next election is in 2025年1月.",
    # review 1: a label colon, and the clause that holds the claim decides
    "下一次總統大選：2025年1月11日。",
    "我已經確認，下一次總統大選預計在2025年1月11日舉行。",
    "下一次總統大選預計在2025年1月11日舉行，地方選舉是2026年11月28日。",
    "下一次大選2025年1月11日舉行。",
    # review 2: policy phrasing and a minguo year without 「民國」
    "2025年起將實施新制。",
    "新制將從2025年1月起上路。",
    "下一次總統大選在114年1月11日。",
    # review 3: an earlier date is no starting point unless told as one
    "上一次總統大選是2024年1月13日，下一次預計在2025年1月11日舉行。",
    "2024年大選已經結束，下一次總統大選預計在2025年1月11日。",
    "下一次總統大選預計在2025年1月11日舉行，這點你說的不對。",
    "你說11月28日投票，但下一次大選預計在2025年1月11日。",
    "下一次投票預計在２０２５年１月１１日。",  # full-width digits
])
def test_past_dates_told_as_coming_are_caught(reply):
    # a presidential election on that day is also caught on its own (round 6)
    assert set(_findings(reply)) - {"presidential_date"} == {"past_upcoming"}


@pytest.mark.parametrize("reply", [
    "下一次總統大選預計在2028年1月。",
    "預計2026年底完工。",  # this year: cannot tell
    "原定2025年1月11日舉行的活動已經取消。",
    "當時預計在2025年完工，後來延後了。",
    "工程預計從2025年延到2027年。",  # a later date makes it a change
    "今年1到9月已有167件，全年預計會超過2024年的185件。",  # 2024 is the comparison
    "日本央行在2026年9月18日將政策利率上調至1.25%。",  # 將 here means 把
    "聯合國大會在1973年12月18日通過決議。",
    "可參考《2024年會員調查報告》。",
    "上一次總統大選是2024年1月13日。",
    "影片說「下一次大選在2025年1月11日」，這是舊消息。",  # quoted
    "2025年1月11日那天下大雨。",
    # review 1: a denied schedule, and a date that belongs to something else
    "下一次總統大選不可能在2025年1月11日舉行。",
    "下一次投票請記得帶2024年換發的身分證。",
    "2025年1月11日舉行的選舉投票率很高。",
    # review 2: "next" counted from an earlier past date
    "2020年疫情後，下一次大型選舉是2022年的九合一。",
    # review 3: corrections, delays and old plans read as such
    "下一次總統大選在2025年1月11日的說法有誤，應該是2028年1月。",
    "網路傳說下一次大選預計在2025年1月舉行，這是舊資訊。",
    "我之前說下一次總統大選在2025年1月11日是錯的。",
    "捷運環狀線預計2025年完工，但已經延到2027年。",
    "這家店原訂2025年1月將開幕，後來延期了。",
    # a plan told as announced then (replay, 2026-07)
    "環保署在2023年7月13日預告草案，規定旅宿業者將從2025年起分階段不主動提供備品。",
])
def test_other_past_and_future_dates_pass(reply):
    assert _findings(reply) == []


def test_past_upcoming_holds_even_with_search_backing():
    reply = "台灣下一次的總統大選預計在2025年1月11日舉行。"
    assert "past_upcoming" in _findings(reply, supported_segments=[reply], backed=True)


# ── 3. denying a date the user gave ──────────────────────────────────────────

@pytest.mark.parametrize("reply", [
    "11月28日不是投票日。",
    "11/28並非選舉日，別記錯。",
    "投票日並不是11月28日。",
    "你說的11月28日投票有誤。",
    # review 1: punctuation, a weekday beside the date, and exemptions that
    # sit in another clause
    "11月28日，並不是投票日。",
    "11月28日星期六不是投票日。",
    "11月28日（六）不是投票日。",
    "11月28日不是投票日，如果要出遊可以放心安排。",
    "11月28日不是投票日，是不是記錯了？",
    # review 2: more ways to say it
    "11月28日並未舉行投票。",
    "11月28日非投票日。",
    "11/28不用投票喔。",
    # review 3
    "11月28日那天不是投票日。",
    "投票不是在11月28日。",
    "九合一不是11月28日投票，是11月29日。",
    "11月28日並不是「投票日」。",
    "11月28日（六），並不是投票日。",
    "１１／２８不是投票日。",
])
def test_denials_of_the_stated_date_are_caught(reply):
    assert _findings(reply, user_text="11/28要去投票") == ["denied_date"]


def test_denying_the_other_date_counts():
    assert _findings("截止日是12月12日，而非12月5日。", user_text="是不是12/5截止") == ["denied_date"]
    # the verified calendar backs it only when the sentence names the listed day
    assert _findings("投票日是11月28日，而非11月29日。", user_text="是不是11/29投票") == []
    # review 4 C1: alone it backs nothing — 11/3 is the US vote, a by-election is not listed
    assert _findings("11月3日並不是投票日。", user_text="美國11/3期中選舉投票") == ["denied_date"]
    assert _findings("12月20日不是投票日。", user_text="12/20立委補選投票") == ["denied_date"]


@pytest.mark.parametrize("reply", [
    "11月28日不是星期五，是星期六的投票日。",  # weekday arithmetic
    "11月28日不是投票日嗎？",  # a question
    "如果11月28日不是投票日，就改約下週。",  # a condition
    "10/1 沒有提醒衝突。",  # the bot's own list, not a public schedule
    "11月29日不是投票日。",  # nobody gave 11/29
    "「11月28日不是投票日」這種說法不對。",  # quoted
    # review 1: honest doubt, a warning not to get it wrong, an affirmation
    "我沒有資料確認11月28日是否投票。",
    "11月28日投票不要記錯，記得帶身分證。",
    "11月28日投票沒有錯。",
    # review 2: the honest answers the no-search contract asks for
    "我沒辦法確認11月28日是不是投票日，建議看中選會公告。",
    "11月28日有沒有投票，我沒辦法確認最新狀況。",
    "11月28日非常適合出遊，記得先投票。",
    # review 3: reminders, and what the user did not say about that date
    "11月28日投票不要搞錯了，記得帶身分證。",
    "11月28日投票千萬別弄錯了。",
    "11月28日不需要登記，直接去投票所投票就好。",
    "11月28日沒有公投，只有九合一投票。",
])
def test_other_negations_pass(reply):
    assert _findings(reply, user_text="11/28投票，10/1回診") == []


def test_a_denial_with_backing_passes():
    # Taiwan's listed vote day is backed by nothing (round 6): not a search
    # segment saying the same, not a stored replay
    reply = "11月28日不是投票日。"
    user = "11/28要去投票"
    assert _findings(reply, user_text=user, supported_segments=["11月28日不是投票日"]) == ["denied_date"]
    assert _findings(reply, user_text=user, backed=True) == ["denied_date"]
    # another day's denial is backed by a segment stating it, or a stored replay
    deadline = "報名不是12月5日截止。"
    assert _findings(deadline, user_text="12/5截止", supported_segments=["報名不是12月5日截止"]) == []
    assert _findings(deadline, user_text="12/5截止", backed=True) == []
    # a search segment about something else backs nothing (review 1)
    assert _findings(reply, user_text=user, supported_segments=["補助每人兩晚"]) == ["denied_date"]
    assert _findings(reply, user_text=user, supported_segments=["11月28日是投票日"]) == ["denied_date"]
    assert _findings(
        "11月28日不是台灣的投票日。11月28日是台灣的補班日。", user_text=user,
        supported_segments=["11月28日是台灣的補班日"],
    ) == ["denied_date"]
    # review 3: evidence that says the user is right backs nothing; evidence
    # (or the verified calendar) giving the vote another date backs the denial
    assert _findings(reply, user_text=user, evidence_text="中選會：投票日為11月28日") == ["denied_date"]
    # the verified calendar outranks evidence for Taiwan's listed vote day
    assert _findings(reply, user_text=user, evidence_text="中選會：11月28日不是投票日") == ["denied_date"]
    assert _findings(
        "報名不是12月5日截止。", user_text="12/5截止", evidence_text="主辦單位：12月5日不是截止日",
    ) == []
    assert _findings("11月29日不是投票日。", user_text="11/29投票") == ["denied_date"]
    assert _findings(
        "12月5日不是截止日。", user_text="12/5截止", evidence_text="報名截止日為12月12日",
    ) == []
    assert _findings(reply, user_text=user, evidence_text="旅遊補助每人兩晚") == ["denied_date"]
    assert _findings(reply, user_text=user, evidence_text="2022年11月26日舉行九合一投票") == ["denied_date"]


def test_a_page_cannot_move_what_it_is_not_about_or_a_listed_vote():
    # review 4 I4: a concert moved, or a rumour that the CEC moved the vote
    assert _findings(
        "11月28日不是投票日。", user_text="11/28投票", material_text="原訂11月28日的演唱會取消",
    ) == ["denied_date"]
    assert _findings(
        "11月28日不是投票日，已延期到12月5日。",
        material_text="網傳中選會宣布11月28日投票延期至12月5日",
    ) == ["denied_date"]


def test_a_type_correction_keeps_the_day():
    # review 4 I3: which vote, not whether there is one
    assert _findings("11月28日不是總統大選，是九合一地方選舉。", user_text="台灣大選11/28") == []
    assert _findings("11月28日不是投票日，是補班日。", user_text="11/28投票") == ["denied_date"]


def test_a_shared_page_that_moved_the_date_backs_the_retelling():
    # review 2 I2: the page itself says the deadline moved
    moved = "原訂11月1日截止，現延長至11月15日"
    reply = "報名不是11月1日截止，已延長到11月15日。"
    assert _findings(reply, material_text=moved) == []
    # a page that only gives the date backs no denial of it (review 3 N1)
    assert _findings("報名不是11月1日截止。", material_text="11月1日截止，名額有限") == ["denied_date"]
    assert _findings("報名不是11月1日截止。", material_text="原本規劃不變，11月1日截止") == ["denied_date"]
    # quotes in what the user wrote still give the date (review 3 N2)
    assert _findings("11月28日不是投票日。", user_text="新聞說「11/28投票」") == ["denied_date"]


# ── 3b. disputing when an election is (the 15:03 reply) ──────────────────────

SECOND = (
    "台灣的總統及立委選舉已於2025年1月11日舉行完畢，因此「台灣大選已近」的說法不符現況。\n"
    "美國期中選舉在11月初。"
)


def test_the_second_reply_is_caught():
    assert sorted(_findings(SECOND, user_text="美國和台灣大選都近了")) == [
        "election_timing", "presidential_date",
    ]
    assert _findings("這個說法關於台灣大選時點不準確。") == ["election_timing"]
    # research evidence does not make the dispute true; a search segment that
    # states it does
    assert _findings("「台灣大選已近」的說法不符現況。", evidence_text="九合一選舉11月28日投票") == [
        "election_timing"
    ]
    assert _findings(
        "「台灣大選已近」的說法不符現況。", supported_segments=["台灣大選已近的說法不符現況"],
    ) == []


def test_an_election_verdict_needs_its_own_clause():
    # review 4 I2: the question in the next clause does not excuse the verdict
    assert "election_timing" in _findings(SECOND.replace("。\n", "，你是不是記錯了？\n", 1))
    # a search segment backs only the same verdict
    assert _findings(
        "台灣大選已近不是事實。美國期中選舉不是總統大選。",
        supported_segments=["美國期中選舉不是總統大選"],
    ) == ["election_timing"]


@pytest.mark.parametrize("reply", [
    "台灣大選還沒到，九合一是11月28日投票。",
    "下一次總統大選不是今年，是2028年。",
    "九合一選舉是今年11月28日，不是明年。",
    "我沒辦法確認大選是不是快到了。",
    "大選快到了，記得投票。",
    # review 4 I2: no verdict on anyone's claim
    "年底選舉前，政府通常不會推出不受歡迎的政策。",
    "選舉過了就沒有人管了。",
    "今年沒有總統大選。下一次在2028年。",
    # a verdict on a date the calendar does not list
    "下一次總統大選在2025年1月11日的說法有誤，應該是2028年1月。",
])
def test_true_or_honest_election_lines_pass(reply):
    assert _findings(reply) == []


def test_the_calendar_line_is_in_every_prompt(monkeypatch):
    line = civic_calendar.election_context(TODAY)
    assert "2026年11月28日（週六）" in line and "2024年1月13日（週六）" in line
    assert "2028年" in line
    assert line in gemini_client._runtime_time_context()  # Gemini and Claude CLI
    assert line in main._runtime_time_context_for_local_llm()


def test_the_calendar_goes_quiet_instead_of_stale():
    assert civic_calendar.election_context(civic_calendar.VALID_UNTIL) != ""
    from datetime import timedelta
    assert civic_calendar.election_context(civic_calendar.VALID_UNTIL + timedelta(days=1)) == ""
    assert "已舉行" in civic_calendar.election_context(date(2026, 11, 29)).split("；")[1]


def test_impossible_dates_are_ignored():
    assert _findings("13月40日不是投票日。", user_text="13/40投票") == []


# ── 4. both reply exits ──────────────────────────────────────────────────────

def test_the_burst_exit_drops_the_whole_reply(monkeypatch):
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    _at_today(monkeypatch)
    outcome = {}
    assert main._enforce_new_value_reply(
        WRONG, source_text=USER, request_text=USER, context=[], addressed=False, outcome=outcome,
    ) == ""
    assert outcome["date_claims_dropped"] is True
    assert main._enforce_new_value_reply(
        RIGHT, source_text=USER, request_text=USER, context=[], addressed=False,
    ) == RIGHT
    # research evidence stating the denial backs it; nothing backs a past
    # date called upcoming
    denial = "報名不是12月5日截止。"
    evidence = "主辦單位公告：12月5日不是截止日，報名延長至12月12日"
    asked = "@咪寶 報名12/5截止嗎"
    assert main._enforce_new_value_reply(
        denial, source_text=asked, request_text=asked, context=[], addressed=True,
        evidence_text=evidence,
    ) == denial
    # asked directly: the honest line instead of silence (review 2 I5)
    assert main._enforce_new_value_reply(
        WRONG, source_text=USER, request_text=USER, context=[], addressed=True,
        evidence_text=evidence,
    ) == main.UNVERIFIED_DATE_REPLY
    # a fact-cache replay stored as searched backs the denial too
    assert main._enforce_new_value_reply(
        denial, source_text=asked, request_text=asked, context=[], addressed=False,
        trusted_grounded=True,
    ) == denial


def test_the_generated_reply_exit_drops_and_marks(monkeypatch):
    _at_today(monkeypatch)
    reply_provenance.reset()
    assert main._guard_generated_reply(WRONG, USER) == ""
    assert reply_provenance.dropped()
    reply_provenance.reset()
    assert main._guard_generated_reply(RIGHT, USER) == RIGHT
    assert not reply_provenance.dropped()


def test_the_guard_uses_taipei_today(monkeypatch):
    seen = {}

    def spy(reply, *, today, **kw):
        seen["today"] = today
        return []

    monkeypatch.setattr(main.reply_policy, "date_claim_findings", spy)
    assert not main._date_claims_unbacked("下一次大選在2028年。")
    from zoneinfo import ZoneInfo
    assert seen["today"] == datetime.now(ZoneInfo("Asia/Taipei")).date()


def test_a_broken_check_lets_the_reply_through(monkeypatch):
    def boom(*a, **kw):
        raise ValueError("x")

    monkeypatch.setattr(main.reply_policy, "date_claim_findings", boom)
    assert not main._date_claims_unbacked(WRONG)


# ── 5. linear time (H5, 2026-10-05) ──────────────────────────────────────────

_SHAPES = {
    "past_upcoming_runs": lambda n: "下一次預計在2025年1月11日舉行，" * (n // 22),
    "denials": lambda n: "11月28日並非投票日，" * (n // 11),
    "dates_no_punctuation": lambda n: "2025年1月11日" * (n // 11),
    "unclosed_quotes": lambda n: "「11月28日不是投票日" * (n // 11),
    "election_disputes": lambda n: "大選已近的說法不符現況，" * (n // 12),
    "denials_in_one_clause": lambda n: "11月28日並非投票日" * (n // 10),
    # round 6: many verdicts in one clause, with and without a closing question
    "verdicts_in_one_clause": lambda n: "大選已近2026年11月28日" + "不符現況" * (n // 4),
    "verdicts_then_question": lambda n: "大選已近2026年11月28日" + "不符現況" * (n // 4) + "？",
    "ranges": lambda n: "將於2026年10月1日至12月31日實施" * (n // 22),
    "breaks_only": lambda n: "，" * n + "下一次在2025年",
    # round 10
    "foreign_then_spaces": lambda n: "美國" + " " * n + "大選",
    "hearsay_in_one_clause": lambda n: "網傳11月28日不是投票日" * (n // 12),
    "rumours_told_after_the_date": lambda n: "11月28日不是投票日的傳聞" * (n // 13),
    "presidential_dates_in_one_clause": lambda n: "總統大選在2025年1月11日" * (n // 16),
    # round 11: hearsay in every clause, the rest of the sentence read once
    "hearsay_before_every_date": lambda n: "有人說11月28日、" * (n // 9) + "不是投票日",
    # round 12: one long clause after many dates; hearsay in every clause
    "dates_then_one_long_clause": lambda n: "有人說11月28日、" * (n // 18) + "，" + "這" * (n // 2),
    "hearsay_clauses": lambda n: "有人說11月28日不是投票日，" * (n // 15),
}


def _seconds(text):
    import time

    started = time.perf_counter()
    _findings(text, user_text=USER)
    return time.perf_counter() - started


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_long_replies_are_checked_in_linear_time(shape):
    make = _SHAPES[shape]
    small = min(_seconds(make(5000)) for _ in range(3))
    large = min(_seconds(make(20000)) for _ in range(3))
    assert large < 1.0
    assert large < 10 * max(small, 0.002)


def test_a_stale_cached_burst_reply_is_regenerated(monkeypatch):
    """A cached reply whose 「預計」 date has passed is answered afresh (review 3 N4)."""
    import inspect

    source = inspect.getsource(main._handle_burst_flush)
    assert "cached reply has a date claim it cannot stand behind; regenerating" in source
    _at_today(monkeypatch)
    assert main._date_claims_unbacked("影城預計於2025年第二季正式營運。", user_text="")


# review 4 I1: the backing texts are read once, not once per date
_BACKING_SHAPES = {
    "evidence": lambda n: {"evidence_text": "1/1投票 " * (n // 6)},
    "material": lambda n: {"material_text": "原訂1月1日投票延期 " * (n // 11)},
    "segments": lambda n: {"supported_segments": ["11月28日不是投票日" * (n // 10)] * 3},
}


@pytest.mark.parametrize("shape", sorted(_BACKING_SHAPES))
def test_long_backing_texts_are_read_in_linear_time(shape):
    import time

    reply = "11月28日不是投票日。" * 300

    def run(n):
        started = time.perf_counter()
        _findings(reply, user_text=USER, **_BACKING_SHAPES[shape](n))
        return time.perf_counter() - started

    small = min(run(5000) for _ in range(3))
    large = min(run(20000) for _ in range(3))
    assert large < 1.0
    assert large < 10 * max(small, 0.002)


# ── 6. post-implementation review (round 6) ──────────────────────────────────

@pytest.mark.parametrize("reply, user, material, segments, evidence, expected", [
    # backing must state the same denial — day and kind
    ("11月28日不是投票日。", "11/28投票", "", ["11月28日不是補班日，是投票日。"], "", ["denied_date"]),
    ("11月28日不是投票日。", "11/28投票", "", [], "11月28日不是補班日，是投票日。", ["denied_date"]),
    ("11月28日不是投票日。", "11/28要去投票", "", [], "中選會：11月28日投票，當天不是國定假日", ["denied_date"]),
    ("11月28日不是投票日。", "11/28要去投票", "", [], "美國期中選舉11月3日投票", ["denied_date"]),
    # a change of kind keeps the day; a change of day stays a denial
    ("11月28日不是投票日，是11月29日投票。", "11/28投票", "", [], "", ["denied_date"]),
    ("11月28日不是總統大選，當天是九合一地方選舉。", "台灣大選11/28", "", [], "", []),
    # a page moves the old day, not the new one
    ("報名不是11月15日截止。", "", "報名原訂11月1日截止，現延長至11月15日。", [], "", ["denied_date"]),
    # Taiwan's listed day backs nothing about another country
    ("美國投票日是11月28日，而非11月3日。", "美國11/3期中選舉投票", "", [], "", ["denied_date"]),
    # 「改選」 is no change of date; a question tacked on asserts the date still
    ("下一次總統大選預計在2025年1月11日舉行而2026年11月28日改選市長。", "", "", [], "",
     ["past_upcoming", "presidential_date"]),
    ("台灣下一次的總統大選預計在2025年1月11日舉行，對吧？", "", "", [], "",
     ["past_upcoming", "presidential_date"]),
    # election verdicts: what it is about, quoted claims, matching segments
    ("今年選舉把民調當開票結果的說法不正確。", "", "", [], "", []),
    ("「台灣的大選已經接近」的說法不符現況。", "", "", [], "", ["election_timing"]),
    ("台灣大選已近不是事實。", "", "", ["美國選舉已結束不是事實。"], "", ["election_timing"]),
    # a presidential election on a day none was held, however phrased
    ("台灣的總統及立法委員選舉已於2025年1月11日舉行完畢，因此您的說法不符現況。", "", "", [], "",
     ["presidential_date"]),
])
def test_round_six_cases(reply, user, material, segments, evidence, expected):
    assert sorted(_findings(
        reply, user_text=user, material_text=material,
        supported_segments=segments, evidence_text=evidence,
    )) == sorted(expected)


@pytest.mark.parametrize("reply", [
    "上一次總統大選是2024年1月13日。",
    "2024年總統大選投票率很高。",
    "美國總統大選在2024年11月5日。",
    "下一次總統大選在2028年。",
    "2016年1月16日的總統大選是第一次政黨輪替後的第二次。",
])
def test_real_presidential_dates_pass(reply):
    assert _findings(reply) == []


def test_a_stale_calendar_backs_nothing():
    # review 6: once expired, nothing in it backs a verdict either way
    after = civic_calendar.VALID_UNTIL + __import__("datetime").timedelta(days=1)
    assert _findings("下一次總統大選在2028年的說法有誤。", today=after) == ["election_timing"]
    assert civic_calendar.verified_election_dates(after) == set()



@pytest.mark.parametrize("reply, user, material, evidence, expected", [
    # correct replies that must still go out (round 6)
    ("11月28日不用帶投票通知單，帶身分證和印章就可以投票。", "11/28投票", "", "", []),
    ("11月28日不用請假也能投票。", "11/28投票", "", "", []),
    ("11月28日投票你沒記錯。", "11/28投票", "", "", []),
    ("11月28日投票沒有搞錯。", "11/28投票", "", "", []),
    ("收到，報名不是11月20日截止了，延長到11月30日。", "原本11/20報名截止，延長到11/30了", "", "", []),
    ("今年九合一選舉延期的說法有誤，投票日仍是11月28日。", "", "", "", []),
    ("網傳下次選舉改用電子投票的說法不是事實。", "", "", "", []),
    ("國旅補助將於2026年10月1日至12月31日實施。", "", "", "", []),
    ("活動將於2026年10月1日到11月30日舉行。", "", "", "", []),
    # and misses that are caught now
    ("11月3日不是投票日，台灣是11月28日投票。", "美國11/3期中選舉投票", "", "", ["denied_date"]),
    ("11月28日不是台灣的投票日，是美國的選舉日。", "11/28投票", "", "", ["denied_date"]),
    ("投票日是11月28日的說法有誤。", "11/28投票", "", "", ["denied_date"]),
    ("九合一選舉在11月28日的說法不正確，應該是11月29日。", "11/28投票", "", "", ["denied_date"]),
    ("11月28日不是投票日。", "美國和台灣大選都近了", "", "", ["denied_date"]),  # listed day, unstated
    ("11月28日不是投票日。", "11/28投票", "", "11月28日不是國定假日", ["denied_date"]),
    ("中選會在2024年1月13日宣布當選人，下一次總統大選預計在2025年1月11日舉行。", "", "", "",
     ["past_upcoming", "presidential_date"]),
    ("台灣總統大選已於2025年1月11日舉行完畢所以台灣大選已近的說法有誤。", "", "", "",
     ["election_timing", "presidential_date"]),
    ("下一次總統大選在2028年的說法有誤。", "", "", "", ["election_timing"]),
])
def test_round_six_correctness_cases(reply, user, material, evidence, expected):
    assert sorted(_findings(
        reply, user_text=user, material_text=material, evidence_text=evidence,
    )) == sorted(expected)


def test_media_the_guard_cannot_read_backs_a_denial(monkeypatch):
    """A reply reading a shared notice image keeps its denial (round 6); a past
    date called upcoming is checked all the same."""
    _at_today(monkeypatch)
    image = object()  # stands for an image part
    reply_provenance.reset()
    assert main._guard_generated_reply("11月28日沒有開放報名。", ["11/28開放報名嗎", image]) != ""
    reply_provenance.reset()
    assert main._guard_generated_reply("影城預計於2025年第二季正式營運。", ["看這張", image]) == ""


# ── 7. round-2 post-implementation review ────────────────────────────────────

@pytest.mark.parametrize("reply, user, segments, evidence, expected", [
    # the US votes for a president too: only the year is judged unless Taiwan is named
    ("川普是在2024年11月5日的總統大選勝選的。", "", (), "", []),
    ("期中選舉是在總統任期中間舉行的選舉，上次總統大選是2024年11月5日。", "", (), "", []),
    ("美國方面，總統大選在2024年11月5日舉行。", "", (), "", []),
    ("總統大選後的就職典禮在2024年5月20日舉行。", "", (), "", []),
    ("下下次總統大選在2032年。", "", (), "", []),
    ("台灣的大選已於2025年1月11日舉行完畢。", "", (), "", ["presidential_date"]),
    ("2025年1月11日舉辦了總統大選。", "", (), "", ["presidential_date"]),
    ("上一次總統大選，是2025年1月11日。", "", (), "", ["presidential_date"]),
    # clarifications of which vote, across sentences and punctuation
    ("11月28日不是總統大選。當天是九合一地方選舉。", "", (), "", []),
    ("11月28日不是總統大選；當天是九合一地方選舉。", "", (), "", []),
    ("11月28日沒有總統選舉，總統大選要到2028年。", "", (), "", []),
    ("11月3日不是總統大選，是美國期中選舉。", "美國11/3選舉", (), "", []),
    ("11月28日不是投票日，是開票日。", "11/28投票", (), "", ["denied_date"]),
    # the listed day named as the answer, unless the talk is of another country
    ("11月29日不是投票日喔，投票日是11月28日（六）。", "11/29要去投票", (), "", []),
    ("投票日是11月28日，而非11月3日。", "美國11/3期中選舉投票", (), "", ["denied_date"]),
    # election verdicts read the sentence, the clause pointed back at, the quote
    ("「大選已經結束」的說法不正確，九合一選舉的投票日是2026年11月28日。", "", (), "", []),
    ("網傳九合一投票日即將延後的說法有誤，仍是11月28日投票。", "", (), "", []),
    ("台灣大選已近，這不是事實。", "", (), "", ["election_timing"]),
    ("「台灣的大選，已經接近」的說法不符現況。", "", (), "", ["election_timing"]),
    ("台灣大選已近不是事實。", "", ("美國大選已近不是事實。",), "", ["election_timing"]),
    ("台灣大選已近不是事實。", "", ("台灣大選已近不是事實。",), "", []),
    # rumours told as rumours
    ("網傳11月28日不是投票日，這是謠言。", "11/28投票", (), "", []),
    ("11月28日不是投票日。", "11/28投票", (), "網傳11月28日不是投票日，中選會澄清這是謠言。", ["denied_date"]),
    # more ways to write the denial
    ("11月28日在台灣並不是投票日。", "投票日11月28日", (), "", ["denied_date"]),
    ("11月28日其實不是投票日。", "投票日11月28日", (), "", ["denied_date"]),
    ("11月28日、並不是投票日。", "11/28投票", (), "", ["denied_date"]),
    ("11/28 (六) 不是投票日。", "11/28投票", (), "", ["denied_date"]),
    # running periods, delays of something else, changes the user announced
    ("國旅補助將於2026年10月1日起實施至12月31日。", "", (), "", []),
    ("補助將於2026年10月1日（四）至12月31日（四）實施。", "", (), "", []),
    ("國旅補助將於2026年9月至12月實施。", "", (), "", []),
    ("下一次九合一選舉預計在2022年11月26日舉行，颱風假則延到2026年11月30日。", "", (), "", ["past_upcoming"]),
    ("報名不是11月20日截止，已延到11月30日。", "11/20截止的報名延到11/30了", (), "", []),
    ("報名不是11月20日截止，已延到11月30日。", "原本是11/20截止，現在延長到11/30", (), "", []),
    ("報名不是11月20日截止，已延到11月30日。", "11/20截止", (),
     "報名截止日延長至11月30日，原11月20日截止。", []),
])
def test_round_seven_cases(reply, user, segments, evidence, expected):
    assert sorted(_findings(
        reply, user_text=user, supported_segments=segments, evidence_text=evidence,
    )) == sorted(expected)


def test_media_does_not_unmake_the_listed_vote_day(monkeypatch):
    _at_today(monkeypatch)
    reply_provenance.reset()
    assert main._guard_generated_reply(
        "2026年11月28日並非台灣的投票日。", ["投票日11月28日", object()],
    ) == ""


def test_a_page_starting_something_on_a_day_did_not_move_it():
    assert _findings(
        "11月15日沒有營業。", user_text="11/15營業", material_text="11月15日起延長營業時間",
    ) == ["denied_date"]


# ── 8. post-deployment review of 9b3e372 (hotfix) ────────────────────────────

@pytest.mark.parametrize("reply, user, expected", [
    # voting logistics about the listed day must go out
    ("11月28日沒有投票通知單也可以投票喔，帶身分證和印章就好。", "", []),
    ("11月28日沒有不在籍投票，記得提早買車票返鄉。", "", []),
    ("11月28日不在國內就不能投票喔", "我11/28要去日本玩還能投票嗎", []),
    ("11月28日不是用健保卡投票，要帶身分證喔。", "", []),
    ("11月28日沒有投票權，要滿20歲才能投。", "", []),
    ("投票通知單不是11月28日當天才發", "", []),
    ("11月28日投票時蓋錯了也不能換票", "", []),
    ("11月28日其實不用投票通知單", "", []),
    # naming the presidential election to deny it
    ("今年不是總統大選，是2026年11月28日的九合一地方選舉；下一次總統大選在2028年。", "", []),
    ("這次投的不是總統大選，是2026年11月28日的縣市長和議員選舉。", "", []),
    # incident phrasings
    ("台灣的總統及立法委員選舉已經於2025年1月11日舉行完畢。", "", ["presidential_date"]),
    ("上一次總統大選是在2025年1月11日。", "", ["presidential_date"]),
    ("總統大選早在2025年1月11日就舉行了。", "", ["presidential_date"]),
    ("需要澄清的是，11月28日並不是投票日。", "", ["denied_date"]),
    ("11月28日並不是投票日，網路上說要投票是不實訊息。", "", ["denied_date"]),
    ("聽說11月28日不是投票日，已經延到12月了。", "", ["denied_date"]),
    # other countries' elections, and Taiwan's own names
    ("南韓在2025年6月3日舉行總統大選，李在明當選。", "", []),
    ("巴西總統大選在2026年10月4日舉行", "", []),
    ("德國大選在2025年2月23日舉行。", "", []),
    ("川普贏了總統大選，在2025年1月20日就職。", "", []),
    ("五都大選在2010年11月27日舉行。", "", []),
    ("11月29日不是投票日，投票日是11月28日，韓國瑜也會回高雄投票。", "11/29投票對吧", []),
    ("台灣的總統大選在2024年1月14日，韓國瑜參選。", "", ["presidential_date"]),
    ("11月29日不是投票日喔，投票日是11月28日（六）。", "我們11/29要去日本，11/29可以投票嗎", []),
    # a date beside the election that is not the vote
    ("總統大選於2023年11月24日截止登記", "", []),
])
def test_round_eight_cases(reply, user, expected):
    assert sorted(_findings(reply, user_text=user)) == sorted(expected)


@pytest.mark.parametrize("reply, user, expected", [
    # real denials with words between the negation and the vote (review of fdcdb42)
    ("2026年11月28日並非台灣的九合一投票日。", "", ["denied_date"]),
    ("11月28日不是投開票日。", "", ["denied_date"]),
    ("11月28日並非是投票日。", "", ["denied_date"]),
    ("11月28日不是什麼投票日。", "", ["denied_date"]),
    ("11月28日那天沒有要投票。", "", ["denied_date"]),
    ("投票日應該不是11月28日。", "", ["denied_date"]),
    ("11月28日投票的資訊有誤。", "", ["denied_date"]),
    ("12月5日不是補助的截止日。", "國旅補助12/5截止對吧", ["denied_date"]),
    # another country in an earlier clause does not hide Taiwan's wrong date
    ("美國期中選舉在11月3日，但台灣的總統及立委選舉已於2025年1月11日舉行完畢。", "美國和台灣大選都近了",
     ["presidential_date"]),
    ("台灣的總統大選已於2025年1月11日舉行並公布結果。", "", ["presidential_date"]),
    ("台灣總統大選剛在2025年1月11日舉行完畢。", "", ["presidential_date"]),
    ("台灣的總統大選日期是2025年1月11日。", "", ["presidential_date"]),
    # what the denial negates is the kind: an opening, not the vote
    ("11月28日沒有開放不在籍投票，要回戶籍地投票喔。", "", []),
    ("11月28日並未開放不在籍投票", "", []),
    ("11月28日不是公投投票日。", "", []),
    ("11月28日不是一般的選舉，一次要投好幾張票喔。", "", []),
    # rumours refuted in other words; travelling is not another country's vote
    ("網傳11月28日不是投票日，這不是真的，投票日就是11月28日。", "", []),
    ("網路上說11月28日不是投票日，那是錯的。", "", []),
    ("11月29日不是投票日喔，投票日是11月28日（六），人在日本沒辦法投票。", "我們11/29要去日本玩還能投票嗎", []),
    ("美國的期中選舉不是在11月28日，而是11月3日。", "", []),
])
def test_round_nine_cases(reply, user, expected):
    assert sorted(_findings(reply, user_text=user)) == sorted(expected)


@pytest.mark.parametrize("reply, user, expected", [
    # each date is judged by its own election, even with another country in the clause
    ("美國總統大選在2024年11月5日、台灣總統大選在2025年1月11日都已舉行完畢。", "美國和台灣大選已近",
     ["presidential_date"]),
    # results come out that night; a notice is another day
    ("台灣總統大選於2025年1月11日公布結果，賴清德當選。", "", ["presidential_date"]),
    ("台灣總統大選於2024年1月13日公布結果，賴清德當選。", "", []),
    ("總統大選於2023年11月14日公告候選人號次。", "", []),
    # 選舉活動／期間, 投票時間／時程 are the schedule
    ("11月28日並沒有選舉活動。", "投票日11月28日", ["denied_date"]),
    ("11月28日並沒有選舉活動，也不用投票。", "投票日11月28日", ["denied_date"]),
    ("11月28日並非台灣的選舉期間。", "投票日11月28日", ["denied_date"]),
    ("11月28日並非投票時間。", "投票日11月28日", ["denied_date"]),
    ("2026年11月28日並非台灣的投票時程。", "投票日11月28日", ["denied_date"]),
    ("11月28日並沒有也不會舉行投票。", "投票日11月28日", ["denied_date"]),
    ("11月28日沒有也不用投票。", "投票日11月28日", ["denied_date"]),
    # a rumour told and refuted, also in the next sentence
    ("網傳11月28日不是投票日。這是謠言，請大家照常投票。", "投票日11月28日", []),
    ("網傳11月28日不是投票日，請大家不要相信，投票日就是11月28日。", "投票日11月28日", []),
    ("有人說11月28日不是投票日，這是假消息，11月28日確實要投票。", "投票日11月28日", []),
    ("謠言說11月28日不是投票日，這不正確。", "投票日11月28日", []),
    ("關於11月28日不是投票日的傳聞，這是不實訊息。", "投票日11月28日", []),
    ("有長輩群組在傳11月28日不是投票日，這是假訊息喔。", "聽說11/28不用投票了？", []),
    ("這則訊息說11月28日不是投票日，是不實訊息。", "聽說11/28不用投票了？", []),
    # ...but not a claim that it is the day, called a rumour, or a rumour confirmed
    ("11月28日是投票日的說法是錯的。", "投票日11月28日", ["denied_date"]),
    ("網傳11月28日不是投票日，這不是謠言，已經延期了。", "投票日11月28日", ["denied_date"]),
])
def test_round_ten_cases(reply, user, expected):
    assert sorted(_findings(reply, user_text=user)) == sorted(expected)


@pytest.mark.parametrize("reply, user, expected", [
    # the next sentence refutes the rumour only when it opens with that
    ("有人說11月28日不是投票日，已經延期了。另外要你提供身分證號碼的訊息不要相信。", "聽說11/28不用投票了？",
     ["denied_date"]),
    ("有人說11月28日不用投票了，因為選舉延期。網路上說要帶健保卡投票是假的。", "聽說11/28不用投票了？",
     ["denied_date"]),
    ("網傳11月28日不是投票日。這是真的，說照常投票的才是假消息。", "聽說11/28不用投票了？", ["denied_date"]),
    # refutations in other words
    ("很多人轉傳說11月28日不是投票日，這是錯誤訊息，投票日還是11月28日。", "聽說11/28不用投票了？", []),
    ("網傳11月28日不是投票日，其實不是這樣，11月28日照常投票。", "聽說11/28不用投票了？", []),
    ("網傳11月28日不是投票日，這是假新聞，請照常投票。", "聽說11/28不用投票了？", []),
    ("網傳11月28日不是投票日，沒這回事，請照常去投票。", "聽說11/28不用投票了？", []),
    ("關於11月28日不用投票的說法，是錯誤的。", "聽說11/28不用投票了？", ["denied_date"]),
    # ...and negated ones refute nothing
    ("網傳11月28日不是投票日，這不是什麼謠言，中選會已經宣布延期。", "聽說11/28不用投票了？", ["denied_date"]),
    ("網傳11月28日不是投票日，這不算是謠言，已經改期了。", "聽說11/28不用投票了？", ["denied_date"]),
    ("網傳11月28日不是投票日，這是沒錯的，投票延到12月了。", "聽說11/28不用投票了？", ["denied_date"]),
    # 不必 still needs the schedule right after it; a change after 沒有／並未 can
    # hide a move (「並未取消投票，只是改到12月5日舉行」), so it is dropped as before
    ("11月28日並沒有也不必特別請假去投票，因為是星期六。", "投票日11月28日", []),
    ("11月28日並沒有延長投票時間，下午4點準時截止。", "投票日11月28日", ["denied_date"]),
    ("11月28日並沒有也不會停止投票，颱風天也一樣。", "投票日11月28日", ["denied_date"]),
    ("11月28日並不在選舉期間內，投票已經延到12月5日。", "投票日11月28日", ["denied_date"]),
    ("日本 的眾議院選舉不是11月28日。", "投票日11月28日", []),
])
def test_round_eleven_cases(reply, user, expected):
    assert sorted(_findings(reply, user_text=user)) == sorted(expected)


@pytest.mark.parametrize("reply, user, expected", [
    # a change of the vote stands only after 沒有-type negations
    ("11月28日不是延期後的投票日，真正的投票日是12月5日。", "投票日11月28日", ["denied_date"]),
    ("11月28日不是變更後的投票日，請以12月5日為準。", "投票日11月28日", ["denied_date"]),
    ("11月28日不是取消投票，而是延到12月5日。", "投票日11月28日", ["denied_date"]),
    ("11月28日並沒有提前投票，投票日已經改到11月29日。", "投票日11月28日", ["denied_date"]),
    ("11月28日沒有也不必去投票了，已經延期。", "投票日11月28日", ["denied_date"]),
    ("香港議會11月28日沒有選舉，台灣這天也不用投票。", "投票日11月28日", ["denied_date"]),
    # a refutation of something else, or one that introduces a new claim
    ("有人說11月28日不是投票日，原本公告的日期是錯誤的，已改到12月5日。", "聽說11/28不用投票了？",
     ["denied_date"]),
    ("有人說11月28日不是投票日，以前的說法是錯誤的，現在改到12月5日。", "聽說11/28不用投票了？",
     ["denied_date"]),
    ("有人說11月28日不用投票了。澄清一下，投票已經延到12月5日。", "聽說11/28不用投票了？", ["denied_date"]),
    ("有人說11月28日不是投票日。不實的說法很多，但這次是真的延期了。", "聽說11/28不用投票了？",
     ["denied_date"]),
    # refutations told in other ways
    ("網傳11月28日不是投票日，這是沒根據的謠言，請照常投票。", "聽說11/28不用投票了？", []),
    ("網傳11月28日不是投票日，這不是新的謠言了，請照常投票。", "聽說11/28不用投票了？", []),
    ("網傳11月28日不是投票日，這是沒查證的假消息。", "聽說11/28不用投票了？", []),
    ("網傳11月28日不是投票日。中選會已經出面澄清，投票日不變。", "聽說11/28不用投票了？", []),
    ("網傳11月28日不是投票日。根據中選會公告，這是不實訊息。", "聽說11/28不用投票了？", []),
    ("網傳11月28日不是投票日。請大家放心，這是假消息。", "聽說11/28不用投票了？", []),
    ("網傳11月28日不是投票日，沒想到這種謠言也有人信，請照常投票。", "聽說11/28不用投票了？", []),
])
def test_round_twelve_cases(reply, user, expected):
    assert sorted(_findings(reply, user_text=user)) == sorted(expected)


@pytest.mark.parametrize("reply, user, expected", [
    # a change after 沒有／並未 can hide a move of the day
    ("11月28日並未取消投票，只是改到12月5日舉行。", "投票日11月28日", ["denied_date"]),
    ("11月28日沒有延期投票，投票日是12月5日。", "投票日11月28日", ["denied_date"]),
    ("11月28日並沒有延期投票，而是直接取消了。", "投票日11月28日", ["denied_date"]),
    ("11月28日沒有停止投票，是整個選舉都取消了。", "投票日11月28日", ["denied_date"]),
    # the same gap as before: a reason in between is not a denial
    ("11月28日並沒有因為颱風而延後投票，請照常前往。", "投票日11月28日", []),
    # refuted later in the sentence, or by the authority in the next one
    ("網傳11月28日不是投票日，中選會表示投票日不變，請大家不要被不實訊息誤導。", "聽說11/28不用投票了？", []),
    ("網傳11月28日不是投票日，我查了中選會的公告，投票日沒有改變，這則訊息是假的。", "聽說11/28不用投票了？",
     []),
    ("網傳11月28日不是投票日，但中選會已經澄清了。", "聽說11/28不用投票了？", []),
    ("網傳11月28日不是投票日，不過官方已經出面否認。", "聽說11/28不用投票了？", []),
    ("網傳11月28日不是投票日。中選會今天上午已經出面澄清，投票照常。", "聽說11/28不用投票了？", []),
    ("網傳11月28日不是投票日，查證後確認是假消息，請照常投票。", "聽說11/28不用投票了？", []),
    ("網傳11月28日不是投票日，中選會沒有否認，已經延期了。", "聽說11/28不用投票了？", ["denied_date"]),
])
def test_round_thirteen_cases(reply, user, expected):
    assert sorted(_findings(reply, user_text=user)) == sorted(expected)
