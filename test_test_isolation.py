from pathlib import Path
import sqlite3

import pytest


def _make_raw_message_db(db_path: Path, category: str | None = None) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE raw_messages (
                group_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                user_id TEXT,
                text TEXT,
                created_at INTEGER,
                category TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO raw_messages "
            "(group_id, message_id, user_id, text, created_at, category) "
            "VALUES ('G1', 'M1', 'U1', 'hello', 1000, ?)",
            (category,),
        )


def _raw_message_category(db_path: Path) -> str | None:
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT category FROM raw_messages "
            "WHERE group_id='G1' AND message_id='M1'"
        ).fetchone()
    assert row is not None
    return row[0]


def test_autouse_fixture_aligns_sqlite_backends():
    import config
    import embedding_recall
    import memory
    import message_classifier

    expected = Path(config.settings.sqlite_path)

    assert memory._DB_PATH == expected
    assert embedding_recall._DB_PATH == expected
    assert message_classifier._DB_PATH == expected
    assert expected.name != "line_bot.db"


def test_collection_time_sqlite_path_is_quarantined():
    import conftest

    assert conftest._ORIG_MEMORY_DB_PATH == conftest._POST_TEST_SQLITE_PATH
    assert conftest._ORIG_FV_DB_PATH == conftest._POST_TEST_SQLITE_PATH
    assert conftest._POST_TEST_SQLITE_PATH.name.startswith(
        "line_bot_pytest_quarantine_"
    )


def test_extra_coverage_check_failure_fails_pytest():
    import test_extra_coverage

    old_pass = test_extra_coverage.PASS
    old_fail = test_extra_coverage.FAIL
    try:
        with pytest.raises(AssertionError):
            test_extra_coverage.check("intentional failure", False)
    finally:
        test_extra_coverage.PASS = old_pass
        test_extra_coverage.FAIL = old_fail


def test_message_classifier_async_keeps_spawn_time_db_path(monkeypatch, tmp_path):
    import message_classifier

    spawn_db = tmp_path / "spawn.db"
    restored_db = tmp_path / "restored.db"
    _make_raw_message_db(spawn_db)
    _make_raw_message_db(restored_db)

    targets = []

    class FakeThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            targets.append(self.target)

    monkeypatch.setattr(message_classifier.threading, "Thread", FakeThread)
    monkeypatch.setattr(message_classifier, "classify", lambda text: "其他")
    monkeypatch.setattr(message_classifier, "_DB_PATH", spawn_db)
    message_classifier._SCHEMA_ENSURED = False

    message_classifier.classify_async("G1", "M1", "hello")
    assert len(targets) == 1

    monkeypatch.setattr(message_classifier, "_DB_PATH", restored_db)
    message_classifier._SCHEMA_ENSURED = False
    targets[0]()

    assert _raw_message_category(spawn_db) == "其他"
    assert _raw_message_category(restored_db) is None


def test_memory_embedding_async_passes_spawn_time_db_path(monkeypatch, tmp_path):
    import embedding_recall
    import memory

    calls = []
    targets = []
    spawn_db = tmp_path / "spawn.db"
    restored_db = tmp_path / "restored.db"

    class FakeExecutor:
        def submit(self, target):
            targets.append(target)

    def fake_index_message(message_id, group_id, text, is_bot=False, db_path=None):
        calls.append(db_path)
        return True

    monkeypatch.setattr(memory, "_EMBED_EXECUTOR", FakeExecutor())
    monkeypatch.setattr(embedding_recall, "index_message", fake_index_message)
    monkeypatch.setattr(embedding_recall, "_DB_PATH", spawn_db)

    memory.log_raw_message("G1", "M1", "U1", "hello with enough text")
    assert len(targets) == 1

    monkeypatch.setattr(embedding_recall, "_DB_PATH", restored_db)
    targets[0]()

    assert calls == [spawn_db]


def test_knowledge_graph_async_passes_spawn_time_db_path(monkeypatch, tmp_path):
    import knowledge_graph

    calls = []
    targets = []
    spawn_db = tmp_path / "spawn.db"
    restored_db = tmp_path / "restored.db"

    class FakeThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            targets.append(self.target)

    def fake_store_triples(group_id, triples, source_text="", db_path=None):
        calls.append(db_path)
        return 1

    monkeypatch.setattr(knowledge_graph.threading, "Thread", FakeThread)
    monkeypatch.setattr(knowledge_graph, "extract_triples", lambda text: [("a", "b", "c")])
    monkeypatch.setattr(knowledge_graph, "store_triples", fake_store_triples)
    monkeypatch.setattr(knowledge_graph, "_DB_PATH", spawn_db)

    knowledge_graph.auto_extract_kg_async("G1", "Andrew 喜歡咖啡")
    assert len(targets) == 1

    monkeypatch.setattr(knowledge_graph, "_DB_PATH", restored_db)
    targets[0]()

    assert calls == [spawn_db]


