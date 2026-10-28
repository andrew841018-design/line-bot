"""market_sources_tw 的解析與抓取測試（/盤勢，2026-10-17）。

fixture 都是照 實測抓下來的真實格式（樣本不進 repo）裁小或合成的公開市場資料；
抓取一律注入假的 http，不連網。
"""

from __future__ import annotations

import copy
import json
import math
import pickle
import subprocess
import sys
from datetime import date, time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

import market_http
import market_sources_tw as m

T = date(2026, 10, 16)


def kind_of(fn, *args, **kwargs) -> str:
    with pytest.raises(m.ParseError) as info:
        fn(*args, **kwargs)
    return info.value.kind


# ── ALLBUT0999 ───────────────────────────────────────────────────────────────────────
INDEX_UP = "<p style ='color:red'>+</p>"  # 指數表的寫法
INDEX_DOWN = "<p style ='color:green'>-</p>"
UP = "<p style= color:red>+</p>"  # 收盤表的寫法
DOWN = "<p style= color:green>-</p>"
BLANK = "<p> </p>"

CLOSE_FIELDS = [
    "證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價",
    "漲跌(+/-)", "漲跌價差", "最後揭示買價", "最後揭示買量", "最後揭示賣價", "最後揭示賣量", "本益比",
]


def close_row(code: str, name: str, volume: str, close: str, sign: str, diff: str) -> list[str]:
    return [code, name, volume, "1,000", "123,456", close, close, close, close, sign, diff, close, "1", close, "1", "0.00"]


INDEX_TABLE = {
    "title": "115年10月16日 價格指數(臺灣證券交易所)",
    "fields": ["指數", "收盤指數", "漲跌(+/-)", "漲跌點數", "漲跌百分比(%)", "特殊處理註記"],
    "data": [
        ["寶島股價指數", "52,176.05", INDEX_UP, "175.67", "0.34", ""],
        ["發行量加權股價指數", "47,111.26", INDEX_UP, "141.39", "0.30", ""],
        ["臺灣50指數", "43,347.41", INDEX_DOWN, "8.51", "-0.02", ""],
    ],
}
RETURN_INDEX_TABLE = {
    "title": "報酬指數(臺灣證券交易所)",
    "fields": ["報酬指數", "收盤指數", "漲跌(+/-)", "漲跌點數", "漲跌百分比(%)", "特殊處理註記"],
    "data": [["發行量加權股價報酬指數", "108,938.83", INDEX_UP, "326.94", "0.30", ""]],
}
SUMMARY_TABLE = {
    "title": "115年10月16日 大盤統計資訊",
    "fields": ["成交統計", "成交金額(元)", "成交股數(股)", "成交筆數"],
    "data": [
        ["1.一般股票", "909,178,480,941", "4,589,501,506", "3,796,071"],
        ["4.ETF", "78,899,151,540", "3,437,003,113", "1,060,518"],
        ["證券合計(1+6+14+15)", "910,738,947,049", "4,599,675,373", "3,805,918"],
        ["總計(1~15)", "993,814,830,090", "11,820,630,612", "4,986,590"],
    ],
    "hints": "單位：元、股",
}
AD_TABLE = {
    "title": "漲跌證券數合計",
    "fields": ["類型", "整體市場", "股票"],
    "data": [
        ["上漲(漲停)", "9,016(68)", "684(19)"],
        ["下跌(跌停)", "6,541(84)", "286(0)"],
        ["持平", "1,190", "106"],
        ["未成交", "17,678", "5"],
        ["無比價", "3,528", "1"],
    ],
}
CLOSES_TABLE = {
    "title": "115年10月16日 每日收盤行情(全部(不含權證、牛熊證、可展延牛熊證))",
    "fields": CLOSE_FIELDS,
    "data": [
        close_row("0050", "元大台灣50", "122,160,698", "109.00", UP, "0.10"),
        close_row("2330", "台積電", "43,046,982", "2,420.00", DOWN, "20.00"),
        close_row("1101", "台泥", "0", "--", BLANK, "0.00"),  # 無成交
        close_row("1216", "統一", "5,000", "80.00", BLANK, "0.00"),  # 平盤
        close_row("2454", "聯發科", "8,683,336", "4,250.00", "<p>X</p>", "0.00"),  # 不比價
        close_row("2603", "長榮", "1,000", "1O9.50", UP, "1.00"),  # 普通股但收盤是亂碼 → 壞列
        close_row("00400A", "主動國泰動能高息", "27,595,457", "15.01", UP, "0.07"),  # ETF 不收
        close_row("2881A", "富邦特", "1,000", "abc", UP, "x"),  # 特別股不收，也不解析
        close_row("910322", "康師傅", "1,000", "--", BLANK, "0.00"),  # TDR 不收
    ],
}
ALLBUT = {
    "stat": "OK",
    "date": "20261016",
    "type": "ALLBUT0999",
    "params": {"date": "20261016", "type": "ALLBUT0999", "response": "json"},
    "tables": [INDEX_TABLE, RETURN_INDEX_TABLE, {}, SUMMARY_TABLE, AD_TABLE, CLOSES_TABLE, {}],
}


def allbut() -> dict:
    return copy.deepcopy(ALLBUT)


def table(payload: dict, title_part: str) -> dict:
    return next(t for t in payload["tables"] if t and title_part in t["title"])


def test_allbut_values():
    day = m.parse_twse_allbut0999(allbut(), T)
    assert day.date == T
    assert (day.taiex_close, day.taiex_change) == (47111.26, 141.39)
    assert day.turnover_twd == 993_814_830_090
    assert (day.adv, day.adv_limit, day.dec, day.dec_limit) == (684, 19, 286, 0)
    assert (day.flat, day.untraded, day.no_compare) == (106, 5, 1)
    assert set(day.closes) == {"0050", "2330", "1101", "1216", "2454"}
    assert day.closes["0050"] == (109.0, 122_160_698, "+", 0.1)
    assert day.closes["2330"] == (2420.0, 43_046_982, "-", -20.0)
    assert day.bad_rows == 1  # 2603 的亂碼收盤
    assert day.closes["2330"].change_sign == "-"


def test_allbut_dash_close_means_no_trade_not_zero():
    day = m.parse_twse_allbut0999(allbut(), T)
    assert day.closes["1101"] == (None, 0, "", None)
    assert day.closes["1216"] == (80.0, 5000, "", 0.0)  # 平盤是 0，不是 None
    assert day.closes["2454"] == (4250.0, 8_683_336, "X", None)  # 不比價沒有漲跌


def test_allbut_garbage_close_is_bad_row_not_no_trade():
    payload = allbut()
    table(payload, "每日收盤行情")["data"].append(close_row("1301", "台塑", "100", "N/A", UP, "1.00"))
    day = m.parse_twse_allbut0999(payload, T)
    assert "1301" not in day.closes and "2603" not in day.closes
    assert day.bad_rows == 2


def test_allbut_tables_and_columns_found_by_header_not_order():
    baseline = m.parse_twse_allbut0999(allbut(), T)
    payload = allbut()
    payload["tables"] = [{}] + list(reversed(payload["tables"]))
    ad = table(payload, "漲跌證券數合計")
    ad["fields"] = ["類型", "股票", "整體市場"]
    ad["data"] = [[row[0], row[2], row[1]] for row in ad["data"]]
    closes = table(payload, "每日收盤行情")
    order = list(reversed(range(len(CLOSE_FIELDS))))
    closes["fields"] = [CLOSE_FIELDS[i] for i in order]
    closes["data"] = [[row[i] for i in order] for row in closes["data"]]
    assert m.parse_twse_allbut0999(payload, T) == baseline


def test_allbut_advance_decline_uses_stock_column_not_whole_market():
    payload = allbut()
    table(payload, "漲跌證券數合計")["data"][0] = ["上漲(漲停)", "9,016(68)", "1,234(56)"]
    day = m.parse_twse_allbut0999(payload, T)
    assert (day.adv, day.adv_limit) == (1234, 56)


def test_allbut_turnover_is_total_1_to_15_and_matches_finmind_money():
    day = m.parse_twse_allbut0999(allbut(), T)
    assert day.turnover_twd != 910_738_947_049  # 「證券合計」會讓量能倍數低 8%
    finmind = {
        "msg": "success",
        "status": 200,
        "data": [
            {"date": "2026-10-16", "stock_id": "TAIEX", "Trading_Volume": 11820630612,
             "Trading_money": 993814830090, "open": 46995.03, "max": 47520.6, "min": 46995.03,
             "close": 47111.26, "spread": 141.39, "Trading_turnover": 4986590},
        ],
    }
    bar = m.parse_finmind_prices(finmind, "TAIEX")[0]
    assert bar.money == day.turnover_twd and bar.close == day.taiex_close


def test_allbut_date_must_equal_target_day():
    assert kind_of(m.parse_twse_allbut0999, allbut(), date(2026, 10, 15)) == "date"
    payload = allbut()
    table(payload, "每日收盤行情")["title"] = "115年10月15日 每日收盤行情(全部)"
    assert kind_of(m.parse_twse_allbut0999, payload, T) == "date"


