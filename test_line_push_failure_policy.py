from __future__ import annotations

import sys
from types import SimpleNamespace


def test_cwa_alert_does_not_mark_failed_push_as_sent(tmp_path, monkeypatch):
    import cwa_alert

    state_file = tmp_path / "alert_state.json"
    monkeypatch.setattr(cwa_alert, "_STATE_FILE", state_file)
    monkeypatch.setattr(cwa_alert, "GROUP_ID", "G1")
    monkeypatch.setattr(cwa_alert, "line_access_token", lambda: "token")
    monkeypatch.setattr(cwa_alert, "_push", lambda text: False)
    monkeypatch.setattr(
        cwa_alert,
        "_fetch_earthquakes",
        lambda: [{"id": "eq1", "text": "earthquake"}],
    )
    monkeypatch.setattr(cwa_alert, "_fetch_typhoon", lambda: [])

    rc = cwa_alert.main()

    assert rc == 1
    assert not state_file.exists()


def test_ptt_alert_does_not_mark_failed_push_as_sent(tmp_path, monkeypatch):
    import ptt_alert

    state_file = tmp_path / "ptt_alert_state.json"
    monkeypatch.setattr(ptt_alert, "_STATE_FILE", state_file)
    monkeypatch.setattr(ptt_alert, "GROUP_ID", "G1")
    monkeypatch.setattr(ptt_alert, "line_access_token", lambda: "token")
    monkeypatch.setattr(ptt_alert, "_push", lambda text: False)
    monkeypatch.setattr(
        ptt_alert,
        "_fetch_ptt_alerts",
        lambda: [{"id": "a1", "text": "ptt alert"}],
    )

    rc = ptt_alert.main()

    assert rc == 1
    assert not state_file.exists()


def test_weekly_summary_returns_failure_when_push_fails(monkeypatch):
    import weekly_summary

    monkeypatch.setattr(weekly_summary, "GROUP_ID", "G1")
    monkeypatch.setattr(weekly_summary, "line_access_token", lambda: "token")
    monkeypatch.setattr(
        weekly_summary.memory,
        "get_messages_since",
        lambda *a, **kw: [("m1", "__bot__", "bot reply", 0)],
    )
    monkeypatch.setattr(weekly_summary.gemini_client, "chat", lambda *a, **kw: "summary")
    monkeypatch.setattr(weekly_summary, "_push", lambda text: False)
    monkeypatch.setattr(weekly_summary.family_interest, "render_summary", lambda *a, **kw: "")
    monkeypatch.setattr(weekly_summary, "_render_finance_summary", lambda *a, **kw: "")
    monkeypatch.setitem(sys.modules, "finance_view_validator", SimpleNamespace(run=lambda: 0))

    assert weekly_summary.main() == 1
