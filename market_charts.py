"""市場盤勢（/盤勢，2026-10-17）的兩張圖：各履約價的期權未平倉量、假設部位下的 Gamma 曲線。

Andrew 2026-10-17 要看「期權市場的下注（最大未平倉價位等等，gamma squeeze…）」，同一天也說字卡的字
太小。所以圖照 PLAN §7、§18.13、§19.5 畫：
- 一張圖一件事。`render_oi_chart` 只畫各履約價的買權／賣權未平倉（買權往上、賣權往下），標出現價、
  買權牆、賣權牆、最大痛點；`render_gex_chart` 只畫 GEX(S) 對模擬標的價格的曲線、0 線、現價、
  變號點（台指沒有這張）。
- 1200×900 PNG、小於 1 MB；所有字至少 28 px（dpi 100：標題與標籤 28 pt 以上＝39 px，刻度 22 pt＝31 px）。
- 橫軸固定是現價 ±15%，和卡上的牆範圍一致（買權牆 [S, 1.15S]、賣權牆 [0.85S, S]）；落在範圍外的牆或
  最大痛點不畫線，只在圖例寫「範圍外」。
- 顏色只用藍 #1E5AA8、橘 #B7791F 和灰色（/股票 有紅漲綠跌的顧慮，不用紅綠）。
- 只用 matplotlib 的物件 API（Figure＋FigureCanvasAgg），不用 pyplot、不改全域 rcParams：publish 子程序
  沒有顯示器，也不能讓一張圖的設定影響另一張。
- 字型依序找 PingFang TC → Heiti TC → Arial Unicode MS，都沒有就用 matplotlib 內建的 DejaVu Sans
  （中文會變成方塊，但不會失敗）。matplotlib 3.10 只讀 .ttc 的第一個字面，看不到 PingFang TC，
  會選 Heiti TC；3.11 看得到。部署腳本先呼叫 `ensure_font_cache()` 把字型快取暖好。全部用一般字重：
  3.11 遇到字重不完全相符（例如要粗體、補字的 DejaVu Sans 只有 400／700）就在 log 記一筆 WARNING。
- 寫檔照 §19.5：圖目錄要先通過 `market_paths.private_dir_ok`（這裡只檢查、不建立，建目錄是 publish 的事），
  再用 `market_paths.write_private_atomic`（同目錄暫存檔 O_EXCL → fsync → rename，0600；bot 用同一個
  帳號讀檔服務）。PNG 先在記憶體畫好、量過大小才寫，所以失敗時不會留下半張圖。

呼叫端傳進來的字（標題、範圍說明、註記、變號方向）都是程式組的固定句；這裡一律關掉 mathtext，
避免「$5 到 $10」這種字被當成數學式。錯誤一律丟 `ChartError(kind)`，只帶種類（outcome 只記列舉值）。
這個模組 import 時不做任何 I/O：matplotlib 在第一次畫圖或暖快取時才 import。
"""

from __future__ import annotations

import math
import threading
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional, Sequence

import market_paths

if TYPE_CHECKING:  # 只給型別檢查用；執行時 matplotlib 延後到第一次畫圖才 import
    from matplotlib.axes import Axes
    from matplotlib.backend_bases import RendererBase
    from matplotlib.figure import Figure
    from matplotlib.text import Text

# ── 版面 ──────────────────────────────────────────────────────────────────────
WIDTH_PX = 1200
HEIGHT_PX = 900
DPI = 100
MAX_BYTES = 1_000_000  # LINE 預覽圖上限 1 MB；用十進位比較保守
RANGE = 0.15  # 橫軸＝現價 ±15%（PLAN §18.13，和卡上的牆範圍一致）
MIN_TEXT_PX = 28
TITLE_PT = 30.0
LABEL_PT = 28.0  # 28 pt × 100 / 72 ≈ 39 px
TICK_PT = 22.0  # 22 pt × 100 / 72 ≈ 31 px，仍 ≥ 28 px
_PAD_PX = 18
_GAP_PX = 12
_LINE_WIDTH = 3.0

# ── 顏色：只用藍、橘、灰 ──────────────────────────────────────────────────────
BLUE = "#1E5AA8"
AMBER = "#B7791F"
INK = "#333333"
GREY = "#555555"
GREY_MID = "#777777"
GREY_LIGHT = "#DDDDDD"
WHITE = "#FFFFFF"
_FILL_ALPHA = 0.35

