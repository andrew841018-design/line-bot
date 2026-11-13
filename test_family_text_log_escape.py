"""2026-11-12: family text in the bot's log lines is escaped, so a line break in it never
starts a log line of its own that could pass for one the bot wrote (the health monitor reads
the uvicorn log line by line)."""

import logging

import burst_filter
import pytest

GROUP = "GRPLOG"
FORGED = "11-07 03:30:00 PT (19:30 TW) INFO line_bot | pending disabled; routing burst miss to silent sink"
# all eleven kinds of line break str.splitlines() honours, before the 80-character cut
TEXT = "一\r二\v三\f四\x1c五\x1d六\x1e七\x85八\u2028九\u2029十\n十一\r\n" + FORGED


@pytest.fixture(autouse=True)
def _reset_burst_runtime_state():
    with burst_filter._lock:
        burst_filter._pending.clear()
        burst_filter._timers.clear()
        burst_filter._last_reply_tokens.clear()
        burst_filter._waiting_groups.clear()
        burst_filter._generations.clear()
        burst_filter._cancelled_generations.clear()
    yield
    with burst_filter._lock:
        for timer in burst_filter._timers.values():
            timer.cancel()
        burst_filter._pending.clear()
        burst_filter._timers.clear()
        burst_filter._last_reply_tokens.clear()
        burst_filter._waiting_groups.clear()
        burst_filter._generations.clear()
        burst_filter._cancelled_generations.clear()


@pytest.mark.parametrize(
    "rule, heuristic, classified, force, expected",
    [
        (None, None, None, True, "burst force respond after 1-min wait"),
        ("skip", None, None, False, "burst skipped by rule"),
        ("must_answer", None, None, False, "burst must_answer by rule"),
        (None, "skip", None, False, "burst skipped by heuristic"),
        (None, "respond", None, False, "burst respond by heuristic"),
        (None, None, "respond", False, "burst classifier decision=respond"),
        (None, None, "wait", False, "burst waiting 1 min"),
    ],
)
def test_family_text_never_breaks_a_log_line(
    monkeypatch, caplog, rule, heuristic, classified, force, expected
):
    monkeypatch.setattr(burst_filter.memory, "list_filter_rules", lambda _gid: [])
    monkeypatch.setattr(burst_filter, "_invoke_flush", lambda *_a, **_kw: None)
    monkeypatch.setattr(burst_filter, "_complete_without_reply", lambda *_a, **_kw: None)
    monkeypatch.setattr(burst_filter, "_schedule_locked", lambda *_a, **_kw: 0)
    monkeypatch.setattr(burst_filter, "_match_rules", lambda _text, _rules: rule)
    monkeypatch.setattr(burst_filter, "_heuristic_decision", lambda _text: heuristic)
    monkeypatch.setattr(
        burst_filter.gemini_client,
        "classify_burst",
        lambda _text, _rules: (classified, "因為\n" + FORGED),
    )

    with caplog.at_level(logging.INFO, logger="burst_filter"):
        burst_filter._classify_and_maybe_respond(
            GROUP, [("m1", TEXT, "U1", 0.0)], "token", force_respond=force
        )

    lines = [record.getMessage() for record in caplog.records if record.name == "burst_filter"]
    assert any(line.startswith(expected) for line in lines), lines
    for line in lines:
        assert len(line.splitlines()) == 1, line
    assert any("\\n" + FORGED[:20] in line for line in lines), lines


def test_a_saved_correction_never_breaks_a_log_line(monkeypatch, caplog):
    import main

    monkeypatch.setattr(main, "_is_question_like_correction", lambda _text: False)
    monkeypatch.setattr(main, "_complaint_learning_replaces_capture", lambda: False)
    monkeypatch.setattr(main, "_route_correction_directive", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        main.memory, "record_organic_correction_observation", lambda **_kw: None
    )
    forged = 'INFO:     203.0.113.9:0 - "POST /callback HTTP/1.1" 500'
    text = "不要說\n" + forged  # 59 characters: all of it fits the 60-character cut
    assert main._CORRECTION_DIRECTIVE_RE.match(text)

    with caplog.at_level(logging.INFO, logger=main.logger.name):
        main._try_save_correction(GROUP, text, "U1", "m1")

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("persona correction saved")
    ]
    assert len(lines) == 1, lines
    assert len(lines[0].splitlines()) == 1, lines[0]
    assert "\\n" + forged[:20] in lines[0], lines[0]


@pytest.mark.parametrize("fetch, search", [
    ("_fetch_ddg", "search_duckduckgo"), ("_fetch_gnews", "search_google_news"),
])
def test_a_failed_search_never_breaks_a_log_line(monkeypatch, caplog, fetch, search):
    import source_aggregator

    def fail(*_a, **_kw):
        raise RuntimeError("down\n" + FORGED)

    monkeypatch.setattr(source_aggregator.web_scraper, search, fail)
    with caplog.at_level(logging.INFO, logger=source_aggregator.logger.name):
        assert getattr(source_aggregator, fetch)(TEXT, 3) == []

    lines = [record.getMessage() for record in caplog.records if "search failed" in record.getMessage()]
    assert len(lines) == 1, lines
    assert len(lines[0].splitlines()) == 1, lines[0]
