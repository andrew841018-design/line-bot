"""Local BM25 index for the LINE bot RAG pipeline.

Pure-Python sparse retrieval (rank_bm25 + jieba), 100% local, no cloud API.
Designed to slot in next to `rag_retriever.retrieve` (cosine dense) so a
hybrid weighted fusion (`hybrid_retrieve.hybrid_search`) can combine both.

Why this exists:

1. Dense embeddings (MiniLM / bge-m3) miss exact-keyword hits — short
   queries like a single name, ticker symbol, or unusual term can rank
   far down because the cosine surface is smooth.
2. BM25 is the classical sparse counterpart: it rewards rare-word
   overlap and term-frequency saturation. Together with cosine they
   cover complementary failure modes.
3. We keep it pure-Python (rank_bm25 ~200 LoC) so there's no native
   build dep, no network call, no model download. Works on any laptop
   that already runs the bot.

Public surface:
    BM25Index.build(corpus: list[str]) -> None
    BM25Index.query(q: str, k: int = 10) -> list[tuple[int, float]]
    BM25Index.save(path: str | Path) -> None
    BM25Index.load(path: str | Path) -> "BM25Index"

Tokenization strategy:

* Primary: jieba (`jieba.lcut`) — handles Mandarin word segmentation
  well enough for short LINE messages. lowercase + ASCII-only filter
  for English mixed in.
* Fallback: character bigrams when jieba isn't importable. Slower-
  matching but never zero-recall on Chinese.
* Stopword list: a small built-in zh + en set. Tuned for chat-style
  noise ("的", "了", "是", "the", "a", ...). NOT exhaustive — the goal
  is to drop near-zero-information terms, not to be a linguistic
  reference.

Persistence:
    pickle.dump((tokenized_corpus, raw_corpus, BM25 internals)) — load
    is faster than rebuilding (especially when corpus grows past a few
    thousand docs and jieba init dominates). Pickle of rank_bm25's
    internal arrays is stable across versions for our use case.
"""

from __future__ import annotations

import logging
import pickle
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)

# ── tokenizer setup ────────────────────────────────────────────────────────

try:
    import jieba

    # Suppress jieba's noisy "Building prefix dict ..." log line.
    jieba.setLogLevel(logging.WARNING)
    _HAS_JIEBA = True
except ImportError:  # pragma: no cover — jieba is in requirements
    _HAS_JIEBA = False

# Small chat-tuned stopword set. Intentionally minimal — overzealous
# stopword removal hurts BM25 because rare terms get over-rewarded.
_STOPWORDS_ZH = {
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "們",
    "也", "和", "與", "或", "但", "而", "就", "都", "還", "又",
    "嗎", "呢", "吧", "啊", "喔", "哦", "唉",
    "把", "被", "讓", "給", "對", "向", "從", "到", "於",
    "這", "那", "哪", "什", "麼", "怎", "為", "何",
    "有", "沒", "不", "會", "要", "可", "以", "能",
    "個", "些", "之", "其", "等", "已",
}
_STOPWORDS_EN = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "and", "or", "but", "if", "of", "to", "in", "on", "at", "by", "for",
    "with", "as", "from", "this", "that", "these", "those",
    "i", "you", "he", "she", "it", "we", "they",
    "my", "your", "his", "her", "its", "our", "their",
    "do", "does", "did", "have", "has", "had",
    "will", "would", "can", "could", "should", "may", "might",
}
_STOPWORDS = _STOPWORDS_ZH | _STOPWORDS_EN

# Allow alphanumerics + CJK + a few punctuation we keep for ticker-style
# tokens (e.g. "BRK.B"). Everything else gets stripped.
_TOKEN_KEEP_RE = re.compile(
    r"[一-鿿㐀-䶿A-Za-z0-9.]+",
    flags=re.UNICODE,
)


def _char_bigrams(text: str) -> list[str]:
    """Fallback tokenizer: character bigrams over CJK runs.

    Used when jieba is unavailable. Latin words are kept whole; CJK
    runs become overlapping 2-grams. Trades precision for guaranteed
    recall on Chinese text.
    """
    out: list[str] = []
    for chunk in _TOKEN_KEEP_RE.findall(text.lower()):
        # Latin / digit chunks stay as one token.
        if any(c.isascii() for c in chunk):
            out.append(chunk)
            continue
        # CJK chunk: emit overlapping bigrams. Single-char chunks emit
        # themselves so we don't lose 1-character tokens entirely.
        if len(chunk) == 1:
            out.append(chunk)
        else:
            out.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return out


