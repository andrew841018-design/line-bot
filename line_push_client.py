"""Shared LINE push authentication helpers.

All push jobs should get their bearer token through this module.  It keeps
scheduled scripts on the refreshed v3 stateless token path and leaves the
legacy .env token only as a fallback.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
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


# ── 「提到人就真 @」 (Andrew 2026-11-12) ────────────────────────────────────────────────────────
# A push that names a family member @s them, so their phone notifies even when they muted the group
# (their LINE 「標註提醒」 must be on).  Plan and reviews: ops/state/line_bot_reminder_alert_20261112/.
# Rules this helper keeps:
# * The @ never costs delivery.  If anything about the @ is off (no usable target, the switch is off,
#   blank text, the validator would treat the body, the textV2 text or the plain version differently, or
#   this code raises before sending), it makes exactly today's call: the text, plain, under the
#   caller's retry key.  A prefix line can flip the validator either way: `{p1}` supplies the digit
#   its "latest data" rule wants, and an @ line in front stops its short-reply rules from matching.
# * LINE refusing the textV2 (a 4xx that is not 409 or 429) sends the plain version once, under its own
#   key.  A 429 (LINE refused it: the monthly quota) raises unchanged, and so do 5xx, timeouts and
#   connection errors (those may have gone out).
# * Both sends accept 409 (LINE already took that retry key).
# * Logs carry the source, the path, reason codes and line_errors.error_detail: never text, names or
#   ids.  Nothing is archived.

_MENTIONS_SWITCH = "LINE_BOT_PUSH_MENTIONS"
_MENTIONS_ON = frozenset({"", "1", "on", "true", "yes"})
_MENTIONS_OFF = frozenset({"0", "off", "false", "no"})
_MAX_MENTIONS = 20
_MENTION_KEY_RE = re.compile(r"[A-Za-z]+")
_PUSH_TEXT_LIMIT = 5000  # LINE counts UTF-16 units; the @ builders clip by those, today's call keeps push_messages' slice
_PUSH_OK = (200, 409)


def push_mentions_enabled() -> bool:
    """The switch, read on every call: unset, empty or 1/on/true/yes means on; anything else is off.

    A value it does not know (a typo such as `disable` or `關`) turns the @ off rather than leaving it on:
    this is the switch someone reaches for to stop whole-family pings."""
    value = os.environ.get(_MENTIONS_SWITCH, "").strip().lower()
    if value in _MENTIONS_ON:
        return True
    if value not in _MENTIONS_OFF:
        logger.warning("push mentions switch %s has a value it does not know: treating it as off", _MENTIONS_SWITCH)
    return False


def _usable_mention_targets(targets) -> list:
    """Filters what the caller built (it never makes a target: test_model_all_mention lists who does)."""
    kept: list = []
    keys: set[str] = set()
    user_ids: set[str] = set()
    for target in targets or ():
        key = str(getattr(target, "key", "") or "")
        if not _MENTION_KEY_RE.fullmatch(key) or key in keys:
            continue
        kind = getattr(target, "kind", "")
        if kind == "user":
            raw = str(getattr(target, "user_id", "") or "")
            user_id = raw.strip()
            if not user_id or user_id != raw or user_id == "__bot__" or user_id in user_ids:
                continue
            user_ids.add(user_id)
        elif kind != "all":
            continue
        keys.add(key)
        kept.append(target)
        if len(kept) >= _MAX_MENTIONS:
            break
    return kept


def push_text_with_mentions(
    to: str,
    text: str,
    targets=(),
    *,
    retry_key: str,
    timeout: int = 10,
    session=None,
    source: str = "push",
) -> bool:
    """Push ``text``, with a first line that @s ``targets`` when that is safe (see the block comment).

    True when LINE accepted (200, or 409 for a retry key it already took); otherwise LinePushError is
    raised, as ``push_messages`` does; an empty ``retry_key`` raises ValueError before anything is sent.
    Never returns False."""
    if not retry_key:
        raise ValueError("push_text_with_mentions needs a retry_key")
    body = text or ""
    template: dict | None = None
    plain = ""
    reason = "no_targets"
    try:
        kept = _usable_mention_targets(targets)
        if not kept:
            reason = "no_targets"
        elif not push_mentions_enabled():
            reason = "switched_off"
        elif not body.strip():
            reason = "blank_text"
        else:
            import line_mentions

            message = line_mentions.text_v2_dict(body, kept, limit=_PUSH_TEXT_LIMIT)
            plain_text = line_mentions.clip_utf16(
                line_mentions.text_with_plain_mentions(body, kept), _PUSH_TEXT_LIMIT
            )
            blocked = []
            for candidate in (body, message["text"], plain_text):
                result = output_validator.validate_outbound_text(candidate)
                if not result.ok or result.text != candidate:
                    blocked.append(str(result.reason or "changed"))
            if blocked:
                reason = "validator:" + ",".join(sorted(set(blocked)))
            else:
                template, plain = message, plain_text
    except Exception as exc:
        reason = f"error:{type(exc).__name__}"
        template = None
    if template is None:
        if reason != "no_targets":
            logger.info("push mentions skipped source=%s reason=%s", source, reason)
        return push_messages(
            to,
            [{"type": "text", "text": body[:_PUSH_TEXT_LIMIT]}],
            retry_key=retry_key,
            timeout=timeout,
            ok_statuses=_PUSH_OK,
            session=session,
        )
    try:
        return push_messages(
            to, [template], retry_key=retry_key, timeout=timeout, ok_statuses=_PUSH_OK, session=session
        )
    except LinePushError as exc:
        import line_errors

        status = line_errors.http_status(exc)
        if status is None or not 400 <= status < 500 or status in (409, 429):
            raise
        logger.warning("push mentions refused source=%s %s; sending plain", source, line_errors.error_detail(exc))
    return push_messages(
        to,
        [{"type": "text", "text": plain}],
        retry_key=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{retry_key}:plain")),
        timeout=timeout,
        ok_statuses=_PUSH_OK,
        session=session,
    )


def try_push_text_with_mentions(
    to: str,
    text: str,
    targets=(),
    *,
    retry_key: str,
    timeout: int = 10,
    session=None,
    source: str = "push",
) -> bool:
    """Boolean wrapper for jobs that loop over groups or rows: any failure is False, logged without text."""
    try:
        return bool(
            push_text_with_mentions(
                to, text, targets, retry_key=retry_key, timeout=timeout, session=session, source=source
            )
        )
    except Exception as exc:
        try:
            import line_errors

            detail = line_errors.error_detail(exc)
        except Exception:
            detail = f"error_type={type(exc).__name__}"
        logger.warning("LINE push failed source=%s %s", source, detail)
        return False
