"""Local vision LLM — 圖片理解 fallback。

Lazy load Qwen2.5-VL-7B-Instruct-4bit (mlx-vlm)。
跟 gemini_client.chat 介面相容，但接受圖片 bytes / path。

回覆風格對齊 gemini_client._CORE_PROMPT 的「咪寶」人設 + 規則 0
（first-sentence-take）+ 黑名單 post-check（共用 _ECHO_OPENERS / _EMPTY_PHRASES）。

System prompt / post-check / compose_prompt 抽到 vision_common.py，
給本機 vision_llm 跟雲端 vision_cloud（Together AI）共用。
"""
from __future__ import annotations
import atexit
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime
import logging
import math
import multiprocessing
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional, TypeVar
from zoneinfo import ZoneInfo

# 規則 0 / 咪寶人設 / 黑名單 post-check 共用模組
from vision_common import (
    compose_prompt as _compose_prompt,  # re-export with old name
    post_check as _post_check,  # re-export with old name
)

logger = logging.getLogger("vision_llm")

_MODEL_NAME = "mlx-community/Qwen2.5-VL-7B-Instruct-4bit"
_FALLBACKS = [
    "mlx-community/Qwen2.5-VL-7B-Instruct-4bit",
    "mlx-community/Qwen2-VL-2B-Instruct-4bit",
]
_model = None
_processor = None
_loaded_name = None
_last_load_error_type = "VisionLoadError"
_last_load_failure_code = "local_model_unavailable"

_VISION_LOAD_FAILURE_CODE = "local_model_unavailable"
_VISION_SNAPSHOT_FAILURE_CODE = "local_snapshot_unavailable"
_VISION_DEFAULT_TIMEOUT_SEC = 40.0
_VISION_WARMUP_TIMEOUT_SEC = 300.0
# Unload the resident model after this long without a request. Andrew chose
# 4 h on 2026-10-25: the Mac was swapping and the idle child held ~6 GB, but
# family images arrive after long gaps and a busy/warming worker's image gets
# no reply, so a short limit would drop most first images after a quiet spell.
# The next image re-warms the model (~3 min from the external drive).
_VISION_IDLE_UNLOAD_DEFAULT_SEC = 4 * 3600.0
# A mistyped tiny value must not reload the model after every image.
_VISION_IDLE_UNLOAD_MIN_SEC = 300.0
# A parked child has no request waiting on it, so a slow SIGKILL reap gets a
# longer window before the supervisor fails closed than a request kill's
# cleanup_pending_timeout_sec.
_VISION_PARK_REAP_SEC = 60.0
_VISION_IDLE_UNLOAD_OFF_WORDS = frozenset(
    {"0", "off", "false", "no", "none", "disabled"}
)
_idle_unload_env_warned = False


def _idle_unload_sec_from_env() -> float:
    """VISION_IDLE_UNLOAD_SEC: unset -> 4 h; 0/off/false/no -> stay resident.

    An unparsable value also keeps the model resident (the behaviour before
    idle unload existed) and is logged once, so a mistyped rollback never
    turns the unload on.
    """
    global _idle_unload_env_warned
    raw = os.environ.get("VISION_IDLE_UNLOAD_SEC", "").strip()
    if not raw:
        return _VISION_IDLE_UNLOAD_DEFAULT_SEC
    if raw.lower() in _VISION_IDLE_UNLOAD_OFF_WORDS:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not math.isfinite(value):
        if not _idle_unload_env_warned:
            _idle_unload_env_warned = True
            logger.warning("vision idle unload off: VISION_IDLE_UNLOAD_SEC unparsable")
        return 0.0
    if value <= 0:
        return 0.0
    return max(_VISION_IDLE_UNLOAD_MIN_SEC, value)


# Load the model when the bot starts, or wait for the first image (2026-10-29).
# Each restart re-warmed ~6 GB that stayed resident for 4 h or more, so the
# overnight Gmail local judge never found memory. 23:00-08:00 Taiwan time is
# Andrew's chosen night window: "day" warms restarts from 08:00 to 22:59.
_VISION_WARM_MODES = frozenset({"always", "never", "day"})
_VISION_WARM_DEFAULT_MODE = "day"
# The window is [08:00, 23:00) Taiwan time; the end is exclusive. The 08:05
# morning restart (morning_restart.sh; LaunchAgent Hour 8 Minute 5, and the
# Mac's local time is Taipei) is the morning warm, and it depends on this 8:
# raising it, or moving that restart before 08:00, makes the morning restart
# park, and the day's first image then gets no reply.
_VISION_WARM_DAY_START_HOUR = 8
_VISION_WARM_DAY_END_HOUR = 23
_VISION_WARM_TZ = "Asia/Taipei"


def _warm_mode_from_env() -> str:
    """VISION_WARM_AT_START: unset/blank -> "day"; always, never or day.

    Anything else warms at every start (the behaviour before this setting)
    and logs a warning without the value. Read once per boot, so no
    warn-once flag.
    """
    raw = os.environ.get("VISION_WARM_AT_START", "").strip().lower()
    if not raw:
        return _VISION_WARM_DEFAULT_MODE
    if raw in _VISION_WARM_MODES:
        return raw
    logger.warning("vision warm at start always: VISION_WARM_AT_START unrecognised")
    return "always"


