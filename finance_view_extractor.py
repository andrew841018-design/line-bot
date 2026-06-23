"""家族財經觀點抽取器。

在 burst_filter flush 時呼叫，將合併對話送 Gemini 抽財經觀點：
- 標的（ticker / macro / crypto / index）
- 方向 / 時間框架 / 信心度
- 目標價 / 條件
- 預期驗證日期（expires_at）

設計：
- prefilter 命中財經 lexicon 才送 Gemini（省 quota，今日已 6.4x 超載）
- 用 settings.gemini_light_model（gemini-2.5-flash-lite）+ thinking_budget=0
- fail-soft（不擋 burst 主流程）
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from google.genai import types

import finance_view_db
import gemini_client
from config import settings

logger = logging.getLogger(__name__)
_TW = ZoneInfo("Asia/Taipei")

# Prefilter — 命中任一才送 Gemini（codex review 整合 token list）
_FINANCE_PATTERN = re.compile(
    r"\d{4}(?:\.TW)?|"
    r"台積電|聯電|鴻海|聯發科|大立光|"
    r"TSMC|NVDA|AMD|MSFT|META|AAPL|GOOGL|TSLA|"
    r"0050|0056|00878|00919|ETF|"
    r"台股|美股|大盤|加權|標普|S&P|那斯達克|Nasdaq|費半|SOXX|SOXL|"
    r"半導體|科技股|金融股|生技|傳產|"
    r"Fed|聯準會|FOMC|FED|"
    r"CPI|通膨|升息|降息|利率|央行|"
    r"美元|匯率|"
    r"BTC|ETH|加密|比特幣|"
    r"看多|看空|偏多|偏空|會漲|會跌|目標價|"
    r"反彈|崩盤|修正|突破|跌破|"
    r"買進|賣出|加碼|減碼|抄底|逃頂"
)


def is_finance_burst(text: str) -> bool:
    """prefilter — 命中財經詞彙才送 Gemini extract。"""
    if not text or not text.strip():
        return False
    return bool(_FINANCE_PATTERN.search(text))


_PROMPT = """你是家族 LINE 群財經觀點記錄員。從下面對話判斷有沒有家族成員提出的「具體財經觀點 / 市場預測」要記下。

只認**個人意見、預測、判斷**：
- 「我覺得 0050 會漲到 180」「半導體會修正」「Fed 年底前應該不會升息」
- 含明確標的（股票代號 / ETF / 大盤 / Fed / 通膨）+ 明確方向或目標

不要抓：
- 純轉貼新聞（沒個人意見）
- 純查詢（「0050 現在多少？」）
- 純抱怨（「股市好難」）
- 客觀事實陳述（「Fed 昨天升息了」— 過去式）

今天是 {today}。

【對話】
{dialogue}

任務：抽 0~N 個觀點。每個觀點為一個 JSON object。

欄位：
- symbol_type: "ticker" | "macro" | "crypto" | "index"
- ticker: 標的代號（台股加 .TW，例 0050.TW；美股 NVDA；macro 為 null）
- macro_topic: macro / 政策議題原文（例 "Fed 升息"），ticker 類為 null
- direction: "bull" | "bear" | "neutral"
- time_frame: "short"（< 1 個月）| "mid"（1-6 個月）| "long"（> 6 個月）
- horizon_days: 對應 time_frame 的天數（short=30, mid=90, long=180）
- target_price: 目標價（數字）或 null
- target_pct: 目標漲跌幅 %（數字）或 null
- confidence: "low" | "mid" | "high"
- condition_text: 條件句（例「若 Fed 升息則」）或 null
- speaker_hint: 提出人的稱謂（媽媽 / 爸爸 / 妹妹 / 弟弟 / 姊姊 / 自己），無法判斷則 null
- raw_quote: 原始一句話（< 80 字）

