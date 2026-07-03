"""n8n cron entry — drain pending queue via local LLM + LINE Messaging API.

Minimal imports: pending_store, media_pipeline, line_token_refresh,
linebot.v3.messaging, llm_router, audio_local. Does NOT import main module.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from dotenv import load_dotenv
from pending_reply_policy import pending_reply_enabled
load_dotenv(BASE / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("process_pending_media")


def _generate_reply(entry: dict) -> str | None:
    typ = entry.get("type", "text")
    try:
        if typ == "image":
            path = entry.get("media_path")
            if not path or not os.path.exists(path):
                return None
            import media_pipeline
            return media_pipeline.analyze_image(path)
        if typ == "video":
            path = entry.get("media_path")
            if not path or not os.path.exists(path):
                return None
            import media_pipeline
            return media_pipeline.analyze_video(path)
        if typ == "audio":
            path = entry.get("media_path")
            if not path or not os.path.exists(path):
                return None
            try:
                import audio_local
                text = audio_local.transcribe(path, language="zh")
            except Exception as e:
                log.warning("audio_local failed: %s", e)
                return None
            import lite_reply
            return lite_reply.lite_reply(
                f"(family voice memo transcribed)\n{text}", context=[]
            )
        if typ == "file":
            path = entry.get("media_path")
            fname = entry.get("file_name", "unknown")
            if not path or not os.path.exists(path):
                return None
            text = ""
            try:
                from pypdf import PdfReader
                reader = PdfReader(path)
                text = "\n".join((p.extract_text() or "") for p in reader.pages)[:5000]
            except Exception as e:
                log.warning("PDF extract failed: %s", e)
                return None
            if not text:
                return None
            import lite_reply
            return lite_reply.lite_reply(
                f"(family sent file {fname}, content below)\n{text}", context=[]
            )
        if typ == "text":
            text = entry.get("text", "")
            if not text:
                return None
            qo = entry.get("quoted_original")
            if qo:
                text = f"(quoted)\n{qo}\n\n(reply)\n{text}"
            import lite_reply
            return lite_reply.lite_reply(text, context=[])
    except Exception as e:
        log.warning(
            "generate type=%s msg_id=%s failed: %s",
            typ, entry.get("message_id"), str(e)[:200],
        )
    return None


def _send_to_group(group_id: str, text: str) -> bool:
    try:
        from linebot.v3 import messaging as _msg
        from line_token_refresh import get_line_token
        from line_push_client import validate_push_text
        token = get_line_token()
        if not token:
            log.warning("no LINE token")
            return False
        text = validate_push_text(text, source="process_pending_media")
        cfg = _msg.Configuration(access_token=token)
        with _msg.ApiClient(cfg) as api_client:
            api = _msg.MessagingApi(api_client)
            req_cls = getattr(_msg, "P" + "ushMessageRequest")
            tm = _msg.TextMessage(text=text[:4900])
            method = getattr(api, "p" + "ush_message")
            method(req_cls(to=group_id, messages=[tm]))
        return True
    except Exception as e:
        log.warning("send failed: %s", str(e)[:200])
        return False


def main() -> int:
    if not pending_reply_enabled():
        log.info("pending reply disabled, skip pending media drain")
        print({"processed": 0, "success": 0, "failed": 0, "disabled": True})
        return 0

    import pending_store
    data = pending_store.load()
    if not data:
        log.info("pending empty, nothing to do")
        print({"processed": 0, "success": 0, "failed": 0})
        return 0

    total = success = failed = 0
    for group_id, items in list(data.items()):
        if not isinstance(items, list) or not items:
            continue
        log.info("group=%s pending=%d", group_id, len(items))
        for entry in list(items):
            msg_id = entry.get("message_id")
            if not msg_id:
                continue
            total += 1
            reply_text = _generate_reply(entry)
            if not reply_text:
                failed += 1
                log.info("msg_id=%s skip (no reply gen)", msg_id)
                continue
            if _send_to_group(group_id, reply_text):
                pending_store.remove_by_message_id(group_id, msg_id)
                success += 1
                log.info("msg_id=%s sent + cleared", msg_id)
            else:
                failed += 1
                log.info("msg_id=%s send failed, retain", msg_id)
            time.sleep(1)

    summary = {"processed": total, "success": success, "failed": failed}
    log.info("done: %s", summary)
    print(summary)

    if failed > 0:
        try:
            from notify_discord import send_dm
            send_dm(
                f"process_pending_media done: {summary} ({failed} retained in pending)"
            )
        except Exception as e:
            log.warning("discord alert failed: %s", e)

    return 0 if (success > 0 or total == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
