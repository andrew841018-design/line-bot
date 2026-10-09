"""Reminder push — LINE 群組獨立提醒排程（2026-05-08 建立）。

每 15 分鐘 launchd 觸發。階梯式 push schedule（用戶要求 2026-05-08）：

  7-30 days：每週推一次（last_weekly_at >= 6.5 days ago）
  > 30 days：太遠，不主動推
  4-6 days：dead zone 不推
  ~ 3 days：推 1 次（pushed_3d = 1）
  ~ 1 day：推 1 次（pushed_1d = 1）
  4 hr 前：推 1 次（pushed_4hr = 1）
  2 hr 前：推 1 次（pushed_2hr = 1）
  1 hr 前：推 1 次（pushed_1hr = 1）
  到時：推 1 次 + mark done（pushed_now = 1, status = 'done'）

每階段都有 flag 防重複 push。launchd 每 15 分鐘跑一次提供精度 ±7.5 min。

跟 daily_briefing_discord 完全獨立（不走 Discord）。
"""

from __future__ import annotations

import argparse
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from linebot.v3.messaging import (
    ApiClient, Configuration, MessagingApi, PushMessageRequest, TextMessage,
)

import memory
import line_mentions
import reminder_intent
import reminder_overview
import reminder_stages
from line_push_client import line_access_token, validate_push_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("reminder_push")
STALE_PENDING_GRACE_SECONDS = 3600
_TW = ZoneInfo("Asia/Taipei")


def _line_access_token() -> str:
    return line_access_token()


def _push_to_group(
    group_id: str,
    text: str,
    max_retries: int = 3,
    message: object | None = None,
    reminder_id: int | None = None,
    source_kind: str = "",
    source_ref: str = "",
    retry_key: str | None = None,
) -> bool:
    """推到 LINE 群組。失敗回 False。

    2026-05-29 加 retry 防 transient 5xx（GP2#2）：
      - 429（monthly quota / rate limit）：不重試，立即 False
      - 其他 exception（5xx / network）：exponential backoff (1s/2s/4s) 重試
    """
    safe_text = validate_push_text(text, source="reminder_push")
    if not safe_text.strip():
        logger.info("reminder push suppressed empty text group=%s", group_id)
        return False
    if safe_text != text:
        message = TextMessage(text=safe_text[:4900])
    cfg = Configuration(access_token=_line_access_token())
    for attempt in range(max_retries):
        try:
            with ApiClient(cfg) as api_client:
                request = PushMessageRequest(
                    to=group_id,
                    messages=[message or TextMessage(text=safe_text[:4900])],
                )
                api = MessagingApi(api_client)
                response = (
                    api.push_message(request, x_line_retry_key=retry_key)
                    if retry_key
                    else api.push_message(request)
                )
            # LINE 已接受後，archive 失敗不可觸發 retry，否則會重複推播。
            try:
                for sent in getattr(response, "sent_messages", None) or []:
                    sent_id = getattr(sent, "id", None)
                    if sent_id:
                        memory.log_raw_message(
                            group_id,
                            str(sent_id),
                            "__bot__",
                            safe_text,
                        )
                        if reminder_id is not None or (
                            source_kind and source_ref
                        ):
                            reference_kwargs: dict[str, object] = {
                                "reminder_id": (
                                    int(reminder_id)
                                    if reminder_id is not None
                                    else None
                                )
                            }
                            if source_kind and source_ref:
                                reference_kwargs.update(
                                    {
                                        "source_kind": source_kind,
                                        "source_ref": source_ref,
                                    }
                                )
                            memory.log_sent_reminder_reference(
                                group_id,
                                str(sent_id),
                                **reference_kwargs,
                            )
            except Exception as archive_error:
                logger.error(
                    "reminder push delivered but sent-message archive failed "
                    "group=%s: %s",
                    group_id,
                    str(archive_error)[:200],
                )
            return True
        except Exception as e:
            err_str = str(e)
            status = getattr(e, "status", None)
            if status is None:
                status = getattr(e, "status_code", None)
            if status == 409:
                # A deterministic X-Line-Retry-Key returning 409 means LINE
                # accepted the same logical delivery earlier.
                logger.info(
                    "LINE push retry key already accepted (group=%s)",
                    group_id,
                )
                return True
            if status == 429 or "429" in err_str:
                logger.warning("LINE push 429 quota (group=%s) — no retry", group_id)
                return False
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.info(
                    "LINE push fail (group=%s, attempt %d/%d), retry in %ds: %s",
                    group_id, attempt + 1, max_retries, wait, err_str[:120],
                )
                time.sleep(wait)
                continue
            logger.warning(
                "LINE push failed after %d attempts (group=%s): %s",
                max_retries, group_id, err_str[:120],
            )
            return False
    return False


def _decide_stage(r: dict, now: int) -> str | None:
    """判斷該 reminder 此刻該推哪個 stage，None = 不推。

    The windows live in reminder_stages (shared with memory.consume_open_stages);
    see that module for the ladder.
    """
    return reminder_stages.open_stage(r, now)


