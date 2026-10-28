"""Bookkeeping for the messages that ride on one LINE reply (2026-10-24, reply-path batch).

`_reply` sends one primary message plus up to four riders: a due calendar reminder, a due natural
reminder, a reminder-creation acknowledgement, a legacy pending text, the exercise nudge.  Each
rider holds something that must be settled exactly once when the outcome of the reply is known: a
delivery claim (`sending` -> finalized / released / fenced as `uncertain`), a confirmation claim
(back to `pending`), a pending-text commit id, or the nudge's dedupe row.

When LINE answers a text reply with a 400 that names the refused message (``details[].property``
= ``messages[N]...``), `main._recover_rejected_reply` takes the riders out of the batch and sends
the primary message alone.  A rider is never sent again as plain text (its plain version starts
with the label of the person it pings -- the literal 「@當事人」 when the creator has no local
alias).  The helpers here settle the riders that were taken out -- *fenced* when LINE named them,
*released* otherwise -- and keep every list `_reply` finalizes later (``event_claims``,
``natural_claims``, ``confirmation_claims``, ``commit_ids``) equal to what is still in the batch,
so that nothing is settled twice.  That matters because `memory.release_reminder_delivery_claim`
is a plain DELETE: releasing a claim that was just fenced as ``uncertain`` would erase the fence,
and the poison reminder would ride on every reply again.

The riders list is *aligned* with the messages list (same length, same order; ``None`` for the
primary message).  It is deliberately not keyed by ``id(message)``: ``drop_indexes`` rebuilds the
message list, a dropped message can be garbage-collected and its id reused by a message built
later.

Nothing here touches the network.  Logs carry kinds and ids, never the text of a message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import memory

logger = logging.getLogger(__name__)

# What LINE accepts for one plain text message is 5000 UTF-16 units; a little less is kept so a
# footer or a rider's own prefix never tips it over.
PLAIN_TEXT_LIMIT = 4900


@dataclass
class NudgeSlot:
    """The exercise nudge claimed for one reply (``parent_health.claim_exercise_nudge``).

    ``settled`` says the claim's fate is decided: kept (delivered, or LINE may have delivered it,
    or LINE refused it -- the 72 hour cap then stops it from coming back on every reply) or
    released (definitely not delivered).  ``_reply``'s ``finally`` releases an unsettled one.
    """

    nudge: Any
    claim: tuple[str, int] | None
    settled: bool = False


@dataclass
class Rider:
    """One message that rides on the primary reply, with everything it holds."""

    kind: str  # "event" | "natural" | "confirmation" | "pending" | "nudge"
    label: str  # an id for logs ("event:abc", "reminder:12"), never text
    event_claims: list = field(default_factory=list)
    natural_claims: list = field(default_factory=list)
    confirmation: tuple[int, str] | None = None
    commit_id: str | None = None
    slot: NudgeSlot | None = None

    def claims(self) -> list:
        return [*self.event_claims, *self.natural_claims]


def classify_rejection(
    named: list[int], riders: list[Rider | None], primary_present: bool
) -> tuple[str, list[int]]:
    """Which message(s) did LINE refuse?  Returns ``(case, rider_hits)``:

    * ``"primary"`` -- the primary message is named (riders named with it are only listed);
    * ``"riders"``  -- only riders are named: ``rider_hits`` are their indexes;
    * ``"unknown"`` -- the error names no message of this request (or none that exists).
    """
    valid = [index for index in named if 0 <= index < len(riders)]
    if not valid:
        return "unknown", []
    hits = [index for index in valid if riders[index] is not None]
    if primary_present and 0 in valid:
        return "primary", hits
    if hits:
        return "riders", hits
    return "unknown", []


def fold_claims(riders: list, source: int, carrier: int) -> None:
    """A message folded into another one (same real-world event: the carrier's message is the one
    that is sent) keeps its delivery claims, and they are settled with the carrier: move them."""
    if not (0 <= source < len(riders) and 0 <= carrier < len(riders)):
        return
    src, dst = riders[source], riders[carrier]
    if src is None or dst is None or src is dst:
        return
    dst.event_claims.extend(src.event_claims)
    dst.natural_claims.extend(src.natural_claims)
    src.event_claims = []
    src.natural_claims = []


def release_nudge(group_id: str, slot: NudgeSlot) -> None:
    """Give the nudge's dedupe row back (LINE definitely did not deliver it).  Never raises."""
    try:
        import parent_health

        parent_health.release_exercise_nudge(group_id, slot.nudge, slot.claim)
    except Exception as exc:
        logger.warning("exercise nudge release skipped error_type=%s", type(exc).__name__)


