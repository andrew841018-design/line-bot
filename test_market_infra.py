"""市場盤勢（2026-10-17）的基礎設施測試：market_paths（私有目錄與檔案）、market_http（白名單、
狀態碼、冷卻）、/static/img 路由（0700 目錄、O_NOFOLLOW、fd 串流）。"""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import market_http  # noqa: E402
import market_paths  # noqa: E402


# ── market_paths ────────────────────────────────────────────────────────────
def test_private_dir_is_created_0700_and_others_are_refused(tmp_path):
    target = tmp_path / "charts"
    assert market_paths.private_dir_ok(target)
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o700
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    os.chmod(loose, 0o755)
    assert not market_paths.private_dir_ok(loose)
    assert stat.S_IMODE(os.lstat(loose).st_mode) == 0o755  # 不去 chmod
    link = tmp_path / "link"
    os.symlink(target, link)
    assert not market_paths.private_dir_ok(link)
    assert not market_paths.private_dir_ok(tmp_path / "absent", create=False)


def test_state_dir_is_never_created(tmp_path):
    state = tmp_path / "state"
    assert not market_paths.state_dir_ok(state)
    assert not state.exists()
    state.mkdir(mode=0o700)
    assert market_paths.state_dir_ok(state)


def test_atomic_write_is_private_and_leaves_no_temp(tmp_path):
    path = tmp_path / "snap.json"
    market_paths.write_private_atomic(path, b'{"a": 1}')
    assert path.read_bytes() == b'{"a": 1}'
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["snap.json"]


def test_read_private_file_refuses_symlinks_and_oversize(tmp_path):
    path = tmp_path / "snap.json"
    market_paths.write_private_atomic(path, b"x" * 10)
    data, key = market_paths.read_private_file(path, 100)
    assert data == b"x" * 10 and len(key) == 3
    assert market_paths.read_private_file(path, 5) is None
    link = tmp_path / "link.json"
    os.symlink(path, link)
    assert market_paths.read_private_file(link, 100) is None
    assert market_paths.read_private_file(tmp_path / "absent", 100) is None


def test_flag_set(tmp_path):
    flag = tmp_path / "x.disabled"
    assert not market_paths.flag_set(flag)
    flag.write_text("")
    assert market_paths.flag_set(flag)


def test_paths_under_keeps_everything_inside(tmp_path):
    paths = market_paths.Paths.under(tmp_path)
    for value in vars(paths).values():
        assert str(value).startswith(str(tmp_path))


# ── market_http ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url", [
    "http://api.nasdaq.com/x", "https://evil.example.com/", "https://user:pw" + "@" + "api.nasdaq.com/",
    "https://api.nasdaq.com:8443/", "https://trumpstruth.org/feed", "ftp://api.nasdaq.com/",
    # 2026-10-23：台灣的 ISP 依法院命令封鎖這三個主機，不在白名單裡（不繞過封鎖）
    "https://api.coinbase.com/v2/prices/BTC-USD/spot", "https://gamma-api.polymarket.com/events?slug=x",
    "https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=x",
])
def test_host_whitelist(url):
    assert market_http.host_of(url) is None
    with pytest.raises(market_http.FetchError) as err:
        market_http.fetch(url, max_bytes=10)
    assert err.value.kind == "host"


class _Ledger:
    def __init__(self):
        self.reserved, self.cooled = [], []

    def reserve(self, host):
        self.reserved.append(host)

    def cooldown(self, host, seconds):
        self.cooled.append((host, seconds))


def test_blocked_hosts_are_off_the_whitelist_and_cost_no_request():
    """被封鎖的主機不能留在白名單裡等著在 TLS 失敗（kind=network）：請求前就以 kind=host 拒絕，帳本也不預扣。"""
    blocked = ("api.coinbase.com", "gamma-api.polymarket.com", "api.elections.kalshi.com")
    assert not set(blocked) & market_http.ALLOWED_HOSTS
    ledger = _Ledger()
    for host in blocked:
        with pytest.raises(market_http.FetchError) as err:
            market_http.fetch(f"https://{host}/", max_bytes=10, ledger=ledger)
        assert err.value.kind == "host"
    assert ledger.reserved == []


def _fake_fetch(monkeypatch, status, ctype="application/json", body=b"{}"):
    import safe_fetch

    calls = []

    def fake(url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=status, headers={"Content-Type": ctype}, content=body)

    monkeypatch.setattr(safe_fetch, "fetch", fake)
    return calls


def test_ok_response_passes_safe_fetch_options(monkeypatch):
    calls = _fake_fetch(monkeypatch, 200, body=b'{"a": 1}')
    ledger = _Ledger()
    assert market_http.fetch_json("https://api.nasdaq.com/api/x", max_bytes=1000, ledger=ledger) == {"a": 1}
    assert ledger.reserved == ["api.nasdaq.com"]
    kwargs = calls[0]
    assert kwargs["allow_redirects"] is False and kwargs["accept_types"] is None
    assert kwargs["max_bytes"] == 1000 and kwargs["deadline"] == 15.0