_STAGE_LABELS = {
    "weekly": "（一週後）",
    "3d": "（3 天後）",
    "1d": "（明天）",
    "4hr": "（4 小時後）",
    "2hr": "（2 小時後）",
    "1hr": "（1 小時後）",
    "now": "（**現在 / 即將到時**）",
}
_DAY_BASED_STAGES = {"weekly", "3d", "1d"}


def _stage_label(
    stage: str,
    remind_at: int,
    now: int | None = None,
    *,
    source_kind: str = "",
) -> str:
    """Return a human label based on the target calendar date, not the window."""
    if stage == "now" and source_kind == "contextual_date_once":
        now_ts = int(datetime.now(_TW).timestamp()) if now is None else now
        if now_ts - int(remind_at) > 15 * 60:
            return "（今天，補送）"
    if stage in _DAY_BASED_STAGES:
        now_ts = int(datetime.now(_TW).timestamp()) if now is None else now
        delta_days = (
            datetime.fromtimestamp(remind_at, _TW).date()
            - datetime.fromtimestamp(now_ts, _TW).date()
        ).days
        if delta_days == 0:
            return "（今天）"
        if delta_days == 1:
            return "（明天）"
        if delta_days == 2:
            return "（後天）"
        if delta_days == 7:
            return "（1 週後）"
        if delta_days == 30:
            return "（1 個月後）"
        if delta_days > 0:
            return f"（{delta_days} 天後）"
    return _STAGE_LABELS.get(stage, "")


def _participant_plain_labels(r: dict) -> list[str]:
    names = line_mentions.parse_participants(r.get("mention_aliases") or [])
    if not names:
        return []
    if line_mentions.is_all_participants(names):
        return ["@all"]
    return [f"@{name}" for name in names if name]


# fixR5a (GP1 r4 #1 #2): a later, fuller mention never becomes the reminder's
# wording (the calendar pairing and quoted cancel / reschedule read it).  The
# push, and the receipt (main._format_persisted_reminder_confirmation), show
# the absorbed wordings that add words on one 「細節：…」 line instead.
_DETAIL_ITEMS = 3
_DETAIL_CHARS = 120
_DETAIL_SEPARATOR = "；"


def _wording_core(text: object) -> str:
    """Letters and digits only, width- and case-folded (spaces and marks dropped)."""
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(char for char in normalized if char.isalnum())


def fuller_detail_line(action: object, merged_details: object) -> str:
    """「細節：…」 listing absorbed wordings that add words to ``action``; "" if none.

    Each wording once: one that another listed wording contains gives way to
    the fuller one (in its place); never one with no words beyond ``action``
    or a 前一天／當天 label; at most three items and 120 characters, one line.
    """
    action_core = _wording_core(action)
    chosen: list[tuple[str, str]] = []
    for item in merged_details or []:
        if not isinstance(item, dict):
            continue
        wording = " ".join(str(item.get("action") or "").split())
        core = _wording_core(wording)
        if not core or core in action_core:
            continue
        if reminder_intent.has_reminder_offset_marker(wording):
            continue
        if any(core in kept for kept, _shown in chosen):
            continue
        covered = [index for index, (kept, _shown) in enumerate(chosen) if kept in core]
        if covered:
            chosen[covered[0]] = (core, wording)
            for index in reversed(covered[1:]):
                del chosen[index]
        else:
            chosen.append((core, wording))
    pieces: list[str] = []
    used = 0
    for _core, wording in chosen:
        if len(pieces) >= _DETAIL_ITEMS:
            break
        cost = len(wording) + (len(_DETAIL_SEPARATOR) if pieces else 0)
        if used + cost > _DETAIL_CHARS:
            if pieces:
                continue
            wording = wording[: _DETAIL_CHARS - 1] + "…"
            cost = len(wording)
        pieces.append(wording)
        used += cost
    return "細節：" + _DETAIL_SEPARATOR.join(pieces) if pieces else ""


def _format_push_text(r: dict, stage: str, now: int | None = None) -> str:
    dt = datetime.fromtimestamp(r["remind_at"], _TW)
    label = _stage_label(
        stage,
        r["remind_at"],
        now=now,
        source_kind=str(r.get("source_kind") or ""),
    )
    # 「媽媽 家長會」: the people lead the action, so no 參加人 line
    # (Andrew 2026-10-07: 主詞放前面).  The @ line above still pings them.
    # 2026-10-09: no one named → the owner the @ line pings goes first.
    action = reminder_overview.subject_first(str(r["action"]), reminder_overview.shown_people(r))
    body = f"⏰ 提醒{label}\n{dt.strftime('%Y-%m-%d %H:%M')} {action}"
    details = fuller_detail_line(r.get("action"), r.get("merged_details"))
    if details:
        body += "\n" + details
    return body


