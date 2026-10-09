"""FCN 評估（fcn_eval.py）的單元測試（2026-10-09）。

不連網：autouse fixture 把 fcn_eval._http_get_json 與 requests.get 換成一呼叫就記下來並
raise 的替身，測試結束時檢查沒有人呼叫過；需要資料的測試只換 _fetch_chart／
_fetch_net_income／_fetch_usd_rf（合成的 Yahoo chart JSON：固定種子的 GBM 價格、約 260 個
交易日、最後一天在這幾天內）。卡住／逾時的測試換新的 executor 與 semaphore，替身等 teardown
會 set 的 Event，不會讓 pytest 結束不了。

路由（main._handle_event、_reply(flex_card=…)）的測試在 test_fcn_routing.py。
"""

from __future__ import annotations

import itertools
import json
import math
import os
import re
import signal
import threading
import time
import tracemalloc
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import fcn_eval  # noqa: E402
import main  # noqa: E402
import output_validator  # noqa: E402
import stock_quote  # noqa: E402
from linebot.v3.messaging import FlexContainer  # noqa: E402

# LINE 限制：altText 1500 字、單張 bubble JSON 30 KB、action label 20 字、message text 300 字。
ALT_TEXT_MAX = 1500
BUBBLE_MAX_BYTES = 30 * 1024
LABEL_MAX = 20
ACTION_TEXT_MAX = 300

_TPE = ZoneInfo("Asia/Taipei")
_PREFIX = "FCN 評估"

# 替身換掉之前的原函式（測試解析邏輯本身時用）。
_REAL_HTTP_GET_JSON = fcn_eval._http_get_json
_REAL_FETCH_CHART = fcn_eval._fetch_chart
_REAL_FETCH_NET_INCOME = fcn_eval._fetch_net_income
_REAL_FETCH_USD_RF = fcn_eval._fetch_usd_rf
_REAL_SIMULATE = fcn_eval.simulate

BANKER_MESSAGE = (
    "【FCN 報價】連結標的：NVIDIA (NVDA)、AMD、特斯拉 TSLA\n"
    "交易日 2026/10/09，to-date 2027/4/22 10:00 AM 前回覆\n"
    "Tenor 6M｜記憶式KO 100%｜KI 60%｜執行價 80%\n"
    "年化 17.89% p.a.（每月1.4908%）\n"
    "最低申購 USD 50,000\n"
    "備註：P.A. 為年化；ADR、AI 概念股\n"
    "理專 ℡ 02-2345-6789 分機 1234"
)


# ── 共用工具 ────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """fcn_eval 的任何真實 HTTP 呼叫都會失敗並被記下；測試結束時必須是空的。"""
    attempts: list[str] = []

    def _blocked(*args, **_kwargs):
        attempts.append(str(args[:1]))
        raise AssertionError("test_fcn_eval must not touch the network")

    monkeypatch.setattr(fcn_eval, "_http_get_json", _blocked)
    monkeypatch.setattr(fcn_eval.requests, "get", _blocked)
    fcn_eval.clear_caches()
    yield attempts
    fcn_eval.clear_caches()
    assert attempts == [], f"network was attempted: {attempts}"


def _business_days(n: int, end=None) -> list:
    day = end or (datetime.now(_TPE).date() - timedelta(days=1))
    days = []
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return days[::-1]


def _gbm(n: int, vol: float, seed: int, *, start: float = 100.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    steps = rng.normal(-0.5 * vol**2 / 252, vol / math.sqrt(252), n - 1)
    return start * np.exp(np.concatenate([[0.0], np.cumsum(steps)]))


def _chart_json(
    *,
    vol: float = 0.35,
    n: int = 260,
    seed: int = 1,
    instrument: str = "EQUITY",
    currency: str = "USD",
    tz: str = "America/New_York",
    end=None,
    closes=None,
    adjclose=None,
    dividends=(),
) -> dict:
    """合成的 Yahoo chart JSON（同 /v8/finance/chart 的形狀）。dividends＝[(第幾天, 金額)]。"""
    days = _business_days(n, end)
    close = list(closes) if closes is not None else _gbm(n, vol, seed).tolist()
    adj = list(adjclose) if adjclose is not None else list(close)
    zone = ZoneInfo(tz)
    stamps = [int(datetime(d.year, d.month, d.day, 16, 0, tzinfo=zone).timestamp()) for d in days]
    events = {str(stamps[i]): {"amount": amount, "date": stamps[i]} for i, amount in dividends}
    return {
        "chart": {
            "result": [
                {
                    "meta": {"instrumentType": instrument, "currency": currency, "exchangeTimezoneName": tz},
                    "timestamp": stamps,
                    "indicators": {"quote": [{"close": close}], "adjclose": [{"adjclose": adj}]},
                    "events": {"dividends": events},
                }
            ]
        }
    }


def _stats(
    symbol: str,
    vol: float,
    *,
    seed: int = 1,
    market: str = "US",
    n: int = 260,
    div: float = 0.0,
    instrument: str = "EQUITY",
    dates=None,
    closes=None,
) -> fcn_eval.TickerStats:
    dates = list(dates) if dates is not None else _business_days(n)
    closes = np.asarray(closes if closes is not None else _gbm(len(dates), vol, seed), dtype=float)
    return fcn_eval.TickerStats(
        symbol=symbol, market=market, currency="TWD" if market == "TW" else "USD", dates=tuple(dates),
        closes=closes, spot=float(closes[-1]), last_date=dates[-1], vol=vol, div_yield=div,
        fetched_at=time.time(), instrument_type=instrument,
    )


def _request(symbols, rate: float = 0.18, currency=None, **terms) -> fcn_eval.FcnRequest:
    return fcn_eval.FcnRequest(
        symbols=tuple(symbols), rate=rate, terms=fcn_eval.Terms(**terms), currency=currency
    )


class _Market:
    """合成的 Yahoo：每個代號的 chart JSON、近四季淨利、^IRX；記錄每次呼叫。"""

    def __init__(self) -> None:
        self.charts: dict[str, object] = {}
        self.income: dict[str, object] = {}
        self.rf: object = 0.0404
        self.chart_calls: list[str] = []
        self.income_calls: list[str] = []
        self.rf_calls = 0
        self._lock = threading.Lock()

    def fetch_chart(self, symbol, range_value="1y"):
        with self._lock:
            self.chart_calls.append(symbol)
        return self.charts.get(symbol)

    def fetch_net_income(self, symbol):
        with self._lock:
            self.income_calls.append(symbol)
        return self.income.get(symbol, (1.0e9, 2.0e9, 1.5e9, 3.0e9))

    def fetch_usd_rf(self):
        with self._lock:
            self.rf_calls += 1
        return self.rf


@pytest.fixture
def market(monkeypatch) -> _Market:
    m = _Market()
    m.charts.update({
        "NVDA": _chart_json(vol=0.50, seed=1),
        "AMD": _chart_json(vol=0.55, seed=2),
        "TSLA": _chart_json(vol=0.60, seed=3),
        "SPY": _chart_json(vol=0.15, seed=4, instrument="ETF"),
        "2330.TW": _chart_json(vol=0.30, seed=5, currency="TWD", tz="Asia/Taipei"),
    })
    monkeypatch.setattr(fcn_eval, "_fetch_chart", m.fetch_chart)
    monkeypatch.setattr(fcn_eval, "_fetch_net_income", m.fetch_net_income)
    monkeypatch.setattr(fcn_eval, "_fetch_usd_rf", m.fetch_usd_rf)
    monkeypatch.setattr(fcn_eval, "N_PATHS", 4000)  # simulate 在呼叫時才讀 N_PATHS
    monkeypatch.delenv("LINE_BOT_FCN_TWD_RF", raising=False)
    return m


@pytest.fixture
def fresh_pools(monkeypatch):
    """卡住／逾時測試：新的 executor 與 semaphore、時限 0.2 秒；teardown 放行並關掉。"""
    release = threading.Event()
    price_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="test-fcn-price")
    fin_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="test-fcn-fin")
    monkeypatch.setattr(fcn_eval, "_PRICE_POOL", price_pool)
    monkeypatch.setattr(fcn_eval, "_FIN_POOL", fin_pool)
    monkeypatch.setattr(fcn_eval, "_PRICE_SLOTS", threading.BoundedSemaphore(6))
    monkeypatch.setattr(fcn_eval, "_FIN_SLOTS", threading.BoundedSemaphore(4))
    monkeypatch.setattr(fcn_eval, "DATA_BUDGET_S", 0.2)
    yield release
    release.set()
    price_pool.shutdown(wait=False, cancel_futures=True)
    fin_pool.shutdown(wait=False, cancel_futures=True)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _card_texts(card: dict) -> list[str]:
    """卡片上看得到或會送出的字：text 節點、按鈕 label 與送出的文字。"""
    texts = []
    for node in _walk(card):
        if node.get("type") == "text":
            texts.append(node["text"])
        action = node.get("action")
        if isinstance(action, dict):
            texts += [action.get("label", ""), action.get("text", "")]
    return texts


def _actions(card: dict) -> list[dict]:
    return [node["action"] for node in _walk(card) if isinstance(node.get("action"), dict)]


def _assert_text_survives_outbound(text: str) -> None:
    result = output_validator.validate_outbound_text(text)
    assert result.ok and result.text == text, (text, result)
    assert main._md_to_line(text) == text
    assert main._prepare_outbound_text(text, source="reply") == text
    assert "@" not in text and "＠" not in text


def _assert_sendable_card(card: dict, alt: str) -> None:
    assert _PREFIX in alt and 0 < len(alt) <= ALT_TEXT_MAX
    assert _PREFIX in card["header"]["contents"][0]["text"]
    assert len(json.dumps(card, ensure_ascii=False).encode("utf-8")) <= BUBBLE_MAX_BYTES
    assert FlexContainer.from_dict(card).to_dict() == card
    for node in _walk(card):
        assert not {"uri", "url", "data", "altUri"} & set(node)
    actions = _actions(card)
    assert actions
    for action in actions:
        assert action["type"] == "message"
        assert 0 < len(action["label"]) <= LABEL_MAX
        assert 0 < len(action["text"]) <= ACTION_TEXT_MAX
    for text in [alt, *_card_texts(card)]:
        _assert_text_survives_outbound(text)
    assert not main._is_system_status_outbound(alt)
    if hasattr(main, "_validated_flex_card_message"):
        main._validated_flex_card_message(alt, card)  # main 送卡前的同一道檢查


def _strict_ok(body: str) -> fcn_eval.FcnRequest:
    req, problem = fcn_eval.parse_strict(body)
    assert problem is None, problem
    assert req is not None
    return req


def _strict_problem(body: str):
    req, problem = fcn_eval.parse_strict(body)
    assert req is None and problem is not None, req
    return problem


