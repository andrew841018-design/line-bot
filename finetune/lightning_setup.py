"""Lightning AI Studios setup helper — 印 step-by-step 上手流程。

Lightning AI 沒有 Modal-style Python SDK 從本機跑 remote function，
所以這支不打 API、只印指引。如果未來 lightning CLI 出 sync 工具會在這裡接。

執行：
    python finetune/lightning_setup.py

額外 flags:
    --check-data   只跑 distilled.jsonl 健檢、不印 setup 流程
    --print-cmd    額外印 Studio 內要跑的單行 command
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
DISTILLED = DATA_DIR / "distilled.jsonl"


SETUP_STEPS = """
================================================================
Lightning AI Studios — LoRA fine-tune 上手流程
================================================================

1. 註冊
   → https://lightning.ai/sign-up
   免費送 22 GPU hr / 月（A10G / L4 / T4 等 24GB-class）；信用卡不需要

2. Create Studio
   → 登入後 → New Studio
   → Template: 選「Code (Python)」或「PyTorch」
   → Machine: 點上排「GPU」→ 選 L4 (24GB) 或 A10G (24GB)
     - L4 免費 22hr/月（足夠 14B LoRA 跑 1 次完整 fine-tune）
     - A10G 跟 L4 同 tier，速度快約 1.3x

3. 把資料 / 程式弄進 Studio（三選一）

   方案 A：直接拖檔（簡單）
     → Studio 左側檔案 panel 拖 distilled.jsonl 進根目錄
     → 拖 lightning_train.py 進根目錄
     → 完工

   方案 B：git clone（建議，有 history）
     在 Studio terminal 跑：
       git clone <你的 line_bot repo URL>
       cd line_bot
       # distilled.jsonl 如果太大沒進 git，另外拖進 finetune/data/

   方案 C：Lightning CLI（如果 lightning 有 sync 命令；目前沒官方）
     → 略，等未來

4. 安裝 deps（Studio terminal）
     pip install -q transformers peft accelerate datasets bitsandbytes safetensors sentencepiece

5. 跑訓練
     python finetune/lightning_train.py
   或（方案 A 平鋪檔的情況）：
     python lightning_train.py

   預期：14B 4-bit LoRA / 1000 pairs / 3 epoch / L4 ≈ 4-6hr
        （bs=2, grad_accum=8, max_len=2048）

   可關瀏覽器，Studio 背景跑（Lightning 不會像 Colab 因為 idle 殺 kernel）

6. 下載 adapter
   訓練完 ./lora_out/ 會有 adapter_config.json + adapter_model.safetensors + tokenizer 檔案
   也會 zip 成 ./lora_out.zip
   → Studio 右側檔案 panel 點 lora_out.zip → Download
   → 本機放到 finetune/adapters_lightning/，解壓

7. 跑 acceptance gate（本機）
     python finetune/acceptance_gate.py \\
         --adapter finetune/adapters_lightning/<unzipped> \\
         --baseline finetune/eval_results/baseline.json
   過 gate → 自動寫 ACTIVE_ADAPTER 進 local_llm_config.py（跟 Modal/Kaggle 流程一致）

8. 注意事項
   - 22hr/月 用完當月不能再開 GPU（但 CPU studio 還能用）
   - Studio idle > 30min 會自動暫停（保留檔案、不算 hr）
   - 如果用 14B 而不是 3B，記得 BASE_MODEL 對齊本機 inference（main.py
     pipeline 也得能載 14B，否則 adapter 對不上）

================================================================
"""


SHORT_CMD = """
# 在 Lightning AI Studio terminal 一行帶過：
pip install -q transformers peft accelerate datasets bitsandbytes safetensors sentencepiece && \\
python finetune/lightning_train.py
"""


def _check_data() -> int:
    if not DISTILLED.exists():
        print(f"[WARN] {DISTILLED} 不存在 — 先跑 distill_daily.py 累積資料才有東西可上傳")
        return 1
    n_total = 0
    n_real = 0
    with DISTILLED.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n_total += 1
            try:
                obj = json.loads(line)
                if not obj.get("metadata", {}).get("mock", False):
                    n_real += 1
            except Exception:
                continue
    size_mb = DISTILLED.stat().st_size / (1024 * 1024)
    print(f"[OK] distilled.jsonl: {n_total} total, {n_real} non-mock, {size_mb:.2f} MB")
    if n_real < 100:
        print(f"[WARN] non-mock < 100 筆，到 Lightning AI 跑訓練效果有限")
    elif n_real < 1000:
        print(f"[INFO] non-mock {n_real} 筆，可以跑但建議累積到 1000+")
    else:
        print(f"[OK] {n_real} 筆夠跑 14B LoRA")
    return 0


def _check_lightning_cli() -> bool:
    """檢查本機是否有 lightning CLI；目前不依賴它，純資訊用。"""
    return shutil.which("lightning") is not None


def main() -> int:
    parser = argparse.ArgumentParser(description="Lightning AI Studios 上手指引")
    parser.add_argument("--check-data", action="store_true", help="只查 distilled.jsonl 狀態")
    parser.add_argument("--print-cmd", action="store_true", help="另印 Studio 內單行 command")
    args = parser.parse_args()

    if args.check_data:
        return _check_data()

    print(SETUP_STEPS)

    # 順便檢查資料
    print("─── 本機資料健檢 ─────────────────────────")
    _check_data()

    # 順便看 lightning CLI
    has_cli = _check_lightning_cli()
    print()
    print("─── Lightning CLI 偵測 ───────────────────")
    if has_cli:
        print("[OK] 找到 `lightning` CLI；目前流程不依賴它，但未來 sync 命令會用")
    else:
        print("[INFO] 沒裝 `lightning` CLI（不影響流程，本機不需要）")
        print("       想裝：pip install lightning  （只裝 SDK，不上傳真 token）")

    if args.print_cmd:
        print()
        print("─── Studio 內單行指令 ─────────────────────")
        print(SHORT_CMD)

    return 0


if __name__ == "__main__":
    sys.exit(main())