# ── 字型 ──────────────────────────────────────────────────────────────────────
FONT_CANDIDATES = ("PingFang TC", "Heiti TC", "Arial Unicode MS")
DEFAULT_FONT = "DejaVu Sans"  # matplotlib 內建、一定有；中文會缺字，但畫圖不會失敗

# ── 固定字 ────────────────────────────────────────────────────────────────────
OI_XLABEL = "履約價"
OI_YLABEL = "未平倉（口）"
OI_CALL_CAPTION = "上：買權未平倉"
OI_PUT_CAPTION = "下：賣權未平倉"
OI_EMPTY = "範圍內沒有未平倉資料"
GEX_XLABEL = "模擬的標的價格"
GEX_YLABEL = "Gamma（美元／1%）"
GEX_EMPTY = "沒有可畫的資料"
OUT_OF_RANGE = "（範圍外）"
# 美股圖的長條與牆是 45 天內合併，最大痛點只用月選（market_publish）；圖例寫明，免得被當成同一個範圍
MAX_PAIN_NAME = "最大痛點（月選）"
_BREAK_CHARS = "｜；，、 "  # 太長要換行時，優先在這些字後面斷


class ChartError(ValueError):
    """畫不出來或不該寫檔。只帶種類、不帶內容（publish 只把 kind 記進 outcome）。

    kind：「data」輸入不合格（長度不一、非有限數、負的未平倉、現價 ≤0…）；「dir」圖目錄不是自己的
    0700 目錄或不存在；「too_big」PNG ≥1 MB。
    """

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


# ── 字型選擇（整個程序選一次） ──────────────────────────────────────────────────
_font_lock = threading.Lock()
_font_choice: Optional[str] = None


def _available_font_names() -> set[str]:
    # 第一次 import font_manager 會讀（或第一次建立）~/.matplotlib 的字型快取，所以放在函式裡
    from matplotlib import font_manager

    return {entry.name for entry in font_manager.fontManager.ttflist}


def _pick_font(available: Iterable[str], candidates: Sequence[str] = FONT_CANDIDATES) -> Optional[str]:
    names = set(available)
    for name in candidates:
        if name in names:
            return name
    return None


def ensure_font_cache() -> str:
    """載入 matplotlib 字型快取並選定中文字型，回傳選到的字型名稱（三個都沒有就回 DejaVu Sans）。

    部署腳本先呼叫一次，第一次建快取的等待就不會落在 publish 的時間預算裡。不強制重建快取。
    """
    global _font_choice
    with _font_lock:
        if _font_choice is None:
            _font_choice = _pick_font(_available_font_names()) or DEFAULT_FONT
        return _font_choice


def _families() -> list[str]:
    # 中文字型缺的符號（例如數學減號）由 DejaVu Sans 補；沒有中文字型時只剩 DejaVu Sans
    chosen = ensure_font_cache()
    return [chosen] if chosen == DEFAULT_FONT else [chosen, DEFAULT_FONT]


# ── 輸入檢查（全部在畫圖前做完） ──────────────────────────────────────────────────
def _as_list(values: object) -> list:
    if isinstance(values, (str, bytes)):
        raise ChartError("data")
    try:
        return list(values)  # type: ignore[call-overload]
    except TypeError:
        raise ChartError("data") from None


def _number(value: object) -> float:
    # bool 和字串都是呼叫端的錯（float("710") 會過），不替它轉
    if isinstance(value, (bool, str, bytes)):
        raise ChartError("data")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ChartError("data") from None
    if not math.isfinite(number):
        raise ChartError("data")
    return number


def _positive(value: object) -> float:
    number = _number(value)
    if number <= 0:
        raise ChartError("data")
    return number


