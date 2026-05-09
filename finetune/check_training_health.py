"""每日訓練 pipeline 健康檢查 — 推 Discord webhook。

5 大檢查項：
  A. distill 累積（過去 7 / 30 天 pair 數，預期每日 10 對）
  B. launchd 健康（distill-daily plist 上次 fire 距現在多久）
  C. LoRA 訓練狀態（adapters/ 子資料夾 + eval_metrics + ACTIVE_ADAPTER）
  D. 進步幅度（最近 2 個 eval_metrics_*.json 4-metric 比較）
  E. organic correction 累積（persona_notes WHERE source='organic'）

任一紅 → 推 full markdown report + @here；全綠 → 1 行健康摘要。
--dry-run 不推 Discord，只 print。
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

HERE = Path(__file__).parent
LINE_BOT = HERE.parent
sys.path.insert(0, str(LINE_BOT))

DATA_DIR = HERE / "data"
DISTILLED_FILE = DATA_DIR / "distilled.jsonl"
SEEN_FILE = DATA_DIR / "distilled_seen.json"
ADAPTERS_DIR = HERE / "adapters"
EVAL_RESULTS_DIR = HERE / "eval_results"
CONFIG_PATH = LINE_BOT / "local_llm_config.py"
LOGS_DIR = Path.home() / "Library" / "Logs"
DISTILL_LOG_GLOB = str(LOGS_DIR / "line_bot_distill_*.log")
DB_PATH = LINE_BOT / "line_bot.db"

DISTILL_LAUNCHD_LABEL = "com.andrew.line-bot-distill-daily"

# Thresholds
TARGET_DAILY_PAIRS = 10
TRAINING_TARGET_PAIRS = 3000
WEEK_PAIRS_WARN = 50      # < 50 in 7d = yellow
WEEK_PAIRS_RED = 20       # < 20 in 7d = red
LAUNCHD_RED_HOURS = 36    # > 36hr since last fire = red

ORGANIC_WEEK_HIGH = 20    # > 20/week = bot 一直被糾正 = red
ORGANIC_WEEK_HEALTHY = 5  # 5-15/week = healthy
ORGANIC_30D_ZERO = 0      # 0 in 30d = yellow

logger = logging.getLogger("check_training_health")


# ── helpers ──────────────────────────────────────────────────────────────────

def _now_ts() -> float:
    return time.time()


def _epoch_days_ago(days: int) -> int:
    return int(_now_ts() - days * 86400)


# ── A. distill 累積 ──────────────────────────────────────────────────────────

def check_distill_accumulation(
    *,
    distilled_path: Path = DISTILLED_FILE,
    seen_path: Path = SEEN_FILE,
) -> dict:
    """累積真實 pair 數 — 算 distilled.jsonl + sft.jsonl + train.jsonl 三來源。

    - distilled.jsonl: distill_daily.py 從 raw_messages 反向蒸餾（filter mock）
    - sft.jsonl: 早期 distill 從歷史 extract（無 mock 標記，全真）
    - train.jsonl: dataset_builder.py 整合 organic + distilled + sft 後 dedup（最權威）

    pairs_7d / 30d 用 distilled.jsonl 的 distilled_at（其他 source 沒 timestamp）。
    total_real 用 train.jsonl 行數（已去重）。
    """
    distilled_real = 0
    pairs_7d = 0
    pairs_30d = 0
    cutoff_7 = _epoch_days_ago(7)
    cutoff_30 = _epoch_days_ago(30)

    if distilled_path.exists():
        for line in distilled_path.open("r", encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            md = obj.get("metadata", {})
            if md.get("mock", False):
                continue
            distilled_real += 1
            ts = int(md.get("distilled_at", 0))
            if ts >= cutoff_30:
                pairs_30d += 1
            if ts >= cutoff_7:
                pairs_7d += 1

    # 算其他兩來源（不算 7d/30d 因為沒 timestamp）
    sft_count = 0
    sft_path = distilled_path.parent / "sft.jsonl"
    if sft_path.exists():
        with sft_path.open("r", encoding="utf-8") as f:
            sft_count = sum(1 for l in f if l.strip())

    train_count = 0
    train_path = distilled_path.parent / "train.jsonl"
    val_path = distilled_path.parent / "val.jsonl"
    test_path = distilled_path.parent / "test.jsonl"
    for p in (train_path, val_path, test_path):
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                train_count += sum(1 for l in f if l.strip())

    # total_real = max(各來源)，避免低估
    total_real = max(distilled_real + sft_count, train_count)

    seen_mtime = None
    if seen_path.exists():
        seen_mtime = seen_path.stat().st_mtime

    if pairs_7d < WEEK_PAIRS_RED:
        status = "red"
        reason = f"過去 7 天僅累積 {pairs_7d} 對 distill (< {WEEK_PAIRS_RED})，distill 可能掛了"
    elif pairs_7d < WEEK_PAIRS_WARN:
        status = "yellow"
        reason = f"過去 7 天累積 {pairs_7d} 對 distill (< {WEEK_PAIRS_WARN})，慢於預期"
    else:
        status = "green"
        reason = f"過去 7 天累積 {pairs_7d} 對 distill，符合每日 ~{TARGET_DAILY_PAIRS} 對節奏"
    # 累積還少 + sft/organic 已有 → 降為 yellow 而非 red
    if status == "red" and total_real >= WEEK_PAIRS_RED:
        status = "yellow"
        reason = (
            f"過去 7 天 0 對新 distill，但既有 {total_real} 對來自 sft/organic"
            f"（distill_daily 還沒第一次跑）"
        )

    return {
        "status": status,
        "reason": reason,
        "total_real_pairs": total_real,
        "distilled_real": distilled_real,
        "sft_count": sft_count,
        "train_total_count": train_count,
        "pairs_7d": pairs_7d,
        "pairs_30d": pairs_30d,
        "seen_last_modified": seen_mtime,
    }


# ── B. launchd 健康 ──────────────────────────────────────────────────────────

def _launchctl_get_exit_code(label: str) -> Optional[int]:
    """parse `launchctl list` 對應 label 的 exit code。"""
    try:
        out = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as e:
        logger.warning("launchctl list failed: %s", e)
        return None
    for line in out.stdout.splitlines():
        if label in line:
            parts = line.split()
            if len(parts) >= 3:
                try:
                    # PID Status Label
                    return int(parts[1])
                except ValueError:
                    return None
    return None


def _last_distill_fire_mtime(glob_pattern: str = DISTILL_LOG_GLOB) -> Optional[float]:
    """讀 distill log 最新 mtime，當作上次 fire 時間。"""
    files = glob.glob(glob_pattern)
    if not files:
        return None
    return max(Path(f).stat().st_mtime for f in files)


def check_launchd_health(
    *,
    label: str = DISTILL_LAUNCHD_LABEL,
    log_glob: str = DISTILL_LOG_GLOB,
) -> dict:
    """檢查 distill launchd 上次 fire 時間 + exit code。"""
    exit_code = _launchctl_get_exit_code(label)
    last_fire = _last_distill_fire_mtime(log_glob)

    if last_fire is None:
        return {
            "status": "red",
            "reason": "找不到任何 distill log file → launchd 從沒 fire 過 / job 沒 load",
            "last_fire_ts": None,
            "exit_code": exit_code,
            "hours_since": None,
        }

    hours_since = (_now_ts() - last_fire) / 3600

    if hours_since > LAUNCHD_RED_HOURS:
        status = "red"
        reason = (
            f"距離上次 fire {hours_since:.1f} hr (> {LAUNCHD_RED_HOURS}hr)，"
            f"launchd 沒在跑（exit_code={exit_code}）"
        )
    elif exit_code not in (None, 0):
        status = "yellow"
        reason = f"距離上次 fire {hours_since:.1f} hr，但 exit_code={exit_code}（非 0）"
    else:
        status = "green"
        reason = f"上次 fire {hours_since:.1f} hr 前，exit_code={exit_code}"

    return {
        "status": status,
        "reason": reason,
        "last_fire_ts": last_fire,
        "exit_code": exit_code,
        "hours_since": hours_since,
    }


# ── C. LoRA 訓練狀態 ─────────────────────────────────────────────────────────

def _read_active_adapter(config_path: Path = CONFIG_PATH) -> Optional[str]:
    """parse local_llm_config.py 拿 ACTIVE_ADAPTER 字面值。"""
    if not config_path.exists():
        return None
    text = config_path.read_text(encoding="utf-8")
    m = re.search(r"^ACTIVE_ADAPTER\s*=\s*(.+?)\s*(?:#.*)?$", text, re.MULTILINE)
    if not m:
        return None
    val = m.group(1).strip()
    if val == "None":
        return None
    # strip quotes
    return val.strip("\"'")


def _list_adapter_dirs(adapters_dir: Path = ADAPTERS_DIR) -> list[Path]:
    if not adapters_dir.exists():
        return []
    return [p for p in adapters_dir.iterdir() if p.is_dir()]


def check_lora_status(
    *,
    adapters_dir: Path = ADAPTERS_DIR,
    eval_dir: Path = EVAL_RESULTS_DIR,
    config_path: Path = CONFIG_PATH,
    total_pairs: int = 0,
) -> dict:
    """A 已產出 total_pairs；判 adapter 狀態。"""
    adapter_dirs = _list_adapter_dirs(adapters_dir)
    active = _read_active_adapter(config_path)

    if not adapter_dirs:
        # 還沒訓練過
        if total_pairs < TRAINING_TARGET_PAIRS:
            days_left = max(
                1,
                (TRAINING_TARGET_PAIRS - total_pairs) // max(TARGET_DAILY_PAIRS, 1),
            )
            return {
                "status": "green",
                "reason": (
                    f"累積中（{total_pairs} / {TRAINING_TARGET_PAIRS} pairs，"
                    f"預計 {days_left} 天達標）"
                ),
                "adapters": [],
                "active": active,
                "days_to_target": days_left,
            }
        return {
            "status": "yellow",
            "reason": f"已 {total_pairs} 對但還沒訓練過 adapter",
            "adapters": [],
            "active": active,
            "days_to_target": 0,
        }

    if not active:
        return {
            "status": "yellow",
            "reason": (
                f"訓練過 {len(adapter_dirs)} 個 adapter 但 gate 沒過 / 沒上線"
                f"（ACTIVE_ADAPTER=None）"
            ),
            "adapters": [str(p.name) for p in adapter_dirs],
            "active": None,
        }

    return {
        "status": "green",
        "reason": f"線上版：{active}",
        "adapters": [str(p.name) for p in adapter_dirs],
        "active": active,
    }


# ── D. 進步幅度 ──────────────────────────────────────────────────────────────

def _list_eval_metrics_files(eval_dir: Path = EVAL_RESULTS_DIR) -> list[Path]:
    """挑 eval_metrics_*.json，ascending mtime。"""
    if not eval_dir.exists():
        return []
    files = list(eval_dir.glob("eval_metrics_*.json"))
    files.sort(key=lambda p: p.stat().st_mtime)
    return files


def _load_eval_metrics(path: Path) -> Optional[dict]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    # eval_harness 寫的格式 {"metrics": {...}, "samples": [...]}
    return obj.get("metrics") or obj


def check_progress_delta(*, eval_dir: Path = EVAL_RESULTS_DIR) -> dict:
    """比較最近 2 個 eval_metrics 的 4 metric。"""
    files = _list_eval_metrics_files(eval_dir)
    if len(files) < 2:
        return {
            "status": "yellow",
            "reason": f"資料不足（找到 {len(files)} 個 eval_metrics 檔，需 ≥ 2）",
            "score": 0,
            "details": [],
        }

    older = _load_eval_metrics(files[-2])
    newer = _load_eval_metrics(files[-1])
    if not older or not newer:
        return {
            "status": "yellow",
            "reason": "eval_metrics JSON 解析失敗",
            "score": 0,
            "details": [],
        }

    score = 0
    details = []

    # 違規率：越低越好
    o_v = older.get("violation_rate")
    n_v = newer.get("violation_rate")
    if o_v is not None and n_v is not None:
        if n_v < o_v:
            score += 1
            details.append(f"violation_rate {o_v:.3f} → {n_v:.3f} (改善)")
        elif n_v > o_v:
            score -= 1
            details.append(f"violation_rate {o_v:.3f} → {n_v:.3f} (退步)")
        else:
            details.append(f"violation_rate 持平 {n_v:.3f}")

    # 中文比率：越高越好
    o_c = older.get("chinese_pass_rate")
    n_c = newer.get("chinese_pass_rate")
    if o_c is not None and n_c is not None:
        if n_c > o_c:
            score += 1
            details.append(f"chinese_pass_rate {o_c:.3f} → {n_c:.3f} (改善)")
        elif n_c < o_c:
            score -= 1
            details.append(f"chinese_pass_rate {o_c:.3f} → {n_c:.3f} (退步)")
        else:
            details.append(f"chinese_pass_rate 持平 {n_c:.3f}")

    # 規則 0 命中：越高越好
    o_r = older.get("rule0_pass_rate")
    n_r = newer.get("rule0_pass_rate")
    if o_r is not None and n_r is not None:
        if n_r > o_r:
            score += 1
            details.append(f"rule0_pass_rate {o_r:.3f} → {n_r:.3f} (改善)")
        elif n_r < o_r:
            score -= 1
            details.append(f"rule0_pass_rate {o_r:.3f} → {n_r:.3f} (退步)")
        else:
            details.append(f"rule0_pass_rate 持平 {n_r:.3f}")

    # judge 分：越高越好
    o_j = older.get("judge_avg_score")
    n_j = newer.get("judge_avg_score")
    if o_j is not None and n_j is not None:
        if n_j > o_j:
            score += 1
            details.append(f"judge_avg_score {o_j:.2f} → {n_j:.2f} (改善)")
        elif n_j < o_j:
            score -= 1
            details.append(f"judge_avg_score {o_j:.2f} → {n_j:.2f} (退步)")
        else:
            details.append(f"judge_avg_score 持平 {n_j:.2f}")

    if score >= 2:
        status = "green"
        reason = f"進步（score={score}）"
    elif score <= -1:
        status = "red"
        reason = f"退步（score={score}）"
    else:
        status = "yellow"
        reason = f"持平（score={score}）"

    return {
        "status": status,
        "reason": reason,
        "score": score,
        "details": details,
        "older_file": str(files[-2].name),
        "newer_file": str(files[-1].name),
    }


# ── E. organic correction 累積 ───────────────────────────────────────────────

def check_organic_corrections(
    *,
    db_path: Path = DB_PATH,
) -> dict:
    """SQLite persona_notes WHERE source='organic'，count last 7 / 30 days。"""
    if not db_path.exists():
        return {
            "status": "yellow",
            "reason": "DB 不存在",
            "count_7d": 0,
            "count_30d": 0,
        }

    cutoff_7 = _epoch_days_ago(7)
    cutoff_30 = _epoch_days_ago(30)

    try:
        con = sqlite3.connect(str(db_path))
        c7 = con.execute(
            "SELECT COUNT(*) FROM persona_notes "
            "WHERE source = 'organic' AND created_at >= ?",
            (cutoff_7,),
        ).fetchone()[0]
        c30 = con.execute(
            "SELECT COUNT(*) FROM persona_notes "
            "WHERE source = 'organic' AND created_at >= ?",
            (cutoff_30,),
        ).fetchone()[0]
        con.close()
    except Exception as e:
        return {
            "status": "yellow",
            "reason": f"DB 讀取錯誤：{e}",
            "count_7d": 0,
            "count_30d": 0,
        }

    if c7 > ORGANIC_WEEK_HIGH:
        status = "red"
        reason = f"過去 7 天 {c7} 條糾正 (> {ORGANIC_WEEK_HIGH})，bot 一直被糾正 → model 出問題"
    elif c30 == ORGANIC_30D_ZERO:
        status = "yellow"
        reason = "過去 30 天 0 條糾正 → user 沒在用 / bot 已經很對了"
    elif ORGANIC_WEEK_HEALTHY <= c7 <= 15:
        status = "green"
        reason = f"過去 7 天 {c7} 條糾正，正常學習中"
    else:
        status = "green"
        reason = f"過去 7 天 {c7} 條，過去 30 天 {c30} 條"

    return {
        "status": status,
        "reason": reason,
        "count_7d": c7,
        "count_30d": c30,
    }


# ── render report ────────────────────────────────────────────────────────────

_STATUS_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def _emoji(status: str) -> str:
    return _STATUS_EMOJI.get(status, "⚪")


def render_report(checks: dict) -> tuple[str, bool]:
    """生成 markdown report；回 (text, has_red)。"""
    a = checks["distill"]
    b = checks["launchd"]
    c = checks["lora"]
    d = checks["progress"]
    e = checks["organic"]

    statuses = [a["status"], b["status"], c["status"], d["status"], e["status"]]
    has_red = "red" in statuses
    all_green = all(s == "green" for s in statuses)

    if all_green:
        # 1 行健康摘要
        total = a.get("total_real_pairs", 0)
        days_left = c.get("days_to_target", 0)
        if days_left > 0:
            return (
                f"✅ 訓練 pipeline 健康（{total} / {TRAINING_TARGET_PAIRS} pairs，"
                f"預計 {days_left} 天達標）"
            ), False
        active = c.get("active") or "無"
        return f"✅ 訓練 pipeline 健康（{total} pairs，線上版：{active}）", False

    # 完整 report
    lines = [
        "## LINE Bot 訓練 Pipeline 健康檢查",
        f"_時間：{datetime.now():%Y-%m-%d %H:%M}_",
        "",
        f"{_emoji(a['status'])} **A. Distill 累積**：{a['reason']}",
        f"  - 真實 pair 數：{a.get('total_real_pairs', 0)}"
        f"（distill {a.get('distilled_real', 0)} + sft {a.get('sft_count', 0)} → train {a.get('train_total_count', 0)}）",
        f"  - 過去 7 / 30 天 distill：{a.get('pairs_7d', 0)} / {a.get('pairs_30d', 0)} 對",
        "",
        f"{_emoji(b['status'])} **B. Launchd 健康**：{b['reason']}",
        "",
        f"{_emoji(c['status'])} **C. LoRA 訓練狀態**：{c['reason']}",
    ]
    if c.get("adapters"):
        lines.append(f"  - adapters：{', '.join(c['adapters'])}")
    lines += [
        "",
        f"{_emoji(d['status'])} **D. 進步幅度**：{d['reason']}",
    ]
    if d.get("details"):
        for det in d["details"]:
            lines.append(f"  - {det}")
    lines += [
        "",
        f"{_emoji(e['status'])} **E. Organic correction**：{e['reason']}",
        f"  - 過去 7 / 30 天：{e.get('count_7d', 0)} / {e.get('count_30d', 0)} 條",
    ]
    f = checks.get("auto_iterate", {"status": "yellow", "reason": "未檢查"})
    has_red = has_red or f["status"] == "red"
    lines += [
        "",
        f"{_emoji(f['status'])} **F. Auto-iterate（code 自動修復）**：{f['reason']}",
    ]

    # 2026-05-09 改：全綠不推 Discord，只在有 🔴 異常時才報
    if has_red:
        lines.insert(0, "@here 訓練 / auto-iterate pipeline 異常")
    else:
        # 全綠 → 設個 sentinel 讓 main 跳過推送
        return "", False  # caller 接到空字串視為 skip

    return "\n".join(lines), has_red


# ── Discord push ─────────────────────────────────────────────────────────────

def push_discord(text: str, *, webhook_url: Optional[str] = None) -> bool:
    """推 Discord — 優先用既有 Bot DM (notify_discord.send_dm)，沒設才退 webhook。

    順序：
      1. notify_discord.send_dm — 共用既有 DISCORD_BOT_TOKEN + DISCORD_USER_ID（推私訊）
      2. webhook_url (env DISCORD_WEBHOOK_URL) — 少用，僅當 user 真有 webhook 時
      3. 兩者都沒 → log skip
    """
    # 優先：Bot DM（跟 daily_briefing / notify_discord 共用）
    try:
        sys.path.insert(0, str(LINE_BOT))
        import notify_discord  # type: ignore
        if getattr(notify_discord, "TOKEN", None) and getattr(notify_discord, "USER_ID", None):
            ok = notify_discord.send_dm(text[:1900])
            if ok:
                return True
            logger.warning("notify_discord.send_dm failed, try webhook fallback")
    except ImportError:
        logger.info("notify_discord 不可用，try webhook")
    except Exception as e:
        logger.warning("notify_discord exception: %s", e)

    # Fallback：webhook
    if webhook_url is None:
        webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        logger.info("Discord 推送 skip（沒設 BOT_TOKEN 也沒 WEBHOOK_URL）")
        return False

    try:
        import requests
        r = requests.post(webhook_url, json={"content": text[:1900]}, timeout=10)
        if r.status_code in (200, 204):
            return True
        logger.warning("discord webhook %d: %s", r.status_code, r.text[:200])
        return False
    except Exception as e:
        logger.warning("discord webhook exception: %s", e)
        return False


# ── 主流程 ───────────────────────────────────────────────────────────────────

AUTO_ITERATE_LABEL = "com.andrew.line-bot-auto-iterate"
AUTO_ITERATE_REPORT_GLOB = str(LINE_BOT / "logs" / "auto_iterate_*.md")
AUTO_ITERATE_LOG = LOGS_DIR / "line_bot_auto_iterate_stdout.log"


def check_auto_iterate_health() -> dict:
    """F. auto-iterate code-fix 排程健康。

    - launchctl 是否 loaded + exit code
    - 最近一次 auto_iterate_YYYYMMDD.md 距現在多久（> 36hr = 紅）
    - 報告長度（過短 = 可能異常 / 失敗）
    """
    import glob
    exit_code = _launchctl_get_exit_code(AUTO_ITERATE_LABEL)

    reports = sorted(glob.glob(AUTO_ITERATE_REPORT_GLOB))
    if not reports:
        return {
            "status": "red",
            "reason": "從沒產生 auto_iterate report — auto-iterate 排程從沒 fire",
            "last_report": None,
            "exit_code": exit_code,
        }
    last = Path(reports[-1])
    age_hr = (_now_ts() - last.stat().st_mtime) / 3600

    size_kb = last.stat().st_size / 1024
    body = last.read_text(encoding="utf-8", errors="ignore")
    failed_markers = sum(1 for m in ("⚠️", "失敗", "Failed", "exit=1") if m in body)

    if age_hr > 36:
        status = "red"
        reason = f"上次 auto-iterate 是 {age_hr:.1f} hr 前 (> 36hr)，可能掛了"
    elif exit_code is not None and exit_code != 0:
        status = "yellow"
        reason = f"上次 auto-iterate 跑完但 exit={exit_code}（最後一輪有問題）"
    elif size_kb < 1:
        status = "yellow"
        reason = f"報告太短（{size_kb:.1f} KB），疑似空跑"
    elif failed_markers >= 3:
        status = "yellow"
        reason = f"報告含 {failed_markers} 個失敗標記，需人工 review"
    else:
        status = "green"
        reason = (
            f"上次 auto-iterate {age_hr:.1f} hr 前完成（{size_kb:.1f} KB 報告）"
        )

    return {
        "status": status,
        "reason": reason,
        "last_report": str(last),
        "report_age_hr": age_hr,
        "report_size_kb": size_kb,
        "exit_code": exit_code,
        "failed_markers": failed_markers,
    }


def run_all_checks() -> dict:
    a = check_distill_accumulation()
    b = check_launchd_health()
    c = check_lora_status(total_pairs=a.get("total_real_pairs", 0))
    d = check_progress_delta()
    e = check_organic_corrections()
    f = check_auto_iterate_health()
    return {
        "distill": a,
        "launchd": b,
        "lora": c,
        "progress": d,
        "organic": e,
        "auto_iterate": f,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="不推 Discord，只 print")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    # load .env if exists（讀 DISCORD_WEBHOOK_URL）
    try:
        from dotenv import load_dotenv
        load_dotenv(LINE_BOT / ".env")
    except Exception:
        pass

    checks = run_all_checks()
    text, has_red = render_report(checks)

    if not text:
        # 全綠 → silent skip Discord
        print("[OK] 6 項全綠，Discord 不推送")
        return 0

    print(text)

    if args.dry_run:
        print("\n[DRY-RUN] 不推 Discord")
        return 0

    ok = push_discord(text)
    if not ok:
        print("[WARN] Discord 推送失敗（graceful degrade）", file=sys.stderr)
    return 0  # 不用 exit code 標記異常，讓 launchd 不誤判


if __name__ == "__main__":
    sys.exit(main())
