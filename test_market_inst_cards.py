"""機構動向第一期（2026-10-31，IMPL_SPEC_p1 §4、PLAN_v2 §3.2–3.3；Phase 6 修正見 reviews/p6_triage.md）：
publish 的新列與 /盤勢 的機構動向卡（P 軌）。

- 新列：us.inst.SOXX（資金流＋13F 新一季進度）、us.inst.QQQ／VOO（只有 13F 進度；卡上寫「資金流：暫不提供」）、
  tw.inst.0050（申購贖回＋前十大個股法人）。SPY 的資金流 obs 只給戰情室，不上卡。
- obs 照 §1、§3 的契約（F 軌的 official_prev_session、days 每筆的 flag；T 軌的 days）；這裡全用合成 obs。
- 卡片：「/盤勢 13F SOXX|QQQ|VOO」＝［機構動向短卡、原本的 13F 卡］；「/盤勢 13F 0050」＝0050 機構動向單張；
  旗標檔 state/market_inst.disabled 在、或快照還沒有那一列時一切照舊。只用固定句（market_calc 的 TEXT_INST_*）＋程式算的
  數字與日期。
- 「最新資料日」會被 output_validator 當成「宣稱最新資料卻沒有來源」擋下（_HIGH_RISK_CURRENT_DATA_RE）；落後時在那一段的
  日期行加「最近一筆」（Phase 6 I-5），不另起一行、不重印日期。
"""

from __future__ import annotations

import ast
import copy
import json
import os
from datetime import date, datetime, time

import pytest

import card_pages
import market_calc
import market_paths
import market_publish
import output_validator
import test_market_publish as tpub
from test_market_dashboard import (  # noqa: F401  (md、paths 是 fixture)
    ALL_NOWS, DISCLAIMER, ET, HERE, LONG_PAGE_URL, NOW, TPE, _never, _row, actions, assert_main_gate, bubbles,
    make_snapshot, md, paths, reply_strings, size, texts, worst_snapshot, write_snapshot,
)

DAY = date(2026, 10, 16)  # make_snapshot 的資料日（NOW＝台北 10/17（六）16:30 的前一天）
TOP_CODES = ("2330", "2317", "2454", "2308", "2382", "2891", "2881", "3711", "2882", "2303")
INST_ROWS = ("us.inst.SOXX", "us.inst.QQQ", "us.inst.VOO", "tw.inst.0050")
TW_HEAD = "資金流（已發行單位變化；1 張＝1,000 單位）"

# 鎖住精確的字（FIXED_TEXTS 的禁用字、2027／2028 validator、年份掃描在 test_market_calc 自動涵蓋）
EXPECTED_TEXTS = {
    "TEXT_INST_TITLE": "機構動向｜{name}",
    "TEXT_INST_SWIPE": "往左滑看機構持股（13F）",
    "TEXT_INST_BUTTON": "機構動向",
    "TEXT_INST_BACK_TW": "回台灣50細節",
    "TEXT_INST_FLOW_HEAD": "資金流（在外股數變化）",
    "TEXT_INST_FLOW_HEAD_TW": TW_HEAD,
    "TEXT_INST_FLOW_NONE": "資金流：暫不提供",
    "TEXT_INST_CHANGE": "{start}→{end}：{change}",
    "TEXT_INST_CHANGE_BEHIND": "{start}→{end}（最近一筆）：{change}",
    "TEXT_INST_FIRST_RECORD": "{md} 起開始記錄",
    "TEXT_INST_FIRST_RECORD_BEHIND": "{md}（最近一筆）起開始記錄",
    "TEXT_INST_AMOUNT_ZHAO": "約 {amount} 兆{currency}（估算）",
    "TEXT_INST_AMOUNT_YI": "約 {amount} 億{currency}（估算）",
    "TEXT_INST_AMOUNT_WAN": "約 {amount} 萬{currency}（估算）",
    "TEXT_INST_AMOUNT_SMALL_IN": "淨流入不到 1 萬{currency}（估算）",
    "TEXT_INST_AMOUNT_SMALL_OUT": "淨流出不到 1 萬{currency}（估算）",
    "TEXT_INST_5D": "近 5 日（{start} 起）：{change}",
    "TEXT_INST_CHECK": "資料待核對",
    "TEXT_INST_FLOW_NOTE": "註：申購贖回含全體投資人，不等於機構買賣",
    "TEXT_INST_UNIT_SHARES": "股",
    "TEXT_INST_UNIT_LOTS": "張",
    "TEXT_INST_CURRENCY_USD": "美元",
    "TEXT_INST_CURRENCY_TWD": "元",
    "TEXT_INST_13F_LABEL": "13F 新一季",
    "TEXT_INST_13F_HEAD": "13F 新一季（{qe}，截止 {deadline}）",
    "TEXT_INST_13F_HEAD_UNKNOWN": "13F 新一季（{qe}，截止日未知）",
    "TEXT_INST_13F_PROGRESS": "成分股前十大持有人：{x}/{n} 筆已是 {qe} 季報",
    "TEXT_INST_13F_NOTE": "（Nasdaq 資料，名單會變動）",
    "TEXT_INST_T86_LABEL": "前十大法人",
    "TEXT_INST_T86_HEAD": "前十大法人（{md}，張）",
    "TEXT_INST_T86_HEAD_BEHIND": "前十大法人（{md}，最近一筆，張）",
    "TEXT_INST_T86_ROW": "{code} 外資 {foreign} 投信 {trust} 自營 {dealer}",
    "TEXT_INST_T86_ROW_MAIN": "{code} 外資 {foreign} 投信 {trust}",
    "TEXT_INST_T86_ROW_DEALER": "{code} 自營 {dealer}",
    "TEXT_INST_T86_SUM_LABEL": "近 5 日合計",
    "TEXT_INST_T86_SUM_HEAD": "近 5 日合計（{start}–{end}，前 5 檔，張）",
    "TEXT_INST_UNDER_ONE_LOT": "不到 1 張",
    "TEXT_INST_T86_NOTE": "外資不含外資自營商；自營＝自行買賣＋避險",
}
F13_LINES = ["13F 新一季（9/30，截止 11/16）", "成分股前十大持有人：37/100 筆已是 9/30 季報", "（Nasdaq 資料，名單會變動）"]
SOXX_LINES = ["資金流（在外股數變化）", "10/15→10/16：+600,000 股", "約 +3.1 億美元（估算）",
              "近 5 日（10/9 起）：+1,200,000 股", *F13_LINES]
TW_FLOW_LINES = [TW_HEAD, "10/15→10/16：+12,000 張", "約 +22.2 億元（估算）", "近 5 日（10/9 起）：-30,000 張"]
T86_DAY_LINES = [f"{code} 外資 +{1234 - 100 * i:,} 投信 -56 自營 -1" for i, code in enumerate(TOP_CODES)]
T86_SUM_LINES = ["近 5 日合計（10/12–10/16，前 5 檔，張）",
                 *[f"{code} 外資 +{6170 - 500 * i:,} 投信 -280" for i, code in enumerate(TOP_CODES[:5])]]


# ── 合成快照的列（照 market_calc 的 INST_*_SPEC） ─────────────────────────────────
def flows_section(now: float = NOW, **changes) -> dict:
    section = {"as_of": "2026-10-16", "prev_as_of": "2026-10-15", "change": 600_000, "flow": 307_404_000.0,
               "change_5d": {"from_as_of": "2026-10-09", "to_as_of": "2026-10-16", "change": 1_200_000},
               "flag": None, "behind": False, "stale_after": now + 86400}
    section.update(changes)
    return section


def tw_flows_section(now: float = NOW, **changes) -> dict:
    return flows_section(now, **{"change": 12_000_000, "flow": 2_220_000_000.0,
                                 "change_5d": {"from_as_of": "2026-10-09", "to_as_of": "2026-10-16",
                                               "change": -30_000_000}, **changes})


def f13_section(now: float = NOW, **changes) -> dict:
    section = {"quarter_end": "2026-09-30", "deadline": "2026-11-16", "updated": 37, "total": 100,
               "stale_after": now + 86400}
    section.update(changes)
    return section


def t86_items(scale: int = 1) -> list[dict]:
    return [{"code": code, "foreign": (1_234_000 - 100_000 * i) * scale, "trust": -56_000 * scale,
             "dealer": -1_000 * scale} for i, code in enumerate(TOP_CODES)]


def t86_section(now: float = NOW, **changes) -> dict:
    section = {"trade_date": "2026-10-16", "behind": False, "stale_after": now + 86400, "rows": t86_items(),
               "sum5": {"from": "2026-10-12", "to": "2026-10-16", "days": 5, "rows": t86_items(5)}}
    section.update(changes)
    return section


def inst_snapshot(now: float = NOW, **sections) -> dict:
    """make_snapshot＋四個機構動向列；sections 可以換掉某一段（例 soxx_flows=None）。"""
    snap = make_snapshot(now)
    rows = snap["rows"]
    rows["us.inst.SOXX"] = _row({"flows": sections.get("soxx_flows", flows_section(now)),
                                 "f13": sections.get("soxx_f13", f13_section(now))}, now, DAY)
    for sym in ("QQQ", "VOO"):
        rows[f"us.inst.{sym}"] = _row({"flows": None, "f13": sections.get(f"{sym.lower()}_f13", f13_section(now))},
                                      now, None)
    rows["tw.inst.0050"] = _row({"flows": sections.get("tw_flows", tw_flows_section(now)),
                                 "t86": sections.get("t86", t86_section(now))}, now, DAY)
    return snap


def old_snapshot(now: float = NOW) -> dict:
    """job 還沒升級時寫的快照：沒有這四列。"""
    snap = make_snapshot(now)
    for row_id in INST_ROWS:
        snap["rows"].pop(row_id, None)
    return snap


