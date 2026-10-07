import base64
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import quick_tunnel_dns as qtd


def _completed(payload: dict, *, rc: int = 0, stderr: bytes = b""):
    return subprocess.CompletedProcess(
        args=["curl"],
        returncode=rc,
        stdout=json.dumps(payload).encode(),
        stderr=stderr,
    )


def _doh_payload(name: str, qtype: int, answers: list[tuple[int, str]], status: int = 0):
    return {
        "Status": status,
        "Question": [{"name": name, "type": qtype}],
        "Answer": [
            {"name": name, "type": answer_type, "TTL": 60, "data": data}
            for answer_type, data in answers
        ],
    }


def test_doh_query_uses_authenticated_literal_transport_without_system_dns():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return _completed(
            _doh_payload("api.trycloudflare.com", 1, [(1, "104.16.230.132")])
        )

    answers = qtd._doh_query_once(
        "api.trycloudflare.com", "A", "1.1.1.1", runner=runner
    )

    assert answers == ["104.16.230.132"]
    command, kwargs = calls[0]
    assert command[0] == "/usr/bin/curl"
    assert "--disable" in command
    assert command[command.index("--noproxy") + 1] == "*"
    assert command[command.index("--proto") + 1] == "=https"
    assert "cloudflare-dns.com:443:1.1.1.1" in command
    assert not {"-k", "--insecure", "-L", "--location"}.intersection(command)
    assert kwargs["timeout"] <= qtd.DOH_TOTAL_TIMEOUT_SEC + 2


@pytest.mark.parametrize(
    "payload",
    [
        {"Status": 0, "Question": [], "Answer": []},
        _doh_payload("evil.example", 1, [(1, "104.16.230.132")]),
        _doh_payload("api.trycloudflare.com", 28, [(28, "2606:4700::1")]),
        _doh_payload("api.trycloudflare.com", 1, [(1, "127.0.0.1")]),
    ],
)
def test_doh_query_rejects_malformed_or_unsafe_answers(payload):
    with pytest.raises(qtd.BootstrapError):
        qtd._doh_query_once(
            "api.trycloudflare.com",
            "A",
            "1.1.1.1",
            runner=lambda *_args, **_kwargs: _completed(payload),
        )


def test_edge_bootstrap_interleaves_two_regions_and_rejects_wrong_port():
    responses = {
        ("api.trycloudflare.com", "A"): ["104.16.230.132"],
        (qtd.EDGE_SRV_HOST, "SRV"): [
            "1 1 7844 region1.v2.argotunnel.com",
            "2 1 7844 region2.v2.argotunnel.com",
        ],
        ("region1.v2.argotunnel.com", "A"): ["198.41.200.13", "198.41.200.23"],
        ("region2.v2.argotunnel.com", "A"): ["198.41.192.7", "198.41.192.17"],
    }

    with patch.object(qtd, "_doh_query", side_effect=lambda name, qtype, **_: responses[(name, qtype)]):
        bootstrap = qtd.build_bootstrap()

    assert bootstrap.api_ips == ("104.16.230.132",)
    assert bootstrap.edges == (
        "198.41.200.13:7844",
        "198.41.192.7:7844",
        "198.41.200.23:7844",
        "198.41.192.17:7844",
    )

    responses[(qtd.EDGE_SRV_HOST, "SRV")] = [
        "1 1 443 region1.v2.argotunnel.com",
        "2 1 7844 region2.v2.argotunnel.com",
    ]
    with patch.object(qtd, "_doh_query", side_effect=lambda name, qtype, **_: responses[(name, qtype)]):
        with pytest.raises(qtd.BootstrapError, match="edge_srv_invalid"):
            qtd.build_bootstrap()


