"""Benchmark Qwen2.5-14B vs 3B（如已 cache）。

對 5 道題比較：response time / 字數 / 主觀品質（heuristic 1-5 分）。
記憶體 peak 用 psutil 抓。最後印 summary 表 + 寫 markdown 結果檔。
"""
from __future__ import annotations
import argparse
import datetime as dt
import gc
import time
from typing import Optional

try:
    import psutil
    _PROC = psutil.Process()

    def rss_gb() -> float:
        return _PROC.memory_info().rss / (1024**3)
except Exception:
    def rss_gb() -> float:
        return -1.0


TESTS = [
    ("簡單對話", "你好嗎？"),
    ("程式", "用 Python 寫一個函式找出 100 以內的所有質數，並回傳 list。"),
    ("知識", "為什麼天空是藍的？請簡短解釋。"),
    ("推理", "分析 SOXL 這檔 3 倍槓桿 ETF 的 vol decay 現象，為什麼長期持有不利？"),
    ("簡單問答", "100 美金等於多少台幣？大約即可。"),
]


def quality_score(category: str, q: str, ans: Optional[str]) -> int:
    """Heuristic 主觀品質 1-5 分。
    沒回答 / 太短 → 低分；空泛廢話 → 中低；有具體內容 → 高分。
    """
    if not ans:
        return 1
    text = ans.strip()
    chars = len(text)
    if chars < 10:
        return 1
    score = 3  # baseline

    # 通用加分：有結構（換行 / 列點）
    if "\n" in text or "•" in text or "1." in text or "- " in text:
        score += 1

    # 類別特定 heuristic
    if category == "簡單對話":
        # 太囉嗦扣分
        if 5 <= chars <= 80:
            score += 1
        if chars > 200:
            score -= 1
    elif category == "程式":
        if "def " in text and ("for " in text or "while " in text):
            score += 1
        if "return" in text:
            score += 1
        if "```" in text or "    " in text:  # code block / indent
            score += 0
    elif category == "知識":
        keywords = ("散射", "Rayleigh", "波長", "藍光", "紫光", "氣體", "光線")
        if sum(1 for k in keywords if k in text) >= 2:
            score += 1
        if chars > 100:
            score += 1
    elif category == "推理":
        keywords = ("槓桿", "波動", "decay", "重置", "每日", "震盪", "複利", "損耗", "rebalance")
        hits = sum(1 for k in keywords if k.lower() in text.lower())
        if hits >= 3:
            score += 2
        elif hits >= 1:
            score += 1
        if chars > 200:
            score += 0  # 推理需要長度但不要過度獎勵
    elif category == "簡單問答":
        # 應該包含數字（匯率 ~30）
        import re
        if re.search(r"3[0-2]", text):
            score += 1
        if chars > 300:  # 過度囉嗦
            score -= 1

    return max(1, min(5, score))


def bench_one_model(model_name: str) -> dict:
    """載入指定 model，跑 5 題，回傳 dict 結果。"""
    print(f"\n{'='*60}\n[BENCH] {model_name}\n{'='*60}")
    print(f"[start] RSS={rss_gb():.2f} GB")

    # 強制用指定 model
    import os
    os.environ["LOCAL_LLM_MODEL"] = model_name

    # 清掉 module cache，重新載入
    import importlib
    import sys
    for mod in ("local_llm", "local_llm_config"):
        if mod in sys.modules:
            del sys.modules[mod]
    gc.collect()

    import local_llm  # noqa: E402

    t0 = time.time()
    ok = local_llm._ensure_loaded()
    load_time = time.time() - t0
    actual = local_llm.loaded_model_name()
    rss_after_load = rss_gb()
    print(f"[load] ok={ok} actual_model={actual} took={load_time:.1f}s RSS={rss_after_load:.2f} GB")

    if not ok:
        return {
            "requested": model_name,
            "actual": None,
            "load_ok": False,
            "load_time": load_time,
            "rss_after_load_gb": rss_after_load,
            "results": [],
            "peak_rss_gb": rss_after_load,
        }

    results = []
    peak = rss_after_load
    for category, q in TESTS:
        t0 = time.time()
        try:
            ans = local_llm.chat(q)
        except Exception as e:
            ans = f"<ERROR: {e}>"
        dt_s = time.time() - t0
        rss = rss_gb()
        peak = max(peak, rss)
        chars = len(ans) if ans else 0
        score = quality_score(category, q, ans)
        snippet = (ans or "")[:150].replace("\n", " ")
        print(f"\n[{category}] {q}")
        print(f"  time={dt_s:.1f}s chars={chars} quality={score}/5 RSS={rss:.2f}GB")
        print(f"  ans> {snippet}{'...' if ans and len(ans) > 150 else ''}")
        results.append({
            "category": category,
            "q": q,
            "ans": ans,
            "time_s": dt_s,
            "chars": chars,
            "quality": score,
        })

    return {
        "requested": model_name,
        "actual": actual,
        "load_ok": True,
        "load_time": load_time,
        "rss_after_load_gb": rss_after_load,
        "results": results,
        "peak_rss_gb": peak,
    }


