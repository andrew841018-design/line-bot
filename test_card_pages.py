"""字卡大字版網頁（2026-10-16）：card_pages.py 與 main 的接線。

2026-10-17：只有股票、FCN 傳 page=True；爸媽健康卡（同樣走 _reply_command_card）不做網頁。
卡片一律是合成的；對外網址用暫存的假 tunnel 檔（和 cloudflared wrapper 一樣結尾有換行）；
頁面寫進每個測試自己的暫存目錄。conftest 預設關掉這個功能，這裡的 fixture 才打開。
"""

from __future__ import annotations

import html
import json
import os
import re
import stat
import time
from collections import OrderedDict
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient
from linebot.v3.messaging import FlexContainer, FlexMessage  # type: ignore[import-untyped]

import card_pages
import fcn_eval
import main
import stock_picks

BASE = "https://quiet-river-1234.trycloudflare.com"
T = date(2026, 10, 8)
PAGE = BASE + "/card/" + "a" * 22
AT = "@"  # 帶帳密的網址寫成變數：git 隱私稽核會把「字@網域」當成 email


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    monkeypatch.setattr(card_pages, "_MINTED", OrderedDict())


@pytest.fixture
def pages(tmp_path, monkeypatch):
    tunnel = tmp_path / "tunnel_url.txt"
    tunnel.write_text(BASE + "\n", encoding="utf-8")  # wrapper 寫的就是「網址＋換行」、0600
    tunnel.chmod(0o600)
    monkeypatch.setattr(card_pages, "TUNNEL_URL_FILE", str(tunnel))
    monkeypatch.setenv(card_pages.ENABLED_ENV, "1")
    monkeypatch.setenv(card_pages.DIR_ENV, str(tmp_path / "card_pages"))
    monkeypatch.delenv(card_pages.BASE_URL_ENV, raising=False)
    monkeypatch.delenv(card_pages.TTL_ENV, raising=False)
    monkeypatch.setattr(main.settings, "bot_muted", False)
    return SimpleNamespace(dir=tmp_path / "card_pages", tunnel=tunnel)


# ── 合成卡片 ──────────────────────────────────────────────────────────────────


def _text(text, **extra):
    return {"type": "text", "text": text, "wrap": True, **extra}


def _bubble(title="標題", lines=("第一行",), footer=True):
    bubble = {
        "type": "bubble",
        "size": "giga",
        "header": {"type": "box", "layout": "vertical", "contents": [_text(title, size="xl", weight="bold")]},
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": [_text(t) for t in lines]},
    }
    if footer:
        bubble["footer"] = {
            "type": "box",
            "layout": "vertical",
            "contents": [
                _text("只供參考", size="sm", color="#555555"),
                {"type": "button", "style": "primary", "action": {"type": "message", "label": "重算", "text": "/FCN 重算"}},
            ],
        }
    return bubble


def _facts(code, name, kind=stock_picks.STOCK):
    stock = kind == stock_picks.STOCK
    return stock_picks.Facts(
        code=code, name=name, kind=kind, data_date=T,
        trend=stock_picks.Trend(close=100.0, ma60=99.8, ma240=90.0, p60=2),
        revenue=stock_picks.Revenue(2026, 8, 10.0, 5.0) if stock else None,
        pe=10.0 if stock else None, pe_median=20.0 if stock else None,
        inst_net=1_000_000 if stock else None, cap=1e12 if stock else None,
    )


def _picks_card():
    picks = [_facts("2344", "華邦電"), _facts("3231", "緯創"), _facts("2330", "台積電")]
    bubbles = [stock_picks.pick_bubble(f, i + 1, 3, None, lead_note=(i == 0)) for i, f in enumerate(picks)]
    bubbles.append(stock_picks.faq_bubble([_facts("0050", "元大台灣50", stock_picks.ETF), picks[2]], T))
    return stock_picks.carousel(bubbles), stock_picks.picks_alt_text(picks, T, not_updated=False)


