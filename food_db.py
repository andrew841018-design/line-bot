"""food_db.py — 家庭飲食 / 採購 signal 儲存（family_food 表）。

照 calendar_db.py 範式：_conn(WAL) / _lock / init_db() module-load 自動建表 /
migration 容忍 / group_id 強制 / 全程 parameterized（kind 當值，不進 column）。

設計決議（2026-05-31 §3 review chain 定案）：
- v1 不存個人 subject（GP2 blocker A）：subject 一律 HOUSEHOLD_SUBJECT="家庭"，
  免 user_id、免在共享家族群點名個人飲食的隱私風險。
- append-only event log；狀態查詢用「同 food 最新一筆事件定狀態」（GP1 Critical 2），
  不用集合差，避免「買→吃完→再買」被舊事件誤判。
- DEAD-code 隔離：不 import food_extractor、不碰 kg_triples（user directive 2026-05-31）。

PK = (group_id, source_msg_id, kind, food)：
- 同訊息重送（同 msg_id）→ INSERT OR IGNORE 自動 dedup（GP1 C5c）
- 不同訊息（不同 msg_id）的同 food 事件 → 保留（event log，GP1 補充 1）
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from config import settings

_DB_PATH = Path(settings.sqlite_path)
_lock = threading.Lock()
_INIT_PATHS: set[str] = set()

# v1：所有 signal 歸屬「家庭」層級，不分個人（GP2 blocker A）
HOUSEHOLD_SUBJECT = "家庭"

# enum source-of-truth（對齊 calendar_db.EVENT_TYPES，防漂移）
FOOD_KINDS: tuple[str, ...] = (
    "wants_to_eat",     # 想吃 X
    "has_food",         # 冰箱 / 家裡有 X
    "wants_bought",     # 要買 X
    "bought",           # 買了 X
    "likes_food",       # 喜歡 X
    "dislikes_food",    # 不喜歡 X
    "finished_food",    # X 沒了 / 吃完
)

# 對立事件對：同 food 取最新一筆決定當前狀態
_INVENTORY_KINDS = ("has_food", "finished_food")
_SHOPPING_KINDS = ("wants_bought", "bought")
_PREF_KINDS = ("likes_food", "dislikes_food")

# 中性營養標籤 / 過敏原白名單（GP2 D：禁自由字串，防滑坡到「降血壓 / 控糖」等醫療標籤）
NUTRITION_TAGS: tuple[str, ...] = (
    "高蛋白", "高澱粉", "高鈉", "中鈉", "低鈉", "高油", "中油", "低油",
)
ALLERGENS: tuple[str, ...] = ("蛋", "魚", "蝦", "貝類", "豆", "奶", "花生", "堅果", "麩質")


def is_valid_kind(kind: str) -> bool:
    return kind in FOOD_KINDS


def _db_path(db_path: Path | str | None = None) -> Path:
    return Path(db_path) if db_path is not None else _DB_PATH


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(
        _db_path(db_path),
        isolation_level=None,
        check_same_thread=False,
        factory=_ClosingConnection,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    path = _db_path(db_path)
    path_key = str(path)
    if path_key in _INIT_PATHS:
        return
    with _lock:
        if path_key in _INIT_PATHS:
            return
        with _conn(path) as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS family_food (
                    group_id      TEXT NOT NULL,
                    source_msg_id TEXT NOT NULL DEFAULT '',
                    kind          TEXT NOT NULL,
                    food          TEXT NOT NULL,
                    subject       TEXT NOT NULL DEFAULT '家庭',
                    source_text   TEXT,
                    created_at    INTEGER NOT NULL,
                    PRIMARY KEY (group_id, source_msg_id, kind, food)
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_family_food_lookup "
                "ON family_food(group_id, food, created_at)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_family_food_kind "
                "ON family_food(group_id, kind, created_at)"
            )
        _INIT_PATHS.add(path_key)


def insert_signal(
    group_id: str,
    kind: str,
    food: str,
    source_msg_id: str = "",
    source_text: str | None = None,
    created_at_ms: int | None = None,
    db_path: Path | str | None = None,
) -> bool:
    """寫一筆飲食 signal。回 True=新增、False=dedup 命中或參數無效。

    subject 固定 HOUSEHOLD_SUBJECT（v1 不分個人）。
    created_at_ms 預設 now（測試可注入以控制事件時序）。
    """
    if not group_id or not food or not is_valid_kind(kind):
        return False
    st = (source_text or "")[:500] or None
    ts = created_at_ms if created_at_ms is not None else int(time.time() * 1000)
    init_db(db_path)
    with _lock, _conn(db_path) as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO family_food "
            "(group_id, source_msg_id, kind, food, subject, source_text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (group_id, source_msg_id or "", kind, food, HOUSEHOLD_SUBJECT, st, ts),
        )
        return cur.rowcount > 0


def _foods_with_latest_kind(
    c: sqlite3.Connection,
    group_id: str,
    pair_kinds: tuple[str, ...],
    active_kind: str,
) -> list[str]:
    """回傳 group 內「最新一筆 pair_kinds 事件 == active_kind」的 food 清單。

    GP1 Critical 2 核心：用「最新事件定狀態」取代集合差。
    tie-break：同 created_at 用 rowid 較大者為最新。
    """
    ph = ",".join("?" * len(pair_kinds))
    rows = c.execute(
        f"SELECT f1.food FROM family_food f1 "
        f"WHERE f1.group_id = ? AND f1.kind = ? "
        f"AND NOT EXISTS ("
        f"  SELECT 1 FROM family_food f2 "
        f"  WHERE f2.group_id = f1.group_id AND f2.food = f1.food "
        f"  AND f2.kind IN ({ph}) "
        f"  AND (f2.created_at > f1.created_at "
        f"       OR (f2.created_at = f1.created_at AND f2.rowid > f1.rowid))"
        f")",
        (group_id, active_kind, *pair_kinds),
    ).fetchall()
    return sorted({r[0] for r in rows})


def query_inventory(group_id: str) -> list[str]:
    """家裡現有食材：每個 food 最新事件為 has_food（未被 finished_food 蓋過）。"""
    with _lock, _conn() as c:
        return _foods_with_latest_kind(c, group_id, _INVENTORY_KINDS, "has_food")


def query_shopping(group_id: str) -> list[str]:
    """待買清單：每個 food 最新事件為 wants_bought（未被 bought 蓋過）。"""
    with _lock, _conn() as c:
        return _foods_with_latest_kind(c, group_id, _SHOPPING_KINDS, "wants_bought")


def query_prefs(group_id: str) -> dict[str, list[str]]:
    """家庭飲食偏好：每個 food 最新一筆 like/dislike 決定。回 {'likes':[], 'dislikes':[]}。"""
    with _lock, _conn() as c:
        likes = _foods_with_latest_kind(c, group_id, _PREF_KINDS, "likes_food")
        dislikes = _foods_with_latest_kind(c, group_id, _PREF_KINDS, "dislikes_food")
    return {"likes": likes, "dislikes": dislikes}


def query_wants(group_id: str, limit: int = 10) -> list[str]:
    """最近想吃的（wants_to_eat，無對立事件，列最近 distinct）。"""
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT food, MAX(created_at) AS t FROM family_food "
            "WHERE group_id = ? AND kind = 'wants_to_eat' "
            "GROUP BY food ORDER BY t DESC LIMIT ?",
            (group_id, limit),
        ).fetchall()
        return [r[0] for r in rows]


def purge_older_than(days: int) -> int:
    """刪除 created_at 早於 N 天前的 signal（GP2 C：飲食是短時效資料）。回刪除筆數。"""
    cutoff = int(time.time() * 1000) - days * 86400 * 1000
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM family_food WHERE created_at < ?", (cutoff,))
        return cur.rowcount


def clear_group(group_id: str) -> int:
    """清空某 group 所有 signal（測試用，對齊 knowledge_graph.clear_triples）。"""
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM family_food WHERE group_id = ?", (group_id,))
        return cur.rowcount


# 允許 import 時自動建表（跟 calendar_db.py / memory.py 同模式）
init_db()
