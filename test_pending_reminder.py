"""Pending reminder 補抽（方案 B）— quota 爆時不再靜默丟提醒。

2026-05-30: reminder 抽取在 Gemini 額度爆時 100% 丟失（webhook 短路繞過
_maybe_extract_reminder）。方案 B：forward-only 入隊 + 額度恢復後補抽。
覆蓋 §3 review chain 的 R1（相對日期用 created_at）/R3（429 mark vs transient）/
drain 的成功·dropped·過期·quota-gate·release。
"""

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def temp_db(monkeypatch):
    """隔離 memory._DB_PATH 到 temp，避免寫穿 production line_bot.db。"""
    import memory
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    monkeypatch.setattr(memory, "_DB_PATH", tmp_path)
    memory._init_db()
    yield tmp_path
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass


def _quota_429_perday():
    return Exception(
        "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
        "generate_content_free_tier_requests ... PerDay ... limit: 20"
    )


def _quota_429_transient():
    return Exception("429 RESOURCE_EXHAUSTED per-minute rate limit")


# ── helper: enqueue gate ──────────────────────────────────────────────────────

def test_enqueue_candidate_with_hint(temp_db):
    import main
    import memory
    main._enqueue_reminder_if_candidate("6/3下午8點開會", "G1", "U1", "m1")
    rows = memory.list_pending_reminder_retries("G1")
    assert len(rows) == 1 and rows[0]["text"] == "6/3下午8點開會"


def test_enqueue_skips_non_candidate(temp_db):
    """無日期+時間 hint 的閒聊不入隊（否則每則訊息都進隊列、drain 燒 quota 在垃圾上）。"""
    import main
    import memory
    main._enqueue_reminder_if_candidate("今天天氣真好啊", "G1", "U1", "m1")
    assert memory.list_pending_reminder_retries("G1") == []


# ── site 2: _maybe_extract_reminder 撞 429 ───────────────────────────────────

def test_maybe_extract_perday_429_marks_and_enqueues(temp_db, monkeypatch):
    """R3: 日額度 429 → _mark_quota_exhausted + 入隊。"""
    import main
    import memory
    import gemini_client
    marked = []
    monkeypatch.setattr(gemini_client, "extract_reminder",
                        lambda *a, **k: (_ for _ in ()).throw(_quota_429_perday()))
    monkeypatch.setattr(main, "_mark_quota_exhausted", lambda: marked.append(1))
    main._maybe_extract_reminder("6/3下午8點開會", "G1", "U1", "m1")
    assert marked == [1], "PerDay 429 應 mark exhausted"
    assert len(memory.list_pending_reminder_retries("G1")) == 1, "應入隊補抽"


def test_maybe_extract_transient_429_enqueues_no_mark(temp_db, monkeypatch):
    """R3 防禦分支: 不含 PerDay 標記的 429 → 入隊但不 mark（不壓死全天額度）。

    註（Phase6 GP-A）: free tier 實測所有 429 都是 PerDay
    (GenerateRequestsPerDayPerProjectPerModel-FreeTier)、無獨立 RPM quota，故此分支
    在當前生產不觸發；保留為 defensive（防未來 Google 加 RPM 或誤分類）。enqueue 在
    兩分支都做，分類錯也不丟提醒。"""
    import main
    import memory
    import gemini_client
    marked = []
    monkeypatch.setattr(gemini_client, "extract_reminder",
                        lambda *a, **k: (_ for _ in ()).throw(_quota_429_transient()))
    monkeypatch.setattr(main, "_mark_quota_exhausted", lambda: marked.append(1))
    main._maybe_extract_reminder("6/3下午8點開會", "G1", "U1", "m1")
    assert marked == [], "transient 429 不可 mark 全天爆"
    assert len(memory.list_pending_reminder_retries("G1")) == 1, "transient 也入隊重抽"


def test_maybe_extract_none_falls_back_to_calendar_regex(temp_db, monkeypatch):
    """Gemini 回 None 時，醫療日期句仍要 deterministic 存進 reminders。"""
    import main
    import memory
    import gemini_client

    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: None)

    main._maybe_extract_reminder("明天早上十點半看台大陳敏惠牙醫師", "G1", "U1", "m1")

    with memory._conn() as c:
        row = c.execute(
            "SELECT action FROM reminders WHERE group_id='G1' AND status='pending'"
        ).fetchone()
    assert row is not None
    assert "牙醫" in row[0]


