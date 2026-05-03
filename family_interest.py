"""家族成員興趣偵測 + 主題新聞摘要。

每週日呼叫一次，從 raw_messages 撈過去 30 天，per-member 抓 top 3 主題，
再從鉅亨 / Yahoo / 中央社抓對應主題的最新新聞，組成「家族熱話週報」。

設計：
- 4 主成員都偵測（user_aliases.json mapping）
- 細類別 lexicon（投資→台股/美股/ETF/加密；健康→飲食/運動/醫療 等）
- 過去 30 天訊息訊號穩定
- 多 source RSS（鉅亨主、Yahoo + 中央社 fallback）
- 純被動偵測，不要求成員主動訂閱
- 模板輸出，不依賴 Gemini（quota 緊）
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

import requests

logger = logging.getLogger("family_interest")

BASE = Path(__file__).parent
DB_PATH = BASE / "line_bot.db"
ALIASES_PATH = BASE / "user_aliases.json"


# ── 細類別 lexicon ────────────────────────────────────────────────────────
# 結構：{大類: {細類: regex pattern}}
# 偵測到細類就同時計大類；計分時細類優先（更精準）
_LEXICON = {
    "投資": {
        "台股": r"台積電|2330|0050|0056|金控|台股|加權|大盤|集中市場",
        "美股": r"美股|Nvidia|輝達|Tesla|蘋果|AAPL|TSLA|NVDA|S&P|納指|標普|道瓊|那斯達克",
        "ETF": r"ETF|0050|0056|VTI|VOO|QQQ|SPY|009802|00878|00919|00929",
        "加密": r"比特幣|BTC|以太|ETH|加密貨幣|Crypto|Solana|Doge",
        "總經": r"關稅|聯準會|Fed|FOMC|降息|升息|通膨|GDP|失業率|PMI",
    },
    "健康": {
        "飲食": r"營養|纖維|蛋白|脂肪|糖分|澱粉|蔬菜|水果|健康食品|益生菌|維他命",
        "運動": r"重訓|跑步|瑜珈|健身|有氧|hiit|核心肌群|拉筋",
        "醫療": r"醫|生病|看診|住院|手術|血壓|血糖|疫苗|藥|門診|急診",
    },
    "政治": {
        "國內": r"賴清德|柯文哲|藍白|民進|國民黨|立委|罷免|公投|抗議",
        "國際": r"川普|拜登|普丁|習近平|烏克蘭|俄羅斯|以色列|哈瑪斯|關稅戰|貿易戰",
    },
    "食物": {
        "料理": r"食譜|做菜|滷|蒸|炒|湯|麵|飯|餃|包子|料理",
        "餐廳": r"餐廳|預約|訂位|排隊|米其林|必比登|宵夜|團購",
    },
    "旅遊": {
        "國內": r"高雄|台南|花蓮|台東|墾丁|宜蘭|九份|溫泉",
        "國外": r"日本|東京|京都|大阪|韓國|首爾|新加坡|泰國|越南|歐洲|美國",
    },
    "AI": {
        "工具": r"ChatGPT|Claude|Gemini|Copilot|Midjourney|Stable\s?Diffusion",
        "應用": r"AI 圖|AI 影片|AI 配音|AI 翻譯|AI 寫作|RAG|MCP",
        "趨勢": r"AGI|大模型|LLM|生成式|算力|推理|訓練",
    },
}


def _load_aliases() -> Dict[str, str]:
    if not ALIASES_PATH.exists():
        return {}
    try:
        with open(ALIASES_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def detect_per_member_topics(group_id: str, days: int = 30) -> Dict[str, List[Tuple[str, int]]]:
    """{member_name: [(主題-細類, 觸發次數), ...]} top 3 per member。

    主題-細類格式：「投資-台股」「健康-飲食」便於後續找對應 RSS。
    """
    aliases = _load_aliases()
    if not aliases:
        return {}

    since_ts = int(time.time()) - days * 86400
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, text FROM raw_messages "
            "WHERE group_id = ? AND created_at > ? AND user_id != '__bot__'",
            (group_id, since_ts),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    # per user → Counter of "大類-細類"
    per_user: Dict[str, Counter] = {}
    for user_id, text in rows:
        if user_id not in aliases:
            continue
        per_user.setdefault(user_id, Counter())
        for big, subs in _LEXICON.items():
            for sub, pat in subs.items():
                if re.search(pat, text or "", re.IGNORECASE):
                    per_user[user_id][f"{big}-{sub}"] += 1

    # 換成 alias name + top 3
    result: Dict[str, List[Tuple[str, int]]] = {}
    for uid, counter in per_user.items():
        name = aliases.get(uid, uid[:8])
        top3 = counter.most_common(3)
        result[name] = [(t, c) for t, c in top3 if c >= 2]  # 至少 2 次才算興趣
    return result


# ── RSS 聚合 ──────────────────────────────────────────────────────────────

# 主題 → RSS / API URL 對應（per Q4=B：鉅亨 + Yahoo + 中央社）
_TOPIC_SOURCES = {
    "投資-台股": [
        ("鉅亨", "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock_news?limit=3"),
    ],
    "投資-美股": [
        ("鉅亨", "https://api.cnyes.com/media/api/v1/newslist/category/wd_stock?limit=3"),
    ],
    "投資-ETF": [
        ("鉅亨", "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock_etf?limit=3"),
    ],
    "投資-加密": [
        ("鉅亨", "https://api.cnyes.com/media/api/v1/newslist/category/cnyescom_blockchain?limit=3"),
    ],
    "投資-總經": [
        ("鉅亨", "https://api.cnyes.com/media/api/v1/newslist/category/headline?limit=3"),
    ],
    "政治-國內": [
        ("中央社 RSS", "https://feeds.feedburner.com/rsscna/politics"),
    ],
    "政治-國際": [
        ("中央社 RSS", "https://feeds.feedburner.com/rsscna/intworld"),
    ],
    "健康-醫療": [
        ("Yahoo 健康", "https://tw.news.yahoo.com/rss/health"),
    ],
    "健康-飲食": [
        ("Yahoo 健康", "https://tw.news.yahoo.com/rss/health"),
    ],
    "健康-運動": [
        ("Yahoo 運動", "https://tw.news.yahoo.com/rss/sports"),
    ],
    "食物-料理": [
        ("Yahoo 美食", "https://tw.news.yahoo.com/rss/food"),
    ],
    "食物-餐廳": [
        ("Yahoo 美食", "https://tw.news.yahoo.com/rss/food"),
    ],
    "旅遊-國內": [
        ("Yahoo 旅遊", "https://tw.news.yahoo.com/rss/travel"),
    ],
    "旅遊-國外": [
        ("Yahoo 旅遊", "https://tw.news.yahoo.com/rss/travel"),
    ],
    "AI-工具": [
        ("鉅亨 AI", "https://api.cnyes.com/media/api/v1/newslist/category/headline?limit=5"),
    ],
    "AI-應用": [
        ("鉅亨 AI", "https://api.cnyes.com/media/api/v1/newslist/category/headline?limit=5"),
    ],
    "AI-趨勢": [
        ("鉅亨 AI", "https://api.cnyes.com/media/api/v1/newslist/category/headline?limit=5"),
    ],
}


def fetch_topic_news(topic: str, max_items: int = 2) -> List[Tuple[str, str]]:
    """回傳 [(title, url), ...] for given topic-subtopic key."""
    sources = _TOPIC_SOURCES.get(topic, [])
    items: List[Tuple[str, str]] = []
    for src_name, url in sources:
        try:
            r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                continue
            # 鉅亨 JSON API
            if "api.cnyes.com" in url:
                data = r.json()
                for it in (data.get("items", {}).get("data", []) or [])[:max_items]:
                    title = it.get("title", "").strip()
                    news_id = it.get("newsId")
                    if title and news_id:
                        items.append((title, f"https://news.cnyes.com/news/id/{news_id}"))
            # Yahoo / 中央社 RSS（XML）
            else:
                # 抓 <item> 內部的 title/link，避免 <channel>/<image> 混進來
                item_blocks = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)
                for block in item_blocks[:max_items]:
                    t_match = re.search(
                        r"<title[^>]*>(?:<!\[CDATA\[)?(.+?)(?:\]\]>)?</title>",
                        block, re.DOTALL,
                    )
                    l_match = re.search(r"<link[^>]*>([^<]+)</link>", block)
                    if t_match and l_match:
                        items.append((t_match.group(1).strip(), l_match.group(1).strip()))
            if items:
                break  # 第一個 source 成功就停
        except Exception as e:
            logger.warning("fetch_topic_news %s %s: %s", topic, src_name, e)
            continue
    return items[:max_items]


def render_summary(group_id: str, days: int = 30, news_per_topic: int = 2) -> str:
    """產出可直接 push LINE / Discord 的摘要文字。"""
    per_member = detect_per_member_topics(group_id, days=days)
    if not per_member:
        return ""

    lines = [f"👨‍👩‍👧‍👦 家族熱話週報（{days}天）", ""]

    # 收集所有觸發過的主題，去重後抓新聞（避免同主題重複 fetch）
    all_topics = set()
    for topics in per_member.values():
        for t, _ in topics:
            all_topics.add(t)
    news_by_topic: Dict[str, List[Tuple[str, str]]] = {}
    for t in all_topics:
        news_by_topic[t] = fetch_topic_news(t, max_items=news_per_topic)

    for name in ["媽媽", "爸爸", "黃聖雅", "黃聖穎"]:
        if name not in per_member or not per_member[name]:
            continue
        topics = per_member[name]
        topic_str = " ".join(f"{t.replace('-', '')}({c})" for t, c in topics)
        lines.append(f"{name} ▸ {topic_str}")
        for t, _ in topics[:3]:
            news = news_by_topic.get(t, [])
            if news:
                title, url = news[0]
                short = title[:30] + "…" if len(title) > 30 else title
                lines.append(f"  📰 {short}")
                lines.append(f"  {url}")
        lines.append("")
    return "\n".join(lines).rstrip()


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
    gid = os.environ.get("LINE_ALLOWED_GROUP_ID") or os.environ.get("ALLOWED_GROUP_ID", "")
    if not gid:
        print("ERR: LINE_ALLOWED_GROUP_ID 未設")
        raise SystemExit(1)
    print(render_summary(gid))
