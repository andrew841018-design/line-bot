"""kb_normalize — 地點知識庫的正規化與嚴格解析（2026-10-31，第一批段一）。

Andrew 10/30：「從對話中自行抓取」、資料要「驗證再驗證」。審查員 3 實測現有守門的比對鍵
太粗（只看路名後 3 字、不看縣市；店名子字串就算對到；卡號會被當成電話），所以知識庫
不用 reply_policy 那套鍵，自己做一套完整的正規化：

- 電話：破折號、點號、全形、+886 都先統一，只留 ASCII 數字；台北 02 一定 10 碼；
  格式不像台灣電話的回 None。
- 地址：要拆得出縣市、區、路（段、巷、弄）和號，拆不全回 None；里、鄰、樓層不進比對。
- 店名：NFKC、臺→台、去空白；藏有看不見的字元一律拒收；連鎖店要寫分店；
  長得像機關的名稱只能當郵局或機關。
- 網域：轉址後的網站代號；同一品牌的不同網域（google.com／google.com.tw、
  tripadvisor.com／.com.tw）算一個；短網址、快取、代理、搜尋結果頁、punycode 拒收。
- 網頁：轉成段落文字（換行也切段），判斷店名和地址、電話是不是「綁在一起」：
  店名前面要有邊界（開頭、空白、標點；英數字結尾的店名後面也要），地址電話要緊接在店名後面，
  中間只准有標點和「地址、電話、位於」這類連接字；另外回報綁上的地址有沒有寫到縣市。

2026-10-31 實作審查（審查員 3 實測反例）後改寫，最終版審查（審查員 1、3）後再收緊；
只有純函式，不連網、不讀寫檔案。
"""
from __future__ import annotations

import functools
import html
import re
import unicodedata
from html.parser import HTMLParser
from typing import NamedTuple
from urllib.parse import urlsplit

# ── 文字 ──────────────────────────────────────────────────────────────────────

