"""2026-10-11 爸媽健康：parent_health_extract 的抽取規則、緊急字詞、固定詞表與線性時間。

句子全部是合成的：計畫語料、審查員列的誤判／漏判句、協調者補的習慣句，不取自真實對話；
需要其他家人的名字時用「成員甲」這類假名。
"""

from __future__ import annotations

import random

import pytest

import parent_health_extract as phx
from parent_health_extract import (
    ALLOWED_ITEMS,
    CATEGORIES,
    FAMILY,
    FATHER,
    IN_CARE_LABELS,
    MAX_CHARS,
    MOTHER,
    PARENTS,
    RED_FLAG_LABELS,
    VALUE_RE,
    Finding,
    RedFlag,
    detect_red_flags,
    extract,
    might_have_red_flag,
)

F, M, X = FATHER, MOTHER, FAMILY
R = "redflag"


def _findings(*items):
    """(role, category, item[, value[, flag]]) → 補齊成五欄後排序，順序不算規格。"""
    return sorted(tuple(item) + ("",) * (5 - len(item)) for item in items)


def _flags(*items):
    """(role, label, urgent, in_care, future)。"""
    return sorted(items)


def _case(tag, speaker, text, findings=(), flags=()):
    return pytest.param(speaker, text, _findings(*findings), _flags(*flags), id=tag)


def _as_tuples(findings):
    return sorted((f.role, f.category, f.item, f.value, f.flag) for f in findings)


def _flag_tuples(flags):
    return sorted((r.role, r.label, r.urgent, r.in_care, r.future) for r in flags)


