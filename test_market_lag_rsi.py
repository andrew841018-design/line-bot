"""市場盤勢 第 9 輪批 1・A2 軌（2026-10-24）的測試：RSI、落後／休市標示、0050 日期、暫定標示繼承。

計畫：PLAN_r9_andrew9_v2.md 的 A18（RSI）、A19（落後／休市標示）、A20（0050 日期）、A27（暫定標示繼承）；
審查依據 reviews/r9_plan_seat3.md（F7、F14、§三的 5、6、9）、seat4（F6、F14）、seat2（N2）、seat1（§4.5）。

這個檔案單獨一份（不和別軌改同一個測試檔）；只借 test_market_dashboard／test_market_publish 的 helper 與 fixture，
不借它們的 test_* 函式（借了會被收集第二次）。檔名要符合 test_market_*.py，標準的測試指令才跑得到。
每個「不該有標示」的測試前面都先放一個「該有標示」的對照，所以沒實作時這些測試也是紅的（不是空測）。
"""

from __future__ import annotations

import itertools
import json
from datetime import date, datetime, timedelta

import pytest

import card_pages
import market_calc
import market_calendar
import market_publish
import market_store
import output_validator
from test_market_dashboard import ALL_NOWS, FLEX_BODIES, LONG_PAGE_URL
from test_market_dashboard import NOW as DASH_NOW
from test_market_dashboard import (  # noqa: F401  md／paths 是 fixture（用名字被 pytest 找到）
    TPE,
    assert_main_gate,
    make_snapshot,
    md,
    paths,
    render_all,
    reply_strings,
    size,
    texts,
    worst_snapshot,
    write_snapshot,
)
from test_market_publish import NOW as PUB_NOW
from test_market_publish import D, TW_DAYS, Clock, World, at_et, at_tpe, price_rows, seed_tw
from test_market_publish import world  # noqa: F401  fixture

NEW_PROGRESS_KEYS = ("us_day", "us_expected_day", "us_waiting_for_source", "tw_day", "tw_expected_day",
                     "tw_waiting_for_source", "tw_closed_today", "tw_closed_confirmed", "tw_exrights_pending")
PROVISIONAL = "（暫定）"
RSI_NOTE = "RSI14：0–100，70 以上偏熱、30 以下偏冷（描述狀態）"


# ── 小工具 ───────────────────────────────────────────────────────────────────
def summary_cards(md_, paths_, snap, now=DASH_NOW):
    """摘要輪播除了重點摘要以外的泡泡：(標題, 副標或 None, 內文列, footer 的字)。"""
    write_snapshot(paths_, snap)
    found = []
    for bubble in md_.handle("", now=now, paths=paths_).flex["contents"]:
        header = texts(bubble["header"])
        if header[0].startswith("重點摘要"):  # A1 的重點摘要排第一張、帶「往左滑」：這裡只看美股、台股以後各張
            continue
        found.append((header[0], header[1] if len(header) > 1 else None, texts(bubble["body"]), texts(bubble["footer"])))
    return found


def snapshot_with_progress(**progress):
    snap = make_snapshot(DASH_NOW)
    snap["progress"].update(progress)
    return snap


# ═════════════════════════════════════════════════════════════════════════════
# 一、RSI（A18）：摘要列的 ETF 與加權、0050
# ═════════════════════════════════════════════════════════════════════════════
def legacy_etf_line(md_, view, name, main_day):
    """b1dfcf1 的 _etf_line（fresh 的那條路）：RSI 只能是「多出來的一小段」，拿掉它必須剛好等於舊輸出。"""
    values = view.values
    close = md_._num(values.get("close"), 0)
    change = md_._num(values.get("chg_pct"), -100, 1000)
    above = md_._flag(values.get("above_ma20"))
    status = md_._above_word(above)
    close_text = md_._price(close) if close is not None else "收盤暫缺"
    change_text = f" {md_._pct(change, 2)}" if change is not None else ""
    status_text = f" {status}" if status else ""
    suffix = md_._date_suffix(view.data_date, main_day)
    tag = market_calc.TEXT_PROVISIONAL if view.provisional else ""
    text = md_._fit(
        f"{name}{close_text}{change_text}{status_text}{tag}{suffix}",
        f"{name}{close_text}{status_text}{tag}{suffix}",
        f"{name}{close_text}{tag}{suffix}",
        f"{name}{close_text}{change_text}{status_text}{tag}",
        f"{name}{close_text}{status_text}{tag}",
    )
    return text, md_._above_color(above)


def legacy_taiex_line(md_, view):
    values = view.values
    close = md_._num(values.get("close"), 0)
    change = md_._num(values.get("chg_pct"), -100, 1000)
    above = md_._flag(values.get("above_ma20"))
    close_text = md_._price(close) if close is not None else "收盤暫缺"
    change_text = f"（{md_._pct(change, 2)}）" if change is not None else ""
    status = md_._above_word(above)
    name = "加權指數"
    text = md_._fit(f"{name} {close_text}{change_text}{' ' + status if status else ''}", f"{name} {close_text}{change_text}")
    return text, md_._above_color(above)


NAMES = ("標普500（VOO）", "那斯達克100（QQQ）", "半導體（SOXX）", "台灣50（0050）")
CLOSES = (612.34, 99999.99, 1234567.0)  # 三位數、五位數（印成 100,000）、七位數
CHANGES = (None, 0.52, -12.34, 99.99, -99.99)
ABOVES = (True, False, None)
RSIS = (None, 0.0, 53.4, 99.6, 100.0)


@pytest.mark.parametrize("provisional", [False, True], ids=["formal", "provisional"])
@pytest.mark.parametrize("behind", [False, True], ids=["same_day", "date_behind"])
def test_etf_line_rsi_is_purely_additive_and_the_first_thing_dropped(md, provisional, behind):
    """審查 r9 s3-F14：RSI 變體放最前面，放不下就整個拿掉；暫定標籤每個寫法都在、資料日比標題舊時日期最後才丟。
    不變量：把輸出裡的「 RSI nn」拿掉，剛好等於 b1dfcf1 的輸出（所以 r9c 定的優先序一個字都沒動）。"""
    main = date(2026, 10, 22)
    day = date(2026, 10, 21) if behind else main
    shown_rsi = 0
    for name, close, change, above, rsi in itertools.product(NAMES, CLOSES, CHANGES, ABOVES, RSIS):
        values = {"close": close, "chg_pct": change, "above_ma20": above, "rsi14": rsi}
        view = md._View(md.FRESH, values, day, False, provisional)
        text, color = md._etf_line(None, view, name, main)
        old, old_color = legacy_etf_line(md, view, name, main)
        rsi_text = f" RSI {rsi:.0f}" if rsi is not None else None
        label = (name, close, change, above, rsi)
        assert md.row_width(text) <= 24 and "…" not in text, (label, text)
        assert color == old_color, label
        if rsi_text is None:
            assert text == old, (label, text)
        else:
            assert text.replace(rsi_text, "", 1) == old, (label, text, old)
            shown_rsi += rsi_text in text
        if provisional:
            assert PROVISIONAL in text, (label, text)  # 暫定標籤每個寫法都要在
        if behind:
            assert "（10/21）" in text, (label, text)  # 落後的資料日不能被 RSI 擠掉
    assert shown_rsi > 0  # 不是空測：放得下的組合真的有寫 RSI


def test_etf_line_rsi_golden_strings(md):
    def line(name, close, change, above, rsi, *, day=date(2026, 10, 22), main=date(2026, 10, 22), provisional=False):
        view = md._View(md.FRESH, {"close": close, "chg_pct": change, "above_ma20": above, "rsi14": rsi}, day, False,
                        provisional)
        return md._etf_line(None, view, name, main)[0]

    assert line("標普500（VOO）", 612.34, 0.52, True, 53.4) == "標普500（VOO）612.34 +0.52% 在月線上 RSI 53"
    assert line("那斯達克100（QQQ）", 540.12, -0.31, False, 41.0) == "那斯達克100（QQQ）540.12 -0.31% 在月線下 RSI 41"
    assert line("標普500（VOO）", 612.34, 0.52, True, 100.0) == "標普500（VOO）612.34 +0.52% 在月線上 RSI 100"
    assert line("標普500（VOO）", 612.34, 0.52, True, None) == "標普500（VOO）612.34 +0.52% 在月線上"  # None 就不寫
    # 日期落後＋暫定＋RSI 同時出現：RSI 先丟，再丟漲跌、再丟月線狀態（r9c 的順序），暫定與日期一直留著
    behind = line("那斯達克100（QQQ）", 540.12, -0.31, False, 41.0, day=date(2026, 10, 21), provisional=True)
    assert behind == "那斯達克100（QQQ）540.12（暫定）（10/21）" and md.row_width(behind) == 20.5
    voo = line("標普500（VOO）", 612.34, None, True, 62.2, day=date(2026, 10, 21), provisional=True)
    assert voo == "標普500（VOO）612.34 在月線上（暫定）（10/21）" and md.row_width(voo) == 23.0
    # 放得下時 RSI 排在暫定標籤與日期之前
    short = line("半導體（SOXX）", 245.6, None, None, 62.2, day=date(2026, 10, 21), provisional=True)
    assert short == "半導體（SOXX）245.6 RSI 62（暫定）（10/21）" and md.row_width(short) == 21.5


