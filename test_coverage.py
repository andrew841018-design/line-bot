"""
Comprehensive coverage tests — no real API calls.
Targets: main.py, grok_client.py, bot_stats.py
"""

import json
import os
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

# ── env setup before any import ──────────────────────────────────────────────
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import main  # noqa: E402
import grok_client  # noqa: E402
import bot_stats  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}")


# ═══════════════════════════════════════════════════════════════════════════════
# Test A: _parse_vtt — 純函式
# ═══════════════════════════════════════════════════════════════════════════════
def test_parse_vtt():
    print("\n── Test A: _parse_vtt ──")
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<c>Hello</c> World\n\n00:00:04.000 --> 00:00:06.000\nHello World\n\n1\n"
    result = main._parse_vtt(vtt)
    check("去掉 WEBVTT header", "WEBVTT" not in result)
    check("去掉時間碼", "-->" not in result)
    check("去掉 HTML tag", "<c>" not in result)
    check("保留文字內容", "Hello World" in result)
    check("相鄰重複行去掉", result.count("Hello World") == 1)
    check("空字串回空", main._parse_vtt("") == "")
    check("純 header 回空", main._parse_vtt("WEBVTT\n\n") == "")


# ═══════════════════════════════════════════════════════════════════════════════
# Test B: _md_to_line — 純函式
# ═══════════════════════════════════════════════════════════════════════════════
def test_md_to_line():
    print("\n── Test B: _md_to_line ──")
    check("header → ▌", main._md_to_line("# 標題").startswith("▌"))
    check("h2 → ▌", main._md_to_line("## 二級").startswith("▌"))
    check("bold 去除星號", main._md_to_line("**粗體**") == "粗體")
    check("bullet * → •", "•" in main._md_to_line("* 項目"))
    check("bullet - → •", "•" in main._md_to_line("- 項目"))
    check("inline code 去反引號", main._md_to_line("`code`") == "code")
    check(
        "link 轉換格式",
        "text（http://x.com）" in main._md_to_line("[text](http://x.com)"),
    )
    check(
        "code block fence 去除", "```" not in main._md_to_line("```python\ncode\n```")
    )
    check("code block 內容保留", "code" in main._md_to_line("```python\ncode\n```"))
    check("水平線轉空行", main._md_to_line("---") == "")
    check("blockquote 去 >", "> " not in main._md_to_line("> 引用"))
    check("空字串不爆", main._md_to_line("") == "")


# ═══════════════════════════════════════════════════════════════════════════════
# Test C: _is_dinner_question
# ═══════════════════════════════════════════════════════════════════════════════
def test_is_dinner_question():
    print("\n── Test C: _is_dinner_question ──")
    check("晚餐吃什麼 → True", main._is_dinner_question("今天晚餐吃什麼"))
    check("今天吃什麼 → True", main._is_dinner_question("今天吃什麼好"))
    check("今晚吃什麼 → True", main._is_dinner_question("今晚吃什麼"))
    check("隨機文字 → False", not main._is_dinner_question("我去便利商店"))
    check("空字串 → False", not main._is_dinner_question(""))


