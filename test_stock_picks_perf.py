"""stock_picks_perf（推薦成績，2026-10-28；2026-10-29 改 v2：隔天收盤進場、5／20／60／240 日）的測試：tmp 的盤勢 DB
（盤勢 job 自己的 market_store 建，同 schema）＋conftest 的 tmp state，不連網。紀錄檔用真實的欄位形狀
（line_bot/state/stock_picks_history.jsonl），數字都是編的。"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import market_store
import stock_card_check
import stock_db_ro
import stock_picks
import stock_picks_perf as perf

TPE = ZoneInfo("Asia/Taipei")
D = date.fromisoformat
TW_HOLIDAYS = ("2026-09-25", "2026-09-28", "2026-10-09", "2026-10-26")  # 證交所休市表裡的（中秋、教師節、國慶、光復節）
TYPHOON = "2026-10-23"  # 盤勢 job 判定的臨時休市（calendar_overrides）
US_HOLIDAYS = ("2026-09-07", "2026-11-26")
HOLIDAY_OBS = {
    "year": 2026,
    "rows": [{"date": d, "name": "休市", "note": "", "kind": "closed"} for d in TW_HOLIDAYS]
    + [{"date": "2026-01-02", "name": "開始交易", "note": "", "kind": "trading"}],
    "unknown": [],
}


def _ts(day: str, hour: int = 21) -> float:
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hour, tzinfo=TPE).timestamp()


NOW = _ts("2026-11-30")


def _weekdays(start: str, end: str, skip=()) -> list[str]:
    out, day = [], D(start)
    while day <= D(end):
        if day.weekday() < 5 and day.isoformat() not in skip:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def tw_sessions(last: str = "2026-11-30") -> list[str]:
    return _weekdays("2026-09-01", last, TW_HOLIDAYS + (TYPHOON,))


def us_sessions(last: str = "2026-11-27") -> list[str]:
    return _weekdays("2026-09-01", last, US_HOLIDAYS)


def nth_after(sessions: list[str], t: str, h: int) -> str:
    return [d for d in sessions if d > t][h - 1]


def exit_day(sessions: list[str], t: str, h: int) -> str:
    """v2 的 T_h：E（t 之後第一個交易日）之後第 h 個交易日。"""
    return nth_after(sessions, t, h + 1)


class World:
    """一個測試用的盤勢 DB。"""

    def __init__(self, path: Path):
        self.path = path

    def store(self) -> market_store.Store:
        return market_store.Store(self.path)

    def closes(self, market: str, symbol: str, series: dict) -> None:
        with self.store() as s:
            s.upsert_closes(market, symbol, [(D(d), v, 1) for d, v in series.items()],
                            "nasdaq" if market == "us" else "twse")

    def tw(self, last="2026-11-30", *, typhoon_confirmed=True, typhoon_kind="closed", coverage_to=None,
           holidays=True, bench=None) -> list[str]:
        days = tw_sessions(last)
        series = {d: 50.0 for d in days}
        series.update({d: v for d, v in (bench or {}).items() if d <= last})
        with self.store() as s:
            s.upsert_closes("tw", "TAIEX", [(D(d), 20000.0, None) for d in days], "twse")
            s.upsert_closes("tw", "0050", [(D(d), v, 1) for d, v in series.items()], "twse")
            s.add_coverage("twt49u", D("2025-08-15"), D(coverage_to or last))
            if holidays:
                s.put_obs("tw.holidays.2026", HOLIDAY_OBS, data_date=None, period="2026")
            s.set_calendar_override("tw", D(TYPHOON), typhoon_kind, "allbut0999_empty+finmind_empty",
                                    confirmed=typhoon_confirmed)
        return days

    def us(self, last="2026-11-27") -> list[str]:
        days = us_sessions(last)
        self.closes("us", "SPY", {d: 600.0 for d in days})
        return days


@pytest.fixture()
def world(tmp_path):
    return World(tmp_path / "market_history.sqlite")


def write_jsonl(name: str, lines: list, *, raw_tail: str = "") -> None:
    path = stock_picks.STATE_DIR / name
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines) + raw_tail, encoding="utf-8")


def old_line(t: str, *picks, computed_at=None) -> dict:  # 舊規則紀錄（stock_picks._append_history 的形狀）
    return {"t": t, "market": "neutral",
            "picks": [{"code": c, "name": "測試", "close": v, "score": 4, "p60": 10} for c, v in picks],
            "computed_at": round(computed_at or _ts(t, 16))}  # 預設：t 當天下午就算出來（下一個交易日開盤前）


def ai_line(t: str, *picks, rev=None, computed_at=None) -> dict:  # AI 相關股紀錄（計畫 C；picks 欄位和舊檔不一樣）
    line = {"t": t, "rule": "ai_v1", "computed_at": round(computed_at or _ts(t, 17)), "source": "00952",
            "core_version": "2026-10",
            "picks": [{"code": c, "name": "測試", "close": v, "ai_source": "00952", "p60": 12} for c, v in picks]}
    if rev is not None:
        line["rev"] = rev
    return line


def us_line(t: str, *picks, computed_at=None) -> dict:  # 美股紀錄（stock_picks_us._history_record 的形狀）
    next_day = (D(t) + timedelta(days=1)).isoformat()  # 預設：台北隔天 14:00（＝美東凌晨，下一個交易日開盤前）
    return {"t": t, "market": None,
            "picks": [{"code": c, "name": "TEST CORP", "close": v, "score": 4, "p60": -18} for c, v in picks],
            "computed_at": round(computed_at or _ts(next_day, 14)), "rule": "us_v1"}


def write_state(*, flags=None, ai_first_t=None, prices=None) -> None:
    payload = {"schema": 1, "prices": prices or {}, "ai_flag_by_t": flags or {}, "ai_first_t": ai_first_t}
    (stock_picks.STATE_DIR / perf.STATE_NAME).write_text(json.dumps(payload), encoding="utf-8")


def read_state() -> dict:
    return json.loads((stock_picks.STATE_DIR / perf.STATE_NAME).read_text(encoding="utf-8"))


def write_library(*, prices=None, needed=None) -> None:
    payload = {"schema": 1, "prices": prices or {}, "needed": needed or {}}
    (stock_picks.STATE_DIR / perf.PRICES_NAME).write_text(json.dumps(payload), encoding="utf-8")


def read_library() -> dict:
    return json.loads((stock_picks.STATE_DIR / perf.PRICES_NAME).read_text(encoding="utf-8"))


def run(world: World, *, now: float = NOW, ai: bool = False):
    return perf.update(ai_enabled=ai, now=now, db_path=world.path)


def ledger_rows() -> list[dict]:
    path = stock_picks.STATE_DIR / perf.LEDGER_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def card_texts(card: dict) -> list[str]:
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                out.append(node["text"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(card)
    return out


def _row(t, h, r, b, *, m="tw", rule="v1", rev=1, code="2330", skip=None, th="2026-10-08", v=None) -> dict:
    return {"v": perf.LEDGER_VERSION if v is None else v, "m": m, "rule": rule, "t": t, "rev": rev, "code": code,
            "h": h, "th": th, "r": r, "b": b, "skip": skip}


def _aggregate(rows, *, lines=None, first_t=None) -> dict:
    return perf.aggregate(rows, lines or {}, [], first_t or {"tw": None, "us": None}, now=NOW,
                          ledger_rows=len(rows), ledger_full=False)


# ── 台股：報酬、日曆、除權息 ──────────────────────────────────────────────────
def test_tw_return_vs_0050_at_5_and_20_sessions():
    world = World(stock_picks.STATE_DIR / "market_history.sqlite")  # 預設路徑（stock_db_ro.db_path）
    days = world.tw(bench={"2026-10-12": 51.0})
    assert nth_after(days, "2026-10-01", 1) == "2026-10-02"  # E：隔天收盤進場
    assert exit_day(days, "2026-10-01", 5) == "2026-10-12"  # E 之後第 5 個交易日（跨過國慶）
    assert exit_day(days, "2026-10-01", 20) == "2026-11-04"  # 跨過國慶、颱風、光復節
    ramp = {"2026-10-01": 100.0, "2026-10-02": 104.0, "2026-10-05": 106.0, "2026-10-06": 106.0, "2026-10-07": 106.0,
            "2026-10-08": 106.0}
    world.closes("tw", "2330", {d: 109.2 for d in days if d >= "2026-10-12"} | ramp)
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("2330", 100.0))])
    result = perf.update(ai_enabled=False, now=NOW)
    rows = {r["h"]: r for r in ledger_rows()}
    assert set(rows) == {5, 20}  # 60、240 日還沒到期
    assert rows[5] == {"v": 2, "m": "tw", "rule": "v1", "t": "2026-10-01", "rev": 1, "code": "2330", "h": 5,
                       "th": "2026-10-12", "r": 0.05, "b": 0.02, "skip": None,  # 109.2 ÷ 104（E）− 1，不是 ÷ 100（t）
                       "c0": 100.0, "ce": 104.0, "ch": 109.2, "cf": 1.0,
                       "be": 50.0, "bh": 51.0, "bf": 1.0}  # 算式輸入也存（P3 #6）；c0 只拿來核對紀錄
    assert (rows[20]["th"], rows[20]["r"], rows[20]["b"]) == ("2026-11-04", 0.05, 0.0)
    tw = result["markets"]["tw"]
    assert tw["h"]["5"] == {"n": 1, "r_pm": 50, "b_pm": 20, "wins": 1, "skipped": 0, "pending": 0}
    assert tw["h"]["60"] == tw["h"]["240"] == {"n": 0, "r_pm": None, "b_pm": None, "wins": 0, "skipped": 0,
                                                "pending": 1}
    assert (tw["first_t"], tw["from"], tw["to"], tw["ai_from"]) == ("2026-10-01", "2026-10-01", "2026-11-04", None)
    assert result["schema"] == 2 and perf.load_result() == result
    assert stat_mode(stock_picks.STATE_DIR / perf.LEDGER_NAME) == 0o600


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_exrights_restored_on_next_session_including_closed_day(world):
    world.tw(bench={d: 49.0 for d in tw_sessions() if d >= "2026-10-13"})
    with world.store() as s:  # t＝10/07、E＝10/08、T_5＝10/16
        s.upsert_exrights([
            (D("2026-10-09"), "2886", 50.0, 48.0, "息"),  # E 之後、國慶休市那天除息 → 算在下一個交易日 10/12
            (D("2026-10-08"), "1216", 52.0, 50.0, "息"),  # E 當天除息：E 的收盤已經是除息後的，不還原
            (D("2026-10-19"), "1216", 50.0, 45.0, "息"),  # T_5 之後：這一筆不算
            (D("2026-10-13"), "0050", 50.0, 49.0, "息"),  # 基準 0050 同樣還原
        ])
    after = ("2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15")
    world.closes("tw", "2886", {"2026-10-07": 50.0, "2026-10-08": 50.0, "2026-10-12": 48.0, "2026-10-13": 48.0,
                                "2026-10-14": 48.0, "2026-10-15": 48.0, "2026-10-16": 48.6})
    world.closes("tw", "1216", {"2026-10-07": 52.0, "2026-10-08": 50.0, **{d: 50.0 for d in after},
                                "2026-10-16": 51.0})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-07", ("2886", 50.0), ("1216", 52.0))])
    run(world)
    rows = {(r["code"], r["h"]): r for r in ledger_rows()}
    assert rows[("2886", 5)]["th"] == "2026-10-16"
    assert rows[("2886", 5)]["r"] == pytest.approx(48.6 * (50 / 48) / 50 - 1, abs=1e-6)  # +1.25%（不還原是 −2.8%）
    assert rows[("2886", 5)]["b"] == pytest.approx(0.0, abs=1e-6)  # 0050 除息 50→49，還原後持平
    assert (rows[("2886", 5)]["cf"], rows[("2886", 5)]["bf"]) == (pytest.approx(50 / 48), pytest.approx(50 / 49))
    assert (rows[("2886", 5)]["c0"], rows[("2886", 5)]["ce"], rows[("2886", 5)]["ch"]) == (50.0, 50.0, 48.6)
    assert (rows[("2886", 5)]["be"], rows[("2886", 5)]["bh"]) == (50.0, 49.0)
    assert rows[("1216", 5)]["r"] == pytest.approx(0.02, abs=1e-6) and rows[("1216", 5)]["cf"] == 1.0  # 51 ÷ 50（E）
    assert rows[("1216", 5)]["c0"] == 52.0  # t 的收盤（除息前）只拿來核對紀錄
    assert rows[("2886", 20)]["skip"] == "missing"  # 之後沒有價格、而且已經過了 10 個交易日


def test_waits_until_exrights_cover_t_h(world):  # r3 #1：4552 的收盤 14:44 就進 DB，除息 16:02 才進
    world.tw(last="2026-10-22", coverage_to="2026-10-14")
    before = ("2026-10-07", "2026-10-08", "2026-10-12", "2026-10-13", "2026-10-14")
    world.closes("tw", "4552", {d: 15.15 for d in before} | {d: 14.6 for d in tw_sessions("2026-10-22") if d >= "2026-10-15"})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-07", ("4552", 15.15))])
    result = run(world, now=_ts("2026-10-22"))
    assert ledger_rows() == []  # 除權息只涵蓋到 10/14、T_5 是 10/15：等，不先用「沒有除息」凍結
    assert result["markets"]["tw"]["h"]["5"]["pending"] == 1
    with world.store() as s:
        s.upsert_exrights([(D("2026-10-15"), "4552", 15.15, 14.55, "息")])
        s.add_coverage("twt49u", D("2026-10-15"), D("2026-10-22"))
    run(world, now=_ts("2026-10-22"))
    row = next(r for r in ledger_rows() if r["h"] == 5)
    assert row["skip"] is None and row["r"] == pytest.approx(14.6 / 14.55 - 1, abs=1e-6)


def test_exrights_coverage_must_pass_t_h(world):  # P3 #4：盤勢 job 下一次會重讀「上次涵蓋的最後一天」
    world.tw(last="2026-10-14", coverage_to="2026-10-12")  # T_5（10/12）＝涵蓋的最後一天
    world.closes("tw", "2886", {d: (50.0 if d < "2026-10-12" else 48.5) for d in tw_sessions("2026-10-14")})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("2886", 50.0))])
    run(world, now=_ts("2026-10-14"))
    assert ledger_rows() == []  # 不能先用「10/12 沒有除息」凍結
    with world.store() as s:  # 重讀 10/12：補進當天的除息
        s.upsert_exrights([(D("2026-10-12"), "2886", 50.0, 48.5, "息")])
        s.add_coverage("twt49u", D("2026-10-12"), D("2026-10-14"))
    run(world, now=_ts("2026-10-14"))
    assert [(r["h"], r["r"], r["skip"]) for r in ledger_rows()] == [(5, 0.0, None)]


def test_provisional_typhoon_closure_waits_until_10_days(world):
    days = world.tw(last="2026-10-30", typhoon_confirmed=False)
    assert exit_day(days, "2026-10-16", 5) == "2026-10-28"
    world.closes("tw", "2330", {d: 100.0 for d in days if d >= "2026-10-16"})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-16", ("2330", 100.0))])
    run(world, now=_ts("2026-10-30"))  # 10/23 暫定休市才 7 天：盤勢 job 還可能撤銷 → 等
    run(world, now=_ts("2026-11-02"))  # 第 10 天：還是等
    assert ledger_rows() == []
    run(world, now=_ts("2026-11-03"))  # 超過 10 天才算確定
    assert [(r["h"], r["th"], r["r"]) for r in ledger_rows()] == [(5, "2026-10-28", 0.0)]


@pytest.mark.parametrize("kind, confirmed, now, frozen", [
    ("closed", True, "2026-10-30", True),  # 確認過的臨時休市：馬上算確定
    ("revoked", False, "2026-12-01", False),  # 撤銷了（其實有開）：TAIEX 又沒有那天 → 日曆有洞，一直等
])
def test_confirmed_or_revoked_closure(world, kind, confirmed, now, frozen):
    days = world.tw(last="2026-10-30", typhoon_confirmed=confirmed, typhoon_kind=kind)
    world.closes("tw", "2330", {d: 100.0 for d in days if d >= "2026-10-16"})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-16", ("2330", 100.0))])
    run(world, now=_ts(now))
    assert bool(ledger_rows()) is frozen


def test_calendar_hole_waits_and_t_h_is_right_after_backfill(world):  # r4 #2：TAIEX 會後面先寫、前面後補
    days = world.tw(last="2026-11-13")
    assert exit_day(days, "2026-11-02", 5) == "2026-11-10"
    world.closes("tw", "2330", {d: 100.0 for d in days})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-11-02", ("2330", 100.0))])
    with sqlite3.connect(world.path) as con:  # 11/04 的 TAIEX 還沒補進來
        con.execute("DELETE FROM closes WHERE market = 'tw' AND symbol = 'TAIEX' AND date = '2026-11-04'")
    con.close()
    run(world, now=_ts("2026-11-13"))
    assert ledger_rows() == []  # 不然會把 11/11 當成 E 之後第 5 個交易日
    world.tw(last="2026-11-13")
    run(world, now=_ts("2026-11-13"))
    assert [(r["h"], r["th"]) for r in ledger_rows()] == [(5, "2026-11-10")]


def test_missing_holiday_table_waits_only_when_needed(world):
    days = world.tw(last="2026-10-22", holidays=False)
    world.closes("tw", "2330", {d: 100.0 for d in days})
    write_jsonl(stock_picks.HISTORY_NAME,
                [old_line("2026-10-07", ("2330", 100.0)), old_line("2026-10-12", ("2330", 100.0))])
    run(world, now=_ts("2026-10-22"))
    got = {(r["t"], r["h"]) for r in ledger_rows()}
    assert got == {("2026-10-12", 5)}  # [10/12, T_5＝10/20] 每天都有 TAIEX：用不到休市表
    # 10/07 跨過國慶（10/09）：休市表讀不到 → 等（T_5 之後才 5 個交易日，還沒到 10 天的出口）


def test_c0_must_match_the_record(world):
    days = world.tw()
    world.closes("tw", "2303", {d: 100.6 for d in days})  # 和紀錄差 0.6%
    world.closes("tw", "2317", {d: 100.5 for d in days})  # 差 0.5%：還算一樣
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("2303", 100.0), ("2317", 100.0))])
    run(world)
    got = {(r["code"], r["h"]): r["skip"] for r in ledger_rows()}
    assert got == {("2303", 5): "mismatch", ("2303", 20): "mismatch", ("2317", 5): None, ("2317", 20): None}


def test_corporate_action_jumps_freeze_as_jump(world):
    days = world.tw()
    world.closes("tw", "2603", {d: (112.0 if d >= "2026-10-05" else 100.0) for d in days})  # 一天 +12%：公司行動
    world.closes("tw", "2609", {d: (110.0 if d >= "2026-10-05" else 100.0) for d in days})  # +10% 漲停：算數
    world.closes("tw", "2615", {d: 100.0 for d in days})
    with world.store() as s:
        s.upsert_exrights([(D("2026-10-06"), "2615", 70.0, 20.0, "權")])  # 比值 3.5：不在 1–3
    write_jsonl(stock_picks.HISTORY_NAME,
                [old_line("2026-10-01", ("2603", 100.0), ("2609", 100.0), ("2615", 100.0))])
    run(world)
    got = {(r["code"], r["h"]): (r["skip"], r["r"]) for r in ledger_rows()}
    assert got[("2603", 5)] == ("jump", None)
    assert got[("2609", 5)] == (None, 0.1)
    assert got[("2615", 5)] == ("jump", None)


def test_benchmark_jump_also_skips(world):
    days = world.tw(bench={d: 57.5 for d in tw_sessions() if d >= "2026-10-05"})  # 0050 一天 +15%
    world.closes("tw", "2330", {d: 100.0 for d in days})
    write_jsonl(stock_picks.HISTORY_NAME,
                [old_line("2026-10-01", ("2330", 100.0)), old_line("2026-10-12", ("2330", 100.0))])
    run(world)
    got = {(r["t"], r["h"]): r["skip"] for r in ledger_rows()}
    assert got[("2026-10-01", 5)] == "jump" and got[("2026-10-12", 5)] is None


# ── 台股價格庫、缺價、帳只增不改 ─────────────────────────────────────────────
def _library_scenario(world: World) -> dict:
    """6666 不是 0050 成分（DB tw 沒有）：10/01、10/02 的收盤只在 tw_all 留 10 個交易日。"""
    world.tw(last="2026-10-02")
    world.closes("tw_all", "6666", {"2026-10-01": 80.0, "2026-10-02": 81.0})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("6666", 80.0))])
    run(world, now=_ts("2026-10-02"))
    assert read_library()["prices"] == {"6666": {"2026-10-01": 80.0, "2026-10-02": 81.0}}  # 0050 在 tw 裡：不存
    days = world.tw(last="2026-10-30")
    world.closes("tw_all", "6666", {d: 84.0 for d in days if d >= "2026-10-05"})
    with world.store() as s:  # tw_all 滾掉 10/01、10/02（盤勢 job 每檔只留最近幾列）
        s.prune("tw_all", 17)
    with sqlite3.connect(world.path) as con:
        assert con.execute("SELECT min(date) FROM closes WHERE market='tw_all' AND symbol='6666'").fetchone() == (
            "2026-10-05",)
    con.close()
    return run(world, now=_ts("2026-10-30"))


def test_price_library_keeps_tw_all_closes_until_frozen(world):
    result = _library_scenario(world)
    [row] = ledger_rows()
    assert (row["h"], row["r"], row["skip"]) == (5, pytest.approx(84 / 81 - 1, abs=1e-6), None)  # E＝10/02 的 81
    assert (row["c0"], row["ce"]) == (80.0, 81.0)  # t 的收盤也從價格庫來（核對紀錄）
    prices = read_library()["prices"]
    assert set(prices) == {"6666"} and prices["6666"]["2026-10-01"] == 80.0  # h=20、60、240 還沒凍結：價格還留著
    assert result["markets"]["tw"]["h"]["20"]["pending"] == 1
    assert not (stock_picks.STATE_DIR / perf.STATE_NAME).exists() or read_state()["prices"] == {}  # 不寫進共用狀態檔


def test_results_do_not_change_after_library_and_tw_all_are_gone(world):
    before = _library_scenario(world)["markets"]["tw"]
    ledger_before = (stock_picks.STATE_DIR / perf.LEDGER_NAME).read_bytes()
    (stock_picks.STATE_DIR / perf.PRICES_NAME).unlink()
    with sqlite3.connect(world.path) as con:
        con.execute("DELETE FROM closes WHERE market = 'tw_all'")
    con.close()
    after = run(world, now=_ts("2026-10-30"))["markets"]["tw"]
    assert (stock_picks.STATE_DIR / perf.LEDGER_NAME).read_bytes() == ledger_before  # 只增不改，也沒有重算
    assert after == before
    world.tw()  # 一個月後：h=20 的價格已經拿不到 → 凍結成缺價；h=5 的成績不變
    late = run(world)["markets"]["tw"]
    assert late["h"]["5"] == before["h"]["5"] | {"pending": 0}
    assert late["h"]["20"]["skipped"] == 1
    assert read_library()["prices"] == {}  # 60、240 日還在等，但價格已經哪裡都拿不到：價格庫是空的


def test_price_gone_from_tw_all_freezes_as_missing_after_10_sessions(world):
    days = world.tw(last="2026-10-30")
    world.closes("tw_all", "7777", {d: 30.0 for d in days if d >= "2026-10-16"})  # bot 沒收到 10/01–10/15
    world.closes("tw_all", "8888", {d: 30.0 for d in days if d >= "2026-10-16"})
    write_jsonl(stock_picks.HISTORY_NAME,
                [old_line("2026-10-01", ("7777", 30.0)), old_line("2026-10-15", ("8888", 30.0))])
    result = run(world, now=_ts("2026-10-30"))
    # 7777：T_5＝10/08，之後已經 13 個交易日 → 缺價凍結；8888：T_5＝10/22，之後才 4 天 → 還可能補上，等
    assert {(r["code"], r["h"]): r["skip"] for r in ledger_rows()} == {("7777", 5): "missing"}
    h5 = result["markets"]["tw"]["h"]["5"]
    assert (h5["n"], h5["skipped"], h5["pending"]) == (0, 1, 1)


def test_db_busy_waits_without_failing(world):
    days = world.tw()
    world.closes("tw", "2330", {d: 100.0 for d in days})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("2330", 100.0))])
    locker = sqlite3.connect(world.path, timeout=0)
    locker.execute("BEGIN EXCLUSIVE")  # 盤勢 job 正在 commit
    try:
        result = run(world)
    finally:
        locker.rollback()
        locker.close()
    assert result is not None and ledger_rows() == []
    assert result["markets"]["tw"]["h"]["5"]["pending"] == 0  # 日曆讀不到：這一輪連來源都不選
    run(world)
    assert len(ledger_rows()) == 2


def test_unreadable_ai_history_skips_tw_this_round(world):  # r4 #1：AI 檔讀不了，不能當成沒有 AI 紀錄
    days = world.tw()
    world.closes("tw", "2330", {d: 100.0 for d in days})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("2330", 100.0))])
    (stock_picks.STATE_DIR / stock_picks.AI_HISTORY_NAME).mkdir()  # 不是一般檔：讀不了
    write_state(prices={"6666": {"2026-10-01": 80.0}})  # v1 的價格庫
    write_library(prices={"6666": {"2026-10-01": 80.0}})
    assert run(world) is not None
    assert ledger_rows() == []
    assert read_library()["prices"] == {"6666": {"2026-10-01": 80.0}}  # 價格庫原樣留著
    assert read_state()["prices"] == {"6666": {"2026-10-01": 80.0}}


# ── 台股紀錄來源（舊規則／AI 相關股） ──────────────────────────────────────────
def test_tw_source_is_chosen_at_freeze_time(world):
    days = world.tw()
    for code in ("1101", "1104", "2302", "2305"):
        world.closes("tw", code, {d: 100.0 for d in days})
    old = [("2026-10-01", "1101"), ("2026-10-02", "1102"), ("2026-10-05", "1103"), ("2026-10-06", "1104"),
           ("2026-10-07", "1105"), ("2026-10-08", "1106"), ("2026-10-12", "1107")]
    write_jsonl(stock_picks.HISTORY_NAME, [old_line(t, (c, 100.0)) for t, c in old])
    write_jsonl(stock_picks.AI_HISTORY_NAME, [ai_line("2026-10-05", ("2301", 100.0)),
                                              ai_line("2026-10-05", ("2302", 100.0), rev=2),
                                              ai_line("2026-10-07", ("2305", 100.0))])
    write_state(flags={"2026-10-02": True, "2026-10-06": False, "2026-10-07": False})
    run(world, ai=True)
    got = {(r["t"], r["rule"], r["rev"], r["code"]) for r in ledger_rows() if r["h"] == 5}
    assert got == {
        ("2026-10-01", "v1", 1, "1101"),  # ④ 沒記到開關、早於第一個 AI 紀錄（10/05）→ 舊檔
        ("2026-10-05", "ai_v1", 2, "2302"),  # ① AI 檔有 → rev 最大那一行（rev 1 的 2301 不算）
        ("2026-10-06", "v1", 1, "1104"),  # ③ 記到 AI 關著 → 舊檔
        ("2026-10-07", "ai_v1", 1, "2305"),  # ① AI 檔有 t 就用 AI（家人最後看到的是 AI 卡）
    }  # 10/02：② 記到開著、AI 檔沒有 → 不算；10/08：④ 沒記到、不早於 10/05 → 不猜；10/12 是最新一行，記成開著
    assert read_state()["ai_flag_by_t"]["2026-10-12"] is True
    result = perf.load_result()
    assert result["markets"]["tw"]["ai_from"] == "2026-10-05"
    assert "10/05 起含 AI 相關股推薦" in card_texts(perf.perf_bubble(result))


def test_late_ai_record_never_becomes_the_shadow_pick(world):  # r4 #1 的反例
    world.tw(last="2026-10-02")
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("1101", 100.0))])  # 舊檔先寫（台股 _tick）
    run(world, now=_ts("2026-10-01", 15), ai=True)  # AI 段還沒算完：只記下「10/01 那天 AI 開著」
    write_jsonl(stock_picks.AI_HISTORY_NAME, [ai_line("2026-10-01", ("2301", 100.0))])  # 20 分鐘後才寫
    write_jsonl(stock_picks.HISTORY_NAME,
                [old_line("2026-10-01", ("1101", 100.0)), old_line("2026-10-02", ("1102", 100.0))])
    run(world, now=_ts("2026-10-02"), ai=True)  # 10/02：AI 從頭到尾都沒有算出來
    days = world.tw()
    for code in ("1101", "1102", "2301"):
        world.closes("tw", code, {d: 100.0 for d in days})
    run(world, ai=True)
    assert {(r["t"], r["code"]) for r in ledger_rows()} == {("2026-10-01", "2301")}


def test_ai_flag_only_for_the_latest_old_t_and_never_changed(world):  # 沒有 DB 也照記（價格收集之外的部分）
    write_jsonl(stock_picks.HISTORY_NAME,
                [old_line("2026-10-01", ("1101", 100.0)), old_line("2026-10-02", ("1102", 100.0))])
    assert run(world, now=_ts("2026-10-02"), ai=True) is not None
    assert read_state()["ai_flag_by_t"] == {"2026-10-02": True}  # 晚才看到的 10/01 不補記
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("1101", 100.0)),
                                           old_line("2026-10-02", ("1102", 100.0)),
                                           old_line("2026-10-05", ("1103", 100.0))])
    run(world, now=_ts("2026-10-05"), ai=False)
    run(world, now=_ts("2026-10-05"), ai=True)  # 記過不改
    assert read_state()["ai_flag_by_t"] == {"2026-10-02": True, "2026-10-05": False}


def test_truncated_ai_file_does_not_turn_ai_days_into_old_picks(world):
    world.tw(last="2026-10-07")
    old = [("2026-10-01", "1101"), ("2026-10-02", "1102"), ("2026-10-05", "1103"), ("2026-10-06", "1104"),
           ("2026-10-07", "1105")]
    write_jsonl(stock_picks.HISTORY_NAME, [old_line(t, (c, 100.0)) for t, c in old])
    write_jsonl(stock_picks.AI_HISTORY_NAME, [ai_line("2026-10-05", ("2303", 100.0)),
                                              ai_line("2026-10-06", ("2304", 100.0))])
    run(world, now=_ts("2026-10-07"), ai=True)
    assert read_state()["ai_first_t"] == "2026-10-05"
    # AI 檔被截斷：前面的行不見了，最後一行還寫到一半
    write_jsonl(stock_picks.AI_HISTORY_NAME, [ai_line("2026-10-06", ("2304", 100.0))],
                raw_tail='{"t":"2026-10-07","rule":"ai_v1","pic')
    days = world.tw()
    for code in ("1101", "1102", "1103", "1105", "2304"):
        world.closes("tw", code, {d: 100.0 for d in days})
    run(world, ai=True)
    got = {(r["t"], r["code"]) for r in ledger_rows() if r["h"] == 5}
    # 10/05：AI 檔沒有了、沒記到開關，但不早於 ai_first_t → 不拿舊檔的 1103 補；10/07：記到開著 → 不算
    assert got == {("2026-10-01", "1101"), ("2026-10-02", "1102"), ("2026-10-06", "2304")}
    assert read_state()["ai_first_t"] == "2026-10-05"


# ── 成績帳：去重、只增不改、滿了 ─────────────────────────────────────────────
def test_same_key_is_written_once_even_with_a_concurrent_writer(world, monkeypatch):
    days = world.tw()
    world.closes("tw", "2330", {d: 100.0 for d in days})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("2330", 100.0))])
    other = _row("2026-10-01", 5, 0.123, 0.0)  # 另一個程序（部署時新舊程序重疊）剛寫的同一個鍵
    write_jsonl(perf.LEDGER_NAME, [other])
    real = perf._read_ledger
    calls = []

    def stale_first(path):  # 鎖外那次讀到的是還沒有這一行的帳
        calls.append(path)
        return perf._Ledger([], set(), 0, True) if len(calls) == 1 else real(path)

    monkeypatch.setattr(perf, "_read_ledger", stale_first)
    result = run(world)
    rows = ledger_rows()
    assert [r for r in rows if r["h"] == 5] == [other]  # 鎖內重讀：不重複寫，先寫的算
    assert len(rows) == 2 and result["markets"]["tw"]["h"]["5"]["r_pm"] == 123
    run(world)
    assert len(ledger_rows()) == 2


def test_duplicate_and_broken_ledger_lines(world):
    world.tw()
    first, again = _row("2026-10-01", 5, 0.1, 0.0), _row("2026-10-01", 5, 0.5, 0.0)
    write_jsonl(perf.LEDGER_NAME, [first, again, {"v": 1, "m": "tw", "skip": "bogus"}],
                raw_tail='{"v":1,"m":"tw","rule"')  # 最後一行寫到一半
    h5 = run(world)["markets"]["tw"]["h"]["5"]
    assert (h5["n"], h5["r_pm"]) == (1, 100)  # 同一個鍵只算第一次寫的；壞行跳過
    days = tw_sessions()
    world.closes("tw", "2330", {d: 100.0 for d in days})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-02", ("2330", 100.0))])
    raw = (stock_picks.STATE_DIR / perf.LEDGER_NAME).read_text(encoding="utf-8")
    assert not raw.endswith("\n")
    run(world)
    lines = (stock_picks.STATE_DIR / perf.LEDGER_NAME).read_text(encoding="utf-8").splitlines()
    assert lines[3] == '{"v":1,"m":"tw","rule"'  # 先補換行：新的列不會黏在寫到一半的那行後面
    assert [json.loads(x)["t"] for x in lines[4:]] == ["2026-10-02", "2026-10-02"]


def test_full_ledger_is_reported_and_rows_keep_waiting(world, monkeypatch):
    monkeypatch.setattr(perf, "LEDGER_MAX_BYTES", 100)
    days = world.tw()
    world.closes("tw", "2330", {d: 100.0 for d in days})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("2330", 100.0))])
    result = run(world)
    assert ledger_rows() == [] and result["ledger_full"] is True and result["pending"] == 4  # 5、20 日寫不進去＋60、240 日
    assert perf.summary()["ledger_full"] is True


# ── 美股 ─────────────────────────────────────────────────────────────────────
def test_us_returns_splits_quarantine_mismatch_range_and_no_jump_gate(world):
    days = world.us()
    t, e = "2026-10-01", "2026-10-02"
    assert (exit_day(days, t, 5), exit_day(days, t, 20)) == ("2026-10-09", "2026-10-30")
    later = [d for d in days if d > e]
    world.closes("us", "NVDA", {t: 200.0, e: 200.0} | {d: 130.0 for d in later})  # E 之後跌 35%：真的大跌要算進去
    world.closes("us", "AAPL", {d: 250.0 for d in days})
    world.closes("us", "MSFT", {t: 200.0, e: 200.0} | {d: 210.0 for d in later})  # 11/05 一拆二：DB 已經整段調整過
    world.closes("us", "AMZN", {d: 180.0 for d in days})
    world.closes("us", "META", {d: 310.0 for d in days})  # 紀錄是 300：對不上
    world.closes("us", "TSLA", {t: 10.0, e: 10.0} | {d: 45.0 for d in later})  # +350%：當資料錯
    with world.store() as s:
        s.upsert_splits([("AAPL", D("2026-10-06"), 4.0, 1.0), ("MSFT", D("2026-11-05"), 2.0, 1.0)], "nasdaq")
        s.add_quarantine("us", "AMZN", D("2026-10-05"), D("2026-10-06"), "jump")
    write_jsonl(perf.US_HISTORY_NAME, [us_line(t, ("NVDA", 200.0), ("AAPL", 250.0), ("MSFT", 400.0),
                                               ("AMZN", 180.0), ("META", 300.0), ("TSLA", 10.0))])
    result = run(world)
    got = {(r["code"], r["h"]): (r["skip"], r["r"]) for r in ledger_rows() if r["m"] == "us"}
    assert got[("NVDA", 5)] == (None, -0.35) and got[("NVDA", 20)] == (None, -0.35)
    assert got[("AAPL", 5)] == ("split", None) and got[("AAPL", 20)] == ("split", None)
    assert got[("MSFT", 5)] == (None, 0.05)  # 分割在 T_h 之後：c0（200）和紀錄（400）不比
    assert got[("AMZN", 5)] == ("quarantine", None)  # 已經過了 10 個交易日還沒解除
    assert got[("META", 5)] == ("mismatch", None)
    assert got[("TSLA", 5)] == ("range", None)
    us = result["markets"]["us"]
    assert us["h"]["5"] == {"n": 2, "r_pm": -150, "b_pm": 0, "wins": 1, "skipped": 4, "pending": 0}
    texts = card_texts(perf.perf_bubble(result))
    assert "美股推薦（美國時間 10/01 起，算到 10/30）" in texts
    assert "推薦後 5 個交易日：紀錄還不夠（目前 2 次）" in texts
    assert texts.count("另有 4 次因為資料問題沒有算進去") == 2


def test_us_waits_for_4_sessions_after_t_h_and_for_quarantine(world):
    t = "2026-10-01"  # E＝10/02、T_5＝10/09
    world.closes("us", "NVDA", {d: 200.0 for d in us_sessions()})
    world.closes("us", "AMZN", {d: 180.0 for d in us_sessions()})
    with world.store() as s:
        s.add_quarantine("us", "AMZN", D("2026-10-05"), D("2026-10-05"), "jump")
    write_jsonl(perf.US_HISTORY_NAME, [us_line(t, ("NVDA", 200.0), ("AMZN", 180.0))])
    world.us(last="2026-10-14")  # 10/09 之後只有 3 個交易日：盤勢 job 還會改寫 10/09 的收盤
    run(world)
    assert ledger_rows() == []
    world.us(last="2026-10-15")
    run(world)
    assert [(r["code"], r["h"], r["th"]) for r in ledger_rows()] == [("NVDA", 5, "2026-10-09")]  # AMZN 隔離中：等
    with world.store() as s:
        s.clear_quarantine("us", "AMZN")
    run(world)
    assert [(r["code"], r["skip"]) for r in ledger_rows() if r["code"] == "AMZN"] == [("AMZN", None)]


def test_us_waits_until_the_pick_itself_has_the_4th_session(world):
    # 盤勢 job 照字母分批寫：SPY 已經有 10/15，NVDA 那一批還沒跑 → NVDA 的 10/09（T_5）還會再被整段重抓改寫一次
    days = world.us(last="2026-10-15")
    world.closes("us", "NVDA", {d: 200.0 for d in days if d <= "2026-10-14"})
    write_jsonl(perf.US_HISTORY_NAME, [us_line("2026-10-01", ("NVDA", 200.0))])
    result = run(world)
    assert ledger_rows() == [] and result["markets"]["us"]["h"]["5"]["pending"] == 1
    world.closes("us", "NVDA", {"2026-10-15": 200.0})
    run(world)
    assert [(r["code"], r["h"], r["th"], r["r"]) for r in ledger_rows()] == [("NVDA", 5, "2026-10-09", 0.0)]


# ── 彙總與卡片 ───────────────────────────────────────────────────────────────
def test_averages_round_half_up_after_averaging():
    days = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"]
    rows = [_row(t, 5, 0.0185, -0.0185) for t in days] + [_row(t, 20, 0.01849, 0.0, th="2026-11-03") for t in days]
    rows += [_row("2026-10-08", 5, 0.01, 0.0271, code="2303"), _row("2026-10-08", 5, 0.0271, 0.01, code="2317")]
    res = _aggregate(rows)
    h5, h20 = res["markets"]["tw"]["h"]["5"], res["markets"]["tw"]["h"]["20"]
    assert (h5["n"], h5["wins"], h20["r_pm"], h20["b_pm"]) == (7, 6, 18, 0)
    assert (h5["r_pm"], h5["b_pm"]) == (19, -8)  # 先平均再四捨五入到 0.1%（1.8514% → 1.9%、−0.7914% → −0.8%）
    only_half = _aggregate([_row(t, 5, 0.0185, -0.0185) for t in days])["markets"]["tw"]["h"]["5"]
    assert (only_half["r_pm"], only_half["b_pm"]) == (19, -19)  # 1.85% → 1.9%，正負對稱
    texts = card_texts(perf.perf_bubble(_aggregate([_row(t, 5, 0.0185, -0.0185) for t in days])))
    assert "推薦後 5 個交易日：平均漲 1.9%，0050 同期跌 1.9%" in texts
    assert "5 次推薦裡有 5 次比 0050 好" in texts
    flat = card_texts(perf.perf_bubble(_aggregate([_row(t, 5, 0.0, 0.0004) for t in days])))
    assert "推薦後 5 個交易日：平均持平，0050 同期持平" in flat


def test_fewer_than_5_says_not_enough():
    rows = [_row("2026-10-01", 5, 0.05, 0.01), _row("2026-10-02", 5, 0.02, 0.03), _row("2026-10-05", 5, -0.01, 0.0)]
    res = _aggregate(rows, first_t={"tw": "2026-10-01", "us": "2026-10-29"})
    texts = card_texts(perf.perf_bubble(res))
    assert "台股推薦（10/01 起，算到 10/08）" in texts
    assert "推薦後 5 個交易日：紀錄還不夠（目前 3 次）" in texts
    assert "推薦後 20 個交易日：紀錄還不夠（目前 0 次）" in texts
    assert "美股推薦還沒有成績（美國時間 10/29 開始記）" in texts
    assert not any("平均" in t for t in texts)
    alt = perf.perf_alt_text(res)
    assert alt == "股票推薦：只供參考｜推薦成績｜台股推薦後 5 個交易日紀錄還不夠（目前 3 次）｜美股推薦還沒有成績"


def test_aggregate_pins_the_first_written_group_per_day():  # P3 #2
    a1 = _row("2026-10-05", 5, 0.1, 0.0, rule="ai_v1", rev=1, code="2301")
    old = _row("2026-10-05", 5, 0.3, 0.0, code="1103")
    a2 = _row("2026-10-05", 20, 0.2, 0.0, rule="ai_v1", rev=2, code="2302", th="2026-11-02")
    rows = [a1, old, a2]
    h = _aggregate(rows)["markets"]["tw"]["h"]
    assert (h["5"]["n"], h["5"]["r_pm"], h["20"]["n"]) == (1, 100, 0)  # 同一天只算第一次寫的那一組（ai_v1 rev 1）
    rev2 = {("tw", "2026-10-05"): perf.Record("2026-10-05", 2, "ai_v1", (perf.Pick("2302", 100.0),), 0.0)}
    assert _aggregate(rows, lines=rev2)["markets"]["tw"]["h"]["5"]["r_pm"] == 100  # 紀錄後來多了 rev 2：不換
    moved = {("tw", "2026-10-05"): perf.Record("2026-10-05", 1, "ai_v1", (perf.Pick("2399", 100.0),), 0.0)}
    assert _aggregate(rows, lines=moved)["markets"]["tw"]["h"]["5"]["n"] == 0  # 那一行被改過：舊代號不算


@pytest.mark.parametrize("case", ["none", "invalid", "empty", "full", "extreme"])
def test_cards_and_alt_text_pass_card_ok(case):
    days = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"]
    ai_days = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]
    rows = [_row(t, 5, 0.03, 0.01) for t in days]
    rows += [_row(t, 5, 0.02, 0.0, rule="ai_v1", code="2454", th="2026-10-22") for t in ai_days]
    rows += [_row("2026-10-08", 5, None, None, skip="missing"), _row("2026-10-08", 20, None, None, skip="jump")]
    rows += [_row(t, 5, -0.02, 0.01, m="us", rule="us_v1", code="BRK.B", th="2026-10-08") for t in days]
    results = {
        "none": None,
        "invalid": {"schema": 99},
        "empty": _aggregate([]),
        "full": _aggregate(rows, first_t={"tw": "2026-10-01", "us": "2026-10-01"}),
        "extreme": _aggregate([_row(t, h, 2.999 if h <= 20 else 48.999, -0.899 if h <= 20 else -0.989)
                               for t in days * 2000 for h in perf.HORIZONS][:40000]),
    }
    result = results[case]
    card, alt = perf.perf_bubble(result), perf.perf_alt_text(result)
    assert stock_card_check.card_ok(card, alt)
    texts = card_texts(card)
    assert texts[0] == "股票推薦：推薦成績" and alt.startswith("股票推薦：只供參考｜推薦成績｜")
    joined = "".join(texts + [alt]).replace(stock_picks.TEXT_DISCLAIMER, "")
    assert not any(word in joined for word in ("買", "賣", "等", "停損"))
    if case in ("none", "invalid"):
        assert texts[1] == "推薦成績計算中，請稍後再按一次" and alt.endswith("計算中，請稍後再按一次")
    else:
        assert "過去的表現不代表以後；只供參考，不是買賣指示" in texts
        assert "台股含股利（除權息還原）、和 0050 比；美股只算股價、和 SPY 比" in texts
    if case == "empty":
        assert "台股推薦還沒有成績" in texts and "美股推薦還沒有成績" in texts
    if case == "full":
        assert "台股推薦（10/01 起，算到 10/22）" in texts and "10/12 起含 AI 相關股推薦" in texts
        assert "推薦後 5 個交易日：平均漲 2.5%，0050 同期漲 0.5%" in texts
        assert "10 次推薦裡有 10 次比 0050 好" in texts
        assert texts.count("另有 1 次因為資料問題沒有算進去") == 2
        assert "美股推薦（美國時間 10/01 起，算到 10/08）" in texts
        assert "推薦後 5 個交易日：平均跌 2.0%，SPY 同期漲 1.0%" in texts
        assert alt == ("股票推薦：只供參考｜推薦成績｜台股推薦後 5 個交易日平均漲 2.5%（0050 漲 0.5%）"
                       "｜美股推薦後 5 個交易日平均跌 2.0%（SPY 漲 1.0%）")


# ── 結果檔、summary、介面 ────────────────────────────────────────────────────
def test_summary_reads_only_the_result_file(world, monkeypatch):
    assert perf.summary() == {"perf_date": None, "ledger_rows": None, "ledger_full": False, "library_full": False,
                              "pending": None, "skipped": None}
    days = world.tw()
    world.closes("tw", "2330", {d: 100.0 for d in days})
    world.closes("tw", "2303", {d: 101.0 for d in days})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("2330", 100.0), ("2303", 100.0)),
                                           old_line("2026-11-27", ("2330", 100.0))])
    result = run(world)

    def boom(*_a, **_k):
        raise AssertionError("summary must not open the DB or write files")

    monkeypatch.setattr(stock_db_ro, "rows", boom)
    monkeypatch.setattr(stock_picks, "_write_json_file", boom)
    # pending：11/27 那天的 4 個天數＋10/01 兩檔的 60、240 日
    assert perf.summary() == {"perf_date": "2026-11-30", "ledger_rows": 4, "ledger_full": False,
                              "library_full": False, "pending": 8, "skipped": 2}
    assert perf.load_result() == result


def test_update_never_raises(world, monkeypatch):
    world.tw()
    (stock_picks.STATE_DIR / perf.STATE_NAME).write_text("{not json", encoding="utf-8")
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("2330", 100.0))], raw_tail="garbage\n[1,2]\n")
    assert run(world) is not None  # 壞的狀態檔當作空的重來、紀錄壞行跳過
    assert read_state()["schema"] == 1

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(perf, "aggregate", boom)
    assert run(world) is None  # 只記 log，不丟出去
    (stock_picks.STATE_DIR / perf.RESULT_NAME).write_text('{"schema": 1}', encoding="utf-8")
    assert perf.load_result() is None


def test_borrowed_names_and_interface():
    for name in ("STATE_DIR", "HISTORY_NAME", "AI_HISTORY_NAME", "AI_RULE", "PREFIX", "TEXT_DISCLAIMER",
                 "_state_file_lock", "_read_json_file", "_write_json_file", "_fmt_date", "_fmt_tenths",
                 "_permille", "_text", "_separator", "_bubble"):
        assert hasattr(stock_picks, name), name
    assert stock_picks.AI_RULE == "ai_v1"
    assert (perf.US_HISTORY_NAME, perf.RESULT_NAME, perf.LEDGER_NAME, perf.STATE_NAME, perf.PRICES_NAME) == (
        "stock_picks_us_history.jsonl", "stock_picks_perf.json", "stock_picks_perf_ledger.jsonl",
        "stock_picks_perf_state.json", "stock_picks_perf_prices.json")
    assert (perf.HORIZONS, perf.CARD_HORIZONS, perf.MIN_N, perf.LEDGER_MAX_BYTES) == (
        (5, 20, 60, 240), (5, 20), 5, 10 * 1024 * 1024)
    assert (perf.LEDGER_VERSION, perf.STATE_SCHEMA, perf.RESULT_SCHEMA) == (2, 1, 2)
    assert perf.REVIEW_DB == Path("stock_picks_review") / "review.sqlite"
    params = inspect.signature(perf.update).parameters
    assert list(params) == ["ai_enabled", "now", "db_path"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())
    for fn in (perf.load_result, perf.summary):
        assert not inspect.signature(fn).parameters
    for fn in (perf.perf_bubble, perf.perf_alt_text):
        assert list(inspect.signature(fn).parameters) == ["result"]


def test_does_not_import_the_us_module_or_market_jobs():
    tree = ast.parse(Path(perf.__file__).read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert "stock_picks_us" not in imported and "main" not in imported
    assert not any(name.startswith("market_") for name in imported)


# ── 實作後審查（P3／P4）補的 ─────────────────────────────────────────────────
def test_rows_computed_after_e_close_do_not_count(world):  # P3 #1：不偷看答案（v2 的界線＝E 收盤 13:30）
    days = world.tw()
    for code in ("1101", "1102", "1103", "1104"):
        world.closes("tw", code, {d: 100.0 for d in days})
    world.closes("tw", "2301", {d: (100.0 if d == "2026-10-01" else 110.0) for d in days})
    write_jsonl(stock_picks.HISTORY_NAME, [
        old_line("2026-10-01", ("1101", 100.0)),  # 10/01 下午算出來：家人當天看到
        old_line("2026-10-02", ("1102", 100.0), computed_at=_ts("2026-10-05", 13) + 1800),  # E 13:30 整才算出來：不算
        old_line("2026-10-05", ("1103", 100.0), computed_at=_ts("2026-10-06", 13) + 1799),  # 13:29:59：還來得及
        old_line("2026-10-06", ("1104", 100.0), computed_at=_ts("2026-10-07", 9) + 1800),  # 開盤後、收盤前（v1 不算）
    ])
    # 反例 S1：快照卡在 10/01，10/09 才開 AI，AI 對 t＝10/01 算出 2301（這時 2301 已經漲了 10%）
    write_jsonl(stock_picks.AI_HISTORY_NAME,
                [ai_line("2026-10-01", ("2301", 100.0), computed_at=_ts("2026-10-09", 10))])
    write_state(flags={"2026-10-01": False})
    run(world)
    got = {(r["t"], r["rule"], r["code"]) for r in ledger_rows() if r["h"] == 5}
    assert got == {("2026-10-01", "v1", "1101"), ("2026-10-05", "v1", "1103"), ("2026-10-06", "v1", "1104")}
    assert read_state()["ai_first_t"] is None  # 晚算出來的 AI 行也不算「看過的第一個 AI 紀錄」


def test_us_rows_must_be_computed_before_the_new_york_e_close(world):  # 美東 13:00（半日市也收了），夏令時間照當地
    days = world.us()
    for code in ("NVDA", "AAPL", "MSFT", "AMZN"):
        world.closes("us", code, {d: 100.0 for d in days})
    write_jsonl(perf.US_HISTORY_NAME, [
        us_line("2026-10-01", ("NVDA", 100.0), computed_at=_ts("2026-10-03", 0) + 3599),  # E＝10/02 12:59:59（EDT）
        us_line("2026-10-05", ("AAPL", 100.0), computed_at=_ts("2026-10-07", 1)),  # E＝10/06 13:00（EDT）整：不算
        us_line("2026-10-30", ("AMZN", 100.0), computed_at=_ts("2026-11-03", 1) + 1800),  # E＝11/02 12:30（EST，夏令剛結束）
        us_line("2026-11-02", ("MSFT", 100.0), computed_at=_ts("2026-11-04", 1) + 3599),  # E＝11/03 12:59:59（EST）
    ])
    run(world)
    assert {(r["t"], r["code"]) for r in ledger_rows()} == {
        ("2026-10-01", "NVDA"), ("2026-10-30", "AMZN"), ("2026-11-02", "MSFT")}


def test_next_session_unknown_waits_but_prices_are_collected(world):
    world.tw(last="2026-10-02")
    world.closes("tw_all", "6666", {"2026-10-01": 80.0, "2026-10-02": 81.0})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-02", ("6666", 81.0))])
    result = run(world, now=_ts("2026-10-02"))  # E（10/02 之後的交易日）還不在日曆上：還判不了這一行算不算
    assert result["markets"]["tw"]["h"]["5"]["pending"] == 0
    library = read_library()
    assert library["prices"] == {"6666": {"2026-10-02": 81.0}}  # 價格先收（從 t 起）
    assert library["needed"] == {"0050": "2026-10-02", "6666": "2026-10-02"}


def test_pinned_group_is_not_replaced_by_a_later_rev(world):  # P3 #2（反例 S2b）
    world.tw(last="2026-10-14")
    world.closes("tw", "2301", {d: (100.0 if d <= "2026-10-02" else 101.0) for d in tw_sessions()})
    write_jsonl(stock_picks.AI_HISTORY_NAME, [ai_line("2026-10-01", ("2301", 100.0))])
    run(world, now=_ts("2026-10-14"), ai=True)
    assert [(r["rev"], r["code"], r["h"]) for r in ledger_rows()] == [(1, "2301", 5)]
    # 之後才多出 rev 2（開盤前算出來的，本來會被選）：帳裡已經有 10/01 → 釘住 rev 1，h=20 也照 rev 1 算
    write_jsonl(stock_picks.AI_HISTORY_NAME, [
        ai_line("2026-10-01", ("2301", 100.0)),
        ai_line("2026-10-01", ("6666", 50.0), rev=2, computed_at=_ts("2026-10-01", 18)),
    ])
    world.tw()
    tw = run(world, ai=True)["markets"]["tw"]
    assert [(r["rev"], r["code"], r["h"]) for r in ledger_rows()] == [(1, "2301", 5), (1, "2301", 20)]
    assert (tw["from"], tw["h"]["5"]["r_pm"], tw["h"]["20"]["r_pm"], tw["h"]["5"]["skipped"]) == (
        "2026-10-01", 10, 10, 0)


def test_quarantine_ending_on_t_counts(world):  # P3 #3（反例 S3）：盤勢 job 對 t 那根還有爭議時不改 DB
    t = "2026-10-01"
    world.closes("us", "NVDA", {t: 200.0} | {d: 220.0 for d in us_sessions() if d > t})
    with world.store() as s:
        s.add_quarantine("us", "NVDA", D(t), D(t), "conflict")
    write_jsonl(perf.US_HISTORY_NAME, [us_line(t, ("NVDA", 200.0))])
    world.us(last="2026-10-15")  # T_5（10/09）之後 4 個交易日：到期了，但 t 那根還在隔離 → 等
    run(world, now=_ts("2026-10-16"))
    assert ledger_rows() == []
    world.us()  # 過了 10 個交易日還沒解除 → 凍結成跳過
    run(world)
    assert {(r["h"], r["skip"]) for r in ledger_rows()} == {(5, "quarantine"), (20, "quarantine")}


def test_price_collection_survives_an_unreadable_history_file(world):  # P3 #5（反例 S6）
    world.tw(last="2026-10-02")
    world.closes("tw_all", "6666", {"2026-10-01": 80.0, "2026-10-02": 81.0})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("6666", 80.0))])
    run(world, now=_ts("2026-10-02"))
    ai_path = stock_picks.STATE_DIR / stock_picks.AI_HISTORY_NAME
    ai_path.mkdir()  # AI 檔 10/05–10/20 讀不了
    days = world.tw(last="2026-10-20")
    for day in [d for d in days if "2026-10-05" <= d <= "2026-10-20"]:
        world.closes("tw_all", "6666", {day: 84.0})
        with world.store() as s:
            s.prune("tw_all", 8)  # tw_all 只留最近幾天
        run(world, now=_ts(day))
    assert ledger_rows() == []  # 讀不了的這段時間不凍結
    prices = read_library()["prices"]["6666"]
    assert len(prices) == 13 and min(prices) == "2026-10-01" and max(prices) == "2026-10-20"  # 照上一輪的 needed 收
    ai_path.rmdir()
    run(world, now=_ts("2026-10-20"))
    row = next(r for r in ledger_rows() if r["h"] == 5)
    assert (row["skip"], row["r"]) == (None, pytest.approx(84 / 81 - 1, abs=1e-6))  # E＝10/02 的 81


def test_price_collection_survives_a_failing_query(world):  # P3 #5：休市表 JSON 壞掉（bad_json）也照收
    world.tw(last="2026-10-02")
    world.closes("tw_all", "6666", {"2026-10-01": 80.0, "2026-10-02": 81.0})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("6666", 80.0))])
    run(world, now=_ts("2026-10-02"))
    days = world.tw(last="2026-10-14")
    world.closes("tw_all", "6666", {d: 84.0 for d in days if d >= "2026-10-05"})
    with sqlite3.connect(world.path) as con:
        con.execute("UPDATE obs SET json = '{broken' WHERE obs_id = 'tw.holidays.2026'")
    con.close()
    result = run(world, now=_ts("2026-10-14"))
    assert result is not None and ledger_rows() == []  # 算成績要的查詢壞了：這一輪不凍結
    assert "2026-10-14" in read_library()["prices"]["6666"]  # 價格照樣收


def test_waits_have_an_exit_after_10_sessions_and_the_calendar_after_40(world):  # P3 #10；impl r3 #1
    def extend(last: str) -> None:
        days = world.tw(last=last, typhoon_kind="revoked", typhoon_confirmed=False, coverage_to="2026-10-14")
        world.closes("tw", "2330", {d: 100.0 for d in days})

    extend("2026-11-30")
    write_jsonl(stock_picks.HISTORY_NAME,
                [old_line("2026-10-07", ("2330", 100.0)), old_line("2026-10-16", ("2330", 100.0))])
    result = run(world)
    got = {(r["t"], r["h"]): r["skip"] for r in ledger_rows()}
    assert got == {("2026-10-07", 5): "exrights"}  # 除權息一直沒涵蓋過 T_5（10/16）：過了 10 個交易日就凍結
    # 10/23 撤銷了臨時休市、TAIEX 又沒有那天：日曆一直不確定。日曆要等 40 個交易日（10/07 的 T_20＝11/10 之後才 14 個）
    assert [result["markets"]["tw"]["h"][h]["pending"] for h in ("5", "20")] == [1, 2]
    extend("2027-01-12")
    run(world, now=_ts("2027-01-12"))
    got = {(r["t"], r["h"]): r["skip"] for r in ledger_rows()}
    assert got == {
        ("2026-10-07", 5): "exrights",
        ("2026-10-07", 20): "calendar",  # T_20＝11/10 之後 45 個交易日
        ("2026-10-16", 5): "calendar",  # T_5＝10/28 之後 54 個
    }  # 10/16 的 T_20＝11/18 之後才 39 個：還在等
    extend("2027-01-13")
    run(world, now=_ts("2027-01-13"))
    assert {(r["t"], r["h"]): r["skip"] for r in ledger_rows()}[("2026-10-16", 20)] == "calendar"  # 第 40 個


def test_calendar_waits_40_sessions_so_a_late_holiday_table_still_counts():  # impl r3 #1：跨年要用下一年的休市表
    days = _weekdays("2026-11-02", "2027-04-30", ("2027-01-01",))  # 元旦休市
    t = "2026-12-24"
    th = days[days.index(t) + 6]  # E＝12/25，T_5 跨過元旦
    assert th == "2027-01-04"

    def at(after: int, table, coverage_to: str = "2099-12-31") -> tuple:
        """日曆到 T_5 之後第 after 個交易日；table＝2027 的休市表（None：盤勢 job 還沒取得）。"""
        cal = tuple(days[:days.index(th) + after + 1])
        data = perf.MarketData("tw", cal, {"2330": {d: 100.0 for d in cal}, "0050": {d: 50.0 for d in cal}}, {},
                               closed_by_year={2026: frozenset(), 2027: table},
                               coverage=(("2020-01-01", coverage_to),))
        return perf.evaluate(perf.Candidate("tw", "ai_v1", t, 1, "2330", 100.0, 5), data)

    assert at(perf.SETTLE_SESSIONS, None) == (perf.WAIT, "calendar")  # 以前在這裡（約 1/18）就永久凍成 calendar
    assert at(perf.CALENDAR_SETTLE_SESSIONS - 1, None) == (perf.WAIT, "calendar")
    kind, row = at(perf.CALENDAR_SETTLE_SESSIONS, None)
    assert (kind, row["skip"], row["r"]) == (perf.FREEZE, "calendar", None)  # 等也有出口
    kind, row = at(perf.CALENDAR_SETTLE_SESSIONS - 1, frozenset({"2027-01-01"}))  # 休市表最後一刻才到也照算
    assert (kind, row["skip"], row["th"], row["r"]) == (perf.FREEZE, None, th, 0.0)
    # 其他原因照舊 10 個交易日：除權息只涵蓋到 T_5 當天（沒有過 T_5）
    assert at(perf.SETTLE_SESSIONS - 1, frozenset({"2027-01-01"}), th) == (perf.WAIT, "exrights")
    kind, row = at(perf.SETTLE_SESSIONS, frozenset({"2027-01-01"}), th)
    assert (kind, row["skip"]) == (perf.FREEZE, "exrights")
    assert (perf.SETTLE_SESSIONS, perf.CALENDAR_SETTLE_SESSIONS) == (10, 40)


def test_old_ledger_rows_without_inputs_still_load():  # P3 #6
    row = perf._ledger_row(_row("2026-10-01", 5, 0.1, 0.0))
    assert row is not None and all(row[k] is None for k in perf.INPUT_KEYS)
    assert perf._ledger_row(_row("2026-10-01", 5, 0.1, 0.0) | {"c0": "x"}) is None  # 輸入欄位壞了：整行不算


def test_ai_note_stays_true_after_a_rollback():  # P3 #7：AI 之後又回到舊規則
    rows = [_row("2026-10-12", 5, 0.01, 0.0, rule="ai_v1", code="2454"), _row("2026-10-20", 5, 0.02, 0.0)]
    texts = card_texts(perf.perf_bubble(_aggregate(rows)))
    assert "10/12 起含 AI 相關股推薦" in texts and not any("改成" in t for t in texts)


def test_n_boundary_4_and_5():  # P4
    days = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"]
    four = card_texts(perf.perf_bubble(_aggregate([_row(t, 5, 0.01, 0.0) for t in days[:4]])))
    five = card_texts(perf.perf_bubble(_aggregate([_row(t, 5, 0.01, 0.0) for t in days])))
    assert "推薦後 5 個交易日：紀錄還不夠（目前 4 次）" in four and not any("平均" in t for t in four)
    assert "推薦後 5 個交易日：平均漲 1.0%，0050 同期持平" in five and "5 次推薦裡有 5 次比 0050 好" in five


def test_reads_a_us_history_row_in_the_exact_stock_picks_us_shape(world):  # P4：讀的這一邊的契約
    days = world.us()
    world.closes("us", "BRK.B", {d: 480.25 if d == "2026-10-01" else 485.0 if d == "2026-10-02" else 490.0
                                 for d in days})
    line = {"t": "2026-10-01", "market": "neutral",  # stock_picks_us._history_record 寫的欄位，一個不多一個不少
            "picks": [{"code": "BRK.B", "name": "BERKSHIRE HATHAWAY", "close": 480.25, "score": 4, "p60": 12}],
            "computed_at": round(_ts("2026-10-02", 14)), "rule": "us_v1"}
    write_jsonl(perf.US_HISTORY_NAME, [line])
    rec = perf.parse_records([line], market=perf.US, default_rule=perf.US_RULE, today="2026-10-28")["2026-10-01"]
    assert (rec.rule, rec.rev, [(p.code, p.close) for p in rec.picks]) == ("us_v1", 1, [("BRK.B", 480.25)])
    run(world)
    rows = {r["h"]: r for r in ledger_rows()}
    assert set(rows) == {5, 20}
    assert (rows[5]["m"], rows[5]["rule"], rows[5]["rev"], rows[5]["code"]) == ("us", "us_v1", 1, "BRK.B")
    assert (rows[5]["c0"], rows[5]["ce"], rows[5]["ch"], rows[5]["cf"], rows[5]["be"]) == (480.25, 485.0, 490.0, 1.0,
                                                                                          600.0)
    assert rows[5]["skip"] is None and rows[5]["r"] == pytest.approx(490 / 485 - 1, abs=1e-6)


# ── 公開介面的契約（回測週報的 job 用；PLAN 2026-10-29 §5.1：改名、不改行為） ─────────────
def _tw_market(days: list[str], stock: dict, bench: dict, *, code: str = "2330", exrights=None) -> perf.MarketData:
    """直接組 evaluate 吃的台股 MarketData（不經 DB）：days 都是交易日、沒有休市、除權息涵蓋整段。"""
    years = {int(d[:4]) for d in days}
    return perf.MarketData("tw", tuple(days), {code: stock, "0050": bench}, {},
                           closed_by_year={y: frozenset() for y in years},
                           coverage=(("2020-01-01", "2099-12-31"),), exrights=exrights or {})


def test_contract_candidate_and_market_data_fields():
    assert [f.name for f in dataclasses.fields(perf.Candidate)] == ["market", "rule", "t", "rev", "code", "close", "h"]
    assert [f.name for f in dataclasses.fields(perf.MarketData)] == [
        "market", "calendar", "prices", "quarantine", "closed_by_year", "coverage", "exrights", "splits"]
    cand = perf.Candidate("tw", "ai_v1", "2026-10-01", 1, "2330", 100.0, 5)
    assert cand.key == (perf.LEDGER_VERSION, "tw", "ai_v1", "2026-10-01", 1, "2330", 5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cand.close = 101.0  # type: ignore[misc]
    data = perf.MarketData("us", ("2026-10-01",), {}, {})
    assert (data.closed_by_year, data.coverage, data.exrights, data.splits) == ({}, (), {}, {})
    assert data.sessions == frozenset({"2026-10-01"})


def test_contract_loader_and_evaluate_signatures():
    assert list(inspect.signature(perf.load_tw).parameters) == ["cands", "calendar", "prices", "today", "db_path"]
    us = inspect.signature(perf.load_us).parameters
    assert list(us) == ["cands", "calendar", "db_path", "prices"] and us["prices"].default is None
    assert list(inspect.signature(perf.evaluate).parameters) == ["c", "data"]
    assert not inspect.signature(perf.read_ledger_rows).parameters
    assert not hasattr(perf, "_load_tw") and not hasattr(perf, "_load_us")  # 只有公開的名字（job 不靠私有函式）
    assert (perf.WAIT, perf.FREEZE) == ("wait", "freeze")


def test_contract_evaluate_returns_wait_reason_or_a_ledger_row():
    days = _weekdays("2026-09-01", "2026-10-30")
    data = _tw_market(days, {d: 100.0 for d in days}, {d: 50.0 for d in days})
    kind, row = perf.evaluate(perf.Candidate("tw", "v1", days[0], 1, "2330", 100.0, 5), data)
    assert kind == perf.FREEZE and isinstance(row, dict)
    assert set(row) == {"v", "m", "rule", "t", "rev", "code", "h", "th", "r", "b", "skip", *perf.INPUT_KEYS}
    assert (row["v"], row["m"], row["rule"], row["t"], row["rev"], row["code"], row["h"]) == (
        perf.LEDGER_VERSION, "tw", "v1", days[0], 1, "2330", 5)
    assert row["skip"] is None and isinstance(row["r"], float) and isinstance(row["b"], float)
    assert perf._ledger_row(row) is not None  # 寫進帳的列讀得回來
    assert perf.evaluate(perf.Candidate("tw", "v1", days[-3], 1, "2330", 100.0, 5), data) == (perf.WAIT, "not_due")


def test_contract_load_us_uses_given_prices_or_the_db(world, monkeypatch):
    days = world.us(last="2026-10-30")
    world.closes("us", "NVDA", {d: 200.0 for d in days})
    with world.store() as s:
        s.upsert_splits([("NVDA", D("2026-10-06"), 4.0, 1.0)], "nasdaq")
    cands = [perf.Candidate("us", "us_v1", "2026-10-01", 1, "NVDA", 200.0, 5)]
    from_db = perf.load_us(cands, tuple(days), world.path)
    assert from_db.prices["NVDA"]["2026-10-01"] == 200.0 and from_db.prices["SPY"]["2026-10-01"] == 600.0
    assert from_db.splits == {"NVDA": ["2026-10-06"]}

    def no_closes(*_a, **_k):
        raise AssertionError("prices were given: do not query DB closes")

    monkeypatch.setattr(stock_db_ro, "closes", no_closes)
    given = {"NVDA": {"2026-10-01": 210.0, "bad": 1.0}, "SPY": {"2026-10-01": 610.0, "2026-10-02": float("nan")}}
    mine = perf.load_us(cands, tuple(days), world.path, prices=given)
    assert mine.prices == {"NVDA": {"2026-10-01": 210.0}, "SPY": {"2026-10-01": 610.0}}  # 一樣只收合法的
    assert mine.splits == {"NVDA": ["2026-10-06"]} and mine.calendar == tuple(days)


def test_read_ledger_rows_is_what_aggregate_reads(monkeypatch):
    assert perf.read_ledger_rows() == []  # 還沒有帳
    first, again = _row("2026-10-01", 5, 0.1, 0.0), _row("2026-10-01", 5, 0.5, 0.0)
    other = _row("2026-10-02", 20, None, None, skip="missing", m="us", rule="us_v1", code="NVDA")
    write_jsonl(perf.LEDGER_NAME, [first, again, {"v": 1, "m": "tw", "skip": "bogus"}, other],
                raw_tail='{"v":1,"m":"tw","rule"')

    def no_lock(*_a, **_k):
        raise AssertionError("reading the ledger must not take the state lock")

    monkeypatch.setattr(stock_picks, "_state_file_lock", no_lock)
    rows = perf.read_ledger_rows()
    assert rows == perf._read_ledger(stock_picks.STATE_DIR / perf.LEDGER_NAME).rows
    assert [(r["t"], r["h"], r["r"], r["skip"]) for r in rows] == [("2026-10-01", 5, 0.1, None),
                                                                 ("2026-10-02", 20, None, "missing")]
    rows[0]["r"] = 9.9
    assert perf.read_ledger_rows()[0]["r"] == 0.1  # 每次都是新的 dict
    (stock_picks.STATE_DIR / perf.LEDGER_NAME).unlink()
    (stock_picks.STATE_DIR / perf.LEDGER_NAME).mkdir()
    with pytest.raises((OSError, ValueError)):  # 讀不了不是「空的帳」
        perf.read_ledger_rows()


# ── v2：隔天收盤進場、5／20／60／240 日（回測 PLAN 2026-10-29 §5.1 commit 2） ─────────────────
LONG_DAYS = _weekdays("2026-01-05", "2027-06-30")  # 合成的長日曆：每個平日都是交易日


def _grow(days: list[str], start: int, base: float, daily: float) -> dict:
    """days[start] 起每天乘 daily（之前都是 base）：每天的漲跌都在 10% 以內，不會被 11% 防護擋掉。"""
    return {d: base * daily ** max(0, i - start) for i, d in enumerate(days)}


def test_v2_entry_is_the_next_session_close_and_t_h_counts_from_e():
    days = LONG_DAYS
    t, e = days[10], days[11]
    stock = _grow(days, 11, 100.0, 1.001)
    stock[t] = 90.0  # t → E 漲 11.1%：不在報酬區間裡，不算公司行動
    data = _tw_market(days, stock, {d: 50.0 for d in days})
    for h in perf.HORIZONS:
        kind, row = perf.evaluate(perf.Candidate("tw", "ai_v1", t, 1, "2330", 90.0, h), data)
        assert kind == perf.FREEZE and row["skip"] is None, h
        assert row["th"] == days[11 + h]  # T_h＝E 之後第 h 個交易日
        assert row["r"] == round(1.001 ** h - 1, 6) and row["b"] == 0.0
        assert (row["c0"], row["ce"], row["be"], row["cf"], row["bf"]) == (90.0, 100.0, 50.0, 1.0, 1.0)
        assert row["ch"] == pytest.approx(100.0 * 1.001 ** h)
    assert perf.evaluate(perf.Candidate("tw", "ai_v1", e, 1, "2330", 100.0, 5), data)[1]["th"] == days[17]
    mismatch = perf.evaluate(perf.Candidate("tw", "ai_v1", t, 1, "2330", 100.0, 5), data)[1]
    assert mismatch["skip"] == "mismatch" and mismatch["c0"] == 90.0  # 照舊拿 t 的收盤核對家人看到的價格


def test_v2_exrights_only_inside_e_to_t_h():
    days = ["2026-10-06", "2026-10-07", "2026-10-08", *_weekdays("2026-10-12", "2026-10-30")]  # 10/09 休市
    stock = {d: 50.0 for d in days if d <= "2026-10-08"} | {"2026-10-12": 48.0} | {
        d: 47.0 for d in days if "2026-10-13" <= d <= "2026-10-19"} | {d: 40.0 for d in days if d >= "2026-10-20"}
    events = {"2330": [("2026-10-09", 50.0, 49.0),  # 休市日：t＝10/07 時在 (E, T_5] 裡（算在 10/12）；t＝10/08 時在 t 和 E 之間
                       ("2026-10-12", 49.0, 48.0),  # t＝10/08 的 E 當天：E 的收盤已經除息，不算
                       ("2026-10-13", 48.0, 47.0),  # 兩個 t 都在區間裡
                       ("2026-10-20", 47.0, 40.0)]}  # 兩個 T_5 之後：不算
    data = perf.MarketData("tw", tuple(days), {"2330": stock, "0050": {d: 100.0 for d in days}}, {},
                           closed_by_year={2026: frozenset({"2026-10-09"})},
                           coverage=(("2020-01-01", "2099-12-31"),), exrights=events)
    _, a = perf.evaluate(perf.Candidate("tw", "v1", "2026-10-07", 1, "2330", 50.0, 5), data)
    assert (a["th"], a["ce"], a["ch"]) == ("2026-10-16", 50.0, 47.0)
    assert a["cf"] == pytest.approx(50 / 47) and a["r"] == pytest.approx(0.0, abs=1e-6)
    _, b = perf.evaluate(perf.Candidate("tw", "v1", "2026-10-08", 1, "2330", 50.0, 5), data)
    assert (b["th"], b["ce"], b["ch"]) == ("2026-10-19", 48.0, 47.0)
    assert b["cf"] == pytest.approx(48 / 47) and b["r"] == pytest.approx(0.0, abs=1e-6)  # 算進 10/09、10/12 會變 +4.2%


def test_v2_us_split_on_e_is_fine_but_inside_the_window_skips():
    days = _weekdays("2026-09-01", "2026-11-30")
    t, spy = "2026-10-01", {d: 600.0 for d in days}

    def row(split_day):
        stock = {d: (400.0 if d < split_day else 100.0) for d in days}  # 一拆四，DB 還沒整段調整
        data = perf.MarketData("us", tuple(days), {"NVDA": stock, "SPY": spy}, {}, splits={"NVDA": [split_day]})
        return perf.evaluate(perf.Candidate("us", "us_v1", t, 1, "NVDA", 400.0, 5), data)[1]

    on_e = row("2026-10-02")  # E 當天分割：E、T_h 都是分割後的價格，報酬照算（t 的收盤不比）
    assert (on_e["skip"], on_e["r"], on_e["c0"], on_e["ce"]) == (None, 0.0, 400.0, 100.0)
    assert row("2026-10-05")["skip"] == "split"  # (E, T_h] 裡：跳過


@pytest.mark.parametrize("market", ["tw", "us"])
def test_v2_t_must_be_on_the_calendar(market):
    bench = "0050" if market == "tw" else "SPY"
    days = _weekdays("2026-03-02", "2026-12-31")
    prices = {"2330": {d: 100.0 for d in ["2026-02-27", *days]}, bench: {d: 50.0 for d in ["2026-02-27", *days]}}

    def data(calendar):
        return perf.MarketData(market, tuple(calendar), prices, {}, closed_by_year={2026: frozenset()},
                               coverage=(("2020-01-01", "2099-12-31"),))

    early = perf.Candidate(market, "x_v1", "2026-02-27", 1, "2330", 100.0, 5)  # 比日曆的起點早（bisect 回 0）
    _, row = perf.evaluate(early, data(days))
    assert row["skip"] == "calendar" and row["r"] is None  # 不默默把 E 當成日曆的第一天
    hole = [d for d in days if d != "2026-06-10"]  # t 那天日曆有洞
    _, row = perf.evaluate(perf.Candidate(market, "x_v1", "2026-06-10", 1, "2330", 100.0, 5), data(hole))
    assert row["skip"] == "calendar"
    recent = [d for d in days if d <= "2026-03-20" and d != "2026-03-02"]  # 還沒過 40 個交易日（日曆的出口）：先等
    assert perf.evaluate(perf.Candidate(market, "x_v1", "2026-03-02", 1, "2330", 100.0, 5), data(recent)) == (
        perf.WAIT, "calendar")


def test_v2_range_depends_on_h_and_keeps_real_long_run_winners():
    days = LONG_DAYS
    t = days[10]
    flat = {d: 50.0 for d in days}

    def tw(daily, h):
        stock = _grow(days, 11, 100.0, daily)
        return perf.evaluate(perf.Candidate("tw", "ai_v1", t, 1, "2408", 100.0, h),
                             _tw_market(days, stock, flat, code="2408"))[1]

    assert tw(4.5 ** (1 / 20), 20)["skip"] == "range"  # 20 日 +350%：家人卡片的天數照舊當資料錯
    assert tw(4.5 ** (1 / 60), 60)["r"] == pytest.approx(3.5, abs=1e-5)  # 60 日 +350%：算數（實測有 +368%）
    winner = tw(11.64 ** (1 / 240), 240)  # 2408 的真實量級：240 日 +1064%，每天都在 ±10% 內
    assert winner["skip"] is None and winner["r"] == pytest.approx(10.64, abs=1e-5)
    assert tw(51.0 ** (1 / 240), 240)["skip"] == "range"  # +5000%：還是當資料錯
    spy = {d: 600.0 for d in days}

    def us(drop, h):
        stock = {d: (100.0 if i <= 11 else 100.0 * (1 - drop)) for i, d in enumerate(days)}
        data = perf.MarketData("us", tuple(days), {"NVDA": stock, "SPY": spy}, {})
        return perf.evaluate(perf.Candidate("us", "us_v1", t, 1, "NVDA", 100.0, h), data)[1]

    assert us(0.95, 5)["skip"] == "range" and us(0.95, 20)["skip"] == "range"  # ≤ −90%
    assert us(0.95, 60)["r"] == pytest.approx(-0.95) and us(0.95, 240)["skip"] is None  # 長天數到 −99% 才算錯
    assert us(0.995, 240)["skip"] == "range"
    assert perf.r_range(5) == perf.r_range(20) == (-0.9, 3.0)
    assert perf.r_range(60) == perf.r_range(240) == (-0.99, 49.0)


def test_close_deadline_is_e_close_1330_taipei_and_1300_new_york_with_dst():
    tw_cal = ["2026-10-01", "2026-10-02", "2026-10-05"]
    assert perf.close_deadline("tw", "2026-10-01", tw_cal) == datetime(2026, 10, 2, 13, 30, tzinfo=TPE).timestamp()
    assert perf.close_deadline("tw", "2026-10-02", tw_cal) == datetime(2026, 10, 5, 13, 30, tzinfo=TPE).timestamp()
    assert perf.close_deadline("tw", "2026-10-05", tw_cal) is None  # E 還不在日曆上：先等
    utc = timezone.utc
    cases = [("2026-10-01", "2026-10-02", datetime(2026, 10, 2, 17, 0, tzinfo=utc)),  # EDT：13:00＝17:00 UTC
             ("2026-10-30", "2026-11-02", datetime(2026, 11, 2, 18, 0, tzinfo=utc)),  # 11/01 夏令結束：EST＝18:00 UTC
             ("2026-03-06", "2026-03-09", datetime(2026, 3, 9, 17, 0, tzinfo=utc)),  # 03/08 夏令開始
             ("2026-11-25", "2026-11-27", datetime(2026, 11, 27, 18, 0, tzinfo=utc))]  # 感恩節隔天半日市：一樣 13:00
    for t, e, want in cases:
        assert perf.close_deadline("us", t, [t, e]) == want.timestamp(), t
    assert not hasattr(perf, "open_after") and not hasattr(perf, "MARKET_OPEN")  # 只有一個期限函式


def test_v2_ignores_v1_rows_and_v1_pins(world):
    days = world.tw()
    for code in ("2301", "2302"):
        world.closes("tw", code, {d: 100.0 for d in days})
    write_jsonl(stock_picks.AI_HISTORY_NAME, [
        ai_line("2026-10-01", ("2301", 100.0)),
        ai_line("2026-10-01", ("2302", 100.0), rev=2, computed_at=_ts("2026-10-02", 10)),  # E 開盤後、收盤前
    ])
    write_jsonl(perf.LEDGER_NAME, [_row("2026-10-01", 5, 0.3, 0.0, rule="ai_v1", code="2301", v=1),  # v1 釘住 rev 1
                                   _row("2026-10-01", 20, 0.3, 0.0, rule="ai_v1", code="2301", v=1)])
    result = run(world, ai=True)
    fresh = [r for r in ledger_rows() if r["v"] == 2]
    assert {(r["rev"], r["code"], r["h"]) for r in fresh} == {(2, "2302", 5), (2, "2302", 20)}
    h5 = result["markets"]["tw"]["h"]["5"]
    assert (h5["n"], h5["r_pm"]) == (1, 0)  # v1 的 +30% 不算進 v2 的成績（留在帳裡備查）
    assert len(perf.read_ledger_rows()) == 4


def test_us_v1_and_us_v2_rules_both_count(world):
    days = world.us()
    world.closes("us", "NVDA", {d: 100.0 for d in days})
    write_jsonl(perf.US_HISTORY_NAME, [us_line("2026-10-01", ("NVDA", 100.0)),
                                       us_line("2026-10-02", ("NVDA", 100.0)) | {"rule": "us_v2"}])
    result = run(world)
    assert {(r["t"], r["rule"]) for r in ledger_rows() if r["h"] == 5} == {("2026-10-01", "us_v1"),
                                                                             ("2026-10-02", "us_v2")}
    assert result["markets"]["us"]["h"]["5"]["n"] == 2


# ── v2 價格庫（獨立的檔）、共用狀態檔、回補 ─────────────────────────────────────────────
def _v1_state(prices=None, needed=None, flags=None) -> dict:
    payload = {"schema": 1, "prices": prices or {}, "needed": needed or {}, "ai_flag_by_t": flags or {},
               "ai_first_t": None}
    (stock_picks.STATE_DIR / perf.STATE_NAME).write_text(json.dumps(payload), encoding="utf-8")
    return payload


def test_v2_library_is_its_own_file_and_survives_a_v1_style_rebuild(world):
    world.tw(last="2026-10-02")
    world.closes("tw_all", "6666", {"2026-10-01": 80.0, "2026-10-02": 81.0})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("6666", 80.0))])
    v1 = _v1_state(prices={"1234": {"2026-09-30": 10.0}}, needed={"1234": "2026-09-30"})
    run(world, now=_ts("2026-10-02"))
    assert read_library()["prices"] == {"6666": {"2026-10-01": 80.0, "2026-10-02": 81.0}}
    state = read_state()
    assert set(state) == {"schema", "prices", "needed", "ai_flag_by_t", "ai_first_t"} and state["schema"] == 1
    assert (state["prices"], state["needed"]) == (v1["prices"], v1["needed"])  # v1 的價格庫原樣保留
    assert state["ai_flag_by_t"] == {"2026-10-01": False}  # 共用的欄位照樣記
    assert perf._state_from_json(state).flags == {"2026-10-01": False}  # v1 的讀法讀得懂
    # 回滾或新舊程序重疊：v1 照自己的 needed 重建狀態檔裡的價格庫（沒有 6666），不碰 v2 的檔
    _v1_state(prices={}, needed={}, flags={"2026-10-01": False})
    days = world.tw(last="2026-10-30")
    world.closes("tw_all", "6666", {d: 84.0 for d in days if d >= "2026-10-05"})
    with world.store() as s:
        s.prune("tw_all", 17)  # 10/01、10/02 滾掉了
    run(world, now=_ts("2026-10-30"))
    [row] = ledger_rows()
    assert (row["h"], row["skip"], row["c0"], row["ce"]) == (5, None, 80.0, 81.0)
    assert row["r"] == pytest.approx(84 / 81 - 1, abs=1e-6)
    assert read_state()["prices"] == {}  # v2 沒有把自己的價格寫進共用狀態檔
    assert read_library()["prices"]["6666"]["2026-10-01"] == 80.0  # 60、240 日還在等：留著
    assert stat_mode(stock_picks.STATE_DIR / perf.PRICES_NAME) == 0o600


def test_v1_price_library_seeds_v2(world):
    days = world.tw(last="2026-10-30")
    world.closes("tw_all", "6666", {d: 84.0 for d in days if d >= "2026-10-05"})  # 10/01、10/02 已經滾掉
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("6666", 80.0))])
    _v1_state(prices={"6666": {"2026-10-01": 80.0, "2026-10-02": 81.0}}, needed={"6666": "2026-10-01"})
    run(world, now=_ts("2026-10-30"))
    row = next(r for r in ledger_rows() if r["h"] == 5)
    assert row["skip"] is None and row["r"] == pytest.approx(84 / 81 - 1, abs=1e-6)  # v1 收過的價格照樣用
    assert read_library()["prices"]["6666"]["2026-10-02"] == 81.0  # 也搬進 v2 自己的價格庫
    assert read_state()["prices"] == {"6666": {"2026-10-01": 80.0, "2026-10-02": 81.0}}  # v1 的不動


def test_library_too_large_keeps_the_ledger_and_result_going(world, monkeypatch, caplog):
    days = world.tw()
    world.closes("tw", "2330", {d: 100.0 for d in days})
    world.closes("tw_all", "6666", {d: 80.0 for d in days if d >= "2026-11-23"})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("2330", 100.0)),
                                           old_line("2026-11-24", ("6666", 80.0))])  # 還沒到期：價格要進價格庫
    real = stock_picks._write_json_file

    def too_large_for_the_library(path, payload):
        if path.name == perf.PRICES_NAME:
            raise ValueError("state payload too large")  # stock_picks._write_json_file 超過 2 MiB 的那個例外
        return real(path, payload)

    monkeypatch.setattr(stock_picks, "_write_json_file", too_large_for_the_library)
    result = run(world)
    assert result is not None and perf.load_result() == result  # 結果檔照寫
    assert {(r["t"], r["h"]) for r in ledger_rows()} == {("2026-10-01", 5), ("2026-10-01", 20)}
    assert not (stock_picks.STATE_DIR / perf.PRICES_NAME).exists()
    assert "price library too large" in caplog.text
    assert result["library_full"] is True and perf.summary()["library_full"] is True  # 每日維護看得到（impl r1 #6）
    _review_db([("tw", "6666", "2026-11-24", 80.0)])
    got = perf.backfill_prices(now=NOW, db_path=world.path)  # 部署時的回補也寫不進去：不假報 added
    assert (got["status"], got["added"]) == ("library_full", 0)
    monkeypatch.setattr(stock_picks, "_write_json_file", real)  # 寫得進去了：下一輪旗標就清掉
    result = run(world)
    assert result["library_full"] is False and perf.summary()["library_full"] is False
    assert read_library()["prices"]["6666"]["2026-11-24"] == 80.0


def test_library_worst_case_size_fits_and_codes_wait_for_the_240_day_row():
    dates = _weekdays("2026-01-05", "2027-03-31")[:280]
    library = perf._Library({f"{1100 + i}": {d: 12345.67 for d in dates} for i in range(150)},
                            {f"{1100 + i}": dates[0] for i in range(150)})
    data = json.dumps(perf._library_to_json(library), ensure_ascii=False, separators=(",", ":")).encode()
    assert len(data) < stock_picks.STATE_MAX_BYTES / 2  # 150 檔 × 280 天還不到上限的一半（實際只有幾十檔）
    only_240 = [perf.Candidate("tw", "ai_v1", "2026-10-01", 1, "6666", 80.0, 240)]
    assert perf.needed_prices(only_240, {}) == {"6666": "2026-10-01", "0050": "2026-10-01"}  # 4 個天數都凍結才放掉


def _review_db(rows) -> Path:
    """回測 job 自己建的 review.sqlite（WAL）：表用 job 的 open_db 建、不手寫 CREATE TABLE，perf 查的欄位和 job 的
    schema 對不上時這裡就會失敗（impl r1 #3、r2 #4）。"""
    rdb = importlib.import_module("jobs.stock_picks_review_db")
    path = stock_picks.STATE_DIR / perf.REVIEW_DB
    path.parent.mkdir(mode=0o700, exist_ok=True)
    con = rdb.open_db(path)
    try:
        with rdb.transaction(con):
            con.executemany("INSERT INTO prices (m, code, d, close, src, seen_at) VALUES (?, ?, ?, ?, 'tw_all', 0)",
                            rows)
    finally:
        con.close()
    return path


