"""Shared LINE push authentication helpers.

All push jobs should get their bearer token through this module.  It keeps
scheduled scripts on the refreshed v3 stateless token path and leaves the
legacy .env token only as a fallback.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

import output_validator

logger = logging.getLogger("line_push_client")

PUSH_URL = "https://api.line.me/v2/bot/message/push"


class LinePushError(RuntimeError):
    """Raised when a LINE push request was not accepted."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_text: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


def line_access_token(fallback_token: str | None = None) -> str:
    """Return a LINE token for push calls, preferring the refreshed cache."""
    try:
        from line_token_refresh import get_line_token

        token = get_line_token()
        if token:
            return token
    except Exception as e:
        logger.warning("LINE token refresh failed, fallback to legacy token: %s", str(e)[:120])

    if fallback_token is not None:
        return fallback_token

    try:
        from config import settings

        return settings.line_channel_access_token
    except Exception:
        return os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")


def line_configuration(fallback_token: str | None = None):
    """Build a LINE SDK Configuration with the current refreshed token."""
    from linebot.v3.messaging import Configuration

    return Configuration(access_token=line_access_token(fallback_token=fallback_token))


def validate_push_text(text: str, *, source: str = "line_push_client") -> str:
    """Return LINE-bound text or a safe replacement if validation blocks it."""
    result = output_validator.validate_outbound_text(text or "")
    if not result.ok:
        logger.warning(
            "outbound push validation blocked source=%s reason=%s preview=%r",
            source,
            result.reason,
            (text or "")[:160],
        )
    return result.text


def _validated_message_dict(message: dict, *, source: str) -> dict | None:
    msg = dict(message)
    msg_type = msg.get("type")
    text = msg.get("text")
    if msg_type not in {"text", "textV2"} or not isinstance(text, str):
        return msg

    safe_text = validate_push_text(text, source=source)
    if not safe_text.strip():
        logger.info("suppressed empty LINE push text source=%s", source)
        return None
    if msg_type == "textV2" and safe_text != text:
        return {"type": "text", "text": safe_text[:5000]}
    msg["text"] = safe_text[:5000]
    return msg


def _validated_messages(messages: Iterable[dict], *, source: str) -> list[dict]:
    out = []
    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        validated = _validated_message_dict(msg, source=source)
        if validated is not None:
            out.append(validated)
    return out


def push_messages(
    to: str,
    messages: Iterable[dict],
    *,
    timeout: int = 10,
    retry_key: str | None = None,
    session=None,
    ok_statuses: tuple[int, ...] = (200,),
    fallback_token: str | None = None,
    sent_message_ids: list[str] | None = None,
) -> bool:
    """Push LINE messages and raise LinePushError on non-accepted responses."""
    import requests

    group_id = (to or "").strip()
    if not group_id:
        raise LinePushError("missing LINE target group id")

    token = line_access_token(fallback_token=fallback_token)
    if not token:
        raise LinePushError("missing LINE access token")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if retry_key:
        headers["X-Line-Retry-Key"] = retry_key

    validated_messages = _validated_messages(messages, source="push_messages")
    if not validated_messages:
        logger.info("LINE push skipped because all messages were suppressed group=%s", group_id)
        raise LinePushError(
            "all LINE push messages were suppressed by outbound validation",
            status_code=400,
        )
    transport = session or requests
    try:
        resp = transport.post(
            PUSH_URL,
            headers=headers,
            json={"to": group_id, "messages": validated_messages},
            timeout=timeout,
        )
    except Exception as e:
        raise LinePushError(f"LINE push request failed: {e}") from e

    status = int(getattr(resp, "status_code", 0) or 0)
    if status in ok_statuses:
        if sent_message_ids is not None:
            try:
                payload = resp.json()
                sent = payload.get("sentMessages", []) if isinstance(payload, dict) else []
                for item in sent:
                    if not isinstance(item, dict):
                        continue
                    message_id = str(item.get("id") or "").strip()
                    if message_id:
                        sent_message_ids.append(message_id)
            except Exception:
                # Older/fake transports may not expose JSON. Delivery success
                # remains authoritative; missing archival metadata is non-fatal.
                pass
        return True

    body = (getattr(resp, "text", "") or "")[:500]
    raise LinePushError(
        f"LINE push HTTP {status}: {body[:300]}",
        status_code=status,
        response_text=body,
    )


def push_text(to: str, text: str, **kwargs) -> bool:
    """Push a single text message and raise LinePushError on failure."""
    return push_messages(
        to,
        [{"type": "text", "text": text[:5000]}],
        **kwargs,
    )


def try_push_messages(to: str, messages: Iterable[dict], **kwargs) -> bool:
    """Boolean wrapper for jobs that should keep their own control flow."""
    try:
        return push_messages(to, messages, **kwargs)
    except LinePushError as e:
        logger.warning("LINE push failed: %s", e)
        return False


def try_push_text(to: str, text: str, **kwargs) -> bool:
    """Boolean wrapper for a single text push."""
    try:
        return push_text(to, text, **kwargs)
    except LinePushError as e:
        logger.warning("LINE push failed: %s", e)
        return False
