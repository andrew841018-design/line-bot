"""LoRA fine-tune script — 本機 mlx-lm 或 Modal 雲端。

**累積到 3000 pairs 才執行（strict 模式）**：
    .venv/bin/python finetune/train_lora.py                       # 本機 mlx-lm (~12hr)
    .venv/bin/python finetune/train_lora.py --cloud               # Modal A10G (~2hr, ~$2/run)
    .venv/bin/python finetune/train_lora.py --cloud lightning     # Lightning AI L4/A10G

**Pilot 模式（80 ~ 3000 pairs，小規模試訓 / 快速驗證 organic 訊號）**：
    .venv/bin/python finetune/train_lora.py --pilot               # 本機（mlx 不一定支援）
    .venv/bin/python finetune/train_lora.py --pilot --cloud lightning

Pilot vs Strict 差異：
    | 項目          | strict (預設) | pilot              |
    | min pairs     | 3000         | 80                 |
    | iters / epoch | 3 epochs     | 1 epoch            |
    | grad_accum    | 16           | 2                  |
    | 用途          | 正式上線      | 條件觸發試訓 / 探溫 |

訓練後仍跑 `eval_harness.py` + `acceptance_gate.py`；4-gate 全過才會寫
`ACTIVE_ADAPTER`。

預期 wall-clock：M2 Pro 16GB / 3000 pairs / 3 epochs ≈ 8–12 hr。建議連電源 + 關閉
Spotlight indexing in finetune/。
雲端 Modal A10G（peft + transformers）~2 hr / ~$2 USD per run；$30/月 free credit
夠跑 ~10 次 reset。實際呼 `finetune/modal_train.py`（首次需 `modal token new`）。

依賴（首次安裝）：
    pip install mlx-lm        # 本機
    pip install modal         # 雲端；首次需 `modal token new` browser auth

輸入：
    finetune/data/distilled.jsonl  + finetune/data/sft.jsonl（合併後）

輸出：
    finetune/adapters/  — LoRA adapter weights (safetensors)

設定來源：
    finetune/lora_config.yaml  → mac_mlx 區段
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
DISTILLED = DATA_DIR / "distilled.jsonl"
SFT = DATA_DIR / "sft.jsonl"
ADAPTER_DIR = HERE / "adapters"
COMBINED = DATA_DIR / "_combined_train.jsonl"

MIN_PAIRS = 3000
PILOT_MIN_PAIRS = 80      # pilot 模式門檻（小規模試訓）
PILOT_MAX_PAIRS = 3000    # 上 strict 線之前都算 pilot 範圍
# 14B base — 32GB Mac 4-bit quantize OK，但 LoRA 訓練吃記憶體要再壓 batch
MODEL = "mlx-community/Qwen2.5-14B-Instruct-4bit"
LORA_RANK = 8        # 14B 比 3B 大 5x，rank 從 16 降到 8 控 adapter 大小 + 記憶體
LORA_ALPHA = 16      # 維持 alpha = 2 * rank
LR = 2e-4
ITERS = 600
BATCH_SIZE = 1       # 14B 4-bit on 32GB → batch 必降到 1
GRAD_ACCUM = 16      # effective batch = 16，跟 3B (bs=4, ga=4) 對齊
NUM_LAYERS = 16      # mlx-lm 用此值控 LoRA 套幾層 attention
SEED = 42

# Pilot 模式 hyperparams（速度優先，避免小資料集 over-fit）
PILOT_ITERS = 200       # ~ 1 epoch 對 80-500 pairs 來說已夠
PILOT_GRAD_ACCUM = 2    # 從 16 降 2 → effective batch 也降，但 step 多訊號多
PILOT_NUM_LAYERS = 16

EVAL_RESULTS_DIR = HERE / "eval_results"


def _check_mlx() -> bool:
    try:
        subprocess.run(
            ["python", "-c", "import mlx_lm"],
            check=True,
            capture_output=True,
        )
        return True
    except Exception:
        return False


def _combine_data(force: bool = False) -> int:
    """把 distilled.jsonl + sft.jsonl 合併成 _combined_train.jsonl，丟掉 mock pair。"""
    pairs: list[dict] = []
    seen_keys: set[str] = set()

    for path in (DISTILLED, SFT):
        if not path.exists():
            continue
        for line in path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            # 丟掉 mock
            if obj.get("metadata", {}).get("mock", False) and not force:
                continue
            # dedupe by user content
            user_text = ""
            for m in obj.get("messages", []):
                if m.get("role") == "user":
                    user_text = m.get("content", "")
                    break
            if user_text in seen_keys:
                continue
            seen_keys.add(user_text)
            pairs.append(obj)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with COMBINED.open("w", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    return len(pairs)


def _run_eval_and_gate(adapter_dir: Path, *, skip_judge: bool = False) -> int:
    """訓練完後 chain：eval baseline + adapter → acceptance_gate。

    回傳：
      0  → 全 4 條 gate 過，已自動寫 ACTIVE_ADAPTER
      非 0 → 任一 gate fail，**不**動 config，印失敗 metrics
    """
    EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    baseline_out = EVAL_RESULTS_DIR / "baseline.json"
    adapter_out = EVAL_RESULTS_DIR / "adapter.json"

    eval_script = HERE / "eval_harness.py"
    gate_script = HERE / "acceptance_gate.py"
    py = sys.executable

    # baseline（純 base model）
    print("[CHAIN] running baseline eval ...")
    rc = subprocess.call([
        py, str(eval_script),
        "--model", MODEL,
        "--label", "baseline",
        "--out", str(baseline_out),
    ] + (["--skip-judge"] if skip_judge else []))
    if rc != 0:
        print(f"[ERR] baseline eval exit={rc}")
        return rc

    # adapter
    print("[CHAIN] running adapter eval ...")
    rc = subprocess.call([
        py, str(eval_script),
        "--model", MODEL,
        "--adapter", str(adapter_dir),
        "--label", "adapter",
        "--out", str(adapter_out),
    ] + (["--skip-judge"] if skip_judge else []))
    if rc != 0:
        print(f"[ERR] adapter eval exit={rc}")
        return rc

    # gate
    print("[CHAIN] running acceptance_gate ...")
    rc = subprocess.call([
        py, str(gate_script),
        "--baseline", str(baseline_out),
        "--adapter-result", str(adapter_out),
        "--adapter-path", str(adapter_dir),
    ])
    if rc == 0:
        print("[CHAIN] OK — adapter 通過 gate，已自動更新 ACTIVE_ADAPTER")
    else:
        print("[CHAIN] FAIL — gate 拒絕，**不**更新 ACTIVE_ADAPTER")
    return rc


def _run_modal(args: argparse.Namespace) -> int:
    """委託 Modal 雲端訓練（透過 modal CLI subprocess，避免直接 import modal_train 觸發 stub init）。

    需要：
      - .venv/bin/modal 已安裝
      - 已 `modal token new` 認證
      - finetune/data/distilled.jsonl 存在
    """
    modal_bin = Path(sys.executable).parent / "modal"
    modal_script = HERE / "modal_train.py"
    if not modal_bin.exists():
        print("[ERR] modal CLI 找不到，先跑：.venv/bin/pip install modal")
        return 2
    if not modal_script.exists():
        print(f"[ERR] {modal_script} 不存在")
        return 2
    cmd = [str(modal_bin), "run", str(modal_script)]
    if args.force:
        cmd.append("--force")
    if args.skip_eval:
        cmd.append("--skip-eval")
    if getattr(args, "pilot", False):
        cmd.append("--pilot")
    print(f"[CLOUD] {' '.join(cmd)}")
    print(f"[CLOUD] 首次需先在 terminal 跑：modal token new（browser auth）")
    return subprocess.call(cmd)


def _run_lightning(args: argparse.Namespace) -> int:
    """委託 Lightning AI Studios 訓練（subprocess 呼 lightning_train.py）。

    Lightning AI Studios 通常是 24GB GPU instance（L4/A10G），免費 22hr/月。
    這支 script 通常在 Lightning Studio 內跑，但本機呼也支援（remote SSH 模式
    需自己處理）。Pilot 模式會把 epochs / grad_accum 降低。
    """
    lightning_script = HERE / "lightning_train.py"
    if not lightning_script.exists():
        print(f"[ERR] {lightning_script} 不存在")
        return 2
    py = sys.executable
    cmd = [py, str(lightning_script)]
    env = dict(os.environ)
    if getattr(args, "pilot", False):
        env["LIGHTNING_PILOT"] = "1"
        env["LIGHTNING_EPOCHS"] = "1"
        env["LIGHTNING_GRAD_ACCUM"] = str(PILOT_GRAD_ACCUM)
        env["LIGHTNING_MIN_PAIRS"] = str(PILOT_MIN_PAIRS)
    if args.skip_eval:
        env["LIGHTNING_SKIP_EVAL"] = "1"
    print(f"[CLOUD-LIGHTNING] {' '.join(cmd)} (pilot={getattr(args, 'pilot', False)})")
    return subprocess.call(cmd, env=env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="忽略 < 3000 門檻")
    parser.add_argument("--allow-mock", action="store_true", help="把 mock pair 也納入")
    parser.add_argument(
        "--cloud",
        nargs="?",
        const="modal",
        default=None,
        choices=["modal", "lightning"],
        help="改走雲端訓練（modal=Modal A10G ~2hr/~$2; lightning=Lightning AI L4/A10G）",
    )
    parser.add_argument(
        "--pilot",
        action="store_true",
        help=(
            "Pilot 模式：允許 80–3000 pairs 跑、epochs 降 1、grad_accum 降 2 "
            "（速度快，避免 over-fit）。預設仍是 strict 3000 拒跑。"
        ),
    )
    parser.add_argument(
        "--skip-eval",
        action="store_true",
        help="只訓練不跑 eval / gate（debug 用）",
    )
    parser.add_argument(
        "--skip-judge",
        action="store_true",
        help="eval 階段跳過 LLM judge（省 Gemini quota）",
    )
    args = parser.parse_args(argv)

    if args.cloud == "modal":
        return _run_modal(args)
    if args.cloud == "lightning":
        return _run_lightning(args)

    n = _combine_data(force=args.allow_mock)
    print(f"[INFO] 合併後可用 pairs: {n}（去掉 mock + dedup user content）")

    # Pilot 模式：80 ~ 3000 區間放行；其他預設 strict 3000 門檻
    if args.pilot:
        if n < PILOT_MIN_PAIRS and not args.force:
            print(
                f"[ERR] pilot 模式 < {PILOT_MIN_PAIRS} pairs，太少不訓。"
                f" 加 --force 強制跑。"
            )
            return 1
        if n >= MIN_PAIRS:
            print(
                f"[INFO] pairs={n} 已達 strict 3000 門檻，但 --pilot 仍生效"
                f"（小 epochs / grad_accum）。"
            )
        else:
            print(
                f"[INFO] pilot 模式：{n} pairs（{PILOT_MIN_PAIRS}-{MIN_PAIRS} 區間），"
                f"用 PILOT 超參跑試訓。"
            )
    else:
        if n < MIN_PAIRS and not args.force:
            print(
                f"[ERR] < {MIN_PAIRS} pairs，fine-tune 預期效果差。"
                f"加 --force 強制跑，或加 --pilot 跑試訓模式（≥ {PILOT_MIN_PAIRS} 即可）。"
            )
            return 1

    if not _check_mlx():
        print("[ERR] 沒裝 mlx-lm，先跑：pip install mlx-lm")
        return 2

    ADAPTER_DIR.mkdir(parents=True, exist_ok=True)

    # mlx_lm.lora 吃 data folder（含 train.jsonl / valid.jsonl），這裡準備一下
    work = DATA_DIR / "_mlx_run"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    # 90/10 split
    pairs = [json.loads(l) for l in COMBINED.open(encoding="utf-8") if l.strip()]
    cut = max(1, int(len(pairs) * 0.9))
    with (work / "train.jsonl").open("w", encoding="utf-8") as f:
        for p in pairs[:cut]:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    with (work / "valid.jsonl").open("w", encoding="utf-8") as f:
        for p in pairs[cut:]:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    iters = PILOT_ITERS if args.pilot else ITERS
    num_layers = PILOT_NUM_LAYERS if args.pilot else NUM_LAYERS
    eff_grad_accum = PILOT_GRAD_ACCUM if args.pilot else GRAD_ACCUM

    cmd = [
        "python", "-m", "mlx_lm.lora",
        "--model", MODEL,
        "--train",
        "--data", str(work),
        "--iters", str(iters),
        "--batch-size", str(BATCH_SIZE),
        "--num-layers", str(num_layers),
        "--learning-rate", str(LR),
        "--val-batches", "25",
        "--steps-per-eval", "100",
        "--steps-per-report", "10",
        "--adapter-path", str(ADAPTER_DIR),
        "--seed", str(SEED),
        "--grad-checkpoint",   # 14B 4-bit on 32GB Mac 必開（省記憶體換 ~30% 速度）
    ]
    mode = "PILOT" if args.pilot else "STRICT"
    print(f"[RUN][{mode}]", " ".join(cmd))
    print(
        f"[NOTE] base={MODEL} (14B), batch={BATCH_SIZE} "
        f"(effective {BATCH_SIZE * eff_grad_accum} via grad-accum), "
        f"lora_rank={LORA_RANK}, mode={mode}"
    )
    rc = subprocess.call(cmd)
    if rc == 0:
        print(f"[OK] adapter saved: {ADAPTER_DIR}")
    else:
        print(f"[ERR] mlx_lm.lora exit code {rc}")
        return rc

    # 訓練成功 → chain eval + acceptance gate
    if args.skip_eval:
        print("[SKIP] --skip-eval；不跑 eval / gate；要上線請手動：")
        print(f"       .venv/bin/python {HERE / 'eval_harness.py'} --model {MODEL} --label baseline")
        print(f"       .venv/bin/python {HERE / 'eval_harness.py'} --model {MODEL} --adapter {ADAPTER_DIR} --label adapter")
        print(f"       .venv/bin/python {HERE / 'acceptance_gate.py'} --baseline finetune/eval_results/baseline.json --adapter-result finetune/eval_results/adapter.json --adapter-path {ADAPTER_DIR}")
        return 0
    return _run_eval_and_gate(ADAPTER_DIR, skip_judge=args.skip_judge)


if __name__ == "__main__":
    sys.exit(main())
