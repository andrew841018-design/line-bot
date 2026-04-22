"""
LINE → FastAPI webhook → Gemini → LINE

回應觸發條件：
1. Explicit：@mention bot，或用 /ai /問 /ask 前綴 → 立刻回
2. Implicit：任何文字訊息都進 burst_filter 佇列 → 30 秒 debounce →
   規則 + 啟發式 + Gemini classifier 判定「值得回」才回（絕大多數情況不回）

訊息類型：
- 文字：走上面兩條路徑之一；URL 會由 burst 啟發式觸發，Gemini 自帶 url_context 會讀網頁
- 圖片 / 影片 / 音訊：預設不主動處理。唯一例外：使用者 @mention 並引用該媒體，
  就下載 bytes 丟給 Gemini multimodal 分析。
- 文字檔：自動分析

指令列表請用 /help 查看。
"""
from __future__ import annotations

import logging
import mimetypes
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests as _requests
from bs4 import BeautifulSoup

try:
    import yt_dlp as _yt_dlp
    _YTDLP_AVAILABLE = True
except ImportError:
    _yt_dlp = None
    _YTDLP_AVAILABLE = False

from fastapi import FastAPI, Header, HTTPException, Request
from google.genai import types
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    MessagingApiBlob,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    AudioMessageContent,
    FileMessageContent,
    GroupSource,
    ImageMessageContent,
    JoinEvent,
    LeaveEvent,
    MemberJoinedEvent,
    MemberLeftEvent,
    MessageEvent,
    TextMessageContent,
    VideoMessageContent,
)

import burst_filter
import feedback_collector
import gemini_client
import memory
import review
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)


# log 時間戳用 PT（Gemini quota 以這個為準）+ TW（使用者看這個）雙時區，
# 避免「現在才 0800 為什麼 quota 就爆了」的誤判 — Gemini 的「一天」是 PT 的一天。
class _DualTZFormatter(logging.Formatter):
    _PT = ZoneInfo("America/Los_Angeles")
    _TW = ZoneInfo("Asia/Taipei")

    def formatTime(self, record, datefmt=None):
        pt = datetime.fromtimestamp(record.created, tz=self._PT)
        tw = datetime.fromtimestamp(record.created, tz=self._TW)
        return f"{pt.strftime('%m-%d %H:%M:%S')} PT ({tw.strftime('%H:%M')} TW)"


for _h in logging.getLogger().handlers:
    _h.setFormatter(_DualTZFormatter(
        "%(asctime)s %(levelname)s %(name)s | %(message)s"
    ))

logger = logging.getLogger("line_bot")

app = FastAPI()

_parser = WebhookParser(settings.line_channel_secret)
_line_config = Configuration(access_token=settings.line_channel_access_token)


# ── LINE 訊息配額 ─────────────────────────────────────────────────────────────

def _get_quota_footer() -> str:
    """每次回應時即時查詢配額，失敗回空字串（不讓 reply 爆錯）。"""
    try:
        headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
        q = _requests.get(
            "https://api.line.me/v2/bot/message/quota", headers=headers, timeout=3
        ).json()
        u = _requests.get(
            "https://api.line.me/v2/bot/message/quota/consumption",
            headers=headers, timeout=3,
        ).json()
        if q.get("type") == "none":
            return ""
        limit = q.get("value", 0)
        used = u.get("totalUsage", 0)
        remaining = limit - used
        return f"\n\n📊 本月配額：剩 {remaining} 則（已用 {used} / {limit}）"
    except Exception:
        return ""


# ── URL 預抓取（繞過 Gemini url_context 的限制）─────────────────────────────

_URL_RE = re.compile(r"https?://\S+")
_PREFETCH_TIMEOUT = 5        # 秒，避免拖太久讓 reply_token 過期
_PREFETCH_MAX_CHARS = 5000   # 截斷上限，避免塞爆 prompt
_PREFETCH_MAX_URLS = 2       # 一次最多抓幾個連結
_PREFETCH_MIN_CHARS = 80     # 低於此長度視為垃圾（JS 渲染空殼），不塞進 prompt

_YTDLP_TIMEOUT = 12          # yt-dlp 單次提取上限（秒）
_YTDLP_SUBTITLE_MAX_CHARS = 3000
_YTDLP_SUBTITLE_LANGS = ["zh-TW", "zh-Hant", "zh", "zh-Hans", "en"]

# JS 渲染 / Cloudflare 保護的網站，requests.get() 抓不到有效內容
# 這些網站一律不 prefetch，直接讓 Gemini 用 Google Search 處理
_JS_RENDERED_DOMAINS = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*("
    r"tiktok\.com|instagram\.com|threads\.net|facebook\.com|fb\.watch|"
    r"dcard\.tw|x\.com|twitter\.com|reddit\.com|"
    r"youtube\.com/shorts|youtu\.be"
    r")/", re.IGNORECASE
)

# TikTok 短網址 pattern（vt.tiktok.com / vm.tiktok.com），需先 redirect 才能丟 oEmbed
_TIKTOK_SHORT_DOMAIN = re.compile(r"https?://(?:vt|vm)\.tiktok\.com/", re.IGNORECASE)
# 從 oEmbed html 欄位抽背景音樂
# html 結構：<a title="♬ xxx" href="..."> ♬ xxx</a>，title 裡也有 ♬ 會誤匹配，
# 所以要求 ♬ 前面必須是 `>`（真正的 anchor content，不是屬性值）
_TIKTOK_MUSIC_RE = re.compile(r">\s*♬\s*([^<]+?)\s*</a>", re.UNICODE)


def _parse_vtt(vtt_text: str) -> str:
    """WebVTT → 純文字，去掉時間碼、HTML tag、相鄰重複行。"""
    lines = []
    for line in vtt_text.splitlines():
        line = line.strip()
        if not line or line.startswith("WEBVTT") or "-->" in line or line.isdigit():
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if line:
            lines.append(line)
    deduped = []
    for ln in lines:
        if not deduped or ln != deduped[-1]:
            deduped.append(ln)
    return "\n".join(deduped)


def _extract_subtitles_from_info(info: dict) -> str | None:
    """從 yt-dlp info dict 拿字幕文字（優先人工字幕 → 自動生成，語言優先順序見常數）。"""
    for subs_dict in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        for lang in _YTDLP_SUBTITLE_LANGS:
            entries = subs_dict.get(lang)
            if not entries:
                continue
            entry = next((e for e in entries if e.get("ext") == "vtt"), entries[0])
            sub_url = entry.get("url") if entry else None
            if not sub_url:
                continue
            try:
                resp = _requests.get(sub_url, timeout=8)
                resp.raise_for_status()
                text = _parse_vtt(resp.text)
                if text and len(text) > 50:
                    if len(text) > _YTDLP_SUBTITLE_MAX_CHARS:
                        text = text[:_YTDLP_SUBTITLE_MAX_CHARS] + "…（字幕截斷）"
                    return text
            except Exception as e:
                logger.debug("subtitle download failed lang=%s: %s", lang, e)
    return None


def _fetch_video_ytdlp(url: str) -> str | None:
    """
    用 yt-dlp 抓影片 metadata + 字幕，支援 YouTube、TikTok、IG、FB、X 等 1000+ 網站。

    優先抓字幕（中文 > 英文）；沒字幕就用 title + description。
    任何錯誤都回 None（讓 caller fallback）。
    """
    if not _YTDLP_AVAILABLE:
        return None
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": _YTDLP_TIMEOUT,
        }
        with _yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None

        title = (info.get("title") or "").strip()
        uploader = (info.get("uploader") or info.get("channel") or "").strip()
        description = (info.get("description") or "").strip()
        duration = info.get("duration")

        if not title and not uploader:
            return None

        lines = [f"（以下是影片連結 {url} 的內容，透過 yt-dlp 擷取）"]
        lines.append("--- 影片資訊開始 ---")
        if title:
            lines.append(f"標題：{title}")
        if uploader:
            lines.append(f"上傳者：{uploader}")
        if duration:
            m, s = divmod(int(duration), 60)
            lines.append(f"長度：{m}:{s:02d}")
        if description:
            desc = description[:500] + "…" if len(description) > 500 else description
            lines.append(f"描述：{desc}")

        subtitle_text = _extract_subtitles_from_info(info)
        if subtitle_text:
            lines.append(f"\n字幕內容：\n{subtitle_text}")

        lines.append("--- 影片資訊結束 ---")
        block = "\n".join(lines)
        logger.info("ytdlp OK url=%s chars=%d has_subs=%s", url, len(block), bool(subtitle_text))
        return block
    except Exception as e:
        logger.info("ytdlp failed url=%s: %s", url, e)
        return None


