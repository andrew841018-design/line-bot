"""Tests for finetune/check_training_health.py.

Coverage:
  A. distill 累積 — 7d / 30d 計數 + filter mock + 紅黃綠判斷
  B. launchd 健康 — log mtime + 36hr threshold
  C. LoRA 狀態 — 沒 adapter（累積中）/ 有 adapter 沒 active / 有 active
  D. 進步幅度 — 無資料 / 進步 / 退步
  E. organic correction — high / healthy / zero
  F. render_report — 全綠 vs 任一紅
  G. push_discord — webhook 失敗 graceful degrade
  H. dry-run 整合
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# bootstrap env
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "finetune"))


def _now() -> int:
    return int(time.time())


def _write_jsonl(path: Path, pairs: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for x in pairs:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")


# ── A. distill 累積 ──────────────────────────────────────────────────────────

def test_distill_accumulation_filters_mock(tmp_path):
    from finetune import check_training_health as cth

    distilled = tmp_path / "distilled.jsonl"
    seen = tmp_path / "seen.json"
    now = _now()
    pairs = [
        {  # mock，應被過濾
            "messages": [{"role": "user", "content": "x"},
                         {"role": "assistant", "content": "y"}],
            "metadata": {"distilled_at": now, "mock": True},
        },
        {  # 7 天內
            "messages": [{"role": "user", "content": "a"},
                         {"role": "assistant", "content": "b"}],
            "metadata": {"distilled_at": now - 3 * 86400, "mock": False},
        },
        {  # 30 天內，但不在 7 天
            "messages": [{"role": "user", "content": "c"},
                         {"role": "assistant", "content": "d"}],
            "metadata": {"distilled_at": now - 20 * 86400, "mock": False},
        },
    ]
    _write_jsonl(distilled, pairs)
    seen.write_text("[]")

    result = cth.check_distill_accumulation(distilled_path=distilled, seen_path=seen)
    assert result["total_real_pairs"] == 2
    assert result["pairs_7d"] == 1
    assert result["pairs_30d"] == 2


def test_distill_accumulation_red_when_few_pairs(tmp_path):
    from finetune import check_training_health as cth

    distilled = tmp_path / "distilled.jsonl"
    seen = tmp_path / "seen.json"
    # 只 5 對 7d → red (< 20)
    now = _now()
    pairs = [
        {
            "messages": [{"role": "user", "content": f"q{i}"},
                         {"role": "assistant", "content": f"a{i}"}],
            "metadata": {"distilled_at": now - i * 86400, "mock": False},
        }
        for i in range(5)
    ]
    _write_jsonl(distilled, pairs)
    seen.write_text("[]")

    r = cth.check_distill_accumulation(distilled_path=distilled, seen_path=seen)
    assert r["status"] == "red"
    assert r["pairs_7d"] == 5


def test_distill_accumulation_yellow_50plus_under_target(tmp_path):
    from finetune import check_training_health as cth

    distilled = tmp_path / "distilled.jsonl"
    seen = tmp_path / "seen.json"
    now = _now()
    # 30 對 7d → yellow（≥ 20 but < 50）
    pairs = [
        {
            "messages": [{"role": "user", "content": f"q{i}"},
                         {"role": "assistant", "content": f"a{i}"}],
            "metadata": {"distilled_at": now - 86400, "mock": False},
        }
        for i in range(30)
    ]
    _write_jsonl(distilled, pairs)
    seen.write_text("[]")

    r = cth.check_distill_accumulation(distilled_path=distilled, seen_path=seen)
    assert r["status"] == "yellow"


def test_distill_accumulation_green(tmp_path):
    from finetune import check_training_health as cth

    distilled = tmp_path / "distilled.jsonl"
    seen = tmp_path / "seen.json"
    now = _now()
    pairs = [
        {
            "messages": [{"role": "user", "content": f"q{i}"},
                         {"role": "assistant", "content": f"a{i}"}],
            "metadata": {"distilled_at": now - 86400, "mock": False},
        }
        for i in range(70)
    ]
    _write_jsonl(distilled, pairs)
    seen.write_text("[]")

    r = cth.check_distill_accumulation(distilled_path=distilled, seen_path=seen)
    assert r["status"] == "green"


# ── B. launchd 健康 ─────────────────────────────────────────────────────────

def test_launchd_red_when_no_log(tmp_path):
    from finetune import check_training_health as cth

    glob_pattern = str(tmp_path / "no_match_*.log")
    with patch.object(cth, "_launchctl_get_exit_code", return_value=0):
        r = cth.check_launchd_health(log_glob=glob_pattern)
    assert r["status"] == "red"
    assert "從沒 fire" in r["reason"] or "找不到" in r["reason"]


def test_launchd_red_when_stale(tmp_path):
    from finetune import check_training_health as cth

    log = tmp_path / "line_bot_distill_stdout.log"
    log.write_text("old")
    # 把 mtime 設成 50 hr 前
    old_ts = time.time() - 50 * 3600
    os.utime(str(log), (old_ts, old_ts))

    glob_pattern = str(tmp_path / "line_bot_distill_*.log")
    with patch.object(cth, "_launchctl_get_exit_code", return_value=0):
        r = cth.check_launchd_health(log_glob=glob_pattern)
    assert r["status"] == "red"
    assert r["hours_since"] > 36


def test_launchd_green_recent(tmp_path):
    from finetune import check_training_health as cth

    log = tmp_path / "line_bot_distill_stdout.log"
    log.write_text("recent")
    # mtime now
    glob_pattern = str(tmp_path / "line_bot_distill_*.log")
    with patch.object(cth, "_launchctl_get_exit_code", return_value=0):
        r = cth.check_launchd_health(log_glob=glob_pattern)
    assert r["status"] == "green"
    assert r["hours_since"] < 1


# ── C. LoRA 狀態 ────────────────────────────────────────────────────────────

def test_lora_status_no_adapters_accumulating(tmp_path):
    from finetune import check_training_health as cth

    adapters_dir = tmp_path / "adapters"
    adapters_dir.mkdir()
    config = tmp_path / "local_llm_config.py"
    config.write_text("ACTIVE_ADAPTER = None\n")

    r = cth.check_lora_status(
        adapters_dir=adapters_dir,
        eval_dir=tmp_path / "eval",
        config_path=config,
        total_pairs=500,
    )
    assert r["status"] == "green"
    assert "累積中" in r["reason"]
    assert r["days_to_target"] > 0


def test_lora_status_adapters_no_active(tmp_path):
    from finetune import check_training_health as cth

    adapters_dir = tmp_path / "adapters"
    adapters_dir.mkdir()
    (adapters_dir / "v1").mkdir()
    config = tmp_path / "local_llm_config.py"
    config.write_text("ACTIVE_ADAPTER = None\n")

    r = cth.check_lora_status(
        adapters_dir=adapters_dir,
        eval_dir=tmp_path / "eval",
        config_path=config,
        total_pairs=3500,
    )
    assert r["status"] == "yellow"
    assert "gate 沒過" in r["reason"] or "沒上線" in r["reason"]


def test_lora_status_active_adapter(tmp_path):
    from finetune import check_training_health as cth

    adapters_dir = tmp_path / "adapters"
    adapters_dir.mkdir()
    (adapters_dir / "v1").mkdir()
    config = tmp_path / "local_llm_config.py"
    config.write_text('ACTIVE_ADAPTER = "finetune/adapters/v1"\n')

    r = cth.check_lora_status(
        adapters_dir=adapters_dir,
        eval_dir=tmp_path / "eval",
        config_path=config,
        total_pairs=3500,
    )
    assert r["status"] == "green"
    assert "線上版" in r["reason"]
    assert r["active"] == "finetune/adapters/v1"


# ── D. 進步幅度 ─────────────────────────────────────────────────────────────

def test_progress_yellow_insufficient(tmp_path):
    from finetune import check_training_health as cth

    eval_dir = tmp_path / "eval_results"
    eval_dir.mkdir()
    # 0 個 file
    r = cth.check_progress_delta(eval_dir=eval_dir)
    assert r["status"] == "yellow"
    assert "資料不足" in r["reason"]


def test_progress_green_when_improving(tmp_path):
    from finetune import check_training_health as cth

    eval_dir = tmp_path / "eval_results"
    eval_dir.mkdir()
    older = eval_dir / "eval_metrics_001.json"
    newer = eval_dir / "eval_metrics_002.json"
    older.write_text(json.dumps({"metrics": {
        "violation_rate": 0.20,
        "chinese_pass_rate": 0.85,
        "rule0_pass_rate": 0.65,
        "judge_avg_score": 6.0,
    }}))
    # mtime 設定 older 比 newer 早
    t = time.time()
    os.utime(str(older), (t - 100, t - 100))
    newer.write_text(json.dumps({"metrics": {
        "violation_rate": 0.10,   # 改善
        "chinese_pass_rate": 0.95,  # 改善
        "rule0_pass_rate": 0.80,    # 改善
        "judge_avg_score": 7.5,     # 改善
    }}))
    os.utime(str(newer), (t, t))

    r = cth.check_progress_delta(eval_dir=eval_dir)
    assert r["status"] == "green"
    assert r["score"] >= 2


def test_progress_red_when_regressing(tmp_path):
    from finetune import check_training_health as cth

    eval_dir = tmp_path / "eval_results"
    eval_dir.mkdir()
    older = eval_dir / "eval_metrics_001.json"
    newer = eval_dir / "eval_metrics_002.json"
    older.write_text(json.dumps({"metrics": {
        "violation_rate": 0.10,
        "chinese_pass_rate": 0.95,
        "rule0_pass_rate": 0.80,
        "judge_avg_score": 8.0,
    }}))
    t = time.time()
    os.utime(str(older), (t - 100, t - 100))
    newer.write_text(json.dumps({"metrics": {
        "violation_rate": 0.20,    # 退步
        "chinese_pass_rate": 0.80,  # 退步
        "rule0_pass_rate": 0.70,    # 退步
        "judge_avg_score": 6.0,     # 退步
    }}))
    os.utime(str(newer), (t, t))

    r = cth.check_progress_delta(eval_dir=eval_dir)
    assert r["status"] == "red"
    assert r["score"] <= -1


# ── E. organic correction ──────────────────────────────────────────────────

@pytest.fixture
def temp_db(tmp_path):
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute("""
        CREATE TABLE persona_notes (
            group_id TEXT NOT NULL,
            note_id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            scenario TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'rule_violation'
        )
    """)
    con.commit()
    con.close()
    return db


def test_organic_red_too_many(temp_db):
    from finetune import check_training_health as cth

    con = sqlite3.connect(str(temp_db))
    now = _now()
    # 25 條糾正 in 7d → red
    for i in range(25):
        con.execute(
            "INSERT INTO persona_notes(group_id, kind, scenario, content, created_at, source) "
            "VALUES (?, 'correction', 's', 'c', ?, 'organic')",
            ("G", now - 86400),
        )
    con.commit()
    con.close()

    r = cth.check_organic_corrections(db_path=temp_db)
    assert r["status"] == "red"
    assert r["count_7d"] == 25


def test_organic_yellow_zero_30d(temp_db):
    from finetune import check_training_health as cth

    r = cth.check_organic_corrections(db_path=temp_db)
    assert r["status"] == "yellow"
    assert r["count_7d"] == 0


def test_organic_green_healthy(temp_db):
    from finetune import check_training_health as cth

    con = sqlite3.connect(str(temp_db))
    now = _now()
    # 8 條 in 7d → green
    for i in range(8):
        con.execute(
            "INSERT INTO persona_notes(group_id, kind, scenario, content, created_at, source) "
            "VALUES (?, 'correction', 's', 'c', ?, 'organic')",
            ("G", now - 86400),
        )
    con.commit()
    con.close()

    r = cth.check_organic_corrections(db_path=temp_db)
    assert r["status"] == "green"
    assert r["count_7d"] == 8


# ── F. render_report ───────────────────────────────────────────────────────

def test_render_all_green_one_liner():
    from finetune import check_training_health as cth

    checks = {
        "distill": {
            "status": "green",
            "reason": "ok",
            "total_real_pairs": 500,
            "pairs_7d": 70,
            "pairs_30d": 300,
        },
        "launchd": {"status": "green", "reason": "ok"},
        "lora": {
            "status": "green",
            "reason": "累積中",
            "days_to_target": 250,
            "active": None,
            "adapters": [],
        },
        "progress": {"status": "green", "reason": "ok", "details": []},
        "organic": {"status": "green", "reason": "ok", "count_7d": 8, "count_30d": 30},
    }
    text, has_red = cth.render_report(checks)
    assert has_red is False
    assert text.startswith("✅")
    assert "500" in text


def test_render_with_red_full_report():
    from finetune import check_training_health as cth

    checks = {
        "distill": {
            "status": "red",
            "reason": "太少",
            "total_real_pairs": 5,
            "pairs_7d": 5,
            "pairs_30d": 5,
        },
        "launchd": {"status": "green", "reason": "ok"},
        "lora": {
            "status": "green",
            "reason": "累積中（5 / 3000）",
            "days_to_target": 300,
            "active": None,
            "adapters": [],
        },
        "progress": {"status": "yellow", "reason": "資料不足", "details": []},
        "organic": {
            "status": "yellow", "reason": "0",
            "count_7d": 0, "count_30d": 0,
        },
    }
    text, has_red = cth.render_report(checks)
    assert has_red is True
    assert "@here" in text
    assert "🔴" in text
    assert "Distill 累積" in text


# ── G. push_discord — graceful degrade ─────────────────────────────────────

def test_push_discord_no_webhook_url(monkeypatch):
    """No webhook URL AND notify_discord Bot DM unavailable → graceful False.

    push_discord() tries Bot DM (notify_discord.send_dm) first, webhook second.
    To test the "all channels unavailable" graceful-degrade path, mock both off.
    """
    from finetune import check_training_health as cth
    import sys
    sys.path.insert(0, str(cth.LINE_BOT))
    import notify_discord

    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(notify_discord, "TOKEN", None, raising=False)
    monkeypatch.setattr(notify_discord, "USER_ID", None, raising=False)

    ok = cth.push_discord("test")
    assert ok is False  # 沒 webhook 也沒 Bot DM 應 graceful degrade


def test_push_discord_request_exception(monkeypatch):
    from finetune import check_training_health as cth

    def _boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr("requests.post", _boom)
    ok = cth.push_discord("test", webhook_url="https://example.com/webhook")
    assert ok is False  # 不 raise


def test_push_discord_success(monkeypatch):
    from finetune import check_training_health as cth

    class _Resp:
        status_code = 204
        text = ""

    def _ok(*a, **kw):
        return _Resp()

    monkeypatch.setattr("requests.post", _ok)
    ok = cth.push_discord("test", webhook_url="https://example.com/webhook")
    assert ok is True


# ── H. dry-run 整合 ────────────────────────────────────────────────────────

def test_dry_run_does_not_push(monkeypatch, capsys):
    from finetune import check_training_health as cth

    # 整套都 mock
    monkeypatch.setattr(cth, "run_all_checks", lambda: {
        "distill": {
            "status": "green", "reason": "ok",
            "total_real_pairs": 100, "pairs_7d": 70, "pairs_30d": 100,
        },
        "launchd": {"status": "green", "reason": "ok"},
        "lora": {
            "status": "green", "reason": "ok",
            "days_to_target": 300, "active": None, "adapters": [],
        },
        "progress": {"status": "green", "reason": "ok", "details": []},
        "organic": {
            "status": "green", "reason": "ok",
            "count_7d": 6, "count_30d": 30,
        },
    })

    push_called = []

    def _fake_push(*a, **kw):
        push_called.append(True)
        return True

    monkeypatch.setattr(cth, "push_discord", _fake_push)
    rc = cth.main(["--dry-run"])
    assert rc == 0
    assert len(push_called) == 0  # dry-run 不該 push
    captured = capsys.readouterr()
    assert "DRY-RUN" in captured.out
