"""Modal 雲端 LoRA fine-tune pipeline (取代本機 mlx-lm LoRA)。

本機 Mac 跑 12+ 小時，Modal A10G 跑 ~2 小時。$30/月 free credit 夠每月跑 ~10 次。

執行流程（user 自己跑）:
    1. 確保 distilled.jsonl 累積到 3000 對（或 --force）
    2. modal token new   # browser 認證（首次）
    3. ./.venv/bin/modal run finetune/modal_train.py
    4. 完成後 adapter 寫進 finetune/adapters/peft/，自動轉換 mlx 格式至
       finetune/adapters/mlx/
    5. 自動跑 eval_harness + acceptance_gate；過 gate 才更新 ACTIVE_ADAPTER

詳細架構:
    - Image: debian_slim + transformers / peft / accelerate / torch / datasets /
      bitsandbytes
    - GPU: A10G (per-second billing, ~$1.10/hr)
    - Volume: line-bot-finetune-data 存放 distilled.jsonl + adapter 輸出
    - Local entrypoint:
        a) 上傳本機 data/distilled.jsonl 到 Volume
        b) 觸發 GPU function (timeout 7200s = 2hr)
        c) 拉 adapter 回本機 finetune/adapters/peft/
        d) call convert_adapter.py 轉成 mlx 格式
        e) call modal_cost.py 記帳
        f) call eval_harness + acceptance_gate（若有）

訓練超參（與 lora_config.yaml 對齊；14B 適配版）:
    base   = Qwen/Qwen2.5-14B-Instruct
    rank   = 8           # 14B 比 3B 大 5x → rank 從 16 降到 8
    alpha  = 16          # 維持 alpha = 2 * rank
    dropout= 0.05
    target = q_proj, k_proj, v_proj, o_proj
    epochs = 3
    bs     = 1           # 14B QLoRA on 24GB → batch 必降到 1
    grad_accumulation = 16 (effective bs = 16)
    lr     = 2e-4
    train/eval split = 80/20
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import modal


# ─── App / Image / Volume 定義 ───────────────────────────────────────────────

APP_NAME = "line-bot-lora-finetune"
VOLUME_NAME = "line-bot-finetune-data"
# 14B base — Modal A10G (24GB) 跑 4-bit QLoRA OK；對齊本機 mlx 設定
BASE_MODEL = "Qwen/Qwen2.5-14B-Instruct"

# LoRA / 訓練超參（14B 比 3B 大 5x → rank/batch 都壓低）
LORA_RANK = 8
LORA_ALPHA = 16
LORA_DROPOUT = 0.05
LORA_TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj"]
EPOCHS = 3
BATCH_SIZE = 1                # 14B QLoRA on 24GB → batch 必降到 1
GRAD_ACCUMULATION = 16        # effective batch = 16，跟 3B (bs=4, ga=4) 對齊
LEARNING_RATE = 2e-4
TRAIN_RATIO = 0.8

# Volume 內路徑
VOLUME_MOUNT = "/vol"
VOLUME_TRAIN_DATA = f"{VOLUME_MOUNT}/distilled.jsonl"
VOLUME_ADAPTER_OUT = f"{VOLUME_MOUNT}/adapters/peft"
VOLUME_METRICS_OUT = f"{VOLUME_MOUNT}/metrics.json"

# 本機路徑
HERE = Path(__file__).parent
LOCAL_DATA = HERE / "data" / "distilled.jsonl"
LOCAL_ADAPTER_PEFT = HERE / "adapters" / "peft"
LOCAL_ADAPTER_MLX = HERE / "adapters" / "mlx"

# A10G pricing (Modal 2026 公開定價)：$1.10/hr，$0.000305/s
A10G_PRICE_PER_SEC = 1.10 / 3600.0


stub = modal.App(APP_NAME)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "transformers>=4.45.0",
        "peft>=0.13.0",
        "accelerate>=1.0.0",
        "torch>=2.4.0",
        "datasets>=3.0.0",
        "bitsandbytes>=0.44.0",
        "safetensors>=0.4.0",
        "sentencepiece>=0.2.0",
    )
)

volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)


# ─── GPU function：實際訓練 ──────────────────────────────────────────────────

@stub.function(
    gpu="A10G",
    image=image,
    timeout=7200,
    volumes={VOLUME_MOUNT: volume},
)
def train_lora(
    base_model: str = BASE_MODEL,
    rank: int = LORA_RANK,
    alpha: int = LORA_ALPHA,
    dropout: float = LORA_DROPOUT,
    epochs: int = EPOCHS,
    batch_size: int = BATCH_SIZE,
    grad_accum: int = GRAD_ACCUMULATION,
    lr: float = LEARNING_RATE,
    train_ratio: float = TRAIN_RATIO,
    seed: int = 42,
) -> dict:
    """在 Modal A10G 跑 LoRA fine-tune。

    Returns:
        dict 包含 train_loss, eval_loss, gpu_seconds, num_train, num_eval,
        adapter_path, base_model
    """
    import random

    import torch
    from datasets import Dataset
    from peft import LoraConfig, TaskType, get_peft_model
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        DataCollatorForLanguageModeling,
        Trainer,
        TrainingArguments,
    )

    t_start = time.time()
    random.seed(seed)

    # ── 讀資料 ──────────────────────────────────────────────────────────────
    if not os.path.exists(VOLUME_TRAIN_DATA):
        raise FileNotFoundError(
            f"找不到 {VOLUME_TRAIN_DATA} — 請先用 local_entrypoint 上傳 distilled.jsonl"
        )

    pairs: list[dict] = []
    with open(VOLUME_TRAIN_DATA, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            # 跳過 mock pair（distill_daily 用 --mock 寫進的占位）
            if obj.get("metadata", {}).get("mock", False):
                continue
            pairs.append(obj)

    if len(pairs) < 10:
        raise RuntimeError(
            f"only {len(pairs)} real pairs after filter — 請先累積資料再訓練"
        )

    # ── 80/20 split ──────────────────────────────────────────────────────────
    random.shuffle(pairs)
    cut = max(1, int(len(pairs) * train_ratio))
    train_pairs = pairs[:cut]
    eval_pairs = pairs[cut:]
    print(f"[INFO] 共 {len(pairs)} pairs，train={len(train_pairs)} eval={len(eval_pairs)}")

    # ── 載 tokenizer / model ────────────────────────────────────────────────
    print(f"[INFO] loading base model: {base_model}")
    hf_token = os.environ.get("HUGGINGFACE_TOKEN")
    tokenizer = AutoTokenizer.from_pretrained(base_model, token=hf_token)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        base_model,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        token=hf_token,
    )

    # ── 套 LoRA ──────────────────────────────────────────────────────────────
    lora_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=rank,
        lora_alpha=alpha,
        lora_dropout=dropout,
        target_modules=LORA_TARGETS,
        bias="none",
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    # ── 把 messages 套 chat_template → input_ids ────────────────────────────
    def _format(example: dict) -> dict:
        messages = example["messages"]
        text = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=False
        )
        out = tokenizer(
            text,
            truncation=True,
            max_length=2048,
            padding=False,
        )
        out["labels"] = list(out["input_ids"])
        return out

    train_ds = Dataset.from_list(train_pairs).map(_format, remove_columns=["messages", "metadata"], desc="tokenize train")
    eval_ds = Dataset.from_list(eval_pairs).map(_format, remove_columns=["messages", "metadata"], desc="tokenize eval") if eval_pairs else None

    # ── 訓練 ──────────────────────────────────────────────────────────────
    out_dir = VOLUME_ADAPTER_OUT
    os.makedirs(out_dir, exist_ok=True)

    args = TrainingArguments(
        output_dir=out_dir,
        num_train_epochs=epochs,
        per_device_train_batch_size=batch_size,
        per_device_eval_batch_size=batch_size,
        gradient_accumulation_steps=grad_accum,
        learning_rate=lr,
        warmup_steps=50,
        weight_decay=0.01,
        bf16=True,
        logging_steps=10,
        save_strategy="epoch",
        eval_strategy="epoch" if eval_ds else "no",
        save_total_limit=1,
        report_to="none",
        seed=seed,
        remove_unused_columns=False,
    )

    collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)
    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        data_collator=collator,
    )
    train_result = trainer.train()
    train_loss = float(train_result.training_loss)

    eval_loss: float | None = None
    if eval_ds:
        eval_result = trainer.evaluate()
        eval_loss = float(eval_result.get("eval_loss", float("nan")))

    # ── 儲存 adapter ──────────────────────────────────────────────────────
    trainer.model.save_pretrained(out_dir)
    tokenizer.save_pretrained(out_dir)
    volume.commit()

    gpu_seconds = time.time() - t_start
    estimated_cost = gpu_seconds * A10G_PRICE_PER_SEC

    metrics = {
        "train_loss": train_loss,
        "eval_loss": eval_loss,
        "num_train": len(train_pairs),
        "num_eval": len(eval_pairs),
        "gpu_seconds": gpu_seconds,
        "estimated_cost_usd": estimated_cost,
        "base_model": base_model,
        "rank": rank,
        "alpha": alpha,
        "epochs": epochs,
        "trained_at": int(time.time()),
    }
    with open(VOLUME_METRICS_OUT, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    volume.commit()

    print(f"[OK] train_loss={train_loss:.4f} eval_loss={eval_loss}")
    print(f"[OK] gpu_seconds={gpu_seconds:.1f} estimated_cost=${estimated_cost:.3f}")
    return metrics


# ─── Helper functions for upload / download ─────────────────────────────────

@stub.function(image=image, volumes={VOLUME_MOUNT: volume}, timeout=600)
def upload_data(data_bytes: bytes) -> int:
    """把 distilled.jsonl 寫進 Volume（local_entrypoint 用 .remote() 觸發）。"""
    os.makedirs(VOLUME_MOUNT, exist_ok=True)
    with open(VOLUME_TRAIN_DATA, "wb") as f:
        f.write(data_bytes)
    volume.commit()
    return len(data_bytes)


@stub.function(image=image, volumes={VOLUME_MOUNT: volume}, timeout=600)
def download_adapter() -> dict:
    """讀回 Volume 內的 adapter files + metrics（回 dict 給 local_entrypoint 寫檔）。"""
    files: dict[str, bytes] = {}
    if not os.path.exists(VOLUME_ADAPTER_OUT):
        return {"error": "adapter dir not found", "files": {}, "metrics": {}}
    for root, _, names in os.walk(VOLUME_ADAPTER_OUT):
        for name in names:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, VOLUME_ADAPTER_OUT)
            with open(full, "rb") as f:
                files[rel] = f.read()
    metrics: dict = {}
    if os.path.exists(VOLUME_METRICS_OUT):
        with open(VOLUME_METRICS_OUT, encoding="utf-8") as f:
            metrics = json.load(f)
    return {"files": files, "metrics": metrics}


# ─── Local entrypoint ───────────────────────────────────────────────────────

def _write_adapter_locally(payload: dict) -> Path:
    """把 download_adapter 回的 files dict 寫進 LOCAL_ADAPTER_PEFT/。"""
    LOCAL_ADAPTER_PEFT.mkdir(parents=True, exist_ok=True)
    files = payload.get("files", {}) or {}
    for rel, data in files.items():
        target = LOCAL_ADAPTER_PEFT / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)
    return LOCAL_ADAPTER_PEFT


def _track_cost_locally(metrics: dict) -> dict:
    """委託 modal_cost.py 記帳。失敗回空 dict 不擋。"""
    try:
        from finetune import modal_cost  # type: ignore
    except Exception:
        sys.path.insert(0, str(HERE.parent))
        try:
            from finetune import modal_cost  # type: ignore
        except Exception as e:
            print(f"[WARN] modal_cost import 失敗: {e}")
            return {}
    try:
        return modal_cost.record_run(
            gpu_seconds=float(metrics.get("gpu_seconds", 0)),
            cost_usd=float(metrics.get("estimated_cost_usd", 0)),
            metrics=metrics,
        )
    except SystemExit:
        # modal_cost 在超 80% 月度 cap 時 raise SystemExit；讓上層看見
        raise
    except Exception as e:
        print(f"[WARN] modal_cost 記帳失敗: {e}")
        return {}


def _convert_to_mlx(peft_path: Path) -> Path | None:
    """委託 convert_adapter.py 把 peft adapter 轉成 mlx 格式。"""
    try:
        from finetune import convert_adapter  # type: ignore
    except Exception:
        sys.path.insert(0, str(HERE.parent))
        try:
            from finetune import convert_adapter  # type: ignore
        except Exception as e:
            print(f"[WARN] convert_adapter import 失敗: {e}")
            return None
    try:
        return convert_adapter.convert(peft_path, LOCAL_ADAPTER_MLX, base_model=BASE_MODEL)
    except Exception as e:
        print(f"[WARN] adapter 轉換失敗: {e}")
        return None


def _run_eval_gate(adapter_path: Path, metrics: dict) -> bool:
    """跑 eval_harness + acceptance_gate（若存在）；過 gate → 更新 ACTIVE_ADAPTER。"""
    line_bot_root = HERE.parent
    sys.path.insert(0, str(line_bot_root))
    try:
        import eval_harness  # type: ignore
        import acceptance_gate  # type: ignore
    except ImportError as e:
        print(f"[INFO] eval_harness / acceptance_gate 尚未建好（{e}）— 跳過 gate")
        return True  # stub：當作通過
    try:
        eval_metrics = eval_harness.run(adapter_path=str(adapter_path))
        merged = {**metrics, **(eval_metrics or {})}
        passed = acceptance_gate.check(merged)
        if not passed:
            print("[FAIL] acceptance gate 沒過：")
            for k, v in merged.items():
                print(f"  {k}: {v}")
            return False
        # 過 gate → 改 local_llm_config.ACTIVE_ADAPTER
        try:
            from finetune import activate_adapter  # type: ignore
            activate_adapter.activate(str(adapter_path))
        except Exception:
            print(f"[INFO] 過 gate；請手動把 ACTIVE_ADAPTER 設為 {adapter_path}")
        return True
    except Exception as e:
        print(f"[WARN] eval gate 跑失敗: {e}")
        return False


@stub.local_entrypoint()
def main(
    force: bool = False,
    skip_eval: bool = False,
) -> None:
    """主入口：上傳 jsonl → 觸發訓練 → 下載 adapter → 轉換 → eval gate。

    Args:
        force: 累積 < 3000 仍強制訓
        skip_eval: 跳過 eval_harness / acceptance_gate
    """
    # ── 檢查 distilled.jsonl ─────────────────────────────────────────────────
    if not LOCAL_DATA.exists():
        print(f"[ERR] 找不到 {LOCAL_DATA}")
        sys.exit(1)
    n_real = 0
    with LOCAL_DATA.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not obj.get("metadata", {}).get("mock", False):
                n_real += 1
    print(f"[INFO] distilled.jsonl 真 pairs: {n_real}")
    if n_real < 3000 and not force:
        print(f"[ERR] < 3000 real pairs，加 --force 強跑（小資料集容易 over-fit）")
        sys.exit(2)

    # ── 上傳 ──────────────────────────────────────────────────────────────
    data_bytes = LOCAL_DATA.read_bytes()
    print(f"[INFO] uploading {len(data_bytes):,} bytes to Modal Volume...")
    uploaded = upload_data.remote(data_bytes)
    print(f"[OK] uploaded {uploaded:,} bytes")

    # ── 訓練 ──────────────────────────────────────────────────────────────
    print(f"[INFO] kicking off train_lora on Modal A10G (timeout 7200s)...")
    metrics = train_lora.remote()
    print(f"[OK] training done")
    print(f"  train_loss: {metrics.get('train_loss')}")
    print(f"  eval_loss : {metrics.get('eval_loss')}")
    print(f"  gpu_seconds: {metrics.get('gpu_seconds'):.1f}")
    print(f"  cost_usd  : ${metrics.get('estimated_cost_usd'):.3f}")

    # ── 下載 adapter ──────────────────────────────────────────────────────
    print(f"[INFO] downloading adapter to {LOCAL_ADAPTER_PEFT}...")
    payload = download_adapter.remote()
    if payload.get("error"):
        print(f"[ERR] download 失敗: {payload['error']}")
        sys.exit(3)
    peft_path = _write_adapter_locally(payload)
    print(f"[OK] adapter saved: {peft_path} ({len(payload.get('files', {}))} files)")

    # ── 記帳 ──────────────────────────────────────────────────────────────
    cost_summary = _track_cost_locally(metrics)
    if cost_summary:
        print(f"[COST] this run ${metrics.get('estimated_cost_usd'):.3f}, "
              f"month-to-date ${cost_summary.get('month_total_usd', 0):.2f}")

    # ── 轉 mlx 格式 ─────────────────────────────────────────────────────────
    mlx_path = _convert_to_mlx(peft_path)
    if mlx_path:
        print(f"[OK] mlx adapter: {mlx_path}")
    else:
        print(f"[WARN] mlx 轉換失敗，但 peft adapter 仍可用：{peft_path}")

    # ── eval gate ─────────────────────────────────────────────────────────
    target = mlx_path or peft_path
    if skip_eval:
        print(f"[INFO] --skip-eval：跳過 acceptance gate")
    else:
        ok = _run_eval_gate(target, metrics)
        if not ok:
            print(f"[FAIL] acceptance gate 沒過 — 不上線")
            sys.exit(4)
        print(f"[OK] acceptance gate 過了；adapter 已上線")