@pytest.mark.parametrize("with_date", [False, True], ids=["same_day", "date_behind"])
def test_taiex_line_rsi_is_purely_additive(md, with_date):
    main = date(2026, 10, 22)
    day = date(2026, 10, 21) if with_date else main
    shown_rsi = 0
    for close, change, above, rsi in itertools.product(CLOSES + (47111.26, 100000.0), CHANGES, ABOVES, RSIS):
        values = {"close": close, "chg_pct": change, "above_ma20": above, "rsi14": rsi}
        view = md._View(md.FRESH, values, day)
        text, color = md._taiex_line(None, view)
        old, old_color = legacy_taiex_line(md, view)
        rsi_text = f" RSI {rsi:.0f}" if rsi is not None else None
        label = (close, change, above, rsi)
        assert md.row_width(text) <= 24 and "…" not in text and color == old_color, (label, text)
        assert (text.replace(rsi_text, "", 1) if rsi_text else text) == old, (label, text, old)
        shown_rsi += bool(rsi_text and rsi_text in text)
        dated, _ = md._taiex_line(None, view, main)  # 細節卡用：和標題日不同時補日期
        if with_date:
            assert "（10/21）" in dated and md.row_width(dated) <= 24 and "…" not in dated, (label, dated)
        else:
            assert dated == text, label
        bare, _ = md._taiex_line(None, view, main, with_rsi=False)  # 細節卡的第一列不放 RSI（那一列原本就沒有）
        assert "RSI" not in bare, label
    assert shown_rsi > 0


def test_taiex_line_golden_strings(md):
    def line(close, change, above, rsi, **kwargs):
        view = md._View(md.FRESH, {"close": close, "chg_pct": change, "above_ma20": above, "rsi14": rsi},
                        kwargs.pop("day", date(2026, 10, 22)))
        return md._taiex_line(None, view, **kwargs)[0]

    assert line(47111.26, 0.52, True, 61.7) == "加權指數 47,111（+0.52%） 在月線上 RSI 62"
    assert line(47111.26, 0.52, True, None) == "加權指數 47,111（+0.52%） 在月線上"
    assert line(100000.0, -10.0, False, 100.0) == "加權指數 100,000（-10.00%） 在月線下 RSI 100"  # 最壞值
    assert md.row_width("加權指數 100,000（-10.00%） 在月線下 RSI 100") == 22.0
    # 細節卡的第一列：和標題日（0050 的資料日）不同才補日期；RSI 放不下就先丟，日期留著
    behind = line(47111.26, 0.52, True, 61.7, day=date(2026, 10, 22), main_day=date(2026, 10, 16))
    assert behind == "加權指數 47,111（+0.52%） 在月線上（10/22）"
    assert line(47111.26, 0.52, True, 61.7, day=date(2026, 10, 22), main_day=date(2026, 10, 16), with_rsi=False) == behind
    assert line(47111.26, 0.52, True, 61.7, with_rsi=False) == "加權指數 47,111（+0.52%） 在月線上"


def test_summary_rows_show_rsi_for_us_etfs_taiex_and_0050(md, paths):
    snap = make_snapshot(DASH_NOW)
    snap["rows"]["tw.taiex"]["values"]["rsi14"] = 61.7
    us, tw = summary_cards(md, paths, snap)[:2]
    assert us[2][:3] == [
        "標普500（VOO）612.34 +0.52% 在月線上 RSI 55",
        "那斯達克100（QQQ）540.12 -0.31% 在月線下 RSI 55",
        "半導體（SOXX）245.6 +1.20% 在月線上（10/15）",  # 帶日期（落後一天）放不下 RSI：RSI 先丟，日期留著
    ]
    assert tw[2][:2] == ["加權指數 47,111（+0.52%） 在月線上 RSI 62", "台灣50（0050）185.3 +0.40% 在月線上 RSI 55"]
    for body in (us[2], tw[2]):
        assert all(md.row_width(text) <= 24 and "…" not in text for text in body)


def test_snapshot_without_rsi_reads_as_before(md, paths):
    """舊快照（tw.taiex 沒有 rsi14 這個鍵、ETF 的 rsi14 是 null）：不寫 RSI、不丟例外。"""
    control = make_snapshot(DASH_NOW)
    control["rows"]["tw.taiex"]["values"]["rsi14"] = 61.7
    us, tw = summary_cards(md, paths, control)[:2]
    assert "RSI" in us[2][0] and "RSI" in tw[2][0] and "RSI" in tw[2][1]  # 對照：有值就寫
    snap = make_snapshot(DASH_NOW)
    snap["rows"]["tw.taiex"]["values"].pop("rsi14", None)
    for row_id in ("us.etf.VOO", "us.etf.QQQ", "tw.etf.0050"):
        snap["rows"][row_id]["values"]["rsi14"] = None
    us, tw = summary_cards(md, paths, snap)[:2]
    assert us[2][:2] == ["標普500（VOO）612.34 +0.52% 在月線上", "那斯達克100（QQQ）540.12 -0.31% 在月線下"]
    assert tw[2][:2] == ["加權指數 47,111（+0.52%） 在月線上", "台灣50（0050）185.3 +0.40% 在月線上"]
    assert not any("RSI" in text for text in us[2] + tw[2])


def test_detail_cards_carry_the_fixed_rsi_note_only_when_rsi_is_shown(md, paths):
    write_snapshot(paths, make_snapshot(DASH_NOW))
    assert market_calc.TEXT_RSI_NOTE == RSI_NOTE and md.TEXT_RSI_NOTE == RSI_NOTE
    for body in (" VOO", " QQQ", " SOXX", " 0050"):
        card = md.handle(body, now=DASH_NOW, paths=paths).flex
        assert any("RSI14 55" in text for text in texts(card["body"])), body  # 有 RSI 那一列
        footer = texts(card["footer"])
        assert RSI_NOTE in footer, body
        assert footer.index(RSI_NOTE) < footer.index(market_calc.TEXT_DISCLAIMER), body  # 註腳在免責句之前
    for body in ("", " 期權", " 新聞", " 季節性", " 選舉"):  # 其他卡不加
        assert RSI_NOTE not in reply_strings(md.handle(body, now=DASH_NOW, paths=paths)), body
    # RSI 那一列沒有（null、整檔過期）：註腳也不放
    for row_id, body in (("us.etf.VOO", " VOO"), ("us.etf.QQQ", " QQQ"), ("us.etf.SOXX", " SOXX"), ("tw.etf.0050", " 0050")):
        broken = make_snapshot(DASH_NOW)
        broken["rows"][row_id]["values"]["rsi14"] = None
        write_snapshot(paths, broken)
        card = md.handle(body, now=DASH_NOW, paths=paths).flex
        assert RSI_NOTE not in texts(card) and not any("RSI14" in text for text in texts(card["body"])), body
        broken = make_snapshot(DASH_NOW)
        broken["rows"][row_id]["stale_after"] = DASH_NOW - 1
        write_snapshot(paths, broken)
        assert RSI_NOTE not in texts(md.handle(body, now=DASH_NOW, paths=paths).flex), body
    assert md.row_width(RSI_NOTE) == 25.0  # 量過：註腳是 size md 可換行的灰字，不受摘要列 24 的限制
    assert RSI_NOTE in market_calc.FIXED_TEXTS


# ═════════════════════════════════════════════════════════════════════════════
# 二、0050 細節卡的日期與原因（A20）
# ═════════════════════════════════════════════════════════════════════════════
LAG_REASON = "0050／成分資料到 10/15（除權息資料待更新）"


