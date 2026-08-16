"""Local, deterministic helpers for adjudicating correction-memory rules.

This module deliberately has no dependency on the bot memory layer or on a
remote/model service.  Group scoping is a caller responsibility: callers must
only pass rules belonging to the group currently being adjudicated.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
import unicodedata
from typing import Any

__all__ = [
    "ORGANIC_CORRECTION_PREFIXES",
    "default_adjudicator",
    "combine_semantic_contexts",
    "extract_candidate_rule",
    "is_ambiguous",
    "is_question_like",
    "normalize_rule",
    "semantic_context",
]

ORGANIC_CORRECTION_PREFIXES = (
    "不對",
    "不是這樣",
    "不是這意思",
    "你誤會",
    "妳誤會",
    "我說的是",
    "我問的是",
    "我是說",
    "我意思是",
    "我意思不是",
    "重來",
    "請重答",
    "你答錯",
    "妳答錯",
    "答錯了",
    "胡說",
    "亂講",
    "不是我要的",
)
_ORGANIC_CORRECTION_PREFIX_PATTERN = "(?:" + "|".join(
    re.escape(prefix)
    for prefix in sorted(ORGANIC_CORRECTION_PREFIXES, key=len, reverse=True)
) + ")"
_QUESTION_CORRECTION_PREFIX_PATTERN = "(?:" + "|".join(
    re.escape(prefix)
    for prefix in sorted(
        (*ORGANIC_CORRECTION_PREFIXES, "不是"), key=len, reverse=True
    )
) + ")"
_QUESTION_WORDS = (
    "為什麼", "為啥", "怎麼", "怎會", "如何", "有沒有", "是否", "哪些",
    "哪個", "哪裡", "哪邊", "哪天", "啥時", "能否", "可否", "可不可以",
    "能不能", "要去哪", "什麼時候", "何時", "幾時", "幾點", "誰", "什麼",
)
_QUESTION_WORD_PATTERN = "(?:" + "|".join(
    re.escape(word) for word in sorted(_QUESTION_WORDS, key=len, reverse=True)
) + ")"
_MAX_NEGATED_INTENT_CLAUSE_CHARS = 200


_RULE_LABEL_RE = re.compile(
    r"^(?:候選規則|規則|教訓|糾正規則|candidate[ _-]?rule|canonical[ _-]?rule)\s*[:：]\s*(.+)$",
    re.IGNORECASE,
)
_CORRECTION_LINE_RE = re.compile(
    r"^(?:user|使用者)\s*糾正\s*[:：]\s*(.+)$",
    re.IGNORECASE,
)
_LINE_PREFIX_RE = re.compile(r"^\s*(?:[-*•]+|\d+[.)、])\s*")
_TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])\s*:\s*([0-5]\d)(?!\d)")
_DEPARTURE_RE = re.compile(r"出發|起程|啟程")
_NEGATED_DEPARTURE_RE = re.compile(
    r"(?:不要|別|不可|不能|不應該|不應|不該|不需要|無需|不必|勿|"
    r"不准|禁止|不得|嚴禁|無須|毋須|不宜|避免|請勿|"
    r"不是|並非|非)\s*(?:再\s*)?"
    r"(?:(?:把|將)\s*(?:\d{1,2}\s*:\s*\d{2})?\s*)?"
    r"(?:(?:必須|務必|應該|應當|應|需要|需)\s*)?"
    r"(?:(?:(?:誤)?(?:標成|寫成|當成|視為|說成|算是|算作)|是)\s*)?"
    r"(?:預計|原定|實際)?\s*(?:的)?\s*(?:出發|起程|啟程)"
)
_NEGATIVE_MODAL_RE = re.compile(
    r"不要|別|不可|不能|不准|禁止|不得|嚴禁|不應該|不應|不該|"
    r"不需要|無需|無須|毋須|不必|不用|不宜|避免|請勿|勿|不是|並非"
)
_POSITIVE_DEPARTURE_RE = re.compile(
    r"(?<!不)(?:必須|務必|應該|應當|應|需|需要).{0,20}"
    r"(?:出發|起程|啟程)"
)
_QUESTION_LIKE_RE = re.compile(
    r"[?？]|"
    rf"^(?:請問|想知道|我想問|{_QUESTION_WORD_PATTERN})|"
    r"^(?:(?:不對|我是說|我是問|我問的是|我的意思是|你誤會(?:了)?)"
    r"\s*[,，:：]?\s*)+"
    r"(?:為什麼|怎麼|如何|哪裡|能否|是否|什麼時候|何時|幾時|幾點|誰|什麼)|"
    r"(?:有什麼(?:風險|問題|差別|影響)|有何(?:風險|問題|差別|影響)|"
    r"會怎樣|怎麼辦|好不好|行不行|是什麼意思|要問誰|"
    r"幾點(?:出發)?|多久|多少|多長|多遠|在哪(?:裡)?|哪一種|哪種)$|"
    r"^(?:自駕|開車(?:時)?|回答(?:時)?|回覆(?:時)?|咪寶|bot).{0,40}"
    r"(?:能否|可否).{0,12}$|(?:嗎|呢)$"
)
_CORRECTION_PREFACED_QUESTION_RE = re.compile(
    rf"^(?:{_QUESTION_CORRECTION_PREFIX_PATTERN}(?:了)?"
    r"(?:啦|啊|喔|哦|呀|嘛)?\s*[,，:：]?\s*)*"
    r"(?:(?:我)?(?:並不是|並非|沒有|不是|不|沒)(?:在|要|想){0,2}(?:問|知道)"
    rf"[^,，。.!！？?；;]{{0,{_MAX_NEGATED_INTENT_CLAUSE_CHARS}}}"
    r"(?:[,，。.!；;]\s*(?:(?:而是|但是|但|可是|不過|只是)\s*)?|"
    r"\s*(?:而是|但是|但|可是|不過|只是)\s*))*"
    r"(?:(?:我的意思是|我意思是)?(?:我)?"
    r"(?:其實|只是|就是|才|原本|主要|大概|可能|真正|是|要|想|正在|在){0,4}"
    r"(?:問|知道|好奇|請教|請問)(?:一下)?(?:你|妳|bot|咪寶|使用者)?"
    r"(?:的是|的就是|的問題是)?|"
    r"我意思不是(?:在)?問|我是說|我是問|我問的是|我問|我的問題是|"
    r"我的意思是|我意思是(?:問)?)?\s*"
    r"(?:還有|還|有|都|到底|究竟){0,2}"
    rf"{_QUESTION_WORD_PATTERN}"
)
_A_NOT_A_RE = re.compile(r"([\u3400-\u9fff]{1,4})不\1")

_GENERIC_AMBIGUOUS = {
    "不對",
    "不是這樣",
    "不要這樣",
    "不要這樣做",
    "不要這樣回答",
    "不要亂講",
    "不要亂說",
    "別這樣",
    "別這樣做",
    "別亂講",
    "別亂說",
    "改一下",
    "請改正",
    "請修正",
    "記住",
    "重來",
    "錯了",
}

_RULE_TEXT_KEYS = (
    "canonical_rule",
    "rule",
    "candidate_rule",
    "content",
    "text",
    "normalized_rule",
)
_RULE_ID_KEYS = ("rule_id", "id", "canonical_rule_id")

_DETAIL_COVERAGE_CUE_RE = re.compile(
    r"納入|未提|未能|忽略|漏掉|僅複述|細節|"
    r"不要忘記.{0,8}(?:提及|提到|納入|包含)|"
    r"具體.{0,10}(?:回應|回答|提及|提到)|"
    r"(?:回應|回答|提及|提到).{0,10}(?:具體|細節|特定)"
)
_DETAIL_DIRECT_EXCLUDE_RE = re.compile(
    r"(?:不要|別|不可|不能|不准|不得|嚴禁|不應該|不應|不該|不需要|"
    r"無需|無須|毋須|不必|不用|不宜|避免|禁止|勿)\s*"
    r"(?:再\s*)?(?:具體\s*)?(?:納入|提及|提到|包含)"
)
_DETAIL_PRIVACY_EXCLUDE_RE = re.compile(
    r"(?:不要|別|不可|不能|不准|不得|嚴禁|不應該|不應|不該|不需要|"
    r"無需|無須|毋須|不必|不用|不宜|避免|禁止|勿)\s*"
    r"(?:再\s*)?(?:公開|洩漏|揭露|顯示)"
)
_DETAIL_AFFIRMATIVE_OMIT_RE = re.compile(
    r"(?<!不)(?:應該|應當|應|要|必須|務必|需要|直接)\s*"
    r"(?:忽略|漏掉|省略|排除|略過)|^(?:忽略|漏掉|省略|排除|略過)"
)
_DETAIL_POSITIVE_INCLUDE_RE = re.compile(
    r"未提|未能.{0,10}(?:回應|回答|提及|提到)|僅複述|"
    r"(?:bot|咪寶|回答|回覆|先前回答).{0,8}(?:忽略|漏掉)|"
    r"不要忘記.{0,8}(?:提及|提到|納入|包含)|"
    r"(?:不要|別|不可|不能|不得|避免)(?:再)?(?:忽略|漏掉)|"
    r"(?:應該|應當|應|要|必須|務必|需要|直接).{0,10}"
    r"(?:納入|提及|提到|包含)"
)
_PELVIS_ALIGNMENT_RE = re.compile(
    r"骨盆.{0,12}(?:傾斜|前傾|後傾|高低|不對稱|歪斜)|"
    r"(?:傾斜|前傾|後傾|高低|不對稱|歪斜|右高左低|左高右低|左低右高|右低左高)"
    r".{0,12}骨盆"
)


def _protected_numbers(text: Any) -> tuple[str, ...]:
    value = unicodedata.normalize("NFKC", str(text or ""))
    return tuple(re.findall(r"\d+(?:[.:/-]\d+)*", value))


def _protected_structured_tokens(text: Any) -> tuple[str, ...]:
    value = unicodedata.normalize("NFKC", str(text or ""))
    return tuple(
        re.sub(r"\s+", "", token.lower())
        for token in re.findall(
            r"[A-Za-z0-9]+(?:\s*[.:/-]\s*[A-Za-z0-9]+)+",
            value,
        )
    )


def _protected_value_signature(text: Any) -> tuple[tuple[str, ...], str]:
    """Preserve value-bearing punctuation before any lexical shortcut.

    False splits are safer than merging two rules that differ by a date,
    sign, comparator, percentage, currency, code separator, or direction.
    Ordinary sentence punctuation is deliberately excluded.
    """

    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    numbers = tuple(
        re.sub(r"\s+", "", token)
        for token in re.findall(
            r"(?:\(\s*)?(?:[$¥￥€£₩₹₽₿]\s*)?"
            r"(?:[<>≤≥≠≦≧=!~≈∼]{1,3}\s*)?"
            r"[+\-−±↑↓↗↘↔→←⇒⇐]?\s*"
            r"(?:\d+(?:\s*[.:/-]\s*\d+)*|\.\s*\d+)"
            r"(?:\s*(?:%|‰|‱)|\s*°\s*[cf])?(?:\s*\))?",
            value,
        )
    )
    code_patterns = (
        r"(?<!\w)\.[A-Za-z_][A-Za-z0-9_]*(?!\w)",
        r"(?<!\w)_+[A-Za-z0-9]+(?:_+[A-Za-z0-9]+)*_*(?!\w)",
        r"(?<!\w)[A-Za-z0-9]+(?:_+[A-Za-z0-9]+)+_*(?!\w)",
        r"(?<!\w)[A-Za-z][A-Za-z0-9]*\+{1,2}(?!\w)",
        r"(?<!\w)[A-Za-z0-9_\u3400-\u9fff]+\s*"
        r"(?:[:：](?:=|:)|[^\w\s，。；、,;:：]+)\s*"
        r"[A-Za-z0-9_\u3400-\u9fff]+(?!\w)",
        r"[A-Za-z0-9]+(?:\s*(?:::|[.:/-])\s*[A-Za-z0-9]+)+",
    )
    structured_values = {
        re.sub(r"\s+", "", token)
        for pattern in code_patterns
        for token in re.findall(pattern, value)
    }
    structured = tuple("code:" + token for token in sorted(structured_values))
    currency_marks = "".join(
        char for char in value if unicodedata.category(char) == "Sc"
    )
    return numbers + structured, currency_marks


def _detail_coverage_semantics(
    text: Any,
) -> tuple[str, str, bool] | None:
    """Recognize only the production-observed pelvic-alignment rule family.

    Generic local n-gram similarity is unsafe for private/entity-bearing
    corrections.  This bounded family is the one repeated production cluster
    that motivated the feature.  Everything else remains exact/fail-distinct.
    """

    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    if (
        not _DETAIL_COVERAGE_CUE_RE.search(value)
        or not _PELVIS_ALIGNMENT_RE.search(value)
    ):
        return None
    direction = ""
    if re.search(r"右高左低|左低右高", value):
        direction = "right-high-left-low"
    elif re.search(r"左高右低|右低左高", value):
        direction = "left-high-right-low"
    elif "前傾" in value:
        direction = "anterior-tilt"
    elif "後傾" in value:
        direction = "posterior-tilt"
    explicit_exclude = bool(
        _DETAIL_DIRECT_EXCLUDE_RE.search(value)
        or _DETAIL_PRIVACY_EXCLUDE_RE.search(value)
        or _DETAIL_AFFIRMATIVE_OMIT_RE.search(value)
    )
    if explicit_exclude:
        return "pelvis-alignment-detail", direction, False
    if not _DETAIL_POSITIVE_INCLUDE_RE.search(value):
        return None
    return "pelvis-alignment-detail", direction, True


def semantic_context(text: Any) -> str:
    """Return the persisted bounded semantic state for one rule."""
    detail = _detail_coverage_semantics(text)
    if detail is None:
        return ""
    family, direction, desired_include = detail
    return f"{family}|{direction or '-'}|{'include' if desired_include else 'exclude'}"


def combine_semantic_contexts(texts: Iterable[Any]) -> str:
    """Aggregate persisted member semantics conservatively for one rule."""
    parsed = [
        context.split("|")
        for context in (semantic_context(text) for text in texts)
        if context
    ]
    if not parsed or not all(
        len(parts) == 3 and parts[0] == parsed[0][0] for parts in parsed
    ):
        return ""
    directions = {parts[1] for parts in parsed if parts[1] != "-"}
    intents = {parts[2] for parts in parsed}
    direction = next(iter(directions)) if len(directions) == 1 else (
        "mixed" if len(directions) > 1 else "-"
    )
    intent = next(iter(intents)) if len(intents) == 1 else "mixed"
    return f"{parsed[0][0]}|{direction}|{intent}"


def _mapped_detail_semantics(value: Any, fallback_text: str):
    if isinstance(value, Mapping):
        persisted = str(value.get("semantic_context") or "")
        parts = persisted.split("|")
        if len(parts) == 3 and parts[0] == "pelvis-alignment-detail":
            if parts[1] == "mixed" or parts[2] not in {"include", "exclude"}:
                return parts[0], "mixed", False
            direction = "" if parts[1] == "-" else parts[1]
            include = parts[2] == "include"
            return parts[0], direction, include
    return _detail_coverage_semantics(fallback_text)


def _clean_rule_text(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    text = _LINE_PREFIX_RE.sub("", text)
    text = text.strip(" \t\r\n`'\"")
    return re.sub(r"\s+", " ", text).strip()


def is_question_like(text: Any) -> bool:
    """Whether the text is a question rather than a reusable correction."""
    cleaned = _clean_rule_text(text)
    return bool(
        cleaned
        and (
            _QUESTION_LIKE_RE.search(cleaned)
            or _CORRECTION_PREFACED_QUESTION_RE.search(cleaned)
            or _A_NOT_A_RE.search(cleaned)
        )
    )


def extract_candidate_rule(content: Any, candidate_rule: Any = "") -> str:
    """Extract one correction rule, preferring an explicit candidate value.

    Labeled lines are preferred when ``content`` also contains explanation or
    metadata.  With no label, the first non-empty line is returned so the
    function remains useful for plain user corrections.
    """

    explicit = _clean_rule_text(candidate_rule)
    if explicit:
        return explicit

    if content is None:
        return ""
    if isinstance(content, Mapping):
        for key in _RULE_TEXT_KEYS:
            extracted = _clean_rule_text(content.get(key))
            if extracted:
                return extracted
        return ""

    lines = [line.strip() for line in str(content).splitlines() if line.strip()]
    for line in lines:
        cleaned_line = _LINE_PREFIX_RE.sub("", line).strip()
        match = _RULE_LABEL_RE.match(cleaned_line)
        if match:
            return _clean_rule_text(match.group(1))

    # Historical organic rows without a Gemini summary contain three lines:
    # original question, bot answer, and the user's correction.  The final
    # correction is the reusable rule; the first line is merely context.
    for line in lines:
        cleaned_line = _LINE_PREFIX_RE.sub("", line).strip()
        match = _CORRECTION_LINE_RE.match(cleaned_line)
        if match:
            return _clean_rule_text(match.group(1))

    return _clean_rule_text(lines[0]) if lines else ""


def _transport_time_semantics(text: Any) -> tuple[str, bool] | None:
    """Return ``(time, is_departure)`` for explicit departure-time claims."""

    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    times = _TIME_RE.findall(value)
    if len(times) != 1 or not _DEPARTURE_RE.search(value):
        return None

    time_value = f"{int(times[0][0]):02d}:{times[0][1]}"
    # Resolve contrastive corrections by the clause that directly asserts the
    # departure meaning.  A negation about arrival ("別標成到站") must not
    # negate a later explicit "必須標成出發" assertion.
    negative_matches = list(_NEGATED_DEPARTURE_RE.finditer(value))
    positive_matches = [
        match
        for match in _POSITIVE_DEPARTURE_RE.finditer(value)
        if not any(
            negative.start() <= match.start() < negative.end()
            for negative in negative_matches
        )
    ]
    direct_positive_matches = list(
        re.finditer(
            r"(?:^|[，,；;。.!！？?\s])(?:應該|應當|必須|務必|要|是|為)\s*"
            r"(?:預計|原定|實際)?\s*(?:的)?\s*"
            r"(?:(?:標成|寫成|當成|視為|說成)\s*)?"
            r"(?:出發|起程|啟程)",
            value,
        )
    )
    positive_matches.extend(
        match
        for match in direct_positive_matches
        if not any(
            negative.start() <= match.start() < negative.end()
            for negative in negative_matches
        )
    )
    if positive_matches or negative_matches:
        last_positive = max((m.start() for m in positive_matches), default=-1)
        last_negative = max((m.start() for m in negative_matches), default=-1)
        if last_positive > last_negative:
            return time_value, True
        if last_negative > last_positive:
            return time_value, False

    if _POSITIVE_DEPARTURE_RE.search(value):
        return time_value, True

    # Handle direct assertions such as "13:10 是出發時間" without treating a
    # mere mention of a departure as a correction rule.
    direct_departure = re.search(
        rf"{re.escape(times[0][0])}\s*:\s*{times[0][1]}.{{0,10}}(?:是|為|算作|標成|當成|說成).{{0,6}}(?:出發|起程|啟程)",
        value,
    )
    if direct_departure:
        clause_start = max(
            value.rfind(delimiter, 0, direct_departure.start())
            for delimiter in "，,；;。.!！？?"
        )
        clause = value[clause_start + 1 : direct_departure.end()]
        if not _NEGATIVE_MODAL_RE.search(clause):
            return time_value, True
    return None


def _transport_context(text: Any) -> str:
    """Keep route/date/entity context while removing correction boilerplate."""

    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    value = _TIME_RE.sub("", value)
    value = re.sub(
        r"不要再|不要|別再|別|不可|不能|不應該|不應當|不應|不該|"
        r"不需要|無需|無須|毋須|不必|不宜|避免|勿|請勿|不是|並非|非|"
        r"不准|禁止|不得|嚴禁|"
        r"必須|務必|應該|應當|應|需要|需|記住|"
        r"出發時間|出發|起程|啟程|抵達|到達|到站|"
        r"當成|說成|標成|寫成|視作|視為|算是|算作|是|為|把|"
        r"這班|該班|班次|時間",
        "",
        value,
    )
    return "".join(char for char in value if char.isalnum())


def normalize_rule(text: Any) -> str:
    """Return a conservative canonical form suitable for local comparison."""

    cleaned = _clean_rule_text(text).lower()
    if not cleaned:
        return ""

    transport = _transport_time_semantics(cleaned)
    if transport is not None:
        time_value, is_departure = transport
        context = _transport_context(cleaned)
        values, marks = _protected_value_signature(cleaned)
        return (
            f"transport-time:{time_value}:"
            f"departure={'true' if is_departure else 'false'}:context={context}:"
            f"values={'|'.join(values)}:marks={marks}"
        )

    replacements = (
        (r"啟程|起程", "出發"),
        (r"抵達|到達", "到站"),
        (r"不可以|不准|禁止|別再|別", "不要"),
        (r"不要再", "不要"),
        (r"當成|說成|標成|寫成|視作", "視為"),
    )
    for pattern, replacement in replacements:
        cleaned = re.sub(pattern, replacement, cleaned)

    # Punctuation and spacing do not carry rule identity.  Keep letters and
    # digits from every script so this remains useful outside Chinese text.
    plain = "".join(char for char in cleaned if char.isalnum())
    numbers = "\x1f".join(_protected_numbers(cleaned))
    structured = "\x1f".join(
        re.sub(r"\s+", "", token)
        for token in _protected_structured_tokens(cleaned)
    )
    protected_values, protected_marks = _protected_value_signature(cleaned)
    if numbers or structured or protected_values or protected_marks:
        return (
            f"{plain}|numbers={numbers}|structured={structured}|"
            f"values={'\x1f'.join(protected_values)}|marks={protected_marks}"
        )
    return plain


def is_ambiguous(text: Any) -> bool:
    """Whether a correction lacks enough subject/action detail to remember."""

    cleaned = _clean_rule_text(text)
    if not cleaned:
        return True
    if is_question_like(cleaned):
        return True
    if _transport_time_semantics(cleaned) is not None:
        return False

    if re.fullmatch(
        r"(?:(?:你|妳|請|記住|以後|下次)\s*)*"
        r"(?:不要|別|不可以|不能|禁止)(?:再)?(?:這樣|那樣)"
        r"(?:回答|回覆|處理|回|做|說|講|弄)?"
        r"[了啦喔哦啊呀嘛嗎]*[。！!？?]*",
        cleaned,
    ):
        return True

    compact = "".join(char for char in cleaned if char.isalnum())
    if compact in {"".join(char for char in item if char.isalnum()) for item in _GENERIC_AMBIGUOUS}:
        return True

    concrete_negative = re.fullmatch(
        r"(?:請\s*)?(?:不要|別|不能|不可|不准|禁止|勿|不應該|不應|"
        r"不該|不得|嚴禁|無需|無須|毋須|不必|不用|不宜|避免)(?:再)?(.+)",
        cleaned,
    )
    if concrete_negative:
        remainder = "".join(
            char for char in concrete_negative.group(1) if char.isalnum()
        )
        if len(remainder) >= 2:
            return False

    # Very short, digit-free reactions are normally feedback, not reusable
    # rules.  Longer statements are retained rather than guessed ambiguous.
    return len(compact) < 5 and not any(char.isdigit() for char in compact)


def _rule_text(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in _RULE_TEXT_KEYS:
            text = _clean_rule_text(value.get(key))
            if text:
                return text
        return ""
    return _clean_rule_text(value)


def _rule_id(value: Any) -> Any | None:
    if not isinstance(value, Mapping):
        return None
    for key in _RULE_ID_KEYS:
        if value.get(key) is not None:
            return value[key]
    return None


def _iter_existing(existing_rules: Any) -> Iterable[Any]:
    if existing_rules is None:
        return ()
    if isinstance(existing_rules, Mapping):
        if any(key in existing_rules for key in _RULE_TEXT_KEYS):
            return (existing_rules,)
        return tuple(
            {"rule_id": key, "rule": value} for key, value in existing_rules.items()
        )
    if isinstance(existing_rules, (str, bytes)):
        return (existing_rules,)
    try:
        return tuple(existing_rules)
    except TypeError:
        return (existing_rules,)


def _polarity(text: str) -> int:
    """Return -1 for prohibition, +1 for obligation, and 0 if unspecified."""

    if re.search(
        r"不要|別|不可|不能|不准|禁止|不是|並非|不應該|不應當|"
        r"不應|不該|不得|嚴禁|不需要|無需|無須|毋須|不必|不用|"
        r"不宜|避免|勿",
        text,
    ):
        return -1
    if re.search(r"必須|務必|應該|應當|需要|一定要", text):
        return 1
    return 0


def _polarity_base(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).lower()
    value = re.sub(
        r"不應該|不應當|不需要|不要|不應|不該|不得|嚴禁|不必|不用|"
        r"無需|無須|毋須|不宜|避免|"
        r"別|不可|不能|不准|禁止|不是|並非|勿|"
        r"必須|務必|應該|應當|需要|一定要",
        "",
        value,
    )
    return normalize_rule(value)


def _result(
    decision: str,
    score: float,
    reason: str,
    rule_id: Any | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "decision": decision,
        "score": round(max(0.0, min(1.0, score)), 4),
        "reason": reason,
    }
    if rule_id is not None:
        result["rule_id"] = rule_id
    return result


def default_adjudicator(existing_rules: Any, candidate: Any) -> dict[str, Any]:
    """Classify a candidate as distinct, equivalent, ambiguous, or conflict.

    Matching is intentionally high precision: context-bound transport-time
    rules and exact canonical equality.  Generic fuzzy similarity is refused.
    """

    candidate_text = _rule_text(candidate)
    if is_ambiguous(candidate_text):
        return _result("ambiguous", 0.0, "candidate lacks reusable subject/action detail")

    candidate_normalized = normalize_rule(candidate_text)
    candidate_transport = _transport_time_semantics(candidate_text)
    candidate_detail = _detail_coverage_semantics(candidate_text)
    candidate_polarity = _polarity(candidate_text)
    candidate_protected = _protected_value_signature(candidate_text)

    best_equivalent: tuple[float, Any | None, str] | None = None
    best_conflict: tuple[float, Any | None, str] | None = None
    equivalent_rule_ids: set[Any] = set()

    for existing in _iter_existing(existing_rules):
        existing_text = _rule_text(existing)
        if not existing_text or is_ambiguous(existing_text):
            continue
        existing_id = _rule_id(existing)
        existing_normalized = normalize_rule(existing_text)
        existing_transport = _transport_time_semantics(existing_text)
        existing_detail = _mapped_detail_semantics(existing, existing_text)
        existing_protected = _protected_value_signature(existing_text)

        # Compare protected values before every semantic shortcut, including
        # transport.  Otherwise dates/codes such as 1/2 vs 12 can collapse.
        if candidate_protected != existing_protected:
            continue

        if candidate_transport is not None and existing_transport is not None:
            candidate_time, candidate_departure = candidate_transport
            existing_time, existing_departure = existing_transport
            same_context = _transport_context(candidate_text) == _transport_context(
                existing_text
            )
            if candidate_time == existing_time and same_context:
                if candidate_departure != existing_departure:
                    match = (1.0, existing_id, "same time has opposite departure semantics")
                    if best_conflict is None or match[0] > best_conflict[0]:
                        best_conflict = match
                else:
                    match = (1.0, existing_id, "same transport-time semantics")
                    if existing_id is not None:
                        equivalent_rule_ids.add(existing_id)
                    if best_equivalent is None or match[0] > best_equivalent[0]:
                        best_equivalent = match
            # Different protected times are separate rules.  Do not let their
            # otherwise-similar wording fall through to polarity fuzzing.
            continue

        # Numeric values are protected facts/constraints, never fuzzy tokens.
        # "規則 1" and "規則 10" can otherwise exceed a high edit-similarity
        # threshold despite meaning different things.
        candidate_numbers = _protected_numbers(candidate_text)
        existing_numbers = _protected_numbers(existing_text)
        if candidate_numbers != existing_numbers:
            continue
        candidate_structured = _protected_structured_tokens(candidate_text)
        existing_structured = _protected_structured_tokens(existing_text)
        if candidate_structured != existing_structured:
            continue

        if candidate_detail is not None and existing_detail is not None:
            candidate_family, candidate_direction, candidate_include = candidate_detail
            existing_family, existing_direction, existing_include = existing_detail
            if "mixed" in {candidate_direction, existing_direction}:
                continue
            compatible_direction = (
                not candidate_direction
                or not existing_direction
                or candidate_direction == existing_direction
            )
            if candidate_family == existing_family and compatible_direction:
                if candidate_include != existing_include:
                    match = (
                        1.0,
                        existing_id,
                        "same concrete-detail topic has opposite inclusion semantics",
                    )
                    if best_conflict is None or match[0] > best_conflict[0]:
                        best_conflict = match
                else:
                    match = (
                        1.0,
                        existing_id,
                        "same concrete-detail coverage topic",
                    )
                    if existing_id is not None:
                        equivalent_rule_ids.add(existing_id)
                    if best_equivalent is None or match[0] > best_equivalent[0]:
                        best_equivalent = match
                continue
            # Same bounded family with two explicit, different directions is
            # materially distinct (e.g. anterior vs posterior tilt).
            if candidate_family == existing_family:
                continue

        if candidate_normalized == existing_normalized:
            match = (1.0, existing_id, "exact normalized match")
            if existing_id is not None:
                equivalent_rule_ids.add(existing_id)
            if best_equivalent is None or match[0] > best_equivalent[0]:
                best_equivalent = match
            continue

        existing_polarity = _polarity(existing_text)
        if candidate_polarity * existing_polarity == -1:
            candidate_base = _polarity_base(candidate_text)
            existing_base = _polarity_base(existing_text)
            if len(candidate_base) >= 4 and candidate_base == existing_base:
                match = (1.0, existing_id, "opposite modality on the same rule")
                if best_conflict is None or match[0] > best_conflict[0]:
                    best_conflict = match
            continue

        # No generic fuzzy merge: a one-token place/person/date change can be
        # semantically decisive even when edit similarity is extremely high.

    # Conflicts take precedence so inconsistent stored rules are never silently
    # reinforced by an equivalent duplicate.
    if best_conflict is not None:
        score, rule_id, reason = best_conflict
        return _result("conflict", score, reason, rule_id)
    if len(equivalent_rule_ids) > 1:
        return _result(
            "ambiguous",
            0.0,
            "multiple active canonical rules match after an explicit split",
        )
    if best_equivalent is not None:
        score, rule_id, reason = best_equivalent
        return _result("equivalent", score, reason, rule_id)
    return _result("distinct", 1.0, "no high-confidence equivalent or conflict")
