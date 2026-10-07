"""Vision LLM 共用模組 — system prompt / post-check / prompt composer。

抽自 vision_llm.py 給 vision_llm（本機 mlx-vlm）跟 vision_cloud（Together AI）共用，
避免兩條 fallback 之間 prompt / 黑名單 / 規則 0 行為偏移。

對外：
  - _VISION_SYSTEM_PROMPT：咪寶人設 prompt（規則 0 對齊 gemini_client._CORE_PROMPT）
  - compose_prompt(user_prompt) → str
  - post_check(reply) → str
  - get_blacklists() → (echo_openers, empty_phrases)  # side-effect-free local copy
"""
from __future__ import annotations

import logging

from mibao_identity import VISION_IDENTITY_ZH
from image_reply import IMAGE_RESPONSE_MARKER, IMAGE_RESPONSE_CONTRACT
from reply_policy import NO_REPEAT_CONTRACT

logger = logging.getLogger("vision_common")

# Keep the local vision child isolated from gemini_client's module-level cloud
# SDK/client initialization. These tuples intentionally mirror the text quality
# gates used by gemini_client, but live here without credentials or network code.
_LOCAL_ECHO_OPENERS = (
    "咪寶看到", "咪寶覺得這", "我看到您", "咪寶之前提醒", "咪寶幫大家整理",
    "咪寶來幫大家", "咪寶來幫您", "咪寶明白了", "好的，咪寶", "謝謝您的提醒",
    "謝謝你的提醒", "明白了，現在是", "這個說法完全正確", "這說法完全正確",
    "完全正確喔", "您說的完全正確", "你說的完全正確", "您說得對", "你說得對",
    "這個觀念很正確", "您的觀念很正確", "這張圖片", "這張圖", "圖片中", "圖中",
    "圖裡", "從圖中", "從圖片", "可以看到", "圖片顯示", "圖片展示", "圖片介紹",
    "這是一張", "這份圖", "這份內容",
)
_LOCAL_EMPTY_PHRASES = (
    "歲月不敗美人", "真的讓人很心疼", "需要平衡多方面", "值得我們深思", "需要重視",
    "需要社會共同關注", "咪寶目前的資料庫", "咪寶的資料庫", "咪寶能查到的最新資料",
    "以咪寶能查到的最新", "咪寶資料庫只到", "咪寶目前的知識截止", "投資股市還是要參考",
    "請您留意", "建議您留意", "請您自行判斷", "咪寶沒辦法預測", "建議您參考最新",
    "請參考最新的市場", "咪寶沒有辦法", "請您查詢", "確實是一種很好的", "確實是一個很好的",
    "確實是很好的方式", "這做法很好", "這方法很好", "值得肯定的做法", "這比單純的",
    "更能全面反映", "最直接的方法", "最有效的方法", "最簡單的方式", "是最好的方式",
    "是最好的方法", "最重要的方式", "最重要的觀念", "可能是想像力太豐富了",
    "可能是您的想像力", "可能是你的想像力",
)


# ════════════════════════════════════════════════════════════════════════════
# 咪寶人設精簡版 — 給 vision LLM 用（對齊 gemini_client._CORE_PROMPT 的規則 0）
# 重點：
#   - 規則 0：第一句必須是具體判斷句，不可 echo「這張圖片展示了 / 顯示了 X」
#   - 觀察優先級：看到什麼 → 重點是什麼 → 你的判斷
#   - 繁中強制（避免 Qwen 自然偏向簡中）
#   - 黑名單對齊：禁用「咪寶看到」「咪寶覺得這」等開頭
# ════════════════════════════════════════════════════════════════════════════
_VISION_SYSTEM_PROMPT = f"""{VISION_IDENTITY_ZH}
{NO_REPEAT_CONTRACT}
你溫柔可愛、安靜乖巧、言簡意賅。
你是直接在 LINE 群組裡發訊息給朋友看，不是寫分析報告。

【絕對語言規則：只說繁體中文】
- 全文只能用繁體中文。不可出現任何簡體字（例如「记录」「类别」「数」「码」「显示」「这」「个」「样」「现」要寫成「記錄」「類別」「數」「碼」「顯示」「這」「個」「樣」「現」）。
- 圖中文字若是英文 / 簡中 / 日文，先在內部翻成繁體中文理解；不要預設轉述給群組。
- 你的中文字元數必須多於英文字母數。

【規則 0｜第一句一定是「具體判斷句」】（凌駕一切）
- 禁止用以下方式開頭（這些一律 post-check 砍掉重組）：
  ❌「這張圖片 / 這張圖 / 這份圖 / 這份內容 X」（描述式 echo）
  ❌「圖片中 / 圖中 / 圖裡 / 從圖中 / 從圖片 X」「可以看到 X」（純看圖描述）
  ❌「圖片顯示 / 圖片展示 / 圖片介紹 / 這是一張 X」（這是寫圖說，不是聊天）
  ❌「具體判斷句：」「重點：」「你的判斷：」這種列點標題（是寫分析報告）
  ❌「咪寶看到」「咪寶覺得這」「我看到您」「咪寶幫大家整理」（echo opener）
  ❌ 任何英文開頭
- 第一句必須直接是「人話 + 觀察重點 + 你的看法」。
- 沒有實質判斷、答案或新資訊時輸出空字串，不用圖片描述或摘要補位。

【內容與說話方式】
直接給根據圖片的判斷或答案，保留會影響理解的原因、限制與下一步；簡潔易讀，不湊論點或字數。
只有使用者明確要求時才分正方／反方或整合觀點；不要自行增加固定段落。
圖片描述與 OCR 只供內部理解，不輸出解析摘錄、內部推理、規則檢查或不回覆的理由。
資料不足時誠實指出限制，不編造事實、數字、機構或 URL。
查證材料用來支持判斷；只有使用者明確要求來源時才列已提供且可核實的來源，不用模糊機構名稱冒充查證。

【示範｜這就是咪寶的說話樣子】

[範例 1：股票交割截圖]
成本欄有缺漏時，不能只用畫面上的總損益判斷實際報酬。
先補齊缺漏，再核對手續費與已實現、未實現損益是否分開計算。

[範例 2：股票 K 線圖]
單日站上短期均線還不足以確認趨勢反轉。
還要核對後續能否守住、成交量是否配合；單張截圖不足以判斷完整走勢。

[範例 3：保單條款]
短期需要用到這筆錢，就要先評估提前解約的損失。
用預計持有年限的解約金對照累積保費，不能只看最後一年的數字。

[範例 4：健康知識截圖]
建議補充量 100-200mg 那個有點誤導耶。
正常飲食每天就攝取 1000-1500mg 色胺酸，多數人不缺。

牛奶+麥片那個搭配是真的有道理，碳水推胰島素把競爭血腦屏障的 BCAA 拉走。
但香蕉助眠是迷思，每根才 11mg 不到牛奶 1/10。
真要助眠改睡前 1.5h 喝牛奶+燕麥。

5-HTP 補充劑要小心，跟 SSRI 抗憂鬱藥同服會血清素症候群。


【禁止】
- 「這張圖很清楚 / 很完整 / 很重要」這類空話單獨一句
- 「需要重視」「值得深思」「請您留意」「希望對您有幫助」「以上僅供參考」這種敷衍
- 「咪寶目前的資料庫」「咪寶能查到的最新」這種假裝 retrieval
- 結尾加「請自行判斷」「投資有風險」這種免責罐頭

【風格】
- 短句分行，像 LINE 訊息，不要寫成段落報告
- 語助詞（喔、耶、啦）偶爾自然出現就好，不堆疊
- emoji 偶爾用，不要多
"""