def snapshot_0050_behind(**progress):
    snap = make_snapshot(DASH_NOW)
    snap["rows"]["tw.etf.0050"]["data_date"] = "2026-10-15"  # 0050 比加權（10/16）晚一天
    snap["progress"].update(progress)
    return snap


def test_0050_detail_dates_the_taiex_row_when_the_two_days_differ(md, paths):
    write_snapshot(paths, snapshot_0050_behind())
    card = md.handle(" 0050", now=DASH_NOW, paths=paths).flex
    shown = texts(card)
    assert shown[0] == "台灣50（0050）｜10/15 收盤"
    assert shown[1] == "加權指數 47,111（+0.52%） 在月線上（10/16）"  # 加權那一列補日期；不加 RSI（細節卡原本就沒有）
    assert market_calc.TEXT_DISCLAIMER in shown
    assert not any("除權息資料待更新" in text for text in shown)  # 快照沒有證據：只補日期、不寫原因


def test_0050_detail_has_no_suffix_when_the_days_are_the_same_or_0050_is_not_fresh(md, paths):
    write_snapshot(paths, snapshot_0050_behind())
    assert texts(md.handle(" 0050", now=DASH_NOW, paths=paths).flex)[1].endswith("（10/16）")  # 對照：不同天就補
    write_snapshot(paths, make_snapshot(DASH_NOW))
    assert texts(md.handle(" 0050", now=DASH_NOW, paths=paths).flex)[1] == "加權指數 47,111（+0.52%） 在月線上"
    snap = snapshot_0050_behind()
    snap["rows"]["tw.etf.0050"]["stale_after"] = DASH_NOW - 1  # 0050 過期：標題改用加權的日期，兩邊一致
    write_snapshot(paths, snap)
    shown = texts(md.handle(" 0050", now=DASH_NOW, paths=paths).flex)
    assert shown[0] == "台灣50（0050）｜10/16 收盤" and shown[1] == "加權指數 47,111（+0.52%） 在月線上"


def test_0050_reason_needs_the_pending_flag_and_an_older_0050_day(md, paths):
    def notes(snap, body=" 0050"):
        write_snapshot(paths, snap)
        return texts(md.handle(body, now=DASH_NOW, paths=paths).flex["footer"])

    footer = notes(snapshot_0050_behind(tw_exrights_pending=True))
    assert LAG_REASON in footer and md.row_width(LAG_REASON) == 21.0
    assert footer.index(LAG_REASON) < footer.index(market_calc.TEXT_DISCLAIMER)
    for pending in (False, None, "yes", 1):  # 沒有證據（旗標不是 true）：只補日期
        assert LAG_REASON not in notes(snapshot_0050_behind(tw_exrights_pending=pending)), pending
    assert LAG_REASON not in notes(snapshot_0050_behind())  # 舊快照沒有這個鍵
    # 旗標為真但兩邊同一天（或 0050 沒有比加權舊）：沒有要解釋的落差，不寫
    same = make_snapshot(DASH_NOW)
    same["progress"]["tw_exrights_pending"] = True
    assert not any("除權息資料待更新" in text for text in notes(same))
    newer = snapshot_0050_behind(tw_exrights_pending=True)
    newer["rows"]["tw.taiex"]["data_date"] = "2026-10-14"
    assert not any("除權息資料待更新" in text for text in notes(newer))
    # 加權過期：兩邊日期比不出來，不寫
    stale = snapshot_0050_behind(tw_exrights_pending=True)
    stale["rows"]["tw.taiex"]["stale_after"] = DASH_NOW - 1
    assert not any("除權息資料待更新" in text for text in notes(stale))
    # 只出現在 0050 細節卡，不進摘要卡、不進美股 ETF 細節
    write_snapshot(paths, snapshot_0050_behind(tw_exrights_pending=True))
    for body in ("", " VOO", " 期權"):
        assert LAG_REASON not in reply_strings(md.handle(body, now=DASH_NOW, paths=paths)), body


def test_exrights_reason_end_to_end(tmp_path, md):
    """publish 看到除權息資料（TWT49U）只涵蓋到 T 的前一天 → 旗標為真；0050 與成分顯示前一天，細節卡寫日期與原因。"""
    w = World(tmp_path)
    try:
        w.store._db().execute("DELETE FROM coverage")
        w.store.add_coverage("twt49u", TW_DAYS[0], TW_DAYS[-2])
        assert w.publish()["kind"] == "ok"
        snap = w.snapshot()
        assert snap["progress"]["tw_exrights_pending"] is True
        assert snap["rows"]["tw.etf.0050"]["data_date"] == TW_DAYS[-2].isoformat()
        shown = texts(md.handle(" 0050", now=PUB_NOW, paths=w.paths).flex)
        assert shown[0] == f"台灣50（0050）｜{TW_DAYS[-2].month}/{TW_DAYS[-2].day} 收盤"
        assert shown[1].startswith("加權指數 ") and shown[1].endswith(f"（{D.month}/{D.day}）")
        assert f"0050／成分資料到 {TW_DAYS[-2].month}/{TW_DAYS[-2].day}（除權息資料待更新）" in shown
        # 資料補到 T 之後：旗標轉假，原因句消失
        w.store.add_coverage("twt49u", TW_DAYS[-1], TW_DAYS[-1])
        w.publish()
        assert w.snapshot()["progress"]["tw_exrights_pending"] is False
        assert not any("除權息資料待更新" in text for text in texts(md.handle(" 0050", now=PUB_NOW, paths=w.paths).flex))
    finally:
        w.close()


# ═════════════════════════════════════════════════════════════════════════════
# 三、落後／休市標示：publish 端的 progress（A19）
# ═════════════════════════════════════════════════════════════════════════════
def test_publish_progress_has_the_new_fields(world):
    world.publish()
    snap = world.snapshot()
    progress = snap["progress"]
    assert progress == {
        "us_members": progress["us_members"], "us_last_complete_day": "2026-10-16", "tw_last_complete_day": "2026-10-16",
        "us_day": "2026-10-16", "us_expected_day": "2026-10-16", "us_waiting_for_source": False,
        "tw_day": "2026-10-16", "tw_expected_day": "2026-10-16", "tw_waiting_for_source": False,
        "tw_closed_today": False, "tw_closed_confirmed": None, "tw_exrights_pending": False,
    }
    assert set(NEW_PROGRESS_KEYS) <= set(progress)
    assert market_publish.validate_snapshot(snap) == []
    assert progress["tw_day"] == snap["rows"]["tw.taiex"]["data_date"]  # 和加權那一列同一天


def test_us_waits_for_the_source_when_the_expected_session_has_no_formal_close(world):
    """預期的最新場次 D（收盤＋30 分鐘已過）比三檔 ETF 的正式資料最新日新 → us_waiting_for_source。"""
    db = world.store._db()
    db.execute("DELETE FROM closes WHERE market = 'us' AND symbol IN ('VOO','QQQ','SOXX') AND date = ?", (D.isoformat(),))
    world.publish()
    progress = world.snapshot()["progress"]
    assert (progress["us_day"], progress["us_expected_day"], progress["us_waiting_for_source"]) == (
        "2026-10-15", "2026-10-16", True)
    # 只有一檔的正式資料到 D：來源已經給了（落後的那兩檔卡上自己帶日期）
    world.store.upsert_closes("us", "VOO", [(D, 551.0, 1_000_000)], "nasdaq")
    world.publish()
    progress = world.snapshot()["progress"]
    assert (progress["us_day"], progress["us_waiting_for_source"]) == ("2026-10-16", False)


def test_a_provisional_close_is_not_formal_data(world):
    """CBOE 的暫定收盤不算正式資料：us_day 停在前一天、waiting 為真（卡上要不要寫由 bot 看自己顯示的日期決定）。"""
    db = world.store._db()
    db.execute("DELETE FROM closes WHERE market = 'us' AND symbol IN ('VOO','QQQ','SOXX') AND date = ?", (D.isoformat(),))
    for symbol in ("VOO", "QQQ", "SOXX"):
        world.store.upsert_closes("us", symbol, [(D, 500.0, None)], "cboe", provisional=True)
    world.publish()
    snap = world.snapshot()
    progress = snap["progress"]
    assert (progress["us_day"], progress["us_waiting_for_source"]) == ("2026-10-15", True)
    assert snap["rows"]["us.etf.VOO"]["data_date"] == "2026-10-16"  # 卡上照樣有 D 的（暫定）收盤


