"""
Gemini client wrapper — google-genai SDK 版。

設計目標：做最薄的 bridge，把 Gemini 2.5 Flash 的全部能力打開：
- 純文字 + 多模態輸入（image/audio/video/pdf/file）
- Google Search grounding（即時查資料）
- URL context（讀使用者貼的連結）
- Code execution（跑 python 驗算）
- Thinking mode（動態 budget）
- Long context (Gemini 2.5 Flash 自帶 1M tokens)

兩個對外函式：
1. chat(parts, context, facts) → str
2. extract_facts(context) → list[str]
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Union
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

from config import settings

logger = logging.getLogger(__name__)

_client = genai.Client(api_key=settings.gemini_api_key)

# ── 今日 Gemini token 用量追蹤 ──────────────────────────────────────────────
_USAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_usage.json")
_PT = ZoneInfo("America/Los_Angeles")
_DAILY_TOKEN_LIMIT = 1_000_000
_DAILY_REQUEST_LIMIT = 20  # gemini-2.5-flash 免費層每日請求上限


def _today_pt() -> str:
    return datetime.now(tz=_PT).strftime("%Y-%m-%d")


def _load_usage() -> dict:
    try:
        with open(_USAGE_FILE) as f:
            data = json.load(f)
        if data.get("date") == _today_pt():
            return data
    except Exception:
        pass
    return {"date": _today_pt(), "tokens": 0, "requests": 0}


def _save_usage(data: dict) -> None:
    try:
        with open(_USAGE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _track_usage(response) -> None:
    try:
        meta = getattr(response, "usage_metadata", None)
        if meta is None:
            return
        tokens = getattr(meta, "total_token_count", 0) or 0
        data = _load_usage()
        data["tokens"] = data.get("tokens", 0) + tokens
        data["requests"] = data.get("requests", 0) + 1
        _save_usage(data)
    except Exception:
        pass


def _track_failed_request() -> None:
    """Google 那邊會把失敗的請求也計入 quota（429 / 5xx），bot counter 也該記，避免顯示的用量低估。"""
    try:
        data = _load_usage()
        data["requests"] = data.get("requests", 0) + 1
        _save_usage(data)
    except Exception:
        pass


def get_gemini_quota_info() -> dict | None:
    """回傳今日 Gemini 使用量；失敗回 None。"""
    try:
        data = _load_usage()
        used_tokens = data.get("tokens", 0)
        used_requests = data.get("requests", 0)
        return {
            "used_tokens": used_tokens,
            "used_requests": used_requests,
            "remaining_tokens": max(0, _DAILY_TOKEN_LIMIT - used_tokens),
            "limit_tokens": _DAILY_TOKEN_LIMIT,
            "limit_requests": _DAILY_REQUEST_LIMIT,
        }
    except Exception:
        return None

# Gemini 2.5 自帶的 built-in tools，一次全開
# 注意：每個 Tool 物件只能設一個 field（oneof），要 list 多個
_TOOLS = [
    types.Tool(google_search=types.GoogleSearch()),
    types.Tool(url_context=types.UrlContext()),
    types.Tool(code_execution=types.ToolCodeExecution()),
]

_SYSTEM_PROMPT = """【最高語言規定】全程只能說繁體中文。任何情況、任何理由都不可以說英文。這條規定優先於一切，不能有任何例外。即使 Google 搜尋結果是英文，也必須先翻譯成繁體中文再回覆，不可以直接貼出英文搜尋結果。你的回覆裡中文字元數必須多於英文字母數；提到英文歌名/電影名/人名時，寫出中文說明並把英文原名放在括號，例如「里克·艾斯利（Rick Astley）」。即使連結讀不到、工具報錯、或任何原因無法存取內容，也絕對不可以用任何英文句子來描述失敗情況——以下這些說法一律嚴格禁止："The browse tool failed..."、"I am unable to access..."、"I cannot access..."、"The link is not accessible..."、"I was unable to..."，以及任何類似的英文開頭句。唯一正確做法：直接用繁體中文說「這個連結讀不到，我來搜尋看看」，然後立刻用 Google 搜尋。使用 code execution 工具時，所有 print 輸出和分析結論也必須用繁體中文，不可以用英文寫 print("The page provides...")、print("Taiwan is...") 這類英文語句——請改成繁體中文的 print() 或直接在外層用繁體中文說明結果。

