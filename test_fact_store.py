"""家人記憶的共用寫入路徑（2026-10-11 每日記憶）：fact_store、memory 的 tombstone 與
raw_messages 讀取、fact_hash。

名字與內容全是合成的；嵌入模型一律是假的（不載入 MiniLM）。
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import stat
import threading
import time

import pytest

import fact_filters
import fact_hash
import fact_store
import grounding_local
import memory

G = "G_FACT_STORE"
ORIGINAL_LOADED_EMBEDDER = fact_store.loaded_embedder  # conftest 會把它換成 lambda: None
ZWSP = chr(0x200B)  # 零寬空白（原始碼裡不放看不見的字）


@pytest.fixture(autouse=True)
def _aliases(tmp_path, monkeypatch):
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"U_A": "成員甲", "U_B": "成員乙"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(tmp_path / "no_roles.json"))
    return aliases


def _lock_path():
    return fact_store._lock_dir() / "fact_store.lock"


def _lock_is_free() -> bool:
    _lock_path().parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


class FakeEmbedder:
    """canonical 文字 → 向量；沒設定的文字各佔一個軸（和誰都 cos 0）。"""

    def __init__(self, pairs: dict[tuple[str, str], float] | None = None):
        self.vectors: dict[str, list[float]] = {}
        self.calls: list[list[str]] = []
        self.under_lock = False
        self._next_axis = 0
        for (a, b), cos in (pairs or {}).items():
            base = self._axis()
            other = self._axis()
            self.vectors[a] = base
            self.vectors[b] = [cos * x + math.sqrt(1 - cos * cos) * y for x, y in zip(base, other)]

    def _axis(self) -> list[float]:
        vector = [0.0] * 64
        vector[self._next_axis] = 1.0
        self._next_axis += 1
        return vector

    def encode(self, texts, **_kwargs):
        if not _lock_is_free():
            self.under_lock = True
        self.calls.append(list(texts))
        out = []
        for text in texts:
            key = fact_hash.canonical(text)
            if key not in self.vectors:
                self.vectors[key] = self._axis()
            out.append(self.vectors[key])
        return out


def _add(items, **kwargs):
    kwargs.setdefault("source", "inline")
    kwargs.setdefault("embedder", None)
    kwargs.setdefault("lock_mode", "try")
    return fact_store.add_person_facts(G, items, **kwargs)


def _ledger_lines():
    path = fact_store._ledger_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ── 正規化 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fact", "label", "body"),
    [
        ("成員甲：住在台中。", "成員甲", "住在台中"),
        ("成員甲:住在台中！", "成員甲", "住在台中"),
        ("（不確定是誰）：不吃香菜；", "成員甲", "不吃香菜"),
        ("興趣：爬山", "成員甲", "興趣:爬山"),  # 不是自己的稱呼：照留（NFKC 後冒號變半形）
        ("成員乙：住在台中", "成員甲", "成員乙:住在台中"),
        ("喜歡「三國」", "成員甲", "喜歡「三國」"),
        ("喜歡（貓）", "成員甲", "喜歡(貓)"),
        ("用 ｉＰｈｏｎｅ．", "成員甲", "用 iPhone"),
        ("住在\n台中", "成員甲", "住在 台中"),
        (f"住在{ZWSP}台中", "成員甲", "住在台中"),
        (f"成員甲：{ZWSP}喜歡貓{ZWSP}。", "成員甲", "喜歡貓"),
    ],
)
def test_facts_are_normalized_before_anything_else(fact, label, body):
    assert fact_store.normalize_fact(fact, label) == body


def test_a_stored_fact_is_label_plus_normalized_body():
    report = _add([("成員甲", "U_A", "成員甲：住在台中。")])
    assert report.added == 1
    assert memory.list_fact_rows(G) == [("U_A", "成員甲：住在台中")]


# ── 過濾、說話的人 ─────────────────────────────────────────────────────────


def test_filtered_facts_are_counted_by_category_only():
    report = _add([
        ("成員甲", "U_A", "在吃降血壓的藥"),
        ("成員甲", "U_A", "常打0900000000"),
        ("成員甲", "U_A", "跟成員乙同校"),
        ("成員甲", "", "住在台中"),
        ("", "U_A", "住在台中"),
        ("咪寶", "__bot__", "住在台中"),
        ("成員甲", "U_A", "喜歡爬山"),
    ])
    assert report.counts() == {
        "added": 1, "dup": 0, "tombstoned": 0, "capped": 0, "locked": 0, "error": 0,
        "rejected": {"contact": 1, "name": 1, "private": 1, "speaker": 3},
    }
    assert report.decisions is None


def test_the_job_refuses_to_write_without_a_name_list(_aliases):
    _aliases.unlink()
    with pytest.raises(fact_filters.NameSourceUnavailable):
        _add([("成員甲", "U_A", "喜歡爬山")], require_names=True, lock_mode="wait")
    assert memory.list_fact_rows(G) == []


# ── tombstone ──────────────────────────────────────────────────────────────


def test_a_forgotten_fact_is_not_added_back():
    _add([("成員甲", "U_A", "住在台中")])
    assert memory.remove_fact(G, "台中") == 1
    report = _add([("成員甲", "U_A", "住在台中。")])
    assert (report.added, report.tombstoned) == (0, 1)
    assert memory.list_fact_rows(G) == []
    # 別人說同一句不受影響
    assert _add([("成員乙", "U_B", "住在台中")]).added == 1


def test_clearing_memory_leaves_tombstones_for_every_fact():
    memory.add_fact(G, "成員甲：喜歡爬山", user_id="U_A")  # /記住 寫的也算
    memory.add_fact(G, "成員乙：不吃香菜", user_id="U_B")
    assert memory.clear_facts(G) == 2
    report = _add([("成員甲", "U_A", "喜歡爬山"), ("成員乙", "U_B", "不吃香菜")])
    assert (report.added, report.tombstoned) == (0, 2)


def test_tombstones_keep_hashes_not_text():
    memory.add_fact(G, "成員甲：住在台中", user_id="U_A")
    memory.remove_fact(G, "台中")
    with memory._conn() as c:
        rows = c.execute("SELECT group_id, user_id, body_hash FROM fact_tombstones").fetchall()
    assert len(rows) == 1
    assert rows[0][:2] == (G, "U_A") and "台中" not in rows[0][2] and len(rows[0][2]) == 64
    assert memory.is_fact_tombstoned(G, "U_A", "住在台中 。")
    assert not memory.is_fact_tombstoned(G, "U_B", "住在台中")


def test_forgetting_still_works_when_the_hash_key_is_lost(caplog):
    memory.fact_hash_key()  # 第一次用：建金鑰、記下指紋
    memory.add_fact(G, "成員甲：住在台中", user_id="U_A")
    fact_hash.key_path().unlink()
    assert memory.remove_fact(G, "台中") == 1  # 照常刪
    assert memory.list_fact_rows(G) == []
    with memory._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM fact_tombstones").fetchone()[0] == 0  # 不寫 tombstone
    assert not fact_hash.key_path().exists()  # 也沒有默默重建
    assert "FactHashKeyMismatch" in caplog.text and "台中" not in caplog.text


def test_forget_and_clear_return_values_are_unchanged():
    assert memory.remove_fact(G, "") == 0
    assert memory.remove_fact(G, "沒有這條") == 0
    assert memory.clear_facts(G) == 0
    memory.add_fact(G, "成員丁生日：01/02（國曆）")
    assert memory.remove_fact(G, "成員丁生日") == 1


# ── 去重 ───────────────────────────────────────────────────────────────────


def test_dedupe_without_a_model_uses_exact_match_and_containment():
    memory.add_fact(G, "成員甲：住在台中", user_id="U_A")
    report = _add([
        ("成員甲", "U_A", "住台中"),  # 字元全在「住在台中」裡
        ("成員甲", "U_A", "住在台中。"),  # 完全相同
        ("成員甲", "U_A", "住台南"),
        ("成員乙", "U_B", "住台中"),  # 別人的事實不互相比
    ])
    assert (report.added, report.dup) == (2, 2)


def test_dedupe_with_a_model_compares_bodies_only():
    embedder = FakeEmbedder({
        ("住在台中", "住在台中市"): 0.9,
        ("喜歡吃牛肉", "不吃牛肉"): 0.97,
        ("喜歡爬山", "喜歡爬高山"): 0.78,
        ("喜歡游泳", "愛去郊外走走"): 0.78,
    })
    memory.add_fact(G, "成員甲：住在台中", user_id="U_A")
    memory.add_fact(G, "成員甲：喜歡吃牛肉", user_id="U_A")
    memory.add_fact(G, "成員甲：喜歡爬山", user_id="U_A")
    memory.add_fact(G, "成員甲：喜歡游泳", user_id="U_A")
    report = _add(
        [
            ("成員甲", "U_A", "住在台中市"),  # cos 0.9 → 重複
            ("成員甲", "U_A", "不吃牛肉"),  # 只有一邊有否定詞 → 不算重複（是更正）
            ("成員甲", "U_A", "喜歡爬高山"),  # cos 0.78 且字元包含 → 重複
            ("成員甲", "U_A", "愛去郊外走走"),  # cos 0.78 但字元不包含 → 新的
        ],
        embedder=embedder,
        lock_mode="wait",
        source="daily",
    )
    assert (report.added, report.dup) == (2, 2)
    encoded = {text for call in embedder.calls for text in call}
    assert not any("成員甲" in text for text in encoded)  # 只編事實本體，不含「稱呼：」


def test_dedupe_inside_one_batch():
    embedder = FakeEmbedder({("住在台中", "住在台中市"): 0.9})
    report = _add(
        [("成員甲", "U_A", "住在台中"), ("成員甲", "U_A", "住在台中市"), ("成員甲", "U_A", "住在台中")],
        embedder=embedder,
    )
    assert (report.added, report.dup) == (1, 2)


def test_the_model_is_never_called_while_holding_the_lock_and_vectors_are_cached():
    embedder = FakeEmbedder()
    memory.add_fact(G, "成員甲：住在台中", user_id="U_A")
    _add([("成員甲", "U_A", "喜歡爬山")], embedder=embedder, lock_mode="wait")
    _add([("成員甲", "U_A", "喜歡游泳")], embedder=embedder, lock_mode="wait")
    assert not embedder.under_lock
    encoded = [text for call in embedder.calls for text in call]
    assert encoded.count("住在台中") == 1  # 第二批沒再編一次


def test_thresholds_can_be_overridden_from_the_environment(monkeypatch):
    embedder = FakeEmbedder({("住在台中", "住在台中市"): 0.9})
    memory.add_fact(G, "成員甲：住在台中", user_id="U_A")
    assert _add([("成員甲", "U_A", "住在台中市")], embedder=embedder, dry_run=True).dup == 1
    monkeypatch.setenv("LINE_BOT_FACT_DEDUPE_HIGH", "0.95")
    monkeypatch.setenv("LINE_BOT_FACT_DEDUPE_LOW", "0.95")
    assert _add([("成員甲", "U_A", "住在台中市")], embedder=embedder).added == 1


def test_a_broken_model_falls_back_inline_but_fails_the_job():
    class Broken:
        def encode(self, *_a, **_k):
            raise RuntimeError("synthetic")

    assert _add([("成員甲", "U_A", "喜歡爬山")], embedder=Broken(), lock_mode="try").added == 1
    with pytest.raises(fact_store.EmbedderUnavailable):
        _add([("成員甲", "U_A", "喜歡游泳")], embedder=Broken(), lock_mode="wait")
    assert len(memory.list_fact_rows(G)) == 1


def test_inline_uses_the_model_only_when_already_loaded(monkeypatch):
    monkeypatch.setattr(grounding_local, "_st_model", None)
    monkeypatch.setattr(grounding_local, "_get_st_model", lambda: pytest.fail("no cold load before a reply"))
    assert ORIGINAL_LOADED_EMBEDDER() is None
    sentinel = FakeEmbedder()
    monkeypatch.setattr(grounding_local, "_st_model", sentinel)
    assert ORIGINAL_LOADED_EMBEDDER() is sentinel


# ── 每人上限 ───────────────────────────────────────────────────────────────


def test_the_cap_counts_every_row_of_that_person(monkeypatch):
    monkeypatch.setenv("LINE_BOT_FACTS_PER_PERSON_MAX", "2")
    memory.add_fact(G, "成員甲：喜歡爬山", user_id="U_A")  # /記住 寫的也算
    report = _add([("成員甲", "U_A", "喜歡游泳"), ("成員甲", "U_A", "喜歡看書"), ("成員乙", "U_B", "喜歡看書")])
    assert (report.added, report.capped) == (2, 1)
    assert memory.count_person_facts(G, "U_A") == 2


def test_the_cap_reads_settings_and_ignores_typos(monkeypatch):
    monkeypatch.setattr(fact_store.settings, "facts_per_person_max_raw", "1")
    assert fact_store.per_person_max() == 1
    monkeypatch.setenv("LINE_BOT_FACTS_PER_PERSON_MAX", "sixty")
    assert fact_store.per_person_max() == fact_store.DEFAULT_PER_PERSON_MAX
    monkeypatch.setenv("LINE_BOT_FACTS_PER_PERSON_MAX", "0")
    assert fact_store.per_person_max() == fact_store.DEFAULT_PER_PERSON_MAX


# ── 寫入與 ledger ──────────────────────────────────────────────────────────


def test_writes_go_through_memory_add_fact(monkeypatch):
    calls = []
    monkeypatch.setattr(memory, "add_fact", lambda *a, **k: calls.append((a, k)) or True)
    assert _add([("成員甲", "U_A", "住在台中")]).added == 1
    assert calls == [((G, "成員甲：住在台中"), {"user_id": "U_A"})]


def test_the_ledger_is_0600_and_holds_an_hmac_not_the_fact():
    _add([("成員甲", "U_A", "住在台中")], source="daily", lock_mode="wait")
    path = fact_store._ledger_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    assert "台中" not in text and "成員甲" not in text
    (entry,) = _ledger_lines()
    assert set(entry) == {"ts", "source", "group_id", "user_id", "fact_hmac", "db_path"}
    assert entry["source"] == "daily" and entry["group_id"] == G and entry["user_id"] == "U_A"
    assert entry["fact_hmac"] == fact_hash.fact_hmac(G, "U_A", "成員甲：住在台中", key=memory.fact_hash_key())
    assert entry["db_path"] == str(memory._DB_PATH)


def test_a_failed_ledger_append_removes_the_row(monkeypatch, tmp_path):
    monkeypatch.setenv("LINE_BOT_FACT_LEDGER_PATH", str(tmp_path))  # 目錄：開不了檔
    report = _add([("成員甲", "U_A", "住在台中")])
    assert (report.added, report.error) == (0, 1)
    assert memory.list_fact_rows(G) == []


def test_dry_run_decides_everything_but_writes_nothing():
    memory.add_fact(G, "成員甲：住在台中", user_id="U_A")
    report = _add(
        [("成員甲", "U_A", "住在台中"), ("成員甲", "U_A", "喜歡爬山"), ("成員甲", "U_A", "在吃藥")],
        dry_run=True,
    )
    assert memory.list_fact_rows(G) == [("U_A", "成員甲：住在台中")]
    assert not fact_store._ledger_path().exists()
    assert report.decisions == [
        {"decision": "rejected", "category": "private"},  # 被擋下的不留文字
        {"decision": "dup", "person": "成員甲", "fact": "住在台中"},
        {"decision": "add", "person": "成員甲", "fact": "喜歡爬山"},
    ]
    assert report.added == 1


# ── 鎖 ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def held_lock():
    _lock_path().parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    released = threading.Event()

    def release():
        if not released.is_set():
            released.set()
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    yield release
    release()


def test_the_reply_path_never_waits_for_the_lock(held_lock):
    started = time.monotonic()
    report = _add([("成員甲", "U_A", "住在台中"), ("成員甲", "U_A", "在吃藥")], lock_mode="try")
    assert time.monotonic() - started < 1
    assert (report.locked, report.added, report.rejected) == (1, 0, {"private": 1})
    assert memory.list_fact_rows(G) == []


def test_the_job_waits_for_the_lock(held_lock):
    timer = threading.Timer(0.3, held_lock)
    timer.start()
    try:
        report = _add([("成員甲", "U_A", "住在台中")], lock_mode="wait")
    finally:
        timer.cancel()
    assert report.added == 1


def test_the_job_gives_up_after_the_wait(held_lock, monkeypatch):
    monkeypatch.setattr(fact_store, "LOCK_WAIT_SEC", 0.2)
    assert _add([("成員甲", "U_A", "住在台中")], lock_mode="wait").locked == 1


def test_an_unknown_lock_mode_is_rejected():
    with pytest.raises(ValueError):
        _add([("成員甲", "U_A", "住在台中")], lock_mode="block")


# ── memory：每日 job 讀 raw_messages ─────────────────────────────────────────


def _raw(message_id, user_id, text, created_at, group_id=G):
    with memory._conn() as c:
        c.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (group_id, message_id, user_id, text, created_at),
        )


def test_raw_messages_after_uses_a_tuple_cursor():
    _raw("m1", "U_A", "一", 100)
    _raw("m3", "U_A", "三", 100)
    _raw("m2", "U_B", "二", 100)
    _raw("m4", "U_A", "四", 101)
    _raw("m5", "U_A", "五", 160)
    _raw("other", "U_A", "別群", 100, group_id="G_OTHER")
    rows = memory.raw_messages_after(G, 100, "m1", 160, 10)
    assert [r[1] for r in rows] == ["m2", "m3", "m4"]
    assert rows[0] == (100, "m2", "U_B", "二")
    assert [r[1] for r in memory.raw_messages_after(G, 99, "", 200, 2)] == ["m1", "m2"]
    assert memory.raw_messages_after(G, 100, "", 161, 10)[0][1] == "m1"  # 初值 message_id ""


def test_raw_messages_stats_counts_and_finds_the_oldest():
    assert memory.raw_messages_stats(G) == (0, None)
    _raw("m1", "U_A", "一", 300)
    _raw("m2", "U_A", "二", 200)
    assert memory.raw_messages_stats(G) == (2, 200)


# ── fact_hash ──────────────────────────────────────────────────────────────


def _stored_fingerprint():
    with memory._conn() as c:
        row = c.execute("SELECT value FROM fact_hash_meta WHERE name = 'key_fingerprint'").fetchone()
    return row[0] if row else None


def test_the_hash_key_is_created_once_as_0600_and_its_fingerprint_recorded(caplog):
    path = fact_hash.key_path()
    assert not path.exists() and _stored_fingerprint() is None
    key = memory.fact_hash_key()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and len(path.read_bytes()) == 32
    assert _stored_fingerprint() == fact_hash.fingerprint(key) and len(_stored_fingerprint()) == 16
    assert "fact hash key created" in caplog.text
    assert memory.fact_hash_key() == key
    assert fact_hash.fact_hmac(G, "U_A", "x", key=key) != fact_hash.fact_hmac(G, "U_B", "x", key=key)


def test_an_existing_key_is_never_replaced():
    path = fact_hash.key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"k" * 32)
    assert fact_hash.create_secret() == b"k" * 32  # 另一個程序搶先建好時
    assert path.read_bytes() == b"k" * 32
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


def test_a_missing_key_with_a_recorded_fingerprint_is_not_recreated():
    memory.fact_hash_key()
    fact_hash.key_path().unlink()
    with pytest.raises(fact_hash.FactHashKeyMismatch):
        memory.fact_hash_key()
    assert not fact_hash.key_path().exists()


def test_a_replaced_key_is_refused():
    memory.fact_hash_key()
    fact_hash.key_path().write_bytes(b"x" * 32)
    with pytest.raises(fact_hash.FactHashKeyMismatch):
        memory.fact_hash_key()
    with pytest.raises(fact_hash.FactHashKeyMismatch):
        memory.is_fact_tombstoned(G, "U_A", "住在台中")


def test_a_database_without_a_fingerprint_adopts_the_existing_key():
    path = fact_hash.key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"k" * 32)  # 例如 DB 從舊備份還原：金鑰檔在、DB 沒有指紋
    key = memory.fact_hash_key()
    assert key == fact_hash.derive_key(b"k" * 32)
    assert _stored_fingerprint() == fact_hash.fingerprint(key)


def test_writes_stop_when_the_key_does_not_match():
    memory.fact_hash_key()
    fact_hash.key_path().write_bytes(b"x" * 32)
    with pytest.raises(fact_hash.FactHashKeyMismatch):
        _add([("成員甲", "U_A", "住在台中")])
    assert memory.list_fact_rows(G) == []
    assert not fact_store._ledger_path().exists()


def test_canonical_text_ignores_spacing_case_and_end_punctuation():
    assert fact_hash.canonical("用 iPhone。") == fact_hash.canonical("用iphone")
    assert fact_hash.canonical(f"住在{ZWSP}台中") == fact_hash.canonical("住在台中")
    assert fact_hash.canonical("喜歡「三國」") == "喜歡「三國」"


# ── 修正第一輪 #1：去重不能吞掉更正（替換守門） ─────────────────────────────

_CORRECTIONS = [  # (舊, 新, 實測 MiniLM cos)：只換了地點或日子，是更正不是重複
    ("住台中", "住台南", 0.911),
    ("在台中上班", "在台北上班", 0.82),
    ("每週日去游泳", "每週六去游泳", 0.89),
    ("喜歡狗", "喜歡貓", 0.9),
]
_STILL_DUPLICATES = [  # 只有一邊多了字：照樣是重複
    ("住在台北", "住台北", 0.95),
    ("常用 ChatGPT、Gemini 查資料", "常用 ChatGPT 查資料", 0.9),
]


@pytest.mark.parametrize(("old", "new", "cos"), _CORRECTIONS)
@pytest.mark.parametrize("with_model", [True, False])
def test_a_changed_place_or_day_is_a_correction_not_a_duplicate(old, new, cos, with_model):
    memory.add_fact(G, f"成員甲：{old}", user_id="U_A")
    embedder = FakeEmbedder({(fact_hash.canonical(old), fact_hash.canonical(new)): cos}) if with_model else None
    report = _add([("成員甲", "U_A", new)], embedder=embedder, lock_mode="wait")
    assert (report.added, report.dup) == (1, 0)


@pytest.mark.parametrize(("old", "new", "cos"), _STILL_DUPLICATES)
@pytest.mark.parametrize("with_model", [True, False])
def test_an_extra_word_on_one_side_is_still_a_duplicate(old, new, cos, with_model):
    memory.add_fact(G, f"成員甲：{old}", user_id="U_A")
    embedder = FakeEmbedder({(fact_hash.canonical(old), fact_hash.canonical(new)): cos}) if with_model else None
    report = _add([("成員甲", "U_A", new)], embedder=embedder, lock_mode="wait")
    assert (report.added, report.dup) == (0, 1)


_SYNONYM_REWRITES = [  # 只換了同義的字（實測 MiniLM ≥ 0.96）：有模型時是重複
    ("不愛吃虱目魚肚", "不喜歡吃虱目魚肚", 0.969),
    ("愛爬山", "喜歡爬山", 0.968),
    ("常去健身房", "常上健身房", 0.977),
]


@pytest.mark.parametrize(("old", "new", "cos"), _SYNONYM_REWRITES)
def test_a_synonym_swap_is_a_duplicate_when_the_model_can_tell(old, new, cos):
    memory.add_fact(G, f"成員甲：{old}", user_id="U_A")
    embedder = FakeEmbedder({(fact_hash.canonical(old), fact_hash.canonical(new)): cos})
    report = _add([("成員甲", "U_A", new)], embedder=embedder, lock_mode="wait")
    assert (report.added, report.dup) == (0, 1)


@pytest.mark.parametrize(("old", "new", "cos"), _SYNONYM_REWRITES)
def test_without_the_model_a_swap_is_kept_rather_than_risk_losing_a_correction(old, new, cos):
    memory.add_fact(G, f"成員甲：{old}", user_id="U_A")
    report = _add([("成員甲", "U_A", new)], embedder=None, lock_mode="wait")
    assert (report.added, report.dup) == (1, 0)


def test_the_substitution_rule_itself():
    assert fact_store._substituted("住台中", "住台南")
    assert fact_store._substituted("住在台中市", "住在台北市")
    assert not fact_store._substituted("住在台中", "住台中")  # 只有一邊多字
    assert not fact_store._substituted("喜歡爬山", "喜歡去郊外走走")  # 各自多的字超過 2 個
    assert not fact_store._substituted("住台中", "住台中")


# ── 修正第一輪小問題：稱呼也過聯絡資料檢查；看不見的字不能繞過 ─────────────────


def test_a_display_name_with_a_phone_number_is_not_stored():
    report = _add([("成員0900-000-000", "U_X", "喜歡爬山"), ("成員甲", "U_A", "喜歡爬山")])
    assert (report.added, report.rejected) == (1, {"label_contact": 1})
    assert memory.list_fact_rows(G) == [("U_A", "成員甲：喜歡爬山")]


def test_a_zero_width_variant_of_a_forgotten_fact_stays_forgotten():
    _add([("成員甲", "U_A", "住在台中")])
    assert memory.remove_fact(G, "台中") == 1
    report = _add([("成員甲", "U_A", f"住在{ZWSP}台中")])
    assert (report.added, report.tombstoned) == (0, 1)


def test_dry_run_keeps_no_text_for_anything_but_add_and_dup():
    report = _add(
        [("成員甲", "U_A", "常打0900000000"), ("成員甲", "U_A", "在吃藥"), ("", "U_A", "某件事"), ("成員甲", "U_A", "喜歡爬山")],
        dry_run=True,
    )
    texts = json.dumps(report.decisions, ensure_ascii=False)
    assert "0900000000" not in texts and "吃藥" not in texts and "某件事" not in texts
    assert {"decision": "add", "person": "成員甲", "fact": "喜歡爬山"} in report.decisions
