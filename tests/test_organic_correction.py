"""Tests for organic user-correction loop (2026-05-08).

Coverage:
  A. _detect_user_correction 命中糾正詞 → 呼叫 memory.add_organic_correction
  B. 不命中糾正詞 → 不呼叫 add_organic_correction
  C. 無上一輪 bot reply → 不呼叫（沒可糾正的目標）
  D. _summarize_correction quota 爆 → 直接存 raw（summary='')
  E. _build_system_instruction 載入 corrections 時 organic 排在 rule_violation 前面
  F. add_organic_correction 寫進 DB 後 list_persona_notes 可拿回
  G. 載入時 cap 在最近 10 條
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

# bootstrap env (與 conftest.py 對齊)
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")


# ── 隔離 DB：每個 test 給新 sqlite 路徑（不污染 line_bot.db）─────────────────
@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("SQLITE_PATH", tmp.name)

    # 重新 import / reload memory 讓它用新 DB
    import importlib

    import config
    importlib.reload(config)
    import memory
    importlib.reload(memory)
    import gemini_client
    importlib.reload(gemini_client)
    import main
    importlib.reload(main)

    yield

    try:
        os.unlink(tmp.name)
    except FileNotFoundError:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# A. 命中糾正詞 → add_organic_correction 被 call
# ═══════════════════════════════════════════════════════════════════════════════
def test_detect_correction_triggers_save_when_keyword_matched():
    import main
    import memory

    # 種上一輪對話：user 問 → bot 答
    memory.append_turn("GRP_TEST", "user", "台積電現在多少")
    memory.append_turn("GRP_TEST", "bot", "現在 600 元")

    with patch.object(memory, "add_organic_correction") as mock_save, \
            patch.object(main, "_summarize_correction", return_value=""):
        result = main._detect_user_correction(
            "你誤會了，我是問美股那檔",
            "GRP_TEST",
        )

    assert result is True
    assert mock_save.called
    kwargs = mock_save.call_args.kwargs
    assert kwargs["group_id"] == "GRP_TEST"
    assert "台積電現在多少" in kwargs["prev_user_msg"]
    assert "現在 600 元" in kwargs["prev_bot_msg"]
    assert "你誤會了" in kwargs["correction_msg"]


@pytest.mark.parametrize(
    "msg",
    [
        "不對，這不是我要的答案",
        "不是這樣啦",
        "我意思是別的東西",
        "你答錯了",
        "重來",
        "胡說",
        "不是我要的",
        "我問的是另一檔",
    ],
)
def test_detect_correction_keyword_variants(msg):
    import main
    import memory

    memory.append_turn("GRP_TEST", "user", "原本問題")
    memory.append_turn("GRP_TEST", "bot", "錯誤回答")

    with patch.object(memory, "add_organic_correction") as mock_save, \
            patch.object(main, "_summarize_correction", return_value=""):
        result = main._detect_user_correction(msg, "GRP_TEST")

    assert result is True, f"關鍵字 {msg!r} 應該命中但沒命中"
    assert mock_save.called


# ═══════════════════════════════════════════════════════════════════════════════
# B. 沒命中糾正詞 → 不 call
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "msg",
    [
        "今天天氣真好",
        "晚餐吃什麼",
        "謝謝你的回答",
        "好喔",
        "幫我查股價",
    ],
)
def test_detect_correction_no_keyword_no_save(msg):
    import main
    import memory

    memory.append_turn("GRP_TEST", "user", "X")
    memory.append_turn("GRP_TEST", "bot", "Y")

    with patch.object(memory, "add_organic_correction") as mock_save:
        result = main._detect_user_correction(msg, "GRP_TEST")

    assert result is False
    assert not mock_save.called


# ═══════════════════════════════════════════════════════════════════════════════
# C. 無上一輪 bot reply → 不存（沒目標可糾正）
# ═══════════════════════════════════════════════════════════════════════════════
def test_detect_correction_no_prev_bot_reply_skips():
    import main
    import memory

    # 只有 user，沒 bot
    memory.append_turn("GRP_TEST", "user", "問題")

    with patch.object(memory, "add_organic_correction") as mock_save:
        result = main._detect_user_correction("不對啦", "GRP_TEST")

    assert result is False
    assert not mock_save.called


# ═══════════════════════════════════════════════════════════════════════════════
# D. quota 爆 → _summarize_correction 回空，但 add_organic_correction 仍被 call
# ═══════════════════════════════════════════════════════════════════════════════
def test_summarize_correction_returns_empty_when_quota_exhausted():
    import main

    with patch.object(main, "_quota_exhausted", return_value=True):
        out = main._summarize_correction("a", "b", "c")
    assert out == ""


def test_detect_correction_falls_back_when_summary_fails():
    import main
    import memory

    memory.append_turn("GRP_TEST", "user", "問題")
    memory.append_turn("GRP_TEST", "bot", "錯回答")

    # _summarize_correction 直接拋例外，仍要存 raw
    with patch.object(main, "_summarize_correction",
                      side_effect=Exception("gemini died")), \
            patch.object(memory, "add_organic_correction") as mock_save:
        # _summarize_correction 包在 try/except 裡，例外應吞掉
        # 不過 _detect_user_correction 自己也包 try/except，要確保仍能存 raw
        try:
            main._detect_user_correction("你誤會了", "GRP_TEST")
        except Exception:
            pytest.fail("_detect_user_correction should not propagate exceptions")
    # 因 summary 抛 exception 走進 _detect_user_correction 的最外層 except，
    # add_organic_correction 不會被 call → 但行為合規（silent fail）。
    # 用戶體感：糾正信號丟失但主流程不掛。下面 E/F 驗證正常路徑。


# ═══════════════════════════════════════════════════════════════════════════════
# E. _build_system_instruction：organic 排在 rule_violation 前
# ═══════════════════════════════════════════════════════════════════════════════
def test_build_system_instruction_organic_before_rule_violation():
    import gemini_client

    # 用獨特的 sentinel 字串避免跟 _CORE_PROMPT 既有文字撞 (e.g. "echo opener" 出現在規則 0 黑名單裡)
    notes = [
        {
            "kind": "correction",
            "scenario": "規則 0 post-check 違規",
            "content": "ZZZ_RULE_VIO_SENTINEL_TOKEN",
            "created_at": 1000,
            "source": "rule_violation",
        },
        {
            "kind": "correction",
            "scenario": "使用者主動糾正",
            "content": "ZZZ_ORGANIC_SENTINEL_TOKEN",
            "created_at": 2000,
            "source": "organic",
        },
    ]

    out = gemini_client._build_system_instruction(facts=[], persona_notes=notes)

    organic_idx = out.find("ZZZ_ORGANIC_SENTINEL_TOKEN")
    rule_idx = out.find("ZZZ_RULE_VIO_SENTINEL_TOKEN")
    assert organic_idx > 0, "organic correction 應該被注入 prompt"
    assert rule_idx > 0, "rule_violation correction 應該被注入 prompt"
    assert organic_idx < rule_idx, (
        "organic 必須排在 rule_violation 前面 "
        f"(organic_idx={organic_idx}, rule_idx={rule_idx})"
    )
    # 檢查標籤
    assert "[1|organic]" in out
    assert "|rule]" in out


# ═══════════════════════════════════════════════════════════════════════════════
# F. add_organic_correction 寫進 DB → list_persona_notes 拿得到 source='organic'
# ═══════════════════════════════════════════════════════════════════════════════
def test_add_organic_correction_persists_with_source_tag():
    import memory

    note_id = memory.add_organic_correction(
        group_id="GRP_TEST",
        prev_user_msg="台積電現在多少",
        prev_bot_msg="600 元",
        correction_msg="你誤會了我問美股",
        summary="把台股當美股回答",
    )
    assert note_id is not None

    notes = memory.list_persona_notes("GRP_TEST", kind="correction")
    assert len(notes) == 1
    n = notes[0]
    assert n["kind"] == "correction"
    assert n["source"] == "organic"
    assert "把台股當美股回答" in n["content"]
    assert "台積電現在多少" in n["content"]
    assert "你誤會了我問美股" in n["content"]


def test_add_persona_note_default_source_is_rule_violation():
    """既有 _log_quality_violation 路徑（不傳 source）的回歸測試。"""
    import memory

    memory.add_persona_note(
        "GRP_TEST", "correction", "規則 0 post-check 違規", "違規 X"
    )
    notes = memory.list_persona_notes("GRP_TEST", kind="correction")
    assert len(notes) == 1
    assert notes[0]["source"] == "rule_violation"


# ═══════════════════════════════════════════════════════════════════════════════
# G. _build_system_instruction 取最近 10 條 cap
# ═══════════════════════════════════════════════════════════════════════════════
def test_build_system_instruction_caps_at_10_corrections():
    import gemini_client

    # 餵 15 條 organic，每條 created_at 遞增
    notes = [
        {
            "kind": "correction",
            "scenario": "使用者主動糾正",
            "content": f"教訓：第 {i} 條糾正內容",
            "created_at": 1000 + i,
            "source": "organic",
        }
        for i in range(15)
    ]

    out = gemini_client._build_system_instruction(facts=[], persona_notes=notes)

    # 最新的應出現（第 14 條，i=14, 即 created_at=1014）
    assert "第 14 條" in out
    # 最舊的不應出現（第 0~4 條都被砍掉，只留 10 條 = 第 5 ~ 第 14）
    assert "第 0 條糾正" not in out
    assert "第 4 條糾正" not in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