@pytest.mark.parametrize(
    ("first_state", "second_state", "expected"),
    [
        ("positive", "positive", qtd.DNS_ALIVE),
        ("positive", "unknown", qtd.DNS_ALIVE),
        ("unknown", "positive", qtd.DNS_ALIVE),
        ("positive", "nxdomain", qtd.DNS_UNKNOWN),
        ("nxdomain", "positive", qtd.DNS_UNKNOWN),
        ("unknown", "unknown", qtd.DNS_UNKNOWN),
        ("unknown", "nxdomain", qtd.DNS_UNKNOWN),
        ("nxdomain", "unknown", qtd.DNS_UNKNOWN),
        ("nxdomain", "nxdomain", qtd.DNS_DEAD),
    ],
)
def test_generated_host_state_requires_double_nxdomain_and_is_tri_state(
    first_state, second_state, expected
):
    host = "fresh-slug.trycloudflare.com"
    observation = lambda state: qtd.DohObservation(
        state, ("104.16.230.132",) if state == "positive" else ()
    )
    with patch.object(
        qtd,
        "_doh_observations",
        return_value=(observation(first_state), observation(second_state)),
    ):
        assert qtd.quick_tunnel_host_dns_state(host)[0] == expected


def test_nxdomain_must_echo_the_exact_authenticated_question():
    payload = _doh_payload("evil.example", 1, [], status=3)
    with pytest.raises(qtd.BootstrapError, match="doh_question_mismatch"):
        qtd._parse_doh_json(
            json.dumps(payload).encode(),
            name="fresh-slug.trycloudflare.com",
            qtype="A",
        )


def test_bootstrap_refuses_uneven_region_pools_that_break_even_odd_partition():
    responses = {
        (qtd.QUICK_TUNNEL_API_HOST, "A"): ["104.16.230.132"],
        (qtd.EDGE_SRV_HOST, "SRV"): [
            "1 1 7844 region1.v2.argotunnel.com",
            "1 1 7844 region2.v2.argotunnel.com",
        ],
        ("region1.v2.argotunnel.com", "A"): [
            "198.41.192.7",
            "198.41.192.17",
            "198.41.192.27",
            "198.41.192.37",
        ],
        ("region2.v2.argotunnel.com", "A"): ["198.41.200.13"],
    }
    with patch.object(
        qtd,
        "_doh_query",
        side_effect=lambda name, qtype, **_: responses[(name, qtype)],
    ):
        with pytest.raises(qtd.BootstrapError, match="edge_region_insufficient"):
            qtd.build_bootstrap()


def test_public_health_probe_uses_doh_literal_ip_and_normal_tls_hostname():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, b"200", b"")

    with patch.object(qtd, "_doh_query", return_value=["104.16.230.132"]):
        ok, reason = qtd.quick_tunnel_public_health_check(
            "https://fresh-slug.trycloudflare.com", runner=runner
        )

    assert ok is True
    assert reason == ""
    command = calls[0]
    assert "fresh-slug.trycloudflare.com:443:104.16.230.132" in command
    assert command[-1] == "https://fresh-slug.trycloudflare.com/health"
    assert not {"-k", "--insecure", "-L", "--location"}.intersection(command)


def test_preflight_requires_api_tls_and_both_edge_regions():
    bootstrap = qtd.QuickTunnelBootstrap(
        api_ips=("104.16.230.132",),
        edges=("198.41.200.13:7844", "198.41.192.7:7844"),
    )
    with patch.object(qtd, "build_bootstrap", return_value=bootstrap), patch.object(
        qtd, "_api_tls_ready", return_value=True
    ), patch.object(qtd, "_edge_transport_ready", return_value=True):
        assert qtd.quick_tunnel_api_dns_check() == (True, "")
    with patch.object(qtd, "build_bootstrap", return_value=bootstrap), patch.object(
        qtd, "_api_tls_ready", return_value=False
    ):
        assert "api_tls_unreachable" in qtd.quick_tunnel_api_dns_check()[1]
    with patch.object(qtd, "build_bootstrap", return_value=bootstrap), patch.object(
        qtd, "_api_tls_ready", return_value=True
    ), patch.object(qtd, "_edge_transport_ready", return_value=False):
        assert "edge_transport_unreachable" in qtd.quick_tunnel_api_dns_check()[1]


def _valid_quick_response(secret: bytes = b"x" * 32) -> dict:
    return {
        "success": True,
        "result": {
            "id": "12345678-1234-4234-8234-123456789abc",
            "name": "",
            "hostname": "safe-slug.trycloudflare.com",
            "account_tag": "account-tag",
            "secret": base64.b64encode(secret).decode(),
        },
        "errors": [],
    }


