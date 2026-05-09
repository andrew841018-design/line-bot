"""Paraphrase augmentation for finetune dataset — 100% local Qwen2.5-14B.

每對 (prompt, completion) 用 local LLM 把 prompt 改寫 N 次同義版（保持 completion
不變），把資料量乘 N。這是 enrichment 不是 replacement —— 原 train.jsonl 不動。

Pipeline:
  1. `paraphrase(text, n)` → 餵 prompt 給 local_llm.chat → parse N 行
     - char-overlap < 0.3 → 視為 hallucination 剔除
     - 失敗 / 解析錯誤 → 回 [] 不阻塞主流程
  2. `augment_pair(p, c, n)` → 對 prompt 改寫 N 次（completion 不動）
     - 回 [(原 p, 原 c), (變 p_1, 原 c), ..., (變 p_n, 原 c)]
  3. `augment_dataset(in_jsonl, out_jsonl, n, max_pairs)` → 串起來

CLI:
    python finetune/paraphrase_aug.py --in finetune/data/train.jsonl \
        --out finetune/data/train_aug.jsonl --n 5 [--max-pairs 100]
    python finetune/paraphrase_aug.py --in ... --out ... --n 5 --dry-run

設計理由：
  - 只改 prompt 不改 completion → user 同問題不同問法 → robustness ↑
  - completion 改寫怕語氣 / 風格漂移（咪寶人格漂掉）
  - char-overlap 過濾避免 LLM 自由發揮把意思改掉（hallucination）
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
LINE_BOT_ROOT = HERE.parent
if str(LINE_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(LINE_BOT_ROOT))

logger = logging.getLogger("paraphrase_aug")

# 跟原句太不像（hallucination）的閾值。char-overlap < 此值 → 剔除。
CHAR_OVERLAP_THRESHOLD = 0.3

# 14B 在 M2 Pro 32GB 平均 5 sec/pair（dry-run 估算用）
SECONDS_PER_PAIR_EST = 5.0


# ─── prompt template ─────────────────────────────────────────────────────────
def _build_prompt(text: str, n: int) -> str:
    """組 paraphrase 指令給 local LLM。N 行同義改寫，無編號無前綴。"""
    return (
        f"下面這句中文，用 {n} 種不同說法改寫，意思保持一致，語氣可以稍微變化。\n"
        f"直接給 {n} 行改寫結果（用 \\n 分），不要編號、不要前綴。\n"
        f"原句：「{text}」"
    )


# ─── helpers ─────────────────────────────────────────────────────────────────
def _char_overlap(a: str, b: str) -> float:
    """以 char-set Jaccard 衡量 a, b 重疊度。0=完全不同, 1=完全相同 set。

    用 set 而非 multiset，因為改寫常會調整字頻但保留主要詞彙。
    """
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = sa & sb
    union = sa | sb
    if not union:
        return 0.0
    return len(inter) / len(union)


def _parse_variants(raw: str, n: int) -> list[str]:
    """從 LLM raw output 抽 N 行變體。

    處理常見髒污：
      - 編號前綴（"1. ...", "2) ...", "1、..."）
      - 引號（"「...」", "「..."', '"..."'）
      - 空行 / 前後空白
    抽不滿 N 個就回實際抽到的（caller 不要硬要 N 個）。
    """
    if not raw:
        return []
    out: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 剝編號前綴：1. / 1) / 1、 / 1・
        for sep in (". ", ") ", "、", ".", ")"):
            if len(line) >= 2 and line[0].isdigit():
                # 找第一個數字 prefix
                idx = 0
                while idx < len(line) and line[idx].isdigit():
                    idx += 1
                if idx > 0 and line[idx:idx + len(sep)] == sep:
                    line = line[idx + len(sep):].strip()
                    break
        # 剝中文標點引號
        line = line.strip("「」\"'“”‘’")
        # 剝可能殘留的「原句：」「改寫：」
        for prefix in ("原句：", "改寫：", "改寫", "原句"):
            if line.startswith(prefix):
                line = line[len(prefix):].lstrip("：: ").strip()
        if line:
            out.append(line)
        if len(out) >= n:
            break
    return out


# ─── core API ────────────────────────────────────────────────────────────────
def paraphrase(
    text: str,
    n: int = 5,
    model: str = "local",
    chat_fn: Callable[..., str | None] | None = None,
) -> list[str]:
    """把 text 改寫 N 次同義版。失敗回 []（不阻塞）。

    Args:
        text: 原句（中文）
        n: 改寫次數
        model: 'local' 用 local_llm.chat；其他值保留給未來擴充（雲端 fallback）
        chat_fn: 注入式 LLM 介面（測試用，預設 lazy import local_llm.chat）

    Returns:
        list of paraphrased strings（可能少於 n，最多 n 個）
        char-overlap < CHAR_OVERLAP_THRESHOLD 的會被剔除（hallucination guard）
    """
    if not text or not text.strip():
        return []
    if n <= 0:
        return []

    # 預設用 local_llm.chat（lazy import 避免 mlx 在無模型機器卡住）
    if chat_fn is None:
        try:
            from local_llm import chat as _local_chat
            chat_fn = _local_chat
        except Exception as e:
            logger.warning("local_llm.chat unavailable: %s", e)
            return []

    prompt = _build_prompt(text, n)
    try:
        # system_prompt 留空（這個任務不需要咪寶人格）
        raw = chat_fn(
            prompt,
            context=None,
            system_prompt="你是中文改寫助手，按指示輸出 N 行同義句，不解釋。",
            max_tokens=400,
        )
    except Exception as e:
        logger.warning("paraphrase chat failed: %s", e)
        return []

    if not raw:
        return []

    variants = _parse_variants(raw, n)
    # 過濾跟原句太不像的（hallucination guard）
    filtered = [
        v for v in variants
        if v != text and _char_overlap(v, text) >= CHAR_OVERLAP_THRESHOLD
    ]
    return filtered[:n]


def augment_pair(
    prompt: str,
    completion: str,
    n: int = 5,
    chat_fn: Callable[..., str | None] | None = None,
) -> list[tuple[str, str]]:
    """對 prompt 改寫 N 次，completion 不動。

    Returns:
        [(原 p, 原 c), (變 p_1, 原 c), ..., (變 p_k, 原 c)]
        其中 k <= n（過濾後可能少於 n）。
        原 pair 永遠在第一個位置（保證原資料不丟）。
    """
    out: list[tuple[str, str]] = [(prompt, completion)]
    variants = paraphrase(prompt, n=n, chat_fn=chat_fn)
    for v in variants:
        out.append((v, completion))
    return out


# ─── jsonl IO ────────────────────────────────────────────────────────────────
def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("skip malformed line in %s", path)
    return out


def _extract_pc(rec: dict[str, Any]) -> tuple[str, str] | None:
    """從 dataset_builder canonical 格式抽 (prompt, completion)。"""
    msgs = rec.get("messages")
    if not isinstance(msgs, list) or len(msgs) < 2:
        return None
    p = msgs[0].get("content", "")
    c = msgs[1].get("content", "")
    if not p or not c:
        return None
    return p, c


def _to_record(prompt: str, completion: str, src_meta: dict[str, Any],
               is_augmented: bool) -> dict[str, Any]:
    """打包成 train.jsonl 同 schema。is_augmented=True 在 metadata 標記。"""
    meta = dict(src_meta or {})
    if is_augmented:
        meta["augmented"] = True
        meta["augment_method"] = "paraphrase_local"
        # 改寫版的 pair_hash 重算（避免跟原 hash 撞）
        import hashlib
        meta["pair_hash"] = hashlib.sha256(
            f"{prompt}│{completion}".encode("utf-8")
        ).hexdigest()
    return {
        "messages": [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": completion},
        ],
        "metadata": meta,
    }


def augment_dataset(
    input_jsonl: Path | str,
    output_jsonl: Path | str,
    n: int = 5,
    max_pairs: int | None = None,
    chat_fn: Callable[..., str | None] | None = None,
    progress: bool = True,
) -> dict[str, int]:
    """讀 input_jsonl，每對 augment_pair，寫到 output_jsonl。

    Args:
        input_jsonl: 原始 train.jsonl
        output_jsonl: 輸出 train_aug.jsonl
        n: 每對改寫幾次
        max_pairs: 限制處理對數（避免 10000 對跑很久），None=全部
        chat_fn: 注入 LLM（測試用）
        progress: 印進度條

    Returns:
        {"input": 原 pair 數, "processed": 實處理數, "output": 寫出 record 數}
    """
    input_jsonl = Path(input_jsonl)
    output_jsonl = Path(output_jsonl)
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)

    records = _read_jsonl(input_jsonl)
    n_input = len(records)

    if max_pairs is not None and max_pairs < n_input:
        records = records[:max_pairs]
    n_processed = len(records)

    # 進度條：tqdm 在則用，沒在就 print
    iterator: Any
    if progress and n_processed > 0:
        try:
            from tqdm import tqdm
            iterator = tqdm(records, desc="paraphrase", unit="pair")
        except ImportError:
            iterator = records
    else:
        iterator = records

    n_out = 0
    with output_jsonl.open("w", encoding="utf-8") as f:
        for i, rec in enumerate(iterator):
            pc = _extract_pc(rec)
            if pc is None:
                continue
            p, c = pc
            src_meta = rec.get("metadata", {})

            pairs = augment_pair(p, c, n=n, chat_fn=chat_fn)
            for j, (pp, cc) in enumerate(pairs):
                is_aug = j > 0  # 第 0 個是原 pair
                out_rec = _to_record(pp, cc, src_meta, is_augmented=is_aug)
                f.write(json.dumps(out_rec, ensure_ascii=False) + "\n")
                n_out += 1

            # 沒 tqdm 時手寫 print 進度
            if progress and not _has_tqdm():
                if (i + 1) % 5 == 0 or (i + 1) == n_processed:
                    print(
                        f"  [paraphrase] {i + 1}/{n_processed} pairs "
                        f"→ {n_out} records",
                        flush=True,
                    )

    return {
        "input": n_input,
        "processed": n_processed,
        "output": n_out,
    }


def _has_tqdm() -> bool:
    try:
        import tqdm  # noqa: F401
        return True
    except ImportError:
        return False


# ─── dry-run estimate ────────────────────────────────────────────────────────
def estimate_runtime(input_jsonl: Path | str, n: int,
                     max_pairs: int | None = None) -> dict[str, Any]:
    """估算 14B 跑這個 dataset 要多久。"""
    input_jsonl = Path(input_jsonl)
    records = _read_jsonl(input_jsonl)
    n_input = len(records)
    n_proc = min(n_input, max_pairs) if max_pairs else n_input
    # 變體總數（不含原句），對應 dry-run 輸出 "Nx = M"
    n_variants = n_proc * n
    # 實際寫出的 record 數：原 + 變體
    n_records = n_proc * (n + 1)
    seconds = n_proc * SECONDS_PER_PAIR_EST
    minutes = seconds / 60.0
    return {
        "input_pairs": n_input,
        "processed_pairs": n_proc,
        "n": n,
        "n_variants": n_variants,
        "expected_output": n_records,
        "seconds_per_pair": SECONDS_PER_PAIR_EST,
        "total_seconds": seconds,
        "total_minutes": minutes,
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Paraphrase augmentation — 14B 本機把每對 prompt 改寫 N 次"
    )
    ap.add_argument("--in", dest="input", required=True,
                    help="input jsonl (e.g. finetune/data/train.jsonl)")
    ap.add_argument("--out", dest="output", required=True,
                    help="output jsonl (e.g. finetune/data/train_aug.jsonl)")
    ap.add_argument("--n", type=int, default=5, help="改寫次數（預設 5）")
    ap.add_argument("--max-pairs", type=int, default=None,
                    help="限制處理對數（避免太多對跑很久）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只估算時間，不真跑 LLM")
    ap.add_argument("--no-progress", action="store_true",
                    help="關閉進度條")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    if args.dry_run:
        est = estimate_runtime(args.input, args.n, args.max_pairs)
        # 格式："X 對 → Nx = Y 對，14B 平均 5 sec/pair → ~Z 分鐘"
        # Y = N × X（純變體，不含原），這樣比較直觀「資料變幾倍」
        minutes_disp = max(1, round(est["total_minutes"]))
        print(
            f"估算 {est['processed_pairs']} 對 → "
            f"{est['n']}x = {est['n_variants']} 對，"
            f"14B 平均 {est['seconds_per_pair']:.0f} sec/pair → "
            f"~{minutes_disp} 分鐘"
        )
        return 0

    t0 = time.time()
    stats = augment_dataset(
        args.input, args.output, n=args.n,
        max_pairs=args.max_pairs,
        progress=not args.no_progress,
    )
    elapsed = time.time() - t0
    print(
        f"done: {stats['input']} input pairs → "
        f"{stats['processed']} processed → "
        f"{stats['output']} records "
        f"({elapsed:.1f}s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
