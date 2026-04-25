"""
Handler & flow tests for main.py — mocked LINE events, no real API calls.
"""

import json
import os
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import main  # noqa: E402
from linebot.v3.webhooks import (  # noqa: E402
    AudioMessageContent,
    FileMessageContent,
    GroupSource,
    ImageMessageContent,
    JoinEvent,
    LeaveEvent,
    MemberJoinedEvent,
    MessageEvent,
    TextMessageContent,
    VideoMessageContent,
)

PASS = 0
FAIL = 0


def check(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}")


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_group_source(group_id="GRP001", user_id="USR001"):
    src = MagicMock(spec=GroupSource)
    src.group_id = group_id
    src.user_id = user_id
    src.type = "group"
    return src


def _make_text_msg(text="你好", msg_id="MSG001"):
    msg = MagicMock(spec=TextMessageContent)
    msg.id = msg_id
    msg.text = text
    msg.mention = None
    msg.quoted_message_id = None
    msg.quote_token = "qt"
    msg.type = "text"
    return msg


def _make_message_event(msg, source=None, redelivery=False, reply_token="TOKEN001"):
    evt = MagicMock(spec=MessageEvent)
    evt.message = msg
    evt.source = source or _make_group_source()
    evt.reply_token = reply_token
    evt.timestamp = int(time.time() * 1000)
    dctx = MagicMock()
    dctx.is_redelivery = redelivery
    evt.delivery_context = dctx
    return evt


def _mock_memory():
    """Patch all memory functions used in handlers."""
    patches = {
        "main.memory.get_context": ([], {}),
        "main.memory.top_facts": ([], {}),
        "main.memory.append_turn": (None, {}),
        "main.memory.bump_and_should_extract": (False, {}),
        "main.memory.get_raw_message": (None, {}),
        "main.memory.check_fact_cache": (None, {}),
        "main.memory.log_raw_message": (None, {}),
        "main.memory.list_facts": ([], {}),
        "main.memory.add_fact": (True, {}),
        "main.memory.remove_fact": (1, {}),
        "main.memory.clear_facts": (1, {}),
        "main.memory.add_filter_rule": (1, {}),
        "main.memory.list_filter_rules": ([], {}),
        "main.memory.delete_filter_rule": (True, {}),
        "main.memory.clear_filter_rules": (1, {}),
        "main.memory.list_rule_drafts": ([], {}),
    }
    return patches


# ═══════════════════════════════════════════════════════════════════════════════
# Test A: _handle_event routing
# ═══════════════════════════════════════════════════════════════════════════════
def test_handle_event_routing():
    print("\n── Test A: _handle_event routing ──")

    # JoinEvent → _handle_join
    join_evt = MagicMock(spec=JoinEvent)
    join_evt.source = MagicMock()
    join_evt.source.group_id = "GRP001"
    join_evt.source.room_id = None
    join_evt.reply_token = "TOKEN"
    with patch("main._handle_join") as mock_join, patch("main._reply"):
        main._handle_event(join_evt)
    check("JoinEvent → _handle_join called", mock_join.called)

    # LeaveEvent → _handle_leave
    leave_evt = MagicMock(spec=LeaveEvent)
    leave_evt.source = MagicMock()
    leave_evt.source.group_id = "GRP001"
    leave_evt.timestamp = 12345
    with patch("main._handle_leave") as mock_leave:
        main._handle_event(leave_evt)
    check("LeaveEvent → _handle_leave called", mock_leave.called)

    # MemberJoinedEvent → ignored
    member_evt = MagicMock(spec=MemberJoinedEvent)
    handled = True
    try:
        main._handle_event(member_evt)
    except Exception:
        handled = False
    check("MemberJoinedEvent → 不爆", handled)

    # Non-MessageEvent (random object) → return
    with patch("main.memory.log_raw_message") as mock_log:
        main._handle_event(MagicMock())
    check("非 MessageEvent → 不 log raw", not mock_log.called)


