"""Helpers for LINE textV2 mention messages.

The public text should contain readable @ labels, while real LINE userIds stay
inside textV2 substitution payloads and should not be logged as message text.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

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


class AllowsAllMention(str):
    """A message one of the program's own formatters put together (a poll, a reminder receipt): an
    @all in it may ping the whole group (Andrew 2026-11-07).

    A reply the model wrote never may, whatever it printed: a page, a video's text or an article in
    its prompt can tell it to print 「@all」.  Every `str` method that changes the words hands back a
    plain `str`, so losing this mark on the way to LINE only makes a message quiet.  Wrap a
    formatter's result, never a model's reply.
    """


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


class DisplayNamesUnavailable(RuntimeError):
    """The display-name file is there but cannot be read as a JSON object of names."""


def require_display_names_readable() -> None:
    """Raise ``DisplayNamesUnavailable`` when the display-name file is there but cannot be
    read, decoded or parsed, is not a JSON object, or holds a key or name that is not a
    string (the writer only writes strings); a missing file is normal (nobody was looked
    up yet).  ``display_names()`` answers {} or drops entries for all of these, so a gate
    that must know every name (search, the nightly memory job) asks this first (2026-10-29).
    Every message is fixed: no name, path or byte of the file."""
    path = _display_names_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # a link whose target is gone (an unmounted drive), at the file or at a folder
        # above it, is not "nobody was looked up yet" (review 2026-10-29)
        if any(os.path.islink(p) and not os.path.exists(p) for p in (path, *path.parents)):
            raise DisplayNamesUnavailable("display name file is behind a broken link") from None
        return
    except Exception:
        # never chained: a decode error's text carries the file's bytes
        raise DisplayNamesUnavailable("display name file unreadable") from None
    if not isinstance(data, dict):
        raise DisplayNamesUnavailable("display name file is not a JSON object")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise DisplayNamesUnavailable("display name file has an entry that is not a string")


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
    # 2026-10-29: a file that is there but cannot be read stays as it is.  The gates that
    # need every name (search, stock-quote names, the nightly memory job) stop on it;
    # rewritten with this one name it would let them go on with everyone else missing.
    # It waits for a repair (a backup, or the drive back for a broken link).  The warning
    # carries the reason's fixed text only: no id, name, path or content.
    try:
        require_display_names_readable()
    except DisplayNamesUnavailable as exc:
        logger.warning("%s: left as it is, not rewritten", exc)
        return
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

    return aliases


# 2026-11-07: 「@all」 rings every phone in the family, so it has to stand on its own: no word
# character, "/" (a link's path) or "@" before it, and no word character after it, directly or past
# one of ". @ / -" (a handle, a domain).  re.ASCII: Chinese is no word character here, so 「大家@all」
# and 「@all吃飯」 still count.  Half-width and lower case, as it always was.
_ALL_MENTION_RE = re.compile(r"(?<![\w@/])@all(?![.@/-]?\w)", re.ASCII)


def has_all_mention(text: str | None) -> bool:
    """An @all that stands on its own.  Ask this of the final text: `_md_to_line` still changes what is next to it."""
    return bool(_ALL_MENTION_RE.search(text or ""))


# 2026-11-07: a family message asks for the whole family out loud or not at all: 「@全家」 counts, a
# plain 「全部買完」 or 「大家好」 does not.  It reads the whole message, not the @-tokens alone: cut out of an
# address at all.example, 「@all」 would look real.  The left edge is _ALL_MENTION_RE's.  「all」 and
# 「everyone」 take its right edge too (a handle, a domain) and any case, since LINE's own mention menu
# writes 「@All」 (Andrew, 11/10: family messages only; the bot's own text keeps has_all_mention).  The
# Chinese words take no right edge: 「@全家8點吃飯」 is no handle.
_FAMILY_WORDS = sorted(word for word in {_clean_name(name) for name in _ALL_PARTICIPANT_NAMES} if word)
_ASCII_FAMILY_WORDS = [word for word in _FAMILY_WORDS if word.isascii()]
_CHINESE_FAMILY_WORDS = [word for word in _FAMILY_WORDS if not word.isascii()]
if not (_ASCII_FAMILY_WORDS and _CHINESE_FAMILY_WORDS):  # an empty group would make every "@" a family word
    raise RuntimeError("_ALL_PARTICIPANT_NAMES needs English and Chinese family words")
_WHOLE_FAMILY_MENTION_RE = re.compile(
    r"(?<![\w@/])@((?i:"
    + "|".join(map(re.escape, _ASCII_FAMILY_WORDS))
    + r")(?![.@/-]?\w)|(?:"
    + "|".join(map(re.escape, _CHINESE_FAMILY_WORDS))
    + r"))",
    re.ASCII,
)


def whole_family_mentions(text: str | None) -> list[str]:
    """The family words a message @-mentions (「all」, 「全家」…), each once, English in lower case.
    Full-width 「＠」 counts."""
    found = _WHOLE_FAMILY_MENTION_RE.findall((text or "").replace("＠", "@"))
    return list(dict.fromkeys(word.lower() for word in found))


# 2026-11-07: an @ inside an address or a link mentions nobody: 「x@成員甲」, 「.com/@成員甲」.  A "/" with no
# ASCII letter, digit or "_" before it is no link: 「@成員甲/@成員乙」 is two people (so is 「分類/@成員甲」).
_MENTION_NAME_RE = re.compile(r"(?<![\w@])(?<!\w/)@([A-Za-z0-9_\u4e00-\u9fff]{1,30})", re.ASCII)


def mention_names(text: str | None) -> list[str]:
    """Who a text @-mentions, as written.  Full-width 「＠」 counts."""
    return _MENTION_NAME_RE.findall((text or "").replace("＠", "@"))


# 2026-11-07: 全家 is also FamilyMart, and the name of some clinics and pharmacies.  For the code that
# reads a bare 全家 as the family: an event's title, the medical actor.  Three ways it is not the family:
# - a word of the shop's comes next: 便利商店, 取貨, 寄件, and 診所, 藥局 (Andrew, 11/10).  A place next to
#   it does not count (全家門口, 全家樓下, 全家對面: Andrew, 11/12, they read as the family);
# - someone goes there: 去全家, 到全家, 去一趟全家.  「過去全家出遊」 and 「輪到全家打疫苗」 go nowhere, and
#   「去全家人的聚會」, 「回去全家團圓」, 「回去全家一起吃飯」 go to the family;
# - it is half of another word: 交通安全家長會, 健全家庭, 保全家裡.  Not 「確保全家…」 or 「全家長輩」, where it
#   is the family again; 周 and 完 are left out for 「這周全家用餐」 and 「吃完全家用餐」.
# 「全家買牛奶」 stays the family: 「全家買年貨」 has the same shape.
_FAMILYMART_RE = re.compile(
    r"全家(?:便利商店|便利超商|便利店|超商|門市|分店|店到店|取貨|取件|寄件|寄貨|領貨|領包裹|寄包裹|取包裹|拿包裹|退貨|繳費|聯合診所|診所|藥局|藥房)"
    r"|(?<!過去)(?<![輪看想等遇回直得收提說找聽碰感]到)(?:(?<=[去到])|(?<=去趟)|(?<=去一趟)|(?<=去一下))"
    r"全家(?![人福聚都團]|一起|圍爐|過年)"
    r"|(?<!確保)(?<=[安保健成齊])全家(?=[居裡庭用具電訪教事園屬政務]|長(?!輩))"
)


def names_the_family(text: str | None) -> bool:
    """Whether the text has a 全家 that is the family: not the shop, not a clinic or a pharmacy of that
    name, not half of another word.  Each of those is cut out first, leaving a space so that the letters
    on either side cannot close up into a 全家 nobody wrote."""
    return "全家" in _FAMILYMART_RE.sub(" ", text or "")


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


# ── textV2 safety (2026-10-24, B6.5) ─────────────────────────────────────────────────────────
# In a textV2 message every `{` and `}` of the text is a placeholder delimiter: a literal brace in the
# body (a reply that quotes JSON, a `{x}` in a pasted sentence) has to be written `{{` / `}}` or LINE
# answers HTTP 400 for the whole reply.  LINE also counts text length in UTF-16 code units (an emoji
# is 2), not in Python characters.
_ESCAPED_BRACE_RE = re.compile(r"\{\{|\}\}")


def utf16_len(value: str) -> int:
    """Length LINE counts for a text: UTF-16 code units."""
    text = str(value or "")
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def clip_utf16(value: str, limit: int) -> str:
    """The longest prefix of ``value`` that fits ``limit`` UTF-16 code units (never half a character)."""
    text = str(value or "")
    if limit <= 0:
        return ""
    if len(text) * 2 <= limit:
        return text
    used = 0
    for index, ch in enumerate(text):
        used += 2 if ord(ch) > 0xFFFF else 1
        if used > limit:
            return text[:index]
    return text


def escape_text_v2(value: str) -> str:
    """Literal braces of a textV2 body: `{` -> `{{`, `}` -> `}}`."""
    return str(value or "").replace("{", "{{").replace("}", "}}")


def unescape_text_v2(value: str) -> str:
    """The reverse of `escape_text_v2` for a stored/archived template (`{p1}` placeholders stay)."""
    return _ESCAPED_BRACE_RE.sub(lambda match: match.group(0)[0], str(value or ""))


def _escape_and_clip(body: str, budget: int) -> str:
    """Escape ``body`` and keep as many whole characters as fit ``budget`` UTF-16 units.  The clip is
    made on the escaped text and one character at a time, so it never splits a `{{` / `}}` pair."""
    out: list[str] = []
    used = 0
    for ch in str(body or ""):
        piece = "{{" if ch == "{" else "}}" if ch == "}" else ch
        width = len(piece) + (1 if ord(ch) > 0xFFFF else 0)
        if used + width > budget:
            break
        out.append(piece)
        used += width
    return "".join(out)


def text_v2_dict(
    body: str,
    targets: list[MentionTarget],
    plain_labels: list[str] | None = None,
    *,
    limit: int | None = None,
) -> dict:
    """The textV2 payload.  ``body`` is escaped (see above); with ``limit`` the whole template text
    (the `{p1} ...` prefix plus the escaped body) is clipped to ``limit`` UTF-16 units *after* escaping."""
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
    labels = None if plain_labels is None else [escape_text_v2(label) for label in plain_labels]
    if limit is None:
        escaped = escape_text_v2(body)
    else:
        head = text_with_template_mentions("", targets, labels)
        escaped = _escape_and_clip(body, max(0, int(limit) - utf16_len(head)))
    return {
        "type": "textV2",
        "text": text_with_template_mentions(escaped, targets, labels),
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
