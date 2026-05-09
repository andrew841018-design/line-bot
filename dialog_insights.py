"""
dialog_insights — 整合 helper：對單一 group_id 跑 style + topics + 高頻 entities。

CLI:
    ./.venv/bin/python dialog_insights.py --group <group_id_hash>

100% 本機。不打雲端 API。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from typing import Any

from dialog_style import analyze_user_style, generate_persona_prompt
from topic_modeling import extract_topics

try:
    from chinese_nlp import extract_entities  # type: ignore[import-not-found]
except Exception:  # pragma: no cover

    def extract_entities(text: str) -> dict[str, list[str]]:
        return {}


_DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "line_bot.db")


def _fetch_user_messages(group_id: str, db_path: str, limit: int | None = None) -> list[str]:
    """從 raw_messages 撈該群組的 user 訊息（過濾 __bot__、過濾空字串）。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        sql = (
            "SELECT text FROM raw_messages "
            "WHERE group_id = ? AND user_id != '__bot__' AND text IS NOT NULL AND text != '' "
            "ORDER BY created_at ASC"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        cur.execute(sql, (group_id,))
        return [r[0] for r in cur.fetchall() if r[0]]
    finally:
        con.close()


def analyze_group(
    group_id: str,
    db_path: str = _DEFAULT_DB,
    n_topics: int = 5,
    msg_limit: int | None = 2000,
) -> dict[str, Any]:
    """
    對單一 group_id 跑完整 insight。

    Returns dict:
      {
        "group_id": str,
        "msg_count": int,
        "style": {...},                # analyze_user_style 結果
        "persona_prompt": str,         # generate_persona_prompt 結果
        "topics": [{topic_id, top_words, doc_count}, ...],
        "entities_top": [(name, count), ...],
      }
    """
    msgs = _fetch_user_messages(group_id, db_path, limit=msg_limit)

    style = analyze_user_style(msgs)
    persona_prompt = generate_persona_prompt(style)

    raw_topics = extract_topics(msgs, n_topics=n_topics)
    # 把 model 物件拿掉，留下可序列化欄位
    topics_out: list[dict[str, Any]] = []
    for t in raw_topics:
        topics_out.append(
            {
                "topic_id": t["topic_id"],
                "top_words": t["top_words"],
                "doc_count": len(t["doc_indices"]),
            }
        )

    # 高頻 entities：對前 200 條跑 chinese_nlp.extract_entities，加總
    ent_counter: Counter[str] = Counter()
    for m in msgs[:200]:
        try:
            ents = extract_entities(m) or {}
        except Exception:
            ents = {}
        for v in ents.values():
            for name in v or []:
                if isinstance(name, str) and len(name) >= 2:
                    ent_counter[name] += 1
    entities_top = ent_counter.most_common(20)

    return {
        "group_id": group_id,
        "msg_count": len(msgs),
        "style": style,
        "persona_prompt": persona_prompt,
        "topics": topics_out,
        "entities_top": entities_top,
    }


def _cli() -> int:
    p = argparse.ArgumentParser(description="LINE bot dialog insight (style + topic + entity)")
    p.add_argument("--group", required=True, help="group_id hash (raw_messages.group_id)")
    p.add_argument("--db", default=_DEFAULT_DB, help="SQLite path (default: line_bot.db)")
    p.add_argument("--topics", type=int, default=5, help="LDA topic 數")
    p.add_argument("--limit", type=int, default=2000, help="最多分析多少條訊息")
    p.add_argument("--json", action="store_true", help="輸出 JSON 格式")
    args = p.parse_args()

    try:
        result = analyze_group(args.group, db_path=args.db, n_topics=args.topics, msg_limit=args.limit)
    except FileNotFoundError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    # 人類可讀版
    print(f"== Group: {result['group_id']} (msg_count={result['msg_count']}) ==\n")

    style = result["style"]
    print("── 風格指標 ──")
    print(f"  平均字數     : {style.get('avg_chars', 0):.1f}")
    print(f"  平均詞數     : {style.get('avg_tokens', 0):.1f}")
    print(f"  emoji/msg   : {style.get('emoji_per_msg', 0):.2f}")
    print(f"  提問率       : {style.get('question_ratio', 0):.1%}")
    print(f"  命令式率     : {style.get('imperative_ratio', 0):.1%}")
    pf = style.get("punct_freq", {})
    print(f"  標點頻率     : 。{pf.get('。', 0):.2f} ！{pf.get('！', 0):.2f} ？{pf.get('？', 0):.2f}")
    tone = style.get("tone_particles", {}) or {}
    if tone:
        print("  語氣詞       : " + ", ".join(f"{k}×{v}" for k, v in sorted(tone.items(), key=lambda x: -x[1])[:5]))
    tw = style.get("top_words", [])[:10]
    if tw:
        print("  Top words    : " + ", ".join(f"{w}({s:.2f})" for w, s in tw))

    print("\n── Persona prompt ──")
    print(result["persona_prompt"])

    print("\n── Topics ──")
    for t in result["topics"]:
        print(f"  topic#{t['topic_id']} ({t['doc_count']} docs): {', '.join(t['top_words'])}")

    if result["entities_top"]:
        print("\n── Entities top-10 ──")
        for name, c in result["entities_top"][:10]:
            print(f"  {name} ×{c}")

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
