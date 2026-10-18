"""爸媽健康：從家庭群組留言整理爸媽的健康紀錄（純規則、純函式、只用標準庫）。

公開介面
    might_have_red_flag(text)                          → bool：純子字串預檢，給 webhook 鎖內先篩
    extract(text, speaker, *, other_names=())           → list[Finding]
    detect_red_flags(text, speaker, *, other_names=())  → list[RedFlag]

speaker 是說話者角色：FATHER／MOTHER／FAMILY（其他值一律當 FAMILY）。
other_names 是呼叫端給的家人別名（configured_family_aliases()）：出現在健康詞前面時跟
「朋友」「他」一樣算別人；裡面的「爸爸」「媽媽」這類稱謂、空字串、單字會自動忽略。

輸出只來自固定詞表（ALLOWED_ITEMS、RED_FLAG_LABELS、IN_CARE_LABELS）與數字讀數（VALUE_RE），
絕不回傳留言原文。regex 一律有界重複、沒有巢狀量詞：處理時間跟長度成正比。
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

__all__ = [
    "FATHER", "MOTHER", "PARENTS", "FAMILY", "CATEGORIES", "MAX_CHARS",
    "ALLOWED_ITEMS", "RED_FLAG_LABELS", "IN_CARE_LABELS", "VALUE_RE",
    "Finding", "RedFlag", "might_have_red_flag", "extract", "detect_red_flags",
]

FATHER = "爸爸"
MOTHER = "媽媽"
PARENTS = (FATHER, MOTHER)
FAMILY = "family"
CATEGORIES = ("med", "visit", "exercise", "sleep", "diet", "body")
MAX_CHARS = 200  # 正規化後（去頭尾空白）超過就整則略過

RED_FLAG_LABELS: tuple[str, ...] = (
    "胸痛胸悶", "呼吸困難", "嘴歪或說話不清", "單側無力", "中風", "心臟病發", "昏倒", "跌倒",
)
IN_CARE_LABELS: tuple[str, ...] = ("急診", "救護車", "送醫", "住院")
# re.ASCII：只認 0-9（\d 預設也吃別種文字的數字，NFKC 不一定會轉成 0-9）
VALUE_RE = re.compile(r"\d{2,3}/\d{2,3}|\d{2,3}(?:\.\d)?|\d{1,6}步", re.ASCII)


@dataclass(frozen=True)
class Finding:
    """一筆健康紀錄：item ∈ ALLOWED_ITEMS[category]；value 是 "" 或符合 VALUE_RE。"""

    role: str
    category: str
    item: str
    value: str = ""
    flag: str = ""  # "redflag"＝符合條件的緊急字詞（category="body"、item ∈ RED_FLAG_LABELS）


@dataclass(frozen=True)
class RedFlag:
    """緊急字詞：同一則訊息同一位家長同一標籤只出一次。"""

    role: str
    label: str      # RED_FLAG_LABELS 或 IN_CARE_LABELS
    urgent: bool    # False＝很久以前／已經好了：只記錄（不回 119、不私訊）
    in_care: bool   # 已在急診／救護車／送醫／住院：只私訊
    future: bool    # 只出現在未來／計畫子句：只私訊


# ─────────────────────────────────────────────────────────────────────────────
# 固定詞表：(標籤, pattern)；同一位置先列的先試，所以長的、特定的放前面
# ─────────────────────────────────────────────────────────────────────────────

_VISIT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("回診", r"回診|複診"),
    ("看醫生", r"看(?:了|過|完)?(?:醫生|醫師)"),
    ("看牙", r"看(?:了|過|完)?(?:牙醫|牙齒|牙科|牙)"),
    ("看眼科", r"看(?:了|過|完)?眼科"),
    ("看中醫", r"看(?:了|過|完)?中醫"),
    ("看診", r"看診|門診|看(?:了|過|完)?(?:家醫|心臟|皮膚|骨|神經|內|外|耳鼻喉|婦產|泌尿|腸胃|胸腔|新陳代謝|精神|身心)科"),
    ("洗牙", r"洗(?:了|完)?牙"),
    ("拔牙", r"拔(?:了|完)?牙"),
    ("補牙", r"補(?:了|完)?牙"),
    ("針灸", r"針灸"),
    ("復健", r"復健"),
    ("健檢", r"健檢|健康檢查"),
    ("抽血", r"抽(?:了|完)?血"),
    ("照胃鏡", r"(?:照|做)(?:了|完)?胃鏡"),
    ("照X光", r"(?:照|拍)(?:了|完)?[Xx]光"),
    ("超音波", r"超音波"),
    ("打疫苗", r"打(?:了|完|過)?(?:流感|新冠|肺炎鏈球菌|肺炎|帶狀皰疹|皮蛇)?疫苗"),
    ("急診", r"急診"),
)

_EXERCISE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("元極舞", r"元極舞"),
    ("外丹功", r"外丹功"),
    ("氣功", r"(?<![冷暖蒸換排空])氣功(?![能率效])|八段錦"),  # 「冷氣功能」「蒸氣功能」不是氣功
    # 「走走」：不是「走走停停／走走看／走走走」，也不是講距離或「就好」（「走一走就到了」「走走就好了」）
    ("散步", r"散個?步|走路|健走|快走|(?<!走)走(?:了?一)?走(?![走停看]|就(?:到|好(?!多)))"),
    ("爬山", r"爬山|登山"),
    ("太極", r"太極"),
    ("游泳", r"游(?:了|完|個)?泳"),  # 「游過泳」多半是講經驗
    ("騎腳踏車", r"騎(?:了|完)?(?:腳踏車|單車|自行車|[Yy]ou[Bb]ike|[Uu]bike)"),
    ("跳舞", r"跳(?:了|完)?(?:廣場|土風|排|國標|社交|交際)?舞"),
    ("做操", r"做(?:了|完)?(?:體|早|健康|晨)?操|(?:[早晨]|健康)操(?:也|都)?做(?:了|完|好)"),
    ("甩手", r"甩手|平甩"),
    ("拉筋", r"拉筋"),
    ("瑜伽", r"瑜[伽珈]"),
    ("重訓", r"重訓"),
    ("健身", r"健身"),
    # 「半夜跑了三次廁所」「清晨跑去公園」「晨跑鞋」不是跑步
    ("跑步", r"跑步(?!機)|慢跑|(?<![清凌])晨跑(?![鞋機]|去(?!了)|.{0,4}廁)|(?<![半深昨整熬一每])夜跑(?![鞋機]|去(?!了)|.{0,4}廁)"),
    ("打球", r"打(?:了|完)?(?:桌球|羽毛球|羽球|網球|籃球|排球|棒球|高爾夫球?|門球|槌球|保齡球|撞球|匹克球|球)"),
    ("運動", r"運動(?![鞋服會員品褲衣場神])"),
)

_SLEEP_PATTERNS: tuple[tuple[str, str], ...] = (
    ("睡不好",
     r"睡不(?:太)?(?:好|著|安穩)|沒睡(?:好|飽)"
     r"|睡得?(?:很|好|超|蠻|滿|有點|不太)?(?:不好|差|糟|淺|不安穩)"
     r"|失眠|一直醒|半夜(?:都|會)?醒|很早(?:就)?醒|淺眠"
     r"|只睡(?:了)?\s{0,2}[0-9一二兩三四五六七八九十半]{1,3}\s{0,2}(?:個)?(?:多)?(?:小時|鐘頭)"),
    # 「睡得好晚／好少／好不好」的「好」是程度詞或問句，不算睡得好
    # 沒有「得」時一定要有程度詞：「睡很好」算，「睡好晚」不算
    ("睡得好", r"睡(?:得(?:很|蠻|滿|還|超|挺|好)?|很|蠻|滿|超|挺)(?:好|飽|熟|香|不錯)(?![晚少不嗎吧差糟爛短淺])"
              r"|睡飽(?![嗎吧沒])"),
)

_DIET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("胃口不好", r"沒(?:有|什麼|甚麼)?胃口|胃口(?:很|有點|變得?|超|好)?(?:不好|差)|吃不下"),
    ("沒吃早餐", r"沒(?:有)?吃早(?:餐|飯)|早(?:餐|飯)(?:也|都)?沒(?:有)?吃"),
    ("沒吃午餐", r"沒(?:有)?吃午(?:餐|飯)|午(?:餐|飯)(?:也|都)?沒(?:有)?吃"),
    ("沒吃晚餐", r"沒(?:有)?吃晚(?:餐|飯)|晚(?:餐|飯)(?:也|都)?沒(?:有)?吃"),
    ("沒吃飯", r"沒(?:有)?吃飯"),
    # 要有「吃」：「少糖少冰」是點飲料
    ("少吃甜", r"少吃(?:一點|點)?(?:甜|糖)"),
    ("少吃鹽", r"少吃(?:一點|點)?(?:鹽|鹹)"),
    ("少吃油", r"少吃(?:一點|點)?油"),
    ("戒糖", r"戒(?:了)?(?:糖|甜食)"),
    ("戒酒", r"戒(?:了)?酒"),
    ("吃清淡", r"吃(?:得)?(?:很|比較|蠻|滿)?清淡"),
    ("控制飲食", r"控制飲食|飲食控制"),
    ("喝酒", r"喝(?:了|完|過)?(?:一點|一些|點|些|[0-9一二兩三四五六七八九十幾半]{1,2}(?:杯|瓶|罐|口))?"
             r"(?:啤酒|紅酒|白酒|高粱|威士忌|米酒|清酒|調酒|小酒|酒)(?![精店吧])"),
)
_DIET_NEGATIVE = frozenset({"胃口不好", "沒吃早餐", "沒吃午餐", "沒吃晚餐", "沒吃飯"})  # 本身就是否定，不再看前面的「沒」

# 一般症狀；緊急字詞（胸悶、跌倒…）不在這裡，被排除的緊急字詞因此不會變成一般紀錄
_SYMPTOM_PATTERNS: tuple[tuple[str, str], ...] = (
    ("頭暈", r"頭(?:好|很|有點|一直)?(?:暈|昏)(?![過厥車船])|暈眩|眩暈"),
    ("頭痛", r"頭(?:好|很|有點|一直)?(?:痛|疼)"),
    ("發燒", r"發(?:高)?燒"),
    ("咳嗽", r"咳嗽|一直咳"),
    ("感冒", r"感冒"),
    ("喉嚨痛", r"喉嚨(?:好|很|有點)?痛"),
    ("流鼻水", r"流鼻水|鼻塞"),
    ("膝蓋痛", r"膝蓋(?:好|很|有點|一直)?(?:痛|痠|酸)"),
    ("腰痛", r"腰(?:好|很|有點|一直)?(?:痛|痠|酸)"),
    ("背痛", r"背(?:好|很|有點)?痛"),
    ("肩膀痛", r"肩(?:膀|頸)(?:好|很|有點|一直)?(?:痛|痠|酸)|五十肩"),
    ("腳痛", r"腳(?:好|很|有點|一直)?痛"),
    ("手腳麻", r"(?:手|腳)(?:好|很|有點|一直)?麻(?![煩將醉辣油糬雀])"),
    ("胃痛", r"胃(?:好|很|有點)?痛|胃不舒服"),
    ("肚子痛", r"肚子(?:好|很|有點)?痛"),
    ("拉肚子", r"拉肚子|腹瀉"),
    ("便秘", r"便秘"),
    ("牙痛", r"牙(?:齒)?(?:好|很|有點)?痛"),
    ("耳鳴", r"耳鳴"),
    ("心悸", r"心悸"),
    ("噁心", r"噁心|想吐"),
    ("水腫", r"水腫|腳腫"),
    ("抽筋", r"抽筋"),
    ("皮膚癢", r"皮膚(?:好|很)?癢"),
    ("過敏", r"過敏"),
    ("痛風", r"痛風"),
    ("關節痛", r"關節(?:好|很)?痛"),
    ("骨折", r"骨折"),
    ("血壓高", r"血壓(?:好|很|有點|偏|太)?高|高血壓"),
    ("血壓低", r"血壓(?:好|很|有點|偏|太)?低|低血壓"),
    ("血糖高", r"血糖(?:好|很|有點|偏|太)?高"),
)

_READING_LABELS = ("血壓", "血糖", "體重", "心跳")

# 緊急字詞（PLAN §3.4＋GP1#2 的間隔字：「胸口很悶」「左手沒力」「半邊沒力」「嘴有點歪」）。
# 關鍵字中間可夾最多 3 個程度／時間副詞（「胸口又痛了」「胸口還是很痛」「胸口有一點悶」）；
# 「不」「沒」不在裡面，所以「胸口不痛」「胸口也沒有很悶」本來就比不到。
_GAP = r"(?:好像|有一點|越來越|還在|還是|一點|有點|一直|突然|開始|真的|變得|又|還|也|都|好|很|超)"
_NUMB = r"麻(?![煩將醉辣油糬雀])"  # 「右手好麻煩」「麻將」不是麻
_RED_FLAG_PATTERNS: tuple[tuple[str, str], ...] = (
    ("胸痛胸悶",
     rf"胸(?:口|部)?{_GAP}{{0,3}}(?:刺痛|痛|悶|緊|不舒服)"
     rf"|心口{_GAP}{{0,3}}痛|心絞痛|心臟{_GAP}{{0,3}}(?:痛|不舒服)"),
    ("呼吸困難",
     rf"喘不過(?:氣|來)|喘不上來|呼吸{_GAP}{{0,3}}(?:困難|急促)|呼吸不過來|吸不到氣"
     r"|無法呼吸|不能呼吸|沒辦法呼吸"),
    ("嘴歪或說話不清",
     rf"嘴(?:巴|角)?{_GAP}{{0,3}}(?:歪|斜)|臉{_GAP}{{0,3}}(?:歪|斜)"
     rf"|口齒不清|(?:說|講)話{_GAP}{{0,3}}(?:不清|含糊|不輪轉)|(?:說|講)不出話|大舌頭"),
    ("單側無力",
     rf"(?:左|右|半|一)邊(?:的)?(?:手腳|手臂|手|腳|身體|身|臉)?{_GAP}{{0,3}}"
     rf"(?:沒力|無力|{_NUMB}|不能動|動不了|舉不起來|沒感覺)"
     rf"|(?:左|右)(?:手腳|手臂|手|腳|半身){_GAP}{{0,3}}(?:沒力|無力|{_NUMB}|不能動|動不了|舉不起來|沒感覺)"
     rf"|半身(?:{_NUMB}|無力|不能動|不遂)"),
    ("中風", r"中風(?!險)"),
    ("心臟病發", r"心肌梗塞|心梗|心臟病發"),
    # 「頭暈倒在沙發上」是頭暈＋躺下，不是昏倒
    ("昏倒", r"(?<!頭)(?:昏|暈)(?:倒|過去|厥)|失去意識|叫不醒|昏迷|抽搐"),
    ("跌倒", r"(?:跌|摔|滑)了?一?(?:跤|倒)|摔下(?:來|去)?|跌下(?:來|去)|跌坐"),
    ("急診", r"急診(?!費)"),
    ("救護車", r"救護車(?!的?(?:聲|經過|開過))"),
    ("送醫", r"送醫|送(?:到|去|進)了?醫院"),
    ("住院", r"住院(?![部處費險])"),
)

# 給 might_have_red_flag：每個緊急字詞 pattern 的任何命中都一定含其中一個（fuzz 測試驗證）
_RED_FLAG_HINTS: tuple[str, ...] = (
    "胸", "心口", "心絞", "心臟", "心肌", "心梗", "喘", "呼吸", "吸不到", "嘴", "臉", "口齒", "說話",
    "講話", "不出話", "舌頭", "邊", "左", "右", "半身", "中風", "昏", "暈", "意識", "叫不醒", "抽搐",
    "跌", "摔", "滑", "急診", "救護車", "送醫", "醫院", "住院",
)

ALLOWED_ITEMS: dict[str, frozenset[str]] = {
    "med": frozenset({"吃藥", "忘了吃藥", "拿藥", "停藥"}),
    "visit": frozenset(label for label, _ in _VISIT_PATTERNS),
    "exercise": frozenset(label for label, _ in _EXERCISE_PATTERNS),
    "sleep": frozenset(label for label, _ in _SLEEP_PATTERNS),
    "diet": frozenset(label for label, _ in _DIET_PATTERNS),
    "body": frozenset({*_READING_LABELS, *(label for label, _ in _SYMPTOM_PATTERNS), *RED_FLAG_LABELS}),
}


def _combine(patterns: tuple[tuple[str, str], ...]) -> tuple[re.Pattern[str], dict[str, str]]:
    """(標籤, pattern) 併成一個 regex；回 (regex, 群組名→標籤)。輸出的標籤只會來自這張表。"""
    groups: dict[str, str] = {}
    parts: list[str] = []
    for index, (label, pattern) in enumerate(patterns):
        name = f"g{index}"
        groups[name] = label
        parts.append(f"(?P<{name}>{pattern})")
    return re.compile("|".join(parts)), groups


_VISIT_RE, _VISIT_GROUPS = _combine(_VISIT_PATTERNS)
_EXERCISE_RE, _EXERCISE_GROUPS = _combine(_EXERCISE_PATTERNS)
_SLEEP_RE, _SLEEP_GROUPS = _combine(_SLEEP_PATTERNS)
_DIET_RE, _DIET_GROUPS = _combine(_DIET_PATTERNS)
_SYMPTOM_RE, _SYMPTOM_GROUPS = _combine(_SYMPTOM_PATTERNS)
_RED_FLAG_RE, _RED_FLAG_GROUPS = _combine(_RED_FLAG_PATTERNS)

# 用藥：(吃|服|吞)＋0–2 字（不含飯要該得）＋藥，或中間是常見的藥名詞（「吃了降血壓藥」「吃降血糖的藥」）；
# 主題化「藥吃了」；忘了吃藥；拿藥；停藥
_MED_KIND = r"(?:降血壓|降血糖|降膽固醇|血壓|血糖|糖尿病|心臟|慢性病|感冒|止痛|安眠|助眠|過敏|胃|中|西)的?"
_MED_RE = re.compile(
    r"(?P<forgot>(?:忘了|忘記)要?(?:吃|服|吞)[^藥]{0,6}?藥(?![膳局房妝材酒])|藥(?:也|都|又)?(?:忘了|忘記)(?:吃|服))"
    r"|(?P<took>(?:吃|服|吞)(?:了|過|完)?(?:[一兩二三四五半幾](?:顆|包|粒|錠|片|匙))?"
    r"(?:" + _MED_KIND + r"|[^飯要該得農藥炸火]{0,2})藥(?![膳局房妝材酒行店燉])"
    r"|藥(?:也|都|已經|已|剛剛|剛才)?(?:按時|準時|乖乖)?(?:吃|服)(?:了|過|完|好)"
    r"|藥(?:也|都|已經|已)?有(?:按時|準時|乖乖)?(?:吃|服)(?!的|就好))"  # 「藥有吃的跟擦的」「藥有吃就好」不算
    r"|(?P<fetch>(?:拿|領)(?:了|完|好)?(?:慢性處方箋|慢箋|藥)(?![膳局房妝材酒]))"
    r"|(?P<stop>停(?:了)?藥)"
)
_MED_LABELS = {"forgot": "忘了吃藥", "took": "吃藥", "fetch": "拿藥", "stop": "停藥"}

# 步數：「走了 8000 步」「步數 6500」「一萬步」「五千步」→ 散步＋N步
_STEPS_RE = re.compile(
    r"(?P<arabic>(?<![0-9])(?P<n>[0-9]{3,6})(?![0-9.])\s{0,2}多?\s{0,2}步(?!數))"
    r"|(?P<count>步數\s{0,2}(?:是|有|:)?\s{0,2}(?P<n2>[0-9]{3,6})(?![0-9]))"
    r"|(?P<wan>(?P<w>[一二兩三四五六七八九])?萬(?:(?P<wk>[一二兩三四五六七八九])千)?多?步(?!數))"
    r"|(?P<qian>(?P<k>[一二兩三四五六七八九])千(?:(?P<kh>[一二兩三四五六七八九])百)?多?步(?!數))"
)
_CN_DIGITS = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# 讀數（只抓 0-9；範圍外的丟掉）
_BP_CUE_RE = re.compile(r"血壓|高壓|收縮壓")
_OTHER_READING_CUE_RE = re.compile(r"血糖|體重|心跳|脈搏")
_BP_PAIR_RE = re.compile(r"(?<![0-9/])([0-9]{2,3})\s{0,2}/\s{0,2}([0-9]{2,3})(?![0-9/])")
_BP_HIGH_LOW_RE = re.compile(
    r"(?:高壓|收縮壓)[^0-9]{0,3}?([0-9]{2,3})(?![0-9])[^0-9]{0,4}?(?:低壓|舒張壓)[^0-9]{0,3}?([0-9]{2,3})(?![0-9])"
)
_SINGLE_READINGS: tuple[tuple[str, re.Pattern[str], float, float], ...] = (
    ("血糖", re.compile(r"血糖(?:值)?[^0-9心血體脈]{0,4}?([0-9]{2,3})(?![0-9/])(?!\.[0-9])"), 40, 500),
    ("體重", re.compile(r"體重[^0-9心血體脈]{0,4}?([0-9]{2,3}(?:\.[0-9]{1,2})?)(?![0-9])(?!\s{0,2}(?:台斤|斤|磅))"),
     30, 150),
    ("心跳", re.compile(r"(?:心跳|脈搏)[^0-9心血體脈]{0,4}?([0-9]{2,3})(?![0-9/])(?!\.[0-9])"), 30, 200),
)
_BP_SYSTOLIC = (70, 250)
_BP_DIASTOLIC = (40, 150)
# 「血壓要控制在 130/80 以下」是目標，不是讀數
_THRESHOLD_BEFORE_RE = re.compile(r"超過|高於|低於|控制在|維持在|目標|標準|正常值|以上|以下")
_THRESHOLD_AFTER_RE = re.compile(r"\s{0,2}(?:以下|以上|以內)")


# ─────────────────────────────────────────────────────────────────────────────
# 子句線索：一次掃完；字詞 → 類別（同一位置先試長的：「了沒」先於「了」）
# ─────────────────────────────────────────────────────────────────────────────

_QUESTION = "question"      # 問句（緊急字詞不受影響）
_FUTURE = "future"          # 未來／計畫
_HYPO = "hypo"              # 假設
_ADVICE = "advice"          # 勸告
_REPORTED = "reported"      # 轉述／常識
_ARTICLE = "article"        # 轉貼文章類：後面沒主詞的子句也不算爸媽
_INSTRUCT = "instruct"      # 指示（提醒、叫、請…）：只管一般紀錄
_HEDGE = "hedge"            # 差點、怕、預防…
_HABIT = "habit"            # 容易、常常…：習慣／傾向不是當下狀況
_PAST = "past"              # 很久以前
_NOW = "now"                # 現在／最近：蓋過前一個子句的「很久以前」
_RESOLVED = "resolved"      # 已經好了
_MARKER = "marker"          # 家人轉述緊急狀況時的報告標記
_ANCHOR = "anchor"          # 早上／昨天…：看診、運動算已經發生
_ANCHOR_STEPS = "anchor_steps"  # 步數另外也接受「今天」
_BLOCK = "block"            # 「的時候／時／就／結果」：前面的未來、勸告詞管不到後面的事

_CUE_ENTRIES: tuple[tuple[str, str | None, str], ...] = (
    # (字詞, 有前後文限制時的 regex, 類別)
    ("有沒有", None, _QUESTION), ("是不是", None, _QUESTION), ("要不要", None, _QUESTION),
    ("了沒", r"了沒(?=有?[嗎呢啊啦喔]?$)", _QUESTION),  # 句尾才是問句：「吃藥了沒」；「跌倒了沒什麼事」不是
    ("了嗎", None, _QUESTION), ("好不好", None, _QUESTION),
    ("可不可以", None, _QUESTION), ("能不能", None, _QUESTION), ("對不對", None, _QUESTION),
    ("會不會", None, f"{_QUESTION} {_HABIT}"),
    ("要去", None, _FUTURE), ("等一下", None, _FUTURE), ("待會", None, _FUTURE), ("明天", None, _FUTURE),
    ("後天", None, _FUTURE), ("下週", None, _FUTURE), ("下禮拜", None, _FUTURE), ("下星期", None, _FUTURE),
    ("下個月", None, _FUTURE), ("下次", None, _FUTURE), ("晚點", None, _FUTURE), ("晚一點", None, _FUTURE),
    ("準備", None, _FUTURE), ("打算", None, _FUTURE), ("想去", None, _FUTURE), ("預計", None, _FUTURE),
    ("預約", None, _FUTURE), ("約了", None, _FUTURE), ("約好", None, _FUTURE), ("掛號", None, _FUTURE),
    ("改天", None, _FUTURE),
    ("如果", None, _HYPO), ("萬一", None, _HYPO), ("假如", None, _HYPO), ("假設", None, _HYPO),
    ("的話", None, _HYPO), ("要是", r"(?<![主只需重])要是", _HYPO),
    ("記得", None, f"{_ADVICE} {_INSTRUCT}"), ("應該", None, _ADVICE), ("最好", None, _ADVICE),
    ("建議", None, _ADVICE), ("要多", None, _ADVICE), ("要少", None, _ADVICE),
    ("不要", r"不要(?!緊)", _ADVICE),
    ("別再", r"(?<![特分區個級])別再", _ADVICE), ("別忘", r"(?<![特分區個級])別忘", _ADVICE),
    ("小心", r"(?<!不)小心", _ADVICE),  # 「不小心跌倒」是真的跌倒
    ("注意", r"(?<![沒不])注意(?!到)", _ADVICE),
    ("醫生說", None, _REPORTED), ("醫師說", None, _REPORTED), ("不一定", None, _REPORTED),
    ("可能是", None, _REPORTED),
    ("新聞", None, f"{_REPORTED} {_ARTICLE}"), ("報導", None, f"{_REPORTED} {_ARTICLE}"),
    ("報道", None, f"{_REPORTED} {_ARTICLE}"), ("研究", None, f"{_REPORTED} {_ARTICLE}"),
    ("文章", None, f"{_REPORTED} {_ARTICLE}"), ("專家", None, f"{_REPORTED} {_ARTICLE}"),
    ("網路上", None, f"{_REPORTED} {_ARTICLE}"), ("聽說", None, f"{_REPORTED} {_ARTICLE}"),
    ("徵兆", None, f"{_REPORTED} {_ARTICLE}"), ("症狀", None, f"{_REPORTED} {_ARTICLE}"),
    ("提醒", None, _INSTRUCT), ("催", None, _INSTRUCT), ("幫", None, _INSTRUCT), ("叫", None, _INSTRUCT),
    ("請", r"(?<!申)請(?!假)", _INSTRUCT),
    ("差點", None, _HEDGE), ("快要", None, _HEDGE), ("害怕", None, _HEDGE), ("擔心", None, _HEDGE),
    ("預防", None, _HEDGE), ("避免", None, _HEDGE), ("以免", None, _HEDGE), ("免得", None, _HEDGE),
    ("怕", r"(?<![可恐])怕", _HEDGE), ("防", r"(?<![消國堤邊])防", _HEDGE),
    ("容易", None, _HABIT), ("常常", None, _HABIT), ("經常", None, _HABIT), ("老是", None, _HABIT),
    ("偶爾", None, _HABIT), ("有時候", None, _HABIT), ("一陣子會", None, _HABIT), ("總是", None, _HABIT),
    ("時常", None, _HABIT), ("動不動", None, _HABIT),
    ("上個月", None, _PAST), ("上週", None, _PAST), ("上禮拜", None, _PAST), ("上星期", None, _PAST),
    ("上次", None, _PAST), ("去年", None, _PAST), ("前年", None, _PAST), ("幾年前", None, _PAST),
    ("多年前", None, _PAST), ("很久", None, _PAST),
    # 「睡覺之前」「吃飯以前」是時間點，不是很久以前
    ("之前", r"(?<![覺睡飯餐藥門澡床動步路班家發診刀術吃喝走])之前", _PAST),
    ("以前", r"(?<![覺睡飯餐藥門澡床動步路班家發診刀術吃喝走])以前", _PAST),
    ("今天", None, f"{_NOW} {_MARKER} {_ANCHOR_STEPS}"),
    ("今早", None, f"{_NOW} {_MARKER} {_ANCHOR} {_ANCHOR_STEPS}"),
    ("早上", None, f"{_NOW} {_MARKER} {_ANCHOR} {_ANCHOR_STEPS}"),
    ("昨天", None, f"{_NOW} {_MARKER} {_ANCHOR} {_ANCHOR_STEPS}"),
    ("昨晚", None, f"{_NOW} {_MARKER} {_ANCHOR} {_ANCHOR_STEPS}"),
    ("今晚", None, f"{_NOW} {_MARKER}"), ("現在", None, f"{_NOW} {_MARKER}"),
    ("剛剛", None, f"{_NOW} {_MARKER}"), ("剛才", None, f"{_NOW} {_MARKER}"), ("剛", None, f"{_NOW} {_MARKER}"),
    ("突然", None, f"{_NOW} {_MARKER}"), ("最近", None, f"{_NOW} {_MARKER}"),
    ("這幾天", None, f"{_NOW} {_MARKER}"), ("又", None, f"{_NOW} {_MARKER}"), ("一直", None, f"{_NOW} {_MARKER}"),
    ("還在", None, f"{_NOW} {_MARKER}"), ("正在", None, f"{_NOW} {_MARKER}"),
    ("沒事了", None, _RESOLVED), ("已經沒事", None, _RESOLVED), ("好多了", None, _RESOLVED),
    ("已經好了", None, _RESOLVED), ("都好了", None, _RESOLVED), ("早就好了", None, _RESOLVED),
    ("也好了", None, _RESOLVED), ("不痛了", None, _RESOLVED), ("不悶了", None, _RESOLVED),
    ("恢復了", None, _RESOLVED), ("還好沒事", None, _RESOLVED), ("沒什麼事", None, _RESOLVED),
    ("沒什麼大礙", None, _RESOLVED), ("沒大礙", None, _RESOLVED), ("不要緊", None, _RESOLVED),
    ("沒受傷", None, _RESOLVED), ("沒怎樣", None, _RESOLVED),
    ("沒事", r"沒事(?![做幹情])", _RESOLVED),  # 「沒事做」不是好了
    ("還好", r"還好(?![^了啦喔呀啊])", _RESOLVED),  # 「今天還好」算；「還好你在」「還好痛」不算
    ("現在沒事", None, f"{_RESOLVED} {_NOW} {_MARKER}"), ("現在好了", None, f"{_RESOLVED} {_NOW} {_MARKER}"),
    ("了", r"(?<![為除])了(?!解)", _MARKER), ("在", None, _MARKER), ("有點", None, _MARKER),
    ("好像", None, _MARKER), ("開始", None, _MARKER), ("變得", None, _MARKER), ("怎麼", None, _MARKER),
    ("上午", None, f"{_ANCHOR} {_ANCHOR_STEPS}"), ("一早", None, f"{_ANCHOR} {_ANCHOR_STEPS}"),
    ("清晨", None, f"{_ANCHOR} {_ANCHOR_STEPS}"), ("前天", None, f"{_ANCHOR} {_ANCHOR_STEPS}"),
    ("今日", None, _ANCHOR_STEPS),
    ("的時候", None, _BLOCK), ("時", r"時(?![常候間])", _BLOCK), ("結果", None, _BLOCK),
    ("就", r"就(?![要會得可能去])", _BLOCK),  # 「要去廁所就跌倒了」；「明天就要住院」還是未來
    ("就好了", None, _RESOLVED), ("好了", r"(?<=[痛悶麻暈緊])好了", _RESOLVED),  # 「胸口痛好了」「痛了一下就好了」
)
_CUE_CATEGORIES: dict[str, frozenset[str]] = {word: frozenset(cats.split()) for word, _, cats in _CUE_ENTRIES}
_CUE_RE = re.compile(
    "(?P<lit>"
    + "|".join(rx or re.escape(word) for word, rx, _ in sorted(_CUE_ENTRIES, key=lambda e: -len(e[0])))
    + r")|(?P<ago>[0-9一二兩三四五六七八九十幾]{1,3}(?:年|個月|個禮拜|個星期|週)前)"
)
_AGO_CATEGORIES = frozenset({_PAST})
_A_NOT_A_RE = re.compile(r"([\u4e00-\u9fff])不\1")  # 痛不痛、去不去
_QUESTION_TAIL = frozenset("嗎呢吧沒没")
# 台式問句尾（「爸爸藥有吃齁」）：只讓「吃了藥」和運動不記；不放進 _QUESTION_TAIL（會改 119 的主詞與未來判斷）。
# 這些字也是一般的字（「穿得很厚」「在乎」）：只會漏記，可以接受；「喔」「啦」可問可答，不擋
_TAG_QUESTION_TAIL = frozenset("齁吼厚乎蛤ㄇ")
# 問句尾自己成一個子句（「爸爸藥吃了，對吧」）：前一個子句也當成問句
_TAG_QUESTION_WORDS = frozenset({"對吧", "對嗎", "是吧", "對不對", "是不是", "是嗎", "有嗎", "沒有嗎"})  # 不放「好嗎」：「我藥吃了，好嗎」是回嘴

# 視窗檢查（都只看固定長度的前後文）
_DONE_RE = re.compile(r"(?<![為除])了(?!解)|(?<!不)過(?![敏來去])|完(?!全)|(?<!而)已|剛(?!好)"
                      r"|(?<![沒没])有(?![點些沒人時機閒用效益]|空(?!腹)|幫助)|回來|回家")
_NEG_RE = re.compile(r"[沒没未]|不(?![好舒適錯過])")
_NEG_MED_RE = re.compile(r"[沒没未忘]|不(?![好舒適錯過])")
_MUST_BEFORE_RE = re.compile(r"(?:要|該|(?<!覺)得|需要|必須)(?:去|有|確定|確認)?$")
# 「要確定爸爸藥有吃」「要先確認一下藥有吃」：叫人確認，中間可以夾字（每個子句只找一次第一個位置）
_CHECK_BEFORE_RE = re.compile(r"(?:要|該|(?<!覺)得|需要|必須)[先再]?確[定認]|不確[定認]")  # 「我不確定爸爸藥有吃」是擔心
# 「好久沒有出去走走了」：否定詞被「出去」擠出 3 字視窗，前綴前面再看一次
_GO_PREFIX_RE = re.compile(r"出?去$")
# 「好想出去走走了」「可以出去走走了」：想做、能做，不是做了
_MODAL_BEFORE_RE = re.compile(r"(?:想|要|可以|(?<!只)能)出?去?$")  # 「痛到只能去急診了」是去了
_RESULT_AFTER_RE = re.compile(r"的?(?:報告|結果)")
_WALK_CUE_RE = re.compile(r"走|散步|步行")
_RECOVERED_RE = re.compile(r"好了|好多了|沒事|不痛了|退了|改善|恢復")
# 「年紀大會跌倒」「運動會喘」「喝酒會胸悶」「在家會跌倒」：會＝傾向；「在教會」「去參加聚會」「在里民大會」的會是去的地方
_TENDENCY_RE = re.compile(r"(?<![教聚開社協廟])會(?:一直|有點|再|又|突然|很)?$")
# 只認這些聚會（「在家會跌倒」「去爬山會喘」的會還是傾向）；「X大會」最多兩個字（里民大會、住戶大會）；
# 年會只認「在／參加」，「去年會」的去年是時間
_MEETING_RE = re.compile(r"(?:(?:在|去|參加|從|於)(?:[\u4e00-\u9fff]{0,2}大會|教會|聚會|同學會|家長會|廟會|運動會|老人會|晚會|餐會|舞會)"
                         r"|(?:在|參加)年會)$")
# 其他聚會名稱也是地方（法會、讀書會、演唱會、農會…）：只認這張表、而且要緊接在 在／去／參加 後面；
# 不用「X會」通則：「我在半夜會胸痛」「我去上班會胸悶」的會是傾向，通則會把它們當成地方而誤報 119（第六輪 GP2）
_EVENT_RE = re.compile(r"(?:在|去|參加|從|於)(?:法會|園遊會|演唱會|音樂會|讀書會|農會|說明會|座談會|同鄉會|校友會|博覽會|展覽會|研討會"
                       r"|茶會|酒會|慶生會|追思會|聯誼會|燈會|宗親會|禱告會|早會|同樂會|聯歡會|宴會|生日會)$")
_EMOTION_DEGREE_RE = re.compile(r"(?:笑|哭|氣|嚇)(?:到|得)(?:快要|快|要)?$")  # 「笑到肚子痛」不是症狀
# 緊急字詞專用
_RF_NEGATED_RE = re.compile(r"(?:沒有|没有|沒|没|不會|不再|不用|不需要|不必|未|無|不)(?:有|會|再|太|是|什麼|甚麼|去|用)?$")
# 程度補語：「忙到喘不過氣」「笑到快昏倒」「熱到快中暑昏倒」（到／得和關鍵字之間只能是這些字）；
# 「覺得」「看到」不是程度補語，「走到門口跌倒」「到現在胸口一直痛」中間的字不在清單裡
# 程度補語：到／得和關鍵字之間只能是這些字。只有情緒、忙累、熱或運動到…（笑哭氣忙累熱嚇、跑爬走），
# 或中間有「快／要／差點／幾乎」才排除：「忙到喘不過氣」「跑到喘不過氣」「笑到快昏倒」「熱到快中暑昏倒」排除；
# 「痛到直接昏過去」「暈到跌倒」「滑到跌倒」「喘到說不出話」是真的
_DEGREE_RE = re.compile(r"(?:到|得)((?:快要|快|要|差點|幾乎|直接|就|中暑){0,3})$")
_DEGREE_CAUSES = frozenset("笑哭氣忙累熱嚇跑爬走")
_DEGREE_HEDGES = ("快", "要", "差點", "幾乎")
_GENERIC_AFTER_RE = re.compile(
    r"的?(?:人|病人|患者|風險|機率|原因|前兆|徵兆|症狀|問題|機會|新聞|案例|影片|文章|後遺症|危險|可能|防|預防"
    r"|費用|錢|單據|收據|帳單|遺傳|家族史|講座|課程)|(?:很|好|超|太|最)?(?:危險|可怕)"
)
_LONG_AFTER_RE = re.compile(r"後|以後|之後")  # 「中風後一直在復健」：中風是以前的事
_FORMER_RE = re.compile(r"[\u4e00-\u9fff]{0,2}的")  # 「以前的同事」「以前公司的同事」：以前是修飾人
_INSURANCE_RE = re.compile(r"保險|理賠|保單|保費")  # 「住院險」「理賠住院」講的是保險
_REDUPLICATED_RE = re.compile(r"([\u4e00-\u9fff])\1的")  # 「歪歪的」「悶悶的」：描述現在的樣子
_STOCK_BEFORE_RE = re.compile(r"[股價盤]|指數")
_STOCK_AFTER_RE = re.compile(r"到?(?:谷底|低點|新低|[0-9]|%|點)")
# 「手機摔下來」：東西緊接在「摔」前面才算；「買東西跌倒了」是人跌倒
_OBJECT_BEFORE_RE = re.compile(r"(?:手機|東西|杯子|碗|盤子|花瓶|電腦|平板|眼鏡|遙控器|鍋子|鍋|瓶子)$")
_CLOTHING_RE = re.compile(r"衣|褲|穿|胸罩|罩杯|尺寸")  # 「這件衣服胸部很緊」講的是衣服
_HANDLING_VERBS = frozenset("買拿搬收整提扛撿")  # 「買東西摔倒了」：東西是買的
# 緊急字詞前面可以先跳過的時間、副詞（「我外孫剛剛中風了」的「剛剛」）
_ADVERB_RUN_RE = re.compile(r"(?:剛剛|剛才|剛|今天|昨天|昨晚|前天|早上|上午|中午|下午|晚上|今早|突然|又|也|都|一直|已經|好像|最近"
                            r"|竟然|居然|就|還)*$")

_SHOULDER_RE = re.compile(r"五十肩|肩膀|肩頸")
_INTENSIFIER_RE = re.compile(r"好|很|有點|有一點|一直|突然|超|又|還在|還是|開始|越來越|真的")
# 轉述：「爸爸剛剛打電話來說她…」「傳訊息說」；「小說」「說明」「講座」「演講」「遺傳」「傳染」不是
_SPEECH_RE = re.compile(r"(?<!小)說(?![明服])|(?<!演)講(?![座課義])|告訴|打給|打電話|傳(?:訊息|簡訊|LINE|line|Line|話|來)")
_HABIT_WINDOW = 6  # 習慣詞要在緊急字詞前 6 字內


# ─────────────────────────────────────────────────────────────────────────────
# 稱謂與主詞
# ─────────────────────────────────────────────────────────────────────────────

# 稱謂 → (家人說, 爸爸說, 媽媽說)：F 爸爸、M 媽媽、B 兩位、S 說話者本人、O 別人；
# 「^」＝只有在子句開頭才算（爸爸說「老婆今天有去走路」）。
_TITLE_KINDS: dict[str, tuple[str, str, str]] = {
    "爸爸": ("F", "F", "F"), "爸": ("F", "O", "O"), "老爸": ("F", "O", "O"), "父親": ("F", "O", "O"),
    "我爸": ("F", "O", "O"), "我爸爸": ("F", "O", "O"),
    "你爸": ("F", "F", "F"), "你爸爸": ("F", "F", "F"), "你們爸": ("F", "F", "F"),
    "你們爸爸": ("F", "F", "F"), "你們的爸爸": ("F", "F", "F"), "把拔": ("F", "F", "F"), "爸比": ("F", "F", "F"),
    "媽媽": ("M", "M", "M"), "媽": ("M", "O", "O"), "老媽": ("M", "O", "O"), "母親": ("M", "O", "O"),
    "我媽": ("M", "O", "O"), "我媽媽": ("M", "O", "O"),
    "你媽": ("M", "M", "M"), "你媽媽": ("M", "M", "M"), "你們媽": ("M", "M", "M"),
    "你們媽媽": ("M", "M", "M"), "你們的媽媽": ("M", "M", "M"), "馬麻": ("M", "M", "M"), "媽咪": ("M", "M", "M"),
    "爸爸媽媽": ("B", "B", "B"), "媽媽爸爸": ("B", "B", "B"), "爸媽": ("B", "O", "O"), "我爸媽": ("B", "O", "O"),
    "你爸媽": ("B", "B", "B"), "你們爸媽": ("B", "B", "B"), "父母": ("B", "O", "O"),
    "我老公": ("O", "O", "F"), "我先生": ("O", "O", "F"), "我老伴": ("O", "M", "F"),
    "我老婆": ("O", "M", "O"), "我太太": ("O", "M", "O"), "我丈夫": ("O", "O", "F"), "我妻子": ("O", "M", "O"),
    "老公": ("O", "O", "F^"), "老伴": ("O", "M^", "F^"), "老婆": ("O", "M^", "O"), "太太": ("O", "M^", "O"),
    "丈夫": ("O", "O", "F^"), "妻子": ("O", "M^", "O"),
    "我": ("S", "S", "S"),
}
# 別人：親戚（姑媽、阿媽、外婆…要整個詞吃掉，不然單字「媽」會被當成媽媽）、朋友、職業、動物、代名詞…
_OTHER_WORDS = frozenset({
    "姑媽", "姨媽", "舅媽", "乾媽", "大媽", "伯母", "嬸嬸", "阿姨", "姑姑", "舅舅", "叔叔", "伯伯", "阿伯", "阿桑",
    "阿婆", "阿嬤", "阿媽", "阿公", "阿爸", "外公", "外婆", "爺爺", "奶奶", "祖父", "祖母", "公公", "婆婆", "岳父",
    "岳母", "乾爸", "姨丈", "姑丈", "哥哥", "弟弟", "姊姊", "姐姐", "妹妹", "大哥", "二哥", "大姊", "大姐", "二姊",
    "二姐", "兒子", "女兒", "孫子", "孫女", "女婿", "媳婦", "姪子", "姪女", "外甥", "表哥", "表姊", "表姐", "表弟",
    "表妹", "堂哥", "堂姊", "堂姐", "堂弟", "堂妹", "寶寶", "小孩", "孩子", "小朋友", "朋友", "同事", "鄰居",
    "同學", "老師", "醫生", "醫師", "護士", "護理師", "病人", "患者", "病患", "藝人", "明星", "主角", "演員",
    "角色", "網友", "路人", "客人", "老人家", "老人", "長輩", "老闆", "先生", "學生", "司機", "小姐", "店員",
    "員工", "警察", "師傅", "看護", "歐巴桑", "歐吉桑", "那個人", "這個人", "有個人", "有一個人", "一位", "其他人",
    "同梯", "親家", "好友", "男生", "女生", "長者", "記者",
    "房東", "股東", "志工", "社工", "義工", "主管", "上司", "下屬", "家屬", "客戶", "住戶", "里長", "鄰長", "家長",
    "院長", "校長", "班長", "組長", "社長", "會長", "學長", "董事長", "女士", "博士", "顧客", "乘客", "遊客", "叔公",
    "舅公", "伯公", "姊夫", "姐夫", "妹夫", "大夫", "嫂子", "男子", "女子",
    "貓", "貓咪", "寵物", "毛孩", "隔壁", "樓上", "樓下", "對面",
    "你老公", "你先生", "你老婆", "你太太", "你老伴", "你丈夫", "你妻子", "你們", "我們", "他們", "她們", "大家",
    "別人", "有人",
    "人家",
})
# 「隔壁老王」是人；「我在樓下跌倒了」的「樓下」是地點（前面是在／到／去…）
_PLACE_WORDS = frozenset({"隔壁", "樓上", "樓下", "對面"})
_LOCATIVES = frozenset("在到去從回於住")
_KIN_CHARS = "姊姐妹哥弟兄嫂舅姨姑叔伯孫"
_RANK_CHARS = "大二三四五六七八九十小"
# 常見姓氏：「王媽媽」「林爸爸」是別人家的家長。不含「連」（「連媽媽都跌倒了」）
_SURNAMES = "陳林黃張李王吳劉蔡楊許鄭謝郭洪曾邱廖賴周徐蘇葉莊呂江何蕭羅高潘簡朱鍾彭游詹胡施沈余盧梁趙顏柯翁魏孫戴范宋方鄧杜傅侯曹薛丁卓馬阮董唐温藍蔣石古紀姚馮歐程湯黎田康"
_SURNAME_NOT_AFTER = {"周": "這上下每本前後一", "謝": "謝", "許": "也或"}  # 這周媽媽、謝謝媽媽、也許爸爸
# 結構上的「別人」：短名詞＋表示人的字尾（牌友、師姐、外孫、親家母、主管、房東、志工…）。關係名詞列不完，所以看字尾。
_PERSON_FINAL = "友兄姐姊妹弟哥長管司屬戶東工師父母戚舅婆公子女孫兒婿夫媳伯叔姨姑嫂嬸娘婦客員士"
# 字尾一樣但不是人：身體、時間、東西、地方、副詞
_NOT_PERSON = (
    "肚子", "鼻子", "脖子", "頸子", "身子", "腦子", "嗓子", "腸子", "日子", "前陣子", "這陣子", "那陣子", "陣子",
    "下子", "輩子",
    "樣子", "房子", "車子", "椅子", "梯子", "桌子", "鞋子", "褲子", "被子", "杯子", "盤子", "瓶子", "箱子", "袋子",
    "帽子", "襪子", "鏡子", "位子", "法子", "點子", "種子", "葉子", "句子", "公司", "血管", "氣管", "水管", "不管",
    "儘管", "窗戶", "門戶", "帳戶", "金屬", "上工", "做工", "加工", "打工", "手工", "辦公", "功夫", "工夫", "這兒",
    "那兒", "哪兒", "會兒", "點兒", "巴士", "很長", "太長", "多長", "延長", "成長", "增長", "院子", "巷子", "村子",
    "亭子", "園子", "林子", "包子", "餃子", "粽子", "凳子", "竹子", "筷子", "墊子", "架子", "櫃子", "罐子", "案子",
    "桃子", "橘子", "李子", "豆子", "麥子", "稻子", "屏東", "台東", "臺東", "廣東", "山東",
)
# 人字開頭但整個詞不是人（「公車」「東西」「工作」）：字尾後面接這些就不是人名詞的結尾
_NOT_PERSON_TAIL = ("東西", "東部", "東區", "公車", "公園", "公司", "公寓", "公所", "公路", "公里", "公斤", "公分", "公共",
                    "公開", "工作", "工廠", "工地", "工程", "工具", "工資", "子宮", "客廳", "客房", "長大", "長途")
# 三個字的人名詞前面要是修飾字（好朋友、老戰友、前同事、親家母）；「買東西」「下公車」不是人
_NOUN_MODIFIERS = "老好前舊小大二三四五六七八九十表堂乾親男女師學同外繼養岳"
# 名詞不會用這些字開頭（介系詞、副詞、時間、動詞）：「我在公司」「我今天」「我一直」「我剛剛」不是人
_NOUN_NO_START = "搭坐買吃喝逛拿搬掃爬收整穿洗煮騎推的我你妳他她在從到去回於住跟和與及也都又就還一有沒不是會要剛今昨明早晚最已很真太超說叫讓被把對給向替幫陪帶送接看找餵遛抱載扶顧照等罵問請為想來出打"
_PERSON_FINAL_SET = frozenset(_PERSON_FINAL)
_NOT_PERSON_SET = frozenset(_NOT_PERSON)
# 至少兩個字（「我工作的時候」的「工」不是人）；稱謂（父母、丈夫、妻子、老公…）不算別人
_TITLE_LOOKAHEAD = "|".join(sorted((w for w in _TITLE_KINDS if len(w) > 1 and w[0] not in "我你"), key=len, reverse=True))
# 三字職稱＝兩個字＋員／師／士／工（管理員、清潔工、營養師）。動詞開頭的職稱（收銀員、推銷員、送貨員、搬運工、洗碗工、照服員、
# 會計師）也算，不受「名詞不會用動詞開頭」限制。不算：中間是完／了／過（收完工、下完工）、動詞＋本來就是人的詞（送老師、接醫師）、
# 做／打／當／上／下／開＋X＋工（做苦工、當義工）、X＋打／做／加…＋工（會打工）：這些是在做事
_JOB_FINAL = "員師士工"
_JOB_VERBS = "收推送搬洗掃照會接出"
_JOB_TAIL_WORDS = sorted(w for w in _OTHER_WORDS if len(w) == 2 and w[-1] in _JOB_FINAL)
_JOB_HEAD = (
    rf"(?![打做當上下開][\u4e00-\u9fff]工|[\u4e00-\u9fff][打做當上下開加趕收]工)"
    rf"(?:[{_JOB_VERBS}](?!{'|'.join(_JOB_TAIL_WORDS)})|(?![{_NOUN_NO_START}])[\u4e00-\u9fff])"
    rf"(?![完了過])[\u4e00-\u9fff](?=[{_JOB_FINAL}])"
)
_PERSON_NOUN = (
    rf"(?!{_TITLE_LOOKAHEAD})(?!{'|'.join(_NOT_PERSON)})"
    rf"(?:{_JOB_HEAD}|(?![{_NOUN_NO_START}])(?:[{_NOUN_MODIFIERS}][\u4e00-\u9fff]|[\u4e00-\u9fff]))"
    rf"(?!{'|'.join(_NOT_PERSON_TAIL)})[{_PERSON_FINAL}]"
)
_JOB_NOUN_RE = re.compile(rf"{_JOB_HEAD}[{_JOB_FINAL}]")
_OTHER_PATTERNS = (
    # 我姊、我三姊、你哥、他弟、她姊、我小妹、我舅媽（「我媽」「我爸」是稱謂，親戚字裡沒有爸媽）
    rf"(?:我們|你們|他們|她們|我|你|他|她)的?[{_RANK_CHARS}老]?[{_KIN_CHARS}][{_KIN_CHARS}子女丈媽]?",
    rf"[{_RANK_CHARS}][姊姐妹哥弟兄嫂伯叔姨舅姑][姊姐妹哥弟嫂]?",  # 三姊、小妹、二哥、大嫂、大伯、四姨
    rf"老[{_SURNAMES}]",  # 老王、老陳
    # 「看到一個人跌倒」「一個人在路上昏倒了」是別人；只有「一個人在家／去／住／待」是獨自（「（我／爸爸）一個人在家跌倒了」是自己），
    # 看到、遇到、發現…後面的「一個人」一定是別人
    r"(?<![我你他她己爸媽公婆])(?:(?<=看到|聽到|遇到|看見|碰到|見到|發現)一個人|一個人(?!在家|去|住|待))",
    r"(?<!熱)狗狗?",  # 狗；「熱狗」是吃的
    # 我師姐、他的牌友、她同梯、我外孫：我／你／他／她（的）＋以人字結尾的短名詞
    rf"(?:我們|你們|他們|她們|我|你|他|她)的?{_PERSON_NOUN}",
    # 句首、「的」「說」或並列後面的人：「親戚昏倒了」「爸爸的師姐」「爸爸說牌友」「跟師兄去爬山」
    rf"(?:^|(?<=[的說和跟與及:「])|(?<=看到|聽到|遇到|看見|碰到|見到|發現))(?!的){_PERSON_NOUN}",
)
_KIN_PREFIX = "姑舅姨嬸伯叔乾大阿外祖岳公婆"
_SINGLE_MENTIONS = (
    rf"(?<![{_KIN_PREFIX}])爸", rf"(?<![{_KIN_PREFIX}])媽(?!祖)",
    r"(?<![其吉])他(?!人)",  # 其他、吉他、他人都不算「他」
    "她", "你", "我",
)
# 「我的媽媽」「你的爸爸」「親愛的媽媽」是自己家的；其他「X的媽媽」是別人的家長
_OWN_POSSESSORS = ("我", "你", "我們", "你們", "咱們", "咱", "我家", "我們家", "咱們家", "親愛", "最愛", "可愛", "偉大",
                   "辛苦", "敬愛")
_NOT_POSSESSORS = frozenset({"我", "你", "你們", "我們"})
# 代名詞只認性別相符的家長：他＝爸爸、她＝媽媽（「他老人家」是尊稱）
_PRONOUN_GENDER = {"他": FATHER, "她": MOTHER, "他老人家": FATHER, "她老人家": MOTHER}
_PLURAL_PRONOUNS = frozenset({"他們", "她們"})
_SPOUSE_WORDS = frozenset({"老公", "老婆", "太太", "老伴", "丈夫", "妻子", "先生"})
_COORDINATOR_CHARS = frozenset("和跟與及")
# 稱謂在句中的角色：聽話的人（跟媽媽說、告訴爸爸）、受詞（遛狗、帶孫子、看醫生）、地點（朋友家、媽媽面前）都不是主詞
_ADDRESSEE_BEFORE_RE = re.compile(r"(?:給|對|向|告訴|打給|問)$")
_SPEECH_AFTER_RE = re.compile(r"說|講|聊|提|抱怨|告訴")
_OBJECT_VERB_BEFORE_RE = re.compile(r"(?:[遛帶餵抱接送陪幫載扶找等顧罵問叫教](?:著|了)?|看(?:完|了|過)?|照顧)$")
# 爸媽被陪、帶、扶的時候（「我陪媽媽散步的時候跌倒了」）：看診、用藥、讀數、送醫照算是爸媽的，身體緊急字詞不算
_ESCORT_VERB_BEFORE_RE = re.compile(r"(?:[陪帶載扶幫顧等接抱送](?:著|了)?|照顧|[去來]看)$")
_ADVERBIAL_AFTER_RE = re.compile(r"的?(?:家|那邊|那裡|那兒|這邊|這裡|面前|旁邊|身邊)|的時候|時(?![常候間])")
_QUOTE_OPEN_RE = re.compile(r"說\s{0,2}[:「『\"“]\s{0,2}$")  # 「媽媽說：我胸口好悶」：引號裡的「我」是媽媽
_SPEECH_GAP = 6  # 說話動詞和「他／她」之間最多隔幾個字（中間只能是聽話的人：「告訴爸爸他…」）
_SUBJECT, _OBJECT, _ADDRESSEE, _ADVERBIAL = "subject", "object", "addressee", "adverbial"
_PARENT_OBJECT = "parent_object"
_COMPANION = "companion"
# 緊急字詞前那個名詞的前一個字是地點、動詞或完成標記 →「在院子」「吃了包子」「從凳子」是地方或東西，不是人
_NOT_PERSON_BEFORE = frozenset("在從到去回於住搭坐下上買吃喝逛拿搬掃爬收整穿洗煮看了完過帶陪送接找等顧騎開推抱餵遛扶載進出")
_PIVOT_VERBS = ("看到", "聽到", "遇到", "看見", "碰到", "見到", "發現")  # 「看到牌友昏倒了」：昏倒的是牌友
# 地點名稱裡的機構詞是地方，不是人：「在里長辦公室」「在遊客中心」「在家屬休息室」「到志工站」「醫院長廊」「學校長廊」
_PLACE_TAIL_RE = re.compile(r"室|中心|站|隊|辦公|處|廊|椅|服務|休息|大會|廟|餐廳")
_INSTITUTION_WORDS = frozenset({"院長", "校長", "里長", "鄰長", "遊客", "家屬", "志工", "社工", "義工", "乘客", "顧客", "董事長",
                                "會長", "社長", "住戶", "客戶", "伯公", "家長"})
_PARENT_WORDS = frozenset(word for word, kinds in _TITLE_KINDS.items() if set(kinds) - {"O"})
_SPEAKER_INDEX = {FAMILY: 0, FATHER: 1, MOTHER: 2}
_MAX_OTHER_NAMES = 200


def _clean_names(other_names: Iterable[str] | None) -> tuple[str, ...]:
    """呼叫端給的別名：正規化、去掉稱謂（角色檔會把「爸爸」「媽媽」也列成別名）、空字串和單字。"""
    names: set[str] = set()
    for name in other_names or ():
        if not isinstance(name, str):
            continue
        clean = _normalize(name).strip()
        if len(clean) >= 2 and clean not in _PARENT_WORDS:
            names.add(clean)
    return tuple(sorted(names)[:_MAX_OTHER_NAMES])


@lru_cache(maxsize=64)
def _mention_regex(names: tuple[str, ...]) -> re.Pattern[str]:
    words = {*_TITLE_KINDS, *_OTHER_WORDS, *_PRONOUN_GENDER, *names} - {"爸", "媽", "我", "他", "她"}
    literals = sorted(words, key=lambda w: (-len(w), w))
    return re.compile("|".join([*(re.escape(word) for word in literals), *_OTHER_PATTERNS, *_SINGLE_MENTIONS]))


def _roles_of(word: str, speaker: str, at_clause_start: bool) -> tuple[str, ...]:
    """稱謂 → 指到的家長（說話者不同，同一個詞意思不同）；別人 → ()。"""
    kinds = _TITLE_KINDS.get(word)
    if kinds is None:
        return ()
    kind = kinds[_SPEAKER_INDEX[speaker]]
    if kind.endswith("^"):
        kind = kind[0] if at_clause_start else "O"
    if kind == "F":
        return (FATHER,)
    if kind == "M":
        return (MOTHER,)
    if kind == "B":
        return PARENTS
    if kind == "S" and speaker in PARENTS:
        return (speaker,)
    return ()


def _someone_elses_parent(text: str, start: int, word: str, prev_word: str, prev_end: int) -> bool:
    """「他媽媽」「朋友媽媽」「小明的媽媽」「爸爸的媽媽」「王媽媽」：講的是別人家的家長。"""
    if prev_word and start == prev_end and prev_word not in _TITLE_KINDS and prev_word not in _NOT_POSSESSORS:
        return True
    if text[start - 1:start] == "的" and not text[max(0, start - 4):start - 1].endswith(_OWN_POSSESSORS):
        return True
    if start == 0 or word[0] not in "爸媽" or text[start - 1] not in _SURNAMES:
        return False
    before = text[start - 2] if start >= 2 else ""
    return not (before and before in _SURNAME_NOT_AFTER.get(text[start - 1], ""))


def _mention_kind(text: str, start: int, end: int, roles: tuple[str, ...], word: str, companion_ok: bool,
                  phrase_start: int) -> str:
    """稱謂在句中的角色：只有主詞會被當成健康詞的主人。phrase_start 是名詞片語的開頭（「跟王媽媽」的王）。"""
    coordinated = text[phrase_start - 1:phrase_start] in _COORDINATOR_CHARS
    if _ADDRESSEE_BEFORE_RE.search(text, max(0, start - 4), start) or (coordinated and _SPEECH_AFTER_RE.match(text, end)):
        return _ADDRESSEE  # 「妹妹跟媽媽說她…」「鄰居告訴爸爸他…」「跟王媽媽說」
    if _ADVERBIAL_AFTER_RE.match(text, end):
        return _ADVERBIAL  # 「在朋友家跌倒」「在媽媽面前」
    if not roles and _OBJECT_VERB_BEFORE_RE.search(text, max(0, start - 3), start):
        return _OBJECT  # 「遛狗的時候」「帶孫子去」「看完醫生」
    if roles and word != "我" and _ESCORT_VERB_BEFORE_RE.search(text, max(0, start - 3), start):
        return _PARENT_OBJECT  # 「我陪媽媽去回診」照算是媽媽的看診；「陪媽媽散步的時候跌倒了」跌倒的不是媽媽
    if not roles and companion_ok and coordinated:
        return _COMPANION  # 「爸爸今天跟朋友去爬山」「我和老同學聚餐」「我跟一群朋友」：朋友是同伴，主詞還是前面的人
    return _SUBJECT


def _skip_modifiers(text: str, start: int) -> int:
    """往前跳過最多兩個修飾字（「跟老同學」「和好朋友」的老、好），回跳過後的位置。"""
    pos = start
    while pos > 0 and start - pos < 2 and text[pos - 1] in _NOUN_MODIFIERS:
        pos -= 1
    return pos


def _institution_place(text: str, start: int, end: int, word: str) -> bool:
    """機構詞後面接地點字，前面是在／到／去…（「在里長辦公室」），或是「醫院長廊」「學校長廊」→ 是地方。
    「看到志工站在門口」「遇到里長辦公室的人」的到是看到、遇到，不是地點；「大學校長昏倒了」後面沒有地點字，是人。"""
    if word not in _INSTITUTION_WORDS or start == 0 or not _PLACE_TAIL_RE.match(text, end):
        return False
    before = text[start - 1]
    if before in _LOCATIVES:
        return text[max(0, start - 2):start] not in _PIVOT_VERBS
    return word in ("院長", "校長") and before in ("醫", "學")


# 「跟」後面整個名詞片語都算同伴：數量（一個、一群、好幾個）、「X的」（以前的、教會的、以前公司的）、
# 固定前綴（公司同事、社區志工）、姓氏（王媽媽、林阿姨）；X 不能跨過說話動詞、代名詞或另一個並列連詞
_NP_COUNT = r"[一二兩三四五六七八九十幾好]{1,2}[個群位些堆]"
_NP_STOP = "的跟和與及我你妳他她說講告聊問"
_NP_GAP_RE = re.compile(
    rf"(?:{_NP_COUNT})?(?:(?:(?![{_NP_STOP}])[\u4e00-\u9fff]){{1,4}}的|公司|教會|社區|學校|以前)?(?:{_NP_COUNT})?[{_SURNAMES}]?$"
)
_NP_GAP_MAX = 12


def _phrase_start(text: str, start: int, lower: int) -> int:
    """名詞片語從哪裡開始（不跨過前一個稱謂 lower）：「跟老同學」「跟一群朋友」「跟以前的同事」「跟公司同事」「跟王媽媽」
    都往前跳到「跟」後面；沒有這些就是名詞自己（跳過修飾字）。"""
    pos = max(_skip_modifiers(text, start), lower)
    m = _NP_GAP_RE.search(text, max(lower, pos - _NP_GAP_MAX), pos)
    return m.start() if m else pos


def _coordinated(text: str, start: int) -> int:
    """前面緊接並列連詞（和／跟／與／及／還有）時回連詞開頭的位置，否則 -1。"""
    if text[start - 1:start] in _COORDINATOR_CHARS:
        return start - 1
    if text[max(0, start - 2):start] == "還有":
        return start - 2
    return -1


class _Mentions:
    """一個子句裡的稱謂。代名詞只回指說話動詞的主詞（不回指聽話的人），性別要相符；
    句首的「他／她」可以接前一句轉述的爸媽；「說：」後面的「我」是被引述的人。"""

    __slots__ = ("items", "relayed", "relay_out", "quote_out", "_speech", "_si", "_last_subject",
                 "_speech_subject", "_speech_end", "_subject_after_speech", "_foreign_party")
    # 反過來的轉述：「我剛剛打給爸爸，他說胸口很痛」「我問媽媽，她說剛剛跌倒了」：說話的是我、
    # 唯一的聽話者是爸媽、沒有別人，下一句句首的「他／她」就是那位

    def __init__(self, text: str, speaker: str, mention_re: re.Pattern[str], carry_in: tuple[str, ...],
                 relay_in: tuple[str, ...], quote_in: tuple[str, ...]):
        self.items: list[tuple[int, int, tuple[str, ...], str]] = []
        self.relayed = False
        self._speech = [(m.start(), m.end()) for m in _SPEECH_RE.finditer(text)]
        self._si = 0
        self._last_subject: tuple[tuple[str, ...], str] | None = None
        self._speech_subject: tuple[tuple[str, ...], str] | None = None
        self._speech_end = -1
        self._subject_after_speech = False
        self._foreign_party = False  # 有「我」以外的聽話者或受詞：轉述不帶到下一句（「媽媽打給阿姨，她…」）
        prev_word, prev_end, prev_roles, prev_kind = "", -1, (), ""
        # 前一個詞在「跟…」片語裡（同伴，或「我跟隔壁」這種並列主詞）：(角色, 爸媽)；下一個詞緊接著或只隔「的」就是同一個片語
        phrase: tuple[str, tuple[str, ...]] | None = None
        for m in mention_re.finditer(text):
            word, start, end = m.group(), m.start(), m.end()
            self._advance_speech(start, relay_in)
            if word in _PLACE_WORDS and text[start - 1:start] in _LOCATIVES:
                continue
            if _institution_place(text, start, end, word):
                continue  # 「在里長辦公室」「醫院長廊」：是地方
            if text[end:end + 1] == "會" and _MEETING_RE.search(text, max(0, start - 3), end + 1):
                continue  # 「去同學會」「參加家長會」：是聚會，不是同學、家長
            roles = _roles_of(word, speaker, start == 0)
            if not roles and word in _SPOUSE_WORDS and text[max(0, start - 2):start] == "我的":
                roles = _roles_of("我" + word, speaker, False)  # 「我的老公」＝「我老公」
            elif roles and word != "我" and _someone_elses_parent(text, start, word, prev_word, prev_end):
                roles = ()
            # 前面已經有主詞（或沿用前一句），而且「跟」不是緊接在它後面：跟／和後面的別人是同伴
            companion_ok = (self._last_subject is not None or bool(carry_in)) and prev_end != start - 1
            pstart = _phrase_start(text, start, max(prev_end, 0))
            kind = _mention_kind(text, start, end, roles, word, companion_ok, pstart)
            own = not roles
            in_phrase = False
            if (phrase is not None and own and kind == _SUBJECT and text[prev_end:pstart] in ("", "的")
                    and word not in _PRONOUN_GENDER and word != "我" and not _SPEECH_AFTER_RE.match(text, end)):
                kind, roles = phrase  # 「跟隔壁阿姨」「跟隔壁的阿姨」「跟朋友的媽媽」：同一個片語，和前面那個詞一樣
                in_phrase = True
            if word in _PRONOUN_GENDER:
                roles = self._pronoun(word, start, prev_word, prev_end, prev_roles, prev_kind, relay_in)
            elif word in _PLURAL_PRONOUNS:
                # 「爸媽他們今天去爬山了」：緊接在稱謂後面的「他們」就是那兩位
                roles = prev_roles if (start == prev_end and prev_kind == _SUBJECT and prev_word in _TITLE_KINDS) else ()
            elif word == "我" and (quoted := self._quoted(text, start, quote_in)) is not None:
                roles = quoted
            elif kind == _SUBJECT and not in_phrase and (joint := _coordinated(text, start)) >= 0:
                if prev_word and prev_end == joint and prev_kind == _SUBJECT:
                    roles = tuple(r for r in PARENTS if r in roles or r in prev_roles)  # 爸爸和媽媽、我跟你爸
                    in_phrase = True
                elif self._last_subject is None:
                    roles = tuple(r for r in PARENTS if r in roles or r in carry_in)  # （我）跟朋友去爬山了
                    in_phrase = True
            # 只有別人（不是爸媽本人）才往下帶：「我跟媽媽的朋友」的朋友不是媽媽
            phrase = (kind, roles) if own and (in_phrase or kind == _COMPANION) else None
            self.items.append((start, end, roles, kind))
            if (kind == _SUBJECT and word != "我" and self._speech_subject is not None and start >= self._speech_end
                    and tuple(roles) != tuple(self._speech_subject[0])):
                self._foreign_party = True  # 「爸爸說朋友中風了，他…」：轉述裡有別人，不帶到下一句
            if kind == _SUBJECT:
                self._last_subject = (roles, word)
                self._subject_after_speech = True
            elif word != "我":
                self._foreign_party = True
            prev_word, prev_end, prev_roles, prev_kind = word, end, roles, kind
        self._advance_speech(len(text) + 1, relay_in)
        addressees = [roles for _, _, roles, kind in self.items if kind == _ADDRESSEE]
        others = any(kind != _ADDRESSEE and text[start:end] != "我" for start, end, _, kind in self.items)
        reverse = len(addressees) == 1 and len(addressees[0]) == 1 and not others
        subject = self._speech_subject
        single = subject is not None and subject[1] != "我" and len(subject[0]) == 1
        # 下一句句首的「他／她」可以接這句轉述的爸媽（「爸爸剛剛打電話來，他說胸口很痛」）
        self.relay_out = subject[0] if single and not self._foreign_party else (addressees[0] if reverse else ())
        # 這句以「說：」結尾：下一句開頭的「我」是被引述的爸媽（「媽媽說： 我胸口好悶」）
        self.quote_out = subject[0] if single and _QUOTE_OPEN_RE.search(text) else ()

    def _advance_speech(self, pos: int, relay_in: tuple[str, ...]) -> None:
        """把 pos 以前的說話動詞記下來，主詞是動詞前最近的主詞（句首沒有就看前一句轉述的人）。"""
        while self._si < len(self._speech) and self._speech[self._si][1] <= pos:
            subject = self._last_subject
            if subject is None and relay_in:
                subject = (relay_in, "")
            self._speech_subject = subject
            self._speech_end = self._speech[self._si][1]
            self._subject_after_speech = False
            if subject is not None and subject[1] != "我" and subject[0]:
                self.relayed = True  # 轉述爸媽的話也算報告標記（「爸爸說胸口痛」）
            self._si += 1

    def _pronoun(self, word: str, start: int, prev_word: str, prev_end: int, prev_roles: tuple[str, ...],
                 prev_kind: str, relay_in: tuple[str, ...]) -> tuple[str, ...]:
        gender = _PRONOUN_GENDER[word]
        if self._speech_subject is not None and start - self._speech_end <= _SPEECH_GAP and not self._subject_after_speech:
            roles, subject_word = self._speech_subject
            # 「爸爸說他胸口痛」「爸爸剛剛跟店員說他胸口痛」；「我說他…」「爸爸說她…」「妹妹跟媽媽說她…」都是別人
            return (gender,) if subject_word != "我" and gender in roles else ()
        if prev_word in _TITLE_KINDS and start == prev_end and prev_kind == _SUBJECT and gender in prev_roles:
            return (gender,)  # 「你爸他今天跌倒了」「爸爸他老人家」
        if start == 0 and gender in relay_in:
            self.relayed = True
            return (gender,)  # 前一句轉述的爸媽：「媽媽剛剛打給我 她跌倒了」
        return ()

    def _quoted(self, text: str, start: int, quote_in: tuple[str, ...]) -> tuple[str, ...] | None:
        """「媽媽說：我胸口好悶」的「我」是媽媽；不是引述就回 None。"""
        if start == 0 and quote_in:
            self.relayed = True
            return quote_in
        subject = self._speech_subject
        if subject is None or not _QUOTE_OPEN_RE.search(text, max(0, start - 6), start):
            return None
        if subject[1] == "我" or len(subject[0]) != 1:
            return None
        self.relayed = True
        return subject[0]


# ─────────────────────────────────────────────────────────────────────────────
# 前處理與子句
# ─────────────────────────────────────────────────────────────────────────────

_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
# 只認「/」開頭的指令；先點名咪寶再下指令（「咪寶 /記住 …」）由 main 判斷、不會送進來（這裡不能比 main 寬）
_COMMAND_RE = re.compile(r"/")
_SEP_RE = re.compile(r"[。！？!?；;、，,]|\s+")
_NUMERIC_EDGE = frozenset("0123456789./")
_UNIT_START = frozenset("步公台斤磅下度個小分次毫%kKmMcC")


def _normalize(text: str) -> str:
    """NFKC＋去掉 Cf（零寬、方向控制字元）：「胸」「口」中間夾零寬空白也比得到。"""
    text = unicodedata.normalize("NFKC", text)
    if text.isascii():
        return text
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _prepare(text: object) -> str | None:
    """正規化；空的、以「/」開頭、含網址、超過 MAX_CHARS 的回 None（整則略過）。"""
    if not isinstance(text, str) or not text:
        return None
    norm = _normalize(text).strip()
    if not norm or len(norm) > MAX_CHARS or _COMMAND_RE.match(norm) or _URL_RE.search(norm):
        return None
    return norm


def _speaker_role(speaker: object) -> str:
    return speaker if speaker in PARENTS else FAMILY


def _split_clauses(text: str) -> list[tuple[str, bool, bool, bool]]:
    """切子句，回 [(子句, 問號結尾, 驚嘆號結尾, 後面是逗號或空白)]。數字旁的空白不切（「血壓 135/85」「走了 8000 步」）。
    最後一欄只給台式問句尾往前標記用：「爸爸藥有吃 齁」算同一句，「我散步回來了。厚，好累」不算。"""
    clauses: list[list] = []
    start, size = 0, len(text)
    for m in _SEP_RE.finditer(text):
        sep = m.group()
        if sep[0].isspace() and "\n" not in sep:
            before = text[m.start() - 1] if m.start() else ""
            after = text[m.end()] if m.end() < size else ""
            if after in _NUMERIC_EDGE or (before in _NUMERIC_EDGE and (not after or after in _UNIT_START)):
                continue
        piece = text[start:m.start()]
        if piece:
            clauses.append([piece, sep == "?", sep == "!", sep in "，,、" or (sep[0].isspace() and "\n" not in sep)])
        elif clauses and sep in "?!":
            clauses[-1][1 if sep == "?" else 2] = True  # 「吃藥了 ?」：符號跟著前一個子句
        start = m.end()
    if start < size:
        clauses.append([text[start:], False, False, False])
    return [(piece, asked, exclaimed, soft) for piece, asked, exclaimed, soft in clauses]


def _strip_tail(text: str) -> str:
    """去掉結尾的符號、表情（不用 regex：一長串符號後面接一個字會讓 [\\W]+$ 回溯成平方）。"""
    end = len(text)
    while end and not text[end - 1].isalnum():
        end -= 1
    return text[:end]


class _Clause:
    """一個子句的線索、稱謂位置與跨子句沿用的狀態。"""

    __slots__ = ("text", "cues", "question", "exclaimed", "mentions", "relayed", "relay_out", "quote_out",
                 "mention_starts", "mention_end_set", "subject_ends", "subject_roles", "actor_ends", "actor_roles",
                 "reading_ends", "reading_roles", "carry_in", "actor_carry_in", "past", "bp", "tag_question",
                 "check_end")

    def __init__(self, text: str, asked: bool, exclaimed: bool, speaker: str, mention_re: re.Pattern[str],
                 carry_in: tuple[str, ...], relay_in: tuple[str, ...], quote_in: tuple[str, ...]):
        self.text = text
        self.exclaimed = exclaimed
        self.carry_in = carry_in
        self.cues: dict[str, tuple[list[int], list[int]]] = {}
        for m in _CUE_RE.finditer(text):
            categories = _AGO_CATEGORIES if m.lastgroup == "ago" else _CUE_CATEGORIES[m.group()]
            for category in categories:
                starts, ends = self.cues.setdefault(category, ([], []))
                starts.append(m.start())
                ends.append(m.end())
        tail = _strip_tail(text)
        self.question = (
            asked or _QUESTION in self.cues or bool(_A_NOT_A_RE.search(text))
            or bool(tail and tail[-1] in _QUESTION_TAIL)
        )
        self.tag_question = bool(tail and tail[-1] in _TAG_QUESTION_TAIL)
        check = _CHECK_BEFORE_RE.search(text)
        self.check_end = check.end() if check else -1
        found = _Mentions(text, speaker, mention_re, carry_in, relay_in, quote_in)
        self.mentions = found.items
        self.actor_carry_in = carry_in
        self.relayed, self.relay_out, self.quote_out = found.relayed, found.relay_out, found.quote_out
        self.mention_starts = [start for start, _, _, _ in self.mentions]
        self.mention_end_set = {end for _, end, _, _ in self.mentions}
        if _PAST in self.cues:
            self._drop_former(text)
        subjects = [(end, roles) for _, end, roles, kind in self.mentions if kind in (_SUBJECT, _PARENT_OBJECT)]
        self.subject_ends = [end for end, _ in subjects]
        self.subject_roles = [roles for _, roles in subjects]
        # 身體緊急字詞不認被陪、被帶的爸媽（「我陪媽媽散步的時候跌倒了」）
        actors = [(end, roles) for _, end, roles, kind in self.mentions if kind == _SUBJECT]
        self.actor_ends = [end for end, _ in actors]
        self.actor_roles = [roles for _, roles in actors]
        # 讀數也認受詞：「幫爸爸量血壓 150/95」量的是爸爸
        readers = [(end, roles) for _, end, roles, kind in self.mentions if kind in (_SUBJECT, _OBJECT, _PARENT_OBJECT)]
        self.reading_ends = [end for end, _ in readers]
        self.reading_roles = [roles for _, roles in readers]
        self.past = False
        self.bp = False

    def _drop_former(self, text: str) -> None:
        """「以前的同事」「以前公司的老鄰居」：以前、之前修飾後面的人（＝前同事），不是在說很久以前。"""
        starts, ends = self.cues[_PAST]
        kept = [(s, e) for s, e in zip(starts, ends, strict=True) if not self._modifies_person(text, e)]
        if len(kept) == len(starts):
            return
        if kept:
            self.cues[_PAST] = ([s for s, _ in kept], [e for _, e in kept])
        else:
            del self.cues[_PAST]

    def _modifies_person(self, text: str, end: int) -> bool:
        """end 後面是「（最多兩個字）的」，接著就是下一個稱謂，而且那是別人（「以前的我」「以前的她」還是以前）。"""
        m = _FORMER_RE.match(text, end)
        if m is None:
            return False
        index = bisect_left(self.mention_starts, end)
        if index == len(self.mention_starts) or _skip_modifiers(text, self.mention_starts[index]) != m.end():
            return False
        return not self.mentions[index][2]

    def has(self, category: str) -> bool:
        return category in self.cues

    def before(self, category: str, pos: int) -> bool:
        """這類線索有出現在 pos 前面（同一子句）。"""
        entry = self.cues.get(category)
        return bool(entry) and entry[0][0] < pos

    def ends_near(self, category: str, pos: int, window: int) -> bool:
        """這類線索在 pos 前 window 字內結束。"""
        entry = self.cues.get(category)
        if not entry:
            return False
        ends = entry[1]
        index = bisect_right(ends, pos) - 1
        return index >= 0 and ends[index] >= pos - window

    @property
    def skip_report(self) -> bool:
        """一般紀錄：問句、假設、轉述整個子句都不記（未來、勸告看位置，見 blocked）。"""
        return self.question or any(c in self.cues for c in (_HYPO, _REPORTED))

    def governed(self, category: str, pos: int) -> bool:
        """這類線索在 pos 前面，而且中間沒有「的時候／時／就／結果」隔開（「要去廁所的時候跌倒了」不是未來）。"""
        entry = self.cues.get(category)
        if not entry:
            return False
        index = bisect_left(entry[0], pos) - 1
        if index < 0:
            return False
        cue_end = entry[1][index]
        blocks = self.cues.get(_BLOCK)
        if blocks:
            after = bisect_left(blocks[0], cue_end)
            if after < len(blocks[0]) and blocks[0][after] < pos:
                return False
        return True

    def blocked(self, pos: int) -> bool:
        """一般紀錄：前面有管得到它的未來或勸告詞（「等一下要去散步」「要多運動」）就不記。"""
        return self.governed(_FUTURE, pos) or self.governed(_ADVICE, pos)

    def mention_at(self, pos: int) -> tuple[str, ...] | None:
        """剛好從 pos 開始的稱謂指到誰；沒有稱謂從 pos 開始就回 None。"""
        index = bisect_left(self.mention_starts, pos)
        if index < len(self.mention_starts) and self.mention_starts[index] == pos:
            return self.mentions[index][2]
        return None

    def subject_at(self, pos: int) -> tuple[str, ...]:
        """pos 前面最近的主詞（受詞、聽話的人、地點不算）；子句裡沒有就沿用前一句的主詞（再沒有就是說話者或跳過）。"""
        index = bisect_right(self.subject_ends, pos) - 1
        return self.subject_roles[index] if index >= 0 else self.carry_in

    def actor_at(self, pos: int) -> tuple[str, ...]:
        """身體緊急字詞的主人：最近的主詞，不含被陪、被帶的爸媽；沒有就沿用前一句的主詞。"""
        index = bisect_right(self.actor_ends, pos) - 1
        return self.actor_roles[index] if index >= 0 else self.actor_carry_in

    def reading_subject_at(self, pos: int) -> tuple[str, ...]:
        """讀數的主人：最近的主詞或受詞。"""
        index = bisect_right(self.reading_ends, pos) - 1
        return self.reading_roles[index] if index >= 0 else self.carry_in


def _analyze(norm: str, speaker: str, names: tuple[str, ...]) -> list[_Clause]:
    mention_re = _mention_regex(names)
    clauses: list[_Clause] = []
    carry: tuple[str, ...] = (speaker,) if speaker in PARENTS else ()
    actor_carry = carry
    relay: tuple[str, ...] = ()
    quote: tuple[str, ...] = ()
    past = bp = False
    soft_ends: list[bool] = []
    for piece, asked, exclaimed, soft in _split_clauses(norm):
        soft_ends.append(soft)
        clause = _Clause(piece, asked, exclaimed, speaker, mention_re, carry, relay, quote)
        clause.actor_carry_in = actor_carry
        if clause.subject_roles:
            carry = clause.subject_roles[-1]
        if clause.actor_roles:
            actor_carry = clause.actor_roles[-1]
        if clause.has(_ARTICLE):
            carry = actor_carry = ()  # 「中風三大徵兆：臉歪、手無力、口齒不清」後面的項目不是在講爸媽
        relay, quote = clause.relay_out, clause.quote_out
        if clause.has(_PAST) and not clause.has(_NOW):
            past = True
        elif clause.has(_NOW) or clause.has(_PAST):
            past = False
        clause.past = past
        bp = bool(_BP_CUE_RE.search(piece)) or (bp and not _OTHER_READING_CUE_RE.search(piece))
        clause.bp = bp
        clauses.append(clause)
    # 「爸爸藥有吃 齁」「爸爸有吃藥，對嗎？」：問句尾被空白或逗號切成自己一個子句時，前一個子句也算台式問句。
    # 隔著句號、問號、驚嘆號或換行就不算（「我散步回來了。厚，好累」的「厚」是感嘆）。
    # 只設 tag_question，不動 question：119 不受影響。
    for prev, cur, soft in zip(clauses, clauses[1:], soft_ends):
        if not soft:
            continue
        core = _strip_tail(cur.text)
        head = 0
        while head < len(core) and not core[head].isalnum():  # 「～齁」「😅齁」：開頭的符號、表情也去掉
            head += 1
        core = core[head:].strip()
        if (core and (set(core) <= _TAG_QUESTION_TAIL | _QUESTION_TAIL or core in _TAG_QUESTION_WORDS)) or (
            cur.question and core in ("", "沒有")  # 「爸爸藥吃了 😅?」「爸爸有吃藥 沒有？」
        ):
            prev.tag_question = True
    return clauses


def _window_has(rx: re.Pattern[str], text: str, start: int, end: int) -> bool:
    return rx.search(text, max(0, start), end) is not None


def _done_near(text: str, start: int, end: int) -> bool:
    """完成標記（了、過、完、已、剛、有、回來）在健康詞前 5 字到後 4 字之間。"""
    return _window_has(_DONE_RE, text, start - 5, min(len(text), end + 4))


# ─────────────────────────────────────────────────────────────────────────────
# 緊急字詞
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Hit:
    clause: int
    pos: int
    role: str
    label: str
    urgent: bool
    future: bool


def _negated_before(text: str, start: int) -> bool:
    """緊接在前的否定（沒有胸悶、不會跌倒）；「是不是中風」「有沒有跌倒」是問句，不是否定。"""
    m = _RF_NEGATED_RE.search(text, max(0, start - 6), start)
    if m is None:
        return False
    i = m.start()
    return not (0 < i < len(text) - 1 and text[i - 1] == text[i + 1])


def _red_flag_excluded(clause: _Clause, label: str, start: int, end: int, shoulder: bool, insurance: bool,
                       clothing: bool) -> bool:
    text = clause.text
    if clause.before(_HEDGE, start) or clause.ends_near(_HABIT, start, _HABIT_WINDOW) or clause.governed(_ADVICE, start):
        return True
    if _negated_before(text, start) or _tendency(text, start):
        return True
    if label not in IN_CARE_LABELS and _degree_complement(text, start):
        return True  # 「忙到喘不過氣」；「送到急診」的「到」是地點不是程度
    if _GENERIC_AFTER_RE.match(text, end):
        return True
    if label in IN_CARE_LABELS and insurance:
        return True
    if label == "跌倒" and (
        _window_has(_STOCK_BEFORE_RE, text, start - 4, start) or _STOCK_AFTER_RE.match(text, end)
        or (text[start] == "摔" and _thing_fell(text, start))
    ):
        return True
    if label == "胸痛胸悶" and clothing and text[end - 1] == "緊":
        return True
    return label == "單側無力" and shoulder


def _thing_fell(text: str, start: int) -> bool:
    """「手機摔下來」：摔的是東西；「買東西摔倒了」的東西是買的，摔倒的是人。"""
    m = _OBJECT_BEFORE_RE.search(text, max(0, start - 4), start)
    return m is not None and text[m.start() - 1:m.start()] not in _HANDLING_VERBS


def _degree_complement(text: str, start: int) -> bool:
    m = _DEGREE_RE.search(text, max(0, start - 8), start)
    if m is None:
        return False
    cause = text[m.start() - 1] if m.start() else ""
    return cause in _DEGREE_CAUSES or any(hedge in m.group(1) for hedge in _DEGREE_HEDGES)


def _person_noun_before(clause: _Clause, start: int) -> bool:
    """「看到牌友昏倒了」「爸爸看到外孫跌倒了」：緊急字詞前（跳過時間副詞）是以人字結尾、還沒認出的名詞 → 是那個人。"""
    text = clause.text
    run = _ADVERB_RUN_RE.search(text, max(0, start - 8), start)
    k = run.start() if run else start
    if k == 0 or text[k - 1] not in _PERSON_FINAL_SET or k in clause.mention_end_set:
        return False
    if text[max(0, k - 3):k] in _NOT_PERSON_SET or text[max(0, k - 2):k] in _NOT_PERSON_SET:
        return False
    if text[k - 2:k - 1] in ("完", "了", "過"):
        return False  # 「做完工」「打完工」：做完的事，不是人
    # 「在院子」「吃了包子」「從凳子」：名詞前面是地點、動詞或完成標記 → 是地方或東西；「看到牌友」例外。
    # 三字職稱（「收銀員昏倒了」「搬運工剛剛跌倒了」）看職稱前面那個字
    size = 3 if k >= 3 and _JOB_NOUN_RE.fullmatch(text, k - 3, k) else 2
    before = text[k - size - 1:k - size] if k > size else ""
    return not (before in _NOT_PERSON_BEFORE and text[max(0, k - size - 2):k - size] not in _PIVOT_VERBS)


def _resolved_roles_after(clauses: list[_Clause]) -> tuple[list[set[str]], list[tuple[list[int], list[set[str]]]]]:
    """「已經好了」指到誰：回 (每個子句之後所有子句的聯集, 每個子句內 (位置, 從該位置到句尾的聯集))。"""
    inner: list[tuple[list[int], list[set[str]]]] = []
    for clause in clauses:
        entry = clause.cues.get(_RESOLVED)
        if not entry or clause.question:  # 「沒事吧？」是在問
            inner.append(([], []))
            continue
        starts = entry[0]
        suffix: list[set[str]] = [set() for _ in starts]
        running: set[str] = set()
        for index in range(len(starts) - 1, -1, -1):
            running = running | set(clause.subject_at(starts[index]))
            suffix[index] = running
        inner.append((starts, suffix))
    after: list[set[str]] = [set() for _ in range(len(clauses) + 1)]
    for index in range(len(clauses) - 1, -1, -1):
        suffix = inner[index][1]
        after[index] = after[index + 1] | (suffix[0] if suffix else set())
    return after, inner


def _red_flag_hits(clauses: list[_Clause], speaker: str, norm: str) -> list[_Hit]:
    shoulder = _SHOULDER_RE.search(norm) is not None
    after, inner = _resolved_roles_after(clauses)
    hits: list[_Hit] = []
    for index, clause in enumerate(clauses):
        if clause.has(_HYPO) or clause.has(_REPORTED):
            continue  # 問句照算；假設、轉述整個子句不算；勸告、未來只管後面的事（逐一判斷）
        text = clause.text
        matches = list(_RED_FLAG_RE.finditer(text))
        insurance = _INSURANCE_RE.search(text) is not None  # 每個子句只掃一次，命中再多也是線性
        clothing = _CLOTHING_RE.search(text) is not None
        # 轉述別人的狀況要有報告標記：了、剛剛、現在…，或「！」、「歪歪的」、同一句提到送醫／急診
        clause_reported = (
            clause.has(_MARKER) or clause.exclaimed or clause.relayed or _REDUPLICATED_RE.search(text) is not None
            or any(_RED_FLAG_GROUPS[m.lastgroup] in IN_CARE_LABELS for m in matches)
        )
        for m in matches:
            label = _RED_FLAG_GROUPS[m.lastgroup]
            start, end = m.span()
            if _red_flag_excluded(clause, label, start, end, shoulder, insurance, clothing):
                continue
            roles = clause.subject_at(start) if label in IN_CARE_LABELS else clause.actor_at(start)
            owner = clause.mention_at(end + 1) if text[end:end + 1] == "的" else None
            if owner is not None:
                roles = owner  # 「住院的阿姨」講的是阿姨；「住院的媽媽」講的是媽媽
            elif roles and _person_noun_before(clause, start):
                roles = ()
            if not roles:
                continue
            # 「明天要住院」是未來；「要去廁所的時候跌倒了」「跌倒了明天要住院」的跌倒是現在
            future = clause.governed(_FUTURE, start)
            reported = clause_reported or _INTENSIFIER_RE.search(text, start, end) is not None
            starts, suffix = inner[index]
            later = bisect_right(starts, start)
            resolved_here = suffix[later] if later < len(suffix) else set()
            # 「我中風過」是以前的經驗；「中風後一直在復健」中風是以前的事
            long_ago = clause.past or text.startswith("過", end) or (
                label in ("中風", "心臟病發") and _LONG_AFTER_RE.match(text, end) is not None
            )
            for role in roles:
                if role != speaker and not reported:
                    continue  # 「爸爸胸悶」這類一般描述不算
                resolved = role in resolved_here or role in after[index + 1]
                hits.append(_Hit(index, start, role, label, not long_ago and not resolved, future))
    return hits


def _red_flags_core(norm: str, speaker: str, names: tuple[str, ...]) -> list[RedFlag]:
    """不限長度的緊急字詞判斷（公開函式先擋掉超過 MAX_CHARS 的；測試直接量這個的線性時間）。"""
    hits = _red_flag_hits(_analyze(norm, speaker, names), speaker, norm)
    # 「已在處理」只看現在發生的（「下週要住院」「明天要去急診拿報告」不算已在急診）
    in_care = {hit.role for hit in hits if hit.label in IN_CARE_LABELS and hit.urgent and not hit.future}
    merged: dict[tuple[str, str], list[bool]] = {}
    for hit in hits:
        state = merged.get((hit.role, hit.label))
        if state is None:
            merged[(hit.role, hit.label)] = [hit.urgent, hit.future]
        else:
            state[0] = state[0] or hit.urgent
            state[1] = state[1] and hit.future
    return [
        RedFlag(role, label, urgent, role in in_care, future)
        for (role, label), (urgent, future) in merged.items()
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 一般紀錄
# ─────────────────────────────────────────────────────────────────────────────

class _Collector:
    """收集 (子句, 位置, Finding)；同一則訊息同 (role, category, item, value) 只留一筆，redflag 優先。"""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str, str], list] = {}

    def add(self, clause: int, pos: int, roles: Iterable[str], category: str, item: str,
            value: str = "", flag: str = "") -> None:
        for role in roles:
            key = (role, category, item, value)
            current = self._items.get(key)
            if current is None:
                self._items[key] = [clause, pos, flag]
            elif flag and not current[2]:
                current[2] = flag

    def result(self) -> list[Finding]:
        walked = {(role, value != "") for role, category, item, value in self._items
                  if category == "exercise" and item == "散步"}
        ordered = sorted(self._items.items(), key=lambda kv: (kv[1][0], kv[1][1], PARENTS.index(kv[0][0])))
        findings = []
        for (role, category, item, value), (_, _, flag) in ordered:
            if category == "exercise" and item == "散步" and not value and (role, True) in walked:
                continue  # 同一則有步數就不再另記一筆沒數字的散步
            findings.append(Finding(role, category, item, value, flag))
        return findings


def _instructed(clause: _Clause, start: int) -> bool:
    """「提醒爸爸吃藥」「吃完飯要吃藥」：叫人做、還沒做。"""
    return (
        clause.before(_INSTRUCT, start) or _window_has(_MUST_BEFORE_RE, clause.text, start - 3, start)
        or 0 <= clause.check_end <= start
    )


def _scan_med(index: int, clause: _Clause, out: _Collector) -> None:
    text = clause.text
    for m in _MED_RE.finditer(text):
        kind, start, end = m.lastgroup, m.start(), m.end()
        if clause.blocked(start):
            continue
        if kind == "took" and clause.tag_question:
            continue  # 「爸爸藥有吃齁」是問句：記了會蓋掉「幾天沒提到吃藥」的提醒（忘了吃藥、停藥、拿藥照記）
        if kind == "took" and text[start - 1:start] == "在":
            continue  # 「有在吃降血壓藥」「在吃中藥」是平常在吃，不是今天吃了
        if kind == "forgot":
            # 差點／怕／擔心／以免忘了吃藥 → 沒忘；不要／別忘了是勸告（整句已略過）；沒忘記 → 沒忘
            if clause.before(_HEDGE, start) or _window_has(_NEG_RE, text, start - 3, start):
                continue
        else:
            if _instructed(clause, start) or _window_has(_NEG_MED_RE, text, start - 3, start):
                continue
            if not _done_near(text, start, end):
                continue
        out.add(index, start, clause.subject_at(start), "med", _MED_LABELS[kind])


def _scan_activity(index: int, clause: _Clause, out: _Collector, rx: re.Pattern[str], groups: dict[str, str],
                   category: str) -> None:
    """看診與運動：要有完成標記（或早上／昨天這類時間），沒去、要去、叫人去都不記。"""
    if category == "exercise" and clause.tag_question:
        return
    text = clause.text
    for m in rx.finditer(text):
        start, end = m.span()
        if clause.blocked(start) or _instructed(clause, start) or _window_has(_NEG_RE, text, start - 3, start):
            continue
        go = _GO_PREFIX_RE.search(text, max(0, start - 2), start)
        # 「感冒一直沒好就去看醫生了」：否定講的是前面的條件，「就／才去」照樣是去了
        if go and text[go.start() - 1:go.start()] not in ("就", "才") and _window_has(_NEG_RE, text, go.start() - 3, go.start()):
            continue
        if _window_has(_MODAL_BEFORE_RE, text, start - 4, start):
            continue
        if category == "visit" and _RESULT_AFTER_RE.match(text, end):
            continue  # 「健檢報告出來了」不是今天看診
        if not (_done_near(text, start, end) or clause.has(_ANCHOR) or _now_at(text, start)):
            continue
        out.add(index, start, clause.subject_at(start), category, groups[m.lastgroup])


def _now_at(text: str, start: int) -> bool:
    """「媽媽在急診」「在復健」＝正在；「一直在復健」「都在復健」是持續的事，不算今天去了。"""
    return text[start - 1:start] == "在" and not text[max(0, start - 3):start - 1].endswith(("一直", "都", "常", "總是"))


def _steps_value(m: re.Match[str]) -> int:
    kind = m.lastgroup
    if kind == "arabic":
        return int(m.group("n"))
    if kind == "count":
        return int(m.group("n2"))
    if kind == "wan":
        return _CN_DIGITS.get(m.group("w") or "一", 1) * 10000 + _CN_DIGITS.get(m.group("wk") or "", 0) * 1000
    return _CN_DIGITS[m.group("k")] * 1000 + _CN_DIGITS.get(m.group("kh") or "", 0) * 100


def _scan_steps(index: int, clause: _Clause, out: _Collector) -> None:
    if clause.tag_question:
        return  # 和運動一樣：「爸爸今天走了8000步齁」是問句
    text = clause.text
    for m in _STEPS_RE.finditer(text):
        start, end = m.span()
        if m.lastgroup in ("arabic", "qian") and not _window_has(_WALK_CUE_RE, text, start - 5, start):
            continue  # 「8000 步」要跟「走」連在一起才算
        if clause.blocked(start) or _instructed(clause, start) or _window_has(_NEG_RE, text, start - 4, start):
            continue
        if not (_done_near(text, start, end) or clause.has(_ANCHOR_STEPS)):
            continue
        steps = _steps_value(m)
        if 100 <= steps <= 999_999:
            out.add(index, start, clause.subject_at(start), "exercise", "散步", f"{steps}步")


def _is_habitual(clause: _Clause, start: int) -> bool:
    return clause.ends_near(_HABIT, start, _HABIT_WINDOW) or _tendency(clause.text, start)


def _tendency(text: str, start: int) -> bool:
    """「年紀大會跌倒」：會＝傾向；「我在教會跌倒了」「去參加聚會跌倒了」的會是地方，不算。"""
    m = _TENDENCY_RE.search(text, max(0, start - 4), start)
    return m is not None and not (_MEETING_RE.search(text, max(0, m.start() - 6), m.start() + 1)
                                  or _EVENT_RE.search(text, max(0, m.start() - 6), m.start() + 1))


def _scan_sleep(index: int, clause: _Clause, out: _Collector) -> None:
    text = clause.text
    for m in _SLEEP_RE.finditer(text):
        start = m.start()
        if clause.blocked(start) or clause.before(_INSTRUCT, start) or clause.before(_HEDGE, start):
            continue
        if _is_habitual(clause, start):
            continue
        if _window_has(_NEG_RE, text, start - 4, start):
            continue
        out.add(index, start, clause.subject_at(start), "sleep", _SLEEP_GROUPS[m.lastgroup])


def _scan_diet(index: int, clause: _Clause, out: _Collector) -> None:
    text = clause.text
    for m in _DIET_RE.finditer(text):
        start, end = m.span()
        label = _DIET_GROUPS[m.lastgroup]
        if clause.blocked(start) or clause.before(_INSTRUCT, start) or clause.before(_HEDGE, start):
            continue
        if _is_habitual(clause, start):
            continue
        if label not in _DIET_NEGATIVE and _window_has(_NEG_RE, text, start - 4, start):
            continue
        if label == "喝酒" and not _done_near(text, start, end):
            continue
        out.add(index, start, clause.subject_at(start), "diet", label)


def _below_threshold_context(text: str, start: int, end: int) -> bool:
    return _window_has(_THRESHOLD_BEFORE_RE, text, start - 6, start) or _THRESHOLD_AFTER_RE.match(text, end) is not None


def _scan_readings(index: int, clause: _Clause, out: _Collector) -> None:
    """讀數不受「幫／提醒」限制（「幫爸爸量血壓 150/95」照記）；同一則多筆都記。"""
    text = clause.text
    if clause.bp:
        for rx in (_BP_PAIR_RE, _BP_HIGH_LOW_RE):
            for m in rx.finditer(text):
                systolic, diastolic = int(m.group(1)), int(m.group(2))
                if not (_BP_SYSTOLIC[0] <= systolic <= _BP_SYSTOLIC[1] and _BP_DIASTOLIC[0] <= diastolic <= _BP_DIASTOLIC[1]):
                    continue
                if systolic <= diastolic or _below_threshold_context(text, m.start(1), m.end(2)) or clause.blocked(m.start()):
                    continue
                out.add(index, m.start(), clause.reading_subject_at(m.start()), "body", "血壓", f"{systolic}/{diastolic}")
    for label, rx, low, high in _SINGLE_READINGS:
        for m in rx.finditer(text):
            raw = m.group(1)
            whole, _, decimals = raw.partition(".")
            value = f"{int(whole)}.{decimals[0]}" if decimals else str(int(whole))
            if not low <= float(value) <= high or _below_threshold_context(text, m.start(1), m.end(1)):
                continue
            if clause.blocked(m.start()):
                continue
            out.add(index, m.start(), clause.reading_subject_at(m.start()), "body", label, value)


def _scan_symptoms(index: int, clause: _Clause, out: _Collector) -> None:
    text = clause.text
    flag_spans = [m.span() for m in _RED_FLAG_RE.finditer(text)]
    flag_starts = [span[0] for span in flag_spans]
    last_recovery = -1
    for m in _RECOVERED_RE.finditer(text):
        last_recovery = m.start()
    for m in _SYMPTOM_RE.finditer(text):
        start, end = m.span()
        overlap = bisect_left(flag_starts, end) - 1
        if overlap >= 0 and flag_spans[overlap][1] > start:
            continue  # 「頭暈倒」「左手麻」交給緊急字詞判斷
        if clause.blocked(start) or clause.before(_INSTRUCT, start) or clause.before(_HEDGE, start):
            continue
        if _is_habitual(clause, start):
            continue
        if _window_has(_NEG_RE, text, start - 4, start) or last_recovery >= end:
            continue  # 沒有頭暈；感冒好多了
        if _window_has(_EMOTION_DEGREE_RE, text, start - 4, start):
            continue
        out.add(index, start, clause.subject_at(start), "body", _SYMPTOM_GROUPS[m.lastgroup])


def _extract_core(norm: str, speaker: str, names: tuple[str, ...]) -> list[Finding]:
    """不限長度的抽取（公開函式先擋掉超過 MAX_CHARS 的；測試直接量這個的線性時間）。"""
    clauses = _analyze(norm, speaker, names)
    out = _Collector()
    for index, clause in enumerate(clauses):
        if clause.skip_report:
            continue
        _scan_med(index, clause, out)
        _scan_activity(index, clause, out, _VISIT_RE, _VISIT_GROUPS, "visit")
        _scan_activity(index, clause, out, _EXERCISE_RE, _EXERCISE_GROUPS, "exercise")
        _scan_steps(index, clause, out)
        _scan_sleep(index, clause, out)
        _scan_diet(index, clause, out)
        _scan_readings(index, clause, out)
        _scan_symptoms(index, clause, out)
    # 緊急字詞走自己的規則（問句也算）；只出現在未來子句的不記（卡片不會出現沒發生的 ⚠️）
    for hit in _red_flag_hits(clauses, speaker, norm):
        if hit.label in RED_FLAG_LABELS and not hit.future:
            out.add(hit.clause, hit.pos, (hit.role,), "body", hit.label, "", "redflag" if hit.urgent else "")
    return out.result()


# ─────────────────────────────────────────────────────────────────────────────
# 公開函式
# ─────────────────────────────────────────────────────────────────────────────

def might_have_red_flag(text: str) -> bool:
    """純子字串預檢（O(n)）：False 就一定沒有緊急字詞，webhook 鎖內不用再碰別名檔與 DB。"""
    norm = _prepare(text)
    return norm is not None and any(hint in norm for hint in _RED_FLAG_HINTS)


def extract(text: str, speaker: str, *, other_names: Iterable[str] = ()) -> list[Finding]:
    """從一則留言抽出爸媽的健康紀錄。符合條件的緊急字詞以 body＋flag="redflag" 輸出；
    很久以前／已經好了的以一般 body 紀錄輸出；被排除的（否定、假設、怕、程度補語…）不輸出。"""
    norm = _prepare(text)
    if norm is None:
        return []
    return _extract_core(norm, _speaker_role(speaker), _clean_names(other_names))


def detect_red_flags(text: str, speaker: str, *, other_names: Iterable[str] = ()) -> list[RedFlag]:
    """緊急字詞（問句也算）：每位家長每個標籤一筆。"""
    norm = _prepare(text)
    if norm is None:
        return []
    return _red_flags_core(norm, _speaker_role(speaker), _clean_names(other_names))
