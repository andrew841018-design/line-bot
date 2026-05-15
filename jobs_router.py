"""HTTP-triggerable job dispatcher.

Security:
  1. IP allowlist: loopback only
  2. Origin header reject (browser CSRF defense)
  3. Path regex on {job_name}
  4. Registry lookup -> 404 if unknown
  5. Per-job HMAC token (hmac.compare_digest)
  6. Per-job 60s rate limit

Process:
  - BackgroundTasks runs _run_subprocess
  - subprocess.Popen with EXPLICIT env (no os.environ inheritance)
  - timeout -> SIGTERM 5s grace -> SIGKILL
  - Atomic state write (tmp + os.replace)
  - PID tracked; startup_sweep marks dead-PID 'running' as 'interrupted'

Paths:
  - state: line_bot/state/last_run_{job_name}.json
  - log:   line_bot/logs/jobs/{job_name}_{epoch}.log
  - lock:  line_bot/locks/lb_job_{job_name}.lock
"""
from __future__ import annotations

import fcntl
import signal
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from jobs_config import JOB_REGISTRY, JobSpec

log = logging.getLogger("jobs")
router = APIRouter(prefix="/jobs", tags=["jobs"])

_HERE = Path(__file__).resolve().parent
STATE_DIR = _HERE / "state"
LOG_DIR = _HERE / "logs" / "jobs"
LOCK_DIR = _HERE / "locks"

_JOB_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_ALLOWED_IPS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
_RATE_LIMIT_SECONDS = 60

_SECRET_PATTERNS = [
    (re.compile(r"([?&]key=)[^&\s]+"), r"\1REDACTED"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]+"), r"\1REDACTED"),
    (re.compile(r"(X-Goog-Api-Key:\s*)[^\s]+"), r"\1REDACTED"),
    (re.compile(r"(Authorization:\s*Bot\s+)[^\s]+"), r"\1REDACTED"),
    (re.compile(r"(https?://discord(?:app)?\.com/api/webhooks/\d+/)[A-Za-z0-9_\-]+"), r"\1REDACTED"),
    (re.compile(r"(postgres(?:ql)?://[^:]+:)[^@\s]+"), r"\1REDACTED"),
]


def _master_token() -> str:
    tok = os.environ.get("JOBS_MASTER_TOKEN", "")
    if not tok:
        raise HTTPException(503, "JOBS_MASTER_TOKEN not configured")
    return tok


def _per_job_token(job_name: str) -> str:
    return hmac.new(
        _master_token().encode(), job_name.encode(), hashlib.sha256
    ).hexdigest()


def _sanitize(text: str) -> str:
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    # Substring-redact master token if it ever leaked into output
    master = os.environ.get("JOBS_MASTER_TOKEN")
    if master and master in text:
        text = text.replace(master, "[MASTER_TOKEN]")
    return text


