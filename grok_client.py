"""Stub — Grok 已移除（2026-04-26）。

保留檔案僅供既有 test 檔 import 不爆；實際邏輯都回 None / quota_exhausted。
之後重寫 test 移除 grok 相關 mock 後可刪此檔。
"""
from __future__ import annotations


def quota_exhausted() -> bool:
    return True  # 永遠視為已耗盡


def get_quota_info() -> dict:
    return {"used_requests": 0, "limit_requests": 0, "remaining": 0}


def chat(*args, **kwargs):  # pragma: no cover
    return None


def group_messages(items):  # pragma: no cover
    return None
