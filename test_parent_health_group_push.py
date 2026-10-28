"""爸媽健康：私訊改推家族群組＋去重紀錄定期清除（2026-10-28）。

開關 LINE_BOT_PARENT_HEALTH_GROUP_PUSH 每個測試由 conftest 先刪掉（＝私訊）；LINE 推播由 conftest 換成只記錄的
替身，conftest 預設靜音，要看到推播的測試用 pushes fixture 解除。全部合成資料（U_DAD、U_MOM、U_KID、成員甲），
家族主群是 G_TEST。私訊模式的既有行為由 test_parent_health.py 等既有測試守住（期望值不改）。
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import line_push_client
import main
import output_validator
import parent_health as ph
import parent_health_analysis as pa
import parent_health_db as db
import parent_health_disabled as ph_off

REAL_PUSH = line_push_client.push_messages  # collection 時還沒被 conftest 換掉：傳輸層測試用真的

TPE = ZoneInfo("Asia/Taipei")
G = "G_TEST"
G2 = "G_TEST_2"
SWITCH = "LINE_BOT_PARENT_HEALTH_GROUP_PUSH"
DAY = 86400


def ts(y, m, d, h=12, mi=0) -> float:
    return datetime(y, m, d, h, mi, tzinfo=TPE).timestamp()


NOW = ts(2026, 11, 1, 21, 30)  # 週日晚上，開關生效那天
LONG_AGO = ts(2026, 8, 1, 9)
HEAD = "🩺 爸媽健康每日檢查（11/1）"
FAMILY_TEXT = "👨‍👩‍👧‍👦 家族熱話週報（本週）\n成員甲：散步"


@dataclass(frozen=True)
class F:
    """和 parent_health_extract.Finding 同欄位。"""

    role: str
    category: str
    item: str
    value: str = ""
    flag: str = ""


@dataclass(frozen=True)
class RF:
    """和 parent_health_extract.RedFlag 同欄位。"""

    role: str
    label: str
    urgent: bool = True
    in_care: bool = False
    future: bool = False


_ids = iter(range(1, 10**6))


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
    """抽取換成「什麼都沒提到」；要測緊急字詞的測試用 _red 再換。"""
    import parent_health_extract as extract

    monkeypatch.setattr(extract, "extract", lambda text, speaker, **kw: [])
    monkeypatch.setattr(extract, "might_have_red_flag", lambda text: False)
    monkeypatch.setattr(extract, "detect_red_flags", lambda text, speaker, **kw: [])
    return extract


@pytest.fixture(autouse=True)
def no_reminders(monkeypatch):
    import memory

    monkeypatch.setattr(memory, "list_pending_reminders_full", lambda group_id=None, *, dedupe=True: [])


@pytest.fixture
def group_on(monkeypatch):
    monkeypatch.setenv(SWITCH, "2026-11-01")


@pytest.fixture
def pushes(monkeypatch):
    """conftest 的 LINE 推播替身記下的呼叫；解除靜音才推得出去。"""
    monkeypatch.setattr(db.settings, "bot_muted", False)
    return line_push_client.push_messages.calls


@pytest.fixture
def dms(monkeypatch):
    import notify_discord

    sent: list[str] = []
    monkeypatch.setattr(notify_discord, "send_dm", lambda text: sent.append(text) or True)
    return sent


def consent(role="爸爸", uid="U_DAD", at=LONG_AGO, group=G):
    db.set_consent(group, role, "on", user_id=uid, changed_by=uid, now=at)


def add(role, category, item, value="", flag="", *, at, reporter="U_DAD", group=G):
    return db.add_events(
        group, [F(role, category, item, value, flag)], message_id=f"m{next(_ids)}", reporter_user_id=reporter,
        event_ts=at, source="chat", now=at,
    )


def sleepless(role="爸爸", reporter="U_DAD", *, group=G, until=NOW, days=4):
    for offset in range(days):
        add(role, "sleep", "睡不好", at=until - offset * DAY - 3600, reporter=reporter, group=group)


def cmd(body, user="U_DAD", *, now=NOW):
    return ph.handle_command(G, user, body, message_id=f"c{next(_ids)}", now=now)


def _red(monkeypatch, flags):
    import parent_health_extract as extract

    monkeypatch.setattr(extract, "might_have_red_flag", lambda text: True)
    monkeypatch.setattr(extract, "detect_red_flags", lambda text, speaker, **kw: list(flags))


def decide(*, msg="r1", event=NOW, now=NOW, user="U_KID"):
    return ph.red_flag_decision(G, user, "合成的一句", message_id=msg, event_ts_ms=int(event * 1000), now=now)


def texts(calls) -> list[str]:
    return [call["messages"][0]["text"] for call in calls]


def assert_push_ok(text: str) -> None:
    result = output_validator.validate_outbound_text(text)
    assert result.ok and result.text == text, (result.reason, text)


def assert_reply_ok(text: str) -> None:
    assert_push_ok(text)
    assert text.startswith(ph.REPLY_PREFIX)
    assert main._prepare_outbound_text(text, source="reply") == text
    assert not main._is_system_status_outbound(text)


# ── 開關與替身 ──────────────────────────────────────────────────────────────


def test_conftest_swaps_line_push_for_a_recorder_and_starts_every_test_in_dm_mode():
    assert hasattr(line_push_client.push_messages, "calls") and line_push_client.push_messages is not REAL_PUSH
    assert os.environ.get(SWITCH) == ""
    assert db.settings.bot_muted is True
    assert ph._group_mode(G, NOW) is False


@pytest.mark.parametrize(
    "value, at, expected",
    [
        (None, NOW, False),
        ("", NOW, False),
        ("0", NOW, False),
        ("off", NOW, False),
        ("false", NOW, False),
        ("no", NOW, False),
        ("garbage", NOW, False),
        ("2026-13-01", NOW, False),
        ("2026/11/01", NOW, False),
        ("20261101", NOW, False),
        ("1", NOW, True),
        ("on", NOW, True),
        (" TRUE ", NOW, True),
        ("yes", NOW, True),
        ("2026-11-01", ts(2026, 10, 31, 23, 59) + 59, False),  # 台北 10/31 23:59:59
        ("2026-11-01", ts(2026, 11, 1, 0, 0), True),  # 台北 11/1 00:00 整
        ("2026-11-01", NOW, True),
    ],
)
def test_switch_values_and_the_taipei_midnight_boundary(monkeypatch, value, at, expected):
    if value is None:
        monkeypatch.delenv(SWITCH, raising=False)
    else:
        monkeypatch.setenv(SWITCH, value)
    assert ph._group_mode(G, at) is expected
    assert ph._group_mode(G2, at) is False  # 不是家族主群：永遠私訊


def test_a_dotenv_loaded_in_the_middle_of_a_test_keeps_dm_mode(tmp_path):
    """todo、weekly_summary 這類模組 import 時會 load_dotenv(override=False)：conftest 把開關設成空字串，
    .env 裡的開關不會被加回來（GP1 #1）。"""
    from dotenv import load_dotenv

    env = tmp_path / ".env"
    env.write_text(f"{SWITCH}=on\n", encoding="utf-8")
    load_dotenv(env, override=False)
    assert os.environ.get(SWITCH) == ""
    assert ph._group_mode(G, NOW) is False


