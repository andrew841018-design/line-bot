"""股票推薦回測（計畫 B）的每週 Discord 週報：A 段骨架（收集狀態），C1／C2／C3 再往上加成績、冷凍、背景規則。

規格：ops/state/line_bot_stock_backtest_20261028/PLAN.md（v5）§4.5。
- 時間：台北週日 20:00 之後；只處理最近一個週日（最早 2026-11-01 那份：A 段 10/29 上線，不補 10/25）。
  每輪只要 outbox／sent／failed 都還沒有「YYYY-MM-DD-stock-picks-weekly」開頭的檔，就組一份放進 outbox
  （token_weekly_report 的 _queued／_drop 模式；暫存檔寫在 job 自己的 drafts/）。
- 內容是給 Andrew 看的純文字（relay 每 1,900 字切一段；A 版控制在一則以內）。數字全部由程式從 review.sqlite、
  盤勢 DB 與推薦成績檔算出來；固定的只有各段的檢查點日期（PLAN §1 的排程）。
- 推薦成績卡只讀 state/stock_picks_perf.json 的摘要（唯讀，不 import perf：它的格式 B1 會升版）。

這個模組 import 時不做任何 I/O。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

TPE = ZoneInfo("Asia/Taipei")
FIRST_SUNDAY = date(2026, 11, 1)
SEND_HOUR = 20
NAME_SUFFIX = "stock-picks-weekly"
MAX_CHARS = 1800  # relay 一則 1,900 字
PERF_MAX_BYTES = 1024 * 1024

# (鍵, 名稱, pool_days 的條件, 日曆用哪個市場)
FAMILIES = (
    ("tw/ai", "台股 AI 相關股", "m = 'tw' AND rule LIKE 'ai!_%' ESCAPE '!'", "tw"),
    ("tw/v1", "台股舊規則（對照組）", "m = 'tw' AND rule = 'v1'", "tw"),
    ("us", "美股", "m = 'us'", "us"),
)
CALENDAR_CODES = {"tw": ("TAIEX",), "us": ("SPY", "VOO", "QQQ")}  # 交易日：台股看加權指數，美股看三檔 ETF 的聯集

# 各段的檢查點（PLAN §1；之後的段上線時改這裡的說明）
CHECKPOINTS = (
    ("成績比較（持有 5／20／60／240 個交易日）", "尚未啟用（推薦成績 v2 上線後的第一個週日開始）"),
    ("自動冷凍績效差的股票", "尚未啟用（目標 11/22 前上線；最早可能觸發是 2027/1/31 那份週報）"),
    ("背景規則（測試修正版規則）", "尚未啟用（目標 12/13 前上線；最早約 2027/5 才有結果）"),
)
HOLIDAY_DEADLINE = {2027: "2/8"}  # 跨年的成績要靠下一年的休市表（審查 r3）
METHOD = ("算法：推薦隔天收盤進場，看之後第 5／20／60／240 個交易日；"
          "和股票池等權平均比，台股另列 0050、美股另列 SPY。")


def report_sunday(now: float) -> Optional[date]:
    """最近一個「台北 20:00 已經過了」的週日；早於第一份（11/01）回 None。"""
    local = datetime.fromtimestamp(now, TPE)
    back = (local.weekday() + 1) % 7  # 週日 0、週一 1 …
    sunday = local.date() - timedelta(days=back)
    if back == 0 and local.hour < SEND_HOUR:
        sunday -= timedelta(days=7)
    return sunday if sunday >= FIRST_SUNDAY else None


def report_name(sunday: date) -> str:
    return f"{sunday.isoformat()}-{NAME_SUFFIX}"


def _md(day: str | date) -> str:
    d = date.fromisoformat(day) if isinstance(day, str) else day
    return f"{d.month}/{d.day}"


def _ymd(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.year}/{d.month}/{d.day}"


# ── 資料（純讀） ──────────────────────────────────────────────────────────────
def trading_days(conn, market: str) -> list[str]:
    codes = CALENDAR_CODES[market]
    marks = ",".join("?" * len(codes))
    return [d for (d,) in conn.execute(
        f"SELECT DISTINCT d FROM prices WHERE m = ? AND code IN ({marks}) ORDER BY d", (market, *codes))]


def family_status(conn, today: date) -> list[dict]:
    week_from = (today - timedelta(days=6)).isoformat()
    calendars = {market: trading_days(conn, market) for market in CALENDAR_CODES}
    out = []
    for key, label, where, market in FAMILIES:
        days: set[str] = set()
        by_src: Counter = Counter()
        for t, src, n in conn.execute(f"SELECT t, src, count(*) FROM pool_days WHERE {where} GROUP BY t, src"):
            days.add(t)
            by_src[src] += n
        info: dict[str, Any] = {"key": key, "label": label, "days": len(days), "simple": by_src["simple"],
                                "spool": by_src["spool"], "first": None, "last": None, "week": 0, "gaps": None}
        if days:
            first, last = min(days), max(days)
            info.update(first=first, last=last, week=sum(1 for d in days if d >= week_from))
            calendar = calendars[market]
            if calendar and calendar[0] <= first:  # 日曆要涵蓋到第一天才數得出缺漏
                until = min(last, calendar[-1])
                info["gaps"] = sum(1 for d in calendar if first <= d <= until and d not in days)
        out.append(info)
    return out


def price_status(conn, today: date) -> dict:
    from jobs import stock_picks_review_db as rdb

    wanted = rdb.wanted_codes(conn, today)
    have = {(m, code) for m, code in conn.execute("SELECT DISTINCT m, code FROM prices")}
    out: dict[str, Any] = {}
    for market, codes in wanted.items():
        out[market] = {"wanted": len(codes), "have": sum(1 for code in codes if (market, code) in have)}
    rows, earliest = conn.execute("SELECT count(*), min(d) FROM prices").fetchone()
    out["rows"], out["earliest"] = rows, earliest
    return out


def perf_status(path: Path) -> dict:
    """推薦成績檔的摘要（唯讀；schema 1、2 或之後的版本都只取共同的欄位）。"""
    from jobs import stock_picks_review_pool as pool

    try:
        raw = pool.read_private_bytes(path, PERF_MAX_BYTES)
    except (OSError, ValueError):
        return {"state": "invalid"}
    if raw is None:
        return {"state": "absent"}
    try:
        obj = json.loads(raw)
    except ValueError:
        return {"state": "invalid"}
    computed = obj.get("computed_at") if isinstance(obj, dict) else None
    markets = obj.get("markets") if isinstance(obj, dict) else None
    if not isinstance(computed, (int, float)) or isinstance(computed, bool) or not isinstance(markets, dict):
        return {"state": "invalid"}
    counts: dict[str, list[tuple[int, int]]] = {}
    for market in ("tw", "us"):
        horizons = (markets.get(market) or {}).get("h") if isinstance(markets.get(market), dict) else None
        pairs = []
        for h, stats in (horizons or {}).items():
            n = stats.get("n") if isinstance(stats, dict) else None
            if isinstance(h, str) and h.isdigit() and isinstance(n, int) and not isinstance(n, bool) and n >= 0:
                pairs.append((int(h), n))
        counts[market] = sorted(pairs)
    try:
        day = datetime.fromtimestamp(float(computed), TPE).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return {"state": "invalid"}
    return {"state": "ok", "date": day, "counts": counts}


def holiday_status(market_db: Path, years: tuple[int, ...]) -> dict[int, str]:
    """盤勢 DB 的 obs tw.holidays.YYYY：ok｜missing｜unknown_rows｜invalid｜busy｜unavailable。"""
    try:
        import stock_db_ro
    except Exception:  # noqa: BLE001
        return {year: "unavailable" for year in years}
    out = {}
    for year in years:
        try:
            obj = stock_db_ro.obs_json(f"tw.holidays.{year}", path=market_db)
        except stock_db_ro.DbBusy:
            out[year] = "busy"
            continue
        except stock_db_ro.DbUnavailable:
            out[year] = "unavailable"
            continue
        if obj is None:
            out[year] = "missing"
        elif not isinstance(obj, dict) or not isinstance(obj.get("rows"), list):
            out[year] = "invalid"
        elif obj.get("unknown"):
            out[year] = "unknown_rows"
        else:
            out[year] = "ok"
    return out


# ── 文字 ───────────────────────────────────────────────────────────────────
def _family_line(info: dict, us_live: bool, spool_ok: bool = True) -> str:
    if not info["days"]:
        tail = "（美股推薦還沒上線）" if info["key"] == "us" and not us_live else ""
        return f"・{info['label']}：還沒有紀錄{tail}"
    gaps = info["gaps"]
    gap_text = "缺漏數不出來（日曆還沒有資料）" if gaps is None else "沒有缺漏" if gaps == 0 else f"缺 {gaps} 天"
    line = (f"・{info['label']}：記到 {_md(info['last'])}，共 {info['days']} 個交易日"
            f"（本週 +{info['week']}），{gap_text}")
    if info["key"] == "tw/ai":
        line += f"；簡版 {info['simple']} 份、spool {info['spool']} 份"
        if not spool_ok:
            line += "（spool 目錄不在：hook 停著）"
    return line


def _price_line(prices: dict) -> str:
    parts = []
    for market, name in (("tw", "台股"), ("us", "美股")):
        info = prices[market]
        missing = info["wanted"] - info["have"]
        have = f"{name} {info['have']} 檔"
        parts.append(f"{have}都有收盤" if missing == 0 else f"{have}有收盤、缺 {missing} 檔")
    tail = f"，共 {prices['rows']:,} 筆（最早 {_ymd(prices['earliest'])}）" if prices["earliest"] else "，還沒有資料"
    return "・價格庫：" + "、".join(parts) + tail


def _perf_line(perf: dict) -> str:
    if perf["state"] == "absent":
        return "【推薦成績卡】推薦成績尚未上線"
    if perf["state"] != "ok":
        return "【推薦成績卡】成績檔讀不到（格式不對）"
    parts = []
    for market, name in (("tw", "台股"), ("us", "美股")):
        pairs = perf["counts"].get(market) or []
        if pairs:
            parts.append(name + " " + "、".join(f"{h} 日 {n} 次" for h, n in pairs))
    detail = "；".join(parts) if parts else "還沒有成績"
    return f"【推薦成績卡】上線中（最後更新 {_md(perf['date'])}）：{detail}"


_HOLIDAY_WORDS = {"ok": "已取得", "missing": "尚未取得", "unknown_rows": "有看不懂的列", "invalid": "格式不對",
                  "busy": "這次讀不到（DB 忙碌）", "unavailable": "讀不到"}


def _holiday_line(status: dict[int, str]) -> str:
    parts = []
    for year, state in sorted(status.items()):
        text = f"{year} {_HOLIDAY_WORDS.get(state, '讀不到')}"
        if state != "ok" and year in HOLIDAY_DEADLINE:
            text += f"（要在 {HOLIDAY_DEADLINE[year]} 前取得，不然跨年的成績會延後）"
        parts.append(text)
    return "【台股休市表】" + "；".join(parts)


def build_report(ctx, sunday: date) -> str:
    conn = ctx.db()
    today = ctx.today
    families = family_status(conn, today) if conn is not None else []
    prices = price_status(conn, today) if conn is not None else None
    us_live = (ctx.paths.state_dir / "stock_picks_us.json").exists()
    from jobs import stock_picks_review_pool as pool

    spool_ok = pool.spool_dir_state(ctx.paths.spool) == "ok"
    lines = [f"📊 股票推薦回測週報（{_md(sunday)}）", "目前只收集資料、只報告，還沒有任何自動修正", "", "【資料收集】"]
    lines += [_family_line(info, us_live, spool_ok) for info in families] or ["・資料庫還沒有建立"]
    if prices is not None:
        lines.append(_price_line(prices))
    lines += ["", _perf_line(perf_status(ctx.paths.perf_result)),
              _holiday_line(holiday_status(ctx.paths.market_db, (today.year, today.year + 1))),
              "", "【之後的檢查點】"]
    lines += [f"・{name}：{state}" for name, state in CHECKPOINTS]
    lines += ["", METHOD]
    text = "\n".join(lines)
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS - 1] + "…"


def weekly_stage(ctx) -> dict:
    """週日 20:00 之後、這週還沒排過 → 組一份放進 outbox。dry-run 回文字、不放。"""
    sunday = report_sunday(ctx.now)
    if sunday is None:
        return {"status": "skipped:not_due"}
    name = report_name(sunday)
    if ctx.queued(name):
        return {"status": "skipped:queued", "week": sunday.isoformat()}
    text = build_report(ctx, sunday)
    if ctx.dry_run:
        return {"status": "dry_run", "week": sunday.isoformat(), "chars": len(text), "text": text}
    ctx.drop(f"{name}.md", text)
    return {"status": "queued", "week": sunday.isoformat(), "chars": len(text)}