# ── 1. 嚴格解析：利率 ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "body, rate",
    [
        ("NVDA 年利率18%", 0.18),
        ("NVDA 年化17.89%", 0.1789),
        ("NVDA 年化約17.89%", 0.1789),
        ("NVDA 18%", 0.18),  # 沒有標籤、只有一個 %
        ("NVDA 年利率18", 0.18),  # 標籤後的 % 可省
        ("NVDA 17.89% p.a.", 0.1789),
        ("NVDA 月領1.5%", 0.18),
        ("NVDA 月利率1.5%", 0.18),
        ("NVDA 每月利率1.5%", 0.18),
        ("NVDA 月配息率1.2%", 0.144),
        ("NVDA 每月票息1.5%", 0.18),
        ("NVDA 1.5%/月", 0.18),
        ("NVDA 1.5% per month", 0.18),
        ("NVDA 每月1.4908% 年化17.89%", 0.178896),  # 差 ≤ 0.05 個百分點：同一個，取月×12
        ("NVDA 月利率1.5% 每月1.5%", 0.18),
        ("NVDA 年利率18% 年利率18%", 0.18),
        ("NVDA 年利率18.9999999%", 0.189999999),  # 小數多也不會被截成 18
    ],
)
def test_rate_forms_become_one_annual_rate(body, rate):
    req = _strict_ok(body)
    assert math.isclose(req.rate, rate, abs_tol=1e-6)


@pytest.mark.parametrize(
    "body, text",
    [
        ("NVDA 每月1.5% 年利率20%", "FCN 評估：看到兩個不同的利率（18%、20%），請只寫年利率"),
        ("NVDA 年利率12% 年利率12.01% 月利率1%", "FCN 評估：看到兩個不同的利率（12%、12.01%），請只寫年利率"),
        ("NVDA 每月1.5% 每月1.6%", "FCN 評估：看到兩個不同的利率（18%、19.2%），請只寫年利率"),
        ("NVDA 年利率15~18%", "FCN 評估：利率請寫一個數字，例如 年利率18%"),
        ("NVDA 年利率 15-18%", "FCN 評估：利率請寫一個數字，例如 年利率18%"),
        ("NVDA 年利率 -5%", "FCN 評估：利率請寫一個數字，例如 年利率18%"),
        ("NVDA -5%", "FCN 評估：利率請寫一個數字，例如 年利率18%"),
        ("NVDA 年利率0%", "FCN 評估：年利率 0% 看起來不對，請確認（0–60%）"),
        ("NVDA 年利率61%", "FCN 評估：年利率 61% 看起來不對，請確認（0–60%）"),
    ],
)
def test_conflicting_or_impossible_rates_are_questioned(body, text):
    problem = _strict_problem(body)
    assert problem.kind == "conflict"
    assert problem.text == text


def test_the_rate_limits_are_inclusive_at_sixty_percent():
    assert _strict_ok("NVDA 年利率60%").rate == pytest.approx(0.60)
    assert _strict_ok("NVDA 年利率0.5%").rate == pytest.approx(0.005)


def test_a_negative_rate_right_after_its_label_is_questioned():
    reply = fcn_eval.handle("NVDA 年利率-5%")
    assert reply.flex is None
    assert reply.text == "FCN 評估：利率請寫一個數字，例如 年利率18%"


@pytest.mark.parametrize(
    "body, rate",
    [
        ("NVDA 年化18% 每月配息", 0.18),
        ("NVDA 年利率18%每月配息", 0.18),
        ("NVDA 年利率 15% 每月配息", 0.15),
        ("NVDA 年利率18% 月配", 0.18),
        ("NVDA 18% 每月配息", 0.18),
    ],
)
def test_paid_monthly_after_an_annual_rate_keeps_it_annual(body, rate):
    # 「每月配息」是配息方式，不是把前面的 % 變成月利率（以前會變成 216%）。
    assert _strict_ok(body).rate == pytest.approx(rate)
    req, problem, _unread = fcn_eval.extract_lenient(body)
    assert problem is None and req.rate == pytest.approx(rate)


# ── 1. 嚴格解析：KO／KI／執行價／跌幅 ────────────────────────────────────────
@pytest.mark.parametrize(
    "terms_text, expected",
    [
        ("KO價100%", {"ko": 1.00, "ko_given": True}),
        ("KO 95%", {"ko": 0.95, "ko_given": True}),
        ("提前出場價格100%", {"ko": 1.00, "ko_given": True}),
        ("敲出價105%", {"ko": 1.05}),
        ("Knock-out 98%", {"ko": 0.98}),
        ("autocall 100%", {"ko": 1.00, "ko_given": True}),
        ("KO Barrier: 100% KI Barrier: 60%", {"ko": 1.00, "ki": 0.60, "ko_given": True, "ki_given": True}),
        ("KO trigger 98%", {"ko": 0.98}),
        ("KI Level: 60%", {"ki": 0.60, "strike": 0.60, "ki_given": True}),
        ("Knock-in 60%", {"ki": 0.60, "strike": 0.60}),
        ("敲入價65%", {"ki": 0.65}),
        ("下限價格60%", {"ki": 0.60}),
        ("保護價70%", {"ki": 0.70, "ki_given": True}),
        ("履約價格80%", {"strike": 0.80, "ki": 0.80, "strike_given": True}),
        ("執行價格 80%", {"strike": 0.80}),
        ("strike 85%", {"strike": 0.85}),
        ("KI60% 執行價80%", {"ki": 0.60, "strike": 0.80}),
        ("EKI60%", {"ki": 0.60, "eki": True}),
        ("KI60% 到期觀察", {"ki": 0.60, "eki": True}),
        ("KI60% 歐式", {"ki": 0.60, "eki": True}),
        ("記憶式KO 100%", {"memory_ko": True, "ko": 1.00}),
        ("記憶型 KO95%", {"memory_ko": True, "ko": 0.95}),
    ],
)
def test_barrier_label_variants_are_read(terms_text, expected):
    terms = _strict_ok(f"NVDA 年利率18% {terms_text}").terms
    for name, value in expected.items():
        assert getattr(terms, name) == pytest.approx(value), name


@pytest.mark.parametrize(
    "drop_text, level",
    [
        ("跌30%", 0.70),  # 跌X%＝跌幅
        ("下跌25%", 0.75),
        ("跌40%接股", 0.60),
        ("跌破30%", 0.70),  # 跌破 X<50：當跌幅（Andrew 的用法）
        ("跌破70%", 0.70),  # 跌破 X>50：當門檻
        ("跌破50%", 0.50),
        ("跌破期初70%", 0.70),  # 跌破期初X%：門檻
        ("跌破期初價60%", 0.60),
        ("低於期初60%", 0.60),
    ],
)
def test_drop_words_set_knock_in_and_strike(drop_text, level):
    terms = _strict_ok(f"NVDA 年利率18% {drop_text}").terms
    assert terms.ki == pytest.approx(level)
    assert terms.strike == pytest.approx(level)
    assert terms.ki_given and terms.strike_given


def test_an_explicit_knock_in_wins_over_a_drop_word():
    terms = _strict_ok("NVDA 年利率18% KO100% 跌30% KI60%").terms
    assert (terms.ki, terms.strike) == (pytest.approx(0.60), pytest.approx(0.60))


def test_ski_is_not_a_knock_in():
    problem = _strict_problem("NVDA 年利率18% SKI10%")
    assert problem.kind == "unread" and "10%" in problem.text
    req, problem, unread = fcn_eval.extract_lenient("NVDA 年利率18% SKI10%")
    assert problem is None and req.terms.ki == fcn_eval.KI_DEFAULT
    assert unread == ["10%"]


def test_default_terms_are_marked_as_not_given():
    terms = _strict_ok("NVDA 年利率18%").terms
    assert (terms.ko, terms.ki, terms.strike, terms.months) == (1.0, 0.7, 0.7, 6)
    assert not (terms.ko_given or terms.ki_given or terms.strike_given or terms.months_given)
    assert not (terms.eki or terms.memory_ko)


@pytest.mark.parametrize(
    "terms_text, reason",
    [
        ("KI70% 執行價60%", "執行價比 KI 低"),
        ("KO80% KI80%", "KO 要比 KI 高"),
        ("KO70%", "KO 要在 80%–120%"),
        ("KI30%", "KI 要在 40%–95%"),
        ("執行價120%", "執行價要在 50%–110%"),
    ],
)
def test_impossible_terms_are_questioned(terms_text, reason):
    problem = _strict_problem(f"NVDA 年利率18% {terms_text}")
    assert problem.kind == "conflict"
    assert problem.text == f"FCN 評估：條件看起來不對（{reason}），請確認後再打"


@pytest.mark.parametrize("terms_text, what", [("KO95% KO100%", "KO"), ("KI60% KI65%", "KI"), ("執行價80% 執行價85%", "執行價")])
def test_two_values_for_one_barrier_are_questioned(terms_text, what):
    problem = _strict_problem(f"NVDA 年利率18% {terms_text}")
    assert problem.text == f"FCN 評估：{what}寫了不只一個，請只寫一個"


def test_strike_only_input_sets_knock_in_up_to_95_percent():
    # 只寫執行價：≤ 95% 時 KI＝執行價；更高（例如期初價接股）時 KI 照預設 70%。
    at_95 = _strict_ok("NVDA 年利率18% 執行價95%").terms
    assert (at_95.ki, at_95.strike) == (pytest.approx(0.95), pytest.approx(0.95))
    for text, strike in (("執行價96%", 0.96), ("執行價100%", 1.0)):
        req = _strict_ok(f"NVDA 年利率18% {text}")
        assert (req.terms.ki, req.terms.strike) == (fcn_eval.KI_DEFAULT, pytest.approx(strike))
        assert req.terms.strike_given and not req.terms.ki_given
    # 2026-10-10：KI 只是預設，不寫進標準指令（重算後仍標「預設」）。
    command = fcn_eval.to_command(_strict_ok("NVDA 年利率18% 執行價100%"))
    assert command.endswith("執行價100%") and "KI" not in command


def test_a_default_knock_in_behind_a_high_strike_is_marked_as_default():
    req = _strict_ok("NVDA 年利率18% 執行價100%")
    card, _alt = fcn_eval.build_confirm_card(req, [])
    ki_row = next(t for t in _card_texts(card) if t.startswith("接股門檻"))
    assert "預設" in ki_row
    assert "接股門檻" in fcn_eval._terms_line(req.terms, True)  # 只有 KI 是預設


def test_english_memory_is_consumed_like_記憶式():
    req, problem = fcn_eval.parse_strict("NVDA 年利率18% memory KO100%")
    assert problem is None
    assert req.terms.memory_ko is True


# ── 1. 嚴格解析：期限 ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "tenor_text, months",
    [
        ("6個月", 6), ("12M", 12), ("1Y", 12), ("1年", 12), ("2年期", 24), ("12 months", 12),
        ("半年", 6), ("18月期", 18), ("24個月", 24), ("12個月 1年", 12),
        ("六個月", 6), ("十二個月", 12), ("一年", 12), ("一年期", 12), ("兩年", 24), ("二十四個月", 24),
        ("3M", 3),  # 裸 3M 當期限（DECISIONS：3M 公司的代號是 MMM）
    ],
)
def test_tenor_forms(tenor_text, months):
    terms = _strict_ok(f"NVDA 年利率18% {tenor_text}").terms
    assert terms.months == months and terms.months_given


