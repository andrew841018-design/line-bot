"""Public-text research policy and bounded, provider-independent evidence lookup."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import re
import threading
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


def search_query(text: str) -> str:
    """What a search engine may receive for ``text``.

    A price/policy claim sends the clauses that make it, as written.  A claim
    that is public only because a public-office person did something sends only
    public words from that clause (dates, countries, offices, events, places).
    Names and anything else in a family member's message stay home.
    """
    value = public_query(text)
    if not value:
        return ""
    if _FACT.search(value):
        parts = [part.strip() for part in re.split(f"[{_CLAUSE_MARKS}]", value) if part.strip()]
        claim = [part for part in parts if _FACT.search(part)]
        return value if len(claim) == len(parts) else "，".join(claim)
    clause = _public_affairs_clause(value)
    if not clause:
        return value
    return " ".join(dict.fromkeys(m.group(0) for m in _PUBLIC_TERMS.finditer(clause)))


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


def collect(collector, text: str, *, timeout: float = 16.0) -> list[dict]:
    """Bound caller latency and concurrent abandoned work, without executor join."""
    query = public_query(text)
    if not query or not _SLOTS.acquire(blocking=False):
        return []
    try:
        future = _POOL.submit(collector, query)
    except Exception:
        _SLOTS.release()
        return []
    future.add_done_callback(lambda _: _SLOTS.release())
    try:
        rows = future.result(timeout=timeout)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    usable = []
    for row in (rows or [])[:6]:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "")
        body = str(row.get("full_text") or row.get("snippet") or "").strip()
        # A title, publisher name, URL or search attempt alone is not evidence.
        if urlparse(url).scheme in {"http", "https"} and len(body) >= 20:
            usable.append(row)
    return usable


NO_EVIDENCE = "這次搜尋未取得足夠的可靠資料，還無法核實這個說法。"
NO_ANSWER = "已取得相關資料，但這次未能產生可靠的查證結論。"
