"""媒體（圖片 / 影片）處理 pipeline — 純本機，零雲端 LLM。

組合：
  圖片：OCR 抽文字 → 本機 vision LLM 回應；解析不外顯，無回應則靜默
  影片：抽 keyframes → 本機多圖 vision LLM → 本機失敗 → 沉默

跟 main.py 既有 Gemini Vision (handle image / video event) 平行。
quota 爆時 main 改 call 這邊。
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

from image_reply import (
    IMAGE_RESPONSE_CONTRACT,
    is_image_context_echo,
    render_image_reply,
    unsolicited_image_reply,
)
from video_reply import VIDEO_COMMENTARY_CONTRACT, VIDEO_CACHE_VERSION

logger = logging.getLogger("media_pipeline")

_IMAGE_ANALYSIS_DEFAULT_TIMEOUT_SEC = 40.0
_OCR_DEGRADED_REPLY_PREFIX = "⚠️ 文字辨識降級結果"
_OCR_DEGRADED_REPLY_RESERVE_SEC = 1.0
_OCR_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-ocr")
_OCR_ADMISSION = threading.BoundedSemaphore(1)
_LOCAL_LLM_ALERT_LOCK = threading.Lock()
_local_llm_alert_inflight: int | None = None
_local_llm_alert_epoch = 0
_local_llm_alert_dispatch_epochs: set[int] = set()


class MediaVisionTimeoutError(TimeoutError):
    """The caller's absolute image-analysis budget was exhausted."""


def _remaining_image_budget(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MediaVisionTimeoutError("image analysis deadline exhausted")
    return remaining


def _extract_ocr_with_deadline(extract_text, image, timeout_sec: float):
    """Bound OCR without queuing late work; a hung call can occupy one thread only."""
    if not _OCR_ADMISSION.acquire(blocking=False):
        logger.info("OCR worker busy; continue without OCR")
        return None
    try:
        future = _OCR_EXECUTOR.submit(extract_text, image)
    except BaseException:
        _OCR_ADMISSION.release()
        raise
    future.add_done_callback(lambda _future: _OCR_ADMISSION.release())
    try:
        return future.result(timeout=max(0.0, float(timeout_sec)))
    except FutureTimeoutError as exc:
        future.cancel()
        raise MediaVisionTimeoutError("OCR exceeded image deadline") from exc


# ── Phase 1 media_cache helpers (byte-exact dedup, group-scoped) ───────────
#
# Phase 1.5 deferred (per advisor family-bot threat model recalibration):
#   - In-flight dedup（immediate handler + drain worker 跑同 sha 同 race）
#     — family-bot 5 人 rare event, accept；Phase 1.5 加 Python module-level
#     `_inflight: dict[(media_type, sha), Event]` + try/finally
#   - PII regex on description / Reply quality gate / DoS limits
#     — wrong threat model (family chat 互看 PII，非 adversarial); enterprise scope
#   - cache_version 沒加：見 memory.insert_media_cache docstring，改 prompt
#     後手動 DELETE invalidate

_MIN_CACHE_BYTES = 1024       # 太小檔不算（preview / corrupted / sticker）
_MIN_CACHE_REPLY_LEN = 200    # 太短 reply 不 cache（L3 raw desc 通常 < 200 字）

_local_llm_down_alerted = False  # once-per-outage vision alert; visual success re-arms it

_MARKET_SCREENSHOT_HINT_RE = re.compile(
    r"牛牛|富途|Futu|Futubull|moomoo|Moomoo|道瓊|道琼|那斯達克|納斯達克|"
    r"標普|标普|S&P|費半|费半|現貨黃金|现货黄金|XAU|\bGold\b",
    re.IGNORECASE,
)
_MARKET_SCREENSHOT_SOURCE_RE = re.compile(r"牛牛|富途|Futu|Futubull|moomoo|Moomoo", re.IGNORECASE)
_MARKET_SCREENSHOT_STRONG_TERM_RE = re.compile(
    r"道瓊|道琼|那斯達克|納斯達克|纳斯达克|標普|标普|S\s*&\s*P|費半|费半|"
    r"現貨黃金|现货黄金|國際金|国际金|XAU|\bGold\b|DJI|DJIA|IXIC|NDX|SPX|SOX|SOXX",
    re.IGNORECASE,
)
_MARKET_ALIAS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("道瓊指數", re.compile(r"道瓊|道琼|Dow|DJI|DJIA", re.IGNORECASE)),
    ("納斯達克指數", re.compile(r"那斯達克|納斯達克|纳斯达克|Nasdaq|IXIC|NDX", re.IGNORECASE)),
    ("S&P 500", re.compile(r"標普|标普|S\s*&\s*P\s*500|S&P500|SPX", re.IGNORECASE)),
    ("費城半導體指數", re.compile(r"費半|费半|SOX|SOXX|半導體|半导体", re.IGNORECASE)),
    ("黃金", re.compile(r"現貨黃金|现货黄金|國際金|国际金|黃金|黄金|XAU|\bGold\b", re.IGNORECASE)),
)
_PERCENT_RE = re.compile(r"[+\-−]?\d+(?:\.\d+)?\s*%")
_NUMBER_RE = re.compile(r"[+\-−]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[+\-−]?\d+(?:\.\d+)?")
_IMAGE_ARGUMENT_CONTRACT = IMAGE_RESPONSE_CONTRACT
# v3 (2026-10-07): replies cached before the correction-or-advice policy may
# only retell the picture, or carry post_check's old 「從圖裡看，」 rewrite.
_IMAGE_CACHE_VERSION = b"image-response-only-v3\0"
# The task for a picture nobody asked about.  「請分析這張圖片」 invited the
# model to retell what the picture shows.
_UNASKED_IMAGE_TASK = "有人把這張圖貼到群組，沒有另外提問。"


