"""股票推薦回測（計畫 B，2026-10-29）A 段：封存、三族池子紀錄、B1 spool 匯入（格式定案前的佔位）。

規格：ops/state/line_bot_stock_backtest_20261028/PLAN.md（v5）§4.2、§4.3。

封存（只用標準庫：bot 程式、review.sqlite 壞掉都照樣封存）
- 三個狀態檔（主快照、AI 狀態、美股快照）每輪只讀一次：O_NOFOLLOW、只收自己的一般檔、有大小上限。
  同一份 bytes 拿去算鍵、gzip，解析後的物件再交給池子紀錄，不讀第二次。
- 鍵＝「影響推薦的欄位」的 sha256（Source.fields）：AI 狀態的 last_try、美股快照的 latest_closes 這類一直在變、
  卻不影響推薦的欄位，不會讓封存一直多存。整份原檔存成 archive/YYYY/<名>-<T>-<sha8>.json.gz；同名已在就不再寫。
  保留 3 年（看檔名裡的 T）。
- `--import-archive DIR`：一次性匯入手動封存（stock_picks_tw.json.<ts>.gz 這類檔名），進封存，也記池子紀錄。

池子紀錄（review.sqlite 的 pool_days／pool_rows；冪等鍵 (m, rule, t, ver_s, derive_version)，ver_s＝round(computed_at)）
- tw/v1（舊規則，對照組）：主快照 universe 的 50 檔（pool 多放的常問股 0050 不算成員）；flags＝checks_of、
  values＝_facts_json，走勢就是主池的；c0＝listed 收盤。
- tw/ai_*（簡版）：成員＝AI 狀態的 universe.codes，配同一個 T 的主快照（computed_at 不晚於 AI 那一版的最新一份；
  現行的對不上就從封存找）。c0＝listed 收盤（每個成員都有，不靠 tr）；values＝_facts_json(_facts_for(…))，
  flags＝ai_checks_of（條件 1–3；走勢不知道就是 null）。走勢只記主池有的（pool）與入選的（ok＋picks 的 tr），
  其他 null，不推論。新的一版先等 10 分鐘寬限（spool 可能還在路上）；那一版已經有 spool 就不記簡版。
  寬限內就被下一版蓋掉的那一版，之後從封存（最近 7 天）補記。
- us/us_*：美股快照的 pool；flags＝us_checks_of、values＝美股的 _facts_json；c0＝DB 的 D 收盤，沒有就用快照
  latest_closes 裡日期等於 D 的那筆。
- 只有 spool 可以取代同一個鍵的簡版（src='simple'），其他一律不覆寫。tw/v1 與美股在 A 段也記成 simple
  （PLAN §3 的 src 只有 spool／simple；舊規則永遠不會有 spool，美股 B2 起才有）。
- 真的拿不到收盤的成員（理論上不會有）不編數字：c0 留空、status＝excluded:no_close（review.sqlite 的 CHECK 也擋
  「成員卻沒有 c0」）。

bot 的模組（stock_picks、stock_picks_us、stock_db_ro）只在函式裡 import，只呼叫純函式（解析、條件、
_facts_json／_facts_for）。這個模組 import 時不做任何 I/O。
"""

from __future__ import annotations

import gzip
import hashlib
import importlib
import json
import math
import os
import re
import stat
import time
import zlib
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

TPE = ZoneInfo("Asia/Taipei")

DERIVE_VERSION = 1  # 池子紀錄的推導版本：推導方式改了就升，從封存重算時新舊版本並存
STATE_MAX_BYTES = 4 * 1024 * 1024  # bot 自己的上限是 2 MB（stock_picks.STATE_MAX_BYTES）
ARCHIVE_KEEP_DAYS = 3 * 365 + 1
ARCHIVE_GZ_MAX_BYTES = 8 * 1024 * 1024  # 封存／匯入的 gz（壓縮後）上限
ARCHIVE_RAW_MAX_BYTES = 16 * 1024 * 1024  # 解壓後上限（擋 gzip 炸彈）
AI_GRACE_S = 600.0  # 新的 AI 版本先等 10 分鐘，spool 晚到也不會被簡版搶先（PLAN §4.3、辯論 r3）
AI_LOOKBACK_DAYS = 7  # 封存裡最近幾天的 AI 狀態：寬限內被下一版蓋掉的那一版，之後從封存補記
TMP_MAX_AGE_S = 3600.0
SPOOL_MAX_BYTES = 1024 * 1024
SPOOL_KEEP_DAYS = 30
SPOOL_UNMATCHED_GRACE_S = 86400.0  # 對不上任何已 commit 版本的 spool：留一天再移走（部署重疊時的另一份）
SPOOL_MAX_FILES = 500

TW_OLD_RULE = "v1"  # 舊規則沒有 rule 欄位（和 stock_picks_perf.TW_OLD_RULE 同字）
MEMBER = "member"
NO_CLOSE = "excluded:no_close"
NO_FACTS = "excluded:no_facts"
SIMPLE, SPOOL = "simple", "spool"
FAMILY_V1, FAMILY_AI, FAMILY_US = "tw/v1", "tw/ai", "us"


@dataclass(frozen=True)
class Source:
    kind: str  # tw｜tw_ai｜us
    name: str  # 封存檔名的前綴
    filename: str  # bot 的狀態檔
    day_key: str  # 資料日的欄位
    fields: tuple[str, ...]  # 影響推薦的欄位（封存鍵）


SOURCES = (
    Source("tw", "stock_picks_tw", "stock_picks_tw.json", "t",
           ("t", "computed_at", "market", "universe", "picks", "pool", "listed", "medians")),
    Source("tw_ai", "stock_picks_tw_ai", "stock_picks_tw_ai.json", "t",
           ("t", "rule", "computed_at", "universe", "picks", "funnel", "skipped", "core_version", "holdings")),
    Source("us", "stock_picks_us", "stock_picks_us.json", "d",
           ("d", "rule", "computed_at", "pool", "picks", "spy", "universe_as_of")),
)
SOURCE_BY_KIND = {s.kind: s for s in SOURCES}
SOURCE_BY_NAME = {s.name: s for s in SOURCES}

