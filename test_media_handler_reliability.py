from __future__ import annotations

import sys
import subprocess
import asyncio
import fcntl
import threading
import time
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolate_media_delivery_locks(monkeypatch, tmp_path):
    import main

    monkeypatch.setattr(
        main, "_MEDIA_DELIVERY_LOCK_DIR", str(tmp_path / "delivery_locks")
    )
    monkeypatch.setattr(main, "_media_delivery_locks", {})
    monkeypatch.setattr(main, "_local_media_deliveries", {})


def test_vision_calls_use_one_worker_and_reject_concurrent_load(monkeypatch):
    import vision_llm

    load_started = threading.Event()
    release_load = threading.Event()
    load_threads: list[int] = []
    generate_threads: list[int] = []

    class FakeModel:
        config = object()

    def fake_load(_name):
        load_threads.append(threading.get_ident())
        load_started.set()
        assert release_load.wait(timeout=2)
        return FakeModel(), object()

    def fake_generate(*_args, **_kwargs):
        generate_threads.append(threading.get_ident())
        return "圖片內容：測試\n正方：可讀\n反方：有限\n統一論點：需查證"

    fake_mlx = types.ModuleType("mlx_vlm")
    fake_mlx.load = fake_load
    fake_mlx.generate = fake_generate
    fake_prompt = types.ModuleType("mlx_vlm.prompt_utils")
    fake_prompt.apply_chat_template = lambda *_args, **_kwargs: "prompt"
    monkeypatch.setitem(sys.modules, "mlx_vlm", fake_mlx)
    monkeypatch.setitem(sys.modules, "mlx_vlm.prompt_utils", fake_prompt)
    monkeypatch.setattr(vision_llm, "_model", None)
    monkeypatch.setattr(vision_llm, "_processor", None)
    monkeypatch.setattr(vision_llm, "_loaded_name", None)

    results: list[str | None] = []
    errors: list[BaseException] = []

    def call_vision():
        try:
            results.append(vision_llm.describe_image(b"fake-image"))
        except BaseException as exc:  # typed busy/timeout is part of the contract
            errors.append(exc)

    first = threading.Thread(target=call_vision)
    first.start()
    assert load_started.wait(timeout=1)
    second = threading.Thread(target=call_vision)
    second.start()
    time.sleep(0.05)
    release_load.set()
    first.join(timeout=2)
    second.join(timeout=2)

    third = threading.Thread(target=call_vision)
    third.start()
    third.join(timeout=2)

    assert len(load_threads) == 1
    assert len(generate_threads) == 2
    assert len(set(load_threads + generate_threads)) == 1
    assert len(results) == 2
    assert [type(exc).__name__ for exc in errors] == ["VisionBusyError"]


def test_image_empty_result_sends_visible_receipt_when_pending_disabled(monkeypatch):
    import main

    fake_media = types.ModuleType("media_pipeline")
    fake_media.analyze_image = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "media_pipeline", fake_media)
    monkeypatch.setattr(main, "_download_content", lambda _message_id: b"image-bytes")
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: False)

    replies: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda *args, **kwargs: replies.append((args, kwargs)),
    )
    event = SimpleNamespace(
        reply_token="reply-token",
        message=SimpleNamespace(id="image-message"),
    )

    main._handle_image_message(event, "group")

    assert len(replies) == 1
    args, kwargs = replies[0]
    assert args[0] == "reply-token"
    assert "稍後再傳一次" in args[1]
    assert kwargs["group_id"] == "group"
    assert kwargs["allow_push_fallback"] is False
    assert kwargs["include_auxiliary"] is False


