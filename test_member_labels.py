"""每日記憶 job 的離線稱呼（2026-10-11）：member_labels.resolve_labels_offline。

不呼叫 LINE API；顯示名稱撞名、和別名／角色／短名相同都不用；兩個人同一個稱呼就誰都不給。
名字全是合成的。
"""

from __future__ import annotations

import json
import socket
import urllib.request

import pytest

import line_mentions
import main
import member_labels


@pytest.fixture(autouse=True)
def _aliases(tmp_path, monkeypatch):
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"U_A": "成員甲", "U_B": "成員乙"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(tmp_path / "no_roles.json"))
    monkeypatch.setattr(member_labels, "_food_alias", lambda _uid: "")


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("resolve_labels_offline must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(main, "_get_member_display_name", refuse)


def test_main_reexports_the_same_cleaner():
    assert main._clean_member_label is member_labels._clean_member_label
    assert main._RESERVED_LABEL_STARTS is member_labels._RESERVED_LABEL_STARTS


def test_aliases_come_first_and_display_names_fill_in(no_network):
    line_mentions.remember_display_name("U_A", "別的名字")
    line_mentions.remember_display_name("U_C", "成員丁")
    labels = member_labels.resolve_labels_offline(["U_A", "U_C", "U_NOBODY", "__bot__", None, ""])
    assert labels == {"成員甲": "U_A", "成員丁": "U_C"}


def test_a_display_name_two_people_use_is_not_used(no_network):
    line_mentions.remember_display_name("U_C", "成員丁")
    line_mentions.remember_display_name("U_D", "成員丁")
    assert member_labels.resolve_labels_offline(["U_C", "U_D"]) == {}
    assert member_labels.resolve_labels_offline(["U_C"]) == {}  # 另一位雖然這次沒說話，也撞名


@pytest.mark.parametrize("name", ["成員甲", "員甲", "媽媽"])
def test_a_display_name_equal_to_an_alias_role_or_short_name_is_not_used(tmp_path, monkeypatch, name, no_network):
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({"媽媽": "成員甲"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(roles))
    line_mentions.remember_display_name("U_C", name)
    assert member_labels.resolve_labels_offline(["U_A", "U_C"]) == {"成員甲": "U_A"}


def test_a_label_two_user_ids_share_is_nobodys(tmp_path, monkeypatch, no_network):
    aliases = tmp_path / "dup_aliases.json"
    aliases.write_text(json.dumps({"U_A": "成員甲", "U_X": "成員甲"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    assert member_labels.resolve_labels_offline(["U_A", "U_X"]) == {}


def test_the_food_alias_table_is_the_fallback(monkeypatch, no_network):
    monkeypatch.setattr(member_labels, "_food_alias", lambda uid: {"U_F": "成員庚"}.get(uid, ""))
    assert member_labels.resolve_labels_offline(["U_F"]) == {"成員庚": "U_F"}


def test_display_names_are_cleaned_like_main_does(no_network):
    line_mentions.remember_display_name("U_C", "咪寶")
    line_mentions.remember_display_name("U_D", "成員 丁：")
    assert member_labels.resolve_labels_offline(["U_C", "U_D"]) == {"成員丁": "U_D"}


# ── 修正第一輪 #2：撞名只和最近說過話的人比（和 main._display_name_taken 一樣） ──────────


def test_an_old_name_of_someone_quiet_does_not_block_a_new_member(no_network):
    line_mentions.remember_display_name("U_OLD", "成員丁")  # 退群或改名的人，名字還留在檔裡
    line_mentions.remember_display_name("U_NEW", "成員丁")
    recent = ["U_NEW", "U_A"]
    assert member_labels.resolve_labels_offline(["U_NEW", "U_A"], recent_senders=recent) == {
        "成員丁": "U_NEW", "成員甲": "U_A",
    }


def test_two_recent_speakers_with_one_name_both_go_without(no_network):
    line_mentions.remember_display_name("U_C", "成員丁")
    line_mentions.remember_display_name("U_D", "成員丁")
    recent = ["U_C", "U_D"]
    assert member_labels.resolve_labels_offline(["U_C"], recent_senders=recent) == {}
    assert member_labels.resolve_labels_offline(["U_C", "U_D"], recent_senders=recent) == {}


def test_aliases_still_win_over_recent_display_names(no_network):
    line_mentions.remember_display_name("U_C", "成員甲")  # 和別名同名的顯示名稱
    assert member_labels.resolve_labels_offline(["U_A", "U_C"], recent_senders=["U_A", "U_C"]) == {
        "成員甲": "U_A",
    }


def test_offline_labels_match_main_for_the_same_recent_senders(monkeypatch, no_network):
    import memory

    group = "G_LABEL_PARITY"
    line_mentions.remember_display_name("U_OLD", "成員丁")
    line_mentions.remember_display_name("U_NEW", "成員丁")
    memory.log_raw_message(group, "m1", "U_NEW", "早安", index_for_recall=False)
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_get_member_display_name", lambda *_a, **_k: "成員丁")
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "")
    recent = memory.recent_raw_senders(group, 300)
    offline = member_labels.resolve_labels_offline(["U_NEW"], recent_senders=recent)
    assert offline == {main._member_label(group, "U_NEW"): "U_NEW"} == {"成員丁": "U_NEW"}
