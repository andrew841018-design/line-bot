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


def daily_todos() -> str:
    now = datetime.now()
    today = now.strftime("%m/%d")
    weekday = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
    push_time = now.strftime("%H:%M")
    return (
        f"📌 **每日待辦** ({today} 週{weekday} {push_time})\n"
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


# ── 5.5 費城半導體指數 (^SOX) 乖離率 + 買賣建議 ───────────────────────────────

# 心靈雞湯池 — 依市場情緒分桶，每天用日期 hash 選不同句
# 每句配一條歷史佐證（事件 / 數據 / 名人語錄），不只勵志還能說服自己
# 結構：(quote, evidence)
_QUOTES = {
    "雙過熱": [  # 警惕貪婪
        ("市場的錢是賺不完的，少賺不會死，套住才會。",
         "Buffett 1969 道瓊 800 點時關閉合夥事業全還錢給合夥人；市場 4 年後在 1973 觸頂，1974 崩 -45%。他在 580 點時才大舉回場買 Washington Post，10 年漲 12 倍。"),
        ("別人貪婪時要恐懼。街頭討論晶片股的計程車司機變多了嗎？",
         "1987、2000、2007 三次大頂前，散戶券商開戶數都爆衝。Robinhood 2021 Q1 新開戶 600 萬戶創高點，3 個月後 ARKK 開始崩 -67%。"),
        ("頂部不會敲鑼打鼓。你覺得「再漲一點就賣」，往往就是該賣的時候。",
         "Nasdaq 2000/3/10 創 5048 點，當週 BusinessWeek 封面標題《Tech is the new economy》。3 個月後跌 -35%，3 年跌 -78%。"),
        ("獲利再投入是複利之友，但全部 all in 高點是複利之敵。",
         "1929 道瓊頂 381 點，回到此價位等了 25 年（1954）。2000 Nasdaq 5048 點，回到此價位等了 15 年（2015）。"),
        ("減碼不是看空，是把籌碼換成選擇權——下次大跌時的子彈。",
         "Druckenmiller 1999 預警網路泡沫但被迫追高，當年多賺 50%，2000 全部回吐。他自評「最大遺憾是沒在頂部留現金」。"),
        ("華爾街沒有新鮮事。每一次「這次不一樣」最後都一樣。",
         "李佛摩名言。1929（汽車電氣新時代）、1973（漂亮 50）、2000（網路新經濟）、2008（房地產不會跌）、2022（FAANG 永動機）— 5 次崩盤前 narrative 一致。"),
        ("高處不勝寒。在無人質疑的多頭中保留現金，是最孤獨也最珍貴的決定。",
         "Michael Burry 2007 在房市頂時建 CDS 空頭，被基金投資人罵 6 個月要求贖回；最終《The Big Short》賺 7.5 億美元。"),
        ("你不需要每一塊錢都賺到。少賺最後 20% 的人，才能避開 -40% 的回撤。",
         "數學鐵則：賺最後 20% 後跌 40%，淨值剩 0.72；早 20% 出場保 1.0。前者要再漲 +39% 才追平，需要好幾年。"),
        ("牛市裡最重的代價，是不肯下車的人付的。",
         "Cisco 2000 從 80 跌到 8，25 年至今未回前高。GE 2008 從 60 跌到 6，17 年未回。ARKK 2021 從 159 跌到 30。"),
        ("市場給你機會用恐懼買、用貪婪賣，多數人卻顛倒過來。",
         "JP Morgan《Guide to Retirement》資金流統計：散戶在 2009/3、2020/3 兩個歷史底部前 3 個月淨贖回，在 2007/10、2021/11 兩個頂前 3 個月淨買進。"),
    ],
    "單過熱": [  # 提醒謹慎
        ("上漲時忘記停損，下跌時才會記得。趁理性還在，先想好出場條件。",
         "行為金融學「處置效應」(Odean 1998)：散戶賠錢部位平均持有 124 天，賺錢部位只 104 天就賣 — 賠錢硬抱比賺錢落袋多撐 19%。"),
        ("有計畫的賣出叫紀律，沒計畫的賣出叫恐慌。",
         "Vanguard 2014《Advisor's Alpha》研究：有書面投資計畫的客戶 30 年年化回報比沒計畫者高 3%，複利下差 2.4 倍資產。"),
        ("當你開始算「賺多少可以辭職」時，就是該檢視部位的時候。",
         "1999 矽谷工程師大規模辭職創業；2021 YouTube 充滿「30 歲 FIRE 財富自由」影片。前者隔年崩盤、後者隔年加密 -80%。"),
        ("好公司不一定是好價格。耐心等回檔，是專業與業餘的分水嶺。",
         "Buffett：「以好價格買好公司，比以好公司價買好公司重要」。他 1973 用 130 美元買 Washington Post 被笑貴，10 年後漲到 1,500。"),
        ("風險不是波動，是你買貴了還不知道。",
         "Cisco 2000 P/E 200 倍時被分析師認為「合理反映新經濟」。25 年後股價仍未回前高，但 EPS 已成長 4 倍 — 證明買貴才是真風險。"),
        ("市場永遠對。你的解釋只是事後諸葛。",
         "Keynes 1936 名言：「Markets can stay irrational longer than you can stay solvent.」（市場保持非理性的時間，可以長過你保持償付能力的時間）"),
        ("獲利沒入袋只是浮雲，浮雲會散。",
         "Cathie Wood 2021 帳面 ARKK 浮盈讓無數人賣房 all in，2022 跌 -67%。只有當年 take profit 的人保住資產。"),
    ],
    "中性": [  # 持有觀望
        ("投資最難的不是判斷，是「什麼都不做」。",
         "Fidelity 2014 內部研究：表現最好的帳戶都是「忘記登入」或「持有人已死亡」的客戶 — 因為他們不交易，沒踩追高殺低。"),
        ("時間是好公司的朋友，是平庸公司的敵人。",
         "Buffett 原話。Berkshire 1965-2024 年化 19.8% vs S&P 10.2% — 60 年複利威力差 140 倍。"),
        ("別交易市場，要持有資產。",
         "S&P 500 1990-2020：錯過 10 個最佳交易日，年化從 7.7% 降到 5.0%；錯過 30 個，剩 0.6%。所有最佳日通常緊鄰最差日，無法擇時。"),
        ("10 年 10 倍不是夢，前提是你能撐過中間 5 次的 -30%。",
         "蘋果 2003-2023 漲 250 倍，但中間經歷 2008 -60%、2013 -45%、2015 -32%、2019 -38%、2020 -32% — 不持有就拿不到複利。"),
        ("看盤太頻繁，是把投資做成賭博。",
         "DALBAR《QAIB 2023 報告》25 年數據：頻繁交易散戶年化 3.9% vs S&P 9.8%，落後 5.9%。差距複利下 30 年差 5 倍。"),
        ("資產配置先做對，個股選擇再優化。",
         "Brinson, Hood, Beebower 1986 經典研究：投資組合回報 91.5% 由資產配置決定，個股選擇只佔 4.6%，擇時 1.8%。"),
        ("你的長期回報，取決於你能忍住不動的那段時間有多長。",
         "Peter Lynch 1977-1990 Magellan 基金年化 29.2%，但 Fidelity 統計平均投資人實際回報 -4% — 因為都追高殺低。"),
    ],
    "買區": [  # 鼓勵進場
        ("市場給好價格時不要嫌貴，給壞臉色時不要害怕。",
         "Buffett 2008/10/16《NYT》專欄〈Buy American. I Am.〉，當時 S&P 1000 點所有人逃命；5 年後 S&P 1800，翻倍。"),
        ("下跌不是風險，買貴才是。",
         "蒙格名言：「真正的風險是你付太多了，不是價格波動」。波動是長期投資人的朋友。"),
        ("別人恐懼時要貪婪。今天的鈔票就是明天的股票。",
         "2020/3/23 S&P 觸底 2191，所有人預測再跌 40%。3 年後 S&P 4500，漲 +105%。底部最不舒服，但回報最大。"),
        ("好公司在打折時加碼，是時間給長期投資人的紅利。",
         "Costco 2009 大跌 -32% 時 P/E 從 22 降到 17，當時加碼者至今 16 年漲 18 倍（含股息）。"),
        ("你的下一個十年，往往從這種「沒人想討論股票」的時刻開始。",
         "2002/10、2009/3、2020/3 三個歷史底部，Google Trends 上「stock market」搜尋量都跌到 5 年低點。冷清才是入場時。"),
        ("分批進場是給未知的禮物。一次梭哈是對未知的傲慢。",
         "Vanguard 2012 研究：DCA（定期定額）在 1929-1932、2000-2002、2008-2009 三大空頭期都跑贏 Lump Sum 一次性投入 5-15%。"),
        ("回檔不是錯誤，是市場給有準備的人的入場券。",
         "S&P 500 過去 50 年平均每年最大回檔 -14%，但 75% 的年份仍以正報酬收場。回檔是常態不是異常。"),
    ],
    "深跌": [  # 逆向勇氣
        ("鮮血中買進。當所有人都說「再跌都不奇怪」時，往往離底部不遠。",
         "Rothschild 名言。1815 滑鐵盧戰役英國公債暴跌，Nathan Rothschild 大量買進；戰後消息明朗，價格反彈 +40%，奠定家族財富。"),
        ("歷史上每一次「世界要完了」最後都沒完，活下來的人都贏了。",
         "1929 大蕭條、1973 石油危機、1987 黑色星期一、2000 網路泡沫、2008 金融海嘯、2020 COVID — 6 次危機後 S&P 都復原並創新高。"),
        ("市場最便宜的時候，永遠是最不舒服的時候。",
         "1932/7 道瓊 41 點（從 381 跌 -89%），失業率 25%、銀行倒閉 9,000 家。20 年後道瓊 600 點，1.5 倍漲幅來自此底買進者。"),
        ("今天買進的，是 3 年後別人 FOMO 想要的。",
         "Nvidia 2022/10 跌到 108 美元被嫌貴。2024/6 創 1,340 美元，21 個月漲 12.4 倍。當時敢買的人是現在所有人羨慕的對象。"),
        ("你不需要抓到最低點。在恐慌中分批買進，已經贏 90% 的人。",
         "DALBAR 統計：散戶平均在大跌底部前 30 天到底部後 60 天之間「淨贖回」，錯過反彈最大段。"),
        ("現金在熊市底部最有價值，這是你 6 個月前買飲料時想不到的。",
         "1932 美國 25% 失業率時，1 美元現金能買到 1929 年 5 美元的同一家公司股票。現金在恐慌中購買力放大 5 倍。"),
        ("市場會忘記昨天的恐懼，只記得明天的價格。",
         "2008/11 S&P 跌至 752 點時所有媒體預測「金融體系崩潰」，1 年後 S&P 回到 1,100，3 年後 1,300。恐慌只持續 6 個月。"),
    ],
}


def _market_quote(bias_20: float, bias_60: float) -> str:
    """根據雙線乖離選情緒桶，日期 hash 每天輪一句。"""
    # 桶判定（與 _signal_* 一致的 threshold）
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

    pool = _QUOTES[bucket]
    idx = datetime.now().toordinal() % len(pool)
    quote, evidence = pool[idx]
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
        return "\n".join(lines)
    except Exception as e:
        return f"📈 **費半指數** ⚠️ 抓取失敗：{e}"


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

    prompt = f"""你是 LINE bot 的產品顧問。任務：根據近 3 天群組對話，主動發想 1 個讓 Andrew 的家族 LINE bot 變更實用的新功能。

【近 3 天群組對話樣本（最重要！從這裡找痛點）】
{chr(10).join(recent_msgs) if recent_msgs else "(無樣本，但仍可基於 Andrew 個人需求發想)"}

【📋 已存在的待建議（未實作但已記下）】
{json.dumps(pending_list, ensure_ascii=False)}

【🚫 不要重複的（已實作或已拒絕，名稱相同就不行）】
{json.dumps(blacklist, ensure_ascii=False)}

規則：
1. **積極發想**：對話樣本只要有「資訊缺口、查詢需求、誤解、麻煩」就值得提建議。寧可大膽嘗試也不要消極回 null
2. **黑名單只擋名稱完全相同**：如果你的新點子用不同方式解決類似問題、或從不同角度切入，就 OK
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

    sox = sox_sentiment()
    if sox:
        sections += ["", sox]

    jobs = job_search_summary()
    if jobs:
        sections += ["", jobs]

    message = "\n".join(sections)

    # Discord 訊息上限 2000 字。截斷時要重算還在訊息內的 URL，
    # 避免被截掉的職缺被誤記為「已推」導致下次永遠不再推
    truncated = False
    if len(message) > 1900:
        message = message[:1900] + "\n…（截斷）"
        truncated = True

    ok = send_dm(message)
    if not ok:
        print("Discord 發送失敗", file=sys.stderr)
        sys.exit(1)

    # 推送成功後，把確實出現在 message 裡的職缺 URL 寫入 pushed_jobs.json（永久去重）
    if _PENDING_PUSH_URLS:
        urls_actually_sent = (
            [u for u in _PENDING_PUSH_URLS if u in message]
            if truncated
            else list(_PENDING_PUSH_URLS)
        )
        if urls_actually_sent:
            existing = _load_pushed_jobs()
            existing.update(urls_actually_sent)
            _save_pushed_jobs(existing)
            print(
                f"已記錄 {len(urls_actually_sent)} 個新職缺 URL → pushed_jobs.json"
                f"（總計 {len(existing)}）"
                + (f"；截斷捨棄 {len(_PENDING_PUSH_URLS) - len(urls_actually_sent)} 個" if truncated else "")
            )


if __name__ == "__main__":
    main()
