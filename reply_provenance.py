"""Whether the reply being produced used a real search (2026-10-03).

Claude (CLI/API) and the local model have no search tool; Gemini answers with
Google Search grounding, and lite may gather evidence itself.  ``main._llm_chat``
resets this before each generation and the provider that actually searched
marks it, so the caller can tell a grounded 「查到」 from one made up from memory.
A ContextVar keeps concurrent burst/webhook threads apart.

Every generated reply is checked for a search nobody ran.  The burst, direct
@咪寶 and research paths know more (a shared link was read, the prompt carries
search results) and check the reply themselves, inside ``checked_by_caller()``.
When the shared check drops a reply, ``dropped()`` lets the caller finish the
message as intentionally silent instead of retrying it.

2026-10-04 (family feedback P3): the Gemini answer that is returned also
records what its search found (``record_grounding``): the model, the result
URLs, the reply segments the search supports and the queries.  A sentence
about a named person's health／death／legal event is kept only when such a
segment (or other evidence) backs it.  ``reset()`` clears this record too.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_searched: ContextVar[bool] = ContextVar("reply_searched", default=False)
_caller_checks: ContextVar[bool] = ContextVar("reply_caller_checks", default=False)
_dropped: ContextVar[bool] = ContextVar("reply_dropped", default=False)
_grounding: ContextVar[dict | None] = ContextVar("reply_search_details", default=None)


def reset() -> None:
    _searched.set(False)
    _dropped.set(False)
    _grounding.set(None)


def mark_searched() -> None:
    _searched.set(True)


def searched() -> bool:
    return _searched.get()


def mark_dropped() -> None:
    _dropped.set(True)


def dropped() -> bool:
    return _dropped.get()


@contextmanager
def checked_by_caller():
    """The caller checks the reply's search claims itself, with what it knows."""
    token = _caller_checks.set(True)
    try:
        yield
    finally:
        _caller_checks.reset(token)


def caller_checks() -> bool:
    return _caller_checks.get()


def record_grounding(info: dict) -> None:
    """Keep the search details of the Gemini response whose text is returned.

    ``info`` holds ``model``, ``urls``, ``supported_segments`` and ``queries``
    (a tool-less answer records empty lists).  Recording does not mark
    ``searched()``; the generation that took a grounded answer does that.
    """
    _grounding.set(dict(info))


def grounding() -> dict | None:
    """The recorded search details; None when no Gemini answer recorded any."""
    return _grounding.get()


def take_grounding() -> dict | None:
    """``grounding()``, then cleared: one answer's record backs one reply only.

    ``searched()`` and ``dropped()`` are left as they are.
    """
    info = _grounding.get()
    _grounding.set(None)
    return info


def is_grounded(info: dict | None) -> bool:
    """Whether ``info`` has reply segments that its search supports."""
    return bool(info and info.get("supported_segments"))
