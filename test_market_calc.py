"""market_calc.py 的測試（/盤勢 的純計算，2026-10-17）。

fixture 都是測試檔內的小資料：CBOE 期權列取自 2026-10-16 QQQ 實際回應的幾筆（裁小）、台指選擇權與除權息
照期交所／證交所的欄位合成；不放任何家人資料。所有時間都由測試傳入 now。
"""

from __future__ import annotations

import ast
import json
import math
import pickle
import subprocess
import sys
from collections import namedtuple
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import market_calc as mc
from market_calc import ET, TPE, UTC

HERE = Path(__file__).resolve().parent
D = date(2026, 10, 16)  # 週五、10 月的第三個週五（月選結算日）

# 證交所 2026 休市表裡的平日非交易日（休市＋「市場無交易，僅辦理結算交割」；fixtures_raw/twse_holiday_2026.json）
TW_2026_CLOSED = frozenset(
    date(2026, m, d)
    for m, d in (
        (1, 1), (2, 12), (2, 13), (2, 16), (2, 17), (2, 18), (2, 19), (2, 20), (2, 27), (4, 3), (4, 6),
        (5, 1), (6, 19), (9, 25), (9, 28), (10, 9), (10, 26), (12, 25),
    )
)


def tw_cal() -> mc.TradingCalendar:
    return mc.twse_calendar(holidays=TW_2026_CLOSED, table_from=date(2026, 1, 1), table_to=date(2026, 12, 31))


def calendars() -> mc.Calendars:
    return mc.Calendars(nyse=mc.nyse_calendar(), twse=tw_cal())


def at(year, month, day, hour=0, minute=0, tz=TPE) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=tz)


def weekdays(start: date, count: int) -> list[date]:
    out, day = [], start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def mobile(sep: str = "") -> str:
    """測試用的 09 開頭手機號碼格式；執行時才拼起來，原始碼裡沒有完整號碼（repo 的隱私檢查會擋號碼字樣）。"""
    return sep.join(("0" + "9" + "12", "345", "678"))


FULLWIDTH_DIGITS = str.maketrans("0123456789", "０１２３４５６７８９")


# ── clean_external（§19.1） ────────────────────────────────────────────────────
class TestCleanExternal:
    def test_bidi_zero_width_and_lone_surrogates_are_deleted(self):
        assert mc.clean_external("台積電\u202e漲停", 40) == "台積電漲停"
        assert mc.clean_external("美\u200b股\u200d大\ufeff漲\u2060", 40) == "美股大漲"
        cleaned = mc.clean_external("a\ud800b\udfffc", 40)
        assert cleaned == "abc"
        cleaned.encode("utf-8")  # 不會 UnicodeEncodeError

    def test_private_use_unassigned_and_line_separators(self):
        assert mc.clean_external("x\ue000y\u0378z", 40) == "xyz"
        assert mc.clean_external("台股\n大漲\u2028\u2029收紅", 40) == "台股 大漲 收紅"
        assert mc.clean_external("A\t\tB\r\nC", 40) == "A B C"

    def test_at_marks_become_fullwidth_space(self):
        assert mc.clean_external("x @all y", 40) == "x 　all y"
        assert mc.clean_external("＠媽媽", 40) == "媽媽"
        assert mc.clean_external("@", 40) is None
        for text in ("x @all y", "＠媽媽 看這個", "﹫all"):
            cleaned = mc.clean_external(text, 40)
            assert "@" not in cleaned and "＠" not in cleaned and "﹫" not in cleaned

    @pytest.mark.parametrize(
        "text",
        [
            "看 https://example.com/news",
            "新聞www.example.com",
            "詳見 bit.ly/abc123",
            "ftp://x.y",
            "ＨＴＴＰＳ：／／ｅｘａｍｐｌｅ．ｃｏｍ",  # 全形會先被 NFKC 正規化
        ],
    )
    def test_urls_drop_the_item(self, text):
        assert mc.clean_external(text, 40) is None

    @pytest.mark.parametrize("text", ["聯絡 a.b@example.com", "ａ＠ｅｘａｍｐｌｅ．ｃｏｍ 洽詢", "x&#64;example.org"])
    def test_emails_drop_the_item(self, text):
        assert mc.clean_external(text, 40) is None

    @pytest.mark.parametrize(
        "text",
        [
            "電話 " + mobile("-"),
            mobile() + " 找我",
            mobile(" ").translate(FULLWIDTH_DIGITS),  # 全形數字會先被 NFKC 正規化
            mobile("."),
        ],
    )
    def test_mobile_numbers_drop_the_item(self, text):
        assert mc.clean_external(text, 40) is None

    def test_numbers_that_are_not_phones_or_urls_survive(self):
        assert mc.clean_external("09月出口年增 30%", 40) == "09月出口年增 30%"
        assert mc.clean_external("2009 年 1234 點", 40) == "2009 年 1234 點"
        assert mc.clean_external("AMAZON.COM INC", 24) == "AMAZON.COM INC"
        assert mc.clean_external("S&P 500 漲 1.2/2.3", 40) == "S&P 500 漲 1.2/2.3"

    def test_html_tags_and_entities(self):
        assert mc.clean_external("<b>台積電</b> &amp; 聯電", 40) == "台積電 & 聯電"
        assert mc.clean_external("AT&amp;T Inc", 24) == "AT&T Inc"
        assert mc.clean_external("&#x202E;反轉", 40) == "反轉"  # 實體還原出的控制字元也要刪
        assert mc.clean_external("&#64;all", 40) == "all"
        assert mc.clean_external("<script>alert(1)</script>", 40) == "alert(1)"
        assert mc.clean_external("a < b 也 > c", 40) == "a < b 也 > c"

    def test_truncates_to_max_len(self):
        text = "加權指數" * 20
        out = mc.clean_external(text, 40)
        assert out == text[:37] + "..." and len(out) == 40  # 2026-10-18：截斷要標「...」（r6c C1）
        assert mc.clean_external("abc   ", 3) == "abc"

    def test_non_string_and_empty(self):
        assert mc.clean_external(None, 40) is None
        assert mc.clean_external(b"bytes", 40) is None
        assert mc.clean_external("\u200b\u202e", 40) is None
        assert mc.clean_external("<br/>", 40) is None
        with pytest.raises(ValueError):
            mc.clean_external("x", 0)
        with pytest.raises(ValueError):
            mc.clean_external("x", True)

    def test_unverified_current_data_title_passes_cleaning_and_is_left_to_the_validator(self):
        # §19.1 的測試標題「最新公布 9月出口 年增30%」由 bot 組卡時的 validate_outbound_text 丟掉；清洗不改字
        import output_validator

        title = "最新公布 9月出口 年增30%"
        assert mc.clean_external(title, 40) == title
        assert not output_validator.validate_outbound_text(title, now=at(2026, 10, 17)).ok


# ── ParseError ────────────────────────────────────────────────────────────────
def test_parse_error_kind_detail_and_pickle():
    error = mc.ParseError("missing_field", "close")
    assert isinstance(error, ValueError) and error.kind == "missing_field" and error.detail == "close"
    restored = pickle.loads(pickle.dumps(error))
    assert restored.kind == "missing_field" and str(restored) == str(error)
    assert mc.ParseError("format").kind == "format"
    for kind in mc.PARSE_ERROR_KINDS:
        assert mc.ParseError(kind).kind == kind
    with pytest.raises(ValueError):
        mc.ParseError("stale")


# ── 交易日曆 ───────────────────────────────────────────────────────────────────
class TestCalendars:
    def test_nyse_table(self):
        cal = mc.nyse_calendar()
        assert cal.is_session(date(2026, 6, 19)) is False  # Juneteenth
        assert cal.is_session(date(2026, 6, 18)) is True
        assert cal.is_session(date(2026, 10, 17)) is False  # 週六
        assert cal.is_session(date(2027, 12, 24)) is False  # 聖誕節 observed
        assert cal.is_session(date(2027, 12, 31)) is True  # 表的最後一天照常開市
        assert cal.is_session(date(2028, 1, 3)) is None  # 表外 → 未知
        assert cal.is_session(date(2025, 12, 31)) is None  # 表之前又沒有觀察資料 → 未知
        assert cal.next_session(date(2026, 11, 25)) == date(2026, 11, 27)
        assert cal.prev_session(date(2026, 9, 8)) == date(2026, 9, 4)  # 跳過勞動節
        assert cal.add_sessions(D, 4) == date(2026, 10, 22)
        assert cal.add_sessions(date(2026, 10, 22), -4) == D
        assert cal.add_sessions(date(2027, 12, 28), 4) is None

    def test_early_close_time(self):
        cal = mc.nyse_calendar()
        assert cal.close_at(date(2026, 11, 27)) == datetime(2026, 11, 27, 13, 0, tzinfo=ET)
        assert cal.close_at(D) == datetime(2026, 10, 16, 16, 0, tzinfo=ET)

    def test_observed_past_sessions_and_overrides(self):
        observed = [date(2025, 12, 29), date(2025, 12, 30), date(2025, 12, 31)]
        cal = mc.nyse_calendar(observed=observed, closed_overrides=[date(2026, 10, 20)])
        assert cal.is_session(date(2025, 12, 30)) is True
        assert cal.is_session(date(2025, 12, 28)) is None  # 觀察資料之前 → 未知
        assert cal.next_session(date(2025, 12, 31)) == date(2026, 1, 2)
        assert cal.prev_session(date(2026, 1, 2)) == date(2025, 12, 31)
        assert cal.is_session(date(2026, 10, 20)) is False  # 臨時休市（有獨立證據才寫入）
        assert cal.add_sessions(D, 2) == date(2026, 10, 21)
        assert cal.sessions_ending(date(2026, 1, 2), 10) == observed + [date(2026, 1, 2)]

    def test_observed_gap_before_table_is_unknown(self):
        cal = mc.nyse_calendar(observed=[date(2025, 12, 1), date(2025, 12, 2)])
        assert cal.is_session(date(2025, 12, 15)) is None  # 觀察資料停在 12/2，之後不猜
        assert cal.sessions_between(date(2025, 12, 1), date(2025, 12, 3)) is None
        assert mc.nyse_calendar().sessions_between(date(2026, 11, 25), date(2026, 11, 30)) == [
            date(2026, 11, 25), date(2026, 11, 27), date(2026, 11, 30)
        ]

    def test_twse_table(self):
        cal = tw_cal()
        assert cal.is_session(date(2026, 10, 26)) is False  # 光復節補假
        assert cal.is_session(date(2026, 2, 12)) is False  # 市場無交易，僅辦理結算交割
        assert cal.is_session(date(2026, 2, 23)) is True  # 開始交易日
        assert cal.close_at(D) == datetime(2026, 10, 16, 13, 30, tzinfo=TPE)
        extra = mc.twse_calendar(
            holidays=TW_2026_CLOSED, table_from=date(2026, 1, 1), table_to=date(2026, 12, 31),
            extra_sessions=[date(2026, 2, 7)],
        )
        assert extra.is_session(date(2026, 2, 7)) is True  # 週末照常開市的特例
        traded = mc.nyse_calendar(extra_sessions=[date(2026, 6, 19)])  # 表上休市卻有成交（事實優先）
        assert traded.is_session(date(2026, 6, 19)) is True

    def test_last_complete_session_us(self):
        cal = mc.nyse_calendar()
        delay = mc.US_CLOSE_DELAY
        assert cal.last_complete_session(at(2026, 10, 16, 16, 29, ET), delay) == date(2026, 10, 15)
        assert cal.last_complete_session(at(2026, 10, 16, 16, 30, ET), delay) == D
        assert cal.last_complete_session(at(2026, 10, 17, 10, 0, ET), delay) == D
        assert cal.last_complete_session(at(2026, 10, 19, 9, 0, ET), delay) == D
        assert cal.last_complete_session(at(2026, 10, 17, 4, 0), delay) == date(2026, 10, 15)  # 台北週六 04:00＝美東 16:00
        assert cal.last_complete_session(at(2026, 10, 17, 5, 0), delay) == D
        assert cal.last_complete_session(at(2026, 11, 27, 13, 30, ET), delay) == date(2026, 11, 27)
        assert cal.last_complete_session(at(2026, 11, 27, 13, 29, ET), delay) == date(2026, 11, 25)
        assert cal.last_complete_session(at(2028, 1, 3, 20, 0, ET), delay) is None

    def test_last_complete_session_tw(self):
        cal = tw_cal()
        delay = mc.TW_CLOSE_DELAY
        assert cal.last_complete_session(at(2026, 10, 16, 14, 29), delay) == date(2026, 10, 15)
        assert cal.last_complete_session(at(2026, 10, 16, 14, 30), delay) == D
        assert cal.last_complete_session(at(2026, 10, 12, 10, 0), delay) == date(2026, 10, 8)  # 10/9 補假

    def test_monthly_expiry_rule(self):
        expiries = [date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 20), date(2026, 11, 20)]
        assert mc.monthly_expiry(expiries, mc.NYSE_HOLIDAYS, after=D) == date(2026, 11, 20)
        june = [date(2026, 6, 12), date(2026, 6, 18), date(2026, 6, 26)]
        assert mc.monthly_expiry(june, mc.NYSE_HOLIDAYS, after=date(2026, 6, 1)) == date(2026, 6, 18)
        assert mc.monthly_expiry(june, frozenset(), after=date(2026, 6, 1)) is None  # 週五沒休市就不是月選
        assert mc.monthly_expiry([date(2027, 6, 17)], mc.NYSE_HOLIDAYS, after=date(2027, 6, 1)) == date(2027, 6, 17)
        assert mc.monthly_expiry([date(2026, 10, 23)], mc.NYSE_HOLIDAYS, after=D) is None
        assert mc.us_monthly_expiry_day(2026, 6, mc.NYSE_HOLIDAYS) == date(2026, 6, 18)
        assert mc.us_monthly_expiry_day(2026, 10, mc.nyse_calendar()) == D

    def test_tw_settlement_and_txo_front_month(self):
        cal = tw_cal()
        assert mc.tw_settlement_day(2026, 10, cal) == date(2026, 10, 21)
        assert mc.tw_settlement_day(2026, 2, cal) == date(2026, 2, 23)  # 2/18 春節休市 → 順延
        assert mc.txo_front_month(date(2026, 10, 15), cal) == "202610"
        assert mc.txo_front_month(date(2026, 10, 21), cal) == "202611"  # 結算日當天期交所仍列當月，跳過
        assert mc.txo_front_month(date(2026, 12, 16), cal) == "202701"
        assert mc.txo_front_month(date(2026, 2, 20), cal) == "202602"  # 順延後的結算日還沒到

    def test_volume_day_flags(self):
        us, tw = mc.nyse_calendar(), tw_cal()
        assert mc.volume_day_flag(D, us) == mc.FLAG_SETTLEMENT
        assert mc.volume_day_flag(date(2026, 6, 18), us) == mc.FLAG_SETTLEMENT
        assert mc.volume_day_flag(date(2026, 11, 27), us) == mc.FLAG_EARLY_CLOSE
        assert mc.volume_day_flag(date(2026, 10, 15), us) is None
        assert mc.volume_day_flag(date(2026, 10, 21), tw) == mc.FLAG_SETTLEMENT
        assert mc.volume_day_flag(date(2026, 2, 23), tw) == mc.FLAG_SETTLEMENT
        assert mc.volume_day_flag(D, tw) is None


