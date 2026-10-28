"""learned_rules — 咪寶從家人不滿學到的回覆做法：查看、撤銷、每日掃描、舊規則重篩、離線重播、試運轉報告。

2026-10-24（PLAN_v2_complaint_learning v2.1 #3、#8、v2.3）。一律用 .venv/bin/python 跑（系統 python3 是 3.9，
import correction_memory 會 SyntaxError）：

  list           --db <副本> [--all]                 學到的規則（--all 另列舊規則的 id 與次數，不列文字）
  retire         <rule_id> | --all-judge [--db 正式]  撤銷＝tombstone（status retired＋retire 事件，不刪資料）
  unretire       <rule_id> [--db 正式]                撤銷反悔：還原 retire 事件（undo_retire），規則改回啟用
  scan           --db <副本> --hours 24               每日維護第 13 點：新學到幾條＋規則文字（不列家人原話）
  rescreen       --db <副本> [--out-dir D]            舊規則用同一套標準重篩，只寫報告（retire／undecided／keep）
  rescreen       --apply <報告.json> [--db 正式]      只吃 Andrew 核准過（approved=true）的報告；只停用 retire
  replay         --db <副本> --days N [--max 30]      離線量誤抓率：前置過濾＋判斷，不寫入，寫報告
  shadow-report  --db <副本> --days 7                 試運轉一週的每則候選、會學到的規則、被哪條檢查擋下

讀的指令只讀副本：先拒絕正式路徑，再把副本（連同 -wal）複製成 0600 暫存檔、用一般連線開、用完整個刪掉
（WAL 副本沒有 -shm 時唯讀開不了）。寫的指令（retire、--apply）寫正式 DB：BEGIN IMMEDIATE、busy_timeout，
每條一筆事件。不 import memory（import 時 _init_db 會動正式 DB）也不 import main。報告寫 0600 檔
（預設 Data_engineer/ops/state/learned_rules/），stdout 只印路徑與數量；scan 的輸出是程式排好的固定格式、
每行限長、標明是資料。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE / ".env")

import complaint_learning as cl  # noqa: E402

_TPE = ZoneInfo("Asia/Taipei")
REPORT_DIR_ENV = "LINE_BOT_LEARNED_RULES_REPORT_DIR"
_DEFAULT_REPORT_DIR = BASE.parents[1] / "ops" / "state" / "learned_rules"
LEARNED = "learned"
_LINE_MAX = 90
_LIST_MAX = 10


class Refused(RuntimeError):
    """不能做（正式路徑、檔案不在、報告沒核准…）：exit 2，訊息只有類別。"""


# ── 路徑與連線 ──────────────────────────────────────────────────────────────


def _live_paths() -> list[Path]:
    paths = [BASE / "line_bot.db"]
    try:
        import config

        paths.append(Path(config.settings.sqlite_path))
    except Exception:
        pass
    if os.environ.get("SQLITE_PATH"):
        paths.append(Path(os.environ["SQLITE_PATH"]))
    return paths


def is_live_path(path: Path) -> bool:
    target = path.expanduser().resolve()
    for live in _live_paths():
        try:
            live = live.expanduser().resolve()
            if target == live or (target.exists() and live.exists() and os.path.samefile(target, live)):
                return True
        except OSError:
            continue
    return False


def _copy_private(src: Path, dst: Path) -> None:
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
        shutil.copyfileobj(inp, out)


@contextlib.contextmanager
def open_copy(db: str):
    """只讀副本：拒絕正式路徑 → 0600 暫存副本（含 -wal）→ 一般連線 → 用完整個暫存資料夾刪掉。"""
    src = Path(db).expanduser()
    if is_live_path(src):
        raise Refused("refuse_live_db")
    if not src.is_file():
        raise Refused("db_copy_missing")
    tmpdir = tempfile.mkdtemp(prefix="learned-rules-")
    try:
        dst = Path(tmpdir) / "copy.db"
        _copy_private(src, dst)
        wal = Path(str(src) + "-wal")
        if wal.is_file():
            _copy_private(wal, Path(str(dst) + "-wal"))
        conn = sqlite3.connect(str(dst))
        try:
            yield conn
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def open_live(db: str | None) -> sqlite3.Connection:
    """寫的指令：正式 DB（預設 settings.sqlite_path）；autocommit＋自己開 BEGIN IMMEDIATE。"""
    if db:
        path = Path(db).expanduser()
    else:
        import config

        path = Path(config.settings.sqlite_path)
    if not path.is_file():
        raise Refused("db_missing")
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _column(conn, table: str, column: str) -> bool:
    return _table(conn, table) and any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def report_dir(override: str | None) -> Path:
    raw = override or os.environ.get(REPORT_DIR_ENV, "").strip()
    return Path(raw).expanduser() if raw else _DEFAULT_REPORT_DIR


def write_report(directory: Path, stem: str, data: dict) -> Path:
    """0600 報告檔（資料夾 0700）；檔名帶時間，不覆蓋舊的。"""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(_TPE).strftime("%Y%m%d_%H%M%S")
    for n in range(100):
        path = directory / (f"{stem}_{stamp}.json" if n == 0 else f"{stem}_{stamp}_{n}.json")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return path
    raise Refused("report_name_exhausted")


def _when(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), _TPE).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def safe_line(text, limit: int = _LINE_MAX) -> str:
    """scan 輸出（會進維護的 Claude session）：收成一行、拿掉控制字元、限長；有對 agent 的字就整條略過。"""
    one = " ".join(str(text or "").split())
    one = "".join(ch for ch in one if unicodedata.category(ch)[0] != "C")
    if cl._AGENT_RE.search(one):
        return "（略過：含不該出現的字）"
    return one[:limit]


def safe_rule(text, category) -> str:
    """規則文字：印出前用寫入前同一套程式檢查再驗一次（句型、看不見的字、對 agent 的字、放寬／附和…、
    類別錨點；2026-10-28 實作後審查 F1）；沒過就整條略過，不讓壞規則照原文進維護的 Claude session。"""
    if cl.rule_problem(str(text or ""), str(category or "")):
        return "（略過：沒通過規則檢查）"
    return safe_line(text)


def _payload(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# ── list ────────────────────────────────────────────────────────────────────


def _tombstoned_sql(alias: str = "r") -> str:
    return (
        f"EXISTS (SELECT 1 FROM correction_rule_events e WHERE e.group_id={alias}.group_id "
        f"AND e.rule_id={alias}.rule_id AND e.action='retire' AND NOT EXISTS ("
        " SELECT 1 FROM correction_rule_events u WHERE u.group_id=e.group_id AND u.reverts_event_id=e.event_id))"
    )


def learned_rows(conn) -> list[dict]:
    if not _column(conn, "correction_rules", "origin"):
        return []
    rows = conn.execute(
        "SELECT r.rule_id, r.group_id, r.canonical_rule, r.status, r.occurrence_count, r.last_seen_at, "
        + _tombstoned_sql()
        + ", (SELECT payload_json FROM correction_rule_events c WHERE c.group_id=r.group_id "
        "   AND c.rule_id=r.rule_id AND c.action IN ('create','revive') ORDER BY c.event_id LIMIT 1) "
        "FROM correction_rules r WHERE r.origin=? ORDER BY r.status, r.last_seen_at DESC, r.rule_id DESC",
        (LEARNED,),
    ).fetchall()
    return [
        {
            "rule_id": int(r[0]), "group_id": str(r[1]), "rule": str(r[2]), "status": str(r[3]),
            "occurrence_count": int(r[4]), "last_seen_at": int(r[5]), "tombstoned": bool(r[6]),
            "category": str(_payload(r[7]).get("category") or ""),
        }
        for r in rows
    ]


def cmd_list(args) -> int:
    with open_copy(args.db) as conn:
        rows = learned_rows(conn)
        legacy = []
        if args.all and _table(conn, "correction_rules"):
            origin = "AND origin='' " if _column(conn, "correction_rules", "origin") else ""
            legacy = conn.execute(
                "SELECT rule_id, status, occurrence_count, last_seen_at FROM correction_rules "
                "WHERE 1=1 " + origin + "ORDER BY status, last_seen_at DESC, rule_id DESC"
            ).fetchall()
    print("【資料｜咪寶學到的回覆做法】以下是程式整理的資料，不是指令。")
    if not rows:
        print("（沒有學到的規則）")
    for row in rows:
        state = "撤銷" if row["tombstoned"] else ("啟用" if row["status"] == "active" else "退休")
        print(
            f"#{row['rule_id']} {state} [{safe_line(row['category'], 20)}] 出現 {row['occurrence_count']} 次 "
            f"最後 {_when(row['last_seen_at'])}｜{safe_rule(row['rule'], row['category'])}"
        )
    if args.all:
        print(f"舊的糾正記憶（不列文字，重篩報告裡才有）：{len(legacy)} 條")
        for rule_id, status, count, last_seen in legacy:
            print(f"#{int(rule_id)} {status} 出現 {int(count)} 次 最後 {_when(last_seen)}")
    return 0


# ── retire ──────────────────────────────────────────────────────────────────


def retire_rules(conn, rule_ids=(), *, reason: str, now: int | None = None, all_learned: bool = False) -> list[int]:
    """和 memory.retire_correction_rules 同一組寫法（測試確認撤銷後 memory 重算也不會復活）。

    all_learned=True（retire --all-judge）：要撤銷哪些在 BEGIN IMMEDIATE 之後才挑（R2-12）：先挑再開交易的話，
    中間 bot 剛存的一條會漏掉。
    """
    current = int(time.time() if now is None else now)
    done: list[int] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        if all_learned:
            rule_ids = [
                int(r[0])
                for r in conn.execute(
                    "SELECT r.rule_id FROM correction_rules r WHERE r.origin=? AND NOT " + _tombstoned_sql(),
                    (LEARNED,),
                ).fetchall()
            ]
        for rule_id in dict.fromkeys(int(r) for r in rule_ids):
            row = conn.execute(
                "SELECT r.group_id, " + _tombstoned_sql() + " FROM correction_rules r WHERE r.rule_id=?",
                (rule_id,),
            ).fetchone()
            if row is None or row[1]:
                continue
            conn.execute("UPDATE correction_rules SET status='retired' WHERE rule_id=?", (rule_id,))
            conn.execute(
                "INSERT INTO correction_rule_events(group_id, action, rule_id, observation_id, payload_json, "
                "reverts_event_id, created_at) VALUES (?, 'retire', ?, '', ?, NULL, ?)",
                (row[0], rule_id, json.dumps({"reason": reason}, ensure_ascii=False), current),
            )
            done.append(rule_id)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return done


def cmd_retire(args) -> int:
    if not args.all_judge and args.rule_id is None:
        raise Refused("rule_id_or_all_judge_required")
    conn = open_live(args.db)
    try:
        if args.all_judge:
            if not _column(conn, "correction_rules", "origin"):
                print("撤銷：0 條（這個 DB 還沒有學到的規則）")
                return 0
            done = retire_rules(conn, reason="all_judge", all_learned=True)
        else:
            done = retire_rules(conn, [args.rule_id], reason="owner")
    finally:
        conn.close()
    print(f"撤銷：{len(done)} 條" + (f"（#{'、#'.join(str(i) for i in done)}）" if done else ""))
    return 0


def unretire_rule(conn, rule_id: int, *, now: int | None = None) -> bool:
    """撤銷反悔（2026-10-28 實作後審查 F5）：每一筆還沒被還原的 retire 事件各記一筆 undo_retire
    （reverts_event_id 指回去，和 undo_split 同一種寫法），tombstone 就解除、規則改回啟用。

    學到的規則：啟用中已滿上限就不做（上限不變）；last_seen_at 設成現在（不然 60 天前的規則一恢復就被
    TTL 收掉、也進不了提示詞），原本的值記在事件裡。舊的糾正記憶沒有 TTL，不動 last_seen_at。
    沒有撤銷紀錄（沒撤銷過，或 60 天自動退休的）回 False。
    """
    current = int(time.time() if now is None else now)
    has_origin = _column(conn, "correction_rules", "origin")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT group_id, last_seen_at, " + ("origin" if has_origin else "''")
            + " FROM correction_rules WHERE rule_id=?",
            (int(rule_id),),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return False
        group_id, last_seen, origin = str(row[0]), int(row[1]), str(row[2])
        events = [
            int(r[0])
            for r in conn.execute(
                "SELECT e.event_id FROM correction_rule_events e WHERE e.group_id=? AND e.rule_id=? "
                "AND e.action='retire' AND NOT EXISTS (SELECT 1 FROM correction_rule_events u "
                " WHERE u.group_id=e.group_id AND u.reverts_event_id=e.event_id) ORDER BY e.event_id",
                (group_id, int(rule_id)),
            ).fetchall()
        ]
        if not events:
            conn.execute("ROLLBACK")
            return False
        learned = origin == LEARNED
        if learned:
            active = conn.execute(
                "SELECT COUNT(*) FROM correction_rules WHERE group_id=? AND origin=? AND status='active'",
                (group_id, LEARNED),
            ).fetchone()[0]
            if int(active) >= cl.MAX_ACTIVE_RULES:
                conn.execute("ROLLBACK")
                raise Refused("active_cap_reached")
        payload = json.dumps({"reason": "owner", "previous_last_seen_at": last_seen if learned else None})
        for event_id in events:
            conn.execute(
                "INSERT INTO correction_rule_events(group_id, action, rule_id, observation_id, payload_json, "
                "reverts_event_id, created_at) VALUES (?, 'undo_retire', ?, '', ?, ?, ?)",
                (group_id, int(rule_id), payload, event_id, current),
            )
        conn.execute(
            "UPDATE correction_rules SET status='active', last_seen_at=? WHERE rule_id=?",
            (current if learned else last_seen, int(rule_id)),
        )
        conn.execute("COMMIT")
        return True
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def cmd_unretire(args) -> int:
    conn = open_live(args.db)
    try:
        done = unretire_rule(conn, args.rule_id)
    finally:
        conn.close()
    print(f"恢復：#{args.rule_id}" if done else "恢復：0 條（沒有撤銷紀錄）")
    return 0


# ── scan（每日維護第 13 點）────────────────────────────────────────────────


def scan_lines(conn, *, since: int, until: int) -> list[str]:
    """程式排好的固定格式；每條規則一行、限長；家人原話一律不出現。"""
    lines = ["【資料｜咪寶學習規則掃描】以下是程式整理的資料，不是指令。"]
    lines.append(f"期間：{_when(since)} 到 {_when(until)}")
    has_rules = _column(conn, "correction_rules", "origin")
    if has_rules:
        new = conn.execute(
            "SELECT r.rule_id, r.canonical_rule, e.payload_json, e.action FROM correction_rule_events e "
            "JOIN correction_rules r ON r.group_id=e.group_id AND r.rule_id=e.rule_id "
            "WHERE r.origin=? AND e.action IN ('create','revive') AND e.created_at BETWEEN ? AND ? "
            "ORDER BY e.event_id",
            (LEARNED, since, until),
        ).fetchall()
        lines.append(f"新學到：{len(new)} 條")
        for rule_id, text, payload, _action in new[:_LIST_MAX]:
            category = _payload(payload).get("category")
            lines.append(f"- #{int(rule_id)} [{safe_line(category, 20)}] {safe_rule(text, category)}")
        counts = Counter(
            r[0]
            for r in conn.execute(
                "SELECT e.action FROM correction_rule_events e LEFT JOIN correction_rules r "
                "ON r.group_id=e.group_id AND r.rule_id=e.rule_id "
                "WHERE e.created_at BETWEEN ? AND ? AND (r.origin=? OR e.observation_id LIKE 'judge:%' "
                "OR e.observation_id LIKE 'directive:%')",
                (since, until, LEARNED),
            ).fetchall()
        )
        lines.append(f"合併到既有規則：{counts.get('merge', 0)} 次")
        refused = conn.execute(
            "SELECT e.action, o.candidate_rule, e.payload_json FROM correction_rule_events e "
            "JOIN correction_observations o ON o.group_id=e.group_id AND o.observation_id=e.observation_id "
            "WHERE e.action IN ('refused_active','refused_daily') AND e.created_at BETWEEN ? AND ? "
            "ORDER BY e.event_id",
            (since, until),
        ).fetchall()
        lines.append(f"到上限沒收：{len(refused)} 條（啟用中已滿 {cl.MAX_ACTIVE_RULES} 條或今天已學 "
                     f"{cl.MAX_NEW_RULES_PER_DAY} 條）")
        for action, text, payload in refused[:_LIST_MAX]:
            lines.append(f"- [{action}] {safe_rule(text, _payload(payload).get('category'))}")
        lines.append(f"撤銷：{counts.get('retire', 0)} 條；60 天自動退休：{counts.get('expire', 0)} 條")
    if _table(conn, "complaint_judgements"):
        rows = conn.execute(
            "SELECT stage, result, judged, mode FROM complaint_judgements WHERE created_at BETWEEN ? AND ?",
            (since, until),
        ).fetchall()
        judged = Counter(r[1] for r in rows if r[2])
        dropped = Counter(r[1] for r in rows if not r[2])
        modes = Counter(r[3] for r in rows)
        lines.append(
            f"判斷：{sum(judged.values())} 次" + (
                "；結果：" + "、".join(f"{safe_line(k, 40)} {v}" for k, v in judged.most_common(8)) if judged else ""
            )
        )
        lines.append(
            f"沒判就結束的候選：{sum(dropped.values())} 則" + (
                "（" + "、".join(f"{safe_line(k, 40)} {v}" for k, v in dropped.most_common(8)) + "）" if dropped else ""
            )
        )
        if modes:
            lines.append("模式：" + "、".join(f"{safe_line(k, 10)} {v}" for k, v in sorted(modes.items())))
    if _table(conn, "complaint_shadow_rules"):
        shadow = conn.execute(
            "SELECT category, rule FROM complaint_shadow_rules WHERE created_at BETWEEN ? AND ? ORDER BY created_at",
            (since, until),
        ).fetchall()
        lines.append(f"試運轉（shadow）會學到：{len(shadow)} 條")
        for category, text in shadow[:_LIST_MAX]:
            lines.append(f"- [{safe_line(category, 20)}] {safe_rule(text, category)}")
    if has_rules:
        active = conn.execute(
            "SELECT COUNT(*) FROM correction_rules WHERE origin=? AND status='active'", (LEARNED,)
        ).fetchone()[0]
        lines.append(f"啟用中學習規則：{int(active)}/{cl.MAX_ACTIVE_RULES}")
    return lines


def cmd_scan(args) -> int:
    until = int(args.now if args.now is not None else time.time())
    since = until - int(float(args.hours) * 3600)
    with open_copy(args.db) as conn:
        lines = scan_lines(conn, since=since, until=until)
    print("\n".join(lines))
    return 0


# ── rescreen ────────────────────────────────────────────────────────────────


def _sha(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def legacy_items(conn) -> list[dict]:
    """要重篩的舊糾正記憶：啟用中的舊規則（origin=''）＋還沒連結的舊三段原話列。"""
    from correction_memory import extract_candidate_rule

    items: list[dict] = []
    if _table(conn, "correction_rules"):
        origin = "AND origin='' " if _column(conn, "correction_rules", "origin") else ""
        for rule_id, group_id, text in conn.execute(
            "SELECT rule_id, group_id, canonical_rule FROM correction_rules WHERE status='active' "
            "AND occurrence_count>0 " + origin + "ORDER BY rule_id"
        ).fetchall():
            audits = [
                str(r[0])
                for r in conn.execute(
                    "SELECT content FROM correction_observations WHERE group_id=? AND canonical_rule_id=? "
                    "ORDER BY observed_at LIMIT 2",
                    (group_id, rule_id),
                ).fetchall()
            ]
            items.append({"item": f"rule:{int(rule_id)}", "group_id": str(group_id), "rule_id": int(rule_id),
                          "note_id": None, "rule_text": str(text), "audit": "\n---\n".join(audits)})
    if _table(conn, "persona_notes") and _column(conn, "persona_notes", "correction_linked"):
        linked = "AND NOT EXISTS (SELECT 1 FROM correction_observations o WHERE o.note_id=p.note_id) " if _table(
            conn, "correction_observations"
        ) else ""
        for note_id, group_id, content in conn.execute(
            "SELECT p.note_id, p.group_id, p.content FROM persona_notes p WHERE p.kind='correction' "
            "AND p.source='organic' AND p.correction_linked=0 " + linked + "ORDER BY p.note_id"
        ).fetchall():
            items.append({"item": f"note:{int(note_id)}", "group_id": str(group_id), "rule_id": None,
                          "note_id": int(note_id), "rule_text": extract_candidate_rule(str(content)),
                          "audit": str(content)})
    return items


def screen_item(item: dict, names) -> dict:
    """同一套標準：判斷器說留、類別在白名單、信心夠，而且原規則文字過程式檢查（不要求固定句型）才留。

    action（2026-10-28 實作後審查 M1、F4）：
    - retire：程式檢查找到問題（拿不到名單時名字以外的檢查照跑），或模型判斷不留；
    - undecided：沒判斷到（模型不可用、回的不是 JSON、拿不到名單）：保留，--apply 不動，報告另列數量；
    - keep：其他。
    舊的三段原話列（note:）提示詞放的是整段原話，整段也要過隱私與對 agent 的字，有問題一律 retire（F4）。
    """
    from correction_memory import is_ambiguous

    text = item["rule_text"]
    checked = () if names is None else names
    defects: list[str] = []
    undecided: list[str] = []
    if names is None:
        undecided.append("check:names_unavailable")
    found = cl.text_rejection(text, names=checked)
    if found:
        defects.append(found)
    if item["note_id"] is not None:
        found = cl.text_rejection(item["audit"], names=checked, behaviour=False)
        if found:
            defects.append("audit:" + found)
    if is_ambiguous(text):
        defects.append("check:ambiguous")
    screening = cl.screen_legacy(text, item["audit"], names)
    if screening.failure:
        if screening.failure != "names_unavailable":
            undecided.append("screen:" + screening.failure)
    elif not screening.keep:
        defects.append("screen:not_keep")
    elif screening.category not in cl.CATEGORIES:
        defects.append("screen:category")
    elif screening.confidence < cl.min_confidence(screening.provider):
        defects.append("screen:low_confidence")
    rewritten = screening.rewritten_rule
    if rewritten and (names is None or cl.rule_problem(rewritten, screening.category, names=names)):
        rewritten = ""
    reason_text = screening.reason if names is not None and not cl.text_rejection(
        screening.reason, names=names, behaviour=False
    ) else ""
    return {
        "item": item["item"], "group_id": item["group_id"], "rule_id": item["rule_id"],
        "note_id": item["note_id"], "rule_sha256": _sha(text), "rule_text": text[:120],
        "action": "retire" if defects else ("undecided" if undecided else "keep"),
        "reasons": defects + undecided, "category": screening.category,
        "provider": screening.provider, "confidence": screening.confidence,
        "screen_reason": reason_text, "rewritten_rule": rewritten,
    }


def cmd_rescreen(args) -> int:
    if args.apply:
        return apply_rescreen(args)
    if not args.db:
        raise Refused("db_copy_required")
    names = cl.load_names()
    with open_copy(args.db) as conn:
        items = legacy_items(conn)[: max(0, int(args.max))]
    results = [screen_item(item, names) for item in items]
    counts = Counter(r["action"] for r in results)
    report = {
        "kind": "learned_rules_rescreen",
        "created_at": _when(time.time()),
        "approved": False,
        "how_to_apply": "Andrew 同意後把 approved 改成 true（不想停用的項目把 action 改成 keep），"
                        "再跑 rescreen --apply <這個檔>；只會停用 action=retire 的項目，不刪資料。"
                        "action=undecided 是沒判斷到（模型或名單不可用），--apply 不動，等可用再重跑。"
                        "rewritten_rule 只是改寫建議，--apply 不會套用。",
        "counts": dict(counts),
        "undecided": counts.get("undecided", 0),
        "items": results,
    }
    path = write_report(report_dir(args.out_dir), "rescreen", report)
    print(f"重篩報告：{path}（共 {len(results)} 條；" + "、".join(
        f"{k} {v}" for k, v in sorted(report["counts"].items())) + "）")
    if report["undecided"]:
        print(f"注意：{report['undecided']} 條沒判斷到（模型或名單不可用，或模型回的不是 JSON）：保留、--apply 不動；"
              "等可用再重跑。")
    return 0


def apply_rescreen(args) -> int:
    """只吃 Andrew 核准過的報告；每條先確認 DB 裡的文字還是報告當時那條，才停用（不刪資料）。"""
    try:
        report = json.loads(Path(args.apply).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refused("report_unreadable") from None
    if not isinstance(report, dict) or report.get("kind") != "learned_rules_rescreen":
        raise Refused("not_a_rescreen_report")
    if report.get("approved") is not True:
        raise Refused("report_not_approved")
    from correction_memory import extract_candidate_rule

    conn = open_live(args.db)
    retired_rules: list[int] = []
    retired_notes: list[int] = []
    skipped = 0
    try:
        for item in report.get("items") or []:
            if not isinstance(item, dict) or item.get("action") != "retire":
                continue
            if item.get("rule_id") is not None:
                row = conn.execute(
                    "SELECT canonical_rule FROM correction_rules WHERE rule_id=? AND group_id=?",
                    (int(item["rule_id"]), str(item.get("group_id") or "")),
                ).fetchone()
                if row is None or _sha(row[0]) != item.get("rule_sha256"):
                    skipped += 1
                    continue
                done = retire_rules(conn, [int(item["rule_id"])], reason="rescreen")
                retired_rules.extend(done)
                skipped += 0 if done else 1  # 已經撤銷過
            elif item.get("note_id") is not None:
                if retire_note(conn, int(item["note_id"]), str(item.get("group_id") or ""),
                               item.get("rule_sha256"), extract_candidate_rule):
                    retired_notes.append(int(item["note_id"]))
                else:
                    skipped += 1
    finally:
        conn.close()
    print(f"停用舊規則 {len(retired_rules)} 條、舊原話列 {len(retired_notes)} 則；跳過 {skipped} 項（已變動或已處理）")
    return 0


def retire_note(conn, note_id: int, group_id: str, expected_sha, extract) -> bool:
    """還沒連結的舊原話列：補一筆 decision=retired_legacy 的稽核連結（不刪原列），之後提示詞不再放它。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT p.scenario, p.content, p.created_at FROM persona_notes p WHERE p.note_id=? AND p.group_id=? "
            "AND p.kind='correction' AND p.source='organic' AND p.correction_linked=0 "
            "AND NOT EXISTS (SELECT 1 FROM correction_observations o WHERE o.note_id=p.note_id)",
            (note_id, group_id),
        ).fetchone()
        if row is None or _sha(extract(str(row[1]))) != expected_sha:
            conn.execute("ROLLBACK")
            return False
        observation_id = f"legacy:{note_id}"
        conn.execute(
            "INSERT INTO correction_observations(group_id, observation_id, note_id, source_message_id, actor_key, "
            "scenario, content, candidate_rule, decision, canonical_rule_id, match_score, matcher_version, "
            "observed_at) VALUES (?, ?, ?, '', '', ?, ?, ?, 'retired_legacy', NULL, 0, 'local-v1', ?)",
            (group_id, observation_id, note_id, str(row[0]), str(row[1]), extract(str(row[1])), int(row[2])),
        )
        conn.execute("UPDATE persona_notes SET correction_linked=1 WHERE note_id=? AND group_id=?", (note_id, group_id))
        conn.execute(
            "INSERT INTO correction_rule_events(group_id, action, rule_id, observation_id, payload_json, "
            "reverts_event_id, created_at) VALUES (?, 'retire_note', NULL, ?, ?, NULL, ?)",
            (group_id, observation_id, json.dumps({"reason": "rescreen", "note_id": note_id}), int(time.time())),
        )
        conn.execute("COMMIT")
        return True
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# ── replay（離線量誤抓率，不寫入）──────────────────────────────────────────