def _fetch_tiktok_meta(url: str) -> str | None:
    """
    TikTok 專用 prefetch：走官方 oEmbed API（公開 endpoint，免 token）取 caption / 作者 / 音樂。

    為什麼要這層：TikTok 是 JS 渲染，requests.get() 只抓到空殼；而 Gemini url_context
    對 TikTok 實測 100% 回空字串（連三次 empty reply 後 raise RuntimeError）。
    oEmbed endpoint 直接吐 JSON，能拿到 title（caption + hashtags）/ author_name /
    author_unique_id / html（內含音樂資訊）。

    失敗時回 None，由 caller fallback 回原本 skip 行為，不會退步。
    """
    try:
        # 短網址（vt.tiktok.com / vm.tiktok.com）先 HEAD follow redirect 拿完整 URL
        target_url = url
        if _TIKTOK_SHORT_DOMAIN.search(url):
            try:
                r = _requests.head(
                    url, timeout=_PREFETCH_TIMEOUT,
                    allow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
                )
                target_url = r.url
                logger.info("tiktok short url resolved: %s → %s", url, target_url)
            except Exception as e:
                logger.info("tiktok short url resolve failed url=%s: %s", url, e)
                return None

        # 呼叫 oEmbed API
        resp = _requests.get(
            "https://www.tiktok.com/oembed",
            params={"url": target_url},
            timeout=_PREFETCH_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        if resp.status_code != 200:
            logger.info("tiktok oembed HTTP %d url=%s", resp.status_code, target_url)
            return None

        data = resp.json()
        # oEmbed error response 會是 {"message": "...", "code": 4xx}
        if data.get("code") and int(data.get("code", 0)) >= 400:
            logger.info("tiktok oembed error code=%s url=%s", data.get("code"), target_url)
            return None

        title = (data.get("title") or "").strip()
        author_name = (data.get("author_name") or "").strip()
        author_id = (data.get("author_unique_id") or "").strip()

        # 從 html 欄位抽出背景音樂資訊
        html_field = data.get("html") or ""
        music_match = _TIKTOK_MUSIC_RE.search(html_field)
        music = music_match.group(1).strip() if music_match else ""

        if not title and not author_name:
            logger.info("tiktok oembed empty content url=%s", target_url)
            return None

        lines = [f"（以下是 TikTok 連結 {url} 的影片資訊，透過 oEmbed API 擷取）"]
        lines.append("--- TikTok 影片資訊開始 ---")
        if author_name:
            author_line = f"作者：{author_name}"
            if author_id:
                author_line += f" (@{author_id})"
            lines.append(author_line)
        if title:
            lines.append(f"影片描述：{title}")
        if music:
            lines.append(f"背景音樂：{music}")
        lines.append("--- TikTok 影片資訊結束 ---")

        block = "\n".join(lines)
        logger.info(
            "tiktok oembed OK url=%s author=%s chars=%d",
            url, author_id or author_name, len(block),
        )
        return block
    except Exception as e:
        logger.info("tiktok oembed failed url=%s: %s", url, e)
        return None


def _fetch_youtube_meta(url: str) -> str | None:
    """
    YouTube（含 shorts、youtu.be）走官方 oEmbed API 拿 title + 頻道。免 token、免 auth。

    為什麼要這層：
      - youtube.com/shorts / youtu.be 在 JS 白名單裡（目前 skip，讓 Gemini 處理，但 url_context 對 shorts 吐 metadata 不穩定）
      - youtube.com/watch 走 generic HTML prefetch 只抓到 ~280 chars boilerplate
      - oEmbed endpoint 穩定吐 title + author_name，比前兩條路都好

    限制：oEmbed 不提供 description，想拿內容描述還是只能靠 Gemini；但至少 title 有了。
    """
    try:
        resp = _requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=_PREFETCH_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        if resp.status_code != 200:
            logger.info("youtube oembed HTTP %d url=%s", resp.status_code, url)
            return None

        data = resp.json()
        title = (data.get("title") or "").strip()
        author = (data.get("author_name") or "").strip()
        if not title and not author:
            return None

        lines = [f"（以下是 YouTube 連結 {url} 的影片資訊，透過 oEmbed API 擷取）"]
        lines.append("--- YouTube 影片資訊開始 ---")
        if title:
            lines.append(f"標題：{title}")
        if author:
            lines.append(f"頻道：{author}")
        lines.append("（備註：oEmbed 只給標題與頻道，影片實際內容請自行參考連結）")
        lines.append("--- YouTube 影片資訊結束 ---")
        block = "\n".join(lines)
        logger.info("youtube oembed OK url=%s chars=%d", url, len(block))
        return block
    except Exception as e:
        logger.info("youtube oembed failed url=%s: %s", url, e)
        return None


# Reddit 短網址 pattern：
#   舊 redd.it/xxx（短 domain）
#   新 reddit.com/r/sub/s/xxx（share link）
_REDDIT_SHORT_DOMAIN = re.compile(r"https?://redd\.it/", re.IGNORECASE)
_REDDIT_SHARE_PATH = re.compile(r"reddit\.com/r/[^/]+/s/", re.IGNORECASE)


def _fetch_reddit_meta(url: str) -> str | None:
    """
    Reddit 走公開 .json endpoint 拿 post + 前三條 top comments。免 token，但需 User-Agent。

    為什麼要這層：
      - reddit.com 在 JS 白名單，目前 skip；而 Gemini url_context 對 reddit 常常只拿到 meta tag
      - .json endpoint 是 reddit 官方認可的 public API，吐結構化 JSON（title / selftext / comments）
      - 拿到 selftext + 熱門留言，資訊量遠大於 Gemini 原本拿到的 meta
    """
    try:
        # 短網址 resolve：redd.it/xxx 和 reddit.com/r/.../s/xxx 都要先 follow redirect
        target = url
        if _REDDIT_SHORT_DOMAIN.search(url) or _REDDIT_SHARE_PATH.search(url):
            try:
                r = _requests.head(
                    url, timeout=_PREFETCH_TIMEOUT, allow_redirects=True,
                    headers={"User-Agent": "ptt-line-bot/1.0"},
                )
                target = r.url
                logger.info("reddit short url resolved: %s → %s", url, target)
            except Exception as e:
                logger.info("reddit short url resolve failed url=%s: %s", url, e)
                return None

        # 非貼文 URL（例如 subreddit 首頁、使用者頁面）沒 .json 可抓
        if "/comments/" not in target:
            logger.info("reddit url 非貼文格式 (no /comments/) url=%s", target)
            return None

        # 砍 query/fragment，path 結尾加 .json
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(target)
        json_path = parts.path.rstrip("/") + ".json"
        json_url = urlunsplit((parts.scheme, parts.netloc, json_path, "", ""))

        resp = _requests.get(
            json_url, timeout=_PREFETCH_TIMEOUT,
            headers={"User-Agent": "ptt-line-bot/1.0 (LINE chatbot prefetcher)"},
        )
        if resp.status_code != 200:
            logger.info("reddit .json HTTP %d url=%s", resp.status_code, json_url)
            return None

        data = resp.json()
        # 正常 response：[post_listing, comments_listing]
        if not isinstance(data, list) or len(data) < 1:
            return None

        post_children = data[0].get("data", {}).get("children", [])
        if not post_children:
            return None
        post = post_children[0].get("data", {}) or {}

        title = (post.get("title") or "").strip()
        if not title:
            return None

        selftext = (post.get("selftext") or "").strip()
        subreddit = (post.get("subreddit") or "").strip()
        author = (post.get("author") or "").strip()
        score = post.get("score", 0)
        num_comments = post.get("num_comments", 0)

        # 前三條 top-level 留言（跳過 deleted / removed）
        top_comments = []
        if len(data) > 1:
            for child in data[1].get("data", {}).get("children", []):
                if len(top_comments) >= 3:
                    break
                c = child.get("data", {}) or {}
                body = (c.get("body") or "").strip()
                if not body or body in ("[deleted]", "[removed]"):
                    continue
                if len(body) > 300:
                    body = body[:300] + "…"
                top_comments.append(
                    f"  - u/{c.get('author', '?')} ({c.get('score', 0)} 分): {body}"
                )

        # 內文截斷（避免塞爆 prompt；留空間給 comments）
        if len(selftext) > _PREFETCH_MAX_CHARS - 500:
            selftext = selftext[: _PREFETCH_MAX_CHARS - 500] + "…（內文截斷）"

        lines = [f"（以下是 Reddit 貼文 {url} 的內容，透過 .json endpoint 擷取）"]
        lines.append("--- Reddit 貼文開始 ---")
        lines.append(f"版：r/{subreddit}")
        lines.append(f"作者：u/{author}")
        lines.append(f"標題：{title}")
        lines.append(f"分數：{score} / 留言數：{num_comments}")
        if selftext:
            lines.append(f"內文：\n{selftext}")
        if top_comments:
            lines.append("熱門留言：")
            lines.extend(top_comments)
        lines.append("--- Reddit 貼文結束 ---")

        block = "\n".join(lines)
        logger.info(
            "reddit .json OK url=%s subreddit=%s comments=%d chars=%d",
            url, subreddit, len(top_comments), len(block),
        )
        return block
    except Exception as e:
        logger.info("reddit .json failed url=%s: %s", url, e)
        return None


_IG_REEL_RE = re.compile(r"instagram\.com/(reel|p)/([A-Za-z0-9_-]+)", re.IGNORECASE)


def _fetch_instagram_embed(url: str) -> str | None:
    """
    Instagram Reels / Posts 的 embed 頁面 fallback。

    yt-dlp 對 IG 失敗率高，這層直接抓 /embed/ 公開頁面，
    用 BeautifulSoup 解出 caption（不需要 token / 登入）。
    """
    m = _IG_REEL_RE.search(url)
    if not m:
        return None
    shortcode = m.group(2)
    kind = m.group(1).lower()   # reel 或 p
    embed_url = f"https://www.instagram.com/{kind}/{shortcode}/embed/"
    try:
        resp = _requests.get(
            embed_url,
            timeout=_PREFETCH_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            },
        )
        if resp.status_code != 200:
            logger.info("ig embed HTTP %d url=%s", resp.status_code, embed_url)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # caption 通常在 <div class="Caption"> 或 meta description
        caption = ""
        caption_div = soup.find("div", class_=re.compile(r"Caption", re.I))
        if caption_div:
            caption = caption_div.get_text(separator=" ", strip=True)

        if not caption:
            meta = soup.find("meta", attrs={"name": "description"}) or \
                   soup.find("meta", attrs={"property": "og:description"})
            if meta:
                caption = (meta.get("content") or "").strip()

        if not caption or len(caption) < 10:
            logger.info("ig embed: no caption found url=%s", url)
            return None

        if len(caption) > 800:
            caption = caption[:800] + "…"

        block = (
            f"（以下是 Instagram 連結 {url} 的內容，透過 embed 頁面擷取）\n"
            f"--- Instagram 內容開始 ---\n"
            f"Caption：{caption}\n"
            f"--- Instagram 內容結束 ---"
        )
        logger.info("ig embed OK url=%s chars=%d", url, len(block))
        return block
    except Exception as e:
        logger.info("ig embed failed url=%s: %s", url, e)
        return None