def _build_push_text_and_message(
    r: dict, stage: str, now: int | None = None
) -> tuple[str, object | None]:
    body = _format_push_text(r, stage, now=now)
    targets = line_mentions.reminder_actor_targets(
        str(r.get("user_id") or ""),
        r.get("mention_aliases") or [],
        str(r.get("action") or ""),
    )
    plain_labels = _participant_plain_labels(r) or None
    if not targets:
        text = line_mentions.text_with_plain_mentions(body, [], plain_labels)
        text = validate_push_text(text, source="reminder_push")
        if not text.strip():
            return "", None
        return text, TextMessage(text=text[:4900])
    message_dict = line_mentions.text_v2_dict(body, targets, plain_labels)
    plain_text = line_mentions.text_with_plain_mentions(body, targets, plain_labels)
    safe_plain = validate_push_text(plain_text, source="reminder_push")
    if not safe_plain.strip():
        return "", None
    if safe_plain != plain_text:
        return safe_plain, TextMessage(text=safe_plain[:4900])
    return (plain_text, line_mentions.sdk_message_from_text_v2_dict(message_dict))


def _now_ts() -> int:
    return int(time.time())


def _due_reminder_items(
    group_id: str | None = None,
    limit: int = 10_000,
    now: int | None = None,
    *,
    rows: list[dict] | None = None,
    skip_ids: frozenset[int] | set[int] = frozenset(),
) -> list[dict]:
    """Return reminders due for their next push stage without mutating DB.

    ``rows`` lets a caller pass an already-read listing (the read-only dry
    run); ``skip_ids`` leaves out rows that a planned fold would cancel.
    """
    if limit <= 0:
        return []
    now = _now_ts() if now is None else now
    due: list[dict] = []
    listing = memory.list_pending_reminders_full(group_id) if rows is None else rows
    for r in listing:
        if int(r["reminder_id"]) in skip_ids:
            continue
        stage = _decide_stage(r, now)
        if stage is None:
            continue
        text, message = _build_push_text_and_message(r, stage, now=now)
        if not text.strip() or message is None:
            logger.info(
                "skip reminder due item after validation rid=%s stage=%s",
                r.get("reminder_id"),
                stage,
            )
            continue
        due.append(
            {
                "reminder_id": r["reminder_id"],
                "group_id": r["group_id"],
                "stage": stage,
                "text": text,
                "message": message,
                "action": r["action"],
                "remind_at": r["remind_at"],
                "weekly_count": int(r.get("weekly_count") or 0),
                "user_id": str(r.get("user_id") or ""),
                "source_kind": str(r.get("source_kind") or ""),
                "source_ref": str(r.get("source_ref") or ""),
                "source_text": str(r.get("source_text") or ""),
                "mention_aliases": list(r.get("mention_aliases") or []),
                "time_kind": r.get("time_kind"),
                "merged_details": list(r.get("merged_details") or []),
            }
        )
        if len(due) >= limit:
            break
    return due


def due_reminders_for_reply(
    group_id: str, limit: int = 4, now: int | None = None
) -> list[dict]:
    """Return due reminder_push entries that can piggyback on LINE reply_token.

    Caller must only mark these after reply_message succeeds.  Callers run
    :func:`fold_due_duplicates` first so one event yields one item.
    """
    if limit <= 0 or not group_id:
        return []
    return _due_reminder_items(group_id=group_id, limit=limit, now=now)


def mark_reminders_pushed(reminders: list[tuple[int, str]]) -> int:
    """Mark reminder_push entries after reply_message accepted them."""
    marked = 0
    for reminder_id, stage in reminders:
        if memory.mark_reminder_pushed(reminder_id, stage):
            marked += 1
    return marked


# ── One event, one reminder at push time (2026-10-04, P4 item 1) ─────────────
# Andrew: 同一件事只留一筆、階段照舊；同一時刻同一提醒最多推一則。Rows written
# before the write-time merge (or worded past it) still describe one event.
# When one of them is due, its same-event rows are folded into one primary:
# their words go into the primary's merged_details, they are cancelled, and
# only the primary is delivered, through the usual single-row claim.  Never
# folded: a 前一天／當天 pair the user asked for (contextual_date_once), a
# calendar mirror, an offset-labelled reminder, rows for other people, rows
# on another date, and times that cannot be one occurrence (09:00 vs 19:00).


def _foldable(row: dict) -> bool:
    kind = str(row.get("source_kind") or "")
    if kind == "contextual_date_once":
        return False
    if reminder_stages.is_calendar_mirror(kind, row.get("source_ref")):
        return False
    return not reminder_intent.has_reminder_offset_marker(row.get("action"))


def _tw_date(remind_at: int):
    return datetime.fromtimestamp(int(remind_at), _TW).date()


def _hhmm(remind_at: int) -> str:
    return datetime.fromtimestamp(int(remind_at), _TW).strftime("%H:%M")