def test_allbut_stat_handling():
    assert kind_of(m.parse_twse_allbut0999, {"stat": "很抱歉，沒有符合條件的資料!"}, T) == "empty"
    assert kind_of(m.parse_twse_allbut0999, {"stat": "系統忙碌中"}, T) == "format"
    assert kind_of(m.parse_twse_allbut0999, [], T) == "format"
    payload = allbut()
    payload["tables"] = "x"
    assert kind_of(m.parse_twse_allbut0999, payload, T) == "format"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["tables"].remove(table(p, "每日收盤行情")),
        lambda p: table(p, "大盤統計資訊")["data"].pop(),  # 拿掉「總計(1~15)」
        lambda p: table(p, "漲跌證券數合計").update(fields=["類型", "整體市場"]),
        lambda p: table(p, "價格指數(臺灣證券交易所)")["data"].pop(1),  # 拿掉加權指數
        lambda p: table(p, "每日收盤行情")["data"].pop(0),  # 拿掉 0050
        lambda p: table(p, "漲跌證券數合計")["data"].pop(),  # 拿掉「無比價」
    ],
    ids=["no_closes", "no_total", "no_stock_column", "no_taiex", "no_0050", "no_no_compare"],
)
def test_allbut_missing_parts(mutate):
    payload = allbut()
    mutate(payload)
    assert kind_of(m.parse_twse_allbut0999, payload, T) == "missing_field"


def test_allbut_too_many_bad_rows_rejects_whole_day():
    payload = allbut()
    rows = table(payload, "每日收盤行情")["data"]
    rows.extend(close_row(f"{1400 + i}", "x", "1", "壞", UP, "1.00") for i in range(m.MAX_BAD_CLOSE_ROWS))
    assert kind_of(m.parse_twse_allbut0999, payload, T) == "format"


def test_allbut_unknown_sign_is_bad_row():
    payload = allbut()
    table(payload, "每日收盤行情")["data"].append(close_row("1301", "台塑", "100", "50.00", "<p>?</p>", "1.00"))
    day = m.parse_twse_allbut0999(payload, T)
    assert "1301" not in day.closes and day.bad_rows == 2


def test_allbut_conflicting_duplicate_code_dropped_identical_kept():
    payload = allbut()
    rows = table(payload, "每日收盤行情")["data"]
    rows.append(close_row("2330", "台積電", "43,046,982", "2,425.00", DOWN, "15.00"))
    rows.append(close_row("0050", "元大台灣50", "122,160,698", "109.00", UP, "0.10"))
    day = m.parse_twse_allbut0999(payload, T)
    assert "2330" not in day.closes and day.bad_rows == 2
    assert day.closes["0050"].close == 109.0


def test_allbut_taiex_sign_checked_against_signed_percent():
    payload = allbut()
    table(payload, "價格指數(臺灣證券交易所)")["data"][1][2] = INDEX_DOWN  # 百分比仍是 +0.30
    assert kind_of(m.parse_twse_allbut0999, payload, T) == "conflict"
    payload = allbut()
    table(payload, "價格指數(臺灣證券交易所)")["data"][1][2] = "<img src='up.gif'>"
    assert kind_of(m.parse_twse_allbut0999, payload, T) == "format"


def test_allbut_needs_at_least_one_common_stock_and_valid_extra_codes():
    payload = allbut()
    table(payload, "每日收盤行情")["data"] = [close_row("0050", "元大台灣50", "1", "109.00", UP, "0.10")]
    assert kind_of(m.parse_twse_allbut0999, payload, T) == "too_few"
    with pytest.raises(ValueError):
        m.parse_twse_allbut0999(allbut(), T, extra_codes=("../x",))
    day = m.parse_twse_allbut0999(allbut(), T, extra_codes=("0050", "00400A"))
    assert day.closes["00400A"].close == 15.01


@pytest.mark.parametrize("cell", ["684(19", "--", "684(19)(2)", "-684"])
def test_allbut_bad_count_cell_is_format(cell):
    payload = allbut()
    table(payload, "漲跌證券數合計")["data"][0][2] = cell
    assert kind_of(m.parse_twse_allbut0999, payload, T) == "format"


def test_allbut_full_width_count_brackets_normalised():
    payload = allbut()
    table(payload, "漲跌證券數合計")["data"][0][2] = "684（19）"
    assert m.parse_twse_allbut0999(payload, T).adv_limit == 19


# ── BFI82U ─────────────────────────────────────────────────────────────────────────
BFI = {
    "stat": "OK",
    "date": "20261016",
    "title": "115年10月16日 三大法人買賣金額統計表",
    "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"],
    "data": [
        ["自營商(自行買賣)", "8,381,436,969", "18,560,032,105", "-10,178,595,136"],
        ["自營商(避險)", "28,653,271,647", "51,888,684,916", "-23,235,413,269"],
        ["投信", "43,848,499,145", "32,800,992,175", "11,047,506,970"],
        ["外資及陸資(不含外資自營商)", "356,324,360,692", "402,471,523,529", "-46,147,162,837"],
        ["外資自營商", "0", "0", "0"],
        ["合計", "437,207,568,453", "505,721,232,725", "-68,513,664,272"],
    ],
    "hints": "單位：元",
}


def bfi() -> dict:
    return copy.deepcopy(BFI)


def test_bfi82u_values():
    inst = m.parse_twse_bfi82u(bfi(), expect_date=T)
    assert inst == m.Inst3(T, -46_147_162_837, 11_047_506_970, -10_178_595_136, -23_235_413_269)


def test_bfi82u_foreign_excludes_foreign_dealer():
    payload = bfi()
    payload["data"][4] = ["外資自營商", "5,000", "1,000", "4,000"]  # 已含在自營商，不在合計內
    assert m.parse_twse_bfi82u(payload).foreign == -46_147_162_837


def test_bfi82u_label_rules():
    payload = bfi()
    payload["data"][3][0] = "外資及陸資（不含外資自營商）"  # 全形括號
    assert m.parse_twse_bfi82u(payload).foreign == -46_147_162_837
    payload = bfi()
    payload["data"][3][0] = "外資及陸資"  # 舊格式名稱，不猜
    assert kind_of(m.parse_twse_bfi82u, payload) == "missing_field"


def test_bfi82u_consistency_checks():
    payload = bfi()
    payload["data"][5] = ["合計", "1", "0", "1"]
    assert kind_of(m.parse_twse_bfi82u, payload) == "conflict"
    payload = bfi()
    payload["data"][3][3] = "-46,147,162,830"
    assert kind_of(m.parse_twse_bfi82u, payload) == "conflict"


def test_bfi82u_dates_and_stat():
    assert kind_of(m.parse_twse_bfi82u, bfi(), expect_date=date(2026, 10, 15)) == "date"
    payload = bfi()
    payload["title"] = "115年10月15日 三大法人買賣金額統計表"
    assert kind_of(m.parse_twse_bfi82u, payload) == "date"
    assert kind_of(m.parse_twse_bfi82u, {"stat": "很抱歉，沒有符合條件的資料!"}) == "empty"
    payload = bfi()
    payload["fields"] = ["單位名稱", "買進金額", "賣出金額"]
    assert kind_of(m.parse_twse_bfi82u, payload) == "missing_field"


# ── MI_MARGN ───────────────────────────────────────────────────────────────────────
MARGIN = {
    "stat": "OK",
    "date": "20261016",
    "tables": [
        {
            "title": "115年10月16日 信用交易統計",
            "fields": ["項目", "買進", "賣出", "現金(券)償還", "前日餘額", "今日餘額"],
            "data": [
                ["融資(交易單位)", "343,600", "318,542", "7,624", "9,521,230", "9,538,664"],
                ["融券(交易單位)", "20,982", "30,004", "4,570", "229,150", "233,602"],
                ["融資金額(仟元)", "28,656,955", "28,942,556", "536,222", "646,415,871", "645,594,048"],
            ],
        },
        {},
    ],
}


def test_margin_values():
    margin = m.parse_twse_margin_ms(copy.deepcopy(MARGIN), expect_date=T)
    assert margin == m.MarginTw(T, 645_594_048, 646_415_871, 233_602, 229_150)


def test_margin_errors():
    payload = copy.deepcopy(MARGIN)
    payload["tables"][0]["data"].pop()
    assert kind_of(m.parse_twse_margin_ms, payload) == "missing_field"
    assert kind_of(m.parse_twse_margin_ms, copy.deepcopy(MARGIN), expect_date=date(2026, 10, 15)) == "date"
    payload = copy.deepcopy(MARGIN)
    payload["tables"][0]["title"] = "115年10月15日 信用交易統計"
    assert kind_of(m.parse_twse_margin_ms, payload) == "date"
    payload = copy.deepcopy(MARGIN)
    payload["tables"][0]["data"][2][5] = "--"
    assert kind_of(m.parse_twse_margin_ms, payload) == "format"


# ── TWT49U ─────────────────────────────────────────────────────────────────────────
TWT49U_FIELDS = [
    "資料日期", "股票代號", "股票名稱", "除權息前收盤價", "除權息參考價", "權值+息值", "權/息", "漲停價格", "跌停價格",
    "開盤競價基準", "減除股利參考價", "詳細資料", "最近一次申報資料 季別/日期", "最近一次申報每股 (單位)淨值",
    "最近一次申報每股 (單位)盈餘",
]


def ex_row(day: str, code: str, pre: str, ref: str, kind: str) -> list[str]:
    return [day, code, "名稱", pre, ref, "1.000000", kind, "1", "1", ref, ref, f"{code},x", "", "", ""]


def twt49u(rows: list[list[str]], start: str = "20260601", end: str = "20261016") -> dict:
    return {"stat": "OK", "title": "115年06月01日 至 115年10月16日 除權除息計算結果表",
            "fields": TWT49U_FIELDS, "data": rows, "strDate": start, "endDate": end}


