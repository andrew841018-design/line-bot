"""
Tests for post-reply quality validator (規則 0 enforcement).

Covers:
- _violates_quality detects echo openers
- _violates_quality detects empty phrases
- _violates_quality passes clean replies
- chat() retries once when violation detected
- chat() logs to persona_notes + sends Discord DM if retry still violates
"""

import os
import sys
from unittest.mock import MagicMock, patch

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import gemini_client  # noqa: E402

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
# Test A: _violates_quality basic detection
# ═══════════════════════════════════════════════════════════════════════════════
def test_violates_quality_echo_openers():
    print("\n── Test A: _violates_quality echo openers ──")

    bad, reason = gemini_client._violates_quality("咪寶看到大家在討論虱目魚")
    check("echo: 咪寶看到 → True", bad)
    check("echo reason mentions opener", "echo opener" in reason)

    bad, reason = gemini_client._violates_quality("咪寶覺得這個議題真的很重要")
    check("echo: 咪寶覺得這 → True", bad)

    bad, reason = gemini_client._violates_quality("我看到您分享了這篇新聞")
    check("echo: 我看到您 → True", bad)

    bad, reason = gemini_client._violates_quality("咪寶之前提醒過大家要小心")
    check("echo: 咪寶之前提醒 → True", bad)

    bad, reason = gemini_client._violates_quality("咪寶幫大家整理一下")
    check("echo: 咪寶幫大家整理 → True", bad)

    bad, reason = gemini_client._violates_quality("咪寶來幫大家查一下這個資訊")
    check("echo: 咪寶來幫大家 → True", bad)

    bad, reason = gemini_client._violates_quality("  咪寶看到了喔（前面有空白）")
    check("echo: 開頭空白也偵測得到", bad)


def test_violates_quality_empty_phrases():
    print("\n── Test B: _violates_quality empty phrases ──")

    bad, reason = gemini_client._violates_quality(
        "這個論點很有趣，歲月不敗美人，這就是答案"
    )
    check("empty: 歲月不敗美人 → True", bad)
    check("empty reason mentions phrase", "empty phrase" in reason)

    bad, _ = gemini_client._violates_quality(
        "這個案例真的讓人很心疼，所以要小心"
    )
    check("empty: 真的讓人很心疼 → True", bad)

    bad, _ = gemini_client._violates_quality("需要平衡多方面的觀點才能下判斷")
    check("empty: 需要平衡多方面 → True", bad)

    bad, _ = gemini_client._violates_quality("這個議題值得我們深思")
    check("empty: 值得我們深思 → True", bad)

    bad, _ = gemini_client._violates_quality("這個問題需要重視，光罰錢不夠")
    check("empty: 需要重視 → True", bad)


def test_violates_quality_clean():
    print("\n── Test C: _violates_quality clean reply passes ──")

    ok_reply = (
        "我這邊覺得問題不在個人疏失，而在制度沒擋。\n"
        "- 警方無公定疲勞駕駛判定標準\n"
        "結論：應推工時上限制度。"
    )
    bad, reason = gemini_client._violates_quality(ok_reply)
    check("clean reply → False", not bad)
    check("clean reply reason 為空", reason == "")

    short_take = "這個說法不對，實際的數字是 10%。"
    bad, _ = gemini_client._violates_quality(short_take)
    check("具體判斷句 → False", not bad)


# ═══════════════════════════════════════════════════════════════════════════════
# Test D: chat() retry on violation
# ═══════════════════════════════════════════════════════════════════════════════
def test_chat_retries_on_violation():
    print("\n── Test D: chat() retries once on violation ──")

    # First reply 違規 (echo opener)，retry 後乾淨
    bad_response = MagicMock()
    bad_response.text = "咪寶看到大家分享了新聞"
    bad_response.usage_metadata = MagicMock(total_token_count=10, thinking_token_count=0)
    bad_response.candidates = []

    good_response = MagicMock()
    good_response.text = "這篇新聞的問題在於統計方法不對，實際比例是 10%。"
    good_response.usage_metadata = MagicMock(total_token_count=10, thinking_token_count=0)
    good_response.candidates = []

    mock_chat = MagicMock()
    mock_chat.send_message.side_effect = [bad_response, good_response]

    with (
        patch("gemini_client._client") as mock_client,
        patch.object(gemini_client, "_track_usage"),
    ):
        mock_client.chats.create.return_value = mock_chat
        result = gemini_client.chat("test", [], [])

    check("send_message 被呼叫兩次（原始 + retry）", mock_chat.send_message.call_count == 2)
    check("retry 後乾淨回覆被回傳", "問題在於統計方法" in result)
    # 第二次 call 應該包含 strict 提示
    second_call_args = mock_chat.send_message.call_args_list[1][0][0]
    check(
        "retry prompt 含『規則 0』",
        "規則 0" in second_call_args,
    )


