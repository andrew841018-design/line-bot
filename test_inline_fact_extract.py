"""即時抽取改走 fact_store（2026-10-11 每日記憶）：開關、同一套過濾與 tombstone、回覆路徑不等鎖。

呼叫 extract_facts 的方式不變（既有測試的 lambda _ctx, speakers=() 照樣能用）。名字與內容全是合成的。
"""

from __future__ import annotations

import json

import pytest

import fact_store
import main
import memory

G = "G_INLINE_FACTS"


@pytest.fixture(autouse=True)
def _setup(tmp_path, monkeypatch):
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"U_A": "成員甲", "U_B": "成員乙"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(tmp_path / "no_roles.json"))
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "")
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(memory, "bump_and_should_extract", lambda _g: True)
    memory.append_turn(G, "user", "[burst]\n成員甲：我住台中\n成員乙：我在台北上班")


def _extract(monkeypatch, facts):
    calls = []

    def fake(_ctx, speakers=()):
        calls.append(list(speakers))
        return facts

    monkeypatch.setattr(main.gemini_client, "extract_facts", fake)
    main._extract_facts_now(G)
    return calls


@pytest.mark.parametrize("value", ["0", "false", "off", " NO "])
def test_the_switch_turns_inline_extraction_off(monkeypatch, value):
    monkeypatch.setenv("LINE_BOT_INLINE_FACT_EXTRACT", value)
    monkeypatch.setattr(memory, "bump_and_should_extract", lambda _g: pytest.fail("no bump while off"))
    assert _extract(monkeypatch, [("成員甲", "住在台中")]) == []
    assert memory.list_fact_rows(G) == []


def test_the_switch_also_reads_settings_and_defaults_to_on(monkeypatch):
    monkeypatch.setattr(main.settings, "inline_fact_extract_raw", "0")
    assert _extract(monkeypatch, [("成員甲", "住在台中")]) == []
    monkeypatch.setattr(main.settings, "inline_fact_extract_raw", "")
    assert _extract(monkeypatch, [("成員甲", "住在台中")]) == [["成員乙", "成員甲"]]
    assert memory.list_fact_rows(G) == [("U_A", "成員甲：住在台中")]


def test_inline_writes_go_through_the_shared_path_without_waiting(monkeypatch):
    seen = {}
    sentinel = object()
    monkeypatch.setattr(fact_store, "loaded_embedder", lambda: sentinel)

    def capture(group_id, items, **kwargs):
        seen.update(group_id=group_id, items=list(items), **kwargs)
        return fact_store.Report()

    monkeypatch.setattr(fact_store, "add_person_facts", capture)
    _extract(monkeypatch, [("成員甲", "住在台中"), ("成員乙", "在台北上班"), ("路人", "x")])
    assert seen == {
        "group_id": G,
        "items": [("成員甲", "U_A", "住在台中"), ("成員乙", "U_B", "在台北上班")],
        "source": "inline",
        "embedder": sentinel,
        "lock_mode": "try",
    }


def test_inline_respects_forget_and_the_stricter_filter(monkeypatch):
    memory.add_fact(G, "成員甲：住在台中", user_id="U_A")
    assert memory.remove_fact(G, "台中") == 1
    _extract(monkeypatch, [("成員甲", "住在台中"), ("成員甲", "喜歡咪寶"), ("成員乙", "喜歡爬山")])
    assert memory.list_fact_rows(G) == [("U_B", "成員乙：喜歡爬山")]
    ledger = fact_store._ledger_path().read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["source"] for line in ledger] == ["inline"]


def test_inline_writes_nothing_when_the_hash_key_was_replaced(monkeypatch, caplog):
    import fact_hash

    memory.fact_hash_key()  # 已經用過：DB 記下指紋
    fact_hash.key_path().write_bytes(b"x" * 32)  # 金鑰被換掉
    monkeypatch.setattr(main.gemini_client, "extract_facts", lambda _ctx, speakers=(): [("成員甲", "住在台中")])
    main._maybe_extract_facts(G)
    assert memory.list_fact_rows(G) == []
    assert "FactHashKeyMismatch" in caplog.text and "台中" not in caplog.text
