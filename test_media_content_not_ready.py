"""LINE content API 回 202（影片還在轉檔）時，_download_content 等轉檔而不是 bytes(None)。"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest


class _FakeApiClient:
    def __init__(self, _config):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _install_blob(monkeypatch, contents, statuses):
    import main

    calls = {"content": 0, "status": 0}

    class FakeBlob:
        def __init__(self, _api_client):
            pass

        def get_message_content(self, _message_id):
            calls["content"] += 1
            return contents.pop(0) if contents else None

        def get_message_content_transcoding_by_message_id(self, _message_id):
            calls["status"] += 1
            status = statuses.pop(0) if statuses else "processing"
            if isinstance(status, Exception):
                raise status
            return SimpleNamespace(status=status)

    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApiBlob", FakeBlob)
    monkeypatch.setattr(main, "_get_line_config", lambda: object())
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)
    return calls


def test_ready_content_is_returned_without_status_check(monkeypatch):
    import main

    calls = _install_blob(monkeypatch, [bytearray(b"video")], [])
    assert main._download_content("m1") == b"video"
    assert calls == {"content": 1, "status": 0}


def test_waits_for_transcoding_then_downloads(monkeypatch):
    import main

    calls = _install_blob(
        monkeypatch, [None, None, bytearray(b"video")], ["processing", "succeeded"]
    )
    assert main._download_content("m1") == b"video"
    assert calls == {"content": 3, "status": 2}


def test_transcoding_failed_raises_typed_error(monkeypatch):
    import main

    _install_blob(monkeypatch, [None], ["failed"])
    with pytest.raises(main._MediaContentNotReadyError, match="transcoding failed"):
        main._download_content("m1")


def test_gives_up_at_wait_cap_instead_of_bytes_none(monkeypatch):
    import main

    monkeypatch.setattr(main, "_MEDIA_CONTENT_WAIT_SEC", 0.0)
    _install_blob(monkeypatch, [], [])
    with pytest.raises(main._MediaContentNotReadyError, match="transcoding processing"):
        main._download_content("m1")


def test_status_lookup_error_keeps_polling_then_gives_up(monkeypatch):
    import main

    monkeypatch.setattr(main, "_MEDIA_CONTENT_WAIT_SEC", 0.0)
    _install_blob(monkeypatch, [], [RuntimeError("boom")])
    with pytest.raises(main._MediaContentNotReadyError, match="transcoding unknown"):
        main._download_content("m1")


def test_video_handler_reports_content_not_ready(monkeypatch, tmp_path):
    import main

    monkeypatch.setattr(main, "_MEDIA_DELIVERY_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(main, "_media_delivery_locks", {})
    monkeypatch.setattr(main, "_local_media_deliveries", {})
    monkeypatch.setattr(main, "_MEDIA_ANALYSIS_SLOT", threading.BoundedSemaphore(1))

    def not_ready(_message_id):
        raise main._MediaContentNotReadyError("LINE content not ready (transcoding processing)")

    reasons: list[str] = []
    monkeypatch.setattr(main, "_download_content", not_ready)
    monkeypatch.setattr(
        main,
        "_reply_media_failure",
        lambda _event, _group, _name, reason, **_kw: reasons.append(reason) or True,
    )
    event = SimpleNamespace(reply_token="reply-token", message=SimpleNamespace(id="video-message"))

    main._handle_video_message_owned(event, "group", deadline_monotonic=time.monotonic() + 30)

    assert reasons == ["content not ready"]
