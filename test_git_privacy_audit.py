from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from jobs import git_privacy_audit as audit

_PASS_LINE = "privacy_audit=passed findings=0"
_PATTERNS_ONLY_LINE = "privacy_audit=patterns_only private_terms=unavailable"
_REFUSED_LINE = "privacy_audit=failed findings=1 categories=private_terms_unavailable:1"
_ZERO_OID = "0" * 40
_TERM = "私人姓名甲"


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


def _new_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    (path / "safe.txt").write_text("safe\n", encoding="utf-8")
    _commit(path, "base")
    return path


def _ignore(repo: Path, name: str) -> None:
    """Term files are gitignored for real: add -A and the worktree scope skip them."""
    info = repo / ".git" / "info"
    info.mkdir(exist_ok=True)
    with (info / "exclude").open("a", encoding="utf-8") as exclude:
        exclude.write(f"{name}\n")


def _add_local_terms(repo: Path, *lines: str) -> None:
    _ignore(repo, "privacy_terms.local.txt")
    (repo / "privacy_terms.local.txt").write_text(
        "".join(f"{line}\n" for line in lines),
        encoding="utf-8",
    )


def _break_term_file(repo: Path) -> None:
    _ignore(repo, "user_aliases.json")
    (repo / "user_aliases.json").write_text("{not json", encoding="utf-8")


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    primary = _new_repo(tmp_path / "primary")
    worktree = tmp_path / "linked"
    _git(primary, "worktree", "add", "-q", "-b", "feature", str(worktree))
    return primary, worktree


