"""Monthly highlight — 每月 28 號 20:00 launchd 跑.

掃過去 30 天 family group raw_messages，Gemini 抽月度 highlight
(討論最熱話題 / 人物頻次 / 情緒詞頻)，推到 LINE 群.

Andrew 2026-05-25 directive (Feature C).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402

import memory  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

GROUP_ID = os.environ.get("LINE_ALLOWED_GROUP_ID") or os.environ.get(
    "ALLOWED_GROUP_ID", ""
)
_PUSH_URL = "https://api.line.me/v2/bot/message/push"
THIRTY_DAYS_SEC = 30 * 24 * 3600
MIN_MESSAGES_FOR_HIGHLIGHT = 20
MAX_MESSAGES_IN_PROMPT = 1500


def _get_token() -> str:
    try:
        import line_token_refresh
        return line_token_refresh.get_line_token() or os.environ.get(
            "LINE_CHANNEL_ACCESS_TOKEN", ""
        )
    except Exception:
        return os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")


def _push(text: str) -> bool:
    token = _get_token()
    if not token or not GROUP_ID:
        logger.error("missing TOKEN or GROUP_ID; skip push")
        return False
    try:
        resp = requests.post(
            _PUSH_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"to": GROUP_ID, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        logger.warning("LINE push failed %d: %s", resp.status_code, resp.text[:300])
        return False
    except Exception as e:
        logger.warning("LINE push exception: %s", e)
        return False


def fetch_last_30d_messages(group_id: str) -> list[tuple[str, str, int]]:
    """Returns [(user_id, text, created_at), ...] for last 30 days messages."""
    since_ts = int((time.time() - THIRTY_DAYS_SEC) * 1000)
    with memory._conn() as c:
        rows = c.execute(
            "SELECT user_id, text, created_at FROM raw_messages "
            "WHERE group_id = ? AND created_at >= ? "
            "ORDER BY created_at ASC",
            (group_id, since_ts),
        ).fetchall()
    return [(r[0] or "unknown", r[1] or "", r[2]) for r in rows]


def summarize_monthly_via_gemini(messages: list[tuple[str, str, int]]) -> str | None:
    """用 Gemini 生月度 highlight (categorized). 失敗回 None."""
    if not messages:
        return None
    from google.genai import types
    import gemini_client

    lines = []
    for uid, text, _ts in messages[:MAX_MESSAGES_IN_PROMPT]:
        short_uid = (uid or "unknown")[-6:]
        lines.append(f"[{short_uid}] {text[:200]}")
    dialogue = "\n".join(lines)

    prompt = (
        f"以下是家族 LINE 群過去 30 天的對話（共 {len(messages)} 則）。"
        "請整理月度 highlight，三大 section：\n\n"
        "**A. 本月討論最熱話題（5-8 條）** — 反覆出現、引發多人回應的主題\n"
        "**B. 人物頻次 + 動態** — 誰最常出現 / 誰心情變化大 / 誰新進入話題\n"
        "**C. 情緒詞頻** — 月度心情關鍵字 (e.g. 開心/煩/緊張/累)、出現次數\n\n"
        "格式（純文字、不要 markdown）：\n"
        "📅 本月家族月報\n\n"
        "A. 熱門話題\n"
        "1. ...\n"
        "2. ...\n\n"
        "B. 人物動態\n"
        "- 媽媽：xxx\n"
        "- 妹妹：xxx\n\n"
        "C. 情緒詞頻\n"
        "開心 ×12 / 煩 ×7 / 累 ×5\n\n"
        f"【對話】\n{dialogue}\n"
    )
    try:
        resp = gemini_client._client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                temperature=0.3,
            ),
        )
        return (resp.text or "").strip()
    except Exception as e:
        logger.warning("gemini monthly summarize failed: %s", e)
        return None


def main() -> int:
    if not GROUP_ID:
        logger.error("GROUP_ID 未設定")
        return 1

    messages = fetch_last_30d_messages(GROUP_ID)
    logger.info("fetched %d messages for last 30d", len(messages))
    if len(messages) < MIN_MESSAGES_FOR_HIGHLIGHT:
        logger.info(
            "too few messages (%d < %d), skip monthly highlight",
            len(messages), MIN_MESSAGES_FOR_HIGHLIGHT,
        )
        return 0

    summary = summarize_monthly_via_gemini(messages)
    if not summary:
        logger.warning("monthly summarize failed, skip push")
        return 1

    if _push(summary):
        logger.info("monthly highlight pushed (len=%d)", len(summary))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
