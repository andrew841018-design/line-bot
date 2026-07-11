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
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from config import settings

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


def _mark_quota_exhausted(reason: str) -> None:
    cooldown = max(60, int(settings.claude_quota_cooldown_sec))
    _save_state(
        {
            "quota_exhausted_until": time.time() + cooldown,
            "reason": reason[:160],
            "updated_at": time.time(),
        }
    )


def _clear_quota_exhausted() -> None:
    if _load_state().get("quota_exhausted_until"):
        _save_state({})


def _prefer_cli() -> bool:
    return bool(_load_state().get("prefer_cli"))


def _remember_cli_preference() -> None:
    _save_state({"prefer_cli": True, "updated_at": time.time()})


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


def _merge_history(context: list[tuple[str, str]] | None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for role, text in (context or []):
        if not isinstance(text, str) or not text.strip():
            continue
        normalized = "assistant" if role == "assistant" else "user"
        if messages and messages[-1]["role"] == normalized:
            messages[-1]["content"] += "\n" + text
        else:
            messages.append({"role": normalized, "content": text})
    return messages


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

    system = gemini_client._build_system_instruction(
        facts,
        persona_notes,
        user_input=user_input,
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

    system = gemini_client._build_system_instruction(
        facts,
        persona_notes,
        user_input=user_input,
    )
    history = _merge_history(context)
    history_lines = []
    for message in history:
        role = "使用者" if message["role"] == "user" else "咪寶"
        history_lines.append(f"{role}：{message['content']}")
    history_block = "\n".join(history_lines) or "（沒有先前對話）"
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
    child_env = os.environ.copy()
    # Force account-session auth. An API key in the child environment would
    # make Claude Code charge the API account instead of Settings > Usage.
    for name in (
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
        "AGENT_CLAUDE_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    ):
        child_env.pop(name, None)
    timeout = max(10, int(settings.claude_cli_timeout_sec))
    try:
        completed = subprocess.run(
            [
                executable,
                "-p",
                user_prompt,
                "--system-prompt",
                system_prompt,
                "--effort",
                "low",
                "--output-format",
                "text",
            ],
            check=False,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            env=child_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeProviderError(f"CLI timeout after {timeout}s") from exc
    except OSError as exc:
        raise ClaudeCliUnavailable(type(exc).__name__) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "CLI failed").strip()
        if _quota_error(completed.returncode, detail):
            raise ClaudeQuotaExhausted(detail[:1200])
        raise ClaudeProviderError(detail[:300])
    text = (completed.stdout or "").strip()
    if not text:
        raise ClaudeProviderError("CLI returned empty output")
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
        try:
            result = _chat_via_cli(user_input, context, facts, persona_notes)
        except ClaudeQuotaExhausted as exc:
            _mark_quota_exhausted(str(exc))
            logger.warning("Claude CLI quota gate opened; using Gemini")
            return None
        except ClaudeCliUnavailable:
            if not settings.claude_api_key:
                return None
        except ClaudeProviderError as exc:
            logger.warning("Claude CLI request failed; using Gemini (%s)", exc)
            return None
        else:
            if result:
                _remember_cli_preference()
                return result
            return None

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
        text = _response_text(_request(payload))
    except ClaudeQuotaExhausted as exc:
        error_text = str(exc)
        if _api_credit_empty(error_text):
            try:
                result = _chat_via_cli(user_input, context, facts, persona_notes)
            except ClaudeCliUnavailable:
                pass
            except ClaudeQuotaExhausted as cli_exc:
                _mark_quota_exhausted(str(cli_exc))
                logger.warning("Claude CLI quota gate opened; using Gemini")
                return None
            except ClaudeProviderError as cli_exc:
                logger.warning("Claude CLI fallback failed; using Gemini (%s)", cli_exc)
                return None
            else:
                if result:
                    _remember_cli_preference()
                    return result
        _mark_quota_exhausted(error_text)
        logger.warning("Claude API quota/credit gate opened; using Gemini")
        return None
    except ClaudeProviderError as exc:
        logger.warning("Claude request failed; using Gemini (%s)", exc)
        return None
    if not text:
        logger.warning("Claude returned empty output; using Gemini")
        return None
    _clear_quota_exhausted()
    logger.info("primary reply provider=claude model=%s", settings.claude_model)
    return text