def test_maybe_extract_subjectless_medical_uses_sender_alias(temp_db, monkeypatch):
    """Subjectless medical reminders should include who from sender alias."""
    import main
    import memory
    import gemini_client

    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: None)
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "媽媽" if uid == "U_MOM" else "",
    )

    main._maybe_extract_reminder(
        "明天早上十點半看台大陳敏惠牙醫師", "G1", "U_MOM", "m1"
    )

    with memory._conn() as c:
        row = c.execute(
            "SELECT action FROM reminders WHERE group_id='G1' AND status='pending'"
        ).fetchone()
    assert row is not None
    assert row[0] == "媽媽看台大陳敏惠牙醫師"


def test_maybe_extract_rewrites_first_person_action_to_sender_alias(temp_db, monkeypatch):
    """Gemini may return first-person action; store concrete actor instead."""
    import main
    import memory
    import gemini_client

    future = datetime.now() + timedelta(days=2)
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *a, **k: {
            "action": "我看台大陳敏惠牙醫師",
            "year": future.year,
            "month": future.month,
            "day": future.day,
            "hour": 10,
            "minute": 30,
        },
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "媽媽" if uid == "U_MOM" else "",
    )

    main._maybe_extract_reminder(
        "星期四早上十點半看台大陳敏惠牙醫師", "G1", "U_MOM", "m1"
    )

    with memory._conn() as c:
        row = c.execute(
            "SELECT action FROM reminders WHERE group_id='G1' AND status='pending'"
        ).fetchone()
    assert row is not None
    assert row[0] == "媽媽看台大陳敏惠牙醫師"


def test_maybe_extract_medical_prep_inherits_patient_and_companion(temp_db, monkeypatch):
    """Medical prep reminders must keep patient and companion roles from context."""
    import main
    import memory
    import gemini_client

    future = datetime.now() + timedelta(days=2)
    action = "正子斷層掃描當天 08:00 開始禁食 6 小時，只能喝水"
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *a, **k: {
            "action": action,
            "year": future.year,
            "month": future.month,
            "day": future.day,
            "hour": 8,
            "minute": 0,
        },
    )
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda uid: "媽媽" if uid == "U_MOM" else "",
    )

    text = (
        f"{future.month}月{future.day}日星期二下午兩點前要到台大醫院東址地下1樓"
        "做正子斷層掃描。聖雅要陪我去，大約需要兩個多小時。"
        "當天早上8:00開始禁食 6小時。只能喝水。"
    )
    main._maybe_extract_reminder(text, "G1", "U_MOM", "m1")

    with memory._conn() as c:
        row = c.execute(
            "SELECT action, mention_aliases FROM reminders "
            "WHERE group_id='G1' AND status='pending'"
        ).fetchone()
    assert row is not None
    assert row[0] == f"媽媽{action}（黃聖雅陪同）"
    assert row[1] == '["媽媽", "黃聖雅"]'


def _future_tuesday_before_thursday() -> tuple[datetime, datetime]:
    """Return a future Tuesday message time and its Thursday 10:30 target."""
    target = datetime.now() + timedelta(days=1)
    while target.weekday() != 3:  # Thursday
        target += timedelta(days=1)
    target = target.replace(hour=10, minute=30, second=0, microsecond=0)
    msg_time = (target - timedelta(days=2)).replace(hour=9, minute=0)
    return msg_time, target


# ── drain: R1 相對日期 ────────────────────────────────────────────────────────

def test_drain_relative_date_uses_message_created_at(temp_db, monkeypatch):
    """R1（GP1 critical）: drain 重抽「明天」要用訊息當時 created_at，不是 drain 當天。"""
    import main
    import memory
    import gemini_client
    # 真實情境：訊息昨天進來（quota 爆），今天額度恢復 drain。「明天」必須對到
    # 訊息當天的明天，不是 drain 當天的明天。用昨天（< 7 天 stale 閾值，不被 drop）。
    yesterday = datetime.now() - timedelta(days=1)
    memory.enqueue_pending_reminder("G1", "U1", "明天早上9點開會", "m1")
    with memory._conn() as c:
        c.execute("UPDATE pending_reminder_extract SET created_at=? WHERE message_id='m1'",
                  (int(yesterday.timestamp()),))
    captured = {}

    def fake_extract(text, today_iso=None):
        captured["today_iso"] = today_iso
        return None

    monkeypatch.setattr(gemini_client, "extract_reminder", fake_extract)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    main._drain_pending_reminders("G1")
    expected = yesterday.strftime("%Y-%m-%d")
    assert captured.get("today_iso", "").startswith(expected), \
        f"R1 broken: today_iso={captured.get('today_iso')!r} 應為訊息當天 {expected}（不是今天）"


