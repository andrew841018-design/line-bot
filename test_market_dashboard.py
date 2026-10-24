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
import json
import os
import re
import sys
import unicodedata
import uuid
from datetime import date, datetime, timedelta
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
TRUMP_TEXT = "We will put new tariffs on imported chips, and Iran must stop now."
HOLDING_NAMES = ("輝達", "蘋果", "微軟", "亞馬遜", "Alphabet", "博通", "Meta", "特斯拉", "好市多", "Netflix")
HOLDING_SYMBOLS = (["NVDA"], ["AAPL"], ["MSFT"], ["AMZN"], ["GOOGL", "GOOG"], ["AVGO"], ["META"], ["TSLA"],
                   ["COST"], ["NFLX"])
UNIVERSE = "依 S&P 500 權重（SPY 每日持股 10/15）"
COVERAGE = "有效 500/503 檔"
PERIOD_NOTE = "彙總期間來源未標；前十大持有人申報期間以 6/30 為主（混合）"
ONELINER_US = "美股收在月線上、成分股站上月線 62%、量偏少"
ONELINER_TW = "台股收在月線上、外資賣超 123 億、量偏少"
TOPICS = ("關稅", "伊朗")
EXTERNAL = (*GOOD_TITLES, *NEWS_SOURCES, TRUMP_TEXT, *HOLDING_NAMES, UNIVERSE, COVERAGE, PERIOD_NOTE,
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


def _options(spot):
    return {"spot": spot, "call_wall": float(round(spot * 1.03)), "put_wall": float(round(spot * 0.96)),
            "call_wall_dist_pct": 3.0, "put_wall_dist_pct": -4.0, "wall_status": "ok",
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
        **{f"us.13f.{s}": _row(_holdings(), now, None, state="unknown_period") for s in ("VOO", "QQQ", "SOXX")},
        **{f"us.fund.{s}": _row(_fund(), now, day) for s in ("VOO", "QQQ", "SOXX")},
        "tw.taiex": _row({"close": 47111.26, "chg": 243.1, "chg_pct": 0.52,
                          "ma_dist": {"20": 1.1, "60": 2.2, "120": 3.3, "240": 9.9}, "above_ma20": True,
                          "dd240_pct": -1.5}, now, day),
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
                            "taiex_pending": False}, now, today),
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
           for k, t, s in zip(("us", "tw", "iran"), GOOD_TITLES, NEWS_SOURCES)},
        "oneliner.us": _row({"text": ONELINER_US}, now, day),
        "oneliner.tw": _row({"text": ONELINER_TW}, now, day),
        "contra.us": _row({"codes": [], "text": None}, now, day),
        "contra.tw": _row({"codes": ["foreign_sell_trust_buy"], "text": "外資 5 日賣超、投信 5 日買超"}, now, day),
    }
    macro = {"VIX": 19.99, "VIX3M": 21.5, "UST3M": 4.26, "UST10Y": 5.09, "DXY": 28.92, "WTI": 72.3, "BRENT": 76.1,
             "GOLD": 2650.0, "SILVER": 31.2, "BTC": 81471.0, "USDTWD": 32.338, "RUT": 2250.0, "DJI": 43000.0,
             "IXIC": 19000.0, "MOVE": 98.5, "SPY_DD": -3.2}
    for key, value in macro.items():
        source = "ETF 代理：UUP" if key == "DXY" else "Yahoo"
        # DXY 這格是 UUP 代理：value 是 ETF 的股價 28.92（不是美元指數），chg 是股價的變動，只有 chg_pct 能用
        rows[f"macro.{key}"] = _row({"value": value, "chg": 0.12, "chg_pct": 0.3 if key == "DXY" else None,
                                     "source": source}, now, day)
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
    for kind in ("us", "tw", "iran"):
        rows[f"news.{kind}"]["values"]["items"] = [
            {"title": long_title, "source": long_name, "published_utc": recent} for _ in range(12)]
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
def test_summary_is_four_same_size_bubbles_with_titles(md, paths, variant):
    write_snapshot(paths, _variants()[variant])
    reply = md.handle("", now=NOW, paths=paths)
    card = reply.flex
    assert card["type"] == "carousel" and len(card["contents"]) == 4
    assert {b["size"] for b in card["contents"]} == {"giga"}
    for index, bubble in enumerate(card["contents"], 1):
        title = bubble["header"]["contents"][0]["text"]
        assert f"（{index}/4）" in title and re.search(r"｜\d{1,2}/\d{1,2}", title), title
        assert DISCLAIMER in texts(bubble["footer"])
    assert "往左滑還有 3 張" in texts(card["contents"][0]["header"])
    assert all("往左滑" not in t for b in card["contents"][1:] for t in texts(b["header"]))
    assert reply.alt_text.startswith("盤勢：") and len(reply.alt_text) <= 400
    assert reply.fallback_text.startswith("盤勢：")


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


