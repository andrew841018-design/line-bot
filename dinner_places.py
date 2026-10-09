"""dinner_places — 晚餐推薦只推查證過的店（Andrew 2026-10-09）。

10/7 的推薦是模型憑記憶寫的，把一家店配上另一家店的地址。Andrew：「任何資訊必須
驗證再驗證，他得是真的（加入測試環節）」。

- 清單在 ``dinner_places.json``。每一筆的店名＋地址都有兩個彼此獨立的來源，
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
# 同一個子句裡：口味前面有不／別／沒／除了（不太想吃日式、別再吃日式、除了日式），
# 或後面接以外／吃膩（日式以外、日式吃膩了），或整句以不要／算了結尾（日式不要），
# 就是不要這個口味；「要不要吃」「吃不吃」是在問。
_ASKING_RE = re.compile(r"(要|想|吃)不\1")
_CLAUSE_RE = re.compile(r"[^，,。！？!?；;、\s]+")
_NEG_BEFORE_RE = re.compile(r"不|別|沒|除了")
_NEG_AFTER_RE = re.compile(r"^(?:以外|之外|吃膩|膩)|^(?:不要|不想吃?|不吃|免了|算了)了?$")
_CHEAP_WORDS = ("便宜", "平價", "銅板", "省錢", "不要太貴")
_CHEAP_LIMIT = 250
_AMOUNT = r"(\d{2,5}|[一二兩三四五六七八九][千百](?:[一二兩三四五六七八九][百十]?)?)"
_BUDGET_RE = re.compile(
    rf"(?:預算|每人|每個人|一人|一個人|人均)\s*(?:約|大概|大約)?\s*{_AMOUNT}"
    rf"|{_AMOUNT}\s*(?:元|塊)?\s*(?:以內|以下|內|左右)"
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
    if not name or not address or not cuisine or len(set(sources)) < MIN_SOURCES:
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
    """(想吃的口味, 不想吃的口味)，逐子句判斷。"""
    text = _ASKING_RE.sub(r"\1", (asked or "").lower())
    wanted: set[str] = set()
    unwanted: set[str] = set()
    for clause in _CLAUSE_RE.findall(text):
        for tag, words in _TAG_WORDS.items():
            for word in words:
                word = word.lower()
                start = clause.find(word)
                while start != -1:
                    end = start + len(word)
                    if not (word.endswith("麵") and clause[end:end + 1] == "包"):  # 麵包 is bread
                        negated = _NEG_BEFORE_RE.search(clause[:start]) or _NEG_AFTER_RE.search(clause[end:])
                        (unwanted if negated else wanted).add(tag)
                    start = clause.find(word, start + 1)
    return wanted - unwanted, unwanted


def _amount(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    total = 0
    for digit, unit in re.findall(r"([一二兩三四五六七八九])([千百十]?)", token):
        total += _CN_AMOUNT[digit] * {"千": 1000, "百": 100, "十": 10, "": 1}[unit]
    return total or None


def budget_limit(asked: str) -> int | None:
    text = asked or ""
    match = _BUDGET_RE.search(text)
    if match:
        return _amount(match.group(1) or match.group(2))
    if any(word in text for word in _CHEAP_WORDS):
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
