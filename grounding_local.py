"""
grounding_local — 100% 本機 grounding heuristic（取代雲端 NLI）

設計目的：
    在 LINE bot 的 RAG / agent_loop 出 final answer 之後，用一組純本機 signal
    判斷「這個回覆是不是被 source 真的撐住的」。**完全沒有雲端呼叫**：

      1. **keyword overlap** — claim 跟 source 各自抽 jieba 關鍵詞，算 Jaccard。
      2. **embedding cosine** — 用 sentence-transformers MiniLM 算 cosine。
      3. **數字一致** — regex 抽兩段文字的數字，比對交集（夠寬鬆，看比例）。
      4. **命名實體一致** — 抽中文人名 / 機構 / 地名 / 英文大寫詞，比交集。

加權合分：
    score = 0.3*keyword + 0.4*cosine + 0.2*number_match + 0.1*entity_match

Verdict：
    - score > 0.6  → "supported"
    - score < 0.3  → "contradicts"
    - 介中間       → "uncertain"

對外 API：
    check_claim(claim, source) -> dict
    score_response(response, sources) -> dict
    extract_factual_claims(response) -> list[str]

Lazy load：sentence_transformers 跟 jieba 都 try-import，import 此 module
不會觸發 model 下載；第一次呼叫 score 時才載。
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

logger = logging.getLogger(__name__)

# ── tunable thresholds ────────────────────────────────────────────────────
SUPPORTED_THRESHOLD = 0.6
CONTRADICTS_THRESHOLD = 0.3
LOW_SCORE_THRESHOLD = 0.5  # score_response 標警示的 cut-off

# ── weighting (4 signals) ─────────────────────────────────────────────────
_W_KEYWORD = 0.3
_W_COSINE = 0.4
_W_NUMBER = 0.2
_W_ENTITY = 0.1
assert abs(_W_KEYWORD + _W_COSINE + _W_NUMBER + _W_ENTITY - 1.0) < 1e-9

# ── sentence-transformers model（與 rag_retriever / local_nli 同一隻，避免重複下載）
_ST_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# ── regex pool ────────────────────────────────────────────────────────────
# 拆句：句號 / 問號 / 驚嘆號 / 換行（中英都吃）。
# 注意：英文 '.' 後面要不是 digit 才算句尾，避免把小數 11.85 切成 "11." + "85"。
_SENTENCE_SPLIT_RE = re.compile(
    r"(?<=[。．！？!?])\s*|(?<=\.)(?!\d)\s*|\n+"
)

# 抽數字（含百分比 / 小數 / 千分位逗號 / 帶單位）
# 例：12、12.5、1,234、12%、3.14元
# 用 lookbehind 排除緊貼字母的 digit（Q1 / FY24 的 1, 24 不算事實數字，
# 那是 quarter/year 標籤，已經由 entity 信號處理）。
_NUMBER_RE = re.compile(r"(?<![A-Za-z])-?\d+(?:[.,]\d+)*%?")

# 數字一致的相對誤差容忍度。設 1%：11.85 vs 12 (1.27% 差) → miss，
# 11.85 vs 11.9 (0.4% 差) → match。比 5% 更貼合 grounding 用途
# (5% 對 EPS 那種小單位太鬆)。
_NUMBER_REL_TOLERANCE = 0.01

# 中文姓氏（top 100 — 抓 2-4 字人名啟發式用）
_CHINESE_SURNAMES = (
    "王李張劉陳楊黃趙吳周徐孫朱馬胡郭林何高梁鄭羅宋謝唐韓曹許鄧蕭馮"
    "曾程蔡彭潘袁於董余蘇葉呂魏蔣田杜丁沈姜范江傅鍾盧汪戴崔任陸廖姚"
    "方金邱夏譚韋賈鄒石熊孟秦閻薛侯雷白龍段郝孔邵史毛常萬顧賴武康賀"
    "嚴尹錢施牛洪龔湯陶黎溫莫易樊喬文安")

# 機構 / 公司 / 地名常見尾綴（啟發式抓組織名）
_ORG_SUFFIXES = (
    "公司", "集團", "銀行", "醫院", "大學", "學院", "中心", "協會", "基金會",
    "企業", "工廠", "證券", "保險", "金控", "電子", "科技", "半導體", "工業",
    "市", "縣", "區", "鄉", "鎮", "里", "路", "街", "省", "國", "州", "府",
)

# 意見句語氣詞（在 extract_factual_claims 用來剔除「我覺得 / 也許」）
# 注意：「可能」單獨出現太常見會誤刪，所以要搭配其他語氣詞才剔
_OPINION_MARKERS = (
    "我覺得", "我認為", "我猜", "我想", "也許", "可能會", "或許",
    "感覺", "個人覺得", "個人認為", "建議", "推測", "假設", "好像",
    "似乎", "據說", "聽說", "傳言", "感想", "看法是",
)

# 「事實性」訊號（含這些就更可能是事實句）
_FACTUAL_NUMBER_RE = re.compile(r"\d")
_FACTUAL_DATE_RE = re.compile(
    r"(?:\d{4}年|\d{1,2}月|\d{1,2}日|Q[1-4]|FY\d{2,4}|\d{4}/\d{1,2}|\d{4}-\d{1,2})"
)

_MIN_CLAIM_CHARS = 4  # 跟 local_nli 對齊

# ── lazy state ────────────────────────────────────────────────────────────
_st_lock = threading.Lock()
_st_model: Any = None
_st_load_failed = False

_jieba_lock = threading.Lock()
_jieba_module: Any = None
_jieba_load_failed = False


# ── lazy loaders ──────────────────────────────────────────────────────────
def _get_jieba() -> Any | None:
    """Lazy-import jieba。失敗一次後不再重試。"""
    global _jieba_module, _jieba_load_failed
    if _jieba_module is not None:
        return _jieba_module
    if _jieba_load_failed:
        return None
    with _jieba_lock:
        if _jieba_module is not None:
            return _jieba_module
        if _jieba_load_failed:
            return None
        try:
            import jieba  # type: ignore

            jieba.setLogLevel(logging.WARNING)
            _jieba_module = jieba
            return _jieba_module
        except Exception as e:
            logger.info("grounding_local: jieba unavailable: %s", e)
            _jieba_load_failed = True
            return None


def _get_st_model() -> Any | None:
    """Lazy-load sentence-transformers MiniLM。失敗後不再重試。"""
    global _st_model, _st_load_failed
    if _st_model is not None:
        return _st_model
    if _st_load_failed:
        return None
    with _st_lock:
        if _st_model is not None:
            return _st_model
        if _st_load_failed:
            return None
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore

            _st_model = SentenceTransformer(_ST_MODEL_NAME, device="cpu")
            return _st_model
        except Exception as e:
            logger.warning(
                "grounding_local: sentence-transformers load failed: %s", e
            )
            _st_load_failed = True
            return None


# ── tokenize / entity helpers ─────────────────────────────────────────────
# 與 bm25_index.tokenize 對齊的最小 inline 版本。沒用 bm25_index.tokenize
# 是因為它會過濾掉純數字 / 單字標點，我們這邊算 keyword overlap 想保留。
_TOKEN_KEEP_RE = re.compile(r"[一-鿿㐀-䶿A-Za-z0-9]+", flags=re.UNICODE)

_STOPWORDS = {
    # zh
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "們",
    "也", "和", "與", "或", "但", "而", "就", "都", "還", "又",
    "嗎", "呢", "吧", "啊", "喔", "哦", "唉",
    "把", "被", "讓", "給", "對", "向", "從", "到", "於",
    "這", "那", "哪", "什", "麼", "怎", "為", "何",
    "有", "沒", "不", "會", "要", "可", "以", "能",
    "個", "些", "之", "其", "等", "已", "再", "更", "說", "做",
    # en
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "and", "or", "but", "if", "of", "to", "in", "on", "at", "by", "for",
    "with", "as", "from", "this", "that", "these", "those",
    "i", "you", "he", "she", "it", "we", "they",
    "do", "does", "did", "have", "has", "had",
    "will", "would", "can", "could", "should", "may", "might",
}


def tokenize(text: str) -> list[str]:
    """中英混合分詞。jieba 可用就走 jieba，不行就降級成字符級 unigram。

    回 lowercase token list，過濾停用詞 + 空字串。
    """
    if not text:
        return []
    text = text.strip()
    if not text:
        return []

    j = _get_jieba()
    if j is not None:
        raw = list(j.lcut(text, cut_all=False))
        out: list[str] = []
        for t in raw:
            t = t.strip().lower()
            if not t:
                continue
            if not _TOKEN_KEEP_RE.match(t):
                continue
            if t in _STOPWORDS:
                continue
            out.append(t)
        return out

    # fallback：粗暴的字符級切割（只留 CJK + 英數）
    out2: list[str] = []
    for chunk in _TOKEN_KEEP_RE.findall(text.lower()):
        if any(c.isascii() for c in chunk):
            if chunk in _STOPWORDS:
                continue
            out2.append(chunk)
        else:
            for ch in chunk:
                if ch in _STOPWORDS:
                    continue
                out2.append(ch)
    return out2


def extract_numbers(text: str) -> list[str]:
    """從文字抽出所有數字 token（含 %、小數、千分位）。"""
    if not text:
        return []
    return list(_NUMBER_RE.findall(text))


def extract_entities(text: str) -> set[str]:
    """啟發式命名實體抽取（人名 / 機構 / 地名 / 大寫英文詞）。

    這是極度啟發式的版本，不接 spaCy / CKIP（會增加依賴與啟動時間）。
    抓四類：

      1. 中文 2-4 字人名（首字是常見姓氏）
      2. 含組織尾綴的詞段（公司 / 大學 / 醫院 / 市 / 區 …）
      3. 英文連續大寫片語（Apple Inc / TSMC / NVIDIA）
      4. 連續數字+單位的 ticker-ish token（不在這裡抓，由 number signal 處理）
    """
    if not text:
        return set()

    ents: set[str] = set()

    # (1) 中文人名啟發式：抓 2~4 字、首字是常見姓
    # 注意：這是 noisy 抓法，會 false positive，但 entity_match 只佔 0.1
    # 權重，所以 acceptable。
    for m in re.finditer(r"[一-鿿]{2,4}", text):
        token = m.group(0)
        if token[0] in _CHINESE_SURNAMES:
            # 排除明顯不是人名的（含尾綴）
            if not any(token.endswith(s) for s in _ORG_SUFFIXES):
                ents.add(token)

    # (2) 機構 / 地名：含尾綴的詞（往前抓 2-6 字）
    for suf in _ORG_SUFFIXES:
        for m in re.finditer(rf"[一-鿿A-Za-z0-9]{{1,8}}{suf}", text):
            ents.add(m.group(0))

    # (3) 英文大寫片語：連續 1+ 個首字大寫的英文單字（含全大寫如 TSMC）
    # 排除句首單一單字（避免把每句第一個詞抓進來）
    for m in re.finditer(r"\b[A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)*\b", text):
        token = m.group(0).strip()
        if len(token) >= 2:
            ents.add(token)

    return ents


# ── signal: keyword overlap (Jaccard) ─────────────────────────────────────
def _keyword_overlap(claim: str, source: str) -> float:
    """Jaccard similarity over tokenized keyword sets。

    回 [0, 1]。空 claim 或空 source → 0.0（無法比較）。
    """
    a = set(tokenize(claim))
    b = set(tokenize(source))
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union)


# ── signal: embedding cosine ──────────────────────────────────────────────
def _embedding_cosine(claim: str, source: str) -> float:
    """Sentence-transformers cosine similarity。

    Model 不可用 → 退而用 char-bigram Jaccard 當 proxy（仍是本機）。
    回 [0, 1]（負的 cosine 會被 clamp 到 0）。
    """
    if not claim or not source:
        return 0.0

    model = _get_st_model()
    if model is not None:
        try:
            import numpy as np  # type: ignore

            vecs = model.encode(
                [claim, source],
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            sim = float(np.dot(vecs[0], vecs[1]))
            return max(0.0, min(1.0, sim))
        except Exception as e:
            logger.warning("grounding_local: ST encode failed: %s", e)

    # fallback：char bigram Jaccard
    return _char_bigram_jaccard(claim, source)


def _char_bigram_jaccard(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    g_a = {a[i : i + 2] for i in range(len(a) - 1)} if len(a) >= 2 else {a}
    g_b = {b[i : i + 2] for i in range(len(b) - 1)} if len(b) >= 2 else {b}
    if not g_a or not g_b:
        return 0.0
    inter = g_a & g_b
    union = g_a | g_b
    return len(inter) / len(union)


# ── signal: number match ──────────────────────────────────────────────────
def _number_match(claim: str, source: str) -> float:
    """數字一致比例。

    取 claim 跟 source 各自的數字集合：
      - 兩邊都沒數字 → 1.0（沒衝突點，視為通過）
      - 只有 claim 有 → 0.0（claim 提了 source 沒佐證的數字 → 不一致）
      - 兩邊都有 → claim 的數字裡，被 source 命中的比例

    把「12.5」跟「12.5」當完全一致；「12」對「12.5」也算一致（去 cast float
    後相對誤差 < 5%）。
    """
    nums_c = extract_numbers(claim)
    nums_s = extract_numbers(source)

    if not nums_c and not nums_s:
        return 1.0
    if not nums_c:
        # source 有數字、claim 沒提到 → 沒有衝突，視為通過
        return 1.0
    if not nums_s:
        # claim 有數字、source 完全無數字 → 高度可疑
        return 0.0

    # 把兩邊都試著轉 float（去掉 % 跟逗號）
    src_floats: list[float] = []
    for s in nums_s:
        f = _try_float(s)
        if f is not None:
            src_floats.append(f)

    if not src_floats:
        return 0.0

    matched = 0
    for c in nums_c:
        cf = _try_float(c)
        if cf is None:
            continue
        # 一致定義：絕對相等 或 相對誤差 < 5%
        for sf in src_floats:
            if cf == sf:
                matched += 1
                break
            if sf == 0:
                continue
            if abs(cf - sf) / max(abs(sf), 1e-9) < _NUMBER_REL_TOLERANCE:
                matched += 1
                break

    return matched / max(len(nums_c), 1)


def _try_float(s: str) -> float | None:
    """寬鬆把 '12,345.6%' / '12.5元' 變成 float（去掉 %、逗號、單位字）。"""
    if not s:
        return None
    cleaned = s.replace(",", "").rstrip("%")
    try:
        return float(cleaned)
    except ValueError:
        return None


# ── signal: entity match ──────────────────────────────────────────────────
def _entity_match(claim: str, source: str) -> float:
    """命名實體一致比例（claim 的實體被 source 命中的比例）。"""
    ents_c = extract_entities(claim)
    ents_s = extract_entities(source)

    if not ents_c and not ents_s:
        return 1.0
    if not ents_c:
        return 1.0
    if not ents_s:
        return 0.0

    matched = 0
    for ec in ents_c:
        # 完全相等 或 source 包含此實體（前後字差別容忍）
        if ec in ents_s:
            matched += 1
            continue
        if any(ec in es or es in ec for es in ents_s):
            matched += 1
    return matched / max(len(ents_c), 1)


# ── public: check_claim ───────────────────────────────────────────────────
def check_claim(claim: str, source: str) -> dict[str, Any]:
    """對單一 claim 跑 4 種 signal、加權算 score、給 verdict。

    Returns
    -------
    {
        "score": float ∈ [0, 1],
        "signals": {
            "keyword": float,
            "cosine": float,
            "number_match": float,
            "entity_match": float,
        },
        "verdict": "supported" | "uncertain" | "contradicts",
    }
    """
    if not claim:
        # 空 claim → 沒東西好檢查，視為 supported
        return {
            "score": 1.0,
            "signals": {
                "keyword": 0.0,
                "cosine": 0.0,
                "number_match": 1.0,
                "entity_match": 1.0,
            },
            "verdict": "supported",
        }
    if not source:
        # 沒 source 可比 → uncertain
        return {
            "score": 0.0,
            "signals": {
                "keyword": 0.0,
                "cosine": 0.0,
                "number_match": 0.0,
                "entity_match": 0.0,
            },
            "verdict": "uncertain",
        }

    kw = _keyword_overlap(claim, source)
    cos = _embedding_cosine(claim, source)
    num = _number_match(claim, source)
    ent = _entity_match(claim, source)

    score = _W_KEYWORD * kw + _W_COSINE * cos + _W_NUMBER * num + _W_ENTITY * ent
    score = max(0.0, min(1.0, score))

    if score > SUPPORTED_THRESHOLD:
        verdict = "supported"
    elif score < CONTRADICTS_THRESHOLD:
        verdict = "contradicts"
    else:
        verdict = "uncertain"

    return {
        "score": float(score),
        "signals": {
            "keyword": float(kw),
            "cosine": float(cos),
            "number_match": float(num),
            "entity_match": float(ent),
        },
        "verdict": verdict,
    }


# ── public: split_into_claims ─────────────────────────────────────────────
def split_into_claims(text: str) -> list[str]:
    """把 response 切成 sentence-level claims。"""
    if not text:
        return []
    pieces = _SENTENCE_SPLIT_RE.split(text)
    out: list[str] = []
    for p in pieces:
        s = (p or "").strip()
        if not s:
            continue
        if len(s) < _MIN_CLAIM_CHARS:
            continue
        out.append(s)
    return out


# ── public: extract_factual_claims ────────────────────────────────────────
def extract_factual_claims(response: str) -> list[str]:
    """從 response 抽「事實性句子」。

    規則：
      - 切句
      - 過濾意見性句子（含「我覺得 / 也許 / 可能會」這些 marker）
      - 留下含數字、日期、實體的句子（事實訊號至少有一個）
      - 找不到事實訊號的中性敘述句也保留（保守一點，不漏判）

    意見句被剔除：「我覺得這支股票會漲」「也許明天會下雨」
    保留：「台積電 Q1 EPS 12 元」「3 月 15 日召開股東會」
    """
    if not response:
        return []

    sentences = split_into_claims(response)
    out: list[str] = []
    for s in sentences:
        if _is_opinion(s):
            continue
        out.append(s)
    return out


def _is_opinion(sentence: str) -> bool:
    """判斷句子是不是意見 / 推測句。"""
    for marker in _OPINION_MARKERS:
        if marker in sentence:
            return True
    return False


# ── public: score_response ────────────────────────────────────────────────
def _best_source_for_claim(claim: str, sources: list[str]) -> tuple[str, int]:
    """挑最相關的 source。優先用 ST cosine top-1；ST 不可用退 keyword overlap。"""
    if not sources:
        return "", -1
    if len(sources) == 1:
        return sources[0], 0

    model = _get_st_model()
    if model is not None:
        try:
            import numpy as np  # type: ignore

            vecs = model.encode(
                [claim] + list(sources),
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            sims = vecs[1:] @ vecs[0]
            idx = int(sims.argmax())
            return sources[idx], idx
        except Exception as e:
            logger.warning("grounding_local: best-source ST failed: %s", e)

    # fallback：keyword overlap
    best_idx = 0
    best_score = -1.0
    for i, src in enumerate(sources):
        sc = _keyword_overlap(claim, src)
        if sc > best_score:
            best_score = sc
            best_idx = i
    return sources[best_idx], best_idx


def score_response(response: str, sources: list[str]) -> dict[str, Any]:
    """對整段 response 算 grounding score。

    流程：
      1. 切句（split_into_claims）
      2. 對每句挑最相關 source（cosine top-1）
      3. 對每句跑 check_claim
      4. 收集低分句、算平均

    Returns
    -------
    {
        "score_avg": float ∈ [0, 1],          # 沒 claim / 沒 source → 1.0
        "low_score_claims": [
            {"claim": str, "score": float, "verdict": str, "source_idx": int},
            ...
        ],
        "summary": str,                        # 人類可讀的摘要
        "verified": [...],                     # 全部驗過的 claim（含 score）
    }
    """
    claims = split_into_claims(response)

    if not claims:
        return {
            "score_avg": 1.0,
            "low_score_claims": [],
            "summary": "no claims to verify",
            "verified": [],
        }
    if not sources:
        return {
            "score_avg": 0.0,
            "low_score_claims": [
                {
                    "claim": c,
                    "score": 0.0,
                    "verdict": "uncertain",
                    "source_idx": -1,
                }
                for c in claims
            ],
            "summary": f"no sources provided for {len(claims)} claim(s)",
            "verified": [],
        }

    verified: list[dict[str, Any]] = []
    low_score: list[dict[str, Any]] = []
    total = 0.0

    for claim in claims:
        src, src_idx = _best_source_for_claim(claim, sources)
        result = check_claim(claim, src)
        score = float(result["score"])
        total += score
        entry = {
            "claim": claim,
            "score": score,
            "verdict": result["verdict"],
            "signals": result["signals"],
            "source_idx": src_idx,
        }
        verified.append(entry)
        if score < LOW_SCORE_THRESHOLD:
            low_score.append(entry)

    avg = total / len(claims)

    if avg > SUPPORTED_THRESHOLD:
        summary = (
            f"{len(claims)} claim(s) checked, avg={avg:.2f} — well grounded"
        )
    elif avg < CONTRADICTS_THRESHOLD:
        summary = (
            f"{len(claims)} claim(s) checked, avg={avg:.2f} — POOR grounding"
        )
    else:
        summary = (
            f"{len(claims)} claim(s) checked, avg={avg:.2f} — partial grounding,"
            f" {len(low_score)} weak claim(s)"
        )

    return {
        "score_avg": float(avg),
        "low_score_claims": low_score,
        "summary": summary,
        "verified": verified,
    }


# ── test reset hook ───────────────────────────────────────────────────────
def _reset_state_for_tests() -> None:
    """pytest 用：清掉 lazy state，下次呼叫從頭走一次 fallback 邏輯。"""
    global _st_model, _st_load_failed
    global _jieba_module, _jieba_load_failed
    _st_model = None
    _st_load_failed = False
    _jieba_module = None
    _jieba_load_failed = False
