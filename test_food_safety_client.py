from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import threading
from unittest.mock import Mock

import pytest

import food_safety_client
import main


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.headers = {}
        self.closed = False

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload

    def iter_content(self, chunk_size):
        yield json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _allow_small_catalog_fixtures(monkeypatch, tmp_path):
    monkeypatch.setattr(food_safety_client, "_MIN_FLOW_ROWS", 0)
    monkeypatch.setattr(food_safety_client, "_MIN_MERCHANT_ALIASES", 0)
    monkeypatch.setattr(food_safety_client, "_MIN_RETAINED_RATIO", 0.0)
    monkeypatch.setattr(food_safety_client, "_MAX_DATASET_AGE_SECONDS", 10**12)
    monkeypatch.setattr(
        food_safety_client, "_MAX_OFFICIAL_DATA_AGE_SECONDS", 10**12
    )
    monkeypatch.setattr(food_safety_client, "_MAX_FUTURE_SKEW_SECONDS", 10**12)
    monkeypatch.setattr(
        food_safety_client,
        "_MANIFEST_PATH",
        tmp_path / "food-safety-manifest.json",
    )


def _dataset(*, rows=None):
    rows = list(rows or [])
    for index, row in enumerate(rows):
        row.setdefault(
            "stable_key",
            "fixture-"
            + food_safety_client.normalize_text(str(row.get("business_name") or index))
            + "-"
            + food_safety_client.normalize_text(str(row.get("food_name") or index)),
        )
        row.setdefault("source_url", "https://www.fda.gov.tw/official.pdf")
        row.setdefault(
            "normalized_food_name",
            food_safety_client.normalize_text(str(row.get("food_name") or "")),
        )
        row.setdefault("source_title", "官方測試資料")
        row.setdefault("published_at", "2026-07-14")
        row.setdefault("current_state", "active_alert")
    businesses = {
        food_safety_client.normalize_text(
            str(
                row.get("normalized_business_name")
                or row.get("business_name")
                or ""
            )
        )
        for row in rows
        if str(row.get("business_name") or "")
    }
    products = {
        food_safety_client.normalize_text(str(row.get("food_name") or ""))
        for row in rows
        if str(row.get("food_name") or "")
    }
    return {
        "schema_version": 1,
        "snapshot_id": "a" * 32,
        "generated_at": "2026-07-14T00:00:00+00:00",
        "official_data_updated_at": "2026-07-14T00:00:00+00:00",
        "official_source_syncs": {
            source_name: "2026-07-14T00:00:00+00:00"
            for source_name in food_safety_client._REQUIRED_OFFICIAL_SOURCE_SYNCS
        },
        "coverage": {
            "official_flow_records": len(rows),
            "official_businesses": len(businesses),
            "official_products": len(products),
        },
        "merchant_aliases": [
            {
                "normalized_alias": "鬍鬚張",
                "canonical_name": "鬍鬚張",
                "alias_kind": "alias",
            },
        ],
        "flow_rows": rows,
        "catalog_matches": [],
    }


def _assert_neutral_unavailable(reply):
    assert reply is not None
    assert "官方資料無法通過必要驗證" in reply
    assert "無法連線" not in reply


def test_restaurant_order_returns_official_warning(monkeypatch):
    row = {
        "business_name": "鬍鬚張-A007門市",
        "normalized_business_name": "鬍鬚張a007門市",
        "food_name": "黃金優選大豆沙拉油-18L",
        "batch": "A123",
        "expiry": "2027-01-01",
        "source_url": "https://www.fda.gov.tw/official.pdf",
    }
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset(rows=[row])),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("今晚訂鬍鬚張")

    assert reply is not None
    assert "🚨 官方食安資料命中" in reply
    assert "黃金優選大豆沙拉油-18L" in reply
    assert "商家：鬍鬚張-A007門市" in reply
    assert "官方警示：黃金優選大豆沙拉油-18L" in reply
    assert "有效／保存期限：2027-01-01" in reply
    assert "官方來源" in reply


@pytest.mark.parametrize(
    "text",
    [
        "訂餐鬍鬚張",
        "吃鬍鬚張",
        "去吃鬍鬚張",
        "預約鬍鬚張",
        "預訂鬍鬚張",
        "預定鬍鬚張",
    ],
)
def test_specific_restaurant_dining_commands_trigger_official_lookup(
    monkeypatch, text
):
    row = {
        "business_name": "鬍鬚張-A007門市",
        "normalized_business_name": "鬍鬚張a007門市",
        "food_name": "黃金優選大豆沙拉油-18L",
        "source_url": "https://www.fda.gov.tw/official.pdf",
    }
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset(rows=[row])),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message(text)

    assert reply is not None
    assert "商家：鬍鬚張-A007門市" in reply


@pytest.mark.parametrize(
    "text",
    [
        "訂餐麥當勞",
        "吃麥當勞",
        "去吃麥當勞",
        "預約麥當勞",
        "預訂麥當勞",
        "預定麥當勞",
    ],
)
def test_cold_cache_specific_restaurant_target_fetches_official_data(
    monkeypatch, text
):
    get = Mock(return_value=_Response(_dataset()))
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message(text)

    assert reply is not None
    assert "查無「麥當勞」" in reply
    get.assert_called_once()


@pytest.mark.parametrize("text", ["預約晶華飯店餐廳", "預訂山景民宿餐廳"])
def test_dining_suffix_overrides_lodging_order_exclusion(monkeypatch, text):
    get = Mock(return_value=_Response(_dataset()))
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message(text)

    assert reply is not None
    assert "查無" in reply
    get.assert_called_once()


def test_cold_cache_dining_chatter_and_nonrestaurant_targets_do_not_fetch(monkeypatch):
    get = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    variants = ["吃飯", "吃很飽", "吃蛋糕", "訂餐廳", "預定機票"]

    assert [food_safety_client.check_restaurant_message(text) for text in variants] == [
        None
    ] * len(variants)
    get.assert_not_called()


def test_split_order_messages_use_group_context(monkeypatch):
    row = {
        "business_name": "鬍鬚張-A007門市",
        "normalized_business_name": "鬍鬚張a007門市",
        "food_name": "黃金優選大豆沙拉油-18L",
        "expiry": "2027-01-01",
        "source_url": "https://www.fda.gov.tw/official.pdf",
    }
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset(rows=[row])),
    )
    food_safety_client.reset_cache()

    assert (
        food_safety_client.check_restaurant_message(
            "我先來訂", group_id="GROUP", sender_id="USER-A"
        )
        is None
    )
    reply = food_safety_client.check_restaurant_message(
        "鬍鬚張", group_id="GROUP", sender_id="USER-A"
    )

    assert reply is not None
    assert "黃金優選大豆沙拉油-18L" in reply


def test_order_context_is_sender_scoped_and_single_use(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    assert (
        food_safety_client.check_restaurant_message(
            "我先來訂", group_id="GROUP", sender_id="USER-A"
        )
        is None
    )
    # Another sender's direct lookup cannot consume USER-A's context.
    other_reply = food_safety_client.check_restaurant_message(
        "全新餐館", group_id="GROUP", sender_id="USER-B"
    )
    assert "查無「全新餐館」" in other_reply
    assert ("GROUP", "USER-A") in food_safety_client._pending_order_groups
    reply = food_safety_client.check_restaurant_message(
        "全新餐館", group_id="GROUP", sender_id="USER-A"
    )

    assert reply is not None
    assert "查無「全新餐館」" in reply
    assert ("GROUP", "USER-A") not in food_safety_client._pending_order_groups


def test_order_context_is_disabled_without_sender_id(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    assert food_safety_client.check_restaurant_message("我先來訂", group_id="GROUP") is None
    assert not food_safety_client._pending_order_groups
    reply = food_safety_client.check_restaurant_message("全新餐館", group_id="GROUP")
    assert "查無「全新餐館」" in reply


def test_pending_order_rejects_long_unrelated_message_and_then_expires(monkeypatch):
    get = Mock(return_value=_Response(_dataset()))
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    assert (
        food_safety_client.check_restaurant_message(
            "我先來訂", group_id="GROUP", sender_id="USER-A"
        )
        is None
    )
    get.assert_not_called()
    assert (
        food_safety_client.check_restaurant_message(
            "這只是很長的政策轉傳內容，不是店名，也沒有要查食安",
            group_id="GROUP",
            sender_id="USER-A",
        )
        is None
    )
    reply = food_safety_client.check_restaurant_message(
        "全新餐館", group_id="GROUP", sender_id="USER-A"
    )
    assert "查無「全新餐館」" in reply
    assert get.call_count == 1


def test_official_branch_name_recovers_unseeded_brand_prefix(monkeypatch):
    row = {
        "business_name": "忠青商行-巨城新竹",
        "normalized_business_name": "忠青商行巨城新竹",
        "food_name": "油炸專用油-18L",
        "expiry": "20260731",
        "source_url": "https://www.fda.gov.tw/official.pdf",
    }
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(
            dict(_dataset(rows=[row]), merchant_aliases=[])
        ),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("今晚訂忠青商行")

    assert reply is not None
    assert "忠青商行" in reply
    assert "油炸專用油-18L" in reply


def test_bare_restaurant_name_is_an_explicit_lookup(monkeypatch):
    row = {
        "business_name": "忠青商行-巨城新竹",
        "normalized_business_name": "忠青商行巨城新竹",
        "food_name": "油炸專用油-18L",
        "expiry": "20260731",
        "source_url": "https://www.fda.gov.tw/official.pdf",
    }
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(
            dict(_dataset(rows=[row]), merchant_aliases=[])
        ),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("忠青商行")

    assert reply is not None
    assert "油炸專用油-18L" in reply


def test_long_official_product_names_are_directly_queryable(monkeypatch):
    rows = [
        {
            "business_name": "官方下游業者",
            "normalized_business_name": "官方下游業者",
            "food_name": "黃金優選大豆沙拉油-18L",
            "source_url": "https://www.fda.gov.tw/a.pdf",
        },
        {
            "business_name": "另一官方下游業者",
            "normalized_business_name": "另一官方下游業者",
            "food_name": "泰山吉多陽光種子調合油1L*12入",
            "source_url": "https://www.fda.gov.tw/b.pdf",
        },
        {
            "business_name": "第三官方下游業者",
            "normalized_business_name": "第三官方下游業者",
            "food_name": "公司行號-社會餐",
            "source_url": "https://www.fda.gov.tw/c.pdf",
        },
        {
            "business_name": "第四官方下游業者",
            "normalized_business_name": "第四官方下游業者",
            "food_name": "甜不辣",
            "source_url": "https://www.fda.gov.tw/d.pdf",
        },
        {
            "business_name": "第五官方下游業者",
            "normalized_business_name": "第五官方下游業者",
            "food_name": "炸甜不辣",
            "source_url": "https://www.fda.gov.tw/e.pdf",
        },
        {
            "business_name": "第六官方下游業者",
            "normalized_business_name": "第六官方下游業者",
            "food_name": "訂製品_甜麵醬含香油及乳酸20kg",
            "source_url": "https://www.fda.gov.tw/f.pdf",
        },
    ]
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset(rows=rows)),
    )
    food_safety_client.reset_cache()

    for product in (
        "黃金優選大豆沙拉油-18L",
        "泰山吉多陽光種子調合油1L*12入",
        "公司行號-社會餐",
        "甜不辣",
        "炸甜不辣",
        "訂製品_甜麵醬含香油及乳酸20kg",
    ):
        reply = food_safety_client.check_restaurant_message(product)
        assert reply is not None
        assert f"商品查詢：{product}" in reply