def test_backfill_fills_rolled_off_closes_from_the_review_db(world):
    days = world.tw(last="2026-10-30")
    world.closes("tw_all", "6666", {d: 84.0 for d in days if d >= "2026-10-16"})  # tw_all 只剩 10/16 之後
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("6666", 80.0))])
    early = [d for d in days if "2026-10-05" <= d <= "2026-10-15"]
    _review_db([("tw", "6666", "2026-10-01", 80.0), ("tw", "6666", "2026-10-02", 81.0)]
               + [("tw", "6666", d, 82.0) for d in early]
               + [("tw", "6666", "2026-09-30", 1.0),  # t 之前：不要
                  ("tw", "9999", "2026-10-01", 5.0), ("us", "6666", "2026-10-01", 7.0)])  # 不需要的代號、別的市場
    write_library(prices={"6666": {"2026-10-05": 82.5}})  # 價格庫已經有的優先
    got = perf.backfill_prices(now=_ts("2026-10-30"), db_path=world.path)
    assert got["status"] == "ok" and got["codes"] == 2 and got["added"] > 0  # 6666＋0050
    prices = read_library()["prices"]
    assert set(prices) == {"6666"} and min(prices["6666"]) == "2026-10-01"
    assert (prices["6666"]["2026-10-01"], prices["6666"]["2026-10-05"]) == (80.0, 82.5)
    assert not (stock_picks.STATE_DIR / perf.STATE_NAME).exists()  # 不記 ai_flag_by_t、不寫共用狀態檔
    assert ledger_rows() == []  # 只補價格，不凍結
    run(world, now=_ts("2026-10-30"))
    row = next(r for r in ledger_rows() if r["h"] == 5)
    assert row["skip"] is None and row["r"] == pytest.approx(82 / 81 - 1, abs=1e-6)  # 不是 missing