只回 JSON array（即使 0 個也回 []），不要 markdown：
[]
"""


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s


def _calc_expires_at(horizon_days: Optional[int]) -> Optional[str]:
    if not horizon_days or horizon_days <= 0:
        return None
    return (datetime.now(tz=_TW) + timedelta(days=horizon_days)).strftime("%Y-%m-%d")


def extract(combined_text: str) -> list[dict]:
    """回傳 list of view dicts。失敗回 []。"""
    if not is_finance_burst(combined_text):
        return []

    today = datetime.now(tz=_TW).strftime("%Y-%m-%d")
    prompt = _PROMPT.format(today=today, dialogue=combined_text[:2000])

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
            gemini_client._track_usage(resp)
        except Exception as e:
            gemini_client._track_failed_request()
            err = str(e)
            logger.warning(
                "finance_view extract failed (%s): %s", model_name, err[:200]
            )
            if "429" not in err and "RESOURCE_EXHAUSTED" not in err:
                break
            continue
        try:
            text = _strip_code_fence(resp.text or "")
            data = json.loads(text)
            if isinstance(data, list):
                return [_normalize(v) for v in data if isinstance(v, dict)]
            return []
        except Exception as e:
            err = str(e)
            logger.warning(
                "finance_view extract parse failed (%s): %s",
                model_name,
                err[:200],
            )
            break
    return []


def _normalize(view: dict) -> dict:
    def _s(k: str, max_len: int = 80) -> Optional[str]:
        v = view.get(k)
        if v is None:
            return None
        s = str(v).strip()
        return s[:max_len] if s else None

    def _f(k: str) -> Optional[float]:
        v = view.get(k)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _i(k: str) -> Optional[int]:
        v = view.get(k)
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    symbol_type = _s("symbol_type", 16) or "ticker"
    if symbol_type not in ("ticker", "macro", "crypto", "index"):
        symbol_type = "ticker"

    direction = _s("direction", 8)
    if direction not in ("bull", "bear", "neutral"):
        direction = None

    time_frame = _s("time_frame", 8)
    if time_frame not in ("short", "mid", "long"):
        time_frame = None

    confidence = _s("confidence", 8)
    if confidence not in ("low", "mid", "high"):
        confidence = None

    horizon_days = _i("horizon_days")
    return {
        "symbol_type": symbol_type,
        "ticker": _s("ticker", 16),
        "macro_topic": _s("macro_topic", 60),
        "direction": direction,
        "time_frame": time_frame,
        "horizon_days": horizon_days,
        "target_price": _f("target_price"),
        "target_pct": _f("target_pct"),
        "confidence": confidence,
        "condition_text": _s("condition_text", 200),
        "speaker_hint": _s("speaker_hint", 12),
        "raw_quote": _s("raw_quote", 200) or "",
        "expires_at": _calc_expires_at(horizon_days),
    }


def maybe_extract_and_save_async(
    group_id: str,
    combined_text: str,
    source_msg_id: Optional[str] = None,
    user_id_default: str = "",
    display_name_default: str = "家人",
) -> None:
    """fire-and-forget — 在 burst flush 時呼叫。"""
    if not is_finance_burst(combined_text):
        return
    db_path = finance_view_db._DB_PATH

    def _run() -> None:
        try:
            views = extract(combined_text)
            for v in views:
                display = v.get("speaker_hint") or display_name_default
                finance_view_db.insert_view(
                    group_id=group_id,
                    source_msg_id=source_msg_id,
                    user_id=user_id_default,
                    display_name=display,
                    raw_text=v.get("raw_quote", "")[:200],
                    symbol_type=v["symbol_type"],
                    ticker=v.get("ticker"),
                    macro_topic=v.get("macro_topic"),
                    direction=v.get("direction"),
                    time_frame=v.get("time_frame"),
                    horizon_days=v.get("horizon_days"),
                    target_price=v.get("target_price"),
                    target_pct=v.get("target_pct"),
                    confidence=v.get("confidence"),
                    condition_text=v.get("condition_text"),
                    expires_at=v.get("expires_at"),
                    db_path=db_path,
                )
                logger.info(
                    "finance_view saved: %s %s %s",
                    display,
                    v.get("ticker") or v.get("macro_topic"),
                    v.get("direction"),
                )
        except Exception as e:
            logger.warning("finance_view extract_and_save failed: %s", e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
