"""preflight Discord alert filter — transient (Gemini 429 / 503) suppression.

Scope: `_is_transient_error` heuristic + `_finalize` alert dispatch behavior.
Mocks `_send_discord_alert` and `_emit_jsonl`. JSONL log must always emit
regardless of alert filter; full audit trail preserved.
"""
import argparse
from unittest.mock import patch

import preflight_check as pf


def _make_result(idx, name, *, critical, status, detail=""):
    r = pf.CheckResult(idx, name, critical=critical)
    r.status = status
    r.detail = detail
    return r


def _args(**kw):
    return argparse.Namespace(force=kw.get("force", True), dry_run=kw.get("dry_run", True))


# ---------- tunnel URL validation ----------

def test_preflight_rejects_reserved_trycloudflare_api_url():
    assert pf._normalize_tunnel_url("https://api.trycloudflare.com") == ""
    assert pf._normalize_tunnel_url("https://api.trycloudflare.com/tunnel") == ""
    assert pf._normalize_tunnel_url("https://fresh.trycloudflare.com") == (
        "https://fresh.trycloudflare.com"
    )


# ---------- _is_transient_error ----------

def test_gemini_429_resource_exhausted_is_transient():
    r = _make_result(11, "Gemini main probe", critical=True, status="fail",
                     detail="429 RESOURCE_EXHAUSTED. {'error': {'code': 429}}")
    assert pf._is_transient_error(r) is True


def test_gemini_503_unavailable_is_transient():
    r = _make_result(12, "Gemini lite probe", critical=True, status="fail",
                     detail="503 UNAVAILABLE")
    assert pf._is_transient_error(r) is True


def test_gemini_503_deadline_exceeded_is_transient():
    r = _make_result(11, "Gemini main probe", critical=True, status="fail",
                     detail="503 DEADLINE_EXCEEDED")
    assert pf._is_transient_error(r) is True


def test_line_429_not_filtered_safety_critical():
    # codex C1: LINE API 429 must NOT be filtered — real failure
    r = _make_result(8, "LINE token /v2/bot/info 200", critical=True, status="fail",
                     detail="http 429 body=rate limit")
    assert pf._is_transient_error(r) is False


def test_cloudflared_503_not_filtered():
    r = _make_result(6, "external https://x.trycloudflare.com/health 200",
                     critical=True, status="fail",
                     detail="3 retry fail; last http=503 body=UNAVAILABLE")
    # Name doesn't start with "Gemini" → not filtered even if pattern matches
    assert pf._is_transient_error(r) is False


def test_gemini_500_not_transient():
    r = _make_result(11, "Gemini main probe", critical=True, status="fail",
                     detail="500 INTERNAL")
    assert pf._is_transient_error(r) is False


def test_gemini_empty_detail_not_transient():
    r = _make_result(11, "Gemini main probe", critical=True, status="fail", detail="")
    assert pf._is_transient_error(r) is False


def test_gemini_503_bytes_false_positive_guard():
    # GP2 #6: "503 bytes" without keyword must NOT trigger
    r = _make_result(11, "Gemini main probe", critical=True, status="fail",
                     detail="response body 503 bytes received")
    assert pf._is_transient_error(r) is False


def test_gemini_resource_exhausted_without_status_code_not_transient():
    # Defensive: keyword alone (no 429/503) shouldn't trigger
    r = _make_result(11, "Gemini main probe", critical=True, status="fail",
                     detail="RESOURCE_EXHAUSTED token quota")
    assert pf._is_transient_error(r) is False


# ---------- startup retry guards ----------

@patch("preflight_check.time.sleep", lambda *_args, **_kwargs: None)
def test_local_health_retries_until_ready():
    calls = iter([(0, ""), (0, ""), (200, "ok")])

    with patch("preflight_check._curl", side_effect=lambda *_a, **_kw: next(calls)):
        result = pf.check_2_local_health()

    assert result.status == "pass"
    assert "attempt=3" in result.detail


@patch("preflight_check.time.sleep", lambda *_args, **_kwargs: None)
def test_local_health_fails_after_retry_budget():
    with patch("preflight_check._curl", return_value=(0, "curl_timeout")):
        result = pf.check_2_local_health()

    assert result.status == "fail"
    assert "retry fail" in result.detail


def test_uvicorn_alive_accepts_launchd_when_pgrep_misses():
    with patch("preflight_check._pgrep", return_value=None), \
         patch("preflight_check._launchctl_running", return_value=True):
        result = pf.check_1_uvicorn_alive()

    assert result.status == "pass"


def test_cloudflared_alive_accepts_launchd_when_pgrep_misses():
    with patch("preflight_check._pgrep", return_value=None), \
         patch("preflight_check._launchctl_running", return_value=True):
        result = pf.check_3_cloudflared_alive()

    assert result.status == "pass"


@patch("preflight_check.time.sleep", lambda *_args, **_kwargs: None)
def test_external_tunnel_direct_curl_failure_is_diagnostic_skip():
    with patch("preflight_check._curl", return_value=(0, "dns_fail")):
        result = pf.check_5_external_tunnel("https://x.trycloudflare.com")

    assert result.critical is False
    assert result.status == "skip"
    assert "LINE webhook E2E" in result.detail


