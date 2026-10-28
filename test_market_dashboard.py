"""/盤勢 卡片（market_dashboard）的測試：快照全是合成的（不連網、不放家人資料）、時間一律注入。

量法和 main／card_pages 一致：卡片大小用 json.dumps(card, ensure_ascii=False) 的 UTF-8 位元組；
卡上每個字都要原樣通過 output_validator；送卡前的真閘門 main._validated_flex_card_message 也跑一次。
合成快照的每一列都要通過 market_calc.row_ok（和 job 寫快照用的是同一份 ROW_IDS 欄位表）。
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import importlib.util
import itertools
import json
import os
import re
import sys
import types
import unicodedata
import uuid
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import card_pages
import market_calc
import market_paths
import output_validator

TPE = ZoneInfo("Asia/Taipei")
ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent

NOW = datetime(2026, 10, 17, 16, 30, tzinfo=TPE).timestamp()
NOW_2027 = datetime(2027, 1, 15, 12, 0, tzinfo=TPE).timestamp()
NOW_2028 = datetime(2028, 1, 3, 12, 0, tzinfo=TPE).timestamp()
ALL_NOWS = (NOW, NOW_2027, NOW_2028)

PALETTE = {"#1E5AA8", "#B7791F", "#555555", "#666666"}
NODE_TYPES = {"carousel", "bubble", "box", "text", "separator", "filler"}
FORBIDDEN_KEYS = {"uri", "url", "data", "altUri"}
SUMMARY_JARGON = ("GEX", "PCR", "IV30", "最大痛點", "CFTC", "gamma", "Gamma", "max pain")
# 期權與資金摘要卡的牆那兩列：用代號開頭（QQQ、SOXX），所以不受「先中文」的規則限制；別的列照舊
WALL_ROW_RE = re.compile(r"(?:QQQ|SOXX) (?:買權牆|賣權牆).*")
DISCLAIMER = "程式用公開資料算的，只描述狀態，不是買賣建議"
PLAN_FORBIDDEN = ("建議", "進場", "出場", "停損", "抄底", "逢低", "布局", "可以買", "該賣")
# card_pages 接受的最長固定網域（253 字主機名＋port）＋ /card/<22 字 token>；with_page_button 再加「#bN」
LONG_HOST = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])
LONG_PAGE_URL = f"https://{LONG_HOST}:65535/card/{'A' * 22}"

FLEX_BODIES = ("", " VOO", " QQQ", " SOXX", " 0050", " 期權", " 新聞", " 季節性", " 選舉",
               " 13F QQQ", " 13F VOO", " 13F SOXX")
CHART_BODIES = (" QQQ 圖", " SPY 圖", " SOXX 圖", " 台指 圖")
CHART_KEY = {" QQQ 圖": "QQQ", " SPY 圖": "SPY", " SOXX 圖": "SOXX", " 台指 圖": "TXO"}

# 快照裡的外部字串與 job 的固定句（測試用來分出「bot 自己的句子」）
GOOD_TITLES = ("美股收高 科技股領漲", "台股站穩月線 電子權值股撐盤", "伊朗局勢 油價波動加大")
NEWS_SOURCES = ("經濟日報", "中央社", "鉅亨網")
SEMI_TITLE, SEMI_SOURCE = "費半指數回落 晶片股分歧", "工商時報"  # 2026-10-24 第 9 輪 A17：半導體新聞
TRUMP_TEXT = "We will put new tariffs on imported chips, and Iran must stop now."
HOLDING_NAMES = ("輝達", "蘋果", "微軟", "亞馬遜", "Alphabet", "博通", "Meta", "特斯拉", "好市多", "Netflix")
HOLDING_SYMBOLS = (["NVDA"], ["AAPL"], ["MSFT"], ["AMZN"], ["GOOGL", "GOOG"], ["AVGO"], ["META"], ["TSLA"],
                   ["COST"], ["NFLX"])
UNIVERSE = "依 S&P 500 權重（SPY 每日持股 10/15）"
COVERAGE = "有效 500/503 檔"
PERIOD_NOTE = "彙總期間來源未標；前十大持有人申報期間以 6/30 為主（混合）"
ONELINER_US = "標普收在月線上、成分股站上月線 62%、量偏少"
ONELINER_TW = "台股收在月線上、外資賣超 123 億、量偏少"
TOPICS = ("關稅", "伊朗")
EXTERNAL = (*GOOD_TITLES, SEMI_TITLE, *NEWS_SOURCES, SEMI_SOURCE, TRUMP_TEXT, *HOLDING_NAMES, UNIVERSE, COVERAGE, PERIOD_NOTE,
            ONELINER_US, ONELINER_TW, *TOPICS, "Consumer Price Index")
# PLAN §19.1 的丟棄案例：第一則被 validator 擋；其他是 job 洗過的快照裡不該出現的字（代表沒洗或被改過）
DROP_TITLES = ("最新公布 9月出口 年增30%", "x @all y", "@媽媽", "股市\u202e大漲", "零寬\u200b標題", "孤立\ud800代理")


@pytest.fixture
def md(monkeypatch):
    """每個測試拿一份新載入的 market_dashboard（快取也是新的）。"""
    monkeypatch.delitem(sys.modules, "market_dashboard", raising=False)
    module = importlib.import_module("market_dashboard")
    module.clear_cache()
    return module


@pytest.fixture
def paths(tmp_path):
    built = market_paths.Paths.under(tmp_path)
    os.mkdir(built.state_dir, 0o700)
    os.mkdir(built.chart_dir, 0o700)
    return built


# ── 合成快照（每一列照 market_calc.ROW_IDS） ───────────────────────────────────
def _row(values, now, day, *, state="ok", stale_in=86400.0):
    return {"label": "", "values": values, "data_date": day.isoformat() if day else None, "period": None,
            "source": "合成", "basis": "", "stale_after": now + stale_in, "state": state}


def _never():
    return {"label": "", "values": {}, "data_date": None, "period": None, "source": "", "basis": "",
            "stale_after": 0.0, "state": "never"}


def _etf(close, chg, above):
    return {"close": close, "chg_pct": chg, "ma_dist": {"20": 1.2, "60": 3.4, "120": 5.0, "240": 8.9},
            "above_ma20": above, "dd240_pct": -3.2, "rsi14": 55.4, "vol_ratio": 0.78, "vol_pct250": 40.0,
            "vol_label": "量縮", "pv_label": "價漲量縮", "vol_flag": None, "vol_note": None}


def _breadth(total, valid, above, universe):
    status = "ok" if valid >= 0.8 * total else "low_coverage"
    per = {n: {"above": above, "below": valid - above, "equal": 0, "new_below": 12, "new_above": 8, "valid": valid,
               "total": total, "prev_pct": 58.0, "prev_valid": valid, "same_set_pct": 61.0, "status": status}
           for n in ("20", "60", "120", "240")}
    return {"per_n": per, "adv": 300, "dec": 190, "flat": 10, "new_high": 5, "new_low": 2,
            "universe_label": universe, "coverage_note": COVERAGE}


def _options(spot, *, basis="close"):
    """basis＝牆距的基準（2026-10-24 第 9 輪 I1）：close＝期權鏈自己的收盤（比現價低 1%）、spot＝現價（舊快照沒有這兩個欄位）。"""
    return {"spot": spot, "call_wall": float(round(spot * 1.03)), "put_wall": float(round(spot * 0.96)),
            "call_wall_dist_pct": 3.0, "put_wall_dist_pct": -4.0, "wall_status": "ok",
            "ref_close": round(spot * 0.99, 2) if basis == "close" else None, "dist_basis": basis,
            "monthly": {"expiry": "2026-11-20", "call_wall": float(round(spot * 1.05)),
                        "put_wall": float(round(spot * 0.94)), "max_pain": float(round(spot * 0.99)), "status": "ok"},
            "pcr_oi": 1.25, "pcr_vol": 0.9, "iv30": 18.5,
            "gex": {"status": "ok", "at_spot": -1.2e9, "sign": -1, "near_zero": False, "coverage": 0.92,
                    "lower_bound": False, "rate_assumed": False,
                    "crossings": [{"price": float(round(spot * 0.99)), "direction": "down"},
                                  {"price": float(round(spot * 1.04)), "direction": "up"}]},
            "counts": {"raw": 10954, "bad": 3, "conflict": 2, "oi_valid": 9000, "gamma_valid": 8900, "iv_replaced": 22},
            "oi_date_note": market_calc.TEXT_OI_DATE}


def _holdings():
    rows = [{"name": name, "symbols": list(symbols), "weight": 9.0 - i * 0.5, "increased": 1200 - i * 40,
             "decreased": 1100, "new": 50, "soldout": 30, "own_pct": 65.0}
            for i, (name, symbols) in enumerate(zip(HOLDING_NAMES, HOLDING_SYMBOLS))]
    rows[3]["increased"] = None  # 一家暫缺
    return {"rows": rows, "summary": market_calc.inst13f_summary(rows), "holder_period_note": PERIOD_NOTE,
            "weight_basis": UNIVERSE}


def _fund():
    rows = [{"name": name, "symbols": list(symbols), "pe": 30.0 + i, "status": "ok"}
            for i, (name, symbols) in enumerate(zip(HOLDING_NAMES, HOLDING_SYMBOLS))]
    rows[7].update(pe=None, status="nonpositive")
    rows[8].update(pe=None, status="missing")
    return {"rows": rows, "pe_median": 34.5, "pe_valid": 8, "total": 10}


def _infl_measure(yoy, previous, mom, ann3, level, momentum, move, move_pp):
    return {"month": "2026-09-01", "yoy": yoy, "yoy_month": "2026-09-01", "yoy_prev": previous, "mom": mom, "ann3": ann3,
            "sa_month": "2026-09-01", "level": level, "momentum": momentum, "yoy_move": move, "yoy_move_pp": move_pp}


def _stats(up, median, mean, n):
    return {"up_pct": up, "median": median, "mean": mean, "n": n}


def make_snapshot(now: float = NOW) -> dict:
    today = datetime.fromtimestamp(now, TPE).date()
    day = today - timedelta(days=1)
    recent = now - 3600
    midterm = today.year % 4 == 2
    rows = {
        **{f"us.etf.{s}": _row(_etf(*args), now, day) for s, args in
           (("VOO", (612.34, 0.52, True)), ("QQQ", (540.12, -0.31, False)), ("SPY", (666.0, 0.5, True)))},
        "us.etf.SOXX": _row(_etf(245.6, 1.2, True), now, day - timedelta(days=1)),
        "us.breadth.VOO": _row(_breadth(503, 500, 310, UNIVERSE), now, day),
        "us.breadth.QQQ": _row(_breadth(101, 100, 55, UNIVERSE), now, day),
        "us.breadth.SOXX": _row(_breadth(30, 22, 10, UNIVERSE), now, day),  # 有效 <80%：資料不足
        **{f"us.options.{s}": _row(_options(spot), now, day) for s, spot in (("QQQ", 540.0), ("SPY", 666.0), ("SOXX", 245.0))},
        "us.cftc.es": _row({"contract": "S&P 500 Consolidated", "lev_net": -123456, "lev_net_chg": 5000,
                            "am_net": 654321, "am_net_chg": -2000, "oi": 2_000_000}, now, day),
        "us.cftc.nq": _row({"contract": "NASDAQ-100 Consolidated", "lev_net": 23456, "lev_net_chg": -500,
                            "am_net": 54321, "am_net_chg": 200, "oi": 300_000}, now, day),
        "us.margin": _row({"month": "2026-09", "debit_musd": 1480054.0, "yoy_pct": 25.3}, now, date(2026, 9, 30)),
        # 損益兩平（2026-10-24）：bot 這一版還不讀它；合成快照要跟著 ROW_IDS，舊快照（沒有這一列）另外有測試
        "us.infl.be": _row({"y5": {"date": day.isoformat(), "value": 2.45, "chg5_bp": 12, "move": "up"},
                            "y10": {"date": day.isoformat(), "value": 2.32, "chg5_bp": 0, "move": "flat"},
                            "spread_bp": 13, "spread": "five_higher"}, now, day),
        "us.infl.cpi": _row({"cpi": _infl_measure(3.7, 3.4, 0.6, 4.6, "well_above", "accelerating", "up", 0.3),
                             "core": _infl_measure(2.6, 2.4, 0.3, 3.4, "above", "accelerating", "up", 0.2),
                             "aligned": True, "gap": "headline_higher", "gap_pp": 1.1, "retrieved": day.isoformat()},
                            now, date(2026, 9, 30)),
        **{f"us.13f.{s}": _row(_holdings(), now, None, state="unknown_period") for s in ("VOO", "QQQ", "SOXX")},
        **{f"us.fund.{s}": _row(_fund(), now, day) for s in ("VOO", "QQQ", "SOXX")},
        "tw.taiex": _row({"close": 47111.26, "chg": 243.1, "chg_pct": 0.52,
                          "ma_dist": {"20": 1.1, "60": 2.2, "120": 3.3, "240": 9.9}, "above_ma20": True,
                          "dd240_pct": -1.5, "rsi14": 61.7}, now, day),
        "tw.etf.0050": _row(_etf(185.3, 0.4, True), now, day),
        "tw.breadth.0050": _row(_breadth(50, 50, 31, "0050 成分 50 檔"), now, day),
        "tw.ad": _row({"adv": 684, "dec": 300, "flat": 50, "adv_limit": 19, "dec_limit": 2, "no_compare": 5}, now, day),
        "tw.turnover": _row({"value_twd": 523_400_000_000.0, "vol_ratio": 0.78, "vol_pct250": 30.0, "vol_label": "量縮",
                             "vol_flag": None}, now, day),
        "tw.inst3": _row({"foreign_yi": -123.4, "trust_yi": 45.6, "dealer_yi": 10.2, "foreign_5d_yi": -500.0,
                          "trust_5d_yi": 200.0, "dealer_5d_yi": 30.0, "days_5d": 5}, now, day),
        "tw.margin": _row({"balance_yi": 3456.0, "change_yi": 12.3}, now, day),
        "tw.futures": _row({"foreign_net_big": -97582, "foreign_net_big_chg": -1200, "foreign_net_mini": -3928,
                            "foreign_net_micro": -12415}, now, day),
        "tw.options": _row({"month": "202611", "spot": 47111.26, "call_wall": 54000.0, "put_wall": 46000.0,
                            "max_pain": 50000.0, "call_wall_dist_pct": 14.6, "put_wall_dist_pct": -2.4, "status": "ok",
                            "pcr_oi_pct": 60.84, "foreign_call_net": 1234, "foreign_put_net": -567}, now, day),
        "tw.fund.0050": _row({"pe_median": 18.5, "pe_valid": 48, "total": 50, "rev_month": "2026-09", "rev_pos": 31,
                              "rev_valid": 50}, now, day),
        "events.next": _row({"items": [
            {"name": "FOMC", "kind": "fomc", "at": now + 11 * 86400},
            {"name": "NVDA", "kind": "earnings", "at": now + 20 * 86400},
            {"name": "Consumer Price Index", "kind": "econ", "at": now + 3 * 86400},
            {"name": "FOMC", "kind": "fomc", "at": now - 86400},  # 過了的不列
        ]}, now, today),
        "season.now": _row({"month": today.month, "midterm_year": midterm,
                            "sp500": _stats(60.5, 0.9, 1.1, 76), "sp500_q4": _stats(75.0, 3.1, 3.4, 75),
                            "sp500_midterm": _stats(70.0, 1.5, 1.9, 19) if midterm else None,
                            "sp500_midterm_q4": _stats(84.0, 5.0, 5.2, 19) if midterm else None,
                            "taiex": _stats(55.0, 0.5, 0.8, 27), "taiex_q4": _stats(60.0, 2.0, 2.5, 26),
                            "taiex_midterm": _stats(57.0, 0.7, 0.9, 7) if midterm else None,
                            "taiex_pending": False,
                            # 2026-10-24 第 9 輪 A14：SOXX 自身（Nasdaq 近 9 年，期中選舉年只有 2 年→不放）、SOX 長歷史
                            "soxx": _stats(66.7, 2.43, 1.49, 9), "soxx_q4": _stats(77.8, 11.08, 10.23, 9),
                            "soxx_midterm": None, "soxx_midterm_q4": None, "soxx_pending": False,
                            "soxx_range": {"first": "2016-11", "last": "2025-12"},
                            "sox": _stats(62.5, 3.34, 2.53, 32), "sox_q4": _stats(78.1, 9.39, 8.77, 32),
                            "sox_midterm": _stats(87.5, 5.04, 7.07, 8) if midterm else None,
                            "sox_midterm_q4": _stats(87.5, 9.27, 14.71, 8) if midterm else None, "sox_pending": False,
                            "sox_range": {"first": "1994-06", "last": "2025-12"}}, now, today),
        "election": _row({"vote_date": "2026-11-03", "days_left": 17, "state": "upcoming", "blocked": False, "quotes": [
            {"platform": "polymarket", "chamber": "house", "outcome": "Yes", "mid": 0.905, "last": 0.91,
             "updated_utc": now - 1800, "result": None},  # job 一律寫 result（yes／no／None）
            {"platform": "kalshi", "chamber": "house", "outcome": "Yes", "mid": 0.9015, "last": 0.902,
             "updated_utc": None, "result": None},
        ]}, now, today),
        "trump.summary": _row({"topics": {TOPICS[0]: 2, TOPICS[1]: 1}, "latest_utc": recent, "at_least": False}, now, today),
        "trump.posts": _row({"items": [{"excerpt": TRUMP_TEXT, "published_utc": recent, "quoted_author": None,
                                        "topics": list(TOPICS)}]}, now, today),
        **{f"news.{k}": _row({"items": [{"title": t, "source": s, "published_utc": recent}]}, now, today)
           for k, t, s in zip(("us", "tw", "iran", "semi"), (*GOOD_TITLES, SEMI_TITLE), (*NEWS_SOURCES, SEMI_SOURCE))},
        # 川普壓力指數（TACO 指數，MacroMargin；2026-10-24）：保存的真實回應算出來的數字（test_market_taco_publish）
        "macro.TACO": _row({"value": 9.0005, "chg20": -3.6, "percentile": 88, "n": 394, "since": "2025-03-26",
                            "max": 17.3261, "max_date": "2026-03-27", "min": -4.9868, "min_date": "2025-05-20",
                            "status_key": "high", "method": "v1"}, now, day),
        "oneliner.us": _row({"text": ONELINER_US}, now, day),
        "oneliner.tw": _row({"text": ONELINER_TW}, now, day),
        "contra.us": _row({"codes": [], "text": None, "provisional": False}, now, day),
        "contra.tw": _row({"codes": ["foreign_sell_trust_buy"], "text": "外資 5 日賣超、投信 5 日買超"}, now, day),
    }
    macro = {"VIX": 19.99, "VIX3M": 21.5, "UST3M": 4.26, "UST10Y": 5.09, "DXY": 28.92, "WTI": 72.3, "BRENT": 76.1,
             "GOLD": 2650.0, "SILVER": 31.2, "BTC": 81471.0, "USDTWD": 32.338, "RUT": 2250.0, "DJI": 43000.0,
             "IXIC": 19000.0, "MOVE": 98.5, "SPY_DD": -3.2}
    for key, value in macro.items():
        source = "ETF 代理：UUP" if key == "DXY" else "Yahoo"
        # DXY 這格是 UUP 代理：value 是 ETF 的股價 28.92（不是美元指數），chg 是股價的變動，只有 chg_pct 能用
        rows[f"macro.{key}"] = _row({"value": value, "chg": 0.12, "chg_pct": 0.3 if key == "DXY" else None,
                                     "source": source, **({"provisional": False} if key == "SPY_DD" else {})}, now, day)
    # 2026-10-24 第 9 輪（A12）：VXTLT 是獨立的一列（不寫進 MOVE：量綱差 6–7 倍）；B 軌起 bot 讀它（15.09、−4.67% 是研究紀錄的值；沒有保存 _VXTLT.json 的回應檔）
    rows["macro.VXTLT"] = _row({"value": 15.09, "chg": -0.74, "chg_pct": -4.67, "source": "CBOE"}, now, day)
    charts = {}
    for index, key in enumerate(market_calc.CHART_KEYS):
        files = [f"{index:x}" * 32 + ".png"] + ([] if key == "TXO" else [f"{index + 8:x}" * 32 + ".png"])
        charts[key] = {"files": files, "inputs_date": day.isoformat(), "stale_after": now + 86400}
    return {"schema": 1, "generated_at": now - 600, "run_id": "r1", "rows": rows, "charts": charts,
            "progress": {"us_members": [520, 520], "us_last_complete_day": day.isoformat(),
                         "tw_last_complete_day": day.isoformat()}}


def worst_snapshot(now: float = NOW) -> dict:
    """最長的字、最大的數字、比上限多的清單：量大小與每列寬度用。"""
    snap = make_snapshot(now)
    rows = snap["rows"]
    recent = now - 3600
    long_title, long_name, long_note = "測" * 40, "名" * 24, "註" * 60
    for kind in ("us", "tw", "iran", "semi"):
        # 半導體的標題和美股的不同字：同標題會被半導體段略過（不重複列），那就不是最壞的情形
        title = "半" * 40 if kind == "semi" else long_title
        rows[f"news.{kind}"]["values"]["items"] = [
            {"title": title, "source": long_name, "published_utc": recent} for _ in range(12)]
    rows["trump.posts"]["values"]["items"] = [
        {"excerpt": "川" * 100, "published_utc": recent, "quoted_author": long_name, "topics": ["主題名稱八個字"] * 4}
        for _ in range(12)]
    for sym in ("VOO", "QQQ", "SOXX"):
        holdings = rows[f"us.13f.{sym}"]["values"]
        holdings["rows"] = [dict(r, name=long_name, symbols=["BRK.A", "BRK.B", "GOOGL", "GOOG"], increased=9_999_999,
                                 decreased=9_999_998, new=9_999_999, soldout=9_999_999) for r in holdings["rows"]] * 2
        holdings["holder_period_note"] = long_note
        holdings["weight_basis"] = long_note
        rows[f"us.fund.{sym}"]["values"]["rows"] = [dict(r, name=long_name, symbols=["BRK.A", "BRK.B"])
                                                     for r in rows[f"us.fund.{sym}"]["values"]["rows"]] * 2
        rows[f"us.etf.{sym}"]["values"].update(close=99999.99, chg_pct=-99.99, above_ma20=False, vol_label="明顯量縮",
                                               vol_flag="結算日")
        rows[f"us.breadth.{sym}"]["values"].update(universe_label=long_note, coverage_note=long_note, adv=9999,
                                                   dec=9999, flat=9999)
    rows["tw.etf.0050"]["values"].update(close=99999.99, chg_pct=-99.99, above_ma20=False)
    rows["tw.taiex"]["values"].update(close=99999.99, chg_pct=-99.99, above_ma20=False)
    rows["tw.turnover"]["values"].update(value_twd=9_999_900_000_000.0, vol_ratio=999.99, vol_label="明顯量縮",
                                         vol_flag="結算日")
    rows["tw.inst3"]["values"].update(foreign_yi=-99999.0, trust_yi=-99999.0)
    rows["tw.futures"]["values"].update(foreign_net_big=-99_999_999)
    # 2026-10-24 第 9 輪批 1 B 軌：期權摘要列的最壞值（四位數牆價、距離 ±14.9%）與 VXTLT（三位數、−99.99%）
    for sym in ("QQQ", "SOXX"):
        rows[f"us.options.{sym}"]["values"].update(call_wall=1050.0, put_wall=1010.0, call_wall_dist_pct=14.9,
                                                    put_wall_dist_pct=-14.9)
    rows["macro.VXTLT"]["values"].update(value=199.99, chg=-99.99, chg_pct=-99.99)
    rows["events.next"]["values"]["items"] = [
        {"name": long_name, "kind": "econ", "at": now + 86400 * (2 + i)} for i in range(12)]
    rows["trump.summary"]["values"].update(topics={f"主題{i}": 9999 for i in range(30)}, at_least=True)
    rows["contra.us"]["values"]["codes"] = ["gamma_pos_vix_inverted:SPY", "above_ma20_weak_breadth:SOXX"]
    rows["oneliner.tw"]["values"]["text"] = "註" * 30
    for key in ("VIX", "UST10Y", "DXY", "WTI", "GOLD", "BTC"):
        rows[f"macro.{key}"]["values"].update(value=99999.99 if key not in ("VIX", "UST10Y") else 99.99, chg=-99.99)
    return snap


def write_snapshot(paths, snapshot, *, ensure_ascii: bool = False) -> None:
    """同目錄暫存檔再 os.replace（新的 inode：快取鍵一定會變）。"""
    data = json.dumps(snapshot, ensure_ascii=ensure_ascii).encode("utf-8")
    tmp = paths.state_dir / f".snap-{uuid.uuid4().hex}"
    tmp.write_bytes(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, paths.snapshot)


def write_charts(paths, snapshot) -> None:
    for entry in snapshot["charts"].values():
        for name in entry["files"]:
            target = paths.chart_dir / name
            target.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            os.chmod(target, 0o600)


def _variants(now=NOW):
    normal = make_snapshot(now)
    stale = make_snapshot(now)
    for row in stale["rows"].values():
        row["stale_after"] = now - 1
    half = make_snapshot(now)
    for key in list(half["rows"])[::2]:
        half["rows"][key] = _never()
    return {"normal": normal, "worst": worst_snapshot(now), "stale": stale, "half_never": half}


# ── 走訪卡片 ─────────────────────────────────────────────────────────────────
def nodes(node):
    if isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key != "action":
                yield from nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from nodes(item)


def texts(card) -> list[str]:
    return [n["text"] for n in nodes(card) if n.get("type") == "text"]


def bubbles(card) -> list[dict]:
    return card["contents"] if card.get("type") == "carousel" else [card]


def actions(card) -> list[dict]:
    return [n["action"] for n in nodes(card) if isinstance(n.get("action"), dict)]


def size(card) -> int:
    return len(json.dumps(card, ensure_ascii=False).encode("utf-8"))


def render_all(md, paths, now, bodies=FLEX_BODIES):
    return {body: md.handle(body, now=now, paths=paths) for body in bodies}


def reply_strings(reply) -> list[str]:
    found = [reply.text, reply.alt_text, reply.image_alt, reply.fallback_text]
    if reply.flex is not None:
        found += texts(reply.flex)
        for action in actions(reply.flex):
            found += [action.get("label"), action.get("text")]
    return [s for s in found if s]


def assert_main_gate(reply) -> None:
    """main 送卡前的真閘門：節點白名單、message action、每個字原樣過 validator、FlexContainer.from_dict。"""
    import main

    if reply.flex is not None:
        main._validated_flex_card_message(reply.alt_text, reply.flex)
        assert main._prepare_outbound_text(reply.alt_text) == reply.alt_text
    for value in (reply.text, reply.image_alt, reply.fallback_text):
        if value:
            assert main._prepare_outbound_text(value) == value, value


def program_texts(strings) -> list[str]:
    """扣掉外部字串（新聞標題、名稱、job 的固定句）後剩下的 bot 自己的句子。"""
    found = []
    for value in strings:
        plain = value
        for external in EXTERNAL:
            plain = plain.replace(unicodedata.normalize("NFKC", external), "").replace(external, "")
        found.append(plain)
    return found


# ── 和 job 共用的契約 ─────────────────────────────────────────────────────────
def test_fixture_follows_row_ids_exactly():
    snap = make_snapshot(NOW)
    assert set(snap["rows"]) == set(market_calc.ROW_IDS)
    for row_id, row in snap["rows"].items():
        assert market_calc.row_ok(row_id, row), row_id
    assert set(snap["charts"]) == set(market_calc.CHART_KEYS)


def test_read_rows_and_chart_keys_match_market_calc(md):
    assert set(md.READ_ROWS) <= set(market_calc.ROW_IDS)
    assert set(md.CHART_KEYS.values()) == set(market_calc.CHART_KEYS)
    assert set(md.CHART_KEYS) == set(md.CHART_SYMBOLS)


def test_contradiction_sentences_are_market_calc_templates(md, paths):
    snap = make_snapshot(NOW)
    codes = [f"{code}:{subject}" for code in market_calc.CONTRA_CODES for subject in ("VOO", "0050", "TAIEX")]
    codes += list(market_calc.CONTRA_CODES) + ["unknown_code", "up_low_volume:EVIL", 5]
    snap["rows"]["contra.us"]["values"]["codes"] = codes
    write_snapshot(paths, snap)
    ctx = md._Ctx(snap, NOW)
    expected = []
    for item in codes[:10]:
        if not isinstance(item, str):
            continue
        code, _, subject = item.partition(":")
        if code in market_calc.CONTRA_TEXT and (not subject or subject in md.CONTRA_SUBJECTS):
            expected.append(market_calc.CONTRA_TEXT[code].format(subject=md.CONTRA_SUBJECTS.get(subject, "")))
    assert md._contra_sentences(ctx, "contra.us") == expected
    assert md.CONTRA_SUBJECTS == market_calc.SUBJECT_NAME  # 同一份（鍵與值都比：不各存一份）


def test_oneliner_grammar_matches_market_calc(md):
    day = date(2026, 10, 16)
    fact = market_calc.Fact
    for above in (True, False):
        for label in market_calc.VOL_LABELS:
            for breadth in (41.6, 100.0, 0.0):
                us = market_calc.one_liner({"VOO.above_ma20": fact(above, day), "VOO.breadth20_pct": fact(breadth, day),
                                            "VOO.vol_label": fact(label, day)}, "us")
                assert md._ONELINER_US_RE.fullmatch(us), us
            for foreign in (-120.4, 56.78, 1234.0):
                tw = market_calc.one_liner({"TAIEX.above_ma20": fact(above, day), "tw.foreign": fact(foreign, day),
                                            "TAIEX.vol_label": fact(label, day)}, "tw")
                assert md._ONELINER_TW_RE.fullmatch(tw), tw


# ── 摘要卡 ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("variant", ["normal", "worst"])
def test_summary_is_same_size_bubbles_with_numbered_titles(md, paths, variant):
    write_snapshot(paths, _variants()[variant])
    reply = md.handle("", now=NOW, paths=paths)
    card = reply.flex
    count = md.SUMMARY_CARD_COUNT
    assert card["type"] == "carousel" and len(card["contents"]) == count
    assert {b["size"] for b in card["contents"]} == {"giga"}
    for index, bubble in enumerate(card["contents"], 1):
        title = bubble["header"]["contents"][0]["text"]
        assert f"（{index}/{count}）" in title and re.search(r"｜\d{1,2}/\d{1,2}", title), title
        assert DISCLAIMER in texts(bubble["footer"])
    assert f"往左滑還有 {count - 1} 張" in texts(card["contents"][0]["header"])
    assert all("往左滑" not in t for b in card["contents"][1:] for t in texts(b["header"]))
    assert reply.alt_text.startswith("盤勢：") and len(reply.alt_text) <= 400
    assert reply.fallback_text.startswith("盤勢：")
    assert reply.render_errors == ()


@pytest.mark.parametrize("variant", ["normal", "worst", "stale", "half_never"])
def test_summary_rows_limit_width_and_jargon(md, paths, variant):
    write_snapshot(paths, _variants()[variant])
    card = md.handle("", now=NOW, paths=paths).flex
    for bubble in card["contents"]:
        body = bubble["body"]["contents"]
        assert 1 <= len(body) <= 6
        for node in body:
            assert node["type"] == "text"
            assert md.row_width(node["text"]) <= 24, node["text"]
            if not WALL_ROW_RE.fullmatch(node["text"]):  # 期權牆那兩列用代號開頭（2026-10-24 plan A15、s2-N11：中文名放不下）
                assert not re.match(r"[A-Za-z]", node["text"]), node["text"]  # 先中文、括號英文
        for text in texts(bubble):
            assert not any(word in text for word in SUMMARY_JARGON), text


@pytest.mark.parametrize("variant", ["normal", "worst", "stale", "half_never"])
def test_every_card_fits_line_limits_with_the_large_text_buttons(md, paths, variant):
    write_snapshot(paths, _variants()[variant])
    for body, reply in render_all(md, paths, NOW).items():
        assert reply.flex is not None, (body, reply.text)
        card = reply.flex
        assert size(card) <= 28 * 1024, body
        assert len(bubbles(card)) <= 12
        if body == "":
            assert all(size(b) <= 6656 for b in card["contents"])
        paged = card_pages.with_page_button(card, LONG_PAGE_URL)
        assert card_pages.fits_line_limits(paged), (body, size(paged))


# ── 2026-10-24 第 9 輪批 1（骨架）：張數常數、位置決定頁碼、最壞值大小 ────────────────────────────
def test_summary_texts_follow_the_card_count(md):
    count = md.SUMMARY_CARD_COUNT
    assert len(md.SUMMARY_CARDS) == count  # 登記的卡和張數常數一致（不是 import 時的 assert：A 軌改常數時才會被這裡擋住）
    assert md._page(1) == f"（1/{count}）" and md._page(count) == f"（{count}/{count}）"
    swipe = re.fullmatch(r"往左滑還有 (\d+) 張", md.TEXT_SWIPE)
    assert swipe and int(swipe.group(1)) == count - 1
    usage = re.fullmatch(r"/盤勢 → (\d+) 張摘要卡（(.+)）", md.TEXT_USAGE.splitlines()[1])
    assert usage and int(usage.group(1)) == count
    assert usage.group(2).split("、") == [name for name, _builder in md.SUMMARY_CARDS]  # 用法說明的卡名就是登記的卡名


def test_every_registered_summary_builder_exists(md):
    for name, builder in md.SUMMARY_CARDS:
        assert name and callable(getattr(md, builder)), (name, builder)
    assert len({builder for _name, builder in md.SUMMARY_CARDS}) == len(md.SUMMARY_CARDS)


def test_page_numbers_and_the_swipe_line_follow_the_position(md, paths, monkeypatch):
    """頁碼、第一張的「往左滑」是 SUMMARY_CARDS 的位置決定的，builder 不寫死：調換前兩張，標題與副標跟著位置走。"""
    write_snapshot(paths, make_snapshot(NOW))
    first, second, *rest = md.SUMMARY_CARDS
    monkeypatch.setattr(md, "SUMMARY_CARDS", (second, first, *rest))
    bubbles_ = md.handle("", now=NOW, paths=paths).flex["contents"]
    titles = [b["header"]["contents"][0]["text"] for b in bubbles_]
    assert titles[0].startswith(f"{second[0]}{md._page(1)}｜"), titles
    assert titles[1].startswith(f"{first[0]}{md._page(2)}｜"), titles
    assert md.TEXT_SWIPE in texts(bubbles_[0]["header"])
    assert all(md.TEXT_SWIPE not in texts(b["header"]) for b in bubbles_[1:])


def test_a_new_first_card_only_needs_the_constant_and_a_builder(md, paths, monkeypatch):
    """A 軌的做法：SUMMARY_CARD_COUNT 加 1、SUMMARY_CARDS 最前面插一行、寫新的 builder。標題頁碼、第一張的「往左滑」、
    用法說明的張數都照算，別張的 builder 一個字都不用改。這裡把原始碼的常數改成 N+1，其他用執行時換的。"""
    other = md.SUMMARY_CARD_COUNT + 1
    path = HERE / "market_dashboard.py"
    patched, replaced = re.subn(r"^SUMMARY_CARD_COUNT = \d+$", f"SUMMARY_CARD_COUNT = {other}",
                                path.read_text(encoding="utf-8"), flags=re.M)
    assert replaced == 1  # 它是一行單純的整數常數
    module = types.ModuleType("market_dashboard_one_more_card")
    module.__file__ = str(path)
    monkeypatch.setitem(sys.modules, module.__name__, module)  # dataclass 要在 sys.modules 找得到自己
    exec(compile(patched, str(path), "exec"), module.__dict__)
    assert module.TEXT_SWIPE == f"往左滑還有 {other - 1} 張"
    assert f"/盤勢 → {other} 張摘要卡" in module.TEXT_USAGE
    assert module._page(1) == f"（1/{other}）" and module._page(other) == f"（{other}/{other}）"
    module._card_probe = lambda ctx, head, subtitle=None: module._summary_bubble(
        f"{head}｜試排", [("一列", module.GRAY)], subtitle=subtitle)
    module.SUMMARY_CARDS = (("重點", "_card_probe"), *module.SUMMARY_CARDS)
    write_snapshot(paths, make_snapshot(NOW))
    reply = module.handle("", now=NOW, paths=paths)
    assert reply.render_errors == () and len(reply.flex["contents"]) == other
    titles = [b["header"]["contents"][0]["text"] for b in reply.flex["contents"]]
    assert titles[0] == f"重點（1/{other}）｜試排"
    assert [t.split("｜")[0] for t in titles[1:]] == [f"{name}（{i}/{other}）" for i, (name, _b) in
                                                       enumerate(md.SUMMARY_CARDS, 2)]
    assert module.TEXT_SWIPE in texts(reply.flex["contents"][0]["header"])
    assert all(module.TEXT_SWIPE not in texts(b["header"]) for b in reply.flex["contents"][1:])


def test_worst_case_summary_carousel_leaves_room_for_one_more_bubble(md, paths):
    """審查 r9 s4-F10：輪播之後從 4 張變 5 張，5 × 6,656 > 28,672，不能再靠「每張 ≤6.5 KB 所以整組一定夠」。
    最壞值快照量整組（md.card_bytes：json.dumps、UTF-8），再多一張同樣大的泡泡（連同 🔍 大字版按鈕）也要放得下。"""
    write_snapshot(paths, worst_snapshot())
    reply = md.handle("", now=NOW, paths=paths)
    card = reply.flex
    assert card is not None and reply.render_errors == (), reply.text  # 不是「卡片太大」的固定句
    sizes = [md.card_bytes(bubble) for bubble in card["contents"]]
    assert len(sizes) == md.SUMMARY_CARD_COUNT
    assert all(each <= md.SUMMARY_BUBBLE_MAX_BYTES for each in sizes), sizes
    assert md.card_bytes(card) <= md.CARD_MAX_BYTES == 28 * 1024, sizes
    one_more = dict(card, contents=[*card["contents"], max(card["contents"], key=md.card_bytes)])
    assert md.card_bytes(one_more) <= md.CARD_MAX_BYTES, (md.card_bytes(one_more), sizes)
    assert card_pages.fits_line_limits(card_pages.with_page_button(one_more, LONG_PAGE_URL)), sizes


def test_breadth_line_when_all_three_are_insufficient(md, paths):
    """2026-10-23 審查 r7a N3：三檔都寫「資料不足」會超寬被切成「SOXX 資…」：全部不足就合寫一次。"""
    snap = make_snapshot(NOW)
    for sym, total in (("VOO", 503), ("QQQ", 101), ("SOXX", 30)):
        snap["rows"][f"us.breadth.{sym}"]["values"] = _breadth(total, total // 2, total // 4, UNIVERSE)
    write_snapshot(paths, snap)
    us = texts(summary_bubble(md, md.handle("", now=NOW, paths=paths).flex, "美股")["body"])
    assert "成分股站上月線：資料不足" in us
    snap["rows"]["us.breadth.VOO"]["values"] = _breadth(503, 500, 310, UNIVERSE)
    write_snapshot(paths, snap)
    us = texts(summary_bubble(md, md.handle("", now=NOW, paths=paths).flex, "美股")["body"])
    line = next(text for text in us if "站上月線" in text)
    assert "VOO 62%" in line and "…" not in line and md.row_width(line) <= 24


def test_summary_rows_show_numbers_and_states(md, paths):
    write_snapshot(paths, make_snapshot(NOW))
    card = md.handle("", now=NOW, paths=paths).flex
    digest, us, tw, flow, macro = (texts(b["body"]) for b in card["contents"])
    assert digest == [  # 第 9 輪 A9：輪播第一張（SOXX 的資料日比 VOO、QQQ 晚一天 → 日期放前面）
        "美股 10/15：標普、半導體在月線上，那指在月線下",
        "台股：加權 +0.52% 月線上，外資賣超 123.4 億",
        "風險：VIX 20.0 平穩",  # 19.99 的一位小數是 20.0；狀態詞看實際值，和期權卡的「19.99，平穩」一致
        "利率：10 年債 5.09%（+12bp）",
        "事件：Consumer Price Index，還有 3 天",
        "川普壓力指數 +9.0（高壓，任期內第 88 百分位）",  # 第 6 列：川普壓力指數優先（2026-10-28 整合 A1×TACO）
    ]
    assert us == [
        # 2026-10-24 第 9 輪 A18：摘要列加「RSI nn」（放得下才寫；日期落後的 SOXX 放不下，RSI 先丟）
        "標普500（VOO）612.34 +0.52% 在月線上 RSI 55",
        "那斯達克100（QQQ）540.12 -0.31% 在月線下 RSI 55",
        "半導體（SOXX）245.6 +1.20% 在月線上（10/15）",  # 和標題不同日要寫日期
        "成分股站上月線：VOO 62%、QQQ 55%、SOXX 資料不足",  # SOXX 有效 22/30 <80%
        "量能：VOO 量偏少、QQQ 量偏少、SOXX 量偏少",  # 和一句話同一套字（資料值是「量縮」）
        ONELINER_US,  # 沒有矛盾時放一句話
    ]
    assert tw == [
        "加權指數 47,111（+0.52%） 在月線上 RSI 62",
        "台灣50（0050）185.3 +0.40% 在月線上 RSI 55",
        "0050 成分站上月線：62%（31/50 檔）",
        "成交金額 5,234 億，量偏少（0.78 倍）",
        "外資賣超 123.4 億、投信買超 45.6 億",
        "外資 5 日賣超、投信 5 日買超",  # 矛盾優先於一句話，用 market_calc 的固定句
    ]
    assert flow == [
        "恐慌指數（VIX）19.99（+0.12），平穩",
        "QQQ 買權牆 556（+3.0%）、賣權牆 518（-4.0%）",  # 2026-10-24 A15：兩列併一列、用代號；牆距的基準寫在 footer
        "SOXX 買權牆 252（+3.0%）、賣權牆 235（-4.0%）",  # SOXX 一列（Andrew 第 3 項：各區塊都要含 SOXX）
        "台指選擇權：買權牆 54,000、賣權牆 46,000",
        "外資台指期（大台）淨空 97,582 口",
        "融資：台股 +12.3 億、美股年增 +25%",
    ]
    assert macro == [
        "美債 10 年（UST10Y）5.09%（+0.12）",
        "美元（UUP 代理）單日 +0.30%、油價（WTI）72.3",  # ETF 代理只寫日漲跌幅，不印 ETF 的股價 28.92
        "黃金（Gold）2,650、比特幣（BTC）81,471",
        "下一個事件：Consumer Price Index，還有 3 天",
        "距期中選舉 17 天，Polymarket 民主黨拿下眾院 91%",  # 摘要點名平台（細節卡兩家都列）；90.5 四捨五入進位＝91（F2-16，以前是銀行家捨入的 90）
        "川普近 48 小時：關稅 2 則、伊朗 1 則",
    ]
    assert card["contents"][4]["header"]["contents"][0]["text"] == "宏觀與事件（5/5）｜10/17 16:20 更新"
    # 頁尾那一行：2026-10-24 起是 MacroMargin 的川普壓力指數（數字＋歷史位階），不再是「MM 川普壓力指數：抓不到（要登入）」
    assert texts(card["contents"][4]["footer"])[0] == "川普壓力指數 +9.0（高壓，任期內第 88 百分位）"
    assert all("MM 川普壓力指數" not in t for t in texts(card))


def test_alt_text_has_only_fixed_words_numbers_and_dates(md, paths):
    snap = make_snapshot(NOW)
    write_snapshot(paths, snap)
    replies = render_all(md, paths, NOW)
    assert replies[""].alt_text == f"盤勢：{ONELINER_US}｜{ONELINER_TW}｜資料 10/16"
    for body, reply in replies.items():
        alt = reply.alt_text
        assert alt.startswith("盤勢：") and len(alt) <= 400
        assert "@" not in alt and "＠" not in alt
        skip = (ONELINER_US, ONELINER_TW, *TOPICS)  # 一句話合固定句型才會進來；「伊朗」也是固定詞
        assert not any(external in alt for external in EXTERNAL if external not in skip), alt
    # 一句話不合固定句型（例 job 被改過）→ altText 改用 bot 自己組的固定詞
    snap["rows"]["oneliner.us"]["values"]["text"] = "標普收在月線上、某某標題"
    write_snapshot(paths, snap)
    assert md.handle("", now=NOW, paths=paths).alt_text.startswith("盤勢：美股 VOO 在月線上｜")


# ── 節點、顏色、按鈕 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("variant", ["normal", "worst"])
def test_node_whitelist_colors_and_buttons(md, paths, variant):
    write_snapshot(paths, _variants()[variant])
    for body, reply in render_all(md, paths, NOW).items():
        for node in nodes(reply.flex):
            assert node.get("type") in NODE_TYPES, (body, node.get("type"))
            assert not FORBIDDEN_KEYS & set(node), body
            for key, value in node.items():
                if "olor" in key:
                    assert value in PALETTE, (body, key, value)
            if node.get("type") == "text":
                assert isinstance(node["text"], str) and node["text"].strip()
            if node.get("type") == "box":
                assert node["contents"], (body, "空的 box")  # 拿掉按鈕後不能留下空的按鈕區
            action = node.get("action")
            if action is not None:
                assert node["type"] == "box" and set(action) == {"type", "label", "text"}
                assert action["type"] == "message" and len(action["label"]) <= 20
                assert action["text"] in md.SHIPPED_MARKET_TEXTS, action["text"]
                (inner,) = node["contents"]
                assert inner["type"] == "text" and inner["size"] == "xxl" and inner["text"] == action["label"]
        assert all(sum(1 for n in b["footer"]["contents"] if "action" in n) <= 3 for b in bubbles(reply.flex))
        assert "🔍" not in json.dumps(reply.flex, ensure_ascii=False)  # main 經 card_pages 加


def test_shipped_texts_route_and_every_button_text_is_shipped(md, paths):
    """卡片按鈕送出的字 ⊆ SHIPPED，而且 SHIPPED 每一個都還接得到路由。

    2026-10-24 第 9 輪（A10）：新卡片不再有「回摘要」按鈕，所以不再要求「SHIPPED 每個都有按鈕」；但舊卡片留在聊天紀錄裡的
    「回摘要」仍送 /盤勢，/盤勢 一定要留在 SHIPPED（只能加不能刪）。"""
    assert len(set(md.SHIPPED_MARKET_TEXTS)) == len(md.SHIPPED_MARKET_TEXTS)
    for text in md.SHIPPED_MARKET_TEXTS:
        match = re.match(r"/\s{0,3}盤勢", text)
        assert match is not None and md.parse_command(text[match.end():]) is not None, text
    assert "/盤勢" in md.SHIPPED_MARKET_TEXTS and md.parse_command("") == (md.SUMMARY, None)
    for variant, snap in _variants().items():
        write_snapshot(paths, snap)
        sent = [a for r in render_all(md, paths, NOW).values() for a in actions(r.flex)]
        assert {a["text"] for a in sent} <= set(md.SHIPPED_MARKET_TEXTS), variant
        assert "回摘要" not in {a["label"] for a in sent}, variant
    # 其他導覽按鈕一個都沒少：新卡片送得出來的字＝SHIPPED 扣掉只剩舊卡片會送的 /盤勢
    write_snapshot(paths, make_snapshot(NOW))
    sent = {a["text"] for r in render_all(md, paths, NOW).values() for a in actions(r.flex)}
    assert sent == set(md.SHIPPED_MARKET_TEXTS) - {"/盤勢"}


def test_cards_left_without_navigation_buttons_keep_a_clean_footer(md, paths):
    """季節性、選舉、新聞前兩張拿掉「回摘要」後沒有任何按鈕（可接受）：footer 只剩註記與免責句，沒有空的按鈕區。"""
    write_snapshot(paths, make_snapshot(NOW))
    replies = render_all(md, paths, NOW)
    for body, index in ((" 季節性", 0), (" 選舉", 0), (" 新聞", 0), (" 新聞", 1)):
        bubble = bubbles(replies[body].flex)[index]
        assert not actions(bubble), (body, index)
        footer = bubble["footer"]["contents"]
        assert footer and all(node["type"] == "text" for node in footer), (body, index)
        assert DISCLAIMER in texts(bubble["footer"])


def test_shipped_texts_cover_the_routing_tests(md):
    tree = ast.parse((HERE / "test_market_routing.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "SHIPPED_MARKET_TEXTS" for t in node.targets):
            assert set(ast.literal_eval(node.value)) <= set(md.SHIPPED_MARKET_TEXTS)
            return
    pytest.skip("test_market_routing has no SHIPPED_MARKET_TEXTS")


# ── 每個字都過 validator（三個年份） ─────────────────────────────────────────
@pytest.mark.parametrize("now", ALL_NOWS, ids=["2026-10-17", "2027-01-15", "2028-01-03"])
def test_every_string_passes_the_validator_in_later_years(md, paths, now):
    snap = make_snapshot(now)
    write_snapshot(paths, snap)
    write_charts(paths, snap)
    at = datetime.fromtimestamp(now, TPE)
    for body, reply in render_all(md, paths, now, FLEX_BODIES + CHART_BODIES + (" abc", " 13F 0050")).items():
        for value in reply_strings(reply):
            result = output_validator.validate_outbound_text(value, now=at)
            assert result.ok and result.text == value, (body, value, result.reason)
    for name in dir(md):
        value = getattr(md, name)
        if name.startswith("TEXT_") and isinstance(value, str):
            result = output_validator.validate_outbound_text(value, now=at)
            assert result.ok and result.text == value, (name, result.reason)


def test_main_gate_accepts_every_reply(md, paths):
    for variant, snap in _variants().items():
        write_snapshot(paths, snap)
        write_charts(paths, snap)
        for reply in render_all(md, paths, NOW, FLEX_BODIES + CHART_BODIES + (" abc",)).values():
            assert_main_gate(reply)


def test_forbidden_words_only_checked_on_program_sentences(md, paths):
    snap = make_snapshot(NOW)
    quoted = "專家建議逢低布局"  # 新聞標題是引文：不套禁用字（只過 validator）
    snap["rows"]["news.us"]["values"]["items"].append({"title": quoted, "source": "中央社", "published_utc": NOW - 60})
    write_snapshot(paths, snap)
    replies = render_all(md, paths, NOW)
    assert quoted in texts(replies[" 新聞"].flex)
    for body, reply in replies.items():
        for plain in program_texts(s for s in reply_strings(reply) if s != quoted):
            assert not md.has_forbidden(plain), (body, plain)
            assert not any(word in plain.replace(DISCLAIMER, "") for word in PLAN_FORBIDDEN), (body, plain)
    for name in dir(md):
        if name.startswith("TEXT_") and isinstance(getattr(md, name), str):
            assert not md.has_forbidden(getattr(md, name)), name
    assert md.has_forbidden("可以逢低布局") and not md.has_forbidden(DISCLAIMER)
    assert set(market_calc.FORBIDDEN_WORDS) <= set(md.FORBIDDEN_WORDS)


def test_empty_oneliner_is_not_a_dropped_string(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["oneliner.us"]["values"]["text"] = ""  # market_calc.one_liner 沒有主指數時回空字串
    snap["rows"]["news.us"]["values"]["items"][0]["source"] = None
    write_snapshot(paths, snap)
    card = md.handle("", now=NOW, paths=paths).flex
    assert len(summary_bubble(md, card, "美股")["body"]["contents"]) == 5
    assert md.status_summary(now=NOW, paths=paths)["external_dropped"] == 0


def test_job_sentences_with_forbidden_words_are_dropped(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["oneliner.us"]["values"]["text"] = "美股在月線上，建議進場"
    write_snapshot(paths, snap)
    card = md.handle("", now=NOW, paths=paths).flex
    assert not any("進場" in t for t in texts(card))
    assert len(summary_bubble(md, card, "美股")["body"]["contents"]) == 5


def test_fixed_sentences_make_no_promises(md, paths, monkeypatch):
    spec = importlib.util.spec_from_file_location("bot_promise_scan_for_market", HERE / "jobs" / "bot_promise_scan.py")
    scan = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, scan)  # 它的 dataclass 要在 sys.modules 找得到自己
    spec.loader.exec_module(scan)
    write_snapshot(paths, make_snapshot(NOW))
    strings = [getattr(md, n) for n in dir(md) if n.startswith("TEXT_") and isinstance(getattr(md, n), str)]
    for reply in render_all(md, paths, NOW).values():
        strings += program_texts(reply_strings(reply))
    for value in strings:
        assert scan.promise_sentences(value) == [], value
        assert not re.search(r"下次會|之後會|我會", value), value


# ── 外部字串（PLAN §19.1） ───────────────────────────────────────────────────
def test_unclean_external_strings_are_dropped_and_the_card_still_sends(md, paths):
    snap = make_snapshot(NOW)
    items = snap["rows"]["news.us"]["values"]["items"]
    items[:] = [{"title": t, "source": "中央社", "published_utc": NOW - 60} for t in DROP_TITLES] + items
    snap["rows"]["us.13f.QQQ"]["values"]["rows"][0]["name"] = "@all"
    snap["rows"]["trump.posts"]["values"]["items"][0]["quoted_author"] = "x\u2028y"
    write_snapshot(paths, snap, ensure_ascii=True)  # 孤立代理字元只能用 \\ud800 寫進 JSON
    replies = render_all(md, paths, NOW)
    for body, reply in replies.items():
        assert reply.flex is not None, body
        assert_main_gate(reply)
        for value in reply_strings(reply):
            assert "@" not in value and "＠" not in value and "all" not in value.replace("　", " ").split(), value
            assert not any(unicodedata.category(ch) in {"Cf", "Cs", "Zl", "Zp", "Cc"} for ch in value if ch != "\n"), value
            assert "媽媽" not in value and "大漲" not in value and "零寬" not in value and "孤立" not in value
            assert "9月出口" not in value
    news = texts(replies[" 新聞"].flex)
    assert GOOD_TITLES[0] in news
    assert "另有 6 則格式不符，沒有列出" in news
    assert TRUMP_TEXT in news  # 引用作者壞掉只丟作者，貼文照列
    holders = texts(replies[" 13F QQQ"].flex)
    assert any(t.startswith("1. NVDA 權重 9.0%") for t in holders)  # 名稱丟了，代號照列
    assert md.status_summary(now=NOW, paths=paths)["external_dropped"] >= 8


def test_external_strings_with_urls_phones_or_html(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["news.tw"]["values"]["items"] = [
        {"title": "看這裡 https://example.com/x", "source": "中央社", "published_utc": NOW - 60},
        {"title": "請打 0900000000", "source": "中央社", "published_utc": NOW - 60},
        {"title": "<b>粗體</b>標題&amp;", "source": "中央社", "published_utc": NOW - 60},
    ]
    write_snapshot(paths, snap)
    news = texts(md.handle(" 新聞", now=NOW, paths=paths).flex)
    assert not any("example.com" in t or "0900000000" in t or "<b>" in t for t in news)
    assert market_calc.clean_external("<b>粗體</b>標題&amp;", 40) in news


def test_job_notes_keep_full_width_punctuation_but_are_still_checked(md, paths):
    snap = make_snapshot(NOW)
    write_snapshot(paths, snap)
    found = texts(md.handle(" 13F VOO", now=NOW, paths=paths).flex)
    assert UNIVERSE in found and PERIOD_NOTE in found  # 沒有被 NFKC 成半形括號
    snap["rows"]["us.13f.VOO"]["values"]["weight_basis"] = "依權重 https://example.com"
    snap["rows"]["us.13f.VOO"]["values"]["holder_period_note"] = "<i>期間</i>"
    write_snapshot(paths, snap)
    found = texts(md.handle(" 13F VOO", now=NOW, paths=paths).flex)
    assert "依 S&P 500 權重（SPY 每日持股）" in found and not any("example" in t or "期間</i>" in t for t in found)


def test_old_or_undated_news_is_not_listed(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["news.us"]["values"]["items"] = [
        {"title": "很舊的標題", "source": "中央社", "published_utc": NOW - 40 * 3600},
        {"title": "沒有時間的標題", "source": "中央社", "published_utc": None},
        {"title": "沒有時區的標題", "source": "中央社", "published_utc": "2026-10-17T08:00:00"},
        {"title": "未來的標題", "source": "中央社", "published_utc": NOW + 3600},
    ]
    write_snapshot(paths, snap)
    news = texts(md.handle(" 新聞", now=NOW, paths=paths).flex)
    assert not {"很舊的標題", "沒有時間的標題", "沒有時區的標題", "未來的標題"} & set(news)
    assert "36 小時內沒有列入的標題" in news


# ── 過期、暫缺、期間未標 ─────────────────────────────────────────────────────
def test_stale_rows_show_the_data_date_and_no_numbers(md, paths):
    write_snapshot(paths, _variants()["stale"])
    for body, reply in render_all(md, paths, NOW).items():
        joined = "\n".join(texts(reply.flex))
        for number in ("612.34", "540.12", "47,111", "19.99", "97,582", "123.4", "556", "28.92", "5,234"):
            assert number not in joined, (body, number)
    card = md.handle("", now=NOW, paths=paths).flex
    assert texts(summary_bubble(md, card, "美股")["body"])[0] == "標普500（VOO）過期（資料日 10/16）"
    titles = [b["header"]["contents"][0]["text"] for b in card["contents"]]
    assert titles[1:4] == ["美股（2/5）｜資料過期", "台股（3/5）｜資料過期", "期權與資金（4/5）｜資料過期"]  # 列上寫過期，標題同字
    assert texts(md.handle(" VOO", now=NOW, paths=paths).flex)[0] == "標普500（VOO）｜資料過期"
    assert texts(md.handle(" 0050", now=NOW, paths=paths).flex)[0] == "台灣50（0050）｜資料過期"


def test_never_rows_show_missing(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["us.etf.QQQ"] = _never()
    del snap["rows"]["tw.inst3"]  # 整列不見也一樣
    write_snapshot(paths, snap)
    card = md.handle("", now=NOW, paths=paths).flex
    assert texts(summary_bubble(md, card, "美股")["body"])[1] == "那斯達克100（QQQ）暫缺"
    assert texts(summary_bubble(md, card, "台股")["body"])[4] == "外資與投信 暫缺"


def test_unknown_period_shows_numbers_and_the_label(md, paths):
    write_snapshot(paths, make_snapshot(NOW))
    found = texts(md.handle(" 13F QQQ", now=NOW, paths=paths).flex)
    # job 用 market_calc.inst13f_summary 算的；成分股一律「檔」、機構一律「家」
    assert "前十大成分股中，3 檔的加碼機構多於減碼機構（1 檔資料暫缺）" in found
    assert "彙總期間：期間未標" in found
    assert any(t.startswith("5. Alphabet（GOOGL／GOOG）") for t in found)  # 股別合併成一家


def test_nan_and_wrong_types_in_row_metadata_mean_missing(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["us.etf.VOO"]["stale_after"] = float("nan")
    snap["rows"]["us.etf.QQQ"]["stale_after"] = True
    snap["rows"]["us.etf.SOXX"]["state"] = "fresh"
    snap["rows"]["tw.taiex"]["values"] = [1, 2]
    write_snapshot(paths, snap)
    card = md.handle("", now=NOW, paths=paths).flex
    us = texts(summary_bubble(md, card, "美股")["body"])
    assert all(row.endswith("暫缺") for row in us[:3]), us
    assert texts(summary_bubble(md, card, "台股")["body"])[0] == "加權指數 暫缺"


JUNK = ("x", -1, 10**20, 1e308, True, None, [], {}, [1, None, "x", {"a": []}], {"a": {"b": "c"}})


def _corrupt_leaves(value, junk):
    if isinstance(value, dict):
        return {k: _corrupt_leaves(v, junk) for k, v in value.items()}
    if isinstance(value, list):
        return [_corrupt_leaves(v, junk) for v in value] or [junk]
    return junk


@pytest.mark.parametrize("junk", JUNK, ids=[repr(j)[:12] for j in JUNK])
def test_corrupted_values_never_crash_and_still_pass_the_gate(md, paths, junk):
    for mode in ("leaves", "values"):
        snap = make_snapshot(NOW)
        for row in snap["rows"].values():
            row["values"] = _corrupt_leaves(row["values"], junk) if mode == "leaves" else {k: junk for k in row["values"]}
        snap["charts"] = {k: _corrupt_leaves(v, junk) for k, v in snap["charts"].items()}
        snap["progress"] = _corrupt_leaves(snap["progress"], junk)
        write_snapshot(paths, snap)
        for body, reply in render_all(md, paths, NOW, FLEX_BODIES + CHART_BODIES).items():
            assert isinstance(reply, md.Reply)
            assert reply.flex is not None or reply.text, (mode, body)
            # 輪播有逐泡泡隔離：壞掉的 builder 只會變成「這張卡暫時出不來」，所以要明確檢查沒有任何一張出錯（含新的重點摘要）
            assert reply.render_errors == (), (mode, body, reply.render_errors)
            assert_main_gate(reply)
            if reply.flex is not None:
                assert size(reply.flex) <= 28 * 1024
                if body == "":
                    for bubble in reply.flex["contents"]:
                        assert len(bubble["body"]["contents"]) <= 6
                        assert all(md.row_width(t) <= 24 for t in texts(bubble["body"]))


# ── 讀快照 ───────────────────────────────────────────────────────────────────
def test_missing_snapshot_says_preparing(md, paths):
    reply = md.handle("", now=NOW, paths=paths)
    assert reply.text == "盤勢：資料準備中，請過幾分鐘再按一次" and reply.flex is None


@pytest.mark.parametrize("raw", [b"{not json", b"\xff\xfe", b"[]", b'{"schema": 2, "rows": {}}',
                                 b'{"schema": true, "rows": {}}', b'{"schema": 1, "rows": []}', b"[" * 5000 + b"]" * 5000],
                         ids=["json", "utf8", "list", "schema2", "schema_bool", "rows_list", "deep"])
def test_broken_snapshot_says_preparing(md, paths, raw):
    tmp = paths.state_dir / "tmp"
    tmp.write_bytes(raw)
    os.chmod(tmp, 0o600)
    os.replace(tmp, paths.snapshot)
    assert md.handle("", now=NOW, paths=paths).text == "盤勢：資料準備中，請過幾分鐘再按一次"
    assert md.handle(" QQQ 圖", now=NOW, paths=paths).text == "盤勢：資料準備中，請過幾分鐘再按一次"


def test_oversized_snapshot_is_refused(md, paths):
    snap = make_snapshot(NOW)
    snap["padding"] = "x" * (2 * 1024 * 1024)
    write_snapshot(paths, snap)
    assert md.handle("", now=NOW, paths=paths).text == "盤勢：資料準備中，請過幾分鐘再按一次"


def test_symlinked_snapshot_is_refused(md, paths, tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps(make_snapshot(NOW)), encoding="utf-8")
    os.chmod(real, 0o600)
    os.symlink(real, paths.snapshot)
    assert md.handle("", now=NOW, paths=paths).text == "盤勢：資料準備中，請過幾分鐘再按一次"


def test_all_rows_never_says_preparing_with_progress(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"] = {key: _never() for key in snap["rows"]}
    snap["progress"]["us_members"] = [480, 520]
    write_snapshot(paths, snap)
    reply = md.handle("", now=NOW, paths=paths)
    assert reply.text == "盤勢：資料準備中，請過幾分鐘再按一次（歷史資料補到 480/520 檔）"
    assert_main_gate(reply)


def test_bootstrap_progress_note_on_the_first_card(md, paths):
    snap = make_snapshot(NOW)
    snap["progress"]["us_members"] = [480, 520]
    write_snapshot(paths, snap)
    card = md.handle("", now=NOW, paths=paths).flex
    assert "美股歷史資料補到 480/520 檔" in texts(summary_bubble(md, card, "美股")["footer"])


def test_parsed_snapshot_is_cached_until_the_file_changes(md, paths, monkeypatch):
    write_snapshot(paths, make_snapshot(NOW))
    calls = []
    original = md._parse_snapshot
    monkeypatch.setattr(md, "_parse_snapshot", lambda raw: calls.append(1) or original(raw))
    md.handle("", now=NOW, paths=paths)
    md.handle(" VOO", now=NOW, paths=paths)
    assert len(calls) == 1
    snap = make_snapshot(NOW)
    snap["rows"]["us.etf.VOO"]["values"]["close"] = 700.0
    write_snapshot(paths, snap)
    card = md.handle("", now=NOW, paths=paths).flex
    assert len(calls) == 2 and texts(summary_bubble(md, card, "美股")["body"])[0].startswith("標普500（VOO）700")


# ── 停用旗標 ─────────────────────────────────────────────────────────────────
def test_disabled_flag_is_checked_on_every_call(md, paths):
    write_snapshot(paths, make_snapshot(NOW))
    assert md.handle("", now=NOW, paths=paths).flex is not None
    paths.dashboard_disabled.write_text("", encoding="utf-8")
    for body in FLEX_BODIES + CHART_BODIES + (" abc",):
        assert md.handle(body, now=NOW, paths=paths).text == "盤勢：暫停使用"
    paths.dashboard_disabled.unlink()
    assert md.handle("", now=NOW, paths=paths).flex is not None
    os.symlink(paths.state_dir / "nowhere", paths.dashboard_disabled)  # 斷掉的 symlink 也算存在
    assert md.handle("", now=NOW, paths=paths).text == "盤勢：暫停使用"


def test_flag_that_cannot_be_checked_counts_as_disabled(md, paths, tmp_path):
    write_snapshot(paths, make_snapshot(NOW))
    blocker = tmp_path / "a_file"
    blocker.write_text("", encoding="utf-8")
    odd = dataclasses.replace(paths, dashboard_disabled=blocker / "market_dashboard.disabled")  # NotADirectoryError
    assert md.handle("", now=NOW, paths=odd).text == "盤勢：暫停使用"
    assert md.status_summary(now=NOW, paths=odd)["disabled"] is True


def test_the_job_flag_does_not_stop_the_bot(md, paths):
    write_snapshot(paths, make_snapshot(NOW))
    paths.disabled.write_text("", encoding="utf-8")  # 部署時 job 的旗標：bot 照樣讀舊快照
    assert md.handle("", now=NOW, paths=paths).flex is not None


# ── 指令解析 ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("body, expected", [
    ("", ("summary", None)), ("  ", ("summary", None)), (" voo", ("etf", "VOO")), ("ＱＱＱ", ("etf", "QQQ")),
    (" 0050", ("tw", "0050")), (" 期權？", ("options", None)), (" 新聞", ("news", None)),
    (" 季節性", ("season", None)), (" 選舉", ("election", None)), (" 13f qqq", ("13f", "QQQ")),
    (" 13FVOO", ("13f", "VOO")), (" 13F 0050", ("no_13f", "0050")), (" qqq圖", ("chart", "QQQ")),
    ("　台指　圖", ("chart", "台指")), (" SOXX\u200b 圖", ("chart", "SOXX")),
])
def test_parse_command(md, body, expected):
    assert md.parse_command(body) == expected


@pytest.mark.parametrize("body", [" abc", " VOO 圖", " 13F", "圖", " SPY", " 台指", " QQQ QQQ", "x" * 500,
                                  " @all", " 期權 新聞"])
def test_anything_else_gets_the_usage_text(md, paths, body):
    write_snapshot(paths, make_snapshot(NOW))
    reply = md.handle(body, now=NOW, paths=paths)
    assert reply.text == md.TEXT_USAGE and reply.flex is None
    assert_main_gate(reply)
    for needed in ("/盤勢 VOO", "/盤勢 QQQ 圖", "/盤勢 13F QQQ", "/盤勢 新聞", "/盤勢 季節性", "/盤勢 選舉", DISCLAIMER):
        assert needed in reply.text


def test_none_body_is_the_bare_command(md, paths):
    write_snapshot(paths, make_snapshot(NOW))
    assert md.handle(None, now=NOW, paths=paths).flex["type"] == "carousel"


def test_0050_has_no_13f(md, paths):
    reply = md.handle(" 13F 0050", now=NOW, paths=paths)
    assert reply.text == "盤勢：0050 是台股 ETF，沒有 13F 資料；台股細節請打 /盤勢 0050"


# ── 圖 ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("body", CHART_BODIES)
def test_chart_reply_returns_file_names_only(md, paths, body):
    snap = make_snapshot(NOW)
    write_snapshot(paths, snap)
    write_charts(paths, snap)
    reply = md.handle(body, now=NOW, paths=paths)
    assert reply.images == snap["charts"][CHART_KEY[body]]["files"] and reply.flex is None and reply.text is None
    assert reply.image_alt.startswith("盤勢圖：") and reply.image_alt.endswith("｜資料 10/16")
    assert reply.fallback_text is None  # 送不出去的句子是 main._MARKET_CHART_FAILURE_TEXT，bot 不放第二套


def test_chart_alt_matches_the_routing_example(md, paths):
    snap = make_snapshot(NOW)
    write_snapshot(paths, snap)
    write_charts(paths, snap)
    assert md.handle(" QQQ 圖", now=NOW, paths=paths).image_alt == "盤勢圖：QQQ 期權未平倉與 gamma（假設部位）｜資料 10/16"


@pytest.mark.parametrize("mutate", [
    lambda s: s["charts"]["QQQ"].update(stale_after=NOW - 1),
    lambda s: s["charts"]["QQQ"].update(stale_after=float("nan")),
    lambda s: s["charts"]["QQQ"].update(files=["A" * 32 + ".png"]),
    lambda s: s["charts"]["QQQ"].update(files=["../" + "0" * 32 + ".png"]),
    lambda s: s["charts"]["QQQ"].update(files=["e" * 32 + ".png"]),  # 檔案不在
    lambda s: s["charts"]["QQQ"].update(files=[]),
    lambda s: s["charts"]["QQQ"].update(files=["0" * 32 + ".png"] * 2),
    lambda s: s["charts"]["QQQ"].update(files=["0" * 32 + ".png"] * 6),
    lambda s: s["charts"]["QQQ"].update(files=[5]),
    lambda s: s["charts"].pop("QQQ"),
    lambda s: s.pop("charts"),
], ids=["stale", "nan", "upper", "traversal", "missing_file", "empty", "duplicate", "too_many", "not_str", "no_entry",
        "no_charts"])
def test_unusable_chart_gets_the_fixed_sentence(md, paths, mutate):
    snap = make_snapshot(NOW)
    write_charts(paths, snap)
    mutate(snap)
    write_snapshot(paths, snap)
    reply = md.handle(" QQQ 圖", now=NOW, paths=paths)
    assert reply.text == "盤勢圖：這張圖還沒畫好或已經過期，請過幾分鐘再按一次" and reply.images is None
    assert_main_gate(reply)


def test_symlinked_or_empty_chart_files_are_refused(md, paths, tmp_path):
    snap = make_snapshot(NOW)
    write_snapshot(paths, snap)
    write_charts(paths, snap)
    target = paths.chart_dir / snap["charts"]["QQQ"]["files"][0]
    target.unlink()
    os.symlink(tmp_path / "elsewhere.png", target)
    assert md.handle(" QQQ 圖", now=NOW, paths=paths).images is None
    target.unlink()
    target.write_bytes(b"")
    assert md.handle(" QQQ 圖", now=NOW, paths=paths).images is None


def test_chart_dir_must_be_private_and_is_never_created(md, paths):
    snap = make_snapshot(NOW)
    write_snapshot(paths, snap)
    write_charts(paths, snap)
    os.chmod(paths.chart_dir, 0o755)
    assert md.handle(" QQQ 圖", now=NOW, paths=paths).images is None
    os.chmod(paths.chart_dir, 0o700)
    assert md.handle(" QQQ 圖", now=NOW, paths=paths).images is not None
    for name in os.listdir(paths.chart_dir):
        os.unlink(paths.chart_dir / name)
    os.rmdir(paths.chart_dir)
    assert md.handle(" QQQ 圖", now=NOW, paths=paths).images is None
    assert not paths.chart_dir.exists()


# ── 細節卡 ───────────────────────────────────────────────────────────────────
def test_detail_cards_have_their_sections(md, paths):
    write_snapshot(paths, make_snapshot(NOW))
    voo = texts(md.handle(" VOO", now=NOW, paths=paths).flex)
    assert voo[0] == "標普500（VOO）｜10/16 收盤"
    assert "收盤 612.34（+0.52%）" in voo and "離月線 +1.2%、離季線 +3.4%" in voo
    assert any(t.startswith("站上月線：310 檔（62%，有效 500/503）") for t in voo)
    assert "前十大本益比中位數 34.5（8/10 檔有效）" in voo
    assert "量能：0.78 倍（前 20 日平均），近 250 日第 40 百分位，量偏少" in voo  # 細節卡也用「量偏少」
    assert f"特斯拉（TSLA）：{market_calc.TEXT_PE_NONPOSITIVE}" in voo
    assert market_calc.TEXT_US_NO_DIVIDEND in voo
    options = md.handle(" 期權", now=NOW, paths=paths).flex
    assert len(options["contents"]) == 2
    joined = "\n".join(texts(options))
    for needed in (market_calc.TEXT_GEX_NEGATIVE, market_calc.TEXT_MAX_PAIN, market_calc.TEXT_WALL_DEF,
                   "變號點（價格由低往高）：535（由正轉負）、562（由負轉正）",  # 和圖同一份對照表
                   "外資台指期淨部位（資料 10/16）：大台 淨空 97,582 口、小台 淨空 3,928 口、微台 淨空 12,415 口（大台單日 -1,200）",
                   "台指選擇權（11 月，資料 10/16）：買權牆 54,000、賣權牆 46,000、最大痛點 50,000",
                   "賣買權未平倉比（PCR）1.25、賣買權成交量比 0.90、隱含波動率（IV30）18.5%",
                   "賣買權未平倉比（PCR）60.8%（全部台指選擇權，和月選範圍不同）",
                   "槓桿基金淨部位（CFTC，標普 500 期貨）-123,456 口，週變化 +5,000（10/16 週報）",
                   "美股融資餘額（FINRA）1.48 兆美元（9 月），年增 +25.3%"):
        assert needed in joined, needed
    tw = "\n".join(texts(md.handle(" 0050", now=NOW, paths=paths).flex))
    for needed in ("上市漲跌家數：上漲 684（漲停 19）、下跌 300（跌停 2）、平盤 50",
                   "近 5 日合計：外資賣超 500.0 億、投信買超 200.0 億、自營買超 30.0 億",
                   "融資餘額 3,456 億（+12.3 億），10/16（參考）", "9 月營收年增 >0：31/50 檔（62%）",
                   "外資 5 日賣超、投信 5 日買超"):
        assert needed in tw, needed
    news = md.handle(" 新聞", now=NOW, paths=paths).flex
    assert len(news["contents"]) == 3
    joined = "\n".join(texts(news))
    assert TRUMP_TEXT in joined and market_calc.TEXT_TRUMP_NOTE in joined
    assert "美元（UUP 代理）：單日 +0.30%｜10/16" in joined  # 代理不印 ETF 的股價，也不印 ETF 股價的變動
    assert "美債 10 年（UST10Y）：5.09%（+0.12 個百分點）｜10/16" in joined  # 細節寫出單位
    assert "聯準會利率決議（FOMC）：10/28 16:30（台北）" in joined and "財報（NVDA）" in joined


def test_earnings_date_without_time_is_not_shown_as_a_taipei_time(md, paths):
    """財報日只有日期（market_publish 放美東 16:00、time_known=False）：卡上寫美東日期與「時間未公布」，
    不把猜的 16:00 換成台北的「10/21 04:00」寫出來；倒數用美東的今天算（2026-10-18 F4 回報）。"""
    snap = make_snapshot(NOW)
    at = datetime(2026, 10, 20, 16, 0, tzinfo=ET).timestamp()  # 美東 10/20 16:00＝台北 10/21 04:00
    snap["rows"]["events.next"]["values"]["items"] = [
        {"name": "NVDA", "kind": "earnings", "at": at, "time_known": False, "estimated": True},
    ]
    write_snapshot(paths, snap)
    detail = "\n".join(texts(md.handle(" 新聞", now=NOW, paths=paths).flex))
    assert "財報（NVDA，預估）：美東 10/20（時間未公布）" in detail  # estimated=True：日期是預估的
    assert "04:00" not in detail
    summary = "\n".join(texts(md.handle("", now=NOW, paths=paths).flex))
    # NOW＝台北 10/17 16:30＝美東 10/17 04:30；名稱加了「預估」放不下，先拿掉「（美東日期）」、保留「下一個事件：」
    assert "下一個事件：財報（NVDA，預估），還有 3 天" in summary


def test_detail_events_always_list_fomc(md, paths):
    """細節卡只列 6 筆：財報季時前面全是財報，FOMC 還是要列出來（2026-10-18）。"""
    snap = make_snapshot(NOW)
    items = [{"name": f"Z{c}", "kind": "earnings", "at": NOW + (n + 1) * 3600, "time_known": False}
             for n, c in enumerate("ABCDEFGH")]
    items.append({"name": "FOMC", "kind": "fomc", "at": NOW + 11 * 86400})
    snap["rows"]["events.next"]["values"]["items"] = items
    write_snapshot(paths, snap)
    news = "\n".join(texts(md.handle(" 新聞", now=NOW, paths=paths).flex))
    assert "聯準會利率決議（FOMC）" in news and news.count("財報（Z") == 5


def test_econ_events_use_the_shared_chinese_labels(md, paths):
    """PLAN §6：經濟事件先中文、括號英文（2026-10-18：摘要卡原本印英文「Initial Jobless Claims」）。"""
    snap = make_snapshot(NOW)
    snap["rows"]["events.next"]["values"]["items"] = [
        {"name": "Initial Jobless Claims", "kind": "econ", "at": NOW + 2 * 86400}]
    write_snapshot(paths, snap)
    summary = "\n".join(texts(md.handle("", now=NOW, paths=paths).flex))
    assert "下一個事件：初領失業金，還有 2 天" in summary  # 太寬：拿掉括號裡的英文、保留「下一個事件：」
    detail = "\n".join(texts(md.handle(" 新聞", now=NOW, paths=paths).flex))
    assert "初領失業金（Jobless Claims）：" in detail


def test_trump_topics_that_tie_follow_the_topic_table(md, paths):
    """同票時照主題表的先後（關稅、伊朗、聯準會、利率、油價、中國…），不照字碼（2026-10-18）。"""
    snap = make_snapshot(NOW)
    snap["rows"]["trump.summary"]["values"]["topics"] = {"中國": 2, "伊朗": 2, "油價": 2, "關稅": 2}
    write_snapshot(paths, snap)
    line = next(t for t in texts(md.handle("", now=NOW, paths=paths).flex) if t.startswith("川普近 48 小時"))
    assert "關稅 2 則" in line and "伊朗 2 則" in line and "中國" not in line, line


def test_seasonality_midterm_rows_only_in_midterm_years(md, paths):
    write_snapshot(paths, make_snapshot(NOW))
    found = texts(md.handle(" 季節性", now=NOW, paths=paths).flex)
    assert "期中選舉年的 10 月（S&P 500）" in found and market_calc.TEXT_SEASON_NOTE in found
    write_snapshot(paths, make_snapshot(NOW_2027))
    later = texts(md.handle(" 季節性", now=NOW_2027, paths=paths).flex)
    assert not any("期中選舉年" in t for t in later)


# ── 季節性：半導體（2026-10-24 第 9 輪 A14） ────────────────────────────────────────────────────────────
SEASON_SOXX = [
    "半導體 ETF（SOXX）10 月，月底收盤、價格口徑",
    "上漲比例 67%（9 年），中位數 +2.4%、平均 +1.5%",
    "半導體 ETF（SOXX）第四季（12 月底比 9 月底）",
    "上漲比例 78%（9 年），中位數 +11.1%、平均 +10.2%",
]
SEASON_SOXX_INFO = ["SOXX 樣本 2016-11 至 2025-12", "資料：Nasdaq 收盤價，已調整分割"]
SEASON_SOX = [
    "費城半導體（SOX）10 月，月底收盤、價格口徑",
    "上漲比例 63%（32 年），中位數 +3.3%、平均 +2.5%",  # 62.5：四捨五入進位（F2-16，以前是銀行家捨入的 62）
    "費城半導體（SOX）第四季（12 月底比 9 月底）",
    "上漲比例 78%（32 年），中位數 +9.4%、平均 +8.8%",
]
SEASON_SOX_MIDTERM = [
    "期中選舉年的 10 月（SOX）",
    "上漲比例 88%（8 年），中位數 +5.0%、平均 +7.1%",
    "期中選舉年的第四季（SOX）",
    "上漲比例 88%（8 年），中位數 +9.3%、平均 +14.7%",
]
SEASON_SOX_INFO = ["SOX 樣本 1994-06 至 2025-12", "資料：Nasdaq 費城半導體指數日線收盤"]
SEASON_OLD_SECTIONS_2026 = [
    "標普500（S&P 500）10 月，月均價口徑",
    "上漲比例 61%（76 年），中位數 +0.9%、平均 +1.1%",  # 60.5：同上（F2-16）
    "第四季（12 月均價比 9 月均價，S&P 500）",
    "上漲比例 75%（75 年），中位數 +3.1%、平均 +3.4%",
    "期中選舉年的 10 月（S&P 500）",
    "上漲比例 70%（19 年），中位數 +1.5%、平均 +1.9%",
    "期中選舉年的第四季（S&P 500）",
    "上漲比例 84%（19 年），中位數 +5.0%、平均 +5.2%",
    "加權指數 10 月，月底收盤口徑",
    "上漲比例 55%（27 年），中位數 +0.5%、平均 +0.8%",
    "加權指數第四季",
    "上漲比例 60%（26 年），中位數 +2.0%、平均 +2.5%",
    "期中選舉年的 10 月（加權指數）",
    "上漲比例 57%（7 年），中位數 +0.7%、平均 +0.9%",
]


def season_card(md, paths, snap, now=NOW):
    write_snapshot(paths, snap)
    reply = md.handle(" 季節性", now=now, paths=paths)
    assert reply.flex is not None and reply.flex["type"] == "bubble", reply.text
    return reply


def test_seasonality_lists_soxx_first_then_sox_then_the_older_sections(md, paths):
    reply = season_card(md, paths, make_snapshot(NOW))
    body = texts(reply.flex["body"])
    assert body == (SEASON_SOXX + ["SOXX 的期中選舉年樣本太少，不列"] + SEASON_SOXX_INFO + SEASON_SOX + SEASON_SOX_MIDTERM
                    + SEASON_SOX_INFO + SEASON_OLD_SECTIONS_2026)
    assert [n["type"] for n in reply.flex["body"]["contents"]].count("separator") == 3  # 四組之間各一條線
    footer = texts(reply.flex["footer"])
    assert footer[:4] == [market_calc.TEXT_SEASON_NOTE, market_calc.TEXT_SEASON_SEMI_NOTE,
                          market_calc.TEXT_SEASON_BASIS_NOTE, md.TEXT_SEASON_SOURCE]
    assert "Nasdaq" in md.TEXT_SEASON_SOURCE and "datahub" in md.TEXT_SEASON_SOURCE and "FinMind" in md.TEXT_SEASON_SOURCE
    assert reply.alt_text == "盤勢：季節性（10 月）｜歷史統計"


def test_seasonality_semiconductor_lines_name_the_subject_basis_range_and_sample_size(md, paths):
    reply = season_card(md, paths, make_snapshot(NOW))
    body = texts(reply.flex["body"])
    assert "SOXX" in body[0] and "月底收盤、價格口徑" in body[0] and "9 年" in body[1]  # 標的、口徑、樣本數
    assert "SOX" in body[body.index(SEASON_SOX[0])] and "32 年" in body[body.index(SEASON_SOX[0]) + 1]
    assert "樣本 2016-11 至 2025-12" in "\n".join(body) and "樣本 1994-06 至 2025-12" in "\n".join(body)  # 樣本起迄
    footer = "\n".join(texts(reply.flex["footer"]))
    assert "換過" in footer and "NYSE 半導體指數" in footer  # SOXX 追蹤的指數換過
    assert "推定" in footer and "配息" in footer  # 價格或總報酬口徑沒有來源文件：卡上寫明是推定
    assert not any("勝率" in line for line in body)  # 卡上的統計只寫「上漲比例」


@pytest.mark.parametrize("now", ALL_NOWS, ids=["2026-10-17", "2027-01-15", "2028-01-03"])
@pytest.mark.parametrize("variant", ["normal", "worst", "stale", "half_never"])
def test_the_seasonality_card_never_says_win_rate_anywhere(md, paths, variant, now):
    """Andrew 的市場規則：統計只寫「上漲比例」。標題、內文、註記（TEXT_SEASON_NOTE 原本有一句「…當 SOXX 的勝率」）、
    altText 與按鈕字，一個「勝率」都不能有。"""
    write_snapshot(paths, _variants(now)[variant])
    reply = md.handle(" 季節性", now=now, paths=paths)
    assert reply.flex is not None
    strings = reply_strings(reply)
    assert any("上漲比例" in value for value in strings) or variant in ("stale", "half_never")
    assert not [value for value in strings if "勝率" in value]


def test_seasonality_skips_midterm_sections_outside_midterm_years(md, paths):
    reply = season_card(md, paths, make_snapshot(NOW_2027), now=NOW_2027)  # 2027-01：1 月
    body = texts(reply.flex["body"])
    assert body[0] == "半導體 ETF（SOXX）1 月，月底收盤、價格口徑" and body[2] == SEASON_SOXX[2]
    assert body[4:6] == SEASON_SOXX_INFO  # 非期中選舉年：SOXX 沒有那句「樣本太少」
    assert "費城半導體（SOX）1 月，月底收盤、價格口徑" in body and SEASON_SOX[2] in body
    assert not any("期中選舉年" in line for line in texts(reply.flex))  # 標題、註記、卡上每個字
    assert not set(SEASON_SOX_MIDTERM[1::2]) & set(body)


def test_soxx_midterm_sections_appear_once_the_sample_is_big_enough(md, paths):
    snap = make_snapshot(NOW)
    values = snap["rows"]["season.now"]["values"]
    values["soxx_midterm"], values["soxx_midterm_q4"] = _stats(60.0, 1.0, 2.0, 5), _stats(80.0, 4.0, 5.0, 5)
    body = texts(season_card(md, paths, snap).flex["body"])
    assert "期中選舉年的 10 月（SOXX）" in body and "期中選舉年的第四季（SOXX）" in body
    assert "上漲比例 60%（5 年），中位數 +1.0%、平均 +2.0%" in body
    assert "SOXX 的期中選舉年樣本太少，不列" not in body
    assert body.index("期中選舉年的 10 月（SOXX）") < body.index(SEASON_SOX[0])  # 還是 SOXX 在前


@pytest.mark.parametrize("drop", ["keys", "pending", "garbage"])
def test_old_or_pending_semiconductor_rows_say_insufficient_and_nothing_breaks(md, paths, drop):
    snap = make_snapshot(NOW)
    values = snap["rows"]["season.now"]["values"]
    if drop == "keys":  # 舊 job 寫的快照：沒有半導體的鍵
        for key in [k for k in values if k.startswith(("soxx", "sox"))]:
            del values[key]
    elif drop == "pending":
        values.update(soxx_pending=True, sox_pending=True, soxx=None, sox=None)
    else:
        values.update(soxx_pending="yes", sox_pending=3, soxx="x", sox=[1], soxx_q4={"up_pct": "a"}, sox_q4=None)
    reply = season_card(md, paths, snap)
    body = texts(reply.flex["body"])
    assert body[0] == "半導體 ETF（SOXX）10 月，月底收盤、價格口徑" and body[1] == md.TEXT_INSUFFICIENT
    assert "費城半導體（SOX）10 月，月底收盤、價格口徑" in body
    assert body[body.index("費城半導體（SOX）10 月，月底收盤、價格口徑") + 1] == md.TEXT_INSUFFICIENT
    assert body[-len(SEASON_OLD_SECTIONS_2026):] == SEASON_OLD_SECTIONS_2026  # 標普、加權不受影響
    assert not any(line in body for line in ("SOXX 樣本 2016-11 至 2025-12", "SOX 樣本 1994-06 至 2025-12"))
    assert md.status_summary(now=NOW, paths=paths).get("render_error") is None


@pytest.mark.parametrize(
    "bad_range",
    [None, "2016-11", {}, {"first": "2016-11"}, {"first": "2016-13", "last": "2025-12"}, {"first": 5, "last": "x"},
     {"first": "<b>2016-11", "last": "2025-12"}, {"first": "2016-11", "last": "2025-12 並且 @all"}, [1, 2]],
)
def test_a_bad_sample_range_prints_missing_instead_of_the_text(md, paths, bad_range):
    snap = make_snapshot(NOW)
    snap["rows"]["season.now"]["values"]["soxx_range"] = bad_range
    reply = season_card(md, paths, snap)
    body = texts(reply.flex["body"])
    assert "SOXX 樣本 暫缺" in body and "SOX 樣本 1994-06 至 2025-12" in body
    assert not any("@" in line or "<b>" in line for line in texts(reply.flex))


def test_worst_case_semiconductor_lines_stay_within_24_wide_and_the_bubble_within_6_5_kb(md, paths):
    snap = make_snapshot(NOW)
    values = snap["rows"]["season.now"]["values"]
    extreme = _stats(100.0, -99.9, -99.9, 999)
    for key in ("soxx", "soxx_q4", "soxx_midterm", "soxx_midterm_q4", "sox", "sox_q4", "sox_midterm", "sox_midterm_q4",
                "sp500", "sp500_q4", "sp500_midterm", "sp500_midterm_q4", "taiex", "taiex_q4", "taiex_midterm"):
        values[key] = dict(extreme)
    snap["rows"]["season.now"]["values"]["month"] = 12
    snap["rows"]["season.now"]["stale_after"] = NOW + 10 * 86400
    reply = season_card(md, paths, snap)
    for line in texts(reply.flex["body"]):
        assert md.row_width(line) <= 24, (md.row_width(line), line)
    assert "上漲比例 100%（999 年），中位數 -99.9%" in texts(reply.flex["body"])  # 放不下時先拿掉平均
    assert size(reply.flex) <= 6656, size(reply.flex)
    paged = card_pages.with_page_button(reply.flex, LONG_PAGE_URL)
    assert card_pages.fits_line_limits(paged)


def test_season_stat_line_drops_the_mean_then_the_median_only_when_it_would_not_fit(md):
    for stats, expected in (
        (_stats(88.0, 9.27, 14.71, 8), "上漲比例 88%（8 年），中位數 +9.3%、平均 +14.7%"),  # 現有最寬的真實值（23.5）：全留
        (_stats(0.0, -9.99, -9.99, 32), "上漲比例 0%（32 年），中位數 -10.0%、平均 -10.0%"),  # 剛好 24.0
        (_stats(100.0, 14.71, 12.34, 32), "上漲比例 100%（32 年），中位數 +14.7%"),  # 25.0 → 拿掉平均
        (_stats(100.0, -99.9, -99.9, 999), "上漲比例 100%（999 年），中位數 -99.9%"),
        (_stats(50.0, None, 1.0, 5), "上漲比例 50%（5 年）"),  # 缺值：只寫比例（原行為）
    ):
        assert md._season_stat(stats) == expected and md.row_width(expected) <= 24, expected
    assert md.row_width("上漲比例 100%（32 年），中位數 +14.7%、平均 +12.3%") > 24


# ── 新聞：半導體（2026-10-24 第 9 輪 A17） ────────────────────────────────────────────────────────────
def news_first_bubble(md, paths, snap, now=NOW, **write):
    write_snapshot(paths, snap, **write)
    reply = md.handle(" 新聞", now=now, paths=paths)
    assert reply.flex is not None and reply.flex["type"] == "carousel", reply.text
    return reply, bubbles(reply.flex)[0]


def news_section(body, label, next_label):
    """body（文字清單）裡 label 到 next_label 之間的標題（每則兩個字串：標題、來源｜時間）。"""
    lines = body[body.index(label) + 1: body.index(next_label)]
    return lines[0::2], lines


def test_news_detail_lists_semiconductor_news_between_us_and_tw_in_the_first_bubble(md, paths):
    reply, first = news_first_bubble(md, paths, make_snapshot(NOW))
    assert texts(first["body"]) == [
        "美股新聞", GOOD_TITLES[0], "經濟日報｜10/17 15:30",
        "半導體新聞", SEMI_TITLE, "工商時報｜10/17 15:30",
        "台股新聞", GOOD_TITLES[1], "中央社｜10/17 15:30",
    ]
    assert [node["type"] for node in first["body"]["contents"]].count("separator") == 2
    assert texts(first["footer"])[:2] == [md.TEXT_NEWS_NOTE, md.TEXT_TAIPEI_TIME]
    assert len(bubbles(reply.flex)) == 3 and reply.render_errors == ()  # 還是 3 張，沒有多一張
    assert reply.alt_text == "盤勢：新聞與宏觀｜美股 1 則、半導體 1 則、台股 1 則、伊朗 1 則、川普 1 則"
    assert md.NEWS_PER_KIND == 5 and "news.semi" in md.READ_ROWS and "news.semi" in market_calc.ROW_IDS


def test_semiconductor_news_skips_what_the_us_section_already_lists_but_not_what_it_left_out(md, paths):
    snap = make_snapshot(NOW)
    us = [{"title": f"美股第 {i} 則標題", "source": "經濟日報", "published_utc": NOW - 3600 - i} for i in range(1, 8)]
    snap["rows"]["news.us"]["values"]["items"] = us  # 美股段只列前 5 則
    snap["rows"]["news.semi"]["values"]["items"] = [
        us[0],  # 美股第 1 則（已列）：不重複
        dict(us[2], source="另一家報社"),  # 同標題、不同來源：一樣是同一則
        us[5],  # 美股第 6 則（美股段沒列出來）：不算重複，半導體段照列
        {"title": SEMI_TITLE, "source": SEMI_SOURCE, "published_utc": NOW - 600},
    ]
    _, first = news_first_bubble(md, paths, snap)
    body = texts(first["body"])
    assert body.count(us[0]["title"]) == 1 and body.count(us[2]["title"]) == 1
    titles, _ = news_section(body, "半導體新聞", "台股新聞")
    assert titles == [us[5]["title"], SEMI_TITLE]
    assert body.index(us[0]["title"]) < body.index("半導體新聞")  # 重複的那則留在美股段
    # 美股段本身不受影響：同一個標題出現兩次（兩家報社）都照列
    snap["rows"]["news.us"]["values"]["items"] = [us[0], dict(us[0], source="另一家報社")]
    snap["rows"]["news.semi"]["values"]["items"] = [{"title": SEMI_TITLE, "source": SEMI_SOURCE, "published_utc": NOW - 600}]
    _, first = news_first_bubble(md, paths, snap)
    assert texts(first["body"]).count(us[0]["title"]) == 2


def test_when_every_semiconductor_title_is_already_listed_the_card_says_so(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["news.semi"]["values"]["items"] = list(snap["rows"]["news.us"]["values"]["items"])
    _, first = news_first_bubble(md, paths, snap)
    body = texts(first["body"])
    assert body[body.index("半導體新聞") + 1] == md.TEXT_NEWS_SAME_AS_US == "與上面的美股新聞相同，不重複列"
    assert "36 小時內沒有列入的標題" not in body and body.count(GOOD_TITLES[0]) == 1
    snap["rows"]["news.semi"]["values"]["items"] = []  # 真的沒有：還是原本那句
    _, first = news_first_bubble(md, paths, snap)
    body = texts(first["body"])
    assert body[body.index("半導體新聞") + 1] == "36 小時內沒有列入的標題"


def test_semiconductor_news_goes_through_the_same_external_string_gates(md, paths):
    snap = make_snapshot(NOW)
    items = snap["rows"]["news.semi"]["values"]["items"]
    items[:] = [{"title": t, "source": "中央社", "published_utc": NOW - 60} for t in DROP_TITLES] + items
    reply, first = news_first_bubble(md, paths, snap, ensure_ascii=True)  # 孤立代理字元只能用 ensure_ascii 寫進 JSON
    assert_main_gate(reply)
    body = texts(first["body"])
    assert SEMI_TITLE in body and GOOD_TITLES[0] in body and GOOD_TITLES[1] in body
    assert "另有 6 則格式不符，沒有列出" in texts(first["footer"])
    for value in reply_strings(reply):
        assert "@" not in value and "＠" not in value and "all" not in value.replace("　", " ").split(), value
        assert not any(unicodedata.category(ch) in {"Cf", "Cs", "Zl", "Zp", "Cc"} for ch in value if ch != "\n"), value
        assert "媽媽" not in value and "大漲" not in value and "零寬" not in value and "孤立" not in value
        assert "9月出口" not in value
    assert md.status_summary(now=NOW, paths=paths)["external_dropped"] >= 6


def test_semiconductor_news_that_is_old_undated_or_in_the_future_is_not_listed(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["news.semi"]["values"]["items"] = [
        {"title": "很舊的半導體標題", "source": "中央社", "published_utc": NOW - 40 * 3600},
        {"title": "沒有時間的半導體標題", "source": "中央社", "published_utc": None},
        {"title": "未來的半導體標題", "source": "中央社", "published_utc": NOW + 3600},
    ]
    _, first = news_first_bubble(md, paths, snap)
    body = texts(first["body"])
    assert not {"很舊的半導體標題", "沒有時間的半導體標題", "未來的半導體標題"} & set(body)
    assert body[body.index("半導體新聞") + 1] == "36 小時內沒有列入的標題"


@pytest.mark.parametrize("how", ["missing", "never", "stale"])
def test_an_old_or_unfinished_snapshot_still_renders_the_news_card(md, paths, how):
    snap = make_snapshot(NOW)
    if how == "missing":  # 舊 job 寫的快照：沒有 news.semi 這一列
        del snap["rows"]["news.semi"]
    elif how == "never":
        snap["rows"]["news.semi"] = _never()
    else:
        snap["rows"]["news.semi"]["stale_after"] = NOW - 1
    reply, first = news_first_bubble(md, paths, snap)
    body = texts(first["body"])
    gap = body[body.index("半導體新聞") + 1]
    assert gap.startswith("過期") if how == "stale" else gap == md.TEXT_MISSING
    assert GOOD_TITLES[0] in body and GOOD_TITLES[1] in body and SEMI_TITLE not in body
    assert reply.render_errors == () and "半導體 0 則" in reply.alt_text
    assert_main_gate(reply)


def test_the_worst_case_news_bubbles_stay_within_6_5_kb(md, paths):
    write_snapshot(paths, worst_snapshot(NOW))
    reply = md.handle(" 新聞", now=NOW, paths=paths)
    assert reply.flex is not None and reply.render_errors == ()
    sizes = [size(bubble) for bubble in bubbles(reply.flex)]
    assert all(each <= 6656 for each in sizes), sizes
    assert size(reply.flex) <= 28 * 1024
    assert len(texts(bubbles(reply.flex)[0]["body"])) == 3 * (1 + 2 * md.NEWS_PER_KIND)  # 三段各 5 則
    assert card_pages.fits_line_limits(card_pages.with_page_button(reply.flex, LONG_PAGE_URL))


def test_election_phases(md, paths):
    def at(moment_et, state="upcoming"):
        now = moment_et.timestamp()
        snap = make_snapshot(now)
        snap["rows"]["election"]["values"]["state"] = state
        write_snapshot(paths, snap)
        return md.handle(" 選舉", now=now, paths=paths), md.handle("", now=now, paths=paths)

    def macro_rows(reply):
        return texts(summary_bubble(md, reply.flex, "宏觀與事件")["body"])

    detail, _summary = at(datetime(2026, 10, 20, 12, tzinfo=ET))
    assert "投票日 11/3（美東），還有 14 天" in texts(detail.flex)
    assert "成交時間未知" in "\n".join(texts(detail.flex))
    _detail, summary = at(datetime(2026, 11, 3, 12, tzinfo=ET))
    assert "期中選舉：美東 11/3 投票中" in macro_rows(summary)
    _detail, summary = at(datetime(2026, 11, 6, 12, tzinfo=ET), state="waiting")
    assert "期中選舉：已投票，等待結果" in macro_rows(summary)
    _detail, summary = at(datetime(2026, 11, 8, 12, tzinfo=ET), state="result")
    assert "期中選舉：已投票，結果看 /盤勢 選舉" in macro_rows(summary)
    detail, summary = at(datetime(2026, 11, 20, 12, tzinfo=ET), state="ended")
    assert detail.text == "盤勢：本屆期中選舉已結束"
    assert len(summary_bubble(md, summary.flex, "宏觀與事件")["body"]["contents"]) == 5
    detail, _summary = at(datetime(2027, 2, 2, 12, tzinfo=ET), state="waiting")
    assert detail.text == "盤勢：本屆期中選舉已結束"


# ── 每日維護 ─────────────────────────────────────────────────────────────────
def test_status_summary_has_only_enumerated_values(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["us.etf.VOO"]["stale_after"] = NOW - 1
    snap["rows"]["tw.ad"] = _never()
    snap["rows"]["evil.row"] = _never()
    snap["rows"]["news.us"]["values"]["items"].append({"title": "x @all y", "source": "中央社", "published_utc": NOW - 60})
    snap["charts"]["SPY"]["stale_after"] = NOW - 1
    snap["generated_at"] = NOW - 3 * 3600
    write_snapshot(paths, snap)
    # 照 jobs/market_snapshot.py 的 compose_outcome：tasks.<名稱>.{kind, fail_streak, …}
    outcome = {"schema": 1, "status": "degraded", "code": "ok", "collect": {"status": "ok"},
               "publish": {"status": "failed", "kind": "too_big"},
               "tasks": {"us.hist": {"fail_streak": 4, "kind": "rate_limited", "error": "secret body"},
                         "news.us": {"fail_streak": 0}, "bad name!": {"fail_streak": 9}},
               "last_collect_at": NOW - 900, "last_publish_at": NOW - 3 * 3600, "lock_held_s": 12.5,
               "calendar_mismatch": ["previous_trade_date", "free text with spaces"]}
    paths.outcome.write_bytes(json.dumps(outcome).encode("utf-8"))
    os.chmod(paths.outcome, 0o600)
    summary = md.status_summary(now=NOW, paths=paths)
    blob = json.dumps(summary, ensure_ascii=False)
    assert summary["snapshot"] == "ok" and summary["disabled"] is False
    assert summary["publish_late"] is True and summary["publish_age_s"] == 3 * 3600
    assert summary["rows_stale"] == ["us.etf.VOO"] and summary["rows_missing"] == ["tw.ad"]
    assert summary["rows_unexpected"] == ["evil.row"] and summary["charts_stale"] == ["SPY"]
    assert summary["external_dropped"] >= 1
    assert summary["outcome"]["failing"] == [{"name": "us.hist", "fail_streak": 4, "kind": "rate_limited"}]
    assert (summary["outcome"]["status"], summary["outcome"]["publish"]) == ("degraded", "failed")
    assert summary["outcome"]["calendar_mismatch"] == ["previous_trade_date"]
    for leaked in ("secret body", "@all", "free text", GOOD_TITLES[0], "bad name"):
        assert leaked not in blob


def test_status_summary_without_files(md, paths):
    summary = md.status_summary(now=NOW, paths=paths)
    assert summary["snapshot"] == "missing" and summary["outcome"] == {"state": "missing"}
    paths.snapshot.write_bytes(b"{")
    os.chmod(paths.snapshot, 0o600)
    assert md.status_summary(now=NOW, paths=paths)["snapshot"] == "invalid"


# ── main 的真接線（market_dashboard 用真的，送出用假的） ─────────────────────────────
def test_main_routes_the_real_replies(md, paths, monkeypatch):
    import time
    from types import SimpleNamespace
    from unittest.mock import patch

    import main

    now = time.time()  # main 不傳 now：快照照真的時間做
    snap = make_snapshot(now)
    write_snapshot(paths, snap)
    write_charts(paths, snap)
    monkeypatch.setattr(market_paths.Paths, "default", classmethod(lambda cls: paths))
    event = SimpleNamespace(reply_token="TOKEN_MARKET")
    with patch.object(main, "_reply_command_card") as card, patch.object(main, "_reply_market_chart") as chart:
        main._reply_market(event, "G_TEST", "/盤勢", "")
        main._reply_market(event, "G_TEST", "/盤勢 QQQ 圖", " QQQ 圖")
        main._reply_market(event, "G_TEST", "/盤勢 abc", " abc")
    first, third = card.call_args_list
    assert first.kwargs["page"] is True and first.kwargs["card"]["type"] == "carousel"
    assert first.kwargs["alt_text"].startswith("盤勢：") and first.kwargs["fallback_text"] == md.TEXT_CARD_FAILED
    assert third.kwargs["card"] is None and third.kwargs["reply_text"] == md.TEXT_USAGE
    args, kwargs = chart.call_args
    assert args[2] == snap["charts"]["QQQ"]["files"] and args[3].startswith("盤勢圖：QQQ")
    assert first.kwargs["alt_text"].startswith(main._CARD_PAGE_ALT_PREFIXES)  # 不然 main 會悄悄不加 🔍


# ── 模組本身 ─────────────────────────────────────────────────────────────────
# bot 端「不連網、不叫模型、不開資料庫、不起子行程」（2026-10-24 審查 r9 s4-F4）：整個 /盤勢 bot 端只讀 job 寫好的快照。
# 比的是最上層模組名（http.client → http），所以 html、market_calc 這類名字不會被誤殺。
NO_NETWORK_DB_OR_MODEL = frozenset({
    "urllib", "http", "socket", "ssl", "subprocess", "sqlite3", "market_http", "market_store", "safe_fetch",
    "anthropic", "openai", "google",
})


def top_level_imports(source: str) -> set[str]:
    """一份原始碼 import 的最上層模組名（含函式裡才 import 的，例如 market_calc 的 numpy）。"""
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def test_module_imports_stay_light():
    imported = top_level_imports((HERE / "market_dashboard.py").read_text(encoding="utf-8"))
    banned = {"numpy", "pandas", "matplotlib", "main", "card_pages", "stock_picks", "fcn_eval", "requests"}
    assert not imported & banned
    assert not imported & NO_NETWORK_DB_OR_MODEL
    assert not any(name.startswith("market_sources") for name in imported)
    assert {"market_calc", "market_paths", "output_validator"} <= imported
    # bot 端另外兩個模組（market_calc 只有算 GEX 時才延遲載入 numpy，所以那一項不在這裡禁）
    for name in ("market_calc.py", "market_paths.py"):
        found = top_level_imports((HERE / name).read_text(encoding="utf-8"))
        assert not found & NO_NETWORK_DB_OR_MODEL, (name, found & NO_NETWORK_DB_OR_MODEL)
        assert not any(module.startswith("market_sources") for module in found), name


def test_the_import_guard_catches_network_db_and_model_modules_and_nothing_legitimate():
    for statement in (
        "import urllib.request", "from urllib import parse", "import http.client", "from http import client",
        "import socket", "import ssl", "import subprocess", "import sqlite3", "import market_http",
        "from market_store import Store", "import safe_fetch", "import anthropic", "from openai import OpenAI",
        "from google import genai", "import google.generativeai",
        "def late():\n    import socket\n",  # 函式裡才 import 的也抓得到
    ):
        assert top_level_imports(statement) & NO_NETWORK_DB_OR_MODEL, statement
    for statement in (
        "import html", "import json", "import threading", "from datetime import date", "from zoneinfo import ZoneInfo",
        "import market_calc", "import market_paths", "import output_validator", "import unicodedata",
        "from . import sibling",
    ):
        assert not top_level_imports(statement) & NO_NETWORK_DB_OR_MODEL, statement


def test_handle_never_raises(md, paths, monkeypatch):
    write_snapshot(paths, make_snapshot(NOW))
    monkeypatch.setattr(md, "_summary_reply", lambda ctx: 1 / 0)
    assert md.handle("", now=NOW, paths=paths).text == "盤勢：資料準備中，請過幾分鐘再按一次"
    monkeypatch.setattr(md.market_calc, "clean_external", lambda text, max_len: 1 / 0)
    reply = md.handle(" 新聞", now=NOW, paths=paths)
    assert reply.flex is not None and GOOD_TITLES[0] not in texts(reply.flex)  # 清洗壞掉：丟字、卡照送


# ── 2026-10-24 第 9 輪批 1（骨架）：逐泡泡隔離 ─────────────────────────────────────────────
LEAK = "secret detail that must never be reported"


def raising(name: str):
    """會丟例外的 builder 替身；名稱照被換掉的那個（render_errors 記的是 builder 名稱與例外型別，不記訊息）。"""

    def boom(*_args, **_kwargs):
        raise ZeroDivisionError(LEAK)

    boom.__name__ = name
    return boom


def assert_failed_bubble(md, bubble, title, *, swipe=None):
    assert bubble["header"]["contents"][0]["text"] == title
    assert texts(bubble["body"]) == [md.TEXT_BUBBLE_FAILED]
    assert DISCLAIMER in texts(bubble["footer"]) and not actions(bubble)  # 固定短卡：沒有按鈕
    assert (swipe in texts(bubble["header"])) if swipe else ("往左滑" not in " ".join(texts(bubble["header"])))
    assert md.card_bytes(bubble) < 2000


def test_one_summary_card_failing_leaves_the_other_cards(md, paths, caplog):
    cards = md.SUMMARY_CARDS
    write_snapshot(paths, make_snapshot(NOW))
    good = md.handle("", now=NOW, paths=paths)
    assert good.render_errors == () and len(good.flex["contents"]) == len(cards)
    for position, (card_name, builder) in enumerate(cards):  # 每一張輪流壞掉一次
        caplog.clear()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(md, builder, raising(builder))
            with caplog.at_level("ERROR", logger="market_dashboard"):
                reply = md.handle("", now=NOW, paths=paths)
            summary = md.status_summary(now=NOW, paths=paths)
        assert reply.flex is not None and reply.flex["type"] == "carousel", builder
        assert len(reply.flex["contents"]) == len(cards), builder
        assert reply.render_errors == (f"{builder}:ZeroDivisionError",), builder
        assert reply.alt_text == good.alt_text and reply.fallback_text == good.fallback_text
        for index, (now_bubble, was) in enumerate(zip(reply.flex["contents"], good.flex["contents"])):
            if index == position:
                assert_failed_bubble(md, now_bubble, f"{card_name}{md._page(index + 1)}",
                                     swipe=md.TEXT_SWIPE if index == 0 else None)
            else:
                assert now_bubble == was, (builder, index)  # 其他張一個字都不變
        assert LEAK not in json.dumps(reply.flex, ensure_ascii=False) + repr(reply.render_errors)
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert f"builder={builder} error_type=ZeroDivisionError" in logged and LEAK not in logged  # log 只記名稱與型別
        assert_main_gate(reply)
        assert summary["render_error"] == f"bubble:{builder}:ZeroDivisionError"
        assert LEAK not in json.dumps(summary, ensure_ascii=False)


def test_every_summary_card_failing_still_answers_with_short_cards(md, paths, monkeypatch):
    write_snapshot(paths, make_snapshot(NOW))
    for _name, builder in md.SUMMARY_CARDS:
        monkeypatch.setattr(md, builder, raising(builder))
    reply = md.handle("", now=NOW, paths=paths)
    assert [b["header"]["contents"][0]["text"] for b in reply.flex["contents"]] == [
        f"{name}{md._page(i)}" for i, (name, _builder) in enumerate(md.SUMMARY_CARDS, 1)]
    assert reply.render_errors == tuple(f"{builder}:ZeroDivisionError" for _name, builder in md.SUMMARY_CARDS)
    assert_main_gate(reply)


def test_a_broken_alt_text_no_longer_takes_the_whole_reply_down(md, paths, monkeypatch):
    """2026-10-28 批 1 審查 F2-11：altText 也各自隔離。以前它出錯是整個 /盤勢 回「資料準備中」（把程式出錯說成資料還沒好）、
    status_summary 記成 unexpected；現在輪播照送，altText 退回固定句，錯誤進 render_errors（只有名稱與例外型別）。"""
    write_snapshot(paths, make_snapshot(NOW))
    good = md.handle("", now=NOW, paths=paths)
    monkeypatch.setattr(md, "_summary_alt", lambda ctx: 1 / 0)
    reply = md.handle("", now=NOW, paths=paths)
    assert reply.flex == good.flex and reply.text is None  # 五張卡一個字都沒變
    assert reply.alt_text == md.TEXT_SUMMARY_ALT_FALLBACK == market_calc.TEXT_SUMMARY_ALT_FALLBACK
    assert reply.alt_text.startswith(md.ALT_PREFIX) and reply.alt_text in market_calc.FIXED_TEXTS
    assert reply.render_errors == ("_summary_alt:ZeroDivisionError",)
    assert_main_gate(reply)
    summary = md.status_summary(now=NOW, paths=paths)
    assert summary["render_error"] == "bubble:_summary_alt:ZeroDivisionError"


def test_bubble_errors_over_budget_are_still_reported(md, paths, monkeypatch):
    """整組太大、改回固定句時，哪一張出錯的紀錄也不能丟。"""
    write_snapshot(paths, make_snapshot(NOW))
    monkeypatch.setattr(md, "CARD_MAX_BYTES", 100)
    builder = md.SUMMARY_CARDS[1][1]
    monkeypatch.setattr(md, builder, raising(builder))
    reply = md.handle("", now=NOW, paths=paths)
    assert reply.flex is None and reply.text == md.TEXT_CARD_FAILED
    assert reply.render_errors == (f"{builder}:ZeroDivisionError",)


def only_for(original, row_id):
    """只在讀某一列時丟例外（其他列照舊）：讓同一個函式在輪播的一個泡泡壞掉、另一個泡泡正常。"""

    def wrapper(ctx, current, *args, **kwargs):
        if current == row_id:
            raise ZeroDivisionError(LEAK)
        return original(ctx, current, *args, **kwargs)

    return wrapper


@pytest.mark.parametrize("body,attr,position,title,swipe", [
    (" 期權", "_vol_rows", 0, "美股期權（1/2）", "往左滑還有 1 張"),
    (" 期權", "_tw_option_rows", 1, "台指期權與資金（2/2）", None),
    (" 新聞", "_news_rows", 0, "新聞（1/3）", "往左滑還有 2 張"),
    (" 新聞", "_trump_rows", 1, "伊朗新聞與川普原文（2/3）", None),
    (" 新聞", "_macro_rows", 2, "宏觀與事件（3/3）", None),
])
def test_detail_carousels_isolate_each_bubble_too(md, paths, monkeypatch, body, attr, position, title, swipe):
    write_snapshot(paths, make_snapshot(NOW))
    good = md.handle(body, now=NOW, paths=paths)
    assert good.render_errors == () and good.flex["type"] == "carousel"
    original = getattr(md, attr)
    monkeypatch.setattr(md, attr, only_for(original, "news.us") if attr == "_news_rows" else raising(attr))
    reply = md.handle(body, now=NOW, paths=paths)
    assert reply.flex is not None and len(reply.flex["contents"]) == len(good.flex["contents"])
    assert len(reply.render_errors) == 1 and reply.render_errors[0].endswith(":ZeroDivisionError")
    for index, (now_bubble, was) in enumerate(zip(reply.flex["contents"], good.flex["contents"])):
        if index == position:
            assert_failed_bubble(md, now_bubble, title, swipe=swipe)
        else:
            assert now_bubble == was, index
    assert reply.alt_text.startswith("盤勢：") and LEAK not in json.dumps(reply.flex, ensure_ascii=False)
    if body == " 新聞":  # altText 的各區則數少了一區就不寫則數，不寫成 0
        assert good.alt_text == "盤勢：新聞與宏觀｜美股 1 則、半導體 1 則、台股 1 則、伊朗 1 則、川普 1 則"
        assert reply.alt_text == "盤勢：新聞與宏觀"
    else:
        assert reply.alt_text == good.alt_text
    assert_main_gate(reply)
    assert md.status_summary(now=NOW, paths=paths)["render_error"].startswith("bubble:")


@pytest.mark.parametrize("variant", ["normal", "worst", "stale", "half_never"])
def test_nothing_fails_on_any_snapshot_variant(md, paths, variant):
    write_snapshot(paths, _variants()[variant])
    write_charts(paths, _variants()[variant])
    for body, reply in render_all(md, paths, NOW, FLEX_BODIES + CHART_BODIES + (" abc", " 13F 0050")).items():
        assert reply.render_errors == (), (body, reply.render_errors)
        if reply.flex is not None:
            assert md.TEXT_BUBBLE_FAILED not in texts(reply.flex), body
    assert "render_error" not in md.status_summary(now=NOW, paths=paths)


def test_the_failed_bubble_sentence_is_a_clean_fixed_sentence(md):
    text = md.TEXT_BUBBLE_FAILED
    assert text and not re.search(r"\d", text) and not md.has_forbidden(text) and md.row_width(text) <= md.MAX_ROW_WIDTH
    for now in ALL_NOWS:
        result = output_validator.validate_outbound_text(text, now=datetime.fromtimestamp(now, TPE))
        assert result.ok and result.text == text, (now, result.reason)
    assert text != md.TEXT_CARD_FAILED  # 這個是單張泡泡的，TEXT_CARD_FAILED 是整組卡的


def test_a_snapshot_from_before_the_vxtlt_row_still_renders(md, paths):
    """job 還沒升級時寫的快照沒有 macro.VXTLT：bot 照常，兩張細節卡上 VXTLT 那一行寫暫缺。
    （骨架 commit 時 bot 還不讀這一列；2026-10-24 B 軌起讀，所以它在 READ_ROWS 裡。）"""
    snap = make_snapshot(NOW)
    assert "macro.VXTLT" in snap["rows"] and "macro.VXTLT" in md.READ_ROWS
    del snap["rows"]["macro.VXTLT"]
    write_snapshot(paths, snap)
    write_charts(paths, snap)
    for body, reply in render_all(md, paths, NOW, FLEX_BODIES + CHART_BODIES).items():
        assert reply.flex is not None or reply.images, (body, reply.text)
        assert reply.render_errors == (), body
    assert "TLT 選擇權波動（VXTLT）暫缺" in texts(md.handle(" 期權", now=NOW, paths=paths).flex)
    assert "TLT 選擇權波動（VXTLT）：暫缺" in texts(md.handle(" 新聞", now=NOW, paths=paths).flex)
    summary = md.status_summary(now=NOW, paths=paths)
    assert "render_error" not in summary and summary["rows_unexpected"] == [] and summary["rows_missing"] == []
    snap["rows"]["macro.VXTLT"] = _never()  # 新 job 在佔位階段寫的就是 never：算「暫缺」，不是「沒看過的列」
    write_snapshot(paths, snap)
    summary = md.status_summary(now=NOW, paths=paths)
    assert summary["rows_missing"] == ["macro.VXTLT"] and summary["rows_unexpected"] == []


# ── 2026-10-18 r6 審查：日期、代理、用字（r6b／r6c／r6d） ─────────────────────────────
def _today(now=NOW):
    return datetime.fromtimestamp(now, TPE).date()


def _summary_bodies(md, paths, snap, now=NOW):
    """美股、台股、期權與資金、宏觀與事件這四張卡各自的列（照舊的順序）；第一張「重點摘要」有自己的測試，不在這裡。"""
    write_snapshot(paths, snap)
    flex = md.handle("", now=now, paths=paths).flex
    return [texts(summary_bubble(md, flex, name)["body"]) for name, _builder in md.SUMMARY_CARDS if name != "重點摘要"]


def test_margin_line_dates_the_taiwan_part_when_it_differs_from_the_card_day(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["tw.margin"]["data_date"] = _today().isoformat()  # 台股是 10/17、卡片標題（美股）是 10/16
    assert _summary_bodies(md, paths, snap)[2][5] == "融資：台股 +12.3 億（10/17）、美股年增 +25%"
    # 太寬：改用「台股 10/17 +99,999.9 億」的寫法，日期照樣留著
    snap["rows"]["tw.margin"]["values"]["change_yi"] = 99999.9
    snap["rows"]["us.margin"]["values"]["yoy_pct"] = 1234.0
    line = _summary_bodies(md, paths, snap)[2][5]
    assert line == "融資：台股 10/17 +99,999.9 億、美股年增 +1,234%" and md.row_width(line) <= 24
    # 同一天不寫日期
    snap["rows"]["tw.margin"]["data_date"] = (_today() - timedelta(days=1)).isoformat()
    assert _summary_bodies(md, paths, snap)[2][5] == "融資：台股 +99,999.9 億、美股年增 +1,234%"


def test_flow_card_uses_the_taifex_day_when_both_us_rows_have_no_date(md, paths):
    snap = make_snapshot(NOW)
    for row_id in ("macro.VIX", "us.options.QQQ"):
        snap["rows"][row_id]["stale_after"] = NOW - 1  # 美股兩列過期：沒有資料日
    for row_id in ("tw.options", "tw.futures"):
        snap["rows"][row_id]["data_date"] = _today().isoformat()  # 期交所是 10/17 的資料
    write_snapshot(paths, snap)
    card = summary_bubble(md, md.handle("", now=NOW, paths=paths).flex, "期權與資金")
    rows = texts(card["body"])
    assert card["header"]["contents"][0]["text"] == "期權與資金（4/5）｜10/17 收盤"
    assert rows[0] == "恐慌指數（VIX）過期（資料日 10/16）"
    assert rows[3] == "台指選擇權：買權牆 54,000、賣權牆 46,000" and rows[4] == "外資台指期（大台）淨空 97,582 口"
    assert rows[5] == "融資：台股 +12.3 億（10/16）、美股年增 +25%"  # 台股融資是 10/16，和標題（10/17）不同天
    # 美股兩列沒過期時照舊：標題是美股的日期
    snap["rows"]["macro.VIX"]["stale_after"] = NOW + 3600
    assert _summary_bodies(md, paths, snap)[2][3] == "台指選擇權：10/17 買權牆 54,000、賣權牆 46,000"


# ── 2026-10-24 第 9 輪批 1 B 軌：期權牆一檔一列、牆距的基準、財報行、CFTC 註腳、VXTLT ────────────────────────────
# （計畫 A12、A15、A16；審查 r9 seat1 I1、seat3 F9／F11／F12、seat4 F13、seat2 N9／N11）
def _flow_bubble(md, paths, snap, now=NOW) -> dict:
    write_snapshot(paths, snap)
    contents = md.handle("", now=now, paths=paths).flex["contents"]
    return next(b for b in contents if b["header"]["contents"][0]["text"].startswith("期權與資金"))


def _set_options(snap, symbol, **values) -> None:
    snap["rows"][f"us.options.{symbol}"]["values"].update(values)


def _wall_rows(md, paths, snap, now=NOW) -> tuple[str, str]:
    body = texts(_flow_bubble(md, paths, snap, now)["body"])
    assert len(body) == 6 and body[0].startswith("恐慌指數（VIX）"), body  # 兩列併成一列、加 SOXX 一列：還是六列
    return body[1], body[2]


def test_wall_rows_are_one_row_per_symbol_with_tickers_as_in_the_plan(md, paths):
    snap = make_snapshot(NOW)
    _set_options(snap, "QQQ", call_wall=760.0, put_wall=700.0, call_wall_dist_pct=7.9, put_wall_dist_pct=-0.6)
    _set_options(snap, "SOXX", call_wall=520.0, put_wall=450.0, call_wall_dist_pct=9.4, put_wall_dist_pct=-5.3)
    qqq, soxx = _wall_rows(md, paths, snap)
    assert qqq == "QQQ 買權牆 760（+7.9%）、賣權牆 700（-0.6%）" and md.row_width(qqq) == 22.0
    assert soxx == "SOXX 買權牆 520（+9.4%）、賣權牆 450（-5.3%）" and md.row_width(soxx) == 22.5
    body = texts(_flow_bubble(md, paths, snap)["body"])
    assert not any("那斯達克（QQQ）" in text for text in body)  # 舊的兩列（買權牆、賣權牆各一列）併掉了


# 每組：(說明, 兩檔都改成這組數字, 兩檔該出現的列；{s} 是代號，或 {代號: 列})。
# 變體順序：完整 → 牆價前不留空格（F2-10）→ 拿掉距離 → 只留較近的那道牆（不帶距離）；資料日比標題舊時每個變體後面接日期，最後一個才拿掉
WALL_CASES = [
    ("three digits, one-digit distances", dict(call_wall=760.0, put_wall=700.0, call_wall_dist_pct=7.9,
                                               put_wall_dist_pct=-0.6),
     "{s} 買權牆 760（+7.9%）、賣權牆 700（-0.6%）"),
    ("two-digit distances still fit", dict(call_wall=760.0, put_wall=640.0, call_wall_dist_pct=10.0,
                                           put_wall_dist_pct=-14.9),
     "{s} 買權牆 760（+10.0%）、賣權牆 640（-14.9%）"),
    # F2-10：牆價前不留空格的變體——QQQ 剛好 24.0 放得下（距離留著）；SOXX 24.5 還是放不下，拿掉距離
    ("four-digit walls and two-digit distances: QQQ keeps them in the compact variant, SOXX drops them", dict(
        call_wall=1050.0, put_wall=1010.0, call_wall_dist_pct=14.9, put_wall_dist_pct=-14.9),
     {"QQQ": "QQQ 買權牆1,050（+14.9%）、賣權牆1,010（-14.9%）", "SOXX": "SOXX 買權牆 1,050、賣權牆 1,010"}),
    ("seven-digit walls: still both walls, no distances", dict(
        call_wall=1234567.0, put_wall=1234000.0, call_wall_dist_pct=1.0, put_wall_dist_pct=-1.0),
     "{s} 買權牆 1,234,567、賣權牆 1,234,000"),
    ("the put wall is missing", dict(call_wall=760.0, put_wall=None, call_wall_dist_pct=7.9, put_wall_dist_pct=None),
     "{s} 買權牆 760（+7.9%）、賣權牆暫缺"),
    ("the call wall is missing", dict(call_wall=None, put_wall=700.0, call_wall_dist_pct=None, put_wall_dist_pct=-0.6),
     "{s} 買權牆暫缺、賣權牆 700（-0.6%）"),
    ("a distance outside the believable range is left out", dict(
        call_wall=760.0, put_wall=700.0, call_wall_dist_pct=5000.0, put_wall_dist_pct=-0.6),
     "{s} 買權牆 760、賣權牆 700（-0.6%）"),
    ("a distance that rounds to zero has no sign", dict(
        call_wall=760.0, put_wall=700.0, call_wall_dist_pct=0.04, put_wall_dist_pct=-0.04),
     "{s} 買權牆 760（0.0%）、賣權牆 700（0.0%）"),
    ("walls not enough to call", dict(wall_status="insufficient"), "{s} 買權牆、賣權牆：資料不足"),
    ("status ok but neither wall", dict(call_wall=None, put_wall=None, call_wall_dist_pct=None,
                                         put_wall_dist_pct=None), "{s} 買權牆、賣權牆：資料不足"),
    # 數字大到前兩個變體都放不下（不會真的發生，但不能出現「…」）：只留較近的那道牆
    ("absurd walls: the put wall is nearer", dict(call_wall=1e12, put_wall=1e12, call_wall_dist_pct=7.9,
                                                  put_wall_dist_pct=-0.6),
     "{s} 賣權牆 1,000,000,000,000"),
    ("absurd walls: the call wall is nearer", dict(call_wall=1e12, put_wall=1e12, call_wall_dist_pct=0.4,
                                                   put_wall_dist_pct=-6.0),
     "{s} 買權牆 1,000,000,000,000"),
    ("absurd walls: a tie goes to the call wall", dict(call_wall=1e12, put_wall=1e12, call_wall_dist_pct=5.0,
                                                       put_wall_dist_pct=-5.0),
     "{s} 買權牆 1,000,000,000,000"),
    ("absurd walls: a wall with no distance is not the nearer one", dict(
        call_wall=1e12, put_wall=1e12, call_wall_dist_pct=None, put_wall_dist_pct=-3.0),
     "{s} 賣權牆 1,000,000,000,000"),
]


@pytest.mark.parametrize("change, template", [case[1:] for case in WALL_CASES], ids=[case[0] for case in WALL_CASES])
def test_wall_row_variants_and_the_worst_values(md, paths, change, template):
    snap = make_snapshot(NOW)
    for symbol in ("QQQ", "SOXX"):
        _set_options(snap, symbol, **change)
    for symbol, row in zip(("QQQ", "SOXX"), _wall_rows(md, paths, snap)):
        assert row == (template[symbol] if isinstance(template, dict) else template.format(s=symbol)), row
        assert md.row_width(row) <= 24 and "…" not in row, row


def test_wall_row_boundary_between_the_full_and_the_compact_variant(md, paths):
    """四位數牆價＋一位數距離：QQQ 剛好 24.0 放得下完整版；SOXX 多一個字母 24.5，以前直接拿掉距離（15.5），
    現在先試牆價前不留空格的變體（23.5）——距離留得住（2026-10-28 批 1 審查 F2-10）。"""
    snap = make_snapshot(NOW)
    for symbol in ("QQQ", "SOXX"):
        _set_options(snap, symbol, call_wall=1050.0, put_wall=1010.0, call_wall_dist_pct=2.0, put_wall_dist_pct=-1.0)
    qqq, soxx = _wall_rows(md, paths, snap)
    assert qqq == "QQQ 買權牆 1,050（+2.0%）、賣權牆 1,010（-1.0%）" and md.row_width(qqq) == 24.0
    assert soxx == "SOXX 買權牆1,050（+2.0%）、賣權牆1,010（-1.0%）" and md.row_width(soxx) == 23.5


def test_wall_row_when_the_options_row_is_old_or_missing(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["us.options.QQQ"]["stale_after"] = NOW - 1  # 過期：寫資料日
    snap["rows"]["us.options.SOXX"] = _never()  # 暫缺
    assert _wall_rows(md, paths, snap) == ("QQQ 買權牆、賣權牆過期（資料日 10/16）", "SOXX 買權牆、賣權牆暫缺")
    del snap["rows"]["us.options.SOXX"]  # 舊快照沒有這一列
    assert _wall_rows(md, paths, snap)[1] == "SOXX 買權牆、賣權牆暫缺"


def test_a_wall_row_older_than_the_card_day_keeps_its_date(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["us.options.SOXX"]["data_date"] = (_today() - timedelta(days=2)).isoformat()  # 10/15；QQQ、VIX 是 10/16
    qqq, soxx = _wall_rows(md, paths, snap)
    assert qqq == "QQQ 買權牆 556（+3.0%）、賣權牆 518（-4.0%）"
    assert soxx == "SOXX 買權牆 252、賣權牆 235（10/15）"  # 日期留著、先丟距離（落後比距離重要）


def test_flow_card_buttons_are_the_detail_and_the_two_option_charts(md, paths):
    flow = _flow_bubble(md, paths, make_snapshot(NOW))
    assert [(a["label"], a["text"]) for a in actions(flow)] == [
        ("期權與資金細節", "/盤勢 期權"), ("那斯達克期權圖", "/盤勢 QQQ 圖"), ("半導體期權圖", "/盤勢 SOXX 圖")]
    assert {a["text"] for a in actions(flow)} <= set(md.SHIPPED_MARKET_TEXTS)
    contents = md.handle("", now=NOW, paths=paths).flex["contents"]
    taiwan = next(b for b in contents if b["header"]["contents"][0]["text"].startswith("台股"))
    assert ("台指期權圖", "/盤勢 台指 圖") in [(a["label"], a["text"]) for a in actions(taiwan)]  # 台指圖在台股卡還找得到


@pytest.mark.parametrize("bases, expected", [
    (("close", "close"), "牆距以收盤價計"), (("spot", "spot"), "牆距以最新價計"),
    # 基準混著：摘要卡的牆那幾列沒有印價格，所以直接點名各檔（不能寫「看各檔標示的價格」——那是期權細節卡才有的標題列）
    (("close", "spot"), "牆距：QQQ 以收盤價、SOXX 以最新價計"), (("spot", "close"), "牆距：QQQ 以最新價、SOXX 以收盤價計")])
def test_flow_card_footer_names_the_basis_of_the_wall_distances(md, paths, bases, expected):
    snap = make_snapshot(NOW)
    snap["rows"]["us.options.QQQ"]["values"] = _options(540.0, basis=bases[0])
    snap["rows"]["us.options.SOXX"]["values"] = _options(245.0, basis=bases[1])
    flow = _flow_bubble(md, paths, snap)
    footer = texts(flow["footer"])
    assert expected in footer and [t for t in footer if t.startswith("牆距")] == [expected]  # 只有一句
    assert not any(" 收盤 " in t or "最新價 " in t for t in texts(flow["body"]))  # 摘要卡的牆那幾列確實不印價格


def test_the_basis_sentences_are_the_shared_fixed_ones(md):
    assert md.TEXT_WALL_DIST_CLOSE == market_calc.TEXT_WALL_DIST_CLOSE == "牆距以收盤價計"
    assert md.TEXT_WALL_DIST_SPOT == market_calc.TEXT_WALL_DIST_SPOT == "牆距以最新價計"
    assert md.TEXT_WALL_DIST_MIXED == market_calc.TEXT_WALL_DIST_MIXED
    assert md.TEXT_WALL_DIST_NAMED == market_calc.TEXT_WALL_DIST_NAMED == "牆距：{pairs}計"
    assert md.TEXT_CFTC_NO_SEMI == market_calc.TEXT_CFTC_NO_SEMI == "CFTC 沒有半導體期貨，期貨部位只看標普與那斯達克"
    for name in ("TEXT_EARNINGS_HEAD", "TEXT_EARNINGS_ESTIMATED", "TEXT_EARNINGS_OTHERS", "TEXT_EARNINGS_NONE"):
        assert getattr(md, name) == getattr(market_calc, name), name  # 同一份，不各存一份


# 不能信的收盤（新 bot 讀舊快照、或欄位被改過）一律當現價：印的價格和註記都要是「最新價」
UNTRUSTED_BASIS = [
    None,  # 舊快照：兩個欄位都沒有
    {"dist_basis": "close", "ref_close": None},
    {"dist_basis": "close", "ref_close": "534.6"},
    {"dist_basis": "close", "ref_close": True},
    {"dist_basis": "close", "ref_close": 0},
    {"dist_basis": "close", "ref_close": -5.0},
    {"dist_basis": "close", "ref_close": float("nan")},
    {"dist_basis": "weird", "ref_close": 534.6},
    {"dist_basis": 5, "ref_close": 534.6},
    {"dist_basis": None, "ref_close": 534.6},
    {"dist_basis": "spot", "ref_close": 534.6},
]


def _untrusted(snap, damage) -> None:
    for symbol in ("QQQ", "SPY", "SOXX"):
        values = snap["rows"][f"us.options.{symbol}"]["values"]
        if damage is None:
            values.pop("ref_close"), values.pop("dist_basis")
        else:
            values.update(damage)


@pytest.mark.parametrize("damage", UNTRUSTED_BASIS)
def test_a_close_that_cannot_be_trusted_counts_as_the_latest_price(md, paths, damage):
    snap = make_snapshot(NOW)
    _untrusted(snap, damage)  # NaN 的那組：json 寫成 NaN，bot 的 parse_constant 把它讀成 null
    footer = texts(_flow_bubble(md, paths, snap)["footer"])
    assert md.TEXT_WALL_DIST_SPOT in footer and md.TEXT_WALL_DIST_CLOSE not in footer
    options = md.handle(" 期權", now=NOW, paths=paths)
    assert options.render_errors == ()
    found = texts(options.flex)
    for line in ("那斯達克100（QQQ） 最新價 540｜資料 10/16", "標普500（SPY） 最新價 666｜資料 10/16",
                 "半導體（SOXX） 最新價 245｜資料 10/16"):
        assert line in found, line
    assert not any(" 收盤 " in t for t in found)
    assert md.TEXT_WALL_DIST_SPOT in texts(options.flex["contents"][0]["footer"])
    assert md.TEXT_WALL_DIST_CLOSE not in texts(options.flex["contents"][0]["footer"])


def test_option_detail_prints_the_price_the_distances_use_and_says_so(md, paths):
    snap = make_snapshot(NOW)  # 三檔都是收盤基準（fixture：收盤＝現價 × 0.99）
    write_snapshot(paths, snap)
    options = md.handle(" 期權", now=NOW, paths=paths)
    found = texts(options.flex)
    for line in ("那斯達克100（QQQ） 收盤 534.6｜資料 10/16", "標普500（SPY） 收盤 659.34｜資料 10/16",
                 "半導體（SOXX） 收盤 242.55｜資料 10/16"):
        assert line in found, line
    assert not any(" 現價 " in t for t in found)  # 「現價 X」改成和距離同一個基準的「收盤 X」或「最新價 X」
    footer = texts(options.flex["contents"][0]["footer"])
    assert md.TEXT_WALL_DIST_CLOSE in footer and md.TEXT_WALL_DIST_SPOT not in footer
    assert "45 天內：買權牆 556（+3.0%）、賣權牆 518（-4.0%）" in found  # 距離那一列的寫法照舊
    # 只有 SPY 退回現價：各檔印各檔的基準，footer 改成合併的一句
    snap["rows"]["us.options.SPY"]["values"] = _options(666.0, basis="spot")
    write_snapshot(paths, snap)
    options = md.handle(" 期權", now=NOW, paths=paths)
    found = texts(options.flex)
    assert "標普500（SPY） 最新價 666｜資料 10/16" in found and "那斯達克100（QQQ） 收盤 534.6｜資料 10/16" in found
    footer = texts(options.flex["contents"][0]["footer"])
    assert md.TEXT_WALL_DIST_MIXED in footer and md.TEXT_WALL_DIST_CLOSE not in footer


def test_option_detail_has_no_basis_sentence_when_no_wall_distance_is_shown(md, paths):
    snap = make_snapshot(NOW)
    for symbol in ("QQQ", "SPY"):
        snap["rows"][f"us.options.{symbol}"]["stale_after"] = NOW - 1
    snap["rows"]["us.options.SOXX"]["values"]["wall_status"] = "insufficient"
    write_snapshot(paths, snap)
    options = md.handle(" 期權", now=NOW, paths=paths)
    assert not any(t.startswith("牆距") for t in texts(options.flex))
    assert not any(t.startswith("牆距") for t in texts(_flow_bubble(md, paths, snap)["footer"]))


def test_cftc_note_says_there_is_no_semiconductor_futures_next_to_the_weekly_data_note(md, paths):
    write_snapshot(paths, make_snapshot(NOW))
    first, second = (texts(b["footer"]) for b in md.handle(" 期權", now=NOW, paths=paths).flex["contents"])
    assert market_calc.TEXT_CFTC_NO_SEMI in second and market_calc.TEXT_CFTC_NOTE in second
    assert second.index(market_calc.TEXT_CFTC_NO_SEMI) == second.index(market_calc.TEXT_CFTC_NOTE) + 1
    assert market_calc.TEXT_CFTC_NO_SEMI not in first  # CFTC 的列在第二張，註腳跟著它
    assert not any("半導體期貨" in t for t in first)


# ── 財報行（A16）：三張 ETF 細節卡各一行，只列這檔自己前十大的財報日 ──────────────────────────────────────────────
TOP10_GROUPS = {
    "VOO": (["NVDA"], ["AAPL"], ["MSFT"], ["AMZN"], ["GOOGL", "GOOG"], ["META"], ["AVGO"], ["TSLA"], ["BRK.B"], ["JPM"]),
    "QQQ": (["NVDA"], ["AAPL"], ["MSFT"], ["AMZN"], ["AVGO"], ["META"], ["GOOGL", "GOOG"], ["COST"], ["NFLX"], ["TSLA"]),
    "SOXX": (["NVDA"], ["AVGO"], ["AMD"], ["MU"], ["QCOM"], ["TXN"], ["INTC"], ["AMAT"], ["LRCX"], ["KLAC"]),
}
EARN_HEAD = "前十大 45 天內財報（美東日期）"  # F2-15：只看這檔 ETF 自己的前十大


def _earn(name, month, day, *, estimated=True, hour=16):
    """財報日只有日期：publish 放美東當天 16:00、time_known=False；estimated 省略＝舊快照沒有這個欄位。"""
    item = {"name": name, "kind": "earnings", "at": datetime(2026, month, day, hour, 0, tzinfo=ET).timestamp(),
            "time_known": False}
    if estimated is not None:
        item["estimated"] = estimated
    return item


def _earnings_snapshot(items):
    snap = make_snapshot(NOW)
    for etf, groups in TOP10_GROUPS.items():
        snap["rows"][f"us.fund.{etf}"]["values"]["rows"] = [
            {"name": f"{group[0]} Inc", "symbols": list(group), "pe": 20.0, "status": "ok"} for group in groups]
    snap["rows"]["events.next"]["values"]["items"] = items
    return snap


def _etf_texts(md, paths, snap, etf, now=NOW) -> list[str]:
    write_snapshot(paths, snap)
    reply = md.handle(f" {etf}", now=now, paths=paths)
    assert reply.render_errors == () and reply.flex is not None
    return texts(reply.flex)


def test_each_etf_card_lists_only_its_own_top_holdings_earnings(md, paths):
    items = [_earn("NVDA", 11, 19), _earn("AVGO", 11, 30, estimated=False), _earn("AMD", 10, 28, estimated=None),
             _earn("AAPL", 10, 29), _earn("JPM", 11, 12), _earn("COST", 11, 20), _earn("ZZZZ", 11, 1)]
    snap = _earnings_snapshot(items)
    expected = {
        "VOO": f"{EARN_HEAD}：AAPL 10/29（預估）、JPM 11/12（預估）、NVDA 11/19（預估）、AVGO 11/30",
        "QQQ": f"{EARN_HEAD}：AAPL 10/29（預估）、NVDA 11/19（預估）、COST 11/20（預估）、AVGO 11/30",
        "SOXX": f"{EARN_HEAD}：AMD 10/28、NVDA 11/19（預估）、AVGO 11/30",  # 沒寫 estimated 的不標預估；確認過的日期（False）也不標
    }
    for etf, line in expected.items():
        found = _etf_texts(md, paths, snap, etf)
        assert line in found, (etf, [t for t in found if t.startswith(EARN_HEAD)])
        assert found[found.index(line) + 1] == market_calc.TEXT_EARNINGS_OTHERS  # 缺的那句緊接在行尾，不點名
        assert not any("ZZZZ" in t for t in found)  # 不在任何一檔前十大的不列
    assert "COST" not in " ".join(_etf_texts(md, paths, snap, "VOO")) and "JPM" not in " ".join(
        _etf_texts(md, paths, snap, "QQQ"))
    assert "AMD" not in " ".join(_etf_texts(md, paths, snap, "VOO"))


def test_earnings_line_matches_either_share_class_and_shows_the_name_the_job_checked(md, paths):
    snap = _earnings_snapshot([_earn("GOOG", 11, 3), _earn("BRK.B", 11, 5, estimated=False)])
    assert f"{EARN_HEAD}：GOOG 11/3（預估）、BRK.B 11/5" in _etf_texts(md, paths, snap, "VOO")
    assert f"{EARN_HEAD}：GOOG 11/3（預估）" in _etf_texts(md, paths, snap, "QQQ")  # QQQ 沒有 BRK.B


def test_earnings_line_reads_items_19_to_24_of_the_list(md, paths):
    """publish 的 EVENTS_KEEP 12→24：bot 以前只讀前 18 筆（EVENTS_MAX*3）。前面 18 筆是經濟事件，後面 6 筆財報日要讀得到。"""
    filler = [{"name": "Initial Jobless Claims", "kind": "econ", "at": NOW + 3600 * (i + 1)} for i in range(18)]
    late = [_earn(name, 11, 2 + n) for n, name in enumerate(("NVDA", "AVGO", "AMD", "QCOM", "TXN", "INTC"))]
    snap = _earnings_snapshot(filler + late)
    line = "、".join(f"{name} 11/{2 + n}（預估）" for n, name in enumerate(("NVDA", "AVGO", "AMD", "QCOM", "TXN", "INTC")))
    assert f"{EARN_HEAD}：{line}" in _etf_texts(md, paths, snap, "SOXX")
    assert md.EVENTS_SCAN_MAX >= 24  # publish 的 EVENTS_KEEP（test_market_publish 鎖 24）


def test_events_listing_reads_all_the_items_the_publisher_keeps(md, paths):
    """同一個上限也管細節卡的「接下來的事件」：publish 的 keep_events 把必留的 FOMC、CPI 換到清單最後（第 23、24 筆），
    bot 只讀前 18 筆的話它們會消失。"""
    items = [_earn(f"Z{chr(65 + i)}", 10, 20, estimated=True, hour=10 + i // 4) for i in range(22)]
    items += [{"name": "FOMC", "kind": "fomc", "at": NOW + 11 * 86400},
              {"name": "Consumer Price Index", "kind": "cpi", "at": NOW + 3 * 86400}]
    snap = make_snapshot(NOW)
    snap["rows"]["events.next"]["values"]["items"] = items
    write_snapshot(paths, snap)
    macro = texts(md.handle(" 新聞", now=NOW, paths=paths).flex["contents"][2])
    listed = [t.split("：")[0] for t in macro[macro.index("接下來的事件") + 1:]]
    assert md._EVENT_KINDS["fomc"] in listed and md._EVENT_KINDS["cpi"] in listed, listed


def test_earnings_line_horizon_is_now_to_45_days(md, paths):
    day = 86400
    items = [{"name": "NVDA", "kind": "earnings", "at": NOW - 1, "time_known": False, "estimated": True},  # 已經過了
             {"name": "AVGO", "kind": "earnings", "at": NOW + 1, "time_known": False, "estimated": True},
             {"name": "AMD", "kind": "earnings", "at": NOW + 45 * day, "time_known": False, "estimated": True},
             {"name": "QCOM", "kind": "earnings", "at": NOW + 45 * day + 1, "time_known": False, "estimated": True}]
    found = _etf_texts(md, paths, _earnings_snapshot(items), "SOXX")
    line = next(t for t in found if t.startswith(EARN_HEAD))
    assert "AVGO" in line and "AMD" in line and "NVDA" not in line and "QCOM" not in line, line


def test_earnings_line_uses_the_us_eastern_date_not_the_taipei_one(md, paths):
    item = _earn("NVDA", 11, 19)  # 美東 11/19 16:00＝台北 11/20 05:00
    assert datetime.fromtimestamp(item["at"], TPE).day == 20
    assert f"{EARN_HEAD}：NVDA 11/19（預估）" in _etf_texts(md, paths, _earnings_snapshot([item]), "SOXX")


@pytest.mark.parametrize("estimated, tag", [(True, "（預估）"), (False, ""), (None, ""), (1, ""), ("yes", ""), ("true", "")])
def test_only_a_real_true_marks_the_date_as_estimated(md, paths, estimated, tag):
    found = _etf_texts(md, paths, _earnings_snapshot([_earn("NVDA", 11, 19, estimated=estimated)]), "SOXX")
    assert f"{EARN_HEAD}：NVDA 11/19{tag}" in found


def test_earnings_line_skips_items_it_cannot_read_and_repeats_nothing(md, paths):
    items = [None, 5, "NVDA", {"kind": "earnings"}, {"name": 5, "kind": "earnings", "at": NOW + 3600},
             {"name": "nvda!", "kind": "earnings", "at": NOW + 3600}, {"name": "NVDA", "kind": "earnings", "at": "soon"},
             {"name": "NVDA", "kind": "earnings", "at": float("inf")}, {"name": "NVDA", "kind": "econ", "at": NOW + 3600},
             _earn("NVDA", 11, 19), _earn("NVDA", 11, 21), _earn("AVGO", 11, 2, estimated=False)]
    found = _etf_texts(md, paths, _earnings_snapshot(items), "SOXX")
    assert f"{EARN_HEAD}：AVGO 11/2、NVDA 11/19（預估）" in found  # 同一檔重複的取最早一筆


def test_earnings_line_when_no_listed_holding_reports_in_the_window(md, paths):
    found = _etf_texts(md, paths, _earnings_snapshot([_earn("ZZZZ", 11, 19), _earn("COST", 11, 20)]), "SOXX")
    assert f"{EARN_HEAD}：{market_calc.TEXT_EARNINGS_NONE}" in found
    assert market_calc.TEXT_EARNINGS_OTHERS not in found  # 沒有列出任何一檔時，「其他」兩個字不成立
    assert not any("COST" in t or "ZZZZ" in t for t in found)


def test_earnings_line_says_why_it_has_nothing_to_list(md, paths):
    snap = _earnings_snapshot([_earn("NVDA", 11, 19)])
    snap["rows"]["events.next"]["stale_after"] = NOW - 1
    assert f"{EARN_HEAD}：過期" in _etf_texts(md, paths, snap, "SOXX")  # 事件列的資料日不是「財報的日期」：只寫過期
    snap["rows"]["events.next"] = _never()
    assert f"{EARN_HEAD}：暫缺" in _etf_texts(md, paths, snap, "SOXX")
    del snap["rows"]["events.next"]  # 舊快照
    assert f"{EARN_HEAD}：暫缺" in _etf_texts(md, paths, snap, "SOXX")
    snap = _earnings_snapshot([_earn("NVDA", 11, 19)])
    for row_id in ("us.fund.SOXX", "us.13f.SOXX"):  # 不知道這檔的前十大是誰：不猜
        snap["rows"][row_id]["stale_after"] = NOW - 1
    assert f"{EARN_HEAD}：暫缺" in _etf_texts(md, paths, snap, "SOXX")


def test_earnings_line_falls_back_to_the_13f_top_holdings_when_the_fund_row_is_old(md, paths):
    snap = _earnings_snapshot([_earn("NVDA", 11, 19), _earn("AAPL", 10, 29)])
    snap["rows"]["us.fund.SOXX"]["stale_after"] = NOW - 1
    snap["rows"]["us.13f.SOXX"]["values"]["rows"] = [
        dict(row, symbols=list(symbols)) for row, symbols in zip(snap["rows"]["us.13f.SOXX"]["values"]["rows"],
                                                                 TOP10_GROUPS["SOXX"])]
    assert f"{EARN_HEAD}：NVDA 11/19（預估）" in _etf_texts(md, paths, snap, "SOXX")


def test_missing_dates_are_never_named_on_the_card(md, paths):
    """MU 的 Nasdaq 日期常常是空的，可能本來就不在 45 天內：不寫「MU 日期暫缺」，只有一句固定句。"""
    found = _etf_texts(md, paths, _earnings_snapshot([_earn("NVDA", 11, 19)]), "SOXX")
    assert [t for t in found if "日期暫缺" in t] == [market_calc.TEXT_EARNINGS_OTHERS]
    line = next(t for t in found if t.startswith(EARN_HEAD))
    assert "MU" not in line and "暫缺" not in line


def test_earnings_line_worst_case_fits_the_card_limits(md, paths):
    snap = worst_snapshot()
    names = {etf: [f"{etf[0]}{chr(65 + i)}{chr(75 + i)}{chr(85 - i)}Z" for i in range(10)] for etf in ("VOO", "QQQ", "SOXX")}
    items = []
    for etf, symbols in names.items():
        snap["rows"][f"us.fund.{etf}"]["values"]["rows"] = [
            {"name": "名" * 24, "symbols": [symbol], "pe": 20.0, "status": "ok"} for symbol in symbols]
        items += [_earn(symbol, 11, 1 + i, estimated=True) for i, symbol in enumerate(symbols)]
    snap["rows"]["events.next"]["values"]["items"] = items
    for etf in ("VOO", "QQQ", "SOXX"):
        found = _etf_texts(md, paths, snap, etf)
        line = next(t for t in found if t.startswith(EARN_HEAD))
        assert line.count("（預估）") == 10 and all(symbol in line for symbol in names[etf])
        reply = md.handle(f" {etf}", now=NOW, paths=paths)
        assert md.card_bytes(reply.flex) <= md.CARD_MAX_BYTES
        assert card_pages.fits_line_limits(card_pages.with_page_button(reply.flex, LONG_PAGE_URL))


# ── VXTLT（A12）：自己的一行，不頂替 MOVE、MOVE 的句子裡不出現它的數字 ──────────────────────────────────────────
def _move_sentences(strings) -> list[str]:
    return [s for s in strings if "MOVE" in s]


def test_vxtlt_has_its_own_line_and_leaves_the_move_line_alone(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["macro.MOVE"] = _never()  # MOVE 的來源（Yahoo）常常沒有
    snap["rows"]["macro.VXTLT"]["values"].update(value=13.37, chg=-0.74, chg_pct=-4.67)
    write_snapshot(paths, snap)
    options = texts(md.handle(" 期權", now=NOW, paths=paths).flex)
    assert "美債波動指數（MOVE）暫缺" in options  # 照舊寫暫缺
    assert "TLT 選擇權波動（VXTLT）13.4（-4.67%）" in options
    macro = texts(md.handle(" 新聞", now=NOW, paths=paths).flex)
    assert "美債波動指數（MOVE）：暫缺" in macro
    assert "TLT 選擇權波動（VXTLT）：13.4（-4.67%）｜10/16" in macro
    # 整份快照的所有回覆：含「MOVE」的句子裡沒有 VXTLT 的名字或數字
    for body, reply in render_all(md, paths, NOW).items():
        for sentence in _move_sentences(reply_strings(reply)):
            assert not any(token in sentence for token in ("VXTLT", "13.4", "13.37", "-4.67")), (body, sentence)
        for sentence in (s for s in reply_strings(reply) if "VXTLT" in s):
            assert "MOVE" not in sentence and "美債波動" not in sentence, (body, sentence)


def test_when_move_comes_back_both_lines_stand_side_by_side(md, paths):
    snap = make_snapshot(NOW)  # fixture：MOVE 98.5、VXTLT 15.09（量綱差 6–7 倍）
    write_snapshot(paths, snap)
    options = texts(md.handle(" 期權", now=NOW, paths=paths).flex)
    assert options.index("美債波動指數（MOVE）98.5") + 1 == options.index("TLT 選擇權波動（VXTLT）15.1（-4.67%）")
    macro = texts(md.handle(" 新聞", now=NOW, paths=paths).flex)
    assert "美債波動指數（MOVE）：98.5（+0.12）｜10/16" in macro
    assert "TLT 選擇權波動（VXTLT）：15.1（-4.67%）｜10/16" in macro
    assert macro.index("美債波動指數（MOVE）：98.5（+0.12）｜10/16") + 1 == macro.index(
        "TLT 選擇權波動（VXTLT）：15.1（-4.67%）｜10/16")


@pytest.mark.parametrize("change, options_line, macro_line", [
    (dict(stale_after=NOW - 1), "TLT 選擇權波動（VXTLT）過期（資料日 10/16）", "TLT 選擇權波動（VXTLT）：過期（資料日 10/16）"),
    (dict(state="never", values={}), "TLT 選擇權波動（VXTLT）暫缺", "TLT 選擇權波動（VXTLT）：暫缺"),
    (dict(values={"value": "15", "chg": None, "chg_pct": None, "source": "CBOE"}), "TLT 選擇權波動（VXTLT）暫缺", None),
    (dict(values={"value": 15.09, "chg": None, "chg_pct": None, "source": "CBOE"}), "TLT 選擇權波動（VXTLT）15.1",
     "TLT 選擇權波動（VXTLT）：15.1｜10/16"),
    (dict(values={"value": 15.09, "chg": -0.74, "chg_pct": None, "source": "CBOE"}), "TLT 選擇權波動（VXTLT）15.1（-0.74）",
     "TLT 選擇權波動（VXTLT）：15.1（-0.74）｜10/16"),
])
def test_vxtlt_line_when_the_row_is_old_missing_or_partial(md, paths, change, options_line, macro_line):
    snap = make_snapshot(NOW)
    snap["rows"]["macro.VXTLT"].update(change)
    write_snapshot(paths, snap)
    reply = md.handle(" 期權", now=NOW, paths=paths)
    assert reply.render_errors == () and options_line in texts(reply.flex)
    news = md.handle(" 新聞", now=NOW, paths=paths)
    assert news.render_errors == ()
    if macro_line:
        assert macro_line in texts(news.flex)


def test_the_summary_cards_do_not_read_vxtlt(md, paths):
    snap = make_snapshot(NOW)
    write_snapshot(paths, snap)
    with_row = md.handle("", now=NOW, paths=paths)
    snap["rows"]["macro.VXTLT"] = _never()
    write_snapshot(paths, snap)
    assert md.handle("", now=NOW, paths=paths).flex == with_row.flex  # 摘要輪播一個字都不變


def test_events_scan_limit_covers_what_publish_keeps(md):
    assert md.EVENTS_SCAN_MAX >= 24 > md.EVENTS_MAX * 3  # 24＝market_publish.EVENTS_KEEP；舊上限 18 讀不到第 19–24 筆


def test_alt_text_writes_each_market_day_when_they_differ(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["tw.taiex"]["data_date"] = _today().isoformat()  # 台股已收盤（10/17）、美股還是 10/16
    write_snapshot(paths, snap)
    reply = md.handle("", now=NOW, paths=paths)
    assert reply.alt_text == f"盤勢：{ONELINER_US}｜{ONELINER_TW}｜美股資料 10/16、台股資料 10/17"
    assert_main_gate(reply)
    snap["rows"]["us.etf.VOO"]["stale_after"] = NOW - 1  # 只剩台股有日期：只寫一個「資料」
    write_snapshot(paths, snap)
    assert md.handle("", now=NOW, paths=paths).alt_text.endswith("｜資料 10/17")


def test_etf_proxy_shows_only_the_daily_change_and_never_the_etf_price(md, paths):
    snap = make_snapshot(NOW)
    write_snapshot(paths, snap)
    replies = render_all(md, paths, NOW)
    for body, reply in replies.items():
        assert not any("28.92" in value for value in reply_strings(reply)), body  # UUP 的股價不是美元指數
    assert texts(summary_bubble(md, replies[""].flex, "宏觀與事件")["body"])[1] == "美元（UUP 代理）單日 +0.30%、油價（WTI）72.3"
    detail = texts(replies[" 新聞"].flex["contents"][2]["body"])
    assert "美元（UUP 代理）：單日 +0.30%｜10/16" in detail  # 股價的變動 +0.12 也不印
    # 沒有日漲跌幅：明說沒有，不拿股價頂替
    snap["rows"]["macro.DXY"]["values"]["chg_pct"] = None
    summary, detail = _macro_rows(md, paths, snap)
    assert summary[1] == "美元（UUP 代理）暫無漲跌幅、油價（WTI）72.3"
    assert "美元（UUP 代理）：暫無漲跌幅｜10/16" in detail
    # 兩列都是代理、全名放不下：短寫法也要留「（代理）」
    snap["rows"]["macro.DXY"]["values"]["chg_pct"] = 0.3
    snap["rows"]["macro.WTI"]["values"].update(source="ETF 代理：USO", chg_pct=1.2, value=71.2)
    summary, detail = _macro_rows(md, paths, snap)
    assert summary[1] == "美元（代理）單日 +0.30%、油價（代理）單日 +1.20%" and md.row_width(summary[1]) <= 24
    assert "油價（USO 代理）：單日 +1.20%｜10/16" in detail and not any("71.2" in t for t in detail)


def _macro_rows(md, paths, snap, now=NOW):
    write_snapshot(paths, snap)
    card = md.handle("", now=now, paths=paths).flex
    news = md.handle(" 新聞", now=now, paths=paths).flex
    return texts(summary_bubble(md, card, "宏觀與事件")["body"]), texts(news["contents"][2]["body"])


def test_provisional_us_close_is_marked_on_the_cards(md, paths):
    """2026-10-23 審查 r9 Codex 席 I2：ETF 列的收盤是 CBOE 的暫定值時，卡上要看得出來（摘要列「（暫定）」、細節卡一句
    固定說明）；量能寫「暫不判斷」，前面不能多一個「，」。"""
    snap = make_snapshot(NOW)
    snap["rows"]["us.etf.VOO"]["source"] = md.market_calc.SOURCE_CBOE_FALLBACK
    snap["rows"]["us.etf.VOO"]["values"].update(vol_ratio=None, vol_pct250=None, vol_label=None,
                                                 vol_note=md.market_calc.TEXT_VOL_SOURCE_DIFF)
    write_snapshot(paths, snap)
    us = texts(summary_bubble(md, md.handle("", now=NOW, paths=paths).flex, "美股")["body"])
    line = next(text for text in us if text.startswith("標普500（VOO）"))
    assert "（暫定）" in line and md.row_width(line) <= 24
    detail = reply_strings(md.handle(" VOO", now=NOW, paths=paths))
    assert md.market_calc.TEXT_PROVISIONAL_CLOSE in detail
    assert not any("量能：，" in text for text in detail)
    assert not any("（暫定）" in text for text in texts(summary_bubble(md, md.handle("", now=NOW, paths=paths).flex, "台股")["body"]))


def test_two_etf_proxies_keep_their_whole_changes(md, paths):
    """2026-10-23 審查 r7 #2：兩列都是代理、短寫法還超過 24 字寬 → 原本逐字切成「+12.…」。"""
    snap = make_snapshot(NOW)
    snap["rows"]["macro.DXY"]["values"]["chg_pct"] = 0.3
    snap["rows"]["macro.WTI"]["values"].update(source="ETF 代理：USO", chg_pct=12.34, value=71.2)
    summary, detail = _macro_rows(md, paths, snap)
    assert summary[1] == "美元代理 +0.30%、油價代理 +12.34%" and md.row_width(summary[1]) <= 24
    assert "油價（USO 代理）：單日 +12.34%｜10/16" in detail