def _prefetch_urls(text: str) -> str:
    """
    從文字中抽出 URL，用 Python requests 預先抓取網頁內容，
    轉成純文字後塞進 prompt。

    特殊平台優先走公開 API（oEmbed / .json），比 HTML prefetch 或 Gemini url_context 穩定：
      - TikTok  → www.tiktok.com/oembed（caption + author + music）
      - YouTube → www.youtube.com/oembed（title + channel）
      - Reddit  → <permalink>.json（title + selftext + top 3 comments）

    其他 JS 渲染網站（IG/threads/FB/X/dcard）仍 skip，交給 Gemini Google Search。
    一般靜態網頁走 HTML prefetch + BeautifulSoup 文字萃取。
    """
    urls = _URL_RE.findall(text)
    if not urls:
        return text

    blocks = []
    for url in urls[:_PREFETCH_MAX_URLS]:
        try:
            u_lower = url.lower()

            # 1) 影片平台：yt-dlp 優先（支援字幕），失敗才 fallback oEmbed
            if "tiktok.com" in u_lower:
                block = _fetch_video_ytdlp(url) or _fetch_tiktok_meta(url)
                if block:
                    blocks.append(block)
                else:
                    logger.info("tiktok: ytdlp + oembed both failed, skip url=%s", url)
                continue
            if "youtube.com" in u_lower or "youtu.be" in u_lower:
                block = _fetch_video_ytdlp(url) or _fetch_youtube_meta(url)
                if block:
                    blocks.append(block)
                else:
                    logger.info("youtube: ytdlp + oembed both failed, skip url=%s", url)
                continue
            if "reddit.com" in u_lower or "redd.it" in u_lower:
                block = _fetch_reddit_meta(url)
                if block:
                    blocks.append(block)
                else:
                    logger.info("reddit .json failed, skip url=%s", url)
                continue

            # 2) Instagram Reels / Posts：yt-dlp → embed 頁面 → Google Search
            if "instagram.com" in u_lower:
                block = _fetch_video_ytdlp(url) or _fetch_instagram_embed(url)
                if block:
                    blocks.append(block)
                else:
                    logger.info("instagram: all methods failed url=%s → Gemini Google Search", url)
                continue

            # 3) 其他 JS 渲染網站（FB / X / Threads / dcard）：試 yt-dlp，失敗才 Google Search
            if _JS_RENDERED_DOMAINS.search(url):
                block = _fetch_video_ytdlp(url)
                if block:
                    blocks.append(block)
                else:
                    logger.info("prefetch skip (JS/CF site, ytdlp failed) url=%s → Gemini Google Search", url)
                continue

            # 一般網頁：直接抓取 HTML
            resp = _requests.get(
                url, timeout=_PREFETCH_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            content = soup.get_text(separator="\n", strip=True)

            # 內容太短 = JS 渲染空殼或 Cloudflare 頁面，不塞垃圾進 prompt
            if len(content) < _PREFETCH_MIN_CHARS:
                logger.info("prefetch skip (too short %d chars) url=%s", len(content), url)
                continue

            if len(content) > _PREFETCH_MAX_CHARS:
                content = content[:_PREFETCH_MAX_CHARS] + "\n…（內容截斷）"
            blocks.append(
                f"（以下是連結 {url} 的網頁內容，已預先擷取）\n"
                f"--- 網頁內容開始 ---\n{content}\n--- 網頁內容結束 ---"
            )
            logger.info("prefetch OK url=%s chars=%d", url, len(content))
        except Exception as e:
            logger.info("prefetch failed url=%s: %s", url, e)

    if blocks:
        return "\n\n".join(blocks) + "\n\n" + text
    return text


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "gemini_model": settings.gemini_model,
        "gemini_light_model": settings.gemini_light_model,
        "group_locked": bool(settings.allowed_group_id),
    }


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.post("/callback")
async def callback(request: Request, x_line_signature: str = Header(None)):
    body = (await request.body()).decode("utf-8")
    print(f"[RAW] sig={x_line_signature} len={len(body)} body={body[:800]}", flush=True)
    try:
        events = _parser.parse(body, x_line_signature)
    except InvalidSignatureError:
        logger.warning("invalid signature")
        raise HTTPException(status_code=400, detail="invalid signature")

    print(f"[PARSED] event_count={len(events)}", flush=True)
    for event in events:
        src = getattr(event, "source", None)
        gid = getattr(src, "group_id", None) if src else None
        print(f"[EVENT] type={type(event).__name__} source={type(src).__name__ if src else None} group_id={gid}", flush=True)
        # 把整個 event 物件 dump 出來看有沒有什麼隱藏欄位
        try:
            print(f"[EVENT_DUMP] {event.model_dump_json()}", flush=True)
        except Exception:
            print(f"[EVENT_DUMP] (could not dump) repr={event!r}", flush=True)
        try:
            _handle_event(event)
        except Exception as e:
            logger.exception("handle_event failed: %s", e)
    return {"ok": True}


