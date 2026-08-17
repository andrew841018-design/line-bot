"""Reproduction tests for the 2026-05-30 code review findings.

C1 — pending JSON 兩段式 read-modify-write 繞過 pending_store 單鎖 → 並發 lost update。
I1 — _save_quota_state / gemini_client._save_usage 用固定共享 tmp 名 → 跨 process os.replace 互搶。

這些 test 在「修正前」必須 FAIL（或暴露 race window），「修正後」PASS。
Run: pytest test_pending_concurrency_fix.py -v
"""
import time
from unittest.mock import MagicMock

import pending_store


# ──────────────────────────────────────────────────────────────────────────
# C1: _save_pending_any 必須走 pending_store 單鎖 API，不可 load→save_full 兩段式
# ──────────────────────────────────────────────────────────────────────────

class _FakeMsg:
    """最小 TextMessageContent 替身。"""

    def __init__(self, msg_id: str, text: str) -> None:
        self.id = msg_id
        self.text = text
        self.quote_token = None
        self.quoted_message_id = None


class _FakeSource:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id


class _FakeEvent:
    def __init__(self, msg, user_id: str) -> None:
        self.message = msg
        self.source = _FakeSource(user_id)
        self.reply_token = "tok"


def _make_text_msg(monkeypatch):
    """讓 isinstance(msg, TextMessageContent) 成立：直接 patch main 的型別判斷不可行，
    改用真的 TextMessageContent。"""
    from linebot.v3.webhooks import TextMessageContent
    return TextMessageContent


def _mk_text(msg_id, text):
    """用真的 TextMessageContent 建 msg，確保 main 的 isinstance 分支成立。"""
    from linebot.v3.webhooks import TextMessageContent
    m = TextMessageContent.__new__(TextMessageContent)
    object.__setattr__(m, "id", msg_id)
    object.__setattr__(m, "text", text)
    object.__setattr__(m, "quoteToken", None)
    object.__setattr__(m, "quotedMessageId", None)
    return m


def test_save_pending_any_persists_via_single_lock(monkeypatch):
    """C1 行為保證：_save_pending_any 寫進去的訊息要能被 pending_store.load 讀回，
    且其他 group 既有 pending 不被動到（單鎖 add 不會整段覆寫）。

    先在 G_other 放一筆，再對 G1 _save_pending_any。修正前用整段 save_full 覆寫尚屬
    同 process 安全，但跨 process 會吃掉 G_other；改用 pending_store.add 後，G_other
    一定保留（不讀整段、不寫整段）。這條同 process 也驗證「新訊息真的有落地」。
    """
    import main
    monkeypatch.setattr(main, "_PENDING_REPLY_ENABLED", True)

    pending_store.save_full({"G_other": [{"message_id": "mo", "type": "text", "text": "別組"}]})

    m = _mk_text("m1", "我的訊息")
    ev = _FakeEvent(m, "U1")
    main._save_pending_any(ev, "G1", "U1", m)

    data = pending_store.load()
    g1_ids = {it.get("message_id") for it in data.get("G1", [])}
    assert "m1" in g1_ids, f"_save_pending_any 沒落地: {data}"
    assert "G_other" in data and any(it.get("message_id") == "mo" for it in data["G_other"]), \
        f"別組 pending 不該被動到: {data}"


