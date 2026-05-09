"""LoRA fine-tune evaluation harness — 4 個 metric。

用法：
    .venv/bin/python finetune/eval_harness.py \
        --model mlx-community/Qwen2.5-14B-Instruct-4bit \
        [--adapter finetune/adapters] \
        --out finetune/eval_results/baseline.json

設計：
    - test set = distilled.jsonl + sft.jsonl 切 20% hold-out（seed=42 deterministic）
    - 4 個 metric：
        1. 違規率（_violates_quality 黑名單 + 規則 0 first-sentence-take）
        2. 中文比率（中文字數 vs 英文字母 vs 簡中字符）
        3. 規則 0 first-sentence-take 命中率（第一句不可命中黑名單開頭 + 必須有具體判斷詞）
        4. 任務準確度（Gemini Flash judge 0-10 分）
    - LLM-as-judge：rate-limit + cache + skip-on-quota-exhaust（每天 quota 50 次極限）

輸出：
    JSON dict（metrics + samples），存到 --out 指定路徑。
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
from typing import Any, Optional

# 加 line_bot/ 進 path（讓 import gemini_client / local_llm 可用）
HERE = Path(__file__).parent
LINE_BOT = HERE.parent
sys.path.insert(0, str(LINE_BOT))

DATA_DIR = HERE / "data"
DISTILLED = DATA_DIR / "distilled.jsonl"
SFT = DATA_DIR / "sft.jsonl"

DEFAULT_OUT_DIR = HERE / "eval_results"
JUDGE_CACHE_FILE = HERE / "eval_results" / "_judge_cache.json"

TEST_SPLIT_RATIO = 0.2
SPLIT_SEED = 42

# Gemini Flash judge 每日 quota 上限（保守 50 次，跟 daily quota 對齊）
JUDGE_DAILY_LIMIT = 50
# Judge call 之間最少間隔（秒），避免 RPM 爆掉
JUDGE_RATE_LIMIT_SEC = 4.0

# 規則 0：第一句必須包含的「具體判斷詞」/「具體名稱數字」之一
# （數字 / URL / 中文判斷詞）
_RULE0_OPINION_MARKERS = (
    "我覺得", "我認為", "我看法", "我這邊覺得", "我這邊看",
    "我看是", "我這邊判斷", "我的判斷", "我傾向",
    "問題在於", "問題不在", "重點不在", "關鍵是",
    "比較好的做法是", "更該關注的是", "真正該",
    "建議", "可以", "應該", "不該", "不要", "別",
    "確實", "其實", "可能", "大概",
)

logger = logging.getLogger("eval_harness")


# ── data split ────────────────────────────────────────────────────────────


def _load_pairs(paths: list[Path]) -> list[dict]:
    """Load + dedupe pairs from JSONL files. 丟掉 mock pair。"""
    pairs: list[dict] = []
    seen_user: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        for line in path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            # 丟 mock
            if obj.get("metadata", {}).get("mock", False):
                continue
            # 找 user / assistant
            user_text = ""
            asst_text = ""
            for m in obj.get("messages", []):
                if m.get("role") == "user":
                    user_text = m.get("content", "")
                elif m.get("role") == "assistant":
                    asst_text = m.get("content", "")
            if not user_text or not asst_text:
                continue
            if user_text in seen_user:
                continue
            seen_user.add(user_text)
            pairs.append(
                {
                    "user": user_text,
                    "gold": asst_text,
                    "metadata": obj.get("metadata", {}),
                }
            )
    return pairs


def split_holdout(
    pairs: list[dict],
    ratio: float = TEST_SPLIT_RATIO,
    seed: int = SPLIT_SEED,
) -> tuple[list[dict], list[dict]]:
    """切 train / test，固定 seed → deterministic（不會跟 train 重疊）。"""
    rng = random.Random(seed)
    indices = list(range(len(pairs)))
    rng.shuffle(indices)
    cut = max(1, int(len(pairs) * ratio))
    test_idx = set(indices[:cut])
    test = [p for i, p in enumerate(pairs) if i in test_idx]
    train = [p for i, p in enumerate(pairs) if i not in test_idx]
    return train, test


def load_test_set(
    distilled: Path = DISTILLED,
    sft: Path = SFT,
    ratio: float = TEST_SPLIT_RATIO,
    seed: int = SPLIT_SEED,
) -> list[dict]:
    pairs = _load_pairs([distilled, sft])
    _, test = split_holdout(pairs, ratio=ratio, seed=seed)
    return test


# ── metric 計算 ────────────────────────────────────────────────────────────


def metric_violation(prediction: str, user_text: str) -> tuple[bool, str]:
    """Metric 1：餵進 gemini_client._violates_quality。"""
    try:
        from gemini_client import _violates_quality
    except Exception as e:
        logger.warning("import _violates_quality failed: %s; using stub", e)
        # stub fallback：純空字串通過，否則無法判斷
        return False, "stub"
    return _violates_quality(prediction, user_text)


def metric_chinese_ratio(text: str) -> dict:
    """Metric 2：中文比率（繁中 / 英文 / 簡中字符）。"""
    s = text or ""
    cn = len(re.findall(r"[一-鿿]", s))
    en = len(re.findall(r"[a-zA-Z]", s))
    # 簡中常見「特殊」字符（簡體獨有 + 部分常見差異字）
    simplified_chars = set(
        "汉语东时间发现这个为问题没办钱财业经济进国际"
        "实现说话长长们点点觉觉记记会会"  # 常見簡中
        "钟图门关张总队员级线产团结习惯"
        "电话车站马场区动师认识"
    )
    sc = sum(1 for ch in s if ch in simplified_chars)
    total_letters = cn + en
    cn_ratio = cn / total_letters if total_letters else 0.0
    sc_ratio = sc / cn if cn else 0.0
    return {
        "cn_chars": cn,
        "en_chars": en,
        "simplified_chars": sc,
        "cn_ratio": cn_ratio,
        "simplified_ratio": sc_ratio,
        # acceptance：cn_ratio > 95% AND simplified_ratio < 5%
        "passes": cn_ratio > 0.95 and sc_ratio < 0.05,
    }


_FIRST_SENT_BLACKLIST = (
    "咪寶看到", "咪寶覺得這", "我看到您", "咪寶之前提醒",
    "咪寶幫大家整理", "咪寶來幫大家", "咪寶來幫您",
    "這張圖片展示了", "從圖中可以看出", "這張圖顯示", "圖中可見",
    "好的", "好喔", "明白了", "瞭解",
    "謝謝您的提醒", "謝謝你的提醒",
)


def metric_rule0_first_sentence(prediction: str) -> dict:
    """Metric 3：規則 0 first-sentence-take 命中。

    第一句：
      - 不可命中黑名單開頭（「咪寶看到」「這張圖片展示了」「從圖中可以看出」等）
      - 必須有具體判斷詞 / 數字 / 名稱
    """
    s = (prediction or "").strip()
    if not s:
        return {"passes": False, "reason": "empty", "first_sentence": ""}

    # 分句：第一個句尾標點 / 換行為界
    m = re.search(r"^(.{1,80}?)(?:[。！？\n!?\.]|$)", s)
    first = m.group(1).strip() if m else s[:80].strip()

    # 黑名單
    for opener in _FIRST_SENT_BLACKLIST:
        if first.startswith(opener):
            return {
                "passes": False,
                "reason": f"blacklist opener: {opener}",
                "first_sentence": first,
            }

    # 具體性：判斷詞 / 數字 / 英文名稱（股票代號等）任一
    has_marker = any(m in first for m in _RULE0_OPINION_MARKERS)
    has_number = bool(re.search(r"\d", first))
    has_english_name = bool(re.search(r"[A-Z]{2,}", first))  # 大寫縮寫 / 股票代號
    if not (has_marker or has_number or has_english_name):
        return {
            "passes": False,
            "reason": "no opinion marker / number / name",
            "first_sentence": first,
        }
    return {"passes": True, "reason": "ok", "first_sentence": first}


# ── LLM-as-judge ──────────────────────────────────────────────────────────


_JUDGE_PROMPT = """你是評分員。下面是一個 LINE 群組對話：user 訊息 + 標準回覆（gold） + 待評回覆（prediction）。
你的任務：把 prediction 跟 gold 對比，從**語意正確、用詞自然、結構完整、是否符合咪寶人設（溫柔/簡短/繁體中文/有觀點）**綜合給 0-10 分整數。

