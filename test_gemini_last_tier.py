"""2026-10-04 (P2): a last Gemini tier on its own per-model daily quota.

On 10/2 the family's @咪寶 questions went unanswered: the Claude CLI failed,
gemini-2.5-flash and gemini-2.5-flash-lite had each spent their 20 requests a
day, and the local text model is off inside uvicorn.  The free tier counts
requests per model, and gemini-3.1-flash-lite kept answering 200 in the same
minute.  These tests drive the real gemini_client/_run code through a fake
client: ``_client.chats`` returns a fresh object on every access, so the whole
``_client`` is replaced.  conftest keeps the tier off by default; every text
here is synthetic and the dead proxy in the test command blocks the network.
"""

from __future__ import annotations

import fcntl
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

import config
import gemini_client
import main
import reply_provenance
import restatement_judge
from quote_context import original_block, with_current_reply

MAIN = "gemini-2.5-flash"
LITE = "gemini-2.5-flash-lite"
TIER = "gemini-3.1-flash-lite"
ANSWER = "港式點心挑清蒸類（蝦餃、腸粉）油脂比炸物少很多，點餐時避開炸春捲就不會太油。"
_TW = ZoneInfo("Asia/Taipei")


def perday(model: str) -> RuntimeError:
    return RuntimeError(
        "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
        "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
        f"quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier, model: {model}, "
        "quotaValue: 20"
    )


def unavailable() -> RuntimeError:
    return RuntimeError("503 UNAVAILABLE. The model is overloaded. Please try again later.")


def resp(text: str, *, grounded: bool = False, tokens: int = 1234):
    meta = None
    if grounded:
        meta = SimpleNamespace(
            grounding_chunks=[SimpleNamespace(
                web=SimpleNamespace(uri="https://example.org/dimsum", title="example.org"),
            )],
            grounding_supports=[SimpleNamespace(segment=SimpleNamespace(text=text[:12]))],
            web_search_queries=["港式 點心 油脂"],
        )
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(
            finish_reason=SimpleNamespace(name="STOP"),
            content=SimpleNamespace(parts=[]),
            grounding_metadata=meta,
        )],
        usage_metadata=SimpleNamespace(total_token_count=tokens, thinking_token_count=0),
    )


class FakeGemini:
    """Stands in for ``gemini_client._client``; every model must be scripted.

    A plan maps a model to one outcome or a list of outcomes (the last one
    repeats); an exception outcome is raised.
    """

    def __init__(self, chat=None, generate=None):
        self.chat_plan = dict(chat or {})
        self.generate_plan = dict(generate or {})
        self.created: list[str] = []
        self.sent: list[str] = []
        self.generated: list[str] = []
        self.configs: list[tuple[str, object]] = []
        self.chats = SimpleNamespace(create=self._create)
        self.models = SimpleNamespace(generate_content=self._generate)

    @staticmethod
    def _next(plan, model):
        if model not in plan:
            raise AssertionError(f"unexpected Gemini model {model}")
        step = plan[model]
        if isinstance(step, list):
            step = step.pop(0) if len(step) > 1 else step[0]
        if isinstance(step, BaseException):
            raise step
        return step

    def _create(self, *, model, config=None, history=None):
        self.created.append(model)
        self.configs.append((model, config))
        fake = self

        class _Session:
            def send_message(self, _message):
                fake.sent.append(model)
                return fake._next(fake.chat_plan, model)

        return _Session()

    def _generate(self, *, model, contents=None, config=None):
        self.generated.append(model)
        self.configs.append((model, config))
        return self._next(self.generate_plan, model)


def _patch_settings(monkeypatch, **values):
    seen: list[object] = []
    for obj in (config.settings, gemini_client.settings, main.settings):
        if any(obj is other for other in seen):
            continue
        seen.append(obj)
        for name, value in values.items():
            monkeypatch.setattr(obj, name, value)


@pytest.fixture
def tier(monkeypatch, tmp_path):
    _patch_settings(
        monkeypatch,
        gemini_model=MAIN,
        gemini_light_model=LITE,
        gemini_last_tier_model=TIER,
        gemini_last_tier_daily_cap=40,
    )
    monkeypatch.setattr(gemini_client, "_MODEL_USAGE_FILE", str(tmp_path / "gemini_model_usage.json"), raising=False)
    monkeypatch.setattr(gemini_client, "_MODEL_USAGE_LOCK_FILE", str(tmp_path / "gemini_model_usage.lock"), raising=False)
    monkeypatch.setattr(gemini_client, "_last_tier_backoff_until", 0.0, raising=False)
    monkeypatch.setattr(gemini_client.time, "sleep", lambda _s: None)
    monkeypatch.setattr(main, "_save_quota_state", lambda: None)
    # uvicorn keeps the local text model off (Metal OOM aborts the process).
    monkeypatch.setattr(main, "_local_text_llm_fallback", lambda *_a, **_k: "")
    monkeypatch.delenv("LINE_BOT_RESTATEMENT_JUDGE_MODEL", raising=False)
    import stock_quote

    monkeypatch.setattr(stock_quote, "get_contextual_quotes_text", lambda *_a, **_k: "")
    reply_provenance.reset()

    def install(**plans) -> FakeGemini:
        fake = FakeGemini(**plans)
        monkeypatch.setattr(gemini_client, "_client", fake)
        return fake

    return install