def _fcn_cards():
    req = fcn_eval.FcnRequest(symbols=("NVDA", "TSLA"), rate=0.18, terms=fcn_eval.Terms())
    stats = [
        fcn_eval.TickerStats(symbol=s, market="US", currency="USD", dates=(T,), closes=np.array([1.0]),
                             spot=100.0, last_date=T, vol=0.5, div_yield=0.0, fetched_at=0.0)
        for s in req.symbols
    ]
    sim = fcn_eval.SimResult(fair=0.30, p_convert=0.35, convert_counts=(700, 700), breach_counts=(710, 710),
                             corr_note=False)
    verdict = fcn_eval.judge(0.18, 0.30, 0.04, False)
    income = {s: (1.0, 2.0, 3.0, 4.0) for s in req.symbols}
    return [fcn_eval.build_card(req, stats, sim, verdict, 0.04, "USD", income), fcn_eval.build_confirm_card(req, [])]


def _shapes():
    """股票推薦組、沒推薦（兩種）、單檔（股票／ETF）、FCN 評估卡、FCN 確認卡。"""
    stock_detail = _facts("2330", "台積電")
    etf = _facts("0050", "元大台灣50", stock_picks.ETF)
    return [
        _picks_card(),
        (stock_picks.no_pick_bubble(T, None, None, not_updated=True, lead_note=True), "沒推薦：資料沒更新"),
        (stock_picks.no_pick_bubble(T, None, (stock_picks.CONDITION_ORDER[0], 3, 50), not_updated=False), "沒推薦：沒有符合的"),
        (stock_picks.detail_bubble(stock_detail, market=None), stock_picks.detail_alt_text(stock_detail)),
        (stock_picks.detail_bubble(etf, market=None), stock_picks.detail_alt_text(etf)),
        *_fcn_cards(),
    ]


def _page_links(card):
    """每張 bubble footer 裡連到大字版的 action（照 bubble 順序）。"""
    found = []
    for bubble in card_pages._bubbles(card):
        found.append([
            node["action"] for node in bubble.get("footer", {}).get("contents", [])
            if isinstance(node.get("action"), dict) and node["action"].get("type") == "uri"
        ])
    return found


def _token(url):
    return re.fullmatch(re.escape(BASE) + r"/card/([A-Za-z0-9_-]{22})(?:#b\d+)?", url).group(1)


# ── 網頁內容 ──────────────────────────────────────────────────────────────────


def test_the_page_escapes_text_and_loads_nothing_from_outside():
    card = _bubble(title='<script>alert("x")</script>', lines=("A & B", "第一行\n第二行"))
    page = card_pages.render_html(card, 'title "<b>"')
    assert "<script" not in page and "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page
    assert "A &amp; B" in page and "第一行<br>第二行" in page
    assert "<title>title &quot;&lt;b&gt;&quot;</title>" in page
    assert "http://" not in page and "https://" not in page and "src=" not in page and "@import" not in page
    assert '<meta name="format-detection" content="telephone=no">' in page and 'lang="zh-Hant"' in page


def test_sizes_become_large_print_and_bad_colors_are_dropped():
    card = _bubble(lines=())
    card["body"]["contents"] = [
        _text("小", size="xxs"), _text("中"), _text("紅", color="#C53030"),
        _text("壞", color="red;background:url(x)"), _text("粗", weight="bold"), _text("怪", size="99px"),
    ]
    page = card_pages.render_html(card, "t")
    assert '<p style="font-size:17px">小</p>' in page and '<p style="font-size:22px">中</p>' in page
    assert "color:#C53030" in page and '<p style="font-size:22px">壞</p>' in page and "url(" not in page
    assert "font-weight:700" in page and '<p style="font-size:22px">怪</p>' in page
    assert min(int(x) for x in re.findall(r"font-size:(\d+)px", page)) >= 17


def test_buttons_and_clickable_boxes_do_not_appear_on_the_page():
    card = _bubble()
    card["footer"]["contents"].append(card_pages.page_button(PAGE))
    page = card_pages.render_html(card, "t")
    assert "重算" not in page and "/FCN" not in page and card_pages.BUTTON_LABEL not in page
    assert "只供參考" in page


def test_a_horizontal_box_becomes_a_wrapping_row():
    card = _bubble(lines=())
    card["body"]["contents"] = [{"type": "box", "layout": "horizontal", "contents": [_text("左"), _text("右")]}]
    assert '<div class="row" style=""><p style="font-size:22px">左</p><p' in card_pages.render_html(card, "t").replace("\n", "")


def test_deeply_nested_boxes_do_not_blow_up():
    node = _text("底")
    for _ in range(200):
        node = {"type": "box", "layout": "vertical", "contents": [node]}
    card = _bubble(lines=())
    card["body"]["contents"] = [node]
    assert "<html" in card_pages.render_html(card, "t")