# ═══════════════════════════════════════════════════════════════════════════════
# Test D: _is_quota_error + _friendly_gemini_error
# ═══════════════════════════════════════════════════════════════════════════════
def test_quota_error():
    print("\n── Test D: quota error helpers ──")
    e_day = Exception("429 RESOURCE_EXHAUSTED PerDay free_tier_requests")
    e_min = Exception("429 RESOURCE_EXHAUSTED per_minute_limit")
    e_401 = Exception("401 Unauthorized")
    e_500 = Exception("503 UNAVAILABLE")
    e_other = Exception("SomeOtherError")

    check("日額度 429 → is_quota_error True", main._is_quota_error(e_day))
    check("分鐘 429 → is_quota_error False", not main._is_quota_error(e_min))
    check("401 → is_quota_error False", not main._is_quota_error(e_401))

    check("日額度 → friendly 含時間", "額度" in main._friendly_gemini_error(e_day))
    check("分鐘 429 → friendly 回空", main._friendly_gemini_error(e_min) == "")
    check(
        "401 → friendly 含 key",
        "key" in main._friendly_gemini_error(e_401).lower()
        or "api" in main._friendly_gemini_error(e_401).lower(),
    )
    check(
        "503 → friendly 含斷線",
        "斷線" in main._friendly_gemini_error(e_500)
        or "暫時" in main._friendly_gemini_error(e_500),
    )
    check(
        "其他錯誤 → friendly 含類型名",
        type(e_other).__name__ in main._friendly_gemini_error(e_other),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Test E: _is_mentioned + _extract_gemini_trigger + _strip_mentions
# ═══════════════════════════════════════════════════════════════════════════════
def _make_message(text, mentionees=None):
    """Build a minimal TextMessageContent-like mock."""
    msg = MagicMock()
    msg.text = text
    if mentionees is not None:
        mention = MagicMock()
        mention.mentionees = mentionees
        msg.mention = mention
    else:
        msg.mention = None
    return msg


def _make_self_mentionee(index=0, length=3):
    m = MagicMock()
    m.is_self = True
    m.index = index
    m.length = length
    return m


def test_trigger_detection():
    print("\n── Test E: trigger detection ──")
    no_mention = _make_message("普通訊息")
    check("無 mention → _is_mentioned False", not main._is_mentioned(no_mention))

    self_m = _make_self_mentionee(0, 3)
    with_mention = _make_message("@Bot 你好", mentionees=[self_m])
    check("有 is_self → _is_mentioned True", main._is_mentioned(with_mention))

    # /ai prefix
    ai_msg = _make_message("/ai 請問天氣")
    check(
        "/ai prefix → trigger 回問題",
        main._extract_gemini_trigger("/ai 請問天氣", ai_msg) == "請問天氣",
    )

    # /ask
    ask_msg = _make_message("/ask hello")
    check(
        "/ask prefix → trigger 回問題",
        main._extract_gemini_trigger("/ask hello", ask_msg) == "hello",
    )

    # 咪寶 keyword
    name_msg = _make_message("咪寶你好嗎")
    result = main._extract_gemini_trigger("咪寶你好嗎", name_msg)
    check("咪寶關鍵字 → trigger 非 None", result is not None)
    check("咪寶關鍵字 → 移除名字", "咪寶" not in (result or ""))

    # 普通訊息 → None
    plain_msg = _make_message("今天天氣不錯")
    check(
        "普通訊息 → trigger None",
        main._extract_gemini_trigger("今天天氣不錯", plain_msg) is None,
    )

    # @AI fallback (no mention struct)
    at_ai = _make_message("@AI 幫我查")
    check(
        "@AI 純文字 → trigger 非 None",
        main._extract_gemini_trigger("@AI 幫我查", at_ai) is not None,
    )

    # _strip_mentions: 刪掉 mention range
    self_m2 = _make_self_mentionee(0, 3)
    strip_msg = _make_message("@Bot 你好", mentionees=[self_m2])
    stripped = main._strip_mentions(strip_msg)
    check("_strip_mentions 去掉 mention 區段", len(stripped) < len("@Bot 你好"))


# ═══════════════════════════════════════════════════════════════════════════════
# Test F: _heuristic_group_messages
# ═══════════════════════════════════════════════════════════════════════════════
def test_heuristic_group():
    print("\n── Test F: _heuristic_group_messages ──")
    items = [{"text": "a"}, {"text": "b"}, {"text": "c"}]
    result = main._heuristic_group_messages(items)
    check("3 items → 3 groups", len(result) == 3)
    check("每組 1 item", all(len(g["idxs"]) == 1 for g in result))
    check("idxs 是 0,1,2", [g["idxs"][0] for g in result] == [0, 1, 2])
    check("reply_to == idxs[0]", all(g["reply_to"] == g["idxs"][0] for g in result))
    check("空 items → 空結果", main._heuristic_group_messages([]) == [])


# ═══════════════════════════════════════════════════════════════════════════════
# Test G: _quota_exhausted / _mark_quota_exhausted / _load_quota_state
# ═══════════════════════════════════════════════════════════════════════════════
def test_quota_state():
    print("\n── Test G: quota state ──")
    # 重置狀態
    main._quota_exhausted_until_ts = 0.0
    check("初始不 exhausted", not main._quota_exhausted())

    # 設成未來
    main._quota_exhausted_until_ts = time.time() + 3600
    check("設未來 ts → exhausted True", main._quota_exhausted())

    # 設成過去
    main._quota_exhausted_until_ts = time.time() - 1
    check("設過去 ts → exhausted False", not main._quota_exhausted())

    # _load_quota_state：檔案存在
    main._quota_exhausted_until_ts = 0.0
    future_ts = time.time() + 3600
    state = {"exhausted_until_ts": future_ts, "notified_for_ts": future_ts}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(state, f)
        tmp_path = f.name
    orig_path = main._QUOTA_STATE_FILE
    main._QUOTA_STATE_FILE = tmp_path
    main._load_quota_state()
    main._QUOTA_STATE_FILE = orig_path
    os.unlink(tmp_path)
    check(
        "load_quota_state 讀 exhausted_until_ts",
        main._quota_exhausted_until_ts == future_ts,
    )

    # _load_quota_state：檔案不存在 → 不爆
    main._quota_exhausted_until_ts = 0.0
    main._QUOTA_STATE_FILE = "/tmp/nonexistent_quota_test.json"
    main._load_quota_state()
    main._QUOTA_STATE_FILE = orig_path
    check("load_quota_state 檔案不存在不爆", main._quota_exhausted_until_ts == 0.0)

    # 還原
    main._quota_exhausted_until_ts = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Test H: _fetch_tiktok_meta — mock requests
# ═══════════════════════════════════════════════════════════════════════════════
def test_fetch_tiktok_meta():
    print("\n── Test H: _fetch_tiktok_meta ──")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "title": "超好笑影片 #funny",
        "author_name": "小明",
        "author_unique_id": "xiao_ming",
        "html": '<a href="#">> ♬ 熱門歌曲</a>',
    }
    with patch("main._requests") as mock_req:
        mock_req.get.return_value = mock_resp
        mock_req.head.return_value = MagicMock(
            url="https://www.tiktok.com/@xiao_ming/video/123"
        )
        result = main._fetch_tiktok_meta("https://www.tiktok.com/@xiao_ming/video/123")
    check("成功回傳非 None", result is not None)
    check("包含標題", "超好笑影片" in (result or ""))
    check("包含作者", "小明" in (result or ""))

    # HTTP 非 200
    mock_resp_fail = MagicMock()
    mock_resp_fail.status_code = 404
    with patch("main._requests") as mock_req2:
        mock_req2.get.return_value = mock_resp_fail
        result2 = main._fetch_tiktok_meta("https://www.tiktok.com/bad")
    check("HTTP 404 → None", result2 is None)

    # requests 拋例外
    with patch("main._requests") as mock_req3:
        mock_req3.get.side_effect = Exception("timeout")
        result3 = main._fetch_tiktok_meta("https://www.tiktok.com/err")
    check("exception → None", result3 is None)

    # 短網址 resolve 失敗 → None
    with patch("main._requests") as mock_req4:
        mock_req4.head.side_effect = Exception("network error")
        result4 = main._fetch_tiktok_meta("https://vt.tiktok.com/ZXXXXX/")
    check("短網址 resolve 失敗 → None", result4 is None)


