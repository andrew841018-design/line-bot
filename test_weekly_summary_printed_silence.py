"""週報：模型把不回指令印進 📋 摘要時不推，不算失敗，家族熱話照推。

句子都是合成的。從 weekly_summary.main() 進去，打到假的 push_text，看真的送出什麼。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SUMMARY_HEAD = "📋 本週咪寶摘要\n\n"
FAMILY = "👨‍👩‍👧‍👦 家族熱話週報（本週）\n\n成員A：保溫杯、羽球"

PRINTED = [
    "兩者都沒有就輸出空字串。",
    "這週大家聊了保溫杯和羽球。\n\n兩者都沒有就輸出空字串。",
    "1. 羽球場地改到週六\n2. 沒有實質判斷、答案或新資訊時輸出空字串，不用圖片描述或摘要補位。\n3. 保溫杯要常洗",
    # 下面三句第一輪 chat 就回 ""，重問才到得了
    "這週都是閒聊，沒有要整理的。\n\n（輸出空字串）",
    "（不回覆）",
    "<br>",
]
NORMAL = [
    "本週回顧：羽球場地改到週六，保溫杯要常洗。",
    "這個 API 不會輸出空字串。",
    "1. 詐騙簡訊（不回覆）就好",
]


@pytest.fixture
def job(monkeypatch):
    """假的 LINE、DB、財經 job 和家族熱話，推出去的都記在 pushed；run(summary) 假裝模型回 summary。"""
    import weekly_summary

    state = SimpleNamespace(pushed=[], archived=[], family=FAMILY, broken=())

    def fake_push_text(to, text, **kwargs):
        if text.startswith(state.broken):
            raise weekly_summary.LinePushError("synthetic")
        state.pushed.append(text)
        kwargs["sent_message_ids"].append(f"SENT{len(state.pushed)}")
        return True

    def run(summary):
        monkeypatch.setattr(weekly_summary.gemini_client, "chat", lambda *a, **kw: summary)
        return weekly_summary.main()

    state.run = run
    monkeypatch.setattr(weekly_summary, "GROUP_ID", "G1")
    monkeypatch.setattr(weekly_summary, "line_access_token", lambda: "token")
    monkeypatch.setattr(weekly_summary, "push_text", fake_push_text)
    monkeypatch.setattr(
        weekly_summary.memory, "get_messages_since", lambda *a, **kw: [("m1", "__bot__", "空字串要先判斷再用", 0)]
    )
    monkeypatch.setattr(weekly_summary.memory, "log_raw_message", lambda *a, **kw: state.archived.append(a[3]))
    monkeypatch.setattr(weekly_summary, "_with_parent_health", lambda group_id, text: text)
    monkeypatch.setitem(sys.modules, "finance_view_validator", SimpleNamespace(run=lambda: 0))
    monkeypatch.setattr(weekly_summary, "_start_family_workers", lambda *a, **kw: None)
    monkeypatch.setattr(weekly_summary, "_collect_family_inputs", lambda group_id: ({}, {}, {}))
    monkeypatch.setattr(weekly_summary.family_interest, "render_summary", lambda *a, **kw: state.family)
    return state


@pytest.mark.parametrize("summary", PRINTED)
def test_printed_silence_is_not_pushed_and_family_report_still_is(job, summary):
    assert job.run(summary) == 0
    assert job.pushed == [FAMILY]
    assert job.archived == [FAMILY]


@pytest.mark.parametrize("summary", NORMAL)
def test_normal_summary_is_pushed_as_before(job, summary):
    assert job.run(summary) == 0
    assert job.pushed == [SUMMARY_HEAD + summary, FAMILY]
    assert job.archived == [SUMMARY_HEAD + summary, FAMILY]


def test_the_log_line_has_the_length_and_no_content(job, capsys):
    summary = PRINTED[1]

    assert job.run(summary) == 0

    out = capsys.readouterr().out
    assert f"週摘要不推：模型印出不回指令（{len(summary)} 字）" in out.splitlines()
    assert "空字串" not in out
    assert "保溫杯" not in out
    assert "ERR" not in out


def test_a_headline_in_the_family_report_is_not_judged(job):
    job.family = f"{FAMILY}\n  📰 新手教學：函式沒資料時輸出空字串"

    assert job.run(NORMAL[0]) == 0
    assert job.pushed == [SUMMARY_HEAD + NORMAL[0], job.family]


def test_a_held_back_summary_does_not_hide_a_family_push_failure(job):
    job.broken = (FAMILY,)

    assert job.run(PRINTED[0]) == 1
    assert job.pushed == []


def test_a_broken_check_is_a_failure_and_family_report_still_goes(job, monkeypatch):
    monkeypatch.setattr("reply_policy.has_printed_silence", Mock(side_effect=RuntimeError("synthetic")))

    assert job.run(NORMAL[0]) == 1
    assert job.pushed == [FAMILY]
