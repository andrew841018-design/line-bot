"""Open-source Chinese chat corpus injection for LINE-bot fine-tuning.

Mix a small ratio (5-15%) of generic Chinese instruction/chat corpus into the
personal-data fine-tune set so the model keeps general Chinese fluency without
overfitting solely to Andrew/咪寶 style.

Workflow:
  1. `download_corpus(name, size)` — pull from HuggingFace Hub (one-shot,
     cached locally). Filters out instruction/QA-only entries, normalizes to
     traditional Chinese with OpenCC s2t, drops too-short / too-long.
  2. `filter_for_chat_style(rows)` — keep only conversational entries that
     read like real chat ("為什麼 / 怎麼樣 / 我想 / 我覺得" markers), drop
     pure 「請寫一首詩」「翻譯下面句子」command-style instructions.
  3. `merge_into_dataset(corpus_path, ratio)` — sample `personal_count * ratio`
     entries and write to `finetune/data/open_corpus_sample.jsonl`.

Default corpus: `BelleGroup/train_2M_CN`
  - 200 万条中文 SFT，规模够大 → 容易抽到 chat-style 子集
  - 簡繁混雜 → 用 OpenCC s2t 強制轉繁
  - 主要為單輪 instruction，需 filter_for_chat_style 篩出對話型

Alternatives (CLI `--corpus` switch):
  - `shareAI/ShareGPT-Chinese-English-90k` — 90k chat 多輪對話，質量較高但量小
  - `m-a-p/COIG-CQIA` — 中文質量 instruction，知乎/小红书等社群風格
  - `BAAI/COIG` — 中文通用

CLI:
    python finetune/open_corpus.py --download --size 2000 --filter
    python finetune/open_corpus.py --download --dry-run        # 不真下載

Privacy / cost:
  - HF cache 落地 `~/.cache/huggingface/datasets/`
  - dry-run 只印 metadata，不打雲端
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LINE_BOT_ROOT = HERE.parent
if str(LINE_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(LINE_BOT_ROOT))

logger = logging.getLogger("open_corpus")

DEFAULT_DATA_DIR = HERE / "data"
DEFAULT_CORPUS_OUT = DEFAULT_DATA_DIR / "open_corpus_sample.jsonl"
DEFAULT_CORPUS_NAME = "BelleGroup/train_2M_CN"
DEFAULT_SAMPLE_SIZE = 2000

MIN_LEN = 30
MAX_LEN = 1000

# 對話型保留標記：含這類詞的 instruction 比較像真人在問問題 / 表達看法
_CHAT_MARKERS = (
    "為什麼", "为什么", "怎麼樣", "怎么样", "怎麼辦", "怎么办",
    "我想", "我覺得", "我觉得", "我認為", "我认为",
    "你覺得", "你觉得", "你認為", "你认为",
    "如何看", "你怎麼看", "你怎么看",
    "請問", "请问", "可以分享", "可以說說", "可以说说",
    "對吧", "对吧", "是不是", "是吧",
    "幫我看看", "帮我看看", "推薦", "推荐",
    "想問", "想问", "求建議", "求建议",
)

# 純功能型指令的關鍵字 → 排除
_INSTRUCTION_PREFIXES = (
    "請寫", "请写", "請翻譯", "请翻译",
    "請生成", "请生成", "請產生", "请产生", "請列出", "请列出",
    "請輸出", "请输出", "請給出", "请给出",
    "請計算", "请计算", "請編寫", "请编写",
    "請根據", "请根据",
    "翻譯下面", "翻译下面", "翻譯以下", "翻译以下",
    "改寫下", "改写下", "改寫以下", "改写以下",
    "總結下", "总结下", "總結以下", "总结以下",
    "歸納下", "归纳下",
    "編寫一段", "编写一段",
    "用 Python", "用 python", "寫一個 Python", "写一个 python",
    "回答下面", "回答以下", "解釋下", "解释下",
    "Generate ", "Write ", "Translate ", "Summarize ",
)

# 程式碼/表格/數學專門題類 → 排除（與 chat style 偏離）
_TECHNICAL_MARKERS = (
    "```", "def ", "function(", "SELECT ", "select ",
    "公式", "矩陣", "矩阵", "方程式", "求導", "求导",
    "演算法", "算法",
)


# ── HF download ─────────────────────────────────────────────────────────────
def _hf_load(name: str, split: str = "train"):
    """Lazy import datasets so test-time mock can replace it cheaply."""
    from datasets import load_dataset  # type: ignore[import-not-found]

    return load_dataset(name, split=split)


def _normalize_entry(entry: dict[str, Any]) -> tuple[str, str] | None:
    """Pull (prompt, completion) out of a HF entry — schemas vary by dataset."""
    # BelleGroup: instruction / input / output  OR  conversations[]
    if "conversations" in entry and isinstance(entry["conversations"], list):
        msgs = entry["conversations"]
        if len(msgs) >= 2:
            u = msgs[0].get("value") or msgs[0].get("content") or ""
            b = msgs[1].get("value") or msgs[1].get("content") or ""
            if u and b:
                return u.strip(), b.strip()
    if "instruction" in entry:
        instr = (entry.get("instruction") or "").strip()
        inp = (entry.get("input") or "").strip()
        out = (entry.get("output") or entry.get("response") or "").strip()
        prompt = (instr + ("\n" + inp if inp else "")).strip()
        if prompt and out:
            return prompt, out
    if "prompt" in entry and ("completion" in entry or "response" in entry):
        p = (entry.get("prompt") or "").strip()
        c = (entry.get("completion") or entry.get("response") or "").strip()
        if p and c:
            return p, c
    if "question" in entry and "answer" in entry:
        return (entry["question"] or "").strip(), (entry["answer"] or "").strip()
    return None


def _to_traditional(text: str) -> str:
    """Convert simplified Chinese to traditional via OpenCC s2t.

    Returns input unchanged if OpenCC import fails.
    """
    try:
        from opencc import OpenCC  # type: ignore[import-not-found]

        cc = OpenCC("s2t")
        return cc.convert(text)
    except Exception as e:
        logger.debug("OpenCC failed: %s", e)
        return text


def _length_ok(prompt: str, completion: str) -> bool:
    total = len(prompt) + len(completion)
    return MIN_LEN <= total <= MAX_LEN


def download_corpus(
    name: str = DEFAULT_CORPUS_NAME,
    size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = 42,
    *,
    dry_run: bool = False,
) -> list[dict[str, str]]:
    """Download `name` from HF Hub, sample `size` rows, normalize to繁中.

    Returns: list of {prompt, completion} dicts (traditional Chinese, length-OK).
    On dry_run=True, returns [] without touching network.
    """
    if dry_run:
        logger.info("[dry-run] corpus=%s size=%d (no network call)", name, size)
        return []

    logger.info("loading HF dataset %s ...", name)
    ds = _hf_load(name)
    total = len(ds) if hasattr(ds, "__len__") else size * 4
    logger.info("HF dataset rows=%s, sampling %d", total, size)

    rng = random.Random(seed)
    indices = (
        rng.sample(range(total), min(size * 3, total))
        if total > 0
        else list(range(min(size * 3, len(ds))))
    )

    out: list[dict[str, str]] = []
    for idx in indices:
        if len(out) >= size:
            break
        try:
            entry = ds[int(idx)]
        except (IndexError, KeyError):
            continue
        norm = _normalize_entry(entry)
        if not norm:
            continue
        prompt, completion = norm
        # 簡 → 繁
        prompt = _to_traditional(prompt)
        completion = _to_traditional(completion)
        if not _length_ok(prompt, completion):
            continue
        out.append({"prompt": prompt, "completion": completion})

    logger.info("downloaded+normalized: %d rows", len(out))
    return out


# ── chat-style filter ──────────────────────────────────────────────────────
def _looks_like_instruction(text: str) -> bool:
    """純功能型指令（請翻譯/請生成/code）→ True 表示要排除。"""
    head = text[:30]
    for pref in _INSTRUCTION_PREFIXES:
        if head.startswith(pref):
            return True
    for marker in _TECHNICAL_MARKERS:
        if marker in text:
            return True
    return False


def _has_chat_markers(text: str) -> bool:
    return any(m in text for m in _CHAT_MARKERS)


def _question_or_opinion(text: str) -> bool:
    """Heuristic: ends in question mark or contains opinion words."""
    if re.search(r"[？?]$", text.strip()):
        return True
    if _has_chat_markers(text):
        return True
    return False


def filter_for_chat_style(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep conversational entries; drop pure instruction/QA functional types."""
    kept: list[dict[str, str]] = []
    for r in rows:
        p = r.get("prompt", "")
        c = r.get("completion", "")
        if not p or not c:
            continue
        if _looks_like_instruction(p):
            continue
        if not _question_or_opinion(p):
            continue
        kept.append(r)
    logger.info("chat-style filter: kept %d / %d", len(kept), len(rows))
    return kept


