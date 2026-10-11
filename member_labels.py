"""member_labels — 家人的稱呼怎麼清理、不問 LINE 時怎麼對到人（2026-10-11 從 main 搬出）。

main 回覆時用 _member_label（會查 LINE 顯示名稱）；每日記憶 job（jobs/daily_memory.py）
不能 import main、也不查 LINE，用 resolve_labels_offline 只讀本機別名和 bot 記下的顯示名稱。
兩邊用同一個 _clean_member_label，job 存進記憶的「稱呼：」才對得上 /看記憶。
main 照舊 re-export 這些名字（main._clean_member_label 是同一個物件）。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

import line_mentions

BOT_USER_ID = "__bot__"

# 2026-10-10 review：稱呼拿掉括號與引號、冒號（含形似字）、@、空白、bidi 與零寬等格式字元、
# 看不見的韓文填充字：顯示名稱不能冒充「【系統…】」「（現在說話的是…）」，也不能在畫面上看起來
# 是別人。emoji、符號和其他標點照留（「🌸」「Andy-Lin」「李・大華」）。只看文字和數字比對保留字：
# 是咪寶、開頭是咪寶／系統／使用者、或含「說話的是」「不確定是誰」的就不用。
_LABEL_DROP_CHARS = "\u02d0\ua4fd\u05c3\u1804\u205a\u115f\u1160\u3164\uffa0:：﹕︓∶꞉@＠"
_LABEL_DROP_CATEGORIES = ("Cc", "Cf", "Zs", "Zl", "Zp", "Ps", "Pe", "Pi", "Pf")
# 咪寶的各種寫法；fact_filters 的 meta 類別也用（事實裡不能出現 bot 的名字，但「系統工程師」照收）。
BOT_NAME_VARIANTS = ("咪寶", "咪宝", "咪寳", "米寶", "米宝", "米堡")
_RESERVED_LABEL_STARTS = (*BOT_NAME_VARIANTS, "系統", "系统", "使用者")
_RESERVED_LABEL_PARTS = ("說話的是", "说话的是", "不確定是誰")
_RESERVED_LABELS = ("群組成員", "某人")
_LABEL_LEAD_RE = re.compile(r"^[^\u4e00-\u9fffA-Za-z0-9]+")
_MEMBER_LABEL_MAX = 20


def _clean_member_label(name: str) -> str:
    """Labels go in front of family turns as 「稱呼：」: letters and digits only,
    short, and never anything containing the bot's name or the program's markers."""
    text = unicodedata.normalize("NFKC", str(name or ""))
    clean = "".join(
        ch for ch in text
        if ch not in _LABEL_DROP_CHARS and unicodedata.category(ch) not in _LABEL_DROP_CATEGORIES
    )[:_MEMBER_LABEL_MAX]
    letters = "".join(ch for ch in clean if unicodedata.category(ch)[0] in "LN")
    if (
        _LABEL_LEAD_RE.sub("", letters).startswith(_RESERVED_LABEL_STARTS)  # 「ˉ咪寶」「ー咪寶」
        or any(part in letters for part in _RESERVED_LABEL_PARTS)
        or letters in _RESERVED_LABELS
    ):
        return ""
    return clean


def _food_alias(user_id: str) -> str:
    """food_extractor 的別名表（main._alias_from_user_id 用的同一份）；讀不到回 ""。"""
    try:
        import food_extractor

        return food_extractor.alias_from_user_id(user_id) or ""
    except Exception:
        return ""


def _alias_label(user_id: str) -> str:
    try:
        alias = line_mentions.alias_for_user_id(user_id) or ""
    except Exception:
        alias = ""
    return _clean_member_label(alias or _food_alias(user_id))


def _taken_labels() -> set[str]:
    """別名、角色、短名（key 和 value）：顯示名稱和這些相同就不能用（「媽媽」不能冒充角色）。"""
    try:
        mapping = line_mentions.configured_family_alias_mapping(include_short=True)
    except Exception:
        return set()
    return {_clean_member_label(name) for name in (*mapping, *mapping.values())} - {""}


def _display_label(
    user_id: str, display: dict[str, str], taken: set[str], others: Iterable[str]
) -> str:
    """bot 記下的顯示名稱：不和別名／角色／短名相同、``others`` 裡也沒有人用同一個名字才用。"""
    label = _clean_member_label(display.get(user_id, ""))
    if not label or label in taken:
        return ""
    if any(other != user_id and _clean_member_label(display.get(other, "")) == label for other in others):
        return ""
    return label


def resolve_labels_offline(
    user_ids: Iterable[str | None], *, recent_senders: Iterable[str] | None = None
) -> dict[str, str]:
    """{稱呼: user_id}，不呼叫 LINE API（每日記憶 job 用）。

    別名優先（line_mentions，沒有再用 food_extractor 的表；別名、角色、短名一律優先）；
    沒有別名才用 bot 記下的顯示名稱，而且要和別名、角色、短名都不同。
    撞名只和 ``recent_senders``（job 傳 memory.recent_raw_senders(group_id, 300)）比，
    和 main._display_name_taken 一樣：退群或改名的人留在檔裡的舊名，不會讓新家人一直
    沒有稱呼（2026-10-11 修正第一輪 #2）。沒傳就和檔裡每一個 user_id 比（最嚴）。
    兩個 user_id 得到同一個稱呼時，那個稱呼誰都不給：記憶不能掛錯人。
    """
    ids = list(dict.fromkeys(uid for uid in user_ids if uid and uid != BOT_USER_ID))
    if not ids:
        return {}
    taken = _taken_labels()
    try:
        display = line_mentions.display_names()
    except Exception:
        display = {}
    others = set(display) if recent_senders is None else {uid for uid in recent_senders if uid}
    labels: dict[str, str] = {}
    shared: set[str] = set()
    for user_id in ids:
        label = _alias_label(user_id) or _display_label(user_id, display, taken, others)
        if not label:
            continue
        if labels.setdefault(label, user_id) != user_id:
            shared.add(label)  # two people, one name → nobody's
    for label in shared:
        labels.pop(label, None)
    return labels
