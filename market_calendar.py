"""市場盤勢（/盤勢，2026-10-17）job 用的交易日曆：美股（NYSE）與台股（證交所）。

Andrew 2026-10-17 要 /盤勢 看美股／台股「目前的狀況」：每個數字都要知道是哪一個交易日的、那天是不是
已經收盤、資料是不是漏了。這裡回答「某一天是不是交易日」「最近一個已收盤的交易日是哪天」。

判斷順序（PLAN §3.1、§3.2、§18.3、§18.4；兩個市場一樣，只有輸入不同）：
1. 那天有實際成交的列（美股 SPY、台股加權指數）→ 交易日。事實蓋過覆寫與看不懂的說明
   （颱風判定休市之後又看到資料，就等於撤銷，§18.4）。
2. 覆寫（calendar_overrides）：closed → 不是交易日；open → 是；其他種類（例如 revoked）不算數。
   臨時休市要有獨立證據才會寫進去；只有我們沒抓到資料不算休市（「來源漏日」，§3.1、§3.2）。
3. 官方表涵蓋的年份：平日且不在休市表 → 交易日；台股休市表裡看不懂的說明 → 那天「未知」（不猜）。
   美股表：NYSE 2026–2027（程式內，下面註明出處）；台股表：證交所休市表，以實際抓到的年份為準，
   沒抓到的年份一律未知（給 publish 走「日曆待更新」的退路）。
4. 官方表開始以前：只認實際有成交的日子（美股 2025-09 起的 SPY、台股 FinMind 加權指數）。
5. 其他 → None（未知）。美股週末例外：NYSE 不在週末開盤，表外的週末也是 False。

時間：美股收盤 16:00 美東（早收 13:00），收盤＋30 分鐘才算那天收完；台股 13:30 收盤、14:30 之後才算
當天（ALLBUT0999 14:30 才有）。一律用 aware datetime；`now` 可以是 epoch 秒或 aware datetime。

market_calc 另有給純計算用的 TradingCalendar：`Calendar.to_trading_calendar()`／`trading_calendars(store)`
用同一份輸入轉過去，collect 與 publish 才不會各算各的。

這個模組 import 時不做任何 I/O。
"""

from __future__ import annotations

import dataclasses
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

US_TZ = ZoneInfo("America/New_York")
TW_TZ = ZoneInfo("Asia/Taipei")
US_CLOSE = time(16, 0)
US_EARLY_CLOSE = time(13, 0)
TW_CLOSE = time(13, 30)
US_READY_AFTER = timedelta(minutes=30)  # 收盤＋30 分鐘才算收完（PLAN §3.1 的 D）
TW_READY = time(14, 30)  # 台北 14:30 之後才算當天（PLAN §3.2 的 T）

# NYSE 全天休市與早收：抄自 https://www.nyse.com/markets/hours-calendars
# （2026-10-17 台北時間 14:3x 抄寫；官方頁列 2026–2028、不含 2025；這裡只收 2026–2027）。
NYSE_HOLIDAYS: dict[date, str] = {
    date(2026, 1, 1): "New Year's Day",
    date(2026, 1, 19): "Martin Luther King, Jr. Day",
    date(2026, 2, 16): "Washington's Birthday",
    date(2026, 4, 3): "Good Friday",
    date(2026, 5, 25): "Memorial Day",
    date(2026, 6, 19): "Juneteenth",
    date(2026, 7, 3): "Independence Day (observed)",
    date(2026, 9, 7): "Labor Day",
    date(2026, 11, 26): "Thanksgiving Day",
    date(2026, 12, 25): "Christmas Day",
    date(2027, 1, 1): "New Year's Day",
    date(2027, 1, 18): "Martin Luther King, Jr. Day",
    date(2027, 2, 15): "Washington's Birthday",
    date(2027, 3, 26): "Good Friday",
    date(2027, 5, 31): "Memorial Day",
    date(2027, 6, 18): "Juneteenth (observed)",
    date(2027, 7, 5): "Independence Day (observed)",
    date(2027, 9, 6): "Labor Day",
    date(2027, 11, 25): "Thanksgiving Day",
    date(2027, 12, 24): "Christmas Day (observed)",
}
# 美東 13:00 收盤（合格期權 13:15）；官方頁 2026–2027 沒有其他早收。2025 年的早收不在官方頁上，
# 2025 的日子只用來判斷有沒有開（SPY 有沒有成交），收盤時間照一般 16:00。
NYSE_EARLY_CLOSE: dict[date, time] = {
    date(2026, 11, 27): US_EARLY_CLOSE,
    date(2026, 12, 24): US_EARLY_CLOSE,
    date(2027, 11, 26): US_EARLY_CLOSE,
}
NYSE_TABLE_FROM = date(2026, 1, 1)
NYSE_SUPPORTED_UNTIL = date(2027, 12, 31)