def test_clip_never_prints_half_a_number(md):
    assert md._clip("名稱" * 10 + " +12.34%!") == "名稱" * 10 + "…"  # 原本是「+12.3…」
    assert md._clip("名稱" * 13) == "名稱" * 11 + "名…"
    assert md._clip("名稱" * 10 + " 1,234,567!") == "名稱" * 10 + "…"  # 千分位也是同一個數字


def test_btc_from_the_ibit_proxy_shows_the_daily_change_only(md, paths):
    """2026-10-23：Coinbase 被台灣封鎖，BTC 沒有 Yahoo 時用 IBIT 代理（和 GOLD→GLD 同一個寫法）：只寫單日漲跌幅，
    不把 ETF 股價 46.06 印成比特幣價格。摘要列放不下 ticker 就先拿掉，細節卡有完整的「（IBIT 代理）」。"""
    snap = make_snapshot(NOW)
    snap["rows"]["macro.BTC"]["values"] = {"value": 46.06, "chg": -1.78, "chg_pct": -3.7207, "source": "ETF 代理：IBIT"}
    summary, detail = _macro_rows(md, paths, snap)
    assert summary[2] == "黃金 2,650、比特幣（代理）單日 -3.72%" and md.row_width(summary[2]) <= 24
    assert "比特幣（IBIT 代理）：單日 -3.72%｜10/16" in detail
    for body, reply in render_all(md, paths, NOW).items():
        assert not any("46.06" in value for value in reply_strings(reply)), body
    # 黃金也是代理：兩個代理並排放不下全名，短寫法的數字一樣完整
    snap["rows"]["macro.GOLD"]["values"] = {"value": 378.95, "chg": 1.3, "chg_pct": 0.34, "source": "ETF 代理：GLD"}
    summary, detail = _macro_rows(md, paths, snap)
    assert summary[2] == "黃金代理 +0.34%、比特幣代理 -3.72%" and md.row_width(summary[2]) <= 24
    assert "黃金（GLD 代理）：單日 +0.34%｜10/16" in detail and "比特幣（IBIT 代理）：單日 -3.72%｜10/16" in detail
    assert_main_gate(md.handle("", now=NOW, paths=paths))
    # 沒有日漲跌幅就明說沒有
    snap["rows"]["macro.BTC"]["values"]["chg_pct"] = None
    summary, detail = _macro_rows(md, paths, snap)
    assert "比特幣（IBIT 代理）：暫無漲跌幅｜10/16" in detail and not any("46.06" in t for t in summary + detail)