def fmt_summary(bench_results: list[dict]) -> str:
    """產生 markdown 報告。"""
    today = dt.date.today().isoformat()
    lines = []
    lines.append(f"# Local LLM Benchmark — {today}")
    lines.append("")
    lines.append(f"Mac 32 GB. 比較 14B vs 3B（如已 cache）。\n")

    for b in bench_results:
        lines.append(f"## {b['requested']}")
        if not b["load_ok"]:
            lines.append(f"- 載入失敗")
            lines.append("")
            continue
        actual = b["actual"]
        fallback_note = "" if actual == b["requested"] else f" (fallback from {b['requested']})"
        lines.append(f"- 實際載入: `{actual}`{fallback_note}")
        lines.append(f"- 載入耗時: {b['load_time']:.1f}s")
        lines.append(f"- RSS 載入後: {b['rss_after_load_gb']:.2f} GB")
        lines.append(f"- RSS peak: {b['peak_rss_gb']:.2f} GB")
        lines.append("")
        lines.append("| 類別 | 題目 | time(s) | 字數 | 品質 1-5 |")
        lines.append("|---|---|---|---|---|")
        total_t = 0.0
        total_q = 0
        for r in b["results"]:
            q_disp = r["q"][:30] + ("..." if len(r["q"]) > 30 else "")
            lines.append(f"| {r['category']} | {q_disp} | {r['time_s']:.1f} | {r['chars']} | {r['quality']} |")
            total_t += r["time_s"]
            total_q += r["quality"]
        avg_q = total_q / max(1, len(b["results"]))
        lines.append("")
        lines.append(f"- 總時間: {total_t:.1f}s, 平均品質: {avg_q:.2f}/5")
        lines.append("")
        lines.append("### 範例回應（節錄前 200 字）")
        for r in b["results"]:
            ans = r["ans"] or "<no answer>"
            snippet = ans[:200].replace("\n", " ")
            lines.append(f"- **[{r['category']}]** {snippet}{'...' if len(ans) > 200 else ''}")
        lines.append("")

    # 比較表
    if len(bench_results) >= 2:
        lines.append("## 比較表 (品質 / 時間)")
        lines.append("")
        header = "| 類別 | " + " | ".join(b["actual"] or b["requested"] for b in bench_results) + " |"
        sep = "|" + "---|" * (len(bench_results) + 1)
        lines.append(header)
        lines.append(sep)
        n = len(bench_results[0]["results"])
        for i in range(n):
            cat = bench_results[0]["results"][i]["category"]
            cells = []
            for b in bench_results:
                if i < len(b["results"]):
                    r = b["results"][i]
                    cells.append(f"{r['quality']}/5 ({r['time_s']:.1f}s)")
                else:
                    cells.append("-")
            lines.append(f"| {cat} | " + " | ".join(cells) + " |")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models",
        nargs="+",
        default=["mlx-community/Qwen2.5-14B-Instruct-4bit"],
        help="要 benchmark 的 model list（高 → 低）",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="輸出 markdown 路徑，預設 bench_results_{date}.md",
    )
    args = parser.parse_args()

    bench_results = []
    for m in args.models:
        try:
            r = bench_one_model(m)
            bench_results.append(r)
        except Exception as e:
            print(f"[FATAL] bench {m} crashed: {e}")
            bench_results.append({
                "requested": m,
                "actual": None,
                "load_ok": False,
                "load_time": 0.0,
                "rss_after_load_gb": rss_gb(),
                "results": [],
                "peak_rss_gb": rss_gb(),
                "error": str(e),
            })

    md = fmt_summary(bench_results)
    out = args.out or f"/Users/andrew/Desktop/andrew/Data_engineer/line_bot/bench_results_{dt.date.today().isoformat()}.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"\n\n=== Summary written to {out} ===\n")
    print(md)


if __name__ == "__main__":
    main()
