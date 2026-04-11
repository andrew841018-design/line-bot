"""
Redis-backed 對話 context + 長期記憶。

連 Upstash Free Tier（rediss:// TLS）。Schema：

    line:ctx:{group_id}        LIST    最近 N 輪對話，每筆是 "role\ttext"
    line:facts:{group_id}      SET     長期事實，每筆是純 UTF-8 字串
    line:msgcount:{group_id}   STRING  對話計數器（給 fact_extract_every 判斷用）
"""
from __future__ import annotations

import redis

from config import settings

_r = redis.from_url(settings.redis_url, decode_responses=True)


def _ctx_key(group_id: str) -> str:
    return f"line:ctx:{group_id}"


def _facts_key(group_id: str) -> str:
    return f"line:facts:{group_id}"


def _count_key(group_id: str) -> str:
    return f"line:msgcount:{group_id}"


# ── Context（短期對話歷史）────────────────────────────────────────────────────

def append_turn(group_id: str, role: str, text: str) -> None:
    """role: 'user' | 'bot'。超過 context_rounds*2 筆會自動截掉最舊的。"""
    _r.rpush(_ctx_key(group_id), f"{role}\t{text}")
    _r.ltrim(_ctx_key(group_id), -settings.context_rounds * 2, -1)


def get_context(group_id: str) -> list[tuple[str, str]]:
    """回傳 [(role, text), ...]，舊→新。"""
    raw = _r.lrange(_ctx_key(group_id), 0, -1)
    out: list[tuple[str, str]] = []
    for item in raw:
        role, _, text = item.partition("\t")
        out.append((role, text))
    return out


# ── Facts（長期記憶）──────────────────────────────────────────────────────────

def add_fact(group_id: str, fact: str) -> bool:
    """回傳是否真的新增（False 代表重複）。"""
    return bool(_r.sadd(_facts_key(group_id), fact.strip()))


def remove_fact(group_id: str, fact_substring: str) -> int:
    """刪除所有「包含該子字串」的事實，回傳刪幾筆。"""
    all_facts = _r.smembers(_facts_key(group_id))
    matched = [f for f in all_facts if fact_substring in f]
    if matched:
        _r.srem(_facts_key(group_id), *matched)
    return len(matched)


def list_facts(group_id: str) -> list[str]:
    return sorted(_r.smembers(_facts_key(group_id)))


def clear_facts(group_id: str) -> int:
    n = _r.scard(_facts_key(group_id))
    _r.delete(_facts_key(group_id))
    return n


def top_facts(group_id: str) -> list[str]:
    """給 prompt 注入用，取前 max_facts_in_prompt 條。"""
    return list_facts(group_id)[: settings.max_facts_in_prompt]


# ── 計數器（決定何時觸發事實抽取）──────────────────────────────────────────

def bump_and_should_extract(group_id: str) -> bool:
    """每呼叫一次 +1；每 fact_extract_every 次回傳一次 True。"""
    n = _r.incr(_count_key(group_id))
    return n % settings.fact_extract_every == 0
