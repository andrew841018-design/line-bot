"""股票推薦回測（計畫 B）的每週 Discord 週報：A 段骨架（收集狀態）＋C1 的推薦成績；C2／C3 再往上加冷凍、背景規則。

規格：ops/state/line_bot_stock_backtest_20261028/PLAN.md（v5）§4.5、§6（C1）。
- 時間：台北週日 20:00 之後；只處理最近一個週日（最早 2026-11-01 那份：A 段 10/29 上線，不補 10/25）。
  每輪只要 outbox／sent／failed 都還沒有「YYYY-MM-DD-stock-picks-weekly」開頭的檔，就組一份放進 outbox
  （token_weekly_report 的 _queued／_drop 模式；暫存檔寫在 job 自己的 drafts/）。
  週一到週六還沒送的上週那份照樣補送；週日 20:00 前不補（同一天晚上就有新的一份，不然一天收到兩份，審查 r3#5）。
- 內容是給 Andrew 看的純文字（relay 照行切成每段 1,890 字以內）。整份最多 REPORT_MAX_CHARS 字（三段以內）；
  A 段的收集狀態加上 C1 的成績（三族加上家人看到的舊五條件 × 4 個天數，固定的行數）平常約兩段。數字全部由程式從
  review.sqlite、盤勢 DB、推薦成績檔與成績帳算出來；固定的只有各段的檢查點日期（PLAN §1 的排程）。
- 三族的名稱與順序（台股 AI 相關股、美股、台股舊五條件（對照組））和【推薦成績】同一張表：
  stock_picks_review_pool.FAMILIES。
- C1 的【推薦成績】由 stock_picks_review_returns.report_lines 組（家人看到的推薦讀 perf 帳、舊五條件的背景日與股票池
  基準讀 member_returns）；它出錯（例如 perf import 不了）只把那一段換成一行說明，週報照送。
- 推薦成績卡只讀 state/stock_picks_perf.json 的摘要（唯讀，不呼叫 perf 讀檔：檔案路徑由 job 注入（測試指到暫存目錄），
  而且 v1、v2 兩版的結果檔都要讀得懂，只取兩版共同的欄位）。
- 台股休市表：跨年的成績要靠下一年的休市表。最後期限＝下一年第一個交易日再加 S − 1 個交易日，S＝perf 的
  CALENDAR_SETTLE_SESSIONS（沒有這個常數的舊版 perf 用 SETTLE_SESSIONS；組週報當下才 import perf 讀，讀不到用 10）：
  過了第 S 個交易日，T_h＝第一個交易日的跨年推薦就會被凍結成 calendar 跳過、永久不算。建議期限再提早 3 天。
  週報兩個日子都寫；12/1 起下一年的表還沒取得（或當年的表不能用）就在週報最上面加一行警示。

這個模組 import 時不做任何 I/O。
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

log = logging.getLogger("stock_picks_review")
TPE = ZoneInfo("Asia/Taipei")
FIRST_SUNDAY = date(2026, 11, 1)
SEND_HOUR = 20
NAME_SUFFIX = "stock-picks-weekly"
REPORT_MAX_CHARS = 5000  # 整份上限：relay 照行切，每段最多 1,890 字，5,000 字一定在三段以內（每行都很短）
PERF_MAX_BYTES = 1024 * 1024

# 各段的檢查點（PLAN §1；之後的段上線時改這裡的說明）。C1 的成績比較已經上線（週報的【推薦成績】），不再列。
CHECKPOINTS = (
    ("自動冷凍績效差的股票", "尚未啟用（目標 11/22 前上線；最早可能觸發是 2027/1/31 那份週報）"),
    ("背景規則（測試修正版規則）", "尚未啟用（目標 12/13 前上線；最早約 2027/5 才有結果）"),
)
DEFAULT_SETTLE_SESSIONS = 10  # 讀不到 perf 時用（stock_picks_perf.SETTLE_SESSIONS）
HOLIDAY_MARGIN_DAYS = 3  # 建議期限比最後期限再提早幾天
HOLIDAY_WARN_FROM = (12, 1)  # 這天起下一年的表還沒取得，週報最上面加警示
_HOLIDAY_UNUSABLE = ("missing", "unknown_rows", "invalid")  # 成績算不了的狀態（busy、unavailable 只是這次讀不到）
METHOD = ("算法：推薦隔天收盤進場，看之後第 5／20／60／240 個交易日；"
          "和股票池等權平均比，台股另列 0050、美股另列 SPY。")


def report_sunday(now: float) -> Optional[date]:
    """最近一個「台北 20:00 已經過了」的週日；早於第一份（11/01）回 None。週日 20:00 前回 None：不補上週那份
    （當天晚上就有新的一份，補送的內容也是今天的資料，審查 r3#5）。"""
    local = datetime.fromtimestamp(now, TPE)
    back = (local.weekday() + 1) % 7  # 週日 0、週一 1 …
    if back == 0 and local.hour < SEND_HOUR:
        return None
    sunday = local.date() - timedelta(days=back)
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
    """價格庫的交易日（db.store_calendar：台股 TAIEX、美股 SPY／VOO／QQQ，和 stock_db_ro 的日曆同一組代號）。
    不 import C1 的模組：returns 壞了，【資料收集】照樣組得出來，只有【推薦成績】那一段換成一行說明。"""
    from jobs import stock_picks_review_db as rdb

    return rdb.store_calendar(conn, market)


