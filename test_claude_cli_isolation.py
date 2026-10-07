"""2026-09-26: the bot's `claude -p` call must not load Andrew's own Claude Code
setup (default opus model, hooks, CLAUDE.md, MCP) — that made replies time
out after 60 s — and must keep family chat out of argv and logs.

All prompts and outputs are synthetic; subprocess is always faked.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time

import pytest

import claude_client as cc

SYSTEM = "SYS-合成系統提示"
USER = "USER-合成家庭對話"
LEAK = "合成私人片段"


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_STATE_FILE", tmp_path / "claude-state.json")
    monkeypatch.setattr(cc.settings, "claude_use_cli", True)
    monkeypatch.setattr(cc.settings, "claude_api_key", "")
    monkeypatch.setattr(cc, "_cli_executable", lambda: "/usr/local/bin/claude-fake")
    monkeypatch.setattr(cc, "_build_cli_prompt", lambda *_a: (SYSTEM, USER))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-synthetic-must-not-leak")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-gemini-key")
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "synthetic-line-token")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "synthetic-line-secret")
    monkeypatch.setattr(cc.tempfile, "tempdir", str(tmp_path))
    runs = []

    def use(result):
        def fake_run(args, **kwargs):
            # input= together with stdin= makes subprocess.run raise ValueError.
            assert "stdin" not in kwargs
            path = args[args.index("--system-prompt-file") + 1]
            runs.append({
                "args": list(args),
                "input": kwargs.get("input"),
                "env": kwargs.get("env") or {},
                "path": path,
                "mode": stat.S_IMODE(os.stat(path).st_mode),
                "system": open(path, encoding="utf-8").read(),
            })
            if isinstance(result, BaseException):
                raise result
            return subprocess.CompletedProcess(args, result[0], stdout=result[1], stderr=result[2])

        monkeypatch.setattr(cc.subprocess, "run", fake_run)
        return runs

    return use


def test_cli_is_isolated_fast_and_keeps_chat_out_of_argv(cli):
    runs = cli((0, "合成回覆", ""))
    assert cc._chat_via_cli("x", [], [], None) == "合成回覆"
    run = runs[0]
    args = run["args"]
    for flag in ("-p", "--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in args
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--setting-sources") + 1] == ""
    assert args[args.index("--model") + 1] == cc.settings.claude_cli_model
    assert not any(USER in a or SYSTEM in a for a in args)
    assert run["input"] == USER
    assert run["system"] == SYSTEM and run["mode"] == 0o600
    for secret in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET"):
        assert secret not in run["env"]
    assert "PATH" in run["env"] and "HOME" in run["env"]
    assert not os.path.exists(run["path"])


@pytest.mark.parametrize("result", [
    subprocess.TimeoutExpired(["claude"], 60),
    OSError("synthetic launch failure"),
    (1, "", f"fatal: {LEAK}"),
])
def test_prompt_file_is_removed_and_errors_carry_no_chat(cli, result):
    runs = cli(result)
    with pytest.raises(cc.ClaudeProviderError) as exc:
        cc._chat_via_cli("x", [], [], None)
    assert LEAK not in str(exc.value)
    assert runs and not os.path.exists(runs[0]["path"])


def test_quota_is_detected_without_storing_cli_output(cli, tmp_path):
    cli((1, "", f"rate limit reached · {LEAK}"))
    assert cc.chat("x", [], []) is None  # caller falls back to Gemini
    state = json.loads((tmp_path / "claude-state.json").read_text(encoding="utf-8"))
    assert state.get("quota_exhausted_until") and LEAK not in json.dumps(state, ensure_ascii=False)


@pytest.mark.parametrize("result", [
    subprocess.TimeoutExpired(["claude"], 60),
    OSError("synthetic launch failure"),
    (1, "", f"fatal: {LEAK}"),
])
def test_cli_failures_fall_back_to_gemini(cli, result, caplog):
    cli(result)
    assert cc.chat("x", [], []) is None
    assert LEAK not in caplog.text


def test_empty_cli_answer_is_a_decision_and_remembers_the_cli(cli, tmp_path):
    cli((0, "", ""))
    assert cc.chat("x", [], []) == ""
    state = json.loads((tmp_path / "claude-state.json").read_text(encoding="utf-8"))
    assert state.get("prefer_cli") is True


def test_prompt_file_creation_failure_falls_back(cli, monkeypatch):
    cli((0, "不該執行", ""))

    def no_space(*_a, **_k):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(cc.tempfile, "mkstemp", no_space)
    monkeypatch.setattr(cc.subprocess, "run", lambda *_a, **_k: pytest.fail("no CLI without its prompt"))
    assert cc.chat("x", [], []) is None


def test_prompt_file_removal_failure_is_logged_not_fatal(cli, monkeypatch, caplog):
    runs = cli((0, "合成回覆", ""))

    def stuck(_path):
        raise PermissionError("synthetic")

    # os.unlink is global; keep the failure inside this block so pytest's own
    # cleanup can still delete files afterwards.
    with monkeypatch.context() as patch:
        patch.setattr(cc.os, "unlink", stuck)
        assert cc._chat_via_cli("x", [], [], None) == "合成回覆"
    assert "claude prompt file not removed" in caplog.text
    os.unlink(runs[0]["path"])  # the file the failed removal left behind


def test_stale_prompt_files_are_cleaned_up(cli, tmp_path):
    stale = tmp_path / f"{cc._PROMPT_FILE_PREFIX}old.txt"
    stale.write_text("合成舊 facts", encoding="utf-8")
    old = cc.time.time() - cc._STALE_PROMPT_FILE_SEC - 60
    os.utime(stale, (old, old))
    fresh = tmp_path / f"{cc._PROMPT_FILE_PREFIX}fresh.txt"
    fresh.write_text("另一個還在用的", encoding="utf-8")
    cli((0, "合成回覆", ""))
    cc._chat_via_cli("x", [], [], None)
    assert not stale.exists() and fresh.exists()


def test_prompt_file_write_failure_falls_back_and_leaves_nothing(cli, monkeypatch, tmp_path):
    cli((0, "不該執行", ""))

    def broken(*_a, **_k):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(cc.os, "fdopen", broken)
    monkeypatch.setattr(cc.subprocess, "run", lambda *_a, **_k: pytest.fail("no CLI without its prompt"))
    assert cc.chat("x", [], []) is None
    assert not list(tmp_path.glob(cc._PROMPT_FILE_PREFIX + "*"))


def test_api_credit_switch_survives_a_prompt_file_failure(cli, monkeypatch):
    monkeypatch.setattr(cc.settings, "claude_use_cli", False)
    monkeypatch.setattr(cc.settings, "claude_api_key", "synthetic-key")
    monkeypatch.setattr(cc, "_build_payload", lambda *_a: {"messages": []})

    def no_credit(_payload):
        raise cc.ClaudeQuotaExhausted("Your credit balance is too low to access the API")

    monkeypatch.setattr(cc, "_request", no_credit)

    def no_space(*_a, **_k):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(cc.tempfile, "mkstemp", no_space)
    assert cc.chat("x", [], []) is None


# 2026-09-27: a quota error from the CLI cools the CLI down too.

def test_cli_quota_error_cools_the_cli_down(cli):
    runs = cli((1, "", "rate limit reached"))
    assert cc.chat("x", [], []) is None
    assert cc.chat("x", [], []) is None  # still cooling down
    assert len(runs) == 1  # the CLI was not run again
    state = json.loads(cc._STATE_FILE.read_text(encoding="utf-8"))
    state["quota_exhausted_until"] = time.time() - 1
    cc._STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    cli((0, "合成回覆", ""))
    assert cc.chat("x", [], []) == "合成回覆"  # tried again once the cooldown is over
    assert len(runs) == 2


def test_prefer_cli_mode_waits_out_the_cooldown_without_the_api(cli, monkeypatch):
    monkeypatch.setattr(cc.settings, "claude_use_cli", False)
    monkeypatch.setattr(cc.settings, "claude_api_key", "synthetic-key")
    monkeypatch.setattr(cc, "_build_payload", lambda *_a: {"messages": []})
    monkeypatch.setattr(cc, "_request", lambda _payload: pytest.fail("no API call while cooling down"))
    cc._remember_cli_preference()
    runs = cli((1, "", "rate limit reached"))
    assert cc.chat("x", [], []) is None
    assert cc.chat("x", [], []) is None
    assert len(runs) == 1


def test_prefer_cli_mode_keeps_the_preference_through_the_cooldown(cli, monkeypatch):
    # 2026-10-04: the quota mark is merged into the state; it used to replace
    # the whole file, and without prefer_cli a key-less setup never ran the CLI
    # again.
    monkeypatch.setattr(cc.settings, "claude_use_cli", False)
    monkeypatch.setattr(cc.settings, "claude_api_key", "synthetic-key")
    monkeypatch.setattr(cc, "_build_payload", lambda *_a: {"messages": []})
    api_calls = []

    def no_credit(_payload):
        api_calls.append(1)
        raise cc.ClaudeQuotaExhausted("Your credit balance is too low to access the API")

    monkeypatch.setattr(cc, "_request", no_credit)
    cc._remember_cli_preference()
    runs = cli((1, "", "rate limit reached"))
    assert cc.chat("x", [], []) is None
    assert cc._prefer_cli() is True  # the quota mark kept the preference
    state = json.loads(cc._STATE_FILE.read_text(encoding="utf-8"))
    state["quota_exhausted_until"] = time.time() - 1
    cc._STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    cli((0, "合成回覆", ""))
    assert cc.chat("x", [], []) == "合成回覆"  # the CLI first, no API call
    assert api_calls == [] and len(runs) == 2
    assert cc._prefer_cli() is True
