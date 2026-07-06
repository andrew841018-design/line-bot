import importlib.util
from pathlib import Path


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "ops" / "monitors" / "line_bot.py").exists():
            return parent
    raise AssertionError("cannot locate repo root with ops/monitors/line_bot.py")


def _load_monitor(monkeypatch):
    root = _repo_root()
    monkeypatch.setenv("LINE_BOT_DIR", str(Path(__file__).resolve().parent))
    spec = importlib.util.spec_from_file_location(
        "line_bot_monitor_for_test",
        root / "ops" / "monitors" / "line_bot.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _stub_healthy_environment(monkeypatch, monitor, state=None):
    saved = {}
    state = dict(state or {})
    monkeypatch.setattr(monitor, "load_health_state", lambda: dict(state))
    monkeypatch.setattr(monitor, "save_health_state", lambda d: saved.update(d))
    monkeypatch.setattr(monitor, "proc_alive", lambda pattern: True)
    monkeypatch.setattr(monitor, "http_health", lambda: (True, 200))
    monkeypatch.setattr(monitor, "check_main_py_drift", lambda: 0)
    monkeypatch.setattr(monitor, "cloudflared_origin_reachable", lambda: (True, ""))
    monkeypatch.setattr(monitor, "line_token_check", lambda: (True, ""))
    monkeypatch.setattr(monitor, "webhook_endpoint_check", lambda: (True, ""))
    monkeypatch.setattr(monitor, "sqlite_integrity_check", lambda: (True, ""))
    monkeypatch.setattr(monitor, "count_callback_500_non_local", lambda: (0, [], 0))
    monkeypatch.setattr(
        monitor,
        "count_recent_activity",
        lambda hours=2: {"user_msgs": 0, "bot_msgs": 0, "user_substantive": 0},
    )
    monkeypatch.setattr(monitor, "pending_reply_enabled", lambda: False)
    monkeypatch.setattr(monitor, "read_quota_state", lambda: {})
    return saved


def test_quota_only_issues_do_not_enter_discord_urgent_list(monkeypatch):
    monitor = _load_monitor(monkeypatch)

    quota_issue = monitor._quota_only_issue("🔴 Gemini lite 爆 quota 自修失敗")
    real_issue = "🔴 對話異常且重啟失敗"
    healthy_issue = "✅ SQLite 損毀自修成功"

    urgent = monitor._urgent_discord_issues([
        quota_issue,
        real_issue,
        healthy_issue,
    ])

    assert urgent == [real_issue]
    assert monitor._display_issue(quota_issue) == "🔴 Gemini lite 爆 quota 自修失敗"


def test_quota_remediation_failure_enters_discord_urgent_list(monkeypatch):
    monitor = _load_monitor(monkeypatch)

    quota_pressure = monitor._quota_only_issue("🟡 Gemini lite 爆 quota；等重置")
    remediation_failure = "🔴 Gemini lite 爆 quota 自修失敗 (wait_health_timeout)：quota"

    assert monitor._urgent_discord_issues([
        quota_pressure,
        remediation_failure,
    ]) == [remediation_failure]


def test_discord_send_false_does_not_update_alert_cooldown(monkeypatch):
    monitor = _load_monitor(monkeypatch)
    saved = _stub_healthy_environment(
        monkeypatch,
        monitor,
        {
            "last_alert_ts": 0,
            "last_webhook_check_ts": 9_999_999_999,
            "last_db_check_ts": 9_999_999_999,
        },
    )
    monkeypatch.setattr(
        monitor,
        "count_callback_500_non_local",
        lambda: (1, ["203.0.113.1"], 20),
    )
    monkeypatch.setattr(monitor, "send_dm", lambda msg: False)

    assert monitor.main() == 12
    assert saved.get("last_alert_ts", 0) == 0
    assert not saved.get("alert_issue_ts")


def test_discord_send_none_counts_as_success(monkeypatch):
    monitor = _load_monitor(monkeypatch)
    saved = _stub_healthy_environment(
        monkeypatch,
        monitor,
        {
            "last_alert_ts": 0,
            "last_webhook_check_ts": 9_999_999_999,
            "last_db_check_ts": 9_999_999_999,
        },
    )
    monkeypatch.setattr(
        monitor,
        "count_callback_500_non_local",
        lambda: (1, ["203.0.113.1"], 20),
    )
    monkeypatch.setattr(monitor, "send_dm", lambda msg: None)

    assert monitor.main() == 0
    assert saved.get("last_alert_ts", 0) > 0
    assert saved.get("alert_issue_ts")


def test_alert_cooldown_is_per_issue_type(monkeypatch):
    monitor = _load_monitor(monkeypatch)
    now_ts = 10_000.0
    first = "🔴 外部 callback 500 偵測：近 20 行有 1 筆"
    second = "🔴 Webhook 三段式自修全失敗：endpoint_check=fail"
    state = {
        "alert_issue_ts": {
            monitor._alert_issue_key(first): now_ts - 60,
        }
    }

    due, due_keys = monitor._due_urgent_issues([first, second], state, now_ts)

    assert due == [second]
    assert due_keys == [monitor._alert_issue_key(second)]


def test_quota_exhausted_does_not_probe_or_autofix(monkeypatch):
    monitor = _load_monitor(monkeypatch)
    saved = _stub_healthy_environment(
        monkeypatch,
        monitor,
        {
            "last_alert_ts": 0,
            "last_webhook_check_ts": 9_999_999_999,
            "last_db_check_ts": 9_999_999_999,
            "last_lite_autofix_ts": 0,
        },
    )
    messages = []
    probe_calls = []
    autofix_calls = []

    monkeypatch.setattr(
        monitor,
        "read_quota_state",
        lambda: {"exhausted_until_ts": 9_999_999_999},
    )
    monkeypatch.setattr(
        monitor,
        "probe_gemini",
        lambda model: probe_calls.append(model) or (False, "quota exhausted"),
    )
    monkeypatch.setattr(
        monitor,
        "attempt_auto_fix",
        lambda: autofix_calls.append(1) or (False, "wait_health_timeout"),
    )
    monkeypatch.setattr(monitor, "send_dm", lambda msg: messages.append(msg) or True)

    assert monitor.main() == 0
    assert probe_calls == []
    assert autofix_calls == []
    assert messages == []
    assert saved.get("last_alert_ts", 0) == 0