def get_blacklists() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return local quality gates without importing cloud client modules."""
    return _LOCAL_ECHO_OPENERS, _LOCAL_EMPTY_PHRASES


_HEADER_PREFIXES = (
    "具體判斷句：", "具體判斷句:",
    "重點：", "重點:",
    "你的判斷：", "你的判斷:",
    "判斷：", "判斷:",
    "看法：", "看法:",
    "我的看法：", "我的看法:",
    "我的判斷：", "我的判斷:",
    "結論：", "結論:",
)


_CLAUSE_BREAKS = "，,。．！!：:；;\n"


def post_check(reply: str) -> str:
    """對齊 gemini_client._violates_quality 的精簡版。

    偵測順序：
    1. header prefix（「具體判斷句：」之類）→ 砍到 colon 後第一個非空字元
    2. echo opener → opener 自成一個子句（「您說得對，」）就拿掉；否則留原文 + log
    3. empty phrase → 留原文 + log

    2026-10-07 前第 2 步會砍掉前 20 幾個字再補「從圖裡看，」：砍在句子中間
    （「10月2日」變「月2日」），剩下的還是在描述圖。描述式開頭（「圖片中顯示…」）
    現在保留原文，有沒有糾正或建議交給 image_reply.unsolicited_image_reply 判斷；
    硬拿掉「圖上寫」這類開頭會讓圖上的說法看起來像咪寶自己的主張。
    """
    s = (reply or "").strip()
    if not s:
        return s

    # 0. 全形/半形空白統一一下，方便比對
    for header in _HEADER_PREFIXES:
        if s.startswith(header):
            stripped = s[len(header):].lstrip("，。、,. \n　")
            logger.info("post_check: header prefix hit (%s) → 直接砍掉", header)
            return stripped if stripped else s

    echo_openers, empty_phrases = get_blacklists()

    # 1. echo opener：開頭命中
    for opener in echo_openers:
        if s.startswith(opener):
            rest = s[len(opener):]
            if rest[:1] and rest[0] in _CLAUSE_BREAKS:
                logger.info("post_check: echo opener clause dropped (%s)", opener)
                return rest.lstrip(_CLAUSE_BREAKS + " 　")
            logger.info("post_check: echo opener hit (%s) — 保留原文未重組", opener)
            return s

    # 2. empty phrase：句中命中 → 不重組（會變更語意），記 log 即可
    for phrase in empty_phrases:
        if phrase in s:
            logger.info("post_check: empty phrase hit (%s) — 保留原文未重組", phrase)
            break

    return s


def compose_prompt(user_prompt: str) -> str:
    """把咪寶人設 prompt 拼到 user prompt 前面。"""
    up = (user_prompt or "").strip() or "請針對內容給實質回應；沒有可補充內容就輸出空字串。"
    if IMAGE_RESPONSE_MARKER in up:
        # Image tasks use an answer-only prompt; multi-frame video keeps its own prompt.
        return f"{VISION_IDENTITY_ZH}\n{IMAGE_RESPONSE_CONTRACT}\n\n【本次任務】{up}"

    return (
        f"{_VISION_SYSTEM_PROMPT}\n\n"
        f"【本次任務】{up}\n\n"
        "（用繁體中文，像 LINE 訊息那樣短句分行。"
        "不要寫「具體判斷句:」「重點:」這種空泛標題。"
        "只有使用者明確要求時才分正方／反方或列來源；不要附圖片解析或內部流程。）"
    )
