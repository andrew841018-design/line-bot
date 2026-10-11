"""每日記憶累積 job（2026-10-11）：jobs/daily_memory.py。

模型一律是假的（照對話紀錄的編號回 JSON），嵌入模型也是假的；名字與內容全是合成的。
raw_messages 直接寫進每個測試自己的暫存 DB，created_at 由測試指定。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import fact_store
import memory
import member_labels
from jobs import daily_memory as dm

G = "G_DAILY_MEMORY"
T0 = 1_800_000_000  # 合成的時間起點
NOW = T0 + 86_400
ORIGINAL_GATE = dm._gate  # 下面的 fixture 會換掉它
_ROW_RE = re.compile(r"^#(\d+)【(可引用|不可引用)】\n([^\n]*?)：([^\n]*)", re.M)


class FakeEmbedder:
    """每段不同的文字各佔一個軸：只有完全相同才會被當成重複。"""

    def __init__(self):
        self.axes: dict[str, int] = {}

    def encode(self, texts, **_kwargs):
        out = []
        for text in texts:
            index = self.axes.setdefault(text, len(self.axes))
            vector = [0.0] * 256
            vector[index % 256] = 1.0
            out.append(vector)
        return out


class FakeModel:
    """看到可引用、內容在 facts 裡的那一則，就回那一則的事實（person＝稱呼、src＝編號）。"""

    def __init__(self, facts=None, *, errors=None, reply=None):
        self.facts = dict(facts or {})
        self.errors = dict(errors or {})  # 第幾次呼叫（從 1 起）→ 例外
        self.reply = reply
        self.prompts: list[tuple[str, str, list[str]]] = []

    def __call__(self, backend, system, user, persons):
        self.prompts.append((system, user, list(persons)))
        error = self.errors.get(len(self.prompts))
        if error is not None:
            raise error
        if self.reply is not None:
            return self.reply(user)
        items = []
        for number, mark, label, text in _ROW_RE.findall(user):
            if mark == "可引用" and text in self.facts:
                items.append({"person": label, "fact": self.facts[text], "src": int(number)})
        return json.dumps(items, ensure_ascii=False)


@pytest.fixture(autouse=True)
def job(tmp_path, monkeypatch):
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"U_A": "成員甲", "U_B": "成員乙"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(tmp_path / "no_roles.json"))
    monkeypatch.setattr(member_labels, "_food_alias", lambda _uid: "")
    monkeypatch.setenv("LINE_BOT_DAILY_MEMORY", "1")
    monkeypatch.setattr(dm.settings, "family_group_id", G)
    monkeypatch.setattr(dm, "_gate", lambda _backend: "")
    monkeypatch.setattr(dm, "_load_embedder", FakeEmbedder)
    model = FakeModel({"我住台中": "住在台中", "我喜歡爬山": "喜歡爬山", "我每週日去游泳": "每週日去游泳"})
    monkeypatch.setattr(dm, "_call_model", model)
    return SimpleNamespace(model=model, aliases=aliases)


def _raw(message_id, user_id, text, created_at, group_id=G):
    with memory._conn() as c:
        c.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (group_id, message_id, user_id, text, created_at),
        )


def _write_state(created_at=T0, message_id="", poison=None):
    path = dm._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({G: {"cursor": {"created_at": created_at, "message_id": message_id}, "poison": poison or {}}}),
        encoding="utf-8",
    )


def _state() -> dict:
    return json.loads(dm._state_path().read_text(encoding="utf-8"))[G]


def _cursor() -> tuple[int, str]:
    cursor = _state()["cursor"]
    return cursor["created_at"], cursor["message_id"]


def _outcome() -> dict:
    return json.loads(dm._outcome_path().read_text(encoding="utf-8"))


# ── 開關、設定、狀態 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, "0"])
def test_disabled_does_nothing_and_says_so(monkeypatch, job, value):
    if value is None:
        monkeypatch.delenv("LINE_BOT_DAILY_MEMORY", raising=False)
    else:
        monkeypatch.setenv("LINE_BOT_DAILY_MEMORY", value)
    _raw("m1", "U_A", "我住台中", T0 + 10)
    outcome = dm.run(now=NOW)
    assert (outcome.status, outcome.exit_code) == ("disabled", 0)
    assert _outcome()["status"] == "disabled"
    assert job.model.prompts == [] and memory.list_fact_rows(G) == []


def test_an_unknown_mode_or_backend_is_a_config_error(monkeypatch):
    monkeypatch.setenv("LINE_BOT_DAILY_MEMORY", "yes")
    assert dm.run(now=NOW).exit_code == 2
    monkeypatch.setenv("LINE_BOT_DAILY_MEMORY", "1")
    monkeypatch.setenv("LINE_BOT_DAILY_MEMORY_BACKEND", "other")
    assert (dm.run(now=NOW).status, _outcome()["exit_code"]) == ("config_invalid", 2)


def test_no_family_group_is_a_config_error(monkeypatch, job):
    monkeypatch.setattr(dm.settings, "family_group_id", "")
    outcome = dm.run(now=NOW)
    assert (outcome.status, outcome.exit_code) == ("config_missing", 2)
    assert job.model.prompts == []


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"G_OTHER": {}}), json.dumps({G: {"cursor": {}}})])
def test_a_missing_or_broken_state_never_starts_from_zero(job, content):
    _raw("m1", "U_A", "我住台中", T0 + 10)
    if content is not None:
        dm._state_path().parent.mkdir(parents=True, exist_ok=True)
        dm._state_path().write_text(content, encoding="utf-8")
    outcome = dm.run(now=NOW)
    assert (outcome.status, outcome.exit_code) == ("state_missing", 2)
    assert job.model.prompts == [] and memory.list_fact_rows(G) == []


def test_a_second_copy_skips_while_one_is_running(job):
    _write_state()
    lock = dm._lock_dir() / "daily_memory.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        outcome = dm.run(now=NOW)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert (outcome.status, outcome.exit_code) == ("skipped_locked", 0)
    assert job.model.prompts == []


def test_no_family_name_list_means_no_run(job):
    _write_state()
    job.aliases.unlink()
    outcome = dm.run(now=NOW)
    assert (outcome.status, outcome.exit_code) == ("names_unavailable", 2)
    assert job.model.prompts == []


def test_no_embedder_means_no_writes_and_no_progress(monkeypatch, job):
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    monkeypatch.setattr(dm, "_load_embedder", lambda: None)
    outcome = dm.run(now=NOW)
    assert (outcome.status, outcome.exit_code, outcome.embedder_mode) == ("embedder_unavailable", 2, "unavailable")
    assert job.model.prompts == [] and _cursor() == (T0, "")


# ── 抽取、寫入、進度 ───────────────────────────────────────────────────────


def test_facts_are_stored_under_each_speaker_and_the_cursor_moves(job):
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    _raw("m2", "__bot__", "了解", T0 + 11)
    _raw("m3", "U_B", "我喜歡爬山", T0 + 12)
    _raw("m4", "U_NOBODY", "我每週日去游泳", T0 + 13)
    _raw("m5", "U_A", "剛到", NOW - 60)  # 10 分鐘內的還不讀
    outcome = dm.run(now=NOW)
    assert (outcome.status, outcome.exit_code, outcome.added, outcome.chunks_ok) == ("ok", 0, 2, 1)
    assert sorted(memory.list_fact_rows(G)) == [("U_A", "成員甲：住在台中"), ("U_B", "成員乙：喜歡爬山")]
    assert _cursor() == (T0 + 13, "m4")
    (_system, user, persons) = job.model.prompts[0]
    assert persons == ["成員乙", "成員甲"]
    assert "#2【不可引用】\n咪寶：了解" in user
    assert "#4【不可引用】\n（不確定是誰）：我每週日去游泳" in user
    assert "剛到" not in user
    ledger = fact_store._ledger_path().read_text(encoding="utf-8").splitlines()
    assert len(ledger) == 2 and all(json.loads(line)["source"] == "daily" for line in ledger)


def test_running_twice_over_the_same_messages_adds_nothing_new(job):
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    _raw("m2", "U_B", "我喜歡爬山", T0 + 11)
    assert dm.run(now=NOW).added == 2
    _write_state()  # 進度被退回去，同一段再跑一次
    outcome = dm.run(now=NOW)
    assert (outcome.added, outcome.dup) == (0, 2)
    assert len(memory.list_fact_rows(G)) == 2


def test_a_missed_day_is_caught_up_next_time(monkeypatch, job):
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    monkeypatch.setattr(dm, "_gate", lambda _backend: "quota")
    stopped = dm.run(now=NOW)
    assert (stopped.status, stopped.stop_reason, stopped.exit_code) == ("stopped", "quota", 0)
    assert _cursor() == (T0, "") and job.model.prompts == []
    _raw("m2", "U_B", "我喜歡爬山", NOW + 10)
    monkeypatch.setattr(dm, "_gate", lambda _backend: "")
    caught_up = dm.run(now=NOW + 86_400)
    assert caught_up.added == 2 and _cursor() == (NOW + 10, "m2")


def test_each_chunk_moves_the_cursor_and_a_transient_error_stops_there(monkeypatch, job):
    monkeypatch.setattr(dm, "CHUNK_MAX_ROWS", 1)
    job.model.errors = {2: dm.TransientError("rate_limited")}
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    _raw("m2", "U_B", "我喜歡爬山", T0 + 11)
    _raw("m3", "U_A", "我每週日去游泳", T0 + 12)
    outcome = dm.run(now=NOW)
    assert (outcome.status, outcome.stop_reason) == ("stopped", "rate_limited")
    assert (outcome.chunks_ok, outcome.chunks_failed, outcome.chunks_pending) == (1, 1, 2)
    assert _cursor() == (T0 + 10, "m1") and _state()["poison"] == {}
    assert memory.list_fact_rows(G) == [("U_A", "成員甲：住在台中")]
    assert outcome.cursor_lag_h == round((NOW - (T0 + 11)) / 3600, 1)


def test_same_second_messages_across_a_chunk_boundary(monkeypatch, job):
    monkeypatch.setattr(dm, "CHUNK_MAX_ROWS", 2)
    job.model.errors = {2: dm.TransientError("unavailable")}
    _write_state()
    _raw("m1", "U_A", "早安", T0 + 10)
    _raw("m2", "U_A", "我住台中", T0 + 11)
    _raw("m3", "U_B", "我喜歡爬山", T0 + 11)  # 同一秒，落在下一塊
    dm.run(now=NOW)
    assert _cursor() == (T0 + 11, "m2")
    job.model.errors = {}
    dm.run(now=NOW)
    assert _cursor() == (T0 + 11, "m3")
    assert sorted(memory.list_fact_rows(G)) == [("U_A", "成員甲：住在台中"), ("U_B", "成員乙：喜歡爬山")]


def test_same_second_messages_across_two_runs(monkeypatch, job):
    monkeypatch.setattr(dm, "CHUNK_MAX_ROWS", 1)
    monkeypatch.setattr(dm, "MAX_CHUNKS_PER_RUN", 1)
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    _raw("m2", "U_B", "我喜歡爬山", T0 + 10)
    first = dm.run(now=NOW)
    assert (first.status, first.chunks_pending) == ("ok", 1) and _cursor() == (T0 + 10, "m1")
    dm.run(now=NOW)
    assert _cursor() == (T0 + 10, "m2")
    assert len(memory.list_fact_rows(G)) == 2


def test_a_chunk_that_keeps_failing_is_skipped_after_three_runs(monkeypatch, job):
    monkeypatch.setattr(dm, "CHUNK_MAX_ROWS", 1)
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    _raw("m2", "U_B", "我喜歡爬山", T0 + 11)
    job.model.reply = lambda user: "抱歉，這段我不能處理" if "我住台中" in user else json.dumps(
        [{"person": "成員乙", "fact": "喜歡爬山", "src": 1}], ensure_ascii=False
    )
    key = f"{T0 + 10}:m1"
    for expected in (1, 2):
        outcome = dm.run(now=NOW)
        assert (outcome.status, outcome.stop_reason) == ("stopped", "permanent:unparsable")
        assert _state()["poison"] == {key: expected} and _cursor() == (T0, "")
    outcome = dm.run(now=NOW)
    assert (outcome.status, outcome.chunks_poison, outcome.chunks_ok) == ("ok", 1, 1)
    assert _state()["poison"] == {} and _cursor() == (T0 + 11, "m2")
    assert memory.list_fact_rows(G) == [("U_B", "成員乙：喜歡爬山")]


def test_facts_must_come_from_the_persons_own_citable_message(job):
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    _raw("m2", "U_B", "我喜歡爬山", T0 + 11)
    _raw("m3", "__bot__", "成員甲也喜歡游泳吧", T0 + 12)
    _raw("m4", "U_A", "我喜歡游泳，" + "還有很多很多話" * 30, T0 + 13)  # 超過 200 字：只當上下文
    _raw("m5", "U_A", "轉貼\n第二行\n第三行\n我喜歡唱歌", T0 + 14)  # 超過 3 行：只當上下文
    job.model.reply = lambda _user: json.dumps([
        {"person": "成員乙", "fact": "住在台中", "src": 1},  # 別人的那一則
        {"person": "成員甲", "fact": "喜歡游泳", "src": 3},  # bot 的那一則
        {"person": "成員甲", "fact": "喜歡游泳", "src": 4},  # 不可引用
        {"person": "成員甲", "fact": "喜歡唱歌", "src": 5},  # 不可引用
        {"person": "成員甲", "fact": "喜歡游泳", "src": 1},  # 和原文沒有共用的字
        {"person": "成員丙", "fact": "住在台中", "src": 1},  # 不是這一塊的人
        {"person": "成員甲", "fact": "住在台中", "src": 9},  # 沒有這一則
        {"person": "成員甲", "fact": "住在台中", "src": "1"},  # 通過（src 寫成字串也認）
    ], ensure_ascii=False)
    outcome = dm.run(now=NOW)
    assert (outcome.added, outcome.provenance_mismatch) == (1, 7)
    assert memory.list_fact_rows(G) == [("U_A", "成員甲：住在台中")]


def test_commands_media_and_bare_links_never_reach_the_model(job):
    _write_state()
    for index, text in enumerate([
        "/記住 住台中", "／忘記 台中", "[圖片]", "[檔案: 合成.pdf]", "[StickerMessageContent]",
        "https://example.com/x", "  https://example.com/a  https://example.com/b ",
    ]):
        _raw(f"d{index}", "U_A", text, T0 + 10 + index)
    outcome = dm.run(now=NOW)
    assert job.model.prompts == [] and outcome.status == "ok"
    assert _cursor() == (T0 + 16, "d6")  # 進度照樣越過它們
    _raw("m1", "U_A", "我住台中 https://example.com/x", NOW + 10)
    _raw("m2", "U_B", "我喜歡爬山", NOW + 11)
    _raw("m3", "U_B", "電話 0900000000 記一下", NOW + 12)
    dm.run(now=NOW + 3600)
    (_system, user, _persons) = job.model.prompts[0]
    assert "#1【不可引用】\n成員甲：我住台中 [連結]" in user  # 有網址：只當上下文、網址遮掉
    assert "example.com" not in user and "0900000000" not in user and "[號碼]" in user


def test_a_trimmed_gap_is_reported_as_a_count(monkeypatch, job):
    _write_state()
    monkeypatch.setattr(memory, "raw_messages_stats", lambda _g: (2000, T0 + 3600))
    outcome = dm.run(now=NOW)
    assert (outcome.gap_detected, outcome.gap_seconds) == (True, 3600)
    monkeypatch.setattr(memory, "raw_messages_stats", lambda _g: (1999, T0 + 3600))
    assert dm.run(now=NOW).gap_detected is False


def test_the_outcome_has_counts_only(job):
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    _raw("m2", "U_A", "在吃藥", T0 + 11)
    job.model.facts["在吃藥"] = "在吃藥"
    dm.run(now=NOW)
    path = dm._outcome_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    for private in ("台中", "藥", "成員甲", "U_A", G):
        assert private not in text
    outcome = json.loads(text)
    assert outcome["status"] == "ok" and outcome["added"] == 1 and outcome["rejected"] == {"private": 1}
    assert outcome["last_success_at"] == NOW and outcome["people_near_cap"] == 0
    for key, value in outcome.items():
        if key in ("rejected", "poison_reasons"):
            assert all(isinstance(v, int) for v in value.values())
        elif isinstance(value, str):
            assert key in {"status", "mode", "backend", "stop_reason", "embedder_mode"}


def test_a_failed_run_keeps_the_last_success_time(monkeypatch, job):
    _write_state()
    dm.run(now=NOW)
    monkeypatch.setattr(dm, "_gate", lambda _backend: "quota")
    _raw("m1", "U_A", "我住台中", NOW + 10)
    stopped = dm.run(now=NOW + 7200)
    assert stopped.status == "stopped" and stopped.last_success_at == NOW


def test_people_near_the_cap_are_counted_without_names(monkeypatch, job):
    monkeypatch.setenv("LINE_BOT_FACTS_PER_PERSON_MAX", "2")
    memory.add_fact(G, "成員甲：喜歡爬山", user_id="U_A")
    memory.add_fact(G, "成員甲：喜歡游泳", user_id="U_A")
    _write_state()
    assert dm.run(now=NOW).people_near_cap == 1


def test_a_ledger_failure_stops_without_moving_on(monkeypatch, job):
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)

    def broken(*_a, **_k):
        raise OSError("synthetic")

    monkeypatch.setattr(fact_store, "_append_ledger", broken)
    outcome = dm.run(now=NOW)
    assert (outcome.status, outcome.stop_reason, outcome.error) == ("stopped", "ledger_error", 1)
    assert _cursor() == (T0, "") and memory.list_fact_rows(G) == []


def test_dry_run_writes_a_preview_and_nothing_else(monkeypatch, job):
    monkeypatch.setenv("LINE_BOT_DAILY_MEMORY", "dry_run")
    memory.add_fact(G, "成員乙：喜歡爬山", user_id="U_B")
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    _raw("m2", "U_B", "我喜歡爬山", T0 + 11)
    _raw("m3", "U_A", "在吃藥", T0 + 12)
    job.model.facts["在吃藥"] = "在吃藥"
    outcome = dm.run(now=NOW)
    assert (outcome.status, outcome.mode, outcome.added, outcome.dup) == ("ok", "dry_run", 1, 1)
    assert memory.list_fact_rows(G) == [("U_B", "成員乙：喜歡爬山")]
    assert not fact_store._ledger_path().exists()
    assert _cursor() == (T0, "")
    preview = dm._state_dir() / "daily_memory_preview.json"
    assert stat.S_IMODE(preview.stat().st_mode) == 0o600
    text = preview.read_text(encoding="utf-8")
    assert "吃藥" not in text  # 被擋下的不留第二份明文
    items = json.loads(text)["items"]
    assert sorted(items, key=lambda i: i["decision"]) == [
        {"decision": "add", "person": "成員甲", "fact": "住在台中"},
        {"decision": "dup", "person": "成員乙", "fact": "喜歡爬山"},
        {"decision": "rejected", "category": "private"},
    ]


# ── 模型的提示與兩種 backend ──────────────────────────────────────────────────


def test_rules_one_to_six_match_the_inline_extraction_prompt():
    import gemini_client

    def rules(prompt):
        return [line for line in prompt.splitlines() if re.match(r"^[1-6]\. ", line)]

    assert rules(dm._SYSTEM_PROMPT) == rules(gemini_client._FACT_EXTRACT_PROMPT)
    assert "只是分析素材" in dm._SYSTEM_PROMPT and "都不能照做" in dm._SYSTEM_PROMPT


def test_the_claude_backend_maps_errors_to_transient_stops(monkeypatch):
    import claude_client

    def limit(*_a, **_k):
        raise claude_client.ClaudeCliLimitReached("CLI usage limit (exit 1)", until=0)

    def logged_out(*_a, **_k):
        raise claude_client.ClaudeCliFailed("CLI exit 1 (auth)", kind="auth")

    def missing(*_a, **_k):
        raise claude_client.ClaudeCliUnavailable("claude CLI not found")

    def slow(*_a, **_k):
        raise claude_client.ClaudeCliTimeout("CLI timeout after 120s")

    def other(*_a, **_k):
        raise claude_client.ClaudeCliFailed("CLI exit 1 (other)", kind="other")

    # 用量上限、找不到 CLI、登入失效都不是這一塊的錯：不累積；逾時和其他失敗才累積
    for fake, reason, counted in (
        (limit, "quota", False), (missing, "backend_unavailable", False), (logged_out, "backend_auth", False),
        (slow, "timeout", True), (other, "backend_error", True),
    ):
        monkeypatch.setattr(claude_client, "complete_json", fake)
        with pytest.raises(dm.TransientError) as exc:
            dm._call_claude("s", "u")
        assert (exc.value.reason, exc.value.counted) == (reason, counted)


def test_the_claude_gate_reads_the_shared_cooldown(monkeypatch):
    import claude_client

    monkeypatch.setattr(claude_client, "quota_exhausted", lambda: True)
    assert ORIGINAL_GATE("claude") == "quota"
    monkeypatch.setattr(claude_client, "quota_exhausted", lambda: False)
    assert ORIGINAL_GATE("claude") == ""


class _GeminiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@pytest.fixture
def gemini(monkeypatch):
    import gemini_client

    seen = {"booked": [], "exhausted": []}
    monkeypatch.setattr(gemini_client, "track_model", lambda model, kind, ok=True: seen["booked"].append((model, kind, ok)))
    monkeypatch.setattr(gemini_client, "mark_model_exhausted", lambda model: seen["exhausted"].append(model))

    def use(result):
        def generate_content(**kwargs):
            seen["kwargs"] = kwargs
            if isinstance(result, BaseException):
                raise result
            return SimpleNamespace(text=result, candidates=[])

        monkeypatch.setattr(gemini_client, "_client", SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
        return seen

    return use


def test_the_gemini_backend_asks_for_a_schema_with_the_speakers(gemini, monkeypatch):
    monkeypatch.setenv("LINE_BOT_DAILY_MEMORY_MODEL", "gemini-test-lite")
    seen = gemini('[{"person": "成員甲", "fact": "住在台中", "src": 1}]')
    text = dm._call_gemini("SYS", "USER", ["成員甲"], dm._gemini_model())
    assert json.loads(text)[0]["src"] == 1
    config = seen["kwargs"]["config"]
    assert seen["kwargs"]["model"] == "gemini-test-lite"
    assert config.temperature == 0 and config.response_mime_type == "application/json"
    assert config.response_schema["items"]["properties"]["person"]["enum"] == ["成員甲"]
    assert config.http_options.timeout == int(dm.MODEL_TIMEOUT_S * 1000)
    assert seen["booked"] == [("gemini-test-lite", "extract", True)]


@pytest.mark.parametrize(
    ("error", "kind", "reason"),
    [
        (_GeminiError(429, "429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier"), dm.TransientError, "quota"),
        (_GeminiError(429, "429 RESOURCE_EXHAUSTED per minute"), dm.TransientError, "rate_limited"),
        (_GeminiError(503, "503 UNAVAILABLE"), dm.TransientError, "unavailable"),
        (_GeminiError(504, "504 DEADLINE_EXCEEDED"), dm.TransientError, "timeout"),
        (_GeminiError(400, "400 INVALID_ARGUMENT request too large"), dm.PermanentError, "request_rejected"),
        (_GeminiError(400, "400 INVALID_ARGUMENT API key not valid"), dm.TransientError, "backend_error"),
        (_GeminiError(404, "404 NOT_FOUND model"), dm.TransientError, "backend_error"),
    ],
)
def test_gemini_errors_are_sorted_into_transient_and_permanent(gemini, error, kind, reason):
    seen = gemini(error)
    with pytest.raises(kind) as exc:
        dm._call_gemini("SYS", "USER", ["成員甲"], "gemini-test-lite")
    assert exc.value.reason == reason
    if kind is dm.TransientError:
        assert exc.value.counted == (reason == "timeout")  # 只有逾時可能是這一塊的錯
    assert seen["booked"] == [("gemini-test-lite", "extract", False)]
    assert seen["exhausted"] == (["gemini-test-lite"] if reason == "quota" else [])


def test_an_empty_gemini_answer_is_a_permanent_chunk_error(gemini):
    gemini("")
    with pytest.raises(dm.PermanentError):
        dm._call_gemini("SYS", "USER", ["成員甲"], "gemini-test-lite")


def test_the_gemini_gate_fails_closed_without_quota_info(monkeypatch):
    import gemini_client

    monkeypatch.setattr(gemini_client, "get_gemini_quota_info", lambda: None)
    assert ORIGINAL_GATE("gemini") == "quota_unknown"
    monkeypatch.setattr(gemini_client, "get_gemini_quota_info", lambda: {"used_requests": 0})
    monkeypatch.setattr(gemini_client, "model_exhausted", lambda _model: True)
    assert ORIGINAL_GATE("gemini") == "quota"
    monkeypatch.setattr(gemini_client, "model_exhausted", lambda _model: False)
    assert ORIGINAL_GATE("gemini") == ""


# ── 執行環境 ───────────────────────────────────────────────────────────────


def test_main_sets_a_private_umask_and_offline_model_loading(monkeypatch):
    monkeypatch.delenv("LINE_BOT_DAILY_MEMORY", raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")
    previous = os.umask(0o022)
    try:
        assert dm.main([]) == 0
        assert os.umask(0o022) == 0o077
    finally:
        os.umask(previous)
    assert os.environ["HF_HUB_OFFLINE"] == "1" and os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_unexpected_errors_exit_1_and_log_only_the_type(monkeypatch, job, caplog):
    real_now = int(time.time())  # main() 用真的時間
    _write_state(created_at=real_now - 7200)
    _raw("m1", "U_A", "我住台中", real_now - 3600)

    def boom(*_a, **_k):
        raise RuntimeError("合成私人片段")

    monkeypatch.setattr(fact_store, "add_person_facts", boom)
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")
    previous = os.umask(0o022)
    try:
        assert dm.main([]) == 1
    finally:
        os.umask(previous)
    assert "合成私人片段" not in caplog.text and "RuntimeError" in caplog.text
    assert _outcome()["status"] == "error"


def test_the_job_never_imports_main(tmp_path):
    repo = Path(dm.__file__).resolve().parent.parent
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "LINE_CHANNEL_SECRET": "dummy_secret_32bytes_padding000",
        "LINE_CHANNEL_ACCESS_TOKEN": "dummy",
        "GEMINI_API_KEY": "dummy",
        "SQLITE_PATH": str(tmp_path / "isolated.db"),
        "LINE_BOT_DAILY_MEMORY": "0",
        "LINE_BOT_DAILY_MEMORY_STATE_DIR": str(tmp_path / "state"),
        "LINE_BOT_FACT_LOCK_DIR": str(tmp_path / "locks"),
    }
    code = (
        "import sys\n"
        "from jobs import daily_memory\n"
        "import fact_store, fact_filters, fact_hash, member_labels\n"
        "assert 'main' not in sys.modules, 'main imported'\n"
        "assert daily_memory.run().status == 'disabled'\n"
        "assert 'main' not in sys.modules, 'main imported by run'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env={**env, "PYTHONPATH": str(repo)},
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]


def test_the_documented_first_cursor_is_the_end_of_10_10_in_taipei():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    end_of_day = datetime(2026, 10, 10, 23, 59, 59, tzinfo=ZoneInfo("Asia/Taipei"))
    assert dm.INITIAL_CURSOR_TS == int(end_of_day.timestamp())


# ── 修正第一輪 #5：/忘記、/清除記憶 當晚不能又被學回去 ───────────────────────────


@pytest.mark.parametrize("command", ["/忘記 台中", "／忘記　台中", "/忘記 台中市的事", "/清除記憶"])
def test_a_fact_forgotten_later_in_the_same_window_is_not_added(job, command):
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    _raw("m2", "U_B", "我喜歡爬山", T0 + 11)
    _raw("m3", "U_B", command, T0 + 12)
    outcome = dm.run(now=NOW)
    if command == "/忘記 台中市的事":  # 關鍵字不在事實本體裡：照常新增
        assert (outcome.added, outcome.forgotten) == (2, 0)
        return
    expected = 2 if command == "/清除記憶" else 1
    assert outcome.forgotten == expected
    rows = memory.list_fact_rows(G)
    assert ("U_A", "成員甲：住在台中") not in rows
    assert len(rows) == 2 - expected
    assert _cursor() == (T0 + 12, "m3")  # 指令列照樣越過，不送模型
    assert "忘記" not in job.model.prompts[0][1] and "清除記憶" not in job.model.prompts[0][1]


def test_a_fact_said_after_forget_is_added_normally(job):
    _write_state()
    _raw("m1", "U_A", "/忘記 台中", T0 + 10)
    _raw("m2", "U_B", "/清除記憶", T0 + 11)
    _raw("m3", "U_A", "我住台中", T0 + 12)  # 指令在前、說話在後：照常新增
    _raw("m4", "U_B", "我喜歡爬山", T0 + 13)
    outcome = dm.run(now=NOW)
    assert (outcome.added, outcome.forgotten) == (2, 0)


def test_forgotten_facts_count_in_dry_run_without_text(monkeypatch, job):
    monkeypatch.setenv("LINE_BOT_DAILY_MEMORY", "dry_run")
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    _raw("m2", "U_A", "/忘記 台中", T0 + 11)
    outcome = dm.run(now=NOW)
    assert (outcome.forgotten, outcome.added) == (1, 0)
    text = dm._preview_path().read_text(encoding="utf-8")
    assert "台中" not in text and json.loads(text)["items"] == [{"decision": "forgotten"}]


# ── 修正第一輪 #6：預覽 ────────────────────────────────────────────────────


def test_provenance_mismatches_leave_no_text_in_the_preview(monkeypatch, job):
    monkeypatch.setenv("LINE_BOT_DAILY_MEMORY", "dry_run")
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    job.model.reply = lambda _user: json.dumps([{"person": "成員甲", "fact": "常常咳嗽", "src": 1}], ensure_ascii=False)
    dm.run(now=NOW)
    text = dm._preview_path().read_text(encoding="utf-8")
    assert "咳嗽" not in text and json.loads(text)["items"] == [{"decision": "provenance_mismatch"}]


@pytest.mark.parametrize("mode", ["1", "0"])
def test_any_run_that_is_not_a_dry_run_deletes_an_old_preview(monkeypatch, job, mode):
    monkeypatch.setenv("LINE_BOT_DAILY_MEMORY", "dry_run")
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    dm.run(now=NOW)
    assert dm._preview_path().exists()
    monkeypatch.setenv("LINE_BOT_DAILY_MEMORY", mode)
    dm.run(now=NOW)
    assert not dm._preview_path().exists()


# ── 修正第一輪 #3：逾時不能讓 job 永遠卡住；不呼叫模型的塊不算上限 ───────────────


def test_a_chunk_that_times_out_five_times_is_skipped(monkeypatch, job):
    monkeypatch.setattr(dm, "CHUNK_MAX_ROWS", 1)
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    _raw("m2", "U_B", "我喜歡爬山", T0 + 11)

    def reply(user):
        if "我住台中" in user:
            raise dm.TransientError("timeout", counted=True)
        return json.dumps([{"person": "成員乙", "fact": "喜歡爬山", "src": 1}], ensure_ascii=False)

    job.model.reply = reply
    key = f"{T0 + 10}:m1"
    for expected in range(1, dm.TRANSIENT_SKIP_AFTER):
        outcome = dm.run(now=NOW)
        assert (outcome.status, outcome.stop_reason) == ("stopped", "timeout")
        assert _state()["transient"] == {key: expected} and _cursor() == (T0, "")
    outcome = dm.run(now=NOW)
    assert (outcome.status, outcome.chunks_poison, outcome.poison_reasons) == ("ok", 1, {"transient_repeat": 1})
    assert _state()["transient"] == {} and _cursor() == (T0 + 11, "m2")
    assert memory.list_fact_rows(G) == [("U_B", "成員乙：喜歡爬山")]


@pytest.mark.parametrize("reason", ["quota", "backend_unavailable", "backend_auth", "rate_limited"])
def test_errors_that_are_not_the_chunk_s_fault_never_count(job, reason):
    job.model.errors = {n: dm.TransientError(reason) for n in range(1, 10)}
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    for _ in range(dm.TRANSIENT_SKIP_AFTER + 1):
        outcome = dm.run(now=NOW)
        assert (outcome.status, outcome.stop_reason) == ("stopped", reason)
    assert _state().get("transient", {}) == {} and _state().get("poison", {}) == {} and _cursor() == (T0, "")


def test_the_quota_gate_never_counts_against_a_chunk(monkeypatch, job):
    monkeypatch.setattr(dm, "_gate", lambda _backend: "quota")
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    for _ in range(dm.TRANSIENT_SKIP_AFTER + 1):
        assert dm.run(now=NOW).stop_reason == "quota"
    assert _state().get("transient", {}) == {} and job.model.prompts == []


def test_chunks_without_a_citable_row_do_not_use_up_the_per_run_cap(monkeypatch, job):
    monkeypatch.setattr(dm, "CHUNK_MAX_ROWS", 1)
    monkeypatch.setattr(dm, "MAX_CHUNKS_PER_RUN", 1)
    _write_state()
    _raw("m1", "__bot__", "早安", T0 + 10)
    _raw("m2", "U_NOBODY", "大家早", T0 + 11)
    _raw("m3", "U_A", "我住台中", T0 + 12)
    _raw("m4", "U_B", "我喜歡爬山", T0 + 13)
    outcome = dm.run(now=NOW)
    assert len(job.model.prompts) == 1 and outcome.chunks_pending == 1
    assert _cursor() == (T0 + 12, "m3")
    assert memory.list_fact_rows(G) == [("U_A", "成員甲：住在台中")]


# ── 修正第一輪 #4：金鑰遺失要停下來 ─────────────────────────────────────────


def test_a_lost_hash_key_stops_the_job(job):
    import fact_hash

    memory.fact_hash_key()  # 已經用過：DB 記下指紋
    fact_hash.key_path().unlink()
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    outcome = dm.run(now=NOW)
    assert (outcome.status, outcome.exit_code) == ("hash_key_mismatch", 2)
    assert job.model.prompts == [] and memory.list_fact_rows(G) == [] and _cursor() == (T0, "")
    assert not fact_hash.key_path().exists()


def test_a_key_replaced_mid_run_stops_before_writing(monkeypatch, job):
    import fact_hash

    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    real = dm._call_model

    def swap_key_then_answer(*args):
        fact_hash.key_path().write_bytes(b"x" * 32)
        return real(*args)

    monkeypatch.setattr(dm, "_call_model", swap_key_then_answer)
    outcome = dm.run(now=NOW)
    assert (outcome.status, outcome.exit_code) == ("hash_key_mismatch", 2)
    assert memory.list_fact_rows(G) == [] and _cursor() == (T0, "")


# ── 修正第一輪小問題 ───────────────────────────────────────────────────────


def test_a_broken_dedupe_model_reports_embedder_unavailable(monkeypatch, job):
    class Broken:
        def encode(self, *_a, **_k):
            raise RuntimeError("synthetic")

    monkeypatch.setattr(dm, "_load_embedder", Broken)
    _write_state()
    _raw("m1", "U_A", "我住台中", T0 + 10)
    outcome = dm.run(now=NOW)
    assert (outcome.status, outcome.exit_code, outcome.embedder_mode) == ("embedder_unavailable", 2, "unavailable")
    assert memory.list_fact_rows(G) == [] and _cursor() == (T0, "")


@pytest.mark.parametrize(("src", "kept"), [(1, True), ("1", True), ("#1", True), (" # 1 ", True), ("²", False), ("abc", False), (1.0, False), (True, False), ("１", False)])
def test_src_numbers_are_ascii_digits_only(src, kept):
    rows = dm.build_rows([(T0, "m1", "U_A", "我住台中")])
    _system, _user, numbered, persons = dm.build_prompt(rows)
    candidates, mismatched = dm.check_provenance([{"person": "成員甲", "fact": "住在台中", "src": src}], numbered, persons)
    assert (len(candidates), mismatched) == ((1, 0) if kept else (0, 1))


@pytest.mark.parametrize(
    "reply",
    [
        '[{"person": "成員甲", "fact": "住在台中", "src": 1}]',
        '```json\n[{"person": "成員甲", "fact": "住在台中", "src": 1}]\n```',
        '以下是結果 [註]\n[{"person": "成員甲", "fact": "住在台中", "src": 1}]',
        '[{"person": "成員甲", "fact": "住在台中", "src": 1}]\n（共 1 條 [完]）',
        '見 [1] 與 [2]：[{"person": "成員甲", "fact": "住在台中", "src": 1}]',
    ],
)
def test_the_fact_array_is_found_inside_surrounding_text(reply):
    assert dm.parse_items(reply) == [{"person": "成員甲", "fact": "住在台中", "src": 1}]


@pytest.mark.parametrize("reply", ["抱歉，這段我不能處理", "[註] 沒有", '{"facts": "none"}', ""])
def test_a_reply_without_a_fact_array_is_a_permanent_chunk_error(reply):
    with pytest.raises(dm.PermanentError):
        dm.parse_items(reply)


def test_an_empty_array_means_no_facts():
    assert dm.parse_items("[]") == [] and dm.parse_items("沒有：[]") == []


def test_sharing_only_a_filler_word_is_not_provenance(job):
    _write_state()
    _raw("m1", "U_A", "我喜歡爬山", T0 + 10)
    job.model.reply = lambda _user: json.dumps([{"person": "成員甲", "fact": "喜歡游泳", "src": 1}], ensure_ascii=False)
    outcome = dm.run(now=NOW)
    assert (outcome.added, outcome.provenance_mismatch) == (0, 1)


def test_the_job_labels_a_new_member_whose_name_an_old_member_once_used(job):
    import line_mentions

    line_mentions.remember_display_name("U_OLD", "成員丁")  # 退群的人留在檔裡的名字
    line_mentions.remember_display_name("U_NEW", "成員丁")
    _write_state()
    _raw("m1", "U_NEW", "我住台中", T0 + 10)
    outcome = dm.run(now=NOW)
    assert outcome.added == 1
    assert memory.list_fact_rows(G) == [("U_NEW", "成員丁：住在台中")]