def _build_image_argument_prompt(user_prompt: str = "") -> str:
    task = (user_prompt or "").strip() or _UNASKED_IMAGE_TASK
    return f"{task}\n\n{_IMAGE_ARGUMENT_CONTRACT}"


def _has_image_argument_structure(reply: str | None) -> bool:
    """Compatibility name: require a usable answer, not a fixed section layout."""
    rendered = render_image_reply(reply)
    return bool(rendered and len(rendered) >= 4)


def _ensure_image_argument_structure(
    reply: str | None, *, desc: str = "", ocr_text: str = ""
) -> str | None:
    """Keep the answer without appending internal description or OCR context."""
    if is_image_context_echo(reply, ocr_text):
        return None
    if ocr_text.strip():
        # Vision applies post_check before returning. Its presentation rewrite
        # must not turn a verbatim OCR echo into an apparent answer.
        from vision_common import post_check

        contexts = (ocr_text, ocr_text[:500], ocr_text[:1500])
        if is_image_context_echo(reply, *contexts, *(post_check(s) for s in contexts)):
            return None
    return render_image_reply(reply)


def _normalize_num_token(token: str) -> str:
    return (token or "").strip().replace("−", "-").replace(" ", "")


def _extract_market_numbers(blob: str) -> tuple[str | None, str | None, str | None]:
    """Return (level, change, percent) from one OCR row/window, conservatively."""
    without_pct = _PERCENT_RE.sub(" ", blob or "")
    numbers = [_normalize_num_token(n) for n in _NUMBER_RE.findall(without_pct)]
    numbers = [n for n in numbers if n and n not in {"+", "-"}]
    percent_matches = [_normalize_num_token(p) for p in _PERCENT_RE.findall(blob or "")]

    level = None
    for token in numbers:
        try:
            value = abs(float(token.replace(",", "")))
        except ValueError:
            continue
        if "," in token or value >= 20:
            level = token
            break

    change = None
    for token in numbers:
        if token == level:
            continue
        if token.startswith(("+", "-")):
            change = token
            break

    percent = percent_matches[0] if percent_matches else None
    return level, change, percent


def _iter_market_segments(text: str) -> list[tuple[str, str]]:
    """Split OCR text into per-symbol windows even when OCR collapsed newlines."""
    matches: list[tuple[int, int, str]] = []
    for label, pattern in _MARKET_ALIAS_PATTERNS:
        for match in pattern.finditer(text or ""):
            matches.append((match.start(), match.end(), label))
    matches.sort(key=lambda item: item[0])

    segments: list[tuple[str, str]] = []
    seen: set[str] = set()
    for idx, (start, _end, label) in enumerate(matches):
        if label in seen:
            continue
        stop = matches[idx + 1][0] if idx + 1 < len(matches) else len(text)
        segment = text[start:stop]
        if segment.strip():
            segments.append((label, segment))
            seen.add(label)
    return segments


def _extract_market_screenshot_reply(ocr_text: str) -> Optional[str]:
    """Parse Futu/moomoo/NiuNiu market screenshots from OCR text.

    This path is deliberately extractive: it reports only numbers visible in the
    image OCR and never fills missing values from Yahoo, Gemini, or memory.
    """
    text = (ocr_text or "").strip()
    if not text or not _MARKET_SCREENSHOT_HINT_RE.search(text):
        return None
    if not (_MARKET_SCREENSHOT_SOURCE_RE.search(text) or _MARKET_SCREENSHOT_STRONG_TERM_RE.search(text)):
        return None

    rows: list[tuple[str, str | None, str | None, str | None]] = []
    for label, segment in _iter_market_segments(text):
        level, change, percent = _extract_market_numbers(segment)
        if not (level or change or percent):
            continue
        rows.append((label, level, change, percent))

    if not rows:
        return None

    rising, falling, flat = [], [], []
    for label, _level, _change, percent in rows:
        if not percent:
            continue
        try:
            movement = float(percent.replace("%", "").replace("−", "-").strip())
        except ValueError:
            continue
        (rising if movement > 0 else falling if movement < 0 else flat).append(label)
    if not (rising or falling or flat):
        return None
    if rising and falling:
        take = f"{'、'.join(rising)}走強，但{'、'.join(falling)}轉弱，表現有分歧。"
    elif rising:
        take = f"{'、'.join(rising)}偏強，但單次漲幅還不足以確認趨勢延續。"
    elif falling:
        take = f"{'、'.join(falling)}偏弱，暫時不宜只憑一次跌幅認定已止跌。"
    else:
        take = f"{'、'.join(flat)}變化有限，尚缺明確方向。"
    return take + "\n判斷僅依截圖所示變化；還需確認時間、量價與後續走勢。"


def _reset_local_llm_alert_guard() -> None:
    global _local_llm_down_alerted, _local_llm_alert_epoch
    with _LOCAL_LLM_ALERT_LOCK:
        _local_llm_alert_epoch += 1
        _local_llm_down_alerted = False


