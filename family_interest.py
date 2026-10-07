"""家族成員興趣偵測 + 主題新聞摘要。

每週日呼叫一次，從 raw_messages 撈過去 7 天，per-member 抓 top 3 主題，
再從鉅亨 / Yahoo / 中央社抓對應主題的最新新聞標題（不附連結），組成「家族熱話週報」。

設計：
- 4 主成員都偵測（user_aliases.json mapping）
- 細類別 lexicon（投資→台股/美股/ETF/加密；健康→飲食/運動/醫療 等）
- 過去 30 天訊息訊號穩定
- 多 source RSS（鉅亨主、Yahoo + 中央社 fallback）
- 純被動偵測，不要求成員主動訂閱
- 模板輸出，不依賴 Gemini（quota 緊）
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import html
import json
import logging
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

import requests

from config import settings
import output_validator

logger = logging.getLogger("family_interest")

BASE = Path(__file__).parent
DB_PATH = Path(settings.sqlite_path)
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
        "工具": r"ChatGPT|Gemini|Copilot|Midjourney|Stable\s?Diffusion",
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
    try:
        conn = sqlite3.connect(f"{DB_PATH.resolve().as_uri()}?mode=ro", uri=True)
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
    except sqlite3.Error as exc:
        logger.warning("family interest database unavailable: %s", type(exc).__name__)
        return {}

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


# output_validator 的私有規則；改名時退回同義的本地版本（契約測試會先抓到）
_FALLBACK_HIGH_RISK_RE = re.compile(
    r"(?:最新(?:公布|發布|資料|數據|統計)|官方(?:統計|數據|資料)|完整數據|完整資料|統計資料)"
)


def _headline_trips_unverified_data_check(text: str) -> bool:
    """標題含 output_validator 視為「最新／官方數據」的字眼時回 True。"""
    pattern = getattr(output_validator, "_HIGH_RISK_CURRENT_DATA_RE", _FALLBACK_HIGH_RISK_RE)
    return bool(pattern.search(text))


# 標題精簡（Andrew 2026-10-05：標題能簡潔就簡潔）
HEADLINE_MAX_CHARS = 20
# 欄目標籤：〈熱門股〉【焦點】、「影／」「觀察站／」（只認全形／、不含數字，避免吃掉 10/1、美/中）
_HEADLINE_TAG_PREFIX_RE = re.compile(r"^(?:〈[^〉]{1,8}〉|【[^】]{1,8}】|\[[^\]]{1,8}\]|〔[^〕]{1,8}〕|[^／/\s0-9０-９]{1,4}／)\s*")
_HEADLINE_TAG_SUFFIX_RE = re.compile(r"\s*[（(](?:圖|影|組圖|有片|影音|更新)[）)]\s*$")
_HEADLINE_BREAKS = " ，、。；！？：:;!?「"


def _concise_headline(title: str, limit: int = HEADLINE_MAX_CHARS) -> str:
    text = re.sub(r"\s+", " ", html.unescape(title or "").replace("　", " ")).strip()
    for _ in range(3):
        stripped = _HEADLINE_TAG_PREFIX_RE.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    text = _HEADLINE_TAG_SUFFIX_RE.sub("", text).strip()
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    cut = max(head.rfind(ch) for ch in _HEADLINE_BREAKS)
    if cut >= limit // 2:
        head = head[:cut].rstrip(_HEADLINE_BREAKS)
    return head.rstrip() + "…"


# [圖片]／[音訊]／[檔案: 名稱]／[XxxMessageContent] 這類佔位字不是對話內容
_PLACEHOLDER_RE = re.compile(r"^\[[^\[\]\n]{1,60}\]$")
_MESSAGE_URL_RE = re.compile(
    r"https?://\S+|www\.\S+|(?<![A-Za-z0-9-])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?:/[!-~]*)?",
    re.IGNORECASE,
)


def fetch_member_messages(
    group_id: str, days: int = 7, per_member_limit: int = 40, max_chars: int = 150
) -> Dict[str, List[str]]:
    """{member_name: [文字訊息, ...]}，舊到新、每人最多 per_member_limit 則（含妹妹）。"""
    aliases = _load_aliases()
    if not aliases:
        return {}

    since_ts = int(time.time()) - days * 86400
    try:
        conn = sqlite3.connect(f"{DB_PATH.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT user_id, text FROM raw_messages "
                "WHERE group_id = ? AND created_at > ? AND user_id != '__bot__' "
                "ORDER BY created_at",
                (group_id, since_ts),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("family interest database unavailable: %s", type(exc).__name__)
        return {}

    result: Dict[str, List[str]] = {}
    for user_id, text in rows:
        name = aliases.get(user_id)
        text = (text or "").strip()
        if not name or not text or _PLACEHOLDER_RE.match(text):
            continue
        text = _MESSAGE_URL_RE.sub("[連結]", text).strip()
        result.setdefault(name, []).append(text[:max_chars])
    return {name: texts[-per_member_limit:] for name, texts in result.items()}


def prefetch_news(
    group_id: str, days: int = 7, news_per_topic: int = 2
) -> Tuple[Dict[str, List[Tuple[str, int]]], Dict[str, List[Tuple[str, str]]]]:
    """回傳 (每人話題, 每個話題的新聞)；網路抓取都在這裡做完。"""
    per_member = detect_per_member_topics(group_id, days=days)
    all_topics = sorted({t for topics in per_member.values() for t, _ in topics})
    # 來源相同的話題只抓一次，並行抓（每個請求本身有 8 秒逾時）
    by_sources: Dict[tuple, List[str]] = {}
    for topic in all_topics:
        key = tuple(url for _, url in _TOPIC_SOURCES.get(topic, [])) or (topic,)
        by_sources.setdefault(key, []).append(topic)
    news_by_topic: Dict[str, List[Tuple[str, str]]] = {}
    if by_sources:
        with ThreadPoolExecutor(max_workers=min(6, len(by_sources))) as pool:
            futures = {
                pool.submit(fetch_topic_news, topics[0], max_items=news_per_topic): topics
                for topics in by_sources.values()
            }
            for future, topics in futures.items():
                items = future.result()
                for topic in topics:
                    news_by_topic[topic] = items
    return per_member, news_by_topic


def render_summary(
    group_id: str,
    days: int = 7,
    news_per_topic: int = 2,
    insights: Dict[str, List[str]] | None = None,
    per_member: Dict[str, List[Tuple[str, int]]] | None = None,
    news_by_topic: Dict[str, List[Tuple[str, str]]] | None = None,
) -> str:
    """產出可直接 push LINE / Discord 的摘要文字。

    per_member／news_by_topic 已先抓好時直接用，不再連網；insights 是
    family_weekly_insight 產生的每人小建議（最多 2 點）。
    """
    if per_member is None or news_by_topic is None:
        per_member, news_by_topic = prefetch_news(group_id, days, news_per_topic)
    insights = insights or {}

    period = "本週" if days == 7 else f"{days}天"
    lines = [f"👨‍👩‍👧‍👦 家族熱話週報（{period}）", ""]
    member_blocks = 0
    configured_order = list(dict.fromkeys(_load_aliases().values()))
    for name in configured_order:
        topics = per_member.get(name) or []
        points = insights.get(name) or []
        if not topics and not points:
            continue
        if topics:
            topic_str = " ".join(f"{t.replace('-', '')}({c})" for t, c in topics)
            lines.append(f"{name} ▸ {topic_str}")
        else:
            lines.append(name)
        shown: set[str] = set()
        for t, _ in topics[:3]:
            # 不附來源連結（Andrew 2026-10-04），只留精簡標題。沒有連結時，
            # 含「最新公布／官方數據」等字、或其他推播驗證會擋的標題會讓整則
            # 週報被擋下，所以改用下一則新聞；都不行就不列。同一人不重複列。
            for title, _url in news_by_topic.get(t, []):
                short = _concise_headline(title)
                if (
                    short
                    and short not in shown
                    and not _headline_trips_unverified_data_check(short)
                    and output_validator.validate_outbound_text(short).ok
                ):
                    lines.append(f"  📰 {short}")
                    shown.add(short)
                    break
        for point in points[:2]:
            lines.append(f"  💡 {point}")
        lines.append("")
        member_blocks += 1
    if not member_blocks:
        return ""
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