def test_taifex_fx_proxy_keeps_its_value_and_says_who_it_is(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["macro.USDTWD"]["values"]["source"] = "代理：期交所匯率"  # market_pipeline_misc.SOURCE_FX
    _summary, detail = _macro_rows(md, paths, snap)
    assert "台幣匯率（期交所代理）：32.34（+0.12）｜10/16" in detail  # 和 Yahoo 的 TWD=X 是同一個量：價位照印
    snap["rows"]["macro.USDTWD"]["values"]["source"] = "別的代理"
    assert "台幣匯率（代理）：32.34（+0.12）｜10/16" in _macro_rows(md, paths, snap)[1]


def test_volume_words_on_the_rows_are_the_one_liners_words(md, paths):
    for label, phrase in market_calc.VOL_PHRASE.items():
        assert md._vol_phrase(label) == phrase and re.fullmatch(md._VOL_PHRASES, phrase)
    assert md._vol_phrase("明顯量縮") == "量明顯偏少" and md._vol_phrase("亂寫的") is None
    snap = make_snapshot(NOW)
    for sym in ("VOO", "QQQ", "SOXX"):
        snap["rows"][f"us.etf.{sym}"]["values"]["vol_label"] = "明顯量縮"
    row = _summary_bodies(md, paths, snap)[0][4]
    assert row == "量能：VOO 明顯偏少、QQQ 明顯偏少、SOXX 明顯偏少"  # 三個「量明顯偏少」超過 24 字寬：拿掉開頭的「量」
    for sym in ("VOO", "QQQ", "SOXX"):
        snap["rows"][f"us.etf.{sym}"]["values"].update(vol_label="量縮", vol_flag="結算日")
    row = _summary_bodies(md, paths, snap)[0][4]
    assert row == "量能（結算日）：VOO 偏少、QQQ 偏少、SOXX 偏少" and md.row_width(row) <= 24  # 旗標比「量」字重要
    snap["rows"]["us.etf.SOXX"]["values"].update(vol_label="爆量", vol_flag=None)
    snap["rows"]["us.etf.VOO"]["values"].update(vol_flag=None)
    snap["rows"]["us.etf.QQQ"]["values"].update(vol_flag=None)
    assert _summary_bodies(md, paths, snap)[0][4] == "量能：VOO 量偏少、QQQ 量偏少、SOXX 爆量"


def test_0050_detail_names_the_subject_on_the_close_and_ma_rows(md, paths):
    write_snapshot(paths, make_snapshot(NOW))
    tw = texts(md.handle(" 0050", now=NOW, paths=paths).flex)
    assert tw[1] == "加權指數 47,111（+0.52%） 在月線上"  # 第一列是指數
    assert "台灣50（0050）收盤 185.3（+0.40%）" in tw and "台灣50（0050）在月線上" in tw
    voo = texts(md.handle(" VOO", now=NOW, paths=paths).flex)
    assert "收盤 612.34（+0.52%）" in voo and "在月線上" in voo  # 美股 ETF 的標題已經是名稱，列不重複


def test_title_says_expired_only_when_every_row_it_uses_is_expired(md, paths):
    snap = make_snapshot(NOW)
    for sym in ("VOO", "QQQ", "SOXX"):
        snap["rows"][f"us.etf.{sym}"] = _never()  # 沒有資料（不是過期）
    snap["rows"]["us.etf.SOXX"] = dict(make_snapshot(NOW)["rows"]["us.etf.SOXX"], stale_after=NOW - 1)  # 一檔過期、兩檔暫缺
    write_snapshot(paths, snap)
    card = md.handle("", now=NOW, paths=paths).flex
    assert summary_bubble(md, card, "美股")["header"]["contents"][0]["text"] == "美股（2/5）｜資料暫缺"
    snap["rows"]["us.etf.VOO"] = dict(make_snapshot(NOW)["rows"]["us.etf.VOO"], stale_after=NOW - 1)
    snap["rows"]["us.etf.QQQ"] = dict(make_snapshot(NOW)["rows"]["us.etf.QQQ"], stale_after=NOW - 1)
    write_snapshot(paths, snap)
    card = md.handle("", now=NOW, paths=paths).flex
    assert summary_bubble(md, card, "美股")["header"]["contents"][0]["text"] == "美股（2/5）｜資料過期"


def test_detail_cards_that_print_clock_times_say_they_are_taipei_time(md, paths):
    write_snapshot(paths, make_snapshot(NOW))
    news = md.handle(" 新聞", now=NOW, paths=paths).flex
    footers = [texts(b["footer"]) for b in news["contents"]]
    assert "時間為台北時間" in footers[0] and "時間為台北時間" in footers[1]  # 新聞、伊朗新聞的時刻沒有逐列寫台北
    election = md.handle(" 選舉", now=NOW, paths=paths).flex
    assert "時間為台北時間" in texts(election["footer"])  # 「市場更新 10/17 16:00」
    options = md.handle(" 期權", now=NOW, paths=paths).flex
    assert not any("時間為台北時間" in t for t in texts(options))  # 沒印時刻的卡不加
    snap = make_snapshot(NOW)
    for kind in ("us", "tw", "iran", "semi"):
        snap["rows"][f"news.{kind}"]["values"]["items"] = []
    write_snapshot(paths, snap)
    news = md.handle(" 新聞", now=NOW, paths=paths).flex
    assert not any("時間為台北時間" in texts(b["footer"]) for b in news["contents"][:2])


def test_options_footer_explains_walls_and_max_pain_without_support_resistance(md, paths):
    write_snapshot(paths, make_snapshot(NOW))
    options = md.handle(" 期權", now=NOW, paths=paths).flex
    footer = texts(options["contents"][0]["footer"])
    assert "牆＝未平倉合約最集中的履約價" in footer
    assert "最大痛點＝讓選擇權買方整體虧損最多的結算價（不是目標價）" in footer
    assert not any(word in t for t in texts(options) for word in ("支撐", "壓力"))


def test_events_at_the_same_moment_put_fomc_cpi_and_nfp_first(md, paths):
    snap = make_snapshot(NOW)
    at = NOW + 2 * 86400
    snap["rows"]["events.next"]["values"]["items"] = [  # 快照裡失業率排在非農前面
        {"name": "Unemployment Rate", "kind": "econ", "at": at},
        {"name": "Nonfarm Payrolls", "kind": "nfp", "at": at},
    ]
    write_snapshot(paths, snap)
    summary = texts(summary_bubble(md, md.handle("", now=NOW, paths=paths).flex, "宏觀與事件")["body"])
    assert "下一個事件：非農就業（NFP），還有 2 天" in summary
    macro = texts(md.handle(" 新聞", now=NOW, paths=paths).flex["contents"][2]["body"])
    listed = macro[macro.index("接下來的事件") + 1:]
    assert [t.split("：")[0] for t in listed] == ["非農就業（NFP）", "失業率（Unemployment）"]


def test_event_labels_are_the_shared_ones(md):
    assert md._EVENT_KINDS["fomc"] == "聯準會利率決議（FOMC）"  # 倒數的是利率決議公布的時刻
    for kind, name in (("cpi", "CPI"), ("ppi", "PPI"), ("nfp", "Nonfarm Payrolls"), ("pce", "PCE Price Index"),
                       ("gdp", "GDP")):
        assert md._EVENT_KINDS[kind] == market_calc.US_EVENT_LABELS[name]


def test_dead_names_are_gone(md):
    for name in ("TEXT_CHART_FAILED", "MAX_LABEL_CHARS", "GRAY_LIGHT", "PALETTE"):
        assert not hasattr(md, name), name


def test_estimated_earnings_dates_are_marked_and_confirmed_ones_are_not(md, paths):
    snap = make_snapshot(NOW)
    at = datetime(2026, 10, 20, 16, 0, tzinfo=ET).timestamp()
    snap["rows"]["events.next"]["values"]["items"] = [
        {"name": "NVDA", "kind": "earnings", "at": at, "time_known": False, "estimated": True},
        {"name": "AAPL", "kind": "earnings", "at": at + 3600, "time_known": False, "estimated": False},
        {"name": "MSFT", "kind": "earnings", "at": at + 7200, "time_known": False},  # 沒說＝不宣稱是預估
    ]
    write_snapshot(paths, snap)
    detail = texts(md.handle(" 新聞", now=NOW, paths=paths).flex)
    for needed in ("財報（NVDA，預估）：美東 10/20（時間未公布）", "財報（AAPL）：美東 10/20（時間未公布）",
                   "財報（MSFT）：美東 10/20（時間未公布）"):
        assert needed in detail, needed
    summary = texts(summary_bubble(md, md.handle("", now=NOW, paths=paths).flex, "宏觀與事件")["body"])
    assert "下一個事件：財報（NVDA，預估），還有 3 天" in summary  # 加了「預估」放不下「（美東日期）」
    snap["rows"]["events.next"]["values"]["items"] = snap["rows"]["events.next"]["values"]["items"][1:]
    write_snapshot(paths, snap)
    summary = texts(summary_bubble(md, md.handle("", now=NOW, paths=paths).flex, "宏觀與事件")["body"])
    assert "下一個事件：財報（AAPL），還有 3 天（美東日期）" in summary  # 公司公布的日期：寫法不變


def _election_snapshot(now, state, quotes):
    snap = make_snapshot(now)
    snap["rows"]["election"]["values"].update(state=state, quotes=quotes)
    return snap


def _quote(platform, chamber, *, mid=None, last=None, result=None, updated=None):
    return {"platform": platform, "chamber": chamber, "outcome": "Yes", "mid": mid, "last": last,
            "updated_utc": updated, "result": result}


def test_election_after_the_vote_says_who_won_when_the_market_settled(md, paths):
    now = datetime(2026, 11, 8, 12, tzinfo=ET).timestamp()
    quotes = [
        _quote("polymarket", "house", last=0.99, result="yes", updated=now - 600),
        _quote("kalshi", "house", last=0.98, result="YES"),  # 大小寫不拘
        _quote("polymarket", "senate", last=0.01, result="no", updated=now - 600),
        _quote("kalshi", "senate", result="maybe"),  # 不是 Yes／No：當沒有結果；也沒有價位 → 暫缺
    ]
    write_snapshot(paths, _election_snapshot(now, "result", quotes))
    detail = md.handle(" 選舉", now=now, paths=paths)
    assert_main_gate(detail)
    found = texts(detail.flex)
    for needed in ("眾院民主黨（Polymarket）：已結算，民主黨拿下", "眾院民主黨（Kalshi）：已結算，民主黨拿下",
                   "參院民主黨（Polymarket）：已結算，民主黨未拿下", "參院民主黨（Kalshi）：暫缺",
                   "最後成交 99.0%｜市場更新 11/9 00:50"):
        assert needed in found, needed
    summary = md.handle("", now=now, paths=paths)
    assert_main_gate(summary)
    assert "期中選舉：已投票，眾院已結算，民主黨拿下" in texts(summary_bubble(md, summary.flex, "宏觀與事件")["body"])
    # 兩個平台的結果不同（結算規則不同）：摘要不挑一家，請看細節
    quotes[1] = _quote("kalshi", "house", last=0.02, result="no")
    write_snapshot(paths, _election_snapshot(now, "result", quotes))
    assert "期中選舉：已投票，結果看 /盤勢 選舉" in texts(summary_bubble(md, md.handle("", now=now, paths=paths).flex, "宏觀與事件")["body"])
    # 還沒有結算結果：照舊
    quotes = [_quote("polymarket", "house", mid=0.6, last=0.61, updated=now - 600)]
    write_snapshot(paths, _election_snapshot(now, "result", quotes))
    assert "期中選舉：已投票，結果看 /盤勢 選舉" in texts(summary_bubble(md, md.handle("", now=now, paths=paths).flex, "宏觀與事件")["body"])


def test_election_quote_without_a_mid_still_shows_the_last_trade(md, paths):
    now = datetime(2026, 11, 6, 12, tzinfo=ET).timestamp()
    # 買 0.99／賣 1.00：算不出中間值（publish 要求賣價 <1），但有最後成交 0.99
    quotes = [_quote("polymarket", "house", last=0.99, updated=now - 600), _quote("kalshi", "house", last=0.97)]
    write_snapshot(paths, _election_snapshot(now, "waiting", quotes))
    detail = md.handle(" 選舉", now=now, paths=paths)
    assert_main_gate(detail)
    found = texts(detail.flex)
    assert "眾院民主黨（Polymarket）：最後成交 99.0%（買賣價中間值暫缺）" in found
    assert "眾院民主黨（Kalshi）：最後成交 97.0%（買賣價中間值暫缺）" in found
    assert "市場更新 11/7 00:50" in found and market_calc.TEXT_KALSHI_TIME in found
    assert not any(t.endswith("：暫缺") for t in found)
    assert "期中選舉：已投票，等待結果" in texts(summary_bubble(md, md.handle("", now=now, paths=paths).flex, "宏觀與事件")["body"])


def _blocked_snapshot(now):
    snap = make_snapshot(now)
    snap["rows"]["election"]["values"].update(blocked=True, quotes=[])  # market_publish 在封鎖時寫的樣子
    return snap


@pytest.mark.parametrize("day", [(10, 24), (10, 25), (11, 2)])
def test_election_summary_line_says_the_markets_are_blocked_and_keeps_the_countdown(md, paths, day):
    """2026-10-23：Polymarket、Kalshi 被台灣封鎖。摘要列照寫「期中選舉還有 N 天」，後面接固定句，不寫成「暫缺」。"""
    now = datetime(2026, *day, 12, tzinfo=ET).timestamp()
    write_snapshot(paths, _blocked_snapshot(now))
    reply = md.handle("", now=now, paths=paths)
    assert_main_gate(reply)
    line = texts(summary_bubble(md, reply.flex, "宏觀與事件")["body"])[4]
    days = (date(2026, 11, 3) - date(2026, *day)).days
    assert line == f"期中選舉還有 {days} 天，預測市場：台灣已封鎖" and md.row_width(line) <= 24
    assert "暫缺" not in line
    assert len(summary_bubble(md, reply.flex, "宏觀與事件")["body"]["contents"]) == 6  # 摘要卡的列數不增加


def test_election_detail_says_the_markets_are_blocked_instead_of_missing(md, paths):
    write_snapshot(paths, _blocked_snapshot(NOW))
    detail = md.handle(" 選舉", now=NOW, paths=paths)
    assert_main_gate(detail)
    found = texts(detail.flex)
    assert "投票日 11/3（美東），還有 17 天" in found  # 倒數照寫
    assert "預測市場：台灣已封鎖這些網站，暫不提供" in found and md.TEXT_PREDICTION_BLOCKED in found
    assert market_calc.TEXT_POLL_NONE in found
    assert not any("暫缺" in t or "Polymarket" in t or "Kalshi" in t for t in found)
    assert market_calc.TEXT_ELECTION_NOTE not in found  # 沒有報價可印，「兩平台結算規則不同」那句也不放
    assert "時間為台北時間" not in found


def test_election_after_the_vote_still_says_the_markets_are_blocked(md, paths):
    now = datetime(2026, 11, 8, 12, tzinfo=ET).timestamp()
    snap = _blocked_snapshot(now)
    snap["rows"]["election"]["values"]["state"] = "waiting"
    write_snapshot(paths, snap)
    detail = texts(md.handle(" 選舉", now=now, paths=paths).flex)
    assert "已投票，等待結果" in detail and md.TEXT_PREDICTION_BLOCKED in detail


def test_missing_prediction_data_is_missing_unless_the_row_says_blocked(md, paths):
    """「被封鎖」只在選舉列寫 blocked＝true 時才說；沒寫、寫成別的值、或列過期，都照舊當資料暫缺／過期。"""
    for extra in ({"blocked": False}, {}, {"blocked": "yes"}, {"blocked": 1}):
        snap = make_snapshot(NOW)
        values = snap["rows"]["election"]["values"]
        values.pop("blocked", None)
        values.update(quotes=[], **extra)
        write_snapshot(paths, snap)
        rows = texts(summary_bubble(md, md.handle("", now=NOW, paths=paths).flex, "宏觀與事件")["body"])
        assert rows[4] == "期中選舉還有 17 天，預測市場暫缺", extra
        detail = texts(md.handle(" 選舉", now=NOW, paths=paths).flex)
        assert "預測市場：暫缺" in detail and md.TEXT_PREDICTION_BLOCKED not in detail, extra
    snap = _blocked_snapshot(NOW)
    snap["rows"]["election"]["stale_after"] = NOW - 1  # 列過期：不知道現在有沒有被封鎖
    write_snapshot(paths, snap)
    assert texts(summary_bubble(md, md.handle("", now=NOW, paths=paths).flex, "宏觀與事件")["body"])[4] == "期中選舉還有 17 天，預測市場暫缺"
    assert md.TEXT_PREDICTION_BLOCKED not in texts(md.handle(" 選舉", now=NOW, paths=paths).flex)


def test_blocked_sentences_are_fixed_words_that_pass_the_gate(md):
    for text in (md.TEXT_PREDICTION_BLOCKED, md.TEXT_PREDICTION_BLOCKED_SHORT):
        assert not md.has_forbidden(text) and not re.search(r"\d", text)
    assert md.TEXT_PREDICTION_BLOCKED_SHORT in md.TEXT_PREDICTION_BLOCKED
    assert md.row_width(md.TEXT_PREDICTION_BLOCKED) <= 24


def test_pe_rows_say_why_a_company_has_no_pe(md, paths):
    snap = make_snapshot(NOW)
    rows = snap["rows"]["us.fund.VOO"]["values"]["rows"]
    rows[0].update(pe=None, status="no_market_cap")
    rows[1].update(pe=None, status="stale")
    rows[2].update(pe=None, status="conflict")  # eps_metrics 的其他原因：近四季資料不齊
    rows[3].update(pe=None, status=["junk"])  # 型別不對：同樣當資料不齊，不丟例外
    write_snapshot(paths, snap)
    voo = texts(md.handle(" VOO", now=NOW, paths=paths).flex)
    assert "輝達（NVDA）：市值暫缺，不計本益比" in voo
    assert "蘋果（AAPL）：資料過期，不計本益比" in voo
    assert "微軟（MSFT）：近四季資料不齊，不計本益比" in voo and "亞馬遜（AMZN）：近四季資料不齊，不計本益比" in voo
    assert f"特斯拉（TSLA）：{market_calc.TEXT_PE_NONPOSITIVE}" in voo  # 原本的原因照舊


def test_13f_card_and_alt_text_use_the_same_words(md, paths):
    write_snapshot(paths, make_snapshot(NOW))
    reply = md.handle(" 13F VOO", now=NOW, paths=paths)
    assert "前十大成分股中，3 檔的加碼機構多於減碼機構（1 檔資料暫缺）" in texts(reply.flex)
    assert reply.alt_text == "盤勢：13F 前十大（VOO）｜3 檔的加碼機構多於減碼機構"
    assert "加碼 1,200 家、減碼 1,100 家" in "\n".join(texts(reply.flex))  # 機構一律「家」


def test_status_summary_reports_calendar_coverage(md, paths):
    """2026-10-18 審查 r6a I1／I2：outcome 的 calendar_until／calendar_unclassified 帶進 status_summary（給每日維護）。"""
    write_snapshot(paths, make_snapshot(NOW))
    outcome = {"status": "ok", "code": "verified", "collect": {"status": "ok"}, "publish": {"status": "ok"},
               "calendar_until": {"us": "2027-12-31", "tw": "2026-10-27", "xx": "2026-01-01"},
               "calendar_unclassified": {"us": 0, "tw": 2}}
    market_paths.write_private_atomic(paths.outcome, json.dumps(outcome).encode())
    summary = md.status_summary(now=NOW, paths=paths)["outcome"]
    assert summary["calendar_days_left"] == {"us": 440, "tw": 10}  # NOW＝台北 10/17
    assert summary["calendar_unclassified"] == {"us": 0, "tw": 2}


@pytest.mark.parametrize("extra", [
    {},  # 舊版 job、或 Store 開不起來（outcome 只有 store 那個 key）
    {"calendar_until": {"error": "unexpected:OSError"}},  # 覆蓋率計算出錯（jobs/market_snapshot.py 寫的形狀）
    {"calendar_until": {"us": 20271231, "tw": "2026-13-01"}, "calendar_unclassified": {"us": -1, "tw": "2"}},
    {"calendar_until": {"us": "2027-12-31T00:00", "tw": None}, "calendar_unclassified": {"us": True, "tw": 1.5}},
    {"calendar_until": ["2027-12-31"], "calendar_unclassified": "0"},
])
def test_status_summary_calendar_fields_fall_back_to_null(md, paths, extra):
    """2026-10-23 審查 r7b F7（T9）：outcome 缺 key、{"error": …}、壞值都要給 {"us": None, "tw": None}，不是 {}——
    每日維護「≤30 或是 null」的條目靠 null 才會報。"""
    write_snapshot(paths, make_snapshot(NOW))
    outcome = {"status": "ok", "code": "verified", "collect": {"status": "ok"}, "publish": {"status": "ok"}, **extra}
    market_paths.write_private_atomic(paths.outcome, json.dumps(outcome).encode())
    summary = md.status_summary(now=NOW, paths=paths)["outcome"]
    assert summary["calendar_days_left"] == {"us": None, "tw": None}
    assert summary["calendar_unclassified"] == {"us": None, "tw": None}
    assert summary["calendar_missing_bars"] == {"us": None, "tw": None}


def test_status_summary_reports_missing_calendar_bars(md, paths):
    """2026-10-23 審查 r9 gp1 N2：job 有寫 calendar_missing_bars，status_summary 卻沒帶，每日維護那條看不到。"""
    write_snapshot(paths, make_snapshot(NOW))
    outcome = {"status": "ok", "code": "verified", "collect": {"status": "ok"}, "publish": {"status": "ok"},
               "calendar_missing_bars": {"us": 0, "tw": 1}}
    market_paths.write_private_atomic(paths.outcome, json.dumps(outcome).encode())
    assert md.status_summary(now=NOW, paths=paths)["outcome"]["calendar_missing_bars"] == {"us": 0, "tw": 1}


def test_status_summary_reports_how_far_us_data_lags(md, paths):
    """2026-10-23（investigations/format_20261023.md §2、freshness_20261023.md §5 第 5 點、審查 r7b F2）：outcome 的
    us_etf_lag_sessions／us_etf_lag_s／us_members_lag_sessions／us_hist_vendor 帶進 status_summary；缺值或壞值 → None。"""
    write_snapshot(paths, make_snapshot(NOW))
    base = {"status": "ok", "code": "verified", "collect": {"status": "ok"}, "publish": {"status": "ok"}}
    keys = ("us_etf_lag_sessions", "us_etf_lag_s", "us_members_lag_sessions", "us_hist_vendor")
    for values, expect in (((1, 50400, 4, 2), (1, 50400, 4, 2)), ((0, 0, 0, 0), (0, 0, 0, 0)),
                           ((-1, "9", True, 1.5), (None, None, None, None))):
        market_paths.write_private_atomic(paths.outcome, json.dumps({**base, **dict(zip(keys, values))}).encode())
        summary = md.status_summary(now=NOW, paths=paths)["outcome"]
        assert tuple(summary[key] for key in keys) == expect, values
    market_paths.write_private_atomic(paths.outcome, json.dumps(base).encode())  # 舊版 job：沒有這幾個 key
    summary = md.status_summary(now=NOW, paths=paths)["outcome"]
    assert tuple(summary[key] for key in keys) == (None, None, None, None)


def test_status_summary_reports_the_cpi_month_lag_and_poll_state(md, paths):
    """2026-10-24 審查 r9 s4-F15：BLS 一個月才抓幾次，failing 要連續 3 次失敗才列，一個月內永遠到不了。outcome 的 infl_*（已存最新
    月份、落後幾個月、輪詢狀態）帶進 status_summary；缺值或壞值 → None。"""
    write_snapshot(paths, make_snapshot(NOW))
    base = {"status": "ok", "code": "verified", "collect": {"status": "ok"}, "publish": {"status": "ok"}}
    keys = ("infl_cpi_month", "infl_cpi_lag_months", "infl_bls_poll")
    for values, expect in (
        (("2026-09", 0, "done"), ("2026-09", 0, "done")),
        (("2026-08", 1, "pending"), ("2026-08", 1, "pending")),
        ((None, None, "exhausted"), (None, None, "exhausted")),
        (("2026-08", 2, "disabled"), ("2026-08", 2, "disabled")),
        (("2026-13", -1, "stuck"), (None, None, None)),
        (("september", "1", True), (None, None, None)),
        (("2026-09\n", 1.5, ["done"]), (None, None, None)),
        (("20260930", 10**9, {"done": 1}), (None, None, None)),
    ):
        market_paths.write_private_atomic(paths.outcome, json.dumps({**base, **dict(zip(keys, values))}).encode())
        summary = md.status_summary(now=NOW, paths=paths)["outcome"]
        assert tuple(summary[key] for key in keys) == expect, values
    market_paths.write_private_atomic(paths.outcome, json.dumps(base).encode())  # 舊版 job：沒有這幾個 key
    summary = md.status_summary(now=NOW, paths=paths)["outcome"]
    assert tuple(summary[key] for key in keys) == (None, None, None)


def test_provisional_line_keeps_its_older_date(md, paths):
    """2026-10-24 審查 r9b gp1：暫定標籤加進來後 VOO 第一個寫法超寬，_fit 先丟的是日期後綴；Nasdaq 落後超過一個
    場次時，VOO 前一天的暫定收盤在卡上就看不出是前一天。日期要比漲跌、月線狀態先保住；altText 也要標暫定。"""
    snap = make_snapshot(NOW)
    old = date.fromisoformat(snap["rows"]["us.etf.VOO"]["data_date"]) - timedelta(days=1)
    snap["rows"]["us.etf.VOO"]["data_date"] = old.isoformat()
    snap["rows"]["us.etf.VOO"]["source"] = md.market_calc.SOURCE_CBOE_FALLBACK
    write_snapshot(paths, snap)
    reply = md.handle("", now=NOW, paths=paths)
    line = next(text for text in texts(summary_bubble(md, reply.flex, "美股")["body"]) if text.startswith("標普500（VOO）"))
    assert "（暫定）" in line and f"（{old.month}/{old.day}）" in line and md.row_width(line) <= 24, line
    assert "（暫定）" in reply.alt_text
    assert "（暫定，+0.52%）" in md.handle(" VOO", now=NOW, paths=paths).alt_text  # F2-13：暫定與漲跌幅同一組括號


def test_older_etf_line_drops_the_change_before_the_date(md, paths):
    """同一個規則不只暫定列：名稱寬的 QQQ 以前資料日較舊時日期也會先被丟。"""
    snap = make_snapshot(NOW)
    old = date.fromisoformat(snap["rows"]["us.etf.QQQ"]["data_date"]) - timedelta(days=1)
    snap["rows"]["us.etf.QQQ"]["data_date"] = old.isoformat()
    snap["rows"]["us.etf.QQQ"]["values"]["chg_pct"] = -12.34
    write_snapshot(paths, snap)
    line = next(text for text in texts(summary_bubble(md, md.handle("", now=NOW, paths=paths).flex, "美股")["body"])
                if text.startswith("那斯達克100（QQQ）"))
    assert f"（{old.month}/{old.day}）" in line and md.row_width(line) <= 24, line


def test_summary_alt_tags_provisional_only_on_sentences_built_from_those_rows(md, paths):
    """2026-10-24 審查 r9c gp1 nit 1：VOO 過期、QQQ 暫定時原本寫成「美股資料過期（暫定）」。"""
    snap = make_snapshot(NOW)
    snap["rows"]["oneliner.us"]["values"]["text"] = ""  # 沒有一句話：退回 VOO 的寫法
    snap["rows"]["us.etf.VOO"]["stale_after"] = NOW - 60
    snap["rows"]["us.etf.QQQ"]["source"] = md.market_calc.SOURCE_CBOE_FALLBACK
    write_snapshot(paths, snap)
    alt = md.handle("", now=NOW, paths=paths).alt_text
    assert "美股資料過期" in alt and "（暫定）" not in alt, alt


# ── 2026-10-24 第 9 輪批 1（A1 軌）N7：_bubble 自己擋超過 3 顆按鈕 ───────────────────────────────
THREE_BUTTONS = (("標普500 細節", "/盤勢 VOO"), ("那斯達克 細節", "/盤勢 QQQ"), ("半導體 細節", "/盤勢 SOXX"))


def test_bubble_refuses_more_than_three_buttons(md):
    """審查 r9 s1-N7：「每泡泡 ≤3 顆按鈕」以前只有測試在看；第 4 顆按鈕要在 _bubble 就出錯，不是悄悄多一顆或少一顆。"""
    assert md.MAX_BUTTONS == 3
    for count in range(4):
        bubble = md._bubble("標題", [], buttons=THREE_BUTTONS[:count])
        assert sum(1 for node in bubble["footer"]["contents"] if "action" in node) == count
    with pytest.raises(ValueError, match="at most 3 buttons"):
        md._bubble("標題", [], buttons=(*THREE_BUTTONS, ("台股細節", "/盤勢 0050")))
    with pytest.raises(ValueError):
        md._bubble("標題", [], buttons=[("按鈕", "/盤勢")] * 5)  # list 也一樣


def test_a_fourth_button_becomes_a_visible_failed_bubble_in_a_carousel(md, paths, monkeypatch):
    """輪播裡有一張多放了第 4 顆按鈕：那張換成固定短卡、錯誤進 render_errors（不是悄悄截斷），其他張照送。"""
    write_snapshot(paths, make_snapshot(NOW))
    good = md.handle("", now=NOW, paths=paths)
    names = [builder for _card, builder in md.SUMMARY_CARDS]
    position = names.index("_card_us")

    def four_buttons(ctx, head, subtitle=None):
        return md._bubble(head, [md._text("內容")], subtitle=subtitle, buttons=(*THREE_BUTTONS, ("多的", "/盤勢 0050")))

    four_buttons.__name__ = "_card_us"
    monkeypatch.setattr(md, "_card_us", four_buttons)
    reply = md.handle("", now=NOW, paths=paths)
    assert reply.render_errors == ("_card_us:ValueError",)
    assert len(reply.flex["contents"]) == len(good.flex["contents"])
    for index, (now_bubble, was) in enumerate(zip(reply.flex["contents"], good.flex["contents"])):
        if index == position:
            assert_failed_bubble(md, now_bubble, f"{md.SUMMARY_CARDS[index][0]}{md._page(index + 1)}",
                                 swipe=md.TEXT_SWIPE if index == 0 else None)
        else:
            assert now_bubble == was, index
    assert md.status_summary(now=NOW, paths=paths)["render_error"] == "bubble:_card_us:ValueError"


# ── 2026-10-24 第 9 輪批 1（A1 軌）N2：美股那句一句話的主詞 ─────────────────────────────────
def summary_bubble(md, flex, name):
    """依 SUMMARY_CARDS 的卡名取輪播裡的那一張：前面多一張卡之後位置會移動，測試不寫死索引。"""
    names = [card for card, _builder in md.SUMMARY_CARDS]
    return flex["contents"][names.index(name)]


def test_the_us_one_liner_is_about_the_sp500_on_the_card_and_in_the_alt_text(md, paths):
    """審查 r9 s1-N2：這句只看 VOO，主詞不能再是「美股」——摘要第一張把三檔分組之後會互相矛盾。"""
    write_snapshot(paths, make_snapshot(NOW))
    reply = md.handle("", now=NOW, paths=paths)
    us = texts(summary_bubble(md, reply.flex, "美股")["body"])
    assert ONELINER_US in us and ONELINER_US.startswith("標普收在月線")
    assert not any(text.startswith("美股收在") for text in us)
    assert reply.alt_text == f"盤勢：{ONELINER_US}｜{ONELINER_TW}｜資料 10/16"
    assert reply.render_errors == ()


def test_every_us_one_liner_fits_a_summary_row_and_the_alt_text_pattern(md):
    """改主詞不能讓美股卡的 _note_line 因為太寬把這一句拿掉（「標普500」在 8/144 個組合會超過 24）。"""
    day = date(2026, 10, 16)
    fact = market_calc.Fact
    for above in (True, False):
        for breadth in (None, 0.0, 41.4, 99.6, 100.0):
            for label in (None, *market_calc.VOL_LABELS):
                state = {"VOO.above_ma20": fact(above, day)}
                if breadth is not None:
                    state["VOO.breadth20_pct"] = fact(breadth, day)
                if label is not None:
                    state["VOO.vol_label"] = fact(label, day)
                text = market_calc.one_liner(state, "us")
                assert text.startswith("標普收在月線") and md._ONELINER_US_RE.fullmatch(text), text
                assert md.row_width(text) <= md.MAX_ROW_WIDTH, text


def test_a_snapshot_written_before_the_rename_still_renders(md, paths):
    """job 還沒重簽章、快照裡還是舊句子「美股收在月線上…」：卡上照列（bot 不改 job 寫的字），altText 不收（不合新的固定句型）、
    退回 bot 自己組的 VOO 寫法；不丟例外。"""
    snap = make_snapshot(NOW)
    old = "美股收在月線上、成分股站上月線 62%、量偏少"
    snap["rows"]["oneliner.us"]["values"]["text"] = old
    write_snapshot(paths, snap)
    reply = md.handle("", now=NOW, paths=paths)
    assert reply.render_errors == () and old in texts(summary_bubble(md, reply.flex, "美股")["body"])
    assert reply.alt_text.startswith("盤勢：美股 VOO 在月線上｜")


# ── 2026-10-24 第 9 輪批 1（A1 軌）A9：重點摘要（輪播第一張） ─────────────────────────────────
# 每一列都是 f(ctx) → (文字, 顏色) 的獨立函式：逐列直接呼叫、把各自的狀態空間列舉完（三檔 ETF 各 上／下／過期／暫缺／月線狀態缺 ×
# 資料日落後與否；加權與外資的每種組合；VIX 的各區間 × 牆距的各種情形…），不做跨列的笛卡兒積；再用整個輪播量大小、檢查不丟例外。
# 輪播有逐泡泡隔離（壞的一張換成「這張卡暫時出不來」），所以這裡直接呼叫 builder，或一定檢查 reply.render_errors == ()。
DIGEST = "重點摘要"
DAY, PREV = date(2026, 10, 16), date(2026, 10, 15)  # make_snapshot(NOW) 的最新場次（NOW 是週六台北 16:30）與前一天
US_SYMS = ("VOO", "QQQ", "SOXX")
SIX_PREFIXES = ["美股", "台股", "風險", "利率", "事件", "川普"]
# 重點摘要 footer 最多幾句註記（2026-10-28 批 1 審查 F2-1／F2-2／F2-4／F2-14 之後）：資料日、暫定收盤、美股尚未取得、台股休市／尚未取得、
# 牆距基準、「不是退讓的機率」、來源（第 6 列退回時這兩句換成川普壓力指數的狀態句，所以最多還是這 7 句）
MAX_DIGEST_NOTES = 7
TACO_MEANING_SOURCE = (market_calc.TEXT_TACO_MEANING, market_calc.TEXT_TACO_SOURCE)
ETF_STATES = ("up", "down", "stale", "missing", "flagless")


def digest_snapshot(vix=18.04):
    """make_snapshot(NOW)，但美股三檔一樣新（原本 SOXX 晚一天）、VIX 18.04：預設沒有任何一列落後。"""
    snap = make_snapshot(NOW)
    snap["rows"]["us.etf.SOXX"]["data_date"] = DAY.isoformat()
    snap["rows"]["macro.VIX"]["values"]["value"] = vix
    return snap


def digest_bubble(md, snap, now=NOW):
    return md._card_digest(md._Ctx(snap, now), f"{DIGEST}（1/5）", md.TEXT_SWIPE)


def digest_rows(md, snap, now=NOW):
    bubble = digest_bubble(md, snap, now)
    return [(node["text"], node.get("color")) for node in bubble["body"]["contents"]], bubble


def digest_texts(md, snap, now=NOW):
    return [text for text, _color in digest_rows(md, snap, now)[0]]


def set_etf(snap, sym, state, day=DAY):
    if state == "missing":
        snap["rows"][f"us.etf.{sym}"] = _never()
        return
    row = _row(_etf(612.34, 0.52, state != "down"), NOW, day)
    if state == "flagless":
        row["values"]["above_ma20"] = None
    if state == "stale":
        row["stale_after"] = NOW - 1
    snap["rows"][f"us.etf.{sym}"] = row


def set_taiex(snap, state, chg=0.52, above=True, day=DAY):
    if state == "never":
        snap["rows"]["tw.taiex"] = _never()
        return
    row = _row(dict(snap["rows"]["tw.taiex"]["values"], chg_pct=chg, above_ma20=above), NOW, day)
    if state == "stale":
        row["stale_after"] = NOW - 1
    snap["rows"]["tw.taiex"] = row


def set_inst(snap, state, foreign=-123.4, day=DAY):
    if state == "never":
        snap["rows"]["tw.inst3"] = _never()
        return
    row = _row(dict(snap["rows"]["tw.inst3"]["values"], foreign_yi=foreign), NOW, day)
    if state == "stale":
        row["stale_after"] = NOW - 1
    snap["rows"]["tw.inst3"] = row


def set_vix(snap, state, value=18.04, day=DAY):
    if state == "never":
        snap["rows"]["macro.VIX"] = _never()
        return
    row = _row({"value": value, "chg": 0.12, "chg_pct": None, "source": "CBOE"}, NOW, day)
    if state == "stale":
        row["stale_after"] = NOW - 1
    snap["rows"]["macro.VIX"] = row


def set_options(snap, state="fresh", *, call=3.0, put=-4.0, status="ok", day=DAY):
    if state == "never":
        snap["rows"]["us.options.QQQ"] = _never()
        return
    row = _row(_options(540.0), NOW, day)
    row["values"].update(call_wall_dist_pct=call, put_wall_dist_pct=put, wall_status=status)
    if state == "stale":
        row["stale_after"] = NOW - 1
    snap["rows"]["us.options.QQQ"] = row


def set_ust(snap, state, value=4.96, chg=0.03, day=DAY):
    if state == "never":
        snap["rows"]["macro.UST10Y"] = _never()
        return
    row = _row({"value": value, "chg": chg, "chg_pct": None, "source": "美國財政部"}, NOW, day)
    if state == "stale":
        row["stale_after"] = NOW - 1
    snap["rows"]["macro.UST10Y"] = row


def set_events(snap, items, state="fresh"):
    row = _row({"items": items}, NOW, _today())
    if state == "stale":
        row["stale_after"] = NOW - 1
    snap["rows"]["events.next"] = _never() if state == "never" else row


def row_ctx(md, snap, now=NOW):
    return md._Ctx(snap, now)


US_WORDS = {"在月線上": "up", "月線上": "up", "在月線下": "down", "月線下": "down", "過期": "stale", "暫缺": "missing"}
US_NAMES = {"標普": "VOO", "那指": "QQQ", "半導體": "SOXX"}


def parse_us_row(text):
    """「美股 10/15：標普、那指在月線上，半導體暫缺」→ ("10/15", {"VOO": "up", "QQQ": "up", "SOXX": "missing"})。"""
    head, colon, body = text.partition("：")
    assert colon and head.startswith("美股"), text
    found = {}
    for clause in body.split("，"):
        clause = re.sub(r"（[^）]*）$", "", clause)  # 三檔都過期的「（資料日 10/16）」不是狀態詞（「（暫定）」在列名後面，F3-1）
        word = next(w for w in sorted(US_WORDS, key=len, reverse=True) if clause.endswith(w))
        who = clause[: -len(word)]
        if who.endswith("都") and who != "三檔都":  # 三檔同向：「標普、那指、半導體都在月線上」（F2-8）
            who = who[:-1]
            assert who.count("、") == 2, text
        for name in (US_NAMES if who == "三檔都" else who.split("、")):
            assert name in US_NAMES and US_NAMES[name] not in found, (text, name)
            found[US_NAMES[name]] = US_WORDS[word]
    return head[len("美股"):].strip(), found


def iter_us_cases():
    snap = digest_snapshot()
    for lag in (False, True):
        for states in itertools.product(ETF_STATES, repeat=3):
            for sym, state in zip(US_SYMS, states):
                set_etf(snap, sym, state, PREV if lag else DAY)
            yield snap, lag, states


def iter_tw_cases():
    snap = digest_snapshot()
    taiexes = [("never", None, None), ("stale", None, None)] + [
        ("fresh", chg, above) for chg in (1.81, -1.81, 0.0, -0.004, None, -99.99, 1000.0) for above in (True, False, None)]
    insts = [("never", None), ("stale", None), ("fresh", None)] + [
        ("fresh", yi) for yi in (-816.6, 1234.6, 0.0, 0.04, -99999.0, 99999.0)]
    for lag in (False, True):
        taiex_day = PREV if lag else DAY
        for (t_state, chg, above), (i_state, foreign), other_day in itertools.product(taiexes, insts, (False, True)):
            inst_day = (DAY if lag else PREV) if other_day else taiex_day
            set_taiex(snap, t_state, chg, above, taiex_day)
            set_inst(snap, i_state, foreign, inst_day)
            yield snap, dict(lag=lag, taiex=(t_state, chg, above), inst=(i_state, foreign), taiex_day=taiex_day,
                             inst_day=inst_day)


VIX_VALUES = (0.0, 12.34, 18.04, 19.949, 19.96, 20.0, 29.99, 30.0, 99.99, 999.9)
WALL_DISTANCES = ((3.0, -4.0), (3.0, -0.6), (3.0, -0.96), (3.0, -0.94), (0.8, -4.0), (0.8, -0.3), (0.3, -0.8),
                  (-0.3, 0.4), (3.0, 0.04), (None, None), (3.0, None), (None, -0.5), (0.5, -0.5), (0.95, -4.0),
                  (3.0, -0.95), (-0.95, -4.0), (0.949, 3.0), (3.0, 0.05))  # 0.95／-0.95 是四捨五入進位的邊界：顯示 ±1.0%，不算『不到 1%』


def iter_risk_cases():
    snap = digest_snapshot()
    vixes = [("never", None), ("stale", None), ("fresh", None)] + [("fresh", value) for value in VIX_VALUES]
    options = [("never", None, None), ("stale", 3.0, -0.6), ("bad", 3.0, -0.6)] + [("fresh", c, p) for c, p in WALL_DISTANCES]
    for (v_state, value), (o_state, call, put) in itertools.product(vixes, options):
        set_vix(snap, v_state, value)
        set_options(snap, "fresh" if o_state == "bad" else o_state, call=call, put=put,
                    status="insufficient" if o_state == "bad" else "ok")
        yield snap, dict(vix=(v_state, value), options=(o_state, call, put))


def iter_event_cases():
    snap = digest_snapshot()
    at = datetime(2026, 10, 17, 16, 30, tzinfo=TPE)
    offsets = (timedelta(hours=1), timedelta(hours=30), timedelta(days=3), timedelta(days=12), timedelta(days=40))
    items = [{"name": name, "kind": "econ"} for name in market_calc.US_EVENT_LABELS]
    items += [{"name": kind.upper(), "kind": kind} for kind in ("fomc", "cpi", "ppi", "nfp", "pce", "gdp")]
    items += [{"name": symbol, "kind": "earnings", "time_known": False, "estimated": estimated}
              for symbol in ("NVDA", "GOOGL", "BRK.B") for estimated in (True, False, None)]
    items += [{"name": "名" * 24, "kind": "econ"}]  # 外部來源的名稱可以到 24 個字
    for item, offset in itertools.product(items, offsets):
        set_events(snap, [dict(item, at=(at + offset).timestamp())])
        yield snap, item, offset
    for state in ("stale", "never"):
        set_events(snap, [], state)
        yield snap, {"state": state}, None
    set_events(snap, [])
    yield snap, {"state": "empty"}, None


# ── 輪播：第一張、張數、標題、按鈕 ───────────────────────────────────────────────────────
def test_the_digest_is_the_first_of_five_summary_cards(md, paths):
    assert md.SUMMARY_CARDS[0] == (DIGEST, "_card_digest") and md.SUMMARY_CARD_COUNT == 5
    assert len(md.SUMMARY_CARDS) == 5 and md._page(1) == "（1/5）"
    write_snapshot(paths, make_snapshot(NOW))
    reply = md.handle("", now=NOW, paths=paths)
    assert reply.render_errors == () and len(reply.flex["contents"]) == 5
    assert [b["header"]["contents"][0]["text"] for b in reply.flex["contents"]] == [
        "重點摘要（1/5）｜10/17 16:20 更新", "美股（2/5）｜10/16 收盤", "台股（3/5）｜10/16 收盤",
        "期權與資金（4/5）｜10/16 收盤", "宏觀與事件（5/5）｜10/17 16:20 更新"]
    assert md.TEXT_SWIPE == "往左滑還有 4 張" and md.TEXT_SWIPE in texts(reply.flex["contents"][0]["header"])
    assert all(md.TEXT_SWIPE not in texts(b["header"]) for b in reply.flex["contents"][1:])
    assert md.TEXT_USAGE.splitlines()[1] == "/盤勢 → 5 張摘要卡（重點摘要、美股、台股、期權與資金、宏觀與事件）"
    assert reply.alt_text.startswith("盤勢：") and reply.alt_text == f"盤勢：{ONELINER_US}｜{ONELINER_TW}｜資料 10/16"  # altText 照舊


def test_digest_rows_colors_notes_and_buttons_for_a_normal_snapshot(md):
    rows, bubble = digest_rows(md, digest_snapshot())
    assert [text for text, _color in rows] == [
        "美股：標普、半導體在月線上，那指在月線下",
        "台股：加權 +0.52% 月線上，外資賣超 123.4 億",
        "風險：VIX 18.0 平穩",
        "利率：10 年債 5.09%（+12bp）",
        "事件：Consumer Price Index，還有 3 天",
        "川普壓力指數 +9.0（高壓，任期內第 88 百分位）",  # 第 6 列：川普壓力指數優先（2026-10-28 整合 A1×TACO）
    ]
    assert [color for _text, color in rows] == [md.GRAY, md.BLUE, md.BLUE, md.GRAY, md.GRAY, md.GRAY]
    # footer（F2-1／F2-2）：資料日多了 10 年債和川普壓力指數；第 6 列放川普壓力指數，所以接著「不是退讓的機率」和來源兩句
    assert texts(bubble["footer"])[:4] == [
        "資料日：美股 10/16、台股 10/16、VIX 10/16、10 年債 10/16、川普壓力指數 10/16", *TACO_MEANING_SOURCE, DISCLAIMER]
    buttons = [(a["label"], a["text"]) for a in actions(bubble)]
    assert buttons == [("期權與資金細節", "/盤勢 期權"), ("新聞與宏觀", "/盤勢 新聞"), ("台股細節", "/盤勢 0050")]
    assert {text for _label, text in buttons} <= set(md.SHIPPED_MARKET_TEXTS)
    assert not any(word in text for text in texts(bubble) for word in SUMMARY_JARGON)


@pytest.mark.parametrize("variant", ["normal", "worst", "stale", "half_never"])
def test_the_digest_has_exactly_six_rows_in_order_and_fits_on_every_variant(md, paths, variant):
    write_snapshot(paths, _variants()[variant])
    reply = md.handle("", now=NOW, paths=paths)
    assert reply.render_errors == () and md.TEXT_BUBBLE_FAILED not in texts(reply.flex)
    bubble = summary_bubble(md, reply.flex, DIGEST)
    body = bubble["body"]["contents"]
    assert [node["text"][:2] for node in body] == SIX_PREFIXES  # _summary_bubble 會靜默丟第 7 列：這裡鎖住恰好 6 列
    for node in body:
        assert node["type"] == "text" and md.row_width(node["text"]) <= 24 and "…" not in node["text"], node["text"]
    assert md.card_bytes(bubble) <= md.SUMMARY_BUBBLE_MAX_BYTES
    assert md.card_bytes(reply.flex) <= 28 * 1024
    assert len(bubble["footer"]["contents"]) <= MAX_DIGEST_NOTES + 1 + md.MAX_BUTTONS  # 至多 7 句註記、免責句、3 顆按鈕


# ── 第 1 列：美股 ────────────────────────────────────────────────────────────────────────
def test_digest_us_row_in_every_state(md):
    seen = set()
    for snap, lag, states in iter_us_cases():
        text, color = md._digest_us_row(row_ctx(md, snap))
        seen.add(text)
        stamp, found = parse_us_row(text)
        want = {sym: ("missing" if state == "flagless" else state) for sym, state in zip(US_SYMS, states)}
        assert found == want, (lag, states, text)  # 每一檔都在、狀態對（過期、暫缺不能被拿掉）
        assert stamp == ("10/15" if lag and {"up", "down"} & set(states) else ""), (lag, states, text)
        assert md.row_width(text) <= 24 and "…" not in text, text
        same = len(set(want.values())) == 1
        assert ("三檔都" in text) == (same and want["VOO"] in ("stale", "missing")), text  # 過期、暫缺才寫「三檔都」
        assert ("標普、那指、半導體都" in text) == (same and want["VOO"] in ("up", "down")), text  # 同向：點名三檔（F2-8）
        kinds = set(want.values())
        assert color == (md.BLUE if kinds == {"up"} else md.ORANGE if kinds == {"down"} else md.GRAY), (states, color)
    # 4 種有效狀態（月線狀態缺＝暫缺）的 4³ 種組合；落後時至少一檔有月線狀態的 56 種多一個日期，三檔都過期的那一種另外多一個
    # （「資料日 10/15」和「資料日 10/16」，F2-8）
    assert len(seen) == 64 + 56 + 1


def test_digest_us_row_and_footer_for_every_state_lag_and_provisional_combination(md):
    """三檔 ETF 各 上／下／過期／暫缺／月線狀態缺 × 資料日落後與否 × 各檔是不是 CBOE 暫定收盤：列的寫法只在『列上有月線狀態的檔裡
    有暫定的、而且放得下』時在列名（和日期）後面多一個「（暫定）」（F2-8；位置是第 2 輪複審 F3-1，以前接在列尾；其他寫法一個字都
    不變），footer 的固定句恰好在「至少一檔有數字的 ETF 是暫定」時出現；「資料日」那行只列有月線狀態的檔。"""
    plain = {}
    for snap, lag, states in iter_us_cases():
        plain[(lag, states)] = md._digest_us_row(row_ctx(md, snap))
    checked = 0
    for lag in (False, True):
        for states in itertools.product(ETF_STATES, repeat=3):
            snap = digest_snapshot()
            for sym, state in zip(US_SYMS, states):
                set_etf(snap, sym, state, PREV if lag else DAY)
            for flags in itertools.product((False, True), repeat=3):
                for sym, flag in zip(US_SYMS, flags):
                    snap["rows"][f"us.etf.{sym}"]["source"] = market_calc.SOURCE_CBOE_FALLBACK if flag else "合成"
                ctx = row_ctx(md, snap)
                plain_text, plain_color = plain[(lag, states)]
                name, colon, rest = plain_text.partition("：")
                tagged = f"{name}{market_calc.TEXT_PROVISIONAL}{colon}{rest}"  # 「美股（暫定）：…」「美股 10/15（暫定）：…」（F3-1）
                shown = any(flag and state in ("up", "down") for state, flag in zip(states, flags))
                want = tagged if shown and md.row_width(tagged) <= 24 else plain_text
                assert md._digest_us_row(ctx) == (want, plain_color), (lag, states, flags)
                notes = md._digest_notes(ctx)
                numbers = [flag and state in ("up", "down", "flagless") for state, flag in zip(states, flags)]
                assert (market_calc.TEXT_PROVISIONAL_CLOSE in notes) == any(numbers), (lag, states, flags, notes)
                own = [note for note in notes if note not in TACO_MEANING_SOURCE]  # 川普壓力指數的兩句是另一件事
                assert notes.count(market_calc.TEXT_PROVISIONAL_CLOSE) <= 1 and len(own) <= 2
                dated = [state in ("up", "down") for state in states]
                assert any("美股" in note for note in notes[:1]) == any(dated), (lag, states, flags, notes)
                bubble = md._card_digest(ctx, f"{DIGEST}（1/5）", md.TEXT_SWIPE)
                assert md.card_bytes(bubble) <= md.SUMMARY_BUBBLE_MAX_BYTES
                checked += 1
    assert checked == 2 * 5**3 * 2**3


@pytest.mark.parametrize("states, expected", [
    (("up", "down", "down"), "美股：標普在月線上，那指、半導體在月線下"),
    (("up", "up", "up"), "美股：標普、那指、半導體都在月線上"),  # F2-8：三檔同向要點名，不寫「三檔都」
    (("down", "down", "down"), "美股：標普、那指、半導體都在月線下"),
    (("down", "up", "up"), "美股：那指、半導體在月線上，標普在月線下"),
    (("up", "up", "missing"), "美股：標普、那指在月線上，半導體暫缺"),
    (("up", "down", "missing"), "美股：標普在月線上，那指在月線下，半導體暫缺"),
    (("up", "stale", "missing"), "美股：標普在月線上，那指過期，半導體暫缺"),
    (("up", "flagless", "up"), "美股：標普、半導體在月線上，那指暫缺"),
    (("stale", "stale", "stale"), "美股：三檔都過期（資料日 10/16）"),  # F2-8：其他列過期都有資料日，這一列也要有
    (("missing", "missing", "missing"), "美股：三檔都暫缺"),
])
def test_digest_us_row_examples(md, states, expected):
    snap = digest_snapshot()
    for sym, state in zip(US_SYMS, states):
        set_etf(snap, sym, state)
    assert md._digest_us_row(row_ctx(md, snap))[0] == expected


def test_digest_us_row_puts_the_date_in_front_and_shortens_before_it_drops_anything(md):
    snap = digest_snapshot()
    for sym, state in zip(US_SYMS, ("up", "down", "down")):  # 10/23 那一天：ETF 10/21，VIX 與 10 年債 10/22
        set_etf(snap, sym, state, date(2026, 10, 21))
    set_vix(snap, "fresh", 19.0, date(2026, 10, 22))
    set_ust(snap, "fresh", day=date(2026, 10, 22))
    ctx = row_ctx(md, snap)
    assert md._digest_us_row(ctx)[0] == "美股 10/21：標普在月線上，那指、半導體在月線下"
    assert md._digest_notes(ctx)[0] == "資料日：美股 10/21、台股 10/16、VIX 10/22、10 年債 10/22、川普壓力指數 10/16"
    # 日期 + 三組 = 25.0 放不下：先把「在月線上」縮成「月線上」，日期和暫缺的那一檔都留著
    for sym, state in zip(US_SYMS, ("up", "down", "missing")):
        set_etf(snap, sym, state, PREV)
    text = md._digest_us_row(row_ctx(md, snap))[0]
    assert text == "美股 10/15：標普月線上，那指月線下，半導體暫缺" and md.row_width(text) == 23.0


def test_digest_us_row_expected_session_comes_from_the_other_rows_or_the_publisher(md):
    def row(snap):
        return md._digest_us_row(row_ctx(md, snap))[0]

    snap = digest_snapshot()
    assert row(snap) == "美股：標普、半導體在月線上，那指在月線下"  # 沒有任何列比 ETF 新
    # a. VIX（前一次結算的日期）比 ETF 新 → ETF 落後
    set_vix(snap, "fresh", 18.04, date(2026, 10, 19))
    assert row(snap).startswith("美股 10/16：")
    # b. 10 年債比 ETF 新也算
    snap = digest_snapshot()
    set_ust(snap, "fresh", day=date(2026, 10, 19))
    assert row(snap).startswith("美股 10/16：")
    # c. 期權鏈（收盤後才收進來）比 ETF 新也算——三檔期權都看，不只摘要用的 QQQ
    for symbol in ("QQQ", "SPY", "SOXX"):
        snap = digest_snapshot()
        snap["rows"][f"us.options.{symbol}"]["data_date"] = "2026-10-19"
        assert row(snap).startswith("美股 10/16："), symbol
    # d. 比 ETF 新的列過期了：不算
    snap = digest_snapshot()
    set_vix(snap, "stale", 18.04, date(2026, 10, 19))
    set_options(snap, "stale", day=date(2026, 10, 19))
    assert row(snap).startswith("美股：")
    # e. publisher 寫了 progress.us_expected_day（第 9 輪 A19）：用它——所有列一樣落後時也看得出來
    snap = digest_snapshot()
    snap["progress"]["us_expected_day"] = "2026-10-19"
    assert row(snap).startswith("美股 10/16：")
    snap["progress"]["us_expected_day"] = "2026-10-14"  # 比 ETF 還舊（不合理）：資料沒有落後，不加日期
    assert row(snap).startswith("美股：")
    # f. 壞值當沒有，退回其他列的證據
    for junk in ("2026-13-45", "10/19", 20261019, True, None, [], {"day": "2026-10-19"}):
        snap = digest_snapshot()
        snap["progress"]["us_expected_day"] = junk
        assert row(snap).startswith("美股："), junk
        set_vix(snap, "fresh", 18.04, date(2026, 10, 19))
        assert row(snap).startswith("美股 10/16："), junk
    # g. 三檔日期不同：寫最舊的那天，footer 寫「10/15 至 10/16」
    snap = digest_snapshot()
    set_etf(snap, "QQQ", "down", PREV)
    ctx = row_ctx(md, snap)
    assert md._digest_us_row(ctx)[0].startswith("美股 10/15：")
    assert md._digest_notes(ctx)[0] == "資料日：美股 10/15 至 10/16、台股 10/16、VIX 10/16、10 年債 10/16、川普壓力指數 10/16"
    # h. 過期、暫缺的那檔不算「這一列的資料日」
    snap = digest_snapshot()
    set_etf(snap, "QQQ", "stale", PREV)
    set_etf(snap, "SOXX", "missing")
    assert md._digest_us_row(row_ctx(md, snap))[0] == "美股：標普在月線上，那指過期，半導體暫缺"
    # i. 沒有 progress 欄位的舊快照照常
    snap = digest_snapshot()
    del snap["progress"]
    assert row(snap).startswith("美股：")


# ── 第 2 列：台股 ────────────────────────────────────────────────────────────────────────
def test_digest_tw_row_in_every_state(md):
    seen = set()
    for snap, case in iter_tw_cases():
        text, color = md._digest_tw_row(row_ctx(md, snap))
        seen.add(text)
        t_state, chg, above = case["taiex"]
        i_state, foreign = case["inst"]
        taiex_day, inst_day = case["taiex_day"], case["inst_day"]
        head, _colon, body = text.partition("：")
        parts = body.split("，")
        assert head.startswith("台股") and md.row_width(text) <= 24 and "…" not in text, (case, text)
        day = taiex_day if t_state == "fresh" else inst_day if i_state == "fresh" else None
        assert head[len("台股"):].strip() == ("10/15" if day == PREV else ""), (case, text)  # 預期的最新場次是 10/16
        if t_state == "fresh":
            bits = ([md._pct(chg, 2)] if chg is not None else []) + (
                ["月線上" if above else "月線下"] if above is not None else [])
            assert parts[0] == (" ".join(("加權", *bits)) if bits else "加權暫缺"), (case, text)
            assert color == (md.GRAY if above is None else md.BLUE if above else md.ORANGE), (case, color)
        else:
            gap = "加權暫缺" if t_state == "never" else f"加權過期（資料日 {taiex_day.month}/{taiex_day.day}）"
            assert parts[0] in (gap, "加權過期"), (case, text)
            assert color == md.GRAY
        same_day = t_state != "fresh" or inst_day == taiex_day
        wanted = (md._inst_part("外資", foreign) if i_state == "fresh" and same_day
                  else {"stale": "外資過期", "never": "外資暫缺"}.get(i_state))
        assert len(parts) in (1, 2), (case, text)
        if len(parts) == 2:
            assert wanted is not None and parts[1] == wanted, (case, text)
        if day != PREV and wanted is not None:
            assert len(parts) == 2, (case, text)  # 沒有日期的列最寬 24.0：外資一定放得下
        if i_state == "fresh" and not same_day:
            assert "外資" not in text, (case, text)  # 絕不把另一天的外資放在這天的加權後面
    assert len(seen) > 150


def test_digest_tw_row_examples_and_what_it_drops_first(md):
    snap = digest_snapshot()

    def row():
        return md._digest_tw_row(row_ctx(md, snap))[0]

    set_taiex(snap, "fresh", -1.81, False)
    set_inst(snap, "fresh", -816.6)
    assert row() == "台股：加權 -1.81% 月線下，外資賣超 816.6 億"
    set_inst(snap, "fresh", 1234.6)
    assert row() == "台股：加權 -1.81% 月線下，外資買超 1,235 億"  # ≥1000 億不帶小數（_amount_yi，和台股卡同一組）
    set_inst(snap, "fresh", 0.04)
    assert row() == "台股：加權 -1.81% 月線下，外資持平"
    set_inst(snap, "fresh", None)
    assert row() == "台股：加權 -1.81% 月線下，外資暫缺"
    set_inst(snap, "stale")
    assert row() == "台股：加權 -1.81% 月線下，外資過期"
    set_taiex(snap, "stale")
    assert row() == "台股：加權過期（資料日 10/16），外資過期"
    set_taiex(snap, "never")
    set_inst(snap, "fresh", 45.6)
    assert row() == "台股：加權暫缺，外資買超 45.6 億"
    # 日期放前面；放不下先丟外資、不丟日期
    set_taiex(snap, "fresh", -1.81, False, PREV)
    set_inst(snap, "fresh", -816.6, PREV)
    assert row() == "台股 10/15：加權 -1.81% 月線下"  # 加上外資是 24.5
    set_inst(snap, "fresh", -12.3, PREV)
    assert row() == "台股 10/15：加權 -1.81% 月線下，外資賣超 12.3 億"  # 剛好 24.0
    # 外資不是同一天：不放（昨天的外資不能接在今天的加權後面）
    set_inst(snap, "fresh", -12.3, DAY)
    assert row() == "台股 10/15：加權 -1.81% 月線下"
    set_taiex(snap, "fresh", -1.81, False, DAY)
    set_inst(snap, "fresh", -12.3, PREV)
    assert row() == "台股：加權 -1.81% 月線下"


def test_digest_tw_row_expected_session(md):
    snap = digest_snapshot()

    def row():
        return md._digest_tw_row(row_ctx(md, snap))[0]

    set_taiex(snap, "fresh", 0.52, True, PREV)
    set_inst(snap, "fresh", -12.3, PREV)
    assert row().startswith("台股 10/15：")  # 0050、成交金額、漲跌家數都已經是 10/16：加權落後
    for row_id in ("tw.etf.0050", "tw.turnover", "tw.ad"):
        snap["rows"][row_id]["data_date"] = PREV.isoformat()
    assert row().startswith("台股：")  # 全部一起落後：偵測不到（footer 的「資料日」看得出來）
    snap["progress"]["tw_expected_day"] = "2026-10-16"  # publisher 寫了（第 9 輪 A19）：照它的
    assert row().startswith("台股 10/15：")
    snap["progress"]["tw_expected_day"] = "not a date"
    assert row().startswith("台股：")
    del snap["progress"]
    assert row().startswith("台股：")


# ── 第 3 列：風險 ────────────────────────────────────────────────────────────────────────
def half_up_1(value):
    """獨立於 market_dashboard 的一位小數四捨五入進位（Decimal(repr)＋ROUND_HALF_UP）。測試以前用 round()：round(0.95, 1) 是 0.9
    （二進位的 0.95 比 0.95 小一點），『不到 1%』判成立，印出來卻是 +1.0%——判斷和印出來的字分家（批 1 審查 F2-16）。"""
    return Decimal(repr(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def expected_wall(md, call, put):
    near = []
    for order, (name, dist) in enumerate((("賣權牆", put), ("買權牆", call))):
        if dist is not None and abs(half_up_1(dist)) < 1:
            near.append((abs(half_up_1(dist)), order, f"QQQ {name}距 {md._pct(dist, 1)}"))
    return min(near)[2] if near else None


def test_digest_risk_row_in_every_state(md):
    seen = set()
    for snap, case in iter_risk_cases():
        v_state, value = case["vix"]
        o_state, call, put = case["options"]
        text, color = md._digest_risk_row(row_ctx(md, snap))
        seen.add(text)
        assert text.startswith("風險：") and md.row_width(text) <= 24 and "…" not in text, (case, text)
        vix_text, _comma, wall_text = text[len("風險："):].partition("，")
        if v_state == "fresh" and value is not None:
            wanted, want_color = (f"VIX {value:.1f} {md._vix_word(value)}",), md.BLUE if value < 20 else md.ORANGE
        elif v_state == "stale":
            wanted, want_color = ("VIX 過期（資料日 10/16）", "VIX 過期"), md.GRAY
        else:
            wanted, want_color = ("VIX 暫缺",), md.GRAY
        assert vix_text in wanted and color == want_color, (case, text)
        want_wall = expected_wall(md, call, put) if o_state == "fresh" else None
        assert wall_text == (want_wall or ""), (case, text)  # 牆距永遠放得下
    assert len(seen) > 40


@pytest.mark.parametrize("value, expected", [
    (12.34, "VIX 12.3 平穩"),
    (19.949, "VIX 19.9 平穩"),
    (19.96, "VIX 20.0 平穩"),  # 一位小數是 20.0，狀態詞看實際值——和期權卡的「19.96，平穩」同一個詞，不讓同一輪播裡兩張卡說法不同
    (20.0, "VIX 20.0 偏緊張"),
    (29.99, "VIX 30.0 偏緊張"),
    (30.0, "VIX 30.0 恐慌"),
    (99.99, "VIX 100.0 恐慌"),
])
def test_digest_vix_has_one_decimal_and_the_cards_status_word(md, value, expected):
    snap = digest_snapshot(vix=value)
    ctx = row_ctx(md, snap)
    assert md._digest_risk_row(ctx)[0] == f"風險：{expected}"
    assert expected.rsplit(" ", 1)[1] in md._vix_line(ctx, ctx.row("macro.VIX"))[0]  # 期權卡同一個狀態詞


@pytest.mark.parametrize("call, put, tail", [
    (3.0, -0.6, "，QQQ 賣權牆距 -0.6%"),
    (3.0, -0.94, "，QQQ 賣權牆距 -0.9%"),
    (3.0, -0.96, ""),  # 顯示值是 -1.0：不算「不到 1%」
    (3.0, -0.95, ""),  # 四捨五入進位：-0.95 顯示 -1.0（以前 round(-0.95, 1) 是 -0.9，判成『不到 1%』卻印出 -1.0%）
    (0.95, -4.0, ""),
    (3.0, 0.05, "，QQQ 賣權牆距 +0.1%"),
    (3.0, -4.0, ""),
    (0.8, -4.0, "，QQQ 買權牆距 +0.8%"),
    (3.0, 0.4, "，QQQ 賣權牆距 +0.4%"),  # 賣權牆在價格上面＝價格已經穿牆：正負號保留
    (-0.3, -4.0, "，QQQ 買權牆距 -0.3%"),  # 買權牆在價格下面
    (3.0, 0.04, "，QQQ 賣權牆距 0.0%"),
    (0.3, -0.8, "，QQQ 買權牆距 +0.3%"),  # 兩邊都不到 1%：寫比較近的
    (0.5, -0.5, "，QQQ 賣權牆距 -0.5%"),  # 一樣近：賣權牆
    (None, None, ""),
])
def test_digest_wall_distance_only_when_under_one_percent_and_keeps_the_sign(md, call, put, tail):
    snap = digest_snapshot()
    set_options(snap, call=call, put=put)
    assert md._digest_risk_row(row_ctx(md, snap))[0] == "風險：VIX 18.0 平穩" + tail


def test_digest_wall_distance_is_the_published_field_not_a_recomputation(md):
    """距離的基準由 publisher 決定（B1 軌可能改成用期權鏈自己的收盤）：這一列讀發布的欄位，不用牆價和現價自己算。"""
    snap = digest_snapshot()
    set_options(snap, call=3.0, put=-0.5)
    values = snap["rows"]["us.options.QQQ"]["values"]
    values.update(spot=704.5, put_wall=700.0)  # 牆價除以現價是 -0.64%
    assert md._digest_risk_row(row_ctx(md, snap))[0].endswith("QQQ 賣權牆距 -0.5%")
    values["put_wall_dist_pct"] = -2.0  # 發布的是 -2.0：自己算出 -0.64 也不能寫
    assert md._digest_risk_row(row_ctx(md, snap))[0] == "風險：VIX 18.0 平穩"
    values.update(put_wall_dist_pct=-0.5, wall_status="insufficient")  # 牆本身不可信：不寫
    assert md._digest_risk_row(row_ctx(md, snap))[0] == "風險：VIX 18.0 平穩"


def test_digest_risk_row_keeps_the_wall_when_vix_is_missing(md):
    snap = digest_snapshot()
    set_options(snap, put=-0.6)
    set_vix(snap, "stale")
    assert md._digest_risk_row(row_ctx(md, snap))[0] == "風險：VIX 過期，QQQ 賣權牆距 -0.6%"  # 長寫法 25.0 放不下
    set_vix(snap, "never")
    assert md._digest_risk_row(row_ctx(md, snap)) == ("風險：VIX 暫缺，QQQ 賣權牆距 -0.6%", md.GRAY)
    set_options(snap, "stale", put=-0.6)
    assert md._digest_risk_row(row_ctx(md, snap))[0] == "風險：VIX 暫缺"


# ── 第 4 列：利率 ────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value, change, expected", [
    (4.96, 0.03, "利率：10 年債 4.96%（+3bp）"),
    (5.09, 0.12, "利率：10 年債 5.09%（+12bp）"),
    (4.2, -0.05, "利率：10 年債 4.20%（-5bp）"),
    (4.2, 0.0, "利率：10 年債 4.20%（持平）"),
    (4.2, 0.004, "利率：10 年債 4.20%（持平）"),
    (4.2, 0.005, "利率：10 年債 4.20%（+1bp）"),  # 四捨五入進位，不是銀行家捨入（0.5 → 0）
    (4.2, -0.005, "利率：10 年債 4.20%（-1bp）"),
    (4.2, 0.025, "利率：10 年債 4.20%（+3bp）"),
    (4.2, None, "利率：10 年債 4.20%"),
    (4.2, 1e9, "利率：10 年債 4.20%"),  # 離譜的變化量當沒有
    (99.99, 49.99, "利率：10 年債 99.99%（+4999bp）"),
    (1e9, 0.03, "利率：10 年債暫缺"),
    (None, 0.03, "利率：10 年債暫缺"),
])
def test_digest_rate_row(md, value, change, expected):
    snap = digest_snapshot()
    set_ust(snap, "fresh", value, change)
    text, color = md._digest_rate_row(row_ctx(md, snap))
    assert (text, color) == (expected, md.GRAY) and md.row_width(text) <= 24


def test_digest_rate_row_when_the_ten_year_is_stale_or_missing(md):
    snap = digest_snapshot()
    set_ust(snap, "stale")
    assert md._digest_rate_row(row_ctx(md, snap)) == ("利率：10 年債過期（資料日 10/16）", md.GRAY)
    set_ust(snap, "never")
    assert md._digest_rate_row(row_ctx(md, snap)) == ("利率：10 年債暫缺", md.GRAY)
    del snap["rows"]["macro.UST10Y"]  # 整列不見（舊快照）也一樣
    assert md._digest_rate_row(row_ctx(md, snap)) == ("利率：10 年債暫缺", md.GRAY)


def test_the_rate_and_trump_rows_are_separate_replaceable_functions(md, monkeypatch):
    """批 2 會把第 4 列換成通膨狀態句、TACO 軌會在第 6 列加優先的變體：兩列各自是 f(ctx) → (文字, 顏色)，card 只靠名字呼叫。"""
    snap = digest_snapshot()
    monkeypatch.setattr(md, "_digest_rate_row", lambda ctx: ("利率：換掉了", md.ORANGE))
    monkeypatch.setattr(md, "_digest_trump_row", lambda ctx: ("川普：換掉了", md.BLUE))
    rows, _bubble = digest_rows(md, snap)
    assert rows[3] == ("利率：換掉了", md.ORANGE) and rows[5] == ("川普：換掉了", md.BLUE)
    assert [text[:2] for text, _color in rows[:3]] == ["美股", "台股", "風險"] and rows[4][0].startswith("事件：")


# ── 第 5 列：事件 ────────────────────────────────────────────────────────────────────────
def test_digest_event_row_in_every_state(md):
    seen = set()
    for snap, item, offset in iter_event_cases():
        text, color = md._digest_event_row(row_ctx(md, snap))
        seen.add(text)
        assert text.startswith("事件：") and md.row_width(text) <= 24 and "…" not in text, (item, offset, text)
        assert color == md.GRAY
        if "state" in item:
            continue
        kind = item["kind"]
        if kind == "earnings":
            wanted = f"財報（{item['name']}"
        elif kind == "econ" and item["name"] in market_calc.US_EVENT_LABELS:
            wanted = market_calc.US_EVENT_LABELS[item["name"]].split("（")[0]  # 拿掉括號裡的英文
        elif kind in md._EVENT_KINDS:
            wanted = md._EVENT_KINDS[kind].split("（")[0]
        else:  # 外部來源的 24 字名稱放不下：固定寫法，不跳到下一個事件
            wanted = md.TEXT_DIGEST_NEXT_EVENT
            assert item["name"] not in text
        assert wanted in text, (item, offset, text)
    assert len(seen) > 60


def test_digest_event_row_examples(md):
    snap = digest_snapshot()

    def row(items, state="fresh"):
        set_events(snap, items, state)
        return md._digest_event_row(row_ctx(md, snap))[0]

    tonight = datetime(2026, 10, 17, 22, 0, tzinfo=TPE).timestamp()
    assert row([{"name": "Michigan Consumer Sentiment", "kind": "econ", "at": tonight}]) == "事件：密大消費者信心，今天 22:00"
    assert row([{"name": "FOMC", "kind": "fomc", "at": NOW + 11 * 86400}]) == "事件：聯準會利率決議，還有 11 天"
    assert row([{"name": "CPI", "kind": "cpi", "at": tonight + 86400}]) == "事件：消費者物價，明天 22:00"
    earnings = datetime(2026, 10, 20, 16, 0, tzinfo=ET).timestamp()
    assert row([{"name": "NVDA", "kind": "earnings", "at": earnings, "time_known": False, "estimated": True}]) == (
        "事件：財報（NVDA，預估），還有 3 天（美東日期）")
    assert row([{"name": "GOOGL", "kind": "earnings", "at": earnings + 9 * 86400, "time_known": False,
                 "estimated": True}]) == "事件：財報（GOOGL，預估），還有 12 天"  # 24.5 放不下「（美東日期）」：拿掉，名稱和倒數留著
    # 兩個事件：寫最近的那一個；同一時刻 FOMC、CPI、非農排前面
    assert row([{"name": "Unemployment Rate", "kind": "econ", "at": tonight},
                {"name": "Nonfarm Payrolls", "kind": "nfp", "at": tonight}]) == "事件：非農就業，今天 22:00"
    assert row([{"name": "x", "kind": "econ", "at": NOW - 3600}]) == "事件：近期沒有列入的事件"  # 過了的不列
    assert row([], "stale") == "事件：過期（資料日 10/17）"
    assert row([], "never") == "事件：暫缺"
    del snap["rows"]["events.next"]
    assert md._digest_event_row(row_ctx(md, snap))[0] == "事件：暫缺"
    # 外部來源的長名稱：不用「…」切掉，改寫固定的「下一個事件」
    assert row([{"name": "名" * 24, "kind": "econ", "at": tonight}]) == "事件：下一個事件，今天 22:00"


# ── 第 6 列：川普 ────────────────────────────────────────────────────────────────────────
def test_digest_trump_row_is_the_trump_line_of_the_macro_card(md):
    snap = digest_snapshot()
    snap["rows"]["macro.TACO"] = _never()  # 有川普壓力指數時第 6 列放它（另有測試）；這裡測退回的那一句
    pins = [
        (({"關稅": 2, "伊朗": 1}, False), "川普近 48 小時：關稅 2 則、伊朗 1 則"),
        (({"關稅": 2, "伊朗": 1}, True), "川普近 48 小時（至少）：關稅 2 則、伊朗 1 則"),
        (({}, False), "川普近 48 小時：沒有符合主題的貼文"),
        (({"關稅": 3, "伊朗": 2, "油價": 2, "中國": 1}, False), "川普近 48 小時：關稅 3 則、伊朗 2 則、油價 2 則"),
    ]
    for (topics, at_least), expected in pins:
        snap["rows"]["trump.summary"]["values"].update(topics=topics, at_least=at_least)
        ctx = row_ctx(md, snap)
        assert md._digest_trump_row(ctx) == md._trump_line(ctx)
        assert md._digest_trump_row(ctx)[0] == expected and md.row_width(expected) <= 24
    snap["rows"]["trump.summary"]["stale_after"] = NOW - 1
    assert md._digest_trump_row(row_ctx(md, snap))[0] == "川普近 48 小時：過期（資料日 10/17）"
    snap["rows"]["trump.summary"] = _never()
    assert md._digest_trump_row(row_ctx(md, snap))[0] == "川普近 48 小時：暫缺"


def test_digest_never_shows_a_contradiction_sentence(md):
    """矛盾句是美股、台股的句型（它們在各自的卡上）：摘要的 6 列不放，也不因為有矛盾而換掉第 6 列。"""
    plain = digest_snapshot()
    snap = digest_snapshot()
    snap["rows"]["contra.us"]["values"]["codes"] = [f"{code}:SOXX" if "{subject}" in text else code
                                                    for code, text in market_calc.CONTRA_TEXT.items()]
    snap["rows"]["contra.tw"]["values"]["codes"] = ["foreign_sell_trust_buy", "index_down_margin_up"]
    assert digest_texts(md, snap) == digest_texts(md, plain)
    sentences = [text.format(subject="半導體") for text in market_calc.CONTRA_TEXT.values()]
    assert not any(sentence in "\n".join(digest_texts(md, snap)) for sentence in sentences)


# ── footer：資料日、暫定收盤 ─────────────────────────────────────────────────────────────
def test_digest_footer_lists_only_the_dates_that_exist(md):
    snap = digest_snapshot()

    def notes():
        return [note for note in md._digest_notes(row_ctx(md, snap)) if note.startswith(f"{md.TEXT_DIGEST_DATES}：")]

    # 順序照第 1–6 列：美股、台股、VIX、期權（牆距寫進第 3 列才列）、10 年債、川普壓力指數（F2-1）
    assert notes() == ["資料日：美股 10/16、台股 10/16、VIX 10/16、10 年債 10/16、川普壓力指數 10/16"]
    set_taiex(snap, "stale")
    set_etf(snap, "VOO", "stale")
    assert notes() == ["資料日：美股 10/16、VIX 10/16、10 年債 10/16、川普壓力指數 10/16"]  # 過期的列不列（列上寫的是「過期」）
    set_etf(snap, "QQQ", "missing")
    set_etf(snap, "SOXX", "flagless")  # 有資料但月線狀態缺：列上寫「暫缺」，日期也不列
    assert notes() == ["資料日：VIX 10/16、10 年債 10/16、川普壓力指數 10/16"]
    set_vix(snap, "fresh", None)  # VIX 列在但沒有數字：列上寫「VIX 暫缺」
    assert notes() == ["資料日：10 年債 10/16、川普壓力指數 10/16"]
    set_vix(snap, "never")
    set_ust(snap, "stale")  # 10 年債過期：列上寫「過期（資料日 …）」，不重複列
    assert notes() == ["資料日：川普壓力指數 10/16"]
    snap["rows"]["macro.TACO"]["stale_after"] = NOW - 1  # 川普壓力指數過期：第 6 列退回，不列
    assert notes() == []
    set_ust(snap, "fresh", None)  # 10 年債列在但沒有數字
    assert notes() == []
    set_ust(snap, "never")
    snap["rows"]["macro.TACO"] = _never()
    assert notes() == []
    snap = digest_snapshot()
    snap["rows"]["macro.TACO"]["values"]["percentile"] = 101  # 壞掉的川普壓力指數：第 6 列退回，不列
    assert notes() == ["資料日：美股 10/16、台股 10/16、VIX 10/16、10 年債 10/16"]
    snap = digest_snapshot()
    set_options(snap, put=-0.6, day=date(2026, 10, 15))  # 牆距寫進第 3 列才列期權的日期
    assert notes() == ["資料日：美股 10/16、台股 10/16、VIX 10/16、期權 10/15、10 年債 10/16、川普壓力指數 10/16"]
    set_options(snap, put=-4.0, day=date(2026, 10, 15))
    assert notes() == ["資料日：美股 10/16、台股 10/16、VIX 10/16、10 年債 10/16、川普壓力指數 10/16"]


def test_digest_footer_adds_the_provisional_sentence_by_equality_only(md):
    snap = digest_snapshot()
    base = "資料日：美股 10/16、台股 10/16、VIX 10/16、10 年債 10/16、川普壓力指數 10/16"

    def notes():
        return [note for note in md._digest_notes(row_ctx(md, snap)) if note not in TACO_MEANING_SOURCE]

    assert notes() == [base]
    snap["rows"]["us.etf.QQQ"]["source"] = market_calc.SOURCE_CBOE_FALLBACK  # 任一檔暫定就加
    assert notes() == [base, market_calc.TEXT_PROVISIONAL_CLOSE]
    for not_it in (market_calc.SOURCE_CBOE_FALLBACK + "（舊）", "舊" + market_calc.SOURCE_CBOE_FALLBACK, "CBOE", "備援", ""):
        snap["rows"]["us.etf.QQQ"]["source"] = not_it  # 子字串、相近的字都不算
        assert notes() == [base], not_it
    snap["rows"]["us.etf.QQQ"]["source"] = market_calc.SOURCE_CBOE_FALLBACK
    set_etf(snap, "QQQ", "stale")  # 過期的列沒有數字，不算
    snap["rows"]["us.etf.QQQ"]["source"] = market_calc.SOURCE_CBOE_FALLBACK
    assert notes() == [base]
    snap["rows"]["macro.VIX"]["source"] = market_calc.SOURCE_CBOE_FALLBACK  # 只有 ETF 的收盤會是暫定值
    assert notes() == [base]
    # 整張卡：暫定說明、川普壓力指數的兩句 + 免責句 + 3 顆按鈕，仍在大小上限內
    snap["rows"]["us.etf.VOO"]["source"] = market_calc.SOURCE_CBOE_FALLBACK
    bubble = digest_bubble(md, snap)
    assert texts(bubble["footer"])[:5] == [base, market_calc.TEXT_PROVISIONAL_CLOSE, *TACO_MEANING_SOURCE, DISCLAIMER]
    assert md.card_bytes(bubble) <= md.SUMMARY_BUBBLE_MAX_BYTES


# ── 舊快照、缺的列、壞的值 ───────────────────────────────────────────────────────────────
# us.options.SOXX：第 3 列的牆 QQQ、SOXX 取較近的一道（2026-10-28 批 1 審查 F2-7），所以 SOXX 的期權列也在摘要讀的列裡
DIGEST_SOURCE_ROWS = ("us.etf.VOO", "us.etf.QQQ", "us.etf.SOXX", "macro.VIX", "macro.UST10Y", "us.options.QQQ",
                      "us.options.SOXX", "tw.taiex", "tw.inst3", "events.next", "trump.summary", "macro.TACO")


def test_the_digest_degrades_row_by_row_on_old_and_partial_snapshots(md, paths):
    no_progress = make_snapshot(NOW)
    del no_progress["progress"]  # 還沒有 us_day／tw_day 欄位的舊快照
    junk_progress = make_snapshot(NOW)
    junk_progress["progress"] = {"us_day": 5, "tw_day": [], "us_expected_day": 5, "tw_expected_day": [], "us_members": "x",
                                 "tw_closed_today": "yes"}
    no_rows = make_snapshot(NOW)
    for row_id in DIGEST_SOURCE_ROWS:
        del no_rows["rows"][row_id]  # 這張卡要讀的列整個不見（其他列還在，所以不是「資料準備中」）
    never_rows = make_snapshot(NOW)
    for row_id in DIGEST_SOURCE_ROWS:
        never_rows["rows"][row_id] = _never()
    one_row = {"schema": 1, "generated_at": NOW - 600, "rows": {"tw.taiex": make_snapshot(NOW)["rows"]["tw.taiex"]}}
    for name, snap in (("no_progress", no_progress), ("junk_progress", junk_progress), ("no_rows", no_rows),
                       ("never_rows", never_rows), ("one_row", one_row)):
        write_snapshot(paths, snap)
        reply = md.handle("", now=NOW, paths=paths)
        assert reply.flex is not None and reply.render_errors == (), (name, reply.text)
        body = summary_bubble(md, reply.flex, DIGEST)["body"]["contents"]
        assert [node["text"][:2] for node in body] == SIX_PREFIXES, name
        assert all(md.row_width(node["text"]) <= 24 and "…" not in node["text"] for node in body), name
        if name in ("no_rows", "never_rows"):
            assert [node["text"] for node in body] == [
                "美股：三檔都暫缺", "台股：加權暫缺，外資暫缺", "風險：VIX 暫缺", "利率：10 年債暫缺", "事件：暫缺",
                "川普近 48 小時：暫缺"], name
            footer = texts(summary_bubble(md, reply.flex, DIGEST)["footer"])
            # 沒有任何日期：不寫「資料日」那行；第 6 列退回「川普近 48 小時」，footer 補川普壓力指數的狀態句（F2-14）
            assert footer[:2] == ["川普壓力指數：暫缺", DISCLAIMER], name
    # 一般快照拿掉 progress 之後，除了 progress 不同，整張卡一個字都不變
    write_snapshot(paths, make_snapshot(NOW))
    with_progress = summary_bubble(md, md.handle("", now=NOW, paths=paths).flex, DIGEST)
    write_snapshot(paths, no_progress)
    assert summary_bubble(md, md.handle("", now=NOW, paths=paths).flex, DIGEST) == with_progress


def test_the_digest_survives_every_kind_of_junk_in_the_rows_it_reads(md, paths):
    for junk in JUNK:
        for mode in ("leaves", "values"):
            snap = make_snapshot(NOW)
            for row_id in DIGEST_SOURCE_ROWS:
                row = snap["rows"][row_id]
                row["values"] = _corrupt_leaves(row["values"], junk) if mode == "leaves" else {k: junk for k in row["values"]}
            snap["progress"] = _corrupt_leaves(snap["progress"], junk)
            texts_ = digest_texts(md, snap)  # 直接呼叫 builder：丟例外就是失敗
            assert [text[:2] for text in texts_] == SIX_PREFIXES, (junk, mode)
            assert all(md.row_width(text) <= 24 for text in texts_), (junk, mode, texts_)


# ── 字：固定句、驗證器、禁用字、承諾 ─────────────────────────────────────────────────────
def test_every_digest_sentence_passes_the_text_gates(md, monkeypatch):
    spec = importlib.util.spec_from_file_location("bot_promise_scan_for_digest", HERE / "jobs" / "bot_promise_scan.py")
    scan = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, scan)
    spec.loader.exec_module(scan)
    strings: set[str] = set()
    for snap, *_ in iter_us_cases():
        strings.add(md._digest_us_row(row_ctx(md, snap))[0])
    for snap, _case in iter_tw_cases():
        strings.add(md._digest_tw_row(row_ctx(md, snap))[0])
    for snap, _case in iter_risk_cases():
        ctx = row_ctx(md, snap)
        strings.update((md._digest_risk_row(ctx)[0], *md._digest_notes(ctx)))
    for snap, _item, _offset in iter_event_cases():
        strings.add(md._digest_event_row(row_ctx(md, snap))[0])
    for value, change in itertools.product((None, 4.96, 99.99), (None, 0.03, -0.12, 0.0)):
        snap = digest_snapshot()
        set_ust(snap, "fresh", value, change)
        strings.add(md._digest_rate_row(row_ctx(md, snap))[0])
    fragments = {getattr(md, name) for name in dir(md) if name.startswith("TEXT_DIGEST_")}
    assert fragments >= {"三檔都", "過期", "10 年債", "下一個事件", "資料日"}
    strings |= fragments | {market_calc.TEXT_PROVISIONAL_CLOSE}
    assert len(strings) > 600
    for now in ALL_NOWS:
        at = datetime.fromtimestamp(now, TPE)
        for value in strings:
            result = output_validator.validate_outbound_text(value, now=at)
            assert result.ok and result.text == value, (value, result.reason)
    for value in strings:
        assert not md.has_forbidden(value) and not any(word in value for word in PLAN_FORBIDDEN), value
        assert scan.promise_sentences(value) == [], value
        assert not re.search(r"下次會|之後會|我會", value), value
    assert not any(str(year) in fragment for fragment in fragments for year in range(2020, 2040))  # 固定字不寫年份