def _optional_price(value: object) -> Optional[float]:
    return None if value is None else _positive(value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ChartError("data")
    return value


def _window(spot: float) -> tuple[float, float]:
    return spot * (1 - RANGE), spot * (1 + RANGE)


def _in_window(value: float, low: float, high: float) -> bool:
    # 只容許浮點誤差：剛好在 1.15S 上的履約價要算在範圍內
    tolerance = 1e-9 * max(1.0, abs(high))
    return low - tolerance <= value <= high + tolerance


def _oi_rows(strikes: object, call_oi: object, put_oi: object, low: float, high: float) -> list[tuple[float, float, float]]:
    strike_list, call_list, put_list = _as_list(strikes), _as_list(call_oi), _as_list(put_oi)
    if not len(strike_list) == len(call_list) == len(put_list):
        raise ChartError("data")
    totals: dict[float, list[float]] = {}
    for raw_strike, raw_call, raw_put in zip(strike_list, call_list, put_list):
        strike, calls, puts = _positive(raw_strike), _number(raw_call), _number(raw_put)
        if calls < 0 or puts < 0:
            raise ChartError("data")
        if _in_window(strike, low, high):
            # 同一個履約價出現多次（例如不同到期）就加總，不然長條會疊在一起
            entry = totals.setdefault(strike, [0.0, 0.0])
            entry[0] += calls
            entry[1] += puts
    return [(strike, pair[0], pair[1]) for strike, pair in sorted(totals.items())]


def _curve_points(prices: object, gex: object) -> list[tuple[float, float]]:
    price_list, gex_list = _as_list(prices), _as_list(gex)
    if len(price_list) != len(gex_list):
        raise ChartError("data")
    return sorted((_positive(price), _number(value)) for price, value in zip(price_list, gex_list))


def _crossing_items(crossings: object) -> list[tuple[float, str]]:
    items: list[tuple[float, str]] = []
    for item in _as_list(crossings):
        # 快照裡存的是 {price, direction}；介面是 (price, direction)，兩種都收
        pair = [item.get("price"), item.get("direction")] if isinstance(item, dict) else _as_list(item)
        if len(pair) != 2:
            raise ChartError("data")
        items.append((_positive(pair[0]), _text(pair[1])))
    return items


# ── 數字格式 ───────────────────────────────────────────────────────────────────
def _trim(number: float) -> str:
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _fmt_price(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return _trim(value)


def _fmt_count(value: float) -> str:
    # 賣權畫在 0 線下面，刻度一律寫絕對值
    magnitude = abs(value)
    if magnitude >= 10_000:
        return _trim(magnitude / 10_000) + "萬"
    return f"{magnitude:,.0f}"


def _fmt_dollars(value: float) -> str:
    magnitude = abs(value)
    if magnitude >= 1e8:
        body = _trim(magnitude / 1e8) + "億"
    elif magnitude >= 1e4:
        body = _trim(magnitude / 1e4) + "萬"
    elif magnitude >= 1000:
        body = f"{magnitude:,.0f}"
    else:
        body = _trim(magnitude)
    if body == "0":
        return "0"
    return ("+" if value > 0 else "−") + body


def _direction_text(direction: str) -> str:
    # 方向的格式由 market_calc 決定；只有中文說明（例「由負轉正」）才照寫在圖上，代碼不寫
    # （正負已經由藍橘兩色看得出來）
    text = direction.strip()
    if text and len(text) <= 8 and any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return text
    return ""


# ── 共用畫圖步驟 ───────────────────────────────────────────────────────────────
def _new_figure() -> tuple["Figure", "Axes", list[str]]:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    families = _families()
    figure = Figure(figsize=(WIDTH_PX / DPI, HEIGHT_PX / DPI), dpi=DPI, facecolor=WHITE)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(1, 1, 1)
    axes.set_facecolor(WHITE)
    for name, spine in axes.spines.items():
        spine.set_color(GREY)
        spine.set_linewidth(1.5)
        spine.set_visible(name in ("left", "bottom"))
    axes.tick_params(
        axis="both",
        labelsize=TICK_PT,
        labelcolor=INK,
        color=GREY,
        width=1.5,
        length=6,
        labelfontfamily=families,
    )
    axes.grid(axis="y", color=GREY_LIGHT, linewidth=1)
    axes.set_axisbelow(True)
    return figure, axes, families


def _axis_labels(axes: "Axes", families: list[str], xlabel: str, ylabel: str) -> None:
    common = {"fontsize": LABEL_PT, "fontfamily": families, "color": INK, "parse_math": False}
    axes.set_xlabel(xlabel, labelpad=8, **common)
    axes.set_ylabel(ylabel, labelpad=10, **common)


def _price_axis(axes: "Axes", low: float, high: float) -> None:
    from matplotlib import ticker

    axes.set_xlim(low, high)
    axes.xaxis.set_major_locator(ticker.MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10]))
    axes.xaxis.set_major_formatter(ticker.FuncFormatter(lambda value, _pos: _fmt_price(value)))


