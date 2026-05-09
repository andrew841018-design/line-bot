"""從 Kaggle Notebook output 下載訓練好的 adapter zip → 整合 acceptance gate。

用法：
    # 下載 adapter（user 在 Kaggle 上 publish 完 notebook 後）
    python finetune/kaggle_download_adapter.py --notebook <username>/<notebook-slug>

    # 加跑 acceptance gate（需要 baseline.json）
    python finetune/kaggle_download_adapter.py --notebook <username>/<notebook-slug> \\
        --baseline finetune/eval_results/baseline.json \\
        --run-gate

流程：
    1. kaggle kernels output 抓 notebook 的所有 output → 解 adapter.zip
    2. 解到 finetune/adapters_kaggle/
    3. 若 --run-gate：呼叫 acceptance_gate（如果 eval_harness 存在就用，沒有就 stub）
    4. gate 全過 → 寫 ACTIVE_ADAPTER 進 local_llm_config.py
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
LINE_BOT = HERE.parent
sys.path.insert(0, str(LINE_BOT))

ADAPTER_DIR = HERE / "adapters_kaggle"
DOWNLOAD_DIR = HERE / "_kaggle_dl"

KAGGLE_CONFIG_INSTRUCTIONS = """
[ERR] 找不到 ~/.kaggle/kaggle.json — 設定教學：

  1. 開 https://www.kaggle.com/settings/account
  2. 滾到「API」section → 點「Create New API Token」
  3. 會下載 kaggle.json 到 ~/Downloads/
  4. 跑：
       mkdir -p ~/.kaggle
       mv ~/Downloads/kaggle.json ~/.kaggle/
       chmod 600 ~/.kaggle/kaggle.json
  5. 再跑一次 python finetune/kaggle_download_adapter.py
