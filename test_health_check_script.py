import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent / "health_check.sh"


def _script_text() -> str:
    return SCRIPT.read_text()


def test_processes_up_path_runs_full_path_preflight():
    text = _script_text()

    assert "PREFLIGHT_RAN=0" in text
    assert (
        'if [ "$PREFLIGHT_RAN" -eq 0 ] && [ $UVICORN_UP -eq 1 ] && [ $CF_UP -eq 1 ]; then'
        in text
    )
    assert 'run_preflight "processes UP verification"' in text


def test_preflight_accepts_gemini_degraded_exit_code():
    text = _script_text()

    assert "preflight_is_acceptable()" in text
    assert '[ "$1" = "0" ] || [ "$1" = "2" ]' in text


def test_health_check_uses_one_shared_preflight_call_site():
    text = _script_text()

    assert text.count('preflight_check.py" --force') == 1
    assert 'run_preflight "uvicorn restart"' in text
    assert 'run_preflight "cloudflared down recovery"' in text
    assert 'run_preflight "processes UP verification"' in text


def test_uvicorn_restart_defers_preflight_when_cloudflared_is_down():
    text = _script_text()

    assert "defer full-path preflight until cloudflared is ready" in text
    assert "restart_success_http_200_preflight_deferred_cf_down" in text


def test_cloudflared_restart_requires_a_fresh_url_generation():
    text = _script_text()

    assert 'PREVIOUS_URL=""' in text
    assert 'PREVIOUS_URL=$(cat "$CLOUDFLARED_URL_FILE")' in text
    assert '[ "$CANDIDATE_URL" != "$PREVIOUS_URL" ]' in text
    assert 'CF_ACTION="cf_restart_failed_no_fresh_url"' in text


def test_cloudflared_restart_fails_fast_when_quick_tunnel_dns_is_blocked():
    text = _script_text()

    assert "quick_tunnel_api_dns_ready()" in text
    assert "if ! quick_tunnel_api_dns_ready; then" in text
    assert 'CF_ACTION="cf_restart_blocked_dns"' in text


def _run_recovery_harness(tmp_path, body: str):
    env = os.environ.copy()
    env.update(
        {
            "HEALTH_CHECK_SOURCE_ONLY": "1",
            "CLOUDFLARED_URL_FILE": str(tmp_path / "cloudflared_url.txt"),
            "HC_LOG": str(tmp_path / "health.log"),
        }
    )
    command = f'source "{SCRIPT}"\n{body}'
    return subprocess.run(
        ["/bin/bash", "-c", command],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_dns_blocked_recovery_has_no_launchctl_or_preflight_side_effect(tmp_path):
    result = _run_recovery_harness(
        tmp_path,
        """
quick_tunnel_api_dns_ready() { return 1; }
launchctl() { echo launchctl-called; }
run_preflight() { echo preflight-called; }
recover_cloudflared
printf 'action=%s\n' "$CF_ACTION"
""",
    )

    assert result.returncode == 0, result.stderr
    assert "action=cf_restart_blocked_dns" in result.stdout
    assert "launchctl-called" not in result.stdout
    assert "preflight-called" not in result.stdout


def test_uppercase_reserved_host_is_not_accepted_as_fresh_url(tmp_path):
    result = _run_recovery_harness(
        tmp_path,
        """
quick_tunnel_api_dns_ready() { return 0; }
run_preflight() {
  echo preflight-called
  printf '%s\n' 'https://API.trycloudflare.com' > "$CLOUDFLARED_URL_FILE"
  PREFLIGHT_EXIT=0
}
recover_cloudflared
printf 'action=%s\n' "$CF_ACTION"
""",
    )

    assert result.returncode == 0, result.stderr
    assert "action=cf_restart_failed_no_fresh_url" in result.stdout
    assert "preflight-called" in result.stdout


def test_fresh_registered_url_from_python_owner_is_accepted(tmp_path):
    result = _run_recovery_harness(
        tmp_path,
        """
quick_tunnel_api_dns_ready() { return 0; }
run_preflight() {
  echo preflight-called
  printf '%s\n' 'https://fresh.trycloudflare.com' > "$CLOUDFLARED_URL_FILE"
  PREFLIGHT_EXIT=0
}
recover_cloudflared
printf 'action=%s\n' "$CF_ACTION"
""",
    )

    assert result.returncode == 0, result.stderr
    assert "preflight-called" in result.stdout
    assert "action=cf_restart_success_preflight_0" in result.stdout


def test_health_delegates_cloudflared_recovery_to_single_python_owner():
    recovery = _script_text().split("recover_cloudflared()", 1)[1].split(
        'if [ "${HEALTH_CHECK_SOURCE_ONLY', 1
    )[0]

    assert 'run_preflight "cloudflared down recovery"' in recovery
    assert "launchctl kickstart" not in recovery
    assert "lockf" not in recovery


def test_gemini_429_count_does_not_double_zero_on_no_match():
    text = _script_text()

    assert 'grep -c "^$PT_TODAY .*429" "$UVICORN_LOG" 2>/dev/null || true' in text
    assert "COUNT_429=${COUNT_429:-0}" in text
    assert 'grep -c "^$PT_TODAY .*429" "$UVICORN_LOG" 2>/dev/null || echo 0' not in text