def test_the_note_tells_when_it_was_made_and_that_the_link_can_expire():
    page = card_pages.render_html(_bubble(), "t", now=1792146600)  # 2026-10-16 18:30 台北
    assert "10/16 18:30 產生" in page and "可能提早失效" in page and "重叫一次卡片" in page


# ── 錨點：按鈕和網頁用同一份編號 ────────────────────────────────────────────────


@pytest.mark.parametrize("index", range(7))
def test_every_link_lands_on_its_own_card(index):
    card, alt = _shapes()[index]
    page = card_pages.render_html(card, alt)
    links = _page_links(card_pages.with_page_button(card, PAGE))
    bubbles = card_pages._bubbles(card)
    assert [len(x) for x in links] == [1] * len(bubbles)
    for number, (action,) in enumerate(links, 1):
        if card.get("type") == "carousel":
            assert action["uri"] == f"{PAGE}#b{number}" and f'id="b{number}"' in page
        else:
            assert action["uri"] == PAGE and 'id="b' not in page


def test_the_original_card_is_not_changed_and_a_missing_footer_is_added():
    card = {"type": "carousel", "contents": [_bubble(), _bubble(footer=False)]}
    paged = card_pages.with_page_button(card, PAGE)
    assert "footer" not in card["contents"][1] and len(card["contents"][0]["footer"]["contents"]) == 2
    assert [len(x) for x in _page_links(paged)] == [1, 1]
    assert paged["contents"][0]["footer"]["contents"][-1]["action"]["uri"] == PAGE + "#b1"


# ── 每一種卡片加了按鈕之後 ─────────────────────────────────────────────────────


@pytest.mark.parametrize("index", range(7))
def test_every_card_shape_stays_sendable_with_the_button(index):
    card, alt = _shapes()[index]
    card_pages._remember(PAGE)
    paged = card_pages.with_page_button(card, PAGE)
    assert card_pages.fits_line_limits(paged)
    message = main._validated_flex_card_message(alt, paged)
    assert isinstance(message, FlexMessage)
    assert FlexContainer.from_dict(paged).to_dict() is not None
    assert len(json.dumps(paged, ensure_ascii=False).encode()) <= 30 * 1024
    for (action,) in _page_links(paged):
        assert len(action["label"]) <= 20 and len(action["uri"]) <= 1000
    for bubble in card_pages._bubbles(paged):  # 按鈕一律在 footer 最後，舊的 contents[0] 不變
        assert bubble["footer"]["contents"][-1]["action"]["type"] == "uri"


# ── 對外網址 ──────────────────────────────────────────────────────────────────


def test_the_base_comes_from_the_tunnel_file_even_with_a_trailing_newline(pages):
    assert card_pages.public_base_url() == BASE


@pytest.mark.parametrize(
    "content",
    ["", "\n", "http://quiet-river-1234.trycloudflare.com", "https://evil.example.com",
     "https://quiet-river-1234.trycloudflare.com/x", "https://api.trycloudflare.com",
     "https://Quiet-River-1234.trycloudflare.com", "https://a.trycloudflare.com.evil.io",
     f"https://user{AT}quiet-river-1234.trycloudflare.com", "https://-quiet-river.trycloudflare.com"],
)
def test_a_malformed_tunnel_file_means_no_page(pages, content):
    pages.tunnel.write_text(content, encoding="utf-8")
    pages.tunnel.chmod(0o600)
    assert card_pages.public_base_url() is None
    assert card_pages.publish(_bubble(), "t") is None


def test_a_missing_tunnel_file_means_no_page(pages):
    pages.tunnel.unlink()
    assert card_pages.public_base_url() is None


def test_a_tunnel_file_others_could_have_written_is_ignored(pages, monkeypatch, tmp_path):
    pages.tunnel.chmod(0o644)
    assert card_pages.public_base_url() is None
    pages.tunnel.chmod(0o600)
    monkeypatch.setattr(card_pages.os, "geteuid", lambda: os.getuid() + 1)  # 別人的檔
    assert card_pages.public_base_url() is None


def test_a_symlink_or_fifo_tunnel_file_is_ignored_without_blocking(pages, tmp_path):
    real = tmp_path / "real_url.txt"
    real.write_text(BASE, encoding="utf-8")
    real.chmod(0o600)
    pages.tunnel.unlink()
    pages.tunnel.symlink_to(real)
    assert card_pages.public_base_url() is None
    pages.tunnel.unlink()
    os.mkfifo(pages.tunnel, 0o600)
    started = time.monotonic()
    assert card_pages.public_base_url() is None and time.monotonic() - started < 2


