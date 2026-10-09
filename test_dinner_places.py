"""晚餐推薦只推查證過的店（Andrew 2026-10-09）。

「任何資訊必須驗證再驗證，他得是真的（加入測試環節）」：10/7 模型憑記憶把一家店
配上另一家店的地址。這裡守住：清單格式、過期不推、口味與預算篩選、送出前逐塊
比對，以及 handler 完全不經過模型。店名與地址全是合成的。
"""

from __future__ import annotations

import json
import random
from datetime import date
from types import SimpleNamespace

import pytest

import dinner_places as dp

TODAY = date(2099, 3, 1)


def _place(name, address, cuisine, tags, *, verified_on="2099-02-01", price=None, price_max=None, sources=2, closed=None):
    raw = {
        "name": name,
        "address": address,
        "cuisine": cuisine,
        "tags": tags,
        "verified_on": verified_on,
        "sources": [f"https://example{i}.test/{name}" for i in range(sources)],
    }
    if price is not None:
        raw["price"] = price
    if price_max is not None:
        raw["price_max"] = price_max
    if closed is not None:
        raw["closed_weekdays"] = closed
    return raw


@pytest.fixture
def data(tmp_path, monkeypatch):
    places = [
        _place("甲麵館", "測試市中正區甲路一段1號", "牛肉麵", ["麵食", "台式"], price="每人約 NT$200–300", price_max=300),
        _place("乙食堂", "測試市中正區乙路2號", "日式定食", ["日式"], price="每人約 NT$400–500", price_max=500),
        _place("丙烤肉", "測試市中山區丙街3號1樓", "韓式", ["韓式"]),
        # the display text says 1,200; filtering only reads price_max
        _place("丁披薩", "測試市中正區丁路4巷5號", "義式", ["西式"], price="每人約 NT$800–1,200（2024 菜單）", price_max=1200),
        _place("戊河粉", "測試市中正區戊路6號", "越南", ["東南亞"], price="每人約 NT$250", price_max=250),
        # TODAY (2099-03-01) is a Sunday: weekday 6
        _place("壬餃子", "測試市中正區壬路10號", "水餃", ["麵食"], closed=[6]),
        _place("己老店", "測試市中正區己路7號", "台菜", ["台式"], verified_on="2098-01-01"),  # 過期
        _place("庚小館", "測試市中正區庚路8號", "川菜", ["中式"], sources=1),  # 來源不夠
    ]
    path = tmp_path / "dinner_places.json"
    path.write_text(json.dumps({"max_age_days": 120, "places": places}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(dp, "DATA_PATH", path)
    monkeypatch.setattr(dp, "_today", lambda: TODAY)
    return path


def _names(text: str) -> list[str]:
    return [line.removeprefix("🍽 ") for line in text.splitlines() if line.startswith("🍽 ")]


def test_only_complete_entries_with_two_sources_load(data):
    places, max_age = dp.load()
    assert max_age == 120
    assert "庚小館" not in {p.name for p in places}
    assert "己老店" in {p.name for p in places}  # loads, but is stale


def test_stale_entries_are_never_recommended(data):
    for seed in range(30):
        assert "己老店" not in _names(dp.recommend("今晚吃什麼", rng=random.Random(seed)))


def test_a_recommendation_lists_only_verified_blocks(data):
    text = dp.recommend("今晚吃什麼", rng=random.Random(1))
    assert text.startswith(dp.HEADER) and text.endswith(dp.FOOTER)
    assert 4 <= len(_names(text)) <= 5
    assert dp.verify_reply(text)
    assert "步行" not in text and "分鐘" not in text  # walking time was never verified


def test_places_closed_tonight_are_skipped(data):
    assert date(2099, 3, 1).weekday() == 6
    for seed in range(30):
        assert "壬餃子" not in _names(dp.recommend("今晚吃什麼", rng=random.Random(seed)))
    monday = date(2099, 3, 2)
    seen = {name for seed in range(40) for name in _names(dp.recommend("想吃水餃", today=monday, rng=random.Random(seed)))}
    assert "壬餃子" in seen


def test_not_wanting_a_cuisine_excludes_it(data):
    for seed in range(30):
        assert "乙食堂" not in _names(dp.recommend("今晚不想吃日式", rng=random.Random(seed)))
    assert dp._tag_mentions("想吃麵包") == (set(), set())


@pytest.mark.parametrize("asked", ["今晚吃什麼？要不要吃日式", "吃不吃日式", "想不想吃日式"])
def test_asking_about_a_cuisine_is_wanting_it(data, asked):
    assert dp._tag_mentions(asked) == ({"日式"}, set())
    assert _names(dp.recommend(asked, rng=random.Random(9))) == ["乙食堂"]


def test_a_budget_reads_price_max_not_the_display_text(data):
    for seed in range(20):
        assert "丁披薩" not in _names(dp.recommend("預算1000", rng=random.Random(seed)))


def test_cuisine_and_budget_filter(data):
    assert _names(dp.recommend("今晚吃什麼？想吃日式", rng=random.Random(2))) == ["乙食堂"]
    cheap = _names(dp.recommend("今晚吃什麼 便宜一點", rng=random.Random(3)))
    assert set(cheap) == {"戊河粉"}
    under_300 = _names(dp.recommend("今晚吃什麼 預算300", rng=random.Random(4)))
    assert set(under_300) == {"甲麵館", "戊河粉"}


def test_no_match_says_so_and_lists_others(data):
    text = dp.recommend("今晚吃什麼？想吃火鍋", rng=random.Random(5))
    assert "查證過的店裡沒有符合「火鍋」的" in text
    assert _names(text) and dp.verify_reply(text)


def test_no_fresh_places_means_no_place_details(tmp_path, monkeypatch):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"places": [_place("舊店", "測試路1號", "麵", [], verified_on="2000-01-01")]}), encoding="utf-8")
    monkeypatch.setattr(dp, "DATA_PATH", path)
    monkeypatch.setattr(dp, "_today", lambda: TODAY)
    assert dp.recommend("今晚吃什麼") == dp.NO_FRESH_TEXT


