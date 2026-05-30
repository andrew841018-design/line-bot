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
    import main, memory
    main._enqueue_reminder_if_candidate("6/3下午8點開會", "G1", "U1", "m1")
    rows = memory.list_pending_reminder_retries("G1")
    assert len(rows) == 1 and rows[0]["text"] == "6/3下午8點開會"


def test_enqueue_skips_non_candidate(temp_db):
    """無日期+時間 hint 的閒聊不入隊（否則每則訊息都進隊列、drain 燒 quota 在垃圾上）。"""
    import main, memory
    main._enqueue_reminder_if_candidate("今天天氣真好啊", "G1", "U1", "m1")
    assert memory.list_pending_reminder_retries("G1") == []


# ── site 2: _maybe_extract_reminder 撞 429 ───────────────────────────────────

def test_maybe_extract_perday_429_marks_and_enqueues(temp_db, monkeypatch):
    """R3: 日額度 429 → _mark_quota_exhausted + 入隊。"""
    import main, memory, gemini_client
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
    import main, memory, gemini_client
    marked = []
    monkeypatch.setattr(gemini_client, "extract_reminder",
                        lambda *a, **k: (_ for _ in ()).throw(_quota_429_transient()))
    monkeypatch.setattr(main, "_mark_quota_exhausted", lambda: marked.append(1))
    main._maybe_extract_reminder("6/3下午8點開會", "G1", "U1", "m1")
    assert marked == [], "transient 429 不可 mark 全天爆"
    assert len(memory.list_pending_reminder_retries("G1")) == 1, "transient 也入隊重抽"


# ── drain: R1 相對日期 ────────────────────────────────────────────────────────

def test_drain_relative_date_uses_message_created_at(temp_db, monkeypatch):
    """R1（GP1 critical）: drain 重抽「明天」要用訊息當時 created_at，不是 drain 當天。"""
    import main, memory, gemini_client
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
    import main, memory, gemini_client
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
    rems = memory.list_pending_reminders("G1") if hasattr(memory, "list_pending_reminders") else None
    # reminders 表應有一筆
    with memory._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM reminders WHERE action='開會'").fetchone()[0]
    assert n == 1, "drain 成功應寫進 reminders"


def test_drain_none_drops(temp_db, monkeypatch):
    """Gemini 判定非提醒(None) → dropped，不無限重抽。"""
    import main, memory, gemini_client
    memory.enqueue_pending_reminder("G1", "U1", "6/3下午8點開會", "m1")
    monkeypatch.setattr(gemini_client, "extract_reminder", lambda *a, **k: None)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)
    main._drain_pending_reminders("G1")
    assert memory.list_pending_reminder_retries("G1") == [], "None 應 dropped 離開 pending"
    with memory._conn() as c:
        st = c.execute("SELECT status FROM pending_reminder_extract WHERE message_id='m1'").fetchone()[0]
    assert st == "dropped"


def test_drain_expired_drops(temp_db, monkeypatch):
    """抽出來的時間已過期 → dropped，不寫進 reminders。"""
    import main, memory, gemini_client
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
    import main, memory, gemini_client
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
    import main, memory, gemini_client
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
    import main, memory, gemini_client
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
