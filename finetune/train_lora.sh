#!/usr/bin/env bash
# train_lora.sh — LoRA fine-tune launch script (範本，預期不真跑)
#
# 兩個 backend 二選一：mac (mlx_lm) 或 cuda (accelerate + peft)。
# 都先確認 finetune/data/sft.jsonl 存在 (extract_data.py 跑過)。
#
# 預期 wall-clock / 成本：
# ─────────────────────────────────────────────────────────────────────
# Mac M2 Pro 16GB / Llama-3.2-3B / 1000 pairs / 3 epochs:
#     ≈ 4–8 hr，本地電費 NT$10 以內
#
# H100 80GB (RunPod / Lambda) / Qwen2.5-7B / 1000 pairs / 3 epochs:
#     ≈ 30–90 min，spot 約 $2/hr → 總成本 $3 USD
#
# 4090 24GB (本地 / 租) / Qwen2.5-7B-QLoRA / 1000 pairs / 3 epochs:
#     ≈ 2–4 hr，本地電費 NT$30
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

BACKEND="${BACKEND:-auto}"      # auto / mac / cuda
DATA_FILE="${DATA_FILE:-data/sft.jsonl}"
CONFIG="${CONFIG:-lora_config.yaml}"

# 若 BACKEND=auto，依 OS 選擇
if [[ "$BACKEND" == "auto" ]]; then
    if [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]]; then
        BACKEND="mac"
    else
        BACKEND="cuda"
    fi
fi

# 資料檢查
if [[ ! -f "$DATA_FILE" ]]; then
    echo "[ERR] 找不到 $DATA_FILE — 請先跑：python extract_data.py"
    exit 1
fi

NUM_PAIRS="$(wc -l < "$DATA_FILE" | tr -d ' ')"
echo "[INFO] dataset: $DATA_FILE ($NUM_PAIRS pairs)"
echo "[INFO] backend: $BACKEND"

if [[ "$NUM_PAIRS" -lt 500 ]]; then
    echo "[WARN] dataset < 500 筆，fine-tune 效果預期很差，建議改用 RAG"
    echo "[WARN] 仍要繼續？Ctrl-C 中斷，5 秒後會開始..."
    sleep 5
fi

case "$BACKEND" in
    mac)
        # ─── Mac MLX 後端 ─────────────────────────────────────────────
        # 安裝（首次）：pip install mlx-lm
        # 文件：https://github.com/ml-explore/mlx-examples/tree/main/lora
        echo "[RUN] mac mlx_lm.lora 訓練（範本，未實際執行）"
        cat <<'EOF'
mlx_lm.lora \
    --model mlx-community/Llama-3.2-3B-Instruct-4bit \
    --train \
    --data data \
    --iters 600 \
    --batch-size 4 \
    --num-layers 16 \
    --learning-rate 2e-4 \
    --val-batches 25 \
    --steps-per-eval 100 \
    --steps-per-report 10 \
    --adapter-path outputs/llama32-3b-lora \
    --seed 42
EOF
        echo "[NOTE] 註解掉 echo / cat 改成直接執行即可實際 train"
        ;;

    cuda)
        # ─── CUDA / Linux GPU 後端 ───────────────────────────────────
        # 安裝（首次）：
        #   pip install torch transformers peft accelerate datasets bitsandbytes
        #   accelerate config  # 第一次需互動設定 GPU / mixed precision
        # 也可用 deepspeed zero2 跑 multi-GPU；單卡省略 ds_zero2.yaml
        echo "[RUN] CUDA accelerate launch（範本，未實際執行）"
        cat <<'EOF'
# train.py 是 huggingface 標準 SFT script (還沒寫，需照 lora_config.yaml 自己組)
# 推薦用 trl SFTTrainer 或 axolotl 直接吃 yaml
accelerate launch \
    --config_file ds_zero2.yaml \
    train.py \
        --model_name_or_path Qwen/Qwen2.5-7B-Instruct \
        --train_file data/sft.jsonl \
        --output_dir outputs/qwen25-7b-lora \
        --num_train_epochs 3 \
        --per_device_train_batch_size 4 \
        --gradient_accumulation_steps 4 \
        --learning_rate 2e-4 \
        --warmup_steps 50 \
        --bf16 \
        --use_peft \
        --lora_r 16 \
        --lora_alpha 32 \
        --lora_dropout 0.05 \
        --lora_target_modules q_proj k_proj v_proj o_proj \
        --logging_steps 10 \
        --eval_steps 100 \
        --save_steps 200 \
        --seed 42

# 替代：直接用 axolotl（更簡潔，吃 lora_config.yaml）
# axolotl train lora_config.yaml
EOF
        echo "[NOTE] 註解掉 echo / cat 改成直接執行即可實際 train"
        ;;

    *)
        echo "[ERR] unknown BACKEND=$BACKEND（要 mac / cuda）"
        exit 1
        ;;
esac

echo ""
echo "[NEXT] train 完之後："
echo "  1. ls outputs/  # 確認 adapter 檔有產出"
echo "  2. python inference.py  # smoke test"
echo "  3. 在 main.py 裡 try fine-tuned 失敗 fallback Gemini"