def test_timed_out_vision_keeps_admission_until_native_call_finishes(monkeypatch):
    import vision_llm

    generate_started = threading.Event()
    release_generate = threading.Event()

    class FakeModel:
        config = object()

    fake_mlx = types.ModuleType("mlx_vlm")
    fake_mlx.load = lambda _name: (FakeModel(), object())

    def fake_generate(*_args, **_kwargs):
        generate_started.set()
        assert release_generate.wait(timeout=2)
        return "圖片內容：測試\n正方：可讀\n反方：有限\n統一論點：需查證"

    fake_mlx.generate = fake_generate
    fake_prompt = types.ModuleType("mlx_vlm.prompt_utils")
    fake_prompt.apply_chat_template = lambda *_args, **_kwargs: "prompt"
    monkeypatch.setitem(sys.modules, "mlx_vlm", fake_mlx)
    monkeypatch.setitem(sys.modules, "mlx_vlm.prompt_utils", fake_prompt)
    monkeypatch.setattr(vision_llm, "_model", None)
    monkeypatch.setattr(vision_llm, "_processor", None)
    monkeypatch.setattr(vision_llm, "_loaded_name", None)

    try:
        vision_llm.describe_image(b"slow", timeout_sec=0.02)
    except BaseException as exc:
        assert type(exc).__name__ == "VisionTimeoutError"
    else:
        raise AssertionError("slow native inference must exceed the caller deadline")
    assert generate_started.is_set()

    try:
        vision_llm.describe_image(b"must-not-queue", timeout_sec=0.1)
    except BaseException as exc:
        assert type(exc).__name__ == "VisionBusyError"
    else:
        raise AssertionError("admission must stay busy until native work really finishes")

    release_generate.set()
    deadline = time.monotonic() + 1
    while getattr(vision_llm, "_VISION_ADMISSION")._value == 0:
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_media_handler_overflow_replies_without_queueing(monkeypatch):
    import main

    slots = threading.BoundedSemaphore(2)
    assert slots.acquire(blocking=False)
    assert slots.acquire(blocking=False)
    monkeypatch.setattr(main, "_MEDIA_HANDLER_SLOTS", slots)
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: False)
    submit_calls: list[object] = []
    monkeypatch.setattr(
        main._MEDIA_EXECUTOR,
        "submit",
        lambda *_args, **_kwargs: submit_calls.append(object()),
    )
    replies: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda *args, **kwargs: replies.append((args, kwargs)),
    )
    event = SimpleNamespace(
        reply_token="reply-token",
        timestamp=int(time.time() * 1000),
        message=SimpleNamespace(id="image-message"),
    )

    accepted = main._submit_media_handler(
        main._handle_image_message, event, "group", "圖片"
    )

    assert accepted is False
    assert submit_calls == []
    assert len(replies) == 1
    assert replies[0][1]["include_auxiliary"] is False


def test_seven_image_burst_has_one_outcome_per_reply_token(monkeypatch):
    import main

    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(main, "_MEDIA_HANDLER_SLOTS", slots)
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: False)
    queued: list[object] = []
    monkeypatch.setattr(
        main._MEDIA_EXECUTOR,
        "submit",
        lambda fn: queued.append(fn) or SimpleNamespace(),
    )
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda reply_token, *_args, **_kwargs: replies.append(reply_token),
    )

    def terminal_handler(event, group_id, *, deadline_monotonic):
        main._reply_media_failure(event, group_id, "圖片", "synthetic terminal")

    events = [
        SimpleNamespace(
            reply_token=f"reply-{idx}",
            timestamp=int(time.time() * 1000),
            message=SimpleNamespace(id=f"image-{idx}"),
        )
        for idx in range(7)
    ]

    accepted = [
        main._submit_media_handler(terminal_handler, event, "group", "圖片")
        for event in events
    ]
    assert accepted == [True, True, False, False, False, False, False]
    assert len(queued) == 2
    for run in queued:
        run()

    assert sorted(replies) == [f"reply-{idx}" for idx in range(7)]
    assert len(set(replies)) == 7


def test_media_processing_claim_blocks_same_message_redelivery(monkeypatch, tmp_path):
    import memory

    monkeypatch.setattr(memory, "_DB_PATH", tmp_path / "memory.sqlite3")
    memory._init_db()

    assert memory.begin_inbound_event("group", "same-message") == "new"
    memory.mark_inbound_event_media_processing("group", "same-message")

    future_now = time.time() + memory._INBOUND_PROCESSING_LEASE_SECONDS + 1
    monkeypatch.setattr(
        memory._time,
        "time",
        lambda: future_now,
    )
    assert memory.begin_inbound_event("group", "same-message") == "processing"


