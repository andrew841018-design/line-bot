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
而且只有 add、dup、upgrade 的那幾條帶文字（被擋下的健康、聯絡資料不留第二份明文）。

2026-10-16 轉述（Andrew：A 講到家人 B 的事記到 B 名下、標「（A說的）」）：只有 job 會給
FactItem.said_by、開 allow_hearsay／allow_upgrade，回覆路徑照舊只新增本人的事實。
- 轉述另過 hearsay.scope_reason（只收住、工作上學、喜好、興趣、寵物、在學什麼），標記只由這裡加。
- 去重、過濾、tombstone 一律看去掉標記的本體；轉述和 B 現有任何一條重複就不加（B 本人的版本優先）。
- B 後來自己說了同一件事：那條轉述原地改成 B 的說法（只改 ledger 確認過的轉述列，GP2 S2）。
- 轉述每人最多 HEARSAY_PER_PERSON_MAX 條、同一對（說的人, 對象）最多 HEARSAY_PER_PAIR_MAX 條。
- ledger 多記 kind（self／hearsay／upgrade）與 backend；source 不變（ROLLBACK 照 source 刪）。
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
from typing import Any, Iterable, Iterator, NamedTuple, Sequence

import fact_filters
import fact_hash
import hearsay
import memory
from config import settings
from member_labels import _clean_member_label
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
# 轉述的子上限（2026-10-16 審查 GP1#6、GP2）：一位話多的家人不能用轉述把 B 的 60 條填滿。
HEARSAY_PER_PERSON_MAX = 10
HEARSAY_PER_PAIR_MAX = 5


class EmbedderUnavailable(RuntimeError):
    """job（lock_mode="wait"）的去重模型壞了：整批不寫（fail closed）。"""


class FactItem(NamedTuple):
    """一條要寫的事實：記在 label／user_id 名下；said_by 非空就是轉述（2026-10-16，只有 job 會給）。
    舊的三元組 (稱呼, user_id, 事實) 照樣收。"""

    label: str
    user_id: str
    fact: str
    said_by: str = ""


@dataclass(frozen=True)
class _Candidate:
    label: str
    user_id: str
    body: str
    said_by: str = ""


@dataclass
class _Existing:
    """這個人現有的一列：plain＝去掉標記、正規化後的本體；raw＝資料庫裡的原文；said_by＝標記上的人；
    hearsay＝ledger 確認過的轉述（只有這種會被升級；/記住 打出來的同樣寫法算本人的列，GP2 S2／D2）。"""

    plain: str
    raw: str
    said_by: str = ""
    hearsay: bool = False


