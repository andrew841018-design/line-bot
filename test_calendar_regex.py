"""calendar_regex — pure-regex family event extractor tests.

14:51 / 14:49 verbatim 是 non-negotiable（advisor 要求），其餘 cover edge cases。
"""

from __future__ import annotations

from datetime import date

import calendar_regex


_TODAY = date(2026, 5, 21)  # 14:51 事件當天


# ── 14:51 verbatim：原始失敗 case ──────────────────────────────────────────────
def test_1451_verbatim_iso_format():
    """家人在 14:51 手打的字串 — quota 爆 Gemini 抽不到，regex 必須能抽。"""
    text = "2026-05-22 14:00 拿喜來登贈送的生日蛋糕"
    out = calendar_regex.extract_regex_only(text, _TODAY)
    assert out["has_event"] is True
    assert out["date"] == "2026-05-22"
    assert out["time"] == "14:00"
    assert "蛋糕" in out["title"] or "喜來登" in out["title"]


def test_1451_within_sentence():
    """同樣的字串包在句子裡也要能抽。"""
    text = "提醒一下 2026-05-22 14:00 拿喜來登贈送的生日蛋糕 喔"
    out = calendar_regex.extract_regex_only(text, _TODAY)
    assert out["has_event"] is True
    assert out["date"] == "2026-05-22"
    assert out["time"] == "14:00"


# ── Family keyword filter（GP1 + codex 反饋）──────────────────────────────────
def test_work_event_rejected():
    """「明天 14:00 開週會」不是 family event，必須拒絕。"""
    out = calendar_regex.extract_regex_only("明天 14:00 開週會", _TODAY)
    assert out["has_event"] is False


def test_stock_news_with_date_not_event():
    """含日期/「大漲」的股市訊息不能被誤判成提醒。"""
    out = calendar_regex.extract_many_regex_only(
        "美國國慶7月4日、美伊簽和約、川普生日放利多等等造成美國四大股市現在大漲，明天台股可能比較樂觀。",
        _TODAY,
    )
    assert out == []


def test_multiple_slash_dates_share_tail_title():
    """「7/5 1600和7/12 1800 全家打羽球」兩個日期共用尾端事件標題。"""
    out = calendar_regex.extract_many_regex_only(
        "7/5 1600和7/12 1800 全家打羽球",
        date(2026, 6, 27),
    )
    assert [
        (item["date"], item["time"], item["title"]) for item in out
    ] == [
        ("2026-07-05", "16:00", "全家打羽球"),
        ("2026-07-12", "18:00", "全家打羽球"),
    ]


def test_work_iso_format_rejected():
    """ISO 日期格式的工作事件也要拒絕。"""
    out = calendar_regex.extract_regex_only(
        "2026-05-22 14:00 部門會議討論 Q3 目標", _TODAY
    )
    assert out["has_event"] is False


def test_take_alone_rejected():
    """「拿」單字（如「拿到票了」「拿筆」）不該觸發。"""
    out = calendar_regex.extract_regex_only("2026-05-22 14:00 拿筆寫字", _TODAY)
    assert out["has_event"] is False


def test_take_cake_accepted():
    """「拿蛋糕」明確 family event。"""
    out = calendar_regex.extract_regex_only("2026-05-22 14:00 拿蛋糕", _TODAY)
    assert out["has_event"] is True


def test_classify_medical_with_doctor_name_between_look_and_dentist():
    assert calendar_regex._classify_type("看台大陳敏惠牙醫師") == "medical"


# ── 中文日期格式 ────────────────────────────────────────────────────────────
def test_chinese_date_same_year():
    """5月21日（today）後的同年日期。"""
    out = calendar_regex.extract_regex_only("12月25日 18:00 全家聚餐", _TODAY)
    assert out["has_event"] is True
    assert out["date"] == "2026-12-25"
    assert out["time"] == "18:00"


def test_chinese_date_year_rollover():
    """跨年：12 月時講「1月3日」應該對應明年。"""
    dec_today = date(2026, 12, 20)
    out = calendar_regex.extract_regex_only("1月3日 18:00 全家聚餐", dec_today)
    assert out["has_event"] is True
    assert out["date"] == "2027-01-03"


def test_chinese_date_today_same_day():
    """同一天的日期應該保留（不會誤判推到明年）。"""
    out = calendar_regex.extract_regex_only("5月21日 18:00 全家聚餐", _TODAY)
    assert out["has_event"] is True
    assert out["date"] == "2026-05-21"


# ── 相對日期關鍵字 ─────────────────────────────────────────────────────────
def test_relative_tomorrow():
    out = calendar_regex.extract_regex_only("明天 14:00 拿蛋糕", _TODAY)
    assert out["has_event"] is True
    assert out["date"] == "2026-05-22"