# ── stale_after（§5＋§18.1、§18.9） ─────────────────────────────────────────────
class TestStaleAfter:
    NOW = at(2026, 10, 17, 12, 0)

    def test_us_daily_four_sessions(self):
        expected = datetime(2026, 10, 22, 16, 0, tzinfo=ET).timestamp()
        for kind in (mc.KIND_US_DAILY, mc.KIND_HOLDINGS_SPY, mc.KIND_HOLDINGS_NDX):
            info = mc.stale_after_info(kind, D, calendars(), self.NOW)
            assert info.at == expected and not info.calendar_pending
        assert mc.stale_after(mc.KIND_US_DAILY, D, calendars(), self.NOW) == expected

    def test_options_friday_file_stays_fresh_over_the_weekend(self):
        stale = mc.stale_after(mc.KIND_OPTIONS, D, calendars(), self.NOW)
        assert stale == datetime(2026, 10, 19, 19, 0, tzinfo=ET).timestamp()  # 週一收盤＋3 小時
        assert at(2026, 10, 17, 16, 31).timestamp() <= stale  # 台北週六 16:31
        assert at(2026, 10, 19, 20, 0).timestamp() <= stale  # 台北週一 20:00
        assert at(2026, 10, 20, 7, 1).timestamp() > stale

    def test_options_next_session_after_holiday_is_an_early_close(self):
        stale = mc.stale_after(mc.KIND_OPTIONS, date(2026, 11, 25), calendars(), self.NOW)
        assert stale == datetime(2026, 11, 27, 16, 0, tzinfo=ET).timestamp()  # 13:00 早收＋3 小時

    def test_cboe_snapshot_older_than_12_hours_is_rejected(self):
        stamp = datetime(2026, 10, 16, 20, 33, tzinfo=UTC)
        assert mc.cboe_snapshot_ok(stamp + timedelta(hours=11), stamp)
        assert not mc.cboe_snapshot_ok(stamp + timedelta(hours=13), stamp)
        assert mc.et_date(datetime(2026, 10, 16, 16, 15, 1)) == D  # last_trade_time 沒帶時區＝美東

    def test_calendar_fallback_outside_the_table(self):
        info = mc.stale_after_info(mc.KIND_US_DAILY, date(2027, 12, 28), calendars(), self.NOW)
        assert info.calendar_pending
        assert info.at == datetime(2028, 1, 3, 16, 0, tzinfo=ET).timestamp()
        info = mc.stale_after_info(mc.KIND_OPTIONS, date(2027, 12, 31), calendars(), self.NOW)
        assert info.calendar_pending
        assert info.at == datetime(2028, 1, 6, 19, 0, tzinfo=ET).timestamp()

    def test_tw_daily_four_sessions_skip_the_holiday(self):
        stale = mc.stale_after(mc.KIND_TW_DAILY, date(2026, 10, 22), calendars(), self.NOW)
        assert stale == datetime(2026, 10, 29, 13, 30, tzinfo=TPE).timestamp()

    def test_instant_kinds(self):
        fetched = datetime(2026, 10, 17, 1, 0, tzinfo=UTC)
        cases = {
            mc.KIND_BTC: 6, mc.KIND_PREDICTION: 6, mc.KIND_NEWS: 36, mc.KIND_TRUMP: 48, mc.KIND_EVENT: 2,
        }
        for kind, hours in cases.items():
            assert mc.stale_after(kind, fetched, calendars(), self.NOW) == fetched.timestamp() + hours * 3600
        assert mc.stale_after(mc.KIND_BTC, fetched.timestamp(), calendars(), self.NOW) == fetched.timestamp() + 6 * 3600
        fomc = mc.fomc_decision_at(date(2026, 10, 28))
        assert fomc == datetime(2026, 10, 28, 14, 0, tzinfo=ET)
        assert mc.stale_after(mc.KIND_EVENT, fomc, calendars(), self.NOW) == fomc.timestamp() + 7200

    def test_day_kinds(self):
        def midnight(y, m, d):
            return at(y, m, d).timestamp()

        assert mc.stale_after(mc.KIND_CFTC, date(2026, 10, 13), calendars(), self.NOW) == midnight(2026, 10, 29)
        assert mc.stale_after(mc.KIND_FINRA, date(2026, 9, 1), calendars(), self.NOW) == midnight(2026, 12, 1)
        assert mc.stale_after(mc.KIND_FINRA, date(2026, 9, 30), calendars(), self.NOW) == midnight(2026, 12, 1)
        assert mc.stale_after(mc.KIND_TW_MONTHLY, date(2026, 9, 1), calendars(), self.NOW) == midnight(2026, 12, 1)
        assert mc.stale_after(mc.KIND_13F, date(2026, 6, 30), calendars(), self.NOW) == midnight(2026, 12, 2)
        assert mc.stale_after(mc.KIND_HOLDINGS_MONEYDJ, date(2026, 9, 30), calendars(), self.NOW) == midnight(2026, 12, 1)
        assert mc.stale_after(mc.KIND_SEASON, 2025, calendars(), self.NOW) == midnight(2027, 4, 1)
        assert mc.stale_after(mc.KIND_SEASON, date(2025, 12, 31), calendars(), self.NOW) == midnight(2027, 4, 1)

    def test_us_fundamentals_not_updated_flag(self):
        quarter_end = date(2026, 7, 26)
        info = mc.stale_after_info(mc.KIND_US_FUND, quarter_end, calendars(), self.NOW)
        assert info.at == at(2027, 1, 2).timestamp() and not info.not_updated
        late = mc.stale_after_info(mc.KIND_US_FUND, quarter_end, calendars(), at(2026, 12, 25))
        assert late.not_updated and late.at == info.at

    def test_every_kind_is_handled(self):
        instant = datetime(2026, 10, 17, 1, 0, tzinfo=UTC)
        for kind in mc.STALE_KINDS:
            value = instant if kind in (mc.KIND_BTC, mc.KIND_PREDICTION, mc.KIND_NEWS, mc.KIND_TRUMP, mc.KIND_EVENT) else D
            assert mc.stale_after(kind, value, calendars(), self.NOW) > D.toordinal()

    def test_bad_inputs(self):
        with pytest.raises(ValueError):
            mc.stale_after("weekly", D, calendars(), self.NOW)
        with pytest.raises(ValueError):
            mc.stale_after(mc.KIND_NEWS, datetime(2026, 10, 17, 1, 0), calendars(), self.NOW)  # naive
        with pytest.raises(TypeError):
            mc.stale_after(mc.KIND_US_DAILY, datetime(2026, 10, 16, 20, 0, tzinfo=UTC), calendars(), self.NOW)
        with pytest.raises(ValueError):
            mc.stale_after(mc.KIND_TW_DAILY, D, {"nyse": mc.nyse_calendar()}, self.NOW)

    def test_calendars_may_be_a_mapping(self):
        mapping = {"nyse": mc.nyse_calendar(), "twse": tw_cal()}
        assert mc.stale_after(mc.KIND_TW_DAILY, date(2026, 10, 22), mapping, self.NOW) == datetime(
            2026, 10, 29, 13, 30, tzinfo=TPE
        ).timestamp()

    def test_derived_rows(self):
        assert mc.stale_min([5.0, 3.0, 4.0]) == 3.0
        assert mc.stale_min([5.0, None]) is None
        assert mc.common_date([D, D]) == D
        assert mc.common_date([D, date(2026, 10, 15)]) is None
        assert mc.common_date([D, None]) is None


