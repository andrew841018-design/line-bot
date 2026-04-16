#!/usr/bin/env python3
"""
QA 測試：URL 處理 + Gemini 回覆品質 + 人設 + 資料庫

覆蓋今天所有踩過的坑：
  1. JS 渲染網站偵測（TikTok/Dcard/IG/X/Reddit）
  2. Prefetch 垃圾過濾（短內容不塞進 prompt）
  3. DB 沒有殘留的「忽略 TikTok」規則
  4. 人設正確（溫柔可愛，不是調皮幼稚）
  5. 系統提示詞禁止說「跳過」「不看」
  6. 空回覆重試邏輯
  7. Gemini 回覆品質（非空、中文、無禁止用語）

用法：
  python test_prefetch.py          # 跑全部（含 Gemini API）
  python test_prefetch.py --quick  # 只跑離線測試
"""
import sys
import os
import re
import sqlite3
import argparse

sys.path.insert(0, os.path.dirname(__file__))

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        msg = f"  [FAIL] {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)


# ══════════════════════════════════════════════════════════════════════════════
# Test 1: JS 渲染網站偵測
# ══════════════════════════════════════════════════════════════════════════════

def test_js_domain_detection():
    print("\n── Test 1: JS 渲染網站偵測 ──")
    from main import _JS_RENDERED_DOMAINS

    # 這些必須被偵測（不 prefetch）
    must_match = [
        ("TikTok vt",      "https://vt.tiktok.com/ZSHsUffnc/"),
        ("TikTok vm",      "https://vm.tiktok.com/abc123/"),
        ("TikTok www",     "https://www.tiktok.com/@user/video/123"),
        ("Dcard",          "https://www.dcard.tw/f/trending/p/257217382"),
        ("Instagram post", "https://www.instagram.com/p/abc123/"),
        ("Instagram reel", "https://www.instagram.com/reel/abc123/"),
        ("X",              "https://x.com/user/status/123"),
        ("Twitter",        "https://twitter.com/user/status/123"),
        ("Reddit",         "https://www.reddit.com/r/taiwan/comments/abc/"),
        ("Facebook",       "https://www.facebook.com/story.php?id=123"),
        ("fb.watch",       "https://fb.watch/abc123/"),
        ("Threads",        "https://www.threads.net/@user/post/abc"),
        ("YT Shorts",      "https://www.youtube.com/shorts/abc123"),
    ]
    for name, url in must_match:
        check(name, bool(_JS_RENDERED_DOMAINS.search(url)), f"should match: {url}")

    # 這些不應該被偵測（正常 prefetch）
    must_not_match = [
        ("Yahoo News", "https://news.yahoo.com/article"),
        ("Wikipedia",  "https://zh.wikipedia.org/wiki/Test"),
        ("UDN",        "https://udn.com/news/story/123/456"),
        ("CNA",        "https://www.cna.com.tw/news/123.aspx"),
        ("ETtoday",    "https://www.ettoday.net/news/123/456.htm"),
    ]
    for name, url in must_not_match:
        check(f"{name} (不該匹配)", not bool(_JS_RENDERED_DOMAINS.search(url)), f"should NOT match: {url}")


# ══════════════════════════════════════════════════════════════════════════════
# Test 2: Prefetch 不產生垃圾
# ══════════════════════════════════════════════════════════════════════════════

def test_prefetch_no_garbage():
    print("\n── Test 2: Prefetch 垃圾過濾 ──")
    from main import _prefetch_urls

    garbage_marker = "--- 網頁內容開始 ---"

    # JS 網站不該有 content block
    js_urls = [
        ("TikTok",    "https://vt.tiktok.com/ZSHsUffnc/"),
        ("Dcard",     "https://www.dcard.tw/f/trending/p/257217382"),
        ("Instagram", "https://www.instagram.com/p/abc123/"),
    ]
    for name, url in js_urls:
        result = _prefetch_urls(url)
        check(f"{name} 無垃圾 block", garbage_marker not in result)
        check(f"{name} 原始 URL 保留", url in result)


# ══════════════════════════════════════════════════════════════════════════════
# Test 3: DB 狀態
# ══════════════════════════════════════════════════════════════════════════════

