"""Modal cost tracking — 記錄 GPU 秒數 + 估算費用。

每次 modal_train run 完呼叫 record_run()，會 append 進 modal_runs.json。
若當月累積已超過 80% free credit (default $25)，raise SystemExit 警告。

modal_runs.json 結構：
{
  "monthly_cap_usd": 25.0,
  "warn_threshold_pct": 80,
  "runs": [
    {
      "ts": 1778242477,
      "gpu_seconds": 7200,
      "cost_usd": 2.20,
      "month": "2026-05",
      "metrics": { ... }
    }, ...
  ]
}
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
DEFAULT_LOG = HERE / "modal_runs.json"
DEFAULT_MONTHLY_CAP_USD = 25.0  # $30 free credit 留 $5 buffer
DEFAULT_WARN_THRESHOLD_PCT = 80  # 超過 80% 即提醒退出


def _month_key(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
    return dt.strftime("%Y-%m")


def _load_log(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "monthly_cap_usd": DEFAULT_MONTHLY_CAP_USD,
            "warn_threshold_pct": DEFAULT_WARN_THRESHOLD_PCT,
            "runs": [],
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "monthly_cap_usd": DEFAULT_MONTHLY_CAP_USD,
            "warn_threshold_pct": DEFAULT_WARN_THRESHOLD_PCT,
            "runs": [],
        }


def _save_log(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def month_total_usd(data: dict[str, Any], month: str | None = None) -> float:
    """累計這個月已花多少。"""
    month = month or _month_key()
    return sum(
        float(r.get("cost_usd", 0))
        for r in data.get("runs", [])
        if r.get("month") == month
    )


def record_run(
    gpu_seconds: float,
    cost_usd: float,
    metrics: dict[str, Any] | None = None,
    log_path: Path | None = None,
    enforce_cap: bool = True,
) -> dict[str, Any]:
    """記錄一次 Modal run。

    Args:
        gpu_seconds: 訓練 GPU 秒數（Modal function 自己量到再傳進來）
        cost_usd: 該次估算成本
        metrics: 訓練 metrics (train/eval loss 等)，會 inline 進 log
        log_path: 預設 modal_runs.json
        enforce_cap: 月度超 80% 是否 raise SystemExit

    Returns:
        dict 含 month_total_usd, monthly_cap_usd, used_pct
    """
    log_path = log_path or DEFAULT_LOG
    data = _load_log(log_path)
    cap = float(data.get("monthly_cap_usd", DEFAULT_MONTHLY_CAP_USD))
    threshold_pct = float(data.get("warn_threshold_pct", DEFAULT_WARN_THRESHOLD_PCT))

    ts = time.time()
    month = _month_key(ts)
    entry = {
        "ts": int(ts),
        "month": month,
        "gpu_seconds": float(gpu_seconds),
        "cost_usd": float(cost_usd),
        "metrics": metrics or {},
    }
    data.setdefault("runs", []).append(entry)
    _save_log(log_path, data)

    total = month_total_usd(data, month)
    used_pct = (total / cap) * 100.0 if cap > 0 else 0.0
    summary = {
        "month": month,
        "month_total_usd": total,
        "monthly_cap_usd": cap,
        "used_pct": used_pct,
        "this_run_usd": float(cost_usd),
        "this_run_gpu_seconds": float(gpu_seconds),
    }

    if used_pct >= threshold_pct:
        msg = (
            f"[STOP] {month} Modal 已用 ${total:.2f} / ${cap:.2f} "
            f"({used_pct:.0f}% 超過 {threshold_pct:.0f}% 警戒)。"
            f" 暫停下次訓練，等下個月或調整 monthly_cap_usd。"
        )
        print(msg)
        if enforce_cap:
            raise SystemExit(msg)
    else:
        print(
            f"[COST] {month} 累積 ${total:.2f} / ${cap:.2f} "
            f"({used_pct:.0f}%)，這次 ${cost_usd:.3f}"
        )
    return summary


def main() -> int:
    """CLI: 印目前月累計。"""
    data = _load_log(DEFAULT_LOG)
    cap = float(data.get("monthly_cap_usd", DEFAULT_MONTHLY_CAP_USD))
    month = _month_key()
    total = month_total_usd(data, month)
    used_pct = (total / cap) * 100.0 if cap > 0 else 0.0
    print(f"=== Modal cost tracking ===")
    print(f"month: {month}")
    print(f"runs: {sum(1 for r in data.get('runs', []) if r.get('month') == month)}")
    print(f"total: ${total:.2f} / ${cap:.2f} ({used_pct:.0f}%)")
    print(f"file: {DEFAULT_LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
