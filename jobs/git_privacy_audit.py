"""Fail-closed privacy audit for Git revisions and pending pushes.

Output intentionally contains only category counts: never matched values or
paths, because CI and maintenance output can itself leave the machine.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


_ZERO_OID_RE = re.compile(r"^0+$")
_LINE_IDENTIFIER_RE = re.compile(r"(?<![A-Za-z0-9_])[CUGR][0-9A-Fa-f]{24,}(?![A-Za-z0-9_])")
_CREDENTIAL_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_])AIza[0-9A-Za-z_-]{20,}"),
    re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)"
        r"\s*[:=]\s*(['\"])(?!dummy\b|test\b|example\b)[^\s'\"]{12,}\1"
    ),
    re.compile(r"(?i)authorization\s*[:=]\s*['\"]?bearer\s+[A-Za-z0-9._-]{16,}"),
    re.compile(r"https://discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{12,}"),
)
_PHONE_RE = re.compile(r"(?<!\d)09\d{2}[- ]?\d{3}[- ]?\d{3}(?!\d)")
_EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![A-Za-z0-9.-])")
_NAMED_CLINICIAN_RE = re.compile(
    r"(?:王|李|張|劉|陳|楊|黃|趙|吳|周|徐|孫|馬|朱|胡|郭|何|林|高|羅|鄭|"
    r"梁|謝|宋|唐|許|韓|馮|鄧|曹|彭|曾|蕭|田|董|袁|潘|于|蔣|蔡|余|杜|"
    r"葉|程|蘇|魏|呂|丁|任|沈|姚|盧|姜|崔|鍾|譚|陸|汪|范|金|石|廖|賈|"
    r"夏|韋|傅|方|白|鄒|孟|熊|秦|邱|江|尹|薛|閻|段|雷|侯|龍|史|陶|黎|"
    r"賀|顧|毛|郝|龔|邵|萬|錢|嚴|賴|武|戴|莫|孔|向|湯)"
    r"[\u4e00-\u9fff]{0,2}(?:牙醫師|醫師)"
)
_PRIVATE_SCHEDULE_DATE_RE = re.compile(
    r"(?:20(?:2[0-9]|[3-8][0-9]|9[0-8]))[-/]\d{1,2}[-/]\d{1,2}"
    r"|(?<![\d/])(?:1[0-2]|0?[1-9])(?:/|月)(?:3[01]|[12]\d|0?[1-9])(?:日|號)?"
)
_PRIVATE_SCHEDULE_CLOCK_RE = re.compile(r"(?:[01]?\d|2[0-3])(?::[0-5]\d|點(?:半)?)")
_PRIVATE_SCHEDULE_TOPIC_RE = re.compile(
    r"(?:回診|看診|手術|疫苗|行程|聚餐|住宿|航班|面試|上課|預約|牙科|醫院)",
    re.IGNORECASE,
)
_SYNTHETIC_SCHEDULE_MARKER = "privacy-safe-fixture"
_SAFE_PRIVATE_TERM_RE = re.compile(r"^(?:測試|範例|示例|假名|TEST|EXAMPLE)", re.IGNORECASE)
_GENERIC_FAMILY_TERMS = {
    "爸爸", "媽媽", "父親", "母親", "哥哥", "弟弟", "姊姊", "姐姐",
    "妹妹", "爺爺", "奶奶", "阿公", "阿婆", "全家",
}
_FORBIDDEN_EXACT = {
    "user_aliases.json",
    "family_roles.local.json",
    "privacy_terms.local.json",
    "privacy_terms.local.txt",
    "pending_dlq.jsonl",
    "pending_feedback.json",
    "feedback_state.json",
    "quote_history.json",
    "line_bot.db",
    "event_reminder_private.json",
    "jobs/push_pending_drafts.py",
}
_FORBIDDEN_PREFIXES = ("logs/", "finetune/data/")
_FORBIDDEN_DB_RE = re.compile(
    r"(?:^|/)[^/]+\.(?:db|sqlite|sqlite3)(?:$|[-.](?:wal|shm|journal)$)",
    re.IGNORECASE,
)
_TEXT_SUFFIXES = {
    "", ".cfg", ".conf", ".csv", ".env", ".html", ".ini", ".js",
    ".json", ".jsonl", ".md", ".py", ".rst", ".sh", ".sql", ".toml",
    ".key", ".pem", ".ts", ".txt", ".yaml", ".yml",
}
_MAX_TEXT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Finding:
    category: str
    path: str = ""
    revision: str = ""


def _run_git(repo: Path, args: Sequence[str]) -> bytes:
    completed = subprocess.run(
        ["git", *args], cwd=repo, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("git privacy audit could not inspect repository state")
    return completed.stdout


def _is_forbidden_path(path: str) -> bool:
    normalized = PurePosixPath(path).as_posix().lstrip("./")
    return (
        normalized in _FORBIDDEN_EXACT
        or normalized.startswith(_FORBIDDEN_PREFIXES)
        or _FORBIDDEN_DB_RE.search(normalized) is not None
    )


def _looks_textual(path: str, data: bytes) -> bool:
    return (
        len(data) <= _MAX_TEXT_BYTES
        and b"\0" not in data[:8192]
        and PurePosixPath(path).suffix.lower() in _TEXT_SUFFIXES
    )


def _normalize_private_terms(private_terms: Iterable[str]) -> tuple[str, ...]:
    clean: set[str] = set()
    for value in private_terms:
        term = str(value).strip()
        if (
            len(term) >= 2
            and term not in _GENERIC_FAMILY_TERMS
            and not _SAFE_PRIVATE_TERM_RE.match(term)
        ):
            clean.add(term)
    return tuple(sorted(clean, key=lambda item: (-len(item), item)))


def scan_blob(
    path: str,
    data: bytes,
    *,
    private_terms: Iterable[str] = (),
    revision: str = "",
) -> list[Finding]:
    categories: set[str] = set()
    if _is_forbidden_path(path):
        categories.add("forbidden_path")
    if (
        PurePosixPath(path).suffix.lower() in _TEXT_SUFFIXES
        and len(data) > _MAX_TEXT_BYTES
    ):
        categories.add("oversized_text")
    if not _looks_textual(path, data):
        return [Finding(category, path, revision) for category in sorted(categories)]

    text = data.decode("utf-8", errors="replace")
    if _LINE_IDENTIFIER_RE.search(text):
        categories.add("line_identifier")
    if any(pattern.search(text) for pattern in _CREDENTIAL_PATTERNS):
        categories.add("credential")
    if any(match.group(0).replace(" ", "").replace("-", "") != "0900000000" for match in _PHONE_RE.finditer(text)):
        categories.add("phone_number")
    if any(
        match.rsplit("@", 1)[-1].lower()
        not in {"example.com", "example.net", "example.org", "example.invalid"}
        for match in _EMAIL_RE.findall(text)
    ):
        categories.add("email_address")
    if any(
        _SYNTHETIC_SCHEDULE_MARKER not in line
        and _PRIVATE_SCHEDULE_DATE_RE.search(line)
        and _PRIVATE_SCHEDULE_CLOCK_RE.search(line)
        and _PRIVATE_SCHEDULE_TOPIC_RE.search(line)
        for line in text.splitlines()
    ):
        categories.add("private_schedule")
    if any(term in text for term in _normalize_private_terms(private_terms)):
        categories.add("private_name")
    if _NAMED_CLINICIAN_RE.search(text):
        categories.add("private_name")
    return [Finding(category, path, revision) for category in sorted(categories)]


def _collect_json_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _collect_json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _collect_json_strings(item)


def load_private_terms(repo: Path) -> tuple[str, ...]:
    terms: list[str] = []
    for name in (
        "user_aliases.json",
        "family_roles.local.json",
        "privacy_terms.local.json",
    ):
        path = repo / name
        if path.is_file() and not path.is_symlink():
            try:
                terms.extend(_collect_json_strings(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise RuntimeError("local private-term source could not be read safely") from None
    path = repo / "privacy_terms.local.txt"
    if path.is_file() and not path.is_symlink():
        try:
            terms.extend(path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeError):
            raise RuntimeError("local private-term source could not be read safely") from None
    return _normalize_private_terms(terms)


def _scan_paths(
    repo: Path,
    paths: Iterable[str],
    blob_loader,
    *,
    revision: str,
) -> list[Finding]:
    private_terms = load_private_terms(repo)
    findings: list[Finding] = []
    for path in paths:
        try:
            blob = blob_loader(path)
        except (OSError, RuntimeError):
            findings.append(Finding("unreadable_file", path, revision))
            continue
        findings.extend(scan_blob(path, blob, private_terms=private_terms, revision=revision))
    return findings


def _tree_paths(repo: Path, treeish: str) -> list[str]:
    raw = _run_git(repo, ["ls-tree", "-r", "-z", "--name-only", treeish])
    return [item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item]


def scan_revision(repo: Path | str, revision: str) -> list[Finding]:
    root = Path(repo).resolve()
    return _scan_paths(
        root,
        _tree_paths(root, revision),
        lambda path: _run_git(root, ["show", f"{revision}:{path}"]),
        revision=revision,
    )


def scan_remote_main(repo: Path | str) -> list[Finding]:
    """Refresh and scan the current GitHub-facing main tip.

    A failed refresh is deliberately fatal to the audit: stale local knowledge
    must not be reported as proof that the remote branch is clean.
    """

    root = Path(repo).resolve()
    _run_git(
        root,
        [
            "fetch",
            "--quiet",
            "--no-tags",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        ],
    )
    return scan_revision(root, "refs/remotes/origin/main")


def scan_index(repo: Path | str) -> list[Finding]:
    root = Path(repo).resolve()
    raw = _run_git(root, ["ls-files", "-z", "--cached"])
    paths = [item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item]
    return _scan_paths(root, paths, lambda path: _run_git(root, ["show", f":{path}"]), revision="index")


def scan_worktree(repo: Path | str) -> list[Finding]:
    root = Path(repo).resolve()
    raw = _run_git(root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    paths = [item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item]
    return _scan_paths(root, paths, lambda path: (root / path).read_bytes(), revision="worktree")


def _dedupe(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(set(findings), key=lambda finding: (finding.category, finding.revision, finding.path))


def scan_push_updates(repo: Path | str, update_lines: Iterable[str]) -> list[Finding]:
    root = Path(repo).resolve()
    findings: list[Finding] = []
    for line in update_lines:
        fields = line.strip().split()
        if len(fields) != 4:
            if fields:
                findings.append(Finding("invalid_push_update", revision="push"))
            continue
        _local_ref, local_oid, _remote_ref, remote_oid = fields
        if _ZERO_OID_RE.fullmatch(local_oid):
            continue
        rev_args = ["rev-list", local_oid, "--not", "--remotes"]
        if not _ZERO_OID_RE.fullmatch(remote_oid):
            rev_args = ["rev-list", f"{remote_oid}..{local_oid}"]
        revisions = _run_git(root, rev_args).decode("ascii", errors="strict").splitlines()
        for revision in revisions:
            findings.extend(scan_revision(root, revision))
    return _dedupe(findings)


def format_summary(findings: Iterable[Finding]) -> str:
    counts = Counter(finding.category for finding in findings)
    if not counts:
        return "privacy_audit=passed findings=0"
    categories = ",".join(f"{name}:{counts[name]}" for name in sorted(counts))
    return f"privacy_audit=failed findings={sum(counts.values())} categories={categories}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit Git content for private LINE bot data")
    parser.add_argument("--repo", default=".")
    parser.add_argument(
        "--scope",
        action="append",
        choices=("head", "index", "worktree", "remote", "push"),
        required=True,
    )
    args = parser.parse_args(argv)
    root = Path(args.repo).resolve()
    findings: list[Finding] = []
    try:
        for scope in dict.fromkeys(args.scope):
            if scope == "head":
                findings.extend(scan_revision(root, "HEAD"))
            elif scope == "index":
                findings.extend(scan_index(root))
            elif scope == "worktree":
                findings.extend(scan_worktree(root))
            elif scope == "remote":
                findings.extend(scan_remote_main(root))
            else:
                findings.extend(scan_push_updates(root, sys.stdin.read().splitlines()))
    except (OSError, RuntimeError, UnicodeError, ValueError):
        findings.append(Finding("audit_error"))
    findings = _dedupe(findings)
    print(format_summary(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
