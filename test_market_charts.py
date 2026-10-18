"""market_charts（/盤勢的兩張圖）的測試。

用合成資料（QQQ 現價 717.32、履約價 610–825；台指 47,111、履約價 40,000–55,000）畫圖，檢查：
PNG 1200×900、小於 1 MB、權限 0600；寫檔是原子的（rename 前目標不存在、暫存檔已是完整 PNG、失敗不留檔、
舊檔不被破壞）；圖目錄不是自己的 0700 目錄就不寫；所有字 ≥28 px 且都在畫布內、標題／圖例／註記／座標軸
不重疊；顏色只有藍、橘、灰；橫軸＝現價 ±15%；空資料與全 0 不會失敗；輸入不合格丟 ChartError("data")；
caller 的字不當 mathtext；字型依序挑選、沒有中文字型也畫得出來；import 不載入 matplotlib，畫完也沒有 pyplot。
不依賴系統字型：選到哪個字型都要通過，只有「中文字型下不能有警告」那項在沒有中文字型時略過。
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
import threading
import uuid
import warnings
from math import exp
from pathlib import Path

import pytest

import market_charts as mc

WORK_DIR = Path(__file__).resolve().parent
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"
SPOT = 717.32


# ── 合成資料 ──────────────────────────────────────────────────────────────────
def _qqq_oi(**override) -> dict:
    strikes = list(range(610, 651, 5)) + list(range(651, 781)) + list(range(785, 826, 5))
    calls, puts = [], []
    for k in strikes:
        calls.append(int(9000 * exp(-(((k - 735) / 28) ** 2)) + (52000 if k == 750 else 0) + (21000 if k == 800 else 0)))
        puts.append(int(11000 * exp(-(((k - 690) / 30) ** 2)) + (47000 if k == 700 else 0) + (26000 if k == 650 else 0)))
    kwargs = {
        "title": "QQQ 未平倉量（45 天內到期）｜10/16",
        "strikes": strikes,
        "call_oi": calls,
        "put_oi": puts,
        "spot": SPOT,
        "call_wall": 750.0,
        "put_wall": 700.0,
        "max_pain": 715.0,
        "range_note": "範圍：現價 ±15%｜資料 10/16",
    }
    kwargs.update(override)
    return kwargs


def _txo_oi(**override) -> dict:
    strikes = list(range(40000, 55001, 100))
    kwargs = {
        "title": "台指選擇權 10 月 未平倉｜10/15",
        "strikes": strikes,
        "call_oi": [int(3000 * exp(-(((k - 50000) / 2500) ** 2)) + (9000 if k == 54000 else 0)) for k in strikes],
        "put_oi": [int(3500 * exp(-(((k - 45000) / 2500) ** 2)) + (8000 if k == 46000 else 0)) for k in strikes],
        "spot": 47111.26,
        "call_wall": 54000.0,
        "put_wall": 46000.0,
        "max_pain": 39000.0,  # 月選全部履約價算的，可能在 ±15% 外
        "range_note": "範圍：現價 ±15%｜資料 10/15（期交所）",
    }
    kwargs.update(override)
    return kwargs


def _grid(spot: float = SPOT) -> list[float]:
    return [spot * (0.85 + 0.005 * i) for i in range(61)]


def _qqq_gex(**override) -> dict:
    prices = _grid()
    kwargs = {
        "title": "QQQ 假設部位 Gamma｜10/16",
        "prices": prices,
        "gex": [-4.2e6 * (x - 702) * (x - 745) for x in prices],  # 702 由負轉正、745 由正轉負
        "spot": SPOT,
        "crossings": [(702.0, "由負轉正"), (745.0, "由正轉負")],
        "note": "假設造市商持有買權、賣出賣權；看不到實際部位",
    }
    kwargs.update(override)
    return kwargs


# ── 小工具 ────────────────────────────────────────────────────────────────────
def _chart_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "genimg"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    return directory


def _name() -> str:
    return uuid.uuid4().hex + ".png"


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == PNG_SIGNATURE and data[12:16] == b"IHDR"
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _assert_valid_png(path: Path) -> bytes:
    data = path.read_bytes()
    assert data.startswith(PNG_SIGNATURE) and data.endswith(PNG_END)
    assert _png_size(data) == (1200, 900)
    assert len(data) < mc.MAX_BYTES
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return data


def _all_text(figure) -> list[str]:
    from matplotlib.text import Text

    figure.canvas.draw()
    return [t.get_text() for t in figure.findobj(Text) if t.get_visible() and t.get_text().strip()]


def _vlines_x(axes) -> list[float]:
    # axvline 的 x 資料是 [x, x]、y 是座標軸比例 [0, 1]
    found = []
    for line in axes.get_lines():
        xs, ys = list(line.get_xdata()), list(line.get_ydata())
        if len(xs) == 2 and xs[0] == xs[1] and list(ys) == [0, 1]:
            found.append(float(xs[0]))
    return found


@pytest.fixture(scope="module")
def oi_figure():
    return mc._build_oi_figure(**_qqq_oi())


@pytest.fixture(scope="module")
def gex_figure():
    return mc._build_gex_figure(**_qqq_gex())


@pytest.fixture(scope="module")
def txo_figure():
    return mc._build_oi_figure(**_txo_oi())


# ── import 與 pyplot ──────────────────────────────────────────────────────────
def test_import_loads_no_matplotlib_and_rendering_never_loads_pyplot(tmp_path):
    """新的程序裡驗：import 不碰 matplotlib；畫完兩張圖也沒有 pyplot；沒有 findfont／缺字警告。"""
    script = textwrap.dedent(
        """
        import sys, tempfile
        from pathlib import Path
        import market_charts
        assert "matplotlib" not in sys.modules, "import market_charts loaded matplotlib"
        out = Path(tempfile.mkdtemp(dir=sys.argv[1]))
        market_charts.render_oi_chart(out / "a.png", title="QQQ 未平倉｜10/16", strikes=[700.0, 710.0, 720.0],
            call_oi=[10, 20, 30], put_oi=[30, 20, 10], spot=710.0, call_wall=720.0, put_wall=700.0,
            max_pain=710.0, range_note="範圍：現價 ±15%")
        market_charts.render_gex_chart(out / "b.png", title="QQQ Gamma｜10/16", prices=[650.0, 710.0, 770.0],
            gex=[-1e9, 1e8, 2e9], spot=710.0, crossings=[(705.0, "由負轉正")], note="假設造市商持有買權、賣出賣權")
        assert "matplotlib.pyplot" not in sys.modules, "rendering loaded pyplot"
        assert (out / "a.png").stat().st_size > 0 and (out / "b.png").stat().st_size > 0
        print("FONT=" + market_charts.ensure_font_cache())
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=WORK_DIR,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    font = result.stdout.strip().rsplit("FONT=", 1)[-1]
    assert font in mc.FONT_CANDIDATES + (mc.DEFAULT_FONT,)
    assert "findfont" not in result.stderr  # 字重／字型找不到時 matplotlib 會記 WARNING
    if font != mc.DEFAULT_FONT:
        assert "missing from font" not in result.stderr


