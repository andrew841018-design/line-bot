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

只認「家族實體聚會」：聚餐、出遊、生日趴、就醫陪同、家庭活動、回老家、接送。
**不要**抓：純閒聊、工作排程、新聞、網購、抽象計畫（「以後想去」）。

今天是 {today}（週{weekday}）。

【對話】
{dialogue}

任務：
1. 有沒有人在「邀約 / 約定 / 規劃」一個具體家族活動？→ has_event=true
2. 有沒有人在「取消 / 改期 / 不去了」之前約好的活動？→ is_cancellation=true
3. 兩者都不是 → has_event=false, is_cancellation=false

抽欄位（無就 null）：
- title：簡短 6 字內，例「家族聚餐」「妹妹生日」「爺爺看醫生」
- date：YYYY-MM-DD。模糊詞要換成今日換算後的日期：
    今天=今天日期；明天=+1；後天=+2；下週X=下個週X；本週X=本週X
- time：24h 格式 HH:MM；下午6點→18:00；晚上8點→20:00
- location：餐廳/地點名稱
- participants：講到誰：媽媽、爸爸、姊姊、妹妹、弟弟、全家、奶奶、爺爺…
- cancel_target_keyword：取消時要用來找原 event 的關鍵字（活動標題裡可能出現的字）

只回 JSON，不要 markdown：
{{"has_event": false, "is_cancellation": false, "title": null, "date": null, "time": null, "location": null, "participants": [], "cancel_target_keyword": null}}
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
    fail = {
        "has_event": False,
        "is_cancellation": False,
        "title": None,
        "date": None,
        "time": None,
        "location": None,
        "participants": [],
        "cancel_target_keyword": None,
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
            if "429" not in err and "RESOURCE_EXHAUSTED" not in err:
                break
    return fail


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

    out = {
        "has_event": has,
        "is_cancellation": cancel,
        "title": _s("title", 40),
        "date": _s("date", 10),
        "time": _s("time", 5),
        "location": _s("location", 80),
        "participants": parts,
        "cancel_target_keyword": _s("cancel_target_keyword", 40),
    }
    # date 格式驗證：YYYY-MM-DD；否則 None
    if out["date"]:
        try:
            datetime.strptime(out["date"], "%Y-%m-%d")
        except ValueError:
            out["date"] = None
    # time：HH:MM
    if out["time"]:
        if not re.fullmatch(r"\d{2}:\d{2}", out["time"]):
            out["time"] = None
    return out
