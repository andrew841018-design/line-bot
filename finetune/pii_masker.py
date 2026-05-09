"""PII masker — 100% local, used to scrub sensitive entities from training data
before it leaves the laptop (e.g. cloud finetune dataset upload).

Detects 6 categories:
  - NAME    : Chinese person names (jieba.posseg `nr*`) + relationship prefix
              forms (黃媽媽 / 王爸爸 / 李叔叔 …) commonly seen in family chats
  - AMOUNT  : numeric currency amounts (含 萬/億/元/塊/美元/USD …)
  - ADDRESS : Taiwan addresses (含 路/街/巷/弄/號/樓 + 22 縣市)
  - PHONE   : 09xxxxxxxx / 0x-xxxx-xxxx / 4-3-3 dashed / 1955 etc.
  - ACCOUNT : 16-digit credit card / dashed CC / generic 10–16 digit account
  - ID      : Taiwan national ID (1 letter + 9 digits) + email + URLs (含個人資訊)
              [ID is for "identifier-style PII"; ORG / EMAIL / URL get bucketed
              into their own placeholder name to keep mappings legible]
  - ORG     : Companies (含 「股份有限公司 / 銀行 / 保險 / 金控」等 suffix
              + jieba.posseg `nt*`)

Preserved on purpose:
  - 關係詞: 老婆 / 媽媽 / 爸爸 / 兒子 / 女兒 / 朋友 / 哥哥 / 姊姊 …
  - 百分比 (利率討論需要)
  - 純年月日 (eg 2024.07.10 / 2026/05/08 / 5月10日)
  - 一般詞 / 標點 / emoji

Public API:
  mask(text, mapping=None, strict=False) -> (masked_text, mapping)
    mapping: {"[NAME_1]": "黃媽媽", ...}  — 同名同編號（內 + 跨呼叫）

  unmask(masked_text, mapping) -> str       # round-trip
  count_pii(mapping) -> dict[str, int]      # {"NAME": n, "AMOUNT": n, …}
"""
from __future__ import annotations

import logging
import re
import warnings
from typing import Any

# jieba 在 Python 3.13 會跳 pkg_resources DeprecationWarning，靜音
warnings.filterwarnings("ignore", message=".*pkg_resources.*")

import jieba  # noqa: E402
import jieba.posseg as pseg  # noqa: E402

jieba.setLogLevel(60)  # 靜音第一次 build dict log

logger = logging.getLogger("pii_masker")


# ─────────────────────────────────────────────────────────────────────────────
# Regex catalogue
# ─────────────────────────────────────────────────────────────────────────────
# 順序 (先處理, 最 specific) → (後處理, 最 generic)，避免 overlap。
# 每個 regex group 抓「整段要 mask 的 token」。

# Email — RFC 5322 simplified
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)

# URL — http(s) / www
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)

# Taiwan national ID: 1 uppercase letter + 9 digits
# (no \b — CJK doesn't fire ASCII word boundary; use lookarounds instead)
_ID_RE = re.compile(r"(?<![A-Z0-9])[A-Z][12]\d{8}(?!\d)")

# Credit card / bank account
#   - 16 連續 digits  OR  4-4-4-4 dashed/spaced groups
#   - generic 10–15 純數字 (e.g. bank account)
# Use (?<!\d)/(?!\d) instead of \b so it works inside CJK strings.
_ACCOUNT_DASH_RE = re.compile(
    r"(?<!\d)\d{4}[-\s]\d{4}[-\s]\d{4}[-\s]\d{4}(?!\d)"
)
_ACCOUNT_16_RE = re.compile(r"(?<!\d)\d{16}(?!\d)")
_ACCOUNT_GENERIC_RE = re.compile(r"(?<!\d)\d{10,15}(?!\d)")  # 10–15 digit