def _note(axes: "Axes", families: list[str], x: float, y: float, text: str, color: str, ha: str, va: str) -> None:
    axes.text(
        x,
        y,
        text,
        transform=axes.transAxes,
        ha=ha,
        va=va,
        fontsize=LABEL_PT,
        fontfamily=families,
        color=color,
        parse_math=False,
        zorder=6,
        bbox={"boxstyle": "round,pad=0.2", "facecolor": WHITE, "edgecolor": "none", "alpha": 0.85},
    )


def _width(artist: "Text", renderer: "RendererBase") -> float:
    return artist.get_window_extent(renderer).width


def _longest_fitting(artist: "Text", renderer: "RendererBase", text: str, max_px: float, suffix: str = "") -> int:
    # 字寬隨字數單調增加，二分找最多能放幾個字
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        artist.set_text(text[:middle] + suffix)
        if _width(artist, renderer) <= max_px:
            low = middle
        else:
            high = middle - 1
    return low


def _fit_text(artist: "Text", renderer: "RendererBase", max_px: float, *, min_pt: float, max_lines: int = 2) -> None:
    """放不下時先把字縮到 min_pt，再換行（最多 max_lines 行），最後一行還放不下才截斷加「…」。"""
    text = artist.get_text()
    if not text or _width(artist, renderer) <= max_px:
        return
    size = artist.get_fontsize()
    while size - 1 >= min_pt and _width(artist, renderer) > max_px:
        size -= 1
        artist.set_fontsize(size)
    if _width(artist, renderer) <= max_px:
        return
    lines: list[str] = []
    rest = text
    while len(lines) < max_lines - 1:
        artist.set_text(rest)
        if _width(artist, renderer) <= max_px:
            break
        count = max(1, _longest_fitting(artist, renderer, rest, max_px))
        breaks = [index + 1 for index in range(count) if rest[index] in _BREAK_CHARS]
        cut = breaks[-1] if breaks and breaks[-1] > count // 2 else count
        lines.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    artist.set_text(rest)
    if _width(artist, renderer) > max_px:
        rest = rest[: _longest_fitting(artist, renderer, rest, max_px, "…")] + "…"
    lines.append(rest)
    artist.set_text("\n".join(lines))


def _add_legend(figure: "Figure", renderer: "RendererBase", handles: list, labels: list[str], top_px: float, families: list[str]):
    max_px = WIDTH_PX - 2 * _PAD_PX
    columns = [len(labels)] + [n for n in (2, 1) if n < len(labels)]
    legend = None
    for ncol in columns:
        if legend is not None:
            legend.remove()
        legend = figure.legend(
            handles,
            labels,
            loc="upper left",
            bbox_to_anchor=(_PAD_PX / WIDTH_PX, top_px / HEIGHT_PX),
            ncol=ncol,
            frameon=False,
            prop={"family": families, "size": LABEL_PT},
            handlelength=1.6,
            handletextpad=0.5,
            columnspacing=1.2,
            borderaxespad=0.0,
            borderpad=0.0,
            labelcolor=INK,
        )
        if legend.get_window_extent(renderer).width <= max_px:
            break
    return legend


def _frame(figure: "Figure", families: list[str], *, title: str, footer: str, handles: list, labels: list[str]) -> None:
    """標題（左上）→ 圖例（標題下面）→ 註記（最下面），量好實際高度再把座標軸塞進中間。"""
    renderer = figure.canvas.get_renderer()
    max_px = WIDTH_PX - 2 * _PAD_PX
    head = figure.text(
        _PAD_PX / WIDTH_PX,
        1 - _PAD_PX / HEIGHT_PX,
        title,
        ha="left",
        va="top",
        fontsize=TITLE_PT,
        fontfamily=families,
        color=INK,
        parse_math=False,
    )
    _fit_text(head, renderer, max_px, min_pt=LABEL_PT)
    top = (head.get_window_extent(renderer).y0 - _GAP_PX) if title else HEIGHT_PX - _PAD_PX
    if handles:
        legend = _add_legend(figure, renderer, handles, labels, top, families)
        top = legend.get_window_extent(renderer).y0 - _GAP_PX
    bottom = float(_PAD_PX)
    if footer:
        foot = figure.text(
            _PAD_PX / WIDTH_PX,
            _PAD_PX / HEIGHT_PX,
            footer,
            ha="left",
            va="bottom",
            fontsize=LABEL_PT,
            fontfamily=families,
            color=GREY,
            parse_math=False,
        )
        _fit_text(foot, renderer, max_px, min_pt=LABEL_PT)
        bottom = foot.get_window_extent(renderer).y1 + _GAP_PX
    figure.tight_layout(rect=(0.0, bottom / HEIGHT_PX, 1.0, top / HEIGHT_PX), pad=0.3)


