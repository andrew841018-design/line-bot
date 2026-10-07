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
from pathlib import Path

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
    r"要[一二三四五六七八九十百\d去再]{1,3}買|"
    # 2026-10-07：「記得買蛋」「順便買」「去全聯買牛奶」（去和買中間隔了地方）；
    # 「剛去市場買魚回來」「去全聯買的蛋」「去年買的」「去哪裡買」都不是要買。
    r"記得要?買|順便買|(?<![剛過])去(?![年哪])[^，,；;]{1,6}買(?![了到回好過的來給]))"
)
# 「沒買到蛋」不是買到了，「不用買了」「別買了」也不是（2026-10-07）
_NOT_BOUGHT_BEFORE = r"(?<!沒)(?<!沒有)(?<!不用)(?<!不要)(?<!不必)(?<![不別])"
# 2026-10-07 拿掉「今天買／這次買」：「今天買蛋好了」「這次買蛋要買大盒的」只是打算。
_BOUGHT_TRIGGER = re.compile(
    _NOT_BOUGHT_BEFORE + r"(?:買了|買回家?|買到了?|已經買|剛買|買回來|在.{0,5}買的)"
)
# OV 語序「X買了」（食物在動詞前）的到貨回報——buy-verb 緊接 food，故 anchored `^`
# 比對 food 後 15 字。負向 lookahead 擋問句／反問（「買了嗎/沒」），否則會把
# 「蛋買了嗎?」誤判成 bought 清掉採購清單（confidence 在 food_db 層被丟棄，擋不住）。
# 「買好了」必須帶「了」：否則 `^買好` 會命中「買好貴」（嫌貴≠買到）。
# 2026-10-07：中間可以夾「我／已經／也…」（「牛奶我已經買了」）。
_BOUGHT_SUFFIX_TRIGGER = re.compile(
    r"^(?:我|也|都|已經|剛|有)*(?:買了|買回來了?|買回家了?|買好了|買到了?)(?![嗎呢啊吧?？沒])"
)
# 2026-10-07 拿掉煮／蒸／炒／煎／烤／燉／滷：「我晚上煮麵」「在餐廳吃烤鴨」不代表家裡有。
_HAS_FOOD_TRIGGER = re.compile(
    r"(?:冰箱(?:裡面?)?有|還有|家裡面?有|我帶了|我提了|還剩|有剩)"
)
# 2026-10-07 加「沒了／沒有了／吃光…」：「蛋沒了」是最常見的講法。
# 「還沒用完」「沒吃完」是還有。
_FINISHED_TRIGGER = re.compile(
    r"(?:(?<!沒)(?:吃完|喝完|用完|吃光|喝光|用光)|沒剩|全光|光了|沒了|沒有了)"
)
# 「不用買蛋了」「蛋不用買了」：從待買清單拿掉，但不算買到（2026-10-07）。
# 「要不要買」「需不需要買」「買不買」「怎麼不買」是在問；「不要買太多」「不要買低脂的」
# 還是要買，都不算：前面那種要緊接著食物（中間只能是別的食物），後面那種要在句尾。
_SKIP_BUYING_PREFIX_TRIGGER = re.compile(
    r"(?:(?<!要)不要|(?<!用)不用|(?<!必)不必|(?<!需)不需要?|先不要|先別|別|"
    r"(?<!買)(?<!怎麼)(?<!為什麼)(?<!幹嘛)不)"
    r"(?:幫我|幫忙|去[^，,；;]{0,6})?再?買"
)
_SKIP_BUYING_SUFFIX_TRIGGER = re.compile(
    r"^(?:也|就|先|都|暫時|還)?(?:不要|不用|不必|不需要?|別|不)再?買"
    r"(?:了|啦|喔|囉|哦|唷)*(?=\s*$|[，,。！!？?、~～])"
)