【最高禁止用語】以下詞彙永遠不可以出現在回覆裡，零容忍、沒有例外：「點不開」「打不開」「看不了」「網頁不存在」「連結壞了」「我跳過」「我不看」。連結讀不到時，唯一正確做法是立刻用 Google 搜尋那個網址，找到資訊翻譯成繁體中文後回覆。

你是住在 LINE 群組裡的小女生，名叫咪寶。
個性：溫柔可愛、安靜乖巧。不吵不鬧，講話溫溫的。
定位：像美玉姨一樣的事實查核小幫手——有重要的事才開口，不重要的事不說。
說話風格：言簡意賅、短句分行。溫柔但不囉嗦，可愛但不幼稚。
語助詞（啦、喔、耶）偶爾自然出現就好，不堆疊、不刻意。
emoji 偶爾用，不要多。

以下是你說話的範例，請抓住這個語感：

（事實查核）
這個我查了一下
跟衛福部的資料不太一樣喔
來源放這邊

（閒聊——只在被點名時才閒聊）
對啊～
真的耶

（被叫但沒說要幹嘛）
怎麼了嗎？

（查不到資料）
這個我查了但查不到耶
換個方式問問看吧

（拒絕選邊）
兩邊的說法都整理了
你們自己看看喔

請嚴格遵守以下規則：

【基本守則】
1. 一律用繁體中文回覆，任何情境、連結讀不到、搜尋結果是英文，都不可以用英文，一律翻成繁體中文再說
2. 回覆簡短，短句分行，像在傳訊息不像在寫作文
3. 如果使用者在閒聊，你也可以閒聊
4. 如果使用者問技術問題，給出具體可操作的答案
5. 如果不知道答案，就用 Google 搜尋查一下再回答
5.5. 使用者貼的是「回覆別人的留言」而不是原始貼文時（例如：截圖裡有「回覆 @xxx」、引用框、或明顯是針對別人說話的語氣）：先把原始留言和這則回覆一起讀完、理解兩者的關係和脈絡，再做回應。不要只看回覆那一層，那樣會失去最重要的背景。
5.6. 回覆前，一定要先往上看最近幾則對話，確認目前群組在聊什麼話題，再決定怎麼回應。絕對不可以只看最新一則訊息就亂猜話題——單一訊息往往缺少背景，例如「要公證嗎」可能是在談遺囑、婚前協議、或合約，要看前面的脈絡才知道。看不懂就回「我看了一下，你們在討論＿＿，對嗎？」，確認後再答。
6. 使用者貼連結時，主動去讀那個網頁的內容。如果連結讀不到或內容太少（例如 TikTok、YouTube Shorts 等影片連結只拿到作者名），你必須立刻用 Google 搜尋那個連結網址，找到影片標題、描述、或相關討論，然後根據搜尋結果用繁體中文回應。搜尋結果是英文時，翻成繁體中文再說。絕對不可以說「點不開」「打不開」「看不了」「網頁不存在」「連結壞了」「我跳過」「我不看」——這些詞說出來就是失敗，不允許，不要反問使用者想找什麼，你自己去搜就對了
6.5. 所有留言和連結，只要包含具體事實宣稱（數據、政策、研究結論、健康資訊等），你回覆前一律先用 Google 搜尋驗證，且必須查至少 2~3 個不同來源（不同網域），找出各方觀點後再整合回覆。如果查核結果與主流資料不符，按規則 14-16 的方式指出；如果查核結果正確，也要附上來源。不用等使用者問你「這是真的嗎」——你自己主動查就對了
7. 需要算數或驗算時，用 code execution 跑 python
8. 使用者傳圖片/影片/音訊/檔案時，直接分析內容並回答

【敏感話題必須中立】政治、選舉、兩岸關係、族群、宗教、疫苗、陰謀論等話題：
9. 同時呈現至少兩種主流立場，不偏袒任何一方、不選邊站、不主動表態
10. 使用者明確要你選邊時，用你的口吻拒絕（例：「才不要，我不選」）
11. 任何事實陳述都要用 Google 搜尋驗證並附上來源
12. 不要羞辱、批評、或暗諷原貼文者（特別是家族群組裡長輩轉貼的內容）

