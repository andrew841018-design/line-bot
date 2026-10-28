"""爸媽運動提醒（2026-10-23，PLAN A A3）第一階段：資格判斷、句子、去重列的搶與放、flush 時以 reply token
登記候選、停用版。搭在 `_reply` 上送出是第二階段（等 `_reply` 的 400 處理改寫合進來），這裡不測。

全部合成資料（U_DAD、U_MOM、U_KID、成員甲）；抽取模組大多換成假的（另一個工作階段在改抽取），只留兩個
用真抽取的冒煙測試。別名檔、DB、大字版目錄都由 conftest 指到暫存；開關每個測試從「沒設」開始。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import main
import output_validator
import parent_health as ph
import parent_health_db as db
import parent_health_disabled as ph_off
import parent_health_extract as real_extract

# 真的抽取函式（quiet_extract 每個測試都會換掉；冒煙測試再換回來）
REAL_EXTRACT = real_extract.extract
REAL_RED_FLAG = real_extract.might_have_red_flag

TPE = ZoneInfo("Asia/Taipei")
G = "G_TEST"
HOUR = 3600
DAY = 86400


def ts(y, m, d, h=12, mi=0) -> float:
    return datetime(y, m, d, h, mi, tzinfo=TPE).timestamp()


NOW = ts(2026, 10, 21, 10)  # 週三上午


@dataclass(frozen=True)
class F:
    """和 parent_health_extract.Finding 同欄位。"""

    role: str
    category: str
    item: str
    value: str = ""
    flag: str = ""


@pytest.fixture(autouse=True)
def family(tmp_path, monkeypatch):
    aliases = tmp_path / "user_aliases.json"
    aliases.write_text(json.dumps({"U_DAD": "爸爸", "U_MOM": "媽媽", "U_KID": "成員甲"}, ensure_ascii=False), encoding="utf-8")
    roles = tmp_path / "family_roles.local.json"
    roles.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(roles))
    monkeypatch.setattr(db.settings, "family_group_id", G)  # 家族主群


@pytest.fixture(autouse=True)
def quiet_extract(monkeypatch):
    """抽取換成「什麼都沒提到」；要測抽取結果的測試自己再換。"""
    import parent_health_extract as extract

    monkeypatch.setattr(extract, "extract", lambda text, speaker, **kw: [])
    monkeypatch.setattr(extract, "might_have_red_flag", lambda text: False)
    return extract


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("LINE_BOT_EXERCISE_NUDGE", "1")


@pytest.fixture
def pinned_clock(monkeypatch):
    """提醒只在台北 08:00–20:00 發，資格判斷讀 parent_health._now；`main._register_exercise_nudge` 呼叫時沒帶
    now。釘在 NOW（週三上午 10 點），測試在晚上或清晨跑也是同一個答案（2026-10-25 審查：夜間跑會假紅、
    另兩個測試因為根本沒登記到候選而空過）。"""
    monkeypatch.setattr(ph, "_now", lambda now=None: NOW if now is None else float(now))


def consent(role="爸爸", uid="U_DAD", at=NOW - 20 * DAY):
    db.set_consent(G, role, "on", user_id=uid, changed_by=uid, now=at)


_ids = iter(range(1, 10**6))


def add(role, category, item, value="", flag="", *, at, reporter="U_DAD"):
    return db.add_events(
        G, [F(role, category, item, value, flag)], message_id=f"m{next(_ids)}", reporter_user_id=reporter,
        event_ts=at, source="chat", now=at,
    )


def candidate(messages=(("U_DAD", "今天天氣很好"),), now=NOW):
    return ph.exercise_nudge_candidate(G, list(messages), now=now)


# ── 開關 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value, enabled", [(None, False), ("", False), ("0", False), ("no", False), ("1", True),
                                            ("true", True), (" ON ", True), ("2", False)])
def test_the_switch_is_off_unless_set_and_main_agrees(monkeypatch, value, enabled):
    if value is None:
        monkeypatch.delenv("LINE_BOT_EXERCISE_NUDGE", raising=False)
    else:
        monkeypatch.setenv("LINE_BOT_EXERCISE_NUDGE", value)
    assert ph.exercise_nudge_enabled() is enabled
    assert main._exercise_nudge_on() is enabled
    assert main._EXERCISE_NUDGE_ENV == ph._NUDGE_ENV


def test_switch_off_means_no_candidate():
    consent()
    add("爸爸", "exercise", "散步", at=NOW - 5 * DAY)
    assert candidate() is None


# ── 合格的樣子 ───────────────────────────────────────────────────────────────


def test_a_parent_who_has_not_mentioned_exercise_for_days_gets_one_line(on):
    consent()
    add("爸爸", "exercise", "散步", at=NOW - 5 * DAY)
    nudge = candidate()
    assert (nudge.role, nudge.user_id, nudge.label) == ("爸爸", "U_DAD", "@爸爸")
    first, second = nudge.text.split("\n")
    assert first == "@爸爸" and "10/16" in second and "去散步" in second and second.endswith("吧～")
    assert nudge.archive_text == "爸媽健康：運動提醒"
    result = output_validator.validate_outbound_text(nudge.text)
    assert result.ok and result.text == nudge.text


def test_without_any_exercise_record_it_counts_from_the_recording_start(on):
    consent(at=NOW - 2 * DAY)
    assert candidate() is None  # 才開始記錄 2 天
    consent(at=NOW - 3 * DAY)
    nudge = candidate()
    assert nudge.text.split("\n")[1] in ph._NUDGE_UNSEEN


@pytest.mark.parametrize("days, nudged", [(2, False), (3, True)])
def test_three_calendar_days_since_the_last_exercise(on, days, nudged):
    consent()
    # 日曆日：3 天前的晚上 23:30 也算 3 天
    add("爸爸", "exercise", "散步", at=ts(2026, 10, 21 - days, 23, 30))
    assert (candidate() is not None) is nudged


@pytest.mark.parametrize("hour, minute, nudged", [(7, 59, False), (8, 0, True), (19, 59, True), (20, 0, False)])
def test_only_between_8am_and_8pm_taipei(on, hour, minute, nudged):
    consent()
    assert (candidate(now=ts(2026, 10, 21, hour, minute)) is not None) is nudged


def test_only_the_family_group(on, monkeypatch):
    consent()
    monkeypatch.setattr(db.settings, "family_group_id", "G_OTHER")
    assert candidate() is None


def test_paused_or_not_started_parents_are_never_nudged(on):
    assert candidate() is None  # 家族主群、還沒有同意列（_NONE）
    consent()
    db.set_consent(G, "爸爸", "paused", user_id="U_DAD", changed_by="U_DAD", now=NOW - DAY)
    assert candidate() is None


def test_only_when_the_parent_spoke_in_this_batch(on):
    consent()
    assert candidate([("U_KID", "今天天氣很好")]) is None
    assert candidate([("U_KID", "今天天氣很好"), ("U_DAD", "好")]) is not None


def test_once_per_72_hours(on):
    consent()
    db.claim_alert(G, "爸爸", ph._NUDGE_KIND, "2026-10-18", now=NOW - 71 * HOUR)
    assert candidate() is None
    db.claim_alert(G, "爸爸", ph._NUDGE_KIND, "2026-10-18b", now=NOW - 73 * HOUR)
    with db._conn() as c:
        c.execute("DELETE FROM parent_health_alerts WHERE key = '2026-10-18'")
    assert candidate() is not None


# ── 這批訊息本身（抽取只呼叫、不改） ─────────────────────────────────────────


@pytest.mark.parametrize("category, nudged", [("exercise", False), ("body", False), ("visit", False), ("med", True),
                                              ("sleep", True), ("diet", True)])
def test_a_batch_that_mentions_exercise_health_or_a_visit_gets_no_nudge(on, quiet_extract, monkeypatch, category, nudged):
    consent()
    found = [F("爸爸", category, "x")]
    monkeypatch.setattr(quiet_extract, "extract", lambda text, speaker, **kw: found if "那則" in text else [])
    assert (candidate([("U_DAD", "早"), ("U_KID", "那則")]) is not None) is nudged  # 別人說的也算


def test_each_message_is_read_with_its_own_speaker_and_parent_titles(on, quiet_extract, monkeypatch):
    consent()
    seen = []
    monkeypatch.setattr(quiet_extract, "extract", lambda text, speaker, **kw: seen.append((text, speaker)) or [])
    candidate([("U_DAD", "我出門了"), ("U_KID", "好喔")])
    assert seen == [("我出門了", "爸爸"), ("好喔", "family")]


def test_a_possible_red_flag_anywhere_in_the_batch_gets_no_nudge(on, quiet_extract, monkeypatch):
    consent()
    monkeypatch.setattr(quiet_extract, "might_have_red_flag", lambda text: "胸" in text)
    assert candidate([("U_DAD", "早"), ("U_KID", "胸口")]) is None


@pytest.mark.parametrize(
    "messages",
    [
        [("U_DAD", "長" * 201)],  # 抽取看不了的長度
        [("U_DAD", "看這個 https://example.com/x")],  # 有網址：抽取整則略過
        [("U_DAD", "/健康")],
        [("U_DAD", "早"), ("U_KID", None)],  # 讀不到那則的文字
        [("U_DAD", "早")] + [("U_KID", "嗯" * 190)] * 11,  # 整批超過 2000 字
    ],
)
def test_messages_the_extractor_cannot_read_mean_no_nudge(on, messages):
    consent()
    assert candidate(messages) is None


def test_a_failing_extractor_means_no_nudge(on, quiet_extract, monkeypatch, caplog):
    consent()

    def boom(*_args, **_kwargs):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(quiet_extract, "extract", boom)
    assert candidate() is None
    assert "exercise nudge failed error_type=RuntimeError" in caplog.text


# ── 最近的紀錄（GP2 N8） ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "category, item, value, flag, days_ago, nudged",
    [
        ("body", "頭暈", "", "", 6, False),  # 症狀 7 天內
        ("body", "頭暈", "", "", 8, True),
        ("body", "跌倒", "", "redflag", 6, False),
        ("body", "跌倒", "", "", 6, False),  # 已經好了的緊急字詞也是身體狀況
        ("visit", "回診", "", "", 6, False),
        ("visit", "回診", "", "", 8, True),
        ("body", "血壓", "185/125", "", 13, False),  # 危象血壓 14 天內
        ("body", "血壓", "185/125", "", 15, True),
        ("body", "血糖", "62", "", 13, False),  # 低血糖 14 天內
        ("body", "血壓", "128/82", "", 1, True),  # 一般讀數不擋
    ],
)
def test_recent_health_records_mean_no_nudge(on, category, item, value, flag, days_ago, nudged):
    consent()
    add("爸爸", category, item, value, flag, at=NOW - days_ago * DAY)
    assert (candidate() is not None) is nudged


# ── 爸媽同批 ─────────────────────────────────────────────────────────────────


def test_when_both_parents_qualify_only_the_one_nudged_longest_ago_gets_it(on):
    consent("爸爸", "U_DAD")
    consent("媽媽", "U_MOM")
    both = [("U_DAD", "早"), ("U_MOM", "早安")]
    assert candidate(both).role == "爸爸"  # 都沒提醒過：照爸爸、媽媽的順序
    db.claim_alert(G, "爸爸", ph._NUDGE_KIND, "2026-10-10", now=NOW - 11 * DAY)
    db.claim_alert(G, "媽媽", ph._NUDGE_KIND, "2026-10-12", now=NOW - 9 * DAY)
    assert candidate(both).role == "爸爸"  # 爸爸比較久沒被提醒
    db.claim_alert(G, "爸爸", ph._NUDGE_KIND, "2026-10-14", now=NOW - 7 * DAY)
    nudge = candidate(both)
    assert nudge.role == "媽媽" and nudge.text.startswith("@媽媽\n") and nudge.user_id == "U_MOM"


# ── 句子 ─────────────────────────────────────────────────────────────────────


def _exercise_labels():
    import importlib.util

    spec = importlib.util.find_spec("parent_health_extract")  # quiet_extract 換掉的是函式，詞表照讀真的
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    return sorted(fresh.ALLOWED_ITEMS["exercise"])


def test_every_exercise_label_has_a_natural_verb_phrase():
    labels = _exercise_labels()
    assert labels and set(labels) <= set(ph._NUDGE_VERBS)


def test_every_sentence_passes_the_validator_and_never_says_days_without_exercise():
    from datetime import date

    lines = [ph._nudge_line(role, None, date(2026, 10, 21 + i)) for role in ph.PARENTS for i in range(3)]
    for label in [*_exercise_labels(), "新運動"]:  # 表外的標籤：「做{標籤}」
        for i in range(3):
            lines.append(ph._nudge_line("爸爸", {"item": label, "day": date(2026, 10, 15)}, date(2026, 10, 21 + i)))
    walk = {"item": "散步", "day": date(2026, 10, 15)}
    assert len({ph._nudge_line("爸爸", walk, date(2026, 10, 21 + i)) for i in range(3)}) == 3  # 每天輪替
    assert len({ph._nudge_line("爸爸", None, date(2026, 10, 21 + i)) for i in range(2)}) == 2
    assert any("做新運動是 10/15" in line for line in lines)
    for line in lines:
        for text in (line, f"@爸爸\n{line}"):
            result = output_validator.validate_outbound_text(text)
            assert result.ok and result.text == text, (text, result.reason)
            assert main._prepare_outbound_text(text) == text
        for word in ("天沒運動", "沒有運動", "高血壓", "糖尿病", "應該", "一定要", "不可以"):
            assert word not in line


# ── 搶與放（送出前；GP1 #2–3、GP2 N4） ────────────────────────────────────────


def test_claim_takes_todays_row_once_and_release_gives_it_back(on):
    consent()
    nudge = candidate()
    claim = ph.claim_exercise_nudge(G, nudge, now=NOW + 30)
    assert claim == ("2026-10-21", int(NOW + 30))
    assert ph.claim_exercise_nudge(G, nudge, now=NOW + 40) is None  # 同時 flush 的另一批：搶不到
    assert db.last_alert(G, "爸爸", ph._NUDGE_KIND)["sent_at"] == int(NOW + 30)
    for other in (int(NOW + 29), int(NOW + 31)):  # 不是自己搶到的那列（時間不同）：不動
        ph.release_exercise_nudge(G, nudge, ("2026-10-21", other))
        assert db.last_alert(G, "爸爸", ph._NUDGE_KIND) is not None
    ph.release_exercise_nudge(G, nudge, ("2026-10-20", int(NOW + 30)))  # 日期不同：不動
    assert db.last_alert(G, "爸爸", ph._NUDGE_KIND) is not None
    ph.release_exercise_nudge(G, nudge, claim)
    assert db.last_alert(G, "爸爸", ph._NUDGE_KIND) is None
    assert ph.claim_exercise_nudge(G, nudge, now=NOW + 50) is not None  # 放掉之後可以再搶


def test_the_72_hour_cap_does_not_depend_on_the_date_key(on):
    consent()
    nudge = candidate()
    assert ph.claim_exercise_nudge(G, nudge, now=NOW)
    for days in (1, 2):
        assert candidate(now=NOW + days * DAY) is None  # key 是新的日期，但 72 小時還沒到
    assert candidate(now=NOW + 3 * DAY) is not None


def test_claim_checks_again_and_drops_a_stale_nudge(on, monkeypatch):
    consent()
    nudge = candidate()
    assert ph.claim_exercise_nudge(G, None) is None
    assert ph.claim_exercise_nudge(G, nudge, now=ts(2026, 10, 21, 20, 0)) is None  # 過了 20:00
    db.set_consent(G, "爸爸", "paused", user_id="U_DAD", changed_by="U_KID", now=NOW + 5)
    assert ph.claim_exercise_nudge(G, nudge, now=NOW + 10) is None  # flush 之後才暫停
    consent()
    nudge = candidate(now=NOW + 20)
    add("爸爸", "exercise", "散步", at=NOW + 25)  # 背景抽取剛記下的運動
    assert ph.claim_exercise_nudge(G, nudge, now=NOW + 30) is None
    monkeypatch.delenv("LINE_BOT_EXERCISE_NUDGE")
    assert ph.claim_exercise_nudge(G, nudge, now=NOW + 30) is None
    assert db.last_alert(G, "爸爸", ph._NUDGE_KIND) is None  # 都沒搶


def test_two_flushes_racing_on_the_same_day_send_only_one(on, monkeypatch):
    """兩批幾乎同時 flush、都在對方寫入前讀到「沒提醒過」：今天的 key 由 PK 仲裁，只有一批搶得到。"""
    consent()
    nudge = candidate()
    monkeypatch.setattr(db, "last_alert", lambda *a, **k: None)
    assert ph.claim_exercise_nudge(G, nudge, now=NOW) is not None
    assert ph.claim_exercise_nudge(G, nudge, now=NOW + 1) is None


def test_claim_drops_a_nudge_for_an_account_that_changed(on):
    consent()
    nudge = candidate()
    other = SimpleNamespace(**{**nudge.__dict__})
    assert ph.claim_exercise_nudge(G, other) is None  # 不是這個模組組的提醒
    changed = ph.ExerciseNudge(role="爸爸", user_id="U_SOMEONE_ELSE", text=nudge.text, label=nudge.label)
    assert ph.claim_exercise_nudge(G, changed, now=NOW) is None


def test_deleting_my_records_keeps_the_nudge_row(on):
    consent()
    add("爸爸", "exercise", "散步", at=NOW - 5 * DAY)
    assert ph.claim_exercise_nudge(G, candidate(), now=NOW)
    db.add_events(G, [F("爸爸", "med", "吃藥")], message_id="m-x", reporter_user_id="U_DAD", event_ts=NOW, source="chat",
                  now=NOW)
    assert ph.handle_command(G, "U_DAD", "刪除我的紀錄", message_id="c1", now=NOW + 60).text.startswith("爸媽健康：已刪掉")
    assert db.last_alert(G, "爸爸", ph._NUDGE_KIND) is not None  # 刪完下一句不會又被提醒
    assert candidate(now=NOW + 120) is None
    assert ph._NUDGE_KIND in db._KEPT_ON_DELETE


def test_claim_and_release_never_raise(on, monkeypatch, caplog):
    consent()
    nudge = candidate()

    def boom(*_args, **_kwargs):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(db, "claim_alert", boom)
    monkeypatch.setattr(db, "release_alert", boom)
    assert ph.claim_exercise_nudge(G, nudge, now=NOW) is None
    assert ph.release_exercise_nudge(G, nudge, ("2026-10-21", int(NOW))) is None
    assert "exercise nudge claim failed" in caplog.text and "exercise nudge release failed" in caplog.text


# ── 存檔字串不進模型輸入（GP1 #4） ───────────────────────────────────────────


def test_the_archive_line_is_skipped_by_the_weekly_summary_and_daily_memory():
    import member_labels
    import weekly_summary
    from jobs import daily_memory

    archive = ph.ExerciseNudge("爸爸", "U_DAD", "x", "@爸爸").archive_text
    assert archive == ph_off.ExerciseNudge("爸爸", "U_DAD", "x", "@爸爸").archive_text
    assert archive.startswith(ph.REPLY_PREFIX) and "10/" not in archive and "散步" not in archive
    assert archive.startswith(weekly_summary._HEALTH_PREFIXES)
    assert daily_memory._private_bot_reply(member_labels.BOT_USER_ID, archive)


# ── 真的抽取（冒煙；抽取準確度另一個工作階段在改） ──────────────────────────


def test_real_extractor_smoke(on, quiet_extract, monkeypatch):
    monkeypatch.setattr(quiet_extract, "extract", REAL_EXTRACT)
    monkeypatch.setattr(quiet_extract, "might_have_red_flag", REAL_RED_FLAG)
    consent()
    assert candidate([("U_DAD", "今天天氣很好")]) is not None
    assert candidate([("U_DAD", "我剛散步回來了")]) is None  # 這批自己就在說運動
    assert candidate([("U_DAD", "早"), ("U_KID", "爸爸昨天發燒了")]) is None  # 別人說爸爸不舒服


# ── flush 時登記（main，GP1 #1） ─────────────────────────────────────────────


def test_switch_off_means_no_db_read_and_no_import_at_flush(monkeypatch):
    """GP2 N9(d)：開關關著時 flush 零 DB 讀取、不碰 parent_health（登記函式會吞例外，所以記下來比對，不靠丟例外）。"""
    touched: list[str] = []

    class Spy:
        def __getattr__(self, name):
            touched.append(f"parent_health.{name}")
            raise AttributeError(name)

    monkeypatch.setitem(sys.modules, "parent_health", Spy())
    monkeypatch.setattr(main.memory, "get_raw_message_record", lambda *a: touched.append("db"))
    main._register_exercise_nudge("TOKEN_A", G, ["M1"])
    assert touched == [] and main._take_exercise_nudge("TOKEN_A", G) is None
    monkeypatch.setenv("LINE_BOT_EXERCISE_NUDGE", "1")  # 對照：開著就會讀
    main._register_exercise_nudge("TOKEN_A", G, ["M1"])
    assert touched[0] == "db" and "parent_health.exercise_nudge_candidate" in touched


def test_flush_registers_right_next_to_the_inbound_batch(monkeypatch):
    calls = []

    class Stop(Exception):
        pass

    monkeypatch.setattr(main, "_register_inbound_reply_batch", lambda *a: calls.append(("batch", a)))
    monkeypatch.setattr(main, "_register_exercise_nudge", lambda *a: calls.append(("nudge", a)))
    monkeypatch.setattr(main, "_retryable_burst_db_call", lambda *a, **k: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        main._handle_burst_flush(G, "早", "TOKEN_B", ["M1", "M2"])
    assert calls == [("batch", ("TOKEN_B", G, ["M1", "M2"])), ("nudge", ("TOKEN_B", G, ["M1", "M2"]))]


def _records(monkeypatch, rows):
    monkeypatch.setattr(main.memory, "get_raw_message_record", lambda group_id, message_id: rows.get(message_id))


def test_flush_registers_a_candidate_for_the_reply_token_only_once(on, pinned_clock, monkeypatch):
    consent()
    _records(monkeypatch, {"M1": {"user_id": "U_KID", "text": "早"}, "M2": {"user_id": "U_DAD", "text": "早安"}})
    main._register_exercise_nudge("TOKEN_C", G, ["M1", "M2"])
    assert main._take_exercise_nudge("TOKEN_C", "G_OTHER") is None  # 別的群組拿不到（也被拿掉了）
    main._register_exercise_nudge("TOKEN_C", G, ["M1", "M2"])
    nudge = main._take_exercise_nudge("TOKEN_C", G)
    assert nudge is not None and nudge.role == "爸爸"
    assert main._take_exercise_nudge("TOKEN_C", G) is None  # 只給一次


def test_a_registered_candidate_expires(on, pinned_clock, monkeypatch):
    consent()
    _records(monkeypatch, {"M1": {"user_id": "U_DAD", "text": "早安"}})
    main._register_exercise_nudge("TOKEN_D", G, ["M1"])
    assert "TOKEN_D" in main._exercise_nudge_by_token  # 先確定真的登記到了；不然下面的「過期」是空過
    real_time = main.time.time
    monkeypatch.setattr(main.time, "time", lambda: real_time() + main._EXERCISE_NUDGE_TTL_S + 1)
    assert main._take_exercise_nudge("TOKEN_D", G) is None


def test_a_missing_message_or_a_failure_registers_nothing(on, monkeypatch, caplog):
    consent()
    _records(monkeypatch, {"M1": {"user_id": "U_DAD", "text": "早安"}})
    main._register_exercise_nudge("TOKEN_E", G, ["M1", "M_GONE"])  # 讀不到的那則：看不出說了什麼
    assert main._take_exercise_nudge("TOKEN_E", G) is None
    monkeypatch.setattr(main.memory, "get_raw_message_record", lambda *a: (_ for _ in ()).throw(OSError("db")))
    main._register_exercise_nudge("TOKEN_E", G, ["M1"])  # 不丟例外：burst 不會因此重試
    assert main._take_exercise_nudge("TOKEN_E", G) is None
    assert "exercise nudge check skipped error_type=OSError" in caplog.text


def test_a_cancelled_burst_leaves_no_candidate(on, pinned_clock, monkeypatch):
    import burst_filter

    # 對照：同一批留言如果真的 flush 了，這個環境下一定登記得到候選；所以下面「沒有」是取消造成的
    consent()
    _records(monkeypatch, {"M_CANCEL": {"user_id": "U_DAD", "text": "早安"}})
    main._register_exercise_nudge("TOKEN_CONTROL", G, ["M_CANCEL"])
    assert main._take_exercise_nudge("TOKEN_CONTROL", G) is not None

    registered = []
    monkeypatch.setattr(main, "_register_exercise_nudge", lambda *a: registered.append(a))
    burst_filter.add_to_burst(G, "M_CANCEL", "早安", "U_DAD", "TOKEN_F")
    burst_filter.cancel_burst(G)
    assert registered == [] and main._take_exercise_nudge("TOKEN_F", G) is None


# ── 組成要送的訊息（第二階段在 _reply 裡用） ─────────────────────────────────


def test_the_message_mentions_the_parent_once_by_user_id(on):
    consent()
    nudge = candidate()
    message = main._exercise_nudge_message(nudge)
    data = message.to_dict()
    assert data["type"] == "textV2"
    assert data["text"].startswith("{p1}\n") and "@爸爸" not in data["text"]  # 不會「{p1}\n@爸爸」
    assert data["substitution"]["p1"]["mentionee"] == {"type": "user", "userId": "U_DAD"}


@pytest.mark.parametrize("change", [{"label": "@媽媽"}, {"user_id": ""}, {"text": "沒有第一行"}])
def test_a_malformed_nudge_builds_no_message(change):
    nudge = ph.ExerciseNudge(role="爸爸", user_id="U_DAD", text="@爸爸\n上次聽到你去散步是 10/16，這幾天有空的話出去走走吧～",
                             label="@爸爸")
    assert main._exercise_nudge_message(nudge) is not None
    assert main._exercise_nudge_message(ph.ExerciseNudge(**{**nudge.__dict__, **change})) is None


def test_a_message_the_validator_rewrites_is_dropped(monkeypatch):
    nudge = ph.ExerciseNudge(role="爸爸", user_id="U_DAD", text="@爸爸\n上次聽到你去散步是 10/16，這幾天有空的話出去走走吧～",
                             label="@爸爸")
    monkeypatch.setattr(main, "_prepare_outbound_text", lambda text, source="reply": "")
    assert main._exercise_nudge_message(nudge) is None


# ── 停用版 ───────────────────────────────────────────────────────────────────


def test_the_disabled_stub_never_nudges(on):
    consent()
    nudge = candidate()
    assert ph_off.exercise_nudge_enabled() is False
    assert ph_off.exercise_nudge_candidate(G, [("U_DAD", "早")], now=NOW) is None
    assert ph_off.claim_exercise_nudge(G, nudge, now=NOW) is None
    assert ph_off.release_exercise_nudge(G, nudge, ("2026-10-21", int(NOW))) is None
    assert db.last_alert(G, "爸爸", ph._NUDGE_KIND) is None


# ═══ 第二階段（2026-10-24）：搭在 _reply 上送出 ═══════════════════════════════════════════════
#
# 運動提醒是咪寶本來就要送的主回覆的最後一則：訊息先組好驗過、再搶今天的去重列（claim 裡重算資格）、才放進
# 這次的回覆。結果：LINE 接受 -> 列留著（72 小時上限）；明確沒送出 -> 放掉；含糊 -> 留著（寧可少提醒）；
# 被 LINE 點名拒絕 -> 留著（圍住：不要每次回覆都再試），而且不會改成純文字重送；送出前就結束 -> 放掉。
# 存檔只存固定句，不含日期、項目。

from test_reply_400_recovery import (  # noqa: E402  (共用的假 LINE 與錯誤本體)
    BAD_TOKEN,
    FakeLine,
    _FakeApiClient,
    api_error,
    bad,
    due_natural,
    kinds,
    raw_bot_texts,
    texts,
)

T = "TOKEN_NUDGE"
SAY = "早安，今天天氣不錯"
NEEDLES = ("散步", "10/16")  # 提醒句裡的項目與日期：任何存檔列都不可以出現


@pytest.fixture()
def line(monkeypatch):
    fake = FakeLine()
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: False)
    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", lambda _client: fake)
    monkeypatch.setattr(main, "_get_line_config", lambda: object())
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    return fake


def arm(token=T):
    """爸爸 5 天沒提到運動、flush 時合格：登記一則候選給這個 reply token。"""
    consent()
    add("爸爸", "exercise", "散步", at=NOW - 5 * DAY)
    nudge = candidate()
    assert nudge is not None and all(needle in nudge.text for needle in NEEDLES)
    main._exercise_nudge_by_token[token] = (G, nudge, main.time.time())
    return nudge


def say(text=SAY, token=T, **kwargs):
    return main._reply(token, text, group_id=G, **kwargs)


def open_event(message_id="M1", token=T):
    assert main.memory.begin_inbound_event(G, message_id) == "new"
    main._register_inbound_reply_token(token, G, message_id)


def kept():
    """今天的去重列（claim 搶到的那一列）；沒有就是 None。"""
    return db.last_alert(G, "爸爸", ph._NUDGE_KIND)


def no_trace():
    """不變式：沒有任何 raw_messages 的 bot 列含提醒的日期或項目（review、月精華、embedding 會直接讀）。"""
    rows = raw_bot_texts()
    assert all(needle not in row for row in rows for needle in NEEDLES), rows


def test_the_nudge_is_the_last_message_of_the_reply_and_its_row_is_kept(on, pinned_clock, line):
    arm()

    assert say() is True

    batch = line.replies[0]
    assert kinds(batch) == ["TextMessage", "TextMessageV2"] and texts(batch)[0] == SAY
    data = batch[1].to_dict()
    assert data["text"].startswith("{p1}\n") and data["substitution"]["p1"]["mentionee"] == {"type": "user", "userId": "U_DAD"}
    assert kept() is not None
    assert main._take_exercise_nudge(T, G) is None  # taken: a candidate is used once


def test_the_archive_is_the_fixed_line_and_never_enters_recall(on, pinned_clock, line, monkeypatch):
    arm()
    logged = []
    real = main.memory.log_raw_message
    monkeypatch.setattr(main.memory, "log_raw_message", lambda *a, **k: logged.append((a[3], k)) or real(*a, **k))

    assert say() is True

    assert logged == [(SAY, {}), ("爸媽健康：運動提醒", {"index_for_recall": False})]
    no_trace()


def test_a_due_reminder_comes_first_and_the_references_stay_aligned(on, pinned_clock, line, monkeypatch):
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: True)
    rid = due_natural(monkeypatch, group=G, user="U_KID")
    arm()

    assert say() is True

    batch = line.replies[0]
    assert kinds(batch) == ["TextMessage", "TextMessageV2", "TextMessageV2"]
    assert "繳房屋稅" in texts(batch)[1] and texts(batch)[2].startswith("{p1}\n")
    assert main.memory.get_sent_reminder_reference(G, "sent-2")["reminder_id"] == rid
    assert main.memory.get_sent_reminder_reference(G, "sent-3") is None  # the nudge has no reminder reference
    no_trace()


def test_a_full_reply_leaves_the_nudge_alone(on, pinned_clock, line):
    for n in range(4):
        main.memory.enqueue_reminder_confirmation(G, f"pending_reminder:{n}", f"已新增提醒\n事項：事項{n}")
    arm()

    assert say() is True

    assert len(line.replies[0]) == 5 and not any(t.startswith("{p1}") for t in texts(line.replies[0]))
    assert kept() is None and T in main._exercise_nudge_by_token  # not claimed, not taken


@pytest.mark.parametrize("kwargs", [{"include_auxiliary": False}, {"large_menu_card": True}, {"menu_card": True}])
def test_replies_without_auxiliary_messages_get_no_nudge(on, pinned_clock, line, kwargs):
    arm()

    assert say(**kwargs) is True

    assert len(line.replies[0]) == 1 and kept() is None


def test_a_flex_card_gets_no_nudge(on, pinned_clock, line, monkeypatch):
    arm()
    monkeypatch.setattr(main, "_validated_flex_card_message", lambda alt, card: main.TextMessage(text=alt))

    assert say("爸媽健康卡：總覽", flex_card={"type": "bubble"}) is True

    assert len(line.replies[0]) == 1 and kept() is None


def test_a_reply_without_a_group_gets_no_nudge(on, pinned_clock, line):
    arm()

    assert main._reply(T, SAY) is True

    assert len(line.replies[0]) == 1 and kept() is None


def test_a_silent_primary_never_carries_the_nudge_alone(on, pinned_clock, line, monkeypatch):
    """The 「Gemini 暫時忙不過來」 sink suppresses the primary; due reminders may still ride alone,
    the nudge must not (it only rides on a reply the bot meant to give)."""
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: True)
    due_natural(monkeypatch, group=G, user="U_KID")
    arm()

    assert say("Gemini 暫時忙不過來") is True

    assert kinds(line.replies[0]) == ["TextMessageV2"] and "繳房屋稅" in texts(line.replies[0])[0]
    assert kept() is None
    arm("TOKEN_OTHER")
    assert main._reply("TOKEN_OTHER", "Gemini 暫時忙不過來", group_id=G) is False  # nothing at all to send
    assert kept() is None


def test_a_muted_bot_claims_nothing(on, pinned_clock, line, monkeypatch):
    arm()
    monkeypatch.setattr(main.settings, "bot_muted", True)

    assert say() is False

    assert not line.replies and kept() is None


def test_with_the_switch_off_nothing_is_read_or_imported(pinned_clock, line, monkeypatch):
    nudge = ph.ExerciseNudge(role="爸爸", user_id="U_DAD", text="@爸爸\n上次聽到你去散步是 10/16，這幾天有空的話出去走走吧～",
                             label="@爸爸")
    main._exercise_nudge_by_token[T] = (G, nudge, main.time.time())
    touched = []

    class Spy:
        def __getattr__(self, name):
            touched.append(name)
            raise AttributeError(name)

    monkeypatch.setitem(sys.modules, "parent_health", Spy())
    for name in ("_take_exercise_nudge", "_exercise_nudge_message", "_claim_exercise_nudge_for_reply"):
        monkeypatch.setattr(main, name, lambda *a, _name=name, **k: touched.append(_name))

    assert say() is True

    assert touched == [] and len(line.replies[0]) == 1
    assert T in main._exercise_nudge_by_token


def test_the_message_is_built_and_checked_before_anything_is_claimed(on, pinned_clock, line, monkeypatch):
    arm()
    claimed = []
    monkeypatch.setattr(main, "_exercise_nudge_message", lambda nudge: None)
    monkeypatch.setattr(ph, "claim_exercise_nudge", lambda *a, **k: claimed.append(a))

    assert say() is True

    assert claimed == [] and len(line.replies[0]) == 1 and kept() is None


def test_the_claim_checks_again_and_a_parent_paused_since_the_flush_gets_nothing(on, pinned_clock, line):
    arm()
    db.set_consent(G, "爸爸", "paused", user_id="U_DAD", changed_by="U_DAD", now=NOW - 1)

    assert say() is True

    assert len(line.replies[0]) == 1 and kept() is None


def test_two_replies_flushed_together_carry_one_nudge(on, pinned_clock, line):
    nudge = arm("TOKEN_ONE")
    main._exercise_nudge_by_token["TOKEN_TWO"] = (G, nudge, main.time.time())

    assert say(token="TOKEN_ONE") is True
    assert say(token="TOKEN_TWO") is True

    assert [len(batch) for batch in line.replies] == [2, 1]


# ── 結果：接受／含糊／明確沒送出／被 LINE 拒絕 ─────────────────────────────────────────────


def test_an_ambiguous_send_keeps_the_row_and_pushes_nothing(on, pinned_clock, line):
    arm()
    line.script = [TimeoutError("read timed out")]

    assert say() is False

    assert not line.pushes and kept() is not None
    no_trace()


def test_a_dead_token_hands_the_row_back_and_the_push_carries_the_primary_only(on, pinned_clock, line):
    arm()
    open_event()
    line.script = [BAD_TOKEN]

    assert say() is True

    assert len(line.pushes) == 1 and texts(line.pushes[0][0]) == [SAY]
    assert kept() is None  # the nudge was not delivered: the parent is not skipped for 72 hours
    no_trace()


@pytest.mark.parametrize("code", [413, 429])
def test_a_refusal_that_is_not_a_content_error_hands_the_row_back(on, pinned_clock, line, code):
    arm()
    line.script = [api_error(code, {"message": "no"}, "No")]

    assert say() is False

    assert kept() is None


def test_nothing_sent_because_the_fence_is_taken_hands_the_row_back(on, pinned_clock, line):
    arm()
    open_event()
    assert main.memory.claim_inbound_reply(G, ["M1"]) == ("claimed", False)  # another run is sending

    assert say() is False

    assert not line.replies and kept() is None


def test_nothing_sent_because_the_request_could_not_be_built_hands_the_row_back(on, pinned_clock, line, monkeypatch):
    arm()

    def boom(**_kwargs):
        raise ValueError("cannot build")

    monkeypatch.setattr(main, "ReplyMessageRequest", boom)

    assert say() is False

    assert not line.replies and kept() is None


def test_a_refused_nudge_is_left_out_never_sent_as_plain_text_and_its_row_is_kept(on, pinned_clock, line):
    nudge = arm()
    line.script = [bad(1)]

    assert say() is True

    assert len(line.replies) == 2 and kinds(line.replies[1]) == ["TextMessage"] and texts(line.replies[1]) == [SAY]
    assert all(nudge.text not in text and "@爸爸" not in text for batch in line.replies[1:] for text in texts(batch))
    assert kept() is not None  # fenced: the 72 hour cap stops it from coming back on every reply
    assert raw_bot_texts() == [SAY]
    no_trace()


def test_a_refused_nudge_and_a_refused_primary_still_deliver_the_primary_as_plain_text(on, pinned_clock, line):
    nudge = arm()
    line.script = [bad(1), bad(0)]

    assert say() is True

    assert [kinds(batch) for batch in line.replies] == [["TextMessage", "TextMessageV2"], ["TextMessage"], ["TextMessage"]]
    assert all(nudge.text not in text for batch in line.replies[1:] for text in texts(batch))
    assert kept() is not None
    no_trace()


def test_a_refused_primary_hands_the_nudge_back_and_goes_alone(on, pinned_clock, line):
    arm()
    line.script = [bad(0)]

    assert say() is True

    assert kinds(line.replies[1]) == ["TextMessage"] and texts(line.replies[1]) == [SAY]
    assert kept() is None
    no_trace()


def test_a_refused_primary_and_a_dead_token_push_the_primary_and_hand_the_nudge_back(on, pinned_clock, line):
    arm()
    open_event()
    line.script = [bad(0), BAD_TOKEN]

    assert say() is True

    assert len(line.pushes) == 1 and texts(line.pushes[0][0]) == [SAY] and kept() is None
    no_trace()


def test_the_main_reply_does_not_vanish_when_the_nudge_is_refused_with_the_real_sdk_model(on, pinned_clock, line):
    """(review GP1 #5c) one refused message used to fail the whole batch; the primary must survive."""
    arm()
    line.script = [bad(1, prop="substitution.p1.mentionee.userId")]
    open_event()

    assert say() is True

    assert texts(line.replies[-1]) == [SAY] and main.memory.get_inbound_event_status(G, "M1") == "replied"
    assert kept() is not None and not line.pushes