def test_handle_event_group_filter():
    print("\n── Test B: _handle_event group_id filter ──")
    # Non-group source → skip
    dm_src = MagicMock()
    del dm_src.group_id  # DM has no group_id attribute on GroupSource
    dm_evt = MagicMock(spec=MessageEvent)
    dm_evt.source = MagicMock()  # not GroupSource instance
    dm_evt.message = _make_text_msg()
    dm_evt.delivery_context = MagicMock()
    dm_evt.delivery_context.is_redelivery = False
    with patch("main.memory.log_raw_message") as mock_log:
        main._handle_event(dm_evt)
    check("非 GroupSource → 不 log raw", not mock_log.called)

    # Allowed group filter
    orig_allowed = main.settings.allowed_group_id
    main.settings.allowed_group_id = "ALLOWED_GROUP"

    msg = _make_text_msg()
    evt = _make_message_event(msg, source=_make_group_source(group_id="WRONG_GROUP"))
    with patch("main.memory.log_raw_message") as mock_log2:
        main._handle_event(evt)
    check("非 allowed group → 忽略", not mock_log2.called)

    main.settings.allowed_group_id = orig_allowed


def test_handle_event_redelivery():
    print("\n── Test C: redelivery 處理 ──")
    msg = _make_text_msg()
    evt = _make_message_event(msg, redelivery=True)

    # 已存在 → skip
    with (
        patch("main.memory.get_raw_message", return_value=("text", "舊訊息")),
        patch("main.memory.log_raw_message") as mock_log,
    ):
        main._handle_event(evt)
    check("redelivery 已存在 → skip", not mock_log.called)

    # 不存在 → 補處理
    with (
        patch("main.memory.get_raw_message", return_value=None),
        patch("main.memory.log_raw_message") as mock_log2,
        patch("main._handle_text_message"),
    ):
        main._handle_event(evt)
    check("redelivery 不存在 → log raw", mock_log2.called)


def test_handle_event_message_types():
    print("\n── Test D: 各訊息類型路由 ──")
    # Image → log only
    img_msg = MagicMock(spec=ImageMessageContent)
    img_msg.id = "IMG001"
    img_evt = _make_message_event(img_msg)
    with (
        patch("main.memory.log_raw_message") as mock_log,
        patch("main._handle_text_message") as mock_text,
    ):
        main._handle_event(img_evt)
    check("ImageMessage → log raw", mock_log.called)
    check("ImageMessage → 不走 text handler", not mock_text.called)

    # Video → log only
    vid_msg = MagicMock(spec=VideoMessageContent)
    vid_msg.id = "VID001"
    vid_evt = _make_message_event(vid_msg)
    with patch("main.memory.log_raw_message") as mock_log2:
        main._handle_event(vid_evt)
    check("VideoMessage → log raw", mock_log2.called)

    # Audio → _handle_audio_message
    aud_msg = MagicMock(spec=AudioMessageContent)
    aud_msg.id = "AUD001"
    aud_msg.type = "audio"
    aud_evt = _make_message_event(aud_msg)
    with (
        patch("main.memory.log_raw_message"),
        patch("main._handle_audio_message") as mock_audio,
    ):
        main._handle_event(aud_evt)
    check("AudioMessage → _handle_audio_message", mock_audio.called)

    # File → _handle_file_message
    file_msg = MagicMock(spec=FileMessageContent)
    file_msg.id = "FILE001"
    file_msg.type = "file"
    file_evt = _make_message_event(file_msg)
    with patch("main._handle_file_message") as mock_file:
        main._handle_event(file_evt)
    check("FileMessage → _handle_file_message", mock_file.called)

    # TextMessage → _handle_text_message
    txt_msg = _make_text_msg("你好")
    txt_evt = _make_message_event(txt_msg)
    with (
        patch("main.memory.log_raw_message"),
        patch("main.bot_stats.track_message"),
        patch("main._handle_text_message") as mock_text,
    ):
        main._handle_event(txt_evt)
    check("TextMessage → _handle_text_message", mock_text.called)


def test_handle_event_quota_exhausted():
    print("\n── Test E: quota 爆時 save pending ──")
    main._quota_exhausted_until_ts = time.time() + 3600

    msg = _make_text_msg("quota 爆了")
    evt = _make_message_event(msg)

    with (
        patch("main.memory.log_raw_message"),
        patch("main.bot_stats.track_message"),
        patch("main.bot_stats.track_pending_saved") as mock_pending,
        patch("main._save_pending_any") as mock_save,
    ):
        main._handle_event(evt)

    check("quota 爆 → _save_pending_any called", mock_save.called)
    check("quota 爆 → track_pending_saved called", mock_pending.called)

    main._quota_exhausted_until_ts = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Test F: _handle_text_message flow
