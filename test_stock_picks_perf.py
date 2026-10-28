"""stock_picks_perf（推薦成績，2026-10-28）的測試：tmp 的盤勢 DB（盤勢 job 自己的 market_store 建，同 schema）
＋conftest 的 tmp state，不連網。紀錄檔用真實的欄位形狀（line_bot/state/stock_picks_history.jsonl），數字都是編的。"""

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
from datetime import date, datetime, timedelta
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


def old_line(t: str, *picks) -> dict:  # 舊規則紀錄（stock_picks._append_history 的形狀）
    return {"t": t, "market": "neutral",
            "picks": [{"code": c, "name": "測試", "close": v, "score": 4, "p60": 10} for c, v in picks],
            "computed_at": 1791591767}


def ai_line(t: str, *picks, rev=None) -> dict:  # AI 相關股紀錄（計畫 C；picks 欄位和舊檔不一樣）
    line = {"t": t, "rule": "ai_v1", "computed_at": 1791591767, "source": "00952", "core_version": "2026-10",
            "picks": [{"code": c, "name": "測試", "close": v, "ai_source": "00952", "p60": 12} for c, v in picks]}
    if rev is not None:
        line["rev"] = rev
    return line


def us_line(t: str, *picks) -> dict:  # 美股紀錄（PLAN §4 的形狀）
    return {"t": t, "market": None,
            "picks": [{"code": c, "name": "TEST CORP", "close": v, "score": 4, "p60": -18} for c, v in picks],
            "computed_at": 1791591767, "rule": "us_v1"}


def write_state(*, flags=None, ai_first_t=None, prices=None) -> None:
    payload = {"schema": 1, "prices": prices or {}, "ai_flag_by_t": flags or {}, "ai_first_t": ai_first_t}
    (stock_picks.STATE_DIR / perf.STATE_NAME).write_text(json.dumps(payload), encoding="utf-8")


def read_state() -> dict:
    return json.loads((stock_picks.STATE_DIR / perf.STATE_NAME).read_text(encoding="utf-8"))


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


def _row(t, h, r, b, *, m="tw", rule="v1", rev=1, code="2330", skip=None, th="2026-10-08") -> dict:
    return {"v": 1, "m": m, "rule": rule, "t": t, "rev": rev, "code": code, "h": h, "th": th, "r": r, "b": b,
            "skip": skip}


def _aggregate(rows, *, selections=None, first_t=None) -> dict:
    return perf.aggregate(rows, selections or {}, [], first_t or {"tw": None, "us": None}, now=NOW,
                          ledger_rows=len(rows), ledger_full=False)


# ── 台股：報酬、日曆、除權息 ──────────────────────────────────────────────────
def test_tw_return_vs_0050_at_5_and_20_sessions():
    world = World(stock_picks.STATE_DIR / "market_history.sqlite")  # 預設路徑（stock_db_ro.db_path）
    days = world.tw(bench={"2026-10-08": 51.0})
    assert nth_after(days, "2026-10-01", 5) == "2026-10-08"
    assert nth_after(days, "2026-10-01", 20) == "2026-11-03"  # 跨過國慶、颱風、光復節
    ramp = {"2026-10-01": 100.0, "2026-10-02": 101.0, "2026-10-05": 102.0, "2026-10-06": 103.0, "2026-10-07": 104.0}
    world.closes("tw", "2330", {d: 105.0 for d in days if d >= "2026-10-08"} | ramp)
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("2330", 100.0))])
    result = perf.update(ai_enabled=False, now=NOW)
    rows = {r["h"]: r for r in ledger_rows()}
    assert rows[5] == {"v": 1, "m": "tw", "rule": "v1", "t": "2026-10-01", "rev": 1, "code": "2330", "h": 5,
                       "th": "2026-10-08", "r": 0.05, "b": 0.02, "skip": None}
    assert (rows[20]["th"], rows[20]["r"], rows[20]["b"]) == ("2026-11-03", 0.05, 0.0)
    tw = result["markets"]["tw"]
    assert tw["h"]["5"] == {"n": 1, "r_pm": 50, "b_pm": 20, "wins": 1, "skipped": 0, "pending": 0}
    assert (tw["first_t"], tw["from"], tw["to"], tw["ai_from"]) == ("2026-10-01", "2026-10-01", "2026-11-03", None)
    assert perf.load_result() == result
    assert stat_mode(stock_picks.STATE_DIR / perf.LEDGER_NAME) == 0o600


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_exrights_restored_on_next_session_including_closed_day(world):
    world.tw(bench={d: 49.0 for d in tw_sessions() if d >= "2026-10-13"})
    with world.store() as s:
        s.upsert_exrights([
            (D("2026-10-09"), "2886", 50.0, 48.0, "息"),  # 國慶休市那天除息 → 算在下一個交易日 10/12
            (D("2026-10-07"), "1216", 52.0, 50.0, "息"),  # 推薦當天除息：收盤已經是除息後的，不還原
            (D("2026-10-16"), "1216", 50.0, 45.0, "息"),  # T_5 之後：這一筆不算
            (D("2026-10-13"), "0050", 50.0, 49.0, "息"),  # 基準 0050 同樣還原
        ])
    after = ("2026-10-08", "2026-10-12", "2026-10-13", "2026-10-14")
    world.closes("tw", "2886", {"2026-10-07": 50.0, "2026-10-08": 50.0, "2026-10-12": 48.0, "2026-10-13": 48.0,
                                "2026-10-14": 48.0, "2026-10-15": 48.6})
    world.closes("tw", "1216", {"2026-10-07": 50.0, **{d: 50.0 for d in after}, "2026-10-15": 51.0})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-07", ("2886", 50.0), ("1216", 50.0))])
    run(world)
    rows = {(r["code"], r["h"]): r for r in ledger_rows()}
    assert rows[("2886", 5)]["th"] == "2026-10-15"
    assert rows[("2886", 5)]["r"] == pytest.approx(48.6 * (50 / 48) / 50 - 1, abs=1e-6)  # +1.25%（不還原是 −2.8%）
    assert rows[("2886", 5)]["b"] == pytest.approx(0.0, abs=1e-6)  # 0050 除息 50→49，還原後持平
    assert rows[("1216", 5)]["r"] == pytest.approx(0.02, abs=1e-6)
    assert rows[("2886", 20)]["skip"] == "missing"  # 之後沒有價格、而且已經過了 10 個交易日


