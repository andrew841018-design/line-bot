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
    assert 'run_preflight "cloudflared restart webhook alignment"' in text
    assert 'run_preflight "processes UP verification"' in text


def test_uvicorn_restart_defers_preflight_when_cloudflared_is_down():
    text = _script_text()

    assert "defer full-path preflight until cloudflared is ready" in text
    assert "restart_success_http_200_preflight_deferred_cf_down" in text


def test_gemini_429_count_does_not_double_zero_on_no_match():
    text = _script_text()

    assert 'grep -c "^$PT_TODAY .*429" "$UVICORN_LOG" 2>/dev/null || true' in text
    assert "COUNT_429=${COUNT_429:-0}" in text
    assert 'grep -c "^$PT_TODAY .*429" "$UVICORN_LOG" 2>/dev/null || echo 0' not in text
