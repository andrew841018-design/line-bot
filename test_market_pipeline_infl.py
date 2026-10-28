"""market_pipeline_infl（/盤勢 通膨 collect 任務，2026-10-24）的測試。

用真的 market_store.Store（暫存目錄）、真的 market_calendar 日曆、真的 market_sources_infl 解析器；網路換成假的 http
（財政部的檔與 BLS 的回應，都是從保存的真實回應裁出的；BLS 2026-09 之後的月份是合成的，只給輪詢測試用），帳本換成假的
（另有一個用真的 StoreLedger）。時間一律注入（ctx.now、假的 monotonic clock）。不連網、不碰正式 state。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

import market_calc
import market_calendar
import market_http
import market_paths
import market_pipeline_infl as pipe
import market_pipeline_us
import market_sources_infl as src
import market_store
import market_tasks

ET = ZoneInfo("America/New_York")
TPE = ZoneInfo("Asia/Taipei")
UTC = timezone.utc
TASKS = {task.name: task for task in pipe.TASKS}

D = date(2026, 10, 22)  # 週四
NOW = datetime(2026, 10, 22, 17, 0, tzinfo=ET).timestamp()  # 美東收盤後（D＝10/22）


def at(year, month, day, hour=0, minute=0, tz=ET) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=tz).timestamp()


# ── 假的帳本、時鐘、http ─────────────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.wall = NOW

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.wall += seconds


class FakeLedger:
    """market_http.Ledger：記下預扣；可設某主機第 N 次起說額度不夠；403／429 之後冷卻。"""

    def __init__(self) -> None:
        self.reserved: list[str] = []
        self.cooling: dict[str, float] = {}
        self.cap: dict[str, int] = {}

    def reserve(self, host: str) -> None:
        if host in self.cooling:
            raise market_http.FetchError("cooldown")
        if host in self.cap and self.reserved.count(host) >= self.cap[host]:
            raise market_http.FetchError("budget")
        self.reserved.append(host)

    def cooldown(self, host: str, seconds: float) -> None:
        self.cooling[host] = seconds


CURVES = {"daily_treasury_yield_curve": "nominal", "daily_treasury_real_yield_curve": "real"}


class FakeHttp:
    """假的 market_http：依 type 與月份回財政部的檔、依序列代號回 BLS 的回應；先預扣帳本（和真的 market_http 一樣），
    每次都檢查帳本就是 ctx.ledger。"""

    FetchError = market_http.FetchError

    def __init__(self, ledger: Any, clock: FakeClock) -> None:
        self.ledger = ledger
        self.clock = clock
        self.calls: list[tuple[str, str]] = []  # 財政部：(曲線, 年月)
        self.csv: dict[tuple[str, str], str] = {}  # (曲線, 年月) → 檔內容；沒有的月份＝財政部還沒建檔（非 200）
        self.fail: dict[tuple[str, str], str] = {}  # (曲線, 年月或 "*") → FetchError kind
        self.bls_calls: list[str] = []  # BLS：序列代號（依請求順序）
        self.bls: dict[str, Any] = {}  # 序列代號 → 整份回應（dict 或 bytes）；沒給就用保存的真實值
        self.bls_fail: dict[str, str] = {}  # 序列代號或 "*" → FetchError kind
        self.bls_through = "2026-09"  # BLS 現在公布到哪個月（保存的真實值到 2026-09，之後是合成）
        self.bls_months: dict[str, str] = {}  # 個別序列的公布月份（覆寫 bls_through）
        self.seconds_per_call = 0.0

    def requested(self, curve: Optional[str] = None) -> list[str]:
        return [ym for kind, ym in self.calls if curve in (None, kind)]

    def fetch(self, url, *, max_bytes, timeout=15.0, accept=("json",), headers=None, ledger=None):
        assert ledger is self.ledger, "every fetch must get ctx.ledger"
        parts = urlsplit(url)
        host = parts.hostname or ""
        assert parts.scheme == "https" and host in market_http.ALLOWED_HOSTS
        ledger.reserve(host)
        self.clock.sleep(self.seconds_per_call)
        if host == "api.bls.gov":
            return self._bls(url, parts, max_bytes, accept)
        query = {key: values[0] for key, values in parse_qs(parts.query, keep_blank_values=True).items()}
        curve, ym = CURVES[query["type"]], query["field_tdr_date_value_month"]
        assert parts.path.endswith(f"/daily-treasury-rates.csv/all/{ym}") and accept == ("csv",)
        self.calls.append((curve, ym))
        kind = self.fail.get((curve, ym)) or self.fail.get((curve, "*"))
        if kind:
            if kind in ("blocked", "rate_limited"):
                ledger.cooldown(host, 7200.0)
            raise market_http.FetchError(kind)
        body = self.csv.get((curve, ym))
        if body is None:
            raise market_http.FetchError("status")
        return market_http.Response(200, body.encode("utf-8"), {"content-type": "text/csv"}, url)

    def _bls(self, url, parts, max_bytes, accept):
        assert accept == ("json",) and not parts.query and parts.path.startswith("/publicAPI/v1/timeseries/data/")
        series_id = parts.path.rsplit("/", 1)[1]
        self.bls_calls.append(series_id)
        kind = self.bls_fail.get(series_id) or self.bls_fail.get("*")
        if kind:
            if kind in ("blocked", "rate_limited"):
                self.ledger.cooldown(parts.hostname, 7200.0)
            raise market_http.FetchError(kind)
        body = self.bls.get(series_id)
        if body is None:
            body = bls_response(series_id, saved_values(series_id, self.bls_months.get(series_id, self.bls_through)))
        raw = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
        assert len(raw) <= max_bytes
        return market_http.Response(200, bytes(raw), {"content-type": "application/json"}, url)


# ── 財政部的檔 ───────────────────────────────────────────────────────────────

# 裁自 research_bodies/infl_diag/ust_nominal_202610.body（2026-10-23 抓的 10 月名目殖利率；全部 15 列、14 欄，沒有 10/12 哥倫布日）
NOMINAL_202610 = """Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"
10/22/2026,4.11,4.10,4.16,4.22,4.29,4.29,4.41,4.65,4.72,4.78,4.86,4.96,5.32,5.29
10/21/2026,4.09,4.12,4.16,4.25,4.34,4.35,4.52,4.81,4.89,4.95,5.03,5.13,5.47,5.42
10/20/2026,4.10,4.13,4.17,4.26,4.34,4.36,4.51,4.79,4.86,4.94,5.02,5.12,5.47,5.42
10/19/2026,4.11,4.14,4.17,4.27,4.34,4.37,4.50,4.77,4.85,4.94,5.04,5.15,5.53,5.48
10/16/2026,4.12,4.14,4.17,4.26,4.32,4.34,4.46,4.74,4.82,4.89,4.98,5.09,5.47,5.43
10/15/2026,4.17,4.15,4.19,4.28,4.35,4.35,4.49,4.77,4.85,4.91,5.00,5.10,5.48,5.43
10/14/2026,4.11,4.15,4.15,4.28,4.36,4.37,4.53,4.82,4.92,5.00,5.09,5.19,5.57,5.52
10/13/2026,4.10,4.15,4.15,4.26,4.32,4.34,4.48,4.79,4.90,5.00,5.11,5.21,5.60,5.56
10/09/2026,4.13,4.13,4.13,4.25,4.29,4.32,4.47,4.80,4.89,5.02,5.13,5.24,5.65,5.60
10/08/2026,4.14,4.14,4.13,4.23,4.29,4.30,4.44,4.75,4.85,4.99,5.11,5.22,5.64,5.60
10/07/2026,4.07,4.13,4.14,4.22,4.28,4.28,4.42,4.77,4.87,5.03,5.15,5.28,5.71,5.67
10/06/2026,4.06,4.12,4.13,4.21,4.26,4.28,4.46,4.79,4.88,5.03,5.15,5.27,5.68,5.64
10/05/2026,4.05,4.10,4.13,4.22,4.27,4.30,4.47,4.84,4.97,5.06,5.19,5.31,5.70,5.66
10/02/2026,4.04,4.09,4.11,4.19,4.26,4.27,4.46,4.83,4.96,5.06,5.17,5.28,5.67,5.63
10/01/2026,4.06,4.10,4.13,4.17,4.26,4.27,4.44,4.78,4.91,5.01,5.12,5.24,5.64,5.61"""

# 裁自 research_bodies/infl_diag/ust_real_202610.body（同一天抓的 10 月實質殖利率；欄只有 5／7／10／20／30 年，大寫的 "5 YR"）
REAL_202610 = """Date,"5 YR","7 YR","10 YR","20 YR","30 YR"
10/22/2026,2.33,2.48,2.64,2.91,3.05
10/21/2026,2.55,2.65,2.76,3.01,3.15
10/20/2026,2.55,2.65,2.77,3.02,3.16
10/19/2026,2.56,2.67,2.80,3.06,3.21
10/16/2026,2.52,2.62,2.74,3.01,3.16
10/15/2026,2.58,2.67,2.78,3.03,3.18
10/14/2026,2.64,2.73,2.84,3.09,3.24
10/13/2026,2.67,2.77,2.88,3.13,3.28
10/09/2026,2.68,2.79,2.91,3.17,3.32
10/08/2026,2.62,2.74,2.87,3.15,3.31
10/07/2026,2.66,2.78,2.92,3.20,3.36
10/06/2026,2.66,2.77,2.91,3.19,3.35
10/05/2026,2.70,2.82,2.95,3.22,3.37
10/02/2026,2.69,2.80,2.92,3.19,3.34
10/01/2026,2.65,2.76,2.88,3.16,3.31"""

# 裁自 research_bodies/infl_diag/ust_real_202609.body（9 月實質殖利率，21 列）
REAL_202609 = """Date,"5 YR","7 YR","10 YR","20 YR","30 YR"
09/30/2026,2.73,2.82,2.93,3.18,3.33
09/29/2026,2.72,2.80,2.91,3.15,3.29
09/28/2026,2.73,2.80,2.90,3.14,3.28
09/25/2026,2.64,2.73,2.83,3.08,3.22
09/24/2026,2.70,2.77,2.85,3.08,3.21
09/23/2026,2.65,2.70,2.76,2.99,3.14
09/22/2026,2.51,2.56,2.63,2.88,3.04
09/21/2026,2.50,2.55,2.62,2.86,3.02
09/18/2026,2.55,2.60,2.68,2.93,3.09
09/17/2026,2.46,2.52,2.61,2.87,3.04
09/16/2026,2.51,2.59,2.68,2.93,3.09
09/15/2026,2.42,2.51,2.62,2.90,3.07
09/14/2026,2.40,2.49,2.60,2.89,3.05
09/11/2026,2.38,2.48,2.60,2.89,3.07
09/10/2026,2.29,2.41,2.55,2.87,3.05
09/09/2026,2.20,2.32,2.46,2.79,2.98
09/08/2026,2.17,2.29,2.43,2.77,2.96
09/04/2026,2.17,2.29,2.43,2.77,2.96
09/03/2026,2.15,2.27,2.42,2.76,2.96
09/02/2026,2.19,2.31,2.45,2.78,2.98
09/01/2026,2.18,2.30,2.44,2.78,2.98"""


# 裁自 research_bodies/infl_diag/bls_get_cuur0000sa0.body（CUUR0000SA0）與 bls_post_3series.body（其餘三個序列）：
# 每個序列 2024-01…2026-09 共 33 個月，值照原字串（"336.940"）；2025-10 的 value 是 "-"（2025 年聯邦政府撥款中斷，BLS 註腳
# "Data unavailable due to the 2025 lapse in appropriations"）。CUUR0000SA0 的原始回應另有 2 個年平均列（2024、2025 的 M13），
# 所以是 35 列；季調序列沒有 M13（33 列）——用位置往前數 12 列，未季調會落在 2025-M10 的 "-"、季調剛好是對的月份。
BLS_YEARS = {
    "CUUR0000SA0": {
        "2024": ['308.417', '310.326', '312.332', '313.548', '314.069', '314.175', '314.540', '314.796', '315.301', '315.664', '315.493', '315.605'],
        "2025": ['317.671', '319.082', '319.799', '320.795', '321.465', '322.561', '323.048', '323.976', '324.800', '-', '324.122', '324.054'],
        "2026": ['325.252', '326.785', '330.213', '333.020', '335.123', '333.952', '333.918', '334.980', '336.940'],
    },
    "CUSR0000SA0": {
        "2024": ['309.698', '310.967', '312.345', '313.023', '313.175', '313.044', '313.569', '314.062', '314.732', '315.631', '316.528', '317.604'],
        "2025": ['318.961', '319.679', '319.785', '320.302', '320.620', '321.435', '322.169', '323.291', '324.245', '-', '325.063', '326.031'],
        "2026": ['326.588', '327.460', '330.293', '332.407', '333.979', '332.568', '332.813', '334.131', '336.298'],
    },
    "CUUR0000SA0L1E": {
        "2024": ['313.623', '315.419', '317.088', '317.978', '318.629', '319.003', '319.214', '320.017', '321.109', '321.758', '321.947', '322.007'],
        "2025": ['323.842', '325.252', '325.933', '326.815', '327.509', '328.364', '328.980', '329.970', '330.804', '-', '330.425', '330.506'],
        "2026": ['331.950', '333.242', '334.391', '335.803', '336.846', '336.882', '337.133', '338.041', '339.287'],
    },
    "CUSR0000SA0L1E": {
        "2024": ['314.319', '315.564', '316.792', '317.649', '318.095', '318.390', '318.940', '319.750', '320.732', '321.731', '322.657', '323.259'],
        "2025": ['324.638', '325.465', '325.690', '326.467', '326.893', '327.658', '328.682', '329.700', '330.418', '-', '331.043', '331.814'],
        "2026": ['332.793', '333.512', '334.165', '335.423', '336.121', '336.065', '336.789', '337.765', '338.896'],
    },
}

NOMINAL_HEADER = NOMINAL_202610.splitlines()[0]
REAL_HEADER = REAL_202610.splitlines()[0]


def make_csv(header_line: str, rows: dict) -> str:
    """{日期: (5 年, 10 年)} → 財政部格式的檔（新日期在上）；其他天期放固定值（解析器不看）。"""
    names = [name.strip() for name in next(csv.reader([header_line]))]
    lines = [header_line]
    for day in sorted(rows, reverse=True):
        y5, y10 = rows[day]
        cells = []
        for name in names[1:]:
            value = y5 if name.lower() == "5 yr" else y10 if name.lower() == "10 yr" else 4.0
            cells.append("" if value is None else f"{value:.2f}")
        lines.append(f"{day.month:02d}/{day.day:02d}/{day.year}," + ",".join(cells))
    return "\n".join(lines) + "\n"


# 合成：9 月的名目檔沒有保存（只存了 10 月名目與 9、10 月實質）；名目＝實質＋2.40（5 年）／＋2.35（10 年），日期同保存的 9 月實質檔
NOMINAL_202609 = make_csv(NOMINAL_HEADER, {
    point.date: (round(point.y5 + 2.40, 2), round(point.y10 + 2.35, 2))
    for point in src.parse_treasury_yields(REAL_202609, "real")})


# ── 環境 ─────────────────────────────────────────────────────────────────────


@dataclass
class Env:
    paths: market_paths.Paths
    store: market_store.Store
    clock: FakeClock
    ledger: Any
    http: FakeHttp
    extra: dict = field(default_factory=dict)


@pytest.fixture
def env(tmp_path: Path):
    paths = market_paths.Paths.under(tmp_path)
    paths.state_dir.mkdir(mode=0o700)
    clock = FakeClock()
    store = market_store.Store(paths.history_db, now_fn=lambda: clock.wall)
    ledger = FakeLedger()
    yield Env(paths, store, clock, ledger, FakeHttp(ledger, clock))
    store.close()


def ctx_at(env: Env, now: float) -> market_tasks.Context:
    """一輪 collect 的 Context：日曆照 job 的做法從 store 建。"""
    env.clock.wall = now
    return market_tasks.Context(
        store=env.store, http=env.http, ledger=env.ledger, now=now, paths=env.paths,
        deadline=env.clock() + 600.0, us_cal=market_calendar.us_calendar(env.store),
        tw_cal=market_calendar.Calendar("tw"), clock=env.clock,
    )


def run(env: Env, name: str, now: float = NOW) -> str:
    """經過框架跑一個任務（含 due、例外記帳）；回框架記下的結果種類。"""
    return market_tasks.run_task(ctx_at(env, now), TASKS[name])


def feed_files(env: Env) -> None:
    """保存的 10 月名目／實質檔＋9 月實質檔（真實）＋9 月名目檔（合成）。"""
    env.http.csv.update({
        ("nominal", "202610"): NOMINAL_202610, ("real", "202610"): REAL_202610,
        ("nominal", "202609"): NOMINAL_202609, ("real", "202609"): REAL_202609,
    })


def be_rows(env: Env) -> list[dict]:
    got = env.store.get_obs(pipe.OBS_INFL_BE)
    return list((got or {}).get("payload", {}).get("rows") or [])


# ── 任務表與登記 ─────────────────────────────────────────────────────────────


def test_the_task_is_registered_after_us_ust_in_the_core_group():
    names = [task.name for task in market_tasks.registered()]
    assert names.index("us.ust") < names.index("us.infl.be")  # 同一個財政部主機：us.ust 先抓
    task = TASKS["us.infl.be"]
    assert task.group == "core" and task.market == "us" and 0 < task.budget_s <= 95


def test_the_publisher_reads_the_same_obs_key():
    import market_publish

    assert market_publish.OBS_US_INFL_BE == pipe.OBS_INFL_BE


def test_the_job_loads_the_module_last_and_a_broken_one_stops_only_its_own_tasks():
    """A22：通膨的 collect 程式在自己的模組；它 import 壞掉（這裡用 market_sources_infl 壞掉模擬）只停自己的任務。"""
    import subprocess
    import sys

    from jobs import market_snapshot as job

    assert job.PIPELINES[-1] == "market_pipeline_infl" and job.PIPELINES.index("market_pipeline_us") < len(job.PIPELINES) - 1
    code = (
        "import sys, json; sys.modules['market_sources_infl'] = None\n"
        "import market_tasks\n"
        "status = market_tasks.load_modules(['market_pipeline_us', 'market_pipeline_infl'])\n"
        "names = [t.name for t in market_tasks.registered()]\n"
        "print(json.dumps({'status': status, 'us_ust': 'us.ust' in names, 'be': 'us.infl.be' in names}))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["status"]["market_pipeline_us"] == "ok" and got["status"]["market_pipeline_infl"].startswith("import_error")
    assert got["us_ust"] is True and got["be"] is False


def test_the_code_hash_covers_the_inflation_modules():
    """部署寫下的 sha256 是 market_*.py 的 glob：新模組自動算進去（改了就要 rehash），不用另外登記。"""
    from jobs import market_snapshot as job

    names = {path.name for path in job.code_files()}
    assert {"market_pipeline_infl.py", "market_sources_infl.py"} <= names


# ── 損益兩平：名目與實質各一個項目 ─────────────────────────────────────────────


def test_cold_start_merges_both_files_into_named_rows(env):
    feed_files(env)
    assert run(env, "us.infl.be") == "ok"
    got = env.store.get_obs(pipe.OBS_INFL_BE)
    rows = got["payload"]["rows"]
    assert got["data_date"] == "2026-10-22" and len(rows) == 36  # 9 月 21 列＋10 月 15 列
    assert rows[-1] == {"date": "2026-10-22", "n5": 4.78, "n10": 4.96, "r5": 2.33, "r10": 2.64}
    assert all(set(row) == {"date", "n5", "n10", "r5", "r10"} for row in rows)  # 具名鍵，不是位置陣列
    assert [row["date"] for row in rows] == sorted(row["date"] for row in rows)
    # 冷啟動：每個檔抓本月，已存的列太少所以也抓上個月；全部走 ctx.ledger（FakeHttp 檢查）
    assert env.http.calls == [("nominal", "202610"), ("nominal", "202609"), ("real", "202610"), ("real", "202609")]
    assert env.ledger.reserved == ["home.treasury.gov"] * 4
    values = market_calc.breakeven_values(rows)  # collect 存的列接得上 publish 的計算
    assert values["y5"] == {"date": "2026-10-22", "value": 2.45, "chg5_bp": 12, "move": "up"}
    assert values["y10"]["value"] == 2.32 and values["spread_bp"] == 13


def test_a_normal_day_asks_for_one_file_per_curve(env):
    feed_files(env)
    assert run(env, "us.infl.be", at(2026, 10, 21, 17, 0)) == "ok"  # D＝10/21（檔裡有 10/22 的列，晚於 D 不收）
    assert be_rows(env)[-1]["date"] == "2026-10-21"
    env.http.calls.clear()
    assert run(env, "us.infl.be", NOW) == "ok"  # 隔天：已存的列夠多、最新一列在本月 → 每個檔只抓本月
    assert env.http.calls == [("nominal", "202610"), ("real", "202610")]
    assert be_rows(env)[-1]["date"] == "2026-10-22"


def test_us_ust_and_its_row_are_never_touched(env):
    ust = {"rows": [["2026-10-21", 4.22, 4.65, 4.96, 5.29]]}
    env.store.put_obs("us.ust", ust, data_date="2026-10-21")
    before = env.store.get_obs("us.ust")
    feed_files(env)
    env.http.fail[("real", "*")] = "status"  # 實質來源壞了
    assert run(env, "us.infl.be") == "partial"
    assert env.store.get_obs("us.ust") == before
    env.http.fail.clear()
    assert run(env, "us.infl.be", NOW + 900) == "ok"
    assert env.store.get_obs("us.ust") == before and set(env.store.obs_ids("us.")) == {"us.ust", "us.infl.be"}


def test_a_failing_real_file_does_not_stop_the_nominal_item(env):
    feed_files(env)
    env.http.fail[("real", "*")] = "status"
    assert run(env, "us.infl.be") == "partial"
    last = be_rows(env)[-1]
    assert (last["n5"], last["n10"], last["r5"], last["r10"]) == (4.78, 4.96, None, None)
    got = env.store.get_obs(pipe.OBS_INFL_BE)
    assert got["fail_streak"] == 1 and got["error"] == "status"  # 失敗記在 obs 上，每日維護看得到
    state = market_pipeline_us._state(ctx_at(env, NOW), pipe.BE_KEY)
    assert state["done"] == ["nominal"] and state["fails"] == {"real": 1}
    assert market_calc.breakeven_values(be_rows(env)) is None  # 沒有實質就沒有損益兩平，不拿名目頂替
    env.http.fail.clear()
    env.http.calls.clear()
    assert run(env, "us.infl.be", NOW + 900) == "ok"  # 下一輪只重做 real
    assert {curve for curve, _ in env.http.calls} == {"real"}
    assert be_rows(env)[-1]["r10"] == 2.64 and env.store.get_obs(pipe.OBS_INFL_BE)["fail_streak"] == 0


def test_a_rate_limited_file_cools_the_host_and_stops_the_batch(env):
    feed_files(env)
    env.http.fail[("real", "*")] = "rate_limited"
    assert run(env, "us.infl.be") == "partial"
    assert env.ledger.cooling == {"home.treasury.gov": 7200.0}
    assert env.http.requested("real") == ["202610"]  # 冷卻之後不再打同一個主機（上個月沒打）
    assert market_pipeline_us._state(ctx_at(env, NOW), pipe.BE_KEY)["fails"] == {}  # 不算該項的失敗次數


def test_the_ledger_budget_is_not_a_failure(env):
    feed_files(env)
    env.ledger.cap["home.treasury.gov"] = 1
    assert run(env, "us.infl.be") == "budget"  # 帳本說額度不夠：請求沒送出，不算失敗、不退避
    assert env.http.calls == [("nominal", "202610")]
    assert env.store.task_state("us.infl.be")["fail_streak"] == 0
    assert market_pipeline_us._state(ctx_at(env, NOW), pipe.BE_KEY)["fails"] == {}
    assert be_rows(env)[-1]["date"] == "2026-10-22"  # 拿到的那個月照存
    env.ledger.cap.clear()
    assert run(env, "us.infl.be", NOW + 900) == "ok"  # 下一輪接著做


def test_real_lagging_a_day_behind_nominal_is_not_yet_and_the_day_does_not_count(env):
    feed_files(env)
    lagging = {p.date: (p.y5, p.y10) for p in src.parse_treasury_yields(REAL_202610, "real") if p.date < D}
    env.http.csv[("real", "202610")] = make_csv(REAL_HEADER, lagging)
    assert run(env, "us.infl.be") == "partial"  # 名目到 D、實質還沒
    last = be_rows(env)[-1]
    assert (last["date"], last["n5"], last["r5"]) == ("2026-10-22", 4.78, None)
    values = market_calc.breakeven_values(be_rows(env))
    assert values["y5"]["date"] == "2026-10-21" and values["y10"]["date"] == "2026-10-21"  # 同日才算：停在 10/21
    state = market_pipeline_us._state(ctx_at(env, NOW), pipe.BE_KEY)
    assert state["done"] == ["nominal"] and state["wait"]["real"] == [1, NOW + 3600.0]
    assert run(env, "us.infl.be", NOW + 3599) == "not_due"
    env.http.csv[("real", "202610")] = REAL_202610  # 財政部補上了
    assert run(env, "us.infl.be", NOW + 3600) == "ok"
    assert market_calc.breakeven_values(be_rows(env))["y5"]["date"] == "2026-10-22"


def test_a_bond_holiday_has_no_row_so_d_stays_not_yet_four_times_then_stops(env):
    """10/12 哥倫布日：股市開（D＝10/12）、債市休，兩個檔都沒有那一天；晚於 D 的列（10/13 起）一律不收。"""
    feed_files(env)
    now = at(2026, 10, 12, 17, 0)
    assert market_pipeline_us.current_d(ctx_at(env, now)) == date(2026, 10, 12)
    assert run(env, "us.infl.be", now) == "not_yet"
    assert be_rows(env)[-1]["date"] == "2026-10-09" and "2026-10-13" not in {row["date"] for row in be_rows(env)}
    for hours in (1, 2, 3):
        assert run(env, "us.infl.be", now + hours * 3600) == "not_yet"
    assert run(env, "us.infl.be", now + 4 * 3600) == "not_due"  # 每小時一次、最多 4 次，之後這個 D 不再問
    assert env.store.task_state("us.infl.be")["fail_streak"] == 0
    values = market_calc.breakeven_values(be_rows(env))
    assert values["y10"]["date"] == "2026-10-09"  # 資料停在 10/09，卡上的日期照實寫


def test_a_second_holiday_the_day_before_veterans_day_is_the_same_story(env):
    """11/11 退伍軍人節：同樣兩個檔都沒有那天（合成檔）。"""
    days = [date(2026, 11, n) for n in (2, 3, 4, 5, 6, 9, 10)]
    env.http.csv[("nominal", "202611")] = make_csv(NOMINAL_HEADER, {d: (4.5, 4.7) for d in days})
    env.http.csv[("real", "202611")] = make_csv(REAL_HEADER, {d: (2.2, 2.3) for d in days})
    env.http.csv[("nominal", "202610")], env.http.csv[("real", "202610")] = NOMINAL_202610, REAL_202610
    now = at(2026, 11, 11, 17, 0)
    assert market_pipeline_us.current_d(ctx_at(env, now)) == date(2026, 11, 11)
    assert run(env, "us.infl.be", now) == "not_yet"
    assert be_rows(env)[-1]["date"] == "2026-11-10"


@pytest.mark.parametrize("current_file", ["header_only", "not_created_yet"])
def test_month_start_file_without_rows_falls_back_to_the_previous_month(env, current_file):
    feed_files(env)
    assert run(env, "us.infl.be") == "ok"  # 10/22 之前的資料已存好
    first = env.store.get_obs(pipe.OBS_INFL_BE)
    now = at(2026, 11, 2, 17, 0)  # D＝11/02（週一），新月份的檔還沒有列
    if current_file == "header_only":
        env.http.csv[("nominal", "202611")], env.http.csv[("real", "202611")] = NOMINAL_HEADER + "\n", REAL_HEADER + "\n"
    env.http.calls.clear()
    assert market_pipeline_us.current_d(ctx_at(env, now)) == date(2026, 11, 2)
    assert run(env, "us.infl.be", now) == "not_yet"
    # 最新一列還在上個月 → 上個月也抓回來接起來（兩個檔各一次）
    assert env.http.calls == [("nominal", "202611"), ("nominal", "202610"), ("real", "202611"), ("real", "202610")]
    after = env.store.get_obs(pipe.OBS_INFL_BE)
    assert after["payload"] == first["payload"] and after["last_success_at"] == first["last_success_at"]
    assert after["fail_streak"] == 0


def test_a_non_200_in_the_middle_of_the_month_is_a_real_failure(env):
    feed_files(env)
    now = at(2026, 11, 16, 17, 0)  # D＝11/16：不是月初，新月份的檔沒有就是來源壞了
    env.http.csv[("nominal", "202610")], env.http.csv[("real", "202610")] = NOMINAL_202610, REAL_202610
    assert run(env, "us.infl.be", now) == "status"
    assert env.store.task_state("us.infl.be")["fail_streak"] == 1
    assert env.store.get_obs(pipe.OBS_INFL_BE)["error"] == "status"


def test_cold_start_at_the_start_of_a_month_builds_history_from_the_previous_month(env):
    feed_files(env)
    env.http.csv[("nominal", "202611")], env.http.csv[("real", "202611")] = NOMINAL_HEADER + "\n", REAL_HEADER + "\n"
    assert run(env, "us.infl.be", at(2026, 11, 2, 17, 0)) == "not_yet"
    rows = be_rows(env)
    assert len(rows) == 15 and rows[-1]["date"] == "2026-10-22"  # 新月份只有表頭 → 上個月（10 月）整份補上
    values = market_calc.breakeven_values(rows)
    assert values["y10"]["chg5_bp"] == 0 and values["y5"]["chg5_bp"] == 12  # 有上個月的列，5 日變化算得出來


def test_unchanged_files_do_not_rewrite_the_obs(env):
    feed_files(env)
    assert run(env, "us.infl.be") == "ok"
    first = env.store.get_obs(pipe.OBS_INFL_BE)
    later = at(2026, 10, 23, 17, 0)  # D＝10/23，財政部的檔還沒有那天
    env.clock.wall = later
    assert run(env, "us.infl.be", later) == "not_yet"
    after = env.store.get_obs(pipe.OBS_INFL_BE)
    assert after["payload"] == first["payload"] and after["last_success_at"] == first["last_success_at"]
    assert after["checked_at"] == later and after["fail_streak"] == 0


def test_only_the_latest_forty_rows_are_kept(env):
    old = [date(2026, 7, 1) + timedelta(days=n) for n in range(60) if (date(2026, 7, 1) + timedelta(days=n)).weekday() < 5][:40]
    env.store.put_obs(pipe.OBS_INFL_BE, {"rows": [
        {"date": d.isoformat(), "n5": 4.0, "n10": 4.2, "r5": 2.0, "r10": 2.1} for d in old]}, data_date=old[-1].isoformat())
    feed_files(env)
    assert run(env, "us.infl.be") == "ok"
    rows = be_rows(env)
    assert len(rows) == 40 and rows[-1]["date"] == "2026-10-22" and rows[0]["date"] > old[0].isoformat()


def test_a_conflicting_duplicate_date_is_a_parse_failure_on_that_item_only(env):
    feed_files(env)
    duplicate = NOMINAL_202610.splitlines()[1].replace("4.78", "4.77")
    env.http.csv[("nominal", "202610")] = NOMINAL_202610 + "\n" + duplicate
    assert run(env, "us.infl.be") == "partial"
    state = market_pipeline_us._state(ctx_at(env, NOW), pipe.BE_KEY)
    assert state["done"] == ["real"] and state["fails"] == {"nominal": 1}
    assert be_rows(env)[-1]["r5"] == 2.33 and be_rows(env)[-1]["n5"] is None  # real 照常存，nominal 的 10 月那份沒進來


def test_a_cell_the_source_has_not_published_does_not_blank_a_stored_value(env):
    feed_files(env)
    assert run(env, "us.infl.be") == "ok"
    later = at(2026, 10, 23, 17, 0)
    gappy = {p.date: (p.y5, p.y10) for p in src.parse_treasury_yields(NOMINAL_202610, "nominal")}
    gappy[date(2026, 10, 22)] = (None, None)  # 來源這天兩個天期都變空白
    gappy[date(2026, 10, 23)] = (4.80, 4.99)
    env.http.csv[("nominal", "202610")] = make_csv(NOMINAL_HEADER, gappy)
    assert run(env, "us.infl.be", later) == "partial"  # 名目到 D＝10/23、實質還沒有
    rows = {row["date"]: row for row in be_rows(env)}
    assert rows["2026-10-22"]["n5"] == 4.78 and rows["2026-10-23"]["n5"] == 4.80 and rows["2026-10-23"]["r5"] is None


def test_no_d_means_no_task_run(env):
    ctx = ctx_at(env, at(2028, 1, 3, 18, 0))
    assert market_pipeline_us.current_d(ctx) is None
    assert TASKS["us.infl.be"].due(ctx) is False and TASKS["us.infl.be"].run(ctx) is None
    assert env.http.calls == []


def test_should_stop_before_any_request_is_not_a_success_or_a_failure(env):
    feed_files(env)
    ctx = ctx_at(env, NOW)
    ctx.task_deadline = env.clock() - 1.0
    with pytest.raises(market_http.FetchError) as caught:
        pipe._run_be(ctx)
    assert caught.value.kind == "budget" and env.http.calls == []


def test_the_real_ledger_spaces_the_requests_and_counts_them(env):
    feed_files(env)
    ledger = env.store.ledger(now_fn=lambda: env.clock.wall, sleep_fn=env.clock.sleep)
    env.ledger = env.http.ledger = ledger
    assert run(env, "us.infl.be") == "ok"
    assert env.store.fetch_counts(now=env.clock.wall)["home.treasury.gov"]["count"] == 4


def test_collected_rows_publish_into_a_valid_snapshot(env):
    """接縫：collect 存的 obs（具名鍵的列）→ publish 的計算 → 快照列，整份快照過 validate_snapshot。"""
    import market_publish

    (env.paths.state_dir.parent / "market_seasonality.json").write_bytes(
        (Path(__file__).resolve().parent / "market_seasonality.json").read_bytes())
    feed_files(env)
    assert run(env, "us.infl.be") == "ok"
    outcome = market_publish.publish(env.store, env.paths, now=NOW + 60, charts=object())
    assert outcome["kind"] == "ok" and "row_errors" not in outcome and "shared_errors" not in outcome, outcome
    snapshot = json.loads(env.paths.snapshot.read_bytes().decode("utf-8"))
    row = snapshot["rows"]["us.infl.be"]
    assert row["state"] == "ok" and row["data_date"] == "2026-10-22"
    assert row["values"]["y10"] == {"date": "2026-10-22", "value": 2.32, "chg5_bp": 0, "move": "flat"}
    assert row["values"]["y5"]["value"] == 2.45 and row["values"]["spread"] == "five_higher"
    assert market_publish.validate_snapshot(snapshot) == []


# ═══ CPI（BLS）：先問一個序列，月份前進才抓另外三個 ═══════════════════════════════════════════════════

CPI_TASK = "us.infl.cpi"
PROBE = "CUUR0000SA0"
BLS_BODY_LIMIT = 64 * 1024


def saved_values(series_id: str, through: str = "2026-09") -> dict[str, str]:
    """保存的真實值 2024-01…2026-09（2025-10 是 "-"）；through 比 2026-09 早就截掉，晚就接合成的月份（每月 +0.2%，只給輪詢測試用）。"""
    values = {f"{year}-{index:02d}": value for year, items in BLS_YEARS[series_id].items()
              for index, value in enumerate(items, start=1)}
    for month in sorted(values):
        if month > through:
            del values[month]
    month = max(values)
    while month < through:
        month = market_calc.month_shift(month, 1)
        previous = values[market_calc.month_shift(month, -1)]
        values[month] = str((Decimal(previous) * Decimal("1.002")).quantize(Decimal("0.001")))
    return values


def bls_response(series_id: str, values: dict[str, str], **overrides) -> dict:
    data = [{"year": ym[:4], "period": f"M{ym[5:]}", "periodName": "x", "value": values[ym], "footnotes": [{}]}
            for ym in sorted(values, reverse=True)]
    body = {"status": "REQUEST_SUCCEEDED", "responseTime": 50, "message": [],
            "Results": {"series": [{"seriesID": series_id, "data": data}]}}
    body.update(overrides)
    return body


def seed_cpi(env: Env, through: str = "2026-08", retrieved: str = "2026-09-14") -> None:
    """已存的四個序列（保存的真實值）到 through 這個月。"""
    series = {sid: {ym: float(value) for ym, value in saved_values(sid, through).items() if value != "-"}
              for sid in market_calc.CPI_SERIES}
    env.store.put_obs(pipe.OBS_INFL_CPI, {"series": series, "retrieved": retrieved},
                      data_date=market_pipeline_us._month_end(through).isoformat(), period=through)


def stored_months(env: Env) -> dict[str, str]:
    series = env.store.get_obs(pipe.OBS_INFL_CPI)["payload"]["series"]
    return {sid: max(points) for sid, points in series.items()}


def cpi_state(env: Env, now: float = NOW) -> dict:
    return market_pipeline_us._state(ctx_at(env, now), pipe.CPI_KEY)


def test_the_cpi_task_is_registered_in_the_core_group_after_us_ust():
    names = [task.name for task in market_tasks.registered()]
    assert names.index("us.ust") < names.index("us.infl.be") < names.index(CPI_TASK)
    task = TASKS[CPI_TASK]
    assert task.group == "core" and task.market == "us" and task.budget_s == 35.0  # 和 us.ust 一樣；4 個請求隔 5 秒＝15 秒


def test_the_host_is_on_the_whitelist_with_a_small_daily_budget():
    """2026-10-24 Andrew 同意新增 api.bls.gov（v1 免註冊、每天 25 次；日界線沒寫，所以台北日 12 次＝一半）。"""
    assert market_calc.CPI_HOST in market_http.ALLOWED_HOSTS
    assert market_store.HOST_RULES["api.bls.gov"] == market_store.HostRule(5.0, 12)
    assert src.ENDPOINTS["bls_cpi"].host in market_http.ALLOWED_HOSTS
    assert market_http.host_of(src.BLS_URL.format(series=PROBE)) == "api.bls.gov"


def test_the_publisher_reads_the_same_cpi_obs_key():
    import market_publish

    assert market_publish.OBS_US_INFL_CPI == pipe.OBS_INFL_CPI


def test_cold_start_probes_first_then_fetches_the_other_three_and_stores_one_set(env):
    now = at(2026, 10, 24, 10, 0)  # 週六：不在輪詢日，冷啟動照問
    assert run(env, CPI_TASK, now) == "ok"
    assert env.http.bls_calls == list(market_calc.CPI_SERIES)  # 先 CUUR0000SA0，再另外三個，各一次、全部 GET
    assert env.ledger.reserved == ["api.bls.gov"] * 4
    got = env.store.get_obs(pipe.OBS_INFL_CPI)
    assert got["data_date"] == "2026-09-30" and got["period"] == "2026-09"
    payload = got["payload"]
    assert set(payload) == {"series", "retrieved"} and payload["retrieved"] == "2026-10-24"  # 台北日期（週六 22:00）
    for sid in market_calc.CPI_SERIES:
        points = payload["series"][sid]
        assert len(points) == 32 and "2025-10" not in points and "2025-13" not in points
        assert points["2026-09"] == float(saved_values(sid)["2026-09"])
    values = market_calc.cpi_values(payload["series"], date.fromisoformat(payload["retrieved"]))  # collect 存的接得上 publish 的計算
    assert values["cpi"]["yoy"] == 3.7 and values["core"]["yoy"] == 2.6 and values["aligned"] is True
    assert cpi_state(env, now).get("last_probe") is None
    assert run(env, CPI_TASK, now + 900) == "not_due"  # 已有期待的月份


def test_the_retrieval_date_is_the_taipei_date_of_the_fetch(env):
    assert run(env, CPI_TASK, at(2026, 10, 24, 14, 0)) == "ok"  # 美東週六 14:00＝台北週日 02:00
    assert env.store.get_obs(pipe.OBS_INFL_CPI)["payload"]["retrieved"] == "2026-10-25"


def test_with_the_host_off_the_whitelist_nothing_is_requested(env, monkeypatch):
    """一個開關：把 api.bls.gov 從 market_http.ALLOWED_HOSTS 拿掉，due 一律 False、run 也不送請求、帳本也不預扣。"""
    monkeypatch.setattr(market_http, "ALLOWED_HOSTS", frozenset(market_http.ALLOWED_HOSTS - {"api.bls.gov"}))
    assert pipe.bls_enabled() is False
    ctx = ctx_at(env, at(2026, 10, 14, 9, 0))
    assert TASKS[CPI_TASK].due(ctx) is False and TASKS[CPI_TASK].run(ctx) is None
    assert run(env, CPI_TASK, at(2026, 10, 14, 9, 0)) == "not_due"
    assert env.http.bls_calls == [] and env.http.calls == [] and env.ledger.reserved == []
    seed_cpi(env, "2026-08")  # 有舊資料、也在窗口裡：還是不問
    assert run(env, CPI_TASK, at(2026, 10, 14, 9, 0)) == "not_due" and env.ledger.reserved == []
    from jobs import market_snapshot as job

    assert job._infl_status(env.store, at(2026, 10, 14, 9, 0))["infl_bls_poll"] == "disabled"
    monkeypatch.undo()
    assert pipe.bls_enabled() is True and run(env, CPI_TASK, at(2026, 10, 14, 9, 0)) in ("ok", "not_yet")  # 開關打開就跑


def test_inside_the_window_it_asks_with_one_request_until_the_month_advances(env):
    seed_cpi(env, "2026-08")
    env.http.bls_through = "2026-08"  # BLS 還沒公布 9 月
    assert run(env, CPI_TASK, at(2026, 10, 7, 10, 0)) == "not_due"  # 7 號：期待的是 8 月，已經有了
    assert run(env, CPI_TASK, at(2026, 10, 14, 8, 29)) == "not_due"  # 公布時刻是 08:30（美東）
    first = at(2026, 10, 14, 8, 30)
    assert run(env, CPI_TASK, first) == "not_yet"
    assert env.http.bls_calls == [PROBE]  # 沒進展：只問一個序列
    assert cpi_state(env, first)["last_probe"] == first
    assert env.store.get_obs(pipe.OBS_INFL_CPI)["period"] == "2026-08"  # 沒變
    assert run(env, CPI_TASK, first + 7199) == "not_due"
    env.http.bls_through = "2026-09"  # 公布了
    env.http.bls_calls.clear()
    assert run(env, CPI_TASK, first + 7200) == "ok"
    assert env.http.bls_calls == list(market_calc.CPI_SERIES)  # 月份前進才抓另外三個，同一輪
    assert set(stored_months(env).values()) == {"2026-09"} and cpi_state(env)["last_probe"] is None
    assert run(env, CPI_TASK, first + 7200 + 900) == "not_due"


def test_a_release_three_days_late_is_never_locked_out(env):
    """審查 r9 s3-F2：寫死公布日＋max_not_yet 的做法，延後公布時會在 8 次之後把期間當做完。這裡只看時間：連問三天都沒有，
    第四天公布了就在那一輪補上。"""
    seed_cpi(env, "2026-08")
    env.http.bls_through = "2026-08"
    results = []
    for day in (14, 15, 16):  # 週三、四、五
        for hour in (8, 10, 12, 14, 16, 18, 20):
            results.append(run(env, CPI_TASK, at(2026, 10, day, hour, 30)))
    assert results.count("not_yet") == 18 and results.count("not_due") == 3  # 每天 08:30、10:30…18:30 六次，20:30 已過時段
    assert len(env.http.bls_calls) == 18 and set(env.http.bls_calls) == {PROBE}
    env.http.bls_through = "2026-09"
    assert run(env, CPI_TASK, at(2026, 10, 19, 8, 30)) == "ok"  # 週一：延後 5 天才公布，還是補得上
    assert set(stored_months(env).values()) == {"2026-09"}


def test_after_the_window_a_late_release_is_still_caught_within_a_day(env):
    seed_cpi(env, "2026-08")
    env.http.bls_through = "2026-08"
    assert run(env, CPI_TASK, at(2026, 10, 22, 9, 0)) == "not_yet"  # 22 號：窗口過了，平日還是每天問一次
    assert run(env, CPI_TASK, at(2026, 10, 22, 11, 0)) == "not_due" and run(env, CPI_TASK, at(2026, 10, 23, 8, 59)) == "not_due"
    env.http.bls_through = "2026-09"
    assert run(env, CPI_TASK, at(2026, 10, 23, 9, 0)) == "ok"  # 剛好隔一天
    assert set(stored_months(env).values()) == {"2026-09"}


def test_more_than_a_month_behind_catches_up_at_once(env):
    seed_cpi(env, "2026-07")
    now = at(2026, 10, 25, 2, 0)  # 週日凌晨：不在窗口、不在時段
    assert run(env, CPI_TASK, now) == "ok"
    assert set(stored_months(env).values()) == {"2026-09"}


@pytest.mark.parametrize("window_start, release, old, new", [
    ((2026, 10, 8), (2026, 10, 14), "2026-08", "2026-09"),  # 夏令：08:30 美東＝台北 20:30
    ((2026, 11, 8), (2026, 11, 10), "2026-09", "2026-10"),  # 冬令（11/1 換）：08:30 美東＝台北 21:30
])
def test_a_window_week_stays_inside_the_host_cap_per_taipei_day_and_the_follow_ups_land_in_the_release_round(
        env, window_start, release, old, new):
    """真的帳本（5 秒間隔、每台北日 12 次），每 15 分鐘一輪從窗口第一天跑到公布後三天：不論哪一個台北日都不超過 12 次，
    公布那一輪探針和另外三個序列一起抓完（不拖到隔天），之後不再問。"""
    ledger = env.store.ledger(now_fn=lambda: env.clock.wall, sleep_fn=env.clock.sleep)
    env.ledger = env.http.ledger = ledger
    seed_cpi(env, old)
    release_at = at(*release, 8, 30)
    tick = at(*window_start, 0, 0)
    end = at(*release, 0, 0) + 4 * 86400
    log: list[tuple[float, str]] = []  # (輪的時間, 序列)
    done_at = None
    while tick < end:
        env.http.bls_through = new if tick >= release_at else old
        before = len(env.http.bls_calls)
        result = run(env, CPI_TASK, tick)
        assert result in ("ok", "not_yet", "not_due"), (datetime.fromtimestamp(tick, ET), result)  # 沒有 budget／rate_limited
        log += [(tick, series_id) for series_id in env.http.bls_calls[before:]]
        if result == "ok" and done_at is None:
            done_at = tick
        tick += 900
    per_taipei_day: dict[date, int] = {}
    for stamp, _ in log:
        day = datetime.fromtimestamp(stamp, TPE).date()
        per_taipei_day[day] = per_taipei_day.get(day, 0) + 1
    assert done_at == release_at  # 公布後的第一個輪詢時間就抓到
    assert [series_id for stamp, series_id in log if stamp == done_at] == list(market_calc.CPI_SERIES)  # 探針＋另外三個，同一輪
    assert all(stamp <= done_at for stamp, _ in log)  # 抓到之後不再問
    assert max(per_taipei_day.values()) <= 10, per_taipei_day  # 12 次的上限，公布日至少留 2 次給失敗重試
    assert set(stored_months(env).values()) == {new}


def test_request_not_processed_cools_the_host_and_backs_the_task_off_without_a_lock(env):
    seed_cpi(env, "2026-08")
    env.http.bls[PROBE] = {"status": "REQUEST_NOT_PROCESSED", "responseTime": 5, "Results": {},
                           "message": ["Request could not be serviced, as the daily threshold ... has been reached."]}
    first = at(2026, 10, 14, 8, 30)
    assert run(env, CPI_TASK, first) == "rate_limited"
    assert env.ledger.cooling == {"api.bls.gov": 7200.0}  # 和真的 429 一樣冷卻 2 小時
    state = env.store.task_state(CPI_TASK)
    assert state["fail_streak"] == 1 and state["next_try"] == first + 900  # 任務退避 15 分鐘
    assert env.store.get_obs(pipe.OBS_INFL_CPI)["error"] == "rate_limited" and cpi_state(env, first).get("last_probe") is None
    assert run(env, CPI_TASK, first + 899) == "backoff"
    env.ledger.cooling.clear()
    env.http.bls.clear()
    assert run(env, CPI_TASK, first + 900) == "ok"  # 沒有 max_fails 把這個月鎖死
    assert env.store.task_state(CPI_TASK)["fail_streak"] == 0


@pytest.mark.parametrize("body", [
    {"status": "REQUEST_SUCCEEDED", "responseTime": 5, "message": [], "Results": {}},
    {"status": "REQUEST_SUCCEEDED", "responseTime": 5, "message": [], "Results": {"series": []}},
])
def test_an_empty_results_object_is_handled_like_the_limit(env, body):
    seed_cpi(env, "2026-08")
    env.http.bls[PROBE] = body
    assert run(env, CPI_TASK, at(2026, 10, 14, 8, 30)) == "rate_limited"
    assert "api.bls.gov" in env.ledger.cooling and market_pipeline_us._state(ctx_at(env, NOW), pipe.CPI_KEY).get("fails") is None


def test_a_real_429_goes_through_the_transport_cooldown_only(env):
    seed_cpi(env, "2026-08")
    env.http.bls_fail[PROBE] = "rate_limited"  # market_http.fetch 自己冷卻主機並丟 FetchError("rate_limited")
    first = at(2026, 10, 14, 8, 30)
    assert run(env, CPI_TASK, first) == "rate_limited"
    assert env.ledger.cooling == {"api.bls.gov": 7200.0} and env.http.bls_calls == [PROBE]


def test_a_blocked_host_is_not_retried_inside_the_cooldown(env):
    seed_cpi(env, "2026-08")
    env.http.bls_fail[PROBE] = "blocked"
    first = at(2026, 10, 14, 8, 30)
    assert run(env, CPI_TASK, first) == "blocked"
    env.http.bls_fail.clear()
    assert run(env, CPI_TASK, first + 900) == "cooldown"  # 帳本說還在冷卻：沒有送出請求
    assert env.http.bls_calls == [PROBE]


def test_a_failure_in_a_follow_up_series_writes_nothing_and_retries_the_whole_set(env):
    seed_cpi(env, "2026-08")
    before = env.store.get_obs(pipe.OBS_INFL_CPI)
    env.http.bls_fail["CUUR0000SA0L1E"] = "timeout"
    first = at(2026, 10, 14, 8, 30)
    assert run(env, CPI_TASK, first) == "timeout"
    assert env.http.bls_calls == [PROBE, "CUSR0000SA0", "CUUR0000SA0L1E"]  # 第三個失敗就停
    after = env.store.get_obs(pipe.OBS_INFL_CPI)
    assert after["payload"] == before["payload"] and after["period"] == "2026-08"  # 不寫半組
    env.http.bls_fail.clear()
    env.http.bls_calls.clear()
    assert run(env, CPI_TASK, first + 900) == "ok"
    assert env.http.bls_calls == list(market_calc.CPI_SERIES) and set(stored_months(env).values()) == {"2026-09"}


def test_a_set_whose_series_disagree_is_kept_but_not_done_until_they_line_up(env):
    seed_cpi(env, "2026-08")
    env.http.bls_months = {"CUSR0000SA0": "2026-08"}  # 季調全項還沒到 9 月
    first = at(2026, 10, 14, 8, 30)
    assert run(env, CPI_TASK, first) == "not_yet"
    months = stored_months(env)
    assert months[PROBE] == "2026-09" and months["CUSR0000SA0"] == "2026-08"
    got = env.store.get_obs(pipe.OBS_INFL_CPI)
    assert got["period"] == "2026-08" and got["data_date"] == "2026-08-31"  # 整組只算到最舊的那個
    values = market_calc.cpi_values(got["payload"]["series"], date(2026, 10, 14))
    assert values["aligned"] is False and values["cpi"]["momentum"] is None and values["gap"] is None
    assert cpi_state(env, first)["last_probe"] == first
    env.http.bls_months.clear()
    env.http.bls_calls.clear()
    assert run(env, CPI_TASK, first + 7200) == "ok"
    assert env.http.bls_calls == [PROBE, "CUSR0000SA0"]  # 探針沒進展，但已存的季調落後探針 → 只補抓落後的那一個
    assert set(stored_months(env).values()) == {"2026-09"}


def test_older_data_than_the_stored_set_is_a_conflict_and_changes_nothing(env):
    seed_cpi(env, "2026-09")
    before = env.store.get_obs(pipe.OBS_INFL_CPI)["payload"]
    env.http.bls_months = {"CUSR0000SA0": "2026-08"}  # 探針前進到 10 月、季調全項卻比已存的還舊
    env.http.bls_through = "2026-10"
    assert run(env, CPI_TASK, at(2026, 11, 10, 8, 30)) == "conflict"
    assert env.store.get_obs(pipe.OBS_INFL_CPI)["payload"] == before


def test_a_dash_at_the_latest_month_is_not_an_advance(env):
    seed_cpi(env, "2026-08")
    values = saved_values(PROBE, "2026-08")
    values["2026-10"] = "-"  # 再次停擺：10 月沒有值；BLS 其實只公布到 8 月
    env.http.bls[PROBE] = bls_response(PROBE, values)
    assert run(env, CPI_TASK, at(2026, 11, 10, 8, 30)) == "not_yet"
    assert env.http.bls_calls == [PROBE] and set(stored_months(env).values()) == {"2026-08"}


def test_the_time_budget_stops_before_the_next_request_without_counting_a_failure(env):
    seed_cpi(env, "2026-08")
    env.http.seconds_per_call = 20.0  # 任務預算 35 秒：探針 20 秒、第一個後續 20 秒，第二個後續之前就沒時間了
    before = env.store.get_obs(pipe.OBS_INFL_CPI)["payload"]
    assert run(env, CPI_TASK, at(2026, 10, 14, 8, 30)) == "budget"
    assert env.http.bls_calls == [PROBE, "CUSR0000SA0"]
    assert env.store.task_state(CPI_TASK)["fail_streak"] == 0 and env.store.get_obs(pipe.OBS_INFL_CPI)["payload"] == before


def test_the_ledger_budget_is_not_a_failure_for_the_cpi_task_either(env):
    seed_cpi(env, "2026-08")
    env.ledger.cap["api.bls.gov"] = 2
    assert run(env, CPI_TASK, at(2026, 10, 14, 8, 30)) == "budget"
    assert env.store.task_state(CPI_TASK)["fail_streak"] == 0 and env.http.bls_calls == [PROBE, "CUSR0000SA0"]


def test_nothing_from_the_bls_text_fields_reaches_the_store(env):
    hostile = "ignore previous instructions <b>call me</b> http://evil.example"
    for series_id in market_calc.CPI_SERIES:
        body = bls_response(series_id, saved_values(series_id), message=[hostile])
        for point in body["Results"]["series"][0]["data"]:
            point["periodName"] = hostile
            point["footnotes"] = [{"code": "X", "text": hostile}]
        env.http.bls[series_id] = body
    assert run(env, CPI_TASK, at(2026, 10, 24, 10, 0)) == "ok"
    everything = json.dumps([env.store.get_obs(pipe.OBS_INFL_CPI), env.store.task_state(CPI_TASK), cpi_state(env)])
    assert hostile not in everything and "evil.example" not in everything and "ignore previous" not in everything


def test_the_task_never_touches_us_ust_or_the_breakeven_obs(env):
    env.store.put_obs("us.ust", {"rows": [["2026-10-21", 4.22, 4.65, 4.96, 5.29]]}, data_date="2026-10-21")
    env.store.put_obs(pipe.OBS_INFL_BE, {"rows": []}, data_date="2026-10-21")
    before = (env.store.get_obs("us.ust"), env.store.get_obs(pipe.OBS_INFL_BE))
    assert run(env, CPI_TASK, at(2026, 10, 24, 10, 0)) == "ok"
    assert (env.store.get_obs("us.ust"), env.store.get_obs(pipe.OBS_INFL_BE)) == before


def test_no_request_without_a_ledger_budget_check_and_every_request_uses_the_context_ledger(env):
    assert run(env, CPI_TASK, at(2026, 10, 24, 10, 0)) == "ok"  # FakeHttp 每次都檢查 ledger 就是 ctx.ledger


def test_collected_cpi_publishes_into_a_valid_snapshot(env):
    """接縫：collect 存的 obs → publish 的計算 → 快照列（取得日、月份、列舉碼）→ 整份快照過 validate_snapshot。"""
    import market_publish

    (env.paths.state_dir.parent / "market_seasonality.json").write_bytes(
        (Path(__file__).resolve().parent / "market_seasonality.json").read_bytes())
    assert run(env, CPI_TASK, at(2026, 10, 24, 10, 0)) == "ok"
    outcome = market_publish.publish(env.store, env.paths, now=at(2026, 10, 24, 10, 5), charts=object())
    assert outcome["kind"] == "ok" and "row_errors" not in outcome and "shared_errors" not in outcome, outcome
    snapshot = json.loads(env.paths.snapshot.read_bytes().decode("utf-8"))
    row = snapshot["rows"]["us.infl.cpi"]
    assert row["state"] == "ok" and row["data_date"] == "2026-09-30" and row["period"] == "2026-09" and row["source"] == "BLS"
    values = row["values"]
    assert values["retrieved"] == "2026-10-24" and values["cpi"]["yoy"] == 3.7 and values["cpi"]["level"] == "well_above"
    assert values["core"]["yoy"] == 2.6 and values["gap"] == "headline_higher" and values["aligned"] is True
    assert market_publish.validate_snapshot(snapshot) == []


# ── outcome：每日維護看得到 ────────────────────────────────────────────────────


def test_outcome_reports_the_stored_month_the_lag_and_the_poll_state(env, monkeypatch):
    from jobs import market_snapshot as job

    def status(now):
        out = job._infl_status(env.store, now)
        assert job.sanitize(out) == out and set(out) == {"infl_cpi_month", "infl_cpi_lag_months", "infl_bls_poll"}
        return out

    assert status(at(2026, 10, 24, 10, 0)) == {"infl_cpi_month": None, "infl_cpi_lag_months": None, "infl_bls_poll": "pending"}
    seed_cpi(env, "2026-09")
    assert status(at(2026, 10, 24, 10, 0)) == {"infl_cpi_month": "2026-09", "infl_cpi_lag_months": 0, "infl_bls_poll": "done"}
    assert status(at(2026, 11, 7, 10, 0))["infl_bls_poll"] == "done"  # 11/7：期待的還是 9 月
    assert status(at(2026, 11, 10, 7, 0)) == {"infl_cpi_month": "2026-09", "infl_cpi_lag_months": 1, "infl_bls_poll": "pending"}
    assert status(at(2026, 11, 22, 10, 0)) == {"infl_cpi_month": "2026-09", "infl_cpi_lag_months": 1, "infl_bls_poll": "exhausted"}
    assert status(at(2026, 12, 22, 10, 0)) == {"infl_cpi_month": "2026-09", "infl_cpi_lag_months": 2, "infl_bls_poll": "pending"}
    monkeypatch.setattr(market_http, "ALLOWED_HOSTS", frozenset(market_http.ALLOWED_HOSTS - {"api.bls.gov"}))
    assert status(at(2026, 11, 10, 7, 0))["infl_bls_poll"] == "disabled"


def test_outcome_status_survives_a_damaged_obs(env):
    from jobs import market_snapshot as job

    env.store.put_obs(pipe.OBS_INFL_CPI, {"series": "oops", "retrieved": 5}, data_date="2026-09-30")
    env.store.set_task_state(market_pipeline_us.STATE_PREFIX + pipe.CPI_KEY, {"last_probe": "yesterday"})
    assert job._infl_status(env.store, at(2026, 10, 24, 10, 0)) == {
        "infl_cpi_month": None, "infl_cpi_lag_months": None, "infl_bls_poll": "pending"}


def test_the_job_outcome_carries_the_inflation_status(env):
    """整個 compose_outcome（監督程序寫 outcome 的那一步）帶 infl_*，而且全是列舉值。"""
    import time

    from jobs import market_snapshot as job

    now = at(2026, 10, 24, 10, 0)
    seed_cpi(env, "2026-09")
    supervisor = job.Supervisor(env.paths, job.parse_args(["--root", str(env.paths.state_dir.parent)]), -1, wall=lambda: now)
    outcome = supervisor.compose_outcome(code="unchecked", collect="ok", publish="ok", started_at=now, locked_at=time.monotonic())
    assert (outcome["infl_cpi_month"], outcome["infl_cpi_lag_months"], outcome["infl_bls_poll"]) == ("2026-09", 0, "done")
    assert job.sanitize(outcome) == outcome and "infl_status_error" not in outcome