def test_waits_until_exrights_cover_t_h(world):  # r3 #1：4552 的收盤 14:44 就進 DB，除息 16:02 才進
    world.tw(coverage_to="2026-10-14")
    before = ("2026-10-07", "2026-10-08", "2026-10-12", "2026-10-13", "2026-10-14")
    world.closes("tw", "4552", {d: 15.15 for d in before} | {"2026-10-15": 14.6})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-07", ("4552", 15.15))])
    result = run(world)
    assert ledger_rows() == []  # 除權息只涵蓋到 10/14、T_5 是 10/15：等，不先用「沒有除息」凍結
    assert result["markets"]["tw"]["h"]["5"]["pending"] == 1
    with world.store() as s:
        s.upsert_exrights([(D("2026-10-15"), "4552", 15.15, 14.55, "息")])
        s.add_coverage("twt49u", D("2026-10-15"), D("2026-11-30"))
    run(world)
    row = next(r for r in ledger_rows() if r["h"] == 5)
    assert row["skip"] is None and row["r"] == pytest.approx(14.6 / 14.55 - 1, abs=1e-6)


def test_provisional_typhoon_closure_waits_until_10_days(world):
    days = world.tw(last="2026-10-30", typhoon_confirmed=False)
    assert nth_after(days, "2026-10-16", 5) == "2026-10-27"
    world.closes("tw", "2330", {d: 100.0 for d in days if d >= "2026-10-16"})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-16", ("2330", 100.0))])
    run(world, now=_ts("2026-10-30"))  # 10/23 暫定休市才 7 天：盤勢 job 還可能撤銷 → 等
    run(world, now=_ts("2026-11-02"))  # 第 10 天：還是等
    assert ledger_rows() == []
    run(world, now=_ts("2026-11-03"))  # 超過 10 天才算確定
    assert [(r["h"], r["th"], r["r"]) for r in ledger_rows()] == [(5, "2026-10-27", 0.0)]


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
    days = world.tw()
    assert nth_after(days, "2026-11-02", 5) == "2026-11-09"
    world.closes("tw", "2330", {d: 100.0 for d in days})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-11-02", ("2330", 100.0))])
    with sqlite3.connect(world.path) as con:  # 11/04 的 TAIEX 還沒補進來
        con.execute("DELETE FROM closes WHERE market = 'tw' AND symbol = 'TAIEX' AND date = '2026-11-04'")
    con.close()
    run(world)
    assert ledger_rows() == []  # 不然會把 11/10 當成第 5 個交易日
    world.tw()
    run(world)
    assert [(r["h"], r["th"]) for r in ledger_rows()] == [(5, "2026-11-09")]