# ═══════════════════════════════════════════════════════════════════════════════
def test_handle_text_message():
    print("\n── Test F: _handle_text_message flow ──")

    # Command → _handle_command, cancel burst
    msg = _make_text_msg("/help")
    evt = _make_message_event(msg)
    with (
        patch("main._handle_command", return_value="help text") as mock_cmd,
        patch("main.burst_filter.cancel_burst") as mock_cancel,
        patch("main._reply") as mock_reply,
        patch("main.feedback_collector.in_feedback_window", return_value=False),
    ):
        main._handle_text_message(evt, "GRP001")
    check("命令 → _handle_command 被呼叫", mock_cmd.called)
    check("命令 → burst cancel", mock_cancel.called)
    check("命令 → _reply called", mock_reply.called)

    # Dinner question → _handle_dinner_recommendation
    msg2 = _make_text_msg("今天吃什麼晚餐")
    evt2 = _make_message_event(msg2)
    with (
        patch("main._handle_command", return_value=None),
        patch("main._is_dinner_question", return_value=True),
        patch("main.burst_filter.cancel_burst"),
        patch("main._handle_dinner_recommendation") as mock_dinner,
        patch("main.feedback_collector.in_feedback_window", return_value=False),
    ):
        main._handle_text_message(evt2, "GRP001")
    check("晚餐問題 → _handle_dinner_recommendation", mock_dinner.called)

    # Explicit trigger → _handle_explicit_text
    msg3 = _make_text_msg("/ai 問題")
    evt3 = _make_message_event(msg3)
    with (
        patch("main._handle_command", return_value=None),
        patch("main._is_dinner_question", return_value=False),
        patch("main._extract_gemini_trigger", return_value="問題"),
        patch("main.burst_filter.cancel_burst"),
        patch("main._handle_explicit_text") as mock_explicit,
        patch("main.feedback_collector.in_feedback_window", return_value=False),
    ):
        main._handle_text_message(evt3, "GRP001")
    check("explicit trigger → _handle_explicit_text", mock_explicit.called)

    # Plain text → burst_filter
    msg4 = _make_text_msg("隨便說說")
    evt4 = _make_message_event(msg4)
    with (
        patch("main._handle_command", return_value=None),
        patch("main._is_dinner_question", return_value=False),
        patch("main._extract_gemini_trigger", return_value=None),
        patch("main.burst_filter.add_to_burst") as mock_burst,
        patch("main.feedback_collector.in_feedback_window", return_value=False),
    ):
        main._handle_text_message(evt4, "GRP001")
    check("一般文字 → burst_filter.add_to_burst", mock_burst.called)