# ── 輸出檔 ────────────────────────────────────────────────────────────────────
def test_oi_chart_writes_1200x900_png_under_1mb(tmp_path):
    chart_dir = _chart_dir(tmp_path)
    out = chart_dir / _name()
    assert mc.render_oi_chart(out, **_qqq_oi()) is None
    _assert_valid_png(out)
    assert [p.name for p in chart_dir.iterdir()] == [out.name]  # 沒有留下暫存檔


def test_gex_chart_writes_1200x900_png_under_1mb(tmp_path):
    chart_dir = _chart_dir(tmp_path)
    out = chart_dir / _name()
    assert mc.render_gex_chart(out, **_qqq_gex()) is None
    _assert_valid_png(out)
    assert [p.name for p in chart_dir.iterdir()] == [out.name]


def test_taifex_scale_oi_chart(tmp_path):
    chart_dir = _chart_dir(tmp_path)
    out = chart_dir / _name()
    mc.render_oi_chart(out, **_txo_oi())
    _assert_valid_png(out)


@pytest.mark.parametrize(
    "case",
    [
        {"strikes": [], "call_oi": [], "put_oi": []},
        {"call_oi": [0] * 148, "put_oi": [0] * 148},  # 全部 0（_qqq_oi 有 148 個履約價）
        {"strikes": [100.0, 2000.0], "call_oi": [5, 6], "put_oi": [7, 8]},  # 全在範圍外
        {"call_wall": None, "put_wall": None, "max_pain": None},
        {"title": "", "range_note": ""},
    ],
)
def test_oi_chart_handles_empty_and_flat_data(tmp_path, case):
    out = _chart_dir(tmp_path) / _name()
    mc.render_oi_chart(out, **_qqq_oi(**case))
    _assert_valid_png(out)


