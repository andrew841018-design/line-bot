import notify_discord


def test_notify_quota_pressure_is_disabled(monkeypatch):
    def fail_if_called(_message):
        raise AssertionError("quota-pressure alert should not send Discord DM")

    state = {}
    monkeypatch.setattr(notify_discord, "send_dm", fail_if_called)

    assert notify_discord.notify_quota_pressure("line_bot 共用 key", state) is False
    assert state == {}


def test_discord_timeout_is_capped_for_monitor_sla():
    assert notify_discord.DISCORD_TIMEOUT == (3, 7)
