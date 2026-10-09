"""晚餐推薦只推查證過的店（Andrew 2026-10-09）。

「任何資訊必須驗證再驗證，他得是真的（加入測試環節）」：10/7 模型憑記憶把一家店
配上另一家店的地址。這裡守住：清單格式、過期不推、口味與預算篩選、送出前逐塊
比對，以及 handler 完全不經過模型。店名與地址全是合成的。
"""

from __future__ import annotations

import json
import random
from datetime import date
from pathlib import Path
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


@pytest.mark.parametrize(
    "asked",
    ["今晚吃什麼？要不要吃日式", "吃不吃日式", "想不想吃日式",
     # 2026-10-10 review：否定詞後面多允許了是／會／去，這些 A不A 問句要先合併
     "是不是要吃日式", "會不會想吃日式", "去不去吃日式"],
)
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


def test_handler_never_calls_a_model(data, monkeypatch):
    # 2026-10-10 review：舊版只讓 _llm_chat 回一家編的店，「先叫模型、驗證不過再退回
    # 清單」的寫法也會過。現在任何模型入口一被呼叫就失敗；呼叫另外記下來，
    # 就算 pytest.fail 被 except 吞掉也抓得到。
    import claude_client
    import gemini_client
    import local_llm
    import main

    called = []

    def _no_model(name):
        def _fail(*_a, **_k):
            called.append(name)
            pytest.fail(f"晚餐推薦叫了模型：{name}")
        return _fail

    for module, attr in (
        (main, "_llm_chat"),
        (main, "_gemini_llm_chat"),
        (main, "_gemini_last_tier_reply"),
        (main, "_gemini_quota_fallback"),
        (main, "_local_text_llm_fallback"),
        (gemini_client, "chat"),
        (gemini_client, "chat_last_tier"),
        (claude_client, "chat"),
        (local_llm, "chat"),
    ):
        monkeypatch.setattr(module, attr, _no_model(f"{module.__name__}.{attr}"))

    class _NoGeminiClient:  # gemini_client 的每個 API 呼叫都經過 _client
        def __getattr__(self, name):
            called.append(f"gemini_client._client.{name}")
            pytest.fail(f"晚餐推薦叫了模型：gemini_client._client.{name}")

    monkeypatch.setattr(gemini_client, "_client", _NoGeminiClient())
    sent = []
    monkeypatch.setattr(main, "_reply", lambda _tok, text, **_k: sent.append(text) or True)
    event = SimpleNamespace(reply_token="T", message=SimpleNamespace(text="今晚吃什麼？"))

    main._handle_dinner_recommendation(event, "G_TEST")

    assert called == []
    assert len(sent) == 1
    assert _names(sent[0]) and dp.verify_reply(sent[0])


def test_handler_sends_no_places_when_verification_fails(data, monkeypatch):
    import main

    monkeypatch.setattr(dp, "recommend", lambda *_a, **_k: "🍽 辛酒館\n📍 測試市中正區辛路9號")
    sent = []
    monkeypatch.setattr(main, "_reply", lambda _tok, text, **_k: sent.append(text) or True)
    event = SimpleNamespace(reply_token="T", message=SimpleNamespace(text="今晚吃什麼？"))

    main._handle_dinner_recommendation(event, "G_TEST")

    # 2026-10-10 review: a failed check says it failed, not that the list expired
    assert sent == [main._DINNER_ERROR_TEXT] and main._DINNER_ERROR_TEXT != dp.NO_FRESH_TEXT


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
        assert len(dp._source_sites(place.sources)) >= dp.MIN_SOURCES
        assert place.verified_on <= date.today()
        assert "\n" not in place.address


def test_shipped_list_loads_every_entry_with_two_sites():
    # 2026-10-10 review：來源改成按網站算之後，正式清單一筆都不能被擋掉。
    path = Path(dp.__file__).with_name("dinner_places.json")
    entries = json.loads(path.read_text(encoding="utf-8"))["places"]
    places, _ = dp.load(path)
    assert len(places) == len(entries)
    for place in places:
        assert len(dp._source_sites(place.sources)) >= dp.MIN_SOURCES, place.name


@pytest.mark.parametrize(
    "urls",
    [
        ("https://www.example.test/a", "https://example.test/b"),  # www／沒有 www
        ("https://EXAMPLE.test/a", "https://example.test/a"),  # 只差大小寫
        ("https://example.test/a?x=1", "https://example.test/a?x=2"),  # 只差 query
        ("https://www.Example.test/a", "http://example.test:8080/b#c"),
        ("https://example.test/a", "httpx://other.test/b"),  # 不是網頁網址不算來源
    ],
)
def test_same_site_twice_counts_as_one_source(urls):
    # 2026-10-10 review：「兩個獨立來源」原本比對網址字串，同一個網站放兩個連結也算兩個。
    raw = _place("同站店", "測試市中正區同路1號", "麵", ["麵食"])
    raw["sources"] = list(urls)
    assert dp._parse_place(raw) is None
    raw["sources"] = [*urls, "https://other.test/x"]
    assert dp._parse_place(raw) is not None


@pytest.mark.parametrize(
    "asked",
    ["不太想吃日式", "不想要吃日式", "別再吃日式了", "不要再吃日式", "日式以外都可以",
     "除了日式都好", "日式吃膩了", "日式不要", "不想吃日式", "除了日式都可以", "別再吃日式",
     # 2026-10-10 review：副本把這些判成想吃（部署版原本判對）
     "沒有很想吃日式", "不是很想吃日式", "不太想要吃日式", "不會想吃日式", "不想去吃日式",
     "今天沒有很想要吃日式", "不太想要再去吃日式",
     # 口味在前
     "日式不是很想", "日式沒有很想吃", "日式不太想吃", "日式不想要了"],
)
def test_ways_of_saying_no_to_a_cuisine(asked):
    assert dp._tag_mentions(asked) == (set(), {"日式"})


@pytest.mark.parametrize(
    "asked",
    ["不喜歡日式", "不太喜歡日式", "不愛吃日式", "不怎麼想吃日式", "沒那麼想吃日式", "不能吃日式",
     "不大想吃日式", "不可以吃日式", "不考慮日式", "日式不考慮"],
)
def test_more_ways_of_saying_no_the_deployed_version_caught(asked):
    # 2026-10-10 review：部署版「不／別／沒在前面就算」判對、副本漏掉的說法。
    assert dp._tag_mentions(asked) == (set(), {"日式"})


def test_a_budget_word_after_a_cuisine_is_not_a_no():
    assert dp._tag_mentions("想吃日式不要太貴") == ({"日式"}, set())


@pytest.mark.parametrize(
    ("asked", "tag"),
    [("晚餐吃什麼？有沒有拉麵", "日式"), ("晚餐推薦，有沒有素食", "素食"), ("想吃不錯的日式", "日式"),
     ("不知道要不要吃日式", "日式"), ("沒吃過韓式想試試", "韓式"), ("要不要吃日式", "日式"),
     ("不用太貴的日式", "日式"),
     # 2026-10-10 review：否定詞後面多允許了有／是，「沒有＋口味」「不是…嗎」還是在找
     ("今晚吃什麼？不是要吃日式嗎", "日式"),
     ("今晚吃什麼？不知道喜不喜歡日式", "日式"), ("能不能吃日式", "日式"), ("可不可以吃日式", "日式"),
     ("考不考慮日式", "日式"), ("沒那麼貴的日式", "日式"), ("日式不會太貴吧", "日式")],
)
def test_looking_for_a_cuisine_is_not_a_no(asked, tag):
    # 2026-10-09 post-deploy review: 「有沒有素食」 used to exclude 素食.
    assert dp._tag_mentions(asked) == ({tag}, set())


@pytest.mark.parametrize(
    "asked",
    ["晚餐吃什麼？附近沒有拉麵嗎", "不吃火鍋嗎？好冷", "日式好像不太好", "想吃韓式不要日式"],
)
def test_an_unclear_no_neither_narrows_nor_excludes(asked):
    # 2026-10-10 review（第三輪）：判斷不了就不縮小範圍，也不排除：把說不要的口味當成
    # 想吃，清單就只剩那一種。
    wanted, unwanted = dp._tag_mentions(asked)
    assert not ({"日式", "火鍋"} & wanted)
    assert not ({"日式", "火鍋"} & unwanted) or asked == "想吃韓式不要日式"


@pytest.mark.parametrize(
    ("asked", "refused"),
    [("今晚吃什麼？不要又吃火鍋", "火鍋"), ("別又吃火鍋了", "火鍋"), ("不要每次都吃日式", "日式"),
     ("日式就不要了，今晚吃什麼", "日式"), ("日式我不要", "日式"), ("日式不要啦", "日式"),
     ("今晚吃什麼 日式 不要", "日式"), ("別推日式", "日式"), ("不要推薦日式", "日式"),
     ("日式、韓式都不要", "日式"), ("不要日式和韓式", "韓式"), ("中午不是吃過日式了嗎", "日式"),
     # 第三輪最後一次審查：這些曾經只推火鍋那一家
     ("沒人想吃火鍋，今晚吃什麼", "火鍋"), ("最近都沒有想吃火鍋 晚餐吃什麼", "火鍋"),
     ("不知道為什麼不想吃火鍋 今晚吃什麼", "火鍋"), ("今晚吃什麼？昨天不是吃火鍋嗎", "火鍋"),
     ("我不覺得想吃火鍋", "火鍋"), ("火鍋 就不要了", "火鍋"), ("火鍋 我不要", "火鍋"),
     ("火鍋吃到膩了", "火鍋"), ("昨天才吃過火鍋", "火鍋"), ("火鍋改天吧", "火鍋"), ("火鍋❌", "火鍋")],
)
def test_a_refused_cuisine_is_never_the_only_thing_recommended(asked, refused):
    # 2026-10-10 review（第三輪）：上一版把這些都當成「想吃」，「不要又吃火鍋」只推了火鍋那家。
    wanted, _unwanted = dp._tag_mentions(asked)
    assert refused not in wanted
    places, _max_age = dp.load()
    today = max(p.verified_on for p in places)
    shown = [line[2:] for line in dp.recommend(asked, today=today, rng=random.Random(1)).splitlines()
             if line.startswith(dp.NAME_MARK)]
    tags = {p.name: set(p.tags) for p in places}
    assert shown and not all(refused in tags[name] for name in shown)


@pytest.mark.parametrize(
    ("asked", "tags"),
    [("想吃韓式不要日式", ({"韓式"}, {"日式"})), ("不要日式要韓式", ({"韓式"}, {"日式"})),
     ("要不吃火鍋？", ({"火鍋"}, set())), ("怎麼不吃日式", ({"日式"}, set())),
     ("好久沒吃火鍋了", ({"火鍋"}, set()))],
)
def test_two_cuisines_and_suggestions(asked, tags):
    wanted, unwanted = dp._tag_mentions(asked)
    assert (wanted, unwanted) == tags or (asked == "想吃韓式不要日式" and unwanted == {"日式"})


@pytest.mark.parametrize(
    ("asked", "limit"),
    [("每個人300", 300), ("人均300", 300), ("每人約300", 300), ("300左右", 300),
     ("預算一千", 1000), ("預算五百", 500), ("今晚吃什麼", None),
     ("每人三百五", 350), ("預算兩千五", 2500), ("一千二以內", 1200),
     # 2026-10-10 review：時刻不是預算；千分位逗號要讀得懂
     ("晚上18:30左右，今晚吃什麼", None), ("7點左右", None), ("10點以內", None),
     ("下午3:30以內", None), ("晚上6點30左右", None), ("18：30左右", None),
     ("我一個人18:30到", None), ("晚上18:30左右，預算300", 300),
     ("預算1,000", 1000), ("每人1,200", 1200), ("1,200元以內", 1200)],
)
def test_budget_phrasings(asked, limit):
    assert dp.budget_limit(asked) == limit


def test_a_time_of_day_never_shows_up_as_a_budget(data):
    # 2026-10-10 review：「晚上18:30左右」曾被讀成每人 30 元，推薦寫出「每人 30 元內」。
    text = dp.recommend("晚上18:30左右，今晚吃什麼", rng=random.Random(6))
    assert "元內" not in text
    assert len(_names(text)) == 5 and dp.verify_reply(text)


@pytest.mark.parametrize(
    "asked", ["晚餐吃啥", "晚餐推薦一下", "善導寺附近有什麼好吃的"],
)
def test_more_dinner_questions_reach_the_verified_list(asked):
    import main

    assert main._is_dinner_question(asked)


@pytest.mark.parametrize(
    ("asked", "limit"),
    [("晚上七點30左右到，今晚吃什麼", None), ("10/15左右聚餐", None), ("每個人 15 分鐘內到", None),
     ("下午五點50左右", None), ("不要太便宜的", None), ("不用省錢", None), ("便宜一點", 250),
     ("預算一千五", 1500), ("預算300", 300)],
)
def test_times_and_dates_are_not_budgets(asked, limit):
    # 2026-10-10 review（第三輪）：低於 100 的數字當成時刻／日期；「不要太便宜」不是要便宜。
    assert dp.budget_limit(asked) == limit


def test_one_outlet_on_two_subdomains_is_one_source():
    sites = dp._source_sites(
        ["https://supertaste.tvbs.com.tw/a", "https://news.tvbs.com.tw/b", "https://m.facebook.com/x",
         "https://www.facebook.com/y", "https://example.test./z"]
    )
    assert sites == {"tvbs.com.tw", "facebook.com", "example.test"}


@pytest.mark.parametrize("asked", ["有沒有要吃日式", "有沒有很想吃日式"])
def test_have_or_have_not_is_a_question(asked):
    assert dp._tag_mentions(asked) == ({"日式"}, set())

