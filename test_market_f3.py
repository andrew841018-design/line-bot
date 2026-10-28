"""/盤勢 批 1 第 2 輪複審（4e984e8..38e7223）後的小修正 F3（2026-10-28）的測試：F3-1～F3-3、F3-5、F3-7（F3-4 已退回）。

審查原文：reviews/w1_review_r2_seat4.md 的 R2-I1、R2-N1、R2-N2；reviews/w1_review_r3_A.md 的 I-1（帶日期時的暫定標籤）；reviews/w1_review_r2_gp1.md 的 I-2、N7、N9；
reviews/w1_review_r2_gp2.md 的 N2。判定：reviews/w1_r2_triage.md。F3-6（everything_on 量整個摘要輪播）在 test_market_f2_display。
這個檔案單獨一份；每個測試都在基底（w1-int 38e7223）上是紅的、修正後通過，「對照」性質的測試（確認原本就對的情形沒被改壞）
會在 docstring 或註解裡說明。字寬一律用 market_dashboard.row_width 量；日期用 fixture 的 NOW（台北 10/17 週六 16:30）：
最新場次 DAY＝10/16、前一天 PREV＝10/15。
"""

from __future__ import annotations

import itertools
import json
import re
import time
from datetime import date, datetime

import pytest

import market_calc
import market_http
import market_paths
import output_validator
from test_market_dashboard import (  # noqa: F401  md／paths 是 fixture（用名字被 pytest 找到）
    ALL_NOWS,
    DAY,
    ETF_STATES,
    NOW,
    PREV,
    TPE,
    US_SYMS,
    digest_bubble,
    digest_snapshot,
    md,
    paths,
    row_ctx,
    set_etf,
    summary_bubble,
    texts,
    write_snapshot,
)
from test_market_pipeline_taco import READY, env, pipe, src  # noqa: F401  env 是 fixture

PROVISIONAL = market_calc.TEXT_PROVISIONAL
CBOE = market_calc.SOURCE_CBOE_FALLBACK


def passes_every_gate(md_, text):
    """固定句的閘門：FIXED_TEXTS、禁用字、不寫年份、2026／2027／2028 的 validator。"""
    assert text in market_calc.FIXED_TEXTS and not md_.has_forbidden(text), text
    assert not any(str(year) in text for year in range(2020, 2040)), text
    for now in ALL_NOWS:
        result = output_validator.validate_outbound_text(text, now=datetime.fromtimestamp(now, TPE))
        assert result.ok and result.text == text, (text, now, result.reason)


# ═══ F3-1：第 1 列的「（暫定）」放在列名（和日期）後面、冒號前面 ═════════════════════════════════════════


def us_row(md_, snap):
    return md_._digest_us_row(row_ctx(md_, snap))[0]


def us_states(*states, day=DAY):
    snap = digest_snapshot()
    for sym, state in zip(US_SYMS, states):
        set_etf(snap, sym, state, day)
    return snap


def test_f3_1_only_voo_provisional_tags_the_row_name_not_the_last_group(md):
    """seat4 R2-I1：只有 VOO 是暫定收盤、SOXX 在月線下時，以前寫成「…，半導體在月線下（暫定）」，看起來是半導體暫定，
    而且和三檔都暫定時一字不差。"""
    snap = us_states("up", "up", "down")
    assert us_row(md, snap) == "美股：標普、那指在月線上，半導體在月線下"  # 對照：沒有暫定
    snap["rows"]["us.etf.VOO"]["source"] = CBOE
    text = us_row(md, snap)
    assert text == "美股（暫定）：標普、那指在月線上，半導體在月線下" and md.row_width(text) == 24.0


@pytest.mark.parametrize("states, expected", [
    (("up", "stale", "missing"), "美股（暫定）：標普在月線上，那指過期，半導體暫缺"),  # 以前「…，半導體暫缺（暫定）」
    (("down", "stale", "stale"), "美股（暫定）：標普在月線下，那指、半導體過期"),  # 以前「…，那指、半導體過期（暫定）」
])
def test_f3_1_the_tag_never_follows_an_expired_or_missing_group(md, states, expected):
    """gp1 I-2：VOO 是暫定收盤、列尾那一組是過期或暫缺的檔時，以前「（暫定）」接在「暫缺／過期」後面。"""
    snap = us_states(*states)
    snap["rows"]["us.etf.VOO"]["source"] = CBOE
    text = us_row(md, snap)
    assert text == expected and md.row_width(text) <= 24


