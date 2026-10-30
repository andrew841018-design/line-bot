"""回測週報的池子紀錄 hook（2026-10-29，回測 PLAN §5.2；葉節點：bot 用到時才 import，別的模組不依賴它）。

AI 相關股推薦算完、AI 狀態檔寫好（stock_picks._commit_ai 回 True）、AI 歷史也追加之後，stock_picks._maybe_fill_ai 呼叫
write_ai_spool 一次：把這一輪手上才有、事後重建不回來的東西（冷凍前的全名單、每檔的條件值與收盤、候選排名、每個候選的
走勢結果）寫成一個 spool 檔，給回測 job（launchd com.andrew.stock-picks-review）匯入池子紀錄。job 把 spool 當不可信的
輸入嚴格驗證，用 computed_at 對回已經 commit 的那一版（部署時新舊程序重疊，同一個 T 可能有兩份）。

家人的推薦路徑上只多這一小段，所以：
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

spool 的格式（SPOOL_SCHEMA 1）。每檔一列，欄位對應回測 job 的 pool_rows（PLAN §3），job 的 spool_version 照這個驗證：
    schema: 1、family: "tw_ai"
    t: "YYYY-MM-DD"、rule（例如 "ai_v1"）、computed_at（浮點，和 AI 狀態檔、AI 歷史那一行的一樣）、written_at（浮點）
    source: 名單來源 "00952" | "cached" | "core_only"、core_version: 字串
    market: 主快照的大盤 [level, b20, b60]（和主快照狀態檔的 market 欄位同一種寫法）；主快照沒有大盤是 null
    funnel、picks（入選代號）、skipped：同 AI 狀態
    cooldown_read: null（B2 起：讀到的冷凍檔版本，或 missing／invalid）
    cooldown: null（B2 起：實際拿掉的代號、沒套用的原因 until／25%）
    members: 冷凍前的全名單（B1 就是 universe.codes），照名單順序；每檔
        c: 代號；n: 名稱；c0: 主快照 listed 的收盤（t 的收盤）
        status: "member"（B2 起冷凍股是 "frozen"）；listed 沒有這檔（不該發生）是 "excluded:not_listed"、c0 是 null
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

import logging
import math
import os
import re
import stat
import time
from dataclasses import replace
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
MEMBER = "member"
_ISO_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")  # 回測 job 的走勢原因碼驗證收得下的子集（契約測試核對）


def spool_dir() -> Path:
    """呼叫時才算（測試把 stock_picks.STATE_DIR 換成 tmp 時也跟著換）。"""
    return stock_picks.STATE_DIR / REVIEW_DIR_NAME / SPOOL_DIR_NAME


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
                   fail_tries: dict) -> dict:
    """spool 的內容（格式見模組說明）。只讀傳進來的東西與主快照，不碰檔案、不改任何狀態。"""
    universe, market = state["universe"], snapshot.market
    rank = {f.code: i for i, f in enumerate(candidates, start=1)}
    picked = {p["c"] for p in state["picks"]}
    members: list[dict] = []
    for code in universe["codes"]:
        source = sources.get(code)
        row = snapshot.listed.get(code)
        trend_label, trend = _trend_of(code, snapshot, chart_cache, fail_tries, code in rank)
        member = {"c": code, "n": None, "c0": None, "status": MEMBER, "rank": rank.get(code),
                  "picked": code in picked, "trend": trend_label, "tr": _values(trend), "checks": None,
                  "facts": None, "ai_source": source}
        if row is None:  # 名單是用 listed 過濾出來的，不該發生；照樣留一列，讓 job 看得到
            members.append(member | {"status": "excluded:not_listed"})
            continue
        facts = stock_picks._facts_for(code, row, snapshot.t, None, snapshot.medians)
        if trend is not None:
            facts = replace(facts, trend=trend)
        checks = {check.key: check.state for check in stock_picks.ai_checks_of(facts, source or "")}
        if facts.trend is None:
            checks["trend"] = None  # 走勢不知道：不是「不合格」
        members.append(member | {"n": row.name, "c0": row.close, "checks": checks,
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
        "cooldown_read": None,
        "cooldown": None,
        "members": members,
    }


def write_ai_spool(state: dict, snapshot, candidates: Sequence, sources: dict, *, chart_cache: dict,
                   fail_tries: dict) -> bool:
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
        payload = build_ai_spool(state, snapshot, candidates, sources, chart_cache=chart_cache, fail_tries=fail_tries)
        stock_picks._write_json_file(spool / ai_spool_name(t, computed_at), payload)
        return True
    except Exception as exc:
        logger.warning("stock picks review spool failed error_type=%s", type(exc).__name__)
        return False
