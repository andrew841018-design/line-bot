from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jobs import git_privacy_audit as audit


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=Privacy Test",
        "-c",
        "user.email=privacy-test@example.invalid",
        "commit",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD")


def test_forbidden_private_artifact_paths_are_rejected():
    for path in (
        "user_aliases.json",
        "family_roles.local.json",
        "privacy_terms.local.txt",
        "pending_dlq.jsonl",
        "pending_feedback.json",
        "feedback_state.json",
        "logs/auto_iterate_20260904.md",
        "line_bot.db",
        "event_reminder_private.json",
        "finetune/data/private.jsonl",
        "runtime/private.sqlite3",
        "runtime/private.db-wal",
    ):
        findings = audit.scan_blob(path, b"safe placeholder")
        assert {finding.category for finding in findings} == {"forbidden_path"}


@pytest.mark.parametrize(
    ("content", "category"),
    [
        (("group = \"C" + "1" * 32 + "\"").encode(), "line_identifier"),
        (("api_key = \"sk-" + "1" * 24 + "\"").encode(), "credential"),
        (("聯絡電話 09" + "12-345-678").encode(), "phone_number"),
        (("信箱 owner@" + "private-domain.tw").encode(), "email_address"),
        (("2026-" + "11-16 14:30 回診").encode(), "private_schedule"),
        (("媽媽 " + "/".join(("8", "30")) + " 11:00 回診").encode(), "private_schedule"),
        ("參加人：私人姓名甲".encode(), "private_name"),
        (("陳" + "某醫師").encode(), "private_name"),
    ],
)
def test_sensitive_content_categories_are_detected_without_storing_values(
    content: bytes,
    category: str,
):
    findings = audit.scan_blob(
        "sample.txt",
        content,
        private_terms={"私人姓名甲"},
    )
    assert category in {finding.category for finding in findings}
    rendered = audit.format_summary(findings)
    assert content.decode() not in rendered
    assert "sample.txt" not in rendered


def test_obvious_test_placeholders_and_example_contacts_are_allowed():
    safe = (
        "U_TEST_SENDER\n"
        "測試成員甲\n"
        "2099-01-01 12:00 測試行程\n"
        "privacy-test@example.invalid\n"
    ).encode()
    assert audit.scan_blob("test_fixture.txt", safe) == []


def test_explicit_synthetic_schedule_marker_is_allowed():
    safe = 'text = "8/30 11:00 測試回診"  # privacy-safe-fixture\n'.encode()
    assert audit.scan_blob("test_fixture.py", safe) == []


def test_oversized_text_fails_closed():
    findings = audit.scan_blob("large.txt", b"x" * (2 * 1024 * 1024 + 1))
    assert {item.category for item in findings} == {"oversized_text"}


def test_scan_revision_loads_private_terms_locally_but_never_reports_them(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "public.py").write_text('owner = "私人姓名甲"\n', encoding="utf-8")
    _commit(repo, "private name")
    (repo / "user_aliases.json").write_text(
        '{"U_LOCAL_ONLY": "私人姓名甲"}\n',
        encoding="utf-8",
    )

    findings = audit.scan_revision(repo, "HEAD")

    assert any(f.category == "private_name" for f in findings)
    assert "私人姓名甲" not in audit.format_summary(findings)


def test_push_range_scans_intermediate_commits_even_if_tip_removed_the_leak(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "user_aliases.json").write_text("{}\n", encoding="utf-8")
    _commit(repo, "leak")
    (repo / "user_aliases.json").unlink()
    tip = _commit(repo, "remove")

    findings = audit.scan_push_updates(
        repo,
        [f"refs/heads/main {tip} refs/heads/main {base}"],
    )

    assert any(f.category == "forbidden_path" for f in findings)


def test_push_range_skips_branch_deletion(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    tip = _commit(repo, "base")
    zeros = "0" * 40

    assert audit.scan_push_updates(
        repo,
        [f"(delete) {zeros} refs/heads/main {tip}"],
    ) == []


def test_new_remote_branch_does_not_rescan_existing_remote_history(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "user_aliases.json").write_text("{}\n", encoding="utf-8")
    existing = _commit(repo, "already remote history")
    _git(repo, "update-ref", "refs/remotes/origin/main", existing)
    (repo / "user_aliases.json").unlink()
    tip = _commit(repo, "remove old leak")
    findings = audit.scan_push_updates(
        repo,
        [f"refs/heads/privacy-clean {tip} refs/heads/privacy-clean {'0' * 40}"],
    )
    assert findings == []


def test_remote_scan_refreshes_and_checks_remote_main(tmp_path: Path):
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    checkout = tmp_path / "checkout"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "branch", "-M", "main")
    _git(source, "remote", "add", "origin", str(remote))
    (source / "user_aliases.json").write_text("{}\n", encoding="utf-8")
    _commit(source, "remote leak")
    _git(source, "push", "-q", "-u", "origin", "main")
    _git(tmp_path, "clone", "-q", "--branch", "main", str(remote), str(checkout))

    findings = audit.scan_remote_main(checkout)

    assert any(item.category == "forbidden_path" for item in findings)


def test_cli_output_is_bounded_and_contains_no_matching_payload(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "leak.txt").write_text(
        'token="sk-' + "1" * 24 + '"\n',
        encoding="utf-8",
    )
    _commit(repo, "leak")

    assert audit.main(["--repo", str(repo), "--scope", "head"]) == 1
    output = capsys.readouterr().out
    assert "privacy_audit=failed" in output
    assert "sk-" + "1" * 24 not in output
    assert "leak.txt" not in output
    assert len(output) < 500


def test_index_and_worktree_scopes_detect_uncommitted_private_content(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    _commit(repo, "base")
    (repo / "staged.txt").write_text("聯絡電話 09" + "12-345-678\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")

    assert any(f.category == "phone_number" for f in audit.scan_index(repo))
    assert any(f.category == "phone_number" for f in audit.scan_worktree(repo))


def test_index_scope_does_not_rescan_unchanged_base_content(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "user_aliases.json").write_text("{}\n", encoding="utf-8")
    _commit(repo, "legacy base")
    (repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    _git(repo, "add", "safe.txt")

    assert audit.scan_index(repo) == []


def test_pre_push_hook_invokes_push_scope_without_echoing_stdin():
    hook = Path(__file__).parent / ".githooks" / "pre-push"
    source = hook.read_text(encoding="utf-8")
    assert "jobs/git_privacy_audit.py" in source
    assert "--scope push" in source
    assert "set -eu" in source


def test_local_role_alias_preserves_runtime_behavior_without_public_name(
    tmp_path: Path, monkeypatch
):
    import line_mentions

    user_id = "U" + "4" * 32
    aliases = tmp_path / "user_aliases.json"
    roles = tmp_path / "family_roles.local.json"
    aliases.write_text('{"' + user_id + '": "測試成員甲"}\n', encoding="utf-8")
    roles.write_text('{"妹妹": "測試成員甲"}\n', encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(roles))

    mapping = line_mentions.configured_family_alias_mapping()
    assert mapping["妹妹"] == "測試成員甲"
    assert line_mentions.user_id_for_alias("妹妹") == user_id