def test_backfill_tolerates_a_missing_or_broken_review_db(world):
    world.tw(last="2026-10-30")
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("6666", 80.0))])
    assert perf.backfill_prices(now=_ts("2026-10-30"), db_path=world.path)["status"] == "no_review_db"
    assert not (stock_picks.STATE_DIR / perf.PRICES_NAME).exists()
    path = stock_picks.STATE_DIR / perf.REVIEW_DB
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_bytes(b"not a sqlite file" * 100)
    assert perf.backfill_prices(now=_ts("2026-10-30"), db_path=world.path)["status"] == "review_unavailable"
    path.unlink()
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE other (x)")  # 沒有 prices 表
    con.close()
    assert perf.backfill_prices(now=_ts("2026-10-30"), db_path=world.path)["status"] == "review_unavailable"
    (stock_picks.STATE_DIR / stock_picks.HISTORY_NAME).unlink()
    (stock_picks.STATE_DIR / stock_picks.HISTORY_NAME).mkdir()  # 紀錄讀不了：判不了要補哪些
    assert perf.backfill_prices(now=_ts("2026-10-30"), db_path=world.path)["status"] == "undecided"


def test_result_schema_2_has_all_horizons_and_v1_results_are_invalid():
    res = _aggregate([_row("2026-10-01", 240, 10.64, 0.2, th="2027-09-20"), _row("2026-10-01", 60, -0.95, 0.1)])
    tw = res["markets"]["tw"]
    assert res["schema"] == 2 and set(tw["h"]) == {"5", "20", "60", "240"}
    assert (tw["h"]["240"]["r_pm"], tw["h"]["60"]["r_pm"]) == (10640, -950)
    assert (tw["from"], tw["to"]) == (None, None)  # 卡上的「起、算到」只看 5、20 日
    assert perf._check_result(res) is res and res["library_full"] is False
    no_flag = {k: v for k, v in res.items() if k != "library_full"}  # 診斷旗標沒有就當 False：卡片照常
    assert perf._check_result(no_flag) is no_flag and perf._checked_or_none(dict(res, library_full="yes")) is None
    v1 = dict(res, schema=1)
    assert perf._checked_or_none(v1) is None
    (stock_picks.STATE_DIR / perf.RESULT_NAME).write_text(json.dumps(v1), encoding="utf-8")
    assert perf.load_result() is None  # 回滾的反方向：v1 的結果檔也當成不合格，下一輪重算


def test_card_shows_only_5_and_20_days_with_the_next_day_close_method():
    assert perf.TEXT_METHOD == "算法：推薦隔天收盤，到之後第 5、20 個交易日的收盤；同一檔連續被推薦會算好幾次"
    days = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"]
    rows = [_row(t, h, 0.03 if h <= 20 else 1.5, 0.01) for t in days for h in perf.HORIZONS]
    res = _aggregate(rows)
    card, alt = perf.perf_bubble(res), perf.perf_alt_text(res)
    texts = card_texts(card)
    assert perf.TEXT_METHOD in texts and stock_card_check.card_ok(card, alt)
    assert "推薦後 5 個交易日：平均漲 3.0%，0050 同期漲 1.0%" in texts
    assert "推薦後 20 個交易日：平均漲 3.0%，0050 同期漲 1.0%" in texts
    assert not any("60 個交易日" in x or "240 個交易日" in x or "150" in x for x in texts + [alt])
    assert alt.startswith("股票推薦：只供參考｜推薦成績｜台股推薦後 5 個交易日平均漲 3.0%")
