"""2026-10-07 latency observability: production logs carry the timings needed
to see where a reply's seconds go.  Logging only — every test here also checks
that the code path behaves exactly as before (same calls, same results).

Timing asserts are lower bounds only (a synthetic wait or a back-dated stamp),
never upper bounds on how fast the machine is.  All texts are synthetic and no
log line may carry message or reply text.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from linebot.v3.webhooks import MessageEvent, TextMessageContent

import burst_filter
import gemini_client
import main

_PT = ZoneInfo("America/Los_Angeles")
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s | %(message)s"
# 2023-11-14 22:13:20.042 UTC = 14:13:20 PST = 11-15 06:13 Taipei
_FIXED_TS = 1700000000.042


def _lines(caplog, prefix: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(prefix)
    ]


def _record(created: float, msecs: float, level=logging.INFO, msg="synthetic"):
    record = logging.LogRecord("line_bot", level, __file__, 1, msg, None, None)
    record.created = created
    record.msecs = msecs
    return record


# ── 1. log timestamps ────────────────────────────────────────────────────────

def test_pt_time_carries_milliseconds_tw_stays_minutes():
    fmt = main._DualTZFormatter(_LOG_FORMAT)

    assert fmt.formatTime(_record(_FIXED_TS, 42.0)) == "11-14 14:13:20.042 PT (06:13 TW)"
    assert fmt.formatTime(_record(_FIXED_TS, 999.9)) == "11-14 14:13:20.999 PT (06:13 TW)"


def test_live_record_milliseconds_come_from_record_msecs():
    fmt = main._DualTZFormatter(_LOG_FORMAT)
    record = logging.LogRecord("line_bot", logging.INFO, __file__, 1, "x", None, None)

    stamp = fmt.formatTime(record)

    assert re.fullmatch(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} PT \(\d{2}:\d{2} TW\)", stamp)
    assert stamp[15:18] == f"{int(record.msecs):03d}"


@pytest.mark.skipif(shutil.which("grep") is None, reason="grep not installed")
def test_health_check_429_grep_still_counts_today_only(tmp_path):
    """health_check.sh: grep -c "^$PT_TODAY .*429" "$UVICORN_LOG"."""
    fmt = main._DualTZFormatter(_LOG_FORMAT)
    log = tmp_path / "uvicorn.log"
    log.write_text(
        "\n".join(
            [
                fmt.format(_record(_FIXED_TS, 42.0, logging.WARNING, "gemini 429 RESOURCE_EXHAUSTED")),
                fmt.format(_record(_FIXED_TS, 42.0, logging.INFO, "reply accepted msgs=1")),
                fmt.format(_record(_FIXED_TS - 86400, 42.0, logging.WARNING, "gemini 429 yesterday")),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    pt_today = datetime.fromtimestamp(_FIXED_TS, tz=_PT).strftime("%m-%d")

    result = subprocess.run(
        ["grep", "-c", f"^{pt_today} .*429", str(log)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.stdout.strip() == "1"


def test_daily_briefing_still_tells_error_lines_from_info_lines():
    import daily_briefing_discord as dbd

    fmt = main._DualTZFormatter(_LOG_FORMAT)
    error_line = fmt.format(_record(_FIXED_TS, 42.0, logging.ERROR, "synthetic failure"))
    info_line = fmt.format(_record(_FIXED_TS, 42.0, logging.INFO, "synthetic progress"))

    assert " ERROR " in error_line
    assert dbd._is_real_error(error_line) is True
    assert dbd._is_real_error(info_line) is False


# ── 2. LINE accepted the reply ───────────────────────────────────────────────

def _back_date(token: str, seconds: float) -> None:
    group_id, message_ids, seen_at = main._inbound_reply_by_token[token]
    main._inbound_reply_by_token[token] = (group_id, message_ids, seen_at - seconds)


_ACCEPTED_RE = re.compile(
    r"reply accepted group=(\S+) msgs=(\d+) handler_to_accept_s=(\d+\.\d\d)"
)


def test_batch_reply_accepted_logs_count_and_seconds_since_binding(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    marked: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(
        main.memory,
        "mark_inbound_events_replied",
        lambda group_id, message_ids: marked.append((group_id, message_ids))
        or len(message_ids),
    )
    main._register_inbound_reply_batch("T_LAT", "G_LAT", ["M1", "M2", "M1", ""])
    _back_date("T_LAT", 2.5)

    main._mark_inbound_reply_succeeded("T_LAT")

    (line,) = _lines(caplog, "reply accepted")
    match = _ACCEPTED_RE.fullmatch(line)
    assert match and match.group(1) == "G_LAT" and match.group(2) == "2"
    assert float(match.group(3)) >= 2.5
    # bookkeeping unchanged
    assert marked == [("G_LAT", ["M1", "M2"])]
    assert "T_LAT" not in main._inbound_reply_by_token


def test_acceptance_is_logged_even_when_the_durable_mark_fails(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(
        main.memory,
        "mark_inbound_event_replied",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("sqlite unavailable")),
    )
    main._register_inbound_reply_token("T_FAIL", "G_FAIL", "M_FAIL")

    main._mark_inbound_reply_succeeded("T_FAIL")

    (line,) = _lines(caplog, "reply accepted")
    assert _ACCEPTED_RE.fullmatch(line).group(2) == "1"
    # unchanged: LINE acceptance stays authoritative, the mapping is kept
    assert "T_FAIL" in main._inbound_reply_by_token


def test_unknown_token_logs_nothing_and_marks_nothing(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    mark = MagicMock()
    monkeypatch.setattr(main.memory, "mark_inbound_event_replied", mark)

    main._mark_inbound_reply_succeeded("T_UNKNOWN")
    main._mark_inbound_reply_succeeded(None)

    assert _lines(caplog, "reply accepted") == []
    mark.assert_not_called()


def _line_api(monkeypatch):
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_get_quota_footer", lambda: "")
    monkeypatch.setattr(main, "_prepare_outbound_text", lambda text, **_kwargs: text)
    monkeypatch.setattr(main.memory, "mark_inbound_event_replied", lambda *_a: None)
    api = MagicMock()
    api.reply_message.return_value = SimpleNamespace(sent_messages=[])
    monkeypatch.setattr(main, "MessagingApi", lambda _client: api)
    api_client = MagicMock()
    api_client.__enter__.return_value = object()
    monkeypatch.setattr(main, "ApiClient", lambda _config: api_client)
    return api


def test_real_reply_path_logs_once_per_accepted_reply_only(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    api = _line_api(monkeypatch)
    main._register_inbound_reply_token("T_OK", "G_REPLY", "M_OK")
    main._register_inbound_reply_token("T_BAD", "G_REPLY", "M_BAD")

    sent = main._reply(
        "T_OK", "合成回覆內容", group_id="G_REPLY",
        allow_push_fallback=False, include_auxiliary=False,
    )
    api.reply_message.side_effect = RuntimeError("ambiguous transport failure")
    failed = main._reply(
        "T_BAD", "合成回覆內容", group_id="G_REPLY",
        allow_push_fallback=False, include_auxiliary=False,
    )

    assert (sent, failed) == (True, False)
    (line,) = _lines(caplog, "reply accepted")
    assert _ACCEPTED_RE.fullmatch(line).group(1) == "G_REPLY"
    assert "合成回覆內容" not in line


# ── 3. explicit path: the main LLM call ──────────────────────────────────────

_EXPLICIT_REPLY = "合成回覆：週末去郊外走走，記得帶外套，山上風大。"


def _explicit_event() -> MessageEvent:
    evt = MagicMock(spec=MessageEvent)
    evt.message = TextMessageContent(id="MSG_LAT", text="咪寶 週末去哪走走", quoteToken="qt")
    evt.source = SimpleNamespace(type="group", group_id="G_EXP", user_id="U_LAT")
    evt.reply_token = "TOKEN_LAT"
    return evt


def _run_explicit(llm):
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._build_quoted_block", return_value=""),
        patch("main._requires_public_research", return_value=False),
        patch("main._get_explicit_market_quote_reply", return_value=None),
        patch("main._prefetch_urls", side_effect=lambda t: t),
        patch("main._llm_chat", side_effect=llm),
        patch("main._gemini_llm_chat", return_value=""),
        patch("main._mark_quota_exhausted"),
        patch("main._enforce_new_value_reply", side_effect=lambda text, **_k: text),
        patch("main.memory.append_turn"),
        patch("main._append_bot_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._try_save_correction"),
        patch("main._maybe_capture_calendar_event"),
        patch("main._reply") as mock_reply,
    ):
        main._handle_explicit_text(_explicit_event(), "G_EXP", "週末去哪走走")
    return mock_reply


def test_explicit_llm_done_logs_seconds_and_length_without_text(caplog):
    caplog.set_level(logging.INFO)

    def slow_llm(*_a, **_k):
        time.sleep(0.03)
        return _EXPLICIT_REPLY

    mock_reply = _run_explicit(slow_llm)

    (line,) = _lines(caplog, "explicit llm done")
    match = re.fullmatch(r"explicit llm done secs=(\d+\.\d\d) len=(\d+)", line)
    assert match and float(match.group(1)) >= 0.02
    assert int(match.group(2)) == len(_EXPLICIT_REPLY)
    assert _EXPLICIT_REPLY not in caplog.text
    # unchanged: the same reply still goes out once
    assert mock_reply.call_count == 1
    assert mock_reply.call_args.args[1] == _EXPLICIT_REPLY


def test_failed_main_llm_call_logs_no_done_line(caplog):
    caplog.set_level(logging.INFO)

    def quota(*_a, **_k):
        raise RuntimeError("429 RESOURCE_EXHAUSTED PerDay free_tier_requests")

    mock_reply = _run_explicit(quota)

    assert _lines(caplog, "explicit llm done") == []
    # unchanged degraded fallback
    assert mock_reply.call_args.args[1] == main._visible_llm_degraded_reply()


# ── 4. Gemini generation ─────────────────────────────────────────────────────

_GEMINI_REPLY = "合成資料顯示這個週末北部有鋒面，山區午後容易下雨。"


def _resp(text: str, finish: str = "STOP"):
    return SimpleNamespace(
        text=text,
        candidates=[
            SimpleNamespace(
                finish_reason=SimpleNamespace(name=finish),
                content=SimpleNamespace(parts=[]),
                grounding_metadata=None,
            )
        ],
        usage_metadata=None,
    )


def _script_gemini(monkeypatch, outcomes: list, delay: float = 0.0) -> list[int]:
    sent: list[int] = []

    def send_message(_message):
        sent.append(1)
        if delay:
            time.sleep(delay)
        step = outcomes.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    session = SimpleNamespace(send_message=send_message)
    monkeypatch.setattr(
        gemini_client,
        "_client",
        SimpleNamespace(chats=SimpleNamespace(create=lambda **_k: session)),
    )
    monkeypatch.setattr(gemini_client, "_track_usage", lambda *_a: None)
    return sent


def _gemini_run():
    return gemini_client._run(
        "gemini-test", user_input="這週末天氣如何", context=[], facts=[],
        persona_notes=None, recall_hits=None, case_hits=None, group_id=None,
    )


_DONE_RE = re.compile(
    r"gemini chat done model=(\S+) secs=(\d+\.\d\d) attempts=(\d+) len=(\d+)"
)


def test_gemini_success_logs_model_seconds_attempts(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    sent = _script_gemini(monkeypatch, [_resp(_GEMINI_REPLY)], delay=0.03)

    out = _gemini_run()

    assert out == _GEMINI_REPLY
    assert len(sent) == 1
    (line,) = _lines(caplog, "gemini chat done")
    match = _DONE_RE.fullmatch(line)
    assert match and match.group(1) == "gemini-test"
    assert float(match.group(2)) >= 0.02
    assert (match.group(3), int(match.group(4))) == ("1", len(out))
    assert _GEMINI_REPLY not in line


@pytest.mark.parametrize(
    "first",
    [_resp("", finish="MAX_TOKENS"), RuntimeError("503 UNAVAILABLE synthetic")],
    ids=["empty-text", "transient-error"],
)
def test_gemini_retried_generation_counts_attempts(monkeypatch, caplog, first):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(gemini_client.time, "sleep", lambda _s: None)
    sent = _script_gemini(monkeypatch, [first, _resp(_GEMINI_REPLY)])

    assert _gemini_run() == _GEMINI_REPLY
    assert len(sent) == 2
    (line,) = _lines(caplog, "gemini chat done")
    assert _DONE_RE.fullmatch(line).group(3) == "2"


def test_gemini_choosing_not_to_reply_is_a_timed_empty_generation(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    sent = _script_gemini(monkeypatch, [_resp("")])

    assert _gemini_run() == ""
    assert len(sent) == 1
    (line,) = _lines(caplog, "gemini chat done")
    match = _DONE_RE.fullmatch(line)
    assert (match.group(3), match.group(4)) == ("1", "0")


def test_gemini_failure_logs_no_done_line(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _script_gemini(monkeypatch, [RuntimeError("400 INVALID_ARGUMENT synthetic")])

    with pytest.raises(RuntimeError, match="INVALID_ARGUMENT"):
        _gemini_run()

    assert _lines(caplog, "gemini chat done") == []


# ── 5. burst flush handoff ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_burst_state():
    def clear():
        with burst_filter._lock:
            for timer in burst_filter._timers.values():
                timer.cancel()
            for state in (
                burst_filter._pending,
                burst_filter._timers,
                burst_filter._last_reply_tokens,
                burst_filter._generations,
                burst_filter._cancelled_generations,
                burst_filter._retry_attempts,
            ):
                state.clear()
            burst_filter._waiting_groups.clear()
            burst_filter._claimed_generations.clear()

    clear()
    yield
    clear()


_HANDOFF_RE = re.compile(
    r"burst flush handoff group=(\S+) n_msgs=(\d+) "
    r"first_age_s=(\d+\.\d\d) last_age_s=(\d+\.\d\d)"
)


def test_handoff_logs_batch_size_and_oldest_newest_wait(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    delivered: list[tuple] = []
    monkeypatch.setattr(burst_filter, "_on_flush", lambda *args: delivered.append(args))
    now = time.time()
    pending = [
        ("M1", "合成甲", "U1", now - 12.0),
        ("M2", "合成乙", "U2", now - 7.0),
        ("M3", "合成丙", "U1", now - 3.0),
    ]

    burst_filter._invoke_flush("G_BURST", "合成甲\n合成乙\n合成丙", "T_BURST", pending)

    # unchanged handoff
    assert delivered == [("G_BURST", "合成甲\n合成乙\n合成丙", "T_BURST", ["M1", "M2", "M3"])]
    (line,) = _lines(caplog, "burst flush handoff")
    match = _HANDOFF_RE.fullmatch(line)
    assert match and match.group(1) == "G_BURST" and match.group(2) == "3"
    first_age, last_age = float(match.group(3)), float(match.group(4))
    assert first_age >= 12.0 and last_age >= 3.0 and first_age - last_age >= 8.9
    assert "合成" not in line


def test_cancelled_batch_logs_no_handoff(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    on_flush = MagicMock()
    monkeypatch.setattr(burst_filter, "_on_flush", on_flush)
    monkeypatch.setattr(burst_filter, "_complete_without_reply", lambda *_a: None)
    monkeypatch.setattr(burst_filter, "_remember_cancelled", lambda *_a: None)
    with burst_filter._lock:
        burst_filter._cancelled_generations["G_STALE"] = 5

    burst_filter._invoke_flush(
        "G_STALE", "合成", "T_STALE", [("M1", "合成", "U1", time.time())], generation=5
    )

    on_flush.assert_not_called()
    assert _lines(caplog, "burst flush handoff") == []


def test_timer_flush_path_logs_one_handoff(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    delivered: list[tuple] = []
    monkeypatch.setattr(burst_filter, "_on_flush", lambda *args: delivered.append(args))
    monkeypatch.setattr(burst_filter.memory, "list_filter_rules", lambda _gid: [])
    now = time.time()
    with burst_filter._lock:
        burst_filter._pending["G_TIMER"] = [
            ("M1", "合成第一段", "U1", now - 9.0),
            ("M2", "合成第二段", "U2", now - 8.0),
        ]
        burst_filter._last_reply_tokens["G_TIMER"] = "T_TIMER"
        burst_filter._generations["G_TIMER"] = 1

    burst_filter._flush_burst("G_TIMER", generation=1)

    assert [args[3] for args in delivered] == [["M1", "M2"]]
    (line,) = _lines(caplog, "burst flush handoff")
    match = _HANDOFF_RE.fullmatch(line)
    assert match.group(2) == "2"
    assert float(match.group(3)) >= 9.0 and float(match.group(4)) >= 8.0


def test_pending_ages_of_an_empty_batch_are_zero():
    assert burst_filter._pending_ages([]) == (0.0, 0.0)