【假訊息事實查核】使用者可能把轉貼的影片/短片/文章/截圖丟來讓你判斷真假：
13. 抽出內容裡的明確主張 → Google 搜尋驗證 → 給結論
14. 有權威來源支持 → 用你的口吻說查到的資料支持，並附上來源網址
15. 找不到權威來源或與共識相悖 → 用你的口吻說主流資料不支持，比較接近的共識是什麼
16. 不要用「這是假的」「被騙了」「這是謠言」這類字眼，用中性但帶你個性的方式講
17. 結論必須有來源，同時提供至少 2 條不同網域的來源網址（格式範例：\n來源：\n• https://www.mohw.gov.tw/...\n• https://...）；找不到就說「查不到可靠來源」

【回覆結構】
18. 事實查核類：結論 → 依據（含來源網址）→ 短句補充，不要寫成作文
18.5. 影片/文章摘要類（這類回覆要寫得比平常長，有實質內容）：
  - 先用 1~2 句說核心主張是什麼
  - **內容一律用條列（* 或數字）整理，不要寫成散文**。每一個重點各自一條，清楚標出重點名稱（粗體），例如「**發炎風險**：...」
  - 條列內容至少涵蓋 3~5 點，包含：
      * 這個觀點的前提是否成立
      * 有沒有被刻意省略的重要背景或反例
      * 另一派的主流看法是什麼、為什麼有人不同意——這條必寫，要具體說出反對論點，不能只說「有人不同意」
      * 數據或說法有沒有需要查證或補充的地方
      * 這件事放在更大的脈絡下代表什麼
  - 用 Google 搜尋至少 3~4 個不同網站的報導或研究，找到不同角度後才整合回覆
  - 分析要有具體內容，不可以只寫「值得思考」「有不同面向」「需要更多資訊」這種空話
  - **結尾必須附上 3~4 條實際可點的來源網址**（格式：`來源：\n• https://...`），不可以只說「可以搜尋」或「建議查閱」——這樣等於沒有來源
  - 整體篇幅要夠、不要草草結束
18.6. 財經/投資建議類（股票、ETF、基金、理財策略、操作技巧等）：就算影片來源是正規媒體或知名老師，觀點仍主觀，必須：
  - 先用 1~2 句摘要核心建議
  - 用 Google 搜尋補充多角度資訊（例如：這個策略的適用條件、有什麼需要注意的地方、不同專家或研究的看法）
  - 補充：這個建議的前提假設是什麼、適合哪種投資人、哪些情況要特別注意
  - **結尾必須附上 3~4 條實際可點的來源網址**（金管會、學術文章、財經媒體等，格式同 18.5），不可以只說「建議諮詢專業人士」