def test_missing_holiday_table_waits_only_when_needed(world):
    days = world.tw(holidays=False)
    world.closes("tw", "2330", {d: 100.0 for d in days})
    write_jsonl(stock_picks.HISTORY_NAME,
                [old_line("2026-10-07", ("2330", 100.0)), old_line("2026-10-12", ("2330", 100.0))])
    run(world)
    got = {(r["t"], r["h"]) for r in ledger_rows()}
    assert got == {("2026-10-12", 5)}  # 10/13–10/19 每天都有 TAIEX：用不到休市表
    # 10/07 跨過國慶（10/09）、10/12 的 20 天跨過颱風與光復節：休市表讀不到 → 等


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
    assert read_state()["prices"] == {"6666": {"2026-10-01": 80.0, "2026-10-02": 81.0}}  # 0050 在 tw 裡：不存
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
    assert (row["h"], row["r"], row["skip"]) == (5, pytest.approx(84 / 80 - 1, abs=1e-6), None)
    prices = read_state()["prices"]
    assert set(prices) == {"6666"} and prices["6666"]["2026-10-01"] == 80.0  # h=20 還沒凍結：價格還留著
    assert result["markets"]["tw"]["h"]["20"]["pending"] == 1


def test_results_do_not_change_after_library_and_tw_all_are_gone(world):
    before = _library_scenario(world)["markets"]["tw"]
    ledger_before = (stock_picks.STATE_DIR / perf.LEDGER_NAME).read_bytes()
    (stock_picks.STATE_DIR / perf.STATE_NAME).unlink()
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
    assert read_state()["prices"] == {}  # 都凍結了：價格庫清空


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
    assert result["markets"]["tw"]["h"]["5"]["pending"] == 1
    run(world)
    assert len(ledger_rows()) == 2


def test_unreadable_ai_history_skips_tw_this_round(world):  # r4 #1：AI 檔讀不了，不能當成沒有 AI 紀錄
    days = world.tw()
    world.closes("tw", "2330", {d: 100.0 for d in days})
    write_jsonl(stock_picks.HISTORY_NAME, [old_line("2026-10-01", ("2330", 100.0))])
    (stock_picks.STATE_DIR / stock_picks.AI_HISTORY_NAME).mkdir()  # 不是一般檔：讀不了
    write_state(prices={"6666": {"2026-10-01": 80.0}})
    assert run(world) is not None
    assert ledger_rows() == []
    assert read_state()["prices"] == {"6666": {"2026-10-01": 80.0}}  # 價格庫原樣留著


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
    assert "10/05 起改成 AI 相關股" in card_texts(perf.perf_bubble(result))


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
    assert ledger_rows() == [] and result["ledger_full"] is True and result["pending"] == 2
    assert perf.summary()["ledger_full"] is True


# ── 美股 ─────────────────────────────────────────────────────────────────────
def test_us_returns_splits_quarantine_mismatch_range_and_no_jump_gate(world):
    days = world.us()
    t = "2026-10-01"
    assert (nth_after(days, t, 5), nth_after(days, t, 20)) == ("2026-10-08", "2026-10-29")
    later = [d for d in days if d > t]
    world.closes("us", "NVDA", {t: 200.0} | {d: 130.0 for d in later})  # 隔天跌 35%：真的大跌要算進去
    world.closes("us", "AAPL", {d: 250.0 for d in days})
    world.closes("us", "MSFT", {t: 200.0} | {d: 210.0 for d in later})  # 11/05 一拆二：DB 已經整段調整過
    world.closes("us", "AMZN", {d: 180.0 for d in days})
    world.closes("us", "META", {d: 310.0 for d in days})  # 紀錄是 300：對不上
    world.closes("us", "TSLA", {t: 10.0} | {d: 45.0 for d in later})  # +350%：當資料錯
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
    assert "美股推薦（美國時間 10/01 起，算到 10/29）" in texts
    assert "推薦後 5 個交易日：紀錄還不夠（目前 2 次）" in texts
    assert texts.count("另有 4 次因為資料問題沒有算進去") == 2


def test_us_waits_for_4_sessions_after_t_h_and_for_quarantine(world):
    t = "2026-10-01"  # T_5＝10/08
    world.closes("us", "NVDA", {d: 200.0 for d in us_sessions()})
    world.closes("us", "AMZN", {d: 180.0 for d in us_sessions()})
    with world.store() as s:
        s.add_quarantine("us", "AMZN", D("2026-10-05"), D("2026-10-05"), "jump")
    write_jsonl(perf.US_HISTORY_NAME, [us_line(t, ("NVDA", 200.0), ("AMZN", 180.0))])
    world.us(last="2026-10-13")  # 10/08 之後只有 3 個交易日：盤勢 job 還會改寫 10/08 的收盤
    run(world)
    assert ledger_rows() == []
    world.us(last="2026-10-14")
    run(world)
    assert [(r["code"], r["h"], r["th"]) for r in ledger_rows()] == [("NVDA", 5, "2026-10-08")]  # AMZN 隔離中：等
    with world.store() as s:
        s.clear_quarantine("us", "AMZN")
    run(world)
    assert [(r["code"], r["skip"]) for r in ledger_rows() if r["code"] == "AMZN"] == [("AMZN", None)]