# (kind, pattern, side)：'prefix' = food 前 15 字、'suffix' = food 後同一段的 15 字。
# 2026-10-07 起取離 food 最近的 trigger（同距離才看這裡的先後）：「已經買了蛋，以後再買
#   牛奶」的牛奶最近的是「再買」，不是前一句的「買了」。
# DEFERRED：多食物 OV 清單「蛋和牛奶買了」只蓋緊鄰動詞的最後一個 food（anchored suffix），
#   前面的會漏；見 tests/test_food_signals.py::test_bought_multi_food_ov_still_partial（xfail）。
#   suffix 反問「蛋買了還是沒買?」單字 lookahead 擋不掉（買了後接「還」非阻擋字）→ 罕見 OV
#   反問會誤記 bought；realism 低（家庭群少見此句式），v2 再補（如偵測「還是」）。
_TRIGGER_PIPELINE = [
    ("dislikes_food", _DISLIKE_TRIGGER, "prefix"),
    ("finished_food", _FINISHED_TRIGGER, "suffix"),
    ("skip_buying", _SKIP_BUYING_SUFFIX_TRIGGER, "suffix"),
    ("skip_buying", _SKIP_BUYING_PREFIX_TRIGGER, "prefix"),
    ("bought", _BOUGHT_TRIGGER, "prefix"),
    ("wants_bought", _WANTS_BOUGHT_TRIGGER, "prefix"),
    ("wants_to_eat", _WANTS_TO_EAT_TRIGGER, "prefix"),
    ("likes_food", _LIKES_TRIGGER, "prefix"),
    ("has_food", _HAS_FOOD_TRIGGER, "prefix"),
    ("bought", _BOUGHT_SUFFIX_TRIGGER, "suffix"),
]

# 「沒…買」是沒去買；「蛋沒了，要買蛋」「冰箱沒有蛋了，要買」的「沒」不是（2026-10-07）。
_CANCEL_CUE = re.compile(
    r"沒(?!了|有了|剩|有[^，,。]{1,6}了).{0,15}買|沒去買|取消|算了|不買了?|不要了|"
    r"本來.{0,30}沒|沒有再|忘了買|沒空買"
)
# 一句連同句尾的標點，問號才看得到。
_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]*")
_QUESTION_END = re.compile(r"[嗎呢?？]\s*$")
# 分段：逗號、分號、詞中間的空白（LINE 常用空白代替逗號），還有「嗎／呢」後面直接接字。
_CLAUSE_BREAK = re.compile(r"[，,；;]|(?<=\w)\s+(?=\w)|(?<=[嗎呢])(?=\w)")
# 判斷「這個食物自己那一段」時頓號也算分段（「要買蛋、牛奶」的牛奶那段只有牛奶）。
_ITEM_BREAK = re.compile(r"[，,；;、]|(?<=\w)\s+(?=\w)|(?<=[嗎呢])(?=\w)")
# 「冰箱有蛋嗎，要買牛奶」：只有問句那一段不算數。結尾可以有～、表情、空白。
_CLAUSE_QUESTION = re.compile(r"[嗎呢?？][^\w]*$|了沒[^\w]*$|有沒有|是不是|(\w)不\1")
_TRAILING_YOU = re.compile(r"(?:那)?[你妳您]們?呢[^\w]*$")  # 「我買了牛奶你呢」
_RHETORICAL = re.compile(r"不是.*嗎[^\w]*$")  # 「冰箱不是還有蛋嗎」是在說有
# 「可以幫我買蛋嗎」是拜託，不是問；「需要幫忙買蛋嗎」「要買蛋嗎」才是問。
_POLITE_REQUEST = re.compile(
    r"幫我|麻煩|拜託|可以.{0,6}買|能不能.{0,6}買|誰.{0,6}買|(?:好嗎|好不好|可以嗎|行嗎)[^\w]*$"
)
_OFFER = re.compile(r"需要我?幫|要我幫|要不要我?幫|要不要買|要買.{0,8}嗎|了[^\w]*$")
# 「等你買了牛奶回來再煮」「買了蛋記得放冰箱」還沒買。
_CONDITIONAL = re.compile(r"等.{0,3}買了|如果|要是|的話|買了.{1,8}(?:再|記得)")
# 會改變家裡有什麼／該買什麼的事件：問句裡的不記（「家裡還有蘋果嗎」）。
_STATE_KINDS = frozenset({"has_food", "finished_food", "bought", "wants_bought", "skip_buying"})
# 買到了也算家裡有，但這些不是放家裡的食材：日用品、分類字、外帶熟食和飲料。
_NON_FOOD_ITEMS = frozenset({"消痔丸", "衛生紙", "牙膏", "澱粉類", "蛋白質", "蔬菜類"})
_READY_MEALS = frozenset({
    "便當", "三明治", "漢堡", "麻婆豆腐", "三杯雞", "白斬雞", "雞湯", "魚湯", "蛋花湯",
    "牛肉麵", "蚵仔煎", "肉燥飯", "滷肉飯", "炒飯", "炒麵", "咖啡", "茶", "果汁", "豆花",
    "茶葉蛋",
})
# 含白名單字、但本身不是食材的東西：整個詞吃掉，不拆出「雞」「蛋」「茶」。
_NOT_INGREDIENT_COMPOUNDS = frozenset({
    "雞排", "鹽酥雞", "鹹酥雞", "炸雞", "雞塊", "雞捲", "雞排便當", "雞腿便當", "排骨便當",
    "蛋餅", "蛋糕", "雞蛋糕", "蛋塔", "蛋捲", "奶茶", "珍奶", "珍珠奶茶", "蔥油餅", "蔥抓餅",
    "魚湯麵",
})
_SCAN_TOKENS = sorted(_FOOD_ITEMS | _NOT_INGREDIENT_COMPOUNDS, key=lambda x: -len(x))
_FOODS_ALT = "|".join(map(re.escape, _FOOD_ITEMS_SORTED))
# 「要買蛋還有牛奶」的「還有」是「和」，不是「家裡還有」。
_LIST_JOIN_BEFORE = re.compile(rf"(?:{_FOODS_ALT})[、，,\s]?$")
# 食物後面只有別的食物、連接詞、數量：還是同一張清單，前面的動詞算數。
_LIST_TAIL = re.compile(
    rf"(?:\s|[、，,跟和與及也都各等了喔哦啊啦吧囉]|還有|"
    rf"[一二兩三四五六七八九十半幾\d]+[盒包顆瓶個斤罐袋條把串箱份片塊隻尾杯]|{_FOODS_ALT})*"
)
_FUTURE_HINT = re.compile(r"以後|未來|有空")
_RESTAURANTS = frozenset({
    "和園", "鬍鬚張", "爭鮮", "麥當勞", "肯德基", "摩斯", "漢堡王",
    "星巴克", "全聯", "大潤發", "家樂福", "全家", "仁德街",
})
_URL_RE = re.compile(r"https?://\S+")


