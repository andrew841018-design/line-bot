"""C2a 冷凍條件試算（jobs/stock_picks_review_cooldown.py）：段的走法、條件的邊界、週報這一段的寫法。

規格：ops/state/line_bot_stock_backtest_20261028/PLAN_B2C2.md §3.2–§3.4。週報的整合（段落位置、出錯只換一行、整份放得進
三段）在 test_stock_picks_review_returns.py 與 test_stock_picks_review.py。
"""

from __future__ import annotations

import math

from jobs import stock_picks_review_cooldown as cooldown
from jobs import stock_picks_review_returns as returns


def entry(code: str, t: str, th, r, p=0.0, *, b=0.0, family: str = "tw_ai", h: int = 20, skip=None,
          p_status: str = "ok") -> returns.Entry:
    return returns.Entry(family, "tw", t, code, h, r, b, skip, p_status, p, th)


def chain(code: str, gaps: list[float], start_day: int = 1, *, p: float = 0.0, b: float = 0.0,
          family: str = "tw_ai") -> list[returns.Entry]:
    """一檔一連串不重疊的段：第 i 段 t＝10/(start_day + 2i)、th＝t 的隔天（下一段的 t 一定不早於 th）；r＝p＋gap。"""
    out = []
    for i, gap in enumerate(gaps):
        day = start_day + 2 * i
        out.append(entry(code, f"2026-10-{day:02d}", f"2026-10-{day + 1:02d}", p + gap, p, b=b, family=family))
    return out


# ── 段的走法 ─────────────────────────────────────────────────────────────────
def test_overlapping_windows_are_skipped_and_t_equal_to_th_starts_a_new_segment():
    rows = [
        entry("2330", "2026-10-01", "2026-10-29", -0.08),
        entry("2330", "2026-10-15", "2026-11-12", -0.30),  # t 早於上一段的 th：重疊，略過
        # th 的前一天：E 剛好等於上一段的 th，窗口只是相接；照 PLAN 也略過（段距多一個交易日，debate r3）
        entry("2330", "2026-10-28", "2026-11-25", -0.30),
        entry("2330", "2026-10-29", "2026-11-26", -0.04),  # t＝上一段的 th：新的一段
        entry("2330", "2026-11-26", "2026-12-24", -0.065),
    ]
    eps = cooldown.episodes(reversed(rows))  # 輸入順序不影響：照 (代號, t) 走
    assert [e.t for e in eps["2330"]] == ["2026-10-01", "2026-10-29", "2026-11-26"]


def test_only_usable_rows_become_segments_and_nothing_waits():
    rows = [
        entry("A", "2026-10-01", "2026-10-29", -0.1, skip="missing"),
        entry("B", "2026-10-01", "2026-10-29", -0.1, p_status="pool_missing"),
        entry("C", "2026-10-01", "2026-10-29", -0.1, p_status="pending"),  # p 還沒算完：不等，直接略過
        entry("D", "2026-10-01", "2026-10-29", -0.1, h=5),
        entry("E", "2026-10-01", None, -0.1),  # 帳上沒有出場日：放不進窗口
        entry("F", "2026-10-01", "2026-10-29", math.nan),
        entry("G", "2026-10-01", "2026-10-29", -0.1, p=math.inf),
        entry("H", "2026-10-01", "2026-10-29", None),
        entry("I", "2026-10-01", "2026-10-29", True),  # bool 不是報酬（和 returns._finite 一樣）
        entry("OK", "2026-10-01", "2026-10-29", -0.1),
    ]
    assert list(cooldown.episodes(rows)) == ["OK"]


def test_a_skipped_row_does_not_block_the_next_window():
    """略過的列（skip、pending）不算一段，所以也不擋住它窗口裡的下一筆。"""
    rows = [entry("2330", "2026-10-01", "2026-10-29", -0.1, p_status="pending"),
            entry("2330", "2026-10-15", "2026-11-12", -0.1)]
    assert [e.t for e in cooldown.episodes(rows)["2330"]] == ["2026-10-15"]


def test_since_restarts_only_that_code_before_walking():
    """放回之後重新起算（C2b）：只動那一檔，而且是先篩再走：重新起算日之後的第一筆就是新的第一段。"""
    rows = [entry("A", "2026-10-01", "2026-10-29", -0.1), entry("A", "2026-10-20", "2026-11-17", -0.1),
            entry("A", "2026-11-17", "2026-12-15", -0.1)] + chain("B", [-0.2, -0.2])
    eps = cooldown.episodes(rows, since={"A": "2026-10-10"})
    assert [e.t for e in eps["A"]] == ["2026-10-20", "2026-11-17"]  # 10/20 本來被 10/01 那段的窗口蓋住
    assert [e.t for e in eps["B"]] == ["2026-10-01", "2026-10-03"]  # 別的代號不受影響
    assert [e.t for e in cooldown.episodes(rows, since={"A": "2026-10-20"})["A"]] == ["2026-10-20", "2026-11-17"]


# ── 條件的邊界 ───────────────────────────────────────────────────────────────
def test_exactly_minus_fifty_permille_is_eligible_and_rounding_matches_the_card():
    assert cooldown.eligible(chain("A", [-0.05, -0.05, -0.05]))
    assert cooldown.eligible(chain("A", [-0.0495, -0.0495, -0.0495]))  # 四捨五入到 -50‰
    assert not cooldown.eligible(chain("A", [-0.0494, -0.0494, -0.0494]))  # -49‰
    assert returns._permille(math.fsum([-0.0495] * 3) / 3) == -50


