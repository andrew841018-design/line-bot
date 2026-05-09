"""Tests for `lite_intents_extra._try_bus_arrival`.

驗收項目：
  (a) 抓得到回應：mock requests.get 回固定 HTML/JSON → 回完整訊息
  (b) 沒抓到回 None：route 找不到 / stop 找不到 / RouteDyna 沒對應 sid
  (c) regex 抓 routeNo 失敗時回 None：訊息缺路線 / 缺站名 → None
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import lite_intents_extra as lie


# ── fixture: fake requests.get factory ────────────────────────────────────────


def _make_response(status_code: int = 200, text: str = "", json_data=None):
    """Build a minimal fake requests.Response."""

    class _FakeResp:
        def __init__(self):
            self.status_code = status_code
            self.text = text

        def json(self):
            if json_data is None:
                raise ValueError("no json")
            return json_data

    return _FakeResp()


# ── HTML/JSON fixtures (mimics PDA 5284 real shape) ─────────────────────────


_ROUTELIST_HTML = """
<html><body>
<select name="x">
<option value="0">- 路線 -</option>
<option value="15361">藍7</option>
<option value="16111">307</option>
<option value="11056">552</option>
</select>
</body></html>
"""

# `route.jsp?rid=15361` 的 stop list (片段)
_ROUTE_STOPS_HTML = """
<html><body>
<table>
<tr><td><a href="stop.jsp?sid=12873">捷運市政府站</a></td><td id="tte12873"></td></tr>
<tr><td><a href="stop.jsp?sid=15315">臺北車站(忠孝)</a></td><td id="tte15315"></td></tr>
<tr><td><a href="stop.jsp?sid=99999">不相關站</a></td><td id="tte99999"></td></tr>
</table>
</body></html>
"""

# RouteDyna 真實格式：n1 = "N1,sid,rid,depTime,prev,next,direction,etaSec,seq,..."
_ROUTE_DYNA_JSON = {
    "UpdateTime": "2026-05-08 21:00:00",
    "Stop": [
        {"id": 12873, "n1": "N1,12873,15361,22:00,-1,-1,0,420,5,2,2,260508210311,0,0,0"},
        {"id": 15315, "n1": "N1,15315,15361,21:30,-1,-1,0,-1,12,3,2,260508210140,0,0,0"},
    ],
}


def _route_for_url(url: str, **kw):
    """Dispatch a fake response based on URL path."""
    if "routelist.jsp" in url:
        return _make_response(200, _ROUTELIST_HTML)
    if "route.jsp" in url:
        return _make_response(200, _ROUTE_STOPS_HTML)
    if "RouteDyna" in url:
        return _make_response(200, json.dumps(_ROUTE_DYNA_JSON), json_data=_ROUTE_DYNA_JSON)
    return _make_response(404, "")


# ── (a) Happy path — full pipeline returns formatted text ────────────────────


def test_bus_arrival_happy_path():
    """藍7 + 市政府 → 回包含 '藍7'、'市政府' 與 ETA 文字。"""
    with patch("lite_intents_extra.requests.get", side_effect=_route_for_url):
        out = lie._try_bus_arrival("藍 7 公車 還多久到 市政府站")
    assert out is not None
    assert "藍7" in out
    assert "市政府" in out
    # 420 秒 → 7 分鐘
    assert "7 分鐘" in out
    # 標題 emoji
    assert "🚌" in out


def test_bus_arrival_eta_special_codes():
    """ETA 特殊代碼 '-3' (末班已過) → 顯示『末班已過』。"""
    json_data = {
        "UpdateTime": "x",
        "Stop": [{"id": 12873, "n1": "N1,12873,15361,-1,,,,-3,,3,2,0,0,0,0"}],
    }

    def _dispatch(url, **kw):
        if "routelist.jsp" in url:
            return _make_response(200, _ROUTELIST_HTML)
        if "route.jsp" in url:
            return _make_response(200, _ROUTE_STOPS_HTML)
        if "RouteDyna" in url:
            return _make_response(200, json.dumps(json_data), json_data=json_data)
        return _make_response(404, "")

    with patch("lite_intents_extra.requests.get", side_effect=_dispatch):
        out = lie._try_bus_arrival("藍 7 公車 還多久到 市政府站")
    assert out is not None
    assert "末班已過" in out


def test_bus_arrival_eta_imminent():
    """ETA < 180 秒 → '即將進站'。"""
    json_data = {
        "UpdateTime": "x",
        "Stop": [{"id": 12873, "n1": "N1,12873,15361,21:00,-1,-1,0,90,5,2,2,0,0,0,0"}],
    }

    def _dispatch(url, **kw):
        if "routelist.jsp" in url:
            return _make_response(200, _ROUTELIST_HTML)
        if "route.jsp" in url:
            return _make_response(200, _ROUTE_STOPS_HTML)
        if "RouteDyna" in url:
            return _make_response(200, json.dumps(json_data), json_data=json_data)
        return _make_response(404, "")

    with patch("lite_intents_extra.requests.get", side_effect=_dispatch):
        out = lie._try_bus_arrival("藍 7 公車 還多久到 市政府站")
    assert out is not None
    assert "即將進站" in out


# ── (b) PDA fail → None (TDX env not set) ────────────────────────────────────


def test_bus_arrival_route_not_found_returns_none(monkeypatch):
    """路線名抓不到 (routelist 沒有此 route) → 回 None (without TDX key)。"""
    # 確保沒 TDX env
    monkeypatch.delenv("TDX_CLIENT_ID", raising=False)
    monkeypatch.delenv("TDX_CLIENT_SECRET", raising=False)

    def _dispatch(url, **kw):
        if "routelist.jsp" in url:
            return _make_response(200, _ROUTELIST_HTML)
        return _make_response(404, "")

    with patch("lite_intents_extra.requests.get", side_effect=_dispatch):
        out = lie._try_bus_arrival("999 公車 還多久到 市政府站")
    assert out is None


def test_bus_arrival_stop_not_found_returns_none(monkeypatch):
    """路線找到但站名匹配不到 → 回 None。"""
    monkeypatch.delenv("TDX_CLIENT_ID", raising=False)
    monkeypatch.delenv("TDX_CLIENT_SECRET", raising=False)

    with patch("lite_intents_extra.requests.get", side_effect=_route_for_url):
        out = lie._try_bus_arrival("藍 7 公車 還多久到 不存在站")
    assert out is None


def test_bus_arrival_routedyna_empty_returns_none(monkeypatch):
    """RouteDyna 不含此 sid (匹配不到 ETA) → 回 None。"""
    monkeypatch.delenv("TDX_CLIENT_ID", raising=False)
    monkeypatch.delenv("TDX_CLIENT_SECRET", raising=False)

    json_data = {"UpdateTime": "x", "Stop": []}

    def _dispatch(url, **kw):
        if "routelist.jsp" in url:
            return _make_response(200, _ROUTELIST_HTML)
        if "route.jsp" in url:
            return _make_response(200, _ROUTE_STOPS_HTML)
        if "RouteDyna" in url:
            return _make_response(200, json.dumps(json_data), json_data=json_data)
        return _make_response(404, "")

    with patch("lite_intents_extra.requests.get", side_effect=_dispatch):
        out = lie._try_bus_arrival("藍 7 公車 還多久到 市政府站")
    assert out is None


# ── (c) regex / parse failures → None ────────────────────────────────────────


def test_bus_arrival_no_route_in_text_returns_none():
    """文字無路線編號 → 直接回 None，不發 HTTP。"""
    with patch("lite_intents_extra.requests.get") as mocked:
        out = lie._try_bus_arrival("公車")
    assert out is None
    assert not mocked.called  # 沒發 HTTP


def test_bus_arrival_no_stop_in_text_returns_none():
    """有路線但無站名 → 回 None，不發 HTTP。"""
    with patch("lite_intents_extra.requests.get") as mocked:
        out = lie._try_bus_arrival("307 還有多久")
    assert out is None
    assert not mocked.called


def test_bus_arrival_no_trigger_word_returns_none():
    """完全沒有 trigger 關鍵字 → 回 None，不發 HTTP，不解析。"""
    with patch("lite_intents_extra.requests.get") as mocked:
        out = lie._try_bus_arrival("今天天氣很好")
    assert out is None
    assert not mocked.called


def test_bus_arrival_year_not_treated_as_route():
    """年份字 (2026) 不可被當路線。沒站名 → None；有站名 → 也應 None (regex 過濾年份)。"""
    with patch("lite_intents_extra.requests.get") as mocked:
        out = lie._try_bus_arrival("2026 公車 到 市政府站 還多久")
    # 年份過濾後就沒 route，回 None
    assert out is None


# ── (d) extract helper unit-tests ────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("藍 7 公車 還多久到 市政府站", ("藍7", "市政府")),
        ("307 公車 到 台大醫院 還多久", ("307", "台大醫院")),
        ("公車 235 到 公館 幾分鐘", ("235", "公館")),
        ("棕 1 到 動物園 還多久", ("棕1", "動物園")),
        ("紅 5 公車 還多久到 市政府站", ("紅5", "市政府")),
        ("公車", None),
        ("307 還有多久", None),
        ("今天天氣很好", None),
    ],
)
def test_extract_route_and_stop(text, expected):
    assert lie._bus_extract_route_and_stop(text) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0", "進站中"),
        ("-1", "未發車"),
        ("-3", "末班已過"),
        ("-4", "今日未營運"),
        ("90", "即將進站"),    # < 180 sec
        ("420", "7 分鐘"),     # 420/60 = 7
        ("3600", "60 分鐘"),
        ("abc", "未知"),
    ],
)
def test_format_eta(raw, expected):
    assert lie._bus_format_eta(raw) == expected
