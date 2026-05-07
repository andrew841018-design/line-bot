"""
Tests for bot_health_monitor.py:
  A. cloudflared_url_alive() — process check + URL reachability check
  B. L0d autofix fallback chain — webhook_endpoint_check fail → autofix_webhook_endpoint
     fail → restart_cloudflared fallback → re-run webhook_endpoint_check
  C. webhook check cadence: 6 hours (21600s) instead of 24h
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import bot_health_monitor as bhm  # noqa: E402


# ── A. cloudflared_url_alive ──────────────────────────────────────────────────


def _patch_cloudflared_log(tmp_path: Path, content: str) -> None:
    """Point bhm at a temp cloudflared.log."""
    log = tmp_path / "cloudflared.log"
    log.write_text(content)
    # Patch BASE-derived path: bhm uses BASE / "cloudflared.log" inline
    return log


def test_cloudflared_url_alive_returns_true_on_200(tmp_path, monkeypatch):
    """When cloudflared.log has a URL and curl returns 200, function returns (True, "")."""
    _patch_cloudflared_log(
        tmp_path,
        "2026-05-05T05:29:11Z INF |  https://abc-def.trycloudflare.com  |\n",
    )
    monkeypatch.setattr(bhm, "BASE", tmp_path)

    fake_curl = MagicMock()
    fake_curl.stdout = "200"
    with patch.object(bhm.subprocess, "run", return_value=fake_curl) as run_mock:
        ok, msg = bhm.cloudflared_url_alive()

    assert ok is True
    # Verify curl was called against {url}/health
    args = run_mock.call_args[0][0]
    assert "https://abc-def.trycloudflare.com/health" in args


def test_cloudflared_url_alive_returns_false_when_curl_nxdomain(tmp_path, monkeypatch):
    """The exact bug we just hit: process alive but URL was retired by Cloudflare → curl fails."""
    _patch_cloudflared_log(
        tmp_path,
        "2026-05-05T05:29:11Z INF |  https://retired.trycloudflare.com  |\n",
    )
    monkeypatch.setattr(bhm, "BASE", tmp_path)

    # curl exit code 6 / 0 stdout = name not resolved
    fake_curl = MagicMock()
    fake_curl.stdout = "000"
    with patch.object(bhm.subprocess, "run", return_value=fake_curl):
        ok, reason = bhm.cloudflared_url_alive()

    assert ok is False
    assert reason  # has some reason text


def test_cloudflared_url_alive_no_url_in_log(tmp_path, monkeypatch):
    """If cloudflared.log has no URL yet, return (False, reason)."""
    _patch_cloudflared_log(tmp_path, "no url here yet\n")
    monkeypatch.setattr(bhm, "BASE", tmp_path)

    ok, reason = bhm.cloudflared_url_alive()
    assert ok is False
    assert "URL" in reason or "url" in reason


def test_cloudflared_url_alive_no_log_file(tmp_path, monkeypatch):
    """If cloudflared.log doesn't exist, return (False, reason)."""
    monkeypatch.setattr(bhm, "BASE", tmp_path)
    ok, reason = bhm.cloudflared_url_alive()
    assert ok is False


# ── B. Autofix fallback chain ─────────────────────────────────────────────────