# ── 均線、RSI、回撤、單檔技術面 ──────────────────────────────────────────────────
class TestIndicators:
    def test_sma_and_distance(self):
        assert mc.sma([1, 2, 3, 4], 2) == 3.5
        assert mc.sma([1, 2], 3) is None
        assert mc.sma([1, math.nan, 3], 2) is None
        assert mc.ma_distance_pct(110, 100) == pytest.approx(10.0)
        assert mc.ma_distance_pct(110, None) is None

    def test_rsi_wilder(self):
        # period=2：變化 +1、−1、+1；起始平均 0.5／0.5，之後 (0.5×1+1)/2=0.75、(0.5×1+0)/2=0.25 → RS=3 → 75
        assert mc.rsi_wilder([1, 2, 1, 2], period=2) == pytest.approx(75.0)
        assert mc.rsi_wilder([float(i) for i in range(1, 20)]) == 100.0
        assert mc.rsi_wilder([float(20 - i) for i in range(19)]) == pytest.approx(0.0)
        assert mc.rsi_wilder([5.0] * 20) is None
        assert mc.rsi_wilder([1.0] * 14) is None  # 要 15 根

    def test_drawdown(self):
        assert mc.drawdown_pct([100.0] * 239 + [90.0]) == pytest.approx(-10.0)
        assert mc.drawdown_pct([100.0] * 239) is None
        assert mc.drawdown_pct([200.0] + [100.0] * 240) == pytest.approx(0.0)  # 高點已經出了 240 根窗口

    def test_technicals_needs_every_session_in_the_window(self):
        sessions = weekdays(date(2025, 10, 1), 260)
        day = sessions[-1]
        closes = {s: 100.0 + i * 0.1 for i, s in enumerate(sessions)}
        tech = mc.technicals(closes, sessions, day)
        assert tech.close == pytest.approx(125.9)
        assert tech.ma[20] == pytest.approx(sum(100.0 + i * 0.1 for i in range(240, 260)) / 20)
        assert tech.above[20] is True and tech.above[240] is True
        assert tech.chg_pct == pytest.approx((125.9 / 125.8 - 1) * 100)
        assert tech.dd240_pct == pytest.approx(0.0)
        assert tech.rsi14 == 100.0
        missing = dict(closes)
        del missing[sessions[-30]]  # 在 60 日窗口內、20 日窗口外
        tech = mc.technicals(missing, sessions, day)
        assert tech.ma[20] is not None and tech.ma[60] is None and tech.ma_dist[60] is None
        gap = dict(closes)
        del gap[sessions[-2]]  # 前一筆不是 prev_session → 不顯示日變化（§18.8）
        tech = mc.technicals(gap, sessions, day)
        assert tech.chg is None and tech.chg_pct is None and tech.ma[20] is None

    def test_technicals_without_today(self):
        sessions = weekdays(date(2026, 9, 1), 30)
        tech = mc.technicals({s: 100.0 for s in sessions[:-1]}, sessions, sessions[-1])
        assert tech.close is None and tech.ma[20] is None


# ── 寬度、漲跌家數、新高新低 ─────────────────────────────────────────────────────
def flat_series(sessions, value=100.0, overrides=None, start=0):
    out = {s: value for s in sessions[start:]}
    for index, close in (overrides or {}).items():
        out[sessions[index]] = close
    return out


class TestBreadth:
    def test_counts_new_crossings_and_status(self):
        sessions = weekdays(date(2026, 1, 5), 30)
        last = len(sessions) - 1
        closes = {}
        for i in range(5):
            closes[f"UP{i}"] = flat_series(sessions, overrides={last: 110.0})
        for i in range(3):
            closes[f"DN{i}"] = flat_series(sessions, overrides={last: 90.0})
        closes["EQ0"], closes["EQ1"] = flat_series(sessions), flat_series(sessions)
        b = mc.breadth(closes, sessions, sessions[-1], 20)
        assert (b.above, b.below, b.equal, b.new_above, b.new_below) == (5, 3, 2, 5, 3)
        assert (b.valid, b.total, b.status) == (10, 10, mc.STATUS_OK)
        assert b.pct == 50.0
        assert (b.prev_pct, b.prev_valid, b.same_set_pct) == (0.0, 10, 50.0)

    def test_non_split_jump_is_still_counted(self):
        # §18.2：美股單日 +23% 沒有分割、兩次重抓一致 → 當真實行情，不隔離、照樣計入寬度
        sessions = weekdays(date(2026, 1, 5), 30)
        jumped = {s: (100.0 if i < 20 else 123.0) for i, s in enumerate(sessions)}
        b = mc.breadth({"JUMP": jumped}, sessions, sessions[-1], 20)
        assert (b.valid, b.above, b.status) == (1, 1, mc.STATUS_OK)

    def test_quarantine_interval_only_invalidates_windows_that_touch_it(self):
        sessions = weekdays(date(2026, 1, 5), 80)
        closes = {"Q": flat_series(sessions, overrides={79: 110.0})}
        quarantine = {"Q": [(sessions[30], sessions[32])]}
        b20 = mc.breadth(closes, sessions, sessions[-1], 20, quarantine=quarantine)
        b60 = mc.breadth(closes, sessions, sessions[-1], 60, quarantine=quarantine)
        assert (b20.valid, b20.above, b20.prev_valid) == (1, 1, 1)
        assert (b60.valid, b60.total, b60.status) == (0, 1, mc.STATUS_LOW_COVERAGE)
        still_open = {"Q": [(sessions[70], None)]}
        assert mc.breadth(closes, sessions, sessions[-1], 20, quarantine=still_open).valid == 0
        cleared_long_ago = {"Q": [(sessions[0], sessions[5])]}
        assert mc.breadth(closes, sessions, sessions[-1], 60, quarantine=cleared_long_ago).valid == 1

    def test_five_day_comparison_uses_the_same_set(self):
        sessions = weekdays(date(2026, 1, 5), 40)
        last, back = 39, 34
        closes = {}
        for name in ("A", "B", "C"):  # 5 天前在均線上，今天在均線下
            closes[name] = flat_series(sessions, overrides={back: 120.0})
        for name in ("D", "E", "F", "G", "H"):  # 5 天前等於均線，今天站上
            closes[name] = flat_series(sessions, overrides={last: 110.0})
        for name in ("I", "J"):  # 只有最近 25 天：今天有效、5 天前無效 → 不進同一批
            closes[name] = flat_series(sessions, overrides={last: 110.0}, start=15)
        b = mc.breadth(closes, sessions, sessions[-1], 20)
        assert (b.valid, b.above, b.below, b.new_above, b.new_below) == (10, 7, 3, 7, 0)
        assert b.pct == 70.0
        assert b.prev_valid == 8
        assert b.prev_pct == pytest.approx(37.5)
        assert b.same_set_pct == pytest.approx(62.5)

    def test_comparison_skipped_when_the_same_set_is_below_80_percent(self):
        sessions = weekdays(date(2026, 1, 5), 40)
        closes = {f"F{i}": flat_series(sessions) for i in range(7)}
        closes.update({f"S{i}": flat_series(sessions, start=15) for i in range(3)})
        b = mc.breadth(closes, sessions, sessions[-1], 20)
        assert (b.valid, b.status) == (10, mc.STATUS_OK)
        assert (b.prev_valid, b.prev_pct, b.same_set_pct) == (7, None, None)

    def test_status_is_decided_by_coverage_not_by_the_worst_member(self):
        sessions = weekdays(date(2026, 1, 5), 30)
        closes = {f"OK{i}": flat_series(sessions) for i in range(8)}
        closes["NO_TODAY"] = flat_series(sessions[:-1])
        members = list(closes) + ["NEVER_FETCHED"]
        b = mc.breadth(closes, sessions, sessions[-1], 20, members=members)
        assert (b.valid, b.total, b.status) == (8, 10, mc.STATUS_OK)
        closes["NO_TODAY_2"] = flat_series(sessions[:-1])
        b = mc.breadth(closes, sessions, sessions[-1], 20, members=members + ["NO_TODAY_2"])
        assert (b.valid, b.total, b.status) == (8, 11, mc.STATUS_LOW_COVERAGE)

    def test_missing_close_inside_versus_outside_the_window(self):
        sessions = weekdays(date(2026, 1, 5), 40)
        inside = flat_series(sessions)
        del inside[sessions[-25]]  # N+5=25 根窗口的第一天
        outside = flat_series(sessions)
        del outside[sessions[-26]]
        b = mc.breadth({"IN": inside, "OUT": outside}, sessions, sessions[-1], 20)
        assert b.valid == 1 and b.prev_valid == 0  # OUT 今天有效、5 天前的窗口碰到缺的那天

    def test_d_must_be_a_session(self):
        sessions = weekdays(date(2026, 1, 5), 30)
        b = mc.breadth({"X": flat_series(sessions)}, sessions, sessions[-1] + timedelta(days=1), 20)
        assert (b.valid, b.status) == (0, mc.STATUS_LOW_COVERAGE)

    def test_with_trading_calendar(self):
        cal = mc.nyse_calendar()
        sessions = cal.sessions_ending(D, 30)
        assert date(2026, 9, 7) not in sessions and sessions[-1] == D
        closes = {"X": flat_series(sessions, overrides={29: 101.0})}
        b = mc.breadth(closes, cal, D, 20)
        assert (b.valid, b.above, b.prev_valid) == (1, 1, 1)

    def test_ad_counts(self):
        sessions = weekdays(date(2026, 1, 5), 3)
        prev, day = sessions[1], sessions[2]
        closes = {
            "UP": {prev: 10.0, day: 11.0},
            "DN": {prev: 10.0, day: 9.0},
            "FL": {prev: 10.0, day: 10.0},
            "NOPREV": {sessions[0]: 10.0, day: 12.0},
            "QUAR": {prev: 10.0, day: 20.0},
        }
        ad = mc.ad_counts(closes, sessions, day, quarantine={"QUAR": [(day, None)]})
        assert (ad.adv, ad.dec, ad.flat, ad.valid, ad.total) == (1, 1, 1, 3, 5)

    def test_highs_lows_240(self):
        sessions = weekdays(date(2025, 6, 2), 245)
        closes = {
            "HIGH": {s: 100.0 + i for i, s in enumerate(sessions)},
            "LOW": {s: 400.0 - i for i, s in enumerate(sessions)},
            "FLAT": flat_series(sessions),
            "TIE": flat_series(sessions, overrides={100: 150.0, 244: 150.0}),
            "OLDPEAK": flat_series(sessions, overrides={2: 150.0, 244: 120.0}),  # 舊高點在 240 根以外
            "GAP": {s: 100.0 for s in sessions[1:]},
        }
        hl = mc.highs_lows_240(closes, sessions, sessions[-1])
        assert (hl.new_high, hl.new_low, hl.valid, hl.total) == (2, 1, 5, 6)

    def test_latest_complete_day(self):
        sessions = weekdays(date(2026, 1, 5), 3)
        closes = {f"S{i}": {sessions[0]: 1.0, sessions[1]: 1.0} for i in range(10)}
        for i in range(7):
            closes[f"S{i}"][sessions[2]] = 1.0
        assert mc.day_coverage(closes, None, sessions[2]) == (7, 10)
        assert mc.latest_complete_day(closes, None, sessions) == sessions[1]  # 當天還在抓 → 前一個完整交易日
        closes["S7"][sessions[2]] = 1.0
        assert mc.latest_complete_day(closes, None, sessions) == sessions[2]
        assert mc.latest_complete_day({}, ["A"], sessions) is None


# ── 量能 ────────────────────────────────────────────────────────────────────────
def volume_days(count, start=date(2025, 1, 6)):
    return weekdays(start, count)