def test_f3_1_the_tag_only_ever_sits_right_before_the_colon(md):
    """三檔各 上／下／過期／暫缺／月線狀態缺 × 落後與否 × 各檔暫定與否（2,000 種）：「（暫定）」只出現在冒號前面（列名或日期後面），
    冒號後面一個都沒有；有標的列都 ≤24、沒有被切成「…」。"""
    tagged = 0
    for lag in (False, True):
        for states in itertools.product(ETF_STATES, repeat=3):
            snap = us_states(*states, day=PREV if lag else DAY)
            for flags in itertools.product((False, True), repeat=3):
                for sym, flag in zip(US_SYMS, flags):
                    snap["rows"][f"us.etf.{sym}"]["source"] = CBOE if flag else "合成"
                text = us_row(md, snap)
                assert md.row_width(text) <= 24 and "…" not in text, text
                head, colon, body = text.partition("：")
                assert colon and PROVISIONAL not in body, (lag, states, flags, text)
                if PROVISIONAL in head:
                    assert head in ("美股" + PROVISIONAL, f"美股 {PREV.month}/{PREV.day}{PROVISIONAL}"), text
                    tagged += 1
    assert tagged > 100  # 真的有標到（不是每一種都放不下）


def test_f3_1_a_dated_row_is_tagged_only_when_that_day_is_provisional(md):
    """第 3 輪 r3-A I-1：「（暫定）」緊貼列上的日期，讀起來是「那天的收盤是暫定的」。每個交易日收盤後都有一段時間是
    VOO 10/15 正式、QQQ／SOXX 10/16 CBOE 暫定：列上寫的是 10/15，那天沒有暫定收盤，所以不加，由 footer 的固定句說明。"""
    snap = us_states("up", "up", "up")
    set_etf(snap, "VOO", "up", PREV)
    for sym in ("QQQ", "SOXX"):
        snap["rows"][f"us.etf.{sym}"]["source"] = CBOE
    assert us_row(md, snap) == "美股 10/15：標普、那指、半導體都在月線上"
    assert market_calc.TEXT_PROVISIONAL_CLOSE in texts(digest_bubble(md, snap)["footer"])
    # 對照：列上那天（VOO 10/15）就是暫定收盤時照樣加
    snap = us_states("up", "up", "up")
    set_etf(snap, "VOO", "up", PREV)
    snap["rows"]["us.etf.VOO"]["source"] = CBOE
    text = us_row(md, snap)
    assert text == "美股 10/15（暫定）：標普、那指、半導體都在月線上" and md.row_width(text) == 24.0


# ═══ F3-2：川普壓力指數落後時，用短名稱留住「高壓」 ═════════════════════════════════════════════════════


def taco_snapshot(value=9.0005, percentile=88, *, day=PREV, status_key="high"):
    """digest_snapshot（美股各列 10/16，預期的最新場次 10/16），川普壓力指數換成指定的值；day=PREV＝落後一個場次。"""
    snap = digest_snapshot()
    row = snap["rows"]["macro.TACO"]
    values = row["values"]
    values.update(value=value, percentile=percentile, status_key=status_key,
                  max=max(values["max"], value), min=min(values["min"], value))
    row["data_date"] = day.isoformat()
    return snap


@pytest.mark.parametrize("value, percentile, day, expected, width", [
    (9.0005, 88, PREV, "川普壓力 10/15 +9.0（高壓，任期內第 88 百分位）", 23.5),  # 新寫法：短名稱，狀態詞留著
    (17.3261, 99, PREV, "川普壓力 10/15 +17.3（高壓，任期內第 99 百分位）", 24.0),  # 剛好放得下
    (-4.9868, 0, PREV, "川普壓力 10/15 -5.0（高壓，任期內第 0 百分位）", 23.0),
    # 以下四個是對照（基底上也是這樣）：新寫法放不下時照舊退到②；①帶日期放得下時照舊用①
    (17.3261, 100, PREV, "川普壓力指數 10/15 +17.3（任期內第 100 百分位）", 23.5),  # 新寫法 24.5
    (100.0, 80, PREV, "川普壓力指數 10/15 +100.0（任期內第 80 百分位）", 23.5),  # 三位數：新寫法 24.5
    (-100.0, 100, PREV, "川普壓力指數 10/15 -100.0（任期內第 100 百分位）", 24.0),  # 最壞值：新寫法 25.0，②剛好 24.0
    (9.0005, 7, date(2026, 1, 5), "川普壓力指數 1/5 +9.0（高壓，任期內第 7 百分位）", 24.0),  # 月日、百分位都是個位數
])
def test_f3_2_a_lagging_index_keeps_the_status_word_when_the_short_name_fits(md, value, percentile, day, expected, width):
    """seat4 R2-N1、gp1 N9：落後時（每個交易日收盤後至少 4 小時）①加日期是 25.5 放不下，以前直接退到②，三個地方的「高壓」一起
    消失，看起來和狀態不是 high 的日子一樣。①和②之間加「短名稱＋日期＋數字（狀態詞，位階）」，≤24 才用。"""
    ctx = row_ctx(md, taco_snapshot(value, percentile, day=day))
    line = md._taco_line(ctx)
    assert line == expected and md.row_width(line) == width
    assert md._digest_trump_row(ctx)[0] == line  # 重點摘要第 6 列（宏觀 5/5 頁尾也用 _taco_line）


