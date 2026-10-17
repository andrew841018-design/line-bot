"""2026-10-16: calendar extraction runs on the last tier's own quota.

The 2.5 models' 20 requests a day belong to replies and reminder extraction,
and gemini-2.5-flash-lite's free tier is also 20 a day.  Calendar extraction
now asks gemini-3.1-flash-lite, only while the tier keeps 20 of its daily cap
for replies, books those requests per model only, and runs in the background
so a slow request never holds up a family reply.  Every text here is synthetic.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

import calendar_extractor
import calendar_regex
import config
import gemini_client
import main

MAIN = "gemini-2.5-flash"
LITE = "gemini-2.5-flash-lite"
TIER = "gemini-3.1-flash-lite"


def _patch_settings(monkeypatch, **values):
    seen: list[object] = []
    for obj in (config.settings, gemini_client.settings, main.settings):
        if any(obj is other for other in seen):
            continue
        seen.append(obj)
        for name, value in values.items():
            monkeypatch.setattr(obj, name, value)


class FakeClient:
    """Stands in for ``gemini_client._client``; records every model asked."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.generated: list[str] = []
        self.configs: list[object] = []
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, *, model, contents=None, config=None):
        self.generated.append(model)
        self.configs.append(config)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def answer(payload: dict):
    return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), usage_metadata=None)


def event_answer(day: str, title: str = "全家生日聚餐") -> SimpleNamespace:
    return answer({
        "has_event": True, "is_cancellation": False, "title": title,
        "event_type": "family_gathering", "date": day, "time": "18:00",
        "location": "合成餐廳", "participants": ["全家"], "cancel_target_keyword": None,
    })


def perday() -> RuntimeError:
    return RuntimeError(
        "429 RESOURCE_EXHAUSTED. quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier, "
        f"model: {TIER}, quotaValue: 20"
    )


def perminute() -> RuntimeError:
    return RuntimeError(
        "429 RESOURCE_EXHAUSTED. generate_content_free_tier_requests, "
        f"quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier, model: {TIER}"
    )


class ApiError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(f"{code} {message}")
        self.code = code


@pytest.fixture
def tier(monkeypatch, tmp_path):
    _patch_settings(
        monkeypatch,
        gemini_model=MAIN,
        gemini_light_model=LITE,
        gemini_last_tier_model=TIER,
        gemini_last_tier_daily_cap=40,
    )
    monkeypatch.setattr(gemini_client, "_MODEL_USAGE_FILE", str(tmp_path / "model_usage.json"))
    monkeypatch.setattr(gemini_client, "_MODEL_USAGE_LOCK_FILE", str(tmp_path / "model_usage.lock"))
    monkeypatch.setattr(gemini_client, "_last_tier_backoff_until", 0.0)
    monkeypatch.setattr(gemini_client, "_side_task_backoff_until", 0.0)
    monkeypatch.delenv("GEMINI_LAST_TIER_SIDE_TASK_RESERVE", raising=False)

    def install(outcome) -> FakeClient:
        fake = FakeClient(outcome)
        monkeypatch.setattr(gemini_client, "_client", fake)
        return fake

    return install


@pytest.fixture
def cal_db(monkeypatch, tmp_path):
    db_file = tmp_path / "test_cal.db"
    monkeypatch.setenv("SQLITE_PATH", str(db_file))
    importlib.reload(config)
    config.settings.sqlite_path = str(db_file)
    import calendar_db

    importlib.reload(calendar_db)
    return calendar_db, db_file


def inline_spawn(monkeypatch) -> list[tuple]:
    spawned: list[tuple] = []

    def spawn(*args):
        spawned.append(args)
        main._capture_calendar_event_logged(*args)

    monkeypatch.setattr(main, "_spawn_calendar_capture", spawn)
    return spawned


def use_tier(n_chat: int = 0, n_extract: int = 0) -> None:
    for _ in range(n_chat):
        gemini_client.track_model(TIER, "chat")
    for _ in range(n_extract):
        gemini_client.track_model(TIER, "extract")


def flash_requests() -> int:
    return gemini_client.get_gemini_quota_info()["used_requests"]


