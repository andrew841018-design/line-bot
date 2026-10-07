"""2026-09-28 maintenance: chat replies must not claim reminder changes.

A chat reply to a quoted reminder claimed the reminder "會更新到" a new date,
while the reminder itself stayed on its old date.  Chat models cannot change
reminders or the calendar, so such sentences are dropped (2026-09-11
不能誤稱成功).  All fixtures are synthetic; no real chat content.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import reply_policy  # noqa: E402

INCIDENT_REPLY = "好的，那聚餐提醒會更新到 11 月 5 日週三，地點在社區活動中心 5 樓。"
INCIDENT_SOURCE = "11月5日週三可到社區活動中心五樓"


@pytest.mark.parametrize("reply", [
    INCIDENT_REPLY,
    "我已幫你把行程改到週三下午三點。",
    "提醒已經設定好了，到時候會通知大家。",
    "已新增提醒：11 月 5 日下午兩點聚餐。",
    "咪寶會幫大家把聚餐行程延後一週。",
    "好的，已更新為 11 月 5 日下午兩點。",
    "收到，已取消明天的提醒。",
    "你會收到更新後的提醒。",
])
def test_unbacked_claims_are_dropped(reply):
    assert reply_policy.strip_operation_claims(reply) == ("", 1)


@pytest.mark.parametrize("reply", [
    "建議把提醒改到前一天，比較來得及準備證件。",
    "記得更新行事曆，才不會和其他安排撞期。",
    "可以回覆原提醒，寫清楚新的日期時間。",
    "要不要把行程改到週三？",
    "如果行程會改，記得先通知大家。",
    "行程可能會延後，出門前再確認一次。",
    "出門前記得提醒家人帶證件。",
    "你可以把行程改到週三下午。",
    "我建議你把提醒改到早上九點。",
    "公車末班車通常在晚上十一點左右。",
])
def test_advice_questions_and_facts_are_kept(reply):
    assert reply_policy.strip_operation_claims(reply) == (reply, 0)


@pytest.mark.parametrize("reply", [
    "提醒不會自動更新，要改請回覆原提醒。",
    "行程不會取消。",
    "這樣不會新增重複的提醒。",
    "我不會幫你改行程。",
    "提醒還沒更新。",
    "提醒尚未更新到 11 月 5 日。",
    "行程沒有取消。",
])
def test_negations_are_kept(reply):
    # 2026-09-11: saying a reminder was NOT changed is the honest answer.
    assert reply_policy.strip_operation_claims(reply) == (reply, 0)


def test_only_the_claim_sentence_is_dropped():
    reply = "報到要提早十分鐘。提醒已經幫你改到 11 月 5 日了。記得帶證件。"
    assert reply_policy.strip_operation_claims(reply) == (
        "報到要提早十分鐘。記得帶證件。", 1
    )


def test_copied_confirmation_fields_go_with_the_claim():
    reply = "已新增提醒\n時間：2026-11-05 14:00\n事項：聚餐"  # privacy-safe-fixture
    assert reply_policy.strip_operation_claims(reply)[0] == ""


def test_field_lines_without_a_claim_are_kept():
    reply = "那天的安排如下。\n時間：下午兩點到四點\n地點：社區活動中心"
    assert reply_policy.strip_operation_claims(reply) == (reply, 0)


def test_incident_reply_is_not_sent_through_new_value_policy(monkeypatch):
    import main

    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    assert main._enforce_new_value_reply(
        INCIDENT_REPLY, source_text=INCIDENT_SOURCE, request_text=INCIDENT_SOURCE,
        context=[], addressed=False,
    ) == ""


def test_claim_is_dropped_even_for_summary_requests(monkeypatch):
    import main

    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    request = "幫我摘要一下剛剛的行程"
    out = main._enforce_new_value_reply(
        "摘要：週三下午兩點聚餐。行程已經幫你改到週三了。",
        source_text=request, request_text=request, context=[],
    )
    assert "改到" not in out
    assert "週三下午兩點聚餐" in out


def test_restatement_failure_does_not_restore_the_claim(monkeypatch):
    import main

    monkeypatch.setattr(reply_policy, "strip_restatement",
                        MagicMock(side_effect=RuntimeError("boom")))
    out = main._enforce_new_value_reply(
        "出門前記得帶證件。行程已經幫你改到週三了。",
        source_text="週三可以", request_text="週三可以", context=[],
    )
    assert out == "出門前記得帶證件。"


def test_burst_reply_with_only_a_claim_is_not_sent(monkeypatch):
    import main

    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.check_fact_cache", return_value=None),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._requires_public_research", return_value=False),
        patch("main._prefetch_urls", side_effect=lambda t: t),
        patch("main._llm_chat", return_value=INCIDENT_REPLY),
        patch("main._finish_burst_without_reply") as finish_silent,
        patch("main._reply") as mock_reply,
    ):
        main._handle_burst_flush("GRP001", INCIDENT_SOURCE, "TOKEN901")
    mock_reply.assert_not_called()
    assert finish_silent.call_count == 1


def test_explicit_reply_keeps_advice_but_drops_the_claim(monkeypatch):
    from linebot.v3.webhooks import MessageEvent, TextMessageContent

    import main

    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    evt = MagicMock(spec=MessageEvent)
    evt.message = TextMessageContent(id="MSG902", text="咪寶 週三下午可以去聚餐", quoteToken="qt")
    evt.source = SimpleNamespace(type="group", group_id="GRP001", user_id="U_TEST")
    evt.reply_token = "TOKEN902"
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._build_quoted_block", return_value=""),
        patch("main._requires_public_research", return_value=False),
        patch("main._get_explicit_market_quote_reply", return_value=None),
        patch("main._prefetch_urls", side_effect=lambda t: t),
        patch("main._llm_chat",
              return_value="提醒已經幫你改到週三了。報到要提早十分鐘，記得帶證件。"),
        patch("main.memory.append_turn"),
        patch("main._append_bot_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._try_save_correction"),
        patch("main._maybe_capture_calendar_event"),
        patch("main._reply") as mock_reply,
    ):
        main._handle_explicit_text(evt, "GRP001", "週三下午可以去聚餐")
    assert mock_reply.call_count == 1
    assert mock_reply.call_args.args[1] == "報到要提早十分鐘，記得帶證件。"


# 2026-10-03 maintenance: two burst replies after the 9/28 guard still claimed a
# reminder was moved ("已改成…" with the reminder only implied) while it stayed
# on its original time.  Synthetic fixtures mirror their shape.
@pytest.mark.parametrize("reply", [
    "已改成 11/5 早上 9:00，社區活動中心集合。",
    "提醒時間是 11/5 早上 9:00 集合，不是中午喔，剛剛已經改成早上囉。",
    "好，已經幫你改成下午兩點。",
    "已經把時間改成 11/5 晚上七點。",
    "咪寶已改到週三下午。",
])
def test_implied_reminder_changes_are_dropped(reply):
    assert reply_policy.strip_operation_claims(reply) == ("", 1)


@pytest.mark.parametrize("reply", [
    "會議已改成週四下午。",
    "他已經改成早上九點的班機了。",
    "這篇報導提到的價格是去年的，今年已經改成含運價。",
    "已經改成新制，申請流程變簡單了。",
    "已改成早上嗎？",
    "如果已經改成早上，記得早點出門。",
    "尚未改成早上九點。",
    "提醒時間是 11/5 早上 9:00。",
])
def test_third_party_changes_questions_and_plain_times_are_kept(reply):
    assert reply_policy.strip_operation_claims(reply) == (reply, 0)


def test_reminder_time_sentence_goes_with_an_implied_claim():
    reply = "提醒時間是 11/5 早上 9:00。剛剛已經改成早上囉。"
    assert reply_policy.strip_operation_claims(reply)[0] == ""


# 2026-10-05: every match re-scanned its whole clause (delimiter look-ups, a
# slice and the conditional check), so one long clause with many matches was
# quadratic: 「提醒已更新」×4000＋「如果」 held the GIL for 3.3 s.  The scan now
# reads at most the 5000 characters LINE can send (the rest is left out, as
# the send path leaves it out) and finds clause bounds once per sentence.
@pytest.mark.parametrize("text, expected", [
    # cut at 5000, the 如果 that made every match conditional is not sent
    pytest.param("提醒已更新" * 4000 + "如果", ("", 1), id="op-conditional"),
    pytest.param("提醒已更新" * 4000 + "嗎", ("", 1), id="op-question"),
    pytest.param("我會提醒你" * 4000 + "如果", ("", 1), id="promise-conditional"),
    pytest.param("建議" + "提醒已更新" * 4000, None, id="op-advice"),
])
def test_claim_scan_takes_under_300_ms_on_a_20k_degenerate_reply(text, expected):
    start = time.perf_counter()
    result = reply_policy.strip_operation_claims(text)
    assert time.perf_counter() - start < 0.3
    assert result == ((text[:5000], 0) if expected is None else expected)


@pytest.mark.parametrize("text", [
    pytest.param("提醒已更新" * 999 + "如果", id="op-conditional"),
    pytest.param("我會提醒你" * 999 + "如果", id="promise-conditional"),
    pytest.param("幫你更新提醒" * 832 + "如果", id="help-verb-conditional"),
    pytest.param("建議" + "提醒已更新" * 999, id="op-advice"),
    pytest.param("x" * 2500 + "可能" + "提醒已更新" * 499, id="late-hedge"),
])
def test_claim_scan_is_fast_on_a_long_clause_with_many_matches(text):
    assert len(text) <= 5000
    start = time.perf_counter()
    assert reply_policy.strip_operation_claims(text) == (text, 0)
    assert time.perf_counter() - start < 0.05


def test_text_past_what_line_can_send_is_left_out():
    head = "今天天氣很好，記得帶傘。" * 450
    assert len(head) > 5000
    assert reply_policy.strip_operation_claims(head) == (head[:5000], 0)
    # a claim that would only start past the cut is not sent at all
    text = "記得帶傘。" * 1000 + "提醒已經設定好了。"
    assert reply_policy.strip_operation_claims(text) == (text[:5000], 0)
    # a claim inside what is sent is still dropped
    text = "提醒已經設定好了。" + "記得帶傘。" * 1200
    kept, dropped = reply_policy.strip_operation_claims(text)
    assert dropped == 1 and kept == text[len("提醒已經設定好了。"):5000]
