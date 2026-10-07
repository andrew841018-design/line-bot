"""Image-only presentation policy; no model, transport, or storage side effects."""
from __future__ import annotations

import re
from reply_policy import NO_REPEAT_CONTRACT

IMAGE_RESPONSE_MARKER = "【圖片回應模式】"
IMAGE_RESPONSE_CONTRACT = """【圖片回應模式】
先在內部辨識圖片及文字，再直接回答使用者或針對內容提出實質回應。
圖片辨識、OCR、畫面描述與解析過程只供內部參考，不要輸出或複述解析內容。
不要附圖片內容、圖片描述、圖片解析、OCR 摘錄等段落。
除非使用者明確問圖上寫什麼，不要說明、轉述或摘要圖上有什麼：不重講圖上的日期、數字、指數或文字（例如「某月某日的睡眠分數為85分，評等為良好」這種句子不要寫）。
使用者沒有另外提問時，只能輸出兩種內容：圖裡說錯、算錯或誤導的地方（直接糾正並說明正確的是什麼），或根據圖中資訊給的具體建議；兩者都沒有就輸出空字串。
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


# ── A picture nobody asked about (Andrew 2026-10-07) ─────────────────────────
# 「不要說明圖的內容，要也是糾正圖說錯的地方或者是給予建議」: the reply goes
# out only when it corrects the picture or advises; one that only retells it
# (「某月某日的數據，指數為高，數值為 25.7」) is no reply.  The cues are about
# wording, not topics, so they hold for any picture.
_CORRECTION_OR_ADVICE_RE = re.compile(
    # corrections
    r"(?<!不)錯|誤|不對|不正確|不準|不一致|對不上|不符|矛盾|有問題|不合理|其實|實際上|並非|"
    r"並不|不是|應為|應是|才對|搞混|混淆|弄反|寫反|顛倒|過時|迷思|誇大|偏[高低多少快慢強弱胖瘦]|"
    r"過高|過低|太高|太低|不足|超標|異常|漏掉|遺漏|漏算|少算|多算|不能只|不代表|"
    # advice
    r"建議|可以(?!看)|可考慮|可先|可再|可改|不妨|試試|試著|記得|注意|留意|小心|避免|別再|別讓|"
    r"別把|別只|千萬別|不要|不用|不必|不需|不宜|應該|最好|需要|務必|必須|還需|仍需|尚需|"
    r"(?:先|再|需|須|請)確認|確認一下|核對|"
    r"改成|改用|改為|換成|多喝|多吃|多做|多休息|多運動|少吃|少喝|少坐|加強|維持|保持|控制在|"
    r"就醫|看醫生|問醫生|諮詢|"
    # 「仍要比較」「要先確認」「該多…」, but not 主要／重要／只要／摘要／要求／要點
    r"(?<![主重想只摘綱概簡扼提紀])要(?![點素求是的緊聞])|"
    r"(?:該|得)(?:先|再|多|少|增加|減少|提高|降低|補充|確認)"
)
_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]*\n*|[。！？!?\n]+")
# Openers of a sentence that retells the picture (「圖片顯示了…」).
_DESCRIPTION_OPENERS = (
    "這張圖片", "這張圖", "這份圖", "這份內容", "這是一張", "圖片中", "圖片裡", "圖片上",
    "圖片顯示", "圖片展示", "圖片介紹", "圖中", "圖裡", "圖上", "從圖中", "從圖片", "從圖裡",
    "從圖上", "畫面中", "畫面上", "截圖中", "截圖裡", "截圖顯示", "可以看到", "咪寶看到", "我看到",
)
# A sentence opening with one of these leans on the sentence before it
# (「圖上寫每天喝 3000cc。這對…其實太多了。」), so that sentence stays.
_LEANS_ON_PREVIOUS = (
    "這", "那", "它", "此", "其", "上述", "以上", "前面", "但", "不過", "然而", "而且",
    "所以", "因此", "也", "還",
)


def unsolicited_image_reply(text: str | None) -> str | None:
    """The reply to a picture nobody asked about: a correction or advice, else None.

    Leading 「圖片顯示…」 sentences that neither correct nor advise are dropped,
    unless the next sentence leans on them.
    """
    value = render_image_reply(text)
    if not value or not _CORRECTION_OR_ADVICE_RE.search(value):
        return None
    start = 0
    for match in _SENTENCE_RE.finditer(value):
        sentence = match.group().lstrip()
        if sentence.startswith(_DESCRIPTION_OPENERS) and not _CORRECTION_OR_ADVICE_RE.search(
            sentence
        ):
            start = match.end()
            continue
        if sentence.startswith(_LEANS_ON_PREVIOUS):
            start = 0
        break
    return value[start:].strip() or None