# ═══════════════════════════════════════════════════════════════════════════════
# Test I: _fetch_youtube_meta — mock requests
# ═══════════════════════════════════════════════════════════════════════════════
def test_fetch_youtube_meta():
    print("\n── Test I: _fetch_youtube_meta ──")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"title": "影片標題", "author_name": "頻道名稱"}
    with patch("main._requests") as mock_req:
        mock_req.get.return_value = mock_resp
        result = main._fetch_youtube_meta("https://youtube.com/watch?v=abc")
    check("成功包含標題", "影片標題" in (result or ""))
    check("成功包含頻道", "頻道名稱" in (result or ""))

    # 空 title + author
    mock_resp2 = MagicMock()
    mock_resp2.status_code = 200
    mock_resp2.json.return_value = {"title": "", "author_name": ""}
    with patch("main._requests") as mock_req2:
        mock_req2.get.return_value = mock_resp2
        result2 = main._fetch_youtube_meta("https://youtube.com/watch?v=empty")
    check("空 title/author → None", result2 is None)

    # exception
    with patch("main._requests") as mock_req3:
        mock_req3.get.side_effect = Exception("conn refused")
        result3 = main._fetch_youtube_meta("https://youtube.com/watch?v=err")
    check("exception → None", result3 is None)


# ═══════════════════════════════════════════════════════════════════════════════
# Test J: _fetch_instagram_embed — mock requests
# ═══════════════════════════════════════════════════════════════════════════════
def test_fetch_instagram_embed():
    print("\n── Test J: _fetch_instagram_embed ──")

    html_with_caption = (
        '<html><body><div class="Caption">這是一個很棒的貼文！</div></body></html>'
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = html_with_caption
    with patch("main._requests") as mock_req:
        mock_req.get.return_value = mock_resp
        result = main._fetch_instagram_embed("https://www.instagram.com/reel/ABC123/")
    check("IG embed 成功非 None", result is not None)
    check("IG embed 包含 caption", "很棒的貼文" in (result or ""))

    # 無法解析 URL pattern
    result2 = main._fetch_instagram_embed("https://www.instagram.com/stories/user/123/")
    check("非 reel/p URL → None", result2 is None)

    # HTTP 非 200
    mock_resp3 = MagicMock()
    mock_resp3.status_code = 403
    with patch("main._requests") as mock_req3:
        mock_req3.get.return_value = mock_resp3
        result3 = main._fetch_instagram_embed("https://www.instagram.com/p/XYZ789/")
    check("HTTP 403 → None", result3 is None)

    # caption 太短
    html_short = '<html><body><div class="Caption">短</div></body></html>'
    mock_resp4 = MagicMock()
    mock_resp4.status_code = 200
    mock_resp4.text = html_short
    with patch("main._requests") as mock_req4:
        mock_req4.get.return_value = mock_resp4
        result4 = main._fetch_instagram_embed("https://www.instagram.com/p/SHORT/")
    check("caption 太短 → None", result4 is None)


# ═══════════════════════════════════════════════════════════════════════════════
# Test K: _fetch_reddit_meta — mock requests
# ═══════════════════════════════════════════════════════════════════════════════
def test_fetch_reddit_meta():
    print("\n── Test K: _fetch_reddit_meta ──")
    post_data = {
        "data": {
            "children": [
                {
                    "data": {
                        "title": "測試標題",
                        "selftext": "這是貼文正文",
                        "subreddit": "testsubreddit",
                        "score": 100,
                    }
                }
            ]
        }
    }
    comments_data = {
        "data": {
            "children": [
                {"kind": "t1", "data": {"body": "第一個留言", "score": 50}},
                {"kind": "t1", "data": {"body": "第二個留言", "score": 30}},
            ]
        }
    }

    mock_resp_post = MagicMock()
    mock_resp_post.status_code = 200
    mock_resp_post.json.return_value = [post_data, comments_data]
    with patch("main._requests") as mock_req:
        mock_req.get.return_value = mock_resp_post
        mock_req.head.return_value = MagicMock(
            url="https://www.reddit.com/r/testsubreddit/comments/abc/test/"
        )
        result = main._fetch_reddit_meta(
            "https://www.reddit.com/r/testsubreddit/comments/abc/test/"
        )
    check("reddit 成功非 None", result is not None)
    check("包含標題", "測試標題" in (result or ""))
    check("包含留言", "第一個留言" in (result or ""))

    # exception
    with patch("main._requests") as mock_req2:
        mock_req2.get.side_effect = Exception("timeout")
        result2 = main._fetch_reddit_meta("https://www.reddit.com/r/x/")
    check("exception → None", result2 is None)


# ═══════════════════════════════════════════════════════════════════════════════
# Test L: _prefetch_urls dispatch logic
# ═══════════════════════════════════════════════════════════════════════════════
def test_prefetch_urls():
    print("\n── Test L: _prefetch_urls ──")
    # 無 URL → 原文回傳
    result = main._prefetch_urls("純文字無連結")
    check("無 URL → 原文不變", result == "純文字無連結")

    # YouTube URL → 呼叫 _fetch_video_ytdlp
    with patch("main._fetch_video_ytdlp", return_value="[ytdlp content]") as mock_yt:
        result2 = main._prefetch_urls("看這個 https://youtube.com/watch?v=abc")
    check("YouTube URL → ytdlp 被呼叫", mock_yt.called)
    check("YouTube URL → 結果含 ytdlp content", "[ytdlp content]" in result2)

    # TikTok URL，ytdlp 失敗 fallback oembed
    with (
        patch("main._fetch_video_ytdlp", return_value=None),
        patch("main._fetch_tiktok_meta", return_value="[tiktok meta]") as mock_tt,
    ):
        result3 = main._prefetch_urls("看這個 https://www.tiktok.com/@user/video/123")
    check("TikTok ytdlp 失敗 → oembed 被呼叫", mock_tt.called)
    check("TikTok oembed 結果塞入", "[tiktok meta]" in result3)

    # Instagram URL
    with (
        patch("main._fetch_video_ytdlp", return_value=None),
        patch("main._fetch_instagram_embed", return_value="[ig embed]") as mock_ig,
    ):
        main._prefetch_urls("看 https://www.instagram.com/reel/ABC/")
    check("Instagram → embed 被呼叫", mock_ig.called)

    # 一般網頁
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = (
        "<html><body><p>文章內容非常豐富，超過 80 個字！"
        + "x" * 100
        + "</p></body></html>"
    )
    mock_resp.raise_for_status = MagicMock()
    with patch("main._requests") as mock_req:
        mock_req.get.return_value = mock_resp
        main._prefetch_urls("看 https://news.ycombinator.com/item?id=123")
    check("一般網頁 → requests.get 被呼叫", mock_req.get.called)


# ═══════════════════════════════════════════════════════════════════════════════
# Test M: _next_gemini_reset_tw
# ═══════════════════════════════════════════════════════════════════════════════
def test_next_gemini_reset_tw():
    print("\n── Test M: _next_gemini_reset_tw ──")
    abs_str, rel_str = main._next_gemini_reset_tw()
    check("abs_str 非空", bool(abs_str))
    check("rel_str 含「還有」", "還有" in rel_str)
    check("abs_str 含時間格式（:）", ":" in abs_str)
    check("rel_str 含「分鐘」", "分鐘" in rel_str)


# ═══════════════════════════════════════════════════════════════════════════════
# Test N: _get_quota_footer — mock gemini/grok
# ═══════════════════════════════════════════════════════════════════════════════
def test_get_quota_footer():
    print("\n── Test N: _get_quota_footer ──")
    # Gemini 有量
    main._quota_exhausted_until_ts = 0.0
    with patch(
        "main.gemini_client.get_gemini_quota_info",
        return_value={
            "used_tokens": 5000,
            "limit_tokens": 100000,
            "used_requests": 5,
            "limit_requests": 20,
            "used_thinking_tokens": 0,
        },
    ):
        footer = main._get_quota_footer()
    check("有量 footer 含 %", "%" in footer)

    # Gemini 用完，Grok 有量
    main._quota_exhausted_until_ts = time.time() + 3600
    with patch(
        "main.grok_client.get_quota_info",
        return_value={"remaining": 10, "used_requests": 15, "limit_requests": 25},
    ):
        footer2 = main._get_quota_footer()
    check("Gemini 用完 Grok 有量 → footer 含 Grok", "Grok" in footer2)

    # 兩個都用完
    with patch(
        "main.grok_client.get_quota_info",
        return_value={"remaining": 0, "used_requests": 25, "limit_requests": 25},
    ):
        footer3 = main._get_quota_footer()
    check(
        "兩個都用完 → footer 含「用量已用完」",
        "用量已用完" in footer3 or "Grok" in footer3,
    )

    main._quota_exhausted_until_ts = 0.0  # 還原


# ═══════════════════════════════════════════════════════════════════════════════
# Test O: /health endpoint
# ═══════════════════════════════════════════════════════════════════════════════
def test_health_endpoint():
    print("\n── Test O: /health endpoint ──")
    try:
        from fastapi.testclient import TestClient

        client = TestClient(main.app)
        resp = client.get("/health")
        check("health status 200", resp.status_code == 200)
        data = resp.json()
        check("有 status key", "status" in data)
        check("status == ok", data.get("status") == "ok")
        check("有 gemini_model key", "gemini_model" in data)
    except Exception as e:
        check(f"health endpoint 可建立 TestClient (err={e})", False)


# ═══════════════════════════════════════════════════════════════════════════════
# Test P: _extract_subtitles_from_info — mock requests
# ═══════════════════════════════════════════════════════════════════════════════
def test_extract_subtitles():
    print("\n── Test P: _extract_subtitles_from_info ──")
    # 有字幕
    long_line = (
        "這是非常長的字幕文字內容，一定超過五十個字元，用來測試字幕擷取功能是否正常運作。"
        * 2
    )
    vtt_content = f"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n{long_line}\n\n"
    mock_resp = MagicMock()
    mock_resp.text = vtt_content
    mock_resp.raise_for_status = MagicMock()

    info = {
        "subtitles": {"zh-TW": [{"ext": "vtt", "url": "https://example.com/subs.vtt"}]}
    }
    with patch("main._requests") as mock_req:
        mock_req.get.return_value = mock_resp
        result = main._extract_subtitles_from_info(info)
    check("有字幕 → 非 None", result is not None)
    check("字幕包含文字", "字幕文字內容" in (result or ""))

    # 無字幕
    empty_info = {"subtitles": {}, "automatic_captions": {}}
    result2 = main._extract_subtitles_from_info(empty_info)
    check("無字幕 → None", result2 is None)

    # URL fetch 失敗
    info3 = {
        "subtitles": {"zh-TW": [{"ext": "vtt", "url": "https://example.com/subs.vtt"}]}
    }
    with patch("main._requests") as mock_req3:
        mock_req3.get.side_effect = Exception("timeout")
        result3 = main._extract_subtitles_from_info(info3)
    check("fetch 失敗 → None", result3 is None)


# ═══════════════════════════════════════════════════════════════════════════════
# Test Q: grok_client — quota & chat
# ═══════════════════════════════════════════════════════════════════════════════
def test_grok_client_quota():
    print("\n── Test Q: grok_client quota ──")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"date": grok_client._today_pt(), "requests": 0}, f)
        tmp = f.name
    orig = grok_client._USAGE_FILE
    grok_client._USAGE_FILE = tmp

    check("初始 quota_exhausted False", not grok_client.quota_exhausted())

    info = grok_client.get_quota_info()
    check("get_quota_info 有 used_requests", "used_requests" in info)
    check("get_quota_info used=0", info["used_requests"] == 0)
    check("remaining = limit", info["remaining"] == grok_client._DAILY_REQUEST_LIMIT)

    # 用量達上限
    json.dump(
        {"date": grok_client._today_pt(), "requests": grok_client._DAILY_REQUEST_LIMIT},
        open(tmp, "w"),
    )
    grok_client._client = None
    check("達上限 → quota_exhausted True", grok_client.quota_exhausted())

    # 日期切換 → 重置
    json.dump({"date": "2000-01-01", "requests": 999}, open(tmp, "w"))
    check("過期日期 → quota_exhausted False", not grok_client.quota_exhausted())

    grok_client._USAGE_FILE = orig
    os.unlink(tmp)