def worst_inst_snapshot(now: float = NOW) -> dict:
    """最壞值：13 位數的股數、負值、兩位數日期、最大的金額、6 碼代號、每檔都要分兩列的 T86、落後的加註。"""
    snap = worst_snapshot(now)
    huge = -9_999_999_999_999
    worst_flow = flows_section(now, as_of="2026-12-31", prev_as_of="2026-12-24", change=huge, flow=-9.99e14,
                               change_5d={"from_as_of": "2026-12-17", "to_as_of": "2026-12-31", "change": huge},
                               behind=True)
    worst_f13 = f13_section(now, quarter_end="2026-12-31", deadline="2027-02-16", updated=500, total=500)
    big = [{"code": f"{code}AB", "foreign": huge, "trust": -huge, "dealer": huge} for code in TOP_CODES]
    worst_t86 = t86_section(now, trade_date="2026-12-31", behind=True, rows=big,
                            sum5={"from": "2026-12-24", "to": "2026-12-31", "days": 5, "rows": copy.deepcopy(big)})
    rows = snap["rows"]
    rows["us.inst.SOXX"] = _row({"flows": worst_flow, "f13": worst_f13}, now, date(2026, 12, 31))
    for sym in ("QQQ", "VOO"):
        rows[f"us.inst.{sym}"] = _row({"flows": None, "f13": copy.deepcopy(worst_f13)}, now, None)
    rows["tw.inst.0050"] = _row({"flows": copy.deepcopy(worst_flow), "t86": worst_t86}, now, date(2026, 12, 31))
    return snap


def render(md_, paths_, snap, body, *, now=NOW):
    write_snapshot(paths_, snap)
    return md_.handle(body, now=now, paths=paths_)


def inst_bubble(reply) -> dict:
    """13F 指令是輪播的第一張；0050 是單張。"""
    flex = reply.flex
    return flex["contents"][0] if flex["type"] == "carousel" else flex


def body_texts(bubble: dict) -> list[str]:
    return texts(bubble["body"])


def soxx_body(md_, paths_, snap) -> list[str]:
    return body_texts(inst_bubble(render(md_, paths_, snap, " 13F SOXX")))


def tw_body(md_, paths_, snap) -> list[str]:
    return body_texts(render(md_, paths_, snap, " 13F 0050").flex)


def set_flag(paths_) -> None:
    (paths_.state_dir / "market_inst.disabled").write_text("")


# ── market_calc：新列、固定句、共用檢查、截止日、就緒時間、過期規則 ───────────────────────
def test_the_new_rows_are_registered_after_the_base_rows():
    ids = list(market_calc.ROW_IDS)
    assert set(INST_ROWS) <= set(ids)
    positions = [ids.index(row_id) for row_id in INST_ROWS]
    assert positions == sorted(positions) and min(positions) > ids.index("contra.tw")  # 接在 base 那批列之後（不插在中間）
    for sym in ("SOXX", "QQQ", "VOO"):
        assert market_calc.ROW_IDS[f"us.inst.{sym}"] == {"flows": ("?", market_calc.INST_FLOW_SPEC),
                                                         "f13": ("?", market_calc.INST_13F_SPEC)}
    assert market_calc.ROW_IDS["tw.inst.0050"] == {"flows": ("?", market_calc.INST_FLOW_SPEC),
                                                   "t86": ("?", market_calc.INST_T86_SPEC)}
    assert "us.inst.SPY" not in market_calc.ROW_IDS  # SPY 的資金流只給戰情室
    assert market_calc.INST_FLOW_SYMBOLS == market_publish.INST_FLOW_SYMBOLS == ("SOXX",)  # job、bot 同一份
    assert market_calc.INST_FLOW_FLAGS == ("check",)  # not_updated 沒有人寫（Phase 6 R4-7）
    for row_id in INST_ROWS:
        assert market_publish._label_for(row_id) == "機構動向"
    assert market_publish._label_for("tw.inst3") == "三大法人"  # 舊的三大法人列不受前綴影響


def test_the_synthetic_rows_follow_the_specs_and_the_shared_checks():
    for snap in (inst_snapshot(), worst_inst_snapshot()):
        for row_id in INST_ROWS:
            assert market_calc.row_ok(row_id, snap["rows"][row_id]), row_id
            assert market_calc.inst_values_ok(row_id, snap["rows"][row_id]["values"]), row_id


@pytest.mark.parametrize("section, changes", [
    ("flows", {"prev_as_of": "2026-10-16"}), ("flows", {"prev_as_of": None}),  # 起點不在前面；沒有起點卻有變化
    ("flows", {"flow": 1.0, "change": None, "prev_as_of": None}), ("flows", {"change": 10**14}),
    ("flows", {"change_5d": {"from_as_of": "2026-10-09", "to_as_of": "2026-10-15", "change": 5}}),  # 終點不是資料日
    ("flows", {"flag": "not_updated"}),
    ("f13", {"deadline": "2026-09-30"}), ("f13", {"updated": 101}), ("f13", {"total": 501, "updated": 1}),
    ("f13", {"total": 0, "updated": 0}),
    ("t86", {"rows": t86_items() + t86_items()[:1]}),  # 代號重複（而且 11 檔）
    ("t86", {"rows": [dict(t86_items()[0], trust=None)] + t86_items()[1:]}),  # 三個值不能只缺一個
    ("t86", {"rows": [dict(item, foreign=None, trust=None, dealer=None) for item in t86_items()]}),  # 一檔都沒有值
    ("t86", {"rows": [dict(t86_items()[0], code="23a0")] + t86_items()[1:]}),
    ("t86", {"sum5": {"from": "2026-10-12", "to": "2026-10-15", "days": 5, "rows": t86_items()}}),  # 終點不是交易日
    ("t86", {"sum5": {"from": "2026-10-12", "to": "2026-10-16", "days": 6, "rows": t86_items()}}),
    ("t86", {"sum5": {"from": "2026-10-12", "to": "2026-10-16", "days": 5, "rows": []}}),  # 5 天都齊就要有代號
    ("t86", {"sum5": {"from": "2026-10-12", "to": "2026-10-16", "days": 5,
                      "rows": [{"code": "1101", "foreign": 1, "trust": 1, "dealer": 1}]}}),  # 不在前十大
])
def test_the_shared_checks_reject_inconsistent_sections(section, changes):
    """publish、validate_snapshot、bot 都用這一份（Phase 6 I-6）：以前 bot 不查截止日、代號重複，publish 和 bot 對
    「沒有起點卻有變化」的處理也不一樣。"""
    build = {"flows": flows_section, "f13": f13_section, "t86": t86_section}[section]
    check = {"flows": market_calc.inst_flow_ok, "f13": market_calc.inst_13f_ok, "t86": market_calc.inst_t86_ok}[section]
    assert check(build()) and not check(build(**changes))
    row_id = "tw.inst.0050" if section in ("t86",) else "us.inst.SOXX"
    values = {"flows": flows_section(), "f13": f13_section()} if row_id == "us.inst.SOXX" else {
        "flows": tw_flows_section(), "t86": t86_section()}
    values[section] = build(**changes)
    assert not market_calc.inst_values_ok(row_id, values)


def test_qqq_and_voo_rows_must_not_carry_flows():
    assert market_calc.inst_values_ok("us.inst.QQQ", {"flows": None, "f13": f13_section()})
    assert not market_calc.inst_values_ok("us.inst.QQQ", {"flows": flows_section(), "f13": f13_section()})
    assert not market_calc.inst_values_ok("us.etf.QQQ", {"flows": None, "f13": f13_section()})


def test_obs_ids_follow_the_contract():
    assert market_publish.OBS_US_FLOWS.format("SOXX") == "us.flows.etf.SOXX"
    assert market_publish.OBS_TW_FLOWS == "tw.flows.etf.0050"
    assert market_publish.OBS_TW_T86 == "tw.inst.t86.0050"


def test_fixed_sentences_are_exact_and_pass_the_checks():
    names = sorted(name for name in dir(market_calc) if name.startswith("TEXT_INST_"))
    assert names == sorted(EXPECTED_TEXTS)  # 拿掉的句子（最近資料日、前一份資料暫缺、單位）不會留著
    for name, text in EXPECTED_TEXTS.items():
        assert getattr(market_calc, name) == text and text in market_calc.FIXED_TEXTS, name
        assert not any(word in text for word in market_calc.FORBIDDEN_WORDS), name
        assert not any(str(year) in text for year in range(2020, 2040)), name
        for now in ALL_NOWS:
            result = output_validator.validate_outbound_text(text, now=datetime.fromtimestamp(now, TPE))
            assert result.ok and result.text == text, (name, result.reason)


def test_the_spec_wording_for_the_latest_day_would_be_blocked_by_the_validator():
    """「最新資料日 10/26」被 validator 擋（宣稱最新資料、沒有來源）；落後的加註「最近一筆」過得了。"""
    at = datetime.fromtimestamp(NOW, TPE)
    blocked = output_validator.validate_outbound_text("最新資料日 10/26", now=at)
    assert not blocked.ok and blocked.reason == "unverified_current_or_official_data"
    for used in (market_calc.TEXT_INST_CHANGE_BEHIND.format(start="10/25", end="10/26", change="+600,000 股"),
                 market_calc.TEXT_INST_T86_HEAD_BEHIND.format(md="10/26")):
        assert output_validator.validate_outbound_text(used, now=at).ok, used


@pytest.mark.parametrize("day, expected", [
    (date(2026, 10, 31), date(2026, 9, 30)), (date(2026, 10, 1), date(2026, 9, 30)),
    (date(2026, 9, 30), date(2026, 6, 30)), (date(2026, 12, 31), date(2026, 9, 30)),
    (date(2027, 1, 1), date(2026, 12, 31)), (date(2026, 4, 1), date(2026, 3, 31)),
    (date(2026, 3, 31), date(2025, 12, 31)), (date(2028, 2, 29), date(2027, 12, 31)),
])
def test_latest_quarter_end_is_strictly_before_the_day(day, expected):
    assert market_calc.latest_quarter_end(day) == expected


def test_13f_deadlines_come_from_the_static_table():
    table = {date(2025, 12, 31): date(2026, 2, 17), date(2026, 3, 31): date(2026, 5, 15),
             date(2026, 6, 30): date(2026, 8, 14), date(2026, 9, 30): date(2026, 11, 16),
             date(2026, 12, 31): date(2027, 2, 16), date(2027, 3, 31): date(2027, 5, 17),
             date(2027, 6, 30): date(2027, 8, 16), date(2027, 9, 30): date(2027, 11, 15)}
    assert market_calc.F13_DEADLINES == table
    assert market_calc.f13_deadline(date(2026, 9, 30), date(2026, 10, 31)) == date(2026, 11, 16)
    assert market_calc.f13_deadline(date(2027, 9, 30), date(2027, 12, 31)) == date(2027, 11, 15)
    assert market_calc.F13_DEADLINES_SUPPORTED_UNTIL == date(2027, 12, 31)
    # 表之後：截止日未知（不猜）
    assert market_calc.f13_deadline(date(2027, 12, 31), date(2028, 1, 3)) is None
    assert market_calc.f13_deadline(date(2027, 9, 30), date(2028, 1, 3)) is None
    assert market_calc.f13_deadline(date(2025, 9, 30), date(2025, 10, 31)) is None