def set_flag(monkeypatch, *, recheck: bool = False) -> None:
    monkeypatch.setattr(main, "_quota_exhausted_until_ts", time.time() + 3600)
    monkeypatch.setattr(main, "_quota_last_probe_ts", 0.0 if recheck else time.time())


def quoted_explicit() -> str:
    return with_current_reply(original_block("因為港式很油", "群友"), "你覺得呢")


def today_iso() -> str:
    return datetime.now(_TW).strftime("%Y-%m-%d %A")


def trip(days: int = 18) -> tuple[str, str]:
    day = (datetime.now(_TW) + timedelta(days=days)).date()
    text = f"{day.month}月{day.day}日去日本要買三得利極之青汁"
    payload = json.dumps({
        "action": "去日本買三得利極之青汁",
        "year": day.year, "month": day.month, "day": day.day,
        "hour": 12, "minute": 0,
    }, ensure_ascii=False)
    return text, payload


# ── spec tests (F3 §failing tests 1–4, 6) ─────────────────────────────────────

def test_flag_set_quoted_explicit_uses_last_tier(tier, monkeypatch):
    fake = tier(chat={TIER: resp(ANSWER, grounded=True)})
    set_flag(monkeypatch)
    before = gemini_client.get_gemini_quota_info()

    assert main._gemini_llm_chat(quoted_explicit(), [], [], []) == ANSWER

    assert fake.created == [TIER]
    after = gemini_client.get_gemini_quota_info()
    # The legacy counters are flash-scoped and gate side tasks (I6).
    assert after["used_requests"] == before["used_requests"]
    assert after["used_tokens"] == before["used_tokens"]
    assert gemini_client.model_usage(TIER)["chat"] == 1
    assert main._quota_exhausted()  # a last-tier answer never clears the 2.5 flag
    (_model, cfg), = fake.configs
    # 2026-10-04 live check: search-grounded requests to the tier model were
    # refused (429) while plain ones answered, so the tier runs without tools.
    assert not cfg.tools
    assert cfg.thinking_config is None


def test_last_tier_perday_429_marks_model_and_skips_next_call(tier, monkeypatch):
    fake = tier(chat={TIER: perday(TIER)})
    set_flag(monkeypatch)

    assert main._gemini_llm_chat(quoted_explicit(), [], [], []) == ""
    assert gemini_client.model_exhausted(TIER)
    assert main._gemini_llm_chat(quoted_explicit(), [], [], []) == ""

    assert fake.created == [TIER]  # not asked again on the same Pacific day
    usage = gemini_client.model_usage(TIER)
    assert usage["chat"] == 1 and usage["failed"] == 1


def test_extract_reminder_lite_429_falls_back_to_last_tier(tier):
    text, payload = trip()
    fake = tier(generate={LITE: perday(LITE), TIER: resp(payload)})
    before = gemini_client.get_gemini_quota_info()

    result = gemini_client.extract_reminder(text, today_iso())

    assert result is not None
    day = (datetime.now(_TW) + timedelta(days=18)).date()
    assert (result["month"], result["day"], result["hour"], result["minute"]) == (day.month, day.day, 12, 0)
    assert fake.generated == [LITE, TIER]
    tier_cfg = fake.configs[-1][1]
    assert tier_cfg.thinking_config.thinking_budget == 0
    assert gemini_client.model_usage(TIER)["extract"] == 1
    assert gemini_client.model_exhausted(LITE)
    after = gemini_client.get_gemini_quota_info()
    assert after["used_tokens"] == before["used_tokens"]  # tier 3 stays out of the shared counter


def test_reminder_request_is_created_through_the_last_tier(tier):
    text, payload = trip()
    tier(generate={LITE: perday(LITE), TIER: resp(payload)})

    out = main._maybe_extract_reminder(text, "G_TIER", "U_TIER", message_id="M_TIER")

    assert out and out.startswith("已新增提醒")
    assert "待處理" not in out


