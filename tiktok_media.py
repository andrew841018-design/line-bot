"""Read TikTok videos and photo posts for link prefetch (2026-10-17).

A video is downloaded as a small h264 copy and a photo post's images are
fetched; gemini_light_model reads them inline (``gemini_client.media_side_task``).
Each reader returns ``(block, usable)``: the prompt block, and whether it holds
real content, which main.py notes on the caller's thread (the link-content
recorder is thread-local).  Gemini gets only public media and a fixed prompt:
never chat text, the link, or who shared it.

main.py resolves share links, routes video and photo URLs, and runs a reader
on one daemon thread within the message's time budget; readers check
``deadline`` and ``cancel`` themselves and start no request after either.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from io import BytesIO
from urllib.parse import urlsplit

from google.genai import types

import gemini_client
import reply_policy
import safe_fetch

try:
    import yt_dlp as _yt_dlp  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - the bot runs with yt-dlp installed
    _yt_dlp = None

logger = logging.getLogger("tiktok_media")

VIDEO_MAX_DURATION_SEC = 180
# Gemini's inline request limit is 20 MB after base64 (x4/3): 13 MB leaves room.
MAX_MEDIA_BYTES = 13_000_000
# TikTok's own preference ranks first in `worst`, which picked the watermarked
# full-size `download` stream (16.7 MB for 161 s) or an unplayable bytevc2 one.
# This asks for the smallest playable h264 copy with sound and a known size.
VIDEO_FORMAT = (
    "best[ext=mp4][protocol=https][vcodec^=h264][acodec!=none][filesize<13M]"
    "/best[ext=mp4][protocol=https][vcodec^=h264][acodec!=none][filesize_approx<13M]"
)
VIDEO_FORMAT_SORT = ["+size", "+br"]
VIDEO_GEMINI_MIN_SEC = 8.0  # no Gemini request starts with less time left
PHOTO_GEMINI_MIN_SEC = 5.0
PHOTO_MAX_IMAGES = 6
PHOTO_IMAGE_MAX_BYTES = 5 * 1024 * 1024
PHOTO_MAX_PIXELS = 20_000_000
PHOTO_FORMATS = ("JPEG", "PNG", "WEBP")
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
VIDEO_HOSTS = ("tiktok.com", "tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com")
IMAGE_HOSTS = ("tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com")
SUMMARY_MAX_CHARS = 600
CAPTION_MAX_CHARS = 1500
CACHE_TTL_SEC = 900
CACHE_MAX_ENTRIES = 64
TMP_PREFIX = "linebot_tiktok_"
_TOO_LARGE = "tiktok media too large"
ORPHAN_AGE_SEC = 3600
_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

VIDEO_PROMPT = (
    "用繁體中文寫出這支影片的主要內容：口白、畫面上的文字和想傳達的訊息，200 字內。"
    "看不出內容（沒有口白、沒有文字或畫面看不清楚）就把 has_content 設成 false、summary 留空。"
)
PHOTO_PROMPT = (
    "這些是一則 TikTok 圖文貼文的圖片（依順序）。用繁體中文寫出圖片裡的文字重點和畫面內容，300 字內。"
    "圖片看不清楚或沒有內容就把 has_content 設成 false、summary 留空。"
)
_ANSWER_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "has_content": types.Schema(type=types.Type.BOOLEAN),
        "summary": types.Schema(type=types.Type.STRING),
    },
    required=["has_content", "summary"],
)

# main._VIDEO_ANALYSIS_NON_CONTENT_RE (IG/FB/X keep that one) plus photos: a
# clause saying the model could not see or read the media, asking for help or
# apologising — not 「畫面中的車輛無法啟動」, which describes the media itself.
_NON_CONTENT_RE = re.compile(
    r"^(?:抱歉|很抱歉|不好意思|對不起|sorry)$"
    r"|^(?:因此|所以|也)?(?:我|咪寶)?(?:目前|這次|暫時)?(?:無法|不能|沒辦法)"
    r"(?:辨識|讀取|取得|觀看|分析|存取|處理|解析|提供|判斷|給出|回答|評論|看清|看出)"
    r"|^(?:因此|所以|也)?(?:我|咪寶)?(?:目前|這次|暫時)?(?:看不到|看不見|看不清楚?|看不出|讀不出|讀不到)"
    r"|^(?:因此|所以|也)?(?:目前|這次)?(?:沒有|無)(?:可用|足夠|任何)?的?(?:影片|圖片|照片)?(?:分析|內容|資訊)"
    r"|^(?:這支|這部|這段|這則|這張|這些|這幾張|此張|該張|該|此)?(?:影片|視頻|圖片|照片|圖片文字|畫面)"
    r"(?:內容|文字)?(?:目前)?(?:中)?(?:我|咪寶)?(?:目前|暫時)?(?:太小|太暗|太模糊)?"
    r"(?:無法|不能|沒辦法|看不到|看不見|看不清楚?|不清楚|太?模糊|看不出|讀不出"
    r"|沒有(?:可辨識|可讀|清楚|任何|可用)的?(?:文字|內容|資訊|畫面))"
    r"|^(?:看不清楚?|看不出|讀不出)"
    r"|^(?:你|您)?(?:可以|可|請|需要|如果方便)[^，。]{0,6}(?:提供|補充|上傳|重貼|重新|稍後|自行|自己)"
    r"|^i\s+(?:can(?:no|')t|am\s+unable|could(?:n't|\s+not))",
    re.IGNORECASE,
)


def usable_text(text: str) -> bool:
    """True when some clause of a Gemini answer describes the media itself."""
    if not text or reply_policy.is_empty_marker(text):
        return False
    clauses = re.split(r"[。！？!?；;，,\n]+|但是?|不過|然而", text)
    return any(
        len(reply_policy._normalize(clause)) >= 4 and not _NON_CONTENT_RE.search(clause.strip())
        for clause in clauses
    )


def clean_text(value: object, limit: int) -> str:
    """One line of uploader or model text, safe to put inside a prompt block."""
    text = reply_policy.neutralize_material(value)
    text = " / ".join(part.strip() for part in text.splitlines() if part.strip())
    return text[:limit] + "…" if len(text) > limit else text


def _host_ok(url: object, domains) -> bool:
    """https, no explicit port, and a host under one of ``domains``."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        host = safe_fetch.public_host(url)
        port = parts.port
    except Exception:
        return False
    return (
        parts.scheme == "https"
        and port is None
        and any(host == d or host.endswith("." + d) for d in domains)
    )