# ═══════════════════════════════════════════════════════════════════════════════
# Test G: _handle_command
# ═══════════════════════════════════════════════════════════════════════════════
def test_handle_command():
    print("\n── Test G: _handle_command ──")
    gid = "GRP001"

    check(
        "/group_id 回傳群組 ID", gid in (main._handle_command(gid, "/group_id") or "")
    )
    check("/help 回傳說明", "指令" in (main._handle_command(gid, "/help") or ""))
    check(
        "/指令 同 /help",
        main._handle_command(gid, "/指令") == main._handle_command(gid, "/help"),
    )

    with patch("main.memory.list_facts", return_value=[]):
        check(
            "/看記憶 無資料 → 提示",
            "沒有" in (main._handle_command(gid, "/看記憶") or ""),
        )
    with patch("main.memory.list_facts", return_value=["記住事情A"]):
        check(
            "/看記憶 有資料 → 含事實",
            "記住事情A" in (main._handle_command(gid, "/看記憶") or ""),
        )

    with patch("main.memory.add_fact", return_value=True):
        check(
            "/記住 新事實 → 回應含事實",
            "記住了" in (main._handle_command(gid, "/記住 新事實") or ""),
        )
    with patch("main.memory.add_fact", return_value=False):
        check(
            "/記住 重複事實 → 提示已存在",
            "已經" in (main._handle_command(gid, "/記住 重複") or ""),
        )

    check("/記住 空 → 用法提示", "用法" in (main._handle_command(gid, "/記住 ") or ""))

    with patch("main.memory.remove_fact", return_value=1):
        check(
            "/忘記 找到 → 刪除 1 條",
            "1" in (main._handle_command(gid, "/忘記 關鍵字") or ""),
        )
    with patch("main.memory.remove_fact", return_value=0):
        check(
            "/忘記 找不到 → 提示",
            "沒有找到" in (main._handle_command(gid, "/忘記 不存在") or ""),
        )

    with patch("main.memory.clear_facts", return_value=5):
        check(
            "/清除記憶 → 清 5 條", "5" in (main._handle_command(gid, "/清除記憶") or "")
        )

    with patch("main.memory.add_filter_rule", return_value=1):
        check(
            "/不要回 → 回應含 pattern",
            "早安" in (main._handle_command(gid, "/不要回 早安") or ""),
        )
        check(
            "/以後要查 → 回應含 pattern",
            "疫苗" in (main._handle_command(gid, "/以後要查 疫苗") or ""),
        )

    check(
        "/不要回 空 → 用法提示", "用法" in (main._handle_command(gid, "/不要回 ") or "")
    )

    with patch("main.memory.list_filter_rules", return_value=[]):
        check("/規則 無 → 提示", "沒有" in (main._handle_command(gid, "/規則") or ""))
    with patch(
        "main.memory.list_filter_rules",
        return_value=[
            {"rule_id": 1, "kind": "skip", "source": "user", "pattern": "早安"}
        ],
    ):
        check("/規則 有 → 含編號", "#1" in (main._handle_command(gid, "/規則") or ""))

    with patch("main.memory.delete_filter_rule", return_value=True):
        check(
            "/刪除規則 1 → 成功",
            "1" in (main._handle_command(gid, "/刪除規則 1") or ""),
        )
    with patch("main.memory.delete_filter_rule", return_value=False):
        check(
            "/刪除規則 99 → 找不到",
            "找不到" in (main._handle_command(gid, "/刪除規則 99") or ""),
        )
    check(
        "/刪除規則 abc → 格式錯誤",
        "用法" in (main._handle_command(gid, "/刪除規則 abc") or ""),
    )

    with patch("main.memory.clear_filter_rules", return_value=3):
        check(
            "/清除規則 → 清 3 條", "3" in (main._handle_command(gid, "/清除規則") or "")
        )

    check("未知指令 → None", main._handle_command(gid, "普通文字") is None)
    check("空字串 → None", main._handle_command(gid, "") is None)


# ═══════════════════════════════════════════════════════════════════════════════
# Test H: _reply (muted mode)
# ═══════════════════════════════════════════════════════════════════════════════
def test_reply_muted():
    print("\n── Test H: _reply (muted mode) ──")
    # bot_muted=True → log only, no LINE call
    assert main.settings.bot_muted is True  # 確認 env 設定

    with (
        patch("main._get_quota_footer", return_value=""),
        patch("main.gemini_client.get_gemini_quota_info", return_value=None),
    ):
        # 空文字 → 立即 return
        called = []
        orig_info = main.logger.info
        main.logger.info = lambda *a, **kw: called.append(a)
        main._reply("TOKEN", "", group_id="GRP001")
        main.logger.info = orig_info
        muted_logs = [a for a in called if "MUTED" in str(a)]
        check("空文字 → 不 log MUTED", len(muted_logs) == 0)

        # 有文字 → log MUTED
        called2 = []
        main.logger.info = lambda *a, **kw: called2.append(a)
        main._reply("TOKEN", "有內容的回覆", group_id="GRP001")
        main.logger.info = orig_info
        muted_logs2 = [a for a in called2 if "MUTED" in str(a)]
        check("有內容 muted → log MUTED", len(muted_logs2) > 0)


# ═══════════════════════════════════════════════════════════════════════════════
# Test I: _save_pending_any
# ═══════════════════════════════════════════════════════════════════════════════
def test_save_pending_any():
    print("\n── Test I: _save_pending_any ──")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({}, f)
        tmp = f.name
    orig_path = main._PENDING_EXPLICIT_PATH
    main._PENDING_EXPLICIT_PATH = tmp

    # TextMessageContent
    msg = _make_text_msg("測試訊息", "MSG_TEST")
    evt = _make_message_event(msg)
    with patch("main.memory.get_raw_message", return_value=None):
        main._save_pending_any(evt, "GRP001", "USR001", msg)

    data = json.load(open(tmp))
    check("pending 有 GRP001", "GRP001" in data)
    check("pending 有一筆", len(data.get("GRP001", [])) == 1)
    check("pending type=text", data["GRP001"][0].get("type") == "text")
    check("pending text 正確", data["GRP001"][0].get("text") == "測試訊息")

    # 非支援類型（ImageMessageContent）→ 不存
    img_msg = MagicMock(spec=ImageMessageContent)
    img_msg.id = "IMG001"
    img_evt = _make_message_event(img_msg)
    main._save_pending_any(img_evt, "GRP001", "USR001", img_msg)
    data2 = json.load(open(tmp))
    check("Image pending → 仍只有 1 筆", len(data2.get("GRP001", [])) == 1)

    main._PENDING_EXPLICIT_PATH = orig_path
    os.unlink(tmp)


