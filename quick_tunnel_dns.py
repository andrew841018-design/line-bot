"""Bounded quick-tunnel bootstrap lookup via the host system resolver.

`dig` talks DNS directly and can disagree with macOS scoped/VPN resolution.
cloudflared's Go resolver follows the host resolver path, so all recovery
owners use this helper before asking launchd to create a quick tunnel.
"""
from __future__ import annotations

import argparse
import ipaddress
import signal
import socket


QUICK_TUNNEL_API_HOST = "api.trycloudflare.com"


class _ResolverTimeout(TimeoutError):
    pass


def _raise_timeout(_signum, _frame):
    raise _ResolverTimeout("system resolver timed out")


def quick_tunnel_api_dns_check(
    *, host: str = QUICK_TUNNEL_API_HOST, timeout: float = 5.0
) -> tuple[bool, str]:
    """Return whether the system resolver provides at least one A/AAAA address."""
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    try:
        signal.signal(signal.SIGALRM, _raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, max(0.1, float(timeout)))
        answers = socket.getaddrinfo(
            host,
            443,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        addresses = set()
        for answer in answers:
            sockaddr = answer[4]
            if not sockaddr:
                continue
            try:
                addresses.add(str(ipaddress.ip_address(sockaddr[0])))
            except (ValueError, TypeError):
                continue
        if addresses:
            return True, ""
        return False, (
            f"dns_resolution_failed({host}): system resolver returned no IP address; "
            "VPN/private DNS may be blocking it"
        )
    except _ResolverTimeout:
        return False, f"dns_check_failed({host}): system resolver timeout after {timeout:g}s"
    except (OSError, socket.gaierror) as exc:
        return False, (
            f"dns_resolution_failed({host}): system resolver error: {str(exc)[:120]}; "
            "VPN/private DNS may be blocking it"
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    ok, reason = quick_tunnel_api_dns_check(timeout=args.timeout)
    if not ok:
        print(reason)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