def test_media_failure_receipt_removes_pending_only_after_confirmed_delivery(monkeypatch):
    import main

    event = SimpleNamespace(
        reply_token="reply-token",
        message=SimpleNamespace(id="same-message"),
    )
    removed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main,
        "_remove_pending_by_msg_id",
        lambda group_id, message_id: removed.append((group_id, message_id)),
    )
    monkeypatch.setattr(main, "_record_media_delivery_tombstone", lambda *_args: True)
    monkeypatch.setattr(main, "_reply", lambda *_args, **_kwargs: False)
    assert main._reply_media_failure(event, "group", "圖片", "test") is False
    assert removed == []

    monkeypatch.setattr(main, "_reply", lambda *_args, **_kwargs: True)
    assert main._reply_media_failure(event, "group", "圖片", "test") is True
    assert removed == [("group", "same-message")]

    event.message.id = "fence-failed-message"
    monkeypatch.setattr(main, "_record_media_delivery_tombstone", lambda *_args: False)
    assert main._reply_media_failure(event, "group", "圖片", "test") is True
    assert removed == [
        ("group", "same-message"),
        ("group", "fence-failed-message"),
    ]


def test_confirmed_image_reply_clears_pending_before_memory_bookkeeping(monkeypatch):
    import main

    fake_media = types.ModuleType("media_pipeline")
    fake_media.analyze_image = lambda *_args, **_kwargs: "圖片摘要"
    monkeypatch.setitem(sys.modules, "media_pipeline", fake_media)
    monkeypatch.setattr(main, "_download_content", lambda _message_id: b"image")
    monkeypatch.setattr(main, "_MEDIA_ANALYSIS_SLOT", threading.BoundedSemaphore(1))
    monkeypatch.setattr(main, "_reply", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        main.memory,
        "log_raw_message_meta",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("metadata sqlite unavailable")
        ),
    )
    order: list[str] = []
    monkeypatch.setattr(
        main,
        "_record_media_delivery_tombstone",
        lambda *_args: order.append("tombstone") or True,
    )
    monkeypatch.setattr(
        main,
        "_remove_pending_by_msg_id",
        lambda *_args: order.append("remove"),
    )
    monkeypatch.setattr(
        main.memory,
        "append_turn",
        lambda *_args: order.append("append-user"),
    )
    monkeypatch.setattr(main, "_append_bot_turn", lambda *_args: order.append("append-bot"))
    event = SimpleNamespace(
        reply_token="reply-token", message=SimpleNamespace(id="image-message")
    )

    main._handle_image_message(event, "group")

    assert order == ["tombstone", "remove", "append-user", "append-bot"]