@pytest.mark.parametrize(
    "tenor_text, text",
    [
        ("112個月", "FCN 評估：期限請寫 1–24 個月"),  # 不是 12 個月
        ("120M", "FCN 評估：期限請寫 1–24 個月"),
        ("100 months", "FCN 評估：期限請寫 1–24 個月"),
        ("36個月", "FCN 評估：期限請寫 1–24 個月"),
        ("0個月", "FCN 評估：期限請寫 1–24 個月"),
        ("十年", "FCN 評估：期限請寫 1–24 個月"),
        ("三十個月", "FCN 評估：期限請寫 1–24 個月"),
        ("二十五個月", "FCN 評估：期限請寫 1–24 個月"),
        ("6個月 12個月", "FCN 評估：看到兩個不同的期限，請只寫一個，例如 6個月"),
    ],
)
def test_bad_or_conflicting_tenors_are_questioned_in_both_modes(tenor_text, text):
    body = f"NVDA 年利率18% {tenor_text}"
    problem = _strict_problem(body)
    assert (problem.kind, problem.text) == ("conflict", text)
    req, lenient_problem, _unread = fcn_eval.extract_lenient(body)
    assert req is None and lenient_problem.text == text


def test_a_lock_up_month_glued_to_its_label_is_not_the_tenor():
    # 2026-10-10：鎖定期整段當雜訊略過（不是期限、不影響評估），嚴格模式也能直接評估。
    req = _strict_ok("NVDA 年利率18% 鎖定期1個月")
    assert req.terms.months == 6 and not req.terms.months_given
    req, problem, _unread = fcn_eval.extract_lenient("NVDA 年利率18% 鎖定期1個月")
    assert problem is None and req.terms.months == 6 and not req.terms.months_given


@pytest.mark.parametrize("body", ["NVDA 年利率18% 天期：6個月 鎖定期：1個月", "NVDA 年利率18% 6個月 閉鎖期 1個月"])
def test_a_lock_up_period_with_a_colon_or_space_is_not_a_second_tenor(body):
    req, problem, _unread = fcn_eval.extract_lenient(body)
    assert problem is None
    assert req.terms.months == 6


# ── 1. 嚴格解析：幣別 ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "body, currency",
    [
        ("NVDA 年利率18% 美元計價", "USD"),
        ("NVDA 年利率18% 計價幣別：台幣", "TWD"),
        ("NVDA 年利率18% 幣別 USD", "USD"),
        ("NVDA 年利率18% TWD計價", "TWD"),
        ("NVDA 年利率18% 美元", "USD"),
        ("NVDA 年利率18% 台幣", "TWD"),
        ("NVDA 年利率18% 最低申購 USD 50,000", "USD"),  # 弱訊號
        ("台幣計價 最低申購 USD 50,000 NVDA 年利率18%", "TWD"),  # 強訊號優先
        ("美元計價，台積電期初價1000台幣 年利率12%", "USD"),  # 標的報價旁的幣別不算
        ("NVDA 年利率18%", None),
    ],
)
def test_note_currency_signals(body, currency):
    assert _strict_ok(body).currency == currency


@pytest.mark.parametrize(
    "body, text",
    [
        ("NVDA 年利率18% EUR計價", fcn_eval.TEXT_UNSUPPORTED_CURRENCY),
        ("NVDA 年利率18% 歐元", fcn_eval.TEXT_UNSUPPORTED_CURRENCY),
        ("最低申購 EUR 50,000 NVDA 年利率18%", fcn_eval.TEXT_UNSUPPORTED_CURRENCY),
        ("NVDA 年利率18% 美元 台幣", "FCN 評估：看到兩種計價幣別，請只寫一種（美元或台幣）"),
        ("最低申購 USD 50,000 面額 TWD 1,000,000 NVDA 年利率18%", "FCN 評估：看到兩種計價幣別，請只寫一種（美元或台幣）"),
    ],
)
def test_unsupported_or_conflicting_currencies(body, text):
    assert _strict_problem(body).text == text


# ── 1. 嚴格解析：股票 ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "names, symbols",
    [
        ("輝達", ("NVDA",)),
        ("台積電", ("2330",)),
        ("南亞科", ("2408",)),
        ("南亞", ("1303",)),
        ("長榮航", ("2618",)),
        ("長榮", ("2603",)),
        ("中鋼", ("2002",)),
        ("台積電聯發科", ("2330", "2454")),
        ("台積電ADR", ("TSM",)),
        ("特斯拉、蘋果", ("TSLA", "AAPL")),
        ("2002", ("2002",)),
        ("6488.TWO", ("6488.TWO",)),
        ("6488.two", ("6488.TWO",)),
        ("2330.TW 台積電", ("2330",)),  # 同一檔只算一次
        ("2330 2330.TWO", ("2330.TWO",)),  # 上櫃後綴比較明確
        ("WFC", ("WFC",)),
        ("KO", ("KO",)),  # 單獨的 KO 是可口可樂
        ("MMM", ("MMM",)),
        ("nvda", ("NVDA",)),
        ("NVDA nvda 輝達", ("NVDA",)),
        ("BRK.B", ("BRK-B",)),
        ("NVIDIA", ("NVDA",)),
        ("2330/2454", ("2330", "2454")),
        ("NVDA、AMD與TSLA", ("NVDA", "AMD", "TSLA")),
    ],
)
def test_symbols_names_and_codes(names, symbols):
    assert _strict_ok(f"{names} 年利率18%").symbols == symbols


@pytest.mark.parametrize("ticker", ["^GSPC", "ES=F", "BTC-USD"])
def test_index_future_and_crypto_tickers_are_refused(ticker):
    problem = _strict_problem(f"{ticker} 年利率18%")
    assert problem.kind == "unread"
    reply = fcn_eval.handle(f"{ticker} 年利率18%")
    assert reply.flex is None and reply.text == problem.text


@pytest.mark.parametrize(
    "body",
    [
        "輝達 @all 幫我看",
        "NVDA ＠all 年利率18%",
        "NVDA 年利率18% @爸爸 <b>x</b>",
        "NVDA 年利率18% 甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳 午未 申酉 戌亥",
    ],
)
def test_unread_words_are_shown_sanitized(body):
    problem = _strict_problem(body)
    assert problem.kind == "unread"
    assert "@" not in problem.text and "＠" not in problem.text and "<" not in problem.text
    shown = re.search(r"有看不懂的字（(.*?)）", problem.text)
    assert shown, problem.text
    words = shown.group(1).split("、")
    assert 1 <= len(words) <= 3
    assert all(0 < len(w) <= 12 for w in words)
    reply = fcn_eval.handle(body)
    for text in (reply.text, reply.alt_text):
        assert text is None or ("@" not in text and "＠" not in text)


def test_ticker_count_limits():
    assert _strict_problem("年利率18%").text == fcn_eval.TEXT_NO_SYMBOL
    assert _strict_problem("NVDA AMD").text == fcn_eval.TEXT_NO_RATE
    assert _strict_ok("NVDA AMD TSLA AAPL 年利率18%").symbols == ("NVDA", "AMD", "TSLA", "AAPL")
    problem = _strict_problem("NVDA AMD TSLA AAPL MSFT 年利率18%")
    assert (problem.kind, problem.text) == ("too_many", "FCN 評估：FCN 最多 4 檔，這裡有 5 檔")


def test_taiwan_semiconductor_adr_with_a_space_is_one_us_ticker():
    assert _strict_ok("台積電 ADR 年利率18%").symbols == ("TSM",)


# ── 2. 寬鬆讀取（理專訊息） ─────────────────────────────────────────────────
def test_realistic_banker_message_reads_exactly_the_terms():
    req, problem, unread = fcn_eval.extract_lenient(BANKER_MESSAGE)
    assert problem is None and unread == []
    assert req.symbols == ("NVDA", "AMD", "TSLA")
    assert req.rate == pytest.approx(0.178896, abs=1e-9)
    t = req.terms
    assert (t.months, t.ko, t.ki, t.strike) == (6, 1.0, pytest.approx(0.6), pytest.approx(0.8))
    assert t.memory_ko and not t.eki
    assert req.currency == "USD"


@pytest.mark.parametrize(
    "noise",
    [
        "℡ 02-2345-6789",
        "TEL 02-2345-6789",
        "分機 1234",
        "傳真 02-2345-6780",
        "10:00 AM",
        "2027.04.22",
        "2027/4/22",
        "2027年4月22日",
        "ADR",
        "AI 概念股",
        "P.A.",
        "ISIN US0378331005",
        "https://example.com/AMD?q=TSLA",
        "fund.desk@example.com",
        # 測試資料只放號碼開頭（git 隱私稽核會擋完整的手機號碼格式）。
        "理專手機 0912",
        "行動 0912",
        "聯絡 02-2345-6789",
        "0912",  # 沒標籤的手機號碼開頭：09xx 不是台股代號
    ],
)
def test_lenient_reading_ignores_noise_and_non_whitelisted_words(noise):
    req, problem, _unread = fcn_eval.extract_lenient(f"{noise}\nNVDA 年利率12%")
    assert problem is None
    assert req.symbols == ("NVDA",) and req.rate == pytest.approx(0.12)


def test_lenient_reading_uses_names_and_whitelist_only():
    assert fcn_eval.extract_lenient("台積電 ADR、2330.TW 年化 12%")[0].symbols == ("TSM", "2330")
    assert fcn_eval.extract_lenient("南亞科 長榮航 年化 12%")[0].symbols == ("2408", "2618")
    assert fcn_eval.extract_lenient("Nvidia、Tesla 年化 12%")[0].symbols == ("NVDA", "TSLA")
    assert fcn_eval.extract_lenient("0050、00878 年化 8%")[0].symbols == ("0050", "00878")
    # 理專訊息裡的英文代號只收白名單內、原文大寫的
    assert fcn_eval.extract_lenient("AI ADR TEL 年利率12%") == (None, None, [])
    assert fcn_eval.extract_lenient("nvda 年利率12%") == (None, None, [])
    assert "ADR" not in fcn_eval._US_WHITELIST and "AI" not in fcn_eval._US_WHITELIST


@pytest.mark.parametrize(
    "message, symbols, ko",
    [
        ("KO Barrier: 100% KI Barrier: 60%\nNVDA、AMD 年化 12%", ("NVDA", "AMD"), 1.0),
        ("KO：100%\nNVDA 年化 12%", ("NVDA",), 1.0),
        ("KO 觀察：每月\nNVDA 年化 12%", ("NVDA",), fcn_eval.KO_DEFAULT),
        ("連結標的：KO、NVDA 年化 12%", ("KO", "NVDA"), fcn_eval.KO_DEFAULT),  # 這個 KO 才是可口可樂
    ],
)
def test_lenient_reading_tells_knock_out_from_coca_cola(message, symbols, ko):
    req, problem, _unread = fcn_eval.extract_lenient(message)
    assert problem is None
    assert req.symbols == symbols and req.terms.ko == pytest.approx(ko)


def test_lenient_reading_needs_a_stock_and_a_rate_and_keeps_conflicts():
    assert fcn_eval.extract_lenient("NVDA AMD") == (None, None, [])
    assert fcn_eval.extract_lenient("年利率12%") == (None, None, [])
    _req, problem, _unread = fcn_eval.extract_lenient("NVDA 年化12% 年化14%")
    assert problem.kind == "conflict"


