"""hearsay.py（2026-10-16）：A 講到家人 B 的事記到 B 名下時，誰是 B、能不能收。

全是合成的名字；這裡不碰 DB、不呼叫模型。bigram 用每日 job 的同一個函式。
"""

from __future__ import annotations

import pytest

import hearsay as h
from jobs import daily_memory as dm

ROSTER = h.build_roster(
    ["爸爸", "媽媽", "成員甲", "成員乙丙"],
    {"乙丙": "成員乙丙", "妹妹": "成員乙丙", "甲甲": "成員甲", "不在名單": "陌生人"},
)


def judge(fact, about, speaker, text, roster=ROSTER):
    return h.judge(fact, about, speaker, text, roster, dm._bigrams)


# ── 標記 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("住在台中（成員甲說的）", ("住在台中", "成員甲")),
        ("住在台中(成員甲說的)", ("住在台中", "成員甲")),  # NFKC 之後的半形也認
        ("住在台中（成員甲說的） ", ("住在台中", "成員甲")),
        ("住在台中", ("住在台中", "")),
        ("（成員甲說的）", ("（成員甲說的）", "")),  # 沒有本體不算
        ("喜歡（貓）", ("喜歡（貓）", "")),
        ("聽說的（成員甲說的）事", ("聽說的（成員甲說的）事", "")),  # 不在結尾
    ],
)
def test_split_tag(body, expected):
    assert h.split_tag(body) == expected


def test_the_tag_and_the_tombstone_prefix():
    assert h.tag("成員甲") == "（成員甲說的）"
    assert h.split_tag("住在台中" + h.tag("成員甲")) == ("住在台中", "成員甲")
    # canonical() 拿掉所有空白，前綴裡的空白讓轉述 namespace 永遠撞不到真的本體
    assert " " in h.TOMBSTONE_PREFIX


# ── 範圍閘門（審查 GP1#4、GP2 S1 的語料）────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    ["住在台中", "在台北上班", "在科技公司工作", "喜歡爬山", "不吃牛肉", "愛喝咖啡", "養了一隻貓",
     "最近在學游泳", "在讀大學", "興趣是攝影", "最愛吃火鍋", "正在學日文"],
)
def test_long_term_facts_in_the_allowed_frames_pass(body):
    assert h.scope_reason(body) == ""


@pytest.mark.parametrize(
    ("body", "category"),
    [
        # 健康與身體（基底過濾擋不到，審查員實測）
        ("沒胃口", "health"), ("走路不穩", "health"), ("重聽", "health"), ("吃不下飯", "health"),
        ("老花眼", "health"), ("夜尿很多", "health"), ("行動不便", "health"), ("需要人照顧", "health"),
        ("常常一個人在家", "health"),
        # 評價、個性、壞習慣
        ("腰不好", "judgement"), ("記性變差", "judgement"), ("精神不好", "judgement"),
        ("牙齒不好", "judgement"), ("耳朵不好", "judgement"), ("愛喝酒", "judgement"),
        ("脾氣很差", "judgement"), ("愛賭博", "judgement"),
        # 時間點、一次性的事（Andrew 決定只記長期的事）
        ("每週三下午去市場", "time"), ("下週考試", "time"), ("下個月搬家", "time"),
        ("民國九十年入學", "time"), ("2020年搬到台中", "time"), ("讀三年級", "time"),
        ("準備換工作", "event"), ("剛搬到台中", "event"),
        # 代名詞：誰的事會在 B 名下消失
        ("他在台中工作", "pronoun"), ("對我很好", "pronoun"),
        # 第三者
        ("同學住台中", "third_party"), ("男朋友在台北上班", "third_party"),
        # 心情、感情、成績
        ("情緒起伏很大", "sensitive"), ("在談戀愛", "sensitive"), ("成績很好", "sensitive"),
        ("心情不好", "judgement"),  # 「不好」先被評價那一類擋下
    ],
)
def test_hearsay_outside_the_safe_categories_is_refused(body, category):
    assert h.scope_reason(body) == f"scope:{category}"