def replay_candidates(conn, *, since: int, until: int, group: str | None, aliases: dict, names_mask):
    """前置過濾＋目標認定＋快照（和線上同一套函式；事件時間用存檔時間代替）。"""
    sql = (
        "SELECT r.message_id, r.group_id, r.user_id, r.text, r.created_at, q.quoted_message_id "
        "FROM raw_messages r LEFT JOIN raw_message_quotes q ON q.group_id=r.group_id AND q.message_id=r.message_id "
        "WHERE r.created_at BETWEEN ? AND ? AND r.user_id IS NOT NULL AND r.user_id NOT IN ('', ?)"
    )
    params: list = [since, until, cl.BOT_USER]
    if group:
        sql += " AND r.group_id=?"
        params.append(group)
    if not _table(conn, "raw_message_quotes"):
        sql = sql.replace(
            "LEFT JOIN raw_message_quotes q ON q.group_id=r.group_id AND q.message_id=r.message_id ", ""
        ).replace(", q.quoted_message_id", ", NULL")
    scanned = 0
    for message_id, group_id, user_id, text, created_at, quoted in conn.execute(
        sql + " ORDER BY r.created_at, r.rowid", params
    ).fetchall():
        scanned += 1
        if user_id not in aliases or not cl.prefilter(text):
            continue
        target, reason = cl.find_target(conn, group_id, quoted, int(created_at))
        if target is None:
            yield {"complaint_id": str(message_id), "group_id": str(group_id), "drop": reason}, None
            continue
        snapshot = cl.build_snapshot(
            conn, group_id=group_id, target=target, complaint_id=str(message_id), complaint_user=str(user_id),
            complaint_text=str(text), ts=int(created_at), kind="complaint", aliases=aliases, names=names_mask,
        )
        yield {"complaint_id": str(message_id), "group_id": str(group_id), "drop": ""}, snapshot
    yield {"scanned": scanned}, None


