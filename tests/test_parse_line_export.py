"""Tests for finetune/parse_line_export.py — LINE app export `.txt` parser.

Pure local fixtures — does NOT touch real export files.
"""
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

# bootstrap (matches conftest.py style)
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

LINE_BOT_ROOT = Path(__file__).resolve().parent.parent
if str(LINE_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(LINE_BOT_ROOT))

from finetune import parse_line_export  # noqa: E402


# ─── helpers ───────────────────────────────────────────────────────────────
TAB = "\t"


def _write_export(tmp_path: Path, body: str, name: str = "[LINE]測試群.txt") -> Path:
    """Write a fake export to tmp_path/<name> and return the path."""
    p = tmp_path / name
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def _basic_export_body(group: str = "我家") -> str:
    """A minimal but complete LINE export body."""
    return (
        f"[LINE] 聊天紀錄：{group}\n"
        "儲存日期：2026/05/08 12:34\n"
        "\n"
        "2025/12/01（日）\n"
        f"05:23{TAB}Andrew{TAB}早安咪寶今天天氣如何呢\n"
        f"05:24{TAB}咪寶{TAB}早安喔～看起來會是好天氣，有什麼安排嗎？\n"
        f"05:30{TAB}媽媽{TAB}早餐我做好了\n"
        "\n"
        "2025/12/02（一）\n"
        f"14:00{TAB}Andrew{TAB}[貼圖]\n"
        f"14:01{TAB}咪寶{TAB}看起來心情不錯耶\n"
        f"14:02{TAB}Andrew{TAB}晚上想吃什麼呢推薦一下\n"
        f"14:03{TAB}咪寶{TAB}附近那家牛肉麵不錯吧或是火鍋呢\n"
    )


# ═══════════════════════════════════════════════════════════════════════════
# parse_line_chat
# ═══════════════════════════════════════════════════════════════════════════
def test_parse_basic_messages(tmp_path):
    p = _write_export(tmp_path, _basic_export_body())
    msgs = parse_line_export.parse_line_chat(p)
    # Andrew x3 (含 [貼圖]), 咪寶 x3, 媽媽 x1 = 7
    senders = [m["sender"] for m in msgs]
    assert "Andrew" in senders
    assert "咪寶" in senders
    assert "媽媽" in senders
    assert len(msgs) == 7


def test_parse_extracts_group_name(tmp_path):
    p = _write_export(tmp_path, _basic_export_body(group="我的家人"))
    msgs = parse_line_export.parse_line_chat(p)
    assert all(m["group_name"] == "我的家人" for m in msgs)


def test_parse_date_separator(tmp_path):
    p = _write_export(tmp_path, _basic_export_body())
    msgs = parse_line_export.parse_line_chat(p)
    # 第一則訊息應屬 2025-12-01
    first = next(m for m in msgs if m["sender"] == "Andrew")
    assert first["date"] == "2025-12-01"
    # 14:00 那則應屬 2025-12-02
    later = next(m for m in msgs if m["time"] == "14:00")
    assert later["date"] == "2025-12-02"


def test_parse_time_zero_padding(tmp_path):
    body = (
        "[LINE] 聊天紀錄：群\n"
        "儲存日期：2026/05/08 12:34\n"
        "\n"
        "2025/12/01（日）\n"
        f"5:23{TAB}Andrew{TAB}早安咪寶今天天氣很好\n"
        f"5:24{TAB}咪寶{TAB}早安喔今天看起來不錯\n"
    )
    p = _write_export(tmp_path, body)
    msgs = parse_line_export.parse_line_chat(p)
    assert msgs[0]["time"] == "05:23"
    assert msgs[1]["time"] == "05:24"


def test_parse_multiline_content(tmp_path):
    """連續行不以 HH:MM 開頭 → 接到上一則 message。"""
    body = (
        "[LINE] 聊天紀錄：群\n"
        "儲存日期：2026/05/08 12:34\n"
        "\n"
        "2025/12/01（日）\n"
        f"05:23{TAB}Andrew{TAB}早安咪寶這是第一行\n"
        "這是第二行繼續上面\n"
        "這是第三行也是\n"
        f"05:24{TAB}咪寶{TAB}收到三行訊息了喔\n"
    )
    p = _write_export(tmp_path, body)
    msgs = parse_line_export.parse_line_chat(p)
    assert len(msgs) == 2
    assert "第一行" in msgs[0]["content"]
    assert "第二行" in msgs[0]["content"]
    assert "第三行" in msgs[0]["content"]
    assert msgs[0]["content"].count("\n") == 2