def test_the_env_override_wins_and_a_bad_override_disables_pages(pages, monkeypatch):
    monkeypatch.setenv(card_pages.BASE_URL_ENV, "https://bot.example.org/")
    assert card_pages.public_base_url() == "https://bot.example.org"
    url = card_pages.publish(_bubble(), "t")
    assert url.startswith("https://bot.example.org/card/")
    assert card_pages.page_action_ok({"type": "uri", "label": card_pages.BUTTON_LABEL, "uri": url})
    monkeypatch.setenv(card_pages.BASE_URL_ENV, "https://bot.example.org:8443")
    assert card_pages.public_base_url() == "https://bot.example.org:8443"
    for bad in (
        "http://bot.example.org", f"https://u{AT}bot.example.org", "https://bot.example.org/x",
        "https://bot..example.org", "https://bot.example.org:99999", "https://bot.example.org:0",
    ):
        monkeypatch.setenv(card_pages.BASE_URL_ENV, bad)
        assert card_pages.public_base_url() is None and card_pages.publish(_bubble(), "t") is None


def test_the_image_url_override_is_not_used_for_pages(pages, monkeypatch):
    monkeypatch.setenv("IMAGE_GEN_PUBLIC_URL", "https://old-dead-host.trycloudflare.com")
    assert card_pages.public_base_url() == BASE


# ── 開關 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["0", "false", "OFF", " no "])
def test_any_off_value_disables_pages(pages, monkeypatch, value):
    monkeypatch.setenv(card_pages.ENABLED_ENV, value)
    assert card_pages.publish(_bubble(), "t") is None and not pages.dir.exists()


