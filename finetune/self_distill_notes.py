"""Self-distill from Andrew 既有 markdown 筆記 → Q&A pairs for fine-tune.

訓練目標 = 模仿**目前 Gemini 加 _CORE_PROMPT 後的咪寶輸出**（不是 Andrew 風格）。

Pipeline (新版 2026-05-08，Q / A 分離)：
  1. find_note_files()                ── 自動掃 4 個 note dir 下的 .md
  2. extract_chunks()                 ── 切 chunk（## 標題 / 段落 / 字數 cap）
  3. extract_questions(chunk, n=N)    ── 用本機 14B 從 chunk 抽 N 個「user 可能會問」
                                          的問題（**只出 Q，不出 A**）
  4. generate_answers_via_gemini(qs, chunk)
                                      ── 一次 Gemini call 對 N 個 Q 同時生答
                                          （咪寶風 / _CORE_PROMPT 自動載入）
  5. build_distill_jsonl()            ── 串接以上 + 持久化進度 + quota 上限

CLI:
    python finetune/self_distill_notes.py [--max-chunks 50] [--dry-run]
                                          [--max-gemini-calls 10] [--batch-size 5]

  --dry-run             只印 stats / 估算，不 call LLM
  --max-chunks N        限制 chunk 數
  --max-gemini-calls N  限制本次 Gemini call 數（保留 quota 給主對話），預設 10
  --batch-size N        每 chunk 抽幾 Q / 一個 batch（3-5；預設 5）
  --no-resume           不讀進度檔，從頭跑（會重複處理已做過的 chunk）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
LINE_BOT_ROOT = HERE.parent
if str(LINE_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(LINE_BOT_ROOT))

logger = logging.getLogger("self_distill_notes")

# ── Note source roots ──────────────────────────────────────────────────────
HOME = Path.home()
NOTE_ROOTS: list[Path] = [
    HOME / "Desktop/andrew/Data_engineer/mock_interview",
    HOME / "Desktop/andrew/Data_engineer/project",
    HOME / "Desktop/andrew/job_search",
    HOME / ".claude/projects/-Users-andrew/memory",
]

# ── Chunk thresholds ───────────────────────────────────────────────────────
MIN_CHUNK_LEN = 100
MAX_CHUNK_LEN = 2000
DEFAULT_CHUNK_SIZE = 500
DEFAULT_QUESTIONS_PER_CHUNK = 5  # batch size 給 Gemini，3-5 之間
DEFAULT_MAX_GEMINI_CALLS = 10    # 保留另外 10 quota 給主對話
ESTIMATED_PAIRS_PER_CHUNK = DEFAULT_QUESTIONS_PER_CHUNK

# ── Output ─────────────────────────────────────────────────────────────────
DEFAULT_OUT = HERE / "data" / "notes_distilled.jsonl"
DEFAULT_PROGRESS = HERE / "data" / "notes_distilled_progress.json"

# ── 本機 14B 抽 Q 的 prompt（不出 A）────────────────────────────────────────
QUESTIONS_PROMPT = """你是個資料萃取助理。下面這段是一份筆記/文章的內容。
請站在「使用者」角度，列出 {n} 個使用者**可能會問**的問題（自然口吻、繁體中文）。
- 問題要具體，**不要**「這段在講什麼」這種空泛問句
- 不要回答，只列問題
- 不要 markdown，不要編號，**每行一個問題**

筆記內容：
{chunk}

問題列表（{n} 行，每行 1 題）："""

# ── Gemini batch 生 A 的 prompt（咪寶風由 _CORE_PROMPT 自動帶）──────────────
ANSWERS_BATCH_PROMPT = """下面是參考內容（背景資料）：
---
{chunk}
---

請對下面 {n} 個問題，分別用咪寶風（繁中、有觀點、簡短、第一句具體判斷）回應。
只能用 JSON list 輸出（不要 markdown code fence、不要其他解釋），格式：
[{{"q": "...", "a": "..."}}, ...]

問題：
{numbered_questions}

