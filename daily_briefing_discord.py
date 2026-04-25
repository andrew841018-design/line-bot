#!/usr/bin/env python3
"""每日 10:00 自動匯報 → Discord DM"""

import os
import sys
import subprocess
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# 路徑設定
BASE = Path("/Users/andrew/Desktop/andrew/Data_engineer")
LINE_BOT_DIR = BASE / "line_bot"
PROJECT_DIR = BASE / "project"
DEP_CODE_DIR = PROJECT_DIR / "dependent_code"

sys.path.insert(0, str(LINE_BOT_DIR))
sys.path.insert(
    0, str(DEP_CODE_DIR)
)  # DEP_CODE_DIR 優先，避免 line_bot/config.py 蓋掉 dependent_code/config.py

from notify_discord import send_dm


# ── 1. 每日待辦 ──────────────────────────────────────────────────────────────


def daily_todos() -> str:
    today = datetime.now().strftime("%m/%d")
    weekday = ["一", "二", "三", "四", "五", "六", "日"][datetime.now().weekday()]
    return (
        f"📌 **每日待辦** ({today} 週{weekday})\n"
        "• Mock interview 做了嗎？\n"
        "• IBM 影片看了 30 分鐘嗎？\n"
        "• 學車相關影片看了一則嗎？\n"
        "• Code review 做了嗎？\n"
        "• 小說看了 1.5 小時嗎？\n"
        "• 重訓了 1 小時嗎？\n"
        "• 讀經禱告了嗎？\n"
        "• 寫日記了嗎？"
    )


# ── 2. 爬蟲狀態 ──────────────────────────────────────────────────────────────


