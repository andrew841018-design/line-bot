"""
Layer 3：週期性自我檢討的 orchestrator。

流程：
1. 從 memory 撈出過去 N 天的原始訊息（不含 bot 自貼）
2. 組成 dialogue，丟給 gemini_client.weekly_review()（走 flash-lite）
3. 把產出寫進 rule_drafts 表（先清掉舊 draft）
4. 格式化成一份報告文字，供 /檢討 回覆或 weekly_review.py CLI 推給 LINE

兩個公開 API：
- run_weekly_review(group_id, days=7) -> (report_text, drafts_added)
- adopt_drafts(group_id, spec) -> (promoted_ids, skipped)

adopt_drafts 的 spec 語意：
- "全部" / "all"  → 把所有 drafts 升級為 filter_rules
- "無" / "none"   → 全部丟掉
- "1 3 5" / "1,3" → 指定編號升級（其他保留）

採用完會把被採用的 drafts 從 rule_drafts 刪掉（未採用的保留，等下次 /採用 或被下次 /檢討 蓋掉）。
"""
from __future__ import annotations

import logging
import time

import gemini_client
import memory

logger = logging.getLogger(__name__)

# 少於這個訊息數就不跑 Gemini（省額度、避免空檢討）
_MIN_MESSAGES_FOR_REVIEW = 10

# 丟進 prompt 的對話最長字元數（避免一次爆 context）
_DIALOGUE_CHAR_LIMIT = 12000


def _format_dialogue(
    messages: list[tuple[str, str | None, str, int]],
) -> str:
    """把 raw_messages rows 壓成 prompt 用的純文字（只含使用者訊息）。"""
    lines = []
    total = 0
    for _mid, _uid, text, _ts in messages:
        if not text:
            continue
        snippet = text.strip()
        if not snippet:
            continue
        line = f"使用者：{snippet}"
        total += len(line) + 1
        if total > _DIALOGUE_CHAR_LIMIT:
            lines.append("...（後續訊息已截斷）")
            break
        lines.append(line)
    return "\n".join(lines)


def _format_dialogue_with_bot(
    messages: list[tuple[str, str | None, str, int]],
) -> str:
    """把 raw_messages rows 壓成含 bot 回覆的純文字（給人設檢討用）。"""
    lines = []
    total = 0
    for _mid, uid, text, _ts in messages:
        if not text or not text.strip():
            continue
        label = "bot" if uid == "__bot__" else "使用者"
        line = f"{label}：{text.strip()}"
        total += len(line) + 1
        if total > _DIALOGUE_CHAR_LIMIT:
            lines.append("...（後續訊息已截斷）")
            break
        lines.append(line)
    return "\n".join(lines)