def _warms_at(mode: str, hour: int) -> bool:
    """Whether a start in this Taiwan-time hour loads the model now.

    always yes, never no, day only 08:00-22:59; unknown modes load.
    """
    if mode == "never":
        return False
    if mode == "day":
        return _VISION_WARM_DAY_START_HOUR <= hour < _VISION_WARM_DAY_END_HOUR
    return True


def _taipei_hour(now: datetime | None) -> int:
    """Hour in Taiwan time (no DST); a naive ``now`` is rejected."""
    # Per call: a zone failure must not break the import the spawn child shares.
    tz = ZoneInfo(_VISION_WARM_TZ)
    if now is None:
        now = datetime.now(tz)
    elif now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(tz).hour


class VisionBusyError(RuntimeError):
    """The single MLX worker is already running an inference."""


class VisionTimeoutError(TimeoutError):
    """The caller's reply budget expired before MLX returned."""


class VisionUnavailableError(RuntimeError):
    """The isolated MLX worker cannot be used safely."""

    def __init__(self, message: str, *, code: str = "worker_unavailable") -> None:
        super().__init__(message)
        self.code = code


_T = TypeVar("_T")
_VISION_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vision-mlx")
# MLX GPU streams are thread-affine. One active task and zero queued tasks keeps
# every load/generate call on the same long-lived worker and prevents stale work
# from piling up after a LINE reply-token deadline has expired.
_VISION_ADMISSION = threading.BoundedSemaphore(1)
_vision_worker_ident: int | None = None


def _invoke_on_vision_worker(fn: Callable[[], _T]) -> _T:
    global _vision_worker_ident
    _vision_worker_ident = threading.get_ident()
    return fn()


def _run_on_vision_worker(
    fn: Callable[[], _T], *, timeout_sec: float | None = None
) -> _T:
    """Run one MLX operation on its owner thread without an executor backlog."""
    if threading.get_ident() == _vision_worker_ident:
        return fn()
    if not _VISION_ADMISSION.acquire(blocking=False):
        raise VisionBusyError("vision worker is busy")
    try:
        future = _VISION_EXECUTOR.submit(_invoke_on_vision_worker, fn)
    except BaseException:
        _VISION_ADMISSION.release()
        raise
    future.add_done_callback(lambda _future: _VISION_ADMISSION.release())
    try:
        if timeout_sec is None:
            return future.result()
        return future.result(timeout=max(0.0, float(timeout_sec)))
    except FutureTimeoutError as exc:
        # A running native MLX call is not safely cancellable. The completion
        # callback releases admission only once the future is truly done.
        future.cancel()
        raise VisionTimeoutError("vision worker exceeded caller deadline") from exc


def _ensure_loaded() -> bool:
    global _model, _processor, _loaded_name
    global _last_load_error_type, _last_load_failure_code
    if _model is not None:
        return True
    aggregate_failure_code = _VISION_SNAPSHOT_FAILURE_CODE
    aggregate_error_type = "SnapshotUnavailable"
    for model_index, name in enumerate(_FALLBACKS):
        failure_code = _VISION_LOAD_FAILURE_CODE
        try:
            if _uses_injected_in_process_backend():
                # In-memory doubles have no package path or model cache. This
                # branch is unreachable in the spawned production child.
                from mlx_vlm import load

                _model, _processor = load(name)
            else:
                from huggingface_hub import snapshot_download

                failure_code = _VISION_SNAPSHOT_FAILURE_CODE
                snapshot = Path(
                    snapshot_download(repo_id=name, local_files_only=True)
                ).resolve(strict=True)
                if not snapshot.is_dir() or not snapshot.is_absolute():
                    raise FileNotFoundError("local snapshot is unavailable")
                failure_code = _VISION_LOAD_FAILURE_CODE
                from mlx_vlm import load

                _model, _processor = load(str(snapshot))
            _loaded_name = name
            logger.info("vision_model_ready model_index=%d", model_index)
            return True
        except Exception as exc:
            error_type = type(exc).__name__
            # A later uncached optional fallback must not hide proof that a
            # primary local snapshot existed but failed model initialization.
            if failure_code == _VISION_LOAD_FAILURE_CODE:
                aggregate_failure_code = failure_code
                aggregate_error_type = error_type
            elif aggregate_failure_code != _VISION_LOAD_FAILURE_CODE:
                aggregate_failure_code = failure_code
                aggregate_error_type = error_type
            logger.warning(
                "vision_model_load_failed code=%s model_index=%d error_type=%s",
                failure_code,
                model_index,
                error_type,
            )
    _last_load_failure_code = aggregate_failure_code
    _last_load_error_type = aggregate_error_type
    return False


def _describe_image_on_worker(
    image_path: str | Path | bytes,
    prompt: Optional[str] = None,
    max_tokens: int = 600,
) -> Optional[str]:
    """單張圖片 → 描述。失敗回 None。

    image_path 必須是檔案路徑；bytes 由 parent 在呼叫前安全地暫存。
    prompt 為 None 時使用預設實質回應要求 + 咪寶人設，無可補充內容則不回覆。
    回傳前會跑 _post_check 對齊規則 0。
    """
    try:
        if not _ensure_loaded():
            return None
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        if isinstance(image_path, (bytes, bytearray)):
            raise TypeError("byte image must be staged by the parent")

        composed = _compose_prompt(prompt or "")
        formatted = apply_chat_template(
            _processor, _model.config, composed, num_images=1
        )
        response = generate(
            _model, _processor,
            image=str(image_path), prompt=formatted,
            max_tokens=max_tokens, verbose=False,
        )
        # mlx-vlm >= 0.5 returns a GenerationResult with .text; older returns str
        text = getattr(response, "text", response)
        if not text:
            return None
        return _post_check(text.strip())
    except Exception as exc:
        logger.warning(
            "vision_inference_failed code=describe_failed error_type=%s",
            type(exc).__name__,
        )
        return None