def _calendars() -> dict:
    tw_holidays = {date.fromisoformat(day) for day, _kind in tpub.TW_2026_HOLIDAYS}
    return {"nyse": market_calc.nyse_calendar(),
            "twse": market_calc.twse_calendar(holidays=tw_holidays, table_from=date(2026, 1, 1),
                                              table_to=date(2026, 12, 31))}


def _et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET).timestamp()


def _tpe(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=TPE).timestamp()


def test_the_readiness_table():
    assert market_calc.INST_READY == {"us.flows": ("nyse", time(18, 0)), "tw.flows": ("twse", time(17, 0)),
                                      "tw.t86": ("twse", time(16, 30))}


@pytest.mark.parametrize("source, now, expected", [
    ("us.flows", _et(2026, 10, 16, 17, 59), date(2026, 10, 15)), ("us.flows", _et(2026, 10, 16, 18, 0), date(2026, 10, 16)),
    ("us.flows", _et(2026, 10, 17, 9, 0), date(2026, 10, 16)),  # 週六
    ("us.flows", _et(2026, 10, 19, 9, 0), date(2026, 10, 16)),  # 週一早上：還是上週五
    ("us.flows", _et(2026, 11, 27, 18, 30), date(2026, 11, 27)),  # 早收日一樣看 18:00
    ("tw.flows", _tpe(2026, 10, 16, 16, 59), date(2026, 10, 15)), ("tw.flows", _tpe(2026, 10, 16, 17, 0), date(2026, 10, 16)),
    ("tw.flows", _tpe(2026, 10, 16, 14, 45), date(2026, 10, 15)),  # 以前用 14:30 換日：這時已經算落後
    ("tw.t86", _tpe(2026, 10, 16, 16, 29), date(2026, 10, 15)), ("tw.t86", _tpe(2026, 10, 16, 16, 30), date(2026, 10, 16)),
    ("tw.t86", _tpe(2026, 10, 27, 10, 0), date(2026, 10, 23)),  # 10/26 休市（合成表）
])
def test_expected_day_follows_the_readiness_time(source, now, expected):
    assert market_calc.inst_expected_day(source, _calendars(), now) == expected


def test_expected_day_is_unknown_outside_the_calendar():
    assert market_calc.inst_expected_day("tw.flows", _calendars(), _tpe(2027, 3, 1, 18, 0)) is None


@pytest.mark.parametrize("source, now, day, lag", [
    ("us.flows", _et(2026, 10, 16, 18, 0), date(2026, 10, 16), 0),
    ("us.flows", _et(2026, 10, 16, 17, 0), date(2026, 10, 16), 0),  # 比預期新（就緒時間還沒到就更新了）：0，不是負數
    ("us.flows", _et(2026, 10, 19, 18, 30), date(2026, 10, 15), 2),  # 跨週末只數交易日
    ("tw.t86", _tpe(2026, 10, 27, 17, 0), date(2026, 10, 22), 2),  # 10/26 休市（合成表）：只數 10/23、10/27
    ("us.flows", _et(2026, 10, 16, 18, 0), None, None),  # 從沒跑過
    ("tw.flows", _tpe(2027, 3, 1, 18, 0), date(2026, 12, 31), None),  # 日曆說不出預期的資料日
    ("us.flows", _et(2026, 1, 5, 18, 0), date(1999, 12, 31), None),  # 中間有日曆不知道的日子：數不出來
])
def test_lag_sessions_counts_trading_days_or_says_it_cannot(source, now, day, lag):
    assert market_calc.inst_lag_sessions(source, _calendars(), now, day) == lag


def test_flows_go_stale_two_us_sessions_after_the_data_date():
    cal = _calendars()
    at_close = lambda d: datetime.combine(d, time(16, 0), tzinfo=ET).timestamp()  # noqa: E731
    assert market_calc.inst_stale_after(market_calc.KIND_INST_FLOWS_US, date(2026, 10, 27), cal) == at_close(
        date(2026, 10, 29))
    # 週五的資料：跨週末到下週二收盤
    assert market_calc.inst_stale_after(market_calc.KIND_INST_FLOWS_US, date(2026, 10, 30), cal) == at_close(
        date(2026, 11, 3))
    # 感恩節（11/26 休、11/27 早收）：第 2 個交易日是 11/30
    assert market_calc.inst_stale_after(market_calc.KIND_INST_FLOWS_US, date(2026, 11, 25), cal) == at_close(
        date(2026, 11, 30))
    # 日曆表外：資料日＋6 個日曆日的收盤（同 §18.3 的退路）
    assert market_calc.inst_stale_after(market_calc.KIND_INST_FLOWS_US, date(2027, 12, 30), cal) == at_close(
        date(2028, 1, 5))


def test_0050_flows_and_t86_go_stale_two_tw_sessions_after_the_data_date():
    cal = _calendars()
    at_close = lambda d: datetime.combine(d, time(13, 30), tzinfo=TPE).timestamp()  # noqa: E731
    for kind in (market_calc.KIND_INST_FLOWS_TW, market_calc.KIND_INST_T86):
        assert market_calc.inst_stale_after(kind, date(2026, 10, 16), cal) == at_close(date(2026, 10, 20))
        # 10/26 休市（合成表）：10/23 的資料到 10/28 收盤
        assert market_calc.inst_stale_after(kind, date(2026, 10, 23), cal) == at_close(date(2026, 10, 28))
    with pytest.raises(ValueError):
        market_calc.inst_stale_after(market_calc.KIND_US_DAILY, date(2026, 10, 16), cal)


# ── publish：從合成 obs 組列 ───────────────────────────────────────────────────
SOXX_DAYS = (("2026-10-16", 94_600_000, 512.34), ("2026-10-15", 94_000_000, 505.0), ("2026-10-14", 93_900_000, 500.0),
             ("2026-10-13", 93_800_000, 501.0), ("2026-10-12", 93_600_000, 499.0), ("2026-10-09", 93_400_000, 498.0))


def soxx_days(**flags) -> list[dict]:
    return [{"as_of": day, "shares_out": shares, "nav": nav, "flag": flags.get(day)} for day, shares, nav in SOXX_DAYS]


def us_flow_obs(**changes) -> dict:
    payload = {"v": 1, "symbol": "SOXX", "source": "iShares", "source_url": "https://www.example.com/soxx",
               "unit": "USD", "shares_out": 94_600_000, "shares_out_as_of": "2026-10-16",
               "prev_shares_out": 94_000_000, "prev_as_of": "2026-10-15", "change": 600_000,
               "official_prev_session": None, "nav": 512.34, "nav_as_of": "2026-10-16", "flow": 307_404_000.0,
               "method": "Δshares×NAV", "derived": True, "flag": None,
               "change_5d": {"from_as_of": "2026-10-09", "to_as_of": "2026-10-16", "change": 1_200_000},
               "days": soxx_days()}
    payload.update(changes)
    return payload


def tw_flow_obs(**changes) -> dict:
    payload = {"v": 1, "symbol": "0050", "source": "元大", "source_url": "https://www.example.com/0050",
               "unit": "TWD", "shares_out": 16_000_000_000, "shares_out_as_of": "2026-10-16",
               "prev_shares_out": 15_988_000_000, "prev_as_of": "2026-10-15", "change": 12_000_000,
               "official_prev_session": "2026-10-15", "nav": 185.0, "nav_as_of": "2026-10-16", "flow": 2_220_000_000.0,
               "method": "Δshares×NAV", "derived": True, "flag": None, "change_5d": None,
               "days": [{"as_of": "2026-10-16", "shares_out": 16_000_000_000, "nav": 185.0, "flag": None}]}
    payload.update(changes)
    return payload


T86_DAYS = ("2026-10-16", "2026-10-15", "2026-10-14", "2026-10-13", "2026-10-12")  # 10/9 休市（合成表）


def t86_obs(*, codes=TOP_CODES, missing_day_for=(), **changes) -> dict:
    rows = [{"code": code, "foreign_net": 1_234_000 - 1000 * i, "trust_net": -56_000, "dealer_self_net": 1_000,
             "dealer_hedge_net": -2_000, "total_net": 1_177_000 - 1000 * i} for i, code in enumerate(codes)]
    days = {day: {code: [100_000 * (k + 1), -10_000, 500, -1_500] for code in codes if
                  not (code in missing_day_for and day == "2026-10-14")} for k, day in enumerate(T86_DAYS)}
    payload = {"v": 1, "source": "TWSE T86", "source_url": "https://www.example.com/t86", "unit": "股",
               "trade_date": "2026-10-16", "constituents_as_of": "2026-10-16", "rows": rows, "days": days}
    payload.update(changes)
    return payload


def holdings_obs(codes) -> dict:
    """weight 由大到小：第一個最重。多放兩檔權重小的（不該進前十大）。"""
    rows = [[code, f"台股{code}", 30.0 - i] for i, code in enumerate(codes)]
    return {"source": "moneydj", "as_of": "2026-10-16", "count": len(rows), "rows": rows}


@pytest.fixture
def world(tmp_path, monkeypatch):
    import market_sources_misc

    monkeypatch.setattr(market_sources_misc, "PREDICTION_MARKETS_BLOCKED", False)
    w = tpub.World(tmp_path, misc=False)
    # 這幾個 obs 由每個測試自己放（test_market_publish 的合成資料也有放，先清掉才不互相影響）
    w.store._db().execute("DELETE FROM obs WHERE obs_id IN (?, ?, ?, ?)",
                          ("us.flows.etf.SOXX", "us.flows.etf.SPY", "tw.flows.etf.0050", "tw.inst.t86.0050"))
    w.store.put_obs("tw.holdings.0050", holdings_obs((*TOP_CODES, "1101", "1216")), data_date="2026-10-16")
    yield w
    w.close()


def put(world, obs_id: str, payload: dict, data_date: str = "2026-10-16") -> None:
    world.store.put_obs(obs_id, payload, data_date=data_date)


def build(world, row_id: str, now: float = tpub.NOW) -> dict:
    ctx = market_publish._Ctx(world.store, world.paths, now)
    row = market_publish._build_row(ctx, row_id)
    assert not ctx.row_errors, ctx.row_errors
    return row


def us_close(day: date) -> float:
    return datetime.combine(day, time(16, 0), tzinfo=ET).timestamp()


def tw_close(day: date) -> float:
    return datetime.combine(day, time(13, 30), tzinfo=TPE).timestamp()