def test_whole_video_pipeline_timeout_keeps_frames_until_background_finishes(
    monkeypatch, tmp_path
):
    import main
    import media_pipeline

    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    vision_started = threading.Event()
    release_vision = threading.Event()
    cleanup_finished = threading.Event()

    fake_keyframes = types.ModuleType("video_keyframes")
    fake_keyframes.extract_keyframes = lambda *_args, **_kwargs: [frame]

    def cleanup(frames):
        for path in frames:
            path.unlink(missing_ok=True)
        cleanup_finished.set()

    fake_keyframes.cleanup = cleanup
    fake_vision = types.ModuleType("vision_llm")

    def slow_chat(*_args, **_kwargs):
        vision_started.set()
        assert release_vision.wait(timeout=2)
        return "影片摘要"

    fake_vision.chat_with_images = slow_chat
    monkeypatch.setitem(sys.modules, "video_keyframes", fake_keyframes)
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)
    monkeypatch.setattr(media_pipeline, "_maybe_lookup_media_cache", lambda *_args: None)
    monkeypatch.setattr(media_pipeline, "_maybe_write_media_cache", lambda *_args: None)
    monkeypatch.setattr(main, "_download_content", lambda _message_id: b"video")
    monkeypatch.setattr(main, "_MEDIA_ANALYSIS_SLOT", threading.BoundedSemaphore(1))
    monkeypatch.setattr(main, "_reply", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(main, "_remove_pending_by_msg_id", lambda *_args: None)
    monkeypatch.setattr(main, "_record_media_delivery_tombstone", lambda *_args: True)
    event = SimpleNamespace(
        reply_token="reply-token", message=SimpleNamespace(id="video-message")
    )

    main._handle_video_message(
        event,
        "group",
        deadline_monotonic=(
            time.monotonic() + main._MEDIA_REPLY_SEND_RESERVE_SEC + 0.05
        ),
    )

    assert vision_started.is_set()
    assert frame.exists()
    assert not cleanup_finished.is_set()
    release_vision.set()
    assert cleanup_finished.wait(timeout=1)
    assert not frame.exists()


def test_failed_keyframe_extraction_cleans_byte_input_temp_files(monkeypatch, tmp_path):
    import video_keyframes

    raw_video = tmp_path / "input.mp4"
    frame_dir = tmp_path / "frames"

    class TempVideo:
        name = str(raw_video)

        def write(self, data):
            raw_video.write_bytes(data)

        def close(self):
            pass

    monkeypatch.setattr(
        video_keyframes.tempfile, "NamedTemporaryFile", lambda **_kwargs: TempVideo()
    )
    monkeypatch.setattr(
        video_keyframes.tempfile,
        "mkdtemp",
        lambda **_kwargs: str(frame_dir.mkdir() or frame_dir),
    )
    monkeypatch.setattr(
        video_keyframes.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("ffmpeg", 1)
        ),
    )

    assert video_keyframes.extract_keyframes(b"video") == []
    assert not raw_video.exists()
    assert not frame_dir.exists()


def test_reply_returns_confirmed_delivery_status(monkeypatch):
    import main

    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_get_quota_footer", lambda: "")
    monkeypatch.setattr(main, "_prepare_outbound_text", lambda text, **_kwargs: text)
    api = MagicMock()
    api.reply_message.return_value = SimpleNamespace(sent_messages=[])
    monkeypatch.setattr(main, "MessagingApi", lambda _client: api)
    api_client = MagicMock()
    api_client.__enter__.return_value = object()
    monkeypatch.setattr(main, "ApiClient", lambda _config: api_client)

    assert (
        main._reply(
            "reply-token",
            "已完成",
            group_id="group",
            allow_push_fallback=False,
            include_auxiliary=False,
        )
        is True
    )

    api.reply_message.side_effect = RuntimeError("ambiguous transport failure")
    assert (
        main._reply(
            "reply-token-2",
            "未確認",
            group_id="group",
            allow_push_fallback=False,
            include_auxiliary=False,
        )
        is False
    )
    api.push_message.assert_not_called()


def test_reply_stays_confirmed_when_local_inbound_mark_fails(monkeypatch):
    import main

    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_get_quota_footer", lambda: "")
    monkeypatch.setattr(main, "_prepare_outbound_text", lambda text, **_kwargs: text)
    monkeypatch.setattr(
        main.memory,
        "mark_inbound_event_replied",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("sqlite unavailable")),
    )
    api = MagicMock()
    api.reply_message.return_value = SimpleNamespace(sent_messages=[])
    monkeypatch.setattr(main, "MessagingApi", lambda _client: api)
    api_client = MagicMock()
    api_client.__enter__.return_value = object()
    monkeypatch.setattr(main, "ApiClient", lambda _config: api_client)
    main._register_inbound_reply_token("reply-token", "group", "message")

    assert (
        main._reply(
            "reply-token",
            "已完成",
            group_id="group",
            allow_push_fallback=False,
            include_auxiliary=False,
        )
        is True
    )
    api.push_message.assert_not_called()