def _alert_local_llm_down(
    reason: str, *, expected_epoch: int | None = None
) -> None:
    """Send one categorical vision alert without leaking request content."""
    global _local_llm_down_alerted, _local_llm_alert_inflight
    with _LOCAL_LLM_ALERT_LOCK:
        alert_epoch = _local_llm_alert_epoch
        if expected_epoch is not None and alert_epoch != expected_epoch:
            return
        if (
            _local_llm_down_alerted
            or _local_llm_alert_inflight == alert_epoch
        ):
            return
        _local_llm_alert_inflight = alert_epoch
    code = str(reason or "")
    if code == "snapshot_missing":
        detail = (
            "圖片模型的本機快取無法載入。請檢查 WD_BLACK 與 "
            "~/.cache/huggingface symlink。"
        )
    elif code == "model_init_failed":
        detail = "圖片模型初始化失敗；外接碟與模型快取不一定有問題。"
    elif code == "cleanup_failed":
        detail = "圖片模型子程序無法安全回收；系統會維持隔離並嘗試自動恢復。"
    elif code == "warmup_timeout":
        detail = "圖片模型預熱逾時；這不代表外接碟或模型快取損壞。"
    else:
        detail = "圖片模型子程序目前不可用；這不代表外接碟或模型快取損壞。"
    delivery_status = "pending_unknown"
    try:
        import notify_discord
        result = notify_discord.send_dm_result(
            "⚠️ LINE bot " + detail + " 圖片目前會回降級訊息。"
        )
        delivery_status = getattr(result, "status", "pending_unknown")
    except Exception as exc:
        logger.warning(
            "vision alert delivery failed type=%s", type(exc).__name__
        )
    finally:
        with _LOCAL_LLM_ALERT_LOCK:
            if _local_llm_alert_epoch == alert_epoch:
                # A definite failure is safe to retry on the next affected
                # image. Sent and ambiguous delivery are deduped to avoid
                # double DMs. An older result cannot overwrite real recovery.
                _local_llm_down_alerted = delivery_status != "definite_failed"
            if _local_llm_alert_inflight == alert_epoch:
                _local_llm_alert_inflight = None


def _dispatch_local_llm_alert(reason: str) -> threading.Thread | None:
    """Dispatch one epoch-fenced operational alert off the reply-critical path."""
    global _local_llm_alert_dispatch_epochs
    with _LOCAL_LLM_ALERT_LOCK:
        alert_epoch = _local_llm_alert_epoch
        if (
            _local_llm_down_alerted
            or _local_llm_alert_inflight == alert_epoch
            or alert_epoch in _local_llm_alert_dispatch_epochs
        ):
            return None
        _local_llm_alert_dispatch_epochs.add(alert_epoch)

    def _deliver() -> None:
        try:
            _alert_local_llm_down(reason, expected_epoch=alert_epoch)
        finally:
            with _LOCAL_LLM_ALERT_LOCK:
                _local_llm_alert_dispatch_epochs.discard(alert_epoch)

    worker = threading.Thread(
        target=_deliver,
        name="vision-alert-delivery",
        daemon=True,
    )
    try:
        worker.start()
    except Exception as exc:
        with _LOCAL_LLM_ALERT_LOCK:
            _local_llm_alert_dispatch_epochs.discard(alert_epoch)
        logger.warning(
            "vision alert dispatch failed type=%s", type(exc).__name__
        )
        return None
    return worker


def _respond_to_ocr_text(ocr_text: str, user_prompt: str = "") -> Optional[str]:
    """本機 vision 描述不可用時：把 OCR 文字餵本機 14B，針對『內容』生回應（非 echo / 非裸吐）。

    單次快速本機呼叫（無 web search / grounding / critique），塞得進 _handle_image_message
    的 50s reply 視窗。回傳前過 vision_common.post_check 對齊規則 0 / 黑名單。
    本機 LLM 不可用（模型載不進來）回 None；解析文字不作降級回應。
    """
    text = (ocr_text or "").strip()
    if not text:
        return None
    try:
        import local_llm
    except Exception as e:
        logger.warning("_respond_to_ocr_text: local_llm import failed: %s", e)
        return None
    runtime_enabled = getattr(local_llm, "runtime_enabled", None)
    if callable(runtime_enabled) and not runtime_enabled():
        logger.info("OCR local text fallback skipped: runtime policy disabled")
        return None
    local_chat = local_llm.chat
    # 文字模式咪寶 prompt。不用 vision_common.compose_prompt：那是「看圖」取向、且會把內容
    # 塞進 system 又與 user_input 重複（codex NIT-2）。規則 0 / 黑名單對齊改靠回傳後 post_check。
    system = (
        "你是 LINE 群組對話助理咪寶，繁體中文、短句分行、像在群裡聊天。"
        "使用者貼了一張圖，以下是從圖中 OCR 抽到的文字。"
        "請『根據文字的內容』回應，不是描述圖片："
        "是問題就直接回答、是新聞/觀點就給有依據的判斷、是單據/文件就回答當前問題或指出有依據的問題，不預設摘錄或摘要。"
        "第一句必須是具體判斷或答案，禁止 echo 複述原文，"
        "禁止『這張圖 / 圖中顯示 / 我看到圖片』這種開頭。不確定就說不確定，不要編造數字或來源。"
        "\n\n"
        + _IMAGE_ARGUMENT_CONTRACT
    )
    model_input = text[:1500]
    if user_prompt.strip():
        model_input = f"【使用者要求】\n{user_prompt.strip()}\n\n【OCR（內部參考）】\n{model_input}"
    try:
        out = local_chat(model_input, system_prompt=system, max_tokens=500)
    except Exception as e:
        logger.warning("_respond_to_ocr_text: local_llm.chat failed: %s", e)
        return None
    reply = out.strip() if out and out.strip() else None
    if not reply:
        return None
    if is_image_context_echo(reply, text, model_input):
        return None
    try:
        from vision_common import post_check
        reply = (post_check(reply) or "").strip()
    except Exception as e:
        logger.warning("_respond_to_ocr_text: post_check skipped: %s", e)
    if not reply:
        return None
    if is_image_context_echo(reply, text, model_input):
        return None
    return _ensure_image_argument_structure(reply, ocr_text=text)