EX_ROWS = [
    ex_row("115年06月11日", "1312", "13.20", "13.40", "權"),  # 現增：參考價比前收高
    ex_row("115年06月01日", "2612", "57.70", "55.50", "息"),
    ex_row("115年07月21日", "0050", "99.20", "98.60", "息"),  # 0050 自己也除息，不篩代號
    ex_row("115年07月01日", "2887Z1", "50.00", "49.00", "息"),
    ex_row("115年09月16日", "2330", "2,385.00", "2,377.99", "權息"),
]


def test_twt49u_roc_dates_and_rows():
    events = m.parse_twse_twt49u(twt49u(copy.deepcopy(EX_ROWS)), expect_start=date(2026, 6, 1), expect_end=T)
    assert [(e.date, e.code) for e in events] == [
        (date(2026, 6, 1), "2612"), (date(2026, 6, 11), "1312"), (date(2026, 7, 1), "2887Z1"),
        (date(2026, 7, 21), "0050"), (date(2026, 9, 16), "2330"),
    ]
    assert events[1] == m.ExRight(date(2026, 6, 11), "1312", 13.2, 13.4, "權")
    assert events[-1].pre_close == 2385.0 and events[-1].kind == "權息"


def test_twt49u_range_must_match_request():
    payload = twt49u(copy.deepcopy(EX_ROWS))
    assert kind_of(m.parse_twse_twt49u, payload, expect_start=date(2026, 6, 2)) == "date"
    assert kind_of(m.parse_twse_twt49u, payload, expect_end=date(2026, 10, 17)) == "date"
    assert kind_of(m.parse_twse_twt49u, twt49u([ex_row("115年10月17日", "2330", "1", "1", "息")])) == "date"
    assert kind_of(m.parse_twse_twt49u, twt49u([], start="20261016", end="20260601")) == "date"


def test_twt49u_bad_rows_and_duplicates():
    assert kind_of(m.parse_twse_twt49u, twt49u([ex_row("115年06月01日", "2612", "--", "55.50", "息")])) == "format"
    assert kind_of(m.parse_twse_twt49u, twt49u([ex_row("115/06/01", "2612", "57.70", "55.50", "息")])) == "format"
    assert kind_of(m.parse_twse_twt49u, twt49u([ex_row("115年06月01日", "26 12", "57.70", "55.50", "息")])) == "format"
    same = ex_row("115年06月01日", "2612", "57.70", "55.50", "息")
    assert len(m.parse_twse_twt49u(twt49u([same, list(same)]))) == 1
    other = ex_row("115年06月01日", "2612", "57.70", "55.40", "息")
    assert kind_of(m.parse_twse_twt49u, twt49u([same, other])) == "conflict"


def test_twt49u_no_events_is_empty_list():
    assert m.parse_twse_twt49u({"stat": "很抱歉，沒有符合條件的資料!"}) == []
    assert m.parse_twse_twt49u(twt49u([])) == []
    assert kind_of(m.parse_twse_twt49u, {"stat": "系統忙碌中"}) == "format"
    payload = twt49u(copy.deepcopy(EX_ROWS))
    payload["fields"] = TWT49U_FIELDS[:3]
    assert kind_of(m.parse_twse_twt49u, payload) == "missing_field"


# ── 休市表（2026 年 27 列，證交所原文） ─────────────────────────────────────────────────
_SPRING = "依規定於2月15日至2月19日放假5日。2月15日適逢星期日，於2月20日（星期五）補假。"
HOLIDAY_ROWS = [
    ["2026-01-01", "中華民國開國紀念日", "依規定放假1日。"],
    ["2026-01-02", "國曆新年開始交易日", "國曆新年開始交易。"],
    ["2026-02-11", "農曆春節前最後交易日", "農曆春節前最後交易。\r\n"],
    ["2026-02-12", "市場無交易，僅辦理結算交割作業", ""],
    ["2026-02-13", "市場無交易，僅辦理結算交割作業", ""],
    ["2026-02-15", "農曆除夕及春節", _SPRING],
    ["2026-02-16", "農曆除夕及春節", _SPRING],
    ["2026-02-17", "農曆除夕及春節", _SPRING + "\r\n\r\n\r\n"],
    ["2026-02-18", "農曆除夕及春節", _SPRING],
    ["2026-02-19", "農曆除夕及春節", _SPRING],
    ["2026-02-20", "農曆除夕及春節", _SPRING],
    ["2026-02-23", "農曆春節後開始交易日", "農曆春節後開始交易。"],
    ["2026-02-27", "和平紀念日", "和平紀念日為2月28日適逢星期六，於2月27日（星期五）補假。"],
    ["2026-02-28", "和平紀念日", "依規定放假1日。"],
    ["2026-04-03", "兒童節及民族掃墓節", "兒童節為4月4日適逢星期六，於4月3日（星期五）補假。"],
    ["2026-04-04", "兒童節及民族掃墓節", "依規定放假1日。"],
    ["2026-04-05", "兒童節及民族掃墓節", "依規定放假1日。"],
    ["2026-04-06", "兒童節及民族掃墓節", "民族掃墓節為4月5日適逢星期日，於4月6日（星期一）補假。"],
    ["2026-05-01", "勞動節", "依規定放假1日。"],
    ["2026-06-19", "端午節", "依規定放假1日。"],
    ["2026-09-25", "中秋節", "依規定放假1日。"],
    ["2026-09-28", "孔子誕辰紀念日/ 教師節", "依規定放假1日。"],
    ["2026-10-09", "國慶日", "國慶日為10月10日適逢星期六，於10月9日（星期五）補假。"],
    ["2026-10-10", "國慶日", "依規定放假1日。"],
    ["2026-10-25", "臺灣光復暨金門古寧頭大捷紀念日", "依規定放假1日。"],
    ["2026-10-26", "臺灣光復暨金門古寧頭大捷紀念日", "臺灣光復暨金門古寧頭大捷紀念日為10月25日適逢星期日，於10月26日（星期一）補假。"],
    ["2026-12-25", "行憲紀念日", "依規定放假1日。"],
]


def holidays(rows=None, **extra) -> dict:
    data = copy.deepcopy(HOLIDAY_ROWS if rows is None else rows)
    payload = {"stat": "ok", "date": "20260101", "title": "115 年市場開休市日期", "fields": ["日期", "名稱", "說明"],
               "data": data, "queryYear": 2026, "total": len(data)}
    payload.update(extra)
    return payload


def test_holidays_2026_classified_by_name_and_note():
    rows = m.parse_twse_holidays(holidays(), 2026)
    assert len(rows) == 27
    kinds = {}
    for item in rows:
        kinds[item.kind] = kinds.get(item.kind, 0) + 1
    assert kinds == {m.HOLIDAY_CLOSED: 22, m.HOLIDAY_TRADING: 3, m.HOLIDAY_SETTLEMENT_ONLY: 2}
    by_day = {item.date: item for item in rows}
    for day in (date(2026, 1, 2), date(2026, 2, 11), date(2026, 2, 23)):
        assert by_day[day].kind == m.HOLIDAY_TRADING and by_day[day].is_trading is True
    for day in (date(2026, 2, 12), date(2026, 2, 13)):
        assert by_day[day].kind == m.HOLIDAY_SETTLEMENT_ONLY and by_day[day].is_trading is False
    assert by_day[date(2026, 2, 20)].kind == m.HOLIDAY_CLOSED  # 補假
    assert by_day[date(2026, 2, 11)].note == "農曆春節前最後交易。"  # \r\n 去掉


def test_holidays_2026_non_trading_weekdays():
    rows = m.parse_twse_holidays(holidays(), 2026)
    closed_weekdays = {item.date for item in rows if item.is_trading is False and item.date.weekday() < 5}
    assert closed_weekdays == {
        date(2026, 1, 1), date(2026, 2, 12), date(2026, 2, 13), date(2026, 2, 16), date(2026, 2, 17),
        date(2026, 2, 18), date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 27), date(2026, 4, 3),
        date(2026, 4, 6), date(2026, 5, 1), date(2026, 6, 19), date(2026, 9, 25), date(2026, 9, 28),
        date(2026, 10, 9), date(2026, 10, 26), date(2026, 12, 25),
    }


def test_holidays_unknown_text_is_flagged_not_guessed():
    extra = [["2026-03-02", "特別開市日", "照常交易。"], ["2026-03-03", "颱風", "上午放假，下午開始交易。"]]
    rows = {item.date: item for item in m.parse_twse_holidays(holidays(HOLIDAY_ROWS + extra), 2026)}
    assert rows[date(2026, 3, 2)].kind == m.HOLIDAY_UNKNOWN and rows[date(2026, 3, 2)].is_trading is None
    assert rows[date(2026, 3, 3)].kind == m.HOLIDAY_UNKNOWN
    assert m.classify_twse_holiday("市場無交易，僅辦理結算交割作業", "") == m.HOLIDAY_SETTLEMENT_ONLY
    assert m.classify_twse_holiday("某日", "休市一日") == m.HOLIDAY_CLOSED


