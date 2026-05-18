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

import json as _json
import logging
import mimetypes
import os
import re
import threading
import time
import uuid as _uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests as _requests
from bs4 import BeautifulSoup

try:
    import yt_dlp as _yt_dlp  # type: ignore[import-untyped]

    _YTDLP_AVAILABLE = True
except ImportError:
    _yt_dlp = None
    _YTDLP_AVAILABLE = False

from fastapi import FastAPI, Header, HTTPException, Request
from google.genai import types
from linebot.v3 import WebhookParser  # type: ignore[import-untyped]
from linebot.v3.exceptions import InvalidSignatureError  # type: ignore[import-untyped]
from linebot.v3.messaging import (  # type: ignore[import-untyped]
    ApiClient,
    Configuration,
    ImageMessage,
    MessagingApi,
    MessagingApiBlob,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (  # type: ignore[import-untyped]
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

import bot_stats
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
    _h.setFormatter(
        _DualTZFormatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    )

logger = logging.getLogger("line_bot")

app = FastAPI()

# Optional jobs router (n8n / external scheduler trigger surface).
# Feature flag: JOBS_ROUTES_ENABLED=1 enables /jobs/* endpoints.
# Load .env explicitly because pydantic-settings doesn't push to os.environ.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=False)
except ImportError:
    pass
if os.getenv("JOBS_ROUTES_ENABLED") == "1":
    from jobs_router import router as jobs_router, startup_sweep
    app.include_router(jobs_router)

    @app.on_event("startup")
    async def _jobs_startup_sweep():
        startup_sweep()

_parser = WebhookParser(settings.line_channel_secret)


def _get_line_config() -> Configuration:
    """每次 LINE API 呼叫前用這個拿最新 token：
    - 有 LINE_CHANNEL_ID → v3 stateless short-lived，每 15 分自動 refresh
    - 沒設 → fallback 到 .env 的 long-lived token（向後相容）
    """
    try:
        from line_token_refresh import get_line_token

        tok = get_line_token() or settings.line_channel_access_token
    except Exception:
        tok = settings.line_channel_access_token
    return Configuration(access_token=tok)


_line_config = _get_line_config()  # 啟動時的 fallback；callsite 仍會用 _get_line_config() 取最新


# ── LINE 訊息配額 ─────────────────────────────────────────────────────────────


def _get_quota_footer() -> str:
    """配額 footer — 用戶反饋（2026-05-05）「拿掉 📊 Gemini 今日用量 99.0%」，
    平常 80%+ 警示移除（破壞對話自然度），只保留「真的爆了」紅燈當回覆無能信號。
    """
    if _quota_exhausted():
        return "\n\n📊 Gemini 今日用量已用完"
    return ""


def _llm_chat(
    user_input,
    context: list[tuple[str, str]],
    facts: list[str],
    pnotes: list[dict] | None = None,
) -> str:
    """Gemini chat。quota 爆時改走 lite_reply 純規則模板 fallback。

    2026-05-18：移除 local_llm / RAG / agent_loop / llm_router 等本機 text LLM 系列
    （user directive: local 只負責圖片回應，其餘 gemini 負責）。Gemini 爆時 fallback
    從 4-tier 簡化為 lite_reply 單 tier (Stage 1 寫死 handlers + Stage 3 rule-based,
    lite_reply.py 內 Stage 2 local LLM 自動 graceful degrade)。全敗回 ""。
    """
    if _quota_exhausted():
        try:
            import lite_reply
            from gemini_client import _extract_text
            user_text = _extract_text(user_input)
            out = lite_reply.lite_reply(user_text, context=context)
            if out:
                logger.info(
                    "quota exhausted → lite_reply hit (text=%r)", user_text[:50]
                )
                return out
        except Exception as e:
            logger.warning("lite_reply fallback failed: %s", e)
        return ""
    result = gemini_client.chat(user_input, context, facts, pnotes)
    if result:
        bot_stats.track_reply("gemini")
    return result


# ── URL 預抓取（繞過 Gemini url_context 的限制）─────────────────────────────

_URL_RE = re.compile(r"https?://\S+")
_PREFETCH_TIMEOUT = 5  # 秒，避免拖太久讓 reply_token 過期
_PREFETCH_MAX_CHARS = 5000  # 截斷上限，避免塞爆 prompt
_PREFETCH_MAX_URLS = 2  # 一次最多抓幾個連結
_PREFETCH_MIN_CHARS = 80  # 低於此長度視為垃圾（JS 渲染空殼），不塞進 prompt

_YTDLP_TIMEOUT = 12  # yt-dlp 單次提取上限（秒）
_YTDLP_SUBTITLE_MAX_CHARS = 3000
_YTDLP_SUBTITLE_LANGS = ["zh-TW", "zh-Hant", "zh", "zh-Hans", "en"]

# Gemini Video Understanding fallback 參數
_GEMINI_VIDEO_TIMEOUT = 60  # 整個 download + upload + analyze 的總上限（秒）
_GEMINI_VIDEO_MAX_FILESIZE = "50M"  # yt-dlp --max-filesize
_GEMINI_VIDEO_MIN_REMAINING_QUOTA = 5  # 今日剩餘 < 此值就不啟動（影片呼叫 token 較重）
_GEMINI_VIDEO_THIN_THRESHOLD = 300  # 前面 prefetch 結果 < 此字數才 fallback
# 哪些 domain 才允許走 Gemini video fallback（短影音 / JS 渲染）
# 注意：YouTube 因為 yt-dlp 已能抓字幕，不啟動 fallback
_GEMINI_VIDEO_DOMAINS = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*("
    r"tiktok\.com|instagram\.com|threads\.net|facebook\.com|fb\.watch|x\.com|twitter\.com"
    r")/",
    re.IGNORECASE,
)

# JS 渲染 / Cloudflare 保護的網站，requests.get() 抓不到有效內容
# 這些網站一律不 prefetch，直接讓 Gemini 用 Google Search 處理
_JS_RENDERED_DOMAINS = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*("
    r"tiktok\.com|instagram\.com|threads\.net|facebook\.com|fb\.watch|"
    r"dcard\.tw|x\.com|twitter\.com|reddit\.com|"
    r"youtube\.com/shorts|youtu\.be"
    r")/",
    re.IGNORECASE,
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
    deduped: list[str] = []
    for ln in lines:
        if not deduped or ln != deduped[-1]:
            deduped.append(ln)
    return "\n".join(deduped)


def _extract_subtitles_from_info(info: dict) -> str | None:
    """從 yt-dlp info dict 拿字幕文字（優先人工字幕 → 自動生成，語言優先順序見常數）。"""
    for subs_dict in (
        info.get("subtitles") or {},
        info.get("automatic_captions") or {},
    ):
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
        logger.info(
            "ytdlp OK url=%s chars=%d has_subs=%s", url, len(block), bool(subtitle_text)
        )
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
                    url,
                    timeout=_PREFETCH_TIMEOUT,
                    allow_redirects=True,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
                    },
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
            logger.info(
                "tiktok oembed error code=%s url=%s", data.get("code"), target_url
            )
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
            url,
            author_id or author_name,
            len(block),
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
                    url,
                    timeout=_PREFETCH_TIMEOUT,
                    allow_redirects=True,
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
            json_url,
            timeout=_PREFETCH_TIMEOUT,
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
        top_comments: list[str] = []
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
            url,
            subreddit,
            len(top_comments),
            len(block),
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
    kind = m.group(1).lower()  # reel 或 p
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
            meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
                "meta", attrs={"property": "og:description"}
            )
            if meta:
                caption = str(meta.get("content") or "").strip()

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


def _gemini_video_quota_ok() -> bool:
    """今日 Gemini quota 剩餘 >= _GEMINI_VIDEO_MIN_REMAINING_QUOTA 才允許走 video fallback。

    影片呼叫的 token 量比文字大很多（一段 30 秒影片 ~3-10k tokens），需要保留餘裕給
    一般對話。失敗時 conservative，回 False（寧可不啟動也不要爆 quota）。
    """
    try:
        info = gemini_client.get_gemini_quota_info()
        if info is None:
            return False
        remaining_req = info["limit_requests"] - info["used_requests"]
        return remaining_req >= _GEMINI_VIDEO_MIN_REMAINING_QUOTA
    except Exception:
        return False


def _fetch_video_gemini(url: str) -> str | None:
    """
    終極 fallback：用 yt-dlp 把短影片抓下來，丟給 Gemini 2.5 Flash 用 Files API 分析。

    觸發條件（caller 負責判斷）：
      - prefetch chain 拿到的內容 < _GEMINI_VIDEO_THIN_THRESHOLD 字
      - URL 屬於 _GEMINI_VIDEO_DOMAINS（TikTok / IG / FB / Threads / X，不含 YouTube）
      - 今日 Gemini quota 剩餘 >= _GEMINI_VIDEO_MIN_REMAINING_QUOTA

    流程：
      1. yt-dlp 下載 MP4 到 /tmp/linebot_video_<hash>.mp4（--max-filesize 50M）
      2. 上傳到 Gemini Files API
      3. 輪詢直到 file.state == ACTIVE
      4. generate_content("用繁體中文簡短描述...")
      5. 清理：刪除 Files API 上的檔案 + 本地 mp4

    任何錯誤回 None，由 caller fallback。整個流程有 60 秒總 timeout 保護。
    """
    if not _YTDLP_AVAILABLE:
        return None

    import hashlib
    from pathlib import Path

    start_ts = time.time()
    url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
    local_path = f"/tmp/linebot_video_{url_hash}.mp4"
    uploaded_file = None

    def _elapsed_ok() -> bool:
        return (time.time() - start_ts) < _GEMINI_VIDEO_TIMEOUT

    try:
        # 1) 下載影片：優先低畫質（短影音分析不需要 1080p，能省 5-10x bandwidth）
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "max_filesize": 50 * 1024 * 1024,
            "outtmpl": local_path,
            # 偏好最差但有畫質的 mp4：降低 bandwidth、加快 download
            "format": "worst[ext=mp4]/worst/mp4",
            # 短影音 CDN 偶爾很慢，整個下載階段給 35 秒（剩 25 秒給 upload + analyze）
            "socket_timeout": 35,
            "overwrites": True,
        }
        with _yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
            logger.info("gemini video fallback: download empty url=%s", url)
            return None

        if not _elapsed_ok():
            logger.info("gemini video fallback: timeout after download url=%s", url)
            return None

        # 2) 上傳到 Files API
        uploaded_file = gemini_client._client.files.upload(file=Path(local_path))

        # 3) 輪詢直到 ACTIVE
        while True:
            if not _elapsed_ok():
                logger.info(
                    "gemini video fallback: timeout while waiting active url=%s", url
                )
                return None
            assert uploaded_file.name is not None  # narrow for mypy
            f = gemini_client._client.files.get(name=uploaded_file.name)
            state = getattr(f.state, "name", str(f.state))
            if state == "ACTIVE":
                uploaded_file = f
                break
            if state == "FAILED":
                logger.info("gemini video fallback: file FAILED url=%s", url)
                return None
            time.sleep(1)

        # 4) generate_content — flash 滿了自動 fallback flash-lite
        prompt = (
            "用繁體中文簡短描述這部影片的主要內容（含口白、畫面、訊息），"
            "200 字內。如果是廣告/業配/沒重點請直說。"
        )
        models_to_try = [settings.gemini_model]
        if settings.gemini_light_model and settings.gemini_light_model != settings.gemini_model:
            models_to_try.append(settings.gemini_light_model)

        response = None
        last_error = None
        contents_list = [uploaded_file, prompt]
        for model_name in models_to_try:
            try:
                response = gemini_client._client.models.generate_content(
                    model=model_name,
                    contents=contents_list,  # type: ignore[arg-type]
                )
                logger.info(
                    "gemini video fallback: model=%s succeeded url=%s",
                    model_name, url,
                )
                break
            except Exception as e:
                last_error = e
                err_str = str(e)
                is_quota = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str
                if is_quota and model_name != models_to_try[-1]:
                    logger.info(
                        "gemini video fallback: model=%s 429 quota, trying lite url=%s",
                        model_name, url,
                    )
                    continue
                # 非 quota 錯誤、或已經試完所有模型 → 拋給外層 except
                raise

        if response is None:
            logger.info("gemini video fallback: all models exhausted url=%s err=%s", url, last_error)
            return None

        # 計入 quota counter
        try:
            gemini_client._track_usage(response)
        except Exception:
            pass

        text = (response.text or "").strip()
        if not text:
            logger.info("gemini video fallback: empty response url=%s", url)
            return None

        block = (
            f"（以下是影片連結 {url} 的內容，由 Gemini Video Understanding 分析）\n"
            f"--- 影片分析開始 ---\n"
            f"{text}\n"
            f"--- 影片分析結束 ---"
        )
        logger.info(
            "gemini video fallback used url=%s chars=%d", url, len(block)
        )
        return block
    except Exception as e:
        logger.info("gemini video fallback failed url=%s: %s", url, e)
        return None
    finally:
        # 清理 Files API 上傳的檔案
        if uploaded_file is not None and uploaded_file.name is not None:
            try:
                gemini_client._client.files.delete(name=uploaded_file.name)
            except Exception as e:
                logger.debug("gemini video fallback: delete remote failed: %s", e)
        # 清理本地 mp4
        try:
            if os.path.exists(local_path):
                os.unlink(local_path)
        except Exception as e:
            logger.debug("gemini video fallback: unlink local failed: %s", e)


def _maybe_video_fallback(url: str, current_block: str | None) -> str | None:
    """若 current_block 太薄且 URL 是視訊平台，嘗試 Gemini video fallback。

    回傳：fallback 結果 OR 原 current_block（保持原行為）
    """
    if not _GEMINI_VIDEO_DOMAINS.search(url):
        return current_block
    chars = len(current_block) if current_block else 0
    if current_block is not None and chars >= _GEMINI_VIDEO_THIN_THRESHOLD:
        return current_block
    # 不做本地 quota pre-block——讓 Google server-side 決定。429 由 _fetch_video_gemini
    # 內部 catch 後回 None，pipeline 自然 fallback 不影響流程。能送就送、不要白白浪費機會。
    fallback = _fetch_video_gemini(url)
    if fallback:
        # 如果有原本的 block（薄但非空），把兩者拼起來給 model 更多 context
        if current_block:
            return current_block + "\n\n" + fallback
        return fallback
    return current_block


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
                # 內容太薄（< 300 chars）→ Gemini Video Understanding fallback
                block = _maybe_video_fallback(url, block)
                if block:
                    blocks.append(block)
                else:
                    logger.info("tiktok: ytdlp + oembed + gemini all failed, skip url=%s", url)
                continue
            if "youtube.com" in u_lower or "youtu.be" in u_lower:
                # YouTube 不走 video fallback：yt-dlp 已能抓字幕
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

            # 2) Instagram Reels / Posts：yt-dlp → embed 頁面 → Gemini video → Google Search
            if "instagram.com" in u_lower:
                block = _fetch_video_ytdlp(url) or _fetch_instagram_embed(url)
                block = _maybe_video_fallback(url, block)
                if block:
                    blocks.append(block)
                else:
                    logger.info(
                        "instagram: all methods failed url=%s → Gemini Google Search",
                        url,
                    )
                continue

            # 3) 其他 JS 渲染網站（FB / X / Threads / dcard）：試 yt-dlp，再試 Gemini video，失敗才 Google Search
            if _JS_RENDERED_DOMAINS.search(url):
                block = _fetch_video_ytdlp(url)
                block = _maybe_video_fallback(url, block)
                if block:
                    blocks.append(block)
                else:
                    logger.info(
                        "prefetch skip (JS/CF site, ytdlp failed) url=%s → Gemini Google Search",
                        url,
                    )
                continue

            # 一般網頁：直接抓取 HTML
            resp = _requests.get(
                url,
                timeout=_PREFETCH_TIMEOUT,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
                },
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            content = soup.get_text(separator="\n", strip=True)

            # 內容太短 = JS 渲染空殼或 Cloudflare 頁面，不塞垃圾進 prompt
            if len(content) < _PREFETCH_MIN_CHARS:
                logger.info(
                    "prefetch skip (too short %d chars) url=%s", len(content), url
                )
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


