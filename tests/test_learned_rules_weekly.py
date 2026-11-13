"""jobs/learned_rules_weekly.py（2026-11-12 自動學習糾正週報：每週日 20:00 推 Discord）。

全部是合成資料；DB 是 conftest 給每個測試的暫存檔；outbox、狀態目錄、私人副本的上層目錄都是 conftest 給這個
測試的暫存目錄（這個檔的 weekly_dirs；conftest 的預設是不存在的隔離路徑）。模型呼叫是假的；job 的 sleep 換成
只記秒數、.env 不載入。
"""
from __future__ import annotations

import fcntl
import inspect
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pytest

import claude_client
import complaint_learning as cl
import config
import memory
from jobs import learned_rules_weekly as lw

TPE = lw.TPE
G = "G_SENTINEL_WEEKLY"
FAMILY_TEXT = "哨兵家人原話不能出現"
DECISION = "要正式開（學到的規則開始影響回覆），在 Claude Code 說「正式開自動學習糾正」，開之前會先跑一次安全檢查給你看。"
APPROVED_ZERO_WEEK = f"""🧠 咪寶自動學習糾正週報（11/08 20:00–11/15 20:00）
模式：試運轉（只判斷、不寫入，咪寶的回覆不受影響）

這週
・抓到疑似不滿的留言：1 則
　・沒送去判斷：1 則（找不到它在講哪一則咪寶回覆 1）
　・判斷器看過：0 則
・會學到的規則：0 條

上線以來（10/29 起）：抓到 2 則、判斷器看過 0 則、會學到 0 條
舊的糾正規則：50 條都停用中，這週新收 0 條

{DECISION}"""
EMPTY_WEEK = f"""🧠 咪寶自動學習糾正週報（11/08 20:00–11/15 20:00）
模式：試運轉（只判斷、不寫入，咪寶的回覆不受影響）

這週
・抓到疑似不滿的留言：0 則
　・判斷器看過：0 則
・會學到的規則：0 條

上線以來（10/29 起）：抓到 0 則、判斷器看過 0 則、會學到 0 條
舊的糾正規則：沒有，這週新收 0 條

{DECISION}"""
RULE_OK = "當沒看完影片完整內容時，不要說影片沒提到"
RULE_OK_2 = "當家人詢問影片中的課程時，不要把沒被問到的商品扯進來"
WEEKLY = "2026-11-15-complaint-learning-weekly.md"
FAILED = "2026-11-15-complaint-learning-failed.md"


def ts(y, mo, d, h=0, mi=0, s=0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=TPE).timestamp())


SUNDAY = date(2026, 11, 15)
SINCE, UNTIL = ts(2026, 11, 8, 20), ts(2026, 11, 15, 20)
IN_WEEK = ts(2026, 11, 10, 12)
AFTER_GRACE = UNTIL + lw.GRACE_SECONDS + 60


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    seen: list[float] = []
    monkeypatch.setattr(lw, "_sleep", seen.append)
    monkeypatch.setattr(lw, "_load_dotenv", lambda: None)
    return seen


@pytest.fixture
def weekly_dirs(tmp_path, monkeypatch):
    for sub in ("maintenance", "state", "tmp"):
        (tmp_path / sub).mkdir()
    monkeypatch.setenv(lw.MAINTENANCE_ENV, str(tmp_path / "maintenance"))
    monkeypatch.setenv(lw.STATE_ENV, str(tmp_path / "state"))
    monkeypatch.setenv(lw.COPY_PARENT_ENV, str(tmp_path / "tmp"))
    return tmp_path


def judgement(complaint_id: str, stage: str, result: str, created_at: int, *, judged: int = 0,
              provider: str = "", group: str = G) -> None:
    with memory._lock, memory._conn() as conn:
        conn.execute(
            "INSERT INTO complaint_judgements(group_id, complaint_id, complaint_ids, target_id, kind, mode, actor_key, "
            "stage, result, judged, provider, created_at) VALUES (?, ?, ?, ?, 'complaint', 'shadow', ?, ?, ?, ?, ?, ?)",
            (group, complaint_id, json.dumps([complaint_id]), f"T_{complaint_id}", "A_SENTINEL_ACTOR", stage, result,
             judged, provider, created_at),
        )


def shadow_rule(complaint_id: str, rule: str, created_at: int, category: str = "only_verified") -> None:
    with memory._lock, memory._conn() as conn:
        conn.execute(
            "INSERT INTO complaint_shadow_rules(group_id, complaint_id, category, core_reason, rule, created_at) "
            "VALUES (?, ?, ?, '合成原因', ?, ?)",
            (G, complaint_id, category, rule, created_at),
        )


def legacy_rules(count: int, *, status: str = "retired", first_seen: int = ts(2026, 6, 1)) -> None:
    with memory._lock, memory._conn() as conn:
        for i in range(count):
            conn.execute(
                "INSERT INTO correction_rules(group_id, canonical_key, canonical_rule, status, occurrence_count, "
                "first_seen_at, last_seen_at, origin) VALUES (?, ?, ?, ?, 1, ?, ?, '')",
                (G, f"legacy-{status}-{first_seen}-{i}", f"舊規則{i}", status, first_seen, first_seen),
            )


def learn(rule: str, *, oid: str, now: int, category: str = "only_verified") -> dict:
    return memory.record_learned_rule(
        G, oid, rule=rule, core_reason="合成原因", category=category, source_message_ids=[oid.split(":")[-1]],
        day_start=cl.day_start(now), now=now, observed_at=now, max_active=8, max_new_per_day=1,
        ttl_seconds=cl.LEARNED_TTL_SECONDS,
    )


def report(*, mode=cl.MODE_SHADOW, env_mode=None, names=(), since=SINCE, until=UNTIL, now=None) -> str:
    conn = sqlite3.connect(str(memory._DB_PATH))
    try:
        return lw.build_report(conn, since=since, until=until, mode=mode,
                               env_mode=mode if env_mode is None else env_mode, names=names, now=now)
    finally:
        conn.close()


def week(*, since=SINCE, until=UNTIL) -> lw.Week:
    conn = sqlite3.connect(str(memory._DB_PATH))
    try:
        return lw.gather(conn, since=since, until=until, now=until)
    finally:
        conn.close()


