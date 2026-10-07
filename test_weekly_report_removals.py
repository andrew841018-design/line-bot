"""Andrew 2026-10-04 從週日週報拿掉的內容，不能再跑回來。

- 「📈 本週家族財經觀點」段落不再推播（finance_view validator 照跑）。
- 家族熱話週報不附新聞來源連結，只留標題。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace


def test_weekly_summary_does_not_push_finance_views(monkeypatch):
    import weekly_summary

    pushed: list[str] = []
    validator_calls: list[int] = []
    finance_db_calls: list[str] = []
    view = {
        "ticker": "0050",
        "direction": "bull",
        "validation_result": "hit",
        "created_at": 1_790_000_000_000,
        "display_name": "成員A",
    }

    def _record(name, value):
        def _call(*args, **kwargs):
            finance_db_calls.append(name)
            return value

        return _call

    monkeypatch.setattr(weekly_summary, "GROUP_ID", "G1")
    monkeypatch.setattr(weekly_summary, "line_access_token", lambda: "token")
    monkeypatch.setattr(
        weekly_summary.memory,
        "get_messages_since",
        lambda *a, **kw: [("m1", "__bot__", "bot reply", 0)],
    )
    monkeypatch.setattr(weekly_summary.gemini_client, "chat", lambda *a, **kw: "本週回顧")
    monkeypatch.setattr(weekly_summary, "_push", lambda text: pushed.append(text) or True)
    monkeypatch.setattr(
        weekly_summary.family_interest, "render_summary", lambda *a, **kw: ""
    )
    monkeypatch.setattr(weekly_summary, "_start_family_workers", lambda *a, **kw: None)
    monkeypatch.setitem(
        sys.modules,
        "finance_view_validator",
        SimpleNamespace(run=lambda: validator_calls.append(1) or 0),
    )
    monkeypatch.setitem(
        sys.modules,
        "finance_view_db",
        SimpleNamespace(
            list_recent=_record("list_recent", [view]),
            count_by_result=_record("count_by_result", {"hit": 1}),
        ),
    )

    assert weekly_summary.main() == 0
    assert not hasattr(weekly_summary, "_render_finance_summary")
    assert validator_calls == [1]
    assert finance_db_calls == []
    assert pushed == ["📋 本週咪寶摘要\n\n本週回顧"]


def test_family_interest_report_has_no_source_links(monkeypatch):
    import family_interest

    monkeypatch.setattr(
        family_interest,
        "detect_per_member_topics",
        lambda *a, **kw: {"成員A": [("投資-台股", 5), ("健康-飲食", 3)]},
    )
    monkeypatch.setattr(family_interest, "_load_aliases", lambda: {"U1": "成員A"})
    monkeypatch.setattr(
        family_interest,
        "fetch_topic_news",
        lambda topic, max_items=2: [
            (f"{topic.split('-')[1]}測試新聞標題", "https://news.example.test/a?id=1")
        ],
    )

    text = family_interest.render_summary("G1")

    assert "成員A ▸ 投資台股(5) 健康飲食(3)" in text
    assert "📰 台股測試新聞標題" in text
    assert "📰 飲食測試新聞標題" in text
    assert "http" not in text
    assert "news.example.test" not in text


def test_family_interest_report_without_links_still_passes_push_validation(monkeypatch):
    """沒有連結後，「最新公布／官方數據」類標題不能讓整則週報被推播驗證換成擋下訊息。"""
    import family_interest
    import output_validator

    news = {
        "投資-總經": [
            ("美國最新公布8月非農就業人數增加", "https://news.example.test/1"),
            ("聯準會官員談降息時機", "https://news.example.test/2"),
        ],
        "投資-台股": [
            ("官方數據顯示9月出口年增5%", "https://news.example.test/3"),
            ("內政部統計資料：人口連續下滑", "https://news.example.test/4"),
        ],
    }
    monkeypatch.setattr(
        family_interest,
        "detect_per_member_topics",
        lambda *a, **kw: {"成員A": [("投資-總經", 6), ("投資-台股", 4)]},
    )
    monkeypatch.setattr(family_interest, "_load_aliases", lambda: {"U1": "成員A"})
    monkeypatch.setattr(
        family_interest, "fetch_topic_news", lambda topic, max_items=2: news[topic]
    )

    text = family_interest.render_summary("G1")

    assert "📰 聯準會官員談降息時機" in text
    assert "最新公布" not in text
    assert "官方數據" not in text
    assert "統計資料" not in text
    assert "http" not in text
    assert output_validator.validate_outbound_text(text).ok
