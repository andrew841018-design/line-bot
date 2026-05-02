#!/usr/bin/env python3
"""每日 10:00 自動匯報 → Discord DM"""

import os
import re
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

# ── 已推到 Discord 的職缺 URL 紀錄（永久去重，跟 scraper 的 seen_jobs.json 分開） ─
import json as _json
_PUSHED_JOBS_FILE = LINE_BOT_DIR / "pushed_jobs.json"


def _load_pushed_jobs() -> set:
    if not _PUSHED_JOBS_FILE.exists():
        return set()
    try:
        with open(_PUSHED_JOBS_FILE) as f:
            return set(_json.load(f))
    except Exception:
        return set()


def _save_pushed_jobs(urls: set) -> None:
    """原子寫入：先寫 .tmp 再 rename，避免 crash 中途產生壞檔。"""
    try:
        tmp = _PUSHED_JOBS_FILE.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            _json.dump(sorted(urls), f, ensure_ascii=False)
        os.replace(tmp, _PUSHED_JOBS_FILE)
    except Exception:
        pass


# ── 1. 每日待辦 ──────────────────────────────────────────────────────────────


def upcoming_birthdays() -> str:
    """檢查 facts 表中的家庭生日，列出 7 天內到的，附剩餘天數。"""
    db_path = LINE_BOT_DIR / "line_bot.db"
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("SELECT fact FROM facts WHERE fact LIKE '%生日%'")
        rows = cur.fetchall()
        conn.close()
    except Exception:
        return ""

    if not rows:
        return ""

    today = datetime.now().date()
    upcoming = []
    for (fact,) in rows:
        m = re.search(r"(.+?)生日：(\d{1,2})/(\d{1,2})", fact)
        if not m:
            continue
        who, mm, dd = m.group(1), int(m.group(2)), int(m.group(3))
        # 算今年生日；過了就算明年
        try:
            this_year_bday = today.replace(month=mm, day=dd)
        except ValueError:
            continue
        if this_year_bday < today:
            this_year_bday = this_year_bday.replace(year=today.year + 1)
        days_left = (this_year_bday - today).days
        if 0 <= days_left <= 7:
            upcoming.append((days_left, who, mm, dd))

    if not upcoming:
        return ""

    upcoming.sort()
    lines = ["🎂 **生日提醒（7 天內）**"]
    for days, who, mm, dd in upcoming:
        if days == 0:
            lines.append(f"🎉 **今天是{who}的生日！** ({mm:02d}/{dd:02d})")
        elif days == 1:
            lines.append(f"⚠️ 明天是{who}的生日 ({mm:02d}/{dd:02d})")
        else:
            lines.append(f"• {who}：{mm:02d}/{dd:02d}（剩 {days} 天）")
    return "\n".join(lines)


_PENDING_TODOS_FILE = LINE_BOT_DIR / "pending_todos.json"


def _load_pending_todos() -> list:
    """讀一次性待辦（處理完手動從 json 移除）。"""
    if not _PENDING_TODOS_FILE.exists():
        return []
    try:
        with open(_PENDING_TODOS_FILE) as f:
            data = _json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def daily_todos() -> str:
    now = datetime.now()
    today = now.strftime("%m/%d")
    weekday = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
    push_time = now.strftime("%H:%M")

    lines = [f"📌 **每日待辦** ({today} 週{weekday} {push_time})"]

    # 一次性 urgent todos 排在前面（處理完叫 Claude 從 pending_todos.json 移除）
    pending = _load_pending_todos()
    for item in pending:
        lines.append(f"🚨 {item}")
    if pending:
        lines.append("─────")

    lines += [
        "• Mock interview 做了嗎？",
        "• IBM 影片看了 30 分鐘嗎？",
        "• 學車相關影片看了一則嗎？",
        "• Code review 做了嗎？",
        "• 小說看了 1.5 小時嗎？",
        "• 重訓了 1 小時嗎？",
        "• 健康檢查做了嗎？（微量元素檢測）",
        "• 讀經禱告了嗎？",
        "• 寫日記了嗎？",
    ]
    return "\n".join(lines)


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


# ── False-positive suppression patterns（已知有自動 retry / fallback 機制的事件）
# 加新 pattern 來解決未來類似誤報，不需動下面 _is_real_error 邏輯
_LOG_NOISE_PATTERNS = (
    "transient error",       # gemini ServerError / 503 自動 retry（後續成功 / fallback 不算 ERROR）
    "falling back to",       # main → lite model fallback（已有處理）
    "quota available, processing pending",  # retry worker 正常觸發
    "cached quota exhausted",  # quota 鎖定下的靜默跳過（不是新故障）
)


def _is_real_error(line: str) -> bool:
    """判斷 log 行是否為「真的 ERROR」需要 alert。

    過濾以下 false positive：
    - logging level 是 WARNING / INFO 但內文含 error 字眼（如 ServerError 類別名）
    - 已知有自動 retry / fallback 處理的事件（_LOG_NOISE_PATTERNS）
    """
    line_lower = line.lower()
    # 排除已知正常事件（有自動處理機制）
    for noise in _LOG_NOISE_PATTERNS:
        if noise in line_lower:
            return False
    # 排除 logging level 不是 ERROR 的行
    if "warning" in line_lower or " info " in line_lower or "[info]" in line_lower:
        return False
    # 真 ERROR：含 logging level marker（空格邊界）或 Python Traceback
    return ("[ERROR]" in line or " ERROR " in line or
            " ERROR -" in line or "Traceback" in line)