def test_breadth_line_when_all_three_are_insufficient(md, paths):
    """2026-10-23 審查 r7a N3：三檔都寫「資料不足」會超寬被切成「SOXX 資…」：全部不足就合寫一次。"""
    snap = make_snapshot(NOW)
    for sym, total in (("VOO", 503), ("QQQ", 101), ("SOXX", 30)):
        snap["rows"][f"us.breadth.{sym}"]["values"] = _breadth(total, total // 2, total // 4, UNIVERSE)
    write_snapshot(paths, snap)
    us = texts(md.handle("", now=NOW, paths=paths).flex["contents"][0]["body"])
    assert "成分股站上月線：資料不足" in us
    snap["rows"]["us.breadth.VOO"]["values"] = _breadth(503, 500, 310, UNIVERSE)
    write_snapshot(paths, snap)
    us = texts(md.handle("", now=NOW, paths=paths).flex["contents"][0]["body"])
    line = next(text for text in us if "站上月線" in text)
    assert "VOO 62%" in line and "…" not in line and md.row_width(line) <= 24


def test_summary_rows_show_numbers_and_states(md, paths):
    write_snapshot(paths, make_snapshot(NOW))
    card = md.handle("", now=NOW, paths=paths).flex
    us, tw, flow, macro = (texts(b["body"]) for b in card["contents"])
    assert us == [
        "標普500（VOO）612.34 +0.52% 在月線上",
        "那斯達克100（QQQ）540.12 -0.31% 在月線下",
        "半導體（SOXX）245.6 +1.20% 在月線上（10/15）",  # 和標題不同日要寫日期
        "成分股站上月線：VOO 62%、QQQ 55%、SOXX 資料不足",  # SOXX 有效 22/30 <80%
        "量能：VOO 量偏少、QQQ 量偏少、SOXX 量偏少",  # 和一句話同一套字（資料值是「量縮」）
        ONELINER_US,  # 沒有矛盾時放一句話
    ]
    assert tw == [
        "加權指數 47,111（+0.52%） 在月線上",
        "台灣50（0050）185.3 +0.40% 在月線上",
        "0050 成分站上月線：62%（31/50 檔）",
        "成交金額 5,234 億，量偏少（0.78 倍）",
        "外資賣超 123.4 億、投信買超 45.6 億",
        "外資 5 日賣超、投信 5 日買超",  # 矛盾優先於一句話，用 market_calc 的固定句
    ]
    assert flow == [
        "恐慌指數（VIX）19.99（+0.12），平穩",
        "那斯達克（QQQ）買權牆 556（距現價 +3.0%）",
        "那斯達克（QQQ）賣權牆 518（距現價 -4.0%）",
        "台指選擇權：買權牆 54,000、賣權牆 46,000",
        "外資台指期（大台）淨空 97,582 口",
        "融資：台股 +12.3 億、美股年增 +25%",
    ]
    assert macro == [
        "美債 10 年（UST10Y）5.09%（+0.12）",
        "美元（UUP 代理）單日 +0.30%、油價（WTI）72.3",  # ETF 代理只寫日漲跌幅，不印 ETF 的股價 28.92
        "黃金（Gold）2,650、比特幣（BTC）81,471",
        "下一個事件：Consumer Price Index，還有 3 天",
        "距期中選舉 17 天，Polymarket 民主黨拿下眾院 90%",  # 摘要點名平台（細節卡兩家都列）
        "川普近 48 小時：關稅 2 則、伊朗 1 則",
    ]
    assert card["contents"][3]["header"]["contents"][0]["text"] == "宏觀與事件（4/4）｜10/17 16:20 更新"
    assert "MM 川普壓力指數：抓不到（要登入）" in texts(card["contents"][3]["footer"])


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
    snap["rows"]["oneliner.us"]["values"]["text"] = "美股收在月線上、某某標題"
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
            action = node.get("action")
            if action is not None:
                assert node["type"] == "box" and set(action) == {"type", "label", "text"}
                assert action["type"] == "message" and len(action["label"]) <= 20
                assert action["text"] in md.SHIPPED_MARKET_TEXTS, action["text"]
                (inner,) = node["contents"]
                assert inner["type"] == "text" and inner["size"] == "xxl" and inner["text"] == action["label"]
        assert all(sum(1 for n in b["footer"]["contents"] if "action" in n) <= 3 for b in bubbles(reply.flex))
        assert "🔍" not in json.dumps(reply.flex, ensure_ascii=False)  # main 經 card_pages 加


def test_shipped_texts_route_and_every_one_has_a_button(md, paths):
    assert len(set(md.SHIPPED_MARKET_TEXTS)) == len(md.SHIPPED_MARKET_TEXTS)
    for text in md.SHIPPED_MARKET_TEXTS:
        match = re.match(r"/\s{0,3}盤勢", text)
        assert match is not None and md.parse_command(text[match.end():]) is not None, text
    write_snapshot(paths, make_snapshot(NOW))
    sent = {a["text"] for r in render_all(md, paths, NOW).values() for a in actions(r.flex)}
    assert sent == set(md.SHIPPED_MARKET_TEXTS)


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
    assert len(card["contents"][0]["body"]["contents"]) == 5
    assert md.status_summary(now=NOW, paths=paths)["external_dropped"] == 0


def test_job_sentences_with_forbidden_words_are_dropped(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["oneliner.us"]["values"]["text"] = "美股在月線上，建議進場"
    write_snapshot(paths, snap)
    card = md.handle("", now=NOW, paths=paths).flex
    assert not any("進場" in t for t in texts(card))
    assert len(card["contents"][0]["body"]["contents"]) == 5


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
    assert texts(card["contents"][0]["body"])[0] == "標普500（VOO）過期（資料日 10/16）"
    titles = [b["header"]["contents"][0]["text"] for b in card["contents"]]
    assert titles[:3] == ["美股（1/4）｜資料過期", "台股（2/4）｜資料過期", "期權與資金（3/4）｜資料過期"]  # 列上寫過期，標題同字
    assert texts(md.handle(" VOO", now=NOW, paths=paths).flex)[0] == "標普500（VOO）｜資料過期"
    assert texts(md.handle(" 0050", now=NOW, paths=paths).flex)[0] == "台灣50（0050）｜資料過期"


def test_never_rows_show_missing(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["us.etf.QQQ"] = _never()
    del snap["rows"]["tw.inst3"]  # 整列不見也一樣
    write_snapshot(paths, snap)
    card = md.handle("", now=NOW, paths=paths).flex
    assert texts(card["contents"][0]["body"])[1] == "那斯達克100（QQQ）暫缺"
    assert texts(card["contents"][1]["body"])[4] == "外資與投信 暫缺"


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
    us = texts(card["contents"][0]["body"])
    assert all(row.endswith("暫缺") for row in us[:3]), us
    assert texts(card["contents"][1]["body"])[0] == "加權指數 暫缺"


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
    assert "美股歷史資料補到 480/520 檔" in texts(card["contents"][0]["footer"])


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
    assert len(calls) == 2 and texts(card["contents"][0]["body"])[0].startswith("標普500（VOO）700")


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
                   "融資餘額 3,456 億（+12.3 億），今日（參考）", "9 月營收年增 >0：31/50 檔（62%）",
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


def test_election_phases(md, paths):
    def at(moment_et, state="upcoming"):
        now = moment_et.timestamp()
        snap = make_snapshot(now)
        snap["rows"]["election"]["values"]["state"] = state
        write_snapshot(paths, snap)
        return md.handle(" 選舉", now=now, paths=paths), md.handle("", now=now, paths=paths)

    def macro_rows(reply):
        return texts(reply.flex["contents"][3]["body"])

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
    assert len(summary.flex["contents"][3]["body"]["contents"]) == 5
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
def test_module_imports_stay_light():
    tree = ast.parse((HERE / "market_dashboard.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    banned = {"numpy", "pandas", "matplotlib", "main", "card_pages", "stock_picks", "fcn_eval", "requests"}
    assert not imported & banned
    assert not any(name.startswith("market_sources") for name in imported)
    assert {"market_calc", "market_paths", "output_validator"} <= imported


def test_handle_never_raises(md, paths, monkeypatch):
    write_snapshot(paths, make_snapshot(NOW))
    monkeypatch.setattr(md, "_summary_reply", lambda ctx: 1 / 0)
    assert md.handle("", now=NOW, paths=paths).text == "盤勢：資料準備中，請過幾分鐘再按一次"
    monkeypatch.setattr(md.market_calc, "clean_external", lambda text, max_len: 1 / 0)
    reply = md.handle(" 新聞", now=NOW, paths=paths)
    assert reply.flex is not None and GOOD_TITLES[0] not in texts(reply.flex)  # 清洗壞掉：丟字、卡照送


# ── 2026-10-18 r6 審查：日期、代理、用字（r6b／r6c／r6d） ─────────────────────────────
def _today(now=NOW):
    return datetime.fromtimestamp(now, TPE).date()


def _summary_bodies(md, paths, snap, now=NOW):
    write_snapshot(paths, snap)
    return [texts(b["body"]) for b in md.handle("", now=now, paths=paths).flex["contents"]]


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
    card = md.handle("", now=NOW, paths=paths).flex["contents"][2]
    rows = texts(card["body"])
    assert card["header"]["contents"][0]["text"] == "期權與資金（3/4）｜10/17 收盤"
    assert rows[0] == "恐慌指數（VIX）過期（資料日 10/16）"
    assert rows[3] == "台指選擇權：買權牆 54,000、賣權牆 46,000" and rows[4] == "外資台指期（大台）淨空 97,582 口"
    assert rows[5] == "融資：台股 +12.3 億（10/16）、美股年增 +25%"  # 台股融資是 10/16，和標題（10/17）不同天
    # 美股兩列沒過期時照舊：標題是美股的日期
    snap["rows"]["macro.VIX"]["stale_after"] = NOW + 3600
    assert _summary_bodies(md, paths, snap)[2][3] == "台指選擇權：10/17 買權牆 54,000、賣權牆 46,000"


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
    assert texts(replies[""].flex["contents"][3]["body"])[1] == "美元（UUP 代理）單日 +0.30%、油價（WTI）72.3"
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
    return texts(card["contents"][3]["body"]), texts(news["contents"][2]["body"])


def test_provisional_us_close_is_marked_on_the_cards(md, paths):
    """2026-10-23 審查 r9 Codex 席 I2：ETF 列的收盤是 CBOE 的暫定值時，卡上要看得出來（摘要列「（暫定）」、細節卡一句
    固定說明）；量能寫「暫不判斷」，前面不能多一個「，」。"""
    snap = make_snapshot(NOW)
    snap["rows"]["us.etf.VOO"]["source"] = md.market_calc.SOURCE_CBOE_FALLBACK
    snap["rows"]["us.etf.VOO"]["values"].update(vol_ratio=None, vol_pct250=None, vol_label=None,
                                                 vol_note=md.market_calc.TEXT_VOL_SOURCE_DIFF)
    write_snapshot(paths, snap)
    us = texts(md.handle("", now=NOW, paths=paths).flex["contents"][0]["body"])
    line = next(text for text in us if text.startswith("標普500（VOO）"))
    assert "（暫定）" in line and md.row_width(line) <= 24
    detail = reply_strings(md.handle(" VOO", now=NOW, paths=paths))
    assert md.market_calc.TEXT_PROVISIONAL_CLOSE in detail
    assert not any("量能：，" in text for text in detail)
    assert not any("（暫定）" in text for text in texts(md.handle("", now=NOW, paths=paths).flex["contents"][1]["body"]))


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
    assert card["contents"][0]["header"]["contents"][0]["text"] == "美股（1/4）｜資料暫缺"
    snap["rows"]["us.etf.VOO"] = dict(make_snapshot(NOW)["rows"]["us.etf.VOO"], stale_after=NOW - 1)
    snap["rows"]["us.etf.QQQ"] = dict(make_snapshot(NOW)["rows"]["us.etf.QQQ"], stale_after=NOW - 1)
    write_snapshot(paths, snap)
    card = md.handle("", now=NOW, paths=paths).flex
    assert card["contents"][0]["header"]["contents"][0]["text"] == "美股（1/4）｜資料過期"


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
    for kind in ("us", "tw", "iran"):
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
    summary = texts(md.handle("", now=NOW, paths=paths).flex["contents"][3]["body"])
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
    summary = texts(md.handle("", now=NOW, paths=paths).flex["contents"][3]["body"])
    assert "下一個事件：財報（NVDA，預估），還有 3 天" in summary  # 加了「預估」放不下「（美東日期）」
    snap["rows"]["events.next"]["values"]["items"] = snap["rows"]["events.next"]["values"]["items"][1:]
    write_snapshot(paths, snap)
    summary = texts(md.handle("", now=NOW, paths=paths).flex["contents"][3]["body"])
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
    assert "期中選舉：已投票，眾院已結算，民主黨拿下" in texts(summary.flex["contents"][3]["body"])
    # 兩個平台的結果不同（結算規則不同）：摘要不挑一家，請看細節
    quotes[1] = _quote("kalshi", "house", last=0.02, result="no")
    write_snapshot(paths, _election_snapshot(now, "result", quotes))
    assert "期中選舉：已投票，結果看 /盤勢 選舉" in texts(md.handle("", now=now, paths=paths).flex["contents"][3]["body"])
    # 還沒有結算結果：照舊
    quotes = [_quote("polymarket", "house", mid=0.6, last=0.61, updated=now - 600)]
    write_snapshot(paths, _election_snapshot(now, "result", quotes))
    assert "期中選舉：已投票，結果看 /盤勢 選舉" in texts(md.handle("", now=now, paths=paths).flex["contents"][3]["body"])


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
    assert "期中選舉：已投票，等待結果" in texts(md.handle("", now=now, paths=paths).flex["contents"][3]["body"])


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
    line = texts(reply.flex["contents"][3]["body"])[4]
    days = (date(2026, 11, 3) - date(2026, *day)).days
    assert line == f"期中選舉還有 {days} 天，預測市場：台灣已封鎖" and md.row_width(line) <= 24
    assert "暫缺" not in line
    assert len(reply.flex["contents"][3]["body"]["contents"]) == 6  # 摘要卡的列數不增加


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
        rows = texts(md.handle("", now=NOW, paths=paths).flex["contents"][3]["body"])
        assert rows[4] == "期中選舉還有 17 天，預測市場暫缺", extra
        detail = texts(md.handle(" 選舉", now=NOW, paths=paths).flex)
        assert "預測市場：暫缺" in detail and md.TEXT_PREDICTION_BLOCKED not in detail, extra
    snap = _blocked_snapshot(NOW)
    snap["rows"]["election"]["stale_after"] = NOW - 1  # 列過期：不知道現在有沒有被封鎖
    write_snapshot(paths, snap)
    assert texts(md.handle("", now=NOW, paths=paths).flex["contents"][3]["body"])[4] == "期中選舉還有 17 天，預測市場暫缺"
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
