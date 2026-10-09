"""咪寶答應的改動要列出來給 Andrew 核可（2026-10-09）。內容全是合成的。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "jobs"))
import bot_promise_scan as scan  # noqa: E402

NOW = 2_000_000_000


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("好的，下次會把主詞放前面。", ["好的，下次會把主詞放前面。"]),
        ("好的，以後會用名字稱呼。", ["好的，以後會用名字稱呼。"]),
        ("我會把格式改成表格", ["我會把格式改成表格"]),
        ("收到，我會記住你不吃牛", ["收到，我會記住你不吃牛"]),
        ("台股之後會漲。", []),
        ("到時候會提醒你帶傘。", []),
        ("我會準時提醒大家。", []),
        # promises about how reminders look are exactly what must be listed
        ("好的，下次提醒會把主詞放前面。", ["好的，下次提醒會把主詞放前面。"]),
        ("好的，以後推播會把名字放前面", ["好的，以後推播會把名字放前面"]),
        ("收到！之後行事曆清單我會加上是誰的", ["之後行事曆清單我會加上是誰的"]),
        ("好像之後會下雨", []),
        ("好市多之後會漲價", []),
        ("對不起，以後會把主詞放前面。", ["對不起，以後會把主詞放前面。"]),
        ("不好意思，以後提醒都會寫清楚是誰。", ["不好意思，以後提醒都會寫清楚是誰。"]),
        ("已記下，以後提醒會把名字放前面。", ["已記下，以後提醒會把名字放前面。"]),
        ("收到，以後都用繁體中文回覆。", ["收到，以後都用繁體中文回覆。"]),
        ("了解，往後回覆改用繁體中文。", ["了解，往後回覆改用繁體中文。"]),
        ("沒問題，我之後改成先寫結論。", ["沒問題，我之後改成先寫結論。"]),
        ("這個列表裡的「自己」是指你喔。", []),
        ("以後會越來越冷，記得穿外套。", []),
    ],
)
def test_promise_sentences(text, found):
    assert scan.promise_sentences(text) == found


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "copy.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE raw_messages (group_id TEXT, message_id TEXT, user_id TEXT, text TEXT,
                                   created_at INTEGER, category TEXT);
        CREATE TABLE raw_message_quotes (group_id TEXT, message_id TEXT, quoted_message_id TEXT);
        """
    )
    rows = [
        ("G", "b-list", "__bot__", "目前待辦/提醒：\n1. 測試事項", NOW - 3600),
        ("G", "u-ask", "U_A", "主詞可以放前面嗎？", NOW - 60),
        ("G", "b-yes", "__bot__", "好的，下次會把主詞放前面。", NOW - 30),
        ("G", "b-old", "__bot__", "好的，下次會改。", NOW - 5 * 86400),
        ("G", "b-chat", "__bot__", "台股之後會漲。", NOW - 20),
    ]
    con.executemany("INSERT INTO raw_messages VALUES (?, ?, ?, ?, ?, NULL)", rows)
    con.execute("INSERT INTO raw_message_quotes VALUES ('G', 'u-ask', 'b-list')")
    con.commit()
    con.close()
    return path


def test_scan_lists_recent_promises_with_the_quoted_bot_line(db, capsys):
    assert scan.main(["--db", str(db), "--hours", "24", "--now", str(NOW)]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert "好的，下次會把主詞放前面。" in out[0]
    assert "「目前待辦/提醒：」" in out[0]
    assert "主詞可以放前面嗎" not in out[0]  # the family's own words are not printed


def test_scan_says_so_when_there_is_nothing(db, capsys):
    assert scan.main(["--db", str(db), "--hours", "0.001", "--now", str(NOW + 7200)]) == 0
    assert "沒有答應新的改動" in capsys.readouterr().out


def test_unreadable_db_is_an_error(tmp_path, capsys):
    assert scan.main(["--db", str(tmp_path / "missing.db")]) == 2