def test_the_feature_is_on_unless_turned_off(monkeypatch):
    monkeypatch.delenv(card_pages.ENABLED_ENV, raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert card_pages.enabled() is True


def test_cloud_run_never_makes_or_serves_pages(pages, monkeypatch):
    token = _token(card_pages.publish(_bubble(), "t"))
    monkeypatch.setenv("K_SERVICE", "line-bot")
    assert card_pages.publish(_bubble(), "t") is None
    assert TestClient(main.app).get(f"/card/{token}").status_code == 404


def test_the_note_never_promises_more_days_than_the_page_lasts(monkeypatch):
    monkeypatch.setenv(card_pages.TTL_ENV, "1.5")
    assert "最多保留 2 天" in card_pages.render_html(_bubble(), "t")


@pytest.mark.parametrize("value, days", [("abc", 7), ("nan", 7), ("0", 1), ("99", 30), ("3", 3)])
def test_the_ttl_is_parsed_leniently_and_clamped(monkeypatch, value, days):
    monkeypatch.setenv(card_pages.TTL_ENV, value)
    assert card_pages.ttl_seconds() == days * 86400


# ── 存頁與清理 ────────────────────────────────────────────────────────────────


def test_publish_writes_a_private_page_under_a_random_token(pages):
    url = card_pages.publish(_bubble(), "t")
    token = _token(url)
    path = pages.dir / f"{token}.html"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(pages.dir.stat().st_mode) == 0o700
    assert "標題" in path.read_text(encoding="utf-8")
    assert _token(card_pages.publish(_bubble(), "t")) != token


def test_a_failing_render_never_raises(pages, monkeypatch):
    monkeypatch.setattr(card_pages, "render_html", lambda *_a, **_k: 1 / 0)
    assert card_pages.publish(_bubble(), "t") is None


def test_a_failing_cleanup_still_returns_the_written_page(pages, monkeypatch):
    monkeypatch.setattr(card_pages, "_prune", lambda *_a, **_k: 1 / 0)
    url = card_pages.publish(_bubble(), "t")
    assert (pages.dir / f"{_token(url)}.html").exists()


def _age(path, seconds):
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_cleanup_only_touches_its_own_files(pages, monkeypatch, tmp_path):
    monkeypatch.setattr(card_pages, "MAX_PAGES", 3)
    pages.dir.mkdir(mode=0o700)
    old = pages.dir / ("o" * 22 + ".html")
    old.write_text("old", encoding="utf-8")
    _age(old, 8 * 86400)
    stranger = pages.dir / "notes.html"
    stranger.write_text("keep", encoding="utf-8")
    _age(stranger, 30 * 86400)
    outside = tmp_path / "outside.html"
    outside.write_text("outside", encoding="utf-8")
    _age(outside, 30 * 86400)
    link = pages.dir / ("l" * 22 + ".html")
    link.symlink_to(outside)
    stale_tmp = pages.dir / ".card-abcdefgh.tmp"
    stale_tmp.write_text("x", encoding="utf-8")
    _age(stale_tmp, 2 * 3600)
    fresh_tmp = pages.dir / ".card-ijklmnop.tmp"
    fresh_tmp.write_text("x", encoding="utf-8")
    urls = [card_pages.publish(_bubble(), "t") for _ in range(5)]
    pages_left = {p.name for p in pages.dir.glob("*.html") if not p.is_symlink() and p.name != "notes.html"}
    assert not old.exists() and len(pages_left) == 3 and f"{_token(urls[-1])}.html" in pages_left
    assert stranger.exists() and link.is_symlink() and outside.exists()
    assert not stale_tmp.exists() and fresh_tmp.exists()


# ── 路由 ─────────────────────────────────────────────────────────────────────


def test_the_page_is_served_with_private_headers(pages):
    token = _token(card_pages.publish(_bubble(), "t"))
    response = TestClient(main.app).get(f"/card/{token}")
    assert response.status_code == 200 and "標題" in response.text
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "style-src 'unsafe-inline'" in csp  # 少了 style-src，大字全部失效


@pytest.mark.parametrize("path", ["/card/short", "/card/" + "a" * 23, "/card/" + "a" * 21 + "%2E", "/card/" + "a" * 21 + "~"])
def test_bad_tokens_get_the_same_404(pages, path):
    response = TestClient(main.app).get(path)
    assert response.status_code == 404 and "已經失效" in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/card/..%2F..%2Fetc%2Fpasswd", "/card/" + "a" * 21 + "%2F", "/card/a/../../main.py"])
def test_paths_with_slashes_never_reach_a_file(pages, path):
    pages.dir.mkdir(mode=0o700)
    response = TestClient(main.app).get(path)
    assert response.status_code == 404 and "import" not in response.text


def test_unknown_expired_or_disabled_pages_are_404(pages, monkeypatch):
    client = TestClient(main.app)
    assert client.get("/card/" + "z" * 22).status_code == 404
    token = _token(card_pages.publish(_bubble(), "t"))
    path = pages.dir / f"{token}.html"
    _age(path, 8 * 86400)
    assert client.get(f"/card/{token}").status_code == 404
    os.utime(path, None)
    assert client.get(f"/card/{token}").status_code == 200
    monkeypatch.setenv(card_pages.ENABLED_ENV, "0")
    assert client.get(f"/card/{token}").status_code == 404


def test_an_unsafe_pages_directory_is_never_used(pages, tmp_path, monkeypatch):
    pages.dir.mkdir(mode=0o700)
    pages.dir.chmod(0o777)
    assert card_pages.publish(_bubble(), "t") is None
    pages.dir.chmod(0o755)  # 別人列得出檔名（檔名就是 token）也不行
    assert card_pages.publish(_bubble(), "t") is None
    pages.dir.chmod(0o700)
    token = _token(card_pages.publish(_bubble(), "t"))
    elsewhere = tmp_path / "elsewhere"
    pages.dir.rename(elsewhere)
    pages.dir.symlink_to(elsewhere)
    assert card_pages.publish(_bubble(), "t") is None
    assert TestClient(main.app).get(f"/card/{token}").status_code == 404


def test_a_fifo_named_like_a_page_is_404_without_blocking(pages):
    pages.dir.mkdir(mode=0o700)
    os.mkfifo(pages.dir / ("f" * 22 + ".html"), 0o600)
    started = time.monotonic()
    assert TestClient(main.app).get("/card/" + "f" * 22).status_code == 404
    assert time.monotonic() - started < 2


def test_a_symlink_or_a_directory_with_a_token_name_is_404(pages, tmp_path):
    pages.dir.mkdir(mode=0o700)
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    (pages.dir / ("s" * 22 + ".html")).symlink_to(secret)
    (pages.dir / ("d" * 22 + ".html")).mkdir()
    client = TestClient(main.app)
    assert client.get("/card/" + "s" * 22).status_code == 404
    assert client.get("/card/" + "d" * 22).status_code == 404


# ── 卡片白名單 ────────────────────────────────────────────────────────────────


def _card_with_action(action, *, box=False):
    card = _bubble()
    node = {"type": "box", "layout": "vertical", "contents": [_text("按")], "action": action} if box else {
        "type": "button", "action": action}
    card["footer"]["contents"].append(node)
    return card


@pytest.mark.parametrize("box", [False, True])
def test_the_whitelist_accepts_only_this_page_link(box):
    card_pages._remember(PAGE)
    good = {"type": "uri", "label": "🔍 看大字版", "uri": PAGE + "#b2"}
    assert "🔍 看大字版" in main._flex_card_texts(_card_with_action(good, box=box))
    main._validated_flex_card_message("alt", _card_with_action(good, box=box))


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example.com/card/" + "a" * 22,
        "http://quiet-river-1234.trycloudflare.com/card/" + "a" * 22,
        "https://api.trycloudflare.com/card/" + "a" * 22,
        f"https://user{AT}quiet-river-1234.trycloudflare.com/card/" + "a" * 22,
        "https://quiet-river-1234.trycloudflare.com:8443/card/" + "a" * 22,
        "https://quiet-river-1234.trycloudflare.com./card/" + "a" * 22,
        PAGE + "?x=1",
        PAGE + "/more",
        PAGE + "#b0",
        PAGE + "#b100",
        PAGE + "#b1#b2",
        PAGE + "\\",
        PAGE + "\n",
        BASE + "/card/" + "a" * 21 + "%41",
        BASE + "/static/img/" + "a" * 32 + ".png",
        BASE.upper() + "/card/" + "a" * 22,
        "https://www.trycloudflare.com/card/" + "a" * 22,
        "https://evil-phish-login.trycloudflare.com/card/" + "a" * 22,
        BASE + "/card/" + "b" * 22,  # 格式對，但不是這個程序發的
    ],
)
def test_the_whitelist_rejects_every_other_link(uri):
    """label 用對的「🔍 看大字版」、只有網址不對：這樣測到的才是網址的檢查，不是先被 label 擋掉。"""
    card_pages._remember(PAGE)
    action = {"type": "uri", "label": card_pages.BUTTON_LABEL, "uri": uri}
    assert card_pages.page_action_ok(action) is False
    with pytest.raises(ValueError):
        main._flex_card_texts(_card_with_action(action))


