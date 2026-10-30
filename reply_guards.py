"""Reply-quality guards for chat replies (2026-10-24, PLAN B1–B3 and B8.2).

Three kinds of reply the family called empty talk or plain wrong are caught
here, each by a deterministic check on the reply text:

* B1 — a regulation number (免稅額度, 稅率, 罰款, 年滿 N 歲 …) that nothing the
  program itself gathered backs.  A reply once gave a duty-free allowance as
  1 公升 when it had been 1.5 公升 for months.  Strong cues (免稅, 稅率, 罰款 …) name a
  rule on their own; weak ones (上限, 規定, 限制 …) only beside a word for a public
  authority or a law in the same sentence — 「鹽的上限是5公克」 is advice.
* B2 — 「影片沒提到 X」 said from material that was only partly read (title and
  description, a truncated transcript, search snippets).  The same module
  picks the transcript windows the model is shown and records whether the
  material was partial.
* B3 — hand-backs 「只有某某知道」「直接問某某最快」, guesses at who 「他」 is and
  recaps of what someone said earlier.  Nothing new, so nothing is sent.

Rules every pattern here follows (agent_rules/line_bot.md, 2026-10-05 H5): the
checks run on reply text while holding the GIL, so

* whitespace is collapsed first and no pattern puts two optional whitespace
  runs next to each other;
* every repetition is bounded (``{1,12}``) or a plain character class, never a
  group that can swallow what the next group also matches;
* a number is only looked for at the start of a run (lookbehind), thousands
  separators are removed in one pass before matching, and every text the
  program backs a claim with is cut to ``BACKING_MAX_CHARS`` first.

The module has no runtime dependency on main.py: main calls it at the two
reply exits (``_enforce_new_value_reply``, ``_guard_generated_reply``) and at
the fetchers that record what was read.
"""

from __future__ import annotations

import functools
import json
import math
import re
import threading
import unicodedata
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from typing import NamedTuple
from urllib.parse import urlsplit

import line_mentions
import reply_policy

# ── shared limits and texts ──────────────────────────────────────────────────

# One message to LINE is at most 5000 characters; the callers cut there first.
REPLY_MAX_CHARS = 5000
# Every text a claim may be backed with is cut here before it is read (GP2: a
# shared page with a number table is enough to make a quadratic scan slow).
BACKING_MAX_CHARS = 20_000
# ... and all of them together (GP2 #5): the explicit texts come first, then the
# conversation newest turn first, so old turns never crowd out the current evidence.
BACKING_TOTAL_MAX_CHARS = 100_000
_BACKING_MAX_TURNS = 60

# 2026-10-24 (v3 #6): generic on purpose — the same line answers 免稅, 健保, 簽證
# and 交通規則 questions, and names no agency it cannot know.
REGULATORY_UNCONFIRMED_REPLY = "這個規定的數字我現在沒辦法確認，請以主管機關最新公告為準。"

_WS_RE = re.compile(r"[ \t\u3000\u00a0]+")
_TERMINATORS = "。！？!?"
_CLAUSE_DELIMS = "，,；;：:"


def _squash(text: str) -> str:
    """Collapse runs of blanks to one space (the first step of every check)."""
    return _WS_RE.sub(" ", text).strip()


# ── segments, clauses and deletion (shared by B1–B3) ─────────────────────────

class _Seg(NamedTuple):
    text: str  # as written
    masked: str  # same length, quoted text blanked out: boundaries never fall inside 「…」
    sentence: bool  # something to read (not a line break)
    blank: bool  # a run of two or more line breaks: a paragraph boundary


def _segments(reply: str) -> list[_Seg]:
    """Sentences and line breaks of ``reply``, cut where reply_policy cuts them.

    Cutting runs on the quote-masked copy, so a 。 inside 「…。」 never ends a
    sentence (2026-10-21: deleting half of a quoted sentence left 「」，並未提及…」).
    """
    masked = reply_policy._mask_quotes(reply)
    out: list[_Seg] = []
    for match in reply_policy._SEGMENT_RE.finditer(masked):
        raw = masked[match.start():match.end()]
        out.append(_Seg(
            reply[match.start():match.end()],
            raw,
            bool(raw.strip()) and "\n" not in raw,
            raw.startswith("\n") and len(raw) >= 2,
        ))
    return out


def _clause_spans(masked: str) -> list[tuple[int, int]]:
    """(start, end) of each clause of one sentence; a clause keeps its delimiter."""
    spans: list[tuple[int, int]] = []
    start = 0
    for i, ch in enumerate(masked):
        if ch in _CLAUSE_DELIMS:
            spans.append((start, i + 1))
            start = i + 1
    if start < len(masked):
        spans.append((start, len(masked)))
    return spans


def _terminator(text: str) -> str:
    """The 。！？ the sentence ended with ('' when it had none)."""
    stripped = text.rstrip()
    i = len(stripped)
    while i and stripped[i - 1] in _TERMINATORS:
        i -= 1
    return stripped[i:]


_LEADING_CONNECTIVE_RE = re.compile(r"^\s*(?:但是?|不過|可是|而且|另外|此外|所以|因此|然後|還有)[，, ]*")


def _rebuild_sentence(seg: _Seg, spans: list[tuple[int, int]], drop: set[int]) -> str:
    """The sentence without the clauses in ``drop``, still ending the way it did."""
    kept_spans = [(k, s, e) for k, (s, e) in enumerate(spans) if k not in drop]
    kept = "".join(seg.text[s:e] for _k, s, e in kept_spans)
    if kept_spans and kept_spans[0][0] > 0:  # what it was joined to is gone: 「不過你可以…」 → 「你可以…」
        kept = _LEADING_CONNECTIVE_RE.sub("", kept, count=1)
    if not kept.strip():
        return ""
    body = kept.rstrip().rstrip(_CLAUSE_DELIMS).rstrip(_TERMINATORS)
    if not body.strip():
        return ""
    return body + _terminator(seg.text)


def _repair_quotes(text: str) -> str:
    """Remove 「」『』 marks that lost their partner when a sentence was deleted."""
    if "「" not in text and "」" not in text and "『" not in text and "』" not in text:
        return text
    pairs = {"「": "」", "『": "』"}
    stack: list[int] = []
    orphans: set[int] = set()
    for i, ch in enumerate(text):
        if ch in pairs:
            stack.append(i)
        elif ch in ("」", "』"):
            if stack and pairs[text[stack[-1]]] == ch:
                stack.pop()
            else:
                orphans.add(i)
    orphans.update(stack)
    if not orphans:
        return text
    return "".join(ch for i, ch in enumerate(text) if i not in orphans)


def _finish(kept: list[str]) -> str:
    text = re.sub(r"\n{3,}", "\n\n", "".join(kept)).strip()
    text = _repair_quotes(text)
    return text if len(reply_policy._normalize(text)) >= 6 else ""


# ── B1: regulation numbers nothing backs ─────────────────────────────────────

# Words that make a sentence a statement of a rule.  辦法／細則／門檻 are not here:
# 「有個辦法：每天走30分鐘」「新手門檻是每週3次」 are not rules (GP1 #5).
#
# Strong cues name a public rule on their own.  Weak cues are also how everyday
# advice talks (「鹽的上限是5公克」「行李限制 20 公斤」「爬山可以帶 2 瓶水」「依電池回收規定處理」):
# one counts only when a word naming a public authority or a law stands in the same
# sentence (2026-10-28, GP1 #1 and GP2 #6).  The anchor is looked for in that sentence, not
# in the reply: otherwise one 「看海關公告」 turns every 上限 after it into a rule.
_STRONG_CUES = (
    "免稅", "免徵", "課稅", "徵稅", "繳稅", "補稅", "退稅", "稅率", "稅額", "稅金", "關稅", "營業稅", "所得稅",
    "罰則", "罰款", "罰鍰", "申報", "報關", "通關", "檢疫", "簽證", "免簽", "法規", "法令", "法定", "條例",
    "年滿", "限速", "超速",
)
_WEAK_CUES = (
    "額度", "限額", "限量", "上限", "下限", "配額", "規定", "限制", "限帶", "可帶", "可以帶", "可攜帶", "可以攜帶",
    "不得攜帶", "禁止攜帶", "未滿",
)
_PUBLIC_ANCHORS = (
    "海關", "入境", "出境", "移民署", "機關", "政府", "公告", "法律", "法條", "衛福部", "財政部", "交通部", "內政部",
    "外交部", "勞動部", "國稅局", "監理", "補助", "健保", "勞保", "勞退", "稅", "旅客",
)
# A sentence that gives advice or a plan is not the next line of a rule it follows.
_ADVICE_RE = re.compile(r"建議|應該|最好|避免|記得|別忘|提醒|務必|不妨|可以先|小心|注意|請先")
# Durations and lengths follow a cue from the sentence before only inside a list: 「…海關公告。
# 路上大約要2小時」 is no allowance, 「簽證規定如下：\n- 免簽90天」 is.
_NO_CARRY_KINDS = frozenset({"天", "個月", "小時", "年", "週", "工作天", "m"})

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_SMALL = {"十": 10, "百": 100, "千": 1000}
_CN_CHARS = "零〇一二兩两三四五六七八九十百千萬億"
_MAGNITUDE = {"": Decimal(1), "百": Decimal(100), "千": Decimal(1000), "萬": Decimal(10_000), "億": Decimal(100_000_000)}