def describe_image(
    image_path: str | Path | bytes,
    prompt: Optional[str] = None,
    max_tokens: int = 600,
    *,
    timeout_sec: float | None = None,
) -> Optional[str]:
    """Single-image inference owned by the isolated, zero-queue MLX child."""
    if _uses_injected_in_process_backend():
        return _run_injected_operation(
            "describe",
            {"image": image_path},
            lambda prepared: _describe_image_on_worker(
                prepared["image"], prompt, max_tokens
            ),
            timeout_sec=timeout_sec,
        )
    return _PROCESS_SUPERVISOR.run(
        "describe",
        {
            "image": image_path,
            "prompt": prompt,
            "max_tokens": max_tokens,
        },
        timeout_sec=timeout_sec,
    )


def _chat_with_images_on_worker(
    user_text: str,
    image_paths: list,
    max_tokens: int = 600,
) -> Optional[str]:
    """多圖 + 文字。回 LLM 回應。回傳前會跑 _post_check。"""
    if not _ensure_loaded():
        return None
    if not image_paths:
        return None
    try:
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        composed = _compose_prompt(user_text)
        prompt = apply_chat_template(
            _processor, _model.config, composed, num_images=len(image_paths)
        )
        response = generate(
            _model, _processor,
            image=[str(p) for p in image_paths],
            prompt=prompt, max_tokens=max_tokens, verbose=False,
        )
        text = getattr(response, "text", response)
        if not text:
            return None
        return _post_check(text.strip())
    except Exception as exc:
        logger.warning(
            "vision_inference_failed code=chat_failed error_type=%s",
            type(exc).__name__,
        )
        return None