@pytest.mark.parametrize(
    "case",
    [
        {"prices": [], "gex": [], "crossings": []},
        {"gex": [0.0] * 61, "crossings": []},  # 全部 0
        {"crossings": [(500.0, "由負轉正"), (900.0, "由正轉負")]},  # 變號點在範圍外
        {"prices": [717.32], "gex": [3e8], "crossings": []},  # 只有一點
        {"title": "", "note": ""},
    ],
)
def test_gex_chart_handles_empty_and_flat_data(tmp_path, case):
    out = _chart_dir(tmp_path) / _name()
    mc.render_gex_chart(out, **_qqq_gex(**case))
    _assert_valid_png(out)


def test_crossings_accept_snapshot_dict_shape():
    figure = mc._build_gex_figure(**_qqq_gex(crossings=[{"price": 702.0, "direction": "由負轉正"}]))
    assert "變號 702（由負轉正）" in _all_text(figure)


# ── 原子寫入與目錄 ────────────────────────────────────────────────────────────
def test_out_path_appears_only_through_rename_after_fsync(tmp_path, monkeypatch):
    chart_dir = _chart_dir(tmp_path)
    out = chart_dir / _name()
    real_replace, real_fsync = os.replace, os.fsync
    events: list[str] = []
    temps: list[Path] = []

    def checking_fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    def checking_replace(src, dst, *args, **kwargs):
        src, dst = Path(src), Path(dst)
        if dst == out:
            events.append("replace")
            assert not out.exists(), "目標在 rename 之前就出現了"
            data = src.read_bytes()
            assert data.startswith(PNG_SIGNATURE) and data.endswith(PNG_END), "rename 前暫存檔還不是完整 PNG"
            assert src.parent == chart_dir
            assert stat.S_IMODE(src.stat().st_mode) == 0o600
            temps.append(src)
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", checking_fsync)
    monkeypatch.setattr(os, "replace", checking_replace)
    mc.render_oi_chart(out, **_qqq_oi())
    assert events[:2] == ["fsync", "replace"]  # 先 fsync 暫存檔、再 rename
    assert temps and not temps[0].exists()
    _assert_valid_png(out)
    assert [p.name for p in chart_dir.iterdir()] == [out.name]