def test_recheck_success_only_via_2_5_clears_flag(tier, monkeypatch):
    fake = tier(chat={MAIN: perday(MAIN), LITE: perday(LITE), TIER: resp(ANSWER)})
    set_flag(monkeypatch, recheck=True)

    assert main._gemini_llm_chat("港式點心會不會很油？", [], [], []) == ANSWER

    assert main._quota_exhausted()
    assert fake.created == [MAIN, LITE, TIER]
    assert gemini_client.model_exhausted(MAIN) and gemini_client.model_exhausted(LITE)


def test_last_tier_cap_reserved_for_judge(tier):
    fake = tier(generate={TIER: resp('{"labels": [{"i": 1, "label": "new"}]}')})
    for _ in range(30):
        gemini_client.track_model(TIER, "chat")
    for _ in range(10):
        gemini_client.track_model(TIER, "extract")

    assert not gemini_client.last_tier_allowed("chat")
    assert not gemini_client.last_tier_allowed("extract")
    assert gemini_client.chat_last_tier("港式點心會不會很油？", [], []) is None
    assert fake.created == []

    assert restatement_judge._call_light_model("合成審稿提示") is not None
    assert fake.generated == [TIER]
    assert gemini_client.model_usage(TIER)["judge"] == 1


# ── I5: an unflagged daily 429 marks the flag and reaches the tier once ───────

def test_unflagged_perday_sets_flag_and_answers_without_raising(tier):
    fake = tier(chat={MAIN: perday(MAIN), LITE: perday(LITE), TIER: resp(ANSWER)})
    assert not main._quota_exhausted()

    assert main._gemini_llm_chat("港式點心會不會很油？", [], [], []) == ANSWER

    assert main._quota_exhausted()
    assert fake.sent.count(TIER) == 1


def test_deliberate_empty_last_tier_answer_is_not_retried(tier):
    fake = tier(chat={MAIN: perday(MAIN), LITE: perday(LITE), TIER: resp("")})

    assert main._gemini_llm_chat(quoted_explicit(), [], [], []) == ""

    assert main._quota_exhausted()
    assert fake.sent.count(TIER) == 1


def _explicit_event(text: str):
    from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent

    source = MagicMock(spec=GroupSource)
    source.group_id = "GRP001"
    source.user_id = "USR001"
    source.type = "group"
    evt = MagicMock(spec=MessageEvent)
    evt.message = TextMessageContent(id="MSG_TIER", text=text, quoteToken="qt")
    evt.source = source
    evt.reply_token = "TOKEN_TIER"
    evt.timestamp = int(time.time() * 1000)
    evt.delivery_context = MagicMock(is_redelivery=False)
    return evt


def test_explicit_mention_reaches_the_last_tier_once_per_message(tier, monkeypatch):
    fake = tier(chat={MAIN: perday(MAIN), LITE: perday(LITE), TIER: resp("")})
    monkeypatch.setattr("claude_client.chat", lambda *_a, **_k: None)

    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._prefetch_urls", side_effect=lambda value: value),
        patch("main._maybe_capture_calendar_event"),
        patch("main._reply"),
    ):
        main._handle_explicit_text(_explicit_event("@咪寶 你覺得呢"), "GRP001", "你覺得呢")

    assert fake.sent.count(TIER) == 1
    assert main._quota_exhausted()


# ── N9: 503 also tries the tier; a tier equal to a 2.5 model is off ──────────

def test_unflagged_503_uses_last_tier_without_setting_the_quota_flag(tier):
    fake = tier(chat={MAIN: unavailable(), LITE: unavailable(), TIER: resp(ANSWER)})

    assert main._gemini_llm_chat("港式點心會不會很油？", [], [], []) == ANSWER

    assert not main._quota_exhausted()
    assert fake.sent.count(TIER) == 1


def test_unflagged_503_reraises_and_backs_off_when_the_tier_fails_too(tier):
    fake = tier(chat={MAIN: unavailable(), LITE: unavailable(), TIER: unavailable()})

    with pytest.raises(RuntimeError, match="503"):
        main._gemini_llm_chat("港式點心會不會很油？", [], [], [])
    assert fake.sent.count(TIER) == 1  # one attempt, no in-tier retry

    with pytest.raises(RuntimeError, match="503"):
        main._gemini_llm_chat("港式點心會不會很油？", [], [], [])
    assert fake.sent.count(TIER) == 1  # 60 s backoff after a 503