def _encode_png(figure: "Figure") -> bytes:
    buffer = BytesIO()
    figure.savefig(buffer, format="png", dpi=DPI, facecolor=WHITE)
    return buffer.getvalue()


def _png_bytes(figure: "Figure") -> bytes:
    # 超過就不寫、丟 too_big，不重畫：降 dpi 就不是 1200×900、字也會小於 28 px；同尺寸的最高壓縮
    # （compress_level 9／optimize）實測反而比預設大約 13%。真實的圖約 60–150 KB，離 1 MB 很遠。
    data = _encode_png(figure)
    if len(data) >= MAX_BYTES:
        raise ChartError("too_big")
    return data


def _save(figure: "Figure", out_path: Path) -> None:
    target = Path(out_path)
    if not market_paths.private_dir_ok(target.parent, create=False):
        raise ChartError("dir")
    market_paths.write_private_atomic(target, _png_bytes(figure))


# ── 圖 1：各履約價的未平倉 ──────────────────────────────────────────────────────
def _build_oi_figure(
    *,
    title: str,
    strikes: Sequence[float],
    call_oi: Sequence[int],
    put_oi: Sequence[int],
    spot: float,
    call_wall: Optional[float],
    put_wall: Optional[float],
    max_pain: Optional[float],
    range_note: str,
) -> "Figure":
    from matplotlib import ticker
    from matplotlib.lines import Line2D

    title, range_note = _text(title), _text(range_note)
    spot = _positive(spot)
    markers = (
        ("現價", spot, INK, "-"),
        ("買權牆", _optional_price(call_wall), BLUE, "--"),
        ("賣權牆", _optional_price(put_wall), AMBER, "--"),
        (MAX_PAIN_NAME, _optional_price(max_pain), GREY_MID, ":"),
    )
    low, high = _window(spot)
    rows = _oi_rows(strikes, call_oi, put_oi, low, high)

    figure, axes, families = _new_figure()
    xs = [strike for strike, _calls, _puts in rows]
    gaps = [b - a for a, b in zip(xs, xs[1:])]
    width = 0.8 * min(min(gaps, default=spot * 0.01), spot * 0.01)
    if rows:
        axes.bar(xs, [calls for _s, calls, _p in rows], width=width, color=BLUE, edgecolor="none", linewidth=0, zorder=2)
        axes.bar(xs, [-puts for _s, _c, puts in rows], width=width, color=AMBER, edgecolor="none", linewidth=0, zorder=2)
    peak = max((max(calls, puts) for _s, calls, puts in rows), default=0.0)
    limit = peak * 1.12 if peak > 0 else 1.0
    axes.set_ylim(-limit, limit)
    _price_axis(axes, low, high)
    if peak > 0:
        axes.yaxis.set_major_locator(ticker.MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10], integer=True, symmetric=True))
    else:
        axes.yaxis.set_major_locator(ticker.FixedLocator([0.0]))  # 沒有資料：刻度只留 0，不寫假的 1
    axes.yaxis.set_major_formatter(ticker.FuncFormatter(lambda value, _pos: _fmt_count(value)))
    axes.axhline(0, color=INK, linewidth=1.5, zorder=3)

    handles: list = []
    labels: list[str] = []
    for name, value, color, style in markers:
        if value is None:
            continue
        inside = _in_window(value, low, high)
        if inside:
            axes.axvline(value, color=color, linestyle=style, linewidth=_LINE_WIDTH, zorder=4)
        # 範圍外的只寫在圖例（樣本線透明），圖上不畫線
        handles.append(Line2D([], [], color=color, linestyle=style, linewidth=_LINE_WIDTH, alpha=1.0 if inside else 0.0))
        labels.append(f"{name} {_fmt_price(value)}" + ("" if inside else OUT_OF_RANGE))

    _note(axes, families, 0.01, 0.98, OI_CALL_CAPTION, BLUE, "left", "top")
    _note(axes, families, 0.99, 0.02, OI_PUT_CAPTION, AMBER, "right", "bottom")
    if peak <= 0:
        _note(axes, families, 0.5, 0.5, OI_EMPTY, GREY, "center", "center")
    _axis_labels(axes, families, OI_XLABEL, OI_YLABEL)
    _frame(figure, families, title=title, footer=range_note, handles=handles, labels=labels)
    return figure


