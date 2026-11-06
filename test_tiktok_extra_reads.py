"""2026-11-06: TikTok reads past the daily floor, only on a quiet quota day.

Each day keeps its floor of LINE_BOT_TIKTOK_MEDIA_DAILY_CAP reads.  Reads past
it, up to LINE_BOT_TIKTOK_MEDIA_QUIET_DAY_MAX, run only while gemini_light_model
is not marked exhausted, has seen no 429 today, and its chat + extract + media
stay below 10.  gemini_light_model is also the chat fallback, so that sum has to
count real requests: chat and reminder extraction used to miss the per-model file.

conftest keeps both settings closed; the fixture opens them.  Every text here
is synthetic and these tests drive the real gemini_client code through a fake
client (the same one test_gemini_last_tier.py uses).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import gemini_client
import main
from test_gemini_last_tier import (  # noqa: F401  tier 是 fixture
    ANSWER,
    LITE,
    MAIN,
    TIER,
    perday,
    resp,
    tier,
    trip,
    unavailable,
)

BAD_DRAFT = "[[思考]]\n使用者貼了一個合成連結，並沒有明確提問。\n我需要判斷使用者可能的意圖。"


def per_minute(model: str) -> RuntimeError:
    """The real per-minute 429: its metric also says free_tier_requests."""
    return RuntimeError(
        "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
        "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
        f"quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier, model: {model}"
    )


def token_limit(model: str) -> RuntimeError:
    """A 429 no daily-quota check matches: tokens per minute, the one a big video hits."""
    return RuntimeError(
        "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
        "generativelanguage.googleapis.com/generate_content_free_tier_input_token_count, "
        f"quotaId: GenerateContentInputTokensPerModelPerMinute-FreeTier, model: {model}"
    )


def seed(**counts) -> None:
    gemini_client._update_model_usage(LITE, lambda entry: entry.update(counts))


def usage() -> dict:
    return gemini_client.model_usage(LITE)


def ready_after_backoff() -> bool:
    gemini_client._media_backoff_until = 0.0
    return gemini_client.media_task_ready()


@pytest.fixture
def quota(tier, monkeypatch):
    monkeypatch.setenv("LINE_BOT_TIKTOK_MEDIA_DAILY_CAP", "3")
    monkeypatch.setenv("LINE_BOT_TIKTOK_MEDIA_QUIET_DAY_MAX", "8")
    monkeypatch.setattr(gemini_client, "_log_quality_violation", lambda *a, **k: None)
    monkeypatch.setattr(gemini_client, "_alert_quality_violation", lambda *a, **k: None)
    return tier


# ── ① the floor, the ceiling and the sum ─────────────────────────────────────

@pytest.mark.parametrize(
    "counts,ready",
    [
        ({}, True),
        ({"media": 2, "chat": 50, "extract": 50}, True),  # the floor ignores the sum
        ({"media": 2, "rate_limited": True}, True),  # and a 429
        ({"media": 2, "exhausted": True}, False),  # as before: exhausted stops the floor too
        ({"media": 3}, True),
        ({"media": 3, "chat": 6}, True),  # sum 9
        ({"media": 3, "chat": 7}, False),  # sum 10
        ({"media": 3, "chat": 3, "extract": 4}, False),
        ({"media": 3, "chat": 3, "extract": 3}, True),
        ({"media": 3, "exhausted": True}, False),
        ({"media": 3, "rate_limited": True}, False),
        ({"media": 3, "judge": 50}, True),  # the spec's sum has no judge
        ({"media": 3, "failed": 50}, True),
        ({"media": 7, "chat": 2}, True),  # the 8th read: sum 9
        ({"media": 7, "chat": 3}, False),
        ({"media": 8}, False),
        ({"media": 9}, False),
    ],
)
def test_media_gate(quota, counts, ready):
    seed(**counts)
    assert gemini_client.media_task_ready() is ready


def test_quiet_day_reads_up_to_the_ceiling_and_stops(quota):
    fake = quota(generate={LITE: resp("{}")})
    for _ in range(8):
        assert gemini_client.media_side_task(["x"], None, label="t") is not None
    assert gemini_client.media_side_task(["x"], None, label="t") is None
    assert len(fake.generated) == 8 and usage()["media"] == 8


def test_reads_past_the_floor_stop_once_the_day_gets_busy(quota):
    fake = quota(chat={MAIN: perday(MAIN), LITE: resp(ANSWER)}, generate={LITE: resp("{}")})
    for _ in range(3):
        assert gemini_client.media_side_task(["x"], None, label="t") is not None
    for _ in range(7):
        assert gemini_client.chat("港式點心會不會很油？", [], [], []) == ANSWER
    assert usage()["chat"] == 7
    assert not gemini_client.media_task_ready()
    assert gemini_client.media_side_task(["x"], None, label="t") is None
    assert fake.generated == [LITE] * 3


def test_a_busy_morning_still_leaves_the_floor(quota):
    quota(chat={MAIN: perday(MAIN), LITE: resp(ANSWER)}, generate={LITE: resp("{}")})
    for _ in range(12):
        gemini_client.chat("港式點心會不會很油？", [], [], [])
    for _ in range(3):
        assert gemini_client.media_side_task(["x"], None, label="t") is not None
    assert gemini_client.media_side_task(["x"], None, label="t") is None


def test_the_floor_setting_stays_the_off_switch(quota, monkeypatch):
    monkeypatch.setenv("LINE_BOT_TIKTOK_MEDIA_DAILY_CAP", "0")
    assert not gemini_client.media_task_ready()
    monkeypatch.setenv("LINE_BOT_TIKTOK_MEDIA_DAILY_CAP", "ten")
    assert not gemini_client.media_task_ready()


@pytest.mark.parametrize("raw,value", [("", 8), ("5", 5), ("0", 0), ("-1", 0), ("ten", 0)])
def test_ceiling_is_read_at_call_time_and_fails_closed(monkeypatch, raw, value):
    monkeypatch.setenv("LINE_BOT_TIKTOK_MEDIA_QUIET_DAY_MAX", raw)
    assert gemini_client.media_quiet_day_max() == value


@pytest.mark.parametrize("raw", ["0", "3", "2", "-1", "ten"])
def test_a_ceiling_at_or_under_the_floor_means_no_extra_reads(quota, monkeypatch, raw):
    monkeypatch.setenv("LINE_BOT_TIKTOK_MEDIA_QUIET_DAY_MAX", raw)
    seed(media=2)
    assert gemini_client.media_task_ready()  # the floor does not depend on the ceiling
    seed(media=3)
    assert not gemini_client.media_task_ready()


def test_a_bad_ceiling_warns_once(monkeypatch, caplog):
    monkeypatch.setenv("LINE_BOT_TIKTOK_MEDIA_QUIET_DAY_MAX", "eight")
    with caplog.at_level(logging.WARNING, logger="gemini_client"):
        for _ in range(3):
            gemini_client.media_quiet_day_max()
    assert caplog.text.count("LINE_BOT_TIKTOK_MEDIA_QUIET_DAY_MAX") == 1


# ── ① the sum counts real requests ───────────────────────────────────────────

def test_chat_fallback_books_chat_on_the_light_model_only(quota):
    quota(chat={MAIN: perday(MAIN), LITE: resp(ANSWER)})
    before = gemini_client.get_gemini_quota_info()

    assert gemini_client.chat("港式點心會不會很油？", [], [], []) == ANSWER

    assert usage()["chat"] == 1
    assert gemini_client.model_usage(MAIN)["chat"] == 0
    assert gemini_client.model_usage(TIER)["chat"] == 0
    after = gemini_client.get_gemini_quota_info()
    assert after["used_requests"] > before["used_requests"]  # the legacy counter still runs


def test_chat_on_the_main_model_books_nothing_per_model(quota):
    quota(chat={MAIN: resp(ANSWER)})
    assert gemini_client.chat("港式點心會不會很油？", [], [], []) == ANSWER
    assert gemini_client.model_usage(MAIN)["chat"] == 0
    assert usage()["chat"] == 0


def test_failed_chat_on_the_light_model_is_booked_too(quota):
    quota(chat={MAIN: perday(MAIN), LITE: perday(LITE)})
    with pytest.raises(RuntimeError):
        gemini_client.chat("港式點心會不會很油？", [], [], [])
    current = usage()
    assert current["chat"] == 1 and current["failed"] == 1 and current["exhausted"]


def test_extract_reminder_books_extract_on_the_light_model(quota):
    text, payload = trip()
    quota(generate={LITE: resp(payload)})
    assert gemini_client.extract_reminder(text) is not None
    assert usage()["extract"] == 1


def test_failed_extract_reminder_is_booked_too(quota):
    text, payload = trip()
    quota(generate={LITE: unavailable(), TIER: resp(payload)})
    assert gemini_client.extract_reminder(text) is not None
    current = usage()
    assert current["extract"] == 1 and current["failed"] == 1
    assert not current["rate_limited"]  # a 503 is not a 429


def test_lite_or_main_books_extract_on_the_light_model_only(quota):
    quota(generate={LITE: resp("{}")})
    assert gemini_client._lite_or_main("x") is not None
    assert usage()["extract"] == 1
    assert gemini_client.model_usage(MAIN)["extract"] == 0


def test_lite_or_main_books_the_failure_but_not_the_main_fallback(quota):
    quota(generate={LITE: token_limit(LITE), MAIN: resp("{}")})
    assert gemini_client._lite_or_main("x") is not None
    current = usage()
    assert current["extract"] == 1 and current["failed"] == 1
    assert gemini_client.model_usage(MAIN)["extract"] == 0


def _gate_on_light_model(session) -> str:
    return gemini_client._quality_gate(session, BAD_DRAFT, [], "synthetic link", "G1", model=LITE, max_retries=1)


def test_a_quality_retry_on_the_light_model_is_booked(quota):
    session = MagicMock()
    session.send_message.return_value = resp(ANSWER)
    _gate_on_light_model(session)
    assert usage()["chat"] == 1 and usage()["failed"] == 0


def test_a_failed_quality_retry_on_the_light_model_is_booked(quota):
    session = MagicMock()
    session.send_message.side_effect = token_limit(LITE)
    _gate_on_light_model(session)
    assert usage()["chat"] == 1 and usage()["failed"] == 1


# ── ② any 429 closes the reads past the floor for the day ───────────────────

def _hit_chat(install, error):
    install(chat={MAIN: perday(MAIN), LITE: error(LITE)})
    with pytest.raises(RuntimeError):
        gemini_client.chat("港式點心會不會很油？", [], [], [])


def _hit_extract(install, error):
    text, payload = trip()
    install(generate={LITE: error(LITE), TIER: resp(payload)})
    assert gemini_client.extract_reminder(text) is not None


def _hit_lite_or_main(install, error):
    install(generate={LITE: error(LITE), MAIN: resp("{}")})
    assert gemini_client._lite_or_main("x") is not None


def _hit_media(install, error):
    install(generate={LITE: error(LITE)})
    assert gemini_client.media_side_task(["x"], None, label="t") is None


def _hit_quality_retry(_install, error):
    session = MagicMock()
    session.send_message.side_effect = error(LITE)
    _gate_on_light_model(session)


def _hit_video_fallback(install, error):
    fake = install(generate={MAIN: perday(MAIN), LITE: error(LITE)})
    active = SimpleNamespace(name="files/abc", state=SimpleNamespace(name="ACTIVE"))
    fake.files = SimpleNamespace(
        upload=lambda **_k: active, get=lambda **_k: active, delete=lambda **_k: None,
    )

    def downloader(opts):
        tool = MagicMock()
        tool.__enter__.return_value = tool
        tool.download.side_effect = lambda _urls: Path(opts["outtmpl"]).write_bytes(b"FAKE")
        return tool

    with patch("main._yt_dlp") as ytdlp:
        ytdlp.YoutubeDL.side_effect = downloader
        assert main._fetch_video_gemini("https://www.instagram.com/reel/synthetic/") is None


EVERY_PATH = [_hit_chat, _hit_extract, _hit_lite_or_main, _hit_media, _hit_quality_retry, _hit_video_fallback]


@pytest.mark.parametrize("hit", EVERY_PATH)
def test_a_429_no_quota_check_matches_closes_the_extra_reads_only(quota, hit):
    hit(quota, token_limit)

    current = usage()
    assert current["rate_limited"] and not current["exhausted"]
    seed(media=2)
    assert ready_after_backoff()
    seed(media=3)
    assert not ready_after_backoff()


@pytest.mark.parametrize(
    "hit,exhausted",
    [
        (_hit_media, False),  # media_side_task trusts only PerDay
        (_hit_lite_or_main, False),
        (_hit_quality_retry, False),
        (_hit_video_fallback, False),
        (_hit_chat, True),  # is_daily_quota_error also matches free_tier_requests: as before
        (_hit_extract, True),
    ],
)
def test_a_per_minute_429_closes_the_extra_reads_on_every_path(quota, hit, exhausted):
    hit(quota, per_minute)

    current = usage()
    assert current["rate_limited"] and current["exhausted"] is exhausted
    seed(media=3)
    assert not ready_after_backoff()


def test_a_503_does_not_close_the_extra_reads(quota):
    quota(generate={LITE: unavailable()})
    assert gemini_client.media_side_task(["x"], None, label="t") is None
    seed(media=3)
    assert ready_after_backoff()


def test_the_429_mark_survives_a_restart_and_ends_with_the_pacific_day(quota, monkeypatch):
    quota(generate={LITE: per_minute(LITE)})
    gemini_client.media_side_task(["x"], None, label="t")

    saved = json.loads(Path(gemini_client._MODEL_USAGE_FILE).read_text())
    assert saved["models"][LITE]["rate_limited"] is True

    tomorrow = (datetime.strptime(gemini_client._today_pt(), "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    monkeypatch.setattr(gemini_client, "_today_pt", lambda: tomorrow)
    assert not usage()["rate_limited"]


def test_another_models_429_leaves_the_extra_reads_open(quota):
    quota(chat={MAIN: perday(MAIN), LITE: resp(ANSWER)})
    assert gemini_client.chat("港式點心會不會很油？", [], [], []) == ANSWER
    assert not usage()["rate_limited"]
    assert not gemini_client.model_usage(MAIN)["rate_limited"]
    seed(media=3)
    assert gemini_client.media_task_ready()


# ── ③ the chat fallback order ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "main_error,lite_error",
    [(perday(MAIN), perday(LITE)), (unavailable(), unavailable()), (perday(MAIN), per_minute(LITE))],
    ids=["daily-429", "503", "minute-429-on-light"],
)
def test_chat_fallback_order_is_main_light_last_tier(quota, main_error, lite_error):
    fake = quota(chat={MAIN: main_error, LITE: lite_error, TIER: resp(ANSWER)})
    assert main._gemini_llm_chat("港式點心會不會很油？", [], [], []) == ANSWER
    assert fake.created == [MAIN, LITE, TIER]


def test_a_429_no_quota_check_matches_still_escapes_chat_as_before(quota):
    fake = quota(chat={MAIN: perday(MAIN), LITE: token_limit(LITE), TIER: resp(ANSWER)})
    with pytest.raises(RuntimeError):
        main._gemini_llm_chat("港式點心會不會很油？", [], [], [])
    assert fake.created == [MAIN, LITE]


def test_the_day_marks_do_not_change_the_order(quota):
    seed(exhausted=True, rate_limited=True)
    fake = quota(chat={MAIN: perday(MAIN), LITE: perday(LITE), TIER: resp(ANSWER)})
    assert main._gemini_llm_chat("港式點心會不會很油？", [], [], []) == ANSWER
    assert fake.created == [MAIN, LITE, TIER]  # neither mark ever skipped flash-lite in chat()


@pytest.mark.parametrize("target", ["_save_model_usage", "media_model", "track_model"])
def test_counting_never_breaks_a_reply(quota, monkeypatch, target):
    quota(chat={MAIN: perday(MAIN), LITE: resp(ANSWER)})

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(gemini_client, target, broken)
    assert gemini_client.chat("港式點心會不會很油？", [], [], []) == ANSWER


def test_noting_a_429_never_replaces_the_callers_error(quota, monkeypatch):
    fake = quota(chat={LITE: perday(LITE)})

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(gemini_client, "media_model", broken)
    with pytest.raises(RuntimeError, match="PerDay"):
        gemini_client._run(
            LITE, user_input="合成問題", context=[], facts=[], persona_notes=None,
            recall_hits=None, case_hits=None, group_id=None,
        )
    assert fake.created == [LITE]