# canonical kind → factor for each written unit (volume in mL, mass in g, length
# in m; money, ages, durations and counts keep their own name).  斤 stays a 斤:
# a Taiwanese 台斤 is not a 市斤.
_UNITS: dict[str, tuple[str, Decimal]] = {
    "公升": ("ml", Decimal(1000)), "升": ("ml", Decimal(1000)), "L": ("ml", Decimal(1000)), "l": ("ml", Decimal(1000)),
    "毫升": ("ml", Decimal(1)), "ml": ("ml", Decimal(1)), "mL": ("ml", Decimal(1)), "ML": ("ml", Decimal(1)),
    "cc": ("ml", Decimal(1)), "CC": ("ml", Decimal(1)),
    "公斤": ("g", Decimal(1000)), "kg": ("g", Decimal(1000)), "KG": ("g", Decimal(1000)), "Kg": ("g", Decimal(1000)),
    "公克": ("g", Decimal(1)), "克": ("g", Decimal(1)), "g": ("g", Decimal(1)),
    "斤": ("斤", Decimal(1)), "盎司": ("oz", Decimal(1)), "oz": ("oz", Decimal(1)), "OZ": ("oz", Decimal(1)),
    "公里": ("m", Decimal(1000)), "km": ("m", Decimal(1000)), "KM": ("m", Decimal(1000)),
    "公尺": ("m", Decimal(1)), "公分": ("m", Decimal("0.01")), "cm": ("m", Decimal("0.01")),
    "新台幣": ("元", Decimal(1)), "新臺幣": ("元", Decimal(1)), "台幣": ("元", Decimal(1)), "臺幣": ("元", Decimal(1)),
    "元": ("元", Decimal(1)), "塊": ("元", Decimal(1)),
    "美元": ("美元", Decimal(1)), "美金": ("美元", Decimal(1)), "日圓": ("日圓", Decimal(1)), "日幣": ("日圓", Decimal(1)),
    "日元": ("日圓", Decimal(1)), "歐元": ("歐元", Decimal(1)), "英鎊": ("英鎊", Decimal(1)),
    "人民幣": ("人民幣", Decimal(1)), "港幣": ("港幣", Decimal(1)), "港元": ("港幣", Decimal(1)),
    "韓元": ("韓元", Decimal(1)), "韓幣": ("韓元", Decimal(1)), "澳幣": ("澳幣", Decimal(1)),
    "%": ("%", Decimal(1)),
    "歲": ("歲", Decimal(1)), "天": ("天", Decimal(1)), "個月": ("個月", Decimal(1)), "小時": ("小時", Decimal(1)), "個小時": ("小時", Decimal(1)),
    "年": ("年", Decimal(1)), "日": ("天", Decimal(1)), "週": ("週", Decimal(1)), "周": ("週", Decimal(1)),
    "工作天": ("工作天", Decimal(1)), "個工作天": ("工作天", Decimal(1)), "工作日": ("工作天", Decimal(1)), "個工作日": ("工作天", Decimal(1)),
    "NTD": ("元", Decimal(1)), "TWD": ("元", Decimal(1)), "USD": ("美元", Decimal(1)), "JPY": ("日圓", Decimal(1)),
    "支": ("支", Decimal(1)), "包": ("包", Decimal(1)), "瓶": ("瓶", Decimal(1)), "罐": ("罐", Decimal(1)),
    "條": ("條", Decimal(1)), "件": ("件", Decimal(1)), "盒": ("盒", Decimal(1)), "箱": ("箱", Decimal(1)),
}
_UNIT_ALTS = "|".join(re.escape(u) for u in sorted(_UNITS, key=len, reverse=True))
_DIGITS = r"\d{1,12}(?:\.\d{1,6})?"
_CN_NUMBER = rf"[{_CN_CHARS}]{{1,12}}(?:點[零〇一二三四五六七八九]{{1,6}})?"
_NUMBER = rf"(?:{_DIGITS}|{_CN_NUMBER})"
# A number is read from the start of its run only: nothing number-like (or a
# letter or a dot) may stand in front, so a long run of digits is tried once.
_NOT_AFTER = rf"(?<![0-9A-Za-z.第{_CN_CHARS}點])"
_MAG = "百千萬億"
_QUANTITY_RE = re.compile(
    _NOT_AFTER
    + rf"(?P<num>{_NUMBER})"
    + rf"(?:[~\-–—至到] ?(?P<num2>{_NUMBER}))?"
    + rf"(?:(?: ?(?P<mag>[{_MAG}]))? ?(?P<unit>{_UNIT_ALTS})(?![A-Za-z])"
    + r"| ?(?P<bare>[萬億])(?![\u4e00-\u9fffA-Za-z0-9]))"
)
_PERCENT_ZH_RE = re.compile(_NOT_AFTER + rf"百分之(?P<num>{_NUMBER})")
# 「NT$20,000」「$500」「US$100」: the currency stands in front of the number.
_CURRENCY_PREFIX_KIND = {"NT$": "元", "NTD": "元", "TWD": "元", "$": "元", "US$": "美元", "USD": "美元"}
_CURRENCY_PREFIX_RE = re.compile(
    rf"(?<![A-Za-z$])(?P<cur>NT\$|US\$|NTD|TWD|USD|\$) ?(?P<num>{_NUMBER})(?: ?(?P<mag>[{_MAG}]))?(?![0-9A-Za-z])"
)
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
# 「2萬5千元」「2萬5」「2萬5000元」「1千5百元」: read as 2.5萬 / 1.5千 (a bare 2萬 would be 20000
# and 5千元 another 5000 — both wrong).  Bounded, and only where a number run starts.
_COMPOUND_RE = re.compile(r"(?<![0-9A-Za-z.])(\d{1,6})(萬|千)(\d{1,5})(百|千)?(?![0-9])")
_BARE_YEAR_LOW, _BARE_YEAR_HIGH = 100, 200  # a 民國 year: 114年 = 2025年


