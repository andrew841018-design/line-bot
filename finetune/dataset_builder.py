"""Build train/val/test JSONL splits from organic LINE history + distilled data.

Pipeline:
  1. Extract organic pairs from line_bot.db (`extract_line_history.extract_all_pairs`)
  2. Extract correction-augmented pairs (high priority)
  3. Auto-scan ~/Desktop ~/Documents ~/Downloads for `[LINE]*.txt` exports and
     extract Andrew→咪寶 pairs (`parse_line_export.find_all_exports` +
     `extract_pairs_from_export`)
  4. Read existing distilled data from `finetune/data/distilled.jsonl`
  5. Read self-distilled notes from `finetune/data/notes_distilled.jsonl`
  6. Read line-self-distilled (Gemini 對 30k 家人訊息蒸的「咪寶會怎麼接」)
     from `finetune/data/line_self_distilled.jsonl[.enc]`
  7. (Optional) Mix in open-source Chinese chat corpus as a low-priority base
     (`finetune/data/open_corpus_sample.jsonl`), capped at
     `personal_count * corpus_ratio` (default 15%, recommended 10-15%).
  8. Merge + dedup (SHA256 over prompt+completion)
     priority:
       corrections > organic > line_export > notes_distilled >
       line_self_distill > sft > distilled > open_corpus
     (line_self_distill 比 sft 真實 — 來源是 30k 真實家人 LINE 訊息）
  9. Deterministic shuffle (hash-sorted) → 80/10/10 split
  10. Write `train.jsonl`, `val.jsonl`, `test.jsonl` under `finetune/data/`

Privacy:
  - group_id is SHA256-hashed (truncated 16 hex)
  - line_export group_name is also SHA256-hashed before being written
  - No user_id / display name preserved

CLI:
    python finetune/dataset_builder.py --build       # full pipeline (incl. exports)
    python finetune/dataset_builder.py --stats       # db-only stats, no file writes
    python finetune/dataset_builder.py --build --db <path> --out-dir <dir>
    python finetune/dataset_builder.py --build --no-exports   # skip [LINE]*.txt scan
    python finetune/dataset_builder.py --build --with-corpus 200 --corpus-ratio 0.15
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
LINE_BOT_ROOT = HERE.parent
if str(LINE_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(LINE_BOT_ROOT))

from finetune.extract_line_history import (  # noqa: E402
    extract_all_pairs,
    extract_with_corrections,
)
from finetune.parse_line_export import (  # noqa: E402
    extract_pairs_from_export,
    find_all_exports,
)
from finetune.pii_masker import count_pii  # noqa: E402
from finetune.pii_storage import DEFAULT_DB as DEFAULT_PII_DB  # noqa: E402
from finetune.pii_storage import mask_with_cache  # noqa: E402

DEFAULT_DB = LINE_BOT_ROOT / "line_bot.db"
DEFAULT_DATA_DIR = HERE / "data"
DEFAULT_DISTILLED = DEFAULT_DATA_DIR / "distilled.jsonl"
DEFAULT_NOTES_DISTILLED = DEFAULT_DATA_DIR / "notes_distilled.jsonl"
DEFAULT_LINE_SELF_DISTILL = DEFAULT_DATA_DIR / "line_self_distilled.jsonl"
DEFAULT_LINE_SELF_DISTILL_ENC = DEFAULT_DATA_DIR / "line_self_distilled.jsonl.enc"
DEFAULT_SFT = DEFAULT_DATA_DIR / "sft.jsonl"
DEFAULT_OPEN_CORPUS = DEFAULT_DATA_DIR / "open_corpus_sample.jsonl"

TRAIN_RATIO = 0.80
VAL_RATIO = 0.10
TEST_RATIO = 0.10
assert abs(TRAIN_RATIO + VAL_RATIO + TEST_RATIO - 1.0) < 1e-9

logger = logging.getLogger("dataset_builder")


# ── helpers ──────────────────────────────────────────────────────────────────
def _pair_hash(prompt: str, completion: str) -> str:
    blob = f"{prompt}│{completion}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _to_canonical(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize input record into {messages, metadata} schema.

    Accepts:
      - {prompt, completion, group_id?, timestamp?, source?, ...}
      - {messages: [{role:user, content}, {role:assistant, content}], metadata?}
    Returns None for malformed entries.
    """
    if "messages" in entry and isinstance(entry["messages"], list):
        msgs = entry["messages"]
        if len(msgs) < 2:
            return None
        u = msgs[0].get("content", "")
        b = msgs[1].get("content", "")
        if not u or not b:
            return None
        meta = dict(entry.get("metadata") or {})
        meta.setdefault("source", "distilled")
        meta["pair_hash"] = _pair_hash(u, b)
        return {
            "messages": [
                {"role": "user", "content": u},
                {"role": "assistant", "content": b},
            ],
            "metadata": meta,
        }

    p = entry.get("prompt")
    c = entry.get("completion")
    if not p or not c:
        return None
    meta: dict[str, Any] = {
        "source": entry.get("source", "organic"),
        "pair_hash": entry.get("pair_hash") or _pair_hash(p, c),
    }
    if "group_id" in entry:
        meta["group_id"] = entry["group_id"]
    # line_export records carry plaintext `group_name` — hash it before writing
    if "group_name" in entry and entry["group_name"]:
        gn = str(entry["group_name"])
        meta["group_id"] = hashlib.sha256(gn.encode("utf-8")).hexdigest()[:16]
    if "timestamp" in entry:
        meta["timestamp"] = entry["timestamp"]
    if "priority" in entry:
        meta["priority"] = entry["priority"]
    return {
        "messages": [
            {"role": "user", "content": p},
            {"role": "assistant", "content": c},
        ],
        "metadata": meta,
    }


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


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