_ISO_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_YEAR_RE = re.compile(r"[0-9]{4}")
ARCHIVE_NAME_RE = re.compile(
    r"(stock_picks_tw|stock_picks_tw_ai|stock_picks_us)-([0-9]{4}-[0-9]{2}-[0-9]{2})-([0-9a-f]{8})\.json\.gz")
IMPORT_NAME_RE = re.compile(r"(stock_picks_tw|stock_picks_tw_ai|stock_picks_us)\.json\.([0-9]{8}_[0-9]{6})\.gz")
SPOOL_NAME_RE = re.compile(r"(tw_ai|us)-([0-9]{4}-[0-9]{2}-[0-9]{2})-([0-9]{1,12}(?:\.[0-9]{1,9})?)\.json")
_TW_CODE_RE = re.compile(r"[0-9]{4}")
_US_SYMBOL_RE = re.compile(r"[A-Z]{1,5}(?:\.[A-Z])?")
_REASON_RE = re.compile(r"[a-z0-9_]{1,32}")
_RULE_RE = re.compile(r"[a-z][a-z0-9_]{0,15}")


# ── 小工具 ─────────────────────────────────────────────────────────────────
def iso_day(value) -> Optional[str]:
    """YYYY-MM-DD 的合法日期原樣回傳，其他回 None。"""
    if not isinstance(value, str) or not _ISO_RE.fullmatch(value):
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def taipei_day(now: float) -> date:
    return datetime.fromtimestamp(now, TPE).date()


