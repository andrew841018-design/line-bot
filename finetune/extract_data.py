#!/usr/bin/env python3
"""
extract_data.py — 從 line_bot.db 抽 instruction-tuning data。

資料來源：
- `context` 表（首選）：已經 paired 好的 user/bot 對話（seq 連續、role 標好），
  format: (group_id, seq, role, text)，role ∈ {'user', 'bot'}
- `raw_messages` 表（fallback）：原始群訊息，user_id='__bot__' 是 bot 自己的回覆，
  其他 user_id 是真實使用者；用時間排序找連續 user→bot pair。

過濾規則：
- 跳過 < 3 字（含全形空白後的 strip 後長度）
- 跳過純 emoji / 純 URL / 純標點
- 跳過 [burst] 開頭的合併 marker（保留內容，但不單獨成 example）
- 跳過 bot 內部訊息（quota 用完、提示）— 用關鍵字偵測
- bot 回覆 < 5 字也跳（太短沒訓練價值）

輸出：
- `data/sft.jsonl` — 每行一個 dict {"messages": [{"role": "user", "content": ...}, {"role": "assistant", "content": ...}]}
- 統計報告印 stdout

用法：
    python extract_data.py [--db PATH] [--out PATH] [--min-user-len N] [--min-bot-len N]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from typing import Iterable

# 預設路徑：相對於這個檔案
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.normpath(os.path.join(HERE, "..", "line_bot.db"))
DEFAULT_OUT = os.path.join(HERE, "data", "sft.jsonl")

# bot 內部訊息特徵（quota / 系統訊息 / 開機提示）— 跳掉
BOT_INTERNAL_PATTERNS = [
    r"Gemini.*額度.*用完",
    r"免費層.*已經用完",
    r"^\(系統\)",
    r"^\[系統\]",
    r"請等到明天早上",
    r"aistudio\.google\.com",
    r"^✅.*restart",
]
BOT_INTERNAL_RE = re.compile("|".join(BOT_INTERNAL_PATTERNS), re.IGNORECASE)

# 純 emoji / 標點 / URL — strip 後檢測
EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F"
    r"\U0001F0A0-\U0001F0FF✀-➿]+"
)
URL_RE = re.compile(r"https?://\S+|www\.\S+")
PUNCT_ONLY_RE = re.compile(r"^[\s\W_]+$")

# burst marker — 開頭的 [burst] 拿掉但保留 content
BURST_PREFIX_RE = re.compile(r"^\[burst\]\s*", re.IGNORECASE)


def clean_text(text: str) -> str:
    """去除 burst marker 與前後空白；保留中文 / emoji / URL（emoji-only / URL-only 在 quality_filter 才整則跳）。"""
    if text is None:
        return ""
    t = BURST_PREFIX_RE.sub("", text).strip()
    return t


def is_quality_user_msg(text: str, min_len: int = 3) -> bool:
    """user 訊息品質檢查 — 太短 / 純 emoji / 純 URL / 純標點都跳。"""
    if not text:
        return False
    t = text.strip()
    if len(t) < min_len:
        return False
    # 拿掉 emoji 與 URL 後還剩什麼
    stripped = EMOJI_RE.sub("", t)
    stripped = URL_RE.sub("", stripped)
    if len(stripped.strip()) < min_len:
        return False
    if PUNCT_ONLY_RE.match(t):
        return False
    return True


def is_quality_bot_msg(text: str, min_len: int = 5) -> bool:
    """bot 回覆品質檢查 — 跳太短 / 跳系統內部訊息（quota 提示等）。"""
    if not text:
        return False
    t = text.strip()
    if len(t) < min_len:
        return False
    if BOT_INTERNAL_RE.search(t):
        return False
    return True


def iter_pairs_from_context(
    db_path: str, min_user_len: int, min_bot_len: int
) -> Iterable[tuple[str, str, dict]]:
    """
    從 context 表抽 (user, bot) pair。

    yield: (user_text, bot_text, flags_dict)
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT group_id, seq, role, text FROM context ORDER BY group_id, seq"
    )
    rows = cur.fetchall()
    conn.close()

    # 按 group_id 分組
    by_group: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_group.setdefault(r["group_id"], []).append(r)

    for group_id, seq_rows in by_group.items():
        i = 0
        while i < len(seq_rows) - 1:
            cur_row = seq_rows[i]
            nxt_row = seq_rows[i + 1]
            # 找 user → bot 連續 pair（seq 連續 + role 對）
            if (
                cur_row["role"] == "user"
                and nxt_row["role"] == "bot"
                and nxt_row["seq"] == cur_row["seq"] + 1
            ):
                u = clean_text(cur_row["text"])
                b = clean_text(nxt_row["text"])
                flags = {
                    "source": "context",
                    "group_id": group_id,
                    "user_seq": cur_row["seq"],
                    "user_too_short": len(u) < min_user_len,
                    "bot_too_short": len(b) < min_bot_len,
                    "bot_internal": bool(BOT_INTERNAL_RE.search(b)),
                }
                if is_quality_user_msg(u, min_user_len) and is_quality_bot_msg(
                    b, min_bot_len
                ):
                    yield u, b, flags
                i += 2
            else:
                i += 1