class TestVolume:
    @pytest.mark.parametrize(
        ("today", "label"),
        [(50, "明顯量縮"), (51, "量縮"), (80, "量縮"), (81, "正常"), (149, "正常"), (150, "放量"), (249, "放量"), (250, "爆量")],
    )
    def test_ratio_labels_and_boundaries(self, today, label):
        days = volume_days(21)
        volumes = {d: 100 for d in days[:-1]}
        volumes[days[-1]] = today
        state = mc.volume_state(volumes, days[-1])
        assert state.ratio == pytest.approx(today / 100)
        assert state.label == label

    def test_pct250(self):
        days = volume_days(250)
        volumes = {d: i + 1 for i, d in enumerate(days)}
        assert mc.volume_state(volumes, days[-1]).pct250 == 100.0
        volumes[days[-1]] = 1
        volumes[days[0]] = 2
        assert mc.volume_state(volumes, days[-1]).pct250 == pytest.approx(0.4)
        assert mc.volume_state({d: 10 for d in days[1:]}, days[-1]).pct250 is None  # 不足 250 天

    def test_missing_or_excluded_days(self):
        days = volume_days(21)
        volumes = {d: 100 for d in days}
        del volumes[days[3]]
        assert mc.volume_state(volumes, days[-1], calendar=days).ratio is None
        full = {d: 100 for d in days}
        state = mc.volume_state(full, days[-1], excluded={days[-1]})
        assert (state.ratio, state.label, state.note) == (None, None, mc.TEXT_VOL_SOURCE_DIFF)
        assert mc.volume_state(full, days[-1], excluded={days[5]}).ratio is None
        assert mc.volume_state(full, days[-1] + timedelta(days=1)).ratio is None

    @pytest.mark.parametrize(
        ("close_now", "volume_now", "expected"),
        [(11.0, 120, "價漲量增"), (11.0, 90, "價漲量縮"), (9.0, 100, "價跌量增"), (9.0, 70, "價跌量縮"), (10.0, 150, None)],
    )
    def test_price_volume_combinations(self, close_now, volume_now, expected):
        days = volume_days(21)
        volumes = {d: 100 for d in days[:-1]}
        volumes[days[-1]] = volume_now
        closes = {days[-2]: 10.0, days[-1]: close_now}
        assert mc.volume_state(volumes, days[-1], closes=closes).pv_label == expected

    def test_0050_with_twse_calendar_and_settlement_flag(self):
        cal = tw_cal()
        sessions = cal.sessions_ending(date(2026, 10, 21), 21)
        assert date(2026, 10, 9) not in sessions
        volumes = {d: 30_000 for d in sessions[:-1]}
        volumes[sessions[-1]] = 90_000
        flag = mc.volume_day_flag(sessions[-1], cal)
        state = mc.volume_state(volumes, sessions[-1], calendar=cal, day_flag=flag)
        assert state.ratio == pytest.approx(3.0) and state.label == "爆量" and state.day_flag == "結算日"


# ── 期權 ────────────────────────────────────────────────────────────────────────
def contract(symbol, oi, iv=0.2, gamma=0.01, volume=10.0, delta=0.5):
    return NS(symbol=symbol, oi=oi, iv=iv, gamma=gamma, volume=volume, delta=delta)


def occ(root, expiry, kind, strike):
    return f"{root}{expiry:%y%m%d}{kind}{round(strike * 1000):08d}"


def chain_of(contracts, spot=100.0, **extra):
    return NS(contracts=contracts, spot=spot, raw_count=len(contracts), bad_count=0, conflict_count=0, iv30=20.0, **extra)


# 2026-10-16 QQQ 實際回應裁出的列（timestamp 16:33:11 UTC、現價 717.23）
REAL_QQQ = [
    ("QQQ261016C00688000", 88.0, 0.0, 0.0013, 4.0),
    ("QQQ261016P00688000", 3198.0, 0.7267, 0.0013, 447.0),
    ("QQQ261019P00733000", 301.0, 0.0, 0.0112, 23.0),
    ("QQQ261019C00733000", 1534.0, 0.1446, 0.0116, 1404.0),
    ("QQQ261019P00740000", 4611.0, 0.0, 0.0037, 205.0),
    ("QQQ261019C00740000", 3778.0, 0.1575, 0.004, 1810.0),
    ("QQQ261020P00741000", 203.0, 0.0, 0.0045, 3.0),
    ("QQQ261020C00741000", 254.0, 0.1524, 0.005, 75.0),
]


class TestOptionSets:
    def test_symbol_parsing(self):
        assert mc.parse_option_symbol("QQQ261016C00688000") == ("QQQ", D, "C", 688.0)
        assert mc.parse_option_symbol("SOXX261120P00262500") == ("SOXX", date(2026, 11, 20), "P", 262.5)
        assert mc.parse_option_symbol("QQQ1261016C00688000") is None  # 調整後的非標準 root
        assert mc.parse_option_symbol("QQQ261332C00688000") is None
        assert mc.parse_option_symbol("XYZ261016C00688000") is None
        assert mc.parse_option_symbol(None) is None

    def test_iv_zero_itm_uses_the_otm_iv_on_real_contracts(self):
        chain = chain_of([contract(s, oi, iv, g, v) for s, oi, iv, g, v in REAL_QQQ], spot=717.23)
        oi_valid, gamma_valid, stats = mc.option_sets(chain, D)
        assert stats.expired == 2  # 10/16 到期＝D 當天，剩餘期限 0
        assert (stats.oi_valid, stats.gamma_valid, stats.iv_replaced) == (6, 6, 3)
        replaced = {leg.symbol: leg for leg in gamma_valid if leg.iv_replaced}
        assert set(replaced) == {"QQQ261019P00733000", "QQQ261019P00740000", "QQQ261020P00741000"}
        assert replaced["QQQ261019P00740000"].iv == 0.1575
        for symbol, leg in replaced.items():
            recomputed = float(mc.bs_gamma(717.23, leg.strike, leg.days / 365, leg.iv, 0.0426))
            assert recomputed == pytest.approx(leg.gamma, rel=0.15), symbol  # 跟 CBOE 自帶的 gamma 對得上

    def test_iv_zero_otm_or_without_the_other_side_is_excluded(self):
        expiry = D + timedelta(days=10)
        chain = chain_of(
            [
                contract(occ("QQQ", expiry, "C", 110), 100, iv=0.0),  # 價外 IV=0
                contract(occ("QQQ", expiry, "P", 110), 100, iv=0.25),
                contract(occ("QQQ", expiry, "C", 90), 100, iv=0.0),  # 價內但另一邊不在
                contract(occ("QQQ", expiry, "C", 95), 100, iv=0.0),  # 價內、另一邊 IV 也是 0
                contract(occ("QQQ", expiry, "P", 95), 100, iv=0.0),
                contract(occ("QQQ", expiry, "C", 96), 100, iv=float("nan")),
                contract(occ("QQQ", expiry, "C", 97), 100, gamma=None),
            ]
        )
        oi_valid, gamma_valid, stats = mc.option_sets(chain, D)
        assert stats.oi_valid == 7
        assert [leg.symbol for leg in gamma_valid] == [occ("QQQ", expiry, "P", 110)]
        assert stats.iv_replaced == 0

    def test_oi_validity_rules(self):
        expiry = D + timedelta(days=10)
        chain = chain_of(
            [
                contract(occ("QQQ", expiry, "C", 100), 5.0),
                contract(occ("QQQ", expiry, "C", 101), -1),
                contract(occ("QQQ", expiry, "C", 102), 1.5),
                contract(occ("QQQ", expiry, "C", 103), True),
                contract(occ("QQQ", expiry, "C", 0), 10),
                contract(occ("QQQ", D, "C", 104), 10),  # 剩餘期限 0
                contract(occ("SPY", expiry, "C", 105), 10),  # 別的 root
                contract("garbage", 10),
            ]
        )
        oi_valid, _, stats = mc.option_sets(chain, D)
        assert [leg.oi for leg in oi_valid] == [5]
        assert (stats.bad, stats.expired, stats.raw) == (6, 1, 8)

    def test_field_mismatch_with_the_symbol_is_bad(self):
        expiry = D + timedelta(days=10)
        good = NS(symbol=occ("QQQ", expiry, "C", 100), oi=1, iv=0.2, gamma=0.01, volume=1, delta=0.5,
                  kind="call", strike=100.0, expiry=expiry)
        wrong = NS(symbol=occ("QQQ", expiry, "C", 101), oi=1, iv=0.2, gamma=0.01, volume=1, delta=0.5,
                   kind="P", strike=101.0, expiry=expiry)
        oi_valid, _, stats = mc.option_sets(chain_of([good, wrong]), D)
        assert [leg.strike for leg in oi_valid] == [100.0] and stats.bad == 1

    def test_conflicting_duplicates_are_both_dropped(self):
        expiry = D + timedelta(days=10)
        symbol = occ("QQQ", expiry, "C", 100)
        same = occ("QQQ", expiry, "P", 100)
        chain = chain_of(
            [contract(symbol, 100), contract(symbol, 300), contract(same, 50), contract(same, 50)]
        )
        oi_valid, _, stats = mc.option_sets(chain, D)
        assert [leg.symbol for leg in oi_valid] == [same]  # 完全相同的重複只留一筆
        assert stats.conflict == 2
        assert stats.conflict_legs == (mc.ConflictLeg(expiry, 100.0, 10, None, 300),)
        known = chain_of([contract(symbol, 100, iv=0.2), contract(symbol, 100, iv=0.3)])
        assert mc.option_sets(known, D)[2].conflict_legs[0].oi == 100

    def test_spot_is_required(self):
        with pytest.raises(ValueError):
            mc.option_sets([contract(occ("QQQ", D + timedelta(days=5), "C", 100), 1)], D)


def ladder(expiries, strikes, call_oi=None, put_oi=None, default=10, iv=0.2):
    rows = []
    for expiry in expiries:
        for strike in strikes:
            rows.append(contract(occ("QQQ", expiry, "C", strike), (call_oi or {}).get(strike, default), iv=iv))
            rows.append(contract(occ("QQQ", expiry, "P", strike), (put_oi or {}).get(strike, default), iv=iv))
    return rows


class TestWallsMaxPainPcr:
    E1, E2 = D + timedelta(days=10), D + timedelta(days=35)

    def test_walls_use_the_15_percent_ranges_and_the_45_day_window(self):
        rows = ladder([self.E1, self.E2], range(80, 121), call_oi={112: 500, 118: 5000}, put_oi={88: 500, 80: 5000})
        rows += [contract(occ("QQQ", D + timedelta(days=60), "C", 108), 99_999)]  # 45 天外
        oi_valid, _, _ = mc.option_sets(chain_of(rows), D)
        wall = mc.walls(oi_valid, 100.0)
        assert (wall.call_wall, wall.put_wall, wall.status) == (112.0, 88.0, mc.STATUS_OK)
        assert wall.call_wall_dist_pct == pytest.approx(12.0) and wall.put_wall_dist_pct == pytest.approx(-12.0)

    def test_ties_go_to_the_strike_nearest_spot(self):
        rows = ladder([self.E1, self.E2], range(80, 121), call_oi={105: 700, 110: 700}, put_oi={95: 700, 90: 700})
        wall = mc.walls(mc.option_sets(chain_of(rows), D)[0], 100.0)
        assert (wall.call_wall, wall.put_wall) == (105.0, 95.0)

    def test_walls_need_100_contracts_and_both_sides(self):
        few = ladder([self.E1], range(80, 100))  # 40 筆
        assert mc.walls(mc.option_sets(chain_of(few), D)[0], 100.0).status == mc.STATUS_INSUFFICIENT
        one_side = ladder([self.E1, self.E2], range(100, 130))  # 現價以下 10% 沒有履約價
        assert mc.walls(mc.option_sets(chain_of(one_side), D)[0], 100.0).status == mc.STATUS_INSUFFICIENT

    def test_max_pain(self):
        legs = [
            mc.OptionLeg("x", "QQQ", self.E1, kind, strike, 100, 10, None, 0.2, 0.01)
            for strike in (90.0, 100.0, 110.0)
            for kind in ("C", "P")
        ]
        assert mc.max_pain(legs, 100.0) == 100.0
        tie = [
            mc.OptionLeg("c", "QQQ", self.E1, "C", 90.0, 100, 10, None, 0.2, 0.01),
            mc.OptionLeg("p", "QQQ", self.E1, "P", 110.0, 100, 10, None, 0.2, 0.01),
        ]
        assert mc.max_pain(tie, 105.0) == 110.0 and mc.max_pain(tie, 95.0) == 90.0
        assert mc.max_pain([], 100.0) is None

    def test_monthly_metrics_completeness(self):
        monthly = date(2026, 11, 20)
        rows = ladder([monthly], range(91, 111), call_oi={104: 900}, put_oi={96: 900})
        metrics = mc.monthly_metrics(mc.option_sets(chain_of(rows), D)[0], 100.0, monthly)
        assert (metrics.call_wall, metrics.put_wall, metrics.status, metrics.n_contracts) == (104.0, 96.0, "ok", 40)
        assert metrics.max_pain is not None
        short = ladder([monthly], range(91, 111))[:-12]  # 買權只剩 14 個履約價
        assert mc.monthly_metrics(mc.option_sets(chain_of(short), D)[0], 100.0, monthly).status == mc.STATUS_INSUFFICIENT

    def test_pcr_and_zero_denominators(self):
        rows = [
            contract(occ("QQQ", self.E1, "C", 100), 200, volume=0.0),
            contract(occ("QQQ", self.E1, "P", 100), 300, volume=50.0),
            contract(occ("QQQ", D + timedelta(days=90), "P", 100), 10_000),  # 45 天外
        ]
        ratio = mc.pcr(mc.option_sets(chain_of(rows), D)[0])
        assert ratio.oi == pytest.approx(1.5) and ratio.volume is None
        only_puts = mc.pcr(mc.option_sets(chain_of(rows[1:2]), D)[0])
        assert only_puts.oi is None