def _finite(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def read_private_bytes(path: Path, max_bytes: int) -> Optional[bytes]:
    """自己的一般檔（O_NOFOLLOW、owner＝自己、≤ max_bytes）。不存在回 None；其他問題丟 OSError／ValueError。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("not an own regular file")
        if info.st_size > max_bytes:
            raise ValueError("file too large")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("file too large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def gunzip_capped(data: bytes, max_bytes: int = ARCHIVE_RAW_MAX_BYTES) -> bytes:
    """gzip 解壓；解出來超過 max_bytes 就丟 ValueError（不先整個解開）。"""
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = inflater.decompress(data, max_bytes + 1)
    if len(out) > max_bytes or inflater.unconsumed_tail:
        raise ValueError("archive too large")
    out += inflater.flush()
    if len(out) > max_bytes:
        raise ValueError("archive too large")
    if not inflater.eof:
        raise ValueError("truncated gzip")
    return out


def _sweep(directory: Path, now: float, *, max_age_s: float, match) -> int:
    """刪掉 directory 裡（不遞迴、不跟 symlink）符合 match(name) 而且超過 max_age_s 的一般檔。
    now 要傳真實時間（time.time()）：比的是檔案的 mtime，不是一輪注入的邏輯時間。"""
    removed = 0
    try:
        entries = list(os.scandir(directory))
    except (FileNotFoundError, NotADirectoryError):
        return 0
    for entry in entries:
        if not match(entry.name) or not entry.is_file(follow_symlinks=False):
            continue
        try:
            if now - entry.stat(follow_symlinks=False).st_mtime > max_age_s:
                os.unlink(entry.path)
                removed += 1
        except FileNotFoundError:
            continue
    return removed


# ── 讀狀態檔（每輪一次） ───────────────────────────────────────────────────────
@dataclass
class Loaded:
    source: Source
    status: str  # ok｜missing｜invalid｜empty（AI 狀態還沒有結果，t＝None）
    raw: Optional[bytes] = None
    obj: Optional[dict] = None
    day: Optional[str] = None
    key: Optional[str] = None  # 封存鍵（完整 sha256）


def archive_key(source: Source, obj: dict) -> str:
    subset = {name: obj.get(name) for name in source.fields}
    text = json.dumps(subset, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_loaded(source: Source, raw: bytes) -> Loaded:
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return Loaded(source, "invalid")
    if not isinstance(obj, dict):
        return Loaded(source, "invalid")
    if source.kind == "tw_ai" and obj.get("t") is None:
        return Loaded(source, "empty")
    day = iso_day(obj.get(source.day_key))
    if day is None:
        return Loaded(source, "invalid")
    try:
        key = archive_key(source, obj)
    except (TypeError, ValueError):
        return Loaded(source, "invalid")
    return Loaded(source, "ok", raw=raw, obj=obj, day=day, key=key)


def load_inputs(state_dir: Path) -> dict[str, Loaded]:
    out: dict[str, Loaded] = {}
    for source in SOURCES:
        try:
            raw = read_private_bytes(state_dir / source.filename, STATE_MAX_BYTES)
        except (OSError, ValueError):
            out[source.kind] = Loaded(source, "invalid")
            continue
        out[source.kind] = Loaded(source, "missing") if raw is None else parse_loaded(source, raw)
    return out


# ── 封存 ───────────────────────────────────────────────────────────────────
def archive_path(archive_dir: Path, source: Source, day: str, key: str) -> Path:
    return archive_dir / day[:4] / f"{source.name}-{day}-{key[:8]}.json.gz"


def store_archive(ctx, loaded: Loaded) -> str:
    """回 new｜same。同名已在（同一個 T、同一組影響推薦的欄位）就不寫。dry-run 只回答、不寫。"""
    target = archive_path(ctx.paths.archive, loaded.source, loaded.day, loaded.key)
    if os.path.lexists(target):
        return "same"
    if ctx.dry_run:
        return "new"
    ctx.ensure_dir(ctx.paths.archive)
    ctx.ensure_dir(target.parent)
    ctx.write_atomic(target, gzip.compress(loaded.raw, compresslevel=6, mtime=0))
    return "new"


def _year_dirs(archive_dir: Path) -> list[Path]:
    try:
        entries = list(os.scandir(archive_dir))
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(Path(e.path) for e in entries if _YEAR_RE.fullmatch(e.name) and e.is_dir(follow_symlinks=False))


def archived_files(archive_dir: Path, name: Optional[str] = None, day: Optional[str] = None) -> list[tuple[str, str, Path]]:
    """封存裡的 (名, T, 路徑)，舊到新；name／day 用來篩。只看檔名，不開檔。"""
    out = []
    for year_dir in ([archive_dir / day[:4]] if day else _year_dirs(archive_dir)):
        try:
            entries = list(os.scandir(year_dir))
        except (FileNotFoundError, NotADirectoryError):
            continue
        for entry in entries:
            match = ARCHIVE_NAME_RE.fullmatch(entry.name)
            if not match or not entry.is_file(follow_symlinks=False):
                continue
            if (name and match.group(1) != name) or (day and match.group(2) != day):
                continue
            out.append((match.group(1), match.group(2), Path(entry.path)))
    return sorted(out, key=lambda item: (item[1], item[2].name))


def read_archived(path: Path, source: Source) -> Loaded:
    try:
        data = read_private_bytes(path, ARCHIVE_GZ_MAX_BYTES)
        if data is None:
            return Loaded(source, "missing")
        return parse_loaded(source, gunzip_capped(data))
    except (OSError, ValueError, EOFError, zlib.error):
        return Loaded(source, "invalid")


def prune_archive(ctx) -> int:
    """T 早於 3 年前的封存刪掉（job 自己的保留期限，PLAN §4.2）。"""
    cutoff = (ctx.today - timedelta(days=ARCHIVE_KEEP_DAYS)).isoformat()
    removed = 0
    for _name, day, path in archived_files(ctx.paths.archive):
        if day < cutoff:
            try:
                os.unlink(path)
                removed += 1
            except FileNotFoundError:
                pass
    return removed


def archive_sizes(archive_dir: Path) -> tuple[int, int]:
    total = files = 0
    for _name, _day, path in archived_files(archive_dir):
        try:
            total += path.stat().st_size
            files += 1
        except FileNotFoundError:
            continue
    return total, files


def archive_stage(ctx) -> dict:
    """讀三個狀態檔（每輪一次，結果放 ctx.inputs 給池子紀錄用）→ 封存新的版本 → 清 3 年前的與當機留下的暫存檔。"""
    inputs = load_inputs(ctx.paths.state_dir)
    ctx.inputs = inputs
    out: dict[str, Any] = {"status": "ok"}
    for kind, loaded in inputs.items():
        out[kind] = store_archive(ctx, loaded) if loaded.status == "ok" else loaded.status
    if not ctx.dry_run:
        out["pruned"] = prune_archive(ctx)
        out["tmp_removed"] = sum(_sweep(d, time.time(), max_age_s=TMP_MAX_AGE_S, match=lambda n: n.startswith(".tmp-"))
                                 for d in _year_dirs(ctx.paths.archive))
    out["bytes"], out["files"] = archive_sizes(ctx.paths.archive)
    return out


# ── 池子紀錄：資料結構與寫入 ────────────────────────────────────────────────────
@dataclass
class Row:
    code: str
    name: Optional[str]
    c0: Optional[float]
    picked: bool
    rank: Optional[int]  # 這條規則的候選照 bot 的排序的名次（1 起；picks＝前幾名）；不是候選是 None
    status: str
    trend: dict  # {"trend": pool|ok|no:…|skipped:…|unknown|unchecked|None, "tr": [收盤, 季線, 年線, p60] 或 None}
    flags: dict  # 條件名 → True／False／None
    values: Optional[dict]


@dataclass
class Version:
    m: str
    rule: str
    t: str
    computed_at: float
    family: str
    src: str = SIMPLE
    bot_sha: Optional[str] = None
    source: Optional[str] = None
    core_version: Optional[str] = None
    cooldown_read: Optional[str] = None
    market: Any = None
    meta: dict = field(default_factory=dict)
    rows: list[Row] = field(default_factory=list)
    derive_version: int = DERIVE_VERSION

    @property
    def ver_s(self) -> int:
        return round(self.computed_at)  # 和 bot 的歷史紀錄同一個 round（舊規則、美股存的是 round(computed_at)）

    @property
    def key(self) -> tuple:
        return (self.m, self.rule, self.t, self.ver_s, self.derive_version)


_KEY_WHERE = "m = ? AND rule = ? AND t = ? AND ver_s = ? AND derive_version = ?"


def _dumps(value) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _check_version(version: Version) -> None:
    if not version.rows:
        raise ValueError("version without members")
    if len({row.code for row in version.rows}) != len(version.rows):
        raise ValueError("duplicate member")
    for row in version.rows:
        if row.status == MEMBER and not (row.c0 is not None and math.isfinite(row.c0) and row.c0 > 0):
            raise ValueError("member without c0")


def version_exists(conn, key: tuple) -> Optional[str]:
    """這個鍵已經記了就回它的 src（simple／spool），沒有回 None。conn 是 None（dry-run 沒有 DB）也回 None。"""
    if conn is None:
        return None
    found = conn.execute(f"SELECT src FROM pool_days WHERE {_KEY_WHERE}", key).fetchone()
    return found[0] if found else None


def write_version(conn, version: Version, *, now: float) -> str:
    """回 new｜exists｜replaced。spool 可以取代同一個鍵的簡版；其他已經有的一律不動。"""
    from jobs import stock_picks_review_db as rdb

    _check_version(version)
    with rdb.transaction(conn):
        existing = version_exists(conn, version.key)
        result = "new"
        if existing is not None:
            if not (version.src == SPOOL and existing == SIMPLE):
                return "exists"
            conn.execute(f"DELETE FROM pool_rows WHERE {_KEY_WHERE}", version.key)
            conn.execute(f"DELETE FROM pool_days WHERE {_KEY_WHERE}", version.key)
            result = "replaced"
        conn.execute(
            "INSERT INTO pool_days (m, rule, t, ver_s, derive_version, computed_at, src, bot_sha, n_members, n_picks,"
            " source, core_version, cooldown_read, market_json, meta_json, logged_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*version.key, float(version.computed_at), version.src, version.bot_sha,
             sum(1 for r in version.rows if r.status == MEMBER), sum(1 for r in version.rows if r.picked),
             version.source, version.core_version, version.cooldown_read, _dumps(version.market),
             _dumps(version.meta), float(now)),
        )
        conn.executemany(
            "INSERT INTO pool_rows (m, rule, t, ver_s, derive_version, code, name, c0, picked, rank, status,"
            " trend_json, flags_json, values_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*version.key, r.code, r.name, r.c0, 1 if r.picked else 0, r.rank, r.status,
              _dumps(r.trend), _dumps(r.flags), _dumps(r.values)) for r in version.rows],
        )
    return result


def _save(ctx, conn, version: Version) -> str:
    """dry-run 只回答會不會寫；平常照 write_version。"""
    if ctx.dry_run:
        return "exists" if version_exists(conn, version.key) else "new"
    return write_version(conn, version, now=ctx.now)


def _tr(trend) -> Optional[list]:
    return None if trend is None else [trend.close, trend.ma60, trend.ma240, trend.p60]


_SHA_CACHE: dict[str, str] = {}


def module_sha(module) -> Optional[str]:
    """bot 模組檔的 sha256（pool_days.bot_sha：部署時新舊程式混跑，事後分得出來；審查 r2#7）。"""
    path = getattr(module, "__file__", None)
    if not path:
        return None
    try:
        info = os.stat(path)
        cache_key = f"{path}:{info.st_mtime_ns}:{info.st_size}"
        if cache_key not in _SHA_CACHE:
            with open(path, "rb") as fh:
                _SHA_CACHE[cache_key] = hashlib.sha256(fh.read()).hexdigest()
        return _SHA_CACHE[cache_key]
    except OSError:
        return None


# ── 三族的推導（純函式：輸入是已驗證的 bot 物件） ───────────────────────────────────
def _market_list(market) -> Optional[list]:
    return None if market is None else [market.level, market.b20, market.b60]


def derive_tw_v1(sp, snap) -> Version:
    """舊規則：成員＝主快照 universe（50 檔；pool 裡多放的 0050 不算）。rank＝choose_picks 的排序（bot 選股用的同一個
    純函式，picks 就是前 3 名）。"""
    picks = list(snap.picks)
    universe_facts = [snap.pool[code] for code in snap.universe if code in snap.pool]
    candidates = [f.code for f in sp.choose_picks(universe_facts, limit=len(universe_facts))]
    ranks = {code: index + 1 for index, code in enumerate(candidates)}
    rows = []
    for code in snap.universe:
        facts = snap.pool.get(code)
        listed = snap.listed.get(code)
        c0 = listed.close if listed is not None else None
        if facts is None:  # universe 的代號一定在 pool 裡（refresh 這樣組）；萬一不在，只記代號與收盤
            rows.append(Row(code, listed.name if listed else None, c0, code in picks, None,
                            NO_FACTS if c0 is not None else NO_CLOSE, {"trend": None, "tr": None}, {}, None))
            continue
        rows.append(Row(
            code=code, name=facts.name, c0=c0, picked=code in picks, rank=ranks.get(code),
            status=MEMBER if c0 is not None else NO_CLOSE,
            trend={"trend": "pool" if facts.trend is not None else "unknown", "tr": _tr(facts.trend)},
            flags={check.key: check.state for check in sp.checks_of(facts)},
            values=sp._facts_json(facts),
        ))
    meta = {"pool": len(snap.pool), "listed": len(snap.listed), "candidates": len(candidates),
            "picks_match": candidates[:len(picks)] == picks}  # 重算的排序和 bot 記下的 picks 一致
    return Version(m="tw", rule=TW_OLD_RULE, t=snap.t.isoformat(), computed_at=float(snap.computed_at),
                   family=FAMILY_V1, market=_market_list(snap.market), meta=meta, rows=rows)


def _pick_trend(sp, raw: dict):
    tr = raw.get("tr")
    if isinstance(tr, list) and len(tr) == 4:
        return sp.Trend(close=tr[0], ma60=tr[1], ma240=tr[2], p60=tr[3])
    return None


def derive_tw_ai_simple(sp, state: dict, main, *, main_origin: str) -> Version:
    """AI 簡版：成員＝universe.codes；條件 1–3 用配對主快照的 listed／medians 算；走勢只記 pool／ok，其他 null。
    rank＝ai_candidates 的排序（條件 1–3 都過的候選，bot 用的同一個純函式；和 B1 spool 的 rank 同義）。"""
    universe = state["universe"]
    etf = set(universe.get("etf_codes") or [])
    picks = {raw["c"]: raw for raw in state["picks"]}
    skipped = state.get("skipped") or {}
    # 來源：入選的照 picks 記的；其他只分得出 00952 與核心名單（不呼叫 _ai_core 查產業段，那是 bot 的內部狀態）
    sources = {code: picks[code]["ai_source"] if code in picks else ("00952" if code in etf else "core")
               for code in universe["codes"]}
    candidates, growth_count = sp.ai_candidates(main, universe["codes"], sources)
    ranks = {f.code: index + 1 for index, f in enumerate(candidates)}
    rows: list[Row] = []
    mismatched = 0
    for code in universe["codes"]:
        pick = picks.get(code)
        source = sources[code]
        rank = ranks.get(code)
        listed = main.listed.get(code)
        values: dict[str, Any] = {"ai_source": source}
        if code in skipped:
            values["skipped"] = skipped[code]  # bot 記下的「資料拿不到」原因；走勢欄照規格不推論，仍是 null
        if listed is None:
            rows.append(Row(code, pick["n"] if pick else None, None, pick is not None, rank, NO_CLOSE,
                            {"trend": None, "tr": None}, {}, values))
            continue
        facts = sp._facts_for(code, listed, main.t, None, main.medians)
        pool = main.pool.get(code)
        trend = None
        label = None
        if pool is not None and pool.trend is not None:  # bot 一樣先用主池的走勢（_maybe_fill_ai）
            label, trend = "pool", pool.trend
        elif pick is not None:
            trend = _pick_trend(sp, pick)
            label = "ok" if trend is not None else None
        checks = sp.ai_checks_of(replace(facts, trend=trend) if trend is not None else facts, source)
        flags = {check.key: check.state for check in checks}
        if trend is None:
            flags["trend"] = None  # ai_checks_of 把「沒有走勢」算成 False；這裡是不知道
        if pick is not None and not all(value is True for value in flags.values()):
            mismatched += 1  # bot 選了它，用配對的主快照重算卻不成立：配對可能不是 bot 當時那份
        values = {**sp._facts_json(facts), **values}
        rows.append(Row(code, facts.name, listed.close, pick is not None, rank, MEMBER,
                        {"trend": label, "tr": _tr(trend)}, flags, values))
    funnel = state.get("funnel") or {}
    meta = {
        "funnel": funnel,
        # 重算的條件 1–3 檔數和 bot 記下的漏斗一致：配對的主快照就是 bot 當時用的那份
        "funnel_match": (funnel.get("growth"), funnel.get("cheap")) == (growth_count, len(candidates)),
        "main_ver_s": round(main.computed_at),
        "main_origin": main_origin,
        "pick_mismatch": mismatched,
        "skipped": len(skipped),
        "universe_as_of": universe.get("as_of"),
        "fetched_day": universe.get("fetched_day"),
    }
    return Version(m="tw", rule=state["rule"], t=state["t"], computed_at=float(state["computed_at"]),
                   family=FAMILY_AI, source=universe.get("source"), core_version=state.get("core_version") or None,
                   market=_market_list(main.market), meta=meta, rows=rows)


def derive_us(us, snap, db_closes: Optional[dict]) -> Version:
    """美股：成員＝快照的 pool；c0＝DB 的 D 收盤，沒有就用 latest_closes 裡日期等於 D 的那筆。
    rank＝choose_us_picks 的排序（bot 選股用的同一個純函式，picks 就是前 3 名）。"""
    picks = list(snap.picks)
    candidates = [f.symbol for f in us.choose_us_picks(snap.pool, limit=len(snap.pool))]
    ranks = {symbol: index + 1 for index, symbol in enumerate(candidates)}
    rows = []
    from_db = from_latest = 0
    for facts in snap.pool:
        c0 = _finite((db_closes or {}).get(facts.symbol))
        if c0 is not None and c0 > 0:
            from_db += 1
        else:
            c0 = None
            latest = snap.latest_closes.get(facts.symbol)
            if latest is not None and latest[0] == snap.d:
                c0 = float(latest[1])
                from_latest += 1
        trend: dict[str, Any] = {"trend": "pool" if facts.trend is not None else "unknown", "tr": _tr(facts.trend)}
        if facts.gray:
            trend["gray"] = facts.gray
        rows.append(Row(
            code=facts.symbol, name=facts.name, c0=c0, picked=facts.symbol in picks, rank=ranks.get(facts.symbol),
            status=MEMBER if c0 is not None else NO_CLOSE, trend=trend,
            flags={check.key: check.state for check in us.us_checks_of(facts)},
            values=us._facts_json(facts),
        ))
    spy = None if snap.spy is None else {"level": snap.spy.level, "b20": snap.spy.b20, "b60": snap.spy.b60}
    meta = {"covered": snap.covered, "c0_db": from_db, "c0_latest": from_latest, "candidates": len(candidates),
            "picks_match": candidates[:len(picks)] == picks,
            "universe_as_of": None if snap.universe_as_of is None else snap.universe_as_of.isoformat()}
    return Version(m="us", rule=snap.rule, t=snap.d.isoformat(), computed_at=float(snap.computed_at),
                   family=FAMILY_US, market=spy, meta=meta, rows=rows)


# ── 配對主快照（AI 簡版） ────────────────────────────────────────────────────────
class MainCache:
    """主快照的候選：現行的一份＋同一批匯入的＋封存裡依 T 找的（解析過就留著，同一輪不重讀）。"""

    def __init__(self, sp, archive_dir: Path, current=None):
        self.sp = sp
        self.archive_dir = archive_dir
        self.current = current
        self._added: list = []
        self._archived: dict[str, list] = {}

    def add(self, snap, origin: str) -> None:
        self._added.append((snap, origin))

    def _from_archive(self, day: str) -> list:
        if day not in self._archived:
            found = []
            source = SOURCE_BY_KIND["tw"]
            for _name, _day, path in archived_files(self.archive_dir, source.name, day):
                loaded = read_archived(path, source)
                if loaded.status != "ok":
                    continue
                try:
                    found.append((self.sp.snapshot_from_json(loaded.obj), "archive"))
                except Exception:  # noqa: BLE001 - 舊格式、壞檔：這份不能拿來配
                    continue
            self._archived[day] = found
        return self._archived[day]

    def pair(self, day: str, computed_at: float):
        """同一個 T、computed_at 不晚於 AI 那一版的最新一份（bot 算 AI 時手上那份）；都比它晚就用最早的一份。
        沒有回 None。回 (主快照, 來源)。"""
        current = self.current
        if current is not None and current.t.isoformat() == day and current.computed_at <= computed_at:
            return current, "current"  # 現行的就是最新的，而且不晚於 AI：不用翻封存
        options = [item for item in self._added if item[0].t.isoformat() == day] + self._from_archive(day)
        if current is not None and current.t.isoformat() == day:
            options.append((current, "current"))
        if not options:
            return None
        before = [item for item in options if item[0].computed_at <= computed_at]
        if before:
            return max(before, key=lambda item: item[0].computed_at)
        return min(options, key=lambda item: item[0].computed_at)


# ── 三族各自記一版 ───────────────────────────────────────────────────────────
def _parse_main(sp, loaded: Optional[Loaded]):
    if loaded is None or loaded.status != "ok":
        return None, (loaded.status if loaded else "missing")
    try:
        return sp.snapshot_from_json(loaded.obj), "ok"
    except Exception:  # noqa: BLE001 - 壞檔、舊格式：bot 自己也當作沒有
        return None, "invalid"


def _parse_ai(sp, loaded: Optional[Loaded]):
    if loaded is None or loaded.status != "ok":
        return None, (loaded.status if loaded else "missing")
    try:
        state = sp.ai_state_from_json(loaded.obj)
    except Exception:  # noqa: BLE001
        return None, "invalid"
    if state.get("t") is None:
        return None, "empty"
    return state, "ok"


def log_v1(ctx, conn, sp, snap) -> str:
    version = derive_tw_v1(sp, snap)
    version.bot_sha = module_sha(sp)
    return _save(ctx, conn, version)


def new_ai_counts(status: str = "ok") -> dict:
    return {"status": status, "new": 0, "exists": 0, "replaced": 0, "waiting": 0, "pair_missing": 0, "spool_pending": 0}


def log_ai_version(ctx, conn, sp, state: dict, mains: MainCache, counts: dict) -> None:
    key = ("tw", state["rule"], state["t"], round(state["computed_at"]), DERIVE_VERSION)
    if version_exists(conn, key) is not None:
        counts["exists"] += 1
        return
    if spool_exists(ctx.paths.spool, "tw_ai", state["t"], key[3]):
        counts["spool_pending"] += 1  # 這一版有 spool：交給 import_spools，不記簡版
        return
    if ctx.now - float(state["computed_at"]) < AI_GRACE_S:
        counts["waiting"] += 1
        return
    paired = mains.pair(state["t"], float(state["computed_at"]))
    if paired is None:
        counts["pair_missing"] += 1  # 同一個 T 的主快照現行沒有、封存也沒有：寫進 last_run，7 天後不再試
        return
    main, origin = paired
    version = derive_tw_ai_simple(sp, state, main, main_origin=origin)
    version.bot_sha = module_sha(sp)
    counts[_save(ctx, conn, version)] += 1


def _us_db_closes(ctx, symbols: list[str], day: str) -> Optional[dict]:
    """{代號: D 的收盤}。DB 忙碌照樣丟 DbBusy（這一輪先不記美股）；DB 讀不到回 None（只用快照的 latest_closes）。"""
    import stock_db_ro

    try:
        found = stock_db_ro.closes("us", symbols, day, path=ctx.paths.market_db)
    except stock_db_ro.DbUnavailable:
        return None
    return {symbol: days[day] for symbol, days in found.items() if day in days}


def log_us(ctx, conn, loaded: Optional[Loaded]) -> str:
    if loaded is None or loaded.status != "ok":
        return loaded.status if loaded else "missing"
    try:
        us = importlib.import_module("stock_picks_us")
    except Exception as exc:  # noqa: BLE001 - 美股還沒部署（或壞了）：只略過美股
        return "unavailable:" + type(exc).__name__
    try:
        snap = us.snapshot_from_json(loaded.obj, ctx.now)
    except Exception:  # noqa: BLE001
        return "invalid"
    import stock_db_ro

    try:
        closes = _us_db_closes(ctx, [f.symbol for f in snap.pool], snap.d.isoformat())
    except stock_db_ro.DbBusy:
        return "busy"
    version = derive_us(us, snap, closes)
    version.bot_sha = module_sha(us)
    return _save(ctx, conn, version)


def _recent_archived_ai(ctx, sp) -> list[dict]:
    source = SOURCE_BY_KIND["tw_ai"]
    since = (ctx.today - timedelta(days=AI_LOOKBACK_DAYS)).isoformat()
    states = []
    for _name, day, path in archived_files(ctx.paths.archive, source.name):
        if day < since:
            continue
        state, status = _parse_ai(sp, read_archived(path, source))
        if status == "ok":
            states.append(state)
    return states


def _dedupe_states(states: Iterable[dict]) -> list[dict]:
    seen, out = set(), []
    for state in states:
        key = (state["rule"], state["t"], round(state["computed_at"]))
        if key not in seen:
            seen.add(key)
            out.append(state)
    return sorted(out, key=lambda s: (s["t"], s["computed_at"]))


def committed_versions(states: Iterable[dict], us_loaded: Optional[Loaded], now: float) -> dict:
    """spool 要對得上的已 commit 版本：{(族, T, ver_s): {"rule", "computed_at"}}。"""
    out = {}
    for state in states:
        out[("tw_ai", state["t"], round(state["computed_at"]))] = {
            "rule": state["rule"], "computed_at": float(state["computed_at"])}
    if us_loaded is not None and us_loaded.status == "ok":
        try:
            us = importlib.import_module("stock_picks_us")
            snap = us.snapshot_from_json(us_loaded.obj, now)
        except Exception:  # noqa: BLE001
            snap = None
        if snap is not None:
            out[("us", snap.d.isoformat(), round(snap.computed_at))] = {
                "rule": snap.rule, "computed_at": float(snap.computed_at)}
    return out


def last_t(conn) -> dict:
    if conn is None:
        return {}
    out = {}
    for family, where in ((FAMILY_V1, "m = 'tw' AND rule = 'v1'"),
                          (FAMILY_AI, "m = 'tw' AND rule LIKE 'ai!_%' ESCAPE '!'"),
                          (FAMILY_US, "m = 'us'")):
        out[family] = conn.execute(f"SELECT max(t) FROM pool_days WHERE {where}").fetchone()[0]
    return out


def pool_stage(ctx) -> dict:
    """spool 匯入 → 舊規則 → AI（現行＋封存裡最近 7 天沒記到的版本）→ 美股。"""
    if ctx.inputs is None:  # 封存階段沒跑成（例外）：自己讀一次
        ctx.inputs = load_inputs(ctx.paths.state_dir)
    inputs = ctx.inputs
    conn = ctx.db()
    sp = importlib.import_module("stock_picks")
    state, ai_status = _parse_ai(sp, inputs.get("tw_ai"))
    states = _dedupe_states(([state] if state else []) + _recent_archived_ai(ctx, sp))
    out: dict[str, Any] = {"status": "ok"}
    try:  # spool 只是 B1 的補充：它出問題不能擋住三族的紀錄（沒 spool 的版本照記簡版）
        out["spool"] = import_spools(ctx, conn, committed_versions(states, inputs.get("us"), ctx.now))
    except Exception as exc:  # noqa: BLE001
        out["spool"] = {"status": "error", "error": type(exc).__name__}

    main, out["tw_v1"] = _parse_main(sp, inputs.get("tw"))
    if main is not None:
        out["tw_v1"] = log_v1(ctx, conn, sp, main)
    mains = MainCache(sp, ctx.paths.archive, current=main)
    ai_counts = new_ai_counts(ai_status)
    for item in states:
        log_ai_version(ctx, conn, sp, item, mains, ai_counts)
    out["tw_ai"] = ai_counts
    out["us"] = log_us(ctx, conn, inputs.get("us"))
    out["last_t"] = last_t(conn)
    return out


# ── 手動封存匯入（一次性） ──────────────────────────────────────────────────────
def import_archive_dir(ctx, directory: Path) -> dict:
    """DIR 裡的 stock_picks_tw.json.<ts>.gz、stock_picks_tw_ai.json.<ts>.gz、stock_picks_us.json.<ts>.gz：
    進封存，也記池子紀錄（同一批的主快照可以拿來配 AI 簡版）。其他檔名（例如紀錄檔 *.jsonl.*.gz）不看。"""
    counts: dict[str, Any] = {"archived": 0, "same": 0, "invalid": 0, "ignored": 0}
    groups: dict[str, list[Loaded]] = {"tw": [], "tw_ai": [], "us": []}
    for entry in sorted(os.scandir(directory), key=lambda e: e.name):
        match = IMPORT_NAME_RE.fullmatch(entry.name)
        if not match or not entry.is_file(follow_symlinks=False):
            counts["ignored"] += 1
            continue
        source = SOURCE_BY_NAME[match.group(1)]
        try:
            data = read_private_bytes(Path(entry.path), ARCHIVE_GZ_MAX_BYTES)
            loaded = parse_loaded(source, gunzip_capped(data or b""))
        except (OSError, ValueError, EOFError, zlib.error):
            loaded = Loaded(source, "invalid")
        if loaded.status != "ok":
            counts["invalid" if loaded.status == "invalid" else "ignored"] += 1
            continue
        counts["archived" if store_archive(ctx, loaded) == "new" else "same"] += 1
        groups[source.kind].append(loaded)
    conn = ctx.db()
    sp = importlib.import_module("stock_picks")
    mains = [snap for snap, _ in (_parse_main(sp, item) for item in groups["tw"]) if snap is not None]
    logged: dict[str, Any] = {"tw_v1": [log_v1(ctx, conn, sp, snap) for snap in mains]}
    cache = MainCache(sp, ctx.paths.archive)
    for snap in mains:
        cache.add(snap, "import")
    ai_counts = new_ai_counts()
    for state in _dedupe_states(s for s, status in (_parse_ai(sp, item) for item in groups["tw_ai"]) if status == "ok"):
        log_ai_version(ctx, conn, sp, state, cache, ai_counts)
    logged["tw_ai"] = ai_counts
    logged["us"] = [log_us(ctx, conn, loaded) for loaded in groups["us"]]
    counts["logged"] = logged
    return counts


# ── spool（B1 起；格式由 B1 定案，這裡先照 PLAN §5.2 的欄位嚴格驗證） ───────────────────────
_TREND_FIXED = frozenset({"pool", "ok", "unknown", "unchecked"})


def _trend_label_ok(value) -> bool:
    if value is None or value in _TREND_FIXED:
        return True
    if isinstance(value, str) and ":" in value:
        head, reason = value.split(":", 1)
        return head in ("no", "skipped") and bool(_REASON_RE.fullmatch(reason))
    return False


def _status_ok(value) -> bool:
    if value in (MEMBER, "frozen"):
        return True
    return isinstance(value, str) and value.startswith("excluded:") and bool(_REASON_RE.fullmatch(value[9:]))


def spool_files(directory: Path) -> list[tuple[str, str, float, Path]]:
    """spool 目錄裡合格的檔：(族, T, computed_at, 路徑)。暫存檔（. 開頭）、symlink、其他名字都不看。"""
    try:
        entries = list(os.scandir(directory))
    except (FileNotFoundError, NotADirectoryError):
        return []
    out = []
    for entry in entries:
        match = SPOOL_NAME_RE.fullmatch(entry.name)
        if not match or not iso_day(match.group(2)) or not entry.is_file(follow_symlinks=False):
            continue
        out.append((match.group(1), match.group(2), float(match.group(3)), Path(entry.path)))
    return sorted(out, key=lambda item: item[3].name)[:SPOOL_MAX_FILES]


def spool_dir_state(spool_dir: Path) -> str:
    """ok｜missing｜unsafe（不是自己的真目錄、有 group／other 權限、symlink）。只看不建：spool/ 只在第一次建
    state/stock_picks_review/ 時建一次，之後把它移走就停掉 B1 的 hook（job 不會再建回來）。"""
    try:
        info = os.lstat(spool_dir)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unsafe"
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        return "unsafe"
    return "ok"


def spool_exists(spool_dir: Path, family: str, day: str, ver_s: int) -> bool:
    """這一版還有沒匯入的 spool（只看 spool/ 本身：匯入過的已經在 DB 裡；review.sqlite 重建時 done/ 裡的不算，
    不然那一版會一直等一個不會再被匯入的 spool）。"""
    if spool_dir_state(spool_dir) != "ok":
        return False
    return any(fam == family and t == day and round(computed_at) == ver_s
               for fam, t, computed_at, _path in spool_files(spool_dir))


def spool_version(obj, family: str, day: str, committed: dict) -> Version:
    """spool 內容 → Version（src＝spool）；不合格丟 ValueError。committed＝已 commit 的那一版（rule、computed_at）。
    暫定格式（B1 定案）：{schema:1, family, t, rule, computed_at, source?, core_version?, cooldown_read?, market?,
    funnel?, members:[{c, n, c0, status, rank, picked, trend, tr, checks, facts, ai_source?}]}"""
    def need(cond: bool) -> None:
        if not cond:
            raise ValueError("invalid spool")

    need(isinstance(obj, dict) and obj.get("schema") == 1 and obj.get("family") == family and obj.get("t") == day)
    rule = obj.get("rule")
    need(isinstance(rule, str) and bool(_RULE_RE.fullmatch(rule)) and rule == committed["rule"])
    computed_at = _finite(obj.get("computed_at"))
    need(computed_at is not None and abs(computed_at - committed["computed_at"]) <= 1e-3)
    members = obj.get("members")
    need(isinstance(members, list) and 1 <= len(members) <= 200)
    code_re = _TW_CODE_RE if family == "tw_ai" else _US_SYMBOL_RE
    rows = []
    for item in members:
        need(isinstance(item, dict))
        code, status = item.get("c"), item.get("status", MEMBER)
        need(isinstance(code, str) and bool(code_re.fullmatch(code)) and _status_ok(status))
        c0 = _finite(item.get("c0"))
        need(c0 is None or c0 > 0)
        need(status != MEMBER or c0 is not None)
        name = item.get("n")
        need(name is None or (isinstance(name, str) and len(name) <= 60 and name.isprintable()))
        rank = item.get("rank")
        need(rank is None or (isinstance(rank, int) and not isinstance(rank, bool) and 1 <= rank <= 200))
        need(_trend_label_ok(item.get("trend")))
        tr = item.get("tr")
        need(tr is None or (isinstance(tr, list) and len(tr) == 4 and all(_finite(v) is not None for v in tr)))
        checks = item.get("checks") or {}
        need(isinstance(checks, dict) and len(checks) <= 8 and all(
            isinstance(k, str) and _REASON_RE.fullmatch(k) and v in (True, False, None) for k, v in checks.items()))
        facts = item.get("facts")
        need(facts is None or (isinstance(facts, dict) and len(facts) <= 32))
        values = dict(facts) if facts else {}
        if item.get("ai_source") is not None:
            need(isinstance(item["ai_source"], str) and len(item["ai_source"]) <= 40)
            values["ai_source"] = item["ai_source"]
        rows.append(Row(code, name, c0, item.get("picked") is True, rank, status,
                        {"trend": item.get("trend"), "tr": tr}, dict(checks), values or None))
    need(len({r.code for r in rows}) == len(rows))

    def short(key: str, limit: int) -> Optional[str]:
        value = obj.get(key)
        return value if isinstance(value, str) and 0 < len(value) <= limit and value.isprintable() else None

    market = obj.get("market")
    return Version(m="tw" if family == "tw_ai" else "us", rule=rule, t=day, computed_at=committed["computed_at"],
                   family=FAMILY_AI if family == "tw_ai" else FAMILY_US, src=SPOOL, source=short("source", 20),
                   core_version=short("core_version", 64), cooldown_read=short("cooldown_read", 64),
                   market=market if isinstance(market, (list, dict)) else None,
                   meta={"funnel": obj.get("funnel") if isinstance(obj.get("funnel"), dict) else None}, rows=rows)


def _move_to_done(ctx, path: Path, suffix: str = "") -> None:
    done = ctx.paths.spool / "done"
    ctx.ensure_dir(done)
    target = done / (path.name + suffix)
    if os.path.lexists(target):
        target = done / f"{path.name}.{int(ctx.now)}{suffix}"
    os.replace(path, target)


def import_spools(ctx, conn, committed: dict) -> dict:
    """B1 的 spool 匯入（佔位；格式等 B1 定案）。committed＝{(族, T, ver_s): {"rule", "computed_at"}}：
    只收對得上已 commit 版本的 spool（部署時新舊程序重疊，同一個 T 可能有兩份）；對不上的只記數字、不寫入，
    放一天後移到 done/。匯入的留在 done/，保留 30 天。spool 一律當不可信輸入（大小上限、只收自己的一般檔）。"""
    spool = ctx.paths.spool
    counts: dict[str, Any] = {"dir": spool_dir_state(spool), "new": 0, "replaced": 0, "exists": 0, "unmatched": 0,
                              "invalid": 0}
    if counts["dir"] != "ok":
        return counts
    if not ctx.dry_run:  # bot 的 _write_json_file 當機留下的 .<名>.tmp
        counts["tmp_removed"] = _sweep(spool, time.time(), max_age_s=TMP_MAX_AGE_S,
                                       match=lambda n: n.startswith(".") and n.endswith(".tmp"))
    for family, day, computed_at, path in spool_files(spool):
        info = committed.get((family, day, round(computed_at)))
        if info is None:
            counts["unmatched"] += 1
            try:
                old = time.time() - path.stat().st_mtime > SPOOL_UNMATCHED_GRACE_S  # 檔案年齡看真實時間
            except FileNotFoundError:
                continue
            if old and not ctx.dry_run:
                _move_to_done(ctx, path, ".unmatched")
            continue
        try:
            obj = json.loads(read_private_bytes(path, SPOOL_MAX_BYTES) or b"null")
            version = spool_version(obj, family, day, info)
        except (OSError, ValueError, RecursionError):
            counts["invalid"] += 1
            if not ctx.dry_run:
                _move_to_done(ctx, path, ".invalid")
            continue
        if ctx.dry_run:
            counts["exists" if version_exists(conn, version.key) == SPOOL else "new"] += 1
            continue
        counts[write_version(conn, version, now=ctx.now)] += 1
        _move_to_done(ctx, path)
    if not ctx.dry_run:
        counts["done_removed"] = _sweep(spool / "done", time.time(), max_age_s=SPOOL_KEEP_DAYS * 86400.0,
                                        match=lambda n: True)
    return counts