def _build_ocr_degraded_reply(ocr_text: str) -> Optional[str]:
    """No visible fallback when only parsed text is available."""
    # An OCR extract is internal context, never an answer on model failure.
    return None


def _to_bytes(media) -> Optional[bytes]:
    """Normalize 多型 media 輸入到 bytes（只給 sha256 算），原 media 不動。"""
    if isinstance(media, (bytes, bytearray)):
        return bytes(media)
    if isinstance(media, (str, Path)):
        try:
            with open(media, "rb") as f:
                return f.read()
        except Exception:
            return None
    return None


def _is_cache_quality_reply(reply) -> bool:
    """Phase 1 quality gate：排 OCR-only fallback / 太短 raw desc / 空字串。"""
    if not reply or not reply.strip():
        return False
    s = reply.strip()
    if s.startswith("📷 OCR"):           # L4 OCR-only fallback prefix
        return False
    if s.startswith(_OCR_DEGRADED_REPLY_PREFIX):
        return False
    if len(s) < _MIN_CACHE_REPLY_LEN:    # L3 raw desc 通常 < 200 字
        return False
    return True


def _maybe_lookup_media_cache(
    media,
    group_id: Optional[str],
    media_type: str,
) -> Optional[str]:
    """Cache hit return last_reply + bump_seen；miss / 無 group_id / 極小檔回 None。"""
    if not group_id:
        return None
    image_bytes = _to_bytes(media)
    if not image_bytes or len(image_bytes) < _MIN_CACHE_BYTES:
        return None
    try:
        import memory
        sha = memory.compute_sha256(
            (_IMAGE_CACHE_VERSION if media_type == "image" else
             VIDEO_CACHE_VERSION if media_type == "video" else b"") + image_bytes
        )
        hit = memory.lookup_media_cache(group_id, media_type, sha)
        if hit:
            if media_type == "image" and not _has_image_argument_structure(
                hit.get("last_reply")
            ):
                logger.info(
                    "media_cache STALE image reply ignored group=%s sha=%s",
                    group_id[:8],
                    sha[:8],
                )
                try:
                    memory.delete_media_cache(hit["cache_id"])
                except Exception as e:
                    logger.warning("media_cache stale delete failed: %s", e)
                return None
            memory.bump_media_cache_seen(hit["cache_id"])
            logger.info(
                "media_cache HIT group=%s type=%s sha=%s seen=%d",
                group_id[:8], media_type, sha[:8], hit["seen_count"] + 1,
            )
            return hit["last_reply"]
    except Exception as e:
        logger.warning("media_cache lookup failed: %s", e)
    return None


def _maybe_write_media_cache(
    media,
    group_id: Optional[str],
    media_type: str,
    description: Optional[str],
    reply: str,
) -> None:
    """Phase 1：caller 在高品質 layer return 前 call；quality gate 內部過。"""
    if not group_id or not _is_cache_quality_reply(reply):
        return
    if media_type == "image" and not _has_image_argument_structure(reply):
        logger.info("media_cache skip image reply without argument structure")
        return
    image_bytes = _to_bytes(media)
    if not image_bytes or len(image_bytes) < _MIN_CACHE_BYTES:
        return
    try:
        import memory
        sha = memory.compute_sha256(
            (_IMAGE_CACHE_VERSION if media_type == "image" else
             VIDEO_CACHE_VERSION if media_type == "video" else b"") + image_bytes
        )
        memory.insert_media_cache(group_id, media_type, sha, description, reply)
        logger.info(
            "media_cache WRITE group=%s type=%s sha=%s reply_len=%d",
            group_id[:8], media_type, sha[:8], len(reply),
        )
    except Exception as e:
        logger.warning("media_cache write failed: %s", e)


def analyze_image(
    image: str | Path | bytes,
    user_prompt: str = "",
    group_id: str | None = None,
    *,
    timeout_sec: float | None = None,
    unsolicited: bool | None = None,
) -> Optional[str]:
    """Return only the answer, including for cached and fallback results.

    ``unsolicited`` (default: no ``user_prompt``) means nobody asked about the
    picture, so only a correction or advice is returned (Andrew 2026-10-07:
    不要說明圖的內容); a reply that only retells the picture becomes None.
    With ``unsolicited=True`` a ``user_prompt`` is context such as a file
    name, not a question.
    """
    if unsolicited is None:
        unsolicited = not user_prompt.strip()
    elif unsolicited and user_prompt.strip():
        user_prompt = f"{_UNASKED_IMAGE_TASK}\n\n{user_prompt.strip()}"
    reply = render_image_reply(_analyze_image(
        image, user_prompt=user_prompt, group_id=group_id, timeout_sec=timeout_sec
    ))
    return unsolicited_image_reply(reply) if unsolicited else reply