def test_food_signals_async_passes_spawn_time_db_path(monkeypatch, tmp_path):
    import food_signals

    calls = []
    targets = []
    spawn_db = tmp_path / "spawn.db"
    restored_db = tmp_path / "restored.db"

    class FakeExecutor:
        def submit(self, target):
            targets.append(target)

    def fake_extract_and_store(group_id, source_msg_id, text, db_path=None):
        calls.append(db_path)
        return 1

    monkeypatch.setattr(food_signals, "_EXECUTOR", FakeExecutor())
    monkeypatch.setattr(food_signals, "extract_and_store", fake_extract_and_store)
    monkeypatch.setattr(food_signals.food_db, "_DB_PATH", spawn_db)

    food_signals.extract_and_store_async("G1", "M1", "想吃蘋果")
    assert len(targets) == 1

    monkeypatch.setattr(food_signals.food_db, "_DB_PATH", restored_db)
    targets[0]()

    assert calls == [spawn_db]


def test_food_extractor_async_passes_spawn_time_db_path(monkeypatch, tmp_path):
    import food_extractor
    import knowledge_graph

    calls = []
    targets = []
    spawn_db = tmp_path / "spawn.db"
    restored_db = tmp_path / "restored.db"

    class FakeExecutor:
        def submit(self, target):
            targets.append(target)

    def fake_extract_and_store(group_id, sender_alias, text, db_path=None):
        calls.append(db_path)
        return 1

    monkeypatch.setattr(food_extractor, "_EXECUTOR", FakeExecutor())
    monkeypatch.setattr(food_extractor, "extract_and_store", fake_extract_and_store)
    monkeypatch.setattr(knowledge_graph, "_DB_PATH", spawn_db)

    food_extractor.extract_async("G1", "媽媽", "我想吃蘋果")
    assert len(targets) == 1

    monkeypatch.setattr(knowledge_graph, "_DB_PATH", restored_db)
    targets[0]()

    assert calls == [spawn_db]


def test_finance_view_async_passes_spawn_time_db_path(monkeypatch, tmp_path):
    import finance_view_extractor

    calls = []
    targets = []
    spawn_db = tmp_path / "spawn.db"
    restored_db = tmp_path / "restored.db"

    class FakeThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            targets.append(self.target)

    def fake_insert_view(**kwargs):
        calls.append(kwargs.get("db_path"))
        return "view1"

    monkeypatch.setattr(finance_view_extractor.threading, "Thread", FakeThread)
    monkeypatch.setattr(finance_view_extractor, "is_finance_burst", lambda text: True)
    monkeypatch.setattr(
        finance_view_extractor,
        "extract",
        lambda text: [
            {
                "symbol_type": "ticker",
                "ticker": "TSMC",
                "macro_topic": None,
                "direction": "bull",
                "time_frame": "short",
                "horizon_days": 30,
                "target_price": None,
                "target_pct": None,
                "confidence": "mid",
                "condition_text": None,
                "speaker_hint": None,
                "raw_quote": "TSMC 會漲",
                "expires_at": "2026-07-01",
            }
        ],
    )
    monkeypatch.setattr(finance_view_extractor.finance_view_db, "insert_view", fake_insert_view)
    monkeypatch.setattr(finance_view_extractor.finance_view_db, "_DB_PATH", spawn_db)

    finance_view_extractor.maybe_extract_and_save_async("G1", "TSMC 會漲")
    assert len(targets) == 1

    monkeypatch.setattr(finance_view_extractor.finance_view_db, "_DB_PATH", restored_db)
    targets[0]()

    assert calls == [spawn_db]


def test_sqlite_teardown_uses_quarantine_not_production():
    import conftest
    import config
    import embedding_recall
    import memory
    import message_classifier

    conftest._restore_sqlite_paths()
    expected = conftest._POST_TEST_SQLITE_PATH

    assert Path(config.settings.sqlite_path) == expected
    assert memory._DB_PATH == expected
    assert embedding_recall._DB_PATH == expected
    assert message_classifier._DB_PATH == expected
    assert expected.name.startswith("line_bot_pytest_quarantine_")
