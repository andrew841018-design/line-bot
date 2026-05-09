"""Parse LINE app's exported chat history `.txt` files for fine-tuning data.

LINE's mobile app "儲存聊天記錄" feature dumps a `[LINE]<group>.txt`. Two
on-device formats are supported (zh-TW)::

    # Format A — iOS / 桌面 (TAB-separated, parens around weekday)
    [LINE] 聊天紀錄：群組名
    儲存日期：2026/05/08 12:34

    2025/12/01（日）
    05:23<TAB>Andrew<TAB>早安
    05:24<TAB>咪寶<TAB>早安喔～有什麼想聊的嗎？
    05:24<TAB>咪寶<TAB>看起來心情不錯耶
        續行 (沒有 HH:MM 開頭) → 接到上一則 message
    05:30<TAB>某成員<TAB>[貼圖]

    # Format B — Android (SPACE-separated, dotted date, bare-word media)
    2025.12.01 星期日
    05:23 Andrew 早安
    05:24 咪寶 早安喔
    14:00 Andrew 貼圖
    14:01 媽媽已收回訊息

Public API:
  - parse_line_chat(path) -> list[dict]
      Each dict: {date, time, sender, content, group_name}.
  - extract_pairs_from_export(path) -> list[dict]
      "Andrew → 咪寶" adjacent pairs filtered for media / 撤回 / 短訊息.
      Each dict: {prompt, completion, source: 'line_export', group_name,
                  timestamp}.
  - find_all_exports(search_dirs=None) -> list[Path]
      Auto-scan ~/Desktop, ~/Documents, ~/Downloads (default) for `[LINE]*.txt`.

100% local — no network calls.

CLI::

    python finetune/parse_line_export.py --scan
        # scan default dirs, print stats only (no writes)
    python finetune/parse_line_export.py --file path/to/[LINE]xxx.txt
        # parse a single file, print stats
"""
from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("parse_line_export")

# ── filter thresholds ────────────────────────────────────────────────────────
MIN_USER_LEN = 5
MIN_BOT_LEN = 5

# Default sender names (zh-TW LINE export uses display name)
DEFAULT_USER_NAME = "Andrew"
DEFAULT_BOT_NAME = "咪寶"

# Media / system message placeholders we treat as filler.
# Includes both bracketed (iOS/desktop) and bare (Android) variants.
MEDIA_PLACEHOLDERS = (
    "[貼圖]", "[照片]", "[影片]", "[檔案]", "[語音訊息]",
    "[相簿]", "[禮物]", "[紅包]", "[位置資訊]", "[聯絡人]",
    "[語音通話]", "[視訊通話]", "(已取消)",
    # Android bare-word media tokens
    "貼圖", "照片", "影片", "圖片", "檔案", "語音訊息",
    "相簿", "禮物", "紅包", "位置資訊", "聯絡人",
    "語音通話", "視訊通話",
    # English variants seen on some firmware
    "[Sticker]", "[Photo]", "[Video]", "[File]", "[Voice message]",
    "[Album]", "[Location]", "[Contact]",
    "[Photos]", "[Videos]",
)

# 撤回 placeholders
RECALLED_PHRASES = (
    "已收回訊息",
    "你已收回訊息",
    "收回了一則訊息",
    "Unsent a message",
    "Unsent message",
)

# Sender + 已收回訊息 fused inline (Android format: `媽媽已收回訊息`).
_SENDER_RECALL_RE = re.compile(r"^.+?已收回訊息$")

# Header patterns
_HEADER_GROUP_RE = re.compile(r"^\[LINE\]\s*聊天紀錄[：:]\s*(.+?)\s*$")
_HEADER_GROUP_EN_RE = re.compile(r"^\[LINE\]\s*Chat history with\s*(.+?)\s*$")
_SAVE_DATE_RE = re.compile(r"^儲存日期[：:]\s*(.+?)\s*$")

