"""川普壓力指數（TACO 指數，MacroMargin，2026-10-24）在 /盤勢 卡片上的樣子（market_dashboard）。

Andrew 要卡上有數字和歷史位階（「這樣我查知道目前是在哪個狀況，會不會TACO」）。MM 的指數程式抓不到，改用 MacroMargin 的公開頁：
- 宏觀與事件摘要卡（輪播最後一張）的頁尾那一行，取代「MM 川普壓力指數：抓不到（要登入）」；
- 新聞與宏觀細節卡的第三張（宏觀與事件 3/3）頁尾放完整說明。
卡上只有固定句＋程式算的數字與日期；來源網站的任何文字（簡體標籤、方法說明）不在快照裡、也不會出現在卡上。
快照是合成的（欄位照 market_calc.TACO_SPEC）；數字用保存的真實回應算出來的值（test_market_taco_publish）。
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime

import pytest

import card_pages
import market_calc
import output_validator
from test_market_dashboard import (  # noqa: F401  (md、paths 是 fixture)
    ALL_NOWS, DISCLAIMER, LONG_PAGE_URL, NOW, TPE, _never, _row, assert_main_gate, make_snapshot, md, nodes, paths,
    render_all, size, texts, worst_snapshot, write_snapshot,
)
from test_market_taco_fixture import DECOYS, SIMPLIFIED_ONLY

# 卡上固定的來源句（「來源：MacroMargin（ocmacro.com）…」）本來就有網站名，所以網站名不在卡片層的誘餌裡
CARD_DECOYS = tuple(decoy for decoy in DECOYS if decoy != "ocmacro")

GOOD = {"value": 9.0005, "chg20": -3.6, "percentile": 88, "n": 394, "since": "2025-03-26", "max": 17.3261,
        "max_date": "2026-03-27", "min": -4.9868, "min_date": "2025-05-20", "status_key": "high", "method": "v1"}
LINE = "川普壓力指數 +9.0（高壓，任期內第 88 百分位）"
POSITION = "位階：2025-03-26 起 394 個交易日中第 88 百分位（最高 +17.3，2026-03-27；最低 -5.0，2025-05-20）"  # F2-2：交易日筆數，不是日曆天
CHANGE = "20 個交易日變化：-3.6"
MEANING = "川普壓力指數越高代表壓力越大；數值和百分位都不是退讓的機率"  # 第 2 輪複審 F3-3：寫出主詞、涵蓋百分位
SOURCE = "來源：MacroMargin（ocmacro.com），不是財經M平方的 MM 指數"
COMPOSITION = "組成：淨支持率 20%、10 年美債 25%、MOVE 5%、標普500 22.5%、VIX 5%、CPI 即時預測 22.5%"
METHOD_CHANGED = "方法版本已更新，組成請看來源網站"


def taco(**changes) -> dict:
    values = {**GOOD, **changes}
    # 最高、最低跟著現值走（現值不能在它們之外）
    values["max"] = max(values["max"], values["value"])
    values["min"] = min(values["min"], values["value"])
    return values


def snapshot_with(values=None, *, now=NOW, row=None):
    """合成快照；macro.TACO 那一列：values（新鮮）或自己給整列。values=None 且 row=None → 沒有這一列（舊快照）。"""
    snap = make_snapshot(now)
    day = date.fromisoformat(snap["rows"]["oneliner.us"]["data_date"])
    if row is not None:
        snap["rows"]["macro.TACO"] = row
    elif values is None:
        del snap["rows"]["macro.TACO"]
    else:
        snap["rows"]["macro.TACO"] = _row(values, now, day)
    return snap


def render(md, paths, snap, body="", *, now=NOW):
    write_snapshot(paths, snap)
    return md.handle(body, now=now, paths=paths)


def summary_footer(md, paths, snap, *, now=NOW) -> list[str]:
    return texts(macro_bubble(md, render(md, paths, snap, now=now).flex)["footer"])


def detail_footer(md, paths, snap, *, now=NOW) -> list[str]:
    return texts(render(md, paths, snap, " 新聞", now=now).flex["contents"][2]["footer"])


def macro_bubble(md_, flex):
    """摘要輪播裡「宏觀與事件」那一張：前面多了「重點摘要」（第 9 輪 A1），用 SUMMARY_CARDS 的卡名找，不寫死位置。"""
    names = [name for name, _builder in md_.SUMMARY_CARDS]
    return flex["contents"][names.index("宏觀與事件")]


def notes_of(footer: list[str]) -> list[str]:
    """頁尾裡免責句之前的註記。"""
    return footer[: footer.index(DISCLAIMER)]


def strings_of(reply) -> list[str]:
    found = [reply.text, reply.alt_text, reply.image_alt, reply.fallback_text]
    if reply.flex is not None:
        found += texts(reply.flex)
        found += [a for node in nodes(reply.flex) if isinstance(node.get("action"), dict) for a in node["action"].values()]
    return [s for s in found if isinstance(s, str)]


def data_day_line(snap: dict) -> str:
    """細節卡頁尾的『資料日 M/D』（2026-10-28 批 1 審查 F2-1）：這一筆川普壓力指數是哪一天的。"""
    day = date.fromisoformat(snap["rows"]["macro.TACO"]["data_date"])
    return f"資料日 {day.month}/{day.day}"


def signed(value: float) -> str:
    rounded = round(value, 1)
    return "0.0" if rounded == 0 else f"{rounded:+.1f}"


# ── 摘要卡的頁尾那一行 ───────────────────────────────────────────────────────────


def test_the_summary_footer_has_the_number_status_and_percentile(md, paths):
    for now in ALL_NOWS:
        footer = summary_footer(md, paths, snapshot_with(GOOD, now=now), now=now)
        assert notes_of(footer) == [LINE, MEANING], (now, footer)  # F2-2：宏觀與事件的頁尾也有「不是退讓的機率」
        assert md.row_width(LINE) == 22.5


def test_the_old_mm_sentence_is_gone_everywhere(md, paths):
    write_snapshot(paths, snapshot_with(GOOD))
    for body, reply in render_all(md, paths, NOW).items():
        assert not any("MM 川普壓力指數" in s or "抓不到" in s or "要登入" in s for s in strings_of(reply)), body


def test_the_line_is_in_the_footer_not_in_the_six_body_rows(md, paths):
    card = render(md, paths, snapshot_with(GOOD)).flex
    macro = macro_bubble(md, card)
    assert LINE in texts(macro["footer"])
    assert len(macro["body"]["contents"]) <= 6
    assert not any("川普壓力指數" in t for t in texts(macro["body"]))
    # 重點摘要（第一張）第 6 列本來就放這一句（2026-10-28 整合 A1×TACO）；其他卡都不能有
    others = [bubble for bubble in card["contents"]
              if bubble is not macro and not texts(bubble["header"])[0].startswith("重點摘要")]
    assert not any("川普壓力指數" in t for bubble in others for t in texts(bubble))


@pytest.mark.parametrize("value", [-100.0, -12.3, -5.0, -0.04, 0.0, 0.04, 9.0005, 17.3261, 99.96, 100.0])
@pytest.mark.parametrize("percentile", [0, 1, 7, 88, 99, 100])
def test_every_worst_case_value_fits_in_24_wide_with_the_status_word(md, paths, value, percentile):
    values = taco(value=value, percentile=percentile, chg20=-200.0)
    expected = f"川普壓力指數 {signed(value)}（高壓，任期內第 {percentile} 百分位）"
    for now in ALL_NOWS:
        footer = summary_footer(md, paths, snapshot_with(values, now=now), now=now)
        line, meaning = notes_of(footer)
        assert meaning == MEANING
        assert line == expected and md.row_width(line) <= 24, (value, percentile, md.row_width(line))
        now_dt = datetime.fromtimestamp(now, TPE)
        checked = output_validator.validate_outbound_text(line, now=now_dt)
        assert checked.ok and checked.text == line and not md.has_forbidden(line)


@pytest.mark.parametrize("value, text", [(-100.0, "-100.0"), (100.0, "+100.0"), (99.96, "+100.0")])
def test_the_widest_possible_line_is_exactly_24(md, paths, value, text):
    footer = summary_footer(md, paths, snapshot_with(taco(value=value, percentile=100)))
    assert notes_of(footer) == [f"川普壓力指數 {text}（高壓，任期內第 100 百分位）", MEANING]
    assert md.row_width(notes_of(footer)[0]) == 24.0


def test_longer_variants_give_way_in_this_order_when_the_row_is_too_narrow(md, paths, monkeypatch):
    write_snapshot(paths, snapshot_with(GOOD))
    ctx = md._Ctx(md._load_snapshot(paths), NOW)
    variants = [
        (24.0, LINE),  # 帶狀態詞
        (22.0, "川普壓力指數 +9.0（任期內第 88 百分位）"),  # 拿掉狀態詞
        (19.0, "川普壓力 +9.0｜第 88 百分位"),  # 短寫法
        (13.0, "川普壓力指數 +9.0"),  # 只有數字
    ]
    for width, expected in variants:
        monkeypatch.setattr(md, "MAX_ROW_WIDTH", width)
        assert md._taco_line(ctx) == expected, width
        assert md.row_width(expected) <= width
    # 都放不下時 _fit 的最後保險是既有的 _clip（超寬就從尾巴切掉、補「…」、數字不留半個），TACO 這一行沒有另外的做法
    monkeypatch.setattr(md, "MAX_ROW_WIDTH", 8.0)
    assert md._taco_line(ctx) == md._fit("川普壓力指數 +9.0")


@pytest.mark.parametrize("key", [None, "mid", "low", "extreme", "very_high", "high_"])
def test_unknown_status_keys_get_no_status_word(md, paths, key):
    footer = summary_footer(md, paths, snapshot_with(taco(status_key=key)))
    assert notes_of(footer) == ["川普壓力指數 +9.0（任期內第 88 百分位）", MEANING]


def test_only_the_key_seen_in_the_saved_page_has_a_word(md):
    assert market_calc.TACO_STATUS_WORDS == {"high": "高壓"}


# ── 過期、暫缺、壞掉、舊快照 ─────────────────────────────────────────────────────


def test_a_stale_row_says_stale_with_its_data_date(md, paths):
    snap = snapshot_with(GOOD)
    snap["rows"]["macro.TACO"]["stale_after"] = NOW - 1
    day = date.fromisoformat(snap["rows"]["macro.TACO"]["data_date"])
    expected = f"川普壓力指數：過期（資料日 {day.month}/{day.day}）"
    assert notes_of(summary_footer(md, paths, snap)) == [expected]
    assert notes_of(detail_footer(md, paths, snap)) == [expected, SOURCE]  # 過期：不寫數字、位階、說明與組成
    assert md.row_width(expected) <= 24


def test_a_stale_row_without_a_date_still_says_stale(md, paths):
    snap = snapshot_with(GOOD)
    snap["rows"]["macro.TACO"]["stale_after"] = NOW - 1
    snap["rows"]["macro.TACO"]["data_date"] = None
    assert notes_of(summary_footer(md, paths, snap)) == ["川普壓力指數：過期"]


def test_missing_never_and_old_snapshots_say_missing(md, paths):
    for snap in (snapshot_with(None), snapshot_with(row=_never())):
        assert notes_of(summary_footer(md, paths, snap)) == ["川普壓力指數：暫缺"]
        assert notes_of(detail_footer(md, paths, snap)) == ["川普壓力指數：暫缺", SOURCE]


BROKEN = {
    "percentile_above_100": {"percentile": 101},
    "percentile_below_0": {"percentile": -1},
    "percentile_string": {"percentile": "88"},
    "percentile_float": {"percentile": 88.5},
    "percentile_bool": {"percentile": True},
    "value_string": {"value": "9.0"},
    "value_out_of_range": {"value": 1e9, "max": 1e9},
    "value_nan": {"value": float("nan")},
    "value_below_the_minimum": {"value": -50.0, "min": -4.9868},
    "few_days": {"n": 10},
    "method_unknown": {"method": "v9"},
    "method_missing": {"method": None},
    "status_key_with_odd_characters": {"status_key": "High Pressure"},
    "since_not_a_date": {"since": "2025-02-30"},
    "since_after_the_maximum": {"since": "2026-04-01"},
    "minimum_above_maximum": {"min": 20.0},
    "max_date_missing": {"max_date": None},
}


@pytest.mark.parametrize("name", list(BROKEN))
def test_a_row_with_wrong_types_or_ranges_is_shown_as_missing(md, paths, name):
    values = {**GOOD, **BROKEN[name]}
    if name == "value_nan":
        snap = snapshot_with(GOOD)
        write_snapshot(paths, snap)
        raw = (paths.snapshot.read_text(encoding="utf-8")).replace('"value": 9.0005,', '"value": NaN,', 1)
        paths.snapshot.write_text(raw, encoding="utf-8")
        md.clear_cache()
        reply = md.handle("", now=NOW, paths=paths)
    else:
        reply = render(md, paths, snapshot_with(values))
    assert reply.flex is not None, name
    footer = texts(macro_bubble(md, reply.flex)["footer"])
    assert notes_of(footer) == ["川普壓力指數：暫缺"], (name, footer)
    assert all(t not in strings_of(reply) for t in (LINE,)), name


def test_missing_fields_are_shown_as_missing(md, paths):
    for key in GOOD:
        values = {k: v for k, v in GOOD.items() if k != key}
        footer = summary_footer(md, paths, snapshot_with(values))
        assert notes_of(footer) == ["川普壓力指數：暫缺"], key


def test_a_values_row_that_is_not_a_dict_is_missing(md, paths):
    for bad in (None, [], "x", 5):
        row = _row(GOOD, NOW, date(2026, 10, 16))
        row["values"] = bad
        assert notes_of(summary_footer(md, paths, snapshot_with(row=row))) == ["川普壓力指數：暫缺"], bad


# ── 細節卡（新聞與宏觀 3/3）的頁尾 ───────────────────────────────────────────────


def test_the_detail_footer_has_the_full_explanation(md, paths):
    for now in ALL_NOWS:
        snap = snapshot_with(GOOD, now=now)
        footer = detail_footer(md, paths, snap, now=now)
        # 第二行是這一筆的資料日（F2-1）；位階那行的「394 個交易日」是交易日筆數（F2-2）
        assert notes_of(footer) == [LINE, data_day_line(snap), CHANGE, POSITION, MEANING, SOURCE, COMPOSITION], (now, footer)
        assert footer[len(notes_of(footer))] == DISCLAIMER


def test_a_changed_method_version_does_not_print_the_old_composition(md, paths):
    snap = snapshot_with(taco(method="other"))
    footer = detail_footer(md, paths, snap)
    assert notes_of(footer) == [LINE, data_day_line(snap), CHANGE, POSITION, MEANING, SOURCE, METHOD_CHANGED]
    assert not any("淨支持率" in t for t in footer)


def test_a_missing_20_day_change_is_said_to_be_missing(md, paths):
    footer = detail_footer(md, paths, snapshot_with(taco(chg20=None)))
    assert notes_of(footer)[2] == "20 個交易日變化：暫缺"


@pytest.mark.parametrize("change, text", [(-3.6, "-3.6"), (0.0, "0.0"), (0.04, "0.0"), (12.345, "+12.3"), (-200.0, "-200.0"),
                                           (200.0, "+200.0")])
def test_the_20_day_change_is_signed_with_one_decimal(md, paths, change, text):
    assert notes_of(detail_footer(md, paths, snapshot_with(taco(chg20=change))))[2] == f"20 個交易日變化：{text}"


def test_the_position_line_uses_full_dates_and_one_decimal(md, paths):
    """2026-10-28 整合時改：日期寫完整的年月日。只寫月／日的話，「12/31 起…最高 12/31」分不出是同一天還是隔一年。"""
    values = taco(percentile=0, n=3000, since="2025-12-31", value=-100.0, max=100.0, max_date="2026-12-31",
                  min=-100.0, min_date="2026-10-26")
    line = notes_of(detail_footer(md, paths, snapshot_with(values)))[3]
    assert line == "位階：2025-12-31 起 3000 個交易日中第 0 百分位（最高 +100.0，2026-12-31；最低 -100.0，2026-10-26）"
    assert len(line) <= 80


def test_no_year_appears_in_any_taco_text(md, paths):
    for now in ALL_NOWS:
        footer = detail_footer(md, paths, snapshot_with(GOOD, now=now), now=now)
        assert POSITION in footer  # 位階那一行的日期帶年份（來自資料；2026-10-28 整合時改），其他句子不寫年份
        assert not any(str(year) in t for t in notes_of(footer) if not t.startswith("位階：")
                       for year in (2025, 2026, 2027, 2028)), footer


# ── 來源的文字不能出現 ──────────────────────────────────────────────────────────


def test_source_text_in_the_snapshot_never_reaches_a_card_or_an_alt_text(md, paths):
    values = {**GOOD, "label": "高压", "note": "特朗普净支持率变化", "description": "压力指数处在高压区", "methodology": "持续压力新4"}
    row = _row(values, NOW, date(2026, 10, 16))
    row.update(label="高压", source="Silver Bulletin", basis="较20个交易日前", period="持续高位")
    write_snapshot(paths, snapshot_with(row=row))
    seen = 0
    replies = render_all(md, paths, NOW)
    for body, reply in replies.items():
        for text in strings_of(reply):
            seen += 1
            assert not any(decoy in text for decoy in CARD_DECOYS), (body, text)
            assert not any(ch in text for ch in SIMPLIFIED_ONLY), (body, text)
    assert seen > 100
    assert LINE in strings_of(replies[""]) and POSITION in strings_of(replies[" 新聞"])  # 這一列確實被讀了


def test_a_status_key_is_never_printed_as_is(md, paths):
    footer = summary_footer(md, paths, snapshot_with(taco(status_key="mid")))
    assert "川普壓力指數 +9.0（任期內第 88 百分位）" in footer and not any("mid" in t for t in footer)
    footer = detail_footer(md, paths, snapshot_with(GOOD))
    assert POSITION in footer and not any("high" in t for t in footer)


# ── 每個 TACO 句子過 validator、禁用字、main 的真閘門 ─────────────────────────────


def taco_fixed_texts() -> list[str]:
    return [value for name, value in vars(market_calc).items() if name.startswith("TEXT_TACO_") and isinstance(value, str)]


def test_the_fixed_taco_sentences_are_in_the_fixed_text_list_and_clean(md):
    fixed = taco_fixed_texts()
    assert len(fixed) >= 11 and all(text in market_calc.FIXED_TEXTS for text in fixed)
    for now in ALL_NOWS:
        now_dt = datetime.fromtimestamp(now, TPE)
        for text in fixed:
            checked = output_validator.validate_outbound_text(text, now=now_dt)
            assert checked.ok and checked.text == text, text
            assert not md.has_forbidden(text), text
    assert all(not any(ch in text for ch in SIMPLIFIED_ONLY) for text in fixed)


@pytest.mark.parametrize("case", ["fresh", "other_method", "no_change", "stale", "missing", "broken"])
def test_every_card_with_the_taco_row_passes_the_validator_and_mains_gate(md, paths, case):
    snap = {
        "fresh": lambda: snapshot_with(GOOD),
        "other_method": lambda: snapshot_with(taco(method="other")),
        "no_change": lambda: snapshot_with(taco(chg20=None, status_key=None)),
        "stale": lambda: snapshot_with(row=_row(GOOD, NOW, date(2026, 10, 16), stale_in=-1.0)),
        "missing": lambda: snapshot_with(None),
        "broken": lambda: snapshot_with({**GOOD, "percentile": 150}),
    }[case]()
    for now in ALL_NOWS:
        snap_now = copy.deepcopy(snap)
        write_snapshot(paths, snap_now)
        now_dt = datetime.fromtimestamp(now, TPE)
        seen = []
        for body, reply in render_all(md, paths, now).items():
            assert_main_gate(reply)
            if reply.flex is None:  # 2027 年 2 月之後 /盤勢 選舉 是固定句「本屆期中選舉已結束」
                assert body == " 選舉" and reply.text, (body, reply.text)
                continue
            for text in texts(reply.flex):
                if "川普壓力" in text or "MacroMargin" in text or "位階" in text or "組成" in text:
                    checked = output_validator.validate_outbound_text(text, now=now_dt)
                    assert checked.ok and checked.text == text, (body, text)
                    assert not md.has_forbidden(text), text
                    seen.append((body, text))
        # 摘要頁尾、細節頁尾的第一行一定都在（每種狀態都有 TACO 那一行），不然上面的迴圈什麼都沒檢查
        assert {body for body, text in seen if text.startswith("川普壓力指數")} == {"", " 新聞"}, seen


# ── 大小 ───────────────────────────────────────────────────────────────────────


def test_the_worst_taco_row_keeps_every_card_inside_the_line_limits(md, paths):
    worst = taco(value=-100.0, percentile=100, n=20000, chg20=-200.0, since="2025-12-31", max_date="2026-12-31",
                 min_date="2026-12-30", max=100.0, min=-100.0)
    snap = worst_snapshot(NOW)
    snap["rows"]["macro.TACO"] = _row(worst, NOW, date(2026, 10, 16))
    write_snapshot(paths, snap)
    sizes = {}
    for body, reply in render_all(md, paths, NOW).items():
        assert reply.flex is not None, (body, reply.text)
        card = reply.flex
        assert size(card) <= 28 * 1024, (body, size(card))
        if body == "":
            assert all(size(b) <= 6656 for b in card["contents"]), body
        paged = card_pages.with_page_button(card, LONG_PAGE_URL)
        assert card_pages.fits_line_limits(paged), (body, size(paged))
        sizes[body] = size(card)
    # 新聞與宏觀（細節）多了幾行註記；它是這次唯一變大的卡（摘要頁尾那行比原本的固定句還短）
    without = worst_snapshot(NOW)
    del without["rows"]["macro.TACO"]
    write_snapshot(paths, without)
    base = size(md.handle(" 新聞", now=NOW, paths=paths).flex)
    assert 0 < sizes[" 新聞"] - base < 2200, (sizes[" 新聞"], base)


# ── 監控與讀取的列 ──────────────────────────────────────────────────────────────


def test_the_dashboard_reads_the_row_but_does_not_list_it_among_the_macro_numbers(md, paths):
    assert "macro.TACO" in md.READ_ROWS and set(md.READ_ROWS) <= set(market_calc.ROW_IDS)
    assert "TACO" not in md._MACRO  # 「宏觀數字」那張的通用格式（值、日變化、來源）不適合這個指數
    reply = render(md, paths, snapshot_with(GOOD), " 新聞")
    numbers = texts(reply.flex["contents"][2]["body"])
    assert not any("川普壓力" in t for t in numbers)


def test_status_summary_reports_a_stale_or_missing_taco_row(md, paths):
    stale = snapshot_with(GOOD)
    stale["rows"]["macro.TACO"]["stale_after"] = NOW - 1
    write_snapshot(paths, stale)
    summary = md.status_summary(now=NOW, paths=paths)
    assert "macro.TACO" in summary["rows_stale"] and "macro.TACO" not in summary["rows_missing"]
    assert summary["rows_unexpected"] == [] and "render_error" not in summary and summary["external_dropped"] == 0
    write_snapshot(paths, snapshot_with(row=_never()))
    summary = md.status_summary(now=NOW, paths=paths)
    assert "macro.TACO" in summary["rows_missing"] and summary["external_dropped"] == 0
    write_snapshot(paths, snapshot_with(None))  # 舊 job 寫的快照沒有這一列：bot 照常，不算意外的列
    summary = md.status_summary(now=NOW, paths=paths)
    assert summary["rows_unexpected"] == [] and "render_error" not in summary and summary["snapshot"] == "ok"
    write_snapshot(paths, snapshot_with(GOOD))
    summary = md.status_summary(now=NOW, paths=paths)
    assert "macro.TACO" not in summary["rows_stale"] + summary["rows_missing"] and summary["external_dropped"] == 0


def test_the_snapshot_row_survives_a_json_round_trip_with_ensure_ascii(md, paths):
    write_snapshot(paths, snapshot_with(GOOD), ensure_ascii=True)
    assert notes_of(texts(macro_bubble(md, md.handle("", now=NOW, paths=paths).flex)["footer"])) == [LINE, MEANING]
    assert json.loads(paths.snapshot.read_text(encoding="utf-8"))["rows"]["macro.TACO"]["values"]["percentile"] == 88