def test_l0d_fallback_runs_restart_cloudflared_when_autofix_fails(tmp_path, monkeypatch):
    """When autofix_webhook_endpoint fails, restart_cloudflared must be invoked,
    then webhook_endpoint_check re-runs once more."""
    monkeypatch.setattr(bhm, "BASE", tmp_path)
    monkeypatch.setattr(bhm, "HEALTH_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(bhm, "QUOTA_STATE_FILE", tmp_path / "quota.json")
    monkeypatch.setattr(bhm, "PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(bhm, "DB_FILE", tmp_path / "noop.db")

    # Force into L0d branch
    monkeypatch.setattr(bhm, "proc_alive", lambda p: True)
    monkeypatch.setattr(bhm, "http_health", lambda: (True, 200))
    monkeypatch.setattr(bhm, "line_token_check", lambda: (True, ""))
    monkeypatch.setattr(bhm, "sqlite_integrity_check", lambda: (True, ""))
    monkeypatch.setattr(bhm, "count_recent_activity",
                        lambda hours=2: {"user_msgs": 0, "bot_msgs": 0, "user_substantive": 0})
    monkeypatch.setattr(bhm, "count_pending", lambda: 0)
    monkeypatch.setattr(bhm, "read_quota_state", lambda: {})
    monkeypatch.setattr(bhm, "cloudflared_url_alive", lambda: (True, ""))

    webhook_calls = []

    def fake_webhook_check():
        webhook_calls.append(1)
        return False, "first fail"

    autofix_called = []

    def fake_autofix_webhook():
        autofix_called.append(1)
        return False, "autofix failed"

    restart_called = []

    def fake_restart_cf():
        restart_called.append(1)
        return True, "https://new.trycloudflare.com"

    monkeypatch.setattr(bhm, "webhook_endpoint_check", fake_webhook_check)
    monkeypatch.setattr(bhm, "autofix_webhook_endpoint", fake_autofix_webhook)
    monkeypatch.setattr(bhm, "restart_cloudflared", fake_restart_cf)
    monkeypatch.setattr(bhm, "send_dm", lambda msg: None)

    rc = bhm.main()
    assert rc == 0
    # Three webhook checks: initial fail, plus one re-check after restart_cloudflared fallback
    # (autofix_webhook FAILED so no recheck after step 2; restart_cloudflared SUCCEEDED so we recheck)
    assert len(autofix_called) == 1
    assert len(restart_called) == 1
    assert len(webhook_calls) == 2  # initial + after restart


def test_l0d_alert_message_says_all_three_failed(tmp_path, monkeypatch):
    """When all three (autofix, restart_cloudflared, recheck) fail, alert mentions all."""
    monkeypatch.setattr(bhm, "BASE", tmp_path)
    monkeypatch.setattr(bhm, "HEALTH_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(bhm, "QUOTA_STATE_FILE", tmp_path / "quota.json")
    monkeypatch.setattr(bhm, "PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(bhm, "DB_FILE", tmp_path / "noop.db")

    monkeypatch.setattr(bhm, "proc_alive", lambda p: True)
    monkeypatch.setattr(bhm, "http_health", lambda: (True, 200))
    monkeypatch.setattr(bhm, "line_token_check", lambda: (True, ""))
    monkeypatch.setattr(bhm, "sqlite_integrity_check", lambda: (True, ""))
    monkeypatch.setattr(bhm, "count_recent_activity",
                        lambda hours=2: {"user_msgs": 0, "bot_msgs": 0, "user_substantive": 0})
    monkeypatch.setattr(bhm, "count_pending", lambda: 0)
    monkeypatch.setattr(bhm, "read_quota_state", lambda: {})
    monkeypatch.setattr(bhm, "cloudflared_url_alive", lambda: (True, ""))

    monkeypatch.setattr(bhm, "webhook_endpoint_check", lambda: (False, "wh fail"))
    monkeypatch.setattr(bhm, "autofix_webhook_endpoint", lambda: (False, "autofix fail"))
    monkeypatch.setattr(bhm, "restart_cloudflared", lambda: (False, ""))

    sent = []
    monkeypatch.setattr(bhm, "send_dm", lambda msg: sent.append(msg))

    rc = bhm.main()
    assert rc == 0
    assert sent, "alert should have been sent"
    msg = sent[0]
    # Alert mentions all three failures
    assert "Webhook" in msg or "webhook" in msg
    assert "🔴" in msg


def test_l0d_success_via_restart_cloudflared_fallback(tmp_path, monkeypatch):
    """Fallback chain: webhook fails → autofix fails → restart_cf succeeds → recheck succeeds."""
    monkeypatch.setattr(bhm, "BASE", tmp_path)
    monkeypatch.setattr(bhm, "HEALTH_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(bhm, "QUOTA_STATE_FILE", tmp_path / "quota.json")
    monkeypatch.setattr(bhm, "PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(bhm, "DB_FILE", tmp_path / "noop.db")

    monkeypatch.setattr(bhm, "proc_alive", lambda p: True)
    monkeypatch.setattr(bhm, "http_health", lambda: (True, 200))
    monkeypatch.setattr(bhm, "line_token_check", lambda: (True, ""))
    monkeypatch.setattr(bhm, "sqlite_integrity_check", lambda: (True, ""))
    monkeypatch.setattr(bhm, "count_recent_activity",
                        lambda hours=2: {"user_msgs": 0, "bot_msgs": 0, "user_substantive": 0})
    monkeypatch.setattr(bhm, "count_pending", lambda: 0)
    monkeypatch.setattr(bhm, "read_quota_state", lambda: {})
    monkeypatch.setattr(bhm, "cloudflared_url_alive", lambda: (True, ""))

    state = {"calls": 0}

    def webhook_check():
        state["calls"] += 1
        return (state["calls"] >= 2, "" if state["calls"] >= 2 else "fail")

    monkeypatch.setattr(bhm, "webhook_endpoint_check", webhook_check)
    monkeypatch.setattr(bhm, "autofix_webhook_endpoint", lambda: (False, "drift fix didn't work"))
    monkeypatch.setattr(bhm, "restart_cloudflared", lambda: (True, "https://new.trycloudflare.com"))

    sent = []
    monkeypatch.setattr(bhm, "send_dm", lambda msg: sent.append(msg))

    rc = bhm.main()
    assert rc == 0
    # 2026-05-06 policy change: autofix-only events do NOT alert.
    # The system handled it; nothing the user needs to do → keep Discord quiet.
    assert sent == [], "autofix-only success must not push Discord under new policy"


# ── C. webhook check cadence (6 hours) ────────────────────────────────────────


def test_webhook_check_cadence_is_6_hours(tmp_path, monkeypatch):
    """Webhook check should run when last check was >6h ago, NOT 24h."""
    import time as _time

    monkeypatch.setattr(bhm, "BASE", tmp_path)
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(bhm, "HEALTH_STATE_FILE", state_file)
    monkeypatch.setattr(bhm, "QUOTA_STATE_FILE", tmp_path / "quota.json")
    monkeypatch.setattr(bhm, "PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(bhm, "DB_FILE", tmp_path / "noop.db")

    # Last check was 7 hours ago — under old 24h gate would SKIP, under new 6h gate must RUN
    seven_hours_ago = _time.time() - (7 * 3600)
    state_file.write_text(f'{{"last_webhook_check_ts": {seven_hours_ago}}}')

    monkeypatch.setattr(bhm, "proc_alive", lambda p: True)
    monkeypatch.setattr(bhm, "http_health", lambda: (True, 200))
    monkeypatch.setattr(bhm, "line_token_check", lambda: (True, ""))
    monkeypatch.setattr(bhm, "sqlite_integrity_check", lambda: (True, ""))
    monkeypatch.setattr(bhm, "count_recent_activity",
                        lambda hours=2: {"user_msgs": 0, "bot_msgs": 0, "user_substantive": 0})
    monkeypatch.setattr(bhm, "count_pending", lambda: 0)
    monkeypatch.setattr(bhm, "read_quota_state", lambda: {})
    monkeypatch.setattr(bhm, "cloudflared_url_alive", lambda: (True, ""))

    called = []
    monkeypatch.setattr(bhm, "webhook_endpoint_check",
                        lambda: (called.append(1), (True, ""))[1])
    monkeypatch.setattr(bhm, "send_dm", lambda msg: None)

    bhm.main()
    assert called, "webhook check must run when last check was 7h ago (new 6h cadence)"


def test_webhook_check_skipped_when_under_6_hours(tmp_path, monkeypatch):
    """Webhook check should NOT run when last check was <6h ago."""
    import time as _time

    monkeypatch.setattr(bhm, "BASE", tmp_path)
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(bhm, "HEALTH_STATE_FILE", state_file)
    monkeypatch.setattr(bhm, "QUOTA_STATE_FILE", tmp_path / "quota.json")
    monkeypatch.setattr(bhm, "PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(bhm, "DB_FILE", tmp_path / "noop.db")

    one_hour_ago = _time.time() - 3600
    state_file.write_text(f'{{"last_webhook_check_ts": {one_hour_ago}}}')

    monkeypatch.setattr(bhm, "proc_alive", lambda p: True)
    monkeypatch.setattr(bhm, "http_health", lambda: (True, 200))
    monkeypatch.setattr(bhm, "line_token_check", lambda: (True, ""))
    monkeypatch.setattr(bhm, "sqlite_integrity_check", lambda: (True, ""))
    monkeypatch.setattr(bhm, "count_recent_activity",
                        lambda hours=2: {"user_msgs": 0, "bot_msgs": 0, "user_substantive": 0})
    monkeypatch.setattr(bhm, "count_pending", lambda: 0)
    monkeypatch.setattr(bhm, "read_quota_state", lambda: {})
    monkeypatch.setattr(bhm, "cloudflared_url_alive", lambda: (True, ""))

    called = []
    monkeypatch.setattr(bhm, "webhook_endpoint_check",
                        lambda: (called.append(1), (True, ""))[1])
    monkeypatch.setattr(bhm, "send_dm", lambda msg: None)

    bhm.main()
    assert not called, "webhook check must NOT run when last check was 1h ago"


# ── A2. cloudflared L0b: process alive but URL dead → restart ─────────────────


def test_l0b_treats_url_dead_as_cloudflared_down(tmp_path, monkeypatch):
    """If cloudflared process is alive but URL is unreachable, must trigger restart_cloudflared."""
    monkeypatch.setattr(bhm, "BASE", tmp_path)
    monkeypatch.setattr(bhm, "HEALTH_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(bhm, "QUOTA_STATE_FILE", tmp_path / "quota.json")
    monkeypatch.setattr(bhm, "PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(bhm, "DB_FILE", tmp_path / "noop.db")

    # uvicorn alive; cloudflared process alive but URL dead
    def proc_alive(pat):
        return True

    monkeypatch.setattr(bhm, "proc_alive", proc_alive)
    monkeypatch.setattr(bhm, "http_health", lambda: (True, 200))
    monkeypatch.setattr(bhm, "cloudflared_url_alive", lambda: (False, "NXDOMAIN on stale URL"))
    monkeypatch.setattr(bhm, "line_token_check", lambda: (True, ""))
    monkeypatch.setattr(bhm, "sqlite_integrity_check", lambda: (True, ""))
    monkeypatch.setattr(bhm, "count_recent_activity",
                        lambda hours=2: {"user_msgs": 0, "bot_msgs": 0, "user_substantive": 0})
    monkeypatch.setattr(bhm, "count_pending", lambda: 0)
    monkeypatch.setattr(bhm, "read_quota_state", lambda: {})
    monkeypatch.setattr(bhm, "webhook_endpoint_check", lambda: (True, ""))

    restart_called = []

    def fake_restart_cf():
        restart_called.append(1)
        return True, "https://new.trycloudflare.com"

    monkeypatch.setattr(bhm, "restart_cloudflared", fake_restart_cf)
    monkeypatch.setattr(bhm, "send_dm", lambda msg: None)

    bhm.main()
    assert restart_called, "stale cloudflared URL must trigger restart_cloudflared"


# ── D. Notification policy: only urgent issues push Discord ───────────────────
#
# 2026-05-06: Andrew reported half-hourly Discord pings caused by Gemini lite
# quota probes flapping. Each cycle hit "✅ 自修成功" which bypassed the 60-min
# dedupe (auto_fixed branch). Policy now: ✅-only payloads stay in stdout log,
# Discord push reserved for 🔴/🟡 (anything still requiring human attention).


def _baseline_healthy(tmp_path, monkeypatch):
    """All checks green; tests below override only the bits they need."""
    monkeypatch.setattr(bhm, "BASE", tmp_path)
    monkeypatch.setattr(bhm, "HEALTH_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(bhm, "QUOTA_STATE_FILE", tmp_path / "quota.json")
    monkeypatch.setattr(bhm, "PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(bhm, "DB_FILE", tmp_path / "noop.db")

    monkeypatch.setattr(bhm, "proc_alive", lambda p: True)
    monkeypatch.setattr(bhm, "http_health", lambda: (True, 200))
    monkeypatch.setattr(bhm, "line_token_check", lambda: (True, ""))
    monkeypatch.setattr(bhm, "sqlite_integrity_check", lambda: (True, ""))
    monkeypatch.setattr(bhm, "cloudflared_url_alive", lambda: (True, ""))
    monkeypatch.setattr(bhm, "webhook_endpoint_check", lambda: (True, ""))
    monkeypatch.setattr(
        bhm,
        "count_recent_activity",
        lambda hours=2: {"user_msgs": 0, "bot_msgs": 0, "user_substantive": 0},
    )
    monkeypatch.setattr(bhm, "count_pending", lambda: 0)


def test_autofix_only_does_not_alert(tmp_path, monkeypatch):
    """bot_thinks_exhausted=True + lite probe OK + autofix succeeds → only ✅ in
    issues. Under new policy this MUST NOT push Discord — it's the recurring
    half-hourly noise Andrew complained about."""
    _baseline_healthy(tmp_path, monkeypatch)

    # bot 自認 quota 爆 → 走 L4 lite probe → 探測 OK → attempt_auto_fix 成功
    monkeypatch.setattr(
        bhm, "read_quota_state", lambda: {"exhausted_until_ts": 9_999_999_999.0}
    )
    monkeypatch.setattr(bhm, "probe_gemini", lambda model: (True, ""))
    monkeypatch.setattr(bhm, "attempt_auto_fix", lambda: True)

    sent = []
    monkeypatch.setattr(bhm, "send_dm", lambda msg: sent.append(msg))

    rc = bhm.main()
    assert rc == 0
    assert sent == [], (
        "autofix-only event (✅) must stay silent — it's the recurring noise "
        "from Gemini lite probe flapping"
    )


def test_urgent_red_issue_still_alerts(tmp_path, monkeypatch):
    """Real failure (🔴 cloudflared down + restart fails) MUST still push
    Discord. Defends against the policy change accidentally muting genuine
    incidents."""
    _baseline_healthy(tmp_path, monkeypatch)

    # cloudflared 死掉 + 重啟失敗 → 產生 🔴
    def proc_alive(pat):
        return "cloudflared" not in pat  # uvicorn 活，cloudflared 死

    monkeypatch.setattr(bhm, "proc_alive", proc_alive)
    monkeypatch.setattr(bhm, "restart_cloudflared", lambda: (False, ""))
    monkeypatch.setattr(bhm, "read_quota_state", lambda: {})

    sent = []
    monkeypatch.setattr(bhm, "send_dm", lambda msg: sent.append(msg))

    rc = bhm.main()
    assert rc == 0
    assert sent, "🔴 incident (cloudflared dead + restart failed) must alert"
    assert "🔴" in sent[0]


def test_urgent_yellow_issue_still_alerts(tmp_path, monkeypatch):
    """🟡 ongoing anomaly (silent_anomaly with LINE quota dead) must alert —
    the user can't fix it but needs to know the bot is silent."""
    _baseline_healthy(tmp_path, monkeypatch)

    # 家人發 5 則實質訊息但 bot 0 回 → silent_anomaly
    monkeypatch.setattr(
        bhm,
        "count_recent_activity",
        lambda hours=2: {"user_msgs": 5, "bot_msgs": 0, "user_substantive": 5},
    )
    # LINE push 配額爆 → 不能自修 → 標 🔴 純通知
    monkeypatch.setattr(bhm, "line_push_quota_likely_exhausted", lambda: True)
    monkeypatch.setattr(bhm, "read_quota_state", lambda: {})

    sent = []
    monkeypatch.setattr(bhm, "send_dm", lambda msg: sent.append(msg))

    rc = bhm.main()
    assert rc == 0
    assert sent, "silent anomaly with quota exhausted must alert"
    assert "🔴" in sent[0] or "🟡" in sent[0]


def test_mixed_urgent_and_autofix_alerts_with_only_urgent(tmp_path, monkeypatch):
    """If both ✅ autofix and 🔴 unfixable issues happen the same tick, alert
    fires AND the body should still surface the 🔴 (don't drop it just because
    autofix happened too)."""
    _baseline_healthy(tmp_path, monkeypatch)

    # bot 自認爆 + lite OK + 自修成功 → ✅
    monkeypatch.setattr(
        bhm, "read_quota_state", lambda: {"exhausted_until_ts": 9_999_999_999.0}
    )
    monkeypatch.setattr(bhm, "probe_gemini", lambda model: (True, ""))
    monkeypatch.setattr(bhm, "attempt_auto_fix", lambda: True)
    # 同時有 silent_anomaly + LINE quota dead → 🔴
    monkeypatch.setattr(
        bhm,
        "count_recent_activity",
        lambda hours=2: {"user_msgs": 5, "bot_msgs": 0, "user_substantive": 5},
    )
    monkeypatch.setattr(bhm, "line_push_quota_likely_exhausted", lambda: True)

    sent = []
    monkeypatch.setattr(bhm, "send_dm", lambda msg: sent.append(msg))

    rc = bhm.main()
    assert rc == 0
    assert sent, "mixed payload with 🔴 component must alert"
    assert "🔴" in sent[0], "alert body must include the 🔴 incident line"
