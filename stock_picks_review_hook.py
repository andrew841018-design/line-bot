"""回測週報的池子紀錄與冷凍 hook（2026-10-29，回測 PLAN §5.2；葉節點：bot 用到時才 import，別的模組不依賴它）。

AI 相關股推薦算完、AI 狀態檔寫好（stock_picks._commit_ai 回 True）、AI 歷史也追加之後，stock_picks._maybe_fill_ai 呼叫
write_ai_spool 一次：把這一輪手上才有、事後重建不回來的東西（冷凍前的全名單、每檔的條件值與收盤、候選排名、每個候選的
走勢結果）寫成一個 spool 檔，給回測 job（launchd com.andrew.stock-picks-review）匯入池子紀錄。job 把 spool 當不可信的
輸入嚴格驗證，用 computed_at 對回已經 commit 的那一版（部署時新舊程序重疊，同一個 T 可能有兩份）。

spool 只是家人推薦路徑上多的一小段，所以：
- 只寫自己的檔：一次 stock_picks._write_json_file(state/stock_picks_review/spool/tw_ai-<T>-<round(computed_at)>.json)
  （同目錄暫存檔＋fsync＋os.replace、0600）。不寫 AI 狀態檔、不拿 stock_picks._lock／_state_file_lock、不 mkdir、
  不連網、沒有模組層級的狀態（conftest 不用多重設什麼）。
- AI 關著（LINE_BOT_STOCK_PICKS_AI=0）什麼都不做（_maybe_fill_ai 本來就不跑，這裡再擋一次）。
- spool 目錄要已經存在、是真的目錄（lstat：symlink 不算）、擁有者是自己、權限剛好 0700；上一層 stock_picks_review
  也要是自己的真目錄、別人不能寫。目錄不存在＝回測 job 還沒上線或已經回滾，安靜跳過；其他不符合記 warning、不寫。
  所以回滾時把目錄移走，hook 就停了。
- 目錄裡已經有 200 個以上的檔（job 停了、沒在匯入）就不寫，這一次記一個 warning（沒有模組狀態，所以是每次跳過記一次；
  AI 一天只 commit 幾次）。
- 任何例外都在這裡接住、只記 warning（呼叫端再包一層 try）：不能變成 AI 段的失敗。最大輸入（名單 120 檔）也在
  100 ms 內寫完（有測試）。

冷凍（B2，2026-11-06，PLAN_B2C2 §2）：算推薦之前，stock_picks._maybe_fill_ai（台股 AI）和 stock_picks_us._recompute（美股）
呼叫 apply_cooldown 一次，把冷凍中的股票從可選名單拿掉（家人卡片不提，冷凍股只是不出現）。冷凍檔由回測 job 寫：
    state/stock_picks_review/cooldown.json
    {"schema": 1, "families": {"tw_ai": {"frozen": [{"code", "since", "until"}]}, "us": {"frozen": [...]}}}
- 兩族的鍵都要有，沒有冷凍股就寫空的 frozen：少一族算不合格，整份不用，另一族也不套用。
- 檔案不存在是正常狀況（job 還沒開始冷凍），不記 warning，推薦和沒有這個功能時一樣。每次重讀、不快取（AI 一天算幾次、
  美股一天一次，只是一個小檔）。
- 開檔帶 O_NONBLOCK｜O_NOFOLLOW｜O_CLOEXEC，不沿用 stock_picks._read_json_file：檔案是 FIFO 時 open 不能卡住
  （_maybe_fill_ai 和 _tick 同一條執行緒）。任何一項不合格，整份不用（invalid），照常推薦。
- 資料日（台股 T、美股 D，不是當下時間）落在 since 到 until 之間才算。同一個 T 常重算，少了 since 這一邊，週日凍結後
  週一清晨的重算會把週六家人已經看到的推薦換掉，推薦成績又會取新的 rev，那檔的成績憑空消失。
- 要拿掉的比名單的 25%（無條件進位）多，這一族整份不用（over_25pct，另一族照算）：這不是正常的冷凍量。
- apply_cooldown 絕不丟例外（出錯回 read＝error，什麼都不拿掉）；呼叫端再包一層 try：任何問題都不能停掉或改變推薦。

spool 的格式（SPOOL_SCHEMA 1）。每檔一列，欄位對應回測 job 的 pool_rows（PLAN §3），job 的 spool_version 照這個驗證：
    schema: 1、family: "tw_ai"
    t: "YYYY-MM-DD"、rule（例如 "ai_v1"）、computed_at（浮點，和 AI 狀態檔、AI 歷史那一行的一樣）、written_at（浮點）
    source: 名單來源 "00952" | "cached" | "core_only"、core_version: 字串
    market: 主快照的大盤 [level, b20, b60]（和主快照狀態檔的 market 欄位同一種寫法）；主快照沒有大盤是 null
    funnel、picks（入選代號）、skipped：同 AI 狀態（都是過濾後的結果）
    cooldown_read: apply_cooldown 的 read：ok｜missing｜invalid｜error；沒呼叫到 hook（import 或呼叫失敗）是 null
    cooldown: {"removed": 實際拿掉的代號（排序過）, "reason": null 或 "over_25pct"（超過 25%，整份沒套用）}；
        沒呼叫到 hook 是 null
    members: 冷凍前的全名單（B2 起 universe.codes 是過濾後的名單，所以 _maybe_fill_ai 另外傳進來），照名單順序；每檔
        c: 代號；n: 名稱；c0: 主快照 listed 的收盤（t 的收盤）
        status: "member"；冷凍股是 "frozen"（rank null、picked false，c0、facts、checks 照算）；
            listed 沒有這檔（不該發生）是 "excluded:not_listed"、c0 是 null
        rank: 條件 1–3 都過的候選照 bot 排序（ai_sort_key）的名次（1 起），不是候選是 null
        picked: 有沒有入選
        trend: 走勢結果（候選才有完整的判定）：
            "pool"             主快照的主池有走勢（不是候選也記）
            "ok"               這個 T 抓到的日線算出來的
            "no:<原因碼>"      這個 T 確定算不出窗口（bot 當不合格）
            "skipped:<原因碼>" 資料拿不到而跳過（同 AI 狀態的 skipped）
                               原因碼是 bot 的固定字串（小寫英文開頭、小寫英數與底線、最多 32 字），看不懂的寫 "other"
                               （_REASON_RE：回測 job 的驗證收得下的子集，test_stock_picks_review_hook 的契約測試核對）
            "unknown"          日線有洞（還沒到期或已過期）、這個 T 抓失敗過：不是「不合格」
            "unchecked"        候選，但這一版沒查到它（排在第 3 檔入選者後面）
            null               不是候選、主池也沒有：bot 沒看它的走勢
        tr: [收盤, 季線, 年線, 季線乖離‰]（走勢知道的才有，其他 null）
        checks: {"ai", "growth", "cheap", "trend"}：條件 1–4，true／false；走勢不知道是 null
        facts: stock_picks._facts_json（有 tr 時也在裡面）；ai_source: "00952" 或核心分類
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
import time
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

import stock_picks

logger = logging.getLogger("stock_picks_review_hook")

SPOOL_SCHEMA = 1
REVIEW_DIR_NAME = "stock_picks_review"
SPOOL_DIR_NAME = "spool"
SPOOL_MODE = 0o700
SPOOL_MAX_FILES = 200
FAMILY_AI = "tw_ai"
FAMILY_US = "us"
MEMBER = "member"
FROZEN = "frozen"
COOLDOWN_NAME = "cooldown.json"
COOLDOWN_SCHEMA = 1
COOLDOWN_MAX_BYTES = 64 * 1024
COOLDOWN_MAX_DAYS = 105  # job 寫的 until 是 since＋105 個日曆天（PLAN_B2C2 §4.2）
COOLDOWN_MAX_SHARE = 0.25
OVER_25PCT = "over_25pct"
_ISO_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")  # 回測 job 的走勢原因碼驗證收得下的子集（契約測試核對）


def spool_dir() -> Path:
    """呼叫時才算（測試把 stock_picks.STATE_DIR 換成 tmp 時也跟著換）。"""
    return stock_picks.STATE_DIR / REVIEW_DIR_NAME / SPOOL_DIR_NAME


def cooldown_path() -> Path:
    """呼叫時才算（同 spool_dir）。"""
    return stock_picks.STATE_DIR / REVIEW_DIR_NAME / COOLDOWN_NAME


def ai_spool_name(t: str, computed_at: float) -> str:
    """tw_ai-<T>-<round(computed_at)>.json（秒＝回測 job 的 ver_s）。"""
    return f"{FAMILY_AI}-{t}-{round(computed_at)}.json"


def _dir_problem(path: Path, *, exact_mode: Optional[int]) -> Optional[str]:
    """目錄可以寫 spool 嗎：回 None（可以）或原因碼（"missing" 是正常狀況）。lstat：symlink 不算目錄。"""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    if not stat.S_ISDIR(info.st_mode):
        return "not_dir"
    if info.st_uid != os.getuid():
        return "owner"
    mode = stat.S_IMODE(info.st_mode)
    if exact_mode is not None and mode != exact_mode:
        return "mode"
    if exact_mode is None and mode & 0o022:
        return "writable"
    return None


def _spool_dir_problem(spool: Path) -> Optional[str]:
    parent = _dir_problem(spool.parent, exact_mode=None)
    if parent is not None:
        return "missing" if parent == "missing" else f"review_dir_{parent}"
    return _dir_problem(spool, exact_mode=SPOOL_MODE)


def _too_many_files(spool: Path) -> bool:
    """spool 目錄裡（不含子目錄，例如 job 的 done/）已經有 SPOOL_MAX_FILES 個以上的檔；數到上限就停。"""
    n = 0
    with os.scandir(spool) as entries:
        for entry in entries:
            if entry.is_file(follow_symlinks=False):
                n += 1
                if n >= SPOOL_MAX_FILES:
                    return True
    return False


def _values(trend) -> Optional[list]:
    return None if trend is None else [trend.close, trend.ma60, trend.ma240, trend.p60]


def _reason(value) -> str:
    """原因碼照 bot 的固定字串；看不懂的一律 "other"（spool 不放任意文字）。"""
    return value if isinstance(value, str) and _REASON_RE.fullmatch(value) else "other"


def _trend_of(code: str, snapshot, chart_cache: dict, fail_tries: dict, candidate: bool) -> tuple[Optional[str], object]:
    """(走勢結果, 知道的走勢 Trend 或 None)。主池優先（和 _maybe_fill_ai 一樣），再看這個 T 的日線快取：
    ok → 算出來的值；no → 原因（資料拿不到的原因記 skipped）；hole（還沒到期或已過期）→ unknown；
    沒有快取但這個 T 抓失敗過 → unknown；候選都沒有 → unchecked、不是候選 → None。
    hole／fail 絕不記成 no（r1：未知不是不合格）。"""
    pool = snapshot.pool.get(code)
    if pool is not None and pool.trend is not None:
        return "pool", pool.trend
    entry = chart_cache.get(code)
    if isinstance(entry, tuple) and entry:
        kind = entry[0]
        if kind == stock_picks._AI_OK and len(entry) > 1 and entry[1]:
            return "ok", stock_picks.trend_of(*entry[1])
        if kind == stock_picks._AI_NO:
            reason = _reason(entry[1] if len(entry) > 1 else None)
            return f"{'skipped' if reason in stock_picks._AI_SKIP_REASONS else 'no'}:{reason}", None
        return "unknown", None
    if fail_tries.get(code):
        return "unknown", None
    return ("unchecked" if candidate else None), None


def build_ai_spool(state: dict, snapshot, candidates: Sequence, sources: dict, *, chart_cache: dict,
                   fail_tries: dict, members: Optional[Sequence[str]] = None,
                   cooldown: Optional[Applied] = None) -> dict:
    """spool 的內容（格式見模組說明）。只讀傳進來的東西與主快照，不碰檔案、不改任何狀態。
    members：冷凍前的全名單（universe.codes 是過濾後的名單，走它冷凍股會默默消失），沒傳時退回 universe.codes。
    cooldown：apply_cooldown 的結果；None（沒呼叫到 hook）時 cooldown_read、cooldown 都維持 null。"""
    universe, market = state["universe"], snapshot.market
    rank = {f.code: i for i, f in enumerate(candidates, start=1)}
    picked = {p["c"] for p in state["picks"]}
    removed = cooldown.removed if cooldown is not None else frozenset()
    rows: list[dict] = []
    for code in universe["codes"] if members is None else members:
        source = sources.get(code)
        row = snapshot.listed.get(code)
        trend_label, trend = _trend_of(code, snapshot, chart_cache, fail_tries, code in rank)
        member = {"c": code, "n": None, "c0": None, "status": FROZEN if code in removed else MEMBER,
                  "rank": rank.get(code), "picked": code in picked, "trend": trend_label, "tr": _values(trend),
                  "checks": None, "facts": None, "ai_source": source}
        if row is None:  # 名單是用 listed 過濾出來的，不該發生；照樣留一列，讓 job 看得到
            rows.append(member | {"status": "excluded:not_listed"})
            continue
        facts = stock_picks._facts_for(code, row, snapshot.t, None, snapshot.medians)
        if trend is not None:
            facts = replace(facts, trend=trend)
        checks = {check.key: check.state for check in stock_picks.ai_checks_of(facts, source or "")}
        if facts.trend is None:
            checks["trend"] = None  # 走勢不知道：不是「不合格」
        rows.append(member | {"n": row.name, "c0": row.close, "checks": checks,
                              "facts": stock_picks._facts_json(facts)})
    return {
        "schema": SPOOL_SCHEMA,
        "family": FAMILY_AI,
        "t": state["t"],
        "rule": state["rule"],
        "computed_at": state["computed_at"],
        "written_at": time.time(),
        "source": universe.get("source"),
        "core_version": state.get("core_version", ""),
        "market": None if market is None else [market.level, market.b20, market.b60],
        "funnel": dict(state["funnel"]),
        "picks": [p["c"] for p in state["picks"]],
        "skipped": dict(state.get("skipped") or {}),
        "cooldown_read": None if cooldown is None else cooldown.read,
        "cooldown": None if cooldown is None else {"removed": sorted(removed), "reason": cooldown.reason},
        "members": rows,
    }


def write_ai_spool(state: dict, snapshot, candidates: Sequence, sources: dict, *, chart_cache: dict,
                   fail_tries: dict, members: Optional[Sequence[str]] = None,
                   cooldown: Optional[Applied] = None) -> bool:
    """_maybe_fill_ai 在 _commit_ai 回 True、_append_ai_history 之後呼叫一次。回 True＝寫了一個 spool 檔；
    其他（AI 關著、目錄不在或不對、檔太多、任何例外）回 False，不丟例外。"""
    try:
        if not stock_picks.AI_ENABLED:
            return False
        spool = spool_dir()
        problem = _spool_dir_problem(spool)
        if problem == "missing":
            logger.debug("stock picks review spool skipped reason=missing")
            return False
        if problem is not None:
            logger.warning("stock picks review spool skipped reason=%s", problem)
            return False
        if _too_many_files(spool):
            logger.warning("stock picks review spool skipped reason=too_many_files limit=%d", SPOOL_MAX_FILES)
            return False
        t, computed_at = state.get("t"), state.get("computed_at")
        if not (isinstance(t, str) and _ISO_RE.fullmatch(t) and isinstance(computed_at, (int, float))
                and not isinstance(computed_at, bool) and math.isfinite(computed_at) and computed_at > 0):
            logger.warning("stock picks review spool skipped reason=bad_state")
            return False
        payload = build_ai_spool(state, snapshot, candidates, sources, chart_cache=chart_cache, fail_tries=fail_tries,
                                 members=members, cooldown=cooldown)
        stock_picks._write_json_file(spool / ai_spool_name(t, computed_at), payload)
        return True
    except Exception as exc:
        logger.warning("stock picks review spool failed error_type=%s", type(exc).__name__)
        return False


# ── 冷凍（格式與規則見模組說明） ─────────────────────────────────────────────────
@dataclass(frozen=True)
class Applied:
    """apply_cooldown 的結果。read：ok｜missing｜invalid（read_cooldown 的結果）｜error（意料外的問題）；
    removed：這一輪實際拿掉的代號；reason：None 或 OVER_25PCT（超過上限，整份沒套用）。"""

    read: str
    removed: frozenset
    reason: Optional[str]


def _cooldown_bytes(path: Path) -> Optional[bytes]:
    """自己的一般檔、≤ COOLDOWN_MAX_BYTES 的內容。不存在回 None；其他問題丟 OSError／ValueError。
    O_NONBLOCK：是 FIFO 時 open 立刻回來，下面的 fstat 才擋得到（stock_picks._read_json_file 沒帶，會卡在 open）。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > COOLDOWN_MAX_BYTES:
            raise ValueError("not a small regular file of ours")
        return os.read(fd, COOLDOWN_MAX_BYTES)  # job 換檔是 atomic，開著的 inode 不會長大；讀到一半的 JSON 也過不了解析
    finally:
        os.close(fd)