def test_chat_logs_when_retry_still_violates():
    print("\n── Test E: chat() logs + alerts when retry still violates ──")

    # 兩次都違規
    bad1 = MagicMock()
    bad1.text = "咪寶看到大家在討論這件事"
    bad1.usage_metadata = MagicMock(total_token_count=10, thinking_token_count=0)
    bad1.candidates = []

    bad2 = MagicMock()
    bad2.text = "咪寶覺得這個案例值得我們深思"
    bad2.usage_metadata = MagicMock(total_token_count=10, thinking_token_count=0)
    bad2.candidates = []

    mock_chat = MagicMock()
    mock_chat.send_message.side_effect = [bad1, bad2]

    with (
        patch("gemini_client._client") as mock_client,
        patch.object(gemini_client, "_track_usage"),
        patch.object(gemini_client, "_log_quality_violation") as mock_log,
        patch.object(gemini_client, "_alert_quality_violation") as mock_alert,
    ):
        mock_client.chats.create.return_value = mock_chat
        result = gemini_client.chat("test", [], [], group_id="GRP_TEST")

    check("retry 後仍違規 → 仍回傳結果（不阻塞）", result.startswith("咪寶覺得"))
    check("_log_quality_violation 被呼叫", mock_log.called)
    check("_alert_quality_violation 被呼叫", mock_alert.called)


def test_chat_clean_reply_passes_through():
    print("\n── Test F: clean reply 不 retry ──")

    good = MagicMock()
    good.text = "這個說法是錯的，正確答案是 X，因為 Y。"
    good.usage_metadata = MagicMock(total_token_count=10, thinking_token_count=0)
    good.candidates = []

    mock_chat = MagicMock()
    mock_chat.send_message.return_value = good

    with (
        patch("gemini_client._client") as mock_client,
        patch.object(gemini_client, "_track_usage"),
    ):
        mock_client.chats.create.return_value = mock_chat
        result = gemini_client.chat("test", [], [])

    check("乾淨回覆 → 只 call 一次", mock_chat.send_message.call_count == 1)
    check("乾淨回覆原樣回傳", "這個說法是錯的" in result)


# ═══════════════════════════════════════════════════════════════════════════════
# Test G: _detect_rule_packs expanded to news/case keywords
# ═══════════════════════════════════════════════════════════════════════════════
def test_detect_rule_packs_news_case():
    print("\n── Test G: _detect_rule_packs 觸發 news/case 規則 ──")

    packs = gemini_client._detect_rule_packs("這篇新聞報導了高雄疲勞駕駛事件")
    check("『新聞』→ 觸發 news/case pack", gemini_client._RULE_NEWS_CASE in packs)

    packs = gemini_client._detect_rule_packs("這個保險方案怎麼選")
    check("『保險』→ 觸發 news/case pack", gemini_client._RULE_NEWS_CASE in packs)

    packs = gemini_client._detect_rule_packs("關於這個醫療案例的研究")
    check("『醫療』『研究』『案例』→ 觸發", gemini_client._RULE_NEWS_CASE in packs)

    # 純家常不觸發
    packs = gemini_client._detect_rule_packs("我先吃飯了")
    check("純家常不觸發 news/case", gemini_client._RULE_NEWS_CASE not in packs)


# ═══════════════════════════════════════════════════════════════════════════════
# Test H: news/case sectional structure (正方+反方+綜合) — 2026-05-17 加
# 規則 0 延伸：_violates_quality 要擋住缺正/反方 + 綜合段、URL <3、單一網域、字數 <350
# ═══════════════════════════════════════════════════════════════════════════════

_GOOD_NEWS_REPLY = """我這邊覺得這份保險條款設計有問題，問題在於條款本身刻意模糊、把專業術語包裝成消費者看不懂的語言，讓客戶在簽約當下根本沒辦法判斷自己會被排除哪些理賠情境。

正方（同意的部分）：
- 同意保戶責任這點 — 因為金管會二零二四年壽險業務統計顯示，所有爭議案件中有六成八都來自條款解釋分歧，這代表業界跟保戶對同一條款的理解確實有落差（出處：https://fsc.gov.tw/report2024）
- 同意產業自律的方向 — 美國保險監理官協會明確規定，保單條款必須通過 plain-language 可讀性測試才能上架，這個機制已經運作十年（出處：https://naic.org/plain-language）
- 同意揭露機制要強化 — 日本金融廳要求壽險公司每年抽測一般民眾對條款的理解度，沒過就要重寫條款（出處：https://fsa.go.jp/insurance-disclosure）

反方（質疑的部分）：
- 反對只罰保戶 — 金融消費評議中心年報顯示實際補償率不到三成，代表現行機制偏向業者，光要消費者自己讀懂條款是把問題倒因為果（出處：https://foi.org.tw/annual-report）
- 反對自律就有效 — 美國二零二三年保險申訴件數仍年增百分之十二，自律不能取代外部審查（出處：https://consumerwatch.org/insurance-2023）
- 反對只用單一審查機制 — 歐盟採取雙軌制兼顧業者自審與監理單位抽審，台灣若只走自律路線會跟歐盟標準脫節（出處：https://eiopa.europa.eu/regulations）

綜合判斷：條款設計的結構性問題比保戶責任的個別疏失問題大三倍以上，應該推動可讀性審查機制配合揭露透明化雙軌制。光罰消費者沒看清楚是治標不治本，最終建議產業跟政府共擔審查責任，並引入歐盟雙軌的外部抽審機制當作補強，這樣才能真正降低未來的爭議件數。"""


