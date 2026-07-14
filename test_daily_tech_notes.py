import json
from datetime import datetime

import daily_briefing_discord as dbd


def test_daily_tech_note_topics_are_mini_project_or_jd_scoped():
    banned_fragments = ("LINE", "line_bot", "Discord", "Gemini", "launchd", "n8n", "P" + "TT")

    assert dbd._TECH_NOTE_TOPICS
    for topic in dbd._TECH_NOTE_TOPICS:
        assert topic.get("scope") in {"mini_project", "jd_fit"}
        text = " ".join(
            [
                topic["id"],
                topic["title"],
                topic["why"],
                *topic["points"],
            ]
        )
        assert topic.get("scope") in {"mini_project", "jd_fit"}
        assert all(fragment not in text for fragment in banned_fragments)


def test_daily_tech_note_initializes_first_topic(tmp_path):
    state_path = tmp_path / "tech_note_state.json"

    msg = dbd.daily_tech_note(
        now=datetime(2026, 5, 31, 10, 0),
        state_path=state_path,
    )

    assert "🧠 **每日技術筆記**" in msg
    assert "先不看筆記" in msg
    assert "無提示初答" in msg
    assert "D+1/D+3/D+7/D+14" in msg
    assert "1/" in msg
    assert dbd._TECH_NOTE_TOPICS[0]["title"] in msg
    assert "等你先無提示作答" in msg
    assert state_path.exists()


def test_daily_tech_note_unapproved_topic_does_not_advance_next_day(tmp_path):
    state_path = tmp_path / "tech_note_state.json"

    first = dbd.daily_tech_note(
        now=datetime(2026, 5, 31, 10, 0),
        state_path=state_path,
    )
    second = dbd.daily_tech_note(
        now=datetime(2026, 6, 1, 10, 0),
        state_path=state_path,
    )

    assert dbd._TECH_NOTE_TOPICS[0]["title"] in first
    assert dbd._TECH_NOTE_TOPICS[0]["title"] in second
    assert dbd._TECH_NOTE_TOPICS[1]["title"] not in second


def test_daily_tech_note_approved_today_advances_tomorrow(tmp_path):
    state_path = tmp_path / "tech_note_state.json"

    dbd.daily_tech_note(
        now=datetime(2026, 5, 31, 10, 0),
        state_path=state_path,
    )
    approved = dbd.approve_daily_tech_note(
        now=datetime(2026, 5, 31, 21, 0),
        state_path=state_path,
        summary="我整理完第一題重點。",
    )
    same_day = dbd.daily_tech_note(
        now=datetime(2026, 5, 31, 21, 1),
        state_path=state_path,
    )
    next_day = dbd.daily_tech_note(
        now=datetime(2026, 6, 1, 10, 0),
        state_path=state_path,
    )

    assert "已核可" in approved
    state = json.loads(state_path.read_text())
    assert state["review_schedules"]["mini-project-architecture"] == {
        "D+1": "2026-06-01",
        "D+3": "2026-06-03",
        "D+7": "2026-06-07",
        "D+14": "2026-06-14",
    }
    assert "D+1 2026-06-01" in approved
    assert dbd._TECH_NOTE_TOPICS[0]["title"] in same_day
    assert "明天換下一題" in same_day
    assert f"主題：**{dbd._TECH_NOTE_TOPICS[1]['title']}**" in next_day
    assert "今日到期回想（先不看舊答案）" in next_day
    assert "D+1：Mini Project End-to-End Architecture" in next_day
    assert f"主題：**{dbd._TECH_NOTE_TOPICS[0]['title']}**" not in next_day


def test_approve_daily_tech_note_is_idempotent_for_current_topic(tmp_path):
    state_path = tmp_path / "tech_note_state.json"

    dbd.daily_tech_note(
        now=datetime(2026, 5, 31, 10, 0),
        state_path=state_path,
    )
    first = dbd.approve_daily_tech_note(
        now=datetime(2026, 5, 31, 20, 0),
        state_path=state_path,
    )
    second = dbd.approve_daily_tech_note(
        now=datetime(2026, 5, 31, 20, 5),
        state_path=state_path,
    )

    assert "已核可" in first
    assert "已經核可過" in second


def test_preview_cli_does_not_create_state_file(tmp_path, monkeypatch, capsys):
    state_path = tmp_path / "tech_note_state.json"
    monkeypatch.setattr(dbd, "_TECH_NOTE_STATE_FILE", state_path)

    handled = dbd._handle_cli(["--preview-tech-note"])
    out = capsys.readouterr().out

    assert handled is True
    assert "每日技術筆記" in out
    assert not state_path.exists()
