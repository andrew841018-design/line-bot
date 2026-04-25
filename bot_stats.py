"""
bot_stats.py — LINE bot 每日使用量統計

設計：只存 SQLite，不對外推播。Andrew 問的時候查詢給他看。
用途：判斷要不要付錢、付哪個方案、每月預算大概多少。
"""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

_TW = ZoneInfo("Asia/Taipei")
_lock = threading.Lock()
_DB_PATH = __import__("os").path.join(__import__("os").path.dirname(__file__), "line_bot.db")


def _today() -> str:
    return datetime.now(tz=_TW).strftime("%Y-%m-%d")


def _conn():
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_stats (
            date      TEXT NOT NULL,
            stat_key  TEXT NOT NULL,
            value     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (date, stat_key)
        )
    """)
    conn.commit()
    return conn


def increment(key: str, amount: int = 1, date: str | None = None) -> None:
    """thread-safe 累加一個計數器。"""
    d = date or _today()
    with _lock:
        conn = _conn()
        conn.execute("""
            INSERT INTO daily_stats(date, stat_key, value) VALUES(?, ?, ?)
            ON CONFLICT(date, stat_key) DO UPDATE SET value = value + excluded.value
        """, (d, key, amount))
        conn.commit()
        conn.close()


# ── 訊息類型分類（不需要 LLM，純關鍵字）──────────────────────────────────────

_URL_RE = re.compile(r"https?://")
_QUESTION_RE = re.compile(r"[？?]|嗎|幾|什麼|哪|怎麼|為什麼|如何|是否|有沒有")
_FINANCE_RE = re.compile(r"股票|ETF|投資|基金|台積電|漲|跌|理財|報酬|殖利率|配息|0\d{3}|NVDA|半導體")
_HEALTH_RE = re.compile(r"醫|病|症狀|藥|治療|健康|手術|診斷|癌|發燒|血壓|血糖")
_POLITICAL_RE = re.compile(r"政治|選舉|民進黨|國民黨|兩岸|台灣獨|統一|美國|關稅|川普|習近平")
_FACT_CHECK_RE = re.compile(r"真的假的|是真的|是假的|謠言|查一下|核實|假訊息|真假|可信")
_NEWS_RE = re.compile(r"新聞|報導|表示|宣布|公告|日前|據了解|指出")


def classify_message(text: str) -> str:
    """回傳訊息主類型（單一最符合的）。"""
    if not text or text.startswith("["):
        return "media"
    if _URL_RE.search(text):
        return "url"
    if _FACT_CHECK_RE.search(text):
        return "fact_check"
    if _FINANCE_RE.search(text):
        return "finance"
    if _HEALTH_RE.search(text):
        return "health"
    if _POLITICAL_RE.search(text):
        return "political"
    if _NEWS_RE.search(text):
        return "news"
    if _QUESTION_RE.search(text):
        return "question"
    if len(text) <= 20:
        return "casual"
    return "other"


def track_message(text: str) -> None:
    """收到訊息時呼叫：記錄總數 + 類型。"""
    increment("msg_received")
    cat = classify_message(text or "")
    increment(f"msg_type_{cat}")


def track_reply(provider: str) -> None:
    """成功回覆時呼叫。provider = 'gemini' | 'grok'。"""
    increment("reply_sent")
    increment(f"reply_{provider}")


def track_pending_saved() -> None:
    """訊息存入 pending（quota 爆）時呼叫。"""
    increment("msg_pending_saved")


def track_line_push() -> None:
    """成功 LINE push_message 時呼叫。"""
    increment("line_push_used")


# ── 查詢 ─────────────────────────────────────────────────────────────────────

def query_range(days: int = 30) -> list[dict]:
    """回傳最近 N 天的每日統計，newest first。"""
    conn = _conn()
    rows = conn.execute("""
        SELECT date, stat_key, value
        FROM daily_stats
        ORDER BY date DESC
        LIMIT ?
    """, (days * 20,)).fetchall()
    conn.close()

    from collections import defaultdict
    by_date: dict[str, dict] = defaultdict(dict)
    for date, key, val in rows:
        by_date[date][key] = val

    result = []
    for date in sorted(by_date.keys(), reverse=True)[:days]:
        result.append({"date": date, **by_date[date]})
    return result


def summary_report(days: int = 30) -> str:
    """格式化報告，Andrew 問的時候直接印出。"""
    data = query_range(days)
    if not data:
        return "尚無統計資料。"

    lines = [f"📊 LINE bot 使用統計（最近 {days} 天）\n"]
    total_received = sum(d.get("msg_received", 0) for d in data)
    total_replies = sum(d.get("reply_sent", 0) for d in data)
    total_pending = sum(d.get("msg_pending_saved", 0) for d in data)
    total_push = sum(d.get("line_push_used", 0) for d in data)
    total_gemini = sum(d.get("reply_gemini", 0) for d in data)
    total_grok = sum(d.get("reply_grok", 0) for d in data)

    lines.append(f"收到訊息：{total_received} 則（平均 {total_received//max(len(data),1)}/天）")
    lines.append(f"成功回覆：{total_replies} 則（Gemini {total_gemini} / Grok {total_grok}）")
    lines.append(f"存入 pending：{total_pending} 則（quota 爆時）")
    lines.append(f"LINE push 用量：{total_push} 則（免費上限 200/月）")

    # 訊息類型分布
    type_totals: dict[str, int] = {}
    for d in data:
        for k, v in d.items():
            if k.startswith("msg_type_"):
                cat = k[len("msg_type_"):]
                type_totals[cat] = type_totals.get(cat, 0) + v

    if type_totals:
        lines.append("\n訊息類型分布：")
        for cat, cnt in sorted(type_totals.items(), key=lambda x: -x[1]):
            lines.append(f"  {cat}: {cnt} 則")

    lines.append("\n每日明細：")
    for d in data[:14]:  # 最近 14 天
        recv = d.get("msg_received", 0)
        rep = d.get("reply_sent", 0)
        pend = d.get("msg_pending_saved", 0)
        push = d.get("line_push_used", 0)
        lines.append(f"  {d['date']}  收:{recv} 回:{rep} pending:{pend} push:{push}")

    return "\n".join(lines)
