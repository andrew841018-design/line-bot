"""
chinese_nlp.py — 純本機中文 NLP 工具（zero cloud API）。

提供 5 個函式給 LINE bot 使用：
  - tokenize(text)            : jieba 斷詞 + 停用詞過濾
  - extract_keywords(text, k) : TF-IDF 關鍵詞抽取
  - classify_intent(text)     : 規則樹意圖分類（9 類）
  - detect_sentiment(text)    : lexicon-based 情感分析
  - extract_entities(text)    : regex + jieba.posseg 命名實體

設計原則：
  - 全本機運算，不打雲端 API
  - 規則 / 詞表手刻，方便 Andrew 加詞
  - 介面 stable，方便 lite_reply.py 第一層 intent 分流調用

整合建議（給主執行緒）：
  lite_reply.py 第一層 intent 分流改用 chinese_nlp.classify_intent，
  原本「if 包含'股票' or '股價'」這類散落 keyword 邏輯統一交給 classify_intent。
  好處：(1) 命中率因為加了同義詞 + 詞性 (2) 維護集中 (3) 加新意圖只動 _INTENT_RULES。
"""

from __future__ import annotations

import re
import warnings
from typing import Any

# jieba 在 Python 3.13 會跳 pkg_resources DeprecationWarning，靜音
warnings.filterwarnings("ignore", message=".*pkg_resources.*")

import jieba  # noqa: E402
import jieba.analyse  # noqa: E402
import jieba.posseg as pseg  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# 繁中分詞優先：jieba 預設簡中字典，這裡硬塞一些常用台灣詞，避免被切碎
# ─────────────────────────────────────────────────────────────────────────────
_TRAD_USERWORDS: tuple[str, ...] = (
    "台積電",
    "聯發科",
    "鴻海",
    "中華電信",
    "高鐵",
    "捷運",
    "颱風",
    "天氣預報",
    "股價",
    "大盤",
    "漲幅",
    "跌幅",
    "外資",
    "投信",
    "本益比",
    "殖利率",
    "新台幣",
    "台幣",
    "台北",
    "新北",
    "桃園",
    "台中",
    "台南",
    "高雄",
    "新竹",
    "嘉義",
    "宜蘭",
    "花蓮",
    "台東",
    "屏東",
    "苗栗",
    "彰化",
    "雲林",
    "南投",
    "基隆",
    "澎湖",
    "金門",
    "馬祖",
    "翻譯",
    "畫一張",
    "生成圖",
    "做張圖",
)
for _w in _TRAD_USERWORDS:
    jieba.add_word(_w)

# 靜音 jieba 第一次 build dict 的 log
jieba.setLogLevel(60)

# ─────────────────────────────────────────────────────────────────────────────
# 自建停用詞 (~50)
# ─────────────────────────────────────────────────────────────────────────────
_STOPWORDS: frozenset[str] = frozenset(
    {
        "的",
        "了",
        "在",
        "是",
        "我",
        "你",
        "他",
        "她",
        "它",
        "們",
        "也",
        "都",
        "就",
        "會",
        "要",
        "可",
        "可以",
        "把",
        "被",
        "和",
        "與",
        "或",
        "及",
        "啊",
        "呢",
        "嗎",
        "吧",
        "唷",
        "喔",
        "耶",
        "嘛",
        "哈",
        "齁",
        "欸",
        "對",
        "對啊",
        "好",
        "好的",
        "嗯",
        "這",
        "那",
        "這個",
        "那個",
        "這些",
        "那些",
        "什麼",
        "怎麼",
        "為",
        "因為",
        "所以",
        "但",
        "但是",
        "不過",
        "然後",
        "之後",
        "之前",
        "現在",
        "已經",
        "還",
        "還有",
        "一個",
        "一些",
        "有",
        "沒有",
        "沒",
        "不",
        "啦",
        "囉",
        "去",
        "來",
        "說",
        "讓",
        "給",
        "用",
        "為了",
        "覺得",
    }
)

