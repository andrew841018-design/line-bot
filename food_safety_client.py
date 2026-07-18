"""Read-only client for the public Food Safety Finder dataset.

The website publishes the same official flow records used by its static UI at
``/search-data.json``.  Keeping the lookup here deterministic avoids sending
restaurant names or food-safety decisions through the LLM path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import unicodedata

import requests


logger = logging.getLogger(__name__)

FOOD_SAFETY_DATA_URL = os.getenv(
    "FOOD_SAFETY_DATA_URL",
    "https://food-safety-finder.pages.dev/search-data.json",
)
_CACHE_TTL_SECONDS = max(
    30,
    int(os.getenv("FOOD_SAFETY_CACHE_TTL_SECONDS", "300")),
)
_ORDER_CONTEXT_TTL_SECONDS = max(
    60,
    int(os.getenv("FOOD_SAFETY_ORDER_CONTEXT_TTL_SECONDS", "600")),
)
_FETCH_TIMEOUT = (3.0, 8.0)
_FETCH_FAILURE_BACKOFF_SECONDS = 30.0
_MIN_FLOW_ROWS = max(1, int(os.getenv("FOOD_SAFETY_MIN_FLOW_ROWS", "3000")))
_MIN_MERCHANT_ALIASES = max(
    1, int(os.getenv("FOOD_SAFETY_MIN_MERCHANT_ALIASES", "900"))
)
_MIN_RETAINED_RATIO = min(
    1.0, max(0.0, float(os.getenv("FOOD_SAFETY_MIN_RETAINED_RATIO", "1.0")))
)
_MAX_DATASET_AGE_SECONDS = max(
    3600, int(os.getenv("FOOD_SAFETY_MAX_DATASET_AGE_SECONDS", str(48 * 3600)))
)
_MAX_FUTURE_SKEW_SECONDS = max(
    0, int(os.getenv("FOOD_SAFETY_MAX_FUTURE_SKEW_SECONDS", "600"))
)
_MAX_OFFICIAL_DATA_AGE_SECONDS = max(
    3600,
    int(
        os.getenv(
            "FOOD_SAFETY_MAX_OFFICIAL_DATA_AGE_SECONDS",
            str(48 * 3600),
        )
    ),
)
_MANIFEST_PATH = Path(
    os.getenv(
        "FOOD_SAFETY_MANIFEST_PATH",
        str(
            Path.home()
            / ".local"
            / "state"
            / "line_bot"
            / "food_safety_manifest.json"
        ),
    )
).expanduser()
_REQUIRED_OFFICIAL_SOURCE_SYNCS = frozenset(
    {
        "TFDA_NEWS",
        "TFDA_DOWNSTREAM",
        "TFDA_AFFECTED_PRODUCTS",
        "CHANGHUA_FLOWCHART",
        "CHANGHUA_DOWNSTREAM",
        "CATALOG_7ELEVEN",
    }
)
_cache_lock = threading.Lock()
_cache_condition = threading.Condition(_cache_lock)
_context_lock = threading.Lock()
_cached_dataset: dict | None = None
_cached_at = 0.0
_dataset_refreshing = False
_last_fetch_failure_at = 0.0
_warm_cache_started = False
_pending_order_groups: dict[tuple[str, str], float] = {}

_RESTAURANT_INTENT_RE = re.compile(
    r"訂(?!單|製)|預約|預訂(?!單|製)|預定(?!單|製)|聚餐|吃飯|吃東西|"
    r"(?:想|要|打算|準備|決定|一起|去|來)吃"
)
_SAFETY_LOOKUP_RE = re.compile(
    r"食安|食品安全|致癌油|是否安全|安全嗎|安全[?？]|"
    r"能不能吃|可以吃嗎|可不可以吃|能否吃|"
    r"能不能買|可以買嗎|可不可以買|能否買|"
    r"不能買嗎|不可以買嗎|不能吃嗎|不可以吃嗎"
)
_EXPLICIT_FOOD_SAFETY_RE = re.compile(r"食安|食品安全|致癌油")
_LOOKUP_REQUEST_RE = re.compile(
    r"查一下|查查|查詢|幫我查|確認|看一下|幫我看|想知道|請問|想問"
)
_LOOKUP_QUESTION_RE = re.compile(
    r"(?:是否安全)[。！!]?$|"
    r"(?:有食安問題|食安|食品安全|安全|受影響|影響|波及|"
    r"能不能吃|不能吃|不可以吃|可以吃|可不可以吃|能否吃|"
    r"能不能買|不能買|不可以買|可以買|可不可以買|能否買)"
    r"(?:嗎|呢|[?？])[。！!]*$"
)
_LOOKUP_SUFFIX_RE = re.compile(
    r"(?:有食安問題|有沒有食安問題|有無食安問題|是否有食安問題|"
    r"食安狀況|食品安全狀況|安全狀況|食安問題|食安紀錄|"
    r"食安影響|食安波及|食品安全|是否安全|食安)[。！!?？]*$"
)
_PURCHASE_OR_CONSUMPTION_RE = re.compile(
    r"(?:可不可以|能不能|是否可以|是否能|能否)(?:購買|買|食用|吃)|"
    r"(?<!不)(?:可以|能|可)(?:購買|買|食用|吃)(?:嗎|呢|[?？])"
)
_SAFETY_PERMISSION_RE = re.compile(
    r"(?:食安|食品安全|食安狀況).{0,8}(?:可以|能|可)(?:購買|買|食用|吃)$"
)
_ORDER_CONTEXT_RE = re.compile(
    r"(?:我先來訂|我來訂|我要先訂|我要訂|先幫忙訂|先訂|開始訂|"
    r"幫大家訂|團訂|訂餐廳|訂餐|訂位|預約|預訂|預定|聚餐)(?:了|啦|喔|哦|吧|嗎)?"
)
_NEGATED_ORDER_RE = re.compile(
    r"(?:沒有|不是|並非|不要|不用|沒|不)"
    r"(?:是|在|要|想|打算|準備|會|能|可以|去)*"
    r"(?:訂|預約|預訂|預定|聚餐|吃)"
)
_NEGATED_LOOKUP_RE = re.compile(
    r"(?:沒有|不要|不用|不想|不需要|無需|不是)"
    r"(?:(?:要|想|需要)|(?:幫我|請你|麻煩你?))*"
    r"(?:食安|食品安全)?(?:查|查詢|確認)(?:一下)?(?:食安|食品安全)?"
)
_NEGATED_PURCHASE_RE = re.compile(
    r"(?:(?<!要)不要|別|(?<!可)不可以|(?<!能)不能)"
    r"(?:購買|買|食用|吃)"
)
_LEADING_MENTIONS_RE = re.compile(
    r"^(?:@[^\s，,、：:。！？!?]+[\s，,、：:。！？!?]*)+"
)
_ORDER_SUBJECT_PREFIX_RE = re.compile(
    r"^(?:(?:幫我|幫忙|麻煩)(?:訂|預約)|"
    r"(?:我們|我|大家|全家)?"
    r"(?:今天|明天|今晚|中午|早上|下午|晚上)?"
    r"(?:要不要|想要|一起去|打算|準備|決定|想|要|一起|去|來)?"
    r"(?:訂餐廳|訂餐|訂位|訂(?!單|製)|預約|預訂(?!單|製)|"
    r"預定(?!單|製)|聚餐|吃(?!飯|東西|的)))"
)
_RESERVATION_SUFFIX_RE = re.compile(r"[0-9一二兩三四五六七八九十]+位$")
_FOOD_SUBJECT_HINT_RE = re.compile(
    r"飯糰|便當|水餃|蒸餃|牛奶|鮮乳|乳品|麵包|吐司|蛋糕|"
    r"餅乾|雞胸肉|牛肉|豬肉|羊肉|魚肉|水果|蘋果|香蕉|"
    r"咖啡|飲料|果汁|食品|零食|食用油|沙拉油|調合油|調和油|大豆油|油品|"
    r"(?:飯|麵|粥|湯|餃|包|堡|肉|魚|菜|果|油|醬|茶|奶)$"
)
_SUBJECT_PROSE_RE = re.compile(
    r"^(?:這是|這只是|只是|不是|沒有|因為|但是|如果|所以|"
    r"這台|這雙|這件|這個|那個|這次)|(?:內容|新聞|政策|標準|心得)$"
    r"|(?:事件|爭議|議題|後續|風波|傳聞|消息|討論)$"
)
_DIRECT_CATALOG_PROSE_RE = re.compile(
    r"(?:我|你|他|她|我們|你們|他們|大家|這(?:台|雙|件|個|條|款|張|間)|"
    r"那(?:台|雙|件|個|條|款)|今天|明天|昨天|今晚|週末|平日|以前|現在|"
    r"覺得|工作|上班|下班|股票|漲停|服務|會議|開會|好累|很好吃|好吃|"
    r"買了|吃了|剛|喝完|還沒|沒到|完了|請|記得|護照|一起|團購|"
    r"最近|漲價|很貴|推薦|排隊|好久|看電影|電影|"
    r"爬山|散步|等等|稍等|"
    r"晚點|再說|到家|真的假的|真的|假的|什麼|普通文字|觸發詞|"
    r"天氣|下雨|雨下|好冷|不錯|好大|晚安|吃飽|飽了|收到|喔|囉|"
    r"怎麼|如何|為什麼|幾點|有問題)"
)
_FOOD_CHATTER_PREDICATE_RE = re.compile(
    r"(?:很|真|太|超)(?:甜|酸|鹹|辣|苦|貴|便宜|新鮮|少|多|大|小|"
    r"好喝|難喝|好吃|難吃)|"
    r"(?<!甜)不(?:甜|酸|鹹|辣|苦|貴|便宜|新鮮|好喝|難喝|好吃|難吃)"
)
_FOOD_POLICY_MARKER_RE = re.compile(
    r"農藥|殘留|標準|容許量|放寬|放大|提高|降低|改成|法規|政策|"
    r"公告|新聞|報導|廠商|ppm",
    re.IGNORECASE,
)
_FOOD_POLICY_CONTEXT_RE = re.compile(
    r"驗出|驗到|檢出|抽驗|超標|查獲|研究|調查|新聞|報導|指出|表示|"
    r"宣布|要求|接受|公告"
)
_MERCHANT_SHAPE_RE = re.compile(
    r"(?:餐廳|餐館|商行|酒店|飯店|咖啡|食堂|小館|小吃|股份有限公司|公司|館|店)$"
)
_MASKED_PERSON_RE = re.compile(
    r"[\u3400-\u9fff]{1,2}[OoＯ0○◯XxＸ＊*][\u3400-\u9fff]{1,2}"
)
_SNAPSHOT_ID_RE = re.compile(r"[0-9a-fA-F]{32}")
_BUSINESS_LEGAL_DESCRIPTOR_RE = re.compile(
    r"股份有限公司|有限責任公司|有限公司|國際"
)
_PRODUCT_PACKAGING_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:ml|l|cc|g|kg|公克|公斤|毫升|公升|入|包|瓶|罐|"
    r"盒|袋|桶|組|片|顆|粒|份)(?:\b|\*|x|×|$)",
    re.IGNORECASE,
)
_PRODUCT_NAME_MARKER_RE = re.compile(
    r"[-()（）/／*×+]|[A-Za-z0-9]|餐|食|烹調|原料|使用|"
    r"鰻|豚骨|餅|芝士|麻糬|醬|豬|排|鴨|雞|茶|肉|蛋|魚|壽司|海鮮|鍋|"
    r"油|腸|咖哩|湯|泥|筍|翅|豆|菇|芋|瓜|蔬果|香料|餡|甜不辣|"
    r"燒|滷|炸|烤|焙|調味|沙拉|美乃滋|鍋貼|肉鬆|烘焙|果子|白汁|"
    r"剁椒|蒜|花生|風味|調合"
)
_PRODUCT_RELATION_DISCOURSE_RE = re.compile(
    r"有受到|受到|受影響|致癌油|食安|食品安全|事件"
)
_NON_RESTAURANT_ORDER_RE = re.compile(
    r"機票|高鐵票|火車票|車票|演唱會票|門票|牙醫|醫生|診所|門診|按摩|"
    r"剪髮|美髮|住宿|旅館|計程車|叫車|行程|課程|會議室|場地|"
    r"iPhone|手機|電腦|筆電|平板|耳機|相機|一束花|花束|鮮花|"
    r"醫師|醫院|民宿|飯店|展覽票|電影票"
)
_DINING_TARGET_SUFFIX_RE = re.compile(
    r"(?:餐廳|餐館|食堂|小館|小吃店|咖啡館|咖啡廳)$"
)
_STARTUP_BARE_RESTAURANT_BRANDS = frozenset(
    {"鬍鬚張", "瓦城", "海霸王", "忠青商行", "晶華酒店", "客美多"}
)
_RESTAURANT_FILLER_RE = re.compile(
    r"訂單|今晚|今天|明天|晚餐|午餐|早餐|吃什麼|吃哪|吃飯|吃東西|"
    r"訂位|訂餐廳|訂餐|預約|預訂|預定|聚餐|一間|一家|某間|哪家|哪間|餐廳|店家|"
    r"推薦|什麼|有沒有|有問題|問題|安全|影響|波及|致癌油|致癌|受到|"
    r"受影響|使用|事件|這次|這個|這家|那家|嗎|呢|是|有|沒有|我先來|我來|"
    r"我要|幫大家|幫忙|開始|先|吃|訂"
)
_ORDER_TARGET_CHATTER_RE = re.compile(
    r"^(?:很|太|超|不|沒|有|了|飽|飯|東西|的|什麼|哪|"
    r"今天|明天|今晚|晚餐|午餐|早餐|一點|更多|少點)"
)
_DIRECT_NAME_EXCLUSION_RE = re.compile(
    r"^(?:我|你|他|她|今天|明天|今晚|現在|等一下|訂單|訂單已送出|已送出|送出|完成|"
    r"謝謝|收到|好的|好|可以|不要|不是|請|幫|晚餐|午餐|早餐|吃飯|晚安|早安)$"
)
_BOT_ADDRESS_RE = re.compile(r"^(?:米堡|米寶|咪寶|咪宝)[，,、:：\s]*")
_QUERY_EDGE_CHARS = " \t，,、：:。！？!?；;"
_URL_IN_TEXT_RE = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
_BARE_SCHEDULE_RE = re.compile(
    r"^(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}|\d{1,2}月\d{1,2}日)(?=$|\D)"
)
_BARE_SCHEDULE_ACTION_RE = re.compile(
    r"打(?:羽球|壁球|球)|羽球|壁球|聚餐|一起吃飯|吃飯|行程|活動|"
    r"開會|會議|看醫生|回診|出發|到站|預約場地"
)

# These are conversational wrappers rather than food/store vocabulary. The
# parser repeatedly removes them from the edges, which lets new combinations
# (for example 「幫我看一下 X 有無食安問題」) work without a bespoke regex.
_QUERY_LEADING_PHRASES = tuple(
    sorted(
        {
            "食安查詢",
            "查詢食安",
            "幫我查食安",
            "我要你確認",
            "我要確認",
            "請幫我確認",
            "幫我確認",
            "麻煩你確認",
            "請你確認",
            "幫我看一下",
            "幫我看",
            "幫我查一下",
            "幫我查",
            "請幫我查",
            "麻煩幫我查",
            "可以幫我查",
            "請協助查",
            "請查",
            "麻煩查",
            "查查",
            "查",
            "我想知道",
            "我想確認",
            "我想查",
            "我在店裡看到",
            "在店裡看到",
            "店裡看到",
            "請問",
            "想問",
            "想查",
            "確認一下",
            "確認",
            "查詢一下",
            "查一下",
            "查詢",
            "幫忙",
            "麻煩你",
            "請你",
            "幫我",
        },
        key=len,
        reverse=True,
    )
)
_QUERY_TRAILING_PHRASES = tuple(
    sorted(
        {
            "可不可以購買",
            "可不可以食用",
            "能不能購買",
            "能不能食用",
            "是否可以購買",
            "是否可以食用",
            "是否能購買",
            "是否能食用",
            "可以購買",
            "可以食用",
            "可不可以買",
            "可不可以吃",
            "能不能買",
            "能不能吃",
            "不能購買嗎",
            "不能食用嗎",
            "不可以購買嗎",
            "不可以食用嗎",
            "不能買嗎",
            "不能吃嗎",
            "不可以買嗎",
            "不可以吃嗎",
            "不能購買",
            "不能食用",
            "不可以購買",
            "不可以食用",
            "不能買",
            "不能吃",
            "不可以買",
            "不可以吃",
            "是否能買",
            "是否能吃",
            "是否可以買",
            "是否可以吃",
            "是否安全",
            "是不是安全",
            "有沒有食安問題",
            "有食安問題嗎",
            "有食安問題",
            "有無食安問題",
            "是否有食安問題",
            "食安狀況",
            "食品安全狀況",
            "安全狀況",
            "食安問題",
            "食安紀錄",
            "食安影響",
            "食安波及",
            "受到影響",
            "受影響",
            "可以買",
            "可以吃",
            "能買",
            "能吃",
            "可買",
            "可吃",
            "能否買",
            "能否吃",
            "想問",
            "如何",
            "怎麼樣",
            "怎樣",
            "好不好",
            "有沒有",
            "有無",
            "是否有",
            "是否",
            "能否",
            "可以",
            "問題",
            "紀錄",
            "影響",
            "波及",
            "食安",
            "食品安全",
            "安全嗎",
            "安全",
            "狀況",
            "有",
            "無",
            "否",
            "嗎",
            "呢",
        },
        key=len,
        reverse=True,
    )
)


@dataclass(frozen=True)
class _MerchantMatch:
    canonical_name: str
    normalized_alias: str
    alias_kind: str = "alias"


def _business_matches_alias(
    alias: str, business: str, *, allow_brand_prefix: bool = False
) -> bool:
    if not alias or not business:
        return False
    if alias == business:
        return True

    # Official records sometimes publish a legal entity rather than the public
    # brand (for example 晶華酒店 -> 晶華國際酒店股份有限公司). Only accept an
    # exact match after removing legal descriptors; never use the old reverse
    # substring/fuzzy rule that caused one branch to absorb sibling branches.
    alias_key = _BUSINESS_LEGAL_DESCRIPTOR_RE.sub("", alias)
    business_key = _BUSINESS_LEGAL_DESCRIPTOR_RE.sub("", business)
    if len(alias_key) >= 4 and alias_key == business_key:
        return True
    return bool(allow_brand_prefix and alias in business)


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(character for character in normalized if character.isalnum())


def reset_cache() -> None:
    """Clear the in-process cache (primarily useful for tests and deploys)."""
    global _cached_dataset, _cached_at, _dataset_refreshing
    global _last_fetch_failure_at, _warm_cache_started
    with _cache_condition:
        _cached_dataset = None
        _cached_at = 0.0
        _dataset_refreshing = False
        _last_fetch_failure_at = 0.0
        _warm_cache_started = False
        _cache_condition.notify_all()
    with _context_lock:
        _pending_order_groups.clear()


def _order_context_key(
    group_id: str | None, sender_id: str | None
) -> tuple[str, str] | None:
    if not group_id or not sender_id:
        return None
    return (group_id, sender_id)


def _mark_order_context(group_id: str | None, sender_id: str | None) -> None:
    key = _order_context_key(group_id, sender_id)
    if key is None:
        return
    now = time.monotonic()
    with _context_lock:
        stale_keys = [
            item
            for item, started_at in _pending_order_groups.items()
            if now - started_at > _ORDER_CONTEXT_TTL_SECONDS
        ]
        for stale_key in stale_keys:
            _pending_order_groups.pop(stale_key, None)
        _pending_order_groups[key] = now


def _consume_pending_order_context(
    group_id: str | None, sender_id: str | None
) -> bool:
    key = _order_context_key(group_id, sender_id)
    if key is None:
        return False
    now = time.monotonic()
    with _context_lock:
        started_at = _pending_order_groups.pop(key, None)
        if started_at is None:
            return False
        if now - started_at > _ORDER_CONTEXT_TTL_SECONDS:
            return False
        return True


def _parse_dataset_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _flow_record_keys(rows: list[object]) -> set[tuple[str, str]]:
    return {
        (
            str(row.get("source_url") or "").strip(),
            str(row.get("stable_key") or "").strip(),
        )
        for row in rows
        if isinstance(row, dict)
    }


def _alias_record_keys(rows: list[object]) -> set[tuple[str, str, str]]:
    return {
        (
            normalize_text(str(row.get("normalized_alias") or "")),
            normalize_text(str(row.get("canonical_name") or "")),
            str(row.get("alias_kind") or ""),
        )
        for row in rows
        if isinstance(row, dict)
    }


def _catalog_record_keys(rows: list[object]) -> set[tuple[str, str]]:
    return {
        (
            normalize_text(str(row.get("normalized_merchant_name") or "")),
            normalize_text(str(row.get("normalized_product_name") or "")),
        )
        for row in rows
        if isinstance(row, dict)
    }


def _manifest_key_rows(
    value: object, field: str, *, key_length: int = 2
) -> set[tuple[str, ...]]:
    if not isinstance(value, list):
        raise ValueError(f"food safety manifest {field} must be a list")
    keys: set[tuple[str, str]] = set()
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != key_length
            or any(not isinstance(part, str) or not part for part in item)
        ):
            raise ValueError(f"food safety manifest {field} has an invalid key")
        keys.add(tuple(item))
    return keys


def _load_accepted_manifest() -> dict | None:
    if not _MANIFEST_PATH.exists():
        return None
    payload = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("food safety manifest schema is invalid")
    if _parse_dataset_timestamp(payload.get("generated_at")) is None:
        raise ValueError("food safety manifest generated_at is invalid")
    for field in ("flow_keys", "alias_keys", "catalog_keys"):
        _manifest_key_rows(
            payload.get(field),
            field,
            key_length=3 if field == "alias_keys" else 2,
        )
    return payload


def _manifest_accepts_dataset(dataset: dict, manifest: dict) -> bool:
    manifest_generated = _parse_dataset_timestamp(manifest.get("generated_at"))
    dataset_generated = _parse_dataset_timestamp(dataset.get("generated_at"))
    if (
        manifest_generated is None
        or dataset_generated is None
        or dataset_generated < manifest_generated
    ):
        return False
    return bool(
        _manifest_key_rows(manifest.get("flow_keys"), "flow_keys").issubset(
            _flow_record_keys(dataset.get("flow_rows", []))
        )
        and _manifest_aliases_accept_dataset(dataset, manifest)
        and _manifest_key_rows(
            manifest.get("catalog_keys"), "catalog_keys"
        ).issubset(_catalog_record_keys(dataset.get("catalog_matches", [])))
    )


def _manifest_aliases_accept_dataset(dataset: dict, manifest: dict) -> bool:
    previous = _manifest_key_rows(
        manifest.get("alias_keys"), "alias_keys", key_length=3
    )
    current = _alias_record_keys(dataset.get("merchant_aliases", []))
    if not previous.issubset(current):
        return False
    business_names = {
        normalize_text(str(row.get("normalized_business_name") or ""))
        for row in dataset.get("flow_rows", [])
        if isinstance(row, dict)
    }
    for normalized_alias, canonical_name, alias_kind in current - previous:
        if alias_kind not in {"merchant", "masked_person"}:
            return False
        if normalized_alias != canonical_name or canonical_name not in business_names:
            return False
    return True


def _write_accepted_manifest(dataset: dict) -> None:
    manifest = {
        "schema_version": 1,
        "snapshot_id": dataset["snapshot_id"],
        "generated_at": dataset["generated_at"],
        "flow_keys": [
            list(key) for key in sorted(_flow_record_keys(dataset["flow_rows"]))
        ],
        "alias_keys": [
            list(key)
            for key in sorted(_alias_record_keys(dataset["merchant_aliases"]))
        ],
        "catalog_keys": [
            list(key)
            for key in sorted(
                _catalog_record_keys(dataset.get("catalog_matches", []))
            )
        ],
    }
    _MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = _MANIFEST_PATH.with_name(
        f".{_MANIFEST_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, _MANIFEST_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def _valid_dataset(
    payload: object, previous_dataset: dict | None = None
) -> dict | None:
    if not isinstance(payload, dict):
        return None
    snapshot_id = payload.get("snapshot_id")
    if (
        payload.get("schema_version") != 1
        or not isinstance(snapshot_id, str)
        or not _SNAPSHOT_ID_RE.fullmatch(snapshot_id.strip())
    ):
        return None
    generated_datetime = _parse_dataset_timestamp(payload.get("generated_at"))
    official_data_datetime = _parse_dataset_timestamp(
        payload.get("official_data_updated_at")
    )
    if generated_datetime is None or official_data_datetime is None:
        return None
    official_source_syncs = payload.get("official_source_syncs")
    if not isinstance(official_source_syncs, dict) or not official_source_syncs:
        return None
    if set(official_source_syncs) != _REQUIRED_OFFICIAL_SOURCE_SYNCS:
        return None
    parsed_source_syncs: list[datetime] = []
    for source_name, synced_at in official_source_syncs.items():
        if not isinstance(source_name, str) or not source_name.strip():
            return None
        parsed = _parse_dataset_timestamp(synced_at)
        if parsed is None:
            return None
        parsed_source_syncs.append(parsed)
    now = datetime.now(timezone.utc)
    dataset_age = (now - generated_datetime).total_seconds()
    official_data_age = (now - official_data_datetime).total_seconds()
    if dataset_age > _MAX_DATASET_AGE_SECONDS:
        return None
    if dataset_age < -_MAX_FUTURE_SKEW_SECONDS:
        return None
    if official_data_age > _MAX_OFFICIAL_DATA_AGE_SECONDS:
        return None
    if official_data_age < -_MAX_FUTURE_SKEW_SECONDS:
        return None
    for source_sync in parsed_source_syncs:
        source_age = (now - source_sync).total_seconds()
        if source_age > _MAX_OFFICIAL_DATA_AGE_SECONDS:
            return None
        if source_age < -_MAX_FUTURE_SKEW_SECONDS:
            return None
    if official_data_datetime.astimezone(timezone.utc) != min(
        parsed_source_syncs
    ).astimezone(timezone.utc):
        return None
    coverage = payload.get("coverage")
    aliases = payload.get("merchant_aliases")
    flow_rows = payload.get("flow_rows")
    if (
        not isinstance(coverage, dict)
        or not isinstance(aliases, list)
        or not isinstance(flow_rows, list)
    ):
        return None
    coverage_values: dict[str, int] = {}
    for key in (
        "official_flow_records",
        "official_businesses",
        "official_products",
    ):
        value = coverage.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        coverage_values[key] = value
    if coverage_values["official_flow_records"] != len(flow_rows):
        return None
    if flow_rows and (
        coverage_values["official_businesses"] == 0
        or coverage_values["official_products"] == 0
    ):
        return None
    if len(flow_rows) < _MIN_FLOW_ROWS or len(aliases) < _MIN_MERCHANT_ALIASES:
        return None
    for alias in aliases:
        if not isinstance(alias, dict):
            return None
        if not isinstance(alias.get("normalized_alias"), str) or not str(
            alias["normalized_alias"]
        ).strip():
            return None
        if not isinstance(alias.get("canonical_name"), str) or not str(
            alias["canonical_name"]
        ).strip():
            return None
        alias_kind = alias.get("alias_kind")
        if alias_kind not in {
            "alias",
            "merchant",
            "masked_person",
        }:
            return None
    normalized_businesses: set[str] = set()
    normalized_products: set[str] = set()
    official_product_sources: dict[str, set[str]] = {}
    flow_record_keys: set[tuple[str, str]] = set()
    for row in flow_rows:
        if not isinstance(row, dict):
            return None
        for field in (
            "business_name",
            "normalized_business_name",
            "food_name",
            "normalized_food_name",
            "stable_key",
            "source_url",
            "source_title",
            "published_at",
            "current_state",
        ):
            if not isinstance(row.get(field), str) or not str(row[field]).strip():
                return None
        normalized_business = normalize_text(str(row["normalized_business_name"]))
        normalized_food = normalize_text(str(row["normalized_food_name"]))
        if normalized_business != normalize_text(str(row["business_name"])):
            return None
        if normalized_food != normalize_text(str(row["food_name"])):
            return None
        normalized_businesses.add(normalized_business)
        normalized_products.add(normalized_food)
        official_product_sources.setdefault(normalized_food, set()).add(
            str(row["source_url"]).strip()
        )
        business_kind = row.get("business_kind")
        if business_kind is not None and business_kind not in {
            "merchant",
            "masked_person",
        }:
            return None
        record_key = (
            str(row["source_url"]).strip(),
            str(row["stable_key"]).strip(),
        )
        if record_key in flow_record_keys:
            return None
        flow_record_keys.add(record_key)
    if coverage_values["official_businesses"] != len(normalized_businesses):
        return None
    if coverage_values["official_products"] != len(normalized_products):
        return None
    for alias in aliases:
        alias_kind = str(alias["alias_kind"])
        if alias_kind == "alias":
            continue
        normalized_alias = normalize_text(str(alias["normalized_alias"]))
        normalized_canonical = normalize_text(str(alias["canonical_name"]))
        if (
            normalized_alias != normalized_canonical
            or normalized_canonical not in normalized_businesses
        ):
            return None
        canonical_is_masked = bool(
            _MASKED_PERSON_RE.search(str(alias["canonical_name"]))
        )
        if (alias_kind == "masked_person") != canonical_is_masked:
            return None
    catalog_matches = payload.get("catalog_matches", [])
    if not isinstance(catalog_matches, list):
        return None
    for row in catalog_matches:
        if not isinstance(row, dict):
            return None
        for field in (
            "merchant_name",
            "normalized_merchant_name",
            "product_name",
            "normalized_product_name",
            "source_url",
            "official_source_url",
        ):
            if not isinstance(row.get(field), str) or not str(row[field]).strip():
                return None
        if row.get("safety_status") != "possible_match":
            return None
        normalized_merchant = normalize_text(str(row["normalized_merchant_name"]))
        normalized_product = normalize_text(str(row["normalized_product_name"]))
        if normalized_merchant != normalize_text(str(row["merchant_name"])):
            return None
        if normalized_product != normalize_text(str(row["product_name"])):
            return None
        if normalized_product not in normalized_products:
            return None
        if str(row["official_source_url"]).strip() not in official_product_sources.get(
            normalized_product, set()
        ):
            return None
    if previous_dataset is not None:
        previous_generated_datetime = _parse_dataset_timestamp(
            previous_dataset.get("generated_at")
        )
        if (
            previous_generated_datetime is not None
            and generated_datetime < previous_generated_datetime
        ):
            return None
        previous_rows = previous_dataset.get("flow_rows")
        if isinstance(previous_rows, list) and previous_rows:
            minimum_retained = int(len(previous_rows) * _MIN_RETAINED_RATIO)
            if len(flow_rows) < minimum_retained:
                return None
            if not _flow_record_keys(previous_rows).issubset(flow_record_keys):
                return None
        previous_aliases = previous_dataset.get("merchant_aliases")
        if isinstance(previous_aliases, list) and previous_aliases:
            minimum_aliases = int(
                len(previous_aliases) * _MIN_RETAINED_RATIO
            )
            if len(aliases) < minimum_aliases:
                return None
            if not _alias_record_keys(previous_aliases).issubset(
                _alias_record_keys(aliases)
            ):
                return None
        previous_catalog = previous_dataset.get("catalog_matches")
        if isinstance(previous_catalog, list) and previous_catalog:
            if not _catalog_record_keys(previous_catalog).issubset(
                _catalog_record_keys(catalog_matches)
            ):
                return None
    return payload


def _load_dataset() -> dict | None:
    global _cached_dataset, _cached_at, _dataset_refreshing, _last_fetch_failure_at
    now = time.monotonic()
    previous_dataset = None
    with _cache_condition:
        if _cached_dataset is not None and now - _cached_at < _CACHE_TTL_SECONDS:
            return _cached_dataset
        if now - _last_fetch_failure_at < _FETCH_FAILURE_BACKOFF_SECONDS:
            return None
        if _dataset_refreshing:
            deadline = now + sum(_FETCH_TIMEOUT) + 1.0
            while _dataset_refreshing:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                _cache_condition.wait(timeout=remaining)
            refreshed_at = time.monotonic()
            if (
                _cached_dataset is not None
                and refreshed_at - _cached_at < _CACHE_TTL_SECONDS
            ):
                return _cached_dataset
            return None
        previous_dataset = _cached_dataset
        _dataset_refreshing = True

    dataset = None
    try:
        accepted_manifest = _load_accepted_manifest()
        response = requests.get(FOOD_SAFETY_DATA_URL, timeout=_FETCH_TIMEOUT)
        response.raise_for_status()
        dataset = _valid_dataset(response.json(), previous_dataset)
        if (
            dataset is not None
            and accepted_manifest is not None
            and not _manifest_accepts_dataset(dataset, accepted_manifest)
        ):
            dataset = None
            logger.warning("food safety dataset violates accepted manifest retention")
        if dataset is not None:
            _write_accepted_manifest(dataset)
        if dataset is None:
            logger.warning("food safety dataset has an unexpected shape")
    except Exception as exc:
        logger.warning("food safety dataset unavailable: %s", exc)
    finally:
        with _cache_condition:
            if dataset is not None:
                _cached_dataset = dataset
                _cached_at = time.monotonic()
                _last_fetch_failure_at = 0.0
            else:
                _last_fetch_failure_at = time.monotonic()
            _dataset_refreshing = False
            _cache_condition.notify_all()
    return dataset


def _cache_snapshot() -> tuple[dict | None, bool, bool]:
    now = time.monotonic()
    with _cache_condition:
        fresh = bool(
            _cached_dataset is not None
            and now - _cached_at < _CACHE_TTL_SECONDS
        )
        return _cached_dataset, fresh, _dataset_refreshing


def warm_cache_async() -> None:
    """Warm the official catalog once at service startup without delaying health."""
    global _warm_cache_started
    with _cache_condition:
        if _warm_cache_started:
            return
        _warm_cache_started = True
    threading.Thread(
        target=_load_dataset,
        name="food-safety-cache-warm",
        daemon=True,
    ).start()


def _restaurant_name_fragment(text: str) -> str:
    """Remove common ordering words before matching a flow-business prefix."""
    return normalize_text(_RESTAURANT_FILLER_RE.sub("", text))


def _without_leading_mentions(text: str) -> str:
    cleaned = " ".join((text or "").split()).strip()
    cleaned = _LEADING_MENTIONS_RE.sub("", cleaned).strip()
    while cleaned and not (cleaned[0].isalnum() or cleaned[0] == "@"):
        cleaned = cleaned[1:].lstrip()
    return cleaned


def _catalog_subject(text: str, *, max_length: int = 100) -> str:
    cleaned = _BOT_ADDRESS_RE.sub("", _without_leading_mentions(text)).strip(
        _QUERY_EDGE_CHARS
    )
    normalized = normalize_text(cleaned)
    if not 2 <= len(normalized) <= max_length:
        return ""
    if _DIRECT_NAME_EXCLUSION_RE.fullmatch(cleaned) or _SUBJECT_PROSE_RE.search(cleaned):
        return ""
    if any(ord(character) < 32 for character in cleaned):
        return ""
    if re.search(r"[。！？!?；;]", cleaned):
        return ""
    return cleaned


def _direct_catalog_subject(text: str) -> str:
    """Accept a bare store/product token, not a conversational sentence."""
    cleaned = _BOT_ADDRESS_RE.sub("", _without_leading_mentions(text)).strip(
        _QUERY_EDGE_CHARS
    )
    if _URL_IN_TEXT_RE.search(cleaned):
        return ""
    if (
        _BARE_SCHEDULE_RE.search(cleaned)
        and (
            _BARE_SCHEDULE_ACTION_RE.search(cleaned)
            or (
                not _FOOD_SUBJECT_HINT_RE.search(cleaned)
                and not _MERCHANT_SHAPE_RE.search(cleaned)
            )
        )
    ):
        return ""
    subject = _catalog_subject(cleaned, max_length=40)
    if (
        not subject
        or _DIRECT_CATALOG_PROSE_RE.search(subject)
        or _FOOD_CHATTER_PREDICATE_RE.search(subject)
        or _looks_like_food_policy_prose(subject)
    ):
        return ""
    return subject


def _looks_like_food_policy_prose(subject: str) -> bool:
    markers = {
        match.group(0).casefold()
        for match in _FOOD_POLICY_MARKER_RE.finditer(subject)
    }
    return bool(
        len(markers) >= 2
        or _FOOD_POLICY_CONTEXT_RE.search(subject)
    )


def _is_ambiguous_bare_code(subject: str) -> bool:
    """Treat short machine-like tokens as chat unless they name a known merchant."""
    machine_token = unicodedata.normalize("NFKC", subject or "").strip()
    normalized = normalize_text(subject)
    return bool(
        normalized
        and (
            machine_token.isdigit()
            or (
                len(normalized) <= 3
                and machine_token.isascii()
                and machine_token.isalnum()
            )
        )
    )


def _should_format_direct_no_match(subject: str) -> bool:
    """Return no-match only for food/store-shaped direct input."""
    return bool(
        normalize_text(subject) in _STARTUP_BARE_RESTAURANT_BRANDS
        or
        _FOOD_SUBJECT_HINT_RE.search(subject) or _MERCHANT_SHAPE_RE.search(subject)
    )


def _should_sync_direct_lookup(subject: str) -> bool:
    return bool(
        _should_format_direct_no_match(subject)
        or _PRODUCT_PACKAGING_RE.search(subject)
        or _PRODUCT_NAME_MARKER_RE.search(subject)
    )


def _concise_subject(text: str) -> str:
    """Return a short store/product-shaped subject, never an arbitrary clause."""
    cleaned = _BOT_ADDRESS_RE.sub("", _without_leading_mentions(text)).strip(
        _QUERY_EDGE_CHARS
    )
    for suffix in ("可以嗎", "好嗎", "行嗎", "嗎", "呢", "吧"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].rstrip(_QUERY_EDGE_CHARS)
            break
    return _catalog_subject(cleaned, max_length=18)


def _pending_order_subject(text: str) -> str:
    """Accept a one-shot restaurant follow-up, never a conversational filler."""
    subject = _concise_subject(text)
    if not subject or _DIRECT_CATALOG_PROSE_RE.search(subject):
        return ""
    return subject


def _extract_order_subject(text: str) -> str:
    """Extract the short subject from an anchored order/dining command."""
    cleaned = _BOT_ADDRESS_RE.sub("", _without_leading_mentions(text)).strip(
        _QUERY_EDGE_CHARS
    )
    match = _ORDER_SUBJECT_PREFIX_RE.match(cleaned)
    if match is None:
        return ""
    subject = _RESERVATION_SUFFIX_RE.sub("", cleaned[match.end() :]).rstrip(
        _QUERY_EDGE_CHARS
    )
    concise = _concise_subject(subject)
    if _DIRECT_CATALOG_PROSE_RE.search(concise):
        return ""
    return concise


def _is_order_context_start(text: str) -> bool:
    cleaned = _BOT_ADDRESS_RE.sub("", _without_leading_mentions(text)).strip(
        _QUERY_EDGE_CHARS
    )
    return bool(_ORDER_CONTEXT_RE.fullmatch(cleaned))


def _extract_safety_query(text: str) -> str:
    """Extract the store/product subject from a conversational safety query.

    This intentionally does not depend on one complete sentence pattern.
    Conversation scaffolding is removed from both edges until no more applies,
    so leading commands and stacked question phrases can be freely combined.
    """
    query = _BOT_ADDRESS_RE.sub("", _without_leading_mentions(text)).strip(
        _QUERY_EDGE_CHARS
    )
    stripped_tail = False
    while query:
        before = query
        for phrase in _QUERY_LEADING_PHRASES:
            if query.startswith(phrase):
                query = query[len(phrase) :].lstrip(_QUERY_EDGE_CHARS)
                break
        else:
            for phrase in _QUERY_TRAILING_PHRASES:
                if query.endswith(phrase):
                    query = query[: -len(phrase)].rstrip(_QUERY_EDGE_CHARS)
                    stripped_tail = True
                    break
            else:
                if stripped_tail and query.endswith("的"):
                    query = query[:-1].rstrip(_QUERY_EDGE_CHARS)
                else:
                    break
        if query == before:
            break
    return query.strip(_QUERY_EDGE_CHARS)


def _format_missing_query() -> str:
    return "食安查詢需要店家或商品名稱，例如「乳香世家牛奶」或「鬍鬚張」。"


def _format_dataset_unavailable() -> str:
    return (
        "⚠️ 官方食安資料暫時無法連線，目前不能完成核對。\n"
        "請稍後再查；這次不會改用一般網路摘要代替官方資料。\n"
        "完整查詢：https://food-safety-finder.pages.dev/"
    )


def official_dataset_unavailable_message() -> str:
    """Public fail-closed response for callers guarding unexpected exceptions."""
    return _format_dataset_unavailable()


def is_explicit_food_safety_request(text: str) -> bool:
    """Return whether a message explicitly asks for an official safety lookup."""
    intent_text = _without_leading_mentions(text).strip()
    if not intent_text or _NEGATED_LOOKUP_RE.search(intent_text):
        return False
    query_text = _extract_safety_query(intent_text)
    weak_lookup_intent = bool(_SAFETY_LOOKUP_RE.search(intent_text))
    lookup_request = bool(
        _LOOKUP_REQUEST_RE.search(intent_text)
        and (weak_lookup_intent or _LOOKUP_QUESTION_RE.search(intent_text))
    )
    lookup_question = bool(
        _LOOKUP_QUESTION_RE.search(intent_text)
        and (weak_lookup_intent or _LOOKUP_REQUEST_RE.search(intent_text))
    )
    lookup_suffix = bool(
        _LOOKUP_SUFFIX_RE.search(intent_text) and _catalog_subject(query_text)
    )
    purchase_question = bool(
        _PURCHASE_OR_CONSUMPTION_RE.search(intent_text)
        or _SAFETY_PERMISSION_RE.search(intent_text)
    )
    return bool(
        lookup_request or lookup_question or lookup_suffix or purchase_question
    )


def should_fail_closed_on_lookup_error(text: str) -> bool:
    """Protect messages that should have received a deterministic official result."""
    intent_text = _without_leading_mentions(text).strip()
    snapshot, _, _ = _cache_snapshot()
    if is_explicit_food_safety_request(text):
        if _EXPLICIT_FOOD_SAFETY_RE.search(intent_text):
            return True
        query_subject = _catalog_subject(_extract_safety_query(intent_text))
        normalized_query = normalize_text(query_subject)
        if query_subject and (
            normalized_query in _STARTUP_BARE_RESTAURANT_BRANDS
            or _FOOD_SUBJECT_HINT_RE.search(query_subject)
            or _MERCHANT_SHAPE_RE.search(query_subject)
            or _PRODUCT_PACKAGING_RE.search(query_subject)
            or (
                snapshot is not None
                and _dataset_contains_direct_subject(snapshot, query_subject)
            )
        ):
            return True
        return False
    direct_subject = _direct_catalog_subject(intent_text)
    if direct_subject and not _is_ambiguous_bare_code(direct_subject):
        if snapshot is not None and _dataset_contains_direct_subject(
            snapshot, direct_subject
        ):
            return True
        if _should_sync_direct_lookup(direct_subject):
            return True
    order_subject = _extract_order_subject(intent_text)
    return bool(
        order_subject
        and (
            _is_recognized_order_merchant_subject(order_subject, snapshot)
            or _is_explicit_restaurant_order_target(order_subject)
        )
    )


def _business_alias_kind(name: str) -> str:
    return "masked_person" if _MASKED_PERSON_RE.search(name) else "merchant"


def _is_plausible_unknown_merchant_subject(subject: str) -> bool:
    """Allow synthetic no-match results only for clearly merchant-shaped names."""
    normalized = normalize_text(subject)
    if not normalized:
        return False
    if normalized in _STARTUP_BARE_RESTAURANT_BRANDS:
        return True
    fragment = _restaurant_name_fragment(subject)
    return bool(_MERCHANT_SHAPE_RE.search(subject) and len(fragment) >= 2)


def _is_nonrestaurant_order_target(subject: str) -> bool:
    """Keep lodging/transport bookings out unless a dining venue is explicit."""
    return bool(
        _NON_RESTAURANT_ORDER_RE.search(subject)
        and not _DINING_TARGET_SUFFIX_RE.search(subject)
    )


def _is_explicit_restaurant_order_target(subject: str) -> bool:
    """Permit a concise, non-food dining target during a cold-cache window."""
    candidate = _catalog_subject(subject, max_length=18)
    normalized = normalize_text(candidate)
    merchant_fragment = _restaurant_name_fragment(candidate)
    if (
        not candidate
        or len(normalized) < 2
        or len(merchant_fragment) < 2
        or _is_nonrestaurant_order_target(candidate)
        or _FOOD_SUBJECT_HINT_RE.search(candidate)
        or _DIRECT_CATALOG_PROSE_RE.search(candidate)
        or _FOOD_CHATTER_PREDICATE_RE.search(candidate)
        or _ORDER_TARGET_CHATTER_RE.search(candidate)
    ):
        return False
    chinese_characters = sum("\u3400" <= char <= "\u9fff" for char in normalized)
    return chinese_characters >= 2 or (
        len(normalized) >= 3 and normalized.isascii() and normalized.isalnum()
    )


def _find_merchants(
    text: str,
    aliases: list[object],
    flow_rows: list[object],
    *,
    allow_unknown_prefix: bool = False,
) -> list[_MerchantMatch]:
    normalized_text = normalize_text(text)
    matches: dict[str, _MerchantMatch] = {}
    business_kinds: dict[str, str] = {}
    for row in flow_rows:
        if not isinstance(row, dict):
            continue
        business_name = str(row.get("business_name") or "").strip()
        normalized_business = normalize_text(
            str(row.get("normalized_business_name") or business_name)
        )
        if not normalized_business:
            continue
        published_kind = str(row.get("business_kind") or "").strip()
        if published_kind not in {"merchant", "masked_person"}:
            published_kind = _business_alias_kind(business_name)
        business_kinds[normalized_business] = published_kind
    candidates = []
    for item in aliases:
        if not isinstance(item, dict):
            continue
        alias = normalize_text(str(item.get("normalized_alias") or ""))
        canonical = str(item.get("canonical_name") or "").strip()
        if alias and canonical:
            alias_kind = str(item.get("alias_kind") or "").strip()
            if alias_kind not in {"merchant", "masked_person", "alias"}:
                normalized_canonical = normalize_text(canonical)
                if alias == normalized_canonical and alias in business_kinds:
                    alias_kind = business_kinds[alias]
                elif _MASKED_PERSON_RE.search(canonical):
                    alias_kind = "masked_person"
                else:
                    alias_kind = "alias"
            kind_priority = 1 if alias_kind != "alias" else 0
            candidates.append(
                (len(alias), kind_priority, alias, canonical, alias_kind)
            )
    for _, _, alias, canonical, alias_kind in sorted(candidates, reverse=True):
        if alias in normalized_text:
            matches.setdefault(
                canonical,
                _MerchantMatch(
                    canonical_name=canonical,
                    normalized_alias=alias,
                    alias_kind=alias_kind,
                ),
            )
    if matches:
        return list(matches.values())

    # Official downstream records often contain only branch names such as
    # 忠青商行-巨城新竹. Recover a user-entered brand prefix even when the
    # seeded merchant alias table does not contain the shorter brand name.
    fragment = _restaurant_name_fragment(text)
    chinese_characters = sum("\u3400" <= char <= "\u9fff" for char in fragment)
    if len(fragment) >= 3 and chinese_characters >= 3 and any(
        isinstance(row, dict)
        and _business_matches_alias(
            fragment,
            normalize_text(
                str(row.get("normalized_business_name") or row.get("business_name") or "")
            ),
            allow_brand_prefix=True,
        )
        for row in flow_rows
    ):
        business_kind = business_kinds.get(fragment, "alias")
        return [
            _MerchantMatch(
                canonical_name=fragment,
                normalized_alias=fragment,
                alias_kind=business_kind,
            )
        ]
    if allow_unknown_prefix:
        candidate = _concise_subject(text)
        normalized_candidate = _restaurant_name_fragment(candidate)
        if (
            candidate
            and len(normalized_candidate) >= 2
            and _is_plausible_unknown_merchant_subject(candidate)
        ):
            # The alert dataset is not a full restaurant directory. A concise
            # explicit order may still receive a deterministic no-match, but
            # arbitrary prose must never become a synthetic merchant name.
            return [
                _MerchantMatch(
                    canonical_name=candidate,
                    normalized_alias=normalized_candidate,
                    alias_kind="merchant",
                )
            ]
    return list(matches.values())


def _is_recognized_order_merchant_subject(
    subject: str, dataset: dict | None = None
) -> bool:
    """Check whether an order target is safe to treat as a merchant."""
    if _is_plausible_unknown_merchant_subject(subject):
        return True
    if dataset is None:
        return False
    return any(
        merchant.alias_kind != "masked_person"
        for merchant in _find_merchants(
            subject,
            dataset.get("merchant_aliases", []),
            dataset.get("flow_rows", []),
            allow_unknown_prefix=False,
        )
    )


def _has_merchant_product_relation(text: str, merchant: _MerchantMatch) -> bool:
    """Recognize an explicit connector between a merchant and product."""
    cleaned = _without_leading_mentions(text)
    for connector in ("的", "：", ":"):
        if connector not in cleaned:
            continue
        merchant_part, product_part = cleaned.split(connector, 1)
        if (
            merchant.normalized_alias in normalize_text(merchant_part)
            and len(normalize_text(product_part)) >= 2
        ):
            return True
        if (
            merchant.normalized_alias in normalize_text(product_part)
            and len(normalize_text(merchant_part)) >= 2
        ):
            return True
    return False


def _product_subject_for_merchant(
    query: str, merchant: _MerchantMatch
) -> str:
    """Remove an explicitly separated merchant prefix from a product query."""
    cleaned = query.strip(_QUERY_EDGE_CHARS)
    for connector in ("的", "：", ":"):
        if connector not in cleaned:
            continue
        merchant_part, product_part = cleaned.split(connector, 1)
        if merchant.normalized_alias in normalize_text(merchant_part):
            return product_part.strip(_QUERY_EDGE_CHARS)
        if merchant.normalized_alias in normalize_text(product_part):
            return merchant_part.strip(_QUERY_EDGE_CHARS)
    parts = cleaned.split(None, 1)
    if len(parts) == 2 and merchant.normalized_alias in normalize_text(parts[0]):
        return parts[1].strip(_QUERY_EDGE_CHARS)
    if len(parts) == 2 and merchant.normalized_alias in normalize_text(parts[1]):
        return parts[0].strip(_QUERY_EDGE_CHARS)
    return cleaned


def _unseparated_product_subject(
    query: str, merchant: _MerchantMatch
) -> str:
    """Return a normalized product suffix after an unseparated merchant alias."""
    normalized_query = normalize_text(query)
    if normalized_query.startswith(merchant.normalized_alias):
        suffix = normalized_query[len(merchant.normalized_alias) :]
    elif normalized_query.endswith(merchant.normalized_alias):
        suffix = normalized_query[: -len(merchant.normalized_alias)]
    else:
        return ""
    return suffix if len(suffix) >= 2 else ""


def _merchant_match_aliases(
    dataset: dict, merchant: _MerchantMatch
) -> set[str]:
    normalized_canonical = normalize_text(merchant.canonical_name)
    match_aliases = {merchant.normalized_alias, normalized_canonical}
    for item in dataset.get("merchant_aliases", []):
        if not isinstance(item, dict):
            continue
        item_canonical = normalize_text(str(item.get("canonical_name") or ""))
        if item_canonical != normalized_canonical:
            continue
        alias = normalize_text(str(item.get("normalized_alias") or ""))
        if alias:
            match_aliases.add(alias)
    return match_aliases


def _matching_rows(dataset: dict, merchant: _MerchantMatch) -> list[dict]:
    rows: list[dict] = []
    match_aliases = _merchant_match_aliases(dataset, merchant)
    for item in dataset.get("flow_rows", []):
        if not isinstance(item, dict):
            continue
        business = normalize_text(
            str(item.get("normalized_business_name") or item.get("business_name") or "")
        )
        if any(
            _business_matches_alias(
                alias,
                business,
                allow_brand_prefix=merchant.alias_kind == "alias",
            )
            for alias in match_aliases
        ):
            rows.append(item)
    return rows


def _matching_catalog_rows(
    dataset: dict, merchant: _MerchantMatch
) -> list[dict]:
    match_aliases = _merchant_match_aliases(dataset, merchant)
    rows: list[dict] = []
    for item in dataset.get("catalog_matches", []):
        if not isinstance(item, dict):
            continue
        catalog_merchant = normalize_text(
            str(item.get("normalized_merchant_name") or item.get("merchant_name") or "")
        )
        if any(
            _business_matches_alias(alias, catalog_merchant)
            for alias in match_aliases
        ):
            rows.append(item)
    return rows


def _is_high_confidence_bare_merchant(
    merchant: _MerchantMatch, subject: str, dataset: dict
) -> bool:
    """Allow bare aliases only when they look like stores or recur as a brand."""
    normalized_subject = normalize_text(subject)
    if merchant.alias_kind == "masked_person":
        return False
    if (
        normalize_text(merchant.canonical_name) != normalized_subject
        and merchant.normalized_alias != normalized_subject
    ):
        return False
    if (
        merchant.alias_kind == "merchant"
        or _MERCHANT_SHAPE_RE.search(subject)
        or normalized_subject in _STARTUP_BARE_RESTAURANT_BRANDS
        or re.search(r"[A-Za-z0-9]", subject)
        or len(normalized_subject) >= 4
    ):
        return True
    if _matching_catalog_rows(dataset, merchant):
        return True
    businesses = {
        normalize_text(str(row.get("business_name") or ""))
        for row in _matching_rows(dataset, merchant)
        if str(row.get("business_name") or "").strip()
    }
    return len(businesses) >= 3


def _dataset_contains_direct_subject(dataset: dict, subject: str) -> bool:
    if _product_rows(dataset, subject) or _catalog_product_rows(dataset, subject):
        return True
    return _dataset_contains_direct_merchant(dataset, subject)


def _dataset_contains_exact_direct_subject(dataset: dict, subject: str) -> bool:
    """Recognize an exact official name even when it contains chat-like words."""
    normalized_subject = normalize_text(subject)
    if not normalized_subject:
        return False
    if any(
        normalize_text(str(row.get("normalized_food_name") or ""))
        == normalized_subject
        for row in dataset.get("flow_rows", [])
        if isinstance(row, dict)
    ) or any(
        normalize_text(str(row.get("normalized_product_name") or ""))
        == normalized_subject
        for row in dataset.get("catalog_matches", [])
        if isinstance(row, dict)
    ):
        return True
    return any(
        normalize_text(str(row.get("normalized_alias") or ""))
        == normalized_subject
        and str(row.get("alias_kind") or "") != "masked_person"
        for row in dataset.get("merchant_aliases", [])
        if isinstance(row, dict)
    )


def _dataset_contains_exact_masked_merchant(dataset: dict, subject: str) -> bool:
    normalized_subject = normalize_text(subject)
    return bool(
        normalized_subject
        and any(
            normalize_text(str(row.get("normalized_alias") or ""))
            == normalized_subject
            and str(row.get("alias_kind") or "") == "masked_person"
            for row in dataset.get("merchant_aliases", [])
            if isinstance(row, dict)
        )
    )


def _dataset_contains_direct_merchant(dataset: dict, subject: str) -> bool:
    merchants = _find_merchants(
        subject,
        dataset.get("merchant_aliases", []),
        dataset.get("flow_rows", []),
        allow_unknown_prefix=False,
    )
    return any(
        _is_high_confidence_bare_merchant(merchant, subject, dataset)
        for merchant in merchants
    )


def _product_rows(dataset: dict, text: str) -> list[dict]:
    """Match an explicit product name against official flow-row product names.

    Only normalized substring matches are returned. A branded query such as
    ``乳香世家牛奶`` must not be silently broadened to an unrelated product
    that merely happens to contain ``牛奶``.
    """
    normalized_query = normalize_text(text)
    if len(normalized_query) < 2:
        return []
    matches: list[dict] = []
    for row in dataset.get("flow_rows", []):
        if not isinstance(row, dict):
            continue
        food_name = str(row.get("food_name") or "").strip()
        normalized_food = normalize_text(
            str(row.get("normalized_food_name") or food_name)
        )
        if normalized_food and normalized_query in normalized_food:
            matches.append(row)
    return matches


def _catalog_product_rows(dataset: dict, text: str) -> list[dict]:
    normalized_query = normalize_text(text)
    if len(normalized_query) < 2:
        return []
    return [
        row
        for row in dataset.get("catalog_matches", [])
        if isinstance(row, dict)
        and normalized_query
        in normalize_text(
            str(row.get("normalized_product_name") or row.get("product_name") or "")
        )
    ]


def _format_catalog_possible_result(query: str, rows: list[dict]) -> str:
    unique: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (
            str(row.get("merchant_name") or ""),
            str(row.get("product_name") or ""),
        )
        unique.setdefault(key, row)
    rows_sorted = sorted(unique.values(), key=lambda row: (
        str(row.get("merchant_name") or ""),
        str(row.get("product_name") or ""),
    ))
    lines = [
        "⚠️ 官方受影響品名相符，請核對",
        f"查詢：{query}",
        f"共 {len(rows_sorted)} 項店內商品名稱與官方受影響品名完全相符：",
    ]
    for index, row in enumerate(rows_sorted[:12], 1):
        lines.append(
            f"{index}. 店家：{row.get('merchant_name') or '未標示店家'}\n"
            f"商品：{row.get('product_name') or '未標示商品'}"
        )
    if len(rows_sorted) > 12:
        lines.append(f"…其餘 {len(rows_sorted) - 12} 項請開啟網站查完整清單。")
    lines.append(
        "未逐店確認：品名相符不等於該店所有批次受影響，請核對包裝批次、效期與官方來源。"
    )
    source_urls = []
    official_source_urls = []
    for row in rows_sorted:
        url = str(row.get("source_url") or "").strip()
        if url and url not in source_urls:
            source_urls.append(url)
        official_url = str(row.get("official_source_url") or "").strip()
        if official_url and official_url not in official_source_urls:
            official_source_urls.append(official_url)
    if official_source_urls:
        lines.append("官方受影響品名來源：" + official_source_urls[0])
    lines.append(
        "商品目錄來源："
        + (source_urls[0] if source_urls else "https://food-safety-finder.pages.dev/")
    )
    return "\n".join(lines)


def _append_catalog_possible_result(
    official_result: str, query: str, rows: list[dict]
) -> str:
    if not rows:
        return official_result
    return official_result + "\n\n" + _format_catalog_possible_result(query, rows)


def _display_dataset_timestamp(dataset: dict) -> str:
    parsed = _parse_dataset_timestamp(
        dataset.get("official_data_updated_at") or dataset.get("generated_at")
    )
    if parsed is None:
        return "未提供"
    taipei_time = parsed.astimezone(timezone(timedelta(hours=8)))
    return taipei_time.strftime("%Y-%m-%d %H:%M（台灣時間）")


def _format_product_result(query: str, rows: list[dict], dataset: dict) -> str:
    unique: dict[tuple[str, str, str, str], dict] = {}
    for row in rows:
        key = (
            str(row.get("business_name") or ""),
            str(row.get("food_name") or ""),
            str(row.get("batch") or ""),
            str(row.get("expiry") or ""),
        )
        unique.setdefault(key, row)
    if not unique:
        generated = _display_dataset_timestamp(dataset)
        return (
            f"🔎 食安查詢：目前查無「{query}」相符的官方食安警示。\n"
            "這不代表保證安全，仍請以現場標示與官方公告為準。\n"
            f"官方資料更新：{generated}\n"
            "完整查詢：https://food-safety-finder.pages.dev/"
        )

    rows_sorted = sorted(
        unique.values(),
        key=lambda row: (
            str(row.get("business_name") or ""),
            str(row.get("food_name") or ""),
            str(row.get("batch") or ""),
            str(row.get("expiry") or ""),
        ),
    )
    merchants = sorted(
        {
            str(row.get("business_name") or "").strip()
            for row in rows_sorted
            if str(row.get("business_name") or "").strip()
        }
    )
    merchant_label = "、".join(merchants[:12]) or "官方資料未標示"
    if len(merchants) > 12:
        merchant_label += f" 等 {len(merchants)} 家"
    lines = [
        "🚨 官方食安資料命中",
        f"商品查詢：{query}",
        f"相符官方業者：{merchant_label}",
        f"共 {len(rows_sorted)} 筆官方流向／警示紀錄：",
    ]
    for index, row in enumerate(rows_sorted[:12], 1):
        details = [
            f"{index}. 商家：{row.get('business_name') or '未標示業者'}",
            f"官方警示：{row.get('food_name') or '未標示內容'}",
        ]
        batch = str(row.get("batch") or "").strip()
        expiry = str(row.get("expiry") or "").strip()
        if batch:
            details.append(f"批次／產品編號：{batch}")
        if expiry:
            details.append(f"有效／保存期限：{expiry}")
        lines.append("\n".join(details))
    if len(rows_sorted) > 12:
        lines.append(f"…其餘 {len(rows_sorted) - 12} 筆請開啟網站查完整清單。")
    source_urls = []
    for row in rows_sorted:
        url = str(row.get("source_url") or "").strip()
        if url and url not in source_urls:
            source_urls.append(url)
        if len(source_urls) == 2:
            break
    lines.append("請再核對批次／效期；這不代表整間店所有食品都受影響。")
    lines.append("官方來源：" + (source_urls[0] if source_urls else "https://food-safety-finder.pages.dev/"))
    if len(source_urls) > 1:
        lines.append("官方來源 2：" + source_urls[1])
    return "\n".join(lines)


def _format_result(merchant: _MerchantMatch, rows: list[dict], dataset: dict) -> str:
    unique: dict[tuple[str, str, str, str], dict] = {}
    for row in rows:
        key = (
            str(row.get("business_name") or ""),
            str(row.get("food_name") or ""),
            str(row.get("batch") or ""),
            str(row.get("expiry") or ""),
        )
        unique.setdefault(key, row)

    if not unique:
        generated = _display_dataset_timestamp(dataset)
        return (
            f"🔎 食安查詢：目前查無「{merchant.canonical_name}」相符的官方食安警示。\n"
            "這不代表保證安全，仍請以現場標示與官方公告為準。\n"
            f"官方資料更新：{generated}\n"
            "完整查詢：https://food-safety-finder.pages.dev/"
        )

    rows_sorted = sorted(
        unique.values(),
        key=lambda row: (
            str(row.get("food_name") or ""),
            str(row.get("batch") or ""),
            str(row.get("expiry") or ""),
        ),
    )
    official_merchants = sorted(
        {
            str(row.get("business_name") or "").strip()
            for row in rows_sorted
            if str(row.get("business_name") or "").strip()
        }
    )
    if official_merchants:
        merchant_label = official_merchants[0]
        if len(official_merchants) > 1:
            merchant_label += f" 等 {len(official_merchants)} 個官方業者紀錄"
    else:
        merchant_label = merchant.canonical_name
    lines = [
        "🚨 官方食安資料命中",
        f"商家：{merchant_label}",
        f"共 {len(rows_sorted)} 筆官方流向／警示紀錄：",
    ]
    max_items = 12
    for index, row in enumerate(rows_sorted[:max_items], 1):
        details = [
            f"{index}. 商家：{row.get('business_name') or '未標示業者'}",
            f"官方警示：{row.get('food_name') or '未標示內容'}",
        ]
        batch = str(row.get("batch") or "").strip()
        expiry = str(row.get("expiry") or "").strip()
        if batch:
            details.append(f"批次／產品編號：{batch}")
        if expiry:
            details.append(f"有效／保存期限：{expiry}")
        lines.append("\n".join(details))
    if len(rows_sorted) > max_items:
        lines.append(f"…其餘 {len(rows_sorted) - max_items} 筆請開啟網站查完整清單。")

    source_urls = []
    for row in rows_sorted:
        url = str(row.get("source_url") or "").strip()
        if url and url not in source_urls:
            source_urls.append(url)
        if len(source_urls) == 2:
            break
    lines.append("請再核對批次／效期；這不代表整間店所有食品都受影響。")
    lines.append("官方來源：" + (source_urls[0] if source_urls else "https://food-safety-finder.pages.dev/"))
    if len(source_urls) > 1:
        lines.append("官方來源 2：" + source_urls[1])
    return "\n".join(lines)


def check_restaurant_message(
    text: str,
    *,
    group_id: str | None = None,
    sender_id: str | None = None,
) -> str | None:
    """Return a deterministic official result for merchant or product queries."""
    if not text:
        return None

    intent_text = _without_leading_mentions(text).strip()
    query_text = _extract_safety_query(intent_text)
    order_subject = _extract_order_subject(intent_text)
    weak_lookup_intent = bool(_SAFETY_LOOKUP_RE.search(intent_text))
    lookup_request = bool(
        _LOOKUP_REQUEST_RE.search(intent_text)
        and (
            weak_lookup_intent
            or _LOOKUP_QUESTION_RE.search(intent_text)
        )
    )
    lookup_question = bool(
        _LOOKUP_QUESTION_RE.search(intent_text)
        and (weak_lookup_intent or _LOOKUP_REQUEST_RE.search(intent_text))
    )
    lookup_suffix = bool(
        _LOOKUP_SUFFIX_RE.search(intent_text) and _catalog_subject(query_text)
    )
    purchase_question = bool(
        _PURCHASE_OR_CONSUMPTION_RE.search(intent_text)
        or _SAFETY_PERMISSION_RE.search(intent_text)
    )
    negated_lookup = bool(_NEGATED_LOOKUP_RE.search(intent_text))
    negated_order = bool(_NEGATED_ORDER_RE.search(intent_text))
    negated_purchase = bool(_NEGATED_PURCHASE_RE.search(intent_text))

    if _is_order_context_start(intent_text):
        _mark_order_context(group_id, sender_id)
        return None

    pending_order = _consume_pending_order_context(group_id, sender_id)

    if negated_lookup or (
        (negated_order or negated_purchase)
        and not (lookup_question or purchase_question)
    ):
        return None

    has_lookup_intent = bool(
        not negated_lookup
        and (lookup_request or lookup_question or lookup_suffix or purchase_question)
    )
    has_order_intent = bool(
        order_subject
        or (
            _RESTAURANT_INTENT_RE.search(intent_text)
            and not negated_order
        )
    )
    direct_catalog_subject = ""
    if not has_lookup_intent and not has_order_intent and not pending_order:
        direct_catalog_subject = _direct_catalog_subject(intent_text)
        snapshot, _, _ = _cache_snapshot()
        if (
            direct_catalog_subject
            and snapshot is not None
            and _dataset_contains_exact_masked_merchant(
                snapshot, direct_catalog_subject
            )
        ):
            direct_catalog_subject = ""
        if not direct_catalog_subject:
            exact_candidate = _catalog_subject(intent_text, max_length=40)
            if (
                exact_candidate
                and snapshot is not None
                and _dataset_contains_exact_direct_subject(
                    snapshot, exact_candidate
                )
            ):
                direct_catalog_subject = exact_candidate

    pending_subject = _pending_order_subject(intent_text) if pending_order else ""
    explicit_food_safety = bool(
        re.search(r"食安|食品安全|致癌油", intent_text)
    )
    if order_subject and _is_nonrestaurant_order_target(order_subject):
        return None
    if (
        has_order_intent
        and not order_subject
        and not has_lookup_intent
        and not pending_subject
    ):
        return None
    generic_safety_question = bool(
        re.search(r"是否安全|安全嗎|安全[?？]", intent_text)
    )
    if (
        generic_safety_question
        and not explicit_food_safety
        and not _FOOD_SUBJECT_HINT_RE.search(query_text)
    ):
        return None
    if (
        order_subject
        and _FOOD_SUBJECT_HINT_RE.search(order_subject)
        and not has_lookup_intent
        and not pending_subject
    ):
        return None
    if (
        purchase_question
        and not explicit_food_safety
        and not _FOOD_SUBJECT_HINT_RE.search(query_text)
    ):
        return None
    cold_explicit_order_target = bool(
        order_subject and _is_explicit_restaurant_order_target(order_subject)
    )
    order_review_subject = order_subject or pending_subject
    if order_review_subject and not has_lookup_intent:
        order_snapshot, _, _ = _cache_snapshot()
        recognized_order_target = _is_recognized_order_merchant_subject(
            order_review_subject, order_snapshot
        )
        if not recognized_order_target and not cold_explicit_order_target:
            return None
    if (
        not has_order_intent
        and not has_lookup_intent
        and not pending_subject
        and not direct_catalog_subject
    ):
        return None
    if has_lookup_intent and not query_text:
        return _format_missing_query()

    direct_only = bool(
        direct_catalog_subject
        and not has_lookup_intent
        and not has_order_intent
        and not pending_subject
    )
    known_direct_subject = False
    ambiguous_direct_code = bool(
        direct_only and _is_ambiguous_bare_code(direct_catalog_subject)
    )
    if ambiguous_direct_code:
        snapshot, snapshot_is_fresh, _ = _cache_snapshot()
        if snapshot_is_fresh and snapshot is not None:
            if not _dataset_contains_direct_merchant(
                snapshot, direct_catalog_subject
            ):
                return None
            dataset = snapshot
        elif snapshot is not None and _dataset_contains_direct_merchant(
            snapshot, direct_catalog_subject
        ):
            known_direct_subject = True
            dataset = _load_dataset()
        else:
            return None
    elif direct_only and not _should_sync_direct_lookup(direct_catalog_subject):
        snapshot, snapshot_is_fresh, _ = _cache_snapshot()
        if snapshot_is_fresh:
            dataset = snapshot
        elif snapshot is not None and _dataset_contains_direct_subject(
            snapshot, direct_catalog_subject
        ):
            known_direct_subject = True
            dataset = _load_dataset()
        elif normalize_text(direct_catalog_subject) in _STARTUP_BARE_RESTAURANT_BRANDS:
            known_direct_subject = True
            dataset = _load_dataset()
        else:
            return None
    else:
        dataset = _load_dataset()
    if dataset is None:
        unavailable_snapshot, _, _ = _cache_snapshot()
        if has_lookup_intent or (
            direct_catalog_subject
            and _should_format_direct_no_match(direct_catalog_subject)
        ) or known_direct_subject or (
            direct_catalog_subject
            and _should_sync_direct_lookup(direct_catalog_subject)
        ) or cold_explicit_order_target or (
            order_review_subject
            and _is_recognized_order_merchant_subject(
                order_review_subject, unavailable_snapshot
            )
        ):
            return _format_dataset_unavailable()
        return None

    # Prefer a known official merchant entity from the original message. The
    # cleaned subject is only used as a fallback for an otherwise unknown name.
    known_merchants = _find_merchants(
        intent_text,
        dataset.get("merchant_aliases", []),
        dataset.get("flow_rows", []),
        allow_unknown_prefix=False,
    )
    if direct_catalog_subject:
        known_merchants = [
            merchant
            for merchant in known_merchants
            if (
                _is_high_confidence_bare_merchant(
                    merchant, direct_catalog_subject, dataset
                )
                or _has_merchant_product_relation(
                    direct_catalog_subject, merchant
                )
            )
        ]

    merchants = known_merchants
    merchant_subject = order_subject or pending_subject or query_text
    if not merchants and merchant_subject and not direct_catalog_subject:
        merchants = _find_merchants(
            merchant_subject,
            dataset.get("merchant_aliases", []),
            dataset.get("flow_rows", []),
            allow_unknown_prefix=bool(order_subject or pending_subject),
        )
    if not merchants and cold_explicit_order_target:
        merchants = [
            _MerchantMatch(
                canonical_name=order_subject,
                normalized_alias=normalize_text(order_subject),
                alias_kind="merchant",
            )
        ]

    explicit_related_merchants = [
        merchant
        for merchant in known_merchants
        if _has_merchant_product_relation(intent_text, merchant)
    ]
    if has_lookup_intent:
        product_query = query_text
    elif order_subject or pending_subject:
        product_query = order_subject or pending_subject
    elif direct_catalog_subject:
        product_query = direct_catalog_subject
    else:
        product_query = ""
    related_merchants = explicit_related_merchants
    if explicit_related_merchants and product_query:
        product_query = _product_subject_for_merchant(
            product_query, explicit_related_merchants[0]
        )
        product_rows = _product_rows(dataset, product_query)
    else:
        product_rows = _product_rows(dataset, product_query) if product_query else []
        if not product_rows and product_query:
            for merchant in known_merchants:
                implicit_product = _unseparated_product_subject(
                    product_query, merchant
                )
                if implicit_product:
                    implicit_rows = _product_rows(dataset, implicit_product)
                    if (
                        not implicit_rows
                        and (
                            not _FOOD_SUBJECT_HINT_RE.search(implicit_product)
                            or _PRODUCT_RELATION_DISCOURSE_RE.search(implicit_product)
                        )
                    ):
                        continue
                    related_merchants = [merchant]
                    product_query = implicit_product
                    product_rows = implicit_rows
                    break
    result_query = query_text if related_merchants else product_query
    catalog_product_rows = (
        _catalog_product_rows(dataset, product_query) if product_query else []
    )
    if related_merchants:
        merchant_rows = [
            row
            for merchant in related_merchants
            for row in _matching_rows(dataset, merchant)
        ]
        related_rows = [
            row
            for row in product_rows
            if row in merchant_rows
        ]
        merchant_catalog_rows = [
            row
            for merchant in related_merchants
            for row in _matching_catalog_rows(dataset, merchant)
        ]
        related_catalog_rows = [
            row
            for row in catalog_product_rows
            if row in merchant_catalog_rows
        ]
        if not related_rows and not related_catalog_rows:
            return _format_product_result(result_query, [], dataset)
        product_rows = related_rows
        catalog_product_rows = related_catalog_rows
    if product_rows:
        return _append_catalog_possible_result(
            _format_product_result(result_query, product_rows, dataset),
            result_query,
            catalog_product_rows,
        )
    if catalog_product_rows:
        return _format_catalog_possible_result(
            result_query, catalog_product_rows
        )
    if not merchants:
        food_safety_specific = bool(
            lookup_suffix or explicit_food_safety
        )
        food_like_subject = bool(_FOOD_SUBJECT_HINT_RE.search(query_text))
        if direct_catalog_subject and _should_format_direct_no_match(
            direct_catalog_subject
        ):
            return _format_product_result(direct_catalog_subject, [], dataset)
        if (
            has_lookup_intent
            and query_text
            and len(normalize_text(query_text)) >= 2
            and (food_safety_specific or food_like_subject)
        ):
            return _format_product_result(query_text, [], dataset)
        return None
    merchant = merchants[0]
    merchant_rows = _matching_rows(dataset, merchant)
    merchant_catalog_rows = _matching_catalog_rows(dataset, merchant)
    if merchant_rows:
        return _append_catalog_possible_result(
            _format_result(merchant, merchant_rows, dataset),
            merchant.canonical_name,
            merchant_catalog_rows,
        )
    if merchant_catalog_rows:
        return _format_catalog_possible_result(
            merchant.canonical_name, merchant_catalog_rows
        )
    return _format_result(merchant, [], dataset)