@pytest.fixture
def db_env(weekly_dirs, monkeypatch):
    monkeypatch.setenv(lw.DB_ENV, str(memory._DB_PATH))
    monkeypatch.setenv(cl.ENV_FLAG, "shadow")
    return Path(os.environ[lw.MAINTENANCE_ENV])


def outbox_names(maintenance: Path) -> list[str]:
    folder = maintenance / "outbox"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


# ── 到期與期間 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        ((2026, 11, 13, 10, 0), None),  # 部署那週：最近的週日 11/08 早於第一份
        ((2026, 11, 15, 19, 59), None),  # 第一個週日 20:00 前
        ((2026, 11, 15, 20, 0), date(2026, 11, 15)),
        ((2026, 11, 16, 9, 0), date(2026, 11, 15)),  # 週一補送上週日那份
        ((2026, 11, 21, 23, 59), date(2026, 11, 15)),
        ((2026, 11, 22, 19, 59), None),  # 週日 20:00 前不補上週（當晚就有新的）
        ((2026, 11, 22, 20, 0), date(2026, 11, 22)),
    ],
)
def test_report_sunday_is_the_latest_sunday_past_eight_pm(local, expected):
    assert lw.report_sunday(ts(*local)) == expected


def test_all_four_tables_use_the_same_half_open_week():
    assert lw.week_bounds(SUNDAY) == (SINCE, UNTIL)
    judgement("ON-START", "shadow", "shadow_pass", SINCE, judged=1, provider="claude")
    shadow_rule("ON-START", RULE_OK, SINCE)
    judgement("ON-END", "shadow", "shadow_pass", UNTIL, judged=1, provider="claude")  # 下一週
    shadow_rule("ON-END", RULE_OK_2, UNTIL)
    learn(RULE_OK, oid="judge:E1", now=SINCE)
    learn(RULE_OK_2, oid="judge:E2", now=UNTIL)  # 下一週
    legacy_rules(1, status="active", first_seen=SINCE)
    legacy_rules(1, status="active", first_seen=UNTIL)
    w = week()
    assert w.total == 1 and [r[1] for r in w.shadow] == [RULE_OK] and [r[1] for r in w.learned] == [RULE_OK]
    assert w.legacy == (2, 0, 1)


def test_a_monday_catch_up_counts_only_up_to_sunday_eight_pm():
    judgement("SUN", "target", "no_target", UNTIL - 1)
    judgement("MON", "target", "no_target", ts(2026, 11, 16, 9))
    text = report(now=ts(2026, 11, 16, 10))
    assert "・抓到疑似不滿的留言：1 則" in text and "上線以來（10/29 起）：抓到 1 則" in text


# ── 內容 ─────────────────────────────────────────────────────────────────────────────


def test_a_quiet_week_reads_exactly_like_the_sample_andrew_approved():
    judgement("C-NOV01", "target", "quote_not_bot", ts(2026, 11, 1, 9))
    judgement("C-NOV09", "target", "no_target", ts(2026, 11, 9, 21))
    legacy_rules(50)
    assert report() == APPROVED_ZERO_WEEK


def test_an_empty_week():
    assert report() == EMPTY_WEEK


ALL_ROWS = [
    ("target", "no_target", 0), ("target", "quote_not_bot", 0), ("target", "quote_missing", 0),
    ("target", "operational", 0), ("names", "names_unavailable", 0),
    ("dedupe", "already_judged", 0), ("cap", "cap_actor", 0), ("cap", "cap_group", 0),
    ("judge", "judge_unavailable:claude_quota+gemini_withheld", 0),
    ("judge", "judge_unavailable:claude_error+gemini_error", 1), ("judge", "judge_parse_error", 1),
    ("verdict", "not_about_bot", 1), ("verdict", "not_complaint", 1), ("verdict", "reply_kind", 1),
    ("verdict", "category", 1), ("verdict", "low_confidence", 1),
    ("check", "check:unprompted", 1), ("check", "check:name", 1), ("check", "check:names_unavailable", 1),
    ("verify", "verifier_rejected", 1), ("verify", "verify_unavailable", 1), ("verify", "verify_parse_error", 1),
    ("shadow", "shadow_pass", 1), ("shadow", "unsent", 1), ("store", "unsent", 1), ("store", "disabled", 1),
    ("store", "created", 1), ("store", "revived", 1), ("store", "merged", 1), ("store", "refused_active", 1),
    ("store", "refused_daily", 1), ("store", "tombstoned", 1), ("store", "ambiguous", 1),
    ("store", "duplicate", 1), ("store", "store_error", 1), ("store", "conflict", 1),
    ("store", "brand_new_code", 1),
]


def test_every_row_lands_in_exactly_one_bucket_and_only_unknown_codes_are_other():
    for i, (stage, result, judged) in enumerate(ALL_ROWS):
        judgement(f"B{i}", stage, result, IN_WEEK + i, judged=judged, provider="claude" if judged else "")
    w = week()
    assert w.total == len(ALL_ROWS)
    assert sum(sum(c.values()) for c in w.groups.values()) == w.total
    assert sum(sum(c.values()) for c in w.seen.values()) == sum(w.groups["seen"].values())
    assert dict(w.seen["other"]) == {"brand_new_code": 1}
    labels = {**lw.NOT_SENT_LABELS, **lw.LIMIT_LABELS, **lw.NO_ANSWER_LABELS}
    for top in ("not_sent", "limited", "no_answer"):
        assert set(w.groups[top]) <= set(labels)
    text = report(names=cl.load_names())
    assert "　・判斷器沒給結果：3 則（模型回錯或逾時 1、回覆看不懂 1、沒叫到判斷器（Claude 暫停：額度或逾時保護；Gemini 不送或不能用） 1）" in text
    assert "　　・通過：4 則（學到了 1、併到已有的規則 1、學到了（以前退休的又出現） 1、試運轉：會學到 1）" in text
    assert "　　・其他：1 則（沒見過的代碼（例如 brand_new_code） 1）" in text
    assert "　　・第二次驗證不同意：1 則（驗證器說不行 1）" in text
    assert "　　・驗證器沒給結果：2 則（驗證的回覆看不懂 1、驗證時模型不能用 1）" in text


def test_a_judge_that_was_never_called_is_its_own_reason():
    judgement("N1", "judge", "judge_unavailable:claude_quota+gemini_withheld", IN_WEEK, judged=0)
    text = report()
    assert "　・判斷器沒給結果：1 則（沒叫到判斷器（Claude 暫停：額度或逾時保護；Gemini 不送或不能用） 1）" in text
    assert "　・判斷器看過：0 則" in text