def test_ambiguous_bare_merchant_aliases_do_not_route(monkeypatch):
    payload = _dataset(
        rows=[
            {
                "business_name": "員林食品股份有限公司",
                "normalized_business_name": "員林食品股份有限公司",
                "food_name": "食用油",
            }
        ]
    )
    payload["merchant_aliases"] += [
        {"normalized_alias": "員林", "canonical_name": "員林食品"},
        {"normalized_alias": "零售", "canonical_name": "零售通路"},
    ]
    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._cached_dataset = payload
        food_safety_client._cached_at = food_safety_client.time.monotonic()

    assert food_safety_client.check_restaurant_message("員林") is None
    assert food_safety_client.check_restaurant_message("零售") is None


@pytest.mark.parametrize(
    "business_name",
    ["如是我聞食品有限公司", "正裕油行 倉庫(請先電話聯絡)"],
)
def test_exact_official_merchant_name_overrides_chat_word_filter(business_name):
    payload = _dataset(
        rows=[
            {
                "business_name": business_name,
                "normalized_business_name": food_safety_client.normalize_text(
                    business_name
                ),
                "food_name": "食用油",
            }
        ]
    )
    payload["merchant_aliases"].append(
        {
            "normalized_alias": food_safety_client.normalize_text(business_name),
            "canonical_name": business_name,
            "alias_kind": "merchant",
        }
    )
    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._cached_dataset = payload
        food_safety_client._cached_at = food_safety_client.time.monotonic()

    reply = food_safety_client.check_restaurant_message(business_name)

    assert reply is not None
    assert f"商家：{business_name}" in reply


def test_recurring_brand_alias_is_safe_for_bare_lookup():
    rows = [
        {
            "business_name": f"鬍鬚張-{branch}店",
            "normalized_business_name": f"鬍鬚張{branch}店",
            "food_name": "食用油",
        }
        for branch in ("A", "B", "C")
    ]
    payload = _dataset(rows=rows)
    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._cached_dataset = payload
        food_safety_client._cached_at = food_safety_client.time.monotonic()

    reply = food_safety_client.check_restaurant_message("鬍鬚張")

    assert reply is not None
    assert "共 3 筆官方流向／警示紀錄" in reply


def test_ascii_numeric_official_alias_is_safe_for_bare_lookup(monkeypatch):
    row = {
        "business_name": "統一超商測試門市",
        "normalized_business_name": "統一超商測試門市",
        "food_name": "食用油",
    }
    payload = _dataset(rows=[row])
    payload["merchant_aliases"] += [
        {
            "normalized_alias": alias,
            "canonical_name": "7-ELEVEN",
            "alias_kind": "alias",
        }
        for alias in ("7eleven", "711", "統一超商")
    ]
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(payload),
    )
    food_safety_client.reset_cache()

    for alias in ("7-11", "7-ELEVEN", "711"):
        reply = food_safety_client.check_restaurant_message(alias)
        assert reply is not None
        assert "統一超商測試門市" in reply

    product_reply = food_safety_client.check_restaurant_message(
        "7-11 食用油可以吃嗎"
    )
    assert product_reply is not None
    assert "統一超商測試門市" in product_reply
    assert "食用油" in product_reply


def test_masked_person_alias_does_not_route_but_short_merchants_do():
    rows = [
        {
            "business_name": name,
            "normalized_business_name": food_safety_client.normalize_text(name),
            "food_name": "食用油",
        }
        for name in ("劉O輝", "三億行", "上品行", "公道伯")
    ]
    payload = _dataset(rows=rows)
    payload["merchant_aliases"] += [
        {
            "normalized_alias": food_safety_client.normalize_text(name),
            "canonical_name": name,
        }
        for name in ("劉O輝", "三億行", "上品行", "公道伯")
    ]
    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._cached_dataset = payload
        food_safety_client._cached_at = food_safety_client.time.monotonic()

    assert food_safety_client.check_restaurant_message("劉O輝") is None
    for merchant in ("三億行", "上品行", "公道伯"):
        reply = food_safety_client.check_restaurant_message(merchant)
        assert reply is not None
        assert merchant in reply


def test_masked_person_business_with_food_word_does_not_bare_route():
    business_name = "佶達食品行(謝O恩)"
    payload = _dataset(
        rows=[
            {
                "business_name": business_name,
                "normalized_business_name": food_safety_client.normalize_text(
                    business_name
                ),
                "business_kind": "masked_person",
                "food_name": "沙拉油 18L",
            }
        ]
    )
    payload["merchant_aliases"].append(
        {
            "normalized_alias": food_safety_client.normalize_text(business_name),
            "canonical_name": business_name,
            "alias_kind": "masked_person",
        }
    )
    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._cached_dataset = payload
        food_safety_client._cached_at = food_safety_client.time.monotonic()

    assert food_safety_client.check_restaurant_message(business_name) is None


def test_dataset_validation_rejects_incomplete_or_truncated_payload():
    assert (
        food_safety_client._valid_dataset(
            {"merchant_aliases": [], "flow_rows": []}
        )
        is None
    )
    assert (
        food_safety_client._valid_dataset(
            {
                "generated_at": "2026-07-14T00:00:00+00:00",
                "coverage": {
                    "official_flow_records": 10,
                    "official_businesses": 8,
                    "official_products": 6,
                },
                "merchant_aliases": [
                    {"normalized_alias": "鬍鬚張", "canonical_name": "鬍鬚張"}
                ],
                "flow_rows": [
                    {
                        "business_name": "鬍鬚張",
                        "normalized_business_name": "鬍鬚張",
                        "food_name": "食用油",
                    }
                ],
            }
        )
        is None
    )


def test_dataset_validation_rejects_below_minimum_or_abrupt_drop(monkeypatch):
    row = {
        "business_name": "三億行",
        "normalized_business_name": "三億行",
        "food_name": "食用油",
    }
    current = _dataset(rows=[row])
    monkeypatch.setattr(food_safety_client, "_MIN_FLOW_ROWS", 2)
    monkeypatch.setattr(food_safety_client, "_MIN_MERCHANT_ALIASES", 2)

    assert food_safety_client._valid_dataset(current) is None

    monkeypatch.setattr(food_safety_client, "_MIN_FLOW_ROWS", 0)
    monkeypatch.setattr(food_safety_client, "_MIN_MERCHANT_ALIASES", 0)
    monkeypatch.setattr(food_safety_client, "_MIN_RETAINED_RATIO", 0.75)
    previous = _dataset(rows=[dict(row, business_name=f"三億行-{index}") for index in range(4)])

    assert food_safety_client._valid_dataset(current, previous) is None


def test_dataset_validation_rejects_same_count_key_replacement():
    previous = _dataset(
        rows=[
            {
                "business_name": f"原業者-{index}",
                "normalized_business_name": f"原業者{index}",
                "food_name": f"原商品-{index}",
            }
            for index in range(2)
        ]
    )
    current = _dataset(
        rows=[
            {
                "business_name": f"替換業者-{index}",
                "normalized_business_name": f"替換業者{index}",
                "food_name": f"替換商品-{index}",
            }
            for index in range(2)
        ]
    )

    assert food_safety_client._valid_dataset(current, previous) is None


def test_dataset_validation_rejects_bad_rows_older_data_and_alias_drop(
    monkeypatch,
):
    corrupt = _dataset(rows=[{}])
    corrupt["coverage"] = {
        "official_flow_records": 1,
        "official_businesses": 1,
        "official_products": 1,
    }
    assert food_safety_client._valid_dataset(corrupt) is None

    current = _dataset(
        rows=[
            {
                "business_name": "三億行",
                "normalized_business_name": "三億行",
                "food_name": "食用油",
            }
        ]
    )
    previous = _dataset(rows=current["flow_rows"])
    previous["generated_at"] = "2026-07-15T00:00:00+00:00"
    assert food_safety_client._valid_dataset(current, previous) is None

    previous["generated_at"] = current["generated_at"]
    previous["merchant_aliases"] = [
        {"normalized_alias": f"店家{index}", "canonical_name": f"店家{index}"}
        for index in range(4)
    ]
    monkeypatch.setattr(food_safety_client, "_MIN_RETAINED_RATIO", 0.75)
    assert food_safety_client._valid_dataset(current, previous) is None


def test_dataset_validation_rejects_stale_or_future_generated_at(monkeypatch):
    monkeypatch.setattr(food_safety_client, "_MAX_DATASET_AGE_SECONDS", 86400)
    monkeypatch.setattr(food_safety_client, "_MAX_FUTURE_SKEW_SECONDS", 600)
    now = datetime.now(timezone.utc)
    stale = _dataset()
    stale["generated_at"] = (now - timedelta(days=2)).isoformat()
    future = _dataset()
    future["generated_at"] = (now + timedelta(hours=1)).isoformat()

    assert food_safety_client._valid_dataset(stale) is None
    assert food_safety_client._valid_dataset(future) is None


def test_production_freshness_default_is_48_hours():
    assert food_safety_client._DEFAULT_MAX_FRESHNESS_SECONDS == 48 * 3600


