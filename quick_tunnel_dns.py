"""VPN-safe Cloudflare Quick Tunnel bootstrap.

Nord Threat Protection can return NXDOMAIN through the macOS host resolver even
while Cloudflare's authenticated DNS-over-HTTPS endpoint is reachable. This
module bypasses only the ingress-critical Cloudflare A/SRV bootstrap names and
keeps transient tunnel credentials in a short-lived loopback process. The stock
binary may still attempt its optional feature-selector TXT lookup through the
host resolver; that lookup is non-gating and is not represented as
VPN-independent here.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Iterable
from urllib.parse import urlencode
from urllib.parse import urlparse
import uuid


QUICK_TUNNEL_API_HOST = "api.trycloudflare.com"
EDGE_SRV_HOST = "_v2-origintunneld._tcp.argotunnel.com"
DOH_HOST = "cloudflare-dns.com"
DOH_RESOLVER_IPS = ("1.1.1.1", "1.0.0.1")
DOH_TOTAL_TIMEOUT_SEC = 5.0
MAX_DOH_BODY = 64 * 1024
MAX_QUICK_API_BODY = 64 * 1024
PROXY_DEFAULT_TTL_SEC = 45.0
DNS_ALIVE = "alive"
DNS_DEAD = "dead"
DNS_UNKNOWN = "unknown"

_QTYPE_CODES = {"A": 1, "SRV": 33}
_TRYCLOUDFLARE_HOST_RE = re.compile(r"^[a-z0-9-]+\.trycloudflare\.com$")
_EDGE_TARGET_RE = re.compile(r"^region([12])\.v2\.argotunnel\.com$")
_ACCOUNT_TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_HTTP_STATUS_MARKER = b"\n__LINEBOT_HTTP_STATUS__:"


class BootstrapError(RuntimeError):
    """Sanitized bootstrap failure. Never include upstream response bodies."""


class _DohNxDomain(BootstrapError):
    pass


@dataclass(frozen=True)
class DohObservation:
    state: str
    answers: tuple[str, ...]


@dataclass(frozen=True)
class QuickTunnelBootstrap:
    api_ips: tuple[str, ...]
    edges: tuple[str, ...]


def _allowed_doh_name(name: str) -> bool:
    normalized = name.rstrip(".").lower()
    return bool(
        normalized in {QUICK_TUNNEL_API_HOST, EDGE_SRV_HOST}
        or _EDGE_TARGET_RE.fullmatch(normalized)
        or (
            _TRYCLOUDFLARE_HOST_RE.fullmatch(normalized)
            and normalized != QUICK_TUNNEL_API_HOST
        )
    )


def _public_ipv4(value: str) -> str:
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError as exc:
        raise BootstrapError("doh_answer_invalid") from exc
    if parsed.version != 4 or not parsed.is_global:
        raise BootstrapError("doh_answer_invalid")
    return str(parsed)


def _parse_doh_json(payload: bytes, *, name: str, qtype: str) -> list[str]:
    if len(payload) > MAX_DOH_BODY:
        raise BootstrapError("doh_response_oversize")
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("doh_response_malformed") from exc
    if not isinstance(data, dict):
        raise BootstrapError("doh_response_malformed")
    expected_name = name.rstrip(".").lower()
    expected_type = _QTYPE_CODES[qtype]
    questions = data.get("Question")
    if not isinstance(questions, list) or len(questions) != 1:
        raise BootstrapError("doh_question_mismatch")
    question = questions[0]
    if not isinstance(question, dict):
        raise BootstrapError("doh_question_mismatch")
    if str(question.get("name", "")).rstrip(".").lower() != expected_name:
        raise BootstrapError("doh_question_mismatch")
    if question.get("type") != expected_type:
        raise BootstrapError("doh_question_mismatch")
    status = data.get("Status")
    if status == 3:
        raise _DohNxDomain("doh_nxdomain")
    if status != 0:
        raise BootstrapError("doh_status_unavailable")
    parsed: list[str] = []
    answers = data.get("Answer", [])
    if not isinstance(answers, list):
        raise BootstrapError("doh_response_malformed")
    for answer in answers:
        if not isinstance(answer, dict) or answer.get("type") != expected_type:
            continue
        owner = str(answer.get("name", "")).rstrip(".").lower()
        if owner != expected_name:
            continue
        raw = str(answer.get("data", "")).strip()
        if not raw:
            continue
        parsed.append(_public_ipv4(raw) if qtype == "A" else raw)
    if not parsed:
        raise BootstrapError("doh_nodata")
    return list(dict.fromkeys(parsed))


def _doh_query_once(
    name: str,
    qtype: str,
    resolver_ip: str,
    *,
    timeout: float = DOH_TOTAL_TIMEOUT_SEC,
    runner: Callable = subprocess.run,
) -> list[str]:
    normalized = name.rstrip(".").lower()
    if not _allowed_doh_name(normalized) or qtype not in _QTYPE_CODES:
        raise BootstrapError("doh_query_not_allowed")
    resolver = str(ipaddress.ip_address(resolver_ip))
    if resolver not in DOH_RESOLVER_IPS:
        raise BootstrapError("doh_resolver_not_allowed")
    bounded = max(1.0, min(float(timeout), DOH_TOTAL_TIMEOUT_SEC))
    query = urlencode({"name": normalized, "type": qtype})
    command = [
        "/usr/bin/curl",
        "--disable",
        "--noproxy", "*",
        "--proto", "=https",
        "--silent", "--show-error",
        "--header", "accept: application/dns-json",
        "--resolve", f"{DOH_HOST}:443:{resolver}",
        "--connect-timeout", f"{min(2.0, bounded):g}",
        "--max-time", f"{bounded:g}",
        "--max-filesize", str(MAX_DOH_BODY),
        f"https://{DOH_HOST}/dns-query?{query}",
    ]
    try:
        completed = runner(
            command, capture_output=True, timeout=bounded + 1, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError("doh_transport_unavailable") from exc
    if completed.returncode != 0:
        raise BootstrapError("doh_transport_unavailable")
    return _parse_doh_json(completed.stdout or b"", name=normalized, qtype=qtype)


def _observe_one(name: str, qtype: str, resolver_ip: str, timeout: float) -> DohObservation:
    try:
        answers = _doh_query_once(name, qtype, resolver_ip, timeout=timeout)
        return DohObservation("positive", tuple(answers))
    except _DohNxDomain:
        return DohObservation("nxdomain", ())
    except BootstrapError:
        return DohObservation("unknown", ())


def _doh_observations(
    name: str, qtype: str, *, timeout: float = DOH_TOTAL_TIMEOUT_SEC
) -> tuple[DohObservation, DohObservation]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_observe_one, name, qtype, resolver, timeout)
            for resolver in DOH_RESOLVER_IPS
        ]
        observations = tuple(future.result() for future in futures)
    return observations  # type: ignore[return-value]


def _doh_query(
    name: str, qtype: str, *, timeout: float = DOH_TOTAL_TIMEOUT_SEC
) -> list[str]:
    observations = _doh_observations(name, qtype, timeout=timeout)
    states = {observation.state for observation in observations}
    if "positive" in states and "nxdomain" in states:
        raise BootstrapError("doh_authoritative_conflict")
    answers: list[str] = []
    for observation in observations:
        if observation.state == "positive":
            answers.extend(observation.answers)
    if not answers:
        if states == {"nxdomain"}:
            raise BootstrapError("doh_nxdomain")
        raise BootstrapError("doh_bootstrap_unavailable")
    return list(dict.fromkeys(answers))


def _parse_srv_records(records: Iterable[str]) -> dict[str, list[str]]:
    regions: dict[str, list[str]] = {"1": [], "2": []}
    for raw in records:
        parts = raw.rstrip(".").split()
        if len(parts) != 4:
            raise BootstrapError("edge_srv_invalid")
        try:
            _priority, _weight, port = (int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError as exc:
            raise BootstrapError("edge_srv_invalid") from exc
        target = parts[3].rstrip(".").lower()
        match = _EDGE_TARGET_RE.fullmatch(target)
        if not match or port != 7844:
            raise BootstrapError("edge_srv_invalid")
        if target not in regions[match.group(1)]:
            regions[match.group(1)].append(target)
    if not regions["1"] or not regions["2"]:
        raise BootstrapError("edge_regions_incomplete")
    return regions


def build_bootstrap(*, timeout: float = DOH_TOTAL_TIMEOUT_SEC) -> QuickTunnelBootstrap:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        api_future = pool.submit(_doh_query, QUICK_TUNNEL_API_HOST, "A", timeout=timeout)
        srv_future = pool.submit(_doh_query, EDGE_SRV_HOST, "SRV", timeout=timeout)
        api_ips = api_future.result()
        regions = _parse_srv_records(srv_future.result())
    targets = regions["1"] + regions["2"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
        target_futures = {
            target: pool.submit(_doh_query, target, "A", timeout=timeout)
            for target in targets
        }
        resolved = {target: future.result() for target, future in target_futures.items()}
    region_ips: dict[str, list[str]] = {"1": [], "2": []}
    for region in ("1", "2"):
        for target in regions[region]:
            region_ips[region].extend(resolved[target])
        region_ips[region] = list(dict.fromkeys(region_ips[region]))[:4]
        if not region_ips[region]:
            raise BootstrapError("edge_region_unavailable")
    # cloudflared splits repeated --edge values by even/odd index. Emit only
    # complete region pairs so one exhausted region can never be backfilled by
    # an address from the other region.
    pair_count = min(len(region_ips["1"]), len(region_ips["2"]), 4)
    if pair_count < 2:
        raise BootstrapError("edge_region_insufficient")
    edges: list[str] = []
    for index in range(pair_count):
        edges.extend(
            (
                f"{region_ips['1'][index]}:7844",
                f"{region_ips['2'][index]}:7844",
            )
        )
    return QuickTunnelBootstrap(tuple(api_ips[:4]), tuple(edges))


def quick_tunnel_host_dns_state(
    host: str, *, timeout: float = DOH_TOTAL_TIMEOUT_SEC
) -> tuple[str, str]:
    normalized = host.rstrip(".").lower()
    if not _TRYCLOUDFLARE_HOST_RE.fullmatch(normalized) or normalized == QUICK_TUNNEL_API_HOST:
        return DNS_UNKNOWN, "invalid_quick_tunnel_host"
    observations = _doh_observations(normalized, "A", timeout=timeout)
    states = tuple(observation.state for observation in observations)
    if states == ("nxdomain", "nxdomain"):
        return DNS_DEAD, "authoritative_nxdomain"
    if "positive" in states and "nxdomain" not in states:
        return DNS_ALIVE, ""
    return DNS_UNKNOWN, "doh_liveness_unknown"


def quick_tunnel_public_health_check(
    url: str,
    *,
    timeout: float = DOH_TOTAL_TIMEOUT_SEC,
    runner: Callable = subprocess.run,
) -> tuple[bool, str]:
    """Probe a generated URL via DoH-derived literal IP with normal TLS checks."""
    try:
        parsed = urlparse(url.rstrip("/"))
    except ValueError:
        return False, "public_probe_invalid_url"
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.port
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not _TRYCLOUDFLARE_HOST_RE.fullmatch(host)
        or host == QUICK_TUNNEL_API_HOST
    ):
        return False, "public_probe_invalid_url"
    try:
        addresses = _doh_query(host, "A", timeout=timeout)
    except BootstrapError as exc:
        return False, f"public_probe_dns_failed: {exc}"
    bounded = max(1.0, min(float(timeout), DOH_TOTAL_TIMEOUT_SEC))
    for address in addresses[:4]:
        command = [
            "/usr/bin/curl",
            "--disable", "--noproxy", "*", "--proto", "=https",
            "--silent", "--show-error", "--output", "/dev/null",
            "--write-out", "%{http_code}",
            "--resolve", f"{host}:443:{_public_ipv4(address)}",
            "--connect-timeout", f"{min(2.0, bounded):g}",
            "--max-time", f"{bounded:g}",
            f"https://{host}/health",
        ]
        try:
            completed = runner(
                command, capture_output=True, timeout=bounded + 1, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode == 0 and (completed.stdout or b"").strip() == b"200":
            return True, ""
    return False, "public_probe_unreachable"


def quick_tunnel_api_dns_check(
    *, host: str = QUICK_TUNNEL_API_HOST, timeout: float = DOH_TOTAL_TIMEOUT_SEC
) -> tuple[bool, str]:
    """Read-only bootstrap preflight used by every restart owner."""
    if host != QUICK_TUNNEL_API_HOST:
        return False, "quick_tunnel_bootstrap_host_not_allowed"
    try:
        bootstrap = build_bootstrap(timeout=timeout)
    except BootstrapError as exc:
        return False, f"quick_tunnel_bootstrap_failed: {exc}"
    if len(bootstrap.edges) < 2:
        return False, "quick_tunnel_bootstrap_failed: insufficient_edges"
    if not _api_tls_ready(bootstrap.api_ips, timeout=timeout):
        return False, "quick_tunnel_bootstrap_failed: api_tls_unreachable"
    if not _edge_transport_ready(bootstrap.edges, timeout=min(2.0, timeout)):
        return False, "quick_tunnel_bootstrap_failed: edge_transport_unreachable"
    return True, ""


def _api_tls_ready(
    api_ips: tuple[str, ...],
    *,
    timeout: float,
    runner: Callable = subprocess.run,
) -> bool:
    bounded = max(1.0, min(float(timeout), DOH_TOTAL_TIMEOUT_SEC))
    for address in api_ips[:2]:
        command = [
            "/usr/bin/curl",
            "--disable", "--noproxy", "*", "--proto", "=https",
            "--silent", "--show-error", "--output", "/dev/null",
            "--request", "HEAD",
            "--resolve", f"{QUICK_TUNNEL_API_HOST}:443:{_public_ipv4(address)}",
            "--connect-timeout", f"{min(2.0, bounded):g}",
            "--max-time", f"{bounded:g}",
            f"https://{QUICK_TUNNEL_API_HOST}/tunnel",
        ]
        try:
            completed = runner(
                command, capture_output=True, timeout=bounded + 1, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode == 0:
            return True
    return False


def _edge_transport_ready(
    edges: tuple[str, ...],
    *,
    timeout: float,
    connector: Callable = socket.create_connection,
) -> bool:
    def region_ready(candidates: tuple[str, ...]) -> bool:
        for edge in candidates:
            address, raw_port = edge.rsplit(":", 1)
            try:
                connection = connector((address, int(raw_port)), timeout=timeout)
            except OSError:
                continue
            try:
                return True
            finally:
                connection.close()
        return False

    region_one = tuple(edges[0::2])
    region_two = tuple(edges[1::2])
    if not region_one or not region_two:
        return False
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        checks = (
            pool.submit(region_ready, region_one),
            pool.submit(region_ready, region_two),
        )
        return all(check.result() for check in checks)


def _canonicalize_quick_api_response(payload: bytes) -> bytes:
    if len(payload) > MAX_QUICK_API_BODY:
        raise BootstrapError("quick_api_response_oversize")
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("quick_api_response_malformed") from exc
    try:
        if not isinstance(data, dict) or data.get("success") is not True:
            raise ValueError
        result = data["result"]
        if not isinstance(result, dict):
            raise ValueError
        tunnel_id = str(uuid.UUID(str(result["id"])))
        hostname = str(result["hostname"]).rstrip(".").lower()
        if not _TRYCLOUDFLARE_HOST_RE.fullmatch(hostname) or hostname == QUICK_TUNNEL_API_HOST:
            raise ValueError
        account_tag = str(result["account_tag"])
        if not _ACCOUNT_TAG_RE.fullmatch(account_tag):
            raise ValueError
        encoded_secret = str(result["secret"])
        decoded_secret = base64.b64decode(encoded_secret, validate=True)
        if not 16 <= len(decoded_secret) <= 128:
            raise ValueError
        name = str(result.get("name", ""))
        if len(name) > 128:
            raise ValueError
    except (KeyError, TypeError, ValueError, base64.binascii.Error) as exc:
        raise BootstrapError("quick_api_schema_invalid") from exc
    canonical = {
        "success": True,
        "result": {
            "id": tunnel_id,
            "name": name,
            "hostname": hostname,
            "account_tag": account_tag,
            "secret": encoded_secret,
        },
        "errors": [],
    }
    return json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode()


def _request_quick_tunnel(
    api_ips: tuple[str, ...], *, runner: Callable = subprocess.run
) -> bytes:
    for api_ip in api_ips[:2]:
        literal = _public_ipv4(api_ip)
        command = [
            "/usr/bin/curl",
            "--disable", "--noproxy", "*", "--proto", "=https",
            "--silent", "--show-error", "--request", "POST",
            "--header", "Content-Type: application/json",
            "--header", "User-Agent: cloudflared-line-bot-bootstrap/1",
            "--resolve", f"{QUICK_TUNNEL_API_HOST}:443:{literal}",
            "--connect-timeout", "2", "--max-time", "6",
            "--max-filesize", str(MAX_QUICK_API_BODY),
            "--write-out", _HTTP_STATUS_MARKER.decode() + "%{http_code}",
            f"https://{QUICK_TUNNEL_API_HOST}/tunnel",
        ]
        try:
            completed = runner(command, capture_output=True, timeout=7, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        output = completed.stdout or b""
        if completed.returncode != 0 or _HTTP_STATUS_MARKER not in output:
            continue
        body, raw_status = output.rsplit(_HTTP_STATUS_MARKER, 1)
        try:
            status = int(raw_status.strip())
        except ValueError:
            continue
        if status != 200:
            continue
        try:
            return _canonicalize_quick_api_response(body)
        except BootstrapError:
            continue
    raise BootstrapError("quick_api_unavailable")


def _write_metadata(path: Path, *, quick_service_url: str, edges: tuple[str, ...]) -> None:
    if not re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}/[A-Za-z0-9_-]{20,}", quick_service_url):
        raise BootstrapError("proxy_metadata_invalid")
    if not edges or any(
        not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}:7844", edge) for edge in edges
    ):
        raise BootstrapError("proxy_metadata_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "VERSION=1\nQUICK_SERVICE_URL=" + quick_service_url + "\n"
    content += "".join(f"EDGE={edge}\n" for edge in edges)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _fixed_proxy_failure() -> bytes:
    return (
        b'{"success":false,"result":{"id":"","name":"","hostname":"",'
        b'"account_tag":"","secret":""},"errors":[{"code":503,'
        b'"message":"quick tunnel bootstrap unavailable"}]}'
    )


def _create_proxy_server(metadata_path: Path, *, ttl: float = PROXY_DEFAULT_TTL_SEC) -> HTTPServer:
    bootstrap = build_bootstrap()
    token = secrets.token_urlsafe(24)
    expected_path = f"/{token}/tunnel"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format, *_args):
            return

        def _send(self, status: int, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802
            self._send(405, b'{"error":"method not allowed"}')

        def do_HEAD(self):  # noqa: N802
            self._send(405, b"")

        def do_POST(self):  # noqa: N802
            if self.path != expected_path:
                self._send(404, b'{"error":"not found"}')
                return
            content_length = self.headers.get("Content-Length", "0")
            if self.headers.get("Transfer-Encoding") or content_length not in {"", "0"}:
                self._send(400, b'{"error":"empty body required"}')
                return
            if getattr(self.server, "consumed", False):
                self._send(409, b'{"error":"already consumed"}')
                return
            self.server.consumed = True
            try:
                payload = self.server.quick_tunnel_response_provider()
            except BootstrapError:
                self._send(502, _fixed_proxy_failure())
                return
            self._send(200, payload)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = min(1.0, max(0.1, ttl))
    server.consumed = False
    server.quick_tunnel_response_provider = lambda: _request_quick_tunnel(
        bootstrap.api_ips
    )
    host, port = server.server_address
    _write_metadata(
        metadata_path,
        quick_service_url=f"http://{host}:{port}/{token}",
        edges=bootstrap.edges,
    )
    return server


def _serve_proxy_until_consumed(server: HTTPServer, ttl: float) -> None:
    deadline = time.monotonic() + max(1.0, ttl)
    while time.monotonic() < deadline and not getattr(server, "consumed", False):
        server.handle_request()
    if not getattr(server, "consumed", False):
        raise BootstrapError("quick_proxy_expired")


def serve_proxy(metadata_path: Path, *, ttl: float = PROXY_DEFAULT_TTL_SEC) -> None:
    server = _create_proxy_server(metadata_path, ttl=ttl)
    try:
        _serve_proxy_until_consumed(server, ttl)
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=DOH_TOTAL_TIMEOUT_SEC)
    subparsers = parser.add_subparsers(dest="command")
    serve = subparsers.add_parser("serve-proxy")
    serve.add_argument("--metadata", type=Path, required=True)
    serve.add_argument("--ttl", type=float, default=PROXY_DEFAULT_TTL_SEC)
    lookup = subparsers.add_parser("lookup-host")
    lookup.add_argument("--host", required=True)
    args = parser.parse_args()
    try:
        if args.command == "serve-proxy":
            serve_proxy(args.metadata, ttl=args.ttl)
            return 0
        if args.command == "lookup-host":
            state, reason = quick_tunnel_host_dns_state(args.host, timeout=args.timeout)
            print(state if not reason else f"{state}: {reason}")
            return {DNS_ALIVE: 0, DNS_DEAD: 1}.get(state, 2)
        ok, reason = quick_tunnel_api_dns_check(timeout=args.timeout)
        if not ok:
            print(reason)
        return 0 if ok else 1
    except BootstrapError as exc:
        print(f"quick_tunnel_bootstrap_failed: {exc}")
        return 1
    except Exception:
        # Keep unexpected failures redacted as well: the Quick API response can
        # contain a transient tunnel secret and must never enter a traceback.
        print("quick_tunnel_bootstrap_failed: unexpected_internal_error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