def test_holidays_text_cleaned_but_kind_from_raw_text():
    rows = [
        ["2026-03-02", "國慶日\u2028", "詳見 https://www.twse.com.tw 公告，依規定放假1日。"],
        ["2026-03-03", "特別\u200b日", "@全體 依規定放假1日。"],
    ]
    first, second = m.parse_twse_holidays(holidays(rows), 2026)
    assert (first.name, first.note, first.kind) == ("國慶日", "", m.HOLIDAY_CLOSED)  # 含網址的說明整則丟掉
    assert second.name == "特別日" and "@" not in second.note and second.note.endswith("依規定放假1日。")
    assert len(m.parse_twse_holidays(holidays([["2026-03-04", "名" * 40, "依規定放假1日。"]]), 2026)[0].name) == 24


def test_holidays_same_day_two_kinds_becomes_unknown():
    rows = [["2026-01-02", "國曆新年開始交易日", "國曆新年開始交易。"], ["2026-01-02", "補假", "依規定放假1日。"]]
    assert {item.kind for item in m.parse_twse_holidays(holidays(rows), 2026)} == {m.HOLIDAY_UNKNOWN}


def test_holidays_year_total_and_stat_checks():
    assert kind_of(m.parse_twse_holidays, holidays(), 2027) == "date"
    payload = holidays([["2025-12-31", "x", "依規定放假1日。"]])
    del payload["queryYear"]
    assert kind_of(m.parse_twse_holidays, payload, 2026) == "date"
    assert kind_of(m.parse_twse_holidays, holidays(total=28), 2026) == "too_few"
    assert kind_of(m.parse_twse_holidays, holidays([]), 2026) == "empty"
    assert kind_of(m.parse_twse_holidays, {"stat": "很抱歉，沒有符合條件的資料!"}, 2026) == "empty"
    payload = holidays()
    payload["stat"] = "OK"  # 大小寫都收
    assert len(m.parse_twse_holidays(payload, 2026)) == 27
    payload = holidays([["2026/01/01", "x", "依規定放假1日。"]])
    assert kind_of(m.parse_twse_holidays, payload, 2026) == "format"


# ── 證交所 OpenAPI ───────────────────────────────────────────────────────────────────
PE_ROWS = [
    {"Date": "1151008", "Code": "2330", "PEratio": "29.56"},
    {"Date": "1151008", "Code": "2603", "PEratio": "-"},
    {"Date": "1151008", "Code": "1216", "PEratio": "0"},
    {"Date": "1151008", "Code": "1101", "PEratio": "10"},
    {"Date": "1151008", "Code": "1101", "PEratio": "11"},
    {"Date": "1151008", "Code": "2317", "PEratio": "16.08"},
    {"Date": "1151008", "Code": "2317", "PEratio": "16.08"},
]


def test_bwibbu_values_and_date():
    rows = copy.deepcopy(PE_ROWS)
    assert m.bwibbu_date(rows) == date(2026, 10, 8)
    assert m.parse_openapi_bwibbu(rows) == {"2330": 29.56, "2603": None, "1216": None, "2317": 16.08}


def test_bwibbu_errors():
    rows = copy.deepcopy(PE_ROWS) + [{"Date": "1151007", "Code": "2454", "PEratio": "70.19"}]
    assert kind_of(m.parse_openapi_bwibbu, rows) == "date"
    assert kind_of(m.parse_openapi_bwibbu, copy.deepcopy(PE_ROWS), expect_date=date(2026, 10, 9)) == "date"
    assert kind_of(m.parse_openapi_bwibbu, []) == "empty"
    assert kind_of(m.parse_openapi_bwibbu, {"Code": "2330"}) == "format"
    assert kind_of(m.parse_openapi_bwibbu, [{"Date": "1151008", "Code": "2330", "PEratio": "abc"}]) == "format"


def test_revenue_latest_month_and_conflicts():
    rows = [
        {"資料年月": "11509", "公司代號": "2330", "營業收入-去年同月增減(%)": "30.5"},
        {"資料年月": "11508", "公司代號": "2330", "營業收入-去年同月增減(%)": "53.3"},
        {"資料年月": "11508", "公司代號": "2344", "營業收入-去年同月增減(%)": "1"},
        {"資料年月": "11508", "公司代號": "2344", "營業收入-去年同月增減(%)": "2"},
        {"資料年月": "11508", "公司代號": "1101", "營業收入-去年同月增減(%)": ""},
    ]
    assert m.parse_openapi_revenue(rows) == {"2330": m.Revenue(2026, 9, 30.5), "1101": m.Revenue(2026, 8, None)}
    assert kind_of(m.parse_openapi_revenue, [{"資料年月": "11513", "公司代號": "2330"}]) == "format"
    assert kind_of(m.parse_openapi_revenue, []) == "empty"


# ── 期交所 ──────────────────────────────────────────────────────────────────────────
def fut_row(contract: str, item: str, long: int, short: int, trade_net: int, day: str = "20261015", net=None) -> dict:
    return {
        "Date": day, "ContractCode": contract, "Item": item,
        "TradingVolume(Long)": "1", "TradingValue(Long)(Thousands)": "1", "TradingVolume(Short)": "1",
        "TradingValue(Short)(Thousands)": "1", "TradingVolume(Net)": str(trade_net), "TradingValue(Net)(Thousands)": "0",
        "OpenInterest(Long)": str(long), "ContractValueofOpenInterest(Long)(Thousands)": "1",
        "OpenInterest(Short)": str(short), "ContractValueofOpenInterest(Short)(Thousands)": "1",
        "OpenInterest(Net)": str(long - short if net is None else net), "ContractValueofOpenInterest(Net)(Thousands)": "0",
    }


FUT_ROWS = [
    fut_row("臺股期貨", "自營商", 10619, 3725, 4498),
    fut_row("臺股期貨", "外資及陸資", 9171, 106753, -9837),
    fut_row("小型臺指期貨", "外資及陸資", 1775, 5703, -7016),
    fut_row("微型臺指期貨", "外資及陸資", 5330, 17745, -12011),
    fut_row("小型電子期貨", "外資及陸資", 100, 50, 0),
]


def test_futures_inst_big_mini_micro_separately():
    inst = m.parse_taifex_futures_inst(copy.deepcopy(FUT_ROWS))
    assert inst.date == date(2026, 10, 15)
    assert inst.foreign == {
        "TX": (9171, 106753, -97582, -9837),
        "MTX": (1775, 5703, -3928, -7016),
        "TMF": (5330, 17745, -12415, -12011),
    }


def test_futures_inst_errors():
    rows = copy.deepcopy(FUT_ROWS)
    rows[0]["Date"] = "20261014"
    assert kind_of(m.parse_taifex_futures_inst, rows) == "date"
    assert kind_of(m.parse_taifex_futures_inst, copy.deepcopy(FUT_ROWS[2:])) == "missing_field"
    assert kind_of(m.parse_taifex_futures_inst, [fut_row("台股期貨", "外資及陸資", 1, 2, 0)]) == "missing_field"
    assert kind_of(m.parse_taifex_futures_inst, [fut_row("臺股期貨", "外資及陸資", 1, 2, 0, net=5)]) == "conflict"
    assert kind_of(m.parse_taifex_futures_inst, []) == "empty"
    assert kind_of(m.parse_taifex_futures_inst, {"Date": "20261015"}) == "format"


def cp_row(contract: str, side: str, item: str, long: int, short: int, value_net: int) -> dict:
    row = fut_row(contract, item, long, short, 0)
    row["CallPut"] = side
    row["ContractValueofOpenInterest(Net)(Thousands)"] = str(value_net)
    return row


CP_ROWS = [
    cp_row("臺指選擇權", "CALL", "外資及陸資", 18797, 18966, -45413),
    cp_row("臺指選擇權", "PUT", "外資及陸資", 15971, 10151, 211974),
    cp_row("臺指選擇權", "CALL", "投信", 0, 9171, -550974),
    cp_row("電子選擇權", "CALL", "外資及陸資", 5, 1, 10),
]


def test_calls_puts_inst_foreign_txo():
    inst = m.parse_taifex_calls_puts_inst(copy.deepcopy(CP_ROWS))
    assert inst.date == date(2026, 10, 15)
    assert inst.foreign_call == (18797, 18966, -169, -45413)
    assert inst.foreign_put == (15971, 10151, 5820, 211974)
    assert kind_of(m.parse_taifex_calls_puts_inst, copy.deepcopy(CP_ROWS[:1])) == "missing_field"
    rows = copy.deepcopy(CP_ROWS)
    rows[0]["CallPut"] = "XX"
    assert kind_of(m.parse_taifex_calls_puts_inst, rows) == "format"


PCR_HEADER = "日期,賣權成交量,買權成交量,買賣權成交量比率%,賣權未平倉量,買權未平倉量,買賣權未平倉量比率%"
PCR_LINES = [
    "20261015,191704,255629,74.99,77287,127036,60.84",
    "20261014,337868,326539,103.47,50695,68105,74.44",
    "20261013,204511,230410,88.76,77121,111852,68.95",
]


def pcr_text(header: str = PCR_HEADER, lines=PCR_LINES, newline: str = "\n") -> str:
    return "\ufeff" + newline.join([header, *lines]) + newline


def test_pcr_csv_with_bom_sorted_old_to_new():
    rows = m.parse_taifex_pcr_csv(pcr_text())
    assert [row.date for row in rows] == [date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)]
    assert rows[-1] == m.PcrRow(date(2026, 10, 15), 191704, 255629, 74.99, 77287, 127036, 60.84)
    assert m.parse_taifex_pcr_csv(pcr_text(newline="\r\n").encode("utf-8")) == rows  # bytes＋BOM＋CRLF
    assert m.parse_taifex_pcr_csv(pcr_text()[1:]) == rows  # 沒有 BOM 也可以