@pytest.mark.parametrize(
    "action",
    [
        {"type": "uri", "label": "看大字版", "uri": PAGE},  # 只有 label 不對
        {"type": "uri", "label": "🔍 看大字版", "uri": PAGE, "altUri": {"desktop": "https://evil.example.com"}},
        {"type": "uri", "label": "🔍 看大字版"},
        {"type": "postback", "label": "🔍 看大字版", "data": "a=1"},
        "not a dict",
    ],
)
def test_the_whitelist_rejects_wrong_labels_extra_keys_and_other_types(action):
    card_pages._remember(PAGE)
    assert card_pages.page_action_ok(action) is False
    with pytest.raises(ValueError):
        main._flex_card_texts(_card_with_action(action))


def test_only_links_minted_by_this_process_pass_and_old_ones_age_out(monkeypatch):
    monkeypatch.setattr(card_pages, "_MINTED_MAX", 2)
    links = [BASE + "/card/" + c * 22 for c in "abc"]
    for link in links:
        card_pages._remember(link)
    ok = [card_pages.page_action_ok({"type": "uri", "label": card_pages.BUTTON_LABEL, "uri": link}) for link in links]
    assert ok == [False, True, True]


def test_without_the_module_every_link_is_rejected(monkeypatch):
    monkeypatch.setattr(main, "card_pages", None)
    with pytest.raises(ValueError):
        main._flex_card_texts(_card_with_action({"type": "uri", "label": "x", "uri": PAGE}))


def test_the_label_still_goes_through_the_outbound_validator(monkeypatch):
    card_pages._remember(PAGE)
    seen = []
    monkeypatch.setattr(
        main.output_validator, "validate_outbound_text",
        lambda text: seen.append(text) or SimpleNamespace(ok=True, text=text),
    )
    main._validated_flex_card_message("alt", _card_with_action(
        {"type": "uri", "label": card_pages.BUTTON_LABEL, "uri": PAGE}
    ))
    assert card_pages.BUTTON_LABEL in seen