def _handle_event(event) -> None:
    # JoinEvent: bot 被加入群組時觸發（之後可能在 1 秒內被踢出）
    if isinstance(event, JoinEvent):
        _handle_join(event)
        return

    # LeaveEvent: bot 被踢出群組時觸發
    if isinstance(event, LeaveEvent):
        _handle_leave(event)
        return

    # MemberJoinedEvent / MemberLeftEvent: 其他成員進出群組
    if isinstance(event, (MemberJoinedEvent, MemberLeftEvent)):
        try:
            print(f"[MEMBER_EVT] {type(event).__name__} {event.model_dump_json()}", flush=True)
        except Exception:
            pass
        return

    # 只處理群組裡的 message
    if not isinstance(event, MessageEvent):
        return
    if not isinstance(event.source, GroupSource):
        return

    # ── 重送事件處理 ──────────────────────────────────────────────────────
    # 隧道斷線期間 LINE 送不進來的訊息,恢復後會帶 is_redelivery=True 重新投遞。
    # 不能無腦跳過 — 查 raw_messages,如果我們從沒收過,就要補處理。
    # reply_token 一定已過期,但 _reply 會 fallback 到 push_message。
    dctx = getattr(event, "delivery_context", None)
    is_redelivery = bool(dctx and getattr(dctx, "is_redelivery", False))
    if is_redelivery:
        msg = getattr(event, "message", None)
        msg_id = getattr(msg, "id", None) if msg else None
        gid = event.source.group_id
        if msg_id and gid:
            existing = memory.get_raw_message(gid, msg_id)
            if existing is not None:
                logger.info("skip truly-duplicate redelivery msg_id=%s", msg_id)
                return
            logger.info(
                "processing missed redelivery msg_id=%s group=%s", msg_id, gid
            )
        else:
            logger.info("skip redelivered event (no msg_id)")
            return

    group_id = event.source.group_id

    # 若設定了 ALLOWED_GROUP_ID，只接受該群組；沒設就開放給所有邀請 bot 進去的群組
    if settings.allowed_group_id and group_id != settings.allowed_group_id:
        logger.info("ignoring message from non-allowed group_id=%s", group_id)
        return

    msg = event.message
    sender_user_id = getattr(event.source, "user_id", None)

    # 文字：先記進 raw_messages（供 quote 回查 / burst look-back / Layer 2 抓 trigger）
    if isinstance(msg, TextMessageContent):
        memory.log_raw_message(group_id, msg.id, sender_user_id, msg.text or "")
        _handle_text_message(event, group_id)
        return

    # 圖片：只記 placeholder，不 OCR 也不回應。
    if isinstance(msg, ImageMessageContent):
        memory.log_raw_message(group_id, msg.id, sender_user_id, "[圖片]")
        return
    if isinstance(msg, VideoMessageContent):
        memory.log_raw_message(group_id, msg.id, sender_user_id, "[影片]")
        return
    if isinstance(msg, AudioMessageContent):
        memory.log_raw_message(group_id, msg.id, sender_user_id, "[音訊]")
        return

    # 檔案：只處理文字檔，其他婉拒（file 很罕見，不是 burst 的一部分）
    if isinstance(msg, FileMessageContent):
        _handle_file_message(event, group_id)
        return



def _handle_text_message(event: MessageEvent, group_id: str) -> None:
    text = event.message.text or ""

    # 回饋收集：20:00 ~ 02:00 TW 窗口內，將文字訊息存入 pending_feedback.json
    if feedback_collector.in_feedback_window():
        sender = getattr(event.source, "user_id", None) or "unknown"
        try:
            feedback_collector.collect_message(sender, text)
        except Exception as e:
            logger.warning("[Feedback] collect_message failed: %s", e)

    # 1. 指令處理（指令不需要 @mention 也能用，方便管理）
    cmd_reply = _handle_command(group_id, text)
    if cmd_reply is not None:
        # 指令是 explicit 操作 → 取消任何待處理的 burst
        burst_filter.cancel_burst(group_id)
        _reply(event.reply_token, cmd_reply, group_id=group_id)
        return

    # 2. 晚餐推薦觸發
    if _is_dinner_question(text):
        burst_filter.cancel_burst(group_id)
        _handle_dinner_recommendation(event, group_id)
        return

    # 3. Explicit 觸發（@mention / /ai / /問 ...）→ 立刻處理，並取消 pending burst
    clean_text = _extract_gemini_trigger(text, event.message)
    if clean_text is not None:
        burst_filter.cancel_burst(group_id)
        _handle_explicit_text(event, group_id, clean_text)
        return

    # 3. 任何文字訊息都直接回應
    _handle_explicit_text(event, group_id, text)


def _handle_explicit_text(
    event: MessageEvent, group_id: str, clean_text: str
) -> None:
    """使用者明確叫 bot（@mention / /ai 等），立刻丟 Gemini 回覆。"""
    if not clean_text:
        _reply(event.reply_token, "嗯？\n怎麼了嗎\n要找我什麼啦", group_id=group_id)
        return

    # 若引用了媒體訊息（圖片 / 影片 / 音訊），走 multimodal 路徑
    quoted_id = getattr(event.message, "quoted_message_id", None)
    if quoted_id:
        raw = memory.get_raw_message(group_id, quoted_id)
        if raw is not None and raw[1] in _MEDIA_PLACEHOLDERS:
            _handle_media_via_quote(event, group_id, clean_text, quoted_id, raw[1])
            return

    # 短路：cache 已知 quota 爆 → 靜默跳過
    if _quota_exhausted():
        logger.info("explicit reply skipped Gemini (cached quota exhausted)")
        return

    # 純文字 + 可能的文字引用
    quoted_block = _build_quoted_block(event.message, group_id)
    user_input = clean_text if not quoted_block else f"{quoted_block}\n\n{clean_text}"

    # URL 預抓取：先用 Python 抓網頁內容塞進 prompt，繞過 Gemini url_context 的限制
    user_input = _prefetch_urls(user_input)

    context = memory.get_context(group_id)
    facts = memory.top_facts(group_id)
    pnotes = _get_persona_notes(group_id)
    try:
        reply_text = gemini_client.chat(user_input, context, facts, pnotes)
    except Exception as e:
        if _is_quota_error(e):
            _mark_quota_exhausted()
            logger.warning("gemini chat (explicit) quota exhausted")
        else:
            logger.exception("gemini chat (explicit) failed: %s", e)
            _reply(event.reply_token, _friendly_gemini_error(e), group_id=group_id)
        return

    # 即時糾正偵測：使用者如果在糾正 bot，自動記住
    _try_save_correction(group_id, clean_text)

    memory.append_turn(group_id, "user", user_input)
    memory.append_turn(group_id, "bot", reply_text)
    _maybe_extract_facts(group_id)
    _reply(event.reply_token, reply_text, group_id=group_id)


def _handle_burst_flush(
    group_id: str, combined_text: str, reply_token: str
) -> None:
    """burst_filter 判定「值得主動回應」時觸發。跑在 Timer 的 thread 裡。

    規則:「會回應的情境」不能靜默 — 要不是真回應，要不就回 quota 訊息。
    """
    logger.info(
        "burst flush triggered group=%s text_len=%d",
        group_id, len(combined_text),
    )

    # 短路 1：cache 已知 quota 爆 → 靜默跳過
    if _quota_exhausted():
        logger.info("burst flush skipped Gemini (cached quota exhausted)")
        return

    context = memory.get_context(group_id)
    facts = memory.top_facts(group_id)
    pnotes = _get_persona_notes(group_id)

    # URL 預抓取：先用 Python 抓網頁內容塞進 prompt，繞過 Gemini url_context 的限制
    prefetched = _prefetch_urls(combined_text)

    user_input = (
        "(下面是群組裡最近累積的訊息，已經被過濾器判定值得主動回應。"
        "請根據系統指令中的規則，針對其中有查證價值或爭議點的部份做一次"
        "精簡的回應；若只是閒聊請用一句話帶過。)\n\n"
        f"{prefetched}"
    )
    try:
        reply_text = gemini_client.chat(user_input, context, facts, pnotes)
    except Exception as e:
        if _is_quota_error(e):
            _mark_quota_exhausted()
            logger.warning("gemini chat (burst) quota exhausted")
        else:
            logger.exception("gemini chat (burst) failed: %s", e)
            _reply(reply_token, "Gemini 那邊好像塞車了，等一下再回你～", group_id=group_id)
        return

    logger.info("burst gemini reply len=%d text=%s", len(reply_text) if reply_text else 0, repr(reply_text[:200]) if reply_text else "(empty)")
    if not reply_text or not reply_text.strip():
        logger.warning("burst gemini returned empty reply, skipping LINE send")
        return

    memory.append_turn(group_id, "user", f"[burst]\n{combined_text}")
    memory.append_turn(group_id, "bot", reply_text)
    _maybe_extract_facts(group_id)
    _reply(reply_token, reply_text, group_id=group_id)