# 日期分隔行 — 三種變體：
#   YYYY/MM/DD（X）   iOS/桌面，斜線 + 括號週幾
#   YYYY.MM.DD 星期X   Android，點 + 「星期X」
#   YYYY/MM/DD         寬鬆 fallback
_DATE_LINE_RE = re.compile(
    r"^(\d{4})/(\d{1,2})/(\d{1,2})"
    r"[（(](?:[^）)]+)?[）)]\s*$"
)
_DATE_LINE_DOT_RE = re.compile(
    r"^(\d{4})\.(\d{1,2})\.(\d{1,2})\s+(?:星期|週)?[一二三四五六日天]\s*$"
)
_DATE_LINE_LOOSE_RE = re.compile(
    r"^(\d{4})[/.](\d{1,2})[/.](\d{1,2})\s*$"
)

# 訊息行 — 兩種變體：
#   HH:MM<TAB>sender<TAB>content    iOS / 桌面
#   HH:MM<SPACE>sender<SPACE>content   Android
_MSG_LINE_TAB_RE = re.compile(r"^(\d{1,2}:\d{2})\t(.+)$")
_MSG_LINE_SPACE_RE = re.compile(r"^(\d{1,2}:\d{2})\s+(\S+)\s+(.+)$")
# Android-only: `HH:MM <sender>已收回訊息` — sender 跟 已收回訊息 黏在一起，沒 content
_MSG_LINE_RECALL_RE = re.compile(r"^(\d{1,2}:\d{2})\s+(\S+?)已收回訊息\s*$")
# Android-only: `HH:MM <sender>` 後面什麼都沒（理論上不該有但保險）
_MSG_LINE_NO_CONTENT_RE = re.compile(r"^(\d{1,2}:\d{2})\s+(\S+)\s*$")


def _is_media_or_recalled(content: str) -> bool:
    """True if the content is purely a media placeholder / recalled marker."""
    s = content.strip()
    if not s:
        return True
    for ph in MEDIA_PLACEHOLDERS:
        if s == ph:
            return True
    for r in RECALLED_PHRASES:
        if r in s:
            return True
    if _SENDER_RECALL_RE.match(s):
        return True
    return False


def _extract_group_name(line: str) -> str | None:
    m = _HEADER_GROUP_RE.match(line)
    if m:
        return m.group(1).strip()
    m = _HEADER_GROUP_EN_RE.match(line)
    if m:
        return m.group(1).strip()
    return None


def _parse_date_line(line: str) -> str | None:
    """Return YYYY-MM-DD if line is a date separator, else None."""
    for pattern in (_DATE_LINE_RE, _DATE_LINE_DOT_RE, _DATE_LINE_LOOSE_RE):
        m = pattern.match(line)
        if m:
            y, mo, d = m.group(1), m.group(2), m.group(3)
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    return None


def _normalize_time(s: str) -> str:
    h, mn = s.split(":")
    return f"{int(h):02d}:{int(mn):02d}"


def _parse_msg_line(line: str) -> tuple[str, str, str] | None:
    """Parse a message line → (time, sender, content). None if no match.

    Tries TAB format first (iOS/桌面), then SPACE format (Android), then
    `<sender>已收回訊息` and `<sender> only`.
    """
    # Format A: HH:MM <TAB> sender <TAB> content
    m = _MSG_LINE_TAB_RE.match(line)
    if m:
        time_str = m.group(1)
        rest = m.group(2)
        if "\t" in rest:
            sender, content = rest.split("\t", 1)
            sender = sender.strip()
            if sender:
                return _normalize_time(time_str), sender, content

    # Format B-recall: HH:MM <sender>已收回訊息
    m = _MSG_LINE_RECALL_RE.match(line)
    if m:
        return _normalize_time(m.group(1)), m.group(2).strip(), "已收回訊息"

    # Format B: HH:MM <sender> <content>
    m = _MSG_LINE_SPACE_RE.match(line)
    if m:
        return _normalize_time(m.group(1)), m.group(2).strip(), m.group(3)

    # HH:MM <sender>  (content empty — ignored as no signal)
    m = _MSG_LINE_NO_CONTENT_RE.match(line)
    if m:
        return _normalize_time(m.group(1)), m.group(2).strip(), ""

    return None


