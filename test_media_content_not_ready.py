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


def _install_blob(monkeypatch, contents, statuses, *, wait_allowed=True):
    import main

    calls = {"content": 0, "status": 0}
    timeouts = {"content": [], "status": []}

    class FakeBlob:
        def __init__(self, _api_client):
            pass

        def get_message_content(self, _message_id, **kwargs):
            calls["content"] += 1
            timeouts["content"].append(kwargs.get("_request_timeout"))
            return contents.pop(0) if contents else None

        def get_message_content_transcoding_by_message_id(self, _message_id, **kwargs):
            calls["status"] += 1
            timeouts["status"].append(kwargs.get("_request_timeout"))
            status = statuses.pop(0) if statuses else "processing"
            if isinstance(status, Exception):
                raise status
            return SimpleNamespace(status=status)

    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApiBlob", FakeBlob)
    monkeypatch.setattr(main, "_get_line_config", lambda: object())
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)
    monkeypatch.setattr(main, "_content_wait_allowed", lambda: wait_allowed)
    calls["timeouts"] = timeouts
    return calls


def test_ready_content_is_returned_without_status_check(monkeypatch):
    import main

    calls = _install_blob(monkeypatch, [bytearray(b"video")], [])
    assert main._download_content("m1") == b"video"
    assert (calls["content"], calls["status"]) == (1, 0)


def test_waits_for_transcoding_then_downloads(monkeypatch):
    import main

    calls = _install_blob(
        monkeypatch, [None, None, bytearray(b"video")], ["processing", "succeeded"]
    )
    assert main._download_content("m1") == b"video"
    assert (calls["content"], calls["status"]) == (3, 2)


def test_line_calls_carry_timeouts(monkeypatch):
    import main

    calls = _install_blob(monkeypatch, [None, bytearray(b"video")], ["succeeded"])
    main._download_content("m1")
    assert calls["timeouts"]["content"] == [main._LINE_CONTENT_TIMEOUT] * 2
    assert calls["timeouts"]["status"] == [main._LINE_TRANSCODING_STATUS_TIMEOUT]


def test_no_wait_outside_the_media_worker(monkeypatch):
    import main

    calls = _install_blob(monkeypatch, [None], [], wait_allowed=False)
    with pytest.raises(main._MediaContentNotReadyError, match="no wait outside the media worker"):
        main._download_content("m1")
    assert (calls["content"], calls["status"]) == (1, 0)


def test_wait_allowed_only_on_the_media_analysis_worker():
    import main

    assert main._content_wait_allowed() is False  # the test thread, like the webhook thread
    assert main._MEDIA_ANALYSIS_EXECUTOR.submit(main._content_wait_allowed).result(timeout=5) is True


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


def test_status_lookup_error_is_recorded_by_class_then_gives_up(monkeypatch):
    import main

    monkeypatch.setattr(main, "_MEDIA_CONTENT_WAIT_SEC", 0.0)
    _install_blob(monkeypatch, [], [RuntimeError("secret detail")])
    with pytest.raises(main._MediaContentNotReadyError, match=r"transcoding unknown \(RuntimeError\)") as info:
        main._download_content("m1")
    assert "secret detail" not in str(info.value)


def _video_handler_setup(monkeypatch, tmp_path):
    import main

    monkeypatch.setattr(main, "_MEDIA_DELIVERY_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(main, "_media_delivery_locks", {})
    monkeypatch.setattr(main, "_local_media_deliveries", {})
    monkeypatch.setattr(main, "_MEDIA_ANALYSIS_SLOT", threading.BoundedSemaphore(1))
    reasons: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply_media_failure",
        lambda _event, _group, _name, reason, **_kw: reasons.append(reason) or True,
    )
    event = SimpleNamespace(reply_token="reply-token", message=SimpleNamespace(id="video-message"))
    return event, reasons


def test_video_handler_reports_content_not_ready(monkeypatch, tmp_path):
    import main

    event, reasons = _video_handler_setup(monkeypatch, tmp_path)

    def not_ready(_message_id):
        raise main._MediaContentNotReadyError("LINE content not ready (transcoding processing)")

    monkeypatch.setattr(main, "_download_content", not_ready)
    main._handle_video_message_owned(event, "group", deadline_monotonic=time.monotonic() + 30)

    assert reasons == ["content not ready"]


def _slow_download(monkeypatch):
    import main

    monkeypatch.setattr(main, "_MEDIA_CONTENT_POLL_SEC", 0.01)

    def download(_message_id):
        time.sleep(0.02)  # took longer than one poll: the transcoding wait happened
        return b"video"

    monkeypatch.setattr(main, "_download_content", download)


def test_video_gives_up_after_a_wait_when_too_little_time_is_left(monkeypatch, tmp_path):
    import main
    import media_pipeline

    event, reasons = _video_handler_setup(monkeypatch, tmp_path)
    _slow_download(monkeypatch)
    analysed: list[bytes] = []
    monkeypatch.setattr(media_pipeline, "analyze_video", lambda data, **_kw: analysed.append(data))
    # analysis deadline = reply deadline - send reserve ≈ 10 s away, under _VIDEO_MIN_ANALYSIS_SEC
    main._handle_video_message_owned(
        event, "group", deadline_monotonic=time.monotonic() + main._MEDIA_REPLY_SEND_RESERVE_SEC + 10
    )

    assert analysed == []
    assert reasons == ["analysis capacity busy or timed out"]


def test_video_still_analysed_after_a_wait_with_enough_time(monkeypatch, tmp_path):
    import main
    import media_pipeline

    event, reasons = _video_handler_setup(monkeypatch, tmp_path)
    _slow_download(monkeypatch)
    analysed: list[bytes] = []
    monkeypatch.setattr(
        media_pipeline, "analyze_video", lambda data, **_kw: analysed.append(data) or None
    )
    main._handle_video_message_owned(
        event, "group", deadline_monotonic=time.monotonic() + main._MEDIA_REPLY_SEND_RESERVE_SEC + 40
    )

    assert analysed == [b"video"]
    assert reasons == ["analysis returned empty"]