def _find_food_occurrences(sentence: str) -> list[tuple[int, int, str]]:
    """掃 sentence 內所有 white-list food token 的 [(start, end, food)]。Longest-match wins, 不重疊。

    「雞排」「蛋糕」這類不是食材的詞會整個吃掉、不回傳（2026-10-07）。
    """
    if not sentence:
        return []
    consumed = bytearray(len(sentence))
    matches = []
    for token in _SCAN_TOKENS:
        start = 0
        while True:
            idx = sentence.find(token, start)
            if idx < 0:
                break
            end = idx + len(token)
            if any(consumed[idx:end]):
                start = end
                continue
            for i in range(idx, end):
                consumed[i] = 1
            if token not in _NOT_INGREDIENT_COMPOUNDS:
                matches.append((idx, end, token))
            start = end
    matches.sort(key=lambda m: m[0])
    return matches


def _classify_food(sentence: str, fstart: int, fend: int) -> tuple[str | None, str, str]:
    """根據 food 前／後的 trigger 判定 kind：離 food 最近的贏。回 (kind, 讀的前文, 讀的後文)。

    前文最多 15 字；這個食物自己那一段如果還有別的話（「但蛋沒買到」），就不借前一段
    的動詞。後文只看到同一段結束（「蛋還很多，牛奶沒了」的「沒了」跟蛋無關）。
    """
    # 整句一起找：只找到 fstart 為止會讓「空白後面要接字」的判斷看不到下一個字
    before = [m.end() for m in _ITEM_BREAK.finditer(sentence) if m.end() <= fstart]
    own_start = before[-1] if before else 0
    own_end = _ITEM_BREAK.search(sentence, fend)
    tail = sentence[fend:own_end.start() if own_end else len(sentence)]
    lo = max(0, fstart - 15)
    if not _LIST_TAIL.fullmatch(tail):
        lo = max(lo, own_start)
    prefix = sentence[lo:fstart]
    clause_end = _CLAUSE_BREAK.search(sentence, fend)
    suffix = sentence[fend:min(fend + 15, clause_end.start() if clause_end else len(sentence))]
    if prefix.endswith("沒有") and suffix.startswith("了"):
        return "finished_food", prefix, suffix  # 「冰箱沒有蛋了」
    best: tuple[int, int, str] | None = None
    for order, (kind, pattern, side) in enumerate(_TRIGGER_PIPELINE):
        if side == "prefix":
            hits = list(pattern.finditer(prefix))
            if kind == "has_food":
                hits = [
                    h for h in hits
                    if not (h.group() == "還有" and _LIST_JOIN_BEFORE.search(sentence[: lo + h.start()]))
                ]
            elif kind == "skip_buying":
                hits = [h for h in hits if _LIST_TAIL.fullmatch(prefix[h.end():])]
            if not hits:
                continue
            distance = len(prefix) - hits[-1].end()
        else:
            hit = pattern.search(suffix)
            if hit is None:
                continue
            distance = hit.start()
        if best is None or (distance, order) < best[:2]:
            best = (distance, order, kind)
    return (best[2] if best else None), prefix, suffix