def _analyze_image(
    image: str | Path | bytes,
    user_prompt: str = "",
    group_id: str | None = None,
    *,
    timeout_sec: float | None = None,
) -> Optional[str]:
    """對圖片產生回應。失敗回 None。

    流程：
      1. OCR 抽文字（如果有 ocr_helper）
      2. 本機 Vision LLM（mlx-vlm Qwen2.5-VL-7B）看圖 + 用 OCR 文字輔助 prompt
      3. 無法產生實質回應時靜默；OCR/解析文字不作降級答案。

    Phase 1（media_cache）：caller 傳 group_id 時走 byte-exact cache dedup
    （group-scoped），命中跳過 v4 7-step pipeline。
    """
    timeout = (
        _IMAGE_ANALYSIS_DEFAULT_TIMEOUT_SEC
        if timeout_sec is None
        else max(0.0, float(timeout_sec))
    )
    deadline = time.monotonic() + timeout

    # Default image answers cannot satisfy or cache a distinct explicit request.
    use_default_cache = not user_prompt.strip()
    cached = _maybe_lookup_media_cache(image, group_id, "image") if use_default_cache else None
    if cached:
        _remaining_image_budget(deadline)
        return cached

    # OCR
    ocr_text = None
    try:
        from ocr_helper import extract_text
        ocr_text = _extract_ocr_with_deadline(
            extract_text,
            image,
            _remaining_image_budget(deadline),
        )
        if ocr_text:
            logger.info("OCR 抽到 %d chars", len(ocr_text))
    except MediaVisionTimeoutError:
        raise
    except ImportError:
        logger.info("ocr_helper 未建，跳過 OCR")
    except Exception as e:
        logger.warning("OCR failed: %s", e)

    _remaining_image_budget(deadline)

    if ocr_text and not user_prompt.strip():
        market_reply = _extract_market_screenshot_reply(ocr_text)
        if market_reply:
            _remaining_image_budget(deadline)
            _maybe_write_media_cache(image, group_id, "image", ocr_text, market_reply)
            return market_reply

    has_ocr = bool(ocr_text and ocr_text.strip())

    # 拼 prompt
    prompt = _build_image_argument_prompt(user_prompt)
    if ocr_text:
        prompt += f"\n\n圖中已 OCR 抽到的文字（參考）：\n{ocr_text[:500]}"

    # Layer 1：本機 vision LLM 抽描述（raw bytes 不離本機）
    desc = None
    vision_failure_code = ""
    vision_capacity_errors: tuple[type[BaseException], ...] = ()
    vision_unavailable_error: type[BaseException] | None = None
    vision_kwargs = {"prompt": prompt}
    remaining = _remaining_image_budget(deadline)
    if (
        has_ocr
        and remaining is not None
        and remaining <= _OCR_DEGRADED_REPLY_RESERVE_SEC
    ):
        return None
    if remaining is not None:
        vision_kwargs["timeout_sec"] = (
            remaining - _OCR_DEGRADED_REPLY_RESERVE_SEC
            if has_ocr
            else remaining
        )
    try:
        import vision_llm

        candidate_capacity_errors = (
            getattr(vision_llm, "VisionBusyError", None),
            getattr(vision_llm, "VisionTimeoutError", None),
        )
        vision_capacity_errors = tuple(
            error_type
            for error_type in candidate_capacity_errors
            if isinstance(error_type, type)
            and issubclass(error_type, BaseException)
        )
        candidate_unavailable_error = getattr(
            vision_llm, "VisionUnavailableError", None
        )
        if (
            isinstance(candidate_unavailable_error, type)
            and issubclass(candidate_unavailable_error, BaseException)
        ):
            vision_unavailable_error = candidate_unavailable_error

        desc = vision_llm.describe_image(image, **vision_kwargs)
    except ImportError:
        logger.info("vision_llm 未建")
    except Exception as e:
        if vision_capacity_errors and isinstance(e, vision_capacity_errors):
            if has_ocr:
                return None
            raise
        failure_code = getattr(e, "code", "worker_unavailable")
        is_warmup_timeout = (
            vision_unavailable_error is not None
            and isinstance(e, vision_unavailable_error)
            and failure_code == "warmup_timeout"
        )
        if is_warmup_timeout and has_ocr:
            _dispatch_local_llm_alert("warmup_timeout")
            return None
        if failure_code == "local_snapshot_unavailable":
            vision_failure_code = "snapshot_missing"
        elif failure_code == "local_model_unavailable":
            vision_failure_code = "model_init_failed"
        elif failure_code == "cleanup_failed":
            vision_failure_code = "cleanup_failed"
        elif failure_code == "warmup_timeout":
            vision_failure_code = "warmup_timeout"
        else:
            vision_failure_code = "worker_unavailable"
        logger.warning(
            "vision_llm failed code=%s error_type=%s",
            vision_failure_code,
            type(e).__name__,
        )

    _remaining_image_budget(deadline)

    if not desc or not desc.strip():
        if vision_failure_code in {
            "snapshot_missing",
            "model_init_failed",
            "cleanup_failed",
            "warmup_timeout",
        }:
            _alert_local_llm_down(vision_failure_code)
        # 沒 vision 描述（多半本機 vision 模型載不進來）。不要只裸吐 OCR
        # （user: 要「根據 OCR 結果回應」，不是只做辨識）：先用本機 LLM 針對 OCR 內容生回應。
        if ocr_text:
            ocr_reply = (
                _respond_to_ocr_text(ocr_text, user_prompt=user_prompt)
                if user_prompt.strip() else _respond_to_ocr_text(ocr_text)
            )
            if ocr_reply:
                ocr_reply = _ensure_image_argument_structure(
                    ocr_reply, ocr_text=ocr_text
                )
                _remaining_image_budget(deadline)
                if use_default_cache:
                    _maybe_write_media_cache(image, group_id, "image", ocr_text, ocr_reply)
                return ocr_reply
            # No answer is available; do not expose OCR or a status receipt.
            return None
        return None

    # A successful visual result re-arms the once-per-outage notification.
    _reset_local_llm_alert_guard()

    # Layer 2：預設純本機實質回應。圖片內容與圖片摘要不外送；若未來需要
    # web-context 圖片查證，必須明確以 MEDIA_IMAGE_WEB_CONTEXT=1 opt in。
    import os as _os
    if _os.environ.get("MEDIA_IMAGE_WEB_CONTEXT") == "1" and _os.environ.get(
        "MEDIA_HYBRID_DISABLED"
    ) != "1":
        try:
            if _os.environ.get("MEDIA_PIPELINE_V4", "1") != "0":
                wrapped = _v4_news_style_pipeline(desc, ocr_text or "", user_prompt=user_prompt)
            else:
                wrapped = _wrap_with_gemini_news_style(desc, ocr_text or "", user_prompt=user_prompt)
            if wrapped:
                wrapped = _ensure_image_argument_structure(
                    wrapped, desc=desc, ocr_text=ocr_text or ""
                )
                _remaining_image_budget(deadline)
                if use_default_cache:
                    _maybe_write_media_cache(image, group_id, "image", desc, wrapped)
                return wrapped
        except Exception as e:
            logger.warning("hybrid pipeline failed, fallback to raw desc: %s", e)

    # Layer 3：只回傳依圖片產生的答案。
    _remaining_image_budget(deadline)
    return _ensure_image_argument_structure(desc, desc=desc, ocr_text=ocr_text or "")