@dataclass
class Batch:
    """What `_reply` is about to send (or just sent) and what the messages hold.

    ``messages``, ``refs``, ``riders`` and the four claim lists are the very lists `_reply` keeps;
    every change is made in place so `_reply`'s later code (finalize, archive, ``finally``) sees
    the batch that LINE finally accepted."""

    group_id: str
    messages: list
    refs: list
    riders: list
    event_claims: list
    natural_claims: list
    confirmation_claims: list
    commit_ids: list

    # ── what is held ────────────────────────────────────────────────────────────────────────

    def orphan_claims(self) -> tuple[list, list]:
        """Delivery claims no rider in the batch owns (the carrier of a folded reminder was
        dropped before it was sent).  They belong to the batch as a whole."""
        owned_event = {id(claim) for rider in self.riders if rider for claim in rider.event_claims}
        owned_natural = {id(claim) for rider in self.riders if rider for claim in rider.natural_claims}
        return (
            [claim for claim in self.event_claims if id(claim) not in owned_event],
            [claim for claim in self.natural_claims if id(claim) not in owned_natural],
        )

    # ── settling ────────────────────────────────────────────────────────────────────────────

    def _settle_claims(self, claims: list, *, fence: bool) -> None:
        if not claims:
            return
        try:
            if fence:
                memory.mark_reminder_delivery_claims_uncertain(claims)
            else:
                memory.release_reminder_delivery_claims(claims)
        except Exception as exc:
            logger.warning(
                "reminder claims not %s error_type=%s",
                "fenced" if fence else "released",
                type(exc).__name__,
            )

    def _settle_rider(self, rider: Rider, *, fence: bool) -> None:
        self._settle_claims(rider.claims(), fence=fence)
        if rider.confirmation is not None and not fence:
            # An acknowledgement has no "uncertain" state.  Released: back to pending.  Fenced:
            # left claimed -- `claim_reminder_confirmations` hands a claim back after 600 s, so a
            # refused acknowledgement is tried again every ten minutes, not on every reply.
            try:
                memory.release_reminder_confirmations(self.group_id, [rider.confirmation])
            except Exception as exc:
                logger.warning("confirmation not released error_type=%s", type(exc).__name__)
        if rider.slot is not None:
            if not fence:
                release_nudge(self.group_id, rider.slot)
            rider.slot.settled = True
        if fence:
            logger.warning("reply rider fenced after LINE refused it kind=%s id=%s", rider.kind, rider.label)
        else:
            logger.info("reply rider released kind=%s id=%s", rider.kind, rider.label)

    def _forget_claims(self, event: list, natural: list) -> None:
        gone_event = {id(claim) for claim in event}
        gone_natural = {id(claim) for claim in natural}
        self.event_claims[:] = [claim for claim in self.event_claims if id(claim) not in gone_event]
        self.natural_claims[:] = [claim for claim in self.natural_claims if id(claim) not in gone_natural]

    def _forget_rider(self, rider: Rider) -> None:
        self._forget_claims(rider.event_claims, rider.natural_claims)
        if rider.confirmation is not None and rider.confirmation in self.confirmation_claims:
            self.confirmation_claims.remove(rider.confirmation)
        if rider.commit_id is not None and rider.commit_id in self.commit_ids:
            self.commit_ids.remove(rider.commit_id)

    def remove_riders(
        self,
        remove: set[int] | frozenset[int],
        *,
        fence: set[int] | frozenset[int] = frozenset(),
        orphans: str = "keep",
    ) -> None:
        """Take riders out of the batch and settle each exactly once: the ones in ``fence`` are
        fenced (delivery claims -> ``uncertain``, the nudge row kept, an acknowledgement left
        claimed), the rest released.
        ``orphans`` says what happens to claims no rider owns: ``"keep"`` (they stay with the
        batch), ``"fence"`` or ``"release"``.  The primary message (``None`` rider) is never
        removed."""
        removable = {
            index for index in remove if 0 <= index < len(self.riders) and self.riders[index] is not None
        }
        if orphans in ("fence", "release"):
            orphan_event, orphan_natural = self.orphan_claims()
            self._settle_claims([*orphan_event, *orphan_natural], fence=orphans == "fence")
            self._forget_claims(orphan_event, orphan_natural)
        for index in sorted(removable):
            rider = self.riders[index]
            self._settle_rider(rider, fence=index in fence)
            self._forget_rider(rider)
        keep = [index for index in range(len(self.messages)) if index not in removable]
        self.messages[:] = [self.messages[index] for index in keep]
        self.refs[:] = [self.refs[index] for index in keep if index < len(self.refs)]
        self.riders[:] = [self.riders[index] for index in keep]