# ── 3. 標準指令 round trip ──────────────────────────────────────────────────
_RT_SYMBOLS = [
    ("NVDA",), ("NVDA", "AMD", "TSLA"), ("2330",), ("6488.TWO", "2330"), ("BRK-B", "KO", "WFC"),
    ("0050", "2002", "NVDA", "TSM"), ("006208",), ("00878", "SPY"), ("00632R",),
]
_RT_RATES = [0.18, 0.178896, 0.0525, 0.123456, 0.6, 0.0001]
_RT_MONTHS = [(6, False), (1, True), (6, True), (24, True)]  # (月數, 有沒有寫)
_RT_TERMS = [
    {}, {"ko": 0.95}, {"ko": 1.05, "ki": 0.6, "strike": 0.6}, {"ki": 0.6, "strike": 0.8},
    {"ki": 0.65, "strike": 0.65, "eki": True}, {"ki": 0.5, "strike": 1.0},
    {"ko": 0.85, "ki": 0.55, "strike": 0.825, "eki": True, "memory_ko": True}, {"memory_ko": True},
    {"strike": 0.75},
]
_RT_CURRENCIES = [None, "USD", "TWD"]


def test_canonical_command_round_trips_for_generated_requests():
    count = 0
    for symbols, rate, (months, given), terms, currency in itertools.product(
        _RT_SYMBOLS, _RT_RATES, _RT_MONTHS, _RT_TERMS, _RT_CURRENCIES
    ):
        req = _request(symbols, rate, currency, months=months, months_given=given, **terms)
        command = fcn_eval.to_command(req)
        assert command.startswith("/FCN ") and len(command) <= ACTION_TEXT_MAX, command
        assert ("個月" in command) == given, command  # 沒寫的期限不寫進指令
        back, problem = fcn_eval.parse_strict(command[len("/FCN"):])
        assert problem is None, (command, problem)
        assert back.effective() == req.effective(), command
        assert back.terms.months_given == given
        count += 1
    assert count == len(_RT_SYMBOLS) * len(_RT_RATES) * len(_RT_MONTHS) * len(_RT_TERMS) * len(_RT_CURRENCIES)


@pytest.mark.parametrize(
    "body",
    [
        "NVDA AMD TSLA 年利率18%",
        "輝達 台積電 每月1.4908% 年化17.89% KO95% 12個月 美元",
        "6488 2330.TWO 年利率9.5% 跌30% 台幣",
        "NVDA 年利率18% 跌破期初65% 記憶式 3M",
        "NVDA 年利率18% EKI60% 執行價85% 18個月",
        "NVDA 年利率18% KI60% 到期觀察 十二個月",
        "NVDA 年利率18% 執行價100%",
        "NVDA AMD 年化 15% 每月配息 KO Barrier: 98%",
    ],
)
def test_parsed_requests_round_trip_through_the_canonical_command(body):
    req = _strict_ok(body)
    back = _strict_ok(fcn_eval.to_command(req)[len("/FCN"):])
    assert back.effective() == req.effective()
    assert back.terms.months_given == req.terms.months_given


def test_lenient_requests_round_trip_through_the_confirm_button():
    req, _problem, _unread = fcn_eval.extract_lenient(BANKER_MESSAGE)
    card, _alt = fcn_eval.build_confirm_card(req, [])
    (action,) = _actions(card)
    assert action["text"] == fcn_eval.to_command(req)
    assert _strict_ok(action["text"][len("/FCN"):]).effective() == req.effective()


# ── 4. 模擬 ─────────────────────────────────────────────────────────────────
_RF = {"US": 0.04, "TW": 0.017}


def _simulate(stats, terms=None, rf=0.04, **kwargs):
    kwargs.setdefault("n_paths", 4000)
    return _REAL_SIMULATE(list(stats), terms or fcn_eval.Terms(), rf, _RF, **kwargs)


def test_higher_volatility_asks_for_a_higher_rate():
    calm = _simulate([_stats("AAA", 0.20, seed=1)])
    wild = _simulate([_stats("AAA", 0.50, seed=1)])
    assert wild.fair > calm.fair + 0.03
    assert wild.p_convert > calm.p_convert


def test_more_tickers_ask_for_a_higher_rate():
    a, b, c = _stats("AAA", 0.30, seed=1), _stats("BBB", 0.50, seed=2), _stats("CCC", 0.40, seed=3)
    one, two, three = _simulate([a]), _simulate([a, b]), _simulate([a, b, c])
    assert one.fair < two.fair < three.fair
    assert one.p_convert < two.p_convert < three.p_convert


def test_two_identical_perfectly_correlated_tickers_are_about_one():
    a = _stats("AAA", 0.30, seed=1)
    twin = fcn_eval.TickerStats(**{**a.__dict__, "symbol": "AAB"})
    single, pair = _simulate([a]), _simulate([a, twin])
    assert abs(pair.fair - single.fair) < 0.01
    assert abs(pair.p_convert - single.p_convert) < 0.02


def test_reordering_the_tickers_changes_nothing():
    stats = [_stats("AAA", 0.30, seed=1), _stats("BBB", 0.50, seed=2), _stats("CCC", 0.40, seed=3)]
    base = _simulate(stats)
    for order in itertools.permutations(range(3)):
        result = _simulate([stats[i] for i in order])
        assert (result.fair, result.p_convert) == (base.fair, base.p_convert)
        assert result.convert_counts == tuple(base.convert_counts[i] for i in order)
        assert result.breach_counts == tuple(base.breach_counts[i] for i in order)
        top = max(range(3), key=lambda i: result.convert_counts[i])
        assert stats[order[top]].symbol == "BBB"


def test_result_counts_are_consistent():
    result = _simulate([_stats("AAA", 0.40, seed=1), _stats("BBB", 0.50, seed=2)], n_paths=5000)
    assert sum(result.convert_counts) == round(result.p_convert * 5000)
    assert all(b >= c for b, c in zip(result.breach_counts, result.convert_counts))
    again = _simulate([_stats("AAA", 0.40, seed=1), _stats("BBB", 0.50, seed=2)], n_paths=5000)
    assert again == result  # 固定種子：同樣輸入同樣結果


def test_cross_timezone_correlation_uses_weekly_returns():
    # 美股收盤後的消息隔天才進台股：同一串報酬，台股晚一個交易日。
    days = _business_days(261)
    closes = _gbm(260, 0.35, seed=11)
    us = _stats("AAA", 0.35, market="US", dates=days[:-1], closes=closes)
    tw = _stats("2330", 0.35, market="TW", dates=days[1:], closes=closes)
    corr, note = fcn_eval._correlation([tw, us])
    assert corr[0, 1] > 0.5 and not note
    # 當成同一個市場（日報酬）就幾乎不相關：證明是週報酬救回來的
    same_market = fcn_eval.TickerStats(**{**tw.__dict__, "market": "US"})
    daily, _ = fcn_eval._correlation([same_market, us])
    assert abs(daily[0, 1]) < 0.3


def test_pairs_with_too_little_overlap_count_as_unrelated():
    days = _business_days(260)
    a = _stats("AAA", 0.3, seed=1, dates=days[:130])
    b = _stats("BBB", 0.3, seed=2, dates=days[100:])
    corr, note = fcn_eval._correlation([a, b])
    assert note and corr[0, 1] == pytest.approx(0.0, abs=1e-6)
    assert _simulate([a, b]).corr_note


def test_almost_no_volatility_means_no_conversion_and_the_risk_free_rate():
    result = _simulate([_stats("AAA", 1e-4, seed=1)], rf=0.04)
    assert result.p_convert == 0.0
    assert result.fair == pytest.approx(12 * (math.exp(0.04 / 12) - 1), abs=2e-4)


def test_dividends_raise_the_fair_rate():
    plain = _simulate([_stats("AAA", 0.35, seed=1)])
    paying = _simulate([_stats("AAA", 0.35, seed=1, div=0.05)])
    assert paying.fair > plain.fair and paying.p_convert >= plain.p_convert


def test_european_knock_in_converts_less_than_daily_knock_in():
    stats = [_stats("AAA", 0.30, seed=1), _stats("BBB", 0.50, seed=2)]
    daily = _simulate(stats, fcn_eval.Terms(ki=0.6, strike=0.8))
    european = _simulate(stats, fcn_eval.Terms(ki=0.6, strike=0.8, eki=True))
    assert european.p_convert < daily.p_convert
    assert european.fair < daily.fair
    same_level = _simulate(stats, fcn_eval.Terms(ki=0.6, strike=0.6, eki=True))
    assert same_level.p_convert <= _simulate(stats, fcn_eval.Terms(ki=0.6, strike=0.6)).p_convert


def test_a_lower_knock_out_ends_notes_earlier():
    stats = [_stats("AAA", 0.30, seed=1), _stats("BBB", 0.50, seed=2)]
    at_100 = _simulate(stats, fcn_eval.Terms(ko=1.00))
    at_95 = _simulate(stats, fcn_eval.Terms(ko=0.95))
    # 更早提前結束：撐到到期被接的少，但領息的月數也少，每月要多給
    assert at_95.p_convert < at_100.p_convert
    assert at_95.fair > at_100.fair


def test_simulate_reads_n_paths_when_called(monkeypatch):
    monkeypatch.setattr(fcn_eval, "N_PATHS", 1000)
    result = _REAL_SIMULATE([_stats("AAA", 0.6, seed=1)], fcn_eval.Terms(), 0.04, _RF)
    assert result.p_convert > 0
    assert sum(result.convert_counts) == round(result.p_convert * 1000)


def test_realistic_size_simulation_is_fast():
    stats = [_stats(s, v, seed=i) for i, (s, v) in enumerate((("AAA", 0.3), ("BBB", 0.5), ("CCC", 0.4), ("DDD", 0.6)))]
    started = time.perf_counter()
    result = _REAL_SIMULATE(stats, fcn_eval.Terms(months=24), 0.04, _RF)  # 預設 N_PATHS
    assert time.perf_counter() - started < 5.0
    assert math.isfinite(result.fair) and 0.0 <= result.p_convert <= 1.0


def test_simulation_memory_stays_small():
    stats = [_stats(s, 0.4, seed=i) for i, s in enumerate(("AAA", "BBB", "CCC", "DDD"))]
    tracemalloc.start()
    try:
        _REAL_SIMULATE(stats, fcn_eval.Terms(), 0.04, _RF)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 100 * 1024 * 1024


# ── 5. judge ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "e, level",
    [(0.69, fcn_eval.RED), (0.71, fcn_eval.YELLOW), (0.89, fcn_eval.YELLOW), (0.91, fcn_eval.GREEN)],
)
def test_levels_on_both_sides_of_the_thresholds(e, level):
    rf, fair = 0.04, 0.14
    verdict = fcn_eval.judge(rf + e * (fair - rf), fair, rf, False)
    assert verdict.level == level and verdict.e == pytest.approx(e)
    assert not verdict.below_rf and not verdict.downgraded


@pytest.mark.parametrize("rate", [0.04, 0.03, 0.0])
def test_a_rate_not_above_the_risk_free_rate_is_red(rate):
    verdict = fcn_eval.judge(rate, 0.10, 0.04, False)
    assert (verdict.level, verdict.below_rf, verdict.e) == (fcn_eval.RED, True, 0.0)