_GENIMG_DIR = "/tmp/line_bot_genimg"
_GENIMG_FILENAME_RE = re.compile(r"^[a-f0-9]{32}\.png$")


@app.get("/static/img/{filename}")
def serve_genimg(filename: str):
    """Serve generated images for LINE ImageMessage（public via cloudflared）。

    路徑驗證：filename 必須是 UUID hex（32 字）+ .png，避免 path traversal。
    """
    from fastapi.responses import FileResponse
    from fastapi import HTTPException
    if not _GENIMG_FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="invalid filename")
    path = os.path.join(_GENIMG_DIR, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="image/png")


# ── Webhook ───────────────────────────────────────────────────────────────────


@app.post("/callback")
async def callback(request: Request, x_line_signature: str = Header(None)):
    body = (await request.body()).decode("utf-8")
    if not x_line_signature:
        client = request.client.host if request.client else "?"
        logger.warning("missing x-line-signature header from %s body_len=%d", client, len(body))
        raise HTTPException(status_code=400, detail="missing signature")
    # PII protection: 預設只印 body_sha256（除錯時設 DEBUG_RAW_BODY=1 印 body[:800]）
    if os.getenv("DEBUG_RAW_BODY") == "1":
        print(f"[RAW] sig={x_line_signature} len={len(body)} body={body[:800]}", flush=True)
    else:
        import hashlib as _hl
        _body_hash = _hl.sha256(body.encode("utf-8")).hexdigest()[:12]
        print(f"[RAW] sig={x_line_signature} len={len(body)} body_sha256={_body_hash}", flush=True)
    try:
        events = _parser.parse(body, x_line_signature)
    except InvalidSignatureError:
        logger.warning("invalid signature")
        raise HTTPException(status_code=400, detail="invalid signature")

    print(f"[PARSED] event_count={len(events)}", flush=True)
    for event in events:
        src = getattr(event, "source", None)
        gid = getattr(src, "group_id", None) if src else None
        print(
            f"[EVENT] type={type(event).__name__} source={type(src).__name__ if src else None} group_id={gid}",
            flush=True,
        )
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
            print(
                f"[MEMBER_EVT] {type(event).__name__} {event.model_dump_json()}",
                flush=True,
            )
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
            logger.info("processing missed redelivery msg_id=%s group=%s", msg_id, gid)
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

    # ── quota 爆時：所有訊息都存 pending，等恢復後由 Gemini 分組 + 逐組引用回覆 ──
    # 2026-05-17 加 (§3 abbreviated chain): 之前的 short-circuit return 把 reply_token
    # 浪費掉，導致 pending 在 Gemini 爆期間無法 drain。現補 _try_piggyback_drain_with_reply_token
    # — 在 return 前嘗試用 incoming reply_token bundle 最多 2 條 pending drain 走 local
    # LLM fallback，免費不耗 LINE 月配額。failure pending 完整保留 (peek-then-confirm)。
    if _quota_exhausted() and isinstance(
        msg, (TextMessageContent, FileMessageContent, AudioMessageContent)
    ):
        _save_pending_any(event, group_id, sender_user_id, msg)
        if isinstance(msg, TextMessageContent):
            memory.log_raw_message(group_id, msg.id, sender_user_id, msg.text or "")
            bot_stats.track_message(msg.text or "")
            bot_stats.track_pending_saved()
        _try_piggyback_drain_with_reply_token(event.reply_token, group_id)
        return

    # Piggyback drain：有人留言就順手補該 group 的 pending（throttle + quota gate 在 helper 裡）
    _spawn_piggyback_drain(group_id)

    # 文字：先記進 raw_messages（供 quote 回查 / burst look-back / Layer 2 抓 trigger）
    if isinstance(msg, TextMessageContent):
        memory.log_raw_message(group_id, msg.id, sender_user_id, msg.text or "")
        bot_stats.track_message(msg.text or "")
        _handle_text_message(event, group_id)
        return

    # 2026-05-16 改：圖片/影片走 A+B 雙路徑。
    # A: 同步存 pending（webhook 路徑保證不丟資料）
    # B: 非同步 _MEDIA_EXECUTOR (cap 2) 跑 local analyze → reply → remove pending
    # B 失敗（local 跑掛 / timeout / reply 失敗）→ pending 保留給 cron retry worker drain
    if isinstance(msg, ImageMessageContent):
        memory.log_raw_message(group_id, msg.id, sender_user_id, "[圖片]")
        _save_pending_any(event, group_id, sender_user_id, msg)
        _MEDIA_EXECUTOR.submit(_handle_image_message, event, group_id)
        return
    if isinstance(msg, VideoMessageContent):
        memory.log_raw_message(group_id, msg.id, sender_user_id, "[影片]")
        _save_pending_any(event, group_id, sender_user_id, msg)
        _MEDIA_EXECUTOR.submit(_handle_video_message, event, group_id)
        return
    if isinstance(msg, AudioMessageContent):
        memory.log_raw_message(group_id, msg.id, sender_user_id, "[音訊]")
        _handle_audio_message(event, group_id)
        return

    # 檔案：只處理文字檔，其他婉拒（file 很罕見，不是 burst 的一部分）
    if isinstance(msg, FileMessageContent):
        _handle_file_message(event, group_id)
        return


def _handle_audio_message(event: MessageEvent, group_id: str) -> None:
    """語音留言自動分析 — 不需要 @mention，下載後直接丟 Gemini 轉寫 + 回應。"""
    if _quota_exhausted():
        return
    try:
        data = _download_content(event.message.id)
    except Exception as e:
        logger.warning("download audio failed: %s", e)
        return
    if len(data) > _MEDIA_BYTE_LIMIT:
        return
    parts = [
        types.Part.from_bytes(data=bytes(data), mime_type="audio/m4a"),
        "(群組成員傳了一段語音留言，請先完整轉寫內容，再根據系統指令判斷是否有查核或回應價值。若只是閒聊請用一兩句自然回應即可。)",
    ]
    context = memory.get_context(group_id)
    facts = memory.top_facts(group_id)
    pnotes = _get_persona_notes(group_id)
    try:
        with _thinking_indicator(group_id):
            reply_text = _llm_chat(parts, context, facts, pnotes)
    except Exception as e:
        if _is_quota_error(e):
            _mark_quota_exhausted()
        else:
            logger.exception("gemini chat (audio) failed: %s", e)
        return
    if not reply_text or not reply_text.strip():
        return
    memory.append_turn(group_id, "user", "[語音留言]")
    memory.append_turn(group_id, "bot", reply_text)
    _maybe_extract_facts(group_id)
    _reply(event.reply_token, reply_text, group_id=group_id)


def _handle_image_message(event, group_id):
    """Pure-local image handler. Runs in _MEDIA_EXECUTOR (cap 2 concurrent).

    Flow: download → media_pipeline.analyze_image (50s hard timeout per GP1 §2,
    because reply_token TTL is ~1 min) → reply → remove from pending. On any
    failure, leave the pending entry for the cron retry worker to drain later.
    """
    import threading as _threading
    msg_id = event.message.id
    try:
        data = _download_content(msg_id)
    except Exception as e:
        logger.warning("image download failed for handler: %s", e)
        return
    if len(data) > _MEDIA_BYTE_LIMIT:
        logger.info("image too large for handler: %d bytes", len(data))
        return

    holder = {"reply": None}

    def _run():
        try:
            import media_pipeline
            holder["reply"] = media_pipeline.analyze_image(bytes(data))
        except Exception as e:
            logger.warning("image analyze failed: %s", e)

    t = _threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=50)
    if t.is_alive():
        logger.info("image analyze timeout (>50s), leaving pending for retry worker")
        return
    reply_text = holder["reply"]
    if not reply_text or not reply_text.strip():
        logger.info("image analyze returned empty, leaving pending")
        return
    memory.append_turn(group_id, "user", "[圖片]")
    memory.append_turn(group_id, "bot", reply_text)
    try:
        _reply(event.reply_token, reply_text, group_id=group_id)
        _remove_pending_by_msg_id(group_id, msg_id)
    except Exception as e:
        logger.warning("image reply failed: %s", e)


def _handle_video_message(event, group_id):
    """Pure-local video handler. Same shape as _handle_image_message."""
    import threading as _threading
    msg_id = event.message.id
    try:
        data = _download_content(msg_id)
    except Exception as e:
        logger.warning("video download failed for handler: %s", e)
        return
    if len(data) > _MEDIA_BYTE_LIMIT:
        logger.info("video too large for handler: %d bytes", len(data))
        return

    holder = {"reply": None}

    def _run():
        try:
            import media_pipeline
            holder["reply"] = media_pipeline.analyze_video(bytes(data))
        except Exception as e:
            logger.warning("video analyze failed: %s", e)

    t = _threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=50)
    if t.is_alive():
        logger.info("video analyze timeout (>50s), leaving pending for retry worker")
        return
    reply_text = holder["reply"]
    if not reply_text or not reply_text.strip():
        logger.info("video analyze returned empty, leaving pending")
        return
    memory.append_turn(group_id, "user", "[影片]")
    memory.append_turn(group_id, "bot", reply_text)
    try:
        _reply(event.reply_token, reply_text, group_id=group_id)
        _remove_pending_by_msg_id(group_id, msg_id)
    except Exception as e:
        logger.warning("video reply failed: %s", e)


def _handle_text_message(event: MessageEvent, group_id: str) -> None:
    text = event.message.text or ""

    # 回饋收集：20:00 ~ 02:00 TW 窗口內，將文字訊息存入 pending_feedback.json
    if feedback_collector.in_feedback_window():
        sender = getattr(event.source, "user_id", None) or "unknown"
        try:
            feedback_collector.collect_message(sender, text)
        except Exception as e:
            logger.warning("[Feedback] collect_message failed: %s", e)

    # Organic 糾正偵測（2026-05-08 加）：user 講「不對 / 你誤會」之類的
    # 自然糾正訊號 → 抓上一輪 user/bot 訊息拼成 correction 寫進 persona_notes
    # 純信號擷取，**不**接管後續路由（用戶可能糾正完還想繼續對話）
    sender_uid_for_correction = getattr(event.source, "user_id", None) or ""
    _detect_user_correction(text, group_id, sender_uid_for_correction)

    # 自動萃 knowledge graph 三元組（純本機，fire-and-forget）
    try:
        import knowledge_graph
        knowledge_graph.auto_extract_kg_async(group_id, text)
    except (ImportError, Exception) as e:
        logger.debug("knowledge_graph extract skipped: %s", e)

    # 自動分類（rule-first → Gemini lite，fire-and-forget；2026-05-10 加）
    try:
        import message_classifier
        message_classifier.classify_async(group_id, event.message.id, text)
    except (ImportError, Exception) as e:
        logger.debug("message_classifier skipped: %s", e)

    # 自動偵測 reminder（2026-05-08：含日期+時間 hint 的訊息抽 action 存 DB）
    # 失敗 silent，不阻塞主流程
    sender_uid_for_reminder = getattr(event.source, "user_id", None) or ""
    _maybe_extract_reminder(text, group_id, sender_uid_for_reminder)

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

    # 4. 其他文字訊息 → burst_filter debounce（等對方說完再回）
    sender_user_id = getattr(event.source, "user_id", None) or ""
    burst_filter.add_to_burst(
        group_id, event.message.id, text, sender_user_id, event.reply_token
    )


_IMAGE_GEN_PATTERNS = [
    re.compile(r"^[\s]*[畫繪]一?張?[\s]*[:：]?[\s]*(.+)", re.IGNORECASE),
    re.compile(r"^[\s]*生成圖片?[\s]*[:：]?[\s]*(.+)", re.IGNORECASE),
    re.compile(r"^[\s]*做一?張[圖照]?[\s]*[:：]?[\s]*(.+)", re.IGNORECASE),
    re.compile(r"^[\s]*幫我[畫繪][\s]*[:：]?[\s]*(.+)", re.IGNORECASE),
    re.compile(r"^[\s]*draw\s+(?:me\s+)?(.+)", re.IGNORECASE),
    re.compile(r"^[\s]*imagine\s+(.+)", re.IGNORECASE),
]


def _detect_image_gen_request(text: str) -> str | None:
    """偵測「畫一張 X / 生成圖 Y / draw Z」→ 回主題；無命中回 None。"""
    s = (text or "").strip()
    if not s:
        return None
    for pat in _IMAGE_GEN_PATTERNS:
        m = pat.match(s)
        if m:
            subject = m.group(1).strip()
            if subject and len(subject) >= 2:
                return subject
    return None


def _handle_image_gen(event: MessageEvent, group_id: str, subject: str) -> None:
    """圖片生成 — 本機 mlx SD/FLUX。LINE 需要 public URL，目前先存本機。"""
    try:
        import image_gen_local
    except ImportError:
        logger.info("image_gen_local 未安裝，silent skip")
        return
    try:
        png = image_gen_local.generate(subject, style="photo")
    except Exception as e:
        logger.warning("image_gen_local.generate failed: %s", e)
        _reply(event.reply_token, "咪寶生成圖失敗，等下再試試喔", group_id=group_id)
        return
    if not png:
        _reply(event.reply_token, "咪寶今天畫不出來（model 沒載成功）", group_id=group_id)
        return
    import uuid
    out_dir = _GENIMG_DIR
    os.makedirs(out_dir, exist_ok=True)
    fname = f"{uuid.uuid4().hex}.png"
    out_path = os.path.join(out_dir, fname)
    try:
        with open(out_path, "wb") as f:
            f.write(png)
    except Exception as e:
        logger.warning("save genimg failed: %s", e)
        return
    # 取 public URL：env IMAGE_GEN_PUBLIC_URL 優先，否則讀 /tmp/cloudflared_line_bot_url.txt
    public_url = os.environ.get("IMAGE_GEN_PUBLIC_URL", "").rstrip("/")
    if not public_url:
        try:
            with open("/tmp/cloudflared_line_bot_url.txt") as f:
                public_url = f.read().strip().rstrip("/")
        except Exception:
            public_url = ""

    if public_url:
        # 真的傳 ImageMessage
        img_url = f"{public_url}/static/img/{fname}"
        try:
            with ApiClient(_line_config) as api_client:
                MessagingApi(api_client).reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[ImageMessage(
                            original_content_url=img_url,
                            preview_image_url=img_url,
                        )],
                    )
                )
            memory.append_turn(group_id, "user", f"[圖片生成] {subject}")
            memory.append_turn(group_id, "bot", f"[已傳圖] {img_url}")
        except Exception as e:
            logger.warning("reply ImageMessage failed: %s", e)
            _reply(event.reply_token, f"圖生成 OK 但傳 LINE 失敗：{e}", group_id=group_id)
        return

    # 沒設 public URL → 文字 fallback（已存本機）
    msg = (
        f"圖片已生成（{len(png)//1024} KB）\n"
        f"暫存：{out_path}\n"
        f"啟動 cloudflared tunnel 後可直接傳 LINE"
    )
    _reply(event.reply_token, msg, group_id=group_id)
    memory.append_turn(group_id, "user", f"[圖片生成] {subject}")
    memory.append_turn(group_id, "bot", f"[已生成] {out_path}")


