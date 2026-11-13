"""2026-11-07: a printed silence instruction stays out of the family group on every path.

Andrew 2026-11-06: 「以後不要傳這種廢話，我寧可你不回」.  verify-claims checks burst, @ and research
replies in _enforce_new_value_reply.  These are the others: pictures, video, voice, quoted media,
files and the pending queue.  Each test goes in through the handler with the real _reply and a fake
LINE API, so what reaches the fake is what the family would see.  All fixtures are synthetic.
The fixed lines a file used to get when it had nothing to send are here too.
"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import main  # noqa: E402
import media_pipeline  # noqa: E402
import memory  # noqa: E402
import pending_store  # noqa: E402
import reply_policy  # noqa: E402
import reply_provenance  # noqa: E402
from quote_context import original_block, with_current_reply  # noqa: E402
from test_support_timing import cpu_seconds  # noqa: E402

GROUP = "GROUP_SILENCE"
TOKEN = "TOKEN_SILENCE"
MESSAGE = "MSG_SILENCE"
QUOTED = "QUOTED_SILENCE"
REAL_ANALYZE_IMAGE = media_pipeline._analyze_image

# The image contract's sentence on its own has no correction or advice in it, so a picture nobody
# asked about loses it in unsolicited_image_reply either way.  This one gets past that.
PRINTED_ONE = "這張圖沒有要糾正的，也沒有建議，兩者都沒有就輸出空字串。"
PRINTED = [
    "兩者都沒有就輸出空字串。",
    PRINTED_ONE,
    "沒有實質判斷、答案或新資訊時輸出空字串，不用圖片描述或摘要補位。",
    "這段只是閒聊，不需要補充，所以輸出空字串。",
    "只有抽樣畫面而沒有可查證的糾正、新資訊或建議時輸出空字串；",
    "两者都没有就输出空字符串。",  # the local vision model leans to simplified Chinese
    "這段只是閒聊，所以輸出為空字串。",
]
ORDINARY = "先核對資料日期，再判斷要不要改建議。"
HOW_TO = "值是 None 的時候就輸出空字串。"
ASKS = "空字串要怎麼處理"


class FakeBot:
    """Every model the paths can reach says ``model_says``; ``sent()`` is what LINE was given."""

    def __init__(self, monkeypatch, tmp_path):
        self.model_says = ""
        self.tmp_path = tmp_path
        self.api = MagicMock()
        self.api.reply_message.return_value = SimpleNamespace(sent_messages=[])
        self.api.push_message.return_value = SimpleNamespace(sent_messages=[])
        client = MagicMock()
        client.__enter__.return_value = object()
        monkeypatch.setattr(main, "MessagingApi", lambda _client: self.api)
        monkeypatch.setattr(main, "ApiClient", lambda _config: client)
        monkeypatch.setattr(main.settings, "bot_muted", False)
        monkeypatch.setattr(main, "_get_quota_footer", lambda: "")
        monkeypatch.setattr(main, "_download_content", lambda _message_id: b"media-bytes")
        monkeypatch.setattr(main, "_get_persona_notes", lambda *_a: [])
        monkeypatch.setattr(main, "_maybe_extract_facts", lambda *_a: None)
        monkeypatch.setattr(main, "_MEDIA_DELIVERY_LOCK_DIR", str(tmp_path / "delivery_locks"))
        monkeypatch.setattr(main, "_media_delivery_locks", {})
        monkeypatch.setattr(main, "_local_media_deliveries", {})
        monkeypatch.setattr(main, "_PENDING_REPLY_ENABLED", True)
        monkeypatch.setattr(pending_store, "BASE", tmp_path)
        monkeypatch.setattr(pending_store, "PENDING_PATH", tmp_path / "pending.json")
        monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / "pending.lock")

        def says(*_a, **_k):
            return self.model_says

        def gemini(*_a, **_k):  # both clients turn what is_empty_marker knows into ""
            return "" if reply_policy.is_empty_marker(self.model_says) else self.model_says

        def claude(user_input, *_a, **_k):
            return gemini() if isinstance(user_input, str) else None  # no Part reaches it

        monkeypatch.setattr("claude_client.chat", claude)
        monkeypatch.setattr("gemini_client.chat", gemini)
        monkeypatch.setattr(media_pipeline, "_analyze_image", says)
        monkeypatch.setattr(media_pipeline, "_maybe_lookup_media_cache", lambda *_a, **_k: None)
        monkeypatch.setattr(media_pipeline, "_maybe_write_media_cache", lambda *_a, **_k: None)
        monkeypatch.setitem(sys.modules, "video_keyframes", SimpleNamespace(
            extract_keyframes=lambda *_a, **_k: ["frame"], cleanup=lambda *_a: None,
        ))
        monkeypatch.setitem(sys.modules, "vision_llm", SimpleNamespace(chat_with_images=says))
        monkeypatch.setitem(sys.modules, "local_llm", SimpleNamespace(chat=says))

    def sent(self) -> list[str]:
        calls = self.api.reply_message.call_args_list + self.api.push_message.call_args_list
        return [str(getattr(message, "text", "")) for call in calls for message in call.args[0].messages]

    def event(self, file_name: str = "", *, token_known: bool = True) -> SimpleNamespace:
        assert memory.begin_inbound_event(GROUP, MESSAGE) == "new"
        if token_known:
            main._register_inbound_reply_token(TOKEN, GROUP, MESSAGE)
        return SimpleNamespace(
            reply_token=TOKEN,
            message=SimpleNamespace(id=MESSAGE, file_name=file_name),
            source=SimpleNamespace(type="group", group_id=GROUP, user_id="U_TEST_A"),
            timestamp=int(time.time() * 1000),
        )

    def is_finished(self) -> bool:
        return memory.get_inbound_event_status(GROUP, MESSAGE) == "completed_no_reply"

    def saved(self, name: str) -> str:
        path = self.tmp_path / name
        path.write_bytes(b"media-bytes")
        return str(path)


@pytest.fixture
def bot(monkeypatch, tmp_path):
    return FakeBot(monkeypatch, tmp_path)


@pytest.fixture(autouse=True)
def _fresh_provenance():
    reply_provenance.reset()
    yield
    reply_provenance.reset()


@contextmanager
def _quota_spent(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(main, "_quota_exhausted", lambda: True)
        yield


def _quota(fn):
    @wraps(fn)
    def run(bot, monkeypatch, request=""):
        with _quota_spent(monkeypatch):
            fn(bot, monkeypatch, request)
    return run


def _media_unavailable(monkeypatch):
    def fail(_message_id):
        raise RuntimeError("gone")

    monkeypatch.setattr(main, "_download_content", fail)
    monkeypatch.setattr(main.memory, "get_raw_message_meta", lambda *_a: {"description": "舊摘要"})


def _pdf(monkeypatch, *, text: str):
    page = SimpleNamespace(extract_text=lambda: text)
    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(
        PdfReader=lambda _file: SimpleNamespace(pages=[page]),
    ))
    pixmap = SimpleNamespace(tobytes=lambda _format: b"png")
    document = MagicMock()
    document.__len__.return_value = 1
    document.__getitem__.return_value = SimpleNamespace(get_pixmap=lambda **_k: pixmap)
    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(open=lambda **_k: document))


# ── what each path does with a message ───────────────────────────────────────


def direct_image(bot, monkeypatch, request=""):
    main._handle_image_message(bot.event(), GROUP)


def direct_video(bot, monkeypatch, request=""):
    main._handle_video_message(bot.event(), GROUP)


def voice(bot, monkeypatch, request=""):
    main._handle_audio_message(bot.event(), GROUP)


def quoted_image(bot, monkeypatch, request=""):
    main._handle_media_via_quote(bot.event(), GROUP, request, QUOTED, "[圖片]")


def quoted_video(bot, monkeypatch, request=""):
    main._handle_media_via_quote(bot.event(), GROUP, request, QUOTED, "[影片]")


@_quota
def quoted_video_quota_spent(bot, monkeypatch, request=""):
    main._handle_media_via_quote(bot.event(), GROUP, request, QUOTED, "[影片]")


def quoted_voice(bot, monkeypatch, request=""):
    main._handle_media_via_quote(bot.event(), GROUP, request, QUOTED, "[音訊]")


def quoted_image_description(bot, monkeypatch, request=""):
    _media_unavailable(monkeypatch)
    main._handle_media_via_quote(bot.event(), GROUP, request, QUOTED, "[圖片]")


def quoted_video_description(bot, monkeypatch, request=""):
    _media_unavailable(monkeypatch)
    main._handle_media_via_quote(bot.event(), GROUP, request, QUOTED, "[影片]")


def image_file(bot, monkeypatch, request=""):
    main._handle_file_message(bot.event("圖.png"), GROUP)


def text_file(bot, monkeypatch, request=""):
    main._handle_file_message(bot.event("筆記.txt"), GROUP)


def office_file(bot, monkeypatch, request=""):
    monkeypatch.setattr(main, "_extract_office_text", lambda *_a: "會議紀錄")
    main._handle_file_message(bot.event("紀錄.docx"), GROUP)


def pdf_file(bot, monkeypatch, request=""):
    main._handle_file_message(bot.event("報告.pdf"), GROUP)


@_quota
def pdf_file_quota_spent(bot, monkeypatch, request=""):
    _pdf(monkeypatch, text="報告內容")
    main._handle_file_message(bot.event("報告.pdf"), GROUP)


@_quota
def scanned_pdf_quota_spent(bot, monkeypatch, request=""):
    _pdf(monkeypatch, text="")
    main._handle_file_message(bot.event("掃描.pdf"), GROUP)


PATHS = [
    direct_image, direct_video, voice, quoted_image, quoted_video,
    quoted_video_quota_spent, quoted_voice, quoted_image_description, quoted_video_description,
    image_file, text_file, office_file, pdf_file,
]
# _handle_event closes a file when the quota is spent, so only a race gets here, and _reply keeps
# _quota_exhausted_message() from LINE; the message stays open and nothing goes out.
QUOTA_PDF_PATHS = [pdf_file_quota_spent, scanned_pdf_quota_spent]


@pytest.mark.parametrize("path", PATHS)
def test_printed_silence_is_not_sent_and_the_message_is_finished(bot, monkeypatch, path):
    bot.model_says = PRINTED_ONE

    path(bot, monkeypatch)

    assert bot.sent() == []
    assert bot.is_finished()


@pytest.mark.parametrize("path", QUOTA_PDF_PATHS)
def test_printed_silence_is_not_sent_on_a_pdf_when_the_quota_ran_out(bot, monkeypatch, path):
    bot.model_says = PRINTED_ONE

    path(bot, monkeypatch)

    assert bot.sent() == []


@pytest.mark.parametrize("path", PATHS + QUOTA_PDF_PATHS)
def test_an_ordinary_reply_goes_out_unchanged(bot, monkeypatch, path):
    bot.model_says = ORDINARY

    path(bot, monkeypatch)

    assert bot.sent() == [ORDINARY]


@pytest.mark.parametrize("path", [
    quoted_image, quoted_video, quoted_video_quota_spent, quoted_voice,
    quoted_image_description, quoted_video_description,
])
def test_someone_asking_about_empty_strings_gets_the_answer(bot, monkeypatch, path):
    bot.model_says = HOW_TO

    path(bot, monkeypatch, ASKS)

    assert bot.sent() == [HOW_TO]


@pytest.mark.parametrize("path", PATHS + QUOTA_PDF_PATHS)
def test_a_negated_sentence_is_ordinary_text(bot, monkeypatch, path):
    # 「建議」 gets it past unsolicited_image_reply on a picture nobody asked about.
    bot.model_says = "建議改掉，這個 API 不會輸出空字串。"

    path(bot, monkeypatch)

    assert bot.sent() == ["建議改掉，這個 API 不會輸出空字串。"]


def test_an_answer_that_is_only_the_instruction_stays_out_even_when_asked(bot, monkeypatch):
    # The clients and the last gate use is_empty_marker, which has no request.  Gemini's "" is not
    # marked dropped (the TODO in _llm_chat), so the message stays open.
    bot.model_says = "直接輸出空字串。"

    quoted_video(bot, monkeypatch, ASKS)

    assert bot.sent() == []


def test_the_voice_transcription_fallback_sends_nothing(bot):
    # audio_local was removed, so this path ends at its ImportError.
    bot.model_says = PRINTED_ONE

    main._audio_asr_fallback(bot.event(), GROUP, "", QUOTED, "音訊")

    assert bot.sent() == []


@pytest.mark.parametrize("run", [
    lambda bot: main._media_pipeline_fallback(
        bot.event(token_known=False), GROUP, "", QUOTED, "image/jpeg", "圖片"
    ),
    lambda bot: main._handle_quoted_media_description_fallback(
        bot.event(token_known=False), GROUP, "", QUOTED, "圖片"
    ),
    lambda bot: main._handle_file_message(bot.event("圖.png", token_known=False), GROUP),
], ids=["quoted", "description", "file"])
def test_a_message_is_finished_without_its_token_or_a_delivery_slot(bot, monkeypatch, run):
    bot.model_says = PRINTED_ONE
    monkeypatch.setattr(main.memory, "get_raw_message_meta", lambda *_a: {"description": "舊摘要"})
    monkeypatch.setattr(main, "_try_acquire_media_delivery_slot", lambda *_a: None)

    run(bot)

    assert bot.sent() == []
    assert bot.is_finished()


# ── the wrappers around a request are not someone talking about empty strings ─


@pytest.mark.parametrize("path", [quoted_video, quoted_video_description])
def test_the_video_contract_in_the_prompt_is_not_the_asker(bot, monkeypatch, path):
    bot.model_says = PRINTED_ONE

    path(bot, monkeypatch, "這段合理嗎")

    assert bot.sent() == []
    assert bot.is_finished()


def test_the_image_contract_is_not_the_asker(bot, monkeypatch):
    bot.model_says = PRINTED_ONE

    quoted_image_description(bot, monkeypatch, "這張合理嗎")

    assert bot.sent() == []
    assert bot.is_finished()


def test_a_file_that_mentions_empty_strings_is_not_the_asker(bot, monkeypatch):
    # Accepted: the file is material.  A reply explaining it this way stays out too.
    monkeypatch.setattr(main, "_download_content", lambda _message_id: "沒有資料時輸出空字串".encode())
    bot.model_says = HOW_TO

    main._handle_file_message(bot.event("筆記.txt"), GROUP)

    assert bot.sent() == []
    assert bot.is_finished()


# ── a file with nothing to send ─────────────────────────────────────────────
# Andrew 2026-11-07: a picture sent as a file is silent like a pasted one, whether the model had
# nothing to say or the analysis failed, and no file says that its download failed.


def test_a_picture_file_that_is_only_described_is_finished_without_a_line(bot, monkeypatch):
    bot.model_says = "圖片顯示了某月某日的數據。數值為 85。"

    image_file(bot, monkeypatch)

    assert bot.sent() == []
    assert bot.is_finished()


def test_a_picture_file_with_advice_gets_the_advice(bot, monkeypatch):
    bot.model_says = "圖片顯示了某月某日的數據。建議先核對資料日期再判斷。"

    image_file(bot, monkeypatch)

    assert bot.sent() == ["建議先核對資料日期再判斷。"]
    assert memory.get_inbound_event_status(GROUP, MESSAGE) == "replied"


def _analysis_raises(*_a, **_k):
    raise RuntimeError("model not loaded")


def _analysis_finds_nothing(*_a, **_k):
    return None


@pytest.mark.parametrize("analysis", [_analysis_raises, _analysis_finds_nothing])
def test_a_picture_file_that_could_not_be_read_is_finished_without_a_line(bot, monkeypatch, analysis):
    monkeypatch.setattr(media_pipeline, "_analyze_image", analysis)

    image_file(bot, monkeypatch)

    assert bot.sent() == []
    assert bot.is_finished()


@pytest.mark.parametrize("token_known", [True, False])
@pytest.mark.parametrize("file_name", ["圖.png", "報告.pdf", "紀錄.docx", "筆記.txt"])
def test_a_file_that_could_not_be_downloaded_is_finished_without_a_line(
    bot, monkeypatch, file_name, token_known
):
    _media_unavailable(monkeypatch)

    main._handle_file_message(bot.event(file_name, token_known=token_known), GROUP)

    assert bot.sent() == []
    assert bot.is_finished()


# ── media_pipeline ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("printed", PRINTED)
def test_analyze_image_says_empty_for_silence_and_none_for_nothing(bot, printed):
    bot.model_says = printed
    assert media_pipeline.analyze_image(b"image", group_id=GROUP) == ""
    assert media_pipeline.analyze_image(b"image", user_prompt="這張合理嗎", group_id=GROUP) == ""

    bot.model_says = None
    assert media_pipeline.analyze_image(b"image", group_id=GROUP) is None


@pytest.mark.parametrize("printed", PRINTED)
def test_analyze_video_says_empty_for_silence_and_none_for_nothing(bot, printed):
    bot.model_says = printed
    assert media_pipeline.analyze_video(b"video", group_id=GROUP) == ""

    bot.model_says = None
    assert media_pipeline.analyze_video(b"video", group_id=GROUP) is None


def test_a_cached_video_reply_is_checked_too(bot, monkeypatch):
    monkeypatch.setattr(media_pipeline, "_maybe_lookup_media_cache", lambda *_a, **_k: PRINTED_ONE)

    assert media_pipeline.analyze_video(b"video", group_id=GROUP) == ""


def test_a_cached_image_reply_is_checked_too(bot, monkeypatch):
    monkeypatch.setattr(media_pipeline, "_analyze_image", REAL_ANALYZE_IMAGE)
    monkeypatch.setattr(media_pipeline, "_maybe_lookup_media_cache", lambda *_a, **_k: PRINTED_ONE)

    assert media_pipeline.analyze_image(b"image", group_id=GROUP) == ""


def test_the_cache_does_not_keep_it(bot):
    # No expiry: the same picture posted again would never be analysed again.
    long_reply = "這段只是閒聊，不需要補充，所以輸出空字串。" + "今天天氣不錯，" * 60

    assert not media_pipeline._is_cache_quality_reply(long_reply)
    assert media_pipeline._is_cache_quality_reply("今天天氣不錯，" * 60)


def test_analyzers_keep_the_answer_for_someone_asking_about_empty_strings(bot):
    bot.model_says = HOW_TO

    assert media_pipeline.analyze_image(b"image", user_prompt=ASKS, group_id=GROUP) == HOW_TO
    assert media_pipeline.analyze_video(b"video", user_prompt=ASKS, group_id=GROUP) == HOW_TO


# ── the pending queue (switched off in production; tests and rollback turn it on) ──


def _pending(bot, item_type: str, **fields) -> None:
    pending_store.add_unique(GROUP, {
        "type": item_type, "message_id": MESSAGE, "user_id": "U_TEST_A", "timestamp": time.time(), **fields,
    })


def _drain(bot, monkeypatch) -> None:
    class Slot:
        def release(self):
            pass

    monkeypatch.setattr(main, "_try_acquire_drain_slot", lambda _group_id: Slot())
    monkeypatch.setattr(main, "_drop_stale_pending", lambda _group_id: [])
    monkeypatch.setattr(main, "_gemini_group_messages", lambda items: [{"idxs": [0], "reply_to": 0}])
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: text)
    assert memory.begin_inbound_event(GROUP, MESSAGE) == "new"
    main._drain_pending_for_group(GROUP)


def _queue_is_empty() -> bool:
    return not pending_store.list_for_group(GROUP)


@pytest.mark.parametrize("item_type", ["image", "video"])
def test_the_pending_drain_finishes_silent_media(bot, monkeypatch, item_type):
    bot.model_says = PRINTED_ONE
    _pending(bot, item_type, media_path=bot.saved("saved.bin"))

    _drain(bot, monkeypatch)

    assert bot.sent() == []
    assert _queue_is_empty()
    assert bot.is_finished()


def test_the_pending_drain_finishes_silent_text(bot, monkeypatch):
    bot.model_says = PRINTED_ONE
    _pending(bot, "text", text="今天要帶傘嗎")

    _drain(bot, monkeypatch)

    assert bot.sent() == []
    assert _queue_is_empty()
    assert bot.is_finished()


def test_the_pending_drain_still_sends_an_ordinary_reply(bot, monkeypatch):
    bot.model_says = ORDINARY
    _pending(bot, "text", text="今天要帶傘嗎")

    _drain(bot, monkeypatch)

    assert bot.sent() == [ORDINARY]


def test_the_pending_drain_answers_someone_asking_about_empty_strings(bot, monkeypatch):
    bot.model_says = HOW_TO
    _pending(bot, "text", text=ASKS)

    _drain(bot, monkeypatch)

    assert bot.sent() == [HOW_TO]


def test_the_quote_rule_in_a_pending_text_is_not_the_asker(bot, monkeypatch):
    bot.model_says = PRINTED_ONE
    _pending(bot, "text", text="這樣合理嗎", quoted_original="前面的說法")

    _drain(bot, monkeypatch)

    assert bot.sent() == []
    assert _queue_is_empty()


@pytest.mark.parametrize("fields", [
    {"text": "這樣合理嗎", "quoted_original": "前面的說法"},
    {"text": with_current_reply(original_block("前面的說法"), "這樣合理嗎")},  # stored with the rule in it
])
def test_the_quick_pending_drain_does_not_show_the_quote_rule(bot, fields):
    bot.model_says = ORDINARY
    _pending(bot, "text", **fields)

    [(formatted, message_id)] = main._peek_text_pending_for_drain(GROUP, 2, 30)

    assert message_id == MESSAGE
    assert "這樣合理嗎" in formatted and ORDINARY in formatted
    assert "空字串" not in formatted


def test_the_quick_pending_drain_finishes_silent_text(bot):
    bot.model_says = PRINTED_ONE
    _pending(bot, "text", text="今天要帶傘嗎")

    assert main._peek_text_pending_for_drain(GROUP, 2, 30) == []

    assert _queue_is_empty()


# _pop_pending_for_piggyback is the only caller and nothing calls it, even with the queue on.
@pytest.mark.parametrize("item_type, fields", [
    ("text", {"text": "今天要帶傘嗎"}),
    ("file", {"file_name": "筆記.txt"}),
    ("file", {"file_name": "圖.png"}),
    ("image", {}),
])
def test_the_old_piggyback_sends_nothing(bot, item_type, fields):
    bot.model_says = PRINTED_ONE
    if item_type != "text":
        fields = {**fields, "media_path": bot.saved(fields.get("file_name", "saved.jpg"))}
    _pending(bot, item_type, **fields)

    assert main._peek_pending_for_piggyback(GROUP) is None


# ── a path nobody guarded shows up in the log ────────────────────────────────


@pytest.mark.parametrize("source", ["reply", "pending_push"])
def test_outbound_text_mentioning_it_is_noted_and_still_goes_out(bot, caplog, source):
    # A reminder or an answer about empty strings looks the same from here, so nothing is blocked.
    with caplog.at_level("INFO", logger=main.logger.name):
        assert main._prepare_outbound_text(HOW_TO, source=source) == HOW_TO

    assert f"outbound text mentions an empty string source={source} len={len(HOW_TO)}" in caplog.text


def test_an_ordinary_message_is_not_noted(bot, caplog):
    with caplog.at_level("INFO", logger=main.logger.name):
        main._prepare_outbound_text(ORDINARY)

    assert "empty string" not in caplog.text


# ── the local text fallback and the shared guard ─────────────────────────────


def test_the_local_text_fallback_drops_it(bot):
    bot.model_says = PRINTED_ONE

    assert main._local_text_llm_fallback("今天要帶傘嗎") == ""
    assert reply_provenance.dropped()


def test_the_guard_leaves_a_caller_checked_reply_to_its_caller(bot):
    with reply_provenance.checked_by_caller():
        assert main._guard_generated_reply(PRINTED_ONE, "今天要帶傘嗎") == PRINTED_ONE


def test_a_broken_silence_check_does_not_switch_the_other_guards_off(bot, monkeypatch):
    def fail(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(reply_policy, "has_printed_silence", fail)

    assert main._guard_generated_reply(ORDINARY, "今天要帶傘嗎") == ORDINARY
    assert main._guard_generated_reply("我上網查了，明天會下雨。", "今天要帶傘嗎") == ""


@pytest.mark.parametrize("request_text, expected", [
    ("", True),
    ("腦袋一片空白", True),
    ("這個值要輸出空字串嗎", False),
    ("empty string 怎麼處理", False),
    ("空白字符串怎麼處理", False),
])
def test_has_printed_silence_reads_the_asker_words(request_text, expected):
    assert reply_policy.has_printed_silence(PRINTED_ONE, request_text) is expected


@pytest.mark.parametrize("text", [
    *PRINTED,
    "所以輸出為空字串。", "所以輸出是空字串。", "輸出（空字串）", "輸出'空字串'", "輸出：空字串", "輸出「（空字串）」",
    "（輸出空白）", "（輸出空白。）", "這則只是閒聊（只輸出空白）", "（请输出空白）", "所以输出一个空字符串。", "**輸出 空字串**",
    "（這則不回，輸出空白）",
])
def test_has_printed_silence_knows_the_rewordings(text):
    assert reply_policy.has_printed_silence(text) is True


@pytest.mark.parametrize("text", [
    "", None, ORDINARY, "不要輸出空字串。", "這個 API 不會輸出空字串。", "这个 API 不会输出空字符串。", "别输出空字符串。",
    "換新墨水匣後再印一次（上次一直輸出空白）。", "印表機輸出空白頁。",
    "沒資料就回傳空字串。", "結果會輸出\n空字串",  # neither is the bot's own wording on one line
])
def test_has_printed_silence_leaves_other_text_alone(text):
    assert reply_policy.has_printed_silence(text) is False


_SLOW_SHAPES = {
    "the verb": lambda n: "輸出" * n,
    "the verb and a quote": lambda n: "輸出「" * n,
    "open brackets": lambda n: "（" * n,
    "brackets and the verb": lambda n: "（只輸出" * n,
    "reasons in brackets": lambda n: "（這則不回，" * n,
    "a long reason": lambda n: "（" + "，" * n,
    "spaces before the instruction": lambda n: " " * n + "輸出空字串",
    "many lines": lambda n: "輸出\n" * n,
}


@pytest.mark.parametrize("shape", sorted(_SLOW_SHAPES))
def test_long_replies_are_checked_in_linear_time(shape):
    # At most 24 ms on a performance core, 2.6× that on an efficiency core; quadratic would be seconds.
    assert min(cpu_seconds(reply_policy.has_printed_silence, _SLOW_SHAPES[shape](20000)) for _ in range(3)) < 0.25


def test_a_long_request_is_read_quickly():
    # Only read when the reply printed it; a file brings up to 80k characters.
    def check(request):
        return reply_policy.has_printed_silence(PRINTED_ONE, request)

    assert cpu_seconds(check, "--- 內容開始 ---\n" * 6000) < 0.5
    assert cpu_seconds(check, "--- 內容開始 ---\n--- 內容結束 ---\n" * 3000) < 0.5


def test_strip_missing_data_still_drops_it():
    assert reply_policy.strip_missing_data(PRINTED_ONE) == ""
    assert reply_policy.strip_missing_data(HOW_TO, ASKS) == HOW_TO