@patch("preflight_check.time.sleep", lambda *_args, **_kwargs: None)
def test_webhook_put_retries_invalid_endpoint_until_line_accepts():
    calls = [
        (400, '{"message":"Invalid webhook endpoint URL"}'),
        (200, "{}"),
    ]

    with patch("preflight_check._curl_with_token", side_effect=calls) as curl:
        code, body, attempts = pf._put_line_webhook_endpoint(
            "fake-token",
            "https://fresh.trycloudflare.com/callback",
        )

    assert code == 200
    assert body == "{}"
    assert attempts == 2
    assert curl.call_count == 2


def test_line_path_recovery_warranted_for_e2e_only_530():
    alignment = _make_result(
        9,
        "LINE webhook URL 對齊 cloudflared",
        critical=True,
        status="pass",
    )
    e2e = _make_result(
        10,
        "LINE → cloudflared → /callback E2E",
        critical=True,
        status="fail",
        detail="success=False statusCode=530 reason='ERROR_STATUS_CODE'",
    )

    assert pf._line_path_recovery_warranted(alignment, e2e) is True


@patch("preflight_check._send_discord_alert")
@patch("preflight_check._emit_jsonl")
def test_run_restarts_cloudflared_when_line_webhook_path_is_stale(mock_jsonl, mock_alert):
    def result(idx, name, *, status="pass", critical=True, detail=""):
        r = pf.CheckResult(idx, name, critical=critical)
        r.status = status
        r.detail = detail
        return r

    stale = "https://stale.trycloudflare.com"
    fresh = "https://fresh.trycloudflare.com"
    alignment_calls = []

    def fake_alignment(tunnel_url, *, dry_run=False):
        alignment_calls.append(tunnel_url)
        if tunnel_url == stale:
            return (
                result(
                    9,
                    "LINE webhook URL 對齊 cloudflared",
                    status="fail",
                    detail='PUT http 400 after 4 attempts: {"message":"Invalid webhook endpoint URL"}',
                ),
                f"{stale}/callback",
            )
        return result(9, "LINE webhook URL 對齊 cloudflared"), f"{fresh}/callback"

    e2e_calls = []

    def fake_e2e(*, dry_run=False):
        e2e_calls.append(1)
        if len(e2e_calls) == 1:
            return result(
                10,
                "LINE → cloudflared → /callback E2E",
                status="fail",
                detail="success=False statusCode=530 reason='ERROR_STATUS_CODE'",
            )
        return result(10, "LINE → cloudflared → /callback E2E")

    with patch("preflight_check.check_1_uvicorn_alive", return_value=result(1, "uvicorn process alive")), \
         patch("preflight_check.check_2_local_health", return_value=result(2, "local /health 200")), \
         patch(
             "preflight_check.check_3_cloudflared_alive",
             side_effect=[
                 result(
                     3,
                     "cloudflared process alive",
                     status="fail",
                     detail="pgrep/launchctl 都沒找到 cloudflared",
                 ),
                 result(3, "cloudflared process alive"),
             ],
         ), \
         patch(
             "preflight_check.check_4_stash_valid",
             return_value=(result(4, "cloudflared URL stash 可讀 + 安全"), stale),
         ), \
         patch(
             "preflight_check.check_4c_double_source",
             side_effect=[
                 result(
                     5,
                     "cloudflared metrics 內部 URL 對 stash",
                     status="fail",
                     detail="cloudflared metrics port 抓不到",
                 ),
                 result(5, "cloudflared metrics 內部 URL 對 stash"),
             ],
         ), \
         patch("preflight_check.check_5_external_tunnel", return_value=result(6, "external tunnel diagnostic", critical=False)), \
         patch("preflight_check.check_5b_local_callback_route", return_value=result(7, "/callback no-sig → 400 missing")), \
         patch("preflight_check.check_6_line_token", return_value=result(8, "LINE token /v2/bot/info 200")), \
         patch("preflight_check.check_7_webhook_alignment", side_effect=fake_alignment), \
         patch("preflight_check.check_8_line_e2e", side_effect=fake_e2e), \
         patch("preflight_check.check_9_gemini", side_effect=lambda model, idx, label: result(idx, f"Gemini {label} probe")), \
         patch("preflight_check.check_10_sqlite", return_value=result(13, "SQLite integrity + WAL checkpoint", critical=False)), \
         patch("preflight_check.check_11_pending", return_value=result(14, "pending file JSON load", critical=False)), \
         patch("preflight_check._restart_cloudflared_for_preflight", return_value=(True, fresh)) as restart, \
         patch("preflight_check._line_token", return_value="fake-token"), \
         patch("preflight_check._write_cache") as write_cache:
        exit_code = pf.run(_args(dry_run=False))

    assert exit_code == 0
    restart.assert_called_once_with(stale)
    assert alignment_calls == [stale, fresh]
    assert len(e2e_calls) == 2
    record = mock_jsonl.call_args[0][0]
    assert record["tunnel_url"] == fresh
    assert record["webhook_url"] == f"{fresh}/callback"
    assert not [
        r for r in record["results"]
        if r["critical"] and r["status"] == "fail"
    ]
    assert any(
        r["idx"] == 5 and r["status"] == "skip" and "superseded" in r["detail"]
        for r in record["results"]
    )
    assert any(
        r["idx"] == 3 and r["status"] == "skip" and "superseded" in r["detail"]
        for r in record["results"]
    )
    write_cache.assert_called_once()


