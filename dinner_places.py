"""dinner_places — 晚餐推薦只推查證過的店（Andrew 2026-10-09）。

10/7 的推薦是模型憑記憶寫的，把一家店配上另一家店的地址。Andrew：「任何資訊必須
驗證再驗證，他得是真的（加入測試環節）」。

- 清單在 ``dinner_places.json``。每一筆的店名＋地址都有兩個彼此獨立的來源
  （按網站算，同一個網站的不同網址只算一個），
  另一輪查證再獨立重查一次，兩輪都對才收；``verified_on`` 超過 ``max_age_days``
  的店不推，等每日維護重新查證。
- 推薦文字完全由程式從清單組成，不經過模型。
- 送出前 :func:`verify_reply` 再逐塊比對一次：每個「🍽 店名」後面的「📍 地址」
  都必須和清單裡同一筆完全相同。
"""

from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DATA_PATH = Path(__file__).with_name("dinner_places.json")
DEFAULT_MAX_AGE_DAYS = 120
MIN_SOURCES = 2
_TW = ZoneInfo("Asia/Taipei")

NAME_MARK = "🍽 "
ADDRESS_MARK = "📍 "
HEADER = "善導寺站附近、店名和地址都查證過的幾間："
FOOTER = "營業時間可能調整，出門前再確認一下。"
NO_FRESH_TEXT = "目前沒有查證過、還在有效期內的晚餐推薦，等重新查證完再推薦。"