def _unresolved_names(aliases: object) -> set[str]:
    names = line_mentions.parse_participants(aliases or [])
    if line_mentions.is_all_participants(names):
        return set()
    return {name for name in names if not line_mentions.user_id_for_alias(name)}


def _target_keys(targets) -> set[str]:
    return {f"{target.kind}:{target.user_id}" for target in targets}


def _reminder_audience(row: dict) -> tuple[str, ...]:
    """Who the push mentions, plus participants shown without a LINE account."""
    keys = _target_keys(
        line_mentions.reminder_actor_targets(
            str(row.get("user_id") or ""),
            row.get("mention_aliases") or [],
            str(row.get("action") or ""),
        )
    )
    keys |= {f"name:{name}" for name in _unresolved_names(row.get("mention_aliases"))}
    return tuple(sorted(keys))


def _identity_texts(row: dict) -> list[str]:
    return [
        str(row.get("action") or ""),
        *(
            str(item.get("action") or "")
            for item in (row.get("merged_details") or [])
            if isinstance(item, dict) and item.get("action")
        ),
    ]


def _merges_into(incoming: dict, kept: dict) -> bool:
    """Would ``incoming`` have been folded into ``kept`` when it was written?"""
    return reminder_intent.mention_matches_reminder(
        str(incoming.get("action") or ""),
        str(kept.get("action") or ""),
        time_kind=incoming.get("time_kind"),
        hhmm=_hhmm(incoming["remind_at"]),
        kept_kind=kept.get("time_kind"),
        kept_hhmm=_hhmm(kept["remind_at"]),
        kept_identity=_identity_texts(kept),
        kept_source=str(kept.get("source_text") or ""),
        mentions=list(incoming.get("mention_aliases") or []),
        kept_mentions=list(kept.get("mention_aliases") or []),
        same_author=bool(incoming.get("user_id"))
        and str(incoming.get("user_id")) == str(kept.get("user_id") or ""),
    )


def _primary_rank(row: dict) -> tuple:
    """Most precise time, then earliest, then most content, then lowest id."""
    content = len(str(row.get("action") or "")) + sum(
        len(str(item.get("action") or "")) + len(str(item.get("text") or ""))
        for item in (row.get("merged_details") or [])
        if isinstance(item, dict)
    )
    return (
        -reminder_intent.time_rank(row.get("time_kind")),
        int(row["remind_at"]),
        -content,
        int(row["reminder_id"]),
    )


def _identity_conflict(row: dict, identity: list[str], names: tuple[str, ...]) -> bool:
    own = _identity_texts(row)
    return any(
        reminder_intent.same_event_identity_conflict(text, identity, names) for text in own
    ) or any(
        reminder_intent.same_event_identity_conflict(text, own, names) for text in identity
    )


def _same_event_pair_test():
    """The fold's pair test: same group, Taipei date and audience, and the
    same-event matcher reading either row into the other.  Caches audiences."""
    audience: dict[int, tuple[str, ...]] = {}

    def same_audience(left: dict, right: dict) -> bool:
        for row in (left, right):
            rid = int(row["reminder_id"])
            if rid not in audience:
                audience[rid] = _reminder_audience(row)
        return audience[int(left["reminder_id"])] == audience[int(right["reminder_id"])]

    def pair(left: dict, right: dict) -> bool:
        return (
            str(left.get("group_id") or "") == str(right.get("group_id") or "")
            and _tw_date(left["remind_at"]) == _tw_date(right["remind_at"])
            and same_audience(left, right)
            and (_merges_into(left, right) or _merges_into(right, left))
        )

    return pair


