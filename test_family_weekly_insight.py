"""Andrew 2026-10-05：週報改成一週一次、標題精簡、加每人最多 2 點小建議（含妹妹）。"""

from __future__ import annotations

import inspect
import json
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

_FAKE_MAPPING = {
    "王小明": "王小明",
    "小明": "王小明",
    "陳小華": "陳小華",
    "小華": "陳小華",
    "王大明": "王大明",
    "爸爸": "王大明",
    "成員A": "成員A",
    "成員B": "成員B",
}


@pytest.fixture(autouse=True)
def _fake_family_mapping(monkeypatch):
    import family_weekly_insight

    monkeypatch.setattr(family_weekly_insight, "_family_name_mapping", lambda: dict(_FAKE_MAPPING))


# ── 標題精簡 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "title, expected",
    [
        ("〈熱門股〉環宇-KY加速200G PD出貨 周漲15.21%站回500元大關", "環宇-KY加速200G PD出貨…"),
        ("乳癌病友協會籲女性把篩檢化為行動 (圖)", "乳癌病友協會籲女性把篩檢化為行動"),
        ("觀察站／亞運檢討 中華5金較上屆少14面金牌", "亞運檢討 中華5金較上屆少14面金牌"),
        ("32歲女染髮後腹痛狂吐！醫曝1成分引發急性腎衰竭　恐有洗腎風險", "32歲女染髮後腹痛狂吐…"),
        ("美銀示警AI交易是當前美債市場「最後一道防線」！成長敘事若失靈", "美銀示警AI交易是當前美債市場…"),
        ("一二三四五六七八九十一二三四五六七八九十一二三", "一二三四五六七八九十一二三四五六七八九…"),
        ("颱風&amp;豪雨 停班停課資訊", "颱風&豪雨 停班停課資訊"),
        ("10/1起健保新制上路", "10/1起健保新制上路"),
        ("美/中貿易戰升溫", "美/中貿易戰升溫"),
        ("台積電/聯發科同步大漲", "台積電/聯發科同步大漲"),
        ("影／南投音樂節連嗨兩天", "南投音樂節連嗨兩天"),
    ],
)
def test_concise_headline_strips_tags_and_keeps_it_short(title, expected):
    import family_interest

    short = family_interest._concise_headline(title)

    assert short == expected
    assert len(short) <= family_interest.HEADLINE_MAX_CHARS


# ── 依賴的私有介面（別的 session 改名時要先在這裡失敗）──────────────────────

def test_private_interfaces_used_by_weekly_report_still_exist():
    import gemini_client
    import line_mentions
    import memory
    import output_validator

    assert hasattr(output_validator, "_HIGH_RISK_CURRENT_DATA_RE")
    assert hasattr(output_validator, "_LOW_VALUE_HELPLESS_RE")
    assert hasattr(gemini_client, "_client")
    assert callable(line_mentions.configured_family_alias_mapping)
    assert "index_for_recall" in inspect.signature(memory.log_raw_message).parameters


# ── 週報內容 ────────────────────────────────────────────────────────────────

def _render(insights, per_member, news_by_topic):
    import family_interest

    return family_interest.render_summary(
        "G1", days=7, insights=insights, per_member=per_member, news_by_topic=news_by_topic
    )


def test_render_summary_weekly_with_insights_and_no_links(monkeypatch):
    import family_interest
    import output_validator

    monkeypatch.setattr(family_interest, "_load_aliases", lambda: {"U1": "成員A", "U2": "成員B"})
    monkeypatch.setattr(
        family_interest,
        "fetch_topic_news",
        lambda *a, **kw: pytest.fail("render must not fetch when news is prefetched"),
    )

    text = _render(
        {"成員A": ["慢性病藥可請醫師開長期處方，出國前一次領足。"], "成員B": ["燉湯先汆燙去血水，湯比較清。"]},
        {"成員A": [("健康-醫療", 5)], "成員B": []},
        {"健康-醫療": [("〈健康〉流感疫苗開打 長者可先預約 (圖)", "https://news.example.test/1")]},
    )

    assert text.startswith("👨‍👩‍👧‍👦 家族熱話週報（本週）")
    assert "成員A ▸ 健康醫療(5)" in text
    assert "  📰 流感疫苗開打 長者可先預約" in text
    assert "  💡 慢性病藥可請醫師開長期處方，出國前一次領足。" in text
    assert "\n成員B\n  💡 燉湯先汆燙去血水，湯比較清。" in text
    assert "http" not in text
    assert output_validator.validate_outbound_text(text).ok