# 問句裡的口味 → 清單的 tags。只有清單 tags 用到的詞才有意義。
_TAG_WORDS: dict[str, tuple[str, ...]] = {
    "日式": ("日式", "日本料理", "日料", "拉麵", "壽司", "丼飯", "定食", "居酒屋", "日式咖哩"),
    "韓式": ("韓式", "韓國", "韓料", "韓食", "豆腐煲"),
    "台式": ("台式", "台菜", "小吃", "便當", "滷肉飯", "魯肉飯", "熱炒"),
    "麵食": ("麵食", "吃麵", "牛肉麵", "麵店", "麵館", "湯麵", "乾麵", "水餃", "餃子"),
    "中式": ("中式", "川菜", "江浙", "港式", "粵菜", "眷村", "燒臘", "合菜", "陝西"),
    "火鍋": ("火鍋", "鍋物", "涮涮鍋"),
    "素食": ("素食", "蔬食", "吃素"),
    "西式": ("西式", "義式", "義大利", "披薩", "pizza", "義大利麵", "排餐", "牛排", "漢堡"),
    "東南亞": ("東南亞", "泰式", "泰國菜", "越南", "河粉", "南洋", "海南雞", "新加坡"),
}
# 2026-10-10 review（第三輪）：判斷不了就不縮小範圍。把剛說不要的口味當成想吃，清單就只剩
# 那一種（「不要又吃火鍋」只推了火鍋那一家）；把想吃的當成不要，只是少幾個選項。所以：
# 1. 先換掉不是否定的說法：要不要／想不想（A不A 問句）、有沒有、不錯、不知道、不然、不如、
#    不管、沒吃過、好久沒（吃）、怎麼不…、「不要太貴／不要排隊」這種條件，以及沒有
#    「過／才／剛／已經」的「不是…嗎」反問（「不是要吃日式嗎」）；
# 2. 否定詞緊貼在口味前面（中間只有「想／要／又／每次／推薦／那家…」）→ 不想吃；
# 3. 口味後面緊接否定、一直到子句結尾（日式不要、日式就算了、日式我不想吃啦、對日式沒興趣），
#    或接「以外／吃膩／又來」→ 不想吃；
# 4. 否定問句（不吃火鍋嗎）、子句裡離口味比較遠的否定（今天不用加班想吃火鍋）→ 不確定：
#    不推也不排除；
# 5. 都沒有 → 想吃。
# 用「和／跟／、」連在一起的口味一起判斷（「日式、韓式都不要」「不要日式和韓式」）；否定詞夾在
# 兩串口味中間（「想吃韓式不要日式」）算後面那串的，前面那串只算不確定。
# 子句用「，。！？；」、空白和「但／可是／不過」切開（「不想煮飯 想吃火鍋」）；只有否定詞的
# 片段（「日式 不要」「不要 日式」）併回旁邊有口味的那段。「、」不切（「日式、韓式都不要」）。
_ASKING_RE = re.compile(r"(要|想|吃|是|會|去|用|愛|能|喜|可|考|好|行|對)不\1")
_RHETORICAL_RE = re.compile(r"不是(?=說?[要想][^，,。！？!?；;]*嗎)")
_DONE_RE = re.compile(r"過|才|剛|已經")
_NOT_NEGATION_RE = re.compile(
    r"有沒有|(?:要)?不然|不如|不管|不論|無論|不挑|不忌口|沒差|沒關係|沒問題|不知道|不曉得|不錯的?|要不(?=吃)|"
    r"沒吃過|(?:好|很)久(?:都)?沒有?吃|最近都沒有?吃|(?:怎麼|為什麼|為何|何)不(?=去?吃)|"
    r"(?:不會|不用|不要|不必|沒那麼|不能|別)太?"
    r"(?:貴|遠|辣|油|鹹|甜|久|晚|麻煩|擠|難等|排隊|等|連鎖店?|生的|冷|熱|吵)"
)
_CLAUSE_RE = re.compile(r"[^，,。！？!?；;]+")
_SPACE_RE = re.compile(r"[\s\u3000]+")
_CONTRAST_RE = re.compile(r"但是?|可是|不過")
_NEG_WORD = r"(?:不|別|沒|除了|跳過|排除)"
_NEG_MARK_RE = re.compile(rf"{_NEG_WORD}|(?<![a-z])pass(?![a-z])")
# 否定詞和口味之間（或口味後面的否定詞之後）允許的字
_FILLER = (
    r"(?:太|再|想|要|吃|用|有|是|很|會|去|愛|能|大|喜歡|可以|考慮|怎麼|那麼|又|每次|都|一直|老是|點|"
    r"推薦|推|給|選|找|必|需要|打算|敢|那家|那間|那種|這種|什麼|胃口|心情|興趣|同一家|一次|今天|今晚|我|"
    r"特別|真的)"
)
_PARTICLE = r"(?:了|啦|喔|哦|吧|耶|啊|呢|欸|嘛|囉|～|~|!|！|…|\.|\s|[\U0001F000-\U0001FAFF☀-➿])"
_NEG_BEFORE_RE = re.compile(rf"{_NEG_WORD}(?:{_FILLER}|\s){{0,8}}$")
_NEG_AFTER_RE = re.compile(
    rf"(?:就|也|都|先|這次|今天|今晚|我們|我|你|妳|他|她|\s)*{_NEG_WORD}(?:{_FILLER}|{_PARTICLE}|免|算)*$"
    rf"|(?:就|也|都|先|這次|\s)*(?:免了|算了){_PARTICLE}*$"
)
_EXCEPT_AFTER_RE = re.compile(
    r"[\s\u3000]*(?:以外|之外|吃膩|膩|吃到膩|又來|吃過了|❌|(?<![a-z])(?:ng|pass)(?![a-z]))"
)
_JUST_ATE_RE = re.compile(r"(?:才|剛|剛剛|已經|昨天|中午|昨晚)吃過")
_LATER_DAY_RE = re.compile(r"膩|改天|下次再")
_NEG_ONLY_RE = re.compile(rf"(?:{_NEG_WORD}(?:{_FILLER}|{_PARTICLE})*|免了|算了)")
_QUESTION_END_RE = re.compile(r"[嗎呢][\s\u3000～~]*$")


