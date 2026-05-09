"""
tests/test_chinese_nlp.py — 純本機 NLP 模組測試

涵蓋：
  - tokenize 基本斷詞 / 停用詞過濾
  - extract_keywords TF-IDF 前 K 名 + 過濾單字
  - classify_intent 9 大 intent + general fallback + 純股票代號 fallback
  - detect_sentiment positive/negative/neutral + 否定詞反轉
  - extract_entities 股票 / 日期 / 時間 / 金額 / 人名 / 地名

15+ 測試樣本，全本機，不依賴雲端。
"""

from __future__ import annotations

import pytest

from chinese_nlp import (
    classify_intent,
    detect_sentiment,
    extract_entities,
    extract_keywords,
    tokenize,
)


# ─────────────────────────────────────────────────────────────────────────────
# tokenize
# ─────────────────────────────────────────────────────────────────────────────
class TestTokenize:
    def test_basic_chinese(self):
        toks = tokenize("我想查台積電的股價")
        assert "台積電" in toks
        assert "股價" in toks
        # 停用詞應被過濾
        assert "我" not in toks
        assert "的" not in toks

    def test_empty_input_returns_empty_list(self):
        assert tokenize("") == []
        assert tokenize("   ") == []

    def test_punctuation_stripped(self):
        toks = tokenize("你好，世界！今天天氣真好。")
        assert "，" not in toks
        assert "！" not in toks
        assert "天氣" in toks


# ─────────────────────────────────────────────────────────────────────────────
# extract_keywords
# ─────────────────────────────────────────────────────────────────────────────
class TestExtractKeywords:
    def test_returns_top_k(self):
        result = extract_keywords(
            "颱風來襲台北可能停班停課明天上班族要注意通勤",
            top_k=3,
        )
        assert len(result) <= 3
        assert all(isinstance(w, str) and isinstance(s, float) for w, s in result)

    def test_filters_single_chars(self):
        result = extract_keywords("台積電業績亮眼大漲外資買超", top_k=5)
        # 結果不能含長度 < 2 的單字
        assert all(len(w) >= 2 for w, _ in result)

    def test_empty_text(self):
        assert extract_keywords("", top_k=5) == []


# ─────────────────────────────────────────────────────────────────────────────
# classify_intent
# ─────────────────────────────────────────────────────────────────────────────
class TestClassifyIntent:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("台積電2330今天漲多少", "stock"),
            ("明天台北天氣怎樣", "weather"),
            ("捷運從台北到桃園要多久", "transport"),
            ("幫我畫一張貓咪的圖", "image_gen"),
            ("把這句翻譯成英文", "translation"),
            ("現在幾點", "datetime"),
            ("為什麼天空是藍色的", "reasoning"),
            ("今天心情好難過", "emotion"),
        ],
    )
    def test_intent_hits(self, text, expected):
        result = classify_intent(text)
        assert result["intent"] == expected
        assert result["confidence"] > 0
        assert isinstance(result["matched_keywords"], list)
        assert len(result["matched_keywords"]) > 0

    def test_general_fallback(self):
        result = classify_intent("嗯嗯好喔")
        assert result["intent"] == "general"
        assert result["confidence"] == 0.0
        assert result["matched_keywords"] == []

    def test_pure_stock_code_fallback(self):
        # 沒打 "股票" 但給代號 → 仍歸 stock
        result = classify_intent("2330 跟 2317 哪個好")
        assert result["intent"] == "stock"
        assert "2330" in result["matched_keywords"]

    def test_year_not_treated_as_stock(self):
        # 純年份應被排除
        result = classify_intent("2024 是好的一年")
        assert result["intent"] != "stock"

    def test_empty_text(self):
        result = classify_intent("")
        assert result["intent"] == "general"
        assert result["confidence"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# detect_sentiment
# ─────────────────────────────────────────────────────────────────────────────
class TestSentiment:
    def test_positive(self):
        assert detect_sentiment("今天好開心，很喜歡這個禮物") == "positive"

    def test_negative(self):
        assert detect_sentiment("我超難過，整個崩潰") == "negative"

    def test_neutral(self):
        assert detect_sentiment("這是一張桌子") == "neutral"

    def test_negator_flips_polarity(self):
        # 「不開心」應該被反轉成 negative
        assert detect_sentiment("我今天不開心") == "negative"

    def test_empty_text(self):
        assert detect_sentiment("") == "neutral"


# ─────────────────────────────────────────────────────────────────────────────
# extract_entities
# ─────────────────────────────────────────────────────────────────────────────
class TestEntities:
    def test_stock_code(self):
        ent = extract_entities("2330 漲到 600 元了")
        assert "2330" in ent["stock"]

    def test_year_excluded_from_stock(self):
        ent = extract_entities("2024 年的展望")
        assert "2024" not in ent["stock"]

    def test_date_numeric_and_relative(self):
        ent = extract_entities("我們約 2026/05/10，後天先確認")
        assert "2026/05/10" in ent["date"]
        assert "後天" in ent["date"]

    def test_time(self):
        ent = extract_entities("下午三點開會 14:30 結束")
        # 中文時間
        assert any("下午" in t and "三" in t for t in ent["time"])
        # 數字時間
        assert "14:30" in ent["time"]

    def test_amount(self):
        ent = extract_entities("這個包要兩萬五千元")
        # 至少有命中其中一種金額
        assert len(ent["amount"]) > 0

    def test_location_extracted(self):
        ent = extract_entities("我下週要去台北和高雄")
        # jieba ns 至少能抓到一個
        assert any(loc in ("台北", "高雄") for loc in ent["location"])

    def test_empty_returns_all_empty_lists(self):
        ent = extract_entities("")
        for key in ("stock", "date", "time", "amount", "person", "location"):
            assert ent[key] == []