def test_render_summary_skips_headlines_the_push_validator_would_block(monkeypatch):
    import family_interest
    import output_validator

    monkeypatch.setattr(family_interest, "_load_aliases", lambda: {"U1": "成員A"})

    text = _render(
        {},
        {"成員A": [("投資-加密", 3), ("健康-醫療", 2), ("健康-飲食", 2)]},
        {
            "投資-加密": [("比特幣今年漲幅超越2021年", "u1"), ("加密貨幣交易所推新功能", "u2")],
            "健康-醫療": [("流感疫苗開打", "u3")],
            "健康-飲食": [("流感疫苗開打", "u3"), ("早餐吃蛋的好處", "u4")],
        },
    )

    assert "比特幣今年" not in text
    assert "  📰 加密貨幣交易所推新功能" in text
    assert text.count("流感疫苗開打") == 1
    assert "  📰 早餐吃蛋的好處" in text
    assert output_validator.validate_outbound_text(text).ok


def test_render_summary_returns_empty_without_any_member_content(monkeypatch):
    import family_interest

    monkeypatch.setattr(family_interest, "_load_aliases", lambda: {"U1": "成員A"})

    assert _render({}, {"成員A": []}, {}) == ""


def test_fetch_member_messages_skips_placeholders_and_masks_links(monkeypatch, tmp_path):
    import family_interest

    db = tmp_path / "raw.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE raw_messages (group_id TEXT, message_id TEXT, user_id TEXT, text TEXT, created_at INTEGER)")
    now = int(time.time())
    rows = [
        ("G1", "1", "U1", "[圖片]", now - 60),
        ("G1", "2", "U1", "[檔案: 報告.pdf]", now - 50),
        ("G1", "3", "U1", "看這個 https://example.test/a?b=1 很有趣", now - 40),
        ("G1", "4", "__bot__", "咪寶的話", now - 30),
        ("G1", "5", "U2", "很久以前的訊息", now - 9 * 86400),
        ("G1", "6", "U2", "上 gov-tw.cc 登記", now - 20),
        ("G2", "7", "U1", "別的群組", now - 10),
    ]
    conn.executemany("INSERT INTO raw_messages VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    monkeypatch.setattr(family_interest, "DB_PATH", db)
    monkeypatch.setattr(family_interest, "_load_aliases", lambda: {"U1": "成員A", "U2": "成員B"})

    result = family_interest.fetch_member_messages("G1", days=7)

    assert result == {"成員A": ["看這個 [連結] 很有趣"], "成員B": ["上 [連結] 登記"]}


# ── 家人小建議 ─────────────────────────────────────────────────────────────

def _fake_client(monkeypatch, responder):
    import gemini_client

    calls = []

    def generate_content(*, model, contents, config):
        calls.append({"model": model, "contents": contents, "config": config})
        return responder(len(calls))

    monkeypatch.setattr(
        gemini_client, "_client", SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )
    return calls


def _contents_text(contents):
    return "\n".join(part.text for content in contents for part in content.parts)


def _answer(members):
    return SimpleNamespace(text=json.dumps({"members": members}, ensure_ascii=False))


def test_insight_prompt_masks_names_numbers_and_links(monkeypatch):
    import family_weekly_insight as fwi

    calls = _fake_client(
        monkeypatch,
        lambda n: _answer([{"id": "家人1", "points": ["慢性病藥可請醫師開長期處方，出國前一次領足。"]}]),
    )

    result = fwi.generate_member_insights(
        {
            "王小明": ["跟小華說下週要出國", "爸爸叫我打 0900000000", "上 gov-tw.cc 登記"],
            "陳小華": ["好喔"],
        },
        {"王小明": [("健康-醫療", 2)]},
        timeout_s=30,
        already_said=["小明可以先備好常用藥。"],
    )

    sent = _contents_text(calls[0]["contents"])
    for leaked in ("王小明", "小明", "陳小華", "小華", "爸爸", "0900", "gov-tw"):
        assert leaked not in sent
    payload = json.loads(sent)
    assert payload["家人1"]["messages"] == ["跟家人2說下週要出國", "某家人叫我打 [號碼]", "上 [連結] 登記"]
    assert payload["already_said"] == ["家人1可以先備好常用藥。"]
    assert [content.role for content in calls[0]["contents"]] == ["user"]
    assert "咪寶" in calls[0]["config"].system_instruction
    assert calls[0]["model"] == fwi._DEFAULT_MODEL
    assert result == {"王小明": ["慢性病藥可請醫師開長期處方，出國前一次領足。"]}


def test_insight_is_skipped_when_family_names_cannot_be_loaded(monkeypatch):
    import family_weekly_insight as fwi

    monkeypatch.setattr(fwi, "_family_name_mapping", lambda: {})
    calls = _fake_client(monkeypatch, lambda n: pytest.fail("must not call the model"))

    assert fwi.generate_member_insights({"王小明": ["下週要出國"]}, {}, timeout_s=30) == {}
    assert calls == []


def test_insight_points_are_sanitized(monkeypatch):
    import family_weekly_insight as fwi

    member_text = "我這週每天都去公園快走一個小時喔"
    _fake_client(monkeypatch, lambda n: _answer([
        {
            "id": "家人1",
            "points": [
                "確實要多休息",
                "看這個 https://example.test/a",
                "你這週每天都去公園快走一個小時很棒",
                "快走前先暖身五分鐘，膝蓋比較不會痛。" * 4,
                "最新公布的數據顯示運動十分鐘就有效",
                "每天走一萬步，減重3公斤",
                "睡前吃半顆安眠藥就好",
                "胰島素打兩單位就夠",
                "降壓藥先停兩天",
                "分批買入比較穩",
                "上 gov-tw.cc 登記就能領補助",
                "加賴找客服就能退款",
                "超商買點數卡儲值即可解除分期",
                "填身分證就能領補助",
                "症狀持續就去看醫生",
                "貓咪食慾下降應諮詢獸醫",
                "吵架後先冷靜一晚再談",
                "小華也可以一起走",
                "建議諮詢專業醫師",
                "**快走後補充水分**，少量多次。",
                "鞋子選有足弓支撐的款式。",
                "第三點不該出現。",
            ],
        },
        {"id": "家人9", "points": ["不存在的家人"]},
    ]))

    result = fwi.generate_member_insights({"成員A": [member_text], "成員B": ["哈哈"]}, {}, timeout_s=30)

    assert result == {"成員A": ["快走後補充水分，少量多次。", "鞋子選有足弓支撐的款式。"]}


@pytest.mark.parametrize("point", [
    "確實冷藏可以放比較久。",
    "藥局附近停車很方便。",
    "水溫約四十度泡十五分鐘就好。",
    "和家人一起去走比較安全。",
])
def test_insight_cleaner_keeps_harmless_points(point):
    import family_weekly_insight as fwi

    assert fwi._clean_point(point, "", []) == point


def test_insight_does_not_repeat_bot_messages_or_earlier_weeks(monkeypatch):
    import family_weekly_insight as fwi

    _fake_client(monkeypatch, lambda n: _answer([
        {"id": "家人1", "points": ["睡前一小時關掉手機螢幕比較好睡。", "枕頭高度以側躺時脖子打直為準。"]},
    ]))

    result = fwi.generate_member_insights(
        {"成員A": ["最近都睡不好"]},
        {},
        timeout_s=30,
        extra_sources=["咪寶：睡前一小時關掉手機螢幕比較好睡喔"],
        already_said=["枕頭高度以側躺時脖子打直為準。"],
    )

    assert result == {}


def test_insight_total_length_keeps_first_points_for_everyone(monkeypatch):
    import family_weekly_insight as fwi

    numerals = "甲乙丙丁戊己庚辛壬癸子丑"
    members = {f"成員{n}": [f"成員{n}的訊息"] for n in numerals}
    monkeypatch.setattr(fwi, "_family_name_mapping", lambda: {name: name for name in members})
    base = "睡前改成泡溫水澡再看紙本書，讓身體慢慢放鬆比較好入睡"
    answer = [
        {"id": f"家人{i + 1}", "points": [f"{base}{n}", f"{base}{n}再補一句"]}
        for i, n in enumerate(numerals)
    ]
    _fake_client(monkeypatch, lambda n: _answer(answer))

    result = fwi.generate_member_insights(members, {}, timeout_s=30)

    assert set(result) == set(members)
    assert sum(len(p) for points in result.values() for p in points) <= fwi.MAX_TOTAL_CHARS
    assert any(len(points) == 2 for points in result.values())
    assert all(len(points) <= fwi.MAX_POINTS_PER_MEMBER for points in result.values())


def test_insight_uses_only_the_independent_model_and_fails_closed(monkeypatch):
    import family_weekly_insight as fwi

    calls = _fake_client(monkeypatch, lambda n: (_ for _ in ()).throw(RuntimeError("429")))

    assert fwi.generate_member_insights({"成員A": ["最近開始快走"]}, {}, timeout_s=30) == {}
    assert [c["model"] for c in calls] == [fwi._DEFAULT_MODEL]
    assert calls[0]["config"].http_options.timeout >= 10_000


def test_insight_returns_empty_on_garbage_and_without_material(monkeypatch):
    import family_weekly_insight as fwi

    calls = _fake_client(monkeypatch, lambda n: SimpleNamespace(text="不是 JSON"))

    assert fwi.generate_member_insights({"成員A": ["最近開始快走"]}, {}, timeout_s=30) == {}
    assert fwi.generate_member_insights({"成員A": ["  "]}, {}, timeout_s=30) == {}
    assert len(calls) == 1


# ── 週報流程 ────────────────────────────────────────────────────────────────

def _patch_weekly_summary(monkeypatch, pushed, insights_fn, bot_texts=("bot reply",), *, enabled=True):
    import family_interest
    import weekly_summary

    if enabled:
        monkeypatch.setenv("LINE_BOT_WEEKLY_INSIGHT", "1")
    else:
        monkeypatch.delenv("LINE_BOT_WEEKLY_INSIGHT", raising=False)
    monkeypatch.setattr(weekly_summary, "GROUP_ID", "G1")
    monkeypatch.setattr(weekly_summary, "line_access_token", lambda: "token")
    monkeypatch.setattr(
        weekly_summary.memory,
        "get_messages_since",
        lambda *a, **kw: [(f"m{i}", "__bot__", text, 0) for i, text in enumerate(bot_texts)],
    )
    monkeypatch.setattr(weekly_summary.gemini_client, "chat", lambda *a, **kw: "本週回顧")
    monkeypatch.setattr(weekly_summary, "_push", lambda text: pushed.append(text) or True)
    monkeypatch.setitem(sys.modules, "finance_view_validator", SimpleNamespace(run=lambda: 0))
    monkeypatch.setattr(family_interest, "_load_aliases", lambda: {"U1": "成員A"})
    monkeypatch.setattr(
        family_interest,
        "prefetch_news",
        lambda *a, **kw: (
            {"成員A": [("健康-醫療", 3)]},
            {"健康-醫療": [("流感疫苗開打", "https://news.example.test/1")]},
        ),
    )
    monkeypatch.setattr(family_interest, "fetch_member_messages", lambda *a, **kw: {"成員A": ["下週要出國"]})
    monkeypatch.setattr(family_interest, "detect_per_member_topics", lambda *a, **kw: {"成員A": [("健康-醫療", 3)]})
    monkeypatch.setattr(weekly_summary.family_weekly_insight, "generate_member_insights", insights_fn)
    return weekly_summary


def test_weekly_summary_pushes_family_report_with_insights(monkeypatch):
    pushed: list[str] = []
    seen: dict = {}

    def insights(*a, **kw):
        seen.update(kw)
        return {"成員A": ["慢性病藥可請醫師開長期處方，出國前一次領足。"]}

    weekly_summary = _patch_weekly_summary(
        monkeypatch,
        pushed,
        insights,
        bot_texts=("bot reply", "👨‍👩‍👧‍👦 家族熱話週報（本週）\n\n成員A ▸ 健康醫療(2)\n  💡 上週給過的建議。"),
    )

    assert weekly_summary.main() == 0

    assert pushed[0] == "📋 本週咪寶摘要\n\n本週回顧"
    family = pushed[1]
    assert "家族熱話週報（本週）" in family
    assert "  📰 流感疫苗開打" in family
    assert "  💡 慢性病藥可請醫師開長期處方，出國前一次領足。" in family
    assert "http" not in family
    assert seen["already_said"] == ["上週給過的建議。"]
    assert seen["extra_sources"] == ["bot reply"]


def test_weekly_summary_insight_is_off_until_enabled(monkeypatch):
    pushed: list[str] = []
    calls: list[int] = []
    weekly_summary = _patch_weekly_summary(
        monkeypatch, pushed, lambda *a, **kw: calls.append(1) or {"成員A": ["不應該出現。"]}, enabled=False
    )

    assert weekly_summary.main() == 0
    assert "insight" not in weekly_summary._workers
    assert calls == []
    assert "  📰 流感疫苗開打" in pushed[1]
    assert "💡" not in pushed[1]


def test_weekly_summary_does_not_wait_for_slow_insight_past_deadline(monkeypatch):
    pushed: list[str] = []

    def slow_insights(*a, **kw):
        time.sleep(3)
        return {"成員A": ["不應該出現。"]}

    weekly_summary = _patch_weekly_summary(monkeypatch, pushed, slow_insights)
    monkeypatch.setattr(weekly_summary, "_WORKER_DEADLINE_S", 0.5)

    began = time.monotonic()
    assert weekly_summary.main() == 0
    elapsed = time.monotonic() - began

    assert elapsed < 2.5
    assert "  📰 流感疫苗開打" in pushed[1]
    assert "💡" not in pushed[1]


def test_weekly_summary_drops_insights_when_whole_report_fails_validation(monkeypatch):
    pushed: list[str] = []
    weekly_summary = _patch_weekly_summary(monkeypatch, pushed, lambda *a, **kw: {"成員A": ["鞋子選有足弓支撐的款式。"]})
    real_validate = weekly_summary.output_validator.validate_outbound_text

    def validate(text, **kw):
        if "💡" in text:
            return SimpleNamespace(ok=False, text="", reason="test")
        return real_validate(text, **kw)

    monkeypatch.setattr(weekly_summary.output_validator, "validate_outbound_text", validate)

    assert weekly_summary.main() == 0
    assert "  📰 流感疫苗開打" in pushed[1]
    assert "💡" not in pushed[1]


def test_weekly_summary_skips_report_that_cannot_pass_validation(monkeypatch):
    pushed: list[str] = []
    weekly_summary = _patch_weekly_summary(monkeypatch, pushed, lambda *a, **kw: {})
    monkeypatch.setattr(
        weekly_summary.output_validator,
        "validate_outbound_text",
        lambda text, **kw: SimpleNamespace(ok=False, text="", reason="test"),
    )

    assert weekly_summary.main() == 1
    assert pushed == ["📋 本週咪寶摘要\n\n本週回顧"]


def test_weekly_summary_excludes_last_weeks_report_from_summary_source(monkeypatch):
    pushed: list[str] = []
    seen: dict = {}
    weekly_summary = _patch_weekly_summary(
        monkeypatch,
        pushed,
        lambda *a, **kw: {},
        bot_texts=("📋 本週咪寶摘要\n\n上週", "👨‍👩‍👧‍👦 家族熱話週報（本週）\n\n成員A", "真正的回覆"),
    )

    def fake_chat(user_input, context, facts, persona_notes):
        seen["payload"] = context[1][1]
        return "本週回顧"

    monkeypatch.setattr(weekly_summary.gemini_client, "chat", fake_chat)

    assert weekly_summary.main() == 0
    assert json.loads(seen["payload"]) == ["真正的回覆"]


def test_weekly_report_push_failure_is_not_archived(monkeypatch):
    import weekly_summary

    logged = []

    def failing_push_text(to, text, **kwargs):
        raise weekly_summary.LinePushError("LINE push HTTP 429", status_code=429)

    monkeypatch.setattr(weekly_summary, "GROUP_ID", "G1")
    monkeypatch.setattr(weekly_summary, "push_text", failing_push_text)
    monkeypatch.setattr(weekly_summary.memory, "log_raw_message", lambda *a, **kw: logged.append(a))

    assert weekly_summary._push("📋 本週咪寶摘要\n\n本週回顧") is False
    assert logged == []


def test_weekly_report_push_skips_text_the_validator_blocks(monkeypatch):
    import weekly_summary

    monkeypatch.setattr(weekly_summary, "GROUP_ID", "G1")
    monkeypatch.setattr(
        weekly_summary, "push_text", lambda *a, **kw: pytest.fail("blocked text must not be pushed")
    )
    monkeypatch.setattr(
        weekly_summary.output_validator,
        "validate_outbound_text",
        lambda text, **kw: SimpleNamespace(ok=False, text="", reason="test"),
    )

    assert weekly_summary._push("📋 本週咪寶摘要\n\n本週回顧") is False


def test_weekly_report_push_is_archived_as_bot_message(monkeypatch):
    import weekly_summary

    logged = []

    def fake_push_text(to, text, **kwargs):
        kwargs["sent_message_ids"].append("SENT1")
        return True

    monkeypatch.setattr(weekly_summary, "GROUP_ID", "G1")
    monkeypatch.setattr(weekly_summary, "_run_started", time.monotonic())
    monkeypatch.setattr(weekly_summary, "push_text", fake_push_text)
    monkeypatch.setattr(
        weekly_summary.memory, "log_raw_message", lambda *a, **kw: logged.append((a, kw))
    )

    assert weekly_summary._push("👨‍👩‍👧‍👦 家族熱話週報（本週）\n\n成員A") is True
    assert logged == [(("G1", "SENT1", "__bot__", "👨‍👩‍👧‍👦 家族熱話週報（本週）\n\n成員A"), {"index_for_recall": False})]