# ── SHIPPED 三份一致 ─────────────────────────────────────────────────────────────────────
def assigned_tuple(path, name):
    """用 ast 讀一個檔案裡頂層的 `NAME = (…)`（不 import）。"""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError(f"{name} not found in {path}")


def test_the_three_shipped_lists_are_identical_and_the_digest_buttons_are_in_them(md):
    """market_dashboard.SHIPPED_MARKET_TEXTS、test_market_routing 的同名表、deploy/smoke_after_deploy.py 的 SHIPPED 必須是同一份
    （舊卡片一直留在聊天紀錄裡可以點，只能加不能刪）。smoke 在 repo 外（部署資料夾），找不到就只比前兩份。"""
    shipped = md.SHIPPED_MARKET_TEXTS
    assert assigned_tuple(HERE / "test_market_routing.py", "SHIPPED_MARKET_TEXTS") == shipped
    smoke = HERE.parent / "deploy" / "smoke_after_deploy.py"
    if smoke.is_file():
        assert assigned_tuple(smoke, "SHIPPED") == shipped
    sent = [action["text"] for action in actions(digest_bubble(md, make_snapshot(NOW)))]  # 摘要第一張的三顆按鈕
    assert sent == ["/盤勢 期權", "/盤勢 新聞", "/盤勢 0050"]
    for command in sent:
        assert command in shipped and md.parse_command(command[len("/盤勢"):]) is not None