def _handle_explicit_text(event: MessageEvent, group_id: str, clean_text: str) -> None:
    """使用者明確叫 bot（@mention / /ai 等），立刻丟 Gemini 回覆。"""
    sender_user_id = getattr(event.source, "user_id", None) or ""

    # 若引用了媒體訊息（圖片 / 影片 / 音訊），走 multimodal 路徑
    quoted_id = getattr(event.message, "quoted_message_id", None)
    if quoted_id:
        raw = memory.get_raw_message(group_id, quoted_id)
        if raw is not None and raw[1] in _MEDIA_PLACEHOLDERS:
            _handle_media_via_quote(event, group_id, clean_text, quoted_id, raw[1])
            return

    # 圖片生成請求（畫一張 / 生成圖片 / draw me ...）→ 純本機 mlx SD
    gen_subject = _detect_image_gen_request(clean_text)
    if gen_subject:
        _handle_image_gen(event, group_id, gen_subject)
        return

    # clean_text 空且沒引用 → 用戶只打「咪寶」等觸發詞 → 問候回應
    if not clean_text and not quoted_id:
        _reply(event.reply_token, "嗯？\n怎麼了嗎\n要找我什麼啦", group_id=group_id)
        return

    # 純文字 + 可能的文字引用
    quoted_block = _build_quoted_block(event.message, group_id)
    # 空 clean_text + 有引用 → 讓 Gemini 針對原文回應
    if not clean_text and quoted_block:
        user_input = (
            f"{quoted_block}\n\n(使用者只輸入觸發詞呼叫你，請針對上面引用的原文做回應)"
        )
    else:
        user_input = (
            clean_text if not quoted_block else f"{quoted_block}\n\n{clean_text}"
        )

    # URL 預抓取：先用 Python 抓網頁內容塞進 prompt，繞過 Gemini url_context 的限制
    user_input = _prefetch_urls(user_input)

    # 2026-05-16 修：刪掉 quota 短路。
    # Gemini quota 爆時 _llm_chat 內部會自動走 llm_router.fallback_chat
    # (local LLM → RAG → lite_reply) 4-tier waterfall；不能在這層 return，
    # 否則家人 @咪寶時 bot 會完全沉默。
    context = memory.get_context(group_id)
    facts = memory.top_facts(group_id, user_id=sender_user_id)
    pnotes = _get_persona_notes(group_id)
    try:
        with _thinking_indicator(group_id):
            reply_text = _llm_chat(user_input, context, facts, pnotes)
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
    _maybe_extract_facts(group_id, user_id=sender_user_id)
    _reply(event.reply_token, reply_text, group_id=group_id)


def _handle_burst_flush(group_id: str, combined_text: str, reply_token: str) -> None:
    """burst_filter 判定「值得主動回應」時觸發。跑在 Timer 的 thread 裡。

    規則:「會回應的情境」不能靜默 — 要不是真回應，要不就回 quota 訊息。
    """
    logger.info(
        "burst flush triggered group=%s text_len=%d",
        group_id,
        len(combined_text),
    )

    # 2026-05-18 加：抽家族財經觀點寫入 finance_views（fire-and-forget，不擋主流程）
    try:
        import finance_view_extractor
        finance_view_extractor.maybe_extract_and_save_async(
            group_id, combined_text
        )
    except Exception as e:
        logger.debug("finance_view extract skipped: %s", e)

    # 2026-05-16 修：刪掉 quota 短路。
    # 之前在 quota 爆時直接 return 違反 docstring 寫的「會回應的情境不能靜默」原則，
    # 也讓 llm_router.fallback_chat 4-tier waterfall（local LLM → RAG → lite_reply）
    # 完全沒機會啟動。改成繼續走 _llm_chat，由它內部判斷走 Gemini 還是 fallback。

    # cache 命中：謠言快取直接回，省 LLM 呼叫
    cached = memory.check_fact_cache(group_id, combined_text)
    if cached:
        logger.info("burst flush cache hit group=%s", group_id)
        _reply(reply_token, cached, group_id=group_id)
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
        with _thinking_indicator(group_id):
            reply_text = _llm_chat(user_input, context, facts, pnotes)
    except Exception as e:
        if _is_quota_error(e):
            _mark_quota_exhausted()
            logger.warning("gemini chat (burst) quota exhausted")
        else:
            logger.exception("gemini chat (burst) failed: %s", e)
            _reply(
                reply_token, "Gemini 那邊好像塞車了，等一下再回你～", group_id=group_id
            )
        return

    logger.info(
        "burst gemini reply len=%d text=%s",
        len(reply_text) if reply_text else 0,
        repr(reply_text[:200]) if reply_text else "(empty)",
    )
    if not reply_text or not reply_text.strip():
        logger.warning("burst gemini returned empty reply, skipping LINE send")
        return

    memory.store_fact_cache(group_id, combined_text, reply_text)
    memory.append_turn(group_id, "user", f"[burst]\n{combined_text}")
    memory.append_turn(group_id, "bot", reply_text)
    _maybe_extract_facts(group_id)
    _maybe_capture_calendar_event(group_id, combined_text)
    _reply(reply_token, reply_text, group_id=group_id)


def _maybe_capture_calendar_event(group_id: str, combined_text: str) -> None:
    """從 burst 抽出家族活動 → 寫 events / 取消 events。失敗不擋主流程。"""
    try:
        import calendar_db
        import calendar_extractor

        data = calendar_extractor.extract(combined_text)
        if data["is_cancellation"]:
            kw = data.get("cancel_target_keyword")
            target = calendar_db.find_active_event(
                group_id, keyword=kw, near_date=data.get("date")
            )
            if target:
                if data.get("date") and data["date"] != target["event_date"]:
                    calendar_db.update_event_date(
                        target["event_id"], data["date"], data.get("time")
                    )
                    logger.info(
                        "calendar event rescheduled: %s → %s (group=%s)",
                        target["event_id"],
                        data["date"],
                        group_id,
                    )
                else:
                    calendar_db.cancel_event(target["event_id"])
                    logger.info(
                        "calendar event cancelled: %s (kw=%s, group=%s)",
                        target["event_id"],
                        kw,
                        group_id,
                    )
            return
        if data["has_event"] and data.get("title") and data.get("date"):
            event_id = calendar_db.insert_event(
                group_id=group_id,
                title=data["title"],
                event_date=data["date"],
                event_time=data.get("time"),
                location=data.get("location"),
                participants=data.get("participants") or [],
            )
            logger.info(
                "calendar event captured: %s '%s' on %s (group=%s)",
                event_id,
                data["title"],
                data["date"],
                group_id,
            )
    except Exception as e:
        logger.warning("calendar capture failed: %s", e)


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

# Bounded worker pool for image/video webhook handlers (GP1 §6: thread leak guard).
# webhook arrival rate * vision LLM latency could exhaust threads → cap at 2 concurrent.
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
_MEDIA_EXECUTOR = _ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-handler")


def _media_pipeline_fallback(
    event: MessageEvent,
    group_id: str,
    clean_text: str,
    quoted_message_id: str,
    mime_type: str,
    media_name: str,
) -> None:
    """quota 爆時的 local fallback：呼叫 media_pipeline.analyze_image / analyze_video。

    任何失敗（download / import / analyze）都沉默退出，避免回給使用者一堆錯誤訊息。
    """
    # 1. download bytes — 失敗就沉默退出（連 Gemini 路徑也是這個 fail mode）
    try:
        data = _download_content(quoted_message_id)
    except Exception as e:
        logger.warning("media_pipeline fallback: download failed: %s", e)
        return

    if len(data) > _MEDIA_BYTE_LIMIT:
        logger.info(
            "media_pipeline fallback: %s too large (%d bytes), skipping",
            media_name,
            len(data),
        )
        return

    # 2. 路由到 image / video pipeline
    try:
        if mime_type.startswith("image/"):
            from media_pipeline import analyze_image

            reply = analyze_image(data, user_prompt=clean_text or "")
        elif mime_type.startswith("video/"):
            from media_pipeline import analyze_video

            reply = analyze_video(data, user_prompt=clean_text or "")
        else:
            return
    except ImportError as e:
        logger.warning("media_pipeline not available: %s", e)
        return
    except Exception as e:
        logger.warning("media_pipeline %s fallback failed: %s", media_name, e)
        return

    # 3. 回應給 user — 沒結果就沉默
    if not reply or not str(reply).strip():
        logger.info("media_pipeline fallback: no reply for %s", media_name)
        return

    try:
        memory.append_turn(group_id, "user", f"[{media_name} + 問題]\n{clean_text}")
        memory.append_turn(group_id, "bot", reply)
    except Exception as e:
        logger.warning("media_pipeline fallback: memory append failed: %s", e)
    _reply(event.reply_token, reply, group_id=group_id)


def _audio_asr_fallback(
    event: MessageEvent,
    group_id: str,
    clean_text: str,
    quoted_message_id: str,
    media_name: str,
) -> None:
    """quota 爆時的 audio fallback — 走本機 mlx-whisper。

    User policy：純本機（不打 Groq / OpenAI Whisper）。下載 audio bytes →
    audio_local.transcribe → 把轉出的文字當 user 訊息餵 fallback_chat → reply。
    任一步失敗沉默退出。
    """
    try:
        data = _download_content(quoted_message_id)
    except Exception as e:
        logger.warning("audio fallback download failed: %s", e)
        return
    if len(data) > _MEDIA_BYTE_LIMIT:
        logger.info("audio fallback skipped: file too large (%d bytes)", len(data))
        return
    try:
        import audio_local
        text = audio_local.transcribe(bytes(data), language="zh")
    except ImportError:
        logger.info("audio_local not available, silent skip")
        return
    except Exception as e:
        logger.warning("audio_local.transcribe failed: %s", e)
        return
    if not text:
        return
    user_input = f"{clean_text}\n\n（語音轉文字）{text}" if clean_text else text
    try:
        import llm_router
        reply = llm_router.fallback_chat(
            user_input,
            context=memory.get_context(group_id),
            facts=memory.top_facts(group_id),
            persona_notes=_get_persona_notes(group_id),
            group_id=group_id,
        )
    except Exception as e:
        logger.warning("audio fallback chat failed: %s", e)
        return
    if not reply:
        return
    memory.append_turn(group_id, "user", f"[語音轉文字] {text}")
    memory.append_turn(group_id, "bot", reply)
    _reply(event.reply_token, reply, group_id=group_id)


def _handle_media_via_quote(
    event: MessageEvent,
    group_id: str,
    clean_text: str,
    quoted_message_id: str,
    placeholder: str,
) -> None:
    """使用者 @AI 並引用了一則圖片/影片/音訊。

    圖片 → **永遠走 local media_pipeline**（OCR + vision LLM），不消耗 Gemini quota（user policy 2026-05-08）。
    影片 → Gemini multimodal；quota 爆才 fallback local keyframes + vision LLM。
    音訊 → Gemini multimodal；quota 爆時 `_audio_asr_fallback` 沉默退出
            （純本機策略下不打雲端 ASR）。
    """
    mime_type, media_name = _MEDIA_PLACEHOLDERS[placeholder]

    # 圖片：永遠 local，不問 quota
    if mime_type.startswith("image/"):
        logger.info("media quote: routing image to local media_pipeline")
        _media_pipeline_fallback(
            event, group_id, clean_text, quoted_message_id, mime_type, media_name
        )
        return

    # 影片 / 音訊：quota 爆 → 影片走 local，音訊先 Groq Whisper ASR 再餵 chat fallback
    if _quota_exhausted():
        if mime_type.startswith("video/"):
            logger.info("media quote (video): quota exhausted → local fallback")
            _media_pipeline_fallback(
                event, group_id, clean_text, quoted_message_id, mime_type, media_name
            )
        else:
            _audio_asr_fallback(
                event, group_id, clean_text, quoted_message_id, media_name
            )
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
        with _thinking_indicator(group_id):
            reply_text = _llm_chat(parts, context, facts, pnotes)
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
        with ApiClient(_get_line_config()) as api_client:
            profile = MessagingApi(api_client).get_group_member_profile(
                group_id, user_id
            )
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
# Gemini 原生支援直接送 bytes 的 MIME
_GEMINI_NATIVE_MIMES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/heic",
    "image/heif",
}
# 80k 字中文 ≈ 100〜120k tokens，留 buffer 給 system prompt + context
_TEXT_CHAR_LIMIT = 80_000


def _extract_office_text(data: bytes, file_name: str) -> str | None:
    """從 Word/Excel/PPT bytes 抽出純文字，失敗回 None。"""
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    try:
        if ext in ("docx",):
            from docx import Document
            import io

            doc = Document(io.BytesIO(data))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        if ext in ("xlsx", "xls"):
            import openpyxl  # type: ignore[import-untyped]
            import io

            wb = openpyxl.load_workbook(
                io.BytesIO(data), read_only=True, data_only=True
            )
            lines = []
            for sheet in wb.worksheets:
                lines.append(f"[工作表：{sheet.title}]")
                for row in sheet.iter_rows(values_only=True):
                    row_str = "\t".join("" if v is None else str(v) for v in row)
                    if row_str.strip():
                        lines.append(row_str)
            return "\n".join(lines)
        if ext in ("pptx",):
            from pptx import Presentation
            import io

            prs = Presentation(io.BytesIO(data))
            lines = []
            for i, slide in enumerate(prs.slides, 1):
                lines.append(f"[第 {i} 頁]")
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        lines.append(shape.text.strip())
            return "\n".join(lines)
    except Exception as e:
        logger.warning("office extract failed (%s): %s", file_name, e)
    return None