@pytest.mark.parametrize("configured", [MAIN, LITE, "", "not-a-gemini-model"])
def test_last_tier_is_off_unless_it_is_a_separate_gemini_model(tier, monkeypatch, configured):
    _patch_settings(monkeypatch, gemini_last_tier_model=configured)
    text, _payload = trip()
    fake = tier(generate={LITE: perday(LITE)})

    assert gemini_client.last_tier_model() == ""
    assert not gemini_client.last_tier_allowed("chat")
    assert gemini_client.chat_last_tier("港式點心會不會很油？", [], []) is None
    with pytest.raises(RuntimeError, match="429"):
        gemini_client.extract_reminder(text, today_iso())
    assert fake.created == [] and fake.generated == [LITE]


def test_last_tier_makes_one_attempt_with_one_quality_retry(tier):
    echo = "咪寶看到大家在聊港式點心很油的事情。"
    fake = tier(chat={TIER: [resp(echo), resp(echo), resp(echo), resp(echo)]})

    gemini_client.chat_last_tier("港式點心會不會很油？", [], [])

    assert fake.sent == [TIER, TIER]  # the 2.5 path would spend four requests


# ── grounding record (P2↔P3 interface) ───────────────────────────────────────

def test_last_tier_reply_records_grounding_and_llm_chat_resets(tier, monkeypatch):
    tier(chat={TIER: resp(ANSWER, grounded=True)})
    set_flag(monkeypatch)
    monkeypatch.setattr("claude_client.chat", lambda *_a, **_k: None)
    reply_provenance.record_grounding(
        gemini_client.extract_grounding(resp("舊回覆", grounded=True), model="stale")
    )

    assert main._llm_chat(quoted_explicit(), [], [], []) == ANSWER

    info = reply_provenance.grounding()
    assert info["model"] == TIER and reply_provenance.is_grounded(info)
    assert info["urls"] == ["https://example.org/dimsum"]
    assert reply_provenance.searched()  # the same answer, marked where it was taken

    monkeypatch.setattr("claude_client.chat", lambda *_a, **_k: "合成 Claude 回覆")
    assert main._llm_chat("問題", [], [], []) == "合成 Claude 回覆"
    assert reply_provenance.grounding() is None  # Claude is never grounded
    assert not reply_provenance.searched()


def test_gemini_llm_chat_resets_stale_grounding(tier, monkeypatch):
    _patch_settings(monkeypatch, gemini_last_tier_model="")
    tier()
    set_flag(monkeypatch)
    reply_provenance.record_grounding(
        gemini_client.extract_grounding(resp("舊回覆", grounded=True), model="stale")
    )

    assert main._gemini_llm_chat(quoted_explicit(), [], [], []) == ""
    assert reply_provenance.grounding() is None


def test_main_model_reply_records_the_returned_response(tier):
    tier(chat={MAIN: resp(ANSWER, grounded=True)})

    assert gemini_client.chat("港式點心會不會很油？", [], []) == ANSWER

    info = reply_provenance.grounding()
    assert info["model"] == MAIN and reply_provenance.is_grounded(info)
    assert reply_provenance.searched()


def test_tool_less_last_tier_records_its_model_and_never_counts_as_searched(tier, monkeypatch):
    tier(chat={TIER: resp(ANSWER)})
    set_flag(monkeypatch)
    monkeypatch.setattr("claude_client.chat", lambda *_a, **_k: None)

    assert main._llm_chat(quoted_explicit(), [], [], []) == ANSWER

    assert reply_provenance.grounding() == {
        "model": TIER, "urls": [], "supported_segments": [], "queries": [],
    }
    assert not reply_provenance.searched()


def test_quality_retry_records_the_retry_response_not_the_rejected_one(tier):
    echo = "咪寶看到大家在聊港式點心很油的事情。"
    tier(chat={MAIN: [resp(echo, grounded=True), resp(ANSWER)]})

    assert gemini_client.chat("港式點心會不會很油？", [], []) == ANSWER

    info = reply_provenance.grounding()
    assert info["model"] == MAIN and not reply_provenance.is_grounded(info)
    # H1: the rejected draft's search fed this rewrite in the same session, so
    # searched() stays set; only the returned response's segments are recorded.
    assert reply_provenance.searched()


# ── per-model state ──────────────────────────────────────────────────────────

def test_chat_marks_each_model_that_hits_its_daily_quota(tier):
    tier(chat={MAIN: perday(MAIN), LITE: resp(ANSWER)})

    assert gemini_client.chat("港式點心會不會很油？", [], []) == ANSWER

    assert gemini_client.model_exhausted(MAIN)
    assert not gemini_client.model_exhausted(LITE)