def test_an_unsent_shadow_pass_is_counted_as_taken_back():
    judgement("U1", "shadow", "shadow_pass", IN_WEEK, judged=1, provider="claude")
    shadow_rule("U1", RULE_OK, IN_WEEK)
    assert "・會學到的規則：1 條" in report(names=cl.load_names())
    with memory._lock, memory._conn() as conn:  # 同 memory.unlink_unsent_complaint
        conn.execute("DELETE FROM complaint_shadow_rules WHERE group_id=? AND complaint_id='U1'", (G,))
        conn.execute("UPDATE complaint_judgements SET result='unsent' WHERE complaint_id='U1'")
    text = report(names=cl.load_names())
    assert "　　・通過但沒存：1 則（家人收回了訊息 1）" in text and "・會學到的規則：0 條" in text
    assert RULE_OK not in text


def test_rules_are_rechecked_with_the_name_list_before_they_are_listed():
    judgement("R1", "shadow", "shadow_pass", IN_WEEK, judged=1, provider="claude")
    shadow_rule("R1", RULE_OK, IN_WEEK)
    judgement("R2", "shadow", "shadow_pass", IN_WEEK + 1, judged=1, provider="claude")
    shadow_rule("R2", "當測試成員甲問影片時，不要說影片沒提到", IN_WEEK + 1)  # conftest 的別名
    text = report(names=cl.load_names())
    assert "測試成員甲" not in text
    assert f"　1. {RULE_OK}（只說查證過的）" in text
    assert "　2. （這條沒通過送出前的檢查，不列文字）（只說查證過的）" in text


def test_an_unreadable_name_list_withholds_every_rule_without_calling_the_checker(monkeypatch):
    judgement("R1", "shadow", "shadow_pass", IN_WEEK, judged=1, provider="claude")
    shadow_rule("R1", RULE_OK, IN_WEEK)

    def boom(*_a, **_k):
        raise AssertionError("rule_problem must not run without the name list")

    monkeypatch.setattr(cl, "rule_problem", boom)
    text = report(names=None)
    assert RULE_OK not in text and "　（名單讀不到，這次不列規則文字）" in text


def test_more_than_five_rules_and_long_weeks_still_fit_one_discord_message():
    for i in range(40):
        judgement(f"L{i}", "shadow", "shadow_pass", IN_WEEK + i, judged=1, provider="claude")
        shadow_rule(f"L{i}", RULE_OK_2, IN_WEEK + i, category="stay_on_topic")
        judgement(f"X{i}", "target", f"odd_code_{i:02d}_" + "z" * 30, IN_WEEK + 100 + i)
    text = report(names=cl.load_names())
    assert len(text) <= lw.MAX_CHARS and not text.endswith("…")
    assert "・會學到的規則：40 條" in text and "　…還有 35 條" in text
    assert "　・沒送去判斷：40 則（沒見過的代碼（例如 odd_code_00_" in text


def test_a_code_that_is_not_a_plain_code_is_never_echoed():
    judgement("W1", "target", "看起來 像文字", IN_WEEK)
    text = report()
    assert "看起來" not in text and "　・沒送去判斷：1 則（沒見過的代碼 1）" in text


def test_five_rules_are_listed_and_the_rest_are_counted():
    for i in range(7):
        judgement(f"F{i}", "shadow", "shadow_pass", IN_WEEK + i, judged=1, provider="claude")
        shadow_rule(f"F{i}", RULE_OK, IN_WEEK + i)
    text = report(names=cl.load_names())
    assert text.count(RULE_OK) == 5 and "　…還有 2 條" in text


def test_on_mode_lists_learned_rules_with_ids_and_the_rule_events():
    stored = learn(RULE_OK, oid="judge:LR1", now=IN_WEEK)
    assert stored["status"] == "created"
    text = report(mode=cl.MODE_ON, names=cl.load_names())
    assert f"・新學到的規則：1 條\n　1. #{stored['rule_id']} {RULE_OK}（只說查證過的）" in text
    assert "・現在放進提示詞的學到的規則：1/8 條" in text
    assert "上線以來（10/29 起）：抓到 0 則、判斷器看過 0 則、學到 1 條" in text
    assert DECISION not in text and "模式：正式" in text


@pytest.mark.parametrize("mode", [cl.MODE_ON, cl.MODE_SHADOW])
def test_a_week_that_switched_modes_lists_both_kinds_of_rules(mode):
    judgement("S1", "shadow", "shadow_pass", IN_WEEK, judged=1, provider="claude")
    shadow_rule("S1", RULE_OK_2, IN_WEEK, category="stay_on_topic")
    learn(RULE_OK, oid="judge:LR2", now=IN_WEEK + 3600)
    text = report(mode=mode, env_mode=cl.MODE_ON, names=cl.load_names())
    assert "・新學到的規則：1 條" in text and RULE_OK in text
    assert "・試運轉時會學到的規則：1 條" in text and RULE_OK_2 in text
    assert "、學到 1 條、試運轉時會學到 1 條" in text
    if mode == cl.MODE_ON:
        assert "・現在放進提示詞的學到的規則：1/8 條\n" in text
    else:  # 降級／暫停時學到的規則不進提示詞（P1 I-1）
        assert "・現在放進提示詞的學到的規則：0/8 條（試運轉，學到的規則不進提示詞；恢復正式後啟用中的 1 條會回來）" in text


def test_a_paused_week_says_no_learned_rule_is_in_the_prompt():
    learn(RULE_OK, oid="judge:PAUSE", now=IN_WEEK)
    text = report(mode=cl.MODE_OFF, env_mode=cl.MODE_ON, names=cl.load_names())
    assert "模式：關閉（不判斷、不學）；設定是「正式」，被狀態檔暫停或降級了" in text
    assert "・現在放進提示詞的學到的規則：0/8 條（關閉，學到的規則不進提示詞；恢復正式後啟用中的 1 條會回來）" in text


def test_rules_past_their_ttl_are_not_counted_as_in_the_prompt():
    learn(RULE_OK, oid="judge:OLD", now=IN_WEEK)
    later = IN_WEEK + cl.LEARNED_TTL_SECONDS + 86400
    text = report(mode=cl.MODE_ON, since=later - lw.WEEK_SECONDS, until=later, now=later)
    assert "・現在放進提示詞的學到的規則：0/8 條" in text