def family_status(conn, today: date, calendars: dict[str, list[str]]) -> list[dict]:
    from jobs import stock_picks_review_pool as pool

    week_from = (today - timedelta(days=6)).isoformat()
    out = []
    for family in pool.FAMILIES:  # 名稱、順序和【推薦成績】同一張表
        days: set[str] = set()
        by_origin: Counter = Counter()
        for t, origin, n in conn.execute(
                f"SELECT t, origin, count(*) FROM pool_days WHERE {family.where} GROUP BY t, origin"):
            days.add(t)
            by_origin[origin] += n
        info: dict[str, Any] = {"key": family.key, "label": family.name, "days": len(days),
                                "simple": by_origin["simple"], "spool": by_origin["spool"], "first": None,
                                "last": None, "week": 0, "gaps": None}
        if days:
            first, last = min(days), max(days)
            info.update(first=first, last=last, week=sum(1 for d in days if d >= week_from))
            calendar = calendars[family.market]
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


def calendar_settle_sessions() -> int:
    """perf 的日曆不確定時，T_h 之後等幾個交易日才凍結成 calendar：CALENDAR_SETTLE_SESSIONS（沒有這個常數的舊版 perf
    用 SETTLE_SESSIONS）。組週報的當下才讀：部署的是哪一版 perf 就照哪一版；import 不了或值不對用 10。"""
    try:
        import stock_picks_perf as perf

        value = getattr(perf, "CALENDAR_SETTLE_SESSIONS", perf.SETTLE_SESSIONS)
    except Exception:  # noqa: BLE001 - 週報不能因為 perf 壞了就組不出來
        return DEFAULT_SETTLE_SESSIONS
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 250:
        return DEFAULT_SETTLE_SESSIONS
    return value


def holiday_cutoff(year: int, settle: int, tw_calendar: Sequence[str] = ()) -> date:
    """year 的台股休市表的最後期限：那一年第一個交易日再加 settle − 1 個交易日（第 settle 個交易日）。再晚一個交易日，
    T_h＝第一個交易日的跨年推薦就會被 perf 凍結成 calendar 跳過（日曆確認不了 1/1 這種平日休市），之後永遠不算。
    交易日：價格庫日曆（TAIEX）涵蓋到那一年年初時，已經有的日子照日曆；之後的照平日算、只扣元旦（休市表還沒取得時
    只知道這些；春節這類假日也當成交易日，所以算出來的日子只會偏早、不會偏晚）。日曆從年中才開始就全部照平日算。"""
    start = f"{year:04d}-01-01"
    covered = any(d < start for d in tw_calendar)
    known = sorted(d for d in tw_calendar if d.startswith(start[:5])) if covered else []
    sessions = [date.fromisoformat(d) for d in known[:settle]]
    day = sessions[-1] + timedelta(days=1) if sessions else date(year, 1, 2)
    while len(sessions) < settle:
        if day.weekday() < 5:
            sessions.append(day)
        day += timedelta(days=1)
    return sessions[settle - 1]


# ── 文字 ───────────────────────────────────────────────────────────────────
_SPOOL_DIR_WORDS = {"missing": "（spool 目錄不在：hook 停著）", "unsafe": "（spool 目錄權限不對：hook 停著）"}


def _family_line(info: dict, us_live: bool, spool_dir: str = "ok") -> str:
    if not info["days"]:
        tail = "（美股推薦還沒上線）" if info["key"] == "us" and not us_live else ""
        return f"・{info['label']}：還沒有紀錄{tail}"
    gaps = info["gaps"]
    gap_text = "缺漏數不出來（日曆還沒有資料）" if gaps is None else "沒有缺漏" if gaps == 0 else f"缺 {gaps} 天"
    line = (f"・{info['label']}：記到 {_md(info['last'])}，共 {info['days']} 個交易日"
            f"（本週 +{info['week']}），{gap_text}")
    if info["key"] == "tw_ai":
        line += f"；簡版 {info['simple']} 份、spool {info['spool']} 份" + _SPOOL_DIR_WORDS.get(spool_dir, "")
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