def test_soxx_row_has_the_flows_and_the_13f_progress(world):
    put(world, "us.flows.etf.SOXX", us_flow_obs())
    row = build(world, "us.inst.SOXX")
    assert row["state"] == "ok" and market_calc.row_ok("us.inst.SOXX", row) and row["label"] == "機構動向"
    flows = row["values"]["flows"]
    assert flows == {"as_of": "2026-10-16", "prev_as_of": "2026-10-15", "change": 600_000, "flow": 307_404_000.0,
                     "change_5d": {"from_as_of": "2026-10-09", "to_as_of": "2026-10-16", "change": 1_200_000},
                     "flag": None, "behind": False,
                     "stale_after": us_close(date(2026, 10, 20))}  # 10/16（五）＋2 個美股交易日
    # 13F：SOXX 前十大（合成 5 檔）× 各 10 個持有人；10/17 之前最近的季末是 9/30，合成的申報期間都還是 6/30、12/31
    assert row["values"]["f13"] == {"quarter_end": "2026-09-30", "deadline": "2026-11-16", "updated": 0, "total": 50,
                                    "stale_after": datetime(2026, 12, 2, tzinfo=TPE).timestamp()}  # 跟著 13F 列（眾數＋155 天）
    assert row["data_date"] == "2026-10-16" and row["stale_after"] == max(flows["stale_after"],
                                                                           row["values"]["f13"]["stale_after"])


def test_13f_progress_counts_holders_already_on_the_latest_quarter_end(world):
    put(world, "us.13f.sym.CCC", {"increased": 1, "decreased": 1, "holder_periods": ["2026-09-30"] * 4 + ["2026-06-30"] * 6},
        data_date=None)
    put(world, "us.13f.sym.GGG", {"increased": 1, "decreased": 1, "holder_periods": ["2026-09-30"] * 9 + ["bad", None]},
        data_date=None)
    f13 = build(world, "us.inst.SOXX")["values"]["f13"]
    assert (f13["updated"], f13["total"]) == (13, 49)  # 9＋4；壞的兩筆不算進 N
    assert build(world, "us.inst.QQQ")["values"]["f13"]["updated"] == 9  # QQQ 前十大也有 GGG；CCC 不在 QQQ
    # 7/15：最近季末是 6/30（截止 8/14），合成的 6/30 都算已更新
    july = datetime(2026, 7, 15, 12, 0, tzinfo=TPE).timestamp()
    f13 = build(world, "us.inst.VOO", july)["values"]["f13"]
    assert (f13["quarter_end"], f13["deadline"]) == ("2026-06-30", "2026-08-14")
    # 2028：截止日表之外 → null（卡上寫「截止日未知」）
    later = datetime(2028, 1, 3, 12, 0, tzinfo=TPE).timestamp()
    assert build(world, "us.inst.VOO", later)["values"]["f13"]["deadline"] is None


def test_qqq_and_voo_rows_never_carry_flows(world):
    put(world, "us.flows.etf.SPY", us_flow_obs(symbol="SPY"))  # SPY 只給戰情室：不會變成 VOO 的資金流
    for sym in ("QQQ", "VOO"):
        row = build(world, f"us.inst.{sym}")
        assert row["state"] == "ok" and row["values"]["flows"] is None and row["values"]["f13"]["total"] == 50
        assert row["data_date"] is None


@pytest.mark.parametrize("changes, expected", [
    ({"flag": "check"}, {"flag": "check"}),
    ({"flag": "weird"}, {"flag": "check"}),  # 不認得的旗標：從嚴，當待核對
    ({"flag": "not_updated"}, {"flag": "check"}),  # 已經拿掉的值（F 軌不寫；Phase 6 R4-7）也一樣
    ({"change": 600_001}, {"flag": "check"}),  # SOXX 的 change 要等於 shares_out − prev_shares_out
    ({"nav_as_of": "2026-10-15"}, {"flow": None}),  # NAV 和股數不同天：只給股數
    ({"nav_as_of": None}, {"flow": None}),
    ({"unit": "TWD"}, {"flow": None}),
    ({"flow": None}, {"flow": None}),
    ({"prev_as_of": "2026-10-16"}, {"flag": "check", "prev_as_of": None, "change": None, "flow": None}),  # 自相矛盾
    ({"prev_as_of": None, "prev_shares_out": None, "change": None, "flow": None},
     {"prev_as_of": None, "change": None, "flow": None, "flag": None}),  # 第一次抓到
    ({"change_5d": None}, {"change_5d": None}),
    ({"change_5d": {"from_as_of": "2026-10-16", "to_as_of": "2026-10-09", "change": 5}}, {"change_5d": None}),
    ({"change_5d": {"from_as_of": "2026-10-09", "to_as_of": "2026-10-16", "change": 1.5}}, {"change_5d": None}),
    ({"shares_out_as_of": "2026-10-15", "nav_as_of": "2026-10-15", "prev_as_of": "2026-10-14", "shares_out": 94_000_000,
      "prev_shares_out": 93_900_000, "change": 100_000},
     {"as_of": "2026-10-15", "prev_as_of": "2026-10-14", "behind": True, "change_5d": None,
      "stale_after": us_close(date(2026, 10, 19))}),  # 比預期的資料日（10/16）舊
])
def test_soxx_flows_are_checked_before_they_reach_the_card(world, changes, expected):
    put(world, "us.flows.etf.SOXX", us_flow_obs(**changes))
    flows = build(world, "us.inst.SOXX")["values"]["flows"]
    for key, value in expected.items():
        assert flows[key] == value, (key, flows)


@pytest.mark.parametrize("start, end, over", [
    (124_000_000, 100_700_000, True),  # 複審 R2-1-5：起點讀錯 +24%；舊分母（起點）只有 18.8%，較小的那筆是 23.1%
    (100_700_000, 124_000_000, True),
    (120_000_000, 100_000_000, False), (100_000_000, 120_000_000, False),  # 兩個方向同一個幅度；正好 20% 不算超過
    (100_000_000, 100_500_000, False), (100_000_000, 100_000_000, False),
    (0, 100_000_000, True), (100_000_000, 0, True), (-1, 100_000_000, True),  # 股數不是正數：算超過
])
def test_the_five_day_threshold_divides_by_the_smaller_end(start, end, over):
    assert market_calc.inst_5d_over(start, end, 0.20) is over


@pytest.mark.parametrize("start_shares, kept", [
    (116_000_000, False),  # 起點讀錯 +22.6%：-21,400,000／舊分母 1.16 億＝18.4%，過得了；較小的那筆是 9,460 萬＝22.6%
    (80_000_000, True),  # 對照：真的 +18.25%（終點比較大，分母還是起點）
])
def test_publish_divides_the_five_day_threshold_by_the_smaller_end_too(world, start_shares, kept):
    """複審 R2-1-5：collector、publish 同一條門檻（market_calc.inst_5d_over）。起點自己的 flag 不看，所以這裡要擋得住。"""
    days = [dict(item, shares_out=start_shares) if item["as_of"] == "2026-10-09" else item for item in soxx_days()]
    five = {"from_as_of": "2026-10-09", "to_as_of": "2026-10-16", "change": 94_600_000 - start_shares}
    put(world, "us.flows.etf.SOXX", us_flow_obs(change_5d=five, days=days))
    assert build(world, "us.inst.SOXX")["values"]["flows"]["change_5d"] == (five if kept else None)


@pytest.mark.parametrize("five, days", [
    ({"from_as_of": "2026-10-08", "to_as_of": "2026-10-16", "change": 1_300_000}, soxx_days()),  # 不是正好 5 個交易日前
    ({"from_as_of": "2026-10-09", "to_as_of": "2026-10-16", "change": 30_000_000}, soxx_days()),  # 5 日變化超過 20%
    ({"from_as_of": "2026-10-09", "to_as_of": "2026-10-16", "change": 1_200_000},
     soxx_days(**{"2026-10-13": "check"})),  # 窗口裡有一天自己標了 check（分割、抓錯）
    ({"from_as_of": "2026-10-09", "to_as_of": "2026-10-16", "change": 1_200_000}, soxx_days()[:5]),  # 找不到 5 日前的股數
])
def test_publish_drops_a_five_day_change_that_could_hide_a_split(world, five, days):
    """Phase 6 I-1：F 軌已經擋了，publish 再擋一層——分割後連 4 天印出「近 5 日 +62,700,000 股」那種假數字。"""
    put(world, "us.flows.etf.SOXX", us_flow_obs(change_5d=five, days=days))
    flows = build(world, "us.inst.SOXX")["values"]["flows"]
    assert flows["change_5d"] is None and flows["change"] == 600_000  # 當日變化照常


def test_a_split_in_the_window_keeps_the_five_day_line_off_the_card(world, md):
    split = [dict(item, shares_out=item["shares_out"] * 3) if item["as_of"] >= "2026-10-13" else item
             for item in soxx_days(**{"2026-10-13": "check"})]
    put(world, "us.flows.etf.SOXX", us_flow_obs(shares_out=283_800_000, prev_shares_out=282_000_000, change=1_800_000,
                                                flow=1.0e9, days=split,
                                                change_5d={"from_as_of": "2026-10-09", "to_as_of": "2026-10-16",
                                                           "change": 190_400_000}))
    assert world.publish()["kind"] == "ok"
    lines = body_texts(inst_bubble(md.handle(" 13F SOXX", now=tpub.NOW, paths=world.paths)))
    assert not any(line.startswith("近 5 日") for line in lines) and "10/15→10/16：+1,800,000 股" in lines


def test_unknown_flow_payloads_are_left_out(world):
    for payload in (us_flow_obs(v=2), us_flow_obs(v=True), us_flow_obs(symbol="SPY"), us_flow_obs(shares_out_as_of="bad")):
        world.store.put_obs("us.flows.etf.SOXX", payload, data_date=None)  # data_date 也沒有：日期只能從 payload 來
        ctx = market_publish._Ctx(world.store, world.paths, tpub.NOW)
        row = market_publish._build_row(ctx, "us.inst.SOXX")
        assert row["values"]["flows"] is None and row["values"]["f13"] is not None, payload


@pytest.mark.parametrize("changes", [{"change": 10**14}, {"change": 1.5}, {"change": "600000"}])
def test_an_impossible_share_change_is_flagged_for_checking(world, changes):
    put(world, "us.flows.etf.SOXX", us_flow_obs(**changes))
    flows = build(world, "us.inst.SOXX")["values"]["flows"]
    assert flows["flag"] == "check" and flows["change"] is None and flows["flow"] is None


