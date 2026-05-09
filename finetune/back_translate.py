"""Back-translation augmentation 中→英→中 — 局部 LLM 微改寫資料集 prompt。

策略：
  - 走 local_llm.chat（Qwen2.5-14B 等）兩段翻譯：中→英→中。
  - 每輪 sampling 不同 → 同一句可產生多個變體。
  - 過濾：跟原句完全一樣 / char-overlap < 0.3 視為失敗（前者沒 augment 到，
    後者語意飄）。

跟 paraphrase 互補：
  - paraphrase 變句法（同語言內 rewrite，保 nuance）
  - back-translate 變表達（過英文中介，句式變化大但容易丟語氣詞）
  - 建議組合 paraphrase n=1 + bt n=2，過多反而劣化。

CLI:
    python finetune/back_translate.py --in train.jsonl --out train_bt.jsonl --n 2 \
        [--max-pairs 100] [--dry-run]

100% 本機；不 commit / 不 push / 不動 main.py。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

HERE = Path(__file__).resolve().parent
LINE_BOT_ROOT = HERE.parent
if str(LINE_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(LINE_BOT_ROOT))

logger = logging.getLogger("back_translate")

# 過濾門檻：char-set overlap 比例（Jaccard-ish over CJK 字元）
MIN_CHAR_OVERLAP = 0.3
# 預設每對 prompt 估算翻譯耗時（秒）— 14B 跑 中→英→中 兩段
DEFAULT_SEC_PER_PAIR_PER_PASS = 10.0


# ── prompts ────────────────────────────────────────────────────────────────
_PROMPT_ZH_TO_EN = (
    "Translate the following Chinese to natural English. "
    "Output only the translation, no prefix.\n"
    "Chinese: {text}\n"
    "English:"
)

_PROMPT_EN_TO_ZH = (
    "將下面英文翻譯成自然的繁體中文。直接給譯文，不要前綴。\n"
    "English: {text}\n"
    "繁體中文："
)


# ── translation primitives ─────────────────────────────────────────────────
def _strip_prefix(out: str, label: str) -> str:
    """LLM 偶爾仍會吐 'English: ...' / '繁體中文：...'，剝掉。"""
    s = (out or "").strip()
    if not s:
        return s
    # remove leading label like "English:" / "繁體中文:" / "中文："
    for needle in (label + ":", label + "："):
        if s.startswith(needle):
            s = s[len(needle):].lstrip()
            break
    # 去開頭 quote
    if (s.startswith("\"") and s.endswith("\"")) or (s.startswith("「") and s.endswith("」")):
        s = s[1:-1].strip()
    return s


def translate_zh_to_en(text: str) -> Optional[str]:
    """中→英；失敗（local_llm 沒載 / 空 / 例外）回 None。"""
    if not text or not text.strip():
        return None
    try:
        import local_llm  # type: ignore
    except Exception as e:
        logger.warning("import local_llm failed: %s", e)
        return None
    try:
        out = local_llm.chat(
            _PROMPT_ZH_TO_EN.format(text=text.strip()),
            system_prompt="You are a precise bilingual translator. Output only the translation.",
            max_tokens=400,
        )
    except Exception as e:
        logger.warning("zh->en translate failed: %s", e)
        return None
    if not out:
        return None
    return _strip_prefix(out, "English") or None


def translate_en_to_zh(text: str) -> Optional[str]:
    """英→繁中；失敗回 None。"""
    if not text or not text.strip():
        return None
    try:
        import local_llm  # type: ignore
    except Exception as e:
        logger.warning("import local_llm failed: %s", e)
        return None
    try:
        out = local_llm.chat(
            _PROMPT_EN_TO_ZH.format(text=text.strip()),
            system_prompt="你是專業的中英翻譯員，輸出純譯文，不加前綴或說明。",
            max_tokens=400,
        )
    except Exception as e:
        logger.warning("en->zh translate failed: %s", e)
        return None
    if not out:
        return None
    return _strip_prefix(out, "繁體中文") or None


# ── filter helpers ─────────────────────────────────────────────────────────
def _char_overlap_ratio(a: str, b: str) -> float:
    """Jaccard char overlap on CJK + alpha chars。0~1，1 表完全同字集。

    用 char-set 而非 sequence ratio：句法可變但用字差太多就濾掉。
    """
    sa = {c for c in (a or "") if c.strip() and not c.isspace()}
    sb = {c for c in (b or "") if c.strip() and not c.isspace()}
    if not sa or not sb:
        return 0.0
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union) if union else 0.0


def _is_valid_variant(orig: str, variant: str) -> bool:
    if not variant or not variant.strip():
        return False
    if variant.strip() == (orig or "").strip():
        return False  # 完全一樣 → 沒 augment
    if _char_overlap_ratio(orig, variant) < MIN_CHAR_OVERLAP:
        return False  # 語意飄太遠
    return True


# ── core ───────────────────────────────────────────────────────────────────
def back_translate(text: str, n_passes: int = 1) -> list[str]:
    """跑 n_passes 輪 中→英→中。

    每輪受 sampling 影響可能不同 → 累積去重變體 list。
    過濾：原句 / overlap<0.3 → 丟掉。
    """
    if not text or not text.strip() or n_passes <= 0:
        return []
    seen: set[str] = set()
    variants: list[str] = []
    for _ in range(n_passes):
        en = translate_zh_to_en(text)
        if not en:
            continue
        zh = translate_en_to_zh(en)
        if not zh:
            continue
        zh = zh.strip()
        if zh in seen:
            continue
        if not _is_valid_variant(text, zh):
            continue
        seen.add(zh)
        variants.append(zh)
    return variants


def augment_pair_bt(
    prompt: str, completion: str, n: int = 2,
) -> list[tuple[str, str]]:
    """對 prompt back-translate n 次，completion 不動。

    回 [(原 prompt, 原 completion), (bt_1, 原 completion), ...]
    若 bt 全失敗 → 至少回原 pair。
    """
    out: list[tuple[str, str]] = [(prompt, completion)]
    if n <= 0 or not prompt:
        return out
    variants = back_translate(prompt, n_passes=n)
    for v in variants:
        out.append((v, completion))
    return out


# ── dataset-level ──────────────────────────────────────────────────────────
def _extract_pair(record: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """取 (prompt, completion) — 支援 messages or {prompt, completion}。"""
    if "messages" in record and isinstance(record["messages"], list):
        msgs = record["messages"]
        if len(msgs) < 2:
            return None, None
        u = msgs[0].get("content")
        b = msgs[1].get("content")
        return u, b
    return record.get("prompt"), record.get("completion")


def _wrap_pair(prompt: str, completion: str, base: dict[str, Any], augmented: bool) -> dict[str, Any]:
    """把 (p, c) 包成 messages-style record，標 source=back_translate (若是新增)。"""
    meta = dict((base.get("metadata") or {}))
    if augmented:
        meta["source"] = "back_translate"
        meta["augmented"] = True
    return {
        "messages": [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": completion},
        ],
        "metadata": meta,
    }


def augment_dataset_bt(
    input_jsonl: Path | str,
    output_jsonl: Path | str,
    n: int = 2,
    max_pairs: Optional[int] = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """讀 input_jsonl → 對每對跑 augment_pair_bt → 寫 output_jsonl。

    Stats:
      - input: 讀進幾筆
      - kept:  原 record 寫出幾筆（dry_run 時也算）
      - added: bt 多出幾筆變體
      - failed: bt 全失敗（沒任何 valid variant）的 prompt 數
      - eta_sec: dry_run 預估秒數
    """
    in_path = Path(input_jsonl)
    out_path = Path(output_jsonl)

    records: list[dict[str, Any]] = []
    if in_path.exists():
        with in_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("skip malformed line in %s", in_path)

    if max_pairs is not None and max_pairs >= 0:
        records = records[:max_pairs]

    stats = {
        "input": len(records),
        "kept": 0,
        "added": 0,
        "failed": 0,
        "eta_sec": 0,
    }

    if dry_run:
        # 預估：每 pair n 輪 × 一輪 ~10 sec → 估算總時長
        stats["eta_sec"] = int(len(records) * n * DEFAULT_SEC_PER_PAIR_PER_PASS)
        # 預設假設 kept=input，added=input*n（樂觀，filter 之前）
        stats["kept"] = len(records)
        stats["added"] = len(records) * max(0, n)
        return stats

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for rec in records:
            prompt, completion = _extract_pair(rec)
            if not prompt or not completion:
                continue
            pairs = augment_pair_bt(prompt, completion, n=n)
            # pairs[0] 是原 pair；後面是 augment
            for idx, (p, c) in enumerate(pairs):
                aug = idx > 0
                f.write(json.dumps(_wrap_pair(p, c, rec, aug), ensure_ascii=False) + "\n")
                if aug:
                    stats["added"] += 1
                else:
                    stats["kept"] += 1
            if len(pairs) == 1 and n > 0:
                stats["failed"] += 1
    return stats


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="in_path", required=True,
                    help="input JSONL")
    ap.add_argument("--out", dest="out_path", required=True,
                    help="output JSONL")
    ap.add_argument("--n", type=int, default=2,
                    help="back-translation passes per prompt (default 2)")
    ap.add_argument("--max-pairs", type=int, default=None,
                    help="limit to first N pairs (debug)")
    ap.add_argument("--dry-run", action="store_true",
                    help="不真跑翻譯，只印估算耗時")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    t0 = time.time()
    stats = augment_dataset_bt(
        args.in_path, args.out_path,
        n=args.n,
        max_pairs=args.max_pairs,
        dry_run=args.dry_run,
    )
    dt = time.time() - t0

    if args.dry_run:
        eta_min = stats["eta_sec"] / 60.0
        print(
            f"[dry-run] input={stats['input']} n={args.n} "
            f"→ ~+{stats['added']} variants, "
            f"ETA ~{stats['eta_sec']}s ({eta_min:.1f} min) at "
            f"~{DEFAULT_SEC_PER_PAIR_PER_PASS:.0f}s/pass on local 14B"
        )
    else:
        print(
            f"wrote: kept={stats['kept']} added={stats['added']} "
            f"failed={stats['failed']} (took {dt:.1f}s)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
