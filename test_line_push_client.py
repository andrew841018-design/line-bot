from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import line_push_client


def test_line_access_token_prefers_refreshed_token(monkeypatch):
    fake_refresh = type(sys)("line_token_refresh")
    fake_refresh.get_line_token = lambda: "fresh-token"
    monkeypatch.setitem(sys.modules, "line_token_refresh", fake_refresh)

    assert line_push_client.line_access_token(fallback_token="stale-token") == "fresh-token"


def test_line_access_token_falls_back_to_config(monkeypatch):
    fake_refresh = type(sys)("line_token_refresh")
    fake_refresh.get_line_token = lambda: ""
    fake_config = type(sys)("config")
    fake_config.settings = SimpleNamespace(line_channel_access_token="env-token")
    monkeypatch.setitem(sys.modules, "line_token_refresh", fake_refresh)
    monkeypatch.setitem(sys.modules, "config", fake_config)

    assert line_push_client.line_access_token() == "env-token"


def test_line_access_token_falls_back_to_passed_token_when_refresh_raises(monkeypatch):
    fake_refresh = type(sys)("line_token_refresh")

    def boom():
        raise RuntimeError("refresh down")

    fake_refresh.get_line_token = boom
    monkeypatch.setitem(sys.modules, "line_token_refresh", fake_refresh)

    assert line_push_client.line_access_token(fallback_token="legacy-token") == "legacy-token"


def test_push_text_raises_on_non_accepted_status(monkeypatch):
    monkeypatch.setattr(line_push_client, "line_access_token", lambda fallback_token=None: "token")

    class FakeSession:
        def post(self, *args, **kwargs):
            return SimpleNamespace(status_code=401, text="Unauthorized")

    with pytest.raises(line_push_client.LinePushError) as exc:
        line_push_client.push_text("G1", "hello", session=FakeSession())

    assert exc.value.status_code == 401


def test_try_push_text_returns_false_on_non_accepted_status(monkeypatch):
    monkeypatch.setattr(line_push_client, "line_access_token", lambda fallback_token=None: "token")

    class FakeSession:
        def post(self, *args, **kwargs):
            return SimpleNamespace(status_code=429, text="quota")

    assert line_push_client.try_push_text("G1", "hello", session=FakeSession()) is False
