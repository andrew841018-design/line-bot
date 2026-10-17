"""hearsay — 家人記憶裡「A 講到家人 B 的事」（2026-10-16）。

Andrew 2026-10-16：記憶要分到各個說話的人名下，「除非A說的是B的事件那可能要另外處理」；
選定記到 B 名下、標「（A說的）」，只有 B 明確是群組裡的人才記，B 本人說過的版本優先，
只記長期的事。

純函式：不讀檔、不連 DB、不呼叫模型。名單（roster）和本機別名由呼叫的人傳進來；
repo 是 public，這裡只有通用稱謂，沒有任何家人的名字。

- 標記：存成「B：本體（A說的）」，標記只由程式加（fact_store）。NFKC 會把全形括號變半形，
  所以拆標記一律看資料庫裡的原始列文字（審查 GP1#1）。
- 範圍：轉述只收長期、低風險的幾類（住、工作／上學、喜歡與飲食、興趣、養寵物、在學什麼）；
  時間點、代名詞、第三者、評價、健康身體、心情感情成績、打算一律不收（GP1#4、GP2 S1）。
- 對人：模型只回訊息裡對那個人的原字（about）；這裡用本機別名與固定幾條規則決定是誰。
  位置不對、對不到、對到兩個人都不收（debate D1、D4）。精準優先：漏記一條轉述沒有損失
  （B 自己說的還是會記），記錯人才要人工發現。
"""
from __future__ import annotations

import collections
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from fact_hash import strip_format

# ── 標記 ─────────────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"[（(]([^（）()\n]{1,20})說的[）)]\s*$")


# 轉述列的 tombstone namespace（memory._tombstone_body）：canonical() 會拿掉所有空白，所以這個
# 前綴（含一個空白）不可能和任何真的本體撞（debate D3）。
TOMBSTONE_PREFIX = "h: "


def tag(said_by: str) -> str:
    return f"（{said_by}說的）"


def split_tag(body: str) -> tuple[str, str]:
    """(本體, 說的人)：結尾是「（X說的）」就拆開（全形或半形括號都認），不是就 (原文, "")。"""
    text = body or ""
    match = _TAG_RE.search(text)
    if not match:
        return text, ""
    plain = text[: match.start()].rstrip()
    said_by = match.group(1).strip()
    if not plain or not said_by:
        return text, ""
    return plain, said_by


def has_tag(body: str) -> bool:
    return bool(split_tag(body)[1])


# ── 範圍閘門（只給轉述用）────────────────────────────────────────────────────