@pytest.mark.parametrize("timestamp_kind", ["generated", "official", "source"])
@pytest.mark.parametrize("extra_seconds, accepted", [(0, True), (1, False)])
def test_dataset_freshness_48_hour_boundaries(
    monkeypatch, timestamp_kind, extra_seconds, accepted
):
    monkeypatch.setattr(food_safety_client, "_MAX_DATASET_AGE_SECONDS", 48 * 3600)
    monkeypatch.setattr(
        food_safety_client, "_MAX_OFFICIAL_DATA_AGE_SECONDS", 48 * 3600
    )
    now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    current_timestamp = now.isoformat()
    boundary_timestamp = (
        now - timedelta(hours=48, seconds=extra_seconds)
    ).isoformat()
    payload = _dataset()
    payload["generated_at"] = current_timestamp
    payload["official_data_updated_at"] = current_timestamp
    payload["official_source_syncs"] = {
        source_name: current_timestamp
        for source_name in food_safety_client._REQUIRED_OFFICIAL_SOURCE_SYNCS
    }

    if timestamp_kind == "generated":
        payload["generated_at"] = boundary_timestamp
    elif timestamp_kind == "official":
        payload["official_data_updated_at"] = boundary_timestamp
        payload["official_source_syncs"] = {
            source_name: boundary_timestamp
            for source_name in food_safety_client._REQUIRED_OFFICIAL_SOURCE_SYNCS
        }
    else:
        source_name = next(iter(food_safety_client._REQUIRED_OFFICIAL_SOURCE_SYNCS))
        payload["official_data_updated_at"] = boundary_timestamp
        payload["official_source_syncs"][source_name] = boundary_timestamp

    result = food_safety_client._valid_dataset(payload, now=now)

    assert (result is not None) is accepted


def test_dataset_validation_uses_official_sync_freshness_not_export_time(monkeypatch):
    monkeypatch.setattr(
        food_safety_client, "_MAX_OFFICIAL_DATA_AGE_SECONDS", 86400
    )
    now = datetime.now(timezone.utc)
    payload = _dataset()
    payload["generated_at"] = now.isoformat()
    payload["official_data_updated_at"] = (now - timedelta(days=2)).isoformat()

    assert food_safety_client._valid_dataset(payload) is None


def test_dataset_validation_requires_complete_official_source_set():
    payload = _dataset()
    payload["official_source_syncs"].pop("TFDA_DOWNSTREAM")

    assert food_safety_client._valid_dataset(payload) is None


def test_dataset_validation_rejects_injected_merchant_alias():
    payload = _dataset(
        rows=[
            {
                "business_name": "彰化泰山企業股份有限公司",
                "normalized_business_name": "彰化泰山企業股份有限公司",
                "food_name": "食用油",
            }
        ]
    )
    payload["merchant_aliases"].append(
        {
            "normalized_alias": "哥哥",
            "canonical_name": "彰化泰山企業股份有限公司",
            "alias_kind": "merchant",
        }
    )

    assert food_safety_client._valid_dataset(payload) is None


def test_dataset_validation_requires_official_source_contract():
    row = {
        "business_name": "三億行",
        "normalized_business_name": "三億行",
        "food_name": "食用油",
    }
    payload = _dataset(rows=[row])
    payload["flow_rows"][0].pop("source_title")

    assert food_safety_client._valid_dataset(payload) is None


def test_dataset_validation_rejects_unbacked_catalog_possible_match():
    payload = _dataset(
        rows=[
            {
                "business_name": "三億行",
                "normalized_business_name": "三億行",
                "food_name": "食用油",
            }
        ]
    )
    payload["catalog_matches"] = [
        {
            "merchant_name": "7-ELEVEN",
            "normalized_merchant_name": "7eleven",
            "product_name": "不存在官方清單的飯糰",
            "normalized_product_name": "不存在官方清單的飯糰",
            "source_url": "https://www.7-11.com.tw/catalog",
            "official_source_url": "https://www.fda.gov.tw/official.pdf",
            "safety_status": "possible_match",
        }
    ]

    assert food_safety_client._valid_dataset(payload) is None


@pytest.mark.parametrize("tampering", ["display_name", "official_source"])
def test_dataset_validation_rejects_inconsistent_catalog_backing(tampering):
    payload = _dataset(
        rows=[
            {
                "business_name": "官方業者",
                "normalized_business_name": "官方業者",
                "food_name": "受影響飯糰",
            }
        ]
    )
    catalog = {
        "merchant_name": "7-ELEVEN",
        "normalized_merchant_name": "7eleven",
        "product_name": "受影響飯糰",
        "normalized_product_name": "受影響飯糰",
        "source_url": "https://www.7-11.com.tw/catalog",
        "official_source_url": "https://www.fda.gov.tw/official.pdf",
        "safety_status": "possible_match",
    }
    if tampering == "display_name":
        catalog["product_name"] = "完全不同牛奶"
    else:
        catalog["official_source_url"] = "https://www.fda.gov.tw/unrelated.pdf"
    payload["catalog_matches"] = [catalog]

    assert food_safety_client._valid_dataset(payload) is None


@pytest.mark.parametrize("mutation", ["flow", "alias", "catalog"])
def test_persisted_manifest_rejects_cold_start_removals(monkeypatch, mutation):
    previous = _dataset(
        rows=[
            {
                "business_name": "原業者",
                "normalized_business_name": "原業者",
                "food_name": "受影響飯糰",
            }
        ]
    )
    previous["merchant_aliases"].append(
        {
            "normalized_alias": "原業者",
            "canonical_name": "原業者",
            "alias_kind": "merchant",
        }
    )
    previous["catalog_matches"] = [
        {
            "merchant_name": "7-ELEVEN",
            "normalized_merchant_name": "7eleven",
            "product_name": "受影響飯糰",
            "normalized_product_name": "受影響飯糰",
            "source_url": "https://www.7-11.com.tw/catalog",
            "official_source_url": "https://www.fda.gov.tw/official.pdf",
            "safety_status": "possible_match",
        }
    ]
    current = json.loads(json.dumps(previous, ensure_ascii=False))
    current["snapshot_id"] = "b" * 32
    current["generated_at"] = "2026-07-14T01:00:00+00:00"
    if mutation == "flow":
        replacement = {
            "business_name": "替換業者",
            "normalized_business_name": "替換業者",
            "food_name": "受影響飯糰",
        }
        current["flow_rows"] = _dataset(rows=[replacement])["flow_rows"]
        current["coverage"]["official_businesses"] = 1
    elif mutation == "alias":
        current["merchant_aliases"] = current["merchant_aliases"][:-1]
    else:
        current["catalog_matches"] = []

    food_safety_client._write_accepted_manifest(previous)
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(current),
    )
    food_safety_client.reset_cache()

    assert food_safety_client._load_dataset() is None


@pytest.mark.parametrize(("retained", "accepted"), [(9, True), (8, False)])
def test_flow_retention_allows_only_ninety_percent_rolling_revision(
    monkeypatch, retained, accepted
):
    monkeypatch.setattr(food_safety_client, "_MIN_FLOW_RETAINED_RATIO", 0.90)
    previous = _dataset(
        rows=[
            {
                "business_name": f"官方業者-{index}",
                "normalized_business_name": f"官方業者{index}",
                "food_name": "受影響食品",
                "stable_key": f"flow-{index}",
            }
            for index in range(10)
        ]
    )
    current = json.loads(json.dumps(previous, ensure_ascii=False))
    current["snapshot_id"] = "b" * 32
    current["generated_at"] = "2026-07-14T01:00:00+00:00"
    current["flow_rows"] = current["flow_rows"][:retained]
    current["coverage"]["official_flow_records"] = retained
    current["coverage"]["official_businesses"] = retained

    assert (food_safety_client._valid_dataset(current, previous) is current) is accepted


def test_manifest_keeps_alias_and_catalog_retention_strict(monkeypatch):
    monkeypatch.setattr(food_safety_client, "_MIN_FLOW_RETAINED_RATIO", 0.90)
    monkeypatch.setattr(food_safety_client, "_MIN_ALIAS_RETAINED_RATIO", 0.99)
    previous = _dataset(
        rows=[
            {
                "business_name": f"官方業者-{index}",
                "normalized_business_name": f"官方業者{index}",
                "food_name": "受影響食品",
                "stable_key": f"flow-{index}",
            }
            for index in range(10)
        ]
    )
    previous["merchant_aliases"].append(
        {
            "normalized_alias": "官方業者0",
            "canonical_name": "官方業者0",
            "alias_kind": "merchant",
        }
    )
    current = json.loads(json.dumps(previous, ensure_ascii=False))
    current["snapshot_id"] = "b" * 32
    current["generated_at"] = "2026-07-14T01:00:00+00:00"
    current["flow_rows"] = current["flow_rows"][:9]
    current["coverage"]["official_flow_records"] = 9
    current["coverage"]["official_businesses"] = 9
    current["merchant_aliases"] = current["merchant_aliases"][:-1]

    food_safety_client._write_accepted_manifest(previous)

    manifest = food_safety_client._load_accepted_manifest()
    assert manifest is not None
    assert not food_safety_client._manifest_accepts_dataset(current, manifest)


def test_manifest_treats_rotating_tfda_attachment_query_as_same_source(monkeypatch):
    monkeypatch.setattr(food_safety_client, "_MIN_FLOW_RETAINED_RATIO", 1.0)
    previous = _dataset(
        rows=[
            {
                "business_name": "官方業者",
                "normalized_business_name": "官方業者",
                "food_name": "受影響食品",
                "stable_key": "same-flow",
                "source_url": "https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=old",
            }
        ]
    )
    current = json.loads(json.dumps(previous, ensure_ascii=False))
    current["snapshot_id"] = "b" * 32
    current["generated_at"] = "2026-07-14T01:00:00+00:00"
    current["flow_rows"][0]["source_url"] = (
        "https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=new&type=2"
    )

    assert food_safety_client._valid_dataset(current, previous) is current

    food_safety_client._write_accepted_manifest(previous)

    manifest = food_safety_client._load_accepted_manifest()
    assert manifest is not None
    assert food_safety_client._manifest_accepts_dataset(current, manifest)


def test_persisted_manifest_rejects_alias_kind_change(monkeypatch):
    previous = _dataset(
        rows=[
            {
                "business_name": "鬍鬚張",
                "normalized_business_name": "鬍鬚張",
                "food_name": "食用油",
            }
        ]
    )
    current = json.loads(json.dumps(previous, ensure_ascii=False))
    current["snapshot_id"] = "b" * 32
    current["generated_at"] = "2026-07-14T01:00:00+00:00"
    current["merchant_aliases"][0]["alias_kind"] = "merchant"
    assert food_safety_client._valid_dataset(current) is not None

    food_safety_client._write_accepted_manifest(previous)
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(current),
    )
    food_safety_client.reset_cache()

    assert food_safety_client._load_dataset() is None