def test_us_waits_until_the_pick_itself_has_the_4th_session(world):
    # 盤勢 job 照字母分批寫：SPY 已經有 10/14，NVDA 那一批還沒跑 → NVDA 的 10/08 還會再被整段重抓改寫一次
    days = world.us(last="2026-10-14")
    world.closes("us", "NVDA", {d: 200.0 for d in days if d <= "2026-10-13"})
    write_jsonl(perf.US_HISTORY_NAME, [us_line("2026-10-01", ("NVDA", 200.0))])
    result = run(world)
    assert ledger_rows() == [] and result["markets"]["us"]["h"]["5"]["pending"] == 1
    world.closes("us", "NVDA", {"2026-10-14": 200.0})
    run(world)
    assert [(r["code"], r["h"], r["th"], r["r"]) for r in ledger_rows()] == [("NVDA", 5, "2026-10-08", 0.0)]


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


def test_aggregate_uses_one_group_per_day():
    a1 = _row("2026-10-05", 5, 0.1, 0.0, rule="ai_v1", rev=1, code="2301")
    a2 = _row("2026-10-05", 5, 0.2, 0.0, rule="ai_v1", rev=2, code="2302")
    old = _row("2026-10-05", 5, 0.3, 0.0, code="1103")
    rows = [a1, old, a2]
    sel = {("tw", "2026-10-05"): perf.Record("2026-10-05", 2, "ai_v1", (perf.Pick("2302", 100.0),))}
    assert _aggregate(rows, selections=sel)["markets"]["tw"]["h"]["5"]["r_pm"] == 200  # 選得出來源：只算那一組
    assert _aggregate(rows)["markets"]["tw"]["h"]["5"]["r_pm"] == 200  # 紀錄不見了：帳裡最後寫的那一組
    moved = {("tw", "2026-10-05"): perf.Record("2026-10-05", 2, "ai_v1", (perf.Pick("2399", 100.0),))}
    assert _aggregate(rows, selections=moved)["markets"]["tw"]["h"]["5"]["n"] == 0  # 紀錄改過：舊代號不算


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
        "extreme": _aggregate([_row(t, h, 2.999, -0.899) for t in days * 2000 for h in (5, 20)][:20000]),
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
        assert "台股推薦（10/01 起，算到 10/22）" in texts and "10/12 起改成 AI 相關股" in texts
        assert "推薦後 5 個交易日：平均漲 2.5%，0050 同期漲 0.5%" in texts
        assert "10 次推薦裡有 10 次比 0050 好" in texts
        assert texts.count("另有 1 次因為資料問題沒有算進去") == 2
        assert "美股推薦（美國時間 10/01 起，算到 10/08）" in texts
        assert "推薦後 5 個交易日：平均跌 2.0%，SPY 同期漲 1.0%" in texts
        assert alt == ("股票推薦：只供參考｜推薦成績｜台股推薦後 5 個交易日平均漲 2.5%（0050 漲 0.5%）"
                       "｜美股推薦後 5 個交易日平均跌 2.0%（SPY 漲 1.0%）")


# ── 結果檔、summary、介面 ────────────────────────────────────────────────────
def test_summary_reads_only_the_result_file(world, monkeypatch):
    assert perf.summary() == {"perf_date": None, "ledger_rows": None, "ledger_full": False, "pending": None,
                              "skipped": None}
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
    assert perf.summary() == {"perf_date": "2026-11-30", "ledger_rows": 4, "ledger_full": False, "pending": 2,
                              "skipped": 2}
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
    assert (perf.US_HISTORY_NAME, perf.RESULT_NAME, perf.LEDGER_NAME, perf.STATE_NAME) == (
        "stock_picks_us_history.jsonl", "stock_picks_perf.json", "stock_picks_perf_ledger.jsonl",
        "stock_picks_perf_state.json")
    assert (perf.HORIZONS, perf.MIN_N, perf.LEDGER_MAX_BYTES) == ((5, 20), 5, 10 * 1024 * 1024)
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