# ── 一張表：計畫語料（corpus）、GP1 審查 #1 #2 #6 #7 #8、協調者的習慣句、其餘回歸 ──
CASES = [
    # 主詞
    _case("corpus-01", F, "我剛吃完藥了", [(F, "med", "吃藥")]),
    _case("corpus-02", F, "今天有去散步", [(F, "exercise", "散步")]),
    _case("corpus-03", M, "媽媽今天去回診了", [(M, "visit", "回診")]),
    _case("corpus-04", M, "你爸昨晚又睡不好", [(F, "sleep", "睡不好")]),
    _case("corpus-05", M, "爸爸剛爬山回來", [(F, "exercise", "爬山")]),
    _case("corpus-06", M, "我媽今天去看醫生了"),  # 媽媽的媽媽＝長輩
    _case("corpus-07", M, "外婆最近睡不好"),
    _case("corpus-08", F, "老婆今天有去走路", [(M, "exercise", "散步")]),
    _case("corpus-09", X, "爸今天血壓 135/85", [(F, "body", "血壓", "135/85")]),
    _case("corpus-10", X, "我媽剛吃過藥了", [(M, "med", "吃藥")]),
    _case("corpus-11", X, "爸媽今天去爬山了", [(F, "exercise", "爬山"), (M, "exercise", "爬山")]),
    _case("corpus-12", X, "我今天去散步了"),  # 說話者不是家長
    _case("corpus-13", X, "我老公今天去回診了"),  # 兄弟姊妹的配偶
    _case("corpus-14", X, "阿嬤今天去復健"),
    _case("corpus-15", F, "朋友今天去看醫生了"),
    _case("corpus-16", F, "他昨天跌倒了"),
    _case("corpus-17", M, "寶寶昨晚一直醒"),
    # 指示／提醒、問句、未來、假設、勸告、轉述
    _case("corpus-18", X, "記得提醒爸爸吃藥"),
    _case("corpus-19-dad", F, "爸爸叫我去散步"),
    _case("corpus-19-family", X, "爸爸叫我去散步"),
    _case("corpus-20", M, "你爸吃藥了嗎？"),
    _case("corpus-21", X, "媽媽有沒有去回診"),
    _case("corpus-22", F, "明天要去回診"),
    _case("corpus-23", M, "等一下要去散步"),
    _case("corpus-24", F, "如果睡不好就早點休息"),
    _case("corpus-25", M, "要多運動喔"),
    _case("corpus-26", F, "醫生說要多走路"),
    _case("corpus-27", M, "新聞說每天走八千步比較健康"),
    _case("corpus-28", X, "研究發現失眠的人…"),
    # 否定與好轉
    _case("corpus-29", F, "今天沒去散步"),
    _case("corpus-30", M, "頭不痛了"),
    _case("corpus-31", F, "已經沒有發燒"),
    _case("corpus-32", M, "感冒好多了"),
    _case("corpus-33", F, "我還沒吃藥"),  # 還沒≠忘了
    _case("corpus-34", M, "我忘了吃藥", [(M, "med", "忘了吃藥")]),
    _case("corpus-35", F, "昨天藥忘記吃", [(F, "med", "忘了吃藥")]),
    _case("corpus-36", M, "不用吃藥了"),
    # 各類別正例
    _case("corpus-37", F, "降血壓的藥吃了", [(F, "med", "吃藥")]),
    _case("corpus-38", M, "剛去診所拿藥", [(M, "med", "拿藥")]),
    _case("corpus-39", F, "今天洗牙完", [(F, "visit", "洗牙")]),
    _case("corpus-40", M, "剛從醫院復健回來", [(M, "visit", "復健")]),
    _case("corpus-41", F, "走了 8000 步", [(F, "exercise", "散步", "8000步")]),
    _case("corpus-42", M, "早上去公園打太極", [(M, "exercise", "太極")]),  # 「早上」算已經發生
    _case("corpus-43", F, "昨晚睡得很好", [(F, "sleep", "睡得好")]),
    _case("corpus-44", M, "半夜一直醒", [(M, "sleep", "睡不好")]),
    _case("corpus-45", F, "最近胃口不好", [(F, "diet", "胃口不好")]),
    _case("corpus-46", M, "我最近少吃甜的", [(M, "diet", "少吃甜")]),
    _case("corpus-47", F, "今天沒吃早餐", [(F, "diet", "沒吃早餐")]),
    _case("corpus-48", M, "血糖 110", [(M, "body", "血糖", "110")]),
    _case("corpus-49", F, "血壓 300/20"),  # 範圍外
    _case("corpus-50", M, "今天頭暈", [(M, "body", "頭暈")]),
    _case("corpus-51", F, "膝蓋痛了兩天", [(F, "body", "膝蓋痛")]),
    # 緊急字詞（問句也算）
    _case("corpus-52", F, "我胸口好悶", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("corpus-53", M, "你爸剛剛跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("corpus-54", X, "媽媽講話不清楚，嘴有點歪", [(M, "body", "嘴歪或說話不清", "", R)],
          [(M, "嘴歪或說話不清", True, False, False)]),
    _case("corpus-55", F, "胸悶要看哪一科？", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("corpus-56", M, "我右邊手腳突然沒力", [(M, "body", "單側無力", "", R)], [(M, "單側無力", True, False, False)]),
    _case("corpus-57", F, "上個月跌倒，現在沒事了", [(F, "body", "跌倒")], [(F, "跌倒", False, False, False)]),
    _case("corpus-58", M, "沒有胸悶"),
    _case("corpus-59", F, "走路要小心不要跌倒"),
    _case("corpus-60", M, "怕會跌倒所以買了止滑墊"),
    _case("corpus-61", X, "朋友昨天跌倒住院"),
    _case("corpus-62", X, "新聞：某藝人胸痛送醫 https://…"),
    _case("corpus-63", M, "如果胸痛要馬上打119"),
    _case("corpus-64", F, "股票跌倒谷底"),
    # GP1 #1：日常用語不能觸發 119（程度補語、轉貼衛教、怕／差點、親戚與別人）
    _case("gp1-1-degree-breath", F, "最近忙到喘不過氣"),
    _case("gp1-1-degree-speech", M, "氣到說不出話"),
    _case("gp1-1-degree-faint", F, "笑到快昏倒"),
    _case("gp1-1-frozen-shoulder", M, "五十肩，手舉不起來", [(M, "body", "肩膀痛")]),
    _case("gp1-1-tip-signs", F, "中風三大徵兆：臉歪、手無力、口齒不清"),
    _case("gp1-1-tip-not-always", M, "胸悶不一定是心臟病"),
    _case("gp1-1-afraid", F, "怕跌倒所以買止滑墊"),
    _case("gp1-1-almost", M, "今天差點跌倒"),
    _case("gp1-1-aunt-child", X, "姑媽昨天跌倒住院"),
    _case("gp1-1-aunt-mom", M, "姑媽昨天跌倒住院"),
    _case("gp1-1-neighbour-wife", F, "隔壁王太太跌倒了"),
    _case("gp1-1-uncle", M, "樓下阿伯跌倒送醫"),
    _case("gp1-1-dads-friend-family", X, "爸爸說他朋友胸痛住院"),
    _case("gp1-1-dads-friend-mom", M, "爸爸說他朋友胸痛住院"),
    # GP1 #2：真的緊急狀況要認得
    _case("gp1-2-fell-a-tumble", F, "我跌了一跤", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("gp1-2-stairs", M, "從樓梯摔下來", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("gp1-2-chest-pain", F, "胸口好痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("gp1-2-chest-tight", M, "胸口很悶", [(M, "body", "胸痛胸悶", "", R)], [(M, "胸痛胸悶", True, False, False)]),
    _case("gp1-2-left-hand", F, "左手沒力", [(F, "body", "單側無力", "", R)], [(F, "單側無力", True, False, False)]),
    _case("gp1-2-half-side", M, "半邊沒力", [(M, "body", "單側無力", "", R)], [(M, "單側無力", True, False, False)]),
    _case("gp1-2-speech", F, "說話不清楚", [(F, "body", "嘴歪或說話不清", "", R)],
          [(F, "嘴歪或說話不清", True, False, False)]),
    _case("gp1-2-mouth", M, "嘴巴歪一邊", [(M, "body", "嘴歪或說話不清", "", R)],
          [(M, "嘴歪或說話不清", True, False, False)]),
    _case("gp1-2-stroke", X, "爸爸好像中風了", [(F, "body", "中風", "", R)], [(F, "中風", True, False, False)]),
    _case("gp1-2-in-er", X, "媽媽在急診", [(M, "visit", "急診")], [(M, "急診", True, True, False)]),
    _case("gp1-2-ambulance", F, "叫救護車了", [], [(F, "救護車", True, True, False)]),
    # GP1 #6：用藥
    _case("gp1-6-took", F, "我吃藥了", [(F, "med", "吃藥")]),
    _case("gp1-6-topic", M, "藥吃了", [(M, "med", "吃藥")]),
    _case("gp1-6-fu-yao", F, "服藥了", [(F, "med", "吃藥")]),
    _case("gp1-6-already", M, "已經吃藥", [(M, "med", "吃藥")]),
    _case("gp1-6-not-yet", X, "爸爸今天還沒有吃藥"),
    _case("gp1-6-did-not", F, "沒有吃藥"),
    _case("gp1-6-after-meal", M, "吃完飯要吃藥"),
    _case("gp1-6-herbal-soup", F, "吃了藥膳"),
    _case("gp1-6-almost-forgot", M, "差點忘了吃藥"),
    _case("gp1-6-afraid-forget", F, "怕忘了吃藥"),
    # GP1 #7：主詞
    _case("gp1-7-carry", M, "你爸早上去散步，回來說膝蓋痛", [(F, "exercise", "散步"), (F, "body", "膝蓋痛")]),
    _case("gp1-7-vocative", X, "媽 我今天去看牙了"),
    _case("gp1-7-two-titles", X, "爸爸吃藥了 媽媽還沒吃", [(F, "med", "吃藥")]),
    # GP1 #8：其他抽取錯誤與子字串陷阱
    _case("gp1-8-no-dizzy", F, "今天沒有頭暈"),
    _case("gp1-8-slept-late", M, "昨晚睡得好晚"),
    _case("gp1-8-slept-little", F, "昨晚睡得好少"),
    _case("gp1-8-a-not-a-family", X, "媽媽昨晚睡得好不好"),
    _case("gp1-8-a-not-a-dad", F, "媽媽昨晚睡得好不好"),
    _case("gp1-8-less-sugar-family", X, "爸爸要少吃甜的"),
    _case("gp1-8-less-sugar-mom", M, "爸爸要少吃甜的"),
    _case("gp1-8-help-reading", X, "幫爸爸量血壓 150/95", [(F, "body", "血壓", "150/95")]),
    _case("gp1-8-booked", F, "已經預約了回診"),
    _case("gp1-8-report", M, "健檢報告出來了"),
    _case("gp1-8-trap-qita", F, "今天其他還好就是有點頭暈", [(F, "body", "頭暈")]),
    _case("gp1-8-trap-qita-carry", X, "爸爸說其他都好，就是膝蓋痛", [(F, "body", "膝蓋痛")]),
    _case("gp1-8-trap-tebie", M, "這週特別忘了吃藥", [(M, "med", "忘了吃藥")]),
    _case("gp1-8-trap-before-sleep", F, "睡覺之前胸口好悶", [(F, "body", "胸痛胸悶", "", R)],
          [(F, "胸痛胸悶", True, False, False)]),
    # 協調者：習慣／傾向不是緊急狀況（也不記成一般身體紀錄）
    _case("habit-easily", F, "我容易跌倒"),
    _case("habit-often-family", X, "媽媽常常胸悶"),
    _case("habit-often-dad", F, "媽媽常常胸悶"),
    _case("habit-sometimes-family", X, "爸爸偶爾會頭暈"),
    _case("habit-sometimes-mom", M, "爸爸偶爾會頭暈"),
    _case("habit-tends-to", F, "年紀大會跌倒"),
    _case("habit-frequently", M, "我經常喘不過氣"),
    _case("habit-always", F, "我老是頭暈"),
    _case("habit-at-times", X, "爸爸有時候會胸悶"),
    _case("habit-every-while", M, "我隔一陣子會胸悶"),
    _case("habit-will-or-not", X, "爸爸會不會中風"),
    # 其他回歸：常見說法不能被排除詞誤傷
    _case("reg-accidentally", X, "媽媽剛剛不小心跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("reg-i-feel", F, "我覺得胸口好悶", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("reg-meeting", F, "開會的時候跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("reg-fire-brigade", F, "消防隊來的時候我跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("reg-zero-width", F, "胸\u200b口好痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("reg-question-is-not-resolved", X, "爸爸跌倒了 沒事吧", [(F, "body", "跌倒", "", R)],
          [(F, "跌倒", True, False, False)]),
    _case("reg-merge-old-and-new", F, "上個月跌倒，今天又跌倒了", [(F, "body", "跌倒", "", R)],
          [(F, "跌倒", True, False, False)]),
    _case("reg-in-care-spreads", X, "爸爸胸口好痛，已經叫救護車了", [(F, "body", "胸痛胸悶", "", R)],
          [(F, "胸痛胸悶", True, True, False), (F, "救護車", True, True, False)]),
    _case("reg-other-mom-possessive", X, "朋友的媽媽住院了"),
    _case("reg-his-mom", X, "他媽媽昨天跌倒了"),
    _case("reg-slash-command", F, "/記住 爸爸容易跌倒"),
    _case("reg-future-plan", X, "明天帶媽媽回診追蹤跌倒的傷"),
    # 只出現在未來子句：只私訊，不記成 ⚠️ 紀錄
    _case("reg-future-only", F, "下週要去檢查胸悶", [], [(F, "胸痛胸悶", True, False, True)]),
    # 家人轉述要有報告標記：「媽媽胸悶」這類一般描述不算，「媽媽現在胸悶」算
    _case("reg-family-needs-marker", X, "媽媽胸悶"),
    _case("reg-family-with-marker", X, "媽媽現在胸悶", [(M, "body", "胸痛胸悶", "", R)], [(M, "胸痛胸悶", True, False, False)]),
    # 主詞：「他」回指前面的稱謂、並列的家長都算、別人的家長不算
    _case("subject-he-after-said", X, "爸爸說他胸口很悶", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("subject-she-after-adverb", X, "媽媽剛剛說她頭痛", [(M, "body", "頭痛")]),
    _case("subject-topic-pronoun", M, "你爸他今天跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("subject-he-of-a-pair", X, "爸爸跟媽媽說他胸口好痛", [(F, "body", "胸痛胸悶", "", R)],
          [(F, "胸痛胸悶", True, False, False)]),
    _case("subject-dads-mom", X, "爸爸說他媽媽住院了"),  # 爸爸的媽媽＝長輩
    _case("subject-friends-dad", X, "朋友的爸爸說他跌倒了"),
    _case("subject-i-said-he", F, "我說他胸口痛"),
    _case("subject-and", X, "爸爸和媽媽今天去爬山了", [(F, "exercise", "爬山"), (M, "exercise", "爬山")]),
    _case("subject-me-and-your-dad", M, "我跟你爸去爬山了", [(F, "exercise", "爬山"), (M, "exercise", "爬山")]),
    _case("subject-dad-with-me", X, "爸爸跟我去散步了", [(F, "exercise", "散步")]),
    _case("subject-movie-character", M, "這部電影主角中風了"),
    # 家人轉述的報告標記：同一句提到送醫／急診、驚嘆號、「歪歪的」
    _case("marker-in-care", X, "爸爸胸口痛送急診", [(F, "body", "胸痛胸悶", "", R)],
          [(F, "胸痛胸悶", True, True, False), (F, "急診", True, True, False)]),
    _case("marker-exclamation", X, "爸爸跌倒！", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("marker-reduplicated", X, "爸爸嘴角歪歪的", [(F, "body", "嘴歪或說話不清", "", R)],
          [(F, "嘴歪或說話不清", True, False, False)]),
    # 緊急字詞的其他細節
    _case("rf-sent-to-er", X, "爸爸被送到急診了", [(F, "visit", "急診")], [(F, "急診", True, True, False)]),
    _case("rf-a-not-a-is-not-negation", X, "媽媽是不是中風了", [(M, "body", "中風", "", R)], [(M, "中風", True, False, False)]),
    _case("rf-asking-is-not-resolved", X, "爸爸跌倒了 現在沒事了嗎", [(F, "body", "跌倒", "", R)],
          [(F, "跌倒", True, False, False)]),
    _case("rf-experienced-before", F, "我中風過", [(F, "body", "中風")], [(F, "中風", False, False, False)]),
    _case("rf-chest-discomfort", F, "我胸口不舒服", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("rf-ran-out-of-breath", F, "最近跑到喘不過氣"),
    _case("rf-insurance-plan", M, "我有買住院險"),
    _case("rf-insurance-claim", F, "保險可以理賠住院嗎"),
    _case("rf-siren", F, "外面救護車的聲音好吵"),
    _case("rf-anti-fall-mat", F, "買了跌倒防止墊"),
    # 讀數
    _case("reading-then-question", F, "血壓 135/85 正常嗎", [(F, "body", "血壓", "135/85")]),
    _case("reading-fullwidth", M, "血壓１３５／８５", [(M, "body", "血壓", "135/85")]),
    _case("reading-many", M, "血壓 135/85 血糖 110 體重 65.5 心跳 72",
          [(M, "body", "血壓", "135/85"), (M, "body", "血糖", "110"), (M, "body", "體重", "65.5"), (M, "body", "心跳", "72")]),
    _case("reading-two-bp", F, "早上血壓135/85，晚上140/90", [(F, "body", "血壓", "135/85"), (F, "body", "血壓", "140/90")]),
    _case("reading-two-people", X, "爸爸血壓135/85 媽媽140/90", [(F, "body", "血壓", "135/85"), (M, "body", "血壓", "140/90")]),
    _case("reading-swapped-bp", F, "血壓 85/135"),
    _case("reading-low-glucose", F, "血糖 30"),
    _case("reading-heavy", M, "體重 200"),
    _case("reading-fast-pulse", F, "心跳 250"),
    _case("reading-target", M, "血壓要控制在130/80以下"),
    _case("reading-date-is-not-bp", F, "血壓135/85，10/23回診", [(F, "body", "血壓", "135/85")]),
    # 其他類別
    _case("steps-chinese", F, "今天走了一萬步", [(F, "exercise", "散步", "10000步")]),
    _case("steps-replace-plain-walk", M, "爸爸今天有去散步，走了 6000 步", [(F, "exercise", "散步", "6000步")]),
    _case("visit-vaccine", X, "媽媽昨天去打了流感疫苗", [(M, "visit", "打疫苗")]),
    _case("diet-drink-order", M, "我要一杯少糖的"),
    _case("diet-no-appetite", M, "沒什麼胃口", [(M, "diet", "胃口不好")]),
    _case("med-cold-pills", F, "吃了感冒藥", [(F, "med", "吃藥"), (F, "body", "感冒")]),
    # 主線：常見的藥名夾在中間（長輩回報吃藥最常這樣說）
    _case("med-kind-took", F, "我吃降血壓藥了", [(F, "med", "吃藥")]),
    # 主線：「看完醫生」這種把「完」夾在中間的完成說法
    _case("visit-saw-doctor-done", M, "剛看完醫生", [(M, "visit", "看醫生")]),
    _case("visit-saw-dentist-done", F, "看完牙了", [(F, "visit", "看牙")]),
    _case("visit-family-eye-done", X, "媽媽今天看完眼科了", [(M, "visit", "看眼科")]),
    _case("visit-done-then-advice", F, "看完醫生了，醫生說要多走路", [(F, "visit", "看醫生")]),
    _case("med-kind-took-le", M, "吃了降血糖的藥", [(M, "med", "吃藥")]),
    _case("med-kind-just", F, "剛吃完降膽固醇的藥", [(F, "med", "吃藥")]),
    _case("med-kind-family", X, "爸爸剛吃了降血壓的藥", [(F, "med", "吃藥")]),
    _case("med-kind-instruction", X, "爸爸要吃降血壓藥"),
    _case("med-kind-negated", F, "今天沒吃降血壓藥"),
    _case("med-kind-forgot", M, "我忘了吃降血壓藥", [(M, "med", "忘了吃藥")]),
    _case("exercise-should-have", X, "爸爸要有運動"),
    _case("sleep-without-de", F, "昨天睡很好", [(F, "sleep", "睡得好")]),
    _case("sleep-went-to-bed-late", F, "昨天睡好晚"),
    _case("sleep-full", F, "睡飽了", [(F, "sleep", "睡得好")]),
    _case("sleep-not-full", F, "還沒睡飽", [(F, "sleep", "睡不好")]),
    _case("symptom-laughing", F, "笑到肚子痛"),
    _case("unknown-speaker-is-family", "成員乙", "我今天有去散步"),
    # ── 第二輪審查（實作後）#1：別人不能算到說話的家長頭上（誤發 119 是這個功能最糟的結果）──
    _case("r2-1-my-sister", M, "我姊中風了"),
    _case("r2-1-my-third-sister", M, "我三姊中風了"),
    _case("r2-1-my-little-sister", M, "我小妹跌倒了"),
    _case("r2-1-my-sister-in-law", M, "我嫂嫂跌倒了"),
    _case("r2-1-visiting-aunt", M, "我去醫院探望住院的阿姨"),
    _case("r2-1-my-brother", F, "我哥昨天心肌梗塞住院"),
    _case("r2-1-my-younger-brother", F, "我弟胸口痛"),
    _case("r2-1-my-student", F, "我學生昏倒了"),
    _case("r2-1-old-wang", F, "隔壁老王中風了"),
    _case("r2-1-that-driver", F, "那個司機昏倒了"),
    _case("r2-1-a-lady", F, "看到一個小姐跌倒"),
    _case("r2-1-an-auntie", F, "公園有個歐巴桑昏倒了"),
    _case("r2-1-cat", F, "貓從桌上摔下來"),
    _case("r2-1-everyone-else", F, "其他人都跌倒了"),
    _case("r2-1-mrs-wang-family", X, "王媽媽昨天跌倒住院了"),
    _case("r2-1-mrs-wang-dad", F, "王媽媽昨天跌倒住院了"),
    _case("r2-1-mrs-wang-mom", M, "王媽媽昨天跌倒住院了"),
    _case("r2-1-xiaoming-mom-family", X, "小明的媽媽跌倒了"),
    _case("r2-1-xiaoming-mom-dad", F, "小明的媽媽跌倒了"),
    _case("r2-1-xiaoming-mom-mom", M, "小明的媽媽跌倒了"),
    _case("r2-1-mrs-chen-family", X, "陳媽媽中風了"),
    _case("r2-1-mrs-chen-dad", F, "陳媽媽中風了"),
    _case("r2-1-mrs-chen-mom", M, "陳媽媽中風了"),
    _case("r2-1-mr-lin-family", X, "林爸爸昨天中風了"),
    _case("r2-1-mr-lin-dad", F, "林爸爸昨天中風了"),
    _case("r2-1-mr-lin-mom", M, "林爸爸昨天中風了"),
    # 第二輪 #2：真的緊急狀況（副詞夾在中間、兄弟姊妹轉述爸媽的話）
    _case("r2-2-chest-again", F, "胸口又痛了", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r2-2-chest-still", F, "胸口還在痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r2-2-chest-still-very", F, "胸口還是很痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r2-2-chest-a-little", F, "胸口有一點悶", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r2-2-chest-starting", F, "胸口開始痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r2-2-relay-phoned", X, "媽媽剛剛打電話來說她跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r2-2-relay-texted", X, "爸爸剛剛傳訊息說他胸口痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r2-2-relay-told-me", X, "爸爸跟我說他胸口好痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r2-2-relay-told-me-arm", X, "媽媽跟我說她右手沒力", [(M, "body", "單側無力", "", R)], [(M, "單側無力", True, False, False)]),
    _case("r2-2-relay-told-me-morning", X, "爸爸早上跟我說他胸口悶", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r2-2-relay-said", X, "爸爸說他胸口痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r2-2-relay-called-mom", M, "你爸剛剛打給我說他胸口好悶", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    # 第二輪 #5：排定的住院／急診是未來，不是「已在處理」；住院費用不是緊急字詞
    _case("r2-5-surgery-next-week", X, "爸爸下週要住院開刀", [], [(F, "住院", True, False, True)]),
    _case("r2-5-admitted-tomorrow", X, "媽媽明天住院", [], [(M, "住院", True, False, True)]),
    _case("r2-5-er-report-tomorrow", X, "爸爸明天要去急診拿報告", [], [(F, "急診", True, False, True)]),
    _case("r2-5-hospital-bill-family", X, "爸爸住院的費用付了"),
    _case("r2-5-hospital-bill-mom", M, "爸爸住院的費用付了"),
    # 第二輪 #8：已經沒事＝不緊急；「中風後」是以前；程度補語；「頭暈倒在沙發上」
    _case("r2-8-luckily-fine", F, "我今天跌倒了，還好沒事", [(F, "body", "跌倒")], [(F, "跌倒", False, False, False)]),
    _case("r2-8-but-fine", F, "我跌倒了 不過沒事", [(F, "body", "跌倒")], [(F, "跌倒", False, False, False)]),
    _case("r2-8-nothing-much", F, "我跌倒了沒什麼事", [(F, "body", "跌倒")], [(F, "跌倒", False, False, False)]),
    _case("r2-8-ok-today", F, "昨天跌倒，今天還好", [(F, "body", "跌倒")], [(F, "跌倒", False, False, False)]),
    _case("r2-8-after-stroke", X, "爸爸中風後一直在復健", [(F, "body", "中風")], [(F, "中風", False, False, False)]),
    _case("r2-8-heat", F, "熱到快中暑昏倒"),
    _case("r2-8-dizzy-on-sofa", F, "我今天頭暈倒在沙發上", [(F, "body", "頭暈")]),
    # 上面規則不能誤傷的句子（「這個」「一個人」「樓下」「到」「連」「周」「的」「還好」都有一般用法）
    _case("r2-guard-alone", F, "我一個人在家跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r2-guard-this-week", F, "這個禮拜胸口一直痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r2-guard-downstairs", M, "我在樓下跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r2-guard-walked-to", X, "媽媽走到門口跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r2-guard-until-now", F, "從早上到現在胸口一直痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r2-guard-even-mom", X, "連媽媽也跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r2-guard-this-week-zhou", X, "這周媽媽有去散步", [(M, "exercise", "散步")]),
    _case("r2-guard-dear-mom", X, "親愛的媽媽剛剛跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r2-guard-our-family-mom", X, "我們家的媽媽今天有去散步", [(M, "exercise", "散步")]),
    _case("r2-guard-this-medicine", F, "這個藥吃了", [(F, "med", "吃藥")]),
    _case("r2-guard-luckily-you", F, "爸爸跌倒了，還好你在", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r2-guard-still-very-painful", F, "胸口還好痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r2-guard-my-own-chest", X, "我跟媽媽說我胸口痛"),
    _case("r2-guard-falling-is-dangerous", X, "爸爸說跌倒很危險"),
    _case("r2-guard-question-at-end", F, "吃藥了沒"),
    # 轉述要是真的轉述：「遺傳」「講座」「小說」裡的傳／講／說不算
    _case("r2-guard-heredity", X, "爸爸那邊有中風的遺傳"),
    _case("r2-guard-lecture", X, "爸爸去聽中風的講座"),
    _case("r2-relay-line", X, "爸爸剛剛傳LINE說他胸口痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r2-relay-told", X, "爸爸告訴我他胸口悶", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    # ── 第三輪審查（實作後）#1：「他／她」只回指說話動詞的主詞；聽話的人、別人的親戚都不是 ──
    _case("r3-1-his-brother", X, "爸爸說他哥昏倒了"),
    _case("r3-1-her-sister", X, "媽媽說她姊跌倒了"),
    _case("r3-1-her-sister-topic", X, "媽媽她姊跌倒了"),
    _case("r3-1-his-younger-brother", M, "你爸說他弟胸口痛"),
    _case("r3-1-his-card-friend", X, "爸爸說他的牌友跌倒了"),
    _case("r3-1-sister-told-mom", X, "妹妹跟媽媽說她跌倒了"),
    _case("r3-1-brother-told-dad", X, "哥哥跟爸爸說他胸口痛"),
    _case("r3-1-neighbour-told-dad", X, "鄰居告訴爸爸他昏倒了"),
    _case("r3-1-dad-said-she", X, "爸爸說她跌倒了"),
    _case("r3-1-told-the-clerk", X, "爸爸剛剛跟店員說他胸口痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    # 第三輪 #2：受詞、地點不是主詞（遛狗、帶孫子、在朋友家、一個人）
    _case("r3-2-alone-dad", X, "爸爸一個人在家跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r3-2-alone-mom", X, "媽媽一個人在家昏倒了", [(M, "body", "昏倒", "", R)], [(M, "昏倒", True, False, False)]),
    _case("r3-2-walking-dog", X, "爸爸遛狗的時候跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r3-2-walking-dog-self", F, "剛剛遛狗跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r3-2-feeding-cat", X, "媽媽餵貓的時候跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r3-2-old-man", X, "爸爸他老人家昨天跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r3-2-at-the-doctor", X, "爸爸去看醫生的時候昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r3-2-friends-house", X, "爸爸在朋友家跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r3-2-taking-grandchild", M, "剛剛我送孫子去學校跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r3-2-shopping", X, "爸爸去對面買東西跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r3-2-dog-walk-and-fall", M, "我剛剛帶狗去散步跌倒了", [(M, "exercise", "散步"), (M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r3-2-dog-walk", X, "媽媽帶狗去散步了", [(M, "exercise", "散步")]),
    _case("r3-2-grandchild-walk", M, "早上帶孫子去公園散步了", [(M, "exercise", "散步")]),
    _case("r3-2-hike-with-friend", F, "早上跟朋友去爬山了", [(F, "exercise", "爬山")]),
    # 第三輪 #3：以人字結尾的關係名詞是別人（其餘關係名詞見 test_relation_nouns_are_someone_else）
    _case("r3-3-senior-sister", F, "我師姐昏倒了"),
    _case("r3-3-in-law", M, "我親家母跌倒了"),
    _case("r3-3-grandson", F, "我外孫昏倒了"),
    _case("r3-3-relatives", M, "親戚昏倒了"),
    _case("r3-3-uncle", F, "大伯昏倒了"),
    _case("r3-3-senior-sister-far", M, "我師姐今天在醫院昏倒了"),  # 名詞和緊急字詞隔得遠也認得
    _case("r3-3-grandson-far", F, "我外孫昨天在學校跌倒了"),
    # 第三輪 #4：痛／暈／滑／喘到…是真的；忙／氣／笑／熱到、快／要…才是程度補語（那幾句在上面）
    _case("r3-4-pain-passed-out", X, "媽媽痛到直接昏過去了", [(M, "body", "昏倒", "", R)], [(M, "昏倒", True, False, False)]),
    _case("r3-4-pain-passed-out-self", F, "我剛剛痛到昏過去", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r3-4-slipped", F, "剛剛滑到跌倒", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r3-4-dizzy-fell", X, "爸爸剛剛暈到跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r3-4-breathless-speechless", X, "爸爸剛剛喘到說不出話", [(F, "body", "嘴歪或說話不清", "", R)], [(F, "嘴歪或說話不清", True, False, False)]),
    _case("r3-4-dizzy-then-fell", F, "剛剛頭暈到直接跌倒", [(F, "body", "頭暈"), (F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    # 第三輪 #5：麻煩不是麻
    _case("r3-5-left-trouble", M, "左邊很麻煩"),
    _case("r3-5-right-hand-trouble", F, "右手好麻煩"),
    _case("r3-5-right-again", M, "右邊又麻煩了"),
    # 第三輪 #6：未來、勸告詞只管後面的事，中間有「的時候／時／就／結果」也管不到
    _case("r3-6-on-the-way", X, "爸爸剛剛要去廁所的時候跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r3-6-getting-ready", X, "媽媽準備出門的時候跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r3-6-later-doctor", F, "我剛剛跌倒了等一下去看醫生", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r3-6-careful-next-time", X, "爸爸剛剛跌倒了下次要小心", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r3-6-about-to-go", F, "我剛剛要去廁所就跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r3-6-fell-then-admitted", X, "爸爸跌倒了明天要住院", [(F, "body", "跌倒", "", R)],
          [(F, "跌倒", True, False, False), (F, "住院", True, False, True)]),
    _case("r3-6-admitted-soon", X, "爸爸明天就要住院了", [], [(F, "住院", True, False, True)]),
    # 第三輪 #7：轉述跨逗號、空白、引號
    _case("r3-7-phoned-comma", X, "爸爸剛剛打電話來，他說胸口很痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r3-7-called-space", X, "媽媽剛剛打給我 她跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r3-7-said-comma", X, "爸爸早上說，他胸口悶", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r3-7-quote", X, "媽媽說：我胸口好悶", [(M, "body", "胸痛胸悶", "", R)], [(M, "胸痛胸悶", True, False, False)]),
    _case("r3-7-quote-space", X, "媽媽說： 我胸口好悶", [(M, "body", "胸痛胸悶", "", R)], [(M, "胸痛胸悶", True, False, False)]),
    _case("r3-7-called-the-aunt", X, "媽媽打電話給阿姨，她跌倒了"),  # 她可能是阿姨：有別的聽話者就不帶過去
    _case("r3-7-told-the-aunt", X, "媽媽跟阿姨說她頭暈", [(M, "body", "頭暈")]),  # 她＝說話的媽媽
    # 第三輪 #8：用藥
    _case("r3-8-habit-self", F, "我有在吃降血壓藥"),
    _case("r3-8-habit-mom", X, "媽媽有在吃降血壓藥"),
    _case("r3-8-herbal-chicken", F, "吃了中藥燉雞"),
    _case("r3-8-herb-shop", F, "中藥行"),
    _case("r3-8-explosive", F, "吃了炸藥"),
    _case("r3-8-one-pill", F, "吃了一顆降血壓藥", [(F, "med", "吃藥")]),
    # 第三輪 #9
    _case("r3-9-tight-clothes", F, "這件衣服胸部很緊"),
    _case("r3-9-pain-gone", F, "胸口痛好了", [(F, "body", "胸痛胸悶")], [(F, "胸痛胸悶", False, False, False)]),
    _case("r3-9-pain-passed", F, "胸口痛了一下就好了", [(F, "body", "胸痛胸悶")], [(F, "胸痛胸悶", False, False, False)]),
    _case("r3-9-parents-they", X, "爸媽他們今天去爬山了", [(F, "exercise", "爬山"), (M, "exercise", "爬山")]),
    # 第三輪的防誤傷：受詞是爸媽時照算、讀數認受詞、「我工作」不是人、看到別人跌倒
    _case("r3-guard-took-mom", X, "我陪媽媽去回診了", [(M, "visit", "回診")]),
    _case("r3-guard-took-dad", X, "我帶爸爸去看醫生了", [(F, "visit", "看醫生")]),
    _case("r3-guard-drove-mom", X, "我載媽媽去復健了", [(M, "visit", "復健")]),
    _case("r3-guard-at-work", F, "我工作的時候昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r3-guard-waiting", F, "我等孫子回來的時候跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r3-guard-myself", F, "我自己跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r3-guard-ladder", F, "我從梯子摔下來", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r3-guard-all-of-a-sudden", F, "一下子就昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r3-guard-saw-card-friend", F, "看到牌友昏倒了"),
    _case("r3-guard-saw-grandson", X, "爸爸看到外孫跌倒了"),
    _case("r3-guard-saw-senior-sister", M, "剛剛看到師姐跌倒"),
    # 第三輪：反過來的轉述（我打給爸爸，他說…）；聽話者不是爸媽、性別不合、還有別人都不帶
    _case("r3-reverse-called-dad", X, "我剛剛打給爸爸，他說胸口很痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r3-reverse-called-your-dad", M, "我剛剛打給你爸，他說胸口很痛", [(F, "body", "胸痛胸悶", "", R)],
          [(F, "胸痛胸悶", True, False, False)]),
    _case("r3-reverse-asked-mom", X, "我問媽媽，她說剛剛跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r3-reverse-implicit-caller", X, "剛剛打給媽媽，她說跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r3-reverse-wrong-gender", X, "我打給爸爸，她說胸口很痛"),
    _case("r3-reverse-not-a-parent", X, "我打給阿姨，她說胸口很痛"),
    _case("r3-reverse-two-parties", X, "我打給爸爸和阿姨，他說胸口很痛"),
    # 第三輪：稱謂不會被當成以人字結尾的名詞（父母、丈夫、妻子）
    _case("r3-title-my-parents", X, "我父母今天去爬山了", [(F, "exercise", "爬山"), (M, "exercise", "爬山")]),
    _case("r3-title-my-parents-de", X, "我的父母今天去散步了", [(F, "exercise", "散步"), (M, "exercise", "散步")]),
    _case("r3-title-husband", M, "我丈夫昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r3-title-wife", F, "我妻子昏倒了", [(M, "body", "昏倒", "", R)], [(M, "昏倒", True, False, False)]),
    _case("r3-title-husband-start", M, "丈夫剛剛昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r3-title-my-de-husband", M, "我的老公剛剛昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r3-title-siblings-husband", X, "我丈夫昏倒了"),  # 兄弟姊妹的先生
    # ── 第四輪審查（實作後）#1：以人字結尾的一般詞（工作、院子、公車）不是人 ──
    _case("r4-1-work", F, "我下午工作的時候昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r4-1-yard", F, "我在院子跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r4-1-off-the-bus", M, "我下公車的時候跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r4-1-on-the-bus", X, "爸爸今天出門，搭公車的時候跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r4-1-assembly", F, "我在里民大會昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r4-1-bought-then-fell", X, "爸爸買東西摔倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r4-1-bus-to-clinic", M, "我搭公車去回診了", [(M, "visit", "回診")]),
    # 名詞前面是地點或「吃了」：是地方、東西（清單外的詞也一樣）
    _case("r4-1-shed", F, "我在棚子跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r4-1-ate-pomelo", F, "我吃了柚子就胸口痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r4-1-phone-fell", F, "手機摔下來了"),  # 摔的是手機
    _case("r4-1-volunteer", F, "社區志工在院子跌倒了"),
    _case("r4-1-saw-card-friend-yard", X, "爸爸看到牌友在院子跌倒了"),
    _case("r4-1-saw-card-friend-office", X, "爸爸看到牌友在公司跌倒了"),
    # 第四輪 #2：轉述裡有別人時，不把轉述帶到下一句
    _case("r4-2-friend-had-stroke", X, "爸爸說朋友中風了，他說話不清楚"),
    _case("r4-2-aunt-fell", X, "媽媽打來說阿姨跌倒了，她現在胸口很痛"),
    _case("r4-2-tired-then-fell", X, "爸爸說今天很累，他剛剛跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r4-2-fell-then-chest", X, "媽媽打電話說她跌倒了，她現在胸口很痛",
          [(M, "body", "跌倒", "", R), (M, "body", "胸痛胸悶", "", R)],
          [(M, "跌倒", True, False, False), (M, "胸痛胸悶", True, False, False)]),
    # 第四輪 #3：同伴、教會／聚會、看完醫生、一個人
    _case("r4-3-dinner-with-colleague", F, "我今天跟同事吃完飯就胸口痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r4-3-hike-with-friend-fell", X, "爸爸今天跟朋友去爬山跌倒了", [(F, "exercise", "爬山"), (F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r4-3-walk-with-friend", X, "爸爸在公園跟朋友散步了", [(F, "exercise", "散步")]),
    _case("r4-3-church", M, "我在教會跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r4-3-gathering", M, "我去參加聚會跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r4-3-church-family", X, "媽媽在教會昏倒了", [(M, "body", "昏倒", "", R)], [(M, "昏倒", True, False, False)]),
    _case("r4-3-alone-dad", F, "一個人在家跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r4-3-alone-mom", M, "一個人在家跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r4-3-old-classmates", F, "我和老同學聚餐的時候昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r4-3-after-the-doctor", F, "我看完醫生回家就昏倒了", [(F, "visit", "看醫生"), (F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r4-3-reading-with-friend", F, "跟朋友量血壓 135/85", [(F, "body", "血壓", "135/85")]),  # 同伴不是量血壓的人
    # 「會」：去的地方（教會、聚會、同學會、家長會）不是傾向；「運動會喘」「喝酒會胸悶」「早晚會中風」還是習慣／傾向
    _case("r4-3-reunion", X, "媽媽去同學會跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r4-3-parents-meeting", F, "我去參加家長會昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r4-3-habit-exercise", F, "我運動會喘不過氣"),
    _case("r4-3-habit-drinking", M, "喝酒會胸悶"),
    _case("r4-3-habit-sooner-or-later", F, "我這樣早晚會中風"),
    _case("r4-3-habit-old-age", M, "人到老年會跌倒"),
    _case("r4-3-habit-flying", F, "坐飛機會頭暈"),
    # 轉述後面的「我」是聽話的人，不是別人：照樣帶到下一句
    _case("r4-2-listener-me", X, "媽媽說我今天不用回家，她剛剛跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    # 第四輪 #4：被陪、被帶的爸媽不是身體緊急字詞的主人；看診、送醫照算
    _case("r4-4-aunt-with-mom", X, "阿姨陪媽媽散步的時候跌倒了"),
    _case("r4-4-me-with-mom", X, "我陪媽媽散步的時候跌倒了"),
    _case("r4-4-er-with-dad", X, "我陪爸爸去急診", [], [(F, "急診", True, True, False)]),
    _case("r4-4-clinic-with-mom", X, "我陪媽媽去回診了", [(M, "visit", "回診")]),
    _case("r4-4-dad-with-wife", F, "我陪我老婆散步的時候跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    # ── 第五輪審查（實作後）#1：三字職稱（含動詞開頭）是別人，中間隔了地點也一樣 ──
    _case("r5-1-caretaker-relayed", X, "媽媽說管理員剛剛在大廳昏倒了"),
    _case("r5-1-cleaner-relayed", X, "爸爸說清潔工在門口跌倒了"),
    _case("r5-1-cleaner-dad", F, "清潔工在門口昏倒了"),
    _case("r5-1-cashier-relayed", X, "爸爸說收銀員昏倒了"),
    _case("r5-1-cashier", F, "收銀員在門口昏倒了"),
    _case("r5-1-cashier-bare", F, "收銀員昏倒了"),
    _case("r5-1-toll-collector", F, "收費員在門口昏倒了"),
    _case("r5-1-salesman", F, "推銷員在門口昏倒了"),
    _case("r5-1-masseur", F, "推拿師在門口昏倒了"),
    _case("r5-1-courier", F, "送貨員在門口昏倒了"),
    _case("r5-1-porter", F, "搬運工在門口昏倒了"),
    _case("r5-1-dishwasher", M, "洗碗工在廚房昏倒了"),
    _case("r5-1-porter-adverb", F, "搬運工剛剛跌倒了"),
    _case("r5-1-cashier-gap-relayed", X, "爸爸說收銀員剛剛在大廳昏倒了"),
    _case("r5-1-masseur-gap-relayed", X, "爸爸說推拿師剛剛在大廳昏倒了"),
    _case("r5-1-dishwasher-gap-relayed", X, "爸爸說洗碗工剛剛在大廳昏倒了"),
    _case("r5-1-saw-toll-collector", M, "我看到收費員昏倒了"),
    _case("r5-1-my-masseur", F, "我的推拿師中風了"),
    _case("r5-1-store-cashier", X, "媽媽說全聯的收銀員昏倒了"),
    _case("r5-1-accountant", F, "會計師剛剛在門口跌倒了"),
    _case("r5-1-care-worker", F, "照服員在門口昏倒了"),
    _case("r5-1-building-caretaker", F, "大樓管理員剛剛在大廳昏倒了"),
    _case("r5-1-store-cashier-no-de", F, "全聯收銀員昏倒了"),
    _case("r5-1-building-toll-collector", M, "大樓收費員剛剛跌倒了"),
    # 收工、做完工、打完工、下完工：做完的事；看醫師、叫護士、送老師、去打工：動詞＋人／事，都還是說話者自己
    _case("r5-1-off-work-comma", F, "我收工了，回家就跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-done-working", F, "我做完工就跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-done-part-time", F, "我打完工就跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-off-shift", F, "我下完工就昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r5-1-done-opening", F, "我開完工就胸口痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r5-1-finished-work-mom", M, "我收完工就跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-1-hard-labor", F, "我做苦工，回家就跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-can-work", F, "我會打工，剛剛跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-drove-teacher", F, "我送老師回家就跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-part-time-job", F, "我去打工的時候跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-odd-jobs", F, "我打零工的時候跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-processing", F, "我在加工的時候昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r5-1-seeing-doctor", F, "我看醫師的時候昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r5-1-waiting-doctor", F, "我等醫師的時候昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r5-1-calling-nurse", M, "我叫護士的時候跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-1-dishwashing-work", F, "我在洗碗工作的時候昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r5-1-delivering", F, "我在送貨的時候跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-at-the-till", M, "我在收銀的時候跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-1-done-working-bare", F, "做完工就跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-volunteering-when", F, "我去當義工的時候跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-asking-doctor", F, "我問醫師的時候昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r5-1-finding-doctor", F, "我找醫師的時候昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r5-1-called-nurse", M, "我叫護士就跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-1-picking-up-master", F, "我接師父的時候跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-after-the-doctor", F, "看完醫師就昏倒了", [(F, "body", "昏倒", "", R), (F, "visit", "看醫生")],
          [(F, "昏倒", True, False, False)]),
    _case("r5-1-closed-stall", F, "我收完攤就跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-hard-labor-when", F, "我做苦工的時候跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-washing-dishes", M, "我洗碗就跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-1-at-till-then", F, "我收銀就跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-after-massage", F, "我推拿完就胸口痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r5-1-wheelchair", F, "我推輪椅的時候跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-lunchbox", M, "我送便當的時候跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-1-furniture", F, "我搬家具的時候跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-1-laundry", M, "我收衣服的時候跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-1-labor", F, "我在做工的時候昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r5-1-to-work", F, "我去上工的時候昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r5-1-at-part-time", F, "我在打工的時候昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    # ── 第五輪 #2：一個人只有在家／去／住／待前面是獨自；看到、遇到、發現後面的一個人是別人 ──
    _case("r5-2-on-the-road", X, "媽媽說一個人在路上昏倒了"),
    _case("r5-2-saw-at-station", X, "爸爸看到一個人在捷運站昏倒了"),
    _case("r5-2-saw-at-park", F, "我看到一個人在公園昏倒了"),
    _case("r5-2-met-on-road", X, "爸爸遇到一個人在路上昏倒了"),
    _case("r5-2-found-at-home", X, "媽媽發現一個人在家昏倒了"),
    _case("r5-2-heard-at-home", X, "爸爸聽到一個人在家昏倒了"),
    _case("r5-2-outside", F, "一個人在外面跌倒了"),
    _case("r5-2-alone-relayed", X, "爸爸說一個人在家跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-2-alone-me", F, "我一個人在家跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-2-alone-hiking", F, "一個人去爬山跌倒了", [(F, "body", "跌倒", "", R), (F, "exercise", "爬山")],
          [(F, "跌倒", True, False, False)]),
    _case("r5-2-alone-walk", X, "爸爸一個人去散步跌倒了", [(F, "body", "跌倒", "", R), (F, "exercise", "散步")],
          [(F, "跌倒", True, False, False)]),
    # ── 第五輪 #3：地點名稱裡的機構詞是地方（醫院長廊、遊客中心、里長辦公室）；人照樣是人 ──
    _case("r5-3-hospital-corridor", F, "我剛剛在醫院長廊跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-3-hospital-corridor-mom", M, "我剛剛在醫院長廊跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-3-visitor-center", M, "我在遊客中心胸口好痛", [(M, "body", "胸痛胸悶", "", R)], [(M, "胸痛胸悶", True, False, False)]),
    _case("r5-3-chief-office", F, "我在里長辦公室跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-3-family-lounge", F, "我在家屬休息室跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-3-volunteer-station", M, "我在志工站跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-3-principal-office", F, "我在校長室昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r5-3-school-corridor", M, "我在學校長廊跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-3-hospital-bench", F, "我剛才在醫院長椅上跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-3-social-worker-room", F, "我去社工室的時候昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r5-3-client-office", F, "我在客戶處昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r5-3-residents-meeting", F, "我在住戶大會跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-3-temple", F, "我在伯公廟跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-3-mom-stood-up", X, "媽媽站起來就跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-3-sister-stood-up", F, "我姊站起來的時候跌倒了"),
    _case("r5-3-volunteer-stood-up", F, "志工站起來就跌倒了"),
    _case("r5-3-saw-volunteer-standing", X, "爸爸看到志工站在門口昏倒了"),
    _case("r5-3-met-office-person", X, "媽媽遇到里長辦公室的人昏倒了"),
    _case("r5-3-dean", F, "院長在門口昏倒了"),
    _case("r5-3-university-president", F, "大學校長在門口昏倒了"),
    _case("r5-3-chief-relayed", X, "爸爸說里長昏倒了"),
    # ── 第五輪 #4：跟／和／與後面整個片語都是同伴（隔壁阿姨、王媽媽、一個朋友、公司同事、以前的同事、教會的姊妹、一群朋友）──
    _case("r5-4-neighbor-aunt-walk", F, "我今天跟隔壁阿姨去散步跌倒了", [(F, "body", "跌倒", "", R), (F, "exercise", "散步")],
          [(F, "跌倒", True, False, False)]),
    _case("r5-4-neighbor-aunt-dance", M, "我跟隔壁阿姨去跳舞的時候跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-4-neighbor-aunt-meal", F, "我跟隔壁阿姨吃飯就胸口好痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r5-4-mrs-wang-walk", M, "我今天跟王媽媽去散步跌倒了", [(M, "body", "跌倒", "", R), (M, "exercise", "散步")],
          [(M, "跌倒", True, False, False)]),
    _case("r5-4-mrs-wang-dance-dad", F, "我跟王媽媽去跳舞的時候跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-4-mrs-wang-dance-mom", M, "我跟王媽媽去跳舞的時候跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-4-mrs-wang-meal", M, "我跟王媽媽吃飯就胸口好痛", [(M, "body", "胸痛胸悶", "", R)], [(M, "胸痛胸悶", True, False, False)]),
    _case("r5-4-a-friend-walk", F, "我今天跟一個朋友去散步跌倒了", [(F, "body", "跌倒", "", R), (F, "exercise", "散步")],
          [(F, "跌倒", True, False, False)]),
    _case("r5-4-a-friend-dance", M, "我跟一個朋友去跳舞的時候跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-4-a-friend-meal", F, "我跟一個朋友吃飯就胸口好痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r5-4-coworker-walk", F, "我今天跟公司同事去散步跌倒了", [(F, "body", "跌倒", "", R), (F, "exercise", "散步")],
          [(F, "跌倒", True, False, False)]),
    _case("r5-4-coworker-dance", M, "我跟公司同事去跳舞的時候跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-4-coworker-meal", F, "我跟公司同事吃飯就胸口好痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r5-4-former-coworker-walk", F, "我今天跟以前的同事去散步跌倒了", [(F, "body", "跌倒", "", R), (F, "exercise", "散步")],
          [(F, "跌倒", True, False, False)]),
    _case("r5-4-former-coworker-dance", M, "我跟以前的同事去跳舞的時候跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-4-former-coworker-meal", F, "我跟以前的同事吃飯就胸口好痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r5-4-church-sister-walk", M, "我今天跟教會的姊妹去散步跌倒了", [(M, "body", "跌倒", "", R), (M, "exercise", "散步")],
          [(M, "跌倒", True, False, False)]),
    _case("r5-4-church-sister-dance", M, "我跟教會的姊妹去跳舞的時候跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-4-church-sister-meal", F, "我跟教會的姊妹吃飯就胸口好痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r5-4-group-walk", F, "我今天跟一群朋友去散步跌倒了", [(F, "body", "跌倒", "", R), (F, "exercise", "散步")],
          [(F, "跌倒", True, False, False)]),
    _case("r5-4-group-dance", M, "我跟一群朋友去跳舞的時候跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-4-group-meal", F, "我跟一群朋友吃飯就胸口好痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r5-4-several-friends", F, "我今天跟幾個朋友去散步跌倒了", [(F, "body", "跌倒", "", R), (F, "exercise", "散步")],
          [(F, "跌倒", True, False, False)]),
    _case("r5-4-neighbor-de-aunt", M, "我和隔壁的阿姨去散步跌倒了", [(M, "body", "跌倒", "", R), (M, "exercise", "散步")],
          [(M, "跌倒", True, False, False)]),
    _case("r5-4-former-company", F, "我跟以前公司的同事吃飯就胸口好悶", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r5-4-dad-neighbor-aunt", X, "爸爸今天跟隔壁阿姨去散步跌倒了", [(F, "body", "跌倒", "", R), (F, "exercise", "散步")],
          [(F, "跌倒", True, False, False)]),
    _case("r5-4-mom-mrs-wang", X, "媽媽跟王媽媽去跳舞的時候跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-4-dad-group", X, "爸爸跟一群朋友吃飯就胸口好痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r5-4-mom-church-then", X, "媽媽跟教會的姊妹去散步，結果跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-4-wife-neighbor", F, "我太太跟隔壁阿姨去散步跌倒了", [(M, "body", "跌倒", "", R), (M, "exercise", "散步")],
          [(M, "跌倒", True, False, False)]),
    _case("r5-4-he-with-neighbor", X, "爸爸說他跟隔壁阿姨去散步跌倒了", [(F, "body", "跌倒", "", R), (F, "exercise", "散步")],
          [(F, "跌倒", True, False, False)]),
    _case("r5-4-dance-halfway", M, "我跟王媽媽去跳舞，跳到一半昏倒了", [(M, "body", "昏倒", "", R)], [(M, "昏倒", True, False, False)]),
    # 同伴自己出事、片語外的別人、說話對象：都不是爸媽
    _case("r5-4-mrs-wang-fell", X, "王媽媽跌倒了"),
    _case("r5-4-my-sister-fell", M, "我姊跌倒了"),
    _case("r5-4-neighbor-aunt-fell", F, "隔壁阿姨跌倒了"),
    _case("r5-4-group-fell", F, "一群朋友跌倒了"),
    _case("r5-4-former-coworker-fell", F, "以前的同事昏倒了"),
    _case("r5-4-then-neighbor-fell", F, "我跟隔壁阿姨去散步，隔壁阿姨跌倒了"),
    _case("r5-4-then-mrs-wang-fainted", M, "我跟王媽媽去跳舞，王媽媽昏倒了"),
    _case("r5-4-then-one-friend-fell", F, "我跟一群朋友去爬山，一個朋友跌倒了"),
    _case("r5-4-sister-with-mrs-wang", X, "我姊跟王媽媽去散步跌倒了"),
    _case("r5-4-me-with-neighbor-family", X, "我跟隔壁阿姨去散步跌倒了"),
    _case("r5-4-friend-of-mom", X, "我跟媽媽的朋友去散步跌倒了"),
    _case("r5-4-aunt-with-neighbor", X, "阿姨跟隔壁的阿伯去散步跌倒了"),
    _case("r5-4-told-you-neighbor", F, "我跟你說隔壁阿姨跌倒了"),
    _case("r5-4-told-friend-mrs-wang", F, "我跟朋友說王媽媽跌倒了"),
    _case("r5-4-told-neighbor-he", F, "我跟隔壁阿姨說他跌倒了"),
    _case("r5-4-dad-told-me-neighbor", X, "爸爸跟我說隔壁阿姨跌倒了"),
    _case("r5-4-saw-coworker", X, "爸爸看到公司同事跌倒了"),
    # ── 第五輪 #5（併入）：「會」前面只有聚會名稱才是地方；在家會、去爬山會、在浴室會是傾向 ──
    _case("r5-5-home-tends", F, "我在家會跌倒"),
    _case("r5-5-dad-home-tends", X, "爸爸在家會喘不過氣"),
    _case("r5-5-hiking-tends", F, "我去爬山會喘不過氣"),
    _case("r5-5-bathroom-tends", M, "我在浴室會滑倒"),
    _case("r5-5-stairs-tends", M, "我在樓梯會跌倒"),
    _case("r5-5-outside-tends", F, "我在外面會頭暈"),
    _case("r5-5-park-walk-tends", F, "我去公園走會喘不過氣"),
    _case("r5-5-sports-day", F, "我去運動會跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r5-5-temple-fair", M, "我去廟會的時候跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-5-evening-party", F, "我在晚會昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r5-5-banquet", M, "我在餐會胸口好痛", [(M, "body", "胸痛胸悶", "", R)], [(M, "胸痛胸悶", True, False, False)]),
    _case("r5-5-annual-meeting", F, "我去參加年會的時候昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r5-5-assembly", F, "我在大會昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r5-5-elders-club", M, "我在老人會跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r5-5-parents-meeting", F, "我在家長會跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    # 以前／之前修飾人（以前的同事）不是很久以前；「以前爸爸跌倒了」「以前的事了，我中風過」還是以前
    _case("r5-5-former-old-coworker", F, "我跟以前公司的老同事吃飯就胸口好痛", [(F, "body", "胸痛胸悶", "", R)],
          [(F, "胸痛胸悶", True, False, False)]),
    _case("r5-5-previous-neighbor", M, "我跟之前的鄰居去散步跌倒了", [(M, "body", "跌倒", "", R), (M, "exercise", "散步")],
          [(M, "跌倒", True, False, False)]),
    _case("r5-5-former-then-fainted", F, "我跟以前上班的同事吃飯，結果昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r5-5-long-ago-dad", X, "以前爸爸跌倒了", [(F, "body", "跌倒")], [(F, "跌倒", False, False, False)]),
    _case("r5-5-long-ago-stroke", F, "以前的事了，我中風過", [(F, "body", "中風")], [(F, "中風", False, False, False)]),
    _case("r5-5-long-ago-matter", F, "這是以前的事，我中風了", [(F, "body", "中風")], [(F, "中風", False, False, False)]),
    _case("r5-5-old-ailment", X, "爸爸以前的老毛病，中風了", [(F, "body", "中風")], [(F, "中風", False, False, False)]),
    _case("r5-5-former-me", F, "以前的我中風了", [(F, "body", "中風")], [(F, "中風", False, False, False)]),
    _case("r5-5-previous-me", M, "之前的我跌倒了一次", [(M, "body", "跌倒")], [(M, "跌倒", False, False, False)]),
    _case("r5-5-former-her-relayed", X, "媽媽說以前的她跌倒了", [(M, "body", "跌倒")], [(M, "跌倒", False, False, False)]),
    # ── 第六輪：「一個人」照第五輪（後面接在家／去／住／待才是獨自）；看到、說、句首、地點後面的「一個人」都是別人 ──
    _case("r6-1-dad-says-alone-home", X, "爸爸說一個人在家跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r6-1-another-person", F, "另一個人昏倒了"),
    _case("r6-1-one-of-them", M, "其中一個人昏倒了"),
    _case("r6-1-on-the-road", F, "路上一個人昏倒了"),
    _case("r6-1-in-front", M, "前面一個人跌倒了"),
    _case("r6-1-mom-says-someone", X, "媽媽說一個人在路上昏倒了"),
    _case("r6-1-dad-saw-someone", X, "爸爸看到一個人在捷運站昏倒了"),
    _case("r6-1-i-saw-someone", F, "我看到一個人在公園昏倒了"),
    _case("r6-1-clause-start", M, "一個人在路上昏倒了"),
    _case("r6-1-mom-says-today-someone", X, "媽媽說今天一個人在路上昏倒了"),
    _case("r6-1-mom-says-midnight-someone", X, "媽媽說半夜一個人在路上昏倒了"),
    _case("r6-1-over-here", F, "我們這邊一個人跌倒了"),
    _case("r6-1-in-the-park", M, "公園裡一個人昏倒了"),
    # ── 第六輪 #2：清單外的「在／去／參加＋X會」也是地方（法會、農會、演唱會、讀書會、園遊會、舞會）；在家會、去爬山會、去跳舞會還是傾向 ──
    _case("r6-2-dharma-service", M, "我在法會跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r6-2-farmers-association", F, "我在農會跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r6-2-concert", F, "我剛剛在演唱會昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r6-2-book-club", M, "我在讀書會胸口好痛", [(M, "body", "胸痛胸悶", "", R)], [(M, "胸痛胸悶", True, False, False)]),
    _case("r6-2-attend-service", F, "我去參加法會跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r6-2-fair-relayed", X, "爸爸在園遊會跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r6-2-dance-party", F, "我在舞會跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r6-2-habit-home", F, "我在家會跌倒"),
    _case("r6-2-habit-hiking", F, "我去爬山會喘不過氣"),
    _case("r6-2-habit-dancing", M, "我去跳舞會喘不過氣"),
    _case("r6-2-habit-church", F, "我在教會會跌倒"),
    _case("r6-2-habit-book-club", F, "我在讀書會會跌倒"),
    _case("r6-2-fellowship", F, "我在聯誼會胸口好痛", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("r6-2-lantern-festival", M, "我剛剛在燈會跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r6-2-clan-meeting", F, "我在宗親會昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r6-2-prayer-meeting", M, "我在禱告會胸口好痛", [(M, "body", "胸痛胸悶", "", R)], [(M, "胸痛胸悶", True, False, False)]),
    _case("r6-2-morning-meeting", F, "我今天在早會昏倒了", [(F, "body", "昏倒", "", R)], [(F, "昏倒", True, False, False)]),
    _case("r6-2-class-party", X, "媽媽在同樂會跌倒了", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("r6-2-get-together", F, "我去參加聯歡會跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r6-2-banquet", M, "我在宴會昏倒了", [(M, "body", "昏倒", "", R)], [(M, "昏倒", True, False, False)]),
    _case("r6-2-birthday-party", X, "爸爸在生日會跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    # ── 第六輪 GP2：「在／去＋時間、動作、一般地點＋會」是傾向，不是聚會（不回 119）──
    _case("r6-3-habit-midnight", F, "我在半夜會胸痛"),
    _case("r6-3-habit-winter", M, "我在冬天會胸悶"),
    _case("r6-3-habit-work", F, "我去上班會胸悶"),
    _case("r6-3-habit-hospital", F, "我在醫院會昏倒"),
    _case("r6-3-habit-station", F, "我在捷運站會昏倒"),
    _case("r6-3-habit-bath", M, "我在洗澡會喘不過氣"),
    _case("r6-3-habit-slope", F, "我在爬坡會喘不過氣"),
    _case("r6-3-habit-lifting", F, "我在提重物會胸痛"),
    _case("r6-3-habit-cold", M, "我在天氣冷會胸悶"),
    _case("r6-3-habit-dawn", F, "我在凌晨會胸悶"),
    _case("r6-3-habit-mom-midnight", X, "媽媽在半夜會胸痛"),
    _case("r6-3-habit-evening", F, "我在晚上會胸悶"),
    _case("r6-3-habit-office", F, "我在公司會跌倒"),
    _case("r6-3-habit-go-hospital", M, "我去醫院會昏倒"),
    _case("r6-3-habit-moving", F, "我在搬東西會胸痛"),
    _case("r6-3-habit-dad-bedtime", X, "爸爸在睡前會胸悶"),
    _case("r6-3-habit-after-meals", F, "我在飯後會胸悶"),
    _case("r6-3-habit-weather", M, "我在變天會胸悶"),
    _case("r6-3-habit-standing-up", F, "我在站起來會昏倒"),
    _case("r6-3-habit-after-exercise", M, "我在運動完會喘不過氣", [(M, "exercise", "運動")]),
    _case("r6-3-habit-chair", F, "我從椅子會跌倒"),
    _case("r6-3-habit-straining", M, "我在用力會胸痛"),
    _case("r6-3-habit-agitated", F, "我在激動會胸悶"),
    _case("r6-3-habit-drinking", F, "我去喝酒會胸悶"),
    _case("r6-3-habit-tea", M, "我在喝茶會胸悶"),
    # ── 第六輪 GP2：轉述裡的「一個人」、別人、地點裡的人；說／講後面一兩個時間詞再接「一個人」是別人 ──
    _case("r6-4-friend-at-service", X, "爸爸說朋友在法會跌倒了"),
    _case("r6-4-neighbor-alone", X, "媽媽說隔壁一個人在浴室跌倒了"),
    _case("r6-4-neighbors-person", X, "媽媽說隔壁的一個人在浴室跌倒了"),
    _case("r6-4-someone-at-service", X, "爸爸說法會上一個人跌倒了"),
    _case("r6-4-someone-there", X, "爸爸說法會有一個人跌倒了"),
    _case("r6-4-neighbor-at-fair", X, "爸爸說鄰居在園遊會跌倒了"),
    _case("r6-4-aunt-at-service", X, "爸爸說阿姨在法會昏倒了"),
    _case("r6-4-dad-saw-at-service", X, "爸爸在法會看到一個人跌倒了"),
    _case("r6-4-i-saw-at-service", F, "我剛剛在法會看到一個人昏倒了"),
    _case("r6-4-at-service-someone", M, "法會上剛剛一個人昏倒了"),
    _case("r6-4-church-someone", F, "教會裡有一個人昏倒了"),
    _case("r6-4-uncle-at-temple-fair", M, "隔壁阿伯在廟會跌倒了"),
    _case("r6-4-last-night-relay", X, "媽媽說昨晚一個人在浴室跌倒了"),
    _case("r6-4-two-time-words", X, "媽媽說昨天半夜一個人在浴室跌倒了"),
    _case("r6-4-this-morning-relay", X, "爸爸說今天早上一個人在路上昏倒了"),
    _case("r6-4-few-days-ago", X, "爸爸說前幾天一個人在公園跌倒了"),
    _case("r6-4-morning-just-now", X, "媽媽說早上剛剛一個人在浴室跌倒了"),
    _case("r6-4-downstairs", X, "媽媽說樓下剛剛一個人在電梯昏倒了"),
    _case("r6-4-someone-alone", X, "爸爸說有人今天一個人在路上昏倒了"),
    _case("r6-4-heard-say", F, "我聽說今天早上一個人在路上昏倒了"),
    _case("r6-4-news", F, "新聞說昨天晚上一個人在浴室跌倒了"),
    _case("r6-4-our-community", F, "我們社區剛剛一個人在路上昏倒了"),
    _case("r6-4-park-someone", M, "公園剛剛一個人昏倒了"),
    _case("r6-4-someone-at-service-2", F, "剛剛有一個人在法會跌倒了"),
    _case("r6-4-friend-says", F, "我朋友說剛剛一個人在法會昏倒了"),
    _case("r6-4-coworker-says", M, "我同事說今天早上一個人在公司昏倒了"),
    _case("r6-4-friends-classmate", X, "爸爸說他朋友的老同學昏倒了"),
    _case("r6-4-climbing-he-fainted", F, "我跟朋友的老同學去爬山，他昏倒了"),
    _case("r6-4-dad-with-classmate", X, "爸爸跟朋友的老同學在法會跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    # ── 第六輪：沒有主詞、時間詞開頭的「一個人」照第五輪算別人（不知道是誰；「我在看電視，昨天一個人…」就是旁人）──
    _case("r6-5-dad-this-morning", F, "今天早上一個人在路上昏倒了"),
    _case("r6-5-dad-at-service", F, "剛剛一個人在法會跌倒了"),
    _case("r6-5-mom-last-midnight", M, "昨天半夜一個人在浴室跌倒了"),
    # ── 第六輪：跟／和＋修飾語＋同伴（隔壁王媽媽、隔壁的王媽媽、隔壁林阿姨）整個片語都是同伴 ──
    _case("r6-6-neighbor-wang", F, "我跟隔壁王媽媽去散步的時候跌倒了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("r6-6-neighbors-wang-today", F, "我今天跟隔壁的王媽媽去散步的時候跌倒了", [(F, "body", "跌倒", "", R)],
          [(F, "跌倒", True, False, False)]),
    _case("r6-6-neighbor-lin", M, "我和隔壁林阿姨吃飯，結果胸口好痛", [(M, "body", "胸痛胸悶", "", R)], [(M, "胸痛胸悶", True, False, False)]),
    # ── 第六輪 GP1：地點開頭、轉述（說、說，、告訴我）、電視新聞裡的「一個人」是別人 ──
    _case("r6-7-place-first", M, "公園剛剛一個人在涼亭昏倒了"),
    _case("r6-7-relay-that-day", X, "媽媽說那天一個人在路上跌倒了"),
    _case("r6-7-relay-event", X, "爸爸說法會一個人在大廳昏倒了"),
    _case("r6-7-tv", F, "電視播昨天一個人在捷運站昏倒了"),
    _case("r6-7-relay-comma", X, "媽媽說，今天一個人在路上昏倒了"),
    _case("r6-7-told-me", X, "媽媽告訴我今天一個人在路上昏倒了"),
    _case("r6-7-i-tell-you", F, "我跟你說，今天一個人在浴室跌倒了"),
    # ── 第六輪 GP2：前一句是情境、後一句是時間詞＋一個人，那是旁人 ──
    _case("r6-7-watching-tv", F, "我在看電視，昨天一個人在捷運站昏倒了"),
    _case("r6-7-walking-park", F, "我去公園散步，剛剛一個人在涼亭昏倒了"),
    _case("r6-7-waiting-bus", M, "我在等公車，剛剛一個人在站牌昏倒了"),
    _case("r6-7-mom-in-park", X, "媽媽在公園散步，好像一個人在涼亭昏倒了"),
    # ── 10/17 卡片拿掉「我吃藥了／我有運動」（Andrew：「盡量自動化，從他們留言獲取資訊」）：多抓幾種說法 ──
    _case("more-med-you-chi", F, "藥有吃", [(F, "med", "吃藥")]),
    _case("more-med-today", M, "今天藥有吃", [(M, "med", "吃藥")]),
    _case("more-med-dou-you", F, "藥都有吃", [(F, "med", "吃藥")]),
    _case("more-med-morning", M, "早上的藥有吃了", [(M, "med", "吃藥")]),
    _case("more-med-on-time", F, "藥有按時吃", [(F, "med", "吃藥")]),
    _case("more-med-on-time-le", M, "藥按時吃了", [(M, "med", "吃藥")]),
    _case("more-med-relay", X, "爸爸藥有吃", [(F, "med", "吃藥")]),
    _case("more-med-only-me", F, "我藥有吃，媽媽還沒", [(F, "med", "吃藥")]),
    _case("more-med-question", F, "藥有吃嗎"),
    _case("more-med-relay-question", X, "爸爸藥有吃嗎"),
    _case("more-med-a-not-a", F, "藥有沒有吃"),
    _case("more-med-negated", F, "藥沒有吃"),
    _case("more-med-habit", F, "藥有在吃"),  # 平常在吃，不是今天吃了
    _case("more-med-remind", F, "記得藥有吃"),
    _case("more-med-make-sure", F, "要確定藥有吃"),
    _case("more-med-check", F, "要確認藥有吃"),
    _case("more-walk-park", F, "去公園走走了", [(F, "exercise", "散步")]),
    _case("more-walk-yi", M, "出去走一走了", [(M, "exercise", "散步")]),
    _case("more-walk-back", F, "走走回來", [(F, "exercise", "散步")]),
    _case("more-walk-le-yi", M, "在公園走了一走", [(M, "exercise", "散步")]),
    _case("more-walk-relay", X, "媽媽去公園走走了", [(M, "exercise", "散步")]),
    _case("more-walk-both", X, "爸媽都有出去走走了", [(F, "exercise", "散步"), (M, "exercise", "散步")]),
    _case("more-walk-plan", F, "等一下出去走走"),
    _case("more-walk-free", F, "有空出去走走"),
    _case("more-walk-free-relay", X, "爸爸有空會去走走"),
    _case("more-walk-traffic", F, "走走停停"),
    _case("more-walk-try", F, "走走看"),
    _case("more-walk-lets-go", F, "走走走去吃飯"),
    _case("more-walk-chant", F, "走走走去吃飯了"),  # 「走走走」是招呼，不是散步（同一個子句裡有「了」）
    _case("more-walk-stop-go", F, "塞車走走停停了好久"),
    _case("more-walk-said-will", X, "媽媽說要出去走走"),
    _case("more-walk-dog", F, "狗出去走走了"),
    _case("more-qigong", F, "練完氣功", [(F, "exercise", "氣功")]),
    _case("more-baduanjin", M, "八段錦打完了", [(M, "exercise", "氣功")]),
    _case("more-qigong-aircon", F, "冷氣功能修好了"),  # 「冷氣功能」不是氣功（GP1 I-3）
    _case("more-morning-run", F, "晨跑回來", [(F, "exercise", "跑步")]),
    _case("more-night-run", M, "夜跑完了", [(M, "exercise", "跑步")]),
    _case("more-run-shoes", F, "買了晨跑鞋"),
    _case("more-swim-split", F, "游完泳", [(F, "exercise", "游泳")]),
    _case("more-swim-before", F, "以前游過泳"),  # 「游過泳」多半是講經驗（GP1 N-1）
    _case("more-morning-exercise-topic", M, "早操做完了", [(M, "exercise", "做操")]),
    _case("more-morning-exercise-cancelled", F, "早操取消了"),
    _case("more-morning-exercise-time", M, "晨操時間改了"),
    # 「有空」不是做完：原本會誤記
    _case("more-free-walk", F, "有空去散步"),
    _case("more-free-hike", F, "有空再去爬山"),
    _case("more-free-then-swam", M, "今天有空，去游泳了", [(M, "exercise", "游泳")]),
    # ── GP1 事前審查（多抓說法）找到的誤抓與漏記 ──
    # I-1 否定詞被「出去」擠出視窗
    _case("gp1m-not-out-for-long", F, "好久沒有出去走走了"),
    _case("gp1m-too-busy", F, "最近太忙沒空出去走走了"),
    _case("gp1m-no-time", F, "沒時間出去走走了"),
    _case("gp1m-dont-want", F, "今天不想出去走走了"),
    _case("gp1m-rain-cannot", F, "下雨不能出去走走了"),
    _case("gp1m-rain-no-way", F, "下雨沒辦法出去走走了"),
    # I-2 半夜、清晨「跑」不是跑步
    _case("gp1m-night-toilet", F, "昨晚半夜跑了三次廁所"),
    _case("gp1m-night-toilet-2", F, "我半夜跑了好幾趟廁所"),
    _case("gp1m-night-er", F, "半夜跑去急診了", [(F, "visit", "急診")], [(F, "急診", True, True, False)]),
    _case("gp1m-late-store", F, "昨天深夜跑去超商"),
    _case("gp1m-dawn-park", F, "清晨跑去公園了"),
    _case("gp1m-morning-market", F, "早晨跑去買菜了"),
    _case("gp1m-morning-run-step", F, "早晨跑步了", [(F, "exercise", "跑步")]),
    _case("gp1m-night-run-back", F, "夜跑回來", [(F, "exercise", "跑步")]),
    _case("gp1m-this-morning-run", F, "今早晨跑完了", [(F, "exercise", "跑步")]),
    _case("gp1m-early-run", F, "一早晨跑回來", [(F, "exercise", "跑步")]),
    _case("gp1m-run-went", X, "爸爸晨跑去了", [(F, "exercise", "跑步")]),
    # I-3 家電的「氣功能」
    _case("gp1m-aircon-broken", F, "冷氣功能有問題"),
    _case("gp1m-heater", F, "暖氣功能壞了"),
    _case("gp1m-steam", F, "烤箱有蒸氣功能"),
    _case("gp1m-both-air", F, "冷暖氣功能都有"),
    # I-4 台式問句尾：家人催吃藥不是吃了
    _case("gp1m-tag-hou", X, "爸爸藥有吃齁"),
    _case("gp1m-tag-hou-comma", X, "爸爸，藥有吃齁"),
    _case("gp1m-tag-bopomofo", X, "爸，藥有吃ㄇ"),
    _case("gp1m-tag-wife-asks", M, "老公，藥有吃齁"),
    # I-5 叫人確認，中間夾字
    _case("gp1m-make-sure-dad", X, "要確定爸爸藥有吃"),
    _case("gp1m-make-sure-mom", X, "你要確定媽媽藥有吃"),
    _case("gp1m-check-once", F, "要確認一下藥有吃"),
    _case("gp1m-check-first", F, "要先確定藥有吃"),
    _case("gp1m-must-check", F, "必須確定藥有吃"),
    _case("gp1m-i-am-sure", F, "我確定藥有吃了", [(F, "med", "吃藥")]),
    _case("gp1m-sure", F, "確定藥有吃了", [(F, "med", "吃藥")]),
    # I-6 「有空腹」照樣是做了
    _case("gp1m-fasting-med", F, "我有空腹吃藥", [(F, "med", "吃藥")]),
    _case("gp1m-fasting-blood", F, "今天有空腹去抽血", [(F, "visit", "抽血")]),
    _case("gp1m-fasting-blood-2", F, "有空腹抽血", [(F, "visit", "抽血")]),
    # I-7 想、要、可以
    _case("gp1m-want-out", F, "好想出去走走了"),
    _case("gp1m-going-out", F, "我要出去走走了"),
    _case("gp1m-can-out", F, "可以出去走走了"),
    _case("gp1m-finally-can", F, "終於可以出去走走了"),
    _case("gp1m-mom-said-will", X, "媽媽說要出去走走了"),
    # I-8 講距離、「就好」
    _case("gp1m-distance", F, "從捷運站走一走就到了"),
    _case("gp1m-distance-2", F, "走走就到了"),
    _case("gp1m-just-walk", F, "走走就好了"),
    _case("gp1m-no-taxi", F, "不用叫車，走走就好了"),
    _case("gp1m-advice", F, "多出去走走就好了"),
    _case("gp1m-walk-back", F, "走一走就回來了", [(F, "exercise", "散步")]),
    _case("gp1m-feels-better", F, "腳麻，起來走走就好多了", [(F, "body", "手腳麻"), (F, "exercise", "散步")]),
    # N-1～N-5
    _case("gp1m-young-swam", F, "年輕時有游過泳"),
    _case("gp1m-med-kinds", F, "藥有吃的跟擦的"),
    _case("gp1m-med-if", F, "藥有吃就好"),
    _case("gp1m-med-if-relay", X, "爸，藥有吃就好"),
    _case("gp1m-chance", F, "有機會出去走走"),
    _case("gp1m-free-tw", F, "有閒出去走走"),
    _case("gp1m-chance-relay", X, "爸爸有機會會去走走"),
    _case("gp1m-useful", F, "走走很有用"),
    _case("gp1m-helps", F, "多走走很有幫助"),
    _case("gp1m-qigong-works", F, "練氣功很有效"),
    _case("gp1m-swing-works", F, "平甩功很有用"),
    # 多抓的說法（GP1 建議）
    _case("gp1m-stroll", F, "剛散個步回來", [(F, "exercise", "散步")]),
    _case("gp1m-swim-ge", F, "剛游個泳", [(F, "exercise", "游泳")]),
    _case("gp1m-swing", F, "平甩做完了", [(F, "exercise", "甩手")]),
    _case("gp1m-ballroom", F, "跳完國標舞", [(F, "exercise", "跳舞")]),
    _case("gp1m-social-dance", F, "跳了社交舞", [(F, "exercise", "跳舞")]),
    _case("gp1m-health-exercise", F, "健康操做完了", [(F, "exercise", "做操")]),
    _case("gp1m-med-on-time", F, "藥有準時吃", [(F, "med", "吃藥")]),
    _case("gp1m-med-obedient", F, "藥有乖乖吃", [(F, "med", "吃藥")]),
    _case("gp1m-med-on-time-le", F, "藥準時吃了", [(F, "med", "吃藥")]),
    _case("gp1m-med-just", F, "藥剛剛吃了", [(F, "med", "吃藥")]),
    _case("gp1m-med-just-2", F, "藥剛才吃了", [(F, "med", "吃藥")]),
    _case("gp1m-stroll-suggest", F, "散個步吧"),
    _case("gp1m-swim-plan", F, "明天去游個泳"),
    _case("gp1m-health-exercise-q", F, "健康操做好了沒"),
    _case("gp1m-ballroom-watch", F, "國標舞比賽看完了"),
    # 119 不受影響
    _case("gp1m-walk-fall", F, "去公園走走跌倒了", [(F, "exercise", "散步"), (F, "body", "跌倒", "", R)],
          [(F, "跌倒", True, False, False)]),
    _case("gp1m-run-chest", F, "晨跑回來胸口很痛", [(F, "exercise", "跑步"), (F, "body", "胸痛胸悶", "", R)],
          [(F, "胸痛胸悶", True, False, False)]),
    _case("gp1m-no-walk-chest", F, "好久沒有出去走走了，胸口很悶", [(F, "body", "胸痛胸悶", "", R)],
          [(F, "胸痛胸悶", True, False, False)]),
    # ── GP2 事前審查（多抓說法） ──
    # G-1 台式問句尾只擋「吃了藥」：忘了吃藥、停藥、拿藥照記（每日檢查的「忘了吃藥」提醒要用）
    _case("gp2m-forgot-hou", F, "我又忘記吃藥了齁", [(F, "med", "忘了吃藥")]),
    _case("gp2m-forgot-hou-relay", X, "爸爸又忘記吃藥了齁", [(F, "med", "忘了吃藥")]),
    _case("gp2m-stop-hou", X, "爸爸停藥了齁", [(F, "med", "停藥")]),
    _case("gp2m-fetch-hou", F, "我今天去領藥了齁", [(F, "med", "拿藥")]),
    _case("gp2m-took-hou-self", F, "我藥有吃齁"),
    # G-2 問句尾被空白或逗號切開
    _case("gp2m-hou-space", X, "爸爸藥有吃 齁"),
    _case("gp2m-hou-comma", X, "爸爸藥有吃，齁"),
    _case("gp2m-hou-comma-q", X, "爸爸藥有吃，齁？"),
    _case("gp2m-walk-hou-space", X, "爸爸今天有去走走 齁"),
    _case("gp2m-ma-space", X, "爸爸藥吃了 嗎"),  # 原本會誤記
    _case("gp2m-right-q", X, "爸爸有吃藥，對嗎？"),  # 原本會誤記
    _case("gp2m-right-ba", X, "爸爸藥吃了，對吧"),  # 原本會誤記
    _case("gp2m-headache-hou", F, "我今天頭好痛 齁", [(F, "body", "頭痛")]),  # 症狀不受影響
    _case("gp2m-and-you", F, "我藥有吃了，你呢", [(F, "med", "吃藥")]),
    _case("gp2m-mm", F, "我藥有吃了 嗯", [(F, "med", "吃藥")]),
    # G-4 步數也一樣擋台式問句尾
    _case("gp2m-steps-hou", X, "爸爸今天走了8000步齁"),
    # G-5 毛孩是寵物
    _case("gp2m-fur-kid", F, "毛孩藥有吃了"),
    # G-7 119 不受台式問句尾影響（不能把 _TAG_QUESTION_TAIL 併進 _QUESTION_TAIL）
    _case("gp2m-fall-ok-hou", X, "媽媽剛剛跌倒了，現在沒事了齁", [(M, "body", "跌倒")], [(M, "跌倒", False, False, False)]),
    _case("gp2m-fall-hou", X, "爸爸跌倒了齁", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("gp2m-fall-hou-space", X, "爸爸跌倒了 齁", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("gp2m-chest-hou", X, "爸爸胸口好痛齁", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    # ── GP2 事後審查（多抓說法） ──
    # P-1 隔著句號、問號、驚嘆號的「厚／吼／蛤」是感嘆，不是前一句的問句尾
    _case("gp2p-tired", F, "我散步回來了。厚，好累", [(F, "exercise", "散步")]),
    _case("gp2p-what", M, "我剛吃完藥。蛤？你說什麼", [(M, "med", "吃藥")]),
    _case("gp2p-hot", F, "今天去公園走走了。吼，熱死了", [(F, "exercise", "散步")]),
    _case("gp2p-bitter", F, "我吃藥了。吼，好苦", [(F, "med", "吃藥")]),
    # P-2 「好嗎」是回嘴
    _case("gp2p-retort", F, "我藥吃了，好嗎", [(F, "med", "吃藥")]),
    _case("gp2p-retort-2", F, "我早就吃藥了，好嗎", [(F, "med", "吃藥")]),
    _case("gp2p-retort-walk", F, "我今天有去散步了，好嗎！", [(F, "exercise", "散步")]),
    # P-3 問句尾前面的符號、表情
    _case("gp2p-tilde-hou", X, "爸爸藥有吃 ～齁"),
    _case("gp2p-emoji-hou", X, "爸爸藥有吃 😅齁"),
    _case("gp2p-tilde-ma", X, "爸爸藥吃了 ~嗎"),
    _case("gp2p-two-clauses", X, "爸爸有吃藥 齁 媽媽也有吃藥了", [(M, "med", "吃藥")]),
    _case("gp2p-fall-then-wow", X, "爸爸跌倒了。吼，嚇死我了", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    # ── GP1 事後審查（多抓說法） ──
    # P-1 「沒好就去／只能去」是去了（看診、運動都不能被新的否定、意願檢查擋掉）
    _case("gp1p-not-better-then-doctor", F, "感冒一直沒好就去看醫生了", [(F, "body", "感冒"), (F, "visit", "看醫生")]),
    _case("gp1p-fever-er", F, "燒一直沒退就去急診了", [(F, "visit", "急診")], [(F, "急診", True, True, False)]),
    _case("gp1p-no-effect-doctor", F, "吃藥沒效就去看醫生了", [(F, "visit", "看醫生")]),
    _case("gp1p-only-er", F, "痛到只能去急診了", [(F, "visit", "急診")], [(F, "急診", True, True, False)]),
    _case("gp1p-cant-sleep-walk", F, "睡不著就去散步了", [(F, "exercise", "散步"), (F, "sleep", "睡不好")]),
    _case("gp1p-only-walk", F, "腳痛只能去散步了", [(F, "body", "腳痛"), (F, "exercise", "散步")]),
    _case("gp1p-no-time-doctor", F, "沒時間去看醫生了"),
    _case("gp1p-then-doctor-plan", F, "吃完沒好再去看醫生"),
    # P-3 「要再確定」「不確定」
    _case("gp1p-check-again", F, "要再確定一下藥有吃"),
    _case("gp1p-not-sure", X, "我不確定爸爸藥有吃"),
    _case("gp1p-very-sure-walk", F, "我很確定今天有去散步", [(F, "exercise", "散步")]),
    # P-4 跑廁所
    _case("gp1p-all-night-toilet", F, "昨天一夜跑了三次廁所"),
    _case("gp1p-morning-toilet", F, "早晨跑了兩趟廁所"),
    _case("gp1p-night-toilet-3", F, "昨晚夜跑廁所三次"),
    # P-5 其他問句尾
    _case("gp1p-is-it", X, "爸爸藥吃了，是嗎"),
    _case("gp1p-or-not", X, "爸爸有吃藥 沒有？"),
    _case("gp1p-isnt-it", X, "爸爸藥吃了，沒有嗎"),
    _case("gp1p-no-rain", F, "我今天有去散步，沒有下雨", [(F, "exercise", "散步")]),
    _case("gp1p-no-discomfort", F, "我藥吃了 沒有不舒服", [(F, "med", "吃藥")]),
    # P-7 只剩符號的問句尾、只往前標不往後標
    _case("gp1p-emoji-q", X, "爸爸藥吃了 😅?"),
    _case("gp1p-tag-then-statement", F, "我跟你說 齁 我今天藥有吃了", [(F, "med", "吃藥")]),
    # ── 準確度：轉貼文章、地點名詞、泛指的人、停藥被記成吃藥 ──
    # 轉貼文章：整則沒有自稱、家人、時間詞、讀數，又有文章訊號 → 不是在講自己，一般紀錄都不記
    _case("pc-article-heading", F, "3. 熬夜的影響\n 長期熬夜會讓免疫力下降，容易頭痛、失眠。\n 也有研究發現和心血管疾病有關。"),
    _case("pc-article-heading-only", F, "2. 姿勢不良\n 長時間低頭滑手機，頸部負擔變大，容易肩頸痠痛、頭痛。"),
    _case("pc-article-heading-colon", M, "3. 睡眠問題：睡前喝咖啡容易失眠、心悸"),
    _case("pc-article-cue-after", M, "免疫力下降，常見頭痛、失眠，研究也指出熬夜和多種慢性病有關"),
    _case("pc-article-expository", F, "熬夜會導致血壓不穩，罹患心血管疾病的風險變高，即使有在運動也一樣"),
    _case("pc-article-other", M, "5. 其他提醒\n 睡前滑手機會讓大腦興奮，容易失眠、頭痛。"),  # 「其他」的他不是家人
    _case("pc-article-self-care", F, "6. 自我保健\n 頭痛、失眠、腰痠都要多留意。"),  # 「自我」的我不是自稱
    # 有自稱、時間詞、讀數，或文章用字只有一種又是轉述醫生的話：照舊記
    _case("pc-article-own-line", M, "研究說熬夜不好，我今天有去散步了", [(M, "exercise", "散步")]),
    _case("pc-article-own-no-time", F, "研究指出熬夜不好，我有去散步了", [(F, "exercise", "散步")]),
    _case("pc-walk-then-news", M, "今天去散步了，看到新聞報導說熬夜很傷", [(M, "exercise", "散步")]),
    _case("pc-reading-then-news", F, "血壓 128/82，新聞報導說要控制在130以下", [(F, "body", "血壓", "128/82")]),
    # 文章用字只有一種：多半是自己的話
    _case("pc-own-doctor-cause", M, "膝蓋痛，醫生說可能是退化導致的", [(M, "body", "膝蓋痛")]),
    _case("pc-own-doctor-risk", M, "藥吃了，醫生說副作用的風險不大", [(M, "med", "吃藥")]),
    _case("pc-own-risk", M, "血壓高，中風風險比較高", [(M, "body", "血壓高")]),
    _case("pc-own-cause", M, "頭痛，熬夜導致的", [(M, "body", "頭痛")]),
    _case("pc-own-study", F, "腰痠，研究說要多走路", [(F, "body", "腰痛")]),
    _case("pc-own-news-med", M, "藥吃了，研究說這種藥很安全", [(M, "med", "吃藥")]),
    _case("pc-long-own-no-pronoun", M, "一早先去公園散步了一個小時，回家吃完早餐，晚上再去游泳了，覺得腰有點痠",
          [(M, "body", "腰痛"), (M, "exercise", "散步"), (M, "exercise", "游泳")]),
    # 自己的編號清單（沒有 10 字以上的內文）不是文章
    _case("pc-own-numbered-list", F, "1)散步完了\n2)藥吃了", [(F, "exercise", "散步"), (F, "med", "吃藥")]),
    _case("pc-own-numbered-symptoms", M, "1.頭痛\n2.睡不好\n3.胃口不好",
          [(M, "body", "頭痛"), (M, "diet", "胃口不好"), (M, "sleep", "睡不好")]),
    # 119 不受影響：文章樣子的訊息裡，緊急字詞照舊回 119、照舊記
    _case("pc-article-chest", M, "胸口好悶，研究指出這種情況風險很高嗎", [(M, "body", "胸痛胸悶", "", R)],
          [(M, "胸痛胸悶", True, False, False)]),
    _case("pc-heading-chest", M, "1. 胸口好悶\n 研究指出冬天要特別小心", [(M, "body", "胸痛胸悶", "", R)],
          [(M, "胸痛胸悶", True, False, False)]),
    # 運動後面接「習慣」是講習慣
    _case("pc-swim-habit", F, "我有游泳的習慣"),
    _case("pc-walk-habit", M, "我有散步的習慣"),
    _case("pc-exercise-today", F, "我今天有運動", [(F, "exercise", "運動")]),
    # 地點名詞：有健身房、運動中心不是運動；去了（去／到／從，中間可隔幾個字）才算，有別的運動就只記那個
    _case("pc-building-gym", M, "那棟大樓地下室有健身房"),
    _case("pc-center-cafe", F, "運動中心旁邊開了一家咖啡店"),
    _case("pc-pool-exists", M, "社區健身中心也有游泳池"),
    _case("pc-relay-gym-exists", X, "媽媽說社區新蓋了健身房"),
    _case("pc-wait-at-gym", F, "我在健身房等你"),
    _case("pc-see-gym", M, "看到健身房了"),
    _case("pc-went-gym", F, "我今天去健身房了", [(F, "exercise", "健身")]),
    _case("pc-went-le-gym", M, "我去了健身房", [(M, "exercise", "健身")]),
    _case("pc-community-gym", F, "去社區健身房了", [(F, "exercise", "健身")]),
    _case("pc-back-from-gym", M, "剛從健身房回來", [(M, "exercise", "健身")]),
    _case("pc-two-places", F, "今天去了健身房和游泳池", [(F, "exercise", "健身"), (F, "exercise", "游泳")]),
    _case("pc-relay-went-gym", X, "媽媽今天去健身房了", [(M, "exercise", "健身")]),
    _case("pc-center-swim", M, "早上去運動中心游完泳了", [(M, "exercise", "游泳")]),
    _case("pc-sports-park-walk", F, "去運動公園散步了", [(F, "exercise", "散步")]),
    _case("pc-at-gym-exercise", M, "我在健身房運動", [(M, "exercise", "運動")]),
    _case("pc-at-pool-swim", F, "我在游泳池游泳", [(F, "exercise", "游泳")]),
    _case("pc-not-at-gym", M, "我沒有在健身房運動"),
    _case("pc-not-want-gym", F, "我不想在健身房運動"),
    _case("pc-always-at-gym", M, "我都在健身房運動"),
    _case("pc-often-at-pool", F, "我常常在游泳池游泳"),
    _case("pc-relay-always-gym", X, "媽媽都在健身房運動"),
    _case("pc-nearby-gym", F, "今天去了附近的健身房", [(F, "exercise", "健身")]),
    _case("pc-been-gym-expensive", M, "去過的健身房都很貴"),
    _case("pc-mention-gym", F, "聊到健身房了"),
    _case("pc-workout-done", F, "剛剛健身完", [(F, "exercise", "健身")]),
    # 泛指的人、副作用：不是本人的症狀；單獨的「人」（頭好暈人好累）是自己
    _case("pc-generic-people", M, "那場講座是給高血壓的人聽的"),
    _case("pc-generic-people-coord", M, "這套運動是給高血壓和關節不好的人做的"),
    _case("pc-generic-group", F, "痛風族群要少吃海鮮"),
    _case("pc-side-effect", M, "說明書寫會有口乾、頭暈等副作用"),
    _case("pc-patients", F, "痛風患者要少喝酒"),
    _case("pc-insomnia-people", M, "失眠的人很多"),
    _case("pc-diet-people", F, "沒吃早餐的人很多"),
    _case("pc-own-hypertension", M, "我有高血壓", [(M, "body", "血壓高")]),
    _case("pc-own-bp-high", M, "我血壓有點高", [(M, "body", "血壓高")]),
    _case("pc-people-then-own", F, "頭痛的人很多，我也頭痛", [(F, "body", "頭痛")]),
    _case("pc-dizzy-tired", M, "頭好暈人好累", [(M, "body", "頭暈")]),
    _case("pc-appetite-thin", F, "胃口不好人瘦了", [(F, "diet", "胃口不好")]),
    _case("pc-insomnia-told-family", M, "失眠跟家裡的人說了", [(M, "sleep", "睡不好")]),
    _case("pc-headache-told-neighbor", M, "頭痛跟隔壁的人說了", [(M, "body", "頭痛")]),
    _case("pc-headache-like-family", F, "我頭痛跟家裡的人一樣", [(F, "body", "頭痛")]),
    _case("pc-own-patient", M, "我是高血壓患者", [(M, "body", "血壓高")]),
    _case("pc-relay-patient", X, "媽媽是痛風病人", [(M, "body", "痛風")]),
    _case("pc-own-insomnia-tribe", F, "我也是失眠族", [(F, "sleep", "睡不好")]),
    _case("pc-own-side-effect", M, "我吃藥後有頭暈的副作用", [(M, "body", "頭暈"), (M, "med", "吃藥")]),
    _case("pc-friend-patient", F, "我有朋友是高血壓患者"),
    # 停藥不是吃藥；「吃藥前」是時間點；「了解」的「了」不是完成
    _case("pc-stop-quit", M, "我已經停止吃那種藥了", [(M, "med", "停藥")]),
    _case("pc-stop-use", M, "我停用降膽固醇的藥了", [(M, "med", "停藥")]),
    _case("pc-stop-topic", M, "胃藥已經停了", [(M, "med", "停藥")]),
    _case("pc-relay-stop", X, "媽媽已經停掉那顆止痛藥了", [(M, "med", "停藥")]),
    _case("pc-stop-what-if", F, "藥停了會怎樣"),
    _case("pc-not-stopped", M, "我沒有停吃那種藥"),
    _case("pc-cannot-stop", F, "藥不能停"),
    _case("pc-stop-question", X, "媽媽的藥停了嗎"),
    _case("pc-before-med-read", M, "吃藥前已經看過說明書"),
    _case("pc-before-med-habit", F, "吃藥前都有量血壓"),
    _case("pc-walk-understand", F, "散步之前先了解路線"),
    _case("pc-exercise-understand", M, "去運動之前先了解一下"),
    # ── 119：緊急字詞後面緊接的「人」是說話的人自己（整個人），不是泛指的人 ──
    _case("rf-chest-person", F, "我胸悶人不舒服", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("rf-fall-person", M, "剛剛跌倒人站不起來", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("rf-breath-person", F, "喘不過氣人很不舒服", [(F, "body", "呼吸困難", "", R)], [(F, "呼吸困難", True, False, False)]),
    _case("rf-faint-person", M, "剛剛昏倒人很虛", [(M, "body", "昏倒", "", R)], [(M, "昏倒", True, False, False)]),
    _case("rf-relay-fall-person", X, "爸爸剛剛跌倒人站不起來", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("rf-sweat", F, "我胸悶人好多汗", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    # 「人滿頭大汗」「人口吐白沫」「人滿身是血」「人好多血」「人很多地方痛」：整個人的急症，不是人多、人口
    _case("rf-sweat-head", F, "胸悶人滿頭大汗", [(F, "body", "胸痛胸悶", "", R)], [(F, "胸痛胸悶", True, False, False)]),
    _case("rf-foam", M, "昏倒人口吐白沫", [(M, "body", "昏倒", "", R)], [(M, "昏倒", True, False, False)]),
    _case("rf-blood-body", F, "跌倒人滿身是血", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("rf-blood-much", M, "跌倒人好多血", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    _case("rf-many-places", F, "剛剛跌倒人很多地方痛", [(F, "body", "跌倒", "", R)], [(F, "跌倒", True, False, False)]),
    _case("rf-advice-careful", M, "跌倒人要小心"),
    _case("rf-fall-slurred", M, "跌倒人口齒不清", [(M, "body", "嘴歪或說話不清", "", R), (M, "body", "跌倒", "", R)],
          [(M, "嘴歪或說話不清", True, False, False), (M, "跌倒", True, False, False)]),
    # 「人沒事」「人好多了」：已經好了，只記錄
    _case("rf-fall-person-ok", M, "跌倒人沒事", [(M, "body", "跌倒")], [(M, "跌倒", False, False, False)]),
    _case("rf-fall-better", F, "跌倒人好多了", [(F, "body", "跌倒")], [(F, "跌倒", False, False, False)]),
    # 泛指的人、人數、人人、保險、成語照舊不算
    _case("rf-generic-de-person", F, "跌倒的人很多"),
    _case("rf-generic-count", M, "跌倒人數增加"),
    _case("rf-generic-everyone", F, "跌倒人人有責"),
    _case("rf-generic-insurance", M, "跌倒人壽有賠嗎"),
    _case("rf-generic-idiom", F, "跌倒人命關天"),
    _case("rf-generic-patient", M, "中風病人要復健"),
    # 病名後面的「人」多半是泛指（中風人很難照顧）：照舊不算
    _case("rf-disease-generic", F, "中風人很難照顧"),
    _case("rf-disease-young", M, "心肌梗塞人越來越年輕"),
    _case("rf-more-and-more", F, "跌倒人越來越多"),
    # 「等人」原本就回 119，照舊回（新的泛指寫法只認「的人」和「人…」，不會讓 119 變少）
    _case("rf-wait-help", M, "我跌倒等人來扶", [(M, "body", "跌倒", "", R)], [(M, "跌倒", True, False, False)]),
    # 已在處理的字詞（急診、住院…）後面接「人…」照舊不算：同一則的 119 不會降成只私訊
    _case("rf-er-crowded", F, "我胸口很痛，急診人超級多不想去", [(F, "body", "胸痛胸悶", "", R)],
          [(F, "胸痛胸悶", True, False, False)]),
    _case("rf-er-crowded-de", M, "我胸口很痛，急診的人很多不想去", [(M, "body", "胸痛胸悶", "", R)],
          [(M, "胸痛胸悶", True, False, False)]),
    _case("rf-er-squeeze", F, "我胸口很痛，急診人很擠不想去", [(F, "body", "胸痛胸悶", "", R)],
          [(F, "胸痛胸悶", True, False, False)]),
    _case("rf-er-crowded-alone", M, "急診人很多"),
    _case("rf-ward-staff", F, "住院人力不足"),
]


@pytest.mark.parametrize("speaker, text, findings, flags", CASES)
def test_sentence_outcomes(speaker, text, findings, flags):
    assert _as_tuples(extract(text, speaker)) == findings
    assert _flag_tuples(detect_red_flags(text, speaker)) == flags
    if flags:
        assert might_have_red_flag(text)  # 預檢是緊急字詞判斷的超集合


@pytest.mark.parametrize("forced", [True, False])
def test_article_rule_never_changes_red_flags(monkeypatch, forced):
    """轉貼文章的整則判斷只管一般紀錄：強制成立或不成立，緊急字詞（119 判斷與緊急紀錄）都一樣。"""
    def red(text, speaker):
        rows = [row for row in _as_tuples(extract(text, speaker)) if row[2] in RED_FLAG_LABELS]
        return rows, _flag_tuples(detect_red_flags(text, speaker))

    samples = [(param.values[0], param.values[1]) for param in CASES]
    before = [red(text, speaker) for speaker, text in samples]
    monkeypatch.setattr(phx, "_impersonal", lambda norm, speaker: forced)
    monkeypatch.setattr(phx, "_heading_signal", lambda norm: forced)
    monkeypatch.setattr(phx, "_article_words", lambda norm: forced)
    assert [red(text, speaker) for speaker, text in samples] == before


# 緊急字詞後面的泛指「人…」：每一個都要擋住（病名、已在處理的字詞另有寬排除，這裡用跌倒、胸悶測主表）
_RF_GENERIC_TAILS = ("的人很多", "人人有責", "人的照顧", "人們都怕", "人類最常見", "人性化", "人際關係", "人參可以吃嗎",
                     "人壽有賠嗎", "人體會怎樣", "人命關天", "人數增加", "人次", "人士", "人群", "人員", "人潮", "人家會笑",
                     "人生", "人力", "人滿為患", "人口老化", "人身意外險", "人要小心", "人很多", "人越來越多")


@pytest.mark.parametrize("head", ["跌倒", "胸悶"])
@pytest.mark.parametrize("tail", _RF_GENERIC_TAILS)
def test_red_flag_generic_person_tails(head, tail):
    assert detect_red_flags(head + tail, FATHER) == []


# ── 第三輪審查的關係名詞掃描：這些都是別人，不能算到爸媽頭上 ──

_RELATION_NOUNS = (
    "同梯 牌友 球友 山友 室友 教友 師兄 師姐 姊妹 兄弟 學長 學姊 主管 上司 下屬 客戶 房東 病友 里長 志工 牧師 師父 親家 "
    "親家母 親戚 表舅 姑婆 叔公 舅公 好朋友 好友 老友 老朋友 戰友 前同事 老鄰居 舊同事 大學同學 國小同學 高中同學 老戰友 姪子 "
    "外甥女 外孫 孫女兒 乾女兒 嫂子 妹婿 姊夫 弟媳 小舅子 大伯 小叔 二伯 三叔 四姨 大舅 堂弟 表姊 舅媽 姨媽 婆婆 公公 同事 朋友 鄰居"
).split()


@pytest.mark.parametrize("noun", _RELATION_NOUNS)
def test_relation_nouns_are_someone_else(noun):
    for speaker in (F, M):
        for text in (f"我{noun}昏倒了", f"我的{noun}跌倒了", f"我{noun}剛剛中風了", f"{noun}昏倒了"):
            assert detect_red_flags(text, speaker) == [], text
            assert not any(finding.flag for finding in extract(text, speaker)), text
    for title, pronoun in (("爸爸", "他"), ("媽媽", "她")):
        for text in (f"{title}說{pronoun}{noun}昏倒了", f"{title}說{pronoun}的{noun}跌倒了", f"{title}剛剛說{pronoun}{noun}中風了"):
            assert detect_red_flags(text, X) == [], text


_HELD_OUT_NOUNS = "店長 村長 鎮長 隊長 局長 科長 處長 訪客 旅客 房客 移工 勞工 清潔工 技工 水電工 店東 人士 道士 騙子 胖子".split()


@pytest.mark.parametrize("noun", _HELD_OUT_NOUNS)
def test_held_out_person_nouns_are_someone_else(noun):
    """審查員保留的人名詞（不在任何清單裡）：靠字尾規則認得，不能算到爸媽頭上。"""
    for speaker, text in ((M, f"我{noun}昏倒了"), (F, f"{noun}剛剛跌倒了"), (X, f"爸爸說{noun}剛剛跌倒了")):
        assert detect_red_flags(text, speaker) == [], text


# 第五輪審查的 30 個三字職稱（含收銀員、推拿師、搬運工這類動詞開頭的），中間隔了地點、時間也一樣是別人
_JOB_TITLES = (
    "收銀員 收費員 推銷員 業務員 管理員 公務員 服務員 清潔員 保全員 快遞員 送貨員 售貨員 照服員 居服員 清潔工 水電工 搬運工 洗碗工 "
    "臨時工 鐘點工 營養師 藥劑師 治療師 會計師 工程師 設計師 理髮師 美髮師 按摩師 推拿師"
).split()


@pytest.mark.parametrize("title", _JOB_TITLES)
def test_job_titles_are_someone_else(title):
    for speaker, text in (
        (F, f"{title}在門口昏倒了"), (M, f"{title}剛剛在店裡跌倒了"), (X, f"爸爸說{title}在門口跌倒了"),
        (X, f"爸爸說{title}剛剛在大廳昏倒了"), (X, f"媽媽說{title}剛剛在大廳昏倒了"), (F, f"我們的{title}在公司昏倒了"),
        (M, f"我看到{title}在路上昏倒了"), (F, f"{title}昏倒了"), (X, f"爸爸說{title}昏倒了"), (M, f"我的{title}中風了"),
    ):
        assert detect_red_flags(text, speaker) == [], text
        assert not any(finding.flag for finding in extract(text, speaker)), text


@pytest.mark.parametrize("word", sorted(w for w in phx._TITLE_KINDS if len(w) > 1))
def test_titles_are_never_read_as_someone_else(word):
    """稱謂不會被「以人字結尾的名詞」或其他規則吃掉：句首、「說」後面、「我的」後面都照稱謂表。"""
    for speaker in (F, M, X):
        cases = [(word + "今天去散步了", phx._roles_of(word, speaker, True)),
                 ("阿甲說" + word + "今天去散步了", phx._roles_of(word, speaker, False))]
        if word[0] not in "我你":
            own = "我" + word if word in phx._SPOUSE_WORDS and "我" + word in phx._TITLE_KINDS else word
            cases.append(("我的" + word + "今天去散步了", phx._roles_of(own, speaker, False)))
        for text, expected in cases:
            clause = phx._analyze(phx._normalize(text), speaker, ())[0]
            start = text.index(word)
            mention = next((m for m in clause.mentions if m[0] == start), None)
            assert mention is not None and mention[1] == start + len(word), (text, speaker, clause.mentions)
            assert mention[2] == expected, (text, speaker, mention)


@pytest.mark.parametrize("title, pronoun", [("爸爸", "他"), ("媽媽", "她")])
def test_relayed_grandparents_are_someone_else(title, pronoun):
    for text in (f"{title}說{pronoun}爸爸昏倒了", f"{title}說{pronoun}媽媽跌倒了", f"{title}說{pronoun}的媽媽跌倒了"):
        assert detect_red_flags(text, X) == [], text


# ── 呼叫端傳進來的家人別名 ──

def test_configured_family_names_count_as_someone_else():
    # 「阿甲」不是稱謂、也不是以人字結尾的名詞：不給別名時就當成說話者自己
    assert _as_tuples(extract("阿甲今天跌倒了", F)) == _findings((F, "body", "跌倒", "", R))
    assert extract("阿甲今天跌倒了", F, other_names=["阿甲"]) == []
    assert detect_red_flags("阿甲今天跌倒了", F, other_names=("阿甲",)) == []
    assert extract("阿甲的媽媽今天去回診了", X, other_names=iter(["阿甲"])) == []
    # 「成員甲」本身就以「成員」開頭，不給別名也認得是別人
    assert extract("成員甲今天跌倒了", F) == []


def test_parent_titles_and_junk_in_other_names_are_ignored():
    names = ("爸爸", "媽媽", "老媽", "我", "", "甲", "成員甲", None)
    assert _as_tuples(extract("媽媽今天有去散步", X, other_names=names)) == _findings((M, "exercise", "散步"))
    assert _flag_tuples(detect_red_flags("爸爸剛剛跌倒了", X, other_names=names)) == [(F, "跌倒", True, False, False)]


# ── 整則略過：「/」開頭、網址、太長、不是字串 ──

@pytest.mark.parametrize("text", [
    # 先點名咪寶再下指令（「咪寶 /記住 …」）由 main 判斷、不會送進來；這裡只認「/」開頭（GP2 R3 #1）
    "/健康", "/記住 爸爸胸口好痛", "  /健康 吃藥了", "／記住 我胸口好痛",
    "爸爸胸口好痛 https://example.com/a", "我胸口好痛 www.example.com",
    "", "   ",
])
def test_skipped_messages(text):
    assert extract(text, F) == []
    assert detect_red_flags(text, F) == []
    assert not might_have_red_flag(text)


def test_max_chars_boundary():
    body = "我胸口好痛"
    at_limit = body + "。" * (MAX_CHARS - len(body))
    assert len(at_limit) == MAX_CHARS
    assert _flag_tuples(detect_red_flags(at_limit, F)) == [(F, "胸痛胸悶", True, False, False)]
    assert extract(at_limit, F)
    assert might_have_red_flag(at_limit)
    assert detect_red_flags(at_limit + "。", F) == []
    assert extract(at_limit + "。", F) == []
    assert not might_have_red_flag(at_limit + "。")


@pytest.mark.parametrize("value", [None, 123, b"\xe8\x83\xb8"])
def test_non_text_input_is_ignored(value):
    assert extract(value, F) == []
    assert detect_red_flags(value, F) == []
    assert might_have_red_flag(value) is False


def test_plain_chat_does_not_pass_the_precheck():
    for text in ("今天天氣很好", "晚餐吃什麼", "我到家了", "明天幾點出門"):
        assert not might_have_red_flag(text)


# ── 固定詞表 ──

def test_allowed_items_cover_every_category_and_red_flag_label():
    assert set(ALLOWED_ITEMS) == set(CATEGORIES)
    assert all(isinstance(items, frozenset) and items for items in ALLOWED_ITEMS.values())
    assert set(RED_FLAG_LABELS) <= ALLOWED_ITEMS["body"]
    assert {"血壓", "血糖", "體重", "心跳"} <= ALLOWED_ITEMS["body"]
    assert ALLOWED_ITEMS["med"] == {"吃藥", "忘了吃藥", "拿藥", "停藥"}
    assert "氣功" in ALLOWED_ITEMS["exercise"]  # 10/17 新標籤：卡片、週報、DB 詞表都從這裡拿
    import parent_health_db

    assert parent_health_db.is_allowed("exercise", "氣功")
    assert ALLOWED_ITEMS["sleep"] == {"睡不好", "睡得好"}
    assert not set(IN_CARE_LABELS) & set(RED_FLAG_LABELS)


@pytest.mark.parametrize("value", ["135/85", "70/40", "250/150", "65.5", "110", "72", "8000步", "1步", "999999步"])
def test_value_re_accepts_readings(value):
    assert VALUE_RE.fullmatch(value)


@pytest.mark.parametrize("value", [
    "", "8000", "1234567步", "135/85x", "65.55", "1/2", "abc", "１３５/８５", "١٢٣", "135/85\n",
])
def test_value_re_rejects_everything_else(value):
    assert not VALUE_RE.fullmatch(value)


def test_dataclasses_are_frozen():
    finding = Finding(F, "med", "吃藥")
    flag = RedFlag(F, "跌倒", True, False, False)
    with pytest.raises(AttributeError):
        finding.item = "x"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        flag.label = "x"  # type: ignore[misc]


# ── fuzz：隨機字串＋關鍵字，輸出一定在固定詞表內，預檢一定是超集合 ──

_FUZZ_PIECES = (
    # 稱謂與別人
    "爸爸", "媽媽", "爸", "媽", "爸媽", "我", "你", "他", "她", "你爸", "我媽", "老婆", "老公", "太太", "姑媽", "阿嬤",
    "朋友", "其他", "成員甲", "的", "說",
    # 線索
    "了", "過", "完", "剛剛", "今天", "早上", "昨天", "上個月", "以前", "睡覺之前", "明天", "要去", "如果", "記得",
    "不要", "小心", "不小心", "醫生說", "新聞", "徵兆", "提醒", "幫", "差點", "怕", "容易", "常常", "會", "會不會",
    "沒", "沒有", "不", "還沒", "好", "很", "有點", "到", "得", "快", "好了", "沒事了", "嗎", "呢", "有沒有",
    # 健康詞
    "吃藥", "吃", "藥", "藥膳", "忘了", "拿藥", "停藥", "回診", "看醫生", "看牙", "健檢", "報告", "復健", "急診",
    "散步", "走路", "爬山", "太極", "運動", "走了", "步", "萬步", "睡不好", "睡得好", "睡得", "失眠", "胃口不好",
    "沒吃早餐", "少吃甜", "喝了酒", "頭暈", "膝蓋痛", "感冒", "發燒", "血壓", "血糖", "體重", "心跳",
    # 緊急字詞
    "胸口", "胸悶", "胸痛", "痛", "悶", "喘不過氣", "呼吸困難", "嘴歪", "嘴", "歪", "說話不清", "說不出話",
    "左手", "右邊", "半邊", "沒力", "麻", "中風", "心肌梗塞", "昏倒", "暈", "跌倒", "摔下來", "跌了一跤", "滑倒",
    "救護車", "送醫", "住院", "股票", "谷底", "五十肩",
    # 數字、符號、雜訊
    "135/85", "300/20", "110", "65.5", "8000", "1", "/", ".", "，", "。", "、", "？", "?", "！", " ", "  ", "\n",
    "https://x.y", "\u200b", "\ufeff", "１２０", "／", "Ｘ光", "~", "😀", "abc", "x",
    # 第三輪：聽話者、受詞、引號、阻斷詞、結構名詞、稱謂
    "打給", "打電話給", "告訴", "跟", "問", "說:", "說「", "他老人家", "他們", "的時候", "時", "結果", "就", "遛", "帶",
    "餵", "家", "面前", "師姐", "牌友", "外孫", "一個人", "有在", "一顆", "麻煩", "衣服", "丈夫", "妻子", "父母", "店員",
    # 第四輪：教會、聚會、傾向的會、陪／扶、同伴、東西、公車、院子、志工、看到
    "教會", "聚會", "同學會", "年紀大會", "陪", "扶", "跟老同學", "買東西", "公車", "院子", "志工", "看到", "社區",
    # 第五輪：三字職稱、一個人、地點裡的機構詞、同伴片語、聚會名稱、以前的
    "收銀員", "搬運工", "管理員", "做完工", "收工", "看到一個人", "一個人在家", "里長", "辦公室", "醫院長廊", "校長", "站",
    "隔壁", "一群", "一個", "公司", "以前的", "王", "和", "與", "在家會", "家長會", "大會", "年會",
    # 第六輪：清單外的聚會名稱、時間詞後面的一個人
    "法會", "農會", "演唱會", "讀書會", "園遊會", "舞會", "參加", "跳舞", "半夜一個人", "說今天一個人", "另一個人", "其中",
    "現在", "告訴我", "電視播", "常常一個人", "三點",
)


def _assert_valid_findings(findings):
    seen = set()
    for finding in findings:
        assert isinstance(finding, Finding)
        assert finding.role in PARENTS
        assert finding.category in CATEGORIES
        assert finding.item in ALLOWED_ITEMS[finding.category]
        assert finding.value == "" or VALUE_RE.fullmatch(finding.value)
        assert finding.flag in ("", R)
        if finding.flag:
            assert finding.category == "body" and finding.item in RED_FLAG_LABELS
        key = (finding.role, finding.category, finding.item, finding.value)
        assert key not in seen  # 同一則同 (role, category, item, value) 只出一次
        seen.add(key)


def _assert_valid_flags(flags):
    seen = set()
    for flag in flags:
        assert isinstance(flag, RedFlag)
        assert flag.role in PARENTS
        assert flag.label in RED_FLAG_LABELS + IN_CARE_LABELS
        assert all(isinstance(value, bool) for value in (flag.urgent, flag.in_care, flag.future))
        if flag.label in IN_CARE_LABELS and flag.urgent and not flag.future:
            assert flag.in_care  # 正在急診／救護車／送醫／住院；「明天住院」不算已在處理
        assert (flag.role, flag.label) not in seen
        seen.add((flag.role, flag.label))


def _random_text(rng, max_pieces):
    parts = []
    for _ in range(rng.randint(1, max_pieces)):
        roll = rng.random()
        if roll < 0.7:
            parts.append(rng.choice(_FUZZ_PIECES))
        elif roll < 0.9:
            parts.append(chr(rng.randint(0x4E00, 0x9FFF)))
        else:
            parts.append(chr(rng.randint(0x21, 0x7E)))
    return "".join(parts)


def test_fuzz_outputs_stay_on_the_fixed_lists():
    rng = random.Random(20261011)
    names_pool = ("成員甲", "爸爸", "媽媽", "成員乙", "x", "阿明")
    for _ in range(3000):
        text = _random_text(rng, 30)
        speaker = rng.choice((F, M, X, "成員甲"))
        names = tuple(name for name in names_pool if rng.random() < 0.3)
        findings = extract(text, speaker, other_names=names)
        flags = detect_red_flags(text, speaker, other_names=names)
        _assert_valid_findings(findings)
        _assert_valid_flags(flags)
        if flags or any(finding.flag for finding in findings):
            assert might_have_red_flag(text), text
        assert extract(text, speaker, other_names=names) == findings  # 純函式：同輸入同輸出


def test_fuzz_uncapped_helpers_stay_on_the_fixed_lists():
    """公開函式只處理 200 字內；內部函式在長文字上也一樣只吐固定詞表。"""
    rng = random.Random(1011)
    for _ in range(400):
        norm = phx._normalize(_random_text(rng, 150))
        speaker = rng.choice((F, M, X))
        _assert_valid_findings(phx._extract_core(norm, speaker, ("成員甲",)))
        _assert_valid_flags(phx._red_flags_core(norm, speaker, ("成員甲",)))


# ── 線性時間：webhook 鎖內跑，不能讓一則刁鑽留言卡住 ──

class _Deadline(Exception):
    pass


def _seconds(check, text):
    """``check(text)`` 花多久；卡住的話 5 秒後失敗，不拖垮整套測試（照 test_grounded_reply_policy）。"""
    import signal
    import time

    def expire(*_):
        raise _Deadline(text[:12])

    previous = signal.signal(signal.SIGALRM, expire)
    outer = signal.setitimer(signal.ITIMER_REAL, 5)
    started = time.perf_counter()
    try:
        check(text)
        return time.perf_counter() - started
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if outer[0]:
            left = max(outer[0] - (time.perf_counter() - started), 0.001)
            signal.setitimer(signal.ITIMER_REAL, left, outer[1])


_SLOW_SHAPES = {
    "胸口好好好…痛": lambda n: "胸口" + "好" * n + "痛",
    "many chest hits": lambda n: "胸口好痛" * (n // 4),
    "爸 run": lambda n: "爸" * n + "跌倒了",
    "媽 run": lambda n: "媽" * n + "吃藥了",
    "爸媽 run": lambda n: "爸媽" * (n // 2) + "去散步了",
    "不 run": lambda n: "不" * n + "吃藥了",
    "沒 run": lambda n: "沒" * n + "胸悶",
    "沒有 run": lambda n: "沒有" * (n // 2) + "頭暈",
    "many med clauses": lambda n: "吃藥了。" * (n // 4),
    "要確定 then many 藥有吃": lambda n: "要確定" + "藥有吃" * (n // 3),  # 「要確定」只在每個子句找一次
    "many tiny clauses": lambda n: "x。" * (n // 2),
    "separators only": lambda n: "，" * n,
    "spaces then a reading": lambda n: " " * n + "血壓 135/85",
    "digits and spaces": lambda n: "1 " * (n // 2),
    "many readings": lambda n: "血壓135/85" * (n // 9),
    "many falls": lambda n: "跌倒" * (n // 2),
    "habit words then a fall": lambda n: "我容易" * (n // 3) + "跌倒",
    "其他 run": lambda n: "其他" * (n // 2) + "頭暈",
    "了 run": lambda n: "了" * n,
    "old falls then resolved": lambda n: "上個月跌倒，" * (n // 6) + "好多了",
    "chest and resolved clauses": lambda n: "爸爸胸口好痛，沒事了" * (n // 10),
    "junk tail": lambda n: "~" * n + "嗎x",
    "吃 run": lambda n: "吃" * n + "藥",
    "forgot with a long gap": lambda n: "忘了吃" + "x" * n + "藥",
    "slash pairs": lambda n: "1/" * (n // 2),
    "possessives": lambda n: "朋友的媽媽" * (n // 5) + "跌倒了",
    "A-not-A run": lambda n: "好不好" * (n // 3),
    "mixed message": lambda n: "爸爸剛剛胸口好痛 媽媽今天有去散步，血壓 135/85 走了 8000 步！我忘了吃藥嗎？" * (n // 48),
    # 第二輪規則：回指、姓氏、親戚、「的」、副詞間隔、已經沒事、句尾問句、程度補語
    "pronouns after a reset": lambda n: "爸爸朋友" + "他" * n,
    "relay chain": lambda n: "爸爸跟我說他胸口痛，" * (n // 10),
    "我 run before a pronoun": lambda n: "爸爸" + "我" * n + "說他胸口痛",
    "surname titles": lambda n: "王媽媽" * (n // 3) + "跌倒了",
    "kin after 我": lambda n: "我姊" * (n // 2) + "中風了",
    "possessive titles": lambda n: "的媽媽" * (n // 3),
    "adverb gap run": lambda n: "胸口" + "又還也都" * (n // 4) + "痛",
    "resolution words": lambda n: "跌倒了還好沒事" * (n // 7),
    "了沒 run": lambda n: "了沒" * (n // 2),
    "degree fillers": lambda n: "熱到" + "快" * n + "昏倒",
    "owner after 的": lambda n: "住院的阿姨" * (n // 5),
    # 第三輪：說話動詞的主詞、引號、受詞、副詞跳過、未來詞與阻斷詞、結構名詞
    "剛剛 run then a keyword": lambda n: "剛剛" * (n // 2) + "昏倒",
    "adverb runs before keywords": lambda n: ("剛剛又" * 3 + "昏倒") * (n // 11),
    "addressee relays": lambda n: "跟店員說他胸口痛" * (n // 8),
    "quoted 我 run": lambda n: "說：我" * (n // 3),
    "他們 run": lambda n: "他們" * (n // 2),
    "遛狗 run then a fall": lambda n: "遛狗" * (n // 2) + "跌倒",
    "爸爸說他 run": lambda n: "爸爸說他" * (n // 4),
    "future blocked by 的時候": lambda n: "要去廁所的時候跌倒了" * (n // 10),
    "structural nouns": lambda n: "我師姐" * (n // 3) + "昏倒了",
    "possessed nouns": lambda n: "的牌友" * (n // 3),
    "speech verbs only": lambda n: "說" * n + "他胸口痛",
    "blockers only": lambda n: "就" * n + "跌倒",
    "relay across clauses": lambda n: "爸爸打電話來，他說胸口痛，" * (n // 13),
    # 第四輪：同伴、修飾字、東西摔、看到某人、被陪的爸媽、轉述裡的別人
    "companions with modifiers": lambda n: "我今天跟老同學" * (n // 7) + "昏倒",
    "things that fell": lambda n: "買東西摔倒" * (n // 5),
    "pivot nouns": lambda n: "看到牌友" * (n // 4) + "跌倒",
    "volunteer compounds": lambda n: "社區志工" * (n // 4) + "跌倒",
    "escorted parents": lambda n: "陪媽媽" * (n // 3) + "跌倒",
    "third parties in relays": lambda n: "爸爸說朋友中風了，他說" * (n // 11),
    # 第五輪：職稱、一個人、地點裡的機構詞、「跟…的…的」同伴片語、數量詞、姓氏、聚會、以前的同事
    "job titles with gaps": lambda n: "收銀員在門口" * (n // 6) + "昏倒",
    "job-title activities": lambda n: "我做完工" * (n // 4) + "就跌倒",
    "一個人 after pivots": lambda n: "看到一個人" * (n // 5) + "昏倒",
    "institution places": lambda n: "在里長辦公室" * (n // 6) + "跌倒",
    "companion 的 chains": lambda n: "我跟隔壁的阿姨的" * (n // 8) + "朋友跌倒",
    "companion 的 runs": lambda n: "我跟" + "的" * n + "朋友跌倒",
    "companion phrases": lambda n: "我跟以前公司的同事" * (n // 9) + "跌倒",
    "quantifier runs": lambda n: "跟一群" * (n // 3) + "朋友跌倒",
    "surname runs": lambda n: "跟王" * (n // 2) + "媽媽跌倒",
    "meeting places": lambda n: "在家會" * (n // 3) + "跌倒",
    "former coworkers": lambda n: "以前的同事" * (n // 5) + "跌倒",
    # 第六輪：清單外的「X會」、時間詞後面的一個人、說＋時間詞＋一個人、句中標點後的一個人、跟＋修飾語＋同伴
    "unlisted event places": lambda n: "在法會" * (n // 3) + "跌倒",
    "long names before 會": lambda n: "去參加演唱" * (n // 5) + "會跌倒",
    "一個人 after time words": lambda n: "我剛剛一個人" * (n // 6) + "跌倒",
    "說 time-word 一個人": lambda n: "說今天一個人" * (n // 6) + "昏倒",
    "subject and adverbs before 一個人": lambda n: "我剛剛" * (n // 3) + "一個人在浴室跌倒",
    "time words before 一個人": lambda n: "今天" * (n // 2) + "一個人在浴室跌倒",
    "listeners before 一個人": lambda n: "告訴我今天" * (n // 5) + "一個人在路上昏倒",
    "一個人 after commas": lambda n: "，一個人" * (n // 4) + "昏倒",
    "companions with surnames": lambda n: "我跟隔壁王" * (n // 5) + "媽媽去散步跌倒",
    "numbered headings": lambda n: "1. 熬夜\n 長期熬夜會導致免疫力下降研究\n" * (n // 22) + "頭痛",
}


@pytest.mark.parametrize("shape", sorted(_SLOW_SHAPES))
def test_uncapped_helpers_run_in_linear_time(shape):
    make = _SLOW_SHAPES[shape]
    small_text, large_text = phx._normalize(make(5000)), phx._normalize(make(20000))
    for core in (phx._extract_core, phx._red_flags_core):
        def run(text, core=core):
            return core(text, FATHER, ("成員甲",))

        small = min(_seconds(run, small_text) for _ in range(3))
        large = min(_seconds(run, large_text) for _ in range(3))
        # 50 ms 的上限量在公開函式上（下一個測試）；內部函式 5000 字實測最慢約 25 ms，這裡留空間給並行測試的機器
        assert small < 0.1, (core.__name__, small)
        assert large < 1.0, (core.__name__, large)
        assert large < 8 * max(small, 0.002), (core.__name__, small, large)  # 4 倍長約 4 倍時間


@pytest.mark.parametrize("shape", sorted(_SLOW_SHAPES))
def test_public_functions_are_fast_on_long_and_max_length_input(shape):
    make = _SLOW_SHAPES[shape]
    for text in (make(5000), make(5000)[:MAX_CHARS]):
        for check in (might_have_red_flag, lambda t: extract(t, FATHER), lambda t: detect_red_flags(t, MOTHER)):
            assert min(_seconds(check, text) for _ in range(3)) < 0.05
    small = min(_seconds(might_have_red_flag, make(5000)) for _ in range(3))
    large = min(_seconds(might_have_red_flag, make(20000)) for _ in range(3))
    assert large < 8 * max(small, 0.002)
