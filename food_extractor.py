"""
food_extractor.py — 從家庭群組對話抽家庭飲食 / 採購訊號。

Storage: 直接寫進既有 kg_triples (knowledge_graph.py)，relation = food 子類。
Pattern: 食物 white-list × 觸發詞 regex × sentence-level 取消 cue。
無 LLM, 純規則, fire-and-forget thread pool cap=2.

Public API:
    extract(text, sender_alias)         → list[dict]
    extract_and_store(group_id, ..., text) → int
    extract_async(group_id, ..., text)  → None
    canonical(food)                     → str
    is_food(token)                      → bool
    alias_from_user_id(user_id)         → str

每 signal 寫成 (group_id, subject, kind, food) 三元組存進 kg_triples，
source_text 保留原訊息供 audit。
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger("food_extractor")

# ─────────────────────────────────────────────────────────────────────────────
# Public constants
# ─────────────────────────────────────────────────────────────────────────────

FOOD_RELATIONS = (
    "wants_to_eat",     # 想吃 X    → F3 wants
    "has_food",         # 冰箱有 X  → F3 inventory
    "wants_bought",     # 要買 X    → F1 shopping pending
    "bought",           # 買了 X    → F1 mark done
    "likes_food",       # 喜歡 X    → F2 prefer
    "dislikes_food",    # 不喜歡 X  → F2 avoid
    "finished_food",    # X 沒了    → F3 inventory minus
)

# ─────────────────────────────────────────────────────────────────────────────
# Food white-list (~100 items, 台灣家常 + raw_messages 真實樣本)
# ─────────────────────────────────────────────────────────────────────────────

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
    # 料理 (常見家常菜)
    "麻婆豆腐", "三杯雞", "白斬雞", "雞湯", "魚湯", "蛋花湯",
    "牛肉麵", "蚵仔煎", "肉燥飯", "滷肉飯", "炒飯", "炒麵",
    # 採購非食物 (家庭採購清單擴展)
    "消痔丸", "衛生紙", "牙膏",
    # 粗粒度類別 (likes 統計用)
    "澱粉類", "蛋白質", "蔬菜類",
})

_FOOD_ITEMS_SORTED = sorted(_FOOD_ITEMS, key=lambda x: -len(x))


# ─────────────────────────────────────────────────────────────────────────────
# Canonical mapping (同義詞 → 標準名)
# ─────────────────────────────────────────────────────────────────────────────

_CANONICAL = {
    "魚肉": "魚",
    "雞肉": "雞",
    "豬肉": "豬",
    "牛肉": "牛",
    "鴨肉": "鴨",
    "雞蛋": "蛋",
    "米飯": "飯",
    "餃子": "水餃",
    "番茄": "蕃茄",
    "嫩雞胸肉": "雞胸",
    "雞胸肉": "雞胸",
}


def canonical(food: str) -> str:
    """正規化食物名 (魚肉→魚, 雞肉→雞)。不在表內原樣返回。"""
    return _CANONICAL.get(food, food)


def is_food(token: str) -> bool:
    """白名單檢查 (含 canonical 同義)。"""
    if not token:
        return False
    if token in _FOOD_ITEMS:
        return True
    return canonical(token) in _FOOD_ITEMS


# ─────────────────────────────────────────────────────────────────────────────
# Named subject detection (3rd person attribution)
# ─────────────────────────────────────────────────────────────────────────────

_NAMED_SUBJECTS = (
    "媽媽", "爸爸", "姊姊", "姐姐", "哥哥", "弟弟", "妹妹",
    "黃聖雅", "黃聖穎", "阿婆", "阿公", "奶奶", "爺爺", "舅舅", "阿姨",
)

_SUBJECT_VERB_RE = re.compile(
    rf"({'|'.join(_NAMED_SUBJECTS)})"
    r"(?:很|超|真|還|也|就|又|沒|不|常)*"
    r"(?:喜歡|愛吃|愛喝|不喜歡|不愛|不敢|想吃|想喝|要買|需要買|還有|沒了|吃完|買了|買的)"
)


def _detect_subject(sentence: str, default: str) -> str:
    m = _SUBJECT_VERB_RE.search(sentence)
    if m:
        return m.group(1)
    return default or "群組"


# ─────────────────────────────────────────────────────────────────────────────
# Trigger patterns (compile once)
# 順序：specific / negative 優先，避免 "不喜歡" 被 "喜歡" 誤判
# ─────────────────────────────────────────────────────────────────────────────

_DISLIKE_TRIGGER = re.compile(
    r"(?:不喜歡|不愛吃|不愛喝|不敢吃|不敢喝)"
)
_LIKES_TRIGGER = re.compile(
    r"(?<![不沒])(?:很?喜歡|愛吃|愛喝)"
)
_WANTS_TO_EAT_TRIGGER = re.compile(
    r"(?<![不沒])(?:想吃|想喝|好想吃|好想喝|想來|要不要[吃喝])"
)
_WANTS_BOUGHT_TRIGGER = re.compile(
    r"(?<![不沒])(?:要買|需要買|可以買|想買|"
    r"幫(?:我|忙)再?買|還要買|多買|可買|再買|去買|"
    r"要[一二三四五六七八九十百\d去再]{1,3}買)"
)
_BOUGHT_TRIGGER = re.compile(
    r"(?:買了|買回家?|買到了?|已經買|剛買|今天買|這次買|買回來|在.{0,5}買的)"
)
_HAS_FOOD_TRIGGER = re.compile(
    r"(?:冰箱(?:裡)?有|還有|家裡有|我帶了|我提了|還剩|有剩|"
    r"煮|蒸|炒|煎|烤|燉|滷)"
)
_FINISHED_TRIGGER = re.compile(
    r"(?:吃完|喝完|沒剩|用完|全光|光了)"
)

# (kind, pattern, side)：'prefix' = food 前 15 字、'suffix' = food 後 15 字
_TRIGGER_PIPELINE = [
    ("dislikes_food",  _DISLIKE_TRIGGER,      "prefix"),
    ("finished_food",  _FINISHED_TRIGGER,     "suffix"),
    ("bought",         _BOUGHT_TRIGGER,       "prefix"),
    ("wants_bought",   _WANTS_BOUGHT_TRIGGER, "prefix"),
    ("wants_to_eat",   _WANTS_TO_EAT_TRIGGER, "prefix"),
    ("likes_food",     _LIKES_TRIGGER,        "prefix"),
    ("has_food",       _HAS_FOOD_TRIGGER,     "prefix"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Sentence-level cancel cue (跨字距 > 10 字也能抓)
# ─────────────────────────────────────────────────────────────────────────────

_CANCEL_CUE = re.compile(
    r"沒.{0,15}買|沒去買|取消|算了|不買了?|不要了|本來.{0,30}沒|沒有再|"
    r"忘了買|沒空買"
)


# ─────────────────────────────────────────────────────────────────────────────
# Sentence split / question modal
# ─────────────────────────────────────────────────────────────────────────────

_SENTENCE_SPLIT = re.compile(r"[。！？!?\n]+")
_QUESTION_END = re.compile(r"[嗎呢?？]\s*$")
_FUTURE_HINT = re.compile(r"以後|未來|有空")


# ─────────────────────────────────────────────────────────────────────────────
# Restaurant / brand black-list (這些名詞不算 food)
# ─────────────────────────────────────────────────────────────────────────────

_RESTAURANTS = frozenset({
    "和園", "鬍鬚張", "爭鮮", "麥當勞", "肯德基", "摩斯", "漢堡王",
    "星巴克", "全聯", "大潤發", "家樂福", "全家", "仁德街",
})


# ─────────────────────────────────────────────────────────────────────────────
# URL stripping
# ─────────────────────────────────────────────────────────────────────────────

_URL_RE = re.compile(r"https?://\S+")


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_food_occurrences(sentence: str) -> list[tuple[int, int, str]]:
    """掃 sentence 內所有 white-list food token 的 [(start, end, food), ...]。
    Longest-match wins, 不重疊。
    """
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
    """根據 food 前/後 15 字 trigger 判定 kind。回 kind 或 None。"""
    prefix = sentence[max(0, fstart - 15):fstart]
    suffix = sentence[fend:fend + 15]
    for kind, pattern, side in _TRIGGER_PIPELINE:
        target = prefix if side == "prefix" else suffix
        if pattern.search(target):
            return kind
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Public extract
# ─────────────────────────────────────────────────────────────────────────────

def extract(text: str, sender_alias: str = "") -> list[dict]:
    """從 text 抽 food signals。回 list[{kind, subject, food, surface, confidence, source_phrase}]。"""
    if not text or not text.strip():
        return []

    text = text[:500]
    text = _URL_RE.sub(" ", text)

    # quick gate: 沒任何 white-list food 就 short-circuit
    if not any(food in text for food in _FOOD_ITEMS):
        return []

    cancelled = bool(_CANCEL_CUE.search(text))

    signals: list[dict] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue

        # 餐廳問句 (含餐廳名 + 問號 OR 「還是」) → 整句 skip
        if any(r in sentence for r in _RESTAURANTS) and (
            _QUESTION_END.search(sentence) or "還是" in sentence
        ):
            continue

        question_modal = bool(_QUESTION_END.search(sentence)) or bool(
            _FUTURE_HINT.search(sentence)
        )

        subject = _detect_subject(sentence, sender_alias)

        for fstart, fend, food in _find_food_occurrences(sentence):
            kind = _classify_food(sentence, fstart, fend)
            if not kind:
                continue

            # 跨整段 cancel cue (e.g.「本來要買...結果沒買」) → 砍掉 want_bought / wants_to_eat
            if cancelled and kind in ("wants_bought", "wants_to_eat"):
                continue

            confidence = "low" if question_modal else "high"
            signals.append({
                "kind": kind,
                "subject": subject,
                "food": canonical(food),
                "surface": food,
                "confidence": confidence,
                "source_phrase": sentence,
            })

    # dedupe within text (相同 kind+subject+food 只留一次)
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict] = []
    for s in signals:
        key = (s["kind"], s["subject"], s["food"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(s)
    return unique


# ─────────────────────────────────────────────────────────────────────────────
# Alias lookup (user_id → 媽媽 / 爸爸 / 黃聖雅 / 黃聖穎)
# ─────────────────────────────────────────────────────────────────────────────

_ALIASES_PATH = Path(__file__).parent / "user_aliases.json"
_ALIASES_CACHE: dict[str, str] | None = None


def _load_aliases() -> dict[str, str]:
    global _ALIASES_CACHE
    if _ALIASES_CACHE is not None:
        return _ALIASES_CACHE
    if not _ALIASES_PATH.exists():
        _ALIASES_CACHE = {}
        return _ALIASES_CACHE
    try:
        with open(_ALIASES_PATH, encoding="utf-8") as f:
            _ALIASES_CACHE = json.load(f)
    except Exception as e:
        logger.warning("load aliases failed: %s", e)
        _ALIASES_CACHE = {}
    return _ALIASES_CACHE


def alias_from_user_id(user_id: str) -> str:
    """user_id → 中文別名（媽媽 / 爸爸 / 黃聖雅 / 黃聖穎）；找不到回空字串。"""
    if not user_id:
        return ""
    return _load_aliases().get(user_id, "")


# ─────────────────────────────────────────────────────────────────────────────
# Store + fire-and-forget
# ─────────────────────────────────────────────────────────────────────────────

def extract_and_store(
    group_id: str,
    sender_alias: str,
    text: str,
    db_path=None,
) -> int:
    """extract + 寫進 kg_triples。回新增筆數 (PK 衝突 IGNORE)。"""
    if not group_id or not text:
        return 0
    signals = extract(text, sender_alias=sender_alias)
    if not signals:
        return 0

    # late import 避免 module-load 時的 jieba 重 init
    import knowledge_graph as kg

    triples = [(s["subject"], s["kind"], s["food"]) for s in signals]
    try:
        return kg.store_triples(group_id, triples, source_text=text, db_path=db_path)
    except Exception as e:
        logger.warning("food_extractor store failed: %s", e)
        return 0


_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="food-extract")


def extract_async(group_id: str, sender_alias: str, text: str) -> None:
    """背景執行緒抽 + 寫，永不阻塞 caller。失敗 silent。"""
    if not group_id or not text or not text.strip():
        return
    try:
        import knowledge_graph as kg

        db_path = kg._DB_PATH
    except Exception:
        db_path = None

    def _run() -> None:
        try:
            extract_and_store(group_id, sender_alias, text, db_path=db_path)
        except Exception:
            pass

    try:
        _EXECUTOR.submit(_run)
    except Exception as e:
        logger.warning("extract_async submit failed: %s", e)
