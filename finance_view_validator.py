"""家族財經觀點驗證器。

跑邏輯：列出 expires_at <= now 的 pending 觀點，
- ticker 類用 yfinance 取 created_at → expires_at 期間最高/最低/收盤價，跟 direction + target 比對
- macro 類暫標 na（簡化）

每週日 weekly_summary.py 推播前呼叫。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import finance_view_db

logger = logging.getLogger(__name__)


def _fetch_yf_range(
    ticker: str, since_ms: int, until_iso: str
) -> Optional[tuple[float, float]]:
    """回 (start_price, end_price) 或 None。"""
    try:
        import yfinance as yf

        start_dt = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
        end_dt = until_iso
        t = yf.Ticker(ticker)
        df = t.history(start=start_dt, end=end_dt, auto_adjust=False)
        if df is None or df.empty:
            return None
        start = float(df["Close"].iloc[0])
        end = float(df["Close"].iloc[-1])
        return (start, end)
    except Exception as e:
        logger.warning("yfinance fetch failed for %s: %s", ticker, e)
        return None


def _judge_ticker(view: dict, prices: tuple[float, float]) -> tuple[str, str]:
    """回 (result, detail)。"""
    start, end = prices
    pct = (end - start) / start * 100 if start else 0.0

    direction = view.get("direction")
    target_price = view.get("target_price")
    target_pct = view.get("target_pct")

    if target_price is not None and target_price > 0:
        if direction == "bull":
            hit = end >= target_price
        elif direction == "bear":
            hit = end <= target_price
        else:
            hit = abs(end - target_price) / target_price < 0.05
        return (
            "hit" if hit else "miss",
            f"目標 {target_price}，實際 {end:.2f}（{pct:+.1f}%）",
        )

    if target_pct is not None:
        if direction == "bull":
            hit = pct >= target_pct
        elif direction == "bear":
            hit = pct <= -abs(target_pct)
        else:
            hit = abs(pct - target_pct) < 5
        return ("hit" if hit else "miss", f"目標 {target_pct:+.1f}%，實際 {pct:+.1f}%")

    if direction == "bull":
        hit = pct > 0
    elif direction == "bear":
        hit = pct < 0
    else:
        hit = abs(pct) < 5
    return ("hit" if hit else "miss", f"{pct:+.1f}% 期間漲跌")


def run() -> int:
    """跑一次驗證循環，回更新筆數。"""
    now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    pending = finance_view_db.list_pending_validation(now_iso)
    updated = 0
    for view in pending:
        symbol_type = view.get("symbol_type")
        ticker = view.get("ticker")
        if symbol_type == "ticker" and ticker:
            prices = _fetch_yf_range(ticker, view["created_at"], view["expires_at"])
            if prices is None:
                continue
            result, detail = _judge_ticker(view, prices)
            finance_view_db.update_validation(
                view["view_id"],
                result,
                detail,
                price_start=prices[0],
                price_end=prices[1],
            )
        else:
            finance_view_db.update_validation(
                view["view_id"],
                "na",
                f"{view.get('macro_topic') or '無 ticker'} 暫無客觀驗證",
            )
        updated += 1
    return updated


if __name__ == "__main__":
    n = run()
    print(f"finance_view_validator: {n} views updated")
