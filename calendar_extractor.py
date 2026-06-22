"""家族行事曆事件抽取器。

每次 burst flush 時呼叫一次，吃合併後的對話文字，回 JSON：

  {
    "has_event": bool,                 # 是否包含一個新「家族活動」
    "title": str,                      # "全家聚餐" / "去花蓮玩"
    "date": "YYYY-MM-DD" | null,       # 解析出的日期；null = 沒講
    "time": "HH:MM" | null,            # 24h；null = 沒講
    "location": str | null,
    "participants": [str, ...],        # 從稱謂抽：媽媽 / 爸爸 / 姊姊 / 妹妹 / 弟弟 / 全家
    "is_cancellation": bool,           # 是不是在取消／改期已存在的活動
    "cancel_target_keyword": str | null  # 取消時用來找原 event 的關鍵字
  }

設計：
- 只認家族「實體聚會」（聚餐、出遊、生日趴、就醫陪同…），不抓工作排程、純對話
- 模糊日期（「下週六」「明天」）就地用今天日期換算成 YYYY-MM-DD
- 失敗一律回 has_event=false，**不擋** burst 主流程
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from google.genai import types

import gemini_client
from config import settings

logger = logging.getLogger(__name__)
_TW = ZoneInfo("Asia/Taipei")


_PROMPT = """你是家族 LINE 群行事曆助手。從下面這段對話判斷有沒有家族活動需要記下。

抓「家族 / 個人重要事件」三類：
1. family_gathering — 家族實體聚會：聚餐、出遊、生日趴、回老家、接送、婚禮、滿月
2. personal_trip   — 個人旅程：媽媽/爸爸 回台北/北上/南下/出差/搭高鐵
3. medical         — 醫療事件：做胃鏡/大腸鏡/健檢、看醫生/牙醫、陪同就醫、打疫苗、抽血、回診

**不要**抓：純閒聊、工作會議、新聞、網購、抽象計畫（「以後想去」）、純感想（「做胃鏡很可怕」）。

今天是 {today}（週{weekday}）。

【對話】
{dialogue}

任務：
1. 有沒有人在「邀約 / 約定 / 規劃」一個具體事件？→ has_event=true
2. 有沒有人在「取消 / 改期 / 不去了」之前約好的事件？→ is_cancellation=true
3. 兩者都不是 → has_event=false, is_cancellation=false

抽欄位（無就 null） — 目標「人事時地物」五元素完整：
- title：12 字內，**含動詞 + 物 / 主角**，越具體越好：
    ✅「拿爸爸生日蛋糕」「領楊偉勛處方簽」「全家媽媽生日聚餐」「媽媽回台北做胃鏡」
    ❌「拿蛋糕」「領藥」「聚餐」「回台北」(都太空泛)
- event_type：三選一 — "family_gathering" / "personal_trip" / "medical"
- date：YYYY-MM-DD。模糊詞要換成今日換算後的日期：
    今天=今天日期；明天=+1；後天=+2；下週X=下個週X；本週X=本週X
- time：24h 格式 HH:MM；下午6點→18:00；晚上8點→20:00
- location：餐廳/地點/醫院 + 分店名（**完整地點**），例「喜來登日本料理」「台大醫院東址1樓」「徐卅路地中海料理」
- participants：**含角色標記**：
    ✅ ["爸爸(壽星)", "媽媽(陪同)", "全家"]
    ✅ ["媽媽(就醫)", "黃將修(陪同)"]
    ✅ ["爸爸(取者)"]
    ❌ ["爸爸"] (只列名不夠，要說明扮演什麼角色)
- cancel_target_keyword：取消時要用來找原 event 的關鍵字（活動標題裡可能出現的字）