def future_day(calendar_db, days: int = 5) -> str:
    return (calendar_db._today_tw() + timedelta(days=days)).isoformat()


def slash(day: str) -> str:
    _y, m, d = day.split("-")
    return f"{int(m)}/{int(d)}"


# ── 1. 主要路徑：flash 用完時，行事曆照樣用第三層 ─────────────────────────────

def test_dated_family_plan_reaches_the_last_tier_while_flash_is_spent(tier, cal_db, monkeypatch):
    calendar_db, _ = cal_db
    monkeypatch.setattr(main, "_quota_exhausted_until_ts", time.time() + 3600)
    for _ in range(19):
        gemini_client._track_failed_request()
    before = flash_requests()
    day = future_day(calendar_db)
    fake = tier(event_answer(day))
    spawned = inline_spawn(monkeypatch)

    main._maybe_capture_calendar_event("G1", f"{slash(day)} 晚上六點全家一起吃飯幫爸爸慶生")

    assert spawned, "a model-backed capture must go through the background path"
    assert fake.generated == [TIER]
    events = calendar_db.list_upcoming("G1", days=30)
    assert [(e["event_date"], e["event_time"]) for e in events] == [(day, "18:00")]
    assert flash_requests() == before
    assert gemini_client.model_usage(TIER)["extract"] == 1
    cfg = fake.configs[0]
    assert cfg.response_mime_type == "application/json"
    assert cfg.thinking_config.thinking_budget == 0
    assert cfg.http_options.timeout == 20_000


def test_calendar_no_longer_consults_the_flash_side_task_gate(tier, cal_db, monkeypatch):
    calendar_db, _ = cal_db
    day = future_day(calendar_db)
    fake = tier(event_answer(day))
    inline_spawn(monkeypatch)

    def no_flash_gate(*_a, **_k):
        raise AssertionError("calendar capture must not use the flash side-task gate")

    monkeypatch.setattr(main, "_gemini_side_task_allowed", no_flash_gate)
    main._maybe_capture_calendar_event("G1", f"{slash(day)} 晚上六點全家一起吃飯幫爸爸慶生")
    assert fake.generated == [TIER]
    assert len(calendar_db.list_upcoming("G1", days=30)) == 1


# ── 2. 不卡回覆 ───────────────────────────────────────────────────────────────

def test_model_backed_capture_is_handed_to_the_background(tier, monkeypatch):
    fake = tier(event_answer("2099-01-01"))
    spawned: list[tuple] = []
    monkeypatch.setattr(main, "_spawn_calendar_capture", lambda *a: spawned.append(a))
    inline: list[str] = []
    monkeypatch.setattr(main, "_capture_calendar_event_now", lambda *a, **_k: inline.append("ran"))

    main._maybe_capture_calendar_event("G1", "下週六晚上全家聚餐", "U1", "m1")

    assert spawned == [("G1", "下週六晚上全家聚餐", "U1", "m1")]
    assert inline == []
    assert fake.generated == []


def test_rules_only_capture_stays_inline(monkeypatch):
    # conftest keeps the last tier off, so nothing would ask a model.
    spawned: list[tuple] = []
    monkeypatch.setattr(main, "_spawn_calendar_capture", lambda *a: spawned.append(a))
    inline: list[tuple] = []
    monkeypatch.setattr(main, "_capture_calendar_event_now", lambda *a: inline.append(a))

    main._maybe_capture_calendar_event("G1", "下週六晚上全家聚餐")

    assert spawned == []
    assert inline == [("G1", "下週六晚上全家聚餐", "", "")]


def test_background_executor_runs_one_capture_at_a_time():
    assert main._CALENDAR_CAPTURE_EXECUTOR._max_workers == 1


# ── 3. 邊界 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("chat", "extract", "ready"), [(19, 0, True), (10, 9, True), (20, 0, False), (12, 8, False)])
def test_side_task_keeps_twenty_of_the_cap_for_replies(tier, chat, extract, ready):
    tier(event_answer("2099-01-01"))
    use_tier(chat, extract)
    assert gemini_client.side_task_last_tier_ready() is ready
    assert gemini_client.last_tier_allowed("chat") is True