def run_weekly_review(
    group_id: str, days: int = 7
) -> tuple[str, list[dict]]:
    """跑一次檢討，回傳 (報告文字, 新寫入的 drafts list)。"""
    since_ts = int(time.time()) - days * 86400
    messages = memory.get_messages_since(group_id, since_ts, exclude_bot=True)
    msg_count = len(messages)
    existing_rules = memory.list_filter_rules(group_id)

    header = (
        f"📋 過去 {days} 天檢討報告\n"
        f"- 訊息數：{msg_count}\n"
        f"- 目前規則數：{len(existing_rules)}"
    )

    if msg_count < _MIN_MESSAGES_FOR_REVIEW:
        return (
            f"{header}\n\n"
            f"訊息量太少（< {_MIN_MESSAGES_FOR_REVIEW}），這次不產生建議。",
            [],
        )

    dialogue_text = _format_dialogue(messages)
    suggestions = gemini_client.weekly_review(dialogue_text, existing_rules)

    if not suggestions:
        rule_report = "沒有抓到值得新增的規則。"
        saved = []
    else:
        # 先清掉舊 drafts，避免編號亂跳（每次檢討只保留最新建議）
        memory.clear_rule_drafts(group_id)
        saved: list[dict] = []
        for s in suggestions:
            draft_id = memory.add_rule_draft(
                group_id=group_id,
                kind=s["kind"],
                pattern=s["pattern"],
                reason=s.get("reason", ""),
            )
            if draft_id:
                saved.append({
                    "draft_id": draft_id,
                    "kind": s["kind"],
                    "pattern": s["pattern"],
                    "reason": s.get("reason", ""),
                })
        rule_report = _format_rule_section(saved)

    # ── 人設檢討 ──────────────────────────────────────────────────────
    persona_report = ""
    all_messages = memory.get_messages_since(group_id, since_ts, exclude_bot=False)
    bot_dialogue = _format_dialogue_with_bot(all_messages)
    if bot_dialogue:
        persona_result = gemini_client.persona_review(bot_dialogue)
        ex_count = 0
        for ex in persona_result.get("examples", []):
            memory.add_persona_note(
                group_id, "example", ex["scenario"], ex["response"]
            )
            ex_count += 1
        cor_count = 0
        for cor in persona_result.get("corrections", []):
            memory.add_persona_note(
                group_id, "correction", cor["scenario"], cor["rule"]
            )
            cor_count += 1
        if ex_count or cor_count:
            persona_report = (
                f"\n\n🐱 人設學習\n"
                f"- 學到 {ex_count} 個好範例\n"
                f"- 記住 {cor_count} 條糾正"
            )

    full_report = f"{header}\n\n{rule_report}{persona_report}"
    return (full_report, saved)


def _format_rule_section(drafts: list[dict]) -> str:
    if not drafts:
        return "沒有抓到值得新增的規則。"
    lines = [f"💡 建議新增 {len(drafts)} 條規則："]
    for d in drafts:
        tag = "不要回" if d["kind"] == "skip" else "要回"
        lines.append(f"{d['draft_id']}. [{tag}] {d['pattern']}")
        if d.get("reason"):
            lines.append(f"   理由：{d['reason']}")
    lines.append("")
    lines.append("回覆指令：")
    lines.append("/採用 1 2   → 採用指定編號")
    lines.append("/採用 全部  → 全部採用")
    lines.append("/採用 無    → 全部丟掉")
    return "\n".join(lines)


def adopt_drafts(
    group_id: str, spec: str
) -> tuple[list[int], str]:
    """採用（或拋棄）drafts。回傳 (被升級為 rule 的 draft_id list, 人類訊息)。"""
    spec = spec.strip()
    drafts = memory.list_rule_drafts(group_id)
    if not drafts:
        return ([], "目前沒有待採用的建議。跑 /檢討 先產生一份。")

    # 解析 spec
    if spec in ("全部", "all", "ALL"):
        selected_ids = [d["draft_id"] for d in drafts]
    elif spec in ("無", "none", "NONE"):
        memory.clear_rule_drafts(group_id)
        return ([], f"已丟棄全部 {len(drafts)} 條建議。")
    else:
        selected_ids = []
        for token in spec.replace(",", " ").split():
            try:
                selected_ids.append(int(token))
            except ValueError:
                continue
        if not selected_ids:
            return ([], "看不懂你要採用哪幾條。用 /採用 1 2 或 /採用 全部。")

    # 做實際升級
    promoted: list[int] = []
    for did in selected_ids:
        d = memory.get_rule_draft(group_id, did)
        if not d:
            continue
        rule_id = memory.add_filter_rule(
            group_id=group_id,
            kind=d["kind"],
            pattern=d["pattern"],
            source="learned",
        )
        if rule_id:
            promoted.append(did)

    # 把被採用的 draft 刪掉（沒被採用的保留）
    for did in promoted:
        memory.delete_rule_draft(group_id, did)

    if not promoted:
        return ([], "沒有成功採用任何建議（編號可能不存在）。")

    remaining = len(drafts) - len(promoted)
    msg = f"已採用 {len(promoted)} 條規則（編號 {', '.join(map(str, promoted))}）。"
    if remaining:
        msg += f" 還剩 {remaining} 條建議未處理。"
    return (promoted, msg)