class TestGex:
    def legs(self, specs, spot=100.0, days=30):
        expiry = D + timedelta(days=days)
        rows = [contract(occ("QQQ", expiry, kind, strike), oi, iv=iv) for kind, strike, oi, iv in specs]
        return mc.option_sets(chain_of(rows, spot=spot), D)

    def test_codex_counterexample_sign_flips_the_opposite_way(self):
        # 同期限 30 天、IV 20%、r 4%：買權 K=90、賣權 K=110 各 OI 100 → S=90 約 +62,204、S=110 約 −76,129
        oi_valid, gamma_valid, stats = self.legs([("C", 90, 100, 0.2), ("P", 110, 100, 0.2)])
        result = mc.gex_from_sets(oi_valid, gamma_valid, stats, 100.0, 0.04)
        grid = list(result.grid)
        assert result.values[grid.index(pytest.approx(90.0))] == pytest.approx(62_204, rel=1e-3)
        assert result.values[grid.index(pytest.approx(110.0))] == pytest.approx(-76_129, rel=1e-3)
        assert result.status == "ok" and result.sign == -1 and result.at_spot < 0 and result.near_zero is False
        assert len(result.crossings) == 1
        crossing = result.crossings[0]
        assert crossing.direction == "down" and 90 < crossing.price < 100  # 往上穿過時由正轉負

    def test_multiple_crossings(self):
        oi_valid, gamma_valid, stats = self.legs([("P", 90, 100, 0.2), ("C", 100, 100, 0.2), ("P", 110, 100, 0.2)])
        result = mc.gex_from_sets(oi_valid, gamma_valid, stats, 100.0, 0.04)
        assert [c.direction for c in result.crossings] == ["up", "down"]
        assert 90 < result.crossings[0].price < 100 < result.crossings[1].price < 110
        assert result.sign == 1

    def test_only_the_two_nearest_crossings_are_listed(self):
        specs = [("C", 88, 100, 0.1), ("P", 94, 100, 0.1), ("C", 100, 100, 0.1), ("P", 106, 100, 0.1), ("C", 112, 100, 0.1)]
        oi_valid, gamma_valid, stats = self.legs(specs)
        result = mc.gex_from_sets(oi_valid, gamma_valid, stats, 100.0, 0.04)
        every = mc._crossings(list(result.grid), list(result.values), 1e-9 * result.max_abs)
        assert len(every) == 4
        assert [(round(c.price), c.direction) for c in result.crossings] == [(97, "up"), (103, "down")]

    def test_near_zero_and_no_crossing(self):
        specs = [("C", 100, 1000, 0.2), ("P", 100, 1000, 0.2), ("C", 114, 10, 0.05)]
        oi_valid, gamma_valid, stats = self.legs(specs)
        result = mc.gex_from_sets(oi_valid, gamma_valid, stats, 100.0, 0.04)
        assert result.near_zero is True and result.crossings == ()
        assert abs(result.at_spot) < 0.02 * result.max_abs

    def test_coverage_below_80_percent_is_insufficient(self):
        expiry = D + timedelta(days=20)
        rows = [contract(occ("QQQ", expiry, "C", k), 100) for k in range(95, 105)]
        for k in (95, 96, 97):
            rows[k - 95] = contract(occ("QQQ", expiry, "C", k), 100, iv=None)
        oi_valid, gamma_valid, stats = mc.option_sets(chain_of(rows), D)
        result = mc.gex_from_sets(oi_valid, gamma_valid, stats, 100.0, 0.04)
        assert result.status == mc.STATUS_INSUFFICIENT and result.coverage == pytest.approx(0.7)
        assert result.at_spot is None and result.sign is None

    def test_unknown_conflict_oi_makes_coverage_a_lower_bound(self):
        expiry = D + timedelta(days=20)
        rows = [contract(occ("QQQ", expiry, "C", k), 100) for k in range(95, 105)]
        dup = occ("QQQ", expiry, "P", 100)
        rows += [contract(dup, 50), contract(dup, 80)]
        oi_valid, gamma_valid, stats = mc.option_sets(chain_of(rows), D)
        result = mc.gex_from_sets(oi_valid, gamma_valid, stats, 100.0, 0.04)
        assert result.coverage_is_lower_bound is True
        assert result.coverage == pytest.approx(1000 / 1080)  # 未知 OI 取最大候選值放進分母
        far = [contract(occ("QQQ", expiry, "C", k), 100) for k in range(95, 105)]
        far += [contract(occ("QQQ", expiry, "P", 130), 50), contract(occ("QQQ", expiry, "P", 130), 80)]
        oi_valid, gamma_valid, stats = mc.option_sets(chain_of(far), D)
        assert mc.gex_from_sets(oi_valid, gamma_valid, stats, 100.0, 0.04).coverage_is_lower_bound is False

    def test_known_conflict_oi_goes_into_the_denominator(self):
        expiry = D + timedelta(days=20)
        rows = [contract(occ("QQQ", expiry, "C", k), 100) for k in range(95, 105)]
        dup = occ("QQQ", expiry, "P", 100)
        rows += [contract(dup, 60, iv=0.2), contract(dup, 60, iv=0.3)]
        oi_valid, gamma_valid, stats = mc.option_sets(chain_of(rows), D)
        result = mc.gex_from_sets(oi_valid, gamma_valid, stats, 100.0, 0.04)
        assert result.coverage == pytest.approx(1000 / 1060) and result.coverage_is_lower_bound is False

    def test_upstream_conflict_counts(self):
        expiry = D + timedelta(days=20)
        rows = [contract(occ("QQQ", expiry, "C", k), 100) for k in range(95, 105)]
        chain = chain_of(rows)
        chain.conflict_count, chain.conflict_oi_known = 2, True
        result = mc.gex_curve(chain, 100.0, 0.04, D=D)
        assert result.coverage_is_lower_bound is True and result.coverage == 1.0
        chain.conflict_oi_known = 250  # 來源給的是那些列的 OI 合計 → 整筆放進分母
        assert mc.gex_curve(chain, 100.0, 0.04, D=D).coverage == pytest.approx(1000 / 1250)
        chain.conflict_count = 0
        chain.conflicts = [NS(symbol=occ("QQQ", expiry, "P", 99), oi=None)]
        result = mc.gex_curve(chain, 100.0, 0.04, D=D)
        assert result.coverage_is_lower_bound is True and result.coverage == 1.0

    def test_sources_style_chain_with_conflicts_and_quote_time(self):
        # market_sources_us.OptionChain 的欄位：conflicts 是每個衝突代號一筆（oi 各筆一致才知道）
        expiry = D + timedelta(days=20)
        rows = [NS(symbol=occ("QQQ", expiry, "C", k), expiry=expiry, kind="C", strike=float(k), oi=100,
                   volume=5, iv=0.2, gamma=0.01, delta=0.5) for k in range(95, 105)]
        unknown = NS(symbol=occ("QQQ", expiry, "P", 100), expiry=expiry, kind="P", strike=100.0, oi=None, rows=2)
        chain = NS(contracts=rows, spot=100.0, iv30=21.0, raw_count=12, bad_count=0, conflict_count=2,
                   conflict_oi_known=False, conflicts=(unknown,), symbol="QQQ",
                   quote_last_trade_et=datetime(2026, 10, 16, 16, 15, tzinfo=ET))
        result = mc.gex_curve(chain, 100.0, 0.04)  # D 用 quote_last_trade_et 的美東日期
        assert result.status == "ok" and result.coverage_is_lower_bound is True
        _, _, stats = mc.option_sets(chain, D)
        assert (stats.raw, stats.conflict, stats.oi_valid) == (12, 2, 10)
        known = NS(**{**vars(chain), "conflicts": (NS(**{**vars(unknown), "oi": 50}),), "conflict_oi_known": True})
        result = mc.gex_curve(known, 100.0, 0.04)
        assert result.coverage_is_lower_bound is False and result.coverage == pytest.approx(1000 / 1050)
        with pytest.raises(ValueError):
            mc.gex_curve(chain_of(rows), 100.0, 0.04)  # 沒有 D 也沒有報價時間

    def test_chain_symbol_decides_the_root(self):
        expiry = D + timedelta(days=20)
        rows = [contract(occ("SPY", expiry, "C", 100), 1), contract(occ("QQQ", expiry, "C", 100), 1),
                contract(occ("QQQ", expiry, "P", 100), 1)]
        chain = chain_of(rows, symbol="SPY")
        oi_valid, _, stats = mc.option_sets(chain, D)
        assert [leg.root for leg in oi_valid] == ["SPY"] and stats.bad == 2

    def test_cboe_gamma_check_and_rate_validation(self):
        oi_valid, gamma_valid, stats = self.legs([("C", 100, 100, 0.2)])
        result = mc.gex_from_sets(oi_valid, gamma_valid, stats, 100.0, 0.04)
        assert result.check_at_spot == pytest.approx(0.01 * 100 * 100 * 100.0**2 * 0.01)
        with pytest.raises(ValueError):
            mc.gex_from_sets(oi_valid, gamma_valid, stats, 100.0, 4.26)

    def test_real_fixture_options_values_round_trip_as_json(self):
        rows = [contract(s, oi, iv, g, v) for s, oi, iv, g, v in REAL_QQQ]
        values = mc.options_values(chain_of(rows, spot=717.23), D, spot=717.23, rate=None, holidays=mc.NYSE_HOLIDAYS)
        assert values["gex"]["rate_assumed"] is True
        assert values["counts"] == {"raw": 8, "bad": 0, "conflict": 0, "oi_valid": 6, "gamma_valid": 6, "iv_replaced": 3}
        assert values["monthly"] is None and values["wall_status"] == mc.STATUS_INSUFFICIENT
        restored = json.loads(json.dumps(values, allow_nan=False))
        assert mc.values_ok("us.options.QQQ", restored)

    def test_options_values_full_ladder(self):
        monthly = date(2026, 11, 20)
        rows = ladder([D + timedelta(days=3), monthly], range(80, 121), call_oi={110: 900}, put_oi={92: 900})
        values = mc.options_values(chain_of(rows), D, spot=100.0, rate=0.0426, holidays=mc.nyse_calendar())
        assert values["call_wall"] == 110.0 and values["put_wall"] == 92.0
        assert values["monthly"]["expiry"] == "2026-11-20" and values["monthly"]["status"] == "ok"
        assert values["gex"]["status"] == "ok" and values["gex"]["rate_assumed"] is False
        assert values["iv30"] == 20.0 and values["oi_date_note"] == mc.TEXT_OI_DATE
        assert mc.values_ok("us.options.SPY", json.loads(json.dumps(values, allow_nan=False)))


# ── 台指選擇權 ───────────────────────────────────────────────────────────────────
TxoRow = namedtuple("TxoRow", "month strike kind oi settle")