# 在 module load 時把 callback 注入 burst_filter
burst_filter.register_on_flush(_handle_burst_flush)


# ── 媒體 quote 觸發（唯一會分析圖片/影片/音訊的路徑）──────────────────────

# placeholder → (mime_type, 中文名)；dispatch 時寫入 raw_messages，explicit
# 路徑遇到對應 quote 時用這張表查 mime 再重新下載。
_MEDIA_PLACEHOLDERS: dict[str, tuple[str, str]] = {
    "[圖片]": ("image/jpeg", "圖片"),
    "[影片]": ("video/mp4", "影片"),
    "[音訊]": ("audio/m4a", "音訊"),
}

# 單次可下載的媒體上限（LINE 上傳原本就有限制，這裡做二層保護）
_MEDIA_BYTE_LIMIT = 20 * 1024 * 1024  # 20 MB


def _handle_media_via_quote(
    event: MessageEvent,
    group_id: str,
    clean_text: str,
    quoted_message_id: str,
    placeholder: str,
) -> None:
    """使用者 @AI 並引用了一則圖片/影片/音訊 → 下載 bytes 丟 Gemini multimodal。

    LINE 的 message content 通常會保留 7 天，期限內都能重新下載。
    """
    mime_type, media_name = _MEDIA_PLACEHOLDERS[placeholder]

    # 短路：cache 已知 quota 爆 → 靜默跳過
    if _quota_exhausted():
        logger.info("media quote skipped Gemini (cached quota exhausted)")
        return

    try:
        data = _download_content(quoted_message_id)
    except Exception as e:
        logger.warning("download quoted media failed: %s", e)
        _reply(
            event.reply_token,
            f"這則{media_name}下載不到（LINE 最多保留 7 天，可能已經過期）。"
            "要分析的話請重新貼一次。",
            group_id=group_id,
        )
        return

    if len(data) > _MEDIA_BYTE_LIMIT:
        _reply(
            event.reply_token,
            f"這則{media_name}太大（{len(data) / 1024 / 1024:.1f} MB），"
            f"超過 {_MEDIA_BYTE_LIMIT // 1024 // 1024} MB 上限，沒辦法分析。",
            group_id=group_id,
        )
        return

    prompt_text = clean_text or f"請分析這則{media_name}的內容並回應。"
    parts = [
        types.Part.from_bytes(data=bytes(data), mime_type=mime_type),
        f"(使用者引用了一則{media_name}向你提問)\n\n{prompt_text}",
    ]

    context = memory.get_context(group_id)
    facts = memory.top_facts(group_id)
    pnotes = _get_persona_notes(group_id)
    try:
        reply_text = gemini_client.chat(parts, context, facts, pnotes)
    except Exception as e:
        if _is_quota_error(e):
            _mark_quota_exhausted()
            logger.warning("gemini chat (quoted-%s) quota exhausted", media_name)
        else:
            logger.exception("gemini chat (quoted-%s) failed: %s", media_name, e)
            _reply(event.reply_token, _friendly_gemini_error(e), group_id=group_id)
        return

    memory.append_turn(group_id, "user", f"[{media_name} + 問題]\n{prompt_text}")
    memory.append_turn(group_id, "bot", reply_text)
    _maybe_extract_facts(group_id)
    _reply(event.reply_token, reply_text, group_id=group_id)


def _build_quoted_block(message: TextMessageContent, group_id: str) -> str | None:
    """如果訊息有引用原始訊息，回傳「原始訊息」block；否則回 None。"""
    quoted_id = getattr(message, "quoted_message_id", None)
    if not quoted_id:
        return None
    raw = memory.get_raw_message(group_id, quoted_id)
    if raw is not None:
        sender_user_id, original_text = raw
        sender_name = _get_member_display_name(group_id, sender_user_id)
        return (
            "(使用者引用了下面這則原始訊息向你提問)\n"
            f"--- 原始訊息 開始 ---\n"
            f"[{sender_name}]: {original_text}\n"
            f"--- 原始訊息 結束 ---"
        )

    # 找不到精確的那則 → 撈最近對話當上下文，讓 Gemini 自己判斷被引用的是哪一則
    recent = memory.get_recent_raw_messages(group_id, limit=20)
    if not recent:
        return (
            "(使用者引用了群組裡的一則訊息,但原文不在記憶中，"
            "也沒有近期對話紀錄。請根據使用者自己寫的文字盡力回應。)"
        )
    lines = []
    for _mid, uid, text, _ts in recent:
        name = _get_member_display_name(group_id, uid)
        lines.append(f"[{name}]: {text}")
    ctx_block = "\n".join(lines)
    return (
        "(使用者引用了群組裡的一則訊息,但該則原文不在記憶中。\n"
        "以下是群組最近的對話紀錄,請從中推斷使用者引用的是哪一則,\n"
        "並據此回應。不要跟使用者說你找不到原文。)\n"
        f"--- 最近對話 開始 ---\n{ctx_block}\n--- 最近對話 結束 ---"
    )


def _get_member_display_name(group_id: str, user_id: str | None) -> str:
    """查群組成員的顯示名稱;失敗就用 fallback。"""
    if user_id is None:
        return "某人"
    if user_id == "__bot__":
        return "我 (bot)"
    try:
        with ApiClient(_line_config) as api_client:
            profile = MessagingApi(api_client).get_group_member_profile(group_id, user_id)
            return getattr(profile, "display_name", None) or "群組成員"
    except Exception as e:
        logger.debug("get_group_member_profile failed: %s", e)
        return "群組成員"


# 文字類 mime 白名單 — 這些 decode 成字串丟給 Gemini
_TEXT_LIKE_MIMES = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/x-sh",
    "application/x-python",
    "application/x-python-code",
}
# 80k 字中文 ≈ 100〜120k tokens，留 buffer 給 system prompt + context
_TEXT_CHAR_LIMIT = 80_000


def _handle_file_message(event: MessageEvent, group_id: str) -> None:
    """檔案訊息 — 只處理文字類 (txt/md/json/csv/code)，binary 檔案婉拒。"""
    msg = event.message
    file_name = getattr(msg, "file_name", "") or "unknown"
    mime_type = _guess_mime_type(file_name)
    is_text_like = mime_type.startswith("text/") or mime_type in _TEXT_LIKE_MIMES

    if not is_text_like:
        _reply(
            event.reply_token,
            f"這個檔案 ({file_name}) 不是文字檔,目前不處理 binary/PDF/壓縮檔。\n"
            f"如果是文章,請直接貼「網址」或「內容文字」給我。",
            group_id=group_id,
        )
        return

    # 短路：cache 已知 quota 爆 → 靜默跳過
    if _quota_exhausted():
        logger.info("file handler skipped Gemini (cached quota exhausted)")
        return

    # 下載 + decode + 截斷
    try:
        data = _download_content(msg.id)
    except Exception as e:
        logger.exception("download file failed: %s", e)
        _reply(event.reply_token, f"下載檔案失敗:{type(e).__name__}", group_id=group_id)
        return

    try:
        content = data.decode("utf-8", errors="replace")
    except Exception as e:
        logger.exception("decode file failed: %s", e)
        _reply(event.reply_token, f"這個檔案沒辦法用 UTF-8 讀,可能是 binary。", group_id=group_id)
        return

    original_len = len(content)
    note = ""
    if original_len > _TEXT_CHAR_LIMIT:
        content = content[:_TEXT_CHAR_LIMIT]
        note = (
            f"\n\n(注意:原始檔案共 {original_len:,} 字,"
            f"我只看了前 {_TEXT_CHAR_LIMIT:,} 字,要看完整內容請把檔案切小一點。)"
        )

    prompt_text = (
        f"(使用者丟了一個文字檔:{file_name}){note}\n\n"
        f"--- 檔案內容開始 ---\n{content}\n--- 檔案內容結束 ---\n\n"
        f"請分析這個檔案的內容並回應。"
    )

    context = memory.get_context(group_id)
    facts = memory.top_facts(group_id)
    pnotes = _get_persona_notes(group_id)
    try:
        reply_text = gemini_client.chat(prompt_text, context, facts, pnotes)
    except Exception as e:
        if _is_quota_error(e):
            _mark_quota_exhausted()
            logger.warning("gemini chat (file) quota exhausted")
        else:
            logger.exception("gemini chat (file) failed: %s", e)
        _reply(event.reply_token, _friendly_gemini_error(e, file_name), group_id=group_id)
        return

    memory.append_turn(group_id, "user", f"[file: {file_name}]")
    memory.append_turn(group_id, "bot", reply_text)

    _maybe_extract_facts(group_id)

    _reply(event.reply_token, reply_text, group_id=group_id)