# Phone numbers (TW conventions)
#   - 09xxxxxxxx (mobile, 10 digits)
#   - 09xx-xxx-xxx
#   - (0x) xxxx-xxxx / 0x-xxxx-xxxx (landline)
#   - +886-... international
#   - 4-3-3 dashed phone (xxxx-xxx-xxx)
#   - 短碼 165 / 1955 / 1922 (let user-defined extend later)
_PHONE_RE = re.compile(
    r"(?:"
    r"\+?886[-\s]?\d{1,2}[-\s]?\d{3,4}[-\s]?\d{3,4}"   # +886 / 886
    r"|0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{3,4}"              # 0X-xxxx-xxxx (landline / mobile)
    r"|09\d{8}"                                         # 09xxxxxxxx
    r"|\d{4}[-\s]\d{3}[-\s]\d{3}"                       # xxxx-xxx-xxx
    r"|\d{3}[-\s]\d{3}[-\s]\d{4}"                       # xxx-xxx-xxxx (US-style)
    r")"
)

# Amount — 數字 + 中/英文幣別後綴
# 注意: 5000元 / 5萬 / 1.5億 / 100 USD / 200 美元 / 50 塊 / NT$1000 / $300
#       50萬元 (compound) / 1000 美元 (space-separated)
# 不抓: 純百分比 / 純年月日 / 純小數
_CCY_SUFFIX = (
    r"(?:萬元|億元|萬|億|千|百|元|塊|美元|台幣|新台幣|日元|港幣|"
    r"USD|US\$|NTD|JPY)"
)
_AMOUNT_RE = re.compile(
    # NT$1000 / $300 / US$1.5
    r"(?:NT\$|US\$|\$)\d+(?:,\d{3})*(?:\.\d+)?"
    # 1,000元 / 1,234,567.89 元 (with optional comma + optional space + suffix)
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*" + _CCY_SUFFIX + r"?"
    # 50萬元 / 1000 美元 / 100USD
    r"|\d+(?:\.\d+)?\s*" + _CCY_SUFFIX
)

# Taiwan addresses — must contain at least one of: 路/街/巷/弄/號/樓/段/區
# Anchor on a city/county prefix OR address keywords (looser fallback)
_TW_CITIES = (
    "台北市", "臺北市", "新北市", "桃園市", "台中市", "臺中市", "台南市",
    "臺南市", "高雄市", "基隆市", "新竹市", "嘉義市", "新竹縣", "苗栗縣",
    "彰化縣", "南投縣", "雲林縣", "嘉義縣", "屏東縣", "宜蘭縣", "花蓮縣",
    "台東縣", "臺東縣", "澎湖縣", "金門縣", "連江縣",
    "台北", "新北", "桃園", "台中", "台南", "高雄", "基隆", "新竹",
    "嘉義", "苗栗", "彰化", "南投", "雲林", "屏東", "宜蘭", "花蓮", "台東",
    "澎湖", "金門",
)
_CITY_ALT = "(?:" + "|".join(_TW_CITIES) + ")"
# 地址 = 縣市 + 區域? + 路名 + 段? + 巷弄? + 號 + 樓? — 寬鬆
# 也接受沒有縣市但有「X路Y號」「X街Y巷Z號」的本機形式
# 用非貪婪量詞 + 限制中文字數 (避免吞前綴姓名)
_ADDR_FULL_RE = re.compile(
    rf"{_CITY_ALT}[一-鿿]{{0,8}}?(?:[路街道](?:[一二三四五六七八九十\d]+段)?)"
    r"[一-鿿]{0,4}?\d+[巷弄號]"
    r"(?:[-之]\d+)?"
    r"(?:\d+[樓F號])?"
)
_ADDR_LOCAL_RE = re.compile(
    r"(?:[一-鿿]{2,5}(?:路|街|大道))"
    r"(?:[一二三四五六七八九十\d]+段)?"
    r"(?:\d+[巷弄])?"
    r"\d+[號]"
    r"(?:[-之]\d+)?"
    r"(?:\d+[樓F])?"
)

# Date (preserved — used to detect, exclude amount overlap)
# Use (?<!\d)/(?!\d) so dates inside CJK contexts (e.g. 「於2024/07/10發生」) match.
_DATE_RE = re.compile(
    r"(?<!\d)(?:"
    r"\d{4}[/.\-]\d{1,2}[/.\-]\d{1,2}"        # 2024/07/10
    r"|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"     # 07/10/2024
    r"|\d{1,2}月\d{1,2}日"                     # 5月10日
    r"|\d{1,2}:\d{2}(?::\d{2})?"               # 18:02
    r")(?!\d)"
)

