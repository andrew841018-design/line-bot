"""test_food_signals.py — 純規則抽取 7 種 relation + canonical + cancel + 餐廳黑名單 + 存 DB。"""
import pytest

import food_db as fdb
import food_signals as fs

G = "C_food_signals_test"


def _pairs(text):
    return {(s["kind"], s["food"]) for s in fs.extract(text)}


def test_wants_to_eat():
    assert ("wants_to_eat", "滷肉飯") in _pairs("好想吃滷肉飯")


def test_has_food():
    assert ("has_food", "蘿蔔糕") in _pairs("冰箱有蘿蔔糕")


def test_wants_bought():
    assert ("wants_bought", "蛋") in _pairs("要買蛋")


def test_bought():
    assert ("bought", "魚") in _pairs("今天買了魚")


def test_likes_food():
    assert ("likes_food", "虱目魚") in _pairs("我愛吃虱目魚")


def test_dislikes_food():
    assert ("dislikes_food", "魚") in _pairs("我不敢吃魚")


def test_finished_food():
    assert ("finished_food", "蛋") in _pairs("蛋吃完了")


def test_canonical_stored():
    # 雞肉 → 雞（GP1 C5e：存 canonical，否則對不上食譜的「雞」）
    assert ("bought", "雞") in _pairs("今天買了雞肉")


def test_cancel_cue_suppresses_wants_bought():
    assert ("wants_bought", "蛋") not in _pairs("本來要買蛋結果沒買")


def test_restaurant_question_not_extracted():
    # 含白名單 food（牛肉麵）但是餐廳問句 → 整句 skip
    assert _pairs("今晚吃牛肉麵還是鬍鬚張?") == set()


def test_no_food_returns_empty():
    assert fs.extract("今天天氣真好") == []


def test_extract_and_store_into_db():
    fdb.clear_group(G)
    n = fs.extract_and_store(G, "msg1", "冰箱有蛋,要買牛奶")
    assert n >= 2
    assert "蛋" in fdb.query_inventory(G)
    assert "牛奶" in fdb.query_shopping(G)
    fdb.clear_group(G)


def test_store_dedup_same_msg():
    fdb.clear_group(G)
    fs.extract_and_store(G, "msgX", "冰箱有蛋")
    n2 = fs.extract_and_store(G, "msgX", "冰箱有蛋")  # 同 msg 重送
    assert n2 == 0
    fdb.clear_group(G)


def test_bought_ov_word_order():
    # OV 語序「X買了」（食物在動詞前）現在抓得到 → 採購清單能清掉（2026-06-01 修）
    assert ("bought", "蛋") in _pairs("蛋買了")


def test_bought_ov_suffix_variants():
    # suffix-anchored bought 的其他到貨說法
    assert ("bought", "蛋") in _pairs("蛋買回來了")
    assert ("bought", "蛋") in _pairs("蛋買好了")
    assert ("bought", "蛋") in _pairs("蛋買到了")


def test_bought_suffix_not_triggered_by_question():
    # 問句／反問不是採購回報 → 不可清掉採購清單。confidence 在 DB 層被丟棄
    # （food_db.insert_signal 無 confidence 參數），故必須在抽取層擋，不能靠 confidence。
    assert ("bought", "蛋") not in _pairs("蛋買了嗎")
    assert ("bought", "蛋") not in _pairs("蛋買了沒")


def test_bought_suffix_not_triggered_by_price_complaint():
    # 「買好貴」是嫌貴不是買到 → 不可誤判 bought（故 _BOUGHT_SUFFIX「買好了」需要「了」）
    assert ("bought", "蛋") not in _pairs("蛋買好貴")


def test_prefix_bought_still_works():
    # VO 語序（動詞在前）原本就會抽，新增 suffix entry 不可回歸 prefix 路徑
    assert ("bought", "蛋") in _pairs("買了蛋")


def test_bought_not_suppressed_by_future_hint_in_sentence():
    # 回歸見證：句中有「以後」(future hint) 不可整句丟掉既有 bought。
    # 鎖死被否決的提案（在 extract() 對 question_modal 整句抑制 bought 會誤殺這句的「蛋」）。
    assert ("bought", "蛋") in _pairs("已經買了蛋，以後再買牛奶")


@pytest.mark.xfail(
    reason="多食物 OV「蛋和牛奶買了」只抓得到緊鄰動詞的最後一個（suffix anchored）；v2 再補",
    strict=False,
)
def test_bought_multi_food_ov_still_partial():
    # 顯性標記已知缺口：anchored suffix 只蓋緊鄰動詞的 food，前面的會漏（非隱形）
    assert ("bought", "蛋") in _pairs("蛋和牛奶買了")