@pytest.mark.parametrize("index", range(7))
def test_the_page_shows_only_text_the_validator_already_approved(index):
    card, alt = _shapes()[index]
    page = card_pages.render_html(card, alt)
    approved = set(main._flex_card_texts(card))
    shown = [html.unescape(t).replace("<br>", "\n") for t in re.findall(r'<p style="[^"]*">(.*?)</p>', page)]
    assert shown and all(text in approved for text in shown)


def test_an_oversized_card_is_sent_without_the_button(pages, monkeypatch):
    card, alt = _picks_card()
    monkeypatch.setattr(card_pages, "MAX_CARD_BYTES", 1000)
    assert _send(card, alt).kwargs["flex_card"] == card


# ── 送卡接線 ──────────────────────────────────────────────────────────────────


def _send(card, alt, *, page=True):
    event = SimpleNamespace(reply_token="TOKEN_CARD_PAGE")
    with patch("main._reply") as reply, patch("main._clear_reply_mention_targets"):
        main._reply_command_card(
            event, "G_TEST", "/股票", reply_text=None, card=card, alt_text=alt, fallback_text="固定句", page=page
        )
    reply.assert_called_once()
    return reply.call_args


# ── 只有股票、FCN 加按鈕（2026-10-17）：爸媽健康卡也走 _reply_command_card，但絕不做網頁 ──────────


def test_the_page_is_added_only_when_the_caller_asks(pages):
    card, alt = _picks_card()
    assert _send(card, alt, page=False).kwargs["flex_card"] == card
    assert not pages.dir.exists()


def test_a_health_card_never_gets_a_page_even_if_asked(pages):
    card = _bubble(title="爸媽健康", lines=("近 7 天的紀錄",), footer=False)
    alt = "爸媽健康卡：近 7 天紀錄（點開看）"
    assert main._with_card_page(card, alt) is card
    sent = _send(card, alt, page=True).kwargs["flex_card"]
    assert sent == card and _page_links(sent) == [[]]
    assert not pages.dir.exists()


@pytest.mark.parametrize("kind", ["stock", "fcn"])
def test_stock_and_fcn_replies_ask_for_the_page(pages, monkeypatch, kind):
    card, alt = _picks_card() if kind == "stock" else _shapes()[5]
    monkeypatch.setattr(main, "_handle_stock_command", lambda *_a: (None, card, alt, "固定句"))
    monkeypatch.setattr(main, "_handle_fcn_command", lambda *_a: (None, card, alt))
    event = SimpleNamespace(reply_token="TOKEN_CARD_PAGE")
    with patch("main._reply") as reply, patch("main._clear_reply_mention_targets"):
        if kind == "stock":
            main._reply_stock_picks(event, "G_TEST", "/股票", "")
        else:
            main._reply_fcn(event, "G_TEST", "/FCN", "")
    links = _page_links(reply.call_args.kwargs["flex_card"])
    assert links and all(len(x) == 1 for x in links)


@pytest.mark.parametrize(
    "alt, gets_page",
    [
        ("股票推薦：只供參考｜華邦電 2344｜資料到 10/08", True),
        ("股票評估：只供參考｜台積電 2330｜✅ 符合 5 條", True),
        ("FCN 評估：NVDA、TSLA｜偏低", True),
        ("FCN 評估：我讀到 NVDA、4417｜年利率 18%，點按鈕評估", False),  # 確認卡：沒標籤的數字可能是帳號尾碼
        ("爸媽健康卡：近 7 天紀錄（點開看）", False),
        ("爸媽健康卡：點開看爸媽近 14 天的分析", False),  # 2026-10-17 分析卡
        ("咪寶選單：點開看按鈕", False),
        ("其他卡片", False),
    ],
)
def test_only_stock_and_fcn_evaluation_cards_get_a_page(pages, alt, gets_page):
    card = _bubble(footer=False)
    sent = main._with_card_page(card, alt)
    assert (sent is not card) is gets_page
    assert pages.dir.exists() is gets_page


def test_the_fcn_confirm_card_is_sent_without_a_page(pages):
    card, alt = _fcn_cards()[1]
    assert _send(card, alt).kwargs["flex_card"] == card
    assert not pages.dir.exists()