def _deadline_text(cutoff: date, today: date) -> str:
    """建議期限（最後期限再提早 HOLIDAY_MARGIN_DAYS 天；過了就不寫）＋最後期限。today 要不晚於最後期限。"""
    target = cutoff - timedelta(days=HOLIDAY_MARGIN_DAYS)
    last = f"最晚 {_ymd(cutoff.isoformat())} 前沒有，跨年那幾天的推薦會永久不算"
    return (f"建議 {_ymd(target.isoformat())} 前取得；" if today <= target else "") + last


def _holiday_line(status: dict[int, str], cutoffs: dict[int, date], today: date) -> str:
    parts = []
    for year, state in sorted(status.items()):
        text = f"{year} {_HOLIDAY_WORDS.get(state, '讀不到')}"
        if state in _HOLIDAY_UNUSABLE and today <= cutoffs[year]:  # busy／unavailable 只是這次讀不到：不寫期限
            text += f"（{_deadline_text(cutoffs[year], today)}）"
        parts.append(text)
    return "【台股休市表】" + "；".join(parts)


def _holiday_warnings(status: dict[int, str], cutoffs: dict[int, date], today: date) -> list[str]:
    """週報最上面的警示：下一年的表 12/1 起還不能用；當年的表不能用（最後期限前、過了都要寫）。"""
    lines = []
    for year, state in sorted(status.items()):
        if state not in _HOLIDAY_UNUSABLE:
            continue
        if year > today.year and (today.month, today.day) < HOLIDAY_WARN_FROM:
            continue
        head = f"⚠️ {year} 台股休市表{_HOLIDAY_WORDS[state]}："
        if today <= cutoffs[year]:
            lines.append(head + _deadline_text(cutoffs[year], today))
        else:
            lines.append(head + f"已經過了 {_ymd(cutoffs[year].isoformat())} 的最後期限，"
                                "期間有休市日的推薦（含跨年那幾天）都會永久不算")
    return lines


def performance_lines(ctx, conn, calendars: dict[str, list[str]]) -> list[str]:
    """C1 的【推薦成績】一段；出錯只換成一行（例外類別），不讓整份週報組不出來。沒有 review.sqlite 就不寫這段。"""
    if conn is None:
        return []
    try:
        from jobs import stock_picks_review_returns as returns

        return returns.report_lines(ctx, conn, calendars)
    except Exception as exc:  # noqa: BLE001
        log.error("stock picks review weekly performance error_type=%s", type(exc).__name__)
        return [f"【推薦成績】這週算不出來（{type(exc).__name__}），其他內容照常"]


def build_report(ctx, sunday: date) -> str:
    conn = ctx.db()
    today = ctx.today
    calendars = {market: trading_days(conn, market) for market in ("tw", "us")} if conn is not None else {}
    families = family_status(conn, today, calendars) if conn is not None else []
    prices = price_status(conn, today) if conn is not None else None
    us_live = (ctx.paths.state_dir / "stock_picks_us.json").exists()
    from jobs import stock_picks_review_pool as pool

    spool_dir = pool.spool_dir_state(ctx.paths.spool)
    holidays = holiday_status(ctx.paths.market_db, (today.year, today.year + 1))
    settle = calendar_settle_sessions()
    cutoffs = {year: holiday_cutoff(year, settle, calendars.get("tw", ())) for year in holidays}
    lines = [f"📊 股票推薦回測週報（{_md(sunday)}）", *_holiday_warnings(holidays, cutoffs, today),
             "目前只收集資料、只報告，還沒有任何自動修正", "", "【資料收集】"]
    lines += [_family_line(info, us_live, spool_dir) for info in families] or ["・資料庫還沒有建立"]
    if prices is not None:
        lines.append(_price_line(prices))
    lines += ["", _perf_line(perf_status(ctx.paths.perf_result)), _holiday_line(holidays, cutoffs, today)]
    performance = performance_lines(ctx, conn, calendars)
    if performance:
        lines += ["", *performance]
    lines += ["", "【之後的檢查點】"]
    lines += [f"・{name}：{state}" for name, state in CHECKPOINTS]
    lines += ["", METHOD]
    text = "\n".join(lines)
    return text if len(text) <= REPORT_MAX_CHARS else text[:REPORT_MAX_CHARS - 1] + "…"


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