# Org-by-suffix — 公司 / 銀行 / 保險 / 金控 / 證券 / 投信 / 工業 / 科技 …
# anchor: 至少 2 個中文字 + 後綴
_ORG_SUFFIX = (
    "股份有限公司", "有限公司", "股份公司", "公司", "銀行", "保險",
    "人壽", "金控", "證券", "投信", "投顧", "工業", "科技", "電子",
    "建設", "實業", "集團", "股份", "商行", "三信",
)
_ORG_ALT = "(?:" + "|".join(_ORG_SUFFIX) + ")"
_ORG_RE = re.compile(rf"[一-鿿]{{2,}}{_ORG_ALT}")

# Person-by-relationship-prefix
# 黃媽媽 / 王爸爸 / 李叔叔 / 張阿姨 …
# 注意: 單獨「媽媽 / 爸爸」preserve；只要前面有姓氏 (1 字 + 關係詞) 才 mask
_REL_WORDS = ("媽媽", "爸爸", "叔叔", "阿姨", "舅舅", "舅媽", "姑姑",
              "姑丈", "姨丈", "姨媽", "嬸嬸", "伯父", "伯母",
              "爺爺", "奶奶", "嫂子", "嫂嫂", "姊夫", "妹夫")
_REL_ALT = "(?:" + "|".join(_REL_WORDS) + ")"
# 姓 = 常見百家姓 (避免「說媽媽」「跟爸爸」這類動詞/介詞被誤抓為姓)
_SURNAMES = (
    "趙錢孫李周吳鄭王馮陳褚衛蔣沈韓楊朱秦尤許何呂施張孔曹嚴華"
    "金魏陶姜戚謝鄒喻柏水竇章雲蘇潘葛奚範彭郎魯韋昌馬苗鳳花方俞任袁柳酆鮑史唐"
    "費廉岑薛雷賀倪湯滕殷羅畢郝鄔安常樂於時傅皮卞齊康伍餘元卜顧孟平黃和穆蕭尹"
    "姚邵湛汪祁毛禹狄米貝明臧計伏成戴談宋茅龐熊紀舒屈項祝董梁杜阮藍閔席季麻強"
    "賈路婁危江童顏郭梅盛林刁鍾徐邱駱高夏蔡田樊胡淩霍虞萬支柯昝管盧莫經房裘繆"
    "幹解應宗丁宣賁鄧鬱單杭洪包諸左石崔吉鈕龔程嵇邢滑裴陸榮翁荀羊於惠甄曲家封"
    "芮羿儲靳汲邴糜松井段富巫烏焦巴弓牧隗山穀車侯宓蓬全郗班仰秋仲伊宮甯仇欒暴"
    "甘鈄歷戎祖武符劉景詹束龍葉幸司韶郜黎薊薄印宿白懷蒲邰從鄂索鹹籍賴卓藺屠蒙"
    "池喬陰鬱胥能蒼雙聞莘黨翟譚貢勞逄姬申扶堵冉宰酈雍郤璩桑桂濮牛壽通邊扈燕冀"
    "郟浦尚農溫別莊晏柴瞿閻充慕連茹習宦艾魚容向古易慎戈廖庾終暨居衡步都耿滿弘"
    "匡國文寇廣祿闕東歐殳沃利蔚越夔隆師鞏厙聶晁勾敖融冷訾辛闞那簡饒空曾毋沙乜"
    "養鞠須豐巢關蒯相查後荊紅遊"
)
# 一字符姓氏 + 關係詞
_NAME_REL_RE = re.compile(rf"[{_SURNAMES}]{_REL_ALT}")

# 純關係詞 (preserve — these are NOT masked; this pattern is for whitelist check)
_PURE_REL = set(_REL_WORDS) | {
    "老婆", "老公", "妻子", "丈夫", "兒子", "女兒", "孫子", "孫女",
    "朋友", "同事", "同學", "客戶", "老師", "學生", "哥哥", "姊姊",
    "弟弟", "妹妹", "姐姐", "舅舅",
}

