"""家人可要求提醒只推一次 (2026-11-12 proposal): pure text and reply wording.

A family member quotes one bot reminder (a push or a receipt) and says
「只要一小時前提醒」 or 「提醒一次就好」; from then on only that stage of that
reminder is pushed (reminders.push_keep_stage, honoured by reminder_stages).
「照原本提醒就好」 restores the whole ladder.  This module performs no database
writes: ``main`` resolves the quoted reminder and ``memory`` writes the limit.

One left-to-right scan over a closed vocabulary decides every status (the
shape of reminder_reschedule.classify_reschedule_text): a leftover character
means someone is saying something else, so the message is not ours and keeps
its old routing.  That is what keeps 「咪寶 晚上提醒我吃藥」 or 「提醒他一次就好」
away from this feature.

Statuses:

- ``keep``: write layer; ``keep`` is one of reminder_stages.KEEPABLE_STAGES.
- ``restore``: write layer; clears the limit.
- ``reply``: understood, but nothing can be written (``reason`` says why);
  the caller answers one line and owns the message, so it never falls to the
  quoted-reschedule refusal or the chat model.
- ``not_ours``: everything else.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")

KEEP = "keep"
RESTORE = "restore"
REPLY = "reply"
NOT_OURS = "not_ours"

_MAX_LENGTH = 40


@dataclass(frozen=True)
class PushLimitRequest:
    status: str
    keep: str = ""
    reason: str = ""


# A digit or Chinese numeral right before the number would make 「24小時前」
# read as 「4小時前」 (GP3 I3a).
_NO_DIGIT_BEFORE = r"(?<![0-9一二三四五六七八九十兩])"
_TOKENS: tuple[tuple[str, str], ...] = (
    ("fill", r"小咪寶|咪寶|幫我|麻煩|拜託|請|那就|好的|謝謝|感謝|這則|這個|這筆|我們|我"),
    ("change", r"改成|改為|設成|設定成|調整成|換成|更正"),
    ("restore", r"照原本的規則|照原本的|照原本|照原來的|照原來|恢復原本的|恢復原來的|照舊|改回原本的|改回原來的|改回原本|改回原來|改回"),
    ("global", r"以後都|以後|之後都|每次都|所有的?提醒|全部"),
    ("now", r"(?:到時候?|時間到|準時|到點)(?:的時候)?再?"),
    (
        "hr",
        r"(?:提前|提早|前)(?:一|1|兩|二|2|四|4)個?(?:小時|鐘頭)(?:之?前)?"
        r"|" + _NO_DIGIT_BEFORE + r"(?:一|1|兩|二|2|四|4)個?(?:小時|鐘頭)之?前",
    ),
    (
        "unsup",
        r"(?:提前|提早)(?:\d+|[一二三四五六七八九十兩半]+)個?(?:小時|鐘頭|分鐘|分|天|日)(?:之?前)?"
        r"|(?:\d+|[一二三四五六七八九十兩半]+)個?(?:小時|鐘頭|分鐘|分|天|日|週|周|禮拜|星期)之?前"
        r"|(?:\d+|[一二三四五六七八九十兩]+)個半(?:小時|鐘頭)之?前"
        r"|半小時前|前一天|前一晚|前一日|一天前|當天|當日|出門前",
    ),
    ("once", r"(?:一|1)(?:次|遍)"),
    ("count", r"(?:\d+|[兩二三四五六七八九十]+)(?:次|遍)"),
    ("pre", r"只要|只需要|只需|只用|只"),
    ("post", r"就好了?|就夠了?|就行了?|就可以了?|即可|就足夠了?|就ok|就OK"),
    ("verb", r"提醒|推播|推|通知"),
    ("others", r"其他的?都?不用了?|其他的?不要了?|其餘的?都?不用了?|其他的?免了?"),
    (
        "complaint",
        r"(?:不用|不要|別)?(?:一直|那麼多次|這麼多次|那麼多|這麼多|太多次|太多|好多次|很多次|太頻繁)",
    ),
    # A bare 不用／不要 only counts next to a complaint word; with a stage it
    # may negate the stage (「不要一小時前提醒就好」), so nothing is written.
    ("neg", r"不用|不要|別"),
    ("join", r"跟|和|與|及|還有|或是|或"),
    ("tail", r"喔|哦|唷|囉|啦|了|啊|呀|嘿|吧|ㄛ|嗯|耶|嘛|哈哈+|呵呵+"),
)
_TOKEN_RES = tuple((kind, re.compile(pattern)) for kind, pattern in _TOKENS)
_POLITE_RE = re.compile(r"^(?:可以|可不可以|能不能|能否|可否)")
_QUESTION_TAIL_RE = re.compile(r"(?:嗎|呢)$")
_NEGATED_RE = re.compile(r"不是只|不要只|不用只|別只|沒有只")
_PLACEHOLDER_RE = re.compile(r"\{p\d{1,3}\}")
_MENTION_RE = re.compile(r"[@＠]\S+")
_HOUR_STAGE = {
    "一": "1hr", "1": "1hr",
    "兩": "2hr", "二": "2hr", "2": "2hr",
    "四": "4hr", "4": "4hr",
}
_HOUR_DIGIT_RE = re.compile(r"(?:一|1|兩|二|2|四|4)(?=個?(?:小時|鐘頭))")


def _compact(text: str) -> str:
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = _PLACEHOLDER_RE.sub("", s)
    s = _MENTION_RE.sub("", s)
    return re.sub(r"[\W_]+", "", s)


def _scan(s: str) -> list[tuple[str, str]] | None:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(s):
        for kind, rx in _TOKEN_RES:
            match = rx.match(s, pos)
            if match is not None and match.end() > pos:
                tokens.append((kind, match.group(0)))
                pos = match.end()
                break
        else:
            return None
    return tokens


def classify(text: str) -> PushLimitRequest:
    """Classify one message (bot vocative and mentions already allowed)."""

    import reminder_reschedule as rr

    raw = str(text or "")
    s = _compact(raw)
    if not s or len(s) > _MAX_LENGTH:
        return PushLimitRequest(NOT_OURS)
    asked = "?" in raw or "？" in raw or bool(_QUESTION_TAIL_RE.search(s))
    polite = bool(_POLITE_RE.match(s))
    if asked and not polite:
        return PushLimitRequest(NOT_OURS)
    if polite:
        s = _QUESTION_TAIL_RE.sub("", _POLITE_RE.sub("", s, count=1))
    if _NEGATED_RE.search(s) or rr.has_schedule_hint(raw):
        return PushLimitRequest(NOT_OURS)
    tokens = _scan(s)
    if tokens is None:
        return PushLimitRequest(NOT_OURS)
    kinds = [kind for kind, _ in tokens]
    present = set(kinds)
    # Particles only at the end: 「只提醒了一次」 reports what happened.
    first_tail = next((i for i, kind in enumerate(kinds) if kind == "tail"), len(kinds))
    if any(kind != "tail" for kind in kinds[first_tail:]):
        return PushLimitRequest(NOT_OURS)
    # 我 only as 「提醒我」 or 「我只要…」: 「我提醒一次就好」 means the speaker will do it.
    for index, (kind, word) in enumerate(tokens):
        if word in ("我", "我們"):
            after_verb = index > 0 and kinds[index - 1] == "verb"
            before_only = index + 1 < len(kinds) and kinds[index + 1] == "pre"
            if not (after_verb or before_only):
                return PushLimitRequest(NOT_OURS)
    if any(word == "了" for kind, word in tokens if kind == "tail") and not (
        {"post", "complaint"} & present
    ):
        return PushLimitRequest(NOT_OURS)
    if "restore" in present and "post" in present and not (present - {"restore", "post", "fill", "tail", "verb"}):
        return PushLimitRequest(RESTORE)
    if "verb" not in present:
        return PushLimitRequest(NOT_OURS)
    stage_words = [word for kind, word in tokens if kind in ("hr", "now")]
    marked = bool({"pre", "post", "change"} & present)
    if "restore" in present:
        if {"hr", "now", "once", "unsup", "count", "global", "complaint", "neg"} & present:
            return PushLimitRequest(NOT_OURS)
        return PushLimitRequest(RESTORE)
    if "complaint" in present and not stage_words and "once" not in present:
        return PushLimitRequest(REPLY, reason="complaint")
    if "neg" in present:
        return PushLimitRequest(NOT_OURS)
    if not marked:
        return PushLimitRequest(NOT_OURS)
    if "join" in present:
        stage_like = [word for kind, word in tokens if kind in ("hr", "now", "unsup")]
        if len(stage_like) >= 2:
            return PushLimitRequest(REPLY, reason="multiple_stages")
        return PushLimitRequest(NOT_OURS)
    if "global" in present:
        return PushLimitRequest(REPLY, reason="global")
    if "unsup" in present:
        return PushLimitRequest(REPLY, reason="unsupported_stage")
    if "count" in present:
        return PushLimitRequest(REPLY, reason="unsupported_count")
    if len(stage_words) > 1:
        return PushLimitRequest(REPLY, reason="multiple_stages")
    hours = [word for kind, word in tokens if kind == "hr"]
    if hours:
        digit = _HOUR_DIGIT_RE.search(hours[0])
        if digit is None:
            return PushLimitRequest(NOT_OURS)
        return PushLimitRequest(KEEP, keep=_HOUR_STAGE[digit.group(0)])
    if stage_words or "once" in present:
        return PushLimitRequest(KEEP, keep="now")
    if "others" in present or "complaint" in present:
        return PushLimitRequest(REPLY, reason="complaint")
    return PushLimitRequest(NOT_OURS)


# ── Reply texts ─────────────────────────────────────────────────────────────


def format_when(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), TAIPEI).strftime("%Y-%m-%d %H:%M")


def _shown(action: str, people: object) -> str:
    import reminder_overview

    return reminder_overview.subject_first(action, people)


def stage_phrase(keep: str) -> str:
    if keep == "now":
        return "時間到時提醒一次"
    return f"{keep[:-2]} 小時前提醒"


def _after(prefix: str, phrase: str) -> str:
    """「已改成只在 1 小時前提醒」 but 「已改成只在時間到時提醒一次」."""
    return f"{prefix} {phrase}" if phrase[:1].isdigit() else f"{prefix}{phrase}"


# Receipt first lines.  reminder_reschedule._RECEIPT_HEADERS lists these
# prefixes so a quoted receipt still names its reminder without a binding.
RECEIPT_HEADER_KEEP = "已改成只在"
RECEIPT_HEADER_RESTORE = "已改回照原本的規則提醒"
RECEIPT_HEADER_KEEP_UNCHANGED = "提醒本來就只在"
RECEIPT_HEADER_RESTORE_UNCHANGED = "提醒本來就是照原本的規則推"


def receipt(
    status: str,
    keep: str,
    *,
    remind_at: int,
    action: str,
    people: object = (),
    kept_already_sent: bool = False,
    calendar_title: str = "",
) -> str:
    if keep:
        if status == "unchanged":
            head = _after(RECEIPT_HEADER_KEEP_UNCHANGED, stage_phrase(keep)) + "，沒有變更。"
        elif kept_already_sent:
            head = _after(RECEIPT_HEADER_KEEP, stage_phrase(keep)) + "，這段已經提醒過，之後不會再推"
        else:
            head = _after(RECEIPT_HEADER_KEEP, stage_phrase(keep))
    elif status == "unchanged":
        head = f"{RECEIPT_HEADER_RESTORE_UNCHANGED}，沒有變更。"
    else:
        head = RECEIPT_HEADER_RESTORE
    body = f"{head}\n時間：{format_when(remind_at)}\n事項：{_shown(action, people)}"
    title = str(calendar_title or "").strip()
    if title and "\n" not in title and status == "updated" and keep:
        body += f"\n行事曆的「{title}」照原本的規則提醒，不受影響。"
    return body


HOWTO_LIMIT = "要調整推播次數，請引用那則提醒，再說「只要提醒一次」或「只要 1 小時前提醒」。"
HOWTO_RESTORE = "要改回原本的提醒，請引用那則提醒，再說「照原本提醒就好」。"
HOWTO_QUOTED = "要少推幾次，請直接說「只要提醒一次」或「只要 1 小時前提醒」，提醒沒有變更。"


def howto_text(request: PushLimitRequest) -> str:
    return HOWTO_RESTORE if request.status == RESTORE else HOWTO_LIMIT


_REFUSALS = {
    "unsupported_stage": "目前只能留時間到時、1 小時前、2 小時前或 4 小時前其中一段，提醒沒有變更。",
    "multiple_stages": "一次只能留一段推播，提醒沒有變更。\n請說「只要提醒一次」或「只要 1 小時前提醒」。",
    "unsupported_count": "目前只能改成提醒一次，或只留某個時間前的那則，提醒沒有變更。",
    "global": "目前只能一筆一筆調整，提醒沒有變更。\n這則要少推的話，請說「只要提醒一次」或「只要 1 小時前提醒」。",
    "multiple": "引用的訊息裡有多筆提醒，不確定要改哪一筆，提醒沒有變更。\n請引用單一筆提醒的推播再說一次。",
    "ambiguous": "這則訊息對應到不只一筆提醒，為避免改錯，提醒沒有變更。",
    "calendar": "這則是行事曆活動的提醒，推播照活動的規則，提醒沒有變更。",
    "not_generic": "這筆提醒和其他紀錄連動，不能單獨調整推播，提醒沒有變更。",
    "not_found": "這筆提醒已經提醒過、取消或改過了，提醒沒有變更。",
    "terminal": "這筆提醒已經提醒過、取消或改過了，提醒沒有變更。",
    "stale_quote": "這筆提醒之後已經被改過，提醒沒有變更。\n請引用最新的提醒訊息再說一次。",
    "conflict": "這筆提醒剛被改過，提醒沒有變更。\n請引用最新的提醒訊息再說一次。",
    "busy": "這筆提醒正在推送，提醒沒有變更。\n請稍後再引用提醒說一次。",
    "no_clock": "這筆提醒沒有確定的時間，只能改成「提醒一次就好」，提醒沒有變更。",
    "offset_row": "這筆提醒本身就是提前的提醒，只能改成「提醒一次就好」，提醒沒有變更。",
    "past": "要留的那段時間已經過了，提醒沒有變更。",
    "display_unsafe": "這筆提醒的內容無法完整顯示確認，為避免沒確認就改動，提醒沒有變更。",
    "unavailable": "系統暫時無法更新，提醒沒有變更。\n請稍後再試一次。",
}


def refusal_text(reason: str) -> str:
    """Refusals start with 「尚未更新提醒：」 like the quoted reschedule; a complaint gets the how-to line."""

    if reason == "complaint":
        return HOWTO_QUOTED
    return "尚未更新提醒：" + _REFUSALS.get(reason, _REFUSALS["unavailable"])