# 喜好只收「喜歡＋動作」與固定的興趣名詞（R2 GP2 N2：「喜歡＋人或東西」的敏感尾巴列不完，改用允許清單）
_HOBBY_VERBS = (
    r"吃|喝|看|聽|玩|去|打|學|爬|騎|游|跑|唱|畫|拍|煮|做|彈|逛|釣|種|寫|讀|跳|散步|運動|旅行|旅遊|露營|健身"
)
_HOBBY_NOUNS = (
    r"攝影|園藝|閱讀|烹飪|爬山|登山|游泳|跑步|騎車|釣魚|旅行|旅遊|露營|畫畫|書法|音樂|唱歌|跳舞|下棋|健身"
    r"|瑜伽|瑜珈|網球|羽球|桌球|籃球|棒球|高爾夫|看書|看電影|追劇|咖啡|甜點|手作|編織|模型|拼圖|單車|自行車"
)
# 必須以這幾類開頭（前面只能有幾個副詞）：住、工作上學、喜好、興趣、寵物、在學什麼（GP2 S1 (a)）。
# 一定要錨在開頭：不錨的話「討厭公司」因為有「公司」就會過（post-impl GP2#1）。
_FRAME_RE = re.compile(
    r"^(?:最近|現在|目前|平常|平時|常常|經常|每天|每週|每周|週末|也|都|很|超|非常|特別|一直|還|正)*"
    r"(?:住(?:在)?|搬(?:到|去)"
    r"|在[^，,。；;]{1,12}?(?:上班|工作|上學|讀書|唸書|念書|就讀)|上班|上學"
    r"|在?(?:讀|念|唸|就讀)(?:大學|高中|國中|國小|研究所|碩士|博士|[^，,。；;]{1,10}?(?:系|學院|大學|高中))"
    r"|(?:不?喜歡|最愛|迷上)(?:" + _HOBBY_VERBS + r")|興趣是(?:" + _HOBBY_VERBS + "|" + _HOBBY_NOUNS + r")"
    r"|迷上(?:" + _HOBBY_NOUNS + r")|愛吃|愛喝|愛看|愛聽|愛玩|不愛吃|不愛喝|不吃|不喝|討厭吃|討厭喝|收藏"
    r"|養(?:了|著)?(?:一|兩|[0-9])?(?:隻|條)?(?:貓|狗|魚|鳥|兔|倉鼠|烏龜|寵物)"
    r"|在學|學(?:日文|英文|韓文|法文|德文|西班牙文|鋼琴|吉他|小提琴|烏克麗麗|畫畫|繪畫|游泳|跳舞|唱歌"
    r"|烹飪|做菜|攝影|書法|瑜伽|瑜珈|程式|開車|騎車|打鼓))"
)
# 任何一類都不收（GP2 S1 (b)、GP1#4、0086f5 的觀察）；依序檢查，回第一個命中的類別。
_DENY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("time", re.compile(
        r"今天|明天|後天|昨天|前天|今晚|明晚|昨晚|下週|下周|下禮拜|下星期|上週|上周|上禮拜|上星期"
        r"|這週|這周|這禮拜|這星期|[週周][一二三四五六日天末]|星期[一二三四五六日天]|禮拜[一二三四五六日天]"
        r"|早上|上午|中午|下午|傍晚|晚上|半夜|凌晨|[0-9０-９一二三四五六七八九十兩]+\s*點"
        r"|下個?月|上個?月|這個?月|明年|去年|今年|前年|民國|西元|[0-9０-９]{2,4}\s*年|年級|年次"
        r"|寒假|暑假|過年|連假"
    )),
    ("pronoun", re.compile(r"[我咱俺你妳您祂]|(?<![其吉])[他她它]")),
    ("third_party", re.compile(
        r"同學|同事|朋友|老闆|主管|鄰居|室友|客戶|男友|女友|男朋友|女朋友|對象|別人|人家"
    )),
    ("judgement", re.compile(
        r"脾氣|個性|性格|懶|笨|胖|瘦|醜|小氣|囉嗦|嘮叨|碎念|很差|太差|不好|變差|退化|壞|兇|罵|吵"
        r"|酒|賭|菸|煙|檳榔|毒"
    )),
    ("health", re.compile(
        r"(?:眼睛?|耳朵?|牙齒?|腰|背|膝蓋?|腿|腳|手|頭|胃|肚子|身體)"
        r"(?:不好|不舒服|痠|酸|痛|麻|抖|腫|差|弱|不行|開刀|受傷)"
        r"|胃口|吃不下|食慾|不穩|重聽|聽力|聽不清|聽不到|看不清|老花|近視|散光|白內障|記性|健忘|忘東忘西"
        r"|精神|沒力|無力|疲倦|疲勞|累|睡|夜尿|頻尿|尿|便祕|便秘|血|照顧|不便|行動"
        r"|一個人|獨居|輪椅|拐杖|看護|安養|養老|老人院|長照|吃藥|身體|三高|療養|病院|勒戒|戒治|戒癮|戒毒"
        r"|護理之家|養護|日照|榮民之家|觀護|殯儀"
        r"|眼|耳|牙|腰|膝|關節"
    )),
    ("sensitive", re.compile(
        r"心情|情緒|壓力|憂|難過|傷心|生氣|哭|寂寞|孤單|戀愛|暗戀|喜歡的人|成績|分數|考試|考上|落榜|補習|升學"
        r"|同性|異性|女生|男生|女人|男人|性|砲|色情|(?i:a片|av)|成人片|夜店|酒店|監獄|看守所|坐牢|庇護|收容"
        r"|資遣|裁員|失業|找不到工作"
        r"|宗教|信教|基督|天主|佛教|道教|教會|教堂|禱告|受洗|拜拜|廟|念經|吃齋|政治|政論|選舉|投票|黨"
        r"|雪茄|高粱|威士忌|麻將|彩券|樂透|股票|成人影片"
    )),
    ("event", re.compile(r"準備|打算|即將|快要|要去|會去|剛|已經|終於|將要|計畫|預計")),
)


def scope_reason(body: str) -> str:
    """轉述的本體可以收回 ""；不行回類別（scope:<類別>；沒有符合任何一類是 scope:frame）。"""
    text = unicodedata.normalize("NFKC", strip_format(body or ""))
    for category, pattern in _DENY_RULES:
        if pattern.search(text):
            return f"scope:{category}"
    if not _FRAME_RE.search(text):
        return "scope:frame"
    return ""


