"""Small Claude Messages API client used by the LINE bot's primary reply path.

Claude is optional.  This module deliberately uses the standard-library HTTP
client so the bot does not need a second SDK just to have a provider fallback.
When Claude is not configured, or when its request cannot be handled, callers
receive ``None`` and can continue with Gemini.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from config import settings
from reply_policy import is_empty_marker

logger = logging.getLogger(__name__)

_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 2048
_STATE_FILE = Path(
    os.environ.get(
        "CLAUDE_USAGE_FILE",
        str(Path(__file__).resolve().with_name("claude_usage.json")),
    )
)


class ClaudeQuotaExhausted(RuntimeError):
    """Claude rejected a request because quota/credits/rate limit is exhausted."""


class ClaudeProviderError(RuntimeError):
    """Non-quota Claude failure; it should not permanently disable Claude."""


class ClaudeCliUnavailable(ClaudeProviderError):
    """The optional logged-in Claude CLI is not installed or not executable."""


class ClaudeCliLimitReached(ClaudeQuotaExhausted):
    """The logged-in account hit its Claude usage limit; ``until`` = retry time."""

    def __init__(self, message: str, *, until: float) -> None:
        super().__init__(message)
        self.until = until


# claude_usage.json is read-modify-written (the gate keys next to prefer_cli);
# webhook threads and burst timers share this process.
_STATE_LOCK = threading.Lock()
_QUOTA_KEYS = ("quota_exhausted_until", "reason")


def _load_state() -> dict[str, Any]:
    try:
        with _STATE_FILE.open(encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    """Atomically persist the small provider gate without logging secrets."""
    tmp_name: str | None = None
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{_STATE_FILE.name}.",
            suffix=".tmp",
            dir=str(_STATE_FILE.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp_name, _STATE_FILE)
    except OSError as exc:
        logger.warning("could not persist Claude quota state: %s", exc)
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def quota_exhausted(now: float | None = None) -> bool:
    until = _load_state().get("quota_exhausted_until", 0)
    try:
        return float(until) > (time.time() if now is None else now)
    except (TypeError, ValueError):
        return False


def quota_status(now: float | None = None) -> dict[str, Any]:
    state = _load_state()
    until = state.get("quota_exhausted_until", 0)
    try:
        until_f = float(until)
    except (TypeError, ValueError):
        until_f = 0.0
    current = time.time() if now is None else now
    return {
        "configured": bool(settings.claude_api_key),
        "quota_exhausted_until": until_f,
        "quota_exhausted": until_f > current,
        "reason": str(state.get("reason", "")),
    }


def _mark_quota_exhausted(reason: str, *, until: float | None = None) -> None:
    """Open the provider gate, merged into the state (keeps ``prefer_cli``).

    2026-10-04: this used to rewrite the whole file.  Production has no API
    key, so the CLI runs only because of ``prefer_cli``; losing it on the
    first quota error disabled Claude for good.  ``reason`` is a category,
    never CLI output.
    """
    now = time.time()
    if until is None:
        until = now + max(60, int(settings.claude_quota_cooldown_sec))
    with _STATE_LOCK:
        state = _load_state()
        state["quota_exhausted_until"] = until
        state["reason"] = reason[:160]
        state["updated_at"] = now
        _save_state(state)


def _clear_quota_exhausted() -> None:
    """Close the gate: drop only the gate keys, keep ``prefer_cli``."""
    with _STATE_LOCK:
        state = _load_state()
        if not state.get("quota_exhausted_until"):
            return
        for key in _QUOTA_KEYS:
            state.pop(key, None)
        state["updated_at"] = time.time()
        _save_state(state)


def _prefer_cli() -> bool:
    return bool(_load_state().get("prefer_cli"))


def _remember_cli_preference() -> None:
    """A working CLI: prefer it from now on; any (expired) gate is over."""
    with _STATE_LOCK:
        state = _load_state()
        state["prefer_cli"] = True
        for key in _QUOTA_KEYS:
            state.pop(key, None)
        state["updated_at"] = time.time()
        _save_state(state)


def _cli_executable() -> str | None:
    override = os.environ.get("CLAUDE_CODE_CMD", "").strip()
    if override:
        return override if os.path.isfile(override) else shutil.which(override)
    for candidate in (
        shutil.which("claude"),
        str(Path.home() / ".local" / "bin" / "claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ):
        if candidate and (os.path.isfile(candidate) or shutil.which(candidate)):
            return candidate
    return None


def _text_from_part(part: Any) -> str:
    text = getattr(part, "text", None)
    return text if isinstance(text, str) else ""


def _image_block(part: Any) -> dict[str, Any] | None:
    inline = getattr(part, "inline_data", None)
    if inline is None:
        return None
    data = getattr(inline, "data", None)
    mime_type = getattr(inline, "mime_type", None)
    if not data or not isinstance(mime_type, str) or not mime_type.startswith("image/"):
        return None
    if isinstance(data, bytes):
        encoded = base64.b64encode(data).decode("ascii")
    elif isinstance(data, str):
        # google-genai may already expose inline data as base64 text.
        encoded = data
    else:
        return None
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": mime_type, "data": encoded},
    }


def _to_claude_content(user_input: Any) -> str | list[dict[str, Any]] | None:
    """Convert the existing Gemini-like input to Claude text/image blocks.

    Audio/video/remote file parts intentionally return ``None`` so Gemini keeps
    its existing multimodal path instead of silently dropping the attachment.
    """
    if isinstance(user_input, str):
        return user_input
    parts = user_input if isinstance(user_input, list) else [user_input]
    content: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, str):
            if part:
                content.append({"type": "text", "text": part})
            continue
        text = _text_from_part(part)
        if text:
            content.append({"type": "text", "text": text})
            continue
        image = _image_block(part)
        if image is not None:
            content.append(image)
            continue
        return None
    return content or None


# memory.append_turn stores the bot's own turns as "bot"; Gemini history says
# "model".  2026-10-09: only "assistant" used to count, so every bot reply was
# merged into the family's "user" turn.
_BOT_ROLES = frozenset({"assistant", "bot", "model"})


def _merge_history(context: list[tuple[str, str]] | None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for role, text in (context or []):
        if not isinstance(text, str) or not text.strip():
            continue
        normalized = "assistant" if role in _BOT_ROLES else "user"
        if messages and messages[-1]["role"] == normalized:
            messages[-1]["content"] += "\n" + text
        elif messages or normalized == "user":
            # The Messages API wants the first turn from the user; a bot turn
            # left at the head of a trimmed history is dropped.
            messages.append({"role": normalized, "content": text})
    return messages


def _with_no_search_contract(system: str) -> str:
    """Claude runs without any search tool (CLI ``--tools ""``; API without tools).

    2026-10-03: it denied a recent, widely reported event from stale memory and
    claimed to have re-checked; the shared prompt never told it it cannot search.
    """
    import gemini_client
    import reply_policy

    system = gemini_client.without_search_instructions(system)
    return f"{system}\n\n{reply_policy.NO_SEARCH_CONTRACT}"


def _build_payload(
    user_input: Any,
    context: list[tuple[str, str]],
    facts: list[str],
    persona_notes: list[dict] | None,
) -> dict[str, Any] | None:
    content = _to_claude_content(user_input)
    if content is None:
        return None
    # Reuse the established LINE persona/rule prompt so changing providers
    # does not silently remove the user's existing response constraints.
    import gemini_client

    system = _with_no_search_contract(
        gemini_client._build_system_instruction(
            facts,
            persona_notes,
            user_input=user_input,
        )
    )
    messages = _merge_history(context)
    if messages and messages[-1]["role"] == "user":
        previous = messages[-1]["content"]
        if isinstance(previous, str) and isinstance(content, str):
            messages[-1]["content"] = previous + "\n" + content
        else:
            previous_blocks = (
                [{"type": "text", "text": previous}]
                if isinstance(previous, str)
                else list(previous)
            )
            current_blocks = (
                [{"type": "text", "text": content}]
                if isinstance(content, str)
                else content
            )
            messages[-1]["content"] = previous_blocks + current_blocks
    else:
        messages.append({"role": "user", "content": content})
    return {
        "model": settings.claude_model,
        "max_tokens": _DEFAULT_MAX_TOKENS,
        "system": system,
        "messages": messages,
    }


def _build_cli_prompt(
    user_input: Any,
    context: list[tuple[str, str]],
    facts: list[str],
    persona_notes: list[dict] | None,
) -> tuple[str, str] | None:
    """Build a text-only prompt for the account-authenticated Claude CLI."""
    content = _to_claude_content(user_input)
    if not isinstance(content, str):
        # The CLI path cannot safely carry Gemini Part bytes. Let the API or
        # Gemini multimodal path handle images/audio/video instead.
        return None
    import gemini_client

    system = _with_no_search_contract(
        gemini_client._build_system_instruction(
            facts,
            persona_notes,
            user_input=user_input,
        )
    )
    # 2026-10-09 Andrew：「區分不同人（說話的人），不能全部都一概當成『使用者』」。
    # Family turns already start with who said them (「成員甲：…」); only the bot's
    # own turns get a name here.
    history_lines = []
    for role, text in (context or []):
        if not isinstance(text, str) or not text.strip():
            continue
        history_lines.append(f"咪寶：{text}" if role in _BOT_ROLES else text)
    # A blank line between turns: a multi-line bot reply never runs into the
    # next family turn (older turns, or ones whose speaker is unknown, carry no
    # 「稱呼：」 of their own).
    history_block = "\n\n".join(history_lines) or "（沒有先前對話）"
    user_prompt = (
        f"【最近對話】\n{history_block}\n\n"
        f"【最新訊息】\n{content}"
    )
    return system, user_prompt


def _quota_error(status: int, body: str) -> bool:
    lowered = body.lower()
    if status in {402, 429}:
        return True
    return any(
        marker in lowered
        for marker in (
            "credit balance",
            "quota",
            "rate limit",
            "rate_limit",
            "too many requests",
            "billing",
            "exceeded",
        )
    )


def _api_credit_empty(error_text: str) -> bool:
    lowered = error_text.lower()
    return "credit balance is too low" in lowered or (
        "credit balance" in lowered and "anthropic api" in lowered
    )


_PROMPT_FILE_PREFIX = "line-bot-claude-"
_STALE_PROMPT_FILE_SEC = 600
_CHILD_SECRET_ENV_RE = re.compile(
    r"API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.IGNORECASE
)


def _remove_stale_prompt_files() -> None:
    """Delete system-prompt files a killed worker left behind (they hold facts)."""
    cutoff = time.time() - _STALE_PROMPT_FILE_SEC
    try:
        for path in Path(tempfile.gettempdir()).glob(_PROMPT_FILE_PREFIX + "*.txt"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
    except OSError:
        pass


# 2026-10-04: the account's usage limit (shared with Andrew's own Claude Code)
# was classified "exit 1 (other)": no cooldown, a failed call for every
# message.  Notices seen: "Claude AI usage limit reached|<epoch>", "5-hour
# limit reached ∙ resets 3pm", "Weekly limit reached ∙ resets Oct 9, 3pm",
# "You've hit your limit · resets 11pm (Asia/Taipei)".  stdout can echo chat
# or model text, so it only counts as a short line that is nothing but such a
# notice, and a reset epoch is read from stderr alone (a 7-day cooldown from
# echoed text would silence Claude for a week).
_CLI_LIMIT_RE = re.compile(
    r"usage limit|limit reached|hit your limit|(?:5-hour|weekly|session|opus) limit|/upgrade",
    re.IGNORECASE,
)
_CLI_LIMIT_RESETS_RE = re.compile(r"\bresets?\b", re.IGNORECASE)
_CLI_LIMIT_LINE_RE = re.compile(
    r"(?:claude ai )?usage limit reached(?:\s*\|\s*\d{9,11})?"
    r"|(?:5-hour|weekly|session|opus|sonnet)(?: usage)? limit reached\b.{0,80}"
    r"|you(?:'|’)ve hit your (?:usage )?limit\b.{0,80}",
    re.IGNORECASE,
)
_CLI_LIMIT_LINE_MAX_CHARS = 160
_CLI_LIMIT_EPOCH_RE = re.compile(r"limit reached\s*\|\s*(\d{10})\b", re.IGNORECASE)
_CLI_LIMIT_MIN_SEC = 600
_CLI_LIMIT_MAX_SEC = 7 * 86400


def _stderr_reports_limit(stderr: str) -> bool:
    text = stderr or ""
    if _CLI_LIMIT_RE.search(text):
        return True
    return "limit" in text.lower() and _CLI_LIMIT_RESETS_RE.search(text) is not None


def _is_limit_notice_line(line: str) -> bool:
    line = (line or "").strip()
    return (
        0 < len(line) <= _CLI_LIMIT_LINE_MAX_CHARS
        and _CLI_LIMIT_LINE_RE.fullmatch(line) is not None
    )


def _cli_limit_until(stderr: str, now: float) -> float:
    """Retry time: the epoch the CLI printed on stderr (10 min–7 days), else
    ``CLAUDE_CLI_LIMIT_COOLDOWN_SEC`` from now."""
    match = _CLI_LIMIT_EPOCH_RE.search(stderr or "")
    if match:
        until = float(match.group(1))
        return min(max(until, now + _CLI_LIMIT_MIN_SEC), now + _CLI_LIMIT_MAX_SEC)
    return now + max(60, int(getattr(settings, "claude_cli_limit_cooldown_sec", 1800)))


def _cli_failure_kind(detail: str) -> str:
    lowered = (detail or "").lower()
    if any(k in lowered for k in ("log in", "login", "auth", "unauthorized", "401", "403")):
        return "auth"
    if "model" in lowered:
        return "model"
    if "unknown option" in lowered or "error: option" in lowered:
        return "usage"
    return "other"


# 2026-10-07: a hung CLI cost every message the full CLAUDE_CLI_TIMEOUT_SEC
# before Gemini was tried.  After this many timeouts in a row the CLI rests.
# Only a successful call ends the streak or a rest (an error or a usage limit
# in between does not), so one more timeout after a rest starts the next one.
# In memory and apart from the quota gate: a timeout says nothing about
# quota, and a restart simply tries the CLI again.
_CLI_TIMEOUT_BREAKER_THRESHOLD = 2
_CLI_TIMEOUT_BREAKER_COOLDOWN_SEC = 600
_CLI_BREAKER_LOCK = threading.Lock()
_cli_timeout_streak = 0
_cli_timeout_breaker_until = 0.0


def _cli_timeout_breaker_remaining() -> float:
    """Seconds the CLI still rests after repeated timeouts (0 = it may run)."""
    return max(0.0, _cli_timeout_breaker_until - time.time())


def _record_cli_outcome(started: float, outcome: str) -> None:
    """Log one CLI call's seconds and outcome, and feed the timeout breaker.

    ``outcome`` is ok / empty / timeout / error / limit, never CLI output
    (it can echo family chat).
    """
    global _cli_timeout_streak, _cli_timeout_breaker_until
    logger.info("claude cli secs=%.2f outcome=%s", time.monotonic() - started, outcome)
    if outcome in ("ok", "empty"):
        with _CLI_BREAKER_LOCK:
            _cli_timeout_streak = 0
            _cli_timeout_breaker_until = 0.0
    elif outcome == "timeout":
        with _CLI_BREAKER_LOCK:
            _cli_timeout_streak += 1
            streak = _cli_timeout_streak
            if streak >= _CLI_TIMEOUT_BREAKER_THRESHOLD:
                _cli_timeout_breaker_until = time.time() + _CLI_TIMEOUT_BREAKER_COOLDOWN_SEC
        if streak >= _CLI_TIMEOUT_BREAKER_THRESHOLD:
            logger.warning(
                "Claude CLI timed out %d times in a row; timeout breaker open for %ds",
                streak,
                _CLI_TIMEOUT_BREAKER_COOLDOWN_SEC,
            )


def _chat_via_cli(
    user_input: Any,
    context: list[tuple[str, str]],
    facts: list[str],
    persona_notes: list[dict] | None,
) -> str | None:
    executable = _cli_executable()
    if not executable:
        raise ClaudeCliUnavailable("claude CLI not found")
    prompt_parts = _build_cli_prompt(user_input, context, facts, persona_notes)
    if prompt_parts is None:
        return None
    system_prompt, user_prompt = prompt_parts
    # Force account-session auth. An API key in the child environment would
    # make Claude Code charge the API account instead of Settings > Usage; the
    # bot's other credentials (Gemini, LINE, Discord…) are no business of it.
    child_env = {
        name: value
        for name, value in os.environ.items()
        if not _CHILD_SECRET_ENV_RE.search(name)
    }
    # 2026-10-07: each reply starts a fresh CLI, which needs no update check,
    # telemetry or error reports on the way.  A value set on purpose (e.g. in
    # the launchd plist) wins; Andrew's own sessions still update the install.
    child_env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    child_env.setdefault("DISABLE_AUTOUPDATER", "1")
    timeout = max(10, int(settings.claude_cli_timeout_sec))
    # 2026-09-26: without these flags `claude -p` loaded Andrew's own Claude Code
    # setup — default opus model, hooks, CLAUDE.md, MCP servers — and replies
    # timed out after 60 s.  --safe-mode drops customisations but keeps the
    # account login (--bare would demand an API key); --setting-sources "" skips
    # user/project/local settings.  Chat text goes through stdin and a 0600 file,
    # never argv, and the session is not saved under ~/.claude.
    _remove_stale_prompt_files()
    system_prompt_path = ""
    started = time.monotonic()
    try:
        fd, system_prompt_path = tempfile.mkstemp(prefix=_PROMPT_FILE_PREFIX, suffix=".txt")
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            handle.write(system_prompt)
        completed = subprocess.run(
            [
                executable,
                "-p",
                "--system-prompt-file",
                system_prompt_path,
                "--model",
                settings.claude_cli_model,
                "--effort",
                "low",
                "--output-format",
                "text",
                "--safe-mode",
                "--setting-sources",
                "",
                "--strict-mcp-config",
                "--tools",
                "",
                "--no-session-persistence",
            ],
            input=user_prompt,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=child_env,
        )
    except subprocess.TimeoutExpired as exc:
        _record_cli_outcome(started, "timeout")
        raise ClaudeProviderError(f"CLI timeout after {timeout}s") from exc
    except (OSError, ValueError) as exc:
        _record_cli_outcome(started, "error")
        raise ClaudeCliUnavailable(type(exc).__name__) from exc
    finally:
        if system_prompt_path:
            try:
                os.unlink(system_prompt_path)
            except OSError as exc:
                logger.warning("claude prompt file not removed error_type=%s", type(exc).__name__)
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if completed.returncode != 0:
        # The raw output can echo the conversation; logs and state get a category.
        if _stderr_reports_limit(stderr) or any(
            _is_limit_notice_line(line) for line in stdout.splitlines()
        ):
            _record_cli_outcome(started, "limit")
            raise ClaudeCliLimitReached(
                f"CLI usage limit (exit {completed.returncode})",
                until=_cli_limit_until(stderr, time.time()),
            )
        detail = (stderr or stdout or "CLI failed").strip()
        if _quota_error(completed.returncode, detail):
            _record_cli_outcome(started, "limit")
            raise ClaudeQuotaExhausted(f"CLI quota or rate limit (exit {completed.returncode})")
        _record_cli_outcome(started, "error")
        raise ClaudeProviderError(
            f"CLI exit {completed.returncode} ({_cli_failure_kind(detail)})"
        )
    text = stdout.strip()
    if _is_limit_notice_line(text):
        # Printed as if it were the answer: never send it to the family.
        _record_cli_outcome(started, "limit")
        raise ClaudeCliLimitReached(
            "CLI usage limit (exit 0)", until=_cli_limit_until(stderr, time.time())
        )
    if is_empty_marker(text):
        # A clean exit with nothing printed is Claude following
        # NO_REPEAT_CONTRACT ("nothing new → empty string"), not a failure.
        _record_cli_outcome(started, "empty")
        logger.info("primary reply provider=claude-cli chose not to reply")
        return ""
    _record_cli_outcome(started, "ok")
    logger.info("primary reply provider=claude-cli")
    return text


def _request(payload: dict[str, Any]) -> dict[str, Any]:
    key = settings.claude_api_key.strip()
    if not key:
        raise ClaudeProviderError("Claude API key is not configured")
    request = urllib.request.Request(
        _API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": _ANTHROPIC_VERSION,
        },
        method="POST",
    )
    try:
        timeout = max(5, int(settings.claude_request_timeout_sec))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1200]
        if _quota_error(exc.code, body):
            raise ClaudeQuotaExhausted(f"HTTP {exc.code}: {body}") from exc
        raise ClaudeProviderError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ClaudeProviderError(type(exc).__name__) from exc
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ClaudeProviderError("invalid JSON response") from exc
    if not isinstance(data, dict):
        raise ClaudeProviderError("unexpected response shape")
    return data


def _response_text(data: dict[str, Any]) -> str:
    blocks = data.get("content", [])
    if not isinstance(blocks, list):
        return ""
    return "\n".join(
        str(block.get("text", ""))
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
    ).strip()


def chat(
    user_input: Any,
    context: list[tuple[str, str]],
    facts: list[str],
    persona_notes: list[dict] | None = None,
) -> str | None:
    """Try Claude once; return ``None`` to let the caller use Gemini."""
    if settings.claude_use_cli or _prefer_cli():
        # 2026-09-27: a quota error from the CLI cools it down too.
        if quota_exhausted():
            logger.info("Claude quota gate active; using Gemini")
            return None
        resting = _cli_timeout_breaker_remaining()
        if resting > 0:
            logger.info("Claude CLI timeout breaker open (%.0fs left); using Gemini", resting)
            return None
        try:
            result = _chat_via_cli(user_input, context, facts, persona_notes)
        except ClaudeQuotaExhausted as exc:
            _mark_quota_exhausted(str(exc), until=getattr(exc, "until", None))
            logger.warning("Claude CLI quota gate opened (%s); using Gemini", exc)
            return None
        except ClaudeCliUnavailable:
            if not settings.claude_api_key:
                return None
        except ClaudeProviderError as exc:
            logger.warning("Claude CLI request failed; using Gemini (%s)", exc)
            return None
        else:
            if result is None:  # unsupported media for the text-only CLI
                return None
            _remember_cli_preference()
            # "" = nothing new to add; the caller stays silent instead of
            # asking Gemini to say something anyway.
            return result

    if not settings.claude_api_key:
        return None
    if quota_exhausted():
        logger.info("Claude quota gate active; using Gemini")
        return None
    payload = _build_payload(user_input, context, facts, persona_notes)
    if payload is None:
        logger.info("Claude input contains unsupported media; using Gemini")
        return None
    try:
        data = _request(payload)
        text = _response_text(data)
    except ClaudeQuotaExhausted as exc:
        error_text = str(exc)
        if _api_credit_empty(error_text):
            try:
                result = _chat_via_cli(user_input, context, facts, persona_notes)
            except ClaudeCliUnavailable:
                pass
            except ClaudeQuotaExhausted as cli_exc:
                _mark_quota_exhausted(str(cli_exc), until=getattr(cli_exc, "until", None))
                logger.warning("Claude CLI quota gate opened (%s); using Gemini", cli_exc)
                return None
            except ClaudeProviderError as cli_exc:
                logger.warning("Claude CLI fallback failed; using Gemini (%s)", cli_exc)
                return None
            else:
                if result is not None:
                    _remember_cli_preference()
                    return result
        _mark_quota_exhausted(error_text)
        logger.warning("Claude API quota/credit gate opened; using Gemini")
        return None
    except ClaudeProviderError as exc:
        logger.warning("Claude request failed; using Gemini (%s)", exc)
        return None
    if is_empty_marker(text):
        if isinstance(data, dict) and data.get("stop_reason") == "end_turn":
            # Finished normally with nothing new to add: stay silent.
            _clear_quota_exhausted()
            logger.info("primary reply provider=claude model=%s chose not to reply", settings.claude_model)
            return ""
        logger.warning("Claude returned empty output; using Gemini")
        return None
    _clear_quota_exhausted()
    logger.info("primary reply provider=claude model=%s", settings.claude_model)
    return text