def test_reserve_of_forty_turns_calendar_off_the_tier(tier, monkeypatch):
    fake = tier(event_answer("2099-01-01"))
    monkeypatch.setenv("GEMINI_LAST_TIER_SIDE_TASK_RESERVE", "40")
    assert gemini_client.side_task_last_tier_ready() is False
    assert calendar_extractor.model_call_ready("下週六全家聚餐") is False
    assert fake.generated == []


def test_malformed_reserve_keeps_calendar_off_the_tier(tier, monkeypatch):
    # The env var is the rollback switch; a typo must not quietly turn it back on.
    tier(event_answer("2099-01-01"))
    monkeypatch.setenv("GEMINI_LAST_TIER_SIDE_TASK_RESERVE", "4O")
    assert gemini_client.side_task_last_tier_ready() is False
    assert gemini_client.last_tier_allowed("chat") is True


def test_tier_off_or_bad_cap_means_no_request(tier, monkeypatch):
    tier(event_answer("2099-01-01"))
    _patch_settings(monkeypatch, gemini_last_tier_model="")
    assert gemini_client.side_task_last_tier_ready() is False
    _patch_settings(monkeypatch, gemini_last_tier_model=TIER, gemini_last_tier_daily_cap="many")
    assert gemini_client.side_task_last_tier_ready() is False
    _patch_settings(monkeypatch, gemini_last_tier_daily_cap=-5)
    assert gemini_client.side_task_last_tier_ready() is False


@pytest.mark.parametrize(("text", "ready"), [
    ("下週六晚上全家聚餐", True),
    ("25號回老家", True),
    ("月底帶媽媽去看牙醫", True),
    ("聚餐取消了", True),
    ("聚餐延期", True),
    ("今天好累喔", True),
    ("我不去了", False),
    ("看這個 https://example.com/2026/10/05/news", False),
    ("哈哈好喔", False),
    ("那家餐廳很好吃", False),
])
def test_model_only_sees_text_that_could_date_or_cancel_an_event(tier, text, ready):
    tier(event_answer("2099-01-01"))
    assert calendar_extractor.model_call_ready(text) is ready


# ── 4. 失敗處理：只動行事曆自己的退避，回覆照常可用第三層 ─────────────────────

REGEX_TEXT_DAYS = 3


def _regex_text(calendar_db) -> str:
    return f"{future_day(calendar_db, REGEX_TEXT_DAYS)} 14:00 拿喜來登贈送的生日蛋糕"


def test_daily_quota_marks_the_tier_spent_and_falls_back_to_rules(tier, cal_db):
    calendar_db, _ = cal_db
    text = _regex_text(calendar_db)
    fake = tier(perday())
    before = flash_requests()

    out = calendar_extractor.extract(text)

    assert fake.generated == [TIER]
    assert out == calendar_regex.extract_regex_only(text, calendar_db._today_tw())
    assert out["has_event"] is True
    usage = gemini_client.model_usage(TIER)
    assert usage["exhausted"] is True
    assert (usage["extract"], usage["failed"]) == (1, 1)
    assert gemini_client._last_tier_backoff_until == 0.0
    assert flash_requests() == before


def test_per_minute_quota_backs_off_side_tasks_only(tier, cal_db):
    calendar_db, _ = cal_db
    fake = tier(perminute())
    calendar_extractor.extract(_regex_text(calendar_db))
    assert fake.generated == [TIER]
    assert gemini_client.model_usage(TIER)["exhausted"] is False
    assert gemini_client._last_tier_backoff_until == 0.0
    assert gemini_client.last_tier_allowed("chat") is True
    assert 50 <= gemini_client._side_task_backoff_until - time.time() <= 60


def test_overload_backs_off_side_tasks_for_a_minute_without_touching_replies(tier, cal_db):
    calendar_db, _ = cal_db
    text = _regex_text(calendar_db)
    fake = tier(ApiError(503, "UNAVAILABLE. The model is overloaded."))
    before = flash_requests()

    out = calendar_extractor.extract(text)
    assert out["has_event"] is True
    assert gemini_client._last_tier_backoff_until == 0.0
    assert gemini_client.last_tier_allowed("chat") is True
    assert gemini_client.side_task_last_tier_ready() is False

    calendar_extractor.extract(text)
    assert fake.generated == [TIER], "no second request inside the backoff"
    assert flash_requests() == before


