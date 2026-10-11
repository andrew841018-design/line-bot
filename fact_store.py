"""fact_store — 家人記憶的共用寫入路徑（2026-10-11 每日記憶）。

Andrew 2026-10-11：「記得後續，要自動累積這個記憶，從每日的訊息」。即時抽取
（main._extract_facts_now，source="inline"）和每日 job（jobs/daily_memory.py，
source="daily"）都經過這裡：正規化 → 過濾（fact_filters）→ /忘記 的 tombstone →
語意去重 → 每人上限 → memory.add_fact → ledger（只存 HMAC）。只新增，不改、不刪舊記憶。

鎖：fcntl 鎖 locks/fact_store.lock（目錄可用 LINE_BOT_FACT_LOCK_DIR 覆寫），包住
「讀現有事實 → 去重 → 上限 → 寫入 → ledger」，兩個程序不會一起繞過上限或去重。
回覆路徑用 lock_mode="try"（拿不到就整批略過，絕不等）；job 用 "wait"（最多 10 秒）。
嵌入模型只在拿鎖之前用，持鎖時絕不呼叫模型。

Report 只有數量（log 與 outcome 不能有事實文字或名字）；dry_run 才附每條的決定給預覽用，
而且只有 add、dup 的那幾條帶文字（被擋下的健康、聯絡資料不留第二份明文）。
"""
from __future__ import annotations

import collections
import contextlib
import fcntl
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import fact_filters
import fact_hash
import memory
from config import settings
from speaker_turns import UNKNOWN_SPEAKER

logger = logging.getLogger(__name__)

_BASE = Path(__file__).resolve().parent
LEDGER_PATH_ENV = "LINE_BOT_FACT_LEDGER_PATH"
LOCK_DIR_ENV = "LINE_BOT_FACT_LOCK_DIR"
PER_PERSON_MAX_ENV = "LINE_BOT_FACTS_PER_PERSON_MAX"
DEDUPE_HIGH_ENV = "LINE_BOT_FACT_DEDUPE_HIGH"
DEDUPE_LOW_ENV = "LINE_BOT_FACT_DEDUPE_LOW"

DEFAULT_PER_PERSON_MAX = 60
# MiniLM（paraphrase-multilingual-MiniLM-L12-v2）的中文分數偏擠，門檻用現有記憶調出來的。
FACT_DEDUPE_HIGH = 0.83
FACT_DEDUPE_LOW = 0.75
CONTAINMENT_MIN = 0.8
LOCK_WAIT_SEC = 10.0
_LOCK_POLL_SEC = 0.05
# 只有一邊有否定詞就不算重複：「不吃牛肉」和「喜歡吃牛肉」分數可能很高，卻是更正。
_NEGATION_RE = re.compile(r"不再|不|沒|別|戒")
# 替換守門（2026-10-11 修正第一輪 #1）：兩邊各有自己獨有的字、各 ≤ 2 個、長度差 ≤ 1，
# 就是把地點或日子換掉的更正（住台中→住台南、週日→週六），不是重複。
SUBSTITUTION_MAX_CHARS = 2
# 但同義改寫（喜歡↔愛、去↔上）也長這樣：MiniLM 實測同義 0.968–0.977，換地點／日子／東西
# 0.820–0.911（台南↔台中 0.911、週六↔週日 0.890、牛肉↔豬肉 0.899）。有模型時 ≥ 這個值才算重複；
# 沒有模型分不出來，一律當更正（寧可多一條，不吞更正）。
SUBSTITUTION_SYNONYM_MIN = 0.95
_EMBED_CACHE_MAX = 2048


class EmbedderUnavailable(RuntimeError):
    """job（lock_mode="wait"）的去重模型壞了：整批不寫（fail closed）。"""


@dataclass(frozen=True)
class _Candidate:
    label: str
    user_id: str
    body: str


@dataclass
class Report:
    """一批事實的處理結果：只有數量；dry_run 時 decisions 列每條的決定（給預覽）。"""

    added: int = 0
    dup: int = 0
    tombstoned: int = 0
    capped: int = 0
    locked: int = 0
    error: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    decisions: list[dict[str, str]] | None = None

    def counts(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "dup": self.dup,
            "tombstoned": self.tombstoned,
            "capped": self.capped,
            "locked": self.locked,
            "error": self.error,
            "rejected": dict(sorted(self.rejected.items())),
        }

    def _reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1
        if self.decisions is not None:
            self.decisions.append({"decision": "rejected", "category": reason})

    def _decide(self, label: str, body: str, decision: str) -> None:
        """預覽只有 add、dup 帶稱呼和文字；其他決定只記決定本身（2026-10-11 修正第一輪 #6）。"""
        if self.decisions is None:
            return
        if decision in ("add", "dup"):
            self.decisions.append({"decision": decision, "person": label, "fact": body})
        else:
            self.decisions.append({"decision": decision})


