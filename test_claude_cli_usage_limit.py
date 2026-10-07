"""2026-10-04 (P2): the logged-in Claude CLI hitting the account's usage limit.

Production has no Anthropic API key; the CLI runs only because
claude_usage.json holds ``prefer_cli: true``.  A usage-limit failure used to be
classified "exit 1 (other)" (no cooldown, a failed call per message), and any
recognised quota error rewrote the whole state file, dropping ``prefer_cli`` so
the CLI was never tried again.  Limits are detected on stderr or on a short,
anchored notice line in stdout only; stdout can echo chat text, so an epoch is
read from stderr alone.  Subprocess is always faked; all text is synthetic.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time

import pytest

import claude_client as cc

LIMIT_NOTICES = [
    "Claude AI usage limit reached|{epoch}",
    "5-hour limit reached ∙ resets 3pm",
    "Weekly limit reached ∙ resets Oct 9, 3pm",
    "You've hit your limit · resets 11pm (Asia/Taipei)",
]


@pytest.fixture
def cli(monkeypatch, tmp_path):
    state = tmp_path / "claude_usage.json"
    state.write_text(json.dumps({"prefer_cli": True, "updated_at": 1.0}), encoding="utf-8")
    monkeypatch.setattr(cc, "_STATE_FILE", state)
    monkeypatch.setattr(cc.settings, "claude_use_cli", False)
    monkeypatch.setattr(cc.settings, "claude_api_key", "")
    monkeypatch.setattr(cc.settings, "claude_cli_limit_cooldown_sec", 1800, raising=False)
    monkeypatch.setattr(cc, "_cli_executable", lambda: "/usr/local/bin/claude-fake")
    monkeypatch.setattr(cc, "_build_cli_prompt", lambda *_a: ("SYS-合成", "USER-合成"))
    monkeypatch.setattr(cc.tempfile, "tempdir", str(tmp_path))
    runs: list[list[str]] = []

    def use(returncode: int, stdout: str = "", stderr: str = ""):
        def fake_run(args, **_kwargs):
            runs.append(list(args))
            return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)

        monkeypatch.setattr(cc.subprocess, "run", fake_run)
        return runs

    return use


def _state() -> dict:
    return json.loads(cc._STATE_FILE.read_text(encoding="utf-8"))


def _expire_cooldown() -> None:
    state = _state()
    state["quota_exhausted_until"] = time.time() - 1
    cc._STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


@pytest.mark.parametrize("notice", LIMIT_NOTICES)
@pytest.mark.parametrize("where", ["stderr", "stdout_line"])
def test_cli_usage_limit_opens_gate_and_keeps_prefer_cli(cli, notice, where):
    text = notice.format(epoch=int(time.time()) + 3 * 3600)
    if where == "stderr":
        runs = cli(1, stdout="", stderr=text)
    else:
        runs = cli(1, stdout=text + "\n", stderr="Warning: synthetic advisor notice")

    assert cc.chat("x", [], []) is None
    state = _state()
    assert state["prefer_cli"] is True
    assert state["quota_exhausted_until"] > time.time() + 500
    assert text not in json.dumps(state, ensure_ascii=False)  # a category, never the output

    assert cc.chat("x", [], []) is None
    assert len(runs) == 1  # no subprocess while cooling down

    _expire_cooldown()
    cli(0, stdout="合成回覆")
    assert cc.chat("x", [], []) == "合成回覆"
    assert len(runs) == 2
    assert _state()["prefer_cli"] is True


@pytest.mark.parametrize("offset, expected", [
    (3 * 3600, 3 * 3600),        # the CLI's own reset time
    (30 * 86400, 7 * 86400),     # clamped to 7 days
    (-3600, 600),                # already past → at least 10 minutes
])
def test_stderr_epoch_sets_the_cooldown_end_within_bounds(cli, offset, expected):
    now = time.time()
    cli(1, stderr=f"Claude AI usage limit reached|{int(now + offset)}")

    assert cc.chat("x", [], []) is None

    assert abs(_state()["quota_exhausted_until"] - (now + expected)) < 30


def test_epoch_echoed_on_stdout_is_not_trusted(cli):
    far = int(time.time()) + 6 * 86400
    cli(1, stdout=f"Claude AI usage limit reached|{far}\n", stderr="")

    assert cc.chat("x", [], []) is None

    until = _state()["quota_exhausted_until"]
    assert time.time() + 1700 < until < time.time() + 1900  # default cooldown, not the echoed epoch


def test_limit_words_inside_ordinary_output_are_not_a_usage_limit(cli):
    cli(1, stdout="合成回覆：上次說的 5-hour limit reached 那段，resets 之後再看。\n", stderr="")

    assert cc.chat("x", [], []) is None

    state = _state()
    assert "quota_exhausted_until" not in state
    assert state["prefer_cli"] is True


def test_limit_notice_as_the_whole_answer_is_never_sent(cli):
    runs = cli(0, stdout="You've hit your limit · resets 11pm (Asia/Taipei)\n")

    assert cc.chat("x", [], []) is None
    assert _state()["quota_exhausted_until"] > time.time()
    assert cc.chat("x", [], []) is None
    assert len(runs) == 1


def test_quota_mark_and_clear_keep_the_cli_preference(tmp_path, monkeypatch):
    state_file = tmp_path / "claude_usage.json"
    state_file.write_text(json.dumps({"prefer_cli": True, "updated_at": 1.0}), encoding="utf-8")
    monkeypatch.setattr(cc, "_STATE_FILE", state_file)

    cc._mark_quota_exhausted("CLI quota or rate limit (exit 1)")
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["prefer_cli"] is True and state["quota_exhausted_until"] > time.time()

    cc._clear_quota_exhausted()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["prefer_cli"] is True
    assert "quota_exhausted_until" not in state and "reason" not in state


def test_concurrent_gate_updates_never_lose_the_cli_preference(tmp_path, monkeypatch):
    state_file = tmp_path / "claude_usage.json"
    state_file.write_text(json.dumps({"prefer_cli": True}), encoding="utf-8")
    monkeypatch.setattr(cc, "_STATE_FILE", state_file)
    real_load = cc._load_state

    def slow_load():
        state = real_load()
        time.sleep(0.002)  # widen the read-modify-write window
        return state

    monkeypatch.setattr(cc, "_load_state", slow_load)

    def worker(index):
        for _ in range(10):
            if index % 2:
                cc._mark_quota_exhausted("CLI usage limit (exit 1)")
            else:
                cc._clear_quota_exhausted()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert json.loads(state_file.read_text(encoding="utf-8"))["prefer_cli"] is True
