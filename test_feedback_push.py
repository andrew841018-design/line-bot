from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import feedback_push


def test_push_uses_refreshed_line_token(monkeypatch):
    monkeypatch.setattr(feedback_push.settings, "line_channel_access_token", "stale-token")
    monkeypatch.setattr(
        feedback_push,
        "line_configuration",
        lambda: SimpleNamespace(access_token="fresh-token"),
    )

    seen: dict[str, str] = {}

    class FakeApiClient:
        def __init__(self, cfg):
            seen["token"] = cfg.access_token

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with (
        patch("feedback_push.ApiClient", FakeApiClient),
        patch("feedback_push.MessagingApi") as messaging_api,
    ):
        messaging_api.return_value.push_message = MagicMock()
        feedback_push._push("G1", "hello", max_retries=1)

    assert seen["token"] == "fresh-token"


def test_line_access_token_falls_back_to_env_token(monkeypatch):
    monkeypatch.setattr(feedback_push.settings, "line_channel_access_token", "env-token")

    monkeypatch.setattr(feedback_push, "line_access_token", lambda: "env-token")

    assert feedback_push._line_access_token() == "env-token"