def crawler_status() -> str:
    try:
        from pg_helper import get_pg

        with get_pg() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.source_name,
                           COUNT(a.article_id) AS total,
                           COUNT(a.article_id) FILTER (WHERE a.scraped_at >= NOW() - INTERVAL '24 hours') AS new_24h,
                           COUNT(a.article_id) FILTER (WHERE a.scraped_at >= NOW() - INTERVAL '72 hours') AS new_72h
                    FROM sources s
                    LEFT JOIN articles a ON a.source_id = s.source_id
                    GROUP BY s.source_name
                    ORDER BY total DESC
                """)
                rows = cur.fetchall()

        lines = ["🕷️ **爬蟲狀態**"]
        for name, total, h24, h72 in rows:
            if h24 == 0:
                icon = "🔴" if h72 == 0 else "🟡"
                lines.append(f"{icon} {name}: 總 {total} | 24h +{h24} | 72h +{h72}")
        if len(lines) == 1:
            return ""  # 全部正常 → 不顯示
        return "\n".join(lines)
    except Exception as e:
        return f"🕷️ **爬蟲狀態** ⚠️ 查詢失敗：{e}"


# ── 3. LINE Bot 狀態 ──────────────────────────────────────────────────────────


def line_bot_status() -> str:
    lines = []
    # uvicorn.log 尾部
    log_path = LINE_BOT_DIR / "uvicorn.log"
    try:
        result = subprocess.run(
            ["tail", "-20", str(log_path)], capture_output=True, text=True
        )
        tail = result.stdout
        errors = [l for l in tail.splitlines() if "ERROR" in l or "error" in l.lower()]
        if errors:
            lines.append("🤖 **LINE Bot** 🔴 有 ERROR")
            for e in errors[-3:]:
                lines.append(f"  {e.strip()}")
    except Exception as e:
        lines.append(f"🤖 **LINE Bot** ⚠️ log 讀取失敗：{e}")

    # DB 活躍度：只在 >72h 完全沒活動才警示（紅）
    db_path = LINE_BOT_DIR / "line_bot.db"
    if db_path.exists():
        mtime = datetime.fromtimestamp(db_path.stat().st_mtime)
        age_h = (datetime.now() - mtime).total_seconds() / 3600
        if age_h > 72:
            if not lines:
                lines.append("🤖 **LINE Bot**")
            lines.append(f"🔴 DB 最後更新 {age_h:.0f}h 前")
    return "\n".join(lines)


# ── 4. Git 狀態 ───────────────────────────────────────────────────────────────


def git_status() -> str:
    repos = {
        "主專案": str(PROJECT_DIR),
        "LINE bot": str(LINE_BOT_DIR),
    }
    lines = ["📦 **Git 狀態**"]
    for name, path in repos.items():
        try:
            uncommitted = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=path,
                capture_output=True,
                text=True,
            ).stdout.strip()
            unpushed = subprocess.run(
                ["git", "log", "@{u}..", "--oneline"],
                cwd=path,
                capture_output=True,
                text=True,
            ).stdout.strip()
            u_count = len(uncommitted.splitlines()) if uncommitted else 0
            p_count = len(unpushed.splitlines()) if unpushed else 0
            if u_count > 0 or p_count > 0:
                lines.append(f"🟡 {name}: 未 commit {u_count} 筆 | unpushed {p_count}")
        except Exception as e:
            lines.append(f"⚠️ {name}: {e}")
    if len(lines) == 1:
        return ""  # 全部乾淨 → 不顯示
    return "\n".join(lines)


# ── 5. 系統 & Pipeline ────────────────────────────────────────────────────────


def system_status() -> str:
    lines = ["🖥️ **系統 & Pipeline**"]

    # logs 清理一律執行，但不顯示（常態維護）
    log_files = sorted(
        (PROJECT_DIR / "logs").glob("*"), key=lambda f: f.stat().st_mtime
    )
    if len(log_files) > 30:
        for f in log_files[: len(log_files) - 30]:
            f.unlink()

    # 磁碟：只有 > 90% 才顯示
    df = subprocess.run(
        ["df", "-h", "/Users/andrew"], capture_output=True, text=True
    ).stdout
    for line in df.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 5:
            use_pct = int(parts[4].replace("%", ""))
            if use_pct > 90:
                lines.append(f"🔴 磁碟：{parts[4]} 使用（可用 {parts[3]}）")

    # ETL log：只有 ERROR 才顯示（WARNING 不顯示）
    today_log = (
        PROJECT_DIR / "logs" / f"wayback_{datetime.now().strftime('%Y%m%d')}.log"
    )
    if today_log.exists():
        content = today_log.read_text(errors="ignore")
        err_count = content.count("ERROR")
        if err_count > 0:
            lines.append(f"🔴 今日 ETL log：{err_count} ERROR")

    if len(lines) == 1:
        return ""
    return "\n".join(lines)


# ── 6. 待辦 (CLAUDE.md) ───────────────────────────────────────────────────────


def next_todos() -> str:
    claude_md = PROJECT_DIR / "CLAUDE.md"
    try:
        content = claude_md.read_text(errors="ignore")
        # 找「下次繼續」section
        idx = content.rfind("下次繼續")
        if idx == -1:
            return ""
        section = content[idx : idx + 500].splitlines()
        lines = ["📋 **下次繼續**"]
        for line in section[1:8]:
            if line.strip():
                lines.append(line)
        return "\n".join(lines)
    except Exception:
        return ""


# ── 7. LINE bot 功能建議（Gemini 生成 + 7 天去重 + 沒適合就直說）──────────────

_SUGGESTION_HISTORY = LINE_BOT_DIR / "suggestion_history.json"


def line_bot_suggestions() -> str:
    import json
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=LINE_BOT_DIR / ".env")

    memory_path = Path(
        "/Users/andrew/.claude/projects/-Users-andrew-Desktop-andrew-Data-engineer/memory/project_line_bot_feature_suggestions.md"
    )

    try:
        memory_text = memory_path.read_text(errors="ignore")
    except Exception:
        memory_text = ""

    # 近 7 天已推薦過的
    history = []
    if _SUGGESTION_HISTORY.exists():
        try:
            raw = json.loads(_SUGGESTION_HISTORY.read_text())
            cutoff = datetime.now() - timedelta(days=7)
            history = [h for h in raw if datetime.fromisoformat(h["date"]) >= cutoff]
        except Exception:
            history = []

    # 近 3 天 LINE bot 對話樣本（抽 30 則）
    recent_msgs = []
    try:
        conn = sqlite3.connect(str(LINE_BOT_DIR / "line_bot.db"))
        cur = conn.cursor()
        three_days_ago_ms = int((datetime.now() - timedelta(days=3)).timestamp() * 1000)
        cur.execute(
            "SELECT user_id, text FROM raw_messages WHERE created_at > ? AND user_id != '__bot__' ORDER BY created_at DESC LIMIT 30",
            (three_days_ago_ms,),
        )
        recent_msgs = [f"{uid[:6]}: {text[:80]}" for uid, text in cur.fetchall()]
        conn.close()
    except Exception:
        pass

    # 解析 memory 三個 section
    def _parse_section(text: str, header: str) -> list:
        idx = text.find(header)
        if idx == -1:
            return []
        rest = text[idx + len(header) :]
        # 下一個 ## 標題之前為止
        end = rest.find("\n## ")
        chunk = rest[:end] if end != -1 else rest
        items = []
        for line in chunk.splitlines():
            s = line.strip().lstrip("- ").strip()
            if s and not s.startswith("#") and not s.startswith("###"):
                # 抽出 ** 粗體標題，或整行
                if "**" in s:
                    title = s.split("**")[1]
                else:
                    title = s.split("：")[0].split("（")[0]
                if title and len(title) > 1:
                    items.append(title)
        return items

    done_list = _parse_section(memory_text, "## 已執行")
    skipped_list = _parse_section(memory_text, "## 已略過")
    pending_list = _parse_section(memory_text, "## 待建議")

    # 黑名單：已執行 + 已略過 + 近 7 天推薦過
    blacklist = list(set(done_list + skipped_list + [h["title"] for h in history]))

    prompt = f"""你是 LINE bot 的產品顧問。任務：根據近 3 天群組對話，判斷今天有沒有一個「真的值得 Andrew 注意」的新功能建議。

