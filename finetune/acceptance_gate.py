"""LoRA fine-tune acceptance gate — 訓練後上線把關。

讀 baseline.json + adapter.json（eval_harness 輸出），跑 4 條硬性 gate：
  G1. 違規率 ≤ baseline × 0.5（減半以上）
  G2. 中文比率 ≥ 95%（adapter）
  G3. 規則 0 命中率 ≥ 80%（adapter）
  G4. 任務分 ≥ baseline + 0.5（且絕對值 ≥ 7.0）

任一 fail → exit 1，**不**動 local_llm_config.py。
全過 → 把 adapter path 寫進 local_llm_config.py 的 ACTIVE_ADAPTER。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).parent
LINE_BOT = HERE.parent
sys.path.insert(0, str(LINE_BOT))

CONFIG_PATH = LINE_BOT / "local_llm_config.py"

# Gate thresholds
G1_VIOLATION_HALVE = 0.5  # adapter ≤ baseline × 0.5
G2_CHINESE_MIN = 0.95
G3_RULE0_MIN = 0.80
G4_JUDGE_DELTA = 0.5
G4_JUDGE_ABS_MIN = 7.0

logger = logging.getLogger("acceptance_gate")


def evaluate_gates(baseline_metrics: dict, adapter_metrics: dict) -> dict:
    """跑 4 條 gate，回 {gate_id: {pass, value, threshold, reason}}.

    若 baseline 違規率 = 0：要求 adapter 也要 0（避免除以 0 + 比較失真）。
    若 judge_avg_score 為 None（quota skip）→ G4 視為「無法判定 → fail」。
    """
    results: dict[str, dict] = {}

    # G1：違規率減半
    base_v = baseline_metrics.get("violation_rate", 0.0)
    adp_v = adapter_metrics.get("violation_rate", 0.0)
    if base_v == 0.0:
        g1_pass = adp_v == 0.0
        g1_threshold_text = "baseline=0 → adapter must = 0"
    else:
        threshold = base_v * G1_VIOLATION_HALVE
        g1_pass = adp_v <= threshold
        g1_threshold_text = f"adapter ≤ {threshold:.4f} (baseline {base_v:.4f} × 0.5)"
    results["G1_violation"] = {
        "pass": g1_pass,
        "value": adp_v,
        "baseline": base_v,
        "threshold": g1_threshold_text,
        "reason": "violation rate halve check",
    }

    # G2：中文比率
    adp_cn = adapter_metrics.get("chinese_pass_rate", 0.0)
    g2_pass = adp_cn >= G2_CHINESE_MIN
    results["G2_chinese"] = {
        "pass": g2_pass,
        "value": adp_cn,
        "threshold": f"≥ {G2_CHINESE_MIN}",
        "reason": "chinese ratio check",
    }

    # G3：規則 0 first-sentence-take 命中率
    adp_r0 = adapter_metrics.get("rule0_pass_rate", 0.0)
    g3_pass = adp_r0 >= G3_RULE0_MIN
    results["G3_rule0"] = {
        "pass": g3_pass,
        "value": adp_r0,
        "threshold": f"≥ {G3_RULE0_MIN}",
        "reason": "rule 0 first-sentence-take hit rate",
    }

    # G4：任務分 judge
    base_j = baseline_metrics.get("judge_avg_score")
    adp_j = adapter_metrics.get("judge_avg_score")
    if adp_j is None:
        g4_pass = False
        g4_reason = "adapter judge avg = None (quota skip 或全失敗)"
    elif base_j is None:
        g4_pass = adp_j >= G4_JUDGE_ABS_MIN
        g4_reason = f"baseline judge None；單看 adapter 絕對值 {adp_j:.2f} ≥ {G4_JUDGE_ABS_MIN}?"
    else:
        delta = adp_j - base_j
        g4_pass = (delta >= G4_JUDGE_DELTA) and (adp_j >= G4_JUDGE_ABS_MIN)
        g4_reason = (
            f"delta={delta:.2f} (≥ {G4_JUDGE_DELTA}) AND adapter={adp_j:.2f} (≥ {G4_JUDGE_ABS_MIN})"
        )
    results["G4_judge"] = {
        "pass": g4_pass,
        "value": adp_j,
        "baseline": base_j,
        "threshold": f"adapter ≥ baseline + {G4_JUDGE_DELTA} AND ≥ {G4_JUDGE_ABS_MIN}",
        "reason": g4_reason,
    }

    return results


def all_pass(gate_results: dict) -> bool:
    return all(g.get("pass", False) for g in gate_results.values())


# ── update local_llm_config.py ─────────────────────────────────────────────


def update_active_adapter(adapter_path: str, config_path: Optional[Path] = None) -> None:
    """寫 / 更新 ACTIVE_ADAPTER = "<adapter_path>" 到 config_path。

    若 ACTIVE_ADAPTER 已存在 → 替換。
    若不存在 → 在檔尾 append。
    config_path=None 時，動態讀模組層 CONFIG_PATH（讓 monkeypatch 可以生效）。
    """
    if config_path is None:
        config_path = CONFIG_PATH
    if not config_path.exists():
        # 直接寫一個最小檔（不該發生，但保險）
        config_path.write_text(
            f'ACTIVE_ADAPTER = "{adapter_path}"\n',
            encoding="utf-8",
        )
        return
    text = config_path.read_text(encoding="utf-8")
    pattern = re.compile(r"^ACTIVE_ADAPTER\s*=.*$", re.MULTILINE)
    new_line = f'ACTIVE_ADAPTER = "{adapter_path}"'
    if pattern.search(text):
        text = pattern.sub(new_line, text)
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += "\n# 由 acceptance_gate.py 自動寫入（若不想啟用 LoRA → 設成 None）\n"
        text += new_line + "\n"
    config_path.write_text(text, encoding="utf-8")


def clear_active_adapter(config_path: Path = CONFIG_PATH) -> None:
    """把 ACTIVE_ADAPTER 設為 None（不刪行，方便手動 toggle）。"""
    if not config_path.exists():
        return
    text = config_path.read_text(encoding="utf-8")
    pattern = re.compile(r"^ACTIVE_ADAPTER\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub("ACTIVE_ADAPTER = None", text)
        config_path.write_text(text, encoding="utf-8")


# ── CLI ────────────────────────────────────────────────────────────────────


def _load_metrics(path: Path) -> dict:
    obj = json.loads(path.read_text(encoding="utf-8"))
    # eval_harness 寫的是 {"metrics": {...}, "samples": [...]}
    if "metrics" in obj:
        return obj["metrics"]
    return obj


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--adapter-result", required=True, help="adapter eval JSON")
    parser.add_argument("--adapter-path", required=True, help="實際 adapter 目錄（要寫進 config）")
    parser.add_argument("--dry-run", action="store_true", help="不寫 config，只報告")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    base = _load_metrics(Path(args.baseline))
    adp = _load_metrics(Path(args.adapter_result))

    print("=" * 70)
    print("ACCEPTANCE GATE")
    print("=" * 70)
    print("[BASELINE]", json.dumps(base, ensure_ascii=False, indent=2))
    print("[ADAPTER ]", json.dumps(adp, ensure_ascii=False, indent=2))

    gates = evaluate_gates(base, adp)
    print("\n[GATES]")
    for gid, info in gates.items():
        flag = "PASS" if info["pass"] else "FAIL"
        print(f"  {gid}: {flag} | value={info['value']} | {info.get('reason', '')}")
        print(f"           threshold: {info['threshold']}")

    if all_pass(gates):
        print("\n[OK] 4/4 gate 通過")
        if args.dry_run:
            print("[DRY-RUN] 不更新 local_llm_config.py")
        else:
            update_active_adapter(args.adapter_path)
            print(f"[CONFIG] 已寫 ACTIVE_ADAPTER = {args.adapter_path!r} → {CONFIG_PATH}")
        return 0
    else:
        n_fail = sum(1 for g in gates.values() if not g["pass"])
        print(f"\n[FAIL] {n_fail}/4 gate 失敗，**拒絕** 上線")
        return 1


if __name__ == "__main__":
    sys.exit(main())