@pytest.mark.parametrize(
    ("mode", "env_mode", "line", "decision"),
    [
        (cl.MODE_SHADOW, cl.MODE_SHADOW, "模式：試運轉（只判斷、不寫入，咪寶的回覆不受影響）\n", True),
        (cl.MODE_SHADOW, cl.MODE_ON, "模式：試運轉（只判斷、不寫入，咪寶的回覆不受影響）；設定是「正式」，被狀態檔暫停或降級了", False),
        (cl.MODE_OFF, cl.MODE_ON, "模式：關閉（不判斷、不學）；設定是「正式」，被狀態檔暫停或降級了", False),
        (cl.MODE_OFF, cl.MODE_OFF, "模式：關閉（不判斷、不學）\n", False),
    ],
)
def test_the_mode_line_and_the_decision_line(mode, env_mode, line, decision):
    text = report(mode=mode, env_mode=env_mode)
    assert line in text and (DECISION in text) is decision


def test_run_reads_the_mode_from_the_env_and_the_switch_file(db_env, monkeypatch):
    switch = Path(os.environ[cl.SWITCH_ENV])
    monkeypatch.setenv(cl.ENV_FLAG, "1")
    switch.parent.mkdir(parents=True, exist_ok=True)
    switch.write_text("shadow", encoding="utf-8")
    assert lw.run(AFTER_GRACE) == 0
    text = (db_env / "outbox" / WEEKLY).read_text(encoding="utf-8")
    assert "；設定是「正式」，被狀態檔暫停或降級了" in text and DECISION not in text


def test_active_legacy_rules_and_new_ones_this_week_are_counted():
    legacy_rules(3, status="active")
    legacy_rules(2, status="active", first_seen=IN_WEEK)
    legacy_rules(4)
    assert "舊的糾正規則：啟用中 5 條、停用 4 條，這週新收 2 條" in report()


def test_after_180_days_the_running_total_is_relabelled():
    later = ts(2027, 5, 2, 20)
    text = report(since=later - lw.WEEK_SECONDS, until=later)
    assert "\n近 180 天：抓到 0 則" in text


def test_an_old_db_without_the_tables_gets_one_line_and_no_crash(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    try:
        text = lw.build_report(conn, since=SINCE, until=UNTIL, mode=cl.MODE_SHADOW, env_mode=cl.MODE_SHADOW, names=())
    finally:
        conn.close()
    assert "這個資料庫沒有自動學習糾正的表" in text


def test_a_db_from_before_the_origin_column_counts_every_rule_as_legacy(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "pre_origin.db"))
    try:
        conn.execute("CREATE TABLE complaint_judgements(group_id, complaint_id, stage, result, judged, provider, "
                     "created_at)")
        conn.execute("CREATE TABLE correction_rules(rule_id INTEGER PRIMARY KEY, group_id, canonical_rule, status, "
                     "occurrence_count, first_seen_at, last_seen_at)")
        conn.execute("CREATE TABLE correction_rule_events(event_id INTEGER PRIMARY KEY, group_id, action, rule_id, "
                     "observation_id, payload_json, created_at)")
        conn.execute("INSERT INTO correction_rules VALUES (1, 'g', '舊', 'active', 1, ?, ?)", (IN_WEEK, IN_WEEK))
        text = lw.build_report(conn, since=SINCE, until=UNTIL, mode=cl.MODE_SHADOW, env_mode=cl.MODE_SHADOW, names=())
    finally:
        conn.close()
    assert "舊的糾正規則：啟用中 1 條、停用 0 條，這週新收 1 條" in text


def test_the_report_never_carries_ids_or_family_words(monkeypatch):
    """端到端：真的走一次 observe → 判斷（假模型）→ shadow，再組週報。"""
    monkeypatch.setenv(cl.ENV_FLAG, "shadow")
    monkeypatch.setattr(claude_client, "quota_exhausted", lambda now=None: False)
    monkeypatch.setattr(config.settings, "allowed_group_id", G)
    monkeypatch.setattr(config.settings, "allowed_group_ids_raw", "")
    verdict = {
        "about_bot_reply": True, "is_complaint": True, "category": "only_verified",
        "core_reason": "沒看完內容就說沒提到", "rule": RULE_OK, "evidence": "影片裡沒有提到露營地點",
        "reply_kind": "chat", "same_as_rule_id": None, "confidence": 0.93,
    }
    verify = {"reasonable": True, "loosens_hard_rules": False, "asserts_fact": False, "restricts_replying": False,
              "concedes_to_family": False, "reveals_context": False, "matches_category": True, "confidence": 0.9}

    def fake(system, user, effort):
        return json.dumps(verdict if system == cl.JUDGE_SYSTEM else verify, ensure_ascii=False)

    monkeypatch.setattr(cl, "_claude_json", fake)
    t0 = IN_WEEK
    with memory._lock, memory._conn() as conn:
        conn.execute("INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
                     (G, "M_SENTINEL_BOT", "__bot__", "這支露營影片裡沒有提到露營地點，只介紹了裝備。", t0 - 40))
        conn.execute("INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
                     (G, "M_SENTINEL_FAMILY", "U_TEST_B", f"你沒看到最後嗎？{FAMILY_TEXT}", t0))
        conn.execute("INSERT INTO raw_message_quotes VALUES (?, ?, ?)", (G, "M_SENTINEL_FAMILY", "M_SENTINEL_BOT"))
    cl.observe(G, "M_SENTINEL_FAMILY", "U_TEST_B", f"你沒看到最後嗎？{FAMILY_TEXT}",
               quoted_message_id="M_SENTINEL_BOT", event_ts_ms=t0 * 1000)
    assert cl.process_due(now=t0 + 30, force=True) == ["shadow_pass"]
    text = report(names=cl.load_names())
    assert f"　1. {RULE_OK}（只說查證過的）" in text and "　　・通過：1 則（試運轉：會學到 1）" in text
    for secret in (G, "M_SENTINEL", "U_TEST", "A_SENTINEL", FAMILY_TEXT, "測試成員", "露營地點，只介紹"):
        assert secret not in text


# ── 一輪：放檔、去重、到期、等待、鎖、停用、失敗 ─────────────────────────────────────────


