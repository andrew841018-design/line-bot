from __future__ import annotations

import argparse
import json
import os
import sqlite3

import pytest

import preflight_cloud as pc


@pytest.fixture(autouse=True)
def _restore_explicit_env_after_test():
    keys = set(pc.EXPLICIT_ENV_KEYS) | {"LINE_BOT_DISABLE_DOTENV"}
    original = {key: os.environ.get(key) for key in keys}
    yield
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_normalize_public_url_requires_https_root_only():
    assert pc.normalize_base_url("https://line-bot.example.com/", public=True) == "https://line-bot.example.com"
    assert pc.normalize_base_url("http://line-bot.example.com", public=True) == ""
    assert pc.normalize_base_url("https://line-bot.example.com/callback", public=True) == ""


def test_run_checks_default_skips_line_live_checks(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        if url.endswith("/health"):
            return 200, "{}"
        if url.endswith("/callback"):
            return 400, '{"detail":"missing signature"}'
        raise AssertionError(f"unexpected external call: {method} {url}")

    monkeypatch.setattr(pc, "_request", fake_request)
    args = argparse.Namespace(
        local_base_url="http://127.0.0.1:8080",
        public_base_url="",
        sqlite_path="/tmp/not-present-line-bot.db",
        require_public_url=False,
        require_unmuted=False,
        require_discord=False,
        require_jobs_disabled=False,
        test_discord=False,
        live_line=False,
        env_file="",
    )

    results = pc.run_checks(args)

    assert pc.exit_code(results) == 0
    assert any(r.name == "LINE live checks" and r.status == "skip" for r in results)
    assert all("api.line.me" not in url for _, url in calls)


def test_require_unmuted_needs_explicit_false(monkeypatch):
    monkeypatch.delenv("BOT_MUTED", raising=False)
    assert pc.check_bot_unmuted(required=True).status == "fail"

    monkeypatch.setenv("BOT_MUTED", "true")
    assert pc.check_bot_unmuted(required=True).status == "fail"

    monkeypatch.setenv("BOT_MUTED", "false")
    assert pc.check_bot_unmuted(required=True).status == "pass"


def test_require_discord_checks_alert_config(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_USER_ID", raising=False)
    assert pc.check_discord_config(required=True).status == "fail"

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("DISCORD_USER_ID", "123")
    assert pc.check_discord_config(required=True).status == "pass"


def test_discord_delivery_test_calls_send_dm(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("DISCORD_USER_ID", "123")
    sent = []

    import notify_discord

    monkeypatch.setattr(notify_discord, "send_dm", lambda msg: sent.append(msg) or True)

    result = pc.check_discord_config(required=True, test_delivery=True)

    assert result.status == "pass"
    assert sent == ["[line_bot_cloud] Discord delivery test"]


def test_jobs_routes_disabled_required(monkeypatch):
    monkeypatch.setenv("JOBS_ROUTES_ENABLED", "0")
    monkeypatch.setattr(pc, "_request", lambda method, url, **kwargs: (404, '{"detail":"Not Found"}'))
    assert pc.check_jobs_routes_disabled("http://127.0.0.1:8080", required=True).status == "pass"

    monkeypatch.setenv("JOBS_ROUTES_ENABLED", "1")
    result = pc.check_jobs_routes_disabled("http://127.0.0.1:8080", required=True)
    assert result.status == "fail"
    assert "cloud-portable" in result.detail


def test_jobs_routes_disabled_required_probes_running_route(monkeypatch):
    monkeypatch.setenv("JOBS_ROUTES_ENABLED", "0")
    monkeypatch.setattr(pc, "_request", lambda method, url, **kwargs: (200, '{"jobs": []}'))

    result = pc.check_jobs_routes_disabled("http://127.0.0.1:8080", required=True)

    assert result.status == "fail"
    assert "/jobs route still responds" in result.detail


def test_load_runtime_env_from_explicit_file(tmp_path, monkeypatch):
    env_file = tmp_path / "line-bot.env"
    env_file.write_text("LINE_BOT_PUBLIC_BASE_URL=https://line-bot.example.com\n")
    monkeypatch.delenv("LINE_BOT_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("LINE_BOT_DISABLE_DOTENV", raising=False)

    loaded = pc.load_runtime_env(str(env_file))
    args = pc.build_arg_parser().parse_args([])
    pc.apply_runtime_defaults(args)

    assert loaded == env_file
    assert args.public_base_url == "https://line-bot.example.com"


def test_explicit_env_file_missing_is_fatal(tmp_path, monkeypatch):
    missing = tmp_path / "missing.env"
    monkeypatch.delenv("LINE_BOT_DISABLE_DOTENV", raising=False)

    try:
        pc.load_runtime_env(str(missing))
    except pc.EnvLoadError as exc:
        assert "explicit env file not found" in str(exc)
    else:
        raise AssertionError("missing explicit env file should fail")


def test_explicit_env_file_is_source_of_truth(tmp_path, monkeypatch):
    env_file = tmp_path / "line-bot.env"
    env_file.write_text("LINE_BOT_PUBLIC_BASE_URL=https://line-bot.example.com\n")
    monkeypatch.delenv("LINE_BOT_DISABLE_DOTENV", raising=False)
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "stale-local-token")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "stale-discord-token")

    pc.load_runtime_env(str(env_file))

    assert os.environ["LINE_BOT_DISABLE_DOTENV"] == "1"
    assert "LINE_CHANNEL_ACCESS_TOKEN" not in os.environ
    assert "DISCORD_BOT_TOKEN" not in os.environ


def test_default_runtime_env_disables_later_dotenv_load(tmp_path, monkeypatch):
    env_file = tmp_path / "line-bot.env"
    env_file.write_text("LINE_BOT_PUBLIC_BASE_URL=https://line-bot.example.com\n")
    monkeypatch.setenv("LINE_BOT_ENV_FILE", str(env_file))
    monkeypatch.delenv("LINE_BOT_DISABLE_DOTENV", raising=False)

    loaded = pc.load_runtime_env(None)

    assert loaded == env_file
    assert os.environ["LINE_BOT_DISABLE_DOTENV"] == "1"


def test_sqlite_integrity_passes(tmp_path):
    db = tmp_path / "line_bot.db"
    conn = sqlite3.connect(db)
    conn.execute("create table t (id integer primary key)")
    conn.commit()
    conn.close()

    result = pc.check_sqlite(str(db))

    assert result.status == "pass"


def test_sqlite_reachable_skips_full_integrity(tmp_path, monkeypatch):
    db = tmp_path / "line_bot.db"
    conn = sqlite3.connect(db)
    conn.execute("create table t (id integer primary key)")
    conn.commit()
    conn.close()

    result = pc.check_sqlite(str(db), full_integrity=False)

    assert result.name == "SQLite reachable"
    assert result.status == "pass"


def test_live_line_checks_alignment_and_e2e(monkeypatch):
    responses = {
        pc.LINE_BOT_INFO: (200, "{}"),
        pc.LINE_WEBHOOK_ENDPOINT: (
            200,
            json.dumps({"endpoint": "https://line-bot.example.com/callback"}),
        ),
        pc.LINE_WEBHOOK_TEST: (200, json.dumps({"success": True, "statusCode": 200})),
    }

    def fake_request(method, url, **kwargs):
        if url.endswith("/health"):
            return 200, "{}"
        if url.endswith("/callback"):
            return 400, '{"detail":"missing signature"}'
        return responses[url]

    monkeypatch.setattr(pc, "_request", fake_request)
    monkeypatch.setattr(pc, "_line_token", lambda: "token")
    monkeypatch.setenv("BOT_MUTED", "false")
    args = argparse.Namespace(
        local_base_url="http://127.0.0.1:8080",
        public_base_url="https://line-bot.example.com",
        sqlite_path="/tmp/not-present-line-bot.db",
        require_public_url=True,
        require_unmuted=True,
        require_discord=False,
        require_jobs_disabled=False,
        test_discord=False,
        live_line=True,
        env_file="",
    )

    results = pc.run_checks(args)

    assert pc.exit_code(results) == 0
    assert any(r.name == "LINE webhook test E2E" and r.status == "pass" for r in results)
