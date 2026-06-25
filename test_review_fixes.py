from __future__ import annotations

import os
from pathlib import Path


os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")


def test_health_lock_status_uses_effective_allowed_group_ids(monkeypatch):
    import main

    monkeypatch.setattr(main.settings, "allowed_group_id", "")
    monkeypatch.setattr(main.settings, "allowed_group_ids_raw", "G1,G2")

    payload = main.health()

    assert payload["group_locked"] is True
    assert payload["allowed_group_count"] == 2


def test_webhook_logging_does_not_print_raw_line_signature():
    import main

    source = Path(main.__file__).read_text(encoding="utf-8")

    assert "sig={x_line_signature}" not in source
    assert "body={body" not in source
    assert "sig_sha256" in source


def test_line_api_calls_do_not_use_startup_token_snapshot():
    import main

    source = Path(main.__file__).read_text(encoding="utf-8")

    assert "_line_config =" not in source
    assert "ApiClient(_line_config)" not in source


def test_memory_embedding_indexing_is_bounded():
    import memory

    source = Path(memory.__file__).read_text(encoding="utf-8")

    assert "ThreadPoolExecutor" in source
    assert "_EMBED_INFLIGHT" in source
    assert "threading.Thread(target=_bg_index" not in source


def test_preflight_uses_passive_wal_checkpoint():
    import preflight_check

    source = Path(preflight_check.__file__).read_text(encoding="utf-8")

    assert "PRAGMA wal_checkpoint(PASSIVE)" in source
    assert "PRAGMA wal_checkpoint(TRUNCATE)" not in source