def test_digest_compares_with_the_expected_day_not_the_data_day(md):
    """整合 A1×A2（2026-10-28）：A2 的 progress.us_day／tw_day 是「目前資料到哪天」，預期的最新場次另放
    us_expected_day／tw_expected_day。重點摘要要拿後者比，不然資料停在前一天時永遠看不出落後。"""
    later = (DAY + timedelta(days=3)).isoformat()
    snap = digest_snapshot()  # 三檔都是 DAY、其他美股列也是 DAY：預設沒有落後
    snap["progress"].update(us_day=DAY.isoformat(), us_expected_day=later)
    assert digest_texts(md, snap)[0].startswith(f"美股 {DAY.month}/{DAY.day}："), digest_texts(md, snap)[0]
    snap = digest_snapshot()
    snap["progress"].update(us_day=later)  # 只有「資料日」：不能拿來當預期的場次
    assert digest_texts(md, snap)[0].startswith("美股："), digest_texts(md, snap)[0]
    snap = digest_snapshot()
    snap["progress"].update(tw_day=DAY.isoformat(), tw_expected_day=later)
    assert digest_texts(md, snap)[1].startswith(f"台股 {DAY.month}/{DAY.day}"), digest_texts(md, snap)[1]


def test_digest_trump_row_prefers_the_trump_pressure_index(md):
    """整合 A1×TACO（2026-10-28）：重點摘要第 6 列有川普壓力指數就放它（Andrew 要看數字和位階），
    沒有（暫缺、過期、壞掉）才退回「川普近 48 小時：…」。"""
    snap = digest_snapshot()
    row = digest_texts(md, snap)[5]
    assert row == "川普壓力指數 +9.0（高壓，任期內第 88 百分位）" and md.row_width(row) <= 24, row
    for broken in ("never", "stale", "junk"):
        snap = digest_snapshot()
        if broken == "never":
            snap["rows"]["macro.TACO"] = _never()
        elif broken == "stale":
            snap["rows"]["macro.TACO"]["stale_after"] = NOW - 1
        else:
            snap["rows"]["macro.TACO"]["values"]["percentile"] = 101
        row = digest_texts(md, snap)[5]
        assert row.startswith("川普近 48 小時"), (broken, row)