def test_save_pending_any_uses_pending_store_add_unique(monkeypatch):
    """Pending writes use one locked, message-id-deduplicated RMW operation."""
    import main
    monkeypatch.setattr(main, "_PENDING_REPLY_ENABLED", True)

    pending_store.save_full({})
    called = {"add_unique": 0, "save_full": 0}

    real_add_unique = pending_store.add_unique
    real_save_full = pending_store.save_full

    def spy_add_unique(gid, entry):
        called["add_unique"] += 1
        return real_add_unique(gid, entry)

    def spy_save_full(data):
        called["save_full"] += 1
        return real_save_full(data)

    monkeypatch.setattr(pending_store, "add_unique", spy_add_unique)
    monkeypatch.setattr(pending_store, "save_full", spy_save_full)
    # _save_pending_explicit_raw 內部也呼叫 save_full → 一併攔
    monkeypatch.setattr(main, "_save_pending_explicit_raw",
                        lambda data: spy_save_full(data))

    from linebot.v3.webhooks import TextMessageContent
    m = TextMessageContent.__new__(TextMessageContent)
    object.__setattr__(m, "id", "m1")
    object.__setattr__(m, "text", "hi")
    object.__setattr__(m, "quoteToken", None)
    object.__setattr__(m, "quotedMessageId", None)
    ev = _FakeEvent(m, "U1")

    main._save_pending_any(ev, "G1", "U1", m)

    assert called["add_unique"] == 1, "應走 pending_store.add_unique（單鎖 RMW）"
    assert called["save_full"] == 0, "不可走 save_full 整段覆寫（C1 lost-update 根因）"


def test_clear_pending_explicit_uses_clear_group(monkeypatch):
    """C1：_clear_pending_explicit 應走 pending_store.clear_group（單鎖），不可整段覆寫。"""
    import main

    pending_store.save_full({"G1": [{"message_id": "m1", "type": "text", "text": "x"}]})
    called = {"clear_group": 0, "save_full": 0}
    real_clear = pending_store.clear_group

    monkeypatch.setattr(pending_store, "clear_group",
                        lambda gid: (called.__setitem__("clear_group", called["clear_group"] + 1), real_clear(gid))[1])
    monkeypatch.setattr(main, "_save_pending_explicit_raw",
                        lambda data: called.__setitem__("save_full", called["save_full"] + 1))

    main._clear_pending_explicit("G1")

    assert called["clear_group"] == 1, "應走 pending_store.clear_group"
    assert called["save_full"] == 0, "不可走整段覆寫"
    assert "G1" not in pending_store.load()


def _pending_item(message_id: str, text: str = "x", age: float = 0) -> dict:
    return {
        "message_id": message_id,
        "type": "text",
        "text": text,
        "user_id": "U1",
        "timestamp": time.time() - age,
    }


def _patch_drain_fast_path(monkeypatch, push_impl):
    import main
    monkeypatch.setattr(main, "_PENDING_REPLY_ENABLED", True)

    class _FakeApi:
        def __init__(self, _client):
            pass

        def push_message(self, req, x_line_retry_key=None):
            return push_impl(req, x_line_retry_key)

    class _FakeClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(main, "_gemini_group_messages", lambda items: [
        {"idxs": [i], "reply_to": i} for i in range(len(items))
    ])
    monkeypatch.setattr(main, "_build_group_parts", lambda items, group_id: ["prompt"])
    monkeypatch.setattr(main, "_llm_chat", lambda *a, **kw: "reply")
    monkeypatch.setattr(main, "_get_persona_notes", lambda group_id: [])
    monkeypatch.setattr(main.memory, "get_context", lambda group_id: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda group_id: [])
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **kw: None)
    monkeypatch.setattr(main, "ApiClient", _FakeClient)
    monkeypatch.setattr(main, "MessagingApi", _FakeApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: MagicMock())
    return main


def test_drain_success_preserves_same_group_concurrent_append(monkeypatch):
    """Successful drain must remove only the snapshot entries it processed.

    A webhook can append to the same group while the drain is inside LLM/LINE I/O.
    Clearing the whole group from the old snapshot silently deletes that new item.
    """
    pending_store.save_full({"G1": [_pending_item("M1")]})

    retry_keys = []

    def push_impl(_req, retry_key=None):
        retry_keys.append(retry_key)
        pending_store.add("G1", _pending_item("M_NEW", "new during push"))

    main = _patch_drain_fast_path(monkeypatch, push_impl)

    assert main._drain_pending_for_group("G1", source="test") is True
    assert retry_keys == [main._pending_push_retry_key("G1", ["M1"])]
    ids = [it["message_id"] for it in pending_store.load().get("G1", [])]
    assert ids == ["M_NEW"]


