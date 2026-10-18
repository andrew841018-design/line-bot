"""market_calendar（/盤勢 的 NYSE／證交所交易日曆）測試。

鎖住官方表、判斷順序（有成交→覆寫→官方表→表以前看實際成交→未知）、收盤＋30 分鐘的 D、
台股 14:30 的 T、夏令／冬令、早收、颱風覆寫、休市表分類、market-info 核對。時間一律注入。
休市表的列照 fixtures_raw/twse_holiday_2026.json 的格式裁小。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import market_calendar as mc
from market_calendar import Calendar
from market_store import Store

ET = mc.US_TZ
TPE = mc.TW_TZ


def _d(text: str) -> date:
    return date.fromisoformat(text)


def _weekdays(start: str, end: str, skip: tuple[str, ...] = ()) -> list[date]:
    day, last, out = _d(start), _d(end), []
    while day <= last:
        if day.weekday() < 5 and day.isoformat() not in skip:
            out.append(day)
        day += timedelta(days=1)
    return out


def _et(*args: int) -> datetime:
    return datetime(*args, tzinfo=ET)


def _tpe(*args: int) -> datetime:
    return datetime(*args, tzinfo=TPE)


# 2025 年 9–12 月 SPY 實際有成交的日子（勞動節 9/1、感恩節 11/27、聖誕節 12/25 休市）
SPY_2025 = _weekdays("2025-09-02", "2025-12-31", skip=("2025-11-27", "2025-12-25"))

# 證交所 2026 年休市表（裁自 fixtures_raw/twse_holiday_2026.json，欄位：日期、名稱、說明）
TW_ROWS_2026 = [
    ["2026-01-01", "中華民國開國紀念日", "依規定放假1日。"],
    ["2026-01-02", "國曆新年開始交易日", "國曆新年開始交易。"],
    ["2026-02-11", "農曆春節前最後交易日", "農曆春節前最後交易。\r\n"],
    ["2026-02-12", "市場無交易，僅辦理結算交割作業", ""],
    ["2026-02-13", "市場無交易，僅辦理結算交割作業", ""],
    ["2026-02-16", "農曆除夕及春節", "依規定於2月15日至2月19日放假5日。"],
    ["2026-02-17", "農曆除夕及春節", "依規定於2月15日至2月19日放假5日。\r\n\r\n\r\n"],
    ["2026-02-18", "農曆除夕及春節", "依規定於2月15日至2月19日放假5日。"],
    ["2026-02-19", "農曆除夕及春節", "依規定於2月15日至2月19日放假5日。"],
    ["2026-02-20", "農曆除夕及春節", "2月15日適逢星期日，於2月20日（星期五）補假。"],
    ["2026-02-23", "農曆春節後開始交易日", "農曆春節後開始交易。"],
    ["2026-02-27", "和平紀念日", "和平紀念日為2月28日適逢星期六，於2月27日（星期五）補假。"],
    ["2026-10-09", "國慶日", "國慶日為10月10日適逢星期六，於10月9日（星期五）補假。"],
    ["2026-10-10", "國慶日", "依規定放假1日。"],
    ["2026-12-25", "行憲紀念日", "依規定放假1日。"],
]


@dataclass
class Holiday:  # 和 market_sources_tw.Holiday 同樣的欄位
    date: date
    name: str
    note: str
    kind: str = "unknown"


@pytest.fixture
def store(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    s = Store(state / "market_history.sqlite", now_fn=lambda: 1_790_000_000.0)
    yield s
    s.close()


def _tw(observed=(), overrides=None, rows=TW_ROWS_2026, years=(2026,)) -> Calendar:
    class _FakeStore:
        def closes(self, market, symbol):
            assert (market, symbol) == ("tw", "TAIEX")
            return [(d, 1.0, None, "finmind", False) for d in observed]

        def calendar_overrides(self, market):
            return overrides or {}

    return mc.tw_calendar(_FakeStore(), rows, years=years)


# ── NYSE 官方表 ─────────────────────────────────────────────────────────────────


def test_nyse_table_is_the_official_copy():
    assert sorted(mc.NYSE_HOLIDAYS) == [_d(x) for x in (
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
        "2026-09-07", "2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
        "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    )]
    assert mc.NYSE_EARLY_CLOSE == {_d("2026-11-27"): time(13), _d("2026-12-24"): time(13), _d("2027-11-26"): time(13)}
    assert mc.NYSE_SUPPORTED_UNTIL == _d("2027-12-31")


@pytest.mark.parametrize(
    "day,expected",
    [
        ("2026-11-26", False),  # 感恩節
        ("2026-11-27", True),  # 早收
        ("2026-06-19", False),  # Juneteenth
        ("2027-06-18", False),  # Juneteenth（observed）
        ("2026-07-03", False),
        ("2027-07-05", False),
        ("2027-12-24", False),  # 聖誕節 observed
        ("2027-12-31", True),  # 2028 元旦是週六，不補假
        ("2026-01-02", True),
        ("2026-10-17", False),  # 週六
        ("2028-01-03", None),  # 表外
        ("2028-01-01", False),  # 表外的週末：NYSE 不會開
        ("2025-12-15", None),  # 表以前、又沒有 SPY 資料
    ],
)
def test_us_sessions_from_the_table(day, expected):
    assert Calendar("us").is_session(_d(day)) is expected


def test_us_close_times_and_early_close():
    cal = Calendar("us")
    assert cal.close_time(_d("2026-10-16")) == _et(2026, 10, 16, 16, 0)
    assert cal.close_time(_d("2026-11-27")) == _et(2026, 11, 27, 13, 0)
    assert cal.close_time(_d("2026-12-24")) == _et(2026, 12, 24, 13, 0)
    assert cal.is_early_close(_d("2026-11-27")) and not cal.is_early_close(_d("2026-11-30"))
    assert cal.close_time(_d("2026-11-26")) is None
    assert cal.close_time(_d("2028-01-03")) is None
    # 只給 INTERFACES 的參數也要有早收（別的模組會這樣建）
    plain = Calendar("us", observed=(), holidays=mc.NYSE_HOLIDAYS, overrides={}, supported_until=_d("2027-12-31"))
    assert plain.close_time(_d("2026-11-27")) == _et(2026, 11, 27, 13, 0)
    assert plain.is_session(_d("2025-12-15")) is None


def test_us_before_the_table_uses_spy_dates():
    cal = Calendar("us", observed=SPY_2025)
    assert cal.is_session(_d("2025-11-27")) is False
    assert cal.is_session(_d("2025-11-28")) is True
    assert cal.is_session(_d("2025-09-01")) is None  # SPY 資料從 9/2 開始：更早的不知道
    assert cal.prev_session(_d("2026-01-02")) == _d("2025-12-31")
    assert Calendar("us").prev_session(_d("2026-01-02")) is None
    assert cal.sessions_back(_d("2026-01-05"), 4) == [_d("2025-12-30"), _d("2025-12-31"), _d("2026-01-02"), _d("2026-01-05")]


def test_us_missing_spy_bar_inside_the_table_is_still_a_session():
    """只有我們沒抓到資料不算休市（PLAN §3.1「來源漏日」、§18.3「只有 Nasdaq 缺列不算」）。"""
    observed = _weekdays("2026-10-01", "2026-10-16", skip=("2026-10-14",))
    cal = Calendar("us", observed=observed)
    assert cal.is_session(_d("2026-10-14")) is True
    assert cal.missing_bars(_d("2026-10-01"), _d("2026-10-16")) == [_d("2026-10-14")]


def test_closed_override_then_a_bar_turns_it_back_into_a_session():
    day = _d("2026-10-14")
    overrides = {day: {"kind": "closed", "evidence": "market_info"}}
    assert Calendar("us", overrides=overrides).is_session(day) is False
    assert Calendar("us", overrides=overrides).missing_bars(day, day) == []
    assert Calendar("us", observed=[day], overrides=overrides).is_session(day) is True  # 事實優先
    assert Calendar("us", overrides={day: {"kind": "revoked"}}).is_session(day) is True
    assert Calendar("us", overrides={_d("2026-11-26"): "open"}).is_session(_d("2026-11-26")) is True


def test_us_weekend_rows_are_ignored():
    cal = Calendar("us", observed=[_d("2026-10-17")])
    assert cal.is_session(_d("2026-10-17")) is False


def test_prev_next_back_and_forward():
    cal = Calendar("us")
    assert cal.next_session(_d("2026-11-25")) == _d("2026-11-27")
    assert cal.prev_session(_d("2026-11-27")) == _d("2026-11-25")
    assert cal.sessions_back(_d("2026-11-30"), 3) == [_d("2026-11-25"), _d("2026-11-27"), _d("2026-11-30")]
    assert cal.sessions_back(_d("2026-11-28"), 2) == [_d("2026-11-25"), _d("2026-11-27")]  # 週六：從前一個交易日算
    assert cal.sessions_back(_d("2026-01-06"), 10) == [_d("2026-01-02"), _d("2026-01-05"), _d("2026-01-06")]
    assert cal.sessions_back(_d("2028-01-03"), 3) == []
    assert cal.sessions_back(_d("2026-11-30"), 0) == []
    assert cal.sessions_forward(_d("2026-11-24"), 3) == [_d("2026-11-25"), _d("2026-11-27"), _d("2026-11-30")]
    assert cal.next_session(_d("2027-12-31")) is None
    assert cal.sessions_forward(_d("2027-12-29"), 4) == [_d("2027-12-30"), _d("2027-12-31")]


# ── 美股 D（收盤＋30 分鐘） ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "now,expected",
    [
        # 夏令開始後的週一（3/8 起 EDT）
        (_et(2026, 3, 9, 16, 29), "2026-03-06"),
        (_et(2026, 3, 9, 16, 31), "2026-03-09"),
        # 冬令開始後的週一（11/1 起 EST）
        (_et(2026, 11, 2, 16, 29), "2026-10-30"),
        (_et(2026, 11, 2, 16, 31), "2026-11-02"),
        # 台北午夜與 04:00（夏令：04:00 台北＝16:00 美東，還沒過 16:30）
        (_tpe(2026, 10, 17, 0, 0), "2026-10-15"),
        (_tpe(2026, 10, 17, 4, 0), "2026-10-15"),
        (_tpe(2026, 10, 17, 4, 31), "2026-10-16"),
        # 冬令：04:00 台北＝15:00 美東、05:29＝16:29、05:31＝16:31
        (_tpe(2026, 12, 1, 0, 0), "2026-11-27"),
        (_tpe(2026, 12, 1, 4, 0), "2026-11-27"),
        (_tpe(2026, 12, 1, 5, 29), "2026-11-27"),
        (_tpe(2026, 12, 1, 5, 31), "2026-11-30"),
        # 早收：13:00 收、13:30 之後才算
        (_et(2026, 11, 27, 13, 29), "2026-11-25"),
        (_et(2026, 11, 27, 13, 31), "2026-11-27"),
        # 假日、週末
        (_et(2026, 11, 26, 20, 0), "2026-11-25"),
        (_et(2026, 10, 18, 12, 0), "2026-10-16"),
        # 表外：今天還沒到最早的收盤時刻 → 往前找；之後就不知道
        (_et(2028, 1, 3, 10, 0), "2027-12-31"),
        (_et(2028, 1, 2, 18, 0), "2027-12-31"),
        (_et(2028, 1, 3, 18, 0), None),
    ],
)
def test_latest_closed_us_session(now, expected):
    got = mc.latest_closed_us_session(Calendar("us"), now)
    assert got == (None if expected is None else _d(expected))


def test_latest_closed_us_session_takes_epoch_seconds():
    now = _et(2026, 10, 16, 16, 31).timestamp()
    assert mc.latest_closed_us_session(Calendar("us"), now) == _d("2026-10-16")
    with pytest.raises(ValueError):
        mc.latest_closed_us_session(Calendar("us"), datetime(2026, 10, 16, 16, 31))  # 沒有時區
    with pytest.raises(ValueError):
        mc.latest_closed_us_session(_tw(), now)


# ── 台股 ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,note,kind",
    [
        ("國曆新年開始交易日", "國曆新年開始交易。", "trading"),
        ("農曆春節前最後交易日", "農曆春節前最後交易。\r\n", "trading"),
        ("市場無交易，僅辦理結算交割作業", "", "settlement_only"),
        ("中華民國開國紀念日", "依規定放假1日。", "closed"),
        ("和平紀念日", "和平紀念日為2月28日適逢星期六，於2月27日（星期五）補假。", "closed"),
        ("颱風", "全日休市", "closed"),
        ("某某紀念日", "另行公告", "unknown"),
        ("開始交易日", "當日放假", "unknown"),  # 兩類都符合：不猜
    ],
)
def test_classify_twse_holiday(name, note, kind):
    assert mc.classify_twse_holiday(name, note) == kind


@pytest.mark.parametrize(
    "day,expected",
    [
        ("2026-01-01", False),
        ("2026-01-02", True),  # 開始交易日
        ("2026-02-11", True),  # 最後交易日
        ("2026-02-12", False),  # 無交易，僅結算交割
        ("2026-02-20", False),
        ("2026-02-23", True),
        ("2026-02-27", False),
        ("2026-10-09", False),
        ("2026-10-16", True),  # 一般平日
        ("2026-10-17", False),  # 週六
        ("2027-03-01", None),  # 2027 的表還沒抓到
        ("2025-06-02", None),  # 表以前、又沒有加權指數資料
    ],
)
def test_tw_calendar_from_holiday_rows(day, expected):
    assert _tw().is_session(_d(day)) is expected


def test_tw_rows_accept_dicts_objects_and_given_kinds():
    rows = [
        {"date": "2026-01-02", "name": "國曆新年開始交易日", "note": "", "kind": "trading"},
        Holiday(_d("2026-03-02"), "某某", "看不懂的說明"),  # kind=unknown → 那天未知
        Holiday(_d("2026-03-03"), "某某", "看不懂的說明", kind="closed"),  # sources 已經分好類
        (_d("2026-03-04"), "勞動節", "依規定放假1日。"),
    ]
    cal = _tw(rows=rows)
    assert cal.is_session(_d("2026-01-02")) is True
    assert cal.is_session(_d("2026-03-02")) is None
    assert cal.unclassified == [_d("2026-03-02")]
    assert cal.is_session(_d("2026-03-03")) is False
    assert cal.is_session(_d("2026-03-04")) is False
    assert cal.sessions_back(_d("2026-03-05"), 3) == [_d("2026-03-05")]  # 往前碰到未知就停
    # 有成交蓋過看不懂的說明
    assert _tw(observed=[_d("2026-03-02")], rows=rows).is_session(_d("2026-03-02")) is True


def test_tw_same_day_with_two_meanings_is_unknown():
    rows = [["2026-03-02", "開始交易日", "開始交易。"], ["2026-03-02", "放假", "依規定放假1日。"]]
    assert _tw(rows=rows).is_session(_d("2026-03-02")) is None


def test_tw_missing_taiex_bar_is_still_a_session_until_a_typhoon_override():
    day = _d("2026-10-14")
    observed = _weekdays("2026-10-01", "2026-10-16", skip=("2026-10-09", "2026-10-14"))
    assert _tw(observed=observed).is_session(day) is True  # 只有我們沒抓到：來源漏日
    assert _tw(observed=observed).missing_bars(_d("2026-10-01"), _d("2026-10-16")) == [day]
    typhoon = {day: {"kind": "closed", "evidence": "allbut0999_empty+finmind_empty", "confirmed": False}}
    assert _tw(observed=observed, overrides=typhoon).is_session(day) is False
    assert _tw(observed=[*observed, day], overrides=typhoon).is_session(day) is True  # 之後看到資料＝撤銷
    assert _tw(observed=observed, overrides={day: {"kind": "revoked"}}).is_session(day) is True


def test_tw_before_the_table_follows_taiex_and_unfetched_years_are_unknown():
    observed = _weekdays("2025-06-02", "2025-12-31", skip=("2025-10-10",))
    cal = _tw(observed=observed)
    assert cal.is_session(_d("2025-10-10")) is False  # 表以前：沒有成交＝沒開
    assert cal.is_session(_d("2025-10-13")) is True
    assert cal.is_session(_d("2027-01-04")) is None
    assert cal.supported_until == _d("2026-12-31") and cal.table_years == [2026]
    gap = _tw(rows=[*TW_ROWS_2026, ["2028-01-03", "開始交易日", "開始交易。"]], years=(2026, 2028))
    assert gap.is_session(_d("2027-06-01")) is None  # 中間沒抓到的年份
    assert gap.is_session(_d("2028-01-03")) is True


def test_latest_tw_session():
    cal = _tw()
    assert mc.latest_tw_session(cal, _tpe(2026, 10, 16, 14, 29)) == _d("2026-10-15")
    assert mc.latest_tw_session(cal, _tpe(2026, 10, 16, 14, 31)) == _d("2026-10-16")
    assert mc.latest_tw_session(cal, _tpe(2026, 10, 9, 15, 0)) == _d("2026-10-08")  # 補假
    assert mc.latest_tw_session(cal, _tpe(2026, 2, 23, 10, 0)) == _d("2026-02-11")  # 春節連假
    assert mc.latest_tw_session(cal, _tpe(2026, 2, 23, 14, 30)) == _d("2026-02-23")
    typhoon = _tw(overrides={_d("2026-07-29"): {"kind": "closed"}})
    assert mc.latest_tw_session(typhoon, _tpe(2026, 7, 29, 15, 0)) == _d("2026-07-28")
    assert mc.latest_tw_session(cal, _tpe(2027, 1, 4, 10, 0)) is None  # 2027 表還沒抓到
    assert cal.close_time(_d("2026-10-16")) == _tpe(2026, 10, 16, 13, 30)
    with pytest.raises(ValueError):
        mc.latest_tw_session(Calendar("us"), _tpe(2026, 10, 16, 15, 0))


# ── 從 store 建日曆 ─────────────────────────────────────────────────────────────


def test_us_calendar_reads_spy_and_overrides_from_the_store(store: Store):
    store.upsert_closes("us", "SPY", [(d, 600.0, 1) for d in SPY_2025], "nasdaq")
    store.set_calendar_override("us", _d("2026-10-14"), "closed", "market_info")
    cal = mc.us_calendar(store)
    assert cal.is_session(_d("2025-11-27")) is False and cal.is_session(_d("2025-11-28")) is True
    assert cal.is_session(_d("2026-10-14")) is False
    assert cal.observed_until == _d("2025-12-31")


def test_tw_calendar_reads_the_pipeline_obs(store: Store):
    rows = [{"date": r[0], "name": r[1], "note": r[2], "kind": mc.classify_twse_holiday(r[1], r[2])} for r in TW_ROWS_2026]
    store.put_obs("tw.holidays.2026", {"year": 2026, "rows": rows, "unknown": []}, data_date=None, period="2026")
    store.obs_checked("tw.holidays.2027", error_kind="empty")  # 只失敗過：不算抓到
    store.upsert_closes("tw", "TAIEX", [(_d("2025-12-31"), 28_000.0, None)], "finmind")
    cal = mc.tw_calendar(store)
    assert cal.table_years == [2026]
    assert cal.is_session(_d("2026-02-12")) is False and cal.is_session(_d("2026-02-11")) is True
    assert cal.is_session(_d("2027-01-04")) is None
    assert cal.is_session(_d("2025-12-31")) is True


def test_trading_calendars_match_this_calendar(store: Store):
    """publish 用的 market_calc.TradingCalendar 和這裡的判斷要一致（表內、表以前）。"""
    market_calc = pytest.importorskip("market_calc")
    store.upsert_closes("us", "SPY", [(d, 600.0, 1) for d in SPY_2025] + [(_d("2026-11-26"), 600.0, 1)], "nasdaq")
    store.set_calendar_override("us", _d("2026-10-14"), "closed", "market_info")
    rows = [{"date": r[0], "name": r[1], "note": r[2]} for r in TW_ROWS_2026]
    store.put_obs("tw.holidays.2026", {"year": 2026, "rows": rows}, data_date=None)
    store.set_calendar_override("tw", _d("2026-07-29"), "closed", "allbut0999_empty+finmind_empty")
    store.upsert_closes("tw", "TAIEX", [(d, 1.0, None) for d in _weekdays("2025-11-03", "2025-12-31")], "finmind")
    both = mc.trading_calendars(store)
    assert isinstance(both, market_calc.Calendars)
    pairs = ((mc.us_calendar(store), both.nyse), (mc.tw_calendar(store), both.twse))
    for mine, theirs in pairs:
        day = _d("2025-11-03")
        while day <= _d("2026-12-31"):
            expected = mine.is_session(day)
            if expected is not None:
                assert theirs.is_session(day) is expected, (mine.market, day)
            day += timedelta(days=1)
    assert both.nyse.is_session(_d("2026-11-26")) is True  # 表上休市卻有成交：事實優先
    assert both.nyse.close_at(_d("2026-11-27")) == _et(2026, 11, 27, 13, 0)


def test_trading_calendar_without_any_tw_holiday_table(store: Store):
    """bootstrap 時休市表還沒抓到：兩份日曆都退回「加權指數有成交的日子」，而且結果一樣。"""
    pytest.importorskip("market_calc")
    observed = _weekdays("2026-09-01", "2026-10-16", skip=("2026-09-25", "2026-09-28", "2026-10-09"))
    store.upsert_closes("tw", "TAIEX", [(d, 1.0, None) for d in observed], "finmind")
    store.set_calendar_override("tw", _d("2026-10-19"), "closed", "allbut0999_empty+finmind_empty")
    mine = mc.tw_calendar(store)
    theirs = mc.trading_calendars(store).twse
    assert mine.table_years == [] and mine.is_session(_d("2026-09-25")) is False
    assert mine.is_session(_d("2026-10-16")) is True and mine.is_session(_d("2026-10-20")) is None
    day = _d("2026-08-25")
    while day <= _d("2026-10-25"):
        assert theirs.is_session(day) is mine.is_session(day), day
        day += timedelta(days=1)


# ── market-info 核對 ────────────────────────────────────────────────────────────

# fixtures_raw/nasdaq_market_info.json：美東 10/16（五）晚上抓的回應
INFO_1016 = SimpleNamespace(
    closing_time=_et(2026, 10, 16, 16, 0),
    previous_trade_date=_d("2026-10-15"),
    next_trade_date=_d("2026-10-19"),
    is_business_day=True,
)
NOW_1016 = _et(2026, 10, 16, 22, 17)


# fixtures_raw/nasdaq_market_info.json 原樣（美東 10/16 週五晚上抓的；只有市場時間，沒有個人資料）
MARKET_INFO_PAYLOAD = {
    "data": {
        "country": "U.S.", "marketIndicator": "Market Closed", "uiMarketIndicator": "Market Closed",
        "marketCountDown": "Market Opens in 2D 11H 13M", "preMidOpeningTime": None, "preMidClosingTime": None,
        "postMidOpeningTime": None, "postMidClosingTime": None,
        "preMarketOpeningTime": "Oct 16, 2026 04:00 AM ET", "preMarketClosingTime": "Oct 16, 2026 09:30 AM ET",
        "marketOpeningTime": "Oct 16, 2026 09:30 AM ET", "marketClosingTime": "Oct 16, 2026 04:00 PM ET",
        "afterHoursMarketOpeningTime": "Oct 16, 2026 04:00 PM ET",
        "afterHoursMarketClosingTime": "Oct 16, 2026 08:00 PM ET",
        "previousTradeDate": "Oct 15, 2026", "nextTradeDate": "Oct 19, 2026", "isBusinessDay": True,
        "mrktStatus": "Closed", "mrktCountDown": "Opens in 2D 11H 13M", "pmOpenRaw": "2026-10-16T04:00:00",
        "ahCloseRaw": "2026-10-16T20:00:00", "openRaw": "2026-10-16T09:30:00", "closeRaw": "2026-10-16T16:00:00",
    },
    "message": None,
    "status": {"rCode": 200, "bCodeMessage": None, "developerMessage": None},
}


def test_check_market_info_with_the_us_sources_parser():
    """market_sources_us.parse_nasdaq_market_info 的 MarketInfo（closing_time、previous_trade_date、
    next_trade_date、is_business_day、opening_time）直接交給 check_market_info。"""
    import market_sources_us

    info = market_sources_us.parse_nasdaq_market_info(MARKET_INFO_PAYLOAD)
    assert mc.check_market_info(Calendar("us"), info, NOW_1016) == []
    shifted = json.loads(json.dumps(MARKET_INFO_PAYLOAD))
    shifted["data"]["previousTradeDate"] = "Oct 14, 2026"
    shifted["data"]["isBusinessDay"] = False
    info = market_sources_us.parse_nasdaq_market_info(shifted)
    assert mc.check_market_info(Calendar("us"), info, NOW_1016) == ["is_business_day", "previous_trade_date"]


def test_check_market_info_matches_the_fixture():
    assert mc.check_market_info(Calendar("us"), INFO_1016, NOW_1016) == []
    raw = {
        "marketClosingTime": "Oct 16, 2026 04:00 PM ET",
        "previousTradeDate": "Oct 15, 2026",
        "nextTradeDate": "Oct 19, 2026",
        "isBusinessDay": True,
    }
    assert mc.check_market_info(Calendar("us"), raw, NOW_1016.timestamp()) == []


@pytest.mark.parametrize(
    "change,kinds",
    [
        ({"is_business_day": False}, ["is_business_day"]),
        ({"previous_trade_date": _d("2026-10-14")}, ["previous_trade_date"]),
        ({"next_trade_date": _d("2026-10-20")}, ["next_trade_date"]),
        ({"closing_time": _et(2026, 10, 16, 13, 0)}, ["closing_time"]),
        ({"closing_time": None}, ["info_incomplete"]),
        ({"is_business_day": "yes"}, ["info_incomplete"]),
    ],
)
def test_check_market_info_reports_each_mismatch(change, kinds):
    info = SimpleNamespace(**{**vars(INFO_1016), **change})
    assert mc.check_market_info(Calendar("us"), info, NOW_1016) == kinds


def test_check_market_info_early_close_holiday_weekend_and_unsupported():
    early = SimpleNamespace(
        closing_time=_et(2026, 11, 27, 13, 0), previous_trade_date=_d("2026-11-25"),
        next_trade_date=_d("2026-11-30"), is_business_day=True,
    )
    assert mc.check_market_info(Calendar("us"), early, _et(2026, 11, 27, 18, 0)) == []
    holiday = SimpleNamespace(
        closing_time=_et(2026, 11, 25, 16, 0), previous_trade_date=_d("2026-11-25"),
        next_trade_date=_d("2026-11-27"), is_business_day=False,
    )
    assert mc.check_market_info(Calendar("us"), holiday, _et(2026, 11, 26, 12, 0)) == []
    assert mc.check_market_info(Calendar("us"), INFO_1016, _et(2026, 10, 17, 12, 0)) == []  # 週末不核對
    assert mc.check_market_info(Calendar("us"), INFO_1016, _et(2028, 1, 4, 12, 0)) == ["calendar_unsupported"]
    # 新的臨時休市：market-info 說今天不是交易日
    assert mc.check_market_info(
        Calendar("us"), SimpleNamespace(**{**vars(INFO_1016), "is_business_day": False}), NOW_1016
    ) == ["is_business_day"]
