"""週摘要推播 — launchd 每週日 20:00 TW 觸發。

從 raw_messages 取過去 7 天 bot 的回應，請 Gemini 整理成一則摘要推播給群組。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

import family_interest  # noqa: E402
import gemini_client  # noqa: E402
from line_push_client import line_access_token, try_push_text  # noqa: E402
import memory  # noqa: E402

GROUP_ID = os.environ.get("LINE_ALLOWED_GROUP_ID") or os.environ.get(
    "ALLOWED_GROUP_ID", ""
)


def _push(text: str) -> bool:
    return try_push_text(GROUP_ID, text, timeout=10)


def _render_finance_summary(group_id: str, days: int = 14) -> str:
    """純 SQL 模板輸出 — 家族財經觀點週推。

    第一句判斷句、用名詞短語開頭（避 _ECHO_OPENERS 黑名單）。
    """
    import finance_view_db
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    views = finance_view_db.list_recent(group_id, limit=30)
    if not views:
        return ""

    tw = _ZI("Asia/Taipei")
    counts = finance_view_db.count_by_result(group_id)
    total = sum(counts.values())

    if total == 0:
        return ""

    hit = counts.get("hit", 0)
    miss = counts.get("miss", 0)
    pending = counts.get("pending", 0)

    lines = ["📈 本週家族財經觀點", ""]
    lines.append(
        f"家族至今累積 {total} 條觀點，"
        f"已驗證 {hit} 命中 / {miss} 落空，{pending} 仍待驗。"
    )
    lines.append("")

    now = _dt.now(tz=tw)
    cutoff_ms = int((now.timestamp() - days * 86400) * 1000)
    recent = [v for v in views if v.get("created_at", 0) >= cutoff_ms]
    if not recent:
        lines.append(f"（最近 {days} 天無新觀點）")
        return "\n".join(lines)

    lines.append(f"最近 {days} 天新觀點：")
    for v in recent[:10]:
        label = v.get("ticker") or v.get("macro_topic") or "?"
        d = v.get("direction") or ""
        dir_str = {"bull": "看多", "bear": "看空", "neutral": "持平"}.get(d, "")
        target = ""
        if v.get("target_price"):
            target = f" 目標 {v['target_price']}"
        elif v.get("target_pct"):
            target = f" 目標 {v['target_pct']:+.0f}%"
        result = v.get("validation_result") or ""
        result_str = {"hit": "✅", "miss": "❌", "pending": "⏳", "na": "—"}.get(result, "")
        created = _dt.fromtimestamp(v["created_at"] / 1000, tz=tw).strftime("%m-%d")
        speaker = v.get("display_name") or "家人"
        lines.append(f"• {created} {speaker} {label} {dir_str}{target} {result_str}")
    return "\n".join(lines)


def main() -> int:
    if not GROUP_ID or not line_access_token():
        print("ERR: LINE_ALLOWED_GROUP_ID or LINE_CHANNEL_ACCESS_TOKEN not set")
        return 1

    since_ts = int(time.time()) - 7 * 86400
    all_msgs = memory.get_messages_since(GROUP_ID, since_ts, exclude_bot=False)

    # 只取 bot 的回應
    bot_replies = [text for _, uid, text, _ in all_msgs if uid == "__bot__"]

    if not bot_replies:
        print("本週沒有 bot 回應，跳過摘要推播")
        return 0
    push_failed = False

    # 每次最多取最近 20 則，避免塞爆 prompt
    sample = bot_replies[-20:]
    joined = "\n---\n".join(sample)

    prompt = (
        "以下是 LINE 群組 bot 咪寶這週所有的回應。"
        "請用繁體中文、溫柔可愛的語氣，幫我整理成一則「本週查核/分析摘要」，"
        "讓群組成員知道這週咪寶查了哪些重要的事、有哪些假訊息被揭穿。"
        "格式：條列重點（3~5 點），不超過 200 字，結尾一句溫馨收尾。\n\n"
        f"{joined}"
    )

    # bot 摘要（Gemini 失敗就跳過，但不影響家族熱話）
    try:
        summary = gemini_client.chat(prompt, [], [], None)
        push_text = f"📋 本週咪寶摘要\n\n{summary}"
        if _push(push_text):
            print(f"週摘要已推播 ({len(bot_replies)} 則回應，取最近 {len(sample)} 則)")
        else:
            push_failed = True
            print("ERR 週摘要推播失敗")
    except Exception as e:
        print(f"ERR Gemini bot 摘要 (跳過，繼續家族熱話): {e}")

    # 家族熱話週報（per Q5=B）— 偵測 4 主成員過去 30 天興趣 + 對應新聞
    # 不依賴 Gemini，純 lexicon + RSS，所以 Gemini 爆 quota 不影響
    try:
        family_text = family_interest.render_summary(GROUP_ID, days=30)
        if family_text:
            if _push(family_text[:4900]):
                print(f"家族熱話週報已推播（{len(family_text)} 字）")
            else:
                push_failed = True
                print("ERR 家族熱話週報推播失敗")
        else:
            print("家族熱話無偵測到主題（過去 30 天訊息不足）")
    except Exception as e:
        print(f"ERR 家族熱話: {e}")

    # 家族財經觀點週報（2026-05-18 加）— validator 先跑一次，再純 SQL 推
    try:
        import finance_view_validator
        n_validated = finance_view_validator.run()
        if n_validated:
            print(f"finance_view validator updated {n_validated} views")
    except Exception as e:
        print(f"ERR finance_view validator: {e}")

    try:
        finance_text = _render_finance_summary(GROUP_ID, days=14)
        if finance_text:
            if _push(finance_text[:4900]):
                print(f"家族財經觀點週報已推播（{len(finance_text)} 字）")
            else:
                push_failed = True
                print("ERR 家族財經觀點週報推播失敗")
        else:
            print("家族財經觀點 — 過去 14 天無記錄")
    except Exception as e:
        print(f"ERR 家族財經觀點: {e}")
    return 1 if push_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