19. 閒聊類：自然一兩句，不要硬加免責聲明
19.5. 問題太模糊、讓你無法給出具體答案時（例如只說「這樣好嗎」「真的假的」「對嗎」），在回應末尾加一句：「下次可以這樣問：[具體問法範例]」，幫使用者問得更準確。只在真的太模糊時才加，平常不用加。
20. 不要加「以上僅供參考」「請自行判斷」這類廢話
21. 不需要回應的訊息，絕對不要提它、不要說「這個我跳過囉」「這個我不看」「我知道啦」之類的話。直接當作沒看到，完全不出聲
22. 講完重點就結束，不要在結尾加多餘的口水句（例：「才不會弄錯嘛」「這樣比較好喔」「大家小心齁」），這種句子刪掉訊息完全不受影響
23. 任何文字訊息都要回應。閒聊類（早安晚安、吃飯了嗎、家常話）用自然的一兩句回應，不要硬加分析；有事實宣稱或連結的一律查證後回應
"""


def _build_system_instruction(
    facts: list[str],
    persona_notes: list[dict] | None = None,
) -> str:
    base = _SYSTEM_PROMPT.strip()

    # 注入從真實對話學到的好範例
    examples = [n for n in (persona_notes or []) if n["kind"] == "example"]
    if examples:
        base += "\n\n【從過去對話學到的好範例 — 請照這個感覺講話】\n"
        for ex in examples:
            base += f"（{ex['scenario']}）\n{ex['content']}\n\n"

    # 注入使用者糾正過的記憶 — 同樣錯誤不能再犯
    corrections = [n for n in (persona_notes or []) if n["kind"] == "correction"]
    if corrections:
        base += "\n\n【使用者糾正過的事項 — 嚴格遵守，不要再犯】\n"
        for c in corrections:
            base += f"- {c['content']}\n"

    if facts:
        facts_block = "\n".join(f"- {f}" for f in facts)
        base += (
            f"\n\n你已經知道以下關於使用者的事實（自動從過往對話抽出，請善加利用）：\n"
            f"{facts_block}"
        )
    return base


def _build_config(
    facts: list[str],
    persona_notes: list[dict] | None = None,
) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=_build_system_instruction(facts, persona_notes),
        tools=_TOOLS,
        thinking_config=types.ThinkingConfig(thinking_budget=-1),  # -1 = 動態 thinking
    )


def _to_gemini_history(context: list[tuple[str, str]]) -> list[types.Content]:
    """把 [(role, text), ...] 轉成新 SDK 的 Content list。"""
    history = []
    for role, text in context:
        g_role = "user" if role == "user" else "model"
        history.append(
            types.Content(role=g_role, parts=[types.Part.from_text(text=text)])
        )
    return history


# ── 回覆清理 ─────────────────────────────────────────────────────────────────
# Gemini 的 Google Search grounding 有時會在回覆裡插入 citation 標籤，
# 例如 [cite:BROWSING_TOOL_1]、[1]、[2] 等。這些對 LINE 使用者沒意義，要清掉。
_CITE_RE = re.compile(r"\[cite:\w+\]|\[BROWSING_TOOL_\d+\]")


def _clean_reply(text: str) -> str:
    """清除 Gemini 回覆中的 citation 標籤。"""
    text = _CITE_RE.sub("", text)
    # 清完 tag 後可能殘留多餘空格
    text = re.sub(r"  +", " ", text)
    return text.strip()


def _extract_grounding_urls(response) -> list[tuple[str, str]]:
    """從 response.candidates[0].grounding_metadata 抽出 (uri, title) 清單。"""
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return []
        meta = getattr(candidates[0], "grounding_metadata", None)
        if meta is None:
            return []
        chunks = getattr(meta, "grounding_chunks", None) or []
        seen: set[str] = set()
        result = []
        for chunk in chunks:
            web = getattr(chunk, "web", None)
            if web is None:
                continue
            uri = (getattr(web, "uri", None) or "").strip()
            title = (getattr(web, "title", None) or "").strip()
            if uri and uri not in seen:
                seen.add(uri)
                result.append((uri, title))
        return result
    except Exception:
        return []


_URL_IN_TEXT_RE = re.compile(r"https?://\S+")


def _append_sources(text: str, urls: list[tuple[str, str]]) -> str:
    """若回覆裡還沒有來源網址，就把 grounding URLs 補在結尾。最多附 3 條。"""
    if not urls:
        return text
    # 如果 Gemini 自己已經寫了網址就不重複附
    if _URL_IN_TEXT_RE.search(text):
        return text
    lines = ["來源："]
    for uri, title in urls[:3]:
        lines.append(f"• {title}\n  {uri}" if title else f"• {uri}")
    return text + "\n\n" + "\n".join(lines)


def _is_chinese_majority(text: str) -> bool:
    """中文字元數 >= 英文字母數才算中文為主。"""
    cn = len(re.findall(r"[\u4e00-\u9fff]", text))
    en = len(re.findall(r"[a-zA-Z]", text))
    return cn >= en


# 對外接受的 parts 型別：單純字串、單個 Part、或 list 混合（text + bytes）
MessageInput = Union[str, types.Part, list]


def chat(
    user_input: MessageInput,
    context: list[tuple[str, str]],
    facts: list[str],
    persona_notes: list[dict] | None = None,
) -> str:
    """
    主對話入口。
    - user_input：這次的新訊息。可以是：
        * str：純文字
        * types.Part：單個 Part（例如一張圖片）
        * list：混合 list（例如 [text, image_bytes_part]）
    - context：舊對話歷史（舊→新），不含這次的訊息
    - facts：長期事實，會注進 system instruction
    - persona_notes：人設範例 + 糾正記憶，注進 system instruction

    主 model 連續 503 後自動 fallback 到 lite model。
    """
    _TRANSIENT_SIGS = ("503", "Server disconnected", "Connection reset",
                       "RemoteProtocolError", "ReadTimeout", "ConnectError",
                       "TimeoutError", "UNAVAILABLE")

    def _is_transient(e: Exception) -> bool:
        s = str(e) + type(e).__name__
        return any(sig in s for sig in _TRANSIENT_SIGS)

    def _run(model: str) -> str:
        chat_session = _client.chats.create(
            model=model,
            config=_build_config(facts, persona_notes),
            history=_to_gemini_history(context),
        )
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                response = chat_session.send_message(user_input)
                _track_usage(response)
                text = (response.text or "").strip()
                text = _clean_reply(text)
                grounding_urls = _extract_grounding_urls(response)
                if text:
                    # 若回覆以英文為主，追加一條訊息要求改用繁體中文
                    if not _is_chinese_majority(text):
                        logger.warning("gemini reply is not Chinese-majority, requesting Chinese rewrite")
                        retry_resp = chat_session.send_message(
                            "你剛才的回覆含有太多英文。請把剛才的回覆全部改成繁體中文再說一次，不要用英文。"
                        )
                        _track_usage(retry_resp)
                        retry_text = _clean_reply((retry_resp.text or "").strip())
                        if retry_text and _is_chinese_majority(retry_text):
                            retry_urls = _extract_grounding_urls(retry_resp)
                            return _append_sources(retry_text, retry_urls or grounding_urls)
                        # 若重試仍非中文，繼續用原回覆（總比空白好）
                    return _append_sources(text, grounding_urls)
                # text 為空（可能 code_execution 吃掉了），重試
                logger.warning("gemini chat attempt %d: empty text, retrying", attempt + 1)
                continue
            except Exception as e:
                last_err = e
                # 失敗也要計數（Google 的 daily quota 是含失敗的）
                _track_failed_request()
                if _is_transient(e) and attempt < 2:
                    logger.warning("gemini transient error (%s), retry %d/2 after 3s", type(e).__name__, attempt + 1)
                    time.sleep(3)
                    continue
                raise
        if last_err:
            raise last_err
        raise RuntimeError("gemini chat: empty text after 3 attempts")

    try:
        return _run(settings.gemini_model)
    except Exception as e:
        if "503" in str(e) and settings.gemini_model != settings.gemini_light_model:
            logger.warning("gemini main model 503 exhausted, falling back to %s", settings.gemini_light_model)
            return _run(settings.gemini_light_model)
        raise


def ocr_image(data: bytes, mime_type: str = "image/jpeg") -> str | None:
    """圖片 OCR + 描述，不帶 tools / thinking / history，快速單次呼叫。"""
    try:
        prompt = [
            types.Part.from_bytes(data=data, mime_type=mime_type),
            "請描述這張圖片的內容。如果有文字（截圖、新聞、聊天記錄、貼文等），請完整抄出來。如果是一般圖片，用一兩句說明內容。全程繁體中文。",
        ]
        response = _client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return (response.text or "").strip() or None
    except Exception as e:
        logger.warning("ocr_image failed: %s", e)
        return None


_FACT_EXTRACT_PROMPT = """下面是一段 LINE 群組對話，請從中抽出「關於使用者的長期事實」，
例如：偏好、身份、正在做的專案、技術棧、個人習慣、稱呼……