def test_a_fact_in_no_allowed_frame_is_refused():
    assert h.scope_reason("很會煮菜") == "scope:frame"
    assert h.scope_reason("討厭公司") == "scope:frame"  # 框架要在開頭，「公司」出現在哪裡都算是錯的
    assert h.scope_reason("喜歡吃其他口味") == ""  # 「其他」的「他」不是代名詞
    assert h.scope_reason("在學吉他") == ""  # 「吉他」的「他」也不是


@pytest.mark.parametrize(
    ("body", "category"),
    [("喜歡同性", "sensitive"), ("喜歡女生", "sensitive"), ("住在監獄", "sensitive"), ("住在療養院", "health"),
     ("三高", "health"), ("約砲", "sensitive"), ("被公司資遣", "sensitive"),
     ("一個人住", "health"), ("耳背", "health"), ("眼睛花", "health"), ("住在養老院", "health"),
     ("在學戒癮", "health"), ("喜歡男生", "sensitive"), ("喜歡去教會", "sensitive"), ("支持某黨", "sensitive"),
     ("喜歡喝高粱", "sensitive"), ("喜歡抽雪茄", "sensitive"), ("喜歡看A片", "sensitive"), ("愛去夜店", "sensitive"),
     ("住在護理之家", "health"), ("喜歡打麻將", "sensitive"), ("喜歡玩股票", "sensitive"), ("喜歡去教堂", "sensitive")],
)
def test_bodies_the_post_implementation_review_got_through_are_refused(body, category):
    assert h.scope_reason(body) == f"scope:{category}"


# ── 名單 ─────────────────────────────────────────────────────────────────────


def test_the_roster_maps_names_aliases_and_roles_and_drops_outsiders():
    assert ROSTER.terms["乙丙"] == ROSTER.terms["妹妹"] == ROSTER.terms["成員乙丙"] == "成員乙丙"
    assert "不在名單" not in ROSTER.terms  # 對到不在名單上的人：不收


def test_a_term_shared_by_two_people_belongs_to_nobody():
    roster = h.build_roster(["成員甲", "成員乙"], {"小寶": "成員甲", "小寶 ": "成員乙"})
    assert "小寶" not in roster.terms


# ── 對人與位置 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fact", "about", "speaker", "text", "label"),
    [
        ("最近在學游泳", "爸爸", "媽媽", "爸爸最近在學游泳", "爸爸"),
        ("住在台中", "妹妹", "媽媽", "其實妹妹住台中", "成員乙丙"),
        ("住在台中", "乙丙", "成員甲", "成員乙丙住台中", "成員乙丙"),  # 模型寫短的叫法也行
        ("喜歡爬山", "老公", "媽媽", "我老公喜歡爬山", "爸爸"),
        ("喜歡爬山", "老婆", "爸爸", "老婆喜歡爬山", "媽媽"),
        ("喜歡爬山", "爸爸", "成員甲", "我們爸爸喜歡爬山", "爸爸"),
        ("住在台中", "爸爸", "成員甲", "爸爸住台中媽媽住台北", "爸爸"),
        ("住在台中", "爸爸", "成員甲", "早安！爸爸住台中，媽媽住台北", "爸爸"),
    ],
)
def test_a_family_member_named_as_the_subject_gets_the_fact(fact, about, speaker, text, label):
    assert judge(fact, about, speaker, text) == h.Verdict(True, label, speaker)


