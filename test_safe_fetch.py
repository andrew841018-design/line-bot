# Synthetic fixtures compose fake clinician names or URL userinfo explicitly; no real identities.
"""2026-09-27: links pasted into the chat are read without reaching private
networks, stalling a reply, or filling memory.  Only local servers; the
conftest blocks every real lookup and connection, and these tests re-open
exactly the local test server."""

from __future__ import annotations

import http.server
import socket
import socketserver
import threading
import time

import pytest
import requests

import safe_fetch as sf

PUBLIC_STAND_IN = "93.184.216.34"


class _Handler(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []
    redirect_bytes = 0  # body bytes a redirect response managed to send
    redirect_done = threading.Event()

    def log_message(self, *_a):
        pass

    def do_HEAD(self):
        self.do_GET()

    def _redirect(self, location, body_bytes=0, drip=False):
        self.send_response(302)
        if location:
            self.send_header("Location", location)
        if body_bytes:
            self.send_header("Content-Length", str(body_bytes))
        self.end_headers()
        try:
            if drip:  # one byte every 0.25 s: reading it would take minutes
                for _ in range(body_bytes):
                    time.sleep(0.25)
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    _Handler.redirect_bytes += 1
            else:
                for _ in range(body_bytes // 2**16):
                    self.wfile.write(b"x" * 2**16)
                    _Handler.redirect_bytes += 2**16
        except OSError:
            pass
        finally:
            if body_bytes:
                _Handler.redirect_done.set()

    def do_GET(self):
        _Handler.hits.append(self.path)
        routes = {
            "/rel": lambda: self._redirect("final"),
            "/abs-private": lambda: self._redirect("http://10.0.0.1/x"),
            "/to-file": lambda: self._redirect("file:///etc/passwd"),
            "/to-creds": lambda: self._redirect("http://user:pw@" "example.test/"),
            "/loop": lambda: self._redirect("/loop"),
            "/redirect-big-body": lambda: self._redirect("/final", body_bytes=50 * 2**20),
            "/redirect-slow-body": lambda: self._redirect("/final", body_bytes=1000, drip=True),
            "/to-ftp": lambda: self._redirect("ftp://example.test/x"),
            "/to-data": lambda: self._redirect("data:text/html,hi"),
            "/no-location": lambda: self._redirect(""),
        }
        if self.path in routes:
            routes[self.path]()
            return
        if self.path in ("/set-cookie", "/set-path-cookie", "/set-secure-cookie", "/set-foreign-cookie",
                         "/set-expired-cookie"):
            cookie = {
                "/set-cookie": "sid=abc; Path=/",
                "/set-expired-cookie": "sid=abc; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
                "/set-path-cookie": "sid=abc; Path=/elsewhere",
                "/set-secure-cookie": "sid=abc; Path=/; Secure",
                "/set-foreign-cookie": "sid=abc; Path=/; Domain=other.example",
            }[self.path]
            self.send_response(302)
            self.send_header("Set-Cookie", cookie)
            self.send_header("Location", "/need-cookie")
            self.end_headers()
            return
        if self.path == "/need-cookie":
            ok = "sid=abc" in (self.headers.get("Cookie") or "")
            self.send_response(200 if ok else 403)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"cookie ok" if ok else b"no cookie")
            return
        if self.path in ("/big", "/big-declared"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            if self.path == "/big-declared":
                self.send_header("Content-Length", "300000")
            self.end_headers()
            try:
                self.wfile.write(b"a" * 300_000)
            except OSError:
                pass
            return
        if self.path == "/pdf":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.end_headers()
            self.wfile.write(b"%PDF-1.4")
            return
        if self.path in ("/drip", "/slow-headers"):
            if self.path == "/slow-headers":
                self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", "100000")
                self.end_headers()
            try:
                for _ in range(40):
                    time.sleep(0.5)
                    self.wfile.write(b"X-A: b\r\n" if self.path == "/slow-headers" else b"a")
                    self.wfile.flush()
            except OSError:
                pass
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<p>合成內文</p>".encode())


@pytest.fixture(scope="module")
def server():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def tls_stall():
    """Accepts TCP and never answers the TLS hello."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    held = []
    stop = threading.Event()

    def accept():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                held.append(listener.accept()[0])
            except OSError:
                continue

    threading.Thread(target=accept, daemon=True).start()
    yield listener.getsockname()[1]
    stop.set()
    for conn in held:
        conn.close()
    listener.close()


@pytest.fixture
def loopback_is_public(monkeypatch):
    """The local server stands in for a public site; nothing else is allowed."""
    monkeypatch.setattr(sf, "is_public_ip", lambda ip: str(ip).split("%")[0] in ("127.0.0.1", PUBLIC_STAND_IN))
    monkeypatch.setattr(sf, "_resolve", lambda host, port: ["127.0.0.1"] if host == "127.0.0.1" else ["10.0.0.1"])


# ── address policy ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True), ("2001:4860:4860::8888", True), ("64:ff9b::808:808", True),
    ("127.0.0.1", False), ("10.0.0.1", False), ("192.168.1.1", False), ("169.254.169.254", False),
    ("100.64.0.1", False), ("0.0.0.0", False), ("::1", False), ("fe80::1", False),
    ("::ffff:127.0.0.1", False), ("64:ff9b::7f00:1", False), ("64:ff9b::a00:1", False),
    ("2002:7f00:1::1", False), ("2001:0:4136:e378:8000:63bf:80ff:fffe", False), ("224.0.0.1", False),
    ("not-an-ip", False),
])
def test_public_address_policy(ip, public):
    assert sf._is_public_ip_impl(ip) is public


@pytest.mark.parametrize("url", [
    "http://127.0.0.1\\@tiktok.com/a",           # Python and urllib3 disagree on the host
    "http://127.0.0.1:80\\@tiktok.com/",
    "http://user@" "tiktok.com/",
    "http://user:pw@" "tiktok.com/",
    "http://tiktok.com\r\nHost: x/",
    "http://tiktok.com/\x00",
    "file:///etc/passwd",
    "ftp://example.test/",
    "data:text/html,hi",
    "http:///nohost",
    "http://ｔｉｋｔｏｋ．ｃｏｍ/x",                  # never fetchable: urllib3 refuses it
    "http://example.test:99999/",
])
def test_unsafe_url_syntax_is_refused(url):
    with pytest.raises(sf.BlockedURL):
        sf.public_host(url)


@pytest.mark.parametrize("url,host", [
    ("https://www.tiktok.com/@u/video/1", "www.tiktok.com"),
    ("https://zh.wikipedia.org/wiki/臺灣", "zh.wikipedia.org"),
    ("HTTPS://Example.TEST/a?b=1", "example.test"),
    ("http://[2001:db8::1]:8080/", "2001:db8::1"),
])
def test_public_host_reads_the_same_host_as_urllib3(url, host):
    assert sf.public_host(url) == host


@pytest.mark.parametrize("literal", ["2130706433", "0x7f.1", "127.1"])
def test_odd_ip_literals_are_resolved_before_the_check(monkeypatch, literal):
    monkeypatch.setattr(sf, "is_public_ip", sf._is_public_ip_impl)
    monkeypatch.setattr(sf, "_resolve", sf._resolve_impl)  # a literal never leaves the machine
    with pytest.raises(sf.BlockedURL):
        sf.check_url(f"http://{literal}/")


# ── the connection itself ────────────────────────────────────────────────────

def test_private_targets_are_refused_before_connecting(server, monkeypatch):
    monkeypatch.setattr(sf, "is_public_ip", sf._is_public_ip_impl)
    monkeypatch.setattr(sf, "_resolve", sf._resolve_impl)
    before = len(_Handler.hits)
    for url in (server + "/", server.replace("127.0.0.1", "localhost") + "/"):
        with pytest.raises(sf.BlockedURL):
            sf.fetch(url)
    assert len(_Handler.hits) == before


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_connect_guard_catches_a_changed_dns_answer(monkeypatch, scheme):
    # The pre-check is told the host is public; the lookup at connect time
    # says 127.0.0.1.  Not even a TCP connection may be made — if the urllib3
    # hook ever stopped running, this listener would accept one.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(0.3)
    port = listener.getsockname()[1]
    monkeypatch.setattr(sf, "is_public_ip", sf._is_public_ip_impl)
    monkeypatch.setattr(sf, "_resolve", lambda host, port_: [PUBLIC_STAND_IN])
    try:
        with pytest.raises(sf.BlockedURL):
            sf.fetch(f"{scheme}://localhost:{port}/")
        with pytest.raises(socket.timeout):
            listener.accept()[0].close()
    finally:
        listener.close()


def _workers_finished(timeout=3.0):
    """Join every safe-fetch worker; True once none is left running."""
    end = time.monotonic() + timeout
    for worker in [t for t in threading.enumerate() if t.name == "safe-fetch"]:
        worker.join(max(0.0, end - time.monotonic()))
    return not [t for t in threading.enumerate() if t.name == "safe-fetch" and t.is_alive()]


def test_public_page_is_read(server, loopback_is_public):
    resp = sf.fetch(server + "/", accept_types={"text/html"})
    assert resp.status_code == 200 and "合成內文" in resp.text


def test_relative_redirect_is_followed(server, loopback_is_public):
    assert sf.fetch(server + "/rel").url.endswith("/final")


@pytest.mark.parametrize("path", ["/abs-private", "/to-file", "/to-ftp", "/to-data", "/to-creds", "/loop"])
def test_bad_redirects_are_refused(server, loopback_is_public, path):
    with pytest.raises(sf.BlockedURL):
        sf.fetch(server + path)


def test_redirect_body_is_never_read(server, loopback_is_public):
    _Handler.redirect_bytes = 0
    _Handler.redirect_done.clear()
    assert sf.fetch(server + "/redirect-big-body").status_code == 200
    assert _Handler.redirect_done.wait(10)
    # Only what fits in the socket buffers was written; reading the body
    # would have pulled all 50 MiB through.
    assert _Handler.redirect_bytes < 16 * 2**20


def test_slow_redirect_body_is_never_waited_for(server, loopback_is_public):
    start = time.monotonic()
    assert sf.fetch(server + "/redirect-slow-body", deadline=5).status_code == 200
    assert time.monotonic() - start < 2


def test_redirect_without_location_is_returned_as_is(server, loopback_is_public):
    resp = sf.fetch(server + "/no-location")
    assert resp.status_code == 302 and resp.content == b""


def test_declared_length_over_the_limit(server, loopback_is_public):
    with pytest.raises(sf.TooLarge, match="declared"):
        sf.fetch(server + "/big-declared", max_bytes=100_000)
    assert len(sf.fetch(server + "/big-declared", max_bytes=100_000, truncate=True).content) == 100_000


def test_size_limit(server, loopback_is_public):
    with pytest.raises(sf.TooLarge):
        sf.fetch(server + "/big", max_bytes=100_000)
    assert len(sf.fetch(server + "/big", max_bytes=100_000, truncate=True).content) == 100_000


def test_non_text_is_not_downloaded(server, loopback_is_public):
    with pytest.raises(sf.Unsupported):
        sf.fetch(server + "/pdf", accept_types={"text/html"})


@pytest.mark.parametrize("path", ["/drip", "/slow-headers"])
def test_trickling_server_hits_the_deadline(server, loopback_is_public, path):
    start = time.monotonic()
    with pytest.raises(sf.FetchTimeout):
        sf.fetch(server + path, deadline=1.5)
    assert time.monotonic() - start < 2.5
    assert _workers_finished()


def test_stalled_tls_handshake_hits_the_deadline(tls_stall, loopback_is_public):
    start = time.monotonic()
    with pytest.raises(sf.FetchTimeout):
        sf.fetch(f"https://127.0.0.1:{tls_stall}/", deadline=1.5)
    assert time.monotonic() - start < 2.5
    assert _workers_finished()


def test_stalled_dns_returns_at_the_deadline_and_never_connects(server, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(sf, "is_public_ip", lambda ip: True)

    def slow_resolve(host, port):
        release.wait(5)
        return ["127.0.0.1"]

    monkeypatch.setattr(sf, "_resolve", slow_resolve)
    before = len(_Handler.hits)
    start = time.monotonic()
    with pytest.raises(sf.FetchTimeout):
        sf.fetch(server + "/", deadline=1.0)
    assert time.monotonic() - start < 1.5
    release.set()
    assert _workers_finished()  # the late worker ran to its end...
    assert len(_Handler.hits) == before  # ...and saw the cancel flag


def test_credentials_are_never_sent(server, loopback_is_public):
    for header in ("Authorization", "Cookie", "proxy-authorization"):
        with pytest.raises(ValueError):
            sf.fetch(server + "/", headers={header: "x"})


def test_facade_matches_what_main_uses(server, loopback_is_public):
    head = sf.http.head(server + "/rel")
    assert head.status_code == 302 and head.headers["Location"] == "final"
    first_hop = sf.http.get(server + "/rel", allow_redirects=False, stream=True)
    assert first_hop.status_code == 302 and first_hop.content == b""
    page = sf.http.get(server + "/", params={"q": "合成"}, headers={"User-Agent": "t"})
    assert "q=%E5%90%88%E6%88%90" in page.url


def test_global_requests_is_untouched():
    assert requests.get is not sf.http.get
    assert requests.adapters.HTTPAdapter.init_poolmanager is not sf._GuardedAdapter.init_poolmanager


# ── debate amendments ────────────────────────────────────────────────────────

def test_cookie_set_on_a_redirect_is_sent_to_the_next_hop(server, loopback_is_public):
    assert sf.fetch(server + "/set-cookie").status_code == 200


@pytest.mark.parametrize("path", ["/set-path-cookie", "/set-secure-cookie", "/set-foreign-cookie", "/set-expired-cookie"])
def test_cookie_rules_are_kept(server, loopback_is_public, path):
    assert sf.fetch(server + path).status_code == 403


def test_cookies_never_outlive_one_fetch(server, loopback_is_public):
    sf.fetch(server + "/set-cookie")
    assert sf.fetch(server + "/need-cookie").status_code == 403


def test_host_header_is_refused(server, loopback_is_public):
    with pytest.raises(ValueError):
        sf.fetch(server + "/", headers={"Host": "evil.example"})


def test_stalled_connect_lookup_never_connects_after_cancel(monkeypatch):
    """The lookup inside the connect (after the pre-check) stalls past the deadline."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    accepted = []
    stop = threading.Event()

    def accept():
        while not stop.is_set():
            try:
                accepted.append(listener.accept()[0])
            except OSError:
                continue

    threading.Thread(target=accept, daemon=True).start()
    release = threading.Event()
    monkeypatch.setattr(sf, "is_public_ip", lambda ip: True)
    monkeypatch.setattr(sf, "_resolve", lambda host, port: [PUBLIC_STAND_IN])

    def stalled_lookup(host, port_, *args):
        release.wait(5)
        return socket.getaddrinfo("127.0.0.1", port_, *args)

    monkeypatch.setattr(sf, "_connect_getaddrinfo", stalled_lookup)
    try:
        start = time.monotonic()
        with pytest.raises(sf.FetchTimeout):
            sf.fetch(f"http://stalled.example:{port}/", deadline=1.0)
        assert time.monotonic() - start < 1.5
        release.set()
        assert _workers_finished()
        time.sleep(0.3)  # let the accept loop observe any late connection
        assert accepted == []  # the late worker saw the cancel flag
    finally:
        stop.set()
        release.set()
        for conn in accepted:
            conn.close()
        listener.close()


def test_fails_closed_when_the_connection_hook_does_not_run(server, loopback_is_public, monkeypatch):
    import urllib3.connection

    monkeypatch.setattr(sf._GuardedHTTPConnection, "_new_conn", urllib3.connection.HTTPConnection._new_conn)
    with pytest.raises(sf.BlockedURL, match="guard inactive"):
        sf.fetch(server + "/")