def _not_negations(text: str) -> str:
    text = _ASKING_RE.sub(r"\1", text)
    text = _NOT_NEGATION_RE.sub(lambda m: "有" if m.group(0) == "有沒有" else "", text)
    return text


def _clauses(text: str) -> list[str]:
    parts: list[str] = []
    for clause in _CLAUSE_RE.findall(text):
        if not _DONE_RE.search(clause):
            clause = _RHETORICAL_RE.sub("是", clause)
        for piece in _CONTRAST_RE.split(clause):
            parts.extend(chunk for chunk in _SPACE_RE.split(piece) if chunk)
    merged: list[str] = []
    pending = ""  # 只有否定詞、前面又沒有口味的片段，接到下一段前面（「不要 日式」）
    for part in parts:
        if _NEG_ONLY_RE.fullmatch(part) or _NEG_AFTER_RE.fullmatch(part):
            if merged and _mentions(merged[-1]):
                merged[-1] += " " + part  # 「日式 不要」
            else:
                pending += part + " "
            continue
        merged.append(pending + part)
        pending = ""
    if pending:
        merged.append(pending.strip())
    return merged


_JOIN_RE = re.compile(r"[\s\u3000、]*(?:和|跟|與|及|或|或是|或者|還有|以及)?[\s\u3000、]*")


def _stance(prefix: str, suffix: str, clause: str, more_after: bool) -> str:
    """一串口味的態度：'wanted' / 'unwanted' / 'unsure'。"""
    asking = bool(_QUESTION_END_RE.search(clause))
    if _JUST_ATE_RE.search(prefix) or _LATER_DAY_RE.search(suffix):
        return "unwanted"  # 「昨天才吃過火鍋」「火鍋吃到都膩了」「火鍋改天吧」
    if _NEG_BEFORE_RE.search(prefix):
        if prefix.rstrip().endswith("沒有") and not _NEG_MARK_RE.search(prefix.rstrip()[:-2]):
            return "unsure"  # 「附近沒有日式嗎」是在問有沒有
        return "unsure" if asking else "unwanted"
    if _NEG_MARK_RE.search(prefix):
        return "unsure"  # 離口味比較遠的否定：「今天不用加班想吃火鍋」「沒人想吃火鍋」
    if more_after and _NEG_MARK_RE.search(suffix):
        return "unsure"  # 否定詞在下一串口味前面：「想吃韓式不要日式」
    if _EXCEPT_AFTER_RE.match(suffix) or _NEG_AFTER_RE.match(suffix):
        return "unsure" if asking else "unwanted"
    if _NEG_MARK_RE.search(suffix):
        return "unsure"
    return "wanted"


def _mentions(clause: str) -> list[tuple[int, int, set[str]]]:
    """子句裡提到的口味（起點、終點、tags），重疊的併成一個，依位置排好。"""
    found: list[tuple[int, int, str]] = []
    for tag, words in _TAG_WORDS.items():
        for word in words:
            word = word.lower()
            start = clause.find(word)
            while start != -1:
                end = start + len(word)
                if not (word.endswith("麵") and clause[end:end + 1] == "包"):  # 麵包 is bread
                    found.append((start, end, tag))
                start = clause.find(word, start + 1)
    merged: list[tuple[int, int, set[str]]] = []
    for start, end, tag in sorted(found):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]), merged[-1][2] | {tag})
        else:
            merged.append((start, end, {tag}))
    return merged


_CHEAP_WORDS = ("便宜", "平價", "銅板", "省錢", "不要太貴")
_CHEAP_LIMIT = 250
# 2026-10-10 review：數字可以有千分位逗號（預算1,000、每人1,200）。
_AMOUNT = r"(\d{1,2}(?:,\d{3})+|\d{2,5}|[一二兩三四五六七八九][千百](?:[一二兩三四五六七八九][百十]?)?)"
_BUDGET_RE = re.compile(
    # 2026-10-10 review：「我一個人18:30到」的 18 是時刻；數字後面接數字／冒號／點就不是預算
    rf"(?:預算|每人|每個人|一人|一個人|人均)\s*(?:約|大概|大約)?\s*{_AMOUNT}(?![\d:：]|\s*點)"
    # 2026-10-10 review：「晚上18:30左右」「下午3:30以內」「6點30左右」的 30 是時刻，不是預算
    rf"|(?<![\d:：.])(?<!\d點){_AMOUNT}\s*(?:元|塊)?\s*(?:以內|以下|內|左右)"
)
_CN_AMOUNT = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