@pytest.mark.parametrize(
    ("fact", "about", "speaker", "text", "reason"),
    [
        ("住在台中", "媽媽", "成員甲", "同學媽媽住台中", "position_left"),  # 別人的媽媽
        ("喜歡爬山", "爸爸", "成員甲", "他們爸爸喜歡爬山", "position_left"),
        ("住在台中", "妹妹", "媽媽", "跟妹妹住台中", "position_left"),  # 受詞、陪同的位置
        ("住在台中", "妹妹", "媽媽", "妹妹的同學住台中", "position_possessive"),
        ("住在台中", "爸爸", "成員甲", "爸爸是不是住台中？", "hedged"),
        ("住在台中", "爸爸", "成員甲", "聽說爸爸住台中", "hedged"),
        ("住在台北", "爸爸", "成員甲", "爸爸住台中媽媽住台北", "bind_none"),  # 換錯人
        ("喜歡爬山", "老公", "成員甲", "我老公喜歡爬山", "unresolved"),  # 只有媽媽說的老公是爸爸
        ("喜歡爬山", "我妹", "成員甲", "我妹喜歡爬山", "unresolved"),  # 「我＋稱謂」沒有子女名單：不收
        ("住在台中", "同學", "成員甲", "我同學住台中", "unresolved"),  # 群組以外的人
        ("住在台中", "妹妹", "媽媽", "爸爸住台中", "not_in_clause"),
    ],
)
def test_anything_unclear_about_who_it_is_is_dropped(fact, about, speaker, text, reason):
    assert judge(fact, about, speaker, text) == h.Verdict(False, reason=reason)


def test_the_clause_must_be_unambiguous():
    # 兩句和事實的重疊一樣多：分不出出自哪一句
    assert judge("喜歡爬山", "爸爸", "成員甲", "爸爸喜歡爬山，媽媽喜歡爬山").reason == "clause_tie"


def test_every_mention_of_the_person_in_the_clause_must_be_in_subject_position():
    assert judge("住在台中", "爸爸", "成員甲", "爸爸住台中跟爸爸").reason == "position_left"


# ── 本人的事（轉述開關開啟時的衛生檢查）─────────────────────────────────────


@pytest.mark.parametrize(
    ("fact", "about", "text"),
    [
        ("住在台中", "我", "我住台中"),
        ("住在台中", "（省略）", "住台中很久了"),
        ("住在台中", "", "住台中很久了"),  # 沒填 about 當主詞省略
        ("住在台中", "成員甲", "成員甲住台中"),  # 用自己的稱呼講自己
    ],
)
def test_the_speakers_own_facts_stay_with_the_speaker(fact, about, text):
    assert judge(fact, about, "成員甲", text) == h.Verdict(True, "成員甲", "")


@pytest.mark.parametrize(
    ("fact", "about", "text", "reason"),
    [
        ("住在台中", "（省略）", "我同學住台中", "self_other_people"),  # 0086f5 10/16 看到的情況
        ("住在台中", "（省略）", "他住台中", "self_other_people"),
        ("住在台中", "（省略）", "爸爸住台中", "self_other_member"),  # 模型漏填 about
        ("住在台中", "我", "住台中很久了", "self_word_missing"),
    ],
)
def test_a_speakers_fact_that_may_be_about_someone_else_is_dropped(fact, about, text, reason):
    assert judge(fact, about, "成員甲", text) == h.Verdict(False, reason=reason)


# ── post-impl 審查（GP2 #1、#2）：原文裡模型改寫看不到的東西 ──────────────────────


@pytest.mark.parametrize(
    ("fact", "about", "speaker", "text", "reason"),
    [
        ("搬到台中", "爸爸", "成員甲", "爸爸想搬到台中", "source:wish_past_other"),
        ("搬到台中", "爸爸", "成員甲", "爸爸下個月要搬到台中", "message:time_or_retraction"),
        ("住在台中", "爸爸", "成員甲", "爸爸以前住台中，現在住台北", "message:time_or_retraction"),
        ("喜歡爬山", "爸爸", "成員甲", "爸爸不喜歡爬山", "source:negation"),
        ("在科技公司上班", "爸爸", "成員甲", "爸爸跟他老闆在科技公司上班", "source:pronoun"),
        ("在科技公司上班", "爸爸", "成員甲", "爸爸被公司資遣", "source:sensitive"),
        ("住在台中", "爸爸", "成員甲", "聽說，爸爸住台中", "hedged"),
        ("住在台中", "爸爸", "成員甲", "爸爸住台中，我亂說的", "hedged"),
        ("住在台中", "爸爸", "成員甲", "爸爸住台中，對嗎？", "hedged"),
        ("住在台中", "爸爸", "成員甲", "據說爸爸住台中", "hedged"),
        ("住在台中", "爸爸", "成員甲", "我猜爸爸住台中", "hedged"),
        ("住在台中", "爸爸", "成員甲", "爸爸住台中才怪", "hedged"),
        ("住在台中", "爸爸", "成員甲", "爸爸住台中對不對", "hedged"),
        ("住在台中", "爸爸", "成員甲", "爸爸沒住台中", "source:negation"),
        ("住在台中", "爸爸", "成員甲", "爸爸一個人住台中", "source:health"),
        ("住在台中", "媽媽", "成員甲", "媽媽有個朋友住台中", "source:third_party"),
        ("住在台中", "媽媽", "成員甲", "媽媽說她朋友住台中", "source:pronoun"),
        ("在學游泳", "爸爸", "成員甲", "爸爸幫孫子在學游泳", "source:wish_past_other"),
    ],
)
def test_the_source_sentence_is_checked_not_just_the_models_rewrite(fact, about, speaker, text, reason):
    assert judge(fact, about, speaker, text) == h.Verdict(False, reason=reason)