def line_bot_status() -> str:
    lines = []
    # uvicorn.log 尾部
    log_path = LINE_BOT_DIR / "uvicorn.log"
    try:
        result = subprocess.run(
            ["tail", "-20", str(log_path)], capture_output=True, text=True
        )
        tail = result.stdout
        errors = [l for l in tail.splitlines() if _is_real_error(l)]
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


# ── 5.5 費城半導體指數 (^SOX) 乖離率 + 買賣建議 ───────────────────────────────

# 心靈雞湯池：base 365 句在 investment_quotes.py，dynamic 池每天可能累積
# 每句配一條歷史佐證（事件 / 數據 / 名人語錄），不只勵志還能說服自己
from investment_quotes import QUOTES as _BASE_QUOTES, BRAINYQUOTE_AUTHORS as _BQ_AUTHORS

_DYNAMIC_QUOTES_FILE = LINE_BOT_DIR / "dynamic_quotes.json"

# 當下 Fed 循環：cutting / hiking / hold
# 用途：過濾雞湯池中 framing 與當下宏觀循環衝突的句子（避免「現在升息所以股市會跌」這種錯位提醒）
# 切換循環時更新這一行；之後可改為從 FOMC 紀要 / FedWatch 自動推算
_FED_REGIME = "cutting"

_HIKING_FRAMING = re.compile(
    r"聯準會.{0,4}(?:還在|正在|開始|繼續|正)?\s*升息|"
    r"(?:升息|加息).{0,3}(?:循環|週期|期間|中)|"
    r"Fed\s+(?:is\s+)?(?:hiking|raising|tightening)|"
    r"rate\s*hik|hiking\s+cycle|tightening\s+cycle"
)
_CUTTING_FRAMING = re.compile(
    r"聯準會.{0,4}(?:還在|正在|開始|繼續|正)?\s*降息|"
    r"(?:降息|減息).{0,3}(?:循環|週期|期間|中)|"
    r"Fed\s+(?:is\s+)?(?:cutting|easing)|"
    r"rate\s*cut|cutting\s+cycle|easing\s+cycle"
)


def _quote_matches_regime(quote_text: str) -> bool:
    """True = 此句 framing 與當下 Fed 循環相容（或無 framing）；False = 衝突應跳過。"""
    if _FED_REGIME == "cutting" and _HIKING_FRAMING.search(quote_text):
        return False
    if _FED_REGIME == "hiking" and _CUTTING_FRAMING.search(quote_text):
        return False
    return True


def _load_dynamic_quotes() -> dict:
    """讀運行期累積的雞湯池（同 _BASE_QUOTES 結構）。檔案不存在或壞掉回空 dict。"""
    if not _DYNAMIC_QUOTES_FILE.exists():
        return {}
    try:
        with open(_DYNAMIC_QUOTES_FILE) as f:
            data = _json.load(f)
        # 把 list of [quote, evidence] 轉回 tuple
        return {k: [tuple(p) for p in v] for k, v in data.items()}
    except Exception:
        return {}


def _save_dynamic_quotes(d: dict) -> None:
    try:
        tmp = _DYNAMIC_QUOTES_FILE.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            _json.dump({k: [list(p) for p in v] for k, v in d.items()},
                       f, ensure_ascii=False, indent=2)
        os.replace(tmp, _DYNAMIC_QUOTES_FILE)
    except Exception:
        pass


# 投資家 Wikiquote 來源池 — 每天輪換一位抓
_WIKIQUOTE_AUTHORS = [
    "Warren_Buffett", "Charlie_Munger", "Peter_Lynch", "John_C._Bogle",
    "Benjamin_Graham", "George_Soros", "Ray_Dalio", "John_Templeton",
    "Philip_Fisher", "Howard_Marks", "Seth_Klarman", "Jesse_Livermore",
    "Carl_Icahn", "Jim_Rogers", "Bill_Ackman",
]