def plan_folds(
    rows: list[dict], now: int, keep_ids: frozenset[int] | set[int] = frozenset()
) -> list[tuple[dict, list[dict]]]:
    """Pure: (primary, rows folded into it) for each event with a due row.

    ``keep_ids`` are rows a receipt being sent right now names (fixC12): a
    connected set of same-event rows holding one is not folded at this moment,
    so the receipt's own row is never cancelled under it; a later moment folds
    it as usual.
    """
    eligible = [row for row in rows if _foldable(row)]
    if len(eligible) < 2:
        return []
    keep = {int(rid) for rid in keep_ids}
    pair = _same_event_pair_test()

    taken: set[int] = set()
    plans: list[tuple[dict, list[dict]]] = []
    for row in sorted(eligible, key=lambda r: (int(r["remind_at"]), int(r["reminder_id"]))):
        if int(row["reminder_id"]) in taken or reminder_stages.open_stage(row, now) is None:
            continue
        component = [row]
        seen = {int(row["reminder_id"])}
        frontier = [row]
        while frontier:
            current = frontier.pop()
            for other in eligible:
                other_id = int(other["reminder_id"])
                if other_id in seen or other_id in taken:
                    continue
                if pair(current, other):
                    seen.add(other_id)
                    component.append(other)
                    frontier.append(other)
        if seen & keep:
            taken |= seen
            continue
        if len(component) < 2:
            continue
        primary = min(component, key=_primary_rank)
        names = tuple(
            dict.fromkeys(
                alias for member in component for alias in (member.get("mention_aliases") or [])
            )
        )
        folded = [primary]
        identity = _identity_texts(primary)
        remaining = [member for member in component if member is not primary]

        def fits(left: dict, right: dict) -> bool:
            return reminder_intent.times_compatible(
                left.get("time_kind"),
                _hhmm(left["remind_at"]),
                right.get("time_kind"),
                _hhmm(right["remind_at"]),
            )

        grown = True
        while grown:
            grown = False
            for other in list(remaining):
                # Every folded row must fit the primary's time and agree with
                # everything already folded (an absorbed 第一劑 keeps 第二劑 out).
                if not fits(primary, other):
                    continue
                if not any(pair(other, member) for member in folded):
                    continue
                if _identity_conflict(other, identity, names):
                    continue
                # A vague mention that could also be another occurrence the
                # same day (12:00 default next to both 14:00 and 17:00) stays:
                # guessing could silence a real appointment.
                if any(
                    member is not other
                    and not fits(primary, member)
                    and fits(other, member)
                    and pair(other, member)
                    for member in component
                ):
                    continue
                folded.append(other)
                identity += _identity_texts(other)
                remaining.remove(other)
                grown = True
        if len(folded) > 1:
            plans.append((primary, folded[1:]))
            taken.update(int(member["reminder_id"]) for member in folded)
    return plans


def fold_due_duplicates(
    group_id: str | None = None,
    now: int | None = None,
    *,
    keep_ids: frozenset[int] | set[int] = frozenset(),
) -> int:
    """Fold same-event rows before anything is collected for delivery.

    Writes (cancels folded rows); never raises.  Returns how many rows were
    folded.  Used by push_reminders and both reply-token piggyback paths;
    ``keep_ids`` (rows a receipt in the same reply names) see :func:`plan_folds`.
    """
    try:
        now = _now_ts() if now is None else int(now)
        folded = 0
        rows = memory.list_pending_reminders_full(group_id)
        for primary, peers in plan_folds(rows, now, keep_ids):
            done = memory.fold_same_event_reminders(primary, peers)
            if done:
                logger.info(
                    "folded same-event reminders into rid=%d: %s",
                    int(primary["reminder_id"]),
                    ",".join(str(rid) for rid in done),
                )
            folded += len(done)
        return folded
    except Exception as exc:
        logger.warning("same-event fold skipped: %s", type(exc).__name__)
        return 0


def same_event_ids(group_id: str, reminder_ids) -> set[int]:
    """Other pending rows the fold reads as the event of ``reminder_ids``.

    fixC12: a receipt is its event's notice, so none of these may ride on the
    receipt's own reply; they go out at a later moment.  The fold's pair test,
    followed through chains of pairs, from every named row of the group, still
    pending or already folded away; a row whose merged details hold a named
    row's own words absorbed it and belongs to the event too.  Rows the fold
    never touches (calendar mirrors, 前一天／當天 rows, offset labels) are left
    to the batch pairing.  Makes no writes of its own (it reads without the
    duplicate cleanup); it may raise (the caller then sends no reminder along
    with the receipt).
    """
    named = {int(rid) for rid in reminder_ids or ()}
    if not group_id or not named:
        return set()
    seeds = []
    for rid in sorted(named):
        row = memory.get_reminder(rid)
        if row and str(row.get("group_id") or "") == str(group_id) and _foldable(row):
            seeds.append(row)
    if not seeds:
        return set()
    rows = [
        row
        for row in memory.list_pending_reminders_full(group_id, dedupe=False)
        if _foldable(row) and int(row["reminder_id"]) not in named
    ]
    own_words = {
        memory.merged_detail_key(
            str(seed.get("action") or ""), str(seed.get("source_text") or "")
        )
        for seed in seeds
    }
    pair = _same_event_pair_test()
    held: set[int] = set()
    frontier = list(seeds)
    for row in rows:
        if any(
            isinstance(item, dict) and item.get("key") in own_words
            for item in row.get("merged_details") or []
        ):
            held.add(int(row["reminder_id"]))
            frontier.append(row)
    while frontier:
        current = frontier.pop()
        for row in rows:
            rid = int(row["reminder_id"])
            if rid not in held and pair(current, row):
                held.add(rid)
                frontier.append(row)
    return held


# ── One message per event inside one piggyback batch (2026-10-04, v3 item 6) ─


def _audience_covered(needed: set[str], item: dict) -> bool:
    """Whether ``item``'s message mentions everyone in ``needed``."""
    item_keys = set(_reminder_audience(item))
    if "all:" in item_keys:
        return not {key for key in needed if key.startswith("name:")} - item_keys
    return needed <= item_keys


def _event_audience_covered(event: dict, item: dict) -> bool:
    """Whether the reminder message mentions everyone the calendar one would."""
    event_keys = _target_keys(line_mentions.event_mention_targets(event))
    event_keys |= {f"name:{name}" for name in _unresolved_names(event.get("participants"))}
    return _audience_covered(event_keys, item)