def test_parse_no_date_header(tmp_path):
    """檔案沒有日期分隔行 → date 欄位為空字串，不該 crash。"""
    body = (
        "[LINE] 聊天紀錄：群\n"
        "儲存日期：2026/05/08 12:34\n"
        "\n"
        f"05:23{TAB}Andrew{TAB}沒有日期行的訊息\n"
        f"05:24{TAB}咪寶{TAB}收到囉沒問題的\n"
    )
    p = _write_export(tmp_path, body)
    msgs = parse_line_export.parse_line_chat(p)
    assert len(msgs) == 2
    assert msgs[0]["date"] == ""


def test_parse_missing_file_returns_empty(tmp_path):
    msgs = parse_line_export.parse_line_chat(tmp_path / "[LINE]nope.txt")
    assert msgs == []


def test_parse_android_space_separated_format(tmp_path):
    """Android export uses space-sep + dotted date + bare-word media + sender-fused recall."""
    body = (
        "2024.07.10 星期三\n"
        "18:02 媽媽 你還是帶回家吧好不好\n"
        "18:09 媽媽 太離譜了民主黨的這條法規通過\n"
        "20:35 爸爸 貼圖\n"
        "20:35 爸爸 大家各吃一些就好不要客氣\n"
        "2024.07.11 星期四\n"
        "08:58 黃聖穎 早安今天的市場狀況怎麼樣呢\n"
        "14:22 媽媽已收回訊息\n"
        "15:24 媽媽 我訂好餐廳了等下大家準時到\n"
    )
    p = tmp_path / "[LINE]黃家.txt"
    p.write_text(body, encoding="utf-8")
    msgs = parse_line_export.parse_line_chat(p)
    assert len(msgs) == 7
    # 第一則時間 + 日期
    assert msgs[0]["time"] == "18:02"
    assert msgs[0]["date"] == "2024-07-10"
    assert msgs[0]["sender"] == "媽媽"
    assert "帶回家" in msgs[0]["content"]
    # 換日期後
    later = next(m for m in msgs if m["sender"] == "黃聖穎")
    assert later["date"] == "2024-07-11"
    # 撤回訊息：sender 取出，content 標記為已收回
    recall = next(m for m in msgs if m["time"] == "14:22")
    assert recall["sender"] == "媽媽"
    assert "已收回" in recall["content"]
    # group_name fallback to filename stem when no [LINE] header
    assert msgs[0]["group_name"] == "黃家"


def test_extract_pairs_android_format(tmp_path):
    """空白分隔 + 兩種格式混用一樣可以萃 pair。"""
    body = (
        "2024.07.10 星期三\n"
        "10:00 Andrew 早安咪寶今天好嗎\n"
        "10:01 咪寶 早安喔今天天氣很好欸\n"
        "10:02 Andrew 貼圖\n"  # 媒體應被過濾
        "10:03 咪寶 收到貼圖了喔很可愛\n"
        "10:04 Andrew 中午想吃什麼呢推薦一下\n"
        "10:05 咪寶 附近的牛肉麵不錯吧或是火鍋\n"
        "10:06 Andrew已收回訊息\n"  # 已收回 應過濾
        "10:07 咪寶 不要再撤回了啦\n"
    )
    p = tmp_path / "[LINE]Android.txt"
    p.write_text(body, encoding="utf-8")
    pairs = parse_line_export.extract_pairs_from_export(p)
    # 只有 10:00→10:01 跟 10:04→10:05 是合格 pair
    assert len(pairs) == 2
    assert any("早安咪寶" in pa["prompt"] for pa in pairs)
    assert any("中午想吃" in pa["prompt"] for pa in pairs)