@dataclass(frozen=True)
class Place:
    name: str
    address: str
    cuisine: str
    tags: tuple[str, ...]
    price: str | None  # 顯示用，照來源寫法
    price_max: int | None  # 篩預算用：查證時從來源填的每人上限
    verified_on: date
    sources: tuple[str, ...]
    closed_weekdays: frozenset[int] = frozenset()  # 晚上不開的星期（0=週一…6=週日）


def _today() -> date:
    return datetime.now(_TW).date()


_SECOND_LEVEL = {"com", "org", "net", "gov", "edu", "co", "ac", "or", "ne", "idv", "go"}


def _source_sites(urls: tuple[str, ...] | list[str]) -> set[str]:
    """來源網址各自的網站（主網域，例如 tvbs.com.tw、pixnet.net）。

    2026-10-10 review：「兩個獨立來源」原本比對網址字串，同一個網站放兩個連結
    （www／沒有 www、只差大小寫或 query）也算兩個；現在按網站算。
    """
    sites: set[str] = set()
    for url in urls:
        try:
            parts = urlsplit(str(url).strip())
            host = parts.hostname or ""
        except ValueError:
            continue
        if parts.scheme not in ("http", "https"):
            continue
        labels = [label for label in host.rstrip(".").split(".") if label]
        # 2026-10-10 review（第三輪）：同一家媒體的子網域（supertaste.tvbs.com.tw 和
        # news.tvbs.com.tw、m.facebook.com）算同一個網站。
        keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL else 2
        if len(labels) >= 2:
            sites.add(".".join(labels[-keep:]))
    return sites


def _parse_place(raw: object) -> Place | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    address = str(raw.get("address") or "").strip()
    cuisine = str(raw.get("cuisine") or "").strip()
    sources = tuple(
        str(url).strip() for url in raw.get("sources") or () if str(url).strip().startswith("http")
    )
    try:
        verified_on = date.fromisoformat(str(raw.get("verified_on") or ""))
    except ValueError:
        return None
    if not name or not address or not cuisine or len(_source_sites(sources)) < MIN_SOURCES:
        return None
    if "\n" in name or "\n" in address:
        return None
    tags = tuple(str(tag).strip() for tag in raw.get("tags") or () if str(tag).strip())
    price = str(raw.get("price") or "").strip() or None
    price_max = raw.get("price_max")
    if not isinstance(price_max, int) or isinstance(price_max, bool) or price_max <= 0:
        price_max = None
    closed = frozenset(
        day for day in raw.get("closed_weekdays") or ()
        if isinstance(day, int) and not isinstance(day, bool) and 0 <= day <= 6
    )
    return Place(name, address, cuisine, tags, price, price_max, verified_on, sources, closed)


_cache: dict[str, tuple[float, tuple[list[Place], int]]] = {}
_warned: set[str] = set()