# ── core pipeline ────────────────────────────────────────────────────────────
def gather_records(
    db_path: Path | str = DEFAULT_DB,
    distilled_path: Path | str = DEFAULT_DISTILLED,
    include_distilled: bool = True,
    include_exports: bool = True,
    export_search_dirs: list[Path | str] | None = None,
    notes_distilled_path: Path | str = DEFAULT_NOTES_DISTILLED,
    include_notes: bool = True,
    sft_path: Path | str = DEFAULT_SFT,
    include_sft: bool = True,
    open_corpus_path: Path | str | None = None,
    open_corpus_ratio: float = 0.15,
    line_self_distill_path: Path | str = DEFAULT_LINE_SELF_DISTILL,
    line_self_distill_enc_path: Path | str = DEFAULT_LINE_SELF_DISTILL_ENC,
    include_line_self_distill: bool = True,
) -> list[dict[str, Any]]:
    """Pull from all sources, normalize, dedup.

    Priority (first-seen wins on dedup):
      corrections > organic > line_export > notes_distilled >
      line_self_distill > sft > distilled > open_corpus

    notes_distilled 排比 sft 高（user 自己寫的 vs Gemini 蒸的）。
    line_self_distill 比 sft 高（真實 30k 家人訊息 vs 通用 SFT）。
    open_corpus 是最低優先 — 通用中文 chat corpus，當 base 注入不主導風格。
    """
    db_path = Path(db_path)
    distilled_path = Path(distilled_path)
    notes_distilled_path = Path(notes_distilled_path)
    sft_path = Path(sft_path)

    raw: list[dict[str, Any]] = []

    # 1. Corrections (highest priority)
    try:
        corrections = extract_with_corrections(db_path)
        logger.info("corrections: %d", len(corrections))
        raw.extend(corrections)
    except Exception as e:
        logger.warning("extract_with_corrections failed: %s", e)

    # 2. Organic pairs from context
    try:
        organic = extract_all_pairs(db_path)
        logger.info("organic: %d", len(organic))
        raw.extend(organic)
    except Exception as e:
        logger.warning("extract_all_pairs failed: %s", e)

    # 3. LINE export `[LINE]*.txt` files (medium priority — between organic and notes)
    if include_exports:
        try:
            files = find_all_exports(export_search_dirs)
            export_pairs: list[dict[str, Any]] = []
            for f in files:
                export_pairs.extend(extract_pairs_from_export(f))
            logger.info("line_export: %d pairs from %d files", len(export_pairs), len(files))
            raw.extend(export_pairs)
        except Exception as e:
            logger.warning("line_export scan failed: %s", e)

    # 4. Notes-self-distilled (Andrew 自己寫的筆記 → LLM 反生 Q&A)
    #    比 sft / Gemini-distilled 都高，因為內容是 Andrew 親手寫的觀點
    if include_notes and notes_distilled_path.exists():
        notes = _read_jsonl(notes_distilled_path)
        # ensure source label is set
        for n in notes:
            meta = n.setdefault("metadata", {})
            meta.setdefault("source", "notes_distilled")
        logger.info("notes_distilled: %d", len(notes))
        raw.extend(notes)

    # 4.5 Line-self-distilled (Gemini 對 30k 家人 LINE 訊息蒸的「咪寶會怎麼接」)
    #     比 sft 高（30k 真實家人訊息 vs 通用 SFT examples）。
    #     優先讀 plaintext jsonl；不存在則嘗試解 .enc 加密版（讀完即刪 /tmp 明文）。
    if include_line_self_distill:
        lsd_pairs: list[dict[str, Any]] = []
        plain_p = Path(line_self_distill_path)
        enc_p = Path(line_self_distill_enc_path)
        if plain_p.exists():
            lsd_pairs = _read_jsonl(plain_p)
        elif enc_p.exists():
            try:
                from finetune.dataset_crypt import decrypt_file
                tmp = Path("/tmp") / f"_lsd_{enc_p.stem}"
                try:
                    decrypt_file(enc_p, tmp)
                    lsd_pairs = _read_jsonl(tmp)
                finally:
                    try:
                        if tmp.exists():
                            import os as _os
                            _os.unlink(tmp)
                    except OSError:
                        pass
            except Exception as e:
                logger.warning("line_self_distill enc decrypt failed: %s", e)
        for r in lsd_pairs:
            # canonical schema 兩種：{prompt,completion,source} 或 {messages,metadata}
            if "messages" in r:
                meta = r.setdefault("metadata", {})
                meta.setdefault("source", "line_self_distill")
            else:
                r.setdefault("source", "line_self_distill")
        logger.info("line_self_distill: %d", len(lsd_pairs))
        raw.extend(lsd_pairs)

    # 5. SFT jsonl (curated SFT examples)
    if include_sft and sft_path.exists():
        sft = _read_jsonl(sft_path)
        for s in sft:
            meta = s.setdefault("metadata", {})
            meta.setdefault("source", "sft")
        logger.info("sft: %d", len(sft))
        raw.extend(sft)

    # 6. Distilled jsonl (Gemini-蒸的，最低優先)
    if include_distilled and distilled_path.exists():
        distilled = _read_jsonl(distilled_path)
        logger.info("distilled: %d", len(distilled))
        raw.extend(distilled)

    # 7. Open-source Chinese chat corpus (最最低優先 — 只當 base 防 overfit)
    #    放在所有 personal source 之後 → first-seen-wins dedup 保護 personal
    #    pair。同時用 open_corpus_ratio cap 數量為 personal_count * ratio。
    open_corpus_loaded: list[dict[str, Any]] = []
    if open_corpus_path is not None:
        ocp = Path(open_corpus_path)
        if ocp.exists():
            open_corpus_loaded = _read_jsonl(ocp)
            for o in open_corpus_loaded:
                meta = o.setdefault("metadata", {})
                meta.setdefault("source", "open_corpus")
            logger.info("open_corpus loaded: %d", len(open_corpus_loaded))

    # Normalize + dedup by pair_hash; keep first occurrence so
    # (correction > organic > line_export > notes_distilled > sft >
    #  distilled > open_corpus)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in raw:
        canon = _to_canonical(r)
        if canon is None:
            continue
        h = canon["metadata"]["pair_hash"]
        if h in seen:
            continue
        seen.add(h)
        out.append(canon)

    # Now `out` is the personal-only canonical set; sample corpus by ratio.
    if open_corpus_loaded and open_corpus_ratio > 0:
        target = max(0, int(round(len(out) * open_corpus_ratio)))
        if target > 0:
            corpus_canon: list[dict[str, Any]] = []
            for o in open_corpus_loaded:
                canon = _to_canonical(o)
                if canon is None:
                    continue
                # ensure source label survives (defaults to 'distilled' otherwise)
                src = (o.get("metadata") or {}).get("source", "open_corpus")
                canon["metadata"]["source"] = src
                if canon["metadata"]["pair_hash"] in seen:
                    continue
                corpus_canon.append(canon)
            corpus_canon = corpus_canon[:target]
            for c in corpus_canon:
                seen.add(c["metadata"]["pair_hash"])
                out.append(c)
            logger.info(
                "open_corpus injected: target=%d written=%d ratio=%.3f",
                target, len(corpus_canon), open_corpus_ratio,
            )
    return out