def test_violates_quality_news_case_missing_sectional_structure():
    print("\n── Test H1: news case 缺正方/反方/綜合 sectional 結構 → violate ──")

    # 字數夠、URL 夠、有觀點 marker，但沒有正方/反方/綜合段
    reply = (
        "我覺得這份保險條款設計有問題，主要的問題在於條款本身刻意模糊，"
        "消費者根本看不懂。" + "保戶權益必須被保障。" * 30 +
        "來源：\nhttps://fsc.gov.tw/a\nhttps://naic.org/b\nhttps://fsa.go.jp/c"
    )
    bad, reason = gemini_client._violates_quality(
        reply, user_input_text="這份保險條款合理嗎"
    )
    check("缺正方/反方/綜合段結構 → violate", bad)
    check("reason 提到 sectional",
          "正方" in reason or "反方" in reason or "綜合" in reason or "結構" in reason)


def test_violates_quality_news_case_complete_passes():
    print("\n── Test H2: 完整 reply (sectional + URL≥3 + domains≥2 + 字數≥350) → pass ──")
    bad, reason = gemini_client._violates_quality(
        _GOOD_NEWS_REPLY, user_input_text="保險條款合理嗎"
    )
    check(f"完整 reply 應 pass，got reason={reason!r}", not bad)


def test_violates_quality_news_case_url_threshold_bumped_to_3():
    print("\n── Test H3: URL 門檻提高到 3 條 ──")
    # 完整 sectional + 字數夠，但只 2 條 URL
    reply = (
        "我這邊覺得問題在 X。\n正方：同意 A B C。\n反方：反對 D E F。\n"
        "綜合判斷：權衡後支持 X。" + "理由如下這是一段長文。" * 50 +
        "\n來源：https://a.com https://b.com"
    )
    bad, reason = gemini_client._violates_quality(
        reply, user_input_text="保險"
    )
    check(f"URL 只 2 條應 violate (門檻 3)，got reason={reason!r}", bad)


def test_violates_quality_news_case_single_domain_violates():
    print("\n── Test H4: 3 條 URL 但全是同一網域 → violate ──")
    reply = (
        "我覺得問題在 X。\n正方：同意 ABC。\n反方：反對 DEF。\n綜合判斷：權衡後選 X。"
        + "詳細理由如下這是補字數。" * 50 +
        "\n來源：https://a.com/1 https://a.com/2 https://a.com/3"
    )
    bad, reason = gemini_client._violates_quality(
        reply, user_input_text="保險"
    )
    check(f"URL 集中單網域 → violate，got reason={reason!r}", bad)


def test_violates_quality_news_case_too_short_violates():
    print("\n── Test H5: 字數 < 350 → violate ──")
    # 有 sectional + 3 URL + 多網域，但太短
    reply = (
        "我覺得問題在 X。正方：同意 A B C。反方：反對 D E F。綜合判斷：選 X。"
        "https://a.com https://b.com https://c.com"
    )
    bad, reason = gemini_client._violates_quality(
        reply, user_input_text="保險"
    )
    check(f"字數 < 350 → violate，got reason={reason!r}", bad)


def test_violates_quality_non_news_case_skips_sectional_check():
    print("\n── Test H6: 非 news/case 議題不適用 sectional check ──")
    # 純閒聊回覆，沒 URL 沒 sectional，但 user_input 不含 news/case 觸發詞
    reply = "好啊，我等等過去。"
    bad, _ = gemini_client._violates_quality(reply, user_input_text="幾點出發")
    check("純閒聊不應觸發 sectional check", not bad)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    test_violates_quality_echo_openers()
    test_violates_quality_empty_phrases()
    test_violates_quality_clean()
    test_chat_retries_on_violation()
    test_chat_logs_when_retry_still_violates()
    test_chat_clean_reply_passes_through()
    test_detect_rule_packs_news_case()
    test_violates_quality_news_case_missing_sectional_structure()
    test_violates_quality_news_case_complete_passes()
    test_violates_quality_news_case_url_threshold_bumped_to_3()
    test_violates_quality_news_case_single_domain_violates()
    test_violates_quality_news_case_too_short_violates()
    test_violates_quality_non_news_case_skips_sectional_check()

    print(f"\n{'=' * 50}")
    print(f"TOTAL: {PASS} passed, {FAIL} failed")
    print("=" * 50)
    if FAIL:
        print("Some tests FAILED.")
        sys.exit(1)
    else:
        print("All tests passed!")