def test_an_impossible_amount_is_left_out(world):
    put(world, "us.flows.etf.SOXX", us_flow_obs(flow=1e16))
    flows = build(world, "us.inst.SOXX")["values"]["flows"]
    assert flows["flow"] is None and flows["change"] == 600_000 and flows["flag"] is None


def test_0050_row_has_the_flows_and_the_top_ten_t86(world):
    put(world, "tw.flows.etf.0050", tw_flow_obs())
    put(world, "tw.inst.t86.0050", t86_obs())
    row = build(world, "tw.inst.0050")
    assert row["state"] == "ok" and market_calc.row_ok("tw.inst.0050", row)
    flows, t86 = row["values"]["flows"], row["values"]["t86"]
    assert (flows["prev_as_of"], flows["change"], flows["flow"]) == ("2026-10-15", 12_000_000, 2_220_000_000.0)
    assert flows["change_5d"] is None and flows["stale_after"] == tw_close(date(2026, 10, 20))  # 10/16（五）＋2 個台股交易日
    assert t86["trade_date"] == "2026-10-16" and t86["behind"] is False
    assert t86["stale_after"] == tw_close(date(2026, 10, 20))
    # 前十大＝tw.holdings.0050 權重前 10（1101、1216 權重最小，不在裡面）；自營＝自行＋避險
    assert [item["code"] for item in t86["rows"]] == list(TOP_CODES)
    assert t86["rows"][0] == {"code": "2330", "foreign": 1_234_000, "trust": -56_000, "dealer": -1_000}
    # 5 日合計：最近 5 個台股交易日（10/9 休市）都有才算
    sum5 = t86["sum5"]
    assert (sum5["from"], sum5["to"], sum5["days"]) == ("2026-10-12", "2026-10-16", 5)
    assert sum5["rows"][0] == {"code": "2330", "foreign": 1_500_000, "trust": -50_000, "dealer": -5_000}
    assert row["data_date"] == "2026-10-16"


@pytest.mark.parametrize("changes, start", [
    ({}, "2026-10-15"),  # F 軌給的官方差異數起點
    ({"prev_as_of": None, "prev_shares_out": None, "official_prev_session": None}, "2026-10-15"),  # 第一天：台股日曆的前一天
    ({"official_prev_session": "bad"}, "2026-10-15"), ({"official_prev_session": "2026-10-16"}, "2026-10-15"),
    ({"prev_as_of": "2026-10-13", "official_prev_session": "2026-10-15"}, "2026-10-15"),  # 漏抓兩天：起點仍是前一個交易日
])
def test_0050_shows_the_official_change_from_the_previous_session(world, changes, start):
    """Phase 6 I-2：官方差異數本來就是「與前一個交易日」比，第一天、漏抓之後都照樣顯示；不再出現「前一份資料暫缺」。"""
    put(world, "tw.flows.etf.0050", tw_flow_obs(**changes))
    flows = build(world, "tw.inst.0050")["values"]["flows"]
    assert (flows["prev_as_of"], flows["change"], flows["flag"]) == (start, 12_000_000, None)


def test_0050_official_start_from_the_collector_wins_over_the_calendar(world):
    put(world, "tw.flows.etf.0050", tw_flow_obs(official_prev_session="2026-10-14"))
    assert build(world, "tw.inst.0050")["values"]["flows"]["prev_as_of"] == "2026-10-14"


def test_t86_five_day_sums_report_how_many_days_are_in(world):
    """複審 R2-3-5：只算前 5 檔裡 5 天都齊的代號；某一檔缺天只跳過那一檔，第 6 檔不遞補；一檔都不齊才是「累積中」。"""
    put(world, "tw.inst.t86.0050", t86_obs(missing_day_for=("2330", "2317")))  # 前 5 檔裡有兩檔只有 4 天（例如停牌）
    sum5 = build(world, "tw.inst.0050")["values"]["t86"]["sum5"]
    assert sum5["days"] == 5 and [item["code"] for item in sum5["rows"]] == list(TOP_CODES[2:5])
    put(world, "tw.inst.t86.0050", t86_obs(missing_day_for=("2303",)))  # 第 10 檔缺一天：不影響前 5 檔
    sum5 = build(world, "tw.inst.0050")["values"]["t86"]["sum5"]
    assert sum5["days"] == 5 and [item["code"] for item in sum5["rows"]] == list(TOP_CODES[:5])
    put(world, "tw.inst.t86.0050", t86_obs(missing_day_for=TOP_CODES[:5]))  # 前 5 檔都缺一天：第 6 檔以後齊也不算
    sum5 = build(world, "tw.inst.0050")["values"]["t86"]["sum5"]
    assert (sum5["days"], sum5["rows"]) == (4, [])
    days = t86_obs()["days"]
    del days["2026-10-12"]  # 不足 5 天：一檔都不算
    put(world, "tw.inst.t86.0050", t86_obs(days=days))
    sum5 = build(world, "tw.inst.0050")["values"]["t86"]["sum5"]
    assert (sum5["days"], sum5["rows"]) == (4, [])
    del days["2026-10-13"]["2330"]  # 都不齊時 n＝最齊的那檔：obs 累積到幾天，不被某一檔自己缺的那天拖低
    put(world, "tw.inst.t86.0050", t86_obs(days=days))
    assert build(world, "tw.inst.0050")["values"]["t86"]["sum5"]["days"] == 4


def test_a_top_five_code_missing_today_is_skipped_and_the_sixth_does_not_step_in(world, md):
    """複審 R2-3-5 輸入 A：第 3 大的 2454 當天 T86 沒有。以前 2454 不見、第 6 大的 2891 悄悄頂上，標題還寫「前 5 檔」。"""
    payload = t86_obs()
    payload["rows"] = [row for row in payload["rows"] if row["code"] != "2454"]
    del payload["days"]["2026-10-16"]["2454"]
    put(world, "tw.inst.t86.0050", payload)
    t86 = build(world, "tw.inst.0050")["values"]["t86"]
    assert t86["rows"][2] == {"code": "2454", "foreign": None, "trust": None, "dealer": None}
    assert [item["code"] for item in t86["sum5"]["rows"]] == ["2330", "2317", "2308", "2382"] and t86["sum5"]["days"] == 5
    assert world.publish()["kind"] == "ok"
    lines = body_texts(md.handle(" 13F 0050", now=tpub.NOW, paths=world.paths).flex)
    head = lines.index("近 5 日合計（10/12–10/16，前 5 檔，張）")
    assert "2454 暫缺" in lines[:head]
    assert [line.split()[0] for line in lines[head + 1:]] == ["2330", "2317", "2308", "2382"]


def test_one_suspended_code_does_not_hide_the_others_on_the_card(world, md):
    """複審 R2-3-5 輸入 B：2454 只缺 5 天裡的一天（例如停牌）。以前整段「資料累積中（4/5）」，要等缺的那天滑出窗口。"""
    put(world, "tw.inst.t86.0050", t86_obs(missing_day_for=("2454",)))
    assert world.publish()["kind"] == "ok"
    lines = body_texts(md.handle(" 13F 0050", now=tpub.NOW, paths=world.paths).flex)
    assert not any("資料累積中" in line for line in lines)
    head = lines.index("近 5 日合計（10/12–10/16，前 5 檔，張）")
    assert [line.split()[0] for line in lines[head + 1:]] == ["2330", "2317", "2308", "2382"]


def test_t86_codes_missing_from_the_response_are_kept_as_missing(world):
    put(world, "tw.inst.t86.0050", t86_obs(codes=TOP_CODES[:9]))  # 2303 不在 T86
    rows = build(world, "tw.inst.0050")["values"]["t86"]["rows"]
    assert rows[-1] == {"code": "2303", "foreign": None, "trust": None, "dealer": None}


@pytest.mark.parametrize("changes", [{"v": 2}, {"unit": "張"}, {"trade_date": None}, {"rows": []}, {"rows": "x"}])
def test_bad_t86_payloads_leave_only_the_flows(world, changes):
    put(world, "tw.flows.etf.0050", tw_flow_obs())
    payload = t86_obs(**changes)
    world.store.put_obs("tw.inst.t86.0050", payload, data_date=None)
    row = build(world, "tw.inst.0050")
    assert row["state"] == "ok" and row["values"]["t86"] is None and row["values"]["flows"] is not None


def test_t86_needs_the_holdings_list_for_the_top_ten(world):
    put(world, "tw.inst.t86.0050", t86_obs())
    assert build(world, "tw.inst.0050")["state"] == "ok"  # 正對照：有成分名單時這一列有值
    put(world, "tw.holdings.0050", {"source": "moneydj", "as_of": "2026-10-16", "rows": []})
    assert build(world, "tw.inst.0050")["state"] == "never"


@pytest.mark.parametrize("now, behind", [
    (_tpe(2026, 10, 16, 15, 0), False),  # T86 還沒到 16:30：10/15 就是該有的最新一天
    (_tpe(2026, 10, 16, 16, 45), True), (tpub.NOW, True),
])
def test_t86_from_an_older_trade_date_is_behind_only_after_the_ready_time(world, now, behind):
    put(world, "tw.inst.t86.0050", t86_obs(trade_date="2026-10-15"))
    t86 = build(world, "tw.inst.0050", now)["values"]["t86"]
    assert t86["behind"] is behind and t86["stale_after"] == tw_close(date(2026, 10, 19))
    # 10/15 往前 5 個交易日（10/8、10/12–10/15）在合成的 days 裡少了 10/8：只有 4 天
    assert (t86["sum5"]["from"], t86["sum5"]["days"], t86["sum5"]["rows"]) == ("2026-10-08", 4, [])


@pytest.mark.parametrize("market, now, behind", [
    ("tw", _tpe(2026, 10, 16, 16, 45), False), ("tw", _tpe(2026, 10, 16, 17, 5), True),  # 元大 17:00
    ("us", _et(2026, 10, 16, 17, 0), False), ("us", _et(2026, 10, 16, 18, 30), True),  # iShares 美東 18:00
])
def test_flows_are_behind_only_after_the_ready_time(world, market, now, behind):
    if market == "tw":
        put(world, "tw.flows.etf.0050", tw_flow_obs(shares_out_as_of="2026-10-15", nav_as_of="2026-10-15",
                                                    prev_as_of="2026-10-14", official_prev_session="2026-10-14"))
        flows = build(world, "tw.inst.0050", now)["values"]["flows"]
    else:
        put(world, "us.flows.etf.SOXX", us_flow_obs(shares_out_as_of="2026-10-15", nav_as_of="2026-10-15",
                                                    prev_as_of="2026-10-14", shares_out=94_000_000,
                                                    prev_shares_out=93_900_000, change=100_000))
        flows = build(world, "us.inst.SOXX", now)["values"]["flows"]
    assert flows["as_of"] == "2026-10-15" and flows["behind"] is behind