# ═══════════════════════════════════════════════════════════════════════════
# extract_pairs_from_export
# ═══════════════════════════════════════════════════════════════════════════
def test_extract_pairs_basic(tmp_path):
    p = _write_export(tmp_path, _basic_export_body())
    pairs = parse_line_export.extract_pairs_from_export(p)
    # Andrew → 咪寶 直接相鄰兩次：但第一次貼圖被過濾。剩 2 對：
    #   05:23 → 05:24
    #   14:02 → 14:03
    assert len(pairs) == 2
    assert all(pa["source"] == "line_export" for pa in pairs)
    contents = [pa["prompt"] for pa in pairs]
    assert any("早安咪寶" in c for c in contents)
    assert any("晚上想吃什麼" in c for c in contents)


def test_extract_filters_media(tmp_path):
    """[貼圖] / [照片] / [影片] / [語音訊息] 都不該成為 prompt 或 completion。"""
    body = (
        "[LINE] 聊天紀錄：群\n"
        "儲存日期：2026/05/08 12:34\n"
        "\n"
        "2025/12/01（日）\n"
        f"10:00{TAB}Andrew{TAB}[貼圖]\n"
        f"10:01{TAB}咪寶{TAB}你發貼圖了喔有什麼想說的嗎\n"
        f"10:02{TAB}Andrew{TAB}今天台北的天氣怎麼樣呢可以講嗎\n"
        f"10:03{TAB}咪寶{TAB}[照片]\n"
        f"10:04{TAB}Andrew{TAB}剛才那張照片是什麼地方呢\n"
        f"10:05{TAB}咪寶{TAB}是我隨手拍的天空很漂亮的雲朵\n"
    )
    p = _write_export(tmp_path, body)
    pairs = parse_line_export.extract_pairs_from_export(p)
    # 只有最後一對是合格的
    assert len(pairs) == 1
    assert "剛才那張照片" in pairs[0]["prompt"]


def test_extract_filters_recalled(tmp_path):
    """已收回訊息 過濾。"""
    body = (
        "[LINE] 聊天紀錄：群\n"
        "儲存日期：2026/05/08 12:34\n"
        "\n"
        "2025/12/01（日）\n"
        f"10:00{TAB}Andrew{TAB}已收回訊息\n"
        f"10:01{TAB}咪寶{TAB}這條應該是針對被撤回的回覆\n"
        f"10:02{TAB}Andrew{TAB}今天的工作都完成了感覺不錯\n"
        f"10:03{TAB}咪寶{TAB}辛苦你了要不要去放鬆一下呢\n"
    )
    p = _write_export(tmp_path, body)
    pairs = parse_line_export.extract_pairs_from_export(p)
    assert len(pairs) == 1
    assert "已收回" not in pairs[0]["prompt"]


def test_extract_filters_short_messages(tmp_path):
    """< 5 字 prompt or completion 都被丟。"""
    body = (
        "[LINE] 聊天紀錄：群\n"
        "儲存日期：2026/05/08 12:34\n"
        "\n"
        "2025/12/01（日）\n"
        f"10:00{TAB}Andrew{TAB}嗨\n"  # 太短
        f"10:01{TAB}咪寶{TAB}哈囉今天有什麼新鮮事呢可以聊聊\n"
        f"10:02{TAB}Andrew{TAB}最近在思考要不要換工作呢\n"
        f"10:03{TAB}咪寶{TAB}OK\n"  # 太短
        f"10:04{TAB}Andrew{TAB}講個比較深入的議題給我聽看看\n"
        f"10:05{TAB}咪寶{TAB}好的我來想一個有趣的話題說給你聽吧\n"
    )
    p = _write_export(tmp_path, body)
    pairs = parse_line_export.extract_pairs_from_export(p)
    # 只有第三組通過長度過濾
    assert len(pairs) == 1
    assert "深入的議題" in pairs[0]["prompt"]


def test_extract_only_immediate_neighbor(tmp_path):
    """Andrew → 媽媽 → 咪寶 的 sequence 不算 pair（不相鄰）。"""
    body = (
        "[LINE] 聊天紀錄：群\n"
        "儲存日期：2026/05/08 12:34\n"
        "\n"
        "2025/12/01（日）\n"
        f"10:00{TAB}Andrew{TAB}今天有什麼好玩的事情可以做嗎\n"
        f"10:01{TAB}媽媽{TAB}你還是先把作業寫完比較重要喔\n"
        f"10:02{TAB}咪寶{TAB}媽媽說的也有道理欸先把該做的做好\n"
    )
    p = _write_export(tmp_path, body)
    pairs = parse_line_export.extract_pairs_from_export(p)
    assert len(pairs) == 0


