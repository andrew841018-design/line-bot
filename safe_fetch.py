"""Read a link someone pasted into the chat without reaching private networks.

2026-09-27: the bot prefetches links family members share.  A link must not
make it talk to this Mac, the home router or anything else that is not on the
public internet, and a hostile server must not be able to stall a reply or
fill memory.  So every fetch here

- accepts only http(s) URLs without credentials, backslashes or control
  characters, whose host Python and urllib3 read the same way;
- resolves the host first and refuses any non-public address, and checks
  every address again right before connecting (a DNS answer can change);
- follows redirects one hop at a time, checking each target the same way;
- stops reading at a byte limit and gives up at a wall-clock deadline.

main.py reaches the network only through `http.get` / `http.head`.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import sys
import threading
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from requests.cookies import RequestsCookieJar, extract_cookies_to_jar
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import (
    ConnectTimeoutError,
    LocationParseError,
    NameResolutionError,
    NewConnectionError,
)
from urllib3.util import parse_url
from urllib3.util.connection import allowed_gai_family
from urllib3.util.timeout import _DEFAULT_TIMEOUT

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_DEADLINE = 10.0
MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_FORBIDDEN_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")
_FORBIDDEN_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization", "host"})


class FetchRefused(ValueError):
    """The fetch was refused or cut short by policy.

    Not an OSError on purpose: raised inside urllib3's connect, an OSError
    would be retried or wrapped; a ValueError propagates as-is.
    """


class BlockedURL(FetchRefused):
    pass


class TooLarge(FetchRefused):
    pass


class Unsupported(FetchRefused):
    pass


class FetchTimeout(FetchRefused):
    pass


# ── address policy ───────────────────────────────────────────────────────────

def _is_public_ip_impl(value: str) -> bool:
    """A global unicast address, including any IPv4 address embedded in IPv6."""
    try:
        ip = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    candidates = [ip]
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            candidates.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            candidates.append(ip.sixtofour)
        if ip.teredo is not None:
            candidates.extend(ip.teredo)
        if ip in _NAT64:  # Python counts 64:ff9b::7f00:1 as global
            candidates.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return all(c.is_global and not c.is_multicast for c in candidates)


def _resolve_impl(host: str, port: int) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


# Looked up at call time, so the test suite can refuse every lookup and
# connection (conftest) and a transport test can re-open one local server.
is_public_ip = _is_public_ip_impl
_resolve = _resolve_impl
_connect_getaddrinfo = socket.getaddrinfo  # the lookup right before connecting


def public_host(url: str) -> str:
    """The host a URL points at, or BlockedURL — syntax only, no DNS.

    Also used before handing a link to yt-dlp, which has no connect guard.
    """
    if not isinstance(url, str) or _FORBIDDEN_URL_CHARS.search(url):
        raise BlockedURL("url characters")
    try:
        parts = urlsplit(url)
    except ValueError as exc:  # 「https://[abc/」
        raise BlockedURL("url") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise BlockedURL("scheme")
    if "@" in parts.netloc:
        raise BlockedURL("credentials")
    try:
        host = parts.hostname or ""
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError as exc:
        raise BlockedURL("port") from exc
    if not host:
        raise BlockedURL("host")
    try:
        ascii_host = host if host.isascii() else host.encode("idna").decode("ascii")
        other = (parse_url(url).host or "").strip("[]").lower()
    except (UnicodeError, LocationParseError, ValueError) as exc:
        raise BlockedURL("host") from exc
    # 「http://127.0.0.1\@tiktok.com/」: Python reads tiktok.com, urllib3 connects
    # to 127.0.0.1.  Backslashes are refused above; any other disagreement too.
    if other != ascii_host.lower():
        raise BlockedURL("ambiguous host")
    return ascii_host.lower()


def check_url(url: str) -> None:
    """public_host() plus: every address the host resolves to is public."""
    host = public_host(url)
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    try:
        addresses = _resolve(host, port)
    except OSError as exc:
        raise BlockedURL("unresolvable") from exc
    if not addresses or not all(is_public_ip(a) for a in addresses):
        raise BlockedURL("non-public address")


# ── connection guard ─────────────────────────────────────────────────────────

_local = threading.local()


class _FetchContext:
    """Sockets one fetch opened, so a timed-out fetch can be cut off."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._dups: list[socket.socket] = []
        self.connections = 0

    def register(self, sock: socket.socket) -> None:
        # TLS detaches the original socket object; a dup still reaches the
        # same connection, and shutting it down wakes a stalled read.
        dup = sock.dup()
        with self._lock:
            self._dups.append(dup)
            self.connections += 1
        if self.cancelled.is_set():
            self._cut(dup)
            raise FetchTimeout("cancelled")

    @staticmethod
    def _cut(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def cancel(self) -> None:
        self.cancelled.set()
        with self._lock:
            dups, self._dups = self._dups, []
        for dup in dups:
            self._cut(dup)

    def release(self) -> None:
        with self._lock:
            dups, self._dups = self._dups, []
        for dup in dups:
            dup.close()


def _before_connect() -> None:
    ctx = getattr(_local, "ctx", None)
    if ctx is not None and ctx.cancelled.is_set():
        raise FetchTimeout("cancelled")


def _guarded_create_connection(address, timeout, source_address, socket_options) -> socket.socket:
    """urllib3's create_connection, refusing non-public addresses before connecting.

    The cancel flag is checked again after this lookup (it may have stalled),
    and each socket is registered before its blocking connect.
    """
    host, port = address
    if host.startswith("["):
        host = host.strip("[]")
    try:
        host.encode("idna")
    except UnicodeError:
        raise LocationParseError(f"'{host}', label empty or too long") from None
    _before_connect()
    err: BaseException | None = None
    for af, socktype, proto, _canon, sa in _connect_getaddrinfo(host, port, allowed_gai_family(), socket.SOCK_STREAM):
        _before_connect()
        if not is_public_ip(sa[0]):
            err = BlockedURL("non-public address")
            continue
        sock = socket.socket(af, socktype, proto)
        try:
            ctx = getattr(_local, "ctx", None)
            if ctx is not None:
                ctx.register(sock)
            for option in socket_options or ():
                sock.setsockopt(*option)
            if timeout is not _DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            _before_connect()
            peer = sock.getpeername()[0]
            if not is_public_ip(peer):
                raise BlockedURL("connected to a non-public address")
            return sock
        except OSError as exc:
            err = exc
            sock.close()
        except BaseException:
            sock.close()
            raise
    if err is not None:
        raise err
    raise OSError("getaddrinfo returns an empty list")


def _guarded_new_conn(conn) -> socket.socket:
    """urllib3 2.x HTTPConnection._new_conn with the guarded connect."""
    try:
        sock = _guarded_create_connection(
            (conn._dns_host, conn.port), conn.timeout,
            source_address=conn.source_address, socket_options=conn.socket_options,
        )
    except socket.gaierror as exc:
        raise NameResolutionError(conn.host, conn, exc) from exc
    except TimeoutError as exc:
        raise ConnectTimeoutError(
            conn, f"Connection to {conn.host} timed out. (connect timeout={conn.timeout})"
        ) from exc
    except OSError as exc:
        raise NewConnectionError(conn, f"Failed to establish a new connection: {exc}") from exc
    sys.audit("http.client.connect", conn, conn.host, conn.port)
    return sock


class _GuardedHTTPConnection(HTTPConnection):
    def _new_conn(self):  # urllib3 hook
        return _guarded_new_conn(self)


class _GuardedHTTPSConnection(HTTPSConnection):
    def _new_conn(self):
        return _guarded_new_conn(self)


class _GuardedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _GuardedHTTPConnection


class _GuardedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


class _GuardedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": _GuardedHTTPPool,
            "https": _GuardedHTTPSPool,
        }


