"""
LINE → FastAPI webhook → Gemini → LINE

觸發條件（Q2）：
- 必須是 group 事件
- 必須是 TextMessage
- 必須 @mention 本 bot（linebot-sdk 會在 event.message.mention 標出 mentionees）

指令（Q2 記憶系統）：
- /看記憶          → 列出所有事實
- /記住 <內容>     → 手動新增事實
- /忘記 <關鍵字>   → 刪除包含關鍵字的所有事實
- /清除記憶        → 全砍
- /group_id        → 回覆本群 ID（給使用者抓來鎖定用）
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Header, HTTPException, Request
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent

import gemini_client
import memory
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("line_bot")

app = FastAPI()

_parser = WebhookParser(settings.line_channel_secret)
_line_config = Configuration(access_token=settings.line_channel_access_token)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "gemini_model": settings.gemini_model,
        "group_locked": bool(settings.allowed_group_id),
    }


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.post("/callback")
async def callback(request: Request, x_line_signature: str = Header(None)):
    body = (await request.body()).decode("utf-8")
    try:
        events = _parser.parse(body, x_line_signature)
    except InvalidSignatureError:
        logger.warning("invalid signature")
        raise HTTPException(status_code=400, detail="invalid signature")

    for event in events:
        try:
            _handle_event(event)
        except Exception as e:
            logger.exception("handle_event failed: %s", e)
    return {"ok": True}


def _handle_event(event) -> None:
    # 只處理群組裡的文字訊息
    if not isinstance(event, MessageEvent):
        return
    if not isinstance(event.message, TextMessageContent):
        return
    if not isinstance(event.source, GroupSource):
        return

    group_id = event.source.group_id

    # Q6=B：鎖定單一群組
    if not settings.allowed_group_id:
        logger.info("ALLOWED_GROUP_ID not set yet; observed group_id=%s", group_id)
        # 尚未鎖定，允許 /group_id 指令讓使用者抓 id
        if event.message.text.strip() == "/group_id":
            _reply(event.reply_token, f"本群 group_id：\n{group_id}\n\n把這個值填進 .env 的 ALLOWED_GROUP_ID 再重啟我就會鎖定此群。")
        return
    if group_id != settings.allowed_group_id:
        logger.info("ignoring message from non-allowed group_id=%s", group_id)
        return

    text = event.message.text or ""

    # 指令處理（指令不需要 @mention 也能用，方便管理）
    cmd_reply = _handle_command(group_id, text)
    if cmd_reply is not None:
        _reply(event.reply_token, cmd_reply)
        return

    # 非指令：必須被 @mention 才回
    if not _is_mentioned(event.message):
        return

    # 去掉 @bot_name 的部分，只留下使用者真正問的內容
    clean_text = _strip_mentions(event.message).strip()
    if not clean_text:
        _reply(event.reply_token, "叫我幹嘛？")
        return

    # 丟給 Gemini
    context = memory.get_context(group_id)
    facts = memory.top_facts(group_id)
    reply_text = gemini_client.chat(clean_text, context, facts)

    memory.append_turn(group_id, "user", clean_text)
    memory.append_turn(group_id, "bot", reply_text)

    # 每 N 輪抽一次長期事實
    if memory.bump_and_should_extract(group_id):
        new_facts = gemini_client.extract_facts(memory.get_context(group_id))
        added = 0
        for f in new_facts:
            if memory.add_fact(group_id, f):
                added += 1
        logger.info("auto-extracted facts: %d new (total=%d)", added, len(memory.list_facts(group_id)))

    _reply(event.reply_token, reply_text)


# ── Command 處理 ──────────────────────────────────────────────────────────────

def _handle_command(group_id: str, text: str) -> str | None:
    """有對應到指令回 str；沒有回 None。"""
    t = text.strip()
    if t == "/group_id":
        return f"本群 group_id：\n{group_id}"

    if t == "/看記憶":
        facts = memory.list_facts(group_id)
        if not facts:
            return "目前沒有任何記憶。要讓我記住什麼，用：\n/記住 <內容>"
        return "目前的記憶：\n" + "\n".join(f"• {f}" for f in facts)

    if t.startswith("/記住 "):
        fact = t[len("/記住 "):].strip()
        if not fact:
            return "用法：/記住 <要記住的內容>"
        if memory.add_fact(group_id, fact):
            return f"好，記住了：{fact}"
        return f"這條已經在記憶裡了：{fact}"

    if t.startswith("/忘記 "):
        keyword = t[len("/忘記 "):].strip()
        if not keyword:
            return "用法：/忘記 <關鍵字>"
        n = memory.remove_fact(group_id, keyword)
        return f"刪除了 {n} 條含「{keyword}」的記憶。" if n else f"沒有找到含「{keyword}」的記憶。"

    if t == "/清除記憶":
        n = memory.clear_facts(group_id)
        return f"已清除 {n} 條記憶。"

    return None


# ── LINE SDK helpers ──────────────────────────────────────────────────────────

def _is_mentioned(message: TextMessageContent) -> bool:
    """檢查這則訊息是否 @mention 了本 bot。
    LINE 的 mention 結構：message.mention.mentionees[i].is_self == True 代表 mention 到我。"""
    mention = getattr(message, "mention", None)
    if mention is None:
        return False
    mentionees = getattr(mention, "mentionees", None) or []
    for m in mentionees:
        if getattr(m, "is_self", False):
            return True
    return False


def _strip_mentions(message: TextMessageContent) -> str:
    """把訊息裡所有 @mention 的子字串挖掉，只留真正的問題。"""
    text = message.text or ""
    mention = getattr(message, "mention", None)
    if mention is None:
        return text
    mentionees = getattr(mention, "mentionees", None) or []
    # 從後往前刪，避免 index 位移
    ranges = sorted(
        [(m.index, m.index + m.length) for m in mentionees],
        key=lambda x: x[0],
        reverse=True,
    )
    for start, end in ranges:
        text = text[:start] + text[end:]
    return text


def _reply(reply_token: str, text: str) -> None:
    # LINE 單則訊息上限 5000 字
    text = text[:4900]
    with ApiClient(_line_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )
