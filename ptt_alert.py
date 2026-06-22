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

from line_push_client import line_access_token, try_push_text  # noqa: E402

GROUP_ID = os.environ.get("LINE_ALLOWED_GROUP_ID") or os.environ.get(
    "ALLOWED_GROUP_ID", ""
)

_STATE_FILE = Path(__file__).parent / "ptt_alert_state.json"

_MIN_PUSH_COUNT = 50  # 低於此推文數不警示

_KEYWORDS = [
    "颱風",
    "颱風警報",
    "陸上警報",
    "海上警報",
    "地震",
    "規模",
    "震度",
    "食安",
    "食物中毒",
    "食品召回",
    "食品安全",
    "違法添加",
    "問題食品",
    "疫情",
    "確診",
    "新型病毒",
    "傳染病",
    "輻射",
    "輻射超標",
    "毒素",
    "中毒",
    "農藥超標",
    "警報",
    "緊急警告",
]


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:
        return {"pushed_ids": []}


def _save_state(state: dict) -> None:
    _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))


def _push(text: str) -> bool:
    return try_push_text(GROUP_ID, text, timeout=10)


def _fetch_ptt_alerts() -> list[dict]:
    try:
        from pg_helper import get_pg

        with get_pg() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT article_id, title, push_count, url
                    FROM articles
                    WHERE source_id = 1
                      AND scraped_at >= NOW() - INTERVAL '2 hours'
                      AND push_count >= %s
                    ORDER BY push_count DESC
                """,
                    (_MIN_PUSH_COUNT,),
                )
                rows = cur.fetchall()
    except Exception as e:
        print(f"DB 查詢失敗: {e}")
        return []

    alerts = []
    for article_id, title, push_count, url in rows:
        title_str = title or ""
        if not any(kw in title_str for kw in _KEYWORDS):
            continue
        text = f"📢 PTT 熱門警示\n【{title_str}】\n推文數：{push_count}\n{url}"
        alerts.append({"id": str(article_id), "text": text})
    return alerts


def main() -> int:
    if not GROUP_ID or not line_access_token():
        print("ERR: LINE_ALLOWED_GROUP_ID or LINE_CHANNEL_ACCESS_TOKEN not set")
        return 1

    state = _load_state()
    pushed_ids: list[str] = state.get("pushed_ids", [])
    new_pushed: list[str] = []
    push_failed = False

    alerts = _fetch_ptt_alerts()

    for alert in alerts:
        if alert["id"] in pushed_ids:
            continue
        if _push(alert["text"]):
            new_pushed.append(alert["id"])
            print(f"推播 PTT 警示：{alert['text'][:60]}")
        else:
            push_failed = True
            print(f"推播 PTT 警示失敗，保留待下次重試：{alert['text'][:60]}", file=sys.stderr)

    if new_pushed:
        all_ids = (pushed_ids + new_pushed)[-500:]
        _save_state({"pushed_ids": all_ids})
    else:
        print("無新 PTT 警示")
    return 1 if push_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