def test_exactly_two_thirds_behind_is_enough_and_less_is_not():
    assert cooldown.eligible(chain("A", [-0.20, -0.20, 0.01]))  # 3 段輸 2 段：剛好 2/3
    assert not cooldown.eligible(chain("A", [-0.40, 0.01, 0.01]))  # 平均落後很多，但只輸 1/3
    assert cooldown.eligible(chain("A", [-0.1, -0.1, -0.1, -0.1, 0.01, 0.01]))  # 6 段輸 4 段
    assert not cooldown.eligible(chain("A", [-0.2, -0.2, -0.2, 0.01, 0.01, 0.01]))  # 6 段輸 3 段
    assert cooldown.eligible(chain("A", [-0.1, -0.1, -0.1, 0.0]))  # 4 段要輸 3 段（0 不算輸）
    assert not cooldown.eligible(chain("A", [-0.2, -0.2, 0.0, 0.0]))


def test_two_segments_are_never_enough():
    assert not cooldown.eligible(chain("A", [-0.9, -0.9]))
    assert not cooldown.eligible([])


# ── 週報這一段 ───────────────────────────────────────────────────────────────
def test_no_comparable_twenty_day_scores_yet():
    """有 20 日成績、只是股票池平均還不能比（pending 等）時，也不能寫成「還沒有 20 日成績」（【推薦成績】那段會寫有）。"""
    pending = [entry("2330", "2026-10-01", "2026-10-29", 0.1, p_status="pending")]
    assert cooldown.format_lines({"tw_ai": pending}) == [
        cooldown.HEAD, "・台股 AI 相關股：還沒有可和股票池比的 20 日成績", "・美股：還沒有可和股票池比的 20 日成績"]


def test_summary_counts_and_hits_show_own_pool_and_benchmark_returns():
    """達條件那一行寫平均落後多少，也寫本身、股票池、基準各賺多少：可能每段都賺，只是股票池更熱（PLAN_B2C2 §4.6）。"""
    rows = (chain("2330", [-0.08, -0.04, -0.065], p=0.1, b=0.03) + chain("2454", [-0.2, -0.2, -0.2], p=-0.05, b=-0.02)
            + chain("2317", [0.02]))
    us = chain("AAPL", [-0.3], family="us")
    lines = cooldown.format_lines({"tw_ai": rows, "us": us, "tw_v1": chain("1101", [-0.5] * 3)})
    assert lines == [
        cooldown.HEAD,
        "・台股 AI 相關股：可和股票池比的 20 日成績 3 檔，最多 3 段，達條件 2 檔",
        "　2454：3 段平均比股票池 -20.0%（本身 -25.0%、股票池 -5.0%、0050 -2.0%），輸 3 段（10/1 -20.0%、10/3 -20.0%、"
        "10/5 -20.0%）",
        "　2330：3 段平均比股票池 -6.2%（本身 +3.8%、股票池 +10.0%、0050 +3.0%），輸 3 段（10/1 -8.0%、10/3 -4.0%、"
        "10/5 -6.5%）",
        "・美股：可和股票池比的 20 日成績 1 檔，最多 1 段，達條件 0 檔",
    ]  # 舊五條件（對照組）不冷凍，不列


def test_long_lists_are_capped_and_show_the_latest_segments():
    gaps = [-0.04, -0.05, -0.06, -0.07, -0.08, -0.09, -0.10]  # 7 段都不一樣：看得出列的是哪幾段、平均用全部
    rows = chain("1000", gaps) + [e for i in range(1, cooldown.MAX_LISTED + 2) for e in chain(f"{1000 + i}", [-0.2] * 4)]
    lines = cooldown.format_lines({"tw_ai": rows})
    hits = [line for line in lines if line.startswith("　") and "段平均比股票池" in line]
    assert len(hits) == cooldown.MAX_LISTED and lines[-2] == "　另外 2 檔也達條件"
    rows = chain("1000", gaps)
    assert cooldown.format_lines({"tw_ai": rows})[2] == (
        "　1000：7 段平均比股票池 -7.0%（本身 -7.0%、股票池 0.0%、0050 0.0%），輸 7 段（前面還有 4 段、10/9 -8.0%、"
        "10/11 -9.0%、10/13 -10.0%）")


def test_tiny_negative_gaps_read_as_behind():
    """四捨五入成 0.0% 的負數也算輸：畫面寫 -0.0%，不然「輸 3 段」對不上。"""
    rows = chain("Z", [-0.2, -0.2, -0.0001])
    assert cooldown.format_lines({"tw_ai": rows})[2].endswith("輸 3 段（10/1 -20.0%、10/3 -20.0%、10/5 -0.0%）")


def test_unreadable_ledger_does_not_look_like_no_scores():
    assert cooldown.format_lines({}, "error:PermissionError") == [cooldown.HEAD, "・成績帳讀不到（PermissionError）：這週先不算"]
    assert cooldown.format_lines({}, "elsewhere") == [cooldown.HEAD, "・成績帳不在這個 bot 目錄（測試或 staging）：這週先不算"]
