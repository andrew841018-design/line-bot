"""股票推薦回測 C2a：冷凍條件試算（只讀，給週報用；真的冷凍在 C2b）。

規格：ops/state/line_bot_stock_backtest_20261028/PLAN_B2C2.md §3（冷凍規則見 PLAN.md §0 第 4 點）。
- 輸入是 stock_picks_review_returns.report_data 的 Entry（和週報【推薦成績】同一條組裝，每週各自讀一次）。只看
  COOLDOWN_FAMILIES（台股 AI 相關股、美股）家人看到的推薦：h＝20、skip 是 None、p_status 是 ok（有股票池平均可以比）、
  r 與 p 是有限數、帳上有 th。
- 段（episodes）：照 (代號, t) 走，t 早於上一段的出場日 th 就略過（窗口不重疊；只比帳上的日期，不查日曆，所以不怕
  價格窗修剪）。t 要不早於上一段的 th，所以段距至少 h＋1 個交易日，比 r4 原型（用 E 比）嚴一天：這是 PLAN 刻意的選擇
  （debate r3），不是 bug。每週從只增不改的成績帳重算，不存。還沒有 20 日成績、或股票池平均還沒算完的推薦不等，直接略過。
- 條件（eligible）：段數 ≥ 3；平均（r − p）四捨五入到千分位 ≤ −50‰（平均落後股票池 5%）；r − p < 0 的段數
  ≥ 2/3 × 段數。千分位的四捨五入和【推薦成績】、家人成績卡同一種（returns._permille）。
- 週報只列結果：每族可和股票池比的 20 日成績有幾檔、最多幾段、達條件幾檔；達條件的列代號、平均（本身、股票池、基準），
  以及最近幾段。每族最多列 MAX_LISTED 檔、每檔最多列最近 MAX_SEGMENTS_SHOWN 段，整份週報才放得進 relay 的三段。

頂層只 import pool 與標準庫；returns 在函式裡才 import：C2b 的 CLI（--release-all 等）會 import 這個模組，returns 壞了
也要能跑（PLAN_B2C2 §4.7、§4.9）。weekly 也是在函式裡才 import 這個模組，這一段出錯只換成一行，週報照送。
這個模組 import 時不做任何 I/O。
"""

from __future__ import annotations

import importlib
import math
from typing import Iterable, Mapping, NamedTuple, Optional, Sequence

from jobs import stock_picks_review_pool as pool

HOLD_H = 20  # 冷凍只看 20 日成績
MIN_SEGMENTS = 3
LAG_PERMILLE = -50  # 平均（r − p）的上限（千分位）：落後股票池 5% 以上
COOLDOWN_FAMILIES = (pool.FAMILY_AI, pool.FAMILY_US)  # 舊五條件（對照組）不冷凍
MAX_LISTED = 5  # 每族最多列幾檔達條件的（其餘只寫檔數）
MAX_SEGMENTS_SHOWN = 3  # 每檔最多列最近幾段
HEAD = "【冷凍條件試算】只算，還沒有自動拿掉任何股票"
INDENT = "　"  # 達條件的那幾行用全形空白縮排（Discord 會吃掉半形的行首空白）


class Summary(NamedTuple):
    """一檔的段合起來：段數、平均 r − p、輸股票池的段數，和本身、股票池、基準的平均（C2b 的冷凍列也存這幾個數）。"""

    n: int
    gap: float
    behind: int
    r: float
    p: float
    b: Optional[float]  # 沒有一段有基準報酬時是 None


def _usable(e) -> bool:
    """能放進段的帳列：h＝20、沒跳過、股票池平均算好了、r 與 p 是有限數（bool 不算，和 returns._finite 一樣）、帳上有
    出場日。"""
    from jobs import stock_picks_review_returns as returns

    return (e.h == HOLD_H and e.skip is None and e.p_status == "ok" and e.th is not None
            and returns._finite(e.r) is not None and returns._finite(e.p) is not None)


def episodes(entries: Iterable, since: Optional[Mapping[str, str]] = None) -> dict[str, list]:
    """{代號: [段…]}：一族的 Entry 照 (代號, t) 走，每一段是一筆能用的推薦（_usable）。

    - since：{代號: 重新起算日}，t 早於那檔自己的那天就略過（放回之後重新起算，C2b 才用；其他代號不受影響）。
    - t 早於上一段的 th：窗口和上一段重疊，略過；t 等於 th 算新的一段。"""
    out: dict[str, list] = {}
    for e in sorted(entries, key=lambda x: (x.code, x.t)):
        if not _usable(e) or (since is not None and e.code in since and e.t < since[e.code]):
            continue
        mine = out.setdefault(e.code, [])
        if mine and e.t < mine[-1].th:
            continue
        mine.append(e)
    return out