def _cn_section(s: str) -> int | None:
    """A Chinese numeral below 萬: 十五, 一百零五, 二〇二五, 三百, 一百五 (= 150: a last
    digit straight after 百／千 stands for the next unit down; a 零 in between says it does not)."""
    if not s:
        return 0
    if all(ch in _CN_DIGITS for ch in s):
        return int("".join(str(_CN_DIGITS[ch]) for ch in s))
    total, digit, has_digit, last_unit = 0, 0, False, 0
    for ch in s:
        if ch in _CN_DIGITS:
            if ch in "零〇":
                last_unit = 0  # 一百零五: the five is a five
            digit, has_digit = _CN_DIGITS[ch], True
        elif ch in _CN_SMALL:
            if not has_digit:
                if ch != "十":
                    return None
                digit = 1
            total += digit * _CN_SMALL[ch]
            digit, has_digit, last_unit = 0, False, _CN_SMALL[ch]
        else:
            return None
    if has_digit and last_unit:
        return total + digit * (last_unit // 10)
    return total + digit


def _cn_int(s: str) -> int | None:
    if "億" in s:
        high, _, rest = s.partition("億")
        h, r = _cn_int(high), (_cn_int(rest) if rest else 0)
        return None if h is None or r is None else h * 100_000_000 + r
    if "萬" in s:
        high, _, rest = s.partition("萬")
        h, r = _cn_section(high), (_cn_section(rest) if rest else 0)
        if len(rest) == 1 and rest in _CN_DIGITS and rest not in "零〇":
            r = _CN_DIGITS[rest] * 1000  # 兩萬五 = 25000
        return None if not high or h is None or r is None else h * 10_000 + r
    return _cn_section(s)


def _number(text: str) -> Decimal | None:
    """The value of a written number: 1.5, 十八, 一點五, 兩萬五千."""
    try:
        if text[:1].isdigit():
            return Decimal(text)
        whole, point, fraction = text.partition("點")
        value = _cn_int(whole)
        if value is None:
            return None
        result = Decimal(value)
        if point:
            if any(ch not in _CN_DIGITS for ch in fraction):
                return None
            result += Decimal("0." + "".join(str(_CN_DIGITS[ch]) for ch in fraction))
        return result
    except (InvalidOperation, ValueError):
        return None


class Quantity(NamedTuple):
    kind: str  # ml, g, 元, 歲, 年份 …
    value: str  # normalised, no exponent


def _quantity(kind: str, value: Decimal) -> Quantity | None:
    if not value.is_finite() or value < 0 or value > Decimal(10) ** 15:
        return None
    return Quantity(kind, format(value.normalize(), "f"))


def _year_or_duration(value: Decimal) -> Quantity | None:
    """114年 and 2025年 are the same year; 3年 is a length of time."""
    if value == value.to_integral_value():
        if _BARE_YEAR_LOW <= value <= _BARE_YEAR_HIGH:
            return _quantity("年份", value + 1911)
        if 1912 <= value <= 2200:
            return _quantity("年份", value)
    return _quantity("年", value)


def _compound(match: re.Match) -> str:
    whole, big_unit, rest, small_unit = match.groups()
    big = 10_000 if big_unit == "萬" else 1_000
    if small_unit:
        if small_unit == "千" and big_unit == "千":
            return match.group(0)
        value = int(whole) * big + int(rest) * (1000 if small_unit == "千" else 100)
    elif len(rest) >= 3:
        value = int(whole) * big + int(rest)
    elif len(rest) == 1:
        value = int(whole) * big + int(rest) * (big // 10)
    else:
        return match.group(0)  # 2萬25: say nothing rather than guess
    return f"{format((Decimal(value) / big).normalize(), 'f')}{big_unit}"


def _match_text(text: str) -> str:
    """What the checks read: NFKC (１．５ → 1.5, ％ → %), one space for blanks, no
    thousands separators — one pass each, linear."""
    s = unicodedata.normalize("NFKC", text)
    s = _WS_RE.sub(" ", s)
    s = _THOUSANDS_RE.sub("", s)
    s = _COMPOUND_RE.sub(_compound, s)
    return reply_policy.URL_SPAN_RE.sub(" ", s)


def quantities(text: str) -> list[Quantity]:
    """Every number-with-unit in ``text`` in a canonical form.

    1.5公升, 一點五公升, 1500毫升 and 1.5L are the same quantity; 2萬元, 兩萬元 and
    20000元 are the same quantity; 114年 and 2025年 are the same year; 5公升 is
    not 1.5公升 (values are compared as numbers, never as substrings).
    """
    s = _match_text(text)[:REPLY_MAX_CHARS * 8]
    found: list[Quantity] = []
    for match in _QUANTITY_RE.finditer(s):
        first = _number(match.group("num"))
        if first is None:
            continue
        mag_text = match.group("mag") or ""
        values = [first]
        if match.group("num2"):
            second = _number(match.group("num2"))
            if second is not None:
                values.append(second)
        if match.group("bare"):
            mag, unit = _MAGNITUDE[match.group("bare")], "元"
        else:
            mag, unit = _MAGNITUDE[mag_text], match.group("unit")
            # 「1月25日」 is a date, not 25 days
            if unit == "日" and match.start("num") > 0 and s[match.start("num") - 1] == "月":
                continue
        for value in values:
            value = value * mag
            if unit == "年":
                q = _year_or_duration(value)
            else:
                kind, factor = _UNITS.get(unit, (unit, Decimal(1)))
                q = _quantity(kind, value * factor)
            if q is not None:
                found.append(q)
    for match in _PERCENT_ZH_RE.finditer(s):
        value = _number(match.group("num"))
        q = _quantity("%", value) if value is not None else None
        if q is not None:
            found.append(q)
    for match in _CURRENCY_PREFIX_RE.finditer(s):
        value = _number(match.group("num"))
        if value is None:
            continue
        q = _quantity(_CURRENCY_PREFIX_KIND[match.group("cur")], value * _MAGNITUDE[match.group("mag") or ""])
        if q is not None:
            found.append(q)
    return found


def has_regulation_cue(text: str) -> bool:
    """A sentence that states a public rule: a strong cue, or a weak one next to a word
    naming an authority or a law in the same sentence."""
    if any(cue in text for cue in _STRONG_CUES):
        return True
    return any(cue in text for cue in _WEAK_CUES) and any(word in text for word in _PUBLIC_ANCHORS)


def has_any_cue(text: str) -> bool:
    """Strong or weak, anchored or not: the line talks about a rule or a limit."""
    return any(cue in text for cue in _STRONG_CUES) or any(cue in text for cue in _WEAK_CUES)


# ── B1: what a regulation number may be backed with ──────────────────────────

_BOT_QUOTE_OPEN_RE = re.compile(r"-{3} ?原始訊息 ?開始 ?-{3}")
_BOT_QUOTE_SENDER_RE = re.compile(r"\s*\[我 \(bot\)\]\s*[:：]")
_QUOTE_CLOSE_RE = re.compile(r"-{3} ?原始訊息 ?結束 ?-{3}")


def strip_bot_quotes(text: str) -> str:
    """``text`` without the quoted blocks the bot wrote itself (`[我 (bot)]:`).

    A family member quoting the bot's wrong 「1公升」 must not make that number
    "backed" (a number the bot got wrong); quoted family messages stay.  One left-to-right
    pass: each search starts where the last block ended.
    """
    out: list[str] = []
    pos = 0
    for opening in _BOT_QUOTE_OPEN_RE.finditer(text):
        if opening.start() < pos or not _BOT_QUOTE_SENDER_RE.match(text, opening.end()):
            continue
        closing = _QUOTE_CLOSE_RE.search(text, opening.end())
        out.append(text[pos:opening.start()])
        pos = closing.end() if closing else len(text)
    out.append(text[pos:])
    return "".join(out)


def _system_free(text: str) -> str:
    """No bot instruction (QUOTE_CONTEXT_RULE …) can back a number."""
    for fixed in reply_policy._system_texts():
        text = text.replace(fixed, "")
    return text


def regulation_backing(*texts: str, context: list[tuple[str, str]] | None = None) -> list[str]:
    """The texts a regulation number may be backed with — only what the program
    itself gathered: the research evidence, the pre-read material, what the
    family wrote (now, quoted, or earlier in the conversation).

    Never the bot's own quoted messages, the bot's earlier turns, the facts it
    remembers or Gemini's supported segments: a number the bot said wrongly once
    must not vouch for itself (GP2 #6).  Each text is cut to BACKING_MAX_CHARS and
    all of them together to BACKING_TOTAL_MAX_CHARS: ``texts`` first, then the
    family's turns of the conversation, the newest first.
    """
    backing: list[str] = []
    room = BACKING_TOTAL_MAX_CHARS

    def add(text) -> None:
        nonlocal room
        if room > 0 and isinstance(text, str) and text:
            clean = strip_bot_quotes(_system_free(text[:min(BACKING_MAX_CHARS, room)]))
            backing.append(clean)
            room -= len(clean)

    for text in texts:
        add(text)
    for role, text in reversed(list(context or [])[-_BACKING_MAX_TURNS:]):
        if role == "user":
            add(text)
    return backing


class RegulatoryResult(NamedTuple):
    text: str  # '' when nothing worth sending is left
    dropped: int  # sentences removed
    remaining: int  # regulation sentences kept (every number in them is backed)


# 「上限是5公克」 straight after a rule goes on with it; 「高血壓每天鹽的上限是5公克」 is a claim of its own.
_STARTS_WITH_LIMIT_RE = re.compile(r"^[ ,，]*(?:現在|目前|新制|改成|改為|調整為)?(?:上限|下限|額度|限額|限量|配額)")


def _continues_rule(text: str, found: list[Quantity]) -> bool:
    """Whether a sentence without a cue of its own is the next line of the rule
    before it (「免稅額度有調整。現在是1公升。」) and not advice, a plan or an
    ordinary limit judged on its own (「…海關公告。路上大約要2小時。」「記得帶3天份的藥」
    「鹽的上限是5公克」「行李限制 20 公斤」)."""
    if _ADVICE_RE.search(text):
        return False
    if has_any_cue(text) and not _STARTS_WITH_LIMIT_RE.match(text):
        return False
    kinds = {q.kind for q in found if q.kind != "年份"}
    return bool(kinds) and not (kinds & _NO_CARRY_KINDS)


def _line_breaks_of(segs: list[_Seg], drop: set[int]) -> set[int]:
    """The line break after each deleted sentence that stood alone on its line, so a
    deleted list item leaves no empty line behind."""
    extra: set[int] = set()
    for i in drop:
        before = i == 0 or segs[i - 1].text == "\n" or segs[i - 1].blank
        if before and i + 1 < len(segs) and segs[i + 1].text == "\n":
            extra.add(i + 1)
    return extra


def strip_unbacked_regulatory_numbers(reply: str, backing) -> RegulatoryResult:
    """Delete each sentence that states a regulation number nothing in ``backing`` has.

    A sentence is a regulation statement when it holds a number with a unit and a
    cue stands in it (a strong cue, or a weak one beside a public authority or law
    word, see ``has_regulation_cue``), in the sentence just before it, or in the
    header line of the list it belongs to.  The sentence before only passes its
    cue on to a sentence that goes on with the rule — no advice, no durations —
    and a deleted sentence takes the number-less statement it followed with it.
    It is dropped when any of those numbers (compared as values, after 國字
    numerals, 萬, 毫升↔公升 and 民國↔西元 years are normalised) is not among the
    numbers of ``backing``.  A year only counts against years the backing mentions.

    ``backing`` is a list of texts or a function returning one; it is read (and
    scanned) only when a sentence needs it.
    """
    if not isinstance(reply, str) or not reply.strip():
        return RegulatoryResult(reply, 0, 0)
    original = reply
    reply = reply[:REPLY_MAX_CHARS]
    segs = _segments(reply)
    # Cheap exit: no number with a unit anywhere (the common case for chat).
    sentence_quantities: dict[int, list[Quantity]] = {}
    for i, seg in enumerate(segs):
        if seg.sentence:
            found = quantities(seg.text)
            if found:
                sentence_quantities[i] = found
    if not sentence_quantities:
        return RegulatoryResult(original, 0, 0)

    # Where each sentence gets its cue from: itself, the sentence before it in
    # the paragraph, or the header of the list it is an item of.
    candidates: list[int] = []
    carried_from: dict[int, int] = {}
    header_of_item: dict[int, int] = {}
    previous_cue = header_cue = run_cue = False
    header_index = previous_index = -1
    for i, seg in enumerate(segs):
        if seg.blank:
            previous_cue = header_cue = run_cue = False
            header_index = previous_index = -1
            continue
        if not seg.sentence:
            continue
        own = has_regulation_cue(seg.text)
        is_item = bool(reply_policy._NEW_ITEM_RE.match(seg.text))
        found = sentence_quantities.get(i)
        if is_item:
            run_cue = run_cue or own
            cue = own or header_cue or run_cue
            if header_index >= 0:
                header_of_item[i] = header_index
        else:
            run_cue = False
            header_cue = own
            header_index = i
            cue = own
            if not own and previous_cue and found and _continues_rule(seg.text, found):
                cue = True
                carried_from[i] = previous_index
        previous_cue = own
        previous_index = i
        # A year alone ("自114年起規定改了") is a date, not a regulation number.
        if cue and found and not all(q.kind == "年份" for q in found):
            candidates.append(i)
    if not candidates:
        return RegulatoryResult(original, 0, 0)

    texts = backing() if callable(backing) else backing
    backed: set[Quantity] = set()
    for text in texts:
        backed.update(quantities(text[:BACKING_MAX_CHARS]))
    backed_years = {q for q in backed if q.kind == "年份"}

    def is_backed(q: Quantity) -> bool:
        if q.kind == "年份":
            return not backed_years or q in backed_years
        return q in backed

    drop: set[int] = set()
    remaining = 0
    for i in candidates:
        if all(is_backed(q) for q in sentence_quantities[i]):
            remaining += 1
        else:
            drop.add(i)
    if not drop:
        return RegulatoryResult(original, 0, remaining)

    # The number-less statement a deleted sentence continued has nothing left to introduce.
    for i in list(drop):
        lead = carried_from.get(i, -1)
        if lead >= 0 and lead not in sentence_quantities and not _ADVICE_RE.search(segs[lead].text):
            drop.add(lead)
    # A list header whose every item went has nothing left to introduce.
    items_by_header: dict[int, list[int]] = {}
    for item, header in header_of_item.items():
        items_by_header.setdefault(header, []).append(item)
    for header, items in items_by_header.items():
        if header not in drop and items and all(item in drop for item in items):
            if segs[header].text.rstrip().endswith(("：", ":")):
                drop.add(header)
    gone = drop | _line_breaks_of(segs, drop)
    text = _finish([seg.text for i, seg in enumerate(segs) if i not in gone])
    return RegulatoryResult(text, len(drop), remaining)


# ── B2: 「沒提到」 from material that was only partly read ─────────────────────

_NEG_SUBJECT = (
    r"(?:並?(?:沒有|沒)|並未|從未|不曾|都沒有|都沒|均未|皆未|都未|完全沒有|完全沒|根本沒有|根本沒|未曾)"
)
_NEG_VERB = (
    r"(?:提到|提及|談到|談及|談論|講到|說到|寫到|涉及|觸及|涵蓋|包含|包括|出現|討論|著墨)"
)
# 有沒有提到 asks; the lookbehind keeps it out.  The gaps are bounded.
# 「…都未提及」 says what a text does not contain; 「查不到相關報導」 only says a search came back empty.
_NEGATIVE_MENTION_PATTERN = (
    rf"(?<!有){_NEG_SUBJECT}[^，,。！？!?\n]{{0,10}}?{_NEG_VERB}"
    r"|(?<!有)未(?:有)?(?:提到|提及|談及|涉及|涵蓋|包含)"
)
_NEGATIVE_MENTION_RE = re.compile(_NEGATIVE_MENTION_PATTERN)
_NEGATIVE_COVERAGE_RE = re.compile(
    _NEGATIVE_MENTION_PATTERN
    + r"|(?:找不到|查不到|沒找到|沒查到|查無)[^，,。！？!?\n]{0,12}(?:提到|提及|相關|紀錄|報導|內容)"
)
# What is left of 「我查了這支影片的描述與搜尋到的摘要，都未提及…」 once the claim is gone says
# only that something was looked at (GP1 #6).
_LEAD_IN_ONLY_RE = re.compile(
    r"^(?:我|咪寶)?(?:有|已經|剛剛|剛才)?(?:查了|看了|找了|搜尋了|搜了|檢查了|讀了|翻了|查閱了|比對了|查過|看過)"
    r"[^，,。！？!?\n]{0,60}$"
)
_MATERIAL_WORDS = (
    "影片", "視頻", "直播", "節目", "描述", "說明欄", "標題", "字幕", "逐字稿", "內容", "文章", "報導",
    "貼文", "網頁", "連結", "搜尋", "資料", "摘要", "頻道",
)
# A question, a condition or advice is not a claim about the material.
_ASKING_RE = re.compile(r"[?？]|嗎")
_LEADING_DECOR_RE = reply_policy._LEADING_DECOR_RE
_CONDITION_OR_ADVICE_RE = re.compile(
    r"如果|若(?!干)|萬一|要是|假如|的話|建議(?:你|您|妳|大家|先|直接|自己)|請(?:你|您|妳)|可以先|記得"
)


def strip_negative_coverage(reply: str, partial: bool, search_only: bool = False) -> tuple[str, int]:
    """Drop 「影片沒有提到…」 clauses when the material was only partly read.

    ``partial``: the link gave no real content, the transcript was cut, or the
    reply disputes a quoted answer — a title, a cut transcript or search snippets
    cannot show that something is absent (NO_REPEAT_CONTRACT line 20).
    ``search_only``: nothing was read, the reply rests on a search; then only a
    claim about what a text does *not contain* (「都未提及」) goes, not 「查不到相關
    報導」, which is what a search that found nothing says.  Questions, conditions
    and advice stay; the check works per clause, so 「…，建議你自己看」 keeps its
    advice.  A sentence that is left with only a 「我查了…」 lead-in goes whole.
    Returns ``(text, dropped clauses)``.
    """
    if not (partial or search_only) or not isinstance(reply, str) or not reply.strip():
        return reply, 0
    claim = _NEGATIVE_COVERAGE_RE if partial else _NEGATIVE_MENTION_RE
    original = reply
    reply = reply[:REPLY_MAX_CHARS]
    segs = _segments(reply)
    out: list[str] = []
    dropped = 0
    previous_material = False
    for seg in segs:
        if seg.blank:
            previous_material = False
        if not seg.sentence:
            out.append(seg.text)
            continue
        material = any(w in seg.text for w in _MATERIAL_WORDS)
        if not claim.search(seg.text):
            out.append(seg.text)
            previous_material = material
            continue
        spans = _clause_spans(seg.masked)
        # a question, condition or piece of advice protects its own clause only:
        # 「描述裡沒有提到日本，你有看到嗎？」 loses the claim and keeps the question.
        # A claim about a person (「你沒有提到他的年紀」) is about the material only
        # when its own sentence says so.
        drop = {
            k for k, (s, e) in enumerate(spans)
            if claim.search(_squash(seg.text[s:e]))
            and not _ASKING_RE.search(seg.masked[s:e])
            and not _CONDITION_OR_ADVICE_RE.search(seg.text[s:e])
            and (material or (previous_material and not _PERSON_START_RE.match(_squash(seg.text[s:e]))))
        }
        previous_material = material
        if not drop:
            out.append(seg.text)
            continue
        dropped += len(drop)
        rebuilt = _rebuild_sentence(seg, spans, drop)
        if rebuilt and _LEAD_IN_ONLY_RE.match(_LEADING_DECOR_RE.sub("", rebuilt).rstrip(_TERMINATORS + " ")):
            rebuilt = ""
        out.append(rebuilt)
    if not dropped:
        return original, 0
    return _finish(out), dropped


# ── B3: hand-backs, pronoun guesses and chat recaps ──────────────────────────

_KINSHIP = (
    "爸爸", "媽媽", "爸媽", "爸", "媽", "哥哥", "姊姊", "姐姐", "弟弟", "妹妹", "阿公", "阿嬤", "阿媽", "奶奶", "爺爺",
    "外公", "外婆", "老公", "老婆", "老爸", "老媽", "本人", "當事人", "他本人", "她本人", "他自己", "她自己", "對方",
)
# A clause that opens with a person (「你沒有提到他的年紀」) is about what that person said.
_PERSON_START_RE = re.compile(
    r"^[ ,，]*(?:你們|你|您|妳|我們|我|他們|她們|他|她|"
    + "|".join(re.escape(k) for k in sorted(_KINSHIP, key=len, reverse=True)) + ")"
)
# 2026-10-29 (follow-up 5, item 1): the kinship words a search must not contain.  當事人 and
# 對方 name nobody in a question (「對方違規要怎麼檢舉」); they are left out here, at the
# source, so an alias, a display name or a turn label 當事人／對方 still blocks.  本人 and the
# rest stay: 本人 often follows a given name nobody configured (「林小禾本人入境…」).
_SEARCH_KINSHIP = tuple(k for k in _KINSHIP if k not in ("當事人", "對方"))


def _fold(text: str) -> str:
    """NFKC and casefolded: ＡＭＹ, Amy and amy are one name (GP2 #1)."""
    return unicodedata.normalize("NFKC", text or "").casefold()


class NameSourceUnavailable(RuntimeError):
    """The local list of family names cannot be read, or has no name in it."""


def _require_alias_file() -> None:
    """user_aliases.json is there, readable and has a name (``load_user_aliases``
    answers {} for all three, so a missing file would look like a family without names)."""
    try:
        data = json.loads(line_mentions._alias_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise NameSourceUnavailable("family alias file unavailable") from None
    if not isinstance(data, dict) or not any(isinstance(v, str) and v.strip() for v in data.values()):
        raise NameSourceUnavailable("family alias file has no names")


def configured_names(*, strict: bool = False) -> tuple[str, ...]:
    """The family aliases (short names and roles included) from the local
    configuration — the repo is public, no name is written in code.

    Not strict: empty when the files are missing.  ``strict``: raises
    ``NameSourceUnavailable`` instead of answering "no names".
    """
    try:
        if strict:
            _require_alias_file()
        names = tuple(line_mentions.configured_family_aliases())
    except NameSourceUnavailable:
        raise
    except Exception:
        if strict:
            raise NameSourceUnavailable("family aliases unreadable") from None
        return ()
    if strict and not names:
        raise NameSourceUnavailable("no family aliases")
    return names


def _display_name_terms(*, strict: bool = False) -> list[str]:
    """LINE display names the bot looked up for members without a local alias (as
    fact_filters and member_labels use them); a missing file is normal.

    ``strict`` (2026-10-29, only from ``name_terms(strict=True)``): a file that is there but
    cannot be read as a JSON object, or names that fail to load, raise
    ``NameSourceUnavailable`` instead of answering "no display names"."""
    if strict:
        try:
            line_mentions.require_display_names_readable()
        except Exception:
            raise NameSourceUnavailable("display name file unavailable") from None
    try:
        names = [n for n in line_mentions.display_names().values() if isinstance(n, str)][:200]
    except Exception:
        if strict:
            raise NameSourceUnavailable("display names unreadable") from None
        return []
    terms = list(names)
    try:
        from member_labels import _clean_member_label

        terms += [_clean_member_label(n) for n in names]
    except Exception:
        pass
    return terms


_LABEL_HEAD_RE = re.compile(r"^([^\s：:\[\]()（）]{1,20})：")
_MAX_SPEAKER_LABELS = 100


def speaker_labels(context: list[tuple[str, str]] | None) -> list[str]:
    """The names the program itself wrote in front of the family's turns (「稱呼：…」, the
    first line of a turn or of one member's message in a burst; indented lines are
    quoted or forwarded text and give no name)."""
    from speaker_turns import CONTINUATION_INDENT

    labels: dict[str, None] = {}
    for role, text in list(context or [])[-_BACKING_MAX_TURNS:]:
        if role != "user" or not isinstance(text, str):
            continue
        for line in text[:BACKING_MAX_CHARS].splitlines():
            if line.startswith(CONTINUATION_INDENT):
                continue
            match = _LABEL_HEAD_RE.match(line)
            if match and not any(bot in match.group(1) for bot in _BOT_NAMES):
                labels.setdefault(match.group(1))
                if len(labels) >= _MAX_SPEAKER_LABELS:
                    return list(labels)
    return list(labels)


_BOT_NAMES = ("咪寶", "咪宝", "咪寳", "米寶", "米宝", "米堡", "系統", "系统", "使用者")


def name_terms(
    *,
    context: list[tuple[str, str]] | None = None,
    extra: tuple[str, ...] | list[str] = (),
    strict: bool = False,
    kinship: tuple[str, ...] = _KINSHIP,
) -> tuple[str, ...]:
    """Every name a reply or a search must not use for a person, NFKC and casefolded,
    longest first: configured aliases (short names, roles), the ``kinship`` words, ``extra``
    (the sender), LINE display names and the labels in front of the conversation's turns.
    ``strict``: raise ``NameSourceUnavailable`` when the alias file is unreadable or empty,
    or the display-name file is there but cannot be read."""
    short = [*kinship, *extra, *configured_names(strict=strict)]
    terms = {t for t in (_fold(n).strip() for n in short if isinstance(n, str)) if 1 <= len(t) <= 12}
    long_ = [*_display_name_terms(strict=strict), *speaker_labels(context)]
    terms |= {t for t in (_fold(n).strip() for n in long_ if isinstance(n, str)) if 2 <= len(t) <= 24}
    return tuple(sorted(terms, key=lambda t: (-len(t), t)))


def family_name_list(extra: tuple[str, ...] | list[str] = ()) -> tuple[str, ...]:
    """The names of ``name_terms`` (aliases, kinship, display names and ``extra``, the speaker)."""
    return name_terms(extra=extra)


def search_deny_terms(context: list[tuple[str, str]] | None = None) -> tuple[str, ...]:
    """What a question must not contain before it is sent to a search provider: the
    names of ``name_terms``, the labels of the conversation's turns included (a label
    can come from a table the alias file does not hold), with the kinship words but
    當事人 and 對方 (``_SEARCH_KINSHIP``).

    Fails closed: ``NameSourceUnavailable`` when the alias file is unreadable or has no
    name, or the display-name file is there but cannot be read, so no question goes out
    without knowing whom it must not name.  Residual: a given name nobody configured,
    standing in the domain clause itself (「林小禾入境酒類免稅額度是多少」), goes out as written."""
    return name_terms(context=context, strict=True, kinship=_SEARCH_KINSHIP)


_ASK_VERB = r"(?:問問|問一下|問|詢問|去問|請教|確認|去找|找)"
_SUPERLATIVE = (
    r"(?:最快|最準|最直接|最清楚|比較快|比較準|比較清楚|就知道|就能知道|就可以知道|就清楚)"
)
_GAP = r"[^，,。！？!?\n]{0,6}?"
_HEDGE_PRONOUN = (
    r"(?:他們|她們|他|她|它|這個|那個|這間|那間|這裡|那裡|這邊|那邊|這|那)"
)
# (「如果這代表血壓偏高，建議先量三次」 is a condition on a reading, not a guess at who 「他」 is: no 代表.)
_HEDGE_RES = (
    re.compile(
        r"^(?:如果|若|假如|要是|倘若) ?[「『“\"]? ?" + _HEDGE_PRONOUN + r" ?[」』”\"]? ?"
        r"(?:是指|指的是|說的是|指|是說)"
    ),
    re.compile(
        r"(?:不確定|不清楚|不知道|無法確定|沒辦法確定) ?[「『“\"]? ?(?:他|她|它|這個|那個|這|那)"
        r" ?[」』”\"]? ?(?:是指|指的是|是誰|是哪|指誰|指哪)"
    ),
)
# 「某某剛剛說的那間店，週一公休」 describes a shop with what was said; it recaps nothing (no 的 after the verb).
_RECAP_TIME = r"(?:稍早|剛剛|剛才|先前|之前|早些時候|早先)(?:有)?(?:說|講|提到|表示|問過)(?!的)"
# Numbers or advice make a recap a reason to reply, not a recap.
_VALUE_RE = re.compile(r"建議|應該|最好|避免|記得|要注意|小心|不要|必須|需要|[0-9０-９]")


@functools.lru_cache(maxsize=16)
def _nonanswer_patterns(names: tuple[str, ...]) -> tuple[tuple[re.Pattern, ...], re.Pattern, re.Pattern]:
    """``names`` are already folded (NFKC, casefold); the clauses they are matched
    against are too."""
    alts = "|".join(re.escape(n) for n in names)
    slot = r"(?:@[\w一-鿿]{1,12}" + (("|" + alts) if alts else "") + r")"
    handbacks = (
        # 只有X知道 / 只有X才清楚
        re.compile(rf"只有 ?{slot}[^，,。！？!?\n]{{0,4}}?(?:才|自己)?(?:知道|清楚|曉得|能確定|能回答|說得準)"),
        # 直接問X最快 / 問X就知道 / X問一下最準 — an ask verb must stand next to the name,
        # so 「爸爸開車走國道比較快」 (nobody is told to ask anyone) stays.
        re.compile(rf"{_ASK_VERB} ?{slot} ?(?:本人)?{_GAP}{_SUPERLATIVE}"),
        re.compile(rf"{slot} ?(?:本人)? ?(?:問問|問一下|問|詢問|確認){_GAP}{_SUPERLATIVE}"),
        # 要問X才知道
        re.compile(rf"(?:要|得|需要|必須) ?(?:去)?問 ?{slot}{_GAP}(?:才知道|才清楚|才準|比較準)"),
        # 請X自己確認 (「讓媽媽自己決定」 is autonomy advice, not a hand-back)
        re.compile(rf"(?:請|由|讓) ?{slot} ?(?:自己|本人) ?(?:回答|確認|說明|回覆)"),
    )
    # 「直接問某某，他最清楚」: the comma splits it, so two clauses together are matched
    ask_then_pronoun = re.compile(
        rf"{_ASK_VERB} ?{slot} ?(?:本人)?[，,] ?(?:他|她|牠|對方|本人){_SUPERLATIVE}"
    )
    recap = re.compile(rf"^{slot} ?{_RECAP_TIME}")
    return handbacks, ask_then_pronoun, recap


def strip_nonanswer(
    reply: str,
    family_names: tuple[str, ...] | list[str] | None = None,
    addressed: bool = False,
    speaker: str = "",
    context: list[tuple[str, str]] | None = None,
) -> str:
    """Remove hand-backs, pronoun guesses and chat recaps; ``""`` when nothing is left.

    * 「只有{名}知道」「直接問{名}最快」「要問{名}才知道」「請{名}自己確認」 — always;
      an ask verb must stand next to the name.
    * 「如果「他」是指…」「不確定「她」指誰」 and 「{名}稍早說…」 (when the sentence has no
      number or advice) — only when nobody addressed the bot (``addressed`` False).
    {名} is a configured family alias, a kinship word, a LINE display name, a name in
    front of a turn of ``context``, an @mention in the reply or ``speaker``; names
    and clauses are compared NFKC and casefolded.  Deletion is per clause; a
    sentence whose remaining clauses carry no number or advice goes whole.  Fewer
    than 6 characters left → ``""``.
    """
    if not isinstance(reply, str) or not reply.strip():
        return reply
    original = reply
    reply = reply[:REPLY_MAX_CHARS]
    if family_names is None:
        names = name_terms(context=context, extra=(speaker,))
    else:
        names = tuple(sorted(
            {t for t in (_fold(n).strip() for n in (*family_names, speaker, *speaker_labels(context))
                         if isinstance(n, str)) if t},
            key=lambda n: (-len(n), n),
        ))
    handbacks, ask_then_pronoun, recap = _nonanswer_patterns(names)
    segs = _segments(reply)
    out: list[str] = []
    changed = False
    for seg in segs:
        if not seg.sentence:
            out.append(seg.text)
            continue
        spans = _clause_spans(seg.masked)
        sentence_has_value = bool(_VALUE_RE.search(seg.text))
        kinds: dict[int, str] = {}
        clauses = [_LEADING_DECOR_RE.sub("", _squash(_fold(seg.text[s:e]))) for s, e in spans]
        for k, clause in enumerate(clauses):
            if any(p.search(clause) for p in handbacks):
                kinds[k] = "handback"
            elif not addressed and any(p.search(clause) for p in _HEDGE_RES):
                kinds[k] = "hedge"
            elif not addressed and not sentence_has_value and recap.search(clause):
                kinds[k] = "recap"
        for k in range(len(clauses) - 1):
            if k not in kinds and ask_then_pronoun.search(_squash(_fold(seg.text[spans[k][0]:spans[k + 1][1]]))):
                kinds[k] = kinds[k + 1] = "handback"
        if not kinds:
            out.append(seg.text)
            continue
        changed = True
        rest = [k for k in range(len(spans)) if k not in kinds]
        # Without a hedge to keep an answer after: what the other clauses say
        # without any number or advice only pointed at the hand-back or recap.
        if "hedge" not in kinds.values() and not any(
            _VALUE_RE.search(seg.text[spans[k][0]:spans[k][1]]) for k in rest
        ):
            continue
        out.append(_rebuild_sentence(seg, spans, set(kinds)))
    if not changed:
        return original
    return _finish(out)


# ── what was read: partial material, transcripts and their keywords ──────────

class MaterialRecord(list):
    """What `main._recording_link_content` hands out.  It stays the list of
    "real content was read" marks it always was; ``partial`` adds why the
    material may not be the whole story (set by the fetchers themselves, never
    guessed from the text of the prompt)."""

    def __init__(self) -> None:
        super().__init__()
        self.partial: list[str] = []


def note_partial(record, reason: str) -> None:
    """Record, on the active recorder, that the material was read only in part."""
    partial = getattr(record, "partial", None)
    if isinstance(partial, list) and len(partial) < 20:
        partial.append(reason)


def is_partial(record) -> bool:
    return bool(getattr(record, "partial", None))


_VIDEO_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "tiktok.com", "fb.watch")
_VIDEO_PATH_HOSTS = {
    "instagram.com": ("/reel/", "/reels/", "/tv/"),
    "facebook.com": ("/reel/", "/watch", "/videos/", "/share/v/", "/share/r/"),
}


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def has_video_link(text: str) -> bool:
    """Whether ``text`` (a message, or a batch with its quotes) links a video."""
    for url in reply_policy.link_urls(text or ""):
        try:
            parts = urlsplit(url if "://" in url else "https://" + url)
            host = (parts.hostname or "").lower()
        except ValueError:
            continue
        if any(_host_matches(host, d) for d in _VIDEO_HOSTS):
            return True
        for domain, prefixes in _VIDEO_PATH_HOSTS.items():
            if _host_matches(host, domain) and any(parts.path.startswith(p) for p in prefixes):
                return True
    return False


def silent_video_comment(text: str, link_content) -> bool:
    """A burst that only comments on a video nothing could be read of, asking nothing.

    The 9/26 rule for bare links, widened (B2.2): with a video link in the batch (a
    family member's quote of one too, not the bot's own quoted reply), no real
    content read from any link and no question on any line of the batch, a reply
    could only retell a title.  Not silent when the batch disputes a reply of the
    bot's own, asks anything anywhere, or holds another line that states something
    checkable (a number with a unit, a rule or a limit: a wrong 免稅 figure next to a
    video comment still deserves its correction).  The comment on the video itself,
    on its own line or beside the link, is not such a line.
    """
    if link_content or not isinstance(text, str) or not text:
        return False
    visible = strip_bot_quotes(text)
    if not has_video_link(visible) or disputes_quoted_bot(text):
        return False
    if reply_policy.asks_question(visible, addressed=True):
        return False
    video_words = {
        reply_policy._user_words(line).strip()
        for line in visible.splitlines()
        if ("." in line or "http" in line) and has_video_link(line)
    }
    other = "\n".join(
        line for line in reply_policy._user_words(visible).splitlines()
        if line.strip() and line.strip() not in video_words
    )
    return not (has_any_cue(other) or quantities(other))


# ── B2.4: a family member telling the bot its own reply was wrong ────────────

DISPUTE_NOTE = reply_policy.DISPUTE_NOTE
_BOT_QUOTE_BLOCK_RE = re.compile(r"-{3} ?原始訊息 ?開始 ?-{3}\s*\[我 \(bot\)\]\s*[:：]")


def quotes_bot_reply(text: str) -> bool:
    return bool(_BOT_QUOTE_BLOCK_RE.search(text or ""))


def disputes_quoted_bot(text: str) -> bool:
    """The current message disputes a bot reply that it quotes.

    Read from the prompt text itself (quote block plus the typed words), so it
    works wherever the quote travels: an @mention, a burst, the research path.
    """
    if not quotes_bot_reply(text):
        return False
    return reply_policy.disputes_bot_claim(reply_policy._user_words(text))


# ── B8.2: only what the user typed may name a stock ──────────────────────────

_USER_BLOCK_LABEL_RE = re.compile(r"原始訊息|目前回覆|群組訊息 ?\d+")
_MATERIAL_LEAD_RE = re.compile(r"^（以下是[^\n]*）[ \t]*$", re.MULTILINE)


def user_typed_text(text: str) -> str:
    """``text`` without the link material the program put into the prompt.

    Titles and descriptions of a pre-read video say 「TV」「APP」「AI」 and
    nothing the family asked about.  What the family wrote — their quoted
    messages and the current reply included — stays; so does the rest of the
    prompt, as before.  Only blocks of pre-read material (影片資訊, 網頁內容,
    Reddit, TikTok, 影片分析 …) and their lead lines go.
    """
    if not isinstance(text, str) or not text:
        return text or ""

    def keep_user_blocks(match: re.Match) -> str:
        return match.group(0) if _USER_BLOCK_LABEL_RE.fullmatch(match.group("label").strip()) else ""

    text = reply_policy._MATERIAL_BLOCK_RE.sub(keep_user_blocks, text)
    return _MATERIAL_LEAD_RE.sub("", text)


# ── transcripts: cues with times, and the windows the model is shown ─────────

SUBTITLE_BUDGET = 6000  # characters of transcript in the prompt (was 3000)
_OPENING_CHARS = 1500
_CHUNK_CHARS = 300
# One cue can be a whole page of text (an auto-caption without breaks): it is cut into
# pieces of this size before the windows are chosen, and the text handed out never
# exceeds the budget by more than the marks between the windows (GP2 #2).
_MAX_PIECE_CHARS = 300
_JOIN_ALLOWANCE = 40
_MAX_KEYWORD_GRAMS = 200
_CUE_TIME_RE = re.compile(r"^(?:(\d{1,3}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?[ \t]*-->")
_TAG_RE = re.compile(r"<[^<>]{0,100}>")
# Function words never make a keyword.
_STOP_CHARS = frozenset("的了是在有和與及也都就還又不沒很嗎呢吧啊喔哦耶啦呀我你妳他她它們這那個之以於而但或把被讓給對向跟從到")


def is_hls_manifest(body: str) -> bool:
    """An HLS playlist (#EXTM3U) some live replays serve in place of a VTT file."""
    return (body or "").lstrip("\ufeff \t\r\n").startswith("#EXTM3U")


def parse_vtt_cues(vtt_text: str) -> list[tuple[float, str]]:
    """``(start seconds, line)`` for each caption line of a WebVTT file.

    Header, NOTE and style blocks are skipped; tags are removed; a line equal to
    the one before it (rolling auto-captions repeat it) is kept once, at the
    time it first appeared.
    """
    cues: list[tuple[float, str]] = []
    start = 0.0
    in_cue = False
    last = None
    for raw in (vtt_text or "").splitlines():
        line = raw.strip()
        timing = _CUE_TIME_RE.match(line)
        if timing:
            hours, minutes, seconds, millis = timing.groups()
            start = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int((millis or "0").ljust(3, "0")) / 1000
            in_cue = True
            continue
        if not line:
            in_cue = False
            continue
        if not in_cue:
            continue
        text = _TAG_RE.sub("", line).strip()
        if text and text != last:
            cues.append((start, text))
            last = text
    return cues


def format_clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def keyword_source(prompt_text: str) -> str:
    """The words of a prompt that say what the user is after: their message and
    the messages they quoted — not the bot's wrappers, links or pre-read material."""
    text = reply_policy.strip_links(user_typed_text(prompt_text or ""))
    for fixed in reply_policy._system_texts():
        text = text.replace(fixed, "")
    text = reply_policy._WRAPPER_TEXT_RE.sub("", text)
    text = re.sub(r"-{3} ?[^\n-]{1,24}? ?(?:開始|結束) ?-{3}", "", text)
    text = re.sub(r"\[[^\]\n]{1,20}\]\s*[:：]", "", text)
    return text.replace(DISPUTE_NOTE, "")[:1200]


def keyword_grams(text: str) -> list[str]:
    """Bigrams of Chinese words and whole ASCII words the user mentioned."""
    s = unicodedata.normalize("NFKC", text or "").lower()
    seen: dict[str, None] = {}
    for run in re.findall(r"[\u3400-\u9fff]+", s):
        for i in range(len(run) - 1):
            gram = run[i:i + 2]
            if gram[0] not in _STOP_CHARS and gram[1] not in _STOP_CHARS:
                seen.setdefault(gram)
    for word in re.findall(r"[a-z0-9]{2,}", s):
        seen.setdefault(word)
    return list(seen)[:_MAX_KEYWORD_GRAMS]


class SubtitleWindows(NamedTuple):
    text: str
    complete: bool  # the whole transcript is in ``text``
    ranges: list[tuple[float, float]]  # seconds covered, in order


def select_subtitle_windows(
    cues: list[tuple[float, str]],
    keywords: str = "",
    duration: float | None = None,
    budget: int = SUBTITLE_BUDGET,
) -> SubtitleWindows:
    """The transcript, or its opening plus the stretch the user's words point at.

    Up to ``budget`` characters: the opening segment (the start of the video)
    and the window where the keywords are densest, each keyword weighted by how
    rare it is in this transcript (IDF — 「我們」 everywhere counts for nothing,
    「日本」 in one stretch counts a lot).  No keywords, or none found → the
    opening only.  The text opens with a note of the time ranges it covers and
    the full length, so neither the model nor a reader mistakes it for the
    whole video.
    """
    lines = [text for _t, text in cues]
    total = sum(len(t) + 1 for t in lines)
    if total <= budget:
        return SubtitleWindows(
            reply_policy.neutralize_material("\n".join(lines)), True,
            [(cues[0][0], cues[-1][0])] if cues else [],
        )
    # A cue longer than a chunk is cut into pieces, so no chunk is bigger than a cue's slice.
    if any(len(text) > _MAX_PIECE_CHARS for text in lines):
        cues = [
            (t, text[i:i + _MAX_PIECE_CHARS])
            for t, text in cues
            for i in range(0, max(len(text), 1), _MAX_PIECE_CHARS)
        ]
        lines = [text for _t, text in cues]
    # Chunks of about _CHUNK_CHARS: (first cue, end cue, characters)
    chunks: list[tuple[int, int, int]] = []
    first, size = 0, 0
    for i, text in enumerate(lines):
        size += len(text) + 1
        if size >= _CHUNK_CHARS:
            chunks.append((first, i + 1, size))
            first, size = i + 1, 0
    if size:
        chunks.append((first, len(lines), size))
    opening_end, used = 0, 0
    while opening_end < len(chunks) and used < _OPENING_CHARS:
        used += chunks[opening_end][2]
        opening_end += 1
    room = budget - used
    best = (0.0, opening_end, opening_end)
    grams = keyword_grams(keywords)
    if grams and opening_end < len(chunks):
        texts = ["\n".join(lines[a:b]).lower() for a, b, _n in chunks]
        count = len(chunks)
        weights: dict[str, float] = {}
        for gram in grams:
            df = sum(1 for t in texts if gram in t)
            if df:
                weights[gram] = max(0.0, math.log(count / df))
        scores = [
            sum(w * min(t.count(g), 3) for g, w in weights.items() if w)
            for t in texts
        ]
        j, window_chars, window_score = opening_end, 0, 0.0
        for i in range(opening_end, len(chunks)):
            if j < i:
                j, window_chars, window_score = i, 0, 0.0
            while j < len(chunks) and window_chars + chunks[j][2] <= room:
                window_chars += chunks[j][2]
                window_score += scores[j]
                j += 1
            if window_score > best[0]:
                best = (window_score, i, j)
            if j > i:
                window_chars -= chunks[i][2]
                window_score -= scores[i]
    picked: list[tuple[int, int]] = []  # chunk ranges, in order
    if best[0] > 0:
        picked = [(0, opening_end)]
        if best[1] == opening_end:  # touches the opening: one stretch
            picked = [(0, best[2])]
        else:
            picked.append((best[1], best[2]))
    else:  # nothing to point at: the opening, as far as the budget goes
        end, taken = 0, 0
        while end < len(chunks) and taken + chunks[end][2] <= budget:
            taken += chunks[end][2]
            end += 1
        picked = [(0, max(end, 1))]
    ranges: list[tuple[float, float]] = []
    parts: list[str] = []
    for a, b in picked:
        first_cue, end_cue = chunks[a][0], chunks[b - 1][1]
        parts.append("\n".join(lines[first_cue:end_cue]))
        stop = cues[end_cue][0] if end_cue < len(cues) else (duration or cues[-1][0])
        ranges.append((cues[first_cue][0], stop))
    length = duration if duration else cues[-1][0]
    labels = ["開頭" if k == 0 and picked[0][0] == 0 else "關鍵字最密的一段" for k in range(len(picked))]
    if len(picked) == 1 and best[0] > 0 and picked[0][0] == 0 and picked[0][1] > opening_end:
        labels = ["開頭到關鍵字最密的一段"]
    covered = "、".join(
        f"{format_clock(a)}–{format_clock(b)}（{label}）" for (a, b), label in zip(ranges, labels)
    )
    note = (
        f"（字幕只附了部分時段：{covered}；全長 {format_clock(length)}。"
        "其餘時段沒有附上，不能因為這裡面沒有就說影片沒提到。）"
    )
    body = "\n…（略過的時段）…\n".join(parts)
    if len(body) > budget + _JOIN_ALLOWANCE:  # the budget is a promise, not a hint
        body = body[:budget] + "…（略）"
    return SubtitleWindows(reply_policy.neutralize_material(note + "\n" + body), False, ranges)


_subtitle_hint = threading.local()


@contextmanager
def subtitle_hint(prompt_text: str):
    """Tell the transcript reader, in this thread, what the user is asking about."""
    previous = getattr(_subtitle_hint, "text", "")
    try:
        _subtitle_hint.text = keyword_source(prompt_text)
    except Exception:  # a hint is a nicety: without it the transcript's opening is shown
        _subtitle_hint.text = ""
    try:
        yield
    finally:
        _subtitle_hint.text = previous


def current_subtitle_hint() -> str:
    return getattr(_subtitle_hint, "text", "")


# ── one call for the reply exit ──────────────────────────────────────────────

class GuardResult(NamedTuple):
    text: str  # '' when nothing worth sending is left
    negative_dropped: int = 0  # 「沒提到」 clauses removed (B2)
    numbers_dropped: int = 0  # regulation sentences removed (B1)
    nonanswer: bool = False  # something removed by B3


def apply_reply_guards(
    reply: str,
    *,
    source_text: str = "",
    request_text: str = "",
    material_text: str = "",
    evidence_text: str = "",
    context: list[tuple[str, str]] | None = None,
    addressed: bool = True,
    partial: bool = False,
    search_only: bool = False,
    speaker: str = "",
) -> GuardResult:
    """B2, B1, B3 in that order (see the module docstring).

    ``partial``: the material was read only in part.  ``search_only``: nothing was
    read and the reply rests on a search (see ``strip_negative_coverage``).
    ``text`` is ``""`` when nothing worth sending is left.  What to do then is
    the caller's: silence, except that a direct question whose answer lost a
    regulation number (``numbers_dropped``) gets `unconfirmed_regulation_reply`
    instead of nothing.
    """
    text, negative = strip_negative_coverage(reply, partial, search_only)
    if negative and not text:
        return GuardResult("", negative_dropped=negative)
    # What backs a number is gathered only if a sentence of the reply needs it.
    result = strip_unbacked_regulatory_numbers(
        text,
        lambda: regulation_backing(source_text, request_text, material_text, evidence_text, context=context),
    )
    if result.dropped:
        text = result.text
        if not text:
            return GuardResult("", negative_dropped=negative, numbers_dropped=result.dropped)
    cleaned = strip_nonanswer(text, addressed=addressed, speaker=speaker, context=context)
    return GuardResult(cleaned, negative, result.dropped, nonanswer=cleaned != text)


def unconfirmed_regulation_reply(text: str, *, addressed: bool, numbers_dropped: int) -> str:
    """``text``; or, when someone asked the bot directly and a regulation number was
    deleted from an answer that is now empty, the fixed line saying so instead of
    silence (GP1 #6).  A burst nobody asked about stays silent."""
    if text or not (addressed and numbers_dropped):
        return text
    return REGULATORY_UNCONFIRMED_REPLY