def _vision_child_main(connection, generation: int) -> None:
    """Own MLX in a spawn child; never perform LINE, DB, Discord, or cloud I/O."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        if not _ensure_loaded():
            connection.send(
                (
                    "load_failed",
                    generation,
                    _last_load_failure_code,
                    _last_load_error_type,
                )
            )
            return
        connection.send(("ready", generation, _loaded_name or _MODEL_NAME))
        while True:
            message = connection.recv()
            if not isinstance(message, tuple) or not message:
                continue
            if message[0] == "stop":
                return
            if len(message) != 5 or message[0] != "request":
                continue
            _, request_generation, request_id, operation, payload = message
            if request_generation != generation:
                continue
            try:
                if operation == "describe":
                    result = _describe_image_on_worker(
                        payload["image"],
                        payload.get("prompt"),
                        payload.get("max_tokens", 600),
                    )
                elif operation == "chat":
                    result = _chat_with_images_on_worker(
                        payload.get("user_text", ""),
                        payload.get("image_paths", []),
                        payload.get("max_tokens", 600),
                    )
                else:
                    raise ValueError(f"unsupported vision operation: {operation}")
                connection.send(
                    ("result", generation, request_id, result)
                )
            except BaseException as exc:
                connection.send(
                    (
                        "error",
                        generation,
                        request_id,
                        "inference_failed",
                        type(exc).__name__,
                    )
                )
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        try:
            connection.close()
        except Exception:
            pass


class _PrivateIpcStore:
    """Parent-owned, private files used only to pass image bytes to spawn."""

    _NAME_RE = re.compile(
        r"^vision-ipc-p(?P<pid>[1-9][0-9]*)-r[1-9][0-9]*-[0-9a-f]{16}\.bin$"
    )

    def __init__(self, *, root: str | Path | None = None) -> None:
        default_root = Path(tempfile.gettempdir()) / f"line-bot-vision-ipc-{os.getuid()}"
        self.root = Path(root or default_root).absolute()

    def ensure_private_root(self) -> None:
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError:
            pass
        metadata = self.root.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise VisionUnavailableError("vision IPC root failed ownership validation")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            os.chmod(self.root, 0o700, follow_symlinks=False)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True

    @staticmethod
    def _safe_metadata(metadata) -> bool:
        return bool(
            stat.S_ISREG(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) == 0o600
        )

    def _directory_fd(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_CLOEXEC
        flags |= getattr(os, "O_NOFOLLOW", 0)
        return os.open(self.root, flags)

    def _safe_unlink_name(self, name: str) -> bool:
        directory_fd = self._directory_fd()
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not self._safe_metadata(metadata):
                return False
            os.unlink(name, dir_fd=directory_fd)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False
        finally:
            os.close(directory_fd)

    def sweep_orphans(self) -> None:
        self.ensure_private_root()
        try:
            entries = list(os.scandir(self.root))
        except OSError as exc:
            raise VisionUnavailableError(
                f"vision IPC sweep failed: {type(exc).__name__}"
            ) from exc
        for entry in entries:
            match = self._NAME_RE.fullmatch(entry.name)
            if not match:
                continue
            source_pid = int(match.group("pid"))
            if self._pid_alive(source_pid):
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if self._safe_metadata(metadata):
                if not self._safe_unlink_name(entry.name):
                    raise VisionUnavailableError("vision IPC orphan cleanup failed")

    def stage(self, data: bytes | bytearray, request_id: int) -> Path:
        self.ensure_private_root()
        name = (
            f"vision-ipc-p{os.getpid()}-r{request_id}-"
            f"{secrets.token_hex(8)}.bin"
        )
        directory_fd = self._directory_fd()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
            os.fchmod(descriptor, 0o600)
            view = memoryview(bytes(data))
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short vision IPC write")
                view = view[written:]
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            if not self._safe_metadata(metadata):
                raise VisionUnavailableError("vision IPC file failed validation")
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
                descriptor = -1
            if not self._safe_unlink_name(name):
                raise VisionUnavailableError(
                    "vision IPC staging cleanup failed"
                ) from None
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(directory_fd)
        return self.root / name

    def prepare_payload(
        self, operation: str, payload: dict, request_id: int
    ) -> tuple[dict, list[Path]]:
        prepared = dict(payload)
        staged: list[Path] = []
        try:
            if "image" in prepared:
                image = prepared.get("image")
                if isinstance(image, (bytes, bytearray)):
                    path = self.stage(image, request_id)
                    staged.append(path)
                    prepared["image"] = str(path)
                elif isinstance(image, Path):
                    prepared["image"] = str(image)
            if operation == "chat":
                image_paths = []
                for image in prepared.get("image_paths", []):
                    if isinstance(image, (bytes, bytearray)):
                        path = self.stage(image, request_id)
                        staged.append(path)
                        image_paths.append(str(path))
                    else:
                        image_paths.append(str(image))
                prepared["image_paths"] = image_paths
            return prepared, staged
        except BaseException:
            self.cleanup(staged)
            raise

    def cleanup(self, paths: list[Path]) -> None:
        for path in paths:
            candidate = Path(path)
            if candidate.parent == self.root and self._NAME_RE.fullmatch(candidate.name):
                if not self._safe_unlink_name(candidate.name):
                    raise VisionUnavailableError("vision IPC cleanup failed")


class _VisionProcessSupervisor:
    """One eager-loading spawn child, one active request, and no request queue."""

    def __init__(
        self,
        *,
        context=None,
        child_target=_vision_child_main,
        ipc_store: _PrivateIpcStore | None = None,
        default_timeout_sec: float = _VISION_DEFAULT_TIMEOUT_SEC,
        terminate_timeout_sec: float = 0.2,
        kill_timeout_sec: float = 0.2,
        cleanup_pending_timeout_sec: float = 10.0,
        warmup_timeout_sec: float = _VISION_WARMUP_TIMEOUT_SEC,
        poll_slice_sec: float = 0.05,
        idle_unload_sec: float | None = None,
    ) -> None:
        self._context = context or multiprocessing.get_context("spawn")
        self._child_target = child_target
        self._ipc_store = ipc_store or _PrivateIpcStore()
        self._default_timeout_sec = max(0.0, float(default_timeout_sec))
        self._terminate_timeout_sec = max(0.0, terminate_timeout_sec)
        self._kill_timeout_sec = max(0.0, kill_timeout_sec)
        self._cleanup_pending_timeout_sec = max(
            0.0, float(cleanup_pending_timeout_sec)
        )
        self._warmup_timeout_sec = max(0.0, float(warmup_timeout_sec))
        if self._terminate_timeout_sec + self._kill_timeout_sec > 0.5:
            raise ValueError("vision process cleanup budget exceeds 0.5 seconds")
        self._poll_slice_sec = max(0.001, poll_slice_sec)
        self._lock = threading.Lock()
        self._process = None
        self._connection = None
        self._generation = 0
        self._next_request_id = 0
        self._active_request: tuple[int, int] | None = None
        self._state = "stopped"
        self._failure_reason = ""
        self._failure_code = ""
        self._cleanup_deadline = 0.0
        self._warmup_deadline = 0.0
        self._cleanup_failure_code = ""
        self._failure_after_cleanup = ""
        self._failure_code_after_cleanup = ""
        self._ipc_swept = False
        self._shutdown_started = False
        # None: read VISION_IDLE_UNLOAD_SEC at check time (this supervisor is
        # built at import, possibly before .env is loaded; .env itself is
        # loaded once, so changing the value still needs a restart).
        self._idle_unload_sec = idle_unload_sec
        self._last_used: float | None = None
        self._parked = False

    def _close_connection_locked(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    @staticmethod
    def _safe_label(value, fallback: str) -> str:
        label = str(value) if value is not None else ""
        return label if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", label) else fallback

    def _close_process_handle_locked(self, process) -> bool:
        try:
            process.close()
        except BaseException as exc:
            self._failure_reason = (
                "vision worker cleanup failed: handle_close_"
                f"{type(exc).__name__}"
            )
            self._failure_code = "cleanup_failed"
            self._state = "failed"
            return False
        return True

    def _refresh_cleanup_pending_locked(self) -> bool:
        """Non-blockingly reap a child that exited after the fast kill budget.

        A native MLX child can take slightly longer than the request cleanup
        budget to become waitable.  Keep its exact ``Process`` handle fenced so
        no replacement can overlap it, then let the lifespan maintenance tick
        perform the eventual waitpid/close without blocking the webhook.
        """
        process = self._process
        if process is None or self._cleanup_deadline <= 0:
            return False
        try:
            process.join(0)
            alive = process.is_alive()
        except BaseException as exc:
            self._state = "failed"
            self._failure_reason = (
                "vision worker cleanup failed: late_reap_"
                f"{type(exc).__name__}"
            )
            self._failure_code = "cleanup_failed"
            return False
        if alive:
            if (
                self._state == "cleanup_pending"
                and time.monotonic() >= self._cleanup_deadline
            ):
                self._state = "failed"
                self._failure_reason = (
                    "vision worker cleanup failed: "
                    f"{self._cleanup_failure_code or 'cleanup_timeout'}"
                )
                self._failure_code = "cleanup_failed"
                logger.warning("vision_worker_cleanup_stuck")
            return False

        self._close_connection_locked()
        if not self._close_process_handle_locked(process):
            return False
        self._process = None
        # A cleanup timeout describes the old child, not a permanent model
        # failure.  Once that exact handle is waitpid-reaped and closed it is
        # safe to recover.  Only a pre-existing load/IPC failure remains sticky.
        saved_failure = self._failure_after_cleanup
        saved_failure_code = self._failure_code_after_cleanup
        self._cleanup_deadline = 0.0
        self._cleanup_failure_code = ""
        self._failure_after_cleanup = ""
        self._failure_code_after_cleanup = ""
        self._failure_reason = saved_failure
        self._failure_code = saved_failure_code
        self._state = "failed" if saved_failure else "stopped"
        logger.warning(
            "vision_worker_late_reaped recoverable=%s",
            not bool(saved_failure),
        )
        return not saved_failure

    def _start_locked(self) -> None:
        if self._shutdown_started:
            raise VisionUnavailableError(
                "vision worker is shut down", code="worker_shutdown"
            )
        if self._cleanup_deadline > 0:
            self._refresh_cleanup_pending_locked()
        if self._state == "cleanup_pending":
            raise VisionUnavailableError(
                "vision worker cleanup pending", code="cleanup_pending"
            )
        if self._failure_reason:
            raise VisionUnavailableError(
                self._failure_reason,
                code=self._failure_code or "worker_unavailable",
            )
        # A warming child can publish a terminal load failure immediately
        # before exiting. Drain that message before treating a dead process as
        # replaceable, otherwise the sticky fail-closed reason is lost.
        if (
            self._process is not None
            and self._state == "warming"
            and self._connection is not None
        ):
            self._refresh_warming_locked()
            if self._failure_reason:
                raise VisionUnavailableError(
                    self._failure_reason,
                    code=self._failure_code or "worker_unavailable",
                )
        if self._process is not None and self._process.is_alive():
            return
        if self._process is not None:
            if not self._close_process_handle_locked(self._process):
                raise VisionUnavailableError(
                    self._failure_reason,
                    code=self._failure_code or "cleanup_failed",
                )
        self._close_connection_locked()
        self._process = None
        self._active_request = None
        self._sweep_ipc_once_locked()
        self._generation += 1
        generation = self._generation
        parent_connection, child_connection = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=self._child_target,
            args=(child_connection, generation),
            daemon=True,
            name="vision-mlx-process",
        )
        try:
            process.start()
        except BaseException:
            try:
                parent_connection.close()
            finally:
                child_connection.close()
            raise
        child_connection.close()
        self._process = process
        self._connection = parent_connection
        self._state = "warming"
        self._parked = False
        # The idle clock starts when this child is ready; if that stamp were
        # ever lost, the model stays resident instead of re-parking at once.
        self._last_used = None
        self._warmup_deadline = time.monotonic() + self._warmup_timeout_sec

    def _refresh_warming_locked(
        self, *, terminate_failed_child: bool = True
    ) -> None:
        if self._state != "warming" or self._connection is None:
            return
        # Sample liveness before draining: a child that sends load_failed and
        # exits just after the drain keeps that message for the next call
        # instead of being reported as a bare death.
        exited = self._process is not None and not self._process.is_alive()
        try:
            has_message = self._connection.poll(0)
        except (EOFError, OSError):
            has_message = False
        while has_message:
            try:
                message = self._connection.recv()
            except (EOFError, OSError):
                break
            if not isinstance(message, tuple) or len(message) < 2:
                pass
            else:
                kind, generation = message[:2]
                if generation == self._generation:
                    if kind == "ready":
                        self._state = "ready"
                        self._warmup_deadline = 0.0
                        self._last_used = time.monotonic()
                        return
                    if kind == "load_failed":
                        code = self._safe_label(
                            message[2] if len(message) > 2 else None,
                            _VISION_LOAD_FAILURE_CODE,
                        )
                        error_type = self._safe_label(
                            message[3] if len(message) > 3 else None,
                            "VisionLoadError",
                        )
                        self._failure_reason = (
                            "vision worker load failed: "
                            f"code={code} error_type={error_type}"
                        )
                        self._failure_code = code
                        self._state = "failed"
                        self._warmup_deadline = 0.0
                        if terminate_failed_child:
                            self._terminate_locked(
                                "load failure", preserve_failure_reason=True
                            )
                        else:
                            # The child sends load_failed and then returns.  A
                            # lifespan tick must remain nonblocking, so let the
                            # following tick reap that natural exit instead of
                            # synchronously spending the TERM/KILL budget.
                            self._close_connection_locked()
                        return
            try:
                has_message = self._connection.poll(0)
            except (EOFError, OSError):
                break
        if exited:
            self._state = "failed"
            self._failure_reason = "vision worker exited before ready"
            # A load that dies (killed for memory, Metal abort) is a model
            # init failure: this code makes media_pipeline send its one
            # deduped local-LLM alert instead of failing silently.
            self._failure_code = _VISION_LOAD_FAILURE_CODE
            logger.warning(
                "vision_worker_exited_before_ready exitcode=%s",
                getattr(self._process, "exitcode", None),
            )
            self._warmup_deadline = 0.0
            self._close_connection_locked()
            self._close_process_handle_locked(self._process)
            self._process = None

    def _refresh_failed_process_locked(self) -> None:
        """Reap a naturally exited failed child without clearing its reason."""
        process = self._process
        if process is None or self._state != "failed":
            return
        try:
            process.join(0)
            if process.is_alive():
                return
        except BaseException:
            return
        self._close_connection_locked()
        if not self._close_process_handle_locked(process):
            return
        self._process = None
        self._active_request = None
        logger.warning("vision_worker_failed_child_reaped")

    def start_background(self) -> bool:
        """Start eager model loading; callers never wait on the safe warm-up."""
        with self._lock:
            self._start_locked()
            self._refresh_warming_locked()
            return self._state == "ready"

    def _sweep_ipc_once_locked(self) -> None:
        """Delete a dead parent's staged image files, once per process."""
        if not self._ipc_swept:
            self._ipc_store.sweep_orphans()
            self._ipc_swept = True

    def defer_start(self) -> str:
        """Start parked: no child until the first run(); maintenance never spawns.

        Only a supervisor that has done nothing yet is deferred; otherwise
        nothing changes and the current state is returned. The dead-parent
        IPC sweep still runs now; if it fails, the first spawn retries it.
        """
        with self._lock:
            # Every spawn attempt and every recycle bumps _generation, so 0
            # means no spawn attempt yet.
            if self._generation or self._shutdown_started:
                return self._state
            self._parked = True
            try:
                self._sweep_ipc_once_locked()
            except Exception as exc:
                logger.warning(
                    "vision_worker_ipc_sweep_failed error_type=%s",
                    type(exc).__name__,
                )
            return "parked"

    def _terminate_locked(
        self,
        reason: str,
        *,
        preserve_failure_reason: bool = False,
        routine: bool = False,
    ) -> bool:
        process = self._process
        saved_failure = self._failure_reason if preserve_failure_reason else ""
        saved_failure_code = self._failure_code if preserve_failure_reason else ""
        self._warmup_deadline = 0.0
        # Invalidate before signalling so a racing old result is never accepted.
        self._generation += 1
        self._active_request = None
        if process is None:
            self._close_connection_locked()
            self._failure_reason = saved_failure
            self._failure_code = saved_failure_code
            self._state = "failed" if saved_failure else "stopped"
            return True
        cleanup_error = ""
        try:
            if process.is_alive():
                process.terminate()
                process.join(self._terminate_timeout_sec)
        except BaseException as exc:
            cleanup_error = f"terminate_{type(exc).__name__}"
        try:
            if process.is_alive():
                process.kill()
                process.join(self._kill_timeout_sec)
            if process.is_alive():
                cleanup_error = "process_survived_terminate_and_kill"
            else:
                cleanup_error = ""
        except BaseException as exc:
            cleanup_error = f"kill_{type(exc).__name__}"
        self._close_connection_locked()
        if not cleanup_error and not self._close_process_handle_locked(process):
            cleanup_error = "handle_close_failed"
        if cleanup_error:
            # SIGKILL may have been delivered even though the process did not
            # become waitable inside the sub-second request budget.  Preserve
            # the exact handle and fail closed until a later nonblocking tick
            # can reap it; never spawn an overlapping Metal child.
            self._close_connection_locked()
            self._state = "cleanup_pending"
            self._failure_reason = "vision worker cleanup pending"
            self._failure_code = "cleanup_pending"
            self._failure_after_cleanup = saved_failure
            self._failure_code_after_cleanup = saved_failure_code
            self._cleanup_failure_code = cleanup_error
            self._cleanup_deadline = (
                time.monotonic() + self._cleanup_pending_timeout_sec
            )
            logger.warning("vision_worker_cleanup_pending")
            return False
        self._process = None
        self._failure_reason = saved_failure
        self._failure_code = saved_failure_code
        self._state = "failed" if saved_failure else "stopped"
        logger.log(
            logging.INFO if routine else logging.WARNING,
            "vision worker recycled after %s",
            reason,
        )
        return True

    def _idle_unload_due_locked(self) -> bool:
        limit = self._idle_unload_sec
        if limit is None:
            limit = _idle_unload_sec_from_env()
        if limit <= 0 or self._last_used is None:
            return False
        return time.monotonic() - self._last_used >= limit

    # TODO(2026-10-25 review, deferred): a re-warm that fails after a park
    # (snapshot missing, warm-up timeout, child dying before ready) stays
    # failed until the bot restarts, like every warm-up today; parking adds
    # about one unattended reload per quiet spell. A cooldown that returns
    # such a failure to "parked" would bound the outage (needs a failed-at
    # field and an edit of the pinned sticky-reload test).
    def _park_locked(self) -> None:
        """Stop an idle ready child to free its memory; the next run() re-warms.

        Same bounded TERM/KILL as any recycle (the child keeps no state worth
        a graceful exit). While parked, maintenance never respawns it.
        """
        idle_sec = int(time.monotonic() - (self._last_used or 0.0))
        self._parked = True
        if not self._terminate_locked("idle unload", routine=True):
            self._cleanup_deadline = time.monotonic() + max(
                self._cleanup_pending_timeout_sec, _VISION_PARK_REAP_SEC
            )
        logger.info(
            "vision_worker_idle_unloaded idle_sec=%d state=%s", idle_sec, self._state
        )

    def maintenance_tick(self) -> str:
        """Advance lifecycle state; the lifespan runs this off its event loop.

        Returns the state, or "parked" while no child exists until the next
        request: after an idle unload, or a start deferred at boot.
        """
        with self._lock:
            if self._shutdown_started:
                if self._cleanup_deadline > 0:
                    self._refresh_cleanup_pending_locked()
                elif self._state == "failed":
                    self._refresh_failed_process_locked()
                return self._state
            if self._cleanup_deadline > 0:
                self._refresh_cleanup_pending_locked()
            if self._state == "cleanup_pending":
                return self._state
            if self._failure_reason:
                self._refresh_failed_process_locked()
                return self._state
            if self._state == "warming":
                self._refresh_warming_locked(terminate_failed_child=False)
                if (
                    self._state == "warming"
                    and self._warmup_deadline > 0
                    and time.monotonic() >= self._warmup_deadline
                ):
                    self._failure_reason = "vision worker warmup timed out"
                    self._failure_code = "warmup_timeout"
                    self._state = "failed"
                    self._terminate_locked(
                        "warmup timeout", preserve_failure_reason=True
                    )
                return self._state
            if self._state == "ready":
                process = self._process
                if process is not None and process.is_alive():
                    if self._idle_unload_due_locked():
                        self._park_locked()
                        if self._state == "stopped":
                            return "parked"
                    return self._state
                if process is not None:
                    process.join(0)
                    self._close_connection_locked()
                    if not self._close_process_handle_locked(process):
                        return self._state
                self._process = None
                self._active_request = None
                self._state = "stopped"
                logger.warning("vision_worker_idle_exit_reaped")
            if self._state == "stopped":
                if self._parked:
                    return "parked"
                self._start_locked()
                self._refresh_warming_locked(terminate_failed_child=False)
            return self._state

    def abort_active_request(self) -> bool:
        """Idempotently terminate only an active inference, never a safe loader."""
        with self._lock:
            if self._state != "busy" or self._active_request is None:
                return False
            return self._terminate_locked("external deadline")

    def run(self, operation: str, payload: dict, *, timeout_sec: float | None):
        timeout = self._default_timeout_sec if timeout_sec is None else timeout_sec
        deadline = time.monotonic() + max(0.0, float(timeout))
        staged_paths: list[Path] = []
        try:
            with self._lock:
                # An image is demand: even when this call cannot be served
                # (a parked child still being reaped), the next tick re-warms.
                if self._parked:
                    self._parked = False
                    logger.info("vision_worker_rewarm_on_demand")
                self._start_locked()
                self._refresh_warming_locked()
                if self._failure_reason:
                    raise VisionUnavailableError(
                        self._failure_reason,
                        code=self._failure_code or "worker_unavailable",
                    )
                if self._state == "warming":
                    raise VisionBusyError("vision worker is warming up")
                if self._state == "busy":
                    raise VisionBusyError("vision worker is busy")
                if self._state != "ready" or self._connection is None:
                    raise VisionUnavailableError("vision worker is not ready")
                self._next_request_id += 1
                request_id = self._next_request_id
                prepared_payload, staged_paths = self._ipc_store.prepare_payload(
                    operation, payload, request_id
                )
                if time.monotonic() >= deadline:
                    raise VisionTimeoutError(
                        "vision worker exceeded caller deadline"
                    )
                generation = self._generation
                connection = self._connection
                self._active_request = (generation, request_id)
                self._state = "busy"
                try:
                    connection.send(
                        (
                            "request",
                            generation,
                            request_id,
                            operation,
                            prepared_payload,
                        )
                    )
                except BaseException as exc:
                    transient_reason = (
                        "vision worker request send failed: "
                        f"{type(exc).__name__}"
                    )
                    cleanup_ok = self._terminate_locked("request send failure")
                    if cleanup_ok:
                        raise VisionUnavailableError(transient_reason) from exc
                    raise VisionUnavailableError(
                        self._failure_reason,
                        code=self._failure_code or "cleanup_pending",
                    ) from exc

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    with self._lock:
                        if self._active_request == (generation, request_id):
                            self._terminate_locked("request timeout")
                    raise VisionTimeoutError("vision worker exceeded caller deadline")
                wait_sec = min(self._poll_slice_sec, remaining)
                try:
                    has_message = connection.poll(wait_sec)
                except (EOFError, OSError) as exc:
                    with self._lock:
                        self._terminate_locked("connection poll failure")
                    raise VisionUnavailableError(
                        "vision worker connection failed"
                    ) from exc
                if not has_message:
                    with self._lock:
                        process_alive = bool(
                            self._process is not None
                            and self._process.is_alive()
                        )
                    if not process_alive:
                        with self._lock:
                            self._terminate_locked("unexpected worker exit")
                        raise VisionUnavailableError(
                            "vision worker exited during request"
                        )
                    continue
                try:
                    message = connection.recv()
                except (EOFError, OSError) as exc:
                    with self._lock:
                        self._terminate_locked("connection receive failure")
                    raise VisionUnavailableError(
                        "vision worker connection failed"
                    ) from exc
                if not isinstance(message, tuple) or len(message) < 4:
                    continue
                kind, response_generation, response_request_id, value = message[:4]
                if (
                    response_generation != generation
                    or response_request_id != request_id
                ):
                    continue
                with self._lock:
                    if self._active_request != (generation, request_id):
                        continue
                    self._active_request = None
                    self._state = "ready"
                    self._last_used = time.monotonic()
                if kind == "result":
                    return value
                if kind == "error":
                    code = self._safe_label(value, "inference_failed")
                    error_type = self._safe_label(
                        message[4] if len(message) > 4 else None,
                        "VisionInferenceError",
                    )
                    raise VisionUnavailableError(
                        "vision worker request failed: "
                        f"code={code} error_type={error_type}"
                    )
        finally:
            try:
                self._ipc_store.cleanup(staged_paths)
            except BaseException as exc:
                with self._lock:
                    self._failure_reason = (
                        "vision IPC cleanup failed: "
                        f"{type(exc).__name__}"
                    )
                    self._failure_code = "ipc_cleanup_failed"
                    self._terminate_locked(
                        "IPC cleanup failure", preserve_failure_reason=True
                    )
                raise VisionUnavailableError(
                    self._failure_reason,
                    code=self._failure_code or "ipc_cleanup_failed",
                ) from exc

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown_started = True
            if self._process is None:
                self._close_connection_locked()
                self._state = "stopped"
                return
            if self._state == "ready" and self._connection is not None:
                try:
                    self._connection.send(("stop", self._generation))
                    self._process.join(self._terminate_timeout_sec)
                except Exception:
                    pass
            if self._process is not None and self._process.is_alive():
                self._terminate_locked("parent shutdown")
            else:
                self._close_connection_locked()
                if not self._close_process_handle_locked(self._process):
                    return
                self._process = None
                self._state = "stopped"