def _clause_of(sentence: str, fstart: int, fend: int) -> str:
    """food 所在那一段（逗號、分號、空白分段）。"""
    before = [m.end() for m in _CLAUSE_BREAK.finditer(sentence) if m.end() <= fstart]
    after = _CLAUSE_BREAK.search(sentence, fend)
    return sentence[before[-1] if before else 0:after.start() if after else len(sentence)].strip()


def _is_question(clause: str, kind: str) -> bool:
    clause = _TRAILING_YOU.sub("", clause)
    if not _CLAUSE_QUESTION.search(clause) or _RHETORICAL.search(clause):
        return False
    if kind == "wants_bought" and _POLITE_REQUEST.search(clause) and not _OFFER.search(clause):
        return False  # 「可以幫我買蛋嗎」是拜託
    return True


def extract(text: str) -> list[dict]:
    """從 text 抽 food signals。回 list[{kind, food, surface, confidence}]。

    v1 不歸個人 subject（GP2 A）；food 已 canonical（GP1 C5e）。
    同一則訊息同一種事件只留最後一次提到的，順序照最後提到的位置
    （「冰箱還有蛋，結果蛋吃完了，剛買了蛋」最後是有蛋）。
    """
    if not text or not text.strip():
        return []
    text = text[:500]
    text = _URL_RE.sub(" ", text)
    if not any(food in text for food in _FOOD_ITEMS):
        return []

    cancelled = bool(_CANCEL_CUE.search(text))
    signals: list[dict] = []
    for match in _SENTENCE_RE.finditer(text):
        sentence = match.group().strip()
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
            kind, prefix, suffix = _classify_food(sentence, fstart, fend)
            if not kind:
                continue
            if cancelled and kind in ("wants_bought", "wants_to_eat"):
                continue
            clause = _clause_of(sentence, fstart, fend)
            if kind in _STATE_KINDS and _is_question(clause, kind):
                continue
            if kind == "bought" and _CONDITIONAL.search(clause):
                continue  # 還沒買
            confidence = "low" if question_modal else "high"
            signals.append({
                "kind": kind,
                "food": canonical(food),     # 存 canonical（GP1 C5e）
                "surface": food,
                "confidence": confidence,
            })
            if (
                kind == "bought"
                and food not in _NON_FOOD_ITEMS
                and food not in _READY_MEALS
                and "還是" not in sentence
                and (_BOUGHT_TRIGGER.search(prefix) or _BOUGHT_SUFFIX_TRIGGER.search(suffix))
            ):
                # 買回來了，家裡就有（2026-10-07）
                signals.append({
                    "kind": "has_food",
                    "food": canonical(food),
                    "surface": food,
                    "confidence": confidence,
                })

    # 同 kind+food 只留最後一次（同一則訊息在 DB 只能有一筆，見 food_db PK）
    last = {(s["kind"], s["food"]): i for i, s in enumerate(signals)}
    return [s for i, s in enumerate(signals) if last[(s["kind"], s["food"])] == i]


def extract_and_store(
    group_id: str,
    source_msg_id: str,
    text: str,
    db_path: Path | str | None = None,
) -> int:
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
                source_msg_id=source_msg_id or "", source_text=text, db_path=db_path,
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
    db_path = food_db._DB_PATH

    def _run() -> None:
        try:
            extract_and_store(group_id, source_msg_id, text, db_path=db_path)
        except Exception:
            pass

    try:
        _EXECUTOR.submit(_run)
    except Exception as e:
        logger.warning("food_signals submit failed: %s", e)