def _out_of_time(deadline: float, cancel: threading.Event, need: float = 0.0) -> bool:
    return cancel.is_set() or deadline - time.monotonic() <= need


_runtime_logged = False


def _log_runtime_once() -> None:
    """Log the yt-dlp / curl_cffi pair once: outside curl_cffi 0.10-0.16 yt-dlp
    drops impersonation and TikTok's challenge fails without any other sign."""
    global _runtime_logged
    if _runtime_logged or _yt_dlp is None:
        return
    _runtime_logged = True
    try:
        import curl_cffi  # type: ignore[import-untyped]

        curl_version = curl_cffi.__version__
    except Exception:  # noqa: BLE001
        curl_version = "missing"
    try:
        with _yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "logger": _YtdlpLogger()}) as ydl:
            targets = len(ydl._get_available_impersonate_targets())
    except Exception:  # noqa: BLE001
        targets = 0
    (logger.info if targets else logger.warning)(
        "tiktok media: yt-dlp %s, curl_cffi %s, impersonate targets %d",
        _yt_dlp.version.__version__, curl_version, targets,
    )


class _YtdlpLogger:
    """yt-dlp writes errors to stderr even when quiet; keep them short and URL-free."""

    @staticmethod
    def _short(msg: object) -> str:
        return re.sub(r"https?://\S+", "<url>", str(msg))[:160]

    def debug(self, msg) -> None:
        pass

    def info(self, msg) -> None:
        pass

    def warning(self, msg) -> None:
        logger.debug("yt-dlp warning: %s", self._short(msg))

    def error(self, msg) -> None:
        logger.info("yt-dlp error: %s", self._short(msg))


# ── cache and temp dirs ──────────────────────────────────────────────────────
# The same post often arrives twice within minutes (a burst, then a quote or a
# pending drain): finished reads, negative ones included, are reused.

_cache: dict[tuple[str, str], tuple[float, object]] = {}
_cache_lock = threading.Lock()
_swept = False


def _cache_get(key: tuple[str, str]) -> tuple[bool, object]:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None or time.monotonic() - entry[0] > CACHE_TTL_SEC:
            _cache.pop(key, None)
            return False, None
        return True, entry[1]


def _cache_put(key: tuple[str, str], value: object) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
        while len(_cache) > CACHE_MAX_ENTRIES:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]))


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _sweep_orphans() -> None:
    """Remove temp dirs a killed process left behind (once per process)."""
    global _swept
    if _swept:
        return
    _swept = True
    root = tempfile.gettempdir()
    try:
        names = os.listdir(root)
    except OSError:
        return
    now = time.time()
    for name in names:
        if not name.startswith(TMP_PREFIX):
            continue
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and not os.path.islink(path) and now - os.path.getmtime(path) > ORPHAN_AGE_SEC:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