# ── 設定 ────────────────────────────────────────────────────────────────────


def _setting(env_name: str, attr: str) -> str:
    """環境變數優先，否則 settings。兩個程序都會把 .env 載入環境變數（main.py 匯入時、
    job 開頭都 load_dotenv，不覆蓋已設的值），所以改 .env 要重啟 uvicorn 才生效。"""
    raw = os.environ.get(env_name)
    if raw is None:
        raw = getattr(settings, attr, "")
    return str(raw or "").strip()


def per_person_max() -> int:
    """每人最多幾條事實（含 /記住 寫的）；LINE_BOT_FACTS_PER_PERSON_MAX，打錯就用預設 60。"""
    raw = _setting(PER_PERSON_MAX_ENV, "facts_per_person_max_raw")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_PER_PERSON_MAX
    return value if value > 0 else DEFAULT_PER_PERSON_MAX


def _threshold(env_name: str, default: float) -> float:
    try:
        value = float(os.environ.get(env_name, ""))
    except ValueError:
        return default
    return value if 0.0 < value <= 1.0 else default


def _ledger_path() -> Path:
    return Path(os.environ.get(LEDGER_PATH_ENV) or _BASE / "state" / "fact_ledger.jsonl")


def _lock_dir() -> Path:
    return Path(os.environ.get(LOCK_DIR_ENV) or _BASE / "locks")


def loaded_embedder() -> Any | None:
    """即時抽取用：MiniLM 已經載入才用，絕不在回覆前冷載入（GP1 #12 d）。"""
    try:
        import grounding_local
    except Exception:
        return None
    return grounding_local._st_model


# ── 正規化與去重 ────────────────────────────────────────────────────────────


def normalize_fact(fact: str, label: str = "") -> str:
    """NFKC、一行；開頭是這個人自己的稱呼或「不確定是誰」才去掉「X：」（「興趣：爬山」照留）；
    去句尾的。．.！!；;。"""
    text = fact_hash.normalize_text(fact)
    own = {unicodedata.normalize("NFKC", name) for name in (label, UNKNOWN_SPEAKER, "不確定是誰") if name}
    for name in sorted(own, key=len, reverse=True):
        for sep in (":", "："):
            if text.startswith(name + sep):
                return fact_hash.normalize_text(text[len(name) + len(sep):])
    return text


def _negated(text: str) -> bool:
    return _NEGATION_RE.search(text) is not None


def _letters(text: str) -> str:
    """替換守門用：NFKC、去格式字元、去空白和標點（Unicode P 類別）、不分大小寫。"""
    return "".join(ch for ch in fact_hash.canonical(text) if unicodedata.category(ch)[0] != "P")


def _substituted(a: str, b: str) -> bool:
    """兩邊各有自己獨有的字（各 ≤ 2 個）、長度差 ≤ 1：是換了內容的更正，不是重複。"""
    mine, theirs = _letters(a), _letters(b)
    only_mine = collections.Counter(mine) - collections.Counter(theirs)
    only_theirs = collections.Counter(theirs) - collections.Counter(mine)
    return (
        bool(only_mine)
        and bool(only_theirs)
        and sum(only_mine.values()) <= SUBSTITUTION_MAX_CHARS
        and sum(only_theirs.values()) <= SUBSTITUTION_MAX_CHARS
        and abs(len(mine) - len(theirs)) <= 1
    )


