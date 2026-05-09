"""把 distilled.jsonl + train notebook 打包上傳成 Kaggle Dataset。

用法：
    python finetune/kaggle_upload.py

前置：
    1. pip install kaggle
    2. https://www.kaggle.com/settings/account → Create New API Token → 下載 kaggle.json
    3. mkdir -p ~/.kaggle && mv ~/Downloads/kaggle.json ~/.kaggle/ && chmod 600 ~/.kaggle/kaggle.json

輸出：
    上傳到 https://www.kaggle.com/datasets/<your-username>/linebot-distilled
    （首次跑會 create，之後跑會 version-bump）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
DISTILLED = DATA_DIR / "distilled.jsonl"
NOTEBOOK = HERE / "kaggle_train.ipynb"

DATASET_SLUG = "linebot-distilled"
DATASET_TITLE = "LINE Bot Distilled Pairs (LoRA SFT data)"

KAGGLE_CONFIG_INSTRUCTIONS = """
[ERR] 找不到 ~/.kaggle/kaggle.json — 設定教學：

  1. 開 https://www.kaggle.com/settings/account
  2. 滾到「API」section → 點「Create New API Token」
  3. 會下載 kaggle.json 到 ~/Downloads/
  4. 跑：
       mkdir -p ~/.kaggle
       mv ~/Downloads/kaggle.json ~/.kaggle/
       chmod 600 ~/.kaggle/kaggle.json
  5. 再跑一次 python finetune/kaggle_upload.py
"""


def _check_kaggle_auth() -> bool:
    home = Path.home()
    cfg = home / ".kaggle" / "kaggle.json"
    if cfg.exists():
        return True
    # KAGGLE_USERNAME / KAGGLE_KEY env vars 也算
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


def _get_username() -> str:
    """從 ~/.kaggle/kaggle.json 或 env var 拿 username。"""
    cfg = Path.home() / ".kaggle" / "kaggle.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            return data.get("username", "")
        except Exception:
            pass
    return os.environ.get("KAGGLE_USERNAME", "")


def main() -> int:
    if not _check_kaggle_sdk():
        print("[ERR] kaggle SDK 未安裝。請跑：pip install kaggle")
        return 1

    if not _check_kaggle_auth():
        print(KAGGLE_CONFIG_INSTRUCTIONS.strip())
        return 2

    if not DISTILLED.exists():
        print(f"[ERR] {DISTILLED} 不存在 — 先跑 distill_daily.py 累積資料")
        return 3

    username = _get_username()
    if not username:
        print("[ERR] 無法從 kaggle.json 拿到 username")
        return 4

    # 統計可用 pair（非 mock）
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
    print(f"[INFO] distilled.jsonl: {n_total} total, {n_real} non-mock")
    if n_real < 100:
        print(f"[WARN] 非 mock pair < 100，訓出來效果有限。要繼續嗎？(y/N) ", end="")
        ans = input().strip().lower()
        if ans != "y":
            return 0

    # 準備 staging dir
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        # copy data
        shutil.copy2(DISTILLED, td_path / "distilled.jsonl")
        # copy notebook（給 reference / 自包含）
        if NOTEBOOK.exists():
            shutil.copy2(NOTEBOOK, td_path / "kaggle_train.ipynb")

        # dataset metadata
        meta = {
            "title": DATASET_TITLE,
            "id": f"{username}/{DATASET_SLUG}",
            "licenses": [{"name": "CC0-1.0"}],
        }
        (td_path / "dataset-metadata.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )

        # 用 kaggle CLI 上傳
        # 第一次：datasets create -p .   往後：datasets version -p . -m "..."
        # 先試 version；fail 再 create
        print(f"[INFO] 嘗試 version-bump 既有 dataset {meta['id']} ...")
        rc = subprocess.call(
            [sys.executable, "-m", "kaggle", "datasets", "version",
             "-p", str(td_path), "-m", f"update {n_real} pairs", "--dir-mode", "zip"],
            cwd=td_path,
        )
        if rc != 0:
            print(f"[INFO] version 失敗（可能是首次），改試 create ...")
            rc = subprocess.call(
                [sys.executable, "-m", "kaggle", "datasets", "create",
                 "-p", str(td_path), "--dir-mode", "zip"],
                cwd=td_path,
            )
        if rc != 0:
            print(f"[ERR] kaggle dataset upload 失敗 (rc={rc})")
            return 5

    print(f"[OK] 已上傳 → https://www.kaggle.com/datasets/{username}/{DATASET_SLUG}")
    print()
    print("[NEXT] 1. 開 https://www.kaggle.com/code → New Notebook")
    print("       2. Settings → Accelerator → GPU P100（每週 30hr 免費）")
    print("       3. Add Data → 找剛上傳的 linebot-distilled")
    print("       4. File → Import Notebook → 選本地 finetune/kaggle_train.ipynb")
    print("       5. Run All（首次 ~2-3 hr）")
    print("       6. 跑完 publish notebook → 跑 kaggle_download_adapter.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
