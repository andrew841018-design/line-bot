"""LINE 30k 家人訊息 → 咪寶 self-distill pipeline。

對 line_export_raw.jsonl.enc 裡的家人訊息（30432 條），用 Gemini + 既有
咪寶 _CORE_PROMPT 生成「咪寶會怎麼接」的回覆，配對成 SFT pair 後
**再加密寫回**。Sample 模式 + smart batching 省 quota。

設計：
  - decrypt_and_load: 解 .enc → /tmp/line_export_raw.jsonl → 讀 jsonl → list
    跑完 finally clause 確保 /tmp/ 明文刪除（即使 Gemini 中途炸）
  - sample_messages: random 或 diverse（按 sender 均衡）sample N 條
    過濾：純媒體（貼圖/圖片/影片）、< 5 字、純表情
  - batch_distill: batch_size 條一次 Gemini call（省 quota），輸出 JSON list
    parse → SFT pair → PII mask → 加密寫回 line_self_distilled.jsonl.enc
  - 持久化進度：finetune/data/line_self_distill_progress.json
    記哪些 message_id 處理過，避免重複燒 quota
  - quota cap：max_calls 上限，超過就停（每天 ~10 calls = 50 對 / day）

CLI:
    python finetune/distill_line_to_meiba.py --sample 1000 --batch-size 5 --max-calls 10
    python finetune/distill_line_to_meiba.py --sample 1000 --max-calls 10 --dry-run
        # 估算「sample N 條 / batch B → max-calls × pairs / call = total pairs」

隱私：
  - 解密只在 /tmp/，跑完 (即使中途異常 finally clause) 立刻刪明文
  - 加密寫 line_self_distilled.jsonl.enc（共用 ~/.line_bot_encryption.key）
  - 過 PII masker（NAME / ADDRESS / PHONE / AMOUNT / ID / ORG）

注意：
  - **不要真實跑** — Gemini 20 req/day quota 會被燒爆
  - Mock 模式跟測試模式可以放心跑（chat_fn injection）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
LINE_BOT_ROOT = HERE.parent
if str(LINE_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(LINE_BOT_ROOT))

from finetune.dataset_crypt import decrypt_file, encrypt_file  # noqa: E402

logger = logging.getLogger("distill_line_to_meiba")

# ── 路徑常量 ───────────────────────────────────────────────────────────────
DEFAULT_ENC_INPUT = HERE / "data" / "line_export_raw.jsonl.enc"
DEFAULT_TMP_DECRYPTED = Path("/tmp/line_export_raw.jsonl")
DEFAULT_PROGRESS = HERE / "data" / "line_self_distill_progress.json"
DEFAULT_OUTPUT_PLAINTEXT = HERE / "data" / "line_self_distilled.jsonl"
DEFAULT_OUTPUT_ENC = HERE / "data" / "line_self_distilled.jsonl.enc"

# ── 過濾門檻 ──────────────────────────────────────────────────────────────
MIN_MSG_LEN = 5  # < 5 字過濾
MEDIA_PLACEHOLDERS = (
    "貼圖", "照片", "影片", "圖片", "檔案", "語音訊息",
    "相簿", "禮物", "紅包", "位置資訊", "聯絡人",
    "語音通話", "視訊通話",
    "[貼圖]", "[照片]", "[影片]", "[檔案]", "[語音訊息]",
    "[相簿]", "[禮物]", "[紅包]", "[位置資訊]", "[聯絡人]",
    "[Sticker]", "[Photo]", "[Video]", "[File]",
)

# 純表情 / emoji-only
_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F"
    r"\U0001F0A0-\U0001F0FF✀-➿]+"
)
_PUNCT_ONLY_RE = re.compile(r"^[\s\W_]+$")

DEFAULT_SAMPLE_SEED = 42
DEFAULT_BATCH_SIZE = 5
DEFAULT_MAX_CALLS = 10  # Gemini free tier 20/day 安全上限


# ─────────────────────────────────────────────────────────────────────────
# Distill prompt 範本（batch 版 — 一次餵 batch_size 條）
# ─────────────────────────────────────────────────────────────────────────
DISTILL_BATCH_PROMPT = """你是咪寶（既有人設按 system prompt）。下面是 LINE 家人群組訊息序列。
對每條訊息，請給「如果咪寶在群裡會怎麼接」的回覆（短、有觀點、繁中、規則 0）。
輸出 JSON list（不要 markdown，不要其他解釋）：
[{{"msg": "...", "reply": "..."}}, ...]

訊息：
{numbered}

