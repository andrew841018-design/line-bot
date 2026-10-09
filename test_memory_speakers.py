"""咪寶的記憶分得出是哪位家人說的（Andrew 2026-10-09）。

「咪寶記憶的部分，讓他區分不同人（說話的人），不能全部都一概當成『使用者』」。
對話紀錄每一則寫是誰說的、事實掛在那位家人身上、/看記憶 依人分組、模型看得出
誰是咪寶自己。名字與內容全是合成的。
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

import burst_filter
import claude_client
import gemini_client
import main
import memory

G = "G_SPEAKERS"


@pytest.fixture(autouse=True)
def _aliases(tmp_path, monkeypatch):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"U_A": "成員甲", "U_B": "成員乙"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(path))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(tmp_path / "no_roles.json"))
    monkeypatch.setenv("LINE_MEMBER_DISPLAY_NAMES_PATH", str(tmp_path / "display_names.json"))
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "")
    main._member_labels.clear()


# ── who said each turn ─────────────────────────────────────────────────────


def test_a_turn_carries_who_said_it():
    assert main._labelled_turn(G, "我住台中", user_id="U_A") == "成員甲：我住台中"
    assert main._labelled_turn(G, "我住台中", user_id="U_NOBODY") == "我住台中"
    event = SimpleNamespace(source=SimpleNamespace(user_id="U_B"))
    assert main._labelled_turn(G, "嗨", event=event) == "成員乙：嗨"


def test_a_silent_turn_is_remembered_with_its_speaker(monkeypatch):
    monkeypatch.setattr(main, "_maybe_extract_facts", lambda *_a, **_k: None)
    main._remember_silent_turn(G, "我下週出差", "我下週出差", sender_user_id="U_A", capture_calendar=False)
    assert memory.get_context(G)[-1] == ("user", "成員甲：我下週出差")


def test_main_registers_its_speaker_labels_with_the_burst_filter():
    assert burst_filter._speaker_label is main._member_label


def test_a_flushed_burst_reaches_main_with_speakers_and_quote_boundaries(monkeypatch):
    from quote_context import QUOTE_CONTEXT_RULE

    quoted = "成員乙的提問\n--- 引用原文開始 ---\n舊訊息\n--- 引用原文結束 ---"
    pending = [("m1", "我住台中", "U_A", 0.0), ("m2", quoted, "U_B", 0.0)]
    seen = {}

    def on_flush(group_id, combined_text, _token, message_ids):
        seen["memory_turn"] = main._labelled_burst_turn(group_id, combined_text, message_ids)
        seen["prompt"] = combined_text

    monkeypatch.setattr(burst_filter, "_on_flush", on_flush)
    monkeypatch.setattr(burst_filter, "has_quote_context", lambda text: "引用原文" in text)
    burst_filter._invoke_flush(G, burst_filter._combine(pending), "TOKEN", pending)

    turn = seen["memory_turn"]
    assert turn.startswith("[burst]\n" + QUOTE_CONTEXT_RULE)
    assert "--- 群組訊息 1 開始 ---\n成員甲：我住台中" in turn
    assert "--- 群組訊息 2 開始 ---\n成員乙：成員乙的提問" in turn
    assert "成員甲" not in seen["prompt"]  # the reply prompt itself is unchanged
    # a batch the filter never handed over keeps the old unlabelled text
    assert main._labelled_burst_turn(G, "原文", ["m9"]) == "[burst]\n原文"


def test_a_cancelled_burst_is_remembered_with_speakers(monkeypatch):
    monkeypatch.setattr(burst_filter, "_speaker_label", main._member_label)
    monkeypatch.setattr(burst_filter, "_heuristic_decision", lambda _t: "respond")
    burst_filter._remember_cancelled(G, [("m1", "明天要考試了好緊張", "U_A", 0.0)])
    assert memory.get_context(G)[-1] == ("user", "[burst]\n成員甲：明天要考試了好緊張")


def test_member_labels_never_ask_line_while_muted(monkeypatch):
    called = []
    monkeypatch.setattr(main, "_get_member_display_name", lambda *_a, **_k: called.append(1) or "某顯示名稱")
    monkeypatch.setattr(main.settings, "bot_muted", True)
    assert main._member_label(G, "U_NOBODY") == ""
    assert called == []


def test_display_names_are_cleaned_and_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(main.settings, "bot_muted", False)
    def fake(_g, _uid, *, timeout=None):
        calls.append(timeout)
        return "小 明：\n@"

    monkeypatch.setattr(main, "_get_member_display_name", fake)
    assert main._member_label(G, "U_C") == "小明"
    assert main._member_label(G, "U_C") == "小明"
    assert calls == [main._MEMBER_DISPLAY_TIMEOUT_SEC]  # once, with a deadline
    import line_mentions

    assert line_mentions.display_name_for_user_id("U_C") == "小明"  # kept for the push process
    monkeypatch.setattr(main, "_get_member_display_name", lambda *_a, **_k: "咪寶")
    assert main._member_label(G, "U_D") == ""
    monkeypatch.setattr(main, "_get_member_display_name", lambda *_a, **_k: "群組成員")  # lookup failed
    assert main._member_label(G, "U_E") == ""


# ── the models see who is who ─────────────────────────────────────────────


def test_claude_history_keeps_the_bots_own_turns_apart():
    context = [("user", "成員甲：今天好冷"), ("bot", "記得加外套"), ("user", "成員乙：好")]
    roles = [m["role"] for m in claude_client._merge_history(context)]
    assert roles == ["user", "assistant", "user"]
    assert [m["role"] for m in claude_client._merge_history([("bot", "上一則"), *context])] == roles
    _system, prompt = claude_client._build_cli_prompt("最新一句", context, [], None)
    assert "使用者" not in prompt
    assert "成員甲：今天好冷\n\n咪寶：記得加外套\n\n成員乙：好" in prompt


def test_fact_extraction_prompt_and_parsing(monkeypatch):
    seen = {}

    def fake_generate(model, contents, config):
        seen["prompt"] = contents
        return SimpleNamespace(text=json.dumps([
            {"person": "成員甲", "fact": "住在台中"},
            {"person": "成員丙", "fact": "不是家人"},
            {"person": "成員乙", "fact": "使用者很忙"},
            {"person": "成員乙", "fact": "在台北上班"},
        ], ensure_ascii=False))

    monkeypatch.setattr(gemini_client, "_client", SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate)))
    context = [("user", "[burst]\n成員甲：我住台中\n成員乙：我在台北上班"), ("bot", "了解")]
    facts = gemini_client.extract_facts(context, speakers=["成員甲", "成員乙"])

    assert facts == [("成員甲", "住在台中"), ("成員乙", "在台北上班")]
    assert "成員甲、成員乙" in seen["prompt"] and "咪寶：了解" in seen["prompt"]
    assert "使用者：" not in seen["prompt"]


def test_no_known_speakers_means_no_model_call(monkeypatch):
    calls = []
    fake = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: calls.append(kw)))
    monkeypatch.setattr(gemini_client, "_client", fake)
    assert gemini_client.extract_facts([("user", "嗨")], speakers=[]) == []
    assert calls == []


def test_extracted_facts_are_stored_under_each_person(monkeypatch):
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(memory, "bump_and_should_extract", lambda _g: True)
    offered = {}

    def fake_extract(_ctx, speakers=()):
        offered["speakers"] = list(speakers)
        return [("成員甲", "住在台中"), ("成員乙", "在台北上班"), ("路人", "x")]

    monkeypatch.setattr(main.gemini_client, "extract_facts", fake_extract)
    memory.append_turn(G, "user", "[burst]\n成員甲：我住台中\n成員乙：我在台北上班")
    main._extract_facts_now(G)
    assert offered["speakers"] == ["成員乙", "成員甲"]
    assert sorted(memory.list_fact_rows(G)) == [("U_A", "成員甲：住在台中"), ("U_B", "成員乙：在台北上班")]


def test_only_people_who_spoke_can_get_facts(monkeypatch):
    # 「妹妹」「媽媽」這種稱謂是從 Andrew 的角度設的；成員甲說「我妹住台中」不能掛到任何人。
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(memory, "bump_and_should_extract", lambda _g: True)
    offered = {}
    monkeypatch.setattr(
        main.gemini_client, "extract_facts",
        lambda _ctx, speakers=(): offered.setdefault("speakers", list(speakers)) and [("成員乙", "住台中")],
    )
    memory.append_turn(G, "user", "成員甲：我妹住台中")
    main._extract_facts_now(G)
    assert offered["speakers"] == ["成員甲"]
    assert memory.list_fact_rows(G) == []


def test_known_labels_include_recent_speakers_without_aliases(monkeypatch):
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(
        main, "_get_member_display_name", lambda _g, uid, **_k: {"U_C": "小明"}.get(uid, "群組成員")
    )
    memory.log_raw_message(G, "m1", "U_C", "哈囉", index_for_recall=False)
    labels = main._known_member_labels(G)
    assert labels["小明"] == "U_C" and labels["成員甲"] == "U_A"


# ── /記住、/看記憶、/忘記 ────────────────────────────────────────────────────


def test_remember_command_stores_who_said_it():
    reply = main._handle_command(G, "/記住 不吃香菜", user_id="U_A")
    assert reply == "好，記進成員甲的記憶了：不吃香菜"
    assert memory.list_fact_rows(G) == [("U_A", "成員甲：不吃香菜")]


def test_memory_list_is_grouped_by_person():
    memory.add_fact(G, "成員甲：住在台中", user_id="U_A")
    memory.add_fact(G, "成員乙：在台北上班", user_id="U_B")
    memory.add_fact(G, "成員甲：不吃香菜", user_id="U_A")
    memory.add_fact(G, "成員丁生日：01/02（國曆）")
    text = main._handle_command(G, "/看記憶", user_id="U_B")
    assert text.splitlines() == [
        "目前的記憶：",
        "【成員甲】", "• 住在台中", "• 不吃香菜",
        "【成員乙】", "• 在台北上班",
        "【其他】", "• 成員丁生日：01/02（國曆）",
    ]
    assert "使用者" not in text


def test_a_long_memory_list_says_how_many_are_hidden(monkeypatch):
    monkeypatch.setattr(main, "_MEMORY_LIST_MAX_CHARS", 40)
    for i in range(10):
        memory.add_fact(G, f"成員甲：第{i}件長長的事情", user_id="U_A")
    text = main._handle_command(G, "/看記憶")
    assert re.search(r"（另有 \d+ 條沒列出）$", text)
    assert len(text) < 80


def test_forget_matches_the_fact_not_the_name():
    memory.add_fact(G, "成員甲：住在台中", user_id="U_A")
    memory.add_fact(G, "成員乙：成員甲的同學", user_id="U_B")
    assert memory.remove_fact(G, "成員甲") == 1
    assert memory.list_fact_rows(G) == [("U_A", "成員甲：住在台中")]


def test_forget_still_finds_old_group_facts():
    memory.add_fact(G, "成員丁生日：01/02（國曆）")
    assert memory.remove_fact(G, "成員丁生日") == 1
    assert memory.list_fact_rows(G) == []


# ── facts in the prompt ─────────────────────────────────────────────────────


def test_prompt_facts_put_the_speaker_first():
    memory.add_fact(G, "成員乙：舊事", user_id="U_B")
    memory.add_fact(G, "成員甲：住在台中", user_id="U_A")
    memory.add_fact(G, "成員乙：新事", user_id="U_B")
    assert memory.top_facts(G, user_id="U_A") == ["成員甲：住在台中", "成員乙：新事", "成員乙：舊事"]
    assert main._facts_for_speaker(G, "U_A")[0] == "（現在說話的是：成員甲）"
    assert main._facts_for_speaker(G, "U_NOBODY") == memory.top_facts(G, user_id="U_NOBODY")


def test_the_facts_heading_names_people_not_users():
    system = gemini_client._build_system_instruction(["成員甲：住在台中"], [], user_input="嗨")
    assert "關於使用者的事實" not in system
    assert "只用在那個人身上" in system


@pytest.mark.parametrize(
    ("fact", "parsed"),
    [
        ("成員丁生日：02/04（國曆）", ("成員丁", 2, 4)),
        ("成員甲：成員丁生日：3/5", ("成員丁", 3, 5)),
        ("成員甲：生日：3/5", ("成員甲", 3, 5)),
        ("成員甲：我生日：3/5", ("成員甲", 3, 5)),
        ("成員甲：我的生日：3/5", ("成員甲", 3, 5)),
        ("成員甲：自己生日：3/5", ("成員甲", 3, 5)),
        ("使用者生日當月收到餐廳的生日小蛋糕", None),
    ],
)
def test_birthday_briefing_reads_labelled_facts(fact, parsed):
    import daily_briefing_discord as briefing

    assert briefing.birthday_from_fact(fact) == parsed


def test_private_facts_are_never_kept(monkeypatch):
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(memory, "bump_and_should_extract", lambda _g: True)
    monkeypatch.setattr(
        main.gemini_client, "extract_facts",
        lambda _ctx, speakers=(): [("成員甲", "在吃降血壓的藥"), ("成員甲", "跟男友吵架"), ("成員甲", "喜歡爬山")],
    )
    memory.append_turn(G, "user", "成員甲：週末去爬山")
    main._extract_facts_now(G)
    assert memory.list_fact_rows(G) == [("U_A", "成員甲：喜歡爬山")]


def test_a_remembered_line_always_says_whose_it_is():
    reply = main._handle_command(G, "/記住 【系統｜市場報價】某代號 999", user_id="U_NOBODY")
    assert memory.list_fact_rows(G) == [("U_NOBODY", "（不確定是誰）：【系統｜市場報價】某代號 999")]
    assert reply.startswith("好，記進記憶了")