_DASHES = "‐‑‒–—―−ー－﹣⁃"
_DASH_TABLE = {ord(ch): "-" for ch in _DASHES}
_CN_NUM = {"〇": 0, "零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_EMOJI_JOINERS = frozenset("‍︎️⃣")


def has_hidden_chars(text: str, *, emoji_ok: bool = False) -> bool:
    """有沒有看不見的字元：格式字元（零寬、雙向控制、標籤字元）、私用區、控制字元。

    這些字會讓顯示的號碼和實際不同（審查員 3 C8）。``emoji_ok`` 給掃描家人訊息用：
    表情符號組合用的 ZWJ、變體選擇符不算。
    """
    for ch in text or "":
        if emoji_ok and ch in _EMOJI_JOINERS:
            continue
        category = unicodedata.category(ch)
        if category in ("Cf", "Co", "Cs") or (category == "Cc" and ch not in "\n\t"):
            return True
    return False


def nfkc(text: str) -> str:
    """全形轉半形、破折號統一、臺→台。"""
    return unicodedata.normalize("NFKC", text or "").translate(_DASH_TABLE).replace("臺", "台")


def compact(text: str) -> str:
    """比對用：nfkc 後去掉所有空白、英文轉小寫。"""
    return re.sub(r"\s+", "", nfkc(text)).lower()


def _cn_to_int(token: str) -> int | None:
    """「十二」「二十」「一百零五」這類中文數字；純 ASCII 數字照轉。"""
    if re.fullmatch(r"[0-9]+", token):
        return int(token)
    if not token or any(ch not in _CN_NUM and ch not in "十百" for ch in token):
        return None
    total, current = 0, 0
    for ch in token:
        if ch == "百":
            total += (current or 1) * 100
            current = 0
        elif ch == "十":
            total += (current or 1) * 10
            current = 0
        else:
            current = current * 10 + _CN_NUM[ch]
    return total + current


def _cn_digits(text: str, unit: str) -> str:
    """「一段」→「1段」、「十二巷」→「12巷」：只換單位字前面的中文數字。"""
    def repl(match: re.Match) -> str:
        value = _cn_to_int(match.group(1))
        return f"{value}{unit}" if value is not None else match.group(0)

    return re.sub(rf"([〇零一二兩三四五六七八九十百]+){unit}", repl, text)


# ── 電話 ──────────────────────────────────────────────────────────────────────

_EXT_RE = re.compile(r"\s*(?:轉|分機|ext\.?|#)\s*[0-9]{1,6}\s*$", re.I)
# 抓候選時，號碼前面緊接這些字就不是電話（卡號、訂單號、取貨號；審查員 3 C5）。
NUMBER_WORDS_RE = re.compile(
    r"卡號|信用卡|帳號|帳戶|訂單|單號|編號|統編|發票|條碼|代碼|序號|會員|追蹤|物流|繳費|驗證碼|密碼|"
    r"取貨|取件|代號|票號"
)
_DIGIT_RUN_RE = re.compile(r"\+?[0-9(][0-9\s\-.()]{7,24}[0-9]")
_DOT_GROUPS_RE = re.compile(r"[0-9]{2,4}\.[0-9]{3,4}\.[0-9]{3,4}")


def parse_phone(raw: str) -> str | None:
    """嚴格解析一支台灣電話，回只有 ASCII 數字的寫法（含區碼）；不像電話就回 None。

    手機 09 開頭 10 碼、0800 免付費 10 碼；台北 02 開頭 10 碼；其他市話 0[3-8] 開頭 9–10 碼。
    分機拿掉不存。點號只收「02.2345.6789」這種分組（「0.912345678」是小數）。
    沒有區碼的 8 碼、卡號、阿拉伯－印度數字都不收。
    """
    if not raw or has_hidden_chars(raw):
        return None
    text = _EXT_RE.sub("", nfkc(raw)).strip()
    if not re.fullmatch(r"[0-9\s\-.()+]{8,26}", text):
        return None
    if "." in text and not _DOT_GROUPS_RE.fullmatch(text):
        return None
    digits = re.sub(r"[^0-9]", "", text)
    if text.startswith("+") or (digits.startswith("886") and len(digits) in (11, 12)):
        if not digits.startswith("886"):
            return None
        rest = digits[3:]
        digits = rest if rest.startswith("0") else "0" + rest
    if re.fullmatch(r"09[0-9]{8}", digits) or re.fullmatch(r"0800[0-9]{6}", digits):
        return digits
    if re.fullmatch(r"02[0-9]{8}", digits) or re.fullmatch(r"0[3-8][0-9]{7,8}", digits):
        return digits
    return None


def find_phones(text: str) -> list[tuple[int, int, str]]:
    """在一段文字裡找電話：(起點, 終點, 數字)。位置是 nfkc 後的位置。

    數字串的正規式會把空白、破折號隔開的數字接成一串，所以「4000 0900 000 000」整串超過
    12 碼就不算；前面有卡號、訂單這類字的也不算。
    """
    norm = nfkc(text)
    found: list[tuple[int, int, str]] = []
    for match in _DIGIT_RUN_RE.finditer(norm):
        run = match.group(0)
        if len(re.sub(r"[^0-9]", "", run)) > 12:
            continue
        if NUMBER_WORDS_RE.search(norm[max(0, match.start() - 8):match.start()]):
            continue
        digits = parse_phone(run)
        if digits:
            found.append((match.start(), match.end(), digits))
    return found


# ── 地址 ──────────────────────────────────────────────────────────────────────

CITIES = (
    "台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市", "基隆市", "新竹市", "嘉義市",
    "新竹縣", "苗栗縣", "彰化縣", "南投縣", "雲林縣", "嘉義縣", "屏東縣", "宜蘭縣", "花蓮縣",
    "台東縣", "澎湖縣", "金門縣", "連江縣",
)
_CITY = "(?:" + "|".join(CITIES) + ")"
# 區名先試「…區」，再試鄉、鎮、縣轄市：「前鎮區」「平鎮區」「新市區」「左鎮區」才不會被拆成
# 「前鎮」「平鎮」「新市」「左鎮」（審查員 3 實測）。
_DISTRICT = r"(?P<district>[一-鿿]{1,3}?區|[一-鿿]{1,3}?(?:鄉|鎮|市))"
ROAD_SUFFIX = r"(?:路|街|大道)"
_ROAD = r"(?P<road>[一-鿿]{1,8}?" + ROAD_SUFFIX + ")"
_TAIL = (
    r"(?P<section>[0-9]{1,2}段)?(?P<lane>[0-9]{1,4}巷)?(?P<alley>[0-9]{1,4}弄)?"
    r"(?P<number>[0-9]{1,5}(?:之[0-9]{1,4})?號(?:之[0-9]{1,4})?)"
)
# 里、鄰放在最後才試（??）：「大里區大里東路1號」的「大里」是路名，不是里（審查員 1 實測）。
_FULL_ADDRESS_RE = re.compile(
    rf"^(?:[0-9]{{3,6}})?(?P<city>{_CITY}){_DISTRICT}"
    r"(?:[一-鿿]{1,4}?[里村])??(?:[0-9]{1,3}鄰)??"
    + _ROAD + _TAIL
    + r"(?:(?:[0-9]{1,3}樓|B[0-9]{1,2}|地下[0-9]{1,2}樓)(?:之[0-9]{1,3})?)?$"
)
ADDRESS_HINT_RE = re.compile(
    r"[一-鿿]{1,8}" + ROAD_SUFFIX + r"(?:[0-9]{1,2}段)?(?:[0-9]{1,4}巷)?(?:[0-9]{1,4}弄)?"
    r"[0-9]{1,5}(?:之[0-9]{1,4})?號"
)
# 路名前面緊接著這些字時，前面沒寫行政區也算（「位於重慶南路…」「在重慶南路…」）。
_ROAD_LEAD_PARTICLES = ("在", "於", "址", "是", "到", "近", "往", "從", "至")


def address_text(raw: str) -> str:
    """比對地址用的文字：nfkc、去空白、段巷弄號樓鄰前的中文數字轉阿拉伯數字、「-」當成「之」。"""
    text = re.sub(r"\s+", "", nfkc(raw))
    for unit in ("段", "巷", "弄", "號", "樓", "鄰"):
        text = _cn_digits(text, unit)
    return re.sub(r"([0-9])-([0-9])", r"\1之\2", text)


def _tail(match: re.Match) -> str:
    return "".join(match.group(key) or "" for key in ("section", "lane", "alley", "number"))


_VILLAGE_PREFIX_RE = re.compile(r"^[一-鿿]{1,3}[里村](?P<road>[一-鿿]{2,}" + ROAD_SUFFIX + ")$")


def _road(road: str) -> str:
    """路名前面黏著里名時拿掉：「光復里重慶南路」→「重慶南路」。

    剩下的路名至少 3 個字才拿（「大里東路」剩「東路」，那是路名本身，不是大里里）。
    """
    match = _VILLAGE_PREFIX_RE.match(road)
    return match.group("road") if match and len(match.group("road")) >= 3 else road


def parse_address(raw: str) -> str | None:
    """嚴格解析一個完整地址，回正規化字串（縣市＋區＋路段巷弄號）；拆不全回 None。

    「3号」（簡體）、「5鄰12號」（沒有路名）、少了縣市或區的都不收。
    """
    if not raw or has_hidden_chars(raw) or "\n" in raw:
        return None
    match = _FULL_ADDRESS_RE.match(address_text(raw))
    if not match:
        return None
    return match.group("city") + match.group("district") + _road(match.group("road")) + _tail(match)


def address_parts(full: str) -> tuple[str, str, str, str] | None:
    """parse_address 產生的字串拆回 (縣市, 區, 路, 段巷弄號)。"""
    match = _FULL_ADDRESS_RE.match(full or "")
    if not match:
        return None
    return match.group("city"), match.group("district"), match.group("road"), _tail(match)


def region(full: str) -> str:
    """縣市＋區：同名店在不同區要分開存（審查員 3：台北和台南的甲麵館會互相覆蓋）。"""
    parts = address_parts(full)
    return parts[0] + parts[1] if parts else ""


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


class AddressSpan(NamedTuple):
    start: int      # 郵遞區號、縣市、區或路名的開頭（有寫到的最前面那個）
    end: int        # 號的後面；接著樓層時到樓層後面
    district: bool  # 有寫到區
    city: bool      # 縣市和區都寫到了（縣市緊接在區前面）


# 縣市或區前面的郵遞區號（3、5、6 碼，可以包在一對括號裡）；沒有括號時，前面緊接數字、「之」「-」的
# 是電話或門牌的尾巴，不算。
_POSTAL = r"[0-9]{3}(?:[0-9]{2,3})?"
_POSTAL_RE = re.compile(rf"(?:(?<![0-9之-]){_POSTAL}|\({_POSTAL}\)|\[{_POSTAL}\])$")
# 號後面的樓層（address_text 後的寫法；網頁文字已轉小寫，B1、1F 也收）。
_FLOOR_RE = re.compile(r"(?:[0-9]{1,3}樓|[bB][0-9]{1,2}|地下[0-9]{1,2}樓|[0-9]{1,3}[fF])(?:之[0-9]{1,3})?")


def address_spans(text: str, full: str) -> list[AddressSpan]:
    """在 address_text(text) 裡，這個完整地址出現的位置。

    直接找「路＋段巷弄號」，再看前面（先拆網頁上的地址會被前面黏著的店名拆錯）：
    - 緊接著是這個區 → 算；區前面若還有漢字，必須是這個縣市或「在、於」這類字
      （「安南區」不算「南區」）；
    - 緊接著是這個縣市，或「在、於」這類字，或不是漢字 → 算，但沒有寫到區；
    - 其他漢字（別的區、別的縣市、里名、「新中山路」的「新」）→ 不算。
    號碼後面接著「之N」的是另一個門牌，不算；接著樓層或電話照算。
    起點往前包含緊接著的縣市、郵遞區號，終點包含樓層：店名和地址、地址和電話之間才不會剩下這些字。
    ``city`` 要縣市和區都寫到（最終版審查員 1：只寫「中正區」的頁，台北、基隆的中正區都對得上）。
    """
    parts = address_parts(full)
    if parts is None:
        return []
    city, district, road, tail = parts
    norm = address_text(text)
    needle = road + tail
    spans: list[AddressSpan] = []
    start = norm.find(needle)
    while start != -1:
        end = start + len(needle)
        before = norm[:start]
        ok = not re.match(r"之[0-9]", norm[end:end + 2])
        with_district = False
        if ok and before.endswith(district):
            pre = before[: len(before) - len(district)]
            ok = not pre or not _is_cjk(pre[-1]) or pre.endswith(city) or pre.endswith(_ROAD_LEAD_PARTICLES)
            with_district = ok
        elif ok and before and _is_cjk(before[-1]):
            ok = before.endswith(city) or before.endswith(_ROAD_LEAD_PARTICLES)
        if ok:
            first = start - len(district) if with_district else start
            with_city = norm[:first].endswith(city)
            if with_city:
                first -= len(city)
            if with_city or with_district:
                code = _POSTAL_RE.search(norm, 0, first)
                first = code.start() if code else first
            floor = _FLOOR_RE.match(norm, end)
            spans.append(AddressSpan(first, floor.end() if floor else end, with_district, with_city and with_district))
        start = norm.find(needle, start + 1)
    return spans



# ── 店名 ──────────────────────────────────────────────────────────────────────

KINDS = ("店家", "餐廳", "郵局", "機關")
OFFICE_KINDS = ("郵局", "機關")
# 第一批不收：醫療院所放第 2 批（和 /健康 的同意、暫停規則一起做），銀行放第 3 批。掃描和管理指令共用。
MEDICAL_RE = re.compile(
    r"診所|醫院|醫學中心|醫療|牙醫|醫師|醫生|看診|掛號|門診|藥局|藥房|藥師|中醫|復健|檢驗所|衛生所|"
    r"耳鼻喉|眼科|皮膚科|牙科|小兒科|婦產科|骨科|內科|外科|家醫科|身心科|精神科|泌尿科|復健科|醫美"
)
BANK_RE = re.compile(r"銀行|農會|漁會|信用合作社|信合社|證券|保險|郵政儲金|ATM", re.I)
# 和 fact_filters.LINK_RE、KINSHIP_TERMS 同一套（不 import fact_filters：它會帶進 reply_policy 等模組）。
LINK_RE = re.compile(
    r"https?://\S+|www\.\S+|(?<![A-Za-z0-9-])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?:/[!-~]*)?",
    re.IGNORECASE,
)
KINSHIP_TERMS = (
    "媽媽", "爸爸", "姊姊", "姐姐", "妹妹", "弟弟", "哥哥", "阿婆", "外婆", "奶奶", "阿嬤", "爺爺",
    "阿公", "外公", "兒子", "女兒", "老公", "老婆", "舅舅", "阿姨", "姑姑", "叔叔", "伯伯", "婆婆",
    "公公", "岳母", "媳婦", "女婿", "父親", "母親", "孫子", "孫女",
    "我媽", "我爸", "我哥", "我姊", "我姐", "我妹", "我弟",
)
# 常見連鎖品牌：名稱以它開頭又沒寫分店就不收（審查員 3：「鼎泰豐電話？」會回到某一家分店）。
CHAIN_BRANDS = (
    "7-11", "711", "7-eleven", "統一超商", "全家", "familymart", "萊爾富", "hi-life", "ok超商",
    "ok便利", "全聯", "家樂福", "carrefour", "大潤發", "好市多", "costco", "美廉社", "屈臣氏", "康是美",
    "寶雅", "星巴克", "starbucks", "路易莎", "louisa", "85度c", "麥當勞", "mcdonald", "肯德基", "kfc",
    "摩斯", "mos burger", "鼎泰豐", "爭鮮", "八方雲集", "四海遊龍", "50嵐", "五十嵐", "清心福全", "coco都可",
    "迷客夏", "必勝客", "pizzahut", "達美樂", "domino", "拿坡里", "丹丹", "麥味登", "美而美", "拉亞",
    "弘爺", "ikea", "特力屋", "燦坤", "全國電子", "大創", "daiso", "無印良品", "muji", "uniqlo", "誠品",
    "金石堂", "王品", "瓦城", "饗食天堂", "漢堡王", "subway",
)
# 名字剛好以品牌開頭、其實不是連鎖店的，明列在這裡（最終版審查員 1：不能用「後面接漢字就放過」，
# 那樣「全聯福利中心」「王品牛排」都不用寫分店）。
_NOT_CHAINS = ("全家福",)
# 品牌後面的類別字：先去掉（可以連著好幾個），剩下的還要是「某某店／門市／分店／分館／館」才算寫了分店。
# 「50嵐飲料店」去掉類別字後什麼都不剩；「分店」「專賣店」「旗艦店」本身也不是哪一家分店。
_CHAIN_CATEGORIES = tuple(sorted((
    "便利商店", "便利店", "超商", "超市", "福利中心", "量販店", "量販", "賣場", "百貨", "生活", "藥妝", "美妝",
    "咖啡", "coffee", "漢堡", "速食", "炸雞", "早餐", "餐廳", "麵包", "迴轉壽司", "壽司", "牛排", "鍋貼", "水餃",
    "披薩", "比薩", "飲料店", "飲料", "茶飲", "手搖飲", "書店", "泰式料理", "泰國料理", "料理",
    "專賣店", "旗艦", "分店", "門市", "商店",
), key=len, reverse=True))
_BRANCH_RE = re.compile(r".+(?:店|門市|分店|分館|館)$")
OFFICE_RE = re.compile(
    r".+(?:郵局|區公所|鄉公所|鎮公所|市公所|戶政事務所|地政事務所|派出所|分駐所|分局|圖書館|運動中心|"
    r"國稅局|稽徵所|稅捐稽徵處|監理所|監理站|服務處|分處)$"
)
_NAME_BAD_RE = re.compile(r"https?://|www\.|[\[\]<>{}「」【】〔〕《》〈〉『』\n\r\t]|[0-9]{5,}")
# 括號只准一組、而且放在最後：「鼎泰豐(信義店)」（審查員 3：「鼎泰豐(總)店」「(甲麵館)」只會永遠核對不到）
_ONE_BRACKET_RE = re.compile(r"[^()]+(?:\([^()]+\))?")


def name_key(name: str) -> str:
    # 「鼎泰豐(信義店)」和「鼎泰豐信義店」是同一家：兩輪寫法不同也要對得上
    return compact(name).replace("(", "").replace(")", "")


def _leading_categories(text: str) -> str:
    """開頭連著的類別字（可以好幾個）。"""
    lead = ""
    while True:
        category = next((c for c in _CHAIN_CATEGORIES if text.startswith(c, len(lead))), "")
        if not category:
            return lead
        lead += category


_BRACKETED_RE = re.compile(r"\(([^()]+)\)")


def _branch_of(rest: str) -> tuple[str, str] | None:
    """連鎖品牌後面的字拆成（類別字, 分店）；沒寫分店回 None。

    分店外面可以有一對括號（「鼎泰豐（信義店）」，拆的時候拿掉）。分店去掉開頭的類別字以後，要剩下
    「某某店／門市／分店／分館／館」（「50嵐飲料店」「50嵐（飲料店）」都沒寫分店）。
    """
    lead = _leading_categories(rest)
    branch = rest[len(lead):]
    inner = _BRACKETED_RE.fullmatch(branch)
    branch = inner.group(1) if inner else branch
    return (lead, branch) if _BRANCH_RE.match(branch[len(_leading_categories(branch)):]) else None


def _chain_brands(key: str, spaced: str):
    """店名開頭的連鎖品牌（去掉空白的寫法）。``spaced`` 是 nfkc、小寫、空白縮成一個的店名。"""
    for brand in CHAIN_BRANDS:
        if re.fullmatch(r"[a-z0-9 \-]+", brand) and brand[-1].isalpha():
            # 英文品牌：保留空白的寫法要以品牌開頭、後面不緊接英文字母
            # （審查員 4：「Seven Seas Bistro」「Mosaic Cafe」不是連鎖店）
            if not re.match(re.escape(brand) + r"(?![a-z])", spaced):
                continue
        elif not key.startswith(brand.replace(" ", "")):
            continue
        brand = brand.replace(" ", "")
        if not key.startswith(tuple(other for other in _NOT_CHAINS if other.startswith(brand))):
            yield brand  # 「全家福海鮮餐廳」不是全家


def validate_name(raw: str, kind: str) -> tuple[str | None, str]:
    """(顯示用店名, 拒收原因)。顯示用店名是 nfkc 後去頭尾空白的寫法。"""
    if not raw or has_hidden_chars(raw):
        return None, "店名有看不見的字元"
    name = re.sub(r"\s+", " ", nfkc(raw)).strip()
    if not 2 <= len(name) <= 40:
        return None, "店名長度不合理"
    if _NAME_BAD_RE.search(name):
        return None, "店名裡有網址、括號或長數字"
    if not _ONE_BRACKET_RE.fullmatch(name):
        return None, "店名的括號要成對、最多一組，而且放在最後"
    key = name_key(name)
    looks_official = OFFICE_RE.match(key) is not None
    if kind in OFFICE_KINDS:
        if not looks_official:
            return None, "機關或郵局要寫完整名稱（例如某某郵局、某某區公所）"
        return name, ""
    if looks_official:
        return None, "看起來是機關或郵局，種類要用「郵局」或「機關」"
    brands = list(_chain_brands(key, re.sub(r"\s+", " ", nfkc(name)).lower()))
    for brand in brands:
        if _branch_of(key[len(brand):]) is None:
            return None, "連鎖店要寫分店名稱"
    if "(" in name and not brands:
        return None, "括號只給連鎖店的分店用（例如 鼎泰豐(信義店)）；其他店名不要加括號"
    return name, ""


# ── 網域 ──────────────────────────────────────────────────────────────────────

_SHORTENERS = frozenset({
    "bit.ly", "reurl.cc", "tinyurl.com", "goo.gl", "ppt.cc", "pse.is", "t.co", "lihi.cc", "lihi1.com",
    "is.gd", "ow.ly", "s.id", "shorturl.at", "tiny.cc", "rebrand.ly", "cutt.ly", "buff.ly", "maps.app.goo.gl",
    "g.co", "lin.ee", "fb.me", "shrturl.app", "pros.is", "instagr.am", "youtu.be", "amzn.to", "lnkd.in",
})
_CACHES = frozenset({
    "webcache.googleusercontent.com", "archive.org", "web.archive.org", "archive.ph", "archive.today",
    "archive.is", "archive.li", "archive.vn", "archive.md", "cachedview.nl", "r.jina.ai", "12ft.io",
})
_CACHE_SUFFIXES = (".translate.goog", ".cdn.ampproject.org", ".ampproject.org")
_SEARCH_HOSTS = ("bing.com", "duckduckgo.com", "search.yahoo.com", "baidu.com")
# 同一個來源的不同網域（審查員 3 C4）。
_ALIASES = (
    (re.compile(r"(?:^|\.)google\.[a-z.]+$|(?:^|\.)g\.page$"), "google"),
    (re.compile(r"(?:^|\.)(?:facebook\.com|fb\.com|fb\.watch)$"), "facebook"),
    (re.compile(r"(?:^|\.)instagram\.com$"), "instagram"),
    (re.compile(r"(?:^|\.)(?:line\.me|line-apps\.com)$"), "line"),
    (re.compile(r"(?:^|\.)youtube\.com$"), "youtube"),
    (re.compile(r"(?:^|\.)(?:twitter\.com|x\.com)$"), "x"),
)
_TW_SECOND = frozenset({"com", "org", "net", "gov", "edu", "idv", "mil", "club", "ebiz", "game"})
_GENERIC_SECOND = frozenset({"co", "com", "org", "net", "gov", "edu", "ac", "or", "ne", "go"})
GOV_SUFFIXES = (".gov.tw", ".gov.taipei")


def source_site(url: str) -> tuple[str | None, str, str]:
    """(網站代號, 主機名稱, 拒收原因)。網站代號用來算「幾個不同網站」：同一品牌的不同國別網域算一個。"""
    try:
        parts = urlsplit((url or "").strip())
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return None, "", "網址格式不對"
    if parts.scheme not in ("http", "https") or not host:
        return None, "", "只收 http(s) 網址"
    if parts.username or parts.password:
        return None, host, "網址不能帶帳號密碼"
    if host.startswith("xn--") or ".xn--" in host:
        return None, host, "不收 punycode 網域"
    if re.fullmatch(r"[0-9.]+|\[?[0-9a-f:]+\]?", host):
        return None, host, "不收 IP 位址"
    bare = re.sub(r"^(?:www|m|mobile|amp)\.", "", host)
    if bare in _SHORTENERS or host in _SHORTENERS:
        return None, host, "不收短網址"
    if bare in _CACHES or host in _CACHES or host.endswith(_CACHE_SUFFIXES):
        return None, host, "不收網頁快取、代理或翻譯頁"
    path = (parts.path or "").lower()
    is_google = re.search(r"(?:^|\.)google\.[a-z.]+$", bare) is not None
    if any(bare == h or bare.endswith("." + h) for h in _SEARCH_HOSTS) or (
        is_google and (path.startswith(("/search", "/url")) or path.startswith("/maps/search") or "q=" in (parts.query or ""))
    ):
        return None, host, "不收搜尋結果頁"
    labels = bare.split(".")
    if host.endswith(GOV_SUFFIXES):
        return "gov:" + ".".join(labels[-3:]), host, ""
    for pattern, canonical in _ALIASES:
        if pattern.search(bare):
            return canonical, host, ""
    if len(labels) >= 3 and labels[-1] == "tw" and labels[-2] in _TW_SECOND:
        return labels[-3], host, ""
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _GENERIC_SECOND:
        return labels[-3], host, ""
    return (labels[-2] if len(labels) >= 2 else labels[0]), host, ""


def is_gov_host(host: str | None) -> bool:
    host = (host or "").lower().rstrip(".")
    return host in ("gov.tw", "gov.taipei") or host.endswith(GOV_SUFFIXES)


# ── 網頁 ──────────────────────────────────────────────────────────────────────

_BLOCK_TAGS = frozenset({
    "p", "div", "li", "tr", "td", "th", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section",
    "article", "header", "footer", "address", "dd", "dt", "table", "ul", "ol", "blockquote", "hr", "pre",
})
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe"})
# 反詐騙公告會同時寫出被冒用的店名和詐騙號碼（審查員 3）；只看店名那一段，頁尾的防詐提醒不影響整頁。
ANTI_FRAUD_RE = re.compile(r"詐騙|假冒|冒用|冒名|反詐|詐團|165專線")
CLOSED_RE = re.compile(r"歇業|停業|結束營業|已關門|收攤|頂讓")
# 店名後面到要核對的地址（電話）前面，只准有標點、符號和這些連接字：地址、電話的標籤，加上「位於、在」
# 這類字；不收說明文字。其他字（別家店名、「二店」「延吉」、英文、數字、營業時間、評分）都算沒綁在一起
# （最終版審查員 1、3：「甲味小館 星巴克 地址…」把星巴克的地址電話綁給甲味小館）。網頁文字已轉小寫。
# 傳真、FAX、客服、總機刻意不收：傳真號碼不能當成電話，客服、總機可能是總公司的號碼。
# 「這家店」「這間店」指的是店名本身（審查員 4 #3 的泛稱）。
_ADDRESS_LABELS = r"(?:店家|門市|營業)?(?:地址|地點|位置)|店址|住址|address|addr"
_PHONE_LABELS = r"(?:(?:聯絡|連絡|訂位|預約|店家|門市)?電話|手機|市話)(?:號碼)?|(?:訂位|預約)?專線|telephone|phone|tel"
_PLACE_WORDS = r"坐落於|座落於|位於|位在|就在|在|這家店|這間店|的|是"
_CONNECTOR_RE = re.compile("|".join((_ADDRESS_LABELS, _PHONE_LABELS, _PLACE_WORDS)))
BIND_GAP = 40
_HEADING_EXTRA = 12
_FRAUD_AFTER = 20  # 電話後面這幾個字內有「詐騙」也不算（「…02-xxx 是詐騙電話」）


class _Blocks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._buf: list[str] = []
        self._skip = 0

    def flush(self) -> None:
        # 一個 div 裡用換行分隔的文章（PTT 那種），每一行也是一段（審查員 3）。
        for line in "".join(self._buf).splitlines():
            text = re.sub(r"[ \t　\xa0]+", " ", line).strip()
            if text:
                self.blocks.append(text)
        self._buf = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.flush()

    def handle_endtag(self, tag) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.flush()

    def handle_data(self, data) -> None:
        if not self._skip:
            self._buf.append(data)


def page_blocks(body: str, content_type: str = "text/html") -> list[str]:
    """網頁轉成段落文字；純文字頁用換行切。"""
    if "html" not in (content_type or "text/html"):
        return [line.strip() for line in html.unescape(body or "").splitlines() if line.strip()]
    parser = _Blocks()
    try:
        parser.feed(body or "")
        parser.close()
    except Exception:  # noqa: BLE001 - 壞掉的 HTML 就當成沒有內容
        return []
    parser.flush()
    return parser.blocks


def _address_norm(text: str) -> str:
    return address_text(text).lower()


def _alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


@functools.lru_cache(maxsize=256)
def _name_forms(name: str) -> tuple[tuple[str, frozenset], ...]:
    """店名在網頁上的寫法（去掉空白）和每種寫法裡可以有空白的地方（那個字的索引）。

    一般店名只有一種寫法，可以有空白的地方就是店名本身有空白的地方。連鎖店的品牌和分店之間可以
    什麼都沒有、空白或一對括號：「鼎泰豐信義店」「鼎泰豐 信義店」「鼎泰豐（信義店）」都算，
    店名寫成哪一種都一樣（第二輪審查）。
    """
    spaced = nfkc(name).lower()
    key = re.sub(r"\s+", "", spaced)
    own = [m.start() for m in re.finditer(r"\S", spaced)]
    breaks = frozenset(i for i in range(1, len(own)) if own[i] - own[i - 1] > 1)
    brand = next(_chain_brands(key, re.sub(r"\s+", " ", spaced).strip()), "")
    parts = _branch_of(key[len(brand):]) if brand else None
    if not parts:
        return ((key, breaks),)
    head, branch = brand + parts[0], parts[1]
    h, b = len(head), len(branch)
    start = h + 1 if key == f"{head}({branch})" else h  # 店名本身寫的分店從哪裡開始
    outer = {i for i in breaks if i < h}
    inner = {i - start for i in breaks if start < i < start + b}
    return (
        (head + branch, frozenset(outer | {h} | {h + j for j in inner})),
        (f"{head}({branch})", frozenset(outer | {h, h + 1, h + 1 + b} | {h + 1 + j for j in inner})),
    )


def name_spans(text: str, name: str) -> list[tuple[int, int]]:
    """店名在 text（nfkc、小寫，空白還在）裡的位置。店名裡的空白不管（「甲 麵 館」也找得到）。

    前面要有邊界：開頭、空白、標點都算；緊接漢字或英數字的不算（「大甲麵館」不是「甲麵館」）。
    空白不算邊界、要看空白前面那個字的情況：網頁把店名逐字隔開寫（「大 甲 麵 館」）；英數字開頭的店名
    前面是英文字（英文店名本來就用空白隔字，「Big Mosaic Cafe」不是「Mosaic Cafe」）。
    店名以英數字結尾時，後面緊接英數字也不算（「Mosaic Cafeteria」不是「Mosaic Cafe」）。
    連鎖店的品牌和分店之間可以有空白或一對括號（見 _name_forms），那裡的空白不算逐字隔開。
    ``name`` 傳 compact 過的店名也可以（就是沒有空白的店名）。
    """
    squeezed = re.sub(r"\s+", "", text)
    forms = [(key, allowed) for key, allowed in _name_forms(name) if key and key in squeezed]
    if not forms:
        return []
    where = [m.start() for m in re.finditer(r"\S", text)]  # squeezed 每個字在 text 的位置
    spans = []
    for key, allowed in forms:
        start = squeezed.find(key)
        while start != -1:
            first, last = where[start], where[start + len(key) - 1] + 1
            breaks = {i for i in range(1, len(key)) if where[start + i] - where[start + i - 1] > 1}
            before = text[:first]
            tight = before.rstrip()
            if breaks - allowed or (_alnum(key[0]) and _alnum(tight[-1:])):
                before = tight
            glued_left = bool(before) and (_is_cjk(before[-1]) or _alnum(before[-1]))
            glued_right = _alnum(key[-1]) and _alnum(text[last:last + 1])
            if not glued_left and not glued_right:
                spans.append((first, last))
            start = squeezed.find(key, start + 1)
    return sorted(spans)


def _only_connectors(gap: str) -> bool:
    """去掉連接字以後只剩標點、符號（表情符號的連接字元也算）。"""
    rest = _CONNECTOR_RE.sub(_SEP, gap)
    return all(unicodedata.category(ch)[0] in "PSZ" or ch in _EMOJI_JOINERS for ch in rest)


_DIGIT_JOIN_RE = re.compile(r"([0-9])之(?=[0-9])")
_TABLE_FOLLOW = 2  # 店名那段很短（標題、表格第一格）時，最多接後面兩段（地址、電話各一格）
_SEP = "|"  # 段與段之間、被換掉的自家地址電話：不是漢字、不是數字，別家店名和號碼都不會跨過去接起來


def _bound(gap: str, *, address: str, phone: str | None) -> bool:
    """店名和要核對的地址（或電話）之間的文字 ``gap`` 能不能算「綁在一起」。

    這家自己的地址和電話先換成分隔符號（「電話：…地址：…」兩種順序都算），剩下的要夠短、
    沒有歇業字樣、別的地址或號碼，而且只剩標點和連接字（_CONNECTORS）。address_text 會把
    「02-9999-8888」的「-」改成「之」，查號碼前先換回來（審查員 4）。
    """
    text = _address_norm(gap)
    for span in sorted(address_spans(text, address), reverse=True):
        text = text[:span.start] + _SEP + text[span.end:]
    text = _DIGIT_JOIN_RE.sub(r"\1-", text)
    if phone:
        for start, end, digits in sorted(find_phones(text), reverse=True):
            if digits == phone:
                text = text[:start] + _SEP + text[end:]
    if len(text) > BIND_GAP or CLOSED_RE.search(text) or ADDRESS_HINT_RE.search(text) or _DIGIT_RUN_RE.search(text):
        return False
    return _only_connectors(text)


class _Occurrence(NamedTuple):
    prefix: str  # 店名所在那段裡、店名前面的文字
    after: str   # 店名後面的文字；店名那段很短時接上後面最多兩段


def _occurrences(blocks: list[str], name: str, norm) -> list[_Occurrence]:
    """店名每次出現的前後文。店名在還留著空白的文字裡找（空白算邊界，見 name_spans），
    前後文再用 ``norm`` 正規化（地址用 address_text，電話用 compact）。

    店名和地址電話之間只准有連接字（_bound），所以「甲麵館｜乙麵館 地址…」的乙地址不會綁到甲。
    """
    heading = len(norm(name)) + _HEADING_EXTRA
    found = []
    for index, block in enumerate(blocks):
        text = nfkc(block).lower()
        for start, end in name_spans(text, name):
            after = norm(text[end:])
            if len(norm(block)) <= heading:
                for follow in blocks[index + 1:index + 1 + _TABLE_FOLLOW]:
                    after += _SEP + norm(follow)
            found.append(_Occurrence(norm(text[:start]), after))
    return found


class Support(NamedTuple):
    mentioned: bool     # 有提到這家店（店名前後有邊界）
    address: bool       # 地址綁在店名上
    address_full: bool  # 綁上的地址那段有寫到區
    address_city: bool  # 綁上的地址那段縣市和區都寫到了
    phone: bool         # 電話綁在店名上


def page_supports(blocks: list[str], name: str, *, address: str, phone: str | None) -> Support:
    """一個網頁對這家店的支持程度。

    「綁在店名上」見 _bound：店名之後 40 個字內，中間只准有標點和連接字（別家店名、歇業字樣、
    別的地址或號碼都不行）。店名那段（店名前後）有歇業字樣就整段不算；有反詐騙字樣時電話不算
    （審查員 4：「注意詐騙！甲麵館…來電…」）；頁尾的防詐提醒在別段，不影響。
    ``address_city``：中正、信義、中山區台北和基隆都有，只寫區的頁核對不了縣市（最終版審查員 1）。
    """
    mentioned = address_ok = address_full = address_city = phone_ok = False
    for occ in _occurrences(blocks, name, _address_norm):
        mentioned = True
        if CLOSED_RE.search(occ.prefix) or CLOSED_RE.search(occ.after[:BIND_GAP]):
            continue
        for span in address_spans(occ.after, address):
            if _bound(occ.after[:span.start], address=address, phone=phone):
                address_ok = True
                address_full = address_full or span.district
                address_city = address_city or span.city
    if phone:
        for occ in _occurrences(blocks, name, compact):
            if CLOSED_RE.search(occ.prefix) or ANTI_FRAUD_RE.search(occ.prefix) or CLOSED_RE.search(occ.after[:BIND_GAP]):
                continue
            for start, end, digits in find_phones(occ.after):
                window = occ.after[:end] + occ.after[end:end + _FRAUD_AFTER].split(_SEP, 1)[0]
                if digits == phone and not ANTI_FRAUD_RE.search(window) and _bound(
                    occ.after[:start], address=address, phone=phone
                ):
                    phone_ok = True
                    break
            if phone_ok:
                break
    return Support(mentioned, address_ok, address_full, address_city, phone_ok)