def _handle_file_message(event: MessageEvent, group_id: str) -> None:
    """檔案訊息 — 支援 PDF/圖片/Word/Excel/PPT/文字檔，其餘婉拒。"""
    msg = event.message
    file_name = getattr(msg, "file_name", "") or "unknown"
    mime_type = _guess_mime_type(file_name)
    is_text_like = mime_type.startswith("text/") or mime_type in _TEXT_LIKE_MIMES
    is_native = mime_type in _GEMINI_NATIVE_MIMES
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    is_office = ext in ("docx", "xlsx", "xls", "pptx")

    if not (is_text_like or is_native or is_office):
        _reply(
            event.reply_token,
            f"這個檔案格式 ({ext or mime_type}) 目前還不支援。\n"
            f"可以處理的格式：PDF、Word、Excel、PPT、圖片、txt/csv/json。",
            group_id=group_id,
        )
        return

    # 2026-05-16 改：刪 quota 短路。所有 file 路徑都應該嘗試 local fallback —
    # local LLM / vision / OCR 不受 Gemini quota 限制，能跑就跑。
    # 對齊 feedback_quota_fallback_never_skip.md (binary input 也不該無聲)
    try:
        data = _download_content(msg.id)
    except Exception as e:
        logger.exception("download file failed: %s", e)
        _reply(event.reply_token, f"下載檔案失敗:{type(e).__name__}", group_id=group_id)
        return

    context = memory.get_context(group_id)
    facts = memory.top_facts(group_id)
    pnotes = _get_persona_notes(group_id)

    # ── PDF / 圖片 → 直接送 bytes Part 給 Gemini ──────────────────────────────
    if is_native:
        # quota 爆時走純本機 fallback：image → media_pipeline.analyze_image (mlx-vlm+OCR)；
        # PDF → pypdf 抽文字 → _llm_chat (自帶 fallback chain)
        if _quota_exhausted():
            reply_text = None
            if mime_type.startswith("image/"):
                try:
                    from media_pipeline import analyze_image
                    reply_text = analyze_image(
                        data,
                        user_prompt=(
                            "請用繁體中文分析這張圖的內容、主題、可見文字，"
                            "第一句必須是具體判斷或結論，"
                            "不要以「使用者」「我看到」「咪寶」「這張圖」等空話開頭。"
                            f"\n\n[檔名：{file_name}]"
                        ),
                    )
                    logger.info(
                        "file image local fallback: %s -> %d chars",
                        file_name, len(reply_text or ""),
                    )
                except Exception as e:
                    logger.warning("file image local fallback failed: %s", e)
            elif mime_type == "application/pdf":
                try:
                    from pypdf import PdfReader
                    import io
                    reader = PdfReader(io.BytesIO(data))
                    total_pages = len(reader.pages)
                    content = "\n".join(
                        (p.extract_text() or "") for p in reader.pages[:30]
                    )
                    if content.strip():
                        # 抽到文字 → 走 local LLM；prompt 把檔名收尾 metadata，
                        # 開頭明確指示避免 local 14B echo opener (規則 0 違規)
                        content = content[:_TEXT_CHAR_LIMIT]
                        page_note = (
                            f"（PDF 共 {total_pages} 頁，只看前 30 頁）"
                            if total_pages > 30 else ""
                        )
                        prompt_text = (
                            "請根據以下 PDF 內容做分析回應，用繁體中文，"
                            "第一句必須是具體判斷或結論，"
                            "不要以「使用者」「我看到」「咪寶」「這份檔案」等空話開頭。\n\n"
                            f"--- PDF 內容開始 ---\n{content}\n--- PDF 內容結束 ---\n\n"
                            f"[檔名：{file_name}]{page_note}"
                        )
                        reply_text = _llm_chat(prompt_text, context, facts, pnotes)
                        logger.info(
                            "file pdf local fallback: %s -> %d pages, %d chars text, reply %d chars",
                            file_name, total_pages, len(content), len(reply_text or ""),
                        )
                    else:
                        # 抽不到文字 (scanned PDF) → rasterize 第一頁走 vision (per user 2026-05-16)
                        logger.info(
                            "pdf local fallback: %s 抽不到文字 → rasterize page 0 走 analyze_image",
                            file_name,
                        )
                        try:
                            import fitz
                            doc = fitz.open(stream=data, filetype="pdf")
                            fitz_pages = len(doc)
                            if fitz_pages > 0:
                                pix = doc[0].get_pixmap(dpi=150)
                                img_bytes = pix.tobytes("png")
                                doc.close()
                                from media_pipeline import analyze_image
                                reply_text = analyze_image(
                                    img_bytes,
                                    user_prompt=(
                                        f"這是 scanned PDF「{file_name}」的第一頁。"
                                        "請分析內容，第一句必須是具體判斷，"
                                        "不要以「使用者」「我看到」「咪寶」開頭。"
                                    ),
                                )
                                if reply_text and fitz_pages > 1:
                                    reply_text = (
                                        f"{reply_text}\n\n"
                                        f"（scanned PDF 共 {fitz_pages} 頁，只分析第一頁）"
                                    )
                            else:
                                doc.close()
                        except Exception as e:
                            logger.warning("pdf rasterize fallback failed: %s", e)
                except Exception as e:
                    logger.warning("file pdf local fallback failed: %s", e)
            if reply_text and reply_text.strip():
                memory.append_turn(group_id, "user", f"[file: {file_name}]")
                memory.append_turn(group_id, "bot", reply_text)
                _maybe_extract_facts(group_id)
                _reply(event.reply_token, reply_text, group_id=group_id)
            else:
                # local 也失敗（scanned PDF 抽不到 / vision LLM 掛了）才友善訊息
                _reply(event.reply_token, _quota_exhausted_message(), group_id=group_id)
            return

        # quota OK → Gemini Part bytes
        from google.genai import types as _gtypes

        parts = [
            _gtypes.Part.from_bytes(data=data, mime_type=mime_type),
            f"使用者傳了一個檔案：{file_name}。請分析其內容並回應。",
        ]
        try:
            with _thinking_indicator(group_id):
                reply_text = _llm_chat(parts, context, facts, pnotes)
        except Exception as e:
            if _is_quota_error(e):
                _mark_quota_exhausted()
            else:
                logger.exception("gemini chat (file-native) failed: %s", e)
            _reply(
                event.reply_token,
                _friendly_gemini_error(e, file_name),
                group_id=group_id,
            )
            return
        memory.append_turn(group_id, "user", f"[file: {file_name}]")
        memory.append_turn(group_id, "bot", reply_text)
        _maybe_extract_facts(group_id)
        _reply(event.reply_token, reply_text, group_id=group_id)
        return

    # ── Office 文件 → 抽文字再送 ──────────────────────────────────────────────
    if is_office:
        content = _extract_office_text(data, file_name)
        if content is None:
            _reply(
                event.reply_token,
                f"讀取 {file_name} 失敗，檔案可能損毀或格式不符。",
                group_id=group_id,
            )
            return
    else:
        # 文字檔
        content = data.decode("utf-8", errors="replace")

    original_len = len(content)
    note = ""
    if original_len > _TEXT_CHAR_LIMIT:
        content = content[:_TEXT_CHAR_LIMIT]
        note = f"\n\n(原始檔案共 {original_len:,} 字，只看前 {_TEXT_CHAR_LIMIT:,} 字)"

    prompt_text = (
        f"(使用者丟了一個檔案：{file_name}){note}\n\n"
        f"--- 內容開始 ---\n{content}\n--- 內容結束 ---\n\n請分析這個檔案的內容並回應。"
    )

    try:
        with _thinking_indicator(group_id):
            reply_text = _llm_chat(prompt_text, context, facts, pnotes)
    except Exception as e:
        if _is_quota_error(e):
            _mark_quota_exhausted()
            logger.warning("gemini chat (file) quota exhausted")
        else:
            logger.exception("gemini chat (file) failed: %s", e)
        _reply(
            event.reply_token, _friendly_gemini_error(e, file_name), group_id=group_id
        )
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
_QUOTA_STATE_FILE = os.path.join(os.path.dirname(__file__), "quota_state.json")
_quota_exhausted_until_ts: float = 0.0
_quota_notified_for_ts: float = 0.0


def _load_quota_state() -> None:
    """從磁碟還原 quota exhausted 狀態，避免重啟後重複嘗試已耗盡的 quota。"""
    global _quota_exhausted_until_ts, _quota_notified_for_ts
    try:
        with open(_QUOTA_STATE_FILE) as f:
            d = _json.load(f)
        _quota_exhausted_until_ts = float(d.get("exhausted_until_ts", 0))
        _quota_notified_for_ts = float(d.get("notified_for_ts", 0))
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("load quota state failed: %s", e)


def _save_quota_state() -> None:
    try:
        with open(_QUOTA_STATE_FILE, "w") as f:
            _json.dump(
                {
                    "exhausted_until_ts": _quota_exhausted_until_ts,
                    "notified_for_ts": _quota_notified_for_ts,
                },
                f,
            )
    except Exception as e:
        logger.warning("save quota state failed: %s", e)