def _containment(a: str, b: str) -> float:
    """較短那條的字元有幾成出現在較長那條（重複的字照次數算）。"""
    short, long_ = sorted((a, b), key=len)
    if not short:
        return 0.0
    common = collections.Counter(short) & collections.Counter(long_)
    return sum(common.values()) / len(short)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _is_duplicate(
    body: str,
    others: Iterable[str],
    vectors: dict[str, tuple[float, ...]],
    *,
    use_model: bool,
    high: float,
    low: float,
) -> bool:
    """同一人的事實本體之間：完全相同；有模型時 cos ≥ high，或 cos ≥ low 且字元包含 ≥ 80%；
    沒有模型（或這一條沒有向量）時只看完全相同和字元包含。只有一邊有否定詞、或是替換
    （_substituted）就不算重複；兩道守門都在相似度之前，兩條路徑都套用。"""
    mine = fact_hash.canonical(body)
    for other in others:
        theirs = fact_hash.canonical(other)
        if not theirs:
            continue
        if mine == theirs:
            return True
        if _negated(mine) != _negated(theirs):
            continue
        va, vb = vectors.get(mine), vectors.get(theirs)
        cos = _cosine(va, vb) if use_model and va is not None and vb is not None else None
        if _substituted(mine, theirs):
            if cos is not None and cos >= SUBSTITUTION_SYNONYM_MIN:
                return True  # 同義改寫
            continue
        contained = _containment(mine, theirs)
        if cos is not None:
            if cos >= high or (cos >= low and contained >= CONTAINMENT_MIN):
                return True
            continue
        if contained >= CONTAINMENT_MIN:
            return True
    return False


# 每條事實的向量快取（MiniLM 編過的不再編；回覆前最多 60 條既有事實）。換了模型物件就清掉。
_embed_cache: "collections.OrderedDict[str, tuple[float, ...]]" = collections.OrderedDict()
_embed_cache_owner: Any = None
_embed_cache_lock = threading.Lock()


def _unit(vector: Iterable[float]) -> tuple[float, ...]:
    values = tuple(float(x) for x in vector)
    norm = math.sqrt(sum(x * x for x in values))
    return tuple(x / norm for x in values) if norm else values


def _encode(embedder: Any, texts: Iterable[str]) -> dict[str, tuple[float, ...]]:
    global _embed_cache_owner
    by_key: dict[str, str] = {}
    for text in texts:
        key = fact_hash.canonical(text)
        if key:
            by_key.setdefault(key, text)
    found: dict[str, tuple[float, ...]] = {}
    with _embed_cache_lock:
        if _embed_cache_owner is not embedder:
            _embed_cache.clear()
            _embed_cache_owner = embedder
        for key in by_key:
            if key in _embed_cache:
                _embed_cache.move_to_end(key)
                found[key] = _embed_cache[key]
    missing = [key for key in by_key if key not in found]
    if missing:
        rows = embedder.encode(
            [by_key[key] for key in missing],
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        with _embed_cache_lock:
            for key, row in zip(missing, rows):
                vector = _unit(row)
                found[key] = vector
                if _embed_cache_owner is embedder:
                    _embed_cache[key] = vector
            while len(_embed_cache) > _EMBED_CACHE_MAX:
                _embed_cache.popitem(last=False)
    return found


def _person_bodies(rows: Iterable[tuple[str, str]], user_ids: set[str]) -> dict[str, list[str]]:
    bodies: dict[str, list[str]] = {}
    for user_id, fact in rows:
        if user_id in user_ids:
            bodies.setdefault(user_id, []).append(fact_hash.normalize_text(memory.fact_body(fact)))
    return bodies


# ── 鎖與 ledger ─────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _store_lock(mode: str) -> Iterator[bool]:
    """yield 有沒有拿到鎖；"try" 只試一次，"wait" 最多等 LOCK_WAIT_SEC。"""
    directory = _lock_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / "fact_store.lock", os.O_RDWR | os.O_CREAT, 0o600)
    held = False
    try:
        deadline = time.monotonic() + (LOCK_WAIT_SEC if mode == "wait" else 0.0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_LOCK_POLL_SEC)
        yield held
    finally:
        if held:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _append_ledger(source: str, group_id: str, user_id: str, stored: str, key: bytes) -> None:
    """一行一次 write()、O_APPEND、從建立起就是 0600；只記 HMAC，不記事實原文。"""
    entry = {
        "ts": int(time.time()),
        "source": source,
        "group_id": group_id,
        "user_id": user_id,
        "fact_hmac": fact_hash.fact_hmac(group_id, user_id, stored, key=key),
        "db_path": str(memory._DB_PATH),
    }
    line = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    path = _ledger_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if os.write(fd, line) != len(line):
            raise OSError("short ledger write")
    finally:
        os.close(fd)


# ── 對外 ─────────────────────────────────────────────────────────────────────


