"""Helpers for LINE textV2 mention messages.

The public text should contain readable @ labels, while real LINE userIds stay
inside textV2 substitution payloads and should not be logged as message text.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_DEFAULT_ALIAS_PATH = Path(__file__).with_name("user_aliases.json")
_SPLIT_RE = re.compile(r"[、,，/\s]+")
_ALL_PARTICIPANT_NAMES = {
    "@all",
    "all",
    "everyone",
    "全家",
    "大家",
    "所有人",
    "全部",
    "全員",
}


@dataclass(frozen=True)
class MentionTarget:
    key: str
    kind: str
    user_id: str = ""
    label: str = ""


def _alias_path() -> Path:
    return Path(os.environ.get("LINE_USER_ALIASES_PATH") or _DEFAULT_ALIAS_PATH)


def _clean_name(name: str) -> str:
    return str(name or "").strip().lstrip("@").strip()


def load_user_aliases() -> dict[str, str]:
    path = _alias_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    aliases: dict[str, str] = {}
    for user_id, label in data.items():
        if isinstance(user_id, str) and isinstance(label, str) and user_id and label:
            aliases[user_id] = label
    return aliases


def clear_alias_cache() -> None:
    return None


def alias_for_user_id(user_id: str) -> str | None:
    return load_user_aliases().get(user_id or "")


def user_id_for_alias(name: str) -> str | None:
    target = _clean_name(name)
    if not target:
        return None
    for user_id, alias in load_user_aliases().items():
        if _clean_name(alias) == target:
            return user_id
    return None


def parse_participants(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            loaded = json.loads(text)
        except Exception:
            loaded = None
        if isinstance(loaded, list):
            raw = loaded
        else:
            raw = [p for p in _SPLIT_RE.split(text) if p]
    else:
        return []
    names: list[str] = []
    for item in raw:
        name = _clean_name(str(item))
        if name:
            names.append(name)
    return names


def is_all_participants(names: list[str]) -> bool:
    return any(_clean_name(name).lower() in _ALL_PARTICIPANT_NAMES for name in names)


def event_mention_targets(event: dict) -> list[MentionTarget]:
    names = parse_participants(event.get("participants"))
    if is_all_participants(names):
        return [MentionTarget(key="all", kind="all", label="@all")]

    targets: list[MentionTarget] = []
    seen_user_ids: set[str] = set()
    for name in names:
        user_id = user_id_for_alias(name)
        if not user_id or user_id in seen_user_ids:
            continue
        seen_user_ids.add(user_id)
        targets.append(
            MentionTarget(
                key=f"p{len(targets) + 1}",
                kind="user",
                user_id=user_id,
                label=f"@{name}",
            )
        )
    return targets


def event_plain_labels(event: dict) -> list[str]:
    names = parse_participants(event.get("participants"))
    if is_all_participants(names):
        return ["@all"]
    return [f"@{name}" for name in names if name]


def reminder_actor_targets(
    user_id: str, mention_aliases: list[str] | str | None = None, text: str = ""
) -> list[MentionTarget]:
    if isinstance(mention_aliases, str) and not text:
        text = mention_aliases
        mention_aliases = None
    seen_user_ids: set[str] = set()
    targets: list[MentionTarget] = []
    if user_id:
        label = alias_for_user_id(user_id) or "當事人"
        targets.append(
            MentionTarget(
                key="target",
                kind="user",
                user_id=user_id,
                label=f"@{label}",
            )
        )
        seen_user_ids.add(user_id)

    for alias in mention_aliases or []:
        _append_alias_target(targets, seen_user_ids, str(alias))

    for extra_user_id, alias in load_user_aliases().items():
        if not alias or extra_user_id in seen_user_ids:
            continue
        if alias not in text:
            continue
        _append_alias_target(targets, seen_user_ids, alias)
    return targets


def _append_alias_target(
    targets: list[MentionTarget], seen_user_ids: set[str], alias: str
) -> None:
    name = _clean_name(alias)
    user_id = user_id_for_alias(name)
    if not name or not user_id or user_id in seen_user_ids:
        return
    targets.append(
        MentionTarget(
            key=f"p{len(targets) + 1}",
            kind="user",
            user_id=user_id,
            label=f"@{name}",
        )
    )
    seen_user_ids.add(user_id)


def _template_prefix(
    targets: list[MentionTarget], plain_labels: list[str] | None = None
) -> str:
    target_labels = {target.label for target in targets if target.label}
    unresolved_labels = [
        label for label in (plain_labels or []) if label and label not in target_labels
    ]
    parts = [f"{{{target.key}}}" for target in targets] + unresolved_labels
    return " ".join(parts)


def _plain_prefix(
    targets: list[MentionTarget], plain_labels: list[str] | None = None
) -> str:
    if plain_labels is not None:
        return " ".join(label for label in plain_labels if label)
    labels = []
    for target in targets:
        if target.kind == "all":
            labels.append("@all")
        elif target.label:
            labels.append(target.label)
    return " ".join(labels)


def text_with_template_mentions(
    body: str, targets: list[MentionTarget], plain_labels: list[str] | None = None
) -> str:
    prefix = _template_prefix(targets, plain_labels)
    return f"{prefix}\n{body}" if prefix else body


def text_with_plain_mentions(
    body: str, targets: list[MentionTarget], plain_labels: list[str] | None = None
) -> str:
    prefix = _plain_prefix(targets, plain_labels)
    return f"{prefix}\n{body}" if prefix else body


def text_v2_dict(
    body: str, targets: list[MentionTarget], plain_labels: list[str] | None = None
) -> dict:
    substitution: dict[str, dict] = {}
    for target in targets:
        if target.kind == "all":
            mentionee = {"type": "all"}
        elif target.kind == "user" and target.user_id:
            mentionee = {"type": "user", "userId": target.user_id}
        else:
            continue
        substitution[target.key] = {
            "type": "mention",
            "mentionee": mentionee,
        }
    return {
        "type": "textV2",
        "text": text_with_template_mentions(body, targets, plain_labels),
        "substitution": substitution,
    }


def sdk_message_from_text_v2_dict(message: dict):
    from linebot.v3.messaging import (  # type: ignore[import-untyped]
        AllMentionTarget,
        MentionSubstitutionObject,
        TextMessageV2,
        UserMentionTarget,
    )

    substitution = {}
    for key, value in (message.get("substitution") or {}).items():
        mentionee = value.get("mentionee") or {}
        if mentionee.get("type") == "all":
            target = AllMentionTarget()
        elif mentionee.get("type") == "user" and mentionee.get("userId"):
            target = UserMentionTarget(userId=mentionee["userId"])
        else:
            continue
        substitution[key] = MentionSubstitutionObject(mentionee=target)
    return TextMessageV2(
        text=str(message.get("text") or ""),
        substitution=substitution or None,
    )