JSON 輸出："""


# ─────────────────────────────────────────────────────────────────────────
# 1. decrypt + load
# ─────────────────────────────────────────────────────────────────────────
def decrypt_and_load(
    enc_path: Path | str = DEFAULT_ENC_INPUT,
    tmp_path: Path | str = DEFAULT_TMP_DECRYPTED,
) -> list[dict[str, Any]]:
    """解密 .enc → /tmp/ → 讀 jsonl → 回 list[dict]。

    注意：caller 跑完務必 cleanup_tmp(tmp_path) — 通常用 finally clause。
    """
    enc_path = Path(enc_path)
    tmp_path = Path(tmp_path)

    if not enc_path.exists():
        logger.warning("encrypted input not found: %s", enc_path)
        return []

    decrypted = decrypt_file(enc_path, tmp_path)
    rows: list[dict[str, Any]] = []
    with decrypted.open("r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("skip malformed line %d in %s", ln, decrypted)
    logger.info("loaded %d rows from %s", len(rows), enc_path)
    return rows


def cleanup_tmp(tmp_path: Path | str = DEFAULT_TMP_DECRYPTED) -> None:
    """安全刪除 /tmp/ 明文。FileNotFoundError 不噴。"""
    p = Path(tmp_path)
    try:
        if p.exists():
            os.unlink(p)
            logger.info("cleaned up plaintext %s", p)
    except OSError as e:
        logger.warning("cleanup_tmp failed for %s: %s", p, e)


# ─────────────────────────────────────────────────────────────────────────
# 2. sample
# ─────────────────────────────────────────────────────────────────────────
def _is_media(content: str) -> bool:
    s = content.strip()
    return s in MEDIA_PLACEHOLDERS


def _is_pure_emoji(content: str) -> bool:
    s = content.strip()
    if not s:
        return True
    stripped = _EMOJI_RE.sub("", s).strip()
    if not stripped:
        return True
    if _PUNCT_ONLY_RE.match(stripped):
        return True
    return False


def _is_quality_msg(content: str) -> bool:
    s = (content or "").strip()
    if len(s) < MIN_MSG_LEN:
        return False
    if _is_media(s):
        return False
    if _is_pure_emoji(s):
        return False
    return True


def _msg_id(row: dict[str, Any]) -> str:
    """Stable id over (date, time, sender, content) — used for progress dedup."""
    blob = (
        f"{row.get('date','')}│{row.get('time','')}│"
        f"{row.get('sender','')}│{row.get('content','')}"
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def sample_messages(
    rows: list[dict[str, Any]],
    n: int = 1000,
    strategy: str = "random",
    seed: int = DEFAULT_SAMPLE_SEED,
    skip_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Sample N 條品質 OK 的訊息。

    Args:
      rows: decrypt_and_load() 回的 list
      n: 要 sample 的條數
      strategy: 'random'（純亂選 deterministic）或
                'diverse'（按 sender 均衡 — 每個家人均量）
      seed: random seed
      skip_ids: 已處理過的 message_id（從 progress.json 載入）→ 排除
    """
    skip_ids = skip_ids or set()
    rng = random.Random(seed)

    # 過濾品質 + 已處理過
    candidates: list[dict[str, Any]] = []
    for r in rows:
        c = r.get("content", "")
        if not _is_quality_msg(c):
            continue
        # 補上 stable id 供後面用
        mid = _msg_id(r)
        if mid in skip_ids:
            continue
        # 攜帶 message_id 一起回傳，後面 batch_distill 不用重算
        rec = dict(r)
        rec["_message_id"] = mid
        candidates.append(rec)

    if not candidates:
        return []

    if strategy == "diverse":
        # 按 sender 分組 → round-robin 抽，達到均衡
        by_sender: dict[str, list[dict[str, Any]]] = {}
        for r in candidates:
            s = r.get("sender", "")
            by_sender.setdefault(s, []).append(r)
        # shuffle each bucket for fairness
        for buf in by_sender.values():
            rng.shuffle(buf)
        out: list[dict[str, Any]] = []
        senders = sorted(by_sender.keys())
        cursors = {s: 0 for s in senders}
        while len(out) < n:
            advanced = False
            for s in senders:
                if cursors[s] < len(by_sender[s]):
                    out.append(by_sender[s][cursors[s]])
                    cursors[s] += 1
                    advanced = True
                    if len(out) >= n:
                        break
            if not advanced:
                break
        return out

    # random
    rng.shuffle(candidates)
    return candidates[:n]


