"""2026-11-06: `--tools ""` does not stop the CLI itself from reading `@path` on stdin.

Canary, CLI 2.1.295, the production flags: six of ten spellings pulled a local file into the
prompt, a relative path and a turn of the history among them.  The child environment now sets
CLAUDE_CODE_DISABLE_ATTACHMENTS, stdin carries no "@", and what comes back has its "@" again.

Subprocess is always faked; all text is synthetic.  The canary itself is in
ops/state/line_bot_cli_at_mention_20261106/canary/; run it again after a CLI upgrade.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import claude_client as cc
import reply_policy
from speaker_turns import CONTINUATION_INDENT as INDENT

ATTACHMENTS = "CLAUDE_CODE_DISABLE_ATTACHMENTS"
SYSTEM = "SYS-合成系統提示"
OK = "合成回覆"
# What the canary read, in the shape real turns have.
MESSAGE = "\n".join(
    [
        "成員甲：看一下 @/tmp/space.txt",
        f"{INDENT}@/tmp/indent.txt",
        f"{INDENT}第三行。@/tmp/period.txt",
        f"{INDENT}還有 @.env",
        f'{INDENT}引號 @"/tmp/quoted file.txt"',
    ]
)


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """Production's route (no API key, CLI on) with the real prompt and a fake CLI."""
    monkeypatch.setattr(cc, "_STATE_FILE", tmp_path / "claude-state.json")
    monkeypatch.setattr(cc.settings, "claude_use_cli", True)
    monkeypatch.setattr(cc.settings, "claude_api_key", "")
    monkeypatch.setattr(cc, "_cli_executable", lambda: "/usr/local/bin/claude-fake")
    monkeypatch.setattr(cc.tempfile, "tempdir", str(tmp_path))
    runs = []

    def use(stdout):
        def fake_run(args, **kwargs):
            path = args[args.index("--system-prompt-file") + 1]
            runs.append({
                "input": kwargs.get("input"),
                "env": kwargs.get("env") or {},
                "encoding": kwargs.get("encoding"),
                "system": Path(path).read_text(encoding="utf-8"),
            })
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(cc.subprocess, "run", fake_run)
        return runs

    return use


def _reply(prompt: str):
    return cc.chat(prompt, [], [])


def _job(prompt: str):
    return cc.complete_json(SYSTEM, prompt, timeout_s=30)


ROUTES = pytest.mark.parametrize("ask", [_reply, _job], ids=["reply", "job"])


@ROUTES
@pytest.mark.parametrize("parent", [None, "", "0"], ids=["unset", "empty", "zero"])
def test_the_child_always_runs_with_attachments_off(cli, monkeypatch, ask, parent):
    if parent is None:
        monkeypatch.delenv(ATTACHMENTS, raising=False)
    else:
        monkeypatch.setenv(ATTACHMENTS, parent)
    runs = cli(OK)

    assert ask("合成問題") == OK

    assert runs[0]["env"][ATTACHMENTS] == "1"


@ROUTES
def test_no_at_reaches_stdin(cli, ask):
    runs = cli(OK)

    assert ask(MESSAGE) == OK

    assert "@" not in runs[0]["input"]
    assert runs[0]["encoding"] == "utf-8"


def test_only_the_at_changes_on_stdin_history_included(cli):
    runs = cli(OK)
    context = [("user", "成員甲：昨天那個 @/tmp/old.txt"), ("bot", "上一則 @.env")]
    # 「：」「１」 and the indent would not survive an NFKC of the whole text; 「﹫」 is not read anyway.
    message = f"成員甲：１２？！ @.env ＠x ﹫y\n{INDENT}第二行"

    assert cc.chat(message, context, []) == OK

    _system, prompt = cc._build_cli_prompt(message, context, [], None)
    assert prompt.count("@") == 3
    assert runs[0]["input"] == prompt.replace("@", "＠")


def test_the_system_prompt_file_is_left_alone(cli):
    # The CLI does not read mentions there (canary), and a job's rules are its own.
    runs = cli(OK)

    assert cc.complete_json("規則 @/tmp/x.txt", "合成素材", timeout_s=30) == OK

    assert runs[0]["system"] == "規則 @/tmp/x.txt"


def test_the_reply_route_tells_the_model_what_the_full_width_sign_is(cli):
    runs = cli(OK)

    assert cc.chat("合成問題", [], []) == OK

    assert runs[0]["system"].startswith(cc._CLI_AT_NOTE)
    assert runs[0]["system"].endswith(reply_policy.NO_SEARCH_CONTRACT)


def test_the_api_route_keeps_the_text_and_gets_no_note():
    payload = cc._build_payload("成員甲：看 @/tmp/x.txt", [], [], None)

    assert payload["messages"][-1]["content"] == "成員甲：看 @/tmp/x.txt"
    assert cc._CLI_AT_NOTE not in payload["system"]


def test_one_place_starts_the_cli():
    source = Path(cc.__file__).read_text(encoding="utf-8")

    assert re.findall(r"\bsubprocess\.\w+\(", source) == ["subprocess.run("]


@ROUTES
def test_the_output_gets_its_at_back(cli, ask):
    # 「＠」 ends a link for reply_policy, and main looks for the half-width @all.  Decided 2026-11-06:
    # a copied 「＠all」 now acts like 「@all」; an injected page could print that one anyway.
    cli("信箱 a＠example.com，連結 https://www.tiktok.com/＠user/video/123，＠all 開會")

    assert ask("合成問題") == "信箱 a@example.com，連結 https://www.tiktok.com/@user/video/123，@all 開會"