# ─────────────────────────────────────────────────────────────────────────────
# 意圖規則樹（順序 = 優先級；先命中先決定）
#   tuple 結構：(intent_name, [keyword, ...])
# ─────────────────────────────────────────────────────────────────────────────
_INTENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "image_gen",
        ("畫一張", "畫張", "生成圖", "做張圖", "畫圖", "生圖", "做一張圖", "畫個"),
    ),
    (
        "translation",
        ("翻譯", "翻成", "英文怎麼說", "日文怎麼說", "中翻英", "英翻中", "翻一下"),
    ),
    (
        "datetime",
        ("現在幾點", "今天幾號", "今天星期", "今天禮拜", "幾月幾日", "今天日期", "現在時間"),
    ),
    (
        "stock",
        (
            "股票",
            "股價",
            "漲跌",
            "大盤",
            "漲幅",
            "跌幅",
            "漲多少",
            "跌多少",
            "漲",
            "跌",
            "台股",
            "美股",
            "成交量",
            "本益比",
            "殖利率",
            "外資",
            "投信",
            "台積電",
            "聯發科",
            "鴻海",
            "中華電信",
        ),
    ),
    (
        "weather",
        ("天氣", "氣溫", "下雨", "颱風", "降雨", "氣象", "豪雨", "寒流", "高溫", "低溫"),
    ),
    (
        "transport",
        ("公車", "捷運", "高鐵", "火車", "台鐵", "客運", "班次", "幾分鐘", "幾點到"),
    ),
    (
        "reasoning",
        ("為什麼", "怎麼算", "解釋", "原因", "為何", "怎麼來的", "怎麼會", "為啥"),
    ),
    (
        "emotion",
        (
            "心情",
            "難過",
            "開心",
            "失落",
            "崩潰",
            "鬱悶",
            "焦慮",
            "煩躁",
            "心累",
            "想哭",
            "好爽",
            "超爽",
        ),
    ),
)

# ─────────────────────────────────────────────────────────────────────────────
# 情感詞表（lexicon-based，~100 字）
# ─────────────────────────────────────────────────────────────────────────────
_POSITIVE_WORDS: frozenset[str] = frozenset(
    {
        "開心",
        "高興",
        "快樂",
        "愉快",
        "滿意",
        "喜歡",
        "愛",
        "讚",
        "棒",
        "好棒",
        "厲害",
        "感謝",
        "謝謝",
        "感激",
        "幸福",
        "溫暖",
        "舒服",
        "舒適",
        "順利",
        "成功",
        "完美",
        "優秀",
        "卓越",
        "美好",
        "美麗",
        "漂亮",
        "可愛",
        "好看",
        "好聽",
        "好吃",
        "好喝",
        "好玩",
        "輕鬆",
        "放鬆",
        "驚喜",
        "驚艷",
        "佩服",
        "崇拜",
        "尊敬",
        "希望",
        "期待",
        "信任",
        "安心",
        "踏實",
        "感動",
        "暖心",
        "貼心",
        "支持",
        "鼓勵",
        "贊同",
    }
)

_NEGATIVE_WORDS: frozenset[str] = frozenset(
    {
        "難過",
        "傷心",
        "想哭",
        "崩潰",
        "失落",
        "失望",
        "絕望",
        "焦慮",
        "煩躁",
        "鬱悶",
        "憂鬱",
        "沮喪",
        "痛苦",
        "辛苦",
        "心累",
        "疲憊",
        "厭倦",
        "討厭",
        "生氣",
        "憤怒",
        "暴怒",
        "不爽",
        "煩死",
        "好爛",
        "超爛",
        "很爛",
        "糟糕",
        "悲劇",
        "可悲",
        "可憐",
        "委屈",
        "孤單",
        "寂寞",
        "無聊",
        "無奈",
        "失敗",
        "完蛋",
        "倒楣",
        "可怕",
        "恐怖",
        "害怕",
        "擔心",
        "緊張",
        "不安",
        "懷疑",
        "後悔",
        "遺憾",
        # 常見單字（會被 jieba 切成獨立 token 的場合才命中）
        "哭",
        "氣",
        "煩",
        "爛",
        "糟",
        "慘",
        "輸",
        "衰",
        "恨",
    }
)

# 否定詞：碰到「不開心」這種，把後面的正向詞反轉
_NEGATORS: frozenset[str] = frozenset({"不", "沒", "沒有", "別", "未", "非", "毋"})

# ─────────────────────────────────────────────────────────────────────────────
# 命名實體 regex（按精度排序，越精準越前面）
# ─────────────────────────────────────────────────────────────────────────────
_RE_STOCK_CODE = re.compile(r"\b(\d{4,6}[A-Z]?)\b")
_RE_DATE_NUMERIC = re.compile(r"(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})")
_RE_DATE_RELATIVE = re.compile(
    r"(今天|明天|後天|大後天|昨天|前天|今日|明日|本週|下週|上週|這週|本月|下月|上月)"
)
_RE_TIME_NUMERIC = re.compile(r"(\d{1,2}:\d{2})")
_RE_TIME_CHINESE = re.compile(
    r"(早上|上午|中午|下午|傍晚|晚上|凌晨|半夜)([零一二兩三四五六七八九十百兩\d]+)點"
    r"(?:([零一二兩三四五六七八九十\d]+)分)?"
)
_RE_AMOUNT_NUMERIC = re.compile(r"(\d+(?:[,.]\d+)?\s*(?:元|塊|萬|億|千|百))")
_RE_AMOUNT_CHINESE = re.compile(
    r"([零一二兩三四五六七八九十百千萬億]+(?:萬|億|千|百|十)?(?:元|塊)?)"
)
# 有 4 碼數字接「點」很可能是時間「下午3點」結尾，避開
_PUNCT_RE = re.compile(r"[\s。，、！？；：「」『』（）()【】《》\.,!?;:'\"\-—…]+")