def _v4_news_style_pipeline(
    desc: str, ocr_text: str = "", user_prompt: str = ""
) -> Optional[str]:
    """v4 完整 7 步 pipeline — 比 _wrap_with_gemini_news_style 豐富 3-5x。

    Step 1: vision_describe + OCR（caller 已給）
    Step 2: query expansion（本機 14B 生 6 個多樣 search query）
    Step 3: multi-source aggregate（DDG/GNews/Wiki × N，權威 domain 排序）
    Step 4: top-5 full text fetch（trafilatura 平行）
    Step 5: generate concise reply（本機 14B，查證材料作為內部依據）
    Step 6: grounding verify（grounding_local 4-signal）
    Step 7: self-critique refine（修正矛盾或無依據的主張）

    全步驟有 fallback；任一爆 → graceful 退到簡版。
    """
    # ── Step 2: Query expansion ──
    queries = []
    try:
        from finetune_query_expansion import expand_queries
        queries = expand_queries(desc, ocr_text, n=6)
        logger.info("v4 step 2: %d queries → %s", len(queries), queries[:3])
    except Exception as e:
        logger.warning("v4 step 2 expand_queries failed: %s", e)
        # fallback 用單一 query
        queries = [(desc[:80] + " " + ocr_text[:50]).strip()]

    # ── Step 3: Multi-source aggregate ──
    sources = []
    try:
        from source_aggregator import aggregate_sources
        sources = aggregate_sources(queries, total_max=18)
        logger.info(
            "v4 step 3: %d sources（top authority: %s）",
            len(sources),
            [s.get("domain") for s in sources[:3]],
        )
    except Exception as e:
        logger.warning("v4 step 3 aggregate failed: %s", e)
        return _wrap_with_gemini_news_style(desc, ocr_text, user_prompt=user_prompt)  # 退舊版

    # ── Step 4: Top-5 full text fetch ──
    rich_sources = sources
    try:
        from fulltext_fetcher import fetch_top_sources
        rich_sources = fetch_top_sources(sources, top_n=5, max_chars_per=2500)
        full_count = sum(1 for r in rich_sources if r.get("full_text"))
        logger.info("v4 step 4: %d / %d sources fetched full text", full_count, len(rich_sources))
    except Exception as e:
        logger.warning("v4 step 4 fetch_top failed: %s", e)

    # 拼 sources block
    sources_block = "\n\n".join(
        f"[{i+1}] {(r.get('title') or '')[:80]} ({r.get('domain', '?')}, "
        f"權威 {r.get('authority_score', 0)})\n"
        f"     URL: {r.get('url') or ''}\n"
        f"     {(r.get('full_text') or r.get('snippet') or '')[:2000]}"
        for i, r in enumerate(rich_sources[:10])
    )

    # ── Step 5: Generate rich reply ──
    user_msg = (
        f"LINE 群有人貼了一張圖。請用繁體中文簡潔回答，保留必要原因、限制與建議。\n\n"
        f"【使用者要求】\n{user_prompt.strip() or '請根據圖片直接回應'}\n\n"
        f"【圖片描述】\n{desc}\n"
        f"【OCR 文字】\n{ocr_text or '(無)'}\n\n"
        f"【查證材料】\n{sources_block or '(沒抓到 sources)'}\n\n"
        f"圖片描述、OCR 與查證材料只供內部理解，不附解析摘錄或內部推理。\n"
        f"直接給判斷，不湊論點或字數；只有使用者明確要求時才分正方／反方或列來源。\n"
        f"忠實依據查證材料，沒提的不要憑記憶補；矛盾只說明影響答案的部分。\n"
        f"若被要求來源，只能使用上面已提供且可核實的 URL，不編造。\n"
        f"不要流程說明、不回覆的理由或『希望對您有幫助』結尾。"
    )

    # 圖片 100% 本機 policy（user 2026-05-09）：跳過 Gemini，直接走本機 14B
    # 圖片內容 / 描述都不送雲端，包括 Step 5 reply generation
    reply = None
    logger.info("v4 step 5 圖片走本機 14B（skip Gemini per image policy）")
    try:
        from local_llm import chat as local_chat
        meibao_system = (
            "你是 LINE 群組對話助理咪寶。繁體中文、短句分行，直接給判斷與必要依據。"
            "保持簡潔易讀，保留原意，不設字數或論點數量下限。"
            "圖片解析、OCR 與推理只作內部上下文；不要輸出內部流程或不回覆的理由。"
            "只有使用者明確要求時才分正方／反方或列已提供且可核實的來源。"
        )
        reply = local_chat(user_msg, system_prompt=meibao_system + "\n" + IMAGE_RESPONSE_CONTRACT, max_tokens=1200)
        reply = reply.strip() if reply else None
    except Exception as e:
        logger.warning("v4 step 5 local_llm failed: %s", e)
        return None

    if not reply:
        return None

    # ── Step 6: Grounding verify ──
    try:
        import grounding_local
        source_texts = [
            (r.get("full_text") or r.get("snippet") or "") for r in rich_sources[:5]
        ]
        score = grounding_local.score_response(reply, source_texts)
        avg = score.get("score_avg", 1.0) if score else 1.0
        logger.info("v4 step 6 grounding score: %.2f", avg)
    except Exception as e:
        logger.info("v4 step 6 grounding skip: %s", e)

    # ── Step 7: Self-critique refine（圖片 100% 本機 → critique/refine 也用 14B，不打 Gemini）──
    import os as _os2
    old_force_local = _os2.environ.get("SELF_CRITIQUE_FORCE_LOCAL")
    _os2.environ["SELF_CRITIQUE_FORCE_LOCAL"] = "1"  # 給 self_critique 用的 hint
    try:
        from self_critique import critique_reply, refine_reply
        sources_for_critique = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "text": r.get("full_text") or r.get("snippet") or "",
            }
            for r in rich_sources[:5]
        ]
        critique = critique_reply(reply, sources_for_critique)
        n_factual_defects = sum(
            1 for c in critique.get("claims", [])
            if c.get("verdict") in {"contradicted", "unsupported"}
        )
        n_missing = len(critique.get("missing_facts", []))
        logger.info(
            "v4 step 7 critique: %d factual defects, %d missing facts",
            n_factual_defects, n_missing,
        )
        if n_factual_defects > 0:
            refined = refine_reply(
                reply, critique, sources_for_critique, user_prompt=user_prompt
            )
            if refined and refined.strip():
                logger.info("v4 step 7 refine applied")
                return refined.strip()
    except Exception as e:
        logger.info("v4 step 7 critique/refine skip: %s", e)
    finally:
        if old_force_local is None:
            _os2.environ.pop("SELF_CRITIQUE_FORCE_LOCAL", None)
        else:
            _os2.environ["SELF_CRITIQUE_FORCE_LOCAL"] = old_force_local

    return reply