def load(path: Path | str | None = None) -> tuple[list[Place], int]:
    """(清單裡格式完整的店, max_age_days)。檔案壞掉 → ([], 預設天數)。

    依檔案修改時間快取：每日維護改了清單，下一則推薦就用新的，不用重啟。
    """
    target = Path(path or DATA_PATH)
    key = str(target)
    try:
        mtime = target.stat().st_mtime
    except OSError as exc:
        if key not in _warned:
            _warned.add(key)
            logger.warning("dinner places unreadable error_type=%s", type(exc).__name__)
        return [], DEFAULT_MAX_AGE_DAYS
    cached = _cache.get(key)
    if cached and cached[0] == mtime:
        return list(cached[1][0]), cached[1][1]
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if key not in _warned:
            _warned.add(key)
            logger.warning("dinner places unreadable error_type=%s", type(exc).__name__)
        return [], DEFAULT_MAX_AGE_DAYS
    if not isinstance(data, dict):
        return [], DEFAULT_MAX_AGE_DAYS
    max_age = data.get("max_age_days")
    if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age <= 0:
        max_age = DEFAULT_MAX_AGE_DAYS
    places: list[Place] = []
    skipped = 0
    for raw in data.get("places") or ():
        place = _parse_place(raw)
        if place is None:
            skipped += 1
            continue
        places.append(place)
    if skipped:
        logger.warning("dinner places skipped incomplete entries=%d", skipped)
    _warned.discard(key)
    _cache[key] = (mtime, (places, max_age))
    return list(places), max_age


def fresh(places: list[Place], today: date, max_age_days: int) -> list[Place]:
    """查證日在 max_age_days 內（含當天）、而且不是未來日期的店。"""
    return [p for p in places if 0 <= (today - p.verified_on).days <= max_age_days]


def open_tonight(places: list[Place], today: date) -> list[Place]:
    """排除今天晚上公休的店（查證時記下的公休日）。"""
    return [p for p in places if today.weekday() not in p.closed_weekdays]


def _tag_mentions(asked: str) -> tuple[set[str], set[str]]:
    """(想吃的口味, 不想吃的口味)，逐子句判斷；判斷不了的口味兩邊都不放。"""
    text = _not_negations((asked or "").lower())
    wanted: set[str] = set()
    unwanted: set[str] = set()
    unsure: set[str] = set()
    for clause in _clauses(text):
        mentions = _mentions(clause)
        groups: list[list[tuple[int, int, set[str]]]] = []
        for mention in mentions:
            if groups and _JOIN_RE.fullmatch(clause[groups[-1][-1][1]:mention[0]]):
                groups[-1].append(mention)
            else:
                groups.append([mention])
        previous_end = 0
        for index, group in enumerate(groups):
            start, end = group[0][0], group[-1][1]
            next_start = groups[index + 1][0][0] if index + 1 < len(groups) else len(clause)
            stance = _stance(
                clause[previous_end:start], clause[end:next_start], clause, index + 1 < len(groups)
            )
            tags = set().union(*(m[2] for m in group))
            {"wanted": wanted, "unwanted": unwanted, "unsure": unsure}[stance].update(tags)
            previous_end = end
    return wanted - unwanted - unsure, unwanted


_UNITS = {"千": 1000, "百": 100, "十": 10}


