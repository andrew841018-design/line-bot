"""Group message classifier — 自動辨識訊息類別寫進 raw_messages.category。

設計原則：
  1. **Rule-first**：關鍵字命中直接回類別，省 Gemini quota（多數案例適用）
  2. **Gemini lite fallback**：rule miss 才呼 `gemini_client._lite_or_main`，配額爆會自動 fallback main
  3. **Fire-and-forget**：`classify_async` 啟 thread 不阻塞 webhook
  4. **Silent fail**：任何例外吞掉、不阻塞主流程；DB write 失敗只 log warning

類別清單（user 確認 2026-05-10）：
  財經 / 健康 / 警示 / 影片 / 行程 / 其他
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CATEGORIES = ["財經", "健康", "警示", "影片", "行程", "其他"]
DEFAULT_CATEGORY = "其他"

_DB_PATH = Path(__file__).parent / "line_bot.db"

# ── Rule-based patterns（命中即回，省 Gemini 配額）─────────────────────────

_VIDEO_RE = re.compile(
    r"https?://(?:www\.)?(?:"
    r"youtube\.com/watch|youtu\.be/|"
    r"tiktok\.com/|"
    r"instagram\.com/(?:reel|p|tv)/|"
    r"fb\.watch/|facebook\.com/.*?/videos?/|"
    r"twitter\.com/.*?/status|x\.com/.*?/status|"
    r"threads\.net/@"
    r")",
    re.IGNORECASE,
)

_SCAM_RE = re.compile(
    r"詐騙|假投資|釣魚簡訊|line ?群.*?投資|"
    r"被騙|盜刷|盜帳號|帳號被盜|"
    r"假冒.*?銀行|假冒.*?客服|可疑連結|"
    r"165|刑事局.*?提醒",
    re.IGNORECASE,
)

_FINANCE_RE = re.compile(
    r"台積|TSMC|0050|0056|"
    r"股價|股票|大盤|台股|美股|港股|"
    r"ETF|債券|基金|配息|殖利率|本益比|EPS|"
    r"加密|比特幣|BTC|ETH|"
    r"漲停|跌停|漲幅|跌幅|月線|週線|K 線|"
    r"美聯儲|FED|升息|降息",
    re.IGNORECASE,
)

_HEALTH_RE = re.compile(
    r"血壓|血糖|膽固醇|心律|"
    r"感冒|咳嗽|喉嚨痛|頭痛|肚子痛|過敏|發燒|"
    r"疫苗|普拿疼|止痛藥|抗生素|"
    r"診所|看醫生|門診|急診|健保|"
    r"確診|新冠|流感",
    re.IGNORECASE,
)

_SCHEDULE_RE = re.compile(
    r"\d{1,2}/\d{1,2}|"
    r"下週|這週|下個月|下下週|明天|後天|大後天|"
    r"聚餐|開會|會議|討論|聚會|預約|門診|出發|到站|"
    r"提醒.*?(?:點|時)|早上.*?點|下午.*?點|晚上.*?點",
    re.IGNORECASE,
)


def classify_rule(text: str) -> Optional[str]:
    """Rule-based 分類；無命中回 None。

    Order matters：影片 > 警示 > 財經 > 健康 > 行程
    （影片連結先抓，警示優於財經因「假投資 LINE 群」常含財經詞）
    """
    if not text or not isinstance(text, str):
        return None
    if _VIDEO_RE.search(text):
        return "影片"
    if _SCAM_RE.search(text):
        return "警示"
    if _FINANCE_RE.search(text):
        return "財經"
    if _HEALTH_RE.search(text):
        return "健康"
    if _SCHEDULE_RE.search(text):
        return "行程"
    return None


def classify_gemini(text: str) -> Optional[str]:
    """Gemini lite 分類 fallback；失敗 / 配額爆 → None。"""
    if not text or len(text.strip()) < 2:
        return None
    try:
        import gemini_client  # local import 避免循環依賴
    except ImportError:
        logger.debug("gemini_client unavailable")
        return None

    prompt = (
        f"分類這則訊息為以下其中一個類別：{', '.join(CATEGORIES)}\n"
        f"訊息：{text[:500]}\n"
        f"只回類別名稱兩個中文字（如「財經」），不要解釋。"
    )
    try:
        resp = gemini_client._lite_or_main(prompt)
        if resp is None:
            return None
        out = (getattr(resp, "text", None) or "").strip()
    except Exception as e:
        logger.debug("classify_gemini failed: %s", type(e).__name__)
        return None

    for cat in CATEGORIES:
        if cat in out:
            return cat
    return None


def classify(text: str) -> str:
    """主分類入口：rule first → Gemini lite → 預設「其他」。"""
    if not text or not isinstance(text, str):
        return DEFAULT_CATEGORY
    text = text.strip()
    if len(text) < 2:
        return DEFAULT_CATEGORY

    cat = classify_rule(text)
    if cat is not None:
        return cat

    cat = classify_gemini(text)
    if cat is not None:
        return cat

    return DEFAULT_CATEGORY


# ── DB schema migration ─────────────────────────────────────────────────


_SCHEMA_LOCK = threading.Lock()
_SCHEMA_ENSURED = False


def ensure_schema(db_path: Optional[Path] = None) -> None:
    """加 raw_messages.category 欄位 + index（idempotent）。

    SQLite 沒有 ADD COLUMN IF NOT EXISTS，先用 PRAGMA 查再決定是否 ALTER。
    """
    global _SCHEMA_ENSURED
    if _SCHEMA_ENSURED:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_ENSURED:
            return
        path = db_path or _DB_PATH
        try:
            conn = sqlite3.connect(str(path), timeout=5)
            try:
                cur = conn.cursor()
                cur.execute("PRAGMA table_info(raw_messages)")
                cols = [row[1] for row in cur.fetchall()]
                if "category" not in cols:
                    cur.execute(
                        "ALTER TABLE raw_messages ADD COLUMN category TEXT"
                    )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_raw_messages_category "
                    "ON raw_messages(group_id, category, created_at)"
                )
                conn.commit()
            finally:
                conn.close()
            _SCHEMA_ENSURED = True
        except Exception as e:
            logger.warning("ensure_schema failed: %s", e)


def update_category(
    group_id: str,
    message_id: str,
    category: str,
    db_path: Optional[Path] = None,
) -> None:
    """寫 category 進 raw_messages 對應 row。"""
    if not group_id or not message_id or not category:
        return
    path = db_path or _DB_PATH
    try:
        ensure_schema(path)
        conn = sqlite3.connect(str(path), timeout=5)
        try:
            conn.execute(
                "UPDATE raw_messages SET category=? "
                "WHERE group_id=? AND message_id=?",
                (category, group_id, message_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("update_category failed: %s", e)


# ── Async wrapper for webhook ───────────────────────────────────────────


def classify_async(group_id: str, message_id: str, text: str) -> None:
    """Fire-and-forget：背景 thread 分類 + 寫 DB，不阻塞 caller。

    使用方式（main._handle_text_message）：
        message_classifier.classify_async(group_id, event.message.id, text)
    """
    if not group_id or not message_id or not text:
        return

    def _run() -> None:
        try:
            cat = classify(text)
            update_category(group_id, message_id, cat)
        except Exception:
            # 永不阻塞主流程
            pass

    threading.Thread(
        target=_run, daemon=True, name="msg-classify"
    ).start()


# ── Query helpers（給 /今日X 指令用）─────────────────────────────────────


def list_today_in_category(
    group_id: str,
    category: str,
    db_path: Optional[Path] = None,
    limit: int = 30,
) -> list:
    """List 今日該類別訊息（按時間早→晚）。

    回 list[dict]：[{"message_id", "user_id", "text", "created_at"}, ...]
    """
    if not group_id or category not in CATEGORIES:
        return []
    path = db_path or _DB_PATH
    try:
        ensure_schema(path)
        conn = sqlite3.connect(str(path), timeout=5)
        try:
            cur = conn.cursor()
            # created_at 是 unix timestamp（秒）；今天 = 從本地午夜 0 點起
            import time as _time
            now = _time.localtime()
            today_start = int(_time.mktime((
                now.tm_year, now.tm_mon, now.tm_mday,
                0, 0, 0, 0, 0, -1
            )))
            cur.execute(
                "SELECT message_id, user_id, text, created_at FROM raw_messages "
                "WHERE group_id=? AND category=? AND created_at>=? "
                "ORDER BY created_at ASC LIMIT ?",
                (group_id, category, today_start, limit),
            )
            return [
                {
                    "message_id": row[0],
                    "user_id": row[1],
                    "text": row[2],
                    "created_at": row[3],
                }
                for row in cur.fetchall()
            ]
        finally:
            conn.close()
    except Exception as e:
        logger.warning("list_today_in_category failed: %s", e)
        return []


def today_category_counts(
    group_id: str,
    db_path: Optional[Path] = None,
) -> dict:
    """List 今日每類別數量。回 {"財經": 3, "健康": 1, ...}。"""
    if not group_id:
        return {}
    path = db_path or _DB_PATH
    try:
        ensure_schema(path)
        conn = sqlite3.connect(str(path), timeout=5)
        try:
            cur = conn.cursor()
            import time as _time
            now = _time.localtime()
            today_start = int(_time.mktime((
                now.tm_year, now.tm_mon, now.tm_mday,
                0, 0, 0, 0, 0, -1
            )))
            cur.execute(
                "SELECT category, COUNT(*) FROM raw_messages "
                "WHERE group_id=? AND created_at>=? AND category IS NOT NULL "
                "GROUP BY category",
                (group_id, today_start),
            )
            return {row[0]: row[1] for row in cur.fetchall()}
        finally:
            conn.close()
    except Exception as e:
        logger.warning("today_category_counts failed: %s", e)
        return {}