def test_pcr_csv_columns_found_by_header():
    names = PCR_HEADER.split(",")
    order = [0, 6, 5, 4, 3, 2, 1]
    lines = [",".join(line.split(",")[i] for i in order) for line in PCR_LINES]
    assert m.parse_taifex_pcr_csv(pcr_text(",".join(names[i] for i in order), lines)) == m.parse_taifex_pcr_csv(pcr_text())


def test_pcr_csv_errors():
    assert kind_of(m.parse_taifex_pcr_csv, pcr_text(PCR_HEADER.replace(",買賣權未平倉量比率%", ""))) == "missing_field"
    bad = ["20261015,191704,255629,74.99,77287,127036,70.84"]  # 比率和兩個量對不上 → 欄位錯位
    assert kind_of(m.parse_taifex_pcr_csv, pcr_text(lines=bad)) == "conflict"
    dup = [PCR_LINES[0], "20261015,191704,255629,74.99,77287,127037,60.84"]
    assert kind_of(m.parse_taifex_pcr_csv, pcr_text(lines=dup)) == "conflict"
    assert kind_of(m.parse_taifex_pcr_csv, pcr_text(lines=[])) == "empty"
    assert kind_of(m.parse_taifex_pcr_csv, "") == "empty"
    assert kind_of(m.parse_taifex_pcr_csv, b"\xff\xfe\x00bad") == "format"
    assert kind_of(m.parse_taifex_pcr_csv, pcr_text(lines=["2026-10-15,1,1,100.00,1,1,100.00"])) == "format"


def _pcr_json(*rows) -> bytes:
    keys = ("Date", "PutVolume", "CallVolume", "PutCallVolumeRatio%", "PutOI", "CallOI", "PutCallOIRatio%")
    return json.dumps([dict(zip(keys, row)) for row in rows]).encode()


def test_pcr_json_format_is_accepted():
    """2026-10-18 實測：同一個網址今天回 JSON（英文欄名），10/17 是 CSV；兩種都要收、結果一樣。"""
    rows = m.parse_taifex_pcr_csv(_pcr_json(
        ("20261016", "396499", "446741", "88.75", "55093", "81497", "67.60"),
        ("20261015", "191704", "255629", "74.99", "77287", "127036", "60.84"),
    ))
    assert [r.date for r in rows] == [date(2026, 10, 15), date(2026, 10, 16)]
    assert rows[-1].oi_ratio_pct == 67.6 and rows[-1].put_oi == 55093
    assert rows[0] == m.parse_taifex_pcr_csv(pcr_text(lines=[PCR_LINES[0]]))[0]  # 和 CSV 的同一天一模一樣
    assert kind_of(m.parse_taifex_pcr_csv, b'[{"Date": "20261016"}]') == "missing_field"
    assert kind_of(m.parse_taifex_pcr_csv, _pcr_json(("20261016", "396499", "446741", "88.75", "55093", "81497", "70.00"))) == "conflict"
    assert kind_of(m.parse_taifex_pcr_csv, b"[]") == "empty"
    assert kind_of(m.parse_taifex_pcr_csv, b"[{") == "format"


def opt_row(contract, month, strike, side, session, oi, settle, day="20261015") -> dict:
    return {
        "Date": day, "Contract": contract, "ContractMonth(Week)": month, "StrikePrice": strike, "CallPut": side,
        "Open": "-", "High": "-", "Low": "-", "Close": "-", "Volume": "0", "SettlementPrice": settle,
        "OpenInterest": oi, "BestBid": "-", "BestAsk": "-", "HistoricalHigh": "-", "HistoricalLow": "-",
        "TradingHalt": "", "TradingSession": session,
    }


OPT_ROWS = [
    opt_row("TXO", "202610", "33900", "買權", "一般", "0", "13070"),
    opt_row("TXO", "202610", "33900", "買權", "盤後", "-", "-"),  # 盤後同鍵，只收一般盤
    opt_row("TXO", "202610", "33900", "賣權", "一般", "368", "1.4"),
    opt_row("TXO", "202610", "47100", "賣權", "一般", "-", "-"),
    opt_row("TXO", "202610W2", "47000", "賣權", "一般", "999", "50"),  # 週選
    opt_row("TXO", "202611", "47000", "買權", "一般", "50", "900"),
    opt_row("CAO", "202610", "150", "買權", "一般", "0", "165"),
]


def test_daily_options_txo_regular_session_monthly():
    rows = m.parse_taifex_daily_options(copy.deepcopy(OPT_ROWS))
    assert [(r.month, r.strike, r.kind, r.oi, r.settle) for r in rows] == [
        ("202610", 33900.0, "C", 0, 13070.0),
        ("202610", 33900.0, "P", 368, 1.4),
        ("202610", 47100.0, "P", None, None),
        ("202611", 47000.0, "C", 50, 900.0),
    ]
    assert {r.date for r in rows} == {date(2026, 10, 15)}


def test_daily_options_weekly_kept_verbatim_only_when_asked():
    rows = m.parse_taifex_daily_options(copy.deepcopy(OPT_ROWS), monthly_only=False)
    assert ("202610W2", 47000.0, "P", 999) in [(r.month, r.strike, r.kind, r.oi) for r in rows]
    assert not any(r.month == "202610" and r.strike == 47000.0 for r in rows)
    cao = m.parse_taifex_daily_options(copy.deepcopy(OPT_ROWS), "CAO")
    assert [(r.strike, r.kind, r.settle) for r in cao] == [(150.0, "C", 165.0)]


def test_daily_options_errors():
    rows = copy.deepcopy(OPT_ROWS)
    rows[-1]["Date"] = "20261014"
    assert kind_of(m.parse_taifex_daily_options, rows) == "date"
    rows = copy.deepcopy(OPT_ROWS) + [opt_row("TXO", "202610", "33900", "賣權", "一般", "369", "1.4")]
    assert kind_of(m.parse_taifex_daily_options, rows) == "conflict"
    rows = copy.deepcopy(OPT_ROWS) + [opt_row("TXO", "202610", "33900", "賣權", "一般", "368", "1.4")]
    assert len(m.parse_taifex_daily_options(rows)) == 4
    assert kind_of(m.parse_taifex_daily_options, [opt_row("TXO", "2026-10", "1", "買權", "一般", "0", "1")]) == "format"
    assert kind_of(m.parse_taifex_daily_options, [opt_row("TXO", "202613", "1", "買權", "一般", "0", "1")]) == "format"
    assert kind_of(m.parse_taifex_daily_options, copy.deepcopy(OPT_ROWS[-1:])) == "empty"


FX_ROWS = [
    {"Date": "20261014", "USD/NTD": "32.073", "RMB/NTD": "4.8"},
    {"Date": "20261016", "USD/NTD": "32.338", "RMB/NTD": "4.836245"},
    {"Date": "20261015", "USD/NTD": "32.265", "RMB/NTD": "4.83"},
]


def test_fx_latest_with_previous_row():
    assert m.parse_taifex_fx(copy.deepcopy(FX_ROWS)) == m.FxRow(T, 32.338, date(2026, 10, 15), 32.265)
    rows = copy.deepcopy(FX_ROWS)
    rows[1]["USD/NTD"] = "-"
    assert m.parse_taifex_fx(rows) == m.FxRow(date(2026, 10, 15), 32.265, date(2026, 10, 14), 32.073)
    assert m.parse_taifex_fx(copy.deepcopy(FX_ROWS[:1])) == m.FxRow(date(2026, 10, 14), 32.073, None, None)


def test_fx_errors():
    assert kind_of(m.parse_taifex_fx, copy.deepcopy(FX_ROWS) + [{"Date": "20261016", "USD/NTD": "32.4"}]) == "conflict"
    assert kind_of(m.parse_taifex_fx, [{"Date": "20261016", "RMB/NTD": "4.8"}]) == "missing_field"
    assert kind_of(m.parse_taifex_fx, []) == "empty"
    assert kind_of(m.parse_taifex_fx, [{"Date": "2026-10-16", "USD/NTD": "32.3"}]) == "format"
    assert kind_of(m.parse_taifex_fx, [{"Date": "20261016", "USD/NTD": "-1"}]) == "format"


# ── FinMind ───────────────────────────────────────────────────────────────────────
def fm_row(day: str, close, volume=1000, money=2_000_000, code: str = "2330") -> dict:
    return {"date": day, "stock_id": code, "Trading_Volume": volume, "Trading_money": money, "open": close,
            "max": close, "min": close, "close": close, "spread": 0.0, "Trading_turnover": 10}


def finmind(rows, msg: str = "success", status=200) -> dict:
    return {"msg": msg, "status": status, "data": rows}


def test_finmind_sorted_and_identical_duplicates_kept_once():
    rows = [fm_row("2026-10-16", 2420.0, 43046982, 104583063462), fm_row("2026-09-02", 2385.0),
            fm_row("2026-09-03", 2390.0), fm_row("2026-09-03", 2390.0)]
    bars = m.parse_finmind_prices(finmind(rows), "2330")
    assert [bar.date for bar in bars] == [date(2026, 9, 2), date(2026, 9, 3), T]
    assert bars[-1] == m.TwBar(T, 2420.0, 43046982, 104583063462)