def test_e_is_continuous_and_monotone_around_the_minimum_premium():
    rf, rate = 0.04, 0.0447
    edge = rf + fcn_eval.MIN_PREMIUM
    below = fcn_eval.judge(rate, edge - 1e-9, rf, False).e
    above = fcn_eval.judge(rate, edge + 1e-9, rf, False).e
    assert below == pytest.approx(above, rel=1e-5)
    rank = {fcn_eval.GREEN: 2, fcn_eval.YELLOW: 1, fcn_eval.RED: 0}
    previous_e, previous_rank = math.inf, 3
    for i in range(400):
        fair = rf - 0.05 + i * 0.0005  # 跨過 rf＋0.005，含 fair < rf
        verdict = fcn_eval.judge(rate, fair, rf, False)
        assert verdict.e <= previous_e + 1e-12
        assert rank[verdict.level] <= previous_rank
        previous_e, previous_rank = verdict.e, rank[verdict.level]


@pytest.mark.parametrize(
    "rate, fair, rf",
    [
        (0.18, 0.20, None), (0.18, 0.20, math.nan), (0.18, 0.20, math.inf), (0.18, math.nan, 0.04),
        (0.18, math.inf, 0.04), (math.nan, 0.20, 0.04), (0.18, -math.inf, 0.04),
    ],
)
def test_missing_or_non_finite_inputs_are_gray(rate, fair, rf):
    verdict = fcn_eval.judge(rate, fair, rf, True)
    assert verdict.level == fcn_eval.GRAY and math.isnan(verdict.e)
    assert not verdict.downgraded


@pytest.mark.parametrize(
    "rate, before, after, downgraded",
    [
        (0.20, fcn_eval.GREEN, fcn_eval.YELLOW, True),  # E＝1.6
        (0.12, fcn_eval.YELLOW, fcn_eval.RED, True),  # E＝0.8
        (0.08, fcn_eval.RED, fcn_eval.RED, False),  # E＝0.4
        (0.03, fcn_eval.RED, fcn_eval.RED, False),  # 比無風險利率低
    ],
)
def test_a_loss_downgrades_one_level_and_red_stays_red(rate, before, after, downgraded):
    rf, fair = 0.04, 0.14
    assert fcn_eval.judge(rate, fair, rf, False).level == before
    verdict = fcn_eval.judge(rate, fair, rf, True)
    assert (verdict.level, verdict.downgraded) == (after, downgraded)


# ── 6. 卡片 ─────────────────────────────────────────────────────────────────
# 文字用詞會再調整：這裡只鎖穩定的事實（標題／altText 前綴、驗證器、按鈕、數字、
# 不出現使用者原文），整句只在那句話本身就是要守的行為時才比。
def _line(texts: list[str], prefix: str) -> str:
    found = [t for t in texts if t.startswith(prefix)]
    assert len(found) == 1, (prefix, texts)
    return found[0]