def test_grok_client_chat():
    print("\n── Test R: grok_client chat ──")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"date": grok_client._today_pt(), "requests": 0}, f)
        tmp = f.name
    orig_usage = grok_client._USAGE_FILE
    grok_client._USAGE_FILE = tmp
    orig_client = grok_client._client

    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock()]
    mock_completion.choices[0].message.content = "Grok 的回答"

    mock_openai = MagicMock()
    mock_openai.chat.completions.create.return_value = mock_completion
    grok_client._client = mock_openai

    result = grok_client.chat("你好", [], [])
    check("chat 成功回傳文字", result == "Grok 的回答")
    check("usage 增加 1", json.load(open(tmp))["requests"] == 1)

    # quota 爆時直接回空
    json.dump(
        {"date": grok_client._today_pt(), "requests": grok_client._DAILY_REQUEST_LIMIT},
        open(tmp, "w"),
    )
    result2 = grok_client.chat("你好", [], [])
    check("quota 爆 → 回空字串", result2 == "")

    # API 拋例外
    json.dump({"date": grok_client._today_pt(), "requests": 0}, open(tmp, "w"))
    mock_openai.chat.completions.create.side_effect = Exception("API error")
    result3 = grok_client.chat("錯誤", [], [])
    check("API 例外 → 回空字串", result3 == "")

    grok_client._USAGE_FILE = orig_usage
    grok_client._client = orig_client
    os.unlink(tmp)