def test_without_a_family_group_everything_stays_dm(monkeypatch):
    monkeypatch.setenv(SWITCH, "on")
    monkeypatch.setattr(db.settings, "family_group_id", "")
    assert ph._group_mode(G, NOW) is False and ph._group_mode("", NOW) is False


# ── 推播出口 ────────────────────────────────────────────────────────────────


def test_group_push_is_skipped_while_muted_and_when_the_validator_says_no(monkeypatch, caplog):
    calls = line_push_client.push_messages.calls
    caplog.set_level("DEBUG")
    assert ph._push_group(G, "🩺 爸媽健康：測試", kind="daily_check", key="k") is False  # conftest 預設靜音
    assert calls == []
    monkeypatch.setattr(db.settings, "bot_muted", False)
    monkeypatch.setattr(
        output_validator, "validate_outbound_text",
        lambda text, **kw: output_validator.ValidationResult(ok=False, text="替代文字", reason="synthetic_block"),
    )
    assert ph._push_group(G, "爸爸的血壓 185/125", kind="daily_check", key="k") is False
    assert calls == []  # 不推替代文字
    assert "reason=muted" in caplog.text and "reason=synthetic_block" in caplog.text
    assert "185" not in caplog.text and "爸爸" not in caplog.text


def test_group_push_only_goes_to_the_family_group(monkeypatch, pushes, caplog):
    caplog.set_level("DEBUG")
    assert ph._push_group(G2, "🩺 爸媽健康：測試", kind="daily_check", key="k") is False
    monkeypatch.setattr(db.settings, "family_group_id", "")
    assert ph._push_group(G, "🩺 爸媽健康：測試", kind="daily_check", key="k") is False
    assert pushes == []  # 不呼叫傳輸層
    assert "reason=not_family_group" in caplog.text and G2 not in caplog.text
    monkeypatch.setattr(db.settings, "family_group_id", G)
    assert ph._push_group(G, "🩺 爸媽健康：測試", kind="daily_check", key="k") is True and len(pushes) == 1


def test_group_push_sends_one_text_with_a_fixed_prefixed_retry_key(pushes):
    for key in ("2026-11-01", "2026-11-01", "2026-11-02"):
        assert ph._push_group(G, "🩺 爸媽健康：測試", kind="daily_check", key=key) is True
    first, again, other = pushes
    assert first["to"] == G and first["messages"] == [{"type": "text", "text": "🩺 爸媽健康：測試"}]
    assert first["retry_key"] == str(uuid.uuid5(uuid.NAMESPACE_URL, f"parent_health:{G}:daily_check:2026-11-01"))
    assert again["retry_key"] == first["retry_key"] != other["retry_key"]
    assert first["ok_statuses"] == (200, 409) and first["timeout"] == 10


@pytest.mark.parametrize("status, expected", [(200, True), (409, True), (429, False), (500, False)])
def test_group_push_counts_409_as_sent_through_the_real_client(monkeypatch, status, expected):
    import requests

    seen: list[str] = []

    def post(url, headers=None, json=None, timeout=None):
        seen.append(headers["X-Line-Retry-Key"])
        return SimpleNamespace(status_code=status, text="x", json=lambda: {})

    monkeypatch.setattr(line_push_client, "push_messages", REAL_PUSH)
    monkeypatch.setattr(line_push_client, "line_access_token", lambda fallback_token=None: "token")
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(db.settings, "bot_muted", False)
    assert ph._push_group(G, "🩺 爸媽健康：測試", kind="notice", key="m1") is expected
    assert seen == [str(uuid.uuid5(uuid.NAMESPACE_URL, f"parent_health:{G}:notice:m1"))]


def test_group_push_failure_is_false_and_logs_only_the_error_type(monkeypatch, caplog):
    def boom(*_a, **_kw):
        raise line_push_client.LinePushError("LINE push HTTP 429: 爸爸 185/125", status_code=429)

    monkeypatch.setattr(line_push_client, "push_messages", boom)
    monkeypatch.setattr(db.settings, "bot_muted", False)
    caplog.set_level("DEBUG")
    assert ph._push_group(G, "🩺 爸媽健康：爸爸 185/125", kind="daily_check", key="k") is False
    assert "error_type=LinePushError" in caplog.text and "185" not in caplog.text


def test_group_push_runs_in_a_daemon_thread_when_background_is_on(monkeypatch, pushes):
    monkeypatch.setattr(ph, "_BACKGROUND_ENABLED", True)
    done = threading.Event()
    seen = []

    def push(to, messages, **kwargs):
        seen.append(threading.current_thread())
        done.set()
        return True

    monkeypatch.setattr(line_push_client, "push_messages", push)
    ph._push_group_background(G, "🩺 爸媽健康：測試", kind="notice", key="m")
    assert done.wait(5)
    assert seen[0] is not threading.main_thread() and seen[0].daemon