JSON 輸出："""


# ── public API: scan / chunk ───────────────────────────────────────────────
def find_note_files(roots: list[Path] | None = None) -> list[Path]:
    """掃描所有 note dir 找出 .md 檔。回排序後 list[Path]。"""
    if roots is None:
        roots = NOTE_ROOTS
    out: list[Path] = []
    skip_dirs = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache",
                 ".mypy_cache", ".ruff_cache", "venv"}
    for root in roots:
        if not root.exists() or not root.is_dir():
            logger.debug("note root not found: %s", root)
            continue
        for path in root.rglob("*.md"):
            if any(part in skip_dirs for part in path.parts):
                continue
            try:
                if path.is_file():
                    out.append(path)
            except OSError:
                continue
    return sorted(set(out))


def extract_chunks(path: Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> list[str]:
    """讀 markdown 切 chunk。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("read failed for %s: %s", path, e)
        return []

    sections = _split_h2_sections(text)
    chunks: list[str] = []
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        if len(sec) <= chunk_size:
            chunks.append(sec)
        else:
            for sub in _split_by_paragraphs(sec, chunk_size):
                chunks.append(sub)

    return [c for c in chunks if MIN_CHUNK_LEN <= len(c) <= MAX_CHUNK_LEN]


def _split_h2_sections(text: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    sections: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current:
                sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)
    return ["".join(sec) for sec in sections]


def _split_by_paragraphs(text: str, chunk_size: int) -> list[str]:
    paras = re.split(r"\n\s*\n", text)
    out: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for p in paras:
        p = p.strip()
        if not p:
            continue
        if len(p) > chunk_size:
            if cur:
                out.append("\n\n".join(cur))
                cur, cur_len = [], 0
            for i in range(0, len(p), chunk_size):
                out.append(p[i : i + chunk_size])
            continue
        if cur_len + len(p) + 2 > chunk_size and cur:
            out.append("\n\n".join(cur))
            cur, cur_len = [p], len(p)
        else:
            cur.append(p)
            cur_len += len(p) + 2
    if cur:
        out.append("\n\n".join(cur))
    return out


# ── Step A: extract questions via local 14B ───────────────────────────────
def extract_questions(
    chunk: str,
    n: int = DEFAULT_QUESTIONS_PER_CHUNK,
    chat_fn: Callable | None = None,
) -> list[str]:
    """用本機 14B 從 chunk 抽 N 個「user 可能問」的問題（不出 A）。

    Args:
      chunk: 筆記片段
      n: 要抽幾條問題
      chat_fn: 可注入測試用 chat 函式（簽名同 local_llm.chat）

    Returns: list[str]，最多 n 條。失敗 / 回空回 []。
    """
    if not chunk or not chunk.strip() or n <= 0:
        return []
    if chat_fn is None:
        try:
            import local_llm
            chat_fn = local_llm.chat
        except Exception as e:
            logger.warning("local_llm import failed: %s", e)
            return []

    prompt = QUESTIONS_PROMPT.format(chunk=chunk, n=n)
    try:
        raw = chat_fn(
            prompt,
            context=None,
            system_prompt=(
                "你是萃取問題的工具助手。只列出問題，不答題，每行一條，"
                "不要 markdown 不要編號。"
            ),
            max_tokens=400,
        )
    except TypeError:
        try:
            raw = chat_fn(prompt)
        except Exception as e:
            logger.warning("chat_fn fallback call failed: %s", e)
            return []
    except Exception as e:
        logger.warning("chat_fn call failed: %s", e)
        return []

    return _parse_questions_text(raw, n)