# ═══════════════════════════════════════════════════════════════════════════════
# Test J: _clear_pending_explicit
# ═══════════════════════════════════════════════════════════════════════════════
def test_clear_pending_explicit():
    print("\n── Test J: _clear_pending_explicit ──")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(
            {
                "GRP001": [{"text": "a", "type": "text"}],
                "GRP002": [{"text": "b", "type": "text"}],
            },
            f,
        )
        tmp = f.name
    orig_path = main._PENDING_EXPLICIT_PATH
    main._PENDING_EXPLICIT_PATH = tmp

    main._clear_pending_explicit("GRP001")
    data = json.load(open(tmp))
    check("clear GRP001 後不含 GRP001", "GRP001" not in data)
    check("GRP002 不受影響", "GRP002" in data)

    main._PENDING_EXPLICIT_PATH = orig_path
    os.unlink(tmp)


# ═══════════════════════════════════════════════════════════════════════════════
# Test K: _build_quoted_block
# ═══════════════════════════════════════════════════════════════════════════════
def test_build_quoted_block():
    print("\n── Test K: _build_quoted_block ──")
    # 無 quoted_message_id → None
    msg = _make_text_msg("普通訊息")
    msg.quoted_message_id = None
    result = main._build_quoted_block(msg, "GRP001")
    check("無 quoted_message_id → None", result is None)

    # 有 quoted_message_id，DB 找得到 → 含引用
    msg2 = _make_text_msg("回覆")
    msg2.quoted_message_id = "QUOTED_ID"
    with patch("main.memory.get_raw_message", return_value=("text", "被引用的原文")):
        result2 = main._build_quoted_block(msg2, "GRP001")
    check("有引用且 DB 找到 → 非 None", result2 is not None)
    check("有引用且 DB 找到 → 含原文", "被引用的原文" in (result2 or ""))

    # 有 quoted_message_id，DB 找不到 → None
    msg3 = _make_text_msg("回覆")
    msg3.quoted_message_id = "MISSING_ID"
    with patch("main.memory.get_raw_message", return_value=None):
        result3 = main._build_quoted_block(msg3, "GRP001")
    check("引用 DB 找不到 → None", result3 is None)


# ═══════════════════════════════════════════════════════════════════════════════
# Test L: _handle_burst_flush
# ═══════════════════════════════════════════════════════════════════════════════
def test_handle_burst_flush():
    print("\n── Test L: _handle_burst_flush ──")
    main._quota_exhausted_until_ts = 0.0

    # quota 爆 → 靜默
    main._quota_exhausted_until_ts = time.time() + 3600
    with patch("main.memory.check_fact_cache"), patch("main._reply") as mock_reply:
        main._handle_burst_flush("GRP001", "文字", "TOKEN")
    check("quota 爆 burst flush → 靜默", not mock_reply.called)
    main._quota_exhausted_until_ts = 0.0

    # cache hit → 直接回
    with (
        patch("main.memory.check_fact_cache", return_value="快取答案"),
        patch("main._reply") as mock_reply2,
    ):
        main._handle_burst_flush("GRP001", "謠言文字", "TOKEN")
    check("cache hit → _reply called", mock_reply2.called)

    # 正常流程 → LLM → reply（muted 所以不實際送 LINE）
    with (
        patch("main.memory.check_fact_cache", return_value=None),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._llm_chat", return_value="LLM 回覆") as mock_llm,
        patch("main.memory.append_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._reply"),
    ):
        main._handle_burst_flush("GRP001", "正常問題", "TOKEN")
    check("正常 burst flush → LLM 被呼叫", mock_llm.called)


