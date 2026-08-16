"""Canonical correction-memory acceptance tests.

These tests intentionally exercise the public memory API instead of SQLite
implementation details.  Raw user corrections remain auditable while the
prompt consumes one group-local canonical projection per learned rule.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")


@pytest.fixture()
def correction_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "correction-memory.db"))
    monkeypatch.setenv("CORRECTION_CANONICAL_MEMORY_ENABLED", "true")

    import config
    import correction_memory
    import gemini_client
    import memory

    importlib.reload(config)
    importlib.reload(correction_memory)
    importlib.reload(memory)
    importlib.reload(gemini_client)
    return memory, gemini_client, correction_memory


def _force_same_rule(existing_rules, _candidate):
    if not existing_rules:
        return {"decision": "distinct", "score": 1.0}
    return {
        "decision": "equivalent",
        "rule_id": existing_rules[0]["rule_id"],
        "score": 0.99,
    }


def _record(memory, observation_id: str, text: str, *, group_id: str = "G1", adjudicator=None):
    return memory.record_organic_correction_observation(
        group_id=group_id,
        observation_id=observation_id,
        scenario="使用者主動糾正",
        content=f"user 糾正：{text}",
        candidate_rule=text,
        source_message_id=observation_id,
        actor_key="group-local-actor",
        observed_at=1_800_000_000 + int(observation_id.rsplit("-", 1)[-1]),
        adjudicator=adjudicator,
    )


def test_existing_persona_schema_adds_projection_column_before_index(tmp_path):
    db_path = tmp_path / "legacy-persona.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE persona_notes ("
            "group_id TEXT NOT NULL, note_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "kind TEXT NOT NULL, scenario TEXT NOT NULL, content TEXT NOT NULL, "
            "created_at INTEGER NOT NULL, "
            "source TEXT NOT NULL DEFAULT 'rule_violation')"
        )
        conn.execute(
            "INSERT INTO persona_notes"
            "(group_id, kind, scenario, content, created_at, source) "
            "VALUES ('G1', 'correction', 'legacy', 'raw', 1, 'organic')"
        )

    env = os.environ.copy()
    env["SQLITE_PATH"] = str(db_path)
    env["CORRECTION_CANONICAL_MEMORY_ENABLED"] = "true"
    completed = subprocess.run(
        [sys.executable, "-c", "import memory"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(persona_notes)")
        }
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(persona_notes)")
        }
        rule_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(correction_rules)")
        }
    assert "correction_linked" in columns
    assert "idx_persona_notes_prompt_projection" in indexes
    assert "idx_correction_rules_group_recurrence" in rule_indexes
    assert "idx_correction_rules_semantic_repair" in rule_indexes

    # Simulate a crash after ALTER/observation write but before the projection
    # flag update.  A later startup must repair the stale flag idempotently.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO correction_rules"
            "(group_id, canonical_key, canonical_rule, occurrence_count, "
            "first_seen_at, last_seen_at) VALUES ('G1', 'k1', 'rule', 1, 1, 1)"
        )
        rule_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO correction_observations"
            "(group_id, observation_id, note_id, scenario, content, "
            "candidate_rule, decision, canonical_rule_id, observed_at) "
            "VALUES ('G1', 'legacy:1', 1, 'legacy', 'raw', 'rule', "
            "'distinct', ?, 1)",
            (rule_id,),
        )
        conn.execute("UPDATE persona_notes SET correction_linked=0 WHERE note_id=1")
    repaired = subprocess.run(
        [sys.executable, "-c", "import memory"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert repaired.returncode == 0, repaired.stderr
    with sqlite3.connect(db_path) as conn:
        linked = conn.execute(
            "SELECT correction_linked FROM persona_notes WHERE note_id=1"
        ).fetchone()[0]
        repaired_semantic = conn.execute(
            "SELECT semantic_context FROM correction_rules WHERE rule_id=?",
            (rule_id,),
        ).fetchone()[0]
    assert linked == 1
    assert repaired_semantic == "none"


def test_prompt_candidate_queries_use_bounded_ordering_indexes(correction_modules):
    memory, _gemini_client, _correction_memory = correction_modules
    checks = (
        (
            "idx_correction_rules_group_recent",
            "SELECT rule_id FROM correction_rules "
            "INDEXED BY idx_correction_rules_group_recent "
            "WHERE group_id=? AND status='active' AND occurrence_count>0 "
            "ORDER BY last_seen_at DESC, rule_id DESC LIMIT 10",
        ),
        (
            "idx_correction_rules_group_recurrence",
            "SELECT rule_id FROM correction_rules "
            "INDEXED BY idx_correction_rules_group_recurrence "
            "WHERE group_id=? AND status='active' AND occurrence_count>0 "
            "ORDER BY occurrence_count DESC, last_seen_at DESC, rule_id DESC LIMIT 10",
        ),
    )
    with memory._conn() as conn:
        for expected_index, sql in checks:
            plan = [
                row[3]
                for row in conn.execute(
                    "EXPLAIN QUERY PLAN " + sql,
                    ("G1",),
                ).fetchall()
            ]
            assert any(expected_index in step for step in plan)
            assert not any("TEMP B-TREE" in step for step in plan)


def test_three_equivalent_audits_collapse_to_one_canonical_occurrence_three(
    correction_modules,
):
    memory, _gemini_client, _correction_memory = correction_modules
    texts = (
        "不要再把 13:10 當成出發時間",
        "12:35 才是出發，13:10 是抵達",
        "記住 13:10 是天下茶屋到站，不是起程",
    )
    outcomes = [
        _record(memory, f"msg-{idx}", text, adjudicator=_force_same_rule)
        for idx, text in enumerate(texts, 1)
    ]

    audits = memory.list_organic_correction_audits("G1")
    rules = memory.list_canonical_organic_corrections("G1")

    assert len(audits) == 3
    assert [audit["content"] for audit in audits] == [
        f"user 糾正：{text}" for text in texts
    ]
    assert len(rules) == 1
    assert rules[0]["occurrence_count"] == 3
    assert rules[0]["recurrence_count"] == 2
    assert rules[0]["is_recurrence"] is True
    assert rules[0]["last_seen_at"] == 1_800_000_003
    assert [outcome["status"] for outcome in outcomes] == [
        "new",
        "recurrent",
        "recurrent",
    ]


def test_prompt_uses_one_slot_for_equivalent_observations(correction_modules):
    memory, gemini_client, _correction_memory = correction_modules
    for idx, text in enumerate(
        ("不要再混淆出發與抵達", "別把到站時間寫成出發", "出發與到站必須分清楚"),
        1,
    ):
        _record(memory, f"msg-{idx}", text, adjudicator=_force_same_rule)

    notes = memory.list_persona_notes_for_prompt("G1")
    prompt = gemini_client._build_system_instruction([], notes)
    correction_lines = [
        line for line in prompt.splitlines() if line.startswith("[") and "|organic" in line
    ]

    assert len(correction_lines) == 1
    assert "出現 3 次" in correction_lines[0]


def test_same_observation_retry_is_idempotent_and_groups_are_isolated(
    correction_modules,
):
    memory, _gemini_client, _correction_memory = correction_modules
    first = _record(memory, "msg-1", "回答要簡短")
    replay = _record(memory, "msg-1", "回答要簡短")
    other_group = _record(memory, "msg-1", "回答要簡短", group_id="G2")

    assert first["status"] == "new"
    assert replay["status"] == "duplicate"
    assert other_group["status"] == "new"
    assert len(memory.list_organic_correction_audits("G1")) == 1
    assert len(memory.list_organic_correction_audits("G2")) == 1
    assert memory.list_canonical_organic_corrections("G1")[0]["occurrence_count"] == 1
    assert memory.list_canonical_organic_corrections("G2")[0]["occurrence_count"] == 1


def test_concurrent_replay_and_cross_group_rule_reference_fail_closed(
    correction_modules,
):
    memory, _gemini_client, _correction_memory = correction_modules

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(
            pool.map(
                lambda _index: _record(memory, "msg-1", "不要重複這條規則"),
                range(8),
            )
        )
    assert sum(outcome["status"] == "new" for outcome in outcomes) == 1
    assert sum(outcome["status"] == "duplicate" for outcome in outcomes) == 7
    assert len(memory.list_organic_correction_audits("G1")) == 1

    foreign = _record(memory, "msg-2", "另一群自己的規則", group_id="G2")

    def point_to_foreign(_rules, _candidate):
        return {
            "decision": "equivalent",
            "rule_id": foreign["rule_id"],
            "score": 1.0,
        }

    result = _record(
        memory,
        "msg-3",
        "試圖引用別群規則",
        group_id="G1",
        adjudicator=point_to_foreign,
    )
    assert result["status"] == "ambiguous"
    assert len(memory.list_canonical_organic_corrections("G1")) == 1


@pytest.mark.parametrize("decision", ["ambiguous", "conflict"])
def test_ambiguous_or_conflicting_observation_is_audited_but_not_merged_or_prompted(
    correction_modules,
    decision,
):
    memory, gemini_client, _correction_memory = correction_modules
    _record(memory, "msg-1", "不要主動插嘴")

    def reject(_rules, _candidate):
        return {"decision": decision, "score": 0.51}

    rejected = _record(
        memory,
        "msg-2",
        "被點名時必須回答",
        adjudicator=reject,
    )

    rules = memory.list_canonical_organic_corrections("G1")
    audits = memory.list_organic_correction_audits("G1")
    prompt = gemini_client._build_system_instruction(
        [], memory.list_persona_notes_for_prompt("G1")
    )
    assert rejected["status"] == decision
    assert len(audits) == 2
    assert len(rules) == 1
    assert rules[0]["occurrence_count"] == 1
    assert "被點名時必須回答" not in prompt


def test_default_local_matcher_merges_only_high_confidence_equivalents(
    correction_modules,
):
    memory, _gemini_client, _correction_memory = correction_modules
    for idx, text in enumerate(
        (
            "不要再把 13:10 當成出發時間",
            "別把 13:10 說成起程時間",
            "記住 13:10 是到站，不是出發",
        ),
        1,
    ):
        _record(memory, f"msg-{idx}", text)

    rules = memory.list_canonical_organic_corrections("G1")
    assert len(rules) == 1
    assert rules[0]["occurrence_count"] == 3

    conflict = _record(memory, "msg-4", "13:10 必須標成出發時間")
    assert conflict["status"] == "conflict"
    assert memory.list_canonical_organic_corrections("G1")[0]["occurrence_count"] == 3

    contrastive = _record(memory, "msg-40", "13:10 不是到站,是出發")
    assert contrastive["status"] == "conflict"

    different_time = _record(memory, "msg-5", "14:20 必須標成出發時間")
    assert different_time["status"] == "new"
    assert len(memory.list_canonical_organic_corrections("G1")) == 2

    numbered_one = _record(memory, "msg-6", "規則 1 必須保留完整主詞")
    numbered_ten = _record(memory, "msg-7", "規則 10 必須保留完整主詞")
    assert numbered_one["status"] == "new"
    assert numbered_ten["status"] == "new"

    ambiguous = _record(memory, "msg-8", "以後不要這樣回")
    assert ambiguous["status"] == "ambiguous"

    for idx, vague in enumerate(
        (
            "記住別再這樣處理",
            "你以後不可以這樣",
            "以後不可以那樣做",
        ),
        9,
    ):
        assert _record(memory, f"msg-{idx}", vague)["status"] == "ambiguous"


def test_transport_and_entity_context_changes_never_fuzzy_merge(correction_modules):
    memory, _gemini_client, _correction_memory = correction_modules
    _record(memory, "msg-1", "台北到台中這班 13:10 不是出發時間")
    route_change = _record(
        memory, "msg-2", "大阪到京都這班 13:10 不是出發時間"
    )
    date_one = _record(memory, "msg-3", "2026/8/16 13:10 不是出發時間")
    date_two = _record(memory, "msg-4", "2026/8/17 13:10 不是出發時間")
    entity_one = _record(
        memory,
        "msg-5",
        "回答這段很長的規則時不要把台北改成別的城市名稱",
    )
    entity_two = _record(
        memory,
        "msg-6",
        "回答這段很長的規則時不要把台中改成別的城市名稱",
    )

    assert route_change["status"] == "new"
    assert date_one["status"] == "new"
    assert date_two["status"] == "new"
    assert entity_one["status"] == "new"
    assert entity_two["status"] == "new"


def test_motivating_pelvic_detail_paraphrases_merge_without_injected_matcher(
    correction_modules,
):
    memory, _gemini_client, _correction_memory = correction_modules
    texts = (
        "bot 僅複述成因,未提具體骨盆傾斜。",
        "Bot 未能具體回應右高左低的骨盆狀況。",
        "Bot 應直接納入使用者指出的骨盆高低細節。",
    )
    outcomes = [
        _record(memory, f"msg-{idx}", text)
        for idx, text in enumerate(texts, 1)
    ]

    assert [outcome["status"] for outcome in outcomes] == [
        "new",
        "recurrent",
        "recurrent",
    ]
    rules = memory.list_canonical_organic_corrections("G1")
    assert len(rules) == 1
    assert rules[0]["occurrence_count"] == 3


def test_detail_signature_keeps_other_body_topics_and_opposites_separate(
    correction_modules,
):
    memory, _gemini_client, _correction_memory = correction_modules
    _record(memory, "msg-1", "未能具體回應骨盆高低細節")
    other_topic = _record(memory, "msg-2", "未能具體回應肩膀高低細節")
    opposite = _record(memory, "msg-3", "不要納入骨盆高低細節")

    assert other_topic["status"] == "new"
    assert opposite["status"] == "conflict"
    assert len(memory.list_canonical_organic_corrections("G1")) == 2


@pytest.mark.parametrize(
    ("first", "second"),
    (
        ("Bot 未提具體台北住宿價格", "Bot 未提具體台中住宿價格"),
        ("Bot 未提具體哥哥醫療紀錄", "Bot 未提具體妹妹醫療紀錄"),
        ("Bot 未提具體骨盆傾斜", "Bot 未提具體骨盆骨折"),
        ("回答應直接納入骨盆前傾細節", "回答應直接納入骨盆後傾細節"),
        ("回覆忽略台積電股價細節", "回覆忽略台積電法說細節"),
        ("媽媽生日細節未提", "妹妹生日細節未提"),
    ),
)
def test_detail_rules_never_merge_on_shared_generic_ngrams(
    correction_modules, first, second
):
    memory, _gemini_client, _correction_memory = correction_modules
    _record(memory, "msg-1", first)
    result = _record(memory, "msg-2", second)

    assert result["status"] == "new"
    assert len(memory.list_canonical_organic_corrections("G1")) == 2


@pytest.mark.parametrize(
    "same_intent",
    (
        "不要忘記提到具體骨盆傾斜細節",
        "不要忽略骨盆傾斜細節，要納入回答",
        "不要只回答成因，要納入骨盆高低細節",
    ),
)
def test_pelvic_detail_negative_concord_still_requires_inclusion(
    correction_modules, same_intent
):
    memory, _gemini_client, _correction_memory = correction_modules
    _record(memory, "msg-1", "Bot 未提具體骨盆傾斜")
    result = _record(memory, "msg-2", same_intent)

    assert result["status"] == "recurrent"


@pytest.mark.parametrize(
    "opposite",
    (
        "回答不應公開骨盆傾斜細節",
        "回答禁止揭露骨盆傾斜細節",
        "回答不該提到骨盆傾斜細節",
        "回答不用提到骨盆傾斜細節",
        "回答不需要公開骨盆傾斜細節",
        "回答無需公開骨盆傾斜細節",
        "回答不必公開骨盆傾斜細節",
        "回答不准公開骨盆傾斜細節",
        "回答不准納入骨盆傾斜細節",
        "回答不得公開骨盆傾斜細節",
        "回答嚴禁納入骨盆傾斜細節",
        "回答無須公開骨盆傾斜細節",
        "回答毋須納入骨盆傾斜細節",
        "回答不宜公開骨盆傾斜細節",
        "回答避免納入骨盆傾斜細節",
        "忽略骨盆傾斜細節",
        "漏掉骨盆傾斜細節",
        "回答應忽略骨盆傾斜細節",
        "回答必須省略骨盆傾斜細節",
    ),
)
def test_pelvic_detail_explicit_exclusion_is_conflict(
    correction_modules, opposite
):
    memory, _gemini_client, _correction_memory = correction_modules
    _record(memory, "msg-1", "回答應直接納入骨盆傾斜細節")
    result = _record(memory, "msg-2", opposite)

    assert result["status"] == "conflict"


def test_pelvic_canonical_remembers_absorbed_direction(correction_modules):
    memory, _gemini_client, _correction_memory = correction_modules
    _record(memory, "msg-1", "Bot 未提具體骨盆傾斜")
    same_direction = _record(
        memory, "msg-2", "回答應納入右高左低的骨盆細節"
    )
    opposite_direction = _record(
        memory, "msg-3", "回答應納入左高右低的骨盆細節"
    )

    assert same_direction["status"] == "recurrent"
    assert opposite_direction["status"] == "new"
    assert len(memory.list_canonical_organic_corrections("G1")) == 2


def test_startup_rebuilds_missing_pelvic_semantic_state(correction_modules):
    memory, _gemini_client, _correction_memory = correction_modules
    _record(memory, "msg-1", "Bot 未提具體骨盆傾斜")
    _record(memory, "msg-2", "回答應納入右高左低的骨盆細節")
    with memory._conn() as conn:
        conn.execute("UPDATE correction_rules SET semantic_context='' WHERE group_id='G1'")

    importlib.reload(memory)
    result = _record(memory, "msg-3", "回答應納入左高右低的骨盆細節")

    assert result["status"] == "new"
    assert len(memory.list_canonical_organic_corrections("G1")) == 2


@pytest.mark.parametrize(
    "rule",
    (
        "不要推播", "不要傳錢", "別提地址", "不能洩密", "不要刪檔",
        "不准推播", "請勿洩密", "不應刪檔", "不該傳錢", "無需公開",
        "不必回覆", "不用推播",
        "不得推播", "嚴禁洩密", "無須公開", "毋須回覆", "不宜透露", "避免洩密",
    ),
)
def test_short_concrete_safety_rules_are_reusable(correction_modules, rule):
    memory, gemini_client, _correction_memory = correction_modules
    outcome = _record(memory, "msg-1", rule)
    prompt = gemini_client._build_system_instruction(
        [], memory.list_persona_notes_for_prompt("G1")
    )

    assert outcome["status"] == "new"
    assert rule in prompt


@pytest.mark.parametrize(
    ("first", "second"),
    (
        ("日期 1/2 必須保留", "日期 12 必須保留"),
        ("版本 1.2 不要發布", "版本 12 不要發布"),
        ("時長 1:20 必須保留", "時長 120 必須保留"),
        ("代碼 A/B 必須保留", "代碼 AB 必須保留"),
        ("代碼 A / B 必須保留", "代碼 AB 必須保留"),
        ("溫度必須是 -5°C", "溫度必須是 +5°C"),
        ("限制值必須 <5", "限制值必須 >5"),
        ("漲幅必須保留 5%", "漲幅必須保留 5"),
        ("金額必須保留 $5", "金額必須保留 5"),
        ("小數必須保留 .5", "小數必須保留 5"),
        ("容差必須 ±5°C", "容差必須 5°C"),
        ("限制值必須 ~5", "限制值必須 5"),
        ("誤差必須 ≈5%", "誤差必須 5%"),
        ("財務變動必須保留 (5)", "財務變動必須保留 5"),
        ("限制值必須 >=5", "限制值必須 <=5"),
        ("x 必須 != 5", "x 必須 == 5"),
        ("SQL 條件必須 <>5", "SQL 條件必須 >5"),
        ("error_code 必須保留", "errorcode 必須保留"),
        ("代碼 A::B 必須保留", "代碼 AB 必須保留"),
        ("溫度必須 −5°C", "溫度必須 5°C"),
        ("函式 __init__ 必須保留", "函式 init 必須保留"),
        ("代碼 foo__bar 必須保留", "代碼 foobar 必須保留"),
        ("檔案 .env 必須保留", "檔案 env 必須保留"),
        ("語言 C++ 必須保留", "語言 C 必須保留"),
        ("條件 x==y 必須保留", "條件 xy 必須保留"),
        ("條件 x!=y 必須保留", "條件 x==y 必須保留"),
        ("遮罩 A|B 必須保留", "遮罩 AB 必須保留"),
        ("流程 x->y 必須保留", "流程 xy 必須保留"),
        ("流程 A→B 必須保留", "流程 A←B 必須保留"),
        ("方向 北⇒南 必須保留", "方向 北⇐南 必須保留"),
        ("限制值必須 ≠5", "限制值必須 5"),
        ("限制值必須 ≦5", "限制值必須 ≧5"),
        ("趨勢必須 ↑5", "趨勢必須 ↓5"),
        ("誤差必須 ∼5%", "誤差必須 5%"),
        ("金額必須 ₩5", "金額必須 5"),
        ("金額必須 ₹5", "金額必須 5"),
        ("金額必須 ₽5", "金額必須 5"),
        ("金額必須 ₿5", "金額必須 5"),
        ("金額必須 ₫5", "金額必須 5"),
        ("金額必須 ₱5", "金額必須 5"),
        ("金額必須 ฿5", "金額必須 5"),
        ("比例必須 5‰", "比例必須 5"),
        ("比例必須 5‱", "比例必須 5"),
        ("條件 5<x 必須保留", "條件 5x 必須保留"),
        ("條件 5<x 必須保留", "條件 5>x 必須保留"),
        ("條件 200==code 必須保留", "條件 200code 必須保留"),
        ("條件 0!=ready 必須保留", "條件 0==ready 必須保留"),
        ("條件 x≤y 必須保留", "條件 xy 必須保留"),
        ("條件 x≈y 必須保留", "條件 xy 必須保留"),
        ("條件 x∼y 必須保留", "條件 xy 必須保留"),
        ("條件 x~y 必須保留", "條件 xy 必須保留"),
        ("條件 x±y 必須保留", "條件 xy 必須保留"),
        ("條件 x−y 必須保留", "條件 xy 必須保留"),
        ("條件 x×y 必須保留", "條件 xy 必須保留"),
        ("條件 x÷y 必須保留", "條件 xy 必須保留"),
        ("條件 x∧y 必須保留", "條件 xy 必須保留"),
        ("條件 x∨y 必須保留", "條件 xy 必須保留"),
        ("程式碼必須用 A===B", "程式碼必須用 AB"),
        ("程式碼必須用 A!==B", "程式碼必須用 AB"),
        ("程式碼必須用 A^B", "程式碼必須用 AB"),
        ("程式碼必須用 A:=B", "程式碼必須用 AB"),
        ("集合必須用 A⊂B", "集合必須用 A⊃B"),
        ("集合必須用 A∈B", "集合必須用 AB"),
        ("集合必須用 A∩B", "集合必須用 A∪B"),
    ),
)
def test_structured_values_are_protected_before_normalized_exact_match(
    correction_modules,
    first,
    second,
):
    memory, _gemini_client, _correction_memory = correction_modules
    _record(memory, "msg-1", first)
    result = _record(memory, "msg-2", second)

    assert result["status"] == "new"
    assert len(memory.list_canonical_organic_corrections("G1")) == 2


@pytest.mark.parametrize(
    ("first", "second"),
    (
        ("1/2 13:10 不是出發時間", "12 13:10 不是出發時間"),
        ("A/B 這班 13:10 不是出發", "AB 這班 13:10 不是出發"),
        ("版本 1.2 的班次 13:10 不是出發", "版本 12 的班次 13:10 不是出發"),
        ("2026/1/23 13:10 不是出發", "2026/12/3 13:10 不是出發"),
    ),
)
def test_transport_shortcut_never_bypasses_protected_values(
    correction_modules, first, second
):
    memory, _gemini_client, _correction_memory = correction_modules
    _record(memory, "msg-1", first)
    result = _record(memory, "msg-2", second)
    assert result["status"] == "new"


@pytest.mark.parametrize(
    ("negative", "positive"),
    (
        ("13:10 不是預計出發時間", "13:10 是預計出發時間"),
        ("13:10 不是原定出發", "13:10 是原定出發"),
        ("不要把 13:10 寫成出發，應該是抵達", "不要把 13:10 寫成抵達，應該是出發"),
        ("13:10 是到站，不是出發", "13:10 別標成到站，必須標成出發"),
        ("13:10 不應該是出發時間", "13:10 應該是出發時間"),
        ("13:10 不該是出發時間", "13:10 必須是出發時間"),
        ("13:10 不應視為出發時間", "13:10 應視為出發時間"),
        ("13:10 不能算是出發", "13:10 應該算是出發"),
        ("13:10 無需標成出發", "13:10 應該標成出發"),
        ("13:10 不是應該出發", "13:10 應該出發"),
        ("13:10 不准標成出發", "13:10 必須標成出發"),
        ("13:10 禁止標成出發", "13:10 必須標成出發"),
        ("13:10 不得標成出發", "13:10 必須標成出發"),
        ("13:10 嚴禁標成出發", "13:10 必須標成出發"),
        ("13:10 無須標成出發", "13:10 應該標成出發"),
        ("13:10 毋須標成出發", "13:10 應該標成出發"),
        ("13:10 不宜標成出發", "13:10 應該標成出發"),
        ("不宜把13:10算作出發", "必須把13:10算作出發"),
        ("不得把13:10算作出發", "必須把13:10算作出發"),
        ("禁止把13:10算作出發", "必須把13:10算作出發"),
        ("嚴禁把13:10算作出發", "必須把13:10算作出發"),
        ("無須把13:10算作出發", "必須把13:10算作出發"),
        ("避免把13:10算作出發", "必須把13:10算作出發"),
        ("13:10 不是必須算作出發", "13:10 必須算作出發"),
        ("13:10 並非務必算作出發", "13:10 務必算作出發"),
        ("13:10 無需務必算作出發", "13:10 務必算作出發"),
    ),
)
def test_transport_contrast_resolves_direct_assertion(
    correction_modules, negative, positive
):
    memory, _gemini_client, _correction_memory = correction_modules
    _record(memory, "msg-1", negative)
    result = _record(memory, "msg-2", positive)
    assert result["status"] == "conflict"


@pytest.mark.parametrize(
    ("positive", "negative"),
    (
        ("回答時應該提到使用者真名", "回答時不應該提到使用者真名"),
        ("群組回覆需要包含完整地址", "群組回覆不需要包含完整地址"),
        ("回答必須公開聯絡方式", "回答不必公開聯絡方式"),
        ("回答必須公開完整地址", "回答不該公開完整地址"),
        ("回答需要公開完整地址", "回答不用公開完整地址"),
    ),
)
def test_negative_modalities_quarantine_conflicting_rules(
    correction_modules,
    positive,
    negative,
):
    memory, _gemini_client, _correction_memory = correction_modules
    _record(memory, "msg-1", positive)
    result = _record(memory, "msg-2", negative)

    assert result["status"] == "conflict"
    assert len(memory.list_canonical_organic_corrections("G1")) == 1


def test_recurrent_rule_gets_one_priority_slot_with_prompt_cap(correction_modules):
    memory, gemini_client, _correction_memory = correction_modules
    for idx in range(1, 13):
        _record(
            memory,
            f"msg-{idx}",
            f"規則內容 {idx} 必須保留完整主詞",
            adjudicator=lambda _rules, _candidate: {
                "decision": "distinct",
                "score": 1.0,
            },
        )

    first_rule = memory.list_canonical_organic_corrections("G1")[-1]

    def recur_first(_rules, _candidate):
        return {
            "decision": "equivalent",
            "rule_id": first_rule["rule_id"],
            "score": 0.99,
        }

    _record(memory, "msg-13", "第一條規則再次出現", adjudicator=recur_first)
    _record(memory, "msg-14", "第一條規則第三次出現", adjudicator=recur_first)

    prompt = gemini_client._build_system_instruction(
        [], memory.list_persona_notes_for_prompt("G1")
    )
    correction_lines = [
        line for line in prompt.splitlines() if line.startswith("[") and "|organic" in line
    ]
    assert len(correction_lines) == 10
    assert sum("規則內容 1 必須保留完整主詞" in line for line in correction_lines) == 1
    assert any("出現 3 次" in line for line in correction_lines)


def test_newest_singleton_is_not_starved_by_old_recurrent_rules(
    correction_modules,
):
    _memory, gemini_client, _correction_memory = correction_modules
    notes = [
        {
            "note_id": idx,
            "canonical_rule_id": idx,
            "kind": "correction",
            "scenario": "canonical",
            "content": f"舊高頻規則 {idx}",
            "created_at": 1_700_000_000 + idx,
            "last_seen_at": 1_700_000_000 + idx,
            "source": "organic",
            "occurrence_count": 2,
        }
        for idx in range(1, 11)
    ]
    notes.append(
        {
            "note_id": 11,
            "canonical_rule_id": 11,
            "kind": "correction",
            "scenario": "canonical",
            "content": "剛收到的新糾正",
            "created_at": 1_800_000_000,
            "last_seen_at": 1_800_000_000,
            "source": "organic",
            "occurrence_count": 1,
        }
    )

    prompt = gemini_client._build_system_instruction([], notes)
    correction_lines = [
        line for line in prompt.splitlines() if line.startswith("[") and "|organic" in line
    ]
    assert len(correction_lines) == 10
    assert "剛收到的新糾正" in prompt
    assert sum("舊高頻規則" in line for line in correction_lines) == 9


def test_feature_flag_off_restores_legacy_raw_prompt(correction_modules, monkeypatch):
    memory, gemini_client, _correction_memory = correction_modules
    _record(memory, "msg-1", "不要重複開場", adjudicator=_force_same_rule)
    _record(memory, "msg-2", "別再重複使用者的話", adjudicator=_force_same_rule)

    canonical = gemini_client._build_system_instruction(
        [], memory.list_persona_notes_for_prompt("G1")
    )
    assert sum("|organic" in line for line in canonical.splitlines()) == 1

    monkeypatch.setenv("CORRECTION_CANONICAL_MEMORY_ENABLED", "false")
    legacy = gemini_client._build_system_instruction(
        [], memory.list_persona_notes_for_prompt("G1")
    )
    assert sum("|organic" in line for line in legacy.splitlines()) == 2


def test_split_then_undo_restores_projection_without_changing_audits(
    correction_modules,
):
    memory, _gemini_client, _correction_memory = correction_modules
    for idx in range(1, 4):
        _record(
            memory,
            f"msg-{idx}",
            f"同類糾正 {idx}",
            adjudicator=_force_same_rule,
        )
    original_audits = memory.list_organic_correction_audits("G1")
    original_rule = memory.list_canonical_organic_corrections("G1")[0]

    event_id = memory.split_correction_rule(
        "G1", original_rule["rule_id"], ["msg-3"]
    )
    assert sorted(
        rule["occurrence_count"]
        for rule in memory.list_canonical_organic_corrections("G1")
    ) == [1, 2]

    assert memory.undo_correction_rule_event("G1", event_id) is True
    restored = memory.list_canonical_organic_corrections("G1")
    assert len(restored) == 1
    assert restored[0]["occurrence_count"] == 3
    assert memory.list_organic_correction_audits("G1") == original_audits


def test_new_observation_is_ambiguous_when_split_rules_both_match(
    correction_modules,
):
    memory, _gemini_client, _correction_memory = correction_modules
    _record(memory, "msg-1", "不要再重複開場")
    _record(memory, "msg-2", "別再重複開場", adjudicator=_force_same_rule)
    original = memory.list_canonical_organic_corrections("G1")[0]
    memory.split_correction_rule("G1", original["rule_id"], ["msg-2"])

    result = _record(memory, "msg-3", "不要再重複開場")

    assert result["status"] == "ambiguous"
    assert sorted(
        rule["occurrence_count"]
        for rule in memory.list_canonical_organic_corrections("G1")
    ) == [1, 1]


def test_legacy_backfill_is_idempotent_and_keeps_raw_persona_note(correction_modules):
    memory, _gemini_client, _correction_memory = correction_modules
    with memory._lock, memory._conn() as conn:
        conn.execute(
            "INSERT INTO persona_notes"
            "(group_id, kind, scenario, content, created_at, source) "
            "VALUES (?, 'correction', '使用者主動糾正', ?, ?, 'organic')",
            ("G1", "教訓：回答不要過度正式", 1_700_000_000),
        )
    before = memory.list_persona_notes("G1", kind="correction")

    first = memory.backfill_organic_corrections(group_id="G1", dry_run=False)
    second = memory.backfill_organic_corrections(group_id="G1", dry_run=False)

    assert first["linked"] == 1
    assert second["linked"] == 0
    assert memory.list_persona_notes("G1", kind="correction") == before
    assert len(memory.list_organic_correction_audits("G1")) == 1
    assert len(memory.list_canonical_organic_corrections("G1")) == 1


def test_legacy_backfill_links_question_audits_without_activating_rules(
    correction_modules,
):
    memory, gemini_client, _correction_memory = correction_modules
    questions = (
        "不應該喝酒對不對",
        "回答時不公開地址對不對",
        "自駕不走雪地是不是比較好",
        "我問的是為什麼不能去",
        "我問的是怎麼搭車",
        "我問的是哪裡可以停車",
        "我問的是能否公開地址",
        "我是說為什麼不能喝酒",
        "你誤會了，我是問如何處理",
        "不對，我問的是什麼時候出發",
        "不對啦，我問的是為什麼不能去",
        "不對啊，我問的是怎麼搭車",
        "你誤會啦，我是問如何處理",
        "不是這樣，我問的是為什麼不能去",
        "我意思是問為什麼不能去",
        "我意思不是在問如何處理",
        "你誤會了，我是想問怎麼設定",
        "不對，我的意思是我想問為什麼這樣",
        "不是這樣，我只是想知道怎麼做",
        "不對啦，我問為什麼不能去",
        "不對啦，我想問怎麼去",
        "你誤會啦，我只是想問怎麼去",
        "不是啦，我是問為什麼不能去",
        "不對啦，我問為啥不能去",
        "你誤會啦，我想問怎會這樣",
        "你誤會啦，我想問啥時出發",
        "不是啦，我的問題是哪天出發",
        "不是啦，我的問題是哪邊停車",
        "你誤會了，我只是想問怎麼設定",
        "不對，我其實是想問為什麼這樣",
        "不是這樣，我是想知道如何處理",
        "你誤會了，我大概想問哪裡停車",
        "你誤會了，我只是好奇為什麼會這樣",
        "不對，我其實是問怎麼搭車",
        "不是這樣，我真正想問哪裡停車",
        "你誤會了，我原本是想問如何處理",
        "不對，我主要想知道為啥不能去",
        "不對，我要問的是怎麼搭車",
        "不是這樣，我真正想問的是怎麼做",
        "不對，我想問一下怎麼設定",
        "你誤會了，我想知道的是為什麼會這樣",
        "不是這樣，我只是好奇的是如何處理",
        "答錯了，我想請教一下哪裡停車",
        "不是我要的，我想請問怎麼搭車",
        "不對，我是要問怎麼搭車",
        "你誤會了，我就是想問為什麼",
        "不是這樣，我才是想問哪裡停車",
        "不對，我其實只是想問如何設定",
        "不對，我想問一下有沒有其他方法",
        "你誤會了，我想知道哪些可以用",
        "答錯了，我想請問哪個比較好",
        "不是我要的，我想知道要去哪",
        "不對啦，我問你怎麼去",
        "不對啦，我是在問為什麼不能去",
        "不是啦，我問的就是為什麼不能去",
        "你誤會啦，我正在問咪寶哪邊集合",
        "你誤會啦，我才正在問咪寶哪邊集合",
        "我說的是，我可能正在想問bot的問題是哪個比較好",
        "不對，我不是想問為什麼，我是想問怎麼做",
        "不對，我不想知道為什麼，我想知道怎麼做",
        "不是啦，我不是問哪裡，我是問怎麼去",
        "你誤會了，我沒想問為什麼，我真正想問的是如何處理",
        "不對，我不是想問為什麼，而是想問怎麼做",
        "不對，我不想知道為什麼，而是想知道怎麼做",
        "不是啦，我不是問哪裡；我是問怎麼去",
        "不對，我不是在問為什麼，我是在問怎麼做",
        "不對，我不是問為什麼，而是問怎麼做",
        "不對，我不是要問為什麼，是要問怎麼做",
        "你誤會，我沒問哪個，我其實想知道有哪些方法",
        "不對，我想知道還有哪些選項",
        "你答錯了，我想問到底為什麼失敗",
        "不對，我不是問為什麼而是問怎麼做",
        "不對，我不是在問為什麼而是在問怎麼做",
        "不對，我不想知道為什麼只是想知道怎麼做",
        "不對，我不是想問為什麼，但我是想問怎麼做",
        "不對，我不是想問為什麼，但是我想問怎麼做",
        "不對，我不是想問為什麼，可是我想問怎麼做",
        "不對，我不是想問為什麼，不過我想問怎麼做",
        "不對，我沒有在問為什麼，我是在問怎麼做",
        "不對，我沒有要問為什麼，而是想問怎麼做",
        "不對，我並不是想問為什麼，而是想問怎麼做",
        "不對，我並非想問為什麼，而是想問怎麼做",
        "不對，我不是想問你剛回答的機場路線為什麼有問題，而是想問怎麼搭車",
        "不對，我不是想問你剛才回答的機場路線為什麼還有問題，而是想問怎麼搭車",
        "不對，我不是想問" + "甲" * 161 + "，而是我想問怎麼做",
        "不對，我不是想問" + "甲" * 183 + "，而是我想問怎麼做",
        "你答錯了，我是問為什麼不能去",
        "妳答錯了，我想問怎麼去",
        "答錯了，我問的是哪邊集合",
        "我說的是為什麼不能去",
        "不是這意思，我只是想問如何設定",
        "不是我要的，我想問啥時出發",
        "請重答，我其實想問怎麼去",
        *(
            f"{prefix}，我想問怎麼搭車"
            for prefix in _correction_memory.ORGANIC_CORRECTION_PREFIXES
        ),
    )
    with memory._lock, memory._conn() as conn:
        for idx, question in enumerate(questions, 1):
            conn.execute(
                "INSERT INTO persona_notes"
                "(group_id, kind, scenario, content, created_at, source) "
                "VALUES (?, 'correction', 'legacy', ?, ?, 'organic')",
                ("G1", f"教訓：{question}", 1_700_000_000 + idx),
            )

    result = memory.backfill_organic_corrections(group_id="G1", dry_run=False)
    prompt = gemini_client._build_system_instruction(
        [], memory.list_persona_notes_for_prompt("G1")
    )

    assert result == {
        "eligible": len(questions),
        "linked": len(questions),
        "unresolved": len(questions),
    }
    assert len(memory.list_organic_correction_audits("G1")) == len(questions)
    assert memory.list_canonical_organic_corrections("G1") == []
    assert not any(question in prompt for question in questions)


@pytest.mark.parametrize(
    "statement",
    (
        "回答必須解釋什麼是 SCD",
        "判斷重點在於能否驗證來源",
        "效益實現的關鍵在於能否保障所有股東權益並克服數位障礙",
        "不對，我是說回答必須解釋什麼是 SCD",
        "不對，不要再問我為什麼",
        "不對，回答必須先問清楚為什麼要刪除",
        "你誤會了，以後要先問使用者為什麼要公開地址",
        "不是這樣，請先問清楚怎麼處理再回答",
        "答錯了，回答應該先問哪裡集合",
        "不對，我不想知道為什麼，只要結論",
        "不對，我沒想問為什麼，只要結論",
        "不對，我不是想問為什麼，我要結論",
        "不對，不能問為什麼，只要結論",
        "不對，不可問為什麼，只要結論",
        "不對，不應問為什麼，只要結論",
        "不對，不該問為什麼，只要結論",
        "不對，不必問為什麼，只要結論",
        "不對，不用問為什麼，只要結論",
        "不對，無需問為什麼，只要結論",
        "不對，無須問為什麼，只要結論",
        "不對，毋須問為什麼，只要結論",
        "不對，不宜問為什麼，只要結論",
        "不對，我沒有想問為什麼，只要結論",
        "不對，我並非想問為什麼，只要結論",
    ),
)
def test_interrogative_words_inside_declarative_rules_are_not_quarantined(
    correction_modules,
    statement,
):
    memory, _gemini_client, _correction_memory = correction_modules
    outcome = _record(memory, "msg-1", statement)

    assert outcome["status"] == "new"
    assert len(memory.list_canonical_organic_corrections("G1")) == 1


def test_legacy_backfill_without_summary_uses_user_correction_line(
    correction_modules,
):
    memory, _gemini_client, _correction_memory = correction_modules
    raw = (
        "user 原問：今天幾點出發\n"
        "咪寶當時答：13:10 出發\n"
        "user 糾正：13:10 是到站，不是出發"
    )
    with memory._lock, memory._conn() as conn:
        conn.execute(
            "INSERT INTO persona_notes"
            "(group_id, kind, scenario, content, created_at, source) "
            "VALUES (?, 'correction', '使用者主動糾正', ?, ?, 'organic')",
            ("G1", raw, 1_700_000_000),
        )

    result = memory.backfill_organic_corrections(group_id="G1", dry_run=False)
    assert result == {"eligible": 1, "linked": 1, "unresolved": 0}
    rules = memory.list_canonical_organic_corrections("G1")
    assert rules[0]["canonical_rule"] == "13:10 是到站,不是出發"


def test_rule_violation_retention_never_deletes_organic_audit(correction_modules):
    memory, _gemini_client, _correction_memory = correction_modules
    organic = _record(memory, "msg-1", "不要刪除這筆原始糾正")

    for idx in range(memory._PERSONA_NOTE_CAP + 1):
        memory.add_persona_note(
            "G1",
            "correction",
            "自動違規",
            f"rule violation {idx}",
            source="rule_violation",
        )

    remaining_note_ids = {
        note["note_id"] for note in memory.list_persona_notes("G1", kind="correction")
    }
    assert organic["note_id"] in remaining_note_ids
    assert len(memory.list_organic_correction_audits("G1")) == 1

    for idx in range(memory._PERSONA_NOTE_CAP + 1):
        memory.add_persona_note(
            "G1",
            "correction",
            "legacy organic writer",
            f"organic legacy {idx}",
            source="organic",
        )
    remaining_note_ids = {
        note["note_id"] for note in memory.list_persona_notes("G1", kind="correction")
    }
    assert organic["note_id"] in remaining_note_ids
    assert len(memory.list_organic_correction_audits("G1")) == 1


def test_cli_dry_run_does_not_create_schema(tmp_path):
    db_path = tmp_path / "empty.db"
    sqlite3.connect(db_path).close()
    env = dict(os.environ, SQLITE_PATH=str(db_path))
    script = Path(__file__).resolve().parents[1] / "scripts" / "backfill_correction_memory.py"

    completed = subprocess.run(
        [sys.executable, str(script)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert '"mode": "dry-run"' in completed.stdout
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchone()[0] == 0


def test_prompt_reads_rules_and_links_from_one_sqlite_snapshot(
    correction_modules,
    monkeypatch,
):
    memory, _gemini_client, _correction_memory = correction_modules
    raw = "教訓：回答不要省略重要限制"
    with memory._lock, memory._conn() as conn:
        conn.execute(
            "INSERT INTO persona_notes"
            "(group_id, kind, scenario, content, created_at, source) "
            "VALUES ('G1', 'correction', 'legacy', ?, 1700000000, 'organic')",
            (raw,),
        )

    real_conn = memory._conn
    triggered = False

    class InterleavedConnection:
        def __init__(self):
            self.inner = real_conn()

        @property
        def in_transaction(self):
            return self.inner.in_transaction

        def execute(self, sql, params=()):
            nonlocal triggered
            cursor = self.inner.execute(sql, params)
            if "FROM correction_rules" in sql and not triggered:
                triggered = True
                monkeypatch.setattr(memory, "_conn", real_conn)
                try:
                    result = memory.backfill_organic_corrections(
                        group_id="G1", dry_run=False
                    )
                    assert result["linked"] == 1
                finally:
                    monkeypatch.setattr(memory, "_conn", patched_conn)
            return cursor

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self.inner.__exit__(exc_type, exc, tb)

    def patched_conn():
        return InterleavedConnection()

    monkeypatch.setattr(memory, "_conn", patched_conn)
    try:
        notes = memory.list_persona_notes_for_prompt("G1")
    finally:
        monkeypatch.setattr(memory, "_conn", real_conn)

    assert triggered is True
    assert sum(note.get("source") == "organic" for note in notes) == 1
    assert raw in {note["content"] for note in notes}