# jieba 偶爾把 TW 縣市 / 知名地點 tag 成 `nr` (人名) — 這些要 whitelist
# 避免被「以 jieba nr 為人名」邏輯誤抓。
_PLACE_WHITELIST = {
    "台北", "新北", "桃園", "台中", "台南", "高雄", "基隆", "新竹",
    "嘉義", "苗栗", "彰化", "南投", "雲林", "屏東", "宜蘭", "花蓮",
    "台東", "澎湖", "金門", "馬祖", "臺北", "臺中", "臺南", "臺東",
    "東京", "大阪", "首爾", "上海", "北京", "深圳", "香港",
    "美國", "日本", "韓國", "中國", "台灣", "臺灣",
}


# ─────────────────────────────────────────────────────────────────────────────
# Mapping helpers
# ─────────────────────────────────────────────────────────────────────────────
def _next_index(mapping: dict[str, str], category: str) -> int:
    """How many [CATEGORY_*] placeholders are already in mapping?"""
    prefix = f"[{category}_"
    n = 0
    for k in mapping.keys():
        if k.startswith(prefix):
            try:
                idx = int(k[len(prefix):-1])
                n = max(n, idx)
            except (ValueError, IndexError):
                pass
    return n + 1


def _placeholder_for(
    raw: str, category: str, mapping: dict[str, str], reverse: dict[str, str]
) -> str:
    """Get-or-create [CATEGORY_X] for `raw`. Same raw → same placeholder."""
    if raw in reverse:
        return reverse[raw]
    idx = _next_index(mapping, category)
    ph = f"[{category}_{idx}]"
    mapping[ph] = raw
    reverse[raw] = ph
    return ph


# ─────────────────────────────────────────────────────────────────────────────
# Span-based masking (so we don't double-mask overlapping ranges)
# ─────────────────────────────────────────────────────────────────────────────
def _find_spans(
    text: str, pattern: re.Pattern[str], category: str
) -> list[tuple[int, int, str, str]]:
    """Return (start, end, raw, category) spans for one pattern."""
    out: list[tuple[int, int, str, str]] = []
    for m in pattern.finditer(text):
        s, e = m.span()
        raw = m.group(0)
        out.append((s, e, raw, category))
    return out


def _resolve_overlaps(
    spans: list[tuple[int, int, str, str]],
) -> list[tuple[int, int, str, str]]:
    """Sort by (priority_implicit_in_input_order, start). Drop overlaps —
    the first one wins. Caller should pass spans in priority order
    (most-specific category first)."""
    spans_sorted = sorted(enumerate(spans), key=lambda kv: (kv[1][0], kv[0]))
    kept: list[tuple[int, int, str, str]] = []
    last_end = -1
    for _, sp in spans_sorted:
        s, e, raw, cat = sp
        if s < last_end:
            continue  # overlap with already-kept earlier span
        kept.append(sp)
        last_end = e
    return kept


def _detect_jieba_names(text: str) -> list[tuple[int, int, str, str]]:
    """Use jieba.posseg to find Chinese person names (`nr*` flags)
    and orgs (`nt*` flags). Return spans aligned to original text."""
    # We need offsets; jieba.posseg.cut yields tokens in order, so walk through.
    spans: list[tuple[int, int, str, str]] = []
    cursor = 0
    for word, flag in pseg.cut(text):
        # locate this word starting at `cursor` (token may not be unique;
        # left-to-right pass guarantees correct offset given jieba's
        # deterministic order)
        idx = text.find(word, cursor)
        if idx < 0:
            cursor += len(word)
            continue
        end = idx + len(word)
        cursor = end

        if not word.strip():
            continue
        if word in _PURE_REL:
            continue  # 老婆/媽媽 等 standalone — preserve
        if word in _PLACE_WHITELIST:
            continue  # 高雄/台北 等地名 — preserve

        if flag.startswith("nr") and len(word) >= 2:
            spans.append((idx, end, word, "NAME"))
        elif flag.startswith("nt") and len(word) >= 2:
            spans.append((idx, end, word, "ORG"))
    return spans


