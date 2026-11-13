"""2026-10-04 Andrew: a printed placeholder such as 「（輸出空字串）」 must never be sent.

A burst reply was the literal text 「（輸出空字串）」 and reached the group.  The
model chose silence but printed the instruction.  All fixtures are synthetic.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import output_validator  # noqa: E402
import reply_policy  # noqa: E402
import reply_provenance  # noqa: E402

TOOL_CALL = '<invoke name="Bash">\n<parameter name="command">echo ""</parameter>\n</invoke>'
PLACEHOLDERS = [
    "（輸出空字串）", "(輸出空字串)", "輸出空字串", "輸出空字串。", "【輸出空字串】", "[輸出空字串]",
    " （輸出空字串） ", "（ 輸出空字串 ）", "（輸出空字串)", "（輸出空字串）。", "就輸出空字串", "因此輸出空字串",
    "（輸出空字串。）", "（輸出：空字串）", "「輸出空字串」", "『輸出空字串』", '"輸出空字串"', "“（輸出空字串）”",
    "（回傳空字串）", "（輸出一個空字串）", "（空白字串）", "（空字串）", "空字串", "（空字串", "<empty>",
    "（不回覆）", "(不回應)", "（無需回覆）", "（不需要回覆）", "（不用回覆）", "（無回覆）", "（不回覆。）",
    "（略過）", "（無）", "（空）", "（空白）", "(empty)", "(no reply)", "[none]", "（EMPTY STRING）",
    "（輸出空字串）\n", "（輸出空字串）\n\n這只是家人閒聊，就不多說了。", "\n\n（依規則輸出空字串）\n因為只是閒聊。",
    "（輸出空字串，因為只是閒聊）", "（依規則輸出空字串）", "（這則不回覆：只是閒聊）",
    "**（輸出空字串）**", "*（輸出空字串）*", "`（輸出空字串）`", "> （輸出空字串）", "# （輸出空字串）",
    "- （輸出空字串）", "(empty string)", "( no reply )",
    "\u3000（輸出空字串）\u3000", "\u00a0（輸出空字串）", "\ufeff（輸出空字串）", "（輸出\u200b空字串）",
    "（輸出空字串）\ufe0f", "（輸出空字串）\r\n這只是閒聊。",
    # the instruction echoed with its condition (burst prompt, NO_REPEAT_CONTRACT, persona rules)
    "（沒有這些內容就輸出空字串）", "(沒有這些內容就輸出空字串，不要附和、不要重述。)",
    "（沒有實質可補充內容，輸出空字串）", "（沒有實質可補充內容時輸出空字串）", "（三種都沒有，就只輸出空字串）",
    "就只輸出空字串。", "（就只輸出空字串）", "（只想附和，直接輸出空字串）", "（沒有新內容，輸出空字串）",
    "（輸出\"\"）", "（沉默）", "（保持沉默）", "（靜默）", "▌ （輸出空字串）",
    # the placeholder with the model's explanation on the same line
    "（輸出空字串）這只是閒聊。", "（輸出空字串） 這則只是閒聊，不需要回覆。", "（輸出空字串）🙂",
    # more no-reply notes, and the instructions echoed without brackets
    "（不要回覆）", "（無須回覆）", "（不回）", "（免回）", "（輸出空白）", "（依規則：不回覆）", "（這則不用回）",
    "沒有這些內容就輸出空字串，不要附和、不要重述。", "沒有實質可補充內容時輸出空字串，不說明沉默原因。",
    "三種都沒有，就只輸出空字串。", "只想附和、稱讚或表示同意時，直接輸出空字串，不要回。",
    "判定沒有可補充價值時，直接輸出空字串。",
    "（輸出空白）這則沒有需要補充的內容。", "（輸出\"\"）這則沒有需要補充的內容。", "（輸出空白）\n這則沒有需要補充的內容。",
    "沒有實質答案就輸出空字串。", "仍沒有實質答案就輸出空字串。", "沒有人直接問你時輸出空字串。",
    "<br>", "<br/>", "<br />", "</br>", "<BR>", "<br>\n</br>", "<br><br>",
    "不確定該怎麼回就輸出空字串。", "不知道就輸出空字串。", "不知道要回什麼就輸出空字串。", "如果不確定怎麼回，就輸出空字串。",
    "沒把握就只輸出空字串。", "（不確定該怎麼回就輸出空字串）",
    # any bare tag, also longer than _MARKER_MAX_CHARS, and what the printed tool call was running
    "<p></p>", "<no_reply/>", "<empty/>", 'echo ""', "```bash\necho \"\"\n```", "```shell\necho \"\"\n```", "```sh\necho \"\"\n```", 'echo ""\nies',
    "<br/>" * 13, "這只是閒聊。\n" + "<br>" * 17,
    # a bare tag next to the model's reason for silence
    "<br>\n\n這題不用回，我先不插話。", "這題不用回，我先不插話。\n\n<br>", "</br>\n這只是閒聊。", "<no_reply/>\n這只是閒聊。",
    # a tool call printed as text (2026-11-04), alone, cut off, or after other words
    TOOL_CALL, f"{TOOL_CALL}\nies", f"{TOOL_CALL}\niawait", f"{TOOL_CALL}\n</parameter>",
    f"我來處理。\n{TOOL_CALL}", '<invoke name="WebSearch">', "</invoke>", '<parameter name="query">台北天氣',
    '<ns:invoke name="Bash">', '<ns1:invoke name="Bash">', "</ns1:invoke>", "<function_calls>", "</function_calls>",
    "<tool_use>", "<tool_call>", "<tool_calls>", '<INVOKE NAME="Bash">', "我來處理。\n<function_calls>", "我來處理。\n</invoke>",
    # the model explains its silence, then prints the instruction (2026-11-06)
    "這題不用回，我先不插話。\n\n（輸出空字串）", "這只是閒聊。\n（輸出空字串）", "只是打招呼。\n（依規則輸出空字串）",
    "這題不用回。\n\n**（輸出空字串）**", "這題不用回。\n（輸出\"\"）", "這題不用回。\n（輸出空白）。",
    "這題不用回，我先不插話。（輸出空字串）", "這題不用回，（輸出空字串）。", "這題不用回（輸出空字串）", "這題不用回～（輸出空字串）",
    "這題不用回。（依規則輸出空字串）", "這題不用回。（輸出空字串。）", "這題不用回。\n（輸出空字串）🙂", "這題不用回（輸出空字串）……",
    "這題不用回。\n（就輸出空字串）", "這題不用回。\n（輸出：空字串）", "（空字串）\n這只是閒聊。",
    "這只是閒聊。\n\n\"\"", "這只是閒聊。\n「」", "這只是閒聊。\n輸出空字串。", "這只是閒聊。\n（空字串）",
    # the instruction next to the model's reason
    "這只是閒聊。\n（沒有這些內容就輸出空字串）", "這只是閒聊。\n（不確定該怎麼回就輸出空字串）",
    "這只是閒聊。\n不確定該怎麼回就輸出空字串。", "不確定該怎麼回就輸出空字串。\n這題不是在問我。",
    "這只是閒聊。不確定該怎麼回就輸出空字串。", "這題不用回，不知道就輸出空字串。",
    "不確定該不該回就輸出空字串。", "不確定要不要回就輸出空字串。", "不知道怎麼回答就輸出空字串。", "不確定該說什麼就輸出空字串。",
    "（決定不回）", "（這題不用回）", "（本題不回）", "這題輸出空字串。",
    # what the CLI is told to print for silence
    "NO_REPLY", "NO_REPLY。", "`NO_REPLY`", "**NO_REPLY**", "（NO_REPLY）", "NO REPLY", "no_reply", "No Reply", "noreply。",
]
ORDINARY = [
    "週日傍晚往南部的高鐵人多，建議先訂票，別現場買。",
    "請將手機設成靜默模式。",
    "保持沉默是這篇小說主角的選擇。",
    "空字串在 Python 裡是 False。",
    "這個函式會輸出空字串，記得先檢查輸入。",
    "輸出空字串之後，前端要記得判斷。",
    "回傳空字串，這樣前端才不會壞掉。",
    "Python 裡這些值都是 False：\n0\n空字串\nNone",
    "空字串\n寫成兩個引號，長度是 0。",
    "欄位沒有值時，請這樣處理：\n回傳空字串\n不要回傳 None，否則前端會顯示 null。",
    "範例：\n```\nif not s:\n    return ''  # （空字串）\n```",
    "_空字串_ 的長度是 0。",
    "如果欄位是空值（例如空字串）就先補上預設值。",
    "（空字串和 None 不一樣）",
    "詐騙簡訊的處理方式：\n（不回覆）\n先截圖，再封鎖並向 165 查證。",
    "（不回覆）\n先截圖，再封鎖並向 165 查證。",
    "1. **不要回覆**，直接封鎖。",
    "副作用：\n（無）",
    "午餐：（無）",
    "要不要回覆？（不回覆的話對方會等）",
    "（不回覆的話對方會等）",
    "（不回家吃飯的話要先說）",
    "先回覆：\n（不回覆會失禮）",
    "不回覆也沒關係，對方會理解的。",
    "不回覆。直接刪除就好。",
    "回覆空字串會被拒絕嗎？",
    "略過這一步也可以。",
    "（附註）末班車大約 23:00。",
    "建議：\n1. 先訂票\n2. 留意末班車",
    "📬 補回之前漏掉的訊息\n\n原文：\n請解釋這個輸出：\n（輸出空字串）\n\n回應：\n這是程式沒有東西可印。",
    "none of the above",
    "記得帶外套。\r\n晚上會降溫。",
    "記得\u3000帶外套。",
    "「（輸出空字串）」是我內部指令外洩，請忽略。",
    "（注意：這個 API 失敗時會回傳空字串）\n所以呼叫端要先判斷長度。",
    "（失敗時回傳空字串）\n所以呼叫端要先判斷長度。",
    "失敗時，回傳空字串。",
    "（不回應就好）",
    "(none of the above)",
    "（補充）高鐵早鳥票要提前 28 天搶。",
    "1. 先備份\n2. 再升級\n（無）",
    "保持沉默也是一種回答。",
    "（沉默的螺旋是一種傳播學理論）",
    "（無，今天沒有安排）",
    "（略過，這題跟你的問題無關）",
    "（無，不過建議提前 28 天搶早鳥票）",
    "【空字串、None 和 0 的差別】\n三者在 if 判斷裡都是 False。",
    "（空字串、None 和 0）\n這三個在 Python 都是 False。",
    "（空字串：長度為 0 的字串）\n在 Python 裡 bool('') 是 False。",
    "沒有資料時回傳空字串。",
    "（回家路上小心）",
    "（不要回頭）",
    "沒有問題的話我們週六見。",
    "沒有輸出結果就重新整理一次。",
    # programming answers and reminders that only look like instructions (post-implementation review)
    "若沒有姓名，不要輸出空字串，改用匿名。",
    "（這個 API 成功時不會回傳空字串）",
    "（查詢失敗時不要回傳空字串，請顯示錯誤訊息）",
    "沒有資料時輸出空字串。",
    "（不要回傳空字串，改拋 ValueError）",
    "如果沒有姓名就輸出空字串，避免顯示 None。",
    "（不用回覆，看到後記得帶水壺就好）",
    "（回傳空字串，不要拋例外）",
    "（失敗時回傳空字串）",
    "沒有內容不要輸出空字串。",
    "（空字串：長度為 0 的字串）",
    "換行可以用 <br> 標籤。",
    "第一行<br>第二行",
    "不確定型別就輸出空字串。",
    "不知道欄位名稱時輸出空字串。",
    "不確定該怎麼回就先問清楚再回。",
    "不知道就不要輸出空字串。",
    "<parameter 設定要改成 3。",
    "invoke 是呼叫的意思，parameter 是參數。",
    "網址寫 <invoke> 不會有作用。",
    "a<b 而且 c>d，所以順序要換。",
    "<3 愛你",
    "執行結果會回傳空字串：\n（回傳空字串）",
    "遇到這種簡訊：\n先截圖\n（不回覆）",
    "內部指令長這樣：\n「（輸出空字串）」",
    "請解釋這個輸出：\n（輸出空字串）\n\n這是程式輸出的空值。",
    "no reply 是沒有回覆的意思。",
    "他說 no reply，我也沒辦法。",
    "晚上吃什麼？\nnoreply@example.com 寄來的信不要回。",
    "事項：改 <tool_use 區塊",
    "遇到詐騙電話：\n先掛斷\n（保持沉默）",
    "這題我只能說……\n（沉默）",
    "欄位說明：\n不確定型別就輸出空字串。",
    "「<br>" + "x" * 80,
    "<br>" * 16 + "明天下午三點見",
    "提醒我 11/13 檢查 NO_REPLY 統計",
    "寄件人 NO_REPLY@example.com 不是銀行常用網域。",
    "這類信件常見的寄件人：\n- info\n- noreply",
    "「無回應」英文可以寫：\nNo reply",
    "**noreply**\n是系統自動寄信用的地址",
    "<https://example.com/a>",
    "<someone@example.com>",
    "<div>內容</div> 是區塊標籤的範例。",
    "<br>OK",
    "Claude API 回傳的 tool_use 區塊要照順序處理。",
    "echo 是回音的意思。",
]


@pytest.mark.parametrize("text", ["", "  ", '""', "「」", *PLACEHOLDERS])
def test_placeholders_are_empty_markers(text):
    assert reply_policy.is_empty_marker(text)


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_replies_are_not_empty_markers(text):
    assert not reply_policy.is_empty_marker(text)


def test_non_text_is_not_a_marker_and_none_is_empty():
    assert reply_policy.is_empty_marker(None)
    assert not reply_policy.is_empty_marker(123)


def test_printed_placeholder_needs_visible_text():
    assert reply_policy.is_printed_placeholder("（輸出空字串）")
    assert not reply_policy.is_printed_placeholder("")
    assert not reply_policy.is_printed_placeholder("  ")
    assert not reply_policy.is_printed_placeholder(None)
    assert not reply_policy.is_printed_placeholder("記得帶外套。")


@pytest.mark.parametrize("text", [
    "<" * 200_000, "（" * 200_000, "<a" * 100_000, "</" * 100_000, "<invoke " * 30_000, "<a:" * 60_000,
    "<" + " " * 200_000, "<" + "a" * 200_000, "<invoke" + " " * 200_000, "<invoke name" + " " * 200_000,
    "輸出" * 100_000, "noreply" * 30_000, "\n".join(["（輸出空字串"] * 30_000),
    " " * 5000 + "x", "\n" * 5000 + "a", "（ " * 2500 + "輸出", "今天天氣很好，" * 1000,
])
def test_long_replies_are_checked_in_linear_time(text):
    from test_support_timing import cpu_seconds

    assert cpu_seconds(reply_policy.is_empty_marker, text) < 1.0  # the previous regex backtracked for minutes
    assert not reply_policy.is_empty_marker(text)


@pytest.mark.parametrize("text", PLACEHOLDERS)
def test_last_gate_blocks_placeholders(text):
    result = output_validator.validate_outbound_text(text)
    assert (result.ok, result.text, result.reason) == (False, "", "empty_output_marker")


@pytest.mark.parametrize("text", ORDINARY)
def test_last_gate_does_not_treat_ordinary_replies_as_placeholders(text):
    assert output_validator.validate_outbound_text(text).reason != "empty_output_marker"


def test_prepared_text_is_empty_even_when_markdown_hides_the_placeholder():
    import main

    assert main._prepare_outbound_text("（輸出空字串）") == ""
    assert main._prepare_outbound_text("**（輸出空字串）**") == ""
    assert main._prepare_outbound_text("# （輸出空字串）") == ""  # _md_to_line makes it 「▌ …」
    assert main._prepare_outbound_text("記得帶外套。") == "記得帶外套。"


def test_placeholder_is_a_rejected_shape_but_empty_text_is_not():
    import main

    assert main._is_user_rejected_degraded_outbound("（輸出空字串）")
    assert main._is_user_rejected_degraded_outbound("**（輸出空字串）**")
    assert not main._is_user_rejected_degraded_outbound("")
    assert not main._is_user_rejected_degraded_outbound("記得帶外套。")
    assert not main._is_system_status_outbound("")
    assert main._is_system_status_outbound("（輸出空字串）")


@pytest.mark.parametrize("reply", [
    "（輸出空字串）", "（輸出空字串）\n\n這只是家人閒聊，就不多說了。", "<br>", "<br>\n</br>", "不確定該怎麼回就輸出空字串。",
    TOOL_CALL, f"{TOOL_CALL}\nies", "這題不用回，我先不插話。\n\n（輸出空字串）", "NO_REPLY",
])
def test_enforce_new_value_reply_turns_a_placeholder_into_no_reply(reply):
    import main

    assert main._enforce_new_value_reply(
        reply, source_text="週末要去露營", request_text="週末要去露營", context=[],
    ) == ""


@pytest.mark.parametrize("reply", [
    "<br>", "</br>", "不確定該怎麼回就輸出空字串。", TOOL_CALL, f"{TOOL_CALL}\nies",
    "這題不用回，我先不插話。\n\n（輸出空字串）", "NO_REPLY",
])
def test_placeholder_is_dropped_without_the_restatement_judge(reply):
    # None of these may depend on the judge, which fails open and skips short sources.
    import main

    with patch("restatement_judge.filter_restatements", side_effect=AssertionError):
        assert main._enforce_new_value_reply(
            reply, source_text="週末要去露營", request_text="週末要去露營", context=[],
        ) == ""


def test_explicit_placeholder_reply_is_not_sent():
    from types import SimpleNamespace

    from linebot.v3.webhooks import MessageEvent, TextMessageContent

    import main

    evt = MagicMock(spec=MessageEvent)
    evt.message = TextMessageContent(id="MSG910", text="咪寶 週末要去露營", quoteToken="qt")
    evt.source = SimpleNamespace(type="group", group_id="GRP001", user_id="U_TEST")
    evt.reply_token = "TOKEN910"
    turns = []
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._build_quoted_block", return_value=""),
        patch("main._requires_public_research", return_value=False),
        patch("main._get_explicit_market_quote_reply", return_value=None),
        patch("main._prefetch_urls", side_effect=lambda t: t),
        patch("main._llm_chat", return_value="（輸出空字串）"),
        patch("main.memory.append_turn", side_effect=lambda *a: turns.append(a)),
        patch("main._try_save_correction"),
        patch("main._maybe_capture_calendar_event"),
        patch("main._mark_inbound_reply_completed_no_reply") as mark_silent,
        patch("main._reply") as mock_reply,
    ):
        main._handle_explicit_text(evt, "GRP001", "週末要去露營")
    mock_reply.assert_not_called()
    assert mark_silent.call_count == 1
    assert mark_silent.call_args.args[0] == "TOKEN910"
    assert [t[1] for t in turns] == ["user"]


@pytest.mark.parametrize("reply", [
    "（輸出空字串）", "<br>", "不確定該怎麼回就輸出空字串。", TOOL_CALL, "這題不用回，我先不插話。\n\n（輸出空字串）", "NO_REPLY",
])
def test_burst_placeholder_reply_is_not_sent(monkeypatch, reply):
    import main

    monkeypatch.setattr(main, "_quota_exhausted_until_ts", 0.0)
    with (
        patch("main.memory.check_fact_cache", return_value=None),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._requires_public_research", return_value=False),
        patch("main._llm_chat", return_value=reply),
        patch("main.memory.append_turn") as append_turn,
        patch("main._maybe_extract_facts"),
        patch("main._finish_burst_without_reply") as finish,
        patch("main._reply") as mock_reply,
    ):
        main._handle_burst_flush("GRP001", "週末要去露營，記得帶外套", "TOKEN911")
    mock_reply.assert_not_called()
    finish.assert_called_once()
    append_turn.assert_not_called()


def test_reply_last_gate_finishes_the_message_without_sending(monkeypatch):
    import main

    monkeypatch.setattr(main.settings, "bot_muted", False)
    # Only the validator inside _prepare_outbound_text may stop it here.
    monkeypatch.setattr(main, "_is_user_rejected_degraded_outbound", lambda *_a: False)
    monkeypatch.setattr(main, "_is_system_status_outbound", lambda *_a: False)
    api = MagicMock()
    monkeypatch.setattr(main, "MessagingApi", api)
    monkeypatch.setattr(main, "_consume_reply_mention_targets", lambda *_a: [])
    marked = MagicMock()
    monkeypatch.setattr(main, "_mark_inbound_reply_completed_no_reply", marked)
    # The suppressed-primary branch of the real _reply: nothing reaches the SDK and
    # the inbound is finished (the validator and the rejected-shape check both see it).
    assert main._reply("TOKEN912", "**（輸出空字串）**", group_id=None) is False
    api.assert_not_called()
    marked.assert_called_once()
    assert marked.call_args.args[0] == "TOKEN912"


def test_claude_cli_printing_the_placeholder_is_a_no_reply_decision(monkeypatch, tmp_path):
    """The incident's real path: the CLI exits 0 and prints the placeholder."""
    import subprocess

    import claude_client as cc

    monkeypatch.setattr(cc, "_STATE_FILE", tmp_path / "claude-state.json")
    monkeypatch.setattr(cc.settings, "claude_use_cli", True)
    monkeypatch.setattr(cc.settings, "claude_api_key", "")
    monkeypatch.setattr(cc, "_cli_executable", lambda: "/usr/local/bin/claude-fake")
    monkeypatch.setattr(cc, "_build_cli_prompt", lambda *_a: ("SYS-合成", "USER-合成"))
    monkeypatch.setattr(cc.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(cc.subprocess, "run", lambda args, **_k: subprocess.CompletedProcess(
        args, 0, stdout="（輸出空字串）\n", stderr=""))
    assert cc.chat("x", [], []) == ""


@pytest.mark.parametrize("reply", [
    "<br>", "</br>", TOOL_CALL, f"{TOOL_CALL}\nies", f"{TOOL_CALL}\n</parameter>", "不確定該怎麼回就輸出空字串。",
    "這題不用回，我先不插話。\n\n（輸出空字串）", "NO_REPLY",
])
def test_new_shapes_do_not_survive_the_line_formatter(reply):
    # _md_to_line runs before the validator on everything that reaches LINE.
    import main

    assert main._prepare_outbound_text(reply) == ""


def test_reminder_receipt_is_not_a_placeholder():
    receipt = "已新增提醒\n時間：11/08（六）15:00\n事項：改 <parameter 設定，帶 <invoke 文件"
    assert not reply_policy.is_empty_marker(receipt)
    assert output_validator.validate_outbound_text(receipt).ok


def test_llm_chat_marks_a_silent_claude_reply_as_dropped():
    # The quoted-media, audio and file paths finish the message on dropped(), and send a fixed
    # line or leave it open otherwise.
    import main

    with patch("claude_client.chat", return_value=""):
        assert main._llm_chat("成員甲：晚安", [], [], []) == ""

    assert reply_provenance.dropped()


def test_llm_chat_leaves_a_gemini_failure_unmarked():
    import main

    with patch("claude_client.chat", return_value=None), patch("main._gemini_llm_chat", return_value=""):
        assert main._llm_chat("成員甲：晚安", [], [], []) == ""

    assert not reply_provenance.dropped()


def test_quoted_video_is_finished_quietly_when_claude_stays_silent(monkeypatch):
    from types import SimpleNamespace

    import main

    closed = []
    monkeypatch.setattr(main.memory, "get_raw_message_meta", lambda *_a: {"description": "舊影片摘要"})
    monkeypatch.setattr(main.memory, "get_context", lambda *_a: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda *_a: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_a: [])
    monkeypatch.setattr(main, "_mark_inbound_reply_completed_no_reply", lambda token, **_k: closed.append(token))

    with patch("claude_client.chat", return_value=""):
        assert main._handle_quoted_media_description_fallback(
            SimpleNamespace(reply_token="synthetic"), "group", "合理嗎", "quoted", "影片"
        )

    assert closed == ["synthetic"]
