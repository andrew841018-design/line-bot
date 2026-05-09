"""進度監控 — 累積 fine-tune pairs 距離 3000 還要幾天。

讀 finetune/data/distilled.jsonl，依 metadata.distilled_at 計算最近 7 天速率，
線性外推到 3000 對的預估天數。

用法：
    .venv/bin/python finetune/check_progress.py
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DISTILLED = DATA_DIR / "distilled.jsonl"
SFT = DATA_DIR / "sft.jsonl"
TARGET = 3000


def _load_pairs() -> list[dict]:
    if not DISTILLED.exists():
        return []
    out = []
    for line in DISTILLED.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def main() -> None:
    pairs = _load_pairs()
    total = len(pairs)
    sft_total = sum(1 for _ in SFT.open()) if SFT.exists() else 0
    combined = total + sft_total

    print(f"=== Fine-tune 累積進度 ===")
    print(f"distilled.jsonl: {total} pairs")
    print(f"sft.jsonl (extract from history): {sft_total} pairs")
    print(f"combined: {combined} pairs")
    print(f"target:    {TARGET} pairs")
    print(f"remaining: {max(TARGET - combined, 0)} pairs")
    print()

    # 最近 7 天速率
    now = time.time()
    cutoff = now - 7 * 86400
    real_pairs = [p for p in pairs if not p.get("metadata", {}).get("mock", False)]
    recent = [
        p for p in real_pairs
        if p.get("metadata", {}).get("distilled_at", 0) >= cutoff
    ]
    rate_per_day = len(recent) / 7.0 if recent else 0.0

    # 若沒 real data，用 mock rate 當下限估計（提醒這是 mock）
    if rate_per_day == 0.0:
        all_recent = [
            p for p in pairs
            if p.get("metadata", {}).get("distilled_at", 0) >= cutoff
        ]
        if all_recent:
            mock_rate = len(all_recent) / 7.0
            print(f"近 7 天 real-mode 速率: 0 pairs/day（無真資料）")
            print(f"近 7 天 mock 含速率: {mock_rate:.1f} pairs/day（僅參考）")
        else:
            print(f"近 7 天速率: 0 pairs/day（尚未有資料）")
        rate_per_day = 10.0  # 假設 daily distill 啟動後每天 10 pairs（DAILY_LIMIT）
        print(f"假設啟用 daily distill 後速率: {rate_per_day:.0f} pairs/day（DAILY_LIMIT）")
    else:
        print(f"近 7 天速率: {rate_per_day:.1f} pairs/day（real Gemini）")

    print()

    if combined >= TARGET:
        print(f"已達門檻！可開跑 LoRA: python finetune/train_lora.py")
        return

    remaining = TARGET - combined
    days_to_target = remaining / rate_per_day if rate_per_day > 0 else float("inf")
    eta = datetime.now() + timedelta(days=days_to_target)
    print(f"預估達標: {days_to_target:.0f} 天後（{eta.strftime('%Y-%m-%d')}）")
    print(f"半年累積估算 (180d × {rate_per_day:.0f}): {180 * rate_per_day:.0f} pairs")
    print(f"一年累積估算 (365d × {rate_per_day:.0f}): {365 * rate_per_day:.0f} pairs")


if __name__ == "__main__":
    main()
