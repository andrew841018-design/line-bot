import importlib

import pytest

G = "C_family_poll_test"
FATHER = "U38f817726f256ec1fdfa51cf57f4a645"
MOTHER = "U9fde03d0fe1e0669eccc8b9b4ecc28a6"


@pytest.fixture()
def family_poll_db(tmp_path, monkeypatch):
    import config

    db_path = tmp_path / "family_poll.db"
    monkeypatch.setattr(config.settings, "sqlite_path", str(db_path))
    import family_poll

    monkeypatch.setattr(family_poll, "_DB_PATH", db_path)
    family_poll.init_db()
    family_poll.clear_group(G)
    yield family_poll
    family_poll.clear_group(G)
    importlib.invalidate_caches()


def test_natural_sentence_creates_poll_with_all(family_poll_db):
    out = family_poll_db.handle_natural_message(
        G,
        "爸爸想知道，今天晚上有誰可以去吃凱薩",
        user_id=FATHER,
        source_msg_id="m1",
    )

    assert out is not None
    assert out.startswith("@all 民調開好了")
    assert "今天晚上有誰可以去吃凱薩？" in out
    assert "可以 0 人" in out


def test_sender_short_reply_updates_active_poll(family_poll_db):
    family_poll_db.handle_natural_message(
        G,
        "爸爸想知道，今天晚上有誰可以去吃凱薩",
        user_id=FATHER,
        source_msg_id="m1",
    )

    out = family_poll_db.handle_natural_message(
        G,
        "可以",
        user_id=FATHER,
        source_msg_id="m2",
    )

    assert out is not None
    assert "爸爸 → 可以" in out
    assert "可以 1 人：爸爸" in out


def test_named_mixed_votes_in_one_message(family_poll_db):
    family_poll_db.handle_command(
        G,
        "/民調 今天晚上吃凱薩誰可以",
        user_id=FATHER,
        source_msg_id="m1",
    )

    out = family_poll_db.handle_natural_message(
        G,
        "媽媽不行、爸爸可以",
        user_id=MOTHER,
        source_msg_id="m2",
    )

    assert out is not None
    assert "媽媽 → 不行" in out
    assert "爸爸 → 可以" in out
    assert "可以 1 人：爸爸" in out
    assert "不行 1 人：媽媽" in out


def test_first_person_and_named_person_update_together(family_poll_db):
    family_poll_db.create_poll(G, "今晚吃凱薩誰可以", user_id=FATHER)

    out = family_poll_db.handle_natural_message(
        G,
        "我跟媽媽可以",
        user_id=FATHER,
        source_msg_id="m2",
    )

    assert out is not None
    assert "爸爸 → 可以" in out
    assert "媽媽 → 可以" in out
    assert "可以 2 人" in out


def test_no_active_poll_short_reply_is_ignored(family_poll_db):
    assert (
        family_poll_db.handle_natural_message(
            G,
            "可以",
            user_id=FATHER,
            source_msg_id="m1",
        )
        is None
    )


def test_active_poll_does_not_capture_general_food_chat(family_poll_db):
    family_poll_db.create_poll(G, "今晚吃凱薩誰可以", user_id=FATHER)

    assert (
        family_poll_db.handle_natural_message(
            G,
            "今天吃什麼",
            user_id=FATHER,
            source_msg_id="m2",
        )
        is None
    )


def test_active_poll_does_not_capture_named_question(family_poll_db):
    family_poll_db.create_poll(G, "今晚吃凱薩誰可以", user_id=FATHER)

    assert (
        family_poll_db.handle_natural_message(
            G,
            "爸爸可以嗎？",
            user_id=MOTHER,
            source_msg_id="m2",
        )
        is None
    )


def test_status_nudge_and_close_commands(family_poll_db):
    created = family_poll_db.handle_command(
        G,
        "/民調 今天晚上吃凱薩誰可以",
        user_id=FATHER,
        source_msg_id="m1",
    )
    assert created is not None and created.startswith("@all")

    status = family_poll_db.handle_command(G, "/民調")
    assert status is not None and status.startswith("民調：")
    assert not status.startswith("@all")

    nudge = family_poll_db.handle_command(G, "/催民調")
    assert nudge is not None and nudge.startswith("@all")

    closed = family_poll_db.handle_command(G, "/關閉民調")
    assert closed is not None and "已關閉民調" in closed
    assert family_poll_db.get_active_poll(G) is None