# ─────────────────────────────────────────────────────────────────────────
# 3. batch distill
# ─────────────────────────────────────────────────────────────────────────
def _build_batch_prompt(messages: list[dict[str, Any]]) -> str:
    """[{sender, content}, ...] → numbered list 注入 prompt。"""
    lines = []
    for i, m in enumerate(messages, 1):
        sender = m.get("sender", "?")
        content = m.get("content", "")
        # 截長：每條 ≤ 200 字（避免吃爆 Gemini context）
        if len(content) > 200:
            content = content[:200] + "..."
        lines.append(f"{i}. [{sender}] {content}")
    return DISTILL_BATCH_PROMPT.format(numbered="\n".join(lines))


def _parse_batch_json(raw: str) -> list[dict[str, str]]:
    """Robust parse JSON list of {msg, reply}. Strips fences / extra text."""
    if not raw:
        return []
    text = raw.strip()
    # strip code fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    blob = text[start: end + 1]
    try:
        items = json.loads(blob)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        msg = item.get("msg") or item.get("message")
        reply = item.get("reply") or item.get("answer")
        if isinstance(msg, str) and isinstance(reply, str):
            msg, reply = msg.strip(), reply.strip()
            if msg and reply:
                out.append({"msg": msg, "reply": reply})
    return out