只回 JSON，不要 markdown：
{{"has_event": false, "is_cancellation": false, "title": null, "event_type": "family_gathering", "date": null, "time": null, "location": null, "participants": [], "cancel_target_keyword": null}}
"""


def _today_tw() -> tuple[str, str]:
    now = datetime.now(tz=_TW)
    weekday = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
    return now.strftime("%Y-%m-%d"), weekday


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s


def extract(combined_text: str) -> dict:
    """回 dict（必含 has_event / is_cancellation 兩 key）。失敗回 has_event=false。"""
    fail: dict = {
        "has_event": False,
        "is_cancellation": False,
        "title": None,
        "date": None,
        "time": None,
        "location": None,
        "participants": [],
        "cancel_target_keyword": None,
        "event_type": "family_gathering",
    }
    if not combined_text or not combined_text.strip():
        return fail

    today, weekday = _today_tw()
    prompt = _PROMPT.format(today=today, weekday=weekday, dialogue=combined_text[:2000])

    for model_name in (settings.gemini_light_model, "gemini-2.5-flash"):
        try:
            resp = gemini_client._client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                    temperature=0.1,
                ),
            )
            text = _strip_code_fence(resp.text or "")
            data = json.loads(text)
            return _normalize(data)
        except Exception as e:
            err = str(e)
            logger.warning("calendar extract failed (%s): %s", model_name, err[:200])
            # 兩個 model 都試完才走 regex fallback；不論 429 or 非 429 都 try 下個。
            continue

    # Gemini 兩 model 都 fail → 純 regex fallback（family keyword whitelist 防誤判）
    try:
        import calendar_regex
        today_tw_date = datetime.now(_TW).date()
        regex_result = calendar_regex.extract_regex_only(combined_text, today_tw_date)
        if regex_result["has_event"]:
            logger.info(
                "calendar regex fallback hit: title=%r date=%s time=%s",
                regex_result["title"],
                regex_result["date"],
                regex_result["time"],
            )
            return regex_result
    except Exception as e:
        logger.warning("calendar regex fallback failed: %s", e)
    return fail


def _event_key(ev: dict) -> tuple[str, str, str, str]:
    if ev.get("date") and ev.get("time"):
        return (
            str(ev.get("date") or ""),
            str(ev.get("time") or ""),
            "",
            "",
        )
    return (
        str(ev.get("date") or ""),
        str(ev.get("time") or ""),
        str(ev.get("title") or ""),
        str(ev.get("location") or ""),
    )


def _is_insertable_event(ev: dict) -> bool:
    return bool(ev.get("has_event") and ev.get("title") and ev.get("date"))


def extract_many(combined_text: str, primary: dict | None = None) -> dict:
    """Return cancellation metadata plus all insertable events found in text.

    Backward compatibility: `extract()` remains the source of the model-backed
    single-event result. This wrapper adds a deterministic explicit-date
    fallback so a message like "7/4 ... 以及 7/11 ..." persists both events
    even if the model result only contains the first one.
    """
    first = primary if primary is not None else extract(combined_text)
    if first.get("is_cancellation"):
        return {
            "is_cancellation": True,
            "cancel_target_keyword": first.get("cancel_target_keyword"),
            "date": first.get("date"),
            "time": first.get("time"),
            "events": [],
        }

    events: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()

    def _add(ev: dict) -> None:
        if not _is_insertable_event(ev):
            return
        key = _event_key(ev)
        if key in seen:
            return
        seen.add(key)
        events.append(ev)

    _add(first)

    try:
        import calendar_regex
        today_tw_date = datetime.now(_TW).date()
        regex_events = calendar_regex.extract_many_regex_only(
            combined_text, today_tw_date, require_time=False
        )
        if events and len(regex_events) <= 1:
            regex_events = []
        for ev in regex_events:
            _add(_normalize(ev))
    except Exception as e:
        logger.warning("calendar multi regex fallback failed: %s", e)

    return {
        "is_cancellation": False,
        "cancel_target_keyword": None,
        "date": None,
        "time": None,
        "events": events,
    }


def _normalize(data: dict) -> dict:
    has = bool(data.get("has_event"))
    cancel = bool(data.get("is_cancellation"))
    parts = data.get("participants") or []
    if not isinstance(parts, list):
        parts = []
    parts = [str(p)[:20] for p in parts if p]

    def _s(k: str, max_len: int = 80) -> str | None:
        v = data.get(k)
        if v is None:
            return None
        s = str(v).strip()
        return s[:max_len] if s else None

    # event_type whitelist — import calendar_db.EVENT_TYPES 統一 source of truth
    # (GP2 Phase 6 反饋：避免 5 處 drift)
    from calendar_db import EVENT_TYPES as _ALLOWED_TYPES
    et = data.get("event_type") or "family_gathering"
    if et not in _ALLOWED_TYPES:
        et = "family_gathering"

    out = {
        "has_event": has,
        "is_cancellation": cancel,
        "title": _s("title", 60),  # 放寬：含動詞+物+主角更具體
        "date": _s("date", 10),
        "time": _s("time", 5),
        "location": _s("location", 120),  # 放寬：含分店/樓層更精準
        "participants": parts,
        "cancel_target_keyword": _s("cancel_target_keyword", 40),
        "event_type": et,
    }
    # date 格式驗證：YYYY-MM-DD；否則 None
    date_val = out["date"]
    if isinstance(date_val, str) and date_val:
        try:
            datetime.strptime(date_val, "%Y-%m-%d")
        except ValueError:
            out["date"] = None
    # time：HH:MM
    time_val = out["time"]
    if isinstance(time_val, str) and time_val:
        if not re.fullmatch(r"\d{2}:\d{2}", time_val):
            out["time"] = None
    return out