# ═══════════════════════════════════════════════════════════════════════════════
# Test M: _handle_explicit_text
# ═══════════════════════════════════════════════════════════════════════════════
def test_handle_explicit_text():
    print("\n── Test M: _handle_explicit_text ──")
    main._quota_exhausted_until_ts = 0.0

    # 空 clean_text 且無引用 → 問候
    msg = _make_text_msg("")
    msg.quoted_message_id = None
    evt = _make_message_event(msg)
    with (
        patch("main._reply") as mock_reply,
        patch("main.memory.get_raw_message", return_value=None),
    ):
        main._handle_explicit_text(evt, "GRP001", "")
    check("空文字無引用 → 問候回覆", mock_reply.called)

    # quota 爆 → 靜默
    main._quota_exhausted_until_ts = time.time() + 3600
    msg2 = _make_text_msg("問問題")
    evt2 = _make_message_event(msg2)
    with (
        patch("main._reply") as mock_reply2,
        patch("main.memory.get_raw_message", return_value=None),
        patch("main._build_quoted_block", return_value=None),
        patch("main._prefetch_urls", return_value="問問題"),
    ):
        main._handle_explicit_text(evt2, "GRP001", "問問題")
    check("quota 爆 explicit → 靜默", not mock_reply2.called)
    main._quota_exhausted_until_ts = 0.0

    # 正常流程 → LLM → reply
    msg3 = _make_text_msg("一般問題")
    evt3 = _make_message_event(msg3)
    with (
        patch("main.memory.get_raw_message", return_value=None),
        patch("main._build_quoted_block", return_value=None),
        patch("main._prefetch_urls", return_value="一般問題"),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._llm_chat", return_value="回答") as mock_llm,
        patch("main.memory.append_turn"),
        patch("main._try_save_correction"),
        patch("main._maybe_extract_facts"),
        patch("main._reply") as mock_reply3,
        patch("main._thinking_indicator"),
    ):
        main._handle_explicit_text(evt3, "GRP001", "一般問題")
    check("正常 explicit → LLM 被呼叫", mock_llm.called)
    check("正常 explicit → reply 被呼叫", mock_reply3.called)


# ═══════════════════════════════════════════════════════════════════════════════
# Test N: _mark_quota_exhausted
# ═══════════════════════════════════════════════════════════════════════════════
def test_mark_quota_exhausted():
    print("\n── Test N: _mark_quota_exhausted ──")
    orig_ts = main._quota_exhausted_until_ts
    orig_notified = main._quota_notified_for_ts

    with (
        patch("main.gemini_client.mark_quota_exhausted_in_usage"),
        patch("main._save_quota_state"),
        patch("main.settings.bot_muted", True),
    ):
        main._mark_quota_exhausted()

    check("mark_quota_exhausted 設 until_ts > 0", main._quota_exhausted_until_ts > 0)
    check("mark_quota_exhausted → _quota_exhausted() True", main._quota_exhausted())

    # 還原
    main._quota_exhausted_until_ts = orig_ts
    main._quota_notified_for_ts = orig_notified


# ═══════════════════════════════════════════════════════════════════════════════
# Test O: /callback endpoint 基本路由
# ═══════════════════════════════════════════════════════════════════════════════
def test_callback_invalid_sig():
    print("\n── Test O: /callback invalid signature ──")
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    resp = client.post(
        "/callback",
        content=b'{"destination":"x","events":[]}',
        headers={"X-Line-Signature": "badsig"},
    )
    check("無效簽名 → 400", resp.status_code == 400)


# ═══════════════════════════════════════════════════════════════════════════════
# Test P: _quota_exhausted_message
# ═══════════════════════════════════════════════════════════════════════════════
def test_quota_exhausted_message():
    print("\n── Test P: _quota_exhausted_message ──")
    msg = main._quota_exhausted_message()
    check("含「額度」", "額度" in msg)
    check("含「台灣時間」", "台灣時間" in msg)
    check("含 URL", "http" in msg)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    test_handle_event_routing()
    test_handle_event_group_filter()
    test_handle_event_redelivery()
    test_handle_event_message_types()
    test_handle_event_quota_exhausted()
    test_handle_text_message()
    test_handle_command()
    test_reply_muted()
    test_save_pending_any()
    test_clear_pending_explicit()
    test_build_quoted_block()
    test_handle_burst_flush()
    test_handle_explicit_text()
    test_mark_quota_exhausted()
    test_callback_invalid_sig()
    test_quota_exhausted_message()

    print(f"\n{'=' * 50}")
    print(f"TOTAL: {PASS} passed, {FAIL} failed")
    print("=" * 50)
    if FAIL:
        print("Some tests FAILED.")
        sys.exit(1)
    else:
        print("All tests passed!")
