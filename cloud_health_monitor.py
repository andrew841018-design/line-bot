"""Cloud LINE bot health monitor.

Designed for a Linux systemd timer running every 60 seconds. It performs cheap
checks on every run. LINE webhook E2E checks run only when --live-line is set
and no more than every LINE_WEBHOOK_TEST_INTERVAL_SEC seconds (default 180).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

import preflight_cloud

BOT_DIR = Path(__file__).resolve().parent

STATE_PATH = Path(os.environ.get("LINE_BOT_CLOUD_MONITOR_STATE", "/var/lib/line-bot/cloud_health_state.json"))
LINE_INTERVAL_SEC = int(os.environ.get("LINE_WEBHOOK_TEST_INTERVAL_SEC", "180"))
ALERT_COOLDOWN_SEC = int(os.environ.get("LINE_BOT_CLOUD_ALERT_COOLDOWN_SEC", "300"))


def _state_path() -> Path:
    return Path(os.environ.get("LINE_BOT_CLOUD_MONITOR_STATE", str(STATE_PATH)))


def _line_interval_sec() -> int:
    return int(os.environ.get("LINE_WEBHOOK_TEST_INTERVAL_SEC", str(LINE_INTERVAL_SEC)))


def _alert_cooldown_sec() -> int:
    return int(os.environ.get("LINE_BOT_CLOUD_ALERT_COOLDOWN_SEC", str(ALERT_COOLDOWN_SEC)))


def _read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[cloud-health] failed to write state: {exc}", file=sys.stderr)


def _issue_key(result: preflight_cloud.CheckResult) -> str:
    return f"{result.name}:{result.detail[:120]}"


def _due_line_check(state: dict, now: float, *, force: bool) -> bool:
    if force:
        return True
    try:
        last = float(state.get("last_line_check_ts", 0))
    except (TypeError, ValueError):
        last = 0
    return now - last >= _line_interval_sec()


def _send_discord(message: str) -> bool:
    try:
        sys.path.insert(0, str(BOT_DIR))
        from notify_discord import send_dm

        return bool(send_dm(message))
    except Exception as exc:
        print(f"[cloud-health] Discord send failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def _format_alert(results: list[preflight_cloud.CheckResult], *, live_line: bool) -> str:
    failures = [r for r in results if r.status == "fail" and r.critical]
    lines = [
        "[line_bot_cloud] UNHEALTHY",
        "LINE bot cloud monitoring found a critical failure.",
        f"LINE E2E check included: {'yes' if live_line else 'no'}",
        "Issues:",
    ]
    for result in failures[:8]:
        detail = f": {result.detail}" if result.detail else ""
        lines.append(f"- {result.name}{detail}")
    lines.append("Action: check systemctl status line-bot.service cloudflared.service and journalctl -u line-bot.service.")
    return "\n".join(lines)[:1900]


def _alerts_due(results: list[preflight_cloud.CheckResult], state: dict, now: float) -> bool:
    failures = [r for r in results if r.status == "fail" and r.critical]
    if not failures:
        return False
    sent = state.setdefault("alert_sent_ts", {})
    if not isinstance(sent, dict):
        sent = {}
        state["alert_sent_ts"] = sent
    for result in failures:
        key = _issue_key(result)
        try:
            last = float(sent.get(key, 0))
        except (TypeError, ValueError):
            last = 0
        if now - last >= _alert_cooldown_sec():
            return True
    return False


def _mark_alerts(results: list[preflight_cloud.CheckResult], state: dict, now: float) -> None:
    sent = state.setdefault("alert_sent_ts", {})
    if not isinstance(sent, dict):
        sent = {}
        state["alert_sent_ts"] = sent
    for result in results:
        if result.status == "fail" and result.critical:
            sent[_issue_key(result)] = now


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-alerts", action="store_true")
    parser.add_argument("--live-line", action="store_true")
    parser.add_argument("--force-line", action="store_true")
    parser.add_argument("--env-file", default="")
    parser.add_argument("--require-public-url", action="store_true")
    parser.add_argument("--require-unmuted", action="store_true")
    parser.add_argument("--require-discord", action="store_true")
    parser.add_argument("--require-http-jobs-disabled", dest="require_http_jobs_disabled", action="store_true")
    parser.add_argument("--require-jobs-disabled", dest="require_http_jobs_disabled", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        preflight_cloud.load_runtime_env(args.env_file or None)
    except preflight_cloud.EnvLoadError as exc:
        result = preflight_cloud.fail("runtime env file loaded", str(exc))
        if args.json:
            print(json.dumps({
                "ok": False,
                "exit_code": 1,
                "live_line": False,
                "results": [asdict(result)],
            }, ensure_ascii=False))
        else:
            preflight_cloud.print_results([result])
            print("live_line=False exit_code=1")
        return 1
    now = time.time()
    state_path = _state_path()
    state = _read_state(state_path)
    live_line = args.live_line and _due_line_check(state, now, force=args.force_line)

    preflight_args = preflight_cloud.build_arg_parser().parse_args([])
    preflight_args.live_line = live_line
    preflight_args.require_public_url = args.require_public_url
    preflight_args.require_unmuted = args.require_unmuted
    preflight_args.require_discord = args.require_discord
    preflight_args.require_http_jobs_disabled = args.require_http_jobs_disabled
    preflight_args.require_jobs_disabled = args.require_http_jobs_disabled
    preflight_args.skip_sqlite_integrity = True
    preflight_args.test_discord = False
    preflight_args.env_file = args.env_file
    preflight_cloud.apply_runtime_defaults(preflight_args)
    results = preflight_cloud.run_checks(preflight_args)
    code = preflight_cloud.exit_code(results)

    if live_line:
        state["last_line_check_ts"] = now
    state["last_run_ts"] = now
    state["last_exit_code"] = code
    state["last_results"] = [asdict(r) for r in results]

    if args.send_alerts and _alerts_due(results, state, now):
        if _send_discord(_format_alert(results, live_line=live_line)):
            _mark_alerts(results, state, now)

    _write_state(state_path, state)
    if args.json:
        print(json.dumps({
            "ok": code == 0,
            "exit_code": code,
            "live_line": live_line,
            "results": [asdict(r) for r in results],
        }, ensure_ascii=False))
    else:
        preflight_cloud.print_results(results)
        print(f"live_line={live_line} exit_code={code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