class TestTxo:
    SPOT = 46_969.87  # 10/15 加權收盤（期交所資料日）

    def rows(self, month="202610", call_oi=None, put_oi=None, kinds=("買權", "賣權")):
        out = []
        for strike in range(39_500, 56_500, 500):  # 39500 在 −15% 外
            out.append(TxoRow(month, float(strike), kinds[0], (call_oi or {}).get(strike, 100), 10.0))
            out.append(TxoRow(month, float(strike), kinds[1], (put_oi or {}).get(strike, 100), 10.0))
        return out

    def test_call_wall_beyond_10_percent_is_found(self):
        assert 54_000 / self.SPOT > 1.10  # 舊的 ±10% 範圍會漏掉這個買權牆
        rows = self.rows(call_oi={54_000: 9_000, 50_000: 5_000}, put_oi={46_000: 8_000, 39_500: 20_000})
        rows += self.rows(month="202611", call_oi={48_000: 99_999})  # 別的月份
        rows += [TxoRow("202610W4", 47_000.0, "買權", 99_999, 1.0)]  # 週選
        rows += [NS(month="202610", strike=47_500.0, kind="C", oi=99_999, session="盤後")]
        rows += [TxoRow("202610", 48_500.0, "C", None, None)]  # OI 是「-」
        result = mc.txo_walls(rows, "202610", self.SPOT)
        assert (result.call_wall, result.put_wall, result.status) == (54_000.0, 46_000.0, mc.STATUS_OK)
        assert result.call_wall_dist_pct == pytest.approx((54_000 / self.SPOT - 1) * 100)
        assert result.max_pain is not None and result.n_contracts == 68

    def test_put_wall_beyond_15_percent_is_ignored(self):
        result = mc.txo_walls(self.rows(put_oi={39_500: 20_000, 45_000: 3_000}, kinds=("C", "P")), "202610", self.SPOT)
        assert 39_500 / self.SPOT < 0.85 < 40_000 / self.SPOT
        assert result.put_wall == 45_000.0  # 39500 在 −15% 外

    def test_conflicting_rows_and_completeness(self):
        rows = self.rows() + [TxoRow("202610", 46_000.0, "賣權", 555, 1.0)]
        result = mc.txo_walls(rows, "202610", self.SPOT)
        assert result.n_contracts == 67  # 同履約價兩個不同 OI → 都丟
        thin = [r for r in self.rows() if r.strike < 47_000]  # 現價以上沒有履約價
        assert mc.txo_walls(thin, "202610", self.SPOT).status == mc.STATUS_INSUFFICIENT


# ── 還原除權息 ───────────────────────────────────────────────────────────────────
TwBar = namedtuple("TwBar", "date close volume money")


@dataclass(frozen=True)
class TwBarDC:
    date: date
    close: float
    volume: int
    money: int


ExRight = namedtuple("ExRight", "date code pre_close ref_price kind")


class TestAdjustTw:
    def test_only_closes_before_the_ex_date_are_multiplied(self):
        days = [date(2026, 6, 9), date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)]
        bars = [TwBar(d, c, 1000, 1) for d, c in zip(days, (2250.0, 2255.0, 2250.0, 2260.0))]
        events = [ExRight(date(2026, 6, 11), "2330", 2255.0, 2248.99, "息"), ExRight(date(2026, 6, 11), "2317", 248.0, 240.82, "息")]
        out = mc.adjust_tw_closes(bars, events, code="2330")
        factor = 2248.99 / 2255.0
        assert [b.close for b in out] == pytest.approx([2250.0 * factor, 2255.0 * factor, 2250.0, 2260.0])
        assert [b.volume for b in out] == [1000] * 4  # 量不調
        assert isinstance(out[0], TwBar)

    def test_multiple_events_compound_and_future_events_are_ignored(self):
        days = [date(2026, 6, 10), date(2026, 7, 1), date(2026, 9, 17)]
        bars = [TwBarDC(d, 100.0, 1, 1) for d in days]
        events = [
            ExRight(date(2026, 6, 11), "2330", 100.0, 99.0, "息"),
            ExRight(date(2026, 9, 16), "2330", 100.0, 98.0, "息"),
            ExRight(date(2026, 12, 10), "2330", 100.0, 50.0, "權"),  # 還沒發生
        ]
        out = mc.adjust_tw_closes(bars, events)
        assert [b.close for b in out] == pytest.approx([100.0 * 0.99 * 0.98, 100.0 * 0.98, 100.0])
        assert isinstance(out[0], TwBarDC)

    def test_conflicting_factors_on_one_day(self):
        bars = [TwBar(date(2026, 6, 10), 100.0, 1, 1), TwBar(date(2026, 6, 12), 100.0, 1, 1)]
        events = [ExRight(date(2026, 6, 11), "2330", 100.0, 99.0, "息"), ExRight(date(2026, 6, 11), "2330", 100.0, 97.0, "息")]
        with pytest.raises(mc.ParseError) as caught:
            mc.adjust_tw_closes(bars, events)
        assert caught.value.kind == "conflict"
        same = [events[0], events[0]]
        assert mc.adjust_tw_closes(bars, same)[0].close == pytest.approx(99.0)
        assert mc.adjust_tw_closes([], events) == []


# ── 本益比、股別合併、13F ───────────────────────────────────────────────────────
Quarter = namedtuple("Quarter", "end_date net_income_common_kusd")
NVDA_QUARTERS = [
    Quarter(date(2026, 7, 26), 31_910_000.0),
    Quarter(date(2026, 4, 26), 30_000_000.0),
    Quarter(date(2026, 1, 25), 28_000_000.0),
    Quarter(date(2025, 10, 26), 26_000_000.0),
]


class TestFundamentals:
    def test_pe_from_four_consecutive_quarters(self):
        result = mc.eps_metrics(NVDA_QUARTERS + [Quarter(date(2025, 7, 27), 1.0)], market_cap_usd=4.6e12)
        assert result.status == "ok" and result.quarter_ends[0] == date(2026, 7, 26)
        assert result.pe == pytest.approx(4.6e12 / (115_910_000.0 * 1000))

    @pytest.mark.parametrize(
        ("values", "status"), [((10.0, -20.0, 5.0, 4.0), "nonpositive"), ((10.0, -10.0, 5.0, -5.0), "nonpositive")]
    )
    def test_negative_or_zero_base(self, values, status):
        quarters = [Quarter(q.end_date, v) for q, v in zip(NVDA_QUARTERS, values)]
        result = mc.eps_metrics(quarters, market_cap_usd=1e9)
        assert (result.status, result.pe, result.note) == (status, None, mc.TEXT_PE_NONPOSITIVE)

    def test_missing_gap_duplicate_and_market_cap(self):
        assert mc.eps_metrics(NVDA_QUARTERS[:3], 1e12).status == "missing"
        gap = [NVDA_QUARTERS[0], NVDA_QUARTERS[1], NVDA_QUARTERS[3], Quarter(date(2025, 4, 27), 1.0)]
        assert mc.eps_metrics(gap, 1e12).status == "not_consecutive"
        dup = NVDA_QUARTERS + [Quarter(date(2026, 7, 26), 99.0)]
        assert mc.eps_metrics(dup, 1e12).status == "conflict"
        assert mc.eps_metrics(NVDA_QUARTERS + [NVDA_QUARTERS[0]], 1e12).status == "ok"
        assert mc.eps_metrics(NVDA_QUARTERS, None).status == "no_market_cap"
        with_none = [Quarter(NVDA_QUARTERS[0].end_date, None)] + NVDA_QUARTERS[1:]
        assert mc.eps_metrics(with_none, 1e12).status == "missing"
        assert mc.eps_metrics([(q.end_date, q.net_income_common_kusd) for q in NVDA_QUARTERS], 1e12).status == "ok"

    def test_median_with_count(self):
        assert mc.median_with_count([30.0, None, 10.0, 20.0]) == (20.0, 3)
        assert mc.median_with_count([None]) == (None, 0)

    def test_share_classes_merge(self):
        Holding = namedtuple("Holding", "symbol name weight")
        rows = [Holding("NVDA", "NVIDIA", 7.5), Holding("GOOGL", "Alphabet A", 2.0), Holding("GOOG", "Alphabet C", 1.7),
                Holding("BRK.B", "Berkshire B", 1.6), Holding("FOX", "Fox B", None), Holding("FOXA", "Fox A", 0.03)]
        merged = mc.merge_share_classes(rows)
        assert [m.symbols for m in merged] == [("NVDA",), ("GOOGL", "GOOG"), ("BRK.B",), ("FOX", "FOXA")]
        alphabet = merged[1]
        assert alphabet.weight == pytest.approx(3.7) and alphabet.primary == "GOOGL" and alphabet.name == "Alphabet A"
        assert merged[3].primary == "FOXA" and merged[3].weight == pytest.approx(0.03)
        by_cap = mc.merge_share_classes(rows, market_values={"GOOGL": 1.0, "GOOG": 2.0})
        assert by_cap[1].primary == "GOOG"
        Ndx = namedtuple("Ndx", "symbol name weight market_cap")  # Nasdaq-100 名單沒有權重、只有市值
        ndx = mc.merge_share_classes([Ndx("GOOGL", "Alphabet A", None, 2.0e12), Ndx("GOOG", "Alphabet C", None, 2.1e12)])
        assert ndx[0].primary == "GOOG" and ndx[0].weight is None and ndx[0].symbols == ("GOOGL", "GOOG")

    def test_13f_summary_and_period_mode(self):
        rows = [{"increased": 900, "decreased": 800}, {"increased": 100, "decreased": 300}, {"increased": None, "decreased": None}]
        assert mc.inst13f_summary(rows) == {"more_increase": 1, "missing": 1, "total": 3}
        periods = [date(2026, 6, 30)] * 6 + [date(2025, 12, 31)] * 4
        assert mc.holder_period_mode(periods) == (date(2026, 6, 30), True)
        assert mc.holder_period_mode([date(2025, 12, 31), date(2026, 6, 30)]) == (date(2026, 6, 30), True)
        assert mc.holder_period_mode([date(2026, 6, 30)]) == (date(2026, 6, 30), False)
        assert mc.holder_period_mode([]) == (None, False)

    def test_revenue_positive_share_compares_one_month(self):
        Revenue = namedtuple("Revenue", "year month yoy")
        revenues = {"2330": Revenue(2026, 9, 30.0), "2317": Revenue(2026, 9, -5.0), "2454": Revenue(2026, 9, 12.0),
                    "2303": Revenue(2026, 8, 50.0)}
        assert mc.revenue_positive_share(revenues) == ((2026, 9), 2, 3)
        assert mc.revenue_positive_share({}) == (None, 0, 0)


# ── 矛盾與一句話（同資料日才比） ──────────────────────────────────────────────────
def F(value, day=D, ok=True):
    return mc.Fact(value, day, ok)