def test_a_due_run_queues_one_private_report_and_never_twice(db_env):
    judgement("Q1", "target", "no_target", IN_WEEK)
    assert lw.run(AFTER_GRACE) == 0
    assert outbox_names(db_env) == [WEEKLY]
    path = db_env / "outbox" / WEEKLY
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8").startswith("🧠 咪寶自動學習糾正週報（11/08 20:00–11/15 20:00）")
    assert lw.run(AFTER_GRACE + 3600) == 0
    assert outbox_names(db_env) == [WEEKLY]
    for folder in ("sent", "failed"):  # relay 搬走（撞名時加 -<ts>）之後也不會再排
        (db_env / folder).mkdir(exist_ok=True)
        moved = db_env / folder / "2026-11-15-complaint-learning-weekly-1763208000.md"
        path.replace(moved)
        assert lw.run(AFTER_GRACE + 7200) == 0
        assert outbox_names(db_env) == []
        moved.replace(path)


def test_a_run_right_after_eight_waits_out_the_grace_before_copying(db_env, sleeps):
    assert lw.run(UNTIL + 5) == 0
    assert sleeps == [lw.GRACE_SECONDS - 5] and outbox_names(db_env) == [WEEKLY]


def test_nothing_is_copied_or_queued_before_it_is_due(db_env, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("must not copy the DB when nothing is due")

    monkeypatch.setattr(lw, "private_snapshot", boom)
    for local in ((2026, 11, 13, 10, 0), (2026, 11, 15, 19, 59)):
        assert lw.run(ts(*local)) == 0
    assert outbox_names(db_env) == []


def test_a_missing_maintenance_dir_exits_2_without_copying(db_env, monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise AssertionError("must not copy the DB without an outbox")

    monkeypatch.setenv(lw.MAINTENANCE_ENV, str(tmp_path / "gone"))
    monkeypatch.setattr(lw, "private_snapshot", boom)
    assert lw.run(AFTER_GRACE) == lw.EXIT_CONFIG


def test_the_disabled_flag_and_a_held_lock_stop_the_run(db_env):
    state = Path(os.environ[lw.STATE_ENV])
    (state / lw.DISABLED_NAME).touch()
    assert lw.run(AFTER_GRACE) == 0 and outbox_names(db_env) == []
    (state / lw.DISABLED_NAME).unlink()
    fd = os.open(state / lw.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert lw.run(AFTER_GRACE) == 0 and outbox_names(db_env) == []
    finally:
        os.close(fd)
    assert lw.run(AFTER_GRACE) == 0 and outbox_names(db_env) == [WEEKLY]


def test_the_second_failure_in_a_row_queues_one_short_note_a_day(db_env, monkeypatch, capsys):
    monkeypatch.setenv(lw.DB_ENV, str(Path(os.environ[lw.STATE_ENV]) / "missing.db"))
    assert lw.run(AFTER_GRACE) == 1
    assert outbox_names(db_env) == []  # 第一次失敗不通知（多半下一個整點就好了）
    assert lw.run(AFTER_GRACE + 3600) == 1
    assert lw.run(AFTER_GRACE + 7200) == 1
    assert outbox_names(db_env) == [FAILED]
    note = (db_env / "outbox" / FAILED).read_text(encoding="utf-8")
    assert "階段：report" in note and "類別：FileNotFoundError" in note and "missing.db" not in note
    assert "missing.db" not in capsys.readouterr().err
    monkeypatch.setenv(lw.DB_ENV, str(memory._DB_PATH))
    assert lw.run(AFTER_GRACE + 10800) == 0
    assert outbox_names(db_env) == [FAILED, WEEKLY]  # 短訊的前綴擋不到同一天的週報
    assert not (Path(os.environ[lw.STATE_ENV]) / lw.FAILURES_NAME).exists()


def test_a_relay_renamed_failure_note_still_counts_for_the_day(db_env, monkeypatch):
    monkeypatch.setenv(lw.DB_ENV, str(Path(os.environ[lw.STATE_ENV]) / "missing.db"))
    (db_env / "sent").mkdir()
    (db_env / "sent" / "2026-11-15-complaint-learning-failed-1763208000.md").write_text("x", encoding="utf-8")
    for hour in range(3):
        assert lw.run(AFTER_GRACE + 3600 * hour) == 1
    assert outbox_names(db_env) == []


def test_a_copy_that_fails_quick_check_twice_is_retried_once_then_reported(db_env, monkeypatch, sleeps, capsys):
    monkeypatch.setattr(lw, "_copy_db", lambda src, dst: Path(dst).write_bytes(b"not a database" * 100))
    assert lw.run(AFTER_GRACE) == 1
    assert sleeps == [lw.COPY_RETRY_SLEEP]
    assert "failed stage=report error=CopyNotOk" in capsys.readouterr().err


def test_a_timeout_still_removes_the_copy_and_counts_as_a_failure(db_env, monkeypatch, capsys):
    seen = {}

    def slow(conn, **_kwargs):
        seen["path"] = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        raise lw.JobTimeout

    monkeypatch.setattr(lw, "build_report", slow)
    assert lw.run(AFTER_GRACE) == 1
    assert not seen["path"].exists() and not seen["path"].parent.exists()
    assert "error=JobTimeout" in capsys.readouterr().err
    assert json.loads((Path(os.environ[lw.STATE_ENV]) / lw.FAILURES_NAME).read_text())["consecutive"] == 1


def test_the_db_copy_is_private_and_gone_after_the_run(db_env, monkeypatch):
    source = Path(memory._DB_PATH)
    before = (source.stat().st_size, source.stat().st_mtime_ns)
    seen = {}
    real_build = lw.build_report

    def spy(conn, **kwargs):
        path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        seen.update(file=stat.S_IMODE(path.stat().st_mode), folder=stat.S_IMODE(path.parent.stat().st_mode),
                    path=path)
        return real_build(conn, **kwargs)

    monkeypatch.setattr(lw, "build_report", spy)
    assert lw.run(AFTER_GRACE) == 0
    assert seen["file"] == 0o600 and seen["folder"] == 0o700
    assert seen["path"].resolve().is_relative_to(Path(os.environ[lw.COPY_PARENT_ENV]).resolve())
    assert not seen["path"].exists() and not seen["path"].parent.exists()
    assert (source.stat().st_size, source.stat().st_mtime_ns) == before


def test_stale_copies_are_judged_by_the_real_clock(db_env):
    root = lw.copy_root()
    old, fresh = root / "weekly_old", root / "weekly_fresh"
    old.mkdir()
    fresh.mkdir()
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    assert lw.run(ts(2027, 6, 6, 21)) == 0  # 注入很久以後的時間也不會把剛建的當成過期
    assert not old.exists() and fresh.exists()


def test_dry_run_prints_a_preview_or_the_queued_week_and_writes_nothing(db_env, capsys):
    judgement("D1", "target", "no_target", ts(2026, 11, 12, 9))
    assert lw.main(["--dry-run", "--now", str(ts(2026, 11, 13, 10))]) == 0
    out = capsys.readouterr().out
    assert "🧠 咪寶自動學習糾正週報（預覽：11/06 10:00–11/13 10:00）" in out and "（dry-run：" in out
    assert outbox_names(db_env) == []
    assert not (Path(os.environ[lw.STATE_ENV]) / lw.LOCK_NAME).exists()
    assert lw.run(AFTER_GRACE) == 0
    assert lw.main(["--dry-run", "--now", str(AFTER_GRACE + 60)]) == 0
    assert "🧠 咪寶自動學習糾正週報（11/08 20:00–11/15 20:00）" in capsys.readouterr().out
    assert outbox_names(db_env) == [WEEKLY]


def test_now_needs_dry_run_and_the_plist_runs_hourly_with_home_and_path_only(capsys):
    assert lw.main(["--now", "1"]) == lw.EXIT_CONFIG
    assert lw.main(["--print-plist"]) == 0
    plist = capsys.readouterr().out
    assert f"<string>{lw.LABEL}</string>" in plist and "jobs/learned_rules_weekly.py" in plist
    assert "<string>-I</string>\n\t\t<string>-B</string>" in plist
    assert "<key>Minute</key>\n\t\t<integer>0</integer>" in plist and "<key>RunAtLoad</key>\n\t<true/>" in plist
    env = plist.split("<key>EnvironmentVariables</key>")[1].split("</dict>")[0]
    assert env.count("<key>") == 2 and "<key>HOME</key>" in env and "<key>PATH</key>" in env


def test_run_by_path_from_outside_the_repo_imports_no_bot_state_module(db_env, tmp_path):
    """launchd 用路徑跑：sys.path[0] 不是 bot 目錄，要靠程式自己放進去；整輪（dry-run：讀名單、抄 DB、組週報）
    都不能 import memory／main／config（GP1 N16、T13；GP2：用 runpy 包一層，不在程式裡放測試用旗標）。"""
    judgement("P1", "target", "no_target", IN_WEEK)
    job = Path(lw.__file__)
    probe = (
        "import runpy, sys\n"
        f"sys.argv = [{str(job)!r}, '--dry-run', '--now', '{AFTER_GRACE}']\n"
        "try:\n"
        f"    runpy.run_path({str(job)!r}, run_name='__main__')\n"
        "except SystemExit as exc:\n"
        "    print('exit', exc.code)\n"
        "print(sorted(m for m in ('memory', 'main', 'config', 'jobs.learned_rules') if m in sys.modules))\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    done = subprocess.run([sys.executable, "-I", "-B", "-c", probe], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    lines = done.stdout.strip().splitlines()
    assert lines[-2:] == ["exit 0", "[]"], done.stdout[-2000:]
    assert "🧠 咪寶自動學習糾正週報（11/08 20:00–11/15 20:00）" in done.stdout
    assert "（名單讀不到" not in done.stdout  # 名單讀得到（conftest 的隔離名單檔）
    plist = subprocess.run([sys.executable, "-I", "-B", str(job), "--print-plist"], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=60)
    assert plist.returncode == 0 and lw.LABEL in plist.stdout


def test_a_full_run_never_touches_the_real_outbox(db_env):
    real = lw.DEFAULT_MAINTENANCE
    before = {sub: set(os.listdir(real / sub)) if (real / sub).is_dir() else set()
              for sub in ("outbox", "sent", "failed")}
    assert Path(os.environ[lw.MAINTENANCE_ENV]) != real
    far = ts(2031, 1, 5, 20)  # 週日；正式 job 不會有這一天的檔（P1 N-10：不要和真的 11/15 週報撞名）
    assert lw.report_sunday(far + 600) == date(2031, 1, 5)
    assert lw.run(far + 600) == 0
    assert outbox_names(db_env) == ["2031-01-05-complaint-learning-weekly.md"]
    for sub, names in before.items():
        now_names = set(os.listdir(real / sub)) if (real / sub).is_dir() else set()
        assert not {n for n in now_names - names if n.startswith("2031-01-05-")}


# ── 審查 GP2 加的測試 ───────────────────────────────────────────────────────────────


def test_the_conftest_default_paths_do_not_exist_and_a_stray_run_creates_nothing():
    """GP2 G1：conftest 只設環境變數、不建目錄；誤跑到 job 時 outbox 所在目錄不在 → exit 2，什麼都不建。"""
    paths = [Path(os.environ[name]) for name in (lw.MAINTENANCE_ENV, lw.DB_ENV, lw.STATE_ENV, lw.COPY_PARENT_ENV)]
    assert not any(p.exists() for p in paths)
    assert lw.run(AFTER_GRACE) == lw.EXIT_CONFIG
    with pytest.raises(FileNotFoundError):
        lw.dry_run(AFTER_GRACE, sys.stdout)
    assert not any(p.exists() for p in paths) and not paths[0].parent.exists()


def test_every_run_clears_a_leftover_copy_even_when_nothing_is_due(db_env):
    root = lw.copy_root()
    left = root / "weekly_left_by_sigkill"
    left.mkdir()
    old = time.time() - lw.STALE_SECONDS - 60
    os.utime(left, (old, old))
    assert lw.run(ts(2026, 11, 13, 10)) == 0  # 沒到期
    assert not left.exists()


def test_a_failure_count_that_cannot_be_saved_still_sends_the_note(db_env, monkeypatch):
    monkeypatch.setenv(lw.DB_ENV, str(Path(os.environ[lw.STATE_ENV]) / "missing.db"))

    def broken():
        raise OSError("state dir is read-only")

    monkeypatch.setattr(lw, "_record_failure", broken)
    assert lw.run(AFTER_GRACE) == 1
    assert outbox_names(db_env) == [FAILED]


def test_a_sqlite_path_that_points_elsewhere_is_refused_not_followed(weekly_dirs, monkeypatch, tmp_path):
    monkeypatch.delenv(lw.DB_ENV)
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "other.db"))
    with pytest.raises(lw.ConfigMismatch):
        lw.db_path()
    monkeypatch.setenv("SQLITE_PATH", "line_bot.db")  # 相對路徑以 bot 目錄為準，就是同一個檔
    assert lw.db_path() == lw.BASE / "line_bot.db"
    monkeypatch.delenv("SQLITE_PATH")
    assert lw.db_path() == lw.BASE / "line_bot.db"


def test_no_other_text_column_ever_reaches_the_report(db_env, capsys):
    """GP2 G9：核心原因、事件 payload／observation id、舊規則文字、糾正原話都不能出現在週報、stdout、stderr。"""
    judgement("S9", "shadow", "shadow_pass", IN_WEEK, judged=1, provider="claude")
    with memory._lock, memory._conn() as conn:
        conn.execute(
            "INSERT INTO complaint_shadow_rules(group_id, complaint_id, category, core_reason, rule, created_at) "
            "VALUES (?, 'S9', 'only_verified', 'CORE_REASON_SENTINEL', ?, ?)", (G, RULE_OK, IN_WEEK))
    stored = learn(RULE_OK_2, oid="judge:OBS_SENTINEL", now=IN_WEEK + 60, category="stay_on_topic")
    with memory._lock, memory._conn() as conn:
        conn.execute("UPDATE correction_rule_events SET payload_json=? WHERE rule_id=?",
                     (json.dumps({"category": "stay_on_topic", "note": "PAYLOAD_SENTINEL"}), stored["rule_id"]))
    legacy_rules(1, status="active", first_seen=IN_WEEK)
    with memory._lock, memory._conn() as conn:
        conn.execute("UPDATE correction_rules SET canonical_rule='LEGACY_SENTINEL' WHERE origin=''")
    assert lw.run(AFTER_GRACE) == 0
    text = (db_env / "outbox" / WEEKLY).read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert RULE_OK in text and RULE_OK_2 in text  # 兩種規則都列（試運轉＋學到）
    for secret in ("CORE_REASON_SENTINEL", "PAYLOAD_SENTINEL", "OBS_SENTINEL", "LEGACY_SENTINEL", G, "S9"):
        assert secret not in text and secret not in captured.out and secret not in captured.err


def test_every_result_code_the_writers_can_produce_has_a_plain_label():
    """GP2 對 GP1 I2：寫入端新增的代碼要在這裡失敗，不能在週報裡變成英文。"""
    source = Path(cl.__file__).read_text(encoding="utf-8")
    assert set(re.findall(r'"(check:[a-z_]+)"', source)) <= set(lw.CHECK_LABELS)
    assert set(re.findall(r'return "([a-z_]+)"', inspect.getsource(cl.verdict_rejection))) <= set(lw.VERDICT_LABELS)
    assert set(re.findall(r'return None, "([a-z_]+)"', inspect.getsource(cl.find_target))) <= set(lw.NOT_SENT_LABELS)
    verify = set(re.findall(r'return "([a-z_]+)"', inspect.getsource(cl.verify_rule)))
    assert verify and verify <= {"verifier_rejected", *lw.VERIFY_NO_ANSWER_LABELS}
    limits = set(re.findall(r'stage="(?:dedupe|cap)", result="([a-z_]+)"', source))
    assert limits and limits <= set(lw.LIMIT_LABELS)
    store = inspect.getsource(memory.record_learned_rule)
    statuses = set(re.findall(r'"status":\s*"([a-z_]+)"', store))
    statuses |= set(re.findall(r'decision = status = "([a-z_]+)"', store))
    statuses |= set(re.findall(r'decision, status(?:, rule_id)? = "[a-z_]+", "([a-z_]+)"', store))
    for group in re.findall(r'picked in \{([^}]*)\}', store):
        statuses |= set(re.findall(r'"([a-z_]+)"', group))
    assert {"created", "merged", "duplicate", "refused_active"} <= statuses  # 抓得到（寫法改了要更新這裡）
    assert statuses <= set(lw.PASSED_LABELS) | set(lw.NOT_STORED_LABELS)


# ── 實作後審查（POST_P1、POST_P2）加的測試 ─────────────────────────────────────────────


def test_an_empty_name_list_withholds_rule_text_like_an_unreadable_one():
    judgement("E1", "shadow", "shadow_pass", IN_WEEK, judged=1, provider="claude")
    shadow_rule("E1", RULE_OK, IN_WEEK)
    text = report(names=())
    assert RULE_OK not in text and "　（名單讀不到，這次不列規則文字）" in text


@pytest.mark.parametrize("code", ["123456789012345678", "deadbeefdeadbeefdeadbeefdeadbeef0", "abcdef", "x" * 70,
                                  "check_9999_digits"])
def test_codes_that_look_like_ids_are_never_echoed(code):
    judgement("I1", "target", code, IN_WEEK)
    text = report()
    assert code not in text and "　・沒送去判斷：1 則（沒見過的代碼 1）" in text


def test_every_stage_the_writers_use_is_known():
    """P1 N-4：寫入端新增的關卡要在這裡失敗；沒見過的關卡又沒叫到模型時不算「判斷器看過」。"""
    source = Path(cl.__file__).read_text(encoding="utf-8")
    stages = set(re.findall(r'stage="([a-z_]+)"', source))
    stages |= set(re.findall(r'Evaluation\("([a-z_]+)"', source))
    stages |= set(re.findall(r'return "([a-z_]+)" if reason == "names_unavailable" else "([a-z_]+)"', source)[0])
    stages |= set(re.findall(r'stage: str = "([a-z_]+)"', source))
    assert {"target", "names", "cap", "judge", "shadow", "store"} <= stages
    assert stages <= set(lw.KNOWN_STAGES)
    assert lw.classify("prefilter", "new_reason", 0)[0] == "no_answer"
    assert lw.classify("prefilter", "new_reason", 1)[:2] == ("seen", "other")


def test_on_mode_counts_merges_refusals_retirements_expiries_and_revivals():
    first = learn(RULE_OK, oid="judge:EV1", now=IN_WEEK)
    merged = memory.record_learned_rule(
        G, "judge:EV2", rule=RULE_OK, core_reason="合成原因", category="only_verified", source_message_ids=["EV2"],
        same_as_rule_id=first["rule_id"], day_start=cl.day_start(IN_WEEK + 60), now=IN_WEEK + 60,
        observed_at=IN_WEEK + 60, max_active=8, max_new_per_day=1, ttl_seconds=cl.LEARNED_TTL_SECONDS,
    )
    refused = learn(RULE_OK_2, oid="judge:EV3", now=IN_WEEK + 120, category="stay_on_topic")
    assert (merged["status"], refused["status"]) == ("merged", "refused_daily")
    with memory._lock, memory._conn() as conn:
        for action, at in (("retire", IN_WEEK + 180), ("expire", IN_WEEK + 240), ("revive", IN_WEEK + 300)):
            conn.execute("INSERT INTO correction_rule_events(group_id, action, rule_id, observation_id, payload_json, "
                         "created_at) VALUES (?, ?, ?, '', ?, ?)",
                         (G, action, first["rule_id"], json.dumps({"category": "only_verified"}), at))
        conn.execute("UPDATE correction_rules SET status='active' WHERE rule_id=?", (first["rule_id"],))
    text = report(mode=cl.MODE_ON, names=cl.load_names())
    assert "・新學到的規則：2 條" in text  # create＋revive
    assert "・併到已有的規則：1 次；到上限沒收：1 條" in text
    assert "・撤銷：1 條；60 天沒再出現自動退休：1 條" in text


def test_a_learned_rule_whose_complaint_was_unsent_is_listed_without_its_text():
    """P2-I1：正式模式下家人收回抱怨 → memory.unlink_unsent_complaint 停用規則；週報只列編號，不列文字
    （試運轉時收回會連 shadow 列一起刪，兩邊一致）。"""
    stored = learn(RULE_OK, oid="judge:UN1", now=IN_WEEK)
    judgement("UN1", "store", "created", IN_WEEK, judged=1, provider="claude")
    with memory._lock, memory._conn() as conn:
        conn.execute("UPDATE complaint_judgements SET rule_id=? WHERE complaint_id='UN1'", (stored["rule_id"],))
    memory.unlink_unsent_complaint(G, "UN1")
    with memory._lock, memory._conn() as conn:
        status = conn.execute("SELECT status FROM correction_rules WHERE rule_id=?", (stored["rule_id"],)).fetchone()
    assert status[0] != "active"
    text = report(mode=cl.MODE_ON, names=cl.load_names())
    assert RULE_OK not in text
    assert f"　1. #{stored['rule_id']} （已停用，不列文字）（只說查證過的）" in text
    assert "　　・通過但沒存：1 則（家人收回了訊息 1）" in text


def test_a_stale_draft_in_the_outbox_is_removed_and_a_fresh_one_kept(db_env):
    outbox = db_env / "outbox"
    outbox.mkdir()
    old = outbox / ".2026-11-08-complaint-learning-weekly.123.tmp"
    fresh = outbox / ".2026-11-08-complaint-learning-weekly.456.tmp"
    other = outbox / ".someone-else.tmp"
    for path in (old, fresh, other):
        path.write_text("x", encoding="utf-8")
    then = time.time() - lw.STALE_SECONDS - 60
    os.utime(old, (then, then))
    os.utime(other, (then, then))
    assert lw.run(AFTER_GRACE) == 0
    assert not old.exists() and fresh.exists() and other.exists()


def test_sigterm_removes_the_copy_and_is_not_counted_as_a_failure(db_env, monkeypatch):
    seen = {}

    def stopped(conn, **_kwargs):
        seen["path"] = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        raise SystemExit(128 + 15)  # 同 _exit_on_signal

    monkeypatch.setattr(lw, "build_report", stopped)
    with pytest.raises(SystemExit):
        lw.run(AFTER_GRACE)
    assert not seen["path"].exists() and not seen["path"].parent.exists()
    assert not (Path(os.environ[lw.STATE_ENV]) / lw.FAILURES_NAME).exists()
    assert outbox_names(db_env) == []


def test_a_sqlite_path_mismatch_stops_a_run_before_any_copy(db_env, monkeypatch, tmp_path, capsys):
    def boom(*_a, **_k):
        raise AssertionError("must not copy the DB")

    monkeypatch.delenv(lw.DB_ENV)
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "other.db"))
    monkeypatch.setattr(lw, "private_snapshot", boom)
    assert lw.run(AFTER_GRACE) == 1
    assert "failed stage=config error=ConfigMismatch" in capsys.readouterr().err
    assert json.loads((Path(os.environ[lw.STATE_ENV]) / lw.FAILURES_NAME).read_text())["consecutive"] == 1
    monkeypatch.setenv("SQLITE_PATH", "  line_bot.db  ")  # config 會去掉前後空白（P1 N-11）
    assert lw.db_path() == lw.BASE / "line_bot.db"