def test_failed_rename_keeps_old_file_and_leaves_no_temp(tmp_path, monkeypatch):
    chart_dir = _chart_dir(tmp_path)
    out = chart_dir / _name()
    out.write_bytes(b"previous chart")
    os.chmod(out, 0o600)

    def broken_replace(src, dst, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        mc.render_gex_chart(out, **_qqq_gex())
    assert out.read_bytes() == b"previous chart"
    assert [p.name for p in chart_dir.iterdir()] == [out.name]


@pytest.mark.parametrize("case", ["group_readable", "symlink", "missing", "not_a_dir"])
def test_chart_directory_must_be_own_private_dir(tmp_path, case):
    private = _chart_dir(tmp_path)
    if case == "group_readable":
        target = tmp_path / "open"
        target.mkdir()
        os.chmod(target, 0o755)
    elif case == "symlink":
        target = tmp_path / "link"
        target.symlink_to(private, target_is_directory=True)
    elif case == "missing":
        target = tmp_path / "missing"
    else:
        target = tmp_path / "plain_file"
        target.write_bytes(b"x")
    with pytest.raises(mc.ChartError) as caught:
        mc.render_gex_chart(target / _name(), **_qqq_gex())
    assert caught.value.kind == "dir"
    assert list(private.iterdir()) == []  # symlink 指到的私有目錄也沒被寫
    assert not (tmp_path / "missing").exists()  # 這裡只檢查、不建立
    if case == "group_readable":
        assert list(target.iterdir()) == []


def test_too_big_png_is_refused_without_writing(tmp_path, monkeypatch):
    chart_dir = _chart_dir(tmp_path)
    out = chart_dir / _name()
    monkeypatch.setattr(mc, "MAX_BYTES", 1000)
    with pytest.raises(mc.ChartError) as caught:
        mc.render_oi_chart(out, **_qqq_oi())
    assert caught.value.kind == "too_big"
    assert isinstance(caught.value, ValueError)
    assert list(chart_dir.iterdir()) == []


@pytest.mark.parametrize("size, accepted", [(mc.MAX_BYTES - 1, True), (mc.MAX_BYTES, False)])
def test_size_limit_is_checked_on_the_single_encoding(tmp_path, monkeypatch, size, accepted):
    calls: list[int] = []

    def fixed_size(figure):
        calls.append(size)
        return PNG_SIGNATURE + b"\0" * (size - len(PNG_SIGNATURE))

    monkeypatch.setattr(mc, "_encode_png", fixed_size)
    chart_dir = _chart_dir(tmp_path)
    out = chart_dir / _name()
    if accepted:
        mc.render_gex_chart(out, **_qqq_gex())
        assert out.stat().st_size == size
    else:
        with pytest.raises(mc.ChartError) as caught:
            mc.render_gex_chart(out, **_qqq_gex())
        assert caught.value.kind == "too_big"
        assert list(chart_dir.iterdir()) == []
    assert calls == [size]  # 只編碼一次，不重畫、不降 dpi


# ── 輸入檢查 ──────────────────────────────────────────────────────────────────
_SMALL_OI = {
    "title": "QQQ",
    "strikes": [700.0, 710.0, 720.0],
    "call_oi": [10, 20, 30],
    "put_oi": [30, 20, 10],
    "spot": 710.0,
    "call_wall": 720.0,
    "put_wall": 700.0,
    "max_pain": 710.0,
    "range_note": "範圍：現價 ±15%",
}


@pytest.mark.parametrize(
    "override",
    [
        {"call_oi": [10, 20]},  # 長度不一
        {"put_oi": [30, 20, 10, 5]},
        {"call_oi": [10, -1, 30]},  # 負的未平倉
        {"put_oi": [30, float("nan"), 10]},
        {"call_oi": [10, None, 30]},
        {"strikes": [700.0, float("inf"), 720.0]},
        {"strikes": [700.0, 0.0, 720.0]},
        {"strikes": "700,710,720"},
        {"spot": 0},
        {"spot": -710.0},
        {"spot": float("nan")},
        {"spot": True},
        {"spot": "710"},
        {"call_wall": float("nan")},
        {"max_pain": -1.0},
        {"title": None},
        {"range_note": 15},
    ],
)
def test_bad_oi_input_raises_data_error_and_writes_nothing(tmp_path, override):
    chart_dir = _chart_dir(tmp_path)
    with pytest.raises(mc.ChartError) as caught:
        mc.render_oi_chart(chart_dir / _name(), **dict(_SMALL_OI, **override))
    assert caught.value.kind == "data"
    assert list(chart_dir.iterdir()) == []


@pytest.mark.parametrize(
    "override",
    [
        {"gex": [1.0, 2.0]},  # 長度不一
        {"gex": [float("nan")] * 61},
        {"gex": [float("inf")] + [0.0] * 60},
        {"prices": [0.0] * 61},
        {"spot": 0.0},
        {"crossings": [(702.0,)]},
        {"crossings": [(702.0, "由負轉正", "x")]},
        {"crossings": [(702.0, 1)]},
        {"crossings": [(float("nan"), "由負轉正")]},
        {"crossings": [{"price": 702.0}]},
        {"note": None},
    ],
)
def test_bad_gex_input_raises_data_error_and_writes_nothing(tmp_path, override):
    chart_dir = _chart_dir(tmp_path)
    with pytest.raises(mc.ChartError) as caught:
        mc.render_gex_chart(chart_dir / _name(), **_qqq_gex(**override))
    assert caught.value.kind == "data"
    assert list(chart_dir.iterdir()) == []


# ── 圖的內容 ──────────────────────────────────────────────────────────────────
def test_oi_chart_shows_spot_walls_and_max_pain_within_spot_15pct(oi_figure):
    axes = oi_figure.axes[0]
    assert axes.get_xlim() == pytest.approx((SPOT * 0.85, SPOT * 1.15))
    texts = _all_text(oi_figure)
    # 圖例寫「（月選）」：長條與買／賣權牆是 45 天內合併，最大痛點只用月選（market_publish）
    for label in ("現價 717.32", "買權牆 750", "賣權牆 700", "最大痛點（月選） 715", "範圍：現價 ±15%｜資料 10/16"):
        assert label in texts
    assert sorted(_vlines_x(axes)) == pytest.approx(sorted([SPOT, 750.0, 700.0, 715.0]))


def test_oi_bars_calls_up_puts_down_only_inside_window(oi_figure):
    from matplotlib.colors import to_rgb
    from matplotlib.patches import Rectangle

    axes = oi_figure.axes[0]
    low, high = SPOT * 0.85, SPOT * 1.15
    inside = [k for k in _qqq_oi()["strikes"] if low <= k <= high]
    bars = [p for p in axes.patches if isinstance(p, Rectangle)]
    calls = [p for p in bars if to_rgb(p.get_facecolor()) == pytest.approx(to_rgb(mc.BLUE))]
    puts = [p for p in bars if to_rgb(p.get_facecolor()) == pytest.approx(to_rgb(mc.AMBER))]
    assert len(calls) == len(puts) == len(inside)
    assert all(p.get_height() >= 0 for p in calls)
    assert all(p.get_height() <= 0 for p in puts)
    centers = sorted(p.get_x() + p.get_width() / 2 for p in calls)
    assert centers[0] >= low and centers[-1] <= high


def test_duplicate_strikes_are_summed():
    figure = mc._build_oi_figure(**dict(_SMALL_OI, strikes=[700.0, 700.0, 710.0], call_oi=[5, 7, 3], put_oi=[1, 2, 3]))
    from matplotlib.patches import Rectangle

    bars = [p for p in figure.axes[0].patches if isinstance(p, Rectangle)]
    heights = sorted(round(p.get_height()) for p in bars)
    assert heights == [-3, -3, 3, 12]


def test_out_of_range_max_pain_is_listed_not_drawn(txo_figure):
    axes = txo_figure.axes[0]
    texts = _all_text(txo_figure)
    assert "最大痛點（月選） 39,000（範圍外）" in texts
    assert "買權牆 54,000" in texts and "賣權牆 46,000" in texts and "現價 47,111" in texts
    assert 39000.0 not in _vlines_x(axes)
    assert axes.get_xlim() == pytest.approx((47111.26 * 0.85, 47111.26 * 1.15))


def test_gex_chart_shows_spot_crossings_and_note(gex_figure):
    axes = gex_figure.axes[0]
    assert axes.get_xlim() == pytest.approx((SPOT * 0.85, SPOT * 1.15))
    texts = _all_text(gex_figure)
    for label in ("現價 717.32", "變號 702（由負轉正）", "變號 745（由正轉負）", "假設造市商持有買權、賣出賣權；看不到實際部位"):
        assert label in texts
    assert sorted(_vlines_x(axes)) == pytest.approx(sorted([SPOT, 702.0, 745.0]))
    low, high = axes.get_ylim()
    assert low < 0 < high  # 0 線一定在圖內


def test_gex_direction_codes_are_not_printed_and_outside_crossings_skipped():
    figure = mc._build_gex_figure(**_qqq_gex(crossings=[(702.0, "up"), (745.0, "pos_to_neg"), (900.0, "由正轉負")]))
    texts = _all_text(figure)
    assert "變號 702" in texts and "變號 745" in texts
    assert not any("up" in t or "pos_to_neg" in t or "900" in t for t in texts)
    assert 900.0 not in _vlines_x(figure.axes[0])


def test_zero_or_empty_data_shows_only_zero_tick():
    for figure in (
        mc._build_oi_figure(**dict(_SMALL_OI, call_oi=[0, 0, 0], put_oi=[0, 0, 0])),
        mc._build_gex_figure(**_qqq_gex(gex=[0.0] * 61, crossings=[])),
        mc._build_gex_figure(**_qqq_gex(prices=[], gex=[], crossings=[])),
    ):
        figure.canvas.draw()
        axes = figure.axes[0]
        assert [label.get_text() for label in axes.get_yticklabels() if label.get_text()] == ["0"]


def test_caller_text_is_never_parsed_as_mathtext():
    figure = mc._build_gex_figure(**_qqq_gex(title="成本 $5 到 $10 的部位", note="假設 $x$ 不是數學式", crossings=[(702.0, "由$負$轉正")]))
    from matplotlib.text import Text

    figure.canvas.draw()
    texts = [t for t in figure.findobj(Text) if "$" in t.get_text()]
    assert len(texts) == 3
    assert all(t.get_parse_math() is False for t in texts)


def test_number_formats():
    assert mc._fmt_price(717.32) == "717.32"
    assert mc._fmt_price(750.0) == "750"
    assert mc._fmt_price(717.5) == "717.5"
    assert mc._fmt_price(47111.26) == "47,111"
    assert mc._fmt_count(-25000) == "2.5萬"
    assert mc._fmt_count(5000) == "5,000"
    assert mc._fmt_count(0) == "0"
    assert mc._fmt_dollars(2.0e10) == "+200億"
    assert mc._fmt_dollars(-1.5e9) == "−15億"
    assert mc._fmt_dollars(3.0e5) == "+30萬"
    assert mc._fmt_dollars(0.0) == "0"
    assert mc._fmt_dollars(0.5) == "+0.5"
    assert mc._fmt_dollars(-1234.0) == "−1,234"


# ── 字的大小、位置、顏色 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("figure_name", ["oi_figure", "gex_figure", "txo_figure"])
def test_every_text_is_at_least_28px(request, figure_name):
    from matplotlib.text import Text

    figure = request.getfixturevalue(figure_name)
    figure.canvas.draw()
    axes = figure.axes[0]
    ticks = {
        id(label)
        for axis in (axes.xaxis, axes.yaxis)
        for tick in list(axis.majorTicks) + list(axis.minorTicks)
        for label in (tick.label1, tick.label2)
    }
    texts = [t for t in figure.findobj(Text) if t.get_visible() and t.get_text().strip()]
    assert len(texts) > 10
    for text in texts:
        assert text.get_fontsize() * mc.DPI / 72 >= mc.MIN_TEXT_PX, text.get_text()
        if id(text) not in ticks:
            assert text.get_fontsize() >= mc.LABEL_PT, text.get_text()


@pytest.mark.parametrize("figure_name", ["oi_figure", "gex_figure", "txo_figure"])
def test_everything_stays_inside_canvas_without_overlap(request, figure_name):
    figure = request.getfixturevalue(figure_name)
    figure.canvas.draw()
    renderer = figure.canvas.get_renderer()
    box = figure.get_tightbbox(renderer)  # 英吋
    assert box.x0 * mc.DPI >= -1 and box.y0 * mc.DPI >= -1
    assert box.x1 * mc.DPI <= mc.WIDTH_PX + 1 and box.y1 * mc.DPI <= mc.HEIGHT_PX + 1
    title, footer = figure.texts[0], figure.texts[1]
    parts = {
        "title": title.get_window_extent(renderer),
        "legend": figure.legends[0].get_window_extent(renderer),
        "footer": footer.get_window_extent(renderer),
        "axes": figure.axes[0].get_tightbbox(renderer),
    }
    names = list(parts)
    for i, first in enumerate(names):
        for second in names[i + 1 :]:
            assert not parts[first].overlaps(parts[second]), (first, second)
    assert parts["title"].y0 > parts["legend"].y1 > parts["axes"].y1
    assert parts["axes"].y0 > parts["footer"].y1


def test_crossing_labels_do_not_overlap_each_other():
    # 兩個變號點都在現價同一側、只差 3：第二個要換到下一列
    figure = mc._build_gex_figure(**_qqq_gex(crossings=[(740.0, "由正轉負"), (743.0, "由負轉正")]))
    figure.canvas.draw()
    renderer = figure.canvas.get_renderer()
    axes = figure.axes[0]
    labels = [t for t in axes.texts if t.get_text().startswith("變號")]
    assert len(labels) == 2
    boxes = [t.get_window_extent(renderer) for t in labels]
    assert not boxes[0].overlaps(boxes[1])
    frame = axes.get_window_extent(renderer)
    for one in boxes:
        assert frame.x0 - 1 <= one.x0 and one.x1 <= frame.x1 + 1


def test_long_title_and_footer_wrap_inside_canvas():
    long_note = "假設造市商持有買權、賣出賣權；看不到實際部位｜資料 10/16（CBOE）收到的有效合約範圍 45 天內到期"
    long_title = "QQQ 假設部位 Gamma（造市商避險量）｜10/16 收盤後｜收到的有效合約 10,954 筆"
    figure = mc._build_gex_figure(**_qqq_gex(title=long_title, note=long_note))
    figure.canvas.draw()
    renderer = figure.canvas.get_renderer()
    title, footer = figure.texts[0], figure.texts[1]
    assert "\n" in footer.get_text() and "".join(footer.get_text().split()) == "".join(long_note.split())
    for text in (title, footer):
        extent = text.get_window_extent(renderer)
        assert extent.x0 >= 0 and extent.x1 <= mc.WIDTH_PX
        assert text.get_fontsize() >= mc.LABEL_PT


def _brand_or_grey(rgba) -> bool:
    from matplotlib.colors import to_rgb

    r, g, b, a = (float(v) for v in rgba)
    if a == 0:
        return True
    if abs(r - g) < 1e-9 and abs(g - b) < 1e-9:
        return True
    return any((r, g, b) == pytest.approx(to_rgb(ref), abs=1e-9) for ref in (mc.BLUE, mc.AMBER))


def _artist_colors(figure) -> list[tuple[str, tuple]]:
    from matplotlib.collections import Collection
    from matplotlib.colors import to_rgba
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    from matplotlib.text import Text

    figure.canvas.draw()
    found: list[tuple[str, tuple]] = []
    for artist in figure.findobj():
        if not artist.get_visible():
            continue
        name = type(artist).__name__
        if isinstance(artist, Line2D):
            for color in (artist.get_color(), artist.get_markerfacecolor(), artist.get_markeredgecolor()):
                found.append((name, to_rgba(color, artist.get_alpha())))
        elif isinstance(artist, Patch):
            found += [(name, tuple(artist.get_facecolor())), (name, tuple(artist.get_edgecolor()))]
        elif isinstance(artist, Collection):
            found += [(name, tuple(row)) for row in list(artist.get_facecolors()) + list(artist.get_edgecolors())]
        elif isinstance(artist, Text):
            found.append((name, to_rgba(artist.get_color(), artist.get_alpha())))
            patch = artist.get_bbox_patch()
            if patch is not None:
                found += [(name, tuple(patch.get_facecolor())), (name, tuple(patch.get_edgecolor()))]
    return found


@pytest.mark.parametrize("figure_name", ["oi_figure", "gex_figure", "txo_figure"])
def test_artists_use_only_blue_amber_and_grey(request, figure_name):
    colors = _artist_colors(request.getfixturevalue(figure_name))
    assert len(colors) > 20
    bad = [(name, rgba) for name, rgba in colors if not _brand_or_grey(rgba)]
    assert bad == []


def _off_palette_colors(path: Path) -> int:
    """PNG 裡「不是灰、也不是藍或橘系」的顏色數。

    看色度方向：(R−G, G−B)。藍或橘和白／黑／灰混合（反鋸齒、半透明底色）只會縮放這個向量、方向不變；
    藍橘互混時兩者幾乎互補、向量會縮到接近 0。所以合格＝向量很短（≤12，接近灰），或方向和藍、橘相差
    12° 以內。紅（約 0°）、綠（約 135°）、matplotlib 預設藍（約 215°）都會被抓到。
    """
    np = pytest.importorskip("numpy")
    image_module = pytest.importorskip("PIL.Image")
    from matplotlib.colors import to_rgb

    with image_module.open(path) as image:
        pixels = np.asarray(image.convert("RGB"), dtype=np.int64).reshape(-1, 3)
    packed = np.unique((pixels[:, 0] << 16) | (pixels[:, 1] << 8) | pixels[:, 2])
    red, green, blue = (packed >> 16) & 255, (packed >> 8) & 255, packed & 255
    first, second = (red - green).astype(float), (green - blue).astype(float)
    angle = np.degrees(np.arctan2(second, first)) % 360

    def direction(hex_color: str) -> float:
        r, g, b = (255 * channel for channel in to_rgb(hex_color))
        return float(np.degrees(np.arctan2(g - b, r - g)) % 360)

    def near(target: float):
        return np.abs((angle - target + 180) % 360 - 180) <= 12

    allowed = (np.hypot(first, second) <= 12) | near(direction(mc.BLUE)) | near(direction(mc.AMBER))
    return int((~allowed).sum())


def test_png_pixels_stay_inside_blue_amber_grey_palette(tmp_path):
    chart_dir = _chart_dir(tmp_path)
    for render, kwargs in ((mc.render_oi_chart, _qqq_oi()), (mc.render_gex_chart, _qqq_gex()), (mc.render_oi_chart, _txo_oi())):
        out = chart_dir / _name()
        render(out, **kwargs)
        assert _off_palette_colors(out) == 0


def test_palette_check_catches_red_and_green(tmp_path):
    image_module = pytest.importorskip("PIL.Image")
    sample = tmp_path / "rg.png"
    image = image_module.new("RGB", (4, 1), (255, 255, 255))
    image.putpixel((1, 0), (0xD6, 0x27, 0x28))  # matplotlib 預設紅
    image.putpixel((2, 0), (0x2C, 0xA0, 0x2C))  # matplotlib 預設綠
    image.putpixel((3, 0), (0x1F, 0x77, 0xB4))  # matplotlib 預設藍（不是 #1E5AA8）
    image.save(sample)
    assert _off_palette_colors(sample) == 3


# ── 字型 ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "available, expected",
    [
        ({"PingFang TC", "Heiti TC", "Arial Unicode MS"}, "PingFang TC"),
        ({"Heiti TC", "Arial Unicode MS", "PingFang HK"}, "Heiti TC"),
        ({"Arial Unicode MS", "Songti SC"}, "Arial Unicode MS"),
        ({"PingFang HK", "Hiragino Sans"}, mc.DEFAULT_FONT),
        (set(), mc.DEFAULT_FONT),
    ],
)
def test_font_preference_order_and_cache(monkeypatch, available, expected):
    monkeypatch.setattr(mc, "_font_choice", None)
    monkeypatch.setattr(mc, "_available_font_names", lambda: set(available))
    assert mc.ensure_font_cache() == expected

    def must_not_query():
        raise AssertionError("字型應該只選一次")

    monkeypatch.setattr(mc, "_available_font_names", must_not_query)
    assert mc.ensure_font_cache() == expected
    families = mc._families()
    assert families[0] == expected and families[-1] == mc.DEFAULT_FONT


