"""Convert peft (HuggingFace) LoRA adapter → mlx-lm adapter format。

mlx-lm 與 peft adapter 的差異：
- peft：`adapter_model.safetensors` 內 key 形如
    `base_model.model.model.layers.0.self_attn.q_proj.lora_A.default.weight`
  和 `adapter_config.json`（含 r, alpha, target_modules, base_model_name_or_path）
- mlx-lm：`adapters.safetensors` 內 key 形如
    `model.layers.0.self_attn.q_proj.lora_a`
  和 `adapter_config.json`（mlx-lm 自家 schema：fine_tune_type, lora_parameters.rank）

策略（簡單路線，預設用 fuse 不要轉）：
    A) 若有 `mlx_lm` 套件 + 取得 base model：建議直接用 `mlx_lm.fuse` 把 LoRA
       merge 進 base model；雖然 fused weights 較大（fp16 / 4-bit）但 inference
       不需要再分開載 adapter。
    B) 純 weight rename：跑 `_remap_peft_to_mlx()`，rename keys + 寫 mlx-lm
       自家 adapter_config.json + adapters.safetensors。

預設用 (B) — 簡單、不需要 base model；user 之後愛 fuse 自己跑 mlx_lm.fuse。

CLI：
    python finetune/convert_adapter.py \
        --peft finetune/adapters/peft \
        --out  finetune/adapters/mlx \
        --base mlx-community/Qwen2.5-14B-Instruct-4bit
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


# peft → mlx-lm key rename pattern
# peft     : base_model.model.model.layers.{N}.self_attn.{q,k,v,o}_proj.lora_{A,B}.default.weight
# mlx-lm   : model.layers.{N}.self_attn.{q,k,v,o}_proj.lora_{a,b}
_PEFT_KEY_RE = re.compile(
    r"^base_model\.model\.(?P<body>.+)\.lora_(?P<ab>[AB])\.default\.weight$"
)


def _peft_key_to_mlx(key: str) -> str | None:
    """Rename one peft key → mlx-lm key。返回 None 若不認識。"""
    m = _PEFT_KEY_RE.match(key)
    if not m:
        return None
    body = m.group("body")  # e.g. "model.layers.0.self_attn.q_proj"
    ab = m.group("ab").lower()  # "a" or "b"
    return f"{body}.lora_{ab}"


def _remap_peft_to_mlx(peft_dir: Path, out_dir: Path, base_model: str | None = None) -> Path:
    """讀 peft adapter → 寫 mlx 格式 adapter。

    讀取：
      - adapter_config.json (peft)
      - adapter_model.safetensors

    寫出：
      - adapter_config.json (mlx-lm schema)
      - adapters.safetensors

    Returns:
        out_dir 路徑
    """
    try:
        from safetensors import safe_open
        from safetensors.torch import save_file
    except Exception as e:
        raise RuntimeError(f"safetensors 套件缺失: {e}")

    cfg_in = peft_dir / "adapter_config.json"
    weights_in = peft_dir / "adapter_model.safetensors"
    if not cfg_in.exists():
        raise FileNotFoundError(f"找不到 {cfg_in}")
    if not weights_in.exists():
        raise FileNotFoundError(f"找不到 {weights_in}")

    with cfg_in.open(encoding="utf-8") as f:
        peft_cfg: dict[str, Any] = json.load(f)

    # mlx-lm 自家 adapter_config schema
    mlx_cfg = {
        "fine_tune_type": "lora",
        "num_layers": -1,  # mlx-lm 視為 "全部 layer 都套了"，因為 peft 沒記只套幾層
        "lora_parameters": {
            "rank": int(peft_cfg.get("r", 16)),
            "scale": float(peft_cfg.get("lora_alpha", 32)) / float(peft_cfg.get("r", 16)),
            "dropout": float(peft_cfg.get("lora_dropout", 0.0)),
            "keys": list(peft_cfg.get("target_modules", [])),
        },
        "base_model": base_model or peft_cfg.get("base_model_name_or_path", ""),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "adapter_config.json").open("w", encoding="utf-8") as f:
        json.dump(mlx_cfg, f, indent=2, ensure_ascii=False)

    # rename weight keys
    new_tensors: dict[str, Any] = {}
    skipped: list[str] = []
    with safe_open(str(weights_in), framework="pt") as f:  # type: ignore[arg-type]
        for k in f.keys():
            new_k = _peft_key_to_mlx(k)
            if new_k is None:
                skipped.append(k)
                continue
            new_tensors[new_k] = f.get_tensor(k)

    if not new_tensors:
        raise RuntimeError(f"沒任何 key 從 peft 匹配到 mlx；examples: {list(skipped)[:3]}")

    save_file(new_tensors, str(out_dir / "adapters.safetensors"))
    if skipped:
        print(f"[WARN] {len(skipped)} keys 跳過（不認識）：{skipped[:2]}")
    return out_dir


def convert(peft_dir: Path, out_dir: Path, base_model: str | None = None) -> Path:
    """Public API — 讓 modal_train.py 呼叫。"""
    peft_dir = Path(peft_dir)
    out_dir = Path(out_dir)
    return _remap_peft_to_mlx(peft_dir, out_dir, base_model=base_model)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--peft", required=True, help="peft adapter dir (含 adapter_config.json)")
    ap.add_argument("--out", required=True, help="mlx adapter 輸出 dir")
    ap.add_argument("--base", default=None, help="base model id（mlx-lm 載 inference 用）")
    args = ap.parse_args()

    try:
        out = convert(Path(args.peft), Path(args.out), base_model=args.base)
    except Exception as e:
        print(f"[ERR] {e}", file=sys.stderr)
        return 1
    print(f"[OK] mlx adapter written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