def test_finmind_conflicting_duplicate_rejects_whole_payload():
    rows = [fm_row("2026-09-03", 2390.0), fm_row("2026-09-03", 2395.0)]
    assert kind_of(m.parse_finmind_prices, finmind(rows), "2330") == "conflict"
    rows = [fm_row("2026-09-03", 2390.0, volume=1), fm_row("2026-09-03", 2390.0, volume=2)]
    assert kind_of(m.parse_finmind_prices, finmind(rows), "2330") == "conflict"


@pytest.mark.parametrize(
    "payload",
    [finmind([], msg="Requests reach the upper limit"), finmind([], status=402), finmind([], status=True), {"data": []}],
    ids=["msg", "status_402", "status_bool", "no_msg"],
)
def test_finmind_source_failure_raises_status_error(payload):
    with pytest.raises(m.FinMindStatusError) as info:
        m.parse_finmind_prices(payload, "2330")
    error = info.value
    assert error.kind == "status" and isinstance(error, ValueError) and not isinstance(error, m.ParseError)
    assert pickle.loads(pickle.dumps(error)).kind == "status"  # 子程序之間傳遞 kind 不會掉


def test_finmind_empty_and_format():
    assert kind_of(m.parse_finmind_prices, finmind([]), "2330") == "empty"
    assert kind_of(m.parse_finmind_prices, finmind([fm_row("2026-09-03", 1.0, code="2317")]), "2330") == "format"
    assert kind_of(m.parse_finmind_prices, finmind([fm_row("2026-09-03", "2390")]), "2330") == "format"
    assert kind_of(m.parse_finmind_prices, finmind([fm_row("2026-09-03", math.nan)]), "2330") == "format"
    assert kind_of(m.parse_finmind_prices, finmind([fm_row("2026-09-03", 1.0, volume=True)]), "2330") == "format"
    assert kind_of(m.parse_finmind_prices, finmind([fm_row("20260903", 1.0)]), "2330") == "format"
    assert kind_of(m.parse_finmind_prices, finmind({"x": 1}), "2330") == "format"


def test_finmind_zero_close_rows_skipped():
    rows = [fm_row("2026-09-02", 0.0, volume=0), fm_row("2026-09-03", 2390.0)]
    assert [bar.date for bar in m.parse_finmind_prices(finmind(rows), "2330")] == [date(2026, 9, 3)]
    assert kind_of(m.parse_finmind_prices, finmind([fm_row("2026-09-02", 0.0, volume=0)]), "2330") == "format"


def test_finmind_revised_dates():
    bars = [m.TwBar(date(2026, 9, 2), 2385.0, 100, 1), m.TwBar(date(2026, 9, 3), 2390.0, 200, 1),
            m.TwBar(date(2026, 9, 4), 2410.0, 300, 1), m.TwBar(date(2026, 9, 7), 2400.0, 400, 1)]
    stored = {date(2026, 9, 2): (2385.0, 100), date(2026, 9, 3): (2390.0, 201), date(2026, 9, 4): (2411.0, 300)}
    assert m.finmind_revised_dates(stored, bars) == [date(2026, 9, 3), date(2026, 9, 4)]
    assert m.finmind_revised_dates({}, bars) == []


# ── MoneyDJ 0050 ───────────────────────────────────────────────────────────────────
MONEYDJ_HTML = """<html><head><meta charset="utf-8"></head><body>
<div class="eTitle">元大台灣50(0050.TW)-全部持股
<div id="sdate3" align="right">資料日期：2026/10/16</div></div>
<table class="datalist" id="stable3"><thead><tr>
<th scope="col">個股名稱</th><th scope="col">投資比例(%)</th><th scope="col">持有股數 / 口數</th>
</tr></thead><tbody>
<tr class="even"><td class="col05"><a href='/etf/ea/ETZCW.djhtm?c=1&etfid=2330.TW&back=0050.TW'>台積電(2330.TW)</a></td><td class="col06">56.66</td><td class="col07">579,691,139</td></tr>
<tr class="odd"><td class="col05"><a href='/etf/ea/ETZCW.djhtm?c=1&etfid=2454.TW&back=0050.TW'>聯發科(2454.TW)</a></td><td class="col06">5.99</td><td class="col07">34,893,476</td></tr>
<tr class="even"><td class="col05"><a href='/etf/ea/ETZCW.djhtm?c=1&etfid=FITXN*1.TF&back=0050.TW'>臺股期貨 202610(FITXN*1.TF)</a></td><td class="col06">0.21</td><td class="col07">548</td></tr>
<tr class="odd"><td class="col05">台灣50ETF股票期貨 202610</td><td class="col06">0.03</td><td class="col07">738</td></tr>
<tr class="even"><td class="col05">某外國公司</td><td class="col06">0.50</td><td class="col07">1,000</td></tr>
</tbody></table></body></html>"""


def moneydj_page(extra_stocks: int) -> str:
    """MONEYDJ_HTML 再加幾檔（0050 的共用解析預設至少要 40 檔）。"""
    rows = "".join(
        f"<tr><td class='col05'><a href='/etf/ea/ETZCW.djhtm?c=1&etfid={1101 + i}.TW&back=0050.TW'>股{i}({1101 + i}.TW)</a>"
        f"</td><td class='col06'>0.50</td><td class='col07'>1,000</td></tr>"
        for i in range(extra_stocks)
    )
    return MONEYDJ_HTML.replace("</tbody>", rows + "</tbody>")


def test_moneydj_0050_uses_shared_parser():
    holdings = m.parse_moneydj_0050(MONEYDJ_HTML, min_rows=1)
    assert holdings.as_of == T
    assert [(h.symbol, h.name, h.weight) for h in holdings.rows] == [("2330", "台積電", 56.66), ("2454", "聯發科", 5.99)]
    assert holdings.dropped_count == 2  # 臺股期貨、股票期貨
    assert holdings.unmapped_weight == 0.5
    full = m.parse_moneydj_0050(moneydj_page(45))
    assert len(full.rows) == 47 and full.rows[-1].symbol == "1145"


def test_moneydj_0050_errors():
    assert kind_of(m.parse_moneydj_0050, MONEYDJ_HTML) == "too_few"  # 0050 預設至少 40 檔
    assert kind_of(m.parse_moneydj_0050, MONEYDJ_HTML.replace("資料日期", "日期"), min_rows=1) == "date"
    assert kind_of(m.parse_moneydj_0050, MONEYDJ_HTML.replace("投資比例(%)", "比例"), min_rows=1) == "format"
    assert kind_of(m.parse_moneydj_0050, MONEYDJ_HTML.replace(">56.66<", ">156.66<"), min_rows=1) == "format"
    assert kind_of(m.parse_moneydj_0050, moneydj_page(45).replace(">0.50<", ">2.50<"), min_rows=1) == "format"


# ── 抓取（假 http） ─────────────────────────────────────────────────────────────────
class FakeHttp:
    def __init__(self, body):
        self.body = body
        self.calls: list[tuple[str, dict]] = []

    def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.body, Exception):
            raise self.body
        return SimpleNamespace(status=200, body=self.body, headers={}, url=url)


LEDGER = object()