def test_ensure_font_cache_returns_a_known_font():
    assert mc.ensure_font_cache() in mc.FONT_CANDIDATES + (mc.DEFAULT_FONT,)


def test_charts_render_without_any_cjk_font(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "_font_choice", mc.DEFAULT_FONT)
    assert mc._families() == [mc.DEFAULT_FONT]
    chart_dir = _chart_dir(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # 缺中文字型時 matplotlib 會對每個中文字警告；這裡只驗不會失敗
        mc.render_oi_chart(chart_dir / "a.png", **_qqq_oi())
        mc.render_gex_chart(chart_dir / "b.png", **_qqq_gex())
    _assert_valid_png(chart_dir / "a.png")
    _assert_valid_png(chart_dir / "b.png")


def test_no_warnings_with_cjk_font(tmp_path):
    if mc.ensure_font_cache() == mc.DEFAULT_FONT:
        pytest.skip("這台沒有中文字型")
    chart_dir = _chart_dir(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)  # 缺字、ylim 相同、版面塞不下都是 UserWarning
        mc.render_oi_chart(chart_dir / _name(), **_qqq_oi())
        mc.render_oi_chart(chart_dir / _name(), **_txo_oi())
        mc.render_oi_chart(chart_dir / _name(), **dict(_SMALL_OI, call_oi=[0, 0, 0], put_oi=[0, 0, 0]))
        mc.render_gex_chart(chart_dir / _name(), **_qqq_gex())
        mc.render_gex_chart(chart_dir / _name(), **_qqq_gex(gex=[0.0] * 61, crossings=[]))
        mc.render_gex_chart(chart_dir / _name(), **_qqq_gex(prices=[], gex=[], crossings=[], title="成本 $5 到 $10"))


# ── 併發 ──────────────────────────────────────────────────────────────────────
def test_concurrent_renders_in_threads(tmp_path):
    chart_dir = _chart_dir(tmp_path)
    errors: list[BaseException] = []
    outputs: list[Path] = []

    def work(index: int) -> None:
        try:
            oi_path, gex_path = chart_dir / f"oi{index}.png", chart_dir / f"gex{index}.png"
            mc.render_oi_chart(oi_path, **_qqq_oi())
            mc.render_gex_chart(gex_path, **_qqq_gex())
            outputs.extend([oi_path, gex_path])
        except BaseException as exc:  # noqa: BLE001 - 收集起來在主執行緒判斷
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert errors == []
    assert len(outputs) == 4
    for path in outputs:
        _assert_valid_png(path)
    assert sorted(p.name for p in chart_dir.iterdir()) == sorted(p.name for p in outputs)