# 不太想被當股票代號的純年份
_YEAR_BLACKLIST: frozenset[str] = frozenset(
    {f"{y}" for y in range(1980, 2050)}
)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
def tokenize(text: str) -> list[str]:
    """
    jieba 斷詞 + 過濾停用詞 / 標點 / 空白。

    Args:
        text: 任意中文字串（容忍夾雜英數）。

    Returns:
        斷詞後的 list；不含停用詞 / 標點 / 空字串。
    """
    if not text or not text.strip():
        return []
    # cut() 預設精確模式，繁中已經由 add_word 加強
    raw_tokens = jieba.lcut(text, cut_all=False)
    out: list[str] = []
    for tok in raw_tokens:
        tok = tok.strip()
        if not tok:
            continue
        if _PUNCT_RE.fullmatch(tok):
            continue
        if tok in _STOPWORDS:
            continue
        out.append(tok)
    return out


def extract_keywords(text: str, top_k: int = 5) -> list[tuple[str, float]]:
    """
    TF-IDF 關鍵詞，過濾單字 + 標點。

    Returns:
        [(word, score), ...]，score 為 jieba 內部 TF-IDF 分數。
    """
    if not text or not text.strip():
        return []
    raw = jieba.analyse.extract_tags(text, topK=top_k * 3, withWeight=True)
    out: list[tuple[str, float]] = []
    for word, score in raw:
        if len(word) < 2:
            continue
        if _PUNCT_RE.fullmatch(word):
            continue
        if word in _STOPWORDS:
            continue
        out.append((word, float(score)))
        if len(out) >= top_k:
            break
    return out


def classify_intent(text: str) -> dict[str, Any]:
    """
    規則樹意圖分類。

    Returns:
        {
          "intent": str,                # _INTENT_RULES 之一 or 'general'
          "confidence": float,          # 0~1，命中關鍵詞數 / 該意圖詞表比例（capped 1.0）
          "matched_keywords": list[str],
        }
    """
    if not text or not text.strip():
        return {"intent": "general", "confidence": 0.0, "matched_keywords": []}

    best_intent = "general"
    best_matched: list[str] = []

    for intent, keywords in _INTENT_RULES:
        matched = [kw for kw in keywords if kw in text]
        if not matched:
            continue
        # 第一個有命中的規則直接拿走（規則樹優先級）
        best_intent = intent
        best_matched = matched
        break

    # 後備：純股票代號（如 "2330 跟 2317 哪個好"）也歸到 stock
    if best_intent == "general":
        stock_codes = [
            m for m in _RE_STOCK_CODE.findall(text)
            if m not in _YEAR_BLACKLIST and len(m) < 7
        ]
        if stock_codes:
            best_intent = "stock"
            best_matched = stock_codes

    # confidence 公式：命中越多越高，capped 1.0；general 為 0
    if best_intent == "general":
        confidence = 0.0
    else:
        confidence = min(1.0, 0.5 + 0.15 * len(best_matched))

    return {
        "intent": best_intent,
        "confidence": round(confidence, 3),
        "matched_keywords": best_matched,
    }


def detect_sentiment(text: str) -> str:
    """
    Lexicon-based 情感分析。

    流程：
      - jieba 斷詞
      - 對每個 token 查正/負詞表，遇否定詞反轉下一個情感詞
      - 計算 net score → positive (>0) / negative (<0) / neutral (=0)

    Returns:
        'positive' | 'negative' | 'neutral'
    """
    if not text or not text.strip():
        return "neutral"

    tokens = jieba.lcut(text, cut_all=False)
    score = 0
    negate_next = False
    matched_spans: set[str] = set()  # 已被斷詞 hit 的詞，避免 fallback 重複加分
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if tok in _NEGATORS:
            negate_next = True
            continue
        polarity = 0
        if tok in _POSITIVE_WORDS:
            polarity = 1
        elif tok in _NEGATIVE_WORDS:
            polarity = -1
        if polarity != 0:
            if negate_next:
                polarity = -polarity
                negate_next = False
            score += polarity
            matched_spans.add(tok)
        else:
            # 連兩個非情感詞，否定詞效果就消失（避免「不」黏太遠）
            if negate_next and not _PUNCT_RE.fullmatch(tok):
                negate_next = False

    # Fallback：jieba 偶爾把情感詞切碎（'超開'+'心耶謝'+'謝'）或黏成
    # '不開心' 一整塊。對沒被 hit 過、長度 >= 2 的情感詞做子串掃描補強。
    # （單字詞不做，會誤命中：'氣' in '天氣', '輸' in '運輸' 之類）
    # 注意：fallback 也要尊重否定詞——若情感詞前面緊鄰否定詞則反轉。
    def _polarity_with_negator(needle: str, polarity: int) -> int:
        idx = text.find(needle)
        flipped = polarity
        while idx != -1:
            # 看前一個字是否為單字否定詞
            if idx > 0 and text[idx - 1] in {"不", "沒", "別", "未", "非", "毋"}:
                flipped = -polarity
                break
            # 看前兩字（"沒有"）
            if idx >= 2 and text[idx - 2 : idx] in {"沒有"}:
                flipped = -polarity
                break
            idx = text.find(needle, idx + 1)
        return flipped

    for w in _POSITIVE_WORDS:
        if len(w) >= 2 and w not in matched_spans and w in text:
            score += _polarity_with_negator(w, 1)
            matched_spans.add(w)
    for w in _NEGATIVE_WORDS:
        if len(w) >= 2 and w not in matched_spans and w in text:
            score += _polarity_with_negator(w, -1)
            matched_spans.add(w)

    if score > 0:
        return "positive"
    if score < 0:
        return "negative"
    return "neutral"


