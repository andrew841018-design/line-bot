"""
test_dialog_style — 對 dialog_style.py 的單元測試。

涵蓋 13 條：
1. 空輸入 → 全 0
2. 平均字數 / 詞數 算對
3. 標點頻率（。！？）算對
4. emoji 計數對
5. 提問率（？/嗎/呢）對
6. 命令式率（請/幫我）對
7. 語氣詞計數
8. top_words 有抓到
9. persona prompt — 短訊息→簡短回覆
10. persona prompt — 多提問→反問引導
11. persona prompt — 命令式→服從感
12. persona prompt — emoji→帶 emoji
13. persona prompt — 空 dict 不爆
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dialog_style  # noqa: E402
from dialog_style import analyze_user_style, generate_persona_prompt  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# analyze_user_style
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_input_returns_zero_dict():
    res = analyze_user_style([])
    assert res["msg_count"] == 0
    assert res["avg_chars"] == 0.0
    assert res["avg_tokens"] == 0.0
    assert res["top_words"] == []
    assert res["question_ratio"] == 0.0
    assert res["emoji_per_msg"] == 0.0


def test_avg_chars_and_tokens():
    msgs = ["你好嗎", "今天天氣很好"]  # 3 + 6 = 9 字 / 2 = 4.5
    res = analyze_user_style(msgs)
    assert res["msg_count"] == 2
    assert res["avg_chars"] == pytest.approx(4.5)
    # 詞數依 jieba 結果 > 0 即可
    assert res["avg_tokens"] > 0.0


def test_punct_freq_counts_full_and_half_width():
    # 半形 ! 也要算進「！」
    msgs = ["你好。今天好嗎？", "棒！cool!", "no punct"]
    res = analyze_user_style(msgs)
    pf = res["punct_freq"]
    # 「。」共 1 次 / 3 msg
    assert pf["。"] == pytest.approx(1 / 3)
    # 「！」 全形 1 + 半形 1 = 2 / 3
    assert pf["！"] == pytest.approx(2 / 3)
    # 「？」全形 1 / 3
    assert pf["？"] == pytest.approx(1 / 3)


def test_emoji_per_msg():
    msgs = ["good", "yes✅👍", "🚀🎉"]
    # 第二句 2 個（✅ + 👍 → ✅在符號區內 emoji_re 有蓋）；第三句 2 個
    res = analyze_user_style(msgs)
    # 至少抓到 emoji（不同字符組對 regex 涵蓋率不同；保底 >= 2）
    total_emoji = res["emoji_per_msg"] * res["msg_count"]
    assert total_emoji >= 2


def test_question_ratio():
    msgs = [
        "今天天氣如何？",  # 含 ？
        "你吃飯了嗎",  # 含 嗎
        "我要去散步",  # 不含
        "好棒",  # 不含
    ]
    res = analyze_user_style(msgs)
    assert res["question_ratio"] == pytest.approx(0.5)


def test_imperative_ratio():
    msgs = ["請幫我訂車票", "幫我查天氣", "我自己來", "麻煩處理一下"]
    res = analyze_user_style(msgs)
    # 「請」、「幫我」、「麻煩」命中三條
    assert res["imperative_ratio"] == pytest.approx(0.75)


def test_tone_particles():
    msgs = ["真的喔", "好啦好啦", "這樣耶", "唉真的", "沒語氣詞"]
    res = analyze_user_style(msgs)
    tone = res["tone_particles"]
    assert tone.get("喔", 0) >= 1
    assert tone.get("啦", 0) >= 2
    assert tone.get("耶", 0) >= 1
    assert tone.get("唉", 0) >= 1


def test_top_words_extracted():
    msgs = [
        "今天去吃台北的牛肉麵",
        "牛肉麵真的好吃",
        "想再去台北",
        "台北的小籠包也很棒",
    ] * 3  # 增加密度讓 TF-IDF 穩定
    res = analyze_user_style(msgs)
    assert isinstance(res["top_words"], list)
    assert len(res["top_words"]) > 0
    words = [w for w, _ in res["top_words"]]
    # 「台北」或「牛肉麵」應該出現
    assert any(w in ("台北", "牛肉麵", "好吃") for w in words)


# ─────────────────────────────────────────────────────────────────────────────
# generate_persona_prompt
# ─────────────────────────────────────────────────────────────────────────────
def test_persona_prompt_short_msg_says_simple():
    msgs = ["好", "嗯", "對", "ok"] * 10  # avg_chars < 20
    style = analyze_user_style(msgs)
    prompt = generate_persona_prompt(style)
    assert "簡短" in prompt or "短訊息" in prompt


def test_persona_prompt_high_question_ratio_reverse_ask():
    msgs = ["你會嗎？", "怎麼辦呢", "為什麼？", "可以嗎"] * 5
    style = analyze_user_style(msgs)
    prompt = generate_persona_prompt(style)
    assert "反問" in prompt


def test_persona_prompt_imperative_serves():
    msgs = ["請幫我做這件事", "幫我訂位", "請查資料", "麻煩你"] * 5
    style = analyze_user_style(msgs)
    prompt = generate_persona_prompt(style)
    assert "服從" in prompt or "命令式" in prompt


def test_persona_prompt_emoji_keeps_emoji():
    msgs = ["good🎉", "nice👍", "yes✅", "cool🚀"] * 5
    style = analyze_user_style(msgs)
    prompt = generate_persona_prompt(style)
    # 只要 prompt 文字裡提到 emoji 就算過
    assert "emoji" in prompt.lower()


def test_persona_prompt_empty_dict_returns_default_safely():
    prompt = generate_persona_prompt({"msg_count": 0})
    assert isinstance(prompt, str)
    assert len(prompt) > 0  # 不爆、有訊息


def test_module_exports():
    """規格檢查：module 有對外 export 的兩個 function。"""
    assert hasattr(dialog_style, "analyze_user_style")
    assert hasattr(dialog_style, "generate_persona_prompt")
    assert callable(dialog_style.analyze_user_style)
    assert callable(dialog_style.generate_persona_prompt)