def test_pending_store_add_unique_deduplicates_media(monkeypatch, tmp_path):
    import pending_store

    monkeypatch.setattr(pending_store, "BASE", tmp_path)
    monkeypatch.setattr(pending_store, "PENDING_PATH", tmp_path / "pending.json")
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / "pending.lock")

    first_media = tmp_path / "first.jpg"
    duplicate_media = tmp_path / "duplicate.jpg"
    first_media.write_bytes(b"one")
    duplicate_media.write_bytes(b"two")
    first = {"message_id": "same", "media_path": str(first_media)}
    duplicate = {"message_id": "same", "media_path": str(duplicate_media)}

    assert pending_store.add_unique("group", first) is True
    assert pending_store.add_unique("group", duplicate) is False
    assert len(pending_store.list_for_group("group")) == 1
    assert first_media.exists()
    assert not duplicate_media.exists()


def test_pending_media_drain_retries_locally_and_keeps_until_push_succeeds(
    monkeypatch, tmp_path
):
    import main
    import pending_store

    monkeypatch.setattr(main, "_PENDING_REPLY_ENABLED", True)
    monkeypatch.setattr(pending_store, "BASE", tmp_path)
    monkeypatch.setattr(pending_store, "PENDING_PATH", tmp_path / "pending.json")
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / "pending.lock")
    media_path = tmp_path / "saved.jpg"
    media_path.write_bytes(b"image")
    video_path = tmp_path / "saved.mp4"
    video_path.write_bytes(b"video")
    pending_store.add_unique(
        "group",
        {
            "type": "image",
            "message_id": "image-message",
            "media_path": str(media_path),
            "timestamp": time.time(),
        },
    )
    pending_store.add_unique(
        "group",
        {
            "type": "video",
            "message_id": "video-message",
            "media_path": str(video_path),
            "timestamp": time.time(),
        },
    )

    class Slot:
        def release(self):
            pass

    monkeypatch.setattr(main, "_try_acquire_drain_slot", lambda _group_id: Slot())
    monkeypatch.setattr(main, "_drop_stale_pending", lambda _group_id: [])
    monkeypatch.setattr(
        main,
        "_gemini_group_messages",
        lambda _items: (_ for _ in ()).throw(
            AssertionError("media metadata must not enter cloud grouping")
        ),
    )
    monkeypatch.setattr(main, "_run_media_analysis", lambda fn, _deadline: fn())
    monkeypatch.setattr(
        main,
        "_llm_chat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("private media must not use cloud LLM drain")
        ),
    )
    monkeypatch.setattr(main, "_prepare_outbound_text", lambda text, **_kwargs: text)
    monkeypatch.setattr(main, "_get_quota_footer", lambda: "")
    monkeypatch.setattr(main, "_is_system_status_outbound", lambda _text: False)
    monkeypatch.setattr(main.memory, "append_turn", lambda *_args: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *_args: None)
    fake_media = types.ModuleType("media_pipeline")
    result = {"image": None, "video": "本機影片摘要"}
    local_calls: list[str] = []

    def analyze_image(*_args, **_kwargs):
        local_calls.append("image")
        return result["image"]

    def analyze_video(*_args, **_kwargs):
        local_calls.append("video")
        return result["video"]

    fake_media.analyze_image = analyze_image
    fake_media.analyze_video = analyze_video
    monkeypatch.setitem(sys.modules, "media_pipeline", fake_media)
    messaging = MagicMock()
    monkeypatch.setattr(main, "MessagingApi", lambda _client: messaging)
    api_client = MagicMock()
    api_client.__enter__.return_value = object()
    monkeypatch.setattr(main, "ApiClient", lambda _config: api_client)

    assert main._drain_pending_for_group("group", source="test") is True
    assert len(pending_store.list_for_group("group")) == 2
    messaging.push_message.assert_not_called()
    assert local_calls == ["image"]

    result["image"] = "本機圖片摘要"
    class RetryConflict(RuntimeError):
        status = 409

    messaging.push_message.side_effect = RetryConflict("retry key already accepted")
    assert main._drain_pending_for_group("group", source="test") is True
    assert pending_store.list_for_group("group") == []
    assert messaging.push_message.call_count == 2
    retry_keys = [
        call.kwargs["x_line_retry_key"] for call in messaging.push_message.call_args_list
    ]
    assert retry_keys == [
        main._pending_push_retry_key("group", ["image-message"]),
        main._pending_push_retry_key("group", ["video-message"]),
    ]
    assert local_calls == ["image", "image", "video"]
    assert pending_store.was_media_delivered("group", "image-message")
    assert pending_store.was_media_delivered("group", "video-message")
    assert not media_path.exists()
    assert not video_path.exists()