# ── drain: 成功 / dropped / 過期 ─────────────────────────────────────────────

def test_drain_success_adds_reminder(temp_db, monkeypatch):
    import main
    import memory
    import gemini_client
    future = datetime.now() + timedelta(days=3)
    memory.enqueue_pending_reminder("G1", "U1", "3天後下午8點開會", "m1")
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: {
        "action": "開會", "year": future.year, "month": future.month,
        "day": future.day, "hour": 20, "minute": 0,
    })
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    main._drain_pending_reminders("G1")
    assert memory.list_pending_reminder_retries("G1") == [], "成功後應離開 pending"
    # reminders 表應有一筆
    with memory._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM reminders WHERE action='開會'").fetchone()[0]
    assert n == 1, "drain 成功應寫進 reminders"


def test_drain_add_reminder_failure_releases_claim(temp_db, monkeypatch):
    """After claim, DB/write failures must not strand the row in processing."""
    import main
    import memory
    import gemini_client
    future = datetime.now() + timedelta(days=3)
    memory.enqueue_pending_reminder("G1", "U1", "3天後下午8點開會", "m1")
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: {
        "action": "開會", "year": future.year, "month": future.month,
        "day": future.day, "hour": 20, "minute": 0,
    })
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    monkeypatch.setattr(memory, "add_reminder", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db locked")))

    main._drain_pending_reminders("G1")

    with memory._conn() as c:
        status, retries = c.execute(
            "SELECT status, retries FROM pending_reminder_extract WHERE message_id='m1'"
        ).fetchone()
    assert status == "pending"
    assert retries == 1


def test_processing_claim_reclaimed_after_worker_crash(temp_db):
    """A killed worker must not leave reminder rows invisible until 7d stale drop."""
    import time
    import memory

    pid = memory.enqueue_pending_reminder("G1", "U1", "3天後下午8點開會", "m1")
    assert pid is not None
    assert memory.claim_pending_reminder(pid)
    old_claim = int(time.time()) - 1200
    with memory._conn() as c:
        c.execute(
            "UPDATE pending_reminder_extract SET claimed_at=? WHERE pending_id=?",
            (old_claim, pid),
        )

    rows = memory.list_pending_reminder_retries("G1")

    assert [r["pending_id"] for r in rows] == [pid]
    with memory._conn() as c:
        status, retries, claimed_at = c.execute(
            "SELECT status, retries, claimed_at FROM pending_reminder_extract "
            "WHERE pending_id=?",
            (pid,),
        ).fetchone()
    assert status == "pending"
    assert retries == 1
    assert claimed_at == 0


def test_drain_none_drops(temp_db, monkeypatch):
    """Gemini 判定非提醒(None) → dropped，不無限重抽。"""
    import main
    import memory
    import gemini_client
    memory.enqueue_pending_reminder("G1", "U1", "6/3下午8點開會", "m1")
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: None)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    main._drain_pending_reminders("G1")
    assert memory.list_pending_reminder_retries("G1") == [], "None 應 dropped 離開 pending"
    with memory._conn() as c:
        st = c.execute("SELECT status FROM pending_reminder_extract WHERE message_id='m1'").fetchone()[0]
    assert st == "dropped"


def test_drain_none_falls_back_to_calendar_regex_with_message_date(temp_db, monkeypatch):
    """Pending drain 的 None 也要用訊息 created_at 解「星期四」。"""
    import main
    import memory
    import gemini_client

    memory.enqueue_pending_reminder(
        "G1", "U1", "星期四早上十點半看台大陳敏惠牙醫師", "m1"
    )
    msg_time, expected = _future_tuesday_before_thursday()
    with memory._conn() as c:
        c.execute(
            "UPDATE pending_reminder_extract SET created_at=? WHERE message_id='m1'",
            (int(msg_time.timestamp()),),
        )
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: None)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)

    main._drain_pending_reminders("G1")

    with memory._conn() as c:
        row = c.execute(
            "SELECT action, remind_at "
            "FROM reminders WHERE group_id='G1'"
        ).fetchone()
        status = c.execute(
            "SELECT status FROM pending_reminder_extract WHERE message_id='m1'"
        ).fetchone()[0]
    assert row is not None
    assert row[0] == "看台大陳敏惠牙醫師"
    assert datetime.fromtimestamp(row[1]).strftime("%Y-%m-%d %H:%M") == (
        expected.strftime("%Y-%m-%d %H:%M")
    )
    assert status == "done"


