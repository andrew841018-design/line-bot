"""food_signals.py — 從家庭群組對話抽飲食 / 採購訊號（純規則，無 LLM）。

⚠️ SOURCE: 食材白名單 / canonical / trigger regex / cancel cue 複製自
   food_extractor.py（複製時點 2026-05-31）。依 user directive「不碰 food_extractor /
   kg_triples 死碼線」，此處為「複製即分叉」：日後只改 food_signals，原 food_extractor.py
   視為 DEAD（已無任何 live import，本檔 food_signals 為唯一活路徑）。不 import food_extractor，避免把死碼變活。

與 food_extractor 的差異：
- v1 不抽個人 subject（GP2 blocker A，家庭層級），extract 不回 subject。
- 存進 food_db.family_food（非 kg_triples）；存前已 canonical（GP1 C5e）。

Public API:
    extract(text) -> list[dict{kind, food, surface, confidence}]
    extract_and_store(group_id, source_msg_id, text) -> int
    extract_and_store_async(group_id, source_msg_id, text) -> None
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

import food_db

logger = logging.getLogger("food_signals")

# ── 食材白名單（複製自 food_extractor._FOOD_ITEMS @2026-05-31）──
_FOOD_ITEMS = frozenset({
    # 主食 / 麵食
    "蘿蔔糕", "粽子", "粽", "麵", "飯", "便當", "三明治", "漢堡",
    "水餃", "餃子", "湯圓", "麵包", "饅頭", "包子", "麵線", "米飯",
    # 蔬菜
    "蘿蔔", "白蘿蔔", "紅蘿蔔", "青菜", "高麗菜", "白菜", "菠菜",
    "空心菜", "蕃茄", "番茄", "馬鈴薯", "蕃薯", "地瓜", "洋蔥",
    "蔥", "薑", "蒜", "辣椒", "酸菜", "竹筍", "木耳", "香菇",
    "金針菇", "玉米", "茄子",
    # 水果
    "蘋果", "香蕉", "柳丁", "橘子", "葡萄", "西瓜", "鳳梨", "芒果",
    "草莓", "水果",
    # 海鮮
    "虱目魚", "虱目魚肚", "虱目魚丸", "魚丸", "魚片", "秋刀魚",
    "鮭魚", "鯖魚", "蝦", "蛤蜊", "牡蠣", "鮪魚", "魚肉", "魚",
    # 肉
    "雞", "雞肉", "雞腿", "雞胸", "雞胸肉", "雞翅", "嫩雞胸肉",
    "豬", "豬肉", "豬腿肉", "豬絞肉", "絞肉", "排骨", "腿庫",
    "牛", "牛肉", "牛排", "牛腩", "鴨", "鴨肉", "鴨腿", "鵝",
    "肉鬆", "香腸", "火腿", "培根", "粉腸", "豬血糕", "米血",
    # 蛋豆
    "蛋", "雞蛋", "皮蛋", "茶葉蛋", "豆腐", "豆干", "豆漿", "豆花",
    "味噌",
    # 飲品
    "牛奶", "果汁", "茶", "咖啡",
    # 調味
    "醬油", "鹽", "糖", "醋",
    # 料理（常見家常菜）
    "麻婆豆腐", "三杯雞", "白斬雞", "雞湯", "魚湯", "蛋花湯",
    "牛肉麵", "蚵仔煎", "肉燥飯", "滷肉飯", "炒飯", "炒麵",
    # 採購非食物（家庭採購清單擴展）
    "消痔丸", "衛生紙", "牙膏",
    # 粗粒度類別
    "澱粉類", "蛋白質", "蔬菜類",
})
_FOOD_ITEMS_SORTED = sorted(_FOOD_ITEMS, key=lambda x: -len(x))

# ── canonical 同義詞 → 標準名（複製自 food_extractor._CANONICAL）──
_CANONICAL = {
    "魚肉": "魚", "雞肉": "雞", "豬肉": "豬", "牛肉": "牛", "鴨肉": "鴨",
    "雞蛋": "蛋", "米飯": "飯", "餃子": "水餃", "番茄": "蕃茄",
    "嫩雞胸肉": "雞胸", "雞胸肉": "雞胸",
}


def canonical(food: str) -> str:
    """正規化食物名（魚肉→魚）。不在表內原樣返回。"""
    return _CANONICAL.get(food, food)


def is_food(token: str) -> bool:
    if not token:
        return False
    if token in _FOOD_ITEMS:
        return True
    return canonical(token) in _FOOD_ITEMS


# ── trigger patterns（複製自 food_extractor，順序：specific/negative 優先）──
_DISLIKE_TRIGGER = re.compile(r"(?:不喜歡|不愛吃|不愛喝|不敢吃|不敢喝)")
_LIKES_TRIGGER = re.compile(r"(?<![不沒])(?:很?喜歡|愛吃|愛喝)")
_WANTS_TO_EAT_TRIGGER = re.compile(r"(?<![不沒])(?:想吃|想喝|好想吃|好想喝|想來|要不要[吃喝])")
_WANTS_BOUGHT_TRIGGER = re.compile(
    r"(?<![不沒])(?:要買|需要買|可以買|想買|"
    r"幫(?:我|忙)再?買|還要買|多買|可買|再買|去買|"
    r"要[一二三四五六七八九十百\d去再]{1,3}買)"
)
_BOUGHT_TRIGGER = re.compile(
    r"(?:買了|買回家?|買到了?|已經買|剛買|今天買|這次買|買回來|在.{0,5}買的)"
)
# OV 語序「X買了」（食物在動詞前）的到貨回報——buy-verb 緊接 food，故 anchored `^`
# 比對 food 後 15 字。負向 lookahead 擋問句／反問（「買了嗎/沒」），否則會把
# 「蛋買了嗎?」誤判成 bought 清掉採購清單（confidence 在 food_db 層被丟棄，擋不住）。
# 「買好了」必須帶「了」：否則 `^買好` 會命中「買好貴」（嫌貴≠買到）。
_BOUGHT_SUFFIX_TRIGGER = re.compile(
    r"^(?:買了|買回來了?|買回家了?|買好了|買到了?)(?![嗎呢啊吧?？沒])"
)
_HAS_FOOD_TRIGGER = re.compile(
    r"(?:冰箱(?:裡)?有|還有|家裡有|我帶了|我提了|還剩|有剩|煮|蒸|炒|煎|烤|燉|滷)"
)
_FINISHED_TRIGGER = re.compile(r"(?:吃完|喝完|沒剩|用完|全光|光了)")

# (kind, pattern, side)：'prefix' = food 前 15 字、'suffix' = food 後 15 字
# bought-suffix 放最後 = first-match fallback：prefix 路徑（買了X / 要買X）先贏，沒命中才
#   用 suffix 補 OV 語序（X買了）。2026-06-01 修單食物 OV 缺口。
# DEFERRED：多食物 OV 清單「蛋和牛奶買了」只蓋緊鄰動詞的最後一個 food（anchored suffix），
#   前面的會漏；見 tests/test_food_signals.py::test_bought_multi_food_ov_still_partial（xfail）。
#   prefix 問句 FP「買了蛋嗎」仍會記 bought（baseline 既有，非本次引入），v2 一併處理。
#   suffix 反問「蛋買了還是沒買?」單字 lookahead 擋不掉（買了後接「還」非阻擋字）→ 罕見 OV
#   反問會誤記 bought；realism 低（家庭群少見此句式），v2 再補（如偵測「還是」）。
_TRIGGER_PIPELINE = [
    ("dislikes_food", _DISLIKE_TRIGGER, "prefix"),
    ("finished_food", _FINISHED_TRIGGER, "suffix"),
    ("bought", _BOUGHT_TRIGGER, "prefix"),
    ("wants_bought", _WANTS_BOUGHT_TRIGGER, "prefix"),
    ("wants_to_eat", _WANTS_TO_EAT_TRIGGER, "prefix"),
    ("likes_food", _LIKES_TRIGGER, "prefix"),
    ("has_food", _HAS_FOOD_TRIGGER, "prefix"),
    ("bought", _BOUGHT_SUFFIX_TRIGGER, "suffix"),
]

_CANCEL_CUE = re.compile(
    r"沒.{0,15}買|沒去買|取消|算了|不買了?|不要了|本來.{0,30}沒|沒有再|忘了買|沒空買"
)
_SENTENCE_SPLIT = re.compile(r"[。！？!?\n]+")
_QUESTION_END = re.compile(r"[嗎呢?？]\s*$")
_FUTURE_HINT = re.compile(r"以後|未來|有空")
_RESTAURANTS = frozenset({
    "和園", "鬍鬚張", "爭鮮", "麥當勞", "肯德基", "摩斯", "漢堡王",
    "星巴克", "全聯", "大潤發", "家樂福", "全家", "仁德街",
})
_URL_RE = re.compile(r"https?://\S+")


def _find_food_occurrences(sentence: str) -> list[tuple[int, int, str]]:
    """掃 sentence 內所有 white-list food token 的 [(start, end, food)]。Longest-match wins, 不重疊。"""
    if not sentence:
        return []
    consumed = bytearray(len(sentence))
    matches = []
    for food in _FOOD_ITEMS_SORTED:
        start = 0
        while True:
            idx = sentence.find(food, start)
            if idx < 0:
                break
            end = idx + len(food)
            if any(consumed[idx:end]):
                start = end
                continue
            for i in range(idx, end):
                consumed[i] = 1
            matches.append((idx, end, food))
            start = end
    matches.sort(key=lambda m: m[0])
    return matches


def _classify_food(sentence: str, fstart: int, fend: int) -> str | None:
    """根據 food 前/後 15 字 trigger 判定 kind。"""
    prefix = sentence[max(0, fstart - 15):fstart]
    suffix = sentence[fend:fend + 15]
    for kind, pattern, side in _TRIGGER_PIPELINE:
        target = prefix if side == "prefix" else suffix
        if pattern.search(target):
            return kind
    return None


def extract(text: str) -> list[dict]:
    """從 text 抽 food signals。回 list[{kind, food, surface, confidence}]。

    v1 不歸個人 subject（GP2 A）；food 已 canonical（GP1 C5e）。
    """
    if not text or not text.strip():
        return []
    text = text[:500]
    text = _URL_RE.sub(" ", text)
    if not any(food in text for food in _FOOD_ITEMS):
        return []

    cancelled = bool(_CANCEL_CUE.search(text))
    signals: list[dict] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        # 餐廳問句（含餐廳名 + 問號 OR「還是」）→ 整句 skip
        if any(r in sentence for r in _RESTAURANTS) and (
            _QUESTION_END.search(sentence) or "還是" in sentence
        ):
            continue
        question_modal = bool(_QUESTION_END.search(sentence)) or bool(
            _FUTURE_HINT.search(sentence)
        )
        for fstart, fend, food in _find_food_occurrences(sentence):
            kind = _classify_food(sentence, fstart, fend)
            if not kind:
                continue
            if cancelled and kind in ("wants_bought", "wants_to_eat"):
                continue
            signals.append({
                "kind": kind,
                "food": canonical(food),     # 存 canonical（GP1 C5e）
                "surface": food,
                "confidence": "low" if question_modal else "high",
            })

    # dedupe within text（相同 kind+food 只留一次）
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for s in signals:
        key = (s["kind"], s["food"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(s)
    return unique


def extract_and_store(group_id: str, source_msg_id: str, text: str) -> int:
    """extract + 寫進 food_db.family_food。回新增筆數（dedup 不計）。"""
    if not group_id or not text:
        return 0
    signals = extract(text)
    if not signals:
        return 0
    n = 0
    for s in signals:
        try:
            if food_db.insert_signal(
                group_id, s["kind"], s["food"],
                source_msg_id=source_msg_id or "", source_text=text,
            ):
                n += 1
        except Exception as e:
            logger.warning("food_signals store failed: %s", e)
    return n


_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="food-signals")


def extract_and_store_async(group_id: str, source_msg_id: str, text: str) -> None:
    """背景執行緒抽 + 寫，永不阻塞 caller。失敗 silent。"""
    if not group_id or not text or not text.strip():
        return

    def _run() -> None:
        try:
            extract_and_store(group_id, source_msg_id, text)
        except Exception:
            pass

    try:
        _EXECUTOR.submit(_run)
    except Exception as e:
        logger.warning("food_signals submit failed: %s", e)
