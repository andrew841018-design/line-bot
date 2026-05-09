"""Extract LINE conversation history from SQLite for fine-tuning dataset.

100% local — only reads `line_bot.db`. No network / cloud calls.

Public API:
  - extract_all_pairs(db_path) -> list[dict]
      Pull (user, bot) adjacent pairs from `context` table.
      Filters: role='user' followed by role='bot', length-bounded, bot reply
      not flagged by gemini_client._violates_quality, no pure-emoji / pure-URL,
      SHA256-deduped on (prompt, completion).

  - extract_with_corrections(db_path) -> list[dict]
      Pull pairs where the user's NEXT turn is a correction of the bot's reply.
      Sourced from persona_notes (kind='correction', source='organic'). Higher
      quality signal — user explicitly fixed the bot.

  - augment_with_negatives(db_path) -> list[dict]
      DPO-style negative samples: pair the rejected (bad) bot reply with the
      user's corrected version. Each entry has {prompt, chosen, rejected}.

Privacy:
  - group_id is SHA256-hashed before being placed into output dicts.
  - No user_id / display name is ever included.
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Allow `from gemini_client import _violates_quality` when run from line_bot/.
HERE = Path(__file__).resolve().parent
LINE_BOT_ROOT = HERE.parent
if str(LINE_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(LINE_BOT_ROOT))

logger = logging.getLogger("extract_line_history")

# ── Filter thresholds ────────────────────────────────────────────────────────
MIN_USER_LEN = 3
MIN_BOT_LEN = 5
MAX_USER_LEN = 2000
MAX_BOT_LEN = 4000

# Pure-emoji / sticker / URL detection
_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F"
    r"\U0001F0A0-\U0001F0FF✀-➿]+"
)
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_PUNCT_ONLY_RE = re.compile(r"^[\s\W_]+$")
_BURST_PREFIX_RE = re.compile(r"^\[burst\]\s*", re.IGNORECASE)
# LINE sticker placeholder leaks
_STICKER_RE = re.compile(r"^\s*(\(sticker\)|\(emoji\)|\[sticker\]|\[image\])\s*$", re.IGNORECASE)


# ── Hashing helpers ──────────────────────────────────────────────────────────
def _hash_group_id(group_id: str | None) -> str:
    if not group_id:
        return ""
    return hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:16]


def _pair_key(prompt: str, completion: str) -> str:
    """SHA256 over (prompt + completion) for dedup."""
    blob = f"{prompt}│{completion}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ── Text quality ─────────────────────────────────────────────────────────────
def _clean_text(text: str | None) -> str:
    if not text:
        return ""
    return _BURST_PREFIX_RE.sub("", text).strip()


def _is_pure_emoji_or_sticker(text: str) -> bool:
    """text 在去除 emoji / 標點後沒剩 → 純表情貼圖。"""
    if _STICKER_RE.match(text):
        return True
    stripped = _EMOJI_RE.sub("", text).strip()
    if not stripped:
        return True
    if _PUNCT_ONLY_RE.match(stripped):
        return True
    return False


def _is_pure_url(text: str) -> bool:
    """整段就只是 URL（去掉 URL 後沒剩字）。"""
    stripped = _URL_RE.sub("", text).strip()
    return len(stripped) == 0 and bool(_URL_RE.search(text))


def _is_quality_user(text: str) -> bool:
    if not text:
        return False
    n = len(text)
    if n < MIN_USER_LEN or n > MAX_USER_LEN:
        return False
    if _is_pure_emoji_or_sticker(text):
        return False
    if _is_pure_url(text):
        return False
    return True


def _violates(reply: str, user_text: str = "") -> bool:
    """Wrap gemini_client._violates_quality; return True if bot reply violates rules."""
    try:
        from gemini_client import _violates_quality  # type: ignore[attr-defined]
    except Exception as e:  # gemini_client may be heavy / unavailable in tests
        logger.debug("could not import _violates_quality: %s", e)
        return False
    try:
        violates, _reason = _violates_quality(reply, user_text)
        return bool(violates)
    except Exception as e:
        logger.debug("_violates_quality raised: %s", e)
        return False


def _is_quality_bot(text: str, user_text: str = "") -> bool:
    if not text:
        return False
    n = len(text)
    if n < MIN_BOT_LEN or n > MAX_BOT_LEN:
        return False
    if _is_pure_emoji_or_sticker(text):
        return False
    if _is_pure_url(text):
        return False
    if _violates(text, user_text):
        return False
    return True


# ── Timestamp join (best-effort via raw_messages) ────────────────────────────
def _build_ts_lookup(con: sqlite3.Connection) -> dict[str, int]:
    """Approximate per-group timestamp by min(created_at) of raw_messages.

    `context` table has no timestamp column. We bucket by group_id and use the
    earliest seen raw_messages timestamp as a stable anchor. Best-effort —
    returns empty dict if raw_messages absent.
    """
    out: dict[str, int] = {}
    try:
        rows = con.execute(
            "SELECT group_id, MIN(created_at) FROM raw_messages GROUP BY group_id"
        ).fetchall()
        for gid, ts in rows:
            if gid and ts is not None:
                out[gid] = int(ts)
    except sqlite3.Error:
        pass
    return out


# ── Main extractors ──────────────────────────────────────────────────────────
def extract_all_pairs(db_path: str | Path) -> list[dict[str, Any]]:
    """Pull (user, bot) adjacent pairs from BOTH `context` AND `raw_messages` tables.

    Two sources merged + dedup:
      1. `context` (curated, role='user'/'bot') — 既有
      2. `raw_messages` (full webhook log, user_id='__bot__' = bot) — 新加，數據量 ~40x

    raw_messages has timestamps so per-pair created_at 真實值。
    """
    db_path = Path(db_path)
    if not db_path.exists():
        logger.warning("db not found: %s", db_path)
        return []

    seen: set[str] = set()
    pairs: list[dict[str, Any]] = []

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        # ── Source 1: context table（既有） ──
        rows = con.execute(
            "SELECT group_id, seq, role, text FROM context ORDER BY group_id, seq"
        ).fetchall()
        ts_lookup = _build_ts_lookup(con)
        by_group: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            by_group.setdefault(r["group_id"], []).append(r)
        for gid, seq_rows in by_group.items():
            ghash = _hash_group_id(gid)
            gts = ts_lookup.get(gid, 0)
            i = 0
            while i < len(seq_rows) - 1:
                cur, nxt = seq_rows[i], seq_rows[i + 1]
                if (
                    cur["role"] == "user"
                    and nxt["role"] == "bot"
                    and nxt["seq"] == cur["seq"] + 1
                ):
                    u = _clean_text(cur["text"])
                    b = _clean_text(nxt["text"])
                    if _is_quality_user(u) and _is_quality_bot(b, u):
                        key = _pair_key(u, b)
                        if key not in seen:
                            seen.add(key)
                            pairs.append({
                                "prompt": u,
                                "completion": b,
                                "group_id": ghash,
                                "timestamp": gts,
                                "source": "organic",
                                "pair_hash": key,
                            })
                    i += 2
                else:
                    i += 1

        # ── Source 2: raw_messages（新加，40x 數據量） ──
        # user_id='__bot__' = bot reply；其他 = user
        raw_rows = con.execute(
            "SELECT group_id, message_id, user_id, text, created_at "
            "FROM raw_messages ORDER BY group_id, created_at"
        ).fetchall()
        by_group_raw: dict[str, list[sqlite3.Row]] = {}
        for r in raw_rows:
            by_group_raw.setdefault(r["group_id"], []).append(r)
        for gid, ts_rows in by_group_raw.items():
            ghash = _hash_group_id(gid)
            i = 0
            while i < len(ts_rows) - 1:
                cur, nxt = ts_rows[i], ts_rows[i + 1]
                cur_is_user = cur["user_id"] != "__bot__"
                nxt_is_bot = nxt["user_id"] == "__bot__"
                if cur_is_user and nxt_is_bot:
                    u = _clean_text(cur["text"])
                    b = _clean_text(nxt["text"])
                    if _is_quality_user(u) and _is_quality_bot(b, u):
                        key = _pair_key(u, b)
                        if key not in seen:
                            seen.add(key)
                            pairs.append({
                                "prompt": u,
                                "completion": b,
                                "group_id": ghash,
                                "timestamp": int(nxt["created_at"]),
                                "source": "organic",
                                "pair_hash": key,
                            })
                    i += 2
                else:
                    i += 1
    finally:
        con.close()

    return pairs


def extract_with_corrections(db_path: str | Path) -> list[dict[str, Any]]:
    """Pull (user, corrected_reply) pairs from persona_notes source='organic'.

    For each organic correction we try to recover:
      - prompt: the user's original message (extracted from note content if
        present, else fallback to scenario)
      - completion: the corrected reply suggested by the user

    Returns list with same shape as extract_all_pairs but source='correction'
    and an extra `priority='high'` flag.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        try:
            rows = con.execute(
                "SELECT group_id, scenario, content, created_at "
                "FROM persona_notes "
                "WHERE kind='correction' AND source='organic'"
            ).fetchall()
        except sqlite3.Error:
            return []
    finally:
        con.close()

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        prompt, completion = _parse_correction_note(r["scenario"], r["content"])
        if not prompt or not completion:
            continue
        if not _is_quality_user(prompt) or not _is_quality_bot(completion, prompt):
            continue
        key = _pair_key(prompt, completion)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "prompt": prompt,
            "completion": completion,
            "group_id": _hash_group_id(r["group_id"]),
            "timestamp": int(r["created_at"] or 0),
            "source": "correction",
            "priority": "high",
            "pair_hash": key,
        })
    return out