@pytest.mark.parametrize(
    ("hard_exhausted", "local_media_only"),
    [(True, False), (False, True)],
)
def test_quota_limited_mixed_pending_drains_only_local_media(
    monkeypatch, tmp_path, hard_exhausted, local_media_only
):
    import main
    import pending_store

    monkeypatch.setattr(main, "_PENDING_REPLY_ENABLED", True)
    monkeypatch.setattr(pending_store, "BASE", tmp_path)
    monkeypatch.setattr(pending_store, "PENDING_PATH", tmp_path / "pending.json")
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / "pending.lock")
    image_path = tmp_path / "saved.jpg"
    image_path.write_bytes(b"image")
    pending_store.save_full(
        {
            "group": [
                {
                    "type": "text",
                    "message_id": "text-message",
                    "text": "private text",
                    "timestamp": time.time(),
                },
                {
                    "type": "image",
                    "message_id": "image-message",
                    "media_path": str(image_path),
                    "timestamp": time.time(),
                },
            ]
        }
    )

    class Slot:
        def release(self):
            pass

    monkeypatch.setattr(main, "_try_acquire_drain_slot", lambda _group_id: Slot())
    monkeypatch.setattr(main, "_drop_stale_pending", lambda _group_id: [])
    monkeypatch.setattr(main, "_quota_exhausted", lambda: hard_exhausted)
    monkeypatch.setattr(
        main,
        "_gemini_group_messages",
        lambda _items: (_ for _ in ()).throw(
            AssertionError("known quota outage must not cloud-group non-media")
        ),
    )
    monkeypatch.setattr(
        main,
        "_llm_chat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("known quota outage must retain non-media")
        ),
    )
    monkeypatch.setattr(main, "_run_media_analysis", lambda fn, _deadline: fn())
    monkeypatch.setattr(main, "_prepare_outbound_text", lambda text, **_kwargs: text)
    monkeypatch.setattr(main, "_get_quota_footer", lambda: "")
    monkeypatch.setattr(main, "_is_system_status_outbound", lambda _text: False)
    monkeypatch.setattr(main.memory, "append_turn", lambda *_args: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *_args: None)
    fake_media = types.ModuleType("media_pipeline")
    fake_media.analyze_image = lambda *_args, **_kwargs: "本機圖片摘要"
    monkeypatch.setitem(sys.modules, "media_pipeline", fake_media)
    messaging = MagicMock()
    monkeypatch.setattr(main, "MessagingApi", lambda _client: messaging)
    api_client = MagicMock()
    api_client.__enter__.return_value = object()
    monkeypatch.setattr(main, "ApiClient", lambda _config: api_client)

    assert (
        main._drain_pending_for_group(
            "group", source="test", local_media_only=local_media_only
        )
        is True
    )

    remaining = pending_store.list_for_group("group")
    assert [item["message_id"] for item in remaining] == ["text-message"]
    messaging.push_message.assert_called_once()
    assert pending_store.was_media_delivered("group", "image-message")
    assert not image_path.exists()


def test_tombstoned_pending_commit_failure_releases_delivery_claim(
    monkeypatch, tmp_path
):
    import main
    import pending_store

    monkeypatch.setattr(main, "_PENDING_REPLY_ENABLED", True)
    monkeypatch.setattr(pending_store, "BASE", tmp_path)
    monkeypatch.setattr(pending_store, "PENDING_PATH", tmp_path / "pending.json")
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / "pending.lock")
    image_path = tmp_path / "saved.jpg"
    image_path.write_bytes(b"image")
    pending_store.save_full(
        {
            "group": [
                {
                    "type": "image",
                    "message_id": "image-message",
                    "media_path": str(image_path),
                    "timestamp": time.time(),
                }
            ]
        }
    )

    class GroupSlot:
        def release(self):
            pass

    monkeypatch.setattr(main, "_try_acquire_drain_slot", lambda _group_id: GroupSlot())
    monkeypatch.setattr(main, "_drop_stale_pending", lambda _group_id: [])
    monkeypatch.setattr(main, "_was_media_delivery_tombstoned", lambda *_args: True)
    monkeypatch.setattr(
        main,
        "_commit_pending_removal",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("commit failed")),
    )

    assert main._drain_pending_for_group("group", source="test") is True
    assert [
        item["message_id"] for item in pending_store.list_for_group("group")
    ] == ["image-message"]

    claim = main._try_acquire_media_delivery_slot("group", "image-message")
    assert claim is not None
    claim.release()


