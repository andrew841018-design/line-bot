"""Synthetic same-source transit payload regressions; no network calls."""

from datetime import datetime, timedelta
from types import SimpleNamespace
import json
from zoneinfo import ZoneInfo

import pytest
import main
import memory
import gemini_client
import reminder_restatement as rr
import reminder_push

TW = ZoneInfo("Asia/Taipei")
SOURCE = (
    "我明天早上08:10從測試站出發 等72號公車\n\n"
    "因周末班次少。間隔15-25分\n\n需08:10出門 接09:40的X02A國道快速線"
)


def result():
    day = datetime.now(TW).date() + timedelta(days=1)
    return dict(
        year=day.year,
        month=day.month,
        day=day.day,
        hour=8,
        minute=10,
        action="從測試站出發等72號公車",
    )


def test_same_source_full_transit_model_payload(monkeypatch):
    base = result()
    response = SimpleNamespace(text=json.dumps(base))
    monkeypatch.setattr(
        gemini_client,
        "_client",
        SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: response)),
    )
    monkeypatch.setattr(gemini_client, "_track_usage", lambda response: None)
    parsed = gemini_client.extract_reminder(
        SOURCE, today_iso=datetime.now(TW).strftime("%Y-%m-%d %A")
    )
    assert parsed["action"] == SOURCE
    assert {k: parsed[k] for k in base if k != "action"} == {
        k: base[k] for k in base if k != "action"
    }


@pytest.mark.parametrize("path", ["immediate", "precomputed", "pending"])
def test_complete_content_persists_and_renders_without_rescheduling(monkeypatch, path):
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *a, **kw: True)
    monkeypatch.setattr(
        main.gemini_client, "extract_reminder", lambda *a, **kw: result()
    )
    if path == "pending":
        memory.enqueue_pending_reminder("G_TEST", "U_TEST", SOURCE, "transit")
        main._drain_pending_reminders("G_TEST")
    else:
        reply = main._maybe_extract_reminder(
            SOURCE,
            "G_TEST",
            "U_TEST",
            "transit",
            precomputed_result=result() if path == "precomputed" else None,
        )
        assert "09:40的X02A國道快速線" in reply
    rows = memory.list_pending_reminders_full("G_TEST")
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == SOURCE
    when = datetime.fromtimestamp(row["remind_at"], TW)
    assert (when.hour, when.minute) == (8, 10)
    assert "09:40的X02A國道快速線" in reminder_push._format_push_text(row, "1d")
    main._maybe_extract_reminder(
        SOURCE, "G_TEST", "U_TEST", "repeat", precomputed_result=result()
    )
    assert len(memory.list_pending_reminders_full("G_TEST")) == 1


@pytest.mark.parametrize(
    "source",
    [
        "我明天08:10開會，09:40寫報告",
        "我明天08:10搭72號公車",
        "如果明天08:10出發，可以接09:40的X02A公車嗎？",
    ],
)
def test_unrelated_or_uncommitted_source_not_expanded(source):
    base = result()
    assert rr.preserve_transit_details(source, base) == base


def test_transit_preservation_idempotent_and_keeps_negated_detail():
    source = SOURCE + "，不要搭X03號車"
    base = result()
    parsed = rr.preserve_transit_details(source, base)
    assert parsed["action"] == source
    assert rr.preserve_transit_details(source, parsed) == parsed
    assert base["action"] != ""