_PT_TZ = ZoneInfo("America/Los_Angeles")
_TW_TZ = ZoneInfo("Asia/Taipei")


def _next_gemini_reset_tw() -> tuple[str, str]:
    """算下一次 Gemini free-tier quota 重置的台灣時間。

    Gemini 免費層每天 00:00 PT 重置。DST 期間台灣 = 15:00，非 DST = 16:00。
    回傳 (絕對時間字串, 相對倒數字串)，例如 ("今天 15:00", "還有 6 小時 24 分鐘")。
    """
    now_pt = datetime.now(tz=_PT_TZ)
    next_midnight_pt = (now_pt + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    reset_tw = next_midnight_pt.astimezone(_TW_TZ)
    now_tw = datetime.now(tz=_TW_TZ)

    today_tw = now_tw.date()
    if reset_tw.date() == today_tw:
        prefix = "今天"
    elif reset_tw.date() == today_tw + timedelta(days=1):
        prefix = "明天"
    else:
        prefix = reset_tw.strftime("%m/%d")
    abs_str = f"{prefix} {reset_tw.strftime('%H:%M')}"

    delta = reset_tw - now_tw
    total_min = max(0, int(delta.total_seconds() // 60))
    hours, mins = divmod(total_min, 60)
    if hours > 0:
        rel_str = f"還有 {hours} 小時 {mins} 分鐘"
    else:
        rel_str = f"還有 {mins} 分鐘"
    return abs_str, rel_str


# ── Gemini quota cache ────────────────────────────────────────────────────────
# 第一次遇到 429 PerDay 就記住「今天都是爆的」，之後所有 handler 在打 Gemini 之前
# 先看這個 cache，直接短路回 quota 訊息，不浪費網路 round-trip。
# 下一個 00:00 PT（= 台灣 15:00 夏令 / 16:00 非夏令）自動失效。
_quota_exhausted_until_ts: float = 0.0


def _mark_quota_exhausted() -> None:
    """記錄 Gemini quota 已爆,到下一個 00:00 PT 前都不要再打了。"""
    global _quota_exhausted_until_ts
    now_pt = datetime.now(tz=_PT_TZ)
    next_midnight_pt = (now_pt + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    _quota_exhausted_until_ts = next_midnight_pt.timestamp()
    logger.warning(
        "gemini quota marked exhausted until %s",
        next_midnight_pt.astimezone(_TW_TZ).strftime("%Y-%m-%d %H:%M TW"),
    )


def _quota_exhausted() -> bool:
    """cache 還沒到期就回 True,handler 要直接短路。"""
    return time.time() < _quota_exhausted_until_ts


def _quota_exhausted_message() -> str:
    """quota 爆時統一的使用者訊息(含動態台灣重置時間)。"""
    abs_str, rel_str = _next_gemini_reset_tw()
    return (
        f"Gemini 免費層今日請求額度已用完 (flash 每天 20 次)。\n"
        f"可以再使用的時間:{abs_str}(台灣時間,{rel_str})\n"
        f"想馬上恢復 → https://aistudio.google.com 綁卡開 pay-as-you-go"
    )


def _get_persona_notes(group_id: str) -> list[dict]:
    """取出 persona notes，供 gemini_client.chat() 注入 system prompt。"""
    return memory.list_persona_notes(group_id)


# 糾正偵測：使用者 @mention bot 時如果內容像糾正，自動存下來
_CORRECTION_KEYWORDS = (
    "不要", "不准", "別再", "下次", "記住", "以後",
    "不可以", "禁止", "不能", "改掉", "不用",
)


def _try_save_correction(group_id: str, user_text: str) -> None:
    """如果 user_text 看起來像是在糾正 bot 的行為，存成 persona correction。"""
    t = user_text.strip()
    if len(t) < 4 or len(t) > 100:
        return
    if any(kw in t for kw in _CORRECTION_KEYWORDS):
        memory.add_persona_note(group_id, "correction", "使用者糾正", t)
        logger.info("persona correction saved: %s", t[:60])


def _is_quota_error(e: Exception) -> bool:
    """判斷是不是 Gemini 日額度爆的 429。"""
    s = str(e)
    return ("429" in s or "RESOURCE_EXHAUSTED" in s) and (
        "PerDay" in s or "free_tier_requests" in s
    )


def _friendly_gemini_error(e: Exception, file_name: str | None = None) -> str:
    """把 google-genai SDK 的錯誤翻成使用者友善訊息。"""
    err_str = str(e)
    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
        # 日額度爆 (RequestsPerDay)
        if "PerDay" in err_str or "free_tier_requests" in err_str:
            return _quota_exhausted_message()
        # 分鐘 token 爆 (PerMinute, 通常是單次輸入太大)
        if "input_token" in err_str or "free_tier_input_token_count" in err_str:
            if file_name:
                return (
                    f"這個檔案 ({file_name}) 太大,一次塞爆了 Gemini 免費層的"
                    f"每分鐘 token 限制 (250k tokens/min)。\n"
                    f"建議:把檔案切小一點,或直接貼一段文字給我。"
                )
            return (
                "剛才請求太密或輸入太長,超過 Gemini 免費層的 token 限制 (250k/min)。"
                "等個一分鐘再試。"
            )
        return "Gemini quota 爆了,等一下再試。"
    if "401" in err_str or "403" in err_str:
        return "Gemini API key 有問題,請檢查設定。"
    if "400" in err_str:
        return f"Gemini 說這個輸入有問題:{err_str[:200]}"
    if "500" in err_str or "503" in err_str or "UNAVAILABLE" in err_str:
        return "Gemini 那邊暫時出問題,等一下再試。"
    return f"分析失敗:{type(e).__name__}"


def _maybe_extract_facts(group_id: str) -> None:
    """每 N 輪抽一次長期事實。"""
    if not memory.bump_and_should_extract(group_id):
        return
    new_facts = gemini_client.extract_facts(memory.get_context(group_id))
    added = 0
    for f in new_facts:
        if memory.add_fact(group_id, f):
            added += 1
    logger.info("auto-extracted facts: %d new (total=%d)", added, len(memory.list_facts(group_id)))


# ── Join / Leave 處理 ────────────────────────────────────────────────────────

def _handle_join(event: JoinEvent) -> None:
    """Bot 被加入群組時觸發。立即 reply + 查群組資訊。
    目前問題：LINE 會在 < 1 秒內把 bot 踢出，所以要搶時間收集資訊。"""
    src = event.source
    group_id = getattr(src, "group_id", None)
    room_id = getattr(src, "room_id", None)
    target_id = group_id or room_id
    source_type = "group" if group_id else ("room" if room_id else "unknown")
    print(f"[JOIN] source_type={source_type} id={target_id} reply_token={event.reply_token}", flush=True)

    # 1. 立即 reply 一則 welcome 訊息（搶在被踢之前）
    try:
        _reply(event.reply_token, f"我被加入了！{source_type}_id={target_id}")
        print(f"[JOIN] reply sent", flush=True)
    except Exception as e:
        print(f"[JOIN] reply FAILED: {e}", flush=True)

    # 2. 立即查群組 summary / member count / member ids（可能因未認證而失敗，但試試）
    if group_id:
        try:
            with ApiClient(_line_config) as api_client:
                api = MessagingApi(api_client)
                try:
                    summary = api.get_group_summary(group_id)
                    print(f"[JOIN] group_summary={summary}", flush=True)
                except Exception as e:
                    print(f"[JOIN] group_summary FAILED: {type(e).__name__}: {str(e)[:200]}", flush=True)
                try:
                    count = api.get_group_member_count(group_id)
                    print(f"[JOIN] group_member_count={count}", flush=True)
                except Exception as e:
                    print(f"[JOIN] group_member_count FAILED: {type(e).__name__}: {str(e)[:200]}", flush=True)
                try:
                    ids = api.get_group_members_ids(group_id)
                    print(f"[JOIN] group_members_ids={ids}", flush=True)
                except Exception as e:
                    print(f"[JOIN] group_members_ids FAILED: {type(e).__name__}: {str(e)[:200]}", flush=True)
        except Exception as e:
            print(f"[JOIN] api_client FAILED: {e}", flush=True)


def _handle_leave(event: LeaveEvent) -> None:
    """Bot 被踢出群組時觸發。記錄被踢的時間點以便分析。"""
    src = event.source
    group_id = getattr(src, "group_id", None)
    room_id = getattr(src, "room_id", None)
    target_id = group_id or room_id
    print(f"[LEAVE] id={target_id} timestamp={event.timestamp}", flush=True)


# ── Command 處理 ──────────────────────────────────────────────────────────────

_DINNER_KEYWORDS = ["晚餐吃什麼", "晚餐吃哪", "吃什麼晚餐", "晚餐去哪", "晚餐要吃什麼", "今晚吃什麼", "今天吃什麼"]

def _is_dinner_question(text: str) -> bool:
    return any(kw in text for kw in _DINNER_KEYWORDS)


_DINNER_PROMPT = """你是台北美食達人，以善導寺捷運站（台北市中正區）為中心，推薦附近步行可達的晚餐餐廳。

以下餐廳請勿推薦：喜來登、阜杭豆漿、雙月食品社。

請推薦 4～5 間，盡量多樣（台菜、日式、韓式、異國料理、麵食等皆可），格式如下（用換行分隔每間）：
🍽 餐廳名稱
📍 地址（簡短）
🍴 料理類型 ＋ 招牌菜或特色一句話
💰 價位（每人約 NT$XXX）

回覆風格：親切自然，像朋友推薦，繁體中文，不要加多餘的前言或結語。"""


def _handle_dinner_recommendation(event: MessageEvent, group_id: str) -> None:
    if _quota_exhausted():
        logger.info("dinner recommendation skipped (cached quota exhausted)")
        return
    context = memory.get_context(group_id)
    facts = memory.top_facts(group_id)
    pnotes = _get_persona_notes(group_id)
    try:
        reply_text = gemini_client.chat(_DINNER_PROMPT, context, facts, pnotes)
    except Exception as e:
        if _is_quota_error(e):
            _mark_quota_exhausted()
            logger.warning("dinner recommendation quota exhausted")
        else:
            logger.exception("dinner recommendation failed: %s", e)
            _reply(event.reply_token, _friendly_gemini_error(e), group_id=group_id)
        return
    _reply(event.reply_token, reply_text, group_id=group_id)


def _handle_command(group_id: str, text: str) -> str | None:
    """有對應到指令回 str；沒有回 None。"""
    t = text.strip()
    if t == "/group_id":
        return f"本群 group_id：\n{group_id}"

    if t == "/help" or t == "/指令":
        return _HELP_TEXT

    # ── 長期記憶（facts）──────────────────────────────────────────
    if t == "/看記憶":
        facts = memory.list_facts(group_id)
        if not facts:
            return "目前沒有任何記憶。要讓我記住什麼，用：\n/記住 <內容>"
        return "目前的記憶：\n" + "\n".join(f"• {f}" for f in facts)

    if t.startswith("/記住 "):
        fact = t[len("/記住 "):].strip()
        if not fact:
            return "用法：/記住 <要記住的內容>"
        if memory.add_fact(group_id, fact):
            return f"好，記住了：{fact}"
        return f"這條已經在記憶裡了：{fact}"

    if t.startswith("/忘記 "):
        keyword = t[len("/忘記 "):].strip()
        if not keyword:
            return "用法：/忘記 <關鍵字>"
        n = memory.remove_fact(group_id, keyword)
        return f"刪除了 {n} 條含「{keyword}」的記憶。" if n else f"沒有找到含「{keyword}」的記憶。"

    if t == "/清除記憶":
        n = memory.clear_facts(group_id)
        return f"已清除 {n} 條記憶。"

    # ── Layer 1：使用者手動管理過濾規則 ──────────────────────────────
    if t.startswith("/不要回 "):
        pattern = t[len("/不要回 "):].strip()
        if not pattern:
            return "用法：/不要回 <這類訊息的特徵，例如「早安」「中午吃什麼」>"
        rid = memory.add_filter_rule(group_id, "skip", pattern, source="user")
        return f"好，以後訊息裡有「{pattern}」就不主動回。(規則 #{rid})"

    if t.startswith("/以後要查 "):
        pattern = t[len("/以後要查 "):].strip()
        if not pattern:
            return "用法：/以後要查 <這類訊息的特徵，例如「某醫師說」「疫苗」>"
        rid = memory.add_filter_rule(group_id, "must_answer", pattern, source="user")
        return f"好，以後訊息裡有「{pattern}」就會主動查證。(規則 #{rid})"

    if t == "/規則":
        rules = memory.list_filter_rules(group_id)
        if not rules:
            return (
                "目前沒有過濾規則。\n"
                "新增：/不要回 <特徵>  或  /以後要查 <特徵>"
            )
        lines = ["目前的過濾規則："]
        for r in rules:
            tag = "不要回" if r["kind"] == "skip" else "要查"
            src = "手動" if r["source"] == "user" else "自動學"
            lines.append(f"#{r['rule_id']} [{tag}]({src}) {r['pattern']}")
        return "\n".join(lines)

    if t.startswith("/刪除規則 "):
        raw = t[len("/刪除規則 "):].strip()
        try:
            rid = int(raw)
        except ValueError:
            return "用法：/刪除規則 <數字編號>（用 /規則 看編號）"
        if memory.delete_filter_rule(group_id, rid):
            return f"已刪除規則 #{rid}"
        return f"找不到規則 #{rid}"

    if t == "/清除規則":
        n = memory.clear_filter_rules(group_id)
        return f"已清除 {n} 條過濾規則"

    # ── Layer 3：週期性自我檢討 ──────────────────────────────────────
    if t == "/檢討" or t == "/檢討 7":
        report, _ = review.run_weekly_review(group_id, days=7)
        return report

    if t.startswith("/檢討 "):
        raw = t[len("/檢討 "):].strip()
        try:
            days = int(raw)
        except ValueError:
            return "用法：/檢討 <天數>  例如：/檢討 14"
        if days <= 0 or days > 30:
            return "天數請在 1~30 之間。"
        report, _ = review.run_weekly_review(group_id, days=days)
        return report

    if t == "/採用":
        drafts = memory.list_rule_drafts(group_id)
        if not drafts:
            return "目前沒有待採用的建議。先跑 /檢討 產生一份。"
        lines = ["目前的建議："]
        for d in drafts:
            tag = "不要回" if d["kind"] == "skip" else "要回"
            lines.append(f"{d['draft_id']}. [{tag}] {d['pattern']}")
            if d.get("reason"):
                lines.append(f"   理由：{d['reason']}")
        lines.append("")
        lines.append("用法：/採用 1 2  或  /採用 全部  或  /採用 無")
        return "\n".join(lines)

    if t.startswith("/採用 "):
        spec = t[len("/採用 "):].strip()
        _, msg = review.adopt_drafts(group_id, spec)
        return msg

    # ── Layer 2：糾正剛剛的 bot 回覆 → 自動抽象成規則 ────────────────
    if t.startswith("/閉嘴"):
        reason = t[len("/閉嘴"):].lstrip()
        if not reason:
            return (
                "用法：/閉嘴 <為什麼不應該回>\n"
                "例：/閉嘴 這種只是早安問候，不用回"
            )
        return _handle_layer2_correction(group_id, reason)

    return None


_HELP_TEXT = (
    "可用指令：\n"
    "【記憶】\n"
    "  /看記憶                 看長期事實\n"
    "  /記住 <內容>            手動加一條事實\n"
    "  /忘記 <關鍵字>          刪除含關鍵字的事實\n"
    "  /清除記憶               全砍\n"
    "【主動過濾】\n"
    "  /規則                   看過濾規則\n"
    "  /不要回 <特徵>          以後訊息裡有這個就不回\n"
    "  /以後要查 <特徵>        以後訊息裡有這個就主動查證\n"
    "  /刪除規則 <編號>        刪掉特定規則\n"
    "  /清除規則               全砍\n"
    "  /閉嘴 <理由>            針對剛剛那則 bot 回覆糾正,我會自動學一條規則\n"
    "【週期性自我檢討】\n"
    "  /檢討                   立刻跑一次過去 7 天的檢討(可接天數)\n"
    "  /採用                   列出待採用的建議\n"
    "  /採用 1 2 / 全部 / 無   把建議升級成正式規則\n"
    "【其他】\n"
    "  /group_id               顯示本群 ID\n"
    "  /help                   看這張說明"
)


def _handle_layer2_correction(group_id: str, reason: str) -> str:
    """使用者覺得剛剛 bot 回覆不該出現 → 呼叫 Gemini 抽象一條 skip 規則。"""
    last = memory.get_last_bot_reply(group_id)
    if last is None:
        return "找不到最近的 bot 回覆可以糾正。"
    _, bot_reply = last
    trigger_text = _guess_last_trigger_text(group_id)

    pattern = gemini_client.generate_filter_rule(
        bot_reply=bot_reply,
        user_reason=reason,
        trigger_text=trigger_text,
    )
    if not pattern:
        return (
            "自動生成規則失敗，請改用 /不要回 <特徵> 手動加。\n"
            f"(你剛才說：{reason[:80]})"
        )
    rid = memory.add_filter_rule(group_id, "skip", pattern, source="learned")
    return (
        f"了解。我從這次糾正學到一條規則：\n"
        f"#{rid} [不要回] {pattern}\n"
        f"以後類似訊息就不會主動回了。覺得不對請用 /刪除規則 {rid}"
    )


def _guess_last_trigger_text(group_id: str) -> str:
    """找出最近一次 bot 回覆前，它看到的 user 訊息（當作 trigger 傳給規則產生器）。"""
    recent = memory.get_recent_raw_messages(group_id, limit=20)  # 舊→新
    last_bot_idx = None
    for i in range(len(recent) - 1, -1, -1):
        if recent[i][1] == "__bot__":
            last_bot_idx = i
            break
    if last_bot_idx is None:
        return ""
    before = [
        recent[i][2]
        for i in range(last_bot_idx)
        if recent[i][1] != "__bot__"
    ]
    return "\n".join(before[-5:])


# ── LINE SDK helpers ──────────────────────────────────────────────────────────

def _is_mentioned(message: TextMessageContent) -> bool:
    """檢查這則訊息是否 @mention 了本 bot。
    LINE 的 mention 結構：message.mention.mentionees[i].is_self == True 代表 mention 到我。"""
    mention = getattr(message, "mention", None)
    if mention is None:
        return False
    mentionees = getattr(mention, "mentionees", None) or []
    for m in mentionees:
        if getattr(m, "is_self", False):
            return True
    return False


# 桌機 LINE 打不到 @bot 的後備觸發前綴（全大小寫都接受）
_ASK_PREFIXES = ("/ai ", "/ai", "/問 ", "/問", "/ask ", "/ask", "/AI ", "/AI")

# 桌機 LINE 有時候 @mention 不帶 mention 結構，只是純文字 @名稱。
# 列出 bot 名稱 + 通用 @AI 當 fallback。
# 桌機 LINE 會打出全形 ＠（U+FF20），所以半形全形都要接。
_TEXT_MENTION_PREFIXES = (
    "@咪寶 ", "@咪寶", "＠咪寶 ", "＠咪寶",
    "@ai ", "@ai", "＠ai ", "＠ai",
    "@AI ", "@AI", "＠AI ", "＠AI",
)

# 直接叫名字也算觸發（長輩不用 @，直接說「咪寶...」）
_BOT_NAME_KEYWORDS = ("咪寶",)


def _extract_gemini_trigger(text: str, message: TextMessageContent) -> str | None:
    """判斷這則訊息是否要丟給 Gemini；若是，回傳乾淨的問題文字。

    四種觸發方式（回 None 代表無視）：
    1. 手機 LINE：@ 本 bot，會有 mention 結構 → 去掉 mention 後剩下的字
    2. /ai、/問、/ask 前綴 → 去掉前綴後剩下的字
    3. 桌機 LINE fallback：純文字 @AI 開頭（沒有 mention 結構）→ 去掉前綴
    4. 訊息裡出現 bot 名字（咪寶）→ 去掉名字後剩下的字
    """
    t = text.strip()
    for prefix in _ASK_PREFIXES:
        if t == prefix.strip():
            return ""
        if t.startswith(prefix):
            return t[len(prefix):].strip()
    if _is_mentioned(message):
        return _strip_mentions(message).strip()
    # fallback：桌機 LINE @mention 不帶結構，只有純文字 @AI
    for prefix in _TEXT_MENTION_PREFIXES:
        if t == prefix.strip():
            return ""
        if t.lower().startswith(prefix.lower()):
            return t[len(prefix):].strip()
    # 名字偵測：訊息裡出現 bot 名字就觸發，把名字挖掉後剩下的當問題
    for name in _BOT_NAME_KEYWORDS:
        if name in t:
            clean = t.replace(name, "", 1).strip("，,、。！!？? \t")
            return clean
    return None


def _strip_mentions(message: TextMessageContent) -> str:
    """把訊息裡所有 @mention 的子字串挖掉，只留真正的問題。"""
    text = message.text or ""
    mention = getattr(message, "mention", None)
    if mention is None:
        return text
    mentionees = getattr(mention, "mentionees", None) or []
    # 從後往前刪，避免 index 位移
    ranges = sorted(
        [(m.index, m.index + m.length) for m in mentionees],
        key=lambda x: x[0],
        reverse=True,
    )
    for start, end in ranges:
        text = text[:start] + text[end:]
    return text


def _reply(reply_token: str, text: str, group_id: str | None = None) -> None:
    """
    回覆 LINE 訊息。若帶 group_id,成功後會把 bot 的回覆也存進 raw_messages,
    這樣使用者引用 bot 的回覆問後續問題時,能查得到原文。

    若 reply_token 已過期（例如 redelivery）且有 group_id,
    自動 fallback 到 push_message 補送。

    settings.bot_muted=True 時整個函式 short-circuit:
    不 reply、不 push、只把原本要送的 text 寫進 log 方便除錯。
    """
    # LINE 單則訊息上限 5000 字；在截斷前先預留 footer 空間
    footer = _get_quota_footer()
    text = text[:4900 - len(footer)] + footer

    # ── Mute 守門 ─────────────────────────────────────────────────────────────
    # 修 bug 期間預設靜音。webhook 照收、classifier/chat 照跑、log 照寫，只是不送 LINE。
    if settings.bot_muted:
        logger.info("[MUTED] would_reply group=%s len=%d preview=%r",
                    group_id, len(text), text[:120])
        return

    resp = None
    try:
        with ApiClient(_line_config) as api_client:
            resp = MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=text)],
                )
            )
    except Exception as e:
        # reply_token 過期 / 用過 / 重送事件 → fallback 到 push_message
        logger.warning("reply failed: %s", str(e)[:300])
        if group_id:
            try:
                with ApiClient(_line_config) as api_client:
                    MessagingApi(api_client).push_message(
                        PushMessageRequest(
                            to=group_id,
                            messages=[TextMessage(text=text)],
                        )
                    )
                logger.info("fallback push_message sent to group=%s", group_id)
                # push 成功也要記 bot 回覆（但拿不到 sent_message_id）
                memory.log_raw_message(
                    group_id, f"push_{int(time.time()*1000)}", "__bot__", text
                )
            except Exception as push_err:
                logger.warning(
                    "fallback push also failed: %s", str(push_err)[:300]
                )
        return

    # 把 bot 自己的回覆也記進 raw_messages,供之後 quote-lookup
    if group_id is None:
        return
    sent_messages = getattr(resp, "sent_messages", None) or []
    for sm in sent_messages:
        sm_id = getattr(sm, "id", None)
        if sm_id:
            memory.log_raw_message(group_id, sm_id, "__bot__", text)


def _download_content(message_id: str) -> bytes:
    """從 LINE 下載 image/video/audio/file 訊息的原始 bytes。"""
    with ApiClient(_line_config) as api_client:
        return bytes(MessagingApiBlob(api_client).get_message_content(message_id))


def _guess_mime_type(file_name: str) -> str:
    mt, _ = mimetypes.guess_type(file_name)
    return mt or "application/octet-stream"
