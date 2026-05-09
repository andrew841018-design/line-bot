"""Smoke test for eval_harness — 5 fake test pairs + mock judge.

不真載入 model，只走 metric + gate 邏輯，驗證流程可跑、metrics dict 完整。

跑：
    .venv/bin/python finetune/_smoke_eval.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from finetune import eval_harness, acceptance_gate

# 5 筆假 test pair（含 user / gold；assistant 模擬 baseline 各種狀況）
TEST_SET = [
    {"user": "今天股市怎樣？", "gold": "我覺得今天偏震盪。"},
    {"user": "AAPL 分析一下", "gold": "我看是 175 美元附近盤整。"},
    {"user": "媽祖遶境路線？", "gold": "我這邊查到主要走中部沿海。"},
    {"user": "幫我推薦書", "gold": "我覺得《思考快與慢》很值得看。"},
    {"user": "這張圖在說什麼？", "gold": "圖中是 K 線圖，呈現下降趨勢。"},
]


# 模擬 baseline：違規多、第一句常被黑名單命中
def baseline_gen(user: str) -> str:
    if "今天股市" in user:
        return "我覺得今天股市偏震盪，建議觀望。"  # OK
    if "AAPL" in user:
        return "咪寶看到您問 AAPL 的事。"  # 黑名單 echo opener → 違規
    if "媽祖" in user:
        return "這個值得我們深思。"  # 空附和 → 違規
    if "推薦書" in user:
        return "我覺得《思考快與慢》是好書。"  # OK
    if "這張圖" in user:
        return "這張圖片展示了一個圖表。"  # 黑名單 → 違規 + rule0 fail
    return "ok"


# 模擬 adapter：違規少很多
def adapter_gen(user: str) -> str:
    if "今天股市" in user:
        return "我覺得今天偏震盪，建議觀望。"
    if "AAPL" in user:
        return "我看是 175 美元盤整為主。"
    if "媽祖" in user:
        return "我這邊查到主要走中部沿海一段。"
    if "推薦書" in user:
        return "我覺得《思考快與慢》是好書。"
    if "這張圖" in user:
        return "我看是 K 線下降趨勢，注意支撐位。"
    return "ok"


def mock_judge(user, gold, prediction):
    # baseline 的差不多 5 分；adapter 給 8 分
    if "咪寶看到" in prediction or "值得我們深思" in prediction or prediction.startswith("這張圖片展示"):
        return {"score": 4, "reason": "baseline 違規"}
    if prediction.startswith(("我覺得", "我看是", "我這邊")):
        return {"score": 8, "reason": "adapter 通順"}
    return {"score": 6, "reason": "mid"}


def main() -> int:
    print("=" * 70)
    print("SMOKE TEST — eval_harness + acceptance_gate (no real model)")
    print("=" * 70)

    print("\n[1] BASELINE eval (mock LLM):")
    base = eval_harness.evaluate(
        TEST_SET, generate_fn=baseline_gen, judge_fn=mock_judge, label="baseline"
    )
    print(json.dumps(base["metrics"], ensure_ascii=False, indent=2))

    print("\n[2] ADAPTER eval (mock LLM):")
    adp = eval_harness.evaluate(
        TEST_SET, generate_fn=adapter_gen, judge_fn=mock_judge, label="adapter"
    )
    print(json.dumps(adp["metrics"], ensure_ascii=False, indent=2))

    print("\n[3] ACCEPTANCE GATE:")
    gates = acceptance_gate.evaluate_gates(base["metrics"], adp["metrics"])
    for gid, info in gates.items():
        flag = "PASS" if info["pass"] else "FAIL"
        print(f"  {gid}: {flag} | value={info['value']} | {info.get('reason', '')}")
        print(f"           threshold: {info['threshold']}")
    overall = "PASS" if acceptance_gate.all_pass(gates) else "FAIL"
    print(f"\n[OVERALL] {overall}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