def tokenize(text: str) -> list[str]:
    """Tokenize a string for BM25 indexing/querying.

    Returns a list of lowercase tokens with stopwords filtered out.
    Empty strings yield an empty list (rank_bm25 handles that fine).
    """
    if not text:
        return []
    text = text.strip()
    if not text:
        return []

    if _HAS_JIEBA:
        # jieba returns a generator of tokens; force lowercase + clean.
        raw_tokens = list(jieba.lcut(text, cut_all=False))
        tokens: list[str] = []
        for t in raw_tokens:
            t = t.strip().lower()
            if not t:
                continue
            # Drop pure punctuation / single whitespace runs.
            if not _TOKEN_KEEP_RE.match(t):
                continue
            if t in _STOPWORDS:
                continue
            tokens.append(t)
        return tokens

    # Fallback: char bigrams
    return [t for t in _char_bigrams(text) if t and t not in _STOPWORDS]


# ── BM25Index ──────────────────────────────────────────────────────────────


class BM25Index:
    """Thin wrapper around rank_bm25.BM25Okapi with persistence + tokenizer.

    Usage:

        idx = BM25Index()
        idx.build(["今天天氣很好", "明天可能下雨", ...])
        hits = idx.query("天氣如何", k=3)
        # -> [(0, 1.42), (1, 0.81), ...]   # (corpus_index, bm25_score)

        idx.save("bm25.pkl")
        idx2 = BM25Index.load("bm25.pkl")

    Notes:
        * `build` is O(N) over the corpus; jieba dominates wall time.
        * Empty corpus is allowed but `query` will then always return [].
        * Scores are raw BM25 (NOT normalized). Hybrid fusion is the
          caller's responsibility.
    """

    def __init__(self) -> None:
        self._bm25: BM25Okapi | None = None
        self._tokenized_corpus: list[list[str]] = []
        self._raw_corpus: list[str] = []

    # ── public API ─────────────────────────────────────────────────

    def build(self, corpus: list[str]) -> None:
        """Build the index from a list of raw document strings.

        Empty / whitespace-only docs are kept (their token list is
        empty) so caller-side indices align 1:1 with `corpus`. That
        invariant matters for hybrid fusion — we need stable doc-ids
        across dense + sparse retrieval.
        """
        self._raw_corpus = list(corpus)
        self._tokenized_corpus = [tokenize(doc) for doc in self._raw_corpus]

        # rank_bm25 chokes on a fully-empty corpus (avgdl divides by 0)
        # and on a corpus where every doc tokenizes to []. Guard both.
        if not self._tokenized_corpus or all(
            not toks for toks in self._tokenized_corpus
        ):
            self._bm25 = None
            return

        # rank_bm25 also chokes on per-doc empty token lists when computing
        # idf; substitute a single sentinel token so the doc just never
        # matches anything (score 0) instead of crashing.
        safe_corpus = [toks if toks else ["__empty__"] for toks in self._tokenized_corpus]
        self._bm25 = BM25Okapi(safe_corpus)

    def query(self, q: str, k: int = 10) -> list[tuple[int, float]]:
        """Return top-k (corpus_index, bm25_score) pairs for the query.

        * If the index is empty or query tokenizes to nothing, returns [].
        * Scores can be 0 or negative-near-zero in pathological cases;
          callers should treat low scores as "no match" rather than
          asserting > 0.
        """
        if self._bm25 is None or not self._tokenized_corpus:
            return []
        tokens = tokenize(q)
        if not tokens:
            return []

        scores = self._bm25.get_scores(tokens)
        # numpy array; argpartition would be faster on huge corpora but
        # we expect at most a few thousand docs.
        ranked = sorted(
            enumerate(scores),
            key=lambda x: x[1],
            reverse=True,
        )
        if k <= 0:
            return []
        return [(int(idx), float(score)) for idx, score in ranked[:k]]

    # ── persistence ────────────────────────────────────────────────

    def save(self, path: str | Path) -> None:
        """Pickle the index to disk."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "raw_corpus": self._raw_corpus,
            "tokenized_corpus": self._tokenized_corpus,
            "bm25": self._bm25,
        }
        with path.open("wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)

    @classmethod
    def load(cls, path: str | Path) -> "BM25Index":
        """Load a previously-saved index."""
        path = Path(path)
        with path.open("rb") as f:
            payload = pickle.load(f)
        idx = cls()
        idx._raw_corpus = payload["raw_corpus"]
        idx._tokenized_corpus = payload["tokenized_corpus"]
        idx._bm25 = payload["bm25"]
        return idx

    # ── introspection helpers ──────────────────────────────────────

    @property
    def corpus_size(self) -> int:
        return len(self._raw_corpus)

    def get_doc(self, idx: int) -> str:
        return self._raw_corpus[idx]


# ── manual demo (run as `python bm25_index.py`) ────────────────────────────

if __name__ == "__main__":  # pragma: no cover — demo only
    docs = [
        "今天天氣很好,適合出去玩",
        "明天可能會下雨,記得帶傘",
        "AAPL 股價今天上漲 2%",
        "週末家庭聚餐在內湖餐廳",
        "提醒下週一要去看牙醫",
    ]
    idx = BM25Index()
    idx.build(docs)
    print("Query: 今天天氣如何")
    for i, score in idx.query("今天天氣如何", k=3):
        print(f"  [{i}] score={score:.4f}  {docs[i]}")