def _mark_quota_exhausted() -> None:
    """記錄 Gemini quota 已爆,到下一個 00:00 PT 前都不要再打了；同時推播一次性提醒到群組。"""
    global _quota_exhausted_until_ts, _quota_notified_for_ts
    now_pt = datetime.now(tz=_PT_TZ)
    next_midnight_pt = (now_pt + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    _quota_exhausted_until_ts = next_midnight_pt.timestamp()
    gemini_client.mark_quota_exhausted_in_usage()
    _save_quota_state()
    logger.warning(
        "gemini quota marked exhausted until %s",
        next_midnight_pt.astimezone(_TW_TZ).strftime("%Y-%m-%d %H:%M TW"),
    )

    # quota 爆掉不再 push 通知群組（2026-05-06 用戶要求：不要顯示「⚠️ 今日免費額度已用完」之類訊息）
    # 只更新內部 notified_for_ts state，避免邏輯混亂；不發任何 LINE message
    if _quota_notified_for_ts != _quota_exhausted_until_ts:
        _quota_notified_for_ts = _quota_exhausted_until_ts
        _save_quota_state()


def _quota_exhausted() -> bool:
    """True = 本機記錄到 429 PerDay，且重置時間還沒到。
    只靠 Google 實際回 429 判斷，不靠計數器預測。"""
    return time.time() < _quota_exhausted_until_ts


def _quota_exhausted_message() -> str:
    """quota 爆時統一的使用者訊息(含動態台灣重置時間)。"""
    abs_str, rel_str = _next_gemini_reset_tw()
    return (
        f"Gemini 免費層今日請求額度已用完 (flash 每天 20 次)。\n"
        f"可以再使用的時間:{abs_str}(台灣時間,{rel_str})\n"
        f"想馬上恢復 → https://aistudio.google.com 綁卡開 pay-as-you-go"
    )


# ── Pending（quota 爆時的所有訊息，恢復後由 Gemini 分組 + 引用回覆） ──────────

_PENDING_EXPLICIT_PATH = os.path.join(
    os.path.dirname(__file__), "pending_explicit_reply.json"
)
_PENDING_MEDIA_DIR = os.path.join(os.path.dirname(__file__), "pending_media")
_PENDING_DLQ_PATH = os.path.join(os.path.dirname(__file__), "pending_dlq.jsonl")
_PENDING_MAX_AGE_SEC = 7 * 86400  # 7 天沒被 drain → 進 DLQ，避免 PDF/stuck entry 永久卡住


def _load_pending_explicit() -> dict:
    import pending_store as _ps
    return _ps.load()


def _save_pending_explicit_raw(data: dict) -> None:
    import pending_store as _ps
    try:
        _ps.save_full(data)
    except Exception as e:
        logger.warning("save pending raw failed: %s", str(e)[:200])


def _save_pending_any(event, group_id: str, user_id: str | None, msg) -> None:
    """quota 爆時把任何訊息（文字/檔案/音訊）存進佇列，每則獨立不合併。恢復時由 Gemini 判語意分組。"""
    try:
        data = _load_pending_explicit()
        if group_id not in data or not isinstance(data[group_id], list):
            data[group_id] = []

        entry = {
            "user_id": user_id,
            "message_id": msg.id,
            "quote_token": getattr(msg, "quote_token", None),
            "timestamp": time.time(),
        }

        if isinstance(msg, TextMessageContent):
            entry["type"] = "text"
            entry["text"] = msg.text or ""
            # 若引用他人訊息，帶上被引用的原文，讓 Gemini 恢復時有脈絡
            qid = getattr(msg, "quoted_message_id", None)
            if qid:
                raw = memory.get_raw_message(group_id, qid)
                if raw:
                    entry["quoted_original"] = raw[1]

        elif isinstance(msg, ImageMessageContent):
            entry["type"] = "image"
            try:
                content = _download_content(msg.id)
                if len(content) > _MEDIA_BYTE_LIMIT:
                    logger.info("image too large for pending: %d bytes", len(content))
                    entry["download_failed"] = True
                else:
                    os.makedirs(_PENDING_MEDIA_DIR, exist_ok=True)
                    path = os.path.join(_PENDING_MEDIA_DIR, f"{_uuid.uuid4().hex}.jpg")
                    with open(path, "wb") as f:
                        f.write(bytes(content))
                    entry["media_path"] = path
            except Exception as e:
                logger.warning("download image for pending failed: %s", e)
                entry["download_failed"] = True

        elif isinstance(msg, VideoMessageContent):
            entry["type"] = "video"
            try:
                content = _download_content(msg.id)
                if len(content) > _MEDIA_BYTE_LIMIT:
                    logger.info("video too large for pending: %d bytes", len(content))
                    entry["download_failed"] = True
                else:
                    os.makedirs(_PENDING_MEDIA_DIR, exist_ok=True)
                    path = os.path.join(_PENDING_MEDIA_DIR, f"{_uuid.uuid4().hex}.mp4")
                    with open(path, "wb") as f:
                        f.write(bytes(content))
                    entry["media_path"] = path
            except Exception as e:
                logger.warning("download video for pending failed: %s", e)
                entry["download_failed"] = True

        elif isinstance(msg, FileMessageContent):
            entry["type"] = "file"
            entry["file_name"] = getattr(msg, "file_name", "unknown")
            try:
                content = _download_content(msg.id)
                os.makedirs(_PENDING_MEDIA_DIR, exist_ok=True)
                path = os.path.join(_PENDING_MEDIA_DIR, f"{_uuid.uuid4().hex}.bin")
                with open(path, "wb") as f:
                    f.write(bytes(content))
                entry["media_path"] = path
            except Exception as e:
                logger.warning("download file for pending failed: %s", e)
                entry["download_failed"] = True

        elif isinstance(msg, AudioMessageContent):
            entry["type"] = "audio"
            try:
                content = _download_content(msg.id)
                os.makedirs(_PENDING_MEDIA_DIR, exist_ok=True)
                path = os.path.join(_PENDING_MEDIA_DIR, f"{_uuid.uuid4().hex}.m4a")
                with open(path, "wb") as f:
                    f.write(bytes(content))
                entry["media_path"] = path
                entry["mime_type"] = "audio/m4a"
            except Exception as e:
                logger.warning("download audio for pending failed: %s", e)
                entry["download_failed"] = True
        else:
            return

        data[group_id].append(entry)
        _save_pending_explicit_raw(data)
    except Exception as e:
        logger.warning("save pending any failed: %s", str(e)[:200])


def _clear_pending_explicit(group_id: str) -> None:
    data = _load_pending_explicit()
    # 清掉該 group 所有 media 檔
    for entry in data.get(group_id, []):
        path = entry.get("media_path")
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
    data.pop(group_id, None)
    _save_pending_explicit_raw(data)


def _commit_pending_removal(group_id: str, msg_ids: list[str]) -> int:
    """Remove specified message_ids from group pending list, atomic save.
    回 removed 數。給 peek-then-confirm pattern 用 (§3 GP1 C5)。
    """
    if not msg_ids:
        return 0
    msg_id_set = set(msg_ids)
    data = _load_pending_explicit()
    items = data.get(group_id, [])
    new_items = [it for it in items if it.get("message_id") not in msg_id_set]
    removed = len(items) - len(new_items)
    if removed == 0:
        return 0
    if not new_items:
        data.pop(group_id, None)
    else:
        data[group_id] = new_items
    _save_pending_explicit_raw(data)
    return removed


def _dlq_entry(group_id: str, entry: dict, reason: str) -> None:
    """Append-only DLQ: 一行一筆 JSON。永遠不再 retry，供之後 forensic 查看。"""
    record = {
        "group_id": group_id,
        "reason": reason,
        "dlq_at": time.time(),
        "entry": entry,
    }
    try:
        with open(_PENDING_DLQ_PATH, "a", encoding="utf-8") as f:
            f.write(_json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("DLQ append failed: %s", str(e)[:200])


def _drop_stale_pending(group_id: str, max_age_sec: int = _PENDING_MAX_AGE_SEC) -> int:
    """掃 pending 把 timestamp 超齡的 entry 移到 DLQ + 刪 media file，回 dropped 數。

    保護機制：avoid PDF / non-text / failed-LLM entry 永久卡住消耗 piggyback slot。
    timestamp 缺失的舊 entry 視為 now（不誤刪）。
    """
    data = _load_pending_explicit()
    items = data.get(group_id, [])
    if not items:
        return 0
    now = time.time()
    keep, drop = [], []
    for it in items:
        ts = it.get("timestamp")
        if ts is None or (now - ts) <= max_age_sec:
            keep.append(it)
        else:
            drop.append(it)
    if not drop:
        return 0
    for it in drop:
        _dlq_entry(group_id, it, reason=f"age>{max_age_sec}s")
        path = it.get("media_path")
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
    data[group_id] = keep
    if not keep:
        data.pop(group_id, None)
    _save_pending_explicit_raw(data)
    logger.info("DLQ drop: group=%s dropped=%d kept=%d", group_id, len(drop), len(keep))
    return len(drop)


def _remove_pending_by_msg_id(group_id: str, message_id: str) -> bool:
    """Idempotent removal of a single pending entry by message_id. Goes through
    pending_store (fcntl + RLock + atomic) so safe across handler thread + cron
    subprocess. Returns whether something was removed."""
    try:
        import pending_store as _ps
        return _ps.remove_by_message_id(group_id, message_id)
    except Exception as e:
        logger.warning("remove pending by msg_id failed: %s", str(e)[:200])
        return False


def _heuristic_group_messages(items: list[dict]) -> list[dict]:
    """Fallback：每則各自一組。"""
    return [{"idxs": [i], "reply_to": i} for i in range(len(items))]


def _gemini_group_messages(items: list[dict]) -> list[dict]:
    """讓 Gemini 依內容判斷哪些訊息屬於同一話題/討論串，決定分組與回覆目標。
    回傳：[{"idxs": [int,...], "reply_to": int}, ...]。失敗退回每則各自一組。"""
    if not items:
        return []
    try:
        from google.genai import types

        client = gemini_client._client
        lines = []
        for i, it in enumerate(items):
            from datetime import datetime as _dt

            ts = it.get("timestamp", 0)
            ts_str = _dt.fromtimestamp(ts).strftime("%H:%M") if ts else "??"
            who = (it.get("user_id") or "?")[:8]
            t = it.get("type", "text")
            content = it.get("text", "")
            if t == "file":
                content = f"[檔案: {it.get('file_name', '')}]"
            elif t == "audio":
                content = "[語音留言]"
            lines.append(f"[{i}] {ts_str} ({who}) {content[:200]}")

        prompt = (
            "以下是 LINE 群組在額度耗盡期間積累的訊息（依時間順序，格式：[索引] 時間 (用戶) 內容）。\n"
            "你的任務：把這些訊息分組，每組對應「值得單獨回覆一次」的內容。\n\n"
            "分組規則：\n"
            "1. 同一人連續發的多則訊息，若講的是同一件事（只是分段打），合為一組\n"
            "2. 不同人在討論同一個話題（你問我答、辯論、補充），合為一組\n"
            "3. 話題明顯轉換（新主題、新問題、無關內容）→ 新的一組\n"
            "4. 純閒聊回應（『哈哈』『好喔』『讚』等）可以單獨一組，也可以和觸發它的那則合併\n"
            "5. 一組不宜超過 8 則，若同人連說了很多且話題明顯轉移，請適時切開\n\n"
            "reply_to：從每組中選一則最具代表性的（最能讓回覆有所依附），必須是該組的索引之一。\n\n"
            "訊息列表：\n"
            + "\n".join(lines)
            + "\n\n只回傳 JSON，不要說明（每個索引恰好出現一次）：\n"
            '{"groups":[{"idxs":[int,...], "reply_to": int}, ...]}'
        )
        # 優先用 flash（分組更準確），quota 爆時降回 flash-lite
        group_model = (
            settings.gemini_model
            if not _quota_exhausted()
            else settings.gemini_light_model
        )
        resp = client.models.generate_content(
            model=group_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        data = _json.loads(resp.text or "")
        groups_raw = data.get("groups", [])
        seen: set[int] = set()
        clean: list[dict] = []
        for g in groups_raw:
            idxs = g.get("idxs") if isinstance(g, dict) else None
            if not isinstance(idxs, list):
                continue
            ok = [
                i
                for i in idxs
                if isinstance(i, int) and 0 <= i < len(items) and i not in seen
            ]
            if not ok:
                continue
            seen.update(ok)
            reply_to = g.get("reply_to")
            if not isinstance(reply_to, int) or reply_to not in ok:
                reply_to = max(ok, key=lambda i: len(items[i].get("text") or ""))
            clean.append({"idxs": ok, "reply_to": reply_to})
        for i in range(len(items)):
            if i not in seen:
                clean.append({"idxs": [i], "reply_to": i})
        logger.info("group gemini: %d items → %d groups", len(items), len(clean))
        return clean
    except Exception as e:
        logger.warning("gemini group failed, fallback to heuristic: %s", str(e)[:200])
        return _heuristic_group_messages(items)


def _build_group_parts(items: list[dict], group_id: str) -> list:
    """把一組 pending 訊息合成 Gemini parts（文字 + 檔案/音訊 bytes）。"""
    from google.genai import types

    parts: list[object] = []
    texts = []
    for it in items:
        t = it.get("type", "text")
        if t == "text":
            txt = it.get("text", "")
            quoted_original = it.get("quoted_original")
            if quoted_original:
                texts.append(f"(引用了：『{quoted_original[:80]}』)\n{txt}")
            else:
                texts.append(txt)
        elif t == "file":
            path = it.get("media_path")
            fname = it.get("file_name", "unknown")
            if path and os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                    mime, _ = mimetypes.guess_type(fname)
                    parts.append(
                        types.Part.from_bytes(
                            data=data, mime_type=mime or "application/octet-stream"
                        )
                    )
                    texts.append(f"(使用者傳了檔案：{fname}，請分析其內容)")
                except Exception as e:
                    logger.warning("read pending file failed: %s", e)
                    texts.append(f"(使用者傳了檔案 {fname}，但讀取失敗)")
            else:
                texts.append(f"(使用者傳了檔案 {fname}，但原始內容已遺失)")
        elif t == "audio":
            path = it.get("media_path")
            mime = it.get("mime_type", "audio/m4a")
            if path and os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                    parts.append(types.Part.from_bytes(data=data, mime_type=mime))
                    texts.append("(使用者傳了語音，請先完整轉寫再回應)")
                except Exception as e:
                    logger.warning("read pending audio failed: %s", e)
                    texts.append("(使用者傳了語音但讀取失敗)")
            else:
                texts.append("(使用者傳了語音但原始內容已遺失)")

    combined_text = "\n".join(texts).strip()
    if combined_text:
        # URL 預抓
        combined_text = _prefetch_urls(combined_text)
        parts.append(combined_text)
    return parts


_GLOBAL_GATE_CACHE_TTL_SEC = 60                                # LINE quota API call 60s 內共用結果
_global_gate_cache: tuple[float, bool] | None = None
_global_gate_cache_lock = threading.Lock()


def _global_pending_drain_ready() -> bool:
    """Drain pending 前的全域 gate：mute / Gemini quota / LINE 月額度。

    True 才可以動。三個 caller（startup / retry worker / piggyback）共用。
    Fail-closed：API check 失敗一律回 False，避免不確定狀態白燒 Gemini。
    60 秒 cache 防 cross-group webhook flood 放大 LINE API 呼叫次數。
    """
    global _global_gate_cache
    now = time.time()
    with _global_gate_cache_lock:
        if _global_gate_cache and now - _global_gate_cache[0] < _GLOBAL_GATE_CACHE_TTL_SEC:
            return _global_gate_cache[1]

    _load_quota_state()
    if settings.bot_muted:
        with _global_gate_cache_lock:
            _global_gate_cache = (now, False)
        return False
    if _quota_exhausted():
        logger.info("drain pending: Gemini exhausted, defer")
        with _global_gate_cache_lock:
            _global_gate_cache = (now, False)
        return False

    try:
        import requests as _req
        from line_token_refresh import get_line_token as _glt
        _tok = _glt()
        if not _tok:
            logger.warning("drain pending: no LINE token, fail-closed skip")
            with _global_gate_cache_lock:
                _global_gate_cache = (now, False)
            return False
        _h = {"Authorization": f"Bearer {_tok}"}
        _ru = _req.get(
            "https://api.line.me/v2/bot/message/quota/consumption",
            headers=_h, timeout=5,
        )
        _rl = _req.get(
            "https://api.line.me/v2/bot/message/quota",
            headers=_h, timeout=5,
        )
        if not (_ru.ok and _rl.ok):
            logger.warning(
                "drain pending: LINE quota API non-200 (used=%s limit=%s), fail-closed skip",
                _ru.status_code, _rl.status_code,
            )
            with _global_gate_cache_lock:
                _global_gate_cache = (now, False)
            return False
        _used = _ru.json().get("totalUsage", 0)
        _limit = _rl.json().get("value", 200)
        if _used >= _limit:
            logger.info(
                "drain pending: LINE quota %d/%d exhausted, defer",
                _used, _limit,
            )
            with _global_gate_cache_lock:
                _global_gate_cache = (now, False)
            return False
    except Exception as _e:
        logger.warning(
            "drain pending: LINE quota precheck failed, fail-closed skip: %s",
            str(_e)[:120],
        )
        with _global_gate_cache_lock:
            _global_gate_cache = (now, False)
        return False

    with _global_gate_cache_lock:
        _global_gate_cache = (now, True)
    return True


# Per-group drain lock：三 caller（startup / retry / piggyback）共用，避免同 group 兩個 thread
# 同時 drain 重複 push（GP1 important #3 race）。lock acquire 失敗 = 已被別人在 drain → caller skip。
_drain_locks: dict[str, threading.Lock] = {}
_drain_lock_factory_lock = threading.Lock()


def _try_acquire_drain_slot(group_id: str) -> threading.Lock | None:
    """non-blocking acquire 該 group 的 drain lock。拿到回 lock object，拿不到回 None。"""
    with _drain_lock_factory_lock:
        if group_id not in _drain_locks:
            _drain_locks[group_id] = threading.Lock()
        lock = _drain_locks[group_id]
    return lock if lock.acquire(blocking=False) else None


def _drain_pending_for_group(group_id: str, source: str = "startup") -> bool:
    """處理單一 group 的 pending：分組 → 逐組 LLM 回覆 → 引用推送。

    呼叫前要先 _global_pending_drain_ready() == True。
    source ∈ {"startup", "retry_worker", "piggyback"}，僅用於 log。
    回傳 True 表示有試著 drain（不論成功失敗），False 表示因另一 caller 已在 drain 同 group
    而 skip — 用於 piggyback 判斷要不要寫 throttle ts。
    Gemini 中途重新爆走 _mark_quota_exhausted + 將未處理 items 寫回 pending（不 raise），
    caller 需自己重查 `_quota_exhausted()` 決定要不要繼續下一 group（startup wrapper 有做）。
    """
    slot = _try_acquire_drain_slot(group_id)
    if slot is None:
        logger.info("%s: skip, another drain in progress for group=%s", source, group_id)
        return False
    try:
        pending = _load_pending_explicit()
        items = pending.get(group_id, [])
        if isinstance(items, dict):  # 舊格式相容
            items = [items]

        # D1 TTL: drain 前先清超齡 entry 到 DLQ
        dropped = _drop_stale_pending(group_id)
        if dropped:
            pending = _load_pending_explicit()
            items = pending.get(group_id, [])
        if not items:
            _clear_pending_explicit(group_id)
            return True

        # __bot__ 條目不應出現在 pending（recovery 污染防護）
        items = [it for it in items if it.get("user_id") != "__bot__"]
        if not items:
            _clear_pending_explicit(group_id)
            return True

        groups = _gemini_group_messages(items)
        logger.info(
            "%s: group=%s items=%d groups=%d", source, group_id, len(items), len(groups)
        )

        processed_idx = set()
        try:
            for g in groups:
                idxs = g["idxs"]
                reply_to_idx = g["reply_to"]
                group_items = [items[i] for i in idxs]
                parts = _build_group_parts(group_items, group_id)
                if not parts:
                    processed_idx.update(idxs)
                    continue

                context = memory.get_context(group_id)
                facts = memory.top_facts(group_id)
                pnotes = _get_persona_notes(group_id)
                reply_text = _llm_chat(parts, context, facts, pnotes)

                text = _md_to_line(reply_text)
                footer = _get_quota_footer()
                text = text[: 4900 - len(footer)] + footer

                msg_kwargs = {"text": text}
                qt = items[reply_to_idx].get("quote_token")
                if qt:
                    msg_kwargs["quote_token"] = qt

                with ApiClient(_get_line_config()) as api_client:
                    MessagingApi(api_client).push_message(
                        PushMessageRequest(
                            to=group_id,
                            messages=[TextMessage(**msg_kwargs)],
                        )
                    )
                bot_stats.track_line_push()
                memory.append_turn(
                    group_id,
                    "user",
                    "\n".join(
                        it.get("text", "")
                        for it in group_items
                        if it.get("type") == "text"
                    )[:500]
                    or "[非文字訊息]",
                )
                memory.append_turn(group_id, "bot", reply_text)
                processed_idx.update(idxs)
        except Exception as e:
            remaining = [it for i, it in enumerate(items) if i not in processed_idx]
            if _is_quota_error(e):
                _mark_quota_exhausted()
                data = _load_pending_explicit()
                data[group_id] = remaining
                _save_pending_explicit_raw(data)
                logger.warning(
                    "%s pending: quota re-exhausted, saved %d remaining for group=%s",
                    source, len(remaining), group_id,
                )
                return True
            err_str = str(e)
            if remaining:
                data = _load_pending_explicit()
                data[group_id] = remaining
                _save_pending_explicit_raw(data)
                logger.warning(
                    "%s pending: push failed (%s), saved %d remaining for group=%s",
                    source, err_str[:80], len(remaining), group_id,
                )
            else:
                logger.exception(
                    "%s pending reply failed for group=%s: %s", source, group_id, e
                )
            return True

        _clear_pending_explicit(group_id)
        logger.info("%s: group=%s all pending cleared", source, group_id)
        return True
    finally:
        slot.release()


@app.on_event("startup")
def _process_pending_on_startup() -> None:
    """uvicorn 啟動時處理所有 pending：thin wrapper，所有實作在 helper 裡。"""
    pending = _load_pending_explicit()
    if not pending:
        return
    if not _global_pending_drain_ready():
        return
    for group_id in list(pending.keys()):
        # Gemini 中途又爆，後面的 group 直接放棄這輪
        if _quota_exhausted():
            return
        _drain_pending_for_group(group_id, source="startup")


_PENDING_RETRY_INTERVAL_SEC = 6 * 60 * 60        # 6 小時跑一次（從 30 min 降頻，避免吃光每日 quota）
_PENDING_RETRY_QUOTA_RESERVE = 0.40              # 至少留 40% quota 給新訊息（不讓 retry 把 quota 全吃光）
_PIGGYBACK_DRAIN_THROTTLE_SEC = 30 * 60          # 同 group piggyback drain 冷卻時間（成功一次後）
_last_piggyback_drain_ts: dict[str, float] = {}  # group_id → 上次 piggyback drain epoch
_piggyback_drain_lock = threading.Lock()
# ThreadPoolExecutor 限 piggyback 並行度 = 2，防 webhook 洪水製造無限 thread（GP2 critical #1）。
# 啟動時建立 — 因為 ThreadPoolExecutor 在 import 時間建會在 multiprocessing fork 行為下有問題，
# 但這 module 不被 fork，所以 module-level 建構安全；放在這裡讓 piggyback 函式都看得到。
from concurrent.futures import ThreadPoolExecutor as _PiggybackTPE  # noqa: E402
_PIGGYBACK_EXECUTOR = _PiggybackTPE(max_workers=2, thread_name_prefix="piggyback")


def _has_enough_quota_for_retry() -> bool:
    """retry 前先檢查：今日 quota 用了 ≥ 60% 就停（保留 40% 給新訊息）。"""
    info = gemini_client.get_gemini_quota_info()
    if info is None:
        return True
    used_ratio = info["used_requests"] / max(info["limit_requests"], 1)
    return used_ratio < (1.0 - _PENDING_RETRY_QUOTA_RESERVE)


def _piggyback_drain_pending(group_id: str) -> None:
    """webhook 收到該 group 訊息時呼叫；補該 group 的 pending。

    Gates 順序（任一不過就跳過，且**不寫 throttle ts**，下次 webhook 立刻可再試）：
      1. throttle：同 group 30 分鐘內已成功 drain
      2. 該 group 沒 pending（fast-path bail，省 quota gate）
      3. retry quota gate（用量 ≥ 60% 保 40% 給新訊息）
      4. 全域 gate（mute / Gemini quota / LINE quota，含 60s cache）
      5. per-group drain lock（拿不到 = retry worker / startup 正在 drain 同 group）

    Throttle ts **僅在 _drain_pending_for_group 回傳 True 後寫**，避免 gate fail 燒掉
    30 分鐘冷卻（GP1 critical #1）。
    """
    now = time.time()
    with _piggyback_drain_lock:
        last = _last_piggyback_drain_ts.get(group_id, 0.0)
        if now - last < _PIGGYBACK_DRAIN_THROTTLE_SEC:
            return

    try:
        pending = _load_pending_explicit()
        if group_id not in pending or not pending[group_id]:
            return
        if not _has_enough_quota_for_retry():
            logger.info(
                "piggyback drain: quota usage > 60%%, skip group=%s", group_id
            )
            return
        if not _global_pending_drain_ready():
            return
        logger.info("piggyback drain: triggered by message in group=%s", group_id)
        attempted = _drain_pending_for_group(group_id, source="piggyback")
        if attempted:
            with _piggyback_drain_lock:
                _last_piggyback_drain_ts[group_id] = time.time()
    except Exception as e:
        logger.exception("piggyback drain failed for group=%s: %s", group_id, e)


def _spawn_piggyback_drain(group_id: str) -> None:
    """ThreadPoolExecutor submit piggyback drain，不阻塞 webhook。

    Fast-path bail：若 group 仍在 30 分鐘 throttle 內就不 submit，省下 executor 排隊壓力
    （webhook 洪水時 throttle 一定先 hit）。Executor cap=2 保護 cross-group flood。
    """
    if not group_id:
        return
    with _piggyback_drain_lock:
        last = _last_piggyback_drain_ts.get(group_id, 0.0)
        if time.time() - last < _PIGGYBACK_DRAIN_THROTTLE_SEC:
            return
    try:
        _PIGGYBACK_EXECUTOR.submit(_piggyback_drain_pending, group_id)
    except RuntimeError:
        # Executor shutdown（process 在 teardown）— drop，next restart 會清 pending
        pass


def _start_pending_retry_worker() -> None:
    """背景執行緒定期重試 pending；加 quota gate 避免吃光每日額度。"""
    import threading as _threading

    def _worker():
        while True:
            _threading.Event().wait(_PENDING_RETRY_INTERVAL_SEC)
            if not _load_pending_explicit():
                continue
            if _quota_exhausted():
                continue
            if not _has_enough_quota_for_retry():
                logger.info("pending retry worker: quota usage > 60%%, skip to preserve for new messages")
                continue
            logger.info("pending retry worker: quota available + sufficient, processing pending")
            try:
                _process_pending_on_startup()
            except Exception as e:
                logger.exception("pending retry worker failed: %s", e)

    t = _threading.Thread(target=_worker, daemon=True, name="pending-retry")
    t.start()


@app.on_event("startup")
def _init_on_startup() -> None:
    _load_quota_state()
    _start_pending_retry_worker()


@contextmanager
def _thinking_indicator(group_id: str | None, delay: float = 3.0):
    yield


def _get_persona_notes(group_id: str) -> list[dict]:
    """取出 persona notes，供 gemini_client.chat() 注入 system prompt。"""
    return memory.list_persona_notes(group_id)


# 糾正偵測：使用者 @mention bot 時如果內容像糾正，自動存下來
_CORRECTION_KEYWORDS = (
    "不要",
    "不准",
    "別再",
    "下次",
    "記住",
    "以後",
    "不可以",
    "禁止",
    "不能",
    "改掉",
    "不用",
)


def _try_save_correction(group_id: str, user_text: str) -> None:
    """如果 user_text 看起來像是在糾正 bot 的行為，存成 persona correction。"""
    t = user_text.strip()
    if len(t) < 4 or len(t) > 100:
        return
    if any(kw in t for kw in _CORRECTION_KEYWORDS):
        memory.add_persona_note(group_id, "correction", "使用者糾正", t)
        logger.info("persona correction saved: %s", t[:60])


# ── Organic 糾正偵測（2026-05-08 加）────────────────────────────────────────
# user 主動講「不對 / 你誤會 / 我意思不是這個」時，把上一輪 user msg + bot reply
# + 這次糾正三段拼起來寫進 persona_notes，source='organic'，下一輪 prompt
# 就會把它當 negative example 放在規則 0 延伸區。
#
# 跟 _CORRECTION_KEYWORDS 不同：
# - _CORRECTION_KEYWORDS 抓「未來規則」（不要 X、別再 X、以後 X）
# - _ORGANIC_CORRECTION_KEYWORDS 抓「即時糾正上一輪回覆錯了」
_ORGANIC_CORRECTION_KEYWORDS = (
    "不對",
    "不是這樣",
    "不是這意思",
    "你誤會",
    "妳誤會",
    "我說的是",
    "我問的是",
    "我是說",
    "我意思是",
    "我意思不是",
    "重來",
    "請重答",
    "你答錯",
    "妳答錯",
    "答錯了",
    "胡說",
    "亂講",
    "不是我要的",
)


def _weak_correction_signals(
    text: str, prev_user_msg: str, prev_bot_msg: str
) -> dict:
    """除了強 keyword 以外的 5 種弱糾正信號 — 自動判斷不靠 user 配合特定詞。

    Signals:
      - opening_neg: 短訊息 (< 30 字) AND 開頭「不/沒/錯」
      - corrective_marker: 含「應該/才對/才是/正確的是」
      - sarcasm: 含「呵呵 / ㄏㄏ / ... / 。。。 / 笑死 / ｗ」
      - repeat_question: 跟前一輪 user 訊息 token jaccard > 0.5（暗示重述）
      - contrastive_negation: 「不是 X 是 Y」型句式
      - negative_sentiment: chinese_nlp.detect_sentiment 判定負面

    回 {score: int, signals: list[str]}。score >= 2 → 視為 correction。
    """
    signals: list[str] = []
    t = text.strip()
    if len(t) < 30 and t[:1] in ("不", "沒", "錯"):
        signals.append("opening_neg")
    if any(p in t for p in ("應該", "才對", "才是", "正確的是", "明明")):
        signals.append("corrective_marker")
    if any(p in t for p in ("呵呵", "ㄏㄏ", "...", "。。。", "ｗｗ", "wwww", "笑死")):
        signals.append("sarcasm")
    if "不是" in t and "是" in t.replace("不是", "", 1):
        signals.append("contrastive_negation")
    # repeat_question (跟前一輪 user 訊息 jaccard)
    if prev_user_msg:
        try:
            from chinese_nlp import tokenize
            now = set(tokenize(t))
            prev = set(tokenize(prev_user_msg))
            if now and prev and len(now & prev) / len(now | prev) > 0.5:
                signals.append("repeat_question")
        except Exception:
            pass
    # 負面情感
    try:
        from chinese_nlp import detect_sentiment
        if detect_sentiment(t) == "negative":
            signals.append("negative_sentiment")
    except Exception:
        pass
    return {"score": len(signals), "signals": signals}


def _detect_user_correction(
    text: str, group_id: str, sender_user_id: str = ""
) -> bool:
    """偵測 user 訊息是否在糾正 bot 上一輪回覆。命中 → 寫進 organic correction。

    Layer 1（強 keyword）：17 條糾正詞之一命中 → 直接視為 correction
    Layer 2（弱信號）：6 種弱信號（情感 / 重述 / 否定句式 / 嘲諷 / corrective marker / opening neg）
                     score >= 2 → 視為 correction
    寫進 persona_notes（kind='correction', source='organic'）。
    任何步驟例外都吞掉、回 False，主流程不受影響。
    """
    try:
        t = (text or "").strip()
        if len(t) < 2 or len(t) > 200:
            return False

        # 抓上一輪 user / bot 訊息
        ctx = memory.get_context(group_id)
        prev_user_msg = ""
        prev_bot_msg = ""
        # 由新→舊找最近一筆 bot，再抓它前面最近一筆 user
        last_bot_idx = None
        for i in range(len(ctx) - 1, -1, -1):
            if ctx[i][0] == "bot":
                last_bot_idx = i
                break
        if last_bot_idx is not None:
            prev_bot_msg = ctx[last_bot_idx][1] or ""
            for j in range(last_bot_idx - 1, -1, -1):
                if ctx[j][0] == "user":
                    prev_user_msg = ctx[j][1] or ""
                    break

        # 沒有上一輪 bot reply 可糾正 → 跳過（user 可能在糾正其他人類發言）
        if not prev_bot_msg:
            return False

        # Layer 1：強 keyword
        hit_keyword = any(kw in t for kw in _ORGANIC_CORRECTION_KEYWORDS)
        # Layer 2：弱信號 score >= 2
        weak = _weak_correction_signals(t, prev_user_msg, prev_bot_msg)
        is_correction = hit_keyword or weak["score"] >= 2

        if not is_correction:
            return False

        logger.info(
            "organic correction detected (keyword=%s, weak_signals=%s, text=%r)",
            hit_keyword, weak.get("signals", []), t[:60],
        )

        # 嘗試用 light Gemini call 抽一句話總結 — 失敗就直接存 raw
        summary = _summarize_correction(prev_user_msg, prev_bot_msg, t)

        note_id = memory.add_organic_correction(
            group_id=group_id,
            prev_user_msg=prev_user_msg,
            prev_bot_msg=prev_bot_msg,
            correction_msg=t,
            summary=summary,
        )
        logger.info(
            "organic correction saved (group=%s, note_id=%s, summary=%r, "
            "correction=%r)",
            group_id, note_id, (summary or "")[:50], t[:60],
        )
        return True
    except Exception as e:
        logger.warning("_detect_user_correction failed: %s", e)
        return False


def _summarize_correction(
    prev_user_msg: str, prev_bot_msg: str, correction_msg: str
) -> str:
    """用 Gemini 一句話總結「bot 具體做錯什麼」。quota 爆 / 失敗回空字串。

    刻意 light call：不過 system prompt、不過完整 chat 流程，
    避免占用主對話 quota 預算。失敗 silent，caller 直接存 raw 即可。
    """
    if _quota_exhausted():
        return ""
    try:
        prompt = (
            "下面是一段 LINE 群對話。user 在最後一句糾正 bot 的回覆。"
            "請用一句話（25 字內、繁體中文）總結 bot 到底做錯什麼，"
            "讓 bot 下次不要再犯。直接給結論，不要前綴。\n\n"
            f"user 原問：{(prev_user_msg or '')[:200]}\n"
            f"bot 答：{(prev_bot_msg or '')[:200]}\n"
            f"user 糾正：{(correction_msg or '')[:200]}"
        )
        out = gemini_client.chat(prompt, [], [])
        if not out:
            return ""
        # 截到單句、去除多餘換行
        line = out.strip().splitlines()[0].strip() if out.strip() else ""
        return line[:120]
    except Exception as e:
        logger.warning("_summarize_correction failed (silent): %s", e)
        return ""


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
        # 分鐘級限制（transient）→ 靜默，不告知使用者
        return ""
    if "401" in err_str or "403" in err_str:
        return "Gemini API key 有問題,請檢查設定。"
    if "400" in err_str:
        return f"Gemini 說這個輸入有問題:{err_str[:200]}"
    if (
        "500" in err_str
        or "503" in err_str
        or "UNAVAILABLE" in err_str
        or "Server disconnected" in err_str
        or "RemoteProtocolError" in type(e).__name__
        or "Connection reset" in err_str
        or "ReadTimeout" in err_str
    ):
        return "Gemini 那邊暫時斷線,等一下再試。"
    return f"分析失敗:{type(e).__name__}"


def _maybe_extract_facts(group_id: str, user_id: str = "") -> None:
    """每 N 輪抽一次長期事實，user_id 有值時存為 per-user 事實。"""
    if not memory.bump_and_should_extract(group_id):
        return
    new_facts = gemini_client.extract_facts(memory.get_context(group_id))
    added = 0
    for f in new_facts:
        if memory.add_fact(group_id, f, user_id=user_id):
            added += 1
    logger.info(
        "auto-extracted facts: %d new (total=%d)",
        added,
        len(memory.list_facts(group_id)),
    )


# ── 自動偵測 reminder（2026-05-08 加，用戶要求自動記住日常事項）────────────

# 候選訊息 hint：含「日期 keyword」+「時間 keyword」才送 Gemini，省 quota
_REMINDER_DATE_HINT = re.compile(
    r"(\d+\s*月\s*\d+|\d+/\d+|\d+號|\d+日|"
    r"今天|今晚|明天|明日|明晚|後天|大後天|"
    r"星期[一二三四五六日天]|週[一二三四五六日]|"
    r"下\s*(週|周|星期|月)|這\s*(週|周|星期))"
)
_REMINDER_TIME_HINT = re.compile(
    r"(\d+\s*[:：]\s*\d+|\d+\s*點|"
    r"早上|上午|中午|下午|傍晚|晚上|凌晨|半夜|"
    r"AM|PM|am|pm)"
)


def _maybe_extract_reminder(text: str, group_id: str, user_id: str = "") -> None:
    """偵測 user 訊息中的時間性提醒事項，存進 reminders table。失敗 silent。

    流程：regex pre-filter → Gemini light 抽取 → memory.add_reminder
    """
    if not text or len(text) > 500:
        return
    # 必須同時有日期 hint + 時間 hint，才送 Gemini（避免每訊息都打）
    if not (_REMINDER_DATE_HINT.search(text) and _REMINDER_TIME_HINT.search(text)):
        return

    try:
        result = gemini_client.extract_reminder(text)
        if result is None:
            return
        # 算 remind_at（local timezone）
        from datetime import datetime as _dt
        try:
            remind_dt = _dt(
                int(result["year"]),
                int(result["month"]),
                int(result["day"]),
                int(result["hour"]),
                int(result["minute"]),
            )
        except (ValueError, KeyError, TypeError) as e:
            logger.info("reminder datetime parse failed: %s, result=%s", e, result)
            return
        remind_at = int(remind_dt.timestamp())
        # 過去事件不存（已過 1 hr 以上）
        if remind_at < _dt.now().timestamp() - 3600:
            return
        rid = memory.add_reminder(
            group_id, user_id, result["action"], remind_at, source_text=text[:200]
        )
        if rid:
            logger.info(
                "reminder saved: rid=%d action=%r at=%s",
                rid, result["action"], remind_dt.strftime("%Y-%m-%d %H:%M"),
            )
    except Exception as e:
        logger.warning("_maybe_extract_reminder failed: %s", e)


# ── Join / Leave 處理 ────────────────────────────────────────────────────────


def _handle_join(event: JoinEvent) -> None:
    """Bot 被加入群組時觸發。立即 reply + 查群組資訊。
    目前問題：LINE 會在 < 1 秒內把 bot 踢出，所以要搶時間收集資訊。"""
    src = event.source
    group_id = getattr(src, "group_id", None)
    room_id = getattr(src, "room_id", None)
    target_id = group_id or room_id
    source_type = "group" if group_id else ("room" if room_id else "unknown")
    print(
        f"[JOIN] source_type={source_type} id={target_id} reply_token={event.reply_token}",
        flush=True,
    )

    # 1. 立即 reply 一則 welcome 訊息（搶在被踢之前）
    try:
        _reply(event.reply_token, f"我被加入了！{source_type}_id={target_id}")
        print("[JOIN] reply sent", flush=True)
    except Exception as e:
        print(f"[JOIN] reply FAILED: {e}", flush=True)

    # 2. 立即查群組 summary / member count / member ids（可能因未認證而失敗，但試試）
    if group_id:
        try:
            with ApiClient(_get_line_config()) as api_client:
                api = MessagingApi(api_client)
                try:
                    summary = api.get_group_summary(group_id)
                    print(f"[JOIN] group_summary={summary}", flush=True)
                except Exception as e:
                    print(
                        f"[JOIN] group_summary FAILED: {type(e).__name__}: {str(e)[:200]}",
                        flush=True,
                    )
                try:
                    count = api.get_group_member_count(group_id)
                    print(f"[JOIN] group_member_count={count}", flush=True)
                except Exception as e:
                    print(
                        f"[JOIN] group_member_count FAILED: {type(e).__name__}: {str(e)[:200]}",
                        flush=True,
                    )
                try:
                    ids = api.get_group_members_ids(group_id)
                    print(f"[JOIN] group_members_ids={ids}", flush=True)
                except Exception as e:
                    print(
                        f"[JOIN] group_members_ids FAILED: {type(e).__name__}: {str(e)[:200]}",
                        flush=True,
                    )
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

_DINNER_KEYWORDS = [
    "晚餐吃什麼",
    "晚餐吃哪",
    "吃什麼晚餐",
    "晚餐去哪",
    "晚餐要吃什麼",
    "今晚吃什麼",
    "今天吃什麼",
]


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
    # 2026-05-16 改：刪 quota 短路。dinner prompt 是純 text，llm_router.fallback_chat
    # 4-tier waterfall (local LLM → RAG → lite_reply) 能處理；對齊
    # feedback_quota_fallback_never_skip.md。注意 file handler 因含 PDF/image bytes、
    # _extract_text 會丟，仍保留短路 + 友善訊息（known exception）。
    context = memory.get_context(group_id)
    facts = memory.top_facts(group_id)
    pnotes = _get_persona_notes(group_id)
    try:
        with _thinking_indicator(group_id):
            reply_text = _llm_chat(_DINNER_PROMPT, context, facts, pnotes)
    except Exception as e:
        if _is_quota_error(e):
            _mark_quota_exhausted()
            logger.warning("dinner recommendation quota exhausted")
            _reply(event.reply_token, _quota_exhausted_message(), group_id=group_id)
        else:
            logger.exception("dinner recommendation failed: %s", e)
            _reply(event.reply_token, _friendly_gemini_error(e), group_id=group_id)
        return
    if not reply_text or not reply_text.strip():
        # fallback_chat 全敗回空 → 給使用者明確訊息（不能送空到 LINE SDK）
        _reply(event.reply_token, "晚餐推薦今天罷工了，等一下再試試", group_id=group_id)
        return
    _reply(event.reply_token, reply_text, group_id=group_id)


_CLASSIFY_EMOJIS = {
    "財經": "💰", "健康": "🏥", "警示": "🚨", "影片": "🎬", "行程": "📅", "其他": "📌",
}
_CLASSIFY_CMD_TO_CAT = {
    "/今日財經": "財經",
    "/今日健康": "健康",
    "/今日警示": "警示",
    "/今日影片": "影片",
    "/今日行程": "行程",
    "/今日其他": "其他",
}


def _handle_classify_command(group_id: str, text: str) -> str | None:
    """處理 /分類 與 /今日X 系列指令；無命中回 None。2026-05-10 加。"""
    t = text.strip()
    if t not in _CLASSIFY_CMD_TO_CAT and t != "/分類":
        return None
    try:
        import message_classifier
        import time as _time
        if t == "/分類":
            counts = message_classifier.today_category_counts(group_id)
            lines = ["📊 今日訊息分類"]
            for cat in message_classifier.CATEGORIES:
                n = counts.get(cat, 0)
                lines.append(f"{_CLASSIFY_EMOJIS.get(cat, '')} {cat}: {n} 則")
            lines.append("")
            lines.append("查詢：/今日財經 /今日健康 /今日警示 /今日影片 /今日行程 /今日其他")
            return "\n".join(lines)
        cat = _CLASSIFY_CMD_TO_CAT[t]
        rows = message_classifier.list_today_in_category(group_id, cat, limit=20)
        emoji = _CLASSIFY_EMOJIS.get(cat, "")
        if not rows:
            return f"{emoji} 今日{cat}：沒有訊息"
        out = [f"{emoji} 今日{cat} ({len(rows)} 則)"]
        for i, r in enumerate(rows, 1):
            ts = _time.strftime("%H:%M", _time.localtime(r["created_at"]))
            txt = (r["text"] or "")[:60]
            out.append(f"{i}. {ts} {txt}")
        return "\n".join(out)
    except Exception as e:
        logger.warning("_handle_classify_command failed: %s", e)
        return None


def _handle_finance_view_command(group_id: str, text: str) -> str | None:
    """處理 /觀點 [可選: 家人名 | 標的]。純 SQL 聚合，不過 Gemini。

    第一句必須是判斷句（line_bot CLAUDE.md 規則 0）。
    """
    s = (text or "").strip()
    if not s.startswith("/觀點"):
        return None

    try:
        import finance_view_db
        import stock_quote
    except ImportError as e:
        logger.warning("finance_view import failed: %s", e)
        return None

    args = s.replace("/觀點", "", 1).strip()

    if not args:
        views = finance_view_db.list_recent(group_id, limit=10)
        if not views:
            return (
                "目前家族觀點庫還是空的。\n"
                "（聊到具體標的 + 方向時會自動記下，例「我覺得 0050 會漲到 180」）"
            )
        return _format_finance_views(views, header="📈 家族最近財經觀點")

    tokens = args.split()
    ticker = None
    person = None
    for tok in tokens:
        try:
            resolved = stock_quote.detect_symbols(tok)
        except Exception:
            resolved = None
        if resolved:
            ticker = resolved[0]
        elif not person:
            person = tok

    if ticker:
        views = finance_view_db.list_by_ticker(group_id, ticker, limit=10)
        header = f"📈 {ticker} 相關家族觀點"
    elif person:
        views = finance_view_db.list_by_person(group_id, person, limit=10)
        header = f"📈 {person} 的財經觀點"
    else:
        return None

    if not views:
        return f"{header}\n\n沒找到相關觀點記錄。"
    return _format_finance_views(views, header=header)


def _format_finance_views(views: list[dict], header: str) -> str:
    """純 SQL 聚合輸出。第一句判斷句（規則 0）。"""
    if not views:
        return f"{header}\n\n沒有記錄。"

    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    tw = _ZI("Asia/Taipei")

    hit = sum(1 for v in views if v.get("validation_result") == "hit")
    miss = sum(1 for v in views if v.get("validation_result") == "miss")
    pending = sum(1 for v in views if v.get("validation_result") == "pending")
    na = sum(1 for v in views if v.get("validation_result") == "na")

    if hit and miss:
        summary = f"共 {len(views)} 條觀點，已驗證 {hit} 命中 / {miss} 落空，{pending} 仍待驗。"
    elif hit:
        summary = f"共 {len(views)} 條觀點，已驗證 {hit} 命中（其餘待驗）。"
    elif miss:
        summary = f"共 {len(views)} 條觀點，已驗證 {miss} 落空。"
    else:
        summary = f"共 {len(views)} 條觀點，{pending + na} 仍待驗。"

    lines = [header, "", summary, ""]
    for v in views[:10]:
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
        try:
            created = _dt.fromtimestamp(v["created_at"] / 1000, tz=tw).strftime("%m-%d")
        except Exception:
            created = "??"
        speaker = v.get("display_name") or "家人"
        lines.append(f"• {created} {speaker} {label} {dir_str}{target} {result_str}")
    return "\n".join(lines)


def _handle_command(group_id: str, text: str) -> str | None:
    """有對應到指令回 str；沒有回 None。"""
    t = text.strip()

    # 訊息分類查詢（2026-05-10 加）
    classify_reply = _handle_classify_command(group_id, t)
    if classify_reply is not None:
        return classify_reply

    # 家族財經觀點查詢（2026-05-18 加）
    fv_reply = _handle_finance_view_command(group_id, t)
    if fv_reply is not None:
        return fv_reply

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
        fact = t[len("/記住 ") :].strip()
        if not fact:
            return "用法：/記住 <要記住的內容>"
        if memory.add_fact(group_id, fact):
            return f"好，記住了：{fact}"
        return f"這條已經在記憶裡了：{fact}"

    if t.startswith("/忘記 "):
        keyword = t[len("/忘記 ") :].strip()
        if not keyword:
            return "用法：/忘記 <關鍵字>"
        n = memory.remove_fact(group_id, keyword)
        return (
            f"刪除了 {n} 條含「{keyword}」的記憶。"
            if n
            else f"沒有找到含「{keyword}」的記憶。"
        )

    if t == "/清除記憶":
        n = memory.clear_facts(group_id)
        return f"已清除 {n} 條記憶。"

    # ── Layer 1：使用者手動管理過濾規則 ──────────────────────────────
    if t.startswith("/不要回 "):
        pattern = t[len("/不要回 ") :].strip()
        if not pattern:
            return "用法：/不要回 <這類訊息的特徵，例如「早安」「中午吃什麼」>"
        rid = memory.add_filter_rule(group_id, "skip", pattern, source="user")
        return f"好，以後訊息裡有「{pattern}」就不主動回。(規則 #{rid})"

    if t.startswith("/以後要查 "):
        pattern = t[len("/以後要查 ") :].strip()
        if not pattern:
            return "用法：/以後要查 <這類訊息的特徵，例如「某醫師說」「疫苗」>"
        rid = memory.add_filter_rule(group_id, "must_answer", pattern, source="user")
        return f"好，以後訊息裡有「{pattern}」就會主動查證。(規則 #{rid})"

    if t == "/規則":
        rules = memory.list_filter_rules(group_id)
        if not rules:
            return "目前沒有過濾規則。\n新增：/不要回 <特徵>  或  /以後要查 <特徵>"
        lines = ["目前的過濾規則："]
        for r in rules:
            tag = "不要回" if r["kind"] == "skip" else "要查"
            src = "手動" if r["source"] == "user" else "自動學"
            lines.append(f"#{r['rule_id']} [{tag}]({src}) {r['pattern']}")
        return "\n".join(lines)

    if t.startswith("/刪除規則 "):
        raw = t[len("/刪除規則 ") :].strip()
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
        raw = t[len("/檢討 ") :].strip()
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
        spec = t[len("/採用 ") :].strip()
        _, msg = review.adopt_drafts(group_id, spec)
        return msg

    # ── Layer 2：糾正剛剛的 bot 回覆 → 自動抽象成規則 ────────────────
    if t.startswith("/閉嘴"):
        reason = t[len("/閉嘴") :].lstrip()
        if not reason:
            return "用法：/閉嘴 <為什麼不應該回>\n例：/閉嘴 這種只是早安問候，不用回"
        return _handle_layer2_correction(group_id, reason)

    # ── 家族行事曆 ──────────────────────────────────────────────
    if t in ("/行事曆", "/活動", "/聚餐"):
        return _format_calendar(group_id)

    if t.startswith("/取消活動 "):
        kw = t[len("/取消活動 ") :].strip()
        if not kw:
            return "用法：/取消活動 <活動關鍵字>"
        return _cancel_calendar_event(group_id, kw)

    return None


def _format_calendar(group_id: str) -> str:
    import calendar_db

    events = calendar_db.list_upcoming(group_id, days=30)
    if not events:
        return "📅 未來 30 天沒有家族活動。"
    lines = ["📅 **家族行事曆（未來 30 天）**"]
    for e in events:
        time_part = f" {e['event_time']}" if e["event_time"] else ""
        loc_part = f" @ {e['location']}" if e["location"] else ""
        try:
            import json as _j

            parts = _j.loads(e["participants"] or "[]")
        except Exception:
            parts = []
        ppl = "、".join(parts) if parts else ""
        ppl_part = f"（{ppl}）" if ppl else ""
        lines.append(
            f"• {e['event_date']}{time_part} {e['title']}{loc_part}{ppl_part}"
        )
    return "\n".join(lines)


def _cancel_calendar_event(group_id: str, keyword: str) -> str:
    import calendar_db

    target = calendar_db.find_active_event(group_id, keyword=keyword)
    if not target:
        return f"找不到含「{keyword}」的活動。用 /行事曆 看清單。"
    if calendar_db.cancel_event(target["event_id"]):
        return f"已取消：{target['event_date']} {target['title']}"
    return f"取消失敗（活動可能已被取消）：{target['title']}"


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
    "【家族行事曆】\n"
    "  /行事曆                 列出未來 30 天的家族活動\n"
    "  /取消活動 <關鍵字>      取消含關鍵字的活動\n"
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
    before = [recent[i][2] for i in range(last_bot_idx) if recent[i][1] != "__bot__"]
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
    "@咪寶 ",
    "@咪寶",
    "＠咪寶 ",
    "＠咪寶",
    "@ai ",
    "@ai",
    "＠ai ",
    "＠ai",
    "@AI ",
    "@AI",
    "＠AI ",
    "＠AI",
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
            return t[len(prefix) :].strip()
    if _is_mentioned(message):
        return _strip_mentions(message).strip()
    # fallback：桌機 LINE @mention 不帶結構，只有純文字 @AI
    for prefix in _TEXT_MENTION_PREFIXES:
        if t == prefix.strip():
            return ""
        if t.lower().startswith(prefix.lower()):
            return t[len(prefix) :].strip()
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


def _md_to_line(text: str) -> str:
    """把 Gemini 回傳的 Markdown 語法轉成 LINE 能直接閱讀的純文字。"""
    lines = text.splitlines()
    out = []
    in_code_block = False
    for line in lines:
        # code block fence
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            out.append(line)
            continue
        # 水平分隔線
        if re.match(r"^\s*[-*_]{3,}\s*$", line):
            out.append("")
            continue
        # headers → 加底線感
        m = re.match(r"^#{1,3}\s+(.+)", line)
        if m:
            out.append(f"▌ {m.group(1)}")
            continue
        # blockquote
        line = re.sub(r"^>\s*", "", line)
        # bullet * / - → •（只處理行首）
        line = re.sub(r"^(\s*)[*-]\s+", r"\1• ", line)
        # bold **text** / __text__
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        line = re.sub(r"__(.+?)__", r"\1", line)
        # italic *text* / _text_（小心不要吃掉 URL 或數學符號）
        line = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", line)
        line = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"\1", line)
        # inline code
        line = re.sub(r"`(.+?)`", r"\1", line)
        # [text](url) → text（url）
        line = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", r"\1（\2）", line)
        out.append(line)
    return "\n".join(out)


def _peek_text_pending_for_drain(
    group_id: str, max_count: int, deadline_sec: float
) -> list[tuple[str, str]]:
    """Peek up to max_count text pending items, render LLM reply (local fallback OK),
    return list of (rendered_text, original_msg_id) WITHOUT removing from pending.

    Caller 必須在 reply_message 成功後 call _commit_pending_removal(group_id, msg_ids)
    才會真的把 entries 從 pending 拿掉 (peek-then-confirm pattern, §3 GP1 C5)。

    Honors deadline_sec — 中途逾時 break。LLM 失敗的 entry skip (不阻塞後續)。
    純 text；file/image pending 不在此快速路徑處理。
    """
    pending = _load_pending_explicit()
    items = pending.get(group_id, [])
    text_items = [
        it for it in items
        if it.get("type") == "text" and (it.get("text") or "").strip()
    ]
    if not text_items:
        return []

    facts = memory.top_facts(group_id)
    context = memory.get_context(group_id)
    pnotes = _get_persona_notes(group_id)

    started = time.time()
    results: list[tuple[str, str]] = []
    for item in text_items[:max_count]:
        if time.time() - started > deadline_sec:
            logger.info(
                "peek drain deadline %.1fs hit, stopping at %d/%d items group=%s",
                deadline_sec, len(results), max_count, group_id,
            )
            break
        original = item["text"].strip()
        try:
            reply_text = _llm_chat(original, context, facts, pnotes)
        except Exception as e:
            logger.warning("peek drain LLM failed for one item: %s", str(e)[:120])
            continue
        if not reply_text:
            continue
        orig_preview = original[:300] + ("…" if len(original) > 300 else "")
        rendered = _md_to_line(reply_text)[:1800]   # 收緊到 1800 (§3 codex Q3 / GP1)
        formatted = (
            f"📬 補回之前漏掉的訊息\n\n原文：\n{orig_preview}\n\n回應：\n{rendered}"
        )
        results.append((formatted[:4900], item.get("message_id", "")))
    return results


def _try_piggyback_drain_with_reply_token(
    reply_token: str | None, group_id: str
) -> None:
    """Gemini 爆時利用 incoming webhook 的 reply_token，把最多 2 條 pending 透過
    本機 LLM fallback 生回覆，bundle 進**單一** reply_message API call (LINE 免費，
    不耗月配額)。peek-then-confirm pattern：reply 失敗 pending 完整保留。

    Critical guards (per §3 codex + GP1 review):
      - drain lock 跟 _drain_pending_for_group 共用 (避免 race + lost-update)
      - deadline 6s (webhook 是 async def 但 _handle_event 是 sync call，太久會
        block uvicorn event loop)
      - max_count=2 (兩條 local LLM call ≈ 2-10s 內可接受 webhook latency)
      - peek-then-confirm (reply API fail → pending 不動，下次 webhook 再試)
      - 中文 char cap 1800 + total reply body 保守上限

    Silent on: 缺 reply_token / bot_muted / 無 pending / drain lock 拿不到 /
    所有 LLM 失敗 / reply_message API 失敗。
    """
    if not reply_token or not group_id:
        return
    if settings.bot_muted:
        return
    if not _load_pending_explicit().get(group_id):
        return

    slot = _try_acquire_drain_slot(group_id)
    if slot is None:
        logger.info(
            "quota-exhausted piggyback: another drain in progress group=%s",
            group_id,
        )
        return
    try:
        rendered = _peek_text_pending_for_drain(
            group_id, max_count=2, deadline_sec=6.0
        )
        if not rendered:
            return

        notice = (
            "📵 咪寶今天 Gemini 額度用完，先用本機模型補回前面漏掉的留言。"
            "你這則訊息會等額度恢復後再回。"
        )
        messages: list = [TextMessage(text=notice)]
        for text, _msg_id in rendered:
            messages.append(TextMessage(text=text))

        try:
            with ApiClient(_get_line_config()) as api_client:
                MessagingApi(api_client).reply_message(
                    ReplyMessageRequest(
                        reply_token=reply_token, messages=messages,
                    )
                )
        except Exception as e:
            logger.warning(
                "quota-exhausted piggyback reply failed (pending preserved): %s",
                str(e)[:200],
            )
            return

        # commit：reply 成功才從 pending 移除
        committed_ids = [msg_id for _, msg_id in rendered if msg_id]
        _commit_pending_removal(group_id, committed_ids)

        # log bot's own replies + observability
        import uuid as _uuid
        for m in messages:
            txt = getattr(m, "text", "")[:500]
            uniq = f"piggyback_{_uuid.uuid4().hex[:12]}"
            memory.log_raw_message(group_id, uniq, "__bot__", txt)
        try:
            bot_stats.track_reply("piggyback_local_drain")
        except Exception:
            pass
        logger.info(
            "quota-exhausted piggyback: drained %d via reply_token group=%s",
            len(rendered), group_id,
        )
    finally:
        slot.release()


def _pop_pending_for_piggyback(group_id: str) -> str | None:
    """pending 有訊息時，從佇列頭取 1 則處理生回覆，格式化成 piggyback 訊息。

    優先序：
      1. text pending（每次 1 條，慢消化）
      2. 沒 text → file pending（PDF/Office，走 local fallback，每次 1 個）
      3. 沒 file → image pending（每次 1 張，走 media_pipeline.analyze_image
         local vision；50s thread timeout 跟 _handle_image_message 對齊）

    成功回格式化字串並從 pending 移除；失敗回 None，pending 不動。
    """
    _drop_stale_pending(group_id)  # D1 TTL: age>7d 進 DLQ，避免 PDF stuck 卡 slot
    pending = _load_pending_explicit()
    items = pending.get(group_id, [])
    if not items:
        return None

    facts = memory.top_facts(group_id)
    context = memory.get_context(group_id)
    pnotes = _get_persona_notes(group_id)

    # ── 1. text pending（user 偏好慢消化）─────────────────────────────────
    _text_items = [
        it
        for it in items
        if it.get("type") == "text" and (it.get("text") or "").strip()
    ]
    batch = _text_items[:1]
    if batch:
        original = "\n".join(it["text"].strip() for it in batch)
        try:
            reply_text = _llm_chat(original, context, facts, pnotes)
        except Exception:
            return None
        if not reply_text:
            return None
        processed_ids = {it.get("message_id") for it in batch}
        pending[group_id] = [
            it for it in items if it.get("message_id") not in processed_ids
        ]
        if not pending[group_id]:
            del pending[group_id]
        _save_pending_explicit_raw(pending)
        orig_preview = original[:300] + ("…" if len(original) > 300 else "")
        reply_preview = _md_to_line(reply_text)
        return f"📬 補回之前漏掉的訊息\n\n原文：\n{orig_preview}\n\n回應：\n{reply_preview}"

    # ── 2. file pending（PDF/Office 走 local fallback）──────────────────
    _file_items = [
        it
        for it in items
        if it.get("type") == "file"
        and it.get("media_path")
        and os.path.exists(it["media_path"])
    ]
    file_batch = _file_items[:1]
    if file_batch:
        file_item = file_batch[0]
        file_name = file_item.get("file_name", "unknown")
        media_path = file_item["media_path"]
        try:
            with open(media_path, "rb") as f:
                data = f.read()
        except Exception as e:
            logger.warning("drain pending file read failed: %s", e)
            return None

        reply_text = _drain_pending_file(data, file_name, group_id, context, facts, pnotes)
        if not reply_text or not reply_text.strip():
            return None

        # 移出 pending + 刪 media_path 檔
        processed_ids = {file_item.get("message_id")}
        pending[group_id] = [
            it for it in items if it.get("message_id") not in processed_ids
        ]
        if not pending[group_id]:
            del pending[group_id]
        _save_pending_explicit_raw(pending)
        try:
            os.remove(media_path)
        except Exception as e:
            logger.warning("drain pending file: remove media_path failed: %s", e)

        reply_preview = _md_to_line(reply_text)[:1500]
        return f"📬 補回之前漏掉的檔案 [{file_name}]\n\n回應：\n{reply_preview}"

    # ── 3. image pending（local 圖像辨識 via media_pipeline.analyze_image）─
    _image_items = [
        it
        for it in items
        if it.get("type") == "image"
        and it.get("media_path")
        and os.path.exists(it["media_path"])
    ]
    img_batch = _image_items[:1]
    if not img_batch:
        return None

    img_item = img_batch[0]
    media_path = img_item["media_path"]
    try:
        with open(media_path, "rb") as f:
            img_data = f.read()
    except Exception as e:
        logger.warning("drain pending image read failed: %s", e)
        return None

    # 跟 _handle_image_message 同 50s thread timeout (reply_token TTL ~1min)
    import threading as _threading
    holder: dict[str, str | None] = {"reply": None}

    def _run():
        try:
            import media_pipeline
            holder["reply"] = media_pipeline.analyze_image(img_data)
        except Exception as e:
            logger.warning("piggyback image analyze failed: %s", e)

    t = _threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=50)
    if t.is_alive():
        logger.info("piggyback image analyze timeout (>50s), leaving pending for next retry")
        return None
    reply_text = holder.get("reply")
    if not reply_text or not reply_text.strip():
        return None

    # 移出 pending + 刪 media file
    processed_ids = {img_item.get("message_id")}
    pending[group_id] = [
        it for it in items if it.get("message_id") not in processed_ids
    ]
    if not pending[group_id]:
        del pending[group_id]
    _save_pending_explicit_raw(pending)
    try:
        os.remove(media_path)
    except Exception as e:
        logger.warning("drain pending image: remove media_path failed: %s", e)

    reply_preview = _md_to_line(reply_text)[:1500]
    return f"📷 補回之前漏掉的圖片\n\n圖片分析：\n{reply_preview}"


def _drain_pending_file(
    data: bytes,
    file_name: str,
    group_id: str,
    context: list,
    facts: list,
    pnotes: list | None,
) -> str | None:
    """drain pending file → 回 reply_text。對齊 _handle_file_message 的 quota 爆 fallback：

    - image → media_pipeline.analyze_image (mlx-vlm + OCR)
    - PDF → pypdf 抽文字 → _llm_chat (fallback chain)；scanned PDF rasterize 走 vision
    - Office (docx/xlsx/pptx) → _extract_office_text → _llm_chat
    - text/* → decode → _llm_chat

    任何失敗回 None，caller 不移 pending（下次 retry）。
    """
    mime_type = _guess_mime_type(file_name)
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""

    # image
    if mime_type.startswith("image/"):
        try:
            from media_pipeline import analyze_image
            return analyze_image(
                data,
                user_prompt=(
                    "請用繁體中文分析這張圖的內容、主題、可見文字，"
                    "第一句必須是具體判斷或結論，"
                    "不要以「使用者」「我看到」「咪寶」「這張圖」等空話開頭。"
                    f"\n\n[檔名：{file_name}]"
                ),
            )
        except Exception as e:
            logger.warning("drain image fallback failed: %s", e)
            return None

    # PDF
    if mime_type == "application/pdf":
        try:
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(data))
            total_pages = len(reader.pages)
            content = "\n".join(
                (p.extract_text() or "") for p in reader.pages[:30]
            )
            if content.strip():
                content = content[:_TEXT_CHAR_LIMIT]
                page_note = (
                    f"（PDF 共 {total_pages} 頁，只看前 30 頁）"
                    if total_pages > 30 else ""
                )
                prompt_text = (
                    "請根據以下 PDF 內容做分析回應，用繁體中文，"
                    "第一句必須是具體判斷或結論，"
                    "不要以「使用者」「我看到」「咪寶」「這份檔案」等空話開頭。\n\n"
                    f"--- PDF 內容開始 ---\n{content}\n--- PDF 內容結束 ---\n\n"
                    f"[檔名：{file_name}]{page_note}"
                )
                return _llm_chat(prompt_text, context, facts, pnotes)
            # scanned PDF → rasterize 第一頁
            if len(data) > 50 * 1024 * 1024:
                logger.info("drain pdf rasterize skip: %s 太大", file_name)
                return None
            try:
                import fitz
                doc = fitz.open(stream=data, filetype="pdf")
                fitz_pages = len(doc)
                img_bytes = None
                try:
                    if fitz_pages > 0:
                        pix = doc[0].get_pixmap(dpi=150)
                        try:
                            img_bytes = pix.tobytes("png")
                        finally:
                            pix = None
                finally:
                    doc.close()
                if img_bytes:
                    from media_pipeline import analyze_image
                    desc = analyze_image(
                        img_bytes,
                        user_prompt=(
                            f"這是 scanned PDF「{file_name}」的第一頁。"
                            "請分析內容，第一句必須是具體判斷，"
                            "不要以「使用者」「我看到」「咪寶」開頭。"
                        ),
                    )
                    if desc and fitz_pages > 1:
                        return f"{desc}\n\n（scanned PDF 共 {fitz_pages} 頁，只分析第一頁）"
                    return desc
            except Exception as e:
                logger.warning("drain pdf rasterize failed: %s", e)
            return None
        except Exception as e:
            logger.warning("drain pdf fallback failed: %s", e)
            return None

    # Office
    if ext in ("docx", "xlsx", "xls", "pptx"):
        content = _extract_office_text(data, file_name)
        if not content or not content.strip():
            return None
        content = content[:_TEXT_CHAR_LIMIT]
        prompt_text = (
            "請根據以下檔案內容做分析回應，用繁體中文，"
            "第一句必須是具體判斷或結論。\n\n"
            f"--- 內容開始 ---\n{content}\n--- 內容結束 ---\n\n"
            f"[檔名：{file_name}]"
        )
        return _llm_chat(prompt_text, context, facts, pnotes)

    # text/*
    if mime_type.startswith("text/"):
        try:
            content = data.decode("utf-8", errors="replace")
        except Exception:
            return None
        if not content.strip():
            return None
        content = content[:_TEXT_CHAR_LIMIT]
        prompt_text = (
            "請根據以下檔案內容做分析回應，用繁體中文，"
            "第一句必須是具體判斷或結論。\n\n"
            f"--- 內容開始 ---\n{content}\n--- 內容結束 ---\n\n"
            f"[檔名：{file_name}]"
        )
        return _llm_chat(prompt_text, context, facts, pnotes)

    return None


