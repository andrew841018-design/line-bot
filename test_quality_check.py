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

    print(f"\n{'=' * 50}")
    print(f"TOTAL: {PASS} passed, {FAIL} failed")
    print("=" * 50)
    if FAIL:
        print("Some tests FAILED.")
        sys.exit(1)
    else:
        print("All tests passed!")