def _state_path(job_name: str) -> Path:
    return STATE_DIR / f"last_run_{job_name}.json"


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp per call to avoid clobber between concurrent writers.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{int(time.time()*1e6)}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _check_ip(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in _ALLOWED_IPS:
        raise HTTPException(403, f"IP not allowed: {host}")


def _check_origin(request: Request) -> None:
    origin = (request.headers.get("Origin") or "").strip()
    if origin:
        raise HTTPException(403, "Origin header present; reject browser-originated")


def _validate_job_name(job_name: str) -> JobSpec:
    if not _JOB_NAME_RE.match(job_name):
        raise HTTPException(400, "invalid job name format")
    spec = JOB_REGISTRY.get(job_name)
    if spec is None:
        raise HTTPException(404, f"unknown job: {job_name}")
    return spec


def _check_token(request: Request, job_name: str) -> None:
    received = request.headers.get("X-Job-Token", "")
    expected = _per_job_token(job_name)
    if not hmac.compare_digest(received, expected):
        raise HTTPException(401, "invalid token")


def _check_rate_limit(job_name: str) -> None:
    state = _state_path(job_name)
    if not state.exists():
        return
    try:
        data = json.loads(state.read_text())
    except (json.JSONDecodeError, OSError):
        return
    last_started = float(data.get("started_at_epoch") or 0)
    elapsed = time.time() - last_started
    if 0 < elapsed < _RATE_LIMIT_SECONDS:
        raise HTTPException(
            429,
            f"rate limited; last fired {int(elapsed)}s ago, cooldown {_RATE_LIMIT_SECONDS}s",
        )


def _run_subprocess(job_name: str, spec: JobSpec) -> None:
    lock_path = LOCK_DIR / f"lb_job_{job_name}.lock"
    state_p = _state_path(job_name)
    started_epoch = time.time()

    state: dict[str, Any] = {
        "ok": None,
        "status": "running",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "started_at_epoch": started_epoch,
        "pid": None,
    }

    lock_fd = None
    log_file = None
    try:
        # Defense in depth: in case startup_sweep hasn't run yet.
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock_fd = open(lock_path, "w")
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("job %s: lock held; skip", job_name)
            state["ok"] = False
            state["status"] = "skipped_locked"
            state["error"] = "another instance already running"
            return

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"{job_name}_{int(started_epoch)}.log"
        log_file = open(log_path, "w")
        state["log_path"] = str(log_path)

        proc = subprocess.Popen(
            spec.command,
            cwd=spec.cwd,
            env=spec.env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        state["pid"] = proc.pid
        _atomic_write(state_p, state)

        try:
            rc = proc.wait(timeout=spec.timeout)
        except subprocess.TimeoutExpired:
            # Kill the WHOLE session group (start_new_session=True made one).
            # Bash wrappers spawn descendants (e.g. claude); terminate only on
            # the direct child leaves orphans.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()
                proc.wait()
            state["ok"] = False
            state["status"] = "timeout"
            state["error"] = f"timeout after {spec.timeout}s"
            state["returncode"] = -1
        else:
            state["ok"] = rc == 0
            state["status"] = "completed" if rc == 0 else "failed"
            state["returncode"] = rc

        state["finished_at"] = datetime.now().isoformat(timespec="seconds")
        state["duration_s"] = int(time.time() - started_epoch)

    except Exception as e:
        log.exception("job %s raised", job_name)
        state["ok"] = False
        state["status"] = "error"
        state["error"] = repr(e)
    finally:
        # If we never acquired the lock (skipped_locked), the lock-holder owns the
        # state file; do not overwrite their 'running' record with our skip note.
        if state.get("status") != "skipped_locked":
            try:
                _atomic_write(state_p, state)
            except Exception:
                log.exception("failed to write state for %s", job_name)
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                lock_fd.close()
            except Exception:
                pass


@router.post("/{job_name}")
async def trigger_job(job_name: str, request: Request, background_tasks: BackgroundTasks):
    _check_ip(request)
    _check_origin(request)
    spec = _validate_job_name(job_name)
    _check_token(request, job_name)
    _check_rate_limit(job_name)
    background_tasks.add_task(_run_subprocess, job_name, spec)
    return JSONResponse(status_code=202, content={"accepted": True, "job": job_name})


@router.get("/{job_name}/last-run")
async def last_run(job_name: str, request: Request):
    _check_ip(request)
    _check_origin(request)
    _validate_job_name(job_name)
    _check_token(request, job_name)

    sp = _state_path(job_name)
    if not sp.exists():
        return JSONResponse(content={"status": "never_run"})
    try:
        data = json.loads(sp.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return JSONResponse(content={"status": "state_corrupt", "error": str(e)})

    for k in ("stdout_tail", "stderr_tail", "error"):
        if k in data and isinstance(data[k], str):
            data[k] = _sanitize(data[k])
    # Avoid leaking absolute home-dir path; expose basename only.
    if isinstance(data.get("log_path"), str):
        data["log_path"] = os.path.basename(data["log_path"])
    return JSONResponse(content=data)


@router.get("")
async def list_jobs(request: Request):
    _check_ip(request)
    _check_origin(request)
    return JSONResponse(
        content={
            "jobs": [
                {"name": name, "description": spec.description, "timeout": spec.timeout}
                for name, spec in sorted(JOB_REGISTRY.items())
            ]
        }
    )


def startup_sweep() -> None:
    """Ensure LOCK_DIR exists; mark dead-PID 'running' state as 'interrupted'."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_DIR.exists():
        return
    for state_file in STATE_DIR.glob("last_run_*.json"):
        try:
            data = json.loads(state_file.read_text())
            if data.get("status") != "running":
                continue
            pid = data.get("pid")
            alive = False
            if pid:
                try:
                    os.kill(int(pid), 0)
                    alive = True
                except (OSError, ValueError):
                    alive = False
            if not alive:
                data["status"] = "interrupted"
                data["ok"] = False
                data["error"] = "FastAPI restarted while job was running; PID died"
                data["finished_at"] = datetime.now().isoformat(timespec="seconds")
                _atomic_write(state_file, data)
                log.warning("swept stale running state: %s", state_file.name)
        except (json.JSONDecodeError, OSError):
            continue
