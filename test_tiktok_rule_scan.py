"""每日維護數「可能叫咪寶忽略 TikTok 的規則」（2026-10-31，取代 test_prefetch.py::test_db_clean）。

DB 一律在 tmp_path 用 memory 的建表程式建（和正式 DB 同一套 schema），規則用正式的寫入函式
寫進去；內容全是合成的。絕不開正式的 line_bot.db。
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sqlite3
import time

import pytest

import burst_filter
import complaint_learning as cl
import memory
from jobs import tiktok_rule_scan as scan


def _line(verdict: str, *n: int) -> str:
    return (
        f"TikTok 忽略規則：{verdict}（persona_notes 會進提示詞：TikTok {n[0]}、忽略 {n[1]}；"
        f"persona_notes 稽核列：TikTok {n[2]}、忽略 {n[3]}；"
        f"filter_rules：skip 提到 TikTok {n[4]}、skip 會擋 TikTok 連結 {n[5]}、must_answer 提到 TikTok {n[6]}；"
        f"correction_rules（啟用中）：TikTok {n[7]}、忽略 {n[8]}）。"
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "copy.db"
    monkeypatch.setattr(memory, "_DB_PATH", path)
    memory._init_db()
    assert path.is_file()  # 建在暫存檔，不是正式 DB
    return path


def _run(path, capsys) -> tuple[int, str, str]:
    rc = scan.main(["--db", str(path)])
    captured = capsys.readouterr()
    return rc, captured.out.strip(), captured.err.strip()


def _copies_with_a_row_in_wal(db, tmp_path):
    """寫一列 TikTok 規則時留一條讀過的連線不關（memory 關連線就不會把 WAL 併回主檔），趁那列只在
    WAL 裡時 cp 兩份：一份只有主檔，一份照每日維護連 -wal 一起 cp。"""
    keeper = sqlite3.connect(db)
    try:
        keeper.execute("SELECT COUNT(*) FROM persona_notes").fetchone()
        memory.add_persona_note("G1", "correction", "x", "教訓：TikTok 連結一律忽略")
        wal = db.with_name(db.name + "-wal")
        assert wal.stat().st_size > 0
        main_only, with_wal = tmp_path / "main_only" / "a.db", tmp_path / "with_wal" / "a.db"
        for copy in (main_only, with_wal):
            copy.parent.mkdir()
            shutil.copy(db, copy)
        shutil.copy(wal, with_wal.with_name("a.db-wal"))
    finally:
        keeper.close()
    return main_only, with_wal


def _organic(summary: str) -> int:
    note_id = memory.add_organic_correction("G1", "看這個", "我不看", "要看啦", summary=summary)
    assert isinstance(note_id, int)  # 這個函式失敗時吞掉例外回 None
    return note_id


def _learned(rule: str) -> dict:
    now = int(time.time())
    return memory.record_learned_rule(
        "G1", f"judge:m{now}", rule=rule, core_reason="合成原因", category="only_verified",
        source_message_ids=[f"m{now}"], day_start=cl.day_start(now), now=now, observed_at=now,
        max_active=8, max_new_per_day=1, ttl_seconds=cl.LEARNED_TTL_SECONDS,
    )


def test_clean_db_says_so(db, capsys):
    memory.add_persona_note("G1", "example", "日常", "回覆先講結論")
    memory.add_filter_rule("G1", "must_answer", "幫我查")
    memory.add_filter_rule("G1", "skip", "早安圖")
    assert _run(db, capsys) == (0, _line("沒有", *[0] * 9), "")


def test_rules_that_could_ignore_tiktok_are_counted(db, capsys):
    memory.add_persona_note("G1", "correction", "使用者主動糾正", "教訓：TikTok 連結一律忽略")
    memory.add_persona_note("G2", "example", "抖音連結", "這種影片不用看")  # scenario 也會進提示詞
    memory.add_persona_note("G1", "correction", "每日回饋", "家人傳的長影片可以忽略")
    memory.add_persona_note("G1", "example", "x", "ＴｉｋＴｏｋ 不用回")  # 全形
    memory.add_persona_note("G1", "example", "x", "Tik Tok 的影片跳過")
    memory.add_persona_note("G1", "correction", "使用者主動糾正", "教訓：TikTok 影片也要點開", source="organic")  # 還沒連結
    _organic("TikTok 連結也要看內容")  # 稽核列＋一條啟用中的 canonical rule
    assert _learned("抖音的連結先忽略")["status"] == "created"  # 稽核列＋一條 learned rule
    memory.add_filter_rule("G1", "skip", "vt.tiktok.com")
    memory.add_filter_rule("G1", "skip", "https://vm.")  # 沒寫 tiktok，照樣擋掉 TikTok 連結
    memory.add_filter_rule("G2", "skip", "/video/")  # 被同群組的 must_answer 蓋過，不算擋
    memory.add_filter_rule("G2", "must_answer", "tiktok.com/@")
    memory.add_filter_rule("G1", "skip", "vt.")  # 另一個群組的 must_answer 管不到這裡
    memory.add_filter_rule("G2", "must_answer", "vt.")

    # 會進提示詞 5／2、稽核列 2／1、skip 提到 1、skip 會擋 3、must_answer 1、correction_rules 2／1
    assert _run(db, capsys) == (0, _line("要 Andrew 看", 5, 2, 2, 1, 1, 3, 1, 2, 1), "")


def test_notes_memory_sends_to_the_prompt_are_counted_as_prompt(db, monkeypatch):
    """memory 原文送進提示詞的 note，一條都不能算成稽核列（memory 的投影改了，這裡要先失敗）。"""
    monkeypatch.setenv("CORRECTION_CANONICAL_MEMORY_ENABLED", "true")  # scanner 的前提：canonical 糾正記憶開著
    memory.add_persona_note("G1", "example", "x", "回覆先講結論")
    memory.add_persona_note("G1", "correction", "x", "教訓：日期要寫清楚")
    memory.add_persona_note("G1", "correction", "x", "教訓：影片也要點開", source="organic")
    _organic("TikTok 連結也要看內容")
    assert _learned("抖音的連結先忽略")["status"] == "created"
    sent = {n["note_id"] for n in memory.list_persona_notes_for_prompt("G1") if "canonical_rule_id" not in n}
    with contextlib.closing(sqlite3.connect(db)) as c:
        rows = c.execute("SELECT note_id, kind, source, correction_linked FROM persona_notes").fetchall()
    counted = {note_id for note_id, kind, source, linked in rows if scan.reaches_prompt(kind, source, linked)}
    assert len(sent) == 3 and sent <= counted


def test_audit_rows_and_must_answer_rules_alone_do_not_ask_for_a_look(db, capsys):
    """不能刪的稽核列、`/必回 tiktok.com` 這種保護性規則只列數字：標題不能因為它們永遠是「要 Andrew 看」。"""
    _organic("TikTok 連結不要忽略")  # 保護性的糾正：稽核列裡常有「忽略」
    with contextlib.closing(sqlite3.connect(db)) as c:
        rule_ids = [r for (r,) in c.execute("SELECT rule_id FROM correction_rules")]
    assert memory.retire_correction_rules("G1", rule_ids, reason="合成") == rule_ids  # 退休的不進提示詞
    memory.add_filter_rule("G1", "must_answer", "tiktok.com")
    assert _run(db, capsys) == (0, _line("沒有", 0, 0, 1, 1, 0, 0, 1, 0, 0), "")


def test_output_is_only_numbers(db, capsys):
    memory.add_persona_note("GROUP_SECRET_X", "correction", "情境甲", "教訓：抖音的連結忽略就好")
    memory.add_filter_rule("GROUP_SECRET_X", "skip", "tiktok.com/@某人")
    assert _run(db, capsys) == (0, _line("要 Andrew 看", 1, 1, 0, 0, 1, 0, 0, 0, 0), "")  # 整行只有數字


def test_rows_still_in_the_wal_are_counted(db, tmp_path, capsys):
    """每日維護連 -wal 一起 cp：還沒併回主檔的列也要數到（immutable=1 會漏掉它們）。"""
    main_only, with_wal = _copies_with_a_row_in_wal(db, tmp_path)
    assert _run(main_only, capsys)[1] == _line("沒有", *[0] * 9)  # 那列真的只在 WAL
    assert _run(with_wal, capsys)[1] == _line("要 Andrew 看", 1, 1, *[0] * 7)


@pytest.mark.parametrize("content", [None, b"", b"this is not a sqlite database at all" * 100])
def test_unreadable_copy_is_an_error_not_a_clean_result(tmp_path, capsys, content):
    """test_db_clean 在 worktree 開到的就是 0 byte 檔：要報讀不到，不能說「沒有」。"""
    path = tmp_path / "copy.db"
    if content is not None:
        path.write_bytes(content)
    rc, out, err = _run(path, capsys)
    assert (rc, out) == (2, "")
    assert re.fullmatch(r"tiktok_rule_scan: 讀不到 DB 副本（\w+Error）", err)
    assert path.exists() == (content is not None)  # 不存在的檔不會被建出來


_REFUSED = "tiktok_rule_scan: 不讀正式 DB 或 bot 程式目錄裡的檔，請給暫存目錄裡的副本"


@pytest.mark.parametrize(
    "where",
    ["checkout", "checkout_state", "other_checkout", "other_checkout_state", "sqlite_path", "hard_link", "live_hard_link"],
)
def test_live_db_and_bot_checkouts_are_refused(tmp_path, monkeypatch, capsys, where):
    """正式 DB、這份或任何一份 bot checkout 底下的檔、SQLITE_PATH（含 hard link）都不讀：只收暫存目錄的副本。"""
    checkout = tmp_path / "line_bot"
    (checkout / "state").mkdir(parents=True)
    other = tmp_path / "another_clone"
    (other / "state").mkdir(parents=True)
    for name in ("main.py", "memory.py"):
        (other / name).write_text("", encoding="utf-8")
    configured = tmp_path / "configured.db"
    configured.write_bytes(b"")
    monkeypatch.setattr(scan, "BASE", checkout)
    monkeypatch.setenv("SQLITE_PATH", str(configured))
    target = {
        "checkout": checkout / "line_bot.db",
        "checkout_state": checkout / "state" / "copy.db",
        "other_checkout": other / "line_bot.db",
        "other_checkout_state": other / "state" / "copy.db",
        "sqlite_path": configured,
        "hard_link": tmp_path / "copies" / "copy.db",
        "live_hard_link": tmp_path / "copies" / "copy.db",
    }[where]
    if where in ("hard_link", "live_hard_link"):
        live = configured if where == "hard_link" else checkout / "line_bot.db"
        live.write_bytes(b"")
        target.parent.mkdir()
        os.link(live, target)
    elif not target.exists():
        target.write_bytes(b"")
    assert _run(target, capsys) == (2, "", _REFUSED)


def test_unexpected_errors_leave_one_line_on_stderr(capsys):
    assert _run("~no_such_user_for_tiktok_scan/copy.db", capsys) == (2, "", "tiktok_rule_scan: 掃描失敗（RuntimeError）")


def test_scan_does_not_write_to_the_copy(db, tmp_path, capsys):
    """副本的主檔和 -wal 一個 byte 都不變。WAL 不是空的：可寫的連線關掉時會把它併回主檔。
    （-shm 是讀者共用的鎖記號，唯讀連線本來就會碰。）"""
    _main_only, copy = _copies_with_a_row_in_wal(db, tmp_path)
    data = [copy, copy.with_name(copy.name + "-wal")]
    before = [p.read_bytes() for p in data]
    assert _run(copy, capsys)[0] == 0
    assert [p.read_bytes() if p.exists() else None for p in data] == before


_ALERTS = ("prompt_tiktok", "prompt_ignore", "skip_tiktok", "skip_blocks", "rule_tiktok", "rule_ignore")
_INFO = ("audit_tiktok", "audit_ignore", "must_tiktok")


@pytest.mark.parametrize("key", [*_ALERTS, *_INFO])
def test_each_number_decides_the_verdict_on_its_own(key):
    """6 個會進提示詞／會擋連結的數字，任何一個非 0 就要 Andrew 看；稽核列與 must_answer 只列數字。"""
    counts = dict.fromkeys((*_ALERTS, *_INFO), 0)
    counts[key] = 1
    verdict = "要 Andrew 看" if key in _ALERTS else "沒有"
    assert scan.render(counts).startswith(f"TikTok 忽略規則：{verdict}（")


@pytest.mark.parametrize("pattern", ["vt.", "vm.", "/video/", "/t/", "/photo/", "is_from_webapp", "_r=1"])
def test_a_skip_rule_that_drops_any_kind_of_tiktok_link_is_counted(db, capsys, pattern):
    """分享連結有這幾種樣子（兩種短網址、網頁版短網址、影片、圖文、分享參數）：擋掉任何一種都要報。"""
    memory.add_filter_rule("G1", "skip", pattern)
    assert _run(db, capsys) == (0, _line("要 Andrew 看", 0, 0, 0, 0, 0, 1, 0, 0, 0), "")


@pytest.mark.parametrize(
    "text", ["TikTok", "ＴｉｋＴｏｋ", "TIKTOK", "Tik Tok", "tik-tok", "Tik‧Tok", "Tik\u200bTok", "TİKTOK", "抖音", "抖 音", "Douyin"]
)
def test_spellings_of_tiktok_are_caught(text):
    assert scan.mentions_tiktok(text)


@pytest.mark.parametrize("text", ["忽略", "忽 略", "忽\u200b略"])
def test_spellings_of_ignore_are_caught(text):
    assert scan.mentions_ignore(text)


def test_similar_words_are_not_caught():
    assert not scan.mentions_tiktok("Tic Tac 和 TikTac 是別的東西")
    assert not scan.mentions_ignore("忽然想到、省略")


def test_scenario_is_read_too(db, capsys):
    memory.add_persona_note("G1", "example", "這種連結先忽略", "x")
    assert _run(db, capsys) == (0, _line("要 Andrew 看", 0, 1, 0, 0, 0, 0, 0, 0, 0), "")


@pytest.mark.parametrize(
    "rules",
    [
        [],
        [{"kind": "skip", "pattern": "vt.tiktok.com"}],
        [{"kind": "skip", "pattern": "https://"}, {"kind": "must_answer", "pattern": "@user"}],
        [{"kind": "skip", "pattern": "TikTok"}],  # 區分大小寫：網址是小寫，不會中
        [{"kind": "skip", "pattern": ""}, {"kind": "must_answer", "pattern": ""}],
        [{"kind": "must_answer", "pattern": "vm."}, {"kind": "skip", "pattern": "vm.tiktok"}],
        [{"kind": "skip", "pattern": "/video/"}, {"kind": "skip", "pattern": "abc123"}],
    ],
)
def test_rule_matching_is_the_burst_filters(rules):
    for text in (*scan.TIKTOK_URLS, "早安圖", "幫我看 https://vt.tiktok.com/x/"):
        assert scan._match_rules(text, rules) == burst_filter._match_rules(text, rules)
