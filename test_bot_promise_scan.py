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
        # 2026-10-10 review：「！」把開頭的「好的／收到／抱歉」切成另一句、句子裡有「會提醒你」
        # 就整句丟——這幾句以前都漏列。
        ("好的！以後會把主詞放前面。", ["以後會把主詞放前面。"]),
        ("收到！之後會先寫主詞。", ["之後會先寫主詞。"]),
        ("我知道了！以後會把主詞放前面。", ["以後會把主詞放前面。"]),
        # 2026-10-10: a family member's words the bot quotes are not its promise,
        # and long quotes are not copied into the list; short ones stay.
        ("有收到更正，但找不到「我以後會把房子賣掉那一則喔」這句話。", []),
        ("好的，以後「自己」會改成寫名字。", ["好的，以後「自己」會改成寫名字。"]),
        ("好的，以後會照「這段要寫在最前面的那一則說明」改。", ["好的，以後會照「…」改。"]),
        ("抱歉！以後提醒都會先寫是誰。", ["以後提醒都會先寫是誰。"]),
        ("好的，以後會提醒大家的時候把主詞放前面。", ["好的，以後會提醒大家的時候把主詞放前面。"]),
        ("下次會提醒你時先寫是誰！", ["下次會提醒你時先寫是誰！"]),
        # 排程確認只丟那個子句，前後的承諾照列
        ("好，到時候會提醒你，也會把主詞放前面。", ["也會把主詞放前面。"]),
        ("好的，下次會改，到時候會提醒你。", ["好的，下次會改"]),
        # 純排程確認仍不列（提醒功能自己做到）；排程字樣跨過「、」也算
        ("好的，明天早上8點會提醒你", []),
        ("收到，到時候會提醒大家", []),
        ("好的，當天會再提醒一次", []),
        ("好的，下次會在週一、週三提醒你", []),
        # 預測：沒有我／咪寶，也沒有開頭附和詞
        ("台股之後會漲", []),
        ("明天之後會下雨", []),
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


@pytest.mark.parametrize(
    "text",
    ["抱歉，我不會再把日期寫錯了。", "好的，我會盡量簡短回覆。", "了解，我會更注意主詞。",
     "好的，接下來我都會先列重點。", "明白！接下來會用條列。", "謝謝提醒！以後會先寫是誰。",
     "下次我一定先查清楚再說。", "好的，我會更新資料。",
     # 第 5 輪審查
     "收到，之後會調整。", "了解，之後會修正。", "好的，之後會更新清單。",
     "謝謝你的建議，我以後會改成先寫日期。", "收到你的建議，下次會先寫是誰。"],
)
def test_more_ways_of_promising_are_listed(text):
    # 2026-10-10 review（第三輪）
    assert scan.promise_sentences(text)


@pytest.mark.parametrize(
    "text",
    ["了解！台股之後會回升的機率不高。", "好的！明天天氣晴，之後會轉雨，記得帶傘。",
     "好的！建議你之後改用 /提醒 指令。", "抱歉！我把時間記錯了。", "上次是週三，下次會是週五。",
     "OK，健保費明年起會改成新的費率。", "收到。颱風之後會轉向北方。"],
)
def test_forecasts_advice_and_apologies_are_not_promises(text):
    assert scan.promise_sentences(text) == []


@pytest.mark.parametrize(
    "text",
    ["好的，媽媽說“我以後會把房子賣掉”。", "好的，你說的「我懷孕了。以後會把房子賣掉」我先不記。",
     "好的，以後提到「我懷孕了」這類事我會先問過再記。", '好的，你說"以後我會早點回家"，我記下了。'],
)
def test_family_words_in_quotes_never_reach_the_list(text):
    # 2026-10-10 review（第三輪）：引號裡是家人的話，不算咪寶答應，也不能寫進清單。
    listed = " ".join(scan.promise_sentences(text))
    assert "房子" not in listed and "懷孕" not in listed and "早點回家" not in listed