def _synthetic_card(req, *, p_convert=0.35, fair=0.30, rf=0.04, verdict=None, corr_note=False, income=None):
    stats = [_stats(s, 0.5, seed=i + 1) for i, s in enumerate(req.symbols)]
    k = len(stats)
    sim = fcn_eval.SimResult(
        fair=fair, p_convert=p_convert, convert_counts=tuple(int(p_convert * 4000) // k for _ in stats),
        breach_counts=tuple(int(p_convert * 4000) // k + 10 for _ in stats), corr_note=corr_note,
    )
    verdict = verdict or fcn_eval.judge(req.rate, fair, rf, False)
    net_income = income if income is not None else {s: (1.0, 2.0, 3.0, 4.0) for s in req.symbols}
    return fcn_eval.build_card(req, stats, sim, verdict, rf, "USD", net_income)


def test_evaluation_card_is_sendable_and_has_the_recalculate_button(market):
    body = "NVDA AMD TSLA 年利率18%"
    reply = fcn_eval.handle(body)
    assert reply.text is None and reply.flex is not None
    _assert_sendable_card(reply.flex, reply.alt_text)
    assert reply.alt_text.startswith("FCN 評估：NVDA、AMD、TSLA｜CP值")
    texts = _card_texts(reply.flex)
    assert texts[0] == "FCN 評估"
    assert texts[2].startswith("輝達 NVDA、超微 AMD、特斯拉 TSLA｜年利率 18%")
    rate_line = _line(texts, "利率：")
    assert "4.04%" in rate_line and "14 個百分點" in rate_line  # 18% − 4.04%
    (action,) = _actions(reply.flex)
    assert action["label"] == "🔄 用今天的資料重算"
    assert action["text"] == fcn_eval.to_command(_strict_ok(body))
    assert _strict_ok(action["text"][len("/FCN"):]).effective() == _strict_ok(body).effective()


def test_confirm_card_is_sendable_and_lists_what_was_read():
    reply = fcn_eval.handle("", quoted_text=BANKER_MESSAGE)
    assert reply.text is None
    _assert_sendable_card(reply.flex, reply.alt_text)
    assert reply.alt_text == "FCN 評估：我讀到 NVDA、AMD、TSLA｜年利率 17.89%，點按鈕評估"
    texts = _card_texts(reply.flex)
    assert _PREFIX in texts[0]
    assert _line(texts, "股票：") == "股票：輝達 NVDA、超微 AMD、特斯拉 TSLA"
    assert _line(texts, "年利率：") == "年利率：17.89%"
    assert _line(texts, "期限：") == "期限：6 個月"
    ko_row, ki_row, strike_row = (_line(texts, p) for p in ("提前出場", "接股門檻", "接股價"))
    assert "期初價" in ko_row and "60%" in ki_row and "80%" in strike_row
    assert not any("預設" in row for row in (ko_row, ki_row, strike_row))
    assert "美元" in _line(texts, "計價：")
    assert any("記憶式" in t for t in texts)
    (action,) = _actions(reply.flex)
    assert action["label"] == "✅ 用這組評估"


def test_confirm_card_marks_defaults_and_unread_numbers():
    reply = fcn_eval.handle("NVDA 年利率12% 價格80% 謝謝")
    _assert_sendable_card(reply.flex, reply.alt_text)
    texts = _card_texts(reply.flex)
    assert "預設" in _line(texts, "期限：")
    for prefix in ("提前出場", "接股門檻", "接股價"):
        assert "預設" in _line(texts, prefix)
    assert "80" in _line(texts, "沒讀懂的數字")
    assert not any(t.startswith("計價：") for t in texts)


def test_free_text_never_reaches_a_card(market):
    sentence = "隔壁陳太太說她買了賺翻天記得幫我留兩盒鳳梨酥"
    grams = {sentence[i:i + 3] for i in range(len(sentence) - 2)}
    replies = [
        fcn_eval.handle(f"NVDA 年利率18% {sentence}"),  # 看不懂的字 → 寬鬆確認卡
        fcn_eval.handle("", quoted_text=f"{BANKER_MESSAGE}\n{sentence}"),
        fcn_eval.handle(f"幫我看 {sentence}", quoted_text=BANKER_MESSAGE),
    ]
    for reply in replies:
        assert reply.flex is not None
        blob = json.dumps(reply.flex, ensure_ascii=False) + reply.alt_text
        assert not [g for g in grams if g in blob]


@pytest.mark.parametrize("symbols", [("NVDA",), ("NVDA", "AMD")])
def test_strike_at_or_above_initial_price_never_says_drop_zero(symbols):
    req = _request(symbols, 0.18, ki=0.7, strike=1.0)
    card, alt = _synthetic_card(req)
    _assert_sendable_card(card, alt)
    texts = _card_texts(card)
    assert not any("跌 0%" in t for t in texts)
    assert any("願意用期初價" in t for t in texts)
    assert not any(t.startswith("如果條件書是用期初價接股") for t in texts)
    below = _card_texts(_synthetic_card(_request(symbols, 0.18, ki=0.7, strike=0.8))[0])
    assert any("願意用跌 20% 的價格" in t for t in below)
    assert any(t.startswith("如果條件書是用期初價接股") for t in below)


def test_a_tiny_conversion_chance_only_says_it_is_low():
    card, alt = _synthetic_card(_request(("NVDA",), 0.10), p_convert=0.004, fair=0.05)
    _assert_sendable_card(card, alt)
    texts = _card_texts(card)
    assert "照近一年的波動，到期被接股的機率很低" in texts
    assert not any("被接股的機率約" in t or "被接" in t and "願意" in t for t in texts)


def test_card_without_a_light_or_below_the_risk_free_rate():
    gray = fcn_eval.judge(0.18, math.nan, 0.04, False)
    card, alt = _synthetic_card(_request(("NVDA", "AMD"), 0.18), verdict=gray, corr_note=True)
    _assert_sendable_card(card, alt)
    assert alt.endswith("｜資料不足")
    texts = _card_texts(card)
    assert "⚪" in _line(texts, "CP值：") and any("先不給燈號" in t for t in texts)
    assert any("當成彼此無關" in t for t in texts)
    below = fcn_eval.judge(0.03, 0.30, 0.04, False)
    card, alt = _synthetic_card(_request(("NVDA",), 0.03), verdict=below)
    _assert_sendable_card(card, alt)
    texts = _card_texts(card)
    assert any("還低" in t and "4%" in t for t in texts)
    assert "低 1 個百分點" in _line(texts, "利率：")
    card, _alt = _synthetic_card(_request(("NVDA",), 0.045))
    assert "多 0.5 個百分點" in _line(_card_texts(card), "利率：")  # 不到 1 個百分點時多一位小數


def test_terms_line_lists_european_knock_in_memory_and_defaults():
    req = _request(("NVDA",), 0.18, ki=0.6, strike=0.8, eki=True, memory_ko=True, ki_given=True, strike_given=True)
    card, alt = _synthetic_card(req)
    _assert_sendable_card(card, alt)
    line = _line(_card_texts(card), "條件：")
    assert line.startswith("條件：到期那天") and "60%" in line and "80%" in line and "40%" in line
    assert "記憶式" in line and "沒寫，照預設" in line and "任一檔" not in line
    basket = _line(_card_texts(_synthetic_card(_request(("NVDA", "AMD"), 0.18))[0]), "條件：")
    assert "任一檔" in basket and "70%" in basket and "沒寫，照預設" in basket


def test_etf_shows_no_earnings_and_its_income_is_never_used(market):
    market.income["SPY"] = (-1.0, -1.0, -1.0, -1.0)  # 就算抓到也不能算進去
    reply = fcn_eval.handle("NVDA SPY 2330 年利率12%")
    _assert_sendable_card(reply.flex, reply.alt_text)
    texts = _card_texts(reply.flex)
    assert any("SPY" in t and "ETF" in t and "不看獲利" in t for t in texts)
    assert any("2 檔近四季都有賺錢" in t for t in texts)
    assert not any("虧損" in t or "降一級" in t for t in texts)
    # 第二次走快取：ETF 的財報連送都不送
    market.income_calls.clear()
    fcn_eval.handle("SPY NVDA 年利率10%")
    assert "SPY" not in market.income_calls


def test_a_loss_is_shown_and_downgrades_only_when_the_level_drops(market):
    market.income["TSLA"] = (2.0e9, -1.0e9, 1.0e9, 3.0e9)
    high = _card_texts(fcn_eval.handle("TSLA 年利率45%").flex)  # 原本 🟢，降成 🟡
    assert "🟡" in _line(high, "CP值：")
    loss = [t for t in high if "近四季有虧損" in t]
    assert loss and all("TSLA" in t for t in loss)
    assert any(t.startswith("公司：") and t.endswith("（降一級）") for t in loss)
    low = _card_texts(fcn_eval.handle("TSLA 年利率5%").flex)  # 本來就 🔴，不能再降
    assert "🔴" in _line(low, "CP值：")
    assert any(t.startswith("公司：") and "近四季有虧損" in t for t in low)
    assert not any("降一級" in t for t in low)


def test_unknown_earnings_do_not_downgrade(market):
    market.income["NVDA"] = None
    texts = _card_texts(fcn_eval.handle("NVDA 年利率45%").flex)
    assert "🟢" in _line(texts, "CP值：")
    assert any("NVDA" in t and "查不到完整的近四季獲利" in t for t in texts)
    assert not any("降一級" in t for t in texts)


def test_most_likely_ticker_on_a_tie_does_not_depend_on_typing_order():
    a, b = _stats("AMD", 0.5, seed=1), _stats("NVDA", 0.5, seed=2)
    sim = fcn_eval.SimResult(fair=0.30, p_convert=0.2, convert_counts=(5, 5), breach_counts=(7, 7), corr_note=False)
    verdict = fcn_eval.judge(0.18, 0.30, 0.04, False)
    lines = []
    for order in ((a, b), (b, a)):
        req = _request(tuple(s.symbol for s in order), 0.18)
        card, _alt = fcn_eval.build_card(req, list(order), sim, verdict, 0.04, "USD", {})
        lines.append(_line(_card_texts(card), "最可能被接的是"))
    assert lines[0] == lines[1]


# ── 7. handle 的分流 ────────────────────────────────────────────────────────
@pytest.fixture
def no_evaluation(monkeypatch):
    """記下被送去評估的 request，不抓資料、不模擬。"""
    seen: list[fcn_eval.FcnRequest] = []

    def _fake(req, deadline):
        seen.append(req)
        return fcn_eval.FcnReply(text="FCN 評估：（測試替身）")

    monkeypatch.setattr(fcn_eval, "_evaluate", _fake)
    return seen


@pytest.mark.parametrize("body", ["", "   ", "幫我看", "FCN 評估 一下", "請問 划算嗎？"])
def test_empty_or_filler_only_body_gets_the_usage_text(body, no_evaluation):
    assert fcn_eval.handle(body).text == fcn_eval.USAGE_TEXT
    assert no_evaluation == []


@pytest.mark.parametrize("body", ["", "幫我看"])
def test_quoting_a_bot_message_gets_the_fixed_sentence(body, no_evaluation):
    # 說明文字裡有範例指令，也不能被拿去評估
    reply = fcn_eval.handle(body, quoted_text=fcn_eval.USAGE_TEXT, quoted_is_bot=True)
    assert reply.text == fcn_eval.TEXT_QUOTED_BOT
    assert no_evaluation == []


@pytest.mark.parametrize(
    "quoted, text",
    [
        ("[圖片]", fcn_eval.TEXT_QUOTED_MEDIA),
        ("[影片]", fcn_eval.TEXT_QUOTED_MEDIA),
        ("", fcn_eval.TEXT_QUOTED_MISSING),
        ("  ", fcn_eval.TEXT_QUOTED_MISSING),
        ("今天天氣很好，晚上吃火鍋", fcn_eval.TEXT_QUOTED_NOTHING),
        ("NVDA 年化12% 年化14%", "FCN 評估：看到兩個不同的利率（12%、14%），請只寫年利率"),
    ],
)
def test_quoted_message_special_cases(quoted, text, no_evaluation):
    assert fcn_eval.handle("幫我看", quoted_text=quoted).text == text
    assert no_evaluation == []


def test_quoted_banker_message_gets_the_confirm_card_not_an_evaluation(no_evaluation):
    reply = fcn_eval.handle("", quoted_text=BANKER_MESSAGE)
    assert reply.flex is not None and reply.alt_text.startswith("FCN 評估：我讀到 ")
    assert no_evaluation == []


def test_a_body_that_parses_strictly_wins_over_the_quote(no_evaluation):
    reply = fcn_eval.handle("AMD 年利率12%", quoted_text=BANKER_MESSAGE)
    assert reply.text == "FCN 評估：（測試替身）"
    (req,) = no_evaluation
    assert req.symbols == ("AMD",) and req.rate == pytest.approx(0.12)


@pytest.mark.parametrize(
    "body, text",
    [
        ("NVDA 年利率12% 年利率14%", "FCN 評估：看到兩個不同的利率（12%、14%），請只寫年利率"),
        ("NVDA AMD TSLA AAPL MSFT 年利率12%", "FCN 評估：FCN 最多 4 檔，這裡有 5 檔"),
        ("NVDA 年利率12% EUR計價", fcn_eval.TEXT_UNSUPPORTED_CURRENCY),
        ("NVDA 年利率12% KI70% 執行價60%", "FCN 評估：條件看起來不對（執行價比 KI 低），請確認後再打"),
    ],
)
def test_strict_conflicts_ask_the_specific_question_even_with_a_quote(body, text, no_evaluation):
    assert fcn_eval.handle(body, quoted_text=BANKER_MESSAGE).text == text
    assert fcn_eval.handle(body).text == text
    assert no_evaluation == []


def test_unread_words_fall_back_to_a_lenient_confirm_card(no_evaluation):
    reply = fcn_eval.handle("NVDA 年利率18% 謝謝")
    assert reply.alt_text == "FCN 評估：我讀到 NVDA｜年利率 18%，點按鈕評估"
    assert no_evaluation == []
    # 只缺利率：body＋引用一起讀
    reply = fcn_eval.handle("NVDA", quoted_text="理專說年化 12%")
    assert reply.alt_text == "FCN 評估：我讀到 NVDA｜年利率 12%，點按鈕評估"
    # 讀不出來就回嚴格模式的那句
    assert fcn_eval.handle("謝謝 你").text.startswith("FCN 評估：有看不懂的字（謝謝、你）")


def test_length_limits_apply_after_nfkc(no_evaluation):
    assert len("㊿" * 1001) <= fcn_eval.BODY_MAX_CHARS  # 原文不長，NFKC 後才超過
    assert fcn_eval.handle("㊿" * 1001).text == fcn_eval.TEXT_TOO_LONG
    assert fcn_eval.handle("幫" * 2001).text == fcn_eval.TEXT_TOO_LONG
    assert fcn_eval.handle("幫" * 2000).text != fcn_eval.TEXT_TOO_LONG
    assert fcn_eval.handle("", quoted_text="㊿" * 1501).text == fcn_eval.TEXT_TOO_LONG
    assert fcn_eval.handle("", quoted_text="理" * 3001).text == fcn_eval.TEXT_TOO_LONG
    assert fcn_eval.handle("", quoted_text="理" * 3000).text == fcn_eval.TEXT_QUOTED_NOTHING
    assert no_evaluation == []


def test_a_spent_reply_deadline_answers_without_fetching(market):
    reply = fcn_eval.handle("NVDA 年利率18%", deadline=time.monotonic() + 1.0)
    assert reply.text == fcn_eval.TEXT_UNAVAILABLE
    assert market.chart_calls == [] and market.income_calls == [] and market.rf_calls == 0
    assert fcn_eval._cooldown_until == 0.0  # 沒抓就沒有逾時，不冷卻
    assert fcn_eval.handle("NVDA 年利率18%", deadline=time.monotonic() + 20).flex is not None


_FIXED_TEXTS = [
    "USAGE_TEXT", "TEXT_TOO_LONG", "TEXT_UNAVAILABLE", "TEXT_QUOTED_BOT", "TEXT_QUOTED_MEDIA",
    "TEXT_QUOTED_MISSING", "TEXT_QUOTED_NOTHING", "TEXT_NO_RATE", "TEXT_NO_SYMBOL",
    "TEXT_UNSUPPORTED_CURRENCY", "TEXT_NO_USD_RF",
]


@pytest.mark.parametrize("name", _FIXED_TEXTS)
def test_fixed_texts_survive_the_outbound_pipeline(name):
    text = getattr(fcn_eval, name)
    assert text.startswith("FCN 評估：")
    _assert_text_survives_outbound(text)
    assert not main._is_system_status_outbound(text)
    assert main._is_market_quote_outbound(text)  # main 據此只用 reply token、不 push


def test_generated_problem_texts_survive_the_outbound_pipeline(no_evaluation):
    bodies = [
        "NVDA 每月1.5% 年利率20%", "NVDA 年利率15~18%", "NVDA 年利率61%", "NVDA 年利率18% KO80% KI80%",
        "NVDA 年利率18% KO95% KO100%", "NVDA 年利率18% 112個月", "NVDA 年利率18% 6個月 12個月",
        "NVDA 年利率18% 美元 台幣", "NVDA AMD TSLA AAPL MSFT 年利率18%", "^GSPC 年利率18%", "謝謝 @all",
    ]
    for body in bodies:
        reply = fcn_eval.handle(body)
        assert reply.flex is None and reply.text.startswith("FCN 評估：")
        _assert_text_survives_outbound(reply.text)
    for symbol, problem in [("NVDA", "type"), ("NVDA", "currency"), ("NVDA", "stale"), ("2330", "missing"), ("x/../y", "missing")]:
        _assert_text_survives_outbound(fcn_eval._symbol_problem_text(symbol, problem))


# ── 8. 抓資料、時限、快取 ─────────────────────────────────────────────────────
def test_stuck_price_fetch_gives_up_within_budget_then_cools_down(market, fresh_pools, monkeypatch):
    release = fresh_pools

    def stuck_chart(symbol, range_value="1y"):
        market.chart_calls.append(symbol)
        release.wait(10)  # teardown 會 set；最多等 10 秒，不會永遠卡住
        return market.charts.get(symbol)

    monkeypatch.setattr(fcn_eval, "_fetch_chart", stuck_chart)
    started = time.monotonic()
    reply = fcn_eval.handle("NVDA 年利率18%")
    elapsed = time.monotonic() - started
    assert reply.text == fcn_eval.TEXT_UNAVAILABLE and reply.flex is None
    assert 0.15 <= elapsed < 1.5
    assert fcn_eval._cooldown_until > time.monotonic() + 50
    calls = (list(market.chart_calls), list(market.income_calls), market.rf_calls)
    monkeypatch.setattr(fcn_eval, "DATA_BUDGET_S", 5.0)  # 沒有冷卻的話這次會卡滿 5 秒
    started = time.monotonic()
    again = fcn_eval.handle("AMD 2330 年利率12%")
    assert again.text == fcn_eval.TEXT_UNAVAILABLE
    assert time.monotonic() - started < 1.0
    assert (market.chart_calls, market.income_calls, market.rf_calls) == calls  # 冷卻中不送任何工作


def test_full_price_queue_answers_unavailable_without_fetching(market, fresh_pools, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    monkeypatch.setattr(fcn_eval, "_PRICE_SLOTS", slots)
    try:
        reply = fcn_eval.handle("NVDA 年利率18%")
    finally:
        slots.release()
    assert reply.text == fcn_eval.TEXT_UNAVAILABLE
    assert market.chart_calls == [] and market.income_calls == []


def test_stuck_income_fetch_still_gives_a_card_without_downgrading(market, fresh_pools, monkeypatch):
    release = fresh_pools

    def stuck_income(symbol):
        market.income_calls.append(symbol)
        release.wait(10)
        return (-1.0, -1.0, -1.0, -1.0)

    monkeypatch.setattr(fcn_eval, "_fetch_net_income", stuck_income)
    started = time.monotonic()
    reply = fcn_eval.handle("NVDA AMD 年利率45%")
    assert time.monotonic() - started < 3.0
    assert reply.flex is not None
    texts = _card_texts(reply.flex)
    assert any("NVDA" in t and "AMD" in t and "查不到完整的近四季獲利" in t for t in texts)
    assert not any("虧損" in t or "降一級" in t for t in texts)
    assert fcn_eval._cooldown_until == 0.0  # 財報慢不算價格逾時


def test_a_failing_price_fetch_is_unavailable_but_does_not_cool_down(market, monkeypatch):
    def broken(symbol, range_value="1y"):
        raise RuntimeError("boom")

    monkeypatch.setattr(fcn_eval, "_fetch_chart", broken)
    assert fcn_eval.handle("NVDA 年利率18%").text == fcn_eval.TEXT_UNAVAILABLE
    assert fcn_eval._cooldown_until == 0.0


@pytest.mark.parametrize("chart", [None, {}, {"chart": {"result": []}}, "short"])
def test_a_symbol_without_usable_prices_is_named_alone(market, chart):
    market.charts["ZZZZ"] = _chart_json(n=50) if chart == "short" else chart
    reply = fcn_eval.handle("NVDA zzzz 年利率18%")
    assert reply.flex is None
    assert reply.text == "FCN 評估：暫時拿不到「ZZZZ」的股價資料，請確認代號或稍後再試"


def test_a_bare_taiwan_code_falls_back_from_tw_to_two(market):
    market.charts["6488.TWO"] = _chart_json(vol=0.4, seed=8, currency="TWD", tz="Asia/Taipei")
    reply = fcn_eval.handle("6488 年利率12%")
    assert reply.flex is not None
    assert market.chart_calls == ["6488.TW", "6488.TWO"]
    assert market.income_calls == ["6488.TWO"]  # 財報用解析後的 Yahoo 代號
    texts = _card_texts(reply.flex)
    assert any(t.startswith("環球晶 6488｜年利率 12%") and "台幣" in t for t in texts)
    assert any("台銀一年期定存" in t for t in texts)
    unknown = fcn_eval.handle("9999 年利率12%")
    assert unknown.text == "FCN 評估：暫時拿不到「9999」的股價資料，請確認代號或稍後再試"
    assert market.chart_calls[-2:] == ["9999.TW", "9999.TWO"]


@pytest.mark.parametrize("instrument", ["INDEX", "CRYPTOCURRENCY", "MUTUALFUND", "FUTURE"])
def test_non_stock_instruments_are_refused(market, instrument):
    market.charts["VIX"] = _chart_json(instrument=instrument)
    assert fcn_eval.handle("VIX 年利率18%").text == "FCN 評估：VIX 不是股票或 ETF"


def test_unsupported_listing_currency_and_stale_prices(market):
    market.charts["SAP"] = _chart_json(currency="EUR")
    assert fcn_eval.handle("SAP 年利率18%").text == "FCN 評估：目前只支援台股和美股（SAP）"
    market.charts["NVDA"] = _chart_json(end=datetime.now(_TPE).date() - timedelta(days=30))
    assert fcn_eval.handle("NVDA 年利率18%").text == "FCN 評估：NVDA 的股價資料太舊，請稍後再試"


def test_a_second_request_uses_the_caches(market, monkeypatch):
    runs = []

    def counting_simulate(*args, **kwargs):
        runs.append(args[1])
        return _REAL_SIMULATE(*args, **kwargs)

    monkeypatch.setattr(fcn_eval, "simulate", counting_simulate)
    first = fcn_eval.handle("NVDA 2330 年利率12%")
    assert first.flex is not None
    calls = (list(market.chart_calls), list(market.income_calls), market.rf_calls)
    second = fcn_eval.handle("2330 NVDA 年利率15% 12個月")
    assert second.flex is not None
    assert (market.chart_calls, market.income_calls, market.rf_calls) == calls  # 價格、財報、利率都不重抓
    assert len(runs) == 2
    again = fcn_eval.handle("NVDA 2330 年利率12%")
    assert len(runs) == 2  # 同一組條件與資料：模擬結果也重用
    assert again.flex == first.flex
    fcn_eval.clear_caches()
    fcn_eval.handle("NVDA 2330 年利率12%")
    assert len(runs) == 3


@pytest.mark.parametrize("body", ["NVDA 年利率18%", "2330 年利率8% 美元", "NVDA 2330 年利率12% 台幣"])
def test_a_missing_us_rate_stops_any_us_evaluation(market, body):
    market.rf = None
    assert fcn_eval.handle(body).text == fcn_eval.TEXT_NO_USD_RF


def test_a_failing_us_rate_fetch_is_not_replaced_by_the_twd_rate(market, monkeypatch):
    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(fcn_eval, "_fetch_usd_rf", broken)
    assert fcn_eval.handle("NVDA 年利率18%").text == fcn_eval.TEXT_NO_USD_RF
    assert fcn_eval._RF_CACHE == {}  # 失敗不快取，下次重抓


def test_a_taiwan_dollar_note_on_taiwan_stocks_does_not_need_the_us_rate(market):
    market.rf = None
    reply = fcn_eval.handle("2330 年利率8%")
    assert reply.flex is not None
    texts = _card_texts(reply.flex)
    rf = fcn_eval._fmt_pct(fcn_eval.TWD_RF_DEFAULT)
    assert any(t.startswith(f"利率：年利率 8%，比台銀一年期定存（{rf}%）多") for t in texts)


# 抓資料函式本身（替身底下的那一層）
class _Response:
    def __init__(self, status, payload=None, bad_json=False):
        self.status_code = status
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


def test_http_get_json_tries_both_fixed_hosts(monkeypatch):
    seen = []

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.append((url, timeout))
        if "query1" in url:
            return _Response(503)
        return _Response(200, {"ok": True})

    monkeypatch.setattr(fcn_eval.requests, "get", fake_get)
    assert _REAL_HTTP_GET_JSON("/v8/finance/chart/NVDA", {"range": "1y"}) == {"ok": True}
    assert seen == [
        ("https://query1.finance.yahoo.com/v8/finance/chart/NVDA", fcn_eval.HTTP_TIMEOUT_S),
        ("https://query2.finance.yahoo.com/v8/finance/chart/NVDA", fcn_eval.HTTP_TIMEOUT_S),
    ]


@pytest.mark.parametrize(
    "responses",
    [
        ["raise", "raise"],
        [_Response(500), _Response(404)],
        [_Response(200, bad_json=True), _Response(200, [1, 2])],
    ],
)
def test_http_get_json_returns_none_when_nothing_usable(monkeypatch, responses):
    queue = list(responses)

    def fake_get(url, params=None, headers=None, timeout=None):
        item = queue.pop(0)
        if item == "raise":
            raise fcn_eval.requests.ConnectionError("down")
        return item

    monkeypatch.setattr(fcn_eval.requests, "get", fake_get)
    assert _REAL_HTTP_GET_JSON("/x", {}) is None
    assert queue == []


def test_fetch_chart_only_requests_safe_symbols(monkeypatch):
    seen = []
    monkeypatch.setattr(fcn_eval, "_http_get_json", lambda path, params: seen.append((path, params)) or {})
    for bad in ["NVDA; rm -rf /", "../../etc", "^GSPC", "ES=F", "nvda", "", "A" * 7]:
        assert _REAL_FETCH_CHART(bad) is None
    assert seen == []
    for good in ["NVDA", "BRK-B", "2330.TW", "6488.TWO", "00632R.TW"]:
        _REAL_FETCH_CHART(good)
    _REAL_FETCH_CHART("^IRX", range_value="5d")
    assert [path for path, _ in seen] == [
        "/v8/finance/chart/NVDA", "/v8/finance/chart/BRK-B", "/v8/finance/chart/2330.TW",
        "/v8/finance/chart/6488.TWO", "/v8/finance/chart/00632R.TW", "/v8/finance/chart/%5EIRX",
    ]
    assert seen[0][1]["events"] == "div" and seen[-1][1]["range"] == "5d"


def _timeseries(rows):
    return {"timeseries": {"result": [{"quarterlyNetIncome": [
        None if row is None else {"asOfDate": row[0], "reportedValue": {"raw": row[1]}} for row in rows
    ]}]}}


def test_fetch_net_income_keeps_the_latest_four_complete_quarters(monkeypatch):
    rows = [("2025-12-31", 4.0), ("2025-03-31", 1.0), ("2026-06-30", 6.0), ("2025-06-30", 2.0),
            ("2026-03-31", 5.0), ("2025-09-30", float("nan")), None]
    monkeypatch.setattr(fcn_eval, "_http_get_json", lambda path, params: _timeseries(rows))
    assert _REAL_FETCH_NET_INCOME("NVDA") == (1.0, 2.0, 4.0, 5.0, 6.0)[-4:]
    monkeypatch.setattr(fcn_eval, "_http_get_json", lambda path, params: _timeseries(rows[:3]))
    assert _REAL_FETCH_NET_INCOME("NVDA") is None  # 不滿四季
    monkeypatch.setattr(fcn_eval, "_http_get_json", lambda path, params: {"timeseries": {}})
    assert _REAL_FETCH_NET_INCOME("NVDA") is None
    monkeypatch.setattr(fcn_eval, "_http_get_json", lambda path, params: None)
    assert _REAL_FETCH_NET_INCOME("NVDA") is None
    assert _REAL_FETCH_NET_INCOME("../x") is None


@pytest.mark.parametrize(
    "closes, rf",
    [([4.0, None, 4.04], 0.0404), ([4.1, math.nan], 0.041), ([25.0], None), ([], None), ([-1.0], None), (None, None)],
)
def test_usd_risk_free_rate_is_validated(monkeypatch, closes, rf):
    data = None if closes is None else {"chart": {"result": [{"indicators": {"quote": [{"close": closes}]}}]}}
    monkeypatch.setattr(fcn_eval, "_fetch_chart", lambda symbol, range_value="1y": data)
    result = _REAL_FETCH_USD_RF()
    assert result == (pytest.approx(rf) if rf is not None else None)


@pytest.mark.parametrize(
    "raw, rf",
    [("", fcn_eval.TWD_RF_DEFAULT), ("0.02", 0.02), ("abc", fcn_eval.TWD_RF_DEFAULT),
     ("0.5", fcn_eval.TWD_RF_DEFAULT), ("-0.01", fcn_eval.TWD_RF_DEFAULT)],
)
def test_twd_rate_override_is_validated(monkeypatch, raw, rf):
    monkeypatch.setenv("LINE_BOT_FCN_TWD_RF", raw)
    assert fcn_eval._twd_rf() == pytest.approx(rf)


@pytest.mark.parametrize(
    "data, problem",
    [
        (None, "missing"), ({}, "missing"), ({"chart": {"result": []}}, "missing"),
        (_chart_json(instrument="INDEX"), "type"), (_chart_json(instrument="CRYPTOCURRENCY"), "type"),
        (_chart_json(currency="EUR"), "currency"), (_chart_json(n=60), "short"),
        (_chart_json(end=datetime.now(_TPE).date() - timedelta(days=20)), "stale"),
    ],
)
def test_parse_chart_problems(data, problem):
    assert fcn_eval._parse_chart("NVDA", data) == (None, problem)


def test_parse_chart_statistics():
    rng = np.random.default_rng(3)
    calm = rng.normal(0, 0.10 / math.sqrt(252), 200)
    wild = rng.normal(0, 0.80 / math.sqrt(252), 63)
    adj = 50.0 * np.exp(np.concatenate([[0.0], np.cumsum(np.concatenate([calm, wild]))]))
    close = adj * 1.1  # 未調整收盤比較高：spot 用它、報酬用調整後
    close_list, adj_list = close.tolist(), adj.tolist()
    close_list[10] = None  # 壞值整天略過
    adj_list[20] = -1.0
    data = _chart_json(n=len(adj), closes=close_list, adjclose=adj_list, dividends=[(150, 1.0), (250, 0.5)])
    stats, problem = fcn_eval._parse_chart("NVDA", data)
    assert problem == "" and stats.market == "US" and stats.currency == "USD"
    assert len(stats.dates) == len(adj) - 2
    kept = np.delete(adj, [10, 20])
    rets = np.diff(np.log(kept))
    expected = max(np.std(rets, ddof=1), np.std(rets[-63:], ddof=1)) * math.sqrt(252)
    assert stats.vol == pytest.approx(expected) and stats.vol > 0.5  # 近三個月比較大，取大的
    assert stats.spot == pytest.approx(close[-1])
    assert np.allclose(stats.closes, kept)
    assert stats.div_yield == pytest.approx(1.5 / close[-1])
    tw, problem = fcn_eval._parse_chart("2330.TW", _chart_json(currency="TWD", tz="Asia/Taipei"))
    assert problem == "" and tw.market == "TW" and tw.instrument_type == "EQUITY"


def test_parse_chart_caps_dividends_and_ignores_old_ones():
    data = _chart_json(n=400, dividends=[(0, 50.0)])  # 一年多以前的配息不算
    stats, _ = fcn_eval._parse_chart("NVDA", data)
    assert stats.div_yield == 0.0
    data = _chart_json(n=300, dividends=[(299, 50.0)])
    stats, _ = fcn_eval._parse_chart("NVDA", data)
    assert stats.div_yield == fcn_eval.DIV_CAP


# ── 9. 正規表示式的耗時（ReDoS） ─────────────────────────────────────────────
def _collect_patterns() -> list[tuple[str, re.Pattern]]:
    found: list[tuple[str, re.Pattern]] = []

    def collect(obj, name):
        if isinstance(obj, re.Pattern):
            found.append((name, obj))
        elif isinstance(obj, (tuple, list)):
            for i, item in enumerate(obj):
                collect(item, f"{name}[{i}]")

    for name, value in sorted(vars(fcn_eval).items()):
        collect(value, name)
    return found


_PATTERNS = _collect_patterns()


class _Deadline(Exception):
    pass


def _seconds(check, text) -> float:
    """``check(text)`` 花多久；卡住的話 5 秒後失敗，不會讓整個測試停住。"""

    def expire(*_):
        raise _Deadline(repr(text[:12]))

    previous = signal.signal(signal.SIGALRM, expire)
    outer = signal.setitimer(signal.ITIMER_REAL, 5)
    started = time.perf_counter()
    try:
        check(text)
        return time.perf_counter() - started
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if outer[0]:
            left = max(outer[0] - (time.perf_counter() - started), 0.001)
            signal.setitimer(signal.ITIMER_REAL, left, outer[1])


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


_SHAPES = {
    "digit run": lambda n: "1" * n,
    "digits then %": lambda n: "1" * (n - 1) + "%",
    "decimal points": lambda n: "1." * (n // 2),
    "thousands commas": lambda n: "1," * (n // 2),
    "circled 50 (NFKC)": lambda n: _nfkc("㊿" * (n // 2)),
    "circled 50 with %": lambda n: _nfkc("㊿%" * (n // 3)),
    "KO + 觸發價 run": lambda n: "KO" + "觸發價" * ((n - 3) // 3) + "%",
    "KO + 價格 run": lambda n: "KO" + "價格" * (n // 2 - 2) + "1%",
    "跌 run": lambda n: "跌" * (n - 3) + "30%",
    "跌破期初 run": lambda n: "跌破期初" * (n // 4),
    "spaces before %": lambda n: "1" + " " * (n - 2) + "%",
    "percent signs": lambda n: "%" * n,
    "label + spaces": lambda n: ("年利率1" + " " * 3) * (n // 7),
    "repeated labels": lambda n: "年利率" * (n // 3),
    "KI run": lambda n: "KI" * (n // 2),
    "capital letters": lambda n: "A" * n,
    "dotted tickers": lambda n: "AB." * (n // 3),
    "monthly words": lambda n: "每月" * (n // 2),
    "dashes then %": lambda n: "-" * (n - 2) + "1%",
    "range separators": lambda n: "1~" * (n // 2 - 1) + "1%",
    "date-like slashes": lambda n: "1/" * (n // 2),
    "time-like colons": lambda n: "1:" * (n // 2),
    "phone-like": lambda n: "02-" * (n // 3),
    "TEL then digits": lambda n: "TEL" * ((n - 100) // 3) + "1" * 100,
    "email-like": lambda n: "a@" * (n // 2),
    "url": lambda n: "http://" + "a" * (n - 7),
    "brackets": lambda n: "[" * n,
    "year runs": lambda n: "1年" * (n // 2),
    "month words": lambda n: "個月" * (n // 2),
    "tenor letters": lambda n: "1M" * (n // 2),
    "long tenor digits": lambda n: "1234個月" * (n // 6),
    "chinese numerals": lambda n: "十二" * (n // 2 - 2) + "個月",
    "chinese years": lambda n: "一二三年" * (n // 4),
    "barrier words": lambda n: "KO Barrier " * (n // 11),
    "currency then amount": lambda n: "USD 1" * (n // 5),
    "amount labels": lambda n: "最低申購" * (n // 4),
    "names": lambda n: "台積電" * (n // 3),
}


def test_the_regex_scan_sees_every_parsing_pattern():
    names = {name.split("[")[0] for name, _ in _PATTERNS}
    assert {
        "_NAME_RE", "_NOISE_RES", "_CUR_STRONG_RES", "_CUR_WEAK_RES", "_CUR_ANY_RE", "_MONTHLY_RE",
        "_MONTHLY_SUFFIX_RE", "_ANNUAL_RE", "_PA_RE", "_KO_RE", "_KI_RE", "_STRIKE_RE", "_DROP_RE",
        "_PRICE_RES", "_TENOR_RES", "_BAD_TENOR_RE", "_RANGE_RE", "_NEGATIVE_PCT_RE", "_LEFTOVER_PCT_RE",
        "_FILLER_RE", "_TOKEN_RE", "_CONNECTOR_RE", "_TW_CODE_SCAN_RE", "_US_SCAN_RE", "_ASCII_NAME_SCAN_RE",
        "_LEFTOVER_TENOR_RE", "_KO_CONTEXT_RE", "_EKI_WORD_RE", "_MEMORY_RE",
    } <= names
    assert len(_PATTERNS) >= 50


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_every_pattern_runs_in_linear_time_on_adversarial_text(shape):
    text = _SHAPES[shape](5000)
    assert len(text) >= 4900
    slow = []
    for name, pattern in _PATTERNS:
        seconds = min(_seconds(lambda s: list(pattern.finditer(s)), text) for _ in range(3))
        if seconds >= 0.05:
            slow.append((name, round(seconds * 1000, 1)))
    assert slow == []


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_handle_answers_adversarial_text_quickly(shape, no_evaluation):
    body, quote = _SHAPES[shape](2000), _SHAPES[shape](3000)
    assert _seconds(fcn_eval.handle, body) < 0.5
    assert _seconds(lambda q: fcn_eval.handle("", quoted_text=q), quote) < 0.5
    assert _seconds(lambda b: fcn_eval.handle(b + " 幫我看", quoted_text=quote), body[:1990]) < 0.5


# ── 10. 契約：fcn_eval 借用 stock_quote 的兩張表 ─────────────────────────────
def test_stock_quote_tables_that_fcn_eval_borrows():
    assert isinstance(stock_quote._TW_NAME_MAP, dict) and stock_quote._TW_NAME_MAP
    assert all(isinstance(k, str) and re.fullmatch(r"\d{4,6}[A-Z]?", v) for k, v in stock_quote._TW_NAME_MAP.items())
    assert isinstance(stock_quote._US_TICKERS, (set, frozenset)) and stock_quote._US_TICKERS
    assert all(isinstance(t, str) for t in stock_quote._US_TICKERS)
    # 真的有被併進來：stock_quote 的名稱與代號、本模組的補充都在
    assert fcn_eval._CJK_NAMES["台積電"] == "2330" and fcn_eval._CJK_NAMES["南亞科"] == "2408"
    assert {t.replace(".", "-") for t in stock_quote._US_TICKERS} <= fcn_eval._US_WHITELIST


# ── 2026-10-10 Codex 最終複核補的案例 ───────────────────────────────────────
@pytest.mark.parametrize("body", ["NVDA 年利率12% 6.5個月", "NVDA 年利率12% 120年", "NVDA 年利率12% -6個月"])
def test_decimal_negative_or_huge_tenors_are_questioned(body):
    problem = _strict_problem(body)
    assert problem.kind == "conflict" and "期限" in problem.text


def test_lockout_is_read_and_survives_the_canonical_command():
    req = _strict_ok("NVDA 年利率12% 6個月 鎖定期3個月")
    assert req.terms.lockout_months == 3
    back = _strict_ok(fcn_eval.to_command(req)[len("/FCN"):])
    assert back.effective() == req.effective()
    assert _strict_problem("NVDA 年利率12% 3個月 鎖定期3個月").kind == "conflict"


def test_lockout_delays_early_termination_in_the_model():
    stats = [_stats("NVDA", vol=0.45, seed=3)]
    plain = fcn_eval.simulate(stats, fcn_eval.Terms(months=6), 0.04, {"US": 0.04}, n_paths=4000)
    locked = fcn_eval.simulate(stats, fcn_eval.Terms(months=6, lockout_months=3), 0.04, {"US": 0.04}, n_paths=4000)
    assert locked.fair != pytest.approx(plain.fair, abs=1e-9)


def test_roc_dates_are_noise_not_tenors():
    req, problem, _unread = fcn_eval.extract_lenient("NVDA 年化12% 到期日 民國115年4月22日")
    assert problem is None and req.terms.months == 6 and not req.terms.months_given


def test_taiwan_only_twd_evaluation_does_not_need_the_usd_rate(monkeypatch):
    def _no_usd():
        raise AssertionError("USD rate must not be fetched for a TWD, Taiwan-only FCN")

    monkeypatch.setattr(fcn_eval, "_fetch_usd_rf", _no_usd)
    monkeypatch.setattr(fcn_eval, "_fetch_chart", lambda symbol, range_value="1y": _chart_json(
        currency="TWD", tz="Asia/Taipei"))
    monkeypatch.setattr(fcn_eval, "_fetch_net_income", lambda symbol: (1.0, 2.0, 3.0, 4.0))
    reply = fcn_eval.handle("2330 年利率12% 台幣")
    assert reply.flex is not None, reply.text
