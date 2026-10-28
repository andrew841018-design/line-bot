"""LINE Messaging API error helpers (2026-10-24).

Kept apart from main.py (23k lines) so tests and small scripts can use them without
importing the bot.  Nothing here touches the network, the database or the settings.

Two jobs:

* ``error_detail`` -- one log line for a failed LINE call: HTTP status, reason,
  ``x-line-request-id``, the body's ``message`` and the ``details[].property`` paths
  (``messages[2].text`` says which message LINE refused).  The reply token never appears,
  LINE ids (``U``/``C``/``R`` + 32 hex) and URLs are masked, and anything that is not an
  HTTP answer is reduced to its type name: the text of such an exception can carry request
  fields (the reply token in a validation error) or the family's words.
* ``http_status`` / ``is_client_error`` -- tell "LINE answered with a 4xx" (nothing was
  sent) from a timeout, a 5xx or a connection error (the message may have been delivered).
* ``is_bad_request`` / ``rejected_message_indexes`` -- a 400 whose ``details[].property``
  paths (``messages[2].text``) name the message(s) of the request LINE refused (2026-10-24).

Every pattern is a single character class with a plain quantifier, so matching is linear
(the H5 rule: no pattern that can backtrack).
"""

from __future__ import annotations

import json
import re

_LINE_ID_RE = re.compile(r"\b[UCR][0-9a-f]{32}\b")
_URL_RE = re.compile(r"https?://\S+")
_LONG_RUN_RE = re.compile(r"[A-Za-z0-9]{24,}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_REQUEST_ID_RE = re.compile(r"[0-9A-Za-z-]{1,64}")
_NOT_A_NAME_CHAR_RE = re.compile(r"[^A-Za-z0-9_.]+")

# `details[].property` of a refused message: "messages[2].text", "messages[0].substitution..."
# (one anchored match with a bounded quantifier: linear).
_MESSAGE_PROPERTY_RE = re.compile(r"messages\[(\d{1,4})\]")

_MAX_BODY_CHARS = 8000
_MAX_DETAILS_SCANNED = 100
_MAX_MESSAGE_CHARS = 200
_MAX_PROPERTY_CHARS = 60
_MAX_PROPERTIES = 8


def http_status(exc: BaseException) -> int | None:
    """The HTTP status an SDK exception carries, or None (timeout, connection error...)."""
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def is_client_error(exc: BaseException) -> bool:
    """LINE answered 4xx: it refused the request and delivered nothing."""
    status = http_status(exc)
    return status is not None and 400 <= status < 500


def is_bad_request(exc: BaseException) -> bool:
    """LINE answered 400: the request (or its reply token) was refused, nothing was sent."""
    return http_status(exc) == 400


def rejected_message_indexes(exc: BaseException) -> list[int]:
    """Indexes (into the request's ``messages`` array) that a LINE error body names in
    ``details[].property`` -- ``messages[2].text`` means the third message.  Sorted, without
    duplicates; empty when the body is not LINE's JSON or names no message (the caller then
    does not know which message was refused).  Nothing but integers is returned: no text of the
    body or of the chat reaches a caller or a log."""
    raw = _body_text(getattr(exc, "body", None))
    if not raw:
        return []
    try:
        doc = json.loads(raw)
    except Exception:
        return []
    details = doc.get("details") if isinstance(doc, dict) else None
    if not isinstance(details, list):
        return []
    found: set[int] = set()
    for item in details[:_MAX_DETAILS_SCANNED]:
        prop = item.get("property") if isinstance(item, dict) else None
        if isinstance(prop, str):
            match = _MESSAGE_PROPERTY_RE.match(prop)
            if match:
                found.add(int(match.group(1)))
    return sorted(found)


def _clean(value: object, limit: int, secrets: tuple[str, ...] = ()) -> str:
    text = str(value if value is not None else "")
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "<token>")
    text = _CONTROL_RE.sub(" ", text)
    text = _URL_RE.sub("<url>", text)
    text = _LINE_ID_RE.sub("<id>", text)
    text = _LONG_RUN_RE.sub("<tok>", text)
    return text.strip()[:limit]


def _request_id(exc: BaseException) -> str:
    headers = getattr(exc, "headers", None)
    value: object = ""
    try:
        if hasattr(headers, "get"):
            value = headers.get("x-line-request-id") or headers.get("X-Line-Request-Id") or ""
        elif headers:
            for key, item in headers:
                if str(key).lower() == "x-line-request-id":
                    value = item
                    break
    except Exception:
        value = ""
    value = str(value or "")
    return value if _REQUEST_ID_RE.fullmatch(value) else ""


def _body_text(body: object) -> str | None:
    if isinstance(body, (bytes, bytearray)):
        return bytes(body[:_MAX_BODY_CHARS]).decode("utf-8", "replace")
    if isinstance(body, str):
        return body[:_MAX_BODY_CHARS]
    return None


def error_detail(exc: BaseException, *, secrets: tuple[str, ...] = ()) -> str:
    """One log line for a failed LINE call (see the module docstring)."""
    status = http_status(exc)
    if status is None:
        return f"error_type={_NOT_A_NAME_CHAR_RE.sub('', type(exc).__name__)[:60]}"
    parts = [f"status={status}"]
    reason = _clean(getattr(exc, "reason", ""), 60, secrets)
    if reason:
        parts.append(f"reason={reason}")
    request_id = _request_id(exc)
    if request_id:
        parts.append(f"request_id={request_id}")
    raw = _body_text(getattr(exc, "body", None))
    if raw:
        message = ""
        properties: list[str] = []
        parsed = False
        try:
            doc = json.loads(raw)
            parsed = isinstance(doc, dict)
        except Exception:
            doc = None
        if parsed:
            message = _clean(doc.get("message"), _MAX_MESSAGE_CHARS, secrets)
            details = doc.get("details")
            if isinstance(details, list):
                for item in details[:_MAX_PROPERTIES]:
                    if isinstance(item, dict):
                        prop = _clean(item.get("property"), _MAX_PROPERTY_CHARS, secrets)
                        if prop:
                            properties.append(prop)
        if message:
            parts.append(f'message="{message}"')
        if properties:
            parts.append("props=" + ",".join(properties))
        if not parsed:
            # Not LINE's JSON (a proxy page, HTML...): its text is not ours to log.
            parts.append(f"body_len={len(raw)}")
    return " ".join(parts)
