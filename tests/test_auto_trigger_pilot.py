"""Tests for finetune/auto_trigger_pilot.py — 條件觸發 LoRA pilot 訓練。

Coverage:
  (a) 條件全滿足 → 觸發 train_lora（subprocess 被 mock）
  (b) corrections < 30 → 不觸發
  (c) pairs < 80 → 不觸發
  (d) 上次 pilot 距今 < 7 天 → 不觸發
  (e) gate fail 後 ACTIVE_ADAPTER 不更新（subprocess returncode 1，pilot_runs.json 仍記錄）
  (f) ACTIVE_ADAPTER 已存在 + judge_avg ≥ 7 → 不觸發
  (g) ACTIVE_ADAPTER 已存在 + judge_avg < 7 → 觸發（adapter 表現不夠好）
  (h) pilot_runs.json append 行為（多次寫入累加）
  (i) Discord notifier mocked，trigger 成功 / 失敗都會呼叫
  (j) dry_run 模式：條件命中也不觸發 subprocess
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# bootstrap env
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "finetune"))


# ── helpers ──────────────────────────────────────────────────────────────────


def _mk_db_with_organic(db_path: Path, n_organic: int) -> None:
    """新建空 SQLite DB + persona_notes table，塞 n_organic 條 organic correction。"""
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE persona_notes (
            group_id   TEXT NOT NULL,
            note_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            kind       TEXT NOT NULL,
            scenario   TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            source     TEXT NOT NULL DEFAULT 'rule_violation'
        )
        """
    )
    now = int(time.time())
    for i in range(n_organic):
        con.execute(
            "INSERT INTO persona_notes (group_id, kind, scenario, content, created_at, source) "
            "VALUES (?, 'correction', 'sc', 'ct', ?, 'organic')",
            ("g", now - i * 60),
        )
    # 加幾條非 organic 確保只算 source='organic'
    con.execute(
        "INSERT INTO persona_notes (group_id, kind, scenario, content, created_at, source) "
        "VALUES ('g', 'example', 's', 'c', ?, 'rule_violation')",
        (now,),
    )
    con.commit()
    con.close()


