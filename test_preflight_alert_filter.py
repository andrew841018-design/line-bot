"""preflight Discord alert filter — transient (Gemini 429 / 503) suppression.

Scope: `_is_transient_error` heuristic + `_finalize` alert dispatch behavior.
Mocks `_send_discord_alert` and `_emit_jsonl`. JSONL log must always emit
regardless of alert filter; full audit trail preserved.
"""
import argparse
from types import SimpleNamespace
from unittest.mock import patch

import preflight_check as pf


def _make_result(idx, name, *, critical, status, detail=""):
    r = pf.CheckResult(idx, name, critical=critical)
    r.status = status
    r.detail = detail
    return r


def _args(**kw):
    return argparse.Namespace(force=kw.get("force", True), dry_run=kw.get("dry_run", True))


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
    assert exit_code == 1  # transient critical still fails


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
def test_single_gemini_fail_existing_downgrade_no_alert(mock_alert, mock_jsonl):
    # Pre-existing behavior (GP1 C1 deferred): single Gemini fail removes from
    # critical_fail but doesn't promote to info_fail → exit=0 PASS, no alert.
    # This test pins the current behavior so future fix is explicit.
    results = [_make_result(11, "Gemini main probe", critical=True, status="fail",
                            detail="429 RESOURCE_EXHAUSTED")]
    exit_code = pf._finalize(results, start=0.0, tunnel_url="", webhook_url="", args=_args())
    assert mock_alert.call_count == 0
    assert exit_code == 0  # current behavior; bug deferred


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