def _wrap_with_gemini_news_style(
    desc: str, ocr_text: str = "", user_prompt: str = ""
) -> Optional[str]:
    """Opt-in web-context image wrapper; generation stays local-only."""
    # Step 1: 抽 topic 跑 web search（純本機爬蟲，多來源 8+ 筆）
    sources_block = ""
    sources_with_url = []  # 查證依據；需要引用時只用實際取得的 URL
    try:
        from web_scraper import search_duckduckgo, search_google_news, search_wiki_full
        topic = (desc[:80] + " " + ocr_text[:50]).strip()

        # DDG 5 筆
        try:
            for r in search_duckduckgo(topic, k=5):
                if r.get("title") and r.get("url"):
                    sources_with_url.append(r)
        except Exception as e:
            logger.info("DDG fail: %s", e)

        # Google News 5 筆
        try:
            for r in search_google_news(topic, k=5):
                if r.get("title") and r.get("url"):
                    sources_with_url.append(r)
        except Exception as e:
            logger.info("GoogleNews fail: %s", e)

        # Wiki（如可）
        try:
            wiki_q = topic.split()[0] if topic else None
            if wiki_q:
                wiki = search_wiki_full(wiki_q)
                if wiki and wiki.get("extract"):
                    sources_with_url.append({
                        "title": wiki.get("title", "Wikipedia"),
                        "url": wiki.get("url", "https://zh.wikipedia.org/"),
                        "snippet": wiki.get("extract", "")[:200],
                    })
        except Exception:
            pass

        # 去 dup（by url）
        seen = set()
        deduped = []
        for r in sources_with_url:
            u = r["url"]
            if u not in seen:
                seen.add(u)
                deduped.append(r)
        sources_with_url = deduped[:10]  # cap 10

        # 拼成 prompt block
        sources_block = "\n".join(
            f"[{i+1}] {r['title'][:80]}\n     URL: {r['url']}\n     {r.get('snippet','')[:200]}"
            for i, r in enumerate(sources_with_url)
        )
    except Exception as e:
        logger.info("web search skip: %s", e)

    # Step 2: 寫 reply — 圖片內容/摘要不送 Gemini，固定本機 14B。
    user_msg = (
        f"LINE 群有人貼了一張圖，請用咪寶風回覆。\n\n"
        f"【使用者要求】\n{user_prompt.strip() or '請根據圖片直接回應'}\n\n"
        f"【圖片描述】\n{desc}\n\n"
        f"【相關 sources（編號跟你引用對應，每條已含 URL）】\n{sources_block or '(沒抓到 sources)'}\n\n"
        f"圖片描述與查證材料只供內部理解，不輸出解析摘錄、內部推理或處理流程。\n"
        f"直接給判斷與必要原因、限制及建議，簡潔易讀，不湊論點或字數。\n"
        f"只有使用者明確要求時才分正方／反方或列來源。若列來源，只用上面已提供且可核實的 URL。\n"
        f"忠實依據查證材料，沒提的不要憑記憶補，不編造。\n"
        f"不要不回覆的理由或『希望對您有幫助』結尾。"
    )

    logger.info("image web-context wrapper uses local 14B only")
    try:
        from local_llm import chat as local_chat
        meibao_system = (
            "你是 LINE 群組對話助理咪寶。繁體中文、短句分行，簡潔回答並保留必要意思。"
            "只有使用者明確要求時才分正方／反方或列已提供且可核實的來源。"
            "圖片解析只作內部上下文；直接回答，不附解析內容。"
            "禁止「希望對您有幫助」結尾。"
        )
        out = local_chat(user_msg, system_prompt=meibao_system + "\n" + IMAGE_RESPONSE_CONTRACT, max_tokens=800)
        return render_image_reply(out)
    except Exception as e:
        logger.warning("local_llm wrap also failed: %s", e)
        return None