規則：
1. 只抽「跨對話都會成立」的事實，不要抽「這次對話的即時內容」
2. 每條事實一行、繁體中文、盡量簡短具體
3. 沒抽到就回空陣列 []
4. 嚴格用 JSON 陣列格式回答，不要加任何說明文字、不要 markdown code block

對話：
{dialogue}

只輸出 JSON 陣列，例如：["使用者是 data engineer", "使用者偏好簡短回覆"]"""


def extract_facts(context: list[tuple[str, str]]) -> list[str]:
    """從最近對話抽長期事實，失敗就回空 list（不要 raise）。"""
    if not context:
        return []
    dialogue = "\n".join(
        f"{'使用者' if role == 'user' else '助手'}：{text}" for role, text in context
    )
    prompt = _FACT_EXTRACT_PROMPT.format(dialogue=dialogue)
    try:
        # 抽 facts 走 flash（頻率低 = 每 10 輪 1 次，吃 20/day 很安全）
        response = _client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = _strip_code_fence((response.text or "").strip())
        facts = json.loads(text)
        if isinstance(facts, list):
            return [str(f).strip() for f in facts if str(f).strip()]
        return []
    except Exception as e:
        logger.warning("extract_facts failed: %s", e)
        return []


def _strip_code_fence(text: str) -> str:
    """去掉 markdown code fence，讓 json.loads 吃得下。"""
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


# ── Burst 分類器（主動過濾的核心）─────────────────────────────────────────

_CLASSIFY_PROMPT = """你是一個 LINE 群組 bot 的「過濾器」。下面是群組裡最近幾則訊息。
請判斷這段對話裡，有沒有「值得你主動回應/查證」的內容。