def iter_pairs_from_raw(
    db_path: str, min_user_len: int, min_bot_len: int
) -> Iterable[tuple[str, str, dict]]:
    """
    從 raw_messages 表抽 fallback (user, bot) pair。

    raw_messages 沒有 explicit role；user_id='__bot__' 視為 bot reply。
    用 created_at 排序，找 user→__bot__ 連續 pair（同 group）。

    這個 source 雜訊較多（包含被 filter 掉沒回應的訊息），主要在 context 太少時補量。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT group_id, message_id, user_id, text, created_at "
        "FROM raw_messages ORDER BY group_id, created_at"
    )
    rows = cur.fetchall()
    conn.close()

    by_group: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_group.setdefault(r["group_id"], []).append(r)

    for group_id, group_rows in by_group.items():
        i = 0
        while i < len(group_rows) - 1:
            cur_row = group_rows[i]
            nxt_row = group_rows[i + 1]
            cur_is_user = (cur_row["user_id"] or "") != "__bot__"
            nxt_is_bot = (nxt_row["user_id"] or "") == "__bot__"
            if cur_is_user and nxt_is_bot:
                u = clean_text(cur_row["text"])
                b = clean_text(nxt_row["text"])
                flags = {
                    "source": "raw_messages",
                    "group_id": group_id,
                    "ts": cur_row["created_at"],
                    "user_too_short": len(u) < min_user_len,
                    "bot_too_short": len(b) < min_bot_len,
                    "bot_internal": bool(BOT_INTERNAL_RE.search(b)),
                }
                if is_quality_user_msg(u, min_user_len) and is_quality_bot_msg(
                    b, min_bot_len
                ):
                    yield u, b, flags
                i += 2
            else:
                i += 1


def dedupe_pairs(
    pairs: Iterable[tuple[str, str, dict]],
) -> list[tuple[str, str, dict]]:
    """同樣 user_text → 同樣 bot_text 只留一筆（避免重複訓練 over-fit 同個 prompt）。"""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, dict]] = []
    for u, b, f in pairs:
        key = (u, b)
        if key in seen:
            continue
        seen.add(key)
        out.append((u, b, f))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"path to line_bot.db (default: {DEFAULT_DB})")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"output JSONL path (default: {DEFAULT_OUT})")
    ap.add_argument("--min-user-len", type=int, default=3)
    ap.add_argument("--min-bot-len", type=int, default=5)
    ap.add_argument(
        "--include-raw",
        action="store_true",
        help="also extract from raw_messages (fallback, noisier)",
    )
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[ERR] DB not found: {args.db}", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    pairs: list[tuple[str, str, dict]] = []
    pairs.extend(iter_pairs_from_context(args.db, args.min_user_len, args.min_bot_len))
    ctx_count = len(pairs)
    if args.include_raw:
        pairs.extend(iter_pairs_from_raw(args.db, args.min_user_len, args.min_bot_len))
    raw_count = len(pairs) - ctx_count

    pairs = dedupe_pairs(pairs)

    # 寫 JSONL
    with open(args.out, "w", encoding="utf-8") as f:
        for u, b, _flags in pairs:
            obj = {
                "messages": [
                    {"role": "user", "content": u},
                    {"role": "assistant", "content": b},
                ]
            }
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    # ── 統計 ──────────────────────────────────────────────────────────
    total = len(pairs)
    if total == 0:
        print("[WARN] no pairs extracted — check DB contents")
        print(f"[OK] wrote 0 pairs to {args.out}")
        return 0

    user_lens = [len(u) for u, _, _ in pairs]
    bot_lens = [len(b) for _, b, _ in pairs]
    sources = Counter(f.get("source", "?") for _, _, f in pairs)

    print(f"[OK] wrote {total} pairs to {args.out}")
    print(f"  source breakdown: {dict(sources)}")
    print(f"    from context table: {ctx_count}")
    if args.include_raw:
        print(f"    from raw_messages: {raw_count}")
    print(
        f"  user msg length: avg={sum(user_lens) / total:.1f}, "
        f"min={min(user_lens)}, max={max(user_lens)}"
    )
    print(
        f"  bot msg length:  avg={sum(bot_lens) / total:.1f}, "
        f"min={min(bot_lens)}, max={max(bot_lens)}"
    )
    # quality flags（潛在問題，沒實際 drop，給人看）
    very_short_user = sum(1 for u, _, _ in pairs if len(u) < 5)
    very_long_bot = sum(1 for _, b, _ in pairs if len(b) > 1000)
    print(
        f"  quality flags: very_short_user(<5)={very_short_user}, "
        f"very_long_bot(>1000)={very_long_bot}"
    )

    # 真實建議：訓練量門檻
    if total < 500:
        print(
            "[INFO] < 500 pairs — fine-tune 資料量太少，建議改用 RAG（見 README）"
        )
    elif total < 3000:
        print(
            "[INFO] 500–3000 pairs — 邊緣案，先用 RAG 驗證需求；fine-tune 預期效果有限"
        )
    else:
        print(f"[INFO] {total} pairs — fine-tune 資料量充足，可進到 train 階段")

    return 0


if __name__ == "__main__":
    sys.exit(main())
