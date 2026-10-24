"""market_sources_misc（/盤勢 的宏觀、新聞、川普原文、預測市場來源）的解析與抓取測試。

fixture 全部是照 實測抓下來的真實格式（樣本不進 repo）合成的最小結構，
不連網（抓取函式一律注入假的 http）、不放任何家人資料；時間一律注入 now。
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import market_sources_misc as msm

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 17, 2, 0, tzinfo=UTC)
HERE = Path(__file__).resolve().parent


class FakeHTTP:
    """market_http 的替身：記下每次呼叫，回固定的 body。"""

    def __init__(self, body):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.calls: list[dict] = []

    def fetch(self, url, *, max_bytes, timeout=15.0, accept=("json",), headers=None, ledger=None):
        self.calls.append({"url": url, "max_bytes": max_bytes, "accept": accept, "ledger": ledger})
        return SimpleNamespace(status=200, body=self.body, headers={}, url=url)


def _kind(excinfo) -> str:
    return excinfo.value.kind


def _rfc(moment: datetime) -> str:
    return format_datetime(moment.astimezone(UTC), usegmt=True)


# ── Yahoo spark ──────────────────────────────────────────────────────────────


def _stamp(year, month, day, hour=0, minute=0, tz=UTC) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=tz).timestamp())


def _spark_entry(symbol, stamps, closes, *, tz="America/New_York", meta_symbol=None):
    meta = {"symbol": meta_symbol or symbol, "currency": "USD"}
    if tz is not None:
        meta["exchangeTimezoneName"] = tz
    return {"symbol": symbol, "response": [{"meta": meta, "timestamp": stamps, "indicators": {"quote": [{"close": closes}]}}]}


def _spark(*entries):
    return {"spark": {"result": list(entries), "error": None}}


def test_spark_dates_use_the_exchange_timezone():
    dxy = _spark_entry("DX-Y.NYB", [_stamp(2026, 10, d, tz=NY) for d in (14, 15, 16)], [99.1, 99.4, 98.9])
    # 倫敦午夜＝UTC 前一天 23:00；用 UTC 換算會整串早一天
    twd = _spark_entry("TWD=X", [_stamp(2026, 10, d, tz=LONDON) for d in (15, 16)], [32.30, 32.34], tz="Europe/London")
    out = msm.parse_yahoo_spark(_spark(dxy, twd))
    assert out["DX-Y.NYB"] == [(date(2026, 10, 14), 99.1), (date(2026, 10, 15), 99.4), (date(2026, 10, 16), 98.9)]
    assert out["TWD=X"] == [(date(2026, 10, 15), 32.30), (date(2026, 10, 16), 32.34)]
    assert datetime.fromtimestamp(_stamp(2026, 10, 15, tz=LONDON), UTC).date() == date(2026, 10, 14)


def test_spark_dates_follow_daylight_saving_changes():
    stamps = [_stamp(2026, 10, 23, tz=LONDON), _stamp(2026, 10, 26, tz=LONDON)]  # 10/25 倫敦夏令結束
    out = msm.parse_yahoo_spark(_spark(_spark_entry("TWD=X", stamps, [32.1, 32.2], tz="Europe/London")))
    assert [day for day, _ in out["TWD=X"]] == [date(2026, 10, 23), date(2026, 10, 26)]


def test_spark_skips_nulls_and_unusable_values():
    stamps = [_stamp(2026, 10, d, tz=NY) for d in range(5, 13)]
    stamps[7] = None
    closes = [100.0, None, float("nan"), 0, -3.0, True, 104.5, 105.0]
    out = msm.parse_yahoo_spark(_spark(_spark_entry("^DJI", stamps, closes)))
    assert out["^DJI"] == [(date(2026, 10, 5), 100.0), (date(2026, 10, 11), 104.5)]


def test_spark_same_day_keeps_the_later_bar_and_through_drops_later_days():
    stamps = [_stamp(2026, 10, 15, tz=NY), _stamp(2026, 10, 16, tz=NY), _stamp(2026, 10, 16, 15, 59, tz=NY), _stamp(2026, 10, 17, tz=NY)]
    entry = _spark_entry("CL=F", stamps, [70.0, 71.0, 71.6, 72.0])
    assert msm.parse_yahoo_spark(_spark(entry))["CL=F"][-2:] == [(date(2026, 10, 16), 71.6), (date(2026, 10, 17), 72.0)]
    # PLAN §18.8：期貨幾乎 24 小時交易，晚於資料日 D 的那根（下一交易日沒收完）不能用
    assert msm.parse_yahoo_spark(_spark(entry), through=date(2026, 10, 16))["CL=F"] == [
        (date(2026, 10, 15), 70.0),
        (date(2026, 10, 16), 71.6),
    ]


def test_spark_one_broken_symbol_does_not_sink_the_payload():
    good = _spark_entry("GC=F", [_stamp(2026, 10, 16, tz=NY)], [2400.0])
    entries = [
        good,
        _spark_entry("SI=F", [_stamp(2026, 10, 16, tz=NY)], [30.0], tz=None),  # 沒有交易所時區：不猜
        _spark_entry("BZ=F", [_stamp(2026, 10, 16, tz=NY)], [75.0], tz="Not/AZone"),
        _spark_entry("^RUT", [_stamp(2026, 10, 15, tz=NY), _stamp(2026, 10, 16, tz=NY)], [2100.0]),  # 長度對不上
        _spark_entry("^IXIC", [_stamp(2026, 10, 16, tz=NY)], [18000.0], meta_symbol="^NDX"),
        _spark_entry("bad symbol", [_stamp(2026, 10, 16, tz=NY)], [1.0]),
        _spark_entry("^MOVE", [_stamp(2026, 10, 16, tz=NY)], [110.0]),
        _spark_entry("^MOVE", [_stamp(2026, 10, 16, tz=NY)], [111.0]),  # 同一代號兩次：兩個都不用
        {"symbol": "BTC-USD", "response": []},
        {"symbol": "^DJI", "response": [{"meta": {"exchangeTimezoneName": "America/New_York"}, "timestamp": [1]}]},
        "not a dict",
    ]
    assert msm.parse_yahoo_spark(_spark(*entries)) == {"GC=F": [(date(2026, 10, 16), 2400.0)]}


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        ([], "format"),
        ({"spark": None}, "format"),
        ({"spark": {"result": "x"}}, "format"),
        ({"spark": {"result": None, "error": {"code": "Not Found"}}}, "empty"),
        ({"spark": {"result": []}}, "empty"),
        (_spark(_spark_entry("GC=F", [_stamp(2026, 10, 16)], [None])), "empty"),
        (b"{not json", "format"),
        (b"", "empty"),
        (b"\xff\xfe\x00", "format"),
    ],
)
def test_spark_payload_errors(payload, kind):
    with pytest.raises(msm.ParseError) as excinfo:
        msm.parse_yahoo_spark(payload)
    assert _kind(excinfo) == kind


def test_spark_accepts_raw_json_bytes_and_checks_through_type():
    body = json.dumps(_spark(_spark_entry("BTC-USD", [_stamp(2026, 10, 16)], [81000.5], tz="UTC"))).encode()
    assert msm.parse_yahoo_spark(body) == {"BTC-USD": [(date(2026, 10, 16), 81000.5)]}
    with pytest.raises(TypeError):
        msm.parse_yahoo_spark(body, through=datetime(2026, 10, 16))


def test_fetch_yahoo_spark_builds_the_fixed_request():
    body = json.dumps(_spark(_spark_entry("^RUT", [_stamp(2026, 10, 16, tz=NY)], [2200.0])))
    http, ledger = FakeHTTP(body), object()
    out = msm.fetch_yahoo_spark(through=date(2026, 10, 16), http=http, ledger=ledger)
    assert out == {"^RUT": [(date(2026, 10, 16), 2200.0)]}
    (call,) = http.calls
    assert call["url"] == (
        "https://query1.finance.yahoo.com/v7/finance/spark?symbols="
        "DX-Y.NYB,CL%3DF,BZ%3DF,GC%3DF,SI%3DF,BTC-USD,TWD%3DX,%5ERUT,%5EDJI,%5EIXIC,%5EMOVE&range=1mo&interval=1d"
    )
    assert call["accept"] == ("json",)
    assert call["max_bytes"] == msm.ENDPOINTS["yahoo_spark"].max_bytes
    assert call["ledger"] is ledger


@pytest.mark.parametrize("symbols", [["AAPL"], "DX-Y.NYB", [], ["DX-Y.NYB", "../x"]])
def test_fetch_yahoo_spark_only_takes_the_fixed_symbols(symbols):
    http = FakeHTTP("{}")
    with pytest.raises(ValueError):
        msm.fetch_yahoo_spark(symbols, http=http)
    assert http.calls == []


# ── 代理：Nasdaq ETF 日線 ────────────────────────────────────────────────────────


def test_proxy_bars_delegate_to_the_nasdaq_historical_fetcher():
    calls = []

    def historical(symbol, start, end, **kwargs):
        calls.append((symbol, start, end, kwargs))
        return [
            SimpleNamespace(date=date(2026, 10, 16), close=28.92, volume=1),
            SimpleNamespace(date=date(2026, 10, 14), close=29.12, volume=1),
            SimpleNamespace(date=date(2026, 10, 17), close=28.50, volume=1),  # 晚於 D：丟掉
            SimpleNamespace(date=date(2026, 10, 15), close=float("nan"), volume=1),
        ]

    ledger, http = object(), FakeHTTP("{}")
    out = msm.fetch_proxy_bars("UUP", date(2026, 10, 1), date(2026, 10, 16), http=http, ledger=ledger, historical=historical)
    assert out == [(date(2026, 10, 14), 29.12), (date(2026, 10, 16), 28.92)]
    assert calls == [("UUP", date(2026, 10, 1), date(2026, 10, 16), {"etf": True, "ledger": ledger, "http": http})]
    assert http.calls == []
    msm.fetch_proxy_bars("UUP", date(2026, 10, 1), date(2026, 10, 16), historical=historical)
    assert "http" not in calls[-1][3]  # 沒給就讓對方用它自己的預設


def test_proxy_bars_from_a_fetcher_reject_conflicts_and_empty_results():
    def conflicting(symbol, start, end, **kwargs):
        return [SimpleNamespace(date=date(2026, 10, 16), close=28.9), SimpleNamespace(date=date(2026, 10, 16), close=27.0)]

    with pytest.raises(msm.ParseError) as excinfo:
        msm.fetch_proxy_bars("GLD", date(2026, 10, 1), date(2026, 10, 16), historical=conflicting)
    assert _kind(excinfo) == "conflict"
    with pytest.raises(msm.ParseError) as excinfo:
        msm.fetch_proxy_bars("GLD", date(2026, 10, 1), date(2026, 10, 16), historical=lambda *a, **k: [])
    assert _kind(excinfo) == "empty"


def test_proxy_bars_use_the_real_nasdaq_historical_fetcher():
    """接縫：代理走 A 的 fetch_nasdaq_historical，參數名（etf、http、ledger）要真的對得上（2026-10-18）。"""
    import inspect

    import market_sources_us

    assert msm._us_historical() is market_sources_us.fetch_nasdaq_historical
    params = inspect.signature(market_sources_us.fetch_nasdaq_historical).parameters
    assert {"etf", "http", "ledger"} <= set(params)
    assert all(params[name].kind is inspect.Parameter.KEYWORD_ONLY for name in ("etf", "http", "ledger"))
    assert {"date", "close"} <= set(market_sources_us.Bar.__dataclass_fields__)


def test_proxy_bars_do_not_hide_a_broken_market_sources_us(monkeypatch, tmp_path):
    (tmp_path / "market_sources_us.py").write_text("import zz_market_dependency_that_is_missing\n", encoding="utf-8")
    monkeypatch.delitem(sys.modules, "market_sources_us", raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ModuleNotFoundError):
        msm.fetch_proxy_bars("UUP", date(2026, 10, 1), date(2026, 10, 16), http=FakeHTTP("{}"))


def test_proxy_bars_validate_their_arguments():
    http = FakeHTTP("{}")
    with pytest.raises(ValueError):
        msm.fetch_proxy_bars("AAPL", date(2026, 10, 1), date(2026, 10, 16), http=http)
    with pytest.raises(ValueError):
        msm.fetch_proxy_bars("UUP", date(2026, 10, 17), date(2026, 10, 16), http=http)
    with pytest.raises(TypeError):
        msm.fetch_proxy_bars("UUP", datetime(2026, 10, 1), date(2026, 10, 16), http=http)
    assert http.calls == []


def test_proxy_tables_and_labels():
    assert set(msm.PROXY_ETF) <= set(msm.YAHOO_MACRO)
    assert "MOVE" not in msm.PROXY_ETF  # MOVE 沒有代理
    assert msm.proxy_source_label("UUP") == "ETF 代理：UUP"
    with pytest.raises(ValueError):
        msm.proxy_source_label("SPY")


def test_btc_proxy_is_the_ibit_etf_through_the_nasdaq_etf_endpoint():
    """2026-10-23：Coinbase 在台灣被封鎖，BTC 沒有 Yahoo 時改用現貨比特幣 ETF 的 IBIT（和 GOLD→GLD 同一條路）。"""
    assert msm.PROXY_ETF["BTC"] == "IBIT" and msm.YAHOO_MACRO["BTC"] == "BTC-USD"
    assert msm.proxy_source_label("IBIT") == "ETF 代理：IBIT"
    calls = []

    def historical(symbol, start, end, **kwargs):
        calls.append((symbol, kwargs["etf"]))
        return [SimpleNamespace(date=date(2026, 10, 21), close=47.84), SimpleNamespace(date=date(2026, 10, 22), close=46.06)]

    assert msm.fetch_proxy_bars("IBIT", date(2026, 10, 9), date(2026, 10, 22), historical=historical) == [
        (date(2026, 10, 21), 47.84), (date(2026, 10, 22), 46.06)]
    assert calls == [("IBIT", True)]


def test_coinbase_is_gone_as_a_source():
    """api.coinbase.com 在台灣被封鎖（2026-10-23）：抓取、解析、端點、網址都拿掉，不留一條會失敗的路。"""
    for name in ("fetch_coinbase_spot", "parse_coinbase_spot", "COINBASE_SPOT_URL", "SOURCE_COINBASE"):
        assert not hasattr(msm, name), name
    assert "coinbase_spot" not in msm.ENDPOINTS
    assert all("coinbase" not in endpoint.host for endpoint in msm.ENDPOINTS.values())


# ── Google News RSS ──────────────────────────────────────────────────────────


def _rss(*items: str, head: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>' + head + '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">'
        "<channel><title>t</title>" + "".join(items) + "</channel></rss>"
    )


def _news_item(title, published, *, guid="news-1", source="甲報", link="https://news.google.com/rss/articles/a1"):
    parts = [f"<title>{title}</title>"]
    if link is not None:
        parts.append(f"<link>{link}</link>")
    if guid is not None:
        parts.append(f'<guid isPermaLink="false">{guid}</guid>')
    if published is not None:
        parts.append(f"<pubDate>{published}</pubDate>")
    parts.append("<description>d</description>")
    if source is not None:
        parts.append(f'<source url="https://example.com">{source}</source>')
    return "<item>" + "".join(parts) + "</item>"


def test_news_title_drops_the_trailing_source():
    hour_ago = _rfc(NOW - timedelta(hours=1))
    xml = _rss(
        _news_item("美股收黑 那指摔逾2% - 甲報", hour_ago, guid="a"),
        _news_item("選擇權交易時間延長- 國際 - 乙報", hour_ago, guid="b", source="乙報"),
        _news_item("標題後面不是來源 - 丙報", hour_ago, guid="c", source="丁報"),
        _news_item("沒有來源欄位 - 戊報", hour_ago, guid="d", source=None),
        _news_item("沒有來源也切不出來", hour_ago, guid="e", source=None),
    )
    got = {news.guid: (news.title, news.source) for news in msm.parse_google_news_rss(xml, NOW)}
    assert got == {
        "a": ("美股收黑 那指摔逾2%", "甲報"),
        "b": ("選擇權交易時間延長- 國際", "乙報"),
        "c": ("標題後面不是來源 - 丙報", "丁報"),
        "d": ("沒有來源欄位", "戊報"),
    }


def test_news_pubdate_window_edges():
    xml = _rss(
        _news_item("剛好三十六小時", _rfc(NOW - timedelta(hours=36)), guid="edge"),
        _news_item("超過一秒", _rfc(NOW - timedelta(hours=36, seconds=1)), guid="old"),
        _news_item("時鐘差五分鐘", _rfc(NOW + timedelta(minutes=5)), guid="skew"),
        _news_item("日期在未來", _rfc(NOW + timedelta(minutes=6)), guid="future"),
        _news_item("沒有時區", "Sat, 17 Oct 2026 01:00:00", guid="naive"),
        _news_item("日期壞掉", "yesterday-ish", guid="bad-date"),
        _news_item("沒有日期", None, guid="no-date"),
    )
    assert [news.guid for news in msm.parse_google_news_rss(xml, NOW)] == ["skew", "naive", "edge"]


def test_news_window_can_be_narrowed_and_now_can_be_epoch_seconds():
    xml = _rss(_news_item("兩小時前", _rfc(NOW - timedelta(hours=2)), guid="x"))
    assert [n.guid for n in msm.parse_google_news_rss(xml, NOW.timestamp())] == ["x"]
    assert msm.parse_google_news_rss(xml, NOW, max_age_hours=1) == []
    item = msm.parse_google_news_rss(xml, NOW)[0]
    assert item.published_utc == NOW - timedelta(hours=2) and item.published_utc.tzinfo is not None


def test_news_titles_are_cleaned_or_dropped():
    hour_ago = _rfc(NOW - timedelta(hours=1))
    xml = _rss(
        _news_item("看這裡 https://evil.example/x - 甲報", hour_ago, guid="url"),
        _news_item("x @all y - 甲報", hour_ago, guid="at"),
        _news_item("＠媽媽 快看 - 甲報", hour_ago, guid="fullwidth-at"),
        _news_item("台股\u202e反轉\u200b字 - 甲報", hour_ago, guid="bidi"),
        _news_item("很長" * 30 + " - 甲報", hour_ago, guid="long"),
        _news_item("聯絡 0900-000-000 - 甲報", hour_ago, guid="phone"),
        _news_item("正常標題 - 甲報", hour_ago, guid="ok", source="甲報 &lt;b&gt;"),
    )
    got = {news.guid: news for news in msm.parse_google_news_rss(xml, NOW)}
    assert "url" not in got and "phone" not in got
    for news in got.values():
        assert "@" not in news.title and "＠" not in news.title
        assert len(news.title) <= msm.NEWS_TITLE_MAX and len(news.source) <= msm.NAME_MAX
        assert not any(ch in news.title for ch in "\u202e\u200b<>")
    assert "all" in got["at"].title and "媽媽" in got["fullwidth-at"].title
    assert got["bidi"].title == "台股反轉字"
    assert "<" not in got["ok"].source and got["ok"].source.startswith("甲報")


def test_news_dedupes_and_sorts_newest_first():
    xml = _rss(
        _news_item("舊的那則", _rfc(NOW - timedelta(hours=5)), guid="same"),
        _news_item("新的那則", _rfc(NOW - timedelta(hours=1)), guid="same"),
        _news_item("中間", _rfc(NOW - timedelta(hours=3)), guid="mid"),
        _news_item("中間", _rfc(NOW - timedelta(hours=4)), guid="mid-copy"),
    )
    assert [(n.guid, n.title) for n in msm.parse_google_news_rss(xml, NOW)] == [("same", "新的那則"), ("mid", "中間")]


def test_news_guid_falls_back_to_the_link():
    hour_ago = _rfc(NOW - timedelta(hours=1))
    xml = _rss(
        _news_item("只有連結", hour_ago, guid=None, link="https://news.google.com/rss/articles/only-link"),
        _news_item("guid 有空白", hour_ago, guid="has space", link=None),
    )
    assert [n.guid for n in msm.parse_google_news_rss(xml, NOW)] == ["https://news.google.com/rss/articles/only-link"]


@pytest.mark.parametrize(
    "xml",
    [
        _rss(_news_item("t - 甲報", "x", guid="1"), head='<!DOCTYPE rss [<!ENTITY boom "boom">]>'),
        _rss(_news_item("t - 甲報", "x", guid="1"), head="<!doctype rss>"),
        _rss(_news_item("t - 甲報", "x", guid="1"), head='<!DOCTYPE rss SYSTEM "file:///etc/passwd">'),
        _rss(_news_item("t - 甲報", "x", guid="1")).replace("<channel>", '<channel><!ENTITY x "y">'),
        _rss(_news_item("t - 甲報", "x", guid="1"), head='<!DOCTYPE rss [<!ENTITY boom "boom">]>').encode("utf-8"),
    ],
)
def test_news_rejects_doctype_and_entity_declarations(xml):
    with pytest.raises(msm.ParseError) as excinfo:
        msm.parse_google_news_rss(xml, NOW)
    assert _kind(excinfo) == "format"


@pytest.mark.parametrize(
    ("xml", "kind"),
    [
        ("<rss><channel>", "format"),
        ("<feed><entry/></feed>", "format"),
        ("<rss version='2.0'/>", "format"),
        ("<rss><channel><title>t</title></channel></rss>", "empty"),
        ("", "empty"),
        ("   ", "empty"),
        (_rss(_news_item("t - 甲報", _rfc(NOW))).encode("utf-16"), "format"),
        (12345, "format"),
        (_rss(_news_item("t - 甲報", None, guid="1"), _news_item("u - 甲報", "bad", guid="2")), "format"),
        (_rss(_news_item("", _rfc(NOW), guid="1")), "format"),
    ],
)
def test_news_malformed_input(xml, kind):
    with pytest.raises(msm.ParseError) as excinfo:
        msm.parse_google_news_rss(xml, NOW)
    assert _kind(excinfo) == kind


def test_news_accepts_utf8_bytes_with_bom_and_needs_an_aware_now():
    xml = "\ufeff" + _rss(_news_item("位元組輸入 - 甲報", _rfc(NOW - timedelta(minutes=10)), guid="b"))
    assert [n.title for n in msm.parse_google_news_rss(xml.encode("utf-8"), NOW)] == ["位元組輸入"]
    assert [n.title for n in msm.parse_google_news_rss(xml, NOW)] == ["位元組輸入"]
    with pytest.raises(ValueError):
        msm.parse_google_news_rss(xml, datetime(2026, 10, 17, 2, 0))
    with pytest.raises(TypeError):
        msm.parse_google_news_rss(xml, "now")


def test_fetch_google_news_requests():
    xml = _rss(_news_item("台股收紅 - 甲報", _rfc(NOW - timedelta(hours=1)), guid="tw-1"))
    http = FakeHTTP(xml)
    assert [n.title for n in msm.fetch_google_news("us", NOW, http=http)] == ["台股收紅"]
    msm.fetch_google_news("iran", NOW, http=http)
    assert [call["url"] for call in http.calls] == [
        "https://news.google.com/rss/search?q=%E7%BE%8E%E8%82%A1&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "https://news.google.com/rss/search?q=%E4%BC%8A%E6%9C%97&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
    ]
    assert all(call["accept"] == ("xml",) for call in http.calls)
    with pytest.raises(ValueError):
        msm.fetch_google_news("crypto", NOW, http=http)


# ── 川普原文（trumpstruth RSS） ─────────────────────────────────────────────────

TRUMP_NS = (
    'xmlns:truth="https://truthsocial.com/ns" xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:media="http://search.yahoo.com/mrss/" xmlns:archive="https://www.trumpstruth.org/rss-extensions"'
)
POST_ID = "200000000000000001"
QUOTED_ID = "200000000000000099"


def _feed(*items: str, ns: str = TRUMP_NS, head: str = "") -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?>{head}<rss version="2.0" {ns}><channel><title>t</title>{"".join(items)}</channel></rss>'


def _post(
    oid,
    published,
    body,
    *,
    post_type="original",
    removed="false",
    reference=None,
    url="default",
    t="truth",
    a="archive",
):
    if url == "default":
        url = f"https://truthsocial.com/@realDonaldTrump/{oid}"
    parts = ["<title><![CDATA[Video post from October 17, 2026]]></title>", "<link>https://www.trumpstruth.org/statuses/1</link>"]
    if body is not None:
        parts.append(f"<description><![CDATA[{body}]]></description>")
    parts.append("<dc:creator>Donald J. Trump</dc:creator>")
    if published is not None:
        parts.append(f"<pubDate>{published}</pubDate>")
    if url is not None:
        parts.append(f"<{t}:originalUrl>{url}</{t}:originalUrl>")
    if oid is not None:
        parts.append(f"<{t}:originalId>{oid}</{t}:originalId>")
    if post_type is not None:
        parts.append(f"<{a}:postType>{post_type}</{a}:postType>")
    if removed is not None:
        parts.append(f"<{a}:removed>{removed}</{a}:removed>")
    if reference is not None:
        parts.append(reference)
    return "<item>" + "".join(parts) + "</item>"


def _reference(author="Quoted Person", a="archive"):
    return (
        f'<{a}:reference type="quote" originalId="{QUOTED_ID}">'
        f"<{a}:link>https://www.trumpstruth.org/statuses/2</{a}:link>"
        f"<{a}:originalUrl>https://truthsocial.com/@someone/{QUOTED_ID}</{a}:originalUrl>"
        f"<dc:creator>{author}</dc:creator>"
        f"<{a}:publishedAt>2026-10-17T00:00:00+00:00</{a}:publishedAt>"
        f"</{a}:reference>"
    )


def _quote_body(own: str, quoted: str, author: str = "Quoted Person") -> str:
    # 照存檔的真實排法：RT 網址接在 span 裡、緊接本人文字；「Quoted 作者」在 blockquote 外面
    return (
        f"<p><span><br />RT: https://truthsocial.com/users/realDonaldTrump/statuses/{QUOTED_ID}</span>{own}</p>"
        '<p><a href="https://truth-archive.example/attachments/1/a.jpg">View image</a></p>'
        f'<p>Quoted <a href="https://www.trumpstruth.org/statuses/2">{author}</a></p>'
        f"<blockquote><p>{quoted}</p>"
        '<p><a href="https://truth-archive.example/attachments/2/b.mov">View video</a></p></blockquote>'
    )


HOUR_AGO = _rfc(NOW - timedelta(hours=1))


def test_trump_original_post_strips_urls_and_media_links():
    body = (
        "<p>New tariffs on foreign steel start Monday! Read https://www.example.com/a?b=1 now</p>"
        '<p><a href="https://truth-archive.example/attachments/9/c.jpg">View image</a></p>'
        '<p><a href="https://example.com/x">https://example.com/x</a></p>'
    )
    (post,) = msm.parse_trumpstruth_rss(_feed(_post(POST_ID, HOUR_AGO, body)), NOW)
    assert post.original_id == POST_ID
    assert post.url == f"https://truthsocial.com/@realDonaldTrump/{POST_ID}"
    assert post.own_text == "New tariffs on foreign steel start Monday! Read now"
    assert post.topics == ("tariff",)
    assert post.post_type == "original" and post.removed is False and post.quoted_author is None
    assert post.published_utc == NOW - timedelta(hours=1)


def test_trump_quote_post_keeps_only_his_own_words():
    body = _quote_body("Taiwan is a great partner!", "China and Iran tariffs are coming!")
    (post,) = msm.parse_trumpstruth_rss(_feed(_post(POST_ID, HOUR_AGO, body, post_type="quote", reference=_reference())), NOW)
    assert post.own_text == "Taiwan is a great partner!"
    assert post.topics == ("taiwan",)  # 引用的內容（中國、伊朗、關稅）不算本人說的
    assert post.quoted_author == "Quoted Person"
    assert post.post_type == "quote"
    # ID／網址是這則自己的，不是 archive:reference 裡被引用的那則
    assert post.original_id == POST_ID and QUOTED_ID not in (post.url or "")


def test_trump_quoted_author_falls_back_to_the_quoted_intro():
    reference = f'<archive:reference type="quote" originalId="{QUOTED_ID}"><archive:link>x</archive:link></archive:reference>'
    body = _quote_body("NATO must pay up!", "anything", author="Another Person")
    (post,) = msm.parse_trumpstruth_rss(_feed(_post(POST_ID, HOUR_AGO, body, post_type="quote", reference=reference)), NOW)
    assert post.quoted_author == "Another Person" and post.own_text == "NATO must pay up!"


def test_trump_posts_with_only_media_or_only_a_quote_are_not_listed():
    feed = _feed(
        _post("200000000000000002", HOUR_AGO, '<p></p><p><a href="https://truth-archive.example/v.mp4">View video</a></p>'),
        _post("200000000000000003", HOUR_AGO, '<p></p><p><a href="https://truth-archive.example/i.jpg">View image</a></p>'),
        _post("200000000000000004", HOUR_AGO, _quote_body("", "Iran Iran Iran"), post_type="quote", reference=_reference()),
        _post("200000000000000005", HOUR_AGO, "<p>!!! https://example.com/z</p>"),
        _post("200000000000000006", HOUR_AGO, "<p>RT @someone: Tariffs are great</p>"),
        _post("200000000000000007", HOUR_AGO, "<p>China deal is done</p>", post_type="retruth"),
        _post(POST_ID, HOUR_AGO, "<p>Big rally tonight</p>"),
    )
    assert [post.original_id for post in msm.parse_trumpstruth_rss(feed, NOW)] == [POST_ID]


def test_trump_removed_posts():
    feed = _feed(
        _post(POST_ID, HOUR_AGO, "<p>Tariffs on everything</p>", removed="true"),
        _post("200000000000000002", HOUR_AGO, "<p>Iran must stop</p>", removed="TRUE"),
    )
    assert msm.parse_trumpstruth_rss(feed, NOW) == []
    tombstones = msm.parse_trumpstruth_rss(feed, NOW, include_removed=True)
    assert {post.original_id for post in tombstones} == {POST_ID, "200000000000000002"}
    assert all(post.removed and post.own_text is None and post.topics == () for post in tombstones)


def test_trump_window_edges():
    feed = _feed(
        _post("200000000000000002", _rfc(NOW - timedelta(hours=48)), "<p>edge of the window</p>"),
        _post("200000000000000003", _rfc(NOW - timedelta(hours=48, seconds=1)), "<p>too old</p>"),
        _post("200000000000000004", _rfc(NOW + timedelta(minutes=6)), "<p>from the future</p>"),
        _post("200000000000000005", "Sat, 17 Oct 2026 01:00:00 +0000", "<p>an hour ago</p>"),
    )
    assert [post.original_id for post in msm.parse_trumpstruth_rss(feed, NOW)] == ["200000000000000005", "200000000000000002"]
    assert msm.parse_trumpstruth_rss(feed, NOW, window_hours=2)[0].original_id == "200000000000000005"


def test_trump_namespaces_are_matched_by_uri_not_by_prefix():
    other_prefixes = (
        'xmlns:t="https://truthsocial.com/ns" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:x="https://www.trumpstruth.org/rss-extensions"'
    )
    feed = _feed(_post(POST_ID, HOUR_AGO, "<p>Israel and the ceasefire</p>", t="t", a="x"), ns=other_prefixes)
    (post,) = msm.parse_trumpstruth_rss(feed, NOW)
    assert post.original_id == POST_ID and post.topics == ("israel", "ceasefire")
    wrong_uri = TRUMP_NS.replace("https://truthsocial.com/ns", "https://example.invalid/ns")
    with pytest.raises(msm.ParseError) as excinfo:
        msm.parse_trumpstruth_rss(_feed(_post(POST_ID, HOUR_AGO, "<p>text</p>"), ns=wrong_uri), NOW)
    assert _kind(excinfo) == "format"


def test_trump_id_only_inside_the_reference_is_not_used():
    # 沒有自己的 truth:originalId，只有 archive:reference 的 originalId 屬性：算壞列，不能拿被引用的 ID
    feed = _feed(_post(None, HOUR_AGO, _quote_body("Own words here", "q"), post_type="quote", reference=_reference(), url=None))
    with pytest.raises(msm.ParseError) as excinfo:
        msm.parse_trumpstruth_rss(feed, NOW)
    assert _kind(excinfo) == "format"


def test_trump_url_must_match_the_post_id():
    feed = _feed(
        _post(POST_ID, HOUR_AGO, "<p>one</p>", url=f"https://truthsocial.com/@realDonaldTrump/{QUOTED_ID}"),
        _post("200000000000000002", HOUR_AGO, "<p>two</p>", url="http://truthsocial.com/@realDonaldTrump/200000000000000002"),
        _post("200000000000000003", HOUR_AGO, "<p>three</p>", url=None),
    )
    assert {post.original_id: post.url for post in msm.parse_trumpstruth_rss(feed, NOW)} == {
        POST_ID: None,
        "200000000000000002": None,
        "200000000000000003": None,
    }


def test_trump_topics_use_the_full_text_and_the_excerpt_is_capped():
    text = "Great rally in Georgia today with many wonderful patriots, " * 3 + "and NATO must finally pay its fair share!"
    (post,) = msm.parse_trumpstruth_rss(_feed(_post(POST_ID, HOUR_AGO, f"<p>{text}</p>")), NOW)
    assert post.topics == ("nato",)
    assert post.own_text is not None and len(post.own_text) <= msm.TRUMP_EXCERPT_MAX
    assert post.own_text.startswith("Great rally in Georgia today") and "NATO" not in post.own_text


def test_trump_excerpt_that_fails_cleaning_still_counts_its_topics():
    (post,) = msm.parse_trumpstruth_rss(_feed(_post(POST_ID, HOUR_AGO, "<p>Call 0900-000-000 about oil prices</p>")), NOW)
    assert post.own_text is None and post.topics == ("oil",)


def test_trump_quoted_intro_is_kept_when_there_is_no_reference():
    body = '<p>Quoted <a href="https://example.com/fox">Fox News</a></p><p>Stock Market at record high</p>'
    (post,) = msm.parse_trumpstruth_rss(_feed(_post(POST_ID, HOUR_AGO, body)), NOW)
    assert post.own_text == "Quoted Fox News Stock Market at record high"
    assert post.topics == ("stock_market",)


def test_trump_html_entities_and_spacing():
    body = "<p>Tariffs &amp; trade<br/>with <b>China</b>&nbsp;today</p><div>Powell is late</div>"
    (post,) = msm.parse_trumpstruth_rss(_feed(_post(POST_ID, HOUR_AGO, body)), NOW)
    assert post.own_text == "Tariffs & trade with China today Powell is late"
    assert post.topics == ("tariff", "fed", "china")


def test_trump_bad_items_are_skipped_and_all_bad_is_a_format_error():
    good = _post(POST_ID, HOUR_AGO, "<p>good one</p>")
    broken = [
        _post("200000000000000002", "not a date", "<p>x</p>"),
        _post("200000000000000003", HOUR_AGO, "<p>x</p>", removed=None),
        _post("200000000000000004", HOUR_AGO, "<p>x</p>", removed="maybe"),
        _post("200000000000000005", HOUR_AGO, None),
        _post("abc", HOUR_AGO, "<p>x</p>"),
        _post("200000000000000006", None, "<p>x</p>"),
    ]
    assert [post.original_id for post in msm.parse_trumpstruth_rss(_feed(good, *broken), NOW)] == [POST_ID]
    with pytest.raises(msm.ParseError) as excinfo:
        msm.parse_trumpstruth_rss(_feed(*broken), NOW)
    assert _kind(excinfo) == "format"


def test_trump_dedupes_by_original_id_and_sorts_newest_first():
    feed = _feed(
        _post("200000000000000002", _rfc(NOW - timedelta(hours=3)), "<p>older</p>"),
        _post(POST_ID, _rfc(NOW - timedelta(hours=2)), "<p>first copy</p>"),
        _post(POST_ID, _rfc(NOW - timedelta(hours=1)), "<p>edited copy</p>"),
    )
    posts = msm.parse_trumpstruth_rss(feed, NOW)
    assert [(post.original_id, post.own_text) for post in posts] == [(POST_ID, "edited copy"), ("200000000000000002", "older")]


@pytest.mark.parametrize(
    "xml",
    [
        _feed(_post(POST_ID, HOUR_AGO, "<p>x</p>"), head='<!DOCTYPE rss [<!ENTITY e SYSTEM "file:///etc/hosts">]>'),
        _feed(_post(POST_ID, HOUR_AGO, "<p>x</p>")).replace("<channel>", "<channel><!ENTITY  x 'y'>"),
    ],
)
def test_trump_feed_rejects_dtd(xml):
    with pytest.raises(msm.ParseError) as excinfo:
        msm.parse_trumpstruth_rss(xml.encode("utf-8"), NOW)
    assert _kind(excinfo) == "format"


@pytest.mark.parametrize(("xml", "kind"), [("<rss><channel></channel></rss>", "empty"), ("<rss>", "format"), (None, "format")])
def test_trump_feed_malformed(xml, kind):
    with pytest.raises(msm.ParseError) as excinfo:
        msm.parse_trumpstruth_rss(xml, NOW)
    assert _kind(excinfo) == kind


def test_fetch_trumpstruth_uses_the_www_feed_and_xml():
    http = FakeHTTP(_feed(_post(POST_ID, HOUR_AGO, "<p>Iran talks today</p>", removed="true")))
    assert msm.fetch_trumpstruth(NOW, http=http) == []
    assert [post.removed for post in msm.fetch_trumpstruth(NOW, http=http, include_removed=True)] == [True]
    assert {call["url"] for call in http.calls} == {"https://www.trumpstruth.org/feed"}
    assert all(call["accept"] == ("xml",) for call in http.calls)


VIDEO_ONLY = '<p><a href="https://truth-archive.example/v.mp4">View video</a></p>'


def test_feed_span_covers_items_the_parser_filters_out():
    # PLAN §18.14 的缺口判斷要用整份 feed 的頭尾，不是濾過後剩下的：只有影片的貼文、被移除的空殼都算
    oldest = NOW - timedelta(hours=30)
    feed = _feed(
        _post("200000000000000002", _rfc(oldest), VIDEO_ONLY),
        _post("200000000000000003", _rfc(NOW - timedelta(hours=5)), "<p>Tariffs</p>", removed="true"),
        _post(POST_ID, HOUR_AGO, "<p>Big rally tonight</p>"),
        _post("abc", _rfc(NOW - timedelta(hours=99)), "<p>bad id is not counted</p>"),
        _post("200000000000000004", "not a date", "<p>bad date is not counted</p>"),
    )
    assert [post.original_id for post in msm.parse_trumpstruth_rss(feed, NOW)] == [POST_ID]
    assert msm.trumpstruth_feed_span(feed, NOW) == msm.FeedSpan(oldest, NOW - timedelta(hours=1), False)
    assert msm.trumpstruth_feed_span(feed, NOW.timestamp()) == msm.trumpstruth_feed_span(feed, NOW)  # now 可以是 epoch 秒
    http = FakeHTTP(feed)
    posts, span = msm.fetch_trumpstruth_with_span(NOW, http=http, include_removed=True)
    assert [post.original_id for post in posts] == [POST_ID, "200000000000000003"] and span.oldest == oldest
    assert len(http.calls) == 1 and http.calls[0]["url"] == "https://www.trumpstruth.org/feed"


def test_feed_span_applies_the_same_time_bounds_as_the_parser():
    """2026-10-23 審查 r7b F1：f78d5de 改看整份 feed 時把 `_within` 的界線也拿掉了。晚於 now + 5 分鐘的是時間打錯、
    不算；比 now − 48 小時舊的（置頂、被移除的空殼）不算進 oldest，只記 reaches_back；視窗內只有圖的貼文照算。"""
    feed = _feed(
        _post("200000000000000002", _rfc(NOW + timedelta(days=10)), VIDEO_ONLY),  # 來源打錯年份
        _post("200000000000000003", _rfc(NOW + timedelta(minutes=5, seconds=1)), VIDEO_ONLY),  # 剛好超過上界
        _post("200000000000000004", _rfc(NOW + timedelta(minutes=5)), VIDEO_ONLY),  # 上界（含）
        _post("200000000000000005", _rfc(NOW - timedelta(days=30)), "", removed="true"),  # 置頂的舊空殼
        _post("200000000000000006", _rfc(NOW - timedelta(hours=48, seconds=1)), VIDEO_ONLY),  # 剛好超過視窗
        _post("200000000000000007", _rfc(NOW - timedelta(hours=48)), VIDEO_ONLY),  # 下界（含）
        _post(POST_ID, HOUR_AGO, "<p>Big rally tonight</p>"),
    )
    assert msm.trumpstruth_feed_span(feed, NOW) == msm.FeedSpan(
        NOW - timedelta(hours=48), NOW + timedelta(minutes=5), True
    )
    # 這份 feed 一則比視窗舊的都沒有 → reaches_back 為假
    recent = _feed(_post("200000000000000007", _rfc(NOW - timedelta(hours=48)), VIDEO_ONLY), _post(POST_ID, HOUR_AGO, "<p>x</p>"))
    assert msm.trumpstruth_feed_span(recent, NOW).reaches_back is False


def test_feed_span_with_nothing_inside_the_window_uses_the_newest_item():
    # 川普（或存檔）48 小時沒新貼文：feed 整份都比視窗舊 → 沒有視窗內的最舊一則，oldest＝newest；feed 涵蓋了視窗之前
    newest = NOW - timedelta(hours=60)
    feed = _feed(
        _post("200000000000000002", _rfc(NOW - timedelta(hours=90)), VIDEO_ONLY),
        _post(POST_ID, _rfc(newest), "<p>old news</p>"),
        _post("200000000000000003", _rfc(NOW + timedelta(hours=1)), VIDEO_ONLY),  # 未來的照樣不算
    )
    assert msm.trumpstruth_feed_span(feed, NOW) == msm.FeedSpan(newest, newest, True)


@pytest.mark.parametrize(
    ("xml", "kind"),
    [
        (_feed(_post("abc", HOUR_AGO, "<p>x</p>"), _post(POST_ID, "bad", "<p>x</p>")), "format"),
        ("<rss><channel></channel></rss>", "empty"),
        (_feed(_post(POST_ID, HOUR_AGO, "<p>x</p>"), head="<!DOCTYPE rss>"), "format"),
        # 每一則都在未來：來源的時間不能信，不拿來更新缺口紀錄
        (_feed(_post(POST_ID, _rfc(NOW + timedelta(hours=1)), "<p>x</p>"), _post("200000000000000002", _rfc(NOW + timedelta(days=3)), "<p>y</p>")), "date"),
    ],
)
def test_feed_span_errors(xml, kind):
    with pytest.raises(msm.ParseError) as excinfo:
        msm.trumpstruth_feed_span(xml, NOW)
    assert _kind(excinfo) == kind


# ── 川普貼文主題 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "topics"),
    [
        ("Great deal on the market today, stock is up, rate is fine, war is bad", []),
        ("THE STOCK MARKET IS AT AN ALL TIME HIGH", ["stock_market"]),
        ("Stock markets love us", ["stock_market"]),
        ("New Tariffs on China", ["tariff", "china"]),
        ("tariff", ["tariff"]),
        ("Iranian leaders called", ["iran"]),
        ("IRAN, Iran, Tehran", ["iran"]),
        ("The Fed must cut now", ["fed"]),
        ("The FED is too slow", ["fed"]),
        ("Federal Reserve and Jerome Powell", ["fed"]),
        ("people are fed up with fake news", []),
        ("Fed up with the Radical Left", []),
        ("FED UP!", []),
        ("The Federalist papers", []),
        ("President Xi called me", ["china"]),
        ("xi and xin", []),
        ("Chinese students", ["china"]),
        ("Lower interest rates now", ["rates"]),
        ("interest rate", ["rates"]),
        ("rates and rate hikes", []),
        ("Oil prices are down", ["oil"]),
        ("the price of oil", ["oil"]),
        ("oil and gas", []),
        ("Israel and Israelis", ["israel"]),
        ("A cease-fire was signed", ["ceasefire"]),
        ("Ceasefire now", ["ceasefire"]),
        ("cease fire", ["ceasefire"]),
        ("Taiwanese chips", ["taiwan", "chips"]),
        ("Semiconductors made in America", ["chips"]),
        ("a chip plant", ["chips"]),
        ("a bargaining chip", []),
        ("blue chip stocks", []),
        ("NATO allies", ["nato"]),
        ("Ukrainian and Russian talks with Putin", ["russia_ukraine"]),
        ("Zelenskyy met Russia", ["russia_ukraine"]),
        ("Iran Iran tariffs Iran", ["tariff", "iran"]),
        ("ＩＲＡＮ ＴＡＲＩＦＦＳ", ["tariff", "iran"]),
        ("Ta\u200biwan", ["taiwan"]),
        ("Tirana and Chinatown", []),
        ("", []),
    ],
)
def test_classify_trump_topics(text, topics):
    assert msm.classify_trump_topics(text) == topics


def test_classify_trump_topics_labels_and_non_text():
    assert msm.classify_trump_topics(None) == [] and msm.classify_trump_topics(123) == []
    assert msm.TRUMP_TOPIC_LABELS["tariff"] == "關稅" and msm.TRUMP_TOPIC_LABELS["iran"] == "伊朗"
    every = "tariffs Iran Fed interest rates oil prices China Israel ceasefire Taiwan chips stock market NATO Russia"
    found = msm.classify_trump_topics(every)
    assert found == list(msm.TRUMP_TOPIC_LABELS)  # 每個主題都有卡上名稱，順序固定


# ── 預測市場 ─────────────────────────────────────────────────────────────────


def _pm_market(market_id="562802", title="Democratic Party", **overrides):
    market = {
        "id": market_id,
        "question": f"Will the {title} control the House after the 2026 Midterm elections?",
        "groupItemTitle": title,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.905", "0.095"]',
        "bestBid": 0.9,
        "bestAsk": 0.91,
        "lastTradePrice": 0.91,
        "updatedAt": "2026-10-17T02:37:14.573417Z",
        "active": True,
        "closed": False,
    }
    market.update(overrides)
    return market


def _placeholder(market_id, letter):
    return _pm_market(
        market_id, f"Party {letter}", outcomePrices=None, bestBid=0, bestAsk=1, lastTradePrice=0, active=False,
        updatedAt="2026-08-10T16:58:28.87427Z",
    )


def _pm_event(*markets):
    return [{"id": "32225", "slug": "which-party-will-win-the-house-in-2026", "markets": list(markets)}]


def test_polymarket_fixed_market_quote():
    payload = _pm_event(_pm_market(), _pm_market("562803", "Republican Party"), _placeholder("562804", "A"))
    quote = msm.parse_polymarket_event(json.dumps(payload).encode(), "562802", expect_outcome="Democratic Party")
    assert (quote.platform, quote.market_id, quote.outcome) == ("polymarket", "562802", "Democratic Party")
    assert (quote.bid, quote.ask, quote.last, quote.mid) == (0.9, 0.91, 0.91, 0.905)
    assert quote.updated_utc == datetime(2026, 10, 17, 2, 37, 14, 573417, tzinfo=UTC)
    assert (quote.active, quote.closed, quote.result) == (True, False, None)
    assert msm.parse_polymarket_event(payload[0], "562803").outcome == "Republican Party"  # 單一 event 也收


@pytest.mark.parametrize(
    ("market", "kind"),
    [
        (_placeholder("562802", "A"), "empty"),  # 佔位：沒結束卻沒有價格
        (_pm_market(title="Party C"), "empty"),
        (_pm_market(outcomePrices="not json"), "empty"),
        (_pm_market(outcomePrices='["0.5"]'), "empty"),
        (_pm_market(outcomes='["No", "Yes"]'), "format"),
        (_pm_market(outcomes=None), "format"),
        (_pm_market(active="true"), "format"),
        (_pm_market(bestBid=1.2), "format"),
        (_pm_market(bestAsk="abc"), "format"),
        (_pm_market(lastTradePrice=True), "format"),
        (_pm_market(bestBid=None, bestAsk=None, lastTradePrice=None), "missing_field"),
        (_pm_market(groupItemTitle="Democrats win", question=None), "conflict"),
        (_pm_market(groupItemTitle="", question=""), "format"),
    ],
)
def test_polymarket_bad_or_placeholder_markets(market, kind):
    with pytest.raises(msm.ParseError) as excinfo:
        msm.parse_polymarket_event(_pm_event(market), "562802", expect_outcome="Democratic Party")
    assert _kind(excinfo) == kind


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        (_pm_event(_pm_market("562803", "Republican Party")), "missing_field"),
        (_pm_event(_pm_market()) + _pm_event(_pm_market()), "conflict"),
        ([], "empty"),
        ("[]", "empty"),
        ("x", "format"),
        ([{"markets": "x"}], "format"),
        (42, "format"),
    ],
)
def test_polymarket_payload_errors(payload, kind):
    with pytest.raises(msm.ParseError) as excinfo:
        msm.parse_polymarket_event(payload, "562802")
    assert _kind(excinfo) == kind


def test_polymarket_resolved_market_reports_the_result():
    resolved = _pm_market(closed=True, active=False, outcomePrices='["1", "0"]', bestBid=None, bestAsk=None, lastTradePrice=None)
    quote = msm.parse_polymarket_event(_pm_event(resolved), "562802")
    assert (quote.closed, quote.result, quote.mid) == (True, "Yes", None)
    lost = _pm_market(closed=True, outcomePrices='["0", "1"]')
    assert msm.parse_polymarket_event(_pm_event(lost), "562802").result == "No"
    pending = _pm_market(closed=True, outcomePrices='["0.99", "0.01"]')
    assert msm.parse_polymarket_event(_pm_event(pending), "562802").result is None  # 還沒判定＝等待結果
    no_prices = _pm_market(closed=True, outcomePrices=None)
    assert msm.parse_polymarket_event(_pm_event(no_prices), "562802").result is None


def test_polymarket_mid_needs_both_sides_and_a_sane_spread():
    assert msm.parse_polymarket_event(_pm_event(_pm_market(bestBid=0.92, bestAsk=0.91)), "562802").mid is None
    assert msm.parse_polymarket_event(_pm_event(_pm_market(bestBid=0, bestAsk=0.91)), "562802").mid is None
    assert msm.parse_polymarket_event(_pm_event(_pm_market(bestBid=0.9, bestAsk=1)), "562802").mid is None
    assert msm.parse_polymarket_event(_pm_event(_pm_market(bestBid="0.4", bestAsk="0.6")), "562802").mid == 0.5
    quote = msm.parse_polymarket_event(_pm_event(_pm_market(updatedAt="sometime", bestAsk=None)), "562802")
    assert quote.updated_utc is None and quote.mid is None and quote.last == 0.91


def _kalshi_market(ticker="CONTROLH-2026-D", **overrides):
    market = {
        "ticker": ticker,
        "event_ticker": "CONTROLH-2026",
        "status": "active",
        "result": "",
        "yes_bid_dollars": "0.9010",
        "yes_ask_dollars": "0.9020",
        "last_price_dollars": "0.9020",
        "updated_time": "2026-07-14T16:40:55.905016Z",
        "yes_sub_title": "Democratic Party",
        "title": "Will Democrats win the House in 2026?",
    }
    market.update(overrides)
    return market


def test_kalshi_market_quote():
    payload = {"cursor": "", "markets": [_kalshi_market(), _kalshi_market("CONTROLH-2026-R", yes_sub_title="Republican Party")]}
    quote = msm.parse_kalshi_markets(json.dumps(payload), "CONTROLH-2026-D", expect_outcome="Democratic Party")
    assert (quote.platform, quote.market_id, quote.outcome) == ("kalshi", "CONTROLH-2026-D", "Democratic Party")
    assert (quote.bid, quote.ask, quote.last, quote.mid) == (0.901, 0.902, 0.902, 0.9015)
    assert quote.updated_utc is None  # updated_time 不是成交時間
    assert (quote.active, quote.closed, quote.result) == (True, False, None)
    assert msm.parse_kalshi_markets({"market": _kalshi_market()}, "CONTROLH-2026-D").mid == 0.9015


def test_kalshi_status_and_result():
    finalized = msm.parse_kalshi_markets({"markets": [_kalshi_market(status="finalized", result="yes")]}, "CONTROLH-2026-D")
    assert (finalized.active, finalized.closed, finalized.result) == (False, True, "Yes")
    closed = msm.parse_kalshi_markets({"markets": [_kalshi_market(status="closed", result=None)]}, "CONTROLH-2026-D")
    assert (closed.active, closed.closed, closed.result) == (False, True, None)
    settled_no = msm.parse_kalshi_markets({"markets": [_kalshi_market(status="settled", result="no")]}, "CONTROLH-2026-D")
    assert settled_no.result == "No"
    paused = msm.parse_kalshi_markets({"markets": [_kalshi_market(status="paused")]}, "CONTROLH-2026-D")
    assert (paused.active, paused.closed) == (False, False)


def test_kalshi_legacy_cent_prices():
    legacy = _kalshi_market(yes_bid=90, yes_ask=91, last_price=91)
    for key in ("yes_bid_dollars", "yes_ask_dollars", "last_price_dollars"):
        del legacy[key]
    quote = msm.parse_kalshi_markets({"markets": [legacy]}, "CONTROLH-2026-D")
    assert (quote.bid, quote.ask, quote.last, quote.mid) == (0.9, 0.91, 0.91, 0.905)


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        ({"markets": [_kalshi_market(result="void")]}, "format"),
        ({"markets": [_kalshi_market(result=1)]}, "format"),
        ({"markets": [_kalshi_market(status=None)]}, "missing_field"),
        ({"markets": [_kalshi_market(status="  ")]}, "missing_field"),
        ({"markets": [_kalshi_market(yes_bid_dollars="abc")]}, "format"),
        ({"markets": [_kalshi_market(yes_ask_dollars="1.5")]}, "format"),
        ({"markets": [_kalshi_market(yes_bid_dollars=None, yes_ask_dollars=None, last_price_dollars=None)]}, "missing_field"),
        ({"markets": [_kalshi_market(yes_sub_title="Republican Party")]}, "conflict"),
        ({"markets": [_kalshi_market("CONTROLH-2026-R")]}, "missing_field"),
        ({"markets": [_kalshi_market(), _kalshi_market()]}, "conflict"),
        ({"markets": []}, "empty"),
        ({"markets": None}, "format"),
        ({"cursor": ""}, "format"),
        ([], "format"),
        (b"\x00", "format"),
    ],
)
def test_kalshi_errors(payload, kind):
    with pytest.raises(msm.ParseError) as excinfo:
        msm.parse_kalshi_markets(payload, "CONTROLH-2026-D", expect_outcome="Democratic Party")
    assert _kind(excinfo) == kind


def test_fetch_prediction_quotes_use_fixed_markets():
    polymarket, kalshi, polymarket_senate, kalshi_senate = msm.PREDICTION_MARKETS
    http = FakeHTTP(json.dumps(_pm_event(_pm_market(), _placeholder("562804", "A"))))
    assert msm.fetch_prediction_quote(polymarket, http=http).mid == 0.905
    assert http.calls[0]["url"] == "https://gamma-api.polymarket.com/events?slug=which-party-will-win-the-house-in-2026"
    assert http.calls[0]["accept"] == ("json",)
    http = FakeHTTP(json.dumps({"cursor": "", "markets": [_kalshi_market()]}))
    assert msm.fetch_prediction_quote(kalshi, http=http).last == 0.902
    assert http.calls[0]["url"] == "https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=CONTROLH-2026"
    assert http.calls[0]["accept"] == ("json",)
    other = msm.PredictionMarket("polymarket.house", "polymarket", "some-other-event", "1", "Democratic Party")
    with pytest.raises(ValueError):
        msm.fetch_prediction_quote(other, http=http)
    assert len(http.calls) == 1
    # 參院：同一個 event 裡還有 Party A–F 的佔位市場（2026-10-18 實測），只取民主黨那個 id
    senate = _pm_market("562793", question="Will the Democratic Party control the Senate after the 2026 Midterm elections?")
    http = FakeHTTP(json.dumps(_pm_event(senate, _placeholder("562795", "A"))))
    assert msm.fetch_prediction_quote(polymarket_senate, http=http).market_id == "562793"
    assert http.calls[0]["url"] == "https://gamma-api.polymarket.com/events?slug=which-party-will-win-the-senate-in-2026"
    http = FakeHTTP(json.dumps({"cursor": "", "markets": [
        _kalshi_market("CONTROLS-2026-D", event_ticker="CONTROLS-2026", yes_bid_dollars="0.6200", yes_ask_dollars="0.6300"),
        _kalshi_market("CONTROLS-2026-R", event_ticker="CONTROLS-2026", yes_sub_title="Republican Party"),
    ]}))
    assert msm.fetch_prediction_quote(kalshi_senate, http=http).mid == 0.625
    assert http.calls[0]["url"] == "https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=CONTROLS-2026"


def test_prediction_market_table_is_fixed():
    assert [(m.key, m.platform, m.market_id) for m in msm.PREDICTION_MARKETS] == [
        ("polymarket.house", "polymarket", "562802"),
        ("kalshi.house", "kalshi", "CONTROLH-2026-D"),
        ("polymarket.senate", "polymarket", "562793"),
        ("kalshi.senate", "kalshi", "CONTROLS-2026-D"),
    ]
    assert all(m.key in msm.ENDPOINTS and m.expect_outcome == "Democratic Party" for m in msm.PREDICTION_MARKETS)


def test_prediction_markets_are_blocked_but_parsers_and_tables_stay():
    """2026-10-23：Polymarket、Kalshi 在台灣被封鎖：job 不抓（PREDICTION_MARKETS_BLOCKED），主機不在白名單；
    解析函式、固定市場表、端點表都留著，之後換來源或解除封鎖時不用重寫。"""
    market_http = pytest.importorskip("market_http")
    assert msm.PREDICTION_MARKETS_BLOCKED is True
    assert callable(msm.parse_polymarket_event) and callable(msm.parse_kalshi_markets) and callable(msm.fetch_prediction_quote)
    assert {m.platform for m in msm.PREDICTION_MARKETS} == {"polymarket", "kalshi"}
    for market in msm.PREDICTION_MARKETS:
        assert msm.ENDPOINTS[market.key].host not in market_http.ALLOWED_HOSTS, market.key


# ── 端點表 ───────────────────────────────────────────────────────────────────


def test_endpoint_table_contract():
    market_http = pytest.importorskip("market_http")
    blocked = {market.key for market in msm.PREDICTION_MARKETS}  # 封鎖中：端點表留著，但主機不在白名單（不繞過封鎖）
    for name, endpoint in msm.ENDPOINTS.items():
        assert (endpoint.host in market_http.ALLOWED_HOSTS) == (name not in blocked), name
        assert endpoint.max_bytes > 0 and endpoint.per_day >= 1 and endpoint.retry_per_day >= 0
        expected_accept = ("xml",) if name.startswith("gnews.") or name == "trumpstruth" else ("json",)
        assert endpoint.accept == expected_accept, name
    for url, name in [
        (msm.TRUMPSTRUTH_URL, "trumpstruth"),
        (msm.YAHOO_SPARK_URL, "yahoo_spark"),
        (msm.GOOGLE_NEWS_URL, "gnews.us"),
        (msm.POLYMARKET_EVENT_URL, "polymarket.house"),
        (msm.KALSHI_MARKETS_URL, "kalshi.house"),
    ]:
        assert url.startswith(f"https://{msm.ENDPOINTS[name].host}/"), name
        host = market_http.host_of(url.replace("{", "").replace("}", ""))
        assert host == (None if name in blocked else msm.ENDPOINTS[name].host), name


def test_endpoint_daily_budgets_match_the_plan():
    per_day = {name: endpoint.per_day for name, endpoint in msm.ENDPOINTS.items()}
    assert per_day["yahoo_spark"] == 1 and msm.ENDPOINTS["yahoo_spark"].retry_per_day == 0
    assert per_day["gnews.us"] + per_day["gnews.tw"] + per_day["gnews.iran"] == 48 + 48 + 24  # §19.2：伊朗一小時
    assert per_day["trumpstruth"] == 48
    assert per_day["polymarket.house"] == per_day["kalshi.house"] == 12


def test_get_refuses_a_url_for_another_host():
    with pytest.raises(ValueError):
        msm._get("trumpstruth", "https://trumpstruth.org/feed", FakeHTTP(""), None)


# ── 外部字串清洗：本模組用的就是 market_calc.clean_external（2026-10-18 拿掉本檔版本，測資留著） ──


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ＡＢＣ１２３", "ABC123"),
        ("abc\u202edef", "abcdef"),
        ("a\u200bb\u2060c\ufeffd", "abcd"),
        ("a\ud800b", "ab"),
        ("a\ue000b\u0378c", "abc"),
        ("line\u2028two\nthree\tfour", "line two three four"),
        ("<b>粗體</b>標題", "粗體 標題"),
        ("&lt;script&gt;alert(1)&lt;/script&gt;x", "alert(1) x"),
        ("＜b＞全形標籤＜/b＞", "全形標籤"),
        ("x @all y", "x \u3000all y"),
        ("＠媽媽", "媽媽"),
        ("最新公布 9月出口 年增30%", "最新公布 9月出口 年增30%"),
        ("2009年09月營收", "2009年09月營收"),
        ("  多個   空白  ", "多個 空白"),
    ],
)
def test_clean_external_scrubs(text, expected):
    assert msm._clean(text, 40) == expected


@pytest.mark.parametrize(
    "text",
    [
        "看 https://x.example/a",
        "HTTP://X.EXAMPLE",
        "到 www.example.org 看",
        "寫信 someone@example.com",
        "寫信 someone@example.invalid",  # 網域不在網址規則裡：只有先查 email 再換 @ 才抓得到
        "寫信 someone＠example.com",
        "編碼的 someone&#64;example.com",
        "電話 0900-000-000",
        "電話 0900000000",
        "電話 ０９００ ０００ ０００",
        "",
        "   ",
        "\u200b\u202e",
    ],
)
def test_clean_external_drops(text):
    assert msm._clean(text, 40) is None


def test_bare_domains_are_kept_on_purpose():
    """沒有斜線的裸網域不擋（持股名稱「AMAZON.COM INC」要留；Flex text 與大字版網頁都不會變成連結）。"""
    assert msm._clean("請上 example.com", 40) == "請上 example.com"
    assert msm._clean("請上 example.com/a", 40) is None


def test_clean_external_truncates_and_rejects_non_text():
    assert msm._clean("長" * 50, 40) == "長" * 37 + "..."
    assert msm._clean(None, 40) is None
    assert msm._clean(42, 40) is None
    with pytest.raises(ValueError):
        msm._clean("abc", 0)  # 長度上限是呼叫端的常數，寫錯要馬上壞


def test_trump_topic_order_matches_the_card_order():
    """卡片同票時照 market_calc.TRUMP_TOPIC_ORDER 排；主題表改了兩邊要一起改（2026-10-18）。"""
    import market_calc

    assert tuple(msm.TRUMP_TOPIC_LABELS.values()) == market_calc.TRUMP_TOPIC_ORDER


def test_shared_parse_error_and_cleaner():
    """接縫（2026-10-18）：三個 sources 模組都直接用 market_calc 的 ParseError 與 clean_external。"""
    import market_calc
    import market_sources_tw
    import market_sources_us

    assert msm.ParseError is market_sources_tw.ParseError is market_sources_us.ParseError is market_calc.ParseError
    assert msm._clean is market_calc.clean_external


# ── 錯誤與 import ─────────────────────────────────────────────────────────────


def test_parse_error_kind_is_an_enum_value():
    with pytest.raises(msm.ParseError) as excinfo:
        msm.parse_kalshi_markets([], "CONTROLH-2026-D")
    assert isinstance(excinfo.value, ValueError)
    assert excinfo.value.kind in msm.PARSE_ERROR_KINDS
    assert str(excinfo.value) == excinfo.value.kind  # 訊息只有種類，不帶回應內容


def test_import_does_not_load_network_modules():
    code = (
        "import sys, market_sources_misc\n"
        "loaded = [m for m in ('market_http', 'market_sources_us', 'requests', 'safe_fetch', 'main') if m in sys.modules]\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_values_are_finite_floats():
    out = msm.parse_yahoo_spark(_spark(_spark_entry("GC=F", [_stamp(2026, 10, 16, tz=NY)], [2400])))
    ((day, value),) = out["GC=F"]
    assert isinstance(day, date) and not isinstance(day, datetime)
    assert isinstance(value, float) and math.isfinite(value)
