"""Helpers for LINE textV2 mention messages.

The public text should contain readable @ labels, while real LINE userIds stay
inside textV2 substitution payloads and should not be logged as message text.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_DEFAULT_ALIAS_PATH = Path(__file__).with_name("user_aliases.json")
_DEFAULT_ROLE_ALIAS_PATH = Path(__file__).with_name("family_roles.local.json")
_SPLIT_RE = re.compile(r"[、,，/\s]+")
_MENTION_TOKEN_RE = re.compile(r"@([A-Za-z0-9_\u4e00-\u9fff]{1,30})")
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


def _role_alias_path() -> Path:
    return Path(
        os.environ.get("LINE_FAMILY_ROLE_ALIASES_PATH") or _DEFAULT_ROLE_ALIAS_PATH
    )


def _clean_name(name: str) -> str:
    return str(name or "").strip().lstrip("@").strip()


# 2026-10-09：沒有本機別名的家人，用 bot 查到的 LINE 顯示名稱當稱呼。主程式查到就記在
# 這個檔（state/ 不進 git、權限 0600），提醒推播這種不能問 LINE 的程序也讀得到。
_DEFAULT_DISPLAY_NAMES_PATH = Path(__file__).with_name("state") / "member_display_names.json"
_DISPLAY_NAMES_LOCK = threading.Lock()


def _display_names_path() -> Path:
    return Path(os.environ.get("LINE_MEMBER_DISPLAY_NAMES_PATH") or _DEFAULT_DISPLAY_NAMES_PATH)


def _load_display_names() -> dict[str, str]:
    try:
        data = json.loads(_display_names_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v} if isinstance(data, dict) else {}


def display_name_for_user_id(user_id: str) -> str | None:
    """The LINE display name the bot looked up earlier for this member, if any."""
    return _load_display_names().get(user_id or "") or None


def display_names() -> dict[str, str]:
    """{user_id: 顯示名稱}：bot 查過的所有沒有本機別名的家人。"""
    return _load_display_names()


def remember_display_name(user_id: str, name: str) -> None:
    """Keep a looked-up display name for processes that cannot ask LINE."""
    if not user_id or not name:
        return
    with _DISPLAY_NAMES_LOCK:
        _write_display_name(user_id, name)


def forget_display_name(user_id: str) -> None:
    """The bot no longer uses this member's display name (2026-10-10: it now
    clashes with an alias or another member's); processes reading the file stop too."""
    if not user_id:
        return
    with _DISPLAY_NAMES_LOCK:
        _write_display_name(user_id, None)


def _write_display_name(user_id: str, name: str | None) -> None:
    data = _load_display_names()
    if data.get(user_id) == name or (name is None and user_id not in data):
        return
    if name is None:
        data.pop(user_id, None)
    else:
        data[user_id] = name
    path = _display_names_path()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


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


def configured_family_alias_mapping(*, include_short: bool = True) -> dict[str, str]:
    """Return local-only display aliases without embedding private names in code."""

    mapping: dict[str, str] = {}
    aliases = load_user_aliases()
    canonical_values = {_clean_name(label) for label in aliases.values()}
    for label in aliases.values():
        clean = _clean_name(label)
        if not clean:
            continue
        mapping[clean] = clean
        if include_short and len(clean) == 3 and all("\u4e00" <= ch <= "\u9fff" for ch in clean):
            mapping.setdefault(clean[1:], clean)
    path = _role_alias_path()
    try:
        roles = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        roles = {}
    if isinstance(roles, dict):
        for role, label in roles.items():
            clean_role = _clean_name(role)
            clean_label = _clean_name(label)
            if clean_role and clean_label in canonical_values:
                mapping[clean_role] = clean_label
    return mapping


def configured_family_aliases(*, include_short: bool = True) -> tuple[str, ...]:
    return tuple(configured_family_alias_mapping(include_short=include_short))


def clear_alias_cache() -> None:
    return None


def alias_for_user_id(user_id: str) -> str | None:
    return load_user_aliases().get(user_id or "")


def user_id_for_alias(name: str) -> str | None:
    target = _clean_name(name)
    if not target:
        return None
    configured = configured_family_alias_mapping(include_short=True)
    canonical = configured.get(target, target)
    for user_id, alias in load_user_aliases().items():
        if _clean_name(alias) == canonical:
            return user_id
    return None


def user_id_for_family_role(role: str) -> str | None:
    """Resolve a configured family role (for example ``妹妹``) to a user id."""
    return user_id_for_alias(role)


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


def aliases_mentioned_in_text(text: str) -> list[str]:
    """從文字抽取可辨識到的 family aliases（包含 @標記與直接名稱）。"""
    aliases: list[str] = []
    if not text:
        return aliases

    raw_aliases = load_user_aliases()
    if not raw_aliases:
        return aliases

    normalized = (text or "").replace("＠", "@").strip()
    alias_set = configured_family_alias_mapping(include_short=True)
    all_aliases = {_clean_name(name) for name in _ALL_PARTICIPANT_NAMES}

    seen = set()
    for candidate in _MENTION_TOKEN_RE.findall(normalized):
        name = _clean_name(candidate)
        if not name:
            continue
        canonical = alias_set.get(name)
        if not canonical:
            continue
        if canonical not in seen:
            aliases.append(canonical)
            seen.add(canonical)

    for canonical in alias_set.values():
        if canonical in seen:
            continue
        if canonical in normalized:
            aliases.append(canonical)
            seen.add(canonical)

    for alias in all_aliases:
        if alias in {"", "@all"}:
            continue
        if alias in seen:
            continue
        if alias in normalized:
            aliases.append(alias)
            seen.add(alias)

    return aliases


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

    aliases = [str(alias) for alias in (mention_aliases or []) if str(alias).strip()]
    if is_all_participants(aliases):
        return [MentionTarget(key="all", kind="all", label="@all")]

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

    for alias in aliases:
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


def sdk_message_from_text_v2_dict(message: dict, quote_token: str | None = None):
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
        quoteToken=quote_token,
    )
