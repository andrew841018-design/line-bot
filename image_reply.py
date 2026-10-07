"""Image-only presentation policy; no model, transport, or storage side effects."""
from __future__ import annotations

import re
from reply_policy import NO_REPEAT_CONTRACT

IMAGE_RESPONSE_MARKER = "【圖片回應模式】"
IMAGE_RESPONSE_CONTRACT = """【圖片回應模式】
先在內部辨識圖片及文字，再直接回答使用者或針對內容提出實質回應。
圖片辨識、OCR、畫面描述與解析過程只供內部參考，不要輸出或複述解析內容。
不要附圖片內容、圖片描述、圖片解析、OCR 摘錄等段落。
第一句直接給答案或具體判斷，必要時用1–4點說明理由、限制或建議。
有爭議時衡量支持與反對證據，但不強制四段格式、不硬湊論點或罐頭。
只有使用者明確要求才列正反方或來源清單；查證依據仍供內部判斷。
不設最低字數，保留原意與必要條件，刪除重複背景。
只輸出實質答案，禁止思緒、推理草稿、規則檢查與靜默或不回覆原因；無實質內容就輸出空字串。
使用繁體中文短句；不要編造來源、網址、看不到的數字或圖片外的事實。
資訊不足就只說影響判斷的具體缺口，不把辨識文字當成回答。""" + "\n" + NO_REPEAT_CONTRACT

_ANALYSIS_LABELS = (
    "圖片內容", "圖片描述", "圖片解析", "圖片分析", "圖片摘要", "畫面描述",
    "圖中文字", "OCR 文字摘錄", "OCR文字摘錄", "OCR 文字", "OCR文字", "OCR 摘錄",
    "OCR摘錄", "辨識結果", "解析內容",
)
_RESPONSE_LABELS = (
    "正方", "反方", "統一論點", "整合論點", "整合", "綜合判斷", "回應",
    "回答", "答案", "建議", "結論", "來源", "最終回應", "最終回答",
    "回覆", "最終回覆", "我的回應", "我的回覆",
)
_LABELS = "|".join(re.escape(label) for label in (*_ANALYSIS_LABELS, *_RESPONSE_LABELS))
_HEADER = re.compile(
    rf"(?m)^[ \t]*(?:\#{{1,6}}\s*|[-*•▌]\s+|\d+[.)、]\s*)?"
    rf"(?:📷[ \t]*)?"
    rf"(?:\*\*|__|【|\[)?(?P<label>{_LABELS})"
    rf"(?:[ \t]*[（(][^\n（）()]{{1,40}}[）)]|如下)?"
    rf"(?:\*\*|__|】|\])?[ \t]*(?:[：:](?:\*\*|__)?[ \t]*|(?=\n|$))"
)
_OCR_PREFIX = re.compile(r"^(?:⚠[\ufe0f]?\s*)?文字辨識降級結果|^📷\s*OCR")
_LEGACY_PREFIX = re.compile(r"^📷 補回之前漏掉的圖片\s*")


def has_image_analysis_envelope(text: str | None) -> bool:
    """Recognize explicit legacy image wrappers, not words in normal sentences."""
    value = _LEGACY_PREFIX.sub("", (text or "").strip())
    return bool(_OCR_PREFIX.match(value)) or any(
        match.group("label").startswith(("圖片", "圖中文字", "OCR"))
        for match in _HEADER.finditer(value)
    )


def render_image_reply(text: str | None) -> str | None:
    """Keep the actual answer and discard explicit image/OCR analysis sections."""
    value = _LEGACY_PREFIX.sub("", (text or "").strip())
    if not value or _OCR_PREFIX.match(value):
        return None
    # The former deterministic wrapper contains no answer, only generic filler.
    if (
        "如果圖片中的主張或呈現方式成立" in value
        and "先把這張圖當成線索" in value
    ):
        return None
    matches = list(_HEADER.finditer(value))
    if not any(m.group("label") in _ANALYSIS_LABELS for m in matches):
        return value
    preamble = value[:matches[0].start()].strip()
    if re.fullmatch(r"(?:以下是|這是)(?:我的|本次)?(?:圖片)?(?:分析|解析|辨識結果)[：:]?", preamble):
        preamble = ""
    parts = [preamble]
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        body = value[match.end():end].strip()
        if match.group("label") not in _ANALYSIS_LABELS and body:
            parts.append(f"{match.group('label')}：{body}")
    return "\n\n".join(part for part in parts if part).strip() or None


def is_image_context_echo(reply: str | None, *contexts: str) -> bool:
    """Reject plain or labelled verbatim context, including whitespace changes."""
    rendered = render_image_reply(reply) or ""
    answer = "".join(_HEADER.sub("", rendered).split())
    return bool(answer) and any(answer == "".join(context.split()) for context in contexts)