def test_rows_without_any_section_are_never(world):
    put(world, "tw.inst.t86.0050", t86_obs())
    assert build(world, "tw.inst.0050")["state"] == "ok" and build(world, "us.inst.SOXX")["state"] == "ok"  # 正對照
    world.store._db().execute("DELETE FROM obs WHERE obs_id = ?", ("tw.inst.t86.0050",))
    assert build(world, "tw.inst.0050")["state"] == "never"
    world.store.put_obs("us.top10.SOXX", {"rows": []}, data_date="2026-10-16")  # 13F 也沒有
    assert build(world, "us.inst.SOXX")["state"] == "never"


def test_full_publish_writes_valid_inst_rows_and_the_bot_reads_them(world, md):
    put(world, "us.flows.etf.SOXX", us_flow_obs())
    put(world, "tw.flows.etf.0050", tw_flow_obs())
    put(world, "tw.inst.t86.0050", t86_obs())
    outcome = world.publish()
    assert outcome["kind"] == "ok" and "row_errors" not in outcome, outcome
    snap = world.snapshot()
    assert market_publish.validate_snapshot(snap) == []
    assert all(snap["rows"][row_id]["state"] == "ok" for row_id in INST_ROWS)
    soxx = md.handle(" 13F SOXX", now=tpub.NOW, paths=world.paths)
    assert body_texts(inst_bubble(soxx))[:5] == [
        "資金流（在外股數變化）", "10/15→10/16：+600,000 股", "約 +3.1 億美元（估算）", "近 5 日（10/9 起）：+1,200,000 股",
        "13F 新一季（9/30，截止 11/16）"]
    tw = body_texts(md.handle(" 13F 0050", now=tpub.NOW, paths=world.paths).flex)
    assert tw[1:3] == ["10/15→10/16：+12,000 張", "約 +22.2 億元（估算）"]
    assert "2330 外資 +1,234 投信 -56 自營 -1" in tw and "近 5 日合計（10/12–10/16，前 5 檔，張）" in tw


def test_validate_snapshot_uses_the_shared_checks():
    """型別由 row_ok 管；語意（日期先後、x≤N、前十大最多 10 檔）是 market_calc.inst_values_ok，和 bot 同一份。"""
    snap = {"schema": market_calc.SNAPSHOT_SCHEMA, "generated_at": NOW, "charts": {}, "progress": {},
            "rows": {row_id: _never() for row_id in market_calc.ROW_IDS}}
    good = inst_snapshot()["rows"]
    snap["rows"].update({row_id: good[row_id] for row_id in INST_ROWS})
    assert market_publish.validate_snapshot(snap) == []
    bad = copy.deepcopy(snap)
    bad["rows"]["us.inst.SOXX"]["values"]["f13"]["updated"] = 101
    bad["rows"]["tw.inst.0050"]["values"]["flows"]["prev_as_of"] = "2026-10-17"
    bad["rows"]["us.inst.QQQ"]["values"]["f13"]["deadline"] = "2026-09-30"  # 截止日不晚於季末
    assert market_publish.validate_snapshot(bad) == ["inst:us.inst.SOXX", "inst:us.inst.QQQ", "inst:tw.inst.0050"]
    long = copy.deepcopy(snap)
    long["rows"]["tw.inst.0050"]["values"]["t86"]["rows"] = t86_items() * 2
    assert market_publish.validate_snapshot(long) == ["inst:tw.inst.0050"]
    assert not hasattr(market_publish, "_inst_values_ok")  # 沒有第二份


# ── 卡片 ────────────────────────────────────────────────────────────────────
def test_soxx_command_sends_the_short_card_then_the_original_13f_card(md, paths):
    reply = render(md, paths, inst_snapshot(), " 13F SOXX")
    assert reply.flex["type"] == "carousel" and len(reply.flex["contents"]) == 2 and reply.render_errors == ()
    first, second = reply.flex["contents"]
    assert texts(first["header"]) == ["機構動向｜半導體（SOXX）", "往左滑看機構持股（13F）"]
    assert body_texts(first) == SOXX_LINES
    assert texts(first["footer"]) == ["註：申購贖回含全體投資人，不等於機構買賣", DISCLAIMER]
    assert not actions(first)
    assert reply.alt_text == "盤勢：機構動向（SOXX）｜資料 10/16"  # 對上第一張卡（Phase 6）
    # 第二張就是原本的 13F 卡（旗標在時的單張回覆，一個字都沒變）
    set_flag(paths)
    original = md.handle(" 13F SOXX", now=NOW, paths=paths)
    assert original.flex["type"] == "bubble" and second == original.flex
    assert original.alt_text == "盤勢：13F 前十大（SOXX）｜3 檔的加碼機構多於減碼機構"


@pytest.mark.parametrize("sym, name", [("QQQ", "那斯達克100（QQQ）"), ("VOO", "標普500（VOO）")])
def test_qqq_and_voo_say_flows_are_not_provided(md, paths, sym, name):
    reply = render(md, paths, inst_snapshot(), f" 13F {sym}")
    first = inst_bubble(reply)
    assert texts(first["header"])[0] == f"機構動向｜{name}"
    assert body_texts(first) == ["資金流：暫不提供", *F13_LINES]  # 中性：不斷言來源有沒有（R3-8）
    assert texts(first["footer"]) == [DISCLAIMER]  # 沒有資金流就不放申購贖回的註記
    assert reply.alt_text == f"盤勢：機構動向（{sym}）｜13F 新一季 9/30"


def test_0050_command_sends_a_shorter_0050_card(md, paths):
    reply = render(md, paths, inst_snapshot(), " 13F 0050")
    bubble = reply.flex
    assert bubble["type"] == "bubble" and reply.render_errors == ()
    assert texts(bubble["header"]) == ["機構動向｜台灣50（0050）"]
    lines = body_texts(bubble)
    assert lines == [*TW_FLOW_LINES, "前十大法人（10/16，張）", *T86_DAY_LINES, *T86_SUM_LINES]
    assert len(lines) == 21  # 以前 26 行：5 日合計只列前 5 檔、外資與投信
    assert texts(bubble["footer"]) == ["註：申購贖回含全體投資人，不等於機構買賣", "外資不含外資自營商；自營＝自行買賣＋避險",
                                       DISCLAIMER, "回台灣50細節"]
    assert [(a["label"], a["text"]) for a in actions(bubble)] == [("回台灣50細節", "/盤勢 0050")]
    assert reply.alt_text == "盤勢：機構動向（0050）｜資料 10/16"


def test_etf_and_0050_detail_buttons(md, paths):
    snap = inst_snapshot()
    voo = render(md, paths, snap, " VOO").flex
    assert ("機構動向", "/盤勢 13F VOO") in [(a["label"], a["text"]) for a in actions(voo)]
    tw = render(md, paths, snap, " 0050").flex
    assert [(a["label"], a["text"]) for a in actions(tw)] == [
        ("台指期權圖", "/盤勢 台指 圖"), ("期權與資金細節", "/盤勢 期權"), ("機構動向", "/盤勢 13F 0050")]


def test_the_flag_file_brings_back_the_old_replies(md, paths):
    snap = inst_snapshot()
    set_flag(paths)
    assert render(md, paths, snap, " 13F QQQ").flex["type"] == "bubble"
    assert md.handle(" 13F 0050", now=NOW, paths=paths).text == md.TEXT_NO_13F_0050
    voo = md.handle(" VOO", now=NOW, paths=paths).flex
    assert ("機構持股（13F）", "/盤勢 13F VOO") in [(a["label"], a["text"]) for a in actions(voo)]
    tw = md.handle(" 0050", now=NOW, paths=paths).flex
    assert [a["text"] for a in actions(tw)] == ["/盤勢 台指 圖", "/盤勢 期權"]
    os.unlink(paths.state_dir / "market_inst.disabled")  # 每次都看
    assert md.handle(" 13F QQQ", now=NOW, paths=paths).flex["type"] == "carousel"


def test_a_flag_that_cannot_be_checked_counts_as_disabled(md, paths, monkeypatch):
    write_snapshot(paths, inst_snapshot())
    real = md.market_paths.flag_set

    def broken(path):
        if path.name == "market_inst.disabled":  # 只有機構動向的旗標看不了；/盤勢 本身的旗標照常
            raise PermissionError("no")
        return real(path)

    monkeypatch.setattr(md.market_paths, "flag_set", broken)
    assert md.handle(" 13F SOXX", now=NOW, paths=paths).flex["type"] == "bubble"
    assert md.handle(" 13F 0050", now=NOW, paths=paths).text == md.TEXT_NO_13F_0050


def test_an_old_snapshot_without_the_new_rows_replies_as_before(md, paths):
    """job 還沒寫新列的空窗：13F 回原卡、0050 回原句，按鈕字與第 3 顆按鈕也照舊（Phase 6 R1-5、R3-7）。"""
    reply = render(md, paths, old_snapshot(), " 13F SOXX")
    assert reply.flex["type"] == "bubble" and reply.render_errors == ()
    assert md.handle(" 13F 0050", now=NOW, paths=paths).text == md.TEXT_NO_13F_0050
    voo = md.handle(" VOO", now=NOW, paths=paths).flex
    assert ("機構持股（13F）", "/盤勢 13F VOO") in [(a["label"], a["text"]) for a in actions(voo)]
    tw = md.handle(" 0050", now=NOW, paths=paths).flex
    assert [a["text"] for a in actions(tw)] == ["/盤勢 台指 圖", "/盤勢 期權"]
    os.unlink(paths.snapshot)
    md.clear_cache()
    assert md.handle(" 13F 0050", now=NOW, paths=paths).text == md.TEXT_NO_13F_0050  # 還沒有快照：照舊