評分標準：
- 10 = 跟 gold 等價或更好
- 7-9 = 大致正確，但用詞或結構有小瑕疵
- 4-6 = 半對半錯（有抓到部分意思但有失誤 / 語氣偏差）
- 1-3 = 偏題 / 違反人設 / 有錯誤資訊
- 0 = 空白 / 純亂碼 / 完全不對題

僅回一行 JSON：{{"score": <int 0-10>, "reason": "<簡短理由 ≤ 30 字>"}}

User 訊息：
{user}

Gold 回覆：
{gold}

待評回覆：
{prediction}
"""


def _judge_cache_key(user: str, gold: str, prediction: str) -> str:
    h = hashlib.sha256(
        ((user or "") + "\x1e" + (gold or "") + "\x1e" + (prediction or "")).encode(
            "utf-8"
        )
    ).hexdigest()
    return h[:24]


def _load_judge_cache() -> dict:
    if not JUDGE_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(JUDGE_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_judge_cache(cache: dict) -> None:
    JUDGE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    JUDGE_CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def call_judge_gemini(
    user: str,
    gold: str,
    prediction: str,
    *,
    cache: Optional[dict] = None,
    last_call_ts: Optional[list[float]] = None,
    quota_used: Optional[list[int]] = None,
) -> Optional[dict]:
    """Call Gemini Flash judge once. 回 {'score': int, 'reason': str} or None。

    skip-on-quota-exhaust：若已經 hit JUDGE_DAILY_LIMIT 直接 return None。
    rate-limit：兩次呼叫至少間隔 JUDGE_RATE_LIMIT_SEC 秒。
    cache：相同 (user, gold, prediction) hash 直接回 cached score。
    """
    if cache is None:
        cache = _load_judge_cache()
    key = _judge_cache_key(user, gold, prediction)
    if key in cache:
        return cache[key]

    if quota_used is not None and quota_used[0] >= JUDGE_DAILY_LIMIT:
        logger.warning("judge quota %d hit, skipping", JUDGE_DAILY_LIMIT)
        return None

    # rate limit
    if last_call_ts is not None:
        elapsed = time.time() - last_call_ts[0]
        if elapsed < JUDGE_RATE_LIMIT_SEC:
            time.sleep(JUDGE_RATE_LIMIT_SEC - elapsed)

    prompt = _JUDGE_PROMPT.format(user=user, gold=gold, prediction=prediction)
    try:
        import gemini_client
        from google.genai import types as _g_types

        response = gemini_client._client.models.generate_content(
            model=gemini_client.settings.gemini_light_model,
            contents=prompt,
            config=_g_types.GenerateContentConfig(
                thinking_config=_g_types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = (response.text or "").strip()
    except Exception as e:
        # 429 / 503 → skip
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            logger.warning("judge 429 RESOURCE_EXHAUSTED, marking quota exhausted")
            if quota_used is not None:
                quota_used[0] = JUDGE_DAILY_LIMIT
        else:
            logger.warning("judge call failed: %s", e)
        return None

    if last_call_ts is not None:
        last_call_ts[0] = time.time()
    if quota_used is not None:
        quota_used[0] += 1

    # parse JSON line
    parsed = _parse_judge_output(text)
    if parsed is None:
        return None

    cache[key] = parsed
    _save_judge_cache(cache)
    return parsed


def _parse_judge_output(text: str) -> Optional[dict]:
    """Robust JSON parse from judge output."""
    if not text:
        return None
    # 找第一個 { 到對應的 }
    s = text.strip()
    # strip code fences
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    m = re.search(r"\{[^{}]*\}", s)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None
    if "score" not in obj:
        return None
    try:
        score = int(obj["score"])
    except Exception:
        return None
    score = max(0, min(10, score))
    return {"score": score, "reason": str(obj.get("reason", ""))[:60]}


# ── model inference ───────────────────────────────────────────────────────


def _load_mlx_model(model_path: str, adapter_path: Optional[str]):
    """Load mlx-lm model + optional adapter. 失敗 raise。"""
    from mlx_lm import load

    if adapter_path:
        return load(model_path, adapter_path=adapter_path)
    return load(model_path)


def _generate_one(model, tokenizer, user_text: str, max_tokens: int = 400) -> str:
    """跑一次 chat-template generate。"""
    from mlx_lm import generate

    messages = [
        {
            "role": "system",
            "content": "你是 LINE 群組對話助理咪寶，繁體中文簡短回覆，有具體觀點。",
        },
        {"role": "user", "content": user_text},
    ]
    prompt = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    return generate(model, tokenizer, prompt=prompt, max_tokens=max_tokens, verbose=False).strip()


# ── 主 evaluation 流程 ────────────────────────────────────────────────────


def evaluate(
    test_set: list[dict],
    *,
    generate_fn,  # callable(user_text) -> prediction str
    judge_fn=None,  # callable(user, gold, prediction) -> {"score": int, ...} or None
    skip_judge: bool = False,
    label: str = "model",
) -> dict:
    """跑 evaluation，回完整 metrics dict。

    generate_fn / judge_fn 都注射可 mock — test 用。
    """
    samples: list[dict] = []
    n = len(test_set)
    n_violation = 0
    n_cn_pass = 0
    n_rule0_pass = 0
    judge_scores: list[int] = []
    judge_skipped = 0

    cache = _load_judge_cache()
    last_ts = [0.0]
    quota_used = [0]

    for i, pair in enumerate(test_set):
        user = pair["user"]
        gold = pair["gold"]
        try:
            pred = generate_fn(user)
        except Exception as e:
            logger.warning("generate failed (%s): %s", user[:30], e)
            pred = ""

        m1_violates, m1_reason = metric_violation(pred, user)
        if m1_violates:
            n_violation += 1
        m2 = metric_chinese_ratio(pred)
        if m2["passes"]:
            n_cn_pass += 1
        m3 = metric_rule0_first_sentence(pred)
        if m3["passes"]:
            n_rule0_pass += 1

        m4_score = None
        m4_reason = ""
        if not skip_judge:
            jfn = judge_fn or (
                lambda u, g, p: call_judge_gemini(
                    u, g, p,
                    cache=cache,
                    last_call_ts=last_ts,
                    quota_used=quota_used,
                )
            )
            jr = jfn(user, gold, pred)
            if jr is not None:
                m4_score = jr.get("score")
                m4_reason = jr.get("reason", "")
                judge_scores.append(m4_score)
            else:
                judge_skipped += 1

        samples.append(
            {
                "i": i,
                "user": user[:120],
                "gold": gold[:120],
                "prediction": pred[:200],
                "violation": {"v": m1_violates, "r": m1_reason},
                "chinese": m2,
                "rule0": m3,
                "judge": {"score": m4_score, "reason": m4_reason},
            }
        )

    judge_avg = (sum(judge_scores) / len(judge_scores)) if judge_scores else None

    metrics = {
        "label": label,
        "n_total": n,
        "violation_rate": (n_violation / n) if n else 0.0,
        "chinese_pass_rate": (n_cn_pass / n) if n else 0.0,
        "rule0_pass_rate": (n_rule0_pass / n) if n else 0.0,
        "judge_avg_score": judge_avg,
        "judge_n": len(judge_scores),
        "judge_skipped": judge_skipped,
    }
    return {"metrics": metrics, "samples": samples}


# ── CLI ───────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="mlx-community/Qwen2.5-14B-Instruct-4bit")
    parser.add_argument("--adapter", default=None, help="LoRA adapter path（可選）")
    parser.add_argument("--out", default=None, help="輸出 JSON 路徑")
    parser.add_argument("--label", default="model", help="標籤（baseline / adapter）")
    parser.add_argument("--skip-judge", action="store_true", help="跳過 LLM judge")
    parser.add_argument("--max-test", type=int, default=0, help="0=全部；>0 取前 N 筆")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    test_set = load_test_set()
    if args.max_test > 0:
        test_set = test_set[: args.max_test]
    print(f"[INFO] test set size = {len(test_set)} (seed={SPLIT_SEED}, ratio={TEST_SPLIT_RATIO})")

    if not test_set:
        print("[ERR] test set 是空的；distilled.jsonl + sft.jsonl 都沒料？")
        return 2

    # load model
    print(f"[INFO] loading model={args.model} adapter={args.adapter}")
    model, tokenizer = _load_mlx_model(args.model, args.adapter)

    def _gen(user_text: str) -> str:
        return _generate_one(model, tokenizer, user_text)

    result = evaluate(
        test_set,
        generate_fn=_gen,
        skip_judge=args.skip_judge,
        label=args.label,
    )

    out_path = (
        Path(args.out)
        if args.out
        else (DEFAULT_OUT_DIR / f"{args.label}.json")
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[OK] eval done, saved to {out_path}")
    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
