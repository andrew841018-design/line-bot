"""PTT 熱門警示推播 — launchd 每小時觸發。

偵測 PTT 近 2 小時內 push_count >= 50 且標題含警示關鍵字的文章，
推播到 LINE 群組。已推過的 article_id 存 ptt_alert_state.json。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

_PROJECT_DEP = Path(__file__).parent.parent / "project" / "dependent_code"
sys.path.insert(0, str(_PROJECT_DEP))

import requests

GROUP_ID = os.environ.get("LINE_ALLOWED_GROUP_ID") or os.environ.get("ALLOWED_GROUP_ID", "")
TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

_PUSH_URL = "https://api.line.me/v2/bot/message/push"
_STATE_FILE = Path(__file__).parent / "ptt_alert_state.json"

_MIN_PUSH_COUNT = 50  # 低於此推文數不警示

_KEYWORDS = [
    "颱風", "颱風警報", "陸上警報", "海上警報",
    "地震", "規模", "震度",
    "食安", "食物中毒", "食品召回", "食品安全", "違法添加", "問題食品",
    "疫情", "確診", "新型病毒", "傳染病",
    "輻射", "輻射超標",
    "毒素", "中毒", "農藥超標",
    "警報", "緊急警告",
]


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:
        return {"pushed_ids": []}


def _save_state(state: dict) -> None:
    _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))


def _push(text: str) -> None:
    requests.post(
        _PUSH_URL,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        json={"to": GROUP_ID, "messages": [{"type": "text", "text": text[:5000]}]},
        timeout=10,
    )


def _fetch_ptt_alerts() -> list[dict]:
    try:
        from pg_helper import get_pg
        with get_pg() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT article_id, title, push_count, url
                    FROM articles
                    WHERE source_id = 1
                      AND scraped_at >= NOW() - INTERVAL '2 hours'
                      AND push_count >= %s
                    ORDER BY push_count DESC
                """, (_MIN_PUSH_COUNT,))
                rows = cur.fetchall()
    except Exception as e:
        print(f"DB 查詢失敗: {e}")
        return []

    alerts = []
    for article_id, title, push_count, url in rows:
        title_str = title or ""
        if not any(kw in title_str for kw in _KEYWORDS):
            continue
        text = (
            f"📢 PTT 熱門警示\n"
            f"【{title_str}】\n"
            f"推文數：{push_count}\n"
            f"{url}"
        )
        alerts.append({"id": str(article_id), "text": text})
    return alerts


def main() -> None:
    if not GROUP_ID or not TOKEN:
        print("ERR: LINE_ALLOWED_GROUP_ID or LINE_CHANNEL_ACCESS_TOKEN not set")
        return

    state = _load_state()
    pushed_ids: list[str] = state.get("pushed_ids", [])
    new_pushed: list[str] = []

    alerts = _fetch_ptt_alerts()

    for alert in alerts:
        if alert["id"] in pushed_ids:
            continue
        _push(alert["text"])
        new_pushed.append(alert["id"])
        print(f"推播 PTT 警示：{alert['text'][:60]}")

    if new_pushed:
        all_ids = (pushed_ids + new_pushed)[-500:]
        _save_state({"pushed_ids": all_ids})
    else:
        print("無新 PTT 警示")


if __name__ == "__main__":
    main()
