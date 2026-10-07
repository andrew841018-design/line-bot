"""2026-10-07: replies felt slow on the logged-in Claude CLI route.

The CLI child now starts without Claude Code's update check and background
traffic, every CLI call logs its seconds and outcome (never its text), and
after two timeouts in a row the CLI rests for ten minutes instead of making
every message wait the full CLAUDE_CLI_TIMEOUT_SEC before Gemini.  The quota
gate, the API route and exit-1 / usage-limit handling are unchanged.

Subprocess and claude_client's clock are always faked; all text is synthetic.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time

import pytest

import claude_client as cc

LEAK = "合成私人片段"
OK = (0, "合成回覆", "")
EMPTY = (0, "", "")
FAILED = (1, "", f"fatal: {LEAK}")
TIMEOUT = "timeout"  # the fake CLI hangs until subprocess.run gives up
LAUNCH_FAILS = "launch-fails"  # the executable cannot be started
CLI_SECS = 1.25  # how long every fake CLI run that returns takes
TIMING_RE = re.compile(r"claude cli secs=(\d+\.\d\d) outcome=(\w+)")


class _Clock:
    """claude_client's time module: wall clock and monotonic, moved by hand."""

    def __init__(self) -> None:
        self.wall = time.time()
        self.mono = 1000.0

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(cc, "time", fake)
    return fake


@pytest.fixture
def cli(monkeypatch, tmp_path, clock):
    """Production's setup (no API key, ``prefer_cli`` saved) and a fake CLI
    that answers each run with the next scripted result."""
    state = tmp_path / "claude_usage.json"
    state.write_text(json.dumps({"prefer_cli": True}), encoding="utf-8")
    monkeypatch.setattr(cc, "_STATE_FILE", state)
    monkeypatch.setattr(cc.settings, "claude_use_cli", False)
    monkeypatch.setattr(cc.settings, "claude_api_key", "")
    monkeypatch.setattr(cc.settings, "claude_cli_timeout_sec", 60)
    monkeypatch.setattr(cc, "_cli_executable", lambda: "/usr/local/bin/claude-fake")
    monkeypatch.setattr(cc, "_build_cli_prompt", lambda *_a: ("SYS-合成", f"USER-{LEAK}"))
    monkeypatch.setattr(cc.tempfile, "tempdir", str(tmp_path))
    runs: list[dict] = []

    def use(*results):
        script = list(results)

        def fake_run(args, **kwargs):
            if not script:
                pytest.fail("the CLI ran more often than scripted")
            result = script.pop(0)
            runs.append({"args": list(args), "env": dict(kwargs.get("env") or {})})
            if result == TIMEOUT:
                clock.mono += kwargs["timeout"]
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            if result == LAUNCH_FAILS:
                raise OSError("synthetic launch failure")
            clock.mono += CLI_SECS
            return subprocess.CompletedProcess(
                args, result[0], stdout=result[1], stderr=result[2]
            )

        monkeypatch.setattr(cc.subprocess, "run", fake_run)
        return runs

    return use


def _ask() -> str | None:
    return cc.chat("x", [], [])


def _timings(caplog) -> list[tuple[str, str]]:
    return [
        TIMING_RE.fullmatch(record.getMessage()).groups()
        for record in caplog.records
        if record.getMessage().startswith("claude cli secs=")
    ]


# ── 1. a lighter CLI start ─────────────────────────────────────────────────