def test_grok_client_group_messages():
    print("\n── Test S: grok_client group_messages ──")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"date": grok_client._today_pt(), "requests": 0}, f)
        tmp = f.name
    orig_usage = grok_client._USAGE_FILE
    grok_client._USAGE_FILE = tmp
    orig_client = grok_client._client

    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock()]
    mock_completion.choices[0].message.content = json.dumps(
        {"groups": [{"idxs": [0, 1], "reply_to": 0}, {"idxs": [2], "reply_to": 2}]}
    )

    mock_openai = MagicMock()
    mock_openai.chat.completions.create.return_value = mock_completion
    grok_client._client = mock_openai

    items = [
        {"text": "a", "user_id": "u1"},
        {"text": "b", "user_id": "u1"},
        {"text": "c", "user_id": "u2"},
    ]
    result = grok_client.group_messages(items)
    check("group_messages 非 None", result is not None)
    check("分成 2 組", len(result) == 2)

    # API 回 invalid JSON
    mock_completion2 = MagicMock()
    mock_completion2.choices = [MagicMock()]
    mock_completion2.choices[0].message.content = "not json"
    mock_openai.chat.completions.create.return_value = mock_completion2
    result2 = grok_client.group_messages(items)
    check("invalid JSON → None", result2 is None)

    # empty items
    result3 = grok_client.group_messages([])
    check("empty items → None 或空", result3 is None or result3 == [])

    grok_client._USAGE_FILE = orig_usage
    grok_client._client = orig_client
    os.unlink(tmp)


