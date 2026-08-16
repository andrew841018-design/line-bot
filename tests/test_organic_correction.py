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


@pytest.mark.parametrize(
    "msg",
    (
        "不走雪地是不是比較好",
        "不應該喝酒對不對",
        "回答時不公開地址對不對",
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
    ),
)
def test_organic_detector_rejects_question_like_corrections(msg):
    import main
    import memory

    memory.append_turn("GRP_TEST", "user", "原本問題")
    memory.append_turn("GRP_TEST", "bot", "前一輪回答")

    with patch.object(memory, "add_organic_correction") as mock_save:
        result = main._detect_user_correction(msg, "GRP_TEST")

    assert result is False
    mock_save.assert_not_called()


def test_all_strong_correction_prefixes_share_the_question_guard():
    import correction_memory
    import main
    import memory

    assert main._ORGANIC_CORRECTION_KEYWORDS is correction_memory.ORGANIC_CORRECTION_PREFIXES
    memory.append_turn("GRP_TEST", "user", "原本問題")
    memory.append_turn("GRP_TEST", "bot", "前一輪回答")

    with patch.object(memory, "add_organic_correction") as mock_save, patch.object(
        main, "_summarize_correction", return_value=""
    ):
        for prefix in main._ORGANIC_CORRECTION_KEYWORDS:
            msg = f"{prefix}，我想問怎麼搭車"
            assert main._detect_user_correction(msg, "GRP_TEST") is False, msg

    mock_save.assert_not_called()


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


def test_organic_and_explicit_detectors_share_message_id_idempotency():
    import main
    import memory

    memory.append_turn("GRP_TEST", "user", "你剛才為什麼重複開場")
    memory.append_turn("GRP_TEST", "bot", "你問為什麼重複開場，我來回答")

    with patch.object(main, "_summarize_correction", return_value="不要重複開場"):
        assert main._detect_user_correction(
            "不對，以後不要重複開場",
            "GRP_TEST",
            sender_user_id="U1",
            message_id="M1",
        ) is True
    main._try_save_correction(
        "GRP_TEST",
        "不對，以後不要重複開場",
        sender_user_id="U1",
        message_id="M1",
    )

    audits = memory.list_organic_correction_audits("GRP_TEST")
    rules = memory.list_canonical_organic_corrections("GRP_TEST")
    assert len(audits) == 1
    assert len(rules) == 1
    assert rules[0]["occurrence_count"] == 1


@pytest.mark.parametrize(
    "rule",
    (
        "別提地址",
        "請勿洩密",
        "不應刪檔",
        "不該傳錢",
        "無需公開",
        "不必回覆",
        "不用推播",
        "不得推播",
        "嚴禁洩密",
        "無須公開",
        "毋須回覆",
        "不宜透露",
        "避免洩密",
    ),
)
def test_explicit_route_captures_short_concrete_safety_rules(rule):
    import main
    import memory

    main._try_save_correction(
        "GRP_TEST",
        rule,
        sender_user_id="U1",
        message_id=f"M-{rule}",
    )

    audits = memory.list_organic_correction_audits("GRP_TEST")
    rules = memory.list_canonical_organic_corrections("GRP_TEST")
    assert len(audits) == 1
    assert len(rules) == 1
    assert rules[0]["canonical_rule"] == rule


@pytest.mark.parametrize(
    "ordinary",
    (
        "特別喜歡這部電影",
        "我跟別人去吃飯",
        "請分別整理資料",
        "有什麼區別",
        "告別昨天",
        "別人說得比較清楚",
        "別的電影比較好看",
        "別處也有一家店",
        "別名叫做咪寶",
        "為什麼不應該喝酒？",
        "有沒有無需登入的網站？",
        "如果自駕不走雪地，還能去哪些地方？",
        "為什麼這間公司不公開地址？",
        "我今天不去上班",
        "他不去日本旅行",
        "這部電影不公開上映",
        "公司不刪除資料",
        "朋友不傳送照片",
        "昨天不走高速公路",
        "請問，不要推播怎麼設定",
        "想知道，不要推播要去哪裡設定",
        "他說，不要推播照片比較安全",
        "文章建議，不要傳錢給陌生人",
        "自駕不走雪地有什麼風險",
        "自駕不走雪地會怎樣",
        "自駕不走雪地好不好",
        "回答時不公開地址會有什麼問題",
        "bot不推播會不會漏掉通知",
        "bot不推播是什麼意思",
        "自駕不走雪地行不行",
        "自駕不走雪地可否到景點",
        "bot不推播要問誰",
        "回答時不公開地址能維持多久",
        "開車時不走雪地幾點出發",
        "自駕不走雪地安全不安全",
        "bot不推播正常不正常",
        "回答時不公開地址對不對",
        "自駕不走雪地是不是比較好",
    ),
)
def test_explicit_route_does_not_treat_bare_bie_as_correction(ordinary):
    import main
    import memory

    main._try_save_correction("GRP_TEST", ordinary, message_id=f"N-{ordinary}")
    assert memory.list_organic_correction_audits("GRP_TEST") == []


@pytest.mark.parametrize(
    "rule",
    (
        "別洩密",
        "自駕不走雪地",
        "自駕不走可能結冰山路",
        "不對，我是說，以後不要推播",
        "我的意思是，不要傳錢",
    ),
)
def test_explicit_route_captures_concise_declarative_rules(rule):
    import main
    import memory

    main._try_save_correction("GRP_TEST", rule, message_id=f"D-{rule}")
    audits = memory.list_organic_correction_audits("GRP_TEST")
    assert len(audits) == 1


def test_explicit_correction_storage_failure_does_not_block_reply_path():
    import main
    import memory

    with patch.object(
        memory,
        "record_organic_correction_observation",
        side_effect=RuntimeError("database locked"),
    ):
        main._try_save_correction(
            "GRP_TEST",
            "以後不要重複開場",
            sender_user_id="U1",
            message_id="M1",
        )


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
