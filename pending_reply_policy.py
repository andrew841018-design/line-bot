"""Shared switch for the legacy pending reply queue.

Keep this module dependency-free: scheduled jobs and preflight import it
without importing the FastAPI app in main.py.
"""

PENDING_REPLY_ENABLED = False


def pending_reply_enabled() -> bool:
    return PENDING_REPLY_ENABLED