def _write_jsonl(path: Path, n_pairs: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for i in range(n_pairs):
            f.write(json.dumps({"messages": [{"role": "user", "content": f"q{i}"}]}) + "\n")


def _write_config(path: Path, active_adapter: str | None) -> None:
    """寫 local_llm_config.py 的最小版本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if active_adapter is None:
        path.write_text("ACTIVE_ADAPTER = None\n", encoding="utf-8")
    else:
        path.write_text(f'ACTIVE_ADAPTER = "{active_adapter}"\n', encoding="utf-8")


def _write_eval_metrics(eval_dir: Path, judge_avg: float | None) -> None:
    eval_dir.mkdir(parents=True, exist_ok=True)
    out = eval_dir / "adapter.json"
    metrics = {"violation_rate": 0.05, "chinese_pass_rate": 0.97, "rule0_pass_rate": 0.85}
    if judge_avg is not None:
        metrics["judge_avg_score"] = judge_avg
    out.write_text(json.dumps({"metrics": metrics, "samples": []}), encoding="utf-8")


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """準備 isolated 環境：DB / data dir / eval dir / config / pilot_runs。"""
    from finetune import auto_trigger_pilot as atp

    db = tmp_path / "line_bot.db"
    data = tmp_path / "data"
    eval_dir = tmp_path / "eval_results"
    config = tmp_path / "local_llm_config.py"
    pilot_runs = data / "pilot_runs.json"

    monkeypatch.setattr(atp, "DB_PATH", db)
    monkeypatch.setattr(atp, "DATA_DIR", data)
    monkeypatch.setattr(atp, "TRAIN_JSONL", data / "train.jsonl")
    monkeypatch.setattr(atp, "VAL_JSONL", data / "val.jsonl")
    monkeypatch.setattr(atp, "TEST_JSONL", data / "test.jsonl")
    monkeypatch.setattr(atp, "PILOT_RUNS_FILE", pilot_runs)
    monkeypatch.setattr(atp, "EVAL_RESULTS_DIR", eval_dir)
    monkeypatch.setattr(atp, "CONFIG_PATH", config)

    return {
        "atp": atp,
        "db": db,
        "data": data,
        "eval_dir": eval_dir,
        "config": config,
        "pilot_runs": pilot_runs,
        "tmp_path": tmp_path,
    }


# ── (a) 條件全滿足 → 觸發 ──────────────────────────────────────────────────


def test_all_conditions_met_triggers_train(env):
    atp = env["atp"]
    _mk_db_with_organic(env["db"], 30)
    _write_jsonl(env["data"] / "train.jsonl", 60)
    _write_jsonl(env["data"] / "val.jsonl", 10)
    _write_jsonl(env["data"] / "test.jsonl", 10)
    _write_config(env["config"], None)  # ACTIVE_ADAPTER=None → cond_adapter=True

    cond = atp.evaluate_conditions()
    assert cond["should_trigger"] is True
    assert cond["organic_count"] == 30
    assert cond["pairs_count"] == 80

    fake_runner = MagicMock()
    fake_runner.return_value = MagicMock(
        returncode=0,
        stdout="[OK] 4/4 gate 通過\n",
        stderr="",
    )
    notifier = MagicMock(return_value=True)
    rc = atp.run(runner=fake_runner, notifier=notifier)
    assert rc == 0
    fake_runner.assert_called_once()
    cmd = fake_runner.call_args[0][0]
    assert "--pilot" in cmd
    assert "--cloud" in cmd and "lightning" in cmd

    runs = json.loads(env["pilot_runs"].read_text(encoding="utf-8"))
    assert len(runs) == 1
    assert runs[0]["gate_result"] == "4/4 通過"
    assert runs[0]["pairs"] == 80
    assert runs[0]["organic_count"] == 30
    notifier.assert_called_once()


# ── (b) corrections < 30 → SFT pilot 仍會觸發 (per 2026-05-09 設計變更) ──────


def test_below_organic_threshold_triggers_sft_pilot(env, capsys):
    """2026-05-09 改：SFT pilot 不 require organic; organic<30 仍會觸發 SFT (DPO 才需 organic)."""
    atp = env["atp"]
    _mk_db_with_organic(env["db"], 29)
    _write_jsonl(env["data"] / "train.jsonl", 100)
    _write_config(env["config"], None)

    cond = atp.evaluate_conditions()
    assert cond["should_trigger"] is True       # SFT 模式仍觸發
    assert cond["cond_organic"] is False        # 29 < 30 OK
    assert "SFT" in cond["reason"]              # mode=SFT


# ── (c) pairs < 80 → 不觸發 ────────────────────────────────────────────────


def test_below_pairs_threshold_does_not_trigger(env, capsys):
    atp = env["atp"]
    _mk_db_with_organic(env["db"], 50)
    _write_jsonl(env["data"] / "train.jsonl", 50)
    _write_jsonl(env["data"] / "val.jsonl", 10)
    _write_jsonl(env["data"] / "test.jsonl", 10)  # total=70 < 80
    _write_config(env["config"], None)

    cond = atp.evaluate_conditions()
    assert cond["should_trigger"] is False
    assert cond["cond_pairs"] is False
    assert cond["pairs_count"] == 70

    fake_runner = MagicMock()
    rc = atp.run(runner=fake_runner)
    assert rc == 0
    fake_runner.assert_not_called()
    assert "70/80" in capsys.readouterr().out


# ── (d) 7 天內已跑過 → 不觸發 ──────────────────────────────────────────────


def test_recent_pilot_within_cooldown_does_not_trigger(env, capsys):
    atp = env["atp"]
    _mk_db_with_organic(env["db"], 50)
    _write_jsonl(env["data"] / "train.jsonl", 100)
    _write_config(env["config"], None)

    # 上次 pilot 3 天前
    env["pilot_runs"].parent.mkdir(parents=True, exist_ok=True)
    recent_ts = time.time() - 3 * 86400
    env["pilot_runs"].write_text(
        json.dumps([{"ts": recent_ts, "pairs": 100, "organic_count": 50, "gate_result": "0/4 失敗"}]),
        encoding="utf-8",
    )

    cond = atp.evaluate_conditions()
    assert cond["should_trigger"] is False
    assert cond["cond_cooldown"] is False

    fake_runner = MagicMock()
    rc = atp.run(runner=fake_runner)
    assert rc == 0
    fake_runner.assert_not_called()
    assert "cooldown" in capsys.readouterr().out


# ── (e) gate fail：subprocess returncode 1 → ACTIVE_ADAPTER 不更新 ─────────


def test_gate_fail_does_not_update_active_adapter(env):
    atp = env["atp"]
    _mk_db_with_organic(env["db"], 30)
    _write_jsonl(env["data"] / "train.jsonl", 100)
    _write_config(env["config"], None)

    fake_runner = MagicMock()
    # 模擬 train_lora.py exit code 1（gate fail，acceptance_gate 沒過）
    fake_runner.return_value = MagicMock(
        returncode=1,
        stdout="[FAIL] 2/4 gate 失敗，**拒絕** 上線\n",
        stderr="",
    )
    notifier = MagicMock(return_value=True)
    rc = atp.run(runner=fake_runner, notifier=notifier)
    assert rc != 0  # 失敗回傳

    # ACTIVE_ADAPTER 仍為 None（acceptance_gate 沒呼到 update_active_adapter）
    cfg_text = env["config"].read_text(encoding="utf-8")
    assert "ACTIVE_ADAPTER = None" in cfg_text

    # pilot_runs.json 仍記錄（即使失敗）
    runs = json.loads(env["pilot_runs"].read_text(encoding="utf-8"))
    assert len(runs) == 1
    assert runs[0]["returncode"] == 1
    assert runs[0]["gate_result"] == "2/4 失敗"

    # 失敗訊息也推 Discord
    notifier.assert_called_once()
    msg = notifier.call_args[0][0]
    assert "失敗" in msg


# ── (f) adapter 已存在 + judge ≥ 7 → 不觸發 ───────────────────────────────


def test_adapter_with_good_judge_does_not_trigger(env, capsys):
    atp = env["atp"]
    _mk_db_with_organic(env["db"], 50)
    _write_jsonl(env["data"] / "train.jsonl", 100)
    _write_config(env["config"], "/abs/path/to/adapter")
    _write_eval_metrics(env["eval_dir"], judge_avg=7.5)

    cond = atp.evaluate_conditions()
    assert cond["should_trigger"] is False
    assert cond["cond_adapter"] is False
    assert cond["judge_avg"] == pytest.approx(7.5)

    fake_runner = MagicMock()
    rc = atp.run(runner=fake_runner)
    assert rc == 0
    fake_runner.assert_not_called()


# ── (g) adapter 存在但 judge < 7 → 觸發重訓 ──────────────────────────────


def test_adapter_with_low_judge_triggers_retrain(env):
    atp = env["atp"]
    _mk_db_with_organic(env["db"], 30)
    _write_jsonl(env["data"] / "train.jsonl", 100)
    _write_config(env["config"], "/abs/path/to/adapter")
    _write_eval_metrics(env["eval_dir"], judge_avg=6.2)

    cond = atp.evaluate_conditions()
    assert cond["should_trigger"] is True
    assert cond["cond_adapter"] is True

    fake_runner = MagicMock()
    fake_runner.return_value = MagicMock(returncode=0, stdout="4/4 gate 通過", stderr="")
    rc = atp.run(runner=fake_runner, notifier=MagicMock())
    assert rc == 0
    fake_runner.assert_called_once()


# ── (h) pilot_runs.json 累加 ───────────────────────────────────────────────


def test_pilot_runs_appends(env):
    atp = env["atp"]
    _mk_db_with_organic(env["db"], 30)
    _write_jsonl(env["data"] / "train.jsonl", 100)
    _write_config(env["config"], None)

    # 第一次寫入
    atp._append_pilot_run({"ts": 1.0, "pairs": 100, "organic_count": 30})
    atp._append_pilot_run({"ts": 2.0, "pairs": 100, "organic_count": 30})
    runs = atp._read_pilot_runs()
    assert len(runs) == 2
    assert runs[0]["ts"] == 1.0
    assert runs[1]["ts"] == 2.0


# ── (i) dry_run：條件命中也不觸發 ──────────────────────────────────────────


def test_dry_run_does_not_trigger_even_when_conditions_met(env, capsys):
    atp = env["atp"]
    _mk_db_with_organic(env["db"], 30)
    _write_jsonl(env["data"] / "train.jsonl", 100)
    _write_config(env["config"], None)

    cond = atp.evaluate_conditions()
    assert cond["should_trigger"] is True

    fake_runner = MagicMock()
    rc = atp.run(dry_run=True, runner=fake_runner, notifier=MagicMock())
    assert rc == 0
    fake_runner.assert_not_called()
    assert "DRY-RUN" in capsys.readouterr().out


# ── (j) 解析 gate output ───────────────────────────────────────────────────


def test_parse_gate_output_4_of_4():
    from finetune import auto_trigger_pilot as atp
    assert atp._parse_gate_output("[OK] 4/4 gate 通過") == "4/4 通過"


def test_parse_gate_output_2_of_4():
    from finetune import auto_trigger_pilot as atp
    assert atp._parse_gate_output("[FAIL] 2/4 gate 失敗，**拒絕** 上線") == "2/4 失敗"


def test_parse_gate_output_unknown():
    from finetune import auto_trigger_pilot as atp
    assert atp._parse_gate_output("random text") is None


# ── (k) 無 pilot_runs.json (從未跑過) → cooldown 通過 ─────────────────────


def test_no_prior_pilot_passes_cooldown(env):
    atp = env["atp"]
    _mk_db_with_organic(env["db"], 30)
    _write_jsonl(env["data"] / "train.jsonl", 100)
    _write_config(env["config"], None)

    cond = atp.evaluate_conditions()
    assert cond["cond_cooldown"] is True
    assert cond["last_pilot_ts"] is None


# ── (l) train_lora.py --pilot flag 存在 ─────────────────────────────────────


def test_train_lora_has_pilot_flag():
    """sanity check：train_lora.py 真的有 --pilot 參數。"""
    from finetune import train_lora as tl
    parser = None
    # parse_args with --help would exit；改用 main 的 SystemExit 路徑驗證
    # 直接看 source 是否含 "--pilot"
    src = Path(tl.__file__).read_text(encoding="utf-8")
    assert '"--pilot"' in src
    assert "PILOT_MIN_PAIRS" in src
    assert "PILOT_GRAD_ACCUM" in src
    # PILOT_GRAD_ACCUM 應 < GRAD_ACCUM 才符合「速度快」設計
    assert tl.PILOT_GRAD_ACCUM < tl.GRAD_ACCUM
    assert tl.PILOT_ITERS < tl.ITERS
    assert tl.PILOT_MIN_PAIRS == 80