def test_drain_failure_preserves_same_group_concurrent_append(monkeypatch):
    """Failure path must not replace the whole group with stale remaining items."""
    pending_store.save_full({"G1": [_pending_item("M1"), _pending_item("M2")]})
    calls = {"n": 0}

    def push_impl(_req, _retry_key=None):
        calls["n"] += 1
        if calls["n"] == 2:
            pending_store.add("G1", _pending_item("M_NEW", "new during failure"))
            raise RuntimeError("simulated LINE failure")

    main = _patch_drain_fast_path(monkeypatch, push_impl)

    assert main._drain_pending_for_group("G1", source="test") is True
    ids = [it["message_id"] for it in pending_store.load().get("G1", [])]
    assert ids == ["M2", "M_NEW"]


def test_drop_stale_pending_preserves_concurrent_append(monkeypatch):
    """TTL cleanup must remove only stale snapshot entries, not overwrite new appends."""
    import main

    pending_store.save_full({"G1": [_pending_item("OLD", age=1000)]})

    def fake_dlq(group_id, entry, reason):
        pending_store.add(group_id, _pending_item("M_NEW", "new during dlq"))

    monkeypatch.setattr(main, "_dlq_entry", fake_dlq)

    assert main._drop_stale_pending("G1", max_age_sec=100) == 1
    ids = [it["message_id"] for it in pending_store.load().get("G1", [])]
    assert ids == ["M_NEW"]


# ──────────────────────────────────────────────────────────────────────────
# I1: state 檔原子寫不可用固定共享 tmp 名（跨 process os.replace 互搶會損毀）
# ──────────────────────────────────────────────────────────────────────────

def test_save_quota_state_unique_tmp(monkeypatch, tmp_path):
    """I1：_save_quota_state 不可用固定 '<file>.tmp'；應 mkstemp 唯一名。

    檢查方式：攔截 tempfile.mkstemp 是否被呼叫（修正後會），且固定 .tmp 不應出現殘留。
    """
    import main

    state_file = tmp_path / "quota_state.json"
    monkeypatch.setattr(main, "_QUOTA_STATE_FILE", str(state_file))

    mkstemp_calls = {"n": 0}
    import tempfile as _tf
    real_mkstemp = _tf.mkstemp

    def spy_mkstemp(*a, **kw):
        mkstemp_calls["n"] += 1
        return real_mkstemp(*a, **kw)

    monkeypatch.setattr(_tf, "mkstemp", spy_mkstemp)

    main._quota_exhausted_until_ts = time.time() + 3600
    main._save_quota_state()

    assert state_file.exists(), "state 檔應寫成功"
    # 修正後：用 mkstemp 唯一 tmp
    assert mkstemp_calls["n"] >= 1, "應使用 tempfile.mkstemp 產唯一 tmp（避免跨 process .tmp 互搶）"
    # 固定共享 .tmp 不應殘留
    assert not (tmp_path / "quota_state.json.tmp").exists(), "不應留固定共享 .tmp"


def test_save_usage_unique_tmp(monkeypatch, tmp_path):
    """I1：gemini_client._save_usage 同樣不可用固定 '<file>.tmp'。"""
    import gemini_client

    usage_file = tmp_path / "gemini_usage.json"
    monkeypatch.setattr(gemini_client, "_USAGE_FILE", str(usage_file))

    mkstemp_calls = {"n": 0}
    import tempfile as _tf
    real_mkstemp = _tf.mkstemp

    def spy_mkstemp(*a, **kw):
        mkstemp_calls["n"] += 1
        return real_mkstemp(*a, **kw)

    monkeypatch.setattr(_tf, "mkstemp", spy_mkstemp)

    gemini_client._save_usage({"date": "2026-05-30", "tokens": 1, "requests": 1})

    assert usage_file.exists()
    assert mkstemp_calls["n"] >= 1, "應使用 tempfile.mkstemp 產唯一 tmp"
    assert not (tmp_path / "gemini_usage.json.tmp").exists(), "不應留固定共享 .tmp"