def test_catalog_possible_matches_keep_line_and_website_store_results_aligned():
    payload = _dataset()
    payload["merchant_aliases"] += [
        {"normalized_alias": "7eleven", "canonical_name": "7-ELEVEN"},
        {"normalized_alias": "711", "canonical_name": "7-ELEVEN"},
        {"normalized_alias": "統一超商", "canonical_name": "7-ELEVEN"},
        {"normalized_alias": "小七", "canonical_name": "7-ELEVEN"},
    ]
    payload["catalog_matches"] = [
        {
            "merchant_name": "7-ELEVEN",
            "normalized_merchant_name": "7eleven",
            "product_name": "雙蔬鮪魚飯糰",
            "normalized_product_name": "雙蔬鮪魚飯糰",
            "source_url": "https://www.7-11.com.tw/freshfoods/1_Ricerolls/index.aspx",
            "official_source_url": "https://www.fda.gov.tw/official.pdf",
            "safety_status": "possible_match",
        }
    ]
    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._cached_dataset = payload
        food_safety_client._cached_at = food_safety_client.time.monotonic()

    for merchant in ("7-11", "711", "統一超商", "小七"):
        reply = food_safety_client.check_restaurant_message(merchant)
        assert reply is not None
        assert "官方受影響品名相符" in reply
        assert "雙蔬鮪魚飯糰" in reply
        assert "未逐店確認" in reply

    product_reply = food_safety_client.check_restaurant_message("雙蔬鮪魚飯糰")
    assert product_reply is not None
    assert "7-ELEVEN" in product_reply
    assert "possible_match" not in product_reply


def test_startup_brand_fallback_handles_cold_cache(monkeypatch):
    rows = [
        {
            "business_name": f"鬍鬚張-{branch}店",
            "normalized_business_name": f"鬍鬚張{branch}店",
            "food_name": "食用油",
        }
        for branch in ("A", "B", "C")
    ]
    get = Mock(return_value=_Response(_dataset(rows=rows)))
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("鬍鬚張")

    assert reply is not None
    assert "共 3 筆官方流向／警示紀錄" in reply
    get.assert_called_once()


def test_startup_brand_fallback_reports_recent_failure(monkeypatch):
    get = Mock(side_effect=AssertionError("failure backoff must suppress fetch"))
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._last_fetch_failure_at = food_safety_client.time.monotonic()

    reply = food_safety_client.check_restaurant_message("鬍鬚張")

    _assert_neutral_unavailable(reply)
    get.assert_not_called()


def test_startup_brand_without_warning_returns_official_no_match(monkeypatch):
    get = Mock(return_value=_Response(_dataset()))
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("客美多")

    assert reply is not None
    assert "查無「客美多」" in reply
    get.assert_called_once()