US_OBSERVED_SYMBOL = "SPY"  # market_pipeline_us 存在 closes("us", "SPY")
TW_OBSERVED_SYMBOL = "TAIEX"  # market_pipeline_tw 存在 closes("tw", "TAIEX")
TW_HOLIDAY_OBS_PREFIX = "tw.holidays."  # ＋西元年；要和 market_pipeline_tw.OBS_HOLIDAYS_PREFIX 一樣

OVERRIDE_CLOSED = "closed"
OVERRIDE_OPEN = "open"

# 證交所休市表的分類（和 market_sources_tw 的 Holiday.kind 同一組值）
TW_HOLIDAY_CLOSED = "closed"  # 放假、補假、休市
TW_HOLIDAY_SETTLEMENT_ONLY = "settlement_only"  # 市場無交易，僅辦理結算交割
TW_HOLIDAY_TRADING = "trading"  # 開始交易日、最後交易日
TW_HOLIDAY_UNKNOWN = "unknown"  # 看不懂或同時符合兩類：報維護，不猜
_TW_KINDS = frozenset({TW_HOLIDAY_CLOSED, TW_HOLIDAY_SETTLEMENT_ONLY, TW_HOLIDAY_TRADING, TW_HOLIDAY_UNKNOWN})
_TW_RULES = (
    (TW_HOLIDAY_TRADING, ("開始交易", "最後交易")),
    (TW_HOLIDAY_SETTLEMENT_ONLY, ("無交易",)),
    (TW_HOLIDAY_CLOSED, ("放假", "補假", "休市")),
)

# check_market_info 回的不一致種類（每日維護只看種類）
MISMATCH_BUSINESS_DAY = "is_business_day"
MISMATCH_PREVIOUS = "previous_trade_date"
MISMATCH_NEXT = "next_trade_date"
MISMATCH_CLOSING = "closing_time"
MISMATCH_INCOMPLETE = "info_incomplete"
MISMATCH_UNSUPPORTED = "calendar_unsupported"

_MAX_GAP_DAYS = 45  # 往前／往後找交易日最多找幾天（台股春節連假約 10 天）


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        raise ValueError("expected a date, got a datetime")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError("expected a date")


def _observed_date(value: Any) -> date:
    # 收 date、"YYYY-MM-DD"、或 Store.closes() 的列（第一欄是日期）
    if isinstance(value, (tuple, list)) and value:
        value = value[0]
    elif hasattr(value, "date") and not isinstance(value, date):
        value = value.date
    return _as_date(value)