# ── Gemini ───────────────────────────────────────────────────────────────────

def _ask(contents: list, deadline: float, cancel: threading.Event, *, need: float,
         low_resolution: bool, label: str) -> tuple[str, bool] | None:
    """``(summary, usable)`` from one media request, or None when none was answered."""
    if _out_of_time(deadline, cancel, need):
        return None
    remaining = deadline - time.monotonic()
    extra = {"media_resolution": types.MediaResolution.MEDIA_RESOLUTION_LOW} if low_resolution else {}
    config = types.GenerateContentConfig(
        http_options=types.HttpOptions(timeout=int(remaining * 1000)),
        response_mime_type="application/json",
        response_schema=_ANSWER_SCHEMA,
        max_output_tokens=1024,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        **extra,
    )
    response = gemini_client.media_side_task(
        contents,
        config,
        label=label,
        still_wanted=lambda: not _out_of_time(deadline, cancel, need * 0.75),
        expired=lambda: _out_of_time(deadline, cancel),
    )
    if response is None:
        return None
    try:
        answer = json.loads((getattr(response, "text", None) or "").strip() or "{}")
    except ValueError:
        answer = None
    if (
        not isinstance(answer, dict)
        or not isinstance(answer.get("has_content"), bool)
        or not isinstance(answer.get("summary"), str)
    ):
        # Malformed or empty: not an answer, so nothing is cached and the next share retries.
        logger.info("%s: answer was not the expected JSON", label)
        return None
    raw = reply_policy.neutralize_material(answer.get("summary"))
    # Judge the answer before clean_text joins its lines: a line break is a
    # clause boundary (「抱歉\n這些圖片無法辨識」 must not pass as one clause).
    usable = answer.get("has_content") is True and usable_text(raw)
    return clean_text(raw, SUMMARY_MAX_CHARS), usable


# ── videos ───────────────────────────────────────────────────────────────────

def _download_video(video_url: str, deadline: float, cancel: threading.Event) -> tuple[bytes | None, bool]:
    """``(data, final)``: the smallest playable h264 copy (at most MAX_MEDIA_BYTES).

    Without data, ``final`` says the answer will not change within minutes
    (too long, live, too large, no small copy, foreign media host) and may be
    cached; time, cancel and network failures are not final.
    """
    if _yt_dlp is None or _out_of_time(deadline, cancel, VIDEO_GEMINI_MIN_SEC):
        return None, False
    _log_runtime_once()
    _sweep_orphans()
    tmpdir = tempfile.mkdtemp(prefix=TMP_PREFIX)
    path = os.path.join(tmpdir, "video.mp4")

    def hook(progress: dict) -> None:
        if int(progress.get("downloaded_bytes") or 0) > MAX_MEDIA_BYTES:
            raise _yt_dlp.utils.DownloadCancelled(_TOO_LARGE)
        if _out_of_time(deadline, cancel, VIDEO_GEMINI_MIN_SEC):
            raise _yt_dlp.utils.DownloadCancelled("tiktok media budget")

    remaining = deadline - time.monotonic()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "logger": _YtdlpLogger(),
        "format": VIDEO_FORMAT,
        "format_sort": VIDEO_FORMAT_SORT,
        "check_formats": False,  # no probe download before the media host is checked
        "outtmpl": os.path.join(tmpdir, "video.%(ext)s"),
        "socket_timeout": max(1.0, min(10.0, remaining)),
        # Strict: a missing duration fails too.  Not a hard bound (a slideshow's
        # music length can stand in); the byte cap is.
        "match_filter": _yt_dlp.utils.match_filter_func(
            f"duration <= {VIDEO_MAX_DURATION_SEC} & !is_live"
        ),
        "max_filesize": MAX_MEDIA_BYTES,
        "progress_hooks": [hook],
    }
    try:
        with _yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            media_url = (info or {}).get("url")
            if not media_url:
                # match_filter already ran inside extract_info: too long, live, or no duration.
                logger.info("tiktok video: filtered (too long, live or no duration)")
                return None, True
            # yt-dlp fetches the stream itself: only a TikTok media host on a public address.
            if not _host_ok(media_url, VIDEO_HOSTS):
                logger.info("tiktok video: media URL refused")
                return None, True
            safe_fetch.check_url(media_url)
            if _out_of_time(deadline, cancel, VIDEO_GEMINI_MIN_SEC):
                return None, False
            ydl.process_ie_result(info, download=True)
        if not os.path.isfile(path):
            if _out_of_time(deadline, cancel, VIDEO_GEMINI_MIN_SEC):
                return None, False
            logger.info("tiktok video: nothing downloaded (too long, live, too large or no small h264 copy)")
            return None, True
        size = os.path.getsize(path)
        if not 0 < size <= MAX_MEDIA_BYTES:
            return None, True
        with open(path, "rb") as fh:
            return fh.read(), False
    except Exception as e:  # noqa: BLE001 - every failure means "not read"
        # No small playable copy, or bigger than its declared size: same answer next time.
        final = "Requested format is not available" in str(e) or _TOO_LARGE in str(e)
        logger.info("tiktok video: download failed error_type=%s final=%d", type(e).__name__, int(final))
        return None, final
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _video_block(url: str, summary: str) -> str:
    return (
        f"（以下是影片連結 {url} 的內容，由 Gemini Video Understanding 分析）\n"
        f"--- 影片分析開始 ---\n{summary}\n--- 影片分析結束 ---"
    )