def test_extract_carries_group_name_and_timestamp(tmp_path):
    p = _write_export(tmp_path, _basic_export_body(group="我的家人"))
    pairs = parse_line_export.extract_pairs_from_export(p)
    assert pairs
    pa = pairs[0]
    assert pa["group_name"] == "我的家人"
    # timestamp 形如 "2025-12-01 05:24"
    assert "2025-12-01" in pa["timestamp"]
    assert ":" in pa["timestamp"]


def test_extract_multiple_groups_via_multiple_files(tmp_path):
    """不同檔案 → 不同 group_name 都正確標註。"""
    p1 = _write_export(tmp_path, _basic_export_body(group="家人群"), "[LINE]家人群.txt")
    body2 = (
        "[LINE] 聊天紀錄：朋友群\n"
        "儲存日期：2026/05/08 12:34\n"
        "\n"
        "2025/12/01（日）\n"
        f"10:00{TAB}Andrew{TAB}朋友群裡的測試訊息夠長吧\n"
        f"10:01{TAB}咪寶{TAB}收到朋友群裡的訊息喔好開心\n"
    )
    p2 = _write_export(tmp_path, body2, "[LINE]朋友群.txt")
    pairs1 = parse_line_export.extract_pairs_from_export(p1)
    pairs2 = parse_line_export.extract_pairs_from_export(p2)
    assert all(pa["group_name"] == "家人群" for pa in pairs1)
    assert all(pa["group_name"] == "朋友群" for pa in pairs2)


def test_extract_custom_user_bot_names(tmp_path):
    body = (
        "[LINE] 聊天紀錄：自訂群\n"
        "儲存日期：2026/05/08 12:34\n"
        "\n"
        "2025/12/01（日）\n"
        f"10:00{TAB}小明{TAB}哈囉這是我跟機器人講話的內容\n"
        f"10:01{TAB}小助手{TAB}你好我會聽你說話然後回覆給你\n"
    )
    p = _write_export(tmp_path, body)
    pairs = parse_line_export.extract_pairs_from_export(
        p, user_name="小明", bot_name="小助手",
    )
    assert len(pairs) == 1
    assert "哈囉" in pairs[0]["prompt"]


# ═══════════════════════════════════════════════════════════════════════════
# find_all_exports
# ═══════════════════════════════════════════════════════════════════════════
def test_find_all_exports_picks_up_pattern(tmp_path):
    a = _write_export(tmp_path, _basic_export_body(), "[LINE]A.txt")
    b = _write_export(tmp_path, _basic_export_body(), "[LINE]子目錄.txt")
    # 不該被抓
    other = tmp_path / "random.txt"
    other.write_text("noise", encoding="utf-8")

    found = parse_line_export.find_all_exports([tmp_path])
    assert a.resolve() in found
    assert b.resolve() in found
    assert other.resolve() not in found
    assert len(found) == 2


def test_find_all_exports_handles_missing_dir(tmp_path):
    found = parse_line_export.find_all_exports([tmp_path / "no_such_dir"])
    assert found == []


def test_find_all_exports_skips_excluded_subdirs(tmp_path):
    sub = tmp_path / ".venv"
    sub.mkdir()
    _write_export(sub, _basic_export_body(), "[LINE]hidden.txt")
    real = _write_export(tmp_path, _basic_export_body(), "[LINE]visible.txt")
    found = parse_line_export.find_all_exports([tmp_path])
    assert real.resolve() in found
    assert all(".venv" not in str(f) for f in found)


# ═══════════════════════════════════════════════════════════════════════════
# CLI smoke
# ═══════════════════════════════════════════════════════════════════════════
def test_cli_scan_runs(tmp_path, capsys):
    _write_export(tmp_path, _basic_export_body(), "[LINE]X.txt")
    rc = parse_line_export.main(["--scan", "--dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "找到" in out or "files" in out.lower()


def test_cli_file_runs(tmp_path, capsys):
    p = _write_export(tmp_path, _basic_export_body(), "[LINE]Y.txt")
    rc = parse_line_export.main(["--file", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "msgs=" in out
    assert "pairs=" in out


def test_cli_no_args_prints_help_and_returns_1(capsys):
    rc = parse_line_export.main([])
    assert rc == 1