def test_us_progress_stays_empty_without_any_formal_etf_close(tmp_path):
    store = market_store.Store(tmp_path / "h.sqlite", now_fn=Clock(PUB_NOW))
    try:
        ctx = market_publish._Ctx(store, None, PUB_NOW)
        market_publish._build_progress(ctx)  # 成分股清單也沒有：原本的 us_members 那段會被略過，新的欄位不受影響
        progress = ctx.progress
        assert progress["us_expected_day"] == "2026-10-16"  # 日曆自己就知道 D
        assert progress["us_day"] is None and progress["us_waiting_for_source"] is None  # 沒有資料可比：不下結論
    finally:
        store.close()


@pytest.mark.parametrize(
    "now, expected",
    [
        (at_tpe(2026, 10, 31, 4, 29), "2026-10-29"),  # 夏令（EDT）：10/30 收盤 16:00＝台北 04:00，收盤＋30 分鐘＝04:30
        (at_tpe(2026, 10, 31, 4, 31), "2026-10-30"),
        (at_tpe(2026, 11, 3, 5, 29), "2026-10-30"),  # 11/1 改冬令（EST）：11/2 收盤＝台北 05:00，＋30 分鐘＝05:30
        (at_tpe(2026, 11, 3, 5, 31), "2026-11-02"),
        (at_tpe(2026, 11, 28, 2, 29), "2026-11-25"),  # 11/26 感恩節休市；11/27 早收 13:00 EST＝台北 02:00，＋30 分鐘＝02:30
        (at_tpe(2026, 11, 28, 2, 31), "2026-11-27"),
        (at_et(2026, 12, 24, 13, 29), "2026-12-23"),  # 12/24 早收
        (at_et(2026, 12, 24, 13, 31), "2026-12-24"),
    ],
)
def test_expected_us_session_follows_dst_and_early_closes(tmp_path, now, expected):
    store = market_store.Store(tmp_path / "h.sqlite", now_fn=Clock(now))
    try:
        for symbol in ("VOO", "QQQ", "SOXX"):
            store.upsert_closes("us", symbol, price_rows([date(2026, 10, 27), date(2026, 10, 28)], 500.0), "nasdaq")
        ctx = market_publish._Ctx(store, None, now)
        market_publish._build_progress(ctx)
        assert ctx.progress["us_expected_day"] == expected
        assert ctx.progress["us_day"] == "2026-10-28" and ctx.progress["us_waiting_for_source"] is True  # 都比 10/28 新
    finally:
        store.close()


