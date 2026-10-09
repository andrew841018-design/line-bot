"""模型寫的地址、電話要有依據，否則整則不送（Andrew 2026-10-09）。

「任何資訊必須驗證再驗證，他得是真的」：依據只能是使用者給的內容、引用原文、預讀
素材、程式取得的搜尋結果或查證過的晚餐清單。內容全是合成的。
"""

from __future__ import annotations

import time

import pytest

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


def test_a_mixed_burst_marks_the_member_without_a_name():
    import burst_filter

    pending = [("m1", "我下個月搬去台中", "U_A", 0.0), ("m2", "我在台北上班", "U_B", 0.0)]
    labels = {"U_A": "成員甲"}
    text = burst_filter._combine(pending, group_id="G", label_of=lambda _g, uid: labels.get(uid, ""))
    assert text == "成員甲：我下個月搬去台中\n（不確定是誰）：我在台北上班"
    # one unknown person alone needs no marker
    solo = burst_filter._combine(pending[1:], group_id="G", label_of=lambda _g, uid: "")
    assert solo == "我在台北上班"