def test_a_plain_run_outside_the_production_checkout_is_refused(db_env, monkeypatch, capsys):
    """P2 N3：worktree／clone 沒指定 outbox 就跑，不能用錯的 DB 排掉正式那週、也不能發假的失敗短訊。"""
    monkeypatch.delenv(lw.MAINTENANCE_ENV)
    assert not lw._same_dir(lw.BASE, lw.PRODUCTION_BOT_DIR)  # 測試在 worktree／clone 跑（不在正式目錄跑測試）
    assert lw.run(AFTER_GRACE) == lw.EXIT_CONFIG
    assert "not the production checkout" in capsys.readouterr().err


def test_a_missing_state_dir_exits_2_without_a_note(db_env, monkeypatch, tmp_path):
    monkeypatch.setenv(lw.STATE_ENV, str(tmp_path / "no_state"))
    assert lw.run(AFTER_GRACE) == lw.EXIT_CONFIG
    assert outbox_names(db_env) == []


def test_the_job_module_does_not_import_networking_modules(tmp_path):
    """P2 N4：不 import xml.sax（會拉進 urllib、http、ssl、socket）。"""
    job = Path(lw.__file__)
    probe = (
        "import runpy, sys\n"
        f"runpy.run_path({str(job)!r}, run_name='weekly_probe')\n"
        "print(sorted(m for m in ('xml.sax', 'urllib.request', 'http.client', 'ssl') if m in sys.modules))\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    done = subprocess.run([sys.executable, "-I", "-B", "-c", probe], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "[]"
