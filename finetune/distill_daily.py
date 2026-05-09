"""每日 Gemini distillation — 累積 conversation pair 為將來 fine-tune 用。

Daily 跑一次：
  1. 讀近 30 天 raw_messages 中沒對應 bot reply 的 user message
  2. Sample 10 條（避免重複，已 distill 過的 skip）
  3. 對每條 call Gemini light model 生成 ideal reply
  4. Append 到 finetune/data/distilled.jsonl

Quota safety：
  - 檢查 main._quota_exhausted()
  - 每日 max 10 calls
  - distilled_seen.json 記已處理 message_id 避免重複
"""
from __future__ import annotations
import argparse
import json
import logging
import sqlite3
import time
import sys
from pathlib import Path
from datetime import datetime, timedelta

# 加 line_bot/ 進 path
sys.path.insert(0, str(Path(__file__).parent.parent))

LINE_BOT = Path(__file__).parent.parent
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = DATA_DIR / "distilled.jsonl"
SEEN_FILE = DATA_DIR / "distilled_seen.json"
DAILY_LIMIT = 10
LOOKBACK_DAYS = 30

logger = logging.getLogger("distill_daily")

DISTILL_PROMPT = """你是個 LINE 群組助理「咪寶」。下面是 user 在群裡的訊息。
請以咪寶的人設（溫柔、簡短、繁中、有觀點）回應這條訊息。
若訊息不需要回（純情緒、純閒聊、bot 不需介入），回 "[SKIP]"。

User 訊息：
{user_text}

咪寶回應："""


def _load_seen() -> set:
    if not SEEN_FILE.exists():
        return set()
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except Exception:
        return set()


def _save_seen(s: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(s)))


def _quota_safe() -> bool:
    """確認 Gemini quota 沒爆。"""
    try:
        import main
        if main._quota_exhausted():
            logger.info("Gemini quota exhausted, skip today")
            return False
    except Exception:
        pass
    return True


def _sample_unprocessed_messages(seen: set, limit: int = 10) -> list[dict]:
    """從 raw_messages 抽近 30 天、user message、未 distill 過的，sample limit 條。"""
    db = LINE_BOT / "line_bot.db"
    if not db.exists():
        return []
    cutoff = int((datetime.now() - timedelta(days=LOOKBACK_DAYS)).timestamp())
    con = sqlite3.connect(str(db))
    rows = con.execute("""
        SELECT message_id, group_id, user_id, text
        FROM raw_messages
        WHERE created_at > ? AND user_id != '__bot__' AND length(text) >= 10
        ORDER BY created_at DESC
        LIMIT 200
    """, (cutoff,)).fetchall()
    con.close()

    candidates = [
        {"message_id": r[0], "group_id": r[1], "user_id": r[2], "text": r[3]}
        for r in rows if r[0] not in seen
    ]
    # 取最新 limit 條
    return candidates[:limit]


def _distill_one(text: str, mock: bool = False) -> str | None:
    """Call Gemini light → 生成 ideal reply。mock=True 時純 echo。"""
    if mock:
        return f"[MOCK reply for: {text[:30]}...]"
    try:
        import gemini_client
        from google.genai import types
        prompt = DISTILL_PROMPT.format(user_text=text)
        response = gemini_client._client.models.generate_content(
            model=gemini_client.settings.gemini_light_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        reply = (response.text or "").strip()
        if reply.startswith("[SKIP]") or len(reply) < 5:
            return None
        return reply
    except Exception as e:
        logger.warning("distill failed: %s", e)
        return None


def run_daily(mock: bool = False) -> int:
    """執行 daily distill。回新增 pair 數量。"""
    if not mock and not _quota_safe():
        return 0

    seen = _load_seen()
    samples = _sample_unprocessed_messages(seen, limit=DAILY_LIMIT)
    if not samples:
        logger.info("沒有 unprocessed messages")
        return 0

    pairs_added = 0
    with OUTPUT_FILE.open("a", encoding="utf-8") as f:
        for s in samples:
            reply = _distill_one(s["text"], mock=mock)
            if not reply:
                seen.add(s["message_id"])  # 失敗 / SKIP 也標記，不重試
                continue
            pair = {
                "messages": [
                    {"role": "user", "content": s["text"]},
                    {"role": "assistant", "content": reply},
                ],
                "metadata": {
                    "message_id": s["message_id"],
                    "group_id": s["group_id"],
                    "user_id": s["user_id"],
                    "distilled_at": int(time.time()),
                    "mock": mock,
                },
            }
            f.write(json.dumps(pair, ensure_ascii=False) + "\n")
            seen.add(s["message_id"])
            pairs_added += 1

    _save_seen(seen)

    # 統計累積
    total = sum(1 for _ in OUTPUT_FILE.open()) if OUTPUT_FILE.exists() else 0
    logger.info("today added %d pairs, total %d", pairs_added, total)
    return pairs_added


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="不真 call Gemini")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    n = run_daily(mock=args.mock)
    print(f"distilled {n} pairs today")