def _parse_questions_text(raw: str | None, n: int) -> list[str]:
    """從 LLM 輸出抽 N 條問題（每行一條）。

    清掉 markdown bullet / 數字編號 / code fence。
    """
    if not raw:
        return []
    text = raw.strip()
    text = re.sub(r"^```(?:json|text)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        # 去掉 1. / 1) / - / * / • 開頭
        s = re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", s)
        s = s.strip()
        if not s:
            continue
        # 過短噪音 / 不像問題的丟掉（容忍中英問號 / 「嗎」/「呢」/「？」缺尾的陳述句）
        if len(s) < 4:
            continue
        out.append(s)
        if len(out) >= n:
            break
    return out


# ── Step B: generate answers via Gemini (batched) ──────────────────────────
def generate_answers_via_gemini(
    questions: list[str],
    context_chunk: str,
    gemini_chat: Callable | None = None,
) -> list[tuple[str, str]]:
    """一次 Gemini call 處理多 Q（同 chunk context 共享，省 quota）。

    Args:
      questions: 上一步抽出來的問題 list
      context_chunk: 提供給 Gemini 的背景片段
      gemini_chat: 注入測試 mock；簽名 = gemini_client.chat
        (user_input, context, facts, persona_notes=None, group_id=None) -> str

    Returns: list[(q, a)]。失敗回 []。
    """
    if not questions:
        return []
    if gemini_chat is None:
        try:
            import gemini_client
            gemini_chat = gemini_client.chat
        except Exception as e:
            logger.warning("gemini_client import failed: %s", e)
            return []

    numbered = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    prompt = ANSWERS_BATCH_PROMPT.format(
        chunk=context_chunk, n=len(questions), numbered_questions=numbered,
    )

    # gemini_client.chat 自動帶 _CORE_PROMPT，不用我們 inject 系統 prompt
    try:
        raw = gemini_chat(
            prompt,
            [],   # context: 空（這不是延續對話）
            [],   # facts: 空
        )
    except TypeError:
        try:
            raw = gemini_chat(prompt)
        except Exception as e:
            logger.warning("gemini_chat fallback call failed: %s", e)
            return []
    except Exception as e:
        logger.warning("gemini_chat call failed: %s", e)
        return []

    if not raw:
        return []
    pairs = _parse_qa_json(raw)
    if not pairs:
        return []
    # 對齊原始問題：若 Gemini 回的 q 跟我們送的對得上就用我們的（避免 Gemini 改寫問題）
    # 不到的部分照 LLM 給的順序裁切到 len(questions)
    aligned: list[tuple[str, str]] = []
    for i, (q, a) in enumerate(pairs):
        original = questions[i] if i < len(questions) else q
        aligned.append((original, a))
    return aligned[: len(questions)]


def _parse_qa_json(raw: str) -> list[tuple[str, str]]:
    """抽 [{"q":..., "a":...}, ...]（也容忍 question/answer key）。"""
    if not raw:
        return []
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    blob = text[start : end + 1]
    try:
        items = json.loads(blob)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    out: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        q = item.get("q") or item.get("question")
        a = item.get("a") or item.get("answer")
        if isinstance(q, str) and isinstance(a, str):
            q, a = q.strip(), a.strip()
            if q and a:
                out.append((q, a))
    return out


# ── 整合：從 chunk 直接拿到 Q&A pair（給 backward compat / 單元測試）──────
def generate_qa_pair(
    chunk: str,
    questions_chat_fn: Callable | None = None,
    gemini_chat_fn: Callable | None = None,
    n: int = DEFAULT_QUESTIONS_PER_CHUNK,
) -> list[tuple[str, str]]:
    """新版 pipeline 一站式：14B 抽 Q → Gemini 答 A。

    Args:
      chunk: 筆記片段
      questions_chat_fn: local 14B chat（測試 mock 點）
      gemini_chat_fn: gemini_client.chat（測試 mock 點）
      n: 抽幾 Q
    """
    qs = extract_questions(chunk, n=n, chat_fn=questions_chat_fn)
    if not qs:
        return []
    return generate_answers_via_gemini(qs, chunk, gemini_chat=gemini_chat_fn)


# ── progress 持久化 ────────────────────────────────────────────────────────
def _chunk_hash(chunk: str) -> str:
    """穩定 hash（sha1 短碼），給 progress 紀錄用。"""
    return hashlib.sha1(chunk.encode("utf-8")).hexdigest()[:16]


def _load_progress(progress_path: Path) -> dict[str, Any]:
    if not progress_path.exists():
        return {"processed_hashes": [], "gemini_calls_total": 0,
                "pairs_total": 0, "last_run_at": 0}
    try:
        return json.loads(progress_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("progress load failed (%s): %s — start fresh", progress_path, e)
        return {"processed_hashes": [], "gemini_calls_total": 0,
                "pairs_total": 0, "last_run_at": 0}


def _save_progress(progress_path: Path, data: dict[str, Any]) -> None:
    try:
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        progress_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("progress save failed: %s", e)


# ── 主跑：scan → chunk → (Q via 14B) → (A via Gemini, batched) → jsonl ────
def build_distill_jsonl(
    out_path: Path | str = DEFAULT_OUT,
    note_roots: list[Path] | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_chunks: int | None = None,
    max_gemini_calls: int = DEFAULT_MAX_GEMINI_CALLS,
    batch_size: int = DEFAULT_QUESTIONS_PER_CHUNK,
    questions_chat_fn: Callable | None = None,
    gemini_chat_fn: Callable | None = None,
    progress_path: Path | str = DEFAULT_PROGRESS,
    resume: bool = True,
) -> dict[str, Any]:
    """完整跑：scan 筆記 → 切 chunk → 14B 抽 Q → Gemini 批量答 A → append jsonl。

    Returns stats: {files, chunks, processed, skipped_resume, pairs, errors,
                    gemini_calls, gemini_calls_remaining}
    """
    out_path = Path(out_path)
    progress_path = Path(progress_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    progress = _load_progress(progress_path) if resume else {
        "processed_hashes": [], "gemini_calls_total": 0,
        "pairs_total": 0, "last_run_at": 0,
    }
    seen: set[str] = set(progress.get("processed_hashes", []))

    files = find_note_files(note_roots)
    all_chunks: list[tuple[Path, str]] = []
    for f in files:
        for c in extract_chunks(f, chunk_size=chunk_size):
            all_chunks.append((f, c))

    if max_chunks is not None and max_chunks > 0:
        all_chunks = all_chunks[:max_chunks]

    pairs_written = 0
    errors = 0
    skipped_resume = 0
    gemini_calls_used = 0
    processed_now: list[str] = []

    # append（不 overwrite，配合 progress 累積 mode）
    mode = "a" if resume and out_path.exists() else "w"
    with out_path.open(mode, encoding="utf-8") as out_f:
        for src_path, chunk in all_chunks:
            h = _chunk_hash(chunk)
            if h in seen:
                skipped_resume += 1
                continue
            if gemini_calls_used >= max_gemini_calls:
                logger.info(
                    "reached --max-gemini-calls=%d, stop early", max_gemini_calls,
                )
                break
            # Step A: 本機 14B 抽 Q（不算 Gemini call）
            qs = extract_questions(
                chunk, n=batch_size, chat_fn=questions_chat_fn,
            )
            if not qs:
                errors += 1
                # 計入 seen 避免下次再試（同 chunk 失敗大機率仍會失敗）
                seen.add(h)
                processed_now.append(h)
                continue
            # Step B: 一次 Gemini call 答全 batch
            qa_pairs = generate_answers_via_gemini(
                qs, chunk, gemini_chat=gemini_chat_fn,
            )
            gemini_calls_used += 1
            if not qa_pairs:
                errors += 1
                # 計入 seen 避免下次再卡（也算 used 了，因為 Gemini call 已花）
                seen.add(h)
                processed_now.append(h)
                continue

            for q, a in qa_pairs:
                rec = {
                    "messages": [
                        {"role": "user", "content": q},
                        {"role": "assistant", "content": a},
                    ],
                    "metadata": {
                        "source": "notes_distilled",
                        "source_file": str(src_path),
                        "distilled_at": int(time.time()),
                    },
                }
                out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                pairs_written += 1
            seen.add(h)
            processed_now.append(h)

    # 寫回 progress
    progress["processed_hashes"] = sorted(seen)
    progress["gemini_calls_total"] = (
        progress.get("gemini_calls_total", 0) + gemini_calls_used
    )
    progress["pairs_total"] = (
        progress.get("pairs_total", 0) + pairs_written
    )
    progress["last_run_at"] = int(time.time())
    _save_progress(progress_path, progress)

    return {
        "files": len(files),
        "chunks": len(all_chunks),
        "processed": len(processed_now),
        "skipped_resume": skipped_resume,
        "pairs": pairs_written,
        "errors": errors,
        "gemini_calls": gemini_calls_used,
        "gemini_calls_remaining": max(0, max_gemini_calls - gemini_calls_used),
    }


def estimate_dry_run(note_roots: list[Path] | None = None,
                     chunk_size: int = DEFAULT_CHUNK_SIZE,
                     batch_size: int = DEFAULT_QUESTIONS_PER_CHUNK,
                     max_gemini_calls: int = DEFAULT_MAX_GEMINI_CALLS,
                     ) -> dict[str, Any]:
    """掃描檔案 + 切 chunk（不 call LLM）→ 估算總 chunk / 預計天數。"""
    files = find_note_files(note_roots)
    by_root: dict[str, dict[str, int]] = {}
    if note_roots is None:
        note_roots = NOTE_ROOTS

    total_bytes = 0
    total_chunks = 0
    for root in note_roots:
        rb = 0
        rc = 0
        rf = 0
        for f in files:
            try:
                if str(f).startswith(str(root)):
                    rf += 1
                    rb += f.stat().st_size
                    rc += len(extract_chunks(f, chunk_size=chunk_size))
            except OSError:
                continue
        by_root[str(root)] = {"files": rf, "bytes": rb, "chunks": rc}
        total_bytes += rb
        total_chunks += rc

    estimated_pairs_total = total_chunks * batch_size
    pairs_per_day = max_gemini_calls * batch_size
    days_needed = (
        (total_chunks + max_gemini_calls - 1) // max_gemini_calls
        if max_gemini_calls > 0
        else 0
    )
    return {
        "total_files": len(files),
        "total_bytes": total_bytes,
        "total_chunks": total_chunks,
        "batch_size": batch_size,
        "max_gemini_calls_per_day": max_gemini_calls,
        "estimated_pairs_total": estimated_pairs_total,
        "pairs_per_day": pairs_per_day,
        "days_needed": days_needed,
        "by_root": by_root,
    }


# ── CLI ────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-chunks", type=int, default=None,
                    help="限制處理 chunk 數")
    ap.add_argument("--dry-run", action="store_true",
                    help="只印 stats，不 call LLM")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--progress", default=str(DEFAULT_PROGRESS))
    ap.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    ap.add_argument("--batch-size", type=int, default=DEFAULT_QUESTIONS_PER_CHUNK,
                    help="每 chunk 抽幾 Q（同一 Gemini batch；3-5；預設 5）")
    ap.add_argument("--max-gemini-calls", type=int,
                    default=DEFAULT_MAX_GEMINI_CALLS,
                    help="本次跑最多用幾 Gemini call（保留 quota 給主對話）")
    ap.add_argument("--no-resume", action="store_true",
                    help="不讀 progress，從頭跑")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    if args.dry_run:
        s = estimate_dry_run(
            chunk_size=args.chunk_size,
            batch_size=args.batch_size,
            max_gemini_calls=args.max_gemini_calls,
        )
        print(f"找到 {s['total_files']} 個 .md 檔, "
              f"總字數 {s['total_bytes']} bytes")
        print(f"切出 {s['total_chunks']} chunks，"
              f"預計每天 {s['max_gemini_calls_per_day']} calls × "
              f"{s['batch_size']} batch = {s['pairs_per_day']} 對 pair / day")
        print(f"估算總 pair = {s['estimated_pairs_total']}，"
              f"需 {s['days_needed']} 天 (在 quota={s['max_gemini_calls_per_day']} / 天的設定下)")
        for root, stats in s["by_root"].items():
            print(f"  {root}: files={stats['files']}, "
                  f"bytes={stats['bytes']}, chunks={stats['chunks']}")
        return 0

    stats = build_distill_jsonl(
        out_path=args.out,
        chunk_size=args.chunk_size,
        max_chunks=args.max_chunks,
        max_gemini_calls=args.max_gemini_calls,
        batch_size=args.batch_size,
        progress_path=args.progress,
        resume=not args.no_resume,
    )
    print(
        f"處理 {stats['files']} 檔 / {stats['chunks']} chunk "
        f"(本次新處理 {stats['processed']}, resume 跳過 {stats['skipped_resume']}). "
        f"用了 {stats['gemini_calls']} 次 Gemini call，"
        f"產出 {stats['pairs']} 對 pair，剩 {stats['gemini_calls_remaining']} call 配額"
    )
    if stats["errors"]:
        print(f"  失敗 {stats['errors']} chunk（local 抽 Q 失敗 / Gemini 解析失敗）")
    print(f"輸出 jsonl: {args.out}")
    print(f"進度檔: {args.progress}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