def test_media_delivery_tombstone_is_private_hashed_and_blocks_redelivery(
    monkeypatch, tmp_path
):
    import main
    import pending_store

    monkeypatch.setattr(pending_store, "BASE", tmp_path)
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / "pending.lock")
    assert pending_store.mark_media_delivered("private-group", "private-message")
    tombstone_path = tmp_path / ".pending_media_state" / "delivered.json"
    stored = tombstone_path.read_text(encoding="utf-8")
    assert "private-group" not in stored
    assert "private-message" not in stored
    assert tombstone_path.stat().st_mode & 0o777 == 0o600

    class FakeGroupSource:
        group_id = "private-group"
        user_id = "private-user"

    class FakeImage:
        id = "private-message"

    class FakeEvent:
        source = FakeGroupSource()
        message = FakeImage()
        reply_token = "reply-token"
        delivery_context = SimpleNamespace(is_redelivery=True)

    monkeypatch.setattr(main, "MessageEvent", FakeEvent)
    monkeypatch.setattr(main, "GroupSource", FakeGroupSource)
    monkeypatch.setattr(main, "ImageMessageContent", FakeImage)
    monkeypatch.setattr(main, "_was_media_delivery_tombstoned", lambda *_args: True)
    begin_calls: list[object] = []
    monkeypatch.setattr(
        main.memory,
        "begin_inbound_event",
        lambda *_args: begin_calls.append(object()),
    )

    main._handle_event(FakeEvent())

    assert begin_calls == []


def test_pending_media_storage_uses_private_permissions(monkeypatch, tmp_path):
    import pending_store

    media_dir = tmp_path / "pending_media"
    monkeypatch.setattr(pending_store, "PENDING_MEDIA_DIR", media_dir)

    path = pending_store.write_pending_media(b"private", ".jpg")

    assert media_dir.stat().st_mode & 0o777 == 0o700
    assert __import__("pathlib").Path(path).stat().st_mode & 0o777 == 0o600


def test_media_delivery_slot_prevents_competing_failure_outcome(monkeypatch):
    import main

    slot = main._try_acquire_media_delivery_slot("group", "same-message")
    assert slot is not None
    replies: list[object] = []
    monkeypatch.setattr(main, "_reply", lambda *_args, **_kwargs: replies.append(object()))
    event = SimpleNamespace(
        reply_token="reply-token", message=SimpleNamespace(id="same-message")
    )
    try:
        assert main._reply_media_failure(event, "group", "圖片", "busy") is False
    finally:
        slot.release()
    assert replies == []


def test_media_delivery_slots_distinguish_full_message_identity():
    import main

    first = main._try_acquire_media_delivery_slot("group", "message-8")
    second = main._try_acquire_media_delivery_slot("group", "message-9")
    duplicate = main._try_acquire_media_delivery_slot("group", "message-8")
    assert first is not None
    assert second is not None
    assert duplicate is None
    first.release()
    second.release()
    assert main._media_delivery_locks == {}


def test_queued_handler_rechecks_tombstone_after_acquiring_delivery_slot(monkeypatch):
    import main

    event = SimpleNamespace(
        reply_token="reply-token", message=SimpleNamespace(id="same-message")
    )
    monkeypatch.setattr(main, "_was_media_delivery_tombstoned", lambda *_args: True)
    removed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main,
        "_remove_pending_by_msg_id",
        lambda group_id, message_id: removed.append((group_id, message_id)),
    )
    monkeypatch.setattr(
        main,
        "_handle_image_message_owned",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("tombstoned queued handler must not analyze")
        ),
    )

    main._handle_image_message(event, "group")

    assert removed == [("group", "same-message")]