def _reply(reply_token: str, text: str, group_id: str | None = None) -> None:
    """
    回覆 LINE 訊息。若帶 group_id,成功後會把 bot 的回覆也存進 raw_messages,
    這樣使用者引用 bot 的回覆問後續問題時,能查得到原文。

    若 reply_token 已過期（例如 redelivery）且有 group_id,
    自動 fallback 到 push_message 補送。

    settings.bot_muted=True 時整個函式 short-circuit:
    不 reply、不 push、只把原本要送的 text 寫進 log 方便除錯。
    """
    if not text or not text.strip():
        return
    # Markdown → LINE 純文字
    text = _md_to_line(text)
    # LINE 單則訊息上限 5000 字；在截斷前先預留 footer 空間
    footer = _get_quota_footer()
    text = text[: 4900 - len(footer)] + footer

    # ── Mute 守門 ─────────────────────────────────────────────────────────────
    # 修 bug 期間預設靜音。webhook 照收、classifier/chat 照跑、log 照寫，只是不送 LINE。
    if settings.bot_muted:
        logger.info(
            "[MUTED] would_reply group=%s len=%d preview=%r",
            group_id,
            len(text),
            text[:120],
        )
        return

    # 額度耗盡時，偷塞 pending 進同一則 reply_message（免費）
    # LINE reply_message 上限 5 則 → 1 則實回覆 + 最多 4 則 piggyback
    # 2026-05-16 改：從 1 提到 4，加速 pending drain（quota 爆等月初重置這種長窗期）
    messages_to_send: list = [TextMessage(text=text)]
    if group_id:
        for _ in range(4):
            if _quota_exhausted():
                logger.info("piggyback skip: gemini exhausted group=%s", group_id)
                break
            pig = _pop_pending_for_piggyback(group_id)
            if not pig:
                # 區分「沒 pending」vs「pending 全是 non-text」(PDF/audio stuck)
                pending_count = len(_load_pending_explicit().get(group_id, []))
                if pending_count > 0:
                    logger.info(
                        "piggyback skip: pending=%d but all non-text or llm failed group=%s",
                        pending_count, group_id,
                    )
                break
            messages_to_send.append(TextMessage(text=pig[:5000]))
            logger.info("piggyback popped 1 group=%s pig_len=%d", group_id, len(pig))

    resp = None
    try:
        with ApiClient(_get_line_config()) as api_client:
            resp = MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=messages_to_send,
                )
            )
    except Exception as e:
        # reply_token 過期 / 用過 / 重送事件 → fallback 到 push_message
        logger.warning("reply failed: %s", str(e)[:300])
        if group_id:
            try:
                with ApiClient(_get_line_config()) as api_client:
                    MessagingApi(api_client).push_message(
                        PushMessageRequest(
                            to=group_id,
                            messages=[TextMessage(text=text)],
                        )
                    )
                logger.info("fallback push_message sent to group=%s", group_id)
                bot_stats.track_line_push()
                memory.log_raw_message(
                    group_id, f"push_{int(time.time() * 1000)}", "__bot__", text
                )
            except Exception as push_err:
                logger.warning("fallback push also failed: %s", str(push_err)[:300])
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
    with ApiClient(_get_line_config()) as api_client:
        return bytes(MessagingApiBlob(api_client).get_message_content(message_id))


def _guess_mime_type(file_name: str) -> str:
    mt, _ = mimetypes.guess_type(file_name)
    return mt or "application/octet-stream"
