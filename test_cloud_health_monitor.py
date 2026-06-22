from __future__ import annotations

from dataclasses import dataclass

import pytest

import cloud_health_monitor as chm


@dataclass
class FakeResult:
    name: str
    status: str
    critical: bool
    detail: str = ""


@pytest.fixture(autouse=True)
def no_real_runtime_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LINE_BOT_ENV_FILE", str(tmp_path / "missing.env"))


def test_monitor_default_does_not_run_line_e2e(monkeypatch, tmp_path, capsys):
    state = tmp_path / "state.json"
    monkeypatch.setattr(chm, "STATE_PATH", state)
    now = [1000.0]
    monkeypatch.setattr(chm.time, "time", lambda: now[0])

    live_values = []

    def fake_run_checks(args):
        live_values.append(args.live_line)
        return [FakeResult("local /health 200", "pass", True)]

    monkeypatch.setattr(chm.preflight_cloud, "run_checks", fake_run_checks)
    monkeypatch.setattr(chm.preflight_cloud, "exit_code", lambda results: 0)

    assert chm.main(["--json"]) == 0
    now[0] = 1100.0
    assert chm.main(["--json"]) == 0

    assert live_values == [False, False]
    assert "live_line" in capsys.readouterr().out


def test_monitor_uses_cheap_sqlite_check(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    monkeypatch.setattr(chm, "STATE_PATH", state)
    monkeypatch.setattr(chm.time, "time", lambda: 1000.0)
    skip_values = []

    def fake_run_checks(args):
        skip_values.append(args.skip_sqlite_integrity)
        return [FakeResult("local /health 200", "pass", True)]

    monkeypatch.setattr(chm.preflight_cloud, "run_checks", fake_run_checks)
    monkeypatch.setattr(chm.preflight_cloud, "exit_code", lambda results: 0)

    assert chm.main(["--json"]) == 0

    assert skip_values == [True]


def test_monitor_loads_default_runtime_env(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    monkeypatch.setattr(chm, "STATE_PATH", state)
    monkeypatch.setattr(chm.time, "time", lambda: 1000.0)
    loaded_env_files = []

    def fake_run_checks(args):
        return [FakeResult("local /health 200", "pass", True)]

    monkeypatch.setattr(
        chm.preflight_cloud,
        "load_runtime_env",
        lambda env_file=None: loaded_env_files.append(env_file),
    )
    monkeypatch.setattr(chm.preflight_cloud, "run_checks", fake_run_checks)
    monkeypatch.setattr(chm.preflight_cloud, "exit_code", lambda results: 0)

    assert chm.main(["--json"]) == 0

    assert loaded_env_files == [None]


def test_monitor_default_runtime_env_feeds_preflight_defaults(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    env_file = tmp_path / "line-bot.env"
    sqlite_path = tmp_path / "cloud.db"
    env_file.write_text(
        "\n".join(
            [
                "LINE_BOT_PUBLIC_BASE_URL=https://line.example.test",
                f"SQLITE_PATH={sqlite_path}",
            ]
        )
    )
    monkeypatch.setattr(chm, "STATE_PATH", state)
    monkeypatch.setattr(chm.time, "time", lambda: 1000.0)
    monkeypatch.setenv("LINE_BOT_ENV_FILE", str(env_file))
    monkeypatch.delenv("LINE_BOT_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("SQLITE_PATH", raising=False)
    captured = []

    def fake_run_checks(args):
        captured.append((args.public_base_url, args.sqlite_path))
        return [FakeResult("local /health 200", "pass", True)]

    monkeypatch.setattr(chm.preflight_cloud, "run_checks", fake_run_checks)
    monkeypatch.setattr(chm.preflight_cloud, "exit_code", lambda results: 0)

    assert chm.main(["--json"]) == 0

    assert captured == [("https://line.example.test", str(sqlite_path))]


def test_monitor_reports_runtime_env_load_error(monkeypatch, capsys):
    def fail_load(env_file=None):
        raise chm.preflight_cloud.EnvLoadError("explicit env file not found")

    monkeypatch.setattr(chm.preflight_cloud, "load_runtime_env", fail_load)
    monkeypatch.setattr(
        chm.preflight_cloud,
        "run_checks",
        lambda args: (_ for _ in ()).throw(AssertionError("run_checks called")),
    )

    assert chm.main(["--env-file", "/missing.env", "--json"]) == 1

    payload = capsys.readouterr().out
    assert '"ok": false' in payload
    assert "runtime env file loaded" in payload
    assert "explicit env file not found" in payload


def test_monitor_runs_line_e2e_only_when_enabled_and_interval_due(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    monkeypatch.setattr(chm, "STATE_PATH", state)
    monkeypatch.setattr(chm, "LINE_INTERVAL_SEC", 180)
    now = [1000.0]
    monkeypatch.setattr(chm.time, "time", lambda: now[0])

    live_values = []

    def fake_run_checks(args):
        live_values.append(args.live_line)
        return [FakeResult("local /health 200", "pass", True)]

    monkeypatch.setattr(chm.preflight_cloud, "run_checks", fake_run_checks)
    monkeypatch.setattr(chm.preflight_cloud, "exit_code", lambda results: 0)

    assert chm.main(["--live-line", "--json"]) == 0
    now[0] = 1100.0
    assert chm.main(["--live-line", "--json"]) == 0
    now[0] = 1181.0
    assert chm.main(["--json"]) == 0

    assert live_values == [True, False, False]


def test_monitor_live_line_runs_again_after_interval(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    monkeypatch.setattr(chm, "STATE_PATH", state)
    monkeypatch.setattr(chm, "LINE_INTERVAL_SEC", 180)
    now = [1000.0]
    monkeypatch.setattr(chm.time, "time", lambda: now[0])
    live_values = []

    def fake_run_checks(args):
        live_values.append(args.live_line)
        return [FakeResult("local /health 200", "pass", True)]

    monkeypatch.setattr(chm.preflight_cloud, "run_checks", fake_run_checks)
    monkeypatch.setattr(chm.preflight_cloud, "exit_code", lambda results: 0)

    assert chm.main(["--live-line", "--json"]) == 0
    now[0] = 1181.0
    assert chm.main(["--live-line", "--json"]) == 0

    assert live_values == [True, True]


def test_monitor_sends_discord_immediately_for_new_critical_failure(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    monkeypatch.setattr(chm, "STATE_PATH", state)
    monkeypatch.setattr(chm.time, "time", lambda: 1000.0)
    result = FakeResult("public /health 200", "fail", True, "http=530")

    monkeypatch.setattr(chm.preflight_cloud, "run_checks", lambda args: [result])
    monkeypatch.setattr(chm.preflight_cloud, "exit_code", lambda results: 1)
    sent = []
    monkeypatch.setattr(chm, "_send_discord", lambda msg: sent.append(msg) or True)

    assert chm.main(["--send-alerts", "--json"]) == 1

    assert len(sent) == 1
    assert "UNHEALTHY" in sent[0]
    assert "public /health 200" in sent[0]


def test_monitor_dry_run_does_not_send_discord(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    monkeypatch.setattr(chm, "STATE_PATH", state)
    monkeypatch.setattr(chm.time, "time", lambda: 1000.0)
    result = FakeResult("local /health 200", "fail", True, "http=0")

    monkeypatch.setattr(chm.preflight_cloud, "run_checks", lambda args: [result])
    monkeypatch.setattr(chm.preflight_cloud, "exit_code", lambda results: 1)
    monkeypatch.setattr(chm, "_send_discord", lambda msg: (_ for _ in ()).throw(AssertionError("sent")))

    assert chm.main(["--json"]) == 1