def test_a_negative_fact_that_matches_the_source_is_kept():
    assert judge("不喜歡吃辣", "爸爸", "成員甲", "爸爸不喜歡吃辣") == h.Verdict(True, "爸爸", "成員甲")


@pytest.mark.parametrize(
    ("fact", "about", "speaker", "text", "reason"),
    [
        ("住在台中", "我", "成員甲", "我同學住台中", "self_other_people"),
        ("在科技公司上班", "我", "成員甲", "我老公在科技公司上班", "self_other_people"),
        ("喜歡爬山", "我", "成員甲", "我的朋友喜歡爬山", "self_other_people"),
        ("住在台中", "（省略）", "成員甲", "我爸住台中", "self_other_people"),
        ("住在台中", "媽媽", "媽媽", "媽媽的朋友住台中", "self_possessive"),
        ("住在台中", "媽媽", "媽媽", "媽媽的媽媽住台中", "self_possessive"),
    ],
)
def test_someone_elses_fact_never_lands_under_the_speaker(fact, about, speaker, text, reason):
    assert judge(fact, about, speaker, text) == h.Verdict(False, reason=reason)


@pytest.mark.parametrize("label", ["他", "朋友", "同學", "大家", "老爸", "爸", "我們", "阿嬤"])
def test_unsafe_display_labels(label):
    assert h.unsafe_label(label)


@pytest.mark.parametrize("label", ["Amy", "成員甲", "小寶貝"])
def test_ordinary_display_labels(label):
    assert not h.unsafe_label(label)


# ── post-impl 審查第二輪（R2 GP1 F1–F3、N1、N2）──────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["爸爸說小明住台中", "爸爸教小明住台中", "爸爸陪小明住台中", "爸爸羨慕老張住台中",  # F1：B 後面接的是別人的事
     "爸爸會搬到台中", "爸爸住過台中", "爸爸小時候住台中", "爸爸住台中，不對，住台南", "爸爸住台中還是台南",  # F2
     "他說其實爸爸住台中", "去年其實爸爸住台中", "爸爸哪有住台中"],
)
def test_more_ways_the_fact_is_not_really_about_the_member(text):
    fact = "搬到台中" if "搬到" in text else "住在台中"
    verdict = judge(fact, "爸爸", "成員甲", text)
    assert not verdict.ok and verdict.reason.startswith(("source:", "hedged", "position", "message:"))


def test_an_allowed_adverb_is_not_a_negation():
    assert judge("喜歡爬山", "爸爸", "成員甲", "爸爸特別喜歡爬山") == h.Verdict(True, "爸爸", "成員甲")


@pytest.mark.parametrize(
    ("about", "speaker", "text", "reason"),
    [
        ("媽媽", "媽媽", "我媽媽住台中", "self_position"),  # 說話的人的媽媽，不是她自己
        ("我", "媽媽", "我媽媽住台中", "self_position"),
        ("我", "媽媽", "我的媽媽住台中", "self_position"),
        ("（省略）", "成員甲", "爸媽住台中", "self_other_people"),
        ("我", "媽媽", "我先生住台中", "self_other_people"),
    ],
)
def test_family_words_the_member_list_does_not_cover_still_block_a_speakers_fact(about, speaker, text, reason):
    assert judge("住在台中", about, speaker, text) == h.Verdict(False, reason=reason)


