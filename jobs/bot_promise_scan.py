"""bot_promise_scan — 列出咪寶最近在群組答應的改動（Andrew 2026-10-09）。

Andrew：「咪寶允許的事情，實際上要做到」。咪寶回「好的，下次會把主詞放前面」時，
它自己改不了程式；每日維護跑這支，把這些承諾列進維護報告，Andrew 核可後再實作。

只讀 ``--db`` 指定的 DB 副本，不連網、不寫入。每條輸出：時間、咪寶那句話、
前幾分鐘內家人引用的咪寶訊息第一行（例如「目前待辦/提醒：」——那是 bot 自己的字）。
不輸出家人寫的原文。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

_TW = ZoneInfo("Asia/Taipei")
BOT_USER = "__bot__"
CONTEXT_SECONDS = 180
_MAX_SENTENCE = 80

# 「下次會…」幾乎都是在講咪寶自己下次怎麼做；「以後／之後會」要有我／咪寶或開頭的
# 「好的／收到」才算，避免「台股之後會漲」這種預測。排程確認（「到時候會提醒你」）
# 由提醒功能自己做到，不列；答應改提醒、推播、清單的寫法照樣要列。
_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
_NEXT_TIME_RE = re.compile(r"下(?:次|回)(?:我|咪寶)?(?:都|就|一定)?(?:會|將)")
_LATER_RE = re.compile(
    r"(?:以後|之後|往後|今後|未來|下次|下回)(?:我|咪寶)?(?:都|就|一定)?(?:會|將|改|用|不會再|不再)"
)
# 「下次提醒會…」「以後推播會…」：時間詞和「會」中間夾了要改的東西。
_GAP_RE = re.compile(r"(?:下次|下回|以後|之後|往後|今後|未來)[^，,。！？!?\n]{1,8}?(?:會|將|改|都)")
_FIRST_PERSON_RE = re.compile(
    r"(?:我|咪寶)(?:之後|以後|下次)?(?:會|將)?(?:改|調整|修正|記得|記住|注意|把|用|放|換|加|拿掉)"
)
# 寧可多列：Andrew 會逐條核可。道歉、「已記下」也常是答應的開頭。
_ACK_RE = re.compile(
    r"^\s*(?:好的|好喔|好哦|好(?=[，,。！!～~\s])|收到|了解|明白|沒問題|OK|ok|知道了|"
    r"抱歉|對不起|不好意思|已記下|記下了|記住了)"
)
_SELF_RE = re.compile(r"我|咪寶")
_EXCLUDE_RE = re.compile(
    r"(?:到時候?|當天|前一天|準時|會再|會在[^，。]{0,12}?)(?:會)?提醒(?:你|您|妳|大家|全家|一次)?"
    r"|會(?:準時|再)?提醒(?:你|您|妳|大家|全家)"
)


@dataclass(frozen=True)
class Promise:
    created_at: int
    sentence: str
    quoted_bot_lines: tuple[str, ...]

    def render(self) -> str:
        when = datetime.fromtimestamp(self.created_at, _TW).strftime("%Y-%m-%d %H:%M")
        quoted = "、".join(f"「{line}」" for line in self.quoted_bot_lines) or "（前幾分鐘沒有引用咪寶的訊息）"
        return f"{when}｜{self.sentence}｜家人當時引用：{quoted}"


def promise_sentences(text: str) -> list[str]:
    """一則咪寶訊息裡答應未來改動的句子。"""
    found: list[str] = []
    for sentence in (s.strip() for s in _SENTENCE_RE.findall(text or "")):
        if not sentence or _EXCLUDE_RE.search(sentence):
            continue
        if (
            _NEXT_TIME_RE.search(sentence)
            or _FIRST_PERSON_RE.search(sentence)
            or (
                (_LATER_RE.search(sentence) or _GAP_RE.search(sentence))
                and (_SELF_RE.search(sentence) or _ACK_RE.search(sentence))
            )
        ):
            found.append(sentence[:_MAX_SENTENCE])
    return found


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:40]
    return ""


def scan(conn: sqlite3.Connection, *, since: int, until: int | None = None) -> list[Promise]:
    until = until if until is not None else int(time.time())
    rows = conn.execute(
        "SELECT group_id, message_id, text, created_at FROM raw_messages "
        "WHERE user_id = ? AND created_at BETWEEN ? AND ? ORDER BY created_at",
        (BOT_USER, since, until),
    ).fetchall()
    promises: list[Promise] = []
    seen: set[str] = set()
    for group_id, _message_id, text, created_at in rows:
        sentences = [s for s in promise_sentences(text) if s not in seen]
        if not sentences:
            continue
        quoted = conn.execute(
            "SELECT b.text FROM raw_messages AS u "
            "JOIN raw_message_quotes AS q ON q.group_id = u.group_id AND q.message_id = u.message_id "
            "JOIN raw_messages AS b ON b.group_id = q.group_id AND b.message_id = q.quoted_message_id "
            "WHERE u.group_id = ? AND u.user_id != ? AND b.user_id = ? "
            "AND u.created_at BETWEEN ? AND ? ORDER BY u.created_at",
            (group_id, BOT_USER, BOT_USER, int(created_at) - CONTEXT_SECONDS, int(created_at)),
        ).fetchall()
        lines = tuple(dict.fromkeys(line for (bot_text,) in quoted if (line := _first_line(bot_text))))
        for sentence in sentences:
            seen.add(sentence)
            promises.append(Promise(int(created_at), sentence, lines))
    return promises


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="line_bot.db 的副本（不要指向正式檔）")
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--now", type=int, default=None, help="測試用：現在的 epoch 秒")
    args = parser.parse_args(argv)
    now = args.now if args.now is not None else int(time.time())
    try:
        conn = sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True)
        promises = scan(conn, since=now - int(args.hours * 3600), until=now)
    except sqlite3.Error as exc:
        print(f"bot_promise_scan: 讀不到 DB 副本（{type(exc).__name__}）", file=sys.stderr)
        return 2
    if not promises:
        print("咪寶這段時間沒有答應新的改動。")
        return 0
    for promise in promises:
        print(promise.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