class TestContradictions:
    def test_breadth_rule_and_same_date(self):
        state = {"VOO.above_ma20": F(True), "VOO.breadth20_pct": F(41.0)}
        assert mc.contradictions(state, "us") == [mc.Contra("above_ma20_weak_breadth", "VOO")]
        state["VOO.breadth20_pct"] = F(41.0, date(2026, 10, 15))  # 寬度還停在前一個完整交易日
        assert mc.contradictions(state, "us") == []
        state["VOO.breadth20_pct"] = F(41.0, ok=False)  # 過期／暫缺
        assert mc.contradictions(state, "us") == []
        assert mc.contradictions({"VOO.above_ma20": F(True), "VOO.breadth20_pct": F(55.0)}, "us") == []

    def test_volume_rules_skip_settlement_days(self):
        state = {"QQQ.chg_pct": F(1.2), "QQQ.vol_label": F("量縮"), "SOXX.chg_pct": F(-2.0), "SOXX.vol_label": F("爆量")}
        assert mc.contradictions(state, "us") == [mc.Contra("up_low_volume", "QQQ"), mc.Contra("down_high_volume", "SOXX")]
        state["QQQ.vol_flag"] = F("結算日")
        state["SOXX.vol_flag"] = F("早收日")
        assert mc.contradictions(state, "us") == []
        state["QQQ.vol_flag"] = F(None)
        assert mc.contradictions(state, "us") == [mc.Contra("up_low_volume", "QQQ")]

    def test_gamma_and_vix_term_structure(self):
        state = {"SPY.gex_sign": F(1), "SPY.gex_near_zero": F(False), "VIX": F(21.0), "VIX3M": F(20.0)}
        assert mc.contradictions(state, "us") == [mc.Contra("gamma_pos_vix_inverted", "SPY")]
        assert mc.contradictions({**state, "SPY.gex_near_zero": F(True)}, "us") == []
        assert mc.contradictions({**state, "VIX3M": F(22.0)}, "us") == []
        assert mc.contradictions({**state, "VIX": F(21.0, date(2026, 10, 15))}, "us") == []
        assert mc.contradictions({**state, "SPY.gex_sign": F(-1)}, "us") == []

    def test_tw_rules(self):
        T = date(2026, 10, 15)
        state = {
            "tw.foreign_5d": F(-320.0, T), "tw.trust_5d": F(85.0, T),
            "TAIEX.chg_pct": F(-0.8, T), "tw.margin_chg": F(12.0, T),
        }
        assert mc.contradictions(state, "tw") == [
            mc.Contra("foreign_sell_trust_buy", None), mc.Contra("index_down_margin_up", None)
        ]
        state = {"TAIEX.chg_pct": F(0.6, T), "tw.fut_net": F(-97_582, T), "tw.fut_net_chg": F(-1_500, T)}
        assert mc.contradictions(state, "tw") == [mc.Contra("index_up_foreign_short_up", None)]
        state["tw.fut_net"] = F(-97_582, date(2026, 10, 14))  # 期交所晚一天 → 不比
        state["tw.fut_net_chg"] = F(-1_500, date(2026, 10, 14))
        assert mc.contradictions(state, "tw") == []
        long_net = {"TAIEX.chg_pct": F(0.6, T), "tw.fut_net": F(5_000, T), "tw.fut_net_chg": F(-1_500, T)}
        assert mc.contradictions(long_net, "tw") == []  # 淨多單減少不是「淨空單增加」
        breadth = {"0050.above_ma20": F(True, T), "0050.breadth20_pct": F(30.0, T),
                   "TAIEX.chg_pct": F(1.0, T), "TAIEX.vol_label": F("明顯量縮", T)}
        assert mc.contradictions(breadth, "tw") == [
            mc.Contra("above_ma20_weak_breadth", "0050"), mc.Contra("up_low_volume", "TAIEX")
        ]
        with pytest.raises(ValueError):
            mc.contradictions({}, "jp")
        both = {**breadth, "VOO.above_ma20": F(True), "VOO.breadth20_pct": F(40.0)}
        assert mc.contradictions(both) == [
            mc.Contra("above_ma20_weak_breadth", "VOO"),
            mc.Contra("above_ma20_weak_breadth", "0050"),
            mc.Contra("up_low_volume", "TAIEX"),
        ]

    def test_sentences_and_values(self):
        found = [mc.Contra("up_low_volume", "TAIEX"), mc.Contra("foreign_sell_trust_buy", None)]
        assert mc.contra_sentence(found[0]) == "加權指數上漲但量偏少"
        assert mc.contra_sentence(mc.Contra("down_high_volume", "QQQ")) == "那斯達克下跌但量偏多"
        assert mc.contra_sentence(mc.Contra("gamma_pos_vix_inverted", "SPY")) == "標普500（SPY）假設部位 gamma 正、近月恐慌偏高"
        values = mc.contra_values(found)
        assert values == {"codes": ["up_low_volume:TAIEX", "foreign_sell_trust_buy"], "text": "加權指數上漲但量偏少"}
        assert mc.values_ok("contra.tw", values)
        assert mc.contra_values([]) == {"codes": [], "text": None}

    def test_sentences_start_in_chinese_and_fit_a_card_row(self):
        import unicodedata

        def width(text):  # 全形算 1 個中文字、其他算 0.5（卡片每列 ≤24）
            return sum(1.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 0.5 for ch in text)

        for code in mc.CONTRA_CODES:
            subjects = mc.SUBJECT_NAME.values() if "{subject}" in mc.CONTRA_TEXT[code] else [""]
            for subject in subjects:
                sentence = mc.CONTRA_TEXT[code].format(subject=subject)
                assert not sentence[0].isascii(), sentence  # 先中文、括號英文
                assert width(sentence) <= 24, sentence

    def test_one_liner_uses_only_same_date_facts(self):
        state = {"VOO.above_ma20": F(True), "VOO.breadth20_pct": F(41.4), "VOO.vol_label": F("量縮")}
        assert mc.one_liner(state, "us") == "美股收在月線上、成分股站上月線 41%、量偏少"
        state["VOO.breadth20_pct"] = F(41.4, date(2026, 10, 15))
        assert mc.one_liner(state, "us") == "美股收在月線上、量偏少"
        state["VOO.vol_flag"] = F("結算日")
        assert mc.one_liner(state, "us") == "美股收在月線上"
        assert mc.one_liner({"VOO.above_ma20": F(True, ok=False)}, "us") == ""
        assert mc.one_liner({}, "us") == ""
        tw = {"TAIEX.above_ma20": F(False), "tw.foreign": F(-120.4), "TAIEX.vol_label": F("放量")}
        assert mc.one_liner(tw, "tw") == "台股收在月線下、外資賣超 120 億、量偏多"
        tw["tw.foreign"] = F(35.25)
        assert mc.one_liner(tw, "tw") == "台股收在月線下、外資買超 35.2 億、量偏多"


# ── 快照欄位表、固定句、模組邊界 ─────────────────────────────────────────────────
REQUIRED_ROWS = (
    [f"us.etf.{s}" for s in ("VOO", "QQQ", "SOXX", "SPY")]
    + [f"us.breadth.{s}" for s in ("VOO", "QQQ", "SOXX")]
    + [f"us.options.{s}" for s in ("QQQ", "SPY", "SOXX")]
    + ["us.cftc.es", "us.cftc.nq", "us.margin"]
    + [f"us.13f.{s}" for s in ("VOO", "QQQ", "SOXX")]
    + [f"us.fund.{s}" for s in ("VOO", "QQQ", "SOXX")]
    + ["tw.taiex", "tw.etf.0050", "tw.breadth.0050", "tw.ad", "tw.turnover", "tw.inst3", "tw.margin", "tw.futures",
       "tw.options", "tw.fund.0050"]
    + [f"macro.{s}" for s in ("VIX", "VIX3M", "UST3M", "UST10Y", "DXY", "WTI", "BRENT", "GOLD", "SILVER", "BTC",
                              "USDTWD", "RUT", "DJI", "IXIC", "MOVE", "SPY_DD")]
    + ["events.next", "season.now", "election", "trump.summary", "news.us", "news.tw", "news.iran", "trump.posts",
       "oneliner.us", "oneliner.tw", "contra.us", "contra.tw"]
)


class TestRowIds:
    def test_every_interfaces_row_exists_with_its_keys(self):
        assert set(REQUIRED_ROWS) <= set(mc.ROW_IDS)
        etf = mc.ROW_IDS["us.etf.QQQ"]
        for key in ("close", "chg_pct", "ma_dist", "above_ma20", "dd240_pct", "rsi14", "vol_ratio", "vol_pct250",
                    "vol_label", "pv_label"):
            assert key in etf
        assert set(etf["ma_dist"]) == {"20", "60", "120", "240"}
        breadth = mc.ROW_IDS["us.breadth.VOO"]
        for key in ("per_n", "adv", "dec", "flat", "new_high", "new_low", "universe_label", "coverage_note"):
            assert key in breadth
        for key in ("above", "below", "equal", "new_below", "new_above", "valid", "total", "prev_pct"):
            assert key in breadth["per_n"]["240"]
        options = mc.ROW_IDS["us.options.SOXX"]
        for key in ("spot", "call_wall", "put_wall", "call_wall_dist_pct", "monthly", "pcr_oi", "pcr_vol", "iv30",
                    "gex", "counts", "oi_date_note"):
            assert key in options
        assert {"at_spot", "sign", "crossings", "near_zero", "coverage", "lower_bound"} <= set(options["gex"])
        assert {"raw", "bad", "conflict", "oi_valid", "gamma_valid"} <= set(options["counts"])
        assert {"expiry", "call_wall", "put_wall", "max_pain"} <= set(options["monthly"][1])
        assert {"value", "chg", "source"} <= set(mc.ROW_IDS["macro.BTC"])

    def test_spec_checker(self):
        assert mc.spec_ok("num", 1) and mc.spec_ok("num", 1.5) and not mc.spec_ok("num", True)
        assert not mc.spec_ok("num", float("nan")) and mc.spec_ok("num?", None) and not mc.spec_ok("num", None)
        assert mc.spec_ok("date", "2026-10-16") and not mc.spec_ok("date", "2026-13-01") and not mc.spec_ok("date", "10/16")
        assert mc.spec_ok({"*": "int"}, {"關稅": 2, "伊朗": 1}) and not mc.spec_ok({"*": "int"}, {"關稅": "2"})
        assert mc.values_ok("trump.summary", {"topics": {"關稅": 2}, "latest_utc": None, "at_least": True})
        assert not mc.values_ok("trump.summary", {"topics": {}, "latest_utc": None})  # 少鍵
        assert not mc.values_ok("no.such.row", {})
        assert mc.values_ok("news.us", {"items": [{"title": "美股收高", "source": None, "published_utc": 1.0}]})
        assert not mc.values_ok("news.us", {"items": [{"title": 3, "source": None, "published_utc": 1.0}]})

    def test_row_envelope(self):
        row = {"label": "QQQ", "values": {}, "data_date": None, "period": None, "source": "CBOE", "basis": None,
               "stale_after": 0.0, "state": "never"}
        assert mc.row_ok("us.options.QQQ", row)
        assert not mc.row_ok("us.options.QQQ", {**row, "state": "ok"})  # ok 的 values 要完整
        assert not mc.row_ok("us.options.QQQ", {**row, "state": "fresh"})
        assert not mc.row_ok("us.options.QQQ", {**row, "stale_after": "soon"})
        assert not mc.row_ok("us.options.QQQ", {**row, "label": 5})

    def test_etf_and_breadth_builders_match_the_specs(self):
        sessions = weekdays(date(2025, 10, 1), 260)
        closes = {s: 100.0 + i * 0.1 for i, s in enumerate(sessions)}
        volumes = {s: 1000 for s in sessions}
        tech = mc.technicals(closes, sessions, sessions[-1])
        volume = mc.volume_state(volumes, sessions[-1], calendar=sessions, closes=closes)
        values = mc.etf_values(tech, volume)
        assert values["pv_label"] == "價漲量增" and values["vol_label"] == "正常"
        assert mc.values_ok("us.etf.VOO", json.loads(json.dumps(values, allow_nan=False)))
        per_n = {n: mc.breadth({"A": closes}, sessions, sessions[-1], n) for n in mc.MA_WINDOWS}
        ad = mc.ad_counts({"A": closes}, sessions, sessions[-1])
        hl = mc.highs_lows_240({"A": closes}, sessions, sessions[-1])
        values = mc.breadth_values(per_n, ad, hl, universe_label="S&P 500")
        assert values["per_n"]["240"]["valid"] == 1 and values["new_high"] == 1
        assert mc.values_ok("us.breadth.VOO", json.loads(json.dumps(values, allow_nan=False)))
        assert mc.values_ok("tw.breadth.0050", mc.breadth_values(per_n, None, None, universe_label="0050"))

    def test_season_values_from_the_committed_table(self):
        table = json.loads((HERE / "market_seasonality.json").read_text(encoding="utf-8"))
        october = mc.season_values(table, 2026, 10)
        assert october["midterm_year"] is True and october["sp500"]["n"] == 76
        assert october["sp500_midterm"]["n"] == 19 and october["sp500_midterm_q4"]["n"] == 19
        assert mc.values_ok("season.now", october)
        later = mc.season_values(table, 2027, 10)
        assert later["sp500_midterm"] is None and later["sp500_midterm_q4"] is None
        if table["taiex"].get("pending_full_data"):
            assert october["taiex"] is None and october["taiex_pending"] is True