def test_pending_gate_cache_is_separate_for_local_media_mode(monkeypatch):
    import main

    now = time.time()
    monkeypatch.setattr(
        main,
        "_global_gate_cache",
        {False: (now, False), True: (now, True)},
    )
    assert main._global_pending_drain_ready(allow_local_media=False) is False
    assert main._global_pending_drain_ready(allow_local_media=True) is True


def test_harden_and_orphan_sweep_preserve_referenced_and_young_files(
    monkeypatch, tmp_path
):
    import pending_store

    media_dir = tmp_path / "pending_media"
    media_dir.mkdir(mode=0o755)
    referenced = media_dir / "referenced.jpg"
    young_orphan = media_dir / "young.jpg"
    old_orphan = media_dir / "old.jpg"
    for path in (referenced, young_orphan, old_orphan):
        path.write_bytes(b"private")
        path.chmod(0o644)
    old_time = time.time() - 2 * 86400
    __import__("os").utime(old_orphan, (old_time, old_time))
    monkeypatch.setattr(pending_store, "BASE", tmp_path)
    monkeypatch.setattr(pending_store, "PENDING_MEDIA_DIR", media_dir)
    monkeypatch.setattr(pending_store, "PENDING_PATH", tmp_path / "pending.json")
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / "pending.lock")
    pending_store.save_full(
        {"group": [{"message_id": "one", "media_path": str(referenced)}]}
    )

    assert pending_store.harden_media_permissions() == 3
    assert pending_store.sweep_orphan_media() == 1

    assert media_dir.stat().st_mode & 0o777 == 0o700
    assert referenced.exists()
    assert referenced.stat().st_mode & 0o777 == 0o600
    assert young_orphan.exists()
    assert young_orphan.stat().st_mode & 0o777 == 0o600
    assert not old_orphan.exists()

    delivery_locks = tmp_path / ".pending_media_state" / "delivery_locks"
    delivery_locks.mkdir(parents=True)
    old_lock = delivery_locks / "old.lock"
    young_lock = delivery_locks / "young.lock"
    old_lock.write_text("")
    young_lock.write_text("")
    __import__("os").utime(old_lock, (old_time, old_time))
    with open(old_lock, "r+") as held_lock:
        fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX)
        assert pending_store.sweep_delivery_lock_files() == 0
        assert old_lock.exists()
        fcntl.flock(held_lock.fileno(), fcntl.LOCK_UN)
    assert pending_store.sweep_delivery_lock_files() == 1
    assert not old_lock.exists()
    assert young_lock.exists()


def test_lifespan_runs_storage_maintenance_before_pending_and_continues_on_error(
    monkeypatch
):
    import main
    import pending_store

    order: list[str] = []
    monkeypatch.delenv("JOBS_ROUTES_ENABLED", raising=False)
    monkeypatch.setattr(main.food_safety_client, "warm_cache_async", lambda: None)
    monkeypatch.setattr(
        pending_store,
        "harden_media_permissions",
        lambda: (_ for _ in ()).throw(RuntimeError("permission audit failed")),
    )
    monkeypatch.setattr(
        pending_store, "sweep_orphan_media", lambda: order.append("sweep") or 0
    )
    monkeypatch.setattr(
        pending_store,
        "sweep_delivery_lock_files",
        lambda: order.append("locks") or 0,
    )
    monkeypatch.setattr(
        main, "_process_pending_on_startup", lambda: order.append("pending")
    )
    monkeypatch.setattr(main, "_init_on_startup", lambda: order.append("init"))
    app = SimpleNamespace(state=SimpleNamespace())

    async def run_lifespan():
        async with main._app_lifespan(app):
            order.append("yield")

    asyncio.run(run_lifespan())

    # A hardening failure is logged, and startup still reaches pending/init.
    assert order == ["sweep", "locks", "pending", "init", "yield"]
