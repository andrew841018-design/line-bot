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


# ── 2026-10-07：更多講法、問句與煮東西不算、買到了也算家裡有 ───────────────────
# Andrew：「家裡有什麼」會一直留著一個月前說過的蛋，「蛋沒了」也拿不掉。


@pytest.mark.parametrize("text", ["蛋沒了", "冰箱裡的蛋沒了", "蛋沒有了", "蛋用光了"])
def test_ran_out_phrasings_finish_the_food(text):
    assert ("finished_food", "蛋") in _pairs(text)


def test_ran_out_then_asked_to_buy():
    # 「沒了」不是「沒買」：要買的那一筆不能被取消
    assert _pairs("牛奶沒了，要買牛奶") == {("finished_food", "牛奶"), ("wants_bought", "牛奶")}


@pytest.mark.parametrize("text", ["蛋不用買了", "不用買蛋了", "蛋先不要買", "別買蛋", "蛋就不買了"])
def test_no_longer_needed_phrasings(text):
    assert _pairs(text) == {("skip_buying", "蛋")}


@pytest.mark.parametrize("text", ["要不要買蛋", "買不買蛋", "不要買太多蛋", "不用買那麼多蛋"])
def test_asking_or_limiting_is_not_no_longer_needed(text):
    assert ("skip_buying", "蛋") not in _pairs(text)


def test_bought_food_is_also_at_home():
    assert _pairs("買了牛奶") == {("bought", "牛奶"), ("has_food", "牛奶")}
    assert _pairs("蛋買回來了") == {("bought", "蛋"), ("has_food", "蛋")}


def test_bought_non_food_is_not_at_home():
    assert _pairs("買了衛生紙") == {("bought", "衛生紙")}


@pytest.mark.parametrize(
    "text",
    ["家裡還有蘋果嗎", "冰箱有蛋?", "冰箱有蛋？", "冰箱有沒有蛋", "買了蛋嗎", "要買蛋嗎", "蛋沒了嗎？", "牛奶喝完了沒"],
)
def test_questions_change_nothing(text):
    assert _pairs(text) == set()


def test_only_the_question_clause_is_skipped():
    assert _pairs("冰箱有蛋嗎，要買牛奶") == {("wants_bought", "牛奶")}


@pytest.mark.parametrize("text", ["我晚上煮麵", "今天煮了番茄炒蛋", "昨天在餐廳吃烤鴨"])
def test_cooking_or_eating_out_is_not_having(text):
    assert not any(kind == "has_food" for kind, _food in _pairs(text))


@pytest.mark.parametrize("text", ["沒買到蛋", "沒有買到蛋", "還沒買回來蛋"])
def test_not_getting_it_is_not_bought(text):
    assert ("bought", "蛋") not in _pairs(text)


@pytest.mark.parametrize("text, food", [("記得買蛋", "蛋"), ("明天去全聯買牛奶", "牛奶"), ("順便買青菜", "青菜")])
def test_more_ways_to_ask_for_buying(text, food):
    assert ("wants_bought", food) in _pairs(text)


def test_buying_at_a_store_already_is_bought():
    assert ("bought", "牛奶") in _pairs("我昨天去全聯買了牛奶")


def test_each_food_takes_the_nearest_trigger():
    # 前一段的「買了」不再算到後面的牛奶身上
    assert _pairs("已經買了蛋，以後再買牛奶") == {
        ("bought", "蛋"), ("has_food", "蛋"), ("wants_bought", "牛奶"),
    }
    assert ("bought", "牛奶") not in _pairs("買了蛋還要買牛奶")
    assert ("wants_bought", "牛奶") in _pairs("買了蛋還要買牛奶")


def test_a_list_after_one_trigger_is_all_wanted():
    assert _pairs("要買蛋、牛奶、青菜") == {
        ("wants_bought", "蛋"), ("wants_bought", "牛奶"), ("wants_bought", "青菜"),
    }


def test_store_bought_lands_in_inventory_and_leaves_shopping():
    fdb.clear_group(G)
    fs.extract_and_store(G, "m1", "要買牛奶")
    assert fdb.query_shopping(G) == ["牛奶"]
    fs.extract_and_store(G, "m2", "牛奶買了")
    assert fdb.query_shopping(G) == []
    assert fdb.query_inventory(G) == ["牛奶"]
    fs.extract_and_store(G, "m3", "牛奶沒了")
    assert fdb.query_inventory(G) == []
    fdb.clear_group(G)


def test_store_no_longer_needed_leaves_shopping_without_reaching_home():
    fdb.clear_group(G)
    fs.extract_and_store(G, "m1", "要買蛋")
    fs.extract_and_store(G, "m2", "蛋不用買了")
    assert fdb.query_shopping(G) == []
    assert fdb.query_inventory(G) == []
    fdb.clear_group(G)