def test_client_error_backs_off_side_tasks_for_an_hour(tier, cal_db):
    calendar_db, _ = cal_db
    tier(ApiError(404, "NOT_FOUND. models/gemini-3.1-flash-lite is not found"))
    calendar_extractor.extract(_regex_text(calendar_db))
    assert gemini_client._side_task_backoff_until - time.time() > 3000
    assert gemini_client._last_tier_backoff_until == 0.0


# ── 5. 模型答案的處理 ─────────────────────────────────────────────────────────

def test_unreadable_answer_falls_back_to_rules(tier, cal_db):
    calendar_db, _ = cal_db
    text = _regex_text(calendar_db)
    tier(SimpleNamespace(text="not json", usage_metadata=None))
    assert calendar_extractor.extract(text) == calendar_regex.extract_regex_only(
        text, calendar_db._today_tw()
    )


def test_rejected_event_still_returns_no_event(tier, monkeypatch):
    tier(event_answer("2099-01-01", title="轉貼公告活動"))
    monkeypatch.setattr(
        calendar_extractor.reminder_intent, "should_reject_reminder_candidate", lambda *_a: True
    )
    out = calendar_extractor.extract("下週六 轉貼公告活動")
    assert out["has_event"] is False


def test_model_event_in_an_explicit_past_year_is_not_written(tier, cal_db, monkeypatch):
    # Same wording as the 10/5 rules test (test_grounded_reply_policy), now with
    # the model answering: an explicit past year must not become an event.
    calendar_db, db_file = cal_db
    year = calendar_db._today_tw().year - 1
    past = f"{year}-09-24"
    fake = tier(event_answer(past))
    inline_spawn(monkeypatch)
    calendar_db.list_upcoming("G1", days=1)  # creates the schema

    main._maybe_capture_calendar_event("G1", f"{year}年9月24日 18:00 全家聚餐")  # privacy-safe-fixture

    assert fake.generated == [TIER]
    with sqlite3.connect(db_file) as con:
        rows = con.execute("SELECT count(*) FROM events").fetchone()[0]
    assert rows == 0


def test_model_event_dated_before_today_returns_no_event(tier):
    tier(event_answer("2001-01-01"))
    assert calendar_extractor.extract("下週六全家聚餐")["has_event"] is False


def test_any_model_date_before_today_is_ignored_even_for_a_reschedule(tier):
    tier(answer({
        "has_event": False, "is_cancellation": True, "title": None,
        "event_type": "family_gathering", "date": "2001-01-01", "time": None,
        "location": None, "participants": [], "cancel_target_keyword": "聚餐",
    }))
    out = calendar_extractor.extract("聚餐改到下週六")
    assert out["is_cancellation"] is False
    assert out["has_event"] is False


def test_model_answer_is_bounded():
    out = calendar_extractor._normalize({
        "has_event": True, "title": "全家聚餐", "date": "2099-01-01", "time": "99:99",
        "participants": [f"家人{i}" for i in range(20)],
    })
    assert out["time"] is None
    assert len(out["participants"]) == 8


def test_model_config_caps_output(tier):
    fake = tier(event_answer("2099-01-01"))
    calendar_extractor.extract("下週六全家聚餐")
    assert fake.configs[0].max_output_tokens == 1024


def test_calendar_logs_never_carry_the_message(tier, cal_db, caplog):
    calendar_db, _ = cal_db
    secret = "合成秘密行程阿爾法"
    tier(ApiError(503, "UNAVAILABLE"))
    with caplog.at_level("DEBUG"):
        calendar_extractor.extract(f"下週六 {secret}")
        gemini_client._side_task_backoff_until = 0.0
        tier(SimpleNamespace(text=f"not json {secret}", usage_metadata=None))
        calendar_extractor.extract(f"下週六 {secret}")
    assert caplog.records, "the check is vacuous without log records"
    assert all(secret not in record.getMessage() for record in caplog.records)


