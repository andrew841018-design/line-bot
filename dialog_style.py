"""
dialog_style — 純本機「對話風格」分析。

從 user 歷史訊息抽取 7 大類風格指標 + 高頻詞 top-20，
再轉成「咪寶該怎麼答」的 persona prompt 補充字串。

100% 本機（jieba TF-IDF + regex），不打雲端 API。

公開 API:
    analyze_user_style(user_msgs)        -> dict
    generate_persona_prompt(style_dict)  -> str
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

# 重用既有模組的 jieba 設定（自訂詞、停用詞），避免在這支再裝載一份
try:
    from chinese_nlp import extract_keywords, tokenize  # type: ignore[import-not-found]
except Exception:  # pragma: no cover — chinese_nlp 沒裝載時 fallback
    import jieba  # type: ignore[import-not-found]
    import jieba.analyse  # type: ignore[import-not-found]

    def tokenize(text: str) -> list[str]:
        if not text or not text.strip():
            return []
        return [t.strip() for t in jieba.lcut(text) if t.strip()]

    def extract_keywords(text: str, top_k: int = 5) -> list[tuple[str, float]]:
        if not text or not text.strip():
            return []
        return [(w, float(s)) for w, s in jieba.analyse.extract_tags(text, topK=top_k, withWeight=True)]


# ── regex / 字典 ──────────────────────────────────────────────────────────────
_PUNCT_END = re.compile(r"[。！？!?]")
_QUESTION_MARK = re.compile(r"[？?]")
# 常見繁中疑問語助詞
_QUESTION_PARTICLES = ("嗎", "呢", "吧", "？", "?")
# 命令式 / 請求語
_IMPERATIVE_HINTS = ("請", "幫我", "麻煩", "給我", "幫忙", "可以", "能不能")
# 語氣詞
_TONE_PARTICLES = {
    "喔": "ou",
    "哦": "ou",
    "啦": "la",
    "耶": "ye",
    "唉": "ai",
    "啊": "a",
    "嗯": "en",
    "欸": "ei",
    "齁": "ho",
}

# 寬鬆 emoji 範圍：BMP + supplementary（含貼圖符號、表情、旗幟、補充象形）
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001fa6f"
    "\U0001fa70-\U0001faff"
    "\U00002600-\U000026ff"
    "\U00002700-\U000027bf"
    "]",
    flags=re.UNICODE,
)


# ── 主分析 ────────────────────────────────────────────────────────────────────
def analyze_user_style(user_msgs: list[str]) -> dict[str, Any]:
    """
    對 user 歷史訊息做 7 大指標 + 高頻詞分析。

    Args:
        user_msgs: user 講過的句子清單（已去 bot / 去 system）。

    Returns:
        {
          "msg_count": int,
          "avg_chars": float,           # 平均字數
          "avg_tokens": float,          # 平均 jieba 詞數
          "punct_freq": {"。": float, "！": float, "？": float},  # per-msg 平均次數
          "emoji_per_msg": float,
          "question_ratio": float,      # 0~1，含「？/嗎/呢」的訊息比例
          "imperative_ratio": float,    # 0~1，含「請/幫我」的訊息比例
          "tone_particles": {"喔": int, "啦": int, ...},
          "top_words": [(word, score), ...],   # TF-IDF top-20
        }
    """
    msgs = [m for m in (user_msgs or []) if isinstance(m, str) and m.strip()]
    n = len(msgs)
    if n == 0:
        return {
            "msg_count": 0,
            "avg_chars": 0.0,
            "avg_tokens": 0.0,
            "punct_freq": {"。": 0.0, "！": 0.0, "？": 0.0},
            "emoji_per_msg": 0.0,
            "question_ratio": 0.0,
            "imperative_ratio": 0.0,
            "tone_particles": {},
            "top_words": [],
        }

    # 1. 字 / 詞 平均長度
    total_chars = sum(len(m) for m in msgs)
    token_counts = [len(tokenize(m)) for m in msgs]
    total_tokens = sum(token_counts)

    # 2. 標點頻率（per-msg 平均，不是 per-char）
    punct_counts = {"。": 0, "！": 0, "？": 0}
    for m in msgs:
        punct_counts["。"] += m.count("。")
        # 「！」全形 + 「!」半形都算
        punct_counts["！"] += m.count("！") + m.count("!")
        punct_counts["？"] += m.count("？") + m.count("?")

    punct_freq = {k: v / n for k, v in punct_counts.items()}

    # 3. emoji 用量
    emoji_total = sum(len(_EMOJI_RE.findall(m)) for m in msgs)
    emoji_per_msg = emoji_total / n

    # 4. 提問頻率（任一條件命中即算「提問訊息」）
    q_msgs = 0
    for m in msgs:
        if any(p in m for p in _QUESTION_PARTICLES):
            q_msgs += 1
    question_ratio = q_msgs / n

    # 5. 命令式
    imp_msgs = 0
    for m in msgs:
        if any(h in m for h in _IMPERATIVE_HINTS):
            imp_msgs += 1
    imperative_ratio = imp_msgs / n

    # 6. 語氣詞
    tone_counter: Counter[str] = Counter()
    joined = "\n".join(msgs)
    for particle in _TONE_PARTICLES:
        c = joined.count(particle)
        if c > 0:
            tone_counter[particle] = c

    # 7. 高頻詞 top-20（用整段語料抽 TF-IDF；單字會被 chinese_nlp 過掉）
    top_words = extract_keywords(joined, top_k=20)

    return {
        "msg_count": n,
        "avg_chars": total_chars / n,
        "avg_tokens": total_tokens / n,
        "punct_freq": punct_freq,
        "emoji_per_msg": emoji_per_msg,
        "question_ratio": question_ratio,
        "imperative_ratio": imperative_ratio,
        "tone_particles": dict(tone_counter),
        "top_words": top_words,
    }


# ── persona prompt 生成 ───────────────────────────────────────────────────────
def generate_persona_prompt(style_dict: dict[str, Any]) -> str:
    """
    把 style_dict 轉成「咪寶該怎麼答」的 prompt 補充。

    啟發式規則（可疊加，文字之間用句點分隔）：
      - 平均訊息字數 < 20 → 簡短、不要長篇
      - 平均訊息字數 > 60 → 對方接受長段落，可給完整脈絡
      - question_ratio > 0.3 → 多反問引導
      - imperative_ratio > 0.3 → 服從感、不挑戰前提
      - emoji_per_msg > 0.5 → 對方愛 emoji，回覆可帶 1 顆
      - 多用「。」少用「！」 → 平靜冷靜口吻
      - 多用「！」 → 帶情緒，可放點熱度
      - 語氣詞集中於「啦/喔/耶」 → 台灣口語化
      - top_words 帶領域字（>= 3 個） → 放進 prompt 讓咪寶接得上 context
    """
    if not style_dict or style_dict.get("msg_count", 0) == 0:
        return "（沒有足夠的對話樣本可分析，使用預設口吻。）"

    lines: list[str] = []

    avg_chars = float(style_dict.get("avg_chars", 0))
    if avg_chars > 0:
        if avg_chars < 20:
            lines.append("使用者習慣短訊息（平均不到 20 字），咪寶要簡短回覆，避免長篇大論。")
        elif avg_chars > 60:
            lines.append("使用者習慣長訊息（平均超過 60 字），咪寶可以給完整脈絡與多層分析。")
        else:
            lines.append(f"使用者平均訊息長度約 {avg_chars:.0f} 字，咪寶以中等篇幅回覆即可。")

    q_ratio = float(style_dict.get("question_ratio", 0))
    if q_ratio > 0.3:
        lines.append("使用者常提問（提問率 {:.0%}），咪寶要在回覆末尾加一個反問引導對話。".format(q_ratio))

    imp_ratio = float(style_dict.get("imperative_ratio", 0))
    if imp_ratio > 0.3:
        lines.append(
            "使用者常用命令式（請 / 幫我，比例 {:.0%}），咪寶要展現服從感，先回應請求再補充，不挑戰前提。".format(imp_ratio)
        )

    emoji_per = float(style_dict.get("emoji_per_msg", 0))
    if emoji_per > 0.5:
        lines.append("使用者愛用 emoji（每則平均 {:.1f} 個），咪寶可在回覆首尾加 1 個合適 emoji。".format(emoji_per))
    elif emoji_per < 0.05:
        lines.append("使用者極少用 emoji，咪寶回覆保持純文字。")

    punct = style_dict.get("punct_freq", {}) or {}
    period = float(punct.get("。", 0))
    excl = float(punct.get("！", 0))
    if period > 0 or excl > 0:
        if period > excl * 2:
            lines.append("使用者語氣偏冷靜（句號顯著多於驚嘆號），咪寶口吻保持平穩。")
        elif excl > period * 2:
            lines.append("使用者語氣偏熱情（驚嘆號多），咪寶可以帶點情緒、節奏快一點。")

    tone = style_dict.get("tone_particles", {}) or {}
    tw_locals = sum(tone.get(k, 0) for k in ("啦", "喔", "耶", "齁", "欸"))
    if tw_locals >= 3:
        common = sorted(tone.items(), key=lambda x: -x[1])[:3]
        common_str = "/".join(k for k, _ in common)
        lines.append(f"使用者偏台灣口語（語氣詞集中於 {common_str}），咪寶可適度模仿。")

    top_words = style_dict.get("top_words", []) or []
    domain_words = [w for w, _ in top_words[:10] if len(w) >= 2]
    if len(domain_words) >= 3:
        sample = "、".join(domain_words[:6])
        lines.append(f"使用者常聊主題詞包含：{sample}。咪寶要熟悉這些 context，回覆時自然帶到。")

    if not lines:
        return "（風格指標都在中位數，使用預設咪寶口吻即可。）"

    header = "── 使用者風格摘要（自動學習）──"
    return header + "\n" + "\n".join(f"- {line}" for line in lines)


__all__ = ["analyze_user_style", "generate_persona_prompt"]