def _strict_same_event(
    title: str, event_time: str, participants: list[str], item: dict, remind_at: int
) -> bool:
    """The v3-item-6 test: strict matcher (no e1/e3), no identity conflict."""
    action = str(item.get("action") or "")
    names = tuple(dict.fromkeys([*(item.get("mention_aliases") or []), *participants]))
    identity = _identity_texts(item)
    if reminder_intent.same_event_identity_conflict(
        title, identity, names
    ) or reminder_intent.same_event_identity_conflict(action, [title], names):
        return False
    same_clock = bool(
        event_time
        and reminder_intent.time_rank(item.get("time_kind")) == 2
        and event_time == _hhmm(remind_at)
    )
    return reminder_intent.same_event_text(
        title, action, same_clock=same_clock, names=names
    )


def calendar_items_covered_by_reminders(
    events: list[dict], items: list[dict]
) -> dict[int, int]:
    """Batch-local pairs {calendar index: reminder index} for one real event.

    Same Taipei date, the strict matcher (no e1/e3 loosening), no identity
    conflict either way, and the reminder message reaching everyone the
    calendar message would.  The caller sends only the reminder message and,
    once LINE accepted the batch, marks both.
    """
    pairs: dict[int, int] = {}
    for event_index, event in enumerate(events):
        title = str(event.get("title") or "")
        event_date = str(event.get("event_date") or "")
        event_time = str(event.get("event_time") or "")
        if not title or not event_date:
            continue
        participants = line_mentions.parse_participants(event.get("participants"))
        for item_index, item in enumerate(items):
            try:
                remind_at = int(item.get("remind_at"))
            except (TypeError, ValueError):
                continue
            action = str(item.get("action") or "")
            if not action or _tw_date(remind_at).isoformat() != event_date:
                continue
            if not _event_audience_covered(event, item):
                continue
            if _strict_same_event(title, event_time, participants, item, remind_at):
                pairs[event_index] = item_index
                break
    return pairs


def _mirror_riders(items: list[dict]) -> dict[int, int]:
    """Batch-local pairs {mirror index: reminder index} for one real event.

    Deferred S10(i) (GP1 r2): a natural reminder written first and a calendar
    event written later for the same appointment both own intraday stages, so
    the event's mirror row and the reminder fall due together.  A due mirror
    pairs with a due non-mirror reminder of the same group on the same Taipei
    date when their times can be one occurrence, the strict v3-item-6 matcher
    agrees (title vs. action, no identity conflict) and the reminder message
    reaches everyone the mirror message would.
    """
    pairs: dict[int, int] = {}
    for mirror_index, mirror in enumerate(items):
        if not reminder_stages.is_calendar_mirror(
            mirror.get("source_kind"), mirror.get("source_ref")
        ):
            continue
        try:
            mirror_at = int(mirror.get("remind_at"))
        except (TypeError, ValueError):
            continue
        title = str(mirror.get("action") or "")
        if not title:
            continue
        mirror_date = _tw_date(mirror_at)
        mirror_hhmm = _hhmm(mirror_at)
        participants = line_mentions.parse_participants(mirror.get("mention_aliases"))
        needed = set(_reminder_audience(mirror))
        for item_index, item in enumerate(items):
            if item_index == mirror_index or reminder_stages.is_calendar_mirror(
                item.get("source_kind"), item.get("source_ref")
            ):
                continue
            if str(item.get("group_id") or "") != str(mirror.get("group_id") or ""):
                continue
            try:
                remind_at = int(item.get("remind_at"))
            except (TypeError, ValueError):
                continue
            if not str(item.get("action") or "") or _tw_date(remind_at) != mirror_date:
                continue
            if not reminder_intent.times_compatible(
                item.get("time_kind"),
                _hhmm(remind_at),
                mirror.get("time_kind"),
                mirror_hhmm,
            ):
                continue
            if not _audience_covered(needed, item):
                continue
            if _strict_same_event(title, mirror_hhmm, participants, item, remind_at):
                pairs[mirror_index] = item_index
                break
    return pairs


# fixC12 (GP1 r2): a 前一天／當天 row the user asked for (contextual_date_once)
# and the reminder of the same appointment.  Its action reads
# 「<actor> <M/D>[ <HH:MM>] <title>（前一天提醒｜當天提醒）」
# (main._contextual_date_reminder_plan); its own time is a default reminder
# time, not the appointment's, so the day comes from the slot and the clock
# only from the label.
_CONTEXTUAL_SLOT_RE = re.compile(r"\(\s*(前一天|當天)\s*提醒\s*\)\s*$")
_CONTEXTUAL_LABEL_RE = re.compile(
    r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])(?:\s+((?:[01]\d|2[0-3]):[0-5]\d)(?!\d))?"
)