def test_f3_2_the_macro_footer_keeps_the_status_word_too(md, paths):
    write_snapshot(paths, taco_snapshot())
    reply = md.handle("", now=NOW, paths=paths)
    footer = texts(summary_bubble(md, reply.flex, "宏觀與事件")["footer"])
    assert footer[0] == "川普壓力 10/15 +9.0（高壓，任期內第 88 百分位）"


def test_f3_2_no_status_word_or_no_date_keeps_the_old_lines(md):
    """對照（基底上就是綠的）：新寫法只在「帶日期而且有狀態詞」時才加。沒有狀態詞照舊從②開始；不帶日期照舊是①
    （最壞值 24.0 也放得下）。"""
    assert md._taco_line(row_ctx(md, taco_snapshot(status_key=None))) == "川普壓力指數 10/15 +9.0（任期內第 88 百分位）"
    for value, text in ((9.0005, "+9.0"), (100.0, "+100.0"), (-100.0, "-100.0")):
        line = md._taco_line(row_ctx(md, taco_snapshot(value, 100, day=DAY)))
        assert line == f"川普壓力指數 {text}（高壓，任期內第 100 百分位）" and md.row_width(line) <= 24


@pytest.mark.parametrize("value, percentile, text", [(9.0005, 88, "+9.0"), (100.0, 100, "+100.0"), (-100.0, 100, "-100.0")])
def test_f3_2_the_detail_footer_starts_with_the_full_undated_line(md, paths, value, percentile, text):
    """新聞與宏觀 3/3 的頁尾：第一行是不帶日期的完整寫法（最壞 24.0，「高壓」永遠在），第二行照舊「資料日 10/15」；
    以前第一行和摘要同一句，落後時「高壓」不見、日期又寫兩次。"""
    write_snapshot(paths, taco_snapshot(value, percentile))
    footer = texts(md.handle(" 新聞", now=NOW, paths=paths).flex["contents"][2]["footer"])
    assert footer[:2] == [f"川普壓力指數 {text}（高壓，任期內第 {percentile} 百分位）", "資料日 10/15"]


# ═══ F3-3：說明句寫出主詞、涵蓋百分位 ══════════════════════════════════════════════════════════════════

MEANING = "川普壓力指數越高代表壓力越大；數值和百分位都不是退讓的機率"


def test_f3_3_the_meaning_sentence_names_the_index_and_covers_the_percentile(md, paths):
    """seat4 R2-N2：摘要裡這句和第 6 列中間最多隔 5 行，「數值越高…」沒有主詞；最容易被讀錯的「第 80 百分位＝80% 會 TACO」也沒涵蓋。"""
    assert market_calc.TEXT_TACO_MEANING == MEANING
    assert len(MEANING) <= md.NOTE_MAX  # 宏觀 5/5 用一般註記的上限 60（摘要與 3/3 是 TACO_NOTE_MAX 80）
    passes_every_gate(md, MEANING)
    snap = digest_snapshot()
    footer = texts(digest_bubble(md, snap)["footer"])  # 摘要：說明句接著來源句
    assert footer[footer.index(MEANING) + 1] == market_calc.TEXT_TACO_SOURCE
    write_snapshot(paths, snap)
    macro = texts(summary_bubble(md, md.handle("", now=NOW, paths=paths).flex, "宏觀與事件")["footer"])
    assert macro[:2] == ["川普壓力指數 +9.0（高壓，任期內第 88 百分位）", MEANING]  # 5/5：緊接在指數那一行下面
    detail = texts(md.handle(" 新聞", now=NOW, paths=paths).flex["contents"][2]["footer"])
    assert detail[detail.index(MEANING) + 1] == market_calc.TEXT_TACO_SOURCE  # 3/3：位階下面、來源上面
    assert md.status_summary(now=NOW, paths=paths)["external_dropped"] == 0  # 沒有被 ctx.note 的長度上限悄悄丟掉


