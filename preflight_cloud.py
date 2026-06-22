"""Cloud deployment preflight for the LINE bot.

This is the Linux/VM counterpart to preflight_check.py. It does not know about
macOS launchd, quick-tunnel URL stashes, or cloudflared log scraping. By
default it only checks local process behavior and public health. LINE API calls
require --live-line so dry runs cannot accidentally touch the channel.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import dotenv_values, load_dotenv

BOT_DIR = Path(__file__).resolve().parent
DEFAULT_ETC_ENV = Path("/etc/line-bot/line-bot.env")

EXPLICIT_ENV_KEYS = {
    "ALLOWED_GROUP_ID",
    "ALLOWED_GROUP_IDS",
    "BOT_MUTED",
    "DISCORD_BOT_TOKEN",
    "DISCORD_USER_ID",
    "FAMILY_GROUP_ID",
    "GEMINI_API_KEY",
    "GEMINI_LIGHT_MODEL",
    "GEMINI_MODEL",
    "GROQ_API_KEY",
    "JOBS_MASTER_TOKEN",
    "JOBS_ROUTES_ENABLED",
    "LINE_BOT_CLOUD_ALERT_COOLDOWN_SEC",
    "LINE_BOT_CLOUD_MONITOR_STATE",
    "LINE_BOT_JOB_LOCK_DIR",
    "LINE_BOT_JOB_LOG_DIR",
    "LINE_BOT_JOB_STATE_DIR",
    "LINE_BOT_LOCAL_BASE_URL",
    "LINE_BOT_PUBLIC_BASE_URL",
    "LINE_CHANNEL_ACCESS_TOKEN",
    "LINE_CHANNEL_ID",
    "LINE_CHANNEL_SECRET",
    "LINE_TOKEN_CACHE_FILE",
    "LINE_WEBHOOK_TEST_INTERVAL_SEC",
    "SQLITE_PATH",
}

LINE_BOT_INFO = "https://api.line.me/v2/bot/info"
LINE_WEBHOOK_ENDPOINT = "https://api.line.me/v2/bot/channel/webhook/endpoint"
LINE_WEBHOOK_TEST = "https://api.line.me/v2/bot/channel/webhook/test"


@dataclass
class CheckResult:
    name: str
    status: str
    critical: bool
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"pass", "skip"}


class EnvLoadError(RuntimeError):
    pass


def pass_(name: str, detail: str = "", *, critical: bool = True) -> CheckResult:
    return CheckResult(name=name, status="pass", critical=critical, detail=detail)


def fail(name: str, detail: str, *, critical: bool = True) -> CheckResult:
    return CheckResult(name=name, status="fail", critical=critical, detail=detail)


def skip(name: str, detail: str, *, critical: bool = False) -> CheckResult:
    return CheckResult(name=name, status="skip", critical=critical, detail=detail)


def default_env_file() -> Path:
    configured = os.environ.get("LINE_BOT_ENV_FILE", "").strip()
    if configured:
        return Path(configured)
    if DEFAULT_ETC_ENV.exists():
        return DEFAULT_ETC_ENV
    return BOT_DIR / ".env"


def load_runtime_env(env_file: str | None = None) -> Path | None:
    explicit = bool(env_file)
    path = Path(env_file) if env_file else default_env_file()
    if explicit:
        os.environ["LINE_BOT_DISABLE_DOTENV"] = "1"
        for key in EXPLICIT_ENV_KEYS:
            os.environ.pop(key, None)
        if not path.exists():
            raise EnvLoadError(f"explicit env file not found: {path}")
    if path.exists():
        os.environ["LINE_BOT_DISABLE_DOTENV"] = "1"
        if env_file:
            for key, value in dotenv_values(path).items():
                if key and value is not None:
                    os.environ[key] = value
            return path
        load_dotenv(path, override=bool(env_file))
        return path
    if explicit:
        raise EnvLoadError(f"explicit env file not found: {path}")
    return None


def apply_runtime_defaults(args: argparse.Namespace) -> argparse.Namespace:
    if args.local_base_url is None:
        args.local_base_url = os.environ.get("LINE_BOT_LOCAL_BASE_URL", "http://127.0.0.1:8080")
    if args.public_base_url is None:
        args.public_base_url = os.environ.get("LINE_BOT_PUBLIC_BASE_URL", "")
    if args.sqlite_path is None:
        args.sqlite_path = os.environ.get("SQLITE_PATH", str(BOT_DIR / "line_bot.db"))
    return args


def normalize_base_url(raw: str | None, *, public: bool) -> str:
    if not raw:
        return ""
    candidate = str(raw).strip().rstrip("/")
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    if public and parsed.scheme != "https":
        return ""
    if not parsed.netloc or parsed.params or parsed.query or parsed.fragment:
        return ""
    if parsed.path not in {"", "/"}:
        return ""
    return candidate


def _request(method: str, url: str, *, timeout: int = 8, **kwargs) -> tuple[int, str]:
    try:
        resp = requests.request(method, url, timeout=timeout, **kwargs)
        return resp.status_code, (resp.text or "")[:500]
    except requests.RequestException as exc:
        return 0, f"{type(exc).__name__}: {str(exc)[:180]}"


def _line_token() -> str:
    try:
        sys.path.insert(0, str(BOT_DIR))
        from line_token_refresh import get_line_token

        return get_line_token() or ""
    except Exception:
        return os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")


def check_local_health(local_base_url: str) -> CheckResult:
    base = normalize_base_url(local_base_url, public=False)
    if not base:
        return fail("local base URL valid", f"invalid local URL: {local_base_url!r}")
    code, body = _request("GET", f"{base}/health", timeout=5)
    if code == 200:
        return pass_("local /health 200")
    return fail("local /health 200", f"http={code} body={body[:160]}")


def check_local_callback(local_base_url: str) -> CheckResult:
    base = normalize_base_url(local_base_url, public=False)
    if not base:
        return fail("local /callback route", f"invalid local URL: {local_base_url!r}")
    code, body = _request("POST", f"{base}/callback", timeout=5, data="{}")
    if code == 400 and "missing signature" in body.lower():
        return pass_("local /callback no-signature returns 400")
    return fail(
        "local /callback no-signature returns 400",
        f"http={code} body={body[:160]}",
    )


def check_public_health(public_base_url: str, *, required: bool) -> CheckResult:
    base = normalize_base_url(public_base_url, public=True)
    if not base:
        if required:
            return fail("public base URL valid", "LINE_BOT_PUBLIC_BASE_URL is missing or invalid")
        return skip("public /health 200", "LINE_BOT_PUBLIC_BASE_URL not set")
    code, body = _request("GET", f"{base}/health", timeout=10)
    if code == 200:
        return pass_("public /health 200")
    return fail("public /health 200", f"http={code} body={body[:160]}")


def check_sqlite(db_path: str, *, full_integrity: bool = True) -> CheckResult:
    path = Path(db_path)
    if not path.exists():
        return skip("SQLite integrity", f"DB not found: {path}", critical=False)
    if not full_integrity:
        try:
            conn = sqlite3.connect(str(path), timeout=2)
            conn.execute("SELECT 1").fetchone()
            conn.close()
        except Exception as exc:
            return fail("SQLite reachable", f"{type(exc).__name__}: {str(exc)[:160]}")
        return pass_("SQLite reachable")
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        row = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
    except Exception as exc:
        return fail("SQLite integrity", f"{type(exc).__name__}: {str(exc)[:160]}")
    if row and row[0] == "ok":
        return pass_("SQLite integrity")
    return fail("SQLite integrity", f"integrity_check={row!r}")


def check_bot_unmuted(*, required: bool) -> CheckResult:
    raw = os.environ.get("BOT_MUTED", "").strip().lower()
    muted = raw in {"1", "true", "yes", "on"}
    explicit_unmuted = raw in {"0", "false", "no", "off"}
    if required and not explicit_unmuted:
        if muted:
            return fail("BOT_MUTED=false", "BOT_MUTED is enabled; webhook receives but bot will not reply")
        return fail("BOT_MUTED=false", "BOT_MUTED must be explicitly set to false in production")
    if muted:
        return skip("BOT_MUTED=false", "BOT_MUTED enabled for staging", critical=False)
    if explicit_unmuted:
        return pass_("BOT_MUTED=false")
    return skip("BOT_MUTED=false", "unset; config default is muted", critical=False)


def check_discord_config(*, required: bool, test_delivery: bool = False) -> CheckResult:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    user_id = os.environ.get("DISCORD_USER_ID", "").strip()
    detail = "DISCORD_BOT_TOKEN and DISCORD_USER_ID are required for failure alerts"
    if not (token and user_id):
        if required or test_delivery:
            return fail("Discord alert config present", detail)
        return skip("Discord alert config present", detail, critical=False)
    if test_delivery:
        try:
            from notify_discord import send_dm

            ok = bool(send_dm("[line_bot_cloud] Discord delivery test"))
        except Exception as exc:
            return fail("Discord delivery test", f"{type(exc).__name__}: {str(exc)[:160]}")
        if ok:
            return pass_("Discord delivery test")
        return fail("Discord delivery test", "send_dm returned false")
    return pass_("Discord alert config present")


def check_jobs_routes_disabled(local_base_url: str, *, required: bool) -> CheckResult:
    raw = os.environ.get("JOBS_ROUTES_ENABLED", "0").strip().lower()
    disabled = raw in {"", "0", "false", "no", "off"}
    detail = (
        "JOBS_ROUTES_ENABLED must stay 0 on the VM until jobs_config is "
        "cloud-portable; this does not verify Mac launchd or systemd timers"
    )
    if disabled and not required:
        return pass_("HTTP jobs routes disabled", "env gate disabled; route probe not required")
    if disabled:
        base = normalize_base_url(local_base_url, public=False)
        if not base:
            return fail("HTTP jobs routes disabled", f"invalid local URL: {local_base_url!r}")
        code, body = _request("GET", f"{base}/jobs", timeout=5)
        if code == 404:
            return pass_("HTTP jobs routes disabled", "env disabled and /jobs returns 404")
        return fail(
            "HTTP jobs routes disabled",
            f"env disabled but /jobs route still responds: http={code} body={body[:160]}",
        )
    if required:
        return fail("HTTP jobs routes disabled", detail)
    return skip("HTTP jobs routes disabled", detail, critical=False)


def check_line_token() -> tuple[CheckResult, str]:
    token = _line_token()
    if not token:
        return fail("LINE token available", "no token"), ""
    code, body = _request(
        "GET",
        LINE_BOT_INFO,
        timeout=8,
        headers={"Authorization": f"Bearer {token}"},
    )
    if code == 200:
        return pass_("LINE token /v2/bot/info 200"), token
    return fail("LINE token /v2/bot/info 200", f"http={code} body={body[:160]}"), token


def check_line_webhook(public_base_url: str, token: str) -> CheckResult:
    base = normalize_base_url(public_base_url, public=True)
    if not base:
        return fail("LINE webhook endpoint aligned", "public base URL missing")
    expected = f"{base}/callback"
    code, body = _request(
        "GET",
        LINE_WEBHOOK_ENDPOINT,
        timeout=8,
        headers={"Authorization": f"Bearer {token}"},
    )
    if code != 200:
        return fail("LINE webhook endpoint aligned", f"GET http={code} body={body[:160]}")
    try:
        current = json.loads(body).get("endpoint", "")
    except json.JSONDecodeError:
        return fail("LINE webhook endpoint aligned", f"invalid JSON: {body[:160]}")
    if current == expected:
        return pass_("LINE webhook endpoint aligned")
    return fail("LINE webhook endpoint aligned", f"current={current!r} expected={expected!r}")


def check_line_e2e(token: str) -> CheckResult:
    code, body = _request(
        "POST",
        LINE_WEBHOOK_TEST,
        timeout=15,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data="",
    )
    if code != 200:
        return fail("LINE webhook test E2E", f"http={code} body={body[:160]}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return fail("LINE webhook test E2E", f"invalid JSON: {body[:160]}")
    success = payload.get("success")
    status_code = payload.get("statusCode")
    if success is True and (status_code in (None, 200, "200")):
        return pass_("LINE webhook test E2E")
    return fail(
        "LINE webhook test E2E",
        f"success={success!r} statusCode={status_code!r} reason={payload.get('reason')!r}",
    )


def run_checks(args: argparse.Namespace) -> list[CheckResult]:
    apply_runtime_defaults(args)
    require_http_jobs_disabled = getattr(
        args,
        "require_http_jobs_disabled",
        getattr(args, "require_jobs_disabled", False),
    )
    skip_sqlite_integrity = getattr(args, "skip_sqlite_integrity", False)
    results = [
        check_local_health(args.local_base_url),
        check_local_callback(args.local_base_url),
        check_public_health(args.public_base_url, required=args.require_public_url),
        check_sqlite(args.sqlite_path, full_integrity=not skip_sqlite_integrity),
        check_bot_unmuted(required=args.require_unmuted),
        check_discord_config(required=args.require_discord, test_delivery=args.test_discord),
        check_jobs_routes_disabled(args.local_base_url, required=require_http_jobs_disabled),
    ]
    if not args.live_line:
        results.append(skip("LINE live checks", "use --live-line to call LINE APIs"))
        return results

    token_result, token = check_line_token()
    results.append(token_result)
    if not token:
        return results
    results.append(check_line_webhook(args.public_base_url, token))
    results.append(check_line_e2e(token))
    return results


def exit_code(results: list[CheckResult]) -> int:
    critical_fail = any(r.status == "fail" and r.critical for r in results)
    noncritical_fail = any(r.status == "fail" and not r.critical for r in results)
    if critical_fail:
        return 1
    if noncritical_fail:
        return 2
    return 0


def print_results(results: list[CheckResult]) -> None:
    marks = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}
    for result in results:
        detail = f" - {result.detail}" if result.detail else ""
        crit = "critical" if result.critical else "info"
        print(f"[{marks[result.status]}] {result.name} ({crit}){detail}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default="")
    parser.add_argument("--local-base-url", default=None)
    parser.add_argument("--public-base-url", default=None)
    parser.add_argument("--sqlite-path", default=None)
    parser.add_argument("--require-public-url", action="store_true")
    parser.add_argument("--require-unmuted", action="store_true")
    parser.add_argument("--require-discord", action="store_true")
    parser.add_argument("--require-http-jobs-disabled", dest="require_http_jobs_disabled", action="store_true")
    parser.add_argument("--require-jobs-disabled", dest="require_http_jobs_disabled", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-sqlite-integrity", action="store_true")
    parser.add_argument("--test-discord", action="store_true")
    parser.add_argument("--live-line", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        load_runtime_env(args.env_file or None)
    except EnvLoadError as exc:
        result = fail("runtime env file loaded", str(exc))
        if args.json:
            print(json.dumps({
                "ok": False,
                "exit_code": 1,
                "elapsed_sec": 0,
                "results": [asdict(result)],
            }, ensure_ascii=False))
        else:
            print_results([result])
        return 1
    apply_runtime_defaults(args)
    start = time.time()
    results = run_checks(args)
    code = exit_code(results)
    if args.json:
        print(json.dumps({
            "ok": code == 0,
            "exit_code": code,
            "elapsed_sec": round(time.time() - start, 3),
            "results": [asdict(r) for r in results],
        }, ensure_ascii=False))
    else:
        print_results(results)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