def _try_append_today_quote() -> None:
    """每天輪一位投資家從 Wikiquote 抓 quote，篩掉已有的，加 1 條到 dynamic 池。

    fail-safe：抓失敗 / 連線超時 / 沒新句 → 完全沉默不影響主流程。
    每天最多加 1 條，符合「緩慢累積」原則。
    """
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        return

    # 用日期 hash 起點，依序試 5 位（自動跳過 404 / 抓不到 candidates 的）
    start_idx = datetime.now().toordinal() % len(_WIKIQUOTE_AUTHORS)
    candidates = []
    author = None
    for offset in range(5):
        idx = (start_idx + offset) % len(_WIKIQUOTE_AUTHORS)
        try_author = _WIKIQUOTE_AUTHORS[idx]
        url = f"https://en.wikiquote.org/wiki/{try_author}"
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            content = soup.select_one("#mw-content-text")
            if not content:
                continue
            top_uls = content.select("div.mw-parser-output > ul")
            for ul in top_uls:
                for li in ul.find_all("li", recursive=False):
                    li_copy = BeautifulSoup(str(li), "lxml")
                    for sub in li_copy.find_all(["ul", "ol"]):
                        sub.extract()
                    quote = li_copy.get_text(" ", strip=True)
                    if 50 < len(quote) < 400:
                        candidates.append(quote)
            if candidates:
                author = try_author
                break
        except Exception:
            continue

    # 若 Wikiquote 全 5 位都沒抓到，改試 Brainyquote 投資家清單
    if not candidates:
        for bq_name, bq_slug in _BQ_AUTHORS:
            try:
                br = requests.get(
                    f"https://www.brainyquote.com/authors/{bq_slug}-quotes",
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=15,
                )
                if br.status_code != 200:
                    continue
                bsoup = BeautifulSoup(br.text, "lxml")
                for el in bsoup.select(".b-qt"):
                    txt = el.get_text(" ", strip=True)
                    if 50 < len(txt) < 400:
                        candidates.append(txt)
                if candidates:
                    author = bq_name.replace(" ", "_")
                    break
            except Exception:
                continue

    if not candidates or not author:
        return

    # Bonus 來源：Howard Marks 那天額外抓 Oaktree memos 一段
    if author == "Howard_Marks":
        try:
            list_r = requests.get(
                "https://www.oaktreecapital.com/insights/memos",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            if list_r.status_code == 200:
                lsoup = BeautifulSoup(list_r.text, "lxml")
                memo_links = [a["href"] for a in lsoup.find_all("a", href=True)
                              if "/insights/memo/" in a["href"]]
                if memo_links:
                    # 用日期 hash 選一篇最新的（前 5 篇輪）
                    pick = memo_links[datetime.now().toordinal() % min(5, len(memo_links))]
                    if not pick.startswith("http"):
                        pick = "https://www.oaktreecapital.com" + pick
                    memo_r = requests.get(pick, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
                    if memo_r.status_code == 200:
                        msoup = BeautifulSoup(memo_r.text, "lxml")
                        for p in msoup.find_all("p"):
                            txt = p.get_text(" ", strip=True)
                            # 找 80-300 chars 的 quotable 段落
                            if 80 < len(txt) < 300 and "." in txt:
                                # 取第一個句號為止當 quote
                                first_sentence = txt.split(".")[0].strip() + "."
                                if 50 < len(first_sentence) < 300:
                                    candidates.insert(0, first_sentence)  # priority
                                    break
        except Exception:
            pass

    # 已存在的 quote（base + dynamic 全桶）→ 集合做去重
    existing = set()
    for bucket in ["雙過熱", "單過熱", "中性", "買區", "深跌"]:
        for q, _ in _BASE_QUOTES.get(bucket, []):
            existing.add(q)
    dyn = _load_dynamic_quotes()
    for bucket_list in dyn.values():
        for q, _ in bucket_list:
            existing.add(q)

    new_ones = [q for q in candidates if q not in existing]
    if not new_ones:
        return

    # 用 author 名字 + 日期 hash 選一條（同一天結果一致）
    chosen = new_ones[(datetime.now().toordinal() + len(author)) % len(new_ones)]
    author_zh = author.replace("_", " ")
    # 判斷來源：Wikiquote 還是 Brainyquote
    is_brainy = any(author_zh == bq_n for bq_n, _ in _BQ_AUTHORS)
    evidence = f"📚 {'Brainyquote' if is_brainy else 'Wikiquote'} / {author_zh}"

    # keyword heuristic 歸桶（英文）
    txt = chosen.lower()
    if any(w in txt for w in ["greed", "bubble", "euphoria", "mania", "speculation", "bull market", "fad"]):
        bucket = "雙過熱"
    elif any(w in txt for w in ["caution", "discipline", "patience", "wait", "margin of safety"]):
        bucket = "單過熱"
    elif any(w in txt for w in ["fear", "panic", "blood", "crash", "depression", "pessimism", "crisis"]):
        bucket = "深跌"
    elif any(w in txt for w in ["opportunity", "buy", "value", "discount", "cheap", "bargain", "undervalued"]):
        bucket = "買區"
    else:
        bucket = "中性"

    bucket_list = dyn.get(bucket, [])
    bucket_list.append((chosen, evidence))
    dyn[bucket] = bucket_list
    _save_dynamic_quotes(dyn)


def _today_market_snapshot() -> str:
    """每日市場即時數據 + 頭條，當作雞湯的「今日佐證」附加。

    來源（fail-soft，每個獨立試）：
      1. VIX 恐慌指數（yfinance）
      2. CNN Fear & Greed Index
      3. 鉅亨網台股頭條
      4. 鉅亨網全球財經頭條

    回傳：單行字串，例如「VIX 18.7（中性）｜鉅亨：大台積電時代來臨」
    完全抓不到回空字串。
    """
    parts = []

    # 1. VIX
    try:
        import yfinance as yf
        vix = yf.Ticker("^VIX")
        vix_price = float(vix.fast_info.last_price)
        # VIX 區間判定
        if vix_price < 12:
            vix_label = "極低"
        elif vix_price < 18:
            vix_label = "中性偏低"
        elif vix_price < 25:
            vix_label = "中性"
        elif vix_price < 35:
            vix_label = "偏高"
        else:
            vix_label = "極高恐慌"
        parts.append(f"VIX {vix_price:.1f}（{vix_label}）")
    except Exception:
        pass

    # 2. CNN Fear & Greed（公開 dataviz API，0-100）
    try:
        import requests
        r = requests.get(
            f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{datetime.now().strftime('%Y-%m-%d')}",
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://edition.cnn.com",
                "Referer": "https://edition.cnn.com/",
            },
            timeout=8,
        )
        if r.status_code == 200:
            d = r.json()
            cur = d.get("fear_and_greed", {})
            score = cur.get("score")
            rating = cur.get("rating", "")
            if score is not None:
                rating_zh = {
                    "extreme fear": "極度恐懼", "fear": "恐懼",
                    "neutral": "中性", "greed": "貪婪",
                    "extreme greed": "極度貪婪",
                }.get(rating.lower(), rating)
                parts.append(f"F&G {score:.0f}（{rating_zh}）")
    except Exception:
        pass

    # 3. 鉅亨網頭條
    try:
        import requests
        r = requests.get(
            "https://api.cnyes.com/media/api/v1/newslist/category/headline?limit=1",
            timeout=8,
        )
        if r.status_code == 200:
            items = r.json().get("items", {}).get("data", [])
            if items:
                title = items[0].get("title", "").strip()
                if title:
                    parts.append(f"鉅亨：{title[:50]}")
    except Exception:
        pass

    if not parts:
        return ""
    return "📰 今日：" + " ｜ ".join(parts)


def _merged_pool(bucket: str) -> list:
    """合併 base + dynamic 該桶的所有句子。"""
    base = _BASE_QUOTES.get(bucket, [])
    dyn = _load_dynamic_quotes().get(bucket, [])
    return list(base) + list(dyn)




_QUOTE_HISTORY_FILE = LINE_BOT_DIR / "quote_history.json"


def _load_quote_history() -> dict:
    """{bucket: {idx_str: "YYYY-MM-DD"}}"""
    if not _QUOTE_HISTORY_FILE.exists():
        return {}
    try:
        with open(_QUOTE_HISTORY_FILE) as f:
            return _json.load(f)
    except Exception:
        return {}


def _save_quote_history(hist: dict) -> None:
    try:
        tmp = _QUOTE_HISTORY_FILE.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            _json.dump(hist, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _QUOTE_HISTORY_FILE)
    except Exception:
        pass


def _pick_quote(bucket: str) -> tuple:
    """從指定桶選一句：優先「365 天內沒用過的」，若全用過則挑「最久沒用」的那句。

    這樣保證 365 天內不重複（除非該桶池子 < 365 句且連續觸發超過池子大小天數）。
    """
    pool = _merged_pool(bucket)  # base 365 + dynamic 累積
    today = datetime.now().date()
    history = _load_quote_history()
    bucket_hist = history.get(bucket, {})

    # 先排除與當下 Fed 循環衝突的 framing（避免「升息會跌」在降息週期出現）
    regime_ok = {i for i in range(len(pool)) if _quote_matches_regime(pool[i][0])}
    if not regime_ok:
        regime_ok = set(range(len(pool)))  # 整桶都被擋的極端情況保底，不空回

    # 找 365 天內沒用過的 idx
    fresh_indices = []
    for i in regime_ok:
        last_used = bucket_hist.get(str(i))
        if last_used is None:
            fresh_indices.append(i)
            continue
        try:
            last_date = datetime.strptime(last_used, "%Y-%m-%d").date()
            if (today - last_date).days >= 365:
                fresh_indices.append(i)
        except ValueError:
            fresh_indices.append(i)
    fresh_indices.sort()

    if fresh_indices:
        # 從 fresh 中用日期 hash 選一個（同一天多次呼叫得到同一句）
        chosen = fresh_indices[today.toordinal() % len(fresh_indices)]
    else:
        # 所有句都在 365 天內用過 → 挑最久沒用的（仍受 regime 過濾）
        def _last(i):
            d = bucket_hist.get(str(i))
            return datetime.strptime(d, "%Y-%m-%d").date() if d else today
        chosen = min(regime_ok, key=_last)

    # 記錄使用日期
    bucket_hist[str(chosen)] = today.strftime("%Y-%m-%d")
    history[bucket] = bucket_hist
    _save_quote_history(history)
    return pool[chosen]


def _market_quote(bias_20: float, bias_60: float) -> str:
    """根據雙線乖離選情緒桶，配合 365 天去重選句。"""
    overheat_20 = bias_20 > 6
    overheat_60 = bias_60 > 12
    deep_down = bias_20 < -6 or bias_60 < -10
    in_buy_20 = -2 <= bias_20 <= 2
    in_buy_60 = -5 <= bias_60 <= 2

    if overheat_20 and overheat_60:
        bucket = "雙過熱"
    elif overheat_20 or overheat_60:
        bucket = "單過熱"
    elif deep_down:
        bucket = "深跌"
    elif in_buy_20 or in_buy_60:
        bucket = "買區"
    else:
        bucket = "中性"

    quote, evidence = _pick_quote(bucket)
    return f"💭 {quote}\n📊 {evidence}"


def sox_sentiment() -> str:
    """抓 ^SOX 最新價 + MA20/MA60 乖離率，給買/賣建議。

    乖離率 = (Price - MA) / MA × 100%
    買區間（user 指定）：
      月線（MA20）乖離 -2% ~ +2%
      季線（MA60）乖離 -5% ~ +2%
    賣區間（依費半歷史 ±1σ ~ ±2σ 自定）：
      月線 > +6% / +10%   過熱 / 強烈賣
      季線 > +12% / +18%  過熱 / 強烈賣
    """
    try:
        import yfinance as yf
    except ImportError:
        return "📈 **費半指數** ⚠️ yfinance 未安裝"

    try:
        sox = yf.Ticker("^SOX")
        hist = sox.history(period="100d", interval="1d")
        if len(hist) < 60:
            return "📈 **費半指數** ⚠️ 歷史資料不足 60 日"

        ma20 = hist["Close"].rolling(20).mean().iloc[-1]
        ma60 = hist["Close"].rolling(60).mean().iloc[-1]
        # last_price 在美股盤中為即時，盤後/盤前則為最近一次成交
        try:
            price = float(sox.fast_info.last_price)
        except Exception:
            price = float(hist["Close"].iloc[-1])
        prev_close = float(hist["Close"].iloc[-1])
        change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0.0

        bias_20 = (price - ma20) / ma20 * 100
        bias_60 = (price - ma60) / ma60 * 100

        # 買賣判定（季線優先，因為 user 設定季線區間較寬）
        def _signal_20(b: float) -> str:
            if b < -6:
                return "🟢🟢 月線深跌 → 強烈買入"
            if -2 <= b <= 2:
                return "🟢 月線買入區間"
            if 2 < b <= 6:
                return "⚪ 月線略偏熱"
            if 6 < b <= 10:
                return "🟡 月線過熱警示"
            return "🔴 月線強烈賣出（{:+.1f}% > +10%）".format(b)

        def _signal_60(b: float) -> str:
            if b < -10:
                return "🟢🟢 季線深跌 → 強烈買入"
            if -5 <= b <= 2:
                return "🟢 季線買入區間"
            if 2 < b <= 12:
                return "⚪ 季線中性"
            if 12 < b <= 18:
                return "🟡 季線過熱警示"
            return "🔴 季線強烈賣出（{:+.1f}% > +18%）".format(b)

        sig_20 = _signal_20(bias_20)
        sig_60 = _signal_60(bias_60)
        # 兩條都進買區 → 升級為強烈買入
        both_buy = ("買入區間" in sig_20 or "強烈買入" in sig_20) and (
            "買入區間" in sig_60 or "強烈買入" in sig_60
        )
        # 兩條都進賣區 → 升級為強烈賣出
        both_sell = ("過熱" in sig_20 or "強烈賣出" in sig_20) and (
            "過熱" in sig_60 or "強烈賣出" in sig_60
        )

        lines = ["📈 **費城半導體指數 (^SOX)**"]
        lines.append(f"報價 {price:,.2f}（{change_pct:+.2f}%）")
        lines.append(f"月線 MA20 {ma20:,.0f} | 乖離 {bias_20:+.2f}%")
        lines.append(f"季線 MA60 {ma60:,.0f} | 乖離 {bias_60:+.2f}%")
        lines.append(sig_20)
        lines.append(sig_60)
        if both_buy:
            lines.append("⭐ **雙線同時進買區 → 強烈買入訊號**")
        elif both_sell:
            lines.append("⚠️ **雙線同時過熱 → 注意減碼風險**")
        lines.append(_market_quote(bias_20, bias_60))
        snapshot = _today_market_snapshot()
        if snapshot:
            lines.append(snapshot)
        return "\n".join(lines)
    except Exception as e:
        return f"📈 **費半指數** ⚠️ 抓取失敗：{e}"


# ── 5.7 練車路線（每日輪換）────────────────────────────────────────────────

# 練車路線池（依難度由簡入難排序；起點固定 善導寺）
# 不刪除任何路線 — 升級後 / 改變偏好可重新解鎖
# types 是該路線涵蓋的道路類型 tag（用於現階段 focus 篩選顯示 ✅ vs ⏸）
_DRIVING_ROUTES = [
    {
        "name": "三重 ↔ 台北（最入門）",
        "path": "善導寺 → 忠孝西路 → 忠孝橋 → 三重重新路 → 折返（或走台北橋 / 中興橋）",
        "duration": "20-30 分鐘",
        "highlights": "短距離 + 橋梁切換，最低壓力起步",
        "level": "新手",
        "types": ["橋", "公路"],
    },
    {
        "name": "板橋土城高架圈（65 快速）",
        "path": "華江橋 → 環河南快速 → 65 快速（土城-中和）→ 折返",
        "duration": "60 分鐘",
        "highlights": "65 是新北車流最少快速道路",
        "level": "新手",
        "types": ["橋", "快速", "公路"],
    },
    {
        "name": "八里十三行（純非國道）",
        "path": "重慶北路 → 環河北快速 → 64 快速 → 八里",
        "duration": "30-40 分鐘",
        "highlights": "練平面切快速 + 64 維持速度",
        "level": "新手",
        "types": ["快速", "公路"],
    },
    {
        "name": "基隆河沿岸高架圈",
        "path": "市民大道 → 環東大道 → 麥帥二橋 → 大直橋 → 圓山 → 重慶北高架",
        "duration": "50-60 分鐘",
        "highlights": "連續匝道密集 + 河岸景",
        "level": "新手進階",
        "types": ["高架", "橋"],
    },
    {
        "name": "北市三高架繞圈",
        "path": "建國高架 → 水源快速 → 福和橋 → 市民大道高架 → 環東大道 → 內湖",
        "duration": "60-90 分鐘",
        "highlights": "練連續匝道切換 + 北市高架網",
        "level": "新手",
        "types": ["高架", "橋"],
    },
    {
        "name": "八里 + 台 15 西濱南下",
        "path": "環河北 → 64 → 八里 → 台 15 西濱（往林口/桃園方向）",
        "duration": "1.5-2 小時",
        "highlights": "西濱長距離維持速度 + 海岸線",
        "level": "進階",
        "types": ["快速", "公路"],
    },
    {
        "name": "桃園永安漁港（含國道練習）",
        "path": "建國高架 → 國道 1 → 林口交流道 → 台 61 西濱 → 永安漁港",
        "duration": "1.5 小時",
        "highlights": "國道 + 西濱快速兼顧",
        "level": "進階（含國道）",
        "types": ["高架", "國道", "快速", "公路"],
    },
    {
        "name": "宜蘭礁溪（雪隧挑戰）",
        "path": "市民大道 → 國道 5 → 雪山隧道 → 礁溪",
        "duration": "1 小時 20 分",
        "highlights": "12.9km 長隧道專練（限速嚴）",
        "level": "挑戰級",
        "types": ["國道", "隧道"],
    },
]

# 現階段練習目標：只練這幾類道路（其餘標 ⏸ 暫緩，不刪除）
_CURRENT_FOCUS_TYPES = {"橋", "高架", "公路", "快速"}


def driving_practice() -> str:
    """練車路線：#1 主練、#2-8 候選分區呈現。

    現階段 focus = {橋, 高架, 公路, 快速}：符合的標 ✅，不符合（如雪隧）標 ⏸。
    """
    if not _DRIVING_ROUTES:
        return ""

    def _format(idx: int, r: dict) -> list:
        types = set(r.get("types", []))
        non_focus = types - _CURRENT_FOCUS_TYPES
        mark = "✅" if not non_focus else f"⏸ 含 {','.join(sorted(non_focus))}"
        type_tags = "/".join(types) if types else "?"
        return [
            f"{idx}. {mark} [{r['level']}] {r['name']}（{r['duration']}，{type_tags}）",
            f"   {r['path']}",
        ]

    lines = [
        "🚗 **練車路線**（起點：善導寺）",
        "📅 現階段：每 2 週 ~ 1 個月練一次",
        f"🎯 現階段練：{' / '.join(sorted(_CURRENT_FOCUS_TYPES))}（其餘 ⏸ 暫緩）",
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
        "▶️ **目前主練（focus）**",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    lines += _format(1, _DRIVING_ROUTES[0])

    if len(_DRIVING_ROUTES) > 1:
        lines += [
            "",
            "📋 **候選路線**（由簡入難，要練哪條跟我說會展開細節）",
        ]
        # 候選路線只列一行（節省字數，避免 Discord 2000 字截斷職缺 URL）
        for i, r in enumerate(_DRIVING_ROUTES[1:], 2):
            types = set(r.get("types", []))
            non_focus = types - _CURRENT_FOCUS_TYPES
            mark = "✅" if not non_focus else f"⏸ 含{','.join(sorted(non_focus))}"
            lines.append(
                f"{i}. {mark} [{r['level']}] {r['name']}（{r['duration']}）"
            )

    lines.append("")
    lines.append("離峰時段：平日 10-15 點 / 假日清晨 6-9 點")
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
        # 永久去重：Andrew 的規則「沒叫我做 = 沒興趣，不要再推同一個」
        # 過去推過的所有 title 全部進黑名單，每天必須是新的
        try:
            history = json.loads(_SUGGESTION_HISTORY.read_text())
        except Exception:
            history = []

    # 近 3 天 LINE bot 對話樣本（抽 30 則）
    # raw_messages.created_at 存秒（INTEGER），不是毫秒；2026-05-02 修：原版用毫秒比較永遠 0 筆 → Gemini 沒原料 → 永遠回 null
    recent_msgs = []
    try:
        conn = sqlite3.connect(str(LINE_BOT_DIR / "line_bot.db"))
        cur = conn.cursor()
        three_days_ago_sec = int((datetime.now() - timedelta(days=3)).timestamp())
        cur.execute(
            "SELECT user_id, text FROM raw_messages WHERE created_at > ? AND user_id != '__bot__' ORDER BY created_at DESC LIMIT 30",
            (three_days_ago_sec,),
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

    # 黑名單：已執行 + 已略過 + 過去推過的所有 title（永久去重，沒採納就當沒興趣）
    blacklist = list(set(done_list + skipped_list + [h["title"] for h in history]))

    prompt = f"""你是 LINE bot 的產品顧問。任務：根據近 3 天群組對話，主動發想 1 個讓 Andrew 的家族 LINE bot 變更實用的新功能。

【近 3 天群組對話樣本（最重要！從這裡找痛點）】
{chr(10).join(recent_msgs) if recent_msgs else "(無樣本，但仍可基於 Andrew 個人需求發想)"}

【📋 已存在的待建議（未實作但已記下）】
{json.dumps(pending_list, ensure_ascii=False)}

【🚫 不要重複的（過去任何時候推過的，永久黑名單）】
{json.dumps(blacklist, ensure_ascii=False)}

規則：
1. **積極發想**：對話樣本只要有「資訊缺口、查詢需求、誤解、麻煩」就值得提建議。寧可大膽嘗試也不要消極回 null
2. **永久去重，不准包裝同樣概念**：黑名單裡的不只 title 完全相同不行，**用不同說法包裝同樣概念也不行**（例：「對話搜尋」vs「過去聊天記錄查詢」算同一件事）。Andrew 的規則「沒叫我做就是沒興趣」，請從**完全不同領域 / 角度 / 痛點**切入
3. **建議方向參考**（任選一個或自創）：
   - 訂閱式提醒（股票代號、特定關鍵字觸發推播）
   - 跨群組 / 個人 DM 整合
   - 行事曆 / 待辦自動建立（從對話抽出時間 + 事件）
   - 圖片 OCR / 文件分析強化
   - 新聞主題訂閱（家族長輩關心議題自動摘要）
   - 投資組合追蹤（提到股票自動補當日資訊）
   - 對話搜尋（過去聊過什麼可以查）
   - 多語翻譯（英文新聞自動中譯）
   - 個人化儀表板（特定使用者的偏好行為）
4. 至少嘗試一條建議。極度不適合才回 null。
5. 建議要具體（標題 + 一句話理由）

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


def _parse_source_breakdown(text: str) -> tuple[dict, dict]:
    """從報告抽出兩張表：
    - per_source_total: {source: (raw_total, ok_keyword_count, total_keyword_count)}
    - jd_fetch: {source: (attempted, succeeded, inline)}
    """
    per_source: dict = {}
    jd_fetch: dict = {}

    # ── 各來源抓取明細表（5 欄：平台 | 類別 | Keyword | 抓回 | 狀態）─────────
    in_per_kw = False
    for line in text.splitlines():
        if "## 🔍 各來源抓取明細" in line:
            in_per_kw = True
            continue
        if in_per_kw:
            if line.startswith("###") or line.startswith("---") or line.startswith("# "):
                in_per_kw = False
                continue
            stripped = line.strip()
            if not stripped.startswith("|") or stripped.startswith("|---") or stripped.startswith("| 平台"):
                continue
            cols = [c.strip() for c in stripped.split("|") if c.strip()]
            if len(cols) < 5:
                continue
            source, _cat, _kw, raw, status = cols[0], cols[1], cols[2], cols[3], cols[4]
            try:
                raw_n = int(raw)
            except ValueError:
                continue
            ok = status.startswith("✅")
            cur = per_source.get(source, (0, 0, 0))
            per_source[source] = (cur[0] + raw_n, cur[1] + (1 if ok else 0), cur[2] + 1)

    # ── JD 內文 fetch 結果表（4 欄：平台 | 嘗試 | 成功 | listing 內含）─────
    in_jd = False
    for line in text.splitlines():
        if "JD 內文 fetch 結果" in line:
            in_jd = True
            continue
        if in_jd:
            if line.startswith("---") or line.startswith("# "):
                in_jd = False
                continue
            stripped = line.strip()
            if not stripped.startswith("|") or stripped.startswith("|---") or stripped.startswith("| 平台"):
                continue
            cols = [c.strip() for c in stripped.split("|") if c.strip()]
            if len(cols) < 4:
                continue
            try:
                jd_fetch[cols[0]] = (int(cols[1]), int(cols[2]), int(cols[3]))
            except ValueError:
                continue

    return per_source, jd_fetch


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

        # 取所有 4 個 bucket 的職缺（DE/AI × 必投/值得投），每筆都附 URL
        # 去重：① 之前推過（pushed_jobs.json） ② URL 相同 ③ 同公司同職位
        pushed_urls = _load_pushed_jobs()
        seen_url = set()
        seen_company_title = set()
        # 各 bucket 的職缺 list
        buckets: dict = {"DE 必投": [], "DE 值得投": [], "AI 必投": [], "AI 值得投": []}
        all_new_urls = []

        current_bucket = None
        for line in text.splitlines():
            # 偵測 bucket header（## 🔴 必投 DE / ## 🟡 值得投 DE / ## 🔴 必投 AI / ## 🟡 值得投 AI）
            if "🔴 必投" in line:
                if "DE" in line:
                    current_bucket = "DE 必投"
                elif "AI" in line:
                    current_bucket = "AI 必投"
                else:
                    current_bucket = None
                continue
            if "🟡 值得投" in line:
                if "DE" in line:
                    current_bucket = "DE 值得投"
                elif "AI" in line:
                    current_bucket = "AI 值得投"
                else:
                    current_bucket = None
                continue
            # 進入新 ## 段（非 bucket header）→ 結束 current_bucket
            if line.startswith("## ") and not ("🔴 必投" in line or "🟡 值得投" in line):
                current_bucket = None
                continue
            if current_bucket is None:
                continue

            stripped = line.strip()
            if not (stripped.startswith("|") and stripped[1:].strip()[:1].isdigit()):
                continue
            cols = [c.strip() for c in stripped.split("|") if c.strip()]
            if len(cols) < 7:
                continue

            company = cols[1][:12]
            # 清掉 title 裡跟公司名重複的部分
            raw_title = cols[2]
            company_core = re.sub(r"(股份有限公司|有限公司|\(.+?\)|（.+?）)", "", cols[1]).strip()
            for pat in [
                rf"^【{re.escape(company_core)}】\s*",
                rf"^\[{re.escape(company_core)}\]\s*",
                rf"^「{re.escape(company_core)}」\s*",
                rf"^{re.escape(company_core)}\s*[-—|｜:：]\s*",
            ]:
                raw_title = re.sub(pat, "", raw_title)
            title = raw_title[:24]
            score = cols[5]
            link_col = cols[6]
            url = ""
            m = re.search(r"\(https?://[^\)]+\)", link_col)
            if m:
                url = m.group(0)[1:-1]

            # 過去推過 → 跳過
            if url and url in pushed_urls:
                continue
            # 本次重複 → 跳過
            if url and url in seen_url:
                continue
            ct_key = (cols[1], cols[2])
            if ct_key in seen_company_title:
                continue
            seen_url.add(url)
            seen_company_title.add(ct_key)
            if url:
                all_new_urls.append(url)
            buckets[current_bucket].append(f"• {company} — {title} (S{score}) {url}")

        # 任一 bucket 不空就有東西要推
        any_jobs = any(buckets.values())

        # 多來源 breakdown（聚合 per source）
        per_source, jd_fetch = _parse_source_breakdown(text)

        if not summary_lines and not any_jobs and not per_source:
            return "💼 **今日職缺**：今天沒有新職缺"

        lines = ["💼 **今日職缺 (AI+DE)**"]
        lines += summary_lines[:4]

        if per_source:
            lines.append("🔍 **各來源**")
            for src, (raw, ok, total) in sorted(per_source.items(), key=lambda x: -x[1][0]):
                mark = "" if ok == total else f" ({ok}/{total} ✅)"
                lines.append(f"- {src}: {raw}{mark}")

        if jd_fetch:
            inline_total = sum(v[2] for v in jd_fetch.values())
            attempted_total = sum(v[0] for v in jd_fetch.values())
            succeeded_total = sum(v[1] for v in jd_fetch.values())
            if attempted_total or inline_total:
                lines.append(
                    f"📥 **JD**：fetch {succeeded_total}/{attempted_total}, listing 內含 {inline_total}"
                )

        # 4 個 bucket 依序輸出（每個都附 URL）
        bucket_emoji = {
            "DE 必投": "🔴",
            "DE 值得投": "🟢",
            "AI 必投": "🔴",
            "AI 值得投": "🟢",
        }
        for bname in ["DE 必投", "DE 值得投", "AI 必投", "AI 值得投"]:
            items = buckets[bname]
            if items:
                lines.append(f"**{bucket_emoji[bname]} {bname}（{len(items)} 間，已過濾推過的）：**")
                lines += items

        if not any_jobs:
            lines.append("（今日無新職缺，全部已推過或被過濾）")

        # 暫存本次新推 URL，main() 推送成功後寫入 pushed_jobs.json
        global _PENDING_PUSH_URLS
        _PENDING_PUSH_URLS = list(all_new_urls)
        return "\n".join(lines)
    except Exception as e:
        return f"💼 **今日職缺** ⚠️ 讀取失敗：{e}"


_PENDING_PUSH_URLS: list = []


# ── 主流程 ────────────────────────────────────────────────────────────────────


def main():
    sections = [daily_todos()]

    bday = upcoming_birthdays()
    if bday:
        sections += ["", bday]

    for part in (crawler_status(), line_bot_status(), git_status(), system_status()):
        if part:
            sections += ["", part]

    suggestions = line_bot_suggestions()
    if suggestions:
        sections += ["", suggestions]

    # 每天嘗試從 Wikiquote 抓一條新雞湯加入 dynamic 池（fail-safe）
    _try_append_today_quote()

    sox = sox_sentiment()
    if sox:
        sections += ["", sox]

    drive = driving_practice()
    if drive:
        sections += ["", drive]

    jobs = job_search_summary()
    if jobs:
        sections += ["", jobs]

    message = "\n".join(sections)

    # Discord 訊息上限 2000 字。超過則拆兩則送（avoid 截斷遺失 URL）。
    # 拆點挑「section 邊界」(空白行)，避免拆到表格中間。
    chunks = _split_for_discord(message, limit=1900)

    sent_text = ""  # 累積真正送出去的內容（用來判斷哪些 URL 確實送到了）
    for ch in chunks:
        ok = send_dm(ch)
        if not ok:
            print("Discord 發送失敗", file=sys.stderr)
            sys.exit(1)
        sent_text += ch + "\n"

    # 推送成功後，把確實出現在訊息裡的職缺 URL 寫入 pushed_jobs.json（永久去重）
    if _PENDING_PUSH_URLS:
        urls_actually_sent = [u for u in _PENDING_PUSH_URLS if u in sent_text]
        if urls_actually_sent:
            existing = _load_pushed_jobs()
            existing.update(urls_actually_sent)
            _save_pushed_jobs(existing)
            print(
                f"已記錄 {len(urls_actually_sent)} 個新職缺 URL → pushed_jobs.json"
                f"（總計 {len(existing)}）"
            )


def _split_for_discord(message: str, limit: int = 1900) -> list:
    """把長訊息拆成多個 ≤ limit 字的 chunk，盡量在 section 邊界（空白行）拆。"""
    if len(message) <= limit:
        return [message]
    chunks = []
    remaining = message
    while len(remaining) > limit:
        # 找 limit 範圍內最後一個 \n\n（section 邊界）
        cut = remaining.rfind("\n\n", 0, limit)
        if cut <= 0:
            # 沒 section 邊界 → 找 limit 內最後一個 \n
            cut = remaining.rfind("\n", 0, limit)
            if cut <= 0:
                cut = limit  # 硬切（罕見）
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


if __name__ == "__main__":
    main()
