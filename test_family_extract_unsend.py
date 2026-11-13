"""收回訊息（2026-11-13，TODO／紀念日抽取）：main 把收回的那一則轉給兩個模組忘掉。

各模組的 forget_message 有自己的測試（test_todo_extract.py、test_anniversary_extract.py）；這裡只驗
main 的轉交：兩個都叫到、群組白名單照舊、一個出錯不擋另一個、log 只記型別。合成 id。
"""

import logging
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")

import main  # noqa: E402
from linebot.v3.webhooks import GroupSource, UnsendDetail, UnsendEvent  # noqa: E402


def _unsend(group_id: str = "G_TEST", message_id: str = "MSG_GONE") -> UnsendEvent:
    return UnsendEvent(
        type="unsend",
        source=GroupSource(type="group", groupId=group_id, userId="U_MEMBER"),
        timestamp=1_791_700_000_000,
        mode="active",
        webhookEventId="WEBHOOK_TEST",
        deliveryContext={"isRedelivery": False},
        unsend=UnsendDetail(messageId=message_id),
    )


@pytest.fixture
def forgets(monkeypatch):
    import anniversary
    import todo

    calls: list = []
    monkeypatch.setattr(todo, "forget_message", lambda g, m, **k: calls.append(("todo", g, m)), raising=False)
    monkeypatch.setattr(
        anniversary, "forget_message", lambda g, m, **k: calls.append(("anniversary", g, m)), raising=False
    )
    monkeypatch.setattr(main, "_forget_unsent_health_records", lambda event: None)
    monkeypatch.setattr(main, "_forget_unsent_complaint", lambda event: None)
    return calls


def _handle(event, allowed: str = "") -> None:
    with (
        patch.object(main.settings, "allowed_group_ids_raw", allowed),
        patch.object(main.settings, "allowed_group_id", ""),
    ):
        main._handle_event(event)


def test_an_unsent_message_is_forgotten_by_both_jobs(forgets):
    _handle(_unsend())
    assert forgets == [("todo", "G_TEST", "MSG_GONE"), ("anniversary", "G_TEST", "MSG_GONE")]


def test_an_unsend_from_a_group_that_is_not_allowed_is_ignored(forgets):
    _handle(_unsend(), allowed="G_OTHER")
    assert forgets == []


def test_one_job_failing_does_not_stop_the_other_and_the_log_has_no_ids(forgets, monkeypatch, caplog):
    import todo

    def boom(group_id, message_id, **kwargs):
        raise RuntimeError(f"cannot forget {message_id} in {group_id}")

    monkeypatch.setattr(todo, "forget_message", boom, raising=False)
    caplog.set_level(logging.WARNING)

    _handle(_unsend())

    assert forgets == [("anniversary", "G_TEST", "MSG_GONE")]
    assert "todo unsend cleanup failed error_type=RuntimeError" in caplog.text
    assert "MSG_GONE" not in caplog.text and "G_TEST" not in caplog.text