def test_never_and_stale_rows(md, paths):
    snap = inst_snapshot()
    snap["rows"]["us.inst.SOXX"] = _never()
    assert soxx_body(md, paths, snap) == ["資金流（在外股數變化）", "暫缺", "13F 新一季：暫缺"]
    # 整列過期：每一段印自己的資料日
    snap = inst_snapshot(soxx_flows=flows_section(stale_after=NOW - 1), soxx_f13=f13_section(stale_after=NOW - 1),
                         t86=t86_section(stale_after=NOW - 1), tw_flows=tw_flows_section(stale_after=NOW - 1))
    snap["rows"]["us.inst.SOXX"]["stale_after"] = NOW - 1
    snap["rows"]["tw.inst.0050"]["stale_after"] = NOW - 1
    assert soxx_body(md, paths, snap) == ["資金流（在外股數變化）", "過期（資料日 10/16）", "13F 新一季：過期"]
    assert tw_body(md, paths, snap) == [TW_HEAD, "過期（資料日 10/16）", "前十大法人：過期（資料日 10/16）"]


def test_one_stale_section_does_not_hide_the_other(md, paths):
    snap = inst_snapshot(soxx_flows=flows_section(stale_after=NOW - 1))
    assert soxx_body(md, paths, snap) == ["資金流（在外股數變化）", "過期（資料日 10/16）", *F13_LINES]
    snap = inst_snapshot(soxx_f13=f13_section(stale_after=NOW - 1))  # 反過來：13F 過期、資金流新鮮
    assert soxx_body(md, paths, snap) == [*SOXX_LINES[:4], "13F 新一季：過期"]
    assert render(md, paths, snap, " 13F SOXX").alt_text == "盤勢：機構動向（SOXX）｜資料 10/16"


def test_check_and_behind(md, paths):
    snap = inst_snapshot(soxx_flows=flows_section(flag="check"), tw_flows=tw_flows_section(flag="check"))
    assert soxx_body(md, paths, snap) == ["資金流（在外股數變化）", "資料待核對", *F13_LINES]  # 待核對：不印金額與股數
    assert tw_body(md, paths, snap)[:3] == [TW_HEAD, "資料待核對", "前十大法人（10/16，張）"]
    # 落後（Phase 6 I-5）：在那一段的日期行加註，不另起一行、不重印日期
    snap = inst_snapshot(soxx_flows=flows_section(behind=True), t86=t86_section(behind=True))
    assert soxx_body(md, paths, snap)[:4] == ["資金流（在外股數變化）", "10/15→10/16（最近一筆）：+600,000 股",
                                              "約 +3.1 億美元（估算）", "近 5 日（10/9 起）：+1,200,000 股"]
    tw = tw_body(md, paths, snap)
    assert tw[4:6] == ["前十大法人（10/16，最近一筆，張）", T86_DAY_LINES[0]]
    for lines in (soxx_body(md, paths, snap), tw):
        assert not any("最近資料日" in line for line in lines)
        assert all(line.count("10/16") <= 1 for line in lines)


def test_a_first_soxx_reading_says_when_recording_started(md, paths):
    snap = inst_snapshot(soxx_flows=flows_section(prev_as_of=None, change=None, flow=None, change_5d=None))
    assert soxx_body(md, paths, snap) == ["資金流（在外股數變化）", "10/16 起開始記錄", *F13_LINES]
    snap = inst_snapshot(soxx_flows=flows_section(prev_as_of=None, change=None, flow=None, change_5d=None, behind=True))
    assert soxx_body(md, paths, snap)[1] == "10/16（最近一筆）起開始記錄"
    for snap in (inst_snapshot(), worst_inst_snapshot()):
        for body in (" 13F SOXX", " 13F 0050"):
            assert not any("前一份資料暫缺" in s for s in reply_strings(render(md, paths, snap, body)))


def test_no_amount_line_when_the_nav_is_from_another_day_or_nothing_changed(md, paths):
    snap = inst_snapshot(soxx_flows=flows_section(flow=None))
    assert soxx_body(md, paths, snap)[1:3] == ["10/15→10/16：+600,000 股", "近 5 日（10/9 起）：+1,200,000 股"]
    snap = inst_snapshot(soxx_flows=flows_section(change=0, flow=0.0, change_5d=None))
    assert soxx_body(md, paths, snap)[1:3] == ["10/15→10/16：0 股", F13_LINES[0]]
    snap = inst_snapshot(soxx_flows=flows_section(change_5d=None))  # 近 5 日是 null 就不印（I-1）
    assert not any(line.startswith("近 5 日") for line in soxx_body(md, paths, snap))


@pytest.mark.parametrize("flow, expected", [
    (307_404_000.0, "約 +3.1 億美元（估算）"), (-250_000_000.0, "約 -2.5 億美元（估算）"),
    (145_000_000.0, "約 +1.5 億美元（估算）"),  # 平手往遠離 0 進位
    (150_000_000_000.0, "約 +1,500 億美元（估算）"), (99_950_000.0, "約 +9,995 萬美元（估算）"),
    (99_995_000.0, "約 +1.0 億美元（估算）"),  # 進位到 1 萬萬：改用億
    (-30_000.0, "約 -3 萬美元（估算）"),
    (4_999.0, "淨流入不到 1 萬美元（估算）"), (-4_000.0, "淨流出不到 1 萬美元（估算）"),  # 帶方向
    (1.2e12, "約 +1.2 兆美元（估算）"), (999_996_000_000.0, "約 +1.0 兆美元（估算）"),  # 9,999.96 億進位到 1 萬億：改用兆
    (-9.99e14, "約 -999.0 兆美元（估算）"), (1e15, "約 +1,000 兆美元（估算）"),
])
def test_amounts_are_rounded_half_up_into_zhao_yi_or_wan(md, paths, flow, expected):
    assert soxx_body(md, paths, inst_snapshot(soxx_flows=flows_section(flow=flow)))[2] == expected


@pytest.mark.parametrize("units, expected", [
    (12_000_000, "+12,000 張"), (-1_500, "-2 張"), (499, "+不到 1 張"), (-500, "-1 張"), (0, "0 張"),
])
def test_0050_units_are_shown_in_lots(md, paths, units, expected):
    snap = inst_snapshot(tw_flows=tw_flows_section(change=units, flow=units * 185.0 if units else 0.0, change_5d=None))
    assert tw_body(md, paths, snap)[1] == f"10/15→10/16：{expected}"


@pytest.mark.parametrize("shares, expected", [
    (1_234_000, "+1,234"), (-56_000, "-56"), (1_500, "+2"), (-500, "-1"), (499, "+不到 1 張"), (-1, "-不到 1 張"),
    (0, "0"), (-1_499, "-1"),
])
def test_shares_become_lots_with_half_up_rounding(md, paths, shares, expected):
    rows = [dict(item, trust=shares) for item in t86_items()]
    lines = tw_body(md, paths, inst_snapshot(t86=t86_section(rows=rows, sum5=None)))
    assert lines[5] == f"2330 外資 +1,234 投信 {expected} 自營 -1"


def test_a_wide_t86_line_splits_into_two_rows(md, paths):
    rows = [dict(item, foreign=-123_456_789_000, trust=123_456_789_000, dealer=-1_234_000) for item in t86_items()]
    rows[1] = dict(rows[1], dealer=-1_000)  # 這檔一列放得下：不分
    lines = tw_body(md, paths, inst_snapshot(t86=t86_section(rows=rows, sum5=None)))
    assert lines[5:8] == ["2330 外資 -123,456,789 投信 +123,456,789", "2330 自營 -1,234",
                          "2317 外資 -123,456,789 投信 +123,456,789 自營 -1"]
    assert md.row_width("2330 外資 -123,456,789 投信 +123,456,789 自營 -1,234") > 24  # 一列放不下才分
    assert md.row_width(lines[7]) == 24


def test_five_day_sums_only_when_complete(md, paths):
    lines = tw_body(md, paths, inst_snapshot(t86=t86_section(sum5=None)))  # 日曆說不出 5 天：整段不印
    assert not any(line.startswith("近 5 日合計") for line in lines) and len(lines) == 15
    sum5 = {"from": "2026-10-12", "to": "2026-10-16", "days": 3, "rows": []}
    lines = tw_body(md, paths, inst_snapshot(t86=t86_section(sum5=sum5)))
    assert lines[-1] == "近 5 日合計：資料累積中（3/5）" and len(lines) == 16
    rows = t86_items()
    rows[3] = {"code": "2308", "foreign": None, "trust": None, "dealer": None}
    sum5 = {"from": "2026-10-12", "to": "2026-10-16", "days": 5, "rows": [r for r in t86_items(5) if r["code"] != "2308"]}
    lines = tw_body(md, paths, inst_snapshot(t86=t86_section(rows=rows, sum5=sum5)))
    # 缺的那檔跳過，第 6 檔不遞補（複審 R2-3-5；快照裡有第 6 檔也一樣：bot 用權重前 5 檔擋一層）
    assert "2308 暫缺" in lines and lines[-5:] == [T86_SUM_LINES[i] for i in (0, 1, 2, 3, 5)]
    sum5 = {"from": "2026-10-12", "to": "2026-10-16", "days": 5, "rows": t86_items(5)[5:]}  # 只有第 6 檔以後：不留空標題
    lines = tw_body(md, paths, inst_snapshot(t86=t86_section(sum5=sum5)))
    assert not any(line.startswith("近 5 日合計") for line in lines) and len(lines) == 15


def test_status_summary_carries_the_inst_flows_through_the_whitelist(md, paths):
    """複審 R2-3-1：每日維護只讀 status_summary；inst_flows 以前只在 outcome 裡。只放日期、整數、布林與 check；固定四項、固定鍵，
    舊版 job、inst_flows 是 null、壞值都落在 None（和 calendar_days_left 一樣：少一個鍵的話「或是 null」永遠不會觸發）。"""
    base = {"status": "ok", "code": "verified", "collect": {"status": "ok"}, "publish": {"status": "ok"}}
    flow = {"data_date": "2026-10-20", "lag_sessions": 5, "nav_as_of": "2026-10-16", "fail_streak": 15, "flag": "check",
            "last_success_at": NOW - 3600.0, "enabled": True}
    t86 = {"data_date": "2026-10-16", "lag_sessions": 0, "fail_streak": 2, "last_success_at": NOW - 7200.0, "enabled": False}

    def summary(**extra):
        market_paths.write_private_atomic(paths.outcome, json.dumps({**base, **extra}).encode())
        return md.status_summary(now=NOW, paths=paths)["outcome"]

    got = summary(inst_flows={"SOXX": flow, "SPY": dict(flow, flag=None, fail_streak=0), "0050": flow, "T86": t86})
    assert got["inst_flows"] == {"SOXX": flow, "SPY": dict(flow, flag=None, fail_streak=0), "0050": flow, "T86": t86}
    assert got["inst_status_error"] is None
    empty_flow = dict.fromkeys(flow)
    empty_t86 = dict.fromkeys(t86)
    empty = {"SOXX": empty_flow, "SPY": empty_flow, "0050": empty_flow, "T86": empty_t86}
    assert summary()["inst_flows"] == empty  # 舊版 job：沒有這個鍵
    got = summary(inst_flows=None, inst_status_error="unexpected:RuntimeError")  # job 算不出來
    assert got["inst_flows"] == empty and got["inst_status_error"] == "unexpected:RuntimeError"
    bad = {"data_date": "2026-02-30", "lag_sessions": -1, "nav_as_of": "٢٠٢٦-١٠-١٦", "fail_streak": "15", "flag": "secret flag",
           "last_success_at": True, "enabled": "yes", "error": "secret body"}
    got = summary(inst_flows={"SOXX": bad, "SPY": "secret text", "0050": ["secret"], "T86": dict(bad), "EVIL": flow},
                  inst_status_error="free text with spaces")
    assert got["inst_flows"] == empty and got["inst_status_error"] is None
    assert "secret" not in json.dumps(got, ensure_ascii=False) and "free text" not in json.dumps(got, ensure_ascii=False)