# ---------- _finalize alert dispatch ----------

def _gemini_fail_pair():
    return [
        _make_result(11, "Gemini main probe", critical=True, status="fail",
                     detail="429 RESOURCE_EXHAUSTED"),
        _make_result(12, "Gemini lite probe", critical=True, status="fail",
                     detail="429 RESOURCE_EXHAUSTED"),
    ]


@patch("preflight_check._emit_jsonl")
@patch("preflight_check._send_discord_alert")
def test_all_gemini_transient_critical_skips_alert(mock_alert, mock_jsonl):
    results = _gemini_fail_pair()
    exit_code = pf._finalize(results, start=0.0, tunnel_url="", webhook_url="", args=_args())
    assert mock_alert.call_count == 0
    assert mock_jsonl.call_count == 1  # JSONL still emitted (audit trail)
    assert exit_code == 2  # Gemini-only transient degradation is deployable


@patch("preflight_check._emit_jsonl")
@patch("preflight_check._send_discord_alert")
def test_mixed_critical_alerts_with_filtered_msg(mock_alert, mock_jsonl):
    # 1 Gemini transient + 1 real LINE webhook fail → alert sent, msg shows ONLY non-transient
    results = _gemini_fail_pair() + [
        _make_result(9, "LINE webhook URL 對齊 cloudflared", critical=True,
                     status="fail", detail="PUT http 401: unauthorized"),
    ]
    exit_code = pf._finalize(results, start=0.0, tunnel_url="", webhook_url="", args=_args())
    assert mock_alert.call_count == 1
    msg = mock_alert.call_args[0][0]
    assert "LINE webhook URL" in msg
    assert "Gemini main probe" not in msg  # filtered out of msg body
    assert exit_code == 1


@patch("preflight_check._emit_jsonl")
@patch("preflight_check._send_discord_alert")
def test_info_fail_only_still_alerts(mock_alert, mock_jsonl):
    # Preserve current behavior: info_fail (non-critical fail) triggers alert
    results = [
        _make_result(13, "SQLite integrity + WAL checkpoint", critical=False,
                     status="fail", detail="integrity: 'corrupt'"),
    ]
    exit_code = pf._finalize(results, start=0.0, tunnel_url="", webhook_url="", args=_args())
    assert mock_alert.call_count == 1
    assert exit_code == 2


@patch("preflight_check._emit_jsonl")
@patch("preflight_check._send_discord_alert")
def test_autofix_only_alerts(mock_alert, mock_jsonl):
    # No fail, autofix present → still alert (audit trail)
    results = [
        _make_result(9, "LINE webhook URL 對齊 cloudflared", critical=True,
                     status="autofix", detail="drift fixed: 'a' → 'b'"),
    ]
    exit_code = pf._finalize(results, start=0.0, tunnel_url="", webhook_url="", args=_args())
    assert mock_alert.call_count == 1
    assert exit_code == 0


@patch("preflight_check._emit_jsonl")
@patch("preflight_check._send_discord_alert")
def test_all_pass_no_alert(mock_alert, mock_jsonl):
    results = [_make_result(1, "uvicorn process alive", critical=True, status="pass")]
    exit_code = pf._finalize(results, start=0.0, tunnel_url="", webhook_url="", args=_args())
    assert mock_alert.call_count == 0
    assert exit_code == 0


@patch("preflight_check._emit_jsonl")
@patch("preflight_check._send_discord_alert")
def test_single_gemini_fail_downgrades_to_info_no_alert(mock_alert, mock_jsonl):
    # One Gemini model failing while the other is assumed available is degraded,
    # not deploy-blocking; do not silently mark it as a clean PASS.
    results = [_make_result(11, "Gemini main probe", critical=True, status="fail",
                            detail="429 RESOURCE_EXHAUSTED")]
    exit_code = pf._finalize(results, start=0.0, tunnel_url="", webhook_url="", args=_args())
    assert mock_alert.call_count == 0
    assert exit_code == 2


@patch("preflight_check._emit_jsonl")
@patch("preflight_check._send_discord_alert")
def test_jsonl_always_emitted_on_transient_skip(mock_alert, mock_jsonl):
    # GP1 verification: even when alert is skipped, JSONL audit log persists
    results = _gemini_fail_pair()
    pf._finalize(results, start=0.0, tunnel_url="", webhook_url="", args=_args())
    assert mock_jsonl.call_count == 1
    record = mock_jsonl.call_args[0][0]
    # All fails recorded (not filtered out of JSONL)
    names = [r["name"] for r in record["results"]]
    assert "Gemini main probe" in names
    assert "Gemini lite probe" in names