def parse_line_chat(path: Path | str) -> list[dict[str, Any]]:
    """Parse a `[LINE]xxx.txt` export → list of message dicts.

    Each dict::

        {
            "date": "YYYY-MM-DD",   # may be "" if no date header seen yet
            "time": "HH:MM",
            "sender": "<display name>",
            "content": "<text, multi-line joined with \\n>",
            "group_name": "<from header, or filename stem>",
        }

    Multi-line message content (continuation lines that don't start with HH:MM)
    is concatenated to the most recent message, separated by '\\n'.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("file not found: %s", p)
        return []

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("read failed %s: %s", p, e)
        return []

    lines = text.splitlines()
    group_name = ""
    current_date = ""
    out: list[dict[str, Any]] = []

    # Detect header within first ~10 lines
    for i, raw in enumerate(lines[:10]):
        gn = _extract_group_name(raw)
        if gn:
            group_name = gn
            break
    if not group_name:
        # fallback: use filename stem (strip [LINE] prefix if present)
        stem = p.stem
        if stem.startswith("[LINE]"):
            stem = stem[len("[LINE]"):].strip()
        group_name = stem

    for raw in lines:
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        # save-date / header lines we don't include as messages
        if _SAVE_DATE_RE.match(line):
            continue
        if _HEADER_GROUP_RE.match(line) or _HEADER_GROUP_EN_RE.match(line):
            continue

        # date separator?
        d = _parse_date_line(line)
        if d:
            current_date = d
            continue

        # message line?
        parsed = _parse_msg_line(line)
        if parsed:
            time_str, sender, content = parsed
            out.append({
                "date": current_date,
                "time": time_str,
                "sender": sender,
                "content": content,
                "group_name": group_name,
            })
            continue

        # continuation line → append to last msg's content
        if out:
            out[-1]["content"] = out[-1]["content"] + "\n" + line

    return out


def _is_quality_text(text: str, min_len: int) -> bool:
    """Reject media / recalled / too-short."""
    s = text.strip()
    if len(s) < min_len:
        return False
    if _is_media_or_recalled(s):
        return False
    return True


def extract_pairs_from_export(
    path: Path | str,
    user_name: str = DEFAULT_USER_NAME,
    bot_name: str = DEFAULT_BOT_NAME,
) -> list[dict[str, Any]]:
    """Find adjacent (Andrew, 咪寶) pairs in an export file → SFT records.

    Pair definition: a message from `user_name` immediately followed by a
    message from `bot_name` (any other speakers in between break the pair —
    we only count *immediate* neighbours).

    Filters applied:
      - media placeholders / recalled messages dropped
      - prompt < MIN_USER_LEN chars dropped
      - completion < MIN_BOT_LEN chars dropped

    Returns list of dicts::

        {
            "prompt": str,
            "completion": str,
            "source": "line_export",
            "group_name": str,
            "timestamp": "YYYY-MM-DD HH:MM",
        }
    """
    msgs = parse_line_chat(path)
    pairs: list[dict[str, Any]] = []
    n = len(msgs)
    i = 0
    while i < n - 1:
        cur = msgs[i]
        nxt = msgs[i + 1]
        if cur["sender"] == user_name and nxt["sender"] == bot_name:
            u = cur["content"].strip()
            b = nxt["content"].strip()
            if (
                _is_quality_text(u, MIN_USER_LEN)
                and _is_quality_text(b, MIN_BOT_LEN)
            ):
                ts = f"{nxt.get('date','')} {nxt.get('time','')}".strip()
                pairs.append({
                    "prompt": u,
                    "completion": b,
                    "source": "line_export",
                    "group_name": cur.get("group_name", ""),
                    "timestamp": ts,
                })
            i += 2
        else:
            i += 1
    return pairs


# ── auto-scan ────────────────────────────────────────────────────────────────
DEFAULT_SCAN_DIRS = (
    Path.home() / "Desktop",
    Path.home() / "Documents",
    Path.home() / "Downloads",
)


def find_all_exports(
    search_dirs: Iterable[Path | str] | None = None,
    max_depth: int = 4,
) -> list[Path]:
    """Find `[LINE]*.txt` files under the given dirs (default: Desktop/Docs/Downloads).

    Skips hidden directories, virtual envs, node_modules etc. Returns a sorted,
    de-duplicated list of absolute paths.
    """
    dirs: list[Path] = []
    if search_dirs is None:
        dirs = [Path(p) for p in DEFAULT_SCAN_DIRS]
    else:
        dirs = [Path(p) for p in search_dirs]

    seen: set[Path] = set()
    out: list[Path] = []
    skip_names = {".git", ".venv", "venv", "node_modules", "__pycache__",
                  ".Trash", "Library", ".cache"}
    for root in dirs:
        if not root.exists():
            continue
        try:
            base_depth = len(root.resolve().parts)
        except OSError:
            continue
        for p in root.rglob("[[]LINE[]]*.txt"):
            try:
                rp = p.resolve()
            except OSError:
                continue
            # depth guard
            try:
                if len(rp.parts) - base_depth > max_depth:
                    continue
            except Exception:
                pass
            # skip files inside excluded subdirs
            if any(part in skip_names for part in rp.parts):
                continue
            if rp in seen:
                continue
            seen.add(rp)
            out.append(rp)
    out.sort()
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────
def _scan_and_report(
    search_dirs: Iterable[Path | str] | None = None,
    user_name: str = DEFAULT_USER_NAME,
    bot_name: str = DEFAULT_BOT_NAME,
) -> dict[str, int]:
    files = find_all_exports(search_dirs)
    total_msgs = 0
    total_pairs = 0
    per_file: list[tuple[Path, int, int]] = []
    for f in files:
        msgs = parse_line_chat(f)
        pairs = extract_pairs_from_export(f, user_name=user_name, bot_name=bot_name)
        per_file.append((f, len(msgs), len(pairs)))
        total_msgs += len(msgs)
        total_pairs += len(pairs)

    print(f"找到 {len(files)} 個 export 檔，總共 {total_msgs} 條訊息，可萃出 {total_pairs} 對 pair")
    for f, m, p in per_file:
        print(f"  - {f}  msgs={m}  pairs={p}")
    return {
        "files": len(files),
        "messages": total_msgs,
        "pairs": total_pairs,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scan", action="store_true",
                    help="auto-scan ~/Desktop ~/Documents ~/Downloads, print stats")
    ap.add_argument("--file", help="parse a single [LINE]xxx.txt file")
    ap.add_argument("--user", default=DEFAULT_USER_NAME,
                    help="display name treated as user (default: Andrew)")
    ap.add_argument("--bot", default=DEFAULT_BOT_NAME,
                    help="display name treated as bot (default: 咪寶)")
    ap.add_argument("--dir", action="append", default=None,
                    help="extra dir to scan (repeatable)")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    if not args.scan and not args.file:
        ap.print_help()
        return 1

    if args.file:
        msgs = parse_line_chat(args.file)
        pairs = extract_pairs_from_export(args.file, user_name=args.user, bot_name=args.bot)
        print(f"{args.file}: msgs={len(msgs)}  pairs={len(pairs)}")
        return 0

    _scan_and_report(args.dir, user_name=args.user, bot_name=args.bot)
    return 0


__all__ = [
    "parse_line_chat",
    "extract_pairs_from_export",
    "find_all_exports",
    "DEFAULT_USER_NAME",
    "DEFAULT_BOT_NAME",
    "MEDIA_PLACEHOLDERS",
    "RECALLED_PHRASES",
]


if __name__ == "__main__":
    raise SystemExit(main())