def render_oi_chart(
    out_path: Path,
    *,
    title: str,
    strikes: list[float],
    call_oi: list[int],
    put_oi: list[int],
    spot: float,
    call_wall: float | None,
    put_wall: float | None,
    max_pain: float | None,
    range_note: str,
) -> None:
    """各履約價的買權（藍、往上）／賣權（橘、往下）未平倉，橫軸＝現價 ±15%，寫成 out_path 的 PNG。

    範圍外的履約價不畫；同一履約價重複就加總。現價、買權牆、賣權牆、最大痛點畫成直線並寫進圖例
    （None 就不畫；在範圍外的只在圖例寫「範圍外」）。range_note 寫在最下面一行。
    輸入不合格、圖目錄不合格或 PNG 太大 → ChartError，out_path 不會被建立或改動。
    """
    figure = _build_oi_figure(
        title=title,
        strikes=strikes,
        call_oi=call_oi,
        put_oi=put_oi,
        spot=spot,
        call_wall=call_wall,
        put_wall=put_wall,
        max_pain=max_pain,
        range_note=range_note,
    )
    _save(figure, out_path)


# ── 圖 2：假設部位下的 Gamma 曲線 ───────────────────────────────────────────────
def _interpolate(x: float, points: list[tuple[float, float]]) -> Optional[float]:
    if not points or not points[0][0] <= x <= points[-1][0]:
        return None
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[0][1]


def _place_crossing_labels(axes: "Axes", families: list[str], items: list[tuple[float, str]], spot: float) -> None:
    """變號點的價格寫在直線頂端、離開現價的那一側（現價左邊的往左寫、右邊的往右寫，不蓋住現價線）；
    超出座標軸就改寫另一側；和已放的標籤重疊就往下一列（座標軸版面定了之後才量得準）。"""
    from matplotlib import transforms
    from matplotlib.transforms import Bbox

    renderer = axes.figure.canvas.get_renderer()
    blend = transforms.blended_transform_factory(axes.transData, axes.transAxes)
    frame = axes.get_window_extent(renderer)
    row_step = (LABEL_PT * DPI / 72 * 1.45) / max(frame.height, 1.0)
    placed: list = []
    for price, label in sorted(items):
        first = "right" if price < spot else "left"
        for row in range(3):
            for side in (first, "left" if first == "right" else "right"):
                shift = transforms.offset_copy(blend, x=-8 if side == "right" else 8, units="dots")
                artist = axes.text(
                    price,
                    0.98 - row * row_step,
                    label,
                    transform=shift,
                    ha=side,
                    va="top",
                    fontsize=LABEL_PT,
                    fontfamily=families,
                    color=INK,
                    parse_math=False,
                    zorder=6,
                    bbox={"boxstyle": "round,pad=0.2", "facecolor": WHITE, "edgecolor": "none", "alpha": 0.85},
                )
                box = artist.get_window_extent(renderer)
                if frame.x0 <= box.x0 and box.x1 <= frame.x1:
                    break
                if side != first:
                    break  # 兩側都超出（標籤比半張圖還寬）：留在第二次的位置
                artist.remove()
            padded = Bbox.from_extents(box.x0 - 8, box.y0 - 6, box.x1 + 8, box.y1 + 6)
            if row == 2 or not any(padded.overlaps(other) for other in placed):
                placed.append(padded)
                break
            artist.remove()


