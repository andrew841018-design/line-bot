"""
每日回饋收集系統（Layer 4）。

流程：
1. 推播時間 launchd 推播問題（feedback_push.py）
2. 推播時間 ~ 02:00 TW：webhook 收到文字訊息時呼叫 collect_message()
   → 存入 pending_feedback.json，推播後 1h 內 weight=2，之後 weight=1
3. 02:00 launchd 呼叫 process_feedback.py 嘗試掃描
   → Gemini 有 quota：掃描 → 更新 persona → 推播改進摘要 → 清空 json
   → Gemini 429：不清空，等 15:00 health_check 再試
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_TW_TZ = ZoneInfo("Asia/Taipei")
_BASE_DIR = Path(__file__).parent
_STATE_FILE = _BASE_DIR / "feedback_state.json"    # {"push_ts": float}
_PENDING_FILE = _BASE_DIR / "pending_feedback.json" # [{text, user_id, ts, weight}, ...]

_WINDOW_END_HOUR = 2      # 02:00 TW 停止收集
_HIGH_WEIGHT_SECS = 3600  # 推播後 1 小時內 weight=2


def in_feedback_window() -> bool:
    """判斷現在是否在「推播時間 ~ 隔天 02:00 TW」收集窗口。

    窗口開始由 push_ts 決定（不寫死），窗口結束固定為推播後首個 02:00 TW。
    """
    from datetime import timedelta

    push_ts = _get_push_ts()
    if push_ts == 0.0:
        return False

    now = time.time()
    if now < push_ts:
        return False

    push_dt = datetime.fromtimestamp(push_ts, tz=_TW_TZ)
    # 推播時 hour < 2 → 窗口結束在同一天 02:00；否則在隔天 02:00
    if push_dt.hour < _WINDOW_END_HOUR:
        end_dt = push_dt.replace(hour=_WINDOW_END_HOUR, minute=0, second=0, microsecond=0)
    else:
        end_dt = (push_dt + timedelta(days=1)).replace(
            hour=_WINDOW_END_HOUR, minute=0, second=0, microsecond=0
        )

    return now < end_dt.timestamp()


def record_push_time() -> None:
    """推播問題後呼叫，記錄時間戳供 weight 計算。"""
    _STATE_FILE.write_text(json.dumps({"push_ts": time.time()}))
    logger.info("[Feedback] push time recorded")


def _get_push_ts() -> float:
    try:
        return float(json.loads(_STATE_FILE.read_text()).get("push_ts", 0))
    except Exception:
        return 0.0


def collect_message(user_id: str, text: str) -> None:
    """把一則文字訊息存入 pending_feedback.json。"""
    if not text.strip():
        return
    push_ts = _get_push_ts()
    now = time.time()
    weight = 2 if (push_ts > 0 and now - push_ts <= _HIGH_WEIGHT_SECS) else 1

    pending = _load_pending()
    pending.append({
        "text": text.strip(),
        "user_id": user_id,
        "ts": now,
        "weight": weight,
    })
    _PENDING_FILE.write_text(json.dumps(pending, ensure_ascii=False, indent=2))
    logger.info("[Feedback] collected weight=%d text=%r", weight, text[:50])


def load_pending() -> list[dict]:
    return _load_pending()


def clear_pending() -> None:
    _PENDING_FILE.write_text("[]")
    logger.info("[Feedback] pending_feedback.json cleared")


def _load_pending() -> list[dict]:
    try:
        data = json.loads(_PENDING_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []
