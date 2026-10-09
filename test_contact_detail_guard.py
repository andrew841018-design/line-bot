"""模型寫的地址、電話要有依據，否則整則不送（Andrew 2026-10-09）。

「任何資訊必須驗證再驗證，他得是真的」：依據只能是使用者給的內容、引用原文、預讀
素材、程式取得的搜尋結果或查證過的晚餐清單。內容全是合成的；晚餐清單那幾則
用 dinner_places.json 裡的公開店家。
"""

from __future__ import annotations

import dataclasses
import random
import time
from datetime import timedelta

import pytest

import dinner_places
import main
import reply_policy
import reply_provenance


@pytest.mark.parametrize(
    ("reply", "backing", "unbacked"),
    [
        ("📍 測試市中正區忠孝東路二段134巷6號", [], True),
        ("📍 中正區忠孝東路二段134巷6號", ["我在測試市中正區忠孝東路2段134巷6號"], False),
        ("地址：青島東路3-2號", ["約在青島東路3之2號門口"], False),
        ("林森南路2號1樓之4", [], True),
        ("電話 02-0000-0000", [], True),
        ("電話 (02)0000-0000", ["店家電話 02 0000 0000"], False),
        ("手機 0900-000-000", [], True),
        ("手機 +886 900 000 000", ["0900000000"], False),
        # not addresses or phone numbers
        ("12/31號 09:00 測試活動", [], False),
        ("搭到善導寺站3號出口", [], False),
        ("2026-10-22 12:00 買青汁", [], False),
        ("0050 會漲到 180，00878 也不錯", [], False),
        ("汶水老街有麻糬", [], False),
        ("忠孝東路後右轉3號", [], False),
        ("走路3號出口就到", [], False),
        ("網路1號店", [], False),
        # Chinese numerals compare by value (二十三 is 23, not 213)
        ("仁愛路三段五十五號", ["仁愛路3段55號"], False),
        ("忠孝東路二段一三四巷二十三號", ["忠孝東路2段134巷23號"], False),
        ("忠孝東路二段一三四巷二十三號", ["忠孝東路2段134巷213號"], True),
    ],
)
def test_unbacked_contact_details(reply, backing, unbacked):
    assert bool(reply_policy.unbacked_contact_details(reply, backing)) is unbacked


@pytest.mark.parametrize("n", [5000, 20000])
@pytest.mark.parametrize("unit", ["路", "一", "剛路", "0", "02-", "忠孝東路二段", "巷1號"])
def test_scan_is_linear(n, unit):
    text = (unit * n)[:n]
    start = time.perf_counter()
    reply_policy.unbacked_contact_details(text, [text])
    assert time.perf_counter() - start < 1.0


def _enforce(reply, **kwargs):
    kwargs.setdefault("source_text", "今晚吃什麼？")
    kwargs.setdefault("request_text", "今晚吃什麼？")
    outcome: dict = {}
    out = main._enforce_new_value_reply(reply, outcome=outcome, **kwargs)
    return out, outcome


@pytest.fixture(autouse=True)
def _no_judge(monkeypatch):
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    reply_provenance.reset()


def test_an_unaddressed_reply_with_an_invented_address_is_dropped():
    out, outcome = _enforce("推薦測試小館，地址是測試市中正區忠孝東路二段134巷6號。", addressed=False)
    assert out == ""
    assert outcome.get("contact_details_dropped") is True


def test_a_direct_question_gets_one_honest_line_instead():
    out, outcome = _enforce("測試小館在測試市中正區忠孝東路二段134巷6號。", addressed=True)
    assert out == main.UNVERIFIED_CONTACT_REPLY
    assert outcome.get("contact_details_dropped") is True


def test_a_reply_about_an_attached_file_keeps_its_address():
    reply_provenance.reset()
    user_input = ["這份通知的重點？", object()]  # a file part the guard cannot read as text
    out = main._guard_generated_reply("報到地點是測試路1號，記得帶健保卡。", user_input)
    assert out


def test_an_address_the_user_gave_is_kept():
    out, _ = _enforce(
        "那裡附近停車要先找路邊格，晚上六點後比較好停。",
        source_text="我們約在測試市中正區青島東路3之2號",
        request_text="我們約在測試市中正區青島東路3之2號，好停車嗎？",
    )
    assert out


def test_an_address_from_earlier_user_turns_is_backed():
    reply = "青島東路3-2號那間週一公休，記得換一天。"
    context = [("user", "成員甲：下週去青島東路3之2號吃飯")]
    out, _ = _enforce(reply, context=context, source_text="哪天去？", request_text="哪天去？")
    assert out


def test_a_generated_reply_with_an_invented_phone_is_dropped():
    reply_provenance.reset()
    out = main._guard_generated_reply("可以打 02-0000-0000 訂位。", "幫我訂位")
    assert out == ""
    assert reply_provenance.dropped()


def test_a_generated_reply_with_the_users_phone_is_kept():
    reply_provenance.reset()
    out = main._guard_generated_reply("撥 02-0000-0000 前先準備好人數。", "店家電話是 02-0000-0000")
    assert out
    assert not reply_provenance.dropped()


def test_a_verified_address_counts_only_beside_its_own_name():
    places = [("甲麵館", "測試市中正區甲路一段1號")]
    assert reply_policy.unbacked_contact_details("甲麵館\n📍 測試市中正區甲路一段1號", [], places) == []
    # the 2026-10-07 mistake: a made-up shop with a real neighbour's address
    assert reply_policy.unbacked_contact_details("某披薩屋，地址在測試市中正區甲路一段1號", [], places)


# 2026-10-10 review：清單地址要配「最近的已知店名」，附近有任一個對應店名不算數。
# 這三筆照 dinner_places.json（2026-10-09 查證版）抄寫，不讀檔，清單更新不影響。
_LISTED = [
    ("93蕃茄牛肉麵", "台北市中正區青島東路3之2號"),
    ("基隆麵食館", "台北市中正區青島東路7之3號"),
    ("巷貓 Alleycat's Pizza 華山店", "台北市中正區八德路一段1號（華山1914文創園區）"),
]


@pytest.mark.parametrize(
    "reply",
    [
        # 審查員實測（拿坡里披薩不在清單）：別家店那行配上 93蕃茄牛肉麵 的地址
        "1. 93蕃茄牛肉麵｜青島東路3之2號\n2. 拿坡里披薩｜青島東路3之2號",
        "推薦拿坡里披薩（青島東路3之2號），隔壁就是93蕃茄牛肉麵",
        # 同樣的錯配，兩家都在清單裡
        "1. 93蕃茄牛肉麵｜青島東路3之2號\n2. 基隆麵食館｜青島東路3之2號",
        "推薦基隆麵食館（青島東路3之2號），隔壁就是93蕃茄牛肉麵",
        "93蕃茄牛肉麵\n🍽 基隆麵食館\n📍 青島東路3之2號",
        # 括號裡的地址屬於括號前面那家
        "拿坡里披薩（青島東路3之2號）就在93蕃茄牛肉麵隔壁",
        # 新項目那行可以算，但不能越過它往上找
        "1. 93蕃茄牛肉麵\n2. 拿坡里披薩\n📍 青島東路3之2號",
        "🍽 93蕃茄牛肉麵\n- 拿坡里披薩 青島東路3之2號",
    ],
)
def test_a_listed_address_needs_its_own_name_nearest(reply):
    assert reply_policy.unbacked_contact_details(reply, [], _LISTED)


@pytest.mark.parametrize(
    "reply",
    [
        "93蕃茄牛肉麵（青島東路3之2號）",
        "青島東路3之2號的93蕃茄牛肉麵",
        "青島東路3之2號（善導寺站附近）的93蕃茄牛肉麵",
        "我們約在老地方（青島東路3之2號的93蕃茄牛肉麵）",
        "🍽 93蕃茄牛肉麵\n📍 青島東路3之2號",
        "🍽 93蕃茄牛肉麵\n📍 青島東路3之2號，隔壁是基隆麵食館",
        "1. 93蕃茄牛肉麵｜青島東路3之2號\n2. 基隆麵食館｜青島東路7之3號",
        "1. 93蕃茄牛肉麵\n地址：青島東路3之2號",
        "1️⃣ 93蕃茄牛肉麵\n🍜 番茄湯頭\n📍 青島東路3之2號",
        "**93蕃茄牛肉麵**\n**地址**：青島東路3之2號",
        "🍽\ufe0f 巷貓 Alleycat's Pizza 華山店\n📍 台北市中正區八德路一段1號（華山1914文創園區）",
    ],
)
def test_a_listed_address_beside_its_own_name_is_backed(reply):
    assert reply_policy.unbacked_contact_details(reply, [], _LISTED) == []


def test_backing_still_covers_a_listed_address_beside_another_name():
    reply = "1. 93蕃茄牛肉麵｜青島東路3之2號\n2. 拿坡里披薩｜青島東路3之2號"
    assert reply_policy.unbacked_contact_details(reply, ["拿坡里披薩在青島東路3之2號"], _LISTED) == []


def _list_days():
    """正式清單最新查證日起連續七天（每個星期都有，公休的店換一天會出現）。"""
    places, max_age = dinner_places.load()
    assert places, "dinner_places.json 讀不到"
    latest = max(p.verified_on for p in places)
    return [latest + timedelta(days=k) for k in range(min(7, max_age + 1))]


def test_every_place_the_list_can_recommend_passes_the_guard():
    places, max_age = dinner_places.load()
    shown: set[str] = set()
    expected: set[str] = set()
    for day in _list_days():
        text = dinner_places.recommend("今晚吃什麼？", today=day, rng=random.Random(day.toordinal()), count=len(places))
        assert reply_policy.unbacked_contact_details(text, [], dinner_places.known_places(today=day)) == [], text
        shown |= {line.removeprefix(dinner_places.NAME_MARK) for line in text.splitlines() if line.startswith(dinner_places.NAME_MARK)}
        expected |= {p.name for p in dinner_places.open_tonight(dinner_places.fresh(places, day, max_age), day)}
    assert expected and shown == expected


@pytest.mark.parametrize("asked", ["今晚吃什麼？", "想吃日式", "便宜一點", "預算300", "想吃火鍋"])
def test_recommendations_from_the_list_pass_the_guard(asked):
    for day in _list_days():
        places = dinner_places.known_places(today=day)
        for seed in range(5):
            text = dinner_places.recommend(asked, today=day, rng=random.Random(seed))
            assert dinner_places.ADDRESS_MARK in text
            assert reply_policy.unbacked_contact_details(text, [], places) == [], text


def test_a_listed_name_with_another_listed_address_is_unbacked():
    """10/7 的錯，用推薦本身的格式：清單每一家的店名都配上下一家的地址。"""
    day = _list_days()[0]
    places, max_age = dinner_places.load()
    fresh = dinner_places.fresh(places, day, max_age)
    known = dinner_places.known_places(today=day)
    for place, other in zip(fresh, fresh[1:] + fresh[:1]):
        if reply_policy._address_keys(place.address) == reply_policy._address_keys(other.address):
            continue
        text = dinner_places.render_block(dataclasses.replace(place, address=other.address))
        assert reply_policy.unbacked_contact_details(text, [], known), text


@pytest.mark.parametrize(
    "unit",
    ["93蕃茄牛肉麵", "青島東路3之2號", "93蕃茄牛肉麵青島東路3之2號", "青島東路3之2號，93蕃茄牛肉麵", "1. 青島東路3之2號\n"],
)
def test_scan_with_listed_places_is_linear(unit):
    text = (unit * 20000)[:20000]
    start = time.perf_counter()
    reply_policy.unbacked_contact_details(text, [], _LISTED)
    assert time.perf_counter() - start < 1.0


