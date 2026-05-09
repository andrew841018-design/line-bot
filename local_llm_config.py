"""Model selection — 由 32GB Mac 決定可載入哪個。"""
import os

# 環境變數覆寫，預設用 14B（32GB Mac OK）
LOCAL_LLM_MODEL = os.environ.get(
    "LOCAL_LLM_MODEL",
    "mlx-community/Qwen2.5-14B-Instruct-4bit",
)

# Fallback 階梯（若高階 model 失敗自動降階）
LOCAL_LLM_FALLBACKS = [
    "mlx-community/Qwen2.5-14B-Instruct-4bit",
    "mlx-community/Qwen2.5-7B-Instruct-4bit",
    "mlx-community/Qwen2.5-3B-Instruct-4bit",
]

# LoRA adapter 啟用開關。
# - None → 不載 adapter（純 base model）
# - 路徑字串 → mlx_lm.load(model, adapter_path=ACTIVE_ADAPTER)
# - 由 finetune/acceptance_gate.py 自動寫入；訓練 + 4 條 gate 全過才會被填
ACTIVE_ADAPTER = None