def _cli(capsys, monkeypatch, repo: Path, *scopes: str, stdin: str = "") -> tuple[int, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    args = ["--repo", str(repo)]
    for scope in scopes:
        args += ["--scope", scope]
    code = audit.main(args)
    return code, capsys.readouterr().out.strip()


def _update(local: str, remote: str = _ZERO_OID) -> str:
    return f"refs/heads/topic {local} refs/heads/topic {remote}\n"


def test_forbidden_private_artifact_paths_are_rejected():
    for path in (
        "user_aliases.json",
        "family_roles.local.json",
        "family_kinship.local.json",
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
        "state/memory_review.noindex/report_20261030_230000_ab12.md",
        "state/memory_review.noindex/forget_log.jsonl",
        "state/fact_hash.key",
        "state/fact_ledger.jsonl",
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
    _add_local_terms(repo, _TERM)
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


def _updates_that_can_send_objects(repo: Path) -> list[str]:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", base)
    (repo / "safe.txt").write_text("changed\n", encoding="utf-8")
    tip = _commit(repo, "second")
    other = f"refs/heads/other {tip} refs/heads/other {_ZERO_OID}\n"
    return [
        _update(tip),
        _update(base),  # no new commit: rev-list only lists commits
        _update(tip, tip),
        f"(delete) {_ZERO_OID} refs/heads/old {base}\n" + _update(tip),
        _update(tip) + other,
        _update(base) + other,
    ]


def test_push_needs_terms_for_every_update_that_can_send_objects(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    repo = _new_repo(tmp_path / "repo")
    updates = _updates_that_can_send_objects(repo)

    for stdin in updates:
        assert _cli(capsys, monkeypatch, repo, "push", stdin=stdin) == (1, _REFUSED_LINE)

    _add_local_terms(repo, _TERM)

    for stdin in updates:
        assert _cli(capsys, monkeypatch, repo, "push", stdin=stdin) == (0, _PASS_LINE)


@pytest.mark.parametrize("broken", [False, True])
def test_push_that_sends_nothing_needs_no_terms(
    tmp_path: Path,
    capsys,
    monkeypatch,
    broken: bool,
):
    # A deletion carries no content, and a leak response may need it.
    repo = _new_repo(tmp_path / "repo")
    if broken:
        _break_term_file(repo)
    tip = _git(repo, "rev-parse", "HEAD")

    for stdin in (f"(delete) {_ZERO_OID} refs/heads/old {tip}\n", ""):
        assert _cli(capsys, monkeypatch, repo, "push", stdin=stdin) == (
            0,
            _PATTERNS_ONLY_LINE,
        )


@pytest.mark.parametrize(
    ("scopes", "without_terms"),
    [
        (("head",), (0, _PATTERNS_ONLY_LINE)),  # CI has no term files
        (("head", "push"), (0, _PATTERNS_ONLY_LINE)),
        (("index",), (1, _REFUSED_LINE)),  # agent_rules/line_bot_git_privacy.md
        (("worktree",), (1, _REFUSED_LINE)),  # deploy scripts that only read the rc
        (("remote",), (1, _REFUSED_LINE)),
        (("head", "index"), (1, _REFUSED_LINE)),
        (("index", "head"), (1, _REFUSED_LINE)),
        (("push", "index"), (1, _REFUSED_LINE)),
        (("index", "worktree", "remote"), (1, _REFUSED_LINE)),  # daily_line_bot_review
    ],
)
def test_only_head_runs_without_terms(
    tmp_path: Path,
    capsys,
    monkeypatch,
    scopes: tuple[str, ...],
    without_terms: tuple[int, str],
):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    repo = _new_repo(tmp_path / "repo")
    _git(repo, "branch", "-M", "main")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    (repo / "staged.txt").write_text("safe\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")

    assert _cli(capsys, monkeypatch, repo, *scopes) == without_terms

    _add_local_terms(repo, _TERM)

    assert _cli(capsys, monkeypatch, repo, *scopes) == (0, _PASS_LINE)


def test_terms_that_all_normalize_away_count_as_none(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    repo = _new_repo(tmp_path / "repo")
    _add_local_terms(repo, "爸爸", "測試成員甲", "甲")
    tip = _git(repo, "rev-parse", "HEAD")

    assert _cli(capsys, monkeypatch, repo, "push", stdin=_update(tip)) == (1, _REFUSED_LINE)
    assert _cli(capsys, monkeypatch, repo, "head") == (0, _PATTERNS_ONLY_LINE)


def test_symlinked_term_file_counts_as_none(tmp_path: Path, capsys, monkeypatch):
    repo = _new_repo(tmp_path / "repo")
    outside = tmp_path / "outside.txt"
    outside.write_text(f"{_TERM}\n", encoding="utf-8")
    _ignore(repo, "privacy_terms.local.txt")
    (repo / "privacy_terms.local.txt").symlink_to(outside)
    tip = _git(repo, "rev-parse", "HEAD")

    assert _cli(capsys, monkeypatch, repo, "push", stdin=_update(tip)) == (1, _REFUSED_LINE)


@pytest.mark.parametrize("linked", [False, True])
def test_broken_term_file_fails_every_scan_that_needs_it(
    tmp_path: Path,
    capsys,
    monkeypatch,
    linked: bool,
):
    primary, worktree = _linked_worktree(tmp_path)
    _break_term_file(primary)
    repo = worktree if linked else primary
    tip = _git(repo, "rev-parse", "HEAD")
    audit_error = (1, "privacy_audit=failed findings=1 categories=audit_error:1")

    for scope, stdin in (("push", _update(tip)), ("index", ""), ("head", "")):
        assert _cli(capsys, monkeypatch, repo, scope, stdin=stdin) == audit_error


def test_terms_are_read_once_per_update_not_per_commit(tmp_path: Path, monkeypatch):
    # rev-list gives the clean tip first: the leaking commit is scanned after the file is gone.
    repo = _new_repo(tmp_path / "repo")
    _add_local_terms(repo, _TERM)
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "public.py").write_text(f'owner = "{_TERM}"\n', encoding="utf-8")
    _commit(repo, "leak")
    (repo / "public.py").unlink()
    tip = _commit(repo, "remove")
    scan_tree = audit._scan_tree

    def drop_terms_after_each_scan(*args):
        findings = scan_tree(*args)
        (repo / "privacy_terms.local.txt").unlink(missing_ok=True)
        return findings

    monkeypatch.setattr(audit, "_scan_tree", drop_terms_after_each_scan)

    findings = audit.scan_push_updates(repo, [_update(tip, base).strip()])

    assert any(item.category == "private_name" for item in findings)


@pytest.mark.parametrize("scope", ["head", "index", "worktree"])
def test_linked_worktree_scans_with_the_primary_checkout_terms(
    tmp_path: Path,
    capsys,
    monkeypatch,
    scope: str,
):
    primary, worktree = _linked_worktree(tmp_path)
    _add_local_terms(primary, _TERM)

    assert _cli(capsys, monkeypatch, worktree, scope) == (0, _PASS_LINE)

    (worktree / "public.py").write_text(f'owner = "{_TERM}"\n', encoding="utf-8")
    _git(worktree, "add", "public.py")
    if scope == "head":
        _commit(worktree, "leak")
    code, output = _cli(capsys, monkeypatch, worktree, scope)

    assert code == 1
    assert "private_name" in output
    assert _TERM not in output
    assert "public.py" not in output


def test_linked_worktree_terms_are_its_own_plus_the_primary(tmp_path: Path):
    primary, worktree = _linked_worktree(tmp_path)
    _add_local_terms(primary, _TERM)

    # fact_filters calls load_private_terms from the running bot and from worktree tests.
    assert audit.load_private_terms(worktree) == ()
    assert audit._terms_for_audit(worktree) == (_TERM,)

    (worktree / "privacy_terms.local.txt").write_text("私人姓名乙\n", encoding="utf-8")

    assert set(audit._terms_for_audit(worktree)) == {_TERM, "私人姓名乙"}


def test_worktree_of_a_bare_repo_has_no_primary_checkout(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    source = _new_repo(tmp_path / "source")
    bare = tmp_path / "shared.git"
    _git(tmp_path, "clone", "-q", "--bare", str(source), str(bare))
    worktree = tmp_path / "linked"
    _git(bare, "worktree", "add", "-q", "-b", "feature", str(worktree))
    # Bait: shared.git's parent is not a checkout, so this file must not be read.
    (tmp_path / "privacy_terms.local.txt").write_text(f"{_TERM}\n", encoding="utf-8")
    (worktree / "new.txt").write_text("safe\n", encoding="utf-8")
    tip = _commit(worktree, "new")

    assert _cli(capsys, monkeypatch, worktree, "push", stdin=_update(tip)) == (
        1,
        _REFUSED_LINE,
    )


def test_linked_worktree_remote_scope_uses_the_primary_terms(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    primary, worktree = _linked_worktree(tmp_path)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    _git(primary, "branch", "-M", "main")
    (primary / "public.py").write_text(f'owner = "{_TERM}"\n', encoding="utf-8")
    _commit(primary, "leak")
    _git(primary, "remote", "add", "origin", str(remote))
    _git(primary, "push", "-q", "origin", "main")
    _add_local_terms(primary, _TERM)

    code, output = _cli(capsys, monkeypatch, worktree, "remote")

    assert code == 1
    assert "private_name" in output


def test_term_lookup_ignores_the_process_cwd(tmp_path: Path, capsys, monkeypatch):
    other = _new_repo(tmp_path / "other")
    _add_local_terms(other, _TERM)
    repo = _new_repo(tmp_path / "repo")
    tip = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(other)

    assert _cli(capsys, monkeypatch, repo, "push", stdin=_update(tip)) == (1, _REFUSED_LINE)


def test_replacing_the_term_loader_still_reaches_the_scans(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    # ops/state privacy_audit_worktree.py swaps this module-level name before main().
    _primary, worktree = _linked_worktree(tmp_path)
    monkeypatch.setattr(audit, "load_private_terms", lambda _repo: (_TERM,))

    assert _cli(capsys, monkeypatch, worktree, "index") == (0, _PASS_LINE)

    (worktree / "public.py").write_text(f'owner = "{_TERM}"\n', encoding="utf-8")
    _git(worktree, "add", "public.py")
    code, output = _cli(capsys, monkeypatch, worktree, "index")

    assert code == 1
    assert "private_name:1" in output


def test_push_line_with_spaces_in_the_local_ref_is_refused(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    # git push origin 'HEAD@{1 minute ago}:refs/heads/x' puts the spaces in <local ref>.
    repo = _new_repo(tmp_path / "repo")
    tip = _git(repo, "rev-parse", "HEAD")
    stdin = f"HEAD@{{1 minute ago}} {tip} refs/heads/x {_ZERO_OID}\n"

    assert _cli(capsys, monkeypatch, repo, "push", stdin=stdin) == (
        1,
        "privacy_audit=failed findings=1 categories=invalid_push_update:1",
    )


def _repo_with_real_hook(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """A checkout running the real pre-push and audit, a bare remote, python3 on PATH."""
    here = Path(__file__).parent
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    repo = tmp_path / "primary"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "branch", "-M", "main")
    (repo / ".githooks").mkdir()
    shutil.copy2(here / ".githooks" / "pre-push", repo / ".githooks" / "pre-push")
    (repo / "jobs").mkdir()
    shutil.copy2(
        here / "jobs" / "git_privacy_audit.py",
        repo / "jobs" / "git_privacy_audit.py",
    )
    _git(repo, "config", "core.hooksPath", ".githooks")
    _git(repo, "remote", "add", "origin", str(remote))
    _commit(repo, "base")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python3 = bin_dir / "python3"
    python3.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python3.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return repo, remote, env


def _push(repo: Path, env: dict[str, str], branch: str) -> tuple[int, str]:
    pushed = subprocess.run(
        ["git", "push", "origin", branch],
        cwd=repo,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    return pushed.returncode, pushed.stdout + pushed.stderr


def _remote_has(remote: Path, branch: str) -> bool:
    found = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
        cwd=remote,
        check=False,
        capture_output=True,
    )
    return found.returncode == 0


def test_real_push_runs_the_hook_with_the_primary_terms(tmp_path: Path):
    repo, remote, env = _repo_with_real_hook(tmp_path)
    worktree = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    (worktree / "safe.txt").write_text("safe\n", encoding="utf-8")
    _commit(worktree, "clean")

    code, output = _push(worktree, env, "feature")

    assert code != 0
    assert _REFUSED_LINE in output
    assert not _remote_has(remote, "feature")

    _add_local_terms(repo, _TERM)
    code, output = _push(worktree, env, "feature")

    assert code == 0, output
    assert _PASS_LINE in output  # a hook that never ran would also push
    assert _remote_has(remote, "feature")

    _git(worktree, "checkout", "-q", "-b", "leaky")
    (worktree / "public.py").write_text(f'owner = "{_TERM}"\n', encoding="utf-8")
    _commit(worktree, "leak")

    code, output = _push(worktree, env, "leaky")

    assert code != 0
    assert "private_name" in output
    assert _TERM not in output
    assert not _remote_has(remote, "leaky")