def test_drain_subjectless_medical_uses_sender_alias(temp_db, monkeypatch):
    """Pending drain should preserve sender identity when Gemini returns None."""
    import main
    import memory
    import gemini_client

    memory.enqueue_pending_reminder(
        "G1", "U_MOM", "星期四早上十點半看台大陳敏惠牙醫師", "m1"
    )
    msg_time, _expected = _future_tuesday_before_thursday()
    with memory._conn() as c:
        c.execute(
            "UPDATE pending_reminder_extract SET created_at=? WHERE message_id='m1'",
            (int(msg_time.timestamp()),),
        )
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: None)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    monkeypatch.setattr(main, "_alias_from_user_id", lambda uid: "媽媽" if uid == "U_MOM" else "")

    main._drain_pending_reminders("G1")

    with memory._conn() as c:
        row = c.execute(
            "SELECT action FROM reminders WHERE group_id='G1' AND status='pending'"
        ).fetchone()
    assert row is not None
    assert row[0] == "媽媽看台大陳敏惠牙醫師"


def test_drain_expired_drops(temp_db, monkeypatch):
    """抽出來的時間已過期 → dropped，不寫進 reminders。"""
    import main
    import memory
    import gemini_client
    past = datetime.now() - timedelta(days=2)
    memory.enqueue_pending_reminder("G1", "U1", "前天8點", "m1")
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: {
        "action": "過期事件", "year": past.year, "month": past.month,
        "day": past.day, "hour": 20, "minute": 0,
    })
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    main._drain_pending_reminders("G1")
    with memory._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM reminders WHERE action='過期事件'").fetchone()[0]
        st = c.execute("SELECT status FROM pending_reminder_extract WHERE message_id='m1'").fetchone()[0]
    assert n == 0 and st == "dropped"


# ── drain: quota gate + release ──────────────────────────────────────────────

def test_drain_quota_gate_skips(temp_db, monkeypatch):
    """額度仍爆 → drain 完全不動（不浪費 API、不誤標）。"""
    import main
    import memory
    import gemini_client
    memory.enqueue_pending_reminder("G1", "U1", "6/3下午8點開會", "m1")
    called = []
    monkeypatch.setattr(gemini_client, "extract_reminder",
                        lambda *a, **k: called.append(1))
    monkeypatch.setattr(main, "_quota_exhausted", lambda: True)  # 仍爆
    main._drain_pending_reminders("G1")
    assert called == [], "quota 爆時不可呼叫 extract_reminder"
    assert len(memory.list_pending_reminder_retries("G1")) == 1, "pending 保留"


def test_drain_429_releases_and_stops(temp_db, monkeypatch):
    """drain 中途又撞日額度 429 → release（退回 pending）+ 停本輪。"""
    import main
    import memory
    import gemini_client
    memory.enqueue_pending_reminder("G1", "U1", "6/3下午8點甲", "m1")
    memory.enqueue_pending_reminder("G1", "U1", "6/4下午8點乙", "m2")
    monkeypatch.setattr(gemini_client, "extract_reminder",
                        lambda *a, **k: (_ for _ in ()).throw(_quota_429_perday()))
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    monkeypatch.setattr(main, "_mark_quota_exhausted", lambda: None)
    main._drain_pending_reminders("G1")
    # 兩筆都該還在 pending（第一筆 release 退回、第二筆因 break 沒被碰）
    assert len(memory.list_pending_reminder_retries("G1")) == 2, "429 後兩筆都應保留"


def test_drain_quota_usage_over_60pct_skips(temp_db, monkeypatch):
    """reserve gate（用量>60%）→ 不抽，保額度給新訊息（GP2 D1）。"""
    import main
    import memory
    import gemini_client
    memory.enqueue_pending_reminder("G1", "U1", "6/3下午8點開會", "m1")
    called = []
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: called.append(1))
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: False)  # 用量>60%
    main._drain_pending_reminders("G1")
    assert called == [], "reserve gate 應擋下 drain"
    assert len(memory.list_pending_reminder_retries("G1")) == 1