def test_direct_unmatched_food_or_store_returns_official_no_match(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    for subject in (
        "乳香世家牛奶",
        "水餃",
        "蒸餃",
        "便當",
        "全新餐館",
        "7/11鮪魚飯糰",
    ):
        reply = food_safety_client.check_restaurant_message(subject)
        assert reply is not None
        assert f"查無「{subject}」" in reply


def test_unknown_explicit_restaurant_returns_official_no_match(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("吃客美多")

    assert reply is not None
    assert "查無" in reply
    assert "「客美多」" in reply
    assert "「吃客美多」" not in reply
    assert "不代表保證安全" in reply


def test_long_apple_policy_statement_does_not_trigger_food_safety(monkeypatch):
    get = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()
    variants = [
        (
            "@哥哥 @TestMember 不要買美國蘋果，因為美國廠商要求把我們台灣的蘋果農藥標準"
            "放大六倍，從原來0.5ppm放大為3ppm。台灣已經接受，公告之後如果沒有什麼意見，"
            "之後的標準就是3ppm；但是美國他們自己吃的蘋果標準是0ppm。"
        ),
        (
            "哥哥testmember不要買美國蘋果因為美國廠商要求把我們台灣的蘋果農藥標準放大六倍"
            "從原來05ppm放大為3ppm台灣已經接受公告之後如果意見之後的標準就3ppn"
            "但美國他們自己的蘋果標準0ppm"
        ),
    ]

    assert [food_safety_client.check_restaurant_message(text) for text in variants] == [
        None,
        None,
    ]
    get.assert_not_called()


def test_possessive_eat_prose_does_not_trigger_food_safety(monkeypatch):
    get = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    variants = [
        "這是我自己吃的心得，分享給大家參考。",
        "這是小孩吃的東西，不是在訂餐廳。",
        "昨天吃的心得很長，今天只是整理政策內容。",
        "沒有要訂餐，只是在分享一篇新聞。",
    ]

    assert [food_safety_client.check_restaurant_message(text) for text in variants] == [
        None
    ] * len(variants)
    get.assert_not_called()


def test_food_safety_statement_without_lookup_request_does_not_route(monkeypatch):
    get = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()
    text = "這次致癌油事件波及很多店家，新聞正在討論食品安全與後續影響。"

    assert food_safety_client.check_restaurant_message(text) is None
    get.assert_not_called()


def test_long_explicit_food_safety_subject_still_routes(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()
    product = "超長品牌名稱冷藏即食香草雞胸肉家庭分享包第五代改良版"

    reply = food_safety_client.check_restaurant_message(f"幫我查{product}食安")

    assert reply is not None
    assert f"查無「{product}」" in reply


def test_long_subject_with_supported_safety_suffixes_still_routes(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()
    product = "超長品牌名稱冷藏即食香草雞胸肉家庭分享包第五代改良版"

    for suffix in ("食安狀況", "有無食安紀錄", "有沒有食安問題"):
        reply = food_safety_client.check_restaurant_message(product + suffix)
        assert reply is not None
        assert f"查無「{product}」" in reply


def test_negated_lookup_order_and_purchase_statements_do_not_route(monkeypatch):
    get = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()
    variants = [
        "不要查鬍鬚張食安",
        "不想吃鬍鬚張",
        "不要買美國蘋果",
        "不能買美國蘋果，因為這是轉傳提醒",
        "預訂美國蘋果農藥標準放寬六倍，這是新聞轉傳，不是食安查詢",
    ]

    assert [food_safety_client.check_restaurant_message(text) for text in variants] == [
        None
    ] * len(variants)
    get.assert_not_called()


def test_polite_negated_lookup_does_not_route(monkeypatch):
    get = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    for text in ("不用幫我查鬍鬚張食安", "不需要請你確認鬍鬚張食安"):
        assert food_safety_client.check_restaurant_message(text) is None

    get.assert_not_called()


def test_common_explicit_food_safety_problem_question_routes(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    for subject in ("鬍鬚張", "乳香世家牛奶"):
        reply = food_safety_client.check_restaurant_message(subject + "有食安問題嗎")
        assert reply is not None
        assert subject in reply


def test_negative_purchase_question_keeps_complete_product_name(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("美國蘋果不能買嗎")

    assert reply is not None
    assert "查無「美國蘋果」" in reply
    assert "美國蘋果不」" not in reply


def test_unrelated_chatter_with_known_food_names_does_not_route_or_fetch(monkeypatch):
    get = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    variants = [
        "我以前在鬍鬚張工作",
        "我今天買了乳香世家牛奶",
        "鬍鬚張股票怎麼看",
        "我覺得鬍鬚張很好吃",
        "今天好累",
        "明天幾點開會",
        "週末一起看電影",
        "週末去爬山",
        "會議改到三點",
        "請記得帶護照",
        "台積電漲停",
        "晚點再說",
        "到家了",
        "真的假的",
        "好冷喔",
        "天氣不錯",
        "雨下好大",
        "晚安囉",
        "吃飽了",
        "收到囉",
        "生日快樂",
        "最近很忙",
        "高鐵誤點",
        "這間水餃店很貴",
        "最近便當漲價",
        "公司團購牛奶",
        "剛喝完牛奶",
        "便當還沒到",
        "咖啡喝完了",
        "蘋果很甜",
        "牛奶很好喝",
        "水餃太鹹",
        "排骨便當超難吃",
        "蘋果很新鮮",
        "牛奶很新鮮",
        "水餃份量很少",
        "美食推薦",
        "我要吃飯",
        "排隊好久",
        "鬍鬚張服務有問題嗎",
    ]

    assert [food_safety_client.check_restaurant_message(text) for text in variants] == [
        None
    ] * len(variants)
    get.assert_not_called()


def test_food_policy_or_news_prose_does_not_route_or_fetch(monkeypatch):
    get = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    for text in (
        "美國蘋果農藥標準放寬六倍",
        "蘋果農藥殘留標準改成3ppm",
        "新聞說台灣提高蘋果農藥容許量",
        "美國蘋果驗出農藥",
        "蘋果抽驗超標",
        "牛奶檢出抗生素",
        "雞蛋驗出芬普尼",
        "便當查獲過期食材",
        "美國蘋果農藥爭議",
        "台灣蘋果農藥議題",
        "雞蛋芬普尼事件",
        "牛奶抗生素爭議",
        "致癌油事件後續",
    ):
        assert food_safety_client.check_restaurant_message(text) is None

    get.assert_not_called()


def test_non_restaurant_reservations_do_not_route_or_fetch(monkeypatch):
    get = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    for text in (
        "我要訂機票",
        "預定機票",
        "預訂住宿",
        "預約測試醫師乙",
        "預約市立醫院",
        "預訂民宿",
        "預訂飯店",
        "訂展覽票",
        "我想訂高鐵票",
        "幫我預約牙醫",
        "我要訂演唱會票",
        "幫我預約按摩",
        "我要訂計程車",
    ):
        assert food_safety_client.check_restaurant_message(text) is None

    get.assert_not_called()


def test_non_restaurant_product_orders_do_not_route_or_fetch(monkeypatch):
    get = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    for text in (
        "我要訂iPhone 17",
        "我要訂一台電腦",
        "幫我訂一束花",
    ):
        assert food_safety_client.check_restaurant_message(text) is None

    get.assert_not_called()


def test_bare_numeric_or_short_code_does_not_route_or_fetch(monkeypatch):
    get = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    for text in ("18", "A1"):
        assert food_safety_client.check_restaurant_message(text) is None

    get.assert_not_called()


def test_non_food_safety_questions_do_not_route_or_fetch(monkeypatch):
    get = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    for text in (
        "這台電腦安全嗎",
        "這個網站安全嗎",
        "台積電安全嗎",
        "手機能買嗎",
    ):
        assert food_safety_client.check_restaurant_message(text) is None

    get.assert_not_called()


def test_url_and_calendar_schedule_do_not_route_or_fetch(monkeypatch):
    get = Mock(side_effect=AssertionError("non-food text must not fetch dataset"))
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    for text in (
        "https://vt.tiktok.com/ZSXPy5qv2/",
        "https://example.com/article?id=1",
        "8/1下午 打羽球\n8/2晚上打壁球19:00-20:00",
        "8/1打羽球",
        "8/1晚上全家聚餐",
        "咪寶 8/1應該可以",
    ):
        assert food_safety_client.check_restaurant_message(text) is None

    get.assert_not_called()


def test_common_explicit_query_prefixes_are_removed(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    for text in (
        "查乳香世家牛奶食安",
        "請查乳香世家牛奶食安",
        "查查乳香世家牛奶食安",
        "請幫我查乳香世家牛奶食安",
        "麻煩查乳香世家牛奶食安",
        "麻煩幫我查乳香世家牛奶食安",
        "可以幫我查乳香世家牛奶食安",
        "請協助查乳香世家牛奶食安",
    ):
        reply = food_safety_client.check_restaurant_message(text)
        assert reply is not None
        assert "查無「乳香世家牛奶」" in reply


def test_pending_order_ignores_non_restaurant_followup_and_consumes_context(monkeypatch):
    get = Mock(return_value=_Response(_dataset()))
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    assert (
        food_safety_client.check_restaurant_message(
            "我先來訂", group_id="GROUP", sender_id="USER-A"
        )
        is None
    )
    assert (
        food_safety_client.check_restaurant_message(
            "等等", group_id="GROUP", sender_id="USER-A"
        )
        is None
    )
    get.assert_not_called()
    reply = food_safety_client.check_restaurant_message(
        "全新餐館", group_id="GROUP", sender_id="USER-A"
    )
    assert "查無「全新餐館」" in reply
    get.assert_called_once()


def test_non_food_questions_and_eating_statements_do_not_route(monkeypatch):
    get = Mock(return_value=_Response(_dataset()))
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    for text in ("這台電腦有問題嗎", "這雙鞋能買嗎", "吃蘋果"):
        assert food_safety_client.check_restaurant_message(text) is None


def test_compact_eat_order_still_checks_restaurant(monkeypatch):
    row = {
        "business_name": "鬍鬚張-A007門市",
        "normalized_business_name": "鬍鬚張a007門市",
        "food_name": "黃金優選大豆沙拉油-18L",
        "source_url": "https://www.fda.gov.tw/official.pdf",
    }
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset(rows=[row])),
    )
    food_safety_client.reset_cache()

    for text in ("吃鬍鬚張", "今晚吃鬍鬚張", "我們明天一起去吃鬍鬚張"):
        reply = food_safety_client.check_restaurant_message(text)
        assert reply is not None
        assert "黃金優選大豆沙拉油-18L" in reply


def test_compound_order_verbs_extract_only_the_restaurant(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    for text in ("訂位客美多", "訂餐客美多", "今晚訂客美多兩位"):
        reply = food_safety_client.check_restaurant_message(text)
        assert reply is not None
        assert "查無「客美多」" in reply


def test_brand_name_matches_official_corporate_business_name(monkeypatch):
    row = {
        "business_name": "晶華國際酒店股份有限公司",
        "normalized_business_name": "晶華國際酒店股份有限公司",
        "food_name": "員工餐廳(烹調)使用",
        "expiry": "20260702",
        "source_url": "https://www.chshb.gov.tw/official.pdf",
    }
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(
            dict(_dataset(rows=[row]), merchant_aliases=[])
        ),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("晶華酒店有受到致癌油影響嗎")

    assert reply is not None
    assert "員工餐廳(烹調)使用" in reply
    assert "官方來源" in reply


def test_product_query_strips_bot_address_and_permission_words(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message(
        "米堡，乳香世家牛奶的食安狀況否可以買"
    )

    assert reply is not None
    assert "乳香世家牛奶" in reply
    assert "米堡乳香世家牛奶的食安狀況否可以買" not in reply


def test_product_query_strips_request_prefix_and_trailing_safety_label(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("幫我確認乳香世家牛奶食安")

    assert reply is not None
    assert "乳香世家牛奶" in reply
    assert "幫我確認乳香世家牛奶食安" not in reply


def test_product_query_strips_leading_line_mentions(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message(
        "@哥哥 @TestMember 幫我查乳香世家牛奶食安"
    )

    assert reply is not None
    assert "查無「乳香世家牛奶」" in reply
    assert "哥哥" not in reply
    assert "TestMember" not in reply


def test_product_query_strips_comma_separated_line_mentions(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message(
        "@哥哥，@TestMember 幫我查乳香世家牛奶食安"
    )

    assert reply is not None
    assert "查無「乳香世家牛奶」" in reply
    assert "哥哥" not in reply
    assert "TestMember" not in reply


def test_explicit_lookup_allows_trailing_whitespace(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("乳香世家牛奶食安   ")

    assert reply is not None
    assert "查無「乳香世家牛奶」" in reply


def test_safety_lookup_without_store_or_product_asks_for_subject(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("我要你確認食安狀況")

    assert reply == "食安查詢需要店家或商品名稱，例如「乳香世家牛奶」或「鬍鬚張」。"


def test_product_query_strips_bot_address_without_delimiter(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message(
        "米堡乳香世家牛奶的食安狀況否可以買"
    )

    assert reply is not None
    assert "乳香世家牛奶" in reply
    assert "米堡乳香世家牛奶的食安狀況否可以買" not in reply


def test_product_query_strips_you_wu_safety_question_suffix(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("乳香世家牛奶有無食安問題")

    assert reply is not None
    assert "乳香世家牛奶」" in reply
    assert "乳香世家牛奶無" not in reply
    assert "乳香世家牛奶有無食安問題" not in reply


def test_subject_extraction_composes_common_request_and_question_wrappers():
    expected = "乳香世家牛奶"
    variants = [
        "食安查詢：乳香世家牛奶",
        "我要你確認乳香世家牛奶食安狀況",
        "幫我看一下乳香世家牛奶有沒有食安問題",
        "請問乳香世家牛奶是否安全",
        "我在店裡看到乳香世家牛奶，能不能買",
        "在店裡看到乳香世家牛奶，想問可不可以食用",
        "想查乳香世家牛奶受影響嗎",
        "米堡乳香世家牛奶的食安狀況否可以買",
        "乳香世家牛奶有無食安紀錄",
        "乳香世家牛奶的食品安全狀況如何",
    ]

    assert [food_safety_client._extract_safety_query(text) for text in variants] == [
        expected
    ] * len(variants)


def test_free_form_safety_queries_all_reach_official_lookup(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()
    variants = [
        "我要你確認乳香世家牛奶食安狀況",
        "幫我看一下乳香世家牛奶有沒有食安問題",
        "在店裡看到乳香世家牛奶，想問可不可以食用",
        "乳香世家牛奶有無食安紀錄",
        "米堡乳香世家牛奶的食安狀況否可以買",
    ]

    for text in variants:
        reply = food_safety_client.check_restaurant_message(text)
        assert reply is not None
        assert "「乳香世家牛奶」" in reply


def test_product_query_returns_affected_merchants(monkeypatch):
    row = {
        "business_name": "米香食品店",
        "normalized_business_name": "米香食品店",
        "food_name": "乳香世家鮮乳",
        "batch": "MILK-1",
        "expiry": "2026-08-01",
        "source_url": "https://www.fda.gov.tw/official.pdf",
    }
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset(rows=[row])),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("乳香世家鮮乳可以買嗎")

    assert "商品查詢：乳香世家鮮乳" in reply
    assert "相符官方業者：米香食品店" in reply
    assert "商家：米香食品店" in reply
    assert "MILK-1" in reply


def test_product_query_returns_every_affected_merchant(monkeypatch):
    rows = [
        {
            "business_name": "甲超商",
            "normalized_business_name": "甲超商",
            "food_name": "鮪魚飯糰",
            "batch": "A1",
            "source_url": "https://www.fda.gov.tw/a.pdf",
        },
        {
            "business_name": "乙超商",
            "normalized_business_name": "乙超商",
            "food_name": "鮪魚飯糰",
            "batch": "B2",
            "source_url": "https://www.fda.gov.tw/b.pdf",
        },
    ]
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset(rows=rows)),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("幫我查鮪魚飯糰食安")

    assert "商品查詢：鮪魚飯糰" in reply
    assert "相符官方業者：乙超商、甲超商" in reply
    assert "商家：甲超商" in reply
    assert "商家：乙超商" in reply


def test_purchase_question_without_explicit_food_safety_words_is_supported(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("乳香世家牛奶能買嗎")

    assert reply is not None
    assert "乳香世家牛奶" in reply


def test_merchant_and_product_query_stays_on_same_flow_rows(monkeypatch):
    rows = [
        {
            "business_name": "鬍鬚張-A007門市",
            "normalized_business_name": "鬍鬚張a007門市",
            "food_name": "排骨便當",
            "source_url": "https://www.fda.gov.tw/official.pdf",
        },
        {
            "business_name": "瓦城泰統股份有限公司",
            "normalized_business_name": "瓦城泰統股份有限公司",
            "food_name": "排骨便當",
            "source_url": "https://www.fda.gov.tw/other.pdf",
        },
    ]
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset(rows=rows)),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("鬍鬚張的排骨便當可以吃嗎")

    assert "商品查詢：鬍鬚張的排骨便當" in reply
    assert "商家：鬍鬚張-A007門市" in reply
    assert "商家：瓦城泰統股份有限公司" not in reply


def test_bare_merchant_and_product_query_stays_on_same_flow_rows(monkeypatch):
    rows = [
        {
            "business_name": "鬍鬚張-A007門市",
            "normalized_business_name": "鬍鬚張a007門市",
            "food_name": "黃金優選大豆沙拉油-18L",
        },
        {
            "business_name": "瓦城泰統股份有限公司",
            "normalized_business_name": "瓦城泰統股份有限公司",
            "food_name": "黃金優選大豆沙拉油-18L",
        },
    ]
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset(rows=rows)),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message(
        "鬍鬚張的黃金優選大豆沙拉油-18L"
    )

    assert reply is not None
    assert "商家：鬍鬚張-A007門市" in reply
    assert "商家：瓦城泰統股份有限公司" not in reply


def test_merchant_and_product_without_shared_flow_does_not_leak_other_merchants(monkeypatch):
    rows = [
        {
            "business_name": "鬍鬚張-A007門市",
            "normalized_business_name": "鬍鬚張a007門市",
            "food_name": "排骨便當",
            "source_url": "https://www.fda.gov.tw/a.pdf",
        },
        {
            "business_name": "瓦城泰統股份有限公司",
            "normalized_business_name": "瓦城泰統股份有限公司",
            "food_name": "綠咖哩飯",
            "source_url": "https://www.fda.gov.tw/b.pdf",
        },
    ]
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset(rows=rows)),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("鬍鬚張的綠咖哩飯可以吃嗎")

    assert "查無" in reply
    assert "鬍鬚張-A007門市" not in reply
    assert "瓦城泰統股份有限公司" not in reply


def test_product_brand_alias_does_not_force_merchant_intersection(monkeypatch):
    row = {
        "business_name": "米香食品店",
        "normalized_business_name": "米香食品店",
        "food_name": "乳香世家牛奶",
        "source_url": "https://www.fda.gov.tw/official.pdf",
    }
    payload = _dataset(rows=[row])
    payload["merchant_aliases"].append(
        {
            "normalized_alias": "乳香世家",
            "canonical_name": "乳香世家",
            "alias_kind": "alias",
        }
    )
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(payload),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("乳香世家牛奶食安")

    assert "商家：米香食品店" in reply
    assert "查無" not in reply


def test_spaced_product_brand_is_matched_before_merchant_intersection(monkeypatch):
    row = {
        "business_name": "米香食品店",
        "normalized_business_name": "米香食品店",
        "food_name": "乳香世家牛奶",
        "source_url": "https://www.fda.gov.tw/official.pdf",
    }
    payload = _dataset(rows=[row])
    payload["merchant_aliases"].append(
        {
            "normalized_alias": "乳香世家",
            "canonical_name": "乳香世家",
            "alias_kind": "alias",
        }
    )
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(payload),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("乳香世家 牛奶食安")

    assert reply is not None
    assert "商家：米香食品店" in reply
    assert "查無" not in reply


def test_branded_product_does_not_match_generic_official_name(monkeypatch):
    row = {
        "business_name": "其他食品店",
        "normalized_business_name": "其他食品店",
        "food_name": "牛奶",
        "source_url": "https://www.fda.gov.tw/official.pdf",
    }
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset(rows=[row])),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("乳香世家牛奶食安")

    assert reply is not None
    assert "查無「乳香世家牛奶」" in reply
    assert "其他食品店" not in reply


def test_space_separated_merchant_and_product_filters_other_merchants(monkeypatch):
    rows = [
        {
            "business_name": "鬍鬚張-A007門市",
            "normalized_business_name": "鬍鬚張a007門市",
            "food_name": "排骨便當",
            "source_url": "https://www.fda.gov.tw/hwx.pdf",
        },
        {
            "business_name": "瓦城泰統股份有限公司",
            "normalized_business_name": "瓦城泰統股份有限公司",
            "food_name": "排骨便當",
            "source_url": "https://www.fda.gov.tw/thai.pdf",
        },
    ]
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset(rows=rows)),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("鬍鬚張 排骨便當可以吃嗎")

    assert reply is not None
    assert "鬍鬚張-A007門市" in reply
    assert "瓦城泰統股份有限公司" not in reply


def test_unseparated_merchant_and_product_filters_other_merchants(monkeypatch):
    rows = [
        {
            "business_name": "鬍鬚張-A007門市",
            "normalized_business_name": "鬍鬚張a007門市",
            "food_name": "排骨便當",
            "source_url": "https://www.fda.gov.tw/hwx.pdf",
        },
        {
            "business_name": "鬍鬚張-B008門市",
            "normalized_business_name": "鬍鬚張b008門市",
            "food_name": "牛肉麵",
            "source_url": "https://www.fda.gov.tw/hwx.pdf",
        },
        {
            "business_name": "瓦城泰統股份有限公司",
            "normalized_business_name": "瓦城泰統股份有限公司",
            "food_name": "排骨便當",
            "source_url": "https://www.fda.gov.tw/thai.pdf",
        },
    ]
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset(rows=rows)),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("鬍鬚張排骨便當可以吃嗎")

    assert reply is not None
    assert "商家：鬍鬚張-A007門市" in reply
    assert "牛肉麵" not in reply
    assert "瓦城泰統股份有限公司" not in reply


def test_product_before_merchant_filters_other_store_warnings(monkeypatch):
    rows = [
        {
            "business_name": "鬍鬚張-A007門市",
            "normalized_business_name": "鬍鬚張a007門市",
            "food_name": "排骨便當",
            "source_url": "https://www.fda.gov.tw/hwx.pdf",
        },
        {
            "business_name": "鬍鬚張-B008門市",
            "normalized_business_name": "鬍鬚張b008門市",
            "food_name": "牛肉麵",
            "source_url": "https://www.fda.gov.tw/hwx.pdf",
        },
        {
            "business_name": "瓦城泰統股份有限公司",
            "normalized_business_name": "瓦城泰統股份有限公司",
            "food_name": "排骨便當",
            "source_url": "https://www.fda.gov.tw/thai.pdf",
        },
    ]
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset(rows=rows)),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("排骨便當 鬍鬚張可以吃嗎")

    assert reply is not None
    assert "商家：鬍鬚張-A007門市" in reply
    assert "牛肉麵" not in reply
    assert "瓦城泰統股份有限公司" not in reply


def test_merchant_and_unknown_product_does_not_expand_to_all_store_warnings(monkeypatch):
    row = {
        "business_name": "鬍鬚張-A007門市",
        "normalized_business_name": "鬍鬚張a007門市",
        "food_name": "黃金優選大豆沙拉油-18L",
        "source_url": "https://www.fda.gov.tw/official.pdf",
    }
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset(rows=[row])),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("鬍鬚張 排骨便當可以吃嗎")

    assert reply is not None
    assert "查無「鬍鬚張 排骨便當」" in reply
    assert "黃金優選大豆沙拉油-18L" not in reply


def test_merchant_result_preserves_distinct_branches(monkeypatch):
    rows = [
        {
            "business_name": "鬍鬚張-A店",
            "normalized_business_name": "鬍鬚張a店",
            "food_name": "排骨便當",
            "batch": "B1",
            "expiry": "2026-08-01",
        },
        {
            "business_name": "鬍鬚張-B店",
            "normalized_business_name": "鬍鬚張b店",
            "food_name": "排骨便當",
            "batch": "B1",
            "expiry": "2026-08-01",
        },
    ]
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset(rows=rows)),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("鬍鬚張食安")

    assert "共 2 筆官方流向／警示紀錄" in reply
    assert "等 2 個官方業者紀錄" in reply
    assert "商家：鬍鬚張-A店" in reply
    assert "商家：鬍鬚張-B店" in reply


def test_specific_branch_does_not_fuzzy_match_other_legal_entities():
    target = "漢來美食股份有限公司台南分公司"
    other = "漢來美食股份有限公司台北南港分公司"
    rows = [
        {
            "business_name": target,
            "normalized_business_name": food_safety_client.normalize_text(target),
            "food_name": "台南分店警示品",
        },
        {
            "business_name": other,
            "normalized_business_name": food_safety_client.normalize_text(other),
            "food_name": "台北分店警示品",
        },
    ]
    payload = _dataset(rows=rows)
    payload["merchant_aliases"] += [
        {
            "normalized_alias": food_safety_client.normalize_text(name),
            "canonical_name": name,
            "alias_kind": "merchant",
        }
        for name in (target, other)
    ]
    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._cached_dataset = payload
        food_safety_client._cached_at = food_safety_client.time.monotonic()

    reply = food_safety_client.check_restaurant_message(target)

    assert reply is not None
    assert "台南分店警示品" in reply
    assert "台北分店警示品" not in reply
    assert f"商家：{target}" in reply
    assert f"商家：{other}" not in reply


def test_specific_branch_does_not_absorb_longer_sibling_branch():
    target = "貳樓-台南店"
    other = "貳樓-台南店二館"
    rows = [
        {
            "business_name": target,
            "normalized_business_name": food_safety_client.normalize_text(target),
            "food_name": "本店警示品",
        },
        {
            "business_name": other,
            "normalized_business_name": food_safety_client.normalize_text(other),
            "food_name": "二館警示品",
        },
    ]
    payload = _dataset(rows=rows)
    payload["merchant_aliases"] += [
        {
            "normalized_alias": food_safety_client.normalize_text(target),
            "canonical_name": target,
            "alias_kind": "merchant",
        }
    ]
    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._cached_dataset = payload
        food_safety_client._cached_at = food_safety_client.time.monotonic()

    reply = food_safety_client.check_restaurant_message(target)

    assert reply is not None
    assert "本店警示品" in reply
    assert "二館警示品" not in reply


def test_known_restaurant_without_match_returns_no_match(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("明天聚餐鬍鬚張")

    assert reply is not None
    assert "查無" in reply


def test_order_status_does_not_trigger_restaurant_lookup(monkeypatch):
    get = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    assert food_safety_client.check_restaurant_message("訂單已送出") is None
    get.assert_not_called()


def test_unknown_restaurant_keeps_existing_route(monkeypatch):
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    food_safety_client.reset_cache()

    assert food_safety_client.check_restaurant_message("今晚訂一間新餐廳") is None


def test_known_restaurant_order_reports_official_dataset_unavailable(monkeypatch):
    def fail(*args, **kwargs):
        raise food_safety_client.requests.RequestException("offline")

    monkeypatch.setattr(food_safety_client.requests, "get", fail)
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("今晚訂鬍鬚張")

    _assert_neutral_unavailable(reply)
    assert "目前不能完成食安核對" in reply


def test_cold_unknown_restaurant_order_reports_official_dataset_unavailable(
    monkeypatch,
):
    def fail(*args, **kwargs):
        raise food_safety_client.requests.RequestException("offline")

    monkeypatch.setattr(food_safety_client.requests, "get", fail)
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("預訂麥當勞")

    _assert_neutral_unavailable(reply)
    assert "目前不能完成食安核對" in reply


def test_unknown_restaurant_order_fails_closed_on_unexpected_lookup_error():
    assert food_safety_client.should_fail_closed_on_lookup_error("預訂麥當勞")
    assert not food_safety_client.should_fail_closed_on_lookup_error("預約市立醫院")


@pytest.mark.parametrize("failure_kind", ["network", "stale", "schema"])
def test_explicit_lookup_failures_use_neutral_unavailable_wording(
    monkeypatch, failure_kind
):
    if failure_kind == "network":
        get = Mock(side_effect=food_safety_client.requests.RequestException("offline"))
    else:
        payload = _dataset()
        if failure_kind == "stale":
            monkeypatch.setattr(
                food_safety_client, "_MAX_DATASET_AGE_SECONDS", 48 * 3600
            )
            payload["generated_at"] = (
                datetime.now(timezone.utc) - timedelta(hours=48, seconds=1)
            ).isoformat()
        else:
            payload["schema_version"] = 2
        get = Mock(return_value=_Response(payload))
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("幫我查鬍鬚張食安")

    _assert_neutral_unavailable(reply)
    assert "稍後再查" in reply


def test_bare_food_lookup_reports_official_dataset_unavailable(monkeypatch):
    def fail(*args, **kwargs):
        raise food_safety_client.requests.RequestException("offline")

    monkeypatch.setattr(food_safety_client.requests, "get", fail)
    food_safety_client.reset_cache()

    reply = food_safety_client.check_restaurant_message("乳香世家牛奶")

    _assert_neutral_unavailable(reply)


def test_transient_dns_failure_retries_once_then_recovers(monkeypatch):
    response = _Response(_dataset())
    get = Mock(
        side_effect=[
            food_safety_client.requests.ConnectionError("temporary DNS failure"),
            response,
        ]
    )
    sleep = Mock()
    write_manifest = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    monkeypatch.setattr(food_safety_client.time, "sleep", sleep)
    monkeypatch.setattr(
        food_safety_client, "_write_accepted_manifest", write_manifest
    )
    food_safety_client.reset_cache()

    dataset = food_safety_client._load_dataset()

    assert dataset is not None
    assert get.call_count == 2
    assert all(call.kwargs["stream"] is True for call in get.call_args_list)
    sleep.assert_called_once_with(food_safety_client._FETCH_RETRY_DELAY_SECONDS)
    write_manifest.assert_called_once_with(dataset)
    assert food_safety_client._last_fetch_failure_at == 0.0
    assert response.closed is True


def test_transient_connection_failures_exhaust_retry_budget(monkeypatch):
    get = Mock(
        side_effect=food_safety_client.requests.ConnectionError(
            "temporary connection failure"
        )
    )
    sleep = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    monkeypatch.setattr(food_safety_client.time, "sleep", sleep)
    food_safety_client.reset_cache()

    assert food_safety_client._load_dataset() is None
    assert get.call_count == food_safety_client._FETCH_MAX_ATTEMPTS
    assert sleep.call_count == food_safety_client._FETCH_MAX_ATTEMPTS - 1
    assert food_safety_client._dataset_refreshing is False
    assert food_safety_client._last_fetch_failure_at > 0.0

    assert food_safety_client._load_dataset() is None
    assert get.call_count == food_safety_client._FETCH_MAX_ATTEMPTS


@pytest.mark.parametrize("status_code", [404, 429, 503])
def test_http_error_is_not_retried(monkeypatch, status_code):
    response = _Response(_dataset())
    response.raise_for_status = Mock(
        side_effect=food_safety_client.requests.HTTPError(
            f"HTTP {status_code}",
            response=Mock(status_code=status_code),
        )
    )
    get = Mock(return_value=response)
    sleep = Mock()
    write_manifest = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    monkeypatch.setattr(food_safety_client.time, "sleep", sleep)
    monkeypatch.setattr(
        food_safety_client, "_write_accepted_manifest", write_manifest
    )
    food_safety_client.reset_cache()

    assert food_safety_client._load_dataset() is None
    get.assert_called_once()
    sleep.assert_not_called()
    write_manifest.assert_not_called()


def test_ssl_error_is_not_retried(monkeypatch):
    get = Mock(
        side_effect=food_safety_client.requests.exceptions.SSLError(
            "certificate verification failed"
        )
    )
    sleep = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    monkeypatch.setattr(food_safety_client.time, "sleep", sleep)
    food_safety_client.reset_cache()

    assert food_safety_client._load_dataset() is None
    get.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            food_safety_client.requests.exceptions.ReadTimeout("read timed out"),
            id="read-timeout",
        ),
        pytest.param(
            food_safety_client.requests.RequestException("other request failure"),
            id="generic-request-error",
        ),
    ],
)
def test_non_connection_request_error_is_not_retried(monkeypatch, error):
    get = Mock(side_effect=error)
    sleep = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    monkeypatch.setattr(food_safety_client.time, "sleep", sleep)
    food_safety_client.reset_cache()

    assert food_safety_client._load_dataset() is None
    get.assert_called_once()
    sleep.assert_not_called()


def test_retry_waiter_budget_covers_all_bounded_attempts():
    expected_operation_budget = (
        food_safety_client._FETCH_MAX_ATTEMPTS
        * food_safety_client._FETCH_TIMEOUT[0]
        + food_safety_client._FETCH_TIMEOUT[1]
        + (food_safety_client._FETCH_MAX_ATTEMPTS - 1)
        * food_safety_client._FETCH_RETRY_DELAY_SECONDS
    )

    assert (
        food_safety_client._FETCH_OPERATION_TIMEOUT_SECONDS
        == expected_operation_budget
    )
    assert food_safety_client._FETCH_OPERATION_TIMEOUT_SECONDS <= 15.0
    assert food_safety_client._FETCH_WAITER_TIMEOUT_SECONDS == (
        expected_operation_budget + food_safety_client._FETCH_WAITER_SLACK_SECONDS
    )
    assert food_safety_client._FETCH_WAITER_TIMEOUT_SECONDS <= 16.0


def test_fetch_operation_deadline_is_enforced(monkeypatch):
    release_fetch = threading.Event()

    def get(*args, **kwargs):
        assert release_fetch.wait(timeout=2)
        return _Response(_dataset())

    monkeypatch.setattr(food_safety_client.requests, "get", get)
    monkeypatch.setattr(food_safety_client, "_FETCH_OPERATION_TIMEOUT_SECONDS", 0.05)
    food_safety_client.reset_cache()

    started_at = food_safety_client.time.monotonic()
    assert food_safety_client._load_dataset() is None
    elapsed = food_safety_client.time.monotonic() - started_at

    assert elapsed < 0.5
    assert food_safety_client._dataset_refreshing is False
    assert food_safety_client._last_fetch_failure_at > 0.0
    with food_safety_client._fetch_worker_lock:
        worker = food_safety_client._fetch_worker
    assert worker is not None and worker.is_alive()
    release_fetch.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    with food_safety_client._fetch_worker_lock:
        assert food_safety_client._fetch_worker is None


def test_body_read_connection_error_is_not_retried(monkeypatch):
    response = _Response(_dataset())
    response.iter_content = Mock(
        side_effect=food_safety_client.requests.ConnectionError(
            "wrapped body read timeout"
        )
    )
    get = Mock(return_value=response)
    sleep = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    monkeypatch.setattr(food_safety_client.time, "sleep", sleep)
    food_safety_client.reset_cache()

    assert food_safety_client._load_dataset() is None
    get.assert_called_once()
    sleep.assert_not_called()
    assert response.closed is True


def test_invalid_json_is_not_retried(monkeypatch):
    response = _Response(_dataset())
    response.iter_content = Mock(return_value=iter([b"{malformed"]))
    get = Mock(return_value=response)
    sleep = Mock()
    write_manifest = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    monkeypatch.setattr(food_safety_client.time, "sleep", sleep)
    monkeypatch.setattr(
        food_safety_client, "_write_accepted_manifest", write_manifest
    )
    food_safety_client.reset_cache()

    assert food_safety_client._load_dataset() is None
    get.assert_called_once()
    sleep.assert_not_called()
    write_manifest.assert_not_called()


def test_timed_out_fetch_worker_is_not_duplicated_after_backoff(monkeypatch):
    release_fetch = threading.Event()
    get = Mock(
        side_effect=lambda *args, **kwargs: (
            release_fetch.wait(timeout=2) and _Response(_dataset())
        )
    )
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    monkeypatch.setattr(food_safety_client, "_FETCH_OPERATION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(food_safety_client, "_FETCH_FAILURE_BACKOFF_SECONDS", 0.0)
    food_safety_client.reset_cache()

    assert food_safety_client._load_dataset() is None
    assert food_safety_client._load_dataset() is None
    assert get.call_count == 1

    with food_safety_client._fetch_worker_lock:
        worker = food_safety_client._fetch_worker
    assert worker is not None and worker.is_alive()
    release_fetch.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert food_safety_client._cached_dataset is None


def test_oversized_dataset_response_fails_closed(monkeypatch):
    response = _Response(_dataset())
    response.headers = {
        "Content-Length": str(food_safety_client._FETCH_MAX_RESPONSE_BYTES + 1)
    }
    get = Mock(return_value=response)
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    assert food_safety_client._load_dataset() is None
    get.assert_called_once()
    assert response.closed is True


def test_concurrent_cold_cache_transient_recovery_uses_one_retry_sequence(
    monkeypatch,
):
    retry_started = threading.Event()
    release_retry = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    sleeps = []

    def get(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            raise food_safety_client.requests.ConnectionError(
                "temporary DNS failure"
            )
        return _Response(_dataset())

    def sleep(delay):
        sleeps.append(delay)
        retry_started.set()
        assert release_retry.wait(timeout=2)

    monkeypatch.setattr(food_safety_client.requests, "get", get)
    monkeypatch.setattr(food_safety_client.time, "sleep", sleep)
    food_safety_client.reset_cache()

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(food_safety_client._load_dataset) for _ in range(6)]
        assert retry_started.wait(timeout=1)
        release_retry.set()
        results = [future.result(timeout=3) for future in futures]

    assert calls == 2
    assert sleeps == [food_safety_client._FETCH_RETRY_DELAY_SECONDS]
    assert all(result is results[0] for result in results)
    assert results[0] is not None


def test_concurrent_cold_cache_uses_one_dataset_fetch(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def get(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        assert release.wait(timeout=2)
        return _Response(_dataset())

    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(food_safety_client._load_dataset) for _ in range(6)]
        assert started.wait(timeout=1)
        release.set()
        results = [future.result(timeout=2) for future in futures]

    assert calls == 1
    assert all(result is results[0] for result in results)


def test_expired_cache_is_not_used_when_refresh_fails(monkeypatch):
    old_dataset = _dataset()
    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._cached_dataset = old_dataset
        food_safety_client._cached_at = (
            food_safety_client.time.monotonic()
            - food_safety_client._CACHE_TTL_SECONDS
            - 1
        )

    def fail(*args, **kwargs):
        raise food_safety_client.requests.RequestException("offline")

    monkeypatch.setattr(food_safety_client.requests, "get", fail)

    assert food_safety_client._load_dataset() is None


def test_cache_within_ttl_is_rejected_after_absolute_freshness_boundary(monkeypatch):
    monkeypatch.setattr(food_safety_client, "_MAX_DATASET_AGE_SECONDS", 48 * 3600)
    monkeypatch.setattr(
        food_safety_client, "_MAX_OFFICIAL_DATA_AGE_SECONDS", 48 * 3600
    )
    initial_now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    boundary_timestamp = (initial_now - timedelta(hours=48)).isoformat()
    payload = _dataset()
    payload["generated_at"] = boundary_timestamp
    payload["official_data_updated_at"] = boundary_timestamp
    payload["official_source_syncs"] = {
        source_name: boundary_timestamp
        for source_name in food_safety_client._REQUIRED_OFFICIAL_SOURCE_SYNCS
    }
    assert food_safety_client._valid_dataset(payload, now=initial_now) is not None

    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._cached_dataset = payload
        food_safety_client._cached_at = food_safety_client.time.monotonic()
    monkeypatch.setattr(
        food_safety_client,
        "_utc_now",
        lambda: initial_now + timedelta(seconds=1),
    )
    get = Mock(side_effect=food_safety_client.requests.RequestException("offline"))
    monkeypatch.setattr(food_safety_client.requests, "get", get)

    reply = food_safety_client.check_restaurant_message("今晚訂鬍鬚張")

    _assert_neutral_unavailable(reply)
    get.assert_called_once()


def test_unexpected_fetch_exception_clears_single_flight_flag(monkeypatch):
    food_safety_client.reset_cache()
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        Mock(side_effect=RuntimeError("unexpected")),
    )

    assert food_safety_client._load_dataset() is None
    assert food_safety_client._dataset_refreshing is False

    food_safety_client.reset_cache()
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        lambda *args, **kwargs: _Response(_dataset()),
    )
    assert food_safety_client._load_dataset() is not None


def test_stale_cache_refreshes_known_direct_product(monkeypatch):
    row = {
        "business_name": "官方業者",
        "normalized_business_name": "官方業者",
        "food_name": "(丸大)一級棒A",
    }
    payload = _dataset(rows=[row])
    get = Mock(return_value=_Response(payload))
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._cached_dataset = payload
        food_safety_client._cached_at = (
            food_safety_client.time.monotonic()
            - food_safety_client._CACHE_TTL_SECONDS
            - 1
        )

    reply = food_safety_client.check_restaurant_message("(丸大)一級棒A")

    assert reply is not None
    assert "官方食安資料命中" in reply
    get.assert_called_once()


def test_stale_cache_refreshes_new_product_with_product_markers(monkeypatch):
    row = {
        "business_name": "官方業者",
        "normalized_business_name": "官方業者",
        "food_name": "(丸大)一級棒A",
    }
    refreshed_payload = _dataset(rows=[row])
    get = Mock(return_value=_Response(refreshed_payload))
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._cached_dataset = _dataset()
        food_safety_client._cached_at = (
            food_safety_client.time.monotonic()
            - food_safety_client._CACHE_TTL_SECONDS
            - 1
        )

    reply = food_safety_client.check_restaurant_message("(丸大)一級棒A")

    assert reply is not None
    assert "官方食安資料命中" in reply
    get.assert_called_once()


def test_stale_cache_does_not_refresh_unrelated_chatter(monkeypatch):
    get = Mock()
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._cached_dataset = _dataset()
        food_safety_client._cached_at = (
            food_safety_client.time.monotonic()
            - food_safety_client._CACHE_TTL_SECONDS
            - 1
        )

    assert food_safety_client.check_restaurant_message("生日快樂") is None
    get.assert_not_called()


def test_direct_lookup_waits_for_startup_cache_warm(monkeypatch):
    row = {
        "business_name": "官方業者",
        "normalized_business_name": "官方業者",
        "food_name": "(丸大)一級棒A",
    }
    payload = _dataset(rows=[row])
    monkeypatch.setattr(
        food_safety_client.requests,
        "get",
        Mock(side_effect=AssertionError("waiter must not start another fetch")),
    )
    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._dataset_refreshing = True
        food_safety_client._warm_cache_started = True

    def finish_warm():
        with food_safety_client._cache_condition:
            food_safety_client._cached_dataset = payload
            food_safety_client._cached_at = food_safety_client.time.monotonic()
            food_safety_client._dataset_refreshing = False
            food_safety_client._cache_condition.notify_all()

    timer = threading.Timer(0.05, finish_warm)
    timer.start()
    try:
        reply = food_safety_client.check_restaurant_message("(丸大)一級棒A")
    finally:
        timer.join(timeout=1)

    assert reply is not None
    assert "官方食安資料命中" in reply


def test_ordinary_chat_does_not_wait_for_startup_cache_warm(monkeypatch):
    get = Mock(side_effect=AssertionError("chat must not fetch"))
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._dataset_refreshing = True
        food_safety_client._warm_cache_started = True

    assert food_safety_client.check_restaurant_message("生日快樂") is None
    get.assert_not_called()

    with food_safety_client._cache_condition:
        food_safety_client._dataset_refreshing = False
        food_safety_client._cache_condition.notify_all()


def test_product_marked_direct_query_reports_recent_warm_failure(monkeypatch):
    get = Mock(side_effect=AssertionError("failure backoff must suppress fetch"))
    monkeypatch.setattr(food_safety_client.requests, "get", get)
    food_safety_client.reset_cache()
    with food_safety_client._cache_condition:
        food_safety_client._last_fetch_failure_at = food_safety_client.time.monotonic()

    for product in ("(丸大)一級棒A", "訂製品_甜麵醬含香油及乳酸20kg"):
        reply = food_safety_client.check_restaurant_message(product)
        _assert_neutral_unavailable(reply)
    get.assert_not_called()


def test_main_handler_replies_when_lookup_matches(monkeypatch):
    event = Mock()
    event.reply_token = "reply-token"
    event.source.user_id = "USER-A"
    captured = {}

    def check_message(text, *, group_id=None, sender_id=None):
        captured.update(text=text, group_id=group_id, sender_id=sender_id)
        return "食安結果"

    monkeypatch.setattr(
        main.food_safety_client,
        "check_restaurant_message",
        check_message,
    )
    reply = Mock()
    monkeypatch.setattr(main, "_reply", reply)

    assert main._handle_restaurant_food_safety(event, "GROUP", "今晚訂鬍鬚張") is True
    reply.assert_called_once_with(
        "reply-token",
        "食安結果",
        group_id="GROUP",
        allow_push_fallback=False,
    )
    assert captured == {
        "text": "今晚訂鬍鬚張",
        "group_id": "GROUP",
        "sender_id": "USER-A",
    }


def test_main_explicit_lookup_exception_fails_closed(monkeypatch):
    event = Mock()
    event.reply_token = "reply-token"
    event.source.user_id = "USER-A"
    event.message.text = "幫我查鬍鬚張食安"
    event.message.mention = None
    monkeypatch.setattr(
        main.food_safety_client,
        "check_restaurant_message",
        Mock(side_effect=RuntimeError("unexpected")),
    )
    reply = Mock()
    monkeypatch.setattr(main, "_reply", reply)

    handled = main._handle_restaurant_food_safety(
        event, "GROUP", event.message.text
    )

    assert handled is True
    reply.assert_called_once()
    _assert_neutral_unavailable(reply.call_args.args[1])
    assert reply.call_args.kwargs["allow_push_fallback"] is False


def test_main_bare_lookup_exception_fails_closed(monkeypatch):
    food_safety_client.reset_cache()
    event = Mock()
    event.reply_token = "reply-token"
    event.source.user_id = "USER-A"
    event.message.mention = None
    monkeypatch.setattr(
        main.food_safety_client,
        "check_restaurant_message",
        Mock(side_effect=RuntimeError("unexpected")),
    )
    reply = Mock()
    monkeypatch.setattr(main, "_reply", reply)

    for text in ("鬍鬚張", "乳香世家牛奶"):
        event.message.text = text
        assert main._handle_restaurant_food_safety(event, "GROUP", text) is True

    assert reply.call_count == 2
    for call in reply.call_args_list:
        _assert_neutral_unavailable(call.args[1])


def test_main_ordinary_chatter_exception_keeps_normal_routing(monkeypatch):
    food_safety_client.reset_cache()
    event = Mock()
    event.reply_token = "reply-token"
    event.source.user_id = "USER-A"
    event.message.mention = None
    monkeypatch.setattr(
        main.food_safety_client,
        "check_restaurant_message",
        Mock(side_effect=RuntimeError("unexpected")),
    )
    reply = Mock()
    monkeypatch.setattr(main, "_reply", reply)

    for text in (
        "生日快樂",
        "手機能買嗎",
        "這台電腦可以買嗎",
        "這雙鞋能不能買",
        "網站安全嗎",
    ):
        event.message.text = text
        assert main._handle_restaurant_food_safety(event, "GROUP", text) is False

    reply.assert_not_called()


def test_main_handler_uses_line_mention_ranges_for_spaced_names(monkeypatch):
    event = Mock()
    event.reply_token = "reply-token"
    event.source.user_id = "USER-A"
    event.message.text = "@Test Member 幫我查乳香世家牛奶食安"
    mentionee = Mock(index=0, length=len("@Test Member"))
    event.message.mention.mentionees = [mentionee]
    captured = {}

    def check_message(text, *, group_id=None, sender_id=None):
        captured.update(text=text, group_id=group_id, sender_id=sender_id)
        return "食安結果"

    monkeypatch.setattr(main.food_safety_client, "check_restaurant_message", check_message)
    monkeypatch.setattr(main, "_reply", Mock())

    assert main._handle_restaurant_food_safety(event, "GROUP", event.message.text) is True
    assert captured["text"] == " 幫我查乳香世家牛奶食安"


def test_line_mention_ranges_use_utf16_offsets():
    message = Mock()
    message.text = "😀 @Test Member 幫我查乳香世家牛奶食安"
    mentionee = Mock(index=3, length=len("@Test Member"))
    message.mention.mentionees = [mentionee]

    stripped = main._strip_mentions(message)

    assert stripped == "😀  幫我查乳香世家牛奶食安"