@pytest.mark.parametrize(
    "reply",
    ["📍 青島東路 3 之 2 號", "台北市中正區忠孝東路 2 段 33 號", "林森南路 61 巷 19 號", "中山一路5號", "撥 0800-000-000"],
)
def test_spaced_and_other_common_forms_are_still_checked(reply):
    assert reply_policy.unbacked_contact_details(reply, [])


def test_a_remembered_address_is_backing():
    reply_provenance.reset()
    facts = ["成員甲：阿嬤家地址：測試路5號"]
    out = main._guard_generated_reply("阿嬤家在測試路5號，搭公車最快。", "阿嬤家地址？", facts=facts)
    assert out


_TWO = [("93蕃茄牛肉麵", "台北市中正區青島東路3之2號"), ("基隆麵食館", "台北市中正區青島東路7之3號")]


@pytest.mark.parametrize(
    "reply",
    [
        "1. 基隆麵食館（93蕃茄牛肉麵隔壁）｜青島東路3之2號",
        "基隆麵食館就在93蕃茄牛肉麵旁邊，地址是青島東路3之2號",
        "🍽 基隆麵食館\n就在93蕃茄牛肉麵隔壁\n📍 青島東路3之2號",
        "**基隆麵食館**（93蕃茄牛肉麵對面）\n**地址**：青島東路3之2號",
        "📍 青島東路3之2號（93蕃茄牛肉麵樓上的基隆麵食館）",
        "93蕃茄牛肉麵、基隆麵食館：青島東路7之3號",
        "基隆麵食館隔壁的93蕃茄牛肉麵在青島東路7之3號",
        "93蕃茄牛肉麵\n基隆麵食館在青島東路7之3號，二店在青島東路3之2號",
        "推薦93蕃茄牛肉麵\n基隆麵食館：青島東路7之3號，分店：青島東路3之2號",
    ],
)
def test_every_listed_name_around_an_address_must_own_it(reply):
    # 2026-10-10 review（第三輪）：只看最近的一家會被「A 店（B 店隔壁）｜B 店地址」繞過。
    assert reply_policy.unbacked_contact_details(reply, [], _TWO)


@pytest.mark.parametrize(
    "reply",
    [
        "93蕃茄牛肉麵很好吃，地址是青島東路3之2號",
        "基隆麵食館很讚\n93蕃茄牛肉麵在青島東路3之2號",
        "推薦青島東路3之2號的93蕃茄牛肉麵，還有青島東路7之3號的基隆麵食館",
        "善導寺附近可以試試：\n1. 93蕃茄牛肉麵\n   - 地址：台北市中正區青島東路3之2號\n   - 特色：番茄湯頭\n"
        "2. 基隆麵食館\n   - 地址：台北市中正區青島東路7之3號",
        "1. **93蕃茄牛肉麵**\n   - 📍 台北市中正區青島東路3之2號",
        "🍽 93蕃茄牛肉麵\n- 地址：台北市中正區青島東路3之2號",
        "93蕃茄牛肉麵\n• 地址：台北市中正區青島東路3之2號",
        "• 93蕃茄牛肉麵\n  • 青島東路3之2號",
        "93蕃茄牛肉麵在青島東路3之2號，基隆麵食館在青島東路7之3號",
        "93蕃茄牛肉麵（青島東路3之2號）、基隆麵食館（青島東路7之3號）",
        "推薦93蕃茄牛肉麵，地址青島東路3之2號。基隆麵食館也不錯，地址青島東路7之3號。",
        "比起基隆麵食館，93蕃茄牛肉麵（青島東路3之2號）湯頭更濃",
    ],
)
def test_sub_bullets_and_two_shops_on_one_line_are_backed(reply):
    # 2026-10-10 review（第三輪）：上一版把「- 地址：…」當成新項目，正確的配對被擋。
    assert reply_policy.unbacked_contact_details(reply, [], _TWO) == []

