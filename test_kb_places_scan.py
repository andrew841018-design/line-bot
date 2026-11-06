"""jobs/kb_places_scan：從 DB 副本找地點候選（2026-10-31，第一批段一）。

只用 tmp_path 裡的合成資料庫，不開正式 line_bot.db；例句都是合成的。
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "jobs"))
import kb_places_scan as scan  # noqa: E402

GROUP = "C" + "a" * 32
OTHER = "C" + "b" * 32
NOW = 1_793_400_000


@pytest.fixture(autouse=True)
def _no_real_production_db(monkeypatch, tmp_path):
    # 每個測試都先把「正式 DB」指到 tmp_path 裡不存在的檔：碰不到真的 line_bot.db。
    monkeypatch.setattr(scan, "PRODUCTION_DB", tmp_path / "missing" / "line_bot.db")
    # 暫存副本和清舊副本都在 tmp_path/work：不碰系統的暫存資料夾。
    (tmp_path / "work").mkdir()
    monkeypatch.setattr(scan, "_copy_root", lambda: tmp_path / "work")


@pytest.fixture
def workdirs(tmp_path, monkeypatch):
    """--from-production 開的暫存資料夾（導到 tmp_path/work，順便檢查是 0700）。"""
    root = tmp_path / "work"
    made: list[Path] = []
    real = tempfile.mkdtemp

    def spy(suffix=None, prefix=None, dir=None):
        if not (prefix or "").startswith(scan.COPY_PREFIX):
            return real(suffix, prefix, dir)
        path = real(suffix, prefix, str(root))
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o700
        made.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    return made


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "copy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE raw_messages (group_id TEXT, message_id TEXT, user_id TEXT, text TEXT, created_at INTEGER, category TEXT)"
    )
    conn.commit()

    def add(text, *, user="U" + "1" * 32, group=GROUP, message_id=None, ago=60):
        add.n += 1
        conn.execute(
            "INSERT INTO raw_messages VALUES (?, ?, ?, ?, ?, NULL)",
            (group, message_id or str(100000 + add.n), user, text, NOW - ago),
        )
        conn.commit()

    add.n = 0
    add.path = path
    yield add
    conn.close()


def _run(db, **kw):
    conn = sqlite3.connect(Path(db.path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return scan.scan(conn, since=NOW - 48 * 3600, until=NOW, **kw)
    finally:
        conn.close()


def test_statement_with_business_word_and_phone(db):
    db("甲麵館的電話 02-2345-6789 週一公休")
    [c] = _run(db)
    assert c.kind == "statement" and c.group_id == GROUP
    assert "2345" not in c.snippet and "[號碼]" in c.snippet


def test_statement_with_address_is_masked(db):
    db("乙咖啡在台北市中正區重慶南路一段122號")
    [c] = _run(db)
    assert c.kind == "statement" and "重慶南路" not in c.snippet and "[地址]" in c.snippet


def test_phone_clause_can_follow_the_shop_clause(db):
    db("甲麵館，電話 02-2345-6789")
    assert [c.kind for c in _run(db)] == ["statement"]


def test_question_needs_the_bot_to_be_addressed(db):
    db("@咪寶 甲麵館電話幾號？")
    db("甲麵館電話幾號？")
    db("@咪寶 那家火鍋電話？")
    assert [c.kind for c in _run(db)] == ["question", "question"]


def test_museum_counts_as_a_place(db):
    db("美術館電話 02-2345-6789")
    assert [c.kind for c in _run(db)] == ["statement"]


@pytest.mark.parametrize("text", [
    "/健康 爸爸 血壓",
    "@咪寶 /股票 2330",
    "甲麵館的帳號密碼是 0900000000",
    "卡號 4000 1234 5678 9012 在甲超市刷的",
    "台大醫院門診電話 02-2312-3456",
    "甲耳鼻喉科在台北市中正區重慶南路1段122號",
    "甲銀行電話 02-2345-6789",
    "我家在台北市中正區重慶南路1段122號，附近有超市",
    "阿姨家就在甲超市旁邊 中山路3號",
    "阿嬤家在甲市場後面 中山路3號",
    "我住在甲超市樓上 中山路3號",
    "新地址是甲超市隔壁 中山路3號",
    "送到甲超市 中山路3號",
    "我公司在甲百貨對面 中山路3號",
    "乙咖啡很好喝，我的手機 0900-000-000",
    "我在超商門口，打我手機 0900-000-000",
    "我的手機 0900-000-000",
    "甲麵館​ 02-2345-6789",
    "",
])
def test_skipped_messages(db, text):
    db(text)
    assert _run(db) == []


def test_bot_messages_are_skipped(db):
    db("甲麵館電話 02-2345-6789", user="__bot__")
    assert _run(db) == []


@pytest.mark.parametrize("text, hidden", [
    ("甲麵館 https://maps.example.com/place/0223456789 02-2345-6789", "0223456789"),
    ("甲麵館 goo.gl/maps/abc 02-2345-6789", "goo.gl"),
    ("甲麵館 lin.ee/abc 02-2345-6789", "lin.ee"),
    ("甲麵館在台北市中正區重慶南路1段122號 02-2345-6789 3點後打", "2345"),
    ("甲麵館在台北市中正區重慶南路1段122號 02-2345-6789 02-2345-6780", "6780"),
    ("甲麵館在台北市中正區重慶南路1段122號 1234-5678", "5678"),
])
def test_links_and_numbers_are_masked(db, text, hidden):
    db(text)
    found = _run(db)
    assert found and all(hidden not in c.snippet for c in found)


@pytest.mark.parametrize("text", ["甲麵館電話 02-2345-6789 3點後打", "甲麵館電話 02-2345-6789 02-2345-6780"])
def test_ambiguous_numbers_are_not_candidates(db, text):
    db(text)
    assert _run(db) == []


def test_emoji_with_joiners_is_still_scanned(db):
    db("甲麵館電話 02-2345-6789 👨‍👩‍👧")
    assert len(_run(db)) == 1


def test_skip_list_and_window(db):
    db("甲麵館電話 02-2345-6789", message_id="900001")
    db("乙麵館電話 02-2345-6780", ago=3 * 24 * 3600)
    assert _run(db, skip={(GROUP, "900001")}) == []


def test_two_shops_in_one_message_both_listed(db):
    db("甲麵館電話 02-2345-6789。乙咖啡在台北市中正區重慶南路1段122號")
    found = _run(db)
    assert len(found) == 2 and len({c.message_id for c in found}) == 1


def test_groups_are_kept_apart(db):
    db("甲麵館電話 02-2345-6789", group=GROUP)
    db("甲麵館電話 02-2345-6789", group=OTHER)
    assert sorted(c.group_id for c in _run(db)) == sorted([GROUP, OTHER])


def test_main_json_opens_read_only(db, capsys, monkeypatch):
    db("甲麵館電話 02-2345-6789")
    opened = []
    real_connect = sqlite3.connect

    def spy(target, *args, **kwargs):
        opened.append((str(target), kwargs.get("uri")))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(scan.sqlite3, "connect", spy)
    assert scan.main(["--db", str(db.path), "--now", str(NOW), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows and rows[0]["kind"] == "statement"
    assert opened == [(Path(db.path).resolve().as_uri() + "?mode=ro", True)]


def test_main_reports_nothing_found(db, capsys):
    db("今天天氣很好")
    assert scan.main(["--db", str(db.path), "--now", str(NOW)]) == 0
    assert "沒有地點候選" in capsys.readouterr().out


def test_main_missing_db(tmp_path):
    assert scan.main(["--db", str(tmp_path / "nope.db"), "--now", str(NOW)]) == 2


def test_main_bad_skip_file_scans_nothing(db, tmp_path, capsys):
    db("甲麵館電話 02-2345-6789")
    bad = tmp_path / "unsent.json"
    bad.write_text("{not json", encoding="utf-8")
    assert scan.main(["--db", str(db.path), "--skip-file", str(bad), "--now", str(NOW)]) == 2
    assert "甲麵館" not in capsys.readouterr().out


def test_main_guards(db, monkeypatch):
    assert scan.main(["--db", str(db.path), "--hours", "500", "--now", str(NOW)]) == 2
    monkeypatch.setattr(scan, "PRODUCTION_DB", Path(db.path))
    assert scan.main(["--db", str(db.path), "--now", str(NOW)]) == 2


def test_card_like_numbers_skip_the_whole_message(db):
    db("甲超市刷卡 4000 1234 5678 9012，電話 02-2345-6789")
    assert _run(db) == []


# ── --from-production：自己抄一份正式 DB（測試裡「正式 DB」都是 tmp_path 的合成檔）──
def _from_production(db, monkeypatch, *extra):
    monkeypatch.setattr(scan, "PRODUCTION_DB", Path(db.path))
    return scan.main(["--from-production", "--now", str(NOW), *extra])


def _files(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}


@pytest.fixture
def copy_attempts(monkeypatch):
    """抄了幾份（每份是主檔＋有的 -wal、-shm）。"""
    attempts: list[Path] = []
    real = scan._copy_db

    def spy(source, target):
        attempts.append(target)
        real(source, target)

    monkeypatch.setattr(scan, "_copy_db", spy)
    return attempts


def test_from_production_lists_the_same_candidates_as_db(db, monkeypatch, capsys, workdirs):
    db("甲麵館的電話 02-2345-6789 週一公休")
    db("@咪寶 那家火鍋電話？")
    assert scan.main(["--db", str(db.path), "--now", str(NOW)]) == 0  # 這時正式路徑還指在別處
    plain = capsys.readouterr().out
    assert len(plain.splitlines()) == 2
    assert _from_production(db, monkeypatch) == 0
    assert capsys.readouterr().out == plain
    assert [d.exists() for d in workdirs] == [False]


def test_from_production_leaves_the_source_alone(db, monkeypatch, capsys, workdirs):
    db("甲麵館電話 02-2345-6789")
    src = Path(db.path)
    before, mtime = _files(src.parent), src.stat().st_mtime_ns
    assert _from_production(db, monkeypatch, "--json") == 0
    assert json.loads(capsys.readouterr().out)[0]["kind"] == "statement"
    assert _files(src.parent) == before and src.stat().st_mtime_ns == mtime
    assert not any(Path(f"{src}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))
    assert [d.exists() for d in workdirs] == [False]


def test_from_production_sees_rows_still_only_in_the_wal(db, monkeypatch, capsys, workdirs):
    src = Path(db.path)
    writer = sqlite3.connect(src)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO raw_messages VALUES (?, ?, ?, ?, ?, NULL)",
            (GROUP, "700001", "U" + "1" * 32, "乙咖啡電話 02-2345-6780", NOW - 60),
        )
        writer.commit()  # 寫入方一直開著：這一列只在 -wal，主檔還沒有
        wal = Path(f"{src}-wal")
        assert "乙咖啡".encode() in wal.read_bytes() and "乙咖啡".encode() not in src.read_bytes()
        copied = []
        real_copy = scan._copy_private

        def spy(source, target):
            real_copy(source, target)
            assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
            copied.append(Path(source).name)

        monkeypatch.setattr(scan, "_copy_private", spy)
        before, mtime = _files(src.parent), src.stat().st_mtime_ns
        assert _from_production(db, monkeypatch, "--json") == 0
        # 在關掉寫入方之前比對：來源三個檔都沒變、旁邊沒有多出檔案。
        assert _files(src.parent) == before and src.stat().st_mtime_ns == mtime
        assert [r["message_id"] for r in json.loads(capsys.readouterr().out)] == ["700001"]
        assert copied == [src.name, f"{src.name}-wal", f"{src.name}-shm"]
    finally:
        writer.close()
    assert [d.exists() for d in workdirs] == [False]


def test_from_production_opens_only_the_copy_with_a_normal_connection(db, monkeypatch, capsys, workdirs):
    db("甲麵館電話 02-2345-6789")
    opened = []
    real_connect = sqlite3.connect

    def spy(target, *args, **kwargs):
        opened.append((str(target), args, kwargs))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(scan.sqlite3, "connect", spy)
    assert _from_production(db, monkeypatch) == 0
    # 只開過一次，開的是暫存資料夾裡的副本；一般連線（不是 mode=ro 的 URI）。
    [(target, args, kwargs)] = opened
    assert Path(target).parent == workdirs[0] and args == () and kwargs == {}


@pytest.mark.parametrize("results, code", [([False, False], 2), ([False, True], 0)])
def test_quick_check_not_ok_recopies_once(db, monkeypatch, capsys, workdirs, copy_attempts, results, code):
    db("甲麵館電話 02-2345-6789")
    checks = iter(results)
    monkeypatch.setattr(scan, "_quick_check_ok", lambda conn: next(checks))
    assert _from_production(db, monkeypatch) == code
    out, err = capsys.readouterr()
    assert len(copy_attempts) == 2 and next(checks, None) is None
    if code == 2:
        assert len(err.splitlines()) == 1 and "quick_check" in err and "甲麵館" not in out
    else:
        assert err == "" and "[號碼]" in out
    assert [d.exists() for d in workdirs] == [False]


def test_from_production_corrupt_source_is_not_scanned(tmp_path, monkeypatch, capsys, workdirs, copy_attempts):
    # 沒有替換 quick_check：真的開不起來（not a database）也算不是 ok，重抄一次後放棄。
    src = tmp_path / "garbage.db"
    src.write_bytes(b"not a database " * 300)
    monkeypatch.setattr(scan, "PRODUCTION_DB", src)
    assert scan.main(["--from-production", "--now", str(NOW)]) == 2
    assert len(copy_attempts) == 2 and "quick_check" in capsys.readouterr().err
    assert [d.exists() for d in workdirs] == [False]


@pytest.mark.parametrize("argv", [[], ["--db", "x.db", "--from-production"]])
def test_db_and_from_production_pick_exactly_one(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        scan.main([*argv, "--now", str(NOW)])
    assert exc.value.code == 2


def test_from_production_missing_source(capsys, workdirs):
    assert scan.main(["--from-production", "--now", str(NOW)]) == 2
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1 and "找不到" in err
    assert workdirs == []


def test_from_production_cleans_up_on_sigterm(db, monkeypatch, workdirs):
    # Bash 逾時、排程被停會送 SIGTERM：暫存副本也要刪掉，結束碼 128+15。
    import signal

    db("甲麵館電話 02-2345-6789")

    def killed(conn, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM 應該先讓掃描結束")

    monkeypatch.setattr(scan, "scan", killed)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as exc:
        _from_production(db, monkeypatch)
    assert exc.value.code == 128 + signal.SIGTERM
    assert [d.exists() for d in workdirs] == [False]
    assert signal.getsignal(signal.SIGTERM) is before


def test_from_production_removes_only_old_own_copies(db, monkeypatch, capsys, workdirs, tmp_path):
    db("甲麵館電話 02-2345-6789")
    root = tmp_path / "work"
    old, fresh, other = root / "kb_scan_old", root / "kb_scan_fresh", root / "other_old"
    for d in (old, fresh, other):
        d.mkdir()
        (d / "snapshot.db").write_bytes(b"x")
    target = tmp_path / "keep"
    target.mkdir()
    (target / "f").write_bytes(b"x")
    link = root / "kb_scan_link"
    link.symlink_to(target)
    (root / "kb_scan_file").write_bytes(b"x")
    stamp = scan.time.time() - scan.STALE_COPY_SECONDS - 60
    for path in (old, other, root / "kb_scan_file"):
        os.utime(path, (stamp, stamp))
    os.utime(link, (stamp, stamp), follow_symlinks=False)
    assert _from_production(db, monkeypatch) == 0
    assert not old.exists()
    assert fresh.exists() and other.exists() and (root / "kb_scan_file").exists()
    assert link.is_symlink() and (target / "f").exists()