def test_model_usage_resets_on_a_new_pacific_day(tier):
    Path(gemini_client._MODEL_USAGE_FILE).write_text(json.dumps({
        "date": "2000-01-01",
        "models": {TIER: {"chat": 40, "exhausted": True}},
    }))

    assert not gemini_client.model_exhausted(TIER)
    assert gemini_client.last_tier_allowed("chat")


def test_model_usage_counts_survive_concurrent_writers(monkeypatch, tmp_path):
    monkeypatch.setattr(gemini_client, "_MODEL_USAGE_FILE", str(tmp_path / "usage.json"), raising=False)
    monkeypatch.setattr(gemini_client, "_MODEL_USAGE_LOCK_FILE", str(tmp_path / "usage.lock"), raising=False)

    def worker():
        for _ in range(25):
            gemini_client.track_model(TIER, "judge")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert gemini_client.model_usage(TIER)["judge"] == 200


def test_model_usage_waits_for_another_process_holding_the_file_lock(monkeypatch, tmp_path):
    lock_path = tmp_path / "usage.lock"
    monkeypatch.setattr(gemini_client, "_MODEL_USAGE_FILE", str(tmp_path / "usage.json"), raising=False)
    monkeypatch.setattr(gemini_client, "_MODEL_USAGE_LOCK_FILE", str(lock_path), raising=False)
    # A second open file description conflicts like another process (cron job).
    holder = open(lock_path, "a")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    done = threading.Event()
    writer = threading.Thread(
        target=lambda: (gemini_client.mark_model_exhausted(TIER), done.set())
    )
    writer.start()
    try:
        assert not done.wait(0.3)
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
    assert done.wait(5)
    writer.join()
    assert gemini_client.model_exhausted(TIER)


# ── extract_reminder contract: dict / None / raise ───────────────────────────

@pytest.mark.parametrize("make_error", [
    unavailable,
    lambda: RuntimeError("500 INTERNAL. synthetic"),
    lambda: TimeoutError("ReadTimeout"),
])
def test_extract_reminder_raises_when_no_model_can_answer(tier, make_error):
    text, _payload = trip()
    fake = tier(generate={LITE: make_error(), TIER: make_error()})

    with pytest.raises(Exception) as exc:
        gemini_client.extract_reminder(text, today_iso())

    assert type(exc.value) is type(make_error())
    assert fake.generated == [LITE, TIER]


def test_extract_reminder_other_api_errors_raise_without_the_tier(tier):
    text, _payload = trip()
    fake = tier(generate={LITE: RuntimeError("400 INVALID_ARGUMENT. synthetic")})

    with pytest.raises(RuntimeError, match="400"):
        gemini_client.extract_reminder(text, today_iso())
    assert fake.generated == [LITE]


def test_extract_reminder_tier_perday_marks_it_and_raises_the_lite_error(tier):
    text, _payload = trip()
    fake = tier(generate={LITE: perday(LITE), TIER: perday(TIER)})

    with pytest.raises(RuntimeError) as exc:
        gemini_client.extract_reminder(text, today_iso())
    assert LITE in str(exc.value) and main._is_quota_error(exc.value)
    assert gemini_client.model_exhausted(TIER)

    with pytest.raises(RuntimeError):
        gemini_client.extract_reminder(text, today_iso())
    assert fake.generated == [LITE, TIER, LITE]


def test_extract_reminder_null_or_unreadable_answer_is_none(tier):
    text, _payload = trip()
    fake = tier(generate={LITE: [resp("null"), resp("不是 JSON")]})

    assert gemini_client.extract_reminder(text, today_iso()) is None
    assert gemini_client.extract_reminder(text, today_iso()) is None
    assert fake.generated == [LITE, LITE]


# ── restatement judge (N9) ───────────────────────────────────────────────────

def test_judge_marks_its_own_daily_429_and_then_fails_open_without_calling(tier):
    fake = tier(generate={TIER: perday(TIER)})

    assert restatement_judge._call_light_model("合成審稿提示") is None
    assert gemini_client.model_exhausted(TIER)
    assert restatement_judge._call_light_model("合成審稿提示") is None

    assert fake.generated == [TIER]
    usage = gemini_client.model_usage(TIER)
    assert usage["judge"] == 1 and usage["failed"] == 1


def test_judge_checks_its_configured_model(tier, monkeypatch):
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE_MODEL", "gemini-judge-x")
    gemini_client.mark_model_exhausted(TIER)
    fake = tier(generate={"gemini-judge-x": resp('{"labels": []}')})

    assert restatement_judge._call_light_model("合成審稿提示") == '{"labels": []}'
    assert fake.generated == ["gemini-judge-x"]
    assert gemini_client.model_usage("gemini-judge-x")["judge"] == 1
