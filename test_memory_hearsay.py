"""轉述（A 講到家人 B 的事）的寫入與記憶（2026-10-16）：fact_store、memory、/看記憶。

名字與內容全是合成的；嵌入模型一律不用（只比完全相同與字元包含）。
"""

from __future__ import annotations

import json

import pytest

import fact_hash
import fact_store
import main
import memory

G = "G_HEARSAY"


@pytest.fixture(autouse=True)
def _aliases(tmp_path, monkeypatch):
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"U_A": "成員甲", "U_B": "成員乙"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(tmp_path / "no_roles.json"))
    monkeypatch.setenv("LINE_MEMBER_DISPLAY_NAMES_PATH", str(tmp_path / "display_names.json"))
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "")
    main._member_labels.clear()


def _add(items, **kwargs):
    kwargs.setdefault("source", "daily")
    kwargs.setdefault("embedder", None)
    kwargs.setdefault("lock_mode", "wait")
    kwargs.setdefault("allow_hearsay", True)
    kwargs.setdefault("allow_upgrade", True)
    return fact_store.add_person_facts(G, items, **kwargs)


def _told(body="住在台中", subject=("成員乙", "U_B"), said_by="成員甲"):
    return fact_store.FactItem(subject[0], subject[1], body, said_by)


def _ledger():
    path = fact_store._ledger_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _hmac(user_id, stored):
    return fact_hash.fact_hmac(G, user_id, stored, key=memory.fact_hash_key())


# ── 寫入 ─────────────────────────────────────────────────────────────────────


def test_hearsay_is_stored_under_the_subject_with_who_said_it():
    report = _add([_told()], backend="claude")
    assert report.added == 1
    assert memory.list_fact_rows(G) == [("U_B", "成員乙：住在台中（成員甲說的）")]
    (entry,) = _ledger()
    assert (entry["source"], entry["kind"], entry["backend"]) == ("daily", "hearsay", "claude")
    assert entry["fact_hmac"] == _hmac("U_B", "成員乙：住在台中（成員甲說的）")


def test_only_the_job_may_write_hearsay():
    report = _add([_told()], allow_hearsay=False)
    assert (report.added, report.rejected) == (0, {"hearsay_off": 1})
    assert memory.list_fact_rows(G) == []


@pytest.mark.parametrize("said_by", ["成員乙", "成員:甲", "@成員甲", "咪寶"])
def test_a_bad_or_self_referencing_teller_is_refused(said_by):
    report = _add([_told(said_by=said_by)])
    assert report.rejected == {"said_by": 1} and memory.list_fact_rows(G) == []


@pytest.mark.parametrize(("body", "category"), [("腰不好", "scope:judgement"), ("沒胃口", "scope:health"),
                                                ("很會煮菜", "scope:frame"), ("在吃藥", "private")])
def test_hearsay_goes_through_the_normal_filters_and_the_narrower_scope(body, category):
    assert _add([_told(body)]).rejected == {category: 1}


def test_a_model_fact_that_ends_with_a_tag_is_refused_from_every_source():
    for allow in (True, False):
        report = _add([("成員乙", "U_B", "住在台中（成員甲說的）")], allow_hearsay=allow)
        assert report.rejected == {"said_tag": 1}
    assert memory.list_fact_rows(G) == []


def test_the_subjects_own_version_wins_over_hearsay():
    _add([("成員乙", "U_B", "住在台中")])
    report = _add([_told()])
    assert (report.added, report.dup) == (0, 1)
    assert memory.list_fact_rows(G) == [("U_B", "成員乙：住在台中")]


def test_two_people_telling_the_same_thing_keep_one_row():
    _add([_told()])
    assert _add([_told(said_by="成員丙")]).dup == 1
    assert len(memory.list_fact_rows(G)) == 1


# ── 升級：B 本人後來自己說了 ───────────────────────────────────────────────────


def test_the_subject_saying_it_later_turns_the_hearsay_row_into_their_own():
    _add([_told()])
    before = memory.list_fact_rows(G)
    report = _add([("成員乙", "U_B", "住在台中")], backend="gemini")
    assert (report.upgraded, report.added, report.dup) == (1, 0, 0)
    assert memory.list_fact_rows(G) == [("U_B", "成員乙：住在台中")] and len(before) == 1
    upgrade = _ledger()[-1]
    assert (upgrade["kind"], upgrade["backend"]) == ("upgrade", "gemini")
    assert upgrade["fact_hmac"] == _hmac("U_B", "成員乙：住在台中")
    assert upgrade["replaces_hmac"] == _hmac("U_B", "成員乙：住在台中（成員甲說的）")


def test_the_reply_path_never_rewrites_rows():
    _add([_told()])
    report = _add([("成員乙", "U_B", "住在台中")], source="inline", lock_mode="try",
                  allow_hearsay=False, allow_upgrade=False)
    assert (report.upgraded, report.dup) == (0, 1)
    assert memory.list_fact_rows(G) == [("U_B", "成員乙：住在台中（成員甲說的）")]


def test_a_lookalike_row_typed_by_a_person_is_never_upgraded():
    # /記住 照原文存：看起來像轉述，但 ledger 沒有它，當成本人的列
    memory.add_fact(G, "成員乙：住在台中（成員甲說的）", user_id="U_B")
    report = _add([("成員乙", "U_B", "住在台中")])
    assert (report.upgraded, report.dup) == (0, 1)
    assert memory.list_fact_rows(G) == [("U_B", "成員乙：住在台中（成員甲說的）")]


def test_no_upgrades_when_the_ledger_cannot_be_read(monkeypatch):
    _add([_told()])
    monkeypatch.setattr(fact_store, "_ledger_hearsay_hmacs", lambda _group: None)
    assert _add([("成員乙", "U_B", "住在台中")]).upgraded == 0