# ── 6. 取消／改期：模型只能動訊息裡點名的活動（GP2 S1） ─────────────────────

def _cancel_answer(day: str, kw):
    return answer({
        "has_event": False, "is_cancellation": True, "title": None,
        "event_type": "family_gathering", "date": day, "time": None,
        "location": None, "participants": [], "cancel_target_keyword": kw,
    })


def _event_status(db_file, event_id: str) -> str:
    with sqlite3.connect(db_file) as con:
        return con.execute("SELECT status FROM events WHERE event_id = ?", (event_id,)).fetchone()[0]


@pytest.mark.parametrize(("text", "kw"), [
    ("週六我不去了", None),          # one person not going, no cancel word
    ("週六那個取消", None),          # no keyword: a date alone must not pick the event
    ("週六那個取消", "全家聚餐"),     # matches the title, but the message never said it
    ("週六的聚餐取消了", "聚"),       # one-character keyword
])
def test_model_cancellation_needs_a_named_event(tier, cal_db, monkeypatch, text, kw):
    calendar_db, db_file = cal_db
    day = future_day(calendar_db, 8)
    event_id = calendar_db.insert_event(group_id="G1", title="全家聚餐", event_date=day, event_time="18:00")
    fake = tier(_cancel_answer(day, kw))
    inline_spawn(monkeypatch)

    main._maybe_capture_calendar_event("G1", text)

    assert fake.generated == [TIER], "the cancellation branch must actually be reached"
    assert _event_status(db_file, event_id) == "active"


def test_model_cancellation_of_a_named_event_still_works(tier, cal_db, monkeypatch):
    calendar_db, db_file = cal_db
    day = future_day(calendar_db, 8)
    event_id = calendar_db.insert_event(group_id="G1", title="全家聚餐", event_date=day, event_time="18:00")
    tier(_cancel_answer(day, "全家聚餐"))
    inline_spawn(monkeypatch)

    main._maybe_capture_calendar_event("G1", "週六的全家聚餐取消了")

    assert _event_status(db_file, event_id) != "active"


# ── 7. 只判斷一次；同步路徑永遠不問模型（GP2 A2） ──────────────────────────

def test_spawn_failure_falls_back_to_rules_inline(tier, monkeypatch):
    tier(event_answer("2099-01-01"))

    def broken_spawn(*_a):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(main, "_spawn_calendar_capture", broken_spawn)
    calls: list[tuple] = []
    monkeypatch.setattr(main, "_capture_calendar_event_now", lambda *a, **k: calls.append((a, k)))

    main._maybe_capture_calendar_event("G1", "下週六晚上全家聚餐", "U1", "m1")

    assert calls == [(("G1", "下週六晚上全家聚餐", "U1", "m1"), {})]


def test_inline_capture_never_asks_a_model_even_when_the_tier_is_free(tier, cal_db):
    fake = tier(event_answer("2099-01-01"))
    assert gemini_client.side_task_last_tier_ready() is True
    main._capture_calendar_event_now("G1", "下週六晚上全家聚餐")
    assert fake.generated == []


# ── 8. 實作後審查（POST_GP2 I1／I3／N4） ─────────────────────────────────────

def test_unpadded_model_dates_are_normalized():
    assert calendar_extractor._normalize({"has_event": True, "date": "2026-11-5"})["date"] == "2026-11-05"
    assert calendar_extractor._normalize({"has_event": True, "date": "2026-9-24"})["date"] == "2026-09-24"


def test_unpadded_past_model_date_cannot_slip_past_the_guard(tier, monkeypatch):
    monkeypatch.setattr(calendar_extractor, "_today_tw", lambda: ("2026-10-17", "六"))
    tier(event_answer("2026-9-24"))
    assert calendar_extractor.extract("下週六全家聚餐")["has_event"] is False
    tier(answer({
        "has_event": False, "is_cancellation": True, "title": None,
        "event_type": "family_gathering", "date": "2026-10-5", "time": None,
        "location": None, "participants": [], "cancel_target_keyword": "聚餐",
    }))
    assert calendar_extractor.extract("聚餐改到下週六")["is_cancellation"] is False