def _contextual_appointment(row: dict):
    """(appointment date, its clock or "") of a 前一天／當天 row, else None."""
    if str(row.get("source_kind") or "") != "contextual_date_once":
        return None
    action = reminder_intent.normalize_text(row.get("action"))
    slot = _CONTEXTUAL_SLOT_RE.search(action)
    if slot is None:
        return None
    try:
        day = _tw_date(int(row.get("remind_at")))
    except (TypeError, ValueError):
        return None
    if slot.group(1) == "前一天":
        day += timedelta(days=1)
    label = _CONTEXTUAL_LABEL_RE.search(action)
    if label is None:
        return day, ""
    if (int(label.group(1)), int(label.group(2))) != (day.month, day.day):
        return None  # the label names another day: no guessing
    return day, label.group(3) or ""


def _contextual_riders(items: list[dict]) -> dict[int, int]:
    """Batch-local pairs {前一天／當天 index: reminder index} for one appointment.

    Like the mirror pairing: a plain reminder (one the fold could touch) of the
    same group dated on the appointment day, at a time that can be the
    appointment's when the label gives one, the strict v3-item-6 matcher, and
    the reminder message reaching everyone the 前一天／當天 message would.
    """
    pairs: dict[int, int] = {}
    for rider_index, rider in enumerate(items):
        appointment = _contextual_appointment(rider)
        if appointment is None:
            continue
        day, clock = appointment
        title = str(rider.get("action") or "")
        participants = line_mentions.parse_participants(rider.get("mention_aliases"))
        needed = set(_reminder_audience(rider))
        for item_index, item in enumerate(items):
            if item_index == rider_index or not _foldable(item):
                continue
            if str(item.get("group_id") or "") != str(rider.get("group_id") or ""):
                continue
            try:
                remind_at = int(item.get("remind_at"))
            except (TypeError, ValueError):
                continue
            if not str(item.get("action") or "") or _tw_date(remind_at) != day:
                continue
            if clock and not reminder_intent.times_compatible(
                item.get("time_kind"), _hhmm(remind_at), "clock", clock
            ):
                continue
            if not _audience_covered(needed, item):
                continue
            if _strict_same_event(title, clock, participants, item, remind_at):
                pairs[rider_index] = item_index
                break
    return pairs


def items_riding_on_reminders(items: list[dict]) -> dict[int, int]:
    """Batch-local pairs {rider index: reminder index}: one message per event.

    Andrew 2026-10-04: one real-world event never yields two messages at one
    moment.  A due calendar mirror (S10 i) or a due 前一天／當天 row (fixC12)
    of the same appointment as a due reminder rides on that reminder: the
    caller sends only the reminder's message (it carries the clock and the
    stage label), claims both, marks both once LINE accepted it (a 前一天／當天
    row's "now" completes it) and releases both when it did not.  A rider is
    never cancelled or folded, and with no reminder stage due it goes out on
    its own.
    """
    pairs = _mirror_riders(items)
    contextual = _contextual_riders(items)
    pairs.update(contextual)  # a mirror is never a 前一天／當天 row
    # A mirror that matched a 前一天／當天 row which rides on a reminder follows
    # it there, so every rider's carrier is itself sent.
    for rider, carrier in list(pairs.items()):
        if carrier in contextual:
            pairs[rider] = contextual[carrier]
    return pairs


# The pre-fixC12 name; main._receipt_covers still calls it.
mirror_items_covered_by_reminders = items_riding_on_reminders


def _print_dry_run(now: int) -> int:
    """List what a live run would send, reading only (no fold, no cleanup)."""
    rows = memory.list_pending_reminders_full(None, dedupe=False)
    plans = plan_folds(rows, now)
    for primary, peers in plans:
        print(
            f"[DRY] fold rids={','.join(str(p['reminder_id']) for p in peers)} "
            f"into rid={primary['reminder_id']}"
        )
    folded = {int(peer["reminder_id"]) for _primary, peers in plans for peer in peers}
    items = _due_reminder_items(now=now, rows=rows, skip_ids=folded)
    riding = items_riding_on_reminders(items)
    shown = 0
    for index, item in enumerate(items):
        if index in riding:
            print(
                f"[DRY] rider rid={item['reminder_id']} stage={item['stage']} "
                f"rides with rid={items[riding[index]]['reminder_id']}"
            )
            continue
        print(f"[DRY] rid={item['reminder_id']} stage={item['stage']} group={item['group_id']}")
        print(f"      {item['text']}")
        shown += 1
    return shown