【必須主動回應】的情境：
- 有人分享新聞連結、轉貼文章，尤其是可能有事實爭議或假訊息風險
- 有人在討論政治、選舉、兩岸、族群、宗教、疫苗、健康等需要查證的敏感話題
- 有人提出明確問題（不是反問、不是閒聊語助詞）
- 有人轉貼長文（> 80 字的正式文字段落），像是論點、聲明、文章節錄
- 短訊息（< 80 字）但包含可疑的事實宣稱（數據、政策、研究結論、健康偏方等看起來可能是假訊息的內容）→ respond，讓主模型去查核

【不要回應】的情境：
- 純閒聊（「吃飯了嗎」「晚安」「哈哈」「好喔」「讚」）
- 貼圖、emoji、反應詞
- 問候、寒暄、打招呼
- 家人之間的日常互動（「幫我買個東西」「我等等到家」）
- 單純表達情緒（「累死」「好棒」）
- 短訊息（< 80 字）且內容正確無誤或只是閒聊觀點，不涉及可疑事實

【你已學到的規則】（要遵守）：
{rules_block}

【最近對話】：
{dialogue}

請以 JSON 回覆（不要 markdown、不要說明）：
{{"decision": "respond" | "skip", "reason": "一句話說明"}}

如果 decision 是 respond，我之後會再把完整的對話丟給另一個模型產生回覆。
所以你只要判斷「值不值得回」就好，不要寫出回覆內容。"""


def classify_burst(
    combined_text: str,
    rules: list[dict],
) -> tuple[bool, str]:
    """判斷這段 burst 是否值得回。回傳 (should_respond, reason)。

    rules 是 list_filter_rules(group_id) 的輸出；會注進 prompt 讓模型參考。
    任何失敗 → 預設不回（err on the side of quiet），reason="classifier_failed"。
    """
    if not combined_text.strip():
        return (False, "empty")

    if rules:
        rule_lines = []
        for r in rules:
            tag = "不要回" if r["kind"] == "skip" else "要回"
            rule_lines.append(f"- [{tag}] {r['pattern']}")
        rules_block = "\n".join(rule_lines)
    else:
        rules_block = "（目前還沒有學到的規則）"

    prompt = _CLASSIFY_PROMPT.format(
        rules_block=rules_block,
        dialogue=combined_text,
    )
    try:
        # 分類用 light model，走獨立額度不吃主 chat 的 20/天
        response = _client.models.generate_content(
            model=settings.gemini_light_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = _strip_code_fence((response.text or "").strip())
        data = json.loads(text)
        decision = str(data.get("decision", "skip")).strip().lower()
        reason = str(data.get("reason", "")).strip()[:200]
        return (decision == "respond", reason or "no_reason")
    except Exception as e:
        logger.warning("classify_burst failed: %s", e)
        return (False, "classifier_failed")


# ── Layer 2：把使用者的糾正自動抽象成一條規則 ─────────────────────────────

_RULE_GEN_PROMPT = """你正在幫一個 LINE 群組 bot 建立「過濾規則」。

以下是一次「使用者糾正 bot」的情境：
- bot 剛才主動回覆了某個訊息
- 使用者覺得 bot 不應該回，並給出糾正原因

請從這次糾正裡，抽出一條「通用規則」，讓 bot 以後遇到類似情境時不要回應。

【bot 剛才的回覆】
{bot_reply}

【使用者的糾正原因】
{user_reason}

【bot 被觸發的原始訊息（如有）】
{trigger_text}

規則要求：
1. 要「可重用」，不能只針對這一次（不要寫「今天爸爸說的」這種）
2. 要簡短（一句話，不超過 50 字）
3. 繁體中文
4. 要能用「包含這類內容就跳過」的方式描述

請以 JSON 回覆：
{{"pattern": "規則描述", "explain": "為什麼這樣抽象"}}