def test_the_page_prefixes_follow_the_card_builders():
    assert main._CARD_PAGE_ALT_PREFIXES == (stock_picks.PREFIX, stock_picks.EVAL_PREFIX, fcn_eval.PREFIX)
    (_eval_card, eval_alt), (_confirm_card, confirm_alt) = _fcn_cards()
    assert eval_alt.startswith(fcn_eval.PREFIX) and not eval_alt.startswith(main._CARD_PAGE_SKIP_ALT_PREFIX)
    assert confirm_alt.startswith(main._CARD_PAGE_SKIP_ALT_PREFIX)
    assert _picks_card()[1].startswith(stock_picks.PREFIX)
    assert stock_picks.detail_alt_text(_facts("2330", "台積電")).startswith(stock_picks.EVAL_PREFIX)


def test_a_text_reply_never_publishes_a_page(pages, monkeypatch):
    publish = MagicMock(side_effect=AssertionError("text replies must not publish"))
    monkeypatch.setattr(card_pages, "publish", publish)
    event = SimpleNamespace(reply_token="TOKEN_CARD_PAGE")
    with patch("main._reply") as reply, patch("main._clear_reply_mention_targets"):
        main._reply_command_card(
            event, "G_TEST", "/股票", reply_text="股票推薦：文字", card=None, alt_text=None, fallback_text="固定句",
            page=True,
        )
    assert reply.call_args.args[1] == "股票推薦：文字" and "flex_card" not in reply.call_args.kwargs
    publish.assert_not_called()


def test_a_command_card_is_sent_with_a_working_page_button(pages):
    card, alt = _picks_card()
    call = _send(card, alt)
    sent = call.kwargs["flex_card"]
    assert call.args[1] == alt
    links = _page_links(sent)
    assert [len(x) for x in links] == [1, 1, 1, 1]
    url = links[0][0]["uri"]
    assert url.endswith("#b1") and links[3][0]["uri"].endswith("#b4")
    main._validated_flex_card_message(alt, sent)  # _reply 裡會再驗一次，同樣要過
    response = TestClient(main.app).get(url.split("#")[0].removeprefix(BASE))
    assert response.status_code == 200 and "華邦電" in response.text and "緯創" in response.text


@pytest.mark.parametrize("why", ["muted", "no_base", "no_module", "publish_raises", "button_rejected"])
def test_whenever_the_page_is_not_possible_the_original_card_is_sent(pages, monkeypatch, why):
    card, alt = _picks_card()
    if why == "muted":
        monkeypatch.setattr(main.settings, "bot_muted", True)
    elif why == "no_base":
        pages.tunnel.unlink()
    elif why == "no_module":
        monkeypatch.setattr(main, "card_pages", None)
    elif why == "publish_raises":
        monkeypatch.setattr(card_pages, "publish", lambda *_a: (_ for _ in ()).throw(RuntimeError("boom")))
    else:
        monkeypatch.setattr(card_pages, "page_action_ok", lambda _action: False)
    assert _send(card, alt).kwargs["flex_card"] == card
    if why in ("muted", "no_base", "no_module"):
        assert not pages.dir.exists()


def test_a_card_that_fails_the_first_check_gets_the_fixed_text_and_no_page(pages):
    card = _bubble()
    card["body"]["contents"].append({"type": "image", "url": "https://example.com/x.png"})
    call = _send(card, "alt")
    assert call.kwargs.get("flex_card") is None and call.args[1] == "固定句"
    assert not pages.dir.exists()


def test_the_real_reply_sends_the_card_with_the_button(pages):
    card, alt = _picks_card()
    paged = card_pages.with_page_button(card, card_pages.publish(card, alt))
    api = MagicMock()
    api.reply_message.return_value = SimpleNamespace(sent_messages=[SimpleNamespace(id="SENT_CARD", quote_token=None)])
    with (
        patch("main.MessagingApi", return_value=api),
        patch("main.ApiClient"),
        patch.object(main, "_get_line_config", return_value=MagicMock()),
        patch.object(main.memory, "log_raw_message"),
        patch("reminder_push.due_reminders_for_reply"),
    ):
        assert main._reply("TOKEN_CARD_PAGE", alt, group_id="G_TEST", flex_card=paged, index_for_recall=False) is True
    sent = api.reply_message.call_args[0][0].messages
    assert len(sent) == 1 and isinstance(sent[0], FlexMessage)
    assert sent[0].contents.to_dict()["contents"][0]["footer"]["contents"][-1]["action"]["type"] == "uri"