@dataclass
class Report:
    """一批事實的處理結果：只有數量；dry_run 時 decisions 列每條的決定（給預覽）。"""

    added: int = 0
    dup: int = 0
    tombstoned: int = 0
    capped: int = 0
    locked: int = 0
    error: int = 0
    upgraded: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    decisions: list[dict[str, str]] | None = None

    def counts(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "dup": self.dup,
            "upgraded": self.upgraded,
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

    def _decide(self, label: str, body: str, decision: str, said_by: str = "") -> None:
        """預覽只有 add、dup、upgrade 帶稱呼和文字（轉述另帶 said_by）；其他決定只記決定本身
        （2026-10-11 修正第一輪 #6）。"""
        if self.decisions is None:
            return
        if decision in ("add", "dup", "upgrade"):
            entry = {"decision": decision, "person": label, "fact": body}
            if said_by:
                entry["said_by"] = said_by
            self.decisions.append(entry)
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
    return _duplicate_index(body, others, vectors, use_model=use_model, high=high, low=low) is not None


def _duplicate_index(
    body: str,
    others: Iterable[str],
    vectors: dict[str, tuple[float, ...]],
    *,
    use_model: bool,
    high: float,
    low: float,
) -> int | None:
    """同一人的事實本體之間：完全相同；有模型時 cos ≥ high，或 cos ≥ low 且字元包含 ≥ 80%；
    沒有模型（或這一條沒有向量）時只看完全相同和字元包含。只有一邊有否定詞、或是替換
    （_substituted）就不算重複；兩道守門都在相似度之前，兩條路徑都套用。"""
    mine = fact_hash.canonical(body)
    for index, other in enumerate(others):
        theirs = fact_hash.canonical(other)
        if not theirs:
            continue
        if mine == theirs:
            return index
        if _negated(mine) != _negated(theirs):
            continue
        va, vb = vectors.get(mine), vectors.get(theirs)
        cos = _cosine(va, vb) if use_model and va is not None and vb is not None else None
        if _substituted(mine, theirs):
            if cos is not None and cos >= SUBSTITUTION_SYNONYM_MIN:
                return index  # 同義改寫
            continue
        contained = _containment(mine, theirs)
        if cos is not None:
            if cos >= high or (cos >= low and contained >= CONTAINMENT_MIN):
                return index
            continue
        if contained >= CONTAINMENT_MIN:
            return index
    return None


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


def _plain(fact: str) -> tuple[str, str]:
    """資料庫原文 → (去掉轉述標記、正規化後的本體, 標記上的人)。標記一定在 NFKC 之前拆（GP1#1）。"""
    plain, said_by = hearsay.split_tag(memory.fact_body(fact))
    return fact_hash.normalize_text(plain), said_by


def _person_bodies(rows: Iterable[tuple[str, str]], user_ids: set[str]) -> dict[str, list[str]]:
    bodies: dict[str, list[str]] = {}
    for user_id, fact in rows:
        if user_id in user_ids:
            bodies.setdefault(user_id, []).append(_plain(fact)[0])
    return bodies


def _person_rows(
    rows: Iterable[tuple[str, str]], user_ids: set[str], group_id: str, verified: set[str], key: bytes
) -> dict[str, list[_Existing]]:
    found: dict[str, list[_Existing]] = {}
    for user_id, fact in rows:
        if user_id not in user_ids:
            continue
        plain, said_by = _plain(fact)
        confirmed = bool(said_by) and fact_hash.fact_hmac(group_id, user_id, fact, key=key) in verified
        found.setdefault(user_id, []).append(_Existing(plain, fact, said_by, confirmed))
    return found


def _ledger_hearsay_hmacs(group_id: str) -> set[str] | None:
    """ledger 裡記成轉述（kind=hearsay）的列的 HMAC；讀不到回 None——那就一列都不升級（fail closed）。"""
    path = _ledger_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except (OSError, UnicodeError):
        return None
    found: set[str] = set()
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("kind") == "hearsay" and entry.get("group_id") == group_id:
            value = entry.get("fact_hmac")
            if isinstance(value, str):
                found.add(value)
    return found


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


def _append_ledger(
    source: str,
    group_id: str,
    user_id: str,
    stored: str,
    key: bytes,
    *,
    kind: str = "self",
    backend: str = "",
    replaces: str = "",
) -> None:
    """一行一次 write()、O_APPEND、從建立起就是 0600；只記 HMAC，不記事實原文。

    2026-10-16：kind（self／hearsay／upgrade）、backend（claude／gemini／空）方便回溯；升級另記
    被取代那一列的 HMAC（replaces_hmac）。source 照舊，回滾照 source 刪的流程不變。"""
    entry = {
        "ts": int(time.time()),
        "source": source,
        "kind": kind,
        "backend": backend,
        "group_id": group_id,
        "user_id": user_id,
        "fact_hmac": fact_hash.fact_hmac(group_id, user_id, stored, key=key),
        "db_path": str(memory._DB_PATH),
    }
    if replaces:
        entry["replaces_hmac"] = replaces
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
    items: Iterable[Sequence[str]],
    *,
    source: str,
    embedder: Any,
    lock_mode: str,
    dry_run: bool = False,
    require_names: bool = False,
    allow_hearsay: bool = False,
    allow_upgrade: bool = False,
    backend: str = "",
) -> Report:
    """依序處理 [(稱呼, user_id, 事實[, 誰說的])]（FactItem 或三元組），回傳 Report（只有數量）。

    embedder=None 只做完全相同與字元包含（短句同義靠這個抓）；有 embedder 時也比 MiniLM
    cos。require_names=True（job）時本機名單讀不到就丟 fact_filters.NameSourceUnavailable，
    什麼都不寫。dry_run=True 做完全部判斷但不寫 DB、不寫 ledger，decisions 列每條的決定。
    寫入一律透過 memory.add_fact（測試 mock 得到）。HMAC 金鑰由 memory.fact_hash_key()
    給，金鑰檔不見或被換掉時丟 fact_hash.FactHashKeyMismatch，整批不寫；job 的去重模型壞掉
    丟 EmbedderUnavailable。

    2026-10-16：allow_hearsay（job 才開）才收帶「誰說的」的轉述；allow_upgrade（job 才開）才會把
    ledger 確認過的轉述列改成本人的說法。回覆路徑兩個都不開：照舊只新增。
    """
    if lock_mode not in ("try", "wait"):
        raise ValueError(f"unknown lock mode {lock_mode!r}")
    report = Report(decisions=[] if dry_run else None)
    items = [FactItem(*item) for item in items]
    if not items:
        return report
    names = fact_filters.load_name_terms(require_names=require_names)
    candidates: list[_Candidate] = []
    for label, user_id, fact, said_by in items:
        label, user_id = str(label or "").strip(), str(user_id or "").strip()
        said_by = str(said_by or "").strip()
        body = normalize_fact(fact, label)
        if not label or not user_id or user_id == "__bot__":
            report._reject("speaker")
            continue
        if fact_filters.has_contact(label):
            # 存的是「稱呼：事實」整行，回覆的聯絡資料關卡也看整行：顯示名稱帶電話或地址就不存
            report._reject("label_contact")
            continue
        if said_by:
            if not allow_hearsay:
                report._reject("hearsay_off")
                continue
            if _clean_member_label(said_by) != said_by or said_by == label or fact_filters.has_contact(said_by):
                report._reject("said_by")
                continue
        if hearsay.has_tag(body):
            # 「（X說的）」只能由這裡加：模型給的本體自己帶這種結尾就不收（NFKC 後只會是半形）
            report._reject("said_tag")
            continue
        reason = fact_filters.reject_reason(body, names=names)
        if not reason and said_by:
            reason = hearsay.scope_reason(body)
        if reason:
            report._reject(reason)
            continue
        candidates.append(_Candidate(label, user_id, body, said_by))
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
    use_model = embedder is not None

    def duplicate(body: str, others: list[str]) -> int | None:
        return _duplicate_index(body, others, vectors, use_model=use_model, high=high, low=low)

    with _store_lock(lock_mode) as held:
        if not held:
            report.locked += len(candidates)
            for c in candidates:
                report._decide(c.label, c.body, "locked")
            return report
        rows = memory.list_fact_rows(group_id)
        tagged = allow_upgrade and any(uid in users and memory.is_hearsay_row(fact) for uid, fact in rows)
        verified = (_ledger_hearsay_hmacs(group_id) or set()) if tagged else set()
        existing = _person_rows(rows, users, group_id, verified, key)
        pending: dict[str, int] = {}
        for c in candidates:
            if memory.is_fact_tombstoned(group_id, c.user_id, c.body, key=key, include_hearsay=bool(c.said_by)):
                report.tombstoned += 1
                report._decide(c.label, c.body, "tombstoned")
                continue
            entries = existing.setdefault(c.user_id, [])
            if c.said_by:
                # 轉述：和 B 現有任何一條（本人的或別的轉述）重複就不加——B 本人的版本優先
                if duplicate(c.body, [e.plain for e in entries]) is not None:
                    report.dup += 1
                    report._decide(c.label, c.body, "dup", c.said_by)
                    continue
            else:
                if duplicate(c.body, [e.plain for e in entries if not e.hearsay]) is not None:
                    report.dup += 1
                    report._decide(c.label, c.body, "dup")
                    continue
                # 只有 allow_upgrade 才看 ledger 確認過的轉述列（這一批剛加的轉述也一樣，R2 GP1 N3）
                told = [e for e in entries if e.hearsay] if allow_upgrade else []
                index = duplicate(c.body, [e.plain for e in told])
                if index is not None:
                    _upgrade(report, group_id, c, told[index], source, key, backend, dry_run=dry_run)
                    continue
            if memory.count_person_facts(group_id, c.user_id) + pending.get(c.user_id, 0) >= cap:
                report.capped += 1
                report._decide(c.label, c.body, "capped")
                continue
            if c.said_by and (
                sum(1 for e in entries if e.said_by) >= HEARSAY_PER_PERSON_MAX
                or sum(1 for e in entries if e.said_by == c.said_by) >= HEARSAY_PER_PAIR_MAX
            ):
                report.capped += 1
                report._decide(c.label, c.body, "capped")
                continue
            if dry_run:
                pending[c.user_id] = pending.get(c.user_id, 0) + 1
                entries.append(_Existing(c.body, "", c.said_by, bool(c.said_by) and allow_upgrade))
                report.added += 1
                report._decide(c.label, c.body, "add", c.said_by)
                continue
            stored = memory.one_line(f"{c.label}：{c.body}{hearsay.tag(c.said_by) if c.said_by else ''}")
            if not memory.add_fact(group_id, stored, user_id=c.user_id):
                report.dup += 1
                continue
            try:
                _append_ledger(
                    source, group_id, c.user_id, stored, key,
                    kind="hearsay" if c.said_by else "self", backend=backend,
                )
            except Exception as exc:
                # 每一條自動加的事實都要在 ledger 裡（回滾靠它）：寫不進去就把剛寫的那列刪掉。
                logger.warning("fact ledger append failed error_type=%s", type(exc).__name__)
                try:
                    memory.delete_fact_row(group_id, c.user_id, stored)
                except Exception as undo_exc:
                    logger.error("fact ledger rollback failed error_type=%s", type(undo_exc).__name__)
                report.error += 1
                continue
            entries.append(_Existing(c.body, stored, c.said_by, bool(c.said_by) and allow_upgrade))
            report.added += 1
    return report


