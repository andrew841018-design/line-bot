"""2026-11-07 YouTube 沒字幕時交給 Gemini 看 (Andrew approved the 11/05 proposal).

A video no transcript was read for goes to gemini_light_model as its bare watch URL, on reply paths
and quiet days only.  What it watched counts as read; everything else stays as it was: only a title,
which does not count, and the reply rules decide.  conftest keeps the cap at 0, the fixture opens it.
Every text here is synthetic and these tests drive the real gemini_client through a fake client.
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import gemini_client
import main
import tiktok_media
from test_gemini_last_tier import LITE, perday, tier  # noqa: F401  tier 是 fixture

VIDEO_ID = "dQw4w9WgXcQ"
WATCH = f"https://www.youtube.com/watch?v={VIDEO_ID}"
SHARED = f"https://youtu.be/{VIDEO_ID}?si=SYNTHETICsharerid"
SEEN = json.dumps({"has_content": True, "summary": "影片說明合成產品的三個使用步驟與注意事項。"}, ensure_ascii=False)
NOT_SEEN = json.dumps({"has_content": False, "summary": ""})
REFUSED = json.dumps({"has_content": True, "summary": "抱歉，我無法觀看這支影片。"}, ensure_ascii=False)
META = f"（以下是 YouTube 連結 {WATCH} 的影片資訊，透過 oEmbed API 擷取）\n--- YouTube 影片資訊開始 ---\n標題：合成標題\n--- YouTube 影片資訊結束 ---"


def resp(text, video_tokens=900):
    details = [SimpleNamespace(modality="TEXT", token_count=50)]
    if video_tokens:
        details.append(SimpleNamespace(modality="VIDEO", token_count=video_tokens))
    return SimpleNamespace(text=text, usage_metadata=SimpleNamespace(
        prompt_token_count=1000, total_token_count=1100, thinking_token_count=0, prompt_tokens_details=details,
    ))


class HttpError(RuntimeError):
    def __init__(self, code):
        super().__init__(f"{code} synthetic")
        self.code = code


def seed(**counts) -> None:
    gemini_client._update_model_usage(LITE, lambda entry: entry.update(counts))


def usage() -> dict:
    return gemini_client.model_usage(LITE)


@pytest.fixture
def quota(tier, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LINE_BOT_YOUTUBE_MEDIA_DAILY_CAP", "3")
    monkeypatch.setenv("LINE_BOT_TIKTOK_MEDIA_DAILY_CAP", "3")
    monkeypatch.setenv("LINE_BOT_TIKTOK_MEDIA_QUIET_DAY_MAX", "8")
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_kw: True)
    monkeypatch.setattr(main, "_fetch_video_ytdlp", lambda _url: None)
    monkeypatch.setattr(main, "_fetch_youtube_meta", lambda _url: META)
    monkeypatch.setattr(main, "_youtube_watch_length", lambda _url: 100)
    return tier


def read(url=SHARED, budget=30.0):
    """`_fetch_youtube_context` the way a reply path runs it: (block, recorder)."""
    with main._recording_link_content() as found:
        block = main._fetch_youtube_context(url, time.monotonic() + budget)
    return block, found


# ── ① a transcript is enough ─────────────────────────────────────────────────

def test_a_video_with_subtitles_never_reaches_gemini(quota, monkeypatch):
    fake = quota(generate={LITE: resp(SEEN)})

    def with_subtitles(_url):
        main._note_link_content()
        return "字幕內容：合成字幕"

    monkeypatch.setattr(main, "_fetch_video_ytdlp", with_subtitles)
    block, found = read()
    assert fake.generated == []
    assert block == "字幕內容：合成字幕"
    assert found


# ── ② what it watched counts as read, only then ──────────────────────────────

def test_a_video_without_subtitles_is_watched_and_counts_as_read(quota):
    fake = quota(generate={LITE: resp(SEEN)})
    block, found = read()
    assert fake.generated == [LITE]
    assert found
    assert block.startswith(META)
    assert "三個使用步驟" in block
    assert usage()["youtube"] == 1 and usage()["media"] == 0


def test_a_blocked_yt_dlp_still_gets_the_video_watched(quota, monkeypatch):
    fake = quota(generate={LITE: resp(SEEN)})
    monkeypatch.setattr(main, "_fetch_youtube_meta", lambda _url: None)
    monkeypatch.setattr(main, "_fetch_youtube_html_meta", lambda _url: None)
    block, found = read()
    assert fake.generated == [LITE] and found
    assert "三個使用步驟" in block


def test_a_short_video_was_watched_whole(quota):
    quota(generate={LITE: resp(SEEN)})
    _, found = read()
    assert not main.reply_guards.is_partial(found)


def test_a_long_video_was_watched_in_part(quota, monkeypatch):
    quota(generate={LITE: resp(SEEN)})
    monkeypatch.setattr(main, "_youtube_watch_length", lambda _url: 3600)
    _, found = read()
    assert main.reply_guards.is_partial(found)


def test_an_answer_without_video_tokens_does_not_count(quota):
    quota(generate={LITE: resp(SEEN, video_tokens=0)})
    block, found = read()
    assert not found and block == META


@pytest.mark.parametrize("answer", [NOT_SEEN, REFUSED, "not json"])
def test_an_answer_without_content_does_not_count(quota, answer):
    quota(generate={LITE: resp(answer)})
    block, found = read()
    assert not found
    assert block == META


# ── only what is plainly public ──────────────────────────────────────────────

def _page(**player) -> SimpleNamespace:
    base = {
        "playabilityStatus": {"status": "OK"},
        "videoDetails": {"lengthSeconds": "635", "isPrivate": False},
        "microformat": {"playerMicroformatRenderer": {"isUnlisted": False}},
    }
    base.update(player)
    return SimpleNamespace(status_code=200, text=f"var ytInitialPlayerResponse = {json.dumps(base)};</script>")


def _length(monkeypatch, page):
    monkeypatch.setattr(main, "_requests", SimpleNamespace(get=lambda *_a, **_kw: page))
    return main._youtube_watch_length(WATCH)


def test_a_public_finished_video_has_a_length(monkeypatch):
    assert _length(monkeypatch, _page()) == 635


@pytest.mark.parametrize("player", [
    {"playabilityStatus": {"status": "LOGIN_REQUIRED"}},
    {"playabilityStatus": {"status": "UNPLAYABLE"}},
    {"playabilityStatus": {}},
    {"microformat": {"playerMicroformatRenderer": {"isUnlisted": True}}},
    {"microformat": {"playerMicroformatRenderer": {}}},
    {"microformat": {}},
    {"videoDetails": {"lengthSeconds": "635", "isPrivate": True}},
    {"videoDetails": {"lengthSeconds": "635"}},
    {"videoDetails": {"lengthSeconds": "0", "isPrivate": False}},
    {"videoDetails": {"isPrivate": False}},
    {"videoDetails": {"lengthSeconds": "635", "isPrivate": False, "isLive": True}},
    {"videoDetails": {"lengthSeconds": "635", "isPrivate": False, "isUpcoming": True}},
    {"videoDetails": {"lengthSeconds": "635", "isPrivate": False, "isPostLiveDvr": True}},
    {"microformat": {"playerMicroformatRenderer": {"isUnlisted": False, "liveBroadcastDetails": {"isLiveNow": True}}}},
    {"microformat": {"playerMicroformatRenderer": {"isUnlisted": False, "liveBroadcastDetails": "yes"}}},
    {"microformat": {"playerMicroformatRenderer": {"isUnlisted": False, "liveBroadcastDetails": ["isLiveNow"]}}},
])
def test_whatever_the_page_does_not_state_is_a_no(monkeypatch, player):
    assert _length(monkeypatch, _page(**player)) is None


@pytest.mark.parametrize("reason", [
    "Sign in to confirm you're not a bot",
    "請登入帳戶以確認你已滿限制年齡",
    "Private video",
])
def test_a_login_wall_is_a_no_whatever_it_says(monkeypatch, reason):
    page = _page(playabilityStatus={"status": "LOGIN_REQUIRED", "reason": reason})
    assert _length(monkeypatch, page) is None


def test_a_finished_live_is_a_video(monkeypatch):
    micro = {"isUnlisted": False, "liveBroadcastDetails": {"isLiveNow": False}}
    assert _length(monkeypatch, _page(microformat={"playerMicroformatRenderer": micro})) == 635


@pytest.mark.parametrize("page", [
    SimpleNamespace(status_code=404, text=""),
    SimpleNamespace(status_code=200, text="<html>no player here</html>"),
])
def test_a_page_without_player_data_is_a_no(monkeypatch, page):
    assert _length(monkeypatch, page) is None


@pytest.mark.parametrize("player", [
    {"videoDetails": "not a dict"},
    {"microformat": ["not a dict"]},
    {"videoDetails": {"lengthSeconds": "abc", "isPrivate": False}},
])
def test_a_page_in_another_shape_is_a_no(monkeypatch, player):
    assert _length(monkeypatch, _page(**player)) is None


def test_a_failed_page_is_a_no(monkeypatch):
    def boom(*_a, **_kw):
        raise OSError("synthetic")

    monkeypatch.setattr(main, "_requests", SimpleNamespace(get=boom))
    assert main._youtube_watch_length(WATCH) is None


def test_a_video_not_plainly_public_is_not_sent(quota, monkeypatch):
    fake = quota(generate={LITE: resp(SEEN)})
    monkeypatch.setattr(main, "_youtube_watch_length", lambda _url: None)
    block, found = read()
    assert fake.generated == [] and not found and block == META


def test_the_page_is_read_only_when_a_read_could_follow(quota, monkeypatch):
    quota(generate={LITE: resp(SEEN)})
    monkeypatch.setattr(main, "_youtube_watch_length", lambda _url: pytest.fail("no page fetch"))
    seed(youtube=3)
    read()


# ── ③ a closed budget or a failure changes nothing ───────────────────────────

@pytest.mark.parametrize("counts", [
    {"youtube": 3},
    {"exhausted": True},
    {"rate_limited": True},
    {"chat": 10},
    {"chat": 5, "extract": 3, "media": 2},
    {"chat": 5, "extract": 2, "media": 2, "youtube": 1},
])
def test_gate_closed(quota, counts):
    fake = quota(generate={LITE: resp(SEEN)})
    seed(**counts)
    block, found = read()
    assert fake.generated == [] and not found and block == META


@pytest.mark.parametrize("counts", [
    {},
    {"youtube": 2},
    {"chat": 9},
    {"judge": 50},
    {"failed": 50},
])
def test_gate_open(quota, counts):
    fake = quota(generate={LITE: resp(SEEN)})
    seed(**counts)
    read()
    assert fake.generated == [LITE]


@pytest.mark.parametrize("raw, reads", [("0", 0), ("", 0), ("abc", 0), ("-1", 0), ("1", 1)])
def test_cap_setting(quota, monkeypatch, raw, reads):
    fake = quota(generate={LITE: resp(SEEN)})
    monkeypatch.setenv("LINE_BOT_YOUTUBE_MEDIA_DAILY_CAP", raw)
    read()
    assert len(fake.generated) == reads


def test_the_daily_cap_is_counted(quota):
    fake = quota(generate={LITE: resp(SEEN)})
    for index in range(4):
        with main._recording_link_content():
            main._fetch_youtube_context(f"https://youtu.be/{index:011d}", time.monotonic() + 30)
    assert len(fake.generated) == 3 and usage()["youtube"] == 3


def test_a_429_closes_the_day_for_youtube_and_nothing_else(quota):
    fake = quota(generate={LITE: RuntimeError("429 RESOURCE_EXHAUSTED synthetic per minute")})
    read()
    assert usage()["youtube_closed"] is True
    assert usage()["rate_limited"] is False and usage()["exhausted"] is False
    tiktok_media.clear_cache()
    read()
    assert fake.generated == [LITE]
    assert gemini_client.media_task_ready()


def test_a_per_day_429_closes_youtube_only(quota):
    quota(generate={LITE: perday(LITE)})
    read()
    assert usage()["youtube_closed"] is True and usage()["exhausted"] is False


def test_too_little_time_left_sends_nothing(quota):
    fake = quota(generate={LITE: resp(SEEN)})
    block, found = read(budget=5.0)
    assert fake.generated == [] and not found and block == META


def test_a_stuck_read_does_not_hold_the_reply(quota, monkeypatch):
    release = threading.Event()
    quota(generate={LITE: resp(SEEN)})
    monkeypatch.setattr(main, "_YOUTUBE_JOB_MIN_SEC", 0.0)
    monkeypatch.setattr(tiktok_media, "read_youtube", lambda *_a: release.wait(5) and None)
    monkeypatch.setattr(main, "_YOUTUBE_WATCH_BUDGET_SEC", 0.2)
    started = time.monotonic()
    try:
        block, found = read(budget=0.2)
        assert time.monotonic() - started < 2
        assert block == META and not found
    finally:
        release.set()


def test_another_reading_job_means_no_read(quota, monkeypatch):
    fake = quota(generate={LITE: resp(SEEN)})
    slot = threading.BoundedSemaphore(1)
    slot.acquire()
    monkeypatch.setattr(main, "_TIKTOK_MEDIA_SLOT", slot)
    block, found = read()
    assert fake.generated == [] and not found and block == META


def test_the_token_reserve_still_counts(quota, monkeypatch):
    fake = quota(generate={LITE: resp(SEEN)})
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_kw: False)
    read()
    assert fake.generated == []


# ── TikTok keeps what it had ─────────────────────────────────────────────────

@pytest.mark.parametrize("code", [400, 403, 404, 500, 503])
def test_a_failed_youtube_read_does_not_touch_tiktok(quota, code):
    quota(generate={LITE: HttpError(code)})
    for index in range(3):
        with main._recording_link_content():
            main._fetch_youtube_context(f"https://youtu.be/{index:011d}", time.monotonic() + 30)
    assert gemini_client._media_backoff_until == 0.0
    assert gemini_client._media_bad_requests == 0
    assert gemini_client.media_task_ready()


def test_tiktok_trouble_stops_youtube(quota):
    fake = quota(generate={LITE: resp(SEEN)})
    gemini_client._media_backoff_until = time.time() + 60
    read()
    assert fake.generated == []


def test_tiktok_keeps_its_daily_floor(quota):
    quota(generate={LITE: resp(SEEN)})
    seed(youtube=3)
    assert usage()["media"] == 0
    assert gemini_client.media_task_ready()


def test_tiktok_reads_past_the_floor_count_youtube_too(quota):
    quota(generate={LITE: resp(SEEN)})
    seed(media=3, chat=5, youtube=1)
    assert gemini_client.media_task_ready() is True
    seed(media=3, chat=5, youtube=2)
    assert gemini_client.media_task_ready() is False


# ── ④ what leaves the bot ────────────────────────────────────────────────────

class Capture:
    def __init__(self, answer=SEEN):
        self.contents = []
        self.answer = answer

    def __call__(self, contents, config, **_kw):
        self.contents.append(contents)
        return resp(self.answer)


@pytest.mark.parametrize("shared", [
    SHARED,
    f"https://www.youtube.com/shorts/{VIDEO_ID}?feature=share&si=SYNTHETIC",
    f"https://m.youtube.com/watch?v={VIDEO_ID}&t=30s&si=SYNTHETIC",
    f"https://www.youtube.com/live/{VIDEO_ID}?si=SYNTHETIC",
])
def test_only_the_bare_watch_url_is_sent(quota, monkeypatch, shared):
    quota(generate={LITE: resp(SEEN)})
    capture = Capture()
    monkeypatch.setattr(gemini_client, "youtube_side_task", capture)
    read(shared)
    video, prompt = capture.contents[0]
    assert video.file_data.file_uri == WATCH
    assert prompt == tiktok_media.YOUTUBE_PROMPT


@pytest.mark.parametrize("length, end", [(60, None), (120, None), (121, "120s"), (7200, "120s")])
def test_only_the_start_of_a_long_video_is_asked_for(quota, monkeypatch, length, end):
    quota(generate={LITE: resp(SEEN)})
    capture = Capture()
    monkeypatch.setattr(gemini_client, "youtube_side_task", capture)
    monkeypatch.setattr(main, "_youtube_watch_length", lambda _url: length)
    read()
    meta = capture.contents[0][0].video_metadata
    assert (meta.end_offset if meta else None) == end


def test_the_message_never_reaches_gemini(quota, monkeypatch):
    quota(generate={LITE: resp(SEEN)})
    capture = Capture()
    monkeypatch.setattr(gemini_client, "youtube_side_task", capture)
    with main._recording_link_content(), main._heavy_link_reads():
        main._prefetch_urls(f"合成成員的私人問題 {SHARED}")
    assert capture.contents and "合成成員" not in repr(capture.contents)


# ── where it runs ────────────────────────────────────────────────────────────

def test_a_research_read_stays_as_it_was(quota):
    fake = quota(generate={LITE: resp(SEEN)})
    with main._recording_link_content():
        main._prefetch_urls(SHARED)  # no _heavy_link_reads
    assert fake.generated == []


def test_the_pending_path_without_a_recorder_sends_nothing(quota):
    fake = quota(generate={LITE: resp(SEEN)})
    with main._heavy_link_reads():
        main._prefetch_urls(SHARED)
    assert fake.generated == []


def test_a_second_share_within_minutes_is_free(quota):
    fake = quota(generate={LITE: resp(SEEN)})
    read()
    block, found = read()
    assert fake.generated == [LITE] and found
    assert "三個使用步驟" in block


def test_gemini_side_of_tiktok_is_untouched(quota, monkeypatch):
    fake = quota(generate={LITE: HttpError(403)})
    assert gemini_client.media_side_task(["x"], None, label="t") is None
    assert gemini_client._media_backoff_until > time.time()  # as before
    assert fake.generated == [LITE]


def test_a_failed_read_is_not_paid_for_twice(quota):
    fake = quota(generate={LITE: HttpError(500)})
    read()
    block, found = read()
    assert fake.generated == [LITE]
    assert block == META and not found


def test_the_watch_never_outlasts_its_budget(quota, monkeypatch):
    seen = {}
    quota(generate={LITE: resp(SEEN)})
    monkeypatch.setattr(main, "_run_tiktok_media_job", lambda _job, deadline, *_a, **_kw: seen.update(d=deadline))
    read(budget=30.0)
    assert seen["d"] - time.monotonic() <= main._YOUTUBE_WATCH_BUDGET_SEC


def test_one_budget_for_the_whole_message(quota, monkeypatch):
    seen = {}
    quota(generate={LITE: resp(SEEN)})
    monkeypatch.setattr(main, "_fetch_youtube_context", lambda url, deadline=None: seen.update(d=deadline) or META)
    with main._recording_link_content(), main._heavy_link_reads():
        main._prefetch_urls(SHARED)
    assert seen["d"] is not None
    assert seen["d"] - time.monotonic() <= main._TIKTOK_PREFETCH_BUDGET_SEC


# ── the reply rules ──────────────────────────────────────────────────────────

@pytest.fixture
def burst_env(monkeypatch, tmp_path):
    monkeypatch.setattr(main.memory, "_DB_PATH", tmp_path / "memory.sqlite3")
    main.memory._init_db()
    assert main.memory.begin_inbound_event("GRP001", "MSG001") == "new"
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main.memory, "get_context", lambda _gid: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda _gid: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda _gid: [])
    monkeypatch.setattr(main, "_is_market_quote_request", lambda *_a, **_kw: False)
    monkeypatch.setattr(main, "_thinking_indicator", lambda _gid: nullcontext())
    monkeypatch.setattr(main, "_maybe_capture_calendar_event", lambda *_a, **_kw: None)
    prompts = []
    monkeypatch.setattr(main, "_llm_chat", lambda prompt, *_a: prompts.append(prompt) or "")
    monkeypatch.setattr(main, "_reply", lambda *_a, **_kw: False)
    return prompts


def test_a_bare_link_that_was_watched_reaches_the_model(quota, burst_env):
    quota(generate={LITE: resp(SEEN)})
    main._handle_burst_flush("GRP001", SHARED, "TOKEN001", ["MSG001"])
    assert len(burst_env) == 1
    assert "三個使用步驟" in burst_env[0]


def test_a_reply_that_only_retells_the_video_is_not_sent(quota, burst_env, monkeypatch):
    quota(generate={LITE: resp(SEEN)})
    sent = []
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: "影片說明合成產品的三個使用步驟與注意事項。")
    monkeypatch.setattr(main, "_reply", lambda *a, **k: sent.append(a) or True)
    main._handle_burst_flush("GRP001", SHARED, "TOKEN001", ["MSG001"])
    assert sent == []


def test_a_bare_link_nobody_could_watch_stays_silent(quota, burst_env):
    quota(generate={LITE: resp(NOT_SEEN)})
    main._handle_burst_flush("GRP001", SHARED, "TOKEN001", ["MSG001"])
    assert burst_env == []
    assert main.memory.get_inbound_event_status("GRP001", "MSG001") == "completed_no_reply"
