"""2026-10-11 每日記憶：claude_client.complete_json 用和回覆同一組隔離參數呼叫 CLI。

system prompt 走 0600 暫存檔、用完刪掉；素材走 stdin；用量上限打開既有的冷卻；
不更新 prefer_cli。subprocess 一律是假的；所有文字都是合成的。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess

import pytest

import claude_client as cc

SYSTEM = "SYS-合成抽取規則"
USER = "USER-合成對話紀錄"
LEAK = "合成私人片段"


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_STATE_FILE", tmp_path / "claude-state.json")
    monkeypatch.setattr(cc, "_cli_executable", lambda: "/usr/local/bin/claude-fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-synthetic-must-not-leak")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-gemini-key")
    monkeypatch.setattr(cc.tempfile, "tempdir", str(tmp_path))
    runs = []

    def use(result):
        def fake_run(args, **kwargs):
            assert "stdin" not in kwargs
            path = args[args.index("--system-prompt-file") + 1]
            runs.append({
                "args": list(args),
                "input": kwargs.get("input"),
                "env": kwargs.get("env") or {},
                "timeout": kwargs.get("timeout"),
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


def _state(tmp_path) -> dict:
    path = tmp_path / "claude-state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_same_isolation_flags_as_replies_and_chat_stays_out_of_argv(cli, monkeypatch, tmp_path):
    runs = cli((0, '[{"person": "成員甲", "fact": "住在台中", "src": 1}]\n', ""))
    assert cc.complete_json(SYSTEM, USER, timeout_s=30) == '[{"person": "成員甲", "fact": "住在台中", "src": 1}]'
    job = runs[-1]
    monkeypatch.setattr(cc, "_build_cli_prompt", lambda *_a: (SYSTEM, USER))
    cc._chat_via_cli("x", [], [], None)
    reply = runs[-1]
    strip = lambda args: [a for a in args if a != args[args.index("--system-prompt-file") + 1]]  # noqa: E731
    assert strip(job["args"]) == strip(reply["args"])
    assert job["env"] == reply["env"]
    assert not any(USER in a or SYSTEM in a for a in job["args"])
    assert job["input"] == USER and job["system"] == SYSTEM and job["mode"] == 0o600
    assert "ANTHROPIC_API_KEY" not in job["env"] and "GEMINI_API_KEY" not in job["env"]
    assert not os.path.exists(job["path"])
    assert "prefer_cli" not in _state(tmp_path)


def test_a_short_timeout_is_raised_to_the_cli_minimum(cli):
    runs = cli((0, "[]", ""))
    cc.complete_json(SYSTEM, USER, timeout_s=1)
    assert runs[0]["timeout"] == 10


@pytest.mark.parametrize(
    "result",
    [(1, "", "rate limit reached"), (1, "", "Claude AI usage limit reached|9999999999"), (0, "5-hour limit reached ∙ resets 3pm", "")],
)
def test_a_usage_limit_opens_the_shared_cooldown(cli, tmp_path, result):
    cli(result)
    with pytest.raises(cc.ClaudeQuotaExhausted):
        cc.complete_json(SYSTEM, USER, timeout_s=30)
    state = _state(tmp_path)
    assert state["quota_exhausted_until"] > 0 and cc.quota_exhausted()
    assert "prefer_cli" not in state


def test_no_cli_is_unavailable(monkeypatch):
    monkeypatch.setattr(cc, "_cli_executable", lambda: None)
    with pytest.raises(cc.ClaudeCliUnavailable):
        cc.complete_json(SYSTEM, USER, timeout_s=30)


@pytest.mark.parametrize(
    "result",
    [subprocess.TimeoutExpired(["claude"], 30), (1, "", f"fatal: {LEAK}"), (2, LEAK, "")],
)
def test_other_failures_are_provider_errors_without_chat_text(cli, tmp_path, result, caplog):
    runs = cli(result)
    with pytest.raises(cc.ClaudeProviderError) as exc:
        cc.complete_json(SYSTEM, USER, timeout_s=30)
    assert not isinstance(exc.value, cc.ClaudeCliUnavailable)
    assert LEAK not in str(exc.value) and LEAK not in caplog.text
    assert not os.path.exists(runs[0]["path"])
    assert not _state(tmp_path).get("quota_exhausted_until")


def test_a_launch_failure_is_unavailable(cli):
    cli(OSError("synthetic launch failure"))
    with pytest.raises(cc.ClaudeCliUnavailable):
        cc.complete_json(SYSTEM, USER, timeout_s=30)


# 2026-10-11 修正第一輪 #3：job 要分得出逾時、CLI 其他失敗、登入失效，不靠解析訊息文字。


def test_a_timeout_is_its_own_error_type(cli):
    cli(subprocess.TimeoutExpired(["claude"], 30))
    with pytest.raises(cc.ClaudeCliTimeout):
        cc.complete_json(SYSTEM, USER, timeout_s=30)


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [(f"fatal: {LEAK}", "other"), ("Please log in again", "auth"), ("unknown option --x", "usage"), ("model not found", "model")],
)
def test_other_exit_failures_carry_their_kind(cli, stderr, kind):
    cli((1, "", stderr))
    with pytest.raises(cc.ClaudeCliFailed) as exc:
        cc.complete_json(SYSTEM, USER, timeout_s=30)
    assert exc.value.kind == kind and LEAK not in str(exc.value)


def test_the_reply_path_still_raises_plain_provider_errors(cli, monkeypatch):
    monkeypatch.setattr(cc, "_build_cli_prompt", lambda *_a: (SYSTEM, USER))
    cli(subprocess.TimeoutExpired(["claude"], 60))
    with pytest.raises(cc.ClaudeProviderError) as exc:
        cc._chat_via_cli("x", [], [], None)
    assert type(exc.value) is cc.ClaudeProviderError  # 回覆路徑的行為不變