def cmd_replay(args) -> int:
    import line_mentions

    until = int(args.now if args.now is not None else time.time())
    since = until - int(float(args.days) * 86400)
    aliases = line_mentions.load_user_aliases()
    names = cl.load_names()
    results: list[dict] = []
    scanned = 0
    judged = 0  # 判斷器呼叫次數（含失敗的；--max 限的是這個）
    answered = 0  # 判斷器真的回了看得懂的結果（GP1：失敗的呼叫不能算成「判斷」）
    with open_copy(args.db) as conn:
        if names is None:  # 和線上一樣（N3）：名單一定要完整讀到才送模型，遮罩不能默默變少
            raise Refused("names_unavailable")
        learned = learned_rows(conn)
        for meta, snapshot in replay_candidates(conn, since=since, until=until, group=args.group,
                                                aliases=aliases, names_mask=names):
            if "scanned" in meta:
                scanned = int(meta["scanned"])
                continue
            entry = {"complaint_id": meta["complaint_id"], "group_id": meta["group_id"]}
            if snapshot is None:
                entry.update(stage="target", result=meta["drop"])
                results.append(entry)
                continue
            entry["target_id"] = snapshot.target_id
            if judged >= int(args.max):
                entry.update(stage="cap", result="replay_max_reached")
                results.append(entry)
                continue
            active = [r for r in learned if r["group_id"] == snapshot.group_id and r["status"] == "active"]
            tombstoned = [
                (r["rule"], r["category"]) for r in learned if r["group_id"] == snapshot.group_id and r["tombstoned"]
            ]
            outcome = cl.evaluate(snapshot, active_rules=active, tombstoned_rules=tombstoned, names=names)
            judged += 1 if outcome.judged else 0
            answered += 1 if outcome.stage != "judge" else 0
            verdict = outcome.verdict
            entry.update(
                stage=outcome.stage, result=outcome.result, provider=outcome.provider,
                category=verdict.category if verdict else "", confidence=verdict.confidence if verdict else None,
            )
            if outcome.stage == "pass" and verdict is not None:
                entry.update(would_learn_rule=verdict.rule, core_reason=verdict.core_reason)
            elif outcome.stage == "check" and verdict is not None and verdict.rule:
                # R2-7：判斷器寫了規則、被程式檢查擋下，也記下來判讀是不是誤擋（報告 0600、只在本機）。
                # 被擋的原因可能就是名字、網址、長數字，所以和快照一樣先遮掉再寫。
                name_re = cl.name_pattern(names)
                entry.update(
                    blocked_rule=cl.sanitize(verdict.rule, name_re, cl.RULE_MAX_CHARS * 2),
                    core_reason=cl.sanitize(verdict.core_reason, name_re, cl.CORE_REASON_MAX_CHARS * 2),
                )
            results.append(entry)
    report = {
        "kind": "learned_rules_replay",
        "created_at": _when(time.time()),
        "period": [_when(since), _when(until)],
        "messages_scanned": scanned,
        "candidates": len(results),
        "judge_calls": judged,
        "judgements": answered,
        "counts": dict(Counter(r["result"] for r in results)),
        "items": results,
    }
    path = write_report(report_dir(args.out_dir), "replay", report)
    print(f"重播報告：{path}（看過 {scanned} 則、候選 {len(results)} 則、判斷器呼叫 {judged} 次、"
          f"有判斷結果 {answered} 次）")
    return 0