# ── 子句、猜測 ───────────────────────────────────────────────────────────────

_CLAUSE_SPLIT_RE = re.compile(r"[，,。．.；;！!？?、：:\n\r  ]+")
_HEDGE_RE = re.compile(
    r"是不是|對不對|好像|聽說|據說|可能|應該|也許|大概|似乎|不知道|不確定|我猜|難道|亂說|開玩笑|說笑|騙你|假的|才怪"
    r"|還是|或是|或者|或|不對|記錯|搞錯|說錯|更正|隨便說|蛤|ㄇ|ㄅ"
    r"|嗎|呢|吧|[?？]"
)
# B 那一段原文裡有這些就不收：想要、以前、被、別人插在中間、幫別人做（post-impl GP2#1）。
_SOURCE_DENY_RE = re.compile(
    r"想|要|希望|考慮|如果|以前|之前|曾經|本來|原本|被|幫|替|帶|有個|有一個|那個|這個"
    r"|會|將|快|以後|之後|小時候|年輕時|從前|當年|曾|退休|過幾天|過"
)
# 整則訊息裡有這些就不收轉述（R2 GP2 B1）：時間、過去、假設、更正，寫在別的句子也一樣。
_MESSAGE_DENY_RE = re.compile(
    r"以前|之前|曾|本來|原本|從前|過去|當年|當時|那時|小時候|年輕時|年輕的時候|退休|離職|搬走|搬家|已經不是|不再"
    r"|如果|假如|若是|要是|希望|考慮|的話"
    r"|沒有啦|不是啦|沒啦|不對|搞錯|記錯|講錯|說錯|寫錯|打錯|更正|我是說|錯了|錯啦|才不是|其實是|唬爛|唬你"
)
# 引述別人的話（「同學說：我住台中」）：事實出自的那一句前面是冒號或「說／講／問…」就不收（R2 GP2 N1）
_SPEECH_END_RE = re.compile(r"(?:說|講|問|回|寫|傳|貼|轉貼|表示|提到|留言)[\s,]*$")
# 否定：原文那一段和事實要一致（「爸爸不喜歡爬山」不能變成「喜歡爬山」）
_NEGATION_RE = re.compile(r"不|沒|未|無|非(?!常)|哪有|(?<!特)別")
# 親屬稱謂與口語稱呼：本人事實的子句裡出現就分不出在講誰；名單裡的顯示名稱是這些也不收。
_KIN_RE = re.compile(
    r"媽媽|爸爸|姊姊|姐姐|妹妹|弟弟|哥哥|阿婆|外婆|奶奶|阿嬤|爺爺|阿公|外公|兒子|女兒|老公|老婆|舅舅|阿姨"
    r"|姑姑|叔叔|伯伯|婆婆|公公|岳母|岳父|媳婦|女婿|父親|母親|孫子|孫女|老爸|老媽|老弟|老妹|老哥"
    r"|我[爸媽哥姊姐妹弟]"
)
# 本人事實的子句、B 那一段用的更寬清單（顯示名稱不用這個：「王先生」不該因此不能當對象，post-impl R2 GP1 F3）
_OTHER_KIN_RE = re.compile(
    _KIN_RE.pattern
    + r"|爸媽|父母|雙親|家人|先生|太太|小孩|孩子|姪|甥|表[哥姐姊弟妹兄]|堂[哥姐姊弟妹兄]"
    r"|學[長姐姊弟妹]|房東|親戚|親家|公婆|岳|娘家|婆家|乾[爸媽]|繼[父母]"
    r"|阿爸|阿母|老伴|兒媳|[姊姐妹]夫|醫生|寶寶|家那口子|那口子|爸|媽"
)


def normalize(text: str) -> str:
    """比對用：去格式字元、NFKC、不分大小寫（位置都用這個版本算）。"""
    return unicodedata.normalize("NFKC", strip_format(text or "")).casefold()