def test_13f_deadline_unknown_and_nobody_updated_yet(md, paths):
    snap = inst_snapshot(soxx_f13=f13_section(quarter_end="2027-12-31", deadline=None, updated=0, total=98))
    lines = soxx_body(md, paths, snap)
    assert lines[4:6] == ["13F 新一季（12/31，截止日未知）", "成分股前十大持有人：0/98 筆已是 12/31 季報"]


JUNK = ("x", -1, 10**20, 1e308, True, None, [], {}, [1, None, "x"], {"a": {"b": "c"}})


def _leaves(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, (*path, key))
    elif isinstance(value, list) and value:  # 清單只換第一筆（每一筆的形狀都一樣；全換太慢）
        yield from _leaves(value[0], (*path, 0))
    else:
        yield path


@pytest.mark.parametrize("junk", JUNK, ids=[repr(j)[:12] for j in JUNK])
def test_junk_in_any_inst_value_never_crashes_and_passes_the_gate(md, paths, junk):
    base = inst_snapshot()
    for row_id in INST_ROWS:
        for path in list(_leaves(base["rows"][row_id]["values"])):
            snap = copy.deepcopy(base)
            target = snap["rows"][row_id]["values"]
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = junk
            for body in (" 13F SOXX", " 13F QQQ", " 13F 0050"):
                reply = render(md, paths, snap, body)
                assert reply.flex is not None and reply.render_errors == (), (row_id, path, body)
                assert_main_gate(reply)


def test_broken_sections_show_missing(md, paths):
    snap = inst_snapshot(soxx_flows={"as_of": "2026-10-16"}, soxx_f13=f13_section(deadline="2026-09-30"), t86="x",
                         tw_flows=tw_flows_section(change=10**14))
    assert soxx_body(md, paths, snap) == ["資金流（在外股數變化）", "暫缺", "13F 新一季：暫缺"]
    assert tw_body(md, paths, snap) == [TW_HEAD, "暫缺", "前十大法人：暫缺"]
    # 以前 bot 會把「沒有起點卻有變化」改成 null 照印，publish 卻判整列壞；現在兩邊同一份檢查（I-6）
    snap = inst_snapshot(soxx_flows=flows_section(prev_as_of=None))
    assert soxx_body(md, paths, snap)[:2] == ["資金流（在外股數變化）", "暫缺"]


def test_a_crashing_inst_builder_only_replaces_the_short_card(md, paths, monkeypatch):
    write_snapshot(paths, inst_snapshot())

    def boom(*_args, **_kwargs):
        raise ZeroDivisionError("secret")

    monkeypatch.setattr(md, "_inst_flow_rows", boom)
    reply = md.handle(" 13F SOXX", now=NOW, paths=paths)
    first, second = reply.flex["contents"]
    assert texts(first) == ["機構動向｜半導體（SOXX）", "往左滑看機構持股（13F）", md.TEXT_BUBBLE_FAILED, DISCLAIMER]
    assert texts(second["header"])[0] == "機構持股（13F）｜半導體（SOXX）前十大"
    assert reply.render_errors == ("_inst_us_bubble:ZeroDivisionError",)
    tw = md.handle(" 13F 0050", now=NOW, paths=paths)
    assert md.TEXT_BUBBLE_FAILED in texts(tw.flex) and tw.render_errors == ("_inst_tw_bubble:ZeroDivisionError",)
    summary = md.status_summary(now=NOW, paths=paths)
    assert summary["render_error"] == "bubble:_inst_us_bubble:ZeroDivisionError"  # 每日維護看得到
    assert "secret" not in json.dumps(summary, ensure_ascii=False)


# ── 寬度、大小、validator、禁用字 ─────────────────────────────────────────────
def _inst_texts(reply) -> list[str]:
    """機構動向那張的每一個字（不含免責句；按鈕字也算）。"""
    return [t for t in texts(inst_bubble(reply)) if t != DISCLAIMER]


@pytest.mark.parametrize("make", [inst_snapshot, worst_inst_snapshot], ids=["normal", "worst"])
def test_every_row_fits_and_every_card_fits_line_limits(md, paths, make):
    snap = make()
    for body in (" 13F SOXX", " 13F QQQ", " 13F VOO", " 13F 0050"):
        reply = render(md, paths, snap, body)
        assert reply.flex is not None and reply.render_errors == (), body
        for text in _inst_texts(reply):
            assert md.row_width(text) <= 24, (body, text, md.row_width(text))
        assert md.card_bytes(inst_bubble(reply)) <= md.SUMMARY_BUBBLE_MAX_BYTES, (body, md.card_bytes(inst_bubble(reply)))
        assert size(reply.flex) <= md.CARD_MAX_BYTES == 28 * 1024, body
        assert card_pages.fits_line_limits(card_pages.with_page_button(reply.flex, LONG_PAGE_URL)), body
        assert_main_gate(reply)


def test_worst_values_render_whole_numbers(md, paths):
    snap = worst_inst_snapshot()
    assert soxx_body(md, paths, snap) == [
        "資金流（在外股數變化）", "12/24→12/31（最近一筆）：-9,999,999,999,999 股", "約 -999.0 兆美元（估算）",
        "近 5 日（12/17 起）：-9,999,999,999,999 股", "13F 新一季（12/31，截止 2/16）",
        "成分股前十大持有人：500/500 筆已是 12/31 季報", "（Nasdaq 資料，名單會變動）"]
    tw = tw_body(md, paths, snap)
    assert tw[:4] == [TW_HEAD, "12/24→12/31（最近一筆）：-10,000,000,000 張", "約 -999.0 兆元（估算）",
                      "近 5 日（12/17 起）：-10,000,000,000 張"]
    assert tw[4:7] == ["前十大法人（12/31，最近一筆，張）", "2330AB 外資 -10,000,000,000 投信 +10,000,000,000",
                       "2330AB 自營 -10,000,000,000"]
    assert tw[25:27] == ["近 5 日合計（12/24–12/31，前 5 檔，張）", "2330AB 外資 -10,000,000,000 投信 +10,000,000,000"]
    assert len(tw) == 31 and md.row_width(tw[5]) == 24


@pytest.mark.parametrize("now", ALL_NOWS, ids=["2026-10-17", "2027-01-15", "2028-01-03"])
def test_every_string_passes_the_validator_in_later_years(md, paths, now):
    at = datetime.fromtimestamp(now, TPE)
    for make in (inst_snapshot, worst_inst_snapshot):
        snap = make(now)
        for body in (" 13F SOXX", " 13F QQQ", " 13F VOO", " 13F 0050", " VOO", " 0050"):
            reply = render(md, paths, snap, body, now=now)
            for value in reply_strings(reply):
                result = output_validator.validate_outbound_text(value, now=at)
                assert result.ok and result.text == value, (body, value, result.reason)
                assert not md.has_forbidden(value) or value == DISCLAIMER, (body, value)


def test_no_source_strings_reach_the_card(md, paths):
    snap = inst_snapshot()
    for row_id in INST_ROWS:
        snap["rows"][row_id]["source"] = "iShares https://www.example.com"
        snap["rows"][row_id]["basis"] = "元大 @all"
    for body in (" 13F SOXX", " 13F 0050"):
        for value in reply_strings(render(md, paths, snap, body)):
            assert "iShares" not in value and "example" not in value and "元大" not in value and "@" not in value


# ── 指令與按鈕 ──────────────────────────────────────────────────────────────
def test_the_new_button_command_is_shipped_and_routes(md):
    assert "/盤勢 13F 0050" in md.SHIPPED_MARKET_TEXTS
    assert md.parse_command(" 13F 0050") == (md.NO_13F, "0050")
    tree = ast.parse((HERE / "test_market_routing.py").read_text(encoding="utf-8"))
    shipped = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                   and any(getattr(t, "id", None) == "SHIPPED_MARKET_TEXTS" for t in node.targets))
    assert "/盤勢 13F 0050" in shipped and set(shipped) <= set(md.SHIPPED_MARKET_TEXTS)  # 兩份都只增不刪


def test_every_button_on_the_inst_cards_is_shipped(md, paths):
    snap = inst_snapshot()
    for body in (" 13F SOXX", " 13F QQQ", " 13F VOO", " 13F 0050", " VOO", " QQQ", " SOXX", " 0050"):
        for action in actions(render(md, paths, snap, body).flex):
            assert action["text"] in md.SHIPPED_MARKET_TEXTS and len(action["label"]) <= 20, (body, action)
    for bubble in bubbles(render(md, paths, snap, " 0050").flex):
        assert sum(1 for n in bubble["footer"]["contents"] if "action" in n) <= md.MAX_BUTTONS


def test_status_summary_renders_the_new_cards_without_errors(md, paths):
    write_snapshot(paths, inst_snapshot())
    summary = md.status_summary(now=NOW, paths=paths)
    assert "render_error" not in summary and summary["rows_unexpected"] == [] and summary["rows_missing"] == []


@pytest.mark.parametrize("module", ["market_calc.py", "market_publish.py", "market_dashboard.py"])
def test_no_top_level_name_is_defined_twice(module):
    """2026-10-31：新函式曾經和既有的 _inst_part（三大法人那一格）同名、把它蓋掉，台股卡整張出錯；合併其他批次時也防這個。"""
    tree = ast.parse((HERE / module).read_text(encoding="utf-8"))
    names = [node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))]
    assert sorted({name for name in names if names.count(name) > 1}) == []