# ── fetching ─────────────────────────────────────────────────────────────────

def _with_params(url: str, params) -> str:
    if not params:
        return url
    try:
        return requests.Request("GET", url, params=params).prepare().url or url
    except requests.exceptions.RequestException as exc:
        raise BlockedURL("url") from exc


def _content_type(resp: requests.Response) -> str:
    return (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()


def _read_body(resp: requests.Response, max_bytes: int, truncate: bool) -> None:
    declared = (resp.headers.get("Content-Length") or "").strip()
    if declared.isdigit() and int(declared) > max_bytes and not truncate:
        raise TooLarge("declared length")
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(16384):  # decoded bytes: gzip bombs count too
        total += len(chunk)
        if total > max_bytes:
            if not truncate:
                raise TooLarge("body")
            chunks.append(chunk[: len(chunk) - (total - max_bytes)])
            break
        chunks.append(chunk)
    resp._content = b"".join(chunks)
    resp._content_consumed = True


def _fetch_hops(url, *, method, headers, timeout, allow_redirects, read_body,
                max_bytes, truncate, accept_types, max_redirects) -> requests.Response:
    current = url
    jar = RequestsCookieJar()  # this fetch's redirect hops only; never kept
    with requests.Session() as session:
        session.trust_env = False  # no environment proxies: check the real peer
        adapter = _GuardedAdapter(max_retries=0)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        for _hop in range(max_redirects + 1):
            check_url(current)
            prepared = session.prepare_request(requests.Request(method, current, headers=headers, cookies=jar))
            # adapter.send, not session.send: the session reads a redirect's
            # whole body even with allow_redirects=False.
            resp = adapter.send(prepared, stream=True, timeout=timeout, verify=True, proxies={})
            try:
                extract_cookies_to_jar(jar, prepared, resp.raw)  # adapter.send keeps none itself
                location = resp.headers.get("Location")
                if allow_redirects and resp.status_code in _REDIRECT_STATUSES and location:
                    if _FORBIDDEN_URL_CHARS.search(location):
                        raise BlockedURL("redirect location")
                    current = urljoin(current, location)
                    continue
                if method == "HEAD" or not read_body:
                    resp._content = b""
                    resp._content_consumed = True
                    return resp
                if accept_types and _content_type(resp) and _content_type(resp) not in accept_types:
                    raise Unsupported(_content_type(resp))
                _read_body(resp, max_bytes, truncate)
                return resp
            finally:
                resp.close()
        raise BlockedURL("too many redirects")


def fetch(
    url: str,
    *,
    method: str = "GET",
    params=None,
    headers=None,
    timeout=5,
    deadline: float = DEFAULT_DEADLINE,
    max_bytes: int = DEFAULT_MAX_BYTES,
    truncate: bool = False,
    accept_types=None,
    allow_redirects: bool = True,
    read_body: bool = True,
    max_redirects: int = MAX_REDIRECTS,
) -> requests.Response:
    """GET/HEAD a public URL within `deadline` seconds; the response is closed."""
    if any(str(name).lower() in _FORBIDDEN_HEADERS for name in (headers or {})):
        raise ValueError("a prefetch never sends credentials or its own Host header")
    target = _with_params(url, params)
    ctx = _FetchContext()
    box: dict = {}

    def work() -> None:
        _local.ctx = ctx
        try:
            box["response"] = _fetch_hops(
                target, method=method, headers=headers, timeout=timeout,
                allow_redirects=allow_redirects, read_body=read_body,
                max_bytes=max_bytes, truncate=truncate,
                accept_types=frozenset(accept_types or ()), max_redirects=max_redirects,
            )
        except BaseException as exc:  # noqa: BLE001 - handed to the caller
            box["error"] = exc
        finally:
            _local.ctx = None

    worker = threading.Thread(target=work, name="safe-fetch", daemon=True)
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        ctx.cancel()  # a stalled DNS lookup cannot be cut; it can no longer connect
        raise FetchTimeout(f"no answer within {deadline}s")
    ctx.release()
    if "error" in box:
        raise box["error"]
    if ctx.connections == 0:
        # The urllib3 hook did not run: never hand back unchecked content.
        raise BlockedURL("connection guard inactive")
    return box["response"]


class _Http:
    """What main.py calls `_requests`: get and head only, always through fetch().

    `stream=True` means "do not read the body" (Google Maps only looks at the
    redirect headers); the response is closed either way.
    """

    @staticmethod
    def get(url, params=None, headers=None, timeout=5, allow_redirects=True, stream=False, **limits):
        return fetch(url, params=params, headers=headers, timeout=timeout,
                     allow_redirects=allow_redirects, read_body=not stream, **limits)

    @staticmethod
    def head(url, headers=None, timeout=5, allow_redirects=False, **limits):
        return fetch(url, method="HEAD", headers=headers, timeout=timeout,
                     allow_redirects=allow_redirects, **limits)


http = _Http()