def json_bytes(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def only_call(fake: FakeHttp):
    assert len(fake.calls) == 1
    url, kwargs = fake.calls[0]
    parts = urlsplit(url)
    return parts, {k: v[0] for k, v in parse_qs(parts.query).items()}, kwargs


def test_fetch_twse_allbut0999_url_budget_and_parse():
    fake = FakeHttp(json_bytes(allbut()))
    day = m.fetch_twse_allbut0999(T, http=fake, ledger=LEDGER)
    assert day.turnover_twd == 993_814_830_090
    parts, query, kwargs = only_call(fake)
    assert (parts.scheme, parts.hostname, parts.path) == ("https", "www.twse.com.tw", "/rwd/zh/afterTrading/MI_INDEX")
    assert query == {"date": "20261016", "type": "ALLBUT0999", "response": "json"}
    endpoint = m.ENDPOINTS["twse_allbut0999"]
    assert kwargs == {"max_bytes": endpoint.max_bytes, "timeout": endpoint.timeout_s, "accept": ("json",), "ledger": LEDGER}


def test_fetch_twse_other_endpoints():
    fake = FakeHttp(json_bytes(bfi()))
    assert m.fetch_twse_bfi82u(T, http=fake).foreign == -46_147_162_837
    parts, query, _ = only_call(fake)
    assert parts.path == "/rwd/zh/fund/BFI82U" and query == {"type": "day", "dayDate": "20261016", "response": "json"}

    fake = FakeHttp(json_bytes(MARGIN))
    assert m.fetch_twse_margin_ms(T, http=fake).short_units == 233_602
    parts, query, _ = only_call(fake)
    assert parts.path == "/rwd/zh/marginTrading/MI_MARGN" and query == {"date": "20261016", "selectType": "MS", "response": "json"}

    fake = FakeHttp(json_bytes(twt49u(copy.deepcopy(EX_ROWS))))
    assert len(m.fetch_twse_twt49u(date(2026, 6, 1), T, http=fake)) == 5
    parts, query, _ = only_call(fake)
    assert parts.path == "/rwd/zh/exRight/TWT49U"
    assert query == {"startDate": "20260601", "endDate": "20261016", "response": "json"}
    fake = FakeHttp(json_bytes(twt49u(copy.deepcopy(EX_ROWS))))
    assert kind_of(m.fetch_twse_twt49u, date(2026, 6, 2), T, http=fake) == "date"  # 回應區間和請求不同

    fake = FakeHttp(json_bytes(holidays()))
    assert len(m.fetch_twse_holidays(2026, http=fake)) == 27
    parts, query, _ = only_call(fake)
    assert parts.path == "/rwd/zh/holidaySchedule/holidaySchedule" and query == {"date": "20260101", "response": "json"}


def test_fetch_twse_expect_date_passed_through():
    fake = FakeHttp(json_bytes(bfi()))
    assert kind_of(m.fetch_twse_bfi82u, date(2026, 10, 15), http=fake) == "date"
    fake = FakeHttp(json_bytes(MARGIN))
    assert kind_of(m.fetch_twse_margin_ms, date(2026, 10, 15), http=fake) == "date"


def test_fetch_invalid_arguments_never_call_http():
    fake = FakeHttp(b"{}")
    with pytest.raises(ValueError):
        m.fetch_twse_twt49u(T, date(2026, 6, 1), http=fake)
    with pytest.raises(ValueError):
        m.fetch_twse_holidays(True, http=fake)
    for code in ("2330&x=1", "../etc", "2330 ", "２３３０", "", "taiex"):
        with pytest.raises(ValueError):
            m.fetch_finmind_prices(code, date(2026, 9, 1), T, http=fake)
    with pytest.raises(ValueError):
        m.fetch_finmind_prices("2330", T, date(2026, 9, 1), http=fake)
    assert fake.calls == []


def test_fetch_openapi():
    fake = FakeHttp(json_bytes(PE_ROWS))
    day, table_pe = m.fetch_openapi_bwibbu(http=fake)
    assert day == date(2026, 10, 8) and table_pe["2330"] == 29.56
    parts, _, kwargs = only_call(fake)
    assert (parts.hostname, parts.path) == ("openapi.twse.com.tw", "/v1/exchangeReport/BWIBBU_ALL")
    assert kwargs["accept"] == ("json",)
    fake = FakeHttp(json_bytes([{"資料年月": "11509", "公司代號": "2330", "營業收入-去年同月增減(%)": "30.5"}]))
    assert m.fetch_openapi_revenue(http=fake) == {"2330": m.Revenue(2026, 9, 30.5)}
    assert only_call(fake)[0].path == "/v1/opendata/t187ap05_L"


def test_fetch_taifex_endpoints():
    fake = FakeHttp(json_bytes(FUT_ROWS))
    assert m.fetch_taifex_futures_inst(http=fake).foreign["TX"].oi_net == -97582
    parts, _, _ = only_call(fake)
    assert (parts.hostname, parts.path) == (
        "openapi.taifex.com.tw", "/v1/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate")

    fake = FakeHttp(json_bytes(CP_ROWS))
    assert m.fetch_taifex_calls_puts_inst(http=fake).foreign_put.oi_net == 5820
    assert only_call(fake)[0].path == "/v1/MarketDataOfMajorInstitutionalTradersDetailsOfCallsAndPutsBytheDate"

    fake = FakeHttp(pcr_text().encode("utf-8"))  # 帶 BOM 的 CSV
    assert m.fetch_taifex_pcr(http=fake)[-1].oi_ratio_pct == 60.84
    parts, _, kwargs = only_call(fake)
    assert parts.path == "/v1/PutCallRatio" and kwargs["accept"] == ("octet-stream", "csv")

    fake = FakeHttp(json_bytes(OPT_ROWS))
    assert len(m.fetch_taifex_daily_options(http=fake)) == 4
    parts, _, kwargs = only_call(fake)
    assert parts.path == "/v1/DailyMarketReportOpt"
    assert kwargs["timeout"] == 30.0 and kwargs["max_bytes"] == m.ENDPOINTS["taifex_daily_options"].max_bytes

    fake = FakeHttp(json_bytes(FX_ROWS))
    assert m.fetch_taifex_fx(http=fake).usd_ntd == 32.338
    assert only_call(fake)[0].path == "/v1/DailyForeignExchangeRates"


class ContentTypeHttp(FakeHttp):
    """像 market_http.fetch 一樣比 Content-Type：accept 裡沒有任何子字串在回應型別裡就丟 content_type。"""

    def __init__(self, body, content_type: str):
        super().__init__(body)
        self.content_type = content_type

    def fetch(self, url, **kwargs):
        if not any(token in self.content_type for token in kwargs["accept"]):
            raise market_http.FetchError("content_type")
        return super().fetch(url, **kwargs)


def test_taifex_fetch_parses_octet_stream_responses():
    octet = "application/octet-stream"  # 期交所 OpenAPI 實測每個端點都是這個（內容是 JSON／CSV）
    cases = [
        (m.fetch_taifex_futures_inst, json_bytes(FUT_ROWS), lambda r: r.foreign["TX"].oi_net == -97582),
        (m.fetch_taifex_calls_puts_inst, json_bytes(CP_ROWS), lambda r: r.foreign_call.oi_net == -169),
        (m.fetch_taifex_daily_options, b"\xef\xbb\xbf" + json_bytes(OPT_ROWS), lambda r: len(r) == 4),
        (m.fetch_taifex_fx, json_bytes(FX_ROWS), lambda r: r.usd_ntd == 32.338),
        (m.fetch_taifex_pcr, pcr_text().encode("utf-8"), lambda r: r[-1].oi_ratio_pct == 60.84),
    ]
    for fetch, body, check in cases:
        assert check(fetch(http=ContentTypeHttp(body, octet)))
    with pytest.raises(market_http.FetchError):
        m.fetch_taifex_fx(http=ContentTypeHttp(json_bytes(FX_ROWS), "text/html; charset=utf-8"))
    for name, endpoint in m.ENDPOINTS.items():
        if endpoint.host == "openapi.taifex.com.tw":
            assert "octet-stream" in endpoint.accept, name
    assert m.ENDPOINTS["taifex_daily_options"].max_bytes == 4 * 1024 * 1024


# 2026-10-23 實測：期交所的 CDN 同一個網址有時回 CSV（UTF-8 BOM、中文表頭、CRLF）。DailyMarketReportOpt 的表頭
# 照 10/23 抓到的整份 CSV 原樣抄：18 欄，順序和 JSON 的 18 個英文鍵一一對應
OPT_CSV_HEADER = (
    "日期,契約,到期月份(週別),履約價,買賣權,開盤價,最高價,最低價,最後成交價,成交量,結算價,未沖銷契約量,"
    "最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段"
)
OPT_JSON_KEYS = (
    "Date", "Contract", "ContractMonth(Week)", "StrikePrice", "CallPut", "Open", "High", "Low", "Close", "Volume",
    "SettlementPrice", "OpenInterest", "BestBid", "BestAsk", "HistoricalHigh", "HistoricalLow", "TradingHalt",
    "TradingSession",
)
# 第一列每一欄的值都不一樣：欄位對錯位置（結算價和未平倉對調、整排位移一格）結果一定不同
OPT_CSV_ROWS = [
    ("20261022", "TXO", "202611", "46000", "買權", "12", "15", "10", "13", "57", "1234", "10", "11", "14", "400", "9", "", "一般"),
    ("20261022", "TXO", "202611", "46000", "賣權", "-", "-", "-", "-", "0", "1100", "12", "-", "-", "-", "-", "", "一般"),
    ("20261022", "TXO", "202611", "46000", "買權", "-", "-", "-", "-", "0", "-", "-", "-", "-", "-", "-", "", "盤後"),  # 盤後不收
    ("20261022", "TXO", "202610F5", "41600", "買權", "-", "-", "-", "-", "0", "4540", "0", "-", "-", "-", "-", "", "一般"),  # 週五週選
    ("20261022", "TXO", "202610W4", "41600", "賣權", "28.5", "53", "28.5", "51", "72", "51", "69", "51", "54", "53", "28.5", "",
     "一般"),  # 週選
    ("20261022", "TEO", "202611", "1500", "買權", "-", "-", "-", "-", "0", "80", "5", "-", "-", "-", "-", "", "一般"),  # 別的契約
    ("20261022", "TXO", "202612", "47000", "賣權", "-", "-", "-", "-", "0", "-", "-", "-", "-", "-", "-", "", "一般"),  # 「-」＝沒有值
]


def opt_csv(header: str = OPT_CSV_HEADER, rows=OPT_CSV_ROWS, newline: str = "\r\n") -> bytes:
    return ("\ufeff" + newline.join([header, *(",".join(row) for row in rows)]) + newline).encode("utf-8")


def format_detail(fetch, body: bytes) -> tuple[str, str]:
    with pytest.raises(m.ParseError) as info:
        fetch(http=FakeHttp(body))
    return info.value.kind, info.value.detail


def test_daily_options_csv_body_gives_the_same_rows_as_json():
    """2026-10-23：CDN 給 CSV 時照表頭位置換回英文鍵，結果要和同樣資料的 JSON 一模一樣（以前整個端點 format）。"""
    rows = m.fetch_taifex_daily_options(http=FakeHttp(opt_csv()))
    assert [(r.month, r.strike, r.kind, r.oi, r.settle, r.date) for r in rows] == [
        ("202611", 46000.0, "C", 10, 1234.0, date(2026, 10, 22)),
        ("202611", 46000.0, "P", 12, 1100.0, date(2026, 10, 22)),
        ("202612", 47000.0, "P", None, None, date(2026, 10, 22)),
    ]
    as_json = json_bytes([dict(zip(OPT_JSON_KEYS, row)) for row in OPT_CSV_ROWS])
    assert rows == m.fetch_taifex_daily_options(http=FakeHttp(as_json))
    assert m.fetch_taifex_daily_options(http=FakeHttp(opt_csv(newline="\n"))) == rows
    teo = m.fetch_taifex_daily_options("TEO", http=FakeHttp(opt_csv()))
    assert [(r.month, r.strike, r.kind, r.oi, r.settle) for r in teo] == [("202611", 1500.0, "C", 5, 80.0)]
    assert kind_of(m.fetch_taifex_daily_options, http=FakeHttp(opt_csv(rows=[]))) == "empty"  # 只有表頭＝和 [] 一樣


def test_daily_options_csv_errors_are_format_with_a_detail():
    """表頭要一字不差、順序相同；截斷、HTML 頁、壞掉的 JSON 都是 format，detail 說是哪一種（log 才看得出來）。"""
    fetch = m.fetch_taifex_daily_options
    names = OPT_CSV_HEADER.split(",")
    swapped = ",".join([*names[:10], names[11], names[10], *names[12:]])  # 結算價和未沖銷契約量對調
    assert format_detail(fetch, opt_csv(swapped)) == ("format", "header")
    assert format_detail(fetch, opt_csv(OPT_CSV_HEADER + ",備註")) == ("format", "header")
    assert format_detail(fetch, opt_csv(OPT_CSV_HEADER.replace("結算價", "結算價格"))) == ("format", "header")
    body = opt_csv()
    assert format_detail(fetch, body[: body.rindex(b",47000,") + 4]) == ("format", "columns")  # 截斷在最後一列中間
    assert format_detail(fetch, opt_csv(rows=[*OPT_CSV_ROWS, OPT_CSV_ROWS[0] + ("x",)])) == ("format", "columns")
    # HTML 頁不是 JSON，當 CSV 讀時第一行不是實測的表頭
    assert format_detail(fetch, b"<!DOCTYPE html><html><body>busy</body></html>") == ("format", "html")
    assert format_detail(fetch, b'\xef\xbb\xbf[{"Date": "20261022", "Contract"') == ("format", "json")  # 截斷的 JSON
    assert format_detail(fetch, opt_csv().replace("盤後".encode("utf-8"), b"\xff\xfe")) == ("format", "csv")  # 不是 UTF-8


def test_taifex_csv_without_a_verified_header_is_format_csv():
    """2026-10-23：外資台指期、外資選擇權、匯率的 CSV 表頭還沒有真樣本，不猜對應：CSV 一律 format／csv；JSON 照舊。"""
    body = "\ufeff日期,契約名稱,身份別\r\n20261022,臺股期貨,外資及陸資\r\n".encode("utf-8")
    for fetch in (m.fetch_taifex_futures_inst, m.fetch_taifex_calls_puts_inst, m.fetch_taifex_fx):
        assert format_detail(fetch, body) == ("format", "csv"), fetch.__name__
    assert m.fetch_taifex_futures_inst(http=FakeHttp(b"\xef\xbb\xbf" + json_bytes(FUT_ROWS))).foreign["TX"].oi_net == -97582
    assert format_detail(m.fetch_taifex_futures_inst, b'[{"Date": "20261022"') == ("format", "json")
    # 只收 JSON 的證交所端點：回 HTML 是 json
    assert format_detail(lambda http: m.fetch_twse_bfi82u(T, http=http), b"<html>busy</html>") == ("format", "json")


def test_fetch_finmind_url_and_bootstrap_timeout():
    rows = [fm_row("2026-10-16", 2420.0)]
    fake = FakeHttp(json_bytes(finmind(rows)))
    assert m.fetch_finmind_prices("2330", date(2026, 10, 1), T, http=fake, ledger=LEDGER)[0].close == 2420.0
    parts, query, kwargs = only_call(fake)
    assert (parts.hostname, parts.path) == ("api.finmindtrade.com", "/api/v4/data")
    assert query == {"dataset": "TaiwanStockPrice", "data_id": "2330", "start_date": "2026-10-01", "end_date": "2026-10-16"}
    assert kwargs["timeout"] == 15.0 and kwargs["ledger"] is LEDGER and kwargs["accept"] == ("json",)
    fake = FakeHttp(json_bytes(finmind([fm_row("2026-10-16", 47111.26, code="TAIEX")])))
    m.fetch_finmind_prices("TAIEX", date(1999, 1, 1), T, http=fake)
    assert only_call(fake)[2]["timeout"] == m.FINMIND_BOOTSTRAP_TIMEOUT_S == 30.0


def test_fetch_moneydj_0050():
    fake = FakeHttp(moneydj_page(45).encode("utf-8"))
    assert len(m.fetch_moneydj_0050(http=fake, ledger=LEDGER).rows) == 47
    parts, query, kwargs = only_call(fake)
    assert (parts.hostname, parts.path, query) == ("www.moneydj.com", "/ETF/X/Basic/Basic0007B.xdjhtm", {"etfid": "0050.TW"})
    assert kwargs["accept"] == ("html",) and kwargs["ledger"] is LEDGER
    assert kind_of(m.fetch_moneydj_0050, http=FakeHttp("台積電".encode("big5"))) == "format"


def test_fetch_body_decoding():
    assert kind_of(m.fetch_twse_bfi82u, T, http=FakeHttp(b"<html>busy</html>")) == "format"
    assert kind_of(m.fetch_twse_bfi82u, T, http=FakeHttp(b"\xff\xfe")) == "format"
    assert m.fetch_twse_bfi82u(T, http=FakeHttp(b"\xef\xbb\xbf" + json_bytes(bfi()))).trust == 11_047_506_970


def test_fetch_error_propagates_unwrapped():
    fake = FakeHttp(market_http.FetchError("rate_limited"))
    with pytest.raises(market_http.FetchError) as info:
        m.fetch_taifex_pcr(http=fake)
    assert info.value.kind == "rate_limited"


# ── 預算表與 import ─────────────────────────────────────────────────────────────────
def test_endpoint_budget_table():
    twse = [e for e in m.ENDPOINTS.values() if e.host == "www.twse.com.tw"]
    # 2026-10-23：TWT49U 1→2 之後各端點上限加起來 13（第 2 次只在凌晨補過昨天、或 16:00 那次重試時用到）；
    # 證交所 www 每天合計仍由主機帳本擋在 12，超過的那次是 budget（沒送出、不算失敗）
    assert sum(e.daily_max for e in twse) == 13 and m.HOST_DAILY_MAX["www.twse.com.tw"] == 12
    assert m.ENDPOINTS["twse_allbut0999"].earliest_tpe == time(14, 30)
    assert m.ENDPOINTS["twse_bfi82u"].earliest_tpe == time(15, 30)
    assert m.ENDPOINTS["twse_twt49u"].earliest_tpe == time(16, 0)
    assert m.ENDPOINTS["twse_margin_ms"].earliest_tpe == time(21, 30)
    assert (m.ENDPOINTS["twse_allbut0999"].daily_max, m.ENDPOINTS["twse_bfi82u"].daily_max) == (4, 3)
    assert (m.ENDPOINTS["twse_twt49u"].daily_max, m.ENDPOINTS["twse_margin_ms"].daily_max) == (2, 3)
    assert m.HOST_MIN_INTERVAL_S["www.twse.com.tw"] >= 10 and m.HOST_MIN_INTERVAL_S["api.finmindtrade.com"] >= 3
    assert "octet-stream" in m.ENDPOINTS["taifex_pcr"].accept
    for endpoint in m.ENDPOINTS.values():
        assert endpoint.host in market_http.ALLOWED_HOSTS
        assert endpoint.max_bytes > 0 and 0 < endpoint.timeout_s <= 30
    for base in (m.TWSE_WWW, m.TWSE_OPENAPI, m.TAIFEX_OPENAPI, m.FINMIND_DATA):
        assert urlsplit(base).scheme == "https" and urlsplit(base).hostname in market_http.ALLOWED_HOSTS


def test_the_taifex_comment_and_the_fx_cap_both_say_five_a_day():
    """2026-10-24（審查 r9 seat1 N3）：ENDPOINTS 上面的註解還寫「匯率仍每天 2 次：07、09 點」，程式（taifex_fx 5 次）和同一段
    後面的說明、market_pipeline_tw.taifex_due 的說明早就是 5 次。只改註解，次數不動。"""
    assert m.ENDPOINTS["taifex_fx"].daily_max == 5 and m.ENDPOINTS["taifex_daily_options"].daily_max == 9
    source = (Path(__file__).resolve().parent / "market_sources_tw.py").read_text(encoding="utf-8")
    assert "匯率仍每天 2 次" not in source
    assert "匯率每天 5 次：07、09…15 點" in source


def test_import_has_no_side_effects():
    code = (
        "import sys, market_sources_tw; "
        "print(any(name in sys.modules for name in ('market_http', 'requests', 'safe_fetch', 'market_sources_us')))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


def test_typhoon_closure_wordings_are_closed_days():
    """2026-10-18 審查 r6c Q1：颱風臨時停市的寫法也要認成休市；同時寫到開始交易的仍是 unknown（保守）。"""
    assert m.classify_twse_holiday("颱風停止上班", "因颱風來襲，停止交易") == m.HOLIDAY_CLOSED
    assert m.classify_twse_holiday("天然災害停止上班", "") == m.HOLIDAY_CLOSED
    assert m.classify_twse_holiday("颱風假後開始交易", "") == m.HOLIDAY_UNKNOWN
