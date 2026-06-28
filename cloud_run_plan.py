"""Generate non-mutating Cloud Run deployment commands for the LINE bot.

This helper deliberately does not execute gcloud, create cloud resources, read
local secrets, or change the LINE webhook. It prints a reviewed command plan so
the live cutover can stay explicit.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_REGION = "asia-east1"
DEFAULT_SERVICE = "line-bot"
DEFAULT_ENV_VARS = {
    "BOT_MUTED": "true",
    "SQLITE_PATH": "/tmp/line_bot.db",
    "LOCAL_LLM_PREWARM_DISABLED": "1",
    "JOBS_ROUTES_ENABLED": "0",
    "JOBS_ALLOW_PUBLIC_HTTP": "0",
    "JOBS_SUBPROCESS_INHERIT_ENV": "1",
    "LINE_BOT_JOB_STATE_DIR": "/tmp/line-bot/state",
    "LINE_BOT_JOB_LOG_DIR": "/tmp/line-bot/logs/jobs",
    "LINE_BOT_JOB_LOCK_DIR": "/tmp/line-bot/locks",
}
SECRET_ENV_KEYS = (
    "LINE_CHANNEL_SECRET",
    "LINE_CHANNEL_ACCESS_TOKEN",
    "GEMINI_API_KEY",
)
OPTIONAL_SECRET_ENV_KEYS = ("GROQ_API_KEY",)
SECRET_NAME_PREFIX = "line-bot"
RECOMMENDED_APIS = (
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudscheduler.googleapis.com",
)
JOB_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")


@dataclass(frozen=True)
class Plan:
    project: str
    region: str
    service: str
    service_url_var: str
    commands: list[str]
    notes: list[str]


def shell_join(args: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in args)


def default_env_arg(env: dict[str, str] | None = None) -> str:
    values = env or DEFAULT_ENV_VARS
    return ",".join(f"{key}={value}" for key, value in values.items())


def job_token(master_token: str, job_name: str) -> str:
    if not JOB_NAME_RE.match(job_name):
        raise ValueError(f"invalid job name: {job_name!r}")
    if not master_token:
        raise ValueError("master token is required")
    return hmac.new(master_token.encode(), job_name.encode(), hashlib.sha256).hexdigest()


def require_gcloudignore(bot_dir: Path) -> list[str]:
    path = bot_dir / ".gcloudignore"
    if not path.exists():
        return ["missing .gcloudignore; do not run source deploy yet"]
    text = path.read_text()
    required_patterns = [".env", ".venv/", "line_bot.db", "logs/", "line_token_cache.json"]
    missing = [pattern for pattern in required_patterns if pattern not in text]
    if missing:
        return [f".gcloudignore missing runtime exclusion: {pattern}" for pattern in missing]
    return []


def build_plan(
    *,
    project: str,
    region: str = DEFAULT_REGION,
    service: str = DEFAULT_SERVICE,
    bot_dir: Path | None = None,
) -> Plan:
    bot_dir = bot_dir or Path(__file__).resolve().parent
    service_url_var = (
        f"$(gcloud run services describe {shlex.quote(service)} "
        f"--region {shlex.quote(region)} --format 'value(status.url)')"
    )
    commands = [
        shell_join(["gcloud", "config", "set", "project", project]),
        shell_join(["gcloud", "services", "enable", *RECOMMENDED_APIS]),
        shell_join(
            [
                "gcloud",
                "run",
                "deploy",
                service,
                "--source",
                ".",
                "--region",
                region,
                "--allow-unauthenticated",
                "--memory",
                "1Gi",
                "--cpu",
                "1",
                "--concurrency",
                "1",
                "--min-instances",
                "0",
                "--max-instances",
                "1",
                "--timeout",
                "300",
                "--set-env-vars",
                default_env_arg(),
            ]
        ),
        f"SERVICE_URL={service_url_var}",
        'curl -fsS "$SERVICE_URL/health"',
        '.venv/bin/python preflight_cloud.py --public-base-url "$SERVICE_URL" --require-public-url',
    ]
    notes = [
        "Run from line_bot/.",
        "Deploy starts muted; set BOT_MUTED=false only after LINE webhook E2E passes.",
        "Add real secrets via Cloud Run console or Secret Manager, not this script.",
        "Create a GCP budget alert before live cutover.",
        *require_gcloudignore(bot_dir),
    ]
    return Plan(project=project, region=region, service=service, service_url_var=service_url_var, commands=commands, notes=notes)


def secret_name(env_key: str) -> str:
    return f"{SECRET_NAME_PREFIX}-{env_key.lower().replace('_', '-')}"


def secret_commands(
    *,
    service: str = DEFAULT_SERVICE,
    region: str = DEFAULT_REGION,
    include_optional: bool = False,
) -> list[str]:
    keys = SECRET_ENV_KEYS + (OPTIONAL_SECRET_ENV_KEYS if include_optional else ())
    create_cmds = [
        shell_join(
            [
                "gcloud",
                "secrets",
                "create",
                secret_name(key),
                "--replication-policy",
                "automatic",
            ]
        )
        for key in keys
    ]
    mapping = ",".join(f"{key}={secret_name(key)}:latest" for key in keys)
    update_cmd = shell_join(
        [
            "gcloud",
            "run",
            "services",
            "update",
            service,
            "--region",
            region,
            "--update-secrets",
            mapping,
        ]
    )
    return [
        "# Create only sensitive secrets first, then add secret versions through the console or:",
        "# printf %s '<SECRET_VALUE>' | gcloud secrets versions add <SECRET_NAME> --data-file=-",
        *create_cmds,
        update_cmd,
    ]


def scheduler_command(
    *,
    service_url: str,
    job_name: str,
    schedule: str,
    timezone: str,
    region: str,
    token: str,
) -> str:
    if not JOB_NAME_RE.match(job_name):
        raise ValueError(f"invalid job name: {job_name!r}")
    if not token:
        raise ValueError("per-job token is required")
    uri = f"{service_url.rstrip('/')}/jobs/{job_name}"
    uri_arg = f'"{uri}"' if uri.startswith("$") else shlex.quote(uri)
    return " ".join(
        [
            shell_join(["gcloud", "scheduler", "jobs", "create", "http", job_name]),
            shell_join(["--location", region]),
            shell_join(["--schedule", schedule]),
            shell_join(["--time-zone", timezone]),
            f"--uri {uri_arg}",
            shell_join(["--http-method", "POST"]),
            shell_join(["--headers", f"X-Job-Token={token}"]),
        ]
    )


def print_shell_plan(plan: Plan) -> None:
    print("# Cloud Run deploy command plan")
    for note in plan.notes:
        print(f"# NOTE: {note}")
    print()
    for command in plan.commands:
        print(command)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    deploy = sub.add_parser("deploy-commands", help="print non-mutating deploy commands")
    deploy.add_argument("--project", default="<PROJECT_ID>")
    deploy.add_argument("--region", default=DEFAULT_REGION)
    deploy.add_argument("--service", default=DEFAULT_SERVICE)
    deploy.add_argument("--json", action="store_true")

    secrets = sub.add_parser("secret-commands", help="print Secret Manager setup commands")
    secrets.add_argument("--region", default=DEFAULT_REGION)
    secrets.add_argument("--service", default=DEFAULT_SERVICE)
    secrets.add_argument("--include-optional", action="store_true", help="also include optional GROQ_API_KEY")

    token = sub.add_parser("job-token", help="derive a per-job HMAC token")
    token.add_argument("job_name")
    token.add_argument("--master-token", default=os.environ.get("JOBS_MASTER_TOKEN", ""))

    scheduler = sub.add_parser("scheduler-command", help="print a Cloud Scheduler command")
    scheduler.add_argument("job_name")
    scheduler.add_argument("--service-url", default="$SERVICE_URL")
    scheduler.add_argument("--schedule", default="0 7 * * *")
    scheduler.add_argument("--time-zone", default="Asia/Taipei")
    scheduler.add_argument("--region", default=DEFAULT_REGION)
    scheduler.add_argument("--master-token", default=os.environ.get("JOBS_MASTER_TOKEN", ""))
    scheduler.add_argument("--token", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.command == "deploy-commands":
            plan = build_plan(project=args.project, region=args.region, service=args.service)
            if args.json:
                print(json.dumps(asdict(plan), ensure_ascii=False, indent=2))
            else:
                print_shell_plan(plan)
            return 0
        if args.command == "job-token":
            print(job_token(args.master_token, args.job_name))
            return 0
        if args.command == "secret-commands":
            print("\n".join(secret_commands(service=args.service, region=args.region, include_optional=args.include_optional)))
            return 0
        if args.command == "scheduler-command":
            token = args.token or job_token(args.master_token, args.job_name)
            print(
                scheduler_command(
                    service_url=args.service_url,
                    job_name=args.job_name,
                    schedule=args.schedule,
                    timezone=args.time_zone,
                    region=args.region,
                    token=token,
                )
            )
            return 0
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