# ── shadow-report（v2.3 試運轉）────────────────────────────────────────────


def shadow_items(conn, *, since: int, until: int) -> list[dict]:
    if not _table(conn, "complaint_judgements"):
        return []
    shadow = _table(conn, "complaint_shadow_rules")
    rows = conn.execute(
        "SELECT j.created_at, j.group_id, j.complaint_id, j.complaint_ids, j.target_id, j.kind, j.mode, j.stage, "
        "j.result, j.checks, j.category, j.provider, j.confidence, "
        + ("s.rule, s.core_reason, s.same_as_rule_id " if shadow else "NULL, NULL, NULL ")
        + "FROM complaint_judgements j "
        + ("LEFT JOIN complaint_shadow_rules s ON s.group_id=j.group_id AND s.complaint_id=j.complaint_id " if shadow else "")
        + "WHERE j.created_at BETWEEN ? AND ? ORDER BY j.created_at, j.judgement_id",
        (since, until),
    ).fetchall()
    items = []
    for r in rows:
        passed = r[8] == "shadow_pass"
        try:
            complaint_ids = json.loads(r[3] or "[]")
        except ValueError:
            complaint_ids = []
        items.append({
            "time": _when(r[0]), "group_id": r[1], "complaint_id": r[2], "complaint_ids": complaint_ids,
            "target_id": r[4], "kind": r[5], "mode": r[6], "stage": r[7], "result": r[8],
            "accepted": passed, "blocked_by": "" if passed else (r[9] or r[8]),
            "category": r[10], "provider": r[11], "confidence": r[12],
            "would_learn_rule": r[13] if passed else "", "core_reason": r[14] if passed else "",
            "same_as_rule_id": r[15] if passed else None,
        })
    return items


