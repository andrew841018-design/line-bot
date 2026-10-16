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
    assert h.scope_reason("喜歡吃其他口味") == ""  # 「其他」的「他」不是代名詞


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