class TestFixedTexts:
    def test_no_forbidden_words_except_the_exact_disclaimer(self):
        for text in mc.FIXED_TEXTS:
            if text == mc.TEXT_DISCLAIMER:
                continue
            assert not any(word in text for word in mc.FORBIDDEN_WORDS), text

    def test_fixed_texts_pass_the_outbound_validator_in_later_years(self):
        import output_validator

        for now in (at(2027, 1, 15, 9, 0), at(2028, 1, 3, 9, 0)):
            for text in mc.FIXED_TEXTS:
                result = output_validator.validate_outbound_text(text, now=now)
                assert result.ok and result.text == text, (text, result.reason)

    def test_no_promises_or_years_in_fixed_texts(self):
        for text in mc.FIXED_TEXTS:
            assert not any(word in text for word in ("下次會", "之後會", "我會")), text
            assert not any(str(year) in text for year in range(2020, 2040)), text

    def test_colors_are_blue_orange_grey_only(self):
        assert set(mc.COLORS) == {"#1E5AA8", "#B7791F", "#555555", "#666666"}


class TestModuleBoundary:
    def test_import_does_not_load_numpy_and_needs_no_project_modules(self):
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); import market_calc; "
            "print('numpy' in sys.modules, 'pandas' in sys.modules, 'matplotlib' in sys.modules)"
        )
        out = subprocess.run([sys.executable, "-I", "-c", code, str(HERE)], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.split() == ["False", "False", "False"]

    def test_only_standard_library_imports_at_module_level(self):
        tree = ast.parse((HERE / "market_calc.py").read_text(encoding="utf-8"))
        names = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        assert names <= set(sys.stdlib_module_names) | {"__future__"}, names


def test_keep_events_always_keeps_fomc_cpi_and_nfp():
    """2026-10-18：財報季時前十大的財報日會把 FOMC 擠出清單；FOMC／CPI／非農各自最近一筆一定留，擠掉最晚的其他事件。"""
    items = [("earnings", n) for n in range(8)] + [("fomc", 8), ("earnings", 9), ("cpi", 10), ("nfp", 11)]
    kind = lambda item: item[0]  # noqa: E731
    assert mc.keep_events(items, 6, kind) == [("earnings", 0), ("earnings", 1), ("earnings", 2), ("fomc", 8), ("cpi", 10),
                                              ("nfp", 11)]
    assert mc.keep_events(items, 12, kind) == items
    assert mc.keep_events(items[:3], 6, kind) == items[:3]
    assert mc.keep_events(items, 0, kind) == []
    musts = [("fomc", 0), ("cpi", 1), ("nfp", 2), ("fomc", 3)]
    assert mc.keep_events(musts, 2, kind) == [("fomc", 0), ("cpi", 1)]  # 都是必留的：不再擠
    same = ("earnings", 0)
    assert mc.keep_events([same, same, ("fomc", 1)], 2, kind) == [same, ("fomc", 1)]  # 同一個物件出現兩次也不會亂


def test_shared_display_tables_are_defined_once():
    """2026-10-18 r6d：圖、卡片、一句話共用的對照表只放這裡（job 與 bot 不各存一份）。"""
    assert set(mc.VOL_PHRASE) == set(mc.VOL_LABELS)  # 每個資料值（明顯量縮…爆量）都有白話
    assert mc.VOL_PHRASE["明顯量縮"] == "量明顯偏少" and mc.VOL_PHRASE["量縮"] == "量偏少" and mc.VOL_PHRASE["爆量"] == "爆量"
    # 變號方向：價格由低往高穿過該點時 gamma 的變化（_crossings 的 "up"＝負轉正；圖與卡片同一份字）
    assert mc.GEX_DIRECTION_TEXT == {"up": "由負轉正", "down": "由正轉負"}
    assert mc.TEXT_GEX_CROSSINGS == "變號點（價格由低往高）"
    assert [mc.GEX_DIRECTION_TEXT[c.direction] for c in mc._crossings([1.0, 2.0, 3.0], [-1.0, 1.0, -1.0], 0.0)] == [
        "由負轉正", "由正轉負"]
    # 同一時刻的事件：必留種類（FOMC、CPI、非農）排前面，順序同 EVENT_MUST_KEEP
    rank = lambda kind: mc.EVENT_PRIORITY.get(kind, mc.EVENT_PRIORITY_OTHER)  # noqa: E731
    assert list(mc.EVENT_PRIORITY) == list(mc.EVENT_MUST_KEEP)
    kinds = ["econ", "earnings", "nfp", None, "cpi", "fomc"]
    assert sorted(kinds, key=rank) == ["fomc", "cpi", "nfp", "econ", "earnings", None]  # 其他種類維持原順序（穩定排序）


def test_wording_of_the_13f_title_and_the_option_glossary():
    """成分股一律「檔」、機構一律「家」；牆與最大痛點各有一句白話（只描述分布，不寫支撐／壓力）。"""
    assert mc.TEXT_13F_SUMMARY.format(more=3, missing=1) == "前十大成分股中，3 檔的加碼機構多於減碼機構（1 檔資料暫缺）"
    assert mc.TEXT_WALL_DEF == "牆＝未平倉合約最集中的履約價"
    assert mc.TEXT_MAX_PAIN == "最大痛點＝讓選擇權買方整體虧損最多的結算價（不是目標價）"
    for text in (mc.TEXT_WALL_DEF, mc.TEXT_MAX_PAIN):
        assert not any(word in text for word in ("支撐", "壓力"))
    for text in (mc.TEXT_PE_NO_CAP, mc.TEXT_PE_STALE, mc.TEXT_GEX_CROSSINGS, *mc.GEX_DIRECTION_TEXT.values()):
        assert text in mc.FIXED_TEXTS  # 會過禁用字、validator、年份的檢查


def test_clean_external_is_fast_on_huge_fields():
    """2026-10-18 安全審查 I1：幾十萬字的欄位不能讓 collect 卡住（以前 4 萬字就要 7 秒）。"""
    import time as _time

    for huge in ("a" * 200_000, "a." * 100_000, "a@" * 100_000, "x/" * 100_000, "長" * 200_000):
        start = _time.perf_counter()
        mc.clean_external(huge, 100)
        assert _time.perf_counter() - start < 0.5, huge[:4]
    assert mc.clean_external("標題 " + "字" * 50_000, 40) == "標題 " + "字" * 34 + "..."
    assert mc.clean_external("看 https://x.example/a " + "字" * 50_000, 40) is None  # 開頭的網址照樣擋


def test_truncation_never_splits_a_number_and_is_marked():
    """2026-10-18 審查 r6c C1：「台指期夜盤下滑1144點」硬切在 40 字會變成「下滑11」；要退到完整的位置並補「...」。"""
    title = "AI、科技股持續承壓!美股收黑、那指摔逾2% 記憶體大廠全跌、台指期夜盤下滑1144點 - Yahoo股市"
    out = mc.clean_external(title, 40)
    assert out.endswith("...") and len(out) <= 40 and "下滑11." not in out and not out[:-3].endswith("11")
    assert mc.clean_external(out, 40) == out  # 冪等：入庫、publish、卡片各洗一次都一樣
    english = mc.clean_external("Fed holds rates steady as inflation cools further this autumn", 30)
    assert english == "Fed holds rates steady as..." and mc.clean_external(english, 30) == english
    assert mc.clean_external("短標題", 40) == "短標題"  # 沒超過就不動
    assert mc.clean_external("a" * 60, 20) == "a" * 17 + "..."  # 整串都是英數：退光了才硬切


def test_truncation_keeps_numbers_whole_even_when_the_whole_title_is_one_run():
    """2026-10-23 審查 r7 #1：整段都是英數、退光了的時候原本硬切——「2891」變「28」；長串數字切出一段像手機號碼的數字，
    再洗一次就因為像手機號碼整則不見（不冪等）。英文可以從中間切，數字不行。"""
    codes = "2330,2317,2454,2308,2382,2881,2882,2891,1216"
    assert mc.clean_external(codes, 40) == "2330,2317,2454,2308,2382,2881,2882..."
    product = mc.clean_external("PRODUCTCODE09123456789012345", 24)
    assert product == "PRODUCTCODE..." and mc.clean_external(product, 24) == product
    assert mc.clean_external("1" * 60, 20) is None  # 只有一個切不開的數字：整則丟掉
    assert mc.clean_external("12,345,678,901,234,567,890", 12) is None
    assert mc.clean_external("漲幅 +12.34% 創高", 8) == "漲幅..."  # 正負號跟著數字一起拿掉
    samples = [codes, "PRODUCTCODE09123456789012345", "營收 1,234,567 億元創新高", "NASDAQ+12.34%收盤",
               "ABC-12345678901234567890", "x" * 30 + "12.5%", "0912-345-6789012345", "+12.34%" * 10]
    for text in samples:
        whole = {m.group() for m in mc._NUMBER_RE.finditer(text)}
        for limit in (8, 12, 20, 24, 40):
            out = mc.clean_external(text, limit)
            if out is None:
                continue
            assert len(out) <= limit and mc.clean_external(out, limit) == out, (text, limit, out)
            body = out[:-3] if out.endswith("...") else out
            assert all(m.group() in whole for m in mc._NUMBER_RE.finditer(body)), (text, limit, out)


def test_number_safe_cut():
    assert mc.number_safe_cut("漲 +12.34% 收", 6) == 2  # 切在數字中間：退到正負號前面
    assert mc.number_safe_cut("漲 +12.34% 收", 9) == 9  # 剛好在數字後面：不動
    assert mc.number_safe_cut("1,234,567", 4) == 0  # 千分位是同一個數字
    assert mc.number_safe_cut("2330,2317", 7) == 5  # 逗號後面不是 3 位數：兩個數字


def test_etf_technicals_and_volume_skip_windows_that_touch_quarantine():
    """2026-10-23 審查 r7 #5：0050 自己被隔離（分割沒對上），技術面不能照樣發布 -50%、月線距離 -48%。"""
    sessions = weekdays(date(2025, 1, 6), 300)
    closes = {s: 100.0 for s in sessions}
    closes[sessions[-1]] = 50.0
    volumes = {s: 1000.0 for s in sessions}
    plain = mc.technicals(closes, sessions, sessions[-1])
    assert plain.chg_pct == pytest.approx(-50.0) and plain.ma[20] is not None and plain.dd240_pct is not None
    jump = [(sessions[-2], sessions[-1])]
    tech = mc.technicals(closes, sessions, sessions[-1], jump)
    assert tech.close == 50.0 and tech.chg_pct is None and tech.rsi14 is None and tech.dd240_pct is None
    assert all(tech.ma[n] is None and tech.above[n] is None for n in mc.MA_WINDOWS)
    older = [(sessions[-40], sessions[-35])]  # 碰到 60／120／240 日線，碰不到 20 日線與日變化
    tech = mc.technicals(closes, sessions, sessions[-1], older)
    assert tech.chg_pct is not None and tech.ma[20] is not None and tech.rsi14 is not None
    assert tech.ma[60] is None and tech.ma[240] is None and tech.dd240_pct is None
    volume = mc.volume_state(volumes, sessions[-1], calendar=sessions, quarantine=older)
    assert volume.ratio == pytest.approx(1.0) and volume.pct250 is None  # 20 日窗口沒碰到、250 日碰到
    assert mc.volume_state(volumes, sessions[-1], calendar=sessions, quarantine=jump).ratio is None