# ═══ F3-5：F1-9 範圍外的三個年月正規式也只收 ASCII 數字 ═══════════════════════════════════════════════════

FULLWIDTH_YM, ARABIC_YM = "２０２６-０９", "٢٠٢٦-٠٩"


def test_f3_5_the_dashboard_month_regex_takes_only_ascii_digits(md):
    """gp1 N7：_month_of 用 int()，int() 收全形與阿拉伯數字，「２０２６-０９」以前會變成 9 月。"""
    assert md._MONTH_RE.flags & re.ASCII
    assert md._month_of("2026-09") == 9 and md._month_of("202609") == 9  # 對照
    for text in (FULLWIDTH_YM, "２０２６０９", "2026-０９", ARABIC_YM, "٢٠٢٦09"):
        assert md._month_of(text) is None, text


def test_f3_5_the_season_sample_regex_takes_only_ascii_digits(md):
    """季節性卡的樣本起迄：全形的「２０１６-１１」以前會被轉成 ASCII 的「2016-11」印上卡。"""
    assert md._YEAR_MONTH_RE.flags & re.ASCII
    assert md._year_month_text("2016-11") == "2016-11"  # 對照
    for text in ("２０１６-１１", "2016-１１", "٢٠١٦-١١"):
        assert md._year_month_text(text) is None, text
    assert md._season_range({"first": "２０１６-１１", "last": "2025-12"}) == md.TEXT_MISSING


def test_f3_5_the_seasonality_check_regex_takes_only_ascii_digits():
    assert market_calc._YEAR_MONTH_RE.flags & re.ASCII
    assert market_calc._year_month("2016-11") == (2016, 11)  # 對照
    for text in ("２０１６-１１", "2016-１１", "٢٠١٦-١١"):
        assert market_calc._year_month(text) is None, text


# ═══ F3-7：TACO 用白名單停用後，taco_block_streak 回 0 ═══════════════════════════════════════════════════


def test_f3_7_the_job_checks_the_same_host_as_the_source():
    """監督程序不 import 來源模組，主機名另外寫一份（和 TACO_STATE 同一個作法）：兩邊要一樣。"""
    from jobs import market_snapshot as job

    assert job.TACO_HOST == src.HOST == "ocmacro.com"


def test_f3_7_with_the_host_off_the_whitelist_the_block_streak_reads_zero(env, md, monkeypatch):
    """gp2 N2：照 F1-4 把 ocmacro.com 拿出 ALLOWED_HOSTS 之後，task_state 的 block_streak 不會再被清掉（只有站方回應才清），
    以前 outcome 一直帶舊值，每日維護的「≥ 2」天天報「連續被擋」，看不出其實是停用。"""
    from jobs import market_snapshot as job

    env.store.set_task_state(pipe.STATE_NAME, {"block_streak": 5})

    def streaks():
        supervisor = job.Supervisor(env.paths, job.parse_args(["--root", str(env.paths.state_dir.parent)]), -1,
                                    wall=lambda: READY)
        outcome = supervisor.compose_outcome(code="unchecked", collect="ok", publish="ok", started_at=READY,
                                             locked_at=time.monotonic())
        assert "taco_status_error" not in outcome and job.sanitize(outcome) == outcome
        market_paths.write_private_atomic(env.paths.outcome, json.dumps(outcome).encode())
        summary = md.status_summary(now=READY, paths=env.paths)["outcome"]
        return job._taco_status(env.store)["taco_block_streak"], outcome["taco_block_streak"], summary["taco_block_streak"]

    assert streaks() == (5, 5, 5)  # 對照：主機在白名單，照實帶出去
    allowed = market_http.ALLOWED_HOSTS
    monkeypatch.setattr(market_http, "ALLOWED_HOSTS", allowed - {"ocmacro.com"})
    assert streaks() == (0, 0, 0)
    assert env.store.task_state(pipe.STATE_NAME)["block_streak"] == 5  # 只是不報，task_state 沒動
    monkeypatch.setattr(market_http, "ALLOWED_HOSTS", allowed)  # 不用 undo()：md fixture 也用同一個 monkeypatch
    assert streaks() == (5, 5, 5)  # 主機放回白名單：照舊