def _amount(token: str) -> int | None:
    """「三百五」→350、「兩千五」→2500、「一千二」→1200、「五百」→500、「1,200」→1200。"""
    token = token.replace(",", "")
    if token.isdigit():
        return int(token)
    total = 0
    last_unit = 0
    for digit, unit in re.findall(r"([一二兩三四五六七八九])([千百十]?)", token):
        value = _UNITS.get(unit) or (last_unit // 10 if last_unit else 1)
        total += _CN_AMOUNT[digit] * value
        last_unit = _UNITS.get(unit, 0)
    return total or None


# 2026-10-10 review（第三輪）：清單最便宜的店每人上限是 200 元；低於 100 的數字幾乎都是
# 時刻或日期（七點30左右、10/15左右、15 分鐘內），不當預算，免得顯示「每人 30 元內」。
_MIN_BUDGET = 100
_NOT_CHEAP_RE = re.compile(r"(?:不要太|不用|不要|別|不必|不想)(?:那麼|太)?(?:便宜|平價|銅板|省錢)")


def budget_limit(asked: str) -> int | None:
    text = asked or ""
    match = _BUDGET_RE.search(text)
    if match:
        amount = _amount(match.group(1) or match.group(2))
        return amount if amount is not None and amount >= _MIN_BUDGET else None
    if any(word in _NOT_CHEAP_RE.sub("", text) for word in _CHEAP_WORDS):
        return _CHEAP_LIMIT
    return None


def _pick(candidates: list[Place], count: int, rng: random.Random) -> list[Place]:
    """口味類別盡量不重複：每類先挑一間，不夠再補。"""
    pool = list(candidates)
    rng.shuffle(pool)
    picked: list[Place] = []
    seen: set[str] = set()
    for place in pool:
        if len(picked) >= count:
            break
        kind = place.tags[0] if place.tags else place.cuisine
        if kind not in seen:
            picked.append(place)
            seen.add(kind)
    for place in pool:
        if len(picked) >= count:
            break
        if place not in picked:
            picked.append(place)
    return picked


def render_block(place: Place) -> str:
    """店名、地址、料理、價位（有來源才有）。"""
    lines = [f"{NAME_MARK}{place.name}", f"{ADDRESS_MARK}{place.address}", f"🍴 {place.cuisine}"]
    if place.price:
        lines.append(f"💰 {place.price}")
    return "\n".join(lines)


def recommend(
    asked: str = "",
    *,
    today: date | None = None,
    rng: random.Random | None = None,
    count: int = 5,
    path: Path | str | None = None,
) -> str:
    """依問句挑 4–5 間查證過的店，回傳整則訊息；沒有可推的店回 NO_FRESH_TEXT。"""
    places, max_age = load(path)
    day = today or _today()
    candidates = open_tonight(fresh(places, day, max_age), day)
    if not candidates:
        return NO_FRESH_TEXT
    rng = rng or random.Random()
    tags, unwanted = _tag_mentions(asked)
    candidates = [p for p in candidates if not unwanted & set(p.tags)] or candidates
    limit = budget_limit(asked)
    matching = [
        p for p in candidates
        if (not tags or tags & set(p.tags))
        and (limit is None or (p.price_max is not None and p.price_max <= limit))
    ]
    notes: list[str] = []
    if matching:
        chosen = _pick(matching, count, rng)
    else:
        wish = "、".join(sorted(tags)) if tags else ""
        if limit is not None:
            wish = f"{wish}、每人 {limit} 元內" if wish else f"每人 {limit} 元內"
        notes.append(f"查證過的店裡沒有符合「{wish}」的，先列其他幾間：")
        chosen = _pick(candidates, count, rng)
    blocks = [render_block(p) for p in chosen]
    return "\n\n".join([HEADER, *notes, *blocks, FOOTER])


def verify_reply(text: str, *, path: Path | str | None = None, today: date | None = None) -> bool:
    """每個「🍽 店名」都要緊接著清單裡同一筆的「📍 地址」，而且那筆還在有效期內。

    沒有任何店名區塊的訊息（例如 NO_FRESH_TEXT）回 True：裡面沒有店家資訊。
    """
    places, max_age = load(path)
    known = {(p.name, p.address) for p in fresh(places, today or _today(), max_age)}
    lines = [line.strip() for line in (text or "").splitlines()]
    for index, line in enumerate(lines):
        if not line.startswith(NAME_MARK.strip()):
            continue
        name = line[len(NAME_MARK.strip()):].strip()
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        if not nxt.startswith(ADDRESS_MARK.strip()):
            return False
        address = nxt[len(ADDRESS_MARK.strip()):].strip()
        if (name, address) not in known:
            return False
    return True


def known_places(*, path: Path | str | None = None, today: date | None = None) -> list[tuple[str, str]]:
    """清單裡還在有效期內的 (店名, 地址)：地址查證關卡只在店名也寫在旁邊時才算有依據。"""
    places, max_age = load(path)
    return [(p.name, p.address) for p in fresh(places, today or _today(), max_age)]