def _upgrade(
    report: Report,
    group_id: str,
    c: _Candidate,
    target: _Existing,
    source: str,
    key: bytes,
    backend: str,
    *,
    dry_run: bool,
) -> None:
    """B 自己說了一條 ledger 確認過的轉述：那一列原地改成 B 的說法（「B 本人說過的版本優先」）。

    那一列剛被 /忘記（rowcount 0）或新文字已存在（主鍵衝突）都算 dup：不寫 ledger，也不改成新增——
    新增會把剛忘記的事加回來（GP1#3）。ledger 寫不進去就改回原文。
    """
    if dry_run:
        target.plain, target.raw, target.said_by, target.hearsay = c.body, "", "", False
        report.upgraded += 1
        report._decide(c.label, c.body, "upgrade")
        return
    stored = memory.one_line(f"{c.label}：{c.body}")
    if not memory.promote_hearsay_row(group_id, c.user_id, target.raw, stored):
        report.dup += 1
        return
    try:
        _append_ledger(
            source, group_id, c.user_id, stored, key, kind="upgrade", backend=backend,
            replaces=fact_hash.fact_hmac(group_id, c.user_id, target.raw, key=key),
        )
    except Exception as exc:
        logger.warning("fact ledger append failed error_type=%s", type(exc).__name__)
        try:
            memory.restore_fact_row(group_id, c.user_id, stored, target.raw)
        except Exception as undo_exc:
            logger.error("fact ledger rollback failed error_type=%s", type(undo_exc).__name__)
        report.error += 1
        return
    target.plain, target.raw, target.said_by, target.hearsay = c.body, stored, "", False
    report.upgraded += 1