# ── 119 結果 ────────────────────────────────────────────────────────────────


def _outcome(decision, *, msg="r1", sent=False, suppressed=False, muted=False, now=NOW):
    ph.red_flag_outcome(G, decision, message_id=msg, sent=sent, suppressed=suppressed, muted=muted, now=now)


@pytest.mark.parametrize(
    "flag, event, expected",
    [
        (RF("爸爸", "跌倒"), NOW - 601,
         "🩺 爸媽健康：爸爸 21:19 提到跌倒（咪寶這次沒有回 119 提醒：訊息超過 10 分鐘），請家人確認他現在的狀況"),
        (RF("媽媽", "急診", in_care=True), NOW,
         "🩺 爸媽健康：媽媽 21:30 提到急診（咪寶這次沒有回 119 提醒：說已經在急診或送醫），請家人確認她現在的狀況"),
        (RF("爸爸", "胸痛胸悶", future=True), NOW,
         "🩺 爸媽健康：爸爸 21:30 提到胸痛胸悶（咪寶這次沒有回 119 提醒：說的是之後的事），請家人確認他現在的狀況"),
    ],
)
def test_group_mode_pushes_the_outcomes_the_group_did_not_hear_and_sends_no_dm(
    monkeypatch, group_on, pushes, dms, flag, event, expected
):
    consent("爸爸", "U_DAD")
    consent("媽媽", "U_MOM")
    _red(monkeypatch, [flag])
    decision = decide(event=event)
    assert decision is not None and not decision.reply
    _outcome(decision)
    assert texts(pushes) == [expected] and dms == []
    assert pushes[0]["retry_key"] == str(uuid.uuid5(uuid.NAMESPACE_URL, f"parent_health:{G}:notice:r1"))
    assert db.last_alert(G, flag.role, f"notice:{flag.label}")["key"] == "r1"
    assert db.last_alert(G, flag.role, f"dm:{flag.label}") is None
    assert_push_ok(expected)


def test_group_mode_skips_what_the_group_already_got_or_while_muted(monkeypatch, group_on, pushes, dms):
    consent("爸爸", "U_DAD")
    consent("媽媽", "U_MOM")
    _red(monkeypatch, [RF("爸爸", "跌倒")])
    first = decide(msg="a")
    assert first.reason == "send"
    _outcome(first, msg="a", sent=True)  # 群組收到 119 提醒了
    again = decide(msg="b", now=NOW + 60, event=NOW + 60)
    assert again.reason == "deduped"
    _outcome(again, msg="b", now=NOW + 60)  # 30 分鐘內回過
    _red(monkeypatch, [RF("媽媽", "跌倒")])
    failed = decide(msg="c")
    assert failed.reason == "send"
    _outcome(failed, msg="c", sent=False)  # 回覆失敗：main 已試過 push 備援
    _red(monkeypatch, [RF("媽媽", "中風")])
    old = decide(msg="d", event=NOW - 900)
    assert old.reason == "old_event"
    _outcome(old, msg="d", muted=True)  # 靜音中
    assert pushes == [] and dms == []
    assert db.last_alert(G, "媽媽", "notice:中風") is None  # 靜音時連搶都不搶
    assert db.last_alert(G, "爸爸", "reply")["key"] == "a"  # 119 回覆的去重照舊


def test_group_notice_is_pushed_once_per_parent_and_label_per_30_minutes(monkeypatch, group_on, pushes):
    consent("爸爸", "U_DAD")
    _red(monkeypatch, [RF("爸爸", "跌倒")])
    for offset, expected in ((0, 1), (60, 1), (1799, 1), (1800, 2)):
        decision = decide(msg=f"o{offset}", now=NOW + offset, event=NOW + offset - 601)
        assert decision.reason == "old_event"
        _outcome(decision, msg=f"o{offset}", now=NOW + offset)
        assert len(pushes) == expected, offset


@pytest.mark.parametrize(
    "flags, event, expected",
    [
        ([RF("爸爸", "跌倒"), RF("媽媽", "急診", in_care=True)], NOW - 601,
         "🩺 爸媽健康：21:19 爸爸提到跌倒（訊息超過 10 分鐘）；媽媽提到急診（說已經在急診或送醫）；"
         "咪寶這次都沒有回 119 提醒，請家人確認他們現在的狀況"),
        ([RF("爸爸", "跌倒", future=True), RF("媽媽", "胸痛胸悶", future=True)], NOW,
         "🩺 爸媽健康：21:30 爸爸提到跌倒；媽媽提到胸痛胸悶（咪寶這次沒有回 119 提醒：說的是之後的事），"
         "請家人確認他們現在的狀況"),
    ],
)
def test_both_parents_in_one_message_make_one_push(monkeypatch, group_on, pushes, flags, event, expected):
    consent("爸爸", "U_DAD")
    consent("媽媽", "U_MOM")
    _red(monkeypatch, flags)
    _outcome(decide(event=event))
    assert texts(pushes) == [expected]
    assert_push_ok(expected)


def test_same_parent_and_label_is_pushed_again_after_30_minutes_no_daily_cap(monkeypatch, group_on, pushes):
    """Andrew 10/28：119 結果推群組不設每日上限，只靠 30 分鐘去重。同一天每隔 30 分鐘都會再推一次。"""
    consent("爸爸", "U_DAD")
    _red(monkeypatch, [RF("爸爸", "跌倒")])
    for index in range(4):  # 21:30、22:00、22:30、23:00（台北同一天）
        at = NOW + index * 1800
        _outcome(decide(msg=f"n{index}", now=at, event=at - 601), msg=f"n{index}", now=at)
    assert len(pushes) == 4
    assert all(text.startswith("🩺 爸媽健康：爸爸 ") and "提到跌倒（" in text for text in texts(pushes))
    assert db.last_alert(G, "爸爸", "notice:跌倒")["key"] == "n3"