"""


def _check_kaggle_auth() -> bool:
    home = Path.home()
    cfg = home / ".kaggle" / "kaggle.json"
    if cfg.exists():
        return True
    if os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY"):
        return True
    return False


def _check_kaggle_sdk() -> bool:
    try:
        subprocess.run(
            [sys.executable, "-c", "import kaggle"],
            check=True,
            capture_output=True,
        )
        return True
    except Exception:
        return False


def download_kernel_output(notebook: str, dest: Path) -> int:
    """跑 `kaggle kernels output <notebook> -p dest`。"""
    dest.mkdir(parents=True, exist_ok=True)
    print(f"[INFO] 下載 notebook outputs → {dest}")
    rc = subprocess.call(
        [sys.executable, "-m", "kaggle", "kernels", "output", notebook,
         "-p", str(dest), "--force"],
    )
    return rc


def extract_adapter(download_dir: Path, target: Path) -> bool:
    """找 adapter.zip 解壓到 target/。"""
    candidates = list(download_dir.glob("adapter*.zip"))
    if not candidates:
        # 也許 Kaggle 把 zip 自動拆成 .zip.zip 或單檔
        candidates = list(download_dir.glob("*.zip"))
    if not candidates:
        print(f"[ERR] {download_dir} 下找不到 adapter zip")
        print(f"      內容：{list(download_dir.iterdir())}")
        return False

    zip_path = candidates[0]
    print(f"[INFO] 解 {zip_path} → {target}")
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(target)

    # sanity check：應該有 adapter_config.json + adapter_model.safetensors
    needed = ["adapter_config.json"]
    have_safetensors = any(target.glob("**/adapter_model*.safetensors")) or any(
        target.glob("**/adapter_model*.bin")
    )
    have_config = any(target.glob(f"**/{needed[0]}"))
    if not (have_config and have_safetensors):
        print(f"[WARN] 解壓後缺檔：config={have_config} weights={have_safetensors}")
        print(f"       目錄樹：")
        for p in sorted(target.rglob("*"))[:20]:
            print(f"         {p.relative_to(target)}")
        return False
    return True


def run_acceptance_gate(
    adapter_path: Path,
    baseline_json: Path,
    *,
    skip_judge: bool = False,
) -> int:
    """整合 eval_harness + acceptance_gate。

    - 若 eval_harness.py 存在 → 跑 adapter eval → 跑 gate
    - 若不存在 → stub mode：用 cell 7 的 eval_metrics.json 寫一個假的 metrics 過 G2/G3 不過 G1/G4
    """
    eval_harness = HERE / "eval_harness.py"
    gate = HERE / "acceptance_gate.py"
    eval_results = HERE / "eval_results"
    eval_results.mkdir(parents=True, exist_ok=True)
    adapter_eval_out = eval_results / "kaggle_adapter.json"

    if eval_harness.exists():
        print("[INFO] eval_harness 存在 → 跑 adapter eval")
        cmd = [
            sys.executable, str(eval_harness),
            "--model", "mlx-community/Qwen2.5-14B-Instruct-4bit",
            "--adapter", str(adapter_path),
            "--label", "kaggle_adapter",
            "--out", str(adapter_eval_out),
        ]
        if skip_judge:
            cmd.append("--skip-judge")
        rc = subprocess.call(cmd)
        if rc != 0:
            print(f"[ERR] eval_harness 跑失敗 rc={rc}")
            return rc
    else:
        print("[STUB] eval_harness 不存在 → 寫 stub metrics（gate 會 fail，需手動 review）")
        # 從 Kaggle 下載的 eval_metrics.json（cell 7 寫的）
        kaggle_metrics_files = list(DOWNLOAD_DIR.glob("**/eval_metrics.json"))
        kaggle_loss = None
        if kaggle_metrics_files:
            try:
                kaggle_metrics = json.loads(kaggle_metrics_files[0].read_text(encoding="utf-8"))
                kaggle_loss = kaggle_metrics.get("eval_loss")
            except Exception:
                pass
        stub = {
            "metrics": {
                "label": "kaggle_adapter_stub",
                "n_total": 0,
                "violation_rate": 0.0,
                "chinese_pass_rate": 0.0,
                "rule0_pass_rate": 0.0,
                "judge_avg_score": None,
                "kaggle_eval_loss": kaggle_loss,
                "_note": "stub — eval_harness 未跑，數值無意義；要正式上線請補裝 mlx-lm 後跑 eval_harness",
            },
            "samples": [],
        }
        adapter_eval_out.write_text(
            json.dumps(stub, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if not gate.exists():
        print("[ERR] acceptance_gate.py 不存在")
        return 1

    if not baseline_json.exists():
        print(f"[ERR] baseline {baseline_json} 不存在 — 先跑 eval_harness 不帶 --adapter 產 baseline")
        return 1

    cmd = [
        sys.executable, str(gate),
        "--baseline", str(baseline_json),
        "--adapter-result", str(adapter_eval_out),
        "--adapter-path", str(adapter_path),
    ]
    print("[INFO] 跑 acceptance_gate ...")
    return subprocess.call(cmd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--notebook",
        required=True,
        help="Kaggle notebook id, e.g. 'andrew841018/linebot-lora-train'",
    )
    parser.add_argument(
        "--run-gate",
        action="store_true",
        help="下載完跑 acceptance_gate（需要 --baseline）",
    )
    parser.add_argument(
        "--baseline",
        default=str(HERE / "eval_results" / "baseline.json"),
        help="baseline eval JSON path（給 acceptance gate）",
    )
    parser.add_argument("--skip-judge", action="store_true", help="eval 跳過 LLM judge")
    args = parser.parse_args()

    if not _check_kaggle_sdk():
        print("[ERR] kaggle SDK 未安裝。請跑：pip install kaggle")
        return 1

    if not _check_kaggle_auth():
        print(KAGGLE_CONFIG_INSTRUCTIONS.strip())
        return 2

    # 1. 下載
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    rc = download_kernel_output(args.notebook, DOWNLOAD_DIR)
    if rc != 0:
        print(f"[ERR] kaggle kernels output 失敗 rc={rc}")
        print("       常見原因：notebook 未 publish / id 錯 / 沒有 output")
        return rc

    # 2. 解 adapter zip
    if ADAPTER_DIR.exists():
        # 不刪舊資料；換目錄保留歷史
        import shutil, time as _t
        backup = ADAPTER_DIR.with_name(f"adapters_kaggle_old_{int(_t.time())}")
        shutil.move(str(ADAPTER_DIR), str(backup))
        print(f"[INFO] 既有 adapter 移到 {backup}")

    if not extract_adapter(DOWNLOAD_DIR, ADAPTER_DIR):
        return 3
    print(f"[OK] adapter ready → {ADAPTER_DIR}")

    # 3. 可選：跑 acceptance gate
    if args.run_gate:
        rc = run_acceptance_gate(
            ADAPTER_DIR,
            Path(args.baseline),
            skip_judge=args.skip_judge,
        )
        if rc == 0:
            print("[OK] acceptance gate 全過，ACTIVE_ADAPTER 已寫進 local_llm_config.py")
        else:
            print(f"[FAIL] acceptance gate rc={rc} — 不上線")
        return rc
    else:
        print()
        print("[NEXT] 跑 acceptance gate（4 條 G1-G4）：")
        print(f"       python {HERE.relative_to(LINE_BOT)}/eval_harness.py "
              f"--adapter {ADAPTER_DIR.relative_to(LINE_BOT)} "
              f"--label kaggle_adapter --out finetune/eval_results/kaggle_adapter.json")
        print(f"       python {HERE.relative_to(LINE_BOT)}/acceptance_gate.py "
              f"--baseline finetune/eval_results/baseline.json "
              f"--adapter-result finetune/eval_results/kaggle_adapter.json "
              f"--adapter-path {ADAPTER_DIR.relative_to(LINE_BOT)}")
        print()
        print("       或一鍵跑：")
        print(f"       python finetune/kaggle_download_adapter.py "
              f"--notebook {args.notebook} --run-gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
