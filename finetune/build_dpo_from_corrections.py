"""DPO 三元組 builder — 純本機從 organic corrections 萃出 (prompt, chosen, rejected)。

零 Gemini quota（label 已在 SQLite）：
- prompt   = persona_notes.prev_user_msg
- rejected = persona_notes.prev_bot_msg（user 糾正過的）
- chosen   = ???

「chosen」三層 fallback（按品質）：
  1. **後續 bot reply**：找 user 糾正完之後 bot 重答的訊息（context 表時序檢查）
  2. **summary**：Gemini 一句話「教訓」轉自然回應（簡單模板包裝）
  3. **跳過**：兩者都沒，這條 correction 寫進 negative-only set 給 prompt 注入用

CLI：
    python finetune/build_dpo_from_corrections.py --build
    python finetune/build_dpo_from_corrections.py --stats
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).parent
LINE_BOT = HERE.parent
DB_PATH = LINE_BOT / "line_bot.db"
DATA_DIR = HERE / "data"
DPO_OUT = DATA_DIR / "dpo_corrections.jsonl"
NEGATIVE_OUT = DATA_DIR / "negative_only.jsonl"

logger = logging.getLogger("build_dpo")


def _connect(db: Path = DB_PATH) -> sqlite3.Connection:
    if not db.exists():
        raise FileNotFoundError(f"找不到 {db}")
    return sqlite3.connect(str(db))


def fetch_organic_corrections(db: Path = DB_PATH) -> list[dict]:
    conn = _connect(db)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(persona_notes)")
        cols = [r[1] for r in cur.fetchall()]
        if "source" not in cols:
            return []  # 表 schema 還沒 migrate
        cur.execute(
            "SELECT note_id, group_id, kind, content, source, created_at "
            "FROM persona_notes WHERE source='organic' AND kind='correction' "
            "ORDER BY created_at"
        )
        rows = []
        for r in cur.fetchall():
            content = r[3] or ""
            # parsed content (memory.add_organic_correction 寫的格式)
            parsed = _parse_correction_content(content)
            if not parsed.get("prev_user_msg") or not parsed.get("prev_bot_msg"):
                continue
            rows.append({
                "id": r[0],  # note_id
                "group_id": r[1],
                "kind": r[2],
                "source": r[4],
                "created_at": r[5],
                **parsed,
            })
        return rows
    finally:
        conn.close()


def _parse_correction_content(content: str) -> dict:
    """memory.add_organic_correction 寫的格式 — 看 memory.py 確認真實 schema。

    預期格式（從 main._summarize_correction + memory.add_organic_correction）：
        prev_user: ...
        prev_bot: ...
        correction: ...
        summary: ...
    或 JSON。先嘗試 JSON，失敗解 key:value lines。
    """
    try:
        d = json.loads(content)
        if isinstance(d, dict):
            return {
                "prev_user_msg": d.get("prev_user_msg") or d.get("prev_user") or "",
                "prev_bot_msg": d.get("prev_bot_msg") or d.get("prev_bot") or "",
                "correction_msg": d.get("correction_msg") or d.get("correction") or "",
                "summary": d.get("summary") or "",
            }
    except (json.JSONDecodeError, ValueError):
        pass
    # fallback: line-by-line key parse
    out = {"prev_user_msg": "", "prev_bot_msg": "", "correction_msg": "", "summary": ""}
    for line in content.split("\n"):
        for key in ("prev_user", "prev_bot", "correction", "summary"):
            prefix = f"{key}:"
            if line.startswith(prefix):
                k_full = "prev_user_msg" if key == "prev_user" else \
                         "prev_bot_msg" if key == "prev_bot" else \
                         "correction_msg" if key == "correction" else "summary"
                out[k_full] = line[len(prefix):].strip()
                break
    return out


def find_recovered_reply(
    group_id: str, after_ts: int, db: Path = DB_PATH
) -> Optional[str]:
    """找 user 糾正完之後最近的 bot reply（時序）。

    沒就回 None，由 caller fallback 到 summary。
    """
    conn = _connect(db)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT text FROM context "
            "WHERE group_id=? AND role='bot' AND created_at > ? "
            "ORDER BY created_at LIMIT 1",
            (group_id, after_ts),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def build_chosen(corr: dict) -> tuple[Optional[str], str]:
    """挑 chosen reply。回 (chosen, source_tag)。"""
    # 1. 後續 bot reply
    recovered = find_recovered_reply(corr["group_id"], corr["created_at"])
    if recovered and recovered.strip() != corr["prev_bot_msg"].strip():
        return recovered, "recovered_bot_reply"
    # 2. summary 包裝
    summary = corr.get("summary", "").strip()
    if summary and len(summary) > 5:
        # 把「教訓」轉自然回應 — 簡單模板
        wrapped = f"（依{summary}）{summary}"
        return wrapped, "summary_wrapped"
    # 3. 跳過
    return None, "no_chosen"


def build_dpo_dataset(db: Path = DB_PATH) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    corrections = fetch_organic_corrections(db)
    dpo_pairs = []
    negative_only = []
    skipped = 0
    chosen_sources = {"recovered_bot_reply": 0, "summary_wrapped": 0}

    for corr in corrections:
        chosen, src_tag = build_chosen(corr)
        if not chosen:
            # 沒 chosen → negative-only（給 prompt 注入用）
            negative_only.append({
                "prompt": corr["prev_user_msg"],
                "rejected": corr["prev_bot_msg"],
                "correction": corr.get("correction_msg", ""),
                "summary": corr.get("summary", ""),
                "source": "negative_only",
                "metadata": {"correction_id": corr["id"]},
            })
            skipped += 1
            continue
        chosen_sources[src_tag] = chosen_sources.get(src_tag, 0) + 1
        dpo_pairs.append({
            "prompt": corr["prev_user_msg"],
            "chosen": chosen,
            "rejected": corr["prev_bot_msg"],
            "source": "dpo_correction",
            "metadata": {
                "correction_id": corr["id"],
                "chosen_source": src_tag,
                "summary": corr.get("summary", ""),
            },
        })

    # 寫 jsonl
    with DPO_OUT.open("w", encoding="utf-8") as f:
        for d in dpo_pairs:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    with NEGATIVE_OUT.open("w", encoding="utf-8") as f:
        for d in negative_only:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    return {
        "total_corrections": len(corrections),
        "dpo_pairs": len(dpo_pairs),
        "negative_only": len(negative_only),
        "no_chosen_skipped": skipped,
        "chosen_sources": chosen_sources,
        "dpo_path": str(DPO_OUT),
        "negative_path": str(NEGATIVE_OUT),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build", action="store_true", help="build dpo + negative jsonl")
    ap.add_argument("--stats", action="store_true", help="只印統計，不寫")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.stats:
        rows = fetch_organic_corrections(args.db)
        print(f"organic corrections: {len(rows)}")
        for r in rows[:3]:
            print(
                f"  id={r['id']} prev_user={r['prev_user_msg'][:30]!r} "
                f"summary={r.get('summary', '')[:30]!r}"
            )
        return 0

    if args.build:
        result = build_dpo_dataset(args.db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