def clauses(text: str) -> list[str]:
    """依標點切子句（已 normalize）；子句保留原本的問號給猜測檢查用。"""
    norm = normalize(text)
    parts: list[str] = []
    start = 0
    for match in _CLAUSE_SPLIT_RE.finditer(norm):
        end = match.end() if "?" in match.group() else match.start()
        piece = norm[start:end].strip()
        if piece:
            parts.append(piece)
        start = match.end()
    tail = norm[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def hedged(clause: str) -> bool:
    return _HEDGE_RE.search(clause or "") is not None


# ── 對人 ─────────────────────────────────────────────────────────────────────

# 2026-10-17：子女的「我媽／我爸」、家裡共用的「姊姊」由本機角色檔（family_kinship.local.json）提供。
# TODO(deferred)：兄弟姊妹的「我姊／我哥／我弟／我妹」和「哥哥／弟弟」要等 Andrew 提供出生順序；現在不收。
# TODO(2026-10-16 deferred)：3 字名的後 2 字（例如在地名裡）可能誤配；目前靠位置檢查與模型判斷，殘留風險已告知。
SELF_WORDS = frozenset({"我", "自己", "我自己", "本人"})
OMITTED = "（省略）"
_OMITTED_WORDS = frozenset({"", "省略", "(省略)", "無", "none", "self"})
# 配偶：只有這兩個稱呼剛好都在名單裡才用（媽媽說的「老公」是爸爸，爸爸說的「老婆」是媽媽）。
FATHER, MOTHER = "爸爸", "媽媽"
_SPOUSE_TERMS = {MOTHER: ("老公", "我老公"), FATHER: ("老婆", "我老婆")}
# 子女說的「我媽／我爸」（2026-10-17 Andrew 照建議做）。兄弟姊妹的「我姊／我哥…」要看出生順序，還不認。
_CHILD_TERMS = {"我媽": MOTHER, "我媽媽": MOTHER, "我爸": FATHER, "我爸爸": FATHER}
# 本機角色檔（family_kinship.local.json）只收這兩個叫法，值是 user_id（顯示名稱是自己取的，不能拿來綁角色）
KINSHIP_ROLE_KEYS = frozenset({"姊姊", "姐姐"})
# 一個稱呼左邊緊接這些字之一才算在主詞位置（D1：精準優先，閉集合）。
_CONNECTORS = (
    "其實", "因為", "所以", "但是", "但", "而且", "還有", "不過", "反正", "對了", "現在", "平常",
    "通常", "常常", "一直", "最近", "我們", "你們", "咱們", "大家",
)
_CONNECTORS_LONGEST_FIRST = tuple(sorted(_CONNECTORS, key=len, reverse=True))
_BAD_PLURAL = ("他們", "她們", "它們")
_BAD_LEFT = frozenset("我你妳您他她它的")
# 本人事實（主詞省略）的子句裡出現這些字，就分不出是在講誰（0086f5：同學的事被記在說話的人名下）。
_OTHER_PEOPLE_RE = re.compile(
    r"同學|同事|朋友|老闆|主管|老師|鄰居|室友|客戶|男友|女友|男朋友|女朋友|他們|她們|別人|人家"
    r"|(?<![其吉])[他她它]"
)
# 名單上不能當轉述對象的稱呼（顯示名稱可以自己取，post-impl GP2#3）：一個字、代名詞、第三者、全體、親屬稱謂。
_UNSAFE_LABEL_RE = re.compile(r"^(?:大家|全家|家人|所有人|群組|我們|你們|他們|她們)$|[我你妳您他她它]")
_NOT_SPEAKER = "\x00"
_TERM_STRIP_RE = re.compile(r"[\s\"'「」『』“”‘’《》〈〉（）()\[\]【】:：,，。.!！?？@＠]+")


@dataclass(frozen=True)
class Roster:
    """轉述用的家人名單：terms＝正規化後的叫法 → 稱呼（一個叫法對到兩個人的已拿掉）。

    children：子女（說「我媽／我爸」時對到媽媽／爸爸）；role_terms：本機角色檔來的叫法（姊姊／姐姐），
    只有子女或爸媽說才算（別人說的「姊姊」可能是她自己的姊姊，2026-10-17 審查）。
    """

    labels: frozenset[str]
    terms: Mapping[str, str]
    children: frozenset[str] = frozenset()
    role_terms: frozenset[str] = frozenset()

    def spouse_of(self, speaker: str, term: str) -> str:
        if term in _SPOUSE_TERMS.get(speaker, ()) and {FATHER, MOTHER} <= self.labels:
            return MOTHER if speaker == FATHER else FATHER
        return ""

    def family_speaker(self, speaker: str) -> bool:
        return speaker in self.children or (speaker in (FATHER, MOTHER) and speaker in self.labels)

    def child_term(self, speaker: str, term: str) -> str:
        """子女說的「我媽／我爸」對到誰；不是子女、或名單沒有那位就回 ""。"""
        label = _CHILD_TERMS.get(term, "") if speaker in self.children else ""
        return label if label in self.labels else ""

    def terms_for(self, speaker: str) -> dict[str, str]:
        """這位說話的人可以用的叫法 → 稱呼（角色叫法只給子女和爸媽）。"""
        allowed = {
            term: label for term, label in self.terms.items()
            if term not in self.role_terms or self.family_speaker(speaker)
        }
        if speaker in self.children:
            for term, label in _CHILD_TERMS.items():
                if label in self.labels:
                    allowed.setdefault(term, label)
        return allowed


def term_key(term: str) -> str:
    """模型給的 about：去引號、標點、空白，NFKC、不分大小寫。"""
    return _TERM_STRIP_RE.sub("", normalize(term))


def build_roster(
    labels: Iterable[str],
    aliases: Mapping[str, str],
    *,
    roles: Mapping[str, str] | None = None,
    children: Iterable[str] = (),
) -> Roster:
    """labels：可以掛轉述的家人稱呼；aliases：叫法 → 稱呼（呼叫的人先清理成同一種寫法）；
    roles：角色檔的叫法 → 稱呼（和別名撞到別人時兩邊都不給）；children：子女的稱呼（爸媽不算）。"""
    label_set = frozenset(label for label in labels if label)
    found: dict[str, set[str]] = {}
    for label in label_set:
        found.setdefault(term_key(label), set()).add(label)
    for term, label in aliases.items():
        if label in label_set and term_key(term):
            found.setdefault(term_key(term), set()).add(label)
    role_keys: set[str] = set()
    for term, label in (roles or {}).items():
        if label in label_set and term_key(term):
            found.setdefault(term_key(term), set()).add(label)
            role_keys.add(term_key(term))
    terms = {term: next(iter(owners)) for term, owners in found.items() if len(owners) == 1 and term}
    kids = frozenset(c for c in children if c in label_set and c not in (FATHER, MOTHER))
    return Roster(labels=label_set, terms=terms, children=kids, role_terms=frozenset(role_keys & set(terms)))


def parse_kinship(raw: object, labels_by_user: Mapping[str, str]) -> tuple[dict[str, str], frozenset[str], int, int]:
    """本機角色檔的內容 → (角色叫法 → 稱呼, 子女稱呼, 套用條數, 略過條數)。

    結構只能是 {"roles": {"姊姊"|"姐姐": user_id}, "children": [user_id, …]}；兩種寫法要指同一人、
    子女不能重複。結構不對丟 ValueError（呼叫的人整個檔當作沒有）。user_id 不在名單上的那一條略過；
    爸爸、媽媽不能算子女。純函式：不讀檔。
    """
    if not isinstance(raw, dict) or set(raw) - {"roles", "children"}:
        raise ValueError("kinship: unexpected structure")
    roles_raw, kids_raw = raw.get("roles", {}), raw.get("children", [])
    if not isinstance(roles_raw, dict) or not isinstance(kids_raw, list):
        raise ValueError("kinship: unexpected structure")
    # 型別先檢查：值是 list 或 dict 時 set() 會丟 TypeError，不能讓手改打錯變成整支 job 掛掉（審查 v3 GP2#1）
    if not all(isinstance(v, str) and v for v in (*roles_raw.values(), *kids_raw)):
        raise ValueError("kinship: bad user ids")
    if set(roles_raw) - KINSHIP_ROLE_KEYS or len(set(roles_raw.values())) > 1:
        raise ValueError("kinship: unknown role or roles disagree")
    if len(set(kids_raw)) != len(kids_raw):
        raise ValueError("kinship: duplicate children")
    applied = ignored = 0
    roles: dict[str, str] = {}
    for term, user_id in roles_raw.items():
        label = labels_by_user.get(user_id, "")
        if label:
            roles[term] = label
            applied += 1
        else:
            ignored += 1
    children: set[str] = set()
    for user_id in kids_raw:
        label = labels_by_user.get(user_id, "")
        if label and label not in (FATHER, MOTHER):
            children.add(label)
            applied += 1
        else:
            ignored += 1
    return roles, frozenset(children), applied, ignored


def unsafe_label(label: str) -> bool:
    """顯示名稱來的稱呼能不能當轉述對象：一個字、代名詞、第三者、全體、親屬稱謂都不行。"""
    text = normalize(label)
    return (
        len(text) < 2
        or _UNSAFE_LABEL_RE.search(text) is not None
        or _OTHER_PEOPLE_RE.search(text) is not None
        or _KIN_RE.search(text) is not None
    )


@dataclass(frozen=True)
class Subject:
    """about 解析的結果：kind 是 self_word／omitted／self_label／member／drop。"""

    kind: str
    label: str = ""
    term: str = ""  # 正規化後、要在子句裡找得到的原字
    reason: str = ""


def resolve(about: object, speaker: str, roster: Roster) -> Subject:
    raw = str(about or "")
    key = term_key(raw)
    if key in {term_key(word) for word in SELF_WORDS}:
        return Subject("self_word", term=key)
    if key in {term_key(word) for word in _OMITTED_WORDS}:
        return Subject("omitted")
    spouse = roster.spouse_of(speaker, key)
    if spouse:
        return Subject("member", spouse, key)
    child = roster.child_term(speaker, key)
    if child:
        return Subject("member", child, key) if child != speaker else Subject("drop", reason="unresolved")
    label = roster.terms_for(speaker).get(key, "")
    if not label:
        return Subject("drop", reason="unresolved")
    if label == speaker:
        return Subject("self_label", label, key)
    return Subject("member", label, key)


def _member_spans(clause: str, roster: Roster, speaker: str) -> list[tuple[int, int, str]]:
    """子句裡每一個家人叫法的位置（最長比對、不重疊）：[(開始, 結束, 稱呼)]。"""
    spans: list[tuple[int, int, str]] = []
    lookup = roster.terms_for(speaker)
    for term in _SPOUSE_TERMS.get(speaker, ()):
        if roster.spouse_of(speaker, term):
            lookup[term] = roster.spouse_of(speaker, term)
    for term in _CHILD_TERMS:
        if term in lookup and lookup[term] == speaker:
            lookup[term] = _NOT_SPEAKER  # 對到說話的人自己的「我媽」：當成別人，絕不當成本人的稱呼（審查 GP1#2）
    keys = sorted(lookup, key=len, reverse=True)
    taken = [False] * len(clause)
    for key in keys:
        label = lookup[key]
        start = clause.find(key)
        while start != -1:
            end = start + len(key)
            if key.isascii() and (  # 英文名字要整個字：Ed 不能對到 Edwin（R2 GP2）
                (start and clause[start - 1].isascii() and clause[start - 1].isalnum())
                or (end < len(clause) and clause[end].isascii() and clause[end].isalnum())
            ):
                start = clause.find(key, start + 1)
                continue
            if not any(taken[start:end]):
                spans.append((start, end, label))
                for i in range(start, end):
                    taken[i] = True
            start = clause.find(key, start + 1)
    return sorted(spans)


def _left_ok(clause: str, start: int) -> bool:
    if start == 0:
        return True
    before = clause[:start]
    prev = before[-1]
    if prev.isspace() or unicodedata.category(prev)[0] in "PZ" or prev in "@＠":
        return True
    if before.endswith(_BAD_PLURAL) or prev in _BAD_LEFT:
        return False
    # 只看緊鄰的字不夠（「他說其實爸爸…」）：從右邊一直剝掉連接詞，剩下的一定要是開頭、空白或標點
    rest, peeled = before, False
    while True:
        word = next((w for w in _CONNECTORS_LONGEST_FIRST if rest.endswith(w)), "")
        if not word:
            break
        rest, peeled = rest[: -len(word)], True
    if not peeled:
        return False
    return not rest or rest[-1].isspace() or unicodedata.category(rest[-1])[0] in "PZ" or rest[-1] in "@＠"


def position_reason(clause: str, subject: Subject, roster: Roster, speaker: str) -> str:
    """B 的每一個出現位置都要在主詞位置（D1、D4-3、D4-4）；不行回原因。"""
    spans = [(s, e) for s, e, label in _member_spans(clause, roster, speaker) if label == subject.label]
    # 位置一律用最長比對的那一段算（「小華媽媽」裡的媽媽左邊是實字，不算）；模型寫短的叫法也行
    if not any(subject.term in clause[s:e] for s, e in spans):
        return "not_in_clause"
    for start, end in spans:
        if not _left_ok(clause, start):
            return "position_left"
        if clause[end:end + 1] == "的":
            return "position_possessive"
    return ""


def bind_reason(
    fact: str, clause: str, subject: Subject, roster: Roster, speaker: str,
    bigrams: Callable[[str], set[str]],
) -> str:
    """事實要綁在 B 那一段：子句依家人叫法切段，和 B 那段的重疊要 ≥ 1、而且嚴格大於其他段。"""
    spans = _member_spans(clause, roster, speaker)
    cuts = [0, *(s for s, _e, _label in spans), len(clause)]
    owners = ["", *(label for _s, _e, label in spans)]
    grams = bigrams(fact)
    mine = other = 0
    for index in range(len(cuts) - 1):
        piece = clause[cuts[index]:cuts[index + 1]]
        overlap = len(grams & bigrams(piece))
        if owners[index] == subject.label:
            mine += overlap
        else:
            other = max(other, overlap)
    if mine < 1:
        return "bind_none"
    if mine <= other:
        return "bind_other"
    return ""


def best_clause(fact: str, text: str, bigrams: Callable[[str], set[str]]) -> tuple[str, str]:
    """(子句, 原因)：和事實重疊最多的那一句；平手或 0 回 ("", 原因)。"""
    grams = bigrams(fact)
    scored = sorted(((len(grams & bigrams(c)), c) for c in clauses(text)), key=lambda x: -x[0])
    if not scored or scored[0][0] == 0:
        return "", "clause_none"
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return "", "clause_tie"
    return scored[0][1], ""


def _blank_spans(clause: str, spans: Iterable[tuple[int, int]]) -> str:
    chars = list(clause)
    for start, end in spans:
        for index in range(start, end):
            chars[index] = " "
    return "".join(chars)


def _quoted(text: str, clause: str) -> bool:
    """事實出自的那一句前面是冒號或「說／講／問…」：句子裡的「我」是別人。"""
    norm = normalize(text)
    at = norm.find(clause)
    prefix = norm[:at] if at > 0 else ""
    return ":" in prefix or _SPEECH_END_RE.search(prefix) is not None


_FILLER_CHARS = str.maketrans("", "", " ，,。.；;！!？?、：:\n\r\t「」『』()（）[]【】在了著的是有也都很")


def coverage_reason(fact: str, segment: str) -> str:
    """模型不能自己補內容（R2 GP2 B2）：事實的實字至少八成要出現在原文 B 那一段。"""
    wanted = normalize(fact).translate(_FILLER_CHARS)
    have = collections.Counter(normalize(segment).translate(_FILLER_CHARS))
    if not wanted:
        return "source:fabricated"
    found = sum((collections.Counter(wanted) & have).values())
    return "" if found / len(wanted) >= 0.8 else "source:fabricated"


def self_reason(
    subject: Subject, fact: str, text: str, roster: Roster, speaker: str,
    bigrams: Callable[[str], set[str]],
) -> str:
    """本人事實的衛生檢查（轉述開關開啟時）：分不出在講誰就不收（D4-1、post-impl GP2#2、R2 GP1 F3）。

    事實出自的句子：和事實重疊最多的那幾句（平手就每一句都要過）；about 是「我」時只看有「我／自己」
    的句子。那些句子裡，除了說話的人自己的稱呼以外，不能有別的家人、親屬稱謂、第三者或代名詞；
    自己的稱呼要在主詞位置（「我媽媽」「我的媽媽」是說話的人的媽媽）、後面也不能接「的」。
    """
    grams = bigrams(fact)
    scored = [(len(grams & bigrams(part)), part) for part in clauses(text)]
    scored = [(score, part) for score, part in scored if score]
    if subject.kind == "self_word":
        scored = [(score, part) for score, part in scored if any(word in part for word in SELF_WORDS)]
        if not scored:
            return "self_word_missing"
    if not scored:
        return "clause_none"
    best = max(score for score, _part in scored)
    for _score, clause in (item for item in scored if item[0] == best):
        if _quoted(text, clause):
            return "self_quoted"
        if subject.kind == "self_word" and coverage_reason(fact, clause):
            # 事實的內容其實在別的句子（「同學住台中。我去台中玩」）：不是說話的人自己的事（R3 GP2 回歸 2）
            return "self_fabricated"
        spans = _member_spans(clause, roster, speaker)
        if any(label != speaker for _s, _e, label in spans):
            return "self_other_member"
        own = [(s, e) for s, e, label in spans if label == speaker]
        if any(clause[e:e + 1] == "的" for _s, e in own):
            return "self_possessive"
        if any(not _left_ok(clause, s) for s, _e in own):
            return "self_position"
        rest = _blank_spans(clause, own)
        if _OTHER_PEOPLE_RE.search(rest) or _OTHER_KIN_RE.search(rest):
            return "self_other_people"
    return ""


def _segment_pieces(clause: str, subject: Subject, roster: Roster, speaker: str) -> list[str]:
    """B 每一次出現之後到下一個家人叫法（或這一句結尾）的那一段。"""
    spans = _member_spans(clause, roster, speaker)
    pieces = []
    for index, (start, end, label) in enumerate(spans):
        if label != subject.label:
            continue
        stop = spans[index + 1][0] if index + 1 < len(spans) else len(clause)
        pieces.append(clause[end:stop])
    return pieces


def frame_reason(pieces: Iterable[str]) -> str:
    """B 的每一段都要一開頭就是允許的句型（前面只准幾個副詞）：「爸爸說小明住台中」「爸爸陪小明在學游泳」
    這種事情其實是別人的，模型改寫成「住在台中」也看不出來（post-impl R2 GP1 F1）。"""
    for piece in pieces:
        if not _FRAME_RE.search(normalize(piece).strip()):
            return "source:frame"
    return ""


def _segment(clause: str, subject: Subject, roster: Roster, speaker: str) -> str:
    """B 那一段：B 的叫法之後到下一個家人叫法（或這一句結尾）；B 出現好幾次就全部接起來。"""
    return "".join(_segment_pieces(clause, subject, roster, speaker))


def segment_reason(segment: str, fact: str) -> str:
    """原文裡 B 那一段的檢查（post-impl GP2#1）：模型的改寫看不到的東西在這裡擋。"""
    text = normalize(segment)
    for category, pattern in _DENY_RULES:
        if pattern.search(text):
            return f"source:{category}"
    if _SOURCE_DENY_RE.search(text):
        return "source:wish_past_other"
    if _OTHER_PEOPLE_RE.search(text) or _OTHER_KIN_RE.search(text):
        return "source:other_people"
    # 否定要一樣多：只看有沒有的話，「不喜歡吃無花果」和「喜歡吃無花果」都含否定字（R3 GP2 回歸 1）
    if collections.Counter(_NEGATION_RE.findall(text)) != collections.Counter(_NEGATION_RE.findall(normalize(fact))):
        return "source:negation"
    return ""


@dataclass(frozen=True)
class Verdict:
    """一條模型給的事實要記在誰名下：label＝B（轉述）或說話的人；said_by 只有轉述才有。"""

    ok: bool
    label: str = ""
    said_by: str = ""
    reason: str = ""


def judge(
    fact: str, about: object, speaker: str, text: str, roster: Roster,
    bigrams: Callable[[str], set[str]],
) -> Verdict:
    """轉述開關開啟時，每條事實（本人或轉述）都經過這裡；呼叫的人已經檢查過 src 是說話的人本人的那一則。"""
    subject = resolve(about, speaker, roster)
    if subject.kind == "drop":
        return Verdict(False, reason=subject.reason)
    if subject.kind in ("self_word", "omitted", "self_label"):
        reason = self_reason(subject, fact, text, roster, speaker, bigrams)
        return Verdict(not reason, speaker if not reason else "", reason=reason)
    if any(hedged(part) for part in clauses(text)):
        return Verdict(False, reason="hedged")  # 整則訊息裡任何一句是猜測或問句都不收
    message = normalize(text)
    if _DENY_RULES[0][1].search(message) or _MESSAGE_DENY_RE.search(message):
        return Verdict(False, reason="message:time_or_retraction")  # 時間、過去、更正寫在別句也不收
    clause, reason = best_clause(fact, text, bigrams)
    if reason:
        return Verdict(False, reason=reason)
    if _quoted(text, clause):
        return Verdict(False, reason="quoted")
    segment = _segment(clause, subject, roster, speaker)
    reason = (
        position_reason(clause, subject, roster, speaker)
        or segment_reason(segment, fact)
        or frame_reason(_segment_pieces(clause, subject, roster, speaker))
        or bind_reason(fact, clause, subject, roster, speaker, bigrams)
        or coverage_reason(fact, segment)
    )
    if reason:
        return Verdict(False, reason=reason)
    return Verdict(True, subject.label, speaker)