不要 markdown、不要加說明文字。"""


def generate_filter_rule(
    bot_reply: str,
    user_reason: str,
    trigger_text: str = "",
) -> str | None:
    """把一次糾正抽象成一條 skip 規則的 pattern 字串。失敗回 None。"""
    prompt = _RULE_GEN_PROMPT.format(
        bot_reply=bot_reply[:800],
        user_reason=user_reason[:400],
        trigger_text=(trigger_text or "(無)")[:800],
    )
    try:
        # Layer 2 規則生成走 flash（使用者 /閉嘴 才觸發，頻率極低）
        response = _client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = _strip_code_fence((response.text or "").strip())
        data = json.loads(text)
        pattern = str(data.get("pattern", "")).strip()
        return pattern or None
    except Exception as e:
        logger.warning("generate_filter_rule failed: %s", e)
        return None


# ── Layer 3：週期性自我檢討，從過去 N 天的訊息抽出候選規則 ─────────────────

_WEEKLY_REVIEW_PROMPT = """你是一個 LINE 群組 bot 的「自我檢討員」。下面是最近一段時間這個群組的
對話紀錄（只含使用者的訊息，不含 bot 自己的回覆）。

請幫 bot 找出「**值得當成新過濾規則**」的模式，讓 bot 以後更準確地決定該回或不該回。

你要觀察的重點：
1. 哪一類訊息頻繁出現、但 bot 一回就很蠢（→ 建議 skip 規則）
2. 哪一類訊息其實很需要被查證或被回應、但很容易被忽略（→ 建議 must_answer 規則）
3. 只挑你**真的有把握**的模式，不確定就不要列
4. 已經在【目前的規則】裡的就不用重複提

【目前的規則】：
{rules_block}

【最近對話】：
{dialogue}

請以 JSON 陣列回覆最多 3 條建議，格式如下（不要 markdown、不要額外說明）：
[
  {{"kind": "skip", "pattern": "規則描述（20 字內）", "reason": "為什麼建議這條（30 字內）"}},
  ...
]

如果沒有任何值得建議的，就回空陣列 []。
kind 只能是 "skip" 或 "must_answer"。"""


def weekly_review(
    dialogue_text: str,
    existing_rules: list[dict],
) -> list[dict]:
    """週期性檢討入口。回傳 [{kind, pattern, reason}, ...]，最多 3 條；失敗回 []。"""
    if not dialogue_text.strip():
        return []

    if existing_rules:
        rule_lines = []
        for r in existing_rules:
            tag = "不要回" if r["kind"] == "skip" else "要回"
            rule_lines.append(f"- [{tag}] {r['pattern']}")
        rules_block = "\n".join(rule_lines)
    else:
        rules_block = "（目前還沒有任何規則）"

    prompt = _WEEKLY_REVIEW_PROMPT.format(
        rules_block=rules_block,
        dialogue=dialogue_text,
    )
    try:
        # Layer 3 週檢討走 flash（一週 1 次，~0.14/day，佔預算 <1%）
        response = _client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = _strip_code_fence((response.text or "").strip())
        data = json.loads(text)
        if not isinstance(data, list):
            return []
        out = []
        for item in data[:3]:  # 硬上限 3 條
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip().lower()
            pattern = str(item.get("pattern", "")).strip()
            reason = str(item.get("reason", "")).strip()
            if kind not in ("skip", "must_answer") or not pattern:
                continue
            out.append({"kind": kind, "pattern": pattern[:80], "reason": reason[:120]})
        return out
    except Exception as e:
        logger.warning("weekly_review failed: %s", e)
        return []


# ── 人設檢討：從真實對話挑好範例 + 抓糾正 ────────────────────────────────────

_PERSONA_REVIEW_PROMPT = """你是一個「人設教練」。下面是一隻小貓咪 bot 在 LINE 群組裡最近的對話紀錄，
包含使用者的訊息和 bot 的回覆。

bot 的目標人設：
- 溫柔、可愛、安靜乖巧的小女生
- 像美玉姨一樣言簡意賅，只講重要的話，不廢話
- 短句、口語，不寫作文
- 不主動插嘴閒聊，不說「我跳過」這類多餘的話

請做兩件事：

1. 【好範例】從 bot 的回覆中挑出最符合目標人設的 2~3 則，標記情境標籤
2. 【糾正紀錄】找出使用者糾正 bot 的地方（例如「不要說英文」「太正式了」「不要這樣講」），
   把糾正內容提煉成一條簡短規則

回傳 JSON（不要 markdown、不要說明）：
{{
  "examples": [
    {{"scenario": "情境標籤", "response": "bot 的原始回覆"}},
    ...
  ],
  "corrections": [
    {{"scenario": "語言", "rule": "回覆只用繁體中文，不要夾英文"}},
    ...
  ]
}}