_PROCESS_SUPERVISOR = _VisionProcessSupervisor()


def start_background_worker() -> bool:
    """Start the local-only child load without waiting for model readiness."""
    return _PROCESS_SUPERVISOR.start_background()


def start_background_worker_at_boot(now: datetime | None = None) -> str:
    """Warm the model at start-up, or start parked (VISION_WARM_AT_START).

    Returns "ready", "warming" or "parked", or its current state: a supervisor
    that has already done anything keeps that state. A parked start loads the
    model on the first image, as after an idle unload. Images that arrive
    before the model is ready (about 3 min) get no reply; they do not wait.
    """
    mode = _warm_mode_from_env()
    hour = _taipei_hour(now)
    if _warms_at(mode, hour):
        return "ready" if start_background_worker() else "warming"
    state = _PROCESS_SUPERVISOR.defer_start()
    if state == "parked":
        logger.info("vision_worker_start_deferred mode=%s hour=%d", mode, hour)
    return state


def abort_active_request() -> bool:
    """Abort a timed-out native request without touching a safe cold start."""
    return _PROCESS_SUPERVISOR.abort_active_request()


def shutdown_background_worker() -> None:
    """Idempotently release the local vision child and its IPC handles."""
    _PROCESS_SUPERVISOR.shutdown()