# ═══════════════════════════════════════════════════════════════════════════════
# Test T: bot_stats — track_* functions + query_range + summary_report
# ═══════════════════════════════════════════════════════════════════════════════
def test_bot_stats_track():
    print("\n── Test T: bot_stats track functions ──")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_db = f.name
    os.unlink(tmp_db)  # 讓 bot_stats 自己建

    orig_db = bot_stats._DB_PATH
    bot_stats._DB_PATH = tmp_db
    # 重建 connection
    bot_stats._db_conn = None

    bot_stats.track_message("今天吃什麼晚餐")
    bot_stats.track_reply("gemini")
    bot_stats.track_reply("grok")
    bot_stats.track_pending_saved()
    bot_stats.track_line_push()

    data = bot_stats.query_range(days=1)
    check("query_range 有資料", len(data) > 0)
    today_data = data[0]
    check("msg_received 有計數", today_data.get("msg_received", 0) >= 1)
    check("reply_gemini 有計數", today_data.get("reply_gemini", 0) >= 1)
    check("reply_grok 有計數", today_data.get("reply_grok", 0) >= 1)
    check("msg_pending_saved 有計數", today_data.get("msg_pending_saved", 0) >= 1)
    check("line_push_used 有計數", today_data.get("line_push_used", 0) >= 1)

    report = bot_stats.summary_report(days=30)
    check("summary_report 非空", bool(report))
    check("summary_report 含「統計」", "統計" in report)
    check("summary_report 含「回覆」", "回覆" in report)

    # 無資料時
    bot_stats._db_conn = None
    os.unlink(tmp_db)
    bot_stats._DB_PATH = tmp_db  # 新空 DB
    empty_report = bot_stats.summary_report(days=30)
    check("無資料 → 提示訊息", "尚無" in empty_report or len(empty_report) > 0)

    bot_stats._DB_PATH = orig_db
    bot_stats._db_conn = None
    try:
        os.unlink(tmp_db)
    except FileNotFoundError:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    test_parse_vtt()
    test_md_to_line()
    test_is_dinner_question()
    test_quota_error()
    test_trigger_detection()
    test_heuristic_group()
    test_quota_state()
    test_fetch_tiktok_meta()
    test_fetch_youtube_meta()
    test_fetch_instagram_embed()
    test_fetch_reddit_meta()
    test_prefetch_urls()
    test_next_gemini_reset_tw()
    test_get_quota_footer()
    test_health_endpoint()
    test_extract_subtitles()
    test_grok_client_quota()
    test_grok_client_chat()
    test_grok_client_group_messages()
    test_bot_stats_track()

    print(f"\n{'=' * 50}")
    print(f"TOTAL: {PASS} passed, {FAIL} failed")
    print("=" * 50)
    if FAIL:
        print("Some tests FAILED.")
        sys.exit(1)
    else:
        print("All tests passed!")