def _frozen_item_ok(item) -> bool:
    """一筆冷凍：字串 code；since、until 是合法日期，since ≤ until，相隔最多 COOLDOWN_MAX_DAYS 天。
    形狀對、日期不存在時 date.fromisoformat 丟 ValueError（read_cooldown 接住，一樣是整份不用）。"""
    if not isinstance(item, dict) or not isinstance(item.get("code"), str):
        return False
    since, until = item.get("since"), item.get("until")
    if not all(isinstance(day, str) and _ISO_RE.fullmatch(day) for day in (since, until)):
        return False
    return 0 <= (date.fromisoformat(until) - date.fromisoformat(since)).days <= COOLDOWN_MAX_DAYS


def _cooldown_ok(obj) -> bool:
    """schema 對、兩族都在、每族 frozen 是 list、每筆都合格。代號不驗格式：只拿來和 members 取交集。"""
    if not isinstance(obj, dict) or obj.get("schema") != COOLDOWN_SCHEMA:
        return False
    families = obj.get("families")
    if not isinstance(families, dict) or not {FAMILY_AI, FAMILY_US} <= families.keys():
        return False
    for family in families.values():
        frozen = family.get("frozen") if isinstance(family, dict) else None
        if not (isinstance(frozen, list) and all(_frozen_item_ok(item) for item in frozen)):
            return False
    return True