def add_person_facts(
    group_id: str,
    items: Iterable[tuple[str, str, str]],
    *,
    source: str,
    embedder: Any,
    lock_mode: str,
    dry_run: bool = False,
    require_names: bool = False,
) -> Report:
    """依序處理 [(稱呼, user_id, 事實)]，回傳 Report（只有數量）。

    embedder=None 只做完全相同與字元包含（短句同義靠這個抓）；有 embedder 時也比 MiniLM
    cos。require_names=True（job）時本機名單讀不到就丟 fact_filters.NameSourceUnavailable，
    什麼都不寫。dry_run=True 做完全部判斷但不寫 DB、不寫 ledger，decisions 列每條的決定。
    寫入一律透過 memory.add_fact（測試 mock 得到）。HMAC 金鑰由 memory.fact_hash_key()
    給，金鑰檔不見或被換掉時丟 fact_hash.FactHashKeyMismatch，整批不寫；job 的去重模型壞掉
    丟 EmbedderUnavailable。
    """
    if lock_mode not in ("try", "wait"):
        raise ValueError(f"unknown lock mode {lock_mode!r}")
    report = Report(decisions=[] if dry_run else None)
    items = list(items)
    if not items:
        return report
    names = fact_filters.load_name_terms(require_names=require_names)
    candidates: list[_Candidate] = []
    for label, user_id, fact in items:
        label, user_id = str(label or "").strip(), str(user_id or "").strip()
        body = normalize_fact(fact, label)
        if not label or not user_id or user_id == "__bot__":
            report._reject("speaker")
            continue
        if fact_filters.has_contact(label):
            # 存的是「稱呼：事實」整行，回覆的聯絡資料關卡也看整行：顯示名稱帶電話或地址就不存
            report._reject("label_contact")
            continue
        reason = fact_filters.reject_reason(body, names=names)
        if reason:
            report._reject(reason)
            continue
        candidates.append(_Candidate(label, user_id, body))
    if not candidates:
        return report

    users = {c.user_id for c in candidates}
    vectors: dict[str, tuple[float, ...]] = {}
    if embedder is not None:
        known = _person_bodies(memory.list_fact_rows(group_id), users)
        texts = [c.body for c in candidates] + [body for bodies in known.values() for body in bodies]
        try:
            vectors = _encode(embedder, texts)
        except Exception as exc:
            if lock_mode == "wait":
                # job：沒有模型就整批不寫（fail closed），outcome 寫 embedder_unavailable
                raise EmbedderUnavailable(type(exc).__name__) from None
            # 回覆路徑量小：退回只比完全相同與字元包含
            logger.warning("fact dedupe embedder failed error_type=%s", type(exc).__name__)
            embedder = None
    key = memory.fact_hash_key()  # 金鑰被換掉時丟 FactHashKeyMismatch：整批不寫
    high = _threshold(DEDUPE_HIGH_ENV, FACT_DEDUPE_HIGH)
    low = _threshold(DEDUPE_LOW_ENV, FACT_DEDUPE_LOW)
    cap = per_person_max()

    with _store_lock(lock_mode) as held:
        if not held:
            report.locked += len(candidates)
            for c in candidates:
                report._decide(c.label, c.body, "locked")
            return report
        existing = _person_bodies(memory.list_fact_rows(group_id), users)
        pending: dict[str, int] = {}
        for c in candidates:
            if memory.is_fact_tombstoned(group_id, c.user_id, c.body, key=key):
                report.tombstoned += 1
                report._decide(c.label, c.body, "tombstoned")
                continue
            others = existing.setdefault(c.user_id, [])
            if _is_duplicate(c.body, others, vectors, use_model=embedder is not None, high=high, low=low):
                report.dup += 1
                report._decide(c.label, c.body, "dup")
                continue
            if memory.count_person_facts(group_id, c.user_id) + pending.get(c.user_id, 0) >= cap:
                report.capped += 1
                report._decide(c.label, c.body, "capped")
                continue
            if dry_run:
                pending[c.user_id] = pending.get(c.user_id, 0) + 1
                others.append(c.body)
                report.added += 1
                report._decide(c.label, c.body, "add")
                continue
            stored = memory.one_line(f"{c.label}：{c.body}")
            if not memory.add_fact(group_id, stored, user_id=c.user_id):
                report.dup += 1
                continue
            try:
                _append_ledger(source, group_id, c.user_id, stored, key)
            except Exception as exc:
                # 每一條自動加的事實都要在 ledger 裡（回滾靠它）：寫不進去就把剛寫的那列刪掉。
                logger.warning("fact ledger append failed error_type=%s", type(exc).__name__)
                try:
                    memory.delete_fact_row(group_id, c.user_id, stored)
                except Exception as undo_exc:
                    logger.error("fact ledger rollback failed error_type=%s", type(undo_exc).__name__)
                report.error += 1
                continue
            others.append(c.body)
            report.added += 1
    return report