@pytest.mark.parametrize(
    ("fact", "text"),
    [("喜歡爬山", "我喜歡爬山，爸爸也喜歡爬山"), ("住在新竹", "我住新竹，上班也在新竹")],
)
def test_the_speakers_own_sentence_is_the_one_with_me_in_it(fact, text):
    assert judge(fact, "我", "成員甲", text) == h.Verdict(True, "成員甲", "")



# ── post-impl 審查第二輪（R2 GP2 B1、B2、N1、N2、ASCII 名字）──────────────────


@pytest.mark.parametrize(
    "text",
    ["以前，爸爸住台中", "爸爸住台中，不是啦", "去年，爸爸在科技公司上班", "爸爸住台中，我是說台北",
     "爸爸住台中，已經不是了", "爸爸住台中，現在搬走了", "爸爸住台中的話很方便"],
)
def test_time_past_and_retractions_anywhere_in_the_message_drop_hearsay(text):
    fact = "在科技公司上班" if "上班" in text else "住在台中"
    assert judge(fact, "爸爸", "成員甲", text) == h.Verdict(False, reason="message:time_or_retraction")


@pytest.mark.parametrize(
    ("fact", "text"),
    [("住在台中，養了兩隻貓", "爸爸住台中"), ("在科技公司上班，快退休了", "爸爸在科技公司上班")],
)
def test_the_model_cannot_add_what_the_member_never_said(fact, text):
    assert judge(fact, "爸爸", "成員甲", text) == h.Verdict(False, reason="source:fabricated")


@pytest.mark.parametrize(("about", "text"), [("我", "媽媽：我住台中"), ("我", "同學說：我住台中")])
def test_a_quoted_first_person_is_someone_else(about, text):
    assert judge("住在台中", about, "成員甲", text) == h.Verdict(False, reason="self_quoted")


def test_hearsay_relayed_from_a_third_party_is_dropped():
    assert judge("住在台中", "爸爸", "成員甲", "同學說：爸爸住台中").reason in ("quoted", "source:frame")


@pytest.mark.parametrize("body", ["喜歡帥哥", "喜歡韓國瑜", "喜歡李小姐", "迷上某個人"])
def test_preferences_must_be_an_activity_or_a_listed_hobby(body):
    assert h.scope_reason(body) != ""


@pytest.mark.parametrize("body", ["喜歡爬山", "最愛吃火鍋", "興趣是攝影", "迷上攝影", "不喜歡吃辣", "喜歡去爬山"])
def test_activity_preferences_still_pass(body):
    assert h.scope_reason(body) == ""


def test_an_english_name_must_match_the_whole_word():
    roster = h.build_roster(["Ed", "成員甲"], {})
    assert judge("住在台中", "Ed", "成員甲", "Edwin住台中", roster).ok is False
    assert judge("住在台中", "Ed", "成員甲", "Ed住台中", roster) == h.Verdict(True, "Ed", "成員甲")



# ── round 3（回歸審查）──────────────────────────────────────────────────────


def test_very_is_not_a_negation():
    assert judge("喜歡爬山", "爸爸", "成員甲", "爸爸非常喜歡爬山") == h.Verdict(True, "爸爸", "成員甲")


@pytest.mark.parametrize("text", ["爸爸住台中或台南", "爸爸住台中蛤", "爸爸住台中ㄇ"])
def test_more_hedges(text):
    assert judge("住在台中", "爸爸", "成員甲", text).reason == "hedged"


@pytest.mark.parametrize(
    ("about", "text"),
    [("（省略）", "阿爸住台中"), ("（省略）", "老伴住台中"), ("（省略）", "爸住台中"), ("我", "我老伴住台中"),
     ("我", "我家那口子住台中"), ("（省略）", "醫生住台中")],
)
def test_more_family_words_keep_a_fact_off_the_speaker(about, text):
    assert judge("住在台中", about, "成員甲", text) == h.Verdict(False, reason="self_other_people")