def read_cooldown() -> tuple[str, Optional[dict]]:
    """(read, 內容)：ok（內容驗過）｜missing（檔案不存在：正常狀況，不記 warning）｜invalid（任何一項不合格，整份不用）。"""
    try:
        data = _cooldown_bytes(cooldown_path())
        if data is None:
            return "missing", None
        obj = json.loads(data)
        if _cooldown_ok(obj):
            return "ok", obj
        reason = "content"
    except (OSError, ValueError, RecursionError) as exc:
        reason = type(exc).__name__
    logger.warning("stock picks review cooldown invalid reason=%s", reason)
    return "invalid", None


def effective(family: str, members: Sequence[str], obj: dict, day: str) -> tuple[frozenset, Optional[str]]:
    """這一族在資料日 day（ISO 字串；台股 T、美股 D，不是當下時間）真的要拿掉的代號：回 (代號, 沒套用的原因)。
    obj 是 read_cooldown 驗過的內容；日期格式固定，字串比較就是日期比較。名單外的代號不算，也不佔 25% 的額度。"""
    active = {item["code"] for item in obj["families"][family]["frozen"] if item["since"] <= day <= item["until"]}
    removed = active.intersection(members)
    if len(removed) > math.ceil(COOLDOWN_MAX_SHARE * len(members)):
        logger.warning("stock picks review cooldown not applied reason=%s family=%s removed=%d members=%d",
                       OVER_25PCT, family, len(removed), len(members))
        return frozenset(), OVER_25PCT
    return frozenset(removed), None


def apply_cooldown(family: str, members: Sequence[str], day: str) -> Applied:
    """算推薦之前呼叫一次。絕不丟例外：出錯回 Applied("error", frozenset(), None)，照常推薦。"""
    try:
        read, obj = read_cooldown()
        if obj is None:
            return Applied(read, frozenset(), None)
        removed, reason = effective(family, members, obj, day)
        return Applied(read, removed, reason)
    except Exception as exc:
        logger.warning("stock picks review cooldown failed error_type=%s", type(exc).__name__)
        return Applied("error", frozenset(), None)