@pytest.mark.parametrize("status, host, kind, cooled", [
    (429, "api.nasdaq.com", "rate_limited", True),
    (403, "www.twse.com.tw", "blocked", True),
    (503, "news.google.com", "rate_limited", True),
    (503, "api.nasdaq.com", "status", False),
    (301, "www.ssga.com", "moved", False),
    (500, "api.nasdaq.com", "status", False),
])
def test_status_handling_and_cooldown(monkeypatch, status, host, kind, cooled):
    _fake_fetch(monkeypatch, status, ctype="text/html")
    ledger = _Ledger()
    with pytest.raises(market_http.FetchError) as err:
        market_http.fetch(f"https://{host}/x", max_bytes=100, ledger=ledger)
    assert err.value.kind == kind
    assert bool(ledger.cooled) is cooled


def test_wrong_content_type_and_bad_json(monkeypatch):
    _fake_fetch(monkeypatch, 200, ctype="text/html")
    with pytest.raises(market_http.FetchError) as err:
        market_http.fetch("https://api.nasdaq.com/x", max_bytes=100)
    assert err.value.kind == "content_type"
    _fake_fetch(monkeypatch, 200, body=b"not json")
    with pytest.raises(market_http.FetchError) as err:
        market_http.fetch_json("https://api.nasdaq.com/x", max_bytes=100)
    assert err.value.kind == "not_json"


def test_safe_fetch_refusals_map_to_kinds(monkeypatch):
    import safe_fetch

    for exc, kind in ((safe_fetch.TooLarge("x"), "too_big"), (safe_fetch.FetchTimeout("x"), "timeout"),
                      (safe_fetch.BlockedURL("x"), "blocked_url"), (OSError("x"), "network")):
        def boom(url, _exc=exc, **kwargs):
            raise _exc
        monkeypatch.setattr(safe_fetch, "fetch", boom)
        with pytest.raises(market_http.FetchError) as err:
            market_http.fetch("https://api.nasdaq.com/x", max_bytes=100)
        assert err.value.kind == kind


def test_real_network_is_blocked_in_tests():
    """conftest 讓 safe_fetch 在測試裡連不出去；market_http 建在它上面，所以也連不出去。"""
    with pytest.raises(market_http.FetchError):
        market_http.fetch("https://api.nasdaq.com/api/market-info", max_bytes=1000)


# ── /static/img 路由 ─────────────────────────────────────────────────────────
@pytest.fixture
def genimg(tmp_path, monkeypatch):
    import main

    directory = tmp_path / "genimg"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    monkeypatch.setattr(main, "_GENIMG_DIR", str(directory))
    return main, directory


def _client(main):
    from fastapi.testclient import TestClient

    return TestClient(main.app)


def test_serves_a_private_png(genimg):
    main, directory = genimg
    name = "a" * 32 + ".png"
    (directory / name).write_bytes(b"\x89PNG" + b"x" * 1000)
    response = _client(main).get(f"/static/img/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-length"] == "1004"
    assert response.content == b"\x89PNG" + b"x" * 1000


@pytest.mark.parametrize("name, code", [("A" * 32 + ".png", 400), ("a" * 31 + ".png", 400),
                                        ("b" * 32 + ".png", 404)])
def test_bad_or_missing_names(genimg, name, code):
    main, _directory = genimg
    assert _client(main).get(f"/static/img/{name}").status_code == code


def test_trailing_newline_is_not_accepted():
    import main

    assert not main._GENIMG_FILENAME_RE.fullmatch("a" * 32 + ".png\n")


def test_symlinked_file_and_loose_directory_are_refused(genimg):
    main, directory = genimg
    name = "c" * 32 + ".png"
    os.symlink("/etc/hosts", directory / name)
    assert _client(main).get(f"/static/img/{name}").status_code == 404
    real = "d" * 32 + ".png"
    (directory / real).write_bytes(b"\x89PNG")
    os.chmod(directory, 0o755)
    assert _client(main).get(f"/static/img/{real}").status_code == 404


def test_file_is_read_whole_and_the_fd_closed(genimg):
    """2026-10-18：用同一個 fd 讀完再回（不串流）；回應建好時 fd 已經關了，之後刪檔或斷線都不會漏 fd。"""
    main, directory = genimg
    name = "e" * 32 + ".png"
    (directory / name).write_bytes(b"\x89PNG" + b"y" * 70000)
    before = len(os.listdir("/dev/fd"))
    response = main.serve_genimg(name)
    assert len(os.listdir("/dev/fd")) == before
    os.unlink(directory / name)
    assert response.body == b"\x89PNG" + b"y" * 70000
    big = "f" * 32 + ".png"
    with open(directory / big, "wb") as handle:
        handle.truncate(main._GENIMG_MAX_BYTES + 1)
    assert _client(main).get(f"/static/img/{big}").status_code == 404