def test_unreadable_list_means_no_place_details(tmp_path, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(dp, "DATA_PATH", path)
    assert dp.recommend("今晚吃什麼") == dp.NO_FRESH_TEXT


@pytest.mark.parametrize(
    "text",
    [
        # 10/7 那種錯：清單裡的店配上別家店的地址
        "🍽 丁披薩\n📍 測試市中正區甲路一段1號",
        # 清單外的店
        "🍽 辛酒館\n📍 測試市中正區辛路9號",
        # 店名後面沒有地址
        "🍽 甲麵館\n🍴 牛肉麵",
        # 過期的店
        "🍽 己老店\n📍 測試市中正區己路7號",
    ],
)
def test_verify_reply_rejects_anything_not_on_the_list(data, text):
    assert not dp.verify_reply(text)


def test_verify_reply_accepts_text_without_place_blocks(data):
    assert dp.verify_reply(dp.NO_FRESH_TEXT)


def test_known_places_are_the_fresh_ones(data):
    places = dict(dp.known_places())
    assert places["甲麵館"] == "測試市中正區甲路一段1號"
    assert "己老店" not in places


def test_handler_never_calls_a_model_even_if_one_would_invent(data, monkeypatch):
    import main

    monkeypatch.setattr(
        main, "_llm_chat",
        lambda *_a, **_k: "🍽 某披薩\n📍 測試市中正區甲路一段1號\n🍴 拿坡里披薩",
    )
    sent = []
    monkeypatch.setattr(main, "_reply", lambda _tok, text, **_k: sent.append(text) or True)
    event = SimpleNamespace(reply_token="T", message=SimpleNamespace(text="今晚吃什麼？"))

    main._handle_dinner_recommendation(event, "G_TEST")

    assert len(sent) == 1
    assert "某披薩" not in sent[0]
    assert dp.verify_reply(sent[0])


def test_handler_sends_no_places_when_verification_fails(data, monkeypatch):
    import main

    monkeypatch.setattr(dp, "recommend", lambda *_a, **_k: "🍽 辛酒館\n📍 測試市中正區辛路9號")
    sent = []
    monkeypatch.setattr(main, "_reply", lambda _tok, text, **_k: sent.append(text) or True)
    event = SimpleNamespace(reply_token="T", message=SimpleNamespace(text="今晚吃什麼？"))

    main._handle_dinner_recommendation(event, "G_TEST")

    assert sent == [dp.NO_FRESH_TEXT]


def test_shipped_list_entries_are_complete():
    """正式清單：每一筆都有兩個以上不同的來源、查證日、地址，而且不是未來日期。"""
    raw = json.loads(dp.DATA_PATH.read_text(encoding="utf-8"))
    entries = raw["places"]
    assert entries, "dinner_places.json 不能是空的"
    names = [e["name"] for e in entries]
    assert len(names) == len(set(names))
    for entry in entries:
        place = dp._parse_place(entry)
        assert place is not None, entry.get("name")
        assert len(set(place.sources)) >= dp.MIN_SOURCES
        assert place.verified_on <= date.today()
        assert "\n" not in place.address


@pytest.mark.parametrize(
    "asked",
    ["不太想吃日式", "不想要吃日式", "別再吃日式了", "不要再吃日式", "日式以外都可以",
     "除了日式都好", "日式吃膩了", "日式不要"],
)
def test_ways_of_saying_no_to_a_cuisine(asked):
    assert dp._tag_mentions(asked) == (set(), {"日式"})


def test_a_budget_word_after_a_cuisine_is_not_a_no():
    assert dp._tag_mentions("想吃日式不要太貴") == ({"日式"}, set())


@pytest.mark.parametrize(
    ("asked", "limit"),
    [("每個人300", 300), ("人均300", 300), ("每人約300", 300), ("300左右", 300),
     ("預算一千", 1000), ("預算五百", 500), ("今晚吃什麼", None)],
)
def test_budget_phrasings(asked, limit):
    assert dp.budget_limit(asked) == limit


@pytest.mark.parametrize(
    "asked", ["晚上吃什麼？", "晚餐吃啥", "晚餐推薦一下", "善導寺附近有什麼好吃的"],
)
def test_more_dinner_questions_reach_the_verified_list(asked):
    import main

    assert main._is_dinner_question(asked)