def analyze_video(
    video: str | Path | bytes,
    user_prompt: str = "",
    group_id: str | None = None,
) -> Optional[str]:
    """對影片產生回應。失敗回 None。

    流程：
      1. 抽 keyframes（max 6）
      2. 本機多圖 vision LLM 看
      3. 本機失敗 → None（沉默）

    Phase 1（media_cache）：caller 傳 group_id 時走 byte-exact cache dedup。
    """
    # Phase 1 media_cache lookup
    use_default_cache = not user_prompt.strip()
    cached = _maybe_lookup_media_cache(video, group_id, "video") if use_default_cache else None
    if cached:
        return cached

    # Keyframes
    frames = []
    try:
        from video_keyframes import extract_keyframes
        frames = extract_keyframes(video, max_frames=6)
        if not frames:
            logger.info("沒抽到 keyframes")
            return None
        logger.info("抽到 %d frames", len(frames))
    except ImportError:
        logger.info("video_keyframes 未建")
        return None
    except Exception as e:
        logger.warning("extract_keyframes failed: %s", e)
        return None

    prompt = (
        VIDEO_COMMENTARY_CONTRACT
        + f"\n你只有這段影片的 {len(frames)} 個抽樣關鍵畫面，沒有音訊或完整時序；不可推測口白、逐字稿、人物動機或未看到的事件。"
        + "\n使用者問題：" + (user_prompt or "請客觀、公正評論這段影片的內容。")
    )

    out: Optional[str] = None
    try:
        # 本機 vision LLM 多圖
        try:
            from vision_llm import chat_with_images
            out = chat_with_images(prompt, frames, max_tokens=600)
            if out and out.strip():
                logger.info("vision_llm chat_with_images 成功")
            else:
                out = None
                logger.info("本機 vision_llm 回空")
        except ImportError:
            logger.info("vision_llm 未建")
        except Exception as e:
            logger.warning("vision_llm chat_with_images failed: %s", e)
    finally:
        try:
            from video_keyframes import cleanup as _cleanup
            _cleanup(frames)
        except Exception:
            pass

    if use_default_cache and out and out.strip():
        _maybe_write_media_cache(video, group_id, "video", None, out)
    return out


if __name__ == "__main__":
    # smoke test：自造一張圖
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (400, 100), 'white')
    d = ImageDraw.Draw(img)
    # ASCII-only to avoid default-font CJK encoding issues across envs
    d.text((20, 30), "Test Image 123", fill='black')
    img.save('/tmp/media_test.jpg')

    print("\n>>> analyze_image('/tmp/media_test.jpg')")
    out = analyze_image('/tmp/media_test.jpg', user_prompt="這張圖在說什麼？")
    print(out or "(None — 模組未全部就緒)")
