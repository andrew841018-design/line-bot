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

# 必須是這幾類之一（GP2 S1 (a)）。
_FRAME_RE = re.compile(
    r"住|搬到|搬去|上班|工作|公司|上學|就讀|讀書|唸書|念書"
    r"|(?:讀|念|唸)(?:大學|高中|國中|國小|研究所|碩士|博士)|大學|研究所"
    r"|喜歡|愛吃|愛喝|愛看|愛聽|愛玩|最愛|不吃|不喝|討厭吃|討厭喝|興趣|迷上|收藏"
    r"|養(?:了|著)?\S{0,3}(?:貓|狗|魚|鳥|兔|倉鼠|烏龜|寵物)"
    r"|在學|學(?:日文|英文|韓文|法文|德文|西班牙文|鋼琴|吉他|小提琴|烏克麗麗|畫畫|繪畫|游泳|跳舞|唱歌"
    r"|烹飪|做菜|攝影|書法|瑜伽|瑜珈|程式|開車|騎車|打鼓)"
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
    ("pronoun", re.compile(r"[我咱俺你妳您祂]|(?<!其)[他她它]")),
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
        r"|精神不|沒精神|沒力|無力|疲倦|疲勞|很累|睡|夜尿|頻尿|尿|便祕|便秘|血|照顧|不便|行動"
        r"|一個人在家|獨居|輪椅|拐杖|看護|安養|長照|吃藥|身體"
    )),
    ("sensitive", re.compile(
        r"心情|情緒|壓力|憂|難過|傷心|生氣|哭|寂寞|孤單|戀愛|暗戀|喜歡的人|成績|分數|考試|考上|落榜|補習|升學"
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
_HEDGE_RE = re.compile(r"是不是|好像|聽說|可能|應該|也許|大概|似乎|不知道|難道|嗎|呢|吧|[?？]")


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

SELF_WORDS = frozenset({"我", "自己", "我自己", "本人"})
OMITTED = "（省略）"
_OMITTED_WORDS = frozenset({"", "省略", "(省略)", "無", "none", "self"})
# 配偶：只有這兩個稱呼剛好都在名單裡才用（媽媽說的「老公」是爸爸，爸爸說的「老婆」是媽媽）。
FATHER, MOTHER = "爸爸", "媽媽"
_SPOUSE_TERMS = {MOTHER: ("老公", "我老公"), FATHER: ("老婆", "我老婆")}
# 一個稱呼左邊緊接這些字之一才算在主詞位置（D1：精準優先，閉集合）。
_CONNECTORS = (
    "其實", "因為", "所以", "但是", "但", "而且", "還有", "不過", "反正", "對了", "現在", "平常",
    "通常", "常常", "一直", "然後", "後來", "最近", "以前", "我們", "你們", "咱們", "大家",
)
_BAD_PLURAL = ("他們", "她們", "它們")
_BAD_LEFT = frozenset("我你妳您他她它的")
# 本人事實（主詞省略）的子句裡出現這些字，就分不出是在講誰（0086f5：同學的事被記在說話的人名下）。
_OTHER_PEOPLE_RE = re.compile(
    r"同學|同事|朋友|老闆|主管|老師|鄰居|室友|客戶|男友|女友|男朋友|女朋友|他們|她們|別人|人家|(?<!其)[他她它]"
)
_TERM_STRIP_RE = re.compile(r"[\s\"'「」『』“”‘’《》〈〉（）()\[\]【】:：,，。.!！?？@＠]+")


@dataclass(frozen=True)
class Roster:
    """轉述用的家人名單：terms＝正規化後的叫法 → 稱呼（一個叫法對到兩個人的已拿掉）。"""

    labels: frozenset[str]
    terms: Mapping[str, str]

    def spouse_of(self, speaker: str, term: str) -> str:
        if term in _SPOUSE_TERMS.get(speaker, ()) and {FATHER, MOTHER} <= self.labels:
            return MOTHER if speaker == FATHER else FATHER
        return ""


def term_key(term: str) -> str:
    """模型給的 about：去引號、標點、空白，NFKC、不分大小寫。"""
    return _TERM_STRIP_RE.sub("", normalize(term))


def build_roster(labels: Iterable[str], aliases: Mapping[str, str]) -> Roster:
    """labels：可以掛轉述的家人稱呼；aliases：叫法 → 稱呼（呼叫的人先清理成同一種寫法）。"""
    label_set = frozenset(label for label in labels if label)
    found: dict[str, set[str]] = {}
    for label in label_set:
        found.setdefault(term_key(label), set()).add(label)
    for term, label in aliases.items():
        if label in label_set and term_key(term):
            found.setdefault(term_key(term), set()).add(label)
    terms = {term: next(iter(owners)) for term, owners in found.items() if len(owners) == 1 and term}
    return Roster(labels=label_set, terms=terms)


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
    label = roster.terms.get(key, "")
    if not label:
        return Subject("drop", reason="unresolved")
    if label == speaker:
        return Subject("self_label", label, key)
    return Subject("member", label, key)


def _member_spans(clause: str, roster: Roster, speaker: str) -> list[tuple[int, int, str]]:
    """子句裡每一個家人叫法的位置（最長比對、不重疊）：[(開始, 結束, 稱呼)]。"""
    spans: list[tuple[int, int, str]] = []
    keys = sorted(
        [*roster.terms, *(t for t in _SPOUSE_TERMS.get(speaker, ()) if roster.spouse_of(speaker, t))],
        key=len, reverse=True,
    )
    taken = [False] * len(clause)
    for key in keys:
        label = roster.terms.get(key) or roster.spouse_of(speaker, key)
        start = clause.find(key)
        while start != -1:
            end = start + len(key)
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
    return before.endswith(_CONNECTORS)


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


def overlapping_clauses(fact: str, text: str, bigrams: Callable[[str], set[str]]) -> list[str]:
    grams = bigrams(fact)
    return [c for c in clauses(text) if grams & bigrams(c)]


def self_reason(
    subject: Subject, fact: str, text: str, roster: Roster, speaker: str,
    bigrams: Callable[[str], set[str]],
) -> str:
    """本人事實的衛生檢查（轉述開關開啟時）：分不出在講誰就不收（D4-1）。"""
    if subject.kind == "self_label":
        return ""
    parts = overlapping_clauses(fact, text, bigrams)
    if not parts:
        return "clause_none"
    if subject.kind == "self_word":
        return "" if any(word in part for part in parts for word in SELF_WORDS) else "self_word_missing"
    for part in parts:
        if _OTHER_PEOPLE_RE.search(part):
            return "self_other_people"
        if any(label != speaker for _s, _e, label in _member_spans(part, roster, speaker)):
            return "self_other_member"
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
    clause, reason = best_clause(fact, text, bigrams)
    if reason:
        return Verdict(False, reason=reason)
    if hedged(clause):
        return Verdict(False, reason="hedged")
    reason = position_reason(clause, subject, roster, speaker) or bind_reason(
        fact, clause, subject, roster, speaker, bigrams
    )
    if reason:
        return Verdict(False, reason=reason)
    return Verdict(True, subject.label, speaker)