def tw_ctx_progress(tmp_path, now, *, overrides=(), observed=(), coverage_to=None, seed=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = market_store.Store(tmp_path / "tw.sqlite", now_fn=Clock(now))
    try:
        if seed:
            seed_tw(store)
        if coverage_to is not None:
            store._db().execute("DELETE FROM coverage")
            store.add_coverage("twt49u", TW_DAYS[0], coverage_to)
        for day, confirmed in overrides:
            store.set_calendar_override("tw", day, "closed", "typhoon", confirmed=confirmed)
        for day in observed:
            store.upsert_closes("tw", "TAIEX", [(day, 46000.0, 500_000_000_000)], "twse")
        ctx = market_publish._Ctx(store, None, now)
        market_publish._build_progress(ctx)
        return ctx.progress
    finally:
        store.close()


def test_tw_waits_for_the_source_when_the_expected_session_has_no_taiex_close(world):
    world.store._db().execute("DELETE FROM closes WHERE market = 'tw' AND symbol = 'TAIEX' AND date = ?", (D.isoformat(),))
    world.publish()
    progress = world.snapshot()["progress"]
    assert (progress["tw_day"], progress["tw_expected_day"], progress["tw_waiting_for_source"]) == (
        TW_DAYS[-2].isoformat(), "2026-10-16", True)


def test_tw_progress_is_unknown_without_a_calendar_or_any_taiex_close(tmp_path):
    progress = tw_ctx_progress(tmp_path / "a", at_tpe(2026, 10, 23, 16, 0), seed=False)  # 沒有休市表、沒有收盤
    assert (progress["tw_day"], progress["tw_expected_day"], progress["tw_waiting_for_source"],
            progress["tw_exrights_pending"]) == (None, None, None, None)
    assert (progress["tw_closed_today"], progress["tw_closed_confirmed"]) == (False, None)  # 沒有覆寫：不下「休市」的結論
    # 有休市表、沒有加權收盤：知道預期的場次，不知道正式資料日
    (tmp_path / "b").mkdir()
    store = market_store.Store(tmp_path / "b" / "t2.sqlite", now_fn=Clock(PUB_NOW))
    try:
        seed_tw(store)
        store._db().execute("DELETE FROM closes WHERE market = 'tw' AND symbol = 'TAIEX'")
        ctx = market_publish._Ctx(store, None, PUB_NOW)
        market_publish._build_progress(ctx)
        assert ctx.progress["tw_expected_day"] == "2026-10-16"
        assert ctx.progress["tw_day"] is None and ctx.progress["tw_waiting_for_source"] is None
    finally:
        store.close()


def test_tw_exrights_pending_follows_the_twt49u_coverage(tmp_path):
    """旗標由 _tw_ready_day 同一套判斷導出：TWT49U 沒涵蓋到 T（最近一個台股場次）→ 0050 與成分只能顯示更早的一天。"""
    assert tw_ctx_progress(tmp_path / "a", PUB_NOW, coverage_to=TW_DAYS[-1])["tw_exrights_pending"] is False
    assert tw_ctx_progress(tmp_path / "b", PUB_NOW, coverage_to=TW_DAYS[-2])["tw_exrights_pending"] is True
    # 最近 5 個場次都沒涵蓋：0050 那一列直接暫缺（_tw_ready_day 丟 exrights）→ 照樣是「還在等」
    assert market_publish.BREADTH_LOOKBACK == 5
    assert tw_ctx_progress(tmp_path / "c", PUB_NOW, coverage_to=TW_DAYS[-9])["tw_exrights_pending"] is True


def test_tw_exrights_pending_matches_the_published_0050_row(tmp_path):
    for lag, pending in ((0, False), (1, True), (3, True)):
        root = tmp_path / f"w{lag}"
        root.mkdir()
        w = World(root)
        try:
            w.store._db().execute("DELETE FROM coverage")
            w.store.add_coverage("twt49u", TW_DAYS[0], TW_DAYS[-1 - lag])
            w.publish()
            snap = w.snapshot()
            assert snap["progress"]["tw_exrights_pending"] is pending, lag
            older = snap["rows"]["tw.etf.0050"]["data_date"] < snap["rows"]["tw.taiex"]["data_date"]
            assert older is pending, lag  # 旗標為真 ⇔ 0050 比加權舊（這份合成資料的 0050 收盤是齊的）
        finally:
            w.close()


# ── 台股休市：只有「預期交易日被覆寫成休市」才標 ─────────────────────────────────
def test_typhoon_day_is_marked_closed_but_tentative(tmp_path):
    """10/23（週五）颱風休市：管線推定（confirmed＝False）→ 傍晚 20:00 後 tw_closed_today 為真、confirmed 為假。"""
    progress = tw_ctx_progress(tmp_path, at_tpe(2026, 10, 23, 21, 0), overrides=[(date(2026, 10, 23), False)])
    assert (progress["tw_closed_today"], progress["tw_closed_confirmed"]) == (True, False)
    assert progress["tw_expected_day"] == "2026-10-22"  # 休市日不算場次：預期的最新場次是前一個交易日


def test_confirmed_override_is_marked_confirmed(tmp_path):
    progress = tw_ctx_progress(tmp_path, at_tpe(2026, 10, 23, 21, 0), overrides=[(date(2026, 10, 23), True)])
    assert (progress["tw_closed_today"], progress["tw_closed_confirmed"]) == (True, True)


def test_no_override_means_no_closed_claim(tmp_path):
    """平日 14:30 後還沒有加權資料、也沒有覆寫：沒有證據不能寫休市。"""
    progress = tw_ctx_progress(tmp_path, at_tpe(2026, 10, 23, 16, 0))
    assert (progress["tw_closed_today"], progress["tw_closed_confirmed"]) == (False, None)
    assert progress["tw_expected_day"] == "2026-10-23" and progress["tw_waiting_for_source"] is True  # 只能說「還沒取得」


@pytest.mark.parametrize(
    "now, closed_day",
    [
        (at_tpe(2026, 10, 24, 12, 0), date(2026, 10, 23)),  # 週六：前一天（週五）颱風休市，今天不是預期交易日
        (at_tpe(2026, 10, 24, 12, 0), date(2026, 10, 24)),  # 週六被寫了覆寫（不該發生）：週末照樣不標
        (at_tpe(2026, 10, 25, 12, 0), date(2026, 10, 25)),
        (at_tpe(2026, 10, 26, 12, 0), date(2026, 10, 26)),  # 10/26 補假：休市表本來就不是交易日
    ],
    ids=["saturday_after_closed_friday", "saturday", "sunday", "make_up_holiday"],
)
def test_weekends_and_scheduled_holidays_are_never_marked_closed(tmp_path, now, closed_day):
    control = tw_ctx_progress(tmp_path / "control", at_tpe(2026, 10, 23, 21, 0), overrides=[(date(2026, 10, 23), False)])
    assert control["tw_closed_today"] is True  # 對照：同一個覆寫表在預期交易日就會標
    progress = tw_ctx_progress(tmp_path / "case", now, overrides=[(closed_day, False)])
    assert (progress["tw_closed_today"], progress["tw_closed_confirmed"]) == (False, None)


def test_a_day_that_actually_traded_is_not_closed(tmp_path):
    """覆寫還在、但那天已經有成交資料（事實優先）：不標休市。"""
    progress = tw_ctx_progress(tmp_path, at_tpe(2026, 10, 23, 21, 0), overrides=[(date(2026, 10, 23), False)],
                               observed=[date(2026, 10, 23)])
    assert (progress["tw_closed_today"], progress["tw_closed_confirmed"]) == (False, None)
    assert progress["tw_day"] == "2026-10-23"


def test_calendar_table_session_ignores_overrides_and_actual_trades():
    cal = market_calendar.Calendar(
        "tw", holidays={date(2026, 10, 26): "補假"}, supported_from=date(2026, 1, 1), supported_until=date(2026, 12, 31),
        table_years=[2026], overrides={date(2026, 10, 23): "closed"}, observed=[date(2026, 10, 22)],
    )
    assert cal.is_session(date(2026, 10, 23)) is False  # 覆寫讓它不是交易日
    assert cal.table_session(date(2026, 10, 23)) is True  # 官方表說它是
    assert cal.table_session(date(2026, 10, 22)) is True
    assert cal.table_session(date(2026, 10, 24)) is False  # 週六
    assert cal.table_session(date(2026, 10, 26)) is False  # 補假
    assert cal.table_session(date(2027, 1, 4)) is None  # 表外：不知道
    assert cal.is_session(date(2026, 10, 24)) is False  # 原本的判斷不受影響


def test_old_progress_shape_passes_validate_and_new_fields_are_type_checked(world):
    world.publish()
    snap = world.snapshot()
    assert set(NEW_PROGRESS_KEYS) <= set(snap["progress"])
    assert market_publish.validate_snapshot(snap) == []
    old = json.loads(json.dumps(snap))
    for key in NEW_PROGRESS_KEYS:
        old["progress"].pop(key)
    assert market_publish.validate_snapshot(old) == []  # 舊形狀（沒有這些鍵）照樣合格
    bad_values = {
        "us_day": "10/16", "us_expected_day": 20261016, "tw_day": "2026-13-40", "tw_expected_day": "2026-10-16T00:00",
        "us_waiting_for_source": "yes", "tw_waiting_for_source": 0, "tw_closed_today": 1, "tw_closed_confirmed": "no",
        "tw_exrights_pending": [True],
    }
    assert set(bad_values) == set(NEW_PROGRESS_KEYS)
    for key, bad in bad_values.items():
        broken = json.loads(json.dumps(snap))
        broken["progress"][key] = bad
        assert f"progress_{key}" in market_publish.validate_snapshot(broken), key
        broken["progress"][key] = None  # 不知道＝null：合格
        assert market_publish.validate_snapshot(broken) == [], key


# ═════════════════════════════════════════════════════════════════════════════
# 四、落後／休市標示：bot 端的副標題（A19）
# ═════════════════════════════════════════════════════════════════════════════
WAITING = dict(us_day="2026-10-15", us_expected_day="2026-10-16", us_waiting_for_source=True)
TW_WAITING = dict(tw_day="2026-10-15", tw_expected_day="2026-10-16", tw_waiting_for_source=True)


def lagging_snapshot(**progress):
    """三檔 ETF 都停在 10/15、預期的最新場次是 10/16（來源還沒給）。"""
    snap = snapshot_with_progress(**{**WAITING, **progress})
    for sym in ("VOO", "QQQ", "SOXX"):
        snap["rows"][f"us.etf.{sym}"]["data_date"] = "2026-10-15"
    return snap


def tw_lagging_snapshot(**progress):
    snap = snapshot_with_progress(**{**TW_WAITING, **progress})
    snap["rows"]["tw.taiex"]["data_date"] = "2026-10-15"
    return snap


def test_old_snapshots_get_no_notice_at_all(md, paths):
    waiting = lagging_snapshot(**TW_WAITING)
    waiting["rows"]["tw.taiex"]["data_date"] = "2026-10-15"
    cards = summary_cards(md, paths, waiting)  # 對照：有證據就寫
    assert "尚未取得" in cards[0][1] and "尚未取得" in cards[1][1]
    base = summary_cards(md, paths, make_snapshot(DASH_NOW))
    assert base[0][1] is None and base[1][1] is None  # 「往左滑」在重點摘要那張：美股、台股都沒有副標
    for progress in ({}, {"us_members": [520, 520]}):
        snap = make_snapshot(DASH_NOW)
        snap["progress"] = {**snap["progress"], **progress}
        for key in NEW_PROGRESS_KEYS:
            snap["progress"].pop(key, None)
        assert [card[:2] for card in summary_cards(md, paths, snap)] == [card[:2] for card in base]
    # 壞掉的 progress（不是 dict、鍵的型別不對）：不丟例外、不加標示
    for broken in ("x", [1], {"us_waiting_for_source": "yes", "us_expected_day": 5, "tw_closed_today": 1}):
        snap = make_snapshot(DASH_NOW)
        snap["progress"] = broken
        assert [card[:2] for card in summary_cards(md, paths, snap)] == [card[:2] for card in base]


def test_us_waiting_notice_goes_in_the_subtitle_not_the_title(md, paths):
    cards = summary_cards(md, paths, lagging_snapshot())
    title, subtitle, body, _footer = cards[0]
    assert title == "美股（2/5）｜10/15 收盤"  # 標題不換行：只有副標多一句
    assert subtitle == "10/16 收盤資料尚未取得"  # 「往左滑」在重點摘要那張：美股只剩這一句
    assert "尚未取得" not in "".join(body)
    assert cards[1][1] is None  # 台股沒有這一句
    assert md.row_width(subtitle) == 11.0 <= 24  # 一行放得下，不會換行


def test_the_notice_survives_a_different_card_order(md, paths):
    """A1 軌把重點摘要插在美股前面：「往左滑」跟著第一張走，美股變第二張、副標只剩這一句。"""
    snap = lagging_snapshot(**TW_WAITING)
    snap["rows"]["tw.taiex"]["data_date"] = "2026-10-15"
    write_snapshot(paths, snap)
    headers = [texts(bubble["header"]) for bubble in md.handle("", now=DASH_NOW, paths=paths).flex["contents"]]
    assert headers[0][0].startswith("重點摘要（1/5）") and headers[0][1] == md.TEXT_SWIPE
    assert headers[1][0].startswith("美股（2/5）") and headers[1][1] == "10/16 收盤資料尚未取得"
    assert headers[2][0].startswith("台股（3/5）") and headers[2][1] == "10/16 收盤資料尚未取得"
    assert len(headers[3]) == 1


@pytest.mark.parametrize(
    "change, why",
    [
        ({"us_waiting_for_source": False}, "flag_false"),
        ({"us_waiting_for_source": None}, "flag_unknown"),
        ({"us_waiting_for_source": "yes"}, "flag_wrong_type"),
        ({"us_expected_day": None}, "no_expected_day"),
        ({"us_expected_day": "10/16"}, "bad_expected_day"),
    ],
)
def test_us_waiting_notice_needs_evidence(md, paths, change, why):
    assert "尚未取得" in summary_cards(md, paths, lagging_snapshot())[0][1]  # 對照
    assert summary_cards(md, paths, lagging_snapshot(**change))[0][1] is None, why


def test_us_notice_is_suppressed_when_the_card_already_shows_the_expected_day(md, paths):
    """暫定收盤：pipeline 說正式資料還沒到，但卡上三檔已經顯示 10/16（附「（暫定）」）→ 不能再寫「尚未取得」。"""
    assert "尚未取得" in summary_cards(md, paths, lagging_snapshot())[0][1]  # 對照：卡上停在 10/15 就寫
    snap = lagging_snapshot()
    for sym in ("VOO", "QQQ", "SOXX"):
        snap["rows"][f"us.etf.{sym}"]["data_date"] = "2026-10-16"
        snap["rows"][f"us.etf.{sym}"]["source"] = market_calc.SOURCE_CBOE_FALLBACK
    cards = summary_cards(md, paths, snap)
    assert cards[0][1] is None and cards[0][0] == "美股（2/5）｜10/16 收盤"
    assert PROVISIONAL in cards[0][2][0]
    # 只有一檔顯示到 10/16（其他停在 10/15）：卡上已經有那天的數字，也不寫
    snap = lagging_snapshot()
    snap["rows"]["us.etf.VOO"]["data_date"] = "2026-10-16"
    assert summary_cards(md, paths, snap)[0][1] is None


def test_us_notice_when_every_etf_row_is_expired(md, paths):
    snap = lagging_snapshot()
    for sym in ("VOO", "QQQ", "SOXX"):
        snap["rows"][f"us.etf.{sym}"]["stale_after"] = DASH_NOW - 1
    assert summary_cards(md, paths, snap)[0][1] == "10/16 收盤資料尚未取得"


def test_tw_waiting_notice_and_its_evidence(md, paths):
    cards = summary_cards(md, paths, tw_lagging_snapshot())
    assert cards[1][0] == "台股（3/5）｜10/15 收盤" and cards[1][1] == "10/16 收盤資料尚未取得"
    assert cards[0][1] is None  # 美股那張沒有被影響
    for change in ({"tw_waiting_for_source": False}, {"tw_waiting_for_source": None}, {"tw_waiting_for_source": "yes"},
                   {"tw_expected_day": None}, {"tw_expected_day": "10/16"}):
        assert summary_cards(md, paths, tw_lagging_snapshot(**change))[1][1] is None, change
    # 卡上加權已經是 10/16：不寫
    snap = tw_lagging_snapshot()
    snap["rows"]["tw.taiex"]["data_date"] = "2026-10-16"
    assert summary_cards(md, paths, snap)[1][1] is None
    # 加權過期（沒有資料日）而 0050 停在 10/15：照寫
    snap = tw_lagging_snapshot()
    snap["rows"]["tw.taiex"]["stale_after"] = DASH_NOW - 1
    snap["rows"]["tw.etf.0050"]["data_date"] = "2026-10-15"
    assert summary_cards(md, paths, snap)[1][1] == "10/16 收盤資料尚未取得"


def test_tw_closed_notices(md, paths):
    tentative = summary_cards(md, paths, snapshot_with_progress(tw_closed_today=True, tw_closed_confirmed=False))
    assert tentative[1][1] == "10/17 休市（暫定）" and tentative[0][1] is None  # F2-3：快照產生日的月／日，不寫「今天」
    assert tentative[1][0] == "台股（3/5）｜10/16 收盤"  # 標題不動
    assert summary_cards(md, paths, snapshot_with_progress(tw_closed_today=True, tw_closed_confirmed=True))[1][1] == "10/17 休市"
    # 沒寫 confirmed、或型別不對：照暫定寫（不替來源宣稱已確認）
    for confirmed in (None, "true", 1):
        assert summary_cards(md, paths, snapshot_with_progress(tw_closed_today=True, tw_closed_confirmed=confirmed))[1][1] == (
            "10/17 休市（暫定）")
    for today in (False, None, "yes", 1):
        assert summary_cards(md, paths, snapshot_with_progress(tw_closed_today=today, tw_closed_confirmed=True))[1][1] is None
    assert md.row_width("10/17 休市（暫定）") == 9.0 and md.row_width("12/31 休市（暫定）") == 9.0


def test_tw_closed_notice_is_only_for_the_day_the_snapshot_was_made(md, paths):
    """休市標示的日期是快照產生的那一天：隔天看到同一份快照（job 掛了）不能還寫休市。"""
    snap = snapshot_with_progress(tw_closed_today=True, tw_closed_confirmed=False)
    assert summary_cards(md, paths, snap)[1][1] == "10/17 休市（暫定）"
    snap["generated_at"] = DASH_NOW - 86400
    assert summary_cards(md, paths, snap)[1][1] is None
    del snap["generated_at"]
    assert summary_cards(md, paths, snap)[1][1] is None


def test_closed_beats_waiting_on_the_tw_card(md, paths):
    assert summary_cards(md, paths, tw_lagging_snapshot())[1][1] == "10/16 收盤資料尚未取得"  # 對照
    snap = tw_lagging_snapshot(tw_closed_today=True, tw_closed_confirmed=False)
    assert summary_cards(md, paths, snap)[1][1] == "10/17 休市（暫定）"


def test_notices_never_touch_the_other_cards_or_the_row_limits(md, paths):
    snap = lagging_snapshot(tw_closed_today=True, tw_closed_confirmed=True)
    cards = summary_cards(md, paths, snap)
    assert "尚未取得" in cards[0][1] and cards[1][1] == "10/17 休市"  # 對照：兩張都有
    assert cards[2][1] is None and cards[3][1] is None
    for _title, _subtitle, body, _footer in cards:
        assert 1 <= len(body) <= 6 and all(md.row_width(text) <= 24 for text in body)


def test_notices_are_fixed_sentences_that_pass_every_check():
    forms = {
        "waiting": market_calc.TEXT_CLOSE_PENDING.format(md="10/23"),
        "closed": market_calc.TEXT_TW_CLOSED.format(md="10/23"),
        "tentative": market_calc.TEXT_TW_CLOSED_TENTATIVE.format(md="10/23"),
        "reason": market_calc.TEXT_TW_ETF_LAG.format(md="10/16"),
    }
    assert forms == {"waiting": "10/23 收盤資料尚未取得", "closed": "10/23 休市", "tentative": "10/23 休市（暫定）",
                     "reason": "0050／成分資料到 10/16（除權息資料待更新）"}
    for now in (datetime(2026, 10, 24, 12, tzinfo=TPE), datetime(2027, 1, 15, 12, tzinfo=TPE),
                datetime(2028, 1, 3, 12, tzinfo=TPE)):
        for text in (*forms.values(), market_calc.TEXT_CLOSE_PENDING, market_calc.TEXT_TW_ETF_LAG, market_calc.TEXT_RSI_NOTE):
            result = output_validator.validate_outbound_text(text, now=now)
            assert result.ok and result.text == text, (text, now, result.reason)
    for text in (market_calc.TEXT_CLOSE_PENDING, market_calc.TEXT_TW_CLOSED, market_calc.TEXT_TW_CLOSED_TENTATIVE,
                 market_calc.TEXT_TW_ETF_LAG, market_calc.TEXT_RSI_NOTE):
        assert text in market_calc.FIXED_TEXTS
        assert not any(word in text for word in market_calc.FORBIDDEN_WORDS)
        assert not any(str(year) in text for year in range(2020, 2040))


def test_published_snapshot_drives_the_bot_notice_end_to_end(world, md):
    """publish 寫的 progress 鍵名和 bot 讀的是同一組：10/23（週五）颱風休市，晚上 21:00 看台股那張。"""
    now = at_tpe(2026, 10, 23, 21, 0)
    world.store.set_calendar_override("tw", date(2026, 10, 23), "closed", "typhoon", confirmed=False)
    assert world.publish(now=now)["kind"] == "ok"
    progress = world.snapshot()["progress"]
    assert (progress["tw_closed_today"], progress["tw_closed_confirmed"]) == (True, False)
    card = md.handle("", now=now, paths=world.paths).flex["contents"]
    assert texts(card[2]["header"])[1] == "10/23 休市（暫定）"  # 第一張是重點摘要
    world.store.set_calendar_override("tw", date(2026, 10, 23), "closed", "market_info", confirmed=True)
    world.publish(now=now)
    assert texts(md.handle("", now=now, paths=world.paths).flex["contents"][2]["header"])[1] == "10/23 休市"
    # 週六早上看同一個颱風日：週末不標
    saturday = at_tpe(2026, 10, 24, 9, 0)
    world.publish(now=saturday)
    assert world.snapshot()["progress"]["tw_closed_today"] is False
    header = texts(md.handle("", now=saturday, paths=world.paths).flex["contents"][2]["header"])
    assert not any("休市" in text for text in header)  # 沒有「休市」；這份合成資料停在 10/16，所以只會有「尚未取得」
    assert header[1] == "10/22 收盤資料尚未取得"


def test_published_us_lag_drives_the_bot_notice_end_to_end(world, md):
    db = world.store._db()
    db.execute("DELETE FROM closes WHERE market = 'us' AND symbol IN ('VOO','QQQ','SOXX') AND date = ?", (D.isoformat(),))
    world.publish()
    cards = md.handle("", now=PUB_NOW, paths=world.paths).flex["contents"]
    assert texts(cards[1]["header"])[1] == "10/16 收盤資料尚未取得"  # 第一張是重點摘要
    assert texts(cards[1]["header"])[0] == "美股（2/5）｜10/15 收盤"


# ═════════════════════════════════════════════════════════════════════════════
# 五、publish：tw.taiex 的 rsi14、暫定標示繼承（A18、A27）
# ═════════════════════════════════════════════════════════════════════════════
def test_taiex_row_carries_rsi14_from_the_same_technicals(world):
    world.publish()
    row = world.snapshot()["rows"]["tw.taiex"]
    closes = {r.date: r.close for r in world.store.closes("tw", "TAIEX")}
    calendar = market_calendar.tw_calendar(world.store).to_trading_calendar()
    expected = market_calc.technicals(closes, calendar, D).rsi14  # 和 ETF 的技術指標同一個函式
    assert expected is not None and 0 < expected < 100
    assert row["values"]["rsi14"] == pytest.approx(expected)
    assert market_calc.row_ok("tw.taiex", row)


def test_taiex_rsi14_is_null_when_the_window_has_a_gap(world):
    """RSI 往回算的 14 個變化裡缺一天收盤 → None（鍵還在）：舊的 bot、新的 bot 都照常。"""
    world.store._db().execute("DELETE FROM closes WHERE market = 'tw' AND symbol = 'TAIEX' AND date = ?",
                              (TW_DAYS[-5].isoformat(),))
    world.publish()
    row = world.snapshot()["rows"]["tw.taiex"]
    assert row["state"] == "ok" and row["values"]["rsi14"] is None
    assert market_calc.row_ok("tw.taiex", row)


def test_row_specs_for_the_new_value_keys():
    specs = market_calc.ROW_IDS
    assert specs["tw.taiex"]["rsi14"] == "num?"
    assert specs["macro.SPY_DD"]["provisional"] == "bool" and specs["contra.us"]["provisional"] == "bool"
    assert "provisional" not in specs["contra.tw"] and "provisional" not in specs["macro.VIX"]
    assert specs["macro.SPY_DD"] == {**specs["macro.VIX"], "provisional": "bool"}  # 只在原本的形狀上多一個鍵
    ids = list(specs)
    assert ids.index("macro.MOVE") < ids.index("macro.SPY_DD") < ids.index("macro.VXTLT")  # 列的先後不變


def test_spy_drawdown_row_is_marked_when_the_d_close_is_provisional(world):
    world.publish()
    assert world.snapshot()["rows"]["macro.SPY_DD"]["values"]["provisional"] is False
    db = world.store._db()
    db.execute("DELETE FROM closes WHERE market = 'us' AND symbol = 'SPY' AND date = ?", (D.isoformat(),))
    world.store.upsert_closes("us", "SPY", [(D, 601.0, None)], "cboe", provisional=True)
    world.publish()
    row = world.snapshot()["rows"]["macro.SPY_DD"]
    assert row["state"] == "ok" and row["values"]["provisional"] is True
    assert market_calc.row_ok("macro.SPY_DD", row)
    assert "provisional" not in world.snapshot()["rows"]["macro.VIX"]["values"]  # 別的宏觀列一個字都沒變


@pytest.mark.parametrize("subject, code, provisional_symbols, expected", [
    ("VOO", "above_ma20_weak_breadth", {"VOO"}, True),
    ("VOO", "above_ma20_weak_breadth", {"QQQ"}, False),  # 別檔暫定不影響這一句
    ("QQQ", "up_low_volume", {"QQQ"}, True),
    ("SOXX", "down_high_volume", {"SOXX"}, True),
    ("SPY", "gamma_pos_vix_inverted", {"SPY"}, False),  # 這條用期權的 gamma 與 VIX，不是 D 收盤
    ("VOO", "above_ma20_weak_breadth", set(), False),
])
def test_contra_us_provisional_follows_the_hit_codes(world, monkeypatch, subject, code, provisional_symbols, expected):
    monkeypatch.setattr(market_calc, "contradictions",
                        lambda state, market=None: [market_calc.Contra(code, subject)] if market == "us" else [])
    db = world.store._db()
    for symbol in provisional_symbols:
        db.execute("DELETE FROM closes WHERE market = 'us' AND symbol = ? AND date = ?", (symbol, D.isoformat()))
        world.store.upsert_closes("us", symbol, [(D, 500.0, None)], "cboe", provisional=True)
    world.publish()
    values = world.snapshot()["rows"]["contra.us"]["values"]
    assert values["provisional"] is expected and values["codes"] == [f"{code}:{subject}"]
    assert "provisional" not in world.snapshot()["rows"]["contra.tw"]["values"]


def test_contra_us_row_always_says_whether_it_is_provisional(world):
    world.publish()
    row = world.snapshot()["rows"]["contra.us"]
    assert row["values"]["provisional"] is False and market_calc.row_ok("contra.us", row)


# ═════════════════════════════════════════════════════════════════════════════
# 六、bot：暫定標示繼承（A27）
# ═════════════════════════════════════════════════════════════════════════════
def macro_detail(md_, paths_, snap):
    write_snapshot(paths_, snap)
    return texts(md_.handle(" 新聞", now=DASH_NOW, paths=paths_).flex["contents"][2]["body"])


def test_spy_drawdown_line_gets_the_provisional_tag(md, paths):
    snap = make_snapshot(DASH_NOW)
    plain = "標普500 距高點（SPY 價格代理）：-3.2%｜10/16"
    snap["rows"]["macro.SPY_DD"]["values"]["provisional"] = True
    detail = macro_detail(md, paths, snap)
    assert f"{plain}{PROVISIONAL}" in detail and plain not in detail  # 有旗標：寫「（暫定）」
    snap["rows"]["macro.SPY_DD"]["values"]["provisional"] = False
    assert plain in macro_detail(md, paths, snap)
    del snap["rows"]["macro.SPY_DD"]["values"]["provisional"]  # 舊快照沒有這個鍵：照舊
    assert plain in macro_detail(md, paths, snap)
    snap["rows"]["macro.SPY_DD"]["values"]["provisional"] = "yes"  # 型別不對：不當成暫定
    assert plain in macro_detail(md, paths, snap)
    assert not any(PROVISIONAL in text for text in macro_detail(md, paths, make_snapshot(DASH_NOW)))


def contra_snapshot(code, *, provisional):
    snap = make_snapshot(DASH_NOW)
    sentence = market_calc.CONTRA_TEXT[code.split(":")[0]].format(subject=market_calc.SUBJECT_NAME[code.split(":")[1]])
    snap["rows"]["contra.us"]["values"] = {"codes": [code], "text": sentence, "provisional": provisional}
    return snap, sentence


def test_contra_sentence_on_the_us_card_gets_the_tag_when_it_fits(md, paths):
    snap, sentence = contra_snapshot("up_low_volume:VOO", provisional=True)
    assert sentence == "標普500上漲但量偏少" and md.row_width(sentence + PROVISIONAL) <= 24
    body = summary_cards(md, paths, snap)[0][2]
    assert body[-1] == sentence + PROVISIONAL
    snap, _ = contra_snapshot("up_low_volume:VOO", provisional=False)
    cards = summary_cards(md, paths, snap)
    assert cards[0][2][-1] == sentence and market_calc.TEXT_PROVISIONAL_CLOSE not in cards[0][3]


def test_contra_sentence_too_wide_for_the_tag_moves_the_tag_to_a_footer_note(md, paths):
    snap, sentence = contra_snapshot("above_ma20_weak_breadth:QQQ", provisional=True)
    assert md.row_width(sentence) <= 24 < md.row_width(sentence + PROVISIONAL)  # 放不下「（暫定）」
    _title, _subtitle, body, footer = summary_cards(md, paths, snap)[0]
    assert body[-1] == sentence and all(md.row_width(text) <= 24 for text in body)
    assert market_calc.TEXT_PROVISIONAL_CLOSE in footer  # 改放 notes 的固定句
    snap, _ = contra_snapshot("above_ma20_weak_breadth:QQQ", provisional=False)
    assert market_calc.TEXT_PROVISIONAL_CLOSE not in summary_cards(md, paths, snap)[0][3]


def test_oneliner_is_tagged_only_when_voo_itself_is_provisional(md, paths):
    """2026-10-28 批 1 審查 F2-9（取代原本釘死『一句話不標、由 A1 軌改』的 test_oneliner_is_left_to_the_other_track；那一軌沒有做，
    VOO 是暫定收盤時 VOO 那一列有（暫定）、同一張卡的一句話沒有）。一句話只看 VOO：contra.us 標了暫定、VOO 是正式收盤 → 還是不標；
    VOO 是暫定 → 標（太寬就改放 footer 的固定句）。"""
    snap, sentence = contra_snapshot("up_low_volume:VOO", provisional=True)
    assert summary_cards(md, paths, snap)[0][2][-1] == sentence + PROVISIONAL  # 對照：有矛盾句就標
    usual = "標普收在月線上、成分股站上月線 62%、量偏少"
    snap = make_snapshot(DASH_NOW)
    snap["rows"]["contra.us"]["values"] = {"codes": [], "text": None, "provisional": True}
    _title, _subtitle, body, footer = summary_cards(md, paths, snap)[0]
    assert body[-1] == usual and market_calc.TEXT_PROVISIONAL_CLOSE not in footer  # VOO 是正式收盤：不標、footer 也沒有說明
    snap["rows"]["us.etf.VOO"]["source"] = market_calc.SOURCE_CBOE_FALLBACK  # VOO 暫定：一句話 21.0＋4.0 放不下，句子不動、footer 說明
    _title, _subtitle, body, footer = summary_cards(md, paths, snap)[0]
    assert body[-1] == usual and footer.count(market_calc.TEXT_PROVISIONAL_CLOSE) == 1
    snap["rows"]["oneliner.us"]["values"]["text"] = "標普收在月線上、量偏少"  # 短的句子放得下：標在句尾，footer 不重複
    _title, _subtitle, body, footer = summary_cards(md, paths, snap)[0]
    assert body[-1] == "標普收在月線上、量偏少" + PROVISIONAL and market_calc.TEXT_PROVISIONAL_CLOSE not in footer


def test_etf_detail_contra_rows_get_the_tag(md, paths):
    snap, sentence = contra_snapshot("above_ma20_weak_breadth:VOO", provisional=True)
    write_snapshot(paths, snap)
    shown = texts(md.handle(" VOO", now=DASH_NOW, paths=paths).flex)
    assert sentence + PROVISIONAL in shown
    snap, _ = contra_snapshot("above_ma20_weak_breadth:VOO", provisional=False)
    write_snapshot(paths, snap)
    assert sentence in texts(md.handle(" VOO", now=DASH_NOW, paths=paths).flex)


def test_provisional_flag_in_values_marks_the_view(md):
    snap = make_snapshot(DASH_NOW)
    snap["rows"]["macro.SPY_DD"]["values"]["provisional"] = True
    ctx = md._Ctx(snap, DASH_NOW)
    assert ctx.row("macro.SPY_DD").provisional is True and ctx.row("macro.VIX").provisional is False
    snap["rows"]["macro.SPY_DD"]["values"]["provisional"] = 1  # 只認 true
    assert md._Ctx(snap, DASH_NOW).row("macro.SPY_DD").provisional is False
    snap["rows"]["us.etf.VOO"]["source"] = market_calc.SOURCE_CBOE_FALLBACK  # 原本的來源判斷照舊
    assert md._Ctx(snap, DASH_NOW).row("us.etf.VOO").provisional is True


# ═════════════════════════════════════════════════════════════════════════════
# 七、每個標示同時開著：大小、validator、main 的真閘門（三個年份）
# ═════════════════════════════════════════════════════════════════════════════
def fully_marked_snapshot(now, *, worst=False):
    """所有標示同時出現：美股三檔停在前一天、預期的最新場次沒給；加權也停在前一天；今天休市（暫定）；0050 比加權舊且
    除權息資料還沒到；SPY 距高點與美股矛盾句都是暫定；RSI 都是 100。"""
    snap = worst_snapshot(now) if worst else make_snapshot(now)
    today = datetime.fromtimestamp(now, TPE).date()
    expected, behind = today - timedelta(days=1), today - timedelta(days=2)
    rows = snap["rows"]
    for sym in ("VOO", "QQQ", "SOXX"):
        rows[f"us.etf.{sym}"]["data_date"] = behind.isoformat()
        rows[f"us.etf.{sym}"]["values"]["rsi14"] = 100.0
    rows["tw.taiex"]["values"]["rsi14"] = 100.0
    rows["tw.taiex"]["data_date"] = expected.isoformat()
    rows["tw.etf.0050"]["data_date"] = behind.isoformat()
    rows["tw.etf.0050"]["values"]["rsi14"] = 100.0
    rows["macro.SPY_DD"]["values"]["provisional"] = True
    rows["contra.us"]["values"] = {"codes": ["above_ma20_weak_breadth:SOXX", "up_low_volume:VOO"], "text": None,
                                   "provisional": True}
    snap["progress"].update(
        us_day=behind.isoformat(), us_expected_day=expected.isoformat(), us_waiting_for_source=True,
        tw_day=expected.isoformat(), tw_expected_day=expected.isoformat(), tw_waiting_for_source=False,
        tw_closed_today=True, tw_closed_confirmed=False, tw_exrights_pending=True,
    )
    return snap


@pytest.mark.parametrize("variant", ["normal", "worst"])
def test_every_marker_at_once_stays_within_the_size_and_row_limits(md, paths, variant):
    write_snapshot(paths, fully_marked_snapshot(DASH_NOW, worst=variant == "worst"))
    replies = render_all(md, paths, DASH_NOW, FLEX_BODIES)
    summary = replies[""].flex
    cards = [(texts(b["header"]), texts(b["body"]), texts(b["footer"])) for b in summary["contents"]
             if not texts(b["header"])[0].startswith("重點摘要")]  # 第一張是 A1 的重點摘要：這裡從美股開始數
    assert "尚未取得" in cards[0][0][1] and cards[1][0][1] == "10/17 休市（暫定）"  # 標示真的都開著
    assert any("RSI 100" in text for text in cards[0][1] + cards[1][1])
    for header, body, _footer in cards:
        assert 1 <= len(body) <= 6 and all(md.row_width(text) <= 24 for text in body)
    for _header, body, _footer in cards[:2]:  # 美股、台股兩張（這一軌改的）：最壞值也不能被切成「…」
        assert not any("…" in text for text in body), body
    for body, reply in replies.items():
        assert reply.flex is not None, (body, reply.text)
        assert reply.render_errors == (), (body, reply.render_errors)
        assert size(reply.flex) <= 28 * 1024, body
        if body == "":
            assert all(md.card_bytes(b) <= md.SUMMARY_BUBBLE_MAX_BYTES for b in reply.flex["contents"])
        assert card_pages.fits_line_limits(card_pages.with_page_button(reply.flex, LONG_PAGE_URL)), body


@pytest.mark.parametrize("now", ALL_NOWS, ids=["2026-10-17", "2027-01-15", "2028-01-03"])
def test_every_marker_at_once_passes_the_validator_and_the_main_gate(md, paths, now):
    write_snapshot(paths, fully_marked_snapshot(now))
    at = datetime.fromtimestamp(now, TPE)
    for body, reply in render_all(md, paths, now, FLEX_BODIES).items():
        if body == " 選舉" and reply.flex is None:  # 2027-02-01 之後：本屆選舉已結束，只回一句固定句
            assert reply.text == md.TEXT_ELECTION_ENDED
        else:
            assert reply.flex is not None and reply.render_errors == (), body
        assert_main_gate(reply)
        for value in reply_strings(reply):
            result = output_validator.validate_outbound_text(value, now=at)
            assert result.ok and result.text == value, (body, value, result.reason)
    summary = md.handle("", now=now, paths=paths).flex["contents"]
    today = at.date()  # 休市標示寫快照產生日（＝這一天）的月／日（F2-3）
    assert "尚未取得" in texts(summary[1]["header"])[1]
    assert texts(summary[2]["header"])[1] == f"{today.month}/{today.day} 休市（暫定）"  # 第一張是重點摘要
    detail = texts(md.handle(" 0050", now=now, paths=paths).flex)
    assert any("除權息資料待更新" in text for text in detail) and RSI_NOTE in detail