def _default_chat_fn(prompt: str) -> str:
    """Default chat: 真實 call gemini_client (free tier)。test 時用 inject。"""
    try:
        import gemini_client
        from google.genai import types  # type: ignore
        from config import settings  # type: ignore

        response = gemini_client._client.models.generate_content(
            model=settings.gemini_light_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=gemini_client._CORE_PROMPT.strip(),
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        gemini_client._track_usage(response)
        return (response.text or "").strip()
    except Exception as e:
        logger.warning("gemini call failed: %s", e)
        return ""


def _try_pii_mask(prompt: str, completion: str) -> tuple[str, str, dict[str, str]]:
    """Best-effort PII masking — fallback to raw if pii_masker unavailable."""
    try:
        from finetune.pii_storage import mask_with_cache
        mp_p, mp_c, mapping = mask_with_cache(prompt, completion)
        return mp_p, mp_c, mapping
    except Exception as e:
        # TODO: pii_masker 不可用時的 fallback — 寫到 logger 不阻塞
        logger.warning("pii mask failed (using raw): %s", e)
        return prompt, completion, {}


def batch_distill(
    messages: list[dict[str, Any]],
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_calls: int = DEFAULT_MAX_CALLS,
    chat_fn: Callable[[str], str] | None = None,
    apply_pii_mask: bool = True,
) -> list[dict[str, Any]]:
    """每 batch_size 條一次 chat call。最多 max_calls 次。

    Args:
      messages: sample_messages() 回的 list
      batch_size: 一次 batch 幾條
      max_calls: 最多幾次 chat call（quota cap）
      chat_fn: 注入用 — 預設真實 call gemini_client。測試時 inject mock
      apply_pii_mask: True 時過 PII masker

    Returns: list of {prompt, completion, source, message_id, ...}
    """
    if chat_fn is None:
        chat_fn = _default_chat_fn

    pairs: list[dict[str, Any]] = []
    calls_made = 0
    n = len(messages)
    for i in range(0, n, batch_size):
        if calls_made >= max_calls:
            logger.info(
                "max_calls cap reached (%d) — stopping at message %d/%d",
                max_calls, i, n,
            )
            break
        batch = messages[i: i + batch_size]
        prompt = _build_batch_prompt(batch)

        try:
            raw = chat_fn(prompt)
        except Exception as e:
            logger.warning("chat_fn raised on batch %d: %s", i // batch_size, e)
            calls_made += 1
            continue
        calls_made += 1

        parsed = _parse_batch_json(raw or "")
        if not parsed:
            logger.warning(
                "batch %d: parse 0 pairs (raw head=%r)",
                i // batch_size, (raw or "")[:80],
            )
            continue

        # 對齊 batch 中的 message_id（按 index match — 寬鬆 fallback）
        for j, item in enumerate(parsed):
            if j >= len(batch):
                break
            src_msg = batch[j]
            user_text = item["msg"]
            reply = item["reply"]

            if apply_pii_mask:
                user_text, reply, _mapping = _try_pii_mask(user_text, reply)

            pairs.append({
                "prompt": user_text,
                "completion": reply,
                "source": "line_self_distill",
                "message_id": src_msg.get("_message_id") or _msg_id(src_msg),
                "sender_hash": hashlib.sha256(
                    (src_msg.get("sender") or "").encode("utf-8")
                ).hexdigest()[:8],
                "distilled_at": int(time.time()),
            })
    logger.info(
        "batch_distill: %d calls made, %d pairs produced", calls_made, len(pairs)
    )
    return pairs


# ─────────────────────────────────────────────────────────────────────────
# 4. progress 持久化 + 加密寫回
# ─────────────────────────────────────────────────────────────────────────
def load_progress(path: Path | str = DEFAULT_PROGRESS) -> set[str]:
    """讀 progress.json → set of processed message_ids。不存在回空 set。"""
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(str(x) for x in data)
        if isinstance(data, dict) and "processed_ids" in data:
            return set(str(x) for x in data["processed_ids"])
    except (json.JSONDecodeError, OSError):
        logger.warning("progress 讀取失敗 — 從零開始: %s", p)
    return set()


def save_progress(
    processed_ids: set[str],
    path: Path | str = DEFAULT_PROGRESS,
) -> None:
    """寫 progress.json。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "processed_ids": sorted(processed_ids),
        "count": len(processed_ids),
        "updated_at": int(time.time()),
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=0), encoding="utf-8")


def append_encrypted(
    pairs: list[dict[str, Any]],
    enc_out: Path | str = DEFAULT_OUTPUT_ENC,
    plaintext_buf: Path | str = DEFAULT_OUTPUT_PLAINTEXT,
) -> int:
    """累積寫回加密檔。

    1. 解密既有 enc 到 plaintext_buf（沒檔就空檔）
    2. append 新 pairs（jsonl）
    3. 加密回 enc_out（delete plaintext_buf）

    Returns: 新累積後的總行數。
    """
    enc_out = Path(enc_out)
    plaintext_buf = Path(plaintext_buf)
    plaintext_buf.parent.mkdir(parents=True, exist_ok=True)

    # 1. 解既有累積（如果存在）
    if enc_out.exists():
        try:
            decrypt_file(enc_out, plaintext_buf)
        except Exception as e:
            logger.warning(
                "既有 %s 解密失敗（從零累積，舊檔保留 %s.bak）: %s",
                enc_out, enc_out, e,
            )
            # 為了避免覆蓋既有 enc，做 backup
            try:
                bak = enc_out.with_suffix(enc_out.suffix + ".bak")
                enc_out.replace(bak)
            except OSError:
                pass
            plaintext_buf.write_text("", encoding="utf-8")
    else:
        plaintext_buf.write_text("", encoding="utf-8")

    # 2. append jsonl
    with plaintext_buf.open("a", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    # 3. 重新加密（delete plaintext_buf 就在 encrypt_file 裡做）
    encrypt_file(plaintext_buf, delete_src=True)

    # 4. 估算總行數（從加密檔再讀回算行數成本太高，這邊用簡單法：
    #     新 pairs 數 + 舊 pairs 數，舊的從 progress 推算太冗長 → 直接 count）
    total = 0
    if enc_out.exists():
        # 暫時解出來算行數，再刪
        tmp_count = Path("/tmp") / f"_count_{enc_out.name}"
        try:
            decrypt_file(enc_out, tmp_count)
            with tmp_count.open("r", encoding="utf-8") as f:
                total = sum(1 for _ in f)
        except Exception:
            total = len(pairs)  # fallback estimate
        finally:
            try:
                if tmp_count.exists():
                    os.unlink(tmp_count)
            except OSError:
                pass

    return total


# ─────────────────────────────────────────────────────────────────────────
# 5. CLI
# ─────────────────────────────────────────────────────────────────────────
def estimate_dry_run(
    rows_count: int,
    sample_n: int,
    batch_size: int,
    max_calls: int,
    quota_limit: int = 20,
) -> dict[str, Any]:
    """印「sample N 條 / batch B → estimated calls × pairs / call = total pairs」。"""
    est_pairs_per_day = max_calls * batch_size
    days_needed = (sample_n + est_pairs_per_day - 1) // est_pairs_per_day if est_pairs_per_day else 0
    return {
        "total_rows": rows_count,
        "sample_n": sample_n,
        "batch_size": batch_size,
        "max_calls": max_calls,
        "est_pairs_per_call": batch_size,
        "est_pairs_per_day": est_pairs_per_day,
        "days_to_finish": days_needed,
        "quota_used": max_calls,
        "quota_limit": quota_limit,
    }


def run(
    sample: int = 1000,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_calls: int = DEFAULT_MAX_CALLS,
    strategy: str = "random",
    seed: int = DEFAULT_SAMPLE_SEED,
    enc_input: Path | str = DEFAULT_ENC_INPUT,
    enc_output: Path | str = DEFAULT_OUTPUT_ENC,
    progress_path: Path | str = DEFAULT_PROGRESS,
    chat_fn: Callable[[str], str] | None = None,
    apply_pii_mask: bool = True,
    tmp_path: Path | str = DEFAULT_TMP_DECRYPTED,
) -> dict[str, Any]:
    """完整 pipeline。回 stats dict。

    finally clause 確保 /tmp/ 明文 ALWAYS 刪除，即使 Gemini 中途炸。
    """
    progress = load_progress(progress_path)
    initial_count = len(progress)
    pairs: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    try:
        rows = decrypt_and_load(enc_input, tmp_path)
        if not rows:
            return {
                "rows": 0, "sampled": 0, "pairs": 0, "calls": 0,
                "progress_before": initial_count, "progress_after": initial_count,
            }

        sampled = sample_messages(
            rows, n=sample, strategy=strategy, seed=seed, skip_ids=progress,
        )
        pairs = batch_distill(
            sampled,
            batch_size=batch_size,
            max_calls=max_calls,
            chat_fn=chat_fn,
            apply_pii_mask=apply_pii_mask,
        )
        if pairs:
            append_encrypted(pairs, enc_out=enc_output)
            for p in pairs:
                progress.add(p["message_id"])
            save_progress(progress, progress_path)

        return {
            "rows": len(rows),
            "sampled": len(sampled),
            "pairs": len(pairs),
            "calls": (len(sampled) + batch_size - 1) // batch_size if sampled else 0,
            "progress_before": initial_count,
            "progress_after": len(progress),
        }
    finally:
        # ALWAYS 清明文 — 中途異常也 cleanup
        cleanup_tmp(tmp_path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=1000,
                    help="random sample N 條家人訊息（預設 1000）")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                    help=f"每 batch 幾條（預設 {DEFAULT_BATCH_SIZE}）")
    ap.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS,
                    help=f"最多幾次 Gemini call（預設 {DEFAULT_MAX_CALLS}，"
                         "quota safe）")
    ap.add_argument("--strategy", choices=["random", "diverse"], default="random",
                    help="random（預設）或 diverse（按 sender 均衡）")
    ap.add_argument("--seed", type=int, default=DEFAULT_SAMPLE_SEED)
    ap.add_argument("--enc-input", default=str(DEFAULT_ENC_INPUT))
    ap.add_argument("--enc-output", default=str(DEFAULT_OUTPUT_ENC))
    ap.add_argument("--progress", default=str(DEFAULT_PROGRESS))
    ap.add_argument("--no-pii-mask", action="store_true",
                    help="skip PII mask（debug 用，雲端跑前千萬不要）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只印 sample N 條、estimated calls × pairs，不 call Gemini")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    if args.dry_run:
        # 只解密 + 數行數 + 估算（不 call Gemini）
        try:
            rows = decrypt_and_load(args.enc_input, DEFAULT_TMP_DECRYPTED)
            est = estimate_dry_run(
                rows_count=len(rows),
                sample_n=args.sample,
                batch_size=args.batch_size,
                max_calls=args.max_calls,
            )
            print(
                f"{est['total_rows']} 訊息 sample {est['sample_n']}，"
                f"batch {est['batch_size']}，max-calls {est['max_calls']} → "
                f"{est['est_pairs_per_day']} 對 pair / day → "
                f"{est['sample_n']} 對需 {est['days_to_finish']} 天"
            )
            print(
                f"Gemini quota 預計 {est['quota_used']} / {est['quota_limit']} req/day"
            )
            return 0
        finally:
            cleanup_tmp(DEFAULT_TMP_DECRYPTED)

    stats = run(
        sample=args.sample,
        batch_size=args.batch_size,
        max_calls=args.max_calls,
        strategy=args.strategy,
        seed=args.seed,
        enc_input=args.enc_input,
        enc_output=args.enc_output,
        progress_path=args.progress,
        apply_pii_mask=not args.no_pii_mask,
    )
    print(
        f"rows={stats['rows']} sampled={stats['sampled']} "
        f"pairs={stats['pairs']} calls={stats['calls']} "
        f"progress: {stats['progress_before']} → {stats['progress_after']}"
    )
    return 0


__all__ = [
    "decrypt_and_load",
    "cleanup_tmp",
    "sample_messages",
    "batch_distill",
    "load_progress",
    "save_progress",
    "append_encrypted",
    "estimate_dry_run",
    "run",
    "_build_batch_prompt",
    "_parse_batch_json",
    "_msg_id",
    "_is_quality_msg",
    "DEFAULT_ENC_INPUT",
    "DEFAULT_OUTPUT_ENC",
    "DEFAULT_PROGRESS",
    "DISTILL_BATCH_PROMPT",
]


if __name__ == "__main__":
    sys.exit(main())
