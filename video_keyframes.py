"""影片 keyframes 抽取 — 給 vision LLM 看。

策略：用 ffmpeg 抽 scene-change frames（不是固定間隔，scene 變化才抽）。
最多 8 frames（控 vision LLM 輸入）。
"""
from __future__ import annotations
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger("video_keyframes")

_FFMPEG = "/opt/homebrew/bin/ffmpeg"  # M-series Mac brew 路徑
_TEMP_VIDEO_BY_FRAME_DIR: dict[str, str] = {}
_TEMP_VIDEO_LOCK = threading.Lock()


def extract_keyframes(
    video_path: str | bytes,
    max_frames: int = 8,
    scene_threshold: float = 0.3,
) -> list[Path]:
    """抽 keyframes。回 list of Path。失敗回空 list。

    video_path 可以是檔案路徑或 bytes。
    """
    # bytes → temp file
    temp_video_path: str | None = None
    if isinstance(video_path, bytes):
        tmp_video = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        tmp_video.write(video_path)
        tmp_video.close()
        temp_video_path = tmp_video.name
        video_path = temp_video_path

    out_dir = Path(tempfile.mkdtemp(prefix="kf_"))
    frames: list[Path] = []

    # 試 scene-change selector
    try:
        cmd = [
            _FFMPEG, "-i", str(video_path),
            "-vf", f"select='gt(scene,{scene_threshold})'",
            "-vsync", "vfr",
            "-frames:v", str(max_frames),
            "-q:v", "2",
            str(out_dir / "frame_%03d.jpg"),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            logger.warning("scene-change extract failed: %s", result.stderr.decode()[:200])
            # fallback: 固定間隔抽
            frames = _fallback_uniform(video_path, out_dir, max_frames)
            return frames
        frames = sorted(out_dir.glob("frame_*.jpg"))
        if not frames:
            frames = _fallback_uniform(video_path, out_dir, max_frames)
        return frames
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg timeout")
        return []
    except Exception as e:
        logger.warning("extract_keyframes failed: %s", e)
        return []
    finally:
        if frames:
            if temp_video_path:
                with _TEMP_VIDEO_LOCK:
                    _TEMP_VIDEO_BY_FRAME_DIR[str(out_dir)] = temp_video_path
        else:
            shutil.rmtree(out_dir, ignore_errors=True)
            if temp_video_path:
                try:
                    os.unlink(temp_video_path)
                except OSError:
                    pass


def _fallback_uniform(video_path, out_dir: Path, max_frames: int) -> list[Path]:
    """固定間隔抽 N frames（scene-change 失敗的 fallback）"""
    try:
        # 取 video 長度
        probe = subprocess.run(
            [_FFMPEG, "-i", str(video_path)],
            capture_output=True, timeout=10,
        )
        # 從 stderr parse Duration
        import re
        m = re.search(r"Duration: (\d+):(\d+):(\d+)", probe.stderr.decode())
        if not m:
            return []
        h, mn, s = map(int, m.groups())
        total_s = h*3600 + mn*60 + s
        if total_s == 0:
            return []
        interval = total_s / (max_frames + 1)
        # 抽 max_frames 張
        for i in range(1, max_frames + 1):
            ts = interval * i
            cmd = [
                _FFMPEG, "-ss", str(ts), "-i", str(video_path),
                "-frames:v", "1", "-q:v", "2",
                str(out_dir / f"frame_{i:03d}.jpg"),
            ]
            subprocess.run(cmd, capture_output=True, timeout=10)
        return sorted(out_dir.glob("frame_*.jpg"))
    except Exception as e:
        logger.warning("fallback uniform extract failed: %s", e)
        return []


def cleanup(frames: list[Path]) -> None:
    """清掉 temp frames。"""
    if not frames:
        return
    parent = frames[0].parent
    try:
        shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        pass
    with _TEMP_VIDEO_LOCK:
        temp_video_path = _TEMP_VIDEO_BY_FRAME_DIR.pop(str(parent), None)
    if temp_video_path:
        try:
            os.unlink(temp_video_path)
        except OSError:
            pass