def test_quick_api_response_is_validated_and_canonicalized_without_secret_in_errors():
    source = _valid_quick_response()
    canonical = qtd._canonicalize_quick_api_response(json.dumps(source).encode())
    assert json.loads(canonical) == source

    leaked = b"super-secret-should-never-appear"
    malformed = b'{"success":true,"result":{"secret":"' + leaked + b'"}'
    with pytest.raises(qtd.BootstrapError) as excinfo:
        qtd._canonicalize_quick_api_response(malformed)
    assert leaked.decode() not in str(excinfo.value)

    wrong_host = _valid_quick_response()
    wrong_host["result"]["hostname"] = "api.trycloudflare.com"
    with pytest.raises(qtd.BootstrapError, match="quick_api_schema_invalid"):
        qtd._canonicalize_quick_api_response(json.dumps(wrong_host).encode())


def test_quick_api_transport_uses_literal_ip_tls_hostname_and_redacts_upstream_body():
    calls = []
    good = json.dumps(_valid_quick_response()).encode()

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, good + b"\n__LINEBOT_HTTP_STATUS__:200", b""
        )

    body = qtd._request_quick_tunnel(("104.16.230.132",), runner=runner)
    assert json.loads(body)["result"]["hostname"] == "safe-slug.trycloudflare.com"
    command = calls[0]
    assert "api.trycloudflare.com:443:104.16.230.132" in command
    assert command[-1] == "https://api.trycloudflare.com/tunnel"
    assert not {"-k", "--insecure", "-L", "--location"}.intersection(command)

    secret = "do-not-log-this-secret"
    bad = (secret.encode() + b"\n__LINEBOT_HTTP_STATUS__:503")
    with pytest.raises(qtd.BootstrapError) as excinfo:
        qtd._request_quick_tunnel(
            ("104.16.230.132",),
            runner=lambda command, **kwargs: subprocess.CompletedProcess(
                command, 0, bad, b"upstream failed"
            ),
        )
    assert secret not in str(excinfo.value)


def test_metadata_is_atomic_mode_600_and_contains_no_tunnel_secret(tmp_path):
    metadata = tmp_path / "bootstrap.env"
    secret = b"not-written-anywhere"
    qtd._write_metadata(
        metadata,
        quick_service_url="http://127.0.0.1:43210/random-token-1234567890",
        edges=("198.41.200.13:7844", "198.41.192.7:7844"),
    )

    text = metadata.read_text()
    assert "QUICK_SERVICE_URL=http://127.0.0.1:43210/random-token-1234567890" in text
    assert text.count("EDGE=") == 2
    assert secret.decode() not in text
    assert metadata.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".bootstrap.env.*"))


def test_proxy_rejects_wrong_method_path_body_and_accepts_exact_post_once(tmp_path):
    bootstrap = qtd.QuickTunnelBootstrap(
        api_ips=("104.16.230.132",),
        edges=("198.41.200.13:7844", "198.41.192.7:7844"),
    )
    metadata = tmp_path / "metadata.env"

    with patch.object(qtd, "build_bootstrap", return_value=bootstrap):
        server = qtd._create_proxy_server(metadata, ttl=5)
    server.quick_tunnel_response_provider = lambda: json.dumps(
        _valid_quick_response()
    ).encode()

    try:
        import http.client
        import threading

        thread = threading.Thread(target=qtd._serve_proxy_until_consumed, args=(server, 5))
        thread.start()
        host, port = server.server_address
        token_path = metadata.read_text().split("QUICK_SERVICE_URL=", 1)[1].splitlines()[0]
        path = token_path.split(f"http://{host}:{port}", 1)[1] + "/tunnel"

        conn = http.client.HTTPConnection(host, port, timeout=2)
        conn.request("GET", path)
        assert conn.getresponse().status == 405
        conn.close()

        conn = http.client.HTTPConnection(host, port, timeout=2)
        conn.request("POST", "/wrong/tunnel")
        assert conn.getresponse().status == 404
        conn.close()

        conn = http.client.HTTPConnection(host, port, timeout=2)
        conn.request("POST", path, body=b"x")
        assert conn.getresponse().status == 400
        conn.close()

        conn = http.client.HTTPConnection(host, port, timeout=2)
        conn.request("POST", path)
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["success"] is True
        conn.close()
        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        server.server_close()