def push_reminders(dry_run: bool = False) -> int:
    """掃所有 pending reminder，依階梯式 stage 規則 push。

    回 push 成功的筆數。--dry-run 只讀：不清過期、不折疊、不推。
    """
    if dry_run:
        return _print_dry_run(_now_ts())

    deleted = memory.delete_stale_pending_reminders(
        grace_seconds=STALE_PENDING_GRACE_SECONDS
    )
    if deleted:
        logger.info("cleaned %d stale pending reminders", deleted)

    fold_due_duplicates()

    items = _due_reminder_items()
    # S10(i) / fixC12: a calendar mirror or a 前一天／當天 row due together with
    # the reminder of the same event rides on that reminder's push (one message
    # per event per moment).
    riders: dict[int, list[int]] = {}
    try:
        for rider_index, natural_index in items_riding_on_reminders(items).items():
            riders.setdefault(natural_index, []).append(rider_index)
    except Exception as exc:
        logger.warning("same-event pairing skipped: %s", type(exc).__name__)
        riders = {}
    riding = {index for indexes in riders.values() for index in indexes}

    sent = 0
    for index, item in enumerate(items):
        if index in riding:
            continue  # handled with the reminder it rides on
        sent += _push_due_item(item, [items[i] for i in riders.get(index, [])])
    return sent


def _claim_due_item(item: dict) -> dict | None:
    """Claim one due stage for a push; None when it must not go out now."""
    reminder_id = item["reminder_id"]
    group_id = item["group_id"]
    # The claim is the delivery linearization point shared with
    # cancellation and reply-token piggyback senders.
    if not memory.is_reminder_pending(group_id, reminder_id):
        logger.info(
            "skip no-longer-pending reminder before push rid=%d group=%s",
            reminder_id,
            group_id,
        )
        return None
    claim = memory.claim_natural_reminder_delivery(
        group_id,
        reminder_id,
        item["stage"],
        expected_action=str(item["action"]),
        expected_remind_at=int(item["remind_at"]),
        expected_weekly_count=int(item.get("weekly_count") or 0),
        expected_user_id=(
            str(item.get("user_id") or "") if "user_id" in item else None
        ),
        expected_source_kind=(
            str(item.get("source_kind") or "")
            if "source_kind" in item
            else None
        ),
        expected_source_ref=(
            str(item.get("source_ref") or "")
            if "source_ref" in item
            else None
        ),
        expected_source_text=(
            str(item.get("source_text") or "")
            if "source_text" in item
            else None
        ),
        expected_mention_aliases=(
            list(item.get("mention_aliases") or [])
            if "mention_aliases" in item
            else None
        ),
        transport="push",
    )
    if claim is None:
        logger.info(
            "skip unclaimed reminder before push rid=%d group=%s",
            reminder_id,
            group_id,
        )
    return claim


def _push_due_item(item: dict, riders: list[dict] | None = None) -> int:
    """Push one due item; same-event ``riders`` (a calendar mirror, a
    前一天／當天 row) share its fate.

    Riders are claimed after the item and marked only when LINE accepted its
    message, released when it did not.  If the item itself cannot go out now,
    each rider is pushed on its own.  Returns how many messages LINE accepted.
    """
    reminder_id = item["reminder_id"]
    group_id = item["group_id"]
    stage = item["stage"]
    claim = _claim_due_item(item)
    if claim is None:
        return sum(_push_due_item(rider) for rider in riders or [])
    claims = [(claim, reminder_id, stage)]
    for rider in riders or []:
        rider_claim = _claim_due_item(rider)
        if rider_claim is not None:
            claims.append((rider_claim, rider["reminder_id"], rider["stage"]))
            logger.info(
                "rid=%d stage=%s rides with rid=%d",
                rider["reminder_id"],
                rider["stage"],
                reminder_id,
            )

    ok = _push_to_group(
        group_id,
        item["text"],
        message=item.get("message"),
        reminder_id=reminder_id,
        source_kind=str(item.get("source_kind") or ""),
        source_ref=str(item.get("source_ref") or ""),
        retry_key=str(claim["retry_key"]),
    )
    if ok:
        for each, each_id, each_stage in claims:
            if not memory.finalize_natural_reminder_delivery(each):
                logger.error(
                    "delivery accepted but claim finalization failed rid=%d stage=%s",
                    each_id,
                    each_stage,
                )
        logger.info("pushed rid=%d stage=%s", reminder_id, stage)
        return 1
    for each, _each_id, _each_stage in claims:
        memory.release_reminder_delivery_claim(each)
    logger.warning(
        "push failed rid=%d stage=%s, will retry next run",
        reminder_id, stage,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LINE 提醒推送（階梯式 schedule）")
    parser.add_argument("--dry-run", action="store_true",
                        help="不真推 LINE，只印出來（唯讀）")
    args = parser.parse_args()

    sent = push_reminders(dry_run=args.dry_run)
    if sent:
        if args.dry_run:
            logger.info("reminder_push: %d 筆待推送（dry-run）", sent)
        else:
            logger.info("reminder_push: %d 筆已推送", sent)

    # 過期清理（每天 00:00 一次就夠）；dry-run 不寫。
    if not args.dry_run and datetime.now().hour == 0 and datetime.now().minute < 15:
        expired = memory.expire_old_reminders()
        if expired:
            logger.info("expired %d old reminders", expired)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