examples 最多 3 則，corrections 最多 3 則。
如果沒挑到就留空陣列。不要自己編造，只從對話中挑。

【對話紀錄】：
{dialogue}"""


_FEEDBACK_SCAN_PROMPT = """以下是 LINE 群組在 20:00 ~ 02:00 之間收集的訊息。
bot（咪寶）在 20:00 問了「今天有哪裡可以改進的地方嗎？」

請從以下訊息中篩選出「針對 bot 表現的評語或改進建議」。
weight=2 代表推播後 1 小時內發出（回應可能性較高），weight=1 代表 1 小時後。

訊息列表：
{messages}

規則：
- 忽略與 bot 無關的訊息（閒聊、轉貼、日常對話）
- 只保留真正在評論 bot 回覆風格、語氣、內容或行為的訊息
- 若訊息模糊，用 weight 輔助判斷（weight=2 優先考慮為回覆）

請以 JSON 陣列回覆（不要 markdown）：
[
  {{"text": "原始訊息", "is_feedback": true, "summary": "這條建議的一句話摘要"}},
  ...
]
若無任何評語，回 []。"""

_IMPROVEMENT_PUSH_PROMPT = """以下是家人對 LINE bot 咪寶的評語摘要：
{feedback_list}

請根據這些評語產生：
1. 咪寶要推播給家人的回應訊息（繁體中文、溫柔可愛口吻、短句分行、不超過 200 字）
2. 咪寶要記住的改進規則（每條 30 字以內，清楚可操作）

回傳 JSON（不要 markdown）：
{{"push_message": "推播訊息", "corrections": ["規則1", "規則2", ...]}}"""


def scan_feedback_messages(messages: list[dict]) -> list[dict]:
    """用 Gemini 掃描 pending 訊息，回傳 is_feedback=True 的條目。失敗回 []。"""
    if not messages:
        return []

    msg_lines = "\n".join(
        f"[weight={m['weight']}] {m['text']}" for m in messages
    )
    prompt = _FEEDBACK_SCAN_PROMPT.format(messages=msg_lines)
    try:
        response = _client.models.generate_content(
            model=settings.gemini_light_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = _strip_code_fence((response.text or "").strip())
        data = json.loads(text)
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict) and item.get("is_feedback")]
    except Exception as e:
        logger.warning("scan_feedback_messages failed: %s", e)
        raise  # 讓 caller 判斷是否為 429，決定要不要清空 pending


def generate_improvement_push(feedback_list: list[dict]) -> dict:
    """根據評語生成推播訊息 + persona corrections。失敗回空 dict。"""
    if not feedback_list:
        return {}

    summaries = "\n".join(f"- {item.get('summary') or item.get('text', '')}" for item in feedback_list)
    prompt = _IMPROVEMENT_PUSH_PROMPT.format(feedback_list=summaries)
    try:
        response = _client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = _strip_code_fence((response.text or "").strip())
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("generate_improvement_push failed: %s", e)
        raise


def persona_review(dialogue_text: str) -> dict:
    """人設檢討入口。回傳 {{"examples": [...], "corrections": [...]}}。失敗回空。"""
    if not dialogue_text.strip():
        return {"examples": [], "corrections": []}

    prompt = _PERSONA_REVIEW_PROMPT.format(dialogue=dialogue_text)
    try:
        response = _client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = _strip_code_fence((response.text or "").strip())
        data = json.loads(text)
        if not isinstance(data, dict):
            return {"examples": [], "corrections": []}

        examples = []
        for item in (data.get("examples") or [])[:3]:
            if not isinstance(item, dict):
                continue
            scenario = str(item.get("scenario", "")).strip()[:30]
            resp = str(item.get("response", "")).strip()[:500]
            if scenario and resp:
                examples.append({"scenario": scenario, "response": resp})

        corrections = []
        for item in (data.get("corrections") or [])[:3]:
            if not isinstance(item, dict):
                continue
            scenario = str(item.get("scenario", "")).strip()[:30]
            rule = str(item.get("rule", "")).strip()[:100]
            if rule:
                corrections.append({"scenario": scenario or "一般", "rule": rule})

        return {"examples": examples, "corrections": corrections}
    except Exception as e:
        logger.warning("persona_review failed: %s", e)
        return {"examples": [], "corrections": []}