def _build_gex_figure(
    *,
    title: str,
    prices: Sequence[float],
    gex: Sequence[float],
    spot: float,
    crossings: Sequence[tuple[float, str]],
    note: str,
) -> "Figure":
    from matplotlib import ticker
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    title, note = _text(title), _text(note)
    spot = _positive(spot)
    points = _curve_points(prices, gex)
    items = _crossing_items(crossings)
    low, high = _window(spot)

    figure, axes, families = _new_figure()
    xs = [x for x, _y in points]
    ys = [y for _x, y in points]
    if points:
        axes.fill_between(xs, ys, 0, where=[y > 0 for y in ys], interpolate=True, color=BLUE, alpha=_FILL_ALPHA, linewidth=0)
        axes.fill_between(xs, ys, 0, where=[y < 0 for y in ys], interpolate=True, color=AMBER, alpha=_FILL_ALPHA, linewidth=0)
        axes.plot(xs, ys, color=INK, linewidth=3.5, zorder=3)
    upper = max(max(ys, default=0.0), 0.0)
    lower = min(min(ys, default=0.0), 0.0)
    span = upper - lower
    if span > 0:
        # 上面多留一點，給變號點的價格標籤
        axes.set_ylim(lower - 0.08 * span, upper + 0.35 * span)
    else:
        axes.set_ylim(-1.0, 1.0)
    _price_axis(axes, low, high)
    if span > 0:
        axes.yaxis.set_major_locator(ticker.MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10]))
    else:
        axes.yaxis.set_major_locator(ticker.FixedLocator([0.0]))
    axes.yaxis.set_major_formatter(ticker.FuncFormatter(lambda value, _pos: _fmt_dollars(value)))
    axes.axhline(0, color=GREY_MID, linewidth=1.5, zorder=2)

    axes.axvline(spot, color=INK, linestyle="-", linewidth=_LINE_WIDTH, zorder=4)
    at_spot = _interpolate(spot, points)
    if at_spot is not None:
        axes.plot([spot], [at_spot], marker="o", markersize=14, color=INK, markeredgecolor=WHITE, markeredgewidth=2, zorder=5)
    labels_to_place: list[tuple[float, str]] = []
    for price, direction in items:
        if not _in_window(price, low, high):
            continue
        axes.axvline(price, color=GREY_MID, linestyle="--", linewidth=_LINE_WIDTH - 0.5, zorder=4)
        extra = _direction_text(direction)
        labels_to_place.append((price, f"變號 {_fmt_price(price)}" + (f"（{extra}）" if extra else "")))

    handles = [
        Line2D([], [], color=INK, linestyle="-", linewidth=_LINE_WIDTH),
        Patch(facecolor=BLUE, alpha=_FILL_ALPHA, edgecolor="none"),
        Patch(facecolor=AMBER, alpha=_FILL_ALPHA, edgecolor="none"),
    ]
    labels = [f"現價 {_fmt_price(spot)}", "正值", "負值"]
    if labels_to_place:
        handles.insert(1, Line2D([], [], color=GREY_MID, linestyle="--", linewidth=_LINE_WIDTH - 0.5))
        labels.insert(1, "變號點")
    if not points:
        _note(axes, families, 0.5, 0.5, GEX_EMPTY, GREY, "center", "center")
    _axis_labels(axes, families, GEX_XLABEL, GEX_YLABEL)
    _frame(figure, families, title=title, footer=note, handles=handles, labels=labels)
    _place_crossing_labels(axes, families, labels_to_place, spot)
    return figure


def render_gex_chart(
    out_path: Path,
    *,
    title: str,
    prices: list[float],
    gex: list[float],
    spot: float,
    crossings: list[tuple[float, str]],
    note: str,
) -> None:
    """GEX(S) 對模擬標的價格的曲線（正值藍底、負值橘底）、0 線、現價、變號點，寫成 out_path 的 PNG。

    橫軸＝現價 ±15%；crossings 是 (價格, 方向)，範圍外的不畫，方向是中文說明才寫上去。
    note（例「假設造市商持有買權、賣出賣權；看不到實際部位」）寫在最下面一行。
    輸入不合格、圖目錄不合格或 PNG 太大 → ChartError，out_path 不會被建立或改動。
    """
    figure = _build_gex_figure(title=title, prices=prices, gex=gex, spot=spot, crossings=crossings, note=note)
    _save(figure, out_path)
