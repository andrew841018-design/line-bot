"""Lightning AI Studios LoRA fine-tune (Modal/Kaggle 第三備援).

跟 modal_train.py / kaggle_train.ipynb 邏輯對齊，但是 standalone Python script —
Lightning AI Studios 環境沒 Modal Volume API、不是 Notebook，所以這支設計成
直接 `python lightning_train.py` 就跑。

執行流程（在 Lightning AI Studios 內）：
    1. Studio 開 A10/L4 GPU instance（24GB VRAM 免費 22hr/月）
    2. 上傳 distilled.jsonl 到 ./data/distilled.jsonl
       （或 git clone repo 整包進來，./finetune/data/distilled.jsonl）
    3. pip install transformers peft accelerate datasets bitsandbytes
    4. python finetune/lightning_train.py
    5. 完成後到 ./lora_out/ 下載 adapter（Studio 右側檔案 panel）

訓練超參（與 lora_config.yaml / modal_train / kaggle_train 對齊）：
    base   = Qwen/Qwen2.5-14B-Instruct  (24GB VRAM 跑 4-bit + LoRA OK)
    rank   = 16
    alpha  = 32
    dropout= 0.05
    target = q_proj, k_proj, v_proj, o_proj
    epochs = 3
    bs     = 2  (14B 比 3B 大，bs 砍半免 OOM)
    grad_accumulation = 8 (effective bs = 16 維持一致)
    lr     = 2e-4
    train/eval split = 80/20

Lightning AI 24GB GPU + 14B 模型估算：
    14B fp16 載入 ~28GB → 必須 4-bit (bnb_4bit nf4) → ~7GB
    LoRA r=16 q/k/v/o → 額外 ~50MB trainable
    activation + grad ckpt → ~10GB working memory
    總 footprint ~17-19GB，留 5-7GB buffer，跑 1000 pairs / 3 epoch ≈ 4-6hr on L4
    （A10 比 L4 快約 1.3x；24GB 是 sweet spot 給 14B 4-bit）
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
import time
from pathlib import Path

# ─── 超參（跟其他訓練 script 對齊）────────────────────────────────────────────

BASE_MODEL = os.environ.get("LIGHTNING_BASE_MODEL", "Qwen/Qwen2.5-14B-Instruct")

LORA_RANK = 16
LORA_ALPHA = 32
LORA_DROPOUT = 0.05
LORA_TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj"]

EPOCHS = 3
BATCH_SIZE = 2  # 14B 4-bit 在 24GB 上 bs=2 比較穩
GRAD_ACCUMULATION = 8  # effective bs = 16
LEARNING_RATE = 2e-4
TRAIN_RATIO = 0.8
MAX_LEN = 2048
SEED = 42

# ─── 路徑（Studio 內相對路徑）────────────────────────────────────────────────

HERE = Path(__file__).parent

# 優先吃當前目錄的 distilled.jsonl（user 直接上傳到 Studio 根目錄常見），
# 然後 fallback 到 finetune/data/（git clone 完整 repo 進 Studio 的情況）
_CANDIDATE_DATA_PATHS = [
    Path("./distilled.jsonl"),
    Path("./data/distilled.jsonl"),
    HERE / "data" / "distilled.jsonl",
    Path("/teamspace/studios/this_studio/distilled.jsonl"),  # Lightning Studio default workdir
]

OUTPUT_DIR = Path("./lora_out").resolve()
METRICS_OUT = OUTPUT_DIR / "metrics.json"
ZIP_OUT = OUTPUT_DIR.parent / "lora_out.zip"


def _find_distilled() -> Path:
    for p in _CANDIDATE_DATA_PATHS:
        if p.exists():
            return p.resolve()
    raise FileNotFoundError(
        "找不到 distilled.jsonl —— 請上傳到 Studio ./distilled.jsonl "
        "或 git clone 整包 repo 後跑 from /teamspace/studios/this_studio/line_bot/"
    )


def _load_pairs(path: Path) -> list[dict]:
    pairs: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("metadata", {}).get("mock", False):
                continue
            pairs.append(obj)
    return pairs


def main() -> int:
    t_start = time.time()
    random.seed(SEED)

    # ── 找資料 ──────────────────────────────────────────────────────────────
    try:
        distilled_path = _find_distilled()
    except FileNotFoundError as e:
        print(f"[ERR] {e}")
        return 1
    print(f"[INFO] using {distilled_path}")

    pairs = _load_pairs(distilled_path)
    if len(pairs) < 10:
        print(f"[ERR] 只有 {len(pairs)} 筆非-mock pair，先累積資料再訓")
        return 2

    random.shuffle(pairs)
    cut = max(1, int(len(pairs) * TRAIN_RATIO))
    train_pairs = pairs[:cut]
    eval_pairs = pairs[cut:]
    print(f"[INFO] {len(pairs)} pairs → train={len(train_pairs)} eval={len(eval_pairs)}")

    if len(train_pairs) < 100:
        print("[WARN] 訓練資料 < 100 筆，fine-tune 預期效果差")

    # ── lazy import（讓本機 syntax check 不需要這些 deps）─────────────────────
    import torch
    from datasets import Dataset
    from peft import LoraConfig, TaskType, get_peft_model, prepare_model_for_kbit_training
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        BitsAndBytesConfig,
        DataCollatorForLanguageModeling,
        Trainer,
        TrainingArguments,
    )

    # ── 載 tokenizer / 4-bit model ─────────────────────────────────────────
    print(f"[INFO] loading base model: {BASE_MODEL} (4-bit)")
    hf_token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, token=hf_token)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        quantization_config=bnb_config,
        device_map="auto",
        trust_remote_code=True,
        token=hf_token,
    )
    model.config.use_cache = False
    model = prepare_model_for_kbit_training(model)

    # ── 套 LoRA ─────────────────────────────────────────────────────────────
    lora_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=LORA_RANK,
        lora_alpha=LORA_ALPHA,
        lora_dropout=LORA_DROPOUT,
        target_modules=LORA_TARGETS,
        bias="none",
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    # ── tokenize ────────────────────────────────────────────────────────────
    SYSTEM_PROMPT = "你是 LINE 群組對話助理咪寶，繁體中文簡短回覆，有具體觀點。"

    def _format(example: dict) -> dict:
        msgs = example["messages"]
        # 補 system prompt 對齊 inference / kaggle_train
        full_msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + msgs
        text = tokenizer.apply_chat_template(
            full_msgs, tokenize=False, add_generation_prompt=False
        )
        out = tokenizer(text, truncation=True, max_length=MAX_LEN, padding=False)
        out["labels"] = list(out["input_ids"])
        return out

    train_ds = Dataset.from_list(train_pairs).map(
        _format, remove_columns=["messages", "metadata"], desc="tokenize train"
    )
    eval_ds = (
        Dataset.from_list(eval_pairs).map(
            _format, remove_columns=["messages", "metadata"], desc="tokenize eval"
        )
        if eval_pairs
        else None
    )

    # ── 訓練 ────────────────────────────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    training_args = TrainingArguments(
        output_dir=str(OUTPUT_DIR),
        num_train_epochs=EPOCHS,
        per_device_train_batch_size=BATCH_SIZE,
        per_device_eval_batch_size=BATCH_SIZE,
        gradient_accumulation_steps=GRAD_ACCUMULATION,
        learning_rate=LEARNING_RATE,
        warmup_steps=50,
        weight_decay=0.01,
        bf16=True,
        gradient_checkpointing=True,
        optim="paged_adamw_8bit",
        logging_steps=10,
        save_strategy="epoch",
        eval_strategy="epoch" if eval_ds else "no",
        save_total_limit=1,
        report_to="none",
        seed=SEED,
        remove_unused_columns=False,
    )

    collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)
    trainer = Trainer(
        model=model,
        args=training_args,
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

    # ── 儲存 ────────────────────────────────────────────────────────────────
    trainer.model.save_pretrained(str(OUTPUT_DIR))
    tokenizer.save_pretrained(str(OUTPUT_DIR))

    wall_seconds = time.time() - t_start
    metrics = {
        "label": "lightning_adapter",
        "base_model": BASE_MODEL,
        "train_loss": train_loss,
        "eval_loss": eval_loss,
        "num_train": len(train_pairs),
        "num_eval": len(eval_pairs),
        "wall_seconds": wall_seconds,
        "lora": {
            "r": LORA_RANK,
            "alpha": LORA_ALPHA,
            "dropout": LORA_DROPOUT,
            "target_modules": LORA_TARGETS,
        },
        "hyperparams": {
            "epochs": EPOCHS,
            "batch_size": BATCH_SIZE,
            "grad_accumulation": GRAD_ACCUMULATION,
            "lr": LEARNING_RATE,
        },
        "trained_at": int(time.time()),
    }
    with METRICS_OUT.open("w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    # ── zip 方便從 Studio download 一個檔搞定 ──────────────────────────────
    try:
        zip_base = str(ZIP_OUT.with_suffix(""))
        shutil.make_archive(zip_base, "zip", str(OUTPUT_DIR))
        size_mb = ZIP_OUT.stat().st_size / (1024 * 1024)
        print(f"[OK] zipped {ZIP_OUT} ({size_mb:.1f} MB)")
    except Exception as e:
        print(f"[WARN] zip 失敗（不影響 adapter 本體）: {e}")

    print(f"[OK] train_loss={train_loss:.4f} eval_loss={eval_loss}")
    print(f"[OK] wall_seconds={wall_seconds:.1f}")
    print(f"[OK] adapter saved to {OUTPUT_DIR}")
    print(f"[NEXT] Studio 右側檔案 panel 抓 lora_out.zip 或整個 lora_out/ 下載回本機")
    print(f"       本機解壓到 finetune/adapters_lightning/ 後跑 acceptance_gate.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