# ── merge into personal dataset ─────────────────────────────────────────────
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


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def _slug_from_corpus_name(name: str) -> str:
    """`BelleGroup/train_2M_CN` → `belle`, `shareAI/ShareGPT-...` → `sharegpt`."""
    low = name.lower()
    if "belle" in low:
        return "open_corpus_belle"
    if "sharegpt" in low or "shareai" in low:
        return "open_corpus_sharegpt"
    if "coig" in low:
        return "open_corpus_coig"
    return "open_corpus_generic"


def compute_target_size(personal_count: int, ratio: float) -> int:
    """corpus_n = round(personal_count * ratio).

    e.g. personal=1000, ratio=0.15 → 150
    """
    if personal_count <= 0:
        return 0
    if ratio <= 0:
        return 0
    return max(1, int(round(personal_count * ratio)))


def merge_into_dataset(
    corpus_rows: list[dict[str, str]],
    personal_count: int,
    ratio: float = 0.15,
    out_path: Path | str = DEFAULT_CORPUS_OUT,
    source_label: str = "open_corpus_belle",
    seed: int = 42,
) -> dict[str, Any]:
    """Sample `personal_count * ratio` from corpus_rows and write canonical jsonl.

    Output schema (matches `dataset_builder._to_canonical`):
        {messages: [{role:user, ...}, {role:assistant, ...}],
         metadata: {source: source_label, pair_hash: ...}}
    """
    out_path = Path(out_path)
    target = compute_target_size(personal_count, ratio)
    if target == 0 or not corpus_rows:
        logger.info("merge skipped (target=%d, rows=%d)", target, len(corpus_rows))
        _write_jsonl(out_path, [])
        return {"target": target, "written": 0, "ratio": ratio}

    rng = random.Random(seed)
    sample = (
        rng.sample(corpus_rows, target)
        if len(corpus_rows) > target
        else list(corpus_rows)
    )

    records: list[dict[str, Any]] = []
    for r in sample:
        records.append(
            {
                "messages": [
                    {"role": "user", "content": r["prompt"]},
                    {"role": "assistant", "content": r["completion"]},
                ],
                "metadata": {"source": source_label},
            }
        )
    n_written = _write_jsonl(out_path, records)
    logger.info(
        "merge: target=%d written=%d ratio=%.3f → %s",
        target,
        n_written,
        ratio,
        out_path,
    )
    return {
        "target": target,
        "written": n_written,
        "ratio": ratio,
        "out_path": str(out_path),
    }


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--download", action="store_true",
                    help="run HF download + normalize")
    ap.add_argument("--corpus", default=DEFAULT_CORPUS_NAME,
                    help=f"HF dataset name (default: {DEFAULT_CORPUS_NAME})")
    ap.add_argument("--size", type=int, default=DEFAULT_SAMPLE_SIZE,
                    help=f"max rows to keep (default: {DEFAULT_SAMPLE_SIZE})")
    ap.add_argument("--filter", action="store_true",
                    help="apply chat-style filter")
    ap.add_argument("--dry-run", action="store_true",
                    help="print metadata only, no network call")
    ap.add_argument("--out", default=str(DEFAULT_CORPUS_OUT),
                    help="output jsonl path")
    ap.add_argument("--ratio", type=float, default=0.15,
                    help="mix-in ratio vs personal data (default 0.15)")
    ap.add_argument("--personal-count", type=int, default=0,
                    help="personal-data pair count (for ratio compute, "
                         "if 0 + --download → just save raw download)")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    if not args.download:
        ap.print_help()
        return 1

    if args.dry_run:
        print(f"[dry-run] corpus={args.corpus}")
        print(f"[dry-run] size={args.size}")
        print(f"[dry-run] ratio={args.ratio}")
        print(f"[dry-run] out={args.out}")
        print(f"[dry-run] source_label={_slug_from_corpus_name(args.corpus)}")
        print("[dry-run] no network call. exit.")
        return 0

    rows = download_corpus(name=args.corpus, size=args.size, dry_run=False)
    print(f"下載 {args.size} 條，過濾後剩 {len(rows)} 條，繁中已轉換")

    if args.filter:
        rows = filter_for_chat_style(rows)
        print(f"chat-style filter 後剩 {len(rows)} 條")

    if args.personal_count > 0:
        stats = merge_into_dataset(
            rows,
            personal_count=args.personal_count,
            ratio=args.ratio,
            out_path=Path(args.out),
            source_label=_slug_from_corpus_name(args.corpus),
        )
        print(
            f"混入: target={stats['target']} written={stats['written']} "
            f"ratio={stats['ratio']} → {stats.get('out_path')}"
        )
    else:
        # 直接全量寫出（給後續 dataset_builder 自己決定 ratio）
        records = [
            {
                "messages": [
                    {"role": "user", "content": r["prompt"]},
                    {"role": "assistant", "content": r["completion"]},
                ],
                "metadata": {"source": _slug_from_corpus_name(args.corpus)},
            }
            for r in rows
        ]
        n = _write_jsonl(Path(args.out), records)
        print(f"raw 全量寫出 {n} 條 → {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