def split_records(
    records: list[dict[str, Any]],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Deterministic 80/10/10 by sorting on pair_hash (hash already SHA256-uniform)."""
    sorted_recs = sorted(records, key=lambda r: r["metadata"]["pair_hash"])
    n = len(sorted_recs)
    n_train = int(n * TRAIN_RATIO)
    n_val = int(n * VAL_RATIO)
    train = sorted_recs[:n_train]
    val = sorted_recs[n_train : n_train + n_val]
    test = sorted_recs[n_train + n_val :]
    return train, val, test


def _apply_pii_mask(
    records: list[dict[str, Any]],
    pii_db_path: Path | str = DEFAULT_PII_DB,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Run each record's (user, assistant) content through `mask_with_cache`.

    Mapping is persisted **only** to local SQLite at `pii_db_path` — the
    placeholder-substituted text is what gets written to JSONL (and what
    might later be uploaded to cloud finetune).

    Returns (masked_records, stats) where stats is a category → count dict
    aggregated across all records (eg {"NAME": 12, "AMOUNT": 5, ...}).
    """
    total_counts: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for r in records:
        u_raw = r["messages"][0]["content"]
        b_raw = r["messages"][1]["content"]
        try:
            u_masked, b_masked, mapping = mask_with_cache(
                u_raw, b_raw, db_path=pii_db_path,
            )
        except Exception as e:
            logger.warning("mask_with_cache failed: %s — keeping raw", e)
            u_masked, b_masked, mapping = u_raw, b_raw, {}
        for cat, n in count_pii(mapping).items():
            total_counts[cat] = total_counts.get(cat, 0) + n
        new_meta = dict(r["metadata"])
        new_meta["pii_masked"] = True
        out.append({
            "messages": [
                {"role": "user", "content": u_masked},
                {"role": "assistant", "content": b_masked},
            ],
            "metadata": new_meta,
        })
    return out, total_counts


def build_dataset(
    db_path: Path | str = DEFAULT_DB,
    out_dir: Path | str = DEFAULT_DATA_DIR,
    distilled_path: Path | str = DEFAULT_DISTILLED,
    include_distilled: bool = True,
    include_exports: bool = True,
    export_search_dirs: list[Path | str] | None = None,
    notes_distilled_path: Path | str = DEFAULT_NOTES_DISTILLED,
    include_notes: bool = True,
    sft_path: Path | str = DEFAULT_SFT,
    include_sft: bool = True,
    augment_bt: int = 0,
    augment_paraphrase: int = 0,
    augment_paraphrase_max_pairs: int | None = None,
    mask_pii: bool = True,
    pii_db_path: Path | str = DEFAULT_PII_DB,
    open_corpus_path: Path | str | None = None,
    open_corpus_ratio: float = 0.15,
    line_self_distill_path: Path | str = DEFAULT_LINE_SELF_DISTILL,
    line_self_distill_enc_path: Path | str = DEFAULT_LINE_SELF_DISTILL_ENC,
    include_line_self_distill: bool = True,
) -> dict[str, Any]:
    """Run end-to-end. Returns stats dict.

    augment_bt > 0：split 完只對 train.jsonl 跑 back-translation augmentation
    （val/test 保持乾淨），overwrite train.jsonl 為 augmented 版。

    augment_paraphrase > 0：build 完額外用 local Qwen 對 train.jsonl 每對 prompt
    改寫 N 次，寫到 `train_aug.jsonl`（不影響原 train.jsonl，是 enrichment
    不是 replacement）。

    mask_pii=True (default): each pair is run through pii_masker before being
    written to disk; the raw → placeholder mapping is cached in
    `pii_db_path` (local SQLite, never sync'd to cloud).
    Use mask_pii=False ONLY for purely-local debugging — DO NOT upload
    those files anywhere.
    """
    out_dir = Path(out_dir)
    records = gather_records(
        db_path,
        distilled_path,
        include_distilled,
        include_exports=include_exports,
        export_search_dirs=export_search_dirs,
        notes_distilled_path=notes_distilled_path,
        include_notes=include_notes,
        sft_path=sft_path,
        include_sft=include_sft,
        open_corpus_path=open_corpus_path,
        open_corpus_ratio=open_corpus_ratio,
        line_self_distill_path=line_self_distill_path,
        line_self_distill_enc_path=line_self_distill_enc_path,
        include_line_self_distill=include_line_self_distill,
    )

    pii_counts: dict[str, int] = {}
    if mask_pii:
        records, pii_counts = _apply_pii_mask(records, pii_db_path=pii_db_path)
        logger.info("pii masked: %s", pii_counts)

    train, val, test = split_records(records)
    n_train = _write_jsonl(out_dir / "train.jsonl", train)
    n_val = _write_jsonl(out_dir / "val.jsonl", val)
    n_test = _write_jsonl(out_dir / "test.jsonl", test)
    # Per-source breakdown for visibility
    by_src: dict[str, int] = {}
    for r in records:
        s = r["metadata"].get("source", "unknown")
        by_src[s] = by_src.get(s, 0) + 1

    bt_added = 0
    if augment_bt > 0:
        try:
            from finetune.back_translate import augment_dataset_bt
            train_path = out_dir / "train.jsonl"
            tmp_out = out_dir / "train.bt.jsonl"
            bt_stats = augment_dataset_bt(
                train_path, tmp_out, n=augment_bt,
            )
            tmp_out.replace(train_path)
            bt_added = bt_stats.get("added", 0)
            n_train = bt_stats.get("kept", n_train) + bt_added
            logger.info(
                "back-translate aug: kept=%d added=%d failed=%d",
                bt_stats.get("kept", 0), bt_added, bt_stats.get("failed", 0),
            )
        except Exception as e:
            logger.warning("augment_bt failed: %s", e)

    paraphrase_added = 0
    if augment_paraphrase > 0:
        try:
            from finetune.paraphrase_aug import augment_dataset as _aug_paraphrase
            train_path = out_dir / "train.jsonl"
            aug_path = out_dir / "train_aug.jsonl"
            pa_stats = _aug_paraphrase(
                train_path, aug_path,
                n=augment_paraphrase,
                max_pairs=augment_paraphrase_max_pairs,
            )
            paraphrase_added = pa_stats.get("output", 0) - pa_stats.get(
                "processed", 0
            )
            logger.info(
                "paraphrase aug: input=%d processed=%d output=%d "
                "(written to %s)",
                pa_stats.get("input", 0),
                pa_stats.get("processed", 0),
                pa_stats.get("output", 0),
                aug_path,
            )
        except Exception as e:
            logger.warning("augment_paraphrase failed: %s", e)

    return {
        "total": len(records),
        "train": n_train,
        "val": n_val,
        "test": n_test,
        "by_source": by_src,
        "bt_added": bt_added,
        "paraphrase_added": paraphrase_added,
        "pii_counts": pii_counts,
        "pii_masked": mask_pii,
    }


def compute_stats(db_path: Path | str = DEFAULT_DB) -> dict[str, Any]:
    """Print-friendly stats from the DB only — no file writes."""
    db_path = Path(db_path)
    pairs = extract_all_pairs(db_path)
    corrections = extract_with_corrections(db_path)

    timestamps = [p["timestamp"] for p in pairs if p.get("timestamp")]
    groups = {p["group_id"] for p in pairs if p.get("group_id")}
    return {
        "organic_pairs": len(pairs),
        "correction_pairs": len(corrections),
        "groups": len(groups),
        "ts_min": min(timestamps) if timestamps else 0,
        "ts_max": max(timestamps) if timestamps else 0,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────
def _format_ts(ts: int) -> str:
    if not ts:
        return "n/a"
    from datetime import datetime
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return f"raw={ts}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB), help="path to line_bot.db")
    ap.add_argument("--out-dir", default=str(DEFAULT_DATA_DIR), help="output dir")
    ap.add_argument("--distilled", default=str(DEFAULT_DISTILLED))
    ap.add_argument("--no-distilled", action="store_true",
                    help="skip merging distilled.jsonl")
    ap.add_argument("--no-exports", action="store_true",
                    help="skip auto-scanning [LINE]*.txt exports")
    ap.add_argument("--export-dir", action="append", default=None,
                    help="extra dir to scan for [LINE]*.txt (repeatable)")
    ap.add_argument("--notes-distilled", default=str(DEFAULT_NOTES_DISTILLED),
                    help="path to notes_distilled.jsonl (self-distilled notes)")
    ap.add_argument("--no-notes", action="store_true",
                    help="skip merging notes_distilled.jsonl")
    ap.add_argument("--sft", default=str(DEFAULT_SFT))
    ap.add_argument("--no-sft", action="store_true", help="skip merging sft.jsonl")
    ap.add_argument(
        "--line-self-distill", default=str(DEFAULT_LINE_SELF_DISTILL),
        help="path to line_self_distilled.jsonl (Gemini 蒸的咪寶會怎麼接 30k 家人訊息)",
    )
    ap.add_argument(
        "--line-self-distill-enc", default=str(DEFAULT_LINE_SELF_DISTILL_ENC),
        help="path to line_self_distilled.jsonl.enc（fallback 加密版）",
    )
    ap.add_argument(
        "--no-line-self-distill", action="store_true",
        help="skip merging line_self_distilled",
    )
    ap.add_argument("--build", action="store_true", help="run full build pipeline")
    ap.add_argument("--stats", action="store_true",
                    help="print stats only, no file writes")
    ap.add_argument("--augment-bt", type=int, default=0, metavar="N",
                    help="跑 N 輪 back-translation augmentation 在 train split 上 "
                         "(0=不跑，建議 2)")
    ap.add_argument(
        "--augment-paraphrase", type=int, default=0, metavar="N",
        help=(
            "build 完額外用 local Qwen 把 train.jsonl 每對 prompt 改寫 N 次，"
            "寫到 train_aug.jsonl（enrichment，不影響原 train.jsonl）"
        ),
    )
    ap.add_argument(
        "--augment-paraphrase-max-pairs", type=int, default=None,
        metavar="K",
        help="paraphrase augmentation 最多處理幾對 (避免 10000 對跑很久)",
    )
    ap.add_argument("--no-mask", action="store_true",
                    help="skip PII masking (本機跑 OK，雲端跑前千萬不要)")
    ap.add_argument("--mask-stats", action="store_true",
                    help="print PII mask category counts after build")
    ap.add_argument("--pii-db", default=str(DEFAULT_PII_DB),
                    help="path to pii mappings SQLite (only-local)")
    ap.add_argument(
        "--with-corpus", nargs="?", const=str(DEFAULT_OPEN_CORPUS),
        default=None, metavar="PATH",
        help=(
            "混入 open-source 中文 chat corpus 當 base "
            f"(預設路徑：{DEFAULT_OPEN_CORPUS}). "
            "Use `--with-corpus` 直接套預設路徑，或 `--with-corpus /some/file.jsonl` 指定。"
        ),
    )
    ap.add_argument(
        "--corpus-ratio", type=float, default=0.15, metavar="R",
        help=(
            "open_corpus 混入比例 vs personal pairs (default 0.15). "
            "建議 10-15%%；超過 20%% 會稀釋 personal style。"
        ),
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    if not args.build and not args.stats:
        ap.print_help()
        return 1

    if args.stats:
        s = compute_stats(args.db)
        print(f"{s['organic_pairs']} pairs / {s['groups']} groups")
        print(f"corrections: {s['correction_pairs']}")
        print(
            "time range: "
            f"{_format_ts(s['ts_min'])} → {_format_ts(s['ts_max'])}"
        )

    if args.build:
        stats = build_dataset(
            db_path=args.db,
            out_dir=args.out_dir,
            distilled_path=args.distilled,
            include_distilled=not args.no_distilled,
            include_exports=not args.no_exports,
            export_search_dirs=args.export_dir,
            notes_distilled_path=args.notes_distilled,
            include_notes=not args.no_notes,
            sft_path=args.sft,
            include_sft=not args.no_sft,
            augment_bt=args.augment_bt,
            augment_paraphrase=args.augment_paraphrase,
            augment_paraphrase_max_pairs=args.augment_paraphrase_max_pairs,
            mask_pii=not args.no_mask,
            pii_db_path=args.pii_db,
            open_corpus_path=args.with_corpus,
            open_corpus_ratio=args.corpus_ratio,
            line_self_distill_path=args.line_self_distill,
            line_self_distill_enc_path=args.line_self_distill_enc,
            include_line_self_distill=not args.no_line_self_distill,
        )
        print(
            f"wrote {stats['total']} records: "
            f"train={stats['train']} val={stats['val']} test={stats['test']}"
        )
        if stats.get("by_source"):
            parts = ", ".join(f"{k}={v}" for k, v in sorted(stats["by_source"].items()))
            print(f"by source: {parts}")
        if stats.get("bt_added"):
            print(f"back-translate aug: +{stats['bt_added']} train variants")
        if stats.get("paraphrase_added"):
            print(
                f"paraphrase aug: +{stats['paraphrase_added']} variants "
                f"→ train_aug.jsonl"
            )
        if args.mask_stats or stats.get("pii_masked"):
            pii = stats.get("pii_counts") or {}
            if pii:
                total = sum(pii.values())
                parts = ", ".join(f"{k}={v}" for k, v in sorted(pii.items()))
                print(f"PII masked: total={total} ({parts})")
            elif stats.get("pii_masked"):
                print("PII masked: total=0 (no PII detected)")
            else:
                print("PII masking SKIPPED — do NOT upload these files")

    return 0


if __name__ == "__main__":
    sys.exit(main())
