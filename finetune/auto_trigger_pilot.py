"""每日條件觸發 LoRA pilot 訓練 — 不等 3000 對，達門檻即試訓。

跑在 `check_training_health.py` 之後（每日 09:00 launchd），不擋 health。

Pilot 觸發條件（**全部命中**才觸發）：
  1. SQLite persona_notes WHERE source='organic' count ≥ 30
  2. train.jsonl + val.jsonl + test.jsonl 加總 ≥ 80
  3. 上次 pilot 距今 ≥ 7 天（或從未跑過 → 通過）
  4. ACTIVE_ADAPTER == None  OR  上次 eval judge_avg < 7
     (沒 production adapter 或 adapter 表現不夠好需重訓)

命中 → `python finetune/train_lora.py --pilot --cloud lightning`
失敗 → log + Discord notify
成功 → log + Discord notify「pilot run 完成，4-gate 結果：X/4 過」

紀錄寫到 `finetune/data/pilot_runs.json`：
    [{"ts": ..., "pairs": ..., "organic_count": ..., "gate_result": ..., "adapter_path": ...}, ...]

CLI：
    python finetune/auto_trigger_pilot.py            # 真實跑
    python finetune/auto_trigger_pilot.py --dry-run  # 評估條件但不訓
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).parent
LINE_BOT = HERE.parent
sys.path.insert(0, str(LINE_BOT))

DATA_DIR = HERE / "data"
TRAIN_JSONL = DATA_DIR / "train.jsonl"
VAL_JSONL = DATA_DIR / "val.jsonl"
TEST_JSONL = DATA_DIR / "test.jsonl"
PILOT_RUNS_FILE = DATA_DIR / "pilot_runs.json"
EVAL_RESULTS_DIR = HERE / "eval_results"
DB_PATH = LINE_BOT / "line_bot.db"
CONFIG_PATH = LINE_BOT / "local_llm_config.py"
TRAIN_LORA_SCRIPT = HERE / "train_lora.py"

# 觸發 thresholds（跟 task spec 對齊）
ORGANIC_MIN = 30          # persona_notes source='organic' count ≥ 30
PAIRS_MIN = 80            # train+val+test 加總 ≥ 80
COOLDOWN_DAYS = 7         # 上次 pilot ≥ 7 天前
JUDGE_RETRAIN_THRESHOLD = 7.0   # 上次 judge_avg < 7 視為「不夠好需重訓」

logger = logging.getLogger("auto_trigger_pilot")


# ── helpers ──────────────────────────────────────────────────────────────────


def _now_ts() -> float:
    return time.time()


def _count_organic_corrections(db_path: Optional[Path] = None) -> int:
    """SQLite persona_notes WHERE source='organic' total count（不分時間窗）。

    `db_path=None` → 用模組層 DB_PATH（讓 monkeypatch 生效）。
    """
    if db_path is None:
        db_path = _module_attr("DB_PATH")
    if not db_path.exists():
        return 0
    try:
        con = sqlite3.connect(str(db_path))
        n = con.execute(
            "SELECT COUNT(*) FROM persona_notes WHERE source = 'organic'"
        ).fetchone()[0]
        con.close()
        return int(n or 0)
    except Exception as e:
        logger.warning("count_organic_corrections failed: %s", e)
        return 0


def _count_pairs(
    *,
    train_path: Optional[Path] = None,
    val_path: Optional[Path] = None,
    test_path: Optional[Path] = None,
) -> int:
    """train.jsonl + val.jsonl + test.jsonl 行數加總。任一路徑 None → 用模組層常數。"""
    if train_path is None:
        train_path = _module_attr("TRAIN_JSONL")
    if val_path is None:
        val_path = _module_attr("VAL_JSONL")
    if test_path is None:
        test_path = _module_attr("TEST_JSONL")
    total = 0
    for p in (train_path, val_path, test_path):
        if not p.exists():
            continue
        try:
            with p.open("r", encoding="utf-8") as f:
                total += sum(1 for line in f if line.strip())
        except Exception as e:
            logger.warning("count_pairs %s failed: %s", p, e)
    return total


def _read_pilot_runs(path: Optional[Path] = None) -> list[dict]:
    if path is None:
        path = _module_attr("PILOT_RUNS_FILE")
    if not path.exists():
        return []
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(obj, list):
            return obj
        return []
    except Exception as e:
        logger.warning("read_pilot_runs failed: %s", e)
        return []


def _append_pilot_run(record: dict, path: Optional[Path] = None) -> None:
    """append 一筆 pilot run record，atomic-ish write。"""
    if path is None:
        path = _module_attr("PILOT_RUNS_FILE")
    runs = _read_pilot_runs(path)
    runs.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _last_pilot_ts(path: Optional[Path] = None) -> Optional[float]:
    if path is None:
        path = _module_attr("PILOT_RUNS_FILE")
    runs = _read_pilot_runs(path)
    if not runs:
        return None
    try:
        return max(float(r.get("ts", 0)) for r in runs)
    except Exception:
        return None


def _read_active_adapter(config_path: Optional[Path] = None) -> Optional[str]:
    """parse local_llm_config.py 拿 ACTIVE_ADAPTER 字面值（與 check_training_health 對齊）。"""
    if config_path is None:
        config_path = _module_attr("CONFIG_PATH")
    if not config_path.exists():
        return None
    text = config_path.read_text(encoding="utf-8")
    m = re.search(r"^ACTIVE_ADAPTER\s*=\s*(.+?)\s*(?:#.*)?$", text, re.MULTILINE)
    if not m:
        return None
    val = m.group(1).strip()
    if val == "None":
        return None
    return val.strip("\"'")


def _last_judge_avg(eval_dir: Optional[Path] = None) -> Optional[float]:
    """讀最新 adapter eval 結果的 judge_avg_score，沒檔案 / 解析失敗 → None。"""
    if eval_dir is None:
        eval_dir = _module_attr("EVAL_RESULTS_DIR")
    if not eval_dir.exists():
        return None
    candidates = []
    for name in ("adapter.json",):
        p = eval_dir / name
        if p.exists():
            candidates.append(p)
    candidates += sorted(eval_dir.glob("eval_metrics_*.json"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        return None
    try:
        obj = json.loads(candidates[-1].read_text(encoding="utf-8"))
        metrics = obj.get("metrics", obj) if isinstance(obj, dict) else {}
        v = metrics.get("judge_avg_score")
        return float(v) if v is not None else None
    except Exception:
        return None


def _module_attr(name: str):
    """getattr on this module — needed so monkeypatch 改的常數能被讀到。"""
    return globals()[name]


# ── 條件評估 ─────────────────────────────────────────────────────────────────


def evaluate_conditions(
    *,
    db_path: Optional[Path] = None,
    train_path: Optional[Path] = None,
    val_path: Optional[Path] = None,
    test_path: Optional[Path] = None,
    pilot_runs_path: Optional[Path] = None,
    config_path: Optional[Path] = None,
    eval_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict:
    """評估觸發條件，回 dict 含 should_trigger + 各條件值 + reason。

    所有 path 預設 None → 由 helper 用模組層常數（讓 monkeypatch 生效）。
    """
    if now is None:
        now = _now_ts()

    organic_count = _count_organic_corrections(db_path)
    pairs_count = _count_pairs(train_path=train_path, val_path=val_path, test_path=test_path)
    last_ts = _last_pilot_ts(pilot_runs_path)
    days_since_last = (now - last_ts) / 86400 if last_ts else None
    active_adapter = _read_active_adapter(config_path)
    judge_avg = _last_judge_avg(eval_dir)

    cond_organic = organic_count >= ORGANIC_MIN
    cond_pairs = pairs_count >= PAIRS_MIN
    cond_cooldown = (last_ts is None) or (days_since_last is not None and days_since_last >= COOLDOWN_DAYS)

    # 條件 4：沒 production adapter OR 上次 judge < 7
    if active_adapter is None:
        cond_adapter = True
        adapter_reason = "ACTIVE_ADAPTER=None（沒 production adapter）"
    else:
        if judge_avg is None:
            cond_adapter = True
            adapter_reason = f"adapter={active_adapter} 但 judge_avg 未知 → 重訓"
        elif judge_avg < JUDGE_RETRAIN_THRESHOLD:
            cond_adapter = True
            adapter_reason = (
                f"adapter={active_adapter}, judge_avg={judge_avg:.2f} < {JUDGE_RETRAIN_THRESHOLD}"
            )
        else:
            cond_adapter = False
            adapter_reason = (
                f"adapter={active_adapter}, judge_avg={judge_avg:.2f} ≥ {JUDGE_RETRAIN_THRESHOLD}（無需重訓）"
            )

    # 2026-05-09 改：SFT pilot 不需要 organic（DPO 才需要），改成 organic 是 bonus
    # SFT pilot: pairs ≥ 80 + cooldown + adapter（organic 不 require）
    # DPO pilot: 上面 + organic ≥ 30（後續再做）
    pilot_mode = "DPO" if cond_organic else "SFT"
    should_trigger = cond_pairs and cond_cooldown and cond_adapter

    if should_trigger:
        reason = (
            f"條件全滿足 → 觸發 {pilot_mode} pilot "
            f"(organic={organic_count}/{ORGANIC_MIN}, pairs={pairs_count}/{PAIRS_MIN}, "
            f"days_since_last={days_since_last}, adapter:{adapter_reason})"
        )
    else:
        # 列具體哪一條沒過
        miss = []
        if not cond_pairs:
            miss.append(f"pairs={pairs_count}/{PAIRS_MIN}")
        if not cond_cooldown:
            miss.append(f"cooldown={days_since_last:.1f}d < {COOLDOWN_DAYS}d")
        if not cond_adapter:
            miss.append(f"adapter_ok ({adapter_reason})")
        reason = "條件不滿足，跳過（" + ", ".join(miss) + "）"

    return {
        "should_trigger": should_trigger,
        "reason": reason,
        "organic_count": organic_count,
        "pairs_count": pairs_count,
        "days_since_last": days_since_last,
        "last_pilot_ts": last_ts,
        "active_adapter": active_adapter,
        "judge_avg": judge_avg,
        "cond_organic": cond_organic,
        "cond_pairs": cond_pairs,
        "cond_cooldown": cond_cooldown,
        "cond_adapter": cond_adapter,
    }


# ── 觸發執行 ─────────────────────────────────────────────────────────────────


def _build_train_cmd(*, cloud: str = "lightning", local: bool = False) -> list[str]:
    """組訓練指令；預設走 Lightning AI（mlx-lm 不一定支援 LoRA training）。"""
    py = sys.executable
    cmd = [py, str(TRAIN_LORA_SCRIPT), "--pilot"]
    if local:
        return cmd  # 本機 mlx
    cmd += ["--cloud", cloud]
    return cmd


def trigger_pilot_run(
    *,
    pairs_count: int,
    organic_count: int,
    cloud: str = "lightning",
    local: bool = False,
    runner: Optional[callable] = None,
    now: Optional[float] = None,
) -> dict:
    """執行 pilot training subprocess，回 result dict（含 gate_result）。

    `runner` 預設用 `subprocess.run`；測試可注入 mock。
    成功與否都會 append 一筆紀錄到 pilot_runs.json。
    """
    if now is None:
        now = _now_ts()
    if runner is None:
        runner = subprocess.run

    cmd = _build_train_cmd(cloud=cloud, local=local)
    logger.info("[TRIGGER] %s", " ".join(cmd))

    started = now
    completed = None
    rc = -1
    stdout_text = ""
    stderr_text = ""
    error: Optional[str] = None
    try:
        cp = runner(cmd, capture_output=True, text=True, timeout=60 * 60 * 6)
        rc = cp.returncode
        stdout_text = cp.stdout or ""
        stderr_text = cp.stderr or ""
        completed = _now_ts()
    except Exception as e:
        error = str(e)
        logger.exception("trigger_pilot_run subprocess failed: %s", e)

    # 嘗試解析 4-gate 結果（acceptance_gate.py 印「4/4 gate 通過」或「N/4 gate 失敗」）
    gate_result = _parse_gate_output(stdout_text + "\n" + stderr_text)

    # adapter path 走預設目錄
    adapter_path = str((HERE / "adapters").resolve())

    record = {
        "ts": started,
        "completed_ts": completed,
        "pairs": pairs_count,
        "organic_count": organic_count,
        "cmd": cmd,
        "returncode": rc,
        "gate_result": gate_result,
        "adapter_path": adapter_path,
        "error": error,
    }
    _append_pilot_run(record)
    return record


def _parse_gate_output(text: str) -> Optional[str]:
    """從 train_lora / acceptance_gate stdout 抓 'X/4 gate 通過|失敗'。"""
    if not text:
        return None
    m = re.search(r"(\d+)/4\s*gate\s*(通過|失敗)", text)
    if m:
        return f"{m.group(1)}/4 {m.group(2)}"
    if "[OK] 4/4 gate" in text or "4/4 gate 通過" in text:
        return "4/4 通過"
    if "[FAIL]" in text and "/4 gate" in text:
        return "FAIL（無法解析）"
    return None


# ── Discord 推播 ─────────────────────────────────────────────────────────────


def _push_discord(text: str) -> bool:
    """重用 notify_discord.send_dm；沒設就 graceful skip。"""
    try:
        import notify_discord  # type: ignore
        if getattr(notify_discord, "TOKEN", None) and getattr(notify_discord, "USER_ID", None):
            return bool(notify_discord.send_dm(text[:1900]))
    except Exception as e:
        logger.warning("push_discord failed: %s", e)
    return False


def _format_success(record: dict) -> str:
    g = record.get("gate_result") or "未知"
    p = record.get("pairs", 0)
    o = record.get("organic_count", 0)
    return (
        f"LoRA pilot run 完成\n"
        f"- pairs: {p}\n"
        f"- organic corrections: {o}\n"
        f"- 4-gate 結果：{g}\n"
        f"- adapter: {record.get('adapter_path', '?')}"
    )


def _format_failure(record: dict) -> str:
    err = record.get("error") or f"returncode={record.get('returncode')}"
    p = record.get("pairs", 0)
    o = record.get("organic_count", 0)
    return (
        f"LoRA pilot run 失敗\n"
        f"- pairs: {p}\n"
        f"- organic corrections: {o}\n"
        f"- 錯誤：{err}"
    )


# ── 主流程 ───────────────────────────────────────────────────────────────────


def run(
    *,
    dry_run: bool = False,
    local: bool = False,
    cloud: str = "lightning",
    runner: Optional[callable] = None,
    notifier: Optional[callable] = None,
) -> int:
    """評估條件，命中則觸發訓練；命中與否都印一行訊息。

    Returns:
      0 = 條件不滿足（跳過）或 訓練成功
      非 0 = 訓練失敗
    """
    if notifier is None:
        notifier = _push_discord

    cond = evaluate_conditions()

    print(cond["reason"])
    logger.info("conditions: %s", json.dumps(cond, default=str, ensure_ascii=False))

    if not cond["should_trigger"]:
        return 0

    if dry_run:
        print("[DRY-RUN] 條件命中但 --dry-run，未觸發訓練")
        return 0

    record = trigger_pilot_run(
        pairs_count=cond["pairs_count"],
        organic_count=cond["organic_count"],
        cloud=cloud,
        local=local,
        runner=runner,
    )
    if record.get("returncode") == 0 and not record.get("error"):
        msg = _format_success(record)
        print(msg)
        try:
            notifier(msg)
        except Exception as e:
            logger.warning("notifier exception: %s", e)
        return 0
    else:
        msg = _format_failure(record)
        print(msg, file=sys.stderr)
        try:
            notifier(msg)
        except Exception as e:
            logger.warning("notifier exception: %s", e)
        return record.get("returncode") or 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="評估條件但不真的觸發訓練（驗證流程用）",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="本機 mlx 跑（多半不支援 LoRA training，預設走 cloud）",
    )
    parser.add_argument(
        "--cloud",
        default="lightning",
        choices=["modal", "lightning"],
        help="雲端訓練平台（預設 lightning）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    # load .env if available（Discord notifier 需要）
    try:
        from dotenv import load_dotenv
        load_dotenv(LINE_BOT / ".env")
    except Exception:
        pass

    return run(dry_run=args.dry_run, local=args.local, cloud=args.cloud)


if __name__ == "__main__":
    sys.exit(main())