def _as_utc(now: Any) -> datetime:
    if isinstance(now, datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return now.astimezone(timezone.utc)
    return datetime.fromtimestamp(float(now), timezone.utc)


class Calendar:
    """一個市場的交易日曆（INTERFACES §9.2）。不可變：輸入變了（新的 SPY 列、新的覆寫）就重建一個。"""

    def __init__(
        self,
        market: str,
        *,
        observed: Iterable[Any] = (),
        holidays: Optional[Mapping[Any, str]] = None,
        overrides: Optional[Mapping[Any, Any]] = None,
        supported_until: Optional[date] = None,
        supported_from: Optional[date] = None,
        table_years: Optional[Iterable[int]] = None,
        trading_days: Iterable[Any] = (),
        unclassified: Iterable[Any] = (),
        early_closes: Optional[Mapping[Any, time]] = None,
    ) -> None:
        if market not in ("us", "tw"):
            raise ValueError("market must be 'us' or 'tw'")
        self.market = market
        self.tz = US_TZ if market == "us" else TW_TZ
        is_us = market == "us"
        if holidays is None and is_us:
            holidays = NYSE_HOLIDAYS
        if supported_until is None and is_us:
            supported_until = NYSE_SUPPORTED_UNTIL
        if supported_from is None and is_us:
            supported_from = NYSE_TABLE_FROM
        if early_closes is None and is_us:
            early_closes = NYSE_EARLY_CLOSE
        seen = {_observed_date(d) for d in observed}
        if is_us:
            seen = {d for d in seen if d.weekday() < 5}  # NYSE 不在週末開盤：週末的列當壞資料
        self._observed = frozenset(seen)
        self._obs_min = min(seen) if seen else None
        self._obs_max = max(seen) if seen else None
        self._holidays = {_as_date(d): str(name) for d, name in (holidays or {}).items()}
        self._overrides: dict[date, str] = {}
        for day, value in (overrides or {}).items():
            kind = value.get("kind") if isinstance(value, Mapping) else value
            if kind in (OVERRIDE_CLOSED, OVERRIDE_OPEN):  # revoked 之類的種類＝沒有覆寫
                self._overrides[_as_date(day)] = kind
        self._trading = frozenset(_as_date(d) for d in trading_days)
        self._unclassified = frozenset(_as_date(d) for d in unclassified)
        self._early = {_as_date(d): t for d, t in (early_closes or {}).items()}
        if supported_until is None:
            self._until: Optional[date] = None
            self._from: Optional[date] = None
            self._years: frozenset[int] = frozenset()
        else:
            until = _as_date(supported_until)
            if supported_from is None:
                listed = [d.year for d in (*self._holidays, *self._trading, *self._unclassified) if d <= until]
                supported_from = date(min(listed) if listed else until.year, 1, 1)
            start = _as_date(supported_from)
            years = range(start.year, until.year + 1) if table_years is None else table_years
            self._years = frozenset(int(y) for y in years if start.year <= int(y) <= until.year)
            self._until = until
            self._from = date(min(self._years), 1, 1) if self._years else None
            if self._from is not None and start > self._from:
                self._from = start

    # ── 查詢 ──

    @property
    # supported_until 與 unclassified 由 jobs/market_snapshot._calendar_coverage 寫進 outcome，status_summary 帶出
    # calendar_days_left／calendar_unclassified，每日維護在 ≤30 天或 >0 時報（2026-10-18 審查 r6a I1／I2）。
    def supported_until(self) -> Optional[date]:
        """官方表涵蓋到哪天（每日維護在這天前 30 天開始提醒更新）。"""
        if not self._years or self._until is None:
            return None
        return min(self._until, date(max(self._years), 12, 31))

    @property
    def table_years(self) -> list[int]:
        return sorted(self._years)

    @property
    def unclassified(self) -> list[date]:
        """台股休市表裡看不懂說明的日子（報維護）。"""
        return sorted(self._unclassified)

    @property
    def observed_until(self) -> Optional[date]:
        return self._obs_max

    def _in_table(self, day: date) -> bool:
        return (
            self._from is not None
            and self._until is not None
            and self._from <= day <= self._until
            and day.year in self._years
        )

    def _scheduled(self, day: date) -> Optional[bool]:
        """不看實際成交、只看覆寫與官方表的判斷（is_session 的第 2–5 步）。"""
        kind = self._overrides.get(day)
        if kind == OVERRIDE_CLOSED:
            return False
        if kind == OVERRIDE_OPEN:
            return True
        if self._in_table(day):
            if day in self._unclassified:
                return None
            if day in self._trading:
                return True
            if day.weekday() >= 5:
                return False
            return day not in self._holidays
        if (
            self._obs_min is not None
            and (self._from is None or day < self._from)
            and self._obs_min <= day <= self._obs_max
        ):
            return False  # 表開始以前、資料範圍內卻沒有成交：那天沒開
        if self.market == "us" and day.weekday() >= 5:
            return False
        return None

    def is_session(self, d: Any) -> Optional[bool]:
        """True／False；None＝不知道（表外、沒抓到的年份、看不懂的說明）。"""
        day = _as_date(d)
        if day in self._observed:
            return True
        return self._scheduled(day)

    def _step(self, day: date, direction: int) -> Optional[date]:
        for offset in range(1, _MAX_GAP_DAYS + 1):
            candidate = day + timedelta(days=direction * offset)
            state = self.is_session(candidate)
            if state is None:
                return None
            if state:
                return candidate
        return None

    def prev_session(self, d: Any) -> Optional[date]:
        """d 之前（不含 d）最近的交易日；中間碰到未知就回 None。"""
        return self._step(_as_date(d), -1)

    def next_session(self, d: Any) -> Optional[date]:
        """d 之後（不含 d）最近的交易日；中間碰到未知就回 None。"""
        return self._step(_as_date(d), 1)

    def sessions_back(self, d: Any, n: int) -> list[date]:
        """≤ d 的最近 n 個交易日（含 d），由舊到新；碰到未知就停，所以可能不足 n 個。"""
        if n <= 0:
            return []
        day = _as_date(d)
        state = self.is_session(day)
        if state is None:
            return []
        out: list[date] = []
        current: Optional[date] = day if state else self.prev_session(day)
        while current is not None and len(out) < n:
            out.append(current)
            current = self.prev_session(current)
        out.reverse()
        return out

    def sessions_forward(self, d: Any, n: int) -> list[date]:
        """d 之後（不含 d）的 n 個交易日，由舊到新；碰到未知就停（stale_after 的「4 個交易日後」）。"""
        out: list[date] = []
        current: Optional[date] = _as_date(d)
        while len(out) < n:
            current = self.next_session(current)
            if current is None:
                break
            out.append(current)
        return out

    def close_time(self, d: Any) -> Optional[datetime]:
        """那天的收盤時刻（aware）；不是交易日或不知道 → None。"""
        day = _as_date(d)
        if self.is_session(day) is not True:
            return None
        local = self._early.get(day, US_CLOSE) if self.market == "us" else TW_CLOSE
        return datetime.combine(day, local, tzinfo=self.tz)

    def is_early_close(self, d: Any) -> bool:
        return _as_date(d) in self._early

    def missing_bars(self, start: Any, end: Any) -> list[date]:
        """表上是交易日、卻沒有成交列的日子（給每日維護：可能是颱風覆寫沒寫到，也可能是來源漏日）。"""
        day, last = _as_date(start), _as_date(end)
        out = []
        while day <= last:
            if day not in self._observed and self._scheduled(day) is True:
                out.append(day)
            day += timedelta(days=1)
        return out

    def to_trading_calendar(self) -> Any:
        """轉成 market_calc.TradingCalendar（純計算用的那一份），規則盡量對齊：

        - 有成交卻是休市表／覆寫休市的日子 → extra_sessions（事實優先）；覆寫休市且沒有成交 → closed_overrides。
        - 台股表上看不懂的日子不放進休市：沒有成交時它在 TradingCalendar 是「有開、缺資料」，
          窗口一樣不完整，和這裡回「未知」的效果相同。
        - 抓到的年份不連續時，只取最後一段連續的年份當表（較早的年份落到「表以前、看實際成交」）。
        """
        import market_calc  # noqa: PLC0415 - 只有 publish 要用；collect 不載入 numpy

        years = sorted(self._years)
        if years:
            first = years[-1]
            while first - 1 in self._years:
                first -= 1
            table_from = max(date(first, 1, 1), self._from or date(first, 1, 1))
            table_to = min(self._until or date(years[-1], 12, 31), date(years[-1], 12, 31))
        else:
            # 還沒抓到任何休市表（bootstrap）：把表放到最後，全部日子都走「表以前、看實際成交」，和這裡一致
            table_from, table_to = date.max, date.max
        in_table = [d for d in self._holidays if table_from <= d <= table_to]
        extra = {d for d in self._trading if table_from <= d <= table_to}
        extra |= {d for d, k in self._overrides.items() if k == OVERRIDE_OPEN and table_from <= d <= table_to}
        extra |= {d for d in self._observed if table_from <= d <= table_to}
        closed = [d for d, k in self._overrides.items() if k == OVERRIDE_CLOSED and d not in self._observed]
        observed = [d for d in self._observed if d < table_from]
        if self.market == "us":
            converted = market_calc.nyse_calendar(
                holidays=in_table,
                early_closes=list(self._early),
                table_from=table_from,
                table_to=table_to,
                observed=observed,
                closed_overrides=closed,
            )
            # nyse_calendar 沒有 extra_sessions 參數：表上休市卻有成交（或覆寫有開）的日子另外補進去
            return dataclasses.replace(converted, extra_sessions=frozenset(extra)) if extra else converted
        return market_calc.twse_calendar(
            holidays=in_table,
            table_from=table_from,
            table_to=table_to,
            extra_sessions=sorted(extra),
            observed=observed,
            closed_overrides=closed,
        )


# ── 從 store 建日曆 ─────────────────────────────────────────────────────────────


def us_calendar(store: Any, *, symbol: str = US_OBSERVED_SYMBOL, market_key: str = "us") -> Calendar:
    """美股：程式內的 NYSE 表＋store 裡 SPY 的實際成交日＋覆寫。"""
    observed = [row[0] for row in store.closes(market_key, symbol)]
    return Calendar(
        "us",
        observed=observed,
        holidays=NYSE_HOLIDAYS,
        overrides=store.calendar_overrides("us"),
        supported_until=NYSE_SUPPORTED_UNTIL,
        supported_from=NYSE_TABLE_FROM,
        early_closes=NYSE_EARLY_CLOSE,
    )


def classify_twse_holiday(name: Any, note: Any) -> str:
    """休市表一列的分類（PLAN §18.4）：剛好符合一類才算，兩類以上或都不符合 → unknown。"""
    text = unicodedata.normalize("NFKC", f"{name or ''}{note or ''}")
    text = "".join(text.split())
    kinds = {kind for kind, words in _TW_RULES if any(word in text for word in words)}
    return kinds.pop() if len(kinds) == 1 else TW_HOLIDAY_UNKNOWN


def _holiday_fields(row: Any) -> tuple[date, str, str, Optional[str]]:
    if isinstance(row, Mapping):
        day, name, note, kind = row.get("date"), row.get("name", ""), row.get("note", ""), row.get("kind")
    elif hasattr(row, "date") and hasattr(row, "name"):
        day, name, note, kind = row.date, row.name, getattr(row, "note", ""), getattr(row, "kind", None)
    else:
        values = tuple(row)
        day, name, note = values[0], values[1], (values[2] if len(values) > 2 else "")
        kind = values[3] if len(values) > 3 else None
    return _as_date(day), str(name or ""), str(note or ""), (kind if kind in _TW_KINDS else None)


def twse_holiday_rows(store: Any) -> tuple[list[dict], list[int]]:
    """market_pipeline_tw 存的休市表（obs `tw.holidays.<年>`）→ (列, 抓到的年份)。只失敗過的年份不算抓到。"""
    rows: list[dict] = []
    years: list[int] = []
    for obs_id in store.obs_ids(TW_HOLIDAY_OBS_PREFIX):
        suffix = obs_id[len(TW_HOLIDAY_OBS_PREFIX):]
        if not suffix.isdigit():
            continue
        obs = store.get_obs(obs_id) or {}
        payload = obs.get("payload")
        items = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            continue
        years.append(int(suffix))
        rows.extend(item for item in items if isinstance(item, Mapping))
    return rows, sorted(years)


def tw_calendar(
    store: Any,
    holidays_rows: Optional[Iterable[Any]] = None,
    *,
    years: Optional[Iterable[int]] = None,
    symbol: str = TW_OBSERVED_SYMBOL,
    market_key: str = "tw",
) -> Calendar:
    """台股：證交所休市表（依說明分類）＋store 裡加權指數的實際成交日＋覆寫。

    holidays_rows 沒給就讀 market_pipeline_tw 存的 obs；years＝抓到休市表的年份（沒給就用列的年份）。
    列可以是 dict（date、name、note、kind）、market_sources_tw.Holiday 或 (date, name, note[, kind])；
    有合法的 kind 就用它，沒有就照同一套規則分類。
    """
    if holidays_rows is None:
        holidays_rows, fetched = twse_holiday_rows(store)
        years = fetched if years is None else years
    closed: dict[date, str] = {}
    trading: set[date] = set()
    unclassified: set[date] = set()
    kinds_by_day: dict[date, set[str]] = {}
    names: dict[date, str] = {}
    for row in holidays_rows:
        day, name, note, kind = _holiday_fields(row)
        kinds_by_day.setdefault(day, set()).add(kind or classify_twse_holiday(name, note))
        names.setdefault(day, name)
    for day, kinds in kinds_by_day.items():
        kind = kinds.pop() if len(kinds) == 1 else TW_HOLIDAY_UNKNOWN  # 同一天兩種說法：不猜
        if kind == TW_HOLIDAY_TRADING:
            trading.add(day)
        elif kind in (TW_HOLIDAY_CLOSED, TW_HOLIDAY_SETTLEMENT_ONLY):
            closed[day] = names[day]
        else:
            unclassified.add(day)
    year_set = sorted({int(y) for y in years} if years is not None else {d.year for d in kinds_by_day})
    observed = [row[0] for row in store.closes(market_key, symbol)]
    return Calendar(
        "tw",
        observed=observed,
        holidays=closed,
        overrides=store.calendar_overrides("tw"),
        supported_until=date(year_set[-1], 12, 31) if year_set else None,
        supported_from=date(year_set[0], 1, 1) if year_set else None,
        table_years=year_set,
        trading_days=trading,
        unclassified=unclassified,
    )


def trading_calendars(store: Any) -> Any:
    """publish 用：market_calc.Calendars(nyse=…, twse=…)，和 collect 的 ctx.us_cal／tw_cal 同一份輸入。"""
    import market_calc  # noqa: PLC0415

    return market_calc.Calendars(
        nyse=us_calendar(store).to_trading_calendar(), twse=tw_calendar(store).to_trading_calendar()
    )


# ── 最近一個收完的交易日 ────────────────────────────────────────────────────────


def latest_closed_us_session(cal: Calendar, now: Any) -> Optional[date]:
    """美東收盤＋30 分鐘之後才算那天（PLAN §3.1 的 D）。日曆不知道今天開不開、又可能已經收完 → None。"""
    if cal.market != "us":
        raise ValueError("latest_closed_us_session needs the US calendar")
    moment = _as_utc(now)
    day = moment.astimezone(US_TZ).date()
    for _ in range(_MAX_GAP_DAYS + 1):
        state = cal.is_session(day)
        if state is None:
            # 不知道這天有沒有開：最早的收盤是早收 13:00，那之前這天一定還沒收完，可以往前找
            if moment < datetime.combine(day, US_EARLY_CLOSE, tzinfo=US_TZ) + US_READY_AFTER:
                day -= timedelta(days=1)
                continue
            return None
        if state and moment >= cal.close_time(day) + US_READY_AFTER:
            return day
        day -= timedelta(days=1)
    return None


def latest_tw_session(cal: Calendar, now: Any) -> Optional[date]:
    """台北 14:30 之後才算當天（PLAN §3.2 的 T）；更早就是前一個交易日。"""
    if cal.market != "tw":
        raise ValueError("latest_tw_session needs the TW calendar")
    moment = _as_utc(now)
    day = moment.astimezone(TW_TZ).date()
    for _ in range(_MAX_GAP_DAYS + 1):
        ready = datetime.combine(day, TW_READY, tzinfo=TW_TZ)
        state = cal.is_session(day)
        if state is None:
            if moment < ready:
                day -= timedelta(days=1)
                continue
            return None
        if state and moment >= ready:
            return day
        day -= timedelta(days=1)
    return None


# ── Nasdaq market-info 核對（PLAN §18.3） ───────────────────────────────────────

_INFO_FIELDS = {
    "is_business_day": ("is_business_day", "isBusinessDay"),
    "previous": ("previous_trade_date", "prev_trade_date", "previousTradeDate"),
    "next": ("next_trade_date", "nextTradeDate"),
    "closing": ("closing_time", "market_closing_time", "marketClosingTime", "close_time"),
}


def _info_value(info: Any, key: str) -> Any:
    for name in _INFO_FIELDS[key]:
        if isinstance(info, Mapping):
            if info.get(name) is not None:
                return info[name]
        elif getattr(info, name, None) is not None:
            return getattr(info, name)
    return None


def _info_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    return None


def _info_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None and value.utcoffset() is not None else None
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(" ET"):
            try:
                return datetime.strptime(text[:-3], "%b %d, %Y %I:%M %p").replace(tzinfo=US_TZ)
            except ValueError:
                return None
    return None


def check_market_info(cal: Calendar, info: Any, now: Any) -> list[str]:
    """拿 Nasdaq market-info 核對 NYSE 表，回不一致的種類（空 list＝一致或今天不核對）。

    以美東「今天」T 為準：isBusinessDay ⇔ T 是交易日；previousTradeDate＝prev_session(T)；
    nextTradeDate＝next_session(T)；marketClosingTime＝表內收盤（早收 13:00）。週末回應的語意
    還沒用 fixture 確認，週末不核對。日曆不知道的欄位不比（例如表最後一天的下一個交易日）。
    """
    if cal.market != "us":
        raise ValueError("check_market_info needs the US calendar")
    today = _as_utc(now).astimezone(US_TZ).date()
    if today.weekday() >= 5:
        return []
    session = cal.is_session(today)
    if session is None:
        return [MISMATCH_UNSUPPORTED]
    kinds: list[str] = []
    incomplete = False
    business = _info_value(info, "is_business_day")
    if isinstance(business, bool):
        if business != session:
            kinds.append(MISMATCH_BUSINESS_DAY)
    else:
        incomplete = True
    for key, expected, kind in (
        ("previous", cal.prev_session(today), MISMATCH_PREVIOUS),
        ("next", cal.next_session(today), MISMATCH_NEXT),
    ):
        got = _info_date(_info_value(info, key))
        if got is None:
            incomplete = True
        elif expected is not None and got != expected:
            kinds.append(kind)
    if session:
        closing = _info_time(_info_value(info, "closing"))
        if closing is None:
            incomplete = True
        elif closing != cal.close_time(today):
            kinds.append(MISMATCH_CLOSING)
    if incomplete:
        kinds.append(MISMATCH_INCOMPLETE)
    return kinds
