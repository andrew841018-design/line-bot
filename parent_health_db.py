"""爸媽健康（2026-10-11）：同意狀態、健康紀錄、去重紀錄三張表（line_bot.db）。

照 memory／food_db：connect_private_sqlite、WAL、synchronous=NORMAL、busy_timeout=5000。
import 時不建表、不讀檔；第一次寫入時才建（依路徑記在 _INIT_PATHS，conftest 換 _DB_PATH
後會不帶參數呼叫 init_db）。寫入拿 _lock；讀不拿鎖也不建表（表還沒建就當成沒有資料），
webhook 鎖裡的讀不會被背景寫入卡住。

紀錄只存 parent_health_extract 固定詞表裡的標籤與數字讀數，不存留言原文；add_events 寫入前
再驗一次，不合的丟掉並計數。詞表模組載不進來時一筆都不寫（fail closed）。

2026-10-28：去重紀錄定期清除（purge_alerts，每日迴圈一天一次；白名單以外的種類不動）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import settings
from sqlite_security import connect_private_sqlite

logger = logging.getLogger("parent_health_db")

_DB_PATH = Path(settings.sqlite_path)
_lock = threading.Lock()  # 寫入用
_init_lock = threading.Lock()  # 建表用（和寫入分開：讀的路徑永遠不等寫入鎖）
_INIT_PATHS: set[str] = set()
_TPE = ZoneInfo("Asia/Taipei")

ROLES = ("爸爸", "媽媽")
STATUSES = ("on", "paused")
SOURCES = ("chat", "button")
REDFLAG = "redflag"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS parent_health_consent (
        group_id   TEXT NOT NULL,
        role       TEXT NOT NULL,
        status     TEXT NOT NULL,
        on_since   INTEGER NOT NULL DEFAULT 0,
        user_id    TEXT NOT NULL DEFAULT '',
        changed_by TEXT NOT NULL DEFAULT '',
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (group_id, role)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS parent_health_events (
        event_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id         TEXT NOT NULL,
        role             TEXT NOT NULL,
        category         TEXT NOT NULL,
        item             TEXT NOT NULL,
        value            TEXT NOT NULL DEFAULT '',
        flag             TEXT NOT NULL DEFAULT '',
        event_day        TEXT NOT NULL,
        event_ts         INTEGER NOT NULL,
        source           TEXT NOT NULL,
        message_id       TEXT NOT NULL,
        reporter_user_id TEXT NOT NULL DEFAULT '',
        created_at       INTEGER NOT NULL,
        UNIQUE (group_id, message_id, role, category, item, value)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_parent_health_events_day "
    "ON parent_health_events(group_id, role, event_day)",
    """
    CREATE TABLE IF NOT EXISTS parent_health_alerts (
        group_id TEXT NOT NULL,
        role     TEXT NOT NULL,
        kind     TEXT NOT NULL,
        key      TEXT NOT NULL,
        sent_at  INTEGER NOT NULL,
        PRIMARY KEY (group_id, role, kind, key)
    )
    """,
)


def _db_path(db_path: Path | str | None = None) -> Path:
    return Path(db_path) if db_path is not None else Path(_DB_PATH)


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = connect_private_sqlite(
        _db_path(db_path),
        isolation_level=None,
        check_same_thread=False,
        factory=_ClosingConnection,
    )
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # 主程式、背景執行緒、reminder_push 這類獨立程序會同時寫同一個 db。
        conn.execute("PRAGMA busy_timeout=5000")
    except BaseException:
        conn.close()  # 呼叫端拿不到連線，不會走到 __exit__
        raise
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """CREATE IF NOT EXISTS；同一路徑只做一次。寫入函式第一次用到時自動呼叫。"""
    path = _db_path(db_path)
    key = str(path)
    if key in _INIT_PATHS:
        return
    with _init_lock:
        if key in _INIT_PATHS:
            return
        with _conn(path) as c:
            for statement in _SCHEMA:
                c.execute(statement)
        _INIT_PATHS.add(key)


def _read(db_path: Path | str | None, sql: str, params: tuple) -> list[tuple]:
    """只讀：不拿鎖、不建表；表還沒建（從沒寫過）就回空。"""
    try:
        with _conn(db_path) as c:
            return c.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise


def _write(db_path: Path | str | None, sql: str, params: tuple) -> int:
    init_db(db_path)
    with _lock, _conn(db_path) as c:
        return max(c.execute(sql, params).rowcount, 0)


def day_of(ts: float) -> str:
    """unix 秒 → 台北日期 YYYY-MM-DD。"""
    return datetime.fromtimestamp(int(ts), _TPE).strftime("%Y-%m-%d")


# ── 固定詞表 ────────────────────────────────────────────────────────────────


def vocabulary():
    """(ALLOWED_ITEMS, VALUE_RE, 緊急字詞標籤)；詞表模組載不進來回 None（一律當成不合格）。
    每次都向 parent_health_extract 拿（測試換詞表、模組重新載入都照樣生效）。"""
    try:
        import parent_health_extract as extract

        return extract.ALLOWED_ITEMS, extract.VALUE_RE, frozenset(extract.RED_FLAG_LABELS)
    except Exception as exc:
        logger.error("parent health vocabulary unavailable error_type=%s", type(exc).__name__)
        return None


def record_allowed(vocab, category, item, value="", flag="") -> bool:
    """用 vocabulary() 的結果檢查一筆紀錄（渲染時一次拿詞表、逐筆檢查）。"""
    if vocab is None:
        return False
    allowed, value_re, red_flags = vocab
    if not isinstance(category, str) or not isinstance(item, str):
        return False
    if not isinstance(value, str) or not isinstance(flag, str):
        return False
    items = allowed.get(category) if isinstance(allowed, dict) else None
    if not items or item not in items:
        return False
    if value and not value_re.fullmatch(value):
        return False
    if flag == REDFLAG:
        return category == "body" and item in red_flags
    return flag == ""


def is_allowed(category: str, item: str, value: str = "", flag: str = "") -> bool:
    """這筆紀錄是不是固定詞表裡的標籤＋合格讀數（寫入與渲染都用）。"""
    return record_allowed(vocabulary(), category, item, value, flag)


# ── 同意 ────────────────────────────────────────────────────────────────────


def consent_states(group_id: str, *, db_path: Path | str | None = None) -> dict[str, dict]:
    """{role: {"status", "on_since", "user_id", "changed_by", "updated_at"}}；沒有列的角色不出現。"""
    rows = _read(
        db_path,
        "SELECT role, status, on_since, user_id, changed_by, updated_at "
        "FROM parent_health_consent WHERE group_id = ?",
        (group_id,),
    )
    return {
        role: {
            "status": status,
            "on_since": int(on_since or 0),
            "user_id": user_id or "",
            "changed_by": changed_by or "",
            "updated_at": int(updated_at or 0),
        }
        for role, status, on_since, user_id, changed_by, updated_at in rows
        if role in ROLES and status in STATUSES
    }


def set_consent(
    group_id: str,
    role: str,
    status: str,
    *,
    user_id: str,
    changed_by: str,
    now: float,
    db_path: Path | str | None = None,
) -> None:
    """轉成 on：on_since=now、user_id=同意者。轉成 paused：保留原本的 on_since 與 user_id
    （原本沒有同意列時記空的 user_id，暫停不等於同意）。"""
    if not group_id or role not in ROLES or status not in STATUSES:
        raise ValueError("invalid consent change")
    stamp = int(now)
    if status == "on":
        _write(
            db_path,
            "INSERT INTO parent_health_consent "
            "(group_id, role, status, on_since, user_id, changed_by, updated_at) "
            "VALUES (?, ?, 'on', ?, ?, ?, ?) "
            "ON CONFLICT(group_id, role) DO UPDATE SET status = 'on', "
            "on_since = excluded.on_since, user_id = excluded.user_id, "
            "changed_by = excluded.changed_by, updated_at = excluded.updated_at",
            (group_id, role, stamp, user_id or "", changed_by or "", stamp),
        )
        return
    _write(
        db_path,
        "INSERT INTO parent_health_consent "
        "(group_id, role, status, on_since, user_id, changed_by, updated_at) "
        "VALUES (?, ?, 'paused', 0, '', ?, ?) "
        "ON CONFLICT(group_id, role) DO UPDATE SET status = 'paused', "
        "changed_by = excluded.changed_by, updated_at = excluded.updated_at",
        (group_id, role, changed_by or "", stamp),
    )


def ensure_default_consent(
    group_id: str,
    accounts: dict[str, str],
    *,
    now: float,
    on_since: float | None = None,
    db_path: Path | str | None = None,
) -> int:
    """預設同意紀錄（Andrew 2026-10-17「健康預設同意紀錄」）：家長還沒有同意列 → 寫一列 on
    （on_since＝on_since 或 now、changed_by='default'）；on 但帳號換人 → 只改 user_id／changed_by／
    updated_at，on_since 照舊（暫時改別名不會把之前的天數歸零）。暫停的列不動。呼叫端要先確認是家族群組。
    回改到的列數。"""
    if not group_id:
        return 0
    stamp = int(now)
    start = int(now if on_since is None else on_since)
    changed = 0
    for role, user_id in accounts.items():
        if role not in ROLES or not user_id:
            continue
        changed += _write(
            db_path,
            "INSERT INTO parent_health_consent "
            "(group_id, role, status, on_since, user_id, changed_by, updated_at) "
            "VALUES (?, ?, 'on', ?, ?, 'default', ?) "
            "ON CONFLICT(group_id, role) DO UPDATE SET user_id = excluded.user_id, "
            "changed_by = excluded.changed_by, updated_at = excluded.updated_at "
            "WHERE parent_health_consent.status = 'on' AND parent_health_consent.user_id <> excluded.user_id",
            (group_id, role, start, user_id, stamp),
        )
    return changed


def groups_with_consent(*, db_path: Path | str | None = None) -> list[str]:
    """至少有一位家長目前是 on 的群組。"""
    rows = _read(
        db_path,
        "SELECT DISTINCT group_id FROM parent_health_consent WHERE status = 'on' ORDER BY group_id",
        (),
    )
    return [row[0] for row in rows]


# ── 紀錄 ────────────────────────────────────────────────────────────────────


def add_events(
    group_id: str,
    findings,
    *,
    message_id: str,
    reporter_user_id: str | None,
    event_ts: float,
    source: str,
    now: float,
    db_path: Path | str | None = None,
) -> int:
    """單一 transaction：先讀同意表，只寫 status 是 on 且 event_ts ≥ on_since 的角色。

    標籤不在固定詞表、讀數格式不合的丟掉（不寫、計數）；同一則訊息重送靠 UNIQUE 冪等。
    回寫入筆數。"""
    findings = list(findings or ())
    if not group_id or not message_id or source not in SOURCES or not findings:
        return 0
    vocab = vocabulary()
    rows: list[tuple[str, str, str, str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    dropped = 0
    for finding in findings:
        role = getattr(finding, "role", None)
        category = getattr(finding, "category", None)
        item = getattr(finding, "item", None)
        value = getattr(finding, "value", "")
        flag = getattr(finding, "flag", "")
        if role not in ROLES or not record_allowed(vocab, category, item, value, flag):
            dropped += 1
            continue
        key = (role, category, item, value)
        if key in seen:
            continue
        seen.add(key)
        rows.append((role, category, item, value, flag))
    if dropped:
        logger.warning("parent health dropped findings count=%d", dropped)
    if not rows:
        return 0
    ts = int(event_ts)
    event_day = day_of(ts)
    created_at = int(now)
    written = 0
    init_db(db_path)
    with _lock, _conn(db_path) as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            consent = {
                role: (status, int(on_since or 0))
                for role, status, on_since in c.execute(
                    "SELECT role, status, on_since FROM parent_health_consent WHERE group_id = ?",
                    (group_id,),
                )
            }
            for role, category, item, value, flag in rows:
                state = consent.get(role)
                if state is None or state[0] != "on" or ts < state[1]:
                    continue
                cursor = c.execute(
                    "INSERT OR IGNORE INTO parent_health_events "
                    "(group_id, role, category, item, value, flag, event_day, event_ts, "
                    "source, message_id, reporter_user_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        group_id, role, category, item, value, flag, event_day, ts,
                        source, message_id, reporter_user_id or "", created_at,
                    ),
                )
                written += max(cursor.rowcount, 0)
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
    return written


def list_events(
    group_id: str,
    role: str,
    *,
    since_day: str,
    until_day: str,
    db_path: Path | str | None = None,
) -> list[dict]:
    """event_day 在 since_day..until_day（兩端都含）的紀錄，舊到新。讀不拿鎖。"""
    rows = _read(
        db_path,
        "SELECT event_id, category, item, value, flag, event_day, event_ts, source, "
        "message_id, reporter_user_id, created_at FROM parent_health_events "
        "WHERE group_id = ? AND role = ? AND event_day >= ? AND event_day <= ? "
        "ORDER BY event_ts, event_id",
        (group_id, role, since_day, until_day),
    )
    keys = (
        "event_id", "category", "item", "value", "flag", "event_day", "event_ts", "source",
        "message_id", "reporter_user_id", "created_at",
    )
    return [dict(zip(keys, row, strict=True)) for row in rows]


def delete_message_events(
    group_id: str, message_id: str, *, db_path: Path | str | None = None
) -> int:
    """刪掉某則留言記下的所有健康紀錄（收回訊息、引用＋/健康 刪除）。回刪除筆數。"""
    if not group_id or not message_id:
        return 0
    return _write(
        db_path,
        "DELETE FROM parent_health_events WHERE group_id = ? AND message_id = ?",
        (group_id, message_id),
    )


# 本人刪掉全部紀錄時保留的去重種類：119 回覆（只有訊息 ID）、運動提醒（2026-10-23：只有日期和時間，不含健康資料；
# 刪了的話刪完下一句話又會被提醒）
_KEPT_ON_DELETE = ("reply", "exercise_nudge")


def delete_role_events(group_id: str, role: str, *, db_path: Path | str | None = None) -> int:
    """本人刪掉自己的全部紀錄。由紀錄算出來的私訊去重（種類含標籤或讀數）一起刪；119 回覆的
    去重（每天上限、30 分鐘）只有訊息 ID、運動提醒的去重只有日期，保留。回刪掉的紀錄筆數。"""
    if not group_id or role not in ROLES:
        return 0
    init_db(db_path)
    with _lock, _conn(db_path) as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            deleted = c.execute(
                "DELETE FROM parent_health_events WHERE group_id = ? AND role = ?",
                (group_id, role),
            ).rowcount
            c.execute(
                "DELETE FROM parent_health_alerts WHERE group_id = ? AND role = ? AND kind NOT IN (?, ?)",
                (group_id, role, *_KEPT_ON_DELETE),
            )
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
    return max(deleted, 0)


# ── 去重（119 回覆、私訊、群組通知、每日檢查） ───────────────────────────────


def claim_alert(
    group_id: str,
    role: str,
    kind: str,
    key: str,
    *,
    now: float,
    db_path: Path | str | None = None,
) -> bool:
    """INSERT OR IGNORE；這次插入成功（還沒有人搶過）才回 True。"""
    return (
        _write(
            db_path,
            "INSERT OR IGNORE INTO parent_health_alerts (group_id, role, kind, key, sent_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (group_id, role, kind, key, int(now)),
        )
        == 1
    )


def release_alert(
    group_id: str,
    role: str,
    kind: str,
    key: str,
    *,
    sent_at: int,
    db_path: Path | str | None = None,
) -> bool:
    """放掉 claim_alert 搶到的那一列（2026-10-23 運動提醒：LINE 明確說沒送出時）。key 與 sent_at 都要對：
    只刪自己搶到的那一列，別人後來搶的不會被刪。真的刪到才回 True。"""
    return (
        _write(
            db_path,
            "DELETE FROM parent_health_alerts WHERE group_id = ? AND role = ? AND kind = ? AND key = ? AND sent_at = ?",
            (group_id, role, kind, key, int(sent_at)),
        )
        == 1
    )


def last_alert(
    group_id: str, role: str, kind: str, *, db_path: Path | str | None = None
) -> dict | None:
    """最新一筆 {"key", "sent_at"}；沒有回 None。"""
    rows = _read(
        db_path,
        "SELECT key, sent_at FROM parent_health_alerts WHERE group_id = ? AND role = ? AND kind = ? "
        "ORDER BY sent_at DESC, rowid DESC LIMIT 1",
        (group_id, role, kind),
    )
    if not rows:
        return None
    return {"key": rows[0][0], "sent_at": int(rows[0][1])}


def count_alerts(
    group_id: str, kind: str, *, since_ts: float, db_path: Path | str | None = None
) -> int:
    """某群組 since_ts 之後（含）某種去重紀錄有幾個不同的 key（不分角色：一則 119 回覆提到兩位家長
    會記兩列，但只算一次回覆）。"""
    rows = _read(
        db_path,
        "SELECT COUNT(DISTINCT key) FROM parent_health_alerts WHERE group_id = ? AND kind = ? AND sent_at >= ?",
        (group_id, kind, int(since_ts)),
    )
    return int(rows[0][0]) if rows else 0


# ── 去重紀錄定期清除（2026-10-28） ───────────────────────────────────────────
# Andrew 10/18：「去重紀錄裡的讀數…加上定期清除（例如超過 7 天的就清）」。只清白名單裡的種類；種類用 GLOB 或
# 等號比對，不用 LIKE（`_` 是萬用字元，'bp_crisis:%' 會比到別的種類）。
# (回傳與 log 用的前綴, 比對方式, 比對值, 保留天數, 每個 (群組, 種類) 最新一列一律保留)
_PURGE_RULES: tuple[tuple[str, str, str, int, bool], ...] = (
    ("bp_crisis", "GLOB", "bp_crisis:*", 7, False),
    ("glucose_low", "GLOB", "glucose_low:*", 7, False),
    ("symptom", "GLOB", "symptom:*", 7, False),
    ("dm", "GLOB", "dm:*", 7, False),
    ("notice", "GLOB", "notice:*", 7, False),
    ("med_gap", "=", "med_gap", 7, False),
    ("med_forgot", "=", "med_forgot", 7, False),
    ("sleep_bad", "=", "sleep_bad", 7, False),
    ("exercise_nudge", "=", "exercise_nudge", 7, False),
    # 119 回覆：safety_replies_7d 和每天 2 則的上限都靠它，多留一天
    ("reply", "=", "reply", 8, False),
    # 每日檢查、週分析：status_summary 的 last_daily_check、週分析「今天搶過了沒」看最新一列
    ("daily_check", "=", "daily_check", 8, True),
    ("weekly_analysis", "=", "weekly_analysis", 8, True),
)
_HAS_NEWER_ROW = (
    " AND EXISTS (SELECT 1 FROM parent_health_alerts AS newer"
    " WHERE newer.group_id = parent_health_alerts.group_id AND newer.kind = parent_health_alerts.kind"
    " AND (newer.sent_at > parent_health_alerts.sent_at"
    " OR (newer.sent_at = parent_health_alerts.sent_at AND newer.rowid > parent_health_alerts.rowid)))"
)


def purge_alerts(
    now: float, *, dry_run: bool = False, db_path: Path | str | None = None
) -> dict[str, int]:
    """清掉超過保留天數的去重紀錄，回 {種類前綴: 筆數}（種類本身帶讀數或標籤：回傳與 log 一律只用前綴）。
    保留天數見 _PURGE_RULES：sent_at 早於「現在－保留天數」才清，剛好滿的不清；白名單以外的種類不動。
    dry_run 只算筆數：不刪、不建表、不拿寫入鎖。真的刪時開 secure_delete（刪掉的內容在檔案裡補零）。"""
    stamp = int(now)
    plan = [
        (
            prefix,
            f"kind {match} ? AND sent_at < ?" + (_HAS_NEWER_ROW if keep_latest else ""),
            (pattern, stamp - days * 86400),
        )
        for prefix, match, pattern, days, keep_latest in _PURGE_RULES
    ]
    counts = {prefix: 0 for prefix, _where, _params in plan}
    if dry_run:
        for prefix, where, params in plan:
            rows = _read(db_path, f"SELECT COUNT(*) FROM parent_health_alerts WHERE {where}", params)
            counts[prefix] = int(rows[0][0]) if rows else 0
        return counts
    init_db(db_path)
    with _lock, _conn(db_path) as c:
        c.execute("PRAGMA secure_delete=ON")
        c.execute("BEGIN IMMEDIATE")
        try:
            for prefix, where, params in plan:
                counts[prefix] = max(c.execute(f"DELETE FROM parent_health_alerts WHERE {where}", params).rowcount, 0)
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
    return counts


def stats(*, now: float | None = None, db_path: Path | str | None = None) -> dict:
    """給 status_summary：只有計數與日期，沒有任何紀錄內容或 ID。"""
    now = time.time() if now is None else float(now)
    week_ago = int(now) - 7 * 86400

    def scalar(sql: str, params: tuple = ()):
        rows = _read(db_path, sql, params)
        return rows[0][0] if rows else None

    return {
        "groups": int(scalar(
            "SELECT COUNT(DISTINCT group_id) FROM parent_health_consent WHERE status = 'on'"
        ) or 0),
        "consent_on": int(scalar(
            "SELECT COUNT(*) FROM parent_health_consent WHERE status = 'on'"
        ) or 0),
        "consent_paused": int(scalar(
            "SELECT COUNT(*) FROM parent_health_consent WHERE status = 'paused'"
        ) or 0),
        "events": int(scalar("SELECT COUNT(*) FROM parent_health_events") or 0),
        "events_7d": int(scalar(
            "SELECT COUNT(*) FROM parent_health_events WHERE created_at >= ?", (week_ago,)
        ) or 0),
        "safety_replies_7d": int(scalar(  # 一則回覆提到兩位家長記兩列，只算一次
            "SELECT COUNT(*) FROM (SELECT DISTINCT group_id, key FROM parent_health_alerts "
            "WHERE kind = 'reply' AND sent_at >= ?)",
            (week_ago,),
        ) or 0),
        "last_daily_check": scalar(
            "SELECT MAX(key) FROM parent_health_alerts WHERE kind = 'daily_check'"
        ),
        "oldest_on_since": scalar(
            "SELECT MIN(on_since) FROM parent_health_consent WHERE status = 'on'"
        ),
    }