【近 3 天群組對話樣本】
{chr(10).join(recent_msgs) if recent_msgs else "(無)"}

【待建議池（可直接挑選，或基於對話生成新的）】
{json.dumps(pending_list, ensure_ascii=False)}

【🚫 絕對禁止推薦（已實作過或已拒絕，近 N 天推過的）】
{json.dumps(blacklist, ensure_ascii=False)}

規則：
1. 建議內容**不可與黑名單任一項語意相近**（例：黑名單有「靜音時段」，就不能提「夜間不回應」「睡覺時間靜音」等變體）
2. 只在真的觀察到「對話中有未解決需求」或「現有規則有漏洞」時才提建議
3. 不要硬湊，沒合適就回 null
4. 建議要具體（標題 + 一句話理由）

回 JSON，格式：
{{"suggestion": null 或 {{"title": "標題", "reason": "一句話理由"}}}}"""

    # gemini-2.5-flash 跟 LINE bot 共用 quota，容易 429
    # 依序嘗試：flash-lite（quota 高）→ flash（fallback）→ 本地 fallback
    suggestion = None
    last_error = None
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

        for model_name in ("gemini-2.5-flash-lite", "gemini-2.5-flash"):
            try:
                resp = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.3,
                    ),
                )
                data = json.loads(resp.text)
                suggestion = data.get("suggestion")
                last_error = None
                break
            except Exception as e:
                last_error = e
                if "429" not in str(e) and "RESOURCE_EXHAUSTED" not in str(e):
                    break  # 非 quota 錯誤，不 retry 其他模型
    except Exception as e:
        last_error = e

    # Gemini 全部失敗 → 本地 fallback：從 pending_list 挑一個不在黑名單的
    if last_error is not None:
        candidates = [p for p in pending_list if p not in blacklist]
        if candidates:
            import random

            title = random.choice(candidates)
            suggestion = {"title": title, "reason": "（本地挑選，Gemini quota 用盡）"}
        else:
            return "💡 **LINE bot 建議**：今天沒有新建議"

    if not suggestion:
        return "💡 **LINE bot 建議**：今天沒有新建議"

    # 寫入歷史
    history.append({"date": datetime.now().isoformat(), "title": suggestion["title"]})
    try:
        _SUGGESTION_HISTORY.write_text(
            json.dumps(history, ensure_ascii=False, indent=2)
        )
    except Exception:
        pass

    return f"💡 **LINE bot 建議**：{suggestion['title']} — {suggestion['reason']}"


# ── 8. AI+DE 職缺建議 ─────────────────────────────────────────────────────────


def job_search_summary() -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    report = Path(f"/Users/andrew/Desktop/andrew/job_search/{today}.md")
    if not report.exists():
        return "💼 **今日職缺**：今天沒有適合的職缺"
    try:
        text = report.read_text(errors="ignore")

        # 取今日掃描結果摘要（第一個 ## 📊 段落）
        summary_lines = []
        in_summary = False
        for line in text.splitlines():
            if "## 📊 今日掃描結果" in line:
                in_summary = True
                continue
            if in_summary:
                if line.startswith("##") or line.startswith("---"):
                    break
                if line.strip().startswith("- ") or line.strip().startswith("> "):
                    summary_lines.append(line.strip())

        # 取必投職缺（DE + AI 合併，只抓表格資料行）
        must_apply = []
        in_must = False
        for line in text.splitlines():
            if "🔴 必投" in line:
                section_label = "DE" if "DE" in line else "AI"
                in_must = True
                continue
            if in_must:
                if line.startswith("## ") or line.startswith("---"):
                    in_must = False
                    continue
                # 表格資料行：以 | 數字 | 開頭
                stripped = line.strip()
                if stripped.startswith("|") and stripped[1:].strip()[:1].isdigit():
                    cols = [c.strip() for c in stripped.split("|") if c.strip()]
                    # 欄位順序：#|公司|職位|薪資|刊登|Score|連結|...
                    if len(cols) >= 7:
                        company = cols[1][:12]
                        title = cols[2][:18]
                        score = cols[5]
                        link_col = cols[6]
                        url = ""
                        import re

                        m = re.search(r"\(https?://[^\)]+\)", link_col)
                        if m:
                            url = m.group(0)[1:-1]
                        must_apply.append(f"• {company} — {title} (S{score}) {url}")

        if not summary_lines and not must_apply:
            return "💼 **今日職缺**：今天沒有適合的職缺"

        lines = ["💼 **今日職缺 (AI+DE)**"]
        lines += summary_lines[:4]
        if must_apply:
            lines.append("**🔴 必投：**")
            lines += must_apply[:5]
        return "\n".join(lines)
    except Exception as e:
        return f"💼 **今日職缺** ⚠️ 讀取失敗：{e}"


# ── 主流程 ────────────────────────────────────────────────────────────────────


def main():
    sections = [daily_todos()]

    for part in (crawler_status(), line_bot_status(), git_status(), system_status()):
        if part:
            sections += ["", part]

    suggestions = line_bot_suggestions()
    if suggestions:
        sections += ["", suggestions]

    jobs = job_search_summary()
    if jobs:
        sections += ["", jobs]

    message = "\n".join(sections)

    # Discord 訊息上限 2000 字
    if len(message) > 1900:
        message = message[:1900] + "\n…（截斷）"

    ok = send_dm(message)
    if not ok:
        print("Discord 發送失敗", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