def cached_video(url: str, video_id: str) -> tuple[bool, tuple[str, bool] | None]:
    """``(hit, result)`` from an earlier read of this video, without any work."""
    hit, cached = _cache_get(("video", video_id))
    if not hit:
        return False, None
    if cached is None or not cached[0]:
        return True, None
    return True, (_video_block(url, cached[0]), cached[1])


def read_video(url: str, video_url: str, video_id: str, deadline: float,
               cancel: threading.Event) -> tuple[str, bool] | None:
    """A TikTok video watched by Gemini: ``(block, usable)`` or None.

    ``url`` is the link as shared (shown in the block); ``video_url`` the
    canonical www.tiktok.com/@user/video/<id> that yt-dlp reads.
    """
    hit, result = cached_video(url, video_id)
    if hit:
        return result
    key = ("video", video_id)
    data, final = _download_video(video_url, deadline, cancel)
    if data is None:
        if final:
            _cache_put(key, None)  # same answer within minutes
        return None
    answer = _ask(
        [types.Part.from_bytes(data=data, mime_type="video/mp4"), VIDEO_PROMPT],
        deadline, cancel, need=VIDEO_GEMINI_MIN_SEC, low_resolution=True, label="tiktok video",
    )
    if answer is None:
        return None  # not cached: Gemini may answer next time
    _cache_put(key, answer)
    summary, usable = answer
    return (_video_block(url, summary), usable) if summary else None


# ── photo posts ──────────────────────────────────────────────────────────────

def _fetch_image(src: str, max_bytes: int, deadline: float, index: int) -> tuple[bytes, str] | None:
    # Gemini's share stays untouched: _ask refuses at <= its floor, so keep one more second.
    remaining = deadline - time.monotonic() - PHOTO_GEMINI_MIN_SEC - 1.0
    if remaining < 1.0 or max_bytes <= 0:
        return None
    try:
        resp = safe_fetch.http.get(
            src,
            timeout=5,
            allow_redirects=False,
            headers={"User-Agent": _USER_AGENT},
            max_bytes=max_bytes,
            accept_types=IMAGE_TYPES,
            deadline=min(8.0, remaining),
        )
    except Exception as e:  # noqa: BLE001
        # The message would carry the signed URL; log the type only.
        logger.info("tiktok photo: image %d fetch failed error_type=%s", index, type(e).__name__)
        return None
    if resp.status_code != 200:
        logger.info("tiktok photo: image %d status=%d", index, resp.status_code)
        return None
    data = resp.content or b""
    try:
        from PIL import Image

        with Image.open(BytesIO(data), formats=list(PHOTO_FORMATS)) as image:
            fmt = image.format
            width, height = image.size
        mime = Image.MIME.get(fmt or "")
    except Exception as e:  # noqa: BLE001 - not an image we accept
        logger.info("tiktok photo: image %d not readable error_type=%s", index, type(e).__name__)
        return None
    if fmt not in PHOTO_FORMATS or not mime or width * height > PHOTO_MAX_PIXELS:
        return None
    return data, mime


def _pixels(image: dict) -> int:
    try:
        return int(image.get("imageWidth")) * int(image.get("imageHeight"))
    except (TypeError, ValueError):
        return 0  # unknown: Pillow checks the real size