@pytest.mark.parametrize(("error", "token"), [
    (perday(), "quota=day exhausted=1"),
    (perminute(), "quota=minute exhausted=0"),
    (ApiError(503, "UNAVAILABLE"), "quota=- exhausted=0"),
])
def test_failure_log_names_the_quota_kind(tier, caplog, error, token):
    tier(error)
    with caplog.at_level("WARNING"):
        calendar_extractor.extract("下週六全家聚餐")
    assert any(token in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("code", [408, 409, 499])
def test_transient_client_codes_back_off_a_minute_not_an_hour(tier, code):
    tier(ApiError(code, "TRANSIENT"))
    calendar_extractor.extract("下週六全家聚餐")
    assert gemini_client._side_task_backoff_until - time.time() <= 60


def test_model_date_of_today_is_kept(tier, monkeypatch):
    monkeypatch.setattr(calendar_extractor, "_today_tw", lambda: ("2026-10-17", "六"))
    tier(event_answer("2026-10-17"))
    assert calendar_extractor.extract("今天晚上全家聚餐")["has_event"] is True


def test_negative_reserve_keeps_calendar_off_the_tier(tier, monkeypatch):
    tier(event_answer("2099-01-01"))
    monkeypatch.setenv("GEMINI_LAST_TIER_SIDE_TASK_RESERVE", "-5")
    assert gemini_client.side_task_last_tier_ready() is False


def test_url_does_not_swallow_the_date_after_it(tier):
    tier(event_answer("2099-01-01"))
    assert calendar_extractor.model_call_ready("看 https://example.com/a明天聚餐") is True


# ── 9. 取消字要緊貼點名的活動（Andrew 2026-10-17 採用 POST_GP2 I2 (a)） ─────────

from quote_context import original_block, with_current_reply  # noqa: E402


@pytest.mark.parametrize(("text", "kw", "expected"), [
    ("週六的全家聚餐取消了", "全家聚餐", True),
    ("全家聚餐，因為颱風我們決定取消", "全家聚餐", True),
    ("週六聚餐改到週日", "聚餐", True),
    ("週六聚餐我不去了\n健身課取消了", "聚餐", False),      # cancel word is another message
    ("週六的聚餐沒有取消喔", "聚餐", False),               # negated
    ("週六的聚餐不會延期", "聚餐", False),                 # negated move
    ("全家聚餐的餐廳我們再看看大家什麼時候有空再說還有健身課也取消了", "全家聚餐", False),  # too far
    ("取消週六的全家聚餐", "全家聚餐", True),               # cancel word before the keyword
    ("沒有取消週六的聚餐", "聚餐", False),
    ("取消了啦大家記得下次再約那個什麼時候的全家聚餐", "全家聚餐", False),  # before, too far
    ("週六的聚餐我不去了", "聚餐", False),                  # one person not going (Andrew 10/17)
    ("週六聚餐不能取消", "聚餐", False),
    ("週六聚餐沒辦法延期", "聚餐", False),
    ("週六聚餐取消了嗎", "聚餐", False),                    # a question
    ("週六聚餐不得不取消", "聚餐", True),                   # double negation still cancels
    ("聚餐的地點大家都說要再看看喔然後下次的聚餐取消", "聚餐", True),  # second occurrence
    ("啊" * 2000 + "週六聚餐取消了", "聚餐", False),         # beyond what the model saw
    ("--- 原始訊息 開始 ---\n[群友]: 週六的全家聚餐取消了", "全家聚餐", False),  # cut-off quote
])
def test_cancel_word_must_sit_next_to_the_named_event(text, kw, expected):
    assert calendar_extractor.cancel_word_near_keyword(text, kw) is expected


def test_cancel_word_inside_a_quoted_message_does_not_count():
    text = with_current_reply(original_block("週六的全家聚餐取消了", "群友"), "好的收到")
    assert calendar_extractor.cancel_word_near_keyword(text, "全家聚餐") is False


def test_another_message_in_the_burst_cannot_cancel_the_family_event(tier, cal_db, monkeypatch, caplog):
    # A quoted message never reaches the model on this path (earlier filters);
    # the quote rule is covered by the helper test above.
    calendar_db, db_file = cal_db
    text = "週六聚餐我不去了\n健身課取消了"
    day = future_day(calendar_db, 8)
    event_id = calendar_db.insert_event(group_id="G1", title="全家聚餐", event_date=day, event_time="18:00")
    fake = tier(_cancel_answer(day, "聚餐"))
    inline_spawn(monkeypatch)
    monkeypatch.setattr(calendar_extractor, "model_call_ready", lambda *_a, **_k: True)

    with caplog_ctx(caplog):
        main._maybe_capture_calendar_event("G1", text)

    assert fake.generated == [TIER]
    assert _event_status(db_file, event_id) == "active"
    assert any("reason=cancel_word_not_near_keyword why=other_line" in r.getMessage() for r in caplog.records)


def caplog_ctx(caplog):
    return caplog.at_level("INFO")


@pytest.mark.parametrize(("text", "kw", "why"), [
    ("週六聚餐我不去了\n健身課取消了", "聚餐", "other_line"),
    ("全家聚餐的餐廳我們再看看大家什麼時候有空再說還有健身課也取消了", "全家聚餐", "too_far"),
    ("週六的聚餐沒有取消喔", "聚餐", "negated"),
    ("週六的聚餐我不去了", "聚餐", "no_word"),
])
def test_cancel_check_names_why_it_did_not_count(text, kw, why):
    found, reason, _gap = calendar_extractor.cancel_word_check(text, kw)
    assert (found, reason) == (False, why)


@pytest.mark.parametrize(("text", "kw", "expected"), [
    ("週六的全家聚餐取消了\n媽媽的回診改到下週三", "全家聚餐", False),  # move word is another message
    ("週六的聚餐不延期了，直接取消", "聚餐", False),                   # the move word is negated
    ("原本週六的聚餐，改到週日", "聚餐", True),
])
def test_move_word_must_sit_next_to_the_named_event(text, kw, expected):
    assert calendar_extractor.move_word_near_keyword(text, kw) is expected


@pytest.mark.parametrize(("text", "kw"), [
    ("週六的全家聚餐取消了\n媽媽的回診改到下週三", "全家聚餐"),
    ("週六的聚餐不延期了，直接取消", "聚餐"),
])
def test_cancellation_is_not_turned_into_a_reschedule_by_another_move_word(tier, cal_db, monkeypatch, text, kw):
    calendar_db, db_file = cal_db
    day = future_day(calendar_db, 8)
    event_id = calendar_db.insert_event(group_id="G1", title="全家聚餐", event_date=day, event_time="18:00")
    fake = tier(_cancel_answer(day, kw))
    inline_spawn(monkeypatch)

    main._maybe_capture_calendar_event("G1", text)

    assert fake.generated == [TIER]
    assert _event_status(db_file, event_id) != "active"
    with sqlite3.connect(db_file) as con:
        assert con.execute("SELECT event_date FROM events WHERE event_id = ?", (event_id,)).fetchone()[0] == day


def test_a_date_word_keyword_cannot_pick_an_event(tier, cal_db, monkeypatch):
    calendar_db, db_file = cal_db
    day = future_day(calendar_db, 8)
    event_id = calendar_db.insert_event(group_id="G1", title="週六家庭日", event_date=day, event_time="10:00")
    fake = tier(_cancel_answer(day, "週六"))
    inline_spawn(monkeypatch)

    main._maybe_capture_calendar_event("G1", "週六的健身課取消了")

    assert fake.generated == [TIER]
    assert _event_status(db_file, event_id) == "active"


def test_known_gap_two_topics_on_one_line_still_count():
    """Known residual (FOLLOW_GP I3): another topic's cancel word on the same
    line, comma-separated, still counts.  Tracked by the S2 prompt TODO; change
    this test on purpose when that lands."""
    assert calendar_extractor.cancel_word_near_keyword("週六聚餐我不去了，健身課取消了", "聚餐") is True
