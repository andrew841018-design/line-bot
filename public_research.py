"""Public-text research policy and bounded, provider-independent evidence lookup."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime
import logging
import re
import threading
import unicodedata
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="public-research")
_SLOTS = threading.BoundedSemaphore(2)
# TODO(2026-10-03 review): 「昨天總統宣布停火」「9月24日總統在機場迎接元首」 are not
# researched (no 昨天／M月D日 here); widening this also widens what reaches search.
_CURRENT = re.compile(r"今年|本月|本週|今天|今日|目前|現在|最近|近期|最新|20\d{2}年")
_FACT = re.compile(
    r"產量|生產|供應|供給|需求|過剩|短缺|價格|便宜|昂貴|漲價|降價|上漲|下跌|"
    r"政策|法規|關稅|出口|進口|物價|通膨|利率|匯率|經濟|新聞|統計|數據|疫情|缺貨"
)
# 2026-10-03: a dated claim about a state visit was answered from stale memory
# because diplomacy is not an economic topic.  Public-affairs actors alone (not
# words like 歡迎／主席, nor country names, which family chat uses every day)
# make a dated claim public.
# Only people in public office count as actors (an institution is where a
# relative may work or visit: 「表哥在外交部上班」「去總統府參觀」).  The actor
# does the event, in the same clause, as its subject: 「大嫂去迎接總統」 and
# 「陳小明與總統會面」 are about 大嫂 and 陳小明.
_PUBLIC_ACTOR = r"總統(?!府)|副總統|元首|國家主席|總理|首相|國務卿|外長|川普|拜登|普丁|習近平|賴清德"
_PUBLIC_EVENT = (
    r"訪問|訪美|訪中|訪台|會面|會晤|峰會|國宴|歡迎|接待|迎接|簽署|簽了|協議|談判|宣布|演說|"
    r"選舉|當選|就職|制裁|關稅|開戰|停火|下台|辭職|罷免|否決"
)
_PUBLIC_AFFAIRS = re.compile(rf"(?:{_PUBLIC_ACTOR})[^，,。；;：:！？!?\n]{{0,12}}(?:{_PUBLIC_EVENT})")
_PUBLIC_PLACE = r"白宮|國會|國務院|機場|基地|國宴|峰會|聯合國|北約|歐盟|華府|華盛頓|北京|台北|臺北|東京|首爾|莫斯科"
_COUNTRY = r"美國|中國|大陸|台灣|臺灣|日本|韓國|南韓|北韓|俄羅斯|烏克蘭|以色列|伊朗|英國|法國|德國|印度|兩國|雙方|各國"
_PUBLIC_DATE = r"20\d{2}年|\d{1,2}月\d{1,2}日"
# What may stand in front of the actor: when, where, which country, who said so.
_SUBJECT_LEAD = re.compile(
    rf"(?:{_CURRENT.pattern}|昨天|前天|{_PUBLIC_DATE}|{_COUNTRY}|{_PUBLIC_PLACE}|{_PUBLIC_EVENT}"
    r"|新聞|報導|聽說|據說|說|指出|前|新任|現任|的|在|於|與|和|及|\s)*"
)
# A name written right before the title (賴總統、石破首相) is part of the actor;
# 「陳小明與總統」 ends in 與, so it is someone else's sentence.
_NOT_A_NAME = re.compile(r"[與和跟及同說在去找見到的被向對替幫給讓]")
# The only words a public-affairs search may carry (2026-10-04 review: a name
# or an illness in the same message must never reach a search engine); the
# time words keep the search recent.
_PUBLIC_TERMS = re.compile(
    rf"{_CURRENT.pattern}|昨天|前天|{_PUBLIC_DATE}|{_COUNTRY}|{_PUBLIC_ACTOR}|{_PUBLIC_EVENT}|{_PUBLIC_PLACE}"
)
# Family chat separates sentences with spaces, 「…」 and 「～」 too.
_CLAUSE_MARKS = "，,。；;：:！？!?\n …～~　"
_PRIVATE = re.compile(
    r"我|我們|你家|他家|她家|家人|媽媽|爸爸|妹妹|姊姊|姐姐|弟弟|哥哥|"
    r"阿嬤|阿公|奶奶|爺爺|外婆|外公|表哥|表姊|表姐|表弟|表妹|堂哥|堂姊|堂姐|堂弟|堂妹|"
    r"小孩|孩子|兒子|女兒|孫子|孫女|老公|老婆|姑姑|阿姨|舅舅|叔叔|伯伯|嬸嬸|"
    r"大嫂|二嫂|嫂嫂|嫂子|大哥|二哥|大姊|大姐|姊夫|姐夫|妹夫|弟媳|媳婦|女婿|公公|婆婆|岳父|岳母|"
    r"親戚|鄰居|同事|朋友|男友|女友|男朋友|女朋友|"
    r"回診|看診|看醫生|掛號|住院|出院|開刀|手術|化療|放療|洗腎|復健|吃藥|藥物|診斷|懷孕|"
    r"癌|腫瘤|中風|失智|憂鬱|焦慮症|糖尿病|高血壓|心臟病|過世|去世|喪禮|離婚|官司|訴訟|欠債|負債|借錢|"
    r"確診|生病|感冒|發燒|咳嗽|過敏|看病|開藥|急診|醫院|產檢|坐月子|生小孩|"
    r"房貸|車貸|貸款|存款|保單|他的|她的|他們的|她們的|"
    # 「小芳也確診了」「阿明的房貸」: a nickname doing something
    r"(?:小(?!時|心|吃|學|姐|說|額|型|幅|組|孩|朋|丑|鎮|島)|阿(?!里|根|拉|富|爾|伯|聯|姨|嬤|公))[\u4e00-\u9fff]"
    r"(?=也|的|說|在|要|去|跟|和|今天|昨天|最近|已經|還|又|被|生日|結婚|懷孕)|"
    r"提醒|行事曆|待辦|病歷|病情|帳號|帳戶|密碼|驗證碼|薪水|薪資|"
    r"未公開|內部|機密|保密|私密|住址|地址|電話|手機|身分證|身份證|"
    r"api[ _-]?key|secret|password|credential|access[ _-]?token|authorization|"
    r"[?&](?:token|key|auth|signature)=|sk-[A-Za-z0-9_-]+|"
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<!\d)09\d{8}(?!\d)|U[0-9a-f]{32}|"
    r"[\u4e00-\u9fff]{1,12}(?:路|街|巷|弄)\d{1,5}(?:之\d+)?號",
    re.IGNORECASE,
)
_PROMISE = re.compile(
    r"(?:我會|我來|我先|我可以|我能|讓我|等我|接下來|稍後|待會|將會|會再|先幫你|先幫您)"
    r"[^。！？!?\n]{0,20}(?:搜尋|搜索|查詢|查證|上網|查一下|查查看|查一查|查查|幫你查|幫您查)"
)


def public_query(text: str) -> str:
    """Fail closed for personal/mixed text; never derive a query from history."""
    value = (text or "").strip()
    value = re.sub(r"^(?:(?:@?咪寶|/問)[，,:：\s]*|(?:請)?(?:幫我)?(?:查一下|查詢|查證|搜尋|查)\s*)+", "", value)
    value = re.sub(r"(?:請)?(?:給我|提供給我)(?:來源|出處|連結)", "請附來源", value)
    if not value or len(value) > 240 or _PRIVATE.search(value):
        return ""
    # Reject speaker-wrapped history. Plain lines must all pass the privacy check.
    if re.search(r"\[[^\]]+\]\s*[:：]", value):
        return ""
    return re.sub(r"\s*\n\s*", "，", value)


def _public_affairs_clause(value: str) -> str:
    """The clause in which a public-office person, as its subject, does a public thing."""
    for match in _PUBLIC_AFFAIRS.finditer(value):
        start = max(value.rfind(mark, 0, match.start()) for mark in _CLAUSE_MARKS) + 1
        lead = value[start:match.start()]
        for size in (1, 2, 3):
            name = lead[-size:]
            if (len(lead) >= size and re.fullmatch(r"[\u4e00-\u9fff]+", name)
                    and not _NOT_A_NAME.search(name) and _SUBJECT_LEAD.fullmatch(lead[:-size])):
                lead = lead[:-size]
                break
        if not _SUBJECT_LEAD.fullmatch(lead):
            continue
        ends = [i for i in (value.find(mark, match.end()) for mark in _CLAUSE_MARKS) if i >= 0]
        return value[start:min(ends) if ends else len(value)]
    return ""


# 2026-10-24 (B1.4): a question about the number in a public regulation (an
# allowance, a tax rate, a fine, a visa's days) is looked up even without
# 「今年／現在」, because a reply that states such a number without a source is
# deleted (reply_guards, B1).  Only public-regulation domains count — 「公司規定」
# and 「信用卡額度」 do not.
_REGULATION_DOMAIN = re.compile(
    r"海關|入境|出境|通關|報關|檢疫|免稅|關稅|稅率|稅額|所得稅|綜所稅|營業稅|房屋稅|地價稅|牌照稅|燃料稅|"
    r"遺產稅|贈與稅|健保|勞保|勞退|國民年金|簽證|免簽|護照|罰款|罰鍰|罰單|交通規則|基本工資|最低工資|"
    r"駕照|考照|強制險|違規|特休|加班費|資遣費|退休金"
)
# 「免稅額度」 alone asks nothing; 「免稅額度是…」「有多少」「幾天」 do.
_REGULATION_QUANTITY_ASK = re.compile(
    r"多少|幾(?!乎)|多久|最多|最少|(?:上限|下限|額度|限額|稅率|費率)(?:是|為|有)"
)


def _regulation_clauses(value: str) -> list[str]:
    parts = [part.strip() for part in re.split(f"[{_CLAUSE_MARKS}]", value) if part.strip()]
    return [part for part in parts if _REGULATION_DOMAIN.search(part)]


# 2026-10-29 (follow-up 5, item 2): what a clause the search drops asked about, put back after
# the domain clause from these fixed words only, in message order: never a name or anything
# else said there (「去韓國入境，酒可以帶幾瓶？」 searches 「去韓國入境，酒幾瓶」).  幾 counts only
# before a unit (not 好幾, 幾乎).
_ASK_OBJECTS = (
    "酒類", "烈酒", "紅酒", "啤酒", "酒", "香菸", "菸品", "雪茄", "電子菸", "菸", "現金", "外幣", "美金", "台幣",
    "人民幣", "日圓", "日幣", "韓元", "肉品", "肉類", "水果", "蔬菜", "植物", "奶粉",
)
_ASK_UNITS = (
    "瓶", "罐", "條", "包", "盒", "支", "根", "公升", "毫升", "升", "公斤", "公克", "克", "斤", "天", "日", "晚",
    "個月", "個", "年", "次", "件", "台", "萬", "千", "百", "元", "塊",
)
_ASK_WORDS = re.compile(
    "|".join(re.escape(word) for word in sorted(_ASK_OBJECTS, key=len, reverse=True))
    + r"|多少|多久|最多|最少|(?:上限|下限|額度|限額|稅率|費率)(?:是|為|有)|(?<!好)幾(?:"
    + "|".join(re.escape(unit) for unit in sorted(_ASK_UNITS, key=len, reverse=True)) + ")"
)


def _regulation_ask_phrase(text: str) -> str:
    """The fixed words in ``text`` that say what it asks, in order (「酒可以帶幾瓶」 → 「酒幾瓶」)."""
    return "".join(match.group(0) for match in _ASK_WORDS.finditer(text or ""))


def _with_regulation_ask(value: str, chosen: list[str]) -> tuple[str, str]:
    """(``chosen``, clauses of ``value``, as the search text; the ask phrase appended to it).
    When ``value`` asks a public regulation's number and the chosen clauses do not carry
    the ask, 「，」 and the ask phrase of the dropped clauses follow (2026-10-29, item 2);
    the phrase is "" when nothing was appended."""
    kept = "，".join(chosen)
    if (not _regulation_clauses(value) or not _REGULATION_QUANTITY_ASK.search(value)
            or _REGULATION_QUANTITY_ASK.search(kept)):
        return kept, ""
    parts = [part.strip() for part in re.split(f"[{_CLAUSE_MARKS}]", value) if part.strip()]
    phrase = "".join(_regulation_ask_phrase(part) for part in parts if part not in chosen)
    return (f"{kept}，{phrase}", phrase) if phrase else (kept, "")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


def regulation_clause(text: str, family_names=()) -> str:
    """What to search for an addressed question about a regulation's number; ``""`` if it is not one.

    The message must pass the privacy check (`public_query`), name a public-regulation
    domain and ask for a quantity.  A family alias or kinship word (the caller passes
    the names, or a function that returns them), an @name or a first-person word
    anywhere in it means no search:
    a name must never reach the search provider.  Names are compared as `mentions_any`
    compares them (2026-10-29): NFKC and casefolded (「ＡＭＹ」 and 「amy」 are 「Amy」),
    spaces, symbols and invisible letters ignored, with its masks for 日本人 and the
    goddess 媽祖; @ is looked for in the NFKC form (＠ and ﹫ too).  The list of names
    failing to load, or being empty, means no search at all (2026-10-28: nobody can say
    whom the question names).  Only the clauses that carry the domain word are returned (v3 #4).

    Residual: a given name nobody configured, inside the domain clause itself, is
    not recognised and goes out as written.
    """
    value = public_query(text)
    if not value or "@" in _fold(value):
        return ""
    clauses = _regulation_clauses(value)
    if not clauses or not _REGULATION_QUANTITY_ASK.search(value):
        return ""
    # ``family_names`` may be a function: the names are read only for a real candidate
    try:
        names = family_names() if callable(family_names) else family_names
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "regulation research skipped: name source unavailable error_type=%s", type(exc).__name__
        )
        return ""
    if mentions_any(value, names):  # an empty list counts as a match: no search
        return ""
    return "，".join(clauses)


def requires_regulation_research(text: str, family_names=()) -> bool:
    return bool(regulation_clause(text, family_names))


def search_query(text: str) -> str:
    """What a search engine may receive for ``text``.

    A price/policy claim sends the clauses that make it, as written.  A claim
    that is public only because a public-office person did something sends only
    public words from that clause (dates, countries, offices, events, places).
    Names and anything else in a family member's message stay home.  A question about a
    public regulation's number keeps its ask: 「去韓國入境，酒可以帶幾瓶？」 sends
    「去韓國入境，酒幾瓶」 (`_with_regulation_ask`, 2026-10-29).
    """
    return _search_query(text)[0]


def _search_query(text: str) -> tuple[str, str]:
    """(search_query's text, the ask phrase `_with_regulation_ask` appended to it, or "")."""
    value = public_query(text)
    if not value:
        return "", ""
    if _FACT.search(value):
        parts = [part.strip() for part in re.split(f"[{_CLAUSE_MARKS}]", value) if part.strip()]
        claim = [part for part in parts if _FACT.search(part)]
        return (value, "") if len(claim) == len(parts) else _with_regulation_ask(value, claim)
    clause = _public_affairs_clause(value)
    if not clause:
        # 2026-10-24: a message about a public regulation sends its domain clause only
        regulation = _regulation_clauses(value)
        return _with_regulation_ask(value, regulation) if regulation else (value, "")
    return " ".join(dict.fromkeys(m.group(0) for m in _PUBLIC_TERMS.finditer(clause))), ""


def requires_current_research(text: str) -> bool:
    value = public_query(text)
    return bool(
        value
        and _CURRENT.search(value)
        and (_FACT.search(value) or _public_affairs_clause(value))
    )


def has_search_promise(text: str) -> bool:
    return bool(_PROMISE.search(text or ""))


def dated_query(text: str) -> str:
    year = datetime.now(ZoneInfo("Asia/Taipei")).year
    return re.sub(r"今年", f"{year}年", text)


def collect(
    collector, text: str, *, timeout: float = 16.0, report: dict | None = None
) -> list[dict]:
    """Bound caller latency and concurrent abandoned work, without executor join.

    ``report``, when given, receives why nothing usable came back (``reason``)
    and how many rows the collector returned (``rows``), for the research log.
    """
    report = {} if report is None else report
    query = public_query(text)
    if not query:
        report["reason"] = "no_query"
        return []
    if not _SLOTS.acquire(blocking=False):
        report["reason"] = "busy"
        return []
    try:
        future = _POOL.submit(collector, query)
    except Exception:
        _SLOTS.release()
        report["reason"] = "error"
        return []
    future.add_done_callback(lambda _: _SLOTS.release())
    try:
        rows = future.result(timeout=timeout)
    except FutureTimeout:
        report["reason"] = "timeout"
        return []
    except Exception:
        report["reason"] = "error"
        return []
    if not isinstance(rows, list):
        report["reason"] = "error"
        return []
    report["rows"] = len(rows)
    usable = []
    for row in (rows or [])[:6]:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "")
        body = str(row.get("full_text") or row.get("snippet") or "").strip()
        # A title, publisher name, URL or search attempt alone is not evidence.
        if urlparse(url).scheme in {"http", "https"} and len(body) >= 20:
            usable.append(row)
    if not usable:
        report["reason"] = "no_usable"
    return usable


NO_EVIDENCE = "這次搜尋未取得足夠的可靠資料，還無法核實這個說法。"
NO_ANSWER = "已取得相關資料，但這次未能產生可靠的查證結論。"

# 2026-10-28: plain questions the search found nothing for were answered with
# NO_EVIDENCE, as if someone had forwarded a claim.  A plain question carries
# no claim of its own: it searches on (main._plain_question_answer) and gets
# NO_SOURCE_QUESTION only when every layer fails.  Claims keep NO_EVIDENCE.
NO_SOURCE_QUESTION = "這次沒查到可靠的資料，暫時沒辦法回答這個問題。"
# Appended after the reply guards (「沒查到…資料」 is what the search-claim guard
# drops, and 「最新資料」 next to any digit is what the outbound validator blocks).
COMMON_KNOWLEDGE_NOTE = "（以上依一般常識回答，這次沒有找到可引用的來源。）"
_QUESTION_FORM = re.compile(
    r"[?？]|嗎|呢|什麼|甚麼|哪|如何|怎麼|怎樣|為什麼|為何|多少|"
    r"(?<![這那好前過近沒經十百千萬兩一二三四五六七八九\d])幾(?![乎十百千萬])|"  # not 最近幾天, 二十幾度, 幾十年
    r"有沒有|是不是|會不會|能不能|可不可以|要不要|好不好|"
    # a request: 「推薦台南好玩的景點」 (not 「這家很推薦」「推薦這家，超好吃」)
    r"^(?:請|求)?(?:推薦|介紹)(?!(?:這|那)[家間部個本支款位]|大家|你們)"
)
# Asking whether something said is so: a claim to check, not a plain question.
_CLAIM_CHECK = re.compile(
    r"真的嗎|真的假的|是真的|是假的|真假|假的嗎|謠言|闢謠|假消息|假新聞|聽說|據說|網傳|傳言|轉傳|"
    r"有人說|可信|能信|屬實|查證|核實|對嗎|是嗎|是這樣嗎|對不對|沒錯吧|有這回事|有這種事|"
    r"is\s+it\s+true|fake|hoax|rumou?r",
    re.IGNORECASE,
)
# Answers that change with time, place or plans, and health, law and money, where
# a wrong answer costs too much: never from memory.
_NOT_FROM_MEMORY = re.compile(
    r"明天|後天|昨天|今晚|週末|下週|下個月|明年|大家|你們|誰|"
    r"天氣|票價|多少錢|營業|幾點|股|行情|油價|規定|新制|簽證|餐廳|住宿|"
    r"病|症|藥|醫|疾|痛|癌|瘤|診|療|健康|身體|血壓|血糖|懷孕|"
    r"法律|法規|稅|保險|保費|健保|勞保|國保|投資|理財|貸款|利息|基金"
)


def is_plain_question(text: str) -> bool:
    """A short question that carries no claim to check (``text``: links removed)."""
    value = public_query(text)
    # A regulation question stays a plain question (excluding it would bring
    # NO_EVIDENCE back): reply_guards' number guard judges the Gemini answer.
    return bool(
        value
        and len(value) <= 80
        and _QUESTION_FORM.search(value)
        # Before public_query, which drops a leading 「幫我查證」.
        and not _CLAIM_CHECK.search(text or "")
        and not requires_current_research(text)
    )


# What a question cut down by search_query may take back from the rest of it:
# countries, places and travel words, never names or anything else said there.
_SUBJECT_TERMS = re.compile(
    rf"{_COUNTRY}|{_PUBLIC_PLACE}|"
    r"新北|桃園|台中|臺中|台南|臺南|高雄|基隆|新竹|苗栗|彰化|南投|雲林|嘉義|屏東|宜蘭|花蓮|"
    r"台東|臺東|澎湖|金門|馬祖|墾丁|阿里山|日月潭|太魯閣|"
    r"大阪|京都|北海道|沖繩|福岡|名古屋|釜山|濟州|香港(?!腳)|澳門|上海|新加坡|曼谷|清邁|峇里島|"
    r"越南|泰國|菲律賓|馬來西亞|吉隆坡|印尼|紐西蘭|澳洲|雪梨|加拿大|溫哥華|倫敦|巴黎|歐洲|"
    r"紐約|夏威夷|關島|"
    r"旅遊|旅行|自由行|跟團|出國|機票|飯店|住宿|民宿|簽證|退稅|換匯|高鐵|台鐵|捷運|門票|租車"
)
# A listed word counts only where a place or trip starts: at a clause's start,
# after these verbs, or right after another listed word.  「張嘉義」 is a name.
_SUBJECT_AFTER = "去到在往飛從回來住"
# A clause made only of these loses nothing when search_query drops it.
# One character class for the laughs, not 哈+ inside the outer +: nested repeats
# backtrack exponentially on a long 哈哈哈… followed by anything else (2026-10-28 review).
_COURTESY = re.compile(
    r"(?:(?:請|想)?問(?:一下)?|請教(?:一下)?|謝謝(?:你|您|大家)?|多謝|感謝|感恩|拜託|麻煩了?|不好意思|"
    r"[哈呵嘿嗯喔哦欸])+"
)


def _bare(text: str) -> str:
    """Words only: punctuation, spaces and emoji removed."""
    return re.sub(r"[\W_]+", "", text or "")


def _ask_kept(part: str, appended: str) -> bool:
    """``part`` is a dropped clause whose ask phrase search_query put back: it is in what
    `_with_regulation_ask` ``appended``, not merely in the kept clauses (2026-10-29 review)."""
    phrase = _regulation_ask_phrase(part)
    return bool(phrase) and phrase in appended


def _asks(kept: str, value: str) -> bool:
    """``kept`` still asks: a question word, or a kept clause the message followed with ？."""
    if _QUESTION_FORM.search(kept):
        return True
    return any(
        part and re.search(re.escape(part) + r"[\s…～~！!]*[？?]", value)
        for part in re.split(f"[{_CLAUSE_MARKS}]", kept)
    )


def complete_search_query(text: str) -> str:
    """What may be searched for the whole of a question; "" when its subject cannot go.

    search_query keeps only the price/policy clause of 「去日本旅遊，物價高嗎？」:
    「物價高嗎」 is a question about somewhere else.  Countries, places and travel
    words from the rest come back (「日本 旅遊 物價高嗎」); names and anything
    else said there stay home, as in search_query (2026-10-28).  Since claude/fix-b-replies
    search_query also keeps only the regulation clauses, and drops the 「？」 with the rest:
    a cut that took only punctuation or courtesy (請問、謝謝、哈哈) took nothing.  Nor did one
    whose ask search_query put back (「酒可以帶幾瓶」 as 「酒幾瓶」, 2026-10-29).
    """
    value = public_query(text)
    kept, appended = _search_query(text)
    if not value or not kept or not _bare(kept):
        return ""
    if _bare(kept) == _bare(value):
        return kept
    left = _bare(kept)
    # each dropped clause as written (its ask phrase is read as search_query read it) and bare
    dropped = [(part, _bare(part)) for part in re.split(f"[{_CLAUSE_MARKS}]", value)]
    dropped = [(part, bare) for part, bare in dropped if bare and bare not in left]
    if all(_COURTESY.fullmatch(bare) or _ask_kept(part, appended) for part, bare in dropped):
        return kept if _asks(kept, value) else ""
    subjects: list[str] = []
    chained = -1  # where the last listed word ended
    for match in _SUBJECT_TERMS.finditer(value):
        start = match.start()
        before = value[start - 1] if start else ""
        if start and start != chained and before not in _CLAUSE_MARKS and before not in _SUBJECT_AFTER:
            continue
        chained = match.end()
        if match.group(0) not in kept:
            subjects.append(match.group(0))
    if not subjects or not _asks(kept, value):
        return ""
    restored = " ".join([*dict.fromkeys(subjects), kept])
    return restored if public_query(restored) else ""


def answerable_from_memory(text: str) -> bool:
    """A plain question whose answer does not change with time, place or plans."""
    value = public_query(text)
    return bool(
        is_plain_question(text)
        and not _CURRENT.search(value)
        and not _FACT.search(value)
        and not _public_affairs_clause(value)
        and not _NOT_FROM_MEMORY.search(value)
    )


def asks_for_number(text: str) -> bool:
    """The question asks how many, how much or how long: what a regulation number answers."""
    return bool(_REGULATION_QUANTITY_ASK.search(public_query(text)))


# Moved here from main._plain_question_names on 2026-10-29 (follow-up 5 part 2, the TODO R2 of
# the 2026-10-28 review): regulation_clause and the plain-question path share the rule, and
# the sentences in test_research_plain_question.py's mask test are its contract.  The kinship
# words that name nobody in a search question (當事人, 對方) are left out at the source
# (reply_guards._SEARCH_KINSHIP), so a configured alias, display name or turn label that
# happens to be one of them still counts.  本人 and 媽 stay deny terms, checked
# after the words below are masked (2026-10-29 privacy review, REVIEW_gp1_plan_s9.md
# and REVIEW_gp2_plan_s9.md):
# - 本人 skips only 日本人, on the NFKC-folded text as written: nothing is stripped for
#   it, so 「王旭日、本人…」 never joins into 日本人;
# - 媽 skips 媽祖／媽閣 only as the goddess or her temple, read on the folded text with
#   what _bare drops taken out (「舅媽祖 厝」 is no 媽祖 before a space) and never after
#   a kinship prefix (「舅媽祖廟」 is 舅媽's).  「媽去日本要簽證嗎」 is one person.
_MASKED_BEFORE = {
    "本人": re.compile(r"日本人"),
    "媽": re.compile(
        r"(?<![舅姑姨乾契義繼養親奶乳伯岳])(?:媽祖|媽閣)(?=廟|宮|遶境|繞境|進香|誕|信仰|文化|[，,。！？!?]|$)"
    ),
}
_MASK_VIEW = re.compile(r"[^\w，,。！？!?]+|_")  # what _bare drops, less the marks 媽's mask reads
# 媽 kept apart from 祖／閣 in the folded text (「媽 祖廟」「媽、祖廟」) is no goddess word: the
# stripped view would join them, so the bare 媽 counts as written (GP2 Phase 6 P6-1).
_MA_SPLIT = re.compile(r"媽[\W_]+[祖閣]")
# 2026-10-29 (follow-up 5): simplified kinship characters are the traditional ones (「我妈」 is
# 媽), and letters that show nothing split no name: the Hangul fillers (which _bare keeps as
# letters) and the zero-width characters.  The masks release text, so they read it with those
# letters still in: 媽, U+3164, 祖廟 is no 媽祖, nor is 王旭日, U+200B, 本人 a 日本人.  Only the
# comparison that blocks drops them (research review, 2026-10-29).
_SIMPLIFIED_KINSHIP = str.maketrans({"妈": "媽", "爷": "爺", "嬷": "嬤"})
_INVISIBLE = re.compile(r"[\u115f\u1160\u3164\uffa0\u200b-\u200d\u2060\ufeff]")


def _kin_fold(text: str) -> str:
    """NFKC, casefolded, simplified kinship characters as the traditional ones."""
    return _fold(text).translate(_SIMPLIFIED_KINSHIP)


def _blocking(text: str) -> str:
    """What a name is looked for in: the invisible letters out, then words only."""
    return _bare(_INVISIBLE.sub("", text))


def mentions_any(text: str, names) -> bool:
    """Whether ``text`` holds one of ``names``, compared as regulation_clause does
    (NFKC, casefolded) and with spaces and symbols ignored (「王 大同」).  An empty
    list counts as a match: nobody can say whom the text names (fails closed).

    2026-10-29 (follow-up 5): the bare 本人 and 媽 are looked for after their masks (日本人;
    the goddess 媽祖／媽閣), and a 媽 kept apart from 祖／閣 counts at once; simplified
    kinship characters are the traditional ones and invisible letters split no name,
    while the masks read the text with them still in."""
    terms = [term for term in (_blocking(_kin_fold(name)) for name in names or () if isinstance(name, str)) if term]
    if not terms:
        return True
    folded = _kin_fold(text)
    whole = _blocking(folded)
    for term in terms:
        if term == "媽":
            if _MA_SPLIT.search(folded):
                return True
            seen = _blocking(_MASKED_BEFORE[term].sub("", _MASK_VIEW.sub("", folded)))
        elif term == "本人":
            seen = _blocking(_MASKED_BEFORE[term].sub("", folded))
        else:
            seen = whole
        if term in seen:
            return True
    return False