def test_a_broken_notice_dedupe_still_pushes(monkeypatch, group_on, pushes):
    def boom(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")

    for name in ("last_alert", "claim_alert"):
        monkeypatch.setattr(db, name, boom)
    decision = ph.RedFlagDecision("爸爸", ("跌倒",), False, True, True, "old_event", NOW - 601)
    _outcome(decision)
    assert len(pushes) == 1  # 去重壞了寧可多推一次


def test_the_notice_push_leaves_the_webhook_lock_on_a_daemon_thread(monkeypatch, group_on):
    consent("爸爸", "U_DAD")
    _red(monkeypatch, [RF("爸爸", "跌倒")])
    decision = decide(event=NOW - 601)
    monkeypatch.setattr(ph, "_BACKGROUND_ENABLED", True)
    done = threading.Event()
    seen = []

    def push(group_id, text, *, kind, key):
        seen.append((threading.current_thread(), kind, key))
        done.set()
        return True

    monkeypatch.setattr(ph, "_push_group", push)
    _outcome(decision)
    assert done.wait(5)
    thread, kind, key = seen[0]
    assert thread is not threading.main_thread() and thread.daemon and (kind, key) == ("notice", "r1")


def test_dm_mode_in_the_family_group_still_sends_the_same_dm(monkeypatch, pushes, dms):
    monkeypatch.setenv(SWITCH, "2026-11-01")
    consent("爸爸", "U_DAD")
    _red(monkeypatch, [RF("爸爸", "跌倒")])
    before = ts(2026, 10, 31, 21, 30)  # 開關還沒生效
    _outcome(decide(event=before - 601, now=before), now=before)
    assert dms == ["🚨 LINE 群組：爸爸提到跌倒（21:19）。群組提醒：訊息超過 10 分鐘未回"]
    assert pushes == []


# ── 每日檢查 ────────────────────────────────────────────────────────────────

MOM_BP_GROUP = (
    "11/1 08:12 提到媽媽的血壓 185/125（高壓超過 180、低壓超過 120）：之後還沒再量過的話，請媽媽休息 5 分鐘後"
    "再量一次；還是很高要盡快就醫；有胸痛胸悶、呼吸困難、嘴歪或說話不清、單側手腳無力就打 119"
)
MOM_BP_DM = (
    "11/1 08:12 群組提到媽媽的血壓 185/125（高壓超過 180、低壓超過 120）：請她休息 5 分鐘後再量一次；"
    "還是很高要盡快就醫；有胸痛胸悶、呼吸困難、嘴歪或說話不清、單側手腳無力就打 119"
)
MOM_SUGAR_DM = (
    "11/1 07:30 群組提到媽媽的血糖 62（低於 70）：請她現在再量一次；還是低於 70 或有發抖、冒冷汗，"
    "意識清楚、能吞嚥的話先吃 15 克糖（約 3 顆方糖或 120cc 果汁），15 分鐘後再量，還是很低就送醫；"
    "意識不清或不能吞嚥不要餵，直接打 119"
)


def _seed_daily():
    consent("爸爸", "U_DAD")
    consent("媽媽", "U_MOM")
    sleepless("爸爸")
    add("媽媽", "body", "血壓", "185/125", at=ts(2026, 11, 1, 8, 12), reporter="U_MOM")


def test_group_mode_daily_check_pushes_one_message_and_sends_no_dm(group_on, pushes, dms):
    _seed_daily()
    assert ph.run_daily_check(NOW) == 1
    assert dms == []
    assert texts(pushes) == [f"{HEAD}\n爸爸 7 天內有 4 天睡不好\n{MOM_BP_GROUP}"]
    assert pushes[0]["retry_key"] == str(uuid.uuid5(uuid.NAMESPACE_URL, f"parent_health:{G}:daily_check:2026-11-01"))
    assert_push_ok(texts(pushes)[0])
    assert ph.run_daily_check(NOW + 600) == 0 and len(pushes) == 1  # 一天一則
    assert db.last_alert(G, "爸爸", "sleep_bad") is not None


def test_lines_that_do_not_fit_are_not_claimed_and_go_out_tomorrow(monkeypatch, group_on, pushes):
    consent("爸爸", "U_DAD")
    consent("媽媽", "U_MOM")
    sleepless("爸爸")
    sleepless("媽媽", "U_MOM")
    dad = "爸爸 7 天內有 4 天睡不好"
    monkeypatch.setattr(ph, "_DM_MAX_CHARS", len(HEAD) + 1 + len(dad))  # 只放得下一行
    assert ph.run_daily_check(NOW) == 1
    assert texts(pushes) == [f"{HEAD}\n{dad}"]
    assert db.last_alert(G, "媽媽", "sleep_bad") is None  # 放不下的不搶
    assert ph.run_daily_check(ts(2026, 11, 2, 21, 30)) == 1
    assert texts(pushes)[1] == "🩺 爸媽健康每日檢查（11/2）\n媽媽 7 天內有 4 天睡不好"


@pytest.mark.parametrize("failure", ["muted", "validator", "http"])
def test_a_failed_group_push_sends_only_the_urgent_lines_to_discord(monkeypatch, group_on, pushes, dms, failure):
    _seed_daily()
    add("媽媽", "body", "血糖", "62", at=ts(2026, 11, 1, 7, 30), reporter="U_MOM")
    if failure == "muted":
        monkeypatch.setattr(db.settings, "bot_muted", True)
    elif failure == "validator":
        monkeypatch.setattr(
            output_validator, "validate_outbound_text",
            lambda text, **kw: output_validator.ValidationResult(ok=False, text="", reason="synthetic_block"),
        )
    else:
        def refuse(*_a, **_kw):
            raise line_push_client.LinePushError("LINE push HTTP 429", status_code=429)

        monkeypatch.setattr(line_push_client, "push_messages", refuse)
    assert ph.run_daily_check(NOW) == 1
    assert pushes == []
    assert dms == [f"{HEAD}\n{MOM_BP_DM}\n{MOM_SUGAR_DM}"]  # 原本對 Andrew 說的那幾行，沒有睡不好那行
    assert db.last_alert(G, "爸爸", "sleep_bad") is not None  # 失敗就算了：claim 不放回
    assert ph.run_daily_check(ts(2026, 11, 2, 21, 30)) == 0 and len(dms) == 1


def test_a_failed_group_push_without_urgent_lines_sends_nothing(monkeypatch, group_on, dms):
    consent("爸爸", "U_DAD")
    sleepless("爸爸")
    assert ph.run_daily_check(NOW) == 0  # conftest 預設靜音：推不出去
    assert dms == []
    assert db.last_alert(G, "爸爸", "sleep_bad") is not None


def test_other_groups_keep_the_dm_in_group_mode(group_on, pushes, dms):
    consent("爸爸", "U_DAD", group=G2)
    sleepless("爸爸", group=G2)
    assert ph.run_daily_check(NOW) == 1
    assert dms == [f"{HEAD}\n爸爸 7 天內有 4 天睡不好"] and pushes == []


def test_the_day_before_the_switch_the_family_group_still_gets_the_dm(group_on, pushes, dms):
    before = ts(2026, 10, 31, 21, 30)
    consent("爸爸", "U_DAD")
    sleepless("爸爸", until=before)
    assert ph.run_daily_check(before) == 1
    assert dms == ["🩺 爸媽健康每日檢查（10/31）\n爸爸 7 天內有 4 天睡不好"] and pushes == []


def test_urgent_group_lines_speak_to_the_family_and_keep_the_remeasure_condition():
    at = ts(2026, 11, 1, 8, 12)
    assert pa.urgent_group_text("媽媽", "bp_crisis", "185/125", at) == MOM_BP_GROUP
    sugar = pa.urgent_group_text("爸爸", "glucose_low", "62", ts(2026, 11, 1, 7, 30))
    assert sugar.startswith("11/1 07:30 提到爸爸的血糖 62（低於 70）：之後還沒再量過的話，請爸爸現在再量一次；")
    for kind, value in (("bp_crisis", "185/125"), ("bp_crisis", "200/100"), ("glucose_low", "62")):
        group = pa.urgent_group_text("爸爸", kind, value, at)
        dm = pa.urgent_dm_text("爸爸", "他", kind, value, at)
        assert group.split("之後還沒再量過的話，請爸爸", 1)[1] == dm.split("：請他", 1)[1]  # 當下怎麼做同一段
        assert_push_ok(group)
    with pytest.raises(ValueError):
        pa.urgent_group_text("爸爸", "other", "1", 0)


# ── 暫停／恢復、說明、分析卡 ────────────────────────────────────────────────


def test_group_mode_pause_and_resume_send_nothing_and_say_group_notices(group_on, pushes, dms):
    consent("爸爸", "U_DAD")
    consent("媽媽", "U_MOM")
    paused = cmd("暫停 媽媽", "U_KID")
    assert_reply_ok(paused.text)
    assert "卡片、週報段落、群組通知和 119 提醒也一起停" in paused.text and "私訊" not in paused.text
    resumed = cmd("恢復 媽媽", "U_KID")
    assert resumed.text.startswith("爸媽健康：已恢復記錄媽媽的健康留言")
    cmd("暫停", "U_DAD")
    mine = cmd("恢復", "U_DAD")
    assert_reply_ok(mine.text)
    assert ph._EXPLAIN_HEAD_GROUP in mine.text and ph._EXPLAIN_TAIL_GROUP in mine.text and "私訊" not in mine.text
    assert dms == [] and pushes == []


def test_group_mode_help_never_promises_a_dm(monkeypatch, group_on):
    text = cmd("說明", "U_KID").text
    assert text == ph._help_text(True) != ph._HELP_TEXT
    assert_reply_ok(text)
    assert "私訊" not in text and "週日週報有一段（含每週的分析整理）" in text and "群組通知" in text
    monkeypatch.setenv("LINE_BOT_EXERCISE_NUDGE", "1")
    consent("爸爸", "U_DAD")
    assert "群組通知、運動提醒和 119 提醒也一起停" in cmd("暫停", "U_DAD").text


def test_group_mode_analysis_card_says_the_full_summary_is_in_the_weekly_report(group_on):
    consent("媽媽", "U_MOM")
    sleepless("媽媽", "U_MOM")
    for offset in (0, 1, 2):
        add("媽媽", "body", "膝蓋痛", at=NOW - offset * DAY - 7200, reporter="U_MOM")
    for offset in (1, 2):
        add("媽媽", "med", "忘了吃藥", at=NOW - offset * DAY - 7200, reporter="U_MOM")
        add("媽媽", "diet", "沒吃早餐", at=NOW - offset * DAY - 7200, reporter="U_MOM")
    reply = cmd("分析", "U_KID")
    for card in (reply.card, reply.page_card):
        blob = json.dumps(card, ensure_ascii=False)
        assert "私訊" not in blob and "Andrew" not in blob and "管理者" not in blob
        assert card["footer"]["contents"][-1]["text"] == "每週日的完整整理會放在週報"
        assert "另外還有 1 項，完整整理每週日放在週報" in blob


# ── 週分析併進週報 ──────────────────────────────────────────────────────────


def _walks(group=G):
    for offset in (0, 2, 4):
        add("爸爸", "exercise", "散步", at=NOW - offset * DAY - 7200, group=group)


def test_weekly_analysis_text_only_in_group_mode_and_never_raises(monkeypatch, group_on):
    consent("爸爸", "U_DAD")
    assert ph.weekly_analysis_text(G, NOW) is None  # 這週沒有紀錄
    _walks()
    text = ph.weekly_analysis_text(G, NOW)
    assert text.startswith("🩺 爸媽健康週分析（10/26–11/1）\n爸爸（7 天有 3 天有紀錄）") and text.endswith("不是診斷")
    assert ph.weekly_analysis_text(G2, NOW) is None  # 不是家族主群
    assert ph.weekly_analysis_text(G, ts(2026, 10, 25, 20)) is None  # 開關還沒生效
    monkeypatch.delenv(SWITCH)
    assert ph.weekly_analysis_text(G, NOW) is None
    monkeypatch.setenv(SWITCH, "on")

    def boom(_ctx):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(ph, "_weekly_analysis_text", boom)
    assert ph.weekly_analysis_text(G, NOW) is None
    assert set(db.consent_states(G)) == {"爸爸"}  # 只讀，不寫預設同意列


def test_the_sunday_dm_job_skips_the_family_group_in_group_mode_only(group_on, dms):
    consent("爸爸", "U_DAD")
    consent("爸爸", "U_DAD", group=G2)
    _walks()
    _walks(G2)
    assert ph._run_weekly_analysis(NOW) == 1 and len(dms) == 1  # 只有非主群私訊
    assert db.last_alert(G, "", "weekly_analysis") is None
    assert db.last_alert(G2, "", "weekly_analysis")["key"] == "2026-11-01"


def test_dm_mode_keeps_the_sunday_dm_and_the_plain_weekly_report(monkeypatch, dms):
    import weekly_summary

    monkeypatch.setenv(SWITCH, "")  # 空字串＝私訊；import weekly_summary 時的 load_dotenv 也不會把它打開
    monkeypatch.setattr(ph, "_now", lambda now=None: NOW if now is None else float(now))
    consent("爸爸", "U_DAD")
    _walks()
    section = ph.weekly_section(G, NOW)
    assert section and weekly_summary._with_parent_health(G, FAMILY_TEXT) == f"{FAMILY_TEXT}\n\n{section}"
    assert ph._run_weekly_analysis(NOW) == 1 and dms[0].startswith("🩺 爸媽健康週分析（10/26–11/1）")


def _real_weekly(monkeypatch):
    import weekly_summary

    monkeypatch.setenv(SWITCH, "2026-11-01")
    monkeypatch.setattr(ph, "_now", lambda now=None: NOW if now is None else float(now))  # 週報不帶 now
    return weekly_summary


def test_group_mode_weekly_report_carries_the_section_and_the_analysis(monkeypatch):
    weekly_summary = _real_weekly(monkeypatch)
    consent("爸爸", "U_DAD")
    _walks()
    section, analysis = ph.weekly_section(G, NOW), ph.weekly_analysis_text(G, NOW)
    assert section and analysis
    combined = weekly_summary._with_parent_health(G, FAMILY_TEXT)
    assert combined == f"{FAMILY_TEXT}\n\n{section}\n\n{analysis}"
    assert_push_ok(combined)


def test_a_week_with_only_readings_still_gets_the_analysis(monkeypatch):
    weekly_summary = _real_weekly(monkeypatch)
    consent("爸爸", "U_DAD")
    add("爸爸", "body", "血壓", "135/85", at=NOW - DAY)
    assert ph.weekly_section(G, NOW) == ""
    analysis = ph.weekly_analysis_text(G, NOW)
    assert "讀數：血壓 135/85" in analysis
    assert weekly_summary._with_parent_health(G, FAMILY_TEXT) == f"{FAMILY_TEXT}\n\n{analysis}"


@pytest.fixture
def fake_health(monkeypatch):
    module = SimpleNamespace(
        weekly_section=lambda group_id, now=None: "🩺 爸媽這週\n爸爸：吃藥 5/7 天",
        weekly_analysis_text=lambda group_id, now=None: "🩺 爸媽健康週分析（10/26–11/1）\n爸爸（7 天有 5 天有紀錄）",
    )
    monkeypatch.setitem(sys.modules, "parent_health", module)
    return module


def test_too_long_drops_the_analysis_before_the_section(fake_health):
    import weekly_summary

    section = fake_health.weekly_section(G)
    fake_health.weekly_analysis_text = lambda group_id, now=None: "🩺 爸媽健康週分析\n" + "爸" * 4880  # 自己加上家族熱話就超過 4900
    assert weekly_summary._with_parent_health(G, FAMILY_TEXT) == f"{FAMILY_TEXT}\n\n{section}"
    fake_health.weekly_section = lambda group_id, now=None: ""
    assert weekly_summary._with_parent_health(G, FAMILY_TEXT) == FAMILY_TEXT  # 再丟整段健康


def test_an_analysis_the_validator_blocks_drops_only_the_analysis(fake_health):
    import weekly_summary

    section = fake_health.weekly_section(G)
    fake_health.weekly_analysis_text = lambda group_id, now=None: "最新數據 3 筆"  # 推播驗證會擋
    assert weekly_summary._with_parent_health(G, FAMILY_TEXT) == f"{FAMILY_TEXT}\n\n{section}"

    def boom(*_a, **_kw):
        raise RuntimeError("synthetic")

    fake_health.weekly_analysis_text = boom
    assert weekly_summary._with_parent_health(G, FAMILY_TEXT) == f"{FAMILY_TEXT}\n\n{section}"
    fake_health.weekly_section = boom  # 段落壞了，週分析照接
    fake_health.weekly_analysis_text = lambda group_id, now=None: "🩺 爸媽健康週分析（10/26–11/1）"
    assert weekly_summary._with_parent_health(G, FAMILY_TEXT) == f"{FAMILY_TEXT}\n\n🩺 爸媽健康週分析（10/26–11/1）"


def _main_with(weekly_summary, monkeypatch, *, family, bot_rows=(("m1", "__bot__", "一般回覆", 0),)):
    monkeypatch.setattr(weekly_summary, "GROUP_ID", G)
    monkeypatch.setattr(weekly_summary, "line_access_token", lambda: "token")
    monkeypatch.setattr(weekly_summary.memory, "get_messages_since", lambda *a, **kw: list(bot_rows))
    monkeypatch.setattr(weekly_summary.gemini_client, "chat", lambda *a, **kw: "")
    monkeypatch.setattr(weekly_summary, "_start_family_workers", lambda *a, **kw: None)
    monkeypatch.setattr(weekly_summary, "_collect_family_inputs", lambda group_id: ({}, {}, {}))
    monkeypatch.setattr(weekly_summary, "_render_family_text", lambda *a, **kw: family)
    monkeypatch.setattr(weekly_summary, "_start_worker", lambda name, fn: None)
    monkeypatch.setattr(weekly_summary, "_wait_worker", lambda name, deadline: (False, None))
    return weekly_summary.main()


def test_the_weekly_report_archives_only_the_family_text(monkeypatch):
    weekly_summary = _real_weekly(monkeypatch)
    consent("爸爸", "U_DAD")
    _walks()
    pushed: list[tuple[str, str | None]] = []
    monkeypatch.setattr(weekly_summary, "_push", lambda text, archive_text=None: pushed.append((text, archive_text)) or True)
    assert _main_with(weekly_summary, monkeypatch, family=FAMILY_TEXT) == 0
    section, analysis = ph.weekly_section(G, NOW), ph.weekly_analysis_text(G, NOW)
    assert pushed == [(f"{FAMILY_TEXT}\n\n{section}\n\n{analysis}", FAMILY_TEXT)]


@pytest.mark.parametrize("skip", ["no_bot_replies", "empty_family", "blocked_family"])
def test_a_skipped_weekly_report_sends_no_analysis_either(monkeypatch, pushes, dms, skip):
    weekly_summary = _real_weekly(monkeypatch)
    consent("爸爸", "U_DAD")
    _walks()
    asked = []
    real = ph.weekly_analysis_text
    monkeypatch.setattr(ph, "weekly_analysis_text", lambda group_id, now=None: asked.append(group_id) or real(group_id, now))
    family = {"no_bot_replies": FAMILY_TEXT, "empty_family": "", "blocked_family": "最新數據 3 筆"}[skip]
    rows = () if skip == "no_bot_replies" else (("m1", "__bot__", "一般回覆", 0),)
    _main_with(weekly_summary, monkeypatch, family=family, bot_rows=rows)
    assert pushes == [] and dms == [] and asked == []  # 不單推


# ── 去重紀錄定期清除 ────────────────────────────────────────────────────────

SEVEN_DAY_KINDS = (
    "bp_crisis:2026-10-20:185/125", "glucose_low:2026-10-20:62", "symptom:頭暈", "dm:跌倒", "notice:跌倒",
    "med_gap", "med_forgot", "sleep_bad", "exercise_nudge",
)
PREFIXES = {
    "bp_crisis", "glucose_low", "symptom", "dm", "notice", "med_gap", "med_forgot", "sleep_bad", "exercise_nudge",
    "reply", "daily_check", "weekly_analysis",
}


def _alert(kind, age_s, *, key, group=G, role="爸爸"):
    assert db.claim_alert(group, role, kind, key, now=NOW - age_s)


def _rows() -> set[tuple[str, str, str]]:
    with sqlite3.connect(db._DB_PATH) as c:
        return set(c.execute("SELECT group_id, kind, key FROM parent_health_alerts"))


@pytest.mark.parametrize("kind", SEVEN_DAY_KINDS)
def test_purge_clears_seven_day_kinds_only_after_seven_days(kind):
    _alert(kind, 7 * DAY, key="kept")  # 剛好 7 天：留
    _alert(kind, 7 * DAY + 1, key="gone")
    counts = db.purge_alerts(NOW)
    assert _rows() == {(G, kind, "kept")}
    assert counts[kind.split(":", 1)[0]] == 1 and sum(counts.values()) == 1


def test_purge_keeps_119_reply_rows_for_eight_days():
    _alert("reply", 8 * DAY, key="kept")
    _alert("reply", 7 * DAY + 1, key="week")
    _alert("reply", 8 * DAY + 1, key="gone")
    assert db.purge_alerts(NOW)["reply"] == 1
    assert _rows() == {(G, "reply", "kept"), (G, "reply", "week")}


@pytest.mark.parametrize("kind", ["daily_check", "weekly_analysis"])
def test_purge_keeps_check_rows_for_eight_days_and_the_latest_per_group(kind):
    _alert(kind, 30 * DAY, key="g1-old", role="")
    _alert(kind, 9 * DAY, key="g1-latest", role="")
    _alert(kind, 40 * DAY, key="g2-only", role="", group=G2)
    _alert(kind, 8 * DAY + 1, key="g3-old", role="", group="G_TEST_3")
    _alert(kind, 8 * DAY, key="g3-edge", role="", group="G_TEST_3")
    _alert(kind, DAY, key="g3-latest", role="", group="G_TEST_3")
    assert db.purge_alerts(NOW)[kind] == 2
    assert {key for _group, _kind, key in _rows()} == {"g1-latest", "g2-only", "g3-edge", "g3-latest"}


def test_purge_never_treats_underscore_as_a_wildcard_or_touches_unknown_kinds():
    unknown = ("bpXcrisis:2026-10-01:185/125", "medXgap", "bp_crisis", "glucose_low", "dmx:跌倒", "notice",
               "replyX", "daily_checks", "future_kind")
    for kind in unknown:
        _alert(kind, 30 * DAY, key="k")
    assert sum(db.purge_alerts(NOW).values()) == 0
    assert {kind for _group, kind, _key in _rows()} == set(unknown)


def test_purge_dry_run_counts_without_deleting_and_reports_only_prefixes(tmp_path):
    for kind in SEVEN_DAY_KINDS:
        _alert(kind, 10 * DAY, key="k")
    _alert("reply", 10 * DAY, key="k")
    before = _rows()
    dry = db.purge_alerts(NOW, dry_run=True)
    assert _rows() == before
    real = db.purge_alerts(NOW)
    assert dry == real and set(real) == PREFIXES and sum(real.values()) == 10 and _rows() == set()
    assert all(":" not in prefix for prefix in real)
    assert "185" not in json.dumps(real, ensure_ascii=False) and "頭暈" not in json.dumps(real, ensure_ascii=False)
    fresh = tmp_path / "fresh.db"
    assert db.purge_alerts(NOW, dry_run=True, db_path=fresh) == dict.fromkeys(PREFIXES, 0)
    assert str(fresh) not in db._INIT_PATHS  # 乾跑不建表


def test_purge_turns_on_secure_delete_on_the_deleting_connection(monkeypatch):
    _alert("dm:跌倒", 10 * DAY, key="k")
    statements: list[str] = []
    real_conn = db._conn

    class Spy:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            self.conn.__enter__()
            return self

        def __exit__(self, *exc):
            return self.conn.__exit__(*exc)

        def execute(self, sql, params=()):
            statements.append(sql)
            return self.conn.execute(sql, params)

    monkeypatch.setattr(db, "_conn", lambda db_path=None: Spy(real_conn(db_path)))
    assert db.purge_alerts(NOW)["dm"] == 1
    first_delete = next(i for i, sql in enumerate(statements) if sql.startswith("DELETE"))
    assert "PRAGMA secure_delete=ON" in statements[:first_delete]


def _functions_calling(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and any(
            isinstance(call, ast.Call) and getattr(call.func, "attr", "") == name for call in ast.walk(node)
        ):
            yield node


def test_every_claimed_alert_kind_maps_to_a_purge_rule():
    """程式碼裡所有 claim_alert 的種類都列在這裡、而且都有清除規則；新增種類這個測試會先紅。"""
    tree = ast.parse(Path(ph.__file__).read_text(encoding="utf-8"))
    claimed = sorted(
        ast.unparse(node.args[2]) for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "claim_alert"
    )
    assert claimed == sorted(
        ["'reply'", "kind", "kind", "_NUDGE_KIND", "'daily_check'", "anomaly.kind", "anomaly.kind", "'weekly_analysis'"]
    )
    kind_values = sorted(
        ast.unparse(node.value) for func in _functions_calling(tree, "claim_alert") for node in ast.walk(func)
        if isinstance(node, ast.Assign) and [ast.unparse(target) for target in node.targets] == ["kind"]
    )
    assert kind_values == ["f'dm:{label}'", "f'notice:{label}'"]
    anomaly_kinds = sorted(
        ast.unparse(node.args[1]) for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_Anomaly"
    )
    assert anomaly_kinds == sorted(["_urgent_kind(kind, day, value)", "'med_gap'", "'med_forgot'", "f'symptom:{item}'", "'sleep_bad'"])
    assert ph._URGENT_KINDS == ("bp_crisis", "glucose_low") and ph._NUDGE_KIND == "exercise_nudge"
    samples = {
        "reply": 8, "dm:跌倒": 7, "notice:跌倒": 7, "exercise_nudge": 7, "daily_check": 8, "weekly_analysis": 8,
        ph._urgent_kind("bp_crisis", datetime(2026, 10, 1).date(), "185/125"): 7,
        ph._urgent_kind("glucose_low", datetime(2026, 10, 1).date(), "62"): 7,
        "med_gap": 7, "med_forgot": 7, "symptom:頭暈": 7, "sleep_bad": 7,
    }
    for kind, days in samples.items():
        _alert(kind, days * DAY + 1, key="old")
        _alert(kind, 0, key="new")  # 每日檢查、週分析的最新一列
    counts = db.purge_alerts(NOW)
    assert sum(counts.values()) == len(samples)
    assert _rows() == {(G, kind, "new") for kind in samples}


def test_the_daily_loop_purges_once_a_day_with_the_real_clock_and_logs_only_prefixes(monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(ph, "_purged_day", None)
    _alert("bp_crisis:2026-10-01:185/125", 10 * DAY, key="k")
    ph._purge_old_alerts(NOW)
    assert _rows() == set()
    assert "parent health alerts purged bp_crisis=1" in caplog.text and "185" not in caplog.text
    calls: list[float] = []
    monkeypatch.setattr(db, "purge_alerts", lambda now, **kw: calls.append(now) or {"dm": 0})
    ph._purge_old_alerts(NOW + 3600)  # 同一天（台北）不再清
    assert calls == []
    ph._purge_old_alerts(ts(2026, 11, 2, 0, 5))
    assert calls == [ts(2026, 11, 2, 0, 5)]
    stop = threading.Event()
    loops: list[int] = []

    def daily(now=None):
        loops.append(1)
        if len(loops) >= 3:  # 清除 job 沒跑的話，第三圈也會停下來，不會一直等
            stop.set()
        return 0

    monkeypatch.setattr(ph, "_STARTUP_DELAY_S", 0.0)
    monkeypatch.setattr(ph, "_CHECK_INTERVAL_S", 0.01)
    monkeypatch.setattr(ph, "run_daily_check", daily)
    monkeypatch.setattr(ph, "_run_weekly_analysis", lambda now=None: 0)
    monkeypatch.setattr(ph, "_prune_health_pages", lambda: None)
    monkeypatch.setattr(ph, "_purged_day", None)
    monkeypatch.setattr(db, "purge_alerts", lambda now, **kw: calls.append(now) or stop.set() or {})
    before = time.time()
    ph._daily_loop(stop)
    assert len(calls) == 2 and before <= calls[-1] <= time.time()  # 迴圈不帶 now：真實時鐘


# ── 緊急停用版 ──────────────────────────────────────────────────────────────


def test_the_disabled_stub_has_no_weekly_analysis():
    assert ph_off.weekly_analysis_text(G) is None and ph_off.weekly_analysis_text(G, NOW) is None
    assert "weekly_analysis_text" in ph_off.__all__ and ph_off.__all__ == ph.__all__