# ── gemini_client.extract_reminder 真實 429 行為（Phase6 GP-A IMPORTANT-2）─────

def test_extract_reminder_reraises_perday_429(monkeypatch):
    """R3 基石：extract_reminder 撞真實 429 PerDay 應 bare raise 保留原字串，不靜默
    回 None。monkeypatch generate_content（非整個 extract_reminder）測真實偵測邏輯。"""
    import gemini_client

    class _FakeModels:
        def generate_content(self, **kw):
            raise Exception(
                "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
                "generate_content_free_tier_requests ... PerDay ... limit: 20"
            )

    class _FakeClient:
        models = _FakeModels()

    monkeypatch.setattr(gemini_client, "_client", _FakeClient())
    monkeypatch.setattr(gemini_client, "_track_failed_request", lambda: None)
    with pytest.raises(Exception) as ei:
        gemini_client.extract_reminder("6/3下午8點開會")
    s = str(ei.value)
    assert "429" in s and "free_tier_requests" in s, \
        f"原始 429 字串應保留供下游 _is_quota_error 判 PerDay: {s}"


def test_extract_reminder_non_429_returns_none(monkeypatch):
    """非 429 錯誤仍回 None 不 raise（不影響主流程；本次 scope 限 429 quota 丟失）。"""
    import gemini_client

    class _FakeModels:
        def generate_content(self, **kw):
            raise Exception("some unrelated parse error")

    class _FakeClient:
        models = _FakeModels()

    monkeypatch.setattr(gemini_client, "_client", _FakeClient())
    assert gemini_client.extract_reminder("6/3下午8點開會") is None


def test_add_reminder_normalizes_common_asr_errors(temp_db):
    """Common ASR/OCR slips should not be persisted into reminder actions."""
    import memory

    future = datetime.now() + timedelta(days=1)

    memory.add_reminder(
        "G1",
        "U1",
        "去教會4樓參加嗎？那小組的茶几",
        int(future.timestamp()),
        source_text="去教會4樓參加嗎？那小組的茶几",
    )

    with memory._conn() as c:
        action, source_text = c.execute(
            "SELECT action, source_text FROM reminders WHERE group_id='G1'"
        ).fetchone()
    assert action == "去教會4樓參加嗎哪小組的查經"
    assert source_text == "去教會4樓參加嗎哪小組的查經"


def test_add_reminder_persists_structured_mention_aliases(temp_db):
    import memory

    future = datetime.now() + timedelta(days=1)

    memory.add_reminder(
        "G1",
        "U_MOM",
        "正子斷層掃描當天 08:00 開始禁食",
        int(future.timestamp()),
        mention_aliases=["媽媽", "黃聖雅"],
    )

    rows = memory.list_pending_reminders_full("G1")

    assert len(rows) == 1
    assert rows[0]["mention_aliases"] == ["媽媽", "黃聖雅"]


def test_add_reminder_merges_mana_group_duplicate_with_details(temp_db):
    """Same-time Mana group reminders should merge richer details instead of duplicating."""
    import memory

    future = datetime.now() + timedelta(days=1)
    remind_at = int(future.timestamp())

    rid1 = memory.add_reminder(
        "G1",
        "U1",
        "媽媽行程：嗎哪小組（19:15-21:30）",
        remind_at,
        source_text="媽媽排程圖片：6/5 19:15-21:30 嗎哪小組",
    )
    rid2 = memory.add_reminder(
        "G1",
        "U1",
        "去教會4樓參加嗎？那小組的茶几",
        remind_at,
        source_text="明天晚上7:15我要去教會4樓參加嗎？那小組的茶几",
    )

    with memory._conn() as c:
        rows = c.execute(
            "SELECT reminder_id, action, source_text FROM reminders "
            "WHERE group_id='G1' ORDER BY reminder_id"
        ).fetchall()
    assert rid2 == rid1
    assert len(rows) == 1
    assert rows[0][1] == "媽媽行程：嗎哪小組查經（教會4樓，19:15-21:30）"
    assert "媽媽排程圖片" in rows[0][2]
    assert "教會4樓參加嗎哪小組的查經" in rows[0][2]