def test_db_clean():
    print("\n── Test 3: DB 無殘留問題規則 ──")
    db_path = os.path.join(os.path.dirname(__file__), "line_bot.db")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 沒有「忽略 TikTok」的 persona_note
    cur.execute("SELECT * FROM persona_notes WHERE content LIKE '%tiktok%' OR content LIKE '%TikTok%' OR content LIKE '%忽略%'")
    bad_notes = cur.fetchall()
    check("無 TikTok 忽略規則", len(bad_notes) == 0, f"found: {bad_notes}")

    # 沒有會 skip TikTok 的 filter_rule
    cur.execute("SELECT * FROM filter_rules WHERE pattern LIKE '%tiktok%' OR pattern LIKE '%TikTok%'")
    bad_rules = cur.fetchall()
    check("無 TikTok skip 規則", len(bad_rules) == 0, f"found: {bad_rules}")

    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Test 4: 系統提示詞
# ══════════════════════════════════════════════════════════════════════════════

def test_system_prompt():
    print("\n── Test 4: 系統提示詞正確性 ──")
    from gemini_client import _SYSTEM_PROMPT

    # 人設：溫柔可愛，不是調皮幼稚
    check("人設包含「溫柔」", "溫柔" in _SYSTEM_PROMPT)
    check("人設包含「美玉姨」", "美玉姨" in _SYSTEM_PROMPT)
    check("人設不含「調皮」", "調皮" not in _SYSTEM_PROMPT)
    check("人設不含「嘴賤」", "嘴賤" not in _SYSTEM_PROMPT)
    check("人設不含「機車」", "機車" not in _SYSTEM_PROMPT)
    check("人設不含「欠打」", "欠打" not in _SYSTEM_PROMPT)

    # 禁止用語
    check("Rule: 不說「我跳過」", "我跳過" in _SYSTEM_PROMPT)  # 出現在「不要說」的上下文
    check("Rule: 不說「我不看」", "我不看" in _SYSTEM_PROMPT)
    check("Rule: 不反問使用者", "不要反問" in _SYSTEM_PROMPT)

    # Google Search fallback
    check("Rule: Google 搜尋 fallback", "Google 搜尋" in _SYSTEM_PROMPT)
    check("Rule: TikTok 搜尋", "TikTok" in _SYSTEM_PROMPT)

    # 言簡意賅
    check("風格: 言簡意賅", "言簡意賅" in _SYSTEM_PROMPT)
    check("Rule: 不插嘴", "不要主動插嘴" in _SYSTEM_PROMPT)


# ══════════════════════════════════════════════════════════════════════════════
# Test 5: Persona review prompt
# ══════════════════════════════════════════════════════════════════════════════

def test_persona_review():
    print("\n── Test 5: Persona review prompt ──")
    from gemini_client import _PERSONA_REVIEW_PROMPT

    check("目標人設包含「溫柔」", "溫柔" in _PERSONA_REVIEW_PROMPT)
    check("目標人設包含「美玉姨」", "美玉姨" in _PERSONA_REVIEW_PROMPT)
    check("目標人設不含「調皮」", "調皮" not in _PERSONA_REVIEW_PROMPT)
    check("目標人設不含「嘴賤」", "嘴賤" not in _PERSONA_REVIEW_PROMPT)
    check("目標人設不含「幼稚」", "幼稚" not in _PERSONA_REVIEW_PROMPT)


# ══════════════════════════════════════════════════════════════════════════════
# Test 6: chat() 空回覆重試
# ══════════════════════════════════════════════════════════════════════════════

def test_empty_reply_handling():
    print("\n── Test 6: 空回覆處理邏輯 ──")
    import inspect
    import gemini_client

    # retry 邏輯可能在 chat() 或重構後的 _chat_with_model()
    retry_fn = getattr(gemini_client, "_chat_with_model", None) or gemini_client.chat
    src = inspect.getsource(retry_fn)
    check("chat() 有空回覆重試", "empty text, retrying" in src)
    check("chat() 有重試 loop", "for attempt in range" in src)

    # burst flush 也要檢查空回覆
    import main
    src2 = inspect.getsource(main._handle_burst_flush)
    check("burst flush 檢查空回覆", "empty reply" in src2 or "not reply_text" in src2)


# ══════════════════════════════════════════════════════════════════════════════
# Test 7: Gemini 回覆品質（需要 API）
# ══════════════════════════════════════════════════════════════════════════════

BANNED_PHRASES = ["跳過", "不看", "點不開", "網頁不存在", "連結壞了", "抱歉我這次沒生出"]