def test_a_row_forgotten_just_before_the_upgrade_is_not_added_back(monkeypatch):
    _add([_told()])
    real = memory.promote_hearsay_row

    def forget_first(group_id, user_id, old, new):
        memory.remove_fact(G, "台中")
        return real(group_id, user_id, old, new)

    monkeypatch.setattr(memory, "promote_hearsay_row", forget_first)
    report = _add([("成員乙", "U_B", "住在台中")])
    assert (report.upgraded, report.added, report.dup) == (0, 0, 1)
    assert memory.list_fact_rows(G) == []
    assert all(entry["kind"] != "upgrade" for entry in _ledger())


def test_a_failed_ledger_write_puts_the_hearsay_row_back(monkeypatch):
    _add([_told()])

    def broken(*_args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(fact_store, "_append_ledger", broken)
    report = _add([("成員乙", "U_B", "住在台中")])
    assert (report.upgraded, report.error) == (0, 1)
    assert memory.list_fact_rows(G) == [("U_B", "成員乙：住在台中（成員甲說的）")]


def test_dry_run_previews_hearsay_and_upgrades_without_writing():
    _add([_told()])
    report = _add([("成員乙", "U_B", "住在台中"), _told("喜歡爬山")], dry_run=True)
    assert report.decisions == [
        {"decision": "upgrade", "person": "成員乙", "fact": "住在台中"},
        {"decision": "add", "person": "成員乙", "fact": "喜歡爬山", "said_by": "成員甲"},
    ]
    assert memory.list_fact_rows(G) == [("U_B", "成員乙：住在台中（成員甲說的）")]


# ── 上限 ─────────────────────────────────────────────────────────────────────


def test_hearsay_has_its_own_smaller_caps(monkeypatch):
    monkeypatch.setattr(fact_store, "HEARSAY_PER_PAIR_MAX", 2)
    monkeypatch.setattr(fact_store, "HEARSAY_PER_PERSON_MAX", 3)
    bodies = ["喜歡爬山", "不吃牛肉", "愛喝咖啡", "住在台中", "養了一隻貓"]
    report = _add([_told(body) for body in bodies[:3]])
    assert (report.added, report.capped) == (2, 1)  # 同一個人講同一位家人最多 2 條
    report = _add([_told(body, said_by="成員丙") for body in bodies[3:]])
    assert (report.added, report.capped) == (1, 1)  # 這位家人的轉述最多 3 條
    report = _add([("成員乙", "U_B", "在台北上班")])
    assert report.added == 1  # 本人的事不受轉述上限影響


# ── /忘記、/清除記憶、tombstone ───────────────────────────────────────────────


def test_forgetting_a_name_does_not_wipe_everything_that_person_told():
    _add([_told()])
    assert memory.remove_fact(G, "成員甲") == 0
    assert memory.remove_fact(G, "台中") == 1


def test_forgotten_hearsay_does_not_block_the_subject_saying_it_later():
    _add([_told()])
    memory.remove_fact(G, "台中")
    assert _add([_told()]).tombstoned == 1  # 同一則轉述不會再記回來
    assert _add([("成員乙", "U_B", "住在台中")]).added == 1  # B 本人說的照記


def test_a_fact_the_subject_had_forgotten_cannot_come_back_as_hearsay():
    _add([("成員乙", "U_B", "住在台中")])
    memory.remove_fact(G, "台中")
    assert _add([_told()]).tombstoned == 1


def test_clearing_memory_files_hearsay_tombstones_in_their_own_namespace():
    _add([_told()])
    memory.clear_facts(G)
    assert memory.is_fact_tombstoned(G, "U_B", "住在台中", include_hearsay=True)
    assert not memory.is_fact_tombstoned(G, "U_B", "住在台中")


def test_promote_refuses_rows_that_are_not_hearsay_or_still_tagged():
    memory.add_fact(G, "成員乙：住在台中", user_id="U_B")
    assert not memory.promote_hearsay_row(G, "U_B", "成員乙：住在台中", "成員乙：住在台中呀")
    memory.add_fact(G, "成員乙：喜歡爬山（成員甲說的）", user_id="U_B")
    assert not memory.promote_hearsay_row(G, "U_B", "成員乙：喜歡爬山（成員甲說的）", "成員乙：喜歡爬山（成員丙說的）")
    # 新文字已經存在（主鍵衝突）：什麼都不改
    memory.add_fact(G, "成員乙：喜歡爬山", user_id="U_B")
    assert not memory.promote_hearsay_row(G, "U_B", "成員乙：喜歡爬山（成員甲說的）", "成員乙：喜歡爬山")


# ── 誰看得到 ─────────────────────────────────────────────────────────────────


def test_hearsay_shows_in_the_memory_list_but_not_in_reply_prompts():
    _add([("成員乙", "U_B", "喜歡爬山"), _told()])
    text = main._handle_command(G, "/看記憶")
    assert "【成員乙】" in text and "• 住在台中（成員甲說的）" in text and "• 喜歡爬山" in text
    assert memory.top_facts(G, user_id="U_B") == ["成員乙：喜歡爬山"]
    assert memory.top_facts(G) == ["成員乙：喜歡爬山"]



def test_without_upgrades_a_hearsay_row_from_the_same_batch_is_not_rewritten():
    report = _add([_told(), ("成員乙", "U_B", "住在台中")], allow_upgrade=False)
    assert (report.added, report.upgraded, report.dup) == (1, 0, 1)
    assert memory.list_fact_rows(G) == [("U_B", "成員乙：住在台中（成員甲說的）")]