def vs_pool(segments: Sequence) -> list[float]:
    """各段的 r − p（推薦比股票池等權平均多賺多少）。"""
    return [e.r - e.p for e in segments]


def summary(segments: Sequence) -> Summary:
    from jobs import stock_picks_review_returns as returns

    n = len(segments)
    diffs = vs_pool(segments)
    bench = [b for b in (returns._finite(e.b) for e in segments) if b is not None]
    return Summary(n, math.fsum(diffs) / n, sum(1 for d in diffs if d < 0),
                   math.fsum(e.r for e in segments) / n, math.fsum(e.p for e in segments) / n,
                   math.fsum(bench) / len(bench) if bench else None)


def eligible(segments: Sequence) -> bool:
    """段數 ≥ MIN_SEGMENTS、平均 r − p 的千分位 ≤ LAG_PERMILLE、輸股票池的段數 ≥ 2/3 × 段數。"""
    from jobs import stock_picks_review_returns as returns

    if len(segments) < MIN_SEGMENTS:
        return False
    s = summary(segments)
    return returns._permille(s.gap) <= LAG_PERMILLE and 3 * s.behind >= 2 * s.n


def _gap_text(gap: float) -> str:
    """r − p 的百分比：四捨五入成 0.0% 的負數寫 -0.0%（它算進「輸」的段數，畫面上要看得出來）。"""
    from jobs import stock_picks_review_returns as returns

    text = returns.pct(gap)
    return "-" + text if gap < 0 and not text.startswith("-") else text


def _hit_line(code: str, segments: Sequence, bench: str) -> str:
    from jobs import stock_picks_review_returns as returns

    s = summary(segments)
    shown = segments[-MAX_SEGMENTS_SHOWN:]
    earlier = f"前面還有 {len(segments) - len(shown)} 段、" if len(segments) > len(shown) else ""
    parts = "、".join(f"{returns._md(e.t)} {_gap_text(e.r - e.p)}" for e in shown)
    base = f"、{bench} {returns.pct(s.b)}" if s.b is not None else ""
    return (f"{INDENT}{code}：{s.n} 段平均比股票池 {_gap_text(s.gap)}（本身 {returns.pct(s.r)}、股票池 {returns.pct(s.p)}"
            f"{base}），輸 {s.behind} 段（{earlier}{parts}）")


def family_lines(family: pool.Family, entries: Iterable) -> list[str]:
    """一族的試算：一行摘要，加上達條件的代號（平均落後最多的排前面）。"""
    eps = episodes(entries)
    if not eps:
        return [f"・{family.name}：還沒有可和股票池比的 20 日成績"]
    hits = {code: segments for code, segments in eps.items() if eligible(segments)}
    longest = max(len(segments) for segments in eps.values())
    lines = [f"・{family.name}：可和股票池比的 20 日成績 {len(eps)} 檔，最多 {longest} 段，達條件 {len(hits)} 檔"]
    order = sorted(hits, key=lambda code: (summary(hits[code]).gap, code))
    lines += [_hit_line(code, hits[code], family.bench) for code in order[:MAX_LISTED]]
    if len(order) > MAX_LISTED:
        lines.append(f"{INDENT}另外 {len(order) - MAX_LISTED} 檔也達條件")
    return lines


def format_lines(entries: dict, ledger_state: str = "ok") -> list[str]:
    """{族: [Entry…]} → 週報這一段。成績帳沒讀到時不寫「還沒有…20 日成績」（那會像是真的沒有），原因的說法和
    【推薦成績】那一段同一套（returns.ledger_reason）。"""
    if ledger_state != "ok":
        from jobs import stock_picks_review_returns as returns

        return [HEAD, f"・{returns.ledger_reason(ledger_state)}：這週先不算"]
    lines = [HEAD]
    for key in COOLDOWN_FAMILIES:
        lines += family_lines(pool.FAMILY_BY_KEY[key], entries.get(key, ()))
    return lines


def report_lines(ctx, conn, calendars: dict[str, Sequence[str]]) -> list[str]:
    """週報的【冷凍條件試算】一段（weekly.cooldown_lines 在自己的 try 裡呼叫）。資料和【推薦成績】同一條組裝
    （returns.report_data，各自讀一次），成績帳版本不對時一樣先不算。"""
    from jobs import stock_picks_review_returns as returns

    perf = importlib.import_module("stock_picks_perf")
    if getattr(perf, "LEDGER_VERSION", None) != returns.SUPPORTED_LEDGER_VERSION:
        return [HEAD, f"・推薦成績帳的版本不是 v{returns.SUPPORTED_LEDGER_VERSION}，這週先不算"]
    report, ledger_state = returns.report_data(ctx, conn, calendars)
    return format_lines(report.entries, ledger_state)