def test_relative_day_after_tomorrow():
    out = calendar_regex.extract_regex_only("後天 18:00 全家聚餐", _TODAY)
    assert out["has_event"] is True
    assert out["date"] == "2026-05-23"


def test_relative_today():
    out = calendar_regex.extract_regex_only("今天 19:00 回家吃晚餐", _TODAY)
    assert out["has_event"] is True
    assert out["date"] == "2026-05-21"


def test_weekday_chinese_time_medical():
    """Gemini/quota down 時，也要能抽「星期四早上十點半看牙醫」。"""
    out = calendar_regex.extract_regex_only(
        "星期四早上十點半看台大陳敏惠牙醫師",
        date(2026, 6, 2),
    )
    assert out["has_event"] is True
    assert out["date"] == "2026-06-04"
    assert out["time"] == "10:30"
    assert out["event_type"] == "medical"
    assert "牙醫" in out["title"]


def test_weekday_chinese_time_work_event_rejected():
    """新增中文星期/時間 parser 仍不能放寬到工作會議。"""
    out = calendar_regex.extract_regex_only(
        "星期四早上十點半開週會",
        date(2026, 6, 2),
    )
    assert out["has_event"] is False


def test_weekday_rolls_forward_to_next_week():
    out = calendar_regex.extract_regex_only(
        "星期一早上十點看牙醫",
        date(2026, 6, 2),
    )
    assert out["has_event"] is True
    assert out["date"] == "2026-06-08"
    assert out["time"] == "10:00"


def test_weekday_alias_and_pm_chinese_time():
    out = calendar_regex.extract_regex_only(
        "禮拜四下午三點半看牙醫",
        date(2026, 6, 2),
    )
    assert out["has_event"] is True
    assert out["date"] == "2026-06-04"
    assert out["time"] == "15:30"


# ── HH:MM 驗證（GP1 反饋）─────────────────────────────────────────────────
def test_invalid_hour_25_rejected():
    """25:99 應該不能 match。"""
    out = calendar_regex.extract_regex_only("明天 25:99 拿蛋糕", _TODAY)
    assert out["has_event"] is False


def test_invalid_minute_61_rejected():
    """xx:61 應該不能 match。"""
    out = calendar_regex.extract_regex_only("明天 14:61 拿蛋糕", _TODAY)
    assert out["has_event"] is False


def test_valid_edge_time_2359():
    """23:59 是合法時間。"""
    out = calendar_regex.extract_regex_only("明天 23:59 拿蛋糕", _TODAY)
    assert out["has_event"] is True


# ── Title 邊界 ─────────────────────────────────────────────────────────────
def test_title_no_newline():
    """title 不該吃 newline。"""
    text = "2026-05-22 14:00 拿蛋糕\n下一行不該被吃"
    out = calendar_regex.extract_regex_only(text, _TODAY)
    assert out["has_event"] is True
    assert "\n" not in out["title"]
    assert "下一行" not in out["title"]


def test_title_strips_control_chars():
    """title 不該保留控制字元。"""
    text = "2026-05-22 14:00 拿蛋糕\x07\x1f"
    out = calendar_regex.extract_regex_only(text, _TODAY)
    if out["has_event"]:
        assert "\x07" not in out["title"]
        assert "\x1f" not in out["title"]


# ── 邊界：空字串 / 無 family keyword ─────────────────────────────────────
def test_empty_string():
    out = calendar_regex.extract_regex_only("", _TODAY)
    assert out["has_event"] is False


def test_whitespace_only():
    out = calendar_regex.extract_regex_only("   \n\t  ", _TODAY)
    assert out["has_event"] is False


def test_invalid_date_feb_30():
    """2 月 30 日不存在。"""
    out = calendar_regex.extract_regex_only("2026-02-30 14:00 拿蛋糕", _TODAY)
    assert out["has_event"] is False


# ── 整合：calendar_extractor 串接 ────────────────────────────────────────
def test_extractor_falls_back_to_regex(monkeypatch):
    """Gemini 兩 model 都 fail → 自動走 regex fallback。"""
    import calendar_extractor

    def boom(*args, **kwargs):
        raise RuntimeError("simulated 429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(
        calendar_extractor.gemini_client._client.models,
        "generate_content",
        boom,
    )
    out = calendar_extractor.extract("2026-05-22 14:00 拿喜來登贈送的生日蛋糕")
    # regex fallback 必須命中
    assert out["has_event"] is True
    assert out["date"] == "2026-05-22"
    assert out["time"] == "14:00"