def _photo_post(user: str, post_id: str, deadline: float, cancel: threading.Event) -> tuple[dict | None, bool]:
    """``(post, final)``: author, caption and the Gemini answer for a photo post.

    Without web data, ``final`` says TikTok answered definitely (a status code,
    or a post without images) and the miss may be cached; a failed fetch is not.
    """
    if _yt_dlp is None or _out_of_time(deadline, cancel, PHOTO_GEMINI_MIN_SEC):
        return None, False
    _log_runtime_once()
    remaining = deadline - time.monotonic()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _YtdlpLogger(),
        "socket_timeout": max(1.0, min(10.0, remaining)),
    }
    try:
        with _yt_dlp.YoutubeDL(opts) as ydl:
            # yt-dlp has no photo-post extractor ("Unsupported URL"); its TikTok
            # extractor's web-data reader (private; contract-tested) returns the post.
            data, status = ydl.get_info_extractor("TikTok")._extract_web_data_and_status(
                f"https://www.tiktok.com/@{user}/video/{post_id}", post_id, fatal=False
            )
    except Exception as e:  # noqa: BLE001
        logger.info("tiktok photo: web data failed error_type=%s", type(e).__name__)
        return None, False
    images = (data.get("imagePost") or {}).get("images") if isinstance(data, dict) else None
    if status != 0 or not isinstance(images, list) or not images:
        logger.info("tiktok photo: no images status=%s", status)
        definite = isinstance(status, int) and (status > 0 or (status == 0 and isinstance(data, dict) and bool(data)))
        return None, definite
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    post = {
        "author": clean_text(author.get("nickname") or author.get("uniqueId"), 60),
        "caption": clean_text(data.get("desc"), CAPTION_MAX_CHARS),
        "images": 0,
        "answer": None,
    }
    parts: list = []
    used = 0
    for index, image in enumerate(images[:PHOTO_MAX_IMAGES]):
        # Stop while Gemini still has time to read what was fetched.
        if _out_of_time(deadline, cancel, PHOTO_GEMINI_MIN_SEC + 1.0) or used >= MAX_MEDIA_BYTES:
            break
        if not isinstance(image, dict) or _pixels(image) > PHOTO_MAX_PIXELS:
            continue
        sources = ((image.get("imageURL") or {}).get("urlList") or [])
        for src in sources:
            if not _host_ok(src, IMAGE_HOSTS):
                continue
            got = _fetch_image(src, min(PHOTO_IMAGE_MAX_BYTES, MAX_MEDIA_BYTES - used), deadline, index)
            if got:
                parts.append(types.Part.from_bytes(data=got[0], mime_type=got[1]))
                used += len(got[0])
                break
    post["images"] = len(parts)
    if parts:
        # The images only: a caption paraphrased back would pass as "read".
        post["answer"] = _ask(
            [*parts, PHOTO_PROMPT], deadline, cancel,
            need=PHOTO_GEMINI_MIN_SEC, low_resolution=False, label="tiktok photo",
        )
    return post, False


def _photo_block(url: str, post: dict) -> str:
    answer = post.get("answer")
    summary = answer[0] if answer else ""
    lines = [
        f"（以下是 TikTok 圖文貼文 {url} 的內容：說明文字＋{post['images']} 張圖片，圖片由 Gemini 讀取）",
        "--- TikTok 圖文開始 ---",
    ]
    if post.get("author"):
        lines.append(f"作者：{post['author']}")
    if post.get("caption"):
        lines.append(f"說明文字：{post['caption']}")
    lines.append(f"圖片內容：{summary}" if summary else "圖片內容：未取得；上方只有說明文字，不是完整貼文內容。")
    lines.append("--- TikTok 圖文結束 ---")
    return "\n".join(lines)


def _photo_result(url: str, post: dict | None) -> tuple[str, bool] | None:
    if post is None:
        return None
    answer = post.get("answer")
    return _photo_block(url, post), bool(answer and answer[1])


def cached_photo(url: str, post_id: str) -> tuple[bool, tuple[str, bool] | None]:
    """``(hit, result)`` from an earlier read of this photo post, without any work."""
    hit, post = _cache_get(("photo", post_id))
    return (True, _photo_result(url, post)) if hit else (False, None)


def read_photo_post(url: str, user: str, post_id: str, deadline: float,
                    cancel: threading.Event) -> tuple[str, bool] | None:
    """A TikTok photo post: caption plus Gemini reading its images.

    ``(block, usable)``; usable only when Gemini described the images.  None
    when TikTok's web data has no images (main.py then falls back to oEmbed).
    """
    hit, result = cached_photo(url, post_id)
    if hit:
        return result
    post, final = _photo_post(user, post_id, deadline, cancel)
    if post is not None and post["answer"] is not None:
        _cache_put(("photo", post_id), post)  # only reads Gemini answered; the rest retries
    elif post is None and final:
        _cache_put(("photo", post_id), None)
    return _photo_result(url, post)