def test_child_env_turns_off_update_check_and_background_traffic(cli, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", raising=False)
    monkeypatch.delenv("DISABLE_AUTOUPDATER", raising=False)
    runs = cli(OK)

    assert _ask() == "合成回覆"

    env = runs[0]["env"]
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert "PATH" in env and "HOME" in env
    # Only the child gets them; the bot's own environment is untouched.
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in os.environ
    assert "DISABLE_AUTOUPDATER" not in os.environ


def test_child_env_keeps_values_set_on_purpose(cli, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "0")
    monkeypatch.setenv("DISABLE_AUTOUPDATER", "0")
    runs = cli(OK)

    assert _ask() == "合成回覆"

    env = runs[0]["env"]
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "0"
    assert env["DISABLE_AUTOUPDATER"] == "0"


# ── 2. every CLI call is timed ─────────────────────────────────────────────


@pytest.mark.parametrize("result, outcome, secs", [
    (OK, "ok", "1.25"),
    (EMPTY, "empty", "1.25"),
    (TIMEOUT, "timeout", "60.00"),
    (FAILED, "error", "1.25"),
    (LAUNCH_FAILS, "error", "0.00"),
    ((1, "", "Claude AI usage limit reached"), "limit", "1.25"),
    ((1, "", "API Error: 429 Too Many Requests"), "limit", "1.25"),  # quota / rate limit
    ((0, "You've hit your limit · resets 11pm (Asia/Taipei)\n", ""), "limit", "1.25"),
])
def test_every_cli_call_logs_its_seconds_and_outcome(cli, caplog, result, outcome, secs):
    caplog.set_level(logging.INFO, logger="claude_client")
    cli(result)

    _ask()

    assert _timings(caplog) == [(secs, outcome)]
    assert LEAK not in caplog.text


def test_the_api_routes_cli_fallback_is_timed_too(cli, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="claude_client")
    cli(OK)
    cc._STATE_FILE.write_text("{}", encoding="utf-8")  # no prefer_cli yet
    monkeypatch.setattr(cc.settings, "claude_api_key", "test-key")
    monkeypatch.setattr(cc, "_build_payload", lambda *_a: {"messages": []})

    def no_credit(_payload):
        raise cc.ClaudeQuotaExhausted(
            "HTTP 400: Your credit balance is too low to access the Anthropic API"
        )

    monkeypatch.setattr(cc, "_request", no_credit)

    assert _ask() == "合成回覆"
    assert _timings(caplog) == [("1.25", "ok")]


# ── 3. the CLI rests after timeouts in a row ───────────────────────────────


def test_two_timeouts_in_a_row_rest_the_cli_for_ten_minutes(cli, clock, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="claude_client")
    runs = cli(TIMEOUT, TIMEOUT, OK)

    assert _ask() is None
    assert _ask() is None
    assert "Claude CLI timed out 2 times in a row" in caplog.text

    # Resting: no prompt, no CLI run, the caller goes on to Gemini at once.
    monkeypatch.setattr(cc, "_build_cli_prompt", lambda *_a: pytest.fail("no CLI while resting"))
    assert _ask() is None
    clock.wall += cc._CLI_TIMEOUT_BREAKER_COOLDOWN_SEC - 1
    assert _ask() is None
    assert len(runs) == 2
    assert "Claude CLI timeout breaker open" in caplog.text

    monkeypatch.setattr(cc, "_build_cli_prompt", lambda *_a: ("SYS-合成", "USER-合成"))
    clock.wall += 2
    assert _ask() == "合成回覆"  # ten minutes later the CLI is tried again
    assert len(runs) == 3
    assert LEAK not in caplog.text


def test_the_rest_leaves_the_quota_gate_and_state_file_alone(cli):
    cli(TIMEOUT, TIMEOUT)

    assert _ask() is None
    assert _ask() is None

    assert cc._cli_timeout_breaker_remaining() > 0
    assert cc.quota_exhausted() is False
    assert json.loads(cc._STATE_FILE.read_text(encoding="utf-8")) == {"prefer_cli": True}


def test_one_timeout_does_not_rest_the_cli(cli):
    runs = cli(TIMEOUT, OK)

    assert _ask() is None
    assert _ask() == "合成回覆"
    assert len(runs) == 2


def test_a_successful_call_ends_the_streak(cli):
    runs = cli(TIMEOUT, OK, TIMEOUT, EMPTY, TIMEOUT, OK)

    assert [_ask() for _ in range(6)] == [None, "合成回覆", None, "", None, "合成回覆"]
    assert len(runs) == 6


def test_an_error_between_timeouts_does_not_end_the_streak(cli):
    runs = cli(TIMEOUT, FAILED, TIMEOUT)

    assert [_ask() for _ in range(4)] == [None] * 4
    assert len(runs) == 3  # the fourth message did not run the CLI


def test_errors_and_limits_alone_never_rest_the_cli(cli):
    runs = cli(FAILED, LAUNCH_FAILS, FAILED, FAILED, OK)

    assert [_ask() for _ in range(5)] == [None] * 4 + ["合成回覆"]
    assert len(runs) == 5

    cli((1, "", "Claude AI usage limit reached"))
    assert _ask() is None
    assert cc.quota_exhausted() is True  # the usage limit keeps its own gate
    assert cc._cli_timeout_streak == 0
    assert cc._cli_timeout_breaker_remaining() == 0


def test_after_a_rest_one_more_timeout_rests_it_again(cli, clock):
    runs = cli(TIMEOUT, TIMEOUT, TIMEOUT, OK, TIMEOUT, OK)
    assert _ask() is None
    assert _ask() is None

    clock.wall += cc._CLI_TIMEOUT_BREAKER_COOLDOWN_SEC + 1
    assert _ask() is None  # the CLI is tried again and still hangs
    assert len(runs) == 3
    assert cc._cli_timeout_breaker_remaining() > 0  # no second wasted wait
    assert _ask() is None
    assert len(runs) == 3

    clock.wall += cc._CLI_TIMEOUT_BREAKER_COOLDOWN_SEC + 1
    assert _ask() == "合成回覆"
    assert _ask() is None  # a single timeout after a success ...
    assert _ask() == "合成回覆"  # ... does not rest it
    assert len(runs) == 6


def test_a_success_closes_an_open_breaker(cli):
    # e.g. a slow call that was already running when two others timed out
    cli(TIMEOUT, TIMEOUT, OK)
    assert _ask() is None
    assert _ask() is None
    assert cc._cli_timeout_breaker_remaining() > 0

    assert cc._chat_via_cli("x", [], [], None) == "合成回覆"

    assert cc._cli_timeout_breaker_remaining() == 0
    assert cc._cli_timeout_streak == 0


# ── 4. the API route is not part of it ─────────────────────────────────────


def _api_route(monkeypatch, tmp_path, request) -> None:
    monkeypatch.setattr(cc.settings, "claude_api_key", "test-key")
    monkeypatch.setattr(cc.settings, "claude_use_cli", False)
    monkeypatch.setattr(cc, "_STATE_FILE", tmp_path / "claude-state.json")
    monkeypatch.setattr(cc, "_build_payload", lambda *_a: {"messages": []})
    monkeypatch.setattr(cc, "_request", request)


def test_api_timeouts_never_rest_the_cli(monkeypatch, tmp_path):
    def timed_out(_payload):
        raise cc.ClaudeProviderError("TimeoutError")

    _api_route(monkeypatch, tmp_path, timed_out)

    assert [_ask() for _ in range(3)] == [None] * 3
    assert cc._cli_timeout_streak == 0
    assert cc._cli_timeout_breaker_remaining() == 0
    assert not (tmp_path / "claude-state.json").exists()


def test_a_resting_cli_leaves_the_api_route_alone(monkeypatch, tmp_path):
    _api_route(monkeypatch, tmp_path, lambda _payload: {
        "content": [{"type": "text", "text": "合成 API 回覆"}],
    })
    monkeypatch.setattr(cc, "_cli_timeout_breaker_until", time.time() + 600)

    assert _ask() == "合成 API 回覆"


def test_each_test_starts_with_the_cli_breaker_closed():
    # The tests above leave the CLI resting; conftest's per-test reset ends it.
    assert cc._cli_timeout_streak == 0
    assert cc._cli_timeout_breaker_until == 0.0