def augment_with_negatives(db_path: str | Path) -> list[dict[str, Any]]:
    """DPO-style negatives: (prompt, chosen=corrected, rejected=bad_reply).

    Reads persona_notes source='organic' and parses out the rejected bot reply
    plus the user's corrected version from the note `content`.

    Returns list of dicts:
      { "prompt", "chosen", "rejected", "group_id", "timestamp",
        "source": "dpo_correction", "pair_hash" }
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        try:
            rows = con.execute(
                "SELECT group_id, scenario, content, created_at "
                "FROM persona_notes "
                "WHERE kind='correction' AND source='organic'"
            ).fetchall()
        except sqlite3.Error:
            return []
    finally:
        con.close()

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        prompt, chosen, rejected = _parse_correction_triple(r["scenario"], r["content"])
        if not prompt or not chosen or not rejected:
            continue
        if not _is_quality_user(prompt):
            continue
        if not _is_quality_bot(chosen, prompt):
            continue
        # rejected reply intentionally NOT quality-checked — the whole point is
        # it's a rejected example.
        if len(rejected) < MIN_BOT_LEN or len(rejected) > MAX_BOT_LEN:
            continue
        key = _pair_key(prompt, chosen + "│" + rejected)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "prompt": prompt,
            "chosen": chosen,
            "rejected": rejected,
            "group_id": _hash_group_id(r["group_id"]),
            "timestamp": int(r["created_at"] or 0),
            "source": "dpo_correction",
            "pair_hash": key,
        })
    return out


# ── Note parsing helpers ─────────────────────────────────────────────────────
# persona_notes content uses a free-form schema. We support several formats
# emitted by main._detect_user_correction:
#   "user 訊息：<...>\nbot 原回覆：<...>\nuser 糾正：<...>"
#   "原回覆：<...>\n糾正：<...>"
#   plain text — treated as completion only.

_USER_LABELS = ("user 訊息：", "user：", "使用者：", "使用者訊息：")
_BOT_LABELS = ("bot 原回覆：", "bot：", "原回覆：", "bot 回覆：")
_FIX_LABELS = ("user 糾正：", "糾正：", "正確：", "應該是：")


def _extract_label(text: str, labels: tuple[str, ...]) -> str | None:
    for lab in labels:
        if lab in text:
            after = text.split(lab, 1)[1]
            # cut at next known label boundary or newline
            for nxt in _USER_LABELS + _BOT_LABELS + _FIX_LABELS:
                if nxt == lab:
                    continue
                if nxt in after:
                    after = after.split(nxt, 1)[0]
            return after.strip().rstrip("\n").strip()
    return None


def _parse_correction_note(scenario: str | None, content: str | None) -> tuple[str, str]:
    """Return (user_prompt, corrected_reply). Empty strings if not parseable."""
    scenario = scenario or ""
    content = content or ""

    user_msg = _extract_label(content, _USER_LABELS) or ""
    fixed = _extract_label(content, _FIX_LABELS) or ""

    # Fallback: scenario as prompt, content as completion
    if not user_msg and scenario:
        user_msg = scenario.strip()
    if not fixed:
        # if content isn't structured, treat entire content as the corrected reply
        fixed = content.strip() if not _BOT_LABELS_PRESENT(content) else ""

    return user_msg, fixed


def _parse_correction_triple(
    scenario: str | None, content: str | None
) -> tuple[str, str, str]:
    """Return (user_prompt, chosen=corrected, rejected=original_bad_reply)."""
    scenario = scenario or ""
    content = content or ""
    user_msg = _extract_label(content, _USER_LABELS) or scenario.strip()
    rejected = _extract_label(content, _BOT_LABELS) or ""
    chosen = _extract_label(content, _FIX_LABELS) or ""
    return user_msg, chosen, rejected


def _BOT_LABELS_PRESENT(content: str) -> bool:  # noqa: N802 — internal helper
    return any(lab in content for lab in _BOT_LABELS + _FIX_LABELS + _USER_LABELS)


__all__ = [
    "extract_all_pairs",
    "extract_with_corrections",
    "augment_with_negatives",
]