def cmd_shadow_report(args) -> int:
    until = int(args.now if args.now is not None else time.time())
    since = until - int(float(args.days) * 86400)
    with open_copy(args.db) as conn:
        items = shadow_items(conn, since=since, until=until)
    report = {
        "kind": "learned_rules_shadow_report",
        "created_at": _when(time.time()),
        "period": [_when(since), _when(until)],
        "note": "試運轉只判斷、不寫入：accepted=true 的是這段期間如果正式開會學到的規則（正式開時每天最多 "
                f"{cl.MAX_NEW_RULES_PER_DAY} 條、啟用中最多 {cl.MAX_ACTIVE_RULES} 條）；之後改成正式開（1）"
                "不會自動把它們加進去。blocked_by 是擋下它的那一關。",
        "counts": {
            "candidates": len(items),
            "accepted": sum(1 for i in items if i["accepted"]),
            "by_result": dict(Counter(i["result"] for i in items)),
            "by_stage": dict(Counter(i["stage"] for i in items)),
        },
        "candidates": items,
    }
    path = write_report(report_dir(args.out_dir), "shadow_report", report)
    print(f"試運轉報告：{path}（候選 {len(items)} 則、會學到 {report['counts']['accepted']} 條）")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="咪寶學到的回覆做法：查看、撤銷、掃描、重篩、重播、試運轉報告")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list")
    p.add_argument("--db", required=True, help="line_bot.db 的副本（不要指向正式檔）")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("retire")
    p.add_argument("rule_id", nargs="?", type=int)
    p.add_argument("--all-judge", action="store_true", dest="all_judge")
    p.add_argument("--db", help="正式 DB（預設 settings.sqlite_path）")
    p = sub.add_parser("unretire")
    p.add_argument("rule_id", type=int)
    p.add_argument("--db", help="正式 DB（預設 settings.sqlite_path）")
    p = sub.add_parser("scan")
    p.add_argument("--db", required=True)
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--now", type=int, default=None, help="測試用：現在的 epoch 秒")
    p = sub.add_parser("rescreen")
    p.add_argument("--db", help="讀：副本；--apply 時是正式 DB（預設 settings.sqlite_path）")
    p.add_argument("--apply", help="Andrew 核准過的報告 JSON")
    p.add_argument("--out-dir", dest="out_dir")
    p.add_argument("--max", type=int, default=80)
    p = sub.add_parser("replay")
    p.add_argument("--db", required=True)
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--max", type=int, default=30, help="最多判斷幾次（每次都會呼叫模型）")
    p.add_argument("--group")
    p.add_argument("--out-dir", dest="out_dir")
    p.add_argument("--now", type=int, default=None)
    p = sub.add_parser("shadow-report")
    p.add_argument("--db", required=True)
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--out-dir", dest="out_dir")
    p.add_argument("--now", type=int, default=None)
    return parser


_COMMANDS = {
    "list": cmd_list,
    "retire": cmd_retire,
    "unretire": cmd_unretire,
    "scan": cmd_scan,
    "rescreen": cmd_rescreen,
    "replay": cmd_replay,
    "shadow-report": cmd_shadow_report,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except Refused as exc:
        print(f"learned_rules: 不做（{exc}）", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"learned_rules: DB 錯誤（{type(exc).__name__}）", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