# ─────────────────────────────────────────────────────────────────────────────
# Public mask()
# ─────────────────────────────────────────────────────────────────────────────
def mask(
    text: str,
    mapping: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Mask PII in `text`. Same `raw` → same `[CATEGORY_X]` placeholder
    (within this call AND across calls if `mapping` is reused).

    Args:
      text     : input string
      mapping  : optional pre-existing {placeholder: raw} dict to extend

    Returns:
      (masked_text, mapping)
        mapping example: {"[NAME_1]": "黃媽媽", "[AMOUNT_1]": "5000元"}
    """
    if not text:
        return text, mapping or {}
    mapping = dict(mapping or {})
    reverse: dict[str, str] = {v: k for k, v in mapping.items()}

    # Order matters — most-specific first to avoid being eaten by a
    # generic pattern. We collect spans then resolve overlaps in that order.
    spans: list[tuple[int, int, str, str]] = []

    # First, mark dates so amount regex won't grab them; we add as "DATE"
    # category but won't actually emit DATE placeholders — purely a guard.
    date_spans = _find_spans(text, _DATE_RE, "DATE")

    # Tier 1 (super-specific id-like)
    spans.extend(_find_spans(text, _EMAIL_RE, "ID"))
    spans.extend(_find_spans(text, _URL_RE, "ID"))
    spans.extend(_find_spans(text, _ID_RE, "ID"))

    # Tier 2 account / phone (numeric)
    spans.extend(_find_spans(text, _ACCOUNT_DASH_RE, "ACCOUNT"))
    spans.extend(_find_spans(text, _ACCOUNT_16_RE, "ACCOUNT"))
    spans.extend(_find_spans(text, _PHONE_RE, "PHONE"))
    spans.extend(_find_spans(text, _ACCOUNT_GENERIC_RE, "ACCOUNT"))

    # Tier 3 amounts — but skip ones that overlap dates
    amt_spans = _find_spans(text, _AMOUNT_RE, "AMOUNT")
    for sp in amt_spans:
        s, e, _, _ = sp
        if any(ds <= s < de or ds < e <= de for ds, de, _, _ in date_spans):
            continue
        spans.append(sp)

    # Tier 4 addresses (full + local)
    spans.extend(_find_spans(text, _ADDR_FULL_RE, "ADDRESS"))
    spans.extend(_find_spans(text, _ADDR_LOCAL_RE, "ADDRESS"))

    # Tier 5 organizations by suffix
    spans.extend(_find_spans(text, _ORG_RE, "ORG"))

    # Tier 6 person names
    #   (a) explicit relationship prefix form (黃媽媽 / 王爸爸 …)
    spans.extend(_find_spans(text, _NAME_REL_RE, "NAME"))
    #   (b) jieba posseg `nr*` and `nt*`
    spans.extend(_detect_jieba_names(text))

    # Resolve overlaps — already-priority-ordered (earlier in list wins).
    # _resolve_overlaps requires spans pre-sorted-stable by input order.
    keep = _resolve_overlaps(spans)

    # Apply replacements right-to-left to keep offsets stable
    keep_sorted = sorted(keep, key=lambda sp: sp[0], reverse=True)
    out = text
    for s, e, raw, cat in keep_sorted:
        ph = _placeholder_for(raw, cat, mapping, reverse)
        out = out[:s] + ph + out[e:]

    return out, mapping


# ─────────────────────────────────────────────────────────────────────────────
def unmask(masked_text: str, mapping: dict[str, str]) -> str:
    """Reverse mask using the mapping. Longest placeholder first to avoid
    [NAME_1] partial-matching [NAME_10]."""
    out = masked_text
    for ph in sorted(mapping.keys(), key=len, reverse=True):
        out = out.replace(ph, mapping[ph])
    return out


# ─────────────────────────────────────────────────────────────────────────────
def count_pii(mapping: dict[str, str]) -> dict[str, int]:
    """Count placeholders by category."""
    out: dict[str, int] = {}
    for ph in mapping.keys():
        # ph = "[NAME_1]" → cat = "NAME"
        m = re.match(r"^\[([A-Z]+)_\d+\]$", ph)
        if not m:
            continue
        cat = m.group(1)
        out[cat] = out.get(cat, 0) + 1
    return out


__all__ = ["mask", "unmask", "count_pii"]
