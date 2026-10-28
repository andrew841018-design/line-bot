"""/股票 美股第 3 段的路由（2026-10-28）：stock_picks.handle 的 hook 把哪些字交給美股模組、美股壞了台股照常。

美股模組自己的行為在 test_stock_picks_us.py；這裡只驗「交給誰、壞了怎麼退」。不連網（conftest 擋）。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import date

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import stock_picks as sp  # noqa: E402
import stock_picks_us as us  # noqa: E402

TW_CARDS = sp.Reply(flex=sp.carousel([sp._bubble("股票推薦：台股", None, [sp._text("台股卡")], [sp._text("只供參考")])]),
                    alt_text="股票推薦：只供參考｜台積電｜資料到 10/27")


@pytest.fixture()
def tw_snapshot(monkeypatch):
    """最小的台股快照：只有名稱索引（GIS-KY、AES-KY 和美股代號撞名）；單檔查詢在這裡都回認不出（listed 是空的）。"""
    snap = sp.Snapshot(
        t=date(2026, 10, 27), calendar=(), computed_at=time.time(), market=None, universe=(), picks=(), pool={},
        listed={}, medians={}, otc={}, missed=None,
        index=sp.name_index({"台積電": "2330", "GIS-KY": "6456", "AES-KY": "6781"}),
    )
    monkeypatch.setattr(sp, "_snapshot", snap)
    return snap


@pytest.fixture()
def seen(monkeypatch):
    calls: list[tuple] = []

    def fake_symbol(symbol, *, deadline=None):
        calls.append(("symbol", symbol))
        return sp.Reply(text=f"US:{symbol}")

    def fake_us():
        calls.append(("us",))
        return sp.Reply(text="US-CARDS")

    def fake_perf():
        calls.append(("perf",))
        return sp.Reply(text="PERF")

    monkeypatch.setattr(us, "handle_symbol", fake_symbol)
    monkeypatch.setattr(us, "us_reply", fake_us)
    monkeypatch.setattr(us, "perf_reply", fake_perf)
    return calls


@pytest.mark.parametrize(
    "body, symbol",
    [
        ("NVDA", "NVDA"), ("nvda", "NVDA"), ("nvda嗎", "NVDA"), ("NVDA？", "NVDA"), ("NVDA 現在可以買嗎", "NVDA"),
        ("BRK.B", "BRK.B"), ("brk-b", "BRK.B"), ("BRK/B", "BRK.B"), ("ＮＶＤＡ", "NVDA"),
        ("美股 GIS", "GIS"), ("美股GIS", "GIS"), ("美股 AI", "AI"),  # 強制美股：撞名、保留字都照查
    ],
)
def test_us_tickers_go_to_the_us_module(tw_snapshot, seen, body, symbol):
    assert sp.handle(body).text == f"US:{symbol}"
    assert seen == [("symbol", symbol)]


@pytest.mark.parametrize("body", ["GIS", "aes", "2330 NVDA", "NVDA 2330", "台積電", "台積電 NVDA"])
def test_taiwan_wins_whenever_any_part_is_taiwanese(tw_snapshot, seen, body):
    reply = sp.handle(body)
    assert seen == [] and reply.text == sp.TEXT_NOT_FOUND  # 這個最小快照的單檔都查不到；重點是沒交給美股


@pytest.mark.parametrize(
    "body, text",
    [
        ("S&P", us.TEXT_US_NOT_FOUND), ("J&J", us.TEXT_US_NOT_FOUND), ("P&G", us.TEXT_US_NOT_FOUND),
        ("AT&T", us.TEXT_US_NOT_FOUND), ("美股 S&P", us.TEXT_US_NOT_FOUND),
        ("AI", us.TEXT_RESERVED), ("ETF", us.TEXT_RESERVED), ("help", us.TEXT_RESERVED),
    ],
)
def test_rejected_and_reserved_words_get_fixed_sentences(tw_snapshot, seen, body, text):
    assert sp.handle(body).text == text and seen == []


@pytest.mark.parametrize(
    "body, call",
    [("美股", ("us",)), ("美股推薦", ("us",)), ("美股 推薦", ("us",)), ("成績", ("perf",)), ("推薦成績", ("perf",)),
     ("推薦 成績？", ("perf",))],
)
def test_us_and_performance_commands(tw_snapshot, seen, body, call):
    sp.handle(body)
    assert seen == [call]


def test_without_a_tw_snapshot_only_plain_us_tickers_and_commands_work(monkeypatch, seen):
    monkeypatch.setattr(sp, "_snapshot", None)
    assert sp.handle("NVDA").text == "US:NVDA"
    assert sp.handle("美股").text == "US-CARDS" and sp.handle("成績").text == "PERF"
    for body in ("GIS", "gis-ky", "TPK", "gis?", "GIS嗎", "台積電", "2330", "NVDA 2330"):
        assert sp.handle(body).text == sp.TEXT_NOT_READY, body  # 認不得台股名稱時，純英文的台股簡稱不送美股
    assert seen == [("symbol", "NVDA"), ("us",), ("perf",)]


def test_picks_carousel_gets_the_us_cards(tw_snapshot, monkeypatch):
    monkeypatch.setattr(sp, "_picks_reply", lambda snapshot: TW_CARDS)
    extended = sp.Reply(flex=TW_CARDS.flex, alt_text=TW_CARDS.alt_text + "｜美股：NVDA")
    monkeypatch.setattr(us, "extend_picks_reply", lambda reply: extended if reply is TW_CARDS else None)
    assert sp.handle("") is extended and sp.handle("推薦") is extended


def test_us_failures_never_break_the_tw_cards(tw_snapshot, monkeypatch):
    monkeypatch.setattr(sp, "_picks_reply", lambda snapshot: TW_CARDS)

    def broken(*_args, **_kwargs):
        raise RuntimeError("us broke")

    monkeypatch.setattr(us, "extend_picks_reply", broken)
    monkeypatch.setattr(us, "handle_symbol", broken)
    monkeypatch.setattr(us, "us_reply", broken)
    assert sp.handle("") is TW_CARDS
    assert sp.handle("NVDA").text == sp.TEXT_US_FAILED
    assert sp.handle("美股").text == sp.TEXT_US_CARDS_FAILED


def test_network_guard_is_not_swallowed(tw_snapshot, monkeypatch):
    """conftest 的擋網守門丟 AssertionError：hook 要讓它往上丟（測試失敗），不能變成固定句。"""

    def guard(*_args, **_kwargs):
        raise AssertionError("network")

    monkeypatch.setattr(us, "handle_symbol", guard)
    with pytest.raises(AssertionError):
        sp.handle("NVDA")


def test_us_module_that_cannot_import_falls_back_to_the_old_sentence(tw_snapshot, monkeypatch):
    monkeypatch.setattr(sp, "_picks_reply", lambda snapshot: TW_CARDS)
    monkeypatch.setitem(sys.modules, "stock_picks_us", None)  # import stock_picks_us → ImportError
    assert sp.handle("NVDA").text == sp.TEXT_US
    assert sp.handle("") is TW_CARDS
    assert sp.handle("美股").text == sp.TEXT_US_CARDS_FAILED
    assert sp.handle("台積電").text == sp.TEXT_NOT_FOUND


def test_flag_off_keeps_the_old_behaviour(tw_snapshot, monkeypatch):
    monkeypatch.setattr(us, "ENABLED", False)
    monkeypatch.setattr(sp, "_picks_reply", lambda snapshot: TW_CARDS)
    assert sp.handle("NVDA").text == sp.TEXT_US
    assert sp.handle("") is TW_CARDS
    assert sp.handle("美股").text == us.TEXT_PHASE3_OFF and sp.handle("成績").text == us.TEXT_PHASE3_OFF
    for body in ("AI", "S&P", "美股 AI", "美股 S&P", "brk.b"):  # 關閉時像美股的字一律舊句
        assert sp.handle(body).text == sp.TEXT_US, body


def test_us_worker_starts_even_when_the_tw_thread_is_alive(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(sp, "_BACKGROUND_ENABLED", True)
    monkeypatch.setattr(us, "start_background", lambda: order.append("us_start"))
    alive = threading.Thread(target=lambda: None)
    monkeypatch.setattr(alive, "is_alive", lambda: True)
    monkeypatch.setattr(sp, "_thread", alive)
    sp.start_background()
    assert order == ["us_start"]  # 台股執行緒還活著時提早 return，美股照樣起


def test_stop_signals_both_before_joining(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(us, "signal_stop", lambda: order.append("us_signal"))
    monkeypatch.setattr(us, "join_background", lambda timeout=2.0: order.append("us_join"))
    stop, wake = threading.Event(), threading.Event()
    thread = threading.Thread(target=lambda: order.append("tw_join"))
    monkeypatch.setattr(sp, "_thread", thread)
    monkeypatch.setattr(sp, "_stop", stop)
    monkeypatch.setattr(sp, "_wake", wake)
    monkeypatch.setattr(thread, "join", lambda timeout=None: order.append("tw_join"))
    sp.stop_background(timeout=0.1)
    assert order == ["us_signal", "tw_join", "us_join"] and stop.is_set() and wake.is_set()


def test_not_found_text_teaches_the_us_ticker_form(tw_snapshot):
    assert sp.handle("輝達").text == sp.TEXT_NOT_FOUND and "/股票 NVDA" in sp.TEXT_NOT_FOUND