def maintenance_tick() -> str:
    """Nonblocking lifecycle tick owned by the FastAPI lifespan task."""
    return _PROCESS_SUPERVISOR.maintenance_tick()


def _uses_injected_in_process_backend() -> bool:
    """Keep in-memory MLX test/plugin doubles usable; production uses spawn."""
    module = sys.modules.get("mlx_vlm")
    return bool(
        module is not None
        and getattr(module, "__spec__", None) is None
        and not getattr(module, "__file__", None)
    )


def _run_injected_operation(
    operation: str,
    payload: dict,
    fn: Callable[[dict], _T],
    *,
    timeout_sec: float | None,
) -> _T:
    """Keep legacy in-memory doubles safe without weakening production spawn."""
    request_id = max(1, time.monotonic_ns())
    prepared, staged = _PROCESS_SUPERVISOR._ipc_store.prepare_payload(
        operation, payload, request_id
    )
    timeout = _VISION_DEFAULT_TIMEOUT_SEC if timeout_sec is None else timeout_sec
    try:
        return _run_on_vision_worker(
            lambda: fn(prepared), timeout_sec=max(0.0, float(timeout))
        )
    finally:
        _PROCESS_SUPERVISOR._ipc_store.cleanup(staged)


def chat_with_images(
    user_text: str,
    image_paths: list,
    max_tokens: int = 600,
    *,
    timeout_sec: float | None = None,
) -> Optional[str]:
    """Multi-image inference owned by the isolated, zero-queue MLX child."""
    if _uses_injected_in_process_backend():
        return _run_injected_operation(
            "chat",
            {"image_paths": image_paths},
            lambda prepared: _chat_with_images_on_worker(
                user_text, prepared["image_paths"], max_tokens
            ),
            timeout_sec=timeout_sec,
        )
    return _PROCESS_SUPERVISOR.run(
        "chat",
        {
            "user_text": user_text,
            "image_paths": image_paths,
            "max_tokens": max_tokens,
        },
        timeout_sec=timeout_sec,
    )


atexit.register(_PROCESS_SUPERVISOR.shutdown)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test = sys.argv[1] if len(sys.argv) > 1 else "/path/to/test.jpg"
    print(describe_image(test))