# 測試 URL 清單：涵蓋所有網站類型
# JS 渲染：靠 Gemini Google Search fallback
# 一般網站：prefetch 可讀
_TEST_URLS = [
    # ── JS 渲染網站 ──
    ("https://vt.tiktok.com/ZSHsUffnc/",                           "TikTok"),
    ("https://www.dcard.tw/f/trending/p/257217382",                 "Dcard"),
    ("https://www.instagram.com/p/DGZZbhjyOx2/",                   "Instagram"),
    ("https://x.com/elikiiii_/status/1911632849082249229",          "X/Twitter"),
    ("https://www.reddit.com/r/taiwan/comments/1jutngk/",           "Reddit"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ",                 "YT Shorts"),
    # ── 一般新聞 / 靜態網站 ──
    ("https://news.pts.org.tw/article/729741",                      "公視新聞"),
    ("https://zh.wikipedia.org/wiki/%E5%8F%B0%E7%81%A3",           "Wikipedia"),
    ("https://www.cna.com.tw/news/aipl/202604140207.aspx",         "中央社"),
]


def _try_with_model_fallback(fn):
    """主 model 配額用完或持續 503 → 自動切 lite 重試，測完恢復原設定。"""
    from config import settings
    try:
        return fn()
    except Exception as e:
        err = str(e)
        if "429" not in err and "RESOURCE_EXHAUSTED" not in err and "503" not in err:
            raise
        # 主 model 配額用完或 503，切 lite 再試
        original = settings.gemini_model
        settings.gemini_model = settings.gemini_light_model
        print(f"  [INFO] 主 model 配額用完，切換至 {settings.gemini_model} 繼續測試")
        try:
            return fn()
        finally:
            settings.gemini_model = original


def _is_chinese_majority(text: str) -> bool:
    """中文字元數 >= 英文字母數才算中文為主。"""
    cn = len(re.findall(r"[\u4e00-\u9fff]", text))
    en = len(re.findall(r"[a-zA-Z]", text))
    return cn >= en


def _check_gemini_reply(url: str, name: str):
    import gemini_client
    import time

    user_input = (
        "(下面是群組裡最近累積的訊息，已經被過濾器判定值得主動回應。"
        "請根據系統指令中的規則，針對其中有查證價值或爭議點的部份做一次"
        "精簡的回應；若只是閒聊請用一句話帶過。)\n\n"
        f"{url}"
    )

    try:
        reply = _try_with_model_fallback(
            lambda: gemini_client.chat(user_input, [], [], [])
        )

        check(f"{name}: 回覆非空", bool(reply and reply.strip()), f"got: {repr(reply[:50])}")
        check(f"{name}: 長度 > 10", len(reply) > 10, f"len={len(reply)}")
        check(f"{name}: 中文為主", _is_chinese_majority(reply), f"reply: {reply[:50]}")
        check(f"{name}: 無 citation 標籤", "[cite:" not in reply and "[BROWSING_TOOL_" not in reply,
              f"殘留 tag: {reply[:80]}")

        for phrase in BANNED_PHRASES:
            check(f"{name}: 不含「{phrase}」", phrase not in reply)

        preview = reply[:120].replace("\n", " ")
        print(f"    → {preview}")

    except Exception as e:
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            print(f"  [SKIP] {name}: 主 model + lite 配額全用完")
        else:
            check(f"{name}: 無 exception", False, f"{type(e).__name__}: {err[:100]}")

    time.sleep(1)  # 避免觸發 rate limit


def test_gemini_quality():
    print("\n── Test 7: Gemini 回覆品質（全網站類型）──")
    for url, name in _TEST_URLS:
        _check_gemini_reply(url, name)


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="只跑離線測試")
    args = parser.parse_args()

    test_js_domain_detection()
    test_prefetch_no_garbage()
    test_db_clean()
    test_system_prompt()
    test_persona_review()
    test_empty_reply_handling()
    if not args.quick:
        test_gemini_quality()

    print(f"\n{'='*50}")
    print(f"TOTAL: {PASS} passed, {FAIL} failed")
    print("=" * 50)
    if FAIL == 0:
        print("All tests passed! Safe to deploy.")
    else:
        print(f"{FAIL} test(s) FAILED. Fix before deploying.")
    sys.exit(0 if FAIL == 0 else 1)