def extract_entities(text: str) -> dict[str, list[str]]:
    """
    Regex + jieba.posseg 命名實體抽取。

    Returns:
        {
          "stock": [...],  # 4-6 碼股票代號（避開純年份）
          "date":  [...],  # YYYY/MM/DD or 明天 / 後天
          "time":  [...],  # HH:MM or 下午三點
          "amount": [...], # 100元 / 兩百萬
          "person": [...], # jieba.posseg 'nr'
          "location": [...],# 'ns'
        }
    """
    out: dict[str, list[str]] = {
        "stock": [],
        "date": [],
        "time": [],
        "amount": [],
        "person": [],
        "location": [],
    }
    if not text or not text.strip():
        return out

    # ── 股票代號（避開純年份）──────────────────────────────────────────────
    for m in _RE_STOCK_CODE.findall(text):
        if m in _YEAR_BLACKLIST:
            continue
        # 也避開太長純數字（>=7 碼當電話 / 帳號之類）
        if len(m) >= 7:
            continue
        if m not in out["stock"]:
            out["stock"].append(m)

    # ── 日期 ──────────────────────────────────────────────────────────────
    for m in _RE_DATE_NUMERIC.findall(text):
        if m not in out["date"]:
            out["date"].append(m)
    for m in _RE_DATE_RELATIVE.findall(text):
        if m not in out["date"]:
            out["date"].append(m)

    # ── 時間 ──────────────────────────────────────────────────────────────
    for m in _RE_TIME_NUMERIC.findall(text):
        if m not in out["time"]:
            out["time"].append(m)
    for groups in _RE_TIME_CHINESE.findall(text):
        # groups: (period, hour, minute?)
        period, hour, minute = groups
        joined = f"{period}{hour}點" + (f"{minute}分" if minute else "")
        if joined not in out["time"]:
            out["time"].append(joined)

    # ── 金額 ──────────────────────────────────────────────────────────────
    for m in _RE_AMOUNT_NUMERIC.findall(text):
        m = m.strip()
        if m and m not in out["amount"]:
            out["amount"].append(m)
    # 中文數字（兩百萬 / 五千元）
    for m in _RE_AMOUNT_CHINESE.findall(text):
        m = m.strip()
        # 過濾長度 < 2 的（避免 '十' 這種單字誤抓）
        if len(m) < 2:
            continue
        # 必須帶量詞（萬 / 億 / 千 / 百 / 元 / 塊）才算金額，不然容易誤抓
        if not any(unit in m for unit in ("萬", "億", "千", "百", "元", "塊")):
            continue
        if m not in out["amount"]:
            out["amount"].append(m)

    # ── 人名 / 地名（jieba.posseg）─────────────────────────────────────────
    # 人名常見誤抓：含語助詞、含「謝謝」、含數字 → 過濾
    _PERSON_NOISE_CHARS = set("耶喔啊呢嗎吧唷嘛哈齁欸的了啦囉")
    for word, flag in pseg.lcut(text):
        if flag == "nr" and len(word) >= 2 and word not in out["person"]:
            # 過濾雜訊：含語助詞 / 「謝謝」「不要」這類非人名
            if any(c in _PERSON_NOISE_CHARS for c in word):
                continue
            if word in {"謝謝", "不要", "知道", "可以", "可能", "請問"}:
                continue
            if any(c.isdigit() for c in word):
                continue
            out["person"].append(word)
        elif flag == "ns" and len(word) >= 2 and word not in out["location"]:
            out["location"].append(word)

    return out


__all__ = [
    "tokenize",
    "extract_keywords",
    "classify_intent",
    "detect_sentiment",
    "extract_entities",
]
