import notify_discord
import requests


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_notify_quota_pressure_is_disabled(monkeypatch):
    def fail_if_called(_message):
        raise AssertionError("quota-pressure alert should not send Discord DM")

    state = {}
    monkeypatch.setattr(notify_discord, "send_dm", fail_if_called)

    assert notify_discord.notify_quota_pressure("line_bot 共用 key", state) is False
    assert state == {}


def test_discord_timeout_is_capped_for_monitor_sla():
    assert notify_discord.DISCORD_TIMEOUT == (3, 7)


def test_message_5xx_is_ambiguous_not_retryable(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DISCORD_USER_ID", "test-user")
    responses = iter([_Response(200, {"id": "channel"}), _Response(503)])
    monkeypatch.setattr(notify_discord.requests, "post", lambda *a, **k: next(responses))

    result = notify_discord.send_dm_result("message")

    assert result.status == "pending_unknown"


def test_message_4xx_is_definite_failure(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DISCORD_USER_ID", "test-user")
    responses = iter([_Response(200, {"id": "channel"}), _Response(400)])
    monkeypatch.setattr(notify_discord.requests, "post", lambda *a, **k: next(responses))

    assert notify_discord.send_dm_result("message").status == "definite_failed"


def test_channel_network_error_is_definite_failure(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DISCORD_USER_ID", "test-user")
    monkeypatch.setattr(
        notify_discord.requests,
        "post",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("offline")),
    )

    assert notify_discord.send_dm_result("message").status == "definite_failed"


def test_message_network_error_is_ambiguous(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DISCORD_USER_ID", "test-user")
    responses = iter([_Response(200, {"id": "channel"}), requests.Timeout("timeout")])

    def post(*args, **kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(notify_discord.requests, "post", post)

    assert notify_discord.send_dm_result("message").status == "pending_unknown"
