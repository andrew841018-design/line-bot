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
sys.path.insert(0, str(DEP_CODE_DIR))  # DEP_CODE_DIR 優先，避免 line_bot/config.py 蓋掉 dependent_code/config.py

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
        has_issue = False
        for name, total, h24, h72 in rows:
            if h24 == 0:
                icon = "🔴" if h72 == 0 else "🟡"
                has_issue = True
            else:
                icon = "🟢"
            lines.append(f"{icon} {name}: 總 {total} | 24h +{h24} | 72h +{h72}")
        if not has_issue:
            return "🕷️ **爬蟲狀態** 🟢 全部正常"
        return "\n".join(lines)
    except Exception as e:
        return f"🕷️ **爬蟲狀態** ⚠️ 查詢失敗：{e}"


# ── 3. LINE Bot 狀態 ──────────────────────────────────────────────────────────

def line_bot_status() -> str:
    lines = []
    # uvicorn.log 尾部
    log_path = LINE_BOT_DIR / "uvicorn.log"
    try:
        result = subprocess.run(["tail", "-20", str(log_path)], capture_output=True, text=True)
        tail = result.stdout
        errors = [l for l in tail.splitlines() if "ERROR" in l or "error" in l.lower()]
        if errors:
            lines.append("🤖 **LINE Bot** 🔴 有 ERROR")
            for e in errors[-3:]:
                lines.append(f"  {e.strip()}")
        else:
            lines.append("🤖 **LINE Bot** 🟢 log 無異常")
    except Exception as e:
        lines.append(f"🤖 **LINE Bot** ⚠️ log 讀取失敗：{e}")

    # DB 活躍度
    db_path = LINE_BOT_DIR / "line_bot.db"
    if db_path.exists():
        mtime = datetime.fromtimestamp(db_path.stat().st_mtime)
        age_h = (datetime.now() - mtime).total_seconds() / 3600
        if age_h > 24:
            lines.append(f"  DB 最後更新 {age_h:.0f}h 前 🟡")
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
                ["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True
            ).stdout.strip()
            unpushed = subprocess.run(
                ["git", "log", "@{u}..", "--oneline"], cwd=path, capture_output=True, text=True
            ).stdout.strip()
            last_commit = subprocess.run(
                ["git", "log", "-1", "--format=%cr"], cwd=path, capture_output=True, text=True
            ).stdout.strip()
            u_count = len(uncommitted.splitlines()) if uncommitted else 0
            p_count = len(unpushed.splitlines()) if unpushed else 0
            icon = "🟡" if u_count > 0 or p_count > 0 else "🟢"
            lines.append(f"{icon} {name}: 未 commit {u_count} 筆 | unpushed {p_count} | 最近 commit {last_commit}")
        except Exception as e:
            lines.append(f"⚠️ {name}: {e}")
    return "\n".join(lines)


# ── 5. 系統 & Pipeline ────────────────────────────────────────────────────────

def system_status() -> str:
    lines = ["🖥️ **系統 & Pipeline**"]

    # 磁碟空間
    df = subprocess.run(["df", "-h", "/Users/andrew"], capture_output=True, text=True).stdout
    for line in df.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 5:
            use_pct = int(parts[4].replace("%", ""))
            icon = "🔴" if use_pct > 90 else "🟡" if use_pct > 75 else "🟢"
            lines.append(f"{icon} 磁碟：{parts[4]} 使用（可用 {parts[3]}）")

    # logs 數量，超過 30 自動砍最舊的
    log_files = sorted((PROJECT_DIR / "logs").glob("*"), key=lambda f: f.stat().st_mtime)
    log_count = len(log_files)
    if log_count > 30:
        to_delete = log_files[:log_count - 30]
        for f in to_delete:
            f.unlink()
        lines.append(f"🧹 logs 自動清理：刪除 {len(to_delete)} 個舊檔（保留最新 30 個）")

    # ETL log 錯誤
    today_log = PROJECT_DIR / "logs" / f"wayback_{datetime.now().strftime('%Y%m%d')}.log"
    if today_log.exists():
        content = today_log.read_text(errors="ignore")
        err_count = content.count("ERROR")
        warn_count = content.count("WARNING")
        if err_count > 0 or warn_count > 0:
            lines.append(f"🟡 今日 ETL log：{err_count} ERROR, {warn_count} WARNING")
        else:
            lines.append("🟢 今日 ETL log 無異常")
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
        section = content[idx:idx+500].splitlines()
        lines = ["📋 **下次繼續**"]
        for line in section[1:8]:
            if line.strip():
                lines.append(line)
        return "\n".join(lines)
    except Exception:
        return ""


# ── 7. LINE bot 功能建議 ──────────────────────────────────────────────────────

def line_bot_suggestions() -> str:
    memory_path = Path("/Users/andrew/.claude/projects/-Users-andrew-Desktop-andrew-Data-engineer/memory/project_line_bot_feature_suggestions.md")
    try:
        content = memory_path.read_text(errors="ignore")

        # 抓「已執行」關鍵詞，用來過濾重複
        done_idx = content.find("## 已執行")
        done_text = content[done_idx:] if done_idx != -1 else ""
        done_keywords = [l.strip().lstrip("- ").split("**")[1] if "**" in l else l.strip().lstrip("- ")
                         for l in done_text.splitlines() if l.strip().startswith("- ")]

        # 抓「待建議」區
        idx = content.find("## 待建議")
        end_idx = content.find("## 已執行")
        if idx == -1:
            return ""
        section = content[idx:end_idx if end_idx != -1 else idx+500]
        items = [l.strip() for l in section.splitlines() if l.strip().startswith("- ")]

        # 過濾掉已執行的（關鍵詞比對）
        def _already_done(item: str) -> bool:
            item_lower = item.lower()
            return any(kw.lower() in item_lower for kw in done_keywords if len(kw) > 3)

        items = [i for i in items if not _already_done(i)]
        if not items:
            return ""
        picks = items[:2]
        lines = ["💡 **LINE bot 建議**"]
        for item in picks:
            lines.append(item)
        return "\n".join(lines)
    except Exception:
        return ""


# ── 8. AI+DE 職缺建議 ─────────────────────────────────────────────────────────

def job_search_summary() -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    report = Path(f"/Users/andrew/Desktop/andrew/job_search/{today}.md")
    if not report.exists():
        return ""
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
                        m = re.search(r'\(https?://[^\)]+\)', link_col)
                        if m:
                            url = m.group(0)[1:-1]
                        must_apply.append(f"• {company} — {title} (S{score}) {url}")

        if not summary_lines and not must_apply:
            return ""

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

    # 爬蟲 + 系統：只在異常時顯示
    crawler = crawler_status()
    if "🟢 全部正常" not in crawler:
        sections += ["", crawler]

    # LINE Bot：log 有 ERROR 才顯示
    bot = line_bot_status()
    if "🔴" in bot or "⚠️" in bot:
        sections += ["", bot]

    system = system_status()
    # 系統：磁碟 > 75% 或 ETL 有 ERROR/WARNING 才顯示
    if "🔴" in system or "🟡" in system or "ERROR" in system:
        sections += ["", system]

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
